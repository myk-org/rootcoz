"""Tests for admin authentication and user tracking."""

import asyncio
import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jenkins_job_insight import storage
from jenkins_job_insight.config import get_settings


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path."""
    import asyncio

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
            "JJI_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            "SECURE_COOKIES": "false",
            "DB_PATH": str(temp_db_path),
        },
    ):
        get_settings.cache_clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            from jenkins_job_insight.main import app

            with TestClient(app) as c:
                yield c
        get_settings.cache_clear()


def _admin_login(
    client,
    username="admin",
    api_key="test-admin-key-16chars",  # pragma: allowlist secret
):
    """Helper to login as admin and return cookies."""
    resp = client.post(
        "/api/auth/login", json={"username": username, "api_key": api_key}
    )
    assert resp.status_code == 200
    return resp.cookies


def _wait_for_user_tracked(client, username, timeout=2.0):
    """Poll until user appears in admin users list."""
    import time

    cookies = _admin_login(client)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/api/admin/users", cookies=cookies)
        users = resp.json().get("users", [])
        if any(u["username"] == username for u in users):
            # Clear admin session cookies so subsequent requests aren't affected
            client.cookies.clear()
            return
        time.sleep(0.05)
    raise TimeoutError(f"User '{username}' not tracked within {timeout}s")


class TestAuthLogin:
    def test_admin_login_success(self, client):
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["is_admin"] is True
        assert data["role"] == "admin"
        assert "jji_session" in resp.cookies

    def test_admin_login_wrong_key(self, client):
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "wrong-key",  # pragma: allowlist secret
            },
        )
        assert resp.status_code == 401

    def test_admin_login_missing_fields(self, client):
        resp = client.post("/api/auth/login", json={"username": "admin"})
        assert resp.status_code == 400

    def test_login_invalid_json(self, client):
        resp = client.post(
            "/api/auth/login",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


class TestAuthMe:
    def test_me_as_admin(self, client):
        cookies = _admin_login(client)
        resp = client.get("/api/auth/me", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is True
        assert data["role"] == "admin"

    def test_me_as_regular_user(self, client):
        resp = client.get("/api/auth/me", cookies={"jji_username": "testuser"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testuser"
        assert data["is_admin"] is False
        assert data["role"] == "user"

    def test_me_no_auth(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == ""
        assert data["is_admin"] is False


class TestAuthLogout:
    def test_logout(self, client):
        cookies = _admin_login(client)
        resp = client.post("/api/auth/logout", cookies=cookies)
        assert resp.status_code == 200
        # Session should be invalidated
        resp2 = client.get("/api/auth/me", cookies=cookies)
        data = resp2.json()
        assert data["is_admin"] is False


class TestAdminUsers:
    def test_create_admin_user(self, client):
        cookies = _admin_login(client)
        resp = client.post(
            "/api/admin/users", json={"username": "newadmin"}, cookies=cookies
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "newadmin"
        assert data["role"] == "admin"
        assert "api_key" in data

    def test_create_admin_requires_auth(self, client):
        resp = client.post("/api/admin/users", json={"username": "newadmin"})
        assert resp.status_code == 403

    def test_create_admin_regular_user_forbidden(self, client):
        resp = client.post(
            "/api/admin/users",
            json={"username": "newadmin"},
            cookies={"jji_username": "regular"},
        )
        assert resp.status_code == 403

    def test_list_users(self, client):
        cookies = _admin_login(client)
        resp = client.get("/api/admin/users", cookies=cookies)
        assert resp.status_code == 200
        assert "users" in resp.json()

    def test_list_users_requires_admin(self, client):
        resp = client.get("/api/admin/users", cookies={"jji_username": "regular"})
        assert resp.status_code == 403

    def test_delete_admin_user(self, client):
        cookies = _admin_login(client)
        # Create two admins so we can safely delete one (last-admin guard)
        client.post("/api/admin/users", json={"username": "keeper"}, cookies=cookies)
        client.post("/api/admin/users", json={"username": "todelete"}, cookies=cookies)
        resp = client.delete("/api/admin/users/todelete", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "todelete"
        # Clean up
        client.delete("/api/admin/users/keeper", cookies=cookies)

    def test_delete_self_forbidden(self, client):
        cookies = _admin_login(client)
        resp = client.delete("/api/admin/users/admin", cookies=cookies)
        assert resp.status_code == 400

    def test_delete_nonexistent(self, client):
        cookies = _admin_login(client)
        resp = client.delete("/api/admin/users/nonexistent", cookies=cookies)
        assert resp.status_code == 404

    def test_rotate_key(self, client):
        cookies = _admin_login(client)
        client.post(
            "/api/admin/users", json={"username": "rotateuser"}, cookies=cookies
        )
        resp = client.post("/api/admin/users/rotateuser/rotate-key", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "rotateuser"
        assert "new_api_key" in data


class TestDeleteJobAdminOnly:
    def test_delete_job_requires_admin(self, client):
        """Regular users cannot delete jobs."""
        resp = client.delete(
            "/results/fake-job-id", cookies={"jji_username": "regular"}
        )
        assert resp.status_code == 403

    def test_delete_job_no_auth(self, client):
        """Unauthenticated users cannot delete jobs."""
        resp = client.delete("/results/fake-job-id")
        assert resp.status_code == 403

    def test_delete_job_as_admin(self, client):
        """Admin can delete jobs."""
        cookies = _admin_login(client)
        # Will get 404 since job doesn't exist, but NOT 403
        resp = client.delete("/results/fake-job-id", cookies=cookies)
        assert resp.status_code == 404  # Not found, not forbidden


class TestBulkDeleteAdminOnly:
    def test_bulk_delete_requires_admin(self, client):
        """Regular users cannot bulk delete jobs."""
        resp = client.request(
            "DELETE",
            "/api/results/bulk",
            json={"job_ids": ["a", "b"]},
            cookies={"jji_username": "regular"},
        )
        assert resp.status_code == 403

    def test_bulk_delete_no_auth(self, client):
        """Unauthenticated users cannot bulk delete jobs."""
        resp = client.request(
            "DELETE",
            "/api/results/bulk",
            json={"job_ids": ["a", "b"]},
        )
        assert resp.status_code == 403

    def test_bulk_delete_as_admin(self, client):
        """Admin can bulk delete jobs."""
        cookies = _admin_login(client)
        resp = client.request(
            "DELETE",
            "/api/results/bulk",
            json={"job_ids": ["nonexistent-1", "nonexistent-2"]},
            cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == []
        assert len(data["failed"]) == 2
        assert data["total"] == 2

    def test_bulk_delete_empty_list(self, client):
        """Empty job_ids list returns 422 (Pydantic min_length=1 validation)."""
        cookies = _admin_login(client)
        resp = client.request(
            "DELETE",
            "/api/results/bulk",
            json={"job_ids": []},
            cookies=cookies,
        )
        assert resp.status_code == 422


class TestBearerTokenAuth:
    def test_bearer_admin_key(self, client):
        """Bearer token with admin_key works."""
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is True

    def test_bearer_user_api_key(self, client):
        """Bearer token with user API key works."""
        # Create admin user via Bearer token (admin key)
        create_resp = client.post(
            "/api/admin/users",
            json={"username": "apiuser"},
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        assert create_resp.status_code == 200
        api_key = create_resp.json()["api_key"]
        # Use the created user's API key as Bearer token
        resp = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {api_key}"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "apiuser"
        assert data["is_admin"] is True

    def test_bearer_nonadmin_api_key_sets_username(self, client, temp_db_path):
        """Bearer token with non-admin user API key sets username correctly."""
        import aiosqlite

        from jenkins_job_insight.storage import generate_api_key, hash_api_key

        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)

        # Insert a non-admin user with an API key directly
        async def _insert():
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "INSERT INTO users (username, api_key_hash, role) VALUES (?, ?, 'user')",
                    ("nonadmin-key-user", key_hash),
                )
                await db.commit()

        asyncio.run(_insert())
        resp = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "nonadmin-key-user"
        assert data["is_admin"] is False
        assert data["role"] == "user"

    def test_bearer_admin_api_key_unchanged(self, client):
        """Bearer token with admin API key still grants admin access."""
        create_resp = client.post(
            "/api/admin/users",
            json={"username": "admin-key-user"},
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        assert create_resp.status_code == 200
        api_key = create_resp.json()["api_key"]
        resp = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {api_key}"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin-key-user"
        assert data["is_admin"] is True
        assert data["role"] == "admin"

    def test_bearer_invalid_key(self, client):
        """Bearer token with invalid key returns non-admin."""
        resp = client.get(
            "/api/auth/me", headers={"Authorization": "Bearer invalid-key"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is False


class TestChangeUserRole:
    def test_promote_user_to_admin(self, client):
        cookies = _admin_login(client)
        # Create two admins so we can safely demote one (last-admin guard)
        client.post(
            "/api/admin/users", json={"username": "promoteuser"}, cookies=cookies
        )
        client.post("/api/admin/users", json={"username": "keeper"}, cookies=cookies)
        client.put(
            "/api/admin/users/promoteuser/role",
            json={"role": "user"},
            cookies=cookies,
        )
        client.delete("/api/admin/users/keeper", cookies=cookies)
        # Now promote from user to admin
        resp = client.put(
            "/api/admin/users/promoteuser/role",
            json={"role": "admin"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "promoteuser"
        assert data["role"] == "admin"
        assert "api_key" in data  # API key generated on promotion

    def test_demote_admin_to_user(self, client):
        cookies = _admin_login(client)
        # Create two admins so we can safely demote one (last-admin guard)
        client.post("/api/admin/users", json={"username": "demoteme"}, cookies=cookies)
        client.post("/api/admin/users", json={"username": "keeper2"}, cookies=cookies)
        # Demote to user
        resp = client.put(
            "/api/admin/users/demoteme/role",
            json={"role": "user"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "user"
        assert "api_key" not in data  # No key for regular users
        # Clean up
        client.delete("/api/admin/users/keeper2", cookies=cookies)

    def test_demote_last_admin_blocked(self, client):
        cookies = _admin_login(client)
        # Create a single admin — demoting should be blocked
        client.post("/api/admin/users", json={"username": "onlyadmin"}, cookies=cookies)
        resp = client.put(
            "/api/admin/users/onlyadmin/role",
            json={"role": "user"},
            cookies=cookies,
        )
        assert resp.status_code == 400
        assert "last admin" in resp.json()["detail"].lower()
        # Clean up
        client.delete("/api/admin/users/onlyadmin", cookies=cookies)

    def test_change_role_requires_admin(self, client):
        resp = client.put(
            "/api/admin/users/someone/role",
            json={"role": "admin"},
            cookies={"jji_username": "regular"},
        )
        assert resp.status_code == 403

    def test_change_role_cannot_change_self(self, client):
        cookies = _admin_login(client)
        resp = client.put(
            "/api/admin/users/admin/role",
            json={"role": "user"},
            cookies=cookies,
        )
        assert resp.status_code == 400

    def test_change_role_same_role(self, client):
        cookies = _admin_login(client)
        client.post(
            "/api/admin/users", json={"username": "alreadyadmin"}, cookies=cookies
        )
        resp = client.put(
            "/api/admin/users/alreadyadmin/role",
            json={"role": "admin"},
            cookies=cookies,
        )
        assert resp.status_code == 400

    def test_change_role_missing_role(self, client):
        cookies = _admin_login(client)
        resp = client.put(
            "/api/admin/users/someone/role",
            json={},
            cookies=cookies,
        )
        assert resp.status_code == 400

    def test_change_role_user_not_found(self, client):
        cookies = _admin_login(client)
        resp = client.put(
            "/api/admin/users/nonexistent/role",
            json={"role": "admin"},
            cookies=cookies,
        )
        assert resp.status_code == 404


class TestUserTokens:
    def test_save_and_get_tokens(self, client):
        """Tokens round-trip through encrypt/decrypt."""
        # Track a user first
        client.get("/api/dashboard", cookies={"jji_username": "tokenuser"})
        _wait_for_user_tracked(client, "tokenuser")
        # Save tokens
        resp = client.put(
            "/api/user/tokens",
            json={
                "github_token": "ghp_test123",
                "jira_email": "a@b.com",
                "jira_token": "jira_tok",
            },
            cookies={"jji_username": "tokenuser"},
        )
        assert resp.status_code == 200
        # Get tokens back
        resp = client.get("/api/user/tokens", cookies={"jji_username": "tokenuser"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["github_token"] == "ghp_test123"  # noqa: S105
        assert data["jira_email"] == "a@b.com"
        assert data["jira_token"] == "jira_tok"  # noqa: S105

    def test_get_tokens_no_user(self, client):
        resp = client.get("/api/user/tokens")
        assert resp.status_code == 401

    def test_save_tokens_no_user(self, client):
        resp = client.put("/api/user/tokens", json={"github_token": "x"})
        assert resp.status_code == 401

    def test_get_tokens_nonexistent_user(self, client):
        """Non-tracked user gets empty tokens."""
        resp = client.get("/api/user/tokens", cookies={"jji_username": "ghost"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["github_token"] == ""

    def test_save_tokens_no_username(self, client):
        """Saving tokens without a username should return 401."""
        resp = client.put(
            "/api/user/tokens",
            json={"github_token": "ghp_new"},
        )
        assert resp.status_code == 401

    def test_save_partial_tokens(self, client):
        """Saving one token should NOT wipe others."""
        client.get("/api/dashboard", cookies={"jji_username": "partial"})
        _wait_for_user_tracked(client, "partial")

        # Save all three tokens
        client.put(
            "/api/user/tokens",
            json={
                "github_token": "ghp_original",
                "jira_email": "orig@test.com",
                "jira_token": "jira_orig",
            },
            cookies={"jji_username": "partial"},
        )

        # Now update ONLY github_token
        client.put(
            "/api/user/tokens",
            json={"github_token": "ghp_updated"},
            cookies={"jji_username": "partial"},
        )

        # Verify jira tokens were NOT wiped
        resp = client.get("/api/user/tokens", cookies={"jji_username": "partial"})
        data = resp.json()
        assert data["github_token"] == "ghp_updated"  # noqa: S105
        assert data["jira_email"] == "orig@test.com"  # NOT wiped
        assert data["jira_token"] == "jira_orig"  # NOT wiped  # noqa: S105

    def test_tokens_encrypted_at_rest(self, client, temp_db_path):
        """Verify tokens are not stored as plaintext in the DB."""
        import asyncio

        import aiosqlite

        client.get("/api/dashboard", cookies={"jji_username": "enctest"})
        _wait_for_user_tracked(client, "enctest")
        client.put(
            "/api/user/tokens",
            json={"github_token": "ghp_secret_value"},
            cookies={"jji_username": "enctest"},
        )

        # Read raw DB value
        async def check():
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT github_token_enc FROM users WHERE username = 'enctest'"
                )
                row = await cursor.fetchone()
                assert row is not None
                raw = row[0]
                assert raw != "ghp_secret_value"  # Not plaintext
                assert raw.startswith("enc:")  # Encrypted

        asyncio.run(check())


class TestAdminDeleteComment:
    def test_admin_can_delete_other_users_comment(self, client):
        """Admin should be able to delete comments from other users."""
        import asyncio

        cookies = _admin_login(client)

        # Create a result with a failure so comment endpoints work
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        asyncio.run(
            storage.save_result(
                "test-job-1", "http://jenkins/1", "completed", result_data
            )
        )

        # Add a comment directly as "regularuser"
        comment_id = asyncio.run(
            storage.add_comment(
                job_id="test-job-1",
                test_name="test_foo",
                comment="regular user comment",
                username="regularuser",
            )
        )

        # Admin deletes the regular user's comment
        resp = client.delete(
            f"/results/test-job-1/comments/{comment_id}",
            cookies=cookies,
        )
        assert resp.status_code == 200

    def test_regular_user_cannot_delete_other_users_comment(self, client):
        """Regular user should NOT be able to delete another user's comment."""
        import asyncio

        # Create a result with a failure
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_bar",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        asyncio.run(
            storage.save_result(
                "test-job-2", "http://jenkins/2", "completed", result_data
            )
        )

        # Add a comment directly as "alice"
        comment_id = asyncio.run(
            storage.add_comment(
                job_id="test-job-2",
                test_name="test_bar",
                comment="alice's comment",
                username="alice",
            )
        )

        # "bob" tries to delete alice's comment — should fail
        resp = client.delete(
            f"/results/test-job-2/comments/{comment_id}",
            cookies={"jji_username": "bob"},
        )
        assert resp.status_code == 404  # Not found (not owned by bob)


class TestUserTracking:
    def test_regular_user_tracked(self, client):
        """Regular user activity is tracked in the users table."""
        # Make a request as a regular user
        client.get("/api/dashboard", cookies={"jji_username": "trackeduser"})
        # Poll until the fire-and-forget task completes
        _wait_for_user_tracked(client, "trackeduser")


class TestSessionRenewalStorage:
    """Tests for the renew_session() storage function."""

    def test_renew_session_extends_expiry(self, _init_db, temp_db_path):
        """renew_session() should extend the session's expires_at."""
        import aiosqlite

        async def run():
            # Create a session with a short TTL so we can see the difference
            token = await storage.create_session("admin", is_admin=True, ttl_hours=1)
            token_hash = storage._hash_session_token(token)

            # Read original expiry
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT expires_at FROM sessions WHERE token = ?",
                    (token_hash,),
                )
                row = await cursor.fetchone()
                original_expires = row[0]

            # Renew — this uses SESSION_TTL_HOURS (8h) from now
            result = await storage.renew_session(token)
            assert result is True

            # Read new expiry
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT expires_at FROM sessions WHERE token = ?",
                    (token_hash,),
                )
                row = await cursor.fetchone()
                renewed_expires = row[0]

            # The renewed expiry should be later than the original (1h vs 8h)
            assert renewed_expires > original_expires

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_renew_session_returns_false_for_nonexistent(self, _init_db, temp_db_path):
        """renew_session() should return False when token doesn't exist."""

        async def run():
            result = await storage.renew_session("nonexistent-token-abc123")
            assert result is False

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())

    def test_session_valid_after_renewal(self, _init_db, temp_db_path):
        """Session should still be valid (get_session returns data) after renewal."""

        async def run():
            token = await storage.create_session("admin", is_admin=True, ttl_hours=1)

            # Verify session is valid before renewal
            session_before = await storage.get_session(token)
            assert session_before is not None
            assert session_before["username"] == "admin"

            # Renew
            await storage.renew_session(token)

            # Verify session is still valid after renewal
            session_after = await storage.get_session(token)
            assert session_after is not None
            assert session_after["username"] == "admin"
            assert session_after["is_admin"]

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(run())


class TestSessionRenewalMiddleware:
    """Integration tests for session renewal in AuthMiddleware."""

    def test_authenticated_request_refreshes_session_cookie(self, client, temp_db_path):
        """An authenticated request should return a refreshed jji_session cookie
        when less than 50% of the session TTL remains."""
        import aiosqlite

        # Login to get a session
        login_resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert login_resp.status_code == 200
        session_cookie = login_resp.cookies.get("jji_session")
        assert session_cookie

        # Shorten expiry so <50% TTL remains, triggering renewal
        token_hash = storage._hash_session_token(session_cookie)

        async def shorten_expiry():
            from datetime import datetime, timedelta, timezone

            short_expires = datetime.now(timezone.utc) + timedelta(hours=1)
            expires_str = short_expires.strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "UPDATE sessions SET expires_at = ? WHERE token = ?",
                    (expires_str, token_hash),
                )
                await db.commit()

        asyncio.run(shorten_expiry())

        # Make an authenticated request
        resp = client.get("/api/auth/me", cookies={"jji_session": session_cookie})
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True

        # The response should have a refreshed jji_session cookie
        refreshed = resp.cookies.get("jji_session")
        assert refreshed is not None
        # The cookie value should be the same token (renewal doesn't rotate)
        assert refreshed == session_cookie

    def test_no_renewal_when_over_half_ttl_remains(self, client):
        """No renewal should trigger when >50% of session TTL remains (fresh session)."""
        # Login to get a fresh session (full 8h TTL)
        login_resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert login_resp.status_code == 200
        session_cookie = login_resp.cookies.get("jji_session")
        assert session_cookie

        # Make an authenticated request — no renewal expected
        resp = client.get("/api/auth/me", cookies={"jji_session": session_cookie})
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True

        # No refreshed cookie should be set (renewal was skipped)
        refreshed = resp.cookies.get("jji_session")
        assert refreshed is None

    def test_session_renewal_updates_db_expiry(self, client, temp_db_path):
        """Session renewal in middleware should update expires_at in the DB."""
        import aiosqlite

        # Login to get a session
        login_resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        session_cookie = login_resp.cookies.get("jji_session")
        token_hash = storage._hash_session_token(session_cookie)

        # Read initial expiry
        async def get_expiry():
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT expires_at FROM sessions WHERE token = ?",
                    (token_hash,),
                )
                row = await cursor.fetchone()
                return row[0] if row else None

        initial_expires = asyncio.run(get_expiry())
        assert initial_expires is not None

        # Manually set expiry to 1 hour from now (shorter than SESSION_TTL_HOURS)
        async def shorten_expiry():
            from datetime import datetime, timedelta, timezone

            short_expires = datetime.now(timezone.utc) + timedelta(hours=1)
            expires_str = short_expires.strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "UPDATE sessions SET expires_at = ? WHERE token = ?",
                    (expires_str, token_hash),
                )
                await db.commit()

        asyncio.run(shorten_expiry())
        shortened_expires = asyncio.run(get_expiry())

        # Make an authenticated request — middleware should renew
        client.get("/api/auth/me", cookies={"jji_session": session_cookie})

        # Poll until the renewal updates the DB (now synchronous, should be immediate)
        deadline = time.monotonic() + 2.0
        renewed_expires = shortened_expires
        while time.monotonic() < deadline:
            renewed_expires = asyncio.run(get_expiry())
            if renewed_expires > shortened_expires:
                break
            time.sleep(0.05)
        assert renewed_expires > shortened_expires, (
            "Session renewal did not update DB expires_at within timeout"
        )

    def test_expired_session_not_renewed(self, client, temp_db_path):
        """An expired session should NOT be renewed; user falls back to non-admin."""
        import aiosqlite

        # Login to get a session
        login_resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        session_cookie = login_resp.cookies.get("jji_session")
        token_hash = storage._hash_session_token(session_cookie)

        # Manually expire the session in the DB
        async def expire_session():
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "UPDATE sessions SET expires_at = '2000-01-01 00:00:00' WHERE token = ?",
                    (token_hash,),
                )
                await db.commit()

        asyncio.run(expire_session())

        # Make a request with the expired session — should NOT be admin
        resp = client.get("/api/auth/me", cookies={"jji_session": session_cookie})
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is False

        # The response should NOT have a refreshed jji_session cookie
        assert "jji_session" not in resp.cookies

        # Verify the session is still expired in DB (not renewed)
        async def check_still_expired():
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT expires_at FROM sessions WHERE token = ?",
                    (token_hash,),
                )
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] == "2000-01-01 00:00:00"

        asyncio.run(check_still_expired())


class TestProxyHeaders:
    """Tests for X-Forwarded-User header handling via TRUST_PROXY_HEADERS."""

    @pytest.fixture
    def proxy_client(self, _init_db, temp_db_path):
        """Create a test client with TRUST_PROXY_HEADERS enabled."""
        with patch.dict(
            os.environ,
            {
                "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
                "JJI_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
                "SECURE_COOKIES": "false",
                "DB_PATH": str(temp_db_path),
                "TRUST_PROXY_HEADERS": "true",
            },
        ):
            get_settings.cache_clear()
            with patch.object(storage, "DB_PATH", temp_db_path):
                from jenkins_job_insight.main import app

                with TestClient(app) as c:
                    yield c
            get_settings.cache_clear()

    def test_header_ignored_when_disabled(self, client):
        """X-Forwarded-User is ignored when TRUST_PROXY_HEADERS is false (default)."""
        resp = client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "sso-user"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Without cookie or session, username should be empty
        assert data["username"] == ""
        assert data["is_admin"] is False

    def test_header_sets_username_when_enabled(self, proxy_client):
        """X-Forwarded-User sets username when TRUST_PROXY_HEADERS is true."""
        resp = proxy_client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "sso-user"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "sso-user"
        assert data["is_admin"] is False
        assert data["role"] == "user"

    def test_header_sets_cookie(self, proxy_client):
        """X-Forwarded-User sets jji_username cookie on the response."""
        resp = proxy_client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "sso-user"},
        )
        assert resp.status_code == 200
        assert resp.cookies.get("jji_username") == "sso-user"

    def test_cookie_flow_works_without_header(self, proxy_client):
        """Existing cookie-based flow still works when header is absent."""
        resp = proxy_client.get(
            "/api/auth/me",
            cookies={"jji_username": "cookie-user"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "cookie-user"
        # No proxy cookie should be set when using regular cookie flow
        assert "jji_username" not in resp.cookies

    def test_header_admin_reserved(self, proxy_client):
        """X-Forwarded-User with 'admin' is rejected (reserved username)."""
        resp = proxy_client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "admin"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == ""
        assert data["is_admin"] is False

    def test_register_redirects_for_sso_user(self, proxy_client):
        """SSO user hitting /register is redirected to dashboard."""
        resp = proxy_client.get(
            "/register",
            headers={"X-Forwarded-User": "sso-user"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert resp.cookies.get("jji_username") == "sso-user"

    def test_register_admin_no_redirect(self, proxy_client):
        """SSO user 'admin' hitting /register must NOT redirect (prevents loop)."""
        for name in ("admin", "Admin", "ADMIN"):
            resp = proxy_client.get(
                "/register",
                headers={"X-Forwarded-User": name},
                follow_redirects=False,
            )
            assert resp.status_code != 303, f"admin variant '{name}' caused redirect"
            assert resp.cookies.get("jji_username") is None

    def test_register_no_redirect_without_header(self, proxy_client):
        """Non-SSO user can access /register normally (no SSO redirect)."""
        resp = proxy_client.get(
            "/register",
            follow_redirects=False,
        )
        # Should NOT redirect — either 200 (SPA served) or 404 (no frontend build)
        assert resp.status_code != 303

    def test_session_auth_takes_precedence_over_header(self, proxy_client):
        """Admin session takes precedence over X-Forwarded-User."""
        cookies = _admin_login(proxy_client)
        resp = proxy_client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "sso-user"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["is_admin"] is True

    def test_register_session_takes_precedence_over_header(self, proxy_client):
        """Session auth takes precedence over X-Forwarded-User on /register."""
        cookies = _admin_login(proxy_client)
        resp = proxy_client.get(
            "/register",
            headers={"X-Forwarded-User": "header_user"},
            cookies=cookies,
            follow_redirects=False,
        )
        # Should NOT redirect (session user is already authenticated)
        assert resp.status_code != 303
        # The jji_username cookie should NOT be overwritten with header_user
        assert resp.cookies.get("jji_username") != "header_user"

    def test_empty_header_ignored(self, proxy_client):
        """Empty X-Forwarded-User header is treated as absent."""
        resp = proxy_client.get(
            "/api/auth/me",
            headers={"X-Forwarded-User": "  "},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == ""
        assert "jji_username" not in resp.cookies
