"""Tests for admin token usage API endpoints."""

import asyncio
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jenkins_job_insight import storage
from jenkins_job_insight.config import get_settings


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


def _insert_test_records(client):
    """Insert token usage records via storage layer."""

    async def _insert():
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.05,
            duration_ms=1000,
        )
        await storage.record_token_usage(
            job_id="job-2",
            ai_provider="gemini",
            ai_model="2.5-pro",
            call_type="peer_review",
            input_tokens=200,
            output_tokens=80,
            cost_usd=0.03,
            duration_ms=800,
        )

    asyncio.run(_insert())


class TestGetTokenUsageEndpoint:
    def test_returns_data_for_admin(self, client) -> None:
        """Admin users can access token usage."""
        cookies = _admin_login(client)
        _insert_test_records(client)
        resp = client.get("/api/admin/token-usage", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 2
        assert data["total_input_tokens"] == 300

    def test_403_for_non_admin(self, client) -> None:
        """Non-admin users get 403."""
        # Make a request without admin cookies
        resp = client.get("/api/admin/token-usage")
        assert resp.status_code == 403

    def test_filter_by_provider(self, client) -> None:
        """Provider filter works."""
        cookies = _admin_login(client)
        _insert_test_records(client)
        resp = client.get(
            "/api/admin/token-usage",
            params={"ai_provider": "claude"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 1

    def test_group_by_provider(self, client) -> None:
        """Group by parameter works."""
        cookies = _admin_login(client)
        _insert_test_records(client)
        resp = client.get(
            "/api/admin/token-usage",
            params={"group_by": "provider"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["breakdown"]) == 2

    def test_empty_db_returns_zeros(self, client) -> None:
        """Empty database returns zero totals."""
        cookies = _admin_login(client)
        resp = client.get("/api/admin/token-usage", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 0

    def test_invalid_group_by_returns_422(self, client) -> None:
        """Invalid group_by value returns 422."""
        cookies = _admin_login(client)
        resp = client.get(
            "/api/admin/token-usage",
            params={"group_by": "invalid_field"},
            cookies=cookies,
        )
        assert resp.status_code == 422
        assert "Invalid group_by" in resp.json()["detail"]


class TestGetTokenUsageSummaryEndpoint:
    def test_returns_dashboard_data_for_admin(self, client) -> None:
        """Admin users can access dashboard summary."""
        cookies = _admin_login(client)
        _insert_test_records(client)
        resp = client.get("/api/admin/token-usage/summary", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "today" in data
        assert "this_week" in data
        assert "this_month" in data
        assert "top_models" in data
        assert "top_jobs" in data

    def test_403_for_non_admin(self, client) -> None:
        """Non-admin users get 403."""
        resp = client.get("/api/admin/token-usage/summary")
        assert resp.status_code == 403


class TestGetTokenUsageForJobEndpoint:
    def test_returns_records_for_job(self, client) -> None:
        """Returns records for a specific job."""
        cookies = _admin_login(client)
        _insert_test_records(client)
        resp = client.get("/api/admin/token-usage/job-1", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "job-1"
        assert len(data["records"]) == 1
        assert data["records"][0]["ai_provider"] == "claude"

    def test_404_for_no_records(self, client) -> None:
        """Returns 404 when no records exist for job."""
        cookies = _admin_login(client)
        resp = client.get("/api/admin/token-usage/nonexistent", cookies=cookies)
        assert resp.status_code == 404
        assert "No token usage records" in resp.json()["detail"]

    def test_403_for_non_admin(self, client) -> None:
        """Non-admin users get 403."""
        resp = client.get("/api/admin/token-usage/job-1")
        assert resp.status_code == 403
