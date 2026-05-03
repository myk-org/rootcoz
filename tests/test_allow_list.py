"""Tests for the ALLOWED_USERS allow list feature (#117)."""

import asyncio
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jenkins_job_insight import storage
from jenkins_job_insight.config import Settings, get_settings


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def _seed_result(_init_db, temp_db_path):
    """Create a completed result with a failure for testing write endpoints."""
    result_data = {
        "status": "completed",
        "summary": "1 failure",
        "failures": [
            {
                "test_name": "test_foo",
                "error": "AssertionError",
                "analysis": {"classification": "CODE ISSUE", "details": "test"},
            }
        ],
    }
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(
            storage.save_result("job-1", "http://jenkins/1", "completed", result_data)
        )


def _make_client(temp_db_path, allowed_users: str = "", admin_key: str = ""):
    """Create a test client with allow list configured."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ALLOWED_USERS", "ADMIN_KEY", "JJI_ENCRYPTION_KEY"}
    }
    env["SECURE_COOKIES"] = "false"
    env["DB_PATH"] = str(temp_db_path)
    if allowed_users:
        env["ALLOWED_USERS"] = allowed_users
    if admin_key:
        env["ADMIN_KEY"] = admin_key
        env["JJI_ENCRYPTION_KEY"] = "test-key-for-hmac"  # pragma: allowlist secret
    with patch.dict(os.environ, env, clear=True):
        get_settings.cache_clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            from jenkins_job_insight.main import app

            with TestClient(app) as c:
                yield c
        get_settings.cache_clear()


@pytest.fixture
def client_open(_init_db, temp_db_path):
    """Client with no allow list (open access)."""
    yield from _make_client(temp_db_path)


@pytest.fixture
def client_restricted(_seed_result, temp_db_path):
    """Client with allow list set to 'alice,bob'."""
    yield from _make_client(
        temp_db_path,
        allowed_users="alice,bob",
        admin_key="test-admin-key-16chars",  # pragma: allowlist secret
    )


class TestAllowListConfig:
    """Test ALLOWED_USERS parsing in Settings."""

    def test_empty_allowed_users(self):
        with patch.dict(os.environ, {"ALLOWED_USERS": ""}, clear=False):
            get_settings.cache_clear()
            s = Settings()
            assert s.allowed_users_set == frozenset()
        get_settings.cache_clear()

    def test_single_user(self):
        with patch.dict(os.environ, {"ALLOWED_USERS": "alice"}, clear=False):
            get_settings.cache_clear()
            s = Settings()
            assert s.allowed_users_set == frozenset({"alice"})
        get_settings.cache_clear()

    def test_multiple_users(self):
        with patch.dict(
            os.environ, {"ALLOWED_USERS": "alice, Bob, Charlie"}, clear=False
        ):
            get_settings.cache_clear()
            s = Settings()
            # Case-insensitive (lowercased)
            assert s.allowed_users_set == frozenset({"alice", "bob", "charlie"})
        get_settings.cache_clear()

    def test_whitespace_only(self):
        with patch.dict(os.environ, {"ALLOWED_USERS": "  ,  , "}, clear=False):
            get_settings.cache_clear()
            s = Settings()
            assert s.allowed_users_set == frozenset()
        get_settings.cache_clear()


class TestOpenAccess:
    """When ALLOWED_USERS is empty, all users can write."""

    def test_comment_allowed_without_allow_list(self, client_open):
        """Any user can add a comment when allow list is empty."""
        # Create a result first
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
            storage.save_result("job-open", "http://j/1", "completed", result_data)
        )
        resp = client_open.post(
            "/results/job-open/comments",
            json={"test_name": "test_foo", "comment": "looks good"},
            cookies={"jji_username": "anyone"},
        )
        assert resp.status_code == 201


class TestRestrictedAccess:
    """When ALLOWED_USERS is set, only listed users can write."""

    def test_allowed_user_can_comment(self, client_restricted):
        resp = client_restricted.post(
            "/results/job-1/comments",
            json={"test_name": "test_foo", "comment": "fix coming"},
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 201

    def test_allowed_user_case_insensitive(self, client_restricted):
        """Allow list matching is case-insensitive."""
        resp = client_restricted.post(
            "/results/job-1/comments",
            json={"test_name": "test_foo", "comment": "fix coming"},
            cookies={"jji_username": "Alice"},
        )
        assert resp.status_code == 201

    def test_blocked_user_gets_403(self, client_restricted):
        resp = client_restricted.post(
            "/results/job-1/comments",
            json={"test_name": "test_foo", "comment": "not allowed"},
            cookies={"jji_username": "charlie"},
        )
        assert resp.status_code == 403
        assert "allow list" in resp.json()["detail"].lower()

    def test_no_username_gets_403(self, client_restricted):
        """Requests without a username are blocked."""
        resp = client_restricted.post(
            "/results/job-1/comments",
            json={"test_name": "test_foo", "comment": "anon"},
        )
        assert resp.status_code == 403

    def test_admin_bypasses_allow_list(self, client_restricted):
        """Admin users always bypass the allow list."""
        resp = client_restricted.post(
            "/results/job-1/comments",
            json={"test_name": "test_foo", "comment": "admin override"},
            headers={
                "Authorization": "Bearer test-admin-key-16chars"  # pragma: allowlist secret
            },
        )
        assert resp.status_code == 201

    def test_reviewed_blocked(self, client_restricted):
        resp = client_restricted.put(
            "/results/job-1/reviewed",
            json={"test_name": "test_foo", "reviewed": True},
            cookies={"jji_username": "charlie"},
        )
        assert resp.status_code == 403

    def test_reviewed_allowed(self, client_restricted):
        resp = client_restricted.put(
            "/results/job-1/reviewed",
            json={"test_name": "test_foo", "reviewed": True},
            cookies={"jji_username": "bob"},
        )
        assert resp.status_code == 200

    def test_override_classification_blocked(self, client_restricted):
        resp = client_restricted.put(
            "/results/job-1/override-classification",
            json={"test_name": "test_foo", "classification": "PRODUCT BUG"},
            cookies={"jji_username": "charlie"},
        )
        assert resp.status_code == 403

    def test_override_classification_allowed(self, client_restricted):
        resp = client_restricted.put(
            "/results/job-1/override-classification",
            json={"test_name": "test_foo", "classification": "PRODUCT BUG"},
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 200

    def test_classify_blocked(self, client_restricted):
        resp = client_restricted.post(
            "/history/classify",
            json={
                "test_name": "test_foo",
                "classification": "FLAKY",
                "job_id": "job-1",
            },
            cookies={"jji_username": "charlie"},
        )
        assert resp.status_code == 403

    def test_classify_allowed(self, client_restricted):
        resp = client_restricted.post(
            "/history/classify",
            json={
                "test_name": "test_foo",
                "classification": "FLAKY",
                "job_id": "job-1",
            },
            cookies={"jji_username": "bob"},
        )
        assert resp.status_code == 201

    def test_read_endpoints_not_affected(self, client_restricted):
        """GET endpoints are not restricted by allow list."""
        resp = client_restricted.get(
            "/results/job-1",
            cookies={"jji_username": "charlie"},
            headers={"Accept": "application/json"},
        )
        # Should be 200 (found), NOT 403
        assert resp.status_code == 200

    def test_get_comments_not_affected(self, client_restricted):
        """GET comments endpoint is not restricted."""
        resp = client_restricted.get(
            "/results/job-1/comments",
            cookies={"jji_username": "charlie"},
        )
        assert resp.status_code == 200

    def test_nonadmin_api_key_user_in_allow_list(self, client_restricted, temp_db_path):
        """Non-admin API key user passes ALLOWED_USERS check when in allow list."""
        import aiosqlite

        from jenkins_job_insight.storage import generate_api_key, hash_api_key

        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)

        # Insert a non-admin user 'alice' (who is in the allow list) with an API key
        async def _insert():
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "UPDATE users SET api_key_hash = ? WHERE username = 'alice'",
                    (key_hash,),
                )
                rows = (await (await db.execute("SELECT changes()")).fetchone())[0]
                if rows == 0:
                    await db.execute(
                        "INSERT INTO users (username, api_key_hash, role) VALUES ('alice', ?, 'user')",
                        (key_hash,),
                    )
                await db.commit()

        asyncio.run(_insert())

        resp = client_restricted.post(
            "/results/job-1/comments",
            json={"test_name": "test_foo", "comment": "api key user comment"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 201

    def test_nonadmin_api_key_user_not_in_allow_list(
        self, client_restricted, temp_db_path
    ):
        """Non-admin API key user NOT in allow list gets 403."""
        import aiosqlite

        from jenkins_job_insight.storage import generate_api_key, hash_api_key

        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)

        # Insert a non-admin user 'charlie' (NOT in the allow list) with an API key
        async def _insert():
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "UPDATE users SET api_key_hash = ? WHERE username = 'charlie'",
                    (key_hash,),
                )
                rows = (await (await db.execute("SELECT changes()")).fetchone())[0]
                if rows == 0:
                    await db.execute(
                        "INSERT INTO users (username, api_key_hash, role) VALUES ('charlie', ?, 'user')",
                        (key_hash,),
                    )
                await db.commit()

        asyncio.run(_insert())

        resp = client_restricted.post(
            "/results/job-1/comments",
            json={"test_name": "test_foo", "comment": "should be blocked"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403
