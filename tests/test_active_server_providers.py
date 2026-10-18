"""Admin-controlled active server providers — rootcoz issue #338 feature 5.

``ACTIVE_SERVER_PROVIDERS`` selects which server-side providers keep their
server credential source in user-facing model listings; empty = all (the
pre-existing behavior). Enforcement rechecks on every server-credential use,
so a direct API call cannot bypass the catalog filtering.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from rootcoz import ai_client, config, main, storage
from rootcoz.ai_client import AIResult
from rootcoz.ai_client import (
    call_ai as call_ai_under_test,  # real fn: conftest denies the module attr
)


def _set_active(monkeypatch, value: str) -> None:
    """Point ai_client's settings reads at a stub with the setting set."""

    class _Settings:
        active_server_providers = value
        enable_free_models = False

    monkeypatch.setattr(config, "get_settings", lambda: _Settings())


def _catalog(monkeypatch, *providers: str) -> None:
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(
            return_value=[
                {"provider": provider, "id": f"{provider}-model", "name": provider}
                for provider in providers
            ]
        ),
    )


@pytest.mark.asyncio
async def test_empty_setting_keeps_all_server_sources(monkeypatch):
    """Empty ACTIVE_SERVER_PROVIDERS = no filtering (backward compatible)."""
    _catalog(monkeypatch, "openai", "anthropic")
    _set_active(monkeypatch, "")
    scoped = await ai_client.scoped_models()
    assert scoped["openai"][0]["credential_sources"] == ["server"]
    assert scoped["anthropic"][0]["credential_sources"] == ["server"]


@pytest.mark.asyncio
async def test_inactive_provider_loses_server_source(monkeypatch):
    """Inactive provider: server source removed, user source kept, else dropped."""
    _catalog(monkeypatch, "openai", "anthropic")
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": "secret"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(
            return_value={
                "modelListingSupported": True,
                "models": [{"provider": "openai", "id": "openai-model"}],
            }
        ),
    )
    # Only anthropic is active; openai is deactivated server-side.
    _set_active(monkeypatch, "anthropic")
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        # openai keeps its models for the user's own key, server source gone.
        assert [e["credential_sources"] for e in scoped["openai"]] == [["user"]]
        # anthropic (active) is untouched.
        assert scoped["anthropic"][0]["credential_sources"] == ["server"]
        # A deactivated provider without a user key disappears entirely.
        _catalog(monkeypatch, "openai", "anthropic", "cursor")
        scoped = await ai_client.scoped_models()
        assert "cursor" not in scoped
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_deactivated_provider_mid_chat_gets_deactivation_message(
    tmp_path, monkeypatch
):
    """Post-reply recheck surfaces the deactivation message, not the grant one."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    monkeypatch.setattr(
        ai_client,
        "resolve_catalog_pair",
        AsyncMock(return_value=("cursor", "cursor-model")),
    )
    monkeypatch.setattr(
        ai_client,
        "_call_with_safe_error",
        AsyncMock(return_value=AIResult(success=True, text="ok")),
    )
    # First check (before the call) passes; the post-reply recheck sees the
    # admin deactivate the provider mid-chat.
    monkeypatch.setattr(
        ai_client,
        "require_server_provider_grant",
        AsyncMock(
            side_effect=[
                None,
                ai_client.InactiveServerProviderError(
                    "Server credentials for provider 'cursor' are deactivated "
                    "by the administrator (active_server_providers)"
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        storage, "get_ai_session_source", AsyncMock(return_value="server")
    )
    token = ai_client.chat_session_source.set("server")
    try:
        result = await call_ai_under_test(
            "prompt", session_id="s1", ai_provider="cursor", ai_model="cursor-model"
        )
    finally:
        ai_client.chat_session_source.reset(token)
    assert result.success is False
    assert "deactivated" in result.text
    assert "administrator grant" not in result.text


@pytest.mark.asyncio
async def test_forced_server_credentials_rejected_for_inactive_provider(monkeypatch):
    """force_server_credentials cannot bypass the deactivation."""
    _catalog(monkeypatch, "openai", "anthropic")
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": "secret"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(
            return_value={
                "modelListingSupported": True,
                "models": [{"provider": "openai", "id": "openai-model"}],
            }
        ),
    )
    _set_active(monkeypatch, "anthropic")
    token = ai_client.ai_username.set("alice")
    force = ai_client.force_server_credentials.set(True)
    try:
        with pytest.raises(ai_client.InactiveServerProviderError, match="deactivated"):
            await ai_client.resolve_catalog_pair("openai", "openai-model")
    finally:
        ai_client.force_server_credentials.reset(force)
        ai_client.ai_username.reset(token)
    # Direct server-catalog path (no user scope) is rejected too.
    with pytest.raises(ai_client.InactiveServerProviderError, match="deactivated"):
        await ai_client.resolve_catalog_pair("openai", "openai-model")


@pytest.mark.asyncio
async def test_validate_catalog_pair_maps_inactive_provider_to_400(monkeypatch):
    """_validate_catalog_pair rejects an inactive provider pair with a 400."""
    _catalog(monkeypatch, "cursor", "gemini")
    _set_active(monkeypatch, "gemini")
    with pytest.raises(HTTPException) as exc:
        await main._validate_catalog_pair("cursor", "cursor-model")
    assert exc.value.status_code == 400
    assert "deactivated" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_free_pair_on_inactive_provider_not_usable(tmp_path, monkeypatch):
    """A free_ai_models pair on a deactivated provider cannot use server credentials."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "active-providers.db")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "active-providers.db"))
    await storage.init_db()
    await storage.create_user("freeuser")
    await storage.set_free_ai_models([("cursor", "cursor-model")], "admin")
    # Full server grant + free grant: the deactivation still wins.
    await storage.set_user_can_use_server_providers("freeuser", True)
    _set_active(monkeypatch, "gemini")
    token = ai_client.ai_username.set("freeuser")
    try:
        with pytest.raises(ai_client.InactiveServerProviderError, match="deactivated"):
            await ai_client.require_server_provider_grant("cursor", "cursor-model")
        # Active providers keep the grant path working.
        await ai_client.require_server_provider_grant("gemini", "any-model")
    finally:
        ai_client.ai_username.reset(token)
