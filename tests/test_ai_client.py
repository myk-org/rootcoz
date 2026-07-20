"""Tests for rootcoz.ai_client provider mapping and model listing."""

from __future__ import annotations

import pytest

from rootcoz import ai_client
from rootcoz.ai_client import (
    VALID_AI_PROVIDERS,
    map_provider_model_for_sidecar,
    normalize_provider,
)


@pytest.fixture(autouse=True)
def _clear_route_cache():
    ai_client._model_route_cache.clear()
    yield
    ai_client._model_route_cache.clear()


def test_valid_providers_are_canonical_only() -> None:
    assert VALID_AI_PROVIDERS == {"claude", "cursor", "gemini"}


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("cursor-cli", "cursor"),
        ("claude-cli", "claude"),
        ("gemini-cli", "gemini"),
        ("CURSOR", "cursor"),
        ("cursor", "cursor"),
    ],
)
def test_normalize_provider(raw: str, canonical: str) -> None:
    assert normalize_provider(raw) == canonical


def test_default_sidecar_mapping() -> None:
    assert map_provider_model_for_sidecar("cursor", "")[0] == "acpx-cursor"
    assert map_provider_model_for_sidecar("claude", "")[0] == "google-vertex-claude"
    assert map_provider_model_for_sidecar("gemini", "")[0] == "google"
    assert map_provider_model_for_sidecar("cursor-cli", "")[0] == "acpx-cursor"


def test_map_from_sidecar() -> None:
    assert ai_client._friendly_provider_from_sidecar("acpx-cursor") == "cursor"
    assert ai_client._friendly_provider_from_sidecar("cli-cursor") == "cursor"
    assert ai_client._friendly_provider_from_sidecar("google-vertex-claude") == "claude"
    assert ai_client._friendly_provider_from_sidecar("cli-claude") == "claude"
    assert ai_client._friendly_provider_from_sidecar("google") == "gemini"


def test_cursor_acpx_model_routes_to_acpx() -> None:
    provider, model = map_provider_model_for_sidecar(
        "cursor", "cursor:grok-4.5[effort=high,fast=true]"
    )
    assert provider == "acpx-cursor"
    assert model == "cursor:grok-4.5[effort=high,fast=true]"


def test_cursor_cli_model_routes_to_cli() -> None:
    provider, model = map_provider_model_for_sidecar("cursor", "cursor:composer-2")
    assert provider == "cli-cursor"
    assert model == "cursor:composer-2"


def test_cursor_cli_model_adds_prefix() -> None:
    provider, model = map_provider_model_for_sidecar("cursor", "composer-2")
    # No brackets and no cursor: prefix → treated as default ACPX after prefix add…
    # Actually: model becomes cursor:composer-2 only AFTER sidecar chosen.
    # Heuristic runs on raw model "composer-2" which doesn't start with cursor:
    # and has no [, so → acpx-cursor, then prefix added.
    assert provider == "acpx-cursor"
    assert model == "cursor:composer-2"


def test_legacy_cursor_cli_provider_with_cli_model() -> None:
    provider, model = map_provider_model_for_sidecar("cursor-cli", "cursor:composer-2")
    assert provider == "cli-cursor"
    assert model == "cursor:composer-2"


def test_cache_overrides_heuristic() -> None:
    ai_client._model_route_cache[("cursor", "cursor:composer-2")] = "cli-cursor"
    provider, model = map_provider_model_for_sidecar("cursor", "cursor:composer-2")
    assert provider == "cli-cursor"
    assert model == "cursor:composer-2"


@pytest.mark.asyncio
async def test_list_models_merges_and_tags_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def get_models(self):
            return [
                {
                    "id": "cursor:default[]",
                    "name": "Default",
                    "provider": "acpx-cursor",
                },
                {
                    "id": "cursor:composer-2",
                    "name": "Composer",
                    "provider": "cli-cursor",
                },
                {
                    "id": "claude-opus-4-6",
                    "name": "Opus",
                    "provider": "google-vertex-claude",
                },
            ]

    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: FakeClient())
    models = await ai_client.list_models("cursor")
    assert len(models) == 2
    by_id = {m["id"]: m for m in models}
    assert by_id["cursor:default[]"]["source"] == "acpx"
    assert by_id["cursor:default[]"]["provider"] == "cursor"
    assert by_id["cursor:composer-2"]["source"] == "cli"
    assert ai_client._model_route_cache[("cursor", "cursor:composer-2")] == "cli-cursor"


@pytest.mark.asyncio
async def test_build_friendly_catalog_single_pass() -> None:
    """One sidecar catalog builds all friendly providers without re-fetching."""
    catalog = [
        {
            "id": "cursor:default[]",
            "name": "Default",
            "provider": "acpx-cursor",
        },
        {
            "id": "claude-opus-4-6",
            "name": "Opus",
            "provider": "google-vertex-claude",
        },
        {
            "id": "gemini-2.5-pro",
            "name": "Gemini",
            "provider": "google",
        },
    ]
    result = ai_client.build_friendly_catalog(catalog)
    assert set(result) == VALID_AI_PROVIDERS
    assert [m["id"] for m in result["cursor"]] == ["cursor:default[]"]
    assert [m["id"] for m in result["claude"]] == ["claude-opus-4-6"]
    assert [m["id"] for m in result["gemini"]] == ["gemini-2.5-pro"]
    assert ai_client._model_route_cache[("cursor", "cursor:default[]")] == "acpx-cursor"


@pytest.mark.asyncio
async def test_list_models_route_cache_first_source_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate model ids keep the first sidecar route (ACPX before CLI)."""

    class FakeClient:
        async def get_models(self):
            return [
                {
                    "id": "cursor:shared",
                    "name": "Shared ACPX",
                    "provider": "acpx-cursor",
                },
                {
                    "id": "cursor:shared",
                    "name": "Shared CLI",
                    "provider": "cli-cursor",
                },
            ]

    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: FakeClient())
    models = await ai_client.list_models("cursor")
    assert len(models) == 1
    assert models[0]["source"] == "acpx"
    assert ai_client._model_route_cache[("cursor", "cursor:shared")] == "acpx-cursor"


def test_format_chat_ai_user_error_session_url_not_expired() -> None:
    msg = ai_client.format_chat_ai_user_error(
        "Client error '400 Bad Request' for url 'http://127.0.0.1:9100/sessions'",
        is_admin=True,
    )
    assert "session expired" not in msg.lower()
    assert "provider/model" in msg.lower() or "cursor" in msg.lower()


def test_format_chat_ai_user_error_true_session_not_found() -> None:
    msg = ai_client.format_chat_ai_user_error("Session xyz not found")
    assert "session expired" in msg.lower()


def test_format_chat_ai_user_error_auth_admin() -> None:
    msg = ai_client.format_chat_ai_user_error(
        "Error: Authentication required. Please run 'agent login' first",
        is_admin=True,
    )
    assert "CURSOR_API_KEY" in msg
    assert "does not expire" in msg or "agent login" in msg.lower()


def test_format_chat_ai_user_error_auth_non_admin_no_key_leak() -> None:
    msg = ai_client.format_chat_ai_user_error(
        "Error: Authentication required. Please run 'agent login' first",
        is_admin=False,
    )
    assert "CURSOR_API_KEY" not in msg
    assert "administrator" in msg.lower()


def test_parse_agent_status_auth_expired() -> None:
    assert (
        ai_client._parse_agent_status_text("Not logged in. Run agent login.")
        == "auth_expired"
    )
    assert ai_client._parse_agent_status_text("Logged in as user@example.com") is None


@pytest.mark.asyncio
async def test_probe_cursor_auth_ok_when_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_client.clear_cursor_auth_cache()

    async def fake_list(_provider: str = ""):
        return [{"id": "cursor:default[]", "provider": "cursor", "source": "acpx"}]

    monkeypatch.setattr(ai_client, "list_models", fake_list)
    status = await ai_client.probe_cursor_auth(force=True)
    assert status["ok"] is True
    assert status["model_count"] == 1


@pytest.mark.asyncio
async def test_probe_cursor_auth_uses_fresh_model_count_over_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When callers pass model_count, do not return a stale cached probe."""
    ai_client.clear_cursor_auth_cache()
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    async def empty_list(_provider: str = ""):
        return []

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"Not logged in\n", b""

        def kill(self):
            return None

        async def wait(self):
            return 1

    async def fake_exec(*_a, **_k):
        return FakeProc()

    monkeypatch.setattr(ai_client, "list_models", empty_list)
    monkeypatch.setattr(ai_client.asyncio, "create_subprocess_exec", fake_exec)
    stale = await ai_client.probe_cursor_auth(force=True)
    assert stale["ok"] is False
    assert stale["model_count"] == 0

    # Fresh catalog from /api/ai-models: pass count without force — must refresh.
    fresh = await ai_client.probe_cursor_auth(model_count=3)
    assert fresh["ok"] is True
    assert fresh["model_count"] == 3


@pytest.mark.asyncio
async def test_probe_cursor_auth_expired_when_empty_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_client.clear_cursor_auth_cache()
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    async def fake_list(_provider: str = ""):
        return []

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"Not logged in\n", b""

        def kill(self):
            return None

        async def wait(self):
            return 1

    async def fake_exec(*_a, **_k):
        return FakeProc()

    monkeypatch.setattr(ai_client, "list_models", fake_list)
    monkeypatch.setattr(ai_client.asyncio, "create_subprocess_exec", fake_exec)
    status = await ai_client.probe_cursor_auth(force=True)
    assert status["ok"] is False
    assert status["reason"] == "auth_expired"
    assert status["has_api_key"] is False
    assert "does not expire" in status["hint"]


@pytest.mark.asyncio
async def test_probe_cursor_auth_key_set_never_auth_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CURSOR_API_KEY does not expire — never label auth_expired when key is set."""
    ai_client.clear_cursor_auth_cache()
    monkeypatch.setenv("CURSOR_API_KEY", "test-key-not-real")

    async def fake_list(_provider: str = ""):
        return []

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"Not logged in\n", b""

        def kill(self):
            return None

        async def wait(self):
            return 1

    async def fake_exec(*_a, **_k):
        return FakeProc()

    monkeypatch.setattr(ai_client, "list_models", fake_list)
    monkeypatch.setattr(ai_client.asyncio, "create_subprocess_exec", fake_exec)
    status = await ai_client.probe_cursor_auth(force=True)
    assert status["ok"] is False
    assert status["reason"] == "api_key_not_applied"
    assert status["has_api_key"] is True
    assert "does not expire" in status["hint"]


@pytest.mark.asyncio
async def test_prewarm_model_routes_swallows_catalog_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog prewarm errors must not raise (AI call can still proceed)."""

    async def boom(_provider: str = "") -> list:
        raise RuntimeError("sidecar catalog unavailable")

    monkeypatch.setattr(ai_client, "list_models", boom)
    # Should not raise
    await ai_client._prewarm_model_routes("cursor")
