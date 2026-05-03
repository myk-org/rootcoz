"""Tests for notification and mention API endpoints."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import contextlib

import pytest
from fastapi.testclient import TestClient

from jenkins_job_insight import storage
from jenkins_job_insight.config import get_settings


def _nullcontext():
    return contextlib.nullcontext()


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        asyncio.run(storage.track_user("alice"))
        asyncio.run(storage.track_user("bob"))
        yield


def _make_client(
    temp_db_path, vapid_env: dict | None = None, disable_vapid_auto: bool = False
):
    """Create a test client with optional VAPID config.

    When *disable_vapid_auto* is True, patches ``get_vapid_config`` to
    return ``{}`` so that auto-generation does not kick in.
    """
    env = {
        "AI_PROVIDER": "claude",
        "AI_MODEL": "test",
        "SECURE_COOKIES": "false",
        "DB_PATH": str(temp_db_path),
        "ALLOWED_USERS": "",
    }
    if vapid_env:
        env.update(vapid_env)
    with patch.dict(os.environ, env, clear=True):
        get_settings.cache_clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            ctx = (
                patch("jenkins_job_insight.vapid.get_vapid_config", return_value={})
                if disable_vapid_auto
                else _nullcontext()
            )
            with ctx:
                from jenkins_job_insight.main import app

                with TestClient(app) as c:
                    try:
                        yield c
                    finally:
                        get_settings.cache_clear()


_VAPID_ENV = {
    "VAPID_PUBLIC_KEY": "BFakePublicKeyForTesting123456789012345678901234567890",  # pragma: allowlist secret
    "VAPID_PRIVATE_KEY": "fake-private-key-for-testing",  # pragma: allowlist secret
    "VAPID_CLAIM_EMAIL": "admin@example.com",
}


@pytest.fixture
def client_with_push(_init_db, temp_db_path):
    """Client with VAPID/push configured."""
    yield from _make_client(temp_db_path, vapid_env=_VAPID_ENV)


@pytest.fixture
def client_no_push(_init_db, temp_db_path):
    """Client without VAPID/push configured (auto-generation disabled)."""
    yield from _make_client(temp_db_path, disable_vapid_auto=True)


class TestVapidPublicKey:
    """Tests for GET /api/notifications/vapid-public-key."""

    def test_returns_key_when_configured(self, client_with_push):
        resp = client_with_push.get("/api/notifications/vapid-public-key")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vapid_public_key"] == _VAPID_ENV["VAPID_PUBLIC_KEY"]

    def test_returns_404_when_not_configured(self, client_no_push):
        resp = client_no_push.get("/api/notifications/vapid-public-key")
        assert resp.status_code == 404
        assert "not configured" in resp.json()["detail"].lower()

    def test_returns_503_when_keys_unavailable(self, client_with_push):
        """Returns 503 when web_push_enabled is True but VAPID keys become unavailable."""
        with patch("jenkins_job_insight.main.get_vapid_config", return_value={}):
            resp = client_with_push.get("/api/notifications/vapid-public-key")
            assert resp.status_code == 503
            assert "unavailable" in resp.json()["detail"].lower()


class TestSubscribeNotifications:
    """Tests for POST /api/notifications/subscribe."""

    def test_subscribe_success(self, client_with_push):
        resp = client_with_push.post(
            "/api/notifications/subscribe",
            json={
                "endpoint": "https://push.example.com/sub/test-1",
                "p256dh_key": "p256dh-test",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-test",  # pragma: allowlist secret  # gitleaks:allow
            },
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "subscribed"

    def test_subscribe_401_without_username(self, client_with_push):
        resp = client_with_push.post(
            "/api/notifications/subscribe",
            json={
                "endpoint": "https://push.example.com/sub/test-1",
                "p256dh_key": "p256dh-test",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-test",  # pragma: allowlist secret  # gitleaks:allow
            },
        )
        assert resp.status_code == 401
        assert "username" in resp.json()["detail"].lower()

    def test_subscribe_404_when_push_not_configured(self, client_no_push):
        resp = client_no_push.post(
            "/api/notifications/subscribe",
            json={
                "endpoint": "https://push.example.com/sub/test-1",
                "p256dh_key": "p256dh-test",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-test",  # pragma: allowlist secret  # gitleaks:allow
            },
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 404


class TestUnsubscribeNotifications:
    """Tests for POST /api/notifications/unsubscribe."""

    def test_unsubscribe_success(self, client_with_push):
        # First subscribe
        client_with_push.post(
            "/api/notifications/subscribe",
            json={
                "endpoint": "https://push.example.com/sub/unsub-test",
                "p256dh_key": "p256dh-test",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-test",  # pragma: allowlist secret  # gitleaks:allow
            },
            cookies={"jji_username": "alice"},
        )
        # Then unsubscribe
        resp = client_with_push.post(
            "/api/notifications/unsubscribe",
            json={"endpoint": "https://push.example.com/sub/unsub-test"},
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "unsubscribed"

    def test_unsubscribe_404_for_nonexistent(self, client_with_push):
        resp = client_with_push.post(
            "/api/notifications/unsubscribe",
            json={"endpoint": "https://push.example.com/sub/nonexistent"},
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_unsubscribe_only_own_subscription(self, client_with_push):
        """A user cannot unsubscribe another user's endpoint."""
        # alice subscribes
        client_with_push.post(
            "/api/notifications/subscribe",
            json={
                "endpoint": "https://push.example.com/sub/alice-only",
                "p256dh_key": "p256dh-test",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-test",  # pragma: allowlist secret  # gitleaks:allow
            },
            cookies={"jji_username": "alice"},
        )
        # bob tries to unsubscribe alice's endpoint
        resp = client_with_push.post(
            "/api/notifications/unsubscribe",
            json={"endpoint": "https://push.example.com/sub/alice-only"},
            cookies={"jji_username": "bob"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_unsubscribe_401_without_username(self, client_with_push):
        resp = client_with_push.post(
            "/api/notifications/unsubscribe",
            json={"endpoint": "https://push.example.com/sub/test-1"},
        )
        assert resp.status_code == 401

    def test_unsubscribe_404_when_push_not_configured(self, client_no_push):
        resp = client_no_push.post(
            "/api/notifications/unsubscribe",
            json={"endpoint": "https://push.example.com/sub/test-1"},
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 404


class TestEndpointHttpsValidation:
    """Tests for HTTPS endpoint validation."""

    def test_subscribe_rejects_http_endpoint(self, client_with_push):
        resp = client_with_push.post(
            "/api/notifications/subscribe",
            json={
                "endpoint": "http://push.example.com/sub/insecure",
                "p256dh_key": "p256dh-test",  # pragma: allowlist secret  # gitleaks:allow
                "auth_key": "auth-test",  # pragma: allowlist secret  # gitleaks:allow
            },
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 422

    def test_unsubscribe_rejects_http_endpoint(self, client_with_push):
        resp = client_with_push.post(
            "/api/notifications/unsubscribe",
            json={"endpoint": "http://push.example.com/sub/insecure"},
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 422


class TestMentionableUsers:
    """Tests for GET /api/users/mentionable."""

    def test_returns_user_list(self, client_with_push):
        """Returns list of tracked users."""
        # Make a request as alice to trigger user tracking, then wait
        client_with_push.get(
            "/api/notifications/vapid-public-key",
            cookies={"jji_username": "alice"},
        )
        # Give background task time to track user
        import time

        time.sleep(0.2)

        resp = client_with_push.get(
            "/api/users/mentionable",
            cookies={"jji_username": "alice"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "usernames" in data
        assert isinstance(data["usernames"], list)

    def test_401_without_username(self, client_with_push):
        resp = client_with_push.get("/api/users/mentionable")
        assert resp.status_code == 401


class TestCommentMentionNotification:
    """Tests for mention detection in POST /results/{job_id}/comments."""

    @pytest.fixture
    def _seed_result(self, _init_db, temp_db_path):
        """Create a completed result with a failure for comment testing."""
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
                storage.save_result(
                    "job-mention", "http://jenkins/1", "completed", result_data
                )
            )

    def test_comment_with_mention_triggers_notification(
        self, _seed_result, temp_db_path
    ):
        """Adding a comment with @mention triggers send_mention_notifications."""
        with (
            patch.dict(
                os.environ,
                {
                    "AI_PROVIDER": "claude",
                    "AI_MODEL": "test",
                    "SECURE_COOKIES": "false",
                    "DB_PATH": str(temp_db_path),
                    "ALLOWED_USERS": "",
                    **_VAPID_ENV,
                },
                clear=True,
            ),
            patch.object(storage, "DB_PATH", temp_db_path),
        ):
            get_settings.cache_clear()
            with patch(
                "jenkins_job_insight.main.send_mention_notifications",
                new_callable=AsyncMock,
            ) as mock_send:
                from jenkins_job_insight.main import app

                with TestClient(app) as client:
                    resp = client.post(
                        "/results/job-mention/comments",
                        json={
                            "test_name": "test_foo",
                            "comment": "Hey @bob, can you check this?",
                        },
                        cookies={"jji_username": "alice"},
                    )
                    assert resp.status_code == 201
                    import time

                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        if mock_send.call_count > 0:
                            break
                        time.sleep(0.05)

                mock_send.assert_called_once()
                call_kwargs = mock_send.call_args.kwargs
                assert call_kwargs["mentioned_usernames"] == ["bob"]
                assert call_kwargs["comment_author"] == "alice"
                assert call_kwargs["job_id"] == "job-mention"
                assert call_kwargs["test_name"] == "test_foo"
            get_settings.cache_clear()

    def test_comment_without_mention_no_notification(self, _seed_result, temp_db_path):
        """A comment without @mentions does not trigger notifications."""
        with (
            patch.dict(
                os.environ,
                {
                    "AI_PROVIDER": "claude",
                    "AI_MODEL": "test",
                    "SECURE_COOKIES": "false",
                    "DB_PATH": str(temp_db_path),
                    "ALLOWED_USERS": "",
                    **_VAPID_ENV,
                },
                clear=True,
            ),
            patch.object(storage, "DB_PATH", temp_db_path),
        ):
            get_settings.cache_clear()
            with patch(
                "jenkins_job_insight.main.send_mention_notifications",
                new_callable=AsyncMock,
            ) as mock_send:
                from jenkins_job_insight.main import app

                with TestClient(app) as client:
                    resp = client.post(
                        "/results/job-mention/comments",
                        json={
                            "test_name": "test_foo",
                            "comment": "Looks good, no issues here.",
                        },
                        cookies={"jji_username": "alice"},
                    )
                    assert resp.status_code == 201
                    import time

                    time.sleep(0.3)  # Brief wait — then assert not called

                mock_send.assert_not_called()
            get_settings.cache_clear()
