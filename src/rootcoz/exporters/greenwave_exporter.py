"""Greenwave/ResultsDB exporter plugin.

Pushes rootcoz failure classifications to `ResultsDB`_ (API v2.0) and,
optionally, human-reviewed waivers to `WaiverDB`_ (API v1.0).

Push flow:

1. Create one ResultsDB group for the analysis job (``POST /groups``).
   A group-creation failure aborts the push before any result writes —
   the group UUID is what correlates all results of one analysis.
2. Post each classified failure as a ResultsDB result referencing the
   group UUID (``POST /results``).  The classification is mapped to a
   ResultsDB outcome via ``GREENWAVE_OUTCOME_MAP``; unclassified or
   unmapped failures are skipped.
3. When ``GREENWAVE_PUSH_WAIVERS`` is enabled and the failure's
   classification is listed in ``GREENWAVE_WAIVABLE_CLASSIFICATIONS``,
   submit a waiver to WaiverDB for failures that were reviewed by a
   human.  Waivers are never auto-submitted for reviews recorded by the
   AI system identity (``storage.AI_SYSTEM_USERNAME``) unless
   ``GREENWAVE_ALLOW_AI_WAIVERS`` is set.

Deployment prerequisites (documented per issue #183):

* WaiverDB write permissions for ``rootcoz.*`` testcases — without them
  waiver submissions fail with HTTP 403 (result pushes still succeed).
* ``GREENWAVE_WAIVER_TOKEN`` must be an OIDC bearer / service-account
  token accepted by WaiverDB; the ResultsDB token is separate
  (``GREENWAVE_API_TOKEN``).

.. _ResultsDB: https://resultsdb20.api.fedoraproject.org/
.. _WaiverDB: https://waiverdb.fedoraproject.org/
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError
from simple_logger.logger import get_logger

from rootcoz.config import Settings
from rootcoz.exporters.base import (
    ExportContext,
    Exporter,
    ExporterPrerequisiteError,
    ExporterResult,
)
from rootcoz.models import FailureAnalysis
from rootcoz.storage import AI_SYSTEM_USERNAME
from rootcoz.url_utils import redact_url_userinfo
from rootcoz.utils import (
    normalize_identifier,
    strip_control_chars,
    strip_invisible_chars,
)

logger = get_logger(name=__name__)

#: HTTP timeout for ResultsDB/WaiverDB calls, mirroring the Report Portal
#: exporter's default request timeout.
_DEFAULT_TIMEOUT = 30.0

#: Maximum in-flight ResultsDB/WaiverDB requests.  Result pushes for a
#: single analysis are independent HTTP writes; a bound keeps large
#: analyses from opening one connection-borrowing request per failure
#: while still avoiding O(failures) serial wall-clock time.
_MAX_CONCURRENT_WRITES = 8


def _safe_json_identifier(resp: httpx.Response, key: str) -> Any:
    """Safely extract an identifier field from a JSON object response."""
    try:
        body = resp.json()
        return body.get(key) if isinstance(body, dict) else None
    except ValueError:
        return None


def _positive_integer_response_id(resp: httpx.Response) -> int | None:
    """Extract the required positive integer top-level ``id`` field."""
    identifier = _safe_json_identifier(resp, "id")
    if (
        isinstance(identifier, int)
        and not isinstance(identifier, bool)
        and identifier > 0
    ):
        return identifier
    return None


def _uuid_response_id(resp: httpx.Response) -> str | None:
    """Extract and normalize the required UUID ``uuid`` response field."""
    identifier = _safe_json_identifier(resp, "uuid")
    if not isinstance(identifier, str):
        return None
    try:
        return str(UUID(identifier))
    except ValueError:
        return None


def _testcase_name(job_name: str, test_name: str) -> str:
    """Build the ResultsDB/WaiverDB testcase name for a failure.

    Format: ``rootcoz.{job_name}.{test_name}`` (per issue #183).  ASCII
    control characters and Unicode invisible characters are stripped from
    both parts so stored failure payloads cannot smuggle malformed
    testcase identifiers.
    """
    job = strip_invisible_chars(job_name).strip()
    test = strip_invisible_chars(test_name).strip()
    return f"rootcoz.{job}.{test}"


def _build_waiver_comment(
    classification: str,
    details: str,
    pushed_by: str,
    reviewer: str,
    user_comment: str | None = None,
) -> str:
    """Build a WaiverDB comment with consistent reviewer attribution."""
    normalized_user_comment = user_comment.strip() if user_comment else ""
    prefix = f"{pushed_by}: " if pushed_by else ""
    if normalized_user_comment:
        comment = f"{prefix}{normalized_user_comment} — rootcoz: {classification}"
        details_separator = ", "
    else:
        comment = f"{prefix}Waived by rootcoz: {classification}"
        details_separator = " — "
    if details:
        comment += f"{details_separator}{details}"
    if reviewer:
        comment += f", reviewed by {reviewer}"
    return comment


def _sanitize_error(exc: Exception) -> str:
    """Reduce an exception to a safe message for result details.

    Control characters are stripped and URL userinfo is redacted so
    credential-bearing URLs cannot leak into stored details.  The full
    message is preserved — no arbitrary truncation.
    """
    message = str(exc) or exc.__class__.__name__
    return redact_url_userinfo(strip_control_chars(message))


def _validation_error_summary(exc: ValidationError) -> str:
    """Human-readable, input-free summary of a pydantic ``ValidationError``.

    Only field locations and messages are included — never the rejected
    input values, which can be arbitrarily large or sensitive.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(loc) for loc in err.get("loc", ()))
        parts.append(f"{loc or '<root>'}: {err.get('msg', 'invalid value')}")
    return "; ".join(parts) or "schema validation failed"


class GreenwaveExporter(Exporter):
    """Exporter pushing classifications to ResultsDB and waivers to WaiverDB.

    Args:
        settings: Application settings.  Construction validates the
            required Greenwave configuration (ResultsDB URL + API token,
            plus the waiver settings when ``GREENWAVE_PUSH_WAIVERS`` is
            enabled) and raises :class:`ExporterPrerequisiteError` when
            anything is missing; enablement gating (``enable_greenwave``)
            is handled by ``_create_exporter()``.

    Raises:
        ExporterPrerequisiteError: ``GREENWAVE_URL`` or ``GREENWAVE_API_TOKEN``
            is missing, or ``GREENWAVE_PUSH_WAIVERS`` is enabled but the
            waiver settings are incomplete (missing waiver URL, waiver
            token, or ``GREENWAVE_PRODUCT_VERSION``).  Mapped to HTTP 422 by
            the push endpoint — the server configuration itself is the
            missing input.
    """

    NAME = "greenwave"
    DISPLAY_NAME = "Greenwave"

    @classmethod
    def missing_waiver_settings(cls, settings: Settings) -> list[str]:
        """WaiverDB settings missing while ``GREENWAVE_PUSH_WAIVERS`` is on.

        Single source of truth for waiver prerequisites — used by the
        exporter availability listing (via ``_greenwave_ready()``) and by
        ``__init__`` so both paths always agree.
        """
        missing: list[str] = []
        if not settings.greenwave_waiver_url:
            missing.append("GREENWAVE_WAIVER_URL")
        if not (
            settings.greenwave_waiver_token
            and settings.greenwave_waiver_token.get_secret_value()
        ):
            missing.append("GREENWAVE_WAIVER_TOKEN")
        if not settings.greenwave_product_version:
            missing.append("GREENWAVE_PRODUCT_VERSION")
        return missing

    def __init__(self, settings: Settings) -> None:
        if not settings.greenwave_url:
            raise ExporterPrerequisiteError(
                "Greenwave requires GREENWAVE_URL (ResultsDB API URL) to be configured"
            )
        if not (
            settings.greenwave_api_token
            and settings.greenwave_api_token.get_secret_value()
        ):
            raise ExporterPrerequisiteError(
                "Greenwave requires GREENWAVE_API_TOKEN to be configured"
            )
        self._url = settings.greenwave_url.rstrip("/")
        api_token = settings.greenwave_api_token
        self._api_token = (
            api_token.get_secret_value() if api_token is not None else None
        )
        waiver_url = settings.greenwave_waiver_url
        self._waiver_url = waiver_url.rstrip("/") if waiver_url else None
        waiver_token = settings.greenwave_waiver_token
        self._waiver_token = (
            waiver_token.get_secret_value() if waiver_token is not None else None
        )
        self._push_waivers = settings.greenwave_push_waivers
        self._waivable_classifications = settings.greenwave_waivable_set
        self._allow_ai_waivers = settings.greenwave_allow_ai_waivers
        self._outcome_by_classification = settings.greenwave_outcome_map_parsed
        self._subject_type = settings.greenwave_subject_type
        self._product_version = settings.greenwave_product_version
        self._verify_ssl = settings.greenwave_verify_ssl

        if self._push_waivers:
            missing = self.missing_waiver_settings(settings)
            if missing:
                raise ExporterPrerequisiteError(
                    "GREENWAVE_PUSH_WAIVERS is enabled but required setting(s) "
                    f"missing: {', '.join(missing)}"
                )

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
        """Whether this exporter instance is ready to use.

        Always ``True`` for an instantiated exporter — construction
        requires a valid URL.  Enablement gating is handled by
        ``_create_exporter()`` which checks ``Settings.greenwave_enabled``
        before constructing the exporter.
        """
        return True

    # -- Internals -------------------------------------------------------------

    def _client_kwargs(self) -> dict[str, Any]:
        """Shared httpx.AsyncClient constructor arguments."""
        return {"timeout": _DEFAULT_TIMEOUT, "verify": self._verify_ssl}

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        token: str | None,
        json: dict[str, Any],
        label: str = "",
    ) -> httpx.Response:
        """POST JSON with an optional bearer token and shared timeouts.

        Successful and failed writes are logged at debug level (label,
        URL, status code) so pushes — including auto-pushes — can be
        traced.  Tokens and payloads are never logged.
        """
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = await client.post(url, json=json, headers=headers)
        logger.debug(
            "Greenwave %s: POST %s -> HTTP %s",
            label or "request",
            url,
            response.status_code,
        )
        return response

    # -- Push ------------------------------------------------------------------

    async def push(self, context: ExportContext) -> ExporterResult:
        """Push analysis results to ResultsDB and optionally WaiverDB.

        Waiver submissions require an explicit ``subject_identifier`` (build
        NVR or artifact identifier) — they never fall back to ``job_name``,
        because a waiver keyed on a CI job name is a malformed gating write.
        Result pushes fall back to ``job_name`` when no subject identifier
        was supplied.

        When ``context.skip_waivers`` is set and no subject identifier is
        available (the auto-push path), waivers are skipped and results are
        pushed best-effort instead of refusing the whole push.
        """
        if not context.failures:
            return ExporterResult(
                success=False,
                message="No failures to push to Greenwave.",
                details={
                    "pushed": 0,
                    "skipped": 0,
                    "waived": 0,
                    "errors": ["no failures"],
                    "details": {
                        "resultsdb_ids": [],
                        "waiver_ids": [],
                        "group_uuid": None,
                    },
                },
            )

        subject_identifier = normalize_identifier(context.subject_identifier) or ""
        skip_waivers = context.skip_waivers
        if not subject_identifier and self._push_waivers and not skip_waivers:
            raise ExporterPrerequisiteError(
                "Greenwave waiver submission requires an explicit "
                "subject_identifier (build NVR or artifact identifier); "
                "refusing to fall back to job_name for gating writes."
            )
        if not subject_identifier:
            subject_identifier = strip_control_chars(context.job_name).strip()
            if skip_waivers and self._push_waivers:
                logger.info(
                    "Skipping Greenwave waivers for job_id=%s: no "
                    "subject_identifier available (skip_waivers set)",
                    context.job_id,
                )

        pushed = 0
        skipped = 0
        waived = 0
        errors: list[str] = []
        resultsdb_ids: list[int] = []
        waiver_ids: list[int] = []
        group_uuid: str | None = None

        # Pre-pass: validate and classify every failure BEFORE any HTTP
        # write, so an analysis with no exportable failure never creates
        # an empty ResultsDB group.
        items: list[tuple[int, FailureAnalysis, str, str]] = []
        for index, raw_failure in enumerate(context.failures):
            if not isinstance(raw_failure, dict):
                errors.append(f"failure[{index}]: entry is not an object")
                continue
            try:
                failure = FailureAnalysis(**raw_failure)
            except ValidationError as exc:
                logger.warning(
                    "Greenwave push: failure[%d] for job_id=%s failed schema "
                    "validation: %s",
                    index,
                    context.job_id,
                    _validation_error_summary(exc),
                )
                errors.append(f"failure[{index}]: validation failed")
                continue
            classification = failure.analysis.classification.strip().upper()
            if not classification:
                skipped += 1
                continue
            mapped_outcome = self._outcome_by_classification.get(classification)
            if not mapped_outcome:
                skipped += 1
                continue
            items.append((index, failure, classification, mapped_outcome))

        if items:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                # 1. Create one ResultsDB group for the analysis job.  This is
                #    fatal: the group UUID is what correlates every result of
                #    one analysis, so pushing results without it would create
                #    untraceable ungrouped records while still reporting
                #    success.
                group_payload: dict[str, Any] = {
                    "description": f"rootcoz analysis {context.job_id}",
                    "ref_url": context.report_url or None,
                }
                try:
                    group_resp = await self._post(
                        client,
                        f"{self._url}/groups",
                        token=self._api_token,
                        json=group_payload,
                        label="ResultsDB group creation",
                    )
                    group_resp.raise_for_status()
                    group_uuid = _uuid_response_id(group_resp)
                    if group_uuid is None:
                        raise ValueError("group response missing 'uuid' field")
                except Exception as exc:  # noqa: BLE001
                    safe_error = _sanitize_error(exc)
                    logger.warning(
                        "ResultsDB group creation failed for job_id=%r: %s — "
                        "aborting push before any result writes",
                        context.job_id,
                        safe_error,
                    )
                    # Carry the pre-pass accounting (skipped counts,
                    # validation errors) into the failure response so
                    # operators get the complete picture of the attempted
                    # push, not just the group error.
                    return ExporterResult(
                        success=False,
                        message=(
                            "ResultsDB group creation failed; no results were pushed."
                        ),
                        details={
                            "pushed": 0,
                            "skipped": skipped,
                            "waived": 0,
                            "errors": [
                                *errors,
                                f"group creation failed: {safe_error}",
                            ],
                            "details": {
                                "resultsdb_ids": [],
                                "waiver_ids": [],
                                "group_uuid": None,
                            },
                        },
                    )
                logger.debug(
                    "ResultsDB group uuid=%s created for job_id=%s",
                    group_uuid,
                    context.job_id,
                )

                # 2+3. Push each classified failure (result + optional
                #     waiver) with bounded concurrency.  Outcomes are
                #     aggregated in input order, so errors and recorded
                #     identifiers stay deterministic regardless of
                #     completion order.
                semaphore = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)
                outcomes = await asyncio.gather(
                    *(
                        self._push_failure(
                            client,
                            semaphore,
                            item,
                            context=context,
                            subject_identifier=subject_identifier,
                            group_uuid=group_uuid,
                            skip_waivers=skip_waivers,
                        )
                        for item in items
                    )
                )
                for outcome in outcomes:
                    pushed += outcome["pushed"]
                    skipped += outcome["skipped"]
                    waived += outcome["waived"]
                    errors.extend(outcome["errors"])
                    resultsdb_ids.extend(outcome["resultsdb_ids"])
                    waiver_ids.extend(outcome["waiver_ids"])

        # WaiverDB errors produce partial success whenever any result
        # landed; result-write errors are isolated per failure so one bad
        # failure never aborts the rest.
        success = pushed > 0
        if pushed == 0 and skipped > 0 and not errors:
            message = (
                f"No results pushed ({skipped} skipped — no matching classifications)"
            )
        else:
            message = f"Pushed {pushed} result(s) to ResultsDB"
            if self._push_waivers:
                message += f", {waived} waiver(s)"
            if skipped:
                message += f"; {skipped} skipped"

        return ExporterResult(
            success=success,
            message=message,
            details={
                "pushed": pushed,
                "skipped": skipped,
                "waived": waived,
                "errors": errors,
                "details": {
                    "resultsdb_ids": resultsdb_ids,
                    "waiver_ids": waiver_ids,
                    "group_uuid": group_uuid,
                },
            },
        )

    async def _push_failure(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        item: tuple[int, FailureAnalysis, str, str],
        *,
        context: ExportContext,
        subject_identifier: str,
        group_uuid: str,
        skip_waivers: bool,
    ) -> dict[str, Any]:
        """Push one classified failure: ResultsDB result + optional waiver.

        Receives a pre-validated ``item`` — ``(index, failure,
        classification, outcome)`` — from :meth:`push`'s pre-pass.  Every
        failure mode, including unexpected exceptions anywhere in the
        task body, is caught and reported per failure so one bad entry
        never aborts the rest of the ``asyncio.gather()`` batch.  A waiver
        is only submitted after the corresponding result write succeeded.

        Returns an outcome dict with the failure's counters (``pushed``,
        ``skipped``, ``waived``), ``errors``, and written identifiers
        (``resultsdb_ids``, ``waiver_ids``).
        """
        outcome: dict[str, Any] = {
            "pushed": 0,
            "skipped": 0,
            "waived": 0,
            "errors": [],
            "resultsdb_ids": [],
            "waiver_ids": [],
        }
        index, failure, classification, outcome_for_class = item
        try:
            await self._push_failure_inner(
                client,
                semaphore,
                failure,
                classification,
                outcome_for_class,
                outcome,
                context=context,
                subject_identifier=subject_identifier,
                group_uuid=group_uuid,
                skip_waivers=skip_waivers,
            )
        except Exception as exc:
            safe_error = _sanitize_error(exc)
            logger.warning(
                "Greenwave push: unexpected error for failure[%d] job_id=%s: %s",
                index,
                context.job_id,
                safe_error,
                exc_info=True,
            )
            outcome["errors"].append(f"failure[{index}]: {safe_error}")
        return outcome

    async def _push_failure_inner(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        failure: FailureAnalysis,
        classification: str,
        outcome_for_class: str,
        outcome: dict[str, Any],
        *,
        context: ExportContext,
        subject_identifier: str,
        group_uuid: str,
        skip_waivers: bool,
    ) -> None:
        """Result + waiver writes for one pre-validated failure.

        Mutates *outcome* in place; raises only on unexpected (non-HTTP)
        failures, which :meth:`_push_failure` converts into per-failure
        error outcomes.
        """
        testcase_name = _testcase_name(context.job_name, failure.test_name)
        note = f"rootcoz classification: {classification}"
        if failure.analysis.details:
            note += f" — {failure.analysis.details}"

        payload: dict[str, Any] = {
            "testcase": {"name": testcase_name},
            "outcome": outcome_for_class,
            "note": note,
            "data": {
                "rootcoz_job_id": [context.job_id],
                "rootcoz_classification": [classification],
                "rootcoz_report_url": (
                    [context.report_url] if context.report_url else []
                ),
                "item": [subject_identifier],
                "type": [self._subject_type],
            },
            "groups": [group_uuid],
        }

        try:
            async with semaphore:
                result_resp = await self._post(
                    client,
                    f"{self._url}/results",
                    token=self._api_token,
                    json=payload,
                    label=f"ResultsDB result {testcase_name}",
                )
                result_resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            safe_error = _sanitize_error(exc)
            logger.warning(
                "ResultsDB result push failed for job_id=%s testcase=%s: %s",
                context.job_id,
                testcase_name,
                safe_error,
            )
            outcome["errors"].append(f"ResultsDB {testcase_name}: {safe_error}")
            return  # skip waiver when the result push failed

        outcome["pushed"] = 1
        result_id = _positive_integer_response_id(result_resp)
        if result_id is None:
            outcome["errors"].append(
                f"ResultsDB write for {testcase_name} succeeded but "
                "response had no usable 'id'"
            )
        else:
            outcome["resultsdb_ids"].append(result_id)
            logger.debug(
                "ResultsDB result id=%s written for job_id=%s testcase=%s",
                result_id,
                context.job_id,
                testcase_name,
            )

        # Optional waiver for human-reviewed failures.
        if not self._push_waivers or skip_waivers:
            return
        if classification not in self._waivable_classifications:
            return
        reviewer = context.reviewed_by.get(failure.test_name, "")
        if not reviewer:
            return
        if reviewer == AI_SYSTEM_USERNAME and not self._allow_ai_waivers:
            return

        assert self._waiver_url is not None
        assert self._product_version is not None
        waiver_payload = {
            "subject_type": self._subject_type,
            "subject_identifier": subject_identifier,
            "testcase": testcase_name,
            "waived": True,
            "product_version": self._product_version,
            "comment": _build_waiver_comment(
                classification,
                failure.analysis.details,
                context.pushed_by,
                reviewer,
                user_comment=context.waiver_comment,
            ),
        }
        try:
            async with semaphore:
                waiver_resp = await self._post(
                    client,
                    f"{self._waiver_url}/waivers/",
                    token=self._waiver_token,
                    json=waiver_payload,
                    label=f"WaiverDB waiver {testcase_name}",
                )
                waiver_resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            safe_error = _sanitize_error(exc)
            logger.warning(
                "WaiverDB waiver submission failed for job_id=%s testcase=%s: %s",
                context.job_id,
                testcase_name,
                safe_error,
            )
            outcome["errors"].append(f"WaiverDB {testcase_name}: {safe_error}")
            return

        outcome["waived"] = 1
        waiver_id = _positive_integer_response_id(waiver_resp)
        if waiver_id is None:
            outcome["errors"].append(
                f"WaiverDB write for {testcase_name} succeeded but "
                "response had no usable 'id'"
            )
        else:
            outcome["waiver_ids"].append(waiver_id)
            logger.debug(
                "WaiverDB waiver id=%s submitted for job_id=%s testcase=%s",
                waiver_id,
                context.job_id,
                testcase_name,
            )
        return
