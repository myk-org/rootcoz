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
   human.  A review recorded by the AI system identity
   (``storage.AI_SYSTEM_USERNAME``) never produces a waiver: lifting a
   gate requires a human decision, not the AI reviewing its own
   analysis.  There is no setting that re-enables it.

Deployment prerequisites (documented per issue #183):

* WaiverDB write permissions for ``rootcoz.*`` testcases — without them
  waiver submissions fail with HTTP 403 (result pushes still succeed).
* ``GREENWAVE_WAIVER_TOKEN`` (bearer auth mode only) must be an OIDC bearer / service-account
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

from rootcoz.config import Settings, normalize_greenwave_auth_mode
from rootcoz.exporters.base import (
    ExportContext,
    Exporter,
    ExporterPrerequisiteError,
    ExporterResult,
    ReviewersUnavailable,
)
from rootcoz.exporters.greenwave_auth import (
    GreenwaveAuth,
    build_greenwave_auth,
    build_greenwave_waiver_auth,
)
from rootcoz.models import FailureAnalysis
from rootcoz.storage import AI_SYSTEM_USERNAME
from rootcoz.url_utils import strip_url_userinfo_in_text
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


def _coerce_failure(raw: Any) -> tuple[FailureAnalysis | None, str]:
    """Parse one stored failure entry.

    Single place that turns the raw result-JSON entries into models — used by
    the push loop and by the pre-flight waiver check, so the two can never
    disagree about which failures this push will act on.

    Returns:
        ``(failure, "")`` on success, or ``(None, reason)`` when the entry is
        not a usable object; ``reason`` is a field-location summary that never
        echoes the rejected input.
    """
    if not isinstance(raw, dict):
        return None, "entry is not an object"
    try:
        return FailureAnalysis(**raw), ""
    except ValidationError as exc:
        return None, _validation_error_summary(exc)


def _effective_classification(failure: FailureAnalysis, context: ExportContext) -> str:
    """Resolve a failure's classification for gating writes.

    Human overrides in ``context.effective_classifications`` (from
    ``storage.get_all_effective_classifications``) win over the stored
    analysis JSON — the override table is authoritative, while the
    result-JSON copy is patched best-effort.  The lookup key matches the
    storage function's scope keys exactly: root-scope failures look up
    with the empty child values storage returns for root overrides, so
    a child-scoped override for the same test name can never collide
    with a root-scope lookup.
    """
    override = context.effective_classifications.get(
        (
            failure.test_name,
            context.child_job_name or "",
            context.child_build_number or 0,
        )
    )
    return (override or failure.analysis.classification).strip().upper()


def _sanitize_error(exc: Exception) -> str:
    """Reduce an exception to a safe message for result details.

    Control characters are stripped and URL userinfo is redacted so
    credential-bearing URLs cannot leak into stored details.  The full
    message is preserved — no arbitrary truncation.
    """
    message = str(exc) or exc.__class__.__name__
    return strip_url_userinfo_in_text(strip_control_chars(message))


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
            required Greenwave configuration (ResultsDB URL + the selected
            auth method's fields, plus the waiver settings when
            ``GREENWAVE_PUSH_WAIVERS`` is enabled) and raises
            :class:`ExporterPrerequisiteError` when anything is missing;
            enablement gating (``enable_greenwave``) is handled by
            ``_create_exporter()``.

    Raises:
        ExporterPrerequisiteError: ``GREENWAVE_URL`` or the selected
            ``greenwave_auth_mode``'s fields are missing, or
            ``GREENWAVE_PUSH_WAIVERS`` is enabled but the waiver settings
            are incomplete (missing waiver URL, waiver token, or
            ``GREENWAVE_PRODUCT_VERSION``).  Mapped to HTTP 422 by the push
            endpoint — the server configuration itself is the missing input.
    """

    NAME = "greenwave"
    DISPLAY_NAME = "Greenwave"

    #: ResultsDB results carry ``subject_type`` (e.g. koji_build), so the
    #: pushed subject must identify a build/artifact — never a CI job
    #: name.  The automatic push path supplies no explicit subject
    #: identifier, so it skips this exporter entirely (see
    #: ``Exporter.requires_subject_identifier``).
    requires_subject_identifier = True

    #: Reads ``ExportContext.effective_classifications`` — human
    #: overrides are authoritative for gating writes.
    needs_effective_classifications = True

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
        # WaiverDB carries its own token only in bearer mode; every other
        # mode authenticates the waiver write with the selected strategy
        # (see build_greenwave_waiver_auth), so a token is neither required
        # nor used there.
        if normalize_greenwave_auth_mode(
            settings.greenwave_auth_mode
        ) == "bearer" and not (
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
        # Single source of truth for the selected auth method's fields —
        # the same check Settings.greenwave_enabled and the admin test
        # connection use. Also fails fast on a missing optional pyspnego
        # dependency (kerberos mode).
        self._auth: GreenwaveAuth = build_greenwave_auth(settings)
        self._url = settings.greenwave_url.rstrip("/")
        waiver_url = settings.greenwave_waiver_url
        self._waiver_url = waiver_url.rstrip("/") if waiver_url else None
        self._push_waivers = settings.greenwave_push_waivers
        # WaiverDB auth follows the selected mode (bearer mode uses its own
        # waiver token) — built only when waivers are on, so a results-only
        # deployment never needs waiver credentials.
        # Share ONE strategy across both targets, except for the modes whose
        # credentials are bound to the target host (kerberos SPN, mTLS
        # transport) — WaiverDB is a different host, so reusing the ResultsDB
        # strategy would present the wrong service principal. Sharing still
        # matters for oidc/basic/none, where a second instance would run a
        # second token flow and keep two caches for one client.
        if not self._push_waivers:
            self._waiver_auth: GreenwaveAuth | None = None
        elif normalize_greenwave_auth_mode(settings.greenwave_auth_mode) in (
            "bearer",
            "kerberos",
            "mtls",
        ):
            self._waiver_auth = build_greenwave_waiver_auth(settings)
        else:
            self._waiver_auth = self._auth
        self._waivable_classifications = settings.greenwave_waivable_set
        self._outcome_by_classification = settings.greenwave_outcome_map_parsed
        self._subject_type = settings.greenwave_subject_type
        self._product_version = settings.greenwave_product_version

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
        kwargs = self._auth.client_kwargs()
        kwargs["timeout"] = _DEFAULT_TIMEOUT
        return kwargs

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        json: dict[str, Any],
        label: str = "",
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        """POST JSON with the selected auth strategy and shared timeouts.

        Successful and failed writes are logged at debug level (label,
        URL, status code) so pushes — including auto-pushes — can be
        traced.  Tokens and payloads are never logged.  ResultsDB writes
        pass the mode's strategy; WaiverDB writes pass the waiver strategy,
        which is the same one except in bearer mode (separate token).
        """
        response = await client.post(
            url,
            json=json,
            auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
        )
        logger.debug(
            "Greenwave %s: POST %s -> HTTP %s",
            label or "request",
            url,
            response.status_code,
        )
        return response

    # -- Push ------------------------------------------------------------------

    def _has_waivable_failure(self, context: ExportContext) -> bool:
        """Whether any failure in this push would have been waived.

        Used to keep a reviewer-table outage from blocking a push that never
        intended a waiver: the refusal is only justified when the missing
        reviewer identity could have changed the outcome.
        """
        return any(
            parsed is not None
            and (classification := _effective_classification(parsed, context))
            and classification in self._waivable_classifications
            # The push loop skips a classification with no outcome mapping
            # BEFORE it considers a waiver, so such a failure can never be
            # waived — counting it here would block the mapped failures in the
            # same push for nothing.
            and classification in self._outcome_by_classification
            for parsed, _reason in (_coerce_failure(raw) for raw in context.failures)
        )

    async def push(self, context: ExportContext) -> ExporterResult:
        """Push analysis results to ResultsDB and optionally WaiverDB.

        When waiver submission is enabled, an unavailable reviewer lookup
        refuses the push BEFORE any write: an empty ``reviewed_by`` cannot be
        distinguished from "nobody reviewed this", and pushing results while
        the requested waivers are silently dropped is the failure this
        exporter exists to avoid.

        An explicit ``subject_identifier`` is required for every push — results
        and waivers are both gating writes, keyed on that subject. There is no
        ``job_name`` fallback: a write keyed on a CI job name is a malformed
        gating write, and the automatic push pipeline skips this exporter for
        the same reason (see ``requires_subject_identifier``).

        Each failure's classification is resolved via
        :func:`_effective_classification` — a human override in
        ``context.effective_classifications`` wins over the stored
        analysis JSON — and that single resolved value drives the
        outcome mapping, the waivable-classification gate, and the
        waiver comment, so a failure can never be pushed under one
        classification and waived under another.
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

        # "Reviewers unknown" is not "nobody reviewed this": with an empty
        # reviewed_by map the waiver gate cannot tell a human review from no
        # review at all. Refuse — but only when this push would actually have
        # tried to waive something (waivable classification present); a
        # results-only push has nothing to lose from the outage and must not
        # be blocked by it. ``ReviewersUnavailable`` maps to 503: the request
        # and the configuration are fine, the server could not read its own
        # data, so a retry is the right response.
        if (
            self._push_waivers
            and context.reviews_unavailable
            and self._has_waivable_failure(context)
        ):
            raise ReviewersUnavailable(
                "Greenwave refused the push: waiver submission is enabled and "
                "this push contains waivable failures, but the reviewer lookup "
                "failed, so human-reviewed failures cannot be identified. No "
                "results or waivers were written; retry once the review data "
                "is readable."
            )

        subject_identifier = normalize_identifier(context.subject_identifier) or ""
        if not subject_identifier:
            # Both writes are gating writes: a ResultsDB result carries
            # subject_type (koji_build by default) and a waiver is keyed on
            # the same subject, so a CI job name is not an acceptable
            # substitute for either. Refusing here is what makes the
            # "never a job name as a gating subject" rule true in every
            # configuration, not only when waivers are enabled.
            raise ExporterPrerequisiteError(
                "Greenwave requires an explicit subject_identifier (build NVR "
                "or artifact identifier); refusing to fall back to job_name "
                "for gating writes."
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
            failure, reason = _coerce_failure(raw_failure)
            if failure is None:
                errors.append(f"failure[{index}]: {reason}")
                logger.warning(
                    "Greenwave push: failure[%d] for job_id=%s is not usable: %s",
                    index,
                    context.job_id,
                    reason,
                )
                continue
            classification = _effective_classification(failure, context)
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
                        auth=self._auth.auth,
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
    ) -> None:
        """Result + waiver writes for one pre-validated failure.

        Mutates *outcome* in place; raises only on unexpected (non-HTTP)
        failures, which :meth:`_push_failure` converts into per-failure
        error outcomes.
        """
        testcase_name = _testcase_name(context.job_name, failure.test_name)
        note = f"rootcoz classification: {classification}"
        if failure.analysis.details:
            # The details are free text produced by the model from CI output:
            # sanitize them like every other field that reaches the gating
            # systems (Cc/Cf code points would otherwise survive into the
            # ResultsDB note and the waiver comment).
            note += f" — {strip_invisible_chars(failure.analysis.details)}"

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
                    auth=self._auth.auth,
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

        # Optional waiver for HUMAN-reviewed failures only.
        if not self._push_waivers:
            return
        if classification not in self._waivable_classifications:
            return
        reviewer = context.reviewed_by.get(failure.test_name, "")
        if not reviewer:
            return
        if reviewer == AI_SYSTEM_USERNAME:
            # Never waive an auto-review. A waiver is a gating write that
            # lifts a gate, so its justification must be a human decision;
            # rootcoz auto-reviewing its own analysis is not one. There is
            # deliberately no setting to re-enable this — the results push
            # above still records the failure, it is only the waiver that
            # needs a person.
            logger.info(
                "Greenwave waiver skipped for job_id=%s testcase=%s: "
                "reviewed by %s (auto-review), not a human",
                context.job_id,
                testcase_name,
                AI_SYSTEM_USERNAME,
            )
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
                strip_invisible_chars(failure.analysis.details),
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
                    auth=self._waiver_auth.auth if self._waiver_auth else None,
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
