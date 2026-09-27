"""Server AI grants are admin-managed and immediately effective."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from rootcoz import storage
from rootcoz.config import get_settings
from rootcoz.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "server-grant.db")
    monkeypatch.setenv("ADMIN_KEY", "grant-bootstrap-key")
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server-grant.db"))
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def test_admin_grant_revoke_and_strict_payload(client):
    admin = {"Authorization": "Bearer grant-bootstrap-key"}
    created = client.post(
        "/api/admin/users/create",
        headers=admin,
        json={"username": "grant-user", "role": "operator"},
    )
    assert created.status_code == 200
    assert created.json()["can_use_server_providers"] is False
    key = created.json()["api_key"]
    login = client.post(
        "/api/auth/login", json={"username": "grant-user", "api_key": key}
    )
    assert login.json()["can_use_server_providers"] is False
    session = {"rootcoz_session": login.cookies["rootcoz_session"]}
    url = "/api/admin/users/grant-user/can-use-server-providers"
    assert (
        client.put(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json={"can_use_server_providers": True},
        ).status_code
        == 403
    )
    assert (
        client.put(
            url, headers=admin, json={"can_use_server_providers": "true"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            url, headers=admin, json={"can_use_server_providers": True}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/auth/me", cookies=session).json()["can_use_server_providers"]
        is True
    )
    assert (
        client.put(
            url, headers=admin, json={"can_use_server_providers": False}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/auth/me", cookies=session).json()["can_use_server_providers"]
        is False
    )
    assert (
        client.get("/api/auth/me", headers=admin).json()["can_use_server_providers"]
        is True
    )
    assert (
        client.put(
            "/api/admin/users/admin/can-use-server-providers",
            headers=admin,
            json={"can_use_server_providers": False},
        ).status_code
        == 400
    )


def test_approval_can_grant_access(client):
    admin = {"Authorization": "Bearer grant-bootstrap-key"}
    created = client.post(
        "/api/admin/users/create",
        headers=admin,
        json={"username": "waiting-user", "role": "reviewer"},
    )
    assert created.status_code == 200
    # A pending user is not granted server credentials until an admin approves.
    asyncio.run(storage.set_user_status("waiting-user", "pending"))
    response = client.post(
        "/api/admin/users/waiting-user/approve",
        headers=admin,
        json={"can_use_server_providers": True},
    )
    assert response.status_code == 200
    assert response.json()["can_use_server_providers"] is True
    duplicate = client.post(
        "/api/admin/users/waiting-user/approve",
        headers=admin,
        json={"can_use_server_providers": False},
    )
    assert duplicate.status_code == 400
    users = client.get("/api/admin/users", headers=admin).json()["users"]
    assert (
        next(u for u in users if u["username"] == "waiting-user")[
            "can_use_server_providers"
        ]
        is True
    )
