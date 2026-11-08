"""Shared primitives for the auth strategies.

Owns the ``AuthStrategy`` ABC and the two dataclasses a strategy exchanges with
:mod:`rootcoz.auth.registry`, plus the pieces more than one strategy needs: the
cookie names with their pre-rename fallback, the display-cookie setter, the
bootstrap-administrator check, the role/administrator rule and the
session→identity mapping (both shared with the login path), and the
effective-reports rule.

The package depends on ``rootcoz.storage``, ``rootcoz.config`` and
:mod:`rootcoz` logging only — never on ``rootcoz.main``, which imports it.
"""

from __future__ import annotations

import hmac
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response

from rootcoz import storage
from rootcoz.config import Settings

#: Session cookie holding a verified credential (see :mod:`rootcoz.auth.session`).
SESSION_COOKIE = "rootcoz_session"

#: Display-only cookie naming the user. It grants nothing — see the fallback in
#: ``AuthMiddleware.dispatch``.
USERNAME_COOKIE = "rootcoz_username"

#: ``request.state`` attribute a strategy sets when it renewed the session in
#: the DB, so the caller re-issues the cookie.
RENEW_SESSION_TOKEN_ATTR = "renew_session_token"

#: ``request.state`` attribute a strategy sets when a proxy identity should
#: refresh the display cookie on the response.
SET_PROXY_COOKIE_ATTR = "set_proxy_cookie"

#: The bootstrap administrator's reserved username. It has no users row, so every
#: path that resolves a name must refuse it: the proxy header, the display cookie,
#: the row lookup, and the login pair (which needs ``ADMIN_KEY`` instead).
RESERVED_ADMIN_USERNAME = "admin"

# Pre-rename cookie names from the jenkins-job-insight era. Still read as a
# fallback so an upgrade does not log every existing browser out.
LEGACY_COOKIE_MAP: dict[str, str] = {
    USERNAME_COOKIE: "jji_username",
    SESSION_COOKIE: "jji_session",
}


def read_cookie(request: Request, name: str) -> str:
    """Read a cookie, falling back to its legacy pre-rename name.

    Args:
        request: The incoming request.
        name: Current cookie name (``rootcoz_*``).

    Returns:
        The cookie value, or ``""`` when the browser sent neither name.
    """
    value = request.cookies.get(name, "")
    if not value:
        legacy = LEGACY_COOKIE_MAP.get(name)
        if legacy:
            value = request.cookies.get(legacy, "")
    return value


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Issue the verified-session cookie, clearing its pre-rename twin.

    Args:
        response: Response to attach the cookie to.
        token: Raw session token from :func:`rootcoz.storage.create_session`.
        secure: Whether the deployment serves HTTPS (``SECURE_COOKIES``).
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=storage.current_session_ttl_seconds(),
    )
    # Clear the pre-rename cookie once the new one is issued.
    response.delete_cookie(
        LEGACY_COOKIE_MAP[SESSION_COOKIE],
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    """Expire the session cookie and its pre-rename twin (logout).

    Args:
        response: Response to clear the cookies on.
        secure: Whether the deployment serves HTTPS (``SECURE_COOKIES``).
    """
    for name in (SESSION_COOKIE, LEGACY_COOKIE_MAP[SESSION_COOKIE]):
        response.delete_cookie(
            name,
            httponly=True,
            samesite="lax",
            secure=secure,
        )


def set_username_cookie(response: Response, username: str, *, secure: bool) -> None:
    """Set the display-only username cookie, clearing its legacy twin.

    Args:
        response: Response to attach the cookie to.
        username: Username to display.
        secure: Whether the deployment serves HTTPS (``SECURE_COOKIES``).
    """
    response.set_cookie(
        USERNAME_COOKIE,
        username,
        path="/",
        max_age=365 * 24 * 60 * 60,
        samesite="lax",
        secure=secure,
    )
    # Clear the pre-rename cookie after migration.
    response.delete_cookie(LEGACY_COOKIE_MAP[USERNAME_COOKIE], path="/")


def effective_can_view_reports(is_admin: bool, stored_flag: bool = False) -> bool:
    """Effective reports access.

    Args:
        is_admin: Whether the caller has admin rights (admins always get it).
        stored_flag: The users-table grant; ignored for admins.

    Returns:
        True when reports access is granted.
    """
    return True if is_admin else stored_flag


def matches_bootstrap_admin(credential: str, settings: Settings) -> bool:
    """Whether a presented secret is the deployment's ``ADMIN_KEY``.

    Compared as bytes: :func:`hmac.compare_digest` raises ``TypeError`` on a
    non-ASCII ``str``, and an unauthenticated caller controls one side of the
    comparison — a latin-1 header value, or an ``ADMIN_KEY`` pasted with a stray
    non-ASCII character, would otherwise turn the auth path into a 500.

    Args:
        credential: The secret presented by the caller.
        settings: Application settings carrying ``ADMIN_KEY``.

    Returns:
        True when the credential is the bootstrap administrator's key.
    """
    admin_key = settings.admin_key
    if not admin_key:
        return False
    return hmac.compare_digest(credential.encode(), admin_key.encode())


def role_and_admin(
    user: dict[str, Any] | None, default_role: str = ""
) -> tuple[str, bool]:
    """Role and administrator bit for an identity backed by a users row.

    The administrator bit is derived from the row only. A strategy that
    authenticates without a row (the trusted proxy vouching for a name nobody
    registered) gets ``DEFAULT_USER_ROLE`` and can never be an administrator by
    configuration default alone.

    Args:
        user: The users row, or ``None`` when the identity has none.
        default_role: Role to use when there is no row. Only a caller that can
            authenticate an identity without one (the trusted proxy) passes it.

    Returns:
        ``(role, is_admin)``.
    """
    role = str(user.get("role", "reviewer")) if user else default_role
    return role, bool(user) and role == "admin"


def identity_from_session(session: dict[str, Any], strategy_name: str) -> AuthIdentity:
    """Map a stored session row to an identity.

    One mapping for both callers: the session cookie and the Bearer credential
    that carries a session token (AI internal calls).

    Args:
        session: The sessions row.
        strategy_name: Name of the strategy that authenticated the caller.

    Returns:
        The identity described by the session.
    """
    is_admin = bool(session["is_admin"])
    return AuthIdentity(
        username=str(session["username"]),
        strategy_name=strategy_name,
        is_admin=is_admin,
        role="admin" if is_admin else str(session.get("role", "reviewer")),
    )


@dataclass
class AuthContext:
    """What a strategy may look at: the request and the settings it runs under.

    Attributes:
        request: The request being authenticated.
        settings: Settings for this request, read at call time. Rootcoz reloads
            settings per request, so a strategy must never capture them.
    """

    request: Request
    settings: Settings


@dataclass
class AuthIdentity:
    """An authenticated caller plus the facts the middleware still needs.

    Attributes:
        username: Resolved username.
        strategy_name: ``AuthStrategy.name`` that produced this identity.
        is_admin: Whether the caller has admin rights.
        role: Resolved role (``admin`` / ``operator`` / ``reviewer`` / ...).
        user: The users row already loaded while resolving, reused downstream
            for ``can_view_reports`` and the provider grants so one request
            loads it once. ``None`` when the credential names no row.
    """

    username: str
    strategy_name: str
    is_admin: bool
    role: str
    user: dict[str, Any] | None = None


class AuthStrategy(ABC):
    """One way to authenticate a request.

    Strategies are probed in :data:`rootcoz.auth.registry.AUTH_CHAIN` order by
    :func:`rootcoz.auth.registry.resolve_identity`. A strategy never refuses a
    request: it returns ``None`` and lets the chain keep what it already has.

    A strategy may record a cookie side effect on ``ctx.request.state``, which
    the middleware applies to the response:
    :data:`RENEW_SESSION_TOKEN_ATTR` (a session was renewed in the DB and the
    cookie must be re-issued) and :data:`SET_PROXY_COOKIE_ATTR` (refresh the
    display cookie). Precedence is never declared by a strategy:
    :func:`rootcoz.auth.registry.may_replace` owns it.

    Class attributes:
        name: Stable identifier. :func:`rootcoz.auth.registry.may_replace`
            matches on it, and it names the strategy in audit rows. Required,
            and unique among ``AUTH_CHAIN`` names — the registry refuses to
            import a chain with a duplicate.
    """

    name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a strategy that forgot its identifier.

        Args:
            **kwargs: Passed through to the base implementation.

        Raises:
            TypeError: When the subclass declares no ``name`` — an unnamed
                strategy can never be named by the precedence policy and
                would produce an unattributable identity.
        """
        super().__init_subclass__(**kwargs)
        if not cls.name:
            raise TypeError(f"{cls.__name__} must declare a non-empty name")

    @abstractmethod
    def is_applicable(self, ctx: AuthContext) -> bool:
        """Whether this strategy has a credential worth probing for this request.

        A header/cookie check only — no database access, and no opinion about
        precedence: :func:`rootcoz.auth.registry.may_replace` decides which
        strategy may win, so a strategy never needs to know another one's name.

        Args:
            ctx: The request being authenticated.

        Returns:
            True when :meth:`authenticate` should be called.
        """

    @abstractmethod
    async def authenticate(self, ctx: AuthContext) -> AuthIdentity | None:
        """Resolve an identity from this strategy's credential.

        Args:
            ctx: The request being authenticated.

        Returns:
            The identity, or ``None`` when the credential is absent or invalid.
        """
