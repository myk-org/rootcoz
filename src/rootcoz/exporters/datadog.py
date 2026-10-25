"""Datadog Events exporter: one event per failed test.

Emits one Datadog event per failed test (1:N, ADR 0003) to
``POST https://api.{site}/api/v1/events`` with the ``DD-API-KEY`` header.
Events carry the rootcoz classification as the alert type, a stable
``aggregation_key`` per analysis/test pair, and a tag taxonomy
(``service``, ``env``, ``tfa.stage``, ``tfa.classification``, job/build/test
identity, and the push attribution).

Failures never raise: per-event HTTP/network errors are collected into the
exporter result so a Datadog outage can never break the analysis pipeline.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from rootcoz import get_child_logger
from rootcoz.exporters.base import ExportContext, Exporter, ExporterResult
from rootcoz.storage import AI_SYSTEM_USERNAME

logger = get_child_logger(__name__)

#: Datadog Events API path on ``https://api.{site}``.
_EVENTS_PATH = "/api/v1/events"

#: Per-request timeout (seconds) for event submission.
_REQUEST_TIMEOUT = 30.0

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
        site: str = "datadoghq.eu",
        service: str = "cnv-qe-tests",
        env: str = "production",
    ) -> None:
        self._api_key = api_key
        self._site = (site or "datadoghq.eu").strip().rstrip("/")
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
        tags = [
            f"service:{sanitize_tag_value(self._service)}",
            f"env:{sanitize_tag_value(self._env)}",
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
        analysis = failure.get("analysis")
        analysis = analysis if isinstance(analysis, dict) else {}
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

        return {
            "title": title,
            "text": text,
            "alert_type": _ALERT_TYPE_MAP.get(classification, _DEFAULT_ALERT_TYPE),
            "priority": "normal",
            "date_happened": context.analysis_timestamp or int(time.time()),
            "source_type_name": "rootcoz",
            "aggregation_key": f"rootcoz-{context.job_id}-{test_name}",
            "tags": self._build_tags(context, classification, test_name),
        }

    async def push(self, context: ExportContext) -> ExporterResult:
        """Push one Datadog event per failed test.

        Iterates failures, emits 1:N events, and collects per-event
        results. HTTP/network errors never raise — they are recorded as
        per-event errors in the result details.
        """
        failures = context.failures
        if not failures:
            return ExporterResult(
                success=False,
                message="No failures to push to Datadog.",
                details={"pushed": 0, "total": 0, "errors": []},
            )

        pushed = 0
        errors: list[str] = []
        headers = {"DD-API-KEY": self._api_key}
        logger.info(
            "Datadog push: emitting up to %d event(s) for job=%s to site=%s",
            len(failures),
            context.job_id,
            self._site,
        )
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            for failure in failures:
                if not isinstance(failure, dict):
                    errors.append("Skipped non-dict failure entry")
                    continue
                test_name = failure.get("test_name", "") or "(unnamed test)"
                event = self._build_event(context, failure)
                try:
                    response = await client.post(
                        self._events_url, json=event, headers=headers
                    )
                    response.raise_for_status()
                    pushed += 1
                    logger.debug(
                        "Datadog event accepted for test='%s', job=%s",
                        test_name,
                        context.job_id,
                    )
                except httpx.HTTPError as exc:
                    # One failed event must not abort the remaining events.
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

        success = pushed > 0 and not errors
        if success:
            message = f"Pushed {pushed} Datadog event(s)"
        elif errors:
            message = errors[0]
        else:
            message = "Push completed with no events"

        return ExporterResult(
            success=success,
            message=message,
            details={"pushed": pushed, "total": len(failures), "errors": errors},
        )
