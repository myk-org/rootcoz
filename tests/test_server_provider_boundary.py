"""AI credential source checks at the shared session boundary."""

from unittest.mock import AsyncMock

import pytest
from pi_sidecar_client import AIResult

from rootcoz import ai_client, storage
from rootcoz.ai_client import call_ai_once as real_call_ai_once


@pytest.mark.asyncio
async def test_user_key_pair_works_without_server_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "keys.db")
    await storage.init_db()
    await storage.create_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "private-key")
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "shared"}]),
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
                "models": [{"provider": "openai", "id": "shared"}],
            }
        ),
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok"))
    monkeypatch.setattr(ai_client, "_call_user_session", sidecar)
    token = ai_client.ai_username.set("alice")
    try:
        await real_call_ai_once("hi", ai_provider="openai", ai_model="shared")
        expected_key = "private-key"  # pragma: allowlist secret
        assert sidecar.await_args.kwargs["api_key"] == expected_key
        force = ai_client.force_server_credentials.set(True)
        try:
            with pytest.raises(ValueError, match="Server provider"):
                await real_call_ai_once("hi", ai_provider="openai", ai_model="shared")
        finally:
            ai_client.force_server_credentials.reset(force)
        assert sidecar.await_count == 1
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_database_failure_denies_server_access(monkeypatch):
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "p", "id": "m"}]),
    )
    monkeypatch.setattr(storage, "get_user_ai_credentials", AsyncMock(return_value={}))
    monkeypatch.setattr(
        storage,
        "can_user_use_server_providers",
        AsyncMock(side_effect=OSError("database down")),
    )
    token = ai_client.ai_username.set("alice")
    try:
        assert (await ai_client.scoped_models())["p"][0][
            "can_use_server_providers"
        ] is False
        with pytest.raises(ValueError, match="access unavailable"):
            await real_call_ai_once("hi", ai_provider="p", ai_model="m")
    finally:
        ai_client.ai_username.reset(token)
