"""Atomic user-lifecycle deletion and revocation orchestration.

Encapsulates the multi-step ordering invariant for deleting a user so the
HTTP endpoint in ``main.py`` does not manually coordinate locks, delete
markers, DB rows, ``.by-id`` attachments, admin artifacts, pending counts, and
revoked AI sessions:

1. Acquire owner attachment scope lock (``owner:<id>``)
2. Capture pre-delete audit snapshot
3. Write owner delete marker before the DB transaction
4. Delete DB rows atomically (``storage.delete_user``, which tombstones AI
   sessions, drops ``user_identities`` & ``sso_device_codes``, clears sessions,
   and bumps ``auth_revocation_version``)
5. On error: remove delete marker if the user row still exists
6. On delete (or concurrent-delete race where the row disappeared): purge
   owner attachments and admin artifacts off-loop
7. Notify pending-count listeners and clean up revoked AI sessions
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from rootcoz import storage


@dataclass(frozen=True)
class UserDeleteHooks:
    """Injected filesystem/runtime callbacks so ``auth`` never imports ``main``."""

    hold_scope_lock: Callable[[str], AbstractAsyncContextManager[Any]]
    write_owner_delete_marker: Callable[[int], Awaitable[bool]]
    remove_owner_delete_marker: Callable[[int], Awaitable[Any]]
    cleanup_owner_attachments: Callable[[int], Awaitable[Any]]
    cleanup_admin_artifacts: Callable[[str, int], Awaitable[Any]]
    notify_pending_count_changed: Callable[[], Any]
    cleanup_revoked_ai_sessions: Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class DeletedUserResult:
    """Outcome of an atomic user deletion, including its pre-delete audit snapshot."""

    username: str
    owner_id: int
    audit_snapshot: dict[str, Any] | None


async def delete_user_atomic(
    username: str,
    *,
    actor_username: str,
    hooks: UserDeleteHooks,
) -> DeletedUserResult:
    """Atomically delete *username* and purge all user-scoped state.

    Raises:
        HTTPException: 400 if *username* equals *actor_username* or storage
            rejects the deletion; 404 if the user does not exist.
    """
    if username == actor_username:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    user_row = await storage.get_user_by_username(username)
    if user_row is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    owner_id = int(user_row["id"])
    stored_username = str(user_row["username"])

    async with hooks.hold_scope_lock(f"owner:{owner_id}"):
        snapshot = await storage.get_user_audit_snapshot(username)
        marker_written = await hooks.write_owner_delete_marker(owner_id)
        try:
            deleted = await storage.delete_user(username)
        except ValueError as exc:
            if marker_written and await storage.user_id_exists(owner_id):
                await hooks.remove_owner_delete_marker(owner_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BaseException:
            if marker_written and await storage.user_id_exists(owner_id):
                await hooks.remove_owner_delete_marker(owner_id)
            raise

        if not deleted:
            if not await storage.user_id_exists(owner_id):
                await hooks.cleanup_owner_attachments(owner_id)
                await hooks.cleanup_admin_artifacts(stored_username, owner_id)
            elif marker_written:
                await hooks.remove_owner_delete_marker(owner_id)
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")

        await hooks.cleanup_owner_attachments(owner_id)
        await hooks.cleanup_admin_artifacts(stored_username, owner_id)

    hooks.notify_pending_count_changed()
    await hooks.cleanup_revoked_ai_sessions()
    return DeletedUserResult(
        username=stored_username,
        owner_id=owner_id,
        audit_snapshot=snapshot,
    )
