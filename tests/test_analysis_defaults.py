"""Tests for GET /api/default-server-settings endpoint."""

import asyncio
from unittest.mock import patch

import pytest

from rootcoz import storage
from tests.conftest import admin_login, make_app_client


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def client(_init_db, temp_db_path):
    """Create a test client with default settings."""
    yield from make_app_client(temp_db_path)


@pytest.fixture
def client_with_ai(temp_db_path, _init_db):
    """Create a test client with AI provider/model configured."""
    yield from make_app_client(
        temp_db_path,
        {
            "AI_PROVIDER": "gemini",
            "AI_MODEL": "gemini-2.5-pro",
            "AI_CALL_TIMEOUT": "30",
        },
    )


@pytest.fixture
def client_with_full_config(temp_db_path, _init_db):
    """Create a test client with many settings configured."""
    yield from make_app_client(
        temp_db_path,
        {
            "AI_PROVIDER": "claude",
            "AI_MODEL": "claude-sonnet-4-20250514",
            "AI_CALL_TIMEOUT": "15",
            "TESTS_REPO_URL": "https://github.com/org/tests:develop",
            "ADDITIONAL_REPOS": "helpers:https://github.com/org/helpers",
            "PEER_AI_CONFIGS": "gemini:gemini-2.5-pro",
            "PEER_ANALYSIS_MAX_ROUNDS": "5",
            "JIRA_URL": "https://jira.example.com",
            "JIRA_EMAIL": "user@example.com",
            "JIRA_API_TOKEN": "jira-token-123",  # pragma: allowlist secret
            "JIRA_PROJECT_KEY": "PROJ",
            "GET_JOB_ARTIFACTS": "false",
            "JENKINS_ARTIFACTS_MAX_SIZE_MB": "200",
            "WAIT_FOR_COMPLETION": "false",
            "POLL_INTERVAL_MINUTES": "5",
            "MAX_WAIT_MINUTES": "60",
        },
    )


@pytest.fixture
def client_with_repo_token(temp_db_path, _init_db):
    """Create a test client with additional repos containing a token."""
    yield from make_app_client(
        temp_db_path,
        {
            "ADDITIONAL_REPOS": "myrepo:https://github.com/org/repo@secret-token"
        },  # pragma: allowlist secret
    )


@pytest.fixture
def client_with_userinfo_urls(temp_db_path, _init_db):
    """Create a test client with URLs containing userinfo credentials."""
    yield from make_app_client(
        temp_db_path,
        {
            "TESTS_REPO_URL": "https://user:pass@github.com/org/tests",  # pragma: allowlist secret
            "ADDITIONAL_REPOS": "helpers:https://token@github.com/org/helpers",
        },
    )


class TestDefaultServerSettingsAuth:
    """Authentication tests for /api/default-server-settings."""

    def test_unauthenticated_returns_401(self, client):
        """Unauthenticated requests should get 401."""
        resp = client.get("/api/default-server-settings")
        assert resp.status_code == 401

    def test_authenticated_returns_200(self, client):
        """Any authenticated user can access the endpoint."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        assert resp.status_code == 200


class TestDefaultServerSettingsResponse:
    """Test response structure and values."""

    def test_returns_all_settings_fields(self, client):
        """Response contains all non-sensitive Settings model fields."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        # Must include common non-sensitive fields
        for field in (
            "ai_provider",
            "ai_model",
            "ai_call_timeout",
            "tests_repo_url",
            "tests_repo_ref",
            "additional_repos",
            "peer_ai_configs",
            "peer_analysis_max_rounds",
            "jira_url",
            "jira_project_key",
            "get_job_artifacts",
            "jenkins_artifacts_max_size_mb",
            "wait_for_completion",
            "poll_interval_minutes",
            "max_wait_minutes",
            "jenkins_url",
            "jenkins_ssl_verify",
            "jenkins_timeout",
            "jira_enabled",
        ):
            assert field in data, f"Expected field '{field}' not in response"

    def test_no_sensitive_fields(self, client_with_full_config):
        """Response must not contain any sensitive fields."""
        cookies = admin_login(client_with_full_config)
        resp = client_with_full_config.get(
            "/api/default-server-settings", cookies=cookies
        )
        data = resp.json()
        sensitive_fields = {
            "jenkins_password",
            "jenkins_user",
            "jira_api_token",
            "jira_pat",
            "jira_email",
            "github_token",
            "tests_repo_token",
            "reportportal_api_token",
            "admin_key",
            "vapid_private_key",
        }
        for field in sensitive_fields:
            assert field not in data, f"Sensitive field '{field}' found in response"

    def test_no_server_only_fields(self, client):
        """Server-only settings must not be exposed."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        server_only_fields = {
            "default_user_role",
            "debug",
            "secure_cookies",
            "trust_proxy_headers",
            "require_approval",
            "allowed_users",
            "admin_wait_approve_msg",
            "log_level",
            "db_path",
            "enable_github_issues",
            "enable_reportportal",
            "enable_auto_review",
            "metadata_rules_file",
            "public_base_url",
            "rp_push_classifications",
            "rp_push_rootcoz_url",
            "rp_push_tracker_links",
            "vapid_claim_email",
            "vapid_public_key",
        }
        for field in server_only_fields:
            assert field not in data, f"Server-only field '{field}' found in response"

    def test_no_secretstr_values(self, client):
        """No values should be SecretStr objects (they must be excluded)."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        for key, value in data.items():
            assert not isinstance(value, str) or "SecretStr" not in str(value), (
                f"Field '{key}' contains SecretStr representation"
            )

    def test_defaults_without_ai_config(self, client):
        """Without AI env vars, ai_provider and ai_model are empty."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        assert data["ai_provider"] == ""
        assert data["ai_model"] == ""

    def test_ai_config_from_env(self, client_with_ai):
        """AI provider/model from env vars are returned."""
        cookies = admin_login(client_with_ai)
        resp = client_with_ai.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        assert data["ai_provider"] == "gemini"
        assert data["ai_model"] == "gemini-2.5-pro"
        assert data["ai_call_timeout"] == 30

    def test_full_config_values(self, client_with_full_config):
        """Configured values are returned correctly."""
        cookies = admin_login(client_with_full_config)
        resp = client_with_full_config.get(
            "/api/default-server-settings", cookies=cookies
        )
        data = resp.json()
        assert data["ai_provider"] == "claude"
        assert data["ai_model"] == "claude-sonnet-4-20250514"
        assert data["ai_call_timeout"] == 15
        assert data["tests_repo_url"] == "https://github.com/org/tests"
        assert data["tests_repo_ref"] == "develop"
        assert data["additional_repos"] == [
            {"name": "helpers", "url": "https://github.com/org/helpers", "ref": ""}
        ]
        assert data["peer_ai_configs"] == [
            {"ai_provider": "gemini", "ai_model": "gemini-2.5-pro"}
        ]
        assert data["peer_analysis_max_rounds"] == 5
        assert data["jira_url"] == "https://jira.example.com"
        assert data["jira_project_key"] == "PROJ"
        assert data["get_job_artifacts"] is False
        assert data["jenkins_artifacts_max_size_mb"] == 200
        assert data["wait_for_completion"] is False
        assert data["poll_interval_minutes"] == 5
        assert data["max_wait_minutes"] == 60
        assert data["jira_enabled"] is True

    def test_empty_lists_for_unconfigured(self, client):
        """Peer configs and additional repos default to empty lists."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        assert data["peer_ai_configs"] == []
        assert data["additional_repos"] == []
        assert data["tests_repo_ref"] == ""
        assert data["jira_enabled"] is False

    def test_none_values_coerced_to_empty_string(self, client):
        """Optional string fields with None are returned as empty string."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        # These are optional str fields that default to None
        assert data["tests_repo_url"] == ""
        assert data["jira_url"] == ""
        assert data["jira_project_key"] == ""

    def test_nullable_booleans_preserved_as_null(self, client):
        """Nullable boolean fields are returned as null, not empty string."""
        cookies = admin_login(client)
        resp = client.get("/api/default-server-settings", cookies=cookies)
        data = resp.json()
        # enable_jira is bool | None, defaults to None
        assert data["enable_jira"] is None


class TestDefaultServerSettingsTokenStripping:
    """Token stripping tests for additional_repos."""

    def test_additional_repos_strip_tokens(self, client_with_repo_token):
        """Tokens in additional_repos are stripped from the response."""
        cookies = admin_login(client_with_repo_token)
        resp = client_with_repo_token.get(
            "/api/default-server-settings", cookies=cookies
        )
        data = resp.json()
        repos = data["additional_repos"]
        assert len(repos) == 1
        assert "token" not in repos[0]
        assert repos[0]["name"] == "myrepo"
        assert repos[0]["url"] == "https://github.com/org/repo"


class TestDefaultServerSettingsUserinfoStripping:
    """Userinfo (credentials in URLs) must be stripped from responses."""

    def test_tests_repo_url_strips_userinfo(self, client_with_userinfo_urls):
        """Userinfo in tests_repo_url is stripped."""
        cookies = admin_login(client_with_userinfo_urls)
        resp = client_with_userinfo_urls.get(
            "/api/default-server-settings", cookies=cookies
        )
        data = resp.json()
        assert data["tests_repo_url"] == "https://github.com/org/tests"
        assert "user" not in data["tests_repo_url"]
        assert "pass" not in data["tests_repo_url"]

    def test_additional_repos_strips_userinfo(self, client_with_userinfo_urls):
        """Userinfo in additional repo URLs is stripped."""
        cookies = admin_login(client_with_userinfo_urls)
        resp = client_with_userinfo_urls.get(
            "/api/default-server-settings", cookies=cookies
        )
        data = resp.json()
        repos = data["additional_repos"]
        assert len(repos) == 1
        assert repos[0]["url"] == "https://github.com/org/helpers"
        assert "token" not in repos[0]["url"]
