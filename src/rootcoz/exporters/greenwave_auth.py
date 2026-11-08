"""Greenwave/ResultsDB authentication strategies.

One entry point, :func:`build_greenwave_auth`, selected by the explicit
``greenwave_auth_mode`` Server Setting (mirroring the Jira auth-mode
model). Each mode produces a :class:`GreenwaveAuth` exposing:

* ``auth`` — an ``httpx.Auth`` for the header/flow-based modes
  (bearer, oidc, basic, kerberos); ``None`` for mtls/none.
* ``client_kwargs()`` — transport-level concerns (``verify``, ``cert``).
* ``mode`` — the selected mode label, for reporting and auditing.

Tokens, client secrets, and keytab paths are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from rootcoz.config import (
    Settings,
    greenwave_method_missing_fields,
    is_sendable_bearer_value,
    normalize_greenwave_auth_mode,
    validate_greenwave_url,
)
from rootcoz.exporters.base import ExporterPrerequisiteError
from rootcoz.url_utils import strip_url_userinfo_in_text
from rootcoz.utils import strip_control_chars

#: HTTP timeout for OIDC token requests.
_TOKEN_TIMEOUT = 30.0

#: Refresh an OIDC token this many seconds before its reported expiry.
_TOKEN_EXPIRY_MARGIN = 60.0

#: Maximum SPNEGO exchanges per request before the flow gives up with a
#: clear error — a server that keeps issuing Negotiate challenges must not
#: spin the client forever.
_MAX_NEGOTIATE_EXCHANGES = 5

#: Configured Greenwave file-path settings → stable placeholders used to
#: scrub credential locations out of error messages.
_FILE_PATH_SETTINGS: tuple[tuple[str, str], ...] = (
    ("greenwave_keytab_path", "<keytab>"),
    ("greenwave_client_cert", "<client-cert>"),
    ("greenwave_client_key", "<client-key>"),
    ("greenwave_ca_bundle", "<ca-bundle>"),
)


def _require_header_token(value: str, env_name: str) -> str:
    """Return *value* if it can be a bearer credential, else refuse it.

    A configured token goes straight into an ``Authorization`` header, so the
    same rule the OIDC fetch applies to a remote token applies to an
    admin-supplied one. Refused at BUILD time with a message naming the
    setting and quoting nothing — the value must never reach a log or a push
    error through a header-encoding exception.
    """
    if not is_sendable_bearer_value(value):
        raise ExporterPrerequisiteError(
            f"{env_name} is not a usable bearer token — it contains "
            "whitespace or a character a header cannot carry"
        )
    return value


class GreenwaveAuthError(Exception):
    """A runtime authentication failure with a safe, loggable message."""


@dataclass(frozen=True, slots=True)
class GreenwaveAuth:
    """Auth strategy handle for one Greenwave auth mode.

    ``auth`` is attached per-request (``client.post(..., auth=...)``) so
    WaiverDB writes can keep their separate bearer token; transport
    kwargs (``cert``/``verify``) belong on the client.
    """

    mode: str
    _auth: httpx.Auth | None
    _client_kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def auth(self) -> httpx.Auth | None:
        """``httpx.Auth`` for header/flow modes; ``None`` for mtls/none."""
        return self._auth

    @property
    def spnego_exchange_completed(self) -> bool:
        """Whether a Negotiate exchange completed (kerberos mode only).

        Lets the connection test say "authenticated via kerberos" only
        when the keytab was actually exercised — a 200 without a 401
        challenge proves reachability, not authentication.
        """
        return bool(getattr(self._auth, "negotiate_completed", False))

    def client_kwargs(self) -> dict[str, Any]:
        """httpx client constructor kwargs (``verify``, ``cert``)."""
        return dict(self._client_kwargs)


class _HeaderAuth(httpx.Auth):
    """Attach one static header (the bearer mode's Authorization)."""

    def __init__(self, name: str, value: str) -> None:
        self._name = name
        self._value = value

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        request.headers[self._name] = self._value
        yield request


class _OidcAuth(httpx.Auth):
    """OIDC client-credentials flow with an in-memory token cache.

    The token is fetched on demand, cached until ~60s before its reported
    expiry, refreshed after a 401 (one retry), and never logged.
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        verify: bool | str,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._verify = verify
        self._cached_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            # Cached token rejected — refresh once and retry once. The
            # rejected token is passed in so a concurrent refresh that
            # already replaced it is not repeated (see _token).
            request.headers["Authorization"] = (
                f"Bearer {await self._token(rejected=token)}"
            )
            yield request

    async def _token(self, *, rejected: str | None = None) -> str:
        """Return a usable token, fetching one only when genuinely needed.

        ``rejected`` is the token the caller just sent and had refused. A
        concurrent request may already have replaced it while this one
        waited, so the fetch is skipped when the cached token differs from
        the rejected one: N simultaneous 401s then cost ONE client-credentials
        grant instead of N, which matters on IdPs that invalidate the
        previous token per grant (the later fetches would supersede the
        tokens the earlier retries are still sending).
        """
        if self._needs_fetch(rejected):
            async with self._lock:
                # Double-checked: another coroutine may have refreshed while
                # this one waited on the lock.
                if self._needs_fetch(rejected):
                    self._cached_token = await self._fetch_token()
        assert self._cached_token is not None  # set by _fetch_token or it raised
        return self._cached_token

    def _needs_fetch(self, rejected: str | None) -> bool:
        if rejected is not None:
            # A refresh was already performed for this rejection if the
            # cached token is no longer the one that was refused.
            return self._cached_token is None or self._cached_token == rejected
        if self._cached_token is None:
            return True
        return time.monotonic() >= self._expires_at

    async def _fetch_token(self) -> str:
        try:
            async with httpx.AsyncClient(
                verify=self._verify, timeout=_TOKEN_TIMEOUT
            ) as client:
                response = await client.post(
                    self._token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise GreenwaveAuthError(
                f"Greenwave OIDC token request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise GreenwaveAuthError(
                f"Greenwave OIDC token request failed: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GreenwaveAuthError(
                "Greenwave OIDC token response was not JSON"
            ) from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GreenwaveAuthError(
                "Greenwave OIDC token response missing 'access_token'"
            )
        if not is_sendable_bearer_value(token):
            # Refused here so the error names what happened without echoing the
            # token — see the helper.
            raise GreenwaveAuthError(
                "Greenwave OIDC token response carried an unusable 'access_token'"
            )
        expires_in = payload.get("expires_in")
        lifetime = expires_in if isinstance(expires_in, (int, float)) else 0
        # Cache until ~60s before expiry; a missing/short expiry means the
        # token is never reused from cache.
        self._expires_at = time.monotonic() + max(lifetime - _TOKEN_EXPIRY_MARGIN, 0)
        return token


class _KerberosAuth(httpx.Auth):
    """HTTP Negotiate (SPNEGO) dance backed by a client keytab.

    Per request: send unauthenticated, and on a ``401`` challenge with
    ``WWW-Authenticate: Negotiate [token]`` run the exchange against a
    FRESH security context (never cached or reused across requests). A
    server may answer a client token with another continuation
    ``Negotiate <token>`` challenge — the SAME context is then stepped
    again, up to :data:`_MAX_NEGOTIATE_EXCHANGES` rounds, after which the
    flow fails with a clear error instead of spinning.

    The keytab reaches GSSAPI through ``spnego.KerberosKeytab`` (the
    path is passed to ``krb5_kt_resolve`` directly, never formatted into
    a message). The process environment is not touched: a global
    ``KRB5_CLIENT_KTNAME`` would outlive a mode switch and leak the path
    into unrelated code.
    """

    def __init__(
        self,
        *,
        spnego_module: Any,
        keytab: str,
        principal: str,
        service: str,
        hostname: str,
        path_placeholders: dict[str, str],
    ) -> None:
        self._spnego = spnego_module
        self._keytab = keytab
        self._principal = principal
        self._service = service
        self._hostname = hostname
        self._path_placeholders = path_placeholders
        # Set when a Negotiate exchange completed — read by the Greenwave
        # connection test to word its success message. Fresh per strategy
        # instance, and the test builds its own instance, so no
        # cross-request leakage.
        self.negotiate_completed = False
        # No process-global KRB5_CLIENT_KTNAME: the explicit
        # ``spnego.KerberosKeytab`` credential below supplies the keytab to
        # every context this code creates. Mutating os.environ here would leak
        # the path into the environment of unrelated code and stay set after
        # a switch away from kerberos mode.

    @staticmethod
    def _negotiate_token(header: str) -> str | None:
        """The Negotiate token from a WWW-Authenticate header, if offered.

        A server may offer Negotiate alongside a fallback scheme — the
        standards-compliant ``WWW-Authenticate: Negotiate, Basic realm="x"``
        (common with Apache/mod_auth_gssapi fallback configs). Splitting the
        header on whitespace alone reads the scheme as ``negotiate,`` and
        wrongly reports that Negotiate was not offered.

        Returns:
            The challenge token (empty string when Negotiate is offered
            without one), or ``None`` when no challenge offers Negotiate.
        """
        for challenge in header.split(","):
            parts = challenge.split(None, 1)
            if parts and parts[0].lower() == "negotiate":
                return parts[1].strip() if len(parts) > 1 else ""
        return None

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        response = yield request
        if response.status_code != 401:
            return
        token = self._negotiate_token(response.headers.get("WWW-Authenticate", ""))
        if token is None:
            raise GreenwaveAuthError(
                "Greenwave kerberos authentication failed: server did not "
                "offer Negotiate authentication"
            )
        # ONE context per request — the exchange is multi-step; FRESH on
        # the next request: a security context is single-use. The GSSAPI
        # work is blocking (it can reach the KDC for a TGT), so it runs
        # off the event loop: a push writes one results row per failure,
        # and stalling the loop per row would serialize the whole server
        # behind a KDC round trip.
        try:
            context = await asyncio.to_thread(self._new_context)
        except Exception as exc:
            raise self._gssapi_error(exc) from exc
        for _ in range(_MAX_NEGOTIATE_EXCHANGES):
            try:
                in_token = base64.b64decode(token) if token else None
            except ValueError as exc:
                # A malformed challenge is an authentication failure, not a
                # crash: this decode used to sit OUTSIDE the guarded block, so
                # the connection test reported an unexpected exception with no
                # auth-specific diagnostic instead of "authentication failed".
                raise GreenwaveAuthError(
                    "Greenwave kerberos authentication failed: server sent a "
                    "malformed Negotiate challenge"
                ) from exc
            try:
                out_token = await asyncio.to_thread(context.step, in_token)
            except Exception as exc:
                raise self._gssapi_error(exc) from exc
            if not out_token:
                raise GreenwaveAuthError(
                    "Greenwave kerberos authentication produced no token"
                )
            request.headers["Authorization"] = (
                f"Negotiate {base64.b64encode(out_token).decode('ascii')}"
            )
            response = yield request
            if response.status_code != 401:
                self.negotiate_completed = True
                return
            # A 401 answering our token with another Negotiate challenge
            # is a continuation exchange — feed the new token back into the
            # same context. A 401 that drops Negotiate is a rejection.
            token = self._negotiate_token(response.headers.get("WWW-Authenticate", ""))
            if token is None:
                raise GreenwaveAuthError(
                    "Greenwave kerberos authentication failed: server "
                    "rejected the Negotiate credentials"
                )
        raise GreenwaveAuthError(
            "Greenwave kerberos authentication failed: server kept "
            "requesting Negotiate authentication after "
            f"{_MAX_NEGOTIATE_EXCHANGES} exchanges"
        )

    def _new_context(self) -> Any:
        """Build a fresh client security context (blocking GSSAPI work)."""
        return self._spnego.client(
            username=self._spnego.KerberosKeytab(
                keytab=self._keytab, principal=self._principal
            ),
            protocol="negotiate",
            service=self._service,
            hostname=self._hostname,
        )

    def _gssapi_error(self, exc: Exception) -> GreenwaveAuthError:
        """A loggable error keeping the provider's text, scrubbed.

        The class name alone (e.g. "ValueError") leaves an operator with
        nothing to act on, so the message keeps the provider text — but a
        GSSAPI keytab-open error carries the FILENAME, and every
        configured Greenwave file path is a credential location that must
        not reach logs, warnings or the push response.
        """
        message = strip_control_chars(
            strip_url_userinfo_in_text(f"{type(exc).__name__}: {exc}")
        )
        for path, placeholder in self._path_placeholders.items():
            message = message.replace(path, placeholder)
        return GreenwaveAuthError(
            f"Greenwave kerberos authentication failed: {message}"
        )


def _load_spnego() -> Any:
    """Import pyspnego lazily; the dependency is optional at import time."""
    try:
        import spnego
    except ImportError as exc:  # pragma: no cover - exercised via fakes
        raise ExporterPrerequisiteError(
            "Greenwave kerberos auth mode requires the optional pyspnego "
            "dependency — install rootcoz[kerberos] (plus system krb5 "
            "libraries)"
        ) from exc
    return spnego


def _kerberos_hostname(settings: Settings, url: str) -> str:
    """Target host for the SPN of *url*, optionally realm-qualified.

    pyspnego composes the hostbased name as ``<service>@<hostname>`` and MIT
    splits at the FIRST ``@``, handing the remainder to
    ``krb5_sname_to_principal(host, service, KRB5_NT_SRV_HST)``. A hostname
    of ``host@REALM`` therefore yields the principal ``HTTP/host@REALM`` —
    that is the supported way to pin the SERVICE realm (the realm of the
    client principal is not necessarily the realm of the service).

    The realm is taken from ``GREENWAVE_KRB5_REALM`` when set (the explicit,
    documented knob) and otherwise from the keytab principal's realm. With no
    realm at all the bare host is used and the realm comes from
    ``krb5.conf`` (``[domain_realm]``) — which also avoids
    ``dns_canonicalize_hostname`` trying to resolve a realm-qualified name.
    """
    host = urlsplit(url).hostname or ""
    realm = (settings.greenwave_krb5_realm or "").strip()
    if not realm:
        principal = settings.greenwave_keytab_principal or ""
        realm = principal.rsplit("@", 1)[1] if "@" in principal else ""
    return f"{host}@{realm}" if realm else host


def _greenwave_file_path_placeholders(settings: Settings) -> dict[str, str]:
    """Configured Greenwave file paths → stable log placeholders."""
    return {
        path: placeholder
        for attr, placeholder in _FILE_PATH_SETTINGS
        if (path := getattr(settings, attr))
    }


def build_greenwave_auth(
    settings: Settings, *, url: str | None = None, env_name: str = "GREENWAVE_URL"
) -> GreenwaveAuth:
    """Build the auth strategy for the selected ``greenwave_auth_mode``.

    ``url`` is the target being authenticated (defaults to ``GREENWAVE_URL``).
    It matters for the modes whose credentials are bound to the target host:
    ``kerberos`` builds the SPN from it and ``mtls`` keeps the same client
    certificate. ResultsDB and WaiverDB are different hosts, so their
    kerberos strategies must be built separately.

    The credentials-over-plain-http scheme rule is enforced HERE (not in
    Settings construction, which must stay mergeable when the URL comes
    from the env and the mode from the DB): refusing at the build means a
    push fails closed before any credential is sent.

    Raises:
        ExporterPrerequisiteError: No mode is selected, the selected
            mode's required fields are missing, the target URL (or, for
            oidc, the token endpoint) would carry a credential over plain
            HTTP, or (kerberos) the optional pyspnego dependency is not
            installed.
    """
    mode = normalize_greenwave_auth_mode(settings.greenwave_auth_mode)
    if not mode:
        raise ExporterPrerequisiteError(
            "No Greenwave auth method selected — choose one in Server Settings"
        )
    missing = greenwave_method_missing_fields(settings)
    if missing:
        raise ExporterPrerequisiteError(
            f"Greenwave auth method '{mode}' is missing: {', '.join(missing)}"
        )
    try:
        # 'none' sends no credentials, so a public plain-HTTP ResultsDB is
        # legal for it — every credential-bearing mode must be HTTPS.
        validate_greenwave_url(
            url if url is not None else settings.greenwave_url,
            env_name,
            allow_http=mode == "none",
        )
        if mode == "oidc":
            # The client secret is POSTed to the token endpoint on every
            # push — HTTPS only, in every mode.
            validate_greenwave_url(
                settings.greenwave_oidc_token_url, "GREENWAVE_OIDC_TOKEN_URL"
            )
    except ValueError as exc:
        raise ExporterPrerequisiteError(str(exc)) from None
    verify: bool | str = settings.greenwave_verify_ssl
    if mode == "bearer":
        token = settings.greenwave_api_token
        assert token is not None  # guaranteed by the missing-fields check
        return GreenwaveAuth(
            mode,
            _HeaderAuth(
                "Authorization",
                "Bearer "
                + _require_header_token(
                    token.get_secret_value(), "GREENWAVE_API_TOKEN"
                ),
            ),
            {"verify": verify},
        )
    if mode == "basic":
        user = settings.greenwave_basic_user
        password = settings.greenwave_basic_password
        assert user is not None and password is not None
        return GreenwaveAuth(
            mode,
            httpx.BasicAuth(user, password.get_secret_value()),
            {"verify": verify},
        )
    if mode == "oidc":
        token_url = settings.greenwave_oidc_token_url
        client_id = settings.greenwave_oidc_client_id
        client_secret = settings.greenwave_oidc_client_secret
        assert token_url and client_id and client_secret is not None
        return GreenwaveAuth(
            mode,
            _OidcAuth(
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret.get_secret_value(),
                verify=verify,
            ),
            {"verify": verify},
        )
    if mode == "kerberos":
        keytab = settings.greenwave_keytab_path
        principal = settings.greenwave_keytab_principal
        assert keytab and principal
        return GreenwaveAuth(
            mode,
            _KerberosAuth(
                spnego_module=_load_spnego(),
                keytab=keytab,
                principal=principal,
                # An empty override would build "@host" — a malformed SPN.
                service=(settings.greenwave_krb5_service or "").strip() or "HTTP",
                hostname=_kerberos_hostname(
                    settings, url or settings.greenwave_url or ""
                ),
                path_placeholders=_greenwave_file_path_placeholders(settings),
            ),
            {"verify": verify},
        )
    if mode == "mtls":
        client_cert = settings.greenwave_client_cert
        client_key = settings.greenwave_client_key
        assert client_cert and client_key
        return GreenwaveAuth(
            mode,
            None,
            {
                "cert": (client_cert, client_key),
                "verify": settings.greenwave_ca_bundle or verify,
            },
        )
    # mode == "none": no credentials, plain TLS settings.
    return GreenwaveAuth("none", None, {"verify": verify})


def build_greenwave_waiver_auth(settings: Settings) -> GreenwaveAuth:
    """Auth strategy for WaiverDB waiver writes.

    The selected mode governs both systems: ``kerberos``/``mtls``/``oidc``/
    ``basic``/``none`` authenticate the waiver write exactly like the results
    write. Only ``bearer`` mode distinguishes them, because ResultsDB and
    WaiverDB are separate services with separate tokens
    (``GREENWAVE_API_TOKEN`` vs ``GREENWAVE_WAIVER_TOKEN``).

    Raises:
        ExporterPrerequisiteError: bearer mode without a waiver token, an
            http waiver endpoint (HTTPS-only in every mode — a waiver lifts
            a gate, so it is never downgraded to plain HTTP), or whatever
            :func:`build_greenwave_auth` raises for the mode.
    """
    try:
        # The scheme rule lives here and at the write path, never in
        # Settings construction (an env URL + DB-only mode must stay
        # mergeable — see validate_greenwave_url_structure).
        validate_greenwave_url(settings.greenwave_waiver_url, "GREENWAVE_WAIVER_URL")
    except ValueError as exc:
        raise ExporterPrerequisiteError(str(exc)) from None
    if normalize_greenwave_auth_mode(settings.greenwave_auth_mode) != "bearer":
        # Non-bearer modes authenticate both services with the same
        # mechanism, built for the WaiverDB host: a kerberos SPN is
        # per-service (HTTP/<host>), so reusing the ResultsDB strategy would
        # ask the KDC for the wrong service principal.
        return build_greenwave_auth(
            settings, url=settings.greenwave_waiver_url, env_name="GREENWAVE_WAIVER_URL"
        )
    token = settings.greenwave_waiver_token
    if token is None or not token.get_secret_value():
        raise ExporterPrerequisiteError(
            "Greenwave bearer mode requires GREENWAVE_WAIVER_TOKEN for WaiverDB "
            "waivers (ResultsDB and WaiverDB use separate tokens)"
        )
    return GreenwaveAuth(
        "bearer",
        _HeaderAuth(
            "Authorization",
            "Bearer "
            + _require_header_token(token.get_secret_value(), "GREENWAVE_WAIVER_TOKEN"),
        ),
        {"verify": settings.greenwave_verify_ssl},
    )
