"""Greenwave integration for pushing rootcoz results to ResultsDB and WaiverDB."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import ValidationError
from simple_logger.logger import get_logger

from rootcoz.exporters.base import (
    ExportContext,
    Exporter,
    ExporterPrerequisiteError,
    ExporterResult,
)
from rootcoz.greenwave import (
    evaluate_greenwave_transport,
    normalize_greenwave_outcome_map,
    referenced_placeholders,
)
from rootcoz.storage import AI_SYSTEM_USERNAME
from rootcoz.utils import sanitize_control_chars

logger = get_logger(name=__name__)


def _safe_json_identifier(resp: httpx.Response, key: str) -> Any:
    """Safely extract an identifier field from a JSON object response."""
    try:
        body = resp.json()
        return body.get(key) if isinstance(body, dict) else None
    except ValueError:
        return None


def _positive_integer_response_id(resp: httpx.Response) -> int | None:
    """Extract the required positive integer top-level ``id`` response field."""
    identifier = _safe_json_identifier(resp, "id")
    if (
        isinstance(identifier, int)
        and not isinstance(identifier, bool)
        and identifier > 0
    ):
        return identifier
    return None


def _uuid_response_id(resp: httpx.Response) -> str | None:
    """Extract and normalize the required UUID top-level ``uuid`` response field."""
    identifier = _safe_json_identifier(resp, "uuid")
    if not isinstance(identifier, str):
        return None
    try:
        normalized = str(UUID(identifier))
    except ValueError:
        return None
    return normalized if identifier.lower() == normalized else None


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


def _render_testcase(
    template: str,
    job_name: str,
    test_name: str,
    tier: str | None,
    subject_identifier: str,
) -> str:
    """Render the testcase name using the provided template."""
    return template.format(
        job_name=job_name,
        test_name=test_name,
        tier=tier or "",
        subject_identifier=subject_identifier,
    )


_EMPTY_SUBJECT_MSG = (
    "Greenwave subject template {template!r} rendered an empty or incomplete "
    "subject_identifier (unresolved placeholder or empty value); refusing to "
    "write a malformed gating subject"
)


def _render_subject(
    template: str,
    job_name: str,
    build_number: str,
    tier: str | None,
    product_version: str | None,
) -> str:
    """Render the subject identifier from *template* (fail-closed).

    Raises :exc:`ExporterPrerequisiteError` when the template literal itself
    contains control characters, when any placeholder referenced in the
    template resolves to an empty/None value, when the final sanitized value is
    empty after stripping, or when the result exceeds 500 characters.  This
    prevents malformed gating identifiers from reaching ResultsDB/WaiverDB on
    both auto-push and manual-push paths.
    """
    # Reject templates whose LITERAL text contains control characters.  Without
    # this guard a template like 'build-\x01-{build_number}' would silently
    # produce 'build--240' after the final sanitize_control_chars call below.
    if template != sanitize_control_chars(template):
        raise ExporterPrerequisiteError(
            "Greenwave subject template must not contain control characters"
        )
    available: dict[str, str | None] = {
        "job_name": job_name,
        "build_number": build_number,
        "tier": tier,
        "product_version": product_version,
    }
    # Sanitize each referenced placeholder value BEFORE the emptiness check so
    # control-char-only inputs (e.g. tier='\x00') are caught here rather than
    # surviving the guard and being stripped from the final string, which would
    # produce a malformed subject like 'build--240'.
    clean: dict[str, str] = {}
    for placeholder_name in referenced_placeholders(template):
        raw_value = available.get(placeholder_name)
        cleaned_value = sanitize_control_chars(raw_value or "").strip()
        if not cleaned_value:
            raise ExporterPrerequisiteError(
                _EMPTY_SUBJECT_MSG.format(template=template)
            )
        clean[placeholder_name] = cleaned_value

    rendered = template.format(**clean)
    sanitized = sanitize_control_chars(rendered).strip()
    if not sanitized:
        raise ExporterPrerequisiteError(_EMPTY_SUBJECT_MSG.format(template=template))
    if len(sanitized) > 500:
        raise ExporterPrerequisiteError(
            f"Rendered subject_identifier from GREENWAVE_SUBJECT_TEMPLATE "
            f"exceeds 500 characters ({len(sanitized)}); refusing "
            "to write a malformed gating identifier to ResultsDB/WaiverDB."
        )
    return sanitized


def _sanitize_error(exc: Exception) -> str:
    """Produce a safe, stable error string without token or URL info."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, _InvalidGroupResponseError):
        return "invalid group response"
    return type(exc).__name__


_NEGOTIATE_CHALLENGE_RE = re.compile(
    r"(?:^|,)\s*Negotiate(?:\s+([A-Za-z0-9+/=]+))?(?=\s*(?:,|$))",
    re.IGNORECASE,
)


def _negotiate_challenge(
    response: httpx.Response,
) -> tuple[bool, bytes | None]:
    """Return whether Negotiate was offered and its optional input token."""
    offered = False
    for header in response.headers.get_list("WWW-Authenticate"):
        for match in _NEGOTIATE_CHALLENGE_RE.finditer(header):
            offered = True
            if not match.group(1):
                continue
            try:
                return True, base64.b64decode(match.group(1), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise RuntimeError(
                    "Invalid SPNEGO challenge from Greenwave service"
                ) from exc
    return offered, None


def _negotiate_challenge_token(response: httpx.Response) -> bytes | None:
    """Extract a Negotiate token from one or more WWW-Authenticate headers."""
    return _negotiate_challenge(response)[1]


class _InvalidGroupResponseError(RuntimeError):
    """ResultsDB accepted group creation without returning a usable UUID."""


def _record_write_error(
    errors: list[str],
    exc: Exception,
    *,
    service: str,
    job_id: str,
    failure_index: int,
    result_context: str,
) -> None:
    """Log a stable failure index and append a sanitized item error to ``errors``.

    The rendered testcase can include the request-body subject identifier, so
    it is intentionally restricted to the API result and never passed to the
    logger.
    """
    safe_error = _sanitize_error(exc)
    logger.warning(
        "%s write failed for job_id=%r, failure_index=%d: %s",
        service,
        job_id,
        failure_index,
        safe_error,
    )
    errors.append(f"{result_context} ({service}): {safe_error}")


def _record_reconciliation_error(
    errors: list[str],
    *,
    service: str,
    job_id: str,
    failure_index: int,
    result_context: str,
) -> None:
    """Report an accepted write whose response cannot be reconciled by ID."""
    logger.warning(
        "%s accepted write for job_id=%r, failure_index=%d without a valid "
        "positive integer 'id'; reconciliation is incomplete",
        service,
        job_id,
        failure_index,
    )
    errors.append(
        f"{result_context} ({service}): accepted write returned no valid "
        "positive integer 'id'; external write may have succeeded but cannot be "
        "reconciled"
    )


def _record_corrupt_failure(
    errors: list[str],
    *,
    job_id: str,
    index: int,
    reason: str,
) -> None:
    """Log a stable entry index and account for a corrupt failure."""
    logger.warning(
        "Corrupt Greenwave failure entry for job_id=%r, index=%d: %s",
        job_id,
        index,
        reason,
    )
    errors.append(f"corrupt failure entry at index {index}")


def _require_auth_prerequisites(
    *,
    service: str,
    auth_method: str,
    token: str | None,
    token_setting: str,
    kerberos_keytab: str | None,
    ssl_cert: str | None,
    ssl_key: str | None,
) -> None:
    """Validate credentials required by one selected authentication method."""
    if auth_method in ("token", "oidc") and not token:
        raise ExporterPrerequisiteError(
            f"{token_setting} is required for {service} {auth_method.upper()} authentication"
        )
    if auth_method == "kerberos" and not kerberos_keytab:
        raise ExporterPrerequisiteError(
            f"GREENWAVE_KERBEROS_KEYTAB is required for {service} Kerberos authentication"
        )
    if auth_method == "ssl" and (not ssl_cert or not ssl_key):
        raise ExporterPrerequisiteError(
            f"GREENWAVE_SSL_CERT and GREENWAVE_SSL_KEY are required for {service} SSL authentication"
        )


def _require_write_transport(
    url: str,
    *,
    service: str,
    auth_method: str,
    verify: bool | str,
) -> str:
    """Return a validated base URL and warn for the HTTP escape hatch."""
    policy = evaluate_greenwave_transport(
        url,
        service=service,
        auth_method=auth_method,
        verify=verify,
    )
    if policy.error:
        raise ExporterPrerequisiteError(policy.error)
    if policy.insecure_http:
        logger.warning(
            "%s uses HTTP with effective TLS verification disabled; "
            "never use this outside isolated local development",
            service,
        )
    assert policy.base_url is not None
    return policy.base_url


class GreenwaveExporter(Exporter):
    """Client for pushing rootcoz classifications to Greenwave (ResultsDB / WaiverDB).

    Args:
        url: ResultsDB base URL.
        outcome_map: Map of classifications to outcomes.
        subject_type: Subject type for the result.
        testcase_template: Template string for the testcase name.
        subject_template: Optional template for constructing the subject
            identifier when none is provided at push time. Placeholders:
            {job_name}, {build_number}, {tier}, {product_version}. Required
            when Greenwave is used via AUTO_PUSH_EXPORTERS.
        tier: Optional tier string.
        resultsdb_auth_method: Auth method for ResultsDB ('none', 'token', 'kerberos', 'ssl').
        api_token: Optional token for ResultsDB.
        waiver_url: Optional WaiverDB base URL.
        waiver_auth_method: Auth method for WaiverDB ('oidc', 'kerberos', 'ssl').
        waiver_token: Optional token for WaiverDB.
        push_waivers: Whether to push waivers.
        waivable_classifications: Set of classifications that can be waived.
        allow_ai_waivers: Whether to allow waivers pushed by AI.
        product_version: Required product version if pushing waivers.
        kerberos_keytab: Kerberos keytab path.
        kerberos_principal: Kerberos principal.
        ssl_cert: Path to SSL client certificate.
        ssl_key: Path to SSL client key.
        verify: True, False, or path to a CA bundle. All writes require HTTPS
            by default. Isolated-development HTTP is accepted only for
            unauthenticated (``none``) auth when this effective value is exactly
            ``False``; all authenticated methods (``token``, ``oidc``,
            ``kerberos``, ``ssl``) always require HTTPS.

    Response contract:
        Successful ResultsDB ``/groups`` responses must contain a syntactically
        valid UUID in the top-level ``uuid`` field. Successful ResultsDB
        ``/results`` and WaiverDB ``/waivers/`` responses must contain a positive
        integer in the top-level ``id`` field. An accepted response without its
        required identifier is retained as a successful external write but
        reported as a partial-operation reconciliation error.

    Conditional prerequisites:
        ResultsDB token auth requires ``api_token``; Kerberos requires
        ``kerberos_keytab``; and SSL requires both ``ssl_cert`` and
        ``ssl_key``. When ``push_waivers`` is true, ``waiver_url`` and
        ``product_version`` are required together with the credential selected
        by ``waiver_auth_method``.

    Raises:
        ExporterPrerequisiteError: A selected auth method is missing required
            credentials, waiver submission is incomplete, a URL is invalid, or
            the transport violates the HTTPS policy.
    """

    NAME = "greenwave"
    DISPLAY_NAME = "Greenwave"
    needs_history_classifications = False
    needs_tracked_in_links = False

    def __init__(
        self,
        *,
        url: str,
        outcome_map: dict[str, str],
        subject_type: str,
        testcase_template: str,
        subject_template: str | None = None,
        tier: str | None = None,
        resultsdb_auth_method: str = "token",
        api_token: str | None = None,
        waiver_url: str | None = None,
        waiver_auth_method: str = "oidc",
        waiver_token: str | None = None,
        push_waivers: bool = False,
        waivable_classifications: frozenset[str] | None = None,
        allow_ai_waivers: bool = False,
        product_version: str | None = None,
        kerberos_keytab: str | None = None,
        kerberos_principal: str | None = None,
        ssl_cert: str | None = None,
        ssl_key: str | None = None,
        verify: bool | str = True,
    ) -> None:
        resultsdb_url = _require_write_transport(
            url,
            service="ResultsDB",
            auth_method=resultsdb_auth_method,
            verify=verify,
        )
        _require_auth_prerequisites(
            service="ResultsDB",
            auth_method=resultsdb_auth_method,
            token=api_token,
            token_setting="GREENWAVE_API_TOKEN",
            kerberos_keytab=kerberos_keytab,
            ssl_cert=ssl_cert,
            ssl_key=ssl_key,
        )
        normalized_waiver_url: str | None = None
        if push_waivers:
            if not waiver_url:
                raise ExporterPrerequisiteError(
                    "GREENWAVE_WAIVER_URL is required when GREENWAVE_PUSH_WAIVERS is enabled"
                )
            if not product_version:
                raise ExporterPrerequisiteError(
                    "GREENWAVE_PRODUCT_VERSION is required when GREENWAVE_PUSH_WAIVERS is enabled"
                )
            _require_auth_prerequisites(
                service="WaiverDB",
                auth_method=waiver_auth_method,
                token=waiver_token,
                token_setting="GREENWAVE_WAIVER_TOKEN",
                kerberos_keytab=kerberos_keytab,
                ssl_cert=ssl_cert,
                ssl_key=ssl_key,
            )
            normalized_waiver_url = _require_write_transport(
                waiver_url,
                service="WaiverDB",
                auth_method=waiver_auth_method,
                verify=verify,
            )

        self._url = resultsdb_url
        self._outcome_by_cf = normalize_greenwave_outcome_map(outcome_map.items())
        self._subject_type = subject_type
        self._testcase_template = testcase_template
        self._subject_template = subject_template
        self._tier = tier
        self._resultsdb_auth_method = resultsdb_auth_method
        self._api_token = api_token
        self._waiver_url = normalized_waiver_url
        self._waiver_auth_method = waiver_auth_method
        self._waiver_token = waiver_token
        self._push_waivers = push_waivers
        self._waivable_classifications = waivable_classifications or frozenset()
        self._allow_ai_waivers = allow_ai_waivers
        self._product_version = product_version
        self._kerberos_keytab = kerberos_keytab
        self._kerberos_principal = kerberos_principal
        self._ssl_cert = ssl_cert
        self._ssl_key = ssl_key
        self._verify = verify

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    @property
    def is_enabled(self) -> bool:
        return True

    def close(self) -> None:
        """No persistent resources to clean up."""

    def _auth_method_for(self, service: str) -> str:
        """Return the configured authentication method for a service."""
        return (
            self._resultsdb_auth_method
            if service == "resultsdb"
            else self._waiver_auth_method
        )

    def _headers_for(self, *, service: str) -> dict[str, str]:
        """Get the authentication headers for a specific service."""
        method = self._auth_method_for(service)
        if method in ("token", "oidc"):
            token = self._api_token if service == "resultsdb" else self._waiver_token
            return {"Authorization": f"Bearer {token}"} if token else {}
        if method == "kerberos":
            # Kerberos headers are supplied dynamically by _post_with_auth
            # during the SPNEGO challenge-response loop.
            return {}
        return {}

    def _create_spnego_context(self, url: str) -> Any:
        """Build and return a Kerberos SPNEGO client context for *url*."""
        # NOTE: `spnego` is imported lazily here (not at module top level, against the
        # usual repo import convention) ON PURPOSE: the Kerberos backend depends on the
        # optional `kerberos` extra (pyspnego[kerberos] -> gssapi/krb5), which is only
        # installed in the container, not in base/dev/CI environments. A top-level import
        # would make this module (and the whole exporter) fail to import wherever the
        # extra is absent. Importing inside the kerberos-only code path keeps token/oidc/
        # ssl auth and all tests working without the native krb5 build.
        try:
            import spnego

            host = urlparse(url).hostname or ""
            return spnego.client(
                spnego.KerberosKeytab(
                    keytab=self._kerberos_keytab or "",
                    principal=self._kerberos_principal,
                ),
                hostname=host,
                service="HTTP",
                protocol="kerberos",
            )
        except ImportError as exc:
            raise RuntimeError(
                "Kerberos auth requires the 'kerberos' extra "
                "(pip install 'rootcoz[kerberos]') and system krb5 libraries"
            ) from exc

    async def _post_negotiate(
        self, client: httpx.AsyncClient, url: str, *, json: Any, out_token: bytes
    ) -> httpx.Response:
        """POST with a Negotiate Authorization header built from *out_token*."""
        return await client.post(
            url,
            json=json,
            headers={
                "Authorization": "Negotiate "
                + base64.b64encode(out_token).decode("ascii")
            },
        )

    async def _run_spnego_exchange(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        json: Any,
        ctx: Any,
        in_token: bytes | None,
    ) -> httpx.Response:
        """Run the full SPNEGO challenge-response loop and validate mutual auth."""
        out_token = ctx.step(in_token)
        if not out_token:
            raise RuntimeError("SPNEGO client produced no authentication token")
        resp = await self._post_negotiate(client, url, json=json, out_token=out_token)
        # Continue 401 challenge-response rounds. A successful response may
        # contain the final mutual-auth token, which must also be consumed.
        rounds = 0
        while resp.status_code == 401:
            in_token = _negotiate_challenge_token(resp)
            if in_token is None:
                break
            out_token = ctx.step(in_token)
            if not out_token:
                break
            rounds += 1
            if rounds > 10:
                raise RuntimeError("SPNEGO authentication exceeded 10 rounds")
            resp = await self._post_negotiate(
                client, url, json=json, out_token=out_token
            )
        final_token = (
            _negotiate_challenge_token(resp) if resp.status_code != 401 else None
        )
        if final_token is not None:
            ctx.step(final_token)
        if resp.status_code < 400 and not ctx.complete:
            raise RuntimeError("SPNEGO mutual authentication did not complete")
        return resp

    async def _post_with_auth(
        self, client: httpx.AsyncClient, url: str, *, json: Any, service: str
    ) -> httpx.Response:
        """POST with configured auth, including a complete SPNEGO exchange."""
        method = self._auth_method_for(service)
        if method != "kerberos":
            return await client.post(
                url, json=json, headers=self._headers_for(service=service)
            )

        # First let an existing authenticated connection or session cookie
        # satisfy the request. Creating an optimistic context here would make a
        # cookie-authenticated 2xx look like failed mutual authentication because
        # the server has no reason to return a final Negotiate token.
        resp = await client.post(url, json=json)
        offered, in_token = _negotiate_challenge(resp)
        if resp.status_code != 401 or not offered:
            return resp

        # One security context belongs to one HTTP authentication exchange and
        # must not be reused after completion. Every challenge round below uses
        # this same context and AsyncClient; the client preserves connection and
        # cookie state across retries and subsequent writes.
        ctx = self._create_spnego_context(url)
        return await self._run_spnego_exchange(
            client, url, json=json, ctx=ctx, in_token=in_token
        )

    def _client_kwargs(self, method: str) -> dict[str, Any]:
        """Get the kwargs for httpx client creation."""
        kw: dict[str, Any] = {"verify": self._verify, "timeout": 30.0}
        if method == "ssl" and self._ssl_cert and self._ssl_key:
            kw["cert"] = (self._ssl_cert, self._ssl_key)
        return kw

    def _client_identity(self, *, service: str, url: str) -> tuple[Any, ...]:
        """Return concrete origin, transport, and credential identity for reuse."""
        method = self._auth_method_for(service)
        parsed = urlparse(url)
        origin = (parsed.scheme, parsed.hostname, parsed.port)
        credential: tuple[Any, ...]
        if method in ("token", "oidc"):
            token = self._api_token if service == "resultsdb" else self._waiver_token
            credential = (token,)
        elif method == "kerberos":
            credential = (self._kerberos_keytab, self._kerberos_principal)
        elif method == "ssl":
            credential = (self._ssl_cert, self._ssl_key)
        else:
            credential = ()
        return (origin, method, self._verify, credential)

    async def push(self, context: ExportContext) -> ExporterResult:
        """Push results to Greenwave ResultsDB and optionally WaiverDB.

        The push flow involves two stages:
        1. Create a ResultsDB group for the rootcoz analysis.
        2. Post failures to ResultsDB, and optionally to WaiverDB.
        """
        from rootcoz.models import FailureAnalysis

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

        # Resolve subject_identifier precedence:
        #   1. explicit subject_identifier (from request)
        #   2. valid rendered GREENWAVE_SUBJECT_TEMPLATE (raises on failure — no fallback)
        #   3. job_name fallback ONLY when no template is configured
        # A configured template that cannot render a valid subject raises
        # ExporterPrerequisiteError (HTTP 422); it never falls back to job_name.
        subject_identifier = context.subject_identifier
        if not subject_identifier and self._subject_template:
            subject_identifier = _render_subject(
                self._subject_template,
                context.job_name,
                context.build_number,
                self._tier,
                self._product_version,
            )
        if not subject_identifier and (
            self._push_waivers or context.pushed_by == AI_SYSTEM_USERNAME
        ):
            raise ExporterPrerequisiteError(
                "Greenwave requires an explicit subject_identifier (build NVR) for "
                "waiver submission / auto-push; refusing to fall back to job_name "
                "for gating writes."
            )
        subject_identifier = subject_identifier or context.job_name

        pushed = 0
        skipped = 0
        waived = 0
        errors: list[str] = []
        resultsdb_ids: list[int] = []
        waiver_ids: list[int] = []
        group_uuid: str | None = None
        results_url = self._url

        async with httpx.AsyncClient(
            **self._client_kwargs(self._resultsdb_auth_method)
        ) as client:
            w_client: httpx.AsyncClient | None = None
            if (
                self._push_waivers
                and self._waiver_url
                and self._client_identity(service="waiver", url=self._waiver_url)
                != self._client_identity(service="resultsdb", url=self._url)
            ):
                w_client = httpx.AsyncClient(
                    **self._client_kwargs(self._waiver_auth_method)
                )
            waiver_client = w_client or client

            try:
                # 1. Create ResultsDB Group
                group_payload = {
                    "description": f"rootcoz analysis {context.job_id}",
                    "ref_url": context.report_url or None,
                }
                try:
                    group_resp = await self._post_with_auth(
                        client,
                        f"{results_url}/groups",
                        json=group_payload,
                        service="resultsdb",
                    )
                    group_resp.raise_for_status()
                    group_uuid = _uuid_response_id(group_resp)
                    if group_uuid is None:
                        raise _InvalidGroupResponseError
                except Exception as exc:  # noqa: BLE001
                    safe_error = _sanitize_error(exc)
                    logger.warning(
                        "ResultsDB group creation failed for job_id=%r: %s",
                        context.job_id,
                        safe_error,
                    )
                    errors.append(f"group creation failed: {safe_error}")

                # 2. Process Failures
                for index, raw_failure in enumerate(context.failures):
                    if not isinstance(raw_failure, dict):
                        _record_corrupt_failure(
                            errors,
                            job_id=context.job_id,
                            index=index,
                            reason="entry is not an object",
                        )
                        continue

                    try:
                        failure = FailureAnalysis(**raw_failure)
                    except ValidationError:
                        _record_corrupt_failure(
                            errors,
                            job_id=context.job_id,
                            index=index,
                            reason="validation failed",
                        )
                        continue

                    classification = failure.analysis.classification.strip()
                    if not classification:
                        skipped += 1
                        continue

                    outcome = self._outcome_by_cf.get(classification.casefold())
                    if not outcome:
                        skipped += 1
                        continue

                    testcase_name = _render_testcase(
                        self._testcase_template,
                        context.job_name,
                        failure.test_name,
                        self._tier,
                        subject_identifier,
                    )
                    note = f"rootcoz classification: {classification}"
                    if failure.analysis.details:
                        note += f" — {failure.analysis.details}"

                    data = {
                        "rootcoz_job_id": [context.job_id],
                        "rootcoz_classification": [classification],
                        "rootcoz_report_url": (
                            [context.report_url] if context.report_url else []
                        ),
                        "item": [subject_identifier],
                        "type": [self._subject_type],
                    }

                    payload: dict[str, Any] = {
                        "testcase": {"name": testcase_name},
                        "outcome": outcome,
                        "note": note,
                        "data": data,
                    }
                    if group_uuid:
                        payload["groups"] = [group_uuid]

                    # POST to ResultsDB
                    try:
                        result_resp = await self._post_with_auth(
                            client,
                            f"{results_url}/results",
                            json=payload,
                            service="resultsdb",
                        )
                        result_resp.raise_for_status()
                        r_id = _positive_integer_response_id(result_resp)
                        pushed += 1
                        if r_id is None:
                            _record_reconciliation_error(
                                errors,
                                service="ResultsDB",
                                job_id=context.job_id,
                                failure_index=index,
                                result_context=testcase_name,
                            )
                        else:
                            resultsdb_ids.append(r_id)
                    except Exception as exc:  # noqa: BLE001
                        _record_write_error(
                            errors,
                            exc,
                            service="ResultsDB",
                            job_id=context.job_id,
                            failure_index=index,
                            result_context=testcase_name,
                        )
                        continue  # skip waiver if resultsdb fails

                    # POST to WaiverDB
                    if self._push_waivers:
                        if (
                            classification.casefold()
                            not in self._waivable_classifications
                        ):
                            continue

                        reviewer = context.reviewed_by.get(failure.test_name, "")
                        if not reviewer:
                            continue

                        if (
                            reviewer == AI_SYSTEM_USERNAME
                            and not self._allow_ai_waivers
                        ):
                            continue

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
                            waiver_resp = await self._post_with_auth(
                                waiver_client,
                                f"{self._waiver_url}/waivers/",
                                json=waiver_payload,
                                service="waiver",
                            )
                            waiver_resp.raise_for_status()
                            w_id = _positive_integer_response_id(waiver_resp)
                            waived += 1
                            if w_id is None:
                                _record_reconciliation_error(
                                    errors,
                                    service="WaiverDB",
                                    job_id=context.job_id,
                                    failure_index=index,
                                    result_context=testcase_name,
                                )
                            else:
                                waiver_ids.append(w_id)
                        except Exception as exc:  # noqa: BLE001
                            _record_write_error(
                                errors,
                                exc,
                                service="WaiverDB",
                                job_id=context.job_id,
                                failure_index=index,
                                result_context=testcase_name,
                            )
            finally:
                if w_client:
                    await w_client.aclose()

        # ResultsDB writes are the primary operation. Item-level or best-effort
        # group/WaiverDB errors produce partial success when any result landed.
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

        details = {
            "pushed": pushed,
            "skipped": skipped,
            "waived": waived,
            "errors": errors,
            "details": {
                "resultsdb_ids": resultsdb_ids,
                "waiver_ids": waiver_ids,
                "group_uuid": group_uuid,
            },
        }
        return ExporterResult(success=success, message=message, details=details)
