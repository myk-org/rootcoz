"""In-flight chat results cannot restore a session after credential rotation."""

from unittest.mock import AsyncMock

import pytest

from rootcoz import main, storage


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["job", "__admin_chat__"])
@pytest.mark.parametrize("replacement", ["new-key", None])
async def test_in_flight_chat_cannot_restore_revoked_session(
    tmp_path, monkeypatch, job_id, replacement
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    generation = await storage.get_user_ai_credential_generation("alice")
    await storage.add_chat_message(
        job_id=job_id,
        role="assistant",
        content="old",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="old-session",
    )
    _, pending = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="p", ai_model="m"
    )
    assert await storage.update_user_ai_credential("alice", "p", replacement) == [
        "old-session"
    ]
    await storage.update_chat_message_status(pending, "completed")
    assert not await storage.update_chat_message_ai_fields(
        pending,
        ai_provider="p",
        ai_model="m",
        session_id="old-session",
        credential_generation=generation,
    )
    assert all(
        not msg["session_id"]
        for msg in await storage.get_chat_messages(job_id, username="alice")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_chat_call_rotated_mid_flight_cannot_publish_session(
    tmp_path, monkeypatch, admin
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id,
            "",
            "completed",
            {"status": "completed", "summary": "test", "failures": []},
        )
        monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
        monkeypatch.setattr(
            chat, "install_http_tools_mcp_best_effort_async", AsyncMock()
        )
        monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(
            main,
            "_resolve_chat_credentials",
            AsyncMock(return_value=("", "", "", "", "")),
        )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def rotate_during_call(**kwargs):
        await storage.update_user_ai_credential("alice", "p", None)
        return True, "reply", "stale-session"

    monkeypatch.setattr(
        chat, "admin_chat_with_ai" if admin else "chat_with_ai", rotate_during_call
    )
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="p", ai_model="m"
    )
    process = main._process_admin_chat_message if admin else main._process_chat_message
    await process(
        **({} if admin else {"job_id": job_id}),
        user_msg_id=user_id,
        assistant_msg_id=assistant_id,
        message="hello",
        ai_provider_override="p",
        ai_model_override="m",
        username="alice",
    )
    message = (await storage.get_chat_messages(job_id, username="alice"))[-1]
    assert message["status"] == "completed"
    assert message["session_id"] == ""
