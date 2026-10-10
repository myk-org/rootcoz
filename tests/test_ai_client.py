"""Tests for rootcoz.ai_client catalog provider handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from rootcoz import ai_client, storage
from rootcoz.ai_client import AIResult, normalize_provider
from rootcoz.ai_client import call_ai as call_ai_under_test


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


@pytest.mark.asyncio
async def test_provider_discovery_transport_failure_denies_key_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.get_providers.side_effect = httpx.ConnectError("secret from sidecar")
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)

    assert await ai_client.supported_key_providers() == []


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
    monkeypatch: pytest.MonkeyPatch, tmp_path, provider: str, model: str
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": provider, "id": model}]),
    )
    result = AIResult(success=True, text="result")
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(ai_client, "_call_ai", call)
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    token = ai_client.ai_username.set("admin")
    force_token = ai_client.force_server_credentials.set(True)
    try:
        assert (
            await call_ai_under_test("prompt", ai_provider=provider, ai_model=model)
            is result
        )
    finally:
        ai_client.force_server_credentials.reset(force_token)
        ai_client.ai_username.reset(token)
    assert result.credential_source == "server"
    call.assert_awaited_once_with("prompt", ai_provider=provider, ai_model=model)


@pytest.mark.asyncio
async def test_bootstrap_admin_server_session_is_saved(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    ai_client.update_model_catalog([{"provider": "openai", "id": "model"}])
    token = ai_client.ai_username.set("admin")
    call = AsyncMock(
        return_value=AIResult(success=True, text="reply", session_id="new-session")
    )
    monkeypatch.setattr(ai_client, "_call_ai", call)
    try:
        result = await call_ai_under_test(
            "prompt", ai_provider="openai", ai_model="model"
        )
    finally:
        ai_client.ai_username.reset(token)
    assert result.success and result.text == "reply"
    assert result.credential_source == "server"
    assert (
        await storage.get_ai_session_source("new-session", "admin", "openai")
        == "server"
    )


@pytest.mark.asyncio
async def test_missing_normal_user_cannot_create_server_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    ai_client.update_model_catalog([{"provider": "openai", "id": "model"}])
    token = ai_client.ai_username.set("missing")
    call = AsyncMock(
        return_value=AIResult(success=True, text="reply", session_id="new-session")
    )
    monkeypatch.setattr(ai_client, "_call_ai", call)
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    try:
        with pytest.raises(LookupError, match="account changed"):
            await call_ai_under_test("prompt", ai_provider="openai", ai_model="model")
    finally:
        ai_client.ai_username.reset(token)
    call.assert_not_awaited()
    assert (
        await storage.get_ai_session_source("new-session", "missing", "openai")
        == "unknown"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True])
async def test_server_session_discarded_when_account_changes_during_sidecar_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch, recreate: bool
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    ai_client.update_model_catalog([{"provider": "openai", "id": "model"}])
    token = ai_client.ai_username.set("alice")
    delete = AsyncMock()
    monkeypatch.setattr(
        ai_client, "get_sidecar_client", lambda: AsyncMock(delete_session=delete)
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())

    async def sidecar_call(*args: object, **kwargs: object) -> AIResult:
        await storage.delete_user("alice")
        if recreate:
            await storage.create_admin_user("alice")
        return AIResult(success=True, text="reply", session_id="new-session")

    monkeypatch.setattr(ai_client, "_call_ai", sidecar_call)
    try:
        with pytest.raises(LookupError, match="account changed"):
            await call_ai_under_test("prompt", ai_provider="openai", ai_model="model")
    finally:
        ai_client.ai_username.reset(token)
    delete.assert_awaited_once_with("new-session")
    assert (
        await storage.get_ai_session_source("new-session", "alice", "openai")
        == "unknown"
    )


@pytest.mark.asyncio
async def test_server_session_collision_does_not_delete_existing_sidecar_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("existing", "alice", "openai", "server")
    ai_client.update_model_catalog([{"provider": "openai", "id": "model"}])
    token = ai_client.ai_username.set("alice")
    delete = AsyncMock()
    monkeypatch.setattr(
        ai_client, "get_sidecar_client", lambda: AsyncMock(delete_session=delete)
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(
        ai_client,
        "_call_ai",
        AsyncMock(
            return_value=AIResult(success=True, text="reply", session_id="existing")
        ),
    )
    try:
        with pytest.raises(ValueError, match="already active"):
            await call_ai_under_test("prompt", ai_provider="openai", ai_model="model")
    finally:
        ai_client.ai_username.reset(token)
    delete.assert_not_awaited()
    assert (
        await storage.get_ai_session_source("existing", "alice", "openai") == "server"
    )


@pytest.mark.asyncio
async def test_session_ownership_survives_provider_case_drift(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session stored under one provider capitalization stays usable after the
    catalog refreshes with another — ownership compares provider IDs
    case-insensitively, while a genuinely different provider still rejects."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "case-drift.db")
    await storage.init_db()
    await storage.save_ai_session_source(
        "drift-session", "admin", "EnMaaS", "server", bootstrap_admin=True
    )
    # Catalog drifted to lowercase — the session must remain usable.
    assert (
        await storage.get_ai_session_source("drift-session", "admin", "enmaas")
        == "server"
    )
    # A different provider is still rejected.
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("drift-session", "admin", "other")
    # A different user is still rejected.
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("drift-session", "mallory", "EnMaaS")


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

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
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

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
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
async def test_admin_empty_catalog_update_wins_over_inflight_discovery(
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
    ai_client.update_model_catalog([])
    release.set()

    assert await discovery == []
    assert await ai_client._get_model_catalog() == []


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

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    assert await ai_client.resolve_catalog_pair("openai", "shared-model") == (
        "openai",
        "shared-model",
    )
    with pytest.raises(ValueError, match="Unknown Pi-sidecar provider/model pair"):
        await ai_client.resolve_catalog_pair("openai", "cursor-only-model")


@pytest.mark.asyncio
async def test_resolve_catalog_pair_adopts_mixed_case_catalog_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom agent-dir provider registered with mixed case resolves from any
    input spelling and returns the catalog's exact ID for POST /sessions."""
    ai_client.update_model_catalog(None)
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": "EnMaaS", "id": "enmaas/gpt-4o"}]),
    )

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    for requested in ("enmaas", "EnMaaS", "ENMAAS"):
        assert await ai_client.resolve_catalog_pair(requested, "enmaas/gpt-4o") == (
            "EnMaaS",
            "enmaas/gpt-4o",
        )


@pytest.mark.asyncio
async def test_resolve_catalog_pair_mixed_case_alias_still_maps_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case-insensitive spelling adoption must not break legacy alias mapping."""
    ai_client.update_model_catalog(None)
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(return_value=[{"provider": "google", "id": "gemini-2.5"}]),
    )

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    assert await ai_client.resolve_catalog_pair("GEMINI", "gemini-2.5") == (
        "google",
        "gemini-2.5",
    )


@pytest.mark.asyncio
async def test_resolve_catalog_pair_refresh_reevaluates_mixed_case_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model missing from the cached catalog resolves after refresh, even when
    the spelling adopted from the cache differs from the refreshed catalog's.

    The cached catalog registers ``EnMaaS`` without the new model; the refresh
    spells the provider ``enmaas`` and carries the model. Spelling adoption must
    compare normalized forms on every call — including after refresh — or the
    pair check rejects the valid pair.
    """
    ai_client.update_model_catalog([{"provider": "EnMaaS", "id": "enmaas/gpt-4o"}])
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(
            return_value=[
                {"provider": "enmaas", "id": "enmaas/gpt-4o"},
                {"provider": "enmaas", "id": "enmaas/new-model"},
            ]
        ),
    )

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    assert await ai_client.resolve_catalog_pair("enmaas", "enmaas/new-model") == (
        "enmaas",
        "enmaas/new-model",
    )


@pytest.mark.asyncio
async def test_resolve_catalog_pair_refresh_finds_new_model_same_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model missing from the cached catalog resolves after refresh with the
    same mixed-case spelling in both catalogs."""
    ai_client.update_model_catalog([{"provider": "EnMaaS", "id": "enmaas/gpt-4o"}])
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(
            return_value=[
                {"provider": "EnMaaS", "id": "enmaas/gpt-4o"},
                {"provider": "EnMaaS", "id": "enmaas/new-model"},
            ]
        ),
    )

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    assert await ai_client.resolve_catalog_pair("enmaas", "enmaas/new-model") == (
        "EnMaaS",
        "enmaas/new-model",
    )


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


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_prompt_safely_preserves_partial_cost_flag() -> None:
    """The user-key prompt path must not drop pi-sidecar's cost_partial flag.

    Dropping it stores a lower-bound cost as a complete one, so the report loses
    its partial warning.
    """
    client = AsyncMock()
    client._client.post = AsyncMock(
        return_value=_FakeResponse(
            {
                "text": "answer",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.02,
                    "cost_partial": True,
                    "duration_ms": 100,
                },
            }
        )
    )
    result = await ai_client._prompt_safely(client, "s-1", "hi", None)
    assert result.success is True
    assert result.usage is not None
    assert result.usage.cost_partial is True
    assert result.usage.cost_usd == 0.02


@pytest.mark.asyncio
async def test_prompt_safely_defaults_partial_flag_to_false() -> None:
    """A response without the flag is a complete cost, not a partial one."""
    client = AsyncMock()
    client._client.post = AsyncMock(
        return_value=_FakeResponse(
            {
                "text": "answer",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.02,
                    "duration_ms": 100,
                },
            }
        )
    )
    result = await ai_client._prompt_safely(client, "s-1", "hi", None)
    assert result.usage is not None
    assert result.usage.cost_partial is False
