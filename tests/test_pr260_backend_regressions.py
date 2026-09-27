"""Regressions for credential-scoped sessions and catalog cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from rootcoz import ai_client, main, storage
from rootcoz.engine.chat import safe_exception_frames


@pytest.mark.asyncio
async def test_creation_does_not_lock_writers_and_rotation_revokes_new_session(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key-value")
    started, resume = asyncio.Event(), asyncio.Event()
    delete = AsyncMock()

    async def create():
        started.set()
        await resume.wait()
        return "new-sid"

    task = asyncio.create_task(
        storage.create_ai_session_with_source(create, "alice", "openai", "user", delete)
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(
            storage.update_user_ai_credential("alice", "openai", "new-key-value"), 2
        )
    finally:
        resume.set()
    with pytest.raises(ValueError, match="credential changed"):
        await task
    delete.assert_awaited_once_with("new-sid")
    assert await storage.list_revoked_ai_sessions() == []


@pytest.mark.asyncio
async def test_persistence_failure_deletes_new_sidecar_but_not_active_collision(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "collision.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "server")
    delete = AsyncMock()
    with pytest.raises(ValueError, match="already active"):
        await storage.create_ai_session_with_source(
            AsyncMock(return_value="sid"), "bob", "openai", "server", delete
        )
    delete.assert_not_awaited()  # An ambiguous reused ID belongs to the old owner.
    assert await storage.get_ai_session_source("sid", "alice", "openai") == "server"


@pytest.mark.asyncio
async def test_insert_error_rolls_back_and_deletes_exact_new_sidecar(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "insert.db")
    await storage.init_db()
    delete = AsyncMock()
    async with storage._connect_db() as db:
        await db.execute(
            "CREATE TRIGGER reject_session BEFORE INSERT ON ai_session_sources "
            "BEGIN SELECT RAISE(ABORT, 'db failure'); END"
        )
        await db.commit()
    with pytest.raises(storage.aiosqlite.IntegrityError):
        await storage.create_ai_session_with_source(
            AsyncMock(return_value="sid"), "bob", "openai", "server", delete
        )
    delete.assert_awaited_once_with("sid")
    assert await storage.get_ai_session_source("sid", "bob", "openai") == "unknown"


@pytest.mark.asyncio
async def test_user_session_generation_and_chat_deletion_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "resume.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "valid-key-value")
    await storage.create_ai_session_with_source(
        AsyncMock(return_value="sid"), "alice", "openai", "user"
    )
    assert await storage.get_ai_session_source("sid", "alice", "openai") == "user"
    await storage.add_chat_message(
        "job",
        "assistant",
        "reply",
        username="alice",
        ai_provider="openai",
        session_id="sid",
    )
    await storage.delete_chat_messages("job", "alice")
    assert await storage.list_revoked_ai_sessions() == ["sid"]
    failing = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=failing),
    )
    await main._cleanup_revoked_ai_sessions()
    assert await storage.list_revoked_ai_sessions() == ["sid"]
    delete = AsyncMock()
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=delete),
    )
    await main._cleanup_revoked_ai_sessions()
    delete.assert_awaited_once_with("sid")
    assert await storage.list_revoked_ai_sessions() == []


@pytest.mark.asyncio
async def test_stale_provider_status_and_delete_without_discovery(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "stale.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential(
        "alice", "removed-provider", "valid-key-value"
    )
    monkeypatch.setattr(main, "supported_key_providers", AsyncMock(return_value=[]))
    request = SimpleNamespace(state=SimpleNamespace(username="alice", role="admin"))
    status = await main.get_user_ai_credentials(request)
    assert b"removed-provider" in status.body
    assert (
        await main.delete_user_ai_credential("removed-provider", request)
    ).status_code == 200
    assert await storage.get_user_ai_credentials("alice") == {}


@pytest.mark.asyncio
async def test_record_token_usage_does_not_resummarize(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "usage.db")
    await storage.init_db()
    monkeypatch.setattr(
        storage,
        "_token_usage_snapshot",
        AsyncMock(side_effect=AssertionError("quadratic")),
    )
    await storage.record_token_usage("job", "openai", "model", "analysis")
    assert len(await storage.get_token_usage_for_job("job")) == 1


@pytest.mark.asyncio
async def test_catalog_force_server_override_and_bad_key_isolation(monkeypatch):
    monkeypatch.setattr(main, "_require_authenticated", lambda request: None)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(force_server_credentials=False),
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "server", "id": "model"}]),
    )
    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(return_value={"bad": "invalid-value"}),
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["bad"])
    )
    discover = AsyncMock(side_effect=ValueError("bad credential"))
    monkeypatch.setattr(ai_client, "models_for_api_key", discover)
    request = SimpleNamespace(state=SimpleNamespace(username="alice", is_admin=False))
    result = await main.list_ai_models(
        request, provider="", force_server_credentials=False
    )
    assert result["providers"]["server"][0]["id"] == "model"
    assert result["provider_status"]["bad"]["unavailable"]
    discover.assert_awaited_once()
    await main.list_ai_models(request, provider="", force_server_credentials=True)
    discover.assert_awaited_once()  # Forced server discovery never touches the bad key.


@pytest.mark.asyncio
async def test_short_credential_rejected_at_api(monkeypatch):
    monkeypatch.setattr(
        main, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(main, "_credential_user", AsyncMock(return_value="alice"))
    save = AsyncMock()
    monkeypatch.setattr(storage, "update_user_ai_credential", save)
    request = SimpleNamespace(state=SimpleNamespace(username="alice", role="admin"))
    with pytest.raises(HTTPException, match="8-1024"):
        await main.set_user_ai_credential(
            "openai", main.AiCredentialInput(api_key="a"), request
        )
    save.assert_not_awaited()


def test_traceback_frames_never_include_secret():
    try:
        raise ValueError("credential-secret")
    except ValueError as exc:
        frames = safe_exception_frames(exc)
    assert "test_traceback_frames_never_include_secret" in frames
    assert "credential-secret" not in frames


def test_key_redaction_rejects_short_inputs_without_corrupting_valid_output():
    assert ai_client._redact_key_echo("ordinary analysis", "a") == "[REDACTED]"
    assert ai_client._redact_key_echo(
        "ordinary analysis valid-key-value", "valid-key-value"
    ) == ("ordinary analysis [REDACTED]")
