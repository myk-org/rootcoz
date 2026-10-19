"""Admin-managed server provider credentials and model selection (#347).

Covers storage (encryption at rest, per-provider free-pair sync), the admin
endpoints (verification-gated add/rotate, catalog-validated model selection,
delete warnings), the scoped-models server pool, and per-session apiKey
injection with unchanged credential-source semantics.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pi_sidecar_client import AIResult

from rootcoz import ai_client, storage
from rootcoz.ai_client import call_ai as real_call_ai
from rootcoz.ai_client import call_ai_once as real_call_ai_once
from rootcoz.config import get_settings
from rootcoz.main import app

SECRET = "server-test-key-123"  # pragma: allowlist secret


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "server-providers.db")
    monkeypatch.setenv("ADMIN_KEY", "sp-bootstrap-key")
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server-providers.db"))
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def _admin_headers():
    return {"Authorization": "Bearer sp-bootstrap-key"}


def _fake_catalog(monkeypatch, catalog):
    async def fake(refresh: bool = False):
        return catalog

    monkeypatch.setattr(ai_client, "_get_model_catalog", fake)
    monkeypatch.setattr("rootcoz.main._get_model_catalog", fake, raising=False)


def _mock_supported(monkeypatch, providers):
    async def fake():
        return providers

    monkeypatch.setattr("rootcoz.main.supported_key_providers", fake)


def _mock_verify(monkeypatch, outcome):
    verified = []

    async def fake(provider, model, key):
        verified.append((provider, model, key))
        return outcome

    monkeypatch.setattr(ai_client, "verify_ai_key", fake)
    return verified


CATALOG = [
    {"provider": "google", "id": "gemini-2.5-pro", "name": "Gemini Pro"},
    {"provider": "google", "id": "gemini-2.5-flash"},
    {"provider": "openai", "id": "gpt-5.6-luna"},
]


# --- Storage ---


async def test_storage_roundtrip_encrypted(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    await storage.set_server_ai_credential("google", SECRET, "admin")
    assert await storage.get_server_ai_credential("google") == SECRET
    assert await storage.get_server_ai_credential("openai") is None
    assert b"server-test-key" not in (tmp_path / "sp.db").read_bytes()
    listed = await storage.list_server_ai_providers()
    assert [row["provider"] for row in listed] == ["google"]
    assert "api_key" not in listed[0] and "api_key_enc" not in listed[0]


async def test_model_selection_and_free_pair_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    await storage.set_server_provider_models(
        "google",
        [{"id": "gemini-2.5-pro", "free": True}, {"id": "gemini-2.5-flash"}],
        "admin",
    )
    selection = await storage.list_server_provider_models()
    assert {(row["provider"], row["model"]) for row in selection} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-2.5-flash"),
    }
    free = await storage.list_free_ai_models()
    assert [(row["provider"], row["model"]) for row in free] == [
        ("google", "gemini-2.5-pro")
    ]

    # Replacing the selection drops removed pairs and keeps other providers.
    await storage.set_free_ai_models([("other", "m1")], "admin")
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-flash", "free": False}], "admin"
    )
    free = await storage.list_free_ai_models()
    assert [(row["provider"], row["model"]) for row in free] == [("other", "m1")]
    selection = await storage.list_server_provider_models()
    assert [(row["provider"], row["model"]) for row in selection] == [
        ("google", "gemini-2.5-flash")
    ]


async def test_delete_returns_affected_warning_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-pro", "free": True}], "admin"
    )
    affected = await storage.delete_server_ai_provider("google")
    assert affected == {
        "selected_models": ["gemini-2.5-pro"],
        "free_models": ["gemini-2.5-pro"],
    }
    assert await storage.get_server_ai_credential("google") is None
    assert await storage.list_server_provider_models() == []
    assert await storage.list_free_ai_models() == []


# --- Endpoints ---


def test_endpoints_require_admin(client):
    assert (
        client.get("/api/admin/server-providers").status_code == 403
    )  # no credentials at all
    created = client.post(
        "/api/admin/users/create",
        headers=_admin_headers(),
        json={"username": "plain-user", "role": "operator"},
    )
    key = created.json()["api_key"]
    for method, path in (
        ("get", "/api/admin/server-providers"),
        ("post", "/api/admin/server-providers"),
        ("put", "/api/admin/server-providers/google/models"),
        ("delete", "/api/admin/server-providers/google"),
        ("post", "/api/admin/server-providers/google/rotate-key"),
    ):
        response = getattr(client, method)(
            path, headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == 403, (method, path)


async def test_add_rejects_unverified_key_and_stores_verified(client, monkeypatch):
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    verified = _mock_verify(monkeypatch, "rejected")
    body = {
        "provider": "google",
        "api_key": SECRET,
        "verification_model": "gemini-2.5-pro",
    }
    rejected = client.post(
        "/api/admin/server-providers", headers=_admin_headers(), json=body
    )
    assert rejected.status_code == 200
    assert rejected.json()["ok"] is False
    assert rejected.json()["outcome"] == "rejected"
    assert verified == [("google", "gemini-2.5-pro", SECRET)]
    assert await storage.list_server_ai_providers() == []

    _mock_verify(monkeypatch, "accepted")
    accepted = client.post(
        "/api/admin/server-providers", headers=_admin_headers(), json=body
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"outcome": "accepted", "ok": True}
    # Keys never appear in any response.
    assert SECRET not in accepted.text
    stored = await storage.list_server_ai_providers()
    assert [row["provider"] for row in stored] == ["google"]

    # Adding over an existing key is a conflict — rotate instead.
    again = client.post(
        "/api/admin/server-providers", headers=_admin_headers(), json=body
    )
    assert again.status_code == 409


def test_add_rejects_unsupported_provider_and_bad_key(client, monkeypatch):
    _mock_supported(monkeypatch, ["openai"])
    response = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": SECRET,
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert response.status_code == 400
    short = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "openai",
            "api_key": "short",  # pragma: allowlist secret
            "verification_model": "m",
        },
    )
    assert short.status_code == 400


async def test_rotate_requires_existing_key_and_reverifies(client, monkeypatch):
    _mock_supported(monkeypatch, ["google"])
    missing = client.post(
        "/api/admin/server-providers/google/rotate-key",
        headers=_admin_headers(),
        json={"provider": "google", "api_key": SECRET, "verification_model": "m"},
    )
    assert missing.status_code == 404

    await storage.set_server_ai_credential("google", "old-key-value-1", "admin")
    verified = _mock_verify(monkeypatch, "accepted")
    rotated = client.post(
        "/api/admin/server-providers/google/rotate-key",
        headers=_admin_headers(),
        json={"provider": "google", "api_key": SECRET, "verification_model": "m"},
    )
    assert rotated.status_code == 200
    assert rotated.json()["ok"] is True
    assert verified == [("google", "m", SECRET)]
    assert await storage.get_server_ai_credential("google") == SECRET


async def test_list_reports_key_state_and_model_flags(client, monkeypatch):
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google", "openai"])
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-pro", "free": True}], "admin"
    )
    response = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert response.status_code == 200
    providers = {p["provider"]: p for p in response.json()["providers"]}
    assert providers["google"]["has_key"] is True
    assert providers["google"]["verified"] is True
    assert providers["openai"]["has_key"] is False
    models = {m["id"]: m for m in providers["google"]["models"]}
    assert models["gemini-2.5-pro"] == {
        "id": "gemini-2.5-pro",
        "name": "Gemini Pro",
        "included": True,
        "free": True,
    }
    assert models["gemini-2.5-flash"]["included"] is False
    # Keys never appear in the listing.
    assert SECRET not in response.text


async def test_models_endpoint_normalizes_free_subset_and_validates(
    client, monkeypatch
):
    _fake_catalog(monkeypatch, CATALOG)
    unknown = client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={"models": [{"id": "no-such-model", "included": True}]},
    )
    assert unknown.status_code == 400
    assert unknown.json()["detail"]["unknown"] == [
        {"provider": "google", "model": "no-such-model"}
    ]

    # free=True with included=False auto-includes.
    saved = client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={
            "models": [
                {"id": "gemini-2.5-pro", "included": False, "free": True},
                {"id": "gemini-2.5-flash", "included": True},
            ]
        },
    )
    assert saved.status_code == 200
    models = {m["id"]: m for m in saved.json()["models"]}
    assert models["gemini-2.5-pro"] == {
        "id": "gemini-2.5-pro",
        "included": True,
        "free": True,
    }
    assert models["gemini-2.5-flash"]["free"] is False
    # The free flag landed in the same free_ai_models storage.
    free = await storage.list_free_ai_models()
    assert [(row["provider"], row["model"]) for row in free] == [
        ("google", "gemini-2.5-pro")
    ]
    # Unchecking include clears the free flag.
    cleared = client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={"models": [{"id": "gemini-2.5-pro", "included": True}]},
    )
    assert cleared.status_code == 200
    assert await storage.list_free_ai_models() == []


async def test_standalone_free_models_endpoint_still_works(client, monkeypatch):
    _fake_catalog(monkeypatch, CATALOG)
    listing = client.get("/api/admin/free-models", headers=_admin_headers())
    assert listing.status_code == 200
    saved = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={"models": [{"provider": "google", "model": "gemini-2.5-pro"}]},
    )
    assert saved.status_code == 200
    # The server-providers listing reflects the shared storage.
    providers = {
        p["provider"]: p
        for p in client.get(
            "/api/admin/server-providers", headers=_admin_headers()
        ).json()["providers"]
    }
    models = {m["id"]: m for m in providers["google"]["models"]}
    assert models["gemini-2.5-pro"]["free"] is True


async def test_delete_returns_warning_payload(client, monkeypatch):
    _fake_catalog(monkeypatch, CATALOG)
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-pro", "free": True}], "admin"
    )
    deleted = client.delete(
        "/api/admin/server-providers/google", headers=_admin_headers()
    )
    assert deleted.status_code == 200
    assert deleted.json() == {
        "ok": True,
        "warnings": {
            "selected_models": ["gemini-2.5-pro"],
            "free_models": ["gemini-2.5-pro"],
        },
    }
    assert await storage.list_server_ai_providers() == []
    missing = client.delete(
        "/api/admin/server-providers/google", headers=_admin_headers()
    )
    assert missing.status_code == 404


# --- Scoped model pool ---


async def test_scoped_models_restricts_server_pool_to_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    _fake_catalog(monkeypatch, CATALOG)
    token = ai_client.force_server_credentials.set(True)
    try:
        before = await ai_client.scoped_models()
        assert [m["id"] for m in before["google"]] == [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ]

        await storage.set_server_provider_models(
            "google", [{"id": "gemini-2.5-pro"}], "admin"
        )
        after = await ai_client.scoped_models()
        assert [m["id"] for m in after["google"]] == ["gemini-2.5-pro"]
        # Untouched providers keep their full catalog.
        assert [m["id"] for m in after["openai"]] == ["gpt-5.6-luna"]
    finally:
        ai_client.force_server_credentials.reset(token)


# --- Session integration: per-session apiKey, server source unchanged ---


async def test_call_ai_passes_server_key_as_session_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-call.db")
    await storage.init_db()
    await storage.set_server_ai_credential("p", SECRET, "admin")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok", session_id="s1"))
    monkeypatch.setattr(ai_client, "_call_ai", sidecar)
    token = ai_client.ai_username.set("admin")
    try:
        result = await real_call_ai("prompt", ai_provider="p", ai_model="m")
        assert result.success
        assert result.credential_source == "server"
        assert sidecar.call_args.kwargs["api_key"] == SECRET
        # The server-keyed session is still tracked as server-owned.
        assert await storage.get_ai_session_source("s1", "admin", "p") == "server"
    finally:
        ai_client.ai_username.reset(token)


async def test_call_ai_without_server_key_keeps_ambient_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-ambient.db")
    await storage.init_db()
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok", session_id="s2"))
    monkeypatch.setattr(ai_client, "_call_ai", sidecar)
    token = ai_client.ai_username.set("admin")
    try:
        result = await real_call_ai("prompt", ai_provider="p", ai_model="m")
        assert result.success
        assert result.credential_source == "server"
        assert "api_key" not in sidecar.call_args.kwargs
    finally:
        ai_client.ai_username.reset(token)


async def test_call_ai_once_passes_server_key_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-once.db")
    await storage.init_db()
    await storage.set_server_ai_credential("p", SECRET, "admin")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok", session_id="s3"))
    monkeypatch.setattr(ai_client, "_call_ai", sidecar)
    mock_client = MagicMock()
    mock_client.delete_session = AsyncMock()
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: mock_client)
    token = ai_client.ai_username.set("admin")
    try:
        result = await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
        assert result.success
        assert result.credential_source == "server"
        assert sidecar.call_args.kwargs["api_key"] == SECRET
        # One-shot server sessions are cleaned up like ambient ones.
        mock_client.delete_session.assert_awaited_once_with("s3")
        assert result.session_id is None
    finally:
        ai_client.ai_username.reset(token)


async def test_user_key_still_wins_over_server_key(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-userwins.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "user-key-value-9")
    await storage.set_server_ai_credential("p", SECRET, "admin")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["p"])
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok", session_id="s4"))
    monkeypatch.setattr(ai_client, "_call_user_session", sidecar)
    token = ai_client.ai_username.set("alice")
    try:
        result = await real_call_ai("prompt", ai_provider="p", ai_model="m")
        assert result.success
        assert result.credential_source == "user"
        used_key = sidecar.call_args.kwargs["api_key"]  # pragma: allowlist secret
        assert used_key == "user-key-value-9"  # pragma: allowlist secret
    finally:
        ai_client.ai_username.reset(token)
