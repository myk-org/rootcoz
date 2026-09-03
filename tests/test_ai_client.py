"""Tests for rootcoz.ai_client catalog provider handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from rootcoz import ai_client
from rootcoz.ai_client import call_ai as call_ai_under_test
from rootcoz.ai_client import normalize_provider


@pytest.fixture(autouse=True)
def _clear_model_catalog() -> None:
    ai_client.update_model_catalog(None)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("cursor-cli", "cli-cursor"),
        ("claude-cli", "cli-claude"),
        ("gemini-cli", "cli-gemini"),
        (" OPENAI ", "openai"),
        ("cli-cursor", "cli-cursor"),
    ],
)
def test_normalize_provider(raw: str, canonical: str) -> None:
    assert normalize_provider(raw) == canonical


def test_build_catalog_groups_duplicate_model_ids_by_exact_provider() -> None:
    catalog = [
        {"id": "shared-model", "name": "OpenAI", "provider": "openai"},
        {"id": "shared-model", "name": "Cursor", "provider": "cli-cursor"},
    ]

    assert ai_client.build_friendly_catalog(catalog) == {
        "openai": [
            {
                "id": "shared-model",
                "name": "OpenAI",
                "provider": "openai",
                "source": "api",
            }
        ],
        "cli-cursor": [
            {
                "id": "shared-model",
                "name": "Cursor",
                "provider": "cli-cursor",
                "source": "cli",
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model"),
    [("openai", "gpt-5.4"), ("cli-cursor", "cursor:cursor-grok-4.6-high")],
)
async def test_call_ai_passes_exact_catalog_pair_to_sidecar(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str
) -> None:
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": provider, "id": model}]),
    )
    call = AsyncMock(return_value="result")
    monkeypatch.setattr(ai_client, "_call_ai", call)

    assert (
        await call_ai_under_test("prompt", ai_provider=provider, ai_model=model)
        == "result"
    )
    call.assert_awaited_once_with("prompt", ai_provider=provider, ai_model=model)


@pytest.mark.asyncio
async def test_resolve_catalog_pair_maps_unambiguous_legacy_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_client.update_model_catalog(None)
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": "google", "id": "gemini-2.5"}]),
    )

    assert await ai_client.resolve_catalog_pair("gemini", "gemini-2.5") == (
        "google",
        "gemini-2.5",
    )


@pytest.mark.asyncio
async def test_resolve_catalog_pair_rejects_ambiguous_legacy_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_client.update_model_catalog(None)
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(
            return_value=[
                {"provider": "google", "id": "gemini-2.5"},
                {"provider": "google-vertex", "id": "gemini-2.5"},
            ]
        ),
    )

    with pytest.raises(ValueError, match="Unknown Pi-sidecar provider/model pair"):
        await ai_client.resolve_catalog_pair("gemini", "gemini-2.5")


@pytest.mark.asyncio
async def test_resolve_catalog_pair_uses_warm_catalog_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_client.update_model_catalog([{"provider": "openai", "id": "gpt-5"}])
    fetch = AsyncMock(side_effect=RuntimeError("temporary sidecar failure"))
    monkeypatch.setattr(ai_client, "_list_models_raw", fetch)

    assert await ai_client.resolve_catalog_pair("openai", "gpt-5") == (
        "openai",
        "gpt-5",
    )
    assert fetch.await_count == 0


@pytest.mark.asyncio
async def test_resolve_catalog_pair_propagates_refresh_failure_for_uncached_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_client.update_model_catalog([{"provider": "openai", "id": "gpt-5"}])
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(side_effect=RuntimeError("temporary sidecar failure")),
    )

    with pytest.raises(RuntimeError, match="temporary sidecar failure"):
        await ai_client.resolve_catalog_pair("openai", "gpt-5.4")


@pytest.mark.asyncio
async def test_admin_catalog_update_wins_over_inflight_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def discover(_provider: str) -> list[dict[str, str]]:
        started.set()
        await release.wait()
        return [{"provider": "openai", "id": "stale"}]

    monkeypatch.setattr(ai_client, "_list_models_raw", discover)
    discovery = asyncio.create_task(ai_client._get_model_catalog(refresh=True))
    await started.wait()
    ai_client.update_model_catalog([{"provider": "openai", "id": "fresh"}])
    release.set()

    assert await discovery == [{"provider": "openai", "id": "fresh"}]
    assert await ai_client._get_model_catalog() == [
        {"provider": "openai", "id": "fresh"}
    ]


@pytest.mark.asyncio
async def test_resolve_catalog_pair_rejects_model_from_another_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(
            return_value=[
                {"provider": "openai", "id": "shared-model"},
                {"provider": "cli-cursor", "id": "shared-model"},
            ]
        ),
    )

    assert await ai_client.resolve_catalog_pair("openai", "shared-model") == (
        "openai",
        "shared-model",
    )
    with pytest.raises(ValueError, match="Unknown Pi-sidecar provider/model pair"):
        await ai_client.resolve_catalog_pair("openai", "cursor-only-model")


def test_format_chat_ai_user_error_session_url_not_expired() -> None:
    msg = ai_client.format_chat_ai_user_error(
        "Client error '400 Bad Request' for url 'http://127.0.0.1:9100/sessions'",
        is_admin=True,
        ai_provider="cursor",
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
        ai_provider="cursor",
    )
    assert "CURSOR_API_KEY" in msg
    assert "does not expire" in msg or "agent login" in msg.lower()


def test_format_chat_ai_user_error_auth_non_admin_no_key_leak() -> None:
    msg = ai_client.format_chat_ai_user_error(
        "Error: Authentication required. Please run 'agent login' first",
        is_admin=False,
        ai_provider="cursor",
    )
    assert "CURSOR_API_KEY" not in msg
    assert "administrator" in msg.lower()


def test_format_chat_ai_user_error_generic_auth_not_cursor() -> None:
    msg = ai_client.format_chat_ai_user_error(
        "Error: Authentication required",
        is_admin=True,
        ai_provider="claude",
    )
    assert "CURSOR_API_KEY" not in msg
    assert "claude" in msg.lower()
    assert "cursor is unavailable" not in msg.lower()


def test_format_chat_ai_user_error_generic_auth_non_admin_claude() -> None:
    msg = ai_client.format_chat_ai_user_error(
        "Error: not authenticated",
        is_admin=False,
        ai_provider="gemini",
    )
    assert "CURSOR_API_KEY" not in msg
    assert "gemini" in msg.lower()
    assert "cursor" not in msg.lower()


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
