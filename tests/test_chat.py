"""Tests for the chat feature."""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, call, patch

import pytest
from pi_sidecar_client import AIResult

from rootcoz import storage
from rootcoz.config import Settings
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
from tests.conftest import AdvancingClock, capture_main_logs

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
        from rootcoz.config import clear_settings_cache

        clear_settings_cache()
        try:
            yield
        finally:
            clear_settings_cache()


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
    from rootcoz.config import clear_settings_cache

    with patch.dict(
        os.environ,
        {
            "TESTS_REPO_URL": "https://github.com/org/repo",
            "GITHUB_TOKEN": "ghp_test",  # pragma: allowlist secret
        },
    ):
        clear_settings_cache()
        yield
        clear_settings_cache()


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
        assert "TESTS_REPO_URL" not in refusal.detail
        assert "ask an administrator" in refusal.detail

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

    def test_a_deployment_with_no_jira_method_names_the_admin_setting(self):
        """Issue #499: no saved credential can file where the deployment itself
        configures no Jira auth method, so "re-save your credential" sends the
        user to Profile Settings to fix nothing."""
        from rootcoz.engine.chat import _issue_create_rules
        from rootcoz.main import _chat_tracker_gate, _chat_tracker_state

        # No auth method selected and no URL/ids for any of them.
        unconfigured = self._settings(
            jira_server_url="",
            jira_cloud_url="",
            atlassian_oauth_client_id="",
            atlassian_oauth_cloud_id="",
        )
        reason, refusal = _chat_tracker_gate(
            "jira", unconfigured, {"jira_token": "pat"}
        )
        assert reason == "server_unconfigured"
        assert refusal is not None and refusal.status_code == 400
        assert "Server Settings" in refusal.detail
        assert (
            _chat_tracker_state(
                unconfigured, {"jira_token": "pat"}, github_repo_available=True
            )[1]["jira"]
            == "server_unconfigured"
        )
        prompt = _issue_create_rules({"jira": "server_unconfigured"})
        assert "re-saving the Jira credential will not help" in prompt
        assert "Server Settings" in prompt

    def test_a_credential_the_configured_server_cannot_serve_says_resave(self):
        """The other half of #499: the server has a method, the saved credential
        pins one it cannot serve — re-saving the credential IS the fix."""
        from rootcoz.engine.chat import _issue_create_rules
        from rootcoz.main import _chat_tracker_gate

        # Cloud-only basic auth configured; a bare token pins ``pat``, which
        # needs ``jira_server_url``.
        cloud = self._settings(
            jira_auth_mode="basic",
            jira_cloud_url="https://example.atlassian.net",
            jira_server_url="",
        )
        reason, refusal = _chat_tracker_gate("jira", cloud, {"jira_token": "pat"})
        assert reason == "unusable_credential"
        assert refusal is not None and refusal.status_code == 400
        prompt = _issue_create_rules({"jira": "unusable_credential"})
        assert "re-save the Jira credential" in prompt
        assert "Server Settings" not in prompt

    def test_a_legacy_sa_token_on_an_unconfigured_server_names_the_admin(self):
        """A legacy service-account value is refused, but its own sentence
        points at Profile Settings — which cannot fix a deployment that serves
        no Jira credential. The deployment is asked first, so the answer names
        the administrator and never the account."""
        from rootcoz.error_messages import unavailable_sentence
        from rootcoz.main import _chat_tracker_gate

        settings = _unconfigured_jira_settings()
        reason, refusal = _chat_tracker_gate(
            "jira", settings, {"jira_sa_token": "legacy-not-atoa"}
        )
        assert reason == "server_unconfigured"
        assert refusal is not None
        assert refusal.detail == unavailable_sentence("jira", "server_unconfigured")
        assert "Profile Settings" not in refusal.detail

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

    def test_a_caller_with_no_github_token_is_told_the_real_fix(self):
        """Pin the no-credential answer, because the model ignored it in e2e.

        A chat whose tool list held no ``propose_tracker_issue`` /
        ``create_github_issue`` still answered "I'll draft it" on one attempt and
        blamed a "session-level tooling gap rather than account config" on the
        next — never the token, twice. Model prose cannot be asserted, so the
        product half is pinned here: the exact instruction the server sends and
        the tools it withholds. The model half stays a manual e2e signal; see
        issue #446 for the detail.
        """
        from rootcoz.main import _chat_tracker_state

        settings = TestChatTrackerTargets._settings()

        def told_and_offered(tokens: dict[str, str]) -> tuple[str, list[str]]:
            """The prompt and tool names this caller's chat session is built with."""
            targets, unavailable = _chat_tracker_state(
                settings,
                tokens,
                github_repo_available=bool(settings.tests_repo_url),
            )
            tools = build_chat_custom_tools(
                server_url="http://localhost:8000",
                auth_token="tok",
                job_id="j1",
                tracker_targets=targets,
            )
            return (
                build_system_prompt(
                    job_name="job",
                    build_number=1,
                    job_id="j1",
                    custom_tools=tools,
                    issue_repos=("org/repo",),
                    tracker_unavailable=unavailable,
                ),
                [tool["name"] for tool in tools],
            )

        # No saved token: the sentence naming the account and the fix, and no
        # tool that could file the issue.
        prompt, names = told_and_offered({})
        assert (
            "GitHub: this account has no GitHub token saved. Tell the user the "
            "fix: Profile Settings → add the GitHub token." in prompt
        )
        assert "propose_tracker_issue" not in names
        assert "create_github_issue" not in names

        # A saved token: the tools appear and the no-token sentence is gone, so
        # the assertions above cannot pass by naming a tool that is never built.
        tokens = {"github_token": "ghp_user"}  # pragma: allowlist secret
        prompt, names = told_and_offered(tokens)
        assert "propose_tracker_issue" in names
        assert "create_github_issue" in names
        assert "this account has no GitHub token saved" not in prompt


class TestAskedIssueTracker:
    """The trigger half: only a filing request that names a tracker fires."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("can you open a GitHub issue for this failure?", "github"),
            ("file a jira ticket about this", "jira"),
            ("Could you create an issue in GitHub?", "github"),
            ("submit a bug report as a Jira ticket", "jira"),
            # Both named in the request: the nearest mention wins.
            ("open a Jira ticket or a GitHub issue", "jira"),
            ("open an issue in GitHub", "github"),
            # A tracker named as background context is not the destination.
            (
                "GitHub is our source repository; please create a Jira issue",
                "jira",
            ),
            ("GitHub is down. Please open a Jira ticket.", "jira"),
            # The tracker is bound to the request, not to the message start.
            ("GitHub is blocked for me, file a Jira issue", "jira"),
            ("Jira is blocked for me, file a GitHub issue", "github"),
            # The noun-first order people also write.
            ("I need a GitHub issue filed for this failure", "github"),
            # Every mention counts, so the name inside the request is the one
            # bound to it even when the tracker is named again afterwards.
            (
                "GitHub is irrelevant. Please file a GitHub issue, not a Jira issue.",
                "github",
            ),
            # A refusal or a report of an action already taken is not a request.
            ("Don't open a GitHub issue about this", ""),
            ("do not open a jira ticket", ""),
            ("I already filed a GitHub issue about this", ""),
            # The negation is read only where it touches the verb.
            ("I don't have a Jira token, please open a Jira issue", "jira"),
            # A refused phrase does not swallow the request after it.
            (
                "I already filed a GitHub issue. Please create a Jira issue.",
                "jira",
            ),
            ("Don't open a GitHub issue. Open a Jira ticket.", "jira"),
            # Not a filing request.
            ("why did this test fail?", ""),
            ("open the console log", ""),
            # A filing request that names no tracker has no single tracker to name.
            ("open an issue about this", ""),
            ("", ""),
        ],
    )
    def test_it_reads_a_filing_request_that_names_a_tracker(self, message, expected):
        from rootcoz.engine.chat import asked_issue_tracker

        assert asked_issue_tracker(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            "I need a GitHub issue filed for this failure",
            "Please have a ticket opened",
        ],
    )
    def test_it_reads_the_other_word_orders_people_write(self, message):
        """The noun first and a participle after it is still a filing request."""
        from rootcoz.engine.chat import _ASKS_TO_FILE_ISSUE

        assert _ASKS_TO_FILE_ISSUE.search(message)


class TestCredentialBlockedIssueNotice:
    """The text the server posts: the cause, the fix and WHO must act."""

    def test_a_missing_token_names_the_account_and_the_page(self):
        from rootcoz.engine.chat import credential_blocked_issue_notice

        assert credential_blocked_issue_notice("jira", "no_token") == (
            "No Jira issue can be filed from this chat. Add your Jira API token "
            "in Profile Settings."
        )

    def test_a_rejected_credential_names_the_re_save(self):
        from rootcoz.engine.chat import credential_blocked_issue_notice

        assert credential_blocked_issue_notice("jira", "unusable_credential") == (
            "Your saved Jira credential was rejected. Re-save it in Profile Settings."
        )

    def test_an_unconfigured_server_names_the_administrator(self):
        """Issue #536: the account's tokens were saved, the SERVER was
        unconfigured, and the notice said nothing at all — so the model told the
        user to fix his own settings."""
        from rootcoz.engine.chat import credential_blocked_issue_notice

        assert credential_blocked_issue_notice("jira", "server_unconfigured") == (
            "Jira is not configured on this server — an administrator has to set "
            "it up in Server Settings."
        )

    @pytest.mark.parametrize(
        "reason",
        [
            "server_disabled",
            "no_repository",
            "no_run_repository",
            "server_unconfigured",
            "unusable_credential",
            "no_token",
            "no_token_no_profile",
            "some_future_reason",
        ],
    )
    def test_no_unavailable_state_is_silent(self, reason):
        """Never silent: every reason a tracker is absent answers with a line."""
        from rootcoz.engine.chat import credential_blocked_issue_notice

        for tracker in ("github", "jira"):
            assert credential_blocked_issue_notice(tracker, reason)

    def test_only_an_available_tracker_or_an_unknown_one_is_silent(self):
        from rootcoz.engine.chat import credential_blocked_issue_notice

        # No reason means the tracker is available: nothing to correct.
        assert credential_blocked_issue_notice("jira", "") == ""
        # An unknown tracker has no kind to name.
        assert credential_blocked_issue_notice("", "no_token") == ""

    def test_the_server_side_sentences_never_send_the_user_to_a_page(self):
        """A server-side cause is fixed by an administrator: the text must not
        name any settings page, auth method or setting field."""
        from rootcoz.error_messages import unavailable_sentence

        forbidden = (
            "User Settings",
            "Profile Settings",
            "your settings",
            "basic",
            "pat",
            "oauth",
            "atst",
            "jira_cloud_url",
            "jira_server_url",
            "atlassian_oauth_client_id",
            "jira_email",
            "TESTS_REPO_URL",
        )
        for tracker in ("github", "jira"):
            for reason in (
                "server_unconfigured",
                "server_disabled",
                "no_repository",
                "no_run_repository",
                "some_future_reason",
            ):
                sentence = unavailable_sentence(tracker, reason)
                for word in forbidden:
                    assert word not in sentence, (reason, word)

    def test_the_account_side_sentences_name_profile_settings(self):
        from rootcoz.error_messages import unavailable_sentence

        for tracker in ("github", "jira"):
            for reason in ("no_token", "unusable_credential", "no_token_no_profile"):
                sentence = unavailable_sentence(tracker, reason)
                assert "Profile Settings" in sentence
                assert "your" in sentence.lower()


class TestTheServerWritesTheNoCredentialAnswer:
    """Issue #446: the model ignored the prompt, so the server states the cause.

    The text is stored as a ``system`` chat row under the user's request and
    above the answer, so the fix is present even when the model improvises.
    """

    async def _notice(
        self,
        message: str,
        tokens: dict[str, str],
        *,
        repo: bool = True,
        enabled: bool = True,
        profile: bool = True,
    ) -> str:
        """The text this turn would store, ``""`` when none — real gate included."""
        from rootcoz.config import Settings
        from rootcoz.main import _issue_blocked_notice

        with (
            patch("rootcoz.storage.get_user_tokens", AsyncMock(return_value=tokens)),
            patch("rootcoz.main._account_has_profile", AsyncMock(return_value=profile)),
        ):
            return await _issue_blocked_notice(
                message=message,
                username="alice",
                settings=Settings(enable_github_issues=enabled),
                github_repo_available=repo,
                job_chat=True,
            )

    async def test_a_github_request_with_no_token_names_profile_settings(self):
        notice = await self._notice("can you open a GitHub issue for this failure?", {})
        assert "Profile Settings" in notice
        assert "No GitHub issue can be filed" in notice
        # The cause is the account, so nothing may blame the tools.
        assert "tooling" not in notice.lower()

    async def test_a_saved_token_writes_nothing(self):
        assert await self._notice("open a GitHub issue", {"github_token": "tok"}) == ""
        # A token for the OTHER tracker does not make this one file either.
        assert "GitHub API token" in await self._notice(
            "open a GitHub issue", {"jira_token": "tok"}
        )

    async def test_an_unrelated_message_writes_nothing(self):
        assert await self._notice("why did this fail?", {}) == ""
        assert await self._notice("open the console log", {}) == ""

    async def test_a_tracker_the_server_disabled_names_the_administrator(self):
        """Never silent (#536): a disabled tracker is an administrator setting,
        so the notice names the administrator instead of saying nothing."""
        notice = await self._notice("open a GitHub issue", {}, enabled=False)
        assert "GitHub issue creation is disabled on this server" in notice
        assert "An administrator has to enable it in Server Settings" in notice
        assert "Profile Settings" not in notice

    async def test_no_repository_configured_names_the_missing_repository(self):
        """The gate reads the token BEFORE the destination, so it answers
        ``no_token`` on a deployment with no repository to file in either —
        where a token in Profile Settings would fix nothing at all. The notice
        now names that cause instead of staying silent."""
        notice = await self._notice("open a GitHub issue", {}, repo=False)
        assert "Nowhere to file this issue" in notice
        assert "TESTS_REPO_URL" not in notice
        assert "Profile Settings" not in notice
        # With a repository that would receive the issue, the token IS the fix.
        assert "Profile Settings" in await self._notice("open a GitHub issue", {})

    async def test_a_cloud_deployment_still_promises_the_token(self):
        """A bare probe token reads as a PAT, which needs ``jira_server_url``.
        On a Cloud-only deployment that shape cannot file, but the same token
        saved with an email is Cloud basic auth — so the notice must still
        promise the token instead of going silent."""
        from rootcoz.config import Settings
        from rootcoz.main import _issue_blocked_notice

        settings = Settings(
            jira_auth_mode="basic",
            jira_cloud_url="https://example.atlassian.net",
            jira_server_url="",
        )
        with (
            patch("rootcoz.storage.get_user_tokens", AsyncMock(return_value={})),
            patch("rootcoz.main._account_has_profile", AsyncMock(return_value=True)),
        ):
            notice = await _issue_blocked_notice(
                message="please create a Jira issue for this failure",
                username="alice",
                settings=settings,
                github_repo_available=True,
            )
        assert "Add your Jira API token in Profile Settings" in notice

    @pytest.mark.parametrize(
        "settings_kwargs",
        [
            # ATST scoped token only: no PAT and no basic auth can file there.
            {
                "jira_auth_mode": "atst",
                "atlassian_oauth_cloud_id": "cloud-1",
            },
            # OAuth 2.0 credential only.
            {
                "jira_auth_mode": "oauth",
                "atlassian_oauth_cloud_id": "cloud-1",
                "atlassian_oauth_client_id": "client-1",
            },
        ],
    )
    async def test_a_service_account_deployment_still_promises_the_token(
        self, settings_kwargs
    ):
        """The probe asks the same gate with every shape a Jira credential can
        be saved in, so a deployment that files only with a service-account
        credential (ATOA client secret, ATST scoped token) still names the fix
        for an account that has none."""
        from rootcoz.config import Settings
        from rootcoz.main import _issue_blocked_notice

        with (
            patch("rootcoz.storage.get_user_tokens", AsyncMock(return_value={})),
            patch("rootcoz.main._account_has_profile", AsyncMock(return_value=True)),
        ):
            notice = await _issue_blocked_notice(
                message="please create a Jira issue for this failure",
                username="alice",
                settings=Settings(**settings_kwargs),
                github_repo_available=True,
            )
        assert "Add your Jira API token in Profile Settings" in notice

    async def test_the_bootstrap_admin_is_told_to_sign_in(self):
        """No ``users`` row means ``PUT /api/user/tokens`` answers 404 for it."""
        notice = await self._notice(
            "please file a GitHub issue for this", {}, profile=False
        )
        assert "Sign in with your own account" in notice
        assert "Add your GitHub API token" not in notice
        assert "tooling" not in notice.lower()

    async def test_a_failed_lookup_costs_the_notice_not_the_turn(self):
        from rootcoz.config import Settings
        from rootcoz.main import _issue_blocked_notice

        with patch(
            "rootcoz.storage.get_user_tokens",
            AsyncMock(side_effect=RuntimeError("db is gone")),
        ):
            assert (
                await _issue_blocked_notice(
                    message="open a GitHub issue",
                    username="alice",
                    settings=Settings(enable_github_issues=True),
                    github_repo_available=True,
                    job_chat=True,
                )
                == ""
            )


def _unconfigured_jira_settings(**overrides) -> Settings:
    """A deployment with no Jira configuration, whatever the host exports.

    ``Settings`` reads ``JIRA_URL``/``JIRA_CLOUD_ID``/``ATLASSIAN_*`` from the
    environment (a dev box has them), so every Jira field this module reasons
    about is pinned explicitly.
    """
    base: dict[str, object] = {
        "jira_auth_mode": "",
        "jira_cloud_url": "",
        "jira_server_url": "",
        "jira_site_url": "",
        "jira_email": "",
        "jira_api_token": None,
        "jira_pat": None,
        "jira_oauth_client_secret": None,
        "jira_atst_token": None,
        "atlassian_oauth_client_id": "",
        "atlassian_oauth_cloud_id": "",
    }
    base.update(overrides)
    return Settings(**base)


class TestOneJiraStateOneAnswer:
    """Issue #536: one test per Jira state, through the real gate.

    The defect was a silent server: the notice returned "" exactly when the
    deployment was the cause, so the model improvised the fix and named the
    user's own settings while his tokens were saved.
    """

    async def _notice(self, settings, tokens, *, profile=True) -> str:
        from unittest.mock import AsyncMock, patch

        from rootcoz.main import _issue_blocked_notice

        with (
            patch("rootcoz.storage.get_user_tokens", AsyncMock(return_value=tokens)),
            patch("rootcoz.main._account_has_profile", AsyncMock(return_value=profile)),
        ):
            return await _issue_blocked_notice(
                message="please file a Jira issue for this failure",
                username="alice",
                settings=settings,
                github_repo_available=True,
            )

    async def test_an_unconfigured_server_answers_with_the_administrator(self):
        """The reporter's state: Jira unset on the server, the account's tokens
        saved. The server answers, and it names the administrator."""
        notice = await self._notice(
            _unconfigured_jira_settings(), {"jira_token": "tok"}
        )
        assert notice == (
            "Jira is not configured on this server — an administrator has to set "
            "it up in Server Settings."
        )
        assert "Profile Settings" not in notice
        assert "User Settings" not in notice

    async def test_a_working_deployment_with_no_token_answers_with_the_account(self):
        """A deployment that can file is the account's turn: adding the token is
        the fix, and nothing may send the user to the server settings."""
        settings = _unconfigured_jira_settings(
            jira_auth_mode="pat", jira_server_url="https://jira.example.com"
        )
        notice = await self._notice(settings, {})
        assert notice == (
            "No Jira issue can be filed from this chat. Add your Jira API token "
            "in Profile Settings."
        )
        assert "Server Settings" not in notice

    async def test_a_credential_the_deployment_cannot_serve_asks_for_a_re_save(self):
        """Partially configured: a PAT files, this account's saved credential
        does not pin anything the settings can serve."""
        settings = _unconfigured_jira_settings(
            jira_auth_mode="pat", jira_server_url="https://jira.example.com"
        )
        notice = await self._notice(
            settings, {"jira_token": "tok", "jira_email": "a@example.com"}
        )
        assert notice == (
            "Your saved Jira credential was rejected. Re-save it in Profile Settings."
        )

    async def test_the_bootstrap_admin_on_an_unconfigured_deployment(self):
        """No ``users`` row, and the cause is the server: the administrator
        sentence wins over the "sign in" one."""
        notice = await self._notice(_unconfigured_jira_settings(), {}, profile=False)
        assert "an administrator has to set it up in Server Settings" in notice
        assert "Sign in" not in notice

    async def test_a_job_chat_with_no_run_repository_names_the_run(self):
        from unittest.mock import AsyncMock, patch

        from rootcoz.config import Settings
        from rootcoz.main import _issue_blocked_notice

        with (
            patch("rootcoz.storage.get_user_tokens", AsyncMock(return_value={})),
            patch("rootcoz.main._account_has_profile", AsyncMock(return_value=True)),
        ):
            notice = await _issue_blocked_notice(
                message="open a GitHub issue for this failure",
                username="alice",
                settings=Settings(enable_github_issues=True),
                github_repo_available=False,
                job_chat=True,
            )
        assert "this run has no GitHub test repository" in notice
        assert "A token cannot add one" in notice

    async def test_an_admin_chat_without_a_server_repository_names_the_server(self):
        from unittest.mock import AsyncMock, patch

        from rootcoz.config import Settings
        from rootcoz.main import _issue_blocked_notice

        with (
            patch("rootcoz.storage.get_user_tokens", AsyncMock(return_value={})),
            patch("rootcoz.main._account_has_profile", AsyncMock(return_value=True)),
        ):
            notice = await _issue_blocked_notice(
                message="open a GitHub issue for this failure",
                username="alice",
                settings=Settings(enable_github_issues=True),
                github_repo_available=False,
                job_chat=False,
            )
        assert "this chat has no GitHub repository it may use" in notice
        assert "ask an administrator" in notice


class TestTheNoticeRendersAboveTheAnswer:
    """A correction must not arrive after the wrong answer it corrects (#446).

    Both routes store it with the turn: the reader meets the cause under their
    own request and above the placeholder the model fills in.
    """

    @pytest.mark.parametrize("admin", [False, True])
    async def test_the_notice_is_stored_before_the_placeholder(
        self, admin, monkeypatch, tmp_path
    ):
        from types import SimpleNamespace

        from fastapi import BackgroundTasks

        from rootcoz import main
        from rootcoz.config import Settings
        from rootcoz.models import ChatMessageRequest

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "notice-order.db")
        await storage.init_db()
        await storage.create_admin_user("alice")
        job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
        if not admin:
            await storage.save_result(
                job_id,
                "",
                "completed",
                {"status": "completed", "result": {}, "request_params": {}},
            )
        monkeypatch.setattr(
            main, "get_settings", lambda: Settings(enable_github_issues=True)
        )
        monkeypatch.setattr(
            main,
            "_settings_github_repo_available",
            lambda _settings: True,
        )
        monkeypatch.setattr(
            main,
            "_job_issue_repo_slugs",
            lambda _params: ("org/repo", frozenset({"org/repo"})),
        )
        monkeypatch.setattr(
            main,
            "_current_chat_choice",
            AsyncMock(
                return_value=({"ai_provider": "openai", "ai_model": "m"}, "user")
            ),
        )
        monkeypatch.setattr(
            main,
            "_normalize_and_validate_ai_params",
            AsyncMock(return_value=("openai", "m")),
        )
        monkeypatch.setattr(
            main, "_check_chat_session_choice", AsyncMock(return_value=("sid", "user"))
        )
        request = SimpleNamespace(
            state=SimpleNamespace(username="alice", is_admin=admin, role="admin")
        )
        body = ChatMessageRequest(message="open a GitHub issue for this failure")
        sent = await (
            main.send_admin_chat_message(body, request, BackgroundTasks())
            if admin
            else main.send_chat_message(job_id, body, request, BackgroundTasks())
        )

        rows = await storage.get_chat_messages(job_id, username="alice")
        assert [row["role"] for row in rows] == ["user", "system", "assistant"]
        notice = rows[1]
        # The row the model never writes comes before the placeholder it fills.
        assert notice["id"] < sent["assistant_message_id"]
        assert notice["id"] > rows[0]["id"]
        assert "Add your GitHub API token in Profile Settings" in notice["content"]

    @pytest.mark.parametrize(
        ("tokens", "settings_kwargs", "expected"),
        [
            # The reporter's state: unconfigured server, tokens saved.
            ({"jira_token": "tok"}, {}, "an administrator"),
            # A deployment that can file, no token on the account.
            (
                {"jira_token": ""},
                {"jira_auth_mode": "pat", "jira_server_url": "https://j.example.com"},
                "Add your Jira API token",
            ),
            # And a credential the deployment cannot serve.
            (
                {"jira_token": "tok", "jira_email": "a@example.com"},
                {"jira_auth_mode": "pat", "jira_server_url": "https://j.example.com"},
                "Re-save it in Profile Settings",
            ),
        ],
    )
    async def test_every_jira_state_stores_a_system_row(
        self, tokens, settings_kwargs, expected, monkeypatch, tmp_path
    ):
        """All three states answer, in the role the model never writes."""
        from types import SimpleNamespace

        from fastapi import BackgroundTasks

        from rootcoz import main
        from rootcoz.models import ChatMessageRequest

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "notice-states.db")
        await storage.init_db()
        await storage.create_admin_user("alice")
        await storage.save_result(
            "job",
            "",
            "completed",
            {"status": "completed", "result": {}, "request_params": {}},
        )
        monkeypatch.setattr(
            main, "get_settings", lambda: _unconfigured_jira_settings(**settings_kwargs)
        )
        monkeypatch.setattr(
            main, "_settings_github_repo_available", lambda _settings: True
        )
        monkeypatch.setattr(
            main,
            "_job_issue_repo_slugs",
            lambda _params: ("org/repo", frozenset({"org/repo"})),
        )
        monkeypatch.setattr(
            main,
            "_current_chat_choice",
            AsyncMock(
                return_value=({"ai_provider": "openai", "ai_model": "m"}, "user")
            ),
        )
        monkeypatch.setattr(
            main,
            "_normalize_and_validate_ai_params",
            AsyncMock(return_value=("openai", "m")),
        )
        monkeypatch.setattr(
            main, "_check_chat_session_choice", AsyncMock(return_value=("sid", "user"))
        )
        monkeypatch.setattr(
            "rootcoz.storage.get_user_tokens", AsyncMock(return_value=tokens)
        )
        request = SimpleNamespace(
            state=SimpleNamespace(username="alice", is_admin=False, role="admin")
        )
        body = ChatMessageRequest(message="please file a Jira issue for this failure")
        await main.send_chat_message("job", body, request, BackgroundTasks())

        rows = await storage.get_chat_messages("job", username="alice")
        assert [row["role"] for row in rows] == ["user", "system", "assistant"]
        assert expected in rows[1]["content"]
        assert "User Settings" not in rows[1]["content"]


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
        # Both actors, and never a page that cannot fix a server-side cause.
        assert "Profile Settings" in prompt
        assert "Server Settings" in prompt
        assert "User Settings" not in prompt
        assert "(URL, email, and API token)" not in prompt
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
        assert "Profile Settings" in prompt
        assert "administrator" in prompt
        assert "User Settings" not in prompt
        assert "Jira search" not in prompt  # Jira is configured

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

    def test_a_symlinked_workspace_is_never_walked_into(self, tmp_path):
        """A link planted at the workspace path must not delete a live workspace.

        The shipped delete derives the path from the job id and then walks it, so
        a link swapped in after the caller's checks would delete the *target's*
        contents. A workspace path is never a link, so the walk refuses it.
        """
        from rootcoz.engine.chat import cleanup_chat_workspace

        live = tmp_path / "live"
        (live / "repo").mkdir(parents=True)
        (live / "sessions.jsonl").write_text("keep me")
        planted = tmp_path / "rootcoz-chat-orphan"
        planted.symlink_to(live)

        with patch("rootcoz.engine.chat.get_chat_workspace", return_value=planted):
            assert cleanup_chat_workspace("orphan") == (0, 0, 0)

        assert (live / "sessions.jsonl").read_text() == "keep me"
        assert (live / "repo").is_dir()

    def test_cleanup_counts_the_loose_files_it_unlinks(self, tmp_path):
        """``reclaimed=`` must include the regular files this cleanup really unlinked."""
        from rootcoz.engine.chat import cleanup_chat_repos

        (tmp_path / "console-output.txt").write_text("x" * 64)

        with patch("rootcoz.engine.chat.get_chat_workspace", return_value=tmp_path):
            nbytes, entries, failed = cleanup_chat_repos("job-count")

        assert (nbytes, entries, failed) == (64, 1, 0)

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
        from rootcoz.engine import chat
        from rootcoz.engine.chat import cleanup_chat_workspace

        custom_tmp = tmp_path / "custom-tmp"
        custom_tmp.mkdir()
        monkeypatch.setenv("TMPDIR", str(custom_tmp))
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(custom_tmp))
        # The shared conftest fixture replaces ``chat._tmp_root`` outright, so the
        # containment check would not see the TMPDIR patch without this seam.
        monkeypatch.setattr(chat, "_tmp_root", lambda: custom_tmp.resolve())

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
    async def test_undeletable_symlink_is_counted_not_raised(
        self, tmp_path, monkeypatch
    ):
        """An EACCES on one symlink must not abort the workspace cleanup (#424)."""
        import errno
        from functools import partial

        from rootcoz.engine import chat
        from rootcoz.temp_paths import (
            CleanupOutcome,
            PathStateUnknown,
            cleanup_path_off_loop,
            delete_marker,
        )

        workspace = tmp_path / "rootcoz-chat-job-eacces"
        workspace.mkdir()
        (workspace / "note.txt").write_text("x")
        link = workspace / "build-artifacts"
        link.symlink_to(tmp_path / "artifact-target")
        real_unlink = os.unlink

        def refuse_link(path, *args, **kwargs):
            if Path(os.fspath(path)).name == link.name:
                raise PermissionError(
                    errno.EACCES, "Permission denied", os.fspath(path)
                )
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", refuse_link)
        monkeypatch.setattr(chat, "get_chat_workspace", lambda *a, **k: workspace)

        # A survivor is folded into the result, so the step itself completes.
        result = chat.cleanup_chat_workspace("job-eacces")
        assert result.failed >= 1

        outcome = await cleanup_path_off_loop(
            "job-eacces", workspace, partial(chat.cleanup_chat_workspace, "job-eacces")
        )
        assert outcome is CleanupOutcome.INCOMPLETE
        assert not (workspace / "note.txt").exists(), "the tree delete never ran"
        assert delete_marker(workspace).exists(), "a half-deleted tree must stay marked"
        with pytest.raises(PathStateUnknown):
            async with chat.chat_workspace_lock("job-eacces"):
                pass


@pytest.mark.asyncio
async def test_cleanup_deleted_job_offloads_blocking_cleanup(monkeypatch):
    """Filesystem MCP locks must not run on the event loop."""
    import asyncio

    from rootcoz import main as main_mod

    seen: list[str] = []

    def fake_cleanup(job_id: str, username: str = "") -> None:
        # No running loop here proves the cleanup is off the loop; patching
        # ``to_thread`` would only prove which helper was used.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        seen.append(f"cleanup:{job_id}")

    monkeypatch.setattr("rootcoz.engine.chat.cleanup_chat_workspace", fake_cleanup)
    main_mod._chat_jobs_deleting.clear()
    await main_mod._cleanup_deleted_job_chat_workspaces("job-offload")
    assert seen == ["cleanup:job-offload"]
    assert "job-offload" not in main_mod._chat_jobs_deleting
    assert "job-offload" not in main_mod._chat_job_barriers


@pytest.mark.asyncio
async def test_chat_routes_share_one_lock_scope_and_translation(monkeypatch) -> None:
    """One lock scope and one ``PathStateUnknown`` translation, not four (#424).

    The two init routes and the two message handlers differ only in the vehicle
    that tells the user, so a change to the lock scope or the translation has to
    land in one place.
    """
    import inspect
    from contextlib import asynccontextmanager

    from rootcoz import main as main_mod
    from rootcoz.temp_paths import PathStateUnknown

    source = inspect.getsource(main_mod)
    assert source.count("async with chat_workspace_lock(") == 1
    assert source.count("except PathStateUnknown") == 1
    for route in (
        main_mod._init_chat_under_barrier,
        main_mod._process_chat_message,
        main_mod.init_admin_chat,
        main_mod._process_admin_chat_message,
    ):
        assert "_enter_chat_workspace(" in inspect.getsource(route), route.__name__

    @asynccontextmanager
    async def unknown(job_id, username=""):
        raise PathStateUnknown("half-deleted")
        yield  # pragma: no cover - the lock never yields

    monkeypatch.setattr("rootcoz.engine.chat.chat_workspace_lock", unknown)
    assert await main_mod._enter_chat_workspace("job-1", "alice", "Chat init") is None


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
async def test_process_chat_answers_when_the_workspace_lock_outlives_the_clock(
    setup_test_db, tmp_path, monkeypatch
):
    """A contended graph lock costs the map, not the reply (#511)."""
    from rootcoz import main as main_mod
    from rootcoz.engine import graft

    workspace = tmp_path / "chat-workspace"
    (workspace / "repo" / ".git").mkdir(parents=True)

    def contended(*_args, **_kwargs):
        raise TimeoutError("workspace lock deadline")

    monkeypatch.setattr(graft, "roots_needing_index", contended)
    monkeypatch.setattr(
        "rootcoz.engine.chat.ensure_chat_workspace", lambda *a, **k: workspace
    )
    monkeypatch.setattr(
        "rootcoz.engine.core.copy_rootcoz_pi_resources", lambda *a: None
    )
    monkeypatch.setattr(
        "rootcoz.engine.chat.clone_chat_repos", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "rootcoz.engine.chat.chat_with_ai", AsyncMock(return_value=(True, "ok", "sess"))
    )
    monkeypatch.setattr(
        "rootcoz.engine.session_tools.install_http_tools_mcp_best_effort_async",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.graph_http_tools", lambda *a, **k: []
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
    job_id = "chat-lock-deadline"
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
            "request_params": {},
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

    assert await storage.get_chat_message_status(assistant_id) == "completed"


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
    # build is needed, so the observable is the build itself, not the call. The
    # turn only schedules it, so the test waits for the build it started.

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
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.graph_http_tools", lambda *a, **k: []
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
    from rootcoz.engine import session_tools

    scheduled = list(session_tools._BACKGROUND_INDEX_TASKS)
    assert scheduled, "the turn never scheduled an index"
    await asyncio.gather(*scheduled)
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
async def test_chat_init_prepares_outside_the_job_barrier(setup_test_db, monkeypatch):
    """Init never holds the job barrier; deletion reaches it by cancellation.

    The barrier is held by a message turn across its own clone phase, so init
    taking it would put the router timeout back on Start. Deletion cancels the
    preparation instead of waiting for a barrier it does not hold.
    """
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


#: A test's own ceiling on the wait for preparation: generous against the 10 s that
#: used to flake, short enough that a genuinely hung preparation fails the suite in a
#: minute. It is deliberately not ``CHAT_WAIT_SECONDS`` -- that is the CLI's 600 s of
#: production patience, and a unit test that spends ten wall-clock minutes to prove a
#: timeout is broken itself. The timeout tests inject a fake clock and spend none of it.
_CHAT_PREP_WAIT_CEILING_SECONDS = 60.0
_CHAT_PREP_POLL_SECONDS = 0.01


def _await_preparation(
    test_client,
    path: str,
    timeout: float = _CHAT_PREP_WAIT_CEILING_SECONDS,
    headers: dict[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll history until the background preparation has ended.

    This file drives Start through the sync ``TestClient``, so it cannot await a
    task created on the portal's loop: the server's own state is the signal. The
    phase is per user, so the caller's own headers decide whose preparation ends.

    Elapsed time comes from ``clock`` (``monotonic``, like the deadlines in ``main.py``)
    and each poll waits on ``sleep``. Both are injected so a timeout test can pass the
    ``AdvancingClock`` pair from ``tests.conftest`` and simulate the wait instead of
    spending it; the default ceiling is a test bound, not the CLI's patience, because
    no test may burn ``CHAT_WAIT_SECONDS`` of wall clock to prove a deadline.

    The bound is checked on every poll, whatever the phase label does. When it expires,
    the message names the last phase the server published and how long it has been
    there -- a reading, not a verdict.
    """
    started = clock()
    seen: dict[str, float] = {}  # distinct labels in order, with the moment first seen
    last = ""
    last_at = started
    while True:
        observed = test_client.get(path, headers=headers).json()["preparing"]
        if observed is None:
            return
        phase = observed.get("phase") or "?"
        label = f"{phase} ({observed['detail']})" if observed.get("detail") else phase
        now = clock()
        if label != last:
            last, last_at = label, now
        seen.setdefault(label, now)
        if now - started >= timeout:
            raise AssertionError(
                f"the chat preparation for {path} did not finish within "
                f"{timeout:.0f} s (last phase: {label}, unchanged for "
                f"{now - last_at:.0f} s); phases seen: {' -> '.join(seen)}"
            )
        sleep(_CHAT_PREP_POLL_SECONDS)


class _ScriptedChat:
    """A history client replaying scripted ``preparing`` states, counting calls.

    ``states`` is a list (read in order, ``None`` past the end) or a callable taking
    the call number. A poller that never gives up fails the test instead of hanging it.
    """

    def __init__(self, states) -> None:
        self._states = states
        self.calls = 0

    def get(self, path: str, headers: dict[str, str] | None = None) -> SimpleNamespace:
        self.calls += 1
        if self.calls > 500:
            raise AssertionError("the preparation helper never stopped polling")
        if callable(self._states):
            state = self._states(self.calls)
        else:
            state = (
                self._states[self.calls - 1]
                if self.calls <= len(self._states)
                else None
            )
        return SimpleNamespace(json=lambda: {"preparing": state})


def test_a_slow_preparation_is_awaited() -> None:
    """A label held far past the old 10 s passes: the bound is a minute, not ten.

    One poll = one ``sleep(_CHAT_PREP_POLL_SECONDS)`` = 1 fake second, so the single
    label dwells 40 s -- four times the old 10 s, still under the 60 s ceiling. The fake
    clock keeps the wait simulated, so the test costs no wall clock.
    """
    fake = AdvancingClock(step=1.0)
    client = _ScriptedChat([{"phase": "cloning", "detail": "tests-repo"}] * 40 + [None])

    _await_preparation(client, "/api/chat/job", clock=fake.monotonic, sleep=fake.sleep)

    assert client.calls == 41  # 40 label polls (40 s) plus the clearing one


def test_a_preparation_that_never_ends_reports_the_last_phase_and_elapsed() -> None:
    """A preparation that never ends stops at the ceiling and reports what it saw.

    The label changes on every poll, so the bound firing proves it is checked
    unconditionally rather than only on a repeating label. Ten fake seconds per poll
    reach the 60 s ceiling in seven polls and no wall clock; the real-time assertion is
    the regression guard -- put a real ``sleep`` back in the poller and it fails here
    instead of quietly costing the suite a minute per run.
    """
    fake = AdvancingClock(step=10.0)
    client = _ScriptedChat(
        lambda call: {"phase": "cloning", "detail": f"repo-{'a' if call % 2 else 'b'}"}
    )

    real_started = time.monotonic()
    with pytest.raises(AssertionError) as excinfo:
        _await_preparation(
            client, "/api/chat/job", clock=fake.monotonic, sleep=fake.sleep
        )
    wall_clock = time.monotonic() - real_started

    assert wall_clock < 1.0, f"the timeout path spent {wall_clock:.1f} s of real time"
    message = str(excinfo.value)
    assert "did not finish within 60 s" in message
    assert "last phase: cloning (repo-a)" in message
    assert "phases seen: cloning (repo-a) -> cloning (repo-b)" in message
    assert "never ended" not in message
    assert client.calls == 7  # t = 60 s on the 7th poll


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
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ready"] is False
    assert body["preparing"]["phase"] == "preparing"
    assert "session_id" not in body
    _await_preparation(test_client, f"/api/chat/{job_id}")
    assert test_client.get(f"/api/chat/{job_id}").json()["active_session"] is not None


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

    async def test_init_names_the_workspace_it_could_not_establish(
        self, test_client, temp_db_path: Path, tmp_path, monkeypatch
    ):
        """A leftover workspace must be reported, not raised as a bare 500 (#424).

        ``reusable_path_lock`` raises ``PathStateUnknown`` when a marked workspace
        is still owned by a live builder or a delete left paths behind. Init used
        to let that escape as an unhandled server error, so the user learned
        nothing about the leftover tree that blocks them.
        """
        from rootcoz.engine import chat
        from rootcoz.temp_paths import PathStateUnknown

        await _save_job(temp_db_path, "chat-blocked-job")
        workspace = tmp_path / "rootcoz-chat-chat-blocked-job" / "admin"
        monkeypatch.setattr(chat, "get_chat_workspace", lambda *a, **k: workspace)

        def blocked(*_args, **_kwargs):
            raise PathStateUnknown("workspace could not be established as whole")

        monkeypatch.setattr(chat, "chat_workspace_lock", blocked)

        response = test_client.post(
            "/api/chat/chat-blocked-job/init",
            json={"ai_provider": "claude", "ai_model": "sonnet-4"},
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert str(workspace) in detail, "the error does not name the blocked workspace"
        assert "try again" in detail.lower(), "the error says nothing the user can do"
        assert "administrator" in detail.lower(), detail

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
        # An unmatched provider text is never echoed back to the user (it can be
        # a whole response body): the mapper's generic sentence is the output.
        assert (
            assistant_msgs[-1]["content"] == "Error: AI call failed. Please try again."
        )
        assert "AI CLI timed out" not in assistant_msgs[-1]["content"]

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

    def test_artifact_cleanup_reports_paths_it_could_not_delete(
        self, tmp_path, monkeypatch, caplog
    ):
        """``delete_tree`` returns its failures: report them, do not claim success."""
        from rootcoz import main as main_mod
        from rootcoz.temp_paths import DeleteResult

        artifacts = tmp_path / "alice"
        artifacts.mkdir()
        monkeypatch.setattr(main_mod, "_get_admin_artifacts_dir", lambda u: artifacts)
        monkeypatch.setattr(
            main_mod, "delete_tree", lambda path: DeleteResult(0, 0, failed=2)
        )

        with capture_main_logs(caplog, logging.INFO):
            result = main_mod._cleanup_admin_artifacts("alice")

        assert result.failed == 2
        assert "2 artifact path(s) for alice survived their delete" in caplog.text
        assert "cleaned up artifacts" not in caplog.text, (
            "a partial delete was logged as a completed cleanup"
        )

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
    assert response.status_code == (200 if admin else 202), response.text
    if not admin:
        _await_preparation(test_client, path, headers=headers)
    assert (
        test_client.get(path, headers=headers).json()["active_session"][
            "credential_source"
        ]
        == "server"
    )


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("raises", [False, True])
async def test_init_without_session_is_a_failed_row(
    test_client, temp_db_path, admin, raises
):
    job_id = "failed-start-job" if not admin else "__admin_chat__"
    path = "/api/admin/chat" if admin else f"/api/chat/{job_id}"
    if not admin:
        await _save_job(temp_db_path, job_id)
    from rootcoz import main as main_mod

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
        patch("rootcoz.main.notify_chat_changed") as notify,
    ):
        if admin:
            # Admin chat init is still synchronous: its own contract is unchanged.
            if raises:
                with pytest.raises(RuntimeError, match="session init failed"):
                    test_client.post(
                        f"{path}/init",
                        json={"ai_provider": "claude", "ai_model": "sonnet-4"},
                    )
            else:
                response = test_client.post(
                    f"{path}/init",
                    json={"ai_provider": "claude", "ai_model": "sonnet-4"},
                )
                assert response.status_code == 200, response.text
                assert response.json()["session_id"] == ""
            notify.assert_not_called()
        else:
            # A background preparation never answers with an exception: a Start
            # that produced nothing is one failed row, and the browser is told.
            response = test_client.post(
                f"{path}/init",
                json={"ai_provider": "claude", "ai_model": "sonnet-4"},
            )
            assert response.status_code == 202, response.text
            _await_preparation(test_client, path)
            notify.assert_called()
    history_dbg = test_client.get(path).json()
    assert history_dbg["active_session"] is None
    if not admin:
        failed = [m for m in history_dbg["messages"] if m["status"] == "failed"]
        assert [m["content"] for m in failed] == [main_mod._CHAT_PREP_FAILED_DETAIL]


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
        assert first.status_code == (200 if admin else 202), first.text
        assert first.json()["ready"] is bool(admin)
        if not admin:
            _await_preparation(test_client, path)
        # Preparation phases notify too; every notify stays scoped to the user.
        assert notify.call_args_list
        assert all(c == call(job_id, username="admin") for c in notify.call_args_list)
        assert await storage.get_latest_chat_session(job_id, "admin") is not None
        notifies_before_retry = notify.call_count
        second = test_client.post(f"{path}/init", json=body)
        assert second.status_code == (200 if admin else 202), second.text
        if not admin:
            _await_preparation(test_client, path)
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

    async def test_a_legacy_duplicate_setting_is_refused_with_the_names(
        self, test_client, temp_db_path: Path, github_issue_config
    ):
        """#402: a row stored before PUT validated it is a 422, never a 500.

        The run stored no repos of its own, so the allow-list falls back to the
        server setting — and that setting cannot be parsed.
        """
        from rootcoz.config import update_db_settings_cache

        job_id = await self._save_run(
            temp_db_path,
            "job-repo-duplicate",
            {
                "job_name": "job",
                "build_number": 1,
                "tests_repo_url": "https://github.com/org/tests",
            },
        )
        update_db_settings_cache(
            {
                "additional_repos": (
                    "shared-infra:https://example.com/a/shared-infra,"
                    "shared-infra:https://example.com/b/shared-infra"
                )
            }
        )
        headers = await self._reviewer("duplicate-filer")
        response = await self._draft(test_client, headers, job_id=job_id)
        assert response.status_code == 422, response.text
        assert (
            "Duplicate additional repo names: shared-infra" in response.json()["detail"]
        )

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

    @pytest.mark.parametrize(
        "bad",
        [
            "https://github.com/org/tests",
            "tests",
            "owner/..",
            "owner/.",
            "../..",
            "üñî/çødé",
            "--/--",
        ],
    )
    async def test_a_malformed_repo_is_refused_as_a_url_or_a_bare_name(
        self, test_client, github_issue_config, bad: str
    ):
        """Not a 422: the detail is the string the model relays to the user.

        A slug is what GitHub accepts — ``[A-Za-z0-9._-]`` with neither a dot
        component nor a leading ``-`` — so a dot path (``owner/..``) and a
        non-ASCII owner are refused here rather than sent on to the tracker.
        """
        headers = await self._reviewer("slug")
        response = test_client.post(
            "/api/chat-issue/propose",
            json={"target": "github", "title": "t", "body": "b", "repo": bad},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert "'owner/name'" in response.json()["detail"]

    @pytest.mark.parametrize("repo", ["any/where", "my-org/rootcoz.ci-tools"])
    async def test_an_admin_may_name_any_repo(
        self, test_client, temp_db_path: Path, github_issue_config, repo: str
    ):
        """Every GitHub-shaped slug is accepted: dots and dashes included."""
        admin, api_key = await storage.create_admin_user("repo-admin")
        await storage.save_user_tokens(
            admin,
            github_token="ghp_row_token",  # pragma: allowlist secret
        )
        headers = {"Authorization": f"Bearer {api_key}"}
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": f"https://github.com/{repo}/issues/4",
                "number": 4,
            }
            response = await self._file(test_client, headers, repo=repo)
        assert response.status_code == 201, response.text
        assert self._filed_repo(mock_create) == f"https://github.com/{repo}"

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


class TestChatToolFingerprint:
    """The tool fingerprint a resumed chat session must match.

    Chat indexes after the session starts (#535), so the code-graph tools exist
    only from the next turn on. A resumed sidecar session keeps its creation-time
    tools, so the fingerprint gains ``code-graph`` once graph tools are present —
    that mismatch is what recreates the session and makes them reachable.
    """

    _GITHUB_TOOL: ClassVar[dict[str, Any]] = {
        "name": "create_github_issue",
        "description": "file an issue",
    }
    _GRAPH_TOOL: ClassVar[dict[str, Any]] = {
        "name": "graft_find_code",
        "description": "search the graph",
    }

    def _fingerprint(
        self, targets: tuple[str, ...], tools: list[dict[str, Any]]
    ) -> tuple[str, ...]:
        from rootcoz.main import _chat_tool_fingerprint

        return _chat_tool_fingerprint(targets, tools)

    def test_graph_tools_add_the_graph_entry(self):
        assert self._fingerprint(("github",), [self._GRAPH_TOOL]) == (
            "github",
            "code-graph",
        )

    def test_no_graph_tools_leave_the_fingerprint_alone(self):
        assert self._fingerprint(("github",), [self._GITHUB_TOOL]) == ("github",)
        assert self._fingerprint((), []) == ()

    def test_the_same_inputs_give_the_same_fingerprint(self):
        """Stability matters: an unstable fingerprint restarts every turn."""
        for tools in ([self._GITHUB_TOOL], [self._GITHUB_TOOL, self._GRAPH_TOOL]):
            assert self._fingerprint(("github", "jira"), tools) == self._fingerprint(
                ("github", "jira"), tools
            )

    def test_gaining_the_graph_changes_the_fingerprint(self):
        """The no-graph -> graph change is the session-recreation mechanism."""
        before = self._fingerprint(("github",), [self._GITHUB_TOOL])
        after = self._fingerprint(("github",), [self._GITHUB_TOOL, self._GRAPH_TOOL])
        assert before != after

        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-graph", before)
        assert main_mod._enable_tools_for_session("sid-graph", after) is None

    def test_an_inconclusive_peek_keeps_the_graph_session(self):
        """A build owning the workspace reads as no tools, not as a lost graph.

        Rebuilding for that replays the whole history once the build finishes and
        tears the session down again on the next turn, which is churn nobody asked
        for — and the graph tools report their own freshness anyway.
        """
        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-graph", ("github", "code-graph"))

        assert (
            main_mod._enable_tools_for_session("sid-graph", ("github",)) == "sid-graph"
        )

    def test_a_tracker_change_is_not_excused_by_the_inconclusive_peek(self):
        """The peek excuses the graph entry only, not the trackers beside it.

        A build holding the workspace hides the graph tools for a turn; that must
        not also hide a tracker the user enabled meanwhile, which would resume the
        session with the old set for as long as the build runs.
        """
        from rootcoz import main as main_mod

        main_mod._remember_chat_session_tools("sid-graph", ("github", "code-graph"))

        assert (
            main_mod._enable_tools_for_session("sid-graph", ("github", "jira")) is None
        )

    def test_targets_still_participate(self):
        """Not a parallel mechanism: the tracker targets stay in the fingerprint."""
        assert self._fingerprint(("github",), [self._GRAPH_TOOL]) != self._fingerprint(
            ("github", "jira"), [self._GRAPH_TOOL]
        )
        assert self._fingerprint(("github",), [self._GITHUB_TOOL]) != self._fingerprint(
            ("github", "jira"), [self._GITHUB_TOOL]
        )


# -- One refusal table, three surfaces (#423) ------------------------------
#
# A session refusal is a sentence rootcoz constructs, from the sidecar's three
# static key-free literals or from its own cause text. The start-time 422, the
# send-time mapper and the message content the send path persists must carry
# the same bytes, and no path may echo the body it was handed.

_LIMITS_LITERAL = (
    "Model metadata missing reliable input/output token limits for unknown model. "
    "Declare the model in the agent-dir models.json with contextWindow and maxTokens "
    "to use it with a session API key."
)
_LIMITS_SENTENCE = f"AI session refused: {_LIMITS_LITERAL}"
_MODEL_LITERAL = "Invalid model for session API key: [REDACTED]"
_PROVIDER_LITERAL = "Selected provider does not support session API key authentication"
_NEUTRAL_401 = (
    "AI session refused: the provider rejected the AI credential for openai/undeclared."
    " Check that the credential is valid for that provider, or ask an administrator to"
    " verify the provider key on the server."
)
_BANNED_WORDS = ("your own", "re-enter", "your", "the server's", "rotate")


def _generic_sentence(status: int) -> str:
    return (
        f"AI session refused: the session could not be created for openai/undeclared"
        f" (sidecar HTTP {status}). Ask an administrator to check the server log for the"
        " sidecar's reason."
    )


def _chat_init_request():
    return SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )


async def _refused_init(
    install_start_rig, refusing_sidecar, *, status_code, json_body=None, raw_text=""
):
    """Drive POST /api/chat/{job}/init with a sidecar that refuses.

    The start-time sentence used to be a ``422`` detail. Preparation is a
    background task now, so the same bytes reach the user as one failed chat row —
    the surface moved, the single owner of the sentence did not.
    """
    from rootcoz import main

    client = refusing_sidecar(status_code, json_body=json_body, raw_text=raw_text)
    await install_start_rig(
        credential_source="server", client=client, job_id="refusal-job", admin=False
    )
    response = await main.init_chat(
        "refusal-job",
        _chat_init_request(),
        ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
    )
    assert response["ready"] is False
    entry = main._chat_prep_tasks.get("refusal-job:alice")
    if entry is not None:
        await asyncio.gather(entry.task, return_exceptions=True)
    rows = [
        m
        for m in await storage.get_chat_messages("refusal-job", username="alice")
        if m["status"] == "failed"
    ]
    assert len(rows) == 1, rows
    return rows[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", [_MODEL_LITERAL, _PROVIDER_LITERAL])
async def test_non_limit_4xx_gets_its_own_sentence(
    install_start_rig, refusing_sidecar, literal
):
    """Each non-limits whitelisted literal is forwarded byte for byte.

    A whitelist holding only the limits literal (or a generic sentence for these
    causes) fails here: the detail must equal marker + the sidecar's own words.
    """
    refusal = await _refused_init(
        install_start_rig,
        refusing_sidecar,
        status_code=400,
        json_body={"error": literal},
    )
    assert refusal == f"AI session refused: {literal}"
    assert refusal != f"AI session refused: {_LIMITS_LITERAL}"
    assert "models.json" not in refusal


@pytest.mark.asyncio
async def test_cursor_session_failure_keeps_its_remediation(refusal_secret_key):
    """A Cursor ``/sessions`` 400 keeps the ``agent login`` remediation."""
    from rootcoz import ai_client

    detail = f"sidecar said no for {refusal_secret_key}"
    send = ai_client.format_chat_ai_user_error(
        f"Sidecar session creation failed (HTTP 400): {detail}",
        is_admin=True,
        ai_provider="cursor",
        ai_model="composer",
    )
    start = ai_client._session_refusal(400, detail, "cursor", "composer")
    for message in (send, start):
        assert "agent login" in message
        assert "CURSOR_API_KEY" in message
        assert refusal_secret_key not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "raw_text", "expected"),
    [
        (401, "upstream rejected the key", _NEUTRAL_401),
        (403, "upstream rejected the key", _NEUTRAL_401),
        (418, "upstream is a teapot", "generic"),
    ],
)
async def test_401_and_403_get_the_source_neutral_sentence(
    install_start_rig,
    refusing_sidecar,
    refusal_secret_key,
    status_code,
    raw_text,
    expected,
):
    """A rejected credential gets one source-neutral sentence; other 4xx generic.

    The body carries a fake key and the limits words outside the whitelisted
    literal, and neither may reach the detail — an echo mapper cannot hide them.
    """
    body = f"{raw_text} {refusal_secret_key} Model metadata missing"
    refusal = await _refused_init(
        install_start_rig, refusing_sidecar, status_code=status_code, raw_text=body
    )
    sentence = refusal
    if expected == "generic":
        assert sentence == _generic_sentence(status_code)
    else:
        assert sentence == expected
    assert refusal_secret_key not in sentence
    assert "Model metadata missing" not in sentence
    assert "upstream" not in sentence
    for banned in _BANNED_WORDS:
        assert banned not in sentence


@pytest.mark.parametrize(
    ("fixture_text", "expected"),
    [
        # The shapes call_ai returns on send (the library envelope), and the
        # keyless httpx shape the analysis path sees.
        (
            (
                "Sidecar session creation failed (HTTP 401): rejected sk-fake Model"
                " metadata missing"
            ),
            _NEUTRAL_401,
        ),
        (
            f"Sidecar session creation failed (HTTP 400): {_LIMITS_LITERAL}",
            _LIMITS_SENTENCE,
        ),
        (
            "Client error '401 Unauthorized' for url 'http://127.0.0.1:9100/sessions'",
            _NEUTRAL_401,
        ),
        (
            "Client error '400 Bad Request' for url 'http://127.0.0.1:9100/sessions'",
            _generic_sentence(400),
        ),
    ],
)
def test_credential_refusal_is_source_neutral_everywhere(
    monkeypatch, fixture_text, expected
):
    """One sentence per cause, on both surfaces, and never the raw text.

    The start-time sentence is built by ``_session_refusal`` and the send-time
    one by the mapper; both must answer the same bytes for the same fixture.
    Each parameter also names the leak it rules out.
    """
    from rootcoz import ai_client

    send = ai_client.format_chat_ai_user_error(
        fixture_text, is_admin=False, ai_provider="openai", ai_model="undeclared"
    )
    start = ai_client._session_refusal(
        *ai_client._sidecar_failure_parts(text=fixture_text),
        "openai",
        "undeclared",
    )
    assert send == expected
    assert start == expected
    assert send != fixture_text
    assert "Sidecar session creation failed" not in send
    for banned in _BANNED_WORDS:
        assert banned not in send
    if "sk-fake" in fixture_text:
        assert "sk-fake" not in send
        assert "Model metadata missing" not in send
    if "Client error" in fixture_text:
        assert "Client error" not in send
        assert "/sessions" not in send


@pytest.mark.parametrize(
    "fixture_text",
    [
        _LIMITS_SENTENCE,
        f"Sidecar session creation failed (HTTP 400): {_LIMITS_LITERAL}",
    ],
)
def test_send_after_lost_session_names_the_fix(fixture_text):
    """A refusal met on send names the fix, not the raw library body.

    The marker sentence (a session that expired between start and send) and the
    library envelope (what ``call_ai`` returns) must both resolve to the limits
    sentence; the httpx keyless shape carries no body, so it never gets here.
    """
    from rootcoz import ai_client

    message = ai_client.format_chat_ai_user_error(
        fixture_text, is_admin=False, ai_provider="openai", ai_model="undeclared"
    )
    assert message == _LIMITS_SENTENCE
    if not fixture_text.startswith(ai_client.SESSION_REFUSAL_MARKER):
        assert message != fixture_text
    assert "Sidecar session creation failed" not in message


@pytest.mark.asyncio
async def test_send_time_sentence_is_byte_equal_to_the_refusal_message(
    install_start_rig, refusing_sidecar, test_client, temp_db_path
):
    """The same cause on all three surfaces carries the same bytes.

    (a) the assistant message content the send route persists, (b) the mapper's
    returned string, and (c) the start-time failed row — one drive each,
    because ``init`` stores no message and ``chat_with_ai`` drops the result.
    """
    from rootcoz import ai_client

    library_text = f"Sidecar session creation failed (HTTP 400): {_LIMITS_LITERAL}"
    await _save_job(temp_db_path, "refusal-send-job")
    await _start_chat(test_client, "refusal-send-job")
    with patch("rootcoz.engine.chat.chat_with_ai", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = (False, library_text, None)
        response = test_client.post(
            "/api/chat/refusal-send-job",
            json={"message": "hello", "ai_provider": "claude", "ai_model": "sonnet-4"},
        )
    assert response.status_code == 202
    history = test_client.get("/api/chat/refusal-send-job").json()
    assistant = [m for m in history["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["status"] == "failed"
    assert assistant[-1]["content"] == f"Error: {_LIMITS_SENTENCE}"
    assert "Sidecar session creation failed" not in assistant[-1]["content"]

    mapped = ai_client.format_chat_ai_user_error(
        library_text, is_admin=False, ai_provider="openai", ai_model="undeclared"
    )
    assert mapped == _LIMITS_SENTENCE
    assert "Sidecar session creation failed" not in mapped

    refusal = await _refused_init(
        install_start_rig,
        refusing_sidecar,
        status_code=400,
        json_body={"error": _LIMITS_LITERAL},
    )
    assert refusal == _LIMITS_SENTENCE


def test_prompt_4xx_keeps_its_own_wording_and_stays_retryable():
    """A 4xx on ``/sessions/<id>/prompt`` is not a creation failure.

    The keyless mapper in ``_call_with_safe_error`` leaves the wording alone and
    the retry decision sees no non-retryable marker, so the attempt is retried —
    rewriting it as a refusal would make a live session's 429 permanent.
    """
    from rootcoz import ai_client
    from rootcoz.engine import core

    prompt_text = (
        "Client error '400 Bad Request' for url"
        " 'http://127.0.0.1:9100/sessions/abc/prompt'\nFor more information check:"
        " https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400"
    )
    detail = prompt_text.casefold()
    assert not any(marker in detail for marker in core._NON_RETRYABLE_AI_ERRORS)

    async def _failing_call(**_kwargs):
        return AIResult(success=False, text=prompt_text, error=prompt_text)

    result = asyncio.run(
        ai_client._call_with_safe_error(
            _failing_call, ai_provider="openai", ai_model="undeclared"
        )
    )
    assert result.text == prompt_text


def test_a_built_sentence_keeps_its_cause_through_both_mappers():
    """Re-mapping our own sentence must not answer ``sidecar HTTP None``.

    A 401 sentence carries no status any more, so both the send mapper and the
    safe-error wrapper have to treat a marker sentence as final.
    """
    from rootcoz import ai_client

    sentence = ai_client._session_refusal(
        401, "upstream rejected the key", "openai", "undeclared"
    )
    assert "rejected the AI credential for openai/undeclared" in sentence
    assert (
        ai_client.format_chat_ai_user_error(
            sentence, ai_provider="openai", ai_model="undeclared"
        )
        == sentence
    )

    async def _failing_call(**_kwargs):
        return AIResult(success=False, text=sentence, error=sentence)

    result = asyncio.run(
        ai_client._call_with_safe_error(
            _failing_call, ai_provider="openai", ai_model="undeclared"
        )
    )
    assert result.text == sentence
    assert "HTTP None" not in result.text


def test_a_sidecar_body_wearing_the_marker_is_never_forwarded():
    """Only a whole sentence rootcoz built is trusted as a refusal.

    ``_prompt_safely`` puts a non-200 body into ``AIResult.text`` verbatim, so a
    body that merely starts with the marker must not be forwarded to a chat user
    or skip keyed redaction.
    """
    from rootcoz import ai_client

    body = (
        "AI session refused: provider echoed the key sk-live-EXAMPLE-1234 and its"
        " whole upstream payload"
    )
    assert not ai_client._is_own_refusal_sentence(body)
    # Wearing our exact wording is not enough either: the variable slots hold a
    # pair and a status, so free text parked in one is still a body.
    dressed = (
        ai_client._REFUSAL_TEMPLATES[1]
        .template.replace(
            "$pair", "the key sk-live-EXAMPLE-1234 from the upstream payload"
        )
        .replace("$status", "500")
    )
    assert not ai_client._is_own_refusal_sentence(dressed)

    mapped = ai_client.format_chat_ai_user_error(
        body, is_admin=True, ai_provider="openai", ai_model="undeclared"
    )
    assert "sk-live-EXAMPLE-1234" not in mapped
    assert mapped.startswith(ai_client.SESSION_REFUSAL_MARKER)

    async def _failing_call(**_kwargs):
        return AIResult(success=False, text=body, error=body)

    result = asyncio.run(
        ai_client._call_with_safe_error(
            _failing_call, ai_provider="openai", ai_model="undeclared"
        )
    )
    assert "sk-live-EXAMPLE-1234" not in result.text
    assert result.text.startswith(ai_client.SESSION_REFUSAL_MARKER)

    assert "sk-live-EXAMPLE-1234" not in ai_client.format_chat_ai_user_error(
        dressed, is_admin=True, ai_provider="openai", ai_model="undeclared"
    )


@pytest.mark.parametrize(
    "message",
    [
        "AI session unavailable",
        "Chat credential unavailable; Start a new chat",
        "Server provider access requires an administrator grant",
        (
            "Server credentials for provider 'openai' are deactivated by the"
            " administrator (active_server_providers)"
        ),
        (
            "Model 'gpt-5' is excluded from server credentials for provider"
            " 'openai' by the administrator's model selection"
        ),
    ],
)
def test_rootcoz_own_recovery_message_is_never_replaced_by_retry(message):
    """A status-free sentence rootcoz wrote carries the fix, not "try again"."""
    from rootcoz import ai_client

    assert (
        ai_client.format_chat_ai_user_error(
            message, ai_provider="openai", ai_model="gpt-5"
        )
        == message
    )


def test_a_body_wearing_a_rootcoz_prefix_is_replaced():
    """Only the whole sentence rootcoz raised is forwarded, never the prefix.

    ``_is_rootcoz_actionable_error`` used to accept any text *starting* with
    ``Model '`` or ``Server credentials for provider '``, so a sidecar body
    wearing that prefix was returned verbatim to a chat user.
    """
    from rootcoz import ai_client

    deactivated = (
        "Server credentials for provider 'openai' are deactivated by the"
        " administrator (active_server_providers)"
    )
    deselected = (
        "Model 'gpt-5' is excluded from server credentials for provider 'openai'"
        " by the administrator's model selection"
    )
    bodies = (
        f"{deactivated} — upstream payload: sk-live-EXAMPLE-1234",
        f"{deselected}; see /srv/agent/models.json with key sk-live-EXAMPLE-1234",
        # The slot is a provider/model id: free text parked in it is a body.
        (
            "Server credentials for provider 'the key sk-live-EXAMPLE-1234 from the"
            " upstream payload' are deactivated by the administrator"
            " (active_server_providers)"
        ),
    )
    for body in bodies:
        assert not ai_client._is_rootcoz_actionable_error(body), body
        mapped = ai_client.format_chat_ai_user_error(
            body, is_admin=True, ai_provider="openai", ai_model="gpt-5"
        )
        assert "sk-live-EXAMPLE-1234" not in mapped, body
        assert mapped == "AI call failed. Please try again.", body


def _session_creation_error(status: int, text: str):
    """An ``httpx.HTTPStatusError`` shaped like a sidecar creation failure."""
    import httpx

    request = httpx.Request("POST", "http://127.0.0.1:9100/sessions")
    response = httpx.Response(status, request=request, text=text)
    return httpx.HTTPStatusError(text, request=request, response=response)


def test_a_library_429_text_stays_retryable():
    """A 429 shape is never admitted as a refusal, whatever text carries it."""
    from rootcoz import ai_client
    from rootcoz.engine import core

    library_text = (
        "Client error '429 Too Many Requests' for url"
        " 'http://127.0.0.1:9100/sessions'\nFor more information check:"
        " https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429"
    )
    for text in (library_text, "Sidecar session creation failed (HTTP 429): slow down"):
        assert ai_client._is_refusal_text(text) is False, text
        assert not any(
            marker in text.casefold() for marker in core._NON_RETRYABLE_AI_ERRORS
        ), text
        mapped = ai_client.format_chat_ai_user_error(
            text, ai_provider="openai", ai_model="undeclared"
        )
        assert not mapped.startswith(ai_client.SESSION_REFUSAL_MARKER), text


@pytest.mark.parametrize("status", [408, 425, 429])
def test_a_transient_session_creation_status_stays_retryable(status):
    """Only 408/425/429 are transient: mapping one to a refusal kills retries.

    The refusal sentence carries ``SESSION_REFUSAL_MARKER``, the one
    non-retryable signal, so a request timeout or a rate limit must stay ``None``
    and leave the call retryable.
    """
    from rootcoz import ai_client

    assert ai_client._TRANSIENT_SESSION_STATUSES == frozenset({408, 425, 429})
    exc = _session_creation_error(status, "slow down")
    assert ai_client._session_refusal_for(exc, "openai", "undeclared") is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_non_transient_session_creation_status_is_a_refusal(status):
    """Every other creation 4xx is the sidecar refusing; a retry cannot help."""
    from rootcoz import ai_client

    refusal = ai_client._session_refusal_for(
        _session_creation_error(status, "sidecar says no"), "openai", "undeclared"
    )
    assert isinstance(refusal, ai_client.SessionCreationRefused)
    assert str(refusal).startswith(f"{ai_client.SESSION_REFUSAL_MARKER}: ")


def test_a_sentence_built_for_a_catalog_id_with_a_space_and_a_slash_is_ours():
    """rootcoz registers what it builds, so no id shape can cost the guidance.

    The bounded template slots are tighter than a real catalog id, so the
    constructor registers each sentence it emits; the matcher trusts the
    registry entry, not the shape.
    """
    from rootcoz import ai_client

    provider, model = "my provider/inner", "the team's model/v2"

    def deselected() -> str:
        async def _selection():
            return {provider.lower()}, set()

        with (
            patch.object(ai_client, "_load_server_pool_selection", _selection),
            pytest.raises(ai_client.ExcludedServerModelError) as raised,
        ):
            asyncio.run(ai_client.require_selected_server_pair(provider, model))
        return str(raised.value)

    with pytest.raises(ai_client.InactiveServerProviderError) as raised:
        ai_client._raise_inactive_server_provider(provider)

    sentences = (
        str(raised.value),
        deselected(),
        ai_client._session_refusal(401, "upstream rejected the key", provider, model),
    )
    for sentence in sentences:
        assert ai_client._is_rootcoz_actionable_error(sentence) or (
            ai_client._is_own_refusal_sentence(sentence)
        ), sentence
        assert ai_client.format_chat_ai_user_error(sentence) == sentence


def test_a_body_copying_our_wording_is_never_ours():
    """Registration is what makes a sentence ours — a copy was never built."""
    from rootcoz import ai_client

    hostile = (
        "Server credentials for provider 'upstream said sk-live-EXAMPLE-1234 and"
        " its whole payload' are deactivated by the administrator"
        " (active_server_providers)"
    )
    assert not ai_client._is_rootcoz_actionable_error(hostile)
    assert not ai_client._is_own_refusal_sentence(hostile)
    mapped = ai_client.format_chat_ai_user_error(hostile, is_admin=True)
    assert mapped == "AI call failed. Please try again."
    assert "sk-live-EXAMPLE-1234" not in mapped


def test_the_own_sentence_registry_stays_bounded():
    """Every emitted sentence is remembered, so the registry must not grow."""
    from rootcoz import ai_client

    for index in range(ai_client._OWN_SENTENCE_REGISTRY_MAX + 50):
        ai_client._session_refusal(500, "boom", f"p{index}", "m")
    assert len(ai_client._own_sentences) == ai_client._OWN_SENTENCE_REGISTRY_MAX


def test_cursor_401_names_the_rejected_credential_not_the_browser_login():
    """A refused key is the cause; the browser-login hint is only a bare 4xx."""
    from rootcoz import ai_client

    sentence = ai_client._session_refusal(
        401, "upstream rejected the key", "cursor", "composer"
    )
    assert "agent login" not in sentence
    assert "rejected the AI credential for cursor/composer" in sentence
    # The bare-4xx Cursor failure keeps its remediation.
    assert "agent login" in ai_client._session_refusal(
        400, "upstream said no", "cursor", "composer"
    )


def test_a_cursor_refusal_keeps_its_sentence_at_send_time_too():
    """Start's 422 detail and the send-time message are one sentence.

    A bare-4xx Cursor refusal carries the browser-login hint, whose ``agent
    login`` marker used to re-label it; the sentence rootcoz built wins.
    """
    from rootcoz import ai_client

    sentence = ai_client._session_refusal(400, "upstream said no", "cursor", "composer")
    assert "agent login" in sentence
    for is_admin in (True, False):
        assert (
            ai_client.format_chat_ai_user_error(
                sentence, is_admin=is_admin, ai_provider="cursor", ai_model="composer"
            )
            == sentence
        )


@pytest.mark.asyncio
async def test_a_mapped_refusal_keeps_the_transcript_session_id():
    """The refusal copy still correlates with its recorded transcript.

    ``dataclasses.replace()`` drops non-field attributes, and cleanup has
    already cleared ``session_id`` by the time this copy is returned.
    """
    from rootcoz import ai_client

    async def _failing_call(**_kwargs):
        result = AIResult(
            success=False,
            text=(
                "Client error '400 Bad Request' for url"
                " 'http://127.0.0.1:9100/sessions'"
            ),
            error="bad request",
            session_id="old",
        )
        result.transcript_session_id = "11111111-2222-3333-4444-555555555555"
        return result

    mapped = await ai_client._call_with_safe_error(
        _failing_call, ai_provider="openai", ai_model="undeclared"
    )
    assert mapped.text.startswith(ai_client.SESSION_REFUSAL_MARKER)
    assert (
        getattr(mapped, "transcript_session_id", None)
        == "11111111-2222-3333-4444-555555555555"
    )


@pytest.mark.asyncio
async def test_token_limits_substring_is_not_the_limits_cause():
    """Only the exact sidecar literal is the limits cause — never the topic.

    A body that merely *contains* "token limits" must not be labelled "declare
    the model": on a prompt URL it keeps its own wording and stays retryable, and
    on a creation URL it answers the generic sentence, which names no
    ``models.json``. The admitted-by-no-shape corpus runs through both the shared
    predicate and the mapper, so a bare ``(HTTP 4`` search fails here.
    """
    from rootcoz import ai_client
    from rootcoz.engine import core

    twin = ai_client.format_chat_ai_user_error(
        f"Sidecar session creation failed (HTTP 400): {_LIMITS_LITERAL}",
        ai_provider="openai",
        ai_model="undeclared",
    )
    assert twin == _LIMITS_SENTENCE

    substring = "gateway: maximum token limits exceeded"
    assert ai_client._is_refusal_text(substring) is False
    assert "openai/undeclared (sidecar HTTP 400)" in ai_client._session_refusal(
        400, substring, "openai", "undeclared"
    )
    assert "models.json" not in ai_client._session_refusal(
        400, substring, "openai", "undeclared"
    )

    prompt_url = "Client error '400 Bad Request' for url 'http://127.0.0.1:9100/sessions/abc/prompt'\nbody: maximum token limits exceeded"
    assert ai_client._is_refusal_text(prompt_url) is False
    assert not any(
        marker in prompt_url.casefold() for marker in core._NON_RETRYABLE_AI_ERRORS
    )

    corpus = [
        "Sidecar API key model discovery failed (HTTP 500)",
        "Sidecar unhealthy (HTTP 503)",
        "Sidecar unhealthy (HTTP 400)",
        "Upstream gateway replied (HTTP 429) try later",
        "prompt rejected by upstream (HTTP 400)",
    ]
    for item in corpus:
        assert ai_client._is_refusal_text(item) is False, item
        assert not any(
            marker in item.casefold() for marker in core._NON_RETRYABLE_AI_ERRORS
        ), item
        answer = ai_client.format_chat_ai_user_error(
            item, ai_provider="openai", ai_model="undeclared"
        )
        assert answer != item
        assert item not in answer
        assert "models.json" not in answer


@pytest.mark.asyncio
async def test_text_admitted_by_no_shape_is_never_echoed():
    """A text no shape admits is answered with a constructed sentence.

    (a) a status no creation shape carries stays retryable instead of claiming
    "the session could not be created", (b) no status gets the generic AI-error
    sentence, and (c) shape 2 is a substring: a mid-string creation phrase *is*
    admitted, yet the cause is not claimed, so the wrapper text never survives.
    """
    from rootcoz import ai_client

    unmapped = "Upstream gateway replied (HTTP 429) try later"
    assert ai_client._is_refusal_text(unmapped) is False
    answer = ai_client.format_chat_ai_user_error(
        unmapped, ai_provider="openai", ai_model="undeclared"
    )
    assert answer == "AI call failed. Please try again."
    assert "Upstream gateway" not in answer
    assert "try later" not in answer

    bare = "prompt rejected by upstream"
    assert ai_client._is_refusal_text(bare) is False
    assert (
        ai_client.format_chat_ai_user_error(
            bare, ai_provider="openai", ai_model="undeclared"
        )
        == "AI call failed. Please try again."
    )

    wrapped = f"wrapper: Sidecar session creation failed (HTTP 400): {_LIMITS_LITERAL}"
    assert ai_client._is_refusal_text(wrapped) is True
    assert ai_client.format_chat_ai_user_error(
        wrapped, ai_provider="openai", ai_model="undeclared"
    ) == _generic_sentence(400)
