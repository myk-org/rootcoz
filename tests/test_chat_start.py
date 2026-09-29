"""Explicit chat Start selects a user-owned credential source, not job defaults."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from rootcoz import ai_client, main, storage
from rootcoz.models import ChatInitRequest, ChatMessageRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_start_requires_pair_and_force_and_send_requires_start(
    admin, monkeypatch, tmp_path
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    if not admin:
        await storage.save_result(
            "job",
            "",
            "completed",
            {
                "status": "completed",
                "result": {},
                "request_params": {
                    "ai_provider": "openai",
                    "ai_model": "old",
                    "force_server_credentials": True,
                },
            },
        )
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ChatInitRequest.model_validate({})
    with pytest.raises(HTTPException) as exc:
        if admin:
            await main.send_admin_chat_message(
                ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
        else:
            await main.send_chat_message(
                "job", ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
    assert exc.value.status_code == 409
    assert (
        await storage.count_chat_messages(
            main.ADMIN_CHAT_JOB_ID if admin else "job", username="alice"
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_start_and_send_use_selected_source_not_job_or_settings(
    admin, monkeypatch, tmp_path
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "status": "completed",
                "ai_provider": "other",
                "ai_model": "old",
                "request_params": {"force_server_credentials": True},
            },
        )
    from rootcoz.config import Settings

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(
            ai_provider="other",
            ai_model="old",
            force_server_credentials=True,
            ai_call_timeout=30,
        ),
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: tmp_path)
    monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main, "_resolve_chat_credentials", AsyncMock(return_value=("", "", "", "", ""))
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def validate(p, m):
        ai_client._selected_credential_source.set("user")
        return p, m

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)

    async def create(**kwargs):
        assert (kwargs["ai_provider"], kwargs["ai_model"]) == ("openai", "chosen")
        assert ai_client.force_server_credentials.get() is False
        await storage.save_ai_session_source("sid", "alice", "openai", "user")
        return "sid"

    monkeypatch.setattr(
        chat, "init_admin_chat_session" if admin else "init_chat_session", create
    )
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    body = {
        "ai_provider": "openai",
        "ai_model": "chosen",
        "force_server_credentials": False,
    }
    result = await (
        main.init_admin_chat(request, ChatInitRequest(**body))
        if admin
        else main.init_chat(job_id, request, ChatInitRequest(**body))
    )
    assert result["session_id"] == "sid"
    tasks = BackgroundTasks()
    sent = await (
        main.send_admin_chat_message(ChatMessageRequest(message="hi"), request, tasks)
        if admin
        else main.send_chat_message(
            job_id, ChatMessageRequest(message="hi"), request, tasks
        )
    )
    assert sent["assistant_message_id"]
    assert tasks.tasks[0].kwargs.get("force_server") is False
    for override in (
        {"ai_provider": "openai", "ai_model": "different"},
        {"force_server_credentials": True},
    ):
        with pytest.raises(HTTPException) as exc:
            if admin:
                await main.send_admin_chat_message(
                    ChatMessageRequest(message="hi", **override),
                    request,
                    BackgroundTasks(),
                )
            else:
                await main.send_chat_message(
                    job_id,
                    ChatMessageRequest(message="hi", **override),
                    request,
                    BackgroundTasks(),
                )
        assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_send_after_user_key_removed_requires_new_start(
    monkeypatch, tmp_path, admin
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "send-switch.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id, "", "completed", {"status": "completed", "result": {}}
        )
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="m",
        session_id="sid",
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "m"}]),
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    # Simulate a lost key without clearing the session marker through the rotation endpoint.
    from rootcoz.storage import _connect_db, encrypt_value

    async with _connect_db() as db:
        await db.execute(
            "UPDATE users SET ai_credentials_enc = ? WHERE username = ?",
            (encrypt_value("{}"), "alice"),
        )
        await db.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    with pytest.raises(HTTPException) as exc:
        if admin:
            await main.send_admin_chat_message(
                ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
        else:
            await main.send_chat_message(
                job_id, ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
    assert exc.value.status_code == 422
    assert "Start" in str(exc.value.detail)
    assert await storage.count_chat_messages(job_id, username="alice") == 1


@pytest.mark.asyncio
async def test_server_grant_revoked_cannot_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    await storage.save_ai_session_source("sid", "alice", "openai", "server")
    await storage.add_chat_message(
        "job",
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="m",
        session_id="sid",
    )
    from rootcoz.ai_client import require_server_provider_grant

    token = ai_client.ai_username.set("alice")
    try:
        assert (await main._current_chat_choice("job", "alice"))[1] is True
        await storage.set_user_can_use_server_providers("alice", False)
        with pytest.raises(ValueError, match="grant"):
            await require_server_provider_grant()
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_revoked_key_cannot_resume_or_fall_back_to_server(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key")
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        "job",
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="m",
        session_id="sid",
    )
    await storage.update_user_ai_credential("alice", "openai", "new-key")
    with pytest.raises(HTTPException) as exc:
        await main._current_chat_choice("job", "alice")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.asyncio
async def test_start_server_only_model_requires_explicit_server_choice(
    monkeypatch, tmp_path, admin
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "choice.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    if not admin:
        await storage.save_result(
            "job", "", "completed", {"status": "completed", "result": {}}
        )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "m"}]),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    body = ChatInitRequest(
        ai_provider="openai", ai_model="m", force_server_credentials=False
    )
    with pytest.raises(HTTPException) as exc:
        if admin:
            await main.init_admin_chat(request, body)
        else:
            await main.init_chat("job", request, body)
    assert exc.value.status_code == 422
    assert "server" in str(exc.value.detail).lower()
    assert (
        await storage.count_chat_messages(
            main.ADMIN_CHAT_JOB_ID if admin else "job", username="alice"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_start_rejects_server_without_grant_even_when_job_forced(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_result(
        "job",
        "",
        "completed",
        {"status": "completed", "request_params": {"force_server_credentials": True}},
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "m"}]),
    )
    await storage.create_user("bob", role="reviewer", can_use_server_providers=False)
    request = SimpleNamespace(
        state=SimpleNamespace(username="bob", is_admin=False, role="reviewer")
    )
    with pytest.raises(HTTPException) as exc:
        await main.init_chat(
            "job",
            request,
            ChatInitRequest(
                ai_provider="openai", ai_model="m", force_server_credentials=True
            ),
        )
    assert exc.value.status_code == 422
    assert await storage.count_chat_messages("job", username="bob") == 0
