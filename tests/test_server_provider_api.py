"""Server AI grants are admin-managed and immediately effective."""

import asyncio
import sqlite3

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


def test_managed_admin_cannot_be_revoked(client):
    admin = {"Authorization": "Bearer grant-bootstrap-key"}
    created = client.post(
        "/api/admin/users/create",
        headers=admin,
        json={"username": "db-admin", "role": "admin"},
    )
    assert created.status_code == 200
    assert created.json()["can_use_server_providers"] is True
    key = created.json()["api_key"]
    assert (
        client.post(
            "/api/auth/login", json={"username": "db-admin", "api_key": key}
        ).json()["can_use_server_providers"]
        is True
    )
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {key}"}).json()[
            "can_use_server_providers"
        ]
        is True
    )
    assert (
        next(
            u
            for u in client.get("/api/admin/users", headers=admin).json()["users"]
            if u["username"] == "db-admin"
        )["can_use_server_providers"]
        is True
    )
    assert (
        client.put(
            "/api/admin/users/db-admin/can-use-server-providers",
            headers=admin,
            json={"can_use_server_providers": False},
        ).status_code
        == 400
    )


def test_new_admin_demotion_does_not_keep_role_grant(client):
    admin = {"Authorization": "Bearer grant-bootstrap-key"}
    created = client.post(
        "/api/admin/users/create",
        headers=admin,
        json={"username": "new-admin", "role": "admin"},
    )
    assert created.status_code == 200
    assert created.json()["can_use_server_providers"] is True
    with sqlite3.connect(storage.DB_PATH) as db:
        assert (
            db.execute(
                "SELECT can_use_server_providers FROM users WHERE username = ?",
                ("new-admin",),
            ).fetchone()[0]
            == 0
        )
    assert (
        client.put(
            "/api/admin/users/new-admin/role",
            headers=admin,
            json={"role": "operator"},
        ).status_code
        == 200
    )
    assert not asyncio.run(storage.can_user_use_server_providers("new-admin"))
    assert (
        client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {created.json()['api_key']}"},
        ).json()["can_use_server_providers"]
        is False
    )


def test_explicit_admin_creation_grant_survives_demotion(client):
    admin = {"Authorization": "Bearer grant-bootstrap-key"}
    created = client.post(
        "/api/admin/users/create",
        headers=admin,
        json={
            "username": "explicit-admin",
            "role": "admin",
            "can_use_server_providers": True,
        },
    )
    assert created.status_code == 200
    assert (
        client.put(
            "/api/admin/users/explicit-admin/role",
            headers=admin,
            json={"role": "operator"},
        ).status_code
        == 200
    )
    assert asyncio.run(storage.can_user_use_server_providers("explicit-admin"))


def test_explicit_grant_survives_promotion_and_demotion(client):
    admin = {"Authorization": "Bearer grant-bootstrap-key"}
    created = client.post(
        "/api/admin/users/create",
        headers=admin,
        json={
            "username": "granted",
            "role": "operator",
            "can_use_server_providers": True,
        },
    )
    assert created.status_code == 200
    for role in ("admin", "operator"):
        response = client.put(
            "/api/admin/users/granted/role", headers=admin, json={"role": role}
        )
        assert response.status_code == 200
        assert asyncio.run(storage.can_user_use_server_providers("granted"))
    assert (
        client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {created.json()['api_key']}"},
        ).json()["can_use_server_providers"]
        is True
    )


def test_approval_without_choice_preserves_migrated_grant(client):
    admin = {"Authorization": "Bearer grant-bootstrap-key"}
    asyncio.run(storage.register_user_with_status("migrated", "fake-hash", "pending"))
    asyncio.run(storage.set_user_can_use_server_providers("migrated", True))
    response = client.post("/api/admin/users/migrated/approve", headers=admin)
    assert response.status_code == 200
    assert response.json()["can_use_server_providers"] is True
    assert asyncio.run(storage.can_user_use_server_providers("migrated"))
    asyncio.run(storage.register_user_with_status("revoked", "other-hash", "pending"))
    asyncio.run(storage.set_user_can_use_server_providers("revoked", True))
    response = client.post(
        "/api/admin/users/revoked/approve",
        headers=admin,
        json={"can_use_server_providers": False},
    )
    assert response.json()["can_use_server_providers"] is False
    assert not asyncio.run(storage.can_user_use_server_providers("revoked"))


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
