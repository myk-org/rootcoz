"""Free provider+model pair grants — issue #317.

Covers the ENABLE_FREE_MODELS server toggle (effective access on
auth/me and login, superset semantics) and the admin-declared free
pair list endpoints, including catalog validation and stale-entry
pass-through.
"""

import pytest
from fastapi.testclient import TestClient

from rootcoz import storage
from rootcoz.config import get_settings
from rootcoz.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "free-grant.db")
    monkeypatch.setenv("ADMIN_KEY", "free-bootstrap-key")
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "free-grant.db"))
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def _admin_headers():
    return {"Authorization": "Bearer free-bootstrap-key"}


def _set_free_toggle(monkeypatch, enabled: bool) -> None:
    """Point get_settings at an object with ENABLE_FREE_MODELS set."""
    from rootcoz import config

    class _Settings:
        enable_free_models = enabled

    monkeypatch.setattr(config, "get_settings", lambda: _Settings())


def _create_and_login(client, username):
    created = client.post(
        "/api/admin/users/create",
        headers=_admin_headers(),
        json={"username": username, "role": "operator"},
    )
    assert created.status_code == 200
    key = created.json()["api_key"]
    login = client.post("/api/auth/login", json={"username": username, "api_key": key})
    assert login.json()["can_use_free_providers"] is False
    return key, {"rootcoz_session": login.cookies["rootcoz_session"]}


def test_server_toggle_gates_free_access(client):
    _, session = _create_and_login(client, "free-user")
    me = client.get("/api/auth/me", cookies=session)
    assert me.json()["can_use_free_providers"] is False
    assert me.json()["can_use_server_providers"] is False

    response = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"enable_free_models": "true"}},
    )
    assert response.status_code == 200
    me = client.get("/api/auth/me", cookies=session)
    assert me.json()["can_use_free_providers"] is True
    assert me.json()["can_use_server_providers"] is False

    response = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"enable_free_models": "false"}},
    )
    assert response.status_code == 200
    me = client.get("/api/auth/me", cookies=session)
    assert me.json()["can_use_free_providers"] is False


def test_per_user_free_endpoint_is_gone(client):
    _create_and_login(client, "plain-user")
    assert (
        client.put(
            "/api/admin/users/plain-user/can-use-free-providers",
            headers=_admin_headers(),
            json={"can_use_free_providers": True},
        ).status_code
        in (404, 405)  # gone entirely; FastAPI may 405 on the leftover path shape
    )


def test_server_grant_is_superset_of_free(client):
    _key, session = _create_and_login(client, "superset-user")
    url = "/api/admin/users/superset-user/can-use-server-providers"
    assert (
        client.put(
            url, headers=_admin_headers(), json={"can_use_server_providers": True}
        ).status_code
        == 200
    )
    me = client.get("/api/auth/me", cookies=session)
    # Effective free access follows the superset even with the free flag unset.
    assert me.json()["can_use_free_providers"] is True
    assert me.json()["can_use_server_providers"] is True


def test_bootstrap_admin_implies_free(client):
    me = client.get("/api/auth/me", headers=_admin_headers())
    assert me.json()["can_use_free_providers"] is True


def test_free_models_endpoints_require_admin(client):
    key, _ = _create_and_login(client, "plain-user")
    assert (
        client.get(
            "/api/admin/free-models", headers={"Authorization": f"Bearer {key}"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/admin/free-models",
            headers={"Authorization": f"Bearer {key}"},
            json={"models": []},
        ).status_code
        == 403
    )


def test_free_models_validation_against_catalog(client, monkeypatch):
    from rootcoz import ai_client

    catalog = [
        {"provider": "EnMaaS", "id": "rits/zai-org/glm-5-3"},
        {"provider": "openai", "id": "gpt-5.6-luna"},
    ]

    async def fake_catalog(refresh: bool = False):
        return catalog

    monkeypatch.setattr(ai_client, "_get_model_catalog", fake_catalog)
    monkeypatch.setattr("rootcoz.main._get_model_catalog", fake_catalog, raising=False)

    headers = _admin_headers()
    listing = client.get("/api/admin/free-models", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["models"] == []

    # Unknown pairs are rejected with details.
    bad = client.put(
        "/api/admin/free-models",
        headers=headers,
        json={"models": [{"provider": "EnMaaS", "model": "no-such-model"}]},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["unknown"] == [
        {"provider": "EnMaaS", "model": "no-such-model"}
    ]

    # Valid pairs are stored; provider matching is case-insensitive.
    good = client.put(
        "/api/admin/free-models",
        headers=headers,
        json={
            "models": [
                {"provider": "enmaas", "model": "rits/zai-org/glm-5-3"},
                {"provider": "openai", "model": "gpt-5.6-luna"},
            ]
        },
    )
    assert good.status_code == 200
    stored = good.json()["models"]
    assert {(p["provider"], p["model"]) for p in stored} == {
        ("enmaas", "rits/zai-org/glm-5-3"),
        ("openai", "gpt-5.6-luna"),
    }

    # Replace with an empty list clears it.
    cleared = client.put("/api/admin/free-models", headers=headers, json={"models": []})
    assert cleared.status_code == 200
    assert cleared.json()["models"] == []


def test_free_models_strict_payload(client):
    assert (
        client.put(
            "/api/admin/free-models",
            headers=_admin_headers(),
            json={"models": [{"provider": "p", "model": ""}]},
        ).status_code
        == 422
    )


def test_login_reports_effective_free_grant(client):
    """Login uses the effective grant, not a removed per-user column."""
    _create_and_login(client, "login-user")
    client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"enable_free_models": "true"}},
    )
    # Create a second user and log in with the toggle on.
    created = client.post(
        "/api/admin/users/create",
        headers=_admin_headers(),
        json={"username": "login-user-2", "role": "operator"},
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "login-user-2", "api_key": created.json()["api_key"]},
    )
    assert login.json()["can_use_free_providers"] is True
    assert login.json()["can_use_server_providers"] is False


def test_role_admin_implies_free_access(client):
    """DB users with role=admin get free access regardless of the toggle."""
    created = client.post(
        "/api/admin/users/create",
        headers=_admin_headers(),
        json={"username": "role-admin", "role": "admin"},
    )
    assert created.status_code == 200
    login = client.post(
        "/api/auth/login",
        json={"username": "role-admin", "api_key": created.json()["api_key"]},
    )
    assert login.json()["can_use_free_providers"] is True


@pytest.mark.anyio
async def test_deleted_user_has_no_free_access(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "deleted-user.db")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "deleted-user.db"))
    get_settings.cache_clear()
    with TestClient(app) as _:
        pass
    from rootcoz.storage import can_user_use_free_providers

    assert await can_user_use_free_providers("ghost-user") is False


def test_stale_pairs_pass_through_and_history_is_kept(client, monkeypatch):
    """Stale stored pairs survive replacement; new pairs validate; creators kept."""

    async def catalog(**_):
        return [
            {"provider": "EnMaaS", "id": "glm-5-3"},
            {"provider": "google", "id": "gemini-2.5-pro"},
        ]

    monkeypatch.setattr("rootcoz.main._get_model_catalog", catalog)

    put = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={"models": [{"provider": "EnMaaS", "model": "glm-5-3"}]},
    )
    assert put.status_code == 200

    # Drop glm-5-3 from the catalog; the stored pair is now stale.
    async def slim_catalog(**_):
        return [{"provider": "google", "id": "gemini-2.5-pro"}]

    monkeypatch.setattr("rootcoz.main._get_model_catalog", slim_catalog)
    # Adding an unrelated new pair must not be blocked by the stale one.
    put = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={
            "models": [
                {"provider": "EnMaaS", "model": "glm-5-3"},
                {"provider": "google", "model": "gemini-2.5-pro"},
            ]
        },
    )
    assert put.status_code == 200, put.text
    stored = put.json()["models"]

    # A genuinely new unknown pair is still rejected.
    put = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={
            "models": [
                {"provider": "EnMaaS", "model": "glm-5-3"},
                {"provider": "nope", "model": "nope-model"},
            ]
        },
    )
    assert put.status_code == 400

    # Surviving pairs keep their original creator.
    pair = next(p for p in stored if p["provider"] == "EnMaaS")
    assert pair["created_by"] == "admin"


def test_free_models_optimistic_versioning(client, monkeypatch):
    """A stale list fingerprint rejects the write (409) with current state."""

    async def catalog(**_):
        return [
            {"provider": "EnMaaS", "id": "glm-5-3"},
            {"provider": "google", "id": "gemini-2.5-pro"},
        ]

    monkeypatch.setattr("rootcoz.main._get_model_catalog", catalog)

    initial = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={"models": [{"provider": "EnMaaS", "model": "glm-5-3"}]},
    )
    assert initial.status_code == 200, initial.text
    version = initial.json()["version"]
    assert version

    # GET advertises the same fingerprint the PUT returned.
    listed = client.get("/api/admin/free-models", headers=_admin_headers())
    assert listed.status_code == 200
    assert listed.json()["version"] == version

    # Another admin writes first; our stale fingerprint must not erase it.
    other = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={"models": [{"provider": "google", "model": "gemini-2.5-pro"}]},
    )
    assert other.status_code == 200

    stale = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={
            "models": [{"provider": "EnMaaS", "model": "glm-5-3"}],
            "expected_version": version,
        },
    )
    assert stale.status_code == 409, stale.text
    conflict = stale.json()["detail"]
    assert {p["model"] for p in conflict["models"]} == {"gemini-2.5-pro"}
    assert conflict["version"]

    # Re-playing with the fresh fingerprint from the 409 body succeeds.
    retried = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={
            "models": [
                {"provider": "google", "model": "gemini-2.5-pro"},
                {"provider": "EnMaaS", "model": "glm-5-3"},
            ],
            "expected_version": conflict["version"],
        },
    )
    assert retried.status_code == 200, retried.text

    # Omitting the fingerprint keeps last-write-wins for callers that
    # did not read first (backward compatibility).
    unguarded = client.put(
        "/api/admin/free-models",
        headers=_admin_headers(),
        json={"models": [{"provider": "google", "model": "gemini-2.5-pro"}]},
    )
    assert unguarded.status_code == 200


@pytest.mark.anyio
async def test_removed_free_pair_retires_server_chat_session(tmp_path, monkeypatch):
    """A free-granted user's server session dies with its pair (dac13996)."""
    from rootcoz.main import _available_chat_session

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "retire.db")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "retire.db"))
    get_settings.cache_clear()
    await storage.init_db()
    await storage.create_user("freechatter")
    await storage.set_free_ai_models([("EnMaaS", "glm-5-3")], "admin")
    _set_free_toggle(monkeypatch, True)

    await storage.add_chat_message(
        "job-1",
        "assistant",
        "started",
        username="freechatter",
        ai_provider="EnMaaS",
        ai_model="glm-5-3",
        session_id="sess-1",
    )
    await storage.save_ai_session_source("sess-1", "freechatter", "EnMaaS", "server")

    # Pair still declared → session usable.
    session = await _available_chat_session("job-1", "freechatter")
    assert session == {
        "ai_provider": "EnMaaS",
        "ai_model": "glm-5-3",
        "credential_source": "server",
    }

    # Admin drops the pair → the session must not survive on the free grant.
    await storage.set_free_ai_models([], "admin")
    assert await _available_chat_session("job-1", "freechatter") is None


def test_free_models_version_ignores_provider_casing():
    """A cosmetic provider respelling is not a list change (6d6020cb)."""
    from rootcoz.storage import free_models_version

    a = [
        {"provider": "EnMaaS", "model": "glm-5-3"},
        {"provider": "google", "model": "gemini"},
    ]
    b = [
        {"provider": "google", "model": "gemini"},
        {"provider": "enmaas", "model": "glm-5-3"},
    ]
    assert free_models_version(a) == free_models_version(b)
    # A real change still moves the fingerprint.
    c = [{"provider": "enmaas", "model": "glm-5-3"}]
    assert free_models_version(a) != free_models_version(c)


def test_ai_models_provider_lookup_is_case_insensitive(client, monkeypatch):
    """Per-provider /api/ai-models resolves mixed-case custom providers."""

    async def catalog(**_):
        return [{"provider": "EnMaaS", "id": "glm-5-3"}]

    # scoped_models (ai_client) reads its own module binding; the bootstrap
    # admin holds the server grant so the catalog survives scoping.
    monkeypatch.setattr("rootcoz.ai_client._get_model_catalog", catalog)
    resp = client.get("/api/ai-models?provider=enmaas", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider"] == "EnMaaS"
    assert [m["id"] for m in resp.json()["models"]] == ["glm-5-3"]
