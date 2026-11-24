"""Trusted reverse-proxy (SSO) header authentication."""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from rootcoz import get_child_logger, storage
from rootcoz.auth.approval import (
    account_block_reason,
    approval_gate_applies,
    initial_user_status,
    is_approval_blocked,
)
from rootcoz.auth.base import (
    SESSION_COOKIE,
    SET_PROXY_COOKIE_ATTR,
    USERNAME_COOKIE,
    AuthContext,
    AuthIdentity,
    AuthStrategy,
    read_cookie,
    role_and_admin,
    set_username_cookie,
)
from rootcoz.config import Settings

#: Identities that must never authenticate via proxy even if a row exists: the
#: bootstrap superuser and the AI system account. A proxy forwarding one of
#: these names is not naming a person.
_HARD_REFUSED_USERNAMES: frozenset[str] = frozenset(
    {"admin", storage.AI_SYSTEM_USERNAME}
)

#: Header naming the user the reverse proxy already authenticated.
PROXY_USER_HEADER = "x-forwarded-user"

#: ``request.state`` attribute carrying the account provisioned by this request,
#: so the serving layer can audit it and refresh the pending badge.
PROVISIONED_ATTR = "proxy_provisioned"

#: ``request.state`` attribute carrying a refusal to provision, with its reason,
#: so the serving layer can audit it. A name the deployment will not create an
#: account for is not authenticated at all, which is worth a row.
PROVISION_REFUSED_ATTR = "proxy_provision_refused"

#: Module logger, through the house helper so the package keeps one level authority.
logger = get_child_logger(__name__)


def proxy_username(request: Request, settings: Settings) -> str:
    """The proxy-vouched username, or ``""`` when there is none to use.

    One reader for the header so the name, the normalisation and the reserved
    username exist once. Callers that need the raw header (to report which
    credential a login used) read :data:`PROXY_USER_HEADER` directly.

    Args:
        request: The incoming request.
        settings: Application settings (``TRUST_PROXY_HEADERS``).

    Returns:
        The lower-cased username, or ``""`` when proxy headers are not trusted,
        the header is blank, or it names the reserved administrator.
    """
    if not settings.trust_proxy_headers:
        return ""
    username = request.headers.get(PROXY_USER_HEADER, "").strip().lower()
    # The bootstrap administrator and the AI system identity are refused
    # unconditionally — a proxy that forwards "admin" or "rootcoz-ai" is not
    # naming a person. Other ``rootcoz*`` names may be legacy accounts an
    # administrator created before the prefix restriction, so they are only
    # refused at *provisioning* time (see ``_proxy_provision_refusal``).
    if username in _HARD_REFUSED_USERNAMES:
        return ""
    # System tags ("re-analyze" etc.) are never people either.
    if username in storage.RESERVED_USERNAMES:
        return ""
    return username


def _proxy_provision_refusal(username: str, settings: Settings) -> str | None:
    """Whether an account can be created for a proxy-vouched name, and why not.

    The one eligibility rule for this path: :meth:`ProxyStrategy._provision`
    obeys it before creating a row, and the ``/login`` redirect obeys it before
    sending a visitor to a page that would refuse them. A name the deployment
    will not create must stay on the login page — sent to ``/`` the middleware
    refuses it, sends the HTML request back here, and the two bounce forever.

    Args:
        username: The normalised name from the trusted header.
        settings: Application settings (the ``ALLOWED_USERS`` write allow-list).

    Returns:
        ``"not_allowed"`` for a name outside ``ALLOWED_USERS``,
        ``"unusable_name"`` for one the user store would refuse (malformed or
        reserved), or ``None`` when an account may be created.
    """
    if settings.allowed_users_set and username not in settings.allowed_users_set:
        # The write allow-list bounds *creation* on every other path
        # (registration, SSO create); a proxy header is not an exemption from it.
        return "not_allowed"
    # A name the store would refuse (malformed, reserved prefix, system tag)
    # is not an account either. This enforces the ``rootcoz*`` prefix
    # restriction on brand-new accounts — existing accounts bearing a
    # ``rootcoz*`` name are allowed to authenticate by ``proxy_username``
    # and ``ProxyStrategy.authenticate`` (which skips provisioning when a row
    # already exists).
    if not storage.is_creatable_username(username):
        return "unusable_name"
    return None


def _record_refusal(request: Request, username: str, reason: str) -> None:
    """Announce a refused name on the request, for the serving layer's audit row.

    The auth package owns no auditing, so every refusal leaves the same record
    here and :func:`rootcoz.main._record_proxy_provision_refused` writes it.

    Args:
        request: The request the refusal applies to.
        username: The refused name.
        reason: Why, as a fixed code (see :func:`_proxy_provision_refusal`).
    """
    setattr(
        request.state,
        PROVISION_REFUSED_ATTR,
        {"username": username, "reason": reason},
    )


async def _provision_proxy_user(
    username: str, settings: Settings, request: Request
) -> dict[str, Any] | None:
    """Create the row for a proxy-vouched account on its first request.

    Shared by :meth:`ProxyStrategy.authenticate` (normal dispatch) and
    :func:`_login_destination` (``/login`` redirect) so the provisioning
    logic, its refusal checks and its audit signals exist exactly once.

    Args:
        username: The normalised name from the trusted header.
        settings: Application settings (approval rule).
        request: The request that triggered it, so the caller can audit and
            notify with its context.

    Returns:
        The new users row, or ``None`` when provisioning refuses the name
        (:func:`_proxy_provision_refusal`). The caller then refuses the
        request: a row-less identity would bypass the approval gate that
        every provisioned account now passes through.
    """
    refusal = _proxy_provision_refusal(username, settings)
    if refusal:
        logger.info("Refused proxy account '%s': %s", username, refusal)
        # No row, no identity — and the serving layer writes the refusal to
        # the trail.
        _record_refusal(request, username, refusal)
        return None
    status = initial_user_status(settings)
    try:
        # Keyless: the header is this account's credential, so provisioning
        # must not mint a secret nobody is ever shown. The same role rule,
        # status rule and provenance as every other creation path.
        await storage.create_keyless_user(username, status=status, created_via="proxy")
    except ValueError:
        # Provisioning by name is idempotent, and a browser fires several
        # requests in parallel on its first page load: the losers of that race
        # see "already exists" — a ValueError for a name that was, in fact,
        # just provisioned. Re-read before refusing, or the account's
        # very first load would answer 401 to some of its own requests.
        existing = await storage.get_user_by_username(username)
        if existing is None:
            # No row: the store refused the name, so there is no account to
            # authenticate. That is not an identity, and the request is
            # refused rather than given a row-less session.
            logger.warning("Refused proxy account '%s': unusable name", username)
            _record_refusal(request, username, "unusable_name")
            return None
        return existing
    user = await storage.get_user_by_username(username)
    # Announced on the request so the serving layer can write one audit row
    # and refresh the pending badge: the auth package never imports the app.
    # The role travels with it because the middleware writes the row before
    # ``request.state.role`` is assigned, and an audit row that reports the
    # dispatch default instead of the account's real role is a wrong row.
    setattr(
        request.state,
        PROVISIONED_ATTR,
        {
            "username": username,
            "status": status,
            "role": user["role"] if user else settings.default_user_role,
        },
    )
    return user


class ProxyStrategy(AuthStrategy):
    """Authenticate ``X-Forwarded-User`` from a trusted reverse proxy.

    The proxy has already authenticated the caller, so the header alone is the
    credential — which is why it is only read when ``TRUST_PROXY_HEADERS`` is
    on and nothing else authenticated the request.

    A name with no users row is **provisioned** on its first request: the proxy
    has already authenticated the person, so the row records who they are rather
    than granting them anything. ``created_via`` says ``proxy``, the role comes
    from ``DEFAULT_USER_ROLE``, and the status follows the same rule as every
    other new account — ``pending`` while ``REQUIRE_APPROVAL`` is on, ``active``
    when it is off — because approval is the admission gate for a proxy-vouched
    account exactly as it is for a self-registered one. The provider proves
    *who* someone is, never that they may act.

    The provisioned row holds **no API key**: the header is this account's
    credential, so a key minted at provisioning would be a secret nobody was
    ever shown. A person whose proxy goes away has their key rotated by an
    administrator (``admin users rotate-key``) — never by claiming the name.

    It is never an administrator by default — the administrator bit comes from
    the row only. :data:`~rootcoz.auth.base.RESERVED_ADMIN_USERNAME` is always
    refused, because the bootstrap administrator has no row to resolve.
    """

    name = "proxy"

    def is_applicable(self, ctx: AuthContext) -> bool:
        """Whether the proxy header names a usable user (no DB access).

        Args:
            ctx: The request being authenticated.

        Returns:
            True when proxy headers are trusted and the header names a usable
            user. Whether it may win over an identity another strategy already
            resolved is decided by :func:`rootcoz.auth.registry.may_replace`.
        """
        return bool(proxy_username(ctx.request, ctx.settings))

    async def authenticate(self, ctx: AuthContext) -> AuthIdentity | None:
        """Resolve the proxy-vouched username and its DB role.

        Args:
            ctx: The request being authenticated.

        Returns:
            The proxy identity, or ``None`` when the header is absent, names
            the reserved ``admin``, or proxy headers are not trusted.
        """
        username = proxy_username(ctx.request, ctx.settings)
        if not username:
            return None
        user = await storage.get_user_by_username(username)
        if user is None:
            user = await _provision_proxy_user(username, ctx.settings, ctx.request)
            if user is None:
                # A name the store refuses is not a person the deployment can
                # account for. Authenticating anyway would return a row-less
                # identity, and a row-less identity is past the approval gate —
                # exactly what this stage exists to close.
                return None
        role, is_admin = role_and_admin(user, ctx.settings.default_user_role)
        # Refresh the display cookie on the response.
        setattr(ctx.request.state, SET_PROXY_COOKIE_ATTR, username)
        return AuthIdentity(
            username=username,
            strategy_name=self.name,
            is_admin=is_admin,
            role=role,
            user=user,
        )


async def _login_destination(username: str, ctx: AuthContext) -> str | None:
    """Where a proxy-vouched visitor on the login page belongs.

    A first-time visitor is **provisioned** here — the same
    :func:`_provision_proxy_user` the main dispatch uses — so the account
    exists before this returns, an administrator can see and approve it, and
    the redirect target always matches a real stored status.

    Args:
        username: The normalised name from the trusted header.
        ctx: The request being authenticated, and the settings it runs under.

    Returns:
        ``/pending`` for an account ``REQUIRE_APPROVAL`` holds, ``/`` for one
        that may act, or ``None`` when there is nowhere better to go: the name
        cannot be provisioned (recorded on the request for the trail), or its
        account is blocked.
    """
    user = await storage.get_user_by_username(username)
    if user is None:
        user = await _provision_proxy_user(username, ctx.settings, ctx.request)
        if user is None:
            # Refused by _proxy_provision_refusal or unusable name — already
            # recorded on the request for the serving layer's audit row.
            return None
    if account_block_reason(user):
        return None
    status = await storage.get_user_status(username)
    if status is None:
        # Defensive: the row was just provisioned or already existed, so this
        # should never be None. Fall back to the initial status.
        status = initial_user_status(ctx.settings)
    # The same three conditions the middleware's approval gate applies, so this
    # can never send a visitor to a page that gate would refuse.
    _, is_admin = role_and_admin(user, ctx.settings.default_user_role)
    if approval_gate_applies(is_admin=is_admin, settings=ctx.settings) and (
        is_approval_blocked(status)
    ):
        return "/pending" if status == "pending" else None
    return "/"


async def proxy_login_redirect(ctx: AuthContext) -> Response | None:
    """Redirect a proxy-authenticated visitor away from the login page.

    Args:
        ctx: The request being authenticated.

    Returns:
        A ``303`` to the dashboard, or to the pending page while approval holds
        the account, when the proxy vouches for a user the deployment will
        create. ``None`` when the login page should be served (no proxy
        identity, an existing session, a name provisioning would refuse, or a
        blocked account — anything else would only bounce back here, one audit
        row per hop).
    """
    username = proxy_username(ctx.request, ctx.settings)
    if not username:
        return None
    session_token = read_cookie(ctx.request, SESSION_COOKIE)
    if session_token and await storage.get_session(session_token):
        return None
    destination = await _login_destination(username, ctx)
    if destination is None:
        return None
    response: Response = RedirectResponse(url=destination, status_code=303)
    if read_cookie(ctx.request, USERNAME_COOKIE) != username:
        set_username_cookie(response, username, secure=ctx.settings.secure_cookies)
    return response
