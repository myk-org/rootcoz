"""Admin-managed server provider credentials and model selection (#347).

Covers storage (encryption at rest, per-provider free-pair sync), the admin
endpoints (verification-gated add/rotate, catalog-validated model selection,
delete warnings), the scoped-models server pool, and per-session apiKey
injection with unchanged credential-source semantics.
"""

import contextlib
import json
import logging
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pi_sidecar_client import AIResult

from rootcoz import ai_client, storage
from rootcoz import main as rootcoz_main
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
    # Hermetic defaults for the deployment-credential capture (#372): no agent
    # dir manifest from the host, no TTL carried over from another test, and no
    # startup preload racing a test's own manifest/call-count assertions.
    monkeypatch.delenv("PI_SIDECAR_AGENT_DIR", raising=False)
    monkeypatch.setattr("rootcoz.main._deployment_listing_state", None)
    monkeypatch.setattr("rootcoz.main._safe_preload_cursor_models", AsyncMock())
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _hermetic_deployment_env(monkeypatch):
    """No host ambient key or catalog snapshot leaks into a test (#376).

    ``deployment_provider_credentials()`` resolves ambient API keys for the
    providers in the last-known catalog, so a developer's own exports (and the
    shared test catalog) would turn a provider capturable behind a test's
    back. Tests that need either set it themselves (``_register_providers``,
    ``monkeypatch.setenv``). The default is a *warm but empty* catalog — what
    a deployment has after any successful refresh; the cold-cache case
    (``None``, unknown) is set explicitly by the test that covers it.
    """
    monkeypatch.setattr(ai_client, "_model_catalog_cache", [])
    for ambient_var in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "MISTRAL_API_KEY",
    ):
        monkeypatch.delenv(ambient_var, raising=False)
    yield


def _admin_headers():
    return {"Authorization": "Bearer sp-bootstrap-key"}


@contextlib.contextmanager
def _captured_logs(caplog, *names, level=logging.WARNING):
    """Collect records from rootcoz module loggers (they set propagate=False)."""
    module_loggers = [logging.getLogger(name) for name in names]
    previous_levels = [module_logger.level for module_logger in module_loggers]
    for module_logger in module_loggers:
        # The app configures these loggers at LOG_LEVEL, so a DEBUG assertion
        # would see nothing without lowering the logger itself (caplog's level
        # only covers the handler).
        module_logger.setLevel(level)
        module_logger.addHandler(caplog.handler)
        # The app's DuplicateFilter swallows a consecutive repeat of the same
        # message template, so a warning an earlier test already logged would
        # never reach caplog. Clear that state for the assertion below.
        for log_filter in module_logger.filters:
            if hasattr(log_filter, "last_log"):
                log_filter.last_log = ("", 0, "")
    try:
        with caplog.at_level(level):
            yield caplog
    finally:
        for module_logger, previous_level in zip(
            module_loggers, previous_levels, strict=True
        ):
            module_logger.setLevel(previous_level)
            module_logger.removeHandler(caplog.handler)


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


async def test_guarded_write_skips_when_a_credential_is_stored(tmp_path, monkeypatch):
    """Storage: the deployment write guard is part of the row transaction.

    A deployment capture writes without a generation binding (it has no
    credential row), so "this provider still has no stored key" must be
    tested inside the same transaction as the replacement — a check in the
    caller leaves a window a concurrent add slips through (#376).
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-guard.db")
    await storage.init_db()
    await storage.set_server_ai_credential("openai", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "openai", [{"id": "ambient-model"}], status="supported"
    )
    assert (
        await storage.set_server_provider_key_models(
            "openai",
            [{"id": "deployment-model"}],
            status="supported",
            only_without_stored_credential=True,
        )
        is False
    )
    # Rows and capture status both untouched: nothing was written at all.
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["ambient-model"]
    assert (await storage.list_server_ai_providers())[0]["key_capture_status"] == (
        "supported"
    )
    # The same write succeeds once the credential is gone — the guard is the
    # only reason it was rejected.
    await storage.delete_server_ai_provider("openai")
    assert (
        await storage.set_server_provider_key_models(
            "openai",
            [{"id": "deployment-model"}],
            status="supported",
            only_without_stored_credential=True,
        )
        is True
    )


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
        ("post", "/api/admin/server-providers/google/store-deployment-key"),
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


async def test_list_includes_registry_only_provider(client, monkeypatch):
    """A registered key-capable provider with no catalog model is listed (#378).

    Observed live: the sidecar registered ~30 key-capable providers but the
    catalog held one, so the UI's provider picker could not offer the rest and
    an admin could never store a key for them.
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google", "openai", "deepseek"])
    response = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert response.status_code == 200
    providers = {p["provider"]: p for p in response.json()["providers"]}
    assert sorted(providers) == ["deepseek", "google", "openai"]
    entry = providers["deepseek"]
    assert entry["models"] == []
    assert entry["supports_key"] is True
    assert entry["has_key"] is False
    assert entry["verified"] is False
    assert entry["managed"] is False
    assert entry["key_scoped"] is False
    assert entry["key_scoped_source"] is None
    assert entry["can_refresh_models"] is False


async def test_list_excludes_registered_provider_without_key_support(
    client, monkeypatch
):
    """``supportsSessionApiKey: false`` never reaches the listing (#378)."""
    _fake_catalog(monkeypatch, CATALOG)
    # github-copilot is registered by the sidecar but not key-capable, so the
    # capability answer the endpoint unions is already the gated set.
    _mock_supported(monkeypatch, ["google", "openai"])
    response = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert response.status_code == 200
    assert sorted(p["provider"] for p in response.json()["providers"]) == [
        "google",
        "openai",
    ]


async def test_list_single_entry_for_catalog_and_registered_provider(
    client, monkeypatch
):
    """A provider in both the catalog and the registry keeps one keyed entry."""
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google", "openai"])
    await storage.set_server_ai_credential("google", SECRET, "admin")
    response = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert response.status_code == 200
    providers = response.json()["providers"]
    assert [p["provider"] for p in providers] == ["google", "openai"]
    entry = providers[0]
    assert entry["has_key"] is True
    assert entry["supports_key"] is True
    assert [m["id"] for m in entry["models"]] == [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ]


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


# --- Overlapping credential writes and capture bindings (#350 r3) ---


async def test_credential_upsert_returns_its_assigned_generation(tmp_path, monkeypatch):
    """Storage: the upsert returns the generation it assigned (#350 r3).

    That returned value — produced inside the write transaction — is the
    only generation a capture may bind to; a separate later read could
    observe a concurrent rotation's generation instead.
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-gen3.db")
    await storage.init_db()
    first = await storage.set_server_ai_credential("google", SECRET, "admin")
    second = await storage.set_server_ai_credential("google", SECRET, "admin")
    assert second == first + 1
    assert await storage.get_server_credential_generation("google") == second
    # A capture bound to the older generation writes nothing even though
    # the key value is unchanged.
    assert (
        await storage.set_server_provider_key_models(
            "google", [{"id": "m"}], expected_generation=first
        )
        is False
    )
    assert await storage.list_server_provider_key_models() == []


async def test_same_key_rotation_cannot_lend_its_generation(client, monkeypatch):
    """An overlapping same-key rotation supersedes the older capture (r3).

    The rotation lands between the credential write and the point where
    the old code read the generation back separately — a capture must stay
    bound to the generation its OWN upsert assigned, never the newer one a
    later read might observe, so the older discovery cannot replace the
    newer rotation's snapshot when it finishes last.
    """
    _fake_catalog(monkeypatch, CATALOG)
    _mock_supported(monkeypatch, ["google"])
    _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, [{"id": "gemini-2.5-pro"}])
    real_set = storage.set_server_ai_credential

    async def set_then_overlap(provider, key, updated_by):
        generation = await real_set(provider, key, updated_by)
        # An overlapping rotation to the SAME key lands before the endpoint
        # could read the generation back.
        await real_set(provider, key, "other-admin")
        return generation

    monkeypatch.setattr(storage, "set_server_ai_credential", set_then_overlap)
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
    # The older capture (bound to its own generation) was discarded.
    assert await storage.list_server_provider_key_models() == []


async def test_readded_key_never_reuses_removed_generation(tmp_path, monkeypatch):
    """A re-added key cannot accept a removed key's in-flight capture (r3).

    The credential version is a durable global sequence — an INSERT never
    falls back to the schema default — so a capture still bound to the
    removed credential's generation fails the guard after a re-add of the
    same key value.
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-readd.db")
    await storage.init_db()
    first = await storage.set_server_ai_credential("google", SECRET, "admin")
    # A second provider keeps the table max above google's generation.
    second = await storage.set_server_ai_credential("openai", SECRET, "admin")
    await storage.delete_server_ai_provider("google")
    readded = await storage.set_server_ai_credential("google", SECRET, "admin")
    assert readded not in (first, second)
    assert readded > second
    # The in-flight capture from the removed credential cannot write.
    assert (
        await storage.set_server_provider_key_models(
            "google", [{"id": "m"}], expected_generation=first
        )
        is False
    )
    assert await storage.list_server_provider_key_models() == []


async def test_rotation_landing_before_capture_write_reports_superseded(
    client, monkeypatch
):
    """A skipped generation-bound write reports superseded, not success (r3).

    A rotation landing between the superseded check and the conditional
    write makes the write itself return False — the refresh must reply
    ``superseded`` (nothing was stored), never ``supported`` with a model
    count.
    """
    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])
    real_set_models = storage.set_server_provider_key_models
    real_set_credential = storage.set_server_ai_credential

    async def rotate_then_write(provider, models, **kwargs):
        # The rotation lands after the capture's superseded check passed.
        await real_set_credential(provider, "rotated-mid-capture-1", "admin")
        return await real_set_models(provider, models, **kwargs)

    monkeypatch.setattr(storage, "set_server_provider_key_models", rotate_then_write)
    refreshed = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["status"] == "superseded"
    assert body["model_count"] == 0
    # The stale refresh stored nothing: the captured rows are untouched.
    rows = await storage.list_server_provider_key_models()
    assert [(row["provider"], row["model"]) for row in rows] == [
        ("google", "gemini-2.5-pro")
    ]


async def test_rotation_landing_before_keep_rows_status_write_reports_superseded(
    client, monkeypatch
):
    """A skipped keep-rows status write also reports superseded (r3).

    The keep_on_failure branch (failed discovery, rows kept) must not
    claim ``unavailable`` when its status write was skipped by a rotation.
    """
    import httpx

    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])
    _mock_key_discovery(monkeypatch, httpx.ConnectError("sidecar down"))
    real_status = storage.set_server_provider_key_capture_status
    real_set_credential = storage.set_server_ai_credential

    async def rotate_then_status(provider, status, **kwargs):
        # The rotation lands after the capture's superseded check passed.
        await real_set_credential(provider, "rotated-mid-failure-1", "admin")
        return await real_status(provider, status, **kwargs)

    monkeypatch.setattr(
        storage, "set_server_provider_key_capture_status", rotate_then_status
    )
    degraded = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "superseded"
    # The status write was skipped: the recorded outcome is untouched.
    listed = await storage.list_server_ai_providers()
    assert listed[0]["key_capture_status"] == "supported"


async def test_removal_mid_capture_leaves_no_orphan_rows(client, monkeypatch):
    """A capture whose provider was removed never resurrects rows (r3).

    The removal deletes the key, selection, and key-scoped rows
    atomically; the in-flight capture must lose and leave the deletion
    standing — no orphaned models offered for a provider with no key.
    """
    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])
    inner_discovery = ai_client.models_for_api_key

    async def removing_discovery(provider, key):
        # The provider is removed while discovery is in flight.
        await storage.delete_server_ai_provider(provider)
        return await inner_discovery(provider, key)

    monkeypatch.setattr(ai_client, "models_for_api_key", removing_discovery)
    refreshed = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "superseded"
    assert await storage.list_server_provider_key_models() == []
    assert await storage.get_server_ai_credential("google") is None


async def test_refresh_never_binds_capture_to_missing_credential(client, monkeypatch):
    """A provider removed before the generation read 404s instead (r3).

    A None generation must never become an unbound capture — that write
    would re-insert the rows the removal just deleted.
    """
    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])
    real_generation = storage.get_server_credential_generation

    async def remove_then_read(provider):
        # The removal lands between the key read and the generation read.
        await storage.delete_server_ai_provider(provider)
        return await real_generation(provider)

    monkeypatch.setattr(storage, "get_server_credential_generation", remove_then_read)
    refreshed = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 404
    assert refreshed.json()["detail"] == "Provider 'google' has no stored key"
    assert await storage.list_server_provider_key_models() == []


async def test_failed_refresh_with_snapshot_stays_key_scoped(client, monkeypatch):
    """A failed refresh keeps the snapshot AND reports unavailable (r3).

    The API must surface ``key_scoped: true`` together with
    ``key_scoped_status: "unavailable"`` — that combination is what lets
    the card show a stale-listing hint instead of hiding the failure.
    """
    import httpx

    _add_google_provider(client, monkeypatch, [{"id": "gemini-2.5-pro"}])
    _mock_key_discovery(monkeypatch, httpx.ConnectError("sidecar down"))
    degraded = client.post(
        "/api/admin/server-providers/google/refresh-models",
        headers=_admin_headers(),
    )
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "unavailable"
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    providers = {p["provider"]: p for p in listed.json()["providers"]}
    assert providers["google"]["key_scoped"] is True
    assert providers["google"]["key_scoped_status"] == "unavailable"


# --- Deployment-env provider credentials (#372) ---

DEPLOYMENT_KEY = "deployment-key-7d1f"  # pragma: allowlist secret
DEPLOYMENT_ENV = "MYGATEWAY_API_KEY"


def _point_agent_dir(monkeypatch, tmp_path, providers):
    """Write a sidecar ``models.json`` manifest and point the agent dir at it."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text(json.dumps({"providers": providers}))
    monkeypatch.setenv("PI_SIDECAR_AGENT_DIR", str(agent_dir))
    return agent_dir


def _deployment_provider(api_key=f"${DEPLOYMENT_ENV}"):
    return {"apiKey": api_key, "trustKeyScopedListing": True}


@pytest.mark.parametrize("reference", [f"${DEPLOYMENT_ENV}", f"${{{DEPLOYMENT_ENV}}}"])
def test_deployment_credentials_resolve_both_env_reference_forms(
    tmp_path, monkeypatch, reference
):
    """``$VAR`` and ``${VAR}`` both resolve from the deployment env."""
    _point_agent_dir(
        monkeypatch,
        tmp_path,
        {"mygateway": _deployment_provider(reference)},
    )
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    assert ai_client.deployment_provider_credentials() == {"mygateway": DEPLOYMENT_KEY}


def test_deployment_credentials_skip_unset_unflagged_and_literal(tmp_path, monkeypatch):
    """Only env-referenced, flagged, resolvable keys are returned."""
    _point_agent_dir(
        monkeypatch,
        tmp_path,
        {
            "mygateway": _deployment_provider(),
            "unset": _deployment_provider("$NOT_EXPORTED_KEY"),
            "empty": _deployment_provider("$EMPTY_KEY"),
            "unflagged": {"apiKey": f"${DEPLOYMENT_ENV}"},
            "literal": {
                "apiKey": "plain-text-key",  # pragma: allowlist secret
                "trustKeyScopedListing": True,
            },
        },
    )
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    monkeypatch.setenv("EMPTY_KEY", "   ")
    monkeypatch.delenv("NOT_EXPORTED_KEY", raising=False)
    assert ai_client.deployment_provider_credentials() == {"mygateway": DEPLOYMENT_KEY}
    # No agent dir at all (the default deployment): nothing to resolve.
    monkeypatch.delenv("PI_SIDECAR_AGENT_DIR")
    assert ai_client.deployment_provider_credentials() == {}


def test_deployment_credentials_missing_manifest_is_unknown(tmp_path, monkeypatch):
    """A configured agent dir without a readable manifest is not "nothing" (#376).

    An atomic replace or a mount blip must report ``None`` (unknown), never the
    empty resolve that would sweep every captured deployment listing away.
    """
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    (agent_dir / "models.json").unlink()
    assert ai_client.deployment_provider_credentials() is None
    # A configured dir that is not there yet (mount still coming up) is unknown too.
    monkeypatch.setenv("PI_SIDECAR_AGENT_DIR", str(tmp_path / "not-mounted"))
    assert ai_client.deployment_provider_credentials() is None
    # No agent dir configured: nothing to read, so an honest empty resolve.
    monkeypatch.delenv("PI_SIDECAR_AGENT_DIR")
    assert ai_client.deployment_provider_credentials() == {}


async def test_deployment_capture_lists_all_models_and_keeps_selection(
    client, monkeypatch, tmp_path
):
    """A deployment-credential provider lists its live listing, not the manifest."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(
        monkeypatch,
        [{"provider": "mygateway", "id": "mygateway/gpt-4o", "name": "GPT-4o"}],
    )
    calls = _mock_key_discovery(
        monkeypatch,
        [
            {"id": "mygateway/gpt-4o"},
            {"id": "mygateway/llama-4"},
            {"id": "mygateway/manifest-only", "source": "manifest"},
        ],
    )
    # An admin selection on the provider must survive the capture untouched.
    await storage.set_server_provider_models(
        "mygateway", [{"id": "mygateway/gpt-4o"}], "admin"
    )
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    provider = {p["provider"]: p for p in listed.json()["providers"]}["mygateway"]
    assert calls == [("mygateway", DEPLOYMENT_KEY)]
    assert provider["has_key"] is False
    assert provider["key_scoped"] is True
    assert provider["key_scoped_source"] == "deployment"
    # The live listing is the offered set; manifest-only models never are.
    assert [m["id"] for m in provider["models"]] == [
        "mygateway/gpt-4o",
        "mygateway/llama-4",
    ]
    assert [m["id"] for m in provider["models"] if m["included"]] == [
        "mygateway/gpt-4o"
    ]
    assert {
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_models()
    } == {("mygateway", "mygateway/gpt-4o")}


async def test_deployment_capture_is_ttl_cached_and_refresh_forces_it(
    client, monkeypatch, tmp_path
):
    """Repeated page loads reuse the capture; a refresh re-queries the sidecar."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    _mock_supported(monkeypatch, ["mygateway"])
    calls = _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())
    client.get("/api/admin/server-providers", headers=_admin_headers())
    assert len(calls) == 1
    refreshed = client.post(
        "/api/admin/server-providers/mygateway/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 200
    assert refreshed.json() == {"ok": True, "status": "supported", "model_count": 1}
    assert len(calls) == 2


async def test_deployment_capture_failure_keeps_prior_listing(
    client, monkeypatch, tmp_path
):
    """A failed or unsupported re-capture never downgrades the listing (#372)."""
    import httpx

    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    _mock_key_discovery(
        monkeypatch, [{"id": "mygateway/gpt-4o"}, {"id": "mygateway/llama-4"}]
    )
    client.get("/api/admin/server-providers", headers=_admin_headers())

    async def _listed_models():
        listed = client.get("/api/admin/server-providers", headers=_admin_headers())
        assert listed.status_code == 200
        provider = {p["provider"]: p for p in listed.json()["providers"]}["mygateway"]
        return [m["id"] for m in provider["models"]]

    # Sidecar down: the captured rows survive and the page keeps working.
    monkeypatch.setattr("rootcoz.main._deployment_listing_state", None)
    _mock_key_discovery(monkeypatch, httpx.ConnectError("sidecar down"))
    assert await _listed_models() == ["mygateway/gpt-4o", "mygateway/llama-4"]
    # Listing endpoint withdrawn: still no downgrade to the static manifest.
    monkeypatch.setattr("rootcoz.main._deployment_listing_state", None)
    _mock_key_discovery(monkeypatch, [], listing_supported=False)
    assert await _listed_models() == ["mygateway/gpt-4o", "mygateway/llama-4"]


async def test_stale_deployment_listing_cleared_when_env_var_unset(
    client, monkeypatch, tmp_path
):
    """A removed deployment credential clears its captured rows (#372)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(
        monkeypatch, [{"provider": "mygateway", "id": "mygateway/catalog-only"}]
    )
    _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["mygateway/gpt-4o"]

    # The deployment credential disappears (env var removed, no stored key).
    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    assert await rootcoz_main.capture_deployment_provider_listings(force=True) == {}

    # No credential can back the listing, so the frozen rows are cleared.
    assert await storage.list_server_provider_key_models() == []
    provider = {
        p["provider"]: p
        for p in client.get(
            "/api/admin/server-providers", headers=_admin_headers()
        ).json()["providers"]
    }["mygateway"]
    assert provider["has_key"] is False
    assert provider["key_scoped"] is False
    assert provider["key_scoped_source"] is None
    # Falls back to the static catalog model instead of the dead listing.
    assert [m["id"] for m in provider["models"]] == ["mygateway/catalog-only"]


async def test_stale_deployment_sweep_never_clears_stored_key_rows(
    client, monkeypatch, tmp_path
):
    """A stored key owns its rows; only the credential-less provider is swept."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _fake_catalog(
        monkeypatch,
        [
            {"provider": "mygateway", "id": "mygateway/gpt-4o"},
            {"provider": "google", "id": "gemini-2.5-pro"},
        ],
    )
    _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    await storage.set_server_ai_credential("google", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "google", [{"id": "gemini-2.5-pro"}], status="supported"
    )
    client.get("/api/admin/server-providers", headers=_admin_headers())

    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    await rootcoz_main.capture_deployment_provider_listings(force=True)

    assert [
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_key_models()
    ] == [("google", "gemini-2.5-pro")]
    google = {
        p["provider"]: p
        for p in client.get(
            "/api/admin/server-providers", headers=_admin_headers()
        ).json()["providers"]
    }["google"]
    assert google["key_scoped"] is True
    assert google["key_scoped_source"] == "stored"


async def test_deployment_capture_skips_provider_with_stored_key(
    client, monkeypatch, tmp_path
):
    """A stored key owns its rows (#376): the deployment key never overwrites them."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    await storage.set_server_ai_credential("mygateway", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "mygateway", [{"id": "mygateway/stored-only"}], status="supported"
    )
    calls = _mock_key_discovery(monkeypatch, [{"id": "mygateway/deployment-only"}])

    listed = client.get("/api/admin/server-providers", headers=_admin_headers())

    assert listed.status_code == 200
    # Not one discovery ran with the deployment key for a stored-key provider.
    assert calls == []
    rows = await storage.list_server_provider_key_models()
    assert [(row["provider"], row["model"]) for row in rows] == [
        ("mygateway", "mygateway/stored-only")
    ]
    provider = {p["provider"]: p for p in listed.json()["providers"]}["mygateway"]
    assert provider["has_key"] is True
    assert provider["key_scoped"] is True
    assert provider["key_scoped_status"] == "supported"
    assert provider["key_scoped_source"] == "stored"


async def test_deployment_credential_change_bypasses_ttl(client, monkeypatch, tmp_path):
    """A key rotate captures at once; an unchanged map keeps the TTL (#376)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    calls = _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())
    client.get("/api/admin/server-providers", headers=_admin_headers())
    assert len(calls) == 1  # unchanged map inside the TTL: served from cache

    rotated = "rotated-deployment-key"  # pragma: allowlist secret
    monkeypatch.setenv(DEPLOYMENT_ENV, rotated)
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert listed.status_code == 200
    assert len(calls) == 2  # changed credential: captured immediately
    assert calls[-1] == ("mygateway", rotated)
    client.get("/api/admin/server-providers", headers=_admin_headers())
    assert len(calls) == 2  # unchanged again: back to the TTL

    # The variable disappearing also changes the map: the sweep runs at once.
    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    assert await rootcoz_main.capture_deployment_provider_listings() == {}
    assert await storage.list_server_provider_key_models() == []


async def test_manifest_read_failure_keeps_listing_and_skips_sweep(
    client, monkeypatch, tmp_path, caplog
):
    """An unreadable manifest is not "nothing eligible" (#376): keep the rows."""
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(
        monkeypatch, [{"provider": "mygateway", "id": "mygateway/catalog-only"}]
    )
    _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())

    # Credential gone AND manifest unreadable (mount blip / rewrite): an empty
    # resolve here would sweep the captured listing away.
    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    (agent_dir / "models.json").write_text("{ half-written")
    with _captured_logs(caplog, "rootcoz.ai_client", "rootcoz.main") as captured:
        listed = client.get("/api/admin/server-providers", headers=_admin_headers())

    assert listed.status_code == 200
    assert [
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_key_models()
    ] == [("mygateway", "mygateway/gpt-4o")]
    provider = {p["provider"]: p for p in listed.json()["providers"]}["mygateway"]
    assert provider["key_scoped"] is True
    assert "manifest unreadable" in captured.text


async def test_sweep_storage_failure_keeps_rows_and_endpoint_ok(
    client, monkeypatch, tmp_path, caplog
):
    """A storage error inside the sweep never 500s the listing (#376)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())

    async def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.delenv(DEPLOYMENT_ENV, raising=False)
    monkeypatch.setattr(storage, "set_server_provider_key_models", _boom)
    with _captured_logs(caplog, "rootcoz.main") as captured:
        listed = client.get("/api/admin/server-providers", headers=_admin_headers())

    assert listed.status_code == 200
    provider = {p["provider"]: p for p in listed.json()["providers"]}["mygateway"]
    assert provider["key_scoped"] is True
    assert [m["id"] for m in provider["models"]] == ["mygateway/gpt-4o"]
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["mygateway/gpt-4o"]
    assert "Unable to clear the stale deployment" in captured.text
    assert "provider=mygateway" in captured.text
    assert "database is locked" in captured.text


async def test_sweep_failure_names_provider_and_keeps_unprocessed_rows(
    client, monkeypatch, caplog
):
    """A mid-sweep failure reports the provider and which rows were kept (#376)."""
    _fake_catalog(monkeypatch, [])
    await storage.set_server_provider_key_models(
        "alpha", [{"id": "alpha/model"}], status=None
    )
    await storage.set_server_provider_key_models(
        "zeta", [{"id": "zeta/model"}], status=None
    )
    real_set = storage.set_server_provider_key_models

    async def _fail_for_zeta(provider, models, **kwargs):
        if provider == "zeta":
            raise sqlite3.OperationalError("database is locked")
        return await real_set(provider, models, **kwargs)

    monkeypatch.setattr(storage, "set_server_provider_key_models", _fail_for_zeta)
    with _captured_logs(caplog, "rootcoz.main") as captured:
        await rootcoz_main.capture_deployment_provider_listings(force=True)

    # The provider being processed and the failure itself are both named...
    assert "provider=zeta" in captured.text
    assert "database is locked" in captured.text
    # ...the rows after it are kept, and the earlier clear is not hidden.
    assert "Already cleared before the failure: ['alpha']" in captured.text
    assert [
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_key_models()
    ] == [("zeta", "zeta/model")]


async def test_startup_capture_survives_sweep_storage_failure(tmp_path, monkeypatch):
    """The startup preload never raises from the deployment capture (#376)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setattr("rootcoz.main._deployment_listing_state", None)
    monkeypatch.setattr(rootcoz_main, "list_models", AsyncMock(return_value=[]))
    await storage.set_server_provider_key_models(
        "orphan", [{"id": "orphan/model"}], status=None
    )

    async def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "set_server_provider_key_models", _boom)
    await rootcoz_main._safe_preload_cursor_models()

    assert [
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_key_models()
    ] == [("orphan", "orphan/model")]


async def test_stored_credential_without_rows_reports_no_source(client, monkeypatch):
    """``key_scoped_source`` reflects rows, not a credential record (#376)."""
    _fake_catalog(monkeypatch, [{"provider": "google", "id": "gemini-2.5-pro"}])
    await storage.set_server_ai_credential("google", SECRET, "admin")

    listed = client.get("/api/admin/server-providers", headers=_admin_headers())

    provider = {p["provider"]: p for p in listed.json()["providers"]}["google"]
    assert provider["has_key"] is True
    assert provider["key_scoped"] is False
    assert provider["key_scoped_source"] is None
    # The refresh path reads the stored record, so the button stays offered.
    assert provider["can_refresh_models"] is True


def _providers(client):
    """Admin provider listing, keyed by provider name."""
    listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert listed.status_code == 200
    return {p["provider"]: p for p in listed.json()["providers"]}


async def test_can_refresh_models_true_for_deployment_credential(
    client, monkeypatch, tmp_path
):
    """A capturable deployment credential is refreshable before any capture (#376)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    # No listing endpoint: this load captures no rows at all.
    _mock_key_discovery(monkeypatch, [], listing_supported=False)

    provider = _providers(client)["mygateway"]

    assert provider["has_key"] is False
    assert provider["key_scoped"] is False
    assert await storage.list_server_provider_key_models() == []
    assert provider["can_refresh_models"] is True


async def test_list_reports_deployment_credential_without_a_captured_listing(
    client, monkeypatch
):
    """``has_deployment_credential`` answers the credential, not the listing."""
    _register_providers(monkeypatch, "google", "openai", "mistral")
    _fake_catalog(
        monkeypatch,
        [
            {"provider": "google", "id": "gemini-2.5-pro"},
            {"provider": "openai", "id": "gpt-5.6-luna"},
            {"provider": "mistral", "id": "mistral-large"},
        ],
    )
    monkeypatch.setenv("GEMINI_API_KEY", AMBIENT_KEY)
    await storage.set_server_ai_credential("mistral", SECRET, "admin")
    _mock_supported(monkeypatch, ["google", "openai", "mistral"])
    # No listing support: this load captures no rows at all.
    _mock_key_discovery(monkeypatch, [], listing_supported=False)

    providers = _providers(client)

    # A resolvable env credential, reported with no captured listing at all.
    assert providers["google"]["has_deployment_credential"] is True
    assert providers["google"]["key_scoped"] is False
    assert providers["google"]["key_scoped_source"] is None
    # Neither a stored key nor an env credential: nothing to promote.
    assert providers["openai"]["has_deployment_credential"] is False
    # A stored key is the provider's credential — the deployment adds none.
    assert providers["mistral"]["has_key"] is True
    assert providers["mistral"]["has_deployment_credential"] is False


async def test_can_refresh_models_false_for_ambient_provider(client, monkeypatch):
    """No stored key and no capturable credential: nothing to refresh (#376)."""
    _fake_catalog(monkeypatch, [{"provider": "google", "id": "gemini-2.5-pro"}])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    provider = _providers(client)["google"]

    assert provider["has_key"] is False
    assert provider["can_refresh_models"] is False


async def test_can_refresh_models_with_unreadable_manifest(
    client, monkeypatch, tmp_path
):
    """Manifest unreadable (#376): no deployment credential can be resolved."""
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    _fake_catalog(
        monkeypatch,
        [
            {"provider": "mygateway", "id": "mygateway/gpt-4o"},
            {"provider": "google", "id": "gemini-2.5-pro"},
        ],
    )
    _mock_supported(monkeypatch, ["mygateway"])
    await storage.set_server_provider_key_models(
        "mygateway", [{"id": "mygateway/gpt-4o"}], status="supported"
    )
    (agent_dir / "models.json").write_text("{ half-written")

    providers = _providers(client)

    assert providers["mygateway"]["has_key"] is False
    # The retained rows carry no credential, so the button must not be offered.
    assert providers["mygateway"]["can_refresh_models"] is False
    assert providers["google"]["can_refresh_models"] is False
    # A forced refresh says why: a transient dependency failure, not a 404.
    refreshed = client.post(
        "/api/admin/server-providers/mygateway/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 503
    assert "manifest is unreadable" in refreshed.json()["detail"]
    assert DEPLOYMENT_KEY not in refreshed.text


async def test_stored_key_refresh_survives_unreadable_manifest(
    client, monkeypatch, tmp_path
):
    """A stored key refreshes on its own credential, manifest or not (#376)."""
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    _mock_supported(monkeypatch, ["mygateway"])
    calls = _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    await storage.set_server_ai_credential("mygateway", SECRET, "admin")
    await storage.set_server_provider_key_models(
        "mygateway", [{"id": "mygateway/gpt-4o"}], status="supported"
    )
    (agent_dir / "models.json").write_text("{ half-written")

    refreshed = client.post(
        "/api/admin/server-providers/mygateway/refresh-models",
        headers=_admin_headers(),
    )

    assert refreshed.status_code == 200
    assert refreshed.json() == {"ok": True, "status": "supported", "model_count": 1}
    assert calls == [("mygateway", SECRET)]


async def test_missing_manifest_keeps_captured_listing(
    client, monkeypatch, tmp_path, caplog
):
    """A momentarily missing models.json keeps the captured rows (#376)."""
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(
        monkeypatch, [{"provider": "mygateway", "id": "mygateway/catalog-only"}]
    )
    _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())

    # Atomic replace / mount blip: the agent dir is there, models.json is not.
    (agent_dir / "models.json").unlink()
    with _captured_logs(caplog, "rootcoz.ai_client", "rootcoz.main") as captured:
        listed = client.get("/api/admin/server-providers", headers=_admin_headers())

    assert listed.status_code == 200
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["mygateway/gpt-4o"]
    provider = {p["provider"]: p for p in listed.json()["providers"]}["mygateway"]
    assert provider["key_scoped"] is True
    assert [m["id"] for m in provider["models"]] == ["mygateway/gpt-4o"]
    assert "manifest unreadable" in captured.text


async def test_stored_key_appearing_mid_capture_wins(
    client, monkeypatch, tmp_path, caplog
):
    """A key stored during discovery is never overwritten by the capture (#376)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])

    async def _discovery(provider, key):
        # Another tab (or the startup task) stores a key while the sidecar
        # round-trip is in flight — after the capture scope was computed.
        await storage.set_server_ai_credential(provider, SECRET, "admin")
        return {
            "modelListingSupported": True,
            "models": [
                {
                    "id": "mygateway/deployment-only",
                    "name": "deployment-only",
                    "provider": provider,
                }
            ],
        }

    monkeypatch.setattr(ai_client, "models_for_api_key", _discovery)
    with _captured_logs(caplog, "rootcoz.main") as captured:
        listed = client.get("/api/admin/server-providers", headers=_admin_headers())

    assert listed.status_code == 200
    assert "superseded" in captured.text
    # The stored key owns the rows: the deployment listing was not written.
    assert await storage.list_server_provider_key_models() == []
    provider = {p["provider"]: p for p in listed.json()["providers"]}["mygateway"]
    assert provider["has_key"] is True
    assert provider["key_scoped"] is False
    assert DEPLOYMENT_KEY not in listed.text


def test_ai_models_refresh_forces_deployment_capture(client, monkeypatch, tmp_path):
    """The catalog refresh re-reads deployment listings despite the TTL."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(monkeypatch, [])
    calls = _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())
    assert len(calls) == 1
    sidecar = MagicMock()
    sidecar.refresh_models = AsyncMock(return_value=[])
    monkeypatch.setattr("pi_sidecar_client.get_sidecar_client", lambda: sidecar)
    monkeypatch.setattr(
        "rootcoz.main.probe_cursor_auth", AsyncMock(return_value={"ok": True})
    )
    monkeypatch.setattr(
        "rootcoz.main.registered_provider_ids", AsyncMock(return_value=set())
    )
    try:
        refreshed = client.post(
            "/api/admin/ai-models/refresh", headers=_admin_headers()
        )
    finally:
        ai_client.update_model_catalog(None)
    assert refreshed.status_code == 200
    assert refreshed.json()["deployment_providers"] == {"mygateway": 1}
    assert len(calls) == 2


async def test_startup_discovery_captures_deployment_listings(tmp_path, monkeypatch):
    """Startup model discovery also captures the deployment listing (#372)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp.db")
    await storage.init_db()
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    monkeypatch.setattr("rootcoz.main._deployment_listing_state", None)
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    calls = _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    await rootcoz_main._safe_preload_cursor_models()
    assert calls == [("mygateway", DEPLOYMENT_KEY)]
    assert [
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_key_models()
    ] == [("mygateway", "mygateway/gpt-4o")]


async def test_deployment_key_never_logged_or_returned(
    client, monkeypatch, tmp_path, caplog
):
    """Capture logs and responses name providers, never key material (#372)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    with _captured_logs(
        caplog, "rootcoz.main", "rootcoz.ai_client", level=logging.DEBUG
    ):
        listed = client.get("/api/admin/server-providers", headers=_admin_headers())
    assert DEPLOYMENT_KEY not in listed.text
    assert DEPLOYMENT_KEY not in caplog.text
    assert "mygateway" in caplog.text


# --- Ambient deployment credentials: environment-keyed providers (#376) ---

AMBIENT_KEY = "sk-ambient-4f9c2"  # pragma: allowlist secret gitleaks:allow
FALLBACK_KEY = "google-ambient-71ba"  # pragma: allowlist secret gitleaks:allow


def _register_providers(monkeypatch, *providers):
    """List ``providers`` in the last-known sidecar catalog (the ambient source)."""
    monkeypatch.setattr(
        ai_client,
        "_model_catalog_cache",
        [{"provider": provider, "id": f"{provider}/model"} for provider in providers],
    )


def test_deployment_credentials_resolve_ambient_env_key(monkeypatch):
    """A registered provider keyed from the environment needs no manifest (#376)."""
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)

    assert ai_client.deployment_provider_credentials() == {"openai": AMBIENT_KEY}


@pytest.mark.parametrize("provider", ["my-gateway", "my gateway", "my.gateway"])
def test_ambient_var_folds_provider_id_to_underscores(monkeypatch, provider):
    """The derived variable uppercases the id and folds every non-alnum to ``_``."""
    assert ai_client.ambient_api_key_var(provider) == "MY_GATEWAY_API_KEY"
    _register_providers(monkeypatch, provider)
    monkeypatch.setenv("MY_GATEWAY_API_KEY", AMBIENT_KEY)

    assert ai_client.deployment_provider_credentials() == {provider: AMBIENT_KEY}


def test_deployment_credentials_google_alias_prefers_gemini(monkeypatch):
    """``google`` reads GEMINI_API_KEY first, then GOOGLE_API_KEY (#376)."""
    _register_providers(monkeypatch, "google")
    monkeypatch.setenv("GEMINI_API_KEY", AMBIENT_KEY)
    monkeypatch.setenv("GOOGLE_API_KEY", FALLBACK_KEY)
    assert ai_client.deployment_provider_credentials() == {"google": AMBIENT_KEY}

    monkeypatch.delenv("GEMINI_API_KEY")
    assert ai_client.deployment_provider_credentials() == {"google": FALLBACK_KEY}


def test_deployment_credentials_skip_unset_ambient_var(monkeypatch):
    """An unset or blank ambient variable resolves no credential (#376)."""
    _register_providers(monkeypatch, "openai", "my-gateway")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "   ")

    assert ai_client.deployment_provider_credentials() == {}


def test_ambient_resolution_skips_manifest_declared_providers(tmp_path, monkeypatch):
    """A provider the manifest declares is keyed by the manifest, not the env."""
    _point_agent_dir(
        monkeypatch,
        tmp_path,
        {"openai": {"apiKey": "literal-manifest-key"}},  # pragma: allowlist secret
    )
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)

    assert ai_client.deployment_provider_credentials() == {}


def test_unreadable_manifest_stays_unknown_with_ambient_env(tmp_path, monkeypatch):
    """An unreadable manifest is still ``None`` — ambient resolution included."""
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    (agent_dir / "models.json").write_text("{ half-written")

    assert ai_client.deployment_provider_credentials() is None


async def test_ambient_env_provider_is_captured_and_refreshable(
    client, monkeypatch, caplog
):
    """``OPENAI_API_KEY`` makes openai capturable, listed and refreshable (#376)."""
    _register_providers(monkeypatch, "openai")
    _fake_catalog(monkeypatch, [{"provider": "openai", "id": "gpt-5.6-luna"}])
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    _mock_supported(monkeypatch, ["openai"])
    calls = _mock_key_discovery(
        monkeypatch, [{"id": "gpt-5.6-luna"}, {"id": "gpt-5.6-mini"}]
    )

    with _captured_logs(
        caplog, "rootcoz.main", "rootcoz.ai_client", level=logging.DEBUG
    ) as captured:
        provider = _providers(client)["openai"]

    assert calls == [("openai", AMBIENT_KEY)]
    assert provider["has_key"] is False
    assert provider["key_scoped"] is True
    assert provider["key_scoped_source"] == "deployment"
    assert provider["can_refresh_models"] is True
    assert [m["id"] for m in provider["models"]] == ["gpt-5.6-luna", "gpt-5.6-mini"]
    assert {
        (row["provider"], row["model"])
        for row in await storage.list_server_provider_key_models()
    } == {("openai", "gpt-5.6-luna"), ("openai", "gpt-5.6-mini")}

    refreshed = client.post(
        "/api/admin/server-providers/openai/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 200
    assert refreshed.json() == {"ok": True, "status": "supported", "model_count": 2}
    assert calls[-1] == ("openai", AMBIENT_KEY)
    assert AMBIENT_KEY not in refreshed.text
    assert AMBIENT_KEY not in captured.text
    assert "openai" in captured.text


def test_ambient_resolution_skips_cursor_agents(monkeypatch):
    """Cursor agents authenticate through the CLI, never CURSOR_API_KEY (#376)."""
    _register_providers(monkeypatch, "cursor", "acpx-cursor", "cli-cursor")
    monkeypatch.setenv("CURSOR_API_KEY", AMBIENT_KEY)

    assert ai_client.deployment_provider_credentials() == {}


def test_deployment_credentials_gemini_alias_falls_back_to_google(monkeypatch):
    """The ``gemini`` alias resolves the same two variables, in order (#376)."""
    _register_providers(monkeypatch, "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", FALLBACK_KEY)
    assert ai_client.deployment_provider_credentials() == {"gemini": FALLBACK_KEY}

    monkeypatch.setenv("GEMINI_API_KEY", AMBIENT_KEY)
    assert ai_client.deployment_provider_credentials() == {"gemini": AMBIENT_KEY}


def test_cold_catalog_cache_is_unknown_not_empty(monkeypatch):
    """A cold catalog cannot answer ambient eligibility, so it is ``None`` (#376).

    Startup, a refresh in flight, or a failed ``list_models()`` leaves no
    catalog. Reporting an empty resolve there would sweep every captured
    deployment listing on the next pass (#372's ``_clear_stale_...``).
    """
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    monkeypatch.setattr(ai_client, "_model_catalog_cache", None)

    assert ai_client.deployment_provider_credentials() is None
    # A warm catalog — even one holding no credential — is a real answer.
    _register_providers(monkeypatch, "openai")
    assert ai_client.deployment_provider_credentials() == {"openai": AMBIENT_KEY}


async def test_cold_catalog_keeps_captured_ambient_listing(tmp_path, monkeypatch):
    """A capture pass on a cold catalog captures and sweeps nothing (#376)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-cold.db")
    await storage.init_db()
    await storage.set_server_provider_key_models(
        "openai", [{"id": "gpt-5.6-luna"}], status="supported"
    )
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    _mock_supported(monkeypatch, ["openai"])
    calls = _mock_key_discovery(monkeypatch, [{"id": "gpt-5.6-mini"}])
    monkeypatch.setattr(ai_client, "_model_catalog_cache", None)

    assert await rootcoz_main.capture_deployment_provider_listings(force=True) == {}
    assert calls == []
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["gpt-5.6-luna"]


async def test_server_provider_key_resolves_ambient_env_key(tmp_path, monkeypatch):
    """An ambient-keyed provider's session gets that key; stored still wins (#376).

    Without it the sidecar cannot construct a model id that is absent from
    the static catalog and rejects the session, so the captured ambient
    models would be offered but unusable.
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-key.db")
    await storage.init_db()
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    assert await ai_client.server_provider_key("openai") == AMBIENT_KEY
    # Nothing resolvable: ``None`` keeps the deployment's env bootstrap.
    monkeypatch.delenv("OPENAI_API_KEY")
    assert await ai_client.server_provider_key("openai") is None
    # A stored key always wins over the ambient credential.
    await storage.set_server_ai_credential("openai", SECRET, "admin")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    assert await ai_client.server_provider_key("openai") == SECRET


async def test_server_provider_key_manifest_reference_never_logged(
    tmp_path, monkeypatch, caplog
):
    """A manifest ``$VAR`` credential resolves in memory and is never logged (#376)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-manifest-key.db")
    await storage.init_db()
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)

    with _captured_logs(
        caplog, "rootcoz.ai_client", "rootcoz.main", level=logging.DEBUG
    ):
        assert await ai_client.server_provider_key("mygateway") == DEPLOYMENT_KEY
        # An unreadable manifest is unknown, never an exception.
        (agent_dir / "models.json").write_text("{ half-written")
        assert await ai_client.server_provider_key("mygateway") is None
    assert DEPLOYMENT_KEY not in caplog.text


async def test_deployment_capture_rechecks_stored_key_at_write(
    tmp_path, monkeypatch, caplog
):
    """A key stored during discovery is rejected by the row write itself (#376)."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sp-race.db")
    await storage.init_db()
    await storage.set_server_provider_key_models(
        "mygateway", [{"id": "mygateway/gpt-4o"}], status="supported"
    )
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)

    async def _discovery(provider, key):
        # Lands between the capture's own re-check and the row write, so only
        # the guard inside the write transaction can see it.
        await storage.set_server_ai_credential(provider, SECRET, "admin")
        return {
            "modelListingSupported": True,
            "models": [
                {
                    "id": "mygateway/deployment-only",
                    "name": "deployment-only",
                    "provider": provider,
                }
            ],
        }

    monkeypatch.setattr(ai_client, "models_for_api_key", _discovery)
    with _captured_logs(caplog, "rootcoz.main") as captured:
        models, status = await rootcoz_main._capture_deployment_provider_listing(
            "mygateway", DEPLOYMENT_KEY
        )

    assert (models, status) == ([], "superseded")
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["mygateway/gpt-4o"]
    assert "mygateway" in captured.text
    assert DEPLOYMENT_KEY not in captured.text


# --- Capability gate for key-scoped listing (#376) ---


def _forbid_key_discovery(monkeypatch):
    """Make any ``/models/for-api-key`` call fail the test, not the sidecar."""

    async def _must_not_be_called(provider, key):
        raise AssertionError(f"models_for_api_key called for provider={provider}")

    monkeypatch.setattr(ai_client, "models_for_api_key", _must_not_be_called)


async def test_ambient_provider_without_key_support_is_never_requested(
    client, monkeypatch, caplog
):
    """An ambient provider the sidecar reports as not key-capable gets no call.

    ``/models/for-api-key`` answers it with a 400, so the capture must skip it
    (#376): no rows, no refresh offer, and no sidecar request.
    """
    _register_providers(monkeypatch, "openai")
    _fake_catalog(monkeypatch, [{"provider": "openai", "id": "gpt-5.6-luna"}])
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    _mock_supported(monkeypatch, ["google"])
    _forbid_key_discovery(monkeypatch)

    with _captured_logs(caplog, "rootcoz.main", level=logging.DEBUG) as captured:
        provider = _providers(client)["openai"]

    assert provider["has_key"] is False
    assert provider["key_scoped"] is False
    assert provider["key_scoped_source"] is None
    assert provider["can_refresh_models"] is False
    assert await storage.list_server_provider_key_models() == []
    # The skip is logged with the provider name and no key material.
    assert "Skipping key-scoped deployment listing for provider=openai" in captured.text
    assert AMBIENT_KEY not in captured.text

    # The per-provider path refuses on its own, without reaching the sidecar.
    assert await rootcoz_main._capture_deployment_listing_for("openai") == (
        [],
        "unsupported",
    )
    refreshed = client.post(
        "/api/admin/server-providers/openai/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 400
    assert "does not support" in refreshed.json()["detail"]
    assert AMBIENT_KEY not in refreshed.text


async def test_manifest_provider_without_key_support_is_never_requested(
    client, monkeypatch, tmp_path
):
    """``trustKeyScopedListing`` never overrides the sidecar's answer (#376)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    _mock_supported(monkeypatch, ["google"])
    _forbid_key_discovery(monkeypatch)

    provider = _providers(client)["mygateway"]

    assert provider["has_key"] is False
    assert provider["key_scoped"] is False
    assert provider["can_refresh_models"] is False
    assert [m["id"] for m in provider["models"]] == ["mygateway/gpt-4o"]
    assert await storage.list_server_provider_key_models() == []
    assert await rootcoz_main._capture_deployment_listing_for("mygateway") == (
        [],
        "unsupported",
    )
    refreshed = client.post(
        "/api/admin/server-providers/mygateway/refresh-models",
        headers=_admin_headers(),
    )
    assert refreshed.status_code == 400
    assert "does not support" in refreshed.json()["detail"]
    assert DEPLOYMENT_KEY not in refreshed.text


async def test_key_capability_is_looked_up_once_per_pass(client, monkeypatch, tmp_path):
    """One capability answer per capture pass, never one per provider (#376)."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    _fake_catalog(
        monkeypatch,
        [
            {"provider": "mygateway", "id": "mygateway/gpt-4o"},
            {"provider": "openai", "id": "gpt-5.6-luna"},
        ],
    )
    lookups = []

    async def _supported():
        lookups.append(True)
        return ["mygateway", "openai"]

    monkeypatch.setattr("rootcoz.main.supported_key_providers", _supported)
    calls = _mock_key_discovery(monkeypatch, [{"id": "gateway-model"}])

    client.get("/api/admin/server-providers", headers=_admin_headers())

    # Two capturable providers, one capability answer for the page load.
    assert len(lookups) == 1
    assert sorted(calls) == [
        ("mygateway", DEPLOYMENT_KEY),
        ("openai", AMBIENT_KEY),
    ]

    # A fresh pass re-reads it once — still not once per provider.
    monkeypatch.setattr("rootcoz.main._deployment_listing_state", None)
    await rootcoz_main.capture_deployment_provider_listings(force=True)
    assert len(lookups) == 2
    assert len(calls) == 4


async def test_capability_lookup_failure_keeps_captured_rows(
    client, monkeypatch, tmp_path
):
    """A fail-closed capability answer never sweeps a credential-backed row.

    ``supported_key_providers()`` fails closed, so a sidecar blip must not look
    like "this provider lost support" and clear its captured listing (#376).
    """
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _fake_catalog(monkeypatch, [{"provider": "mygateway", "id": "mygateway/gpt-4o"}])
    _mock_supported(monkeypatch, ["mygateway"])
    _mock_key_discovery(monkeypatch, [{"id": "mygateway/gpt-4o"}])
    client.get("/api/admin/server-providers", headers=_admin_headers())
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["mygateway/gpt-4o"]

    # Sidecar unreachable: the capability answer is empty, the credential is not.
    _mock_supported(monkeypatch, [])
    monkeypatch.setattr("rootcoz.main._deployment_listing_state", None)
    assert await rootcoz_main.capture_deployment_provider_listings(force=True) == {}
    assert [
        row["model"] for row in await storage.list_server_provider_key_models()
    ] == ["mygateway/gpt-4o"]


# --- Store the deployment credential as a server key (#378) ---


def _store_deployment_key(client, provider, model="gpt-5.6-luna"):
    """POST store-deployment-key; ``model=None`` omits the field entirely."""
    return client.post(
        f"/api/admin/server-providers/{provider}/store-deployment-key",
        headers=_admin_headers(),
        json={} if model is None else {"verification_model": model},
    )


async def test_store_deployment_key_promotes_the_resolved_credential(
    client, monkeypatch, tmp_path, caplog
):
    """The deployment credential becomes the stored key, key never re-sent."""
    _register_providers(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    _fake_catalog(monkeypatch, [{"provider": "openai", "id": "gpt-5.6-luna"}])
    _mock_supported(monkeypatch, ["openai"])
    verified = _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, [{"id": "gpt-5.6-luna"}, {"id": "gpt-5.6-nova"}])

    with _captured_logs(caplog, "rootcoz.main", level=logging.INFO) as logs:
        response = _store_deployment_key(client, "openai")

    assert response.status_code == 200
    assert response.json() == {"outcome": "accepted", "ok": True, "model_count": 2}
    # Verification ran with the deployment credential, not with anything posted.
    assert verified == [("openai", "gpt-5.6-luna", AMBIENT_KEY)]
    stored = await storage.list_server_ai_providers()
    assert [row["provider"] for row in stored] == ["openai"]
    # A first store assigns the first generation — the key-scoped capture's guard.
    assert stored[0]["generation"] == 1
    assert stored[0]["key_capture_status"] == "supported"
    assert await storage.get_server_ai_credential("openai") == AMBIENT_KEY
    # Same machinery as add: the selection is seeded from the key-scoped listing.
    assert sorted(
        row["model"] for row in await storage.list_server_provider_models()
    ) == ["gpt-5.6-luna", "gpt-5.6-nova"]
    # Encrypted at rest, and no key material in the response or the logs.
    for path in tmp_path.glob("server-providers.db*"):
        assert AMBIENT_KEY.encode() not in path.read_bytes()
    assert AMBIENT_KEY not in response.text
    assert AMBIENT_KEY not in logs.text
    assert "[AUDIT] Admin 'admin' stored server provider key for 'openai'" in logs.text


def test_store_deployment_key_without_a_credential_is_404(client, monkeypatch):
    """No resolvable credential (unset variable, no manifest entry) is a 404."""
    _mock_supported(monkeypatch, ["openai"])
    response = _store_deployment_key(client, "openai")
    assert response.status_code == 404
    assert "no deployment credential to store" in response.json()["detail"]


def test_store_deployment_key_unreadable_manifest_is_404(client, monkeypatch, tmp_path):
    """An unreadable manifest resolves nothing — a 404, never a 500 (#376)."""
    agent_dir = _point_agent_dir(
        monkeypatch, tmp_path, {"mygateway": _deployment_provider()}
    )
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    (agent_dir / "models.json").unlink()
    _mock_supported(monkeypatch, ["mygateway"])
    response = _store_deployment_key(client, "mygateway")
    assert response.status_code == 404
    assert "no deployment credential to store" in response.json()["detail"]


async def test_store_deployment_key_conflicts_with_an_existing_key(
    client, monkeypatch, tmp_path
):
    """A stored key answers 409 (rotate instead) and is left untouched."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    verified = _mock_verify(monkeypatch, "accepted")
    await storage.set_server_ai_credential("mygateway", SECRET, "admin")
    generation = await storage.get_server_credential_generation("mygateway")

    response = _store_deployment_key(client, "mygateway", "mygateway/gpt-4o")

    assert response.status_code == 409
    assert "already has a stored key; use rotate-key" in response.json()["detail"]
    # The stored credential and its generation are exactly what they were, and
    # no verification round-trip was spent on the rejected request.
    assert await storage.get_server_ai_credential("mygateway") == SECRET
    assert await storage.get_server_credential_generation("mygateway") == generation
    assert verified == []
    assert DEPLOYMENT_KEY not in response.text


async def test_store_deployment_key_reports_rejected_verification(
    client, monkeypatch, tmp_path
):
    """A rejected check keeps the add/rotate convention: 200, ok:false, no write."""
    _point_agent_dir(monkeypatch, tmp_path, {"mygateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _mock_supported(monkeypatch, ["mygateway"])
    _mock_verify(monkeypatch, "rejected")

    response = _store_deployment_key(client, "mygateway", "mygateway/gpt-4o")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["outcome"] == "rejected"
    assert response.json()["message"]
    assert "model_count" not in response.json()
    assert await storage.list_server_ai_providers() == []
    assert DEPLOYMENT_KEY not in response.text


async def test_store_deployment_key_matches_provider_case_insensitively(
    client, monkeypatch, tmp_path
):
    """A lowercase request resolves the mixed-case manifest provider's key."""
    _point_agent_dir(monkeypatch, tmp_path, {"MyGateway": _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _fake_catalog(monkeypatch, [{"provider": "MyGateway", "id": "MyGateway/gpt-4o"}])
    _mock_supported(monkeypatch, ["MyGateway"])
    verified = _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, [{"id": "MyGateway/gpt-4o"}])

    response = _store_deployment_key(client, "mygateway", "MyGateway/gpt-4o")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    # Verification and storage both see the sidecar's exact-case id.
    assert verified == [("MyGateway", "MyGateway/gpt-4o", DEPLOYMENT_KEY)]
    assert [row["provider"] for row in await storage.list_server_ai_providers()] == [
        "MyGateway"
    ]


# --- Verification model discovery (#378 r2) ---


def _zero_model_provider(monkeypatch, tmp_path, provider="mygateway"):
    """A key-capable provider with a deployment credential and no catalog model."""
    _point_agent_dir(monkeypatch, tmp_path, {provider: _deployment_provider()})
    monkeypatch.setenv(DEPLOYMENT_ENV, DEPLOYMENT_KEY)
    _fake_catalog(monkeypatch, [])
    _mock_supported(monkeypatch, [provider])


@pytest.mark.parametrize("posted_model", [None, ""])
async def test_store_deployment_key_discovers_the_verification_model(
    client, monkeypatch, tmp_path, caplog, posted_model
):
    """No model supplied: the credential's own first listing model verifies.

    A zero-model provider has no catalog template to type in, and the sidecar
    rejects an invented id, so discovery is the only path that can store it.
    """
    _zero_model_provider(monkeypatch, tmp_path)
    verified = _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, [{"id": "gw-first"}, {"id": "gw-second"}])

    with _captured_logs(caplog, "rootcoz.main", level=logging.DEBUG) as logs:
        response = _store_deployment_key(client, "mygateway", posted_model)

    assert response.status_code == 200
    assert response.json() == {"outcome": "accepted", "ok": True, "model_count": 2}
    assert verified == [("mygateway", "gw-first", DEPLOYMENT_KEY)]
    assert await storage.get_server_ai_credential("mygateway") == DEPLOYMENT_KEY
    # The chosen id is a DEBUG detail; the key is never logged at any level.
    assert "gw-first" in logs.text
    assert DEPLOYMENT_KEY not in logs.text
    assert DEPLOYMENT_KEY not in response.text


@pytest.mark.parametrize("models,listing", [([], True), ([], False)])
async def test_store_deployment_key_without_a_discoverable_model_is_400(
    client, monkeypatch, tmp_path, models, listing
):
    """Unsupported or empty discovery is a 400 naming the manual model, no write."""
    _zero_model_provider(monkeypatch, tmp_path)
    verified = _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, models, listing_supported=listing)

    response = _store_deployment_key(client, "mygateway", None)

    assert response.status_code == 400
    assert "enter a model id to verify against" in response.json()["detail"]
    assert verified == []
    assert await storage.list_server_ai_providers() == []
    assert DEPLOYMENT_KEY not in response.text


async def test_explicit_verification_model_never_discovers(
    client, monkeypatch, tmp_path
):
    """A caller-supplied model always wins — no discovery before verification.

    The key is rejected so the post-verification capture (which legitimately
    re-runs discovery once a key is stored) never runs: an empty call list
    proves the model itself was never discovered.
    """
    _zero_model_provider(monkeypatch, tmp_path)
    verified = _mock_verify(monkeypatch, "rejected")
    calls = _mock_key_discovery(monkeypatch, [{"id": "gw-first"}])

    response = _store_deployment_key(client, "mygateway", "gw-typed")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert verified == [("mygateway", "gw-typed", DEPLOYMENT_KEY)]
    assert calls == []
    assert await storage.list_server_ai_providers() == []


async def test_add_and_rotate_discover_the_verification_model(client, monkeypatch):
    """Plain add and rotate discover the model too — same shared rule."""
    _fake_catalog(monkeypatch, [])
    _mock_supported(monkeypatch, ["mygateway"])
    verified = _mock_verify(monkeypatch, "accepted")
    _mock_key_discovery(monkeypatch, [{"id": "gw-first"}, {"id": "gw-second"}])

    added = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={"provider": "mygateway", "api_key": SECRET},
    )
    assert added.status_code == 200
    assert added.json()["ok"] is True
    assert await storage.get_server_ai_credential("mygateway") == SECRET

    rotated_key = "rotated-key-456"  # pragma: allowlist secret  # gitleaks:allow
    rotated = client.post(
        "/api/admin/server-providers/mygateway/rotate-key",
        headers=_admin_headers(),
        json={"provider": "mygateway", "api_key": rotated_key},
    )
    assert rotated.status_code == 200
    assert rotated.json()["ok"] is True
    assert await storage.get_server_ai_credential("mygateway") == rotated_key
    assert verified == [
        ("mygateway", "gw-first", SECRET),
        ("mygateway", "gw-first", rotated_key),
    ]
    assert SECRET not in added.text and rotated_key not in rotated.text


def test_add_without_a_model_on_an_unsupported_provider_never_discovers(
    client, monkeypatch
):
    """The capability gate answers first — no discovery for an unsupported id."""
    _mock_supported(monkeypatch, ["openai"])
    calls = _mock_key_discovery(monkeypatch, [{"id": "gw-first"}])

    response = client.post(
        "/api/admin/server-providers",
        headers=_admin_headers(),
        json={"provider": "mygateway", "api_key": SECRET},
    )

    assert response.status_code == 400
    assert "does not support session API keys" in response.json()["detail"]
    assert calls == []
