"""Free-pair enforcement in ai_client — issue #317.

require_server_provider_grant must admit an admin-declared free pair for a
user when the ENABLE_FREE_MODELS server toggle is on, and scoped_models must
flag free pairs on the catalog entries.
"""

import pytest

from rootcoz import ai_client, storage


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "free-ai-client.db")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "free-ai-client.db"))
    await storage.init_db()
    yield storage


def _enable_free_models(monkeypatch, enabled: bool) -> None:
    """Point can_user_use_free_providers at a settings object with the toggle set."""
    from rootcoz import config

    class _Settings:
        enable_free_models = enabled

    monkeypatch.setattr(config, "get_settings", lambda: _Settings())


async def test_grant_function_admits_free_pair(db, monkeypatch):
    _, _key = await storage.create_user("freeuser")
    _enable_free_models(monkeypatch, True)
    token = ai_client.ai_username.set("freeuser")
    try:
        await storage.set_free_ai_models([("EnMaaS", "rits/zai-org/glm-5-3")], "admin")
        # Free pair + free grant → allowed (case-insensitive provider).
        await ai_client.require_server_provider_grant("enmaas", "rits/zai-org/glm-5-3")
        await ai_client.require_server_provider_grant("EnMaaS", "rits/zai-org/glm-5-3")
        # Non-free pair → denied.
        with pytest.raises(ValueError, match="administrator grant"):
            await ai_client.require_server_provider_grant("EnMaaS", "other-model")
        with pytest.raises(ValueError, match="administrator grant"):
            await ai_client.require_server_provider_grant("openai", "gpt-5.6-luna")
    finally:
        ai_client.ai_username.reset(token)


async def test_grant_function_denies_when_toggle_off(db, monkeypatch):
    await storage.create_user("plainuser")
    _enable_free_models(monkeypatch, False)
    await storage.set_free_ai_models([("EnMaaS", "rits/zai-org/glm-5-3")], "admin")
    token = ai_client.ai_username.set("plainuser")
    try:
        with pytest.raises(ValueError, match="administrator grant"):
            await ai_client.require_server_provider_grant(
                "EnMaaS", "rits/zai-org/glm-5-3"
            )
    finally:
        ai_client.ai_username.reset(token)


async def test_grant_function_server_grant_supersedes(db):
    await storage.create_user("serveruser", can_use_server_providers=True)
    token = ai_client.ai_username.set("serveruser")
    try:
        # Any pair passes with the full server grant.
        await ai_client.require_server_provider_grant("openai", "gpt-5.6-luna")
    finally:
        ai_client.ai_username.reset(token)


async def test_is_free_ai_pair_matches_case_insensitive_provider(db):
    await storage.set_free_ai_models([("EnMaaS", "glm-5-3")], "admin")
    assert await ai_client.is_free_ai_pair("enmaas", "glm-5-3") is True
    assert await ai_client.is_free_ai_pair("EnMaaS", "glm-5-3") is True
    assert await ai_client.is_free_ai_pair("EnMaaS", "other") is False
    assert await ai_client.is_free_ai_pair("unknown", "glm-5-3") is False


async def test_scoped_models_flags_free_pairs(db, monkeypatch):
    catalog = [
        {"provider": "EnMaaS", "id": "glm-5-3", "name": "GLM 5.3"},
        {"provider": "openai", "id": "gpt-5.6-luna", "name": "Luna"},
    ]

    async def fake_catalog(refresh: bool = False):
        return catalog

    monkeypatch.setattr(ai_client, "_get_model_catalog", fake_catalog)
    await storage.set_free_ai_models([("EnMaaS", "glm-5-3")], "admin")

    token = ai_client.ai_username.set("anyone")
    try:
        scoped = await ai_client.scoped_models()
    finally:
        ai_client.ai_username.reset(token)

    enmaas = scoped["EnMaaS"]
    assert len(enmaas) == 1
    assert enmaas[0]["free"] is True
    openai = scoped["openai"]
    assert len(openai) == 1
    assert "free" not in openai[0]
