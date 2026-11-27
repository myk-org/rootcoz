"""Greenwave/ResultsDB authentication strategies.

One entry point, :func:`build_greenwave_auth`, selected by the explicit
``greenwave_auth_mode`` Server Setting (mirroring the Jira auth-mode
model). Each mode produces a :class:`GreenwaveAuth` exposing:

* ``auth`` — an ``httpx.Auth`` for the header/flow-based modes
  (bearer, oidc, basic, kerberos, sso); ``None`` for mtls/none.
* ``client_kwargs()`` — transport-level kwargs (``verify`` only: a ``bool`` for
  header/flow modes, or an ``ssl.SSLContext`` carrying both the trust store and
  the client certificate chain for ``mtls``; never ``cert=``).
* ``mode`` — the selected mode label, for reporting and auditing.

``sso`` is the delegated mode: it authenticates with the REQUESTING user's
browser Red Hat SSO session token (resolved by
:func:`rootcoz.auth.sso.tokens.resolve_redhat_sso_tokens`), never with a
deployment credential. This module only BUILDS strategies — it never reads the
session store or drives a provider.

Tokens, client secrets, and keytab paths are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import os
import ssl
import time
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from rootcoz.config import (
    TOKEN_REFRESH_MARGIN_SECONDS,
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
    """A runtime auth-strategy failure with a safe, loggable message.

    Raised while an auth strategy obtains or applies its credentials: a failed
    OIDC token request, or a kerberos/SPNEGO negotiation the server refused.
    Token delegation failures for ``sso`` mode are ``DelegatedTokenError``.
    """


class _DelegatedTokenLike(Protocol):
    """Structural view of :class:`rootcoz.auth.sso.tokens.DelegatedOIDCTokens`.

    Declared here instead of imported so this module never depends on the
    session/provider layer (see the module docstring).
    """

    @property
    def results_token(self) -> str: ...

    @property
    def waiver_token(self) -> str | None: ...

    @property
    def token_refreshed(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class UserSSOGreenwaveTokens:
    """One caller's Red Hat SSO tokens for a Greenwave push.

    ``results_token`` always exists (the refreshed session access token, or one
    exchanged for ``GREENWAVE_SSO_AUDIENCE``); ``waiver_token`` is set only when
    WaiverDB needs its own audience token. ``token_refreshed`` records that
    resolving this caller rotated the session bundle, for the audit trail only.
    """

    results_token: str = field(repr=False)
    waiver_token: str | None = field(default=None, repr=False)
    token_refreshed: bool = False

    @classmethod
    def from_delegated(cls, delegated: _DelegatedTokenLike) -> UserSSOGreenwaveTokens:
        """Convert resolved delegated tokens — the ONLY place the fields are copied.

        Typed structurally (not by importing ``rootcoz.auth``) so this module
        stays a pure strategy builder with no dependency on the session layer.
        """
        return cls(
            results_token=delegated.results_token,
            waiver_token=delegated.waiver_token,
            token_refreshed=delegated.token_refreshed,
        )


@dataclass(frozen=True, slots=True)
class GreenwaveAuth:
    """Auth strategy handle for one Greenwave auth mode.

    ``auth`` is attached per-request (``client.post(..., auth=...)``) so
    WaiverDB writes can keep their separate bearer token; transport
    kwargs (``verify`` — a bool, or an ssl.SSLContext carrying trust store +
    client cert chain for mtls) belong on the client.
    """

    mode: str
    _auth: httpx.Auth | None
    _client_kwargs: dict[str, Any] = field(default_factory=dict)
    # Built lazily so the build never touches disk: missing/bad files surface
    # inside the caller's existing OSError guard, where paths get scrubbed.
    _ssl_context_builder: Callable[[], ssl.SSLContext] | None = None

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
        """httpx client constructor kwargs — ``verify`` only.

        A ``bool`` for header/flow modes, or an ``ssl.SSLContext`` carrying
        both the trust store and the client cert chain for ``mtls``; never
        ``cert=``.
        """
        kwargs = dict(self._client_kwargs)
        if self._ssl_context_builder is not None:
            kwargs["verify"] = self._ssl_context_builder()
        return kwargs


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
        verify: bool,
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
        self._expires_at = time.monotonic() + max(
            lifetime - TOKEN_REFRESH_MARGIN_SECONDS, 0
        )
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
        sanitized = strip_control_chars(
            strip_url_userinfo_in_text(f"{type(exc).__name__}: {exc}")
        )
        return GreenwaveAuthError(
            "Greenwave kerberos authentication failed: "
            f"{scrub_greenwave_paths(sanitized, self._path_placeholders)}"
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


def scrub_greenwave_paths(text: str, source: Settings | Mapping[str, str]) -> str:
    """Replace every configured Greenwave file path in *text* with its placeholder.

    ONE scrubber for errors raised while opening a keytab, client cert/key, or
    CA bundle: those paths are credential locations and must never reach a log,
    a warning, or a connection-test message. Used by the kerberos auth build,
    the Greenwave exporter, and the admin connection test.

    *source* is either the settings or an already-computed path → placeholder
    mapping; paths are always applied longest-first here.
    """
    placeholders = (
        _greenwave_file_path_placeholders(source)
        if isinstance(source, Settings)
        else source
    )
    for path, placeholder in sorted(
        placeholders.items(), key=lambda item: len(item[0]), reverse=True
    ):
        text = text.replace(path, placeholder)
    return text


def _refuse_encrypted_key() -> bytes:
    """Fail immediately instead of letting OpenSSL prompt stdin for a passphrase."""
    raise OSError("Encrypted mTLS client keys are not supported")


def _build_mtls_ssl_context(
    *,
    client_cert: str,
    client_key: str,
    ca_bundle: str | None,
    verify_ssl: bool,
) -> ssl.SSLContext:
    """SSL context carrying BOTH the trust store and the client certificate.

    ``httpx.create_ssl_context(verify=<path>, cert=...)`` returns early on a
    string ``verify`` and silently drops the client certificate (httpx
    0.28), so the cert is loaded here on every path instead.

    ``verify_ssl`` applies only when ``ca_bundle`` is unset — a CA bundle
    always yields a verifying context (``CERT_REQUIRED``, hostname checked),
    matching the documented precedence of ``GREENWAVE_CA_BUNDLE`` over
    ``GREENWAVE_VERIFY_SSL``.

    Raises:
        OSError: A cert/key/CA file is missing, unreadable, or malformed.
    """
    if ca_bundle:
        context = (
            ssl.create_default_context(capath=ca_bundle)
            if os.path.isdir(ca_bundle)
            else ssl.create_default_context(cafile=ca_bundle)
        )
    else:
        # httpx's own default trust store (and SSL_CERT_FILE/SSL_CERT_DIR).
        context = httpx.create_ssl_context(verify=verify_ssl)
    context.load_cert_chain(
        certfile=client_cert,
        keyfile=client_key,
        password=_refuse_encrypted_key,
    )
    return context


def build_greenwave_auth(
    settings: Settings,
    *,
    url: str | None = None,
    env_name: str = "GREENWAVE_URL",
    user_sso_tokens: UserSSOGreenwaveTokens | None = None,
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
        ExporterPrerequisiteError: No mode is chosen, the selected
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
    verify: bool = settings.greenwave_verify_ssl
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
            _ssl_context_builder=lambda: _build_mtls_ssl_context(
                client_cert=client_cert,
                client_key=client_key,
                ca_bundle=settings.greenwave_ca_bundle,
                verify_ssl=settings.greenwave_verify_ssl,
            ),
        )
    if mode == "sso":
        # No deployment credential exists in this mode: the caller's own
        # browser-session token IS the credential (resolved by
        # rootcoz.auth.sso.tokens.resolve_redhat_sso_tokens at the push
        # boundary).
        if user_sso_tokens is None:
            raise ExporterPrerequisiteError(
                "Greenwave auth mode 'sso' requires a caller Red Hat SSO session token"
            )
        return GreenwaveAuth(
            mode,
            _HeaderAuth(
                "Authorization",
                "Bearer "
                + _require_header_token(
                    user_sso_tokens.results_token, "Red Hat SSO access token"
                ),
            ),
            {"verify": verify},
        )
    # mode == "none": no credentials, plain TLS settings.
    return GreenwaveAuth("none", None, {"verify": verify})


def build_greenwave_waiver_auth(
    settings: Settings, *, user_sso_tokens: UserSSOGreenwaveTokens | None = None
) -> GreenwaveAuth:
    """Auth strategy for WaiverDB waiver writes.

    The selected mode governs both systems: ``kerberos``/``mtls``/``oidc``/
    ``basic``/``none`` authenticate the waiver write exactly like the results
    write. Only ``bearer`` mode distinguishes them, because ResultsDB and
    WaiverDB are separate services with separate tokens
    (``GREENWAVE_API_TOKEN`` vs ``GREENWAVE_WAIVER_TOKEN``). ``sso`` also
    distinguishes them, since WaiverDB then needs its own audience-exchanged
    token — there is no fallback to the ResultsDB token.

    Raises:
        ExporterPrerequisiteError: bearer mode without a waiver token, sso
            mode without the caller's waiver-audience token, an http waiver
            endpoint (HTTPS-only in every mode — a waiver lifts a gate, so it
            is never downgraded to plain HTTP), or whatever
            :func:`build_greenwave_auth` raises for the mode.
    """
    try:
        # The scheme rule lives here and at the write path, never in
        # Settings construction (an env URL + DB-only mode must stay
        # mergeable — see validate_greenwave_url_structure).
        validate_greenwave_url(settings.greenwave_waiver_url, "GREENWAVE_WAIVER_URL")
    except ValueError as exc:
        raise ExporterPrerequisiteError(str(exc)) from None
    mode = normalize_greenwave_auth_mode(settings.greenwave_auth_mode)
    if mode == "sso":
        if user_sso_tokens is None:
            raise ExporterPrerequisiteError(
                "Greenwave auth mode 'sso' requires a caller Red Hat SSO session token"
            )
        if not user_sso_tokens.waiver_token:
            # No results-token fallback: a waiver is a gating write against a
            # DIFFERENT service, so a token minted for the results audience is
            # the wrong credential there — refused here rather than at WaiverDB.
            raise ExporterPrerequisiteError(
                "Greenwave auth mode 'sso' requires a WaiverDB audience token for "
                "waivers — set GREENWAVE_SSO_WAIVER_AUDIENCE (or "
                "GREENWAVE_SSO_AUDIENCE) and sign in via Red Hat SSO again"
            )
        return GreenwaveAuth(
            mode,
            _HeaderAuth(
                "Authorization",
                "Bearer "
                + _require_header_token(
                    user_sso_tokens.waiver_token, "Red Hat SSO waiver token"
                ),
            ),
            {"verify": settings.greenwave_verify_ssl},
        )
    if mode != "bearer":
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
