"""Tests for admin approval of new user registrations (issue #54)."""

import asyncio
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rootcoz import storage
from rootcoz.config import get_settings


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def client_approval_on(_init_db, temp_db_path):
    """Create a test client with REQUIRE_APPROVAL=true."""
    with patch.dict(
        os.environ,
        {
            "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
            "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            "SECURE_COOKIES": "false",
            "DB_PATH": str(temp_db_path),
            "REQUIRE_APPROVAL": "true",
        },
    ):
        get_settings.cache_clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            from rootcoz.main import app

            with TestClient(app) as c:
                yield c
        get_settings.cache_clear()


@pytest.fixture
def client_approval_off(_init_db, temp_db_path):
    """Create a test client with REQUIRE_APPROVAL=false (backward compat)."""
    with patch.dict(
        os.environ,
        {
            "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
            "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            "SECURE_COOKIES": "false",
            "DB_PATH": str(temp_db_path),
            "REQUIRE_APPROVAL": "false",
        },
    ):
        get_settings.cache_clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            from rootcoz.main import app

            with TestClient(app) as c:
                yield c
        get_settings.cache_clear()


def _admin_headers():
    """Return admin auth headers."""
    return {
        "Authorization": "Bearer test-admin-key-16chars"
    }  # pragma: allowlist secret


class TestRegisterWithApproval:
    """Test registration when REQUIRE_APPROVAL is True."""

    def test_register_creates_pending_user(self, client_approval_on):
        """Registering with REQUIRE_APPROVAL=true creates a pending user."""
        resp = client_approval_on.post(
            "/api/auth/register", json={"username": "newuser01"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["username"] == "newuser01"
        assert "api_key" in data
        assert "awaiting admin approval" in data["message"].lower()

    def test_pending_user_cannot_access_protected_endpoints(self, client_approval_on):
        """A pending user should get 403 on protected endpoints."""
        # Register a user (pending)
        reg = client_approval_on.post(
            "/api/auth/register", json={"username": "penduser1"}
        )
        assert reg.status_code == 200
        api_key = reg.json()["api_key"]

        # Try to access a protected endpoint with the user's API key
        resp = client_approval_on.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403
        assert "awaiting admin approval" in resp.json()["detail"].lower()

    def test_admin_can_list_pending_users(self, client_approval_on):
        """Admin can list pending users."""
        # Register two users
        client_approval_on.post("/api/auth/register", json={"username": "penduser2"})
        client_approval_on.post("/api/auth/register", json={"username": "penduser3"})

        resp = client_approval_on.get(
            "/api/admin/users/pending",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        users = resp.json()["users"]
        usernames = [u["username"] for u in users]
        assert "penduser2" in usernames
        assert "penduser3" in usernames

    def test_admin_can_approve_user(self, client_approval_on):
        """Admin approves a pending user, user becomes active."""
        reg = client_approval_on.post(
            "/api/auth/register", json={"username": "toapprove"}
        )
        api_key = reg.json()["api_key"]

        # Approve
        resp = client_approval_on.post(
            "/api/admin/users/toapprove/approve",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

        # Now the user can access protected endpoints
        resp = client_approval_on.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

    def test_admin_can_reject_user(self, client_approval_on):
        """Admin rejects a pending user."""
        reg = client_approval_on.post(
            "/api/auth/register", json={"username": "toreject1"}
        )
        api_key = reg.json()["api_key"]

        # Reject
        resp = client_approval_on.post(
            "/api/admin/users/toreject1/reject",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        # Rejected user still cannot access protected endpoints
        resp = client_approval_on.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403
        assert "rejected" in resp.json()["detail"].lower()

    def test_approve_nonexistent_user_returns_404(self, client_approval_on):
        """Approving a non-existent user returns 404."""
        resp = client_approval_on.post(
            "/api/admin/users/ghostuser/approve",
            headers=_admin_headers(),
        )
        assert resp.status_code == 404

    def test_approve_already_active_user_returns_400(self, client_approval_on):
        """Approving an already active user returns 400."""
        # Register and approve
        client_approval_on.post("/api/auth/register", json={"username": "activeone"})
        client_approval_on.post(
            "/api/admin/users/activeone/approve",
            headers=_admin_headers(),
        )
        # Try to approve again
        resp = client_approval_on.post(
            "/api/admin/users/activeone/approve",
            headers=_admin_headers(),
        )
        assert resp.status_code == 400
        assert "not pending" in resp.json()["detail"].lower()

    def test_reject_already_active_user_returns_400(self, client_approval_on):
        """Rejecting an already active user returns 400."""
        client_approval_on.post("/api/auth/register", json={"username": "activetwo"})
        client_approval_on.post(
            "/api/admin/users/activetwo/approve",
            headers=_admin_headers(),
        )
        resp = client_approval_on.post(
            "/api/admin/users/activetwo/reject",
            headers=_admin_headers(),
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_approve(self, client_approval_on):
        """Non-admin users cannot access approve endpoint."""
        resp = client_approval_on.post(
            "/api/admin/users/someuser/approve",
        )
        # Should get 401 (no auth) or 403 (not admin)
        assert resp.status_code in (401, 403)

    def test_non_admin_cannot_list_pending(self, client_approval_on):
        """Non-admin users cannot list pending users."""
        resp = client_approval_on.get("/api/admin/users/pending")
        assert resp.status_code in (401, 403)

    def test_admin_bypasses_pending_check(self, client_approval_on):
        """Admin (bootstrap) always bypasses pending user check."""
        resp = client_approval_on.get(
            "/api/dashboard",
            headers=_admin_headers(),
        )
        assert resp.status_code == 200


class TestRegisterWithoutApproval:
    """Test registration when REQUIRE_APPROVAL is False (backward compat)."""

    def test_register_creates_active_user(self, client_approval_off):
        """Registering with REQUIRE_APPROVAL=false creates an active user."""
        resp = client_approval_off.post(
            "/api/auth/register", json={"username": "freeuser1"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["username"] == "freeuser1"
        assert "api_key" in data

    def test_active_user_can_access_protected_endpoints(self, client_approval_off):
        """Active user (no approval required) can access protected endpoints."""
        reg = client_approval_off.post(
            "/api/auth/register", json={"username": "freeuser2"}
        )
        api_key = reg.json()["api_key"]

        resp = client_approval_off.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200


class TestStorageFunctions:
    """Test storage-level functions for user status management."""

    def test_register_user_with_status(self, _init_db, temp_db_path):
        """register_user_with_status creates a user with the given status."""
        with patch.dict(
            os.environ,
            {
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
            },  # pragma: allowlist secret
        ):
            with patch.object(storage, "DB_PATH", temp_db_path):
                row_id = asyncio.run(
                    storage.register_user_with_status(
                        "statususer", "fake_hash_abc", status="pending"
                    )
                )
                assert row_id > 0

                status = asyncio.run(storage.get_user_status("statususer"))
                assert status == "pending"

    def test_set_user_status(self, _init_db, temp_db_path):
        """set_user_status changes user status."""
        with patch.dict(
            os.environ,
            {
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
            },  # pragma: allowlist secret
        ):
            with patch.object(storage, "DB_PATH", temp_db_path):
                asyncio.run(
                    storage.register_user_with_status(
                        "statuschange", "fake_hash_def", status="pending"
                    )
                )
                result = asyncio.run(storage.set_user_status("statuschange", "active"))
                assert result is True

                status = asyncio.run(storage.get_user_status("statuschange"))
                assert status == "active"

    def test_set_user_status_invalid(self, _init_db, temp_db_path):
        """set_user_status rejects invalid status values."""
        with patch.dict(
            os.environ,
            {
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
            },  # pragma: allowlist secret
        ):
            with patch.object(storage, "DB_PATH", temp_db_path):
                with pytest.raises(ValueError, match="Invalid status"):
                    asyncio.run(storage.set_user_status("anyone", "bogus"))

    def test_get_user_status_nonexistent(self, _init_db, temp_db_path):
        """get_user_status returns None for non-existent user."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            status = asyncio.run(storage.get_user_status("noone"))
            assert status is None

    def test_list_pending_users(self, _init_db, temp_db_path):
        """list_pending_users returns only pending users."""
        with patch.dict(
            os.environ,
            {
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
            },  # pragma: allowlist secret
        ):
            with patch.object(storage, "DB_PATH", temp_db_path):
                asyncio.run(
                    storage.register_user_with_status(
                        "pend01", "hash01", status="pending"
                    )
                )
                asyncio.run(
                    storage.register_user_with_status(
                        "active01", "hash02", status="active"
                    )
                )
                asyncio.run(
                    storage.register_user_with_status(
                        "pend02", "hash03", status="pending"
                    )
                )

                pending = asyncio.run(storage.list_pending_users())
                usernames = [u["username"] for u in pending]
                assert "pend01" in usernames
                assert "pend02" in usernames
                assert "active01" not in usernames

    def test_existing_users_default_to_active(self, _init_db, temp_db_path):
        """Users created before the migration default to 'active' status."""
        with patch.dict(
            os.environ,
            {
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac"
            },  # pragma: allowlist secret
        ):
            with patch.object(storage, "DB_PATH", temp_db_path):
                # create_user (old style) should have active status via DB default
                asyncio.run(storage.create_user("legacyuser"))
                status = asyncio.run(storage.get_user_status("legacyuser"))
                assert status == "active"


class TestConfigSetting:
    """Test the REQUIRE_APPROVAL configuration setting."""

    def test_default_is_true(self):
        """REQUIRE_APPROVAL defaults to True."""
        with patch.dict(
            os.environ,
            {
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            },
            clear=False,
        ):
            get_settings.cache_clear()
            try:
                settings = get_settings()
                assert settings.require_approval is True
            finally:
                get_settings.cache_clear()

    def test_can_set_false(self):
        """REQUIRE_APPROVAL can be set to False via env var."""
        with patch.dict(
            os.environ,
            {
                "REQUIRE_APPROVAL": "false",
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            },
            clear=False,
        ):
            get_settings.cache_clear()
            try:
                settings = get_settings()
                assert settings.require_approval is False
            finally:
                get_settings.cache_clear()
