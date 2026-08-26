"""Tests for the chat feature."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

        from rootcoz.main import app

        with TestClient(app, headers=_ADMIN_AUTH_HEADERS) as client:
            yield client


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
            jira_url="https://jira.example.com",
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
            jira_url="https://jira.example.com",
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
            jira_url="https://jira.example.com",
            jira_token="jira-pat",
        )
        jira_tool = next(t for t in tools if t["name"] == "search_jira")
        assert jira_tool["http"]["headers"]["Authorization"] == "Bearer jira-pat"

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
            jira_url="https://jira.example.com",
            jira_token="jira-tok",
            github_token="gh-tok",
            github_repo="org/repo",
        )
        names = [t["name"] for t in tools]
        assert len(names) == 9  # 5 base + 2 jira + 2 github

    def test_no_jira_without_token(self):
        tools = build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_url="https://jira.example.com",
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


class TestBuildSystemPrompt:
    """Tests for build_system_prompt with custom_tools."""

    def _make_tools(self, *, jira: bool = False, github: bool = False) -> list[dict]:
        """Helper to build custom tools with optional integrations."""
        return build_chat_custom_tools(
            server_url="http://localhost:8000",
            auth_token="tok",
            job_id="j1",
            jira_url="https://jira.example.com" if jira else "",
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
    assert "job-offload" in main_mod._chat_jobs_deleting
    main_mod._chat_jobs_deleting.discard("job-offload")


@pytest.mark.asyncio
async def test_init_chat_under_barrier_rejects_deleting_job():
    from fastapi import HTTPException

    from rootcoz import main as main_mod

    main_mod._chat_jobs_deleting.add("gone-job")
    try:
        with pytest.raises(HTTPException) as exc:
            await main_mod._init_chat_under_barrier("gone-job", "alice")
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
    main_mod._chat_jobs_deleting.discard("job-block")


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
        assert assistant_msgs[0]["content"] == "AI response here"
        assert assistant_msgs[0]["status"] == "completed"

    async def test_send_chat_message_saves_history(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(
            temp_db_path,
            "chat-hist-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
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
        assert data["total"] == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "hello"
        assert data["messages"][1]["role"] == "assistant"
        assert data["messages"][1]["content"] == "first response"

    async def test_delete_chat_history(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-del-job")
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
        response = test_client.delete("/api/chat/chat-del-job")
        assert response.status_code == 200
        assert response.json()["deleted"] == 2  # user + assistant

    def test_delete_chat_history_404(self, test_client):
        response = test_client.delete("/api/chat/nonexistent-job")
        assert response.status_code == 404

    async def test_send_message_no_provider_queues_and_fails(
        self, test_client, temp_db_path: Path
    ):
        """Without a provider, message is queued (202) but background processing fails."""
        await _save_job(temp_db_path, "chat-noprov-job")
        response = test_client.post(
            "/api/chat/chat-noprov-job",
            json={"message": "hello"},
        )
        assert response.status_code == 202
        # Background task fails — assistant message should be marked failed
        history = test_client.get("/api/chat/chat-noprov-job").json()
        assistant_msgs = [m for m in history["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[0]["status"] == "failed"

    def test_send_message_404_for_missing_job(self, test_client):
        response = test_client.post(
            "/api/chat/nonexistent-job",
            json={"message": "hello", "ai_provider": "claude", "ai_model": "sonnet-4"},
        )
        assert response.status_code == 404

    async def test_send_message_invalid_provider(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-badprov-job")
        response = test_client.post(
            "/api/chat/chat-badprov-job",
            json={
                "message": "hello",
                "ai_provider": "invalid-provider",
                "ai_model": "x",
            },
        )
        assert response.status_code == 422
        assert "Invalid AI provider" in response.json()["detail"]

    async def test_send_message_normalizes_legacy_provider_alias(
        self, test_client, temp_db_path: Path
    ):
        """Legacy aliases (cursor-cli) must normalize to cursor before validation."""
        await _save_job(temp_db_path, "chat-alias-job")
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
        assert assistant_msgs[0]["ai_provider"] == "cursor"
        assert assistant_msgs[0]["ai_model"] == "composer-1"

    async def test_send_message_normalizes_provider_case(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(temp_db_path, "chat-case-job")
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
        assert assistant_msgs[0]["ai_provider"] == "cursor"
        assert assistant_msgs[0]["ai_model"] == "composer-1"

    async def test_send_message_provider_without_model_rejected(
        self, test_client, temp_db_path: Path
    ):
        await _save_job(temp_db_path, "chat-partial-job")
        response = test_client.post(
            "/api/chat/chat-partial-job",
            json={"message": "hello", "ai_provider": "claude"},
        )
        assert response.status_code == 422
        assert "Both ai_provider and ai_model" in response.json()["detail"]

    async def test_send_message_ai_failure_marks_failed(
        self, test_client, temp_db_path: Path
    ):
        """AI failure is handled in background — message is queued (202), then marked failed."""
        await _save_job(
            temp_db_path,
            "chat-fail-job",
            {"ai_provider": "claude", "ai_model": "sonnet-4"},
        )
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
        assert assistant_msgs[0]["status"] == "failed"
        assert "AI CLI timed out" in assistant_msgs[0]["content"]

    async def test_get_chat_with_pagination(self, test_client, temp_db_path: Path):
        await _save_job(temp_db_path, "chat-page-job")
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
        assert len(data["messages"]) == 2
        assert data["total"] == 6


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
        test_client.delete("/api/admin/chat")

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
