"""Datadog event exporter: one v2 event per failed test.

Emits one Datadog event per failed test (1:N, ADR 0003) to the v2 event
intake ``POST https://event-management-intake.{site}/api/v2/events``.
Intake requires **both** Datadog keys — ``DD-API-KEY`` (32 hex characters)
and ``DD-APPLICATION-KEY`` (``ddapp_…`` or 40 hex characters) — so the
constructor fails fast when either is missing. Service Access Tokens
(``ddsat_…``) cannot post events at all, and an application key in the API
key slot is reported as misplaced rather than as a rejected key.

Events carry the rootcoz classification (when the ``dd_push_classifications``
toggle is on) as a tag, title prefix, and message line. The exporter never
sets an alert type: classification (root cause) and severity are unrelated
axes. The v2 schema's ``category`` is a required enum of ``change`` |
``alert`` — there is no generic "event" category, and a failed test is an
alert.

Events carry a stable ``aggregation_key`` per analysis/test pair, a tag
taxonomy (``service``, ``env``, ``tfa.stage``, ``tfa.classification``,
job/build identity, and the push attribution) and free-form custom
attributes under ``attributes.custom``: ``test.name`` (the verbatim pytest
node id) for real tests, plus ``rootcoz.scope`` (``test``, or ``console``
for console-only analyses that have no test). Datadog documents
``attributes.custom`` as a free-form JSON object of up to 100 properties and
10 nesting levels; it does **not** document that nested keys become
``@``-searchable facets, so do not rely on ``@test.name``.
Parity with Report Portal: events also carry per-test history classifications
and reviewer attribution from the shared ``ExportContext``.

Failures never raise: per-event HTTP/network errors are collected into the
exporter result so a Datadog outage can never break the analysis pipeline.
Two abort modes stop a doomed run early: an auth rejection (HTTP 401/403)
aborts immediately — retrying with the same keys cannot help — and a circuit
breaker stops the push after 5 consecutive failed events, recording the
remaining events as skipped.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from rootcoz import get_child_logger
from rootcoz.exporters.base import ExportContext, Exporter, ExporterResult
from rootcoz.storage import AI_SYSTEM_USERNAME

logger = get_child_logger(__name__)

#: The v2 event intake lives on its own host, not on ``api.{site}``.
_EVENT_INTAKE_HOST = "event-management-intake.{site}"

#: Datadog v2 event intake path.
_EVENTS_PATH = "/api/v2/events"

#: Per-request timeout (seconds) for event submission.
_REQUEST_TIMEOUT = 30.0

#: Valid Datadog site hostname: lowercase alphanumerics, hyphens, and dots,
#: with at least one dot (``datadoghq.eu``, ``ddog.us``, ``us3.datadoghq.com``).
_SITE_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)

#: Datadog's v2 intake rejects events whose ``timestamp`` is older than its
#: ~18-hour window, so a manual push after a day of human review would drop
#: every event.  17 hours keeps a safety margin below that window: older
#: stored timestamps fall back to push time.
_MAX_EVENT_AGE_SECONDS = 17 * 3600

#: ISO 8601 rendering the v2 intake expects (UTC, second precision).
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Datadog caps ``aggregation_key`` at 100 characters.
_AGGREGATION_KEY_MAX = 100

#: Datadog caps ``title`` at 500 characters.
_TITLE_MAX = 500

#: Datadog caps ``message`` at 4000 characters.
_MESSAGE_MAX = 4000

#: Marker appended when a field must be capped at a Datadog limit, so a
#: truncated title or message is never silent.
_TRUNCATION_MARKER = "… [truncated]"

#: v2 ``category`` is a required enum of ``change`` | ``alert``.  There is no
#: generic "event" category, and a failed test is an alert.
_EVENT_CATEGORY = "alert"

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

#: Datadog API keys: exactly 32 hex characters.
_API_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")

#: Datadog application keys (legacy format): exactly 40 hex characters.
_APPLICATION_KEY_RE = re.compile(r"^[0-9a-fA-F]{40}$")

#: Credential kind labels returned by :func:`classify_datadog_credential`.
_CREDENTIAL_API_KEY = "api_key"  # pragma: allowlist secret
_CREDENTIAL_SERVICE_TOKEN = "service_token"
_CREDENTIAL_APPLICATION_KEY = "application_key"

#: Fail-fast message for an application key in the API key slot: it belongs
#: in the application key slot, not here. Shared by the exporter
#: constructor and the connection-test integration.
APPLICATION_KEY_ERROR = (
    "Datadog application keys cannot post events — that looks like an"
    " application key, put it in the application key setting (DATADOG_APP_KEY);"
    " DATADOG_API_KEY must be an API key (32 hex characters)"
)

#: Fail-fast message for an API key in the application key slot.
API_KEY_IN_APP_SLOT_ERROR = (
    "Datadog application key looks like an API key (32 hex characters) — put it"
    " in the API key setting (DATADOG_API_KEY); DATADOG_APP_KEY must be an"
    " application key (ddapp_… or 40 hex characters)"
)

#: Fail-fast message for a Service Access Token in the application key slot.
APP_KEY_SERVICE_TOKEN_ERROR = (
    "Datadog service access tokens cannot post events and are not application"
    " keys — set DATADOG_APP_KEY to an application key (ddapp_… or 40 hex"
    " characters)"
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
        exactly 40 hex chars — belongs in the application key slot).
    """
    value = (credential or "").strip()
    if value.startswith("ddsat_"):
        return _CREDENTIAL_SERVICE_TOKEN
    if value.startswith("ddapp_") or _APPLICATION_KEY_RE.fullmatch(value):
        return _CREDENTIAL_APPLICATION_KEY
    return _CREDENTIAL_API_KEY


def _auth_headers(api_key: str, app_key: str) -> dict[str, str]:
    """Build the headers for Datadog API calls.

    The v2 event intake authenticates with both keys at once: the API key
    (``DD-API-KEY``) and the application key (``DD-APPLICATION-KEY``).
    Whitespace is stripped from both. Shared by the event POST and the
    connection-test integration so both authenticate identically.

    Args:
        api_key: Datadog API key (32 hex characters).
        app_key: Datadog application key (``ddapp_…`` or 40 hex characters).

    Returns:
        Header dict for ``httpx`` requests.
    """
    return {
        "DD-API-KEY": (api_key or "").strip(),
        "DD-APPLICATION-KEY": (app_key or "").strip(),
        "Accept": "application/json",
    }


def validate_datadog_credentials(
    *, api_key: str, app_key: str, site: str | None
) -> str:
    """Validate the Datadog credential trio and normalize the site.

    Shared by :class:`DatadogExporter` and the Server Settings connection
    test, so configuration and the test button fail fast with the same
    messages instead of one reporting a shape the other accepts.

    Args:
        api_key: Value of the API key setting (``DD-API-KEY``).
        app_key: Value of the application key setting (``DD-APPLICATION-KEY``).
        site: Raw configured site value.

    Returns:
        Bare site hostname (e.g. ``datadoghq.eu``).

    Raises:
        ValueError: A missing or wrong-shaped piece, with a distinct message
            naming the setting to fix.
    """
    api = (api_key or "").strip()
    app = (app_key or "").strip()
    if not api:
        raise ValueError("datadog_api_key is required")
    api_kind = classify_datadog_credential(api)
    if api_kind == _CREDENTIAL_APPLICATION_KEY:
        raise ValueError(APPLICATION_KEY_ERROR)
    if api_kind == _CREDENTIAL_SERVICE_TOKEN:
        raise ValueError(SERVICE_ACCESS_TOKEN_ERROR)
    if not app:
        raise ValueError(
            "datadog_app_key is required — the v2 event intake authenticates with"
            " both an API key and an application key"
        )
    if classify_datadog_credential(app) == _CREDENTIAL_SERVICE_TOKEN:
        raise ValueError(APP_KEY_SERVICE_TOKEN_ERROR)
    if _API_KEY_RE.fullmatch(app):
        raise ValueError(API_KEY_IN_APP_SLOT_ERROR)
    site_host = _normalize_site(site)
    if not site_host:
        raise ValueError(
            f"Invalid Datadog site {site!r} — set DATADOG_SITE to a Datadog "
            "hostname (e.g. datadoghq.eu)"
        )
    return site_host


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


def _truncate(value: str, limit: int) -> str:
    """Cap a v2 text field at Datadog's documented maximum length.

    The cut is always visible — the marker makes the truncation explicit
    rather than silently dropping the tail of a title or message.

    Args:
        value: Assembled title or message.
        limit: Datadog's documented maximum length for the field.

    Returns:
        ``value`` unchanged when it fits, otherwise a marked truncation of
        exactly ``limit`` characters.
    """
    if len(value) <= limit:
        return value
    return value[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _is_console_only_failure(failure: dict[str, Any]) -> bool:
    """Whether a stored failure is a console-level synthetic one.

    Detection relies solely on the synthetic failure message that
    ``CISource.run_console_only_analysis`` writes: ``FailureAnalysis.error``
    stores the source message verbatim, so the marker also recognizes
    results stored before this check existed.

    Name-based heuristics (the synthesized placeholder name equal to
    ``job_name`` or ``f"{job_name}#{build_number}"``) were removed: a real
    structured test may legitimately share the job's name, and matching it
    would strip its ``test`` attribute and title.

    Args:
        failure: Stored failure dict.

    Returns:
        ``True`` when the failure is a console-level synthetic one.
    """
    error = str(failure.get("error") or failure.get("error_message") or "")
    return error.startswith(_CONSOLE_ONLY_ERROR_PREFIX)


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
        api_key: Datadog API key (32 hex chars, ``DD-API-KEY`` header).
        app_key: Datadog application key (``ddapp_…`` or 40 hex chars,
            ``DD-APPLICATION-KEY`` header) — the v2 intake requires it
            alongside the API key.
        site: Datadog site, e.g. ``datadoghq.eu`` (default).
        service: Value of the ``service:`` tag.
        env: Value of the ``env:`` tag.
        push_classifications: Include the classification (tag, title prefix,
            and message line) in the event.
        push_rootcoz_url: Include the RootCoz report link in the event message.
        push_tracker_links: Include tracked-in Jira/GitHub issue links in
            the event message.

    Raises:
        ValueError: A missing API key or application key, an application key
            in the API key slot, a Service Access Token (cannot post events),
            or an invalid site.
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
        app_key: str,
        site: str,
        service: str | None = None,
        env: str | None = None,
        push_classifications: bool = True,
        push_rootcoz_url: bool = True,
        push_tracker_links: bool = True,
    ) -> None:
        self._site = validate_datadog_credentials(
            api_key=api_key, app_key=app_key, site=site
        )
        self._api_key = (api_key or "").strip()
        self._app_key = (app_key or "").strip()
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
        both keys and a site.  Enablement gating (``ENABLE_DATADOG`` +
        credential presence) is handled by ``_create_exporter()`` via
        ``Settings.datadog_enabled``.
        """
        return True

    @property
    def _events_url(self) -> str:
        return f"https://{_EVENT_INTAKE_HOST.format(site=self._site)}{_EVENTS_PATH}"

    def _build_tags(
        self, context: ExportContext, test_name: str, classification: str | None
    ) -> list[str]:
        """Build the tag list for one event.

        Tag order is stable so tests and consumers can rely on it.
        ``classification`` is ``None`` when the ``dd_push_classifications``
        toggle is off — the ``tfa.classification`` tag is then omitted.
        The test name is deliberately not a tag: a tag cannot hold brackets
        or spaces, so a sanitized value could never match the verbatim
        ``test.name`` custom attribute.
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
            Datadog v2 event intake payload.
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
            # tag, no title prefix, no message line.
            classification = ""
        details = str(analysis.get("details", "") or "").strip()

        console_only = _is_console_only_failure(failure)
        if console_only:
            # No test to name: the stored test_name is the placeholder the
            # console-only call sites synthesize, so never show it as a test.
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
        message = "\n".join(lines)

        now = int(time.time())
        timestamp = context.analysis_timestamp or now
        if now - timestamp > _MAX_EVENT_AGE_SECONDS:
            timestamp = now

        # Free-form custom attributes. ``rootcoz.scope`` is always present;
        # ``test.name`` only for a real test, so a console-only analysis never
        # names the job as a test. The full pytest node id travels verbatim
        # (brackets, colons and parametrize ids intact — a tag cannot hold
        # them).
        custom: dict[str, Any] = {
            "rootcoz": {"scope": _SCOPE_CONSOLE if console_only else _SCOPE_TEST}
        }
        if not console_only:
            custom["test"] = {"name": test_name}

        return {
            "data": {
                "type": "event",
                "attributes": {
                    "title": _truncate(title, _TITLE_MAX),
                    "message": _truncate(message, _MESSAGE_MAX),
                    "category": _EVENT_CATEGORY,
                    "timestamp": datetime.fromtimestamp(timestamp, UTC).strftime(
                        _TIMESTAMP_FORMAT
                    ),
                    "aggregation_key": _aggregation_key(context.job_id, test_name),
                    "tags": self._build_tags(
                        context, test_name, classification or None
                    ),
                    "attributes": {"custom": custom},
                },
            }
        }

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
        headers = _auth_headers(self._api_key, self._app_key)
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
                            f"Datadog rejected the credentials (HTTP {status}) — "
                            f"push aborted, {skipped} event(s) skipped"
                        )
                        logger.error(
                            "Datadog push aborted: credentials rejected (HTTP %s),"
                            " job=%s",
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
