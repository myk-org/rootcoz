"""Unsaved chat sessions must be quarantined before sidecar cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rootcoz import main, storage


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["job", main.ADMIN_CHAT_JOB_ID])
async def test_delete_during_session_creation_quarantines_unsaved_reply(
    tmp_path, monkeypatch, job_id
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    _, pending = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="p"
    )
    created, release = asyncio.Event(), asyncio.Event()
    delete = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=delete),
    )

    async def create():
        await storage.save_ai_session_source("sid", "alice", "p", "server")
        created.set()
        await release.wait()
        await main._discard_unsaved_chat_response(
            pending, job_id, "alice", "p", "sid", "Chat"
        )

    task = asyncio.create_task(create())
    try:
        await asyncio.wait_for(created.wait(), 5)
        await storage.delete_chat_messages(job_id, "alice")
        assert await storage.get_ai_session_source("sid", "alice", "p") == "server"
    finally:
        release.set()
        await task
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "alice", "p")
    assert await storage.list_revoked_ai_sessions() == ["sid"]
    delete.side_effect = None
    await main._cleanup_revoked_ai_sessions()
    delete.assert_awaited_with("sid")
    assert await storage.list_revoked_ai_sessions() == []
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "alice", "p", "server")


@pytest.mark.asyncio
async def test_unsaved_init_quarantines_only_own_session(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "init.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("sid", "alice", "p", "server")
    await storage.save_ai_session_source("other", "bob", "p", "server")
    delete = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=delete),
    )
    await main._discard_unsaved_ai_session("sid", "alice", "p")
    await main._discard_unsaved_ai_session("other", "alice", "p")
    await storage.save_ai_session_source("wrong-provider", "alice", "q", "server")
    await main._discard_unsaved_ai_session("wrong-provider", "alice", "p")
    assert await storage.list_revoked_ai_sessions() == ["sid"]
    assert (
        await storage.get_ai_session_source("wrong-provider", "alice", "q") == "server"
    )
    assert await storage.get_ai_session_source("other", "bob", "p") == "server"
    delete.assert_awaited_once_with("sid")
