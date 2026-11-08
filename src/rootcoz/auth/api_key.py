"""Bearer-token authentication: the admin key, a user API key, or a session."""

from __future__ import annotations

from rootcoz import storage
from rootcoz.auth.base import (
    RESERVED_ADMIN_USERNAME,
    AuthContext,
    AuthIdentity,
    AuthStrategy,
    identity_from_session,
    matches_bootstrap_admin,
    role_and_admin,
)


class ApiKeyStrategy(AuthStrategy):
    """Authenticate an ``Authorization: Bearer`` credential.

    Three credentials answer to the same header, in this order: the bootstrap
    ``ADMIN_KEY``, a per-user API key, then a session token — the last one
    exists because AI internal calls pass the caller's session token as a
    Bearer credential.
    """

    name = "api_key"

    def is_applicable(self, ctx: AuthContext) -> bool:
        """Whether the request carries a Bearer credential.

        Args:
            ctx: The request being authenticated.

        Returns:
            True when the request carries a Bearer header.
        """
        return ctx.request.headers.get("authorization", "").startswith("Bearer ")

    async def authenticate(self, ctx: AuthContext) -> AuthIdentity | None:
        """Resolve the Bearer credential to an identity.

        Args:
            ctx: The request being authenticated.

        Returns:
            The identity, or ``None`` when no credential matched — the chain
            then keeps the identity it already has.
        """
        auth_header = ctx.request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:]
        if matches_bootstrap_admin(token, ctx.settings):
            return AuthIdentity(
                username=RESERVED_ADMIN_USERNAME,
                strategy_name=self.name,
                is_admin=True,
                role="admin",
            )
        user = await storage.get_user_by_key(token)
        if user:
            role, is_admin = role_and_admin(user)
            return AuthIdentity(
                username=str(user["username"]),
                strategy_name=self.name,
                is_admin=is_admin,
                role=role,
                user=user,
            )
        # Fall back to a session token passed as Bearer (used by AI for
        # internal API calls).
        session = await storage.get_session(token)
        if session:
            return identity_from_session(session, self.name)
        return None
