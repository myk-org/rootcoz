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

    # Hermetic default: every add/rotate/refresh in this file reaches key-scoped
    # capture, and an unmocked ``models_for_api_key`` fires REAL discovery at
    # SIDECAR_URL — the dev compose publishes 9100, so tests would hit the live
    # sidecar. Tests that care about discovery results override this via
    # _mock_key_discovery (a later monkeypatch.setattr wins).
    async def _hermetic_discovery(provider, api_key):
        return {"modelListingSupported": False, "models": []}

    monkeypatch.setattr(ai_client, "models_for_api_key", _hermetic_discovery)
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


def _mock_key_discovery(monkeypatch, models, *, listing_supported=True):
    """Mock key-scoped discovery; ``models`` may be an exception to raise.

    The mock returns the normalized shape the real ``models_for_api_key``
    produces (id/name/provider/source/capabilities entries). Entries may
    carry ``"source": "manifest"`` to exercise manifest filtering.
    """
    calls = []

    async def fake(provider, key):
        calls.append((provider, key))
        if isinstance(models, Exception):
            raise models
        return {
            "modelListingSupported": listing_supported,
            "models": [
                {
                    "id": entry["id"],
                    "name": entry.get("name") or entry["id"],
                    "provider": provider,
                    "source": entry.get("source", "listing"),
                    "capabilities": entry.get("capabilities", {}),
                }
                for entry in models
            ],
        }

    monkeypatch.setattr(ai_client, "models_for_api_key", fake)
    return calls


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


async def test_key_scoped_models_storage_roundtrip(tmp_path, monkeypatch):
    """Key-scoped rows replace per provider and parse capabilities (#350)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    await storage.set_server_provider_key_models(
        "google",
        [
            {
                "id": "gemini-2.5-pro",
                "name": "Gemini Pro",
                "capabilities": {"inputTokenLimit": 1000000, "outputTokenLimit": 65536},
            },
            {"id": "gemini-3-pro"},
        ],
    )
    rows = await storage.list_server_provider_key_models()
    assert [(row["provider"], row["model"]) for row in rows] == [
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-3-pro"),
    ]
    assert rows[0]["name"] == "Gemini Pro"
    assert rows[0]["capabilities"] == {
        "inputTokenLimit": 1000000,
        "outputTokenLimit": 65536,
    }
    assert rows[1]["capabilities"] is None
    # Case-insensitive replace: a different-case spelling swaps, not adds
    # (the submitted spelling wins, matching set_server_provider_models).
    await storage.set_server_provider_key_models("Google", [{"id": "gemini-2.5-flash"}])
    rows = await storage.list_server_provider_key_models()
    assert [(row["provider"].lower(), row["model"]) for row in rows] == [
        ("google", "gemini-2.5-flash")
    ]
    # An empty list clears the rows — the catalog-fallback marker.
    await storage.set_server_provider_key_models("google", [])
    assert await storage.list_server_provider_key_models() == []
    # Provider deletion removes key-scoped rows with everything else.
    await storage.set_server_provider_key_models("google", [{"id": "gemini-2.5-pro"}])
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.delete_server_ai_provider("google")
    assert await storage.list_server_provider_key_models() == []


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


# --- Key-scoped model discovery (#350) ---


def _add_google_provider(client, monkeypatch, discovery_models, *, listing=True):
    """Add a verified google key with a given key-scoped listing."""
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, discovery_models, listing_supported=listing)
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
    return added


async def test_add_captures_key_scoped_models_and_restricts_exposure(
    client, monkeypatch
):
    """A supported listing becomes the offered set — catalog and beyond (#350)."""
    key_models = [
        {"id": "gemini-2.5-pro", "name": "Gemini Pro"},
        {
            "id": "gemini-3-pro",
            "capabilities": {"inputTokenLimit": 2000000, "outputTokenLimit": 65536},
        },
    ]
    _add_google_provider(client, monkeypatch, key_models)
    # The listing was captured verbatim, capabilities included.
    rows = await storage.list_server_provider_key_models()
    assert {(row["provider"], row["model"]) for row in rows} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-3-pro"),
    }
    # Seeding followed the key-scoped list: gemini-2.5-flash (catalog, not
    # key-accessible) is NOT selected; gemini-3-pro (key-accessible, not in
    # catalog) IS.
    selection = await storage.list_server_provider_models()
    assert {(row["provider"], row["model"]) for row in selection} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-3-pro"),
    }
    # The admin listing offers exactly the key-scoped set (+ stale
    # selections) and reports key_scoped; untouched providers keep the
    # catalog and key_scoped=False.
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    providers = {p["provider"]: p for p in listed.json()["providers"]}
    assert providers["google"]["key_scoped"] is True
    assert [m["id"] for m in providers["google"]["models"]] == [
        "gemini-2.5-pro",
        "gemini-3-pro",
    ]
    assert providers["openai"]["key_scoped"] is False
    assert [m["id"] for m in providers["openai"]["models"]] == ["gpt-5.6-luna"]
    # scoped_models presents the key-scoped set only — the catalog model
    # the key cannot use is gone from the server pool, the non-catalog
    # model is there with its discovery metadata, verified.
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert [m["id"] for m in scoped["google"]] == [
            "gemini-2.5-pro",
            "gemini-3-pro",
        ]
        gemini3 = next(m for m in scoped["google"] if m["id"] == "gemini-3-pro")
        assert gemini3["verified"] is True
        assert gemini3["name"] == "gemini-3-pro"
        assert gemini3["capabilities"] == {
            "inputTokenLimit": 2000000,
            "outputTokenLimit": 65536,
        }
    finally:
        ai_client.force_server_credentials.reset(token)
    await ai_client.require_selected_server_pair("google", "gemini-3-pro")


async def test_add_with_unsupported_listing_falls_back_to_catalog(client, monkeypatch):
    """No listing endpoint → no rows → full-catalog seed, unchanged behavior."""
    _add_google_provider(client, monkeypatch, [], listing=False)
    assert await storage.list_server_provider_key_models() == []
    selection = await storage.list_server_provider_models()
    assert {(row["provider"], row["model"]) for row in selection} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-2.5-flash"),
    }
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    providers = {p["provider"]: p for p in listed.json()["providers"]}
    assert providers["google"]["key_scoped"] is False
    assert [m["id"] for m in providers["google"]["models"]] == [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ]


async def test_rotate_replaces_key_scoped_rows_and_keeps_selection(client, monkeypatch):
    """Rotation refreshes the listing; the admin selection survives it."""
    _add_google_provider(
        client, monkeypatch, [{"id": "gemini-2.5-pro"}, {"id": "gemini-2.5-flash"}]
    )
    # Narrow the selection, then rotate to a key with a different listing.
    client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={
            "models": [
                {"id": "gemini-2.5-pro", "included": True, "free": False},
                {"id": "gemini-2.5-flash", "included": False, "free": False},
            ]
        },
    )
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, [{"id": "gemini-2.5-pro"}])
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
    rows = await storage.list_server_provider_key_models()
    assert [(row["provider"], row["model"]) for row in rows] == [
        ("google", "gemini-2.5-pro")
    ]
    # The selection was never reseeded: the stale survivor stays a row but
    # runtime filtering withdraws it from the server pool.
    selection = await storage.list_server_provider_models()
    assert {(row["provider"], row["model"]) for row in selection} == {
        ("google", "gemini-2.5-pro")
    }


async def test_refresh_models_endpoint(client, monkeypatch):
    """Refresh re-runs discovery with the stored key, no re-entry needed."""
    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])
    _mock_key_discovery(monkeypatch, [{"id": "gemini-2.5-pro"}, {"id": "gemini-3-pro"}])
    refreshed = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 200
    assert refreshed.json() == {"ok": True, "status": "supported", "model_count": 2}
    rows = await storage.list_server_provider_key_models()
    assert {(row["provider"], row["model"]) for row in rows} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-3-pro"),
    }
    # The add seeded a selection of {gemini-2.5-pro}; refresh never reseeds
    # an existing selection (an admin restriction survives), so the wider
    # listing is visible in the admin UI but not exposed until included.
    selection = await storage.list_server_provider_models()
    assert {(row["provider"], row["model"]) for row in selection} == {
        ("google", "gemini-2.5-pro")
    }
    # Unavailable discovery reports the degraded status and KEEPS the last
    # captured listing (#350 r1) — a sidecar hiccup must not revert the
    # provider to the static catalog.
    import httpx

    _mock_key_discovery(monkeypatch, httpx.ConnectError("sidecar down"))
    degraded = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "unavailable"
    rows = await storage.list_server_provider_key_models()
    assert {(row["provider"], row["model"]) for row in rows} == {
        ("google", "gemini-2.5-pro"),
        ("google", "gemini-3-pro"),
    }
    # The kept listing stays key-scoped, but the status records why the
    # models on screen are stale (#350 r2).
    providers = {
        p["provider"]: p
        for p in client.get(
            "/api/admin/server-providers", headers=_admin_headers()
        ).json()["providers"]
    }
    assert providers["google"]["key_scoped"] is True
    assert providers["google"]["key_scoped_status"] == "unavailable"
    # No stored key → 404 (provider must be key-supported to reach the
    # lookup); unsupported provider id → 400.
    _mock_supported(monkeypatch, ["google", "openai"])
    missing = client.post(
        "/api/admin/server-providers/openai/refresh-models",
        headers=_admin_headers(),
    )
    assert missing.status_code == 404
    unsupported = client.post(
        "/api/admin/server-providers/nosuch/refresh-models",
        headers=_admin_headers(),
    )
    assert unsupported.status_code == 400


async def test_refresh_never_reseeds_a_deliberately_empty_selection(
    client, monkeypatch
):
    """An admin who deselected every model keeps that choice on refresh (#350 r1)."""
    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])
    # Deselect everything — a managed provider with no selection rows
    # deliberately exposes nothing.
    client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={"models": [{"id": "gemini-2.5-pro", "included": False, "free": False}]},
    )
    assert await storage.list_server_provider_models() == []
    _mock_key_discovery(monkeypatch, [{"id": "gemini-2.5-pro"}, {"id": "gemini-3-pro"}])
    refreshed = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 200
    # The empty selection survived — nothing was silently re-included.
    assert await storage.list_server_provider_models() == []
    token = ai_client.force_server_credentials.set(True)
    try:
        assert "google" not in await ai_client.scoped_models()
    finally:
        ai_client.force_server_credentials.reset(token)


async def test_empty_supported_listing_exposes_nothing(client, monkeypatch):
    """A supported listing with zero models is "verified empty", not a fallback (#350 r1)."""
    _add_google_provider(client, monkeypatch, [])
    rows = await storage.list_server_provider_key_models()
    # A sentinel row marks the verified-empty listing.
    assert [(row["provider"], row["model"]) for row in rows] == [("google", "")]
    # The admin listing reports the provider key-scoped with no models.
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    providers = {p["provider"]: p for p in listed.json()["providers"]}
    assert providers["google"]["key_scoped"] is True
    assert [m["id"] for m in providers["google"]["models"]] == []
    # Nothing is exposed on server credentials — NOT the catalog fallback.
    token = ai_client.force_server_credentials.set(True)
    try:
        assert "google" not in await ai_client.scoped_models()
    finally:
        ai_client.force_server_credentials.reset(token)


async def test_manifest_only_models_are_not_key_verified(client, monkeypatch):
    """Manifest-only models from discovery never enter the server pool (#350 r1)."""
    _add_google_provider(
        client,
        monkeypatch,
        [
            {"id": "gemini-2.5-pro"},
            {"id": "manifest-only-model", "source": "manifest"},
        ],
    )
    rows = await storage.list_server_provider_key_models()
    assert [(row["provider"], row["model"]) for row in rows] == [
        ("google", "gemini-2.5-pro")
    ]


async def test_non_catalog_provider_with_listing_appears_in_scoped_models(
    tmp_path, monkeypatch
):
    """A keyed provider the static catalog lost still exposes its listing (#350 r1)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    # The catalog does NOT list this provider at all.
    _fake_catalog(monkeypatch, CATALOG)
    await storage.set_server_ai_credential("newprovider", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "newprovider", [{"id": "newmodel", "name": "New Model"}]
    )
    await storage.set_server_provider_models(
        "newprovider", [{"id": "newmodel"}], "admin"
    )
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert [m["id"] for m in scoped["newprovider"]] == ["newmodel"]
        assert scoped["newprovider"][0]["verified"] is True
    finally:
        ai_client.force_server_credentials.reset(token)


async def test_concurrent_rotation_supersedes_stale_capture(client, monkeypatch):
    """A capture whose key was rotated mid-discovery is discarded (#350 r1)."""
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    discovery_calls = _mock_key_discovery(monkeypatch, [{"id": "gemini-2.5-pro"}])
    # Rotate the credential while discovery is in flight — between the
    # capture's start and its post-discovery key check.
    inner_discovery = ai_client.models_for_api_key

    async def rotating_discovery(provider, key):
        await storage.set_server_ai_credential(provider, "newer-key-value-9", "admin")
        return await inner_discovery(provider, key)

    monkeypatch.setattr(ai_client, "models_for_api_key", rotating_discovery)
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
    # The stale capture was discarded: no rows were written for the old key.
    assert await storage.list_server_provider_key_models() == []
    assert discovery_calls == [("google", SECRET)]


def test_repeated_model_ids_dedupe_in_storage(tmp_path, monkeypatch):
    """A discovery response repeating an id must not fail the write (#350 r1)."""
    import asyncio

    async def run():
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
        await storage.init_db()
        await storage.set_server_provider_key_models(
            "google", [{"id": "m"}, {"id": "m"}, {"id": "m"}]
        )
        rows = await storage.list_server_provider_key_models()
        assert [(row["provider"], row["model"]) for row in rows] == [("google", "m")]

    asyncio.run(run())


async def test_key_scoped_entry_with_broken_limits_is_skipped(tmp_path, monkeypatch):
    """Declared-but-broken capability limits keep a model out of the pool (#350 r1)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    _fake_catalog(monkeypatch, CATALOG)
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "google",
        [
            {"id": "good-model"},
            {
                "id": "broken-limits",
                "capabilities": {"inputTokenLimit": 1000000},  # partial
            },
        ],
    )
    await storage.set_server_provider_models(
        "google", [{"id": "good-model"}, {"id": "broken-limits"}], "admin"
    )
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert [m["id"] for m in scoped["google"]] == ["good-model"]
    finally:
        ai_client.force_server_credentials.reset(token)


async def test_models_put_and_free_models_accept_key_scoped_pairs(client, monkeypatch):
    """A key-scoped model outside the catalog is a valid selection target."""
    _add_google_provider(
        client,
        monkeypatch,
        [{"id": "gemini-2.5-pro"}, {"id": "gemini-3-pro"}],
    )
    set_models = client.put(
        "/api/admin/server-providers/google/models",
        headers=_admin_headers(),
        json={"models": [{"id": "gemini-3-pro", "included": True, "free": False}]},
    )
    assert set_models.status_code == 200
    # The standalone free-models save accepts the same non-catalog pair.
    version = client.get("/api/admin/free-models", headers=_admin_headers())
    current = version.json()["models"]
    free = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={
            "models": current + [{"provider": "google", "model": "gemini-3-pro"}],
            "expected_version": version.json()["version"],
        },
    )
    assert free.status_code == 200


async def test_scoped_models_key_scoped_rows_restrict_and_extend(tmp_path, monkeypatch):
    """Storage-level check: rows replace the catalog set for the server pool."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    _fake_catalog(monkeypatch, CATALOG)
    # A stored key makes the provider managed (#347); the key-scoped rows
    # then define its model set. Rows without a key (not a real state —
    # capture always follows a key add) would leave the provider
    # unmanaged with its full catalog.
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "google", [{"id": "gemini-2.5-pro"}, {"id": "gemini-3-pro"}]
    )
    token = ai_client.force_server_credentials.set(True)
    try:
        # Managed by key rows alone (no selection): nothing exposed — the
        # same managed-empty rule the selection applies.
        assert "google" not in await ai_client.scoped_models()
        await storage.set_server_provider_models(
            "google", [{"id": "gemini-2.5-pro"}], "admin"
        )
        scoped = await ai_client.scoped_models()
        # Selection filters the key-scoped set; the catalog-only model
        # (gemini-2.5-flash) is gone even though it is selected-eligible.
        assert [m["id"] for m in scoped["google"]] == ["gemini-2.5-pro"]
        # Untouched providers keep their full catalog.
        assert [m["id"] for m in scoped["openai"]] == ["gpt-5.6-luna"]
    finally:
        ai_client.force_server_credentials.reset(token)


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


# --- Credential generation binding and capture outcomes (#350 r2) ---


async def test_stale_capture_write_is_generation_gated(tmp_path, monkeypatch):
    """Storage: a row write bound to a moved-on generation writes nothing.

    Every credential write bumps the generation — including a rotation to
    the same key value and a removal — so a capture that lost the race can
    never leave one key wearing another key's (or a removed key's) listing.
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-gen.db")
    await storage.init_db()
    await storage.set_server_ai_credential("google", SECRET, "admin")
    generation = await storage.get_server_credential_generation("google")
    # Same-key rotation mid-capture: the key check alone would pass, the
    # generation must not.
    await storage.set_server_ai_credential("google", SECRET, "admin")
    assert await storage.get_server_credential_generation("google") == generation + 1
    assert (
        await storage.set_server_provider_key_models(
            "google", [{"id": "m"}], expected_generation=generation
        )
        is False
    )
    assert await storage.list_server_provider_key_models() == []
    # A removal mid-capture supersedes the same way — no resurrection.
    await storage.set_server_ai_credential("google", SECRET, "admin")
    generation = await storage.get_server_credential_generation("google")
    await storage.delete_server_ai_provider("google")
    assert (
        await storage.set_server_provider_key_models(
            "google", [{"id": "m"}], expected_generation=generation
        )
        is False
    )
    assert await storage.list_server_provider_key_models() == []
    # The current generation writes and records the capture outcome.
    await storage.set_server_ai_credential("google", SECRET, "admin")
    generation = await storage.get_server_credential_generation("google")
    assert (
        await storage.set_server_provider_key_models(
            "google",
            [{"id": "m"}],
            expected_generation=generation,
            status="supported",
        )
        is True
    )
    rows = await storage.list_server_ai_providers()
    assert rows[0]["key_capture_status"] == "supported"
    # Status-only write (kept listing) leaves the rows alone.
    assert (
        await storage.set_server_provider_key_capture_status(
            "google", "unavailable", expected_generation=generation
        )
        is True
    )
    assert [
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_key_models()
    ] == [("google", "m")]
    assert (await storage.list_server_ai_providers())[0]["key_capture_status"] == (
        "unavailable"
    )


async def test_concurrent_same_key_rotation_supersedes_stale_capture(
    client, monkeypatch
):
    """A same-value re-store mid-discovery still discards the stale capture.

    The old key-equality check let this capture through (same key value);
    only the generation binding — every credential write bumps it — makes
    the overlapping write lose (#350 r2).
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, [{"id": "gemini-2.5-pro"}])
    inner_discovery = ai_client.models_for_api_key

    async def rotating_discovery(provider, key):
        # Re-store the SAME key value while discovery is in flight.
        await storage.set_server_ai_credential(provider, key, "admin")
        return await inner_discovery(provider, key)

    monkeypatch.setattr(ai_client, "models_for_api_key", rotating_discovery)
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
    # The stale capture was discarded: no rows were written.
    assert await storage.list_server_provider_key_models() == []


async def test_rotate_with_failing_discovery_clears_previous_key_rows(
    client, monkeypatch
):
    """A failed discovery after rotation never exposes the old key's listing.

    The rotated key's access is unknown, so the previous key's rows must be
    cleared (static-catalog fallback) instead of surviving as the new key's
    verified listing — including models the static catalog does not even
    list (#350 r2).
    """
    import httpx

    # The old key's listing includes a model the catalog lacks.
    _add_google_provider(
        client, monkeypatch, [{"id": "gemini-2.5-pro"}, {"id": "gemini-3-pro"}]
    )
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert "gemini-3-pro" in [m["id"] for m in scoped["google"]]
    finally:
        ai_client.force_server_credentials.reset(token)

    # Rotate to a different key whose discovery fails.
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, httpx.ConnectError("sidecar down"))
    rotated = client.post(
        "/api/admin/server-providers/google/rotate-key",
        headers=_admin_headers(),
        json={
            "provider": "google",
            "api_key": "new-key-with-failing-discovery-1",  # pragma: allowlist secret
            "verification_model": "gemini-2.5-pro",
        },
    )
    assert rotated.status_code == 200
    assert rotated.json()["ok"] is True
    # The old key's rows are cleared...
    assert await storage.list_server_provider_key_models() == []
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    providers = {p["provider"]: p for p in listed.json()["providers"]}
    assert providers["google"]["key_scoped"] is False
    assert providers["google"]["key_scoped_status"] == "unavailable"
    # ...and its non-catalog model is no longer exposed at runtime: the
    # provider falls back to the catalog (the stale selection survivor is
    # ignored, so only the catalog model remains).
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert [m["id"] for m in scoped["google"]] == ["gemini-2.5-pro"]
    finally:
        ai_client.force_server_credentials.reset(token)

    # A refresh that keeps failing reports unavailable but never clears the
    # captured rows once one exists (key unchanged — keep_on_failure).
    _mock_key_discovery(monkeypatch, [{"id": "gemini-2.5-pro"}])
    refreshed = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.json()["status"] == "supported"
    _mock_key_discovery(monkeypatch, httpx.ConnectError("sidecar down"))
    degraded = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert degraded.json()["status"] == "unavailable"
    rows = await storage.list_server_provider_key_models()
    assert [(row["provider"], row["model"]) for row in rows] == [
        ("google", "gemini-2.5-pro")
    ]


async def test_list_reports_key_scoped_status_per_outcome(client, monkeypatch):
    """GET exposes the last capture outcome for every keyed provider (#350 r2).

    ``unsupported`` and ``unavailable`` both leave no rows; the persisted
    status is what lets the UI tell a missing listing endpoint from a
    failed discovery.
    """
    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])

    def google_entry():
        providers = {
            p["provider"]: p
            for p in client.get(
                "/api/admin/server-providers", headers=_admin_headers()
            ).json()["providers"]
        }
        return providers["google"]

    entry = google_entry()
    assert entry["key_scoped"] is True
    assert entry["key_scoped_status"] == "supported"
    # A provider that never captured (no key) reports no status.
    providers = {
        p["provider"]: p
        for p in client.get(
            "/api/admin/server-providers", headers=_admin_headers()
        ).json()["providers"]
    }
    assert providers["openai"]["key_scoped_status"] is None

    # Refresh to an unsupported listing: rows cleared, status follows.
    _mock_key_discovery(monkeypatch, [], listing_supported=False)
    client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    entry = google_entry()
    assert entry["key_scoped"] is False
    assert entry["key_scoped_status"] == "unsupported"

    # Refresh with discovery down: status unavailable.
    import httpx

    _mock_key_discovery(monkeypatch, httpx.ConnectError("sidecar down"))
    client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    entry = google_entry()
    assert entry["key_scoped"] is False
    assert entry["key_scoped_status"] == "unavailable"


async def test_non_catalog_provider_keeps_stored_casing(client, monkeypatch):
    """A mixed-case custom provider id survives to the emitted entries (#350 r2).

    pi-config resolves session providers by their exact id, so a stored
    ``MyGateway`` must never be offered as ``mygateway`` — in the admin
    listing or the scoped-models pool.
    """
    _fake_catalog(monkeypatch, CATALOG)  # MyGateway is NOT in the catalog
    _mock_supported(monkeypatch, [])
    await storage.set_server_ai_credential("MyGateway", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "MyGateway", [{"id": "gw-model", "name": "Gateway Model"}]
    )
    await storage.set_server_provider_models("MyGateway", [{"id": "gw-model"}], "admin")
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    providers = {p["provider"]: p for p in listed.json()["providers"]}
    assert "MyGateway" in providers
    assert "mygateway" not in providers
    assert [m["id"] for m in providers["MyGateway"]["models"]] == ["gw-model"]
    token = ai_client.force_server_credentials.set(True)
    try:
        scoped = await ai_client.scoped_models()
        assert "MyGateway" in scoped
        assert "mygateway" not in scoped
        assert scoped["MyGateway"][0]["verified"] is True
    finally:
        ai_client.force_server_credentials.reset(token)
