"""Trusted reverse-proxy (SSO) header authentication."""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from rootcoz import get_child_logger, storage
from rootcoz.auth.approval import account_block_reason
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
from rootcoz.auth.identity import is_reserved_username
from rootcoz.config import Settings

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
    # The reserved identities are refused before anything else: a proxy that
    # forwards "rootcoz-ai" or "admin" is not naming a person, and provisioning
    # must never be a way to occupy a system name.
    if is_reserved_username(username):
        return ""
    return username


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
            user = await self._provision(username, ctx.settings, ctx.request)
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

    async def _provision(
        self, username: str, settings: Settings, request: Request
    ) -> dict[str, Any] | None:
        """Create the row for a proxy-vouched account on its first request.

        Args:
            username: The normalised name from the trusted header.
            settings: Application settings (approval rule).
            request: The request that triggered it, so the caller can audit and
                notify with its context.

        Returns:
            The new users row, or ``None`` when the account could not be created
            (a name the storage layer refuses). The caller then refuses the
            request: a row-less identity would bypass the approval gate that
            every provisioned account now passes through.
        """
        if settings.allowed_users_set and username not in settings.allowed_users_set:
            # The write allow-list bounds *creation* on every other path
            # (registration, SSO create); a proxy header is not an exemption from
            # it. Writes would be blocked later anyway — refusing here keeps the
            # rule in one place and leaves no orphan row behind.
            logger.info(
                "Refused proxy account '%s': not in the write allow-list", username
            )
            # Same answer as an unusable name: no row, no identity — and the
            # serving layer writes the refusal to the trail.
            setattr(
                request.state,
                PROVISION_REFUSED_ATTR,
                {"username": username, "reason": "not_allowed"},
            )
            return None
        status = "pending" if settings.require_approval else "active"
        try:
            await storage.create_user(username, status=status, created_via="proxy")
        except ValueError:
            # Provisioning by name is idempotent, and a browser fires several
            # requests in parallel on its first page load: the losers of that race
            # see "already has an API key" — a ValueError for a name that was, in
            # fact, just provisioned. Re-read before refusing, or the account's
            # very first load would answer 401 to some of its own requests.
            existing = await storage.get_user_by_username(username)
            if existing is None:
                # No row: the name is malformed or reserved. That is not an
                # identity, so the request is refused rather than given a
                # row-less session.
                logger.warning("Refused proxy account '%s': unusable name", username)
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


async def proxy_login_redirect(ctx: AuthContext) -> Response | None:
    """Redirect a proxy-authenticated visitor away from the login page.

    Args:
        ctx: The request being authenticated.

    Returns:
        A ``303`` to the dashboard when the proxy vouches for a usable user, or
        ``None`` when the login page should be served (no proxy identity, an
        existing session, or a blocked account — a blocked identity sent to
        ``/`` would only bounce back here, one audit row per hop).
    """
    username = proxy_username(ctx.request, ctx.settings)
    if not username:
        return None
    session_token = read_cookie(ctx.request, SESSION_COOKIE)
    if session_token and await storage.get_session(session_token):
        return None
    if account_block_reason(await storage.get_user_by_username(username)):
        return None
    response: Response = RedirectResponse(url="/", status_code=303)
    if read_cookie(ctx.request, USERNAME_COOKIE) != username:
        set_username_cookie(response, username, secure=ctx.settings.secure_cookies)
    return response
