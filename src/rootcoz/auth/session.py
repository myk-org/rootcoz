"""Session-cookie authentication with a sliding-window renewal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from rootcoz import get_child_logger, storage
from rootcoz.auth.base import (
    RENEW_SESSION_TOKEN_ATTR,
    SESSION_COOKIE,
    AuthContext,
    AuthIdentity,
    AuthStrategy,
    identity_from_session,
    read_cookie,
)

logger = get_child_logger(__name__)


class SessionStrategy(AuthStrategy):
    """Authenticate the ``rootcoz_session`` cookie.

    Whether a Bearer credential may take over a session established here is
    decided by :func:`rootcoz.auth.registry.may_replace`, not by this class.
    """

    name = "session"

    def is_applicable(self, ctx: AuthContext) -> bool:
        """Whether the request carries a session cookie (no DB access).

        Args:
            ctx: The request being authenticated.

        Returns:
            True when a session cookie is present.
        """
        return bool(read_cookie(ctx.request, SESSION_COOKIE))

    async def authenticate(self, ctx: AuthContext) -> AuthIdentity | None:
        """Load the session and renew it when less than half its TTL remains.

        Args:
            ctx: The request being authenticated.

        Returns:
            The session identity, or ``None`` when the cookie is absent or the
            session is unknown/expired.
        """
        token = read_cookie(ctx.request, SESSION_COOKIE)
        if not token:
            return None
        session = await storage.get_session(token)
        if not session:
            return None
        identity = identity_from_session(session, self.name)
        renewed = await self._renew(session, token)
        if renewed:
            # The DB write already happened; the middleware re-issues the cookie.
            setattr(ctx.request.state, RENEW_SESSION_TOKEN_ATTR, renewed)
        return identity

    async def _renew(self, session: dict[str, Any], token: str) -> str | None:
        """Renew the session once less than half its TTL remains.

        Args:
            session: The stored session row.
            token: Raw session token from the cookie.

        Returns:
            The token to re-issue as a cookie, or ``None`` when nothing was
            renewed (still fresh, unparsable expiry, or renewal failed).
        """
        expires_at_str = session.get("expires_at", "")
        if not expires_at_str:
            return None
        try:
            expires_at = datetime.strptime(
                str(expires_at_str), storage.SQL_TIMESTAMP_FORMAT
            ).replace(tzinfo=UTC)
        except ValueError, TypeError:
            logger.debug(
                "Failed to parse session expires_at for renewal", exc_info=True
            )
            return None
        half_ttl = timedelta(hours=storage.current_session_ttl_hours() / 2)
        if expires_at - datetime.now(UTC) >= half_ttl:
            return None
        try:
            # Await renewal so the refreshed cookie is only set after the DB
            # update is confirmed.
            return token if await storage.renew_session(token) else None
        except Exception:
            logger.debug("Session renewal failed", exc_info=True)
            return None
