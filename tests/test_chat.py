"""Tests for the chat feature."""

import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, call, patch

import pytest

from rootcoz import storage
from rootcoz.engine.chat import (
    build_admin_custom_tools,
    build_admin_system_prompt,
    build_analysis_history_tools,
    build_chat_custom_tools,
    build_chat_prompt,
    build_system_prompt,
    build_welcome_message,
)
from rootcoz.models import ChatInitRequest
from rootcoz.sources.jenkins_source import _extract_build_params

_TEST_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_TEST_ENCRYPTION_KEY = "test-encryption-key-for-hmac"  # pragma: allowlist secret
_ADMIN_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_ADMIN_KEY}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings(temp_db_path: Path):
    """Mock settings for endpoint tests."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "DB_PATH": str(temp_db_path),
        "ADMIN_KEY": _TEST_ADMIN_KEY,  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": _TEST_ENCRYPTION_KEY,  # pragma: allowlist secret
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def test_client(mock_settings, temp_db_path: Path):
    """Create a test client with admin auth."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient

        from rootcoz import ai_client
        from rootcoz.main import app

        ai_client.update_model_catalog(
            [
                {"provider": "claude", "id": "sonnet-4"},
                {"provider": "cli-cursor", "id": "composer-1"},
                {"provider": "cursor", "id": "composer-1"},
            ]
        )
        with TestClient(app, headers=_ADMIN_AUTH_HEADERS) as client:
            yield client
        ai_client.update_model_catalog(None)


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


# ---------------------------------------------------------------------------
# build_system_prompt tests
# ---------------------------------------------------------------------------


class TestBuildAnalysisHistoryTools:
    """Tests for build_analysis_history_tools (analysis history, no prompt tokens)."""

    def test_expected_tool_names(self):
        tools = build_analysis_history_tools(
            server_url="http://localhost:8700",
            auth_token="secret-tok",
            job_id="job-1",
        )
        assert [t["name"] for t in tools] == [
            "get_failure_history",
            "search_error_signature",
            "get_classification_history",
            "get_job_history_stats",
            "classify_test_pattern",
        ]

    def test_auth_only_in_http_headers(self):
        tools = build_analysis_history_tools(
            server_url="http://localhost:8700/",
            auth_token="secret-tok",
            job_id="job-1",
        )
        for tool in tools:
            assert tool["http"]["headers"]["Authorization"] == "Bearer secret-tok"
            assert "secret-tok" not in tool["description"]
            assert "secret-tok" not in tool["name"]

    def test_exclude_job_id_baked_in(self):
        tools = build_analysis_history_tools(
            server_url="http://localhost:8700",
            auth_token="tok",
            job_id="exclude-me",
        )
        hist = next(t for t in tools if t["name"] == "get_failure_history")
        assert hist["http"]["query_params"]["exclude_job_id"] == "exclude-me"
        assert hist["http"]["query_params"]["job_name"] == "{job_name}"
        assert hist["parameters"]["required"] == ["test_name", "job_name"]
        classify = next(t for t in tools if t["name"] == "classify_test_pattern")
        assert classify["http"]["body_template"]["job_id"] == "exclude-me"
        assert classify["http"]["body_template"]["source"] == "ai"
        assert "MANDATORY for every analyzed test" in classify["description"]
        assert (
            "including tests with human root-cause classifications"
            in classify["description"]
        )
        assert (
            "unless a human user already classified it" not in classify["description"]
        )

    def test_shared_history_url_and_auth_shape_with_chat(self):
        """Chat and analysis share the same endpoint + bearer header shape."""
        analysis = build_analysis_history_tools(
            server_url="http://localhost:8700",
            auth_token="tok",
            job_id="job-a",
        )
        chat = build_chat_custom_tools(
            server_url="http://localhost:8700",
            auth_token="tok",
            job_id="job-a",
        )
        a_hist = next(t for t in analysis if t["name"] == "get_failure_history")
        c_hist = next(t for t in chat if t["name"] == "get_failure_history")
        assert a_hist["http"]["url"] == c_hist["http"]["url"]
        assert a_hist["http"]["method"] == c_hist["http"]["method"] == "GET"
        assert a_hist["http"]["headers"] == c_hist["http"]["headers"]

        a_cls = next(t for t in analysis if t["name"] == "get_classification_history")
        c_cls = next(t for t in chat if t["name"] == "get_classification_history")
        assert a_cls["http"]["url"] == c_cls["http"]["url"]
        assert a_cls["http"]["headers"] == c_cls["http"]["headers"]
        from rootcoz.storage import AI_SYSTEM_USERNAME

        assert AI_SYSTEM_USERNAME in a_cls["description"]


class TestBuildChatCustomTools:
    """Tests for build_chat_custom_tools."""

    def test_basic_tools(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok123",
            job_id="job-1",
        )
        names = [t["name"] for t in tools]
        assert "get_job_result" in names
        assert "get_job_comments" in names
        assert "get_job_tests" in names
        assert "get_failure_history" in names
        assert "get_classification_history" in names
        # No jira/github without credentials
        assert "search_jira" not in names
        assert "search_github_issues" not in names
        # No issue tool either: no tracker was named for this session.
        assert "create_github_issue" not in names
        assert "create_jira_issue" not in names

    def test_get_job_tests_tool_query(self):
        """Optional status stays in query; server sanitizes unsubstituted placeholders."""
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="job-1",
        )
        tool = next(t for t in tools if t["name"] == "get_job_tests")
        assert tool["http"]["query"]["status"] == "{status}"
        assert tool["http"]["query"]["offset"] == "{offset}"
        assert tool["http"]["query"]["limit"] == "{limit}"
        assert "status" in tool["parameters"]["properties"]
        assert "status" not in tool["parameters"].get("required", [])

    def test_failure_history_tool(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
        )
        tool = next(t for t in tools if t["name"] == "get_failure_history")
        assert "test_name" in tool["parameters"]["properties"]
        assert "job_name" in tool["parameters"]["properties"]
        assert tool["parameters"]["required"] == ["test_name", "job_name"]
        assert "{test_name}" in tool["http"]["url"]
        assert tool["http"]["query_params"]["job_name"] == "{job_name}"
        assert tool["http"]["method"] == "GET"

    def test_classification_history_tool(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
        )
        tool = next(t for t in tools if t["name"] == "get_classification_history")
        assert "test_name" in tool["parameters"]["properties"]
        assert "job_id" not in tool["parameters"]["properties"]
        assert tool["parameters"]["required"] == ["test_name"]
        assert tool["http"]["query_params"]["test_name"] == "{test_name}"
        assert "job_id" not in tool["http"]["query_params"]

    def test_jira_tools(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_server_url="https://jira.example.com",
            jira_token="jira-tok",
        )
        names = [t["name"] for t in tools]
        assert "search_jira" in names
        assert "get_jira_issue" in names

    def test_jira_basic_auth(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_cloud_url="https://jira.example.com",
            jira_email="user@example.com",
            jira_token="jira-tok",
        )
        jira_tool = next(t for t in tools if t["name"] == "search_jira")
        assert jira_tool["http"]["headers"]["Authorization"].startswith("Basic ")

    def test_jira_bearer_auth(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_server_url="https://jira.example.com",
            jira_token="jira-pat",
        )
        jira_tool = next(t for t in tools if t["name"] == "search_jira")
        assert jira_tool["http"]["headers"]["Authorization"] == "Bearer jira-pat"

    def test_jira_atoa_in_regular_field_routes_to_oauth(self):
        """An ATOA secret in the regular field selects oauth mode, like validation."""
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_token="ATOA-tok",
        )
        names = [t["name"] for t in tools]
        # The sync fallback cannot mint an access token (async), so the
        # oauth connection stays unusable: no Jira tool is built rather
        # than a misrouted bearer/v2 tool. (Async callers pass a completed
        # connection via jira_connection.)
        assert "search_jira" not in names
        assert "get_jira_issue" not in names

    def test_jira_atst_in_regular_field_routes_to_atst(self):
        """An ATST token in the regular field selects atst mode, like validation."""
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_token="ATST-tok",
        )
        names = [t["name"] for t in tools]
        # The sync fallback has no cloud ID to build the gateway URL, so
        # the atst connection stays unusable here: no Jira tool is built
        # rather than a misrouted bearer/v2 tool. (Async callers pass a
        # completed connection via jira_connection.)
        assert "search_jira" not in names
        assert "get_jira_issue" not in names

    def test_jira_v2_paths_for_server_bearer(self):
        """Server/DC bearer PAT keeps v2 endpoints with a fields param."""
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_server_url="https://jira.example.com",
            jira_token="jira-pat",
        )
        jira_tool = next(t for t in tools if t["name"] == "search_jira")
        assert jira_tool["http"]["url"].endswith("/rest/api/2/search")
        assert "fields" in jira_tool["http"]["query_params"]

    def test_jira_v3_paths_for_custom_domain_cloud(self):
        """Basic-auth Cloud behind a custom domain keeps v3 — never v2."""
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_cloud_url="https://jira.corp.example.com",
            jira_email="user@example.com",
            jira_token="jira-tok",
        )
        jira_tool = next(t for t in tools if t["name"] == "search_jira")
        assert jira_tool["http"]["url"].endswith("/rest/api/3/search/jql")
        assert "fields" not in jira_tool["http"]["query_params"]

    def test_github_tools(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            github_token="gh-tok",
            github_repo="org/repo",
        )
        names = [t["name"] for t in tools]
        assert "search_github_issues" in names
        assert "get_github_issue" in names
        gh_search = next(t for t in tools if t["name"] == "search_github_issues")
        assert "org/repo" in gh_search["http"]["query_params"]["q"]

    def test_all_tools(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_server_url="https://jira.example.com",
            jira_token="jira-tok",
            github_token="gh-tok",
            github_repo="org/repo",
            tracker_targets=("github", "jira"),
        )
        names = [t["name"] for t in tools]
        assert len(names) == 12  # 5 base + 2 jira + 2 github + 2 create + 1 propose

    def test_no_jira_without_token(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_server_url="https://jira.example.com",
        )
        names = [t["name"] for t in tools]
        assert "search_jira" not in names

    def test_no_github_without_repo(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            github_token="gh-tok",
        )
        names = [t["name"] for t in tools]
        assert "search_github_issues" not in names

    def test_auth_header_in_tools(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="my-secret-token",
            job_id="j1",
        )
        for tool in tools:
            assert tool["http"]["headers"]["Authorization"] == "Bearer my-secret-token"

    def test_job_id_in_urls(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="my-job-42",
        )
        result_tool = next(t for t in tools if t["name"] == "get_job_result")
        assert "my-job-42" in result_tool["http"]["url"]
        comments_tool = next(t for t in tools if t["name"] == "get_job_comments")
        assert "my-job-42" in comments_tool["http"]["url"]


class TestResolveChatCredentials:
    """_resolve_chat_credentials: user-scoped, per-method Jira resolution.

    Covers the OAuth completion order (cloud ID from the site URL), the
    failed-selection sentinel (no silent switch to the user's other
    stored credential), and ATOA secrets saved in the regular field.
    """

    @staticmethod
    def _make_settings(**overrides):
        """Build Settings under a cleared environment (no host leakage)."""
        from rootcoz.config import Settings

        with patch.dict(os.environ, {}, clear=True):
            return Settings(_env_file=None, **overrides)

    @pytest.fixture(autouse=True)
    def _isolate_jira_caches(self):
        """Cloud-ID and minted-token caches are in-process."""
        import rootcoz.jira as jira_module

        jira_module._cloud_id_cache.clear()
        jira_module._sa_token_cache.clear()
        yield
        jira_module._cloud_id_cache.clear()
        jira_module._sa_token_cache.clear()

    async def _resolve(self, monkeypatch, settings, user_tokens):
        from rootcoz import main as main_module

        async def fake_get_user_tokens(username):
            return user_tokens

        monkeypatch.setattr(main_module, "get_settings", lambda: settings)
        monkeypatch.setattr(storage, "get_user_tokens", fake_get_user_tokens)
        return await main_module._resolve_chat_credentials({}, "alice")

    async def test_atoa_secret_in_regular_field_builds_oauth_connection(
        self, monkeypatch
    ):
        """An ATOA secret saved as jira_token routes to oauth, not basic/pat."""
        import rootcoz.jira as jira_module

        settings = self._make_settings(
            atlassian_oauth_client_id="test-client-id",
            atlassian_oauth_cloud_id="cloud-id-1",
        )
        tokens = {
            "jira_email": "user@example.com",
            "jira_token": "ATOA-saved-secret",  # pragma: allowlist secret
            "jira_sa_token": "",
            "github_token": "",
        }
        with patch.object(
            jira_module, "mint_atlassian_access_token", new_callable=AsyncMock
        ) as mint:
            mint.return_value = "minted-token"  # pragma: allowlist secret
            _email, _token, connection, _gh, _repo = await self._resolve(
                monkeypatch, settings, tokens
            )
        assert connection is not None
        assert connection.mode == "sa"
        assert connection.api_base.endswith("/cloud-id-1")
        assert connection.headers["Authorization"] == "Bearer minted-token"
        # The saved secret was minted, never used as a basic/PAT bearer
        mint.assert_awaited_once_with("test-client-id", "ATOA-saved-secret", True)

    async def test_atst_token_in_regular_field_builds_atst_connection(
        self, monkeypatch
    ):
        """An ATST token saved as jira_token routes to atst, not basic/pat."""
        settings = self._make_settings(
            atlassian_oauth_client_id="test-client-id",
            atlassian_oauth_cloud_id="cloud-id-1",
        )
        tokens = {
            "jira_email": "user@example.com",
            "jira_token": "ATST-saved-token",  # pragma: allowlist secret
            "jira_sa_token": "",
            "github_token": "",
        }
        _email, _token, connection, _gh, _repo = await self._resolve(
            monkeypatch, settings, tokens
        )
        assert connection is not None
        assert connection.mode == "atst"
        assert connection.api_base.endswith("/cloud-id-1")
        # The ATST token itself is the Bearer — no email/basic pairing
        assert connection.headers["Authorization"] == "Bearer ATST-saved-token"
        # The completed connection yields Jira chat tools
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_connection=connection,
        )
        assert "search_jira" in [t["name"] for t in tools]

    async def test_oauth_without_cloud_id_completes_from_site_url(self, monkeypatch):
        """OAuth + site URL + no cloud ID: completion resolves it (Finding 6)."""
        import rootcoz.jira as jira_module

        settings = self._make_settings(
            atlassian_oauth_client_id="test-client-id",
            atlassian_oauth_cloud_id="",
            jira_site_url="https://myorg.atlassian.net",
        )
        tokens = {
            "jira_email": "",
            "jira_token": "",
            "jira_sa_token": "ATOA-sa-secret",  # pragma: allowlist secret
            "github_token": "",
        }
        with (
            patch.object(
                jira_module,
                "resolve_jira_cloud_id",
                new_callable=AsyncMock,
                return_value="resolved-cloud-id",
            ) as resolve_cloud_id,
            patch.object(
                jira_module,
                "mint_atlassian_access_token",
                new_callable=AsyncMock,
                return_value="minted-token",  # pragma: allowlist secret
            ),
        ):
            _email, _token, connection, _gh, _repo = await self._resolve(
                monkeypatch, settings, tokens
            )
        resolve_cloud_id.assert_awaited_once_with("https://myorg.atlassian.net", True)
        assert connection is not None
        assert connection.mode == "sa"
        assert connection.api_base.endswith("/resolved-cloud-id")
        # The completed connection yields Jira chat tools
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_connection=connection,
        )
        names = [t["name"] for t in tools]
        assert "search_jira" in names
        assert "get_jira_issue" in names

    async def test_oauth_unresolvable_cloud_id_blocks_jira_tools(self, monkeypatch):
        """OAuth + site URL but the cloud ID stays unresolvable → no tools."""
        import rootcoz.jira as jira_module

        settings = self._make_settings(
            atlassian_oauth_client_id="test-client-id",
            atlassian_oauth_cloud_id="",
            jira_site_url="https://myorg.atlassian.net",
        )
        tokens = {
            "jira_email": "",
            "jira_token": "",
            "jira_sa_token": "ATOA-sa-secret",  # pragma: allowlist secret
            "github_token": "",
        }
        with patch.object(
            jira_module,
            "resolve_jira_cloud_id",
            new_callable=AsyncMock,
            return_value=None,
        ):
            _email, _token, connection, _gh, _repo = await self._resolve(
                monkeypatch, settings, tokens
            )
        # A failed selection is a sentinel (non-None, mode "none") — not None
        assert connection is not None
        assert connection.mode == "none"
        assert connection.api_base == ""
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_email=tokens["jira_email"],
            jira_token=tokens["jira_token"],
            jira_connection=connection,
        )
        assert "search_jira" not in [t["name"] for t in tools]

    async def test_failed_selected_oauth_does_not_fall_back_to_basic(self, monkeypatch):
        """A failed OAuth selection must not silently switch to basic (Finding 7)."""
        import rootcoz.jira as jira_module
        from rootcoz.jira import JiraOAuthMintError

        settings = self._make_settings(
            atlassian_oauth_client_id="test-client-id",
            atlassian_oauth_cloud_id="cloud-id-1",
        )
        # The user selected the OAuth credential, but ALSO has stored basic
        # credentials — the basic pair must not be used after OAuth fails.
        tokens = {
            "jira_email": "user@example.com",
            "jira_token": "basic-token",  # pragma: allowlist secret
            "jira_sa_token": "ATOA-sa-secret",  # pragma: allowlist secret
            "github_token": "",
        }
        with patch.object(
            jira_module,
            "mint_atlassian_access_token",
            new_callable=AsyncMock,
            side_effect=JiraOAuthMintError("mint failed"),
        ):
            _email, _token, connection, _gh, _repo = await self._resolve(
                monkeypatch, settings, tokens
            )
        # Sentinel, not None: the selection failed, it did not vanish
        assert connection is not None
        assert connection.mode == "none"
        # Chat tool building must not fall back to the stored basic pair
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_email="user@example.com",
            jira_token="basic-token",
            jira_connection=connection,
        )
        names = [t["name"] for t in tools]
        assert "search_jira" not in names
        assert "get_jira_issue" not in names

    async def test_absent_selection_returns_none(self, monkeypatch):
        """No stored Jira credentials at all → None (not the sentinel)."""
        settings = self._make_settings(jira_cloud_url="https://jira.example.com")
        tokens = {
            "jira_email": "",
            "jira_token": "",
            "jira_sa_token": "",
            "github_token": "",
        }
        _email, _token, connection, _gh, _repo = await self._resolve(
            monkeypatch, settings, tokens
        )
        assert connection is None

    async def test_selected_basic_credentials_resolve(self, monkeypatch):
        """A working basic selection still resolves to a cloud connection."""
        settings = self._make_settings(jira_cloud_url="https://jira.example.com")
        tokens = {
            "jira_email": "user@example.com",
            "jira_token": "basic-token",  # pragma: allowlist secret
            "jira_sa_token": "",
            "github_token": "",
        }
        _email, _token, connection, _gh, _repo = await self._resolve(
            monkeypatch, settings, tokens
        )
        assert connection is not None
        assert connection.mode == "cloud"
        assert connection.api_base == "https://jira.example.com"
        assert connection.auth == ("user@example.com", "basic-token")


class TestBuildAdminCustomTools:
    """Tests for build_admin_custom_tools."""

    def test_returns_six_tools(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        assert len(tools) == 6
        names = [t["name"] for t in tools]
        assert names == [
            "db_schema",
            "db_query",
            "get_report_totals",
            "get_classification_overrides",
            "get_issues_created",
            "save_report",
        ]

    def test_report_tool_urls(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        tools_by_name = {t["name"]: t for t in tools}
        assert (
            tools_by_name["get_report_totals"]["http"]["url"]
            == "http://localhost:8000/api/reports/totals"
        )
        assert (
            tools_by_name["get_classification_overrides"]["http"]["url"]
            == "http://localhost:8000/api/reports/classification-overrides"
        )
        assert (
            tools_by_name["get_issues_created"]["http"]["url"]
            == "http://localhost:8000/api/reports/issues-created"
        )

    def test_report_tools_use_get_with_query_params(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        report_tools = [
            t
            for t in tools
            if t["name"].startswith("get_report") or t["name"] == "get_issues_created"
        ]
        for tool in report_tools:
            assert tool["http"]["method"] == "GET"
            assert tool["http"]["query_params"] is True

    def test_report_tools_have_filter_params(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        expected_params = {
            "team",
            "tier",
            "version",
            "from",
            "to",
            "status",
            "tags",
            "limit",
            "offset",
        }
        report_tools = [
            t
            for t in tools
            if t["name"].startswith("get_") and t["name"] != "db_schema"
        ]
        # Exclude db_query which has sql param
        report_tools = [t for t in report_tools if t["name"] != "db_query"]
        for tool in report_tools:
            actual_params = set(tool["parameters"]["properties"].keys())
            assert actual_params == expected_params, f"{tool['name']} params mismatch"

    def test_auth_headers(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="my-secret-token",
        )
        for tool in tools:
            assert tool["http"]["headers"]["Authorization"] == "Bearer my-secret-token"

    def test_save_report_tool_config(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        save_report = next(t for t in tools if t["name"] == "save_report")
        assert save_report["http"]["method"] == "POST"
        assert (
            save_report["http"]["url"]
            == "http://localhost:8000/api/admin-chat/artifacts"
        )
        assert "html_content" in save_report["parameters"]["properties"]
        assert "filename" in save_report["parameters"]["properties"]
        assert save_report["parameters"]["required"] == [
            "html_content",
            "filename",
        ]
        assert save_report["http"]["body_template"] == {
            "html_content": "{html_content}",
            "filename": "{filename}",
        }


class TestBuildAdminSystemPrompt:
    """Tests for build_admin_system_prompt."""

    def test_includes_analytics_reports_bullet(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        prompt = build_admin_system_prompt(tools)
        assert "pre-built analytics reports" in prompt
        assert "totals" in prompt
        assert "classification overrides" in prompt
        assert "issues created" in prompt

    def test_lists_all_tool_names(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        prompt = build_admin_system_prompt(tools)
        for tool in tools:
            assert tool["name"] in prompt

    def test_includes_report_generation_instructions(self):
        tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="test-token",
        )
        prompt = build_admin_system_prompt(tools)
        assert "Report Generation" in prompt
        assert "save_report" in prompt
        assert "self-contained HTML" in prompt
        assert "CSS inline" in prompt


@pytest.fixture
def github_issue_config():
    """Enable GitHub issue creation (the server-side repo config) for a test."""
    from rootcoz.config import get_settings

    with patch.dict(
        os.environ,
        {
            "TESTS_REPO_URL": "https://github.com/org/repo",
            "GITHUB_TOKEN": "ghp_test",  # pragma: allowlist secret
        },
    ):
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


class TestChatTrackerTargets:
    """Only a tracker the server enables AND the user saved is offered in chat."""

    @staticmethod
    def _settings(**overrides):
        from rootcoz.config import Settings

        overrides.setdefault("tests_repo_url", "https://github.com/org/repo")
        # A Jira credential is usable only with a server auth method it can
        # serve: pat needs a server URL, oauth the client/cloud ids.
        overrides.setdefault("jira_server_url", "https://jira.example.com")
        overrides.setdefault("atlassian_oauth_client_id", "client-id")
        overrides.setdefault("atlassian_oauth_cloud_id", "cloud-id")
        with patch.dict(os.environ, {}, clear=True):
            return Settings(_env_file=None, **overrides)

    def _targets(self, settings, **tokens):
        from rootcoz.main import _chat_tracker_targets

        return _chat_tracker_targets(
            settings,
            tokens,
            github_repo_available=bool(settings.tests_repo_url),
        )

    def test_no_token_offers_nothing(self):
        assert self._targets(self._settings()) == ()

    def test_github_token_and_flag_on_offers_github(self):
        assert self._targets(
            self._settings(),
            github_token="ghp_user",  # pragma: allowlist secret
        ) == ("github",)

    def test_a_jobs_own_repo_offers_github_without_tests_repo_url(self):
        """A job chat files in the run's test repo, so no TESTS_REPO_URL is needed."""
        from rootcoz.main import _chat_tracker_state

        targets, unavailable = _chat_tracker_state(
            self._settings(tests_repo_url=""),
            {"github_token": "ghp_user"},  # pragma: allowlist secret
            github_repo_available=True,
        )
        assert targets == ("github",)
        assert "github" not in unavailable

    def test_github_without_a_repository_offers_nothing(self):
        """No TESTS_REPO_URL and no run repo means every create call would refuse."""
        from rootcoz.main import _chat_tracker_state

        assert (
            self._targets(
                self._settings(tests_repo_url=""),
                github_token="ghp_user",  # pragma: allowlist secret
            )
            == ()
        )
        assert (
            _chat_tracker_state(
                self._settings(tests_repo_url=""),
                {"github_token": "ghp_user"},  # pragma: allowlist secret
                github_repo_available=False,
            )[1]["github"]
            == "no_repository"
        )

    def test_a_missing_repository_is_a_400_when_asked_directly(self):
        """The gate pairs every reason with the refusal the create call answers."""
        from rootcoz.main import _chat_tracker_gate

        reason, refusal = _chat_tracker_gate(
            "github",
            self._settings(tests_repo_url=""),
            {"github_token": "ghp_user"},  # pragma: allowlist secret
            github_repo_available=False,
        )
        assert reason == "no_repository"
        assert refusal is not None and refusal.status_code == 400
        assert "TESTS_REPO_URL" in refusal.detail

    def test_a_job_chat_with_no_run_repo_does_not_send_users_to_tests_repo_url(
        self,
    ):
        """A job chat never files in TESTS_REPO_URL, so that setting is not the fix.

        Its empty repo set means the run itself names no GitHub repository: an
        administrator setting TESTS_REPO_URL cannot enable the tool there, so the
        reason and the prompt must say what can.
        """
        from rootcoz.engine.chat import _issue_create_rules
        from rootcoz.main import _chat_tracker_gate, _chat_tracker_state

        reason, refusal = _chat_tracker_gate(
            "github",
            self._settings(tests_repo_url="https://github.com/org/repo"),
            {"github_token": "ghp_user"},  # pragma: allowlist secret
            github_repo_available=False,
            job_chat=True,
        )
        assert reason == "no_run_repository"
        assert refusal is not None and refusal.status_code == 400
        assert "TESTS_REPO_URL" not in refusal.detail
        assert "run" in refusal.detail

        _targets, unavailable = _chat_tracker_state(
            self._settings(),
            {"github_token": "ghp_user"},  # pragma: allowlist secret
            github_repo_available=False,
            job_chat=True,
        )
        assert unavailable["github"] == "no_run_repository"
        # The prompt names this run, never the server setting or a token.
        prompt = _issue_create_rules(unavailable, issue_repos=())
        assert "this run has no GitHub test repository" in prompt
        assert "TESTS_REPO_URL" not in prompt

    def test_github_flag_off_offers_nothing(self):
        assert (
            self._targets(
                self._settings(enable_github_issues=False),
                github_token="ghp_user",  # pragma: allowlist secret
            )
            == ()
        )

    def test_jira_only_user_gets_only_jira(self):
        assert self._targets(self._settings(), jira_token="jira-user") == ("jira",)
        assert self._targets(self._settings(), jira_sa_token="ATOA-user") == ("jira",)

    def test_a_jira_credential_the_settings_cannot_serve_is_not_offered(self):
        """A legacy SA secret is refused by the create call, so it is not a tool."""
        from rootcoz.main import _chat_tracker_state

        targets, unavailable = _chat_tracker_state(
            self._settings(),
            {"jira_sa_token": "legacy-secret"},
            github_repo_available=True,
        )
        assert targets == ()
        assert unavailable["jira"] == "unusable_credential"

        # The same order of checks as the endpoint: with no method configured
        # there is nothing to file through either.
        no_method = self._settings(jira_server_url="")
        assert (
            _chat_tracker_state(
                no_method, {"jira_token": "pat"}, github_repo_available=True
            )[1]["jira"]
            == "unusable_credential"
        )

    def test_jira_flag_off_offers_nothing(self):
        assert (
            self._targets(self._settings(enable_jira_issues=False), jira_token="jira")
            == ()
        )

    def test_jira_only_target_builds_only_the_jira_tool(self):
        names = [
            t["name"]
            for t in build_chat_custom_tools(
                server_url="http://localhost:8000",
                auth_token="tok",
                job_id="j1",
                tracker_targets=self._targets(self._settings(), jira_token="jira-user"),
            )
        ]
        assert "create_jira_issue" in names
        assert "create_github_issue" not in names


class TestChatIssueTools:
    """The chat model proposes an issue, then files the draft the user approved."""

    def test_issue_tools_in_job_and_admin_chat(self):
        job_tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            tracker_targets=("github", "jira"),
            issue_repos=("org/tests", "org/lib"),
        )
        admin_tools = build_admin_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            tracker_targets=("github", "jira"),
            conversation="__admin_chat__",
        )
        for tools, in_job_chat in ((job_tools, True), (admin_tools, False)):
            by_name = {t["name"]: t for t in tools}
            propose = by_name["propose_tracker_issue"]
            assert propose["http"]["method"] == "POST"
            assert (
                propose["http"]["url"] == "http://localhost:8000/api/chat-issue/propose"
            )
            assert propose["http"]["headers"]["Authorization"] == "Bearer tok"
            assert propose["parameters"]["required"] == ["target", "title", "body"]
            assert propose["parameters"]["properties"]["target"]["enum"] == [
                "github",
                "jira",
            ]
            for target in ("github", "jira"):
                tool = by_name[f"create_{target}_issue"]
                assert tool["http"]["method"] == "POST"
                assert tool["http"]["url"] == "http://localhost:8000/api/chat-issue"
                assert tool["http"]["headers"]["Authorization"] == "Bearer tok"
                expected_body = {
                    "target": target,
                    "title": "{title}",
                    "body": "{body}",
                    "confirmation_id": "{confirmation_id}",
                    "confirmation_code": "{confirmation_code}",
                }
                if target == "github":
                    # Only GitHub takes a repo, and a job chat names its run.
                    expected_body["repo"] = "{repo}"
                    if in_job_chat:
                        expected_body["job_id"] = "j1"
                assert tool["http"]["body_template"] == expected_body
                assert tool["parameters"]["required"] == [
                    "title",
                    "body",
                    "confirmation_id",
                    "confirmation_code",
                ]
                assert (
                    "replied in this chat with the confirmation code"
                    in (tool["description"])
                )
                assert (
                    "without a valid confirmation_id fails" not in (tool["description"])
                )
                # The Jira project is never the model's to pick.
                if target == "jira":
                    assert "do not choose the Jira project" in tool["description"]
            # The draft is proposed for the same run it is later created in, and
            # the conversation the user has to reply in is injected server-side
            # (the model cannot set it, so it cannot propose a draft that skips
            # the user's reply).
            proposed = dict(propose["http"]["body_template"])
            assert proposed["conversation"] == (
                "j1" if in_job_chat else "__admin_chat__"
            )
            if in_job_chat:
                assert proposed["job_id"] == "j1"
                assert proposed["repo"] == "{repo}"
            else:
                assert "job_id" not in proposed
            assert "confirmation_code" in propose["description"]
            assert "reply in this chat with the code" in propose["description"]

    def test_no_issue_tools_at_all_without_a_usable_tracker(self):
        names = [
            tool["name"]
            for tool in build_chat_custom_tools(
                server_url="http://localhost:8000", auth_token="tok", job_id="j1"
            )
        ]
        assert "propose_tracker_issue" not in names
        assert not [n for n in names if n.startswith("create_")]

    async def test_created_issue_is_the_tool_result(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        """A 201 body with the issue URL is what the tool hands the model."""
        _, api_key = await storage.create_user("chat-issue-ok")
        await storage.save_user_tokens(
            "chat-issue-ok",
            github_token="ghp_row_token",  # pragma: allowlist secret
        )
        # A non-admin GitHub write needs the run it belongs to (see #407).
        await _save_job(
            temp_db_path,
            "chat-issue-run",
            {
                "request_params": {
                    "job_name": "chat-issue-run",
                    "build_number": 1,
                    "tests_repo_url": "https://github.com/org/repo",
                }
            },
        )
        headers = {"Authorization": f"Bearer {api_key}"}
        proposal = test_client.post(
            "/api/chat-issue/propose",
            json={
                "target": "github",
                "title": "test_login times out",
                "body": "DNS timeout resolving auth.",
                # The run the draft is for: the same one must be created in.
                "job_id": "chat-issue-run",
                # The chat the user has to reply in (the tool injects it).
                "conversation": "chat-issue-run",
            },
            headers=headers,
        )
        assert proposal.status_code == 201, proposal.text
        code = proposal.json()["confirmation_code"]
        # The user's own reply with the code is the confirmation.
        await storage.add_chat_message(
            "chat-issue-run",
            role="user",
            content=f"Yes, file it: {code}",
            username="chat-issue-ok",
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/7",
                "number": 7,
            }
            response = test_client.post(
                "/api/chat-issue",
                json={
                    "target": "github",
                    "title": proposal.json()["title"],
                    "body": proposal.json()["body"],
                    "confirmation_id": proposal.json()["confirmation_id"],
                    "confirmation_code": code,
                    "job_id": "chat-issue-run",
                },
                headers=headers,
            )
        assert response.status_code == 201, response.text
        # Unwrapped: this JSON body is exactly what the sidecar returns to the model.
        assert response.json()["url"] == "https://github.com/org/repo/issues/7"
        assert response.json()["number"] == 7

    async def test_refusal_body_reaches_the_model_unchanged(
        self, test_client, github_issue_config
    ):
        """No saved token → the followable 400 detail the model must relay verbatim."""
        _, api_key = await storage.create_user("chat-issue-none")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            response = test_client.post(
                "/api/chat-issue",
                json={"target": "github", "title": "t", "body": "b"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "GitHub token is required. Set up your token in Profile Settings."
        )
        mock_create.assert_not_called()

    def test_prompts_require_explicit_request_and_relay(self):
        job_prompt = build_system_prompt(
            job_name="job",
            build_number=1,
            job_id="j1",
            custom_tools=build_chat_custom_tools(
                server_url="http://localhost:8000",
                auth_token="tok",
                job_id="j1",
                tracker_targets=("github", "jira"),
            ),
            tracker_unavailable={"github": "no_token", "jira": "no_token"},
        )
        admin_prompt = build_admin_system_prompt(
            build_admin_custom_tools(
                server_url="http://localhost:8000",
                auth_token="tok",
                tracker_targets=("github", "jira"),
            ),
            tracker_unavailable={"github": "no_token", "jira": "no_token"},
        )
        for prompt in (job_prompt, admin_prompt):
            assert "propose_tracker_issue" in prompt
            assert "create_github_issue" in prompt
            assert "create_jira_issue" in prompt
            # The user files it: the code has to arrive in their own reply.
            assert "the user files it" in prompt
            assert "reply in this chat with the code" in prompt
            assert "Never send the code on the user's behalf" in prompt
            assert "a create the user has not confirmed fails" in prompt
            assert "relay it verbatim" in prompt
            assert "Profile Settings" in prompt
            assert "Never claim an issue exists" in prompt

    def test_prompt_tells_the_model_what_to_do_without_the_tool(self):
        """A tracker the user never set up is absent — and the prompt says so."""
        prompt = build_admin_system_prompt(
            build_admin_custom_tools(
                server_url="http://localhost:8000", auth_token="t"
            ),
            tracker_unavailable={"github": "no_token", "jira": "no_token"},
        )
        assert "create_github_issue" not in prompt.split("## Creating Issues")[0]
        assert "no tool in your tool list cannot be used" in prompt
        assert "Say you cannot open that issue" in prompt
        assert "Profile Settings → add the GitHub token" in prompt
        assert "Profile Settings → add the Jira token" in prompt

    def test_prompt_does_not_send_a_server_disabled_tracker_to_profile(self):
        """Adding a token cannot turn a tracker on, so the prompt stops saying so."""
        prompt = build_admin_system_prompt(
            build_admin_custom_tools(
                server_url="http://localhost:8000", auth_token="t"
            ),
            tracker_unavailable={
                "github": "no_repository",
                "jira": "server_disabled",
            },
        )
        rules = prompt.split("## Creating Issues")[1]
        assert "TESTS_REPO_URL" in rules
        assert "issue creation is disabled on this server" in rules
        # The token advice belongs only to the missing-token case.
        assert "Profile Settings → add" not in rules

    def test_an_account_with_no_profile_is_not_sent_to_profile_settings(self):
        """The bootstrap ``admin`` has no ``users`` row: ``PUT /api/user/tokens``
        answers 404 for it, so the chat prompt must not name it as the fix.

        Row D2: the API half already said "this account has no profile to store
        one"; the chat half still said "Profile Settings → add the GitHub
        token", a page the caller cannot use.
        """
        from rootcoz.main import _chat_tracker_state

        tools = build_admin_custom_tools(
            server_url="http://localhost:8000", auth_token="t"
        )
        rules = build_admin_system_prompt(
            tools, tracker_unavailable={"github": "no_token_no_profile"}
        ).split("## Creating Issues")[1]
        # The reason the API gives, and what this account can actually do.
        assert "no profile to store one" in rules
        assert "Sign in with your own account" in rules
        # Not this account's fix: no dead-end link.
        assert "Profile Settings → add" not in rules

        # The chat session reaches it: a caller with no users row is classified
        # as such, not as a user that simply never saved a token.
        settings = TestChatTrackerTargets._settings()
        _targets, no_profile = _chat_tracker_state(
            settings, {}, github_repo_available=True, has_profile=False
        )
        assert no_profile["github"] == "no_token_no_profile"

        # A registered account keeps the wording it had.
        _targets, registered = _chat_tracker_state(
            settings, {}, github_repo_available=True
        )
        assert registered["github"] == "no_token"
        user_rules = build_admin_system_prompt(
            tools, tracker_unavailable=registered
        ).split("## Creating Issues")[1]
        assert "Profile Settings → add the GitHub token" in user_rules
        assert "no profile to store one" not in user_rules

    def test_prompt_names_an_unusable_credential_as_a_credential_problem(self):
        """A saved-but-unusable Jira credential is not "no token saved"."""
        prompt = build_admin_system_prompt(
            build_admin_custom_tools(
                server_url="http://localhost:8000", auth_token="t"
            ),
            tracker_unavailable={"jira": "unusable_credential"},
        )
        rules = prompt.split("## Creating Issues")[1]
        assert "cannot be used with the server's Jira settings" in rules
        assert "re-save the Jira credential" in rules
        assert "Profile Settings → add" not in rules

    async def test_the_admin_chat_session_prompt_names_the_real_reason(self):
        """The FIRST turn carries the reason too, not just later messages.

        ``init_admin_chat_session`` built its prompt without the unavailable
        reasons, so an admin whose tracker is disabled read the placeholder
        ("every tracker this chat can file in is in your tool list") and was
        sent looking for a token that cannot help.
        """
        from rootcoz.engine import chat as chat_mod

        with patch.object(
            chat_mod, "_create_chat_session", AsyncMock(return_value="sid")
        ) as create:
            await chat_mod.init_admin_chat_session(
                ai_provider="p",
                ai_model="m",
                tracker_unavailable={"github": "server_disabled"},
            )
        prompt = create.call_args.kwargs["system_prompt"]
        assert "issue creation is disabled on this server" in prompt
        assert "every tracker this chat can file in is in your tool list" not in prompt


class TestBuildSystemPrompt:
    """Tests for build_system_prompt with custom_tools."""

    def _make_tools(self, *, jira: bool = False, github: bool = False) -> list[dict]:
        """Helper to build custom tools with optional integrations."""
        return build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_server_url="https://jira.example.com" if jira else "",
            jira_token="jira-tok" if jira else "",
            github_token="gh-tok" if github else "",
            github_repo="org/repo" if github else "",
        )

    def test_basic_prompt_structure(self):
        tools = self._make_tools()
        prompt = build_system_prompt(
            job_name="test-job",
            build_number=42,
            job_id="job-123",
            custom_tools=tools,
        )
        assert "test-job" in prompt
        assert "#42" in prompt
        assert "job-123" in prompt
        assert "get_job_result" in prompt
        assert "read-only" in prompt.lower()

    def test_all_tools_listed(self):
        tools = self._make_tools(jira=True, github=True)
        prompt = build_system_prompt(
            job_name="test-job",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
        )
        assert "get_job_result" in prompt
        assert "get_job_comments" in prompt
        assert "search_jira" in prompt
        assert "search_github_issues" in prompt

    def test_no_tools(self):
        prompt = build_system_prompt(
            job_name="clean",
            build_number=1,
            job_id="job-789",
            custom_tools=[],
        )
        assert "clean" in prompt
        assert "#1" in prompt
        assert "Available Tools" in prompt

    def test_repos_available_note(self):
        tools = self._make_tools()
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
            repos_available=True,
        )
        assert "Source repositories are cloned" in prompt

    def test_repos_not_available(self):
        tools = self._make_tools()
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
            repos_available=False,
        )
        assert "Source repositories are cloned" not in prompt

    def test_ci_build_data_available_note(self):
        """CI-neutral wording when build data is available for chat."""
        tools = self._make_tools()
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
            ci_build_data_available=True,
        )
        assert "CI build data is available in your working directory:" in prompt
        assert "full CI console output" in prompt
        assert "console-output.txt" in prompt
        assert "build-info.json" in prompt
        assert "build-artifacts/" in prompt
        assert "Jenkins build data" not in prompt
        assert "Jenkins console" not in prompt

    def test_ci_build_data_not_available(self):
        tools = self._make_tools()
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
            ci_build_data_available=False,
        )
        assert "console-output.txt" not in prompt
        assert "build-info.json" not in prompt
        assert "build-artifacts/" not in prompt

    def test_only_base_tools(self):
        tools = self._make_tools()
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
        )
        assert "get_job_result" in prompt
        assert "search_jira" not in prompt
        assert "search_github_issues" not in prompt

    def test_unavailable_jira_notice(self):
        """When jira is not configured, system prompt includes unavailable notice."""
        tools = self._make_tools(github=True)
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
        )
        assert "Unavailable Tools" in prompt
        assert "Jira search" in prompt
        assert "User Settings" in prompt
        assert "GitHub search" not in prompt  # GitHub IS configured

    def test_unavailable_github_notice(self):
        """When github is not configured, system prompt includes unavailable notice."""
        tools = self._make_tools(jira=True)
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
        )
        assert "Unavailable Tools" in prompt
        assert "GitHub search" in prompt
        assert "User Settings" in prompt
        assert "Jira search" not in prompt  # Jira IS configured

    def test_both_unavailable_notices(self):
        """When neither jira nor github configured, both notices appear."""
        tools = self._make_tools()
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
        )
        assert "Unavailable Tools" in prompt
        assert "Jira search" in prompt
        assert "GitHub search" in prompt

    def test_no_unavailable_when_all_configured(self):
        """When both are configured, no unavailable section."""
        tools = self._make_tools(jira=True, github=True)
        prompt = build_system_prompt(
            job_name="j",
            build_number=1,
            job_id="j1",
            custom_tools=tools,
        )
        assert "Unavailable Tools" not in prompt


# ---------------------------------------------------------------------------
# build_welcome_message tests
# ---------------------------------------------------------------------------


class TestBuildWelcomeMessage:
    """Tests for build_welcome_message."""

    def test_basic_message(self):
        msg = build_welcome_message(job_name="test-job", build_number=42)
        assert "test-job" in msg
        assert "#42" in msg
        assert "Job analysis results" in msg
        assert "Job comments" in msg
        assert "Failure history" in msg
        assert "Classification history" in msg
        # Not available by default
        assert "Jira" not in msg
        assert "GitHub" not in msg
        assert "repository" not in msg.lower()

    def test_repos_available(self):
        msg = build_welcome_message(job_name="j", build_number=1, repos_available=True)
        assert "repository" in msg.lower()

    def test_jenkins_data(self):
        msg = build_welcome_message(
            job_name="j",
            build_number=1,
            ci_build_data_available=True,
        )
        assert "Build artifacts" in msg
        assert "console output" in msg.lower()
        assert "build metadata" in msg.lower()

    def test_jira_available(self):
        msg = build_welcome_message(job_name="j", build_number=1, jira_available=True)
        assert "Jira" in msg

    def test_github_available(self):
        msg = build_welcome_message(job_name="j", build_number=1, github_available=True)
        assert "GitHub" in msg

    def test_all_resources(self):
        msg = build_welcome_message(
            job_name="pipeline",
            build_number=99,
            repos_available=True,
            ci_build_data_available=True,
            jira_available=True,
            github_available=True,
        )
        assert "pipeline" in msg
        assert "#99" in msg
        assert "repository" in msg.lower()
        assert "Build artifacts" in msg
        assert "Jira" in msg
        assert "GitHub" in msg
        assert "console output" in msg.lower()

    def test_string_build_number(self):
        """build_welcome_message works with string build_number (large Prow build_id)."""
        msg = build_welcome_message(
            job_name="prow-job",
            build_number="9007199254740999",
        )
        assert "prow-job" in msg
        assert "#9007199254740999" in msg


# ---------------------------------------------------------------------------
# build_chat_prompt tests
# ---------------------------------------------------------------------------


class TestBuildChatPrompt:
    """Tests for build_chat_prompt."""

    def test_empty_history(self):
        prompt = build_chat_prompt("SYSTEM", [], "hello")
        assert "SYSTEM" in prompt
        assert "**User:** hello" in prompt
        assert "**Assistant:**" in prompt

    def test_with_history(self):
        history = [
            {"role": "user", "content": "what failed?"},
            {"role": "assistant", "content": "test_foo failed"},
        ]
        prompt = build_chat_prompt("SYS", history, "why?")
        assert "**User:** what failed?" in prompt
        assert "**Assistant:** test_foo failed" in prompt
        assert "**User:** why?" in prompt

    def test_history_order_preserved(self):
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"},
        ]
        prompt = build_chat_prompt("SYS", history, "fifth")
        assert prompt.index("first") < prompt.index("second")
        assert prompt.index("second") < prompt.index("third")
        assert prompt.index("third") < prompt.index("fourth")
        assert prompt.index("fourth") < prompt.index("fifth")


# ---------------------------------------------------------------------------
# _extract_build_params tests
# ---------------------------------------------------------------------------


class TestExtractBuildParams:
    """Tests for _extract_build_params sensitive filtering."""

    def test_extracts_normal_params(self):
        build_info = {
            "actions": [
                {
                    "_class": "hudson.model.ParametersAction",
                    "parameters": [
                        {"name": "BRANCH", "value": "main"},
                        {"name": "ENV", "value": "staging"},
                    ],
                }
            ]
        }
        params = _extract_build_params(build_info)
        assert params == [
            {"name": "BRANCH", "value": "main"},
            {"name": "ENV", "value": "staging"},
        ]

    def test_filters_sensitive_params(self):
        build_info = {
            "actions": [
                {
                    "_class": "hudson.model.ParametersAction",
                    "parameters": [
                        {"name": "BRANCH", "value": "main"},
                        {"name": "API_TOKEN", "value": "secret123"},
                        {"name": "DB_PASSWORD", "value": "pass456"},
                        {"name": "SECRET_KEY", "value": "key789"},
                        {"name": "AWS_CREDENTIAL", "value": "cred"},
                        {"name": "AUTH_HEADER", "value": "bearer xyz"},
                    ],
                }
            ]
        }
        params = _extract_build_params(build_info)
        assert params == [{"name": "BRANCH", "value": "main"}]

    def test_no_actions(self):
        assert _extract_build_params({}) == []
        assert _extract_build_params({"actions": []}) == []


# ---------------------------------------------------------------------------
# Jenkins chat workspace population (via CISource plugin path)
# ---------------------------------------------------------------------------


def _jenkins_chat_settings(**overrides):
    """Build a Settings-like object with model_copy for Jenkins chat tests."""
    from types import SimpleNamespace

    base = {
        "jenkins_url": "https://jenkins.example.com",
        "jenkins_user": "user",
        "jenkins_password": "pass",  # pragma: allowlist secret
        "jenkins_ssl_verify": True,
        "jenkins_timeout": 30,
        "jenkins_artifacts_max_size_mb": 500,
        "get_job_artifacts": False,
    }
    base.update(overrides)

    def _make(data: dict):
        ns = SimpleNamespace(**data)

        def _copy(update):
            merged = {**data, **update}
            return _make(merged)

        ns.model_copy = _copy
        return ns

    return _make(base)


class TestSetupJenkinsWorkspace:
    """Tests for Jenkins chat workspace via setup_ci_build_workspace."""

    @pytest.mark.asyncio
    async def test_skips_without_jenkins_settings(self, tmp_path):
        from rootcoz.sources.chat_workspace import setup_ci_build_workspace

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        params = {
            "analysis_type": "jenkins",
            "job_name": "test-pipeline",
            "build_number": 42,
        }
        result = await setup_ci_build_workspace(workspace, params, settings=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_without_job_name(self, tmp_path):
        from rootcoz.sources.chat_workspace import setup_ci_build_workspace

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        params = {
            "analysis_type": "jenkins",
            "jenkins_url": "https://jenkins.example.com",
            "jenkins_user": "user",
            "jenkins_password": "pass",  # pragma: allowlist secret
        }
        result = await setup_ci_build_workspace(
            workspace, params, settings=_jenkins_chat_settings()
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_non_jenkins_job(self, tmp_path):
        from rootcoz.sources.chat_workspace import setup_ci_build_workspace

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = await setup_ci_build_workspace(workspace, {})
        assert result is False

    @pytest.mark.asyncio
    async def test_writes_console_and_build_info(self, tmp_path):
        from rootcoz.sources.chat_workspace import setup_ci_build_workspace

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        params = {
            "analysis_type": "jenkins",
            "jenkins_url": "https://jenkins.example.com",
            "jenkins_user": "user",
            "jenkins_password": "pass",  # pragma: allowlist secret
            "job_name": "test-pipeline",
            "build_number": 42,
        }
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as MockClient:
            mock = MockClient.return_value
            mock.get_build_console.return_value = "line1\nERROR: boom\nline3"
            mock.get_build_info_safe.return_value = {
                "result": "FAILURE",
                "building": False,
                "duration": 120000,
                "estimatedDuration": 100000,
                "timestamp": 1700000000000,
                "url": "https://jenkins.example.com/job/test/42/",
                "displayName": "#42",
                "description": "",
                "actions": [
                    {
                        "_class": "hudson.model.ParametersAction",
                        "parameters": [
                            {"name": "BRANCH", "value": "main"},
                            {"name": "API_TOKEN", "value": "secret123"},
                            {"name": "DB_PASSWORD", "value": "pass456"},
                        ],
                    }
                ],
                "artifacts": [],
            }
            result = await setup_ci_build_workspace(
                workspace, params, settings=_jenkins_chat_settings()
            )

        assert result is True
        assert (workspace / "console-output.txt").exists()
        assert (workspace / "build-info.json").exists()
        import json

        info = json.loads((workspace / "build-info.json").read_text())
        assert info["result"] == "FAILURE"
        assert info["parameters"] == [{"name": "BRANCH", "value": "main"}]

    @pytest.mark.asyncio
    async def test_skips_existing_files(self, tmp_path):
        from rootcoz.sources.chat_workspace import setup_ci_build_workspace

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "console-output.txt").write_text("existing")
        (workspace / "build-info.json").write_text("{}")
        params = {
            "analysis_type": "jenkins",
            "jenkins_url": "https://jenkins.example.com",
            "jenkins_user": "user",
            "jenkins_password": "pass",  # pragma: allowlist secret
            "job_name": "test-pipeline",
            "build_number": 42,
        }
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as MockClient:
            mock = MockClient.return_value
            await setup_ci_build_workspace(
                workspace, params, settings=_jenkins_chat_settings()
            )
            mock.get_build_console.assert_not_called()
            mock.get_build_info_safe.assert_not_called()
        assert (workspace / "console-output.txt").read_text() == "existing"


# ---------------------------------------------------------------------------
# cleanup tests
# ---------------------------------------------------------------------------


class TestChatCleanup:
    """Tests for cleanup_chat_repos symlink handling."""

    def test_cleanup_repos_resolves_symlinks(self, tmp_path):
        from rootcoz.engine.chat import cleanup_chat_repos

        # Create a fake workspace with a symlink
        job_id = "test-cleanup-job"
        with patch("rootcoz.engine.chat.get_chat_workspace", return_value=tmp_path):
            # Create a target dir and a symlink
            target = tmp_path / "_artifact_target"
            target.mkdir()
            (target / "somefile.txt").write_text("data")
            link = tmp_path / "build-artifacts"
            link.symlink_to(target)

            cleanup_chat_repos(job_id)

            # Both the symlink and its target should be cleaned
            assert not link.exists()
            assert not target.exists()

    def test_cleanup_repos_removes_graph_when_workspace_missing(
        self, tmp_path, monkeypatch
    ):
        from rootcoz.engine.chat import cleanup_chat_repos
        from rootcoz.engine.graft import _root
        from rootcoz.engine.graft_http import _sessions, register_workspace

        workspace = tmp_path / "missing"
        repo = workspace / "repo"
        repo.mkdir(parents=True)
        register_workspace(workspace, {"repo": repo}, job_id="job")
        # The registry is keyed by (workspace, job) so concurrent sessions on
        # one workspace keep their own credentials.
        assert any(key[0] == workspace.resolve() for key in _sessions)
        graph = _root(workspace)
        graph.mkdir()
        (graph / "snapshot").write_text("source")
        import shutil

        shutil.rmtree(workspace)
        monkeypatch.setattr(
            "rootcoz.engine.chat.get_chat_workspace", lambda *args, **kwargs: workspace
        )
        cleanup_chat_repos("job")
        assert workspace.resolve() not in _sessions
        assert not graph.exists()

    def test_cleanup_workspace_resolves_symlinks(self, tmp_path):
        from rootcoz.engine.chat import cleanup_chat_workspace

        # Create target dir outside workspace
        external_target = tmp_path / "external_artifacts"
        external_target.mkdir()
        (external_target / "log.txt").write_text("data")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        link = workspace / "build-artifacts"
        link.symlink_to(external_target)

        with patch("rootcoz.engine.chat.get_chat_workspace", return_value=workspace):
            cleanup_chat_workspace("test-job")

        assert not workspace.exists()
        assert not external_target.exists()

    def test_safe_remove_symlink_validates_tmp(self, tmp_path):
        """_safe_remove_symlink only deletes targets under /tmp/."""
        from rootcoz.engine.chat import _safe_remove_symlink

        # Target under /tmp/ — should be deleted
        target = tmp_path / "artifact_dir"
        target.mkdir()
        (target / "file.txt").write_text("data")
        link = tmp_path / "link"
        link.symlink_to(target)

        _safe_remove_symlink(link)
        assert not link.exists()
        assert not target.exists()  # under /tmp/ → deleted

    def test_cleanup_deletes_jenkins_artifacts_when_tmpdir_differs(
        self, tmp_path, monkeypatch
    ):
        """Jenkins extract dirs under gettempdir() are deleted even if TMPDIR != /tmp."""
        import tempfile

        from rootcoz import jenkins_artifacts
        from rootcoz.engine.chat import cleanup_chat_workspace

        custom_tmp = tmp_path / "custom-tmp"
        custom_tmp.mkdir()
        monkeypatch.setenv("TMPDIR", str(custom_tmp))
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(custom_tmp))

        extract_base = Path(tempfile.gettempdir()) / "rootcoz"
        monkeypatch.setattr(jenkins_artifacts, "EXTRACT_BASE", extract_base)

        artifacts_dir = extract_base / "artifacts-testhash"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "log.txt").write_text("data")

        workspace = Path(tempfile.gettempdir()) / "rootcoz-chat-job-tmpdir"
        workspace.mkdir(parents=True)
        link = workspace / "build-artifacts"
        link.symlink_to(artifacts_dir)

        with patch("rootcoz.engine.chat.get_chat_workspace", return_value=workspace):
            cleanup_chat_workspace("job-tmpdir")

        assert not workspace.exists()
        assert not artifacts_dir.exists()

    def test_cleanup_workspace_removes_nested_user_mcp_dumps(self, tmp_path):
        from rootcoz.engine.chat import cleanup_chat_workspace
        from rootcoz.engine.http_mcp import http_tools_dump_path

        workspace = tmp_path / "rootcoz-chat-job"
        user_ws = workspace / "alice"
        user_ws.mkdir(parents=True)
        dump = http_tools_dump_path(user_ws)
        dump.write_text("[]")
        (user_ws / "session.txt").write_text("x")

        with patch("rootcoz.engine.chat.get_chat_workspace", return_value=workspace):
            cleanup_chat_workspace("job")

        assert not workspace.exists()
        assert not dump.exists()

    def test_cleanup_workspace_waits_for_mcp_install_lock(self, tmp_path):
        import threading
        import time

        from rootcoz.engine import http_mcp as http_mcp_mod
        from rootcoz.engine.chat import cleanup_chat_workspace

        workspace = tmp_path / "ws"
        workspace.mkdir()
        dump = http_mcp_mod.http_tools_dump_path(workspace)
        order: list[str] = []
        started = threading.Event()
        release = threading.Event()

        def hold_lock():
            with http_mcp_mod._workspace_install_lock(workspace):
                order.append("install")
                started.set()
                release.wait(timeout=5)
                dump.write_text("secret")

        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert started.wait(timeout=5)

        def run_cleanup():
            order.append("cleanup-start")
            with patch(
                "rootcoz.engine.chat.get_chat_workspace", return_value=workspace
            ):
                cleanup_chat_workspace("locked-job")
            order.append("cleanup-end")

        cleaner = threading.Thread(target=run_cleanup)
        cleaner.start()
        time.sleep(0.1)
        assert "cleanup-end" not in order
        release.set()
        holder.join(timeout=5)
        cleaner.join(timeout=5)
        assert order[:2] == ["install", "cleanup-start"]
        assert order[-1] == "cleanup-end"
        assert not workspace.exists()
        assert not dump.exists()


@pytest.mark.asyncio
async def test_cleanup_deleted_job_offloads_blocking_cleanup(monkeypatch):
    """Filesystem MCP locks must not run on the event loop."""
    from rootcoz import main as main_mod

    seen: list[str] = []

    def fake_cleanup(job_id: str, username: str = "") -> None:
        seen.append(f"cleanup:{job_id}")

    monkeypatch.setattr("rootcoz.engine.chat.cleanup_chat_workspace", fake_cleanup)
    orig_to_thread = main_mod.asyncio.to_thread

    async def tracking_to_thread(fn, *args, **kwargs):
        seen.append("to_thread")
        return await orig_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(main_mod.asyncio, "to_thread", tracking_to_thread)
    main_mod._chat_jobs_deleting.clear()
    await main_mod._cleanup_deleted_job_chat_workspaces("job-offload")
    assert seen == ["to_thread", "cleanup:job-offload"]
    assert "job-offload" not in main_mod._chat_jobs_deleting
    assert "job-offload" not in main_mod._chat_job_barriers


@pytest.mark.asyncio
async def test_index_chat_repositories_discovers_and_logs_off_loop(
    tmp_path, monkeypatch
):
    from rootcoz import main as main_mod
    from rootcoz.engine import graft, graft_http

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seen = []

    def index(workspace, roots):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        seen.append((workspace, roots))
        return {"repo": {"status": "ready"}}

    original_discover = graft_http.cloned_graph_roots

    def discover(workspace):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return original_discover(workspace)

    monkeypatch.setattr(graft_http, "cloned_graph_roots", discover)
    monkeypatch.setattr(graft, "index_repositories", index)
    monkeypatch.setattr(
        graft, "log_index_outcomes", lambda outcomes, **_: seen.append(outcomes)
    )
    await main_mod._index_chat_repositories(tmp_path)
    assert seen == [
        (tmp_path, original_discover(tmp_path)),
        {"repo": {"status": "ready"}},
    ]


@pytest.mark.asyncio
async def test_init_chat_under_barrier_rejects_deleting_job():
    from fastapi import HTTPException

    from rootcoz import main as main_mod

    main_mod._chat_jobs_deleting.add("gone-job")
    try:
        with pytest.raises(HTTPException) as exc:
            await main_mod._init_chat_under_barrier(
                "gone-job",
                "alice",
                ChatInitRequest(ai_provider="claude", ai_model="sonnet-4"),
            )
        assert exc.value.status_code == 404
    finally:
        main_mod._chat_jobs_deleting.discard("gone-job")


@pytest.mark.asyncio
async def test_cleanup_deleted_job_does_not_block_event_loop(tmp_path, monkeypatch):
    """A contended MCP flock during cleanup must not stall asyncio."""
    import threading
    import time

    from rootcoz import main as main_mod
    from rootcoz.engine import http_mcp as http_mcp_mod

    workspace = tmp_path / "ws"
    workspace.mkdir()
    started = threading.Event()
    release = threading.Event()

    def hold_lock():
        with http_mcp_mod._workspace_install_lock(workspace):
            started.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert started.wait(timeout=5)

    monkeypatch.setattr(
        "rootcoz.engine.chat.get_chat_workspace", lambda job_id, username="": workspace
    )
    main_mod._chat_jobs_deleting.clear()
    cleanup_task = asyncio.create_task(
        main_mod._cleanup_deleted_job_chat_workspaces("job-block")
    )
    t0 = time.monotonic()
    await asyncio.sleep(0.05)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2
    assert not cleanup_task.done()
    release.set()
    holder.join(timeout=5)
    await cleanup_task
    assert "job-block" not in main_mod._chat_job_barriers


@pytest.mark.asyncio
async def test_process_chat_cancel_while_waiting_barrier_marks_failed(
    setup_test_db, monkeypatch
):
    """Cancellation during the lifecycle wait must not leave the placeholder pending."""
    from rootcoz import main as main_mod

    job_id = "chat-cancel-job"
    await storage.save_result(
        job_id, "", "completed", {"status": "completed", "summary": "t", "failures": []}
    )
    _user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="claude", ai_model="sonnet-4"
    )
    barrier = main_mod._get_chat_job_barrier(job_id)
    await barrier.acquire()
    task = asyncio.create_task(
        main_mod._process_chat_message(
            job_id=job_id,
            user_msg_id=_user_id,
            assistant_msg_id=assistant_id,
            message="hello",
            ai_provider_override="claude",
            ai_model_override="sonnet-4",
            username="alice",
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    barrier.release()
    assert await storage.get_chat_message_status(assistant_id) == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "graph_state, expected_indexes",
    [("indexed", 0), ("changed", 1), ("new", 1), ("missing", 1)],
)
async def test_followup_indexes_only_new_or_missing_graph(
    setup_test_db, tmp_path, monkeypatch, graph_state, expected_indexes
):
    """An existing graph must not delay AI, but new clones and lost graphs need indexing."""
    import subprocess

    from rootcoz import main as main_mod
    from rootcoz.engine import graft, graft_http
    from rootcoz.engine.chat import clone_chat_repos

    discover = graft_http.cloned_graph_roots

    def off_loop_discover(workspace):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return discover(workspace)

    monkeypatch.setattr(graft_http, "cloned_graph_roots", off_loop_discover)
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    repo = workspace / "repo"
    repo.mkdir()
    await asyncio.to_thread(
        subprocess.run, ["git", "init", "-q", str(repo)], check=True
    )
    (repo / "a.py").write_text("old")
    builds = []

    def fake_run(argv, **kwargs):
        builds.append(argv)
        (Path(argv[-1]).parent / "graph").mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", fake_run)
    assert (
        graft.index_repositories(workspace, {"repo": repo})["repo"]["status"]
        == "indexed"
    )
    assert len(builds) == 1
    scans = []
    original_sources = graft._sources

    def tracked_sources(path, *args, **kwargs):
        scans.append(path)
        return original_sources(path, *args, **kwargs)

    monkeypatch.setattr(graft, "_sources", tracked_sources)

    class Manager:
        def clone_into(self, url, target, **kwargs):
            target.mkdir()
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            (target / "a.py").write_text("other")

    monkeypatch.setattr("rootcoz.repository.RepositoryManager", Manager)
    params = {"additional_repos": [{"name": "repo", "url": "https://example.com/repo"}]}
    assert await clone_chat_repos(workspace, params) is False
    if graph_state == "new":
        params["additional_repos"].append(
            {"name": "other", "url": "https://example.com/other"}
        )
    elif graph_state == "changed":
        (repo / "a.py").write_text("new")
    elif graph_state == "missing":
        (graft._root(workspace) / "repo" / "manifest.json").unlink()

    # The single builder always asks for indexing and decides inside whether a
    # build is needed, so the observable is the build itself, not the call.

    monkeypatch.setattr(
        "rootcoz.engine.chat.ensure_chat_workspace", lambda *a, **k: workspace
    )
    monkeypatch.setattr(
        "rootcoz.engine.core.copy_rootcoz_pi_resources", lambda *a: None
    )
    monkeypatch.setattr(
        "rootcoz.engine.chat.chat_with_ai", AsyncMock(return_value=(True, "ok", "sess"))
    )
    monkeypatch.setattr(
        "rootcoz.engine.session_tools.install_http_tools_mcp_best_effort_async",
        AsyncMock(),
    )
    monkeypatch.setattr("rootcoz.engine.graft_http.graph_http_tools", lambda *a: [])
    monkeypatch.setattr(
        "rootcoz.sources.chat_workspace.setup_ci_build_workspace",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        main_mod,
        "_resolve_chat_credentials",
        AsyncMock(return_value=("", "", None, "", "")),
    )
    monkeypatch.setattr(main_mod, "_create_ai_auth_header", AsyncMock(return_value=""))
    job_id = f"chat-index-{graph_state}"
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    await storage.save_result(
        job_id,
        "",
        "completed",
        {
            "status": "completed",
            "summary": "t",
            "failures": [],
            "ai_provider": "claude",
            "ai_model": "sonnet-4",
            "request_params": params,
        },
    )
    await _seed_chat_session(job_id)
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="claude", ai_model="sonnet-4"
    )
    await main_mod._process_chat_message(
        job_id=job_id,
        user_msg_id=user_id,
        assistant_msg_id=assistant_id,
        message="hello",
        ai_provider_override="claude",
        ai_model_override="sonnet-4",
        username="alice",
    )
    # One build for the initial graph; a follow-up adds exactly one more when
    # the graph is new, changed or missing, and none when it is current.
    assert len(builds) == (1 if graph_state == "indexed" else 2)
    if graph_state == "new":
        assert "other" in builds[-1][-1]
    if graph_state == "indexed":
        # The freshness check hashes the repo to prove it is current; what
        # matters is that no rebuild was triggered (asserted on builds above).
        assert repo in scans
    if graph_state == "changed":
        assert (
            graft._root(workspace) / "repo" / "snapshot" / "a.py"
        ).read_text() == "new"
    if graph_state == "new":
        assert (workspace / "other" / ".git").is_dir()
        assert (
            graft._root(workspace) / "other" / "snapshot" / "a.py"
        ).read_text() == "other"
    assert await storage.get_chat_message_status(assistant_id) == "completed"


@pytest.mark.asyncio
async def test_process_chat_releases_barrier_before_ai(setup_test_db, monkeypatch):
    """Job deletion must not wait behind chat_with_ai."""
    from rootcoz import main as main_mod

    job_id = "chat-barrier-ai-job"
    held_during_ai: list[bool] = []

    async def fake_chat(**kwargs):
        held_during_ai.append(main_mod._get_chat_job_barrier(job_id).locked())
        assert kwargs.get("install_mcp") is False
        return True, "ok", "sess"

    monkeypatch.setattr("rootcoz.engine.chat.chat_with_ai", fake_chat)
    monkeypatch.setattr(
        "rootcoz.engine.chat.ensure_chat_workspace",
        lambda job_id, username="": setup_test_db,
    )
    monkeypatch.setattr(
        "rootcoz.engine.chat.clone_chat_repos",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "rootcoz.engine.chat.install_http_tools_mcp_best_effort_async",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "rootcoz.sources.chat_workspace.setup_ci_build_workspace",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        main_mod,
        "_resolve_chat_credentials",
        AsyncMock(return_value=("", "", None, "", "")),
    )
    monkeypatch.setattr(main_mod, "_create_ai_auth_header", AsyncMock(return_value=""))

    await storage.save_result(
        job_id,
        "",
        "completed",
        {
            "status": "completed",
            "summary": "t",
            "failures": [],
            "ai_provider": "claude",
            "ai_model": "sonnet-4",
        },
    )
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    await _seed_chat_session(job_id)
    _user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="claude", ai_model="sonnet-4"
    )
    await main_mod._process_chat_message(
        job_id=job_id,
        user_msg_id=_user_id,
        assistant_msg_id=assistant_id,
        message="hello",
        ai_provider_override="claude",
        ai_model_override="sonnet-4",
        username="alice",
    )
    assert held_during_ai == [False]
    assert await storage.get_chat_message_status(assistant_id) == "completed"


@pytest.mark.asyncio
async def test_process_chat_skip_deleting_marks_failed(setup_test_db):
    from rootcoz import main as main_mod

    job_id = "chat-deleting-job"
    await storage.save_result(
        job_id, "", "completed", {"status": "completed", "summary": "t", "failures": []}
    )
    _user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice"
    )
    main_mod._chat_jobs_deleting.add(job_id)
    try:
        await main_mod._process_chat_message(
            job_id=job_id,
            user_msg_id=_user_id,
            assistant_msg_id=assistant_id,
            message="hello",
            ai_provider_override="claude",
            ai_model_override="sonnet-4",
            username="alice",
        )
    finally:
        main_mod._chat_jobs_deleting.discard(job_id)
    assert await storage.get_chat_message_status(assistant_id) == "failed"


@pytest.mark.asyncio
async def test_cleanup_cancels_in_flight_chat_task(monkeypatch):
    """Deletion must cancel active chat work instead of racing the workspace."""
    from rootcoz import main as main_mod

    started = asyncio.Event()

    async def hang() -> None:
        async with main_mod._track_chat_job_task("job-cancel-ai"):
            started.set()
            await asyncio.sleep(30)

    monkeypatch.setattr(
        "rootcoz.engine.chat.cleanup_chat_workspace", lambda job_id, username="": None
    )
    task = asyncio.create_task(hang())
    await started.wait()
    t0 = asyncio.get_running_loop().time()
    await main_mod._cleanup_deleted_job_chat_workspaces("job-cancel-ai")
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 1
    assert task.cancelled() or task.done()
    assert "job-cancel-ai" not in main_mod._chat_job_tasks


@pytest.mark.asyncio
async def test_chat_init_releases_barrier_before_clone(setup_test_db, monkeypatch):
    """Job deletion must not wait behind clone/session initialization I/O."""
    from rootcoz import main as main_mod

    job_id = "chat-init-barrier-job"
    clone_started = asyncio.Event()
    barrier_held_during_clone: list[bool] = []

    async def fake_clone(*args, **kwargs):
        clone_started.set()
        barrier_held_during_clone.append(
            main_mod._get_chat_job_barrier(job_id).locked()
        )
        await asyncio.sleep(30)
        return False

    monkeypatch.setattr("rootcoz.engine.chat.clone_chat_repos", fake_clone)
    monkeypatch.setattr(
        "rootcoz.engine.chat.ensure_chat_workspace",
        lambda job_id, username="": setup_test_db,
    )
    await storage.save_result(
        job_id, "", "completed", {"status": "completed", "summary": "t", "failures": []}
    )
    monkeypatch.setattr(
        main_mod,
        "_validate_chat_selection",
        AsyncMock(return_value=("claude", "sonnet-4")),
    )
    init_task = asyncio.create_task(
        main_mod._init_chat_under_barrier(
            job_id,
            "alice",
            ChatInitRequest(ai_provider="claude", ai_model="sonnet-4"),
        )
    )
    await clone_started.wait()
    assert barrier_held_during_clone == [False]
    init_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await init_task


@pytest.mark.asyncio
async def test_chat_lock_keeps_identity_for_waiters():
    """A queued message must not get a new lock after the holder releases."""
    from rootcoz import main as main_mod

    key = "lock-id-job:alice"
    seen: list[asyncio.Lock] = []
    second_started = asyncio.Event()

    async def holder() -> None:
        async with main_mod._hold_chat_lock(key) as lock:
            seen.append(lock)
            await second_started.wait()
            await asyncio.sleep(0.05)

    async def waiter() -> None:
        await asyncio.sleep(0.01)
        second_started.set()
        waiting_lock = main_mod._chat_locks[key]
        async with main_mod._hold_chat_lock(key) as lock:
            seen.append(lock)
            assert lock is waiting_lock

    await asyncio.gather(holder(), waiter())
    assert seen[0] is seen[1]
    assert key not in main_mod._chat_locks


# ---------------------------------------------------------------------------
# Chat storage tests
# ---------------------------------------------------------------------------


class TestChatStorage:
    """Tests for chat message storage functions."""

    @pytest.mark.asyncio
    async def test_add_and_get_messages(self, setup_test_db):
        msg_id = await storage.add_chat_message(
            job_id="test-job-chat",
            role="user",
            content="hello",
            username="testuser",
        )
        assert msg_id > 0

        messages = await storage.get_chat_messages("test-job-chat", username="testuser")
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[0]["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_count_messages(self, setup_test_db):
        await storage.add_chat_message("count-job", "user", "msg1", username="u1")
        await storage.add_chat_message("count-job", "assistant", "msg2", username="u1")
        count = await storage.count_chat_messages("count-job", username="u1")
        assert count == 2

    @pytest.mark.asyncio
    async def test_delete_messages(self, setup_test_db):
        await storage.add_chat_message("del-job", "user", "msg", username="u1")
        deleted = await storage.delete_chat_messages("del-job", username="u1")
        assert deleted == 1
        messages = await storage.get_chat_messages("del-job", username="u1")
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_get_messages_pagination(self, setup_test_db):
        for i in range(5):
            await storage.add_chat_message(
                "page-job", "user", f"msg-{i}", username="u1"
            )

        messages = await storage.get_chat_messages(
            "page-job", limit=2, offset=0, username="u1"
        )
        assert len(messages) == 2
        assert messages[0]["content"] == "msg-0"
        assert messages[1]["content"] == "msg-1"

        messages = await storage.get_chat_messages(
            "page-job", limit=2, offset=3, username="u1"
        )
        assert len(messages) == 2
        assert messages[0]["content"] == "msg-3"
        assert messages[1]["content"] == "msg-4"

    @pytest.mark.asyncio
    async def test_get_messages_empty(self, setup_test_db):
        messages = await storage.get_chat_messages("nonexistent-job")
        assert messages == []

    @pytest.mark.asyncio
    async def test_count_messages_empty(self, setup_test_db):
        count = await storage.count_chat_messages("nonexistent-job")
        assert count == 0

    @pytest.mark.asyncio
    async def test_add_message_with_ai_fields(self, setup_test_db):
        msg_id = await storage.add_chat_message(
            job_id="ai-job",
            role="assistant",
            content="AI response",
            username="u1",
            ai_provider="claude",
            ai_model="sonnet-4",
        )
        assert msg_id > 0

        messages = await storage.get_chat_messages("ai-job", username="u1")
        assert len(messages) == 1
        assert messages[0]["ai_provider"] == "claude"
        assert messages[0]["ai_model"] == "sonnet-4"

    @pytest.mark.asyncio
    async def test_delete_returns_zero_when_empty(self, setup_test_db):
        deleted = await storage.delete_chat_messages("nothing-here")
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_messages_scoped_by_username(self, setup_test_db):
        """Messages are isolated per user when username is provided."""
        await storage.add_chat_message(
            "scope-job", "user", "from alice", username="alice"
        )
        await storage.add_chat_message("scope-job", "user", "from bob", username="bob")

        alice_msgs = await storage.get_chat_messages("scope-job", username="alice")
        assert len(alice_msgs) == 1
        assert alice_msgs[0]["content"] == "from alice"

        bob_msgs = await storage.get_chat_messages("scope-job", username="bob")
        assert len(bob_msgs) == 1
        assert bob_msgs[0]["content"] == "from bob"

        # Without username filter, all messages are returned
        all_msgs = await storage.get_chat_messages("scope-job")
        assert len(all_msgs) == 2

    @pytest.mark.asyncio
    async def test_delete_scoped_by_username(self, setup_test_db):
        """Deleting with username only removes that user's messages."""
        await storage.add_chat_message(
            "del-scope-job", "user", "alice msg", username="alice"
        )
        await storage.add_chat_message(
            "del-scope-job", "user", "bob msg", username="bob"
        )

        deleted = await storage.delete_chat_messages("del-scope-job", username="alice")
        assert deleted == 1

        remaining = await storage.get_chat_messages("del-scope-job")
        assert len(remaining) == 1
        assert remaining[0]["username"] == "bob"

    @pytest.mark.asyncio
    async def test_count_scoped_by_username(self, setup_test_db):
        """Count with username only counts that user's messages."""
        await storage.add_chat_message("cnt-scope-job", "user", "a", username="alice")
        await storage.add_chat_message("cnt-scope-job", "user", "b", username="alice")
        await storage.add_chat_message("cnt-scope-job", "user", "c", username="bob")

        assert await storage.count_chat_messages("cnt-scope-job", username="alice") == 2
        assert await storage.count_chat_messages("cnt-scope-job", username="bob") == 1
        assert await storage.count_chat_messages("cnt-scope-job") == 3


# ---------------------------------------------------------------------------
# Chat API endpoint tests
# ---------------------------------------------------------------------------


async def _seed_chat_session(job_id: str, username: str = "alice") -> None:
    """Persist an established server session for processor-only tests."""
    from rootcoz.ai_client import _selected_credential_source

    _selected_credential_source.set("server")
    session_id = f"session-{job_id}"
    await storage.save_ai_session_source(session_id, username, "claude", "server")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username=username,
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id=session_id,
    )


async def _start_chat(
    test_client, job_id: str, provider: str = "claude", model: str = "sonnet-4"
) -> None:
    """Start chat through the API with explicit server credentials."""
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    async def init_session(**kwargs):
        session_id = f"session-{job_id}"
        await storage.save_ai_session_source(
            session_id, "admin", kwargs["ai_provider"], "server"
        )
        return session_id

    with (
        patch.object(
            chat, "clone_chat_repos", new_callable=AsyncMock, return_value=False
        ),
        patch.object(
            chat_workspace,
            "setup_ci_build_workspace",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch.object(chat, "init_chat_session", side_effect=init_session),
        patch(
            "rootcoz.main._resolve_chat_credentials",
            new_callable=AsyncMock,
            return_value=("", "", None, "", ""),
        ),
        patch(
            "rootcoz.main._create_ai_auth_header",
            new_callable=AsyncMock,
            return_value="",
        ),
    ):
        response = test_client.post(
            f"/api/chat/{job_id}/init",
            json={"ai_provider": provider, "ai_model": model},
        )
    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == f"session-{job_id}"


async def _save_job(temp_db_path: Path, job_id: str, result: dict | None = None):
    """Save a result via the storage layer with DB_PATH patched."""
    default_result = {"status": "completed", "summary": "test", "failures": []}
    if result:
        default_result.update(result)

    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.save_result(job_id, "", "completed", default_result)


class TestChatEndpoints:
    """Tests for chat API endpoints."""

    def test_get_chat_history_404(self, test_client):
        response = test_client.get("/api/chat/nonexistent-job")
        assert response.status_code == 404

    async def test_get_chat_history_empty(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-test-job")
        response = test_client.get("/api/chat/chat-test-job")
        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == []
        assert data["total"] == 0

    async def test_send_chat_message(self, test_client, temp_db_path: Path):
        await _save_job(
            temp_db_path,
            "chat-send-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        await _start_chat(test_client, "chat-send-job")
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "AI response here", None)
            response = test_client.post(
                "/api/chat/chat-send-job",
                json={
                    "message": "what failed?",
                    "ai_provider": "claude",
                    "ai_model": "sonnet-4",
                },
            )
        assert response.status_code == 202
        data = response.json()
        assert data["user_message"]["content"] == "what failed?"
        assert data["user_message"]["status"] == "completed"
        assert "assistant_message_id" in data
        # Background task runs: verify the assistant message was completed
        history = test_client.get("/api/chat/chat-send-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[-1]["content"] == "AI response here"
        assert assistant_msgs[-1]["status"] == "completed"

    async def test_send_chat_message_saves_history(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(
            temp_db_path,
            "chat-hist-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        await _start_chat(test_client, "chat-hist-job")
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "first response", None)
            test_client.post(
                "/api/chat/chat-hist-job",
                json={
                    "message": "hello",
                    "ai_provider": "claude",
                    "ai_model": "sonnet-4",
                },
            )
        # Verify messages by fetching chat history
        response = test_client.get("/api/chat/chat-hist-job")
        data = response.json()
        assert data["total"] == 4  # hidden Start row, welcome, user, assistant
        assert len(data["messages"]) == 3
        assert data["messages"][1]["role"] == "user"
        assert data["messages"][1]["content"] == "hello"
        assert data["messages"][2]["role"] == "assistant"
        assert data["messages"][2]["content"] == "first response"

    async def test_delete_chat_history(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-del-job")
        await _start_chat(test_client, "chat-del-job")
        # Add a message via POST
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "resp", None)
            test_client.post(
                "/api/chat/chat-del-job",
                json={
                    "message": "hello",
                    "ai_provider": "claude",
                    "ai_model": "sonnet-4",
                },
            )
        version = test_client.get("/api/chat/chat-del-job").json()[
            "active_session_version"
        ]
        response = test_client.delete(
            "/api/chat/chat-del-job", headers={"If-Match": version}
        )
        assert response.status_code == 200
        assert response.json()["deleted"] == 4  # Start, welcome, user, assistant

    def test_delete_chat_history_404(self, test_client):
        response = test_client.delete("/api/chat/nonexistent-job")
        assert response.status_code == 404

    async def test_send_message_requires_start(self, test_client, temp_db_path: Path):
        """Sending without Start must not queue messages or inherit job settings."""
        await _save_job(
            temp_db_path,
            "chat-noprov-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        response = test_client.post(
            "/api/chat/chat-noprov-job", json={"message": "hello"}
        )
        assert response.status_code == 409
        assert "Start a new chat session" in response.json()["detail"]
        history = test_client.get("/api/chat/chat-noprov-job").json()
        assert history["total"] == 0

    def test_send_message_404_for_missing_job(self, test_client):
        response = test_client.post(
            "/api/chat/nonexistent-job",
            json={"message": "hello", "ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        assert response.status_code == 404

    async def test_send_message_rejects_unknown_catalog_pair(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(temp_db_path, "chat-badprov-job")
        response = test_client.post(
            "/api/chat/chat-badprov-job/init",
            json={"ai_provider": "invalid-provider", "ai_model": "x"},
        )
        assert response.status_code == 422
        assert "Unknown Pi-sidecar provider/model pair" in response.json()["detail"]

    async def test_send_message_normalizes_legacy_provider_alias(
        self, test_client, temp_db_path: Path
    ):
        """Legacy aliases retain their exact CLI sidecar provider route."""
        await _save_job(temp_db_path, "chat-alias-job")
        await _start_chat(test_client, "chat-alias-job", "cursor-cli", "composer-1")
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "ok", None)
            response = test_client.post(
                "/api/chat/chat-alias-job",
                json={
                    "message": "hello",
                    "ai_provider": "cursor-cli",
                    "ai_model": "composer-1",
                },
            )
        assert response.status_code == 202
        history = test_client.get("/api/chat/chat-alias-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[-1]["ai_provider"] == "cli-cursor"
        assert assistant_msgs[-1]["ai_model"] == "composer-1"

    async def test_send_message_normalizes_provider_case(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(temp_db_path, "chat-case-job")
        await _start_chat(test_client, "chat-case-job", "CURSOR", "composer-1")
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "ok", None)
            response = test_client.post(
                "/api/chat/chat-case-job",
                json={
                    "message": "hello",
                    "ai_provider": "CURSOR",
                    "ai_model": "composer-1",
                },
            )
        assert response.status_code == 202
        history = test_client.get("/api/chat/chat-case-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[-1]["ai_provider"] == "cursor"
        assert assistant_msgs[-1]["ai_model"] == "composer-1"

    async def test_send_message_provider_without_model_rejected(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(temp_db_path, "chat-partial-job")
        response = test_client.post(
            "/api/chat/chat-partial-job/init",
            json={"ai_provider": "claude"},
        )
        assert response.status_code == 422
        await _start_chat(test_client, "chat-partial-job")
        response = test_client.post(
            "/api/chat/chat-partial-job",
            json={"message": "hello", "ai_provider": "claude"},
        )
        assert response.status_code == 422
        assert "Select both provider and model" in response.json()["detail"]

    async def test_send_message_ai_failure_marks_failed(
        self, test_client, temp_db_path: Path
    ):
        """AI failure is handled in background — message is queued (202), then marked failed."""
        await _save_job(
            temp_db_path,
            "chat-fail-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        await _start_chat(test_client, "chat-fail-job")
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (False, "AI CLI timed out", None)
            response = test_client.post(
                "/api/chat/chat-fail-job",
                json={
                    "message": "hello",
                    "ai_provider": "claude",
                    "ai_model": "sonnet-4",
                },
            )
        assert response.status_code == 202
        # Background task processes failure — check history
        history = test_client.get("/api/chat/chat-fail-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[-1]["status"] == "failed"
        assert "AI CLI timed out" in assistant_msgs[-1]["content"]

    async def test_get_chat_with_pagination(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-page-job")
        await _start_chat(test_client, "chat-page-job")
        # Add messages via multiple POST calls
        with patch(
            "rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = (True, "resp", None)
            for i in range(3):
                test_client.post(
                    "/api/chat/chat-page-job",
                    json={
                        "message": f"msg-{i}",
                        "ai_provider": "claude",
                        "ai_model": "sonnet-4",
                    },
                )
        # 3 user + 3 assistant = 6 total
        response = test_client.get("/api/chat/chat-page-job?limit=2&offset=0")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 1  # offset 0 includes the hidden Start row
        assert data["total"] == 8  # Start, welcome, and three turns
        page = test_client.get("/api/chat/chat-page-job?limit=2&offset=1").json()
        assert len(page["messages"]) == 2


class TestAdminChatArtifactEndpoints:
    """Tests for admin chat artifact save/download endpoints."""

    def test_save_artifact(self, test_client):
        response = test_client.post(
            "/api/admin-chat/artifacts",
            json={
                "html_content": "<html><body>Report</body></html>",
                "filename": "test-report.html",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "artifact_id" in data
        assert "download_url" in data
        assert data["filename"] == "test-report.html"
        assert data["download_url"].startswith("/api/admin-chat/artifacts/")

    def test_save_artifact_adds_html_extension(self, test_client):
        response = test_client.post(
            "/api/admin-chat/artifacts",
            json={
                "html_content": "<html><body>Report</body></html>",
                "filename": "my-report",
            },
        )
        assert response.status_code == 200
        assert response.json()["filename"] == "my-report.html"

    def test_save_artifact_sanitizes_filename(self, test_client):
        response = test_client.post(
            "/api/admin-chat/artifacts",
            json={
                "html_content": "<html>test</html>",
                "filename": "../../etc/passwd",
            },
        )
        assert response.status_code == 200
        # Path separators replaced, no traversal
        filename = response.json()["filename"]
        assert "/" not in filename
        assert ".." not in filename

    def test_save_artifact_empty_content_rejected(self, test_client):
        response = test_client.post(
            "/api/admin-chat/artifacts",
            json={"html_content": "", "filename": "report.html"},
        )
        assert response.status_code == 422

    def test_save_artifact_missing_filename_rejected(self, test_client):
        response = test_client.post(
            "/api/admin-chat/artifacts",
            json={"html_content": "<html>test</html>"},
        )
        assert response.status_code == 422

    def test_download_artifact(self, test_client):
        # Save first
        save_resp = test_client.post(
            "/api/admin-chat/artifacts",
            json={
                "html_content": "<html><body>My Report</body></html>",
                "filename": "download-test.html",
            },
        )
        download_url = save_resp.json()["download_url"]

        # Download
        response = test_client.get(download_url)
        assert response.status_code == 200
        assert "<html><body>My Report</body></html>" in response.text
        assert "attachment" in response.headers.get("content-disposition", "")
        assert response.headers.get("content-type") == "text/html; charset=utf-8"

    def test_download_artifact_not_found(self, test_client):
        response = test_client.get(
            "/api/admin-chat/artifacts/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    def test_download_artifact_invalid_id(self, test_client):
        response = test_client.get("/api/admin-chat/artifacts/not-a-uuid")
        assert response.status_code == 404

    def test_download_artifact_path_traversal_blocked(self, test_client):
        response = test_client.get("/api/admin-chat/artifacts/../../etc/passwd")
        assert response.status_code == 404

    def test_artifact_cleanup_on_clear(self, test_client):
        # Save an artifact
        save_resp = test_client.post(
            "/api/admin-chat/artifacts",
            json={
                "html_content": "<html>cleanup test</html>",
                "filename": "cleanup.html",
            },
        )
        download_url = save_resp.json()["download_url"]

        # Verify it exists
        assert test_client.get(download_url).status_code == 200

        # Clear admin chat — should also clean up artifacts
        version = test_client.get("/api/admin/chat").json()["active_session_version"]
        test_client.delete("/api/admin/chat", headers={"If-Match": version})

        # Artifact should be gone
        assert test_client.get(download_url).status_code == 404


@pytest.mark.asyncio
class TestCiBuildDataForwarding:
    """Regression: ci_build_data_available must reach build_system_prompt."""

    async def test_init_chat_session_forwards_ci_build_data_flag(self):
        from rootcoz.engine.chat import init_chat_session

        with (
            patch(
                "rootcoz.engine.chat.build_system_prompt",
                return_value="prompt",
            ) as mock_prompt,
            patch(
                "rootcoz.engine.chat._create_chat_session",
                new_callable=AsyncMock,
                return_value="sess-1",
            ),
        ):
            await init_chat_session(
                job_id="j1",
                job_name="prow-job",
                build_number="1234567890123456789",
                ai_provider="claude",
                ai_model="claude-opus-4-6",
                ci_build_data_available=True,
            )

        mock_prompt.assert_called_once()
        assert mock_prompt.call_args.kwargs["ci_build_data_available"] is True

    async def test_chat_with_ai_forwards_ci_build_data_flag(self):
        from rootcoz.engine.chat import chat_with_ai

        with (
            patch(
                "rootcoz.engine.chat.build_system_prompt",
                return_value="prompt",
            ) as mock_prompt,
            patch(
                "rootcoz.engine.chat._chat_with_ai_impl",
                new_callable=AsyncMock,
                return_value=(True, "ok", "sess-1"),
            ) as mock_impl,
        ):
            await chat_with_ai(
                job_id="j1",
                job_name="prow-job",
                build_number="1234567890123456789",
                message="hello",
                history=[],
                ai_provider="claude",
                ai_model="claude-opus-4-6",
                ci_build_data_available=True,
            )
            build_prompt_fn = mock_impl.call_args.kwargs["build_prompt_fn"]
            build_prompt_fn()

        assert mock_prompt.call_args.kwargs["ci_build_data_available"] is True


@pytest.mark.parametrize("admin", [False, True])
async def test_rotation_does_not_reactivate_older_server_chat(
    test_client, temp_db_path, admin
):
    job_id = "__admin_chat__" if admin else "rotation-history-job"
    path = "/api/admin/chat" if admin else f"/api/chat/{job_id}"
    if not admin:
        await _save_job(temp_db_path, job_id)
    username, key = await storage.create_admin_user("rotation-owner")
    test_client.cookies.clear()
    headers = {"Authorization": f"Bearer {key}"}
    await storage.update_user_ai_credential(username, "claude", "old-key")

    # Persist the Start rows directly: the current API requires Clear before
    # switching sources, so it cannot produce this legacy sequence itself.
    for sid, source in (("older-server", "server"), ("newer-user", "user")):
        await storage.save_ai_session_source(sid, username, "claude", source)
        await storage.add_chat_message(
            job_id,
            "assistant",
            "",
            username=username,
            ai_provider="claude",
            ai_model="sonnet-4",
            session_id=sid,
        )
    await storage.add_chat_message(
        job_id, "user", "keep this history", username=username
    )
    await storage.update_user_ai_credential(username, "claude", "new-key")

    history = test_client.get(path, headers=headers)
    assert history.status_code == 200
    assert history.json()["active_session"] is None
    assert [msg["content"] for msg in history.json()["messages"]] == [
        "keep this history"
    ]
    assert (
        test_client.post(
            path, json={"message": "do not resume"}, headers=headers
        ).status_code
        == 409
    )
    assert await storage.get_latest_chat_session(job_id, username) is None
    assert (
        await storage.get_ai_session_source("older-server", username, "claude")
        == "server"
    )

    async def start(**kwargs):
        await storage.save_ai_session_source(
            "fresh-server", username, kwargs["ai_provider"], "server"
        )
        return "fresh-server"

    with (
        patch(
            "rootcoz.main._validate_chat_selection",
            new_callable=AsyncMock,
            return_value=("claude", "sonnet-4"),
        ),
        patch(
            "rootcoz.main._create_ai_auth_header",
            new_callable=AsyncMock,
            return_value="Bearer test",
        ),
        patch(
            "rootcoz.main._resolve_chat_credentials",
            new_callable=AsyncMock,
            return_value=("", "", None, "", ""),
        ),
        patch("rootcoz.engine.chat.clone_chat_repos", new_callable=AsyncMock),
        patch(
            "rootcoz.sources.chat_workspace.setup_ci_build_workspace",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "rootcoz.engine.chat.init_admin_chat_session"
            if admin
            else "rootcoz.engine.chat.init_chat_session",
            side_effect=start,
        ),
    ):
        response = test_client.post(
            f"{path}/init",
            json={"ai_provider": "claude", "ai_model": "sonnet-4"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert (
        test_client.get(path, headers=headers).json()["active_session"][
            "credential_source"
        ]
        == "server"
    )


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("raises", [False, True])
async def test_init_without_session_does_not_notify(
    test_client, temp_db_path, admin, raises
):
    job_id = "failed-start-job" if not admin else "__admin_chat__"
    path = "/api/admin/chat" if admin else f"/api/chat/{job_id}"
    if not admin:
        await _save_job(temp_db_path, job_id)
    with (
        patch(
            "rootcoz.main._validate_chat_selection",
            new_callable=AsyncMock,
            return_value=("claude", "sonnet-4"),
        ),
        patch(
            "rootcoz.main._create_ai_auth_header",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "rootcoz.main._resolve_chat_credentials",
            new_callable=AsyncMock,
            return_value=("", "", None, "", ""),
        ),
        patch("rootcoz.engine.chat.clone_chat_repos", new_callable=AsyncMock),
        patch(
            "rootcoz.sources.chat_workspace.setup_ci_build_workspace",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "rootcoz.engine.chat.init_admin_chat_session"
            if admin
            else "rootcoz.engine.chat.init_chat_session",
            new_callable=AsyncMock,
            side_effect=RuntimeError("session init failed") if raises else None,
            return_value=None,
        ),
        # Preparation phases notify their own progress; this asserts the
        # completion contract — no session means no "chat changed" broadcast.
        patch("rootcoz.main.set_chat_prep_phase"),
        patch("rootcoz.main.clear_chat_prep_phase"),
        patch("rootcoz.main.notify_chat_changed") as notify,
    ):
        if raises:
            with pytest.raises(RuntimeError, match="session init failed"):
                test_client.post(
                    f"{path}/init",
                    json={
                        "ai_provider": "claude",
                        "ai_model": "sonnet-4",
                    },
                )
        else:
            response = test_client.post(
                f"{path}/init",
                json={"ai_provider": "claude", "ai_model": "sonnet-4"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["session_id"] == ""
        notify.assert_not_called()
    assert test_client.get(path).json()["active_session"] is None


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
async def test_orphan_history_can_start_without_losing_messages(
    test_client, temp_db_path, admin, legacy
):
    job_id = "orphan-job" if not admin else "__admin_chat__"
    path = "/api/admin/chat" if admin else f"/api/chat/{job_id}"
    if not admin:
        await _save_job(temp_db_path, job_id)
    await storage.add_chat_message(job_id, "user", "older turn", username="admin")
    if legacy:
        await storage.add_chat_message(
            job_id,
            "assistant",
            "",
            username="admin",
            ai_provider="claude",
            ai_model="sonnet-4",
            session_id="legacy-unowned",
        )
    history = test_client.get(path).json()
    assert history["active_session"] is None
    assert history["total"] == 1 + int(legacy)
    assert [m["content"] for m in history["messages"]] == ["older turn"]
    assert bool(await storage.get_latest_chat_session(job_id, "admin")) == legacy

    async def create(**kwargs):
        await storage.save_ai_session_source(
            "new-orphan-session", "admin", kwargs["ai_provider"], "server"
        )
        return "new-orphan-session"

    def select(*args, **kwargs):
        """Resolve as the real validator does: this pair comes from the server pool."""
        from rootcoz import ai_client

        ai_client._selected_credential_source.set("server")
        return "claude", "sonnet-4"

    with (
        patch("rootcoz.main._validate_chat_selection", side_effect=select),
        patch(
            "rootcoz.main._create_ai_auth_header",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "rootcoz.engine.chat.init_admin_chat_session"
            if admin
            else "rootcoz.engine.chat.init_chat_session",
            side_effect=create,
        ) as init,
        patch("rootcoz.engine.chat.clone_chat_repos", new_callable=AsyncMock),
        patch(
            "rootcoz.sources.chat_workspace.setup_ci_build_workspace",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "rootcoz.main._resolve_chat_credentials",
            new_callable=AsyncMock,
            return_value=("", "", None, "", ""),
        ),
        patch("rootcoz.main.notify_chat_changed") as notify,
    ):
        body = {"ai_provider": "claude", "ai_model": "sonnet-4"}
        first = test_client.post(f"{path}/init", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["session_id"] == "new-orphan-session"
        # Preparation phases notify too; every notify stays scoped to the user.
        assert notify.call_args_list
        assert all(c == call(job_id, username="admin") for c in notify.call_args_list)
        assert await storage.get_latest_chat_session(job_id, "admin") is not None
        notifies_before_retry = notify.call_count
        second = test_client.post(f"{path}/init", json=body)
        assert second.status_code == 200, second.text
        assert second.json()["session_id"] == first.json()["session_id"]
        assert notify.call_count > notifies_before_retry
        assert all(c == call(job_id, username="admin") for c in notify.call_args_list)
        assert init.call_count == 1
    history = test_client.get(path).json()
    assert history["active_session"] == {
        "ai_provider": "claude",
        "ai_model": "sonnet-4",
        "credential_source": "server",
    }
    assert "session_id" not in history["active_session"]
    assert any(m["content"] == "older turn" for m in history["messages"])


@pytest.mark.parametrize("admin", [False, True])
async def test_clear_rejects_stale_same_model_session_without_leaking_id(
    test_client, temp_db_path, admin
):
    job_id = "clear-race-job" if not admin else "__admin_chat__"
    path = "/api/admin/chat" if admin else f"/api/chat/{job_id}"
    if not admin:
        await _save_job(temp_db_path, job_id)
    username, key = await storage.create_admin_user("clear-race-owner")
    test_client.cookies.clear()
    headers = {"Authorization": f"Bearer {key}"}

    async def start(sid):
        await storage.save_ai_session_source(sid, username, "claude", "server")
        await storage.add_chat_message(
            job_id,
            "assistant",
            "",
            username=username,
            ai_provider="claude",
            ai_model="sonnet-4",
            session_id=sid,
        )

    await start("private-first-id")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "visible reply",
        username=username,
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="private-first-id",
    )
    first = test_client.get(path, headers=headers).json()
    old_version = first["active_session_version"]
    assert old_version and "private-first-id" not in str(first)
    assert test_client.delete(path, headers=headers).status_code == 409
    await start(
        "private-second-id"
    )  # Another tab Started the same pair after preflight.
    second = test_client.get(path, headers=headers).json()
    assert second["active_session"] == first["active_session"]
    assert second["active_session_version"] != old_version
    assert "private-second-id" not in str(second)
    assert "private-first-id" not in str(second)
    assert (
        test_client.delete(
            path, headers={**headers, "If-Match": old_version}
        ).status_code
        == 409
    )
    assert await storage.get_latest_chat_session(job_id, username) is not None
    assert (
        test_client.delete(
            path, headers={**headers, "If-Match": second["active_session_version"]}
        ).status_code
        == 200
    )
    assert await storage.get_latest_chat_session(job_id, username) is None
    assert (
        test_client.delete(
            path, headers={**headers, "If-Match": second["active_session_version"]}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("admin", [False, True])
async def test_clear_none_version_only_matches_no_start(
    test_client, temp_db_path, admin
):
    job_id = "clear-none-job" if not admin else "__admin_chat__"
    path = "/api/admin/chat" if admin else f"/api/chat/{job_id}"
    if not admin:
        await _save_job(temp_db_path, job_id)
    await storage.add_chat_message(job_id, "user", "orphan", username="admin")
    version = test_client.get(path).json()["active_session_version"]
    assert version
    await storage.save_ai_session_source("new-after-empty", "admin", "claude", "server")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username="admin",
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="new-after-empty",
    )
    assert test_client.delete(path, headers={"If-Match": version}).status_code == 409
    assert await storage.count_chat_messages(job_id, username="admin") == 2


@pytest.mark.parametrize("admin", [False, True])
async def test_clear_orphan_history_with_matching_none_version(
    test_client, temp_db_path, admin
):
    job_id = "orphan-clear-job" if not admin else "__admin_chat__"
    path = "/api/admin/chat" if admin else f"/api/chat/{job_id}"
    if not admin:
        await _save_job(temp_db_path, job_id)
    await storage.add_chat_message(job_id, "user", "orphan", username="admin")
    version = test_client.get(path).json()["active_session_version"]
    response = test_client.delete(path, headers={"If-Match": version})
    assert response.status_code == 200
    assert response.json()["deleted"] == 1


async def test_session_availability_checks_ownership_grant_and_key(
    test_client, temp_db_path
):
    job_id = "availability-job"
    path = f"/api/chat/{job_id}"
    await _save_job(temp_db_path, job_id)
    username, key = await storage.create_admin_user("session-owner")
    test_client.cookies.clear()
    await storage.change_user_role(username, "reviewer")
    headers = {"Authorization": f"Bearer {key}"}
    await storage.save_ai_session_source("sid-other", "admin", "claude", "server")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username=username,
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="sid-other",
    )
    assert test_client.get(path, headers=headers).json()["active_session"] is None

    await storage.save_ai_session_source("sid-server", username, "claude", "server")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username=username,
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="sid-server",
    )
    assert test_client.get(path, headers=headers).json()["active_session"] is None
    await storage.set_user_can_use_server_providers(username, True)
    assert (
        test_client.get(path, headers=headers).json()["active_session"][
            "credential_source"
        ]
        == "server"
    )
    await storage.revoke_ai_session_source("sid-server")
    assert test_client.get(path, headers=headers).json()["active_session"] is None

    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username=username,
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="legacy-unowned",
    )
    assert test_client.get(path, headers=headers).json()["active_session"] is None
    from rootcoz import ai_client as ai_client_module

    with patch.object(
        ai_client_module,
        "provider_accepts_user_keys",
        new_callable=AsyncMock,
        return_value=True,
    ):
        await storage.update_user_ai_credential(username, "claude", "valid-user-key")
        await storage.save_ai_session_source("sid-user", username, "claude", "user")
        await storage.add_chat_message(
            job_id,
            "assistant",
            "",
            username=username,
            ai_provider="claude",
            ai_model="sonnet-4",
            session_id="sid-user",
        )
        assert (
            test_client.get(path, headers=headers).json()["active_session"][
                "credential_source"
            ]
            == "user"
        )
        with patch.object(
            storage,
            "get_user_ai_credential_with_generation",
            new_callable=AsyncMock,
            return_value=("   ", 1),
        ):
            assert (
                test_client.get(path, headers=headers).json()["active_session"] is None
            )
        await storage.update_user_ai_credential(username, "claude", "rotated-user-key")
        assert test_client.get(path, headers=headers).json()["active_session"] is None


async def test_chat_availability_does_not_bypass_route_authorization(
    test_client, temp_db_path
):
    await _save_job(temp_db_path, "authorized-job")
    viewer, key = await storage.create_admin_user("history-viewer")
    await storage.change_user_role(viewer, "viewer")
    test_client.cookies.clear()
    headers = {"Authorization": f"Bearer {key}"}
    assert (
        test_client.get("/api/chat/authorized-job", headers=headers).status_code == 200
    )
    assert test_client.get("/api/admin/chat", headers=headers).status_code == 403
    assert (
        test_client.post(
            "/api/chat/authorized-job/init",
            headers=headers,
            json={"ai_provider": "claude", "ai_model": "sonnet-4"},
        ).status_code
        == 403
    )


class TestChatIssueRepoAllowList:
    """A job chat files only in that run's repos; an admin may name any (#407)."""

    _PARAMS: ClassVar[dict[str, Any]] = {
        "job_name": "job",
        "build_number": 1,
        "tests_repo_url": "https://github.com/org/tests",
        "additional_repos": [{"name": "lib", "url": "https://github.com/org/lib"}],
    }

    async def _save_run(self, temp_db_path: Path, job_id: str, params=None) -> str:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id,
                "",
                "completed",
                {
                    "status": "completed",
                    "summary": "t",
                    "failures": [],
                    "request_params": dict(params or self._PARAMS),
                },
            )
        return job_id

    async def _reviewer(self, username: str = "repo-filer") -> dict[str, str]:
        _, api_key = await storage.create_user(username, role="reviewer")
        await storage.save_user_tokens(
            username,
            github_token="ghp_row_token",  # pragma: allowlist secret
        )
        return {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    async def _reply_as(headers: dict[str, str], code: str, conversation: str) -> None:
        """Store the user message whose code is the confirmation (see #407)."""
        token = headers["Authorization"].removeprefix("Bearer ").strip()
        user = await storage.get_user_by_key(token)
        assert user is not None
        await storage.add_chat_message(
            conversation,
            role="user",
            content=f"Yes, file it: {code}",
            username=user["username"],
        )

    @classmethod
    async def _draft(
        cls, test_client, headers, *, job_id="", repo=None, conversation=None
    ):
        """The proposal step alone: the response says whether it can be filed."""
        if conversation is None:
            conversation = job_id or "__admin_chat__"
        proposal_body: dict[str, str] = {
            "target": "github",
            "title": "t",
            "body": "b",
            # The chat this draft belongs to; the user replies in it.
            "conversation": conversation,
        }
        if job_id:
            proposal_body["job_id"] = job_id
        if repo is not None:
            proposal_body["repo"] = repo
        return test_client.post(
            "/api/chat-issue/propose", json=proposal_body, headers=headers
        )

    @classmethod
    async def _file(
        cls,
        test_client,
        headers: dict[str, str],
        *,
        job_id: str = "",
        repo: str | None = None,
    ):
        """Propose, reply with the code, create — the gate every write crosses."""
        proposal = await cls._draft(test_client, headers, job_id=job_id, repo=repo)
        assert proposal.status_code == 201, proposal.text
        code = proposal.json()["confirmation_code"]
        await cls._reply_as(headers, code, job_id or "__admin_chat__")
        return test_client.post(
            "/api/chat-issue",
            json={
                "target": "github",
                "title": proposal.json()["title"],
                "body": proposal.json()["body"],
                "confirmation_id": proposal.json()["confirmation_id"],
                "confirmation_code": code,
                **({"repo": repo} if repo is not None else {}),
                **({"job_id": job_id} if job_id else {}),
            },
            headers=headers,
        )

    @staticmethod
    def _filed_repo(mock_create) -> str:
        assert mock_create.call_count == 1, mock_create.call_args
        return mock_create.call_args.kwargs["repo_url"]

    async def test_empty_repo_files_in_the_runs_test_repo(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        """Leaving repo empty is today's behaviour: the run's test repo."""
        job_id = await self._save_run(temp_db_path, "job-repo-default")
        headers = await self._reviewer()
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/tests/issues/1",
                "number": 1,
            }
            response = await self._file(test_client, headers, job_id=job_id)
        assert response.status_code == 201, response.text
        assert self._filed_repo(mock_create) == "https://github.com/org/tests"

    async def test_the_runs_test_repo_is_accepted(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        job_id = await self._save_run(temp_db_path, "job-repo-tests")
        headers = await self._reviewer()
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/tests/issues/2",
                "number": 2,
            }
            response = await self._file(
                test_client, headers, job_id=job_id, repo="org/tests"
            )
        assert response.status_code == 201, response.text
        assert self._filed_repo(mock_create) == "https://github.com/org/tests"

    async def test_an_additional_repo_is_accepted(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        job_id = await self._save_run(temp_db_path, "job-repo-additional")
        headers = await self._reviewer()
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/lib/issues/3",
                "number": 3,
            }
            response = await self._file(
                test_client, headers, job_id=job_id, repo="org/lib"
            )
        assert response.status_code == 201, response.text
        assert self._filed_repo(mock_create) == "https://github.com/org/lib"

    async def test_a_foreign_repo_is_refused_and_names_the_allowed_set(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        """The destination is resolved at the proposal, before any approval."""
        job_id = await self._save_run(temp_db_path, "job-repo-foreign")
        headers = await self._reviewer()
        with patch("rootcoz.main.create_github_issue") as mock_create:
            response = await self._draft(
                test_client, headers, job_id=job_id, repo="someone/elsewhere"
            )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "someone/elsewhere" in detail
        assert "org/tests" in detail and "org/lib" in detail
        assert "only go to this run's test repo and additional repos" in detail
        mock_create.assert_not_called()

    async def test_a_changed_server_default_does_not_widen_the_allow_list(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        """A job chat may not name the deployment default; an admin still may.

        ``github_issue_config`` sets ``TESTS_REPO_URL`` to ``org/repo``, which
        this run never had. The job chat must refuse it and name the run's own
        repos; the same request from an admin files there.
        """
        job_id = await self._save_run(temp_db_path, "job-repo-server-default")
        headers = await self._reviewer("server-default-filer")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            response = await self._draft(
                test_client, headers, job_id=job_id, repo="org/repo"
            )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "org/repo" in detail
        assert "org/lib, org/tests" in detail
        assert "only go to this run's test repo and additional repos" in detail
        mock_create.assert_not_called()

        admin, api_key = await storage.create_admin_user("server-default-admin")
        await storage.save_user_tokens(
            admin,
            github_token="ghp_row_token",  # pragma: allowlist secret
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/6",
                "number": 6,
            }
            response = await self._file(
                test_client,
                {"Authorization": f"Bearer {api_key}"},
                repo="org/repo",
            )
        assert response.status_code == 201, response.text
        assert self._filed_repo(mock_create) == "https://github.com/org/repo"

    async def test_a_run_without_a_test_repo_cannot_use_the_server_default(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        """An empty repo is the run's test repo — never the deployment fallback."""
        job_id = await self._save_run(
            temp_db_path,
            "job-repo-no-default",
            {
                "job_name": "job",
                "build_number": 1,
                "tests_repo_url": "",
                "additional_repos": [
                    {"name": "lib", "url": "https://github.com/org/lib"}
                ],
            },
        )
        headers = await self._reviewer("no-default-filer")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            response = await self._draft(test_client, headers, job_id=job_id)
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "this run has no GitHub test repository" in detail
        assert "org/lib" in detail
        assert "org/repo" not in detail
        mock_create.assert_not_called()

    async def test_a_malformed_repo_is_refused_as_a_url_or_a_bare_name(
        self, test_client, github_issue_config
    ):
        """Not a 422: the detail is the string the model relays to the user."""
        headers = await self._reviewer("slug")
        for bad in ("https://github.com/org/tests", "tests"):
            response = test_client.post(
                "/api/chat-issue/propose",
                json={"target": "github", "title": "t", "body": "b", "repo": bad},
                headers=headers,
            )
            assert response.status_code == 400, response.text
            assert "'owner/name'" in response.json()["detail"]

    async def test_an_admin_may_name_any_repo(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        admin, api_key = await storage.create_admin_user("repo-admin")
        await storage.save_user_tokens(
            admin,
            github_token="ghp_row_token",  # pragma: allowlist secret
        )
        headers = {"Authorization": f"Bearer {api_key}"}
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/any/where/issues/4",
                "number": 4,
            }
            response = await self._file(test_client, headers, repo="any/where")
        assert response.status_code == 201, response.text
        assert self._filed_repo(mock_create) == "https://github.com/any/where"

    async def test_a_non_admin_without_a_run_is_refused(
        self, test_client, github_issue_config
    ):
        headers = await self._reviewer("no-run")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            response = await self._draft(test_client, headers, repo="org/tests")
        assert response.status_code == 400, response.text
        assert "must be tied to a run" in response.json()["detail"]
        mock_create.assert_not_called()

    async def test_a_run_repo_alone_is_enough_to_offer_and_file(
        self, test_client, temp_db_path: Path
    ):
        """No TESTS_REPO_URL: the run's own repo is a destination, so the tool is offered."""
        from rootcoz.config import get_settings
        from rootcoz.main import _chat_tracker_state, _job_issue_repo_slugs

        job_id = await self._save_run(temp_db_path, "job-repo-only")
        _default, slugs = _job_issue_repo_slugs(self._PARAMS)
        targets, unavailable = _chat_tracker_state(
            get_settings(),
            {"github_token": "ghp_row_token"},  # pragma: allowlist secret
            github_repo_available=bool(slugs),
        )
        assert "github" in targets, unavailable

        headers = await self._reviewer("repo-only")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/tests/issues/5",
                "number": 5,
            }
            response = await self._file(test_client, headers, job_id=job_id)
        assert response.status_code == 201, response.text
        assert self._filed_repo(mock_create) == "https://github.com/org/tests"

    @pytest.mark.parametrize(
        ("analysis_type", "own_fields"),
        [
            ("prow", {"prow_job_name": "periodic-job", "build_id": "123"}),
            ("raw", {"failures": [{"test_name": "t", "error_message": "boom"}]}),
            ("file", {"raw_xml": "<testsuite name='s'/>"}),
        ],
    )
    def test_every_analysis_type_replays_the_fields_its_validator_needs(
        self, analysis_type: str, own_fields: dict[str, Any]
    ):
        """A prow/raw/file run has no job_name/build_number to be replayed by.

        Its type validator needs the fields that type itself stored
        (``prow_job_name``/``build_id``, ``raw_xml``, ``failures``), and the
        replay was the only thing building the repo allow-list: without them
        the list came back empty and chat could not file an issue at all.
        """
        from rootcoz.main import _job_issue_repo_slugs

        default, slugs = _job_issue_repo_slugs(
            {
                "analysis_type": analysis_type,
                "tests_repo_url": "https://github.com/org/tests",
                **own_fields,
            }
        )
        assert default == "org/tests"
        assert slugs == frozenset({"org/tests"})

    def test_an_unreplayable_run_names_the_missing_field_in_the_log(self):
        """A degraded chat says WHICH stored field stopped the replay.

        The generic "cannot replay" warning made an unavailable feature
        indistinguishable from a run with no repos at all.
        """
        from rootcoz.main import _job_issue_repo_slugs

        with patch("rootcoz.main.logger.warning") as warn:
            default, slugs = _job_issue_repo_slugs(
                {
                    "analysis_type": "prow",
                    "tests_repo_url": "https://github.com/org/tests",
                }
            )
        assert (default, slugs) == ("", frozenset())
        assert "prow_job_name is required for type=prow" in str(warn.call_args)

    def test_job_prompt_lists_the_allowed_repos(self):
        repos = ("org/tests", "org/lib")
        prompt = build_system_prompt(
            job_name="job",
            build_number=1,
            job_id="j1",
            custom_tools=build_chat_custom_tools(
                server_url="http://localhost:8000",
                auth_token="tok",
                job_id="j1",
                tracker_targets=("github",),
                issue_repos=repos,
            ),
            issue_repos=repos,
        )
        assert "only file a GitHub issue in this run's repos" in prompt
        assert "org/tests, org/lib" in prompt

    def test_admin_prompt_says_any_repo(self):
        prompt = build_admin_system_prompt(
            build_admin_custom_tools(
                server_url="http://localhost:8000",
                auth_token="tok",
                tracker_targets=("github",),
            )
        )
        assert "may name any GitHub repository" in prompt


class TestChatSessionToolRefresh:
    """A resumed sidecar session keeps its creation-time tools (#407).

    ``chat_with_ai`` resumes an existing session by sending only the message, so
    a tracker enabled — or a token saved — after the session started never
    reaches the model in it. The session is rebuilt instead of resumed.
    """

    def test_an_unchanged_tool_set_resumes_the_session(self):
        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-same", ("github",))
        assert main_mod._enable_tools_for_session("sid-same", ("github",)) == "sid-same"

    def test_a_newly_enabled_tracker_rebuilds_the_session(self):
        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-new", ("github",))
        assert main_mod._enable_tools_for_session("sid-new", ("github", "jira")) is None

    def test_removed_access_also_rebuilds_the_session(self):
        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-gone", ("github", "jira"))
        assert main_mod._enable_tools_for_session("sid-gone", ("github",)) is None

    def test_a_failed_rebuild_does_not_resume_the_stale_session(self):
        """#416: a rebuild whose turn fails must keep the session stale.

        GitHub is disabled (or its token removed) and the turn that would have
        rebuilt the session fails. The next message must still rebuild instead
        of adopting the old session and offering its removed tool again.
        """
        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-failed", ("github",))
        assert main_mod._enable_tools_for_session("sid-failed", ()) is None
        assert main_mod._enable_tools_for_session("sid-failed", ()) is None

    def test_an_unknown_session_is_adopted_then_rebuilt_on_a_change(self):
        """After a restart the creation-time set is unknown — adopt, do not churn."""
        from rootcoz import main as main_mod

        assert (
            main_mod._enable_tools_for_session("sid-unknown", ("github",))
            == "sid-unknown"
        )
        assert (
            main_mod._enable_tools_for_session("sid-unknown", ("github", "jira"))
            is None
        )

    def test_no_session_means_build_one(self):
        from rootcoz.main import _enable_tools_for_session

        assert _enable_tools_for_session(None, ("github",)) is None

    def test_a_rebuild_keeps_the_stale_targets_known(self):
        """A mismatch is remembered, so a failed rebuild is retried, not adopted."""
        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-stale", ("github",))
        assert main_mod._enable_tools_for_session("sid-stale", ("jira",)) is None
        assert main_mod._chat_session_tracker_targets["sid-stale"] == ("github",)
        assert main_mod._enable_tools_for_session("sid-stale", ("jira",)) is None
