"""Rotation and sidecar revocation races must not expose stale sessions or block writers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from rootcoz import ai_client, main, storage


@pytest.mark.asyncio
async def test_key_rotated_before_session_persistence_is_rejected(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "rotation.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key-value")
    entered, resume = asyncio.Event(), asyncio.Event()
    deleted = AsyncMock()

    async def sidecar(request):
        if request.method == "POST":
            assert request.content and b"old-key-value" in request.content
            entered.set()
            await resume.wait()
            return httpx.Response(200, json={"session_id": "sid"})
        return httpx.Response(200)

    client = ai_client.SidecarClient.__new__(ai_client.SidecarClient)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(sidecar), base_url="http://sidecar"
    )
    client.delete_session = deleted
    token = ai_client.ai_username.set("alice")
    try:
        key = await ai_client.session_key("openai")
        await storage.update_user_ai_credential("alice", "openai", "new-key-value")
        task = asyncio.create_task(
            ai_client.create_session_safely(
                client, provider="openai", model="m", system_prompt="test", api_key=key
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        resume.set()
        with pytest.raises(ValueError, match="credential changed"):
            await task
        deleted.assert_awaited_once_with("sid")
        assert (
            await storage.get_ai_session_source("sid", "alice", "openai") == "unknown"
        )
    finally:
        resume.set()
        ai_client.ai_username.reset(token)
        await client._client.aclose()


@pytest.mark.asyncio
async def test_slow_revocation_does_not_lock_unrelated_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "revoke.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    entered, resume = asyncio.Event(), asyncio.Event()

    async def delete(_sid):
        entered.set()
        await resume.wait()

    task = asyncio.create_task(storage.delete_revoked_ai_session("sid", delete))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(
            storage.update_user_ai_credential("alice", "openai", "new-key-value"), 1
        )
        with pytest.raises(ValueError, match="revoked"):
            await storage.get_ai_session_source("sid", "alice", "openai")
    finally:
        resume.set()
        await task
    assert await storage.list_revoked_ai_sessions() == []


@pytest.mark.asyncio
async def test_delete_recreate_user_revokes_same_generation_session(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "account.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key-value")
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        "job",
        "assistant",
        "reply",
        username="alice",
        ai_provider="openai",
        session_id="sid",
    )
    delete = AsyncMock(side_effect=OSError("sidecar offline"))
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=delete),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(username="admin", role="admin", is_admin=True)
    )
    assert (await main.delete_user_endpoint(request, "alice"))["deleted"] == "alice"
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "different-key-value")
    assert await storage.get_user_ai_credential_generation("alice", "openai") == 1
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "alice", "openai")
    assert await storage.list_revoked_ai_sessions() == ["sid"]
    assert all(
        not m["session_id"]
        for m in await storage.get_chat_messages("job", username="alice")
    )
    delete.assert_awaited_once_with("sid")
    delete.side_effect = None
    await main._cleanup_revoked_ai_sessions()
    assert await storage.list_revoked_ai_sessions() == []
    assert delete.await_count == 2


@pytest.mark.asyncio
async def test_crashed_delete_claim_retries_without_deleting_replacement(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "crash.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    # A process died after committing the claim but before contacting the sidecar.
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE ai_session_sources SET credential_source = 'deleting' WHERE session_id = 'sid'"
        )
        await db.commit()
    delete = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=delete),
    )
    await main._cleanup_revoked_ai_sessions()
    assert await storage.list_revoked_ai_sessions() == ["sid"]
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "alice", "openai")
    delete.side_effect = None
    await main._cleanup_revoked_ai_sessions()
    delete.assert_awaited_with("sid")
    assert delete.await_count == 2
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
    await main._cleanup_revoked_ai_sessions()
    assert delete.await_count == 2
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "bob", "openai")


@pytest.mark.asyncio
async def test_crashed_claim_does_not_delete_reused_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "reused.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE ai_session_sources SET credential_source = 'deleting' WHERE session_id = 'sid'"
        )
        await db.commit()
    delete = AsyncMock()
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=delete),
    )
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
    await main._cleanup_revoked_ai_sessions()
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
    await main._cleanup_revoked_ai_sessions()
    delete.assert_awaited_once_with("sid")
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "bob", "openai")


@pytest.mark.asyncio
async def test_delete_during_session_creation_rejects_recreated_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "create-delete.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key-value")
    entered, resume = asyncio.Event(), asyncio.Event()
    delete = AsyncMock()

    async def create():
        entered.set()
        await resume.wait()
        return "sid"

    task = asyncio.create_task(
        storage.create_ai_session_with_source(create, "alice", "openai", "user", delete)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await storage.delete_user("alice")
        await storage.create_admin_user("alice")
        await storage.update_user_ai_credential("alice", "openai", "new-key-value")
    finally:
        resume.set()
    with pytest.raises(ValueError, match="credential changed"):
        await task
    delete.assert_awaited_once_with("sid")
    assert await storage.get_ai_session_source("sid", "alice", "openai") == "unknown"


@pytest.mark.asyncio
async def test_independent_workers_cannot_delete_same_sidecar_or_reuse_id(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "workers.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_delete(sid):
        calls.append(sid)
        entered.set()
        await resume.wait()

    first = asyncio.create_task(
        storage._delete_revoked_ai_session_claimed("sid", slow_delete)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not await asyncio.wait_for(
            storage._delete_revoked_ai_session_claimed("sid", slow_delete), 1
        )
        with pytest.raises(ValueError, match="already active"):
            await storage.save_ai_session_source("sid", "bob", "openai", "server")
    finally:
        resume.set()
        await first
    assert calls == ["sid"]
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
    assert not await storage._delete_revoked_ai_session_claimed("sid", slow_delete)
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "bob", "openai")


@pytest.mark.asyncio
async def test_expired_crashed_claim_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "expired.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE ai_session_sources SET credential_source = 'deleting', "
            "claim_token = 'dead-worker', claim_expires_at = unixepoch() - 1 "
            "WHERE session_id = 'sid'"
        )
        await db.commit()
    delete = AsyncMock()
    assert await storage._delete_revoked_ai_session_claimed("sid", delete)
    delete.assert_awaited_once_with("sid")
    assert await storage.list_revoked_ai_sessions() == []


@pytest.mark.asyncio
async def test_cancelled_deletion_releases_claim_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "cancel.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    entered = asyncio.Event()

    async def blocked_delete(_sid):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(storage.delete_revoked_ai_session("sid", blocked_delete))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    delete = AsyncMock()
    assert await storage.delete_revoked_ai_session("sid", delete)
    delete.assert_awaited_once_with("sid")
    assert await storage.list_revoked_ai_sessions() == []
