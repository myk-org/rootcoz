"""A DB lease cannot fence an ID-only sidecar delete after the lease expires."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from rootcoz import storage


@pytest.mark.asyncio
async def test_expired_claim_finishes_before_delayed_delete_without_id_reuse(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "auditor.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    entered, release = asyncio.Event(), asyncio.Event()
    deletes = []

    async def delayed_delete(sid):
        entered.set()
        await release.wait()
        deletes.append(sid)

    first = asyncio.create_task(
        storage._delete_revoked_ai_session_claimed("sid", delayed_delete)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Worker A's request is still in flight when worker B takes the expired lease.
        async with storage._connect_db() as db:
            await db.execute(
                "UPDATE ai_session_sources SET claim_expires_at = unixepoch() - 1 "
                "WHERE session_id = 'sid'"
            )
            await db.commit()
        assert await storage._delete_revoked_ai_session_claimed("sid", AsyncMock())
        assert await storage.list_revoked_ai_sessions() == []
        with pytest.raises(ValueError, match="already active"):
            await storage.save_ai_session_source("sid", "bob", "openai", "server")
        rejected_delete = AsyncMock()
        with pytest.raises(ValueError, match="already active"):
            await storage.create_ai_session_with_source(
                AsyncMock(return_value="sid"),
                "bob",
                "openai",
                "server",
                rejected_delete,
            )
        rejected_delete.assert_not_awaited()
    finally:
        release.set()
        await first
    assert deletes == ["sid"]
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "alice", "openai")
    # A fresh connection after startup must still see the quarantine.
    await storage.init_db()
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
    assert await storage.list_revoked_ai_sessions() == []
    async with storage._connect_db() as db:
        row = await (
            await db.execute(
                "SELECT username, provider, credential_generation, claim_token "
                "FROM ai_session_sources WHERE session_id = 'sid'"
            )
        ).fetchone()
    assert tuple(row) == ("", "", None, None)


@pytest.mark.asyncio
async def test_cancelled_unknown_delete_retries_but_id_stays_reserved(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "uncertain.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    entered = asyncio.Event()

    async def uncertain(_sid):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(storage.delete_revoked_ai_session("sid", uncertain))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await storage.list_revoked_ai_sessions() == ["sid"]
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
    assert await storage.delete_revoked_ai_session("sid", AsyncMock())
    assert await storage.list_revoked_ai_sessions() == []
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
