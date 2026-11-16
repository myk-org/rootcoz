"""Tests for rootcoz.ai_client catalog provider handling."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest

from rootcoz import ai_client, storage
from rootcoz.ai_client import AIResult, normalize_provider

# Import-time capture, before conftest's autouse fixture rebinds the module
# attribute to its deny mock: these tests must exercise the real internal call.
from rootcoz.ai_client import _call_ai as real_internal_call_ai
from rootcoz.ai_client import call_ai as call_ai_under_test
from rootcoz.ai_client import call_ai_once as real_call_ai_once
from tests.conftest import capture_main_logs


@pytest.fixture(autouse=True)
def _clear_model_catalog() -> None:
    ai_client.update_model_catalog(None)


@pytest.fixture(autouse=True)
def _no_host_deployment_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No host env/manifest key reaches a session apiKey behind a test (#376).

    ``server_provider_key`` falls back to the deployment credential, so a
    developer's own ``OPENAI_API_KEY``/models.json would change what these
    tests assert about the session kwargs. CI has neither; the fallback itself
    is covered in ``test_server_provider_keys.py``.
    """
    monkeypatch.setattr(ai_client, "deployment_provider_credentials", lambda: None)


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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # The sidecar's explicit status suffix is the only textual status.
        ("Sidecar API key model discovery failed (HTTP 401)", 401),
        ("Sidecar API key model discovery failed (HTTP 403)", 403),
        # A bare word or number is not evidence: unrelated failures keep None.
        ("model rejected by provider", None),
        ("provider request 401 timed out", None),
        ("upstream returned 4013 for this model", None),
        # Unambiguous auth phrases, with no status attached.
        ("invalid api key", 401),
        ("API key is invalid", 401),
        ("Incorrect API key provided", 401),
        ("Unauthorized", 401),
        ("Forbidden", 403),
        ("authentication failed", 401),
    ],
)
def test_auth_failure_status_reads_structured_evidence_only(
    message: str, expected: int | None
) -> None:
    assert ai_client.auth_failure_status(RuntimeError(message)) == expected


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_status_reads_an_http_status_error(status: int) -> None:
    request = httpx.Request("GET", "http://sidecar.test/providers")
    error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )

    assert ai_client.auth_failure_status(error) == status


def test_auth_failure_status_ignores_a_non_auth_status_error() -> None:
    request = httpx.Request("GET", "http://sidecar.test/providers")
    error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(500, request=request)
    )

    assert ai_client.auth_failure_status(error) is None


def test_auth_failure_status_walks_the_exception_chain() -> None:
    request = httpx.Request("GET", "http://sidecar.test/providers")
    rejection = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(401, request=request)
    )
    wrapped = RuntimeError("model discovery failed")
    wrapped.__cause__ = rejection

    assert ai_client.auth_failure_status(wrapped) == 401


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
    try:
        assert (
            await call_ai_under_test("prompt", ai_provider=provider, ai_model=model)
            is result
        )
    finally:
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
        "drift-session", "admin", "MyGateway", "server", bootstrap_admin=True
    )
    # Catalog drifted to lowercase — the session must remain usable.
    assert (
        await storage.get_ai_session_source("drift-session", "admin", "mygateway")
        == "server"
    )
    # A different provider is still rejected.
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("drift-session", "admin", "other")
    # A different user is still rejected.
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("drift-session", "mallory", "MyGateway")


@pytest.mark.asyncio
async def test_key_rotation_revokes_session_across_provider_case_drift(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing a key under a drifted (re-capitalized) provider spelling must
    revoke sessions stored under the old spelling — the sidecar retains the
    creation-time key, so the old session must never resume."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "rotation-drift.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    # Session created while the catalog spelled the provider "MyGateway".
    await storage.update_user_ai_credential("alice", "MyGateway", "old-key")
    await storage.save_ai_session_source(
        "drifted-session", "alice", "MyGateway", "user"
    )
    assert (
        await storage.get_ai_session_source("drifted-session", "alice", "MyGateway")
        == "user"
    )
    # Catalog drifts to lowercase; the user replaces the key under the new
    # spelling. The old-spelling session must be revoked, not resumable.
    assert await storage.update_user_ai_credential(
        "alice", "mygateway", "new-key", registered_providers={"mygateway"}
    ) == ["drifted-session"]
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("drifted-session", "alice", "mygateway")
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("drifted-session", "alice", "MyGateway")


@pytest.mark.asyncio
async def test_new_session_valid_after_provider_case_drift_rotation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh session created under the current spelling stays valid even
    though the provider previously reached higher generations under a former
    spelling — the exact-key generation check must not compare against
    obsolete case variants."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "new-session-drift.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    # Two rotations under the old spelling: generation 2.
    await storage.update_user_ai_credential("alice", "MyGateway", "key-1")
    await storage.update_user_ai_credential("alice", "MyGateway", "key-2")
    await storage.save_ai_session_source("old-session", "alice", "MyGateway", "user")
    # Catalog drifts; the key is replaced under the new spelling, consolidating
    # the old spelling's counter history (generation 3).
    assert await storage.update_user_ai_credential(
        "alice", "mygateway", "key-3", registered_providers={"mygateway"}
    ) == ["old-session"]
    # A NEW session created with the current credential must verify.
    await storage.save_ai_session_source("new-session", "alice", "mygateway", "user")
    assert (
        await storage.get_ai_session_source("new-session", "alice", "mygateway")
        == "user"
    )
    # The pre-drift session stays revoked.
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("old-session", "alice", "mygateway")


@pytest.mark.asyncio
async def test_non_ascii_provider_case_drift_revokes_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider matching must use one Unicode-consistent rule: SQLite
    COLLATE NOCASE folds ASCII only, so a non-ASCII drift (ÄGateway → ägateway)
    must still revoke the old session and accept a new one."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "nonascii-drift.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "ÄGateway", "old-key")
    await storage.save_ai_session_source("unicode-session", "alice", "ÄGateway", "user")
    assert (
        await storage.get_ai_session_source("unicode-session", "alice", "ÄGateway")
        == "user"
    )
    assert await storage.update_user_ai_credential(
        "alice", "ägateway", "new-key", registered_providers={"ägateway"}
    ) == ["unicode-session"]
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("unicode-session", "alice", "ägateway")
    await storage.save_ai_session_source("unicode-new", "alice", "ägateway", "user")
    assert (
        await storage.get_ai_session_source("unicode-new", "alice", "ägateway")
        == "user"
    )


@pytest.mark.asyncio
async def test_case_distinct_providers_not_conflated(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When case-distinct provider IDs coexist as separate credentials,
    rotating one must not revoke the other's sessions."""
    import aiosqlite

    from rootcoz.storage import encrypt_value

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "case-distinct.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    # Seed both spellings as separate credentials directly — the public API
    # consolidates a lone case variant, so the coexistence state is reachable
    # only out-of-band (this exercises the ambiguity guard).
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute(
            "UPDATE users SET ai_credentials_enc = ?, ai_credential_generations = ? "
            "WHERE username = ?",
            (
                encrypt_value(json.dumps({"Foo": "foo-key", "foo": "foo-lower-key"})),
                json.dumps({"Foo": 1, "foo": 1}),
                "alice",
            ),
        )
        await db.commit()
    await storage.save_ai_session_source("upper-session", "alice", "Foo", "user")
    await storage.save_ai_session_source("lower-session", "alice", "foo", "user")
    # Rotate the lowercase spelling.
    assert await storage.update_user_ai_credential("alice", "foo", "new-lower-key") == [
        "lower-session"
    ]
    # The lowercase session is revoked...
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("lower-session", "alice", "foo")
    # ...but the distinct uppercase provider's session is untouched.
    assert (
        await storage.get_ai_session_source("upper-session", "alice", "Foo") == "user"
    )


@pytest.mark.asyncio
async def test_registered_providers_prevent_variant_consolidation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the sidecar registers both spellings as distinct provider IDs,
    adding a key for one must not absorb the other's credential."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "registered-distinct.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential(
        "alice", "Foo", "foo-key", registered_providers={"Foo"}
    )
    # Both spellings registered as distinct providers: credentials stay separate.
    await storage.update_user_ai_credential(
        "alice", "foo", "foo-lower-key", registered_providers={"Foo", "foo"}
    )
    assert await storage.get_user_ai_credentials("alice") == {
        "Foo": "foo-key",
        "foo": "foo-lower-key",
    }
    await storage.save_ai_session_source("upper-session", "alice", "Foo", "user")
    await storage.save_ai_session_source("lower-session", "alice", "foo", "user")
    assert await storage.update_user_ai_credential(
        "alice", "foo", "new-lower-key", registered_providers={"Foo", "foo"}
    ) == ["lower-session"]
    with pytest.raises(ValueError, match="another user or provider"):
        await storage.get_ai_session_source("lower-session", "alice", "foo")
    assert (
        await storage.get_ai_session_source("upper-session", "alice", "Foo") == "user"
    )


@pytest.mark.asyncio
async def test_unregistered_variant_consolidates(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case-variant whose spelling is no longer registered is a drifted
    spelling of the same provider and consolidates into the requested one."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "registered-drift.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential(
        "alice", "MyGateway", "old-key", registered_providers={"MyGateway"}
    )
    # Catalog drifted: only the lowercase spelling is registered now.
    await storage.update_user_ai_credential(
        "alice", "mygateway", "new-key", registered_providers={"mygateway"}
    )
    assert await storage.get_user_ai_credentials("alice") == {"mygateway": "new-key"}


@pytest.mark.asyncio
async def test_unknown_registration_keeps_variants_separate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When registration cannot be confirmed (sidecar unreachable, None), a
    case-variant must NOT be treated as unregistered — no consolidation, and
    rotation touches only the exact spelling."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "unknown-registration.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential(
        "alice", "MyGateway", "old-key", registered_providers={"MyGateway"}
    )
    await storage.save_ai_session_source("old-session", "alice", "MyGateway", "user")
    # Registration unknown: the spellings stay separate, nothing is absorbed.
    await storage.update_user_ai_credential("alice", "mygateway", "new-key")
    assert await storage.get_user_ai_credentials("alice") == {
        "MyGateway": "old-key",
        "mygateway": "new-key",
    }
    # Rotation under the new spelling leaves the old spelling's credential
    # and session intact — each spelling behaves as its own provider.
    assert (
        await storage.get_ai_session_source("old-session", "alice", "MyGateway")
        == "user"
    )


@pytest.mark.asyncio
async def test_registered_provider_ids_uses_provider_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered IDs come from the sidecar provider registry, not the model
    catalog — a registered provider with no current models still counts."""

    class FakeClient:
        async def get_providers(self):
            return [
                {"provider": "Foo", "supportsSessionApiKey": True},
                {"provider": "foo", "supportsSessionApiKey": False},
            ]

    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: FakeClient())
    assert await ai_client.registered_provider_ids() == {"Foo", "foo"}


@pytest.mark.asyncio
async def test_registered_provider_ids_unavailable_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def get_providers(self):
            raise RuntimeError("sidecar down")

    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: FakeClient())
    assert await ai_client.registered_provider_ids() is None


@pytest.mark.asyncio
async def test_registered_provider_ids_malformed_entry_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any malformed entry makes registration unknown, never a filtered subset.

    A partial set reads an omitted provider as unregistered, and credential
    rotation would then delete its stored key and drop its sessions.
    """

    class FakeClient:
        async def get_providers(self):
            return [
                None,
                "not-a-mapping",
                {"provider": ""},
                {"provider": 7},
                "also-not-a-mapping",
                {"provider": "Foo"},
            ]

    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: FakeClient())
    assert await ai_client.registered_provider_ids() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["providers", {"provider": "Foo"}, None, 3])
async def test_registered_provider_ids_non_list_payload_returns_none(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """A payload that is not a list is unknown registration, never a set."""

    class FakeClient:
        async def get_providers(self):
            return payload

    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: FakeClient())
    assert await ai_client.registered_provider_ids() is None


@pytest.mark.asyncio
async def test_revocation_discovery_indexes_exist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation discovery runs inside a write transaction — its lookup
    predicates must be indexed so unrelated history is not table-scanned."""
    import aiosqlite

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "revocation-indexes.db")
    await storage.init_db()
    async with aiosqlite.connect(storage.DB_PATH) as db:
        rows = await (
            await db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_chat_messages_user_provider_session" in names
    assert "idx_ai_session_sources_user" in names


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
        AsyncMock(return_value=[{"provider": "MyGateway", "id": "mygateway/gpt-4o"}]),
    )

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    for requested in ("mygateway", "MyGateway", "MYGATEWAY"):
        assert await ai_client.resolve_catalog_pair(requested, "mygateway/gpt-4o") == (
            "MyGateway",
            "mygateway/gpt-4o",
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

    The cached catalog registers ``MyGateway`` without the new model; the refresh
    spells the provider ``mygateway`` and carries the model. Spelling adoption must
    compare normalized forms on every call — including after refresh — or the
    pair check rejects the valid pair.
    """
    ai_client.update_model_catalog(
        [{"provider": "MyGateway", "id": "mygateway/gpt-4o"}]
    )
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(
            return_value=[
                {"provider": "mygateway", "id": "mygateway/gpt-4o"},
                {"provider": "mygateway", "id": "mygateway/new-model"},
            ]
        ),
    )

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    assert await ai_client.resolve_catalog_pair("mygateway", "mygateway/new-model") == (
        "mygateway",
        "mygateway/new-model",
    )


@pytest.mark.asyncio
async def test_resolve_catalog_pair_refresh_finds_new_model_same_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model missing from the cached catalog resolves after refresh with the
    same mixed-case spelling in both catalogs."""
    ai_client.update_model_catalog(
        [{"provider": "MyGateway", "id": "mygateway/gpt-4o"}]
    )
    monkeypatch.setattr(
        ai_client,
        "_list_models_raw",
        AsyncMock(
            return_value=[
                {"provider": "MyGateway", "id": "mygateway/gpt-4o"},
                {"provider": "MyGateway", "id": "mygateway/new-model"},
            ]
        ),
    )

    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    assert await ai_client.resolve_catalog_pair("mygateway", "mygateway/new-model") == (
        "MyGateway",
        "mygateway/new-model",
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


# ---------------------------------------------------------------------------
# #569: one turn-usability owner, server-credential diagnostics, session
# generation tracking. Key-shaped tokens are assembled at runtime so gitleaks
# and prek stay clean.
# ---------------------------------------------------------------------------


def _runtime_openai_key() -> str:
    return "sk-proj-" + "a1b2c3d4" * 4


def _runtime_google_key() -> str:
    return "AIza" + "SyA1B2C3" * 4


def _make_sidecar(handler) -> tuple[Any, list[httpx.Request]]:
    """A SidecarClient bound to ``handler`` plus the list of requests it saw."""
    from pi_sidecar_client import SidecarClient

    requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = SidecarClient(base_url="http://sidecar.invalid")
    client._client = httpx.AsyncClient(
        base_url="http://sidecar.invalid", transport=httpx.MockTransport(_handle)
    )
    return client, requests


def _bind_internal_call_ai(monkeypatch: pytest.MonkeyPatch, client: Any) -> Any:
    """Run the real internal ``_call_ai`` (conftest patches it to deny)."""
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "_call_ai", real_internal_call_ai)
    return real_internal_call_ai


async def _internal_call(client, monkeypatch, prompt="hello", **kwargs):
    real_internal_call_ai = _bind_internal_call_ai(monkeypatch, client)
    return await real_internal_call_ai(
        prompt, ai_provider="openai", ai_model="model", **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "accept_turn", "expected_success"),
    [
        # An explicit normal stop proves the turn finished.
        ("stop", None, True),
        ("end_turn", None, True),
        # No stop reason: only the caller's schema check can accept the turn.
        (None, lambda _text: True, True),
        (None, None, False),
        (None, lambda _text: False, False),
        # An explicit abnormal stop is never overridden by accept_turn.
        ("error", lambda _text: True, False),
        ("aborted", lambda _text: True, False),
    ],
)
async def test_error_bearing_turn_usability(
    monkeypatch: pytest.MonkeyPatch, stop_reason, accept_turn, expected_success
) -> None:
    """The single usability owner decides inside ``_prompt_safely``."""
    payload = {"text": "a complete answer", "error": "503 Service Unavailable"}
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s-new"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json=payload)
        return httpx.Response(204)

    client, requests = _make_sidecar(handle)
    try:
        result = await _internal_call(client, monkeypatch, accept_turn=accept_turn)
    finally:
        await client.close()
    assert result.success is expected_success
    if expected_success:
        assert result.text == "a complete answer"
        assert result.error == "503 Service Unavailable"
        # A usable turn keeps its session: no DELETE was issued.
        assert [r.method for r in requests] == ["POST", "POST"]
        assert result.session_id == "s-new"
        assert getattr(result, "stop_reason", None) == stop_reason
    else:
        assert result.text == "503 Service Unavailable"
        # An unusable turn on a session we just created is always cleaned up.
        assert [r.method for r in requests] == ["POST", "POST", "DELETE"]
        assert result.session_id is None
        assert result.transcript_session_id == "s-new"


@pytest.mark.asyncio
async def test_completed_turn_with_recorded_error_keeps_answer_over_2048_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """>2048 chars survive whole: no truncation, no "[error detail omitted]"."""
    long_answer = "x" * 5000

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s-long"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(
                200,
                json={
                    "text": long_answer,
                    "error": "503 Service Unavailable",
                    "stop_reason": "stop",
                },
            )
        return httpx.Response(204)

    client, _requests = _make_sidecar(handle)
    try:
        result = await _internal_call(client, monkeypatch)
    finally:
        await client.close()
    assert result.success is True
    assert result.text == long_answer
    assert len(result.text) == 5000
    assert "omitted" not in result.text


@pytest.mark.asyncio
async def test_unusable_turn_on_new_session_writes_no_source_row(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session cleanup happens before any ``ai_session_sources`` row exists."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "no-row.db")
    await storage.init_db()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s-unusable"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"text": "", "error": "503 unavailable"})
        return httpx.Response(204)

    client, _requests = _make_sidecar(handle)
    try:
        result = await _internal_call(client, monkeypatch)
    finally:
        await client.close()
    assert result.success is False
    assert result.session_id is None
    assert await storage.get_ai_session_source("s-unusable", "", "openai") == "unknown"


@pytest.mark.asyncio
async def test_http_503_prompt_fails_and_deletes_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s-503"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(503, json={"error": "upstream unavailable"})
        return httpx.Response(204)

    client, requests = _make_sidecar(handle)
    try:
        result = await _internal_call(client, monkeypatch)
    finally:
        await client.close()
    assert result.success is False
    assert result.status_code == 503
    assert result.text == "upstream unavailable"
    assert result.session_id is None
    assert [r.method for r in requests] == ["POST", "POST", "DELETE"]


def _scripted_result(monkeypatch: pytest.MonkeyPatch, result: AIResult) -> None:
    monkeypatch.setattr(ai_client, "_call_ai", AsyncMock(return_value=result))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("Session 6f2a not found", "session not found"),
        ("not authenticated: review%2Fkey", "authentication required"),
        ("upstream said no", "AI call failed"),
    ],
)
async def test_user_credential_failure_tokens_survive_partial_narration(
    monkeypatch: pytest.MonkeyPatch, detail, expected
) -> None:
    """User-keyed failures keep the exact retry/auth token, never the narration."""
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "model"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="user-key-1"))
    monkeypatch.setattr(
        ai_client,
        "_call_user_session",
        AsyncMock(
            return_value=AIResult(
                success=False,
                text="Let me look at the logs...",
                error=detail,
                session_id="s-user",
            )
        ),
    )
    result = await real_call_ai_once("prompt", ai_provider="openai", ai_model="model")
    assert result.text == expected
    assert result.error == expected


@pytest.mark.asyncio
async def test_server_turn_warning_keeps_answer_and_sanitizes_error(
    initialized_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed server turn keeps its whole answer; the warning is sanitized."""
    long_answer = "y" * 4000
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "model"))
    )
    monkeypatch.setattr(
        ai_client,
        "server_provider_key_with_generation",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(
        ai_client, "_collect_server_redact_keys", AsyncMock(return_value=())
    )
    _scripted_result(
        monkeypatch,
        AIResult(
            success=True,
            text=long_answer,
            error=(
                f"Incorrect API key provided: {_runtime_openai_key()}"
                f" at https://api.example.test/v1?key={_runtime_google_key()}"
            ),
            session_id="s-warn",
        ),
    )
    result = await real_call_ai_once("prompt", ai_provider="openai", ai_model="model")
    assert result.success is True
    assert result.text == long_answer
    assert result.error is not None
    assert "503" not in result.error
    assert _runtime_openai_key() not in result.error
    assert _runtime_google_key() not in result.error
    assert "Incorrect API key provided" in result.error


@pytest.mark.asyncio
async def test_server_failure_keeps_sanitized_cause_not_ai_call_failed(
    initialized_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server failures keep a usable cause with no credential, URL query or ANSI."""
    openai_key = _runtime_openai_key()
    google_key = _runtime_google_key()
    detail = (
        f"\x1b[31m503 Service Unavailable\x1b[0m: Incorrect API key provided: "
        f"{openai_key}, masked {openai_key[:11]}****{openai_key[-4:]}, "
        f"gsk_{'z' * 20}, xai-{'q' * 20}, csk_{'w' * 20}, "
        f"see https://api.example.test/v1?key={google_key}"
    )
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "model"))
    )
    monkeypatch.setattr(
        ai_client,
        "server_provider_key_with_generation",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(
        ai_client, "_collect_server_redact_keys", AsyncMock(return_value=())
    )
    _scripted_result(
        monkeypatch,
        AIResult(success=False, text=detail, error=detail, status_code=503),
    )
    result = await real_call_ai_once("prompt", ai_provider="openai", ai_model="model")
    assert result.success is False
    assert result.text != "AI call failed"
    assert "503 Service Unavailable" in result.text
    for secret in (openai_key, google_key):
        assert secret not in result.text
    assert "\x1b" not in result.text
    diagnostic = ai_client.format_ai_diagnostic(result)
    assert "503 Service Unavailable" in diagnostic
    for secret in (openai_key, google_key):
        assert secret not in diagnostic
    # A status the detail does not already carry is prefixed, once.
    assert (
        ai_client.format_ai_diagnostic(
            AIResult(
                success=False,
                text="upstream refused",
                error="upstream refused",
                status_code=503,
            )
        )
        == "HTTP 503: upstream refused"
    )


@pytest.mark.asyncio
async def test_collect_server_redact_keys_cold_catalog_and_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every deployment key source is collected without a warm catalog or flag."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "keys.db")
    await storage.init_db()
    ambient = "sk-" + "ambient" + "9" * 20
    cursor = "cursor-key-" + "c" * 20
    literal = "literal-manifest-" + "m" * 20
    referenced = "referenced-manifest-" + "r" * 20
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {"apiKey": literal},
                    "mygateway": {"apiKey": "$CUSTOM_GATEWAY_KEY"},
                }
            }
        )
    )
    monkeypatch.setenv("OPENAI_API_KEY", ambient)
    monkeypatch.setenv("CURSOR_API_KEY", cursor)
    monkeypatch.setenv("CUSTOM_GATEWAY_KEY", referenced)
    monkeypatch.setattr(ai_client, "_sidecar_agent_dir", lambda: agent_dir)
    # Cold catalog: ambient eligibility is usually derived from this cache.
    ai_client.update_model_catalog(None)

    assert ambient in await ai_client._collect_server_redact_keys("openai")
    assert cursor in await ai_client._collect_server_redact_keys("cli-cursor")
    assert literal in await ai_client._collect_server_redact_keys("openai")
    assert referenced in await ai_client._collect_server_redact_keys("mygateway")


def test_scrub_answer_and_sanitize_diagnostic_share_one_pipeline() -> None:
    """One ordered redaction sequence, two bounds (R1)."""
    payload = (
        f"\x1b[31mfailed\x1b[0m with {_runtime_openai_key()} "
        "at https://api.example.test/v1?key=abc\t\nnext"
    )
    answer = ai_client._scrub_answer(payload)
    diagnostic = ai_client._sanitize_diagnostic(payload, ())
    assert answer == diagnostic
    assert _runtime_openai_key() not in answer
    assert "\x1b" not in answer

    # Both public functions route through the one private pipeline...
    calls: list[str] = []
    real_pipeline = ai_client._redact_pipeline
    ai_client._redact_pipeline = lambda text, keys: (
        calls.append(text) or real_pipeline(text, keys)
    )
    try:
        ai_client._scrub_answer(payload)
        ai_client._sanitize_diagnostic(payload, ())
    finally:
        ai_client._redact_pipeline = real_pipeline
    assert calls == [payload, payload]

    # ...and a mutation that empties it leaks the key and the escape sequence
    # through BOTH entry points, so neither owns a private copy of the rules.
    ai_client._redact_pipeline = lambda text, keys: text
    try:
        assert _runtime_openai_key() in ai_client._scrub_answer(payload)
        mutated = ai_client._sanitize_diagnostic(payload, ())
    finally:
        ai_client._redact_pipeline = real_pipeline
    assert mutated is not None
    assert _runtime_openai_key() in mutated


def test_sanitize_diagnostic_bounds_before_scrubbing() -> None:
    keyed = "a" * 4000
    assert ai_client._sanitize_diagnostic(keyed, ("some-key",)) == (
        "[error detail omitted]"
    )
    bounded = ai_client._sanitize_diagnostic(keyed, ())
    assert bounded is not None
    assert len(bounded) <= ai_client.MAX_ERROR_DETAIL_LENGTH
    assert ai_client._sanitize_diagnostic(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "\x1b[31mred\x1b[0m",
        "a\x00b",
        "a\x85b",
        "a\u2028b",
        "a\u2029b",
        "a\r\nb",
        "\x1b]0;title\x07after",
    ],
)
def test_sanitize_diagnostic_is_printable_single_line(raw: str) -> None:
    cleaned = ai_client._sanitize_diagnostic(raw, ())
    assert cleaned is not None
    assert "\n" not in cleaned
    assert all(ch.isprintable() or ch == " " for ch in cleaned)
    assert "[31m" not in cleaned
    assert "title" not in cleaned


def test_ai_client_imports_without_a_cycle() -> None:
    """R2: `import rootcoz.ai_client` is clean and feedback is importable after it."""
    import subprocess

    for statement in (
        "import rootcoz.ai_client",
        "import rootcoz.ai_client, rootcoz.feedback",
        "import rootcoz.feedback, rootcoz.ai_client",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", statement],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert completed.returncode == 0, completed.stderr


async def _insert_server_session_row(
    session_id: str, *, provider_generation: int | None, session_generation: int | None
) -> None:
    """Write the rows exactly as the named code version would have."""
    async with storage._connect_db() as db:
        await db.execute(
            "INSERT INTO server_ai_providers (provider, api_key_enc, generation)"
            " VALUES (?, ?, ?)",
            ("openai", "not-a-real-ciphertext", provider_generation),
        )
        await db.execute(
            "INSERT INTO ai_session_sources (session_id, username, provider,"
            " credential_source, credential_generation, server_credential_generation)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "admin",
                "openai",
                "server",
                0,
                session_generation,
            ),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_legacy_untracked_server_session_resumes(
    initialized_db, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A pre-change (NULL generation) server session resumes exactly as on main."""
    await _insert_server_session_row(
        "legacy-session", provider_generation=7, session_generation=None
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions/legacy-session/prompt":
            return httpx.Response(200, json={"text": "the answer"})
        return httpx.Response(204)

    client, requests = _make_sidecar(handle)
    token = ai_client.ai_username.set("admin")
    try:
        _bind_internal_call_ai(monkeypatch, client)
        monkeypatch.setattr(
            ai_client,
            "resolve_catalog_pair",
            AsyncMock(return_value=("openai", "model")),
        )
        monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
        with capture_main_logs(caplog, logging.DEBUG, logger="rootcoz.storage"):
            result = await call_ai_under_test(
                "next turn",
                ai_provider="openai",
                ai_model="model",
                session_id="legacy-session",
            )
    finally:
        ai_client.ai_username.reset(token)
        await client.close()
    assert result.success is True
    assert result.text == "the answer"
    assert [r.url.path for r in requests] == ["/sessions/legacy-session/prompt"]
    assert "untracked server session (legacy or ambient)" in caplog.text
    assert (
        await storage.get_ai_session_source("legacy-session", "admin", "openai")
        == "server"
    )


@pytest.mark.asyncio
async def test_tracked_server_session_after_rotation_is_unavailable(
    initialized_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a tracked row on an older generation fails closed."""
    await _insert_server_session_row(
        "rotated-session", provider_generation=7, session_generation=3
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "should never be asked"})

    client, requests = _make_sidecar(handle)
    token = ai_client.ai_username.set("admin")
    try:
        _bind_internal_call_ai(monkeypatch, client)
        monkeypatch.setattr(
            ai_client,
            "resolve_catalog_pair",
            AsyncMock(return_value=("openai", "model")),
        )
        result = await call_ai_under_test(
            "next turn",
            ai_provider="openai",
            ai_model="model",
            session_id="rotated-session",
        )
    finally:
        ai_client.ai_username.reset(token)
        await client.close()
    assert result.success is False
    assert result.text == "AI session unavailable"
    assert requests == []


@pytest.mark.asyncio
async def test_server_key_rotated_mid_turn_fails_closed(
    initialized_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-turn rotation keeps no old-keyed cause, session or text."""
    old_key = "server-key-" + "o" * 20  # pragma: allowlist secret
    await storage.set_server_ai_credential("openai", old_key, "admin")
    delete = AsyncMock()

    async def rotate_then_fail(*_args: Any, **_kwargs: Any) -> AIResult:
        await storage.set_server_ai_credential("openai", "rotated-" + "n" * 20, "admin")
        return AIResult(
            success=False,
            text=f"Incorrect API key provided: {old_key}",
            error=f"Incorrect API key provided: {old_key}",
            session_id="s-rot",
        )

    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "model"))
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(ai_client, "_call_ai", rotate_then_fail)
    sidecar = AsyncMock(delete_session=delete)
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: sidecar)
    result = await real_call_ai_once("prompt", ai_provider="openai", ai_model="model")
    assert result.success is False
    assert result.text == "AI call failed"
    assert old_key not in result.text
    assert old_key not in (result.error or "")
    delete.assert_awaited_once_with("s-rot")


@pytest.mark.asyncio
async def test_server_session_rotated_during_creation_is_deleted(
    initialized_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generation snapshot taken before create() is enforced inside the write."""
    await storage.set_server_ai_credential(
        "openai",
        "first-key-" + "f" * 20,
        "admin",  # pragma: allowlist secret
    )
    delete = AsyncMock()

    async def create_then_rotate() -> str:
        await storage.set_server_ai_credential(
            "openai",
            "second-key-" + "s" * 20,
            "admin",  # pragma: allowlist secret
        )
        return "created-under-old-key"

    from pi_sidecar_client import SidecarClient

    client = SidecarClient(base_url="http://sidecar.invalid")
    client.delete_session = delete
    token = ai_client.ai_username.set("admin")
    try:
        with pytest.raises(ValueError, match="AI credential changed"):
            await ai_client.create_session_safely(
                client,
                provider="openai",
                model="model",
                system_prompt="sys",
                api_key="first-key-" + "f" * 20,  # pragma: allowlist secret
            )
    finally:
        ai_client.ai_username.reset(token)
        await client.close()
    assert (
        await storage.get_ai_session_source("created-under-old-key", "admin", "openai")
        == "unknown"
    )


@pytest.mark.asyncio
async def test_server_key_never_logged_when_sessions_creation_echoes_base64(
    initialized_db, monkeypatch: pytest.MonkeyPatch, caplog, capsys
) -> None:
    """A 400 on ``/sessions`` echoing the key (base64 too) is never logged."""
    secret = "server-session-key-" + "k" * 24  # pragma: allowlist secret
    encoded = base64.b64encode(secret.encode()).decode()
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(
                400,
                json={"error": f"Incorrect API key provided: {secret} ({encoded})"},
            )
        return httpx.Response(204)

    client, _ = _make_sidecar(handle)
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "_call_ai", real_internal_call_ai)
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "model"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ai_client,
        "server_provider_key_with_generation",
        AsyncMock(return_value=(secret, None)),
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(
        ai_client, "_collect_server_redact_keys", AsyncMock(return_value=(secret,))
    )
    token = ai_client.ai_username.set("admin")
    try:
        with capture_main_logs(
            caplog, logging.DEBUG, logger=("rootcoz.ai_client", "pi_sidecar_client")
        ):
            result = await real_call_ai_once(
                "prompt", ai_provider="openai", ai_model="model"
            )
        captured = caplog.text + capsys.readouterr().err
    finally:
        ai_client.ai_username.reset(token)
        await client.close()
    assert result.success is False
    assert secret not in captured
    assert encoded not in captured
    assert [r.method for r in requests] == ["POST"]


def test_encoded_key_renderings_are_redacted() -> None:
    """Literal, JSON/Unicode, percent-escaped and base64 renderings all go."""
    key = "srv\u00e9-key/with+special"
    renderings = {
        key,
        json.dumps(key, ensure_ascii=True)[1:-1],
        quote(key, safe=""),
        base64.b64encode(key.encode()).decode(),
    }
    for rendering in renderings:
        cleaned = ai_client._sanitize_diagnostic(f"upstream said {rendering}", (key,))
        assert cleaned is not None
        assert rendering not in cleaned
        assert key not in cleaned
