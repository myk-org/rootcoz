"""A resumed AI session cannot switch from its persisted credential source."""

from unittest.mock import AsyncMock

import pytest
from pi_sidecar_client import AIResult

from rootcoz import ai_client, storage
from rootcoz.ai_client import call_ai as real_call_ai
from rootcoz.engine import chat


@pytest.mark.asyncio
async def test_resumed_session_rejects_source_change(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "source.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    await storage.save_ai_session_source("sid", "alice", "openai", "server")

    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="user-key"))
    call = AsyncMock()
    monkeypatch.setattr(ai_client, "_call_with_safe_error", call)
    user = ai_client.ai_username.set("alice")
    source = ai_client.chat_session_source.set("user")
    try:
        result = await real_call_ai(
            "hello", ai_provider="openai", ai_model="m", session_id="sid"
        )
        assert not result.success
        call.assert_not_awaited()
    finally:
        ai_client.chat_session_source.reset(source)
        ai_client.ai_username.reset(user)


@pytest.mark.asyncio
async def test_lost_user_session_never_retries_on_server_after_key_removed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "retry.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(chat, "call_ai", real_call_ai)
    calls = []

    async def safe_call(sidecar, *args, **kwargs):
        calls.append(sidecar)
        if kwargs.get("session_id"):
            await storage.update_user_ai_credential("alice", "openai", None)
            return AIResult(success=False, text="session not found")
        return AIResult(success=True, text="SERVER REPLY")

    monkeypatch.setattr(ai_client, "_call_with_safe_error", safe_call)
    user = ai_client.ai_username.set("alice")
    source = ai_client.chat_session_source.set("user")
    try:
        success, text, _ = await chat._chat_with_ai_impl(
            message="hi",
            history=[],
            ai_provider="openai",
            ai_model="m",
            build_prompt_fn=lambda: "prompt",
            session_id="sid",
            install_mcp=False,
        )
        assert not success
        assert "SERVER REPLY" not in text
        assert len(calls) == 1
    finally:
        ai_client.chat_session_source.reset(source)
        ai_client.ai_username.reset(user)


@pytest.mark.asyncio
async def test_new_chat_call_rejects_source_switch_even_direct(monkeypatch):
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=None))
    sidecar = AsyncMock(return_value=AIResult(success=True, text="SERVER REPLY"))
    monkeypatch.setattr(ai_client, "_call_with_safe_error", sidecar)
    user = ai_client.ai_username.set("alice")
    source = ai_client.chat_session_source.set("user")
    try:
        result = await real_call_ai("hi", ai_provider="openai", ai_model="m")
        assert not result.success
        sidecar.assert_not_awaited()
    finally:
        ai_client.chat_session_source.reset(source)
        ai_client.ai_username.reset(user)


@pytest.mark.asyncio
async def test_direct_server_chat_call_does_not_return_reply_after_grant_revoked(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "grant-direct.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )

    async def revoke(*args, **kwargs):
        await storage.set_user_can_use_server_providers("alice", False)
        return AIResult(success=True, text="SECRET REPLY")

    monkeypatch.setattr(ai_client, "_call_with_safe_error", revoke)
    user = ai_client.ai_username.set("alice")
    source = ai_client.chat_session_source.set("server")
    force = ai_client.force_server_credentials.set(True)
    try:
        result = await real_call_ai("hi", ai_provider="openai", ai_model="m")
        assert not result.success
        assert "SECRET REPLY" not in result.text
    finally:
        ai_client.force_server_credentials.reset(force)
        ai_client.chat_session_source.reset(source)
        ai_client.ai_username.reset(user)


@pytest.mark.asyncio
async def test_stale_selection_does_not_reject_pinned_server_session(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "stale.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
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
    from rootcoz import main

    stale = ai_client._selected_credential_source.set("user")
    try:
        assert await main._check_chat_session_choice(
            "job", "alice", "openai", "m", True
        ) == ("sid", True)
    finally:
        ai_client._selected_credential_source.reset(stale)
