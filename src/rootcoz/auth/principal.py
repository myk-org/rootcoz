"""Unified request principal, public-path contract, grant guards, and SSE revalidation.

This module is the single deep entry point for request identity in rootcoz:

* :class:`Principal` collapses the 9 untyped ``request.state.*`` auth attributes
  into one typed immutable object (with :func:`attach_principal` maintaining the
  backward-compatibility ``request.state.*`` mirrors in one place).
* :data:`PUBLIC_EXACT_PATHS` / :data:`PUBLIC_ROUTE_PATTERNS` / :func:`is_public_path`
  own the explicit anonymous-route contract (no positional path splitting).
* :func:`authenticate_request` runs the 4-path credential resolution chain
  (session cookie -> Bearer token -> trusted proxy header -> display-cookie
  fallback), enforces the disabled-account gate, and loads effective grants.
* :func:`require_authenticated`, :func:`require_admin`, :func:`require_reviewer`,
  :func:`require_operator`, :func:`require_can_view_reports`, and
  :func:`check_allow_list` evaluate grants against the typed :class:`Principal`.
* :func:`is_admin_role_current`, :func:`is_admin_sse_authorized`, and
  :func:`make_admin_sse_auth_check` perform live admin SSE re-authorization with
  revocation-epoch polling.
"""

from __future__ import annotations

import hmac
import re
import time as _time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response

from rootcoz import get_child_logger, storage
from rootcoz.auth import approval as auth_approval
from rootcoz.auth import registry as auth_registry
from rootcoz.auth.base import (
    RESERVED_ADMIN_USERNAME,
    SESSION_COOKIE,
    USERNAME_COOKIE,
    AuthSideEffects,
    effective_can_view_reports,
    read_cookie,
    set_session_cookie,
    set_username_cookie,
)
from rootcoz.config import Settings, get_settings

logger = get_child_logger(__name__)

#: Attribute name storing the typed :class:`Principal` on ``request.state``.
PRINCIPAL_STATE_ATTR = "principal"

#: Roles permitted to perform reviewer-level writes (comments, chat, SSO link/unlink).
REVIEWER_ROLES: frozenset[str] = frozenset({"reviewer", "operator", "admin"})

#: Roles permitted to perform operator-level writes (submit/analyze/re-analyze).
OPERATOR_ROLES: frozenset[str] = frozenset({"operator", "admin"})

#: Default periodic re-check interval for live admin SSE streams (seconds).
ADMIN_SSE_RECHECK_INTERVAL_SECONDS = 1.0

#: Exact paths that bypass authentication.
#: /api/releases/latest is intentionally public — it only proxies the latest
#: GitHub release metadata (version, changelog) with no sensitive data.
PUBLIC_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/login",
        "/health",
        "/api/health",
        "/api/auth/register",
        "/api/auth/login",
        # Logout acts only on the caller's own cookie, and it must still work
        # when that cookie's session has expired — otherwise a browser that sat
        # past its TTL could never end the provider's session either.
        "/api/auth/logout",
        # SSO discovery is public; /login and /callback match PUBLIC_ROUTE_PATTERNS.
        "/api/auth/sso/providers",
        # First-login choice: the ticket is the credential, so these are
        # reachable before a session exists.
        "/api/auth/sso/ticket",
        "/api/auth/sso/create",
        "/api/auth/sso/link",
        # CLI device flow: start and poll exist before any session — the device
        # code is the credential the CLI polls with. Approval is NOT public.
        "/api/auth/sso/device/start",
        "/api/auth/sso/device/poll",
        "/api/auth/needs-key",
        "/api/auth/pending-status",
        "/api/releases/latest",
        "/metrics",
        "/pending",
        # The CLI's approval screen. The page itself is anonymous so a signed-out
        # visitor keeps it as their destination; the approval APIs it calls are NOT.
        "/login/device",
        "/favicon.ico",
        "/favicon.svg",
        "/sw.js",
        # Intentional public API-discovery surfaces (issue #200 / AGENTS.md).
        "/openapi.json",
        "/docs",
        "/redoc",
    }
)

#: Compiled route-shape patterns for parameterised anonymous routes.
#: Exactly ``/api/auth/sso/{provider}/login`` and ``/api/auth/sso/{provider}/callback``:
#: matching the exact shape keeps any future authenticated route under
#: ``/api/auth/sso/`` private.
PUBLIC_ROUTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/auth/sso/[^/]+/(login|callback)$"),
)


def is_public_path(path: str) -> bool:
    """True when *path* (or its non-root trailing-slash form) is anonymous."""
    if path in PUBLIC_EXACT_PATHS:
        return True
    normalized = path if path == "/" else path.rstrip("/")
    if normalized in PUBLIC_EXACT_PATHS:
        return True
    return any(
        pattern.match(normalized) is not None for pattern in PUBLIC_ROUTE_PATTERNS
    )


@dataclass(frozen=True)
class Principal:
    """Typed identity and effective grants resolved for one HTTP request."""

    username: str = ""
    role: str = "reviewer"
    is_admin: bool = False
    is_ai_session: bool = False
    can_view_reports: bool = False
    can_use_server_providers: bool = False
    can_use_free_providers: bool = False
    authenticated: bool = False
    auth_session_token: str = ""
    auth_strategy: str = ""
    auth_provider: str | None = None


#: Default unauthenticated principal attached to public/preflight requests.
ANONYMOUS_PRINCIPAL = Principal()


def attach_principal(request: Request, principal: Principal) -> None:
    """Attach *principal* to ``request.state`` and sync legacy attribute mirrors.

    Keeping all compatibility mirrors in this single helper ensures untouched
    route handlers reading ``request.state.*`` see the exact typed principal
    values without scattering 9 untyped assignments across middleware.
    """
    state = request.state
    setattr(state, PRINCIPAL_STATE_ATTR, principal)
    state.username = principal.username
    state.role = principal.role
    state.is_admin = principal.is_admin
    state.is_ai_session = principal.is_ai_session
    state.can_view_reports = principal.can_view_reports
    state.can_use_server_providers = principal.can_use_server_providers
    state.can_use_free_providers = principal.can_use_free_providers
    state.auth_session_token = principal.auth_session_token
    state.auth_strategy = principal.auth_strategy
    state.auth_provider = principal.auth_provider


def get_principal(request: Request) -> Principal:
    """Return the typed :class:`Principal` attached to *request*.

    Falls back to reading legacy ``request.state.*`` attributes only when a
    unit test constructs a bare ``Request``/stub without running
    ``AuthMiddleware``.
    """
    state = getattr(request, "state", None)
    existing = getattr(state, PRINCIPAL_STATE_ATTR, None) if state is not None else None
    if isinstance(existing, Principal):
        return existing
    if state is None:
        return ANONYMOUS_PRINCIPAL
    username = getattr(state, "username", "")
    username_str = username if isinstance(username, str) else ""
    role = getattr(state, "role", "reviewer")
    role_str = role if isinstance(role, str) else "reviewer"
    raw_token = getattr(state, "auth_session_token", "")
    token_str = raw_token if isinstance(raw_token, str) else ""
    raw_strategy = getattr(state, "auth_strategy", "")
    strategy_str = raw_strategy if isinstance(raw_strategy, str) else ""
    raw_provider = getattr(state, "auth_provider", None)
    provider_str = raw_provider if isinstance(raw_provider, str) else None
    return Principal(
        username=username_str,
        role=role_str,
        is_admin=bool(getattr(state, "is_admin", False)),
        is_ai_session=bool(getattr(state, "is_ai_session", False)),
        can_view_reports=bool(getattr(state, "can_view_reports", False)),
        can_use_server_providers=bool(
            getattr(state, "can_use_server_providers", False)
        ),
        can_use_free_providers=bool(getattr(state, "can_use_free_providers", False)),
        authenticated=bool(username_str),
        auth_session_token=token_str,
        auth_strategy=strategy_str,
        auth_provider=provider_str,
    )


@dataclass(frozen=True)
class AuthOutcome:
    """Full result of :func:`authenticate_request` for ``AuthMiddleware``."""

    principal: Principal
    side_effects: AuthSideEffects
    disabled_block_reason: str | None = None
    fetched_user: dict[str, Any] | None = None


async def authenticate_request(request: Request, settings: Settings) -> AuthOutcome:
    """Resolve credentials, check disabled state, and compute effective grants.

    Implements the complete 4-path resolution:
    1. Session cookie (with sliding-window renewal side effect)
    2. Bearer token (bootstrap ``ADMIN_KEY``, user API key, or session token)
    3. Trusted reverse-proxy header (``X-Forwarded-User``, with auto-provisioning)
    4. Display-only ``rootcoz_username`` cookie fallback (``authenticated=False``)

    If the resolved account is ``disabled``, returns immediately with
    ``disabled_block_reason="disabled"`` so the middleware can audit and refuse.
    """
    resolution = await auth_registry.resolve_auth(request, settings)
    identity = resolution.identity
    side_effects = resolution.side_effects

    is_admin = identity.is_admin if identity else False
    is_ai_session = identity.is_ai_session if identity else False
    username = identity.username if identity else ""
    resolved_role = identity.role if identity else settings.default_user_role
    has_valid_session = identity is not None
    auth_session_token = identity.session_token if identity else ""
    auth_strategy = identity.strategy_name if identity else ""
    auth_provider = identity.auth_provider if identity else None
    fetched_user: dict[str, Any] | None = identity.user if identity else None

    if identity is None:
        cookie_username = read_cookie(request, USERNAME_COOKIE).strip().lower()
        if cookie_username == RESERVED_ADMIN_USERNAME:
            cookie_username = ""
        username = cookie_username

    # Refuse disabled accounts on every path (#379); locked accounts are refused
    # at login only. Bootstrap admin has no users row.
    if (
        has_valid_session
        and username
        and username != RESERVED_ADMIN_USERNAME
        and fetched_user is None
    ):
        fetched_user = await storage.get_user_by_username(username)

    block_reason = auth_approval.account_block_reason(fetched_user)
    if block_reason == "disabled":
        principal = Principal(
            username=username,
            role=resolved_role,
            is_admin=is_admin,
            is_ai_session=is_ai_session,
            authenticated=False,
            auth_session_token=auth_session_token,
            auth_strategy=auth_strategy,
            auth_provider=auth_provider,
        )
        return AuthOutcome(
            principal=principal,
            side_effects=side_effects,
            disabled_block_reason=block_reason,
            fetched_user=fetched_user,
        )

    if is_admin:
        can_view_reports = effective_can_view_reports(True)
    elif has_valid_session and username:
        can_view_reports = effective_can_view_reports(
            False, bool(fetched_user and fetched_user.get("can_view_reports"))
        )
    else:
        can_view_reports = False

    can_use_server_providers = False
    can_use_free_providers = False
    if has_valid_session and username:
        try:
            can_use_server_providers = await storage.can_user_use_server_providers(
                username
            )
            can_use_free_providers = await storage.can_user_use_free_providers(username)
        except Exception:
            logger.exception("Unable to load server provider grant", exc_info=False)

    principal = Principal(
        username=username,
        role=resolved_role,
        is_admin=is_admin,
        is_ai_session=is_ai_session,
        can_view_reports=can_view_reports,
        can_use_server_providers=can_use_server_providers,
        can_use_free_providers=can_use_free_providers,
        authenticated=has_valid_session,
        auth_session_token=auth_session_token,
        auth_strategy=auth_strategy,
        auth_provider=auth_provider,
    )
    return AuthOutcome(
        principal=principal,
        side_effects=side_effects,
        disabled_block_reason=None,
        fetched_user=fetched_user,
    )


def apply_auth_response_cookies(
    request: Request,
    response: Response,
    side_effects: AuthSideEffects,
    settings: Settings,
) -> None:
    """Apply typed cookie side effects (proxy display cookie, session renewal)."""
    if side_effects.set_proxy_cookie:
        proxy_cookie_value = side_effects.set_proxy_cookie
        if read_cookie(request, USERNAME_COOKIE) != proxy_cookie_value:
            set_username_cookie(
                response, proxy_cookie_value, secure=settings.secure_cookies
            )

    if side_effects.renew_session_token and request.url.path not in (
        "/api/auth/login",
        "/api/auth/logout",
    ):
        fresh_settings = get_settings()
        set_session_cookie(
            response,
            side_effects.renew_session_token,
            secure=fresh_settings.secure_cookies,
        )


# ---------------------------------------------------------------------------
# Route grant guards (consume the typed Principal)
# ---------------------------------------------------------------------------


def require_admin(request: Request) -> Principal:
    """Raise 403 if the request is not from an authenticated admin."""
    principal = get_principal(request)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return principal


def require_can_view_reports(request: Request) -> Principal:
    """Raise 403 unless the caller may access ``/api/reports/*``."""
    principal = get_principal(request)
    if principal.can_view_reports:
        return principal
    raise HTTPException(
        status_code=403,
        detail="Reports access required. Ask an administrator to grant can_view_reports.",
    )


def require_authenticated(request: Request) -> Principal:
    """Raise 401 if the request has no authenticated username."""
    principal = get_principal(request)
    if not principal.username:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def require_reviewer(request: Request) -> Principal:
    """Raise 403 if the request is not from at least a reviewer."""
    principal = get_principal(request)
    if not principal.username:
        raise HTTPException(status_code=403, detail="Authentication required")
    if principal.role not in REVIEWER_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Reviewer access required. Viewers cannot perform this action.",
        )
    return principal


def require_operator(request: Request) -> Principal:
    """Raise 403 if the request is not from an operator or admin."""
    principal = get_principal(request)
    if principal.role not in OPERATOR_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Operator access required. Reviewers cannot perform this action.",
        )
    return principal


def check_allow_list(request: Request, settings: Settings) -> Principal:
    """Raise 403 if a non-admin caller is outside ``ALLOWED_USERS``."""
    principal = get_principal(request)
    allowed = settings.allowed_users_set
    if not allowed or principal.is_admin:
        return principal
    username = principal.username.strip().lower()
    if not username or username not in allowed:
        raise HTTPException(
            status_code=403,
            detail="User not allowed. Contact an administrator to be added to the allow list.",
        )
    return principal


# ---------------------------------------------------------------------------
# Live admin SSE re-authorization & revocation-epoch polling
# ---------------------------------------------------------------------------


async def is_admin_role_current(username: str) -> bool:
    """Check the live role and enabled state in SQLite."""
    if username == RESERVED_ADMIN_USERNAME:  # Bootstrap admin has no users row.
        return True
    user = await storage.get_user_by_username(username)
    return bool(user and not user.get("disabled") and user["role"] == "admin")


async def is_admin_sse_authorized(
    request: Request,
    *,
    settings_getter: Callable[[], Settings] = get_settings,
    admin_role_check: Callable[[str], Awaitable[bool]] = is_admin_role_current,
    raise_on_error: bool = False,
) -> bool:
    """Re-verify that a long-lived admin SSE caller is still an active admin.

    Checks both the live account state (not deleted, not disabled, still admin)
    and, when the connection authenticated via a session token or DB API key,
    that the credential itself has not been revoked or rotated.
    """
    principal = get_principal(request)
    username = principal.username
    if not username:
        return False

    try:
        # Honor auth_session_token="" set by AuthMiddleware when a Bearer admin
        # overrides an unrelated or expired session cookie.
        state = getattr(request, "state", None)
        has_principal = isinstance(
            getattr(state, PRINCIPAL_STATE_ATTR, None), Principal
        )
        raw_state_token = (
            principal.auth_session_token
            if has_principal
            else getattr(state, "auth_session_token", None)
        )
        if isinstance(raw_state_token, str):
            session_token = raw_state_token
        elif isinstance(getattr(request, "cookies", None), Mapping):
            raw_cookie = read_cookie(request, SESSION_COOKIE)
            session_token = raw_cookie if isinstance(raw_cookie, str) else ""
        else:
            session_token = ""

        if session_token:
            session = await storage.get_session(session_token)
            if (
                not session
                or not session.get("is_admin")
                or str(session.get("username", "")) != username
            ):
                logger.info(
                    "Admin SSE closed for '%s': session revoked or no longer admin",
                    username,
                )
                return False
        else:
            headers = getattr(request, "headers", None)
            auth_header = (
                headers.get("authorization", "") if isinstance(headers, Mapping) else ""
            )
            if isinstance(auth_header, str) and auth_header.startswith("Bearer "):
                token = auth_header[7:]
                settings = settings_getter()
                if not (
                    username == RESERVED_ADMIN_USERNAME
                    and settings.admin_key
                    and hmac.compare_digest(token.encode(), settings.admin_key.encode())
                ):
                    user = await storage.get_user_by_key(token)
                    if user is not None:
                        if (
                            str(user.get("username", "")) != username
                            or user.get("disabled")
                            or user.get("role") != "admin"
                        ):
                            logger.info(
                                "Admin SSE closed for '%s': API key no longer active admin",
                                username,
                            )
                            return False
                        return True
                    session = await storage.get_session(token)
                    if (
                        not session
                        or not session.get("is_admin")
                        or str(session.get("username", "")) != username
                    ):
                        logger.info(
                            "Admin SSE closed for '%s': Bearer credential revoked",
                            username,
                        )
                        return False

        still_admin = await admin_role_check(username)
        if not still_admin:
            logger.info(
                "Admin SSE closed for '%s': account deleted, disabled, or demoted",
                username,
            )
        return still_admin
    except Exception:
        if raise_on_error:
            raise
        logger.warning(
            "Admin SSE authorization re-check failed for '%s'; closing stream",
            username,
            exc_info=True,
        )
        return False


def make_admin_sse_auth_check(
    request: Request,
    *,
    authorize_fn: Callable[[Request], Awaitable[bool]],
    recheck_interval: float = ADMIN_SSE_RECHECK_INTERVAL_SECONDS,
) -> Callable[[], Awaitable[bool]]:
    """Build a per-stream admin auth check that avoids per-item DB storms.

    Always verifies against SQLite on the first call, immediately whenever
    ``storage.get_auth_revocation_version()`` advances (any disable, demotion,
    deletion, logout, or key rotation), and periodically every
    ``recheck_interval`` seconds (catching natural session expiry).
    """
    last_checked: float | None = None
    last_version = -1

    async def _check() -> bool:
        nonlocal last_checked, last_version
        now = _time.monotonic()
        version = storage.get_auth_revocation_version()
        if (
            last_checked is not None
            and version == last_version
            and (now - last_checked) < recheck_interval
        ):
            return True
        authorized = await authorize_fn(request)
        if authorized:
            last_checked = now
            last_version = version
            return True
        last_checked = None
        return False

    return _check
