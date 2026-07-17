"""Tests for rootcoz.ai_client provider mapping and model listing."""

from __future__ import annotations

import pytest

from rootcoz import ai_client
from rootcoz.ai_client import (
    VALID_AI_PROVIDERS,
    map_provider_for_sidecar,
    map_provider_from_sidecar,
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
    assert map_provider_for_sidecar("cursor") == "acpx-cursor"
    assert map_provider_for_sidecar("claude") == "google-vertex-claude"
    assert map_provider_for_sidecar("gemini") == "google"
    assert map_provider_for_sidecar("cursor-cli") == "acpx-cursor"


def test_map_from_sidecar() -> None:
    assert map_provider_from_sidecar("acpx-cursor") == "cursor"
    assert map_provider_from_sidecar("cli-cursor") == "cursor"
    assert map_provider_from_sidecar("google-vertex-claude") == "claude"
    assert map_provider_from_sidecar("cli-claude") == "claude"
    assert map_provider_from_sidecar("google") == "gemini"


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
