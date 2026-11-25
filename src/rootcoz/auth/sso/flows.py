"""Deep SSO flow orchestration: browser login/link, first-login tickets, identity management, CLI device flow, and IdP logout.

Extracted from :mod:`rootcoz.main` so all SSO state machines, security checks,
and audit decisions live in one deep module under ``rootcoz.auth.sso`` and can
be unit-tested directly without booting FastAPI routes. ``main.py`` retains
only thin HTTP route adapters.

Dependency direction: depends on ``rootcoz.storage``, ``rootcoz.config``,
``rootcoz.url_utils``, ``rootcoz.auth.*``, ``fastapi``, and package logging —
never on ``rootcoz.main``. Runtime hooks (``record_audit``,
``notify_pending_count_changed``, ``lock_after_failed_login``,
``read_json_object``) are injected via :class:`SSOFlowHooks`.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote as _urlquote

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from rootcoz import get_child_logger, storage
from rootcoz.auth import api_key as auth_api_key
from rootcoz.auth import approval as auth_approval
from rootcoz.auth import identity as auth_identity
from rootcoz.auth import password as auth_password
from rootcoz.auth import principal as auth_principal
from rootcoz.auth import sso as auth_sso
from rootcoz.auth.base import (
    RESERVED_ADMIN_USERNAME,
    SESSION_COOKIE,
    read_cookie,
    role_and_admin,
    set_session_cookie,
    set_username_cookie,
)
from rootcoz.auth.principal import REVIEWER_ROLES, Principal
from rootcoz.auth.sso import state as auth_sso_state
from rootcoz.config import Settings, get_settings
from rootcoz.url_utils import is_safe_navigation_url, is_same_origin

logger = get_child_logger(__name__)


@dataclass(frozen=True)
class SSOFlowHooks:
    """Injected runtime callbacks so ``auth.sso.flows`` never imports ``main``."""

    record_audit: Callable[..., Awaitable[Any]]
    notify_pending_count_changed: Callable[[], Any]
    lock_after_failed_login: Callable[
        [str, Settings, Request], Awaitable[dict[str, Any] | None]
    ]
    read_json_object: Callable[[Request], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Constants & public error mapping
# ---------------------------------------------------------------------------

#: Reason codes that describe the *step* that failed and are safe to name in a
#: URL. Account-state refusals collapse to ``denied``.
SSO_PUBLIC_REASONS = frozenset(
    {
        "unknown_provider",
        "login_start_failed",
        "state_invalid",
        "state_mismatch",
        "missing_code",
        "provider_error",
        "exchange_failed",
        "id_token_invalid",
        "no_id_token",
        "userinfo_failed",
        "no_subject",
        "callback_misconfigured",
        "domain_not_allowed",
        "email_not_verified",
        "unknown_mode",
        "link_requires_session",
        "link_forbidden_role",
        "link_session_changed",
        "identity_conflict",
        "link_requires_post",
        "link_cross_site",
    }
)

SSO_DENIED = "denied"
SSO_CONFLICT_DETAIL = "That sign-in is already connected to another account"
SSO_NAME_DETAIL = "That name cannot be used"
SSO_ACCOUNT_DETAIL = "Account cannot be used to sign in"
BOOTSTRAP_ADMIN_LINK_DETAIL = (
    "The bootstrap admin has no account to link; sign in with a"
    " registered admin account instead"
)
LAST_IDENTITY_DETAIL = (
    "This is the only way to sign in to this account. Connect another sign-in"
    " method first, generate an API key, or ask an administrator to disable"
    " the account if losing access is intended."
)

SSO_ERROR_STATUS: dict[str, int] = {
    "identity_conflict": 409,
    "account_missing": 404,
    "domain_not_allowed": 403,
    "email_not_verified": 403,
    "account_disabled": 403,
    "account_locked": 403,
    "account_pending": 403,
    "account_rejected": 403,
}

#: Human-readable provider labels, derived once from the provider registry.
SSO_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    p.name: p.display_name or p.name for p in auth_sso.PROVIDER_CLASSES
}

DEVICE_APPROVAL_PATH = "/login/device"
DEVICE_CLIENT_NAME_MAX = 200
MAX_LIVE_DEVICE_CODES = 500
MAX_LIVE_DEVICE_CODES_PER_CALLER = 50


def sso_error_status(reason: str) -> int:
    """HTTP status for a refusal reason (400 for rejected names)."""
    return SSO_ERROR_STATUS.get(reason, 400)


def sso_public_reason(reason: str) -> str:
    """Map an internal reason to the coarse code the browser may see."""
    return reason if reason in SSO_PUBLIC_REASONS else SSO_DENIED


def sso_error_redirect(reason: str) -> RedirectResponse:
    """Redirect the browser to ``/login`` with a coarse failure code."""
    return RedirectResponse(
        url=f"/login?sso_error={_urlquote(sso_public_reason(reason), safe='')}",
        status_code=303,
    )


def link_error_redirect(destination: str, reason: str) -> RedirectResponse:
    """Redirect a refused link attempt back to the signed-in destination."""
    separator = "&" if "?" in destination else "?"
    public = sso_public_reason(reason)
    return RedirectResponse(
        url=f"{destination}{separator}sso_error={_urlquote(public, safe='')}",
        status_code=303,
    )


def sso_redirect_uri(settings: Settings, provider: str) -> str:
    """Registered callback URL for *provider*, built from ``PUBLIC_BASE_URL`` only."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_BASE_URL is required for SSO login",
        )
    return f"{base}/api/auth/sso/{provider}/callback"


def request_is_same_origin(request: Request, settings: Settings | None = None) -> bool:
    """Whether a POST proves it came from this deployment's own origin."""
    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site:
        return fetch_site == "same-origin"
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    active_settings = settings if settings is not None else get_settings()
    return is_same_origin(source, (active_settings.public_base_url or "").strip())


# ---------------------------------------------------------------------------
# IdP logout & provider discovery
# ---------------------------------------------------------------------------


async def idp_logout_url(
    auth_provider: str | None, settings: Settings
) -> tuple[str | None, str]:
    """Where the browser must go to end the provider's session too.

    Returns:
        ``(url, reason)`` where ``reason`` is one of ``not_sso``,
        ``no_public_base_url``, ``provider_unavailable``, ``local_only``,
        or ``offered``.
    """
    if not auth_provider:
        return None, "not_sso"
    if not settings.public_base_url:
        return None, "no_public_base_url"
    provider = auth_sso.get_provider(auth_provider, settings)
    if provider is None:
        return None, "provider_unavailable"
    post_logout = f"{settings.public_base_url.rstrip('/')}/login"
    try:
        url = await provider.end_session_url(post_logout)
    except Exception:
        logger.warning(
            "Could not build an IdP logout URL for %s", auth_provider, exc_info=True
        )
        return None, "local_only"
    if not url or not is_safe_navigation_url(url, allow_loopback_http=True):
        return None, "local_only"
    return url, "offered"


def sso_providers_payload(settings: Settings) -> dict[str, Any]:
    """Build the ``GET /api/auth/sso/providers`` response body."""
    browser_login = bool((settings.public_base_url or "").strip())
    return {
        "providers": [
            {"name": provider.name, "display_name": provider.display_name}
            for provider in auth_sso.configured_providers(settings)
        ],
        "browser_login": browser_login,
    }


# ---------------------------------------------------------------------------
# Shared SSO refusal & allow-list helpers
# ---------------------------------------------------------------------------


async def _audit_sso_refusal(
    hooks: SSOFlowHooks,
    request: Request,
    provider: str | None,
    reason: str,
    *,
    subject: str = "",
) -> None:
    """Record one refused SSO attempt."""
    await hooks.record_audit(
        action="auth.sso_login_failed",
        actor=subject or "anonymous",
        message=f"SSO login refused for provider '{provider}' ({reason})",
        target_type="provider",
        target_id=provider,
        outcome="denied",
        details={"provider": provider, "reason": reason},
        request=request,
    )


async def _sso_link_allow_list_refused(
    hooks: SSOFlowHooks,
    request: Request,
    *,
    provider: str,
    username: str,
    settings: Settings,
    is_admin: bool = False,
) -> bool:
    """Whether ``ALLOWED_USERS`` forbids attaching a provider to this account."""
    allowed = settings.allowed_users_set
    if is_admin or not allowed or username in allowed:
        return False
    await _audit_sso_refusal(hooks, request, provider, "not_allowed", subject=username)
    return True


async def _audit_device_approve_refusal(
    hooks: SSOFlowHooks,
    request: Request,
    principal: Principal,
    reason: str,
    *,
    provider: str | None,
) -> None:
    """Record one refused device approval (never logs the user/device code)."""
    await hooks.record_audit(
        action="auth.sso_device_approve_refused",
        actor=principal.username,
        message=(
            f"SSO device approval refused for provider '{provider or 'any'}' ({reason})"
        ),
        target_type="provider",
        target_id=provider or "",
        outcome="denied",
        details={"provider": provider, "reason": reason},
        request=request,
    )


# ---------------------------------------------------------------------------
# Browser login & link orchestration
# ---------------------------------------------------------------------------


async def sso_start_flow(
    request: Request, provider: str, mode: str, *, hooks: SSOFlowHooks
) -> RedirectResponse:
    """Start one browser SSO login or link attempt for *provider*."""
    settings = get_settings()
    selected = auth_sso.get_provider(provider, settings)
    if selected is None:
        await _audit_sso_refusal(hooks, request, provider, "unknown_provider")
        return sso_error_redirect("unknown_provider")
    if mode not in auth_sso_state.MODES:
        await _audit_sso_refusal(hooks, request, provider, "unknown_mode")
        return sso_error_redirect("unknown_mode")

    session_token = read_cookie(request, SESSION_COOKIE)
    session = await storage.get_session(session_token) if session_token else None
    caller = session["username"] if session else ""

    if mode == auth_sso_state.MODE_LINK:
        destination = auth_sso_state.safe_next(request.query_params.get("next", ""))
        if request.method != "POST":
            await _audit_sso_refusal(
                hooks, request, provider, "link_requires_post", subject=caller
            )
            return sso_error_redirect("link_requires_post")
        if not request_is_same_origin(request, settings):
            await _audit_sso_refusal(
                hooks, request, provider, "link_cross_site", subject=caller
            )
            return link_error_redirect(destination, "link_cross_site")
    if mode == auth_sso_state.MODE_LINK and not caller:
        await _audit_sso_refusal(hooks, request, provider, "link_requires_session")
        return sso_error_redirect("link_requires_session")
    if (
        mode == auth_sso_state.MODE_LINK
        and caller.strip().lower() == RESERVED_ADMIN_USERNAME
    ):
        await _audit_sso_refusal(
            hooks, request, provider, "bootstrap_admin_cannot_link", subject=caller
        )
        raise HTTPException(status_code=400, detail=BOOTSTRAP_ADMIN_LINK_DETAIL)
    if (
        mode == auth_sso_state.MODE_LINK
        and session is not None
        and session["role"] not in REVIEWER_ROLES
    ):
        await _audit_sso_refusal(
            hooks, request, provider, "link_forbidden_role", subject=caller
        )
        return sso_error_redirect("link_forbidden_role")

    try:
        redirect_uri = sso_redirect_uri(settings, provider)
        attempt, sealed, challenge = auth_sso_state.new_attempt(
            provider=provider,
            next_path=request.query_params.get("next", ""),
            ttl_seconds=settings.sso_state_ttl_seconds,
            mode=mode,
            link_to=caller,
        )
        url = await selected.authorization_url(
            redirect_uri=redirect_uri,
            state=attempt.state,
            code_challenge=challenge,
            nonce=attempt.nonce,
        )
    except (HTTPException, auth_sso.AuthError) as exc:
        reason = getattr(exc, "reason", "login_start_failed")
        logger.debug("SSO login start failed for %s: %s", provider, exc)
        await _audit_sso_refusal(hooks, request, provider, reason)
        return sso_error_redirect(reason)

    response = RedirectResponse(url=url, status_code=303)
    response.set_cookie(
        auth_sso_state.STATE_COOKIE,
        sealed,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=settings.sso_state_ttl_seconds,
    )
    await hooks.record_audit(
        action="auth.sso_login_start",
        actor=caller or "anonymous",
        actor_role=(session["role"] if session else None),
        is_admin=bool(session["is_admin"]) if session else False,
        message=(
            f"SSO {mode} started for provider '{provider}'"
            + (f" by '{caller}'" if caller else "")
        ),
        target_type="provider",
        target_id=provider,
        details={"provider": provider, "mode": mode},
        request=request,
    )
    return response


async def sso_finish_link_flow(
    request: Request,
    attempt: auth_sso_state.LoginAttempt,
    *,
    provider: str,
    outcome: auth_identity.IdentityResolution | auth_identity.LoginChoice,
    hooks: SSOFlowHooks,
) -> RedirectResponse:
    """Attach a just-authenticated provider identity to the signed-in account."""
    session_token = read_cookie(request, SESSION_COOKIE)
    session = await storage.get_session(session_token) if session_token else None
    username = session["username"] if session else ""
    destination = auth_sso_state.safe_next(attempt.next_path)

    if not username:
        await _audit_sso_refusal(hooks, request, provider, "link_requires_session")
        return sso_error_redirect("link_requires_session")
    if session is None or session["role"] not in REVIEWER_ROLES:
        await _audit_sso_refusal(
            hooks, request, provider, "link_forbidden_role", subject=username
        )
        return link_error_redirect(destination, "link_forbidden_role")
    if attempt.link_to and username != attempt.link_to:
        await _audit_sso_refusal(
            hooks, request, provider, "link_session_changed", subject=username
        )
        return link_error_redirect(destination, "link_session_changed")
    if username.strip().lower() == RESERVED_ADMIN_USERNAME:
        await _audit_sso_refusal(
            hooks, request, provider, "bootstrap_admin_cannot_link", subject=username
        )
        return link_error_redirect(destination, "bootstrap_admin_cannot_link")

    user = await storage.get_user_by_username(username)
    status = await storage.get_user_status(username) or ""
    if auth_approval.is_approval_blocked(status):
        refusal = f"account_{status}"
    elif auth_approval.account_block_reason(user) == "disabled":
        refusal = "account_disabled"
    else:
        refusal = ""
    if refusal:
        await _audit_sso_refusal(hooks, request, provider, refusal, subject=username)
        return link_error_redirect(destination, refusal)

    if not isinstance(outcome, auth_identity.LoginChoice):
        if outcome.username == username:
            response = RedirectResponse(
                url=f"{destination}{'&' if '?' in destination else '?'}"
                f"sso_linked={_urlquote(provider, safe='')}",
                status_code=303,
            )
            response.delete_cookie(auth_sso_state.STATE_COOKIE, path="/")
            return response
        await _audit_sso_refusal(
            hooks, request, provider, "identity_conflict", subject=username
        )
        return link_error_redirect(destination, "identity_conflict")

    _, is_admin = role_and_admin(user)
    if await _sso_link_allow_list_refused(
        hooks,
        request,
        provider=provider,
        username=username,
        settings=get_settings(),
        is_admin=is_admin,
    ):
        return link_error_redirect(destination, "not_allowed")

    try:
        await auth_identity.link_existing(
            username=username,
            provider=provider,
            subject=outcome.subject,
            email=outcome.email,
        )
    except auth_sso.AuthError as exc:
        await _audit_sso_refusal(hooks, request, provider, exc.reason, subject=username)
        return link_error_redirect(destination, exc.reason)

    await hooks.record_audit(
        action="auth.identity_linked",
        actor=username,
        actor_role=(session["role"] if session else None),
        is_admin=bool(session["is_admin"]) if session else False,
        message=f"SSO identity linked: user='{username}' provider='{provider}'",
        target_type="user",
        target_id=username,
        details={"provider": provider},
        request=request,
    )
    response = RedirectResponse(
        url=f"{destination}{'&' if '?' in destination else '?'}"
        f"sso_linked={_urlquote(provider, safe='')}",
        status_code=303,
    )
    response.delete_cookie(auth_sso_state.STATE_COOKIE, path="/")
    return response


async def sso_issue_session_flow(
    request: Request,
    resolution: auth_identity.IdentityResolution,
    *,
    provider: str,
    next_path: str,
    hooks: SSOFlowHooks,
    audit_action: str | None = None,
    as_json: bool = False,
) -> Response:
    """Create the session an SSO login ends with and report where to go."""
    settings = get_settings()
    user = await storage.get_user_by_username(resolution.username)
    if user is None:
        await _audit_sso_refusal(
            hooks, request, provider, "account_missing", subject=resolution.username
        )
        if as_json:
            raise HTTPException(status_code=401, detail="That account no longer exists")
        return RedirectResponse(url="/login?sso_error=denied", status_code=303)

    role, is_admin = role_and_admin(user, settings.default_user_role)
    session_token = await storage.create_session(
        resolution.username,
        is_admin=is_admin,
        role=role,
        auth_provider=provider,
    )
    blocked = auth_approval.approval_gate_applies(
        is_admin=is_admin, settings=settings
    ) and auth_approval.is_approval_blocked(resolution.status)
    destination = "/pending" if blocked else next_path
    if blocked and resolution.created:
        hooks.notify_pending_count_changed()

    response: Response
    if as_json:
        content: dict[str, Any] = {
            "username": resolution.username,
            "status": resolution.status,
        }
        if resolution.api_key:
            content["api_key"] = resolution.api_key
        response = JSONResponse(
            content={
                **content,
                "next": destination,
            }
        )
        if resolution.api_key:
            response.headers["Cache-Control"] = "no-store"
    else:
        response = RedirectResponse(url=destination, status_code=303)

    set_session_cookie(response, session_token, secure=settings.secure_cookies)
    set_username_cookie(response, resolution.username, secure=settings.secure_cookies)
    action = (
        audit_action
        if audit_action is not None
        else ("auth.sso_login_blocked" if blocked else "auth.sso_login")
    )
    await hooks.record_audit(
        action=action,
        actor=resolution.username,
        message=(
            f"SSO login: user='{resolution.username}' provider='{provider}'"
            f" created={resolution.created} status={resolution.status}"
        ),
        actor_role=role,
        is_admin=is_admin,
        target_type="user",
        target_id=resolution.username,
        outcome="denied" if blocked else "success",
        details={
            "provider": provider,
            "created": resolution.created,
            "status": resolution.status,
        },
        request=request,
    )
    return response


async def sso_callback_flow(
    provider: str, request: Request, *, hooks: SSOFlowHooks
) -> Response:
    """Finish a browser SSO login or link callback."""
    settings = get_settings()
    selected = auth_sso.get_provider(provider, settings)
    if selected is None:
        await _audit_sso_refusal(hooks, request, provider, "unknown_provider")
        return sso_error_redirect("unknown_provider")

    state_cookie = read_cookie(request, auth_sso_state.STATE_COOKIE)
    error = request.query_params.get("error", "")
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    attempt: auth_sso_state.LoginAttempt | None = None

    async def refuse(reason: str, username: str = "") -> RedirectResponse:
        logger.debug("SSO callback refused for %s: %s", provider, reason)
        await _audit_sso_refusal(hooks, request, provider, reason, subject=username)
        if attempt is not None and attempt.mode == auth_sso_state.MODE_LINK:
            token = read_cookie(request, SESSION_COOKIE)
            if token and await storage.get_session(token) is not None:
                return link_error_redirect(
                    auth_sso_state.safe_next(attempt.next_path), reason
                )
        return sso_error_redirect(reason)

    if not state:
        return await refuse("provider_error" if error else "missing_code")
    try:
        opened = auth_sso_state.open_attempt(state_cookie, provider=provider)
    except auth_sso_state.StateError as exc:
        logger.debug("SSO state rejected: %s", exc)
        return await refuse("state_invalid")
    if not hmac.compare_digest(opened.state.encode(), state.encode()):
        return await refuse("state_mismatch")
    attempt = opened

    if error:
        return await refuse("provider_error")
    if not code:
        return await refuse("missing_code")

    try:
        claims = await selected.exchange(
            code=code,
            code_verifier=attempt.code_verifier,
            redirect_uri=sso_redirect_uri(settings, provider),
            nonce=attempt.nonce,
        )
        outcome = await auth_identity.resolve(provider, claims, settings)
    except auth_sso.AuthError as exc:
        return await refuse(exc.reason, exc.subject or "")
    except HTTPException:
        return await refuse("callback_misconfigured")

    if attempt.mode == auth_sso_state.MODE_LINK:
        return await sso_finish_link_flow(
            request, attempt, provider=provider, outcome=outcome, hooks=hooks
        )

    if isinstance(outcome, auth_identity.LoginChoice):
        binding = auth_sso_state.new_ticket_binding()
        ticket = await storage.create_sso_ticket(
            provider=outcome.provider,
            subject=outcome.subject,
            email=outcome.email,
            suggested_username=outcome.suggested_username,
            binding=binding,
            next_path=auth_sso_state.safe_next(attempt.next_path),
        )
        await hooks.record_audit(
            action="auth.sso_choice_required",
            actor="anonymous",
            message=(
                f"SSO identity needs a choice for provider '{provider}'"
                f" (suggested name: {outcome.suggested_username or 'none'})"
            ),
            target_type="provider",
            target_id=provider,
            details={
                "provider": provider,
                "suggested_username": outcome.suggested_username,
            },
            request=request,
        )
        response = RedirectResponse(
            url=f"/login#sso_ticket={_urlquote(ticket, safe='')}", status_code=303
        )
        response.set_cookie(
            auth_sso_state.TICKET_COOKIE,
            binding,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            max_age=settings.sso_state_ttl_seconds,
        )
        response.delete_cookie(auth_sso_state.STATE_COOKIE, path="/")
        return response

    response = await sso_issue_session_flow(
        request,
        outcome,
        provider=provider,
        next_path=attempt.next_path,
        hooks=hooks,
    )
    response.delete_cookie(auth_sso_state.STATE_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# First-login ticket lifecycle
# ---------------------------------------------------------------------------


async def _require_ticket_binding(request: Request, ticket: str) -> None:
    """Refuse a ticket presented without the browser binding cookie it was minted with."""
    stored = await storage.sso_ticket_binding_hash(ticket)
    presented = read_cookie(request, auth_sso_state.TICKET_COOKIE)
    if (
        not stored
        or not presented
        or not hmac.compare_digest(
            auth_sso_state.ticket_binding_hash(presented), stored
        )
    ):
        raise HTTPException(status_code=404, detail="This sign-in link has expired")


async def _ticket_or_404(ticket: str) -> dict[str, Any]:
    """Read a live first-login ticket, or raise 404."""
    record = await storage.peek_sso_ticket(ticket)
    if record is None:
        raise HTTPException(status_code=404, detail="This sign-in link has expired")
    return record


async def _require_configured_provider(
    hooks: SSOFlowHooks, request: Request, record: dict[str, Any], settings: Settings
) -> None:
    """Refuse a ticket whose provider this deployment no longer offers."""
    if auth_sso.get_provider(record["provider"], settings) is not None:
        return
    await _audit_sso_refusal(hooks, request, record["provider"], "unknown_provider")
    raise HTTPException(status_code=404, detail="This sign-in link has expired")


def email_domain(email: str | None) -> str:
    """Extract only the domain portion of a provider-reported address."""
    if not email or "@" not in email:
        return ""
    return email.partition("@")[2]


async def sso_inspect_ticket_flow(
    request: Request, *, hooks: SSOFlowHooks
) -> JSONResponse:
    """Describe the identity a first-login ticket stands for (and optional username probe)."""
    body = await hooks.read_json_object(request)
    ticket = str(body.get("ticket", ""))
    await _require_ticket_binding(request, ticket)
    record = await _ticket_or_404(ticket)

    content: dict[str, Any] = {
        "provider": record["provider"],
        "suggested_username": record["suggested_username"],
        "email_domain": email_domain(record["email"]),
        "expires_at": record["expires_at"],
    }
    raw_username = body.get("username")
    if isinstance(raw_username, str) and raw_username.strip():
        await _require_configured_provider(hooks, request, record, get_settings())
        existing = await storage.get_user_by_username(raw_username.strip().lower())
        content["username_exists"] = existing is not None
    return JSONResponse(content=content)


async def sso_create_account_flow(request: Request, *, hooks: SSOFlowHooks) -> Response:
    """Finish a first login by creating an account with the chosen name."""
    body = await hooks.read_json_object(request)
    ticket = str(body.get("ticket", ""))
    username = str(body.get("username", ""))
    await _require_ticket_binding(request, ticket)
    record = await _ticket_or_404(ticket)
    settings = get_settings()
    await _require_configured_provider(hooks, request, record, settings)

    try:
        auth_identity.validate_new_username(username, settings)
    except auth_sso.AuthError as exc:
        await _audit_sso_refusal(hooks, request, record["provider"], exc.reason)
        raise HTTPException(status_code=400, detail=SSO_NAME_DETAIL) from exc

    provider_class = auth_sso.PROVIDER_BY_NAME.get(record["provider"])
    if provider_class is not None and not provider_class.can_provision_user:
        await _audit_sso_refusal(
            hooks, request, record["provider"], "provider_read_only"
        )
        raise HTTPException(
            status_code=403,
            detail="This provider cannot create accounts on this server.",
        )

    try:
        auth_identity.check_email_domain(record["email"], settings)
        resolution = await auth_identity.create_new(
            provider=record["provider"],
            subject=record["subject"],
            email=record["email"],
            username=username,
            settings=settings,
            ticket=ticket,
        )
    except auth_sso.AuthError as exc:
        await _audit_sso_refusal(hooks, request, record["provider"], exc.reason)
        detail = (
            SSO_CONFLICT_DETAIL
            if exc.reason == "identity_conflict"
            else SSO_NAME_DETAIL
        )
        raise HTTPException(
            status_code=sso_error_status(exc.reason), detail=detail
        ) from exc

    response = await sso_issue_session_flow(
        request,
        resolution,
        provider=record["provider"],
        next_path=record.get("next_path") or "/",
        hooks=hooks,
        audit_action="auth.sso_create",
        as_json=True,
    )
    response.delete_cookie(auth_sso_state.TICKET_COOKIE, path="/")
    return response


async def sso_link_account_flow(request: Request, *, hooks: SSOFlowHooks) -> Response:
    """Finish a first login by connecting the identity to an existing account."""
    body = await hooks.read_json_object(request)
    ticket = str(body.get("ticket", ""))
    username = str(body.get("username", "")).strip().lower()
    api_key = str(body.get("api_key", ""))
    if not username or not api_key:
        raise HTTPException(status_code=400, detail="Username and api_key are required")

    await _require_ticket_binding(request, ticket)
    record = await _ticket_or_404(ticket)
    settings = get_settings()
    await _require_configured_provider(hooks, request, record, settings)

    verified = await auth_password.PasswordLogin().verify(username, api_key, settings)
    if verified.authenticated and (
        verified.account is None or username == RESERVED_ADMIN_USERNAME
    ):
        await _audit_sso_refusal(
            hooks,
            request,
            record["provider"],
            "bootstrap_admin_cannot_link",
            subject=username,
        )
        raise HTTPException(status_code=400, detail=BOOTSTRAP_ADMIN_LINK_DETAIL)
    if not verified.authenticated:
        await _audit_sso_refusal(
            hooks,
            request,
            record["provider"],
            "link_credentials_invalid",
            subject=username,
        )
        await hooks.lock_after_failed_login(username, settings, request)
        raise HTTPException(status_code=401, detail="Invalid username or API key")

    locked = await storage.settle_login_attempt(username)
    if locked is not None:
        await _audit_sso_refusal(
            hooks, request, record["provider"], f"account_{locked}", subject=username
        )
        raise HTTPException(status_code=403, detail=SSO_ACCOUNT_DETAIL)

    block_reason = auth_approval.account_block_reason(verified.account)
    if block_reason is not None:
        await _audit_sso_refusal(
            hooks,
            request,
            record["provider"],
            f"account_{block_reason}",
            subject=username,
        )
        raise HTTPException(status_code=403, detail=SSO_ACCOUNT_DETAIL)

    role, is_admin = role_and_admin(verified.account)
    if auth_approval.approval_gate_applies(is_admin=is_admin, settings=settings):
        status = await storage.get_user_status(username) or ""
        if auth_approval.is_approval_blocked(status):
            await _audit_sso_refusal(
                hooks,
                request,
                record["provider"],
                f"account_{status}",
                subject=username,
            )
            raise HTTPException(status_code=403, detail=SSO_ACCOUNT_DETAIL)

    if role not in REVIEWER_ROLES:
        await _audit_sso_refusal(
            hooks,
            request,
            record["provider"],
            "link_forbidden_role",
            subject=username,
        )
        raise HTTPException(
            status_code=403,
            detail="Reviewer access required. Viewers cannot perform this action.",
        )

    if await _sso_link_allow_list_refused(
        hooks,
        request,
        provider=record["provider"],
        username=username,
        settings=settings,
        is_admin=is_admin,
    ):
        raise HTTPException(status_code=403, detail="User not allowed")

    try:
        auth_identity.check_email_domain(record["email"], settings)
        resolution = await auth_identity.link_existing(
            username=username,
            provider=record["provider"],
            subject=record["subject"],
            email=record["email"],
            ticket=ticket,
        )
    except auth_sso.AuthError as exc:
        await _audit_sso_refusal(
            hooks, request, record["provider"], exc.reason, subject=username
        )
        if exc.reason == "account_missing":
            raise HTTPException(
                status_code=404, detail="That account no longer exists"
            ) from exc
        if exc.reason == "identity_conflict":
            detail = SSO_CONFLICT_DETAIL
        elif exc.reason.startswith("account_"):
            detail = SSO_ACCOUNT_DETAIL
        else:
            detail = "That sign-in cannot be used"
        raise HTTPException(
            status_code=sso_error_status(exc.reason), detail=detail
        ) from exc

    response = await sso_issue_session_flow(
        request,
        resolution,
        provider=record["provider"],
        next_path=record.get("next_path") or "/",
        hooks=hooks,
        audit_action="auth.sso_link",
        as_json=True,
    )
    response.delete_cookie(auth_sso_state.TICKET_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# Connected identities payload & unlink orchestration
# ---------------------------------------------------------------------------


def identities_payload(
    identities: list[dict[str, Any]], *, has_api_key: bool = False
) -> dict[str, Any]:
    """Shape the sign-in-methods answer for the self-service and admin routes."""
    return {
        "identities": [
            {
                "provider": identity["provider"],
                "display_name": SSO_PROVIDER_DISPLAY_NAMES.get(
                    identity["provider"], identity["provider"]
                ),
                "email": identity["email"],
                "created_at": identity["created_at"],
            }
            for identity in identities
        ],
        "unlinkable": len(identities) > 1 or (len(identities) == 1 and has_api_key),
        "has_api_key": has_api_key,
    }


async def _audit_unlink_refusal(
    hooks: SSOFlowHooks,
    request: Request,
    principal: Principal,
    username: str,
    provider: str,
    *,
    reason: str,
    as_admin: bool,
) -> None:
    """Record a refused sign-in-method removal before raising."""
    await hooks.record_audit(
        action="auth.identity_unlink_refused",
        actor=principal.username,
        actor_role=principal.role,
        is_admin=principal.is_admin,
        message=(
            f"SSO identity removal refused ({reason}):"
            f" user='{username}' provider='{provider}'"
        ),
        target_type="user",
        target_id=username,
        outcome="denied",
        details={"provider": provider, "reason": reason, "as_admin": as_admin},
        request=request,
    )


async def unlink_identity_flow(
    request: Request,
    username: str,
    provider: str,
    *,
    audit_action: str,
    as_admin: bool,
    hooks: SSOFlowHooks,
) -> None:
    """Remove one provider from an account, refusing the last one for keyless accounts."""
    principal = auth_principal.get_principal(request)
    if as_admin and await storage.get_user_by_username(username) is None:
        await _audit_unlink_refusal(
            hooks,
            request,
            principal,
            username,
            provider,
            reason="not_connected",
            as_admin=as_admin,
        )
        raise HTTPException(status_code=404, detail="User not found")

    try:
        outcome = await storage.unlink_user_identity(username, provider)
    except ValueError:
        outcome = "not_found"

    if outcome == "not_found":
        await _audit_unlink_refusal(
            hooks,
            request,
            principal,
            username,
            provider,
            reason="not_connected",
            as_admin=as_admin,
        )
        raise HTTPException(status_code=404, detail="That provider is not connected")
    if outcome == "last":
        await _audit_unlink_refusal(
            hooks,
            request,
            principal,
            username,
            provider,
            reason="last_method",
            as_admin=as_admin,
        )
        raise HTTPException(status_code=409, detail=LAST_IDENTITY_DETAIL)

    await hooks.record_audit(
        action=audit_action,
        actor=principal.username,
        actor_role=principal.role,
        is_admin=principal.is_admin,
        message=(
            f"{'Admin unlinked an SSO identity' if as_admin else 'SSO identity unlinked'}:"
            f" user='{username}' provider='{provider}'"
        ),
        target_type="user",
        target_id=username,
        details={"provider": provider},
        request=request,
    )


# ---------------------------------------------------------------------------
# CLI device-authorization state machine
# ---------------------------------------------------------------------------


def device_client_name(raw: object) -> str | None:
    """Normalise the label a CLI device-flow client reports about itself."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(raw or ""))
    collapsed = " ".join(text.split())
    return collapsed or None


def device_approval_uri(settings: Settings) -> str:
    """Where a human approves a device code (never derived from Host headers)."""
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}{DEVICE_APPROVAL_PATH}" if base else DEVICE_APPROVAL_PATH


def device_caller_key(request: Request, settings: Settings | None = None) -> str:
    """Stable key for the per-caller live device-code ceiling."""
    active_settings = settings if settings is not None else get_settings()
    if active_settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client else "unknown"


async def sso_device_start_flow(
    request: Request, *, hooks: SSOFlowHooks
) -> JSONResponse:
    """Open a CLI device-authorization flow."""
    body = await hooks.read_json_object(request)
    settings = get_settings()
    if not auth_sso.configured_providers(settings):
        raise HTTPException(status_code=404, detail="No SSO provider is configured")
    provider = str(body.get("provider") or "").strip().lower() or None
    if provider is not None and auth_sso.get_provider(provider, settings) is None:
        raise HTTPException(status_code=400, detail="Unknown SSO provider")
    client_name = device_client_name(body.get("client_name"))
    if client_name is not None and len(client_name) > DEVICE_CLIENT_NAME_MAX:
        raise HTTPException(status_code=400, detail="client_name is too long")

    try:
        record = await storage.create_device_code(
            ttl_seconds=settings.sso_device_code_ttl_seconds,
            interval_seconds=settings.sso_device_poll_interval_seconds,
            provider=provider,
            client_name=client_name,
            max_live=MAX_LIVE_DEVICE_CODES,
            max_live_per_caller=MAX_LIVE_DEVICE_CODES_PER_CALLER,
            client_ip=device_caller_key(request, settings),
        )
    except storage.DeviceCodeCapacityError as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many pending sign-in codes. Try again in a few minutes.",
        ) from exc

    await hooks.record_audit(
        action="auth.sso_device_started",
        actor="anonymous",
        message=(
            f"SSO device code started for provider '{provider or 'any'}'"
            f" client='{client_name or 'unknown'}'"
        ),
        target_type="provider",
        target_id=provider or "",
        details={"provider": provider, "client_name": client_name},
        request=request,
    )
    response = JSONResponse(
        content={
            "device_code": record["device_code"],
            "user_code": record["user_code"],
            "verification_uri": device_approval_uri(settings),
            "expires_in": settings.sso_device_code_ttl_seconds,
            "interval": record["interval_seconds"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def _redeem_claimed_device_code_flow(
    request: Request,
    device_code: str,
    *,
    username: str,
    provider: str | None,
    settings: Settings,
    hooks: SSOFlowHooks,
) -> JSONResponse:
    """Validate eligibility & domain policy on a claimed code, then mint its session."""
    user = await storage.get_user_by_username(username)
    if user is None and username != RESERVED_ADMIN_USERNAME:
        await _audit_sso_refusal(
            hooks, request, provider, "account_missing", subject=username
        )
        return JSONResponse(content={"status": "denied"})

    block_reason = auth_approval.account_block_reason(user)
    if block_reason is not None:
        await _audit_sso_refusal(
            hooks, request, provider, f"account_{block_reason}", subject=username
        )
        return JSONResponse(content={"status": "denied"})

    if provider is not None:
        identities = await storage.list_user_identities(username)
        approving = next((i for i in identities if i.get("provider") == provider), None)
        refusal = auth_identity.stored_identity_domain_refusal(
            (approving or {}).get("email"), settings
        )
        if refusal is not None:
            await _audit_sso_refusal(
                hooks, request, provider, refusal, subject=username
            )
            return JSONResponse(content={"status": "denied"})

    role, is_admin = (
        ("admin", True)
        if user is None
        else role_and_admin(user, settings.default_user_role)
    )
    if auth_approval.approval_gate_applies(is_admin=is_admin, settings=settings):
        status = await storage.get_user_status(username) or ""
        if auth_approval.is_approval_blocked(status):
            await _audit_sso_refusal(
                hooks, request, provider, f"account_{status}", subject=username
            )
            return JSONResponse(content={"status": "denied"})

    def _domain_check(email: str | None) -> str | None:
        return auth_identity.stored_identity_domain_refusal(email, get_settings())

    minted = await storage.exchange_device_code_for_session(
        device_code,
        username=username,
        approved_provider=provider,
        eligibility_check=auth_approval.default_eligibility_check,
        domain_check=_domain_check,
    )
    if minted is not None and minted.refused is not None:
        await _audit_sso_refusal(
            hooks, request, provider, minted.refused, subject=username
        )
        return JSONResponse(content={"status": "denied"})
    if minted is None:
        return JSONResponse(content={"status": "expired"})

    token = minted.token
    role, is_admin = minted.role, minted.is_admin
    await hooks.record_audit(
        action="auth.sso_device_redeemed",
        actor=username,
        actor_role=role,
        is_admin=is_admin,
        message=f"SSO device code redeemed: user='{username}'",
        target_type="user",
        target_id=username,
        details={"provider": provider, "role": role},
        request=request,
    )
    response = JSONResponse(
        content={
            "status": "approved",
            "token": token,
            "username": username,
            "provider": provider,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def sso_device_poll_flow(
    request: Request, *, hooks: SSOFlowHooks
) -> JSONResponse:
    """Answer the CLI's poll: pending, slow_down, expired, denied, or approved."""
    body = await hooks.read_json_object(request)
    device_code = str(body.get("device_code") or "")
    settings = get_settings()
    record = await storage.get_device_code(device_code) if device_code else None
    if record is None:
        return JSONResponse(content={"status": "expired"})
    if not await storage.record_device_code_poll(device_code):
        return JSONResponse(
            content={"status": "slow_down", "interval": record["interval_seconds"]}
        )
    if not record["authorized"]:
        return JSONResponse(
            content={"status": "pending", "interval": record["interval_seconds"]}
        )

    redeemed = await storage.redeem_device_code(device_code)
    if redeemed is None:
        return JSONResponse(content={"status": "expired"})
    try:
        return await _redeem_claimed_device_code_flow(
            request,
            device_code,
            username=redeemed["username"],
            provider=redeemed["approved_provider"],
            settings=settings,
            hooks=hooks,
        )
    except Exception:
        await storage.release_device_code(device_code)
        raise


def device_code_request_is_forgeable(
    request: Request, principal: Principal, settings: Settings | None = None
) -> bool:
    """Whether an approval request uses an ambient credential with no same-origin proof."""
    if principal.auth_strategy == auth_api_key.ApiKeyStrategy.name:
        return False
    return not request_is_same_origin(request, settings)


async def _device_code_or_404(
    request: Request, principal: Principal, *, hooks: SSOFlowHooks
) -> dict[str, Any]:
    """Verify same-origin proof (before reading body), then load the live user code."""
    if device_code_request_is_forgeable(request, principal):
        await _audit_device_approve_refusal(
            hooks, request, principal, "cross_origin", provider=None
        )
        raise HTTPException(
            status_code=403,
            detail="Approval must come from this server's own approval page",
        )
    body = await hooks.read_json_object(request)
    user_code = str(body.get("user_code") or "").strip()
    record = (
        await storage.get_device_code_by_user_code(user_code) if user_code else None
    )
    if record is None:
        await _audit_device_approve_refusal(
            hooks, request, principal, "code_not_found", provider=None
        )
        raise HTTPException(status_code=404, detail="That code is not valid")
    return record


async def sso_device_lookup_flow(
    request: Request, *, hooks: SSOFlowHooks
) -> JSONResponse:
    """Describe a pending device code for the authenticated approval screen."""
    principal = auth_principal.get_principal(request)
    record = await _device_code_or_404(request, principal, hooks=hooks)
    return JSONResponse(
        content={
            "user_code": record["user_code"],
            "provider": record["provider"],
            "client_name": record["client_name"],
            "expires_at": record["expires_at"],
            "authorized": record["authorized"],
        }
    )


async def sso_device_approve_flow(
    request: Request, *, hooks: SSOFlowHooks
) -> JSONResponse:
    """Approve a device code for the signed-in principal."""
    principal = auth_principal.get_principal(request)
    record = await _device_code_or_404(request, principal, hooks=hooks)
    user_code = record["user_code"]
    username = principal.username
    # Provider comes solely from the typed Principal resolved by AuthMiddleware,
    # never from re-reading cookies or session rows.
    provider = principal.auth_provider

    if record["authorized"]:
        await _audit_device_approve_refusal(
            hooks, request, principal, "already_approved", provider=provider
        )
        raise HTTPException(status_code=409, detail="That code was already approved")
    if (
        record["provider"] is not None
        and provider is not None
        and record["provider"] != provider
    ):
        await _audit_device_approve_refusal(
            hooks, request, principal, "provider_mismatch", provider=record["provider"]
        )
        raise HTTPException(
            status_code=400,
            detail="That code was opened for a different provider",
        )

    refusal = await storage.authorize_device_code(
        user_code, username=username, provider=provider
    )
    if refusal != "approved":
        await _audit_device_approve_refusal(
            hooks, request, principal, refusal, provider=provider
        )
        if refusal == "already_approved":
            raise HTTPException(
                status_code=409, detail="That code was already approved"
            )
        raise HTTPException(
            status_code=410,
            detail=(
                "That code is no longer valid; start a new sign-in from your"
                " terminal to get a fresh one"
            ),
        )

    await hooks.record_audit(
        action="auth.sso_device_approved",
        actor=username,
        actor_role=principal.role,
        is_admin=principal.is_admin,
        message=(
            f"SSO device code approved: user='{username}'"
            f" provider='{provider or 'none'}'"
            f" requested='{record['provider'] or 'any'}'"
        ),
        target_type="user",
        target_id=username,
        details={
            "provider": provider,
            "requested_provider": record["provider"],
            "client_name": record["client_name"],
        },
        request=request,
    )
    return JSONResponse(
        content={
            "ok": True,
            "user_code": record["user_code"],
            "provider": provider,
        }
    )
