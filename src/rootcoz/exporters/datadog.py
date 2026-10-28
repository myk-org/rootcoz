"""Datadog Events exporter: one event per failed test.

Emits one Datadog event per failed test (1:N, ADR 0003) to
``POST https://api.{site}/api/v1/events``. Credentials are API keys
only (32 hex chars, ``DD-API-KEY`` header): event submission is data
intake, which Datadog authenticates exclusively with API keys. Service
Access Tokens (``ddsat_…``) have no ``events_write`` scope — Bearer-auth
SATs get HTTP 403 on every event endpoint — and application keys
(``ddapp_…`` or exactly 40 hex chars) are read-only, so the constructor
fails fast on both with distinct explanatory messages.
Events carry the rootcoz classification (when the ``dd_push_classifications``
toggle is on) as a tag, title prefix, and text line. The exporter never
sets ``alert_type``: classification (root cause) and alert_type (severity)
are unrelated axes, so Datadog applies its own default.
Events carry a stable ``aggregation_key`` per analysis/test pair, a tag
taxonomy (``service``, ``env``, ``tfa.stage``, ``tfa.classification``,
job/build identity, and the push attribution) and structural attributes:
``@test.name`` (the verbatim pytest node id) plus ``@rootcoz.scope``
(``test``, or ``console`` for console-only analyses that have no test).
Parity with Report Portal: events also carry per-test history classifications
and reviewer attribution from the shared ``ExportContext``.

Failures never raise: per-event HTTP/network errors are collected into the
exporter result so a Datadog outage can never break the analysis pipeline.
Two abort modes stop a doomed run early: an auth rejection (HTTP 401/403)
aborts immediately — retrying with the same key cannot help — and a circuit
breaker stops the push after 5 consecutive failed events, recording the
remaining events as skipped.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from rootcoz import get_child_logger
from rootcoz.exporters.base import ExportContext, Exporter, ExporterResult
from rootcoz.storage import AI_SYSTEM_USERNAME

logger = get_child_logger(__name__)

#: Datadog Events API path on ``https://api.{site}``.
_EVENTS_PATH = "/api/v1/events"

#: Per-request timeout (seconds) for event submission.
_REQUEST_TIMEOUT = 30.0

#: Valid Datadog site hostname: lowercase alphanumerics, hyphens, and dots,
#: with at least one dot (``datadoghq.eu``, ``ddog.us``, ``us3.datadoghq.com``).
_SITE_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)

#: Datadog's v1 Events API rejects ``date_happened`` values older than its
#: ~18-hour intake window, so a manual push after a day of human review
#: would drop every event.  17 hours keeps a safety margin below that window:
#: older stored timestamps fall back to push time.
_MAX_EVENT_AGE_SECONDS = 17 * 3600

#: Datadog caps ``aggregation_key`` at 100 characters.
_AGGREGATION_KEY_MAX = 100

#: HTTP statuses meaning the API key was rejected: retrying cannot help.
_AUTH_FAILURE_STATUSES = frozenset({401, 403})

#: Consecutive failed events (any error) before the push circuit-breaks.
_CIRCUIT_BREAKER_THRESHOLD = 5

#: Fallback classification label when a stored failure carries none.
_UNKNOWN_CLASSIFICATION = "UNKNOWN"

#: Prefix of the synthetic failure message set by
#: ``CISource.run_console_only_analysis`` for console-only analyses.
_CONSOLE_ONLY_ERROR_PREFIX = "Console-only analysis"

#: ``rootcoz.scope`` attribute values (``@rootcoz.scope:test|console``).
_SCOPE_TEST = "test"
_SCOPE_CONSOLE = "console"

# Datadog tags allow alphanumerics, underscores, colons, periods, and
# minus. Every other character (spaces, most punctuation) is replaced
# with an underscore.
_TAG_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")

#: Datadog application keys (legacy format): exactly 40 hex characters.
_APPLICATION_KEY_RE = re.compile(r"^[0-9a-fA-F]{40}$")

#: Credential kind labels returned by :func:`classify_datadog_credential`.
_CREDENTIAL_API_KEY = "api_key"  # pragma: allowlist secret
_CREDENTIAL_SERVICE_TOKEN = "service_token"
_CREDENTIAL_APPLICATION_KEY = "application_key"

#: Fail-fast message for application keys: they are read-only and cannot
#: post events even when paired with an API key (Datadog docs). Shared by
#: the exporter constructor and the connection-test integration so both
#: surface the same explanation instead of a bare HTTP 403.
APPLICATION_KEY_ERROR = (
    "Datadog application keys cannot post events — use an API key (32 hex characters)"
)

#: Fail-fast message for Service Access Tokens: they have no
#: ``events_write`` scope, so Datadog rejects them (HTTP 403) on every
#: event endpoint — event submission is data intake, authenticated
#: exclusively with API keys. Shared by the exporter constructor and
#: the connection-test integration.
SERVICE_ACCESS_TOKEN_ERROR = (
    "Datadog service access tokens cannot post events — use an API key "
    "(32 hex characters)"
)


def classify_datadog_credential(credential: str) -> str:
    """Classify a Datadog credential by its shape.

    Whitespace is stripped before classifying. Unrecognized values fall
    back to API-key auth — the pre-existing behavior — so only the shapes
    Datadog documents are special-cased.

    Args:
        credential: Raw credential value.

    Returns:
        ``"api_key"`` (32 hex chars or any unrecognized value),
        ``"service_token"`` (``ddsat_…`` Service Access Token — cannot
        post events), or ``"application_key"`` (``ddapp_…`` prefix or
        exactly 40 hex chars — read-only, cannot post events).
    """
    value = (credential or "").strip()
    if value.startswith("ddsat_"):
        return _CREDENTIAL_SERVICE_TOKEN
    if value.startswith("ddapp_") or _APPLICATION_KEY_RE.fullmatch(value):
        return _CREDENTIAL_APPLICATION_KEY
    return _CREDENTIAL_API_KEY


def _auth_headers(credential: str) -> dict[str, str]:
    """Build the auth headers for Datadog Events API calls.

    API keys only: event submission is data intake, which Datadog
    authenticates exclusively with API keys (``DD-API-KEY`` header).
    Service Access Tokens and application keys never reach this helper —
    they are rejected earlier with their explanatory errors. Whitespace
    is stripped. Shared by the event POST and the connection-test
    integration so both authenticate identically.

    Args:
        credential: Raw credential value.

    Returns:
        Header dict for ``httpx`` requests.
    """
    return {"DD-API-KEY": (credential or "").strip()}


def _normalize_site(site: str | None) -> str:
    """Normalize a configured Datadog site value to a bare hostname.

    Accepts full URLs (``https://datadoghq.eu``), values with an ``api.``
    prefix, and trailing slashes. Returns an empty string when the value
    is empty or does not normalize to a valid hostname — the caller
    decides how to surface that (no silent fallback site: falling back
    would send the ``DD-API-KEY`` header to a site the operator never
    chose).

    Args:
        site: Raw configured site value.

    Returns:
        Bare lowercase hostname (e.g. ``datadoghq.eu``), or ``""`` when
        empty/invalid.
    """
    raw = (site or "").strip()
    if not raw:
        return ""
    candidate = raw.rstrip("/")
    if "://" in candidate:
        candidate = urlparse(candidate).hostname or ""
    elif "/" in candidate:
        candidate = urlparse(f"https://{candidate}").hostname or ""
    candidate = candidate.lower()
    candidate = candidate.removeprefix("api.")
    if not _SITE_HOSTNAME_RE.fullmatch(candidate):
        logger.warning(
            "Invalid Datadog site %r — no events can be emitted until it is fixed", raw
        )
        return ""
    return candidate


def _aggregation_key(job_id: str, test_name: str) -> str:
    """Build the aggregation key for one analysis/test pair.

    Datadog caps ``aggregation_key`` at 100 characters, which long pytest
    node IDs overflow. Keys that would exceed the cap keep the
    ``rootcoz-{job_id}`` prefix, truncate the test name to fit, and append
    the first 8 hex characters of the SHA-256 of the full test name so
    distinct long names never collide and the key is guaranteed ≤100.

    Args:
        job_id: Analysis job identifier.
        test_name: Failed test name.

    Returns:
        Aggregation key of at most :data:`_AGGREGATION_KEY_MAX` characters.
    """
    key = f"rootcoz-{job_id}-{test_name}"
    if len(key) <= _AGGREGATION_KEY_MAX:
        return key
    prefix = f"rootcoz-{job_id}-"
    digest = hashlib.sha256(test_name.encode("utf-8")).hexdigest()[:8]
    room = _AGGREGATION_KEY_MAX - len(prefix) - len(digest) - 1
    return f"{prefix}{test_name[: max(room, 0)]}-{digest}"


def _is_console_only_failure(failure: dict[str, Any], context: ExportContext) -> bool:
    """Whether a stored failure is a console-level synthetic one.

    Console-only analyses have no test to name, so two markers are checked:

    1. The synthetic failure message set by
       ``CISource.run_console_only_analysis`` — ``FailureAnalysis.error``
       stores the source message verbatim, so this also recognizes
       results that were stored before this marker was relied on.
    2. The placeholder name the console call sites synthesize, for stored
       results whose error text was rewritten: ``job_name`` (main.py) or
       ``f"{job_name}#{build_number}"`` (child jobs in
       jenkins_source.py).

    Args:
        failure: Stored failure dict.
        context: Export context with job metadata.

    Returns:
        ``True`` when the failure is a console-level synthetic one.
    """
    error = str(failure.get("error") or failure.get("error_message") or "")
    if error.startswith(_CONSOLE_ONLY_ERROR_PREFIX):
        return True
    placeholders = {context.job_name}
    if context.child_job_name:
        placeholders.add(f"{context.child_job_name}#{context.child_build_number}")
    return str(failure.get("test_name", "") or "") in placeholders


def _circuit_breaker_message(skipped: int) -> str:
    """Result message for a circuit-broken push."""
    return (
        f"Datadog push aborted after {_CIRCUIT_BREAKER_THRESHOLD} consecutive "
        f"failed events; {skipped} event(s) skipped"
    )


def sanitize_tag_value(value: str) -> str:
    """Sanitize a value for use inside a Datadog tag.

    Datadog tags cannot contain spaces or most punctuation. Disallowed
    characters are replaced with underscores (``PRODUCT BUG`` →
    ``PRODUCT_BUG``). An empty result collapses to ``unknown`` so the tag
    is never dropped for a missing value.

    Args:
        value: Raw value (classification, job name, test name, ...).

    Returns:
        Tag-safe value.
    """
    sanitized = _TAG_INVALID_CHARS.sub("_", value or "").strip("_")
    return sanitized or "unknown"


class DatadogExporter(Exporter):
    """Exporter that pushes one Datadog event per failed test.

    Args:
        api_key: Datadog API key (32 hex chars, ``DD-API-KEY`` header) —
            the only credential that can post events. Service Access
            Tokens (``ddsat_…``) and application keys are rejected.
        site: Datadog site, e.g. ``datadoghq.eu`` (default).
        service: Value of the ``service:`` tag.
        env: Value of the ``env:`` tag.
        push_classifications: Include the classification (tag, title prefix,
            and text line) in the event.
        push_rootcoz_url: Include the RootCoz report link in the event text.
        push_tracker_links: Include tracked-in Jira/GitHub issue links in
            the event text.

    Raises:
        ValueError: Invalid site, an application key credential
            (read-only — cannot post events), or a Service Access Token
            (no ``events_write`` scope — cannot post events).
    """

    NAME = "datadog"
    DISPLAY_NAME = "Datadog"

    #: Consumes tracked-in links from ``ExportContext.tracked_in_links``.
    needs_tracked_in_links = True

    #: Consumes per-test history classifications from
    #: ``ExportContext.history_classifications`` (same as Report Portal).
    needs_history_classifications = True

    def __init__(
        self,
        *,
        api_key: str,
        site: str,
        service: str | None = None,
        env: str | None = None,
        push_classifications: bool = True,
        push_rootcoz_url: bool = True,
        push_tracker_links: bool = True,
    ) -> None:
        self._api_key = (api_key or "").strip()
        _credential_kind = classify_datadog_credential(self._api_key)
        if _credential_kind == _CREDENTIAL_APPLICATION_KEY:
            raise ValueError(APPLICATION_KEY_ERROR)
        if _credential_kind == _CREDENTIAL_SERVICE_TOKEN:
            raise ValueError(SERVICE_ACCESS_TOKEN_ERROR)
        self._site = _normalize_site(site)
        if not self._site:
            raise ValueError(
                f"Invalid Datadog site {site!r} — set DATADOG_SITE to a Datadog "
                "hostname (e.g. datadoghq.eu)"
            )
        self._service = service
        self._env = env
        self._push_classifications = push_classifications
        self._push_rootcoz_url = push_rootcoz_url
        self._push_tracker_links = push_tracker_links

    # -- Exporter ABC ---------------------------------------------------------

    @property
    def name(self) -> str:
        """Machine-readable exporter identifier."""
        return self.NAME

    @property
    def display_name(self) -> str:
        """Human-readable exporter name."""
        return self.DISPLAY_NAME

    @property
    def is_enabled(self) -> bool:
        """Whether this client instance is ready to use.

        Always ``True`` for an instantiated client — construction requires
        an API key.  Enablement gating (``ENABLE_DATADOG`` + key presence)
        is handled by ``_create_exporter()`` via ``Settings.datadog_enabled``.
        """
        return True

    @property
    def _events_url(self) -> str:
        return f"https://api.{self._site}{_EVENTS_PATH}"

    def _build_tags(
        self, context: ExportContext, test_name: str, classification: str | None
    ) -> list[str]:
        """Build the tag list for one event.

        Tag order is stable so tests and consumers can rely on it.
        ``classification`` is ``None`` when the ``dd_push_classifications``
        toggle is off — the ``tfa.classification`` tag is then omitted.
        The test name is deliberately not a tag: a tag cannot hold brackets
        or spaces, so a sanitized value could never match ``@test.name`` —
        the verbatim name travels as the ``test.name`` attribute instead.
        ``test_name`` is still the key for the per-test lookups below.
        """
        tags: list[str] = []
        if self._service:
            tags.append(f"service:{sanitize_tag_value(self._service)}")
        if self._env:
            tags.append(f"env:{sanitize_tag_value(self._env)}")
        tags += [
            "tfa.stage:automated",
            f"job_name:{sanitize_tag_value(context.job_name)}",
            f"build_number:{sanitize_tag_value(context.build_number)}",
            f"rootcoz.analysis_id:{sanitize_tag_value(context.job_id)}",
        ]
        if classification:
            tags.append(f"tfa.classification:{sanitize_tag_value(classification)}")
        if context.child_job_name:
            tags.append(f"child_job_name:{sanitize_tag_value(context.child_job_name)}")
        tags.append(
            f"pushed_by:{sanitize_tag_value(context.pushed_by or AI_SYSTEM_USERNAME)}"
        )
        reviewer = context.reviewed_by.get(test_name, "")
        if reviewer:
            tags.append(f"reviewed_by:{sanitize_tag_value(reviewer)}")
        return tags

    def _build_event(
        self, context: ExportContext, failure: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the Datadog event payload for one failed test.

        Args:
            context: Export context with job metadata.
            failure: Stored failure dict (``test_name`` + ``analysis``).

        Returns:
            Datadog Events API payload.
        """
        test_name = str(failure.get("test_name", "") or "")
        analysis_value = failure.get("analysis")
        if isinstance(analysis_value, str):
            # Legacy stored failures carry the diagnosis as a plain string
            # (mirrors FailedTest._coerce_legacy_analysis in models.py).
            analysis: dict[str, Any] = {"details": analysis_value}
        elif isinstance(analysis_value, dict):
            analysis = analysis_value
        else:
            analysis = {}
        raw_classification = str(analysis.get("classification", "") or "").strip()
        if self._push_classifications:
            classification = raw_classification or _UNKNOWN_CLASSIFICATION
        else:
            # Toggle off: no classification anywhere in the event — no
            # tag, no title prefix, no text line.
            classification = ""
        details = str(analysis.get("details", "") or "").strip()

        console_only = _is_console_only_failure(failure, context)
        if console_only:
            # No test to name: the stored test_name is a synthesized
            # placeholder equal to the job name, so never show it as a test.
            subject = "build failure"
            heading = "### Build failure"
        else:
            subject = test_name
            heading = f"### {test_name}"
        build = f"Build #{context.build_number}"
        if classification:
            title = f"[{classification}] {subject} ({build})"
        else:
            title = f"{subject} ({build})"

        lines = [
            heading,
            "",
        ]
        if classification:
            lines.append(f"- **Classification:** {classification}")
        history_classification = context.history_classifications.get(test_name, "")
        if history_classification:
            # Parity with Report Portal: the previous classification for
            # this test name, when history has one. Not toggle-gated.
            lines.append(f"- **Failure pattern:** {history_classification}")
        lines.append(f"- **Job:** {context.job_name} (#{context.build_number})")
        if self._push_rootcoz_url and context.report_url:
            lines.append(
                f"- **RootCoz report:** [analysis {context.job_id}]({context.report_url})"
            )
        if context.jenkins_url:
            lines.append(
                f"- **CI build:** [build #{context.build_number}]({context.jenkins_url})"
            )
        if self._push_tracker_links:
            for link in context.tracked_in_links.get(test_name, []):
                url = str(link.get("tracked_in_url", "") or "")
                if not url:
                    continue
                label = {
                    "jira": "Jira",
                    "github": "GitHub",
                }.get(str(link.get("tracked_in_type", "") or ""), "issue")
                lines.append(f"- **Tracked in:** [{label}]({url})")
        reviewer = context.reviewed_by.get(test_name, "")
        if reviewer:
            # Parity with Report Portal: reviewer attribution, when the
            # test was human-reviewed. Not toggle-gated.
            lines.append(f"- **Reviewed by:** {reviewer}")
        if details:
            lines += ["", "**AI diagnosis:**", "", details]
        text = "\n".join(lines)
        # Datadog's v1 Events API renders markdown in ``text`` only when
        # it is wrapped in %%% markers (datadogpy#614, confirmed by
        # Datadog support) — always wrap, regardless of content toggles.
        text = f"%%%\n{text}\n%%%"

        now = int(time.time())
        date_happened = context.analysis_timestamp or now
        if now - date_happened > _MAX_EVENT_AGE_SECONDS:
            date_happened = now

        event: dict[str, Any] = {
            "title": title,
            "text": text,
            "priority": "normal",
            "date_happened": date_happened,
            "source_type_name": "rootcoz",
            "aggregation_key": _aggregation_key(context.job_id, test_name),
            "tags": self._build_tags(context, test_name, classification or None),
            "rootcoz": {
                "scope": _SCOPE_CONSOLE if console_only else _SCOPE_TEST,
            },
        }
        if not console_only:
            # Datadog indexes nested event attributes as ``@test.name``, so
            # the full pytest node id stays queryable verbatim (brackets,
            # colons and parametrize ids intact — a tag cannot hold them).
            test_attribute: dict[str, str] = {"name": test_name}
            suite = str(failure.get("suite", "") or "")
            if suite:
                test_attribute["suite"] = suite
            event["test"] = test_attribute
        return event

    async def push(self, context: ExportContext) -> ExporterResult:
        """Push one Datadog event per failed test.

        Iterates failures, emits 1:N events, and collects per-event
        results. HTTP/network errors never raise — they are recorded as
        per-event errors in the result details. Two abort modes stop a
        doomed run early: an auth rejection (HTTP 401/403) aborts the
        whole push immediately, and after
        :data:`_CIRCUIT_BREAKER_THRESHOLD` consecutive failed events the
        remaining events are skipped and counted in the result details.
        """
        failures = context.failures
        if not failures:
            return ExporterResult(
                success=False,
                message="No failures to push to Datadog.",
                details={"pushed": 0, "total": 0, "errors": [], "skipped": 0},
            )

        pushed = 0
        skipped = 0
        errors: list[str] = []
        consecutive_failures = 0
        abort_message: str | None = None
        headers = _auth_headers(self._api_key)
        logger.info(
            "Datadog push: emitting up to %d event(s) for job=%s to site=%s",
            len(failures),
            context.job_id,
            self._site,
        )
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            for index, failure in enumerate(failures):
                if not isinstance(failure, dict):
                    errors.append("Skipped non-dict failure entry")
                    consecutive_failures += 1
                    if consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                        skipped = len(failures) - index - 1
                        abort_message = _circuit_breaker_message(skipped)
                        break
                    continue
                test_name = failure.get("test_name", "") or "(unnamed test)"
                event = self._build_event(context, failure)
                try:
                    response = await client.post(
                        self._events_url, json=event, headers=headers
                    )
                    response.raise_for_status()
                    pushed += 1
                    consecutive_failures = 0
                    logger.debug(
                        "Datadog event accepted for test='%s', job=%s",
                        test_name,
                        context.job_id,
                    )
                except httpx.HTTPError as exc:
                    status = getattr(
                        getattr(exc, "response", None), "status_code", None
                    )
                    msg = f"Failed to emit Datadog event for '{test_name}'" + (
                        f" (HTTP {status})" if status else ""
                    )
                    errors.append(msg)
                    logger.error(
                        "Datadog push failed for test='%s', job=%s: %s",
                        test_name,
                        context.job_id,
                        exc,
                    )
                    if status in _AUTH_FAILURE_STATUSES:
                        # The key is bad — every remaining request would
                        # fail identically, so abort the whole push now.
                        skipped = len(failures) - index - 1
                        abort_message = (
                            f"Datadog rejected the API key (HTTP {status}) — "
                            f"push aborted, {skipped} event(s) skipped"
                        )
                        logger.error(
                            "Datadog push aborted: API key rejected (HTTP %s), job=%s",
                            status,
                            context.job_id,
                        )
                        break
                    consecutive_failures += 1
                    if consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                        # Outage: stop burning a 30s timeout per event.
                        skipped = len(failures) - index - 1
                        abort_message = _circuit_breaker_message(skipped)
                        logger.warning(
                            "Datadog push circuit-breaker tripped after %d "
                            "consecutive failures, job=%s, skipped=%d",
                            _CIRCUIT_BREAKER_THRESHOLD,
                            context.job_id,
                            skipped,
                        )
                        break

        success = pushed > 0 and not errors
        if abort_message:
            message = abort_message
        elif errors:
            message = errors[0]
        elif success:
            message = f"Pushed {pushed} Datadog event(s)"
        else:
            message = "Push completed with no events"

        return ExporterResult(
            success=success,
            message=message,
            details={
                "pushed": pushed,
                "total": len(failures),
                "errors": errors,
                "skipped": skipped,
            },
        )
