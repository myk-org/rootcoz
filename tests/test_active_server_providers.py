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
    entries = [
        {"provider": provider, "id": f"{provider}-model", "name": provider}
        for provider in providers
    ]
    # Keep the module's sync catalog snapshot in sync with the mocked async
    # catalog: membership resolution reads the snapshot, not the mock.
    ai_client.update_model_catalog(entries)
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=entries),
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
async def test_inactive_provider_still_usable_with_own_key(monkeypatch):
    """Deactivation withdraws the server source only — never the user's own key."""
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
    try:
        assert await ai_client.resolve_catalog_pair("openai", "openai-model") == (
            "openai",
            "openai-model",
        )
        assert ai_client._selected_credential_source.get() == "user"
    finally:
        ai_client.ai_username.reset(token)
    # Direct server-catalog path (no user scope) is rejected.
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


@pytest.mark.asyncio
async def test_saved_public_name_keeps_internal_catalog_models(monkeypatch):
    """Saved public names ('claude'/'cursor') keep cli-*/acpx-* server sources."""
    _catalog(monkeypatch, "cli-claude", "acpx-cursor", "openai")
    _set_active(monkeypatch, "claude,cursor")
    scoped = await ai_client.scoped_models()
    assert scoped["cli-claude"][0]["credential_sources"] == ["server"]
    assert scoped["acpx-cursor"][0]["credential_sources"] == ["server"]
    # Providers not in the saved list still lose their server source.
    assert "openai" not in scoped


@pytest.mark.asyncio
async def test_saved_catalog_id_matches_internal_provider(monkeypatch):
    """Reverse direction: a legacy saved catalog ID still matches its models."""
    _catalog(monkeypatch, "cli-claude", "openai")
    _set_active(monkeypatch, "cli-claude")
    scoped = await ai_client.scoped_models()
    assert scoped["cli-claude"][0]["credential_sources"] == ["server"]
    assert "openai" not in scoped


@pytest.mark.asyncio
async def test_grant_enforcement_accepts_public_name_for_internal_provider(monkeypatch):
    """is_server_provider_active matches public names to internal catalog IDs."""
    _catalog(monkeypatch, "cli-claude", "openai")
    _set_active(monkeypatch, "claude")
    # Saved public name matches the internally-prefixed catalog provider.
    assert ai_client.is_server_provider_active("claude")
    assert ai_client.is_server_provider_active("cli-claude")
    # Non-listed providers stay deactivated.
    assert not ai_client.is_server_provider_active("openai")
    # Reverse direction: the setting may hold the internal ID instead.
    _set_active(monkeypatch, "cli-claude")
    assert ai_client.is_server_provider_active("claude")
    assert ai_client.is_server_provider_active("cli-claude")


@pytest.mark.asyncio
async def test_case_insensitive_match_for_custom_providers(monkeypatch):
    """Saved 'mygateway' keeps models of a catalog provider 'MyGateway'.

    Custom catalog providers pass through public_provider_name unchanged
    (preserving case), so the saved restriction may differ in case only.
    The membership test folds case; the display spelling (picker output)
    keeps the catalog's canonical 'MyGateway'.
    """
    _catalog(monkeypatch, "MyGateway", "openai")
    _set_active(monkeypatch, "mygateway")
    # Saved lowercase restriction matches the catalog's mixed-case provider.
    assert ai_client.is_server_provider_active("MyGateway")
    assert ai_client.is_server_provider_active("mygateway")
    # Scoped models keep the mixed-case provider's server source.
    scoped = await ai_client.scoped_models()
    assert scoped["MyGateway"][0]["credential_sources"] == ["server"]
    # Non-listed providers are still excluded.
    assert "openai" not in scoped
    # Reverse direction: catalog saves lowercase, admin saved mixed case.
    _catalog(monkeypatch, "mygateway", "openai")
    _set_active(monkeypatch, "MyGateway")
    scoped = await ai_client.scoped_models()
    assert scoped["mygateway"][0]["credential_sources"] == ["server"]
    assert "openai" not in scoped


@pytest.mark.asyncio
async def test_case_distinct_catalog_providers_not_collapsed(monkeypatch):
    """Selecting only 'foo' must not activate a distinct catalog provider 'Foo'.

    When the catalog registers case-DISTINCT providers (``Foo`` and ``foo``
    as separate providers), blanket lowercasing would collapse them into one
    allowlist entry and silently grant the unselected one server-credential
    access. Resolution must stay exact for such providers.
    """
    _catalog(monkeypatch, "Foo", "foo")
    _set_active(monkeypatch, "foo")
    assert not ai_client.is_server_provider_active("Foo")
    assert ai_client.is_server_provider_active("foo")
    scoped = await ai_client.scoped_models()
    assert "Foo" not in scoped
    assert scoped["foo"][0]["credential_sources"] == ["server"]


@pytest.mark.asyncio
async def test_case_distinct_providers_both_selected_stay_active(monkeypatch):
    """Selecting both case-distinct providers keeps both active."""
    _catalog(monkeypatch, "Foo", "foo")
    _set_active(monkeypatch, "foo,Foo")
    assert ai_client.is_server_provider_active("Foo")
    assert ai_client.is_server_provider_active("foo")
    scoped = await ai_client.scoped_models()
    assert scoped["Foo"][0]["credential_sources"] == ["server"]
    assert scoped["foo"][0]["credential_sources"] == ["server"]


@pytest.mark.asyncio
async def test_unique_case_variant_still_matches_case_insensitively(monkeypatch):
    """R5 regression: 'mygateway' keeps a sole 'MyGateway' catalog provider active.

    A single case-variant in the catalog resolves uniquely, so the saved
    spelling's case still does not matter — the round-6 exactness fix must
    not reintroduce the round-5 bug.
    """
    _catalog(monkeypatch, "MyGateway")
    _set_active(monkeypatch, "mygateway")
    assert ai_client.is_server_provider_active("MyGateway")
    assert ai_client.is_server_provider_active("mygateway")
    scoped = await ai_client.scoped_models()
    assert scoped["MyGateway"][0]["credential_sources"] == ["server"]


@pytest.mark.asyncio
async def test_unknown_saved_entry_restricts_nothing_new(monkeypatch):
    """A saved entry with no catalog match keeps the pre-existing behavior.

    An unknown entry stays unresolved and only ever matches itself; it must
    not restrict (or activate) catalog providers it does not name.
    """
    _catalog(monkeypatch, "openai")
    _set_active(monkeypatch, "nonexistent")
    assert ai_client.is_server_provider_active("nonexistent")
    assert not ai_client.is_server_provider_active("openai")
    scoped = await ai_client.scoped_models()
    assert "openai" not in scoped


@pytest.mark.asyncio
async def test_case_insensitive_grant_for_custom_provider(monkeypatch):
    """require_server_provider_grant accepts a case-variant custom provider."""
    _catalog(monkeypatch, "MyGateway", "openai")
    _set_active(monkeypatch, "mygateway")
    # Active case-variant provider passes the deactivation check; the
    # failure below must come from the grant layer, not deactivation.
    with pytest.raises(ValueError, match="administrator grant"):
        await ai_client.require_server_provider_grant("MyGateway", "m")
    # A non-listed provider is still rejected as deactivated.
    with pytest.raises(ai_client.InactiveServerProviderError, match="deactivated"):
        await ai_client.require_server_provider_grant("openai", "m")


@pytest.mark.asyncio
async def test_server_capable_providers_returns_public_names(monkeypatch):
    """Settings picker options never leak internal cli-* catalog IDs."""
    _catalog(monkeypatch, "cli-claude", "cli-gemini", "cli-cursor", "openai")
    assert await ai_client.server_capable_providers() == [
        "claude",
        "cursor",
        "gemini",
        "openai",
    ]


@pytest.mark.asyncio
async def test_server_capable_providers_dedupes_internal_and_canonical(monkeypatch):
    """An internal and a canonical ID for the same provider yield one entry."""
    _catalog(monkeypatch, "cli-claude", "claude", "acpx-cursor")
    assert await ai_client.server_capable_providers() == ["claude", "cursor"]


@pytest.mark.asyncio
async def test_catalog_update_invalidates_server_capable_cache(monkeypatch):
    """update_model_catalog() refreshes picker options without the 60s TTL wait."""
    ai_client.update_model_catalog(None)
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": "openai", "id": "m", "name": "m"}]),
    )
    assert await ai_client.server_capable_providers() == ["openai"]
    ai_client.update_model_catalog([{"provider": "cli-cursor", "id": "m", "name": "m"}])
    assert await ai_client.server_capable_providers() == ["cursor"]


@pytest.mark.asyncio
async def test_catalog_refresh_invalidates_server_capable_cache(monkeypatch):
    """A catalog refresh (not just replacement) also drops the picker cache."""
    ai_client.update_model_catalog(None)
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": "openai", "id": "m", "name": "m"}]),
    )
    assert await ai_client.server_capable_providers() == ["openai"]
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": "cli-gemini", "id": "m", "name": "m"}]),
    )
    await ai_client._get_model_catalog(refresh=True)
    assert await ai_client.server_capable_providers() == ["gemini"]
