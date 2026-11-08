"""Trusted reverse-proxy (SSO) header authentication."""

from __future__ import annotations

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from rootcoz import storage
from rootcoz.auth.approval import account_block_reason
from rootcoz.auth.base import (
    RESERVED_ADMIN_USERNAME,
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

#: Header naming the user the reverse proxy already authenticated.
PROXY_USER_HEADER = "x-forwarded-user"


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
    if username == RESERVED_ADMIN_USERNAME:
        return ""
    return username


class ProxyStrategy(AuthStrategy):
    """Authenticate ``X-Forwarded-User`` from a trusted reverse proxy.

    The proxy has already authenticated the caller, so the header alone is the
    credential — which is why it is only read when ``TRUST_PROXY_HEADERS`` is
    on and nothing else authenticated the request.

    A name with no users row still authenticates, with ``DEFAULT_USER_ROLE``
    and no row loaded: the proxy is the account boundary. It is never an
    administrator by default — the administrator bit comes from the row only.
    :data:`~rootcoz.auth.base.RESERVED_ADMIN_USERNAME` is always refused,
    because the bootstrap administrator has no row to resolve. Creating the
    missing row is a later stage; this stage keeps the pre-existing behaviour of
    authenticating without one.
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
