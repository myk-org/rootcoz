"""Admin-managed server provider credentials and model selection (#347).

Covers storage (encryption at rest, per-provider free-pair sync), the admin
endpoints (verification-gated add/rotate, catalog-validated model selection,
delete warnings), the scoped-models server pool, and per-session apiKey
injection with unchanged credential-source semantics.
"""

import json
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


async def test_add_accepts_lowercase_provider_id_of_mixed_case_provider(
    client, monkeypatch
):
    """The supported-provider gate is case-insensitive (#347).

    The sidecar catalog reports mixed-case IDs (e.g. ``EnMaaS``), but a
    raw API/CLI caller may post the same provider in lowercase — that
    must pass the gate instead of the misleading "does not support
    session API keys" 400. The shared add/rotate gate and the listing's
    ``supports_key`` flag both match ignoring case.
    """
    _fake_catalog(monkeypatch, [{"provider": "EnMaaS", "id": "enmaas-1"}])
    _mock_supported(monkeypatch, ["EnMaaS"])
    verified = _mock_verify(monkeypatch, "accepted")
    response = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "enmaas",
            "api_key": SECRET,
            "verification_model": "enmaas-1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"outcome": "accepted", "ok": True}
    assert "does not support session API keys" not in response.text
    # Verification and storage used the sidecar's exact-case id — the
    # sidecar itself is case-sensitive and rejects a raw lowercase id.
    assert verified == [("EnMaaS", "enmaas-1", SECRET)]
    stored = await storage.list_server_ai_providers()
    assert [row["provider"] for row in stored] == ["EnMaaS"]
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert listed.status_code == 200
    providers = listed.json()["providers"]
    assert [p["provider"] for p in providers] == ["EnMaaS"]
    entry = providers[0]
    assert entry["has_key"] is True
    assert entry["supports_key"] is True


async def test_add_canonicalizes_before_duplicate_check(client, monkeypatch):
    """A case-drifted stored row still blocks a canonical-case add (#347).

    Canonicalization happens before the duplicate check, so adding
    ``EnMaaS`` over a row stored as ``enmaas`` is a 409 — never a second
    mixed-case duplicate row.
    """
    _fake_catalog(monkeypatch, [{"provider": "EnMaaS", "id": "enmaas-1"}])
    _mock_supported(monkeypatch, ["EnMaaS"])
    _mock_verify(monkeypatch, "accepted")
    await storage.set_server_ai_credential("enmaas", SECRET, "admin")
    conflict = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "EnMaaS",
            "api_key": SECRET,
            "verification_model": "enmaas-1",
        },
    )
    assert conflict.status_code == 409
    stored = await storage.list_server_ai_providers()
    assert [row["provider"] for row in stored] == ["enmaas"]


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


# --- Key add seeds a full selection: exposure never shrinks (#347 r4) ---


async def test_add_key_to_unmanaged_provider_seeds_full_selection(client, monkeypatch):
    """A stored key makes a provider managed — its whole catalog is seeded.

    Without seeding, adding a verified key to a provider that served models
    via deployment credentials would silently withdraw every model from
    discovery and make require_selected_server_pair reject them all.
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    # A pre-existing free pair must survive the seeding untouched.
    await storage.set_free_ai_models([("google", "gemini-2.5-flash")], "admin")
    added = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": SECRET,
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert added.status_code == 200
    assert added.json()["ok"] is True
    # Every catalog model became a selection row — exposure unchanged.
    selection = await storage.list_server_provider_models()
    assert {(row["provider"], row["model"]) for row in selection} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-2.5-flash"),
    }
    # The pre-existing free pair survived.
    free = await storage.list_free_ai_models()
    assert [(row["provider"], row["model"]) for row in free] == [
        ("google", "gemini-2.5-flash")
    ]
    # Discovery and the authorization check are unchanged for the provider.
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert [m["id"] for m in scoped["google"]] == [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ]
    finally:
        ai_client.force_server_credentials.reset(token)
    await ai_client.require_selected_server_pair("google", "gemini-2.5-flash")
    # Untouched providers stay unmanaged with their full catalog.
    assert [m["id"] for m in scoped["openai"]] == ["gpt-5.6-luna"]


async def test_existing_selection_survives_key_add_and_rotate(client, monkeypatch):
    """An admin selection is authoritative — neither add nor rotate reseeds it."""
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    await storage.set_server_ai_credential("google", "old-key-value-2", "admin")
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-pro"}], "admin"
    )
    rotated = client.post(
        "/api/admin/server-providers/google/rotate-key",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": SECRET,
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert rotated.status_code == 200
    selection = await storage.list_server_provider_models()
    assert [(row["provider"], row["model"]) for row in selection] == [
        ("google", "gemini-2.5-pro")
    ]
    # The same holds when the selection predates the first key: a key add
    # over a selection-only provider must not widen the admin's choice.
    await storage.delete_server_ai_provider("google")
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-pro"}], "admin"
    )
    added = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": SECRET,
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert added.status_code == 200
    selection = await storage.list_server_provider_models()
    assert [(row["provider"], row["model"]) for row in selection] == [
        ("google", "gemini-2.5-pro")
    ]


# --- Concurrent add/remove during verification (#347 r3) ---


async def test_concurrent_add_during_verification_returns_409(client, monkeypatch):
    """A key stored by a concurrent add during verification is not overwritten.

    The duplicate check runs once before verification and once after it —
    the second check is what catches the race, so the mock sequencing
    (empty listing first, existing key second) proves the re-check fires.
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    verified = _mock_verify(monkeypatch, "accepted")
    stored = AsyncMock()
    monkeypatch.setattr(storage, "set_server_ai_credential", stored)
    monkeypatch.setattr(
        storage,
        "list_server_ai_providers",
        AsyncMock(side_effect=[[], [{"provider": "google"}]]),
    )
    response = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": SECRET,
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert response.status_code == 409
    # Verification ran (and passed) before the re-check rejected the write.
    assert verified == [("google", "gemini-2.5-pro", SECRET)]
    stored.assert_not_awaited()


async def test_concurrent_remove_during_rotation_returns_404(client, monkeypatch):
    """A key removed while its rotation verifies is not resurrected.

    The pre-check sees the existing key, verification passes, then the
    post-verification re-check finds it gone — a 404 matching the
    absent-provider behavior, and no write.
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    stored = AsyncMock()
    monkeypatch.setattr(storage, "set_server_ai_credential", stored)
    monkeypatch.setattr(
        storage,
        "list_server_ai_providers",
        AsyncMock(side_effect=[[{"provider": "google"}], []]),
    )
    response = client.post(
        "/api/admin/server-providers/google/rotate-key",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": SECRET,
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert response.status_code == 404
    stored.assert_not_awaited()


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


async def test_list_reports_unmanaged_provider_whole_catalog_included(
    client, monkeypatch
):
    """An unmanaged provider's GET listing must match what it exposes (#347 r4).

    No key + no selection rows means the server pool serves the provider's
    whole catalog, so every catalog model must report ``included`` —
    reporting them unchecked would let one Free-pill click PUT a single-model
    draft and silently withdraw the rest of the catalog. The ``managed``
    flag tells the UI which rule the flags follow.
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google", "openai"])
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-pro"}], "admin"
    )
    response = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert response.status_code == 200
    providers = {p["provider"]: p for p in response.json()["providers"]}
    # Managed (key + selection): flags follow the stored selection.
    assert providers["google"]["managed"] is True
    google_models = {m["id"]: m for m in providers["google"]["models"]}
    assert google_models["gemini-2.5-pro"]["included"] is True
    assert google_models["gemini-2.5-flash"]["included"] is False
    # Unmanaged (env/ambient credentials): whole catalog included.
    assert providers["openai"]["managed"] is False
    openai_models = {m["id"]: m for m in providers["openai"]["models"]}
    assert openai_models["gpt-5.6-luna"]["included"] is True
    # A draft PUT built from that listing (everything left checked) keeps
    # the provider's full exposure instead of shrinking it to one model.
    saved = client.put(
        "/api/admin/server-providers/openai/models",
        headers=_admin_headers(),
        json={
            "models": [
                {"id": m["id"], "included": m["included"], "free": m["free"]}
                for m in providers["openai"]["models"]
            ]
        },
    )
    assert saved.status_code == 200
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert [m["id"] for m in scoped["openai"]] == ["gpt-5.6-luna"]
    finally:
        ai_client.force_server_credentials.reset(token)


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


async def test_free_pairs_only_provider_deletes_cleanly(client, monkeypatch):
    """A provider configured solely via free pairs must be removable (#347 r4).

    Deletion removes free_ai_models rows too, so the existence check must
    see them — or a free-pairs-only provider 404s and its pairs survive
    with no way to clear them.
    """
    _fake_catalog(monkeypatch, CATALOG)
    await storage.set_free_ai_models([("google", "gemini-2.5-pro")], "admin")
    deleted = client.delete(
        "/api/admin/server-providers/google", headers=_admin_headers()
    )
    assert deleted.status_code == 200
    assert deleted.json() == {
        "ok": True,
        "warnings": {"selected_models": [], "free_models": ["gemini-2.5-pro"]},
    }
    assert await storage.list_free_ai_models() == []
    assert await storage.list_server_provider_models() == []
    assert await storage.list_server_ai_providers() == []
    # A provider with nothing configured at all still 404s.
    missing = client.delete(
        "/api/admin/server-providers/openai", headers=_admin_headers()
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
    # The pair must be selected: a stored key makes the provider managed,
    # so an unselected model is excluded from server credentials.
    await storage.set_server_provider_models("p", [{"id": "m"}], "admin")
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
    await storage.set_server_provider_models("p", [{"id": "m"}], "admin")
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


# --- Deselected models: never stored, never runnable on server creds ---
# (storage/exposure: #347 review; server-credential authorization: #347 r3)


async def test_call_ai_rejects_deselected_model_on_server_credentials(
    tmp_path, monkeypatch
):
    """A raw catalog pair deselected by the admin must not run on server creds."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-excluded.db")
    await storage.init_db()
    await storage.set_server_ai_credential("p", SECRET, "admin")
    await storage.set_server_provider_models("p", [{"id": "other"}], "admin")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok"))
    monkeypatch.setattr(ai_client, "_call_ai", sidecar)
    token = ai_client.ai_username.set("admin")
    try:
        with pytest.raises(ai_client.ExcludedServerModelError) as excinfo:
            await real_call_ai("prompt", ai_provider="p", ai_model="m")
        # The rejection names the excluded model and provider.
        assert "'m'" in str(excinfo.value)
        assert "'p'" in str(excinfo.value)
        sidecar.assert_not_called()
        # Selecting the pair restores the server-credential call.
        await storage.set_server_provider_models("p", [{"id": "m"}], "admin")
        result = await real_call_ai("prompt", ai_provider="p", ai_model="m")
        assert result.success
        assert result.credential_source == "server"
    finally:
        ai_client.ai_username.reset(token)


async def test_call_ai_once_rejects_deselected_model_on_server_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-excluded-once.db")
    await storage.init_db()
    await storage.set_server_ai_credential("p", SECRET, "admin")
    await storage.set_server_provider_models("p", [], "admin")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok"))
    monkeypatch.setattr(ai_client, "_call_ai", sidecar)
    token = ai_client.ai_username.set("admin")
    try:
        with pytest.raises(ai_client.ExcludedServerModelError, match="'m'"):
            await real_call_ai_once("prompt", ai_provider="p", ai_model="m")
        sidecar.assert_not_called()
    finally:
        ai_client.ai_username.reset(token)


async def test_user_key_can_run_model_excluded_from_server_pool(tmp_path, monkeypatch):
    """The restriction scopes server credentials only — user keys stay allowed."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-excluded-user.db")
    await storage.init_db()
    await storage.create_user("alice")
    await storage.update_user_ai_credential("alice", "p", "user-key-value-7")
    await storage.set_server_ai_credential("p", SECRET, "admin")
    await storage.set_server_provider_models("p", [{"id": "other"}], "admin")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["p"])
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok"))
    monkeypatch.setattr(ai_client, "_call_user_session", sidecar)
    token = ai_client.ai_username.set("alice")
    try:
        result = await real_call_ai("prompt", ai_provider="p", ai_model="m")
        assert result.success
        assert result.credential_source == "user"
        used_key = sidecar.call_args.kwargs["api_key"]  # pragma: allowlist secret
        assert used_key == "user-key-value-7"  # pragma: allowlist secret
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_chat_session_creation_rejects_deselected_model(monkeypatch, tmp_path):
    """Chat session creation refuses a deselected pair on server credentials."""
    from rootcoz.engine import chat

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat-excluded.db")
    await storage.init_db()
    await storage.set_server_ai_credential("openai", SECRET, "admin")
    await storage.set_server_provider_models("openai", [{"id": "other"}], "admin")
    create = AsyncMock(return_value="chat-sid")
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ai_client, "server_provider_key", AsyncMock(return_value=SECRET)
    )
    monkeypatch.setattr(chat, "create_session_safely", create)
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: object())
    choice = ai_client._selected_credential_source.set("server")
    token = ai_client.ai_username.set("admin")
    try:
        assert (
            await chat._create_chat_session(
                system_prompt="hi",
                ai_provider="openai",
                ai_model="m",
                repo_path=tmp_path,
            )
            is None
        )
        create.assert_not_awaited()
    finally:
        ai_client._selected_credential_source.reset(choice)
        ai_client.ai_username.reset(token)


async def test_deselected_model_is_not_stored_or_exposed(client, monkeypatch):
    _fake_catalog(monkeypatch, CATALOG)
    saved = client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={
            "models": [
                {"id": "gemini-2.5-pro", "included": True},
                {"id": "gemini-2.5-flash", "included": False},
            ]
        },
    )
    assert saved.status_code == 200
    # Not stored...
    selection = await storage.list_server_provider_models()
    assert [(row["provider"], row["model"]) for row in selection] == [
        ("google", "gemini-2.5-pro")
    ]
    # ...not in the scoped server pool...
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert [m["id"] for m in scoped["google"]] == ["gemini-2.5-pro"]
    finally:
        ai_client.force_server_credentials.reset(token)
    # ...and GET reports it excluded.
    providers = {
        p["provider"]: p
        for p in client.get(
            "/api/admin/server-providers", headers=_admin_headers()
        ).json()["providers"]
    }
    models = {m["id"]: m for m in providers["google"]["models"]}
    assert models["gemini-2.5-flash"]["included"] is False


async def test_empty_selection_on_managed_provider_exposes_no_server_models(
    client, monkeypatch
):
    """No rows must mean 'nothing exposed', not 'never scoped' (#347 review)."""
    _fake_catalog(monkeypatch, CATALOG)
    await storage.set_server_ai_credential("google", SECRET, "admin")
    # Deselect every model — the selection becomes explicitly empty while
    # the provider stays managed by its stored key.
    cleared = client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={"models": []},
    )
    assert cleared.status_code == 200
    assert await storage.list_server_provider_models() == []
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert scoped.get("google", []) == []
        # Never-managed providers keep their full catalog.
        assert [m["id"] for m in scoped["openai"]] == ["gpt-5.6-luna"]
    finally:
        ai_client.force_server_credentials.reset(token)


# --- free ⊆ included normalization on the standalone endpoint (#347 review) ---


async def test_standalone_free_models_auto_include_into_managed_selection(
    client, monkeypatch
):
    _fake_catalog(monkeypatch, CATALOG)
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-pro"}], "admin"
    )
    saved = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={"models": [{"provider": "google", "model": "gemini-2.5-flash"}]},
    )
    assert saved.status_code == 200
    # The free pair was auto-included in the managed provider's selection.
    selection = await storage.list_server_provider_models()
    assert {(row["provider"], row["model"]) for row in selection} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-2.5-flash"),
    }
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        by_id = {m["id"]: m for m in scoped["google"]}
        assert by_id["gemini-2.5-flash"]["free"] is True
    finally:
        ai_client.force_server_credentials.reset(token)
    # A never-managed provider needs no selection row — its catalog is full.
    unmanaged = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={
            "models": [
                {"provider": "google", "model": "gemini-2.5-flash"},
                {"provider": "openai", "model": "gpt-5.6-luna"},
            ]
        },
    )
    assert unmanaged.status_code == 200
    assert not any(
        row["provider"] == "openai"
        for row in await storage.list_server_provider_models()
    )


# --- Provider casing must not leave rows behind (#347 review) ---


async def test_selection_replacement_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "case.db")
    await storage.init_db()
    await storage.set_server_provider_models(
        "Google", [{"id": "gemini-2.5-pro"}], "admin"
    )
    # A differently-cased PUT replaces every stored spelling, not just the
    # exact match — no "Google" rows may survive beside the new "google" ones.
    await storage.set_server_provider_models(
        "google", [{"id": "gemini-2.5-flash"}], "admin"
    )
    selection = await storage.list_server_provider_models()
    assert [(row["provider"], row["model"]) for row in selection] == [
        ("google", "gemini-2.5-flash")
    ]


async def test_credential_read_and_delete_are_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "case-key.db")
    await storage.init_db()
    await storage.set_server_ai_credential("Google", SECRET, "admin")
    assert await storage.get_server_ai_credential("google") == SECRET
    # A rotate with drifted spelling replaces the stored row instead of
    # duplicating the provider.
    await storage.set_server_ai_credential("google", "rotated-key-1", "admin")
    providers = await storage.list_server_ai_providers()
    assert [row["provider"] for row in providers] == ["Google"]
    assert await storage.get_server_ai_credential("google") == "rotated-key-1"

    await storage.set_server_provider_models(
        "Google", [{"id": "gemini-2.5-pro", "free": True}], "admin"
    )
    affected = await storage.delete_server_ai_provider("google")
    assert affected == {
        "selected_models": ["gemini-2.5-pro"],
        "free_models": ["gemini-2.5-pro"],
    }
    assert await storage.list_server_ai_providers() == []
    assert await storage.list_server_provider_models() == []
    assert await storage.list_free_ai_models() == []


async def test_corrupt_ciphertext_never_becomes_an_api_key(
    tmp_path, monkeypatch, caplog
):
    """A corrupt store surfaces as unreadable — the ciphertext is never a key.

    decrypt_value preserves the ciphertext on decryption failure; the
    credential read must detect that convention and take the failure path
    so the sidecar never receives ciphertext as an apiKey (#347 review).
    """
    import logging

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "corrupt.db")
    await storage.init_db()
    await storage.set_server_ai_credential("google", SECRET, "admin")
    # Corrupt the stored ciphertext (bit rot / rotated encryption key).
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE server_ai_providers SET api_key_enc = api_key_enc || 'corrupted'"
        )
        await db.commit()

    storage_logger = logging.getLogger("rootcoz.storage")
    storage_logger.addHandler(caplog.handler)
    try:
        with pytest.raises(storage.UnreadableAiCredentialsError):
            await storage.get_server_ai_credential("google")
    finally:
        storage_logger.removeHandler(caplog.handler)
    assert "refusing access" in caplog.text

    # The handled caller path: no credential, never the ciphertext.
    from rootcoz.ai_client import server_provider_key

    assert await server_provider_key("google") is None


async def test_list_joins_stored_key_case_insensitively(client, monkeypatch):
    """A case-drifted stored row keys the catalog entry — no split rows.

    Storage matches providers case-insensitively; the listing join must
    too, or a key stored as ``Google`` shows up as both a keyed ``Google``
    and an unkeyed ``google`` entry (#347 review).
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    await storage.set_server_ai_credential("Google", SECRET, "admin")
    response = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert response.status_code == 200
    providers = response.json()["providers"]
    assert [p["provider"] for p in providers] == ["google", "openai"]
    by_provider = {p["provider"]: p for p in providers}
    assert by_provider["google"]["has_key"] is True
    assert by_provider["google"]["verified"] is True
    assert by_provider["openai"]["has_key"] is False
    assert SECRET not in response.text


async def test_endpoint_key_checks_are_case_insensitive(client, monkeypatch):
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    await storage.set_server_ai_credential("Google", SECRET, "admin")
    # Adding a differently-cased spelling is a conflict, not a second row.
    conflict = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": SECRET,
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert conflict.status_code == 409
    # Delete with drifted casing removes the stored key.
    deleted = client.delete(
        "/api/admin/server-providers/google", headers=_admin_headers()
    )
    assert deleted.status_code == 200
    assert await storage.list_server_ai_providers() == []


# --- Chat sessions use the stored server key (#347 review) ---


@pytest.mark.asyncio
async def test_chat_session_passes_stored_server_key(monkeypatch, tmp_path):
    from rootcoz.engine import chat

    create = AsyncMock(return_value="chat-sid")
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=None))
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(
        ai_client, "server_provider_key", AsyncMock(return_value=SECRET)
    )
    monkeypatch.setattr(chat, "create_session_safely", create)
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(chat, "get_sidecar_client", lambda: object())
    choice = ai_client._selected_credential_source.set("server")
    try:
        assert (
            await chat._create_chat_session(
                system_prompt="hi",
                ai_provider="openai",
                ai_model="m",
                repo_path=tmp_path,
            )
            == "chat-sid"
        )
        kwargs = create.call_args.kwargs
        assert kwargs["server_api_key"] == SECRET
        assert "api_key" not in kwargs  # never classified as a user key
    finally:
        ai_client._selected_credential_source.reset(choice)


@pytest.mark.asyncio
async def test_create_session_safely_keeps_server_source_for_server_key(
    tmp_path, monkeypatch
):
    import httpx

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat-server-key.db")
    await storage.init_db()
    seen = {}

    async def sidecar(request):
        if request.url.path == "/sessions":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"session_id": "server-keyed-sid"})
        return httpx.Response(200, json={"text": "reply"})

    client = ai_client.SidecarClient.__new__(ai_client.SidecarClient)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(sidecar), base_url="http://sidecar"
    )
    token = ai_client.ai_username.set("admin")
    try:
        assert (
            await ai_client.create_session_safely(
                client,
                provider="openai",
                model="m",
                system_prompt="test",
                server_api_key=SECRET,
            )
            == "server-keyed-sid"
        )
        assert seen["body"]["api_key"] == SECRET
        # The server-keyed session is classified server-owned, exactly like
        # an ambient session — not as a user-credential session.
        assert (
            await storage.get_ai_session_source("server-keyed-sid", "admin", "openai")
            == "server"
        )
    finally:
        ai_client.ai_username.reset(token)
        await client._client.aclose()


async def test_add_exact_match_wins_over_case_insensitive_fallback(client, monkeypatch):
    """Two registered providers differing only by case stay distinct (#347).

    Exact membership wins: a request for ``foo`` must verify and store
    against ``foo`` itself, never against the ``Foo`` that happens to be
    the first case-insensitive match.
    """
    _fake_catalog(
        monkeypatch,
        [
            {"provider": "Foo", "id": "foo-model"},
            {"provider": "foo", "id": "foo-model"},
        ],
    )
    _mock_supported(monkeypatch, ["Foo", "foo"])
    verified = _mock_verify(monkeypatch, "accepted")
    response = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={"provider": "foo", "api_key": SECRET, "verification_model": "foo-model"},
    )
    assert response.status_code == 200
    # Verification ran against the exact registered spelling
    assert verified[0][0] == "foo"
    stored = await storage.list_server_ai_providers()
    assert [row["provider"] for row in stored] == ["foo"]
