"""Free provider+model pair grants — issue #317.

Covers the can_use_free_providers user flag (API surface, superset
semantics) and the admin-declared free pair list endpoints, including
catalog validation.
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
