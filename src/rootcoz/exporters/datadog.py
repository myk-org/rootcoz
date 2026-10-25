"""Datadog Events exporter: one event per failed test.

Emits one Datadog event per failed test (1:N, ADR 0003) to
``POST https://api.{site}/api/v1/events`` with the ``DD-API-KEY`` header.
Events carry the rootcoz classification as the alert type, a stable
``aggregation_key`` per analysis/test pair, and a tag taxonomy
(``service``, ``env``, ``tfa.stage``, ``tfa.classification``, job/build/test
identity, and the push attribution).

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

#: rootcoz classification → Datadog ``alert_type``.
_ALERT_TYPE_MAP: dict[str, str] = {
    "PRODUCT BUG": "error",
    "INFRASTRUCTURE": "warning",
    "CODE ISSUE": "warning",
    "AUTOMATION": "warning",
}

#: Fallback alert type for UNKNOWN / unmapped classifications.
_DEFAULT_ALERT_TYPE = "info"

#: Fallback classification label when a stored failure carries none.
_UNKNOWN_CLASSIFICATION = "UNKNOWN"

# Datadog tags allow alphanumerics, underscores, colons, periods, and
# minus. Every other character (spaces, most punctuation) is replaced
# with an underscore.
_TAG_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")


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
        api_key: Datadog API key (``DD-API-KEY`` header).
        site: Datadog site, e.g. ``datadoghq.eu`` (default).
        service: Value of the ``service:`` tag.
        env: Value of the ``env:`` tag.
    """

    NAME = "datadog"
    DISPLAY_NAME = "Datadog"

    def __init__(
        self,
        *,
        api_key: str,
        site: str,
        service: str | None = None,
        env: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._site = _normalize_site(site)
        if not self._site:
            raise ValueError(
                f"Invalid Datadog site {site!r} — set DATADOG_SITE to a Datadog "
                "hostname (e.g. datadoghq.eu)"
            )
        self._service = service
        self._env = env

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
        self, context: ExportContext, classification: str, test_name: str
    ) -> list[str]:
        """Build the tag list for one event.

        Tag order is stable so tests and consumers can rely on it.
        """
        tags: list[str] = []
        if self._service:
            tags.append(f"service:{sanitize_tag_value(self._service)}")
        if self._env:
            tags.append(f"env:{sanitize_tag_value(self._env)}")
        tags += [
            "tfa.stage:automated",
            f"tfa.classification:{sanitize_tag_value(classification)}",
            f"job_name:{sanitize_tag_value(context.job_name)}",
            f"build_number:{sanitize_tag_value(context.build_number)}",
            f"test_name:{sanitize_tag_value(test_name)}",
            f"rootcoz.analysis_id:{sanitize_tag_value(context.job_id)}",
        ]
        if context.child_job_name:
            tags.append(f"child_job_name:{sanitize_tag_value(context.child_job_name)}")
        tags.append(
            f"pushed_by:{sanitize_tag_value(context.pushed_by or AI_SYSTEM_USERNAME)}"
        )
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
        classification = str(analysis.get("classification", "") or "").strip()
        if not classification:
            classification = _UNKNOWN_CLASSIFICATION
        details = str(analysis.get("details", "") or "").strip()

        title = f"[{classification}] {test_name} (Build #{context.build_number})"

        lines = [
            f"### {test_name}",
            "",
            f"- **Classification:** {classification}",
            f"- **Job:** {context.job_name} (#{context.build_number})",
        ]
        if context.report_url:
            lines.append(
                f"- **RootCoz report:** [analysis {context.job_id}]({context.report_url})"
            )
        if context.jenkins_url:
            lines.append(
                f"- **CI build:** [build #{context.build_number}]({context.jenkins_url})"
            )
        if details:
            lines += ["", "**AI diagnosis:**", "", details]
        text = "\n".join(lines)

        now = int(time.time())
        date_happened = context.analysis_timestamp or now
        if now - date_happened > _MAX_EVENT_AGE_SECONDS:
            date_happened = now

        return {
            "title": title,
            "text": text,
            "alert_type": _ALERT_TYPE_MAP.get(classification, _DEFAULT_ALERT_TYPE),
            "priority": "normal",
            "date_happened": date_happened,
            "source_type_name": "rootcoz",
            "aggregation_key": _aggregation_key(context.job_id, test_name),
            "tags": self._build_tags(context, classification, test_name),
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
        headers = {"DD-API-KEY": self._api_key}
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
