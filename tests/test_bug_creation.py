"""Tests for bug creation module (AI content generation and external API calls)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from ai_cli_runner import AIResult

from jenkins_job_insight.models import (
    AnalysisDetail,
    CodeFix,
    CreateIssueRequest,
    FailureAnalysis,
    ProductBugReport,
)

_TEST_GITHUB_TOKEN = "ghp_test"  # noqa: S105

# Expected footer substrings for assertion checks.
_GITHUB_FOOTER_MARKER = (
    "Generated using AI with [JJI](https://github.com/myk-org/jenkins-job-insight)"
)
_JIRA_FOOTER_MARKER = (
    "Generated using AI with [JJI|https://github.com/myk-org/jenkins-job-insight]"
)


@pytest.fixture
def code_issue_failure() -> FailureAnalysis:
    """A CODE ISSUE failure with a code fix."""
    return FailureAnalysis(
        test_name="tests.auth.test_login.TestLogin.test_valid_credentials",
        error="AssertionError: Expected status 200, got 500",
        error_signature="abc123def456",  # pragma: allowlist secret
        analysis=AnalysisDetail(
            classification="CODE ISSUE",
            affected_tests=["tests.auth.test_login.TestLogin.test_valid_credentials"],
            details="The test fails because the login endpoint handler does not catch ValueError from the password validator.",
            code_fix=CodeFix(
                file="src/auth/handlers.py",
                line="42",
                change="Add try/except around password_validator.validate(password)",
            ),
        ),
    )


@pytest.fixture
def product_bug_failure() -> FailureAnalysis:
    """A PRODUCT BUG failure with a bug report."""
    return FailureAnalysis(
        test_name="tests.network.test_dns.TestDNS.test_resolve",
        error="TimeoutError: DNS resolution timed out after 30s",
        error_signature="xyz789ghi012",
        analysis=AnalysisDetail(
            classification="PRODUCT BUG",
            affected_tests=["tests.network.test_dns.TestDNS.test_resolve"],
            details="DNS resolution is failing intermittently on the internal resolver.",
            product_bug_report=ProductBugReport(
                title="DNS resolution timeout on internal resolver",
                severity="high",
                component="networking",
                description="Internal DNS resolver fails to resolve hostnames within 30s",
                evidence="TimeoutError at dns_client.py:88 - socket.timeout after 30000ms",
                jira_search_keywords=["DNS", "timeout", "resolver"],
            ),
        ),
    )


class TestGenerateGithubIssueContent:
    async def test_generates_title_and_body(self, code_issue_failure):
        from jenkins_job_insight.bug_creation import generate_github_issue_content

        with patch("jenkins_job_insight.bug_creation.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(
                success=True,
                text="Fix: login handler missing ValueError catch\n\n"
                "## Test Failure\n\n"
                "**Test:** `tests.auth.test_login.TestLogin.test_valid_credentials`\n\n"
                "## Error\n\n"
                "```\nAssertionError: Expected status 200, got 500\n```\n\n"
                "## Analysis\n\n"
                "The login endpoint handler does not catch ValueError.\n\n"
                "## Suggested Fix\n\n"
                "**File:** `src/auth/handlers.py` line 42\n"
                "Add try/except around password_validator.",
            )

            result = await generate_github_issue_content(
                failure=code_issue_failure,
                report_url="https://jji.example.com/results/job-123",
                ai_provider="claude",
                ai_model="sonnet",
            )
            assert result["title"]
            assert result["body"]
            assert "test_valid_credentials" in result["body"]
            assert _GITHUB_FOOTER_MARKER in result["body"]

    async def test_fallback_on_ai_failure(self, code_issue_failure):
        from jenkins_job_insight.bug_creation import generate_github_issue_content

        with patch("jenkins_job_insight.bug_creation.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="AI CLI timed out")

            result = await generate_github_issue_content(
                failure=code_issue_failure,
                report_url="https://jji.example.com/results/job-123",
                ai_provider="claude",
                ai_model="sonnet",
            )
            # Should still return usable content built from failure data
            assert result["title"]
            assert result["body"]
            assert "test_valid_credentials" in result["body"]
            assert _GITHUB_FOOTER_MARKER in result["body"]

    async def test_fallback_includes_code_fix(self, code_issue_failure):
        from jenkins_job_insight.bug_creation import generate_github_issue_content

        with patch("jenkins_job_insight.bug_creation.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="AI CLI timed out")

            result = await generate_github_issue_content(
                failure=code_issue_failure,
                report_url="",
                ai_provider="claude",
                ai_model="sonnet",
            )
            assert "src/auth/handlers.py" in result["body"]
            assert "Suggested Fix" in result["body"]
            assert _GITHUB_FOOTER_MARKER in result["body"]


class TestGenerateJiraBugContent:
    async def test_generates_summary_and_description(self, product_bug_failure):
        from jenkins_job_insight.bug_creation import generate_jira_bug_content

        with patch("jenkins_job_insight.bug_creation.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(
                success=True,
                text="DNS resolution timeout on internal resolver\n\n"
                "h2. Summary\n\n"
                "DNS resolution is failing intermittently.\n\n"
                "h2. Evidence\n\n"
                "TimeoutError at dns_client.py:88",
            )

            result = await generate_jira_bug_content(
                failure=product_bug_failure,
                report_url="https://jji.example.com/results/job-456",
                ai_provider="claude",
                ai_model="sonnet",
            )
            assert result["title"]
            assert result["body"]
            assert _JIRA_FOOTER_MARKER in result["body"]

    async def test_fallback_on_ai_failure(self, product_bug_failure):
        from jenkins_job_insight.bug_creation import generate_jira_bug_content

        with patch("jenkins_job_insight.bug_creation.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="error")

            result = await generate_jira_bug_content(
                failure=product_bug_failure,
                report_url="",
                ai_provider="claude",
                ai_model="sonnet",
            )
            assert result["title"] == "DNS resolution timeout on internal resolver"
            assert "test_resolve" in result["body"]
            assert _JIRA_FOOTER_MARKER in result["body"]


class TestSearchGithubDuplicates:
    async def test_finds_similar_issues(self):
        from jenkins_job_insight.bug_creation import search_github_duplicates

        mock_response = httpx.Response(
            200,
            json={
                "total_count": 1,
                "items": [
                    {
                        "number": 42,
                        "title": "Login fails with valid credentials",
                        "html_url": "https://github.com/org/repo/issues/42",
                        "state": "open",
                    }
                ],
            },
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            results = await search_github_duplicates(
                title="Login fails with valid credentials",
                repo_url="https://github.com/org/repo",
                github_token=_TEST_GITHUB_TOKEN,
            )
            assert len(results) == 1
            assert results[0]["number"] == 42

    async def test_returns_empty_on_error(self):
        from jenkins_job_insight.bug_creation import search_github_duplicates

        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.RequestError("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            results = await search_github_duplicates(
                title="Login fails",
                repo_url="https://github.com/org/repo",
                github_token=_TEST_GITHUB_TOKEN,
            )
            assert results == []

    async def test_returns_empty_on_bad_url(self):
        from jenkins_job_insight.bug_creation import search_github_duplicates

        results = await search_github_duplicates(
            title="Login fails",
            repo_url="not-a-github-url",
            github_token=_TEST_GITHUB_TOKEN,
        )
        assert results == []


def _mock_request() -> httpx.Request:
    """Create a dummy httpx.Request for use in mock responses."""
    return httpx.Request("POST", "https://example.com")


class TestCreateGithubIssue:
    async def test_creates_issue(self):
        from jenkins_job_insight.bug_creation import create_github_issue

        mock_response = httpx.Response(
            201,
            json={
                "number": 99,
                "title": "Bug: login fails",
                "html_url": "https://github.com/org/repo/issues/99",
            },
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await create_github_issue(
                title="Bug: login fails",
                body="## Details\nLogin returns 500",
                repo_url="https://github.com/org/repo",
                github_token=_TEST_GITHUB_TOKEN,
            )
            assert result["url"] == "https://github.com/org/repo/issues/99"
            assert result["number"] == 99

            # Verify footer was appended to the body sent to GitHub API
            posted_body = mock_client.post.call_args.kwargs["json"]["body"]
            assert _GITHUB_FOOTER_MARKER in posted_body

    async def test_creates_issue_with_labels(self):
        from jenkins_job_insight.bug_creation import create_github_issue

        mock_response = httpx.Response(
            201,
            json={
                "number": 100,
                "title": "Bug: login fails",
                "html_url": "https://github.com/org/repo/issues/100",
            },
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await create_github_issue(
                title="Bug: login fails",
                body="Details",
                repo_url="https://github.com/org/repo",
                github_token=_TEST_GITHUB_TOKEN,
                labels=["bug", "test-failure"],
            )
            assert result["number"] == 100

            # Verify footer was appended to the body sent to GitHub API
            posted_body = mock_client.post.call_args.kwargs["json"]["body"]
            assert _GITHUB_FOOTER_MARKER in posted_body


class TestCreateJiraBug:
    async def test_creates_bug(self):
        from jenkins_job_insight.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token123"
        mock_settings.jira_pat = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={
                "key": "PROJ-456",
                "self": "https://jira.example.com/rest/api/3/issue/10001",
            },
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await create_jira_bug(
                title="DNS timeout on resolver",
                body="DNS resolution fails intermittently",
                settings=mock_settings,
            )
            assert result["key"] == "PROJ-456"
            assert "jira.example.com" in result["url"]

            # Verify Jira-format footer was appended
            posted_body = mock_client.post.call_args.kwargs["json"]["fields"][
                "description"
            ]
            assert _JIRA_FOOTER_MARKER in posted_body

    async def test_creates_bug_server_dc(self):
        """Test Jira Server/DC auth (Bearer PAT, no email)."""
        from jenkins_job_insight.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira-server.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = None
        mock_settings.jira_api_token = None
        mock_settings.jira_pat = MagicMock()
        mock_settings.jira_pat.get_secret_value.return_value = "pat123"
        mock_settings.jira_ssl_verify = False

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-789"},
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await create_jira_bug(
                title="DNS timeout",
                body="Description",
                settings=mock_settings,
            )
            assert result["key"] == "PROJ-789"

    async def test_creates_bug_with_custom_issue_type(self):
        """Test creating a Jira issue with a custom issue type."""
        from jenkins_job_insight.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token123"
        mock_settings.jira_pat = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-999"},
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await create_jira_bug(
                title="Story title",
                body="Story description",
                settings=mock_settings,
                issue_type="Story",
            )
            assert result["key"] == "PROJ-999"

            # Verify the payload sent to Jira uses the custom issue type
            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert payload["fields"]["issuetype"] == {"name": "Story"}

    async def test_creates_bug_default_issue_type(self):
        """Test that default issue type is 'Bug' when not specified."""
        from jenkins_job_insight.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token123"
        mock_settings.jira_pat = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-100"},
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await create_jira_bug(
                title="Bug title",
                body="Bug description",
                settings=mock_settings,
            )
            assert result["key"] == "PROJ-100"

            # Verify the payload uses default 'Bug' issue type
            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert payload["fields"]["issuetype"] == {"name": "Bug"}

            # Verify Jira-format footer was appended
            posted_body = mock_client.post.call_args.kwargs["json"]["fields"][
                "description"
            ]
            assert _JIRA_FOOTER_MARKER in posted_body


class TestParseGithubRepoUrl:
    def test_standard_url(self):
        from jenkins_job_insight.bug_creation import _parse_github_repo_url

        owner, repo = _parse_github_repo_url("https://github.com/myorg/myrepo")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_url_with_git_suffix(self):
        from jenkins_job_insight.bug_creation import _parse_github_repo_url

        owner, repo = _parse_github_repo_url("https://github.com/myorg/myrepo.git")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_url_with_dots_in_repo_name(self):
        """Finding 5: Repo names with dots should be parsed correctly."""
        from jenkins_job_insight.bug_creation import _parse_github_repo_url

        owner, repo = _parse_github_repo_url("https://github.com/org/my.repo")
        assert owner == "org"
        assert repo == "my.repo"

    def test_url_with_dots_and_git_suffix(self):
        """Finding 5: Repo names with dots AND .git suffix."""
        from jenkins_job_insight.bug_creation import _parse_github_repo_url

        owner, repo = _parse_github_repo_url("https://github.com/org/my.repo.git")
        assert owner == "org"
        assert repo == "my.repo"

    def test_invalid_url(self):
        from jenkins_job_insight.bug_creation import _parse_github_repo_url

        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_github_repo_url("not-a-url")


class TestBuildFailbackContent:
    def test_github_fallback_with_product_bug(self, product_bug_failure):
        from jenkins_job_insight.bug_creation import (
            _build_failure_context,
            _build_fallback_github_content,
        )

        ctx = _build_failure_context(product_bug_failure)
        result = _build_fallback_github_content(
            ctx, "https://jenkins.example.com/job/1", ""
        )
        assert result["title"] == "DNS resolution timeout on internal resolver"
        assert "test_resolve" in result["body"]

    def test_jira_fallback_with_product_bug(self, product_bug_failure):
        from jenkins_job_insight.bug_creation import (
            _build_failure_context,
            _build_fallback_jira_content,
        )

        ctx = _build_failure_context(product_bug_failure)
        result = _build_fallback_jira_content(ctx, "", "https://report.example.com")
        assert result["title"] == "DNS resolution timeout on internal resolver"
        assert "h2." in result["body"]

    def test_github_fallback_without_product_bug(self, code_issue_failure):
        from jenkins_job_insight.bug_creation import (
            _build_failure_context,
            _build_fallback_github_content,
        )

        ctx = _build_failure_context(code_issue_failure)
        result = _build_fallback_github_content(ctx, "", "")
        assert "test_valid_credentials" in result["title"]


class TestCreateIssueRequestJiraIssueType:
    """Tests for the jira_issue_type field on CreateIssueRequest."""

    def test_default_issue_type_is_bug(self):
        req = CreateIssueRequest(
            test_name="test_foo",
            title="title",
            body="body",
        )
        assert req.jira_issue_type == "Bug"

    def test_custom_issue_type(self):
        req = CreateIssueRequest(
            test_name="test_foo",
            title="title",
            body="body",
            jira_issue_type="Story",
        )
        assert req.jira_issue_type == "Story"

    def test_issue_type_task(self):
        req = CreateIssueRequest(
            test_name="test_foo",
            title="title",
            body="body",
            jira_issue_type="Task",
        )
        assert req.jira_issue_type == "Task"


class TestAiFooterNotDoubled:
    """Verify footer deduplication: content that already has the footer is not doubled."""

    async def test_github_footer_not_doubled(self):
        from jenkins_job_insight.bug_creation import (
            GITHUB_AI_FOOTER,
            create_github_issue,
        )

        body_with_footer = "## Details\nSome content" + GITHUB_AI_FOOTER

        mock_response = httpx.Response(
            201,
            json={
                "number": 200,
                "title": "Test",
                "html_url": "https://github.com/org/repo/issues/200",
            },
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await create_github_issue(
                title="Test",
                body=body_with_footer,
                repo_url="https://github.com/org/repo",
                github_token=_TEST_GITHUB_TOKEN,
            )
            posted_body = mock_client.post.call_args.kwargs["json"]["body"]
            assert posted_body.count(_GITHUB_FOOTER_MARKER) == 1

    async def test_jira_footer_not_doubled(self):
        from jenkins_job_insight.bug_creation import JIRA_AI_FOOTER, create_jira_bug

        body_with_footer = "h2. Details\nSome content" + JIRA_AI_FOOTER

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token"
        mock_settings.jira_pat = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-999"},
            request=_mock_request(),
        )
        with patch("jenkins_job_insight.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await create_jira_bug(
                title="Test",
                body=body_with_footer,
                settings=mock_settings,
            )
            posted_body = mock_client.post.call_args.kwargs["json"]["fields"][
                "description"
            ]
            assert posted_body.count(_JIRA_FOOTER_MARKER) == 1
