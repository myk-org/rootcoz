"""Credential verification for ``POST /api/auth/login``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rootcoz import storage
from rootcoz.auth.base import (
    RESERVED_ADMIN_USERNAME,
    effective_can_view_reports,
    matches_bootstrap_admin,
    role_and_admin,
)
from rootcoz.config import Settings


@dataclass
class PasswordResult:
    """Outcome of verifying a login credential pair.

    Attributes:
        authenticated: Whether the username/key pair was valid.
        is_admin: Whether the account is the bootstrap admin or an admin user.
        role: Resolved role.
        can_view_reports: Effective reports access.
        can_use_server_providers: Stored server-provider grant.
        can_use_free_providers: Effective free-provider access.
        account: The users row the key resolved to, if any.
    """

    authenticated: bool
    is_admin: bool
    role: str
    can_view_reports: bool
    can_use_server_providers: bool
    can_use_free_providers: bool
    account: dict[str, Any] | None


class PasswordLogin:
    """Verify the credentials posted to ``POST /api/auth/login``.

    Not a chain strategy and not an ``AuthStrategy``: it has no
    ``is_applicable``/``authenticate`` because the login route owns the request
    flow (audit rows, lockout, approval gate, session creation and cookies) and
    only asks this class whether the pair is valid.
    """

    async def verify(
        self, username: str, api_key: str, settings: Settings
    ) -> PasswordResult:
        """Verify a username + API key pair.

        Args:
            username: Username from the login body, already stripped and lowered.
            api_key: API key from the login body.
            settings: Settings for this request (for ``ADMIN_KEY``).

        Returns:
            A :class:`PasswordResult`; ``authenticated`` is False when the key
            matches no account or belongs to another user.
        """
        if username == RESERVED_ADMIN_USERNAME and matches_bootstrap_admin(
            api_key, settings
        ):
            return PasswordResult(
                authenticated=True,
                is_admin=True,
                role="admin",
                can_view_reports=effective_can_view_reports(True),
                can_use_server_providers=True,
                can_use_free_providers=True,
                account=None,
            )
        user = await storage.get_user_by_key(api_key)
        if user and user["username"] == username:
            role, is_admin = role_and_admin(user)
            return PasswordResult(
                authenticated=True,
                is_admin=is_admin,
                role=role,
                can_view_reports=effective_can_view_reports(
                    is_admin, bool(user.get("can_view_reports"))
                ),
                can_use_server_providers=bool(user.get("can_use_server_providers")),
                # Effective free access — same rule as /api/auth/me (toggle,
                # role admin, server grant); get_user_by_key has no free column.
                can_use_free_providers=await storage.can_user_use_free_providers(
                    username
                ),
                account=user,
            )
        return PasswordResult(
            authenticated=False,
            is_admin=False,
            role="reviewer",
            can_view_reports=False,
            can_use_server_providers=False,
            can_use_free_providers=False,
            account=user,
        )
