"""Tests for user API key authentication (issue #30).

Tests the new user registration, key rotation, and auth enforcement.
"""

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
def client(_init_db, temp_db_path):
    """Create a test client with admin key configured."""
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


def _admin_login(client):
    """Helper to login as admin and return cookies."""
    resp = client.post(
        "/api/auth/login",
        json={
            "username": "admin",
            "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 200
    return resp.cookies


def _register_user(client, username):
    """Register a user and return (cookies, api_key)."""
    resp = client.post("/api/auth/register", json={"username": username})
    assert resp.status_code == 200
    data = resp.json()
    session = resp.cookies.get("rootcoz_session", "")
    assert session, "Registration should set rootcoz_session cookie"
    cookies = {"rootcoz_session": session}
    return cookies, data["api_key"]


class TestUserRegistration:
    """Tests for POST /api/auth/register."""

    def test_register_new_user(self, client):
        resp = client.post("/api/auth/register", json={"username": "newuser"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "newuser"
        assert data["role"] == "reviewer"
        assert data["is_admin"] is False
        assert "api_key" in data
        assert data["api_key"].startswith("rootcoz_")
        assert "message" in data
        assert "rootcoz_session" in resp.cookies
        assert (
            resp.headers.get("cache-control") == "no-store, no-cache, must-revalidate"
        )

    def test_register_returns_session_cookie(self, client):
        resp = client.post("/api/auth/register", json={"username": "sessionuser"})
        assert resp.status_code == 200
        session = resp.cookies.get("rootcoz_session")
        assert session
        # Verify session is valid
        resp2 = client.get("/api/auth/me", cookies={"rootcoz_session": session})
        assert resp2.status_code == 200
        assert resp2.json()["username"] == "sessionuser"

    def test_register_admin_forbidden(self, client):
        resp = client.post("/api/auth/register", json={"username": "admin"})
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()

    def test_register_system_tag_username_forbidden(self, client):
        """Usernames matching system tags (e.g. 're-analyze') are rejected."""
        resp = client.post("/api/auth/register", json={"username": "re-analyze"})
        assert resp.status_code == 400
        assert "system tag" in resp.json()["detail"].lower()

    def test_register_invalid_username(self, client):
        resp = client.post("/api/auth/register", json={"username": "a"})
        assert resp.status_code == 400
        assert "Invalid username" in resp.json()["detail"]

    def test_register_empty_username(self, client):
        resp = client.post("/api/auth/register", json={"username": ""})
        assert resp.status_code == 400
        assert "required" in resp.json()["detail"].lower()

    def test_register_missing_username(self, client):
        resp = client.post("/api/auth/register", json={})
        assert resp.status_code == 400

    def test_register_duplicate_user_with_key(self, client):
        """Second registration for same user (who already has a key) fails."""
        first = client.post("/api/auth/register", json={"username": "dupuser"})
        assert first.status_code == 200
        resp = client.post("/api/auth/register", json={"username": "dupuser"})
        assert resp.status_code == 400
        assert "already has" in resp.json()["detail"].lower()

    def test_register_legacy_user_migration(self, client, temp_db_path):
        """Pre-tracked user (cookie-only, no key) can register to get an API key."""
        # Simulate a legacy user created by track_user (no api_key_hash)
        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(storage.track_user("legacyuser"))

        resp = client.post("/api/auth/register", json={"username": "legacyuser"})
        assert resp.status_code == 200
        data = resp.json()
        assert "api_key" in data
        assert data["api_key"].startswith("rootcoz_")
        assert "rootcoz_session" in resp.cookies

        # Can now login with the new key
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "legacyuser", "api_key": data["api_key"]},
        )
        assert login_resp.status_code == 200

    def test_register_public_no_auth_needed(self, client):
        """Registration endpoint is accessible without any authentication."""
        resp = client.post("/api/auth/register", json={"username": "publicuser"})
        assert resp.status_code == 200


class TestUserLogin:
    """Tests for user (non-admin) login with API key."""

    def test_user_login_with_api_key(self, client):
        """Registered user can login with their API key."""
        # Register
        reg_resp = client.post("/api/auth/register", json={"username": "loginuser"})
        api_key = reg_resp.json()["api_key"]

        # Login
        resp = client.post(
            "/api/auth/login",
            json={"username": "loginuser", "api_key": api_key},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "loginuser"
        assert data["is_admin"] is False
        assert data["role"] == "reviewer"
        assert "rootcoz_session" in resp.cookies

    def test_user_login_wrong_key(self, client):
        client.post("/api/auth/register", json={"username": "wrongkeyuser"})
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "wrongkeyuser",
                "api_key": "wrong-key-value",  # pragma: allowlist secret
            },
        )
        assert resp.status_code == 401

    def test_user_bearer_auth(self, client):
        """User can authenticate via Bearer token with their API key."""
        _, api_key = _register_user(client, "beareruser")
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "beareruser"
        assert data["is_admin"] is False


class TestNeedsKeyEndpoint:
    """Tests for GET /api/auth/needs-key."""

    def test_needs_key_no_auth(self, client):
        """Unauthenticated user needs a key."""
        resp = client.get("/api/auth/needs-key")
        assert resp.status_code == 200
        data = resp.json()
        assert data["needs_key"] is True
        assert data["username"] == ""

    def test_needs_key_registered_user(self, client):
        """Registered user with key doesn't need one."""
        cookies, _ = _register_user(client, "haskey")
        resp = client.get("/api/auth/needs-key", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["needs_key"] is False
        assert data["username"] == "haskey"

    def test_needs_key_is_public(self, client):
        """Needs-key endpoint is accessible without authentication."""
        resp = client.get("/api/auth/needs-key")
        assert resp.status_code == 200


class TestAuthEnforcement:
    """Tests that API endpoints require authentication."""

    def test_api_endpoint_requires_auth(self, client):
        """API endpoints return 401 without auth."""
        resp = client.get("/api/dashboard")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json()["detail"]

    def test_api_endpoint_cookie_only_rejected(self, client):
        """Cookie-only (no session) is rejected."""
        resp = client.get("/api/dashboard", cookies={"rootcoz_username": "someone"})
        assert resp.status_code == 401

    def test_api_endpoint_with_session(self, client):
        """Session-authenticated user can access API endpoints."""
        cookies, _ = _register_user(client, "dashuser")
        resp = client.get("/api/dashboard", cookies=cookies)
        assert resp.status_code == 200

    def test_api_endpoint_with_bearer(self, client):
        """Bearer-authenticated user can access API endpoints."""
        _, api_key = _register_user(client, "bearerdash")
        resp = client.get(
            "/api/dashboard",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

    def test_api_endpoint_with_admin_bearer(self, client):
        """Admin Bearer token works."""
        resp = client.get(
            "/api/dashboard",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        assert resp.status_code == 200

    def test_public_paths_accessible(self, client):
        """Public paths work without auth."""
        for path in ["/health", "/api/health"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"Failed for {path}"
        # Login is POST-only, GET should not return 401/403 (405 is acceptable)
        resp = client.get("/api/auth/login")
        assert resp.status_code in (200, 404, 405), (
            f"Expected 200, 404 or 405 for /api/auth/login, got {resp.status_code}"
        )

    def test_html_redirect_without_auth(self, client):
        """Browser requests without auth redirect to /login."""
        resp = client.get(
            "/some-page",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestKeyRotation:
    """Tests for admin key rotation of user keys."""

    def test_admin_can_rotate_user_key(self, client):
        """Admin can rotate a regular user's API key."""
        _register_user(client, "rotateuser")
        admin_cookies = _admin_login(client)
        resp = client.post(
            "/api/admin/users/rotateuser/rotate-key",
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "rotateuser"
        assert "new_api_key" in data

    def test_rotated_key_works(self, client):
        """After rotation, the new key works and old one doesn't."""
        _, old_key = _register_user(client, "rotateworker")
        resp = client.post(
            "/api/admin/users/rotateworker/rotate-key",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        assert resp.status_code == 200
        new_key = resp.json()["new_api_key"]

        # Clear any lingering session cookies
        client.cookies.clear()

        # New key works
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {new_key}"},
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "rotateworker"

        # Old key doesn't work
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {old_key}"},
        )
        assert resp.status_code == 401

    def test_rotate_nonexistent_user(self, client):
        admin_cookies = _admin_login(client)
        resp = client.post(
            "/api/admin/users/nonexistent/rotate-key",
            cookies=admin_cookies,
        )
        assert resp.status_code == 404

    def test_rotate_requires_admin(self, client):
        """Non-admin cannot rotate keys."""
        cookies, _ = _register_user(client, "nonadmin")
        resp = client.post(
            "/api/admin/users/nonadmin/rotate-key",
            cookies=cookies,
        )
        assert resp.status_code == 403

    def test_old_session_invalid_after_rotation(self, client):
        """After key rotation, old sessions should be invalidated."""
        # Register a user
        resp = client.post("/api/auth/register", json={"username": "sessionrotateuser"})
        assert resp.status_code == 200
        # Extract session cookie from the response
        session_cookie = resp.cookies.get("rootcoz_session", "")
        assert session_cookie

        # Verify session works
        client.cookies.clear()
        resp = client.get("/api/auth/me", cookies={"rootcoz_session": session_cookie})
        assert resp.status_code == 200
        assert resp.json()["username"] == "sessionrotateuser"

        # Admin rotates the key
        client.cookies.clear()
        resp = client.post(
            "/api/admin/users/sessionrotateuser/rotate-key",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        assert resp.status_code == 200

        # Old session should now be invalid
        client.cookies.clear()
        resp = client.get("/api/auth/me", cookies={"rootcoz_session": session_cookie})
        assert resp.status_code == 401


class TestSelfServiceKeyRotation:
    """Tests for POST /api/auth/rotate-key."""

    def test_rotate_own_key(self, client):
        """Authenticated user can rotate their own key."""
        resp = client.post("/api/auth/register", json={"username": "rotateself"})
        assert resp.status_code == 200
        old_key = resp.json()["api_key"]
        session = resp.cookies.get("rootcoz_session")

        # Rotate using session
        rotate_resp = client.post(
            "/api/auth/rotate-key",
            cookies={"rootcoz_session": session},
        )
        assert rotate_resp.status_code == 200
        data = rotate_resp.json()
        assert "new_api_key" in data
        assert data["new_api_key"] != old_key
        assert data["username"] == "rotateself"
        # Should get a new session cookie
        assert "rootcoz_session" in rotate_resp.cookies
        assert (
            rotate_resp.headers.get("cache-control")
            == "no-store, no-cache, must-revalidate"
        )

    def test_rotate_key_unauthenticated(self, client):
        """Unauthenticated request gets 401."""
        resp = client.post(
            "/api/auth/rotate-key",
            headers={"Authorization": ""},
        )
        assert resp.status_code == 401

    def test_rotate_key_old_session_invalid(self, client):
        """After rotation, old session is invalidated."""
        resp = client.post("/api/auth/register", json={"username": "rotatesession"})
        assert resp.status_code == 200
        old_session = resp.cookies.get("rootcoz_session")

        # Rotate
        rotate_resp = client.post(
            "/api/auth/rotate-key",
            cookies={"rootcoz_session": old_session},
        )
        assert rotate_resp.status_code == 200

        # Old session should be invalid
        me_resp = client.get(
            "/api/auth/me",
            headers={"Authorization": ""},
            cookies={"rootcoz_session": old_session},
        )
        assert me_resp.status_code == 401

    def test_rotate_key_old_key_invalid(self, client):
        """After rotation, old API key no longer works for login."""
        resp = client.post("/api/auth/register", json={"username": "rotatekeycheck"})
        assert resp.status_code == 200
        old_key = resp.json()["api_key"]
        session = resp.cookies.get("rootcoz_session")

        # Rotate
        client.post(
            "/api/auth/rotate-key",
            cookies={"rootcoz_session": session},
        )

        # Old key should not work
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "rotatekeycheck", "api_key": old_key},
        )
        assert login_resp.status_code == 401

    def test_rotate_key_bootstrap_admin_blocked(self, client):
        """Bootstrap admin user cannot rotate via self-service."""
        resp = client.post(
            "/api/auth/rotate-key",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        assert resp.status_code == 400
        assert "admin" in resp.json()["detail"].lower()


class TestSSOWithAuth:
    """Tests that SSO (proxy headers) users are properly authenticated."""

    @pytest.fixture
    def proxy_client(self, _init_db, temp_db_path):
        """Create a test client with TRUST_PROXY_HEADERS enabled."""
        with patch.dict(
            os.environ,
            {
                "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
                "SECURE_COOKIES": "false",
                "DB_PATH": str(temp_db_path),
                "TRUST_PROXY_HEADERS": "true",
                "REQUIRE_APPROVAL": "false",
            },
        ):
            get_settings.cache_clear()
            with patch.object(storage, "DB_PATH", temp_db_path):
                from rootcoz.main import app

                with TestClient(app) as c:
                    yield c
            get_settings.cache_clear()

    def test_sso_user_can_access_api(self, proxy_client):
        """SSO users can access API endpoints without registration."""
        resp = proxy_client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "sso-user"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "sso-user"

    def test_sso_user_can_access_dashboard(self, proxy_client):
        """SSO users can access the dashboard."""
        resp = proxy_client.get(
            "/api/dashboard",
            headers={"X-Forwarded-User": "sso-dashboard-user"},
        )
        assert resp.status_code == 200


class TestStorageCreateUser:
    """Tests for storage.create_user function."""

    def test_create_user(self, _init_db, temp_db_path):
        async def run():
            username, raw_key = await storage.create_user("testcreate")
            assert username == "testcreate"
            assert raw_key.startswith("rootcoz_")

            # Verify user exists and has key
            assert await storage.user_has_key("testcreate")

            # Verify user can be found by key
            user = await storage.get_user_by_key(raw_key)
            assert user is not None
            assert user["username"] == "testcreate"
            assert user["role"] == "reviewer"

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_create_user_admin_reserved(self, _init_db, temp_db_path):
        async def run():
            with pytest.raises(ValueError, match="reserved"):
                await storage.create_user("admin")

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_create_user_invalid_username(self, _init_db, temp_db_path):
        async def run():
            with pytest.raises(ValueError, match="Invalid username"):
                await storage.create_user("a")

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_create_user_duplicate_with_key(self, _init_db, temp_db_path):
        async def run():
            await storage.create_user("duptest")
            with pytest.raises(ValueError, match="already has"):
                await storage.create_user("duptest")

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_create_user_existing_without_key_generates_key(
        self, _init_db, temp_db_path
    ):
        """If user exists without key, create_user generates one."""

        async def run():
            # Track user (creates user without key)
            await storage.track_user("tracked_no_key")
            assert not await storage.user_has_key("tracked_no_key")

            # create_user should generate a key for existing user without one
            _, api_key = await storage.create_user("tracked_no_key")
            assert api_key.startswith("rootcoz_")

            # User now has a key
            assert await storage.user_has_key("tracked_no_key")

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())


class TestStorageRotateUserKey:
    """Tests for storage.rotate_user_key function."""

    def test_rotate_key(self, _init_db, temp_db_path):
        async def run():
            _, old_key = await storage.create_user("rotatetest")
            new_key = await storage.rotate_user_key("rotatetest")
            assert new_key.startswith("rootcoz_")
            assert new_key != old_key

            # Old key shouldn't work
            assert await storage.get_user_by_key(old_key) is None

            # New key should work
            user = await storage.get_user_by_key(new_key)
            assert user is not None
            assert user["username"] == "rotatetest"

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_rotate_nonexistent(self, _init_db, temp_db_path):
        async def run():
            with pytest.raises(ValueError, match="not found"):
                await storage.rotate_user_key("nonexistent")

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())


class TestStorageUserHasKey:
    """Tests for storage.user_has_key function."""

    def test_user_with_key(self, _init_db, temp_db_path):
        async def run():
            await storage.create_user("haskey")
            assert await storage.user_has_key("haskey") is True

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_user_without_key(self, _init_db, temp_db_path):
        async def run():
            await storage.track_user("nokey")
            assert await storage.user_has_key("nokey") is False

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_nonexistent_user(self, _init_db, temp_db_path):
        async def run():
            assert await storage.user_has_key("ghost") is False

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_track_user_skips_system_tag_username(self, _init_db, temp_db_path):
        """track_user with a system tag name (e.g. 're-analyze') is a no-op."""

        async def run():
            await storage.track_user("re-analyze")
            # No user should be created
            assert await storage.user_has_key("re-analyze") is False

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())


class TestCaseInsensitiveUsernames:
    """Tests for case-insensitive username uniqueness (issue #125)."""

    def test_register_normalizes_mixed_case(self, client):
        """Registration with mixed-case username normalizes to lowercase."""
        resp = client.post("/api/auth/register", json={"username": "Alice"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "alice"

    def test_track_user_normalizes_mixed_case(self, _init_db, temp_db_path):
        """track_user with mixed-case normalizes to lowercase."""

        async def run():
            await storage.track_user("BobSmith")
            user = await storage.get_user_by_username("bobsmith")
            assert user is not None
            assert user["username"] == "bobsmith"

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_duplicate_case_variant_prevented(self, client):
        """Registering case-variant of existing username is rejected."""
        resp = client.post("/api/auth/register", json={"username": "charlie"})
        assert resp.status_code == 200
        resp2 = client.post("/api/auth/register", json={"username": "Charlie"})
        assert resp2.status_code == 400
        assert "already has" in resp2.json()["detail"].lower()

    def test_create_admin_user_normalizes(self, _init_db, temp_db_path):
        """create_admin_user normalizes username to lowercase."""

        async def run():
            username, _key = await storage.create_admin_user("DaveAdmin")
            assert username == "daveadmin"
            user = await storage.get_user_by_username("daveadmin")
            assert user is not None
            assert user["role"] == "admin"

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_login_with_mixed_case_username(self, client):
        """Login with mixed-case username matches stored lowercase user."""
        resp = client.post("/api/auth/register", json={"username": "logintest"})
        assert resp.status_code == 200
        api_key = resp.json()["api_key"]
        # Login with different casing
        resp2 = client.post(
            "/api/auth/login",
            json={"username": "LoginTest", "api_key": api_key},
        )
        assert resp2.status_code == 200
        assert resp2.json()["username"] == "logintest"

    def test_get_user_by_username_case_insensitive(self, _init_db, temp_db_path):
        """get_user_by_username normalizes input so lookup is case-insensitive."""

        async def run():
            await storage.track_user("eve")
            user = await storage.get_user_by_username("Eve")
            assert user is not None
            assert user["username"] == "eve"

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_migration_merges_case_variant_duplicates(self, temp_db_path):
        """Startup migration merges existing case-variant usernames."""

        async def run():
            # Pre-populate DB with case-variant users bypassing normalization.
            async with storage._connect_db() as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS users ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "username TEXT UNIQUE NOT NULL, "
                    "api_key_hash TEXT UNIQUE, "
                    "role TEXT NOT NULL DEFAULT 'reviewer', "
                    "status TEXT NOT NULL DEFAULT 'active', "
                    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                    "last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                # Insert case variants: Frank (operator, earlier) and FRANK (admin, later)
                await db.execute(
                    "INSERT INTO users (username, role, created_at) "
                    "VALUES ('Frank', 'operator', '2024-01-01 00:00:00')"
                )
                await db.execute(
                    "INSERT INTO users (username, role, created_at) "
                    "VALUES ('FRANK', 'admin', '2024-06-01 00:00:00')"
                )
                await db.commit()

            # Run init_db which triggers the migration
            await storage.init_db()

            # Verify: one user 'frank' with admin role (highest privilege)
            async with storage._connect_db() as db:
                cursor = await db.execute(
                    "SELECT username, role FROM users WHERE lower(username) = 'frank'"
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                assert len(rows) == 1
                assert rows[0]["username"] == "frank"
                assert rows[0]["role"] == "admin"

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_migration_preserves_api_key_from_duplicate(self, temp_db_path):
        """Migration preserves API key when survivor has none but duplicate does."""

        async def run():
            async with storage._connect_db() as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS users ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "username TEXT UNIQUE NOT NULL, "
                    "api_key_hash TEXT UNIQUE, "
                    "role TEXT NOT NULL DEFAULT 'reviewer', "
                    "status TEXT NOT NULL DEFAULT 'active', "
                    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                    "last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                # Earlier user has no key, later duplicate has a key
                await db.execute(
                    "INSERT INTO users (username, role, api_key_hash, created_at) "
                    "VALUES ('Grace', 'reviewer', NULL, '2024-01-01 00:00:00')"
                )
                await db.execute(
                    "INSERT INTO users (username, role, api_key_hash, created_at) "
                    "VALUES ('GRACE', 'reviewer', 'hash_from_dup', '2024-06-01 00:00:00')"  # pragma: allowlist secret
                )
                await db.commit()

            await storage.init_db()

            async with storage._connect_db() as db:
                cursor = await db.execute(
                    "SELECT username, api_key_hash FROM users "
                    "WHERE lower(username) = 'grace'"
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                assert len(rows) == 1
                assert rows[0]["username"] == "grace"
                expected = "hash_from_dup"  # pragma: allowlist secret
                assert rows[0]["api_key_hash"] == expected

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())
