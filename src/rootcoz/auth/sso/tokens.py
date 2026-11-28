"""Delegated Red Hat SSO tokens for a downstream service.

Resolves the tokens ONE caller's browser Red Hat SSO session can mint for
another API: refresh the session's OIDC bundle when it is about to expire, then
exchange it for the audiences that service needs (RFC 8693). A caller without a
browser Red Hat SSO session has no delegated token and is refused — there is
deliberately no fallback to a deployment credential.

This lives under ``rootcoz.auth.sso`` — not ``rootcoz.exporters`` — because it
reads the session store and drives the provider. Exporters stay pure strategy
builders (``rootcoz.exporters.greenwave_auth``).

Dependency direction: ``rootcoz.storage``, ``rootcoz.config``, this package.
Never ``rootcoz.main`` or ``rootcoz.exporters``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import weakref
from dataclasses import dataclass, field

from rootcoz import storage
from rootcoz.auth.sso.base import (
    AuthError,
    OAuthTokenBundle,
    decrypt_oauth_tokens,
    encrypt_oauth_tokens,
)
from rootcoz.auth.sso.oidc import OIDCProvider
from rootcoz.auth.sso.redhat import RedHatSSOProvider
from rootcoz.config import (
    TOKEN_REFRESH_MARGIN_SECONDS,
    Settings,
    is_sendable_bearer_value,
)

#: One lock per session token hash, so concurrent requests on the SAME browser
#: session cannot each spend the same refresh token. Weak values: an idle entry
#: goes away with its last waiter, so this never grows without bound and never
#: holds a session token (only its hash) past its use.
_SESSION_REFRESH_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


class DelegatedTokenError(Exception):
    """A delegated-token failure with a safe, loggable message.

    ``status_code`` is the HTTP status the caller's request answers with: 401
    for the caller's own unusable/expired session, 400 when the caller is not
    signed in through the required provider, 422 when the deployment did not
    configure the Red Hat SSO client, and 502 (the default) when the identity
    provider refused a refresh or exchange.
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class DelegatedOIDCTokens:
    """One caller's Red Hat SSO tokens for a downstream push.

    ``results_token`` always exists (the refreshed session access token, or one
    exchanged for the target audience); ``waiver_token`` is set only when the
    waiver write needs its own audience. ``token_refreshed`` says whether this
    resolution rotated the session's bundle, for the audit trail — never what
    the tokens are.
    """

    results_token: str = field(repr=False)
    waiver_token: str | None = field(default=None, repr=False)
    token_refreshed: bool = False


def _require_sendable(value: str, label: str) -> str:
    """Refuse a session/exchanged token an ``Authorization`` header cannot carry."""
    if not is_sendable_bearer_value(value):
        raise DelegatedTokenError(
            f"The {label} cannot be sent in an Authorization header — sign out "
            "and sign in via Red Hat SSO again to push",
            status_code=401,
        )
    return value


def _refresh_lock(session_token: str) -> asyncio.Lock:
    """The per-session refresh lock, created on first use.

    Keyed by the token's SHA-256: a module-level mapping must never hold a raw
    credential, and the hash is enough to tell two sessions apart.
    """
    key = hashlib.sha256(session_token.encode()).hexdigest()
    lock = _SESSION_REFRESH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_REFRESH_LOCKS[key] = lock
    return lock


def _needs_refresh(bundle: OAuthTokenBundle) -> bool:
    """Whether the bundle's access token is at (or past) its refresh margin."""
    return bundle.expires_at is not None and (
        time.time() >= bundle.expires_at - TOKEN_REFRESH_MARGIN_SECONDS
    )


async def _refreshed_bundle(
    provider: OIDCProvider, session_token: str
) -> tuple[OAuthTokenBundle, bool]:
    """Refresh the session's bundle once per session and persist the rotation.

    Read-check-refresh-persist runs under the session's lock and re-reads the
    stored bundle first: a request that waited while another refreshed it uses
    that result instead of spending the same refresh token again, which an IdP
    reads as token reuse and answers by revoking the whole grant.

    Returns:
        The usable bundle, and whether THIS call performed the refresh.

    Raises:
        DelegatedTokenError: The session ended mid-refresh (401), has no
            refresh token (401), or the IdP refused the refresh (401).
    """
    async with _refresh_lock(session_token):
        # The ciphertext we read is also the CAS expectation on write: a
        # logout/revocation during the refresh clears the blob, and the write
        # must then fail rather than resurrect a revoked token.
        current_enc = await storage.get_session_sso_tokens(session_token)
        current = decrypt_oauth_tokens(current_enc)
        if current is None or current.provider != "redhat":
            raise DelegatedTokenError(
                "Your Red Hat SSO session is no longer active. Sign in via Red "
                "Hat SSO again to push",
                status_code=401,
            )
        if not _needs_refresh(current):
            return current, False
        if not current.refresh_token:
            raise DelegatedTokenError(
                "Your Red Hat SSO session token has expired. Sign out and sign "
                "in via Red Hat SSO again to push",
                status_code=401,
            )
        try:
            refreshed = await provider.refresh_access_token(current.refresh_token)
        except AuthError as exc:
            raise DelegatedTokenError(
                "Your Red Hat SSO session token could not be refreshed. Sign "
                "out and sign in via Red Hat SSO again to push",
                status_code=401,
            ) from exc
        # Persist the rotated bundle so the browser session keeps working and
        # the next push does not refresh again.
        if not await storage.update_session_sso_tokens(
            session_token,
            encrypt_oauth_tokens(refreshed),
            expected_sso_tokens_enc=current_enc,
        ):
            # The session was logged out, expired, or revoked while we were
            # refreshing: fail closed rather than hand out a token for a session
            # the server no longer honours.
            raise DelegatedTokenError(
                "Your Red Hat SSO session is no longer active. Sign in via Red "
                "Hat SSO again",
                status_code=401,
            )
        return refreshed, True


async def _exchange_audience(
    provider: OIDCProvider,
    *,
    subject_token: str,
    audience: str,
    target: str,
) -> str:
    """RFC 8693 exchange of *subject_token* for *audience*.

    Raises:
        DelegatedTokenError: The IdP refused the exchange (502).
    """
    try:
        exchanged = await provider.exchange_token_for_audience(
            subject_token=subject_token, audience=audience
        )
    except AuthError as exc:
        raise DelegatedTokenError(
            f"Red Hat SSO token exchange for {target} failed — check the "
            "configured SSO audience and realm token-exchange permissions",
            status_code=502,
        ) from exc
    return exchanged.access_token


async def resolve_redhat_sso_tokens(
    settings: Settings,
    *,
    session_token: str,
    auth_strategy: str,
    auth_provider: str | None,
    need_waiver_token: bool = False,
) -> DelegatedOIDCTokens:
    """Delegated Red Hat SSO tokens for ONE caller's browser session.

    Args:
        settings: Effective settings (SSO client + optional audiences).
        session_token: The caller's browser session token.
        auth_strategy: How that session authenticated (``session`` only).
        auth_provider: The SSO provider that issued it (``redhat`` only).
        need_waiver_token: Also mint the waiver-write token.

    Returns:
        The tokens for the results write (always) and the waiver write (when
        asked), plus whether the session bundle was refreshed.

    Raises:
        DelegatedTokenError: The caller has no usable Red Hat SSO session token,
            the IdP refused a refresh/exchange, or the deployment has no Red Hat
            SSO client configured.
    """
    if auth_strategy != "session" or auth_provider != "redhat" or not session_token:
        raise DelegatedTokenError(
            "Greenwave auth mode 'sso' requires an active Red Hat SSO browser "
            "session. Sign in via Red Hat SSO in your browser to push.",
            status_code=400,
        )
    bundle = decrypt_oauth_tokens(await storage.get_session_sso_tokens(session_token))
    if bundle is None or bundle.provider != "redhat":
        raise DelegatedTokenError(
            "Your browser session has no Red Hat SSO OIDC token. Sign out and "
            "sign in via Red Hat SSO again to push to Greenwave.",
            status_code=401,
        )
    provider = RedHatSSOProvider.from_settings(settings)
    if provider is None:
        raise DelegatedTokenError(
            "Red Hat SSO is not configured — set SSO_REDHAT_ISSUER, "
            "SSO_REDHAT_CLIENT_ID, and SSO_REDHAT_CLIENT_SECRET",
            status_code=422,
        )
    token_refreshed = False
    if _needs_refresh(bundle):
        bundle, token_refreshed = await _refreshed_bundle(provider, session_token)
    results_token = bundle.access_token
    if settings.greenwave_sso_audience:
        results_token = await _exchange_audience(
            provider,
            subject_token=bundle.access_token,
            audience=settings.greenwave_sso_audience,
            target="ResultsDB",
        )
    _require_sendable(results_token, "Red Hat SSO access token")
    waiver_token: str | None = None
    if need_waiver_token:
        # An unset waiver audience reuses the results audience (and so its
        # already-minted token); a DIFFERENT one needs its own exchange.
        waiver_audience = (
            settings.greenwave_sso_waiver_audience or settings.greenwave_sso_audience
        )
        if waiver_audience and waiver_audience != settings.greenwave_sso_audience:
            waiver_token = await _exchange_audience(
                provider,
                subject_token=bundle.access_token,
                audience=waiver_audience,
                target="WaiverDB",
            )
        else:
            waiver_token = results_token
        _require_sendable(waiver_token, "Red Hat SSO waiver token")
    return DelegatedOIDCTokens(
        results_token=results_token,
        waiver_token=waiver_token,
        token_refreshed=token_refreshed,
    )
