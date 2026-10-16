"""Tests for bug creation module (AI content generation and external API calls)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pi_sidecar_client import AIResult

from rootcoz.attribution import AiProvenance, apply_ai_attribution, read_provenance
from rootcoz.bug_creation import create_github_issue
from rootcoz.models import (
    AnalysisDetail,
    CodeFix,
    CreateIssueRequest,
    FailureAnalysis,
    ProductBugReport,
)

_TEST_GITHUB_TOKEN = "ghp_test"

# Expected footer substrings for assertion checks.
_GITHUB_FOOTER_MARKER = (
    "Generated using AI with [rootcoz](https://github.com/myk-org/rootcoz)"
)
_JIRA_FOOTER_MARKER = (
    "Generated using AI with [rootcoz|https://github.com/myk-org/rootcoz]"
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
            details=(
                "The test fails because the login endpoint handler"
                " does not catch ValueError from the password validator."
            ),
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
        from rootcoz.bug_creation import generate_github_issue_content

        with patch("rootcoz.bug_creation.call_ai_once") as mock_ai:
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
                report_url="https://rootcoz.example.com/results/job-123",
                ai_provider="claude",
                ai_model="sonnet",
            )
            assert result["title"]
            assert result["body"]
            assert "test_valid_credentials" in result["body"]
            assert _GITHUB_FOOTER_MARKER in result["body"]

    async def test_fallback_on_ai_failure(self, code_issue_failure):
        from rootcoz.bug_creation import generate_github_issue_content

        with patch("rootcoz.bug_creation.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="AI CLI timed out")

            result = await generate_github_issue_content(
                failure=code_issue_failure,
                report_url="https://rootcoz.example.com/results/job-123",
                ai_provider="claude",
                ai_model="sonnet",
            )
            # Should still return usable content built from failure data
            assert result["title"]
            assert result["body"]
            assert "test_valid_credentials" in result["body"]
            assert _GITHUB_FOOTER_MARKER in result["body"]

    async def test_fallback_includes_code_fix(self, code_issue_failure):
        from rootcoz.bug_creation import generate_github_issue_content

        with patch("rootcoz.bug_creation.call_ai_once") as mock_ai:
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
        from rootcoz.bug_creation import generate_jira_bug_content

        with patch("rootcoz.bug_creation.call_ai_once") as mock_ai:
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
                report_url="https://rootcoz.example.com/results/job-456",
                ai_provider="claude",
                ai_model="sonnet",
            )
            assert result["title"]
            assert result["body"]
            assert _JIRA_FOOTER_MARKER in result["body"]

    async def test_fallback_on_ai_failure(self, product_bug_failure):
        from rootcoz.bug_creation import generate_jira_bug_content

        with patch("rootcoz.bug_creation.call_ai_once") as mock_ai:
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


class TestSearchJiraDuplicatesIncludesDescription:
    """Verify search_jira_duplicates returns description for AI filtering."""

    async def test_returns_description_field(self):
        from rootcoz.bug_creation import search_jira_duplicates

        mock_settings = MagicMock()
        mock_settings.jira_enabled = True
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token"
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = None
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = True
        mock_settings.jira_max_results = 5

        mock_client = AsyncMock()
        mock_client.search.return_value = [
            {
                "key": "PROJ-1",
                "summary": "Login broken",
                "description": "Login fails with 500 error",
                "status": "Open",
                "priority": "High",
                "url": "https://jira.example.com/browse/PROJ-1",
            }
        ]

        with patch(
            "rootcoz.bug_creation.JiraClient",
        ) as MockJiraClient:
            MockJiraClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockJiraClient.return_value.__aexit__ = AsyncMock(return_value=None)

            results = await search_jira_duplicates(
                title="Login broken after update",
                settings=mock_settings,
            )

        assert len(results) == 1
        assert results[0]["key"] == "PROJ-1"
        assert results[0]["description"] == "Login fails with 500 error"
        assert results[0]["summary"] == "Login broken"
        assert results[0]["title"] == "Login broken"


class TestSearchGithubDuplicates:
    async def test_finds_similar_issues(self):
        from rootcoz.bug_creation import search_github_duplicates

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
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
        from rootcoz.bug_creation import search_github_duplicates

        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
        from rootcoz.bug_creation import search_github_duplicates

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
        from rootcoz.bug_creation import create_github_issue

        mock_response = httpx.Response(
            201,
            json={
                "number": 99,
                "title": "Bug: login fails",
                "html_url": "https://github.com/org/repo/issues/99",
            },
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
                attribution=AiProvenance(ai_used=True),
            )
            assert result["url"] == "https://github.com/org/repo/issues/99"
            assert result["number"] == 99

            # Verify footer was appended to the body sent to GitHub API
            posted_body = mock_client.post.call_args.kwargs["json"]["body"]
            assert _GITHUB_FOOTER_MARKER in posted_body

    async def test_creates_issue_with_labels(self):
        from rootcoz.bug_creation import create_github_issue

        mock_response = httpx.Response(
            201,
            json={
                "number": 100,
                "title": "Bug: login fails",
                "html_url": "https://github.com/org/repo/issues/100",
            },
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
                attribution=AiProvenance(ai_used=True),
            )
            assert result["number"] == 100

            # Verify footer was appended to the body sent to GitHub API
            posted_body = mock_client.post.call_args.kwargs["json"]["body"]
            assert _GITHUB_FOOTER_MARKER in posted_body


class TestCreateJiraBug:
    async def test_creates_bug(self):
        from rootcoz.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token123"
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = None
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={
                "key": "PROJ-456",
                "self": "https://jira.example.com/rest/api/3/issue/10001",
            },
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
        from rootcoz.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira-server.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = None
        mock_settings.jira_api_token = None
        mock_settings.jira_pat = MagicMock()
        mock_settings.jira_oauth_access_token = None
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_pat.get_secret_value.return_value = "pat123"
        mock_settings.jira_oauth_access_token = None
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = False

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-789"},
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
        from rootcoz.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token123"
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = None
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-999"},
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
        from rootcoz.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token123"
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = None
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-100"},
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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
        from rootcoz.bug_creation import parse_github_repo_url

        owner, repo = parse_github_repo_url("https://github.com/myorg/myrepo")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_url_with_git_suffix(self):
        from rootcoz.bug_creation import parse_github_repo_url

        owner, repo = parse_github_repo_url("https://github.com/myorg/myrepo.git")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_url_with_dots_in_repo_name(self):
        """Finding 5: Repo names with dots should be parsed correctly."""
        from rootcoz.bug_creation import parse_github_repo_url

        owner, repo = parse_github_repo_url("https://github.com/org/my.repo")
        assert owner == "org"
        assert repo == "my.repo"

    def test_url_with_dots_and_git_suffix(self):
        """Finding 5: Repo names with dots AND .git suffix."""
        from rootcoz.bug_creation import parse_github_repo_url

        owner, repo = parse_github_repo_url("https://github.com/org/my.repo.git")
        assert owner == "org"
        assert repo == "my.repo"

    def test_invalid_url(self):
        from rootcoz.bug_creation import parse_github_repo_url

        with pytest.raises(ValueError, match="Cannot parse"):
            parse_github_repo_url("not-a-url")


class TestBuildFailbackContent:
    def test_github_fallback_with_product_bug(self, product_bug_failure):
        from rootcoz.bug_creation import (
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
        from rootcoz.bug_creation import (
            _build_failure_context,
            _build_fallback_jira_content,
        )

        ctx = _build_failure_context(product_bug_failure)
        result = _build_fallback_jira_content(ctx, "", "https://report.example.com")
        assert result["title"] == "DNS resolution timeout on internal resolver"
        assert "h2." in result["body"]

    def test_github_fallback_without_product_bug(self, code_issue_failure):
        from rootcoz.bug_creation import (
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
    """The footer comes from the caller's provenance, never from the body."""

    @staticmethod
    async def _posted(body: str, **kwargs) -> str:
        """Create the issue through the real creator and return what GitHub got."""
        mock_response = httpx.Response(
            201,
            json={
                "number": 200,
                "title": "Test",
                "html_url": "https://github.com/org/repo/issues/200",
            },
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client
            await create_github_issue(
                title="Test",
                body=body,
                repo_url="https://github.com/org/repo",
                github_token=_TEST_GITHUB_TOKEN,
                **kwargs,
            )
        return mock_client.post.call_args.kwargs["json"]["body"]

    async def test_body_footer_never_decides_the_attribution(self):
        """A footer in the body is stripped, not kept (one server footer)."""
        from rootcoz.bug_creation import GITHUB_AI_FOOTER

        posted = await self._posted(
            "## Details\nSome content" + GITHUB_AI_FOOTER,
            attribution=AiProvenance(ai_used=True),
        )
        assert posted.count(_GITHUB_FOOTER_MARKER) == 1

    async def test_client_body_cannot_claim_a_model(self):
        """`create-issue --body` cannot suppress the server's own footer."""
        spoofed = (
            "## Details\nLogin returns 500\n\n---\n*Generated using AI with "
            f"{_GITHUB_FOOTER_MARKER} (evil / spoofed-model)*"
        )
        posted = await self._posted(spoofed, attribution=AiProvenance(ai_used=True))
        assert "spoofed-model" not in posted
        assert posted.count(_GITHUB_FOOTER_MARKER) == 1

    async def test_crlf_client_footer_cannot_claim_a_model(self):
        """A Windows-style fake footer is stripped like any other (Qodo HIGH)."""
        spoofed = (
            "## Details\r\nLogin returns 500\r\n\r\n---\r\n*Generated using AI with "
            f"{_GITHUB_FOOTER_MARKER} (evil / spoofed-model)*\r\n"
        )
        posted = await self._posted(spoofed, attribution=AiProvenance(ai_used=True))
        assert "spoofed-model" not in posted
        assert posted.count(_GITHUB_FOOTER_MARKER) == 1

    async def test_swapped_separator_cannot_credit_a_model(self):
        """The published body is the no-AI one after a separator swap (Qodo HIGH).

        \v, \f, \\x1c-\\x1e, U+2028/U+2029 are not line breaks, so replacing one
        with a newline changes the content: the token no longer verifies and the
        issue creator publishes the NO-AI footer.
        """
        for separator in ["\v", "\f", "\x1c", "\x1d", "\x1e", "\u2028", "\u2029"]:
            preview = apply_ai_attribution(
                f"## Bug{separator}separator{separator}Broken.",
                AiProvenance(ai_used=True, provider="cli-claude", model="sonnet-4-5"),
            )
            tampered = preview.replace(separator, "\n")
            assert read_provenance(preview) is not None
            assert read_provenance(tampered) is None
            posted = await self._posted(tampered, attribution=read_provenance(tampered))
            assert "sonnet-4-5" not in posted
            assert "No AI model generated this issue" in posted
            assert _GITHUB_FOOTER_MARKER not in posted

    async def test_without_a_signal_no_model_is_credited(self):
        posted = await self._posted("## Details\nSome content")
        assert "No AI model generated this issue" in posted
        assert _GITHUB_FOOTER_MARKER not in posted

    async def test_verified_provenance_names_the_model(self):
        posted = await self._posted(
            "## Details\nSome content",
            attribution=AiProvenance(
                ai_used=True, provider="cli-claude", model="sonnet-4-5"
            ),
        )
        assert "(claude / sonnet-4-5)" in posted
        assert "cli-" not in posted

    async def test_jira_footer_not_doubled(self):
        from rootcoz.bug_creation import JIRA_AI_FOOTER, create_jira_bug

        body_with_footer = "h2. Details\nSome content" + JIRA_AI_FOOTER

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://jira.example.com"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "test@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token"
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = None
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201,
            json={"key": "PROJ-999"},
            request=_mock_request(),
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
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

    async def test_sa_token_uses_scoped_gateway_and_v3(self):
        """Service account tokens post to the scoped gateway with REST v3."""
        from rootcoz.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = (
            "https://api.atlassian.com/ex/jira/2b9e35e3-6bd3-4cec-b838-f4249ee02432"
        )
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = "user@example.com"
        mock_settings.jira_api_token = MagicMock()
        mock_settings.jira_api_token.get_secret_value.return_value = "token123"
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = MagicMock()
        mock_settings.jira_oauth_access_token.get_secret_value.return_value = (
            "ATST-test-token"
        )
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = "https://myorg.atlassian.net"
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201, json={"key": "PROJ-789"}, request=_mock_request()
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await create_jira_bug(
                title="SA bug",
                body="body",
                settings=mock_settings,
            )
            assert result["key"] == "PROJ-789"
            # Browse link uses the site URL, never the scoped gateway
            assert result["url"] == "https://myorg.atlassian.net/browse/PROJ-789"

            post_url = mock_client.post.call_args.args[0]
            assert post_url.startswith(
                "https://api.atlassian.com/ex/jira/2b9e35e3-6bd3-4cec-b838-f4249ee02432/rest/api/3/issue"
            )
            post_headers = mock_client.post.call_args.kwargs["headers"]
            assert post_headers["Authorization"] == "Bearer ATST-test-token"
            assert MockClient.call_args.kwargs["auth"] is None

    async def test_sa_token_without_cloud_id_raises(self):
        """An unresolvable SA cloud ID fails fast with a clear message."""
        from rootcoz.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = "https://myorg.atlassian.net"
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = None
        mock_settings.jira_api_token = None
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = MagicMock()
        mock_settings.jira_oauth_access_token.get_secret_value.return_value = (
            "ATST-test-token"
        )
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = True

        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            with pytest.raises(ValueError, match="cloud ID"):
                await create_jira_bug(
                    title="SA bug", body="body", settings=mock_settings
                )

    async def test_sa_description_is_adf_document(self):
        """SA mode (REST v3) wraps the description in an ADF document."""
        from rootcoz.bug_creation import _text_to_adf, create_jira_bug

        # Unit: plain text becomes a doc with one paragraph per line.
        adf = _text_to_adf("line one\n\nline three")
        assert adf["type"] == "doc"
        assert adf["version"] == 1
        assert [p.get("content") for p in adf["content"]] == [
            [{"type": "text", "text": "line one"}],
            [],
            [{"type": "text", "text": "line three"}],
        ]
        mock_settings = MagicMock()
        mock_settings.jira_url = (
            "https://api.atlassian.com/ex/jira/2b9e35e3-6bd3-4cec-b838-f4249ee02432"
        )
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = None
        mock_settings.jira_api_token = None
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = MagicMock()
        mock_settings.jira_oauth_access_token.get_secret_value.return_value = (
            "ATST-test-token"
        )
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = "https://myorg.atlassian.net"
        mock_settings.jira_ssl_verify = True

        mock_response = httpx.Response(
            201, json={"key": "PROJ-790"}, request=_mock_request()
        )
        with patch("rootcoz.bug_creation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await create_jira_bug(
                title="SA bug", body="plain body", settings=mock_settings
            )
            payload = mock_client.post.call_args.kwargs["json"]
            description = payload["fields"]["description"]
            assert description["type"] == "doc"
            paragraphs = description["content"]
            assert paragraphs[0]["content"] == [{"type": "text", "text": "plain body"}]
            # Non-SA modes must keep the plain string description.
            # (Covered by the cloud/server tests asserting no ADF.)

    async def test_sa_token_without_site_url_raises(self):
        """A scoped URL without a site URL fails fast — no broken links."""
        from rootcoz.bug_creation import create_jira_bug

        mock_settings = MagicMock()
        mock_settings.jira_url = (
            "https://api.atlassian.com/ex/jira/2b9e35e3-6bd3-4cec-b838-f4249ee02432"
        )
        mock_settings.jira_project_key = "PROJ"
        mock_settings.jira_email = None
        mock_settings.jira_api_token = None
        mock_settings.jira_pat = None
        mock_settings.jira_oauth_access_token = MagicMock()
        mock_settings.jira_oauth_access_token.get_secret_value.return_value = (
            "ATST-test-token"
        )
        mock_settings.atlassian_oauth_cloud_id = None
        mock_settings.jira_site_url = None
        mock_settings.jira_ssl_verify = True

        with pytest.raises(ValueError, match="JIRA_SITE_URL"):
            await create_jira_bug(title="SA bug", body="body", settings=mock_settings)

    async def test_text_to_adf_translates_wiki_markup(self):
        """Fallback wiki markup renders as structured ADF, not literal text."""
        from rootcoz.bug_creation import _text_to_adf

        adf = _text_to_adf(
            """h2. Test Failure
*Test:* {{code}}test.module::case{{code}}

h2. Error
{code}
AssertionError: boom
{code}

* [Jenkins Build|https://jenkins/job/1]
* [Analysis Report|https://rootcoz/r/1]
plain _italic_ and [bare|https://x] tail"""
        )
        nodes = adf["content"]
        # h2. → heading level 2 with inline strong/code marks
        assert nodes[0]["type"] == "heading"
        assert nodes[0]["attrs"]["level"] == 2
        assert nodes[0]["content"][0]["text"] == "Test Failure"
        # *bold* and {{mono}} become marked text nodes. The fallback emits
        # {{code}} markers around the test name, so the literal word "code"
        # is the monospaced content — conversion is faithful to the wiki.
        para = nodes[1]
        assert para["type"] == "paragraph"
        texts = {
            (n["text"], tuple(m["type"] for m in n.get("marks", [])))
            for n in para["content"]
        }
        assert ("Test:", ("strong",)) in texts
        assert ("code", ("code",)) in texts
        assert ("test.module::case", ()) in texts
        # {code} fence → codeBlock with the raw error inside
        code_block = next(n for n in nodes if n["type"] == "codeBlock")
        assert code_block["content"][0]["text"] == "AssertionError: boom"
        # consecutive "* " lines merge into one bulletList with link marks
        bullet_list = next(n for n in nodes if n["type"] == "bulletList")
        assert len(bullet_list["content"]) == 2
        first_item_text = bullet_list["content"][0]["content"][0]["content"][0]
        assert first_item_text["text"] == "Jenkins Build"
        assert first_item_text["marks"] == [
            {"type": "link", "attrs": {"href": "https://jenkins/job/1"}}
        ]
        # inline italic and links in a plain paragraph
        last_para = nodes[-1]
        marks = {
            tuple(m["type"] for m in n.get("marks", [])) for n in last_para["content"]
        }
        assert ("em",) in marks
        assert ("link",) in marks

    async def test_text_to_adf_unterminated_code_fence_keeps_content(self):
        """An unterminated {code} fence keeps its lines instead of dropping them."""
        from rootcoz.bug_creation import _text_to_adf

        adf = _text_to_adf("{code}\nkept line")
        code_block = next(n for n in adf["content"] if n["type"] == "codeBlock")
        assert code_block["content"][0]["text"] == "kept line"
