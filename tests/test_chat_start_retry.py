"""A lost Chat Start response can be retried without creating another session."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from rootcoz import ai_client, main, storage
from rootcoz.cli.config import ServerConfig
from rootcoz.cli.main import app
from rootcoz.models import ChatInitRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("source", ["user", "server"])
async def test_repeated_start_reuses_session_without_duplicate_welcome(
    admin, source, monkeypatch, tmp_path
):
    from rootcoz.engine import chat, graft_http
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id, "", "completed", {"status": "completed", "result": {}}
        )
        monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
        monkeypatch.setattr(graft_http, "cloned_graph_roots", lambda workspace: {})
        monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def validate(body, username):
        ai_client._selected_credential_source.set(source)
        return body.ai_provider, body.ai_model

    monkeypatch.setattr(main, "_validate_chat_selection", validate)

    async def create(**kwargs):
        await storage.save_ai_session_source("session-123", "alice", "openai", source)
        return "session-123"

    new_session = AsyncMock(side_effect=create)
    monkeypatch.setattr(
        chat, "init_admin_chat_session" if admin else "init_chat_session", new_session
    )
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    body = ChatInitRequest(
        ai_provider="openai", ai_model="m", force_server_credentials=source == "server"
    )

    async def start():
        if admin:
            return await main.init_admin_chat(request, body)
        return await main.init_chat(job_id, request, body)

    alice_event, bob_event, broadcast_event = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    monkeypatch.setitem(main._chat_listeners, f"{job_id}:alice", {alice_event})
    monkeypatch.setitem(main._chat_listeners, f"{job_id}:bob", {bob_event})
    monkeypatch.setitem(main._chat_listeners, job_id, {broadcast_event})
    with patch.object(
        main, "notify_chat_changed", wraps=main.notify_chat_changed
    ) as notify:
        first = await start()
        assert first["ready"] is True
        assert first["session_id"] == "session-123"
        assert alice_event.is_set()
        assert not bob_event.is_set()
        assert not broadcast_event.is_set()
        notify.assert_called_once_with(job_id, username="alice")
        assert await storage.get_latest_chat_session(job_id, "alice") is not None

        alice_event.clear()
        retry = await start()
        assert alice_event.is_set()
        assert not bob_event.is_set()
        assert not broadcast_event.is_set()
        assert notify.call_count == 2
    assert retry["ready"] is True
    assert retry["session_id"] == first["session_id"]
    new_session.assert_awaited_once()
    messages = await storage.get_chat_messages(job_id, username="alice")
    assert len(messages) == (1 if admin else 2)
    assert sum(bool(message["content"]) for message in messages) == (0 if admin else 1)


@pytest.mark.parametrize("admin", [False, True])
def test_cli_reports_reused_session_as_ready(admin, monkeypatch):
    monkeypatch.setenv("ROOTCOZ_SERVER", "http://test-server:8000")
    label = "Admin chat" if admin else "Chat"
    args = ["admin-chat", "init"] if admin else ["chat", "init", "job"]
    args.extend(["--provider", "openai", "--model", "m"])
    with (
        patch(
            "rootcoz.cli.main.get_server_config",
            return_value=ServerConfig(url="http://test-server:8000"),
        ),
        patch("rootcoz.cli.main._get_client") as get_client,
    ):
        client = MagicMock()
        get_client.return_value = client
        getattr(client, "init_admin_chat" if admin else "init_chat").return_value = {
            "ready": True,
            "session_id": "session-123",
        }
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0
        assert f"{label} started" in result.output
        assert "Ready: True" in result.output
        assert "session-123" not in result.output
        json_result = CliRunner().invoke(app, ["--json", *args])
        assert json_result.exit_code == 0
        assert json.loads(json_result.output)["session_id"] == "session-123"
