"""Key-scoped catalog and forced server routing."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from rootcoz import ai_client, main, storage


@pytest.mark.asyncio
async def test_scoped_discovery_and_call_fail_closed(monkeypatch):
    secret = "private-key-do-not-return"  # pragma: allowlist secret
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(
            return_value=[
                {"provider": "openai", "id": "shared", "name": "Shared"},
                {"provider": "anthropic", "id": "claude", "name": "Claude"},
            ]
        ),
    )
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"openai": secret})
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
                "models": [
                    {"provider": "openai", "id": "shared", "name": "Shared"},
                    {"provider": "openai", "id": "user-only", "name": "User only"},
                ],
            }
        ),
    )
    token = ai_client.ai_username.set("alice")
    try:
        models = await ai_client.scoped_models()
        assert models["openai"] == [
            {
                "provider": "openai",
                "id": "shared",
                "name": "Shared",
                "source": "api",
                "credential_sources": ["user", "server"],
                "verified": True,
            },
            {
                "provider": "openai",
                "id": "user-only",
                "name": "User only",
                "source": "api",
                "credential_sources": ["user"],
                "verified": True,
            },
        ]
        assert "xai" not in models
        assert secret not in str(models)
        assert await ai_client.resolve_catalog_pair("openai", "user-only") == (
            "openai",
            "user-only",
        )
        with pytest.raises(ValueError):
            await ai_client.resolve_catalog_pair("openai", "claude")
        ai_client.force_server_credentials.set(True)
        with pytest.raises(ValueError):
            await ai_client.resolve_catalog_pair("openai", "user-only")
        assert await ai_client.session_key("openai") is None
    finally:
        ai_client.force_server_credentials.set(False)
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_no_listing_allows_manual_only_with_matching_key(monkeypatch):
    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        storage, "get_user_ai_credentials", AsyncMock(return_value={"xai": "x-secret"})
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["xai"])
    )
    discovery = AsyncMock(return_value={"models": [], "modelListingSupported": False})
    monkeypatch.setattr(ai_client, "models_for_api_key", discovery)
    token = ai_client.ai_username.set("alice")
    try:
        scoped = await ai_client.scoped_models()
        assert scoped["xai"]
        assert all(not m["verified"] for m in scoped["xai"])
        assert await ai_client.resolve_catalog_pair("xai", "manual-model") == (
            "xai",
            "manual-model",
        )
        assert ai_client._selected_credential_source.get() == "user"
        with pytest.raises(ValueError):
            await ai_client.resolve_catalog_pair("openai", "manual-model")
        with pytest.raises(ValueError):
            await ai_client.resolve_catalog_pair("xai", "  ")
        force = ai_client.force_server_credentials.set(True)
        try:
            with pytest.raises(ValueError):
                await ai_client.resolve_catalog_pair("xai", "manual-model")
        finally:
            ai_client.force_server_credentials.reset(force)
        assert discovery.await_args.args == ("xai", "x-secret")
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_manual_only_provider_visible_in_endpoint(monkeypatch):
    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        storage,
        "get_user_ai_credentials",
        AsyncMock(return_value={"custom-provider": "key"}),
    )
    monkeypatch.setattr(
        ai_client,
        "supported_key_providers",
        AsyncMock(return_value=["custom-provider"]),
    )
    monkeypatch.setattr(
        ai_client,
        "models_for_api_key",
        AsyncMock(return_value={"models": [], "modelListingSupported": False}),
    )
    monkeypatch.setattr(main, "_require_authenticated", lambda request: None)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: type("Settings", (), {"force_server_credentials": False})(),
    )
    request = type(
        "Request",
        (),
        {"state": type("State", (), {"username": "alice", "is_admin": False})()},
    )()
    data = await main.list_ai_models(request, provider="custom-provider")
    assert data["models"] == []
    assert data["modelListingSupported"] is False
    assert "key" not in str(data)


@pytest.mark.asyncio
async def test_missing_upstream_does_not_guess_user_models(monkeypatch):
    monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
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
            side_effect=httpx.HTTPStatusError(
                "404",
                request=httpx.Request("POST", "http://localhost/models/for-api-key"),
                response=httpx.Response(404),
            )
        ),
    )
    token = ai_client.ai_username.set("alice")
    try:
        with pytest.raises(
            ValueError, match="Key-scoped model discovery failed.*Retry"
        ):
            await ai_client.scoped_models()
        with pytest.raises(ValueError, match="Key-scoped model discovery failed"):
            await ai_client.resolve_catalog_pair("openai", "made-up")
        # Even an exact server pair cannot silently switch credential source.
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(return_value=[{"provider": "openai", "id": "shared"}]),
        )
        with pytest.raises(ValueError, match="Key-scoped model discovery failed"):
            await ai_client.resolve_catalog_pair("openai", "shared")
        monkeypatch.setattr(main, "_require_authenticated", lambda request: None)
        monkeypatch.setattr(
            main,
            "get_settings",
            lambda: type("Settings", (), {"force_server_credentials": False})(),
        )
        request = type(
            "Request",
            (),
            {"state": type("State", (), {"username": "alice", "is_admin": False})()},
        )()
        with pytest.raises(HTTPException) as exc:
            await main.list_ai_models(request, provider="openai")
        assert exc.value.status_code == 503
        assert "Retry" in exc.value.detail
        monkeypatch.setattr(
            ai_client,
            "models_for_api_key",
            AsyncMock(return_value={"models": [], "modelListingSupported": False}),
        )
        assert await ai_client.resolve_catalog_pair("openai", "manual") == (
            "openai",
            "manual",
        )
    finally:
        ai_client.ai_username.reset(token)
