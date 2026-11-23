"""Dedicated unit and architectural tests for ``rootcoz.ai_catalog``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from rootcoz import ai_client, storage
from rootcoz.ai_catalog import (
    ResolvedAiSelection,
    ScopedCatalog,
    check_server_provider_grant,
    load_scoped_catalog,
    resolve_ai_selection,
    resolve_provider_model_values,
    scoped_ai_context,
    validate_pinned_source,
)


def _rejected_http_error(status_code: int = 401) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://provider.invalid/v1/models")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("rejected", request=request, response=response)


@pytest.fixture
async def catalog_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ai-catalog.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))
    await storage.init_db()
    return db_path


def _set_settings_override(monkeypatch, **overrides):
    from rootcoz import config

    base = {
        "ai_provider": "",
        "ai_model": "",
        "active_server_providers": "",
        "enable_free_models": False,
        **overrides,
    }
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(**base))


# ---------------------------------------------------------------------------
# ScopedCatalog value object
# ---------------------------------------------------------------------------


class TestScopedCatalog:
    def test_resolve_provider_spelling_preserves_mixed_case(self):
        catalog = ScopedCatalog(
            providers={
                "EnMaaS": [{"id": "glm-5-3", "provider": "EnMaaS"}],
                "MyGateway": [{"id": "gw-1", "provider": "MyGateway"}],
            },
            provider_status={
                "ManualCustom": {"has_api_key": True, "modelListingSupported": False}
            },
        )
        assert catalog.resolve_provider_spelling("enmaas") == "EnMaaS"
        assert catalog.resolve_provider_spelling("ENMAAS") == "EnMaaS"
        assert catalog.resolve_provider_spelling("mygateway") == "MyGateway"
        assert catalog.resolve_provider_spelling("manualcustom") == "ManualCustom"
        assert catalog.resolve_provider_spelling("unknown") == "unknown"

    def test_resolve_provider_spelling_rejects_ambiguous_case_variants(self):
        catalog = ScopedCatalog(
            providers={
                "Foo": [{"id": "m-upper", "provider": "Foo"}],
                "foo": [{"id": "m-lower", "provider": "foo"}],
            },
            provider_status={},
        )
        # Ambiguous variants must never silently pick another spelling.
        assert catalog.resolve_provider_spelling("FOO") == "FOO"
        assert catalog.resolve_provider_spelling("foo") == "foo"
        assert catalog.resolve_provider_spelling("Foo") == "Foo"

    def test_models_for_handles_exact_case_insensitive_status_only_and_ambiguous(self):
        catalog = ScopedCatalog(
            providers={
                "EnMaaS": [{"id": "glm-5-3", "provider": "EnMaaS"}],
                "Foo": [{"id": "m-upper", "provider": "Foo"}],
                "foo": [{"id": "m-lower", "provider": "foo"}],
            },
            provider_status={
                "ManualOnly": {"has_api_key": True, "modelListingSupported": False}
            },
        )
        exact, models, status = catalog.models_for("  enmaas ")
        assert exact == "EnMaaS"
        assert [m["id"] for m in models] == ["glm-5-3"]
        assert status is None

        # Status-only provider still resolves cleanly.
        exact, models, status = catalog.models_for("manualonly")
        assert exact == "ManualOnly"
        assert models == []
        assert status == {"has_api_key": True, "modelListingSupported": False}

        # Ambiguous case-distinct query refuses silent match unless exact.
        exact, models, status = catalog.models_for("FOO")
        assert exact == "foo"
        assert models == []
        assert status is None

        exact, models, status = catalog.models_for("Foo")
        assert exact == "Foo"
        assert [m["id"] for m in models] == ["m-upper"]

        # Unknown provider returns normalized spelling and empty results.
        exact, models, status = catalog.models_for("NoSuchProvider")
        assert exact == "nosuchprovider"
        assert models == []
        assert status is None


# ---------------------------------------------------------------------------
# load_scoped_catalog
# ---------------------------------------------------------------------------


class TestLoadScopedCatalog:
    @pytest.mark.asyncio
    async def test_load_scoped_catalog_full_pipeline_without_contextvar_mutation(
        self, catalog_db, monkeypatch
    ):
        await storage.create_user(
            "alice", role="reviewer", can_use_server_providers=True
        )
        await storage.update_user_ai_credential("alice", "openai", "user-openai-key")
        await storage.update_user_ai_credential("alice", "manualp", "user-manual-key")
        await storage.update_user_ai_credential("alice", "badkey", "user-bad-key")

        # Admin-selected server models on openai: only gpt-shared exposed on server.
        await storage.set_server_provider_models(
            "openai", [{"id": "gpt-shared", "included": True, "free": True}], "admin"
        )
        # Key-scoped server models on MyGateway (not in static catalog).
        await storage.set_server_provider_key_models(
            "MyGateway", [{"id": "gw-1", "name": "GW 1"}]
        )

        # Active server providers: allow openai and MyGateway, exclude cursor.
        _set_settings_override(
            monkeypatch, active_server_providers="openai,MyGateway,badkey"
        )
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(
                return_value=[
                    {"provider": "openai", "id": "gpt-shared"},
                    {"provider": "openai", "id": "gpt-deselected"},
                    {"provider": "cursor", "id": "cursor-1"},
                    {"provider": "badkey", "id": "bk-server"},
                ]
            ),
        )
        monkeypatch.setattr(
            ai_client,
            "supported_key_providers",
            AsyncMock(return_value=["openai", "manualp", "badkey"]),
        )

        async def discover(provider: str, _key: str):
            if provider == "openai":
                return {
                    "modelListingSupported": True,
                    "models": [
                        {"provider": "openai", "id": "gpt-shared"},
                        {"provider": "openai", "id": "gpt-user-only"},
                    ],
                }
            if provider == "manualp":
                return {"modelListingSupported": False, "models": []}
            if provider == "badkey":
                raise _rejected_http_error(401)
            raise AssertionError(f"Unexpected provider {provider}")

        monkeypatch.setattr(ai_client, "models_for_api_key", discover)

        # Seed sentinel value in ambient ContextVar and verify it is NOT mutated.
        tok_user = ai_client.ai_username.set("sentinel-user")
        try:
            catalog = await load_scoped_catalog("alice")

            # Ambient ContextVar untouched!
            assert ai_client.ai_username.get() == "sentinel-user"
        finally:
            ai_client.ai_username.reset(tok_user)

        # Deactivated provider excluded.
        assert "cursor" not in catalog.providers

        # Key-scoped server provider keeps exact mixed-case spelling.
        assert "MyGateway" in catalog.providers
        assert [m["id"] for m in catalog.providers["MyGateway"]] == ["gw-1"]

        # openai: user priority on shared pair + free tag; deselected server model excluded.
        openai_by_id = {m["id"]: m for m in catalog.providers["openai"]}
        assert set(openai_by_id) == {"gpt-shared", "gpt-user-only"}
        assert openai_by_id["gpt-shared"]["credential_sources"] == ["user", "server"]
        assert openai_by_id["gpt-shared"]["free"] is True
        assert openai_by_id["gpt-user-only"]["credential_sources"] == ["user"]

        # Manual-only user provider preserved in providers + provider_status.
        assert "manualp" in catalog.providers
        assert catalog.provider_status["manualp"]["modelListingSupported"] is False

        # Rejected user key tracked in rejected_user_keys, not leaked as key flag in provider_status.
        assert catalog.rejected_user_keys == frozenset({"badkey"})
        assert catalog.provider_status["badkey"]["unavailable"] is True
        assert "rejected" not in catalog.provider_status["badkey"]
        assert [m["id"] for m in catalog.providers["badkey"]] == ["bk-server"]


# ---------------------------------------------------------------------------
# resolve_ai_selection & grants
# ---------------------------------------------------------------------------


class TestResolveAiSelection:
    @pytest.mark.asyncio
    async def test_user_key_preferred_over_server_pool(self, catalog_db, monkeypatch):
        await storage.create_user(
            "alice", role="reviewer", can_use_server_providers=True
        )
        await storage.update_user_ai_credential("alice", "openai", "user-key")
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(return_value=[{"provider": "openai", "id": "gpt-5"}]),
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
                    "models": [{"provider": "openai", "id": "gpt-5"}],
                }
            ),
        )

        selection = await resolve_ai_selection("openai", "gpt-5", username="alice")
        assert selection == ResolvedAiSelection("openai", "gpt-5", "user")
        assert selection.as_pair() == ("openai", "gpt-5")

    @pytest.mark.asyncio
    async def test_server_fallback_when_user_key_absent_or_rejected(
        self, catalog_db, monkeypatch
    ):
        await storage.create_user(
            "alice", role="reviewer", can_use_server_providers=True
        )
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(return_value=[{"provider": "EnMaaS", "id": "glm-5-3"}]),
        )

        # Absent user key -> server pool with exact custom provider casing (#649).
        sel_absent = await resolve_ai_selection("enmaas", "glm-5-3", username="alice")
        assert sel_absent == ResolvedAiSelection("EnMaaS", "glm-5-3", "server")

        # Rejected user key + server pool serves pair -> server fallback.
        await storage.update_user_ai_credential("alice", "EnMaaS", "bad-key")
        monkeypatch.setattr(
            ai_client, "supported_key_providers", AsyncMock(return_value=["EnMaaS"])
        )
        monkeypatch.setattr(
            ai_client,
            "models_for_api_key",
            AsyncMock(side_effect=_rejected_http_error(401)),
        )
        sel_fallback = await resolve_ai_selection("enmaas", "glm-5-3", username="alice")
        assert sel_fallback == ResolvedAiSelection("EnMaaS", "glm-5-3", "server")

        # Rejected user key + no server pair -> RejectedUserKeyError.
        with pytest.raises(ai_client.RejectedUserKeyError, match="rejected"):
            await resolve_ai_selection("enmaas", "missing-model", username="alice")

    @pytest.mark.asyncio
    async def test_manual_only_user_provider_resolves_as_user(
        self, catalog_db, monkeypatch
    ):
        await storage.create_user("alice", role="reviewer")
        await storage.update_user_ai_credential("alice", "MyCustom", "secret-key")
        monkeypatch.setattr(ai_client, "_get_model_catalog", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            ai_client, "supported_key_providers", AsyncMock(return_value=["MyCustom"])
        )
        monkeypatch.setattr(
            ai_client,
            "models_for_api_key",
            AsyncMock(return_value={"modelListingSupported": False, "models": []}),
        )

        sel = await resolve_ai_selection(
            "mycustom", "custom-model-id", username="alice"
        )
        assert sel == ResolvedAiSelection("MyCustom", "custom-model-id", "user")

    @pytest.mark.asyncio
    async def test_inactive_provider_blocks_server_but_allows_user_key(
        self, catalog_db, monkeypatch
    ):
        await storage.create_user(
            "alice", role="reviewer", can_use_server_providers=True
        )
        _set_settings_override(monkeypatch, active_server_providers="anthropic")
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(
                return_value=[
                    {"provider": "MyGateway", "id": "gw-1"},
                    {"provider": "anthropic", "id": "claude-4"},
                ]
            ),
        )

        # Without user key: deactivated server provider raises InactiveServerProviderError.
        with pytest.raises(ai_client.InactiveServerProviderError, match="deactivated"):
            await resolve_ai_selection("mygateway", "gw-1", username="alice")

        # With user key on the same provider: resolves with credential_source="user".
        await storage.update_user_ai_credential("alice", "MyGateway", "user-key")
        monkeypatch.setattr(
            ai_client, "supported_key_providers", AsyncMock(return_value=["MyGateway"])
        )
        monkeypatch.setattr(
            ai_client,
            "models_for_api_key",
            AsyncMock(
                return_value={
                    "modelListingSupported": True,
                    "models": [{"provider": "MyGateway", "id": "gw-1"}],
                }
            ),
        )
        sel = await resolve_ai_selection("mygateway", "gw-1", username="alice")
        assert sel == ResolvedAiSelection("MyGateway", "gw-1", "user")

    @pytest.mark.asyncio
    async def test_deselected_server_model_raises_excluded_error(
        self, catalog_db, monkeypatch
    ):
        await storage.create_user(
            "alice", role="reviewer", can_use_server_providers=True
        )
        await storage.set_server_provider_models(
            "openai", [{"id": "gpt-allowed", "included": True, "free": False}], "admin"
        )
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(
                return_value=[
                    {"provider": "openai", "id": "gpt-allowed"},
                    {"provider": "openai", "id": "gpt-blocked"},
                ]
            ),
        )

        with pytest.raises(ai_client.ExcludedServerModelError):
            await resolve_ai_selection("openai", "gpt-blocked", username="alice")

    @pytest.mark.asyncio
    async def test_legacy_alias_unambiguous_resolves_and_ambiguous_fails(
        self, catalog_db, monkeypatch
    ):
        from rootcoz import ai_catalog

        monkeypatch.setattr(
            ai_catalog, "check_server_provider_grant", AsyncMock(return_value=None)
        )
        # Unambiguous legacy alias: gemini -> google.
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(return_value=[{"provider": "google", "id": "gemini-2.5-pro"}]),
        )
        sel = await resolve_ai_selection("gemini", "gemini-2.5-pro", username="")
        assert sel == ResolvedAiSelection("google", "gemini-2.5-pro", "server")

        # Ambiguous legacy alias: model exists under both google and google-vertex.
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(
                return_value=[
                    {"provider": "google", "id": "gemini-2.5-pro"},
                    {"provider": "google-vertex", "id": "gemini-2.5-pro"},
                ]
            ),
        )
        with pytest.raises(ValueError, match="Unknown Pi-sidecar provider/model pair"):
            await resolve_ai_selection("gemini", "gemini-2.5-pro", username="")

    @pytest.mark.asyncio
    async def test_free_model_grant_and_full_server_grant_precedence(
        self, catalog_db, monkeypatch
    ):
        await storage.create_user(
            "regular", role="reviewer", can_use_server_providers=False
        )
        await storage.create_user(
            "granted", role="reviewer", can_use_server_providers=True
        )
        await storage.set_free_ai_models([("openai", "gpt-free")], "admin")
        monkeypatch.setattr(
            ai_client,
            "_get_model_catalog",
            AsyncMock(
                return_value=[
                    {"provider": "openai", "id": "gpt-free"},
                    {"provider": "openai", "id": "gpt-paid"},
                ]
            ),
        )

        # Toggle ON: regular user can use free pair, denied non-free pair.
        _set_settings_override(monkeypatch, enable_free_models=True)
        sel = await resolve_ai_selection("openai", "gpt-free", username="regular")
        assert sel == ResolvedAiSelection("openai", "gpt-free", "server")
        with pytest.raises(ValueError, match="administrator grant"):
            await resolve_ai_selection("openai", "gpt-paid", username="regular")

        # Toggle OFF: regular user denied free pair, granted user still allowed everything.
        _set_settings_override(monkeypatch, enable_free_models=False)
        with pytest.raises(ValueError, match="administrator grant"):
            await resolve_ai_selection("openai", "gpt-free", username="regular")
        sel_granted = await resolve_ai_selection(
            "openai", "gpt-paid", username="granted"
        )
        assert sel_granted == ResolvedAiSelection("openai", "gpt-paid", "server")


# ---------------------------------------------------------------------------
# Grant checks, ContextVar scope manager, and helpers
# ---------------------------------------------------------------------------


class TestGrantAndContextScoping:
    @pytest.mark.asyncio
    async def test_check_server_provider_grant_does_not_mutate_ai_username(
        self, catalog_db
    ):
        await storage.create_user(
            "granted", role="reviewer", can_use_server_providers=True
        )
        await storage.create_user(
            "ungranted", role="reviewer", can_use_server_providers=False
        )
        tok = ai_client.ai_username.set("ambient-caller")
        try:
            await check_server_provider_grant("granted", "openai", "gpt-5")
            assert ai_client.ai_username.get() == "ambient-caller"

            with pytest.raises(ValueError, match="administrator grant"):
                await check_server_provider_grant("ungranted", "openai", "gpt-5")
            assert ai_client.ai_username.get() == "ambient-caller"
        finally:
            ai_client.ai_username.reset(tok)

    def test_scoped_ai_context_sets_and_restores_on_exit_and_exception(self):
        tok_user = ai_client.ai_username.set("outer-user")
        try:
            with scoped_ai_context("inner-user"):
                assert ai_client.ai_username.get() == "inner-user"
            assert ai_client.ai_username.get() == "outer-user"

            with (
                pytest.raises(RuntimeError, match="boom"),
                scoped_ai_context("err-user"),
            ):
                assert ai_client.ai_username.get() == "err-user"
                raise RuntimeError("boom")
            assert ai_client.ai_username.get() == "outer-user"
        finally:
            ai_client.ai_username.reset(tok_user)

    def test_validate_pinned_source(self):
        sel = ResolvedAiSelection("openai", "gpt-5", "user")
        validate_pinned_source(sel, pinned_source="")
        validate_pinned_source(sel, pinned_source="user")
        with pytest.raises(ValueError, match="changed during Start"):
            validate_pinned_source(sel, pinned_source="server")

    def test_resolve_provider_model_values_priority_and_normalization(self):
        settings = SimpleNamespace(ai_provider="OpenAI", ai_model="gpt-default")
        assert resolve_provider_model_values(settings=settings) == (
            "openai",
            "gpt-default",
        )
        assert resolve_provider_model_values(
            "  Anthropic ", "claude-4", settings=settings
        ) == ("anthropic", "claude-4")
        assert resolve_provider_model_values("Google", None, settings=settings) == (
            "google",
            "gpt-default",
        )


# ---------------------------------------------------------------------------
# Architectural invariants
# ---------------------------------------------------------------------------


def test_main_and_chat_have_no_direct_credential_source_contextvar_references():
    """Credential-source ContextVars and dual-path test-patch branches must not exist."""
    import re

    repo_root = Path(__file__).resolve().parents[1]
    forbidden_identifiers = (
        r"\b_selected_credential_source\b",
        r"\bchat_session_source\b",
        r"\bmodel_listing_status\b",
        r"\b_rejected_user_keys\b",
        r"_DEFAULT_VALIDATE_",
        r"_DEFAULT_RESOLVE_",
        r"_DEFAULT_SCOPED_",
        r"_DEFAULT_REQUIRE_",
        r"_DEFAULT_NORMALIZE_",
    )
    for py_file in (repo_root / "src" / "rootcoz").rglob("*.py"):
        source_text = py_file.read_text(encoding="utf-8")
        for pattern in forbidden_identifiers:
            assert not re.search(pattern, source_text), (
                f"{py_file.relative_to(repo_root)} must not reference {pattern!r}"
            )
