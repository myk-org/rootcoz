"""A key removed between selection and session creation must not select server auth."""

from unittest.mock import AsyncMock

import pytest

from rootcoz import ai_client
from rootcoz.engine import chat


@pytest.mark.asyncio
async def test_user_selection_does_not_fall_back_when_key_disappears(
    monkeypatch, tmp_path
):
    create = AsyncMock(return_value="sid")
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=None))
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(chat, "create_session_safely", create)
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: object())
    choice = ai_client._selected_credential_source.set("user")
    try:
        assert (
            await chat._create_chat_session(
                system_prompt="hi",
                ai_provider="openai",
                ai_model="m",
                repo_path=tmp_path,
            )
            is None
        )
        create.assert_not_awaited()
    finally:
        ai_client._selected_credential_source.reset(choice)
