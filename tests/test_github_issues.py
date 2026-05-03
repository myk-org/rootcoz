"""Tests for GitHub Issues integration (tests repo issue matching)."""

import os
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from jenkins_job_insight.config import Settings
from jenkins_job_insight.github_issues import (
    _CODE_ISSUE_CLASSIFICATIONS,
    _collect_code_fix_reports,
    _parse_github_repo_url,
    enrich_with_tests_repo_matches,
    search_github_issues,
)
from jenkins_job_insight.models import (
    AnalysisDetail,
    CodeFix,
    FailureAnalysis,
)


_TEST_GITHUB_TOKEN = "ghp_test_token_123"  # noqa: S105  # pragma: allowlist secret

_BASE_ENV = {
    "JENKINS_URL": "https://jenkins.example.com",
    "JENKINS_USER": "testuser",
    "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
}


@pytest.fixture
def settings_with_tests_repo() -> Generator[Settings, None, None]:
    """Create Settings with TESTS_REPO_URL and GITHUB_TOKEN configured."""
    env = {
        **_BASE_ENV,
        "TESTS_REPO_URL": "https://github.com/org/test-repo",
        "GITHUB_TOKEN": _TEST_GITHUB_TOKEN,
    }
    with patch.dict(os.environ, env, clear=True):
        yield Settings(_env_file=None)


@pytest.fixture
def settings_no_tests_repo() -> Generator[Settings, None, None]:
    """Create Settings without TESTS_REPO_URL."""
    with patch.dict(os.environ, _BASE_ENV, clear=True):
        yield Settings(_env_file=None)


@pytest.fixture
def code_issue_failure() -> FailureAnalysis:
    """A failure classified as CODE ISSUE with search keywords."""
    return FailureAnalysis(
        test_name="test_login_form",
        error="AssertionError: element not found",
        analysis=AnalysisDetail(
            classification="CODE ISSUE",
            details="Test selector is outdated",
            code_fix=CodeFix(
                file="tests/test_login.py",
                line="42",
                change="Update CSS selector",
                tests_repo_search_keywords=[
                    "login form selector",
                    "element not found login",
                    "CSS selector update",
                ],
            ),
        ),
    )


@pytest.fixture
def code_issue_no_keywords() -> FailureAnalysis:
    """A CODE ISSUE failure without search keywords."""
    return FailureAnalysis(
        test_name="test_config",
        error="ImportError",
        analysis=AnalysisDetail(
            classification="CODE ISSUE",
            details="Missing import",
            code_fix=CodeFix(
                file="tests/test_config.py",
                line="10",
                change="Add missing import",
            ),
        ),
    )


@pytest.fixture
def product_bug_failure() -> FailureAnalysis:
    """A failure classified as PRODUCT BUG (should be skipped)."""
    return FailureAnalysis(
        test_name="test_api",
        error="HTTP 500",
        analysis=AnalysisDetail(
            classification="PRODUCT BUG",
            details="API broken",
        ),
    )


class TestParseGitHubRepoUrl:
    """Tests for _parse_github_repo_url."""

    def test_parses_https_url(self) -> None:
        owner, repo = _parse_github_repo_url("https://github.com/org/my-repo")
        assert owner == "org"
        assert repo == "my-repo"

    def test_parses_git_url(self) -> None:
        owner, repo = _parse_github_repo_url("https://github.com/org/my-repo.git")
        assert owner == "org"
        assert repo == "my-repo"

    def test_parses_trailing_slash(self) -> None:
        owner, repo = _parse_github_repo_url("https://github.com/org/my-repo/")
        assert owner == "org"
        assert repo == "my-repo"

    def test_raises_on_invalid_url(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_github_repo_url("https://gitlab.com/org/repo")

    def test_raises_on_non_github_url(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_github_repo_url("not-a-url")


class TestSearchGitHubIssues:
    """Tests for search_github_issues."""

    async def test_returns_candidates(self) -> None:
        """Search returns candidate dicts from GitHub API."""
        mock_response = httpx.Response(
            200,
            json={
                "items": [
                    {
                        "number": 42,
                        "title": "Login selector broken",
                        "body": "The CSS selector for login form is outdated",
                        "state": "open",
                        "html_url": "https://github.com/org/repo/issues/42",
                    },
                ]
            },
            request=httpx.Request("GET", "https://api.github.com/search/issues"),
        )

        with patch("jenkins_job_insight.github_issues.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            candidates = await search_github_issues(
                ["login selector"],
                "https://github.com/org/repo",
                _TEST_GITHUB_TOKEN,
            )

        assert len(candidates) == 1
        assert candidates[0]["key"] == "42"
        assert candidates[0]["title"] == "Login selector broken"
        assert candidates[0]["number"] == 42
        assert candidates[0]["status"] == "open"
        assert candidates[0]["url"] == "https://github.com/org/repo/issues/42"

        # Verify the outbound query includes repo scope and issue filter
        call_kwargs = mock_client.get.call_args
        query = call_kwargs.kwargs["params"]["q"]
        assert "repo:org/repo" in query
        assert "is:issue" in query

    async def test_empty_keywords(self) -> None:
        """Search with empty keywords returns empty list."""
        result = await search_github_issues([], "https://github.com/org/repo")
        assert result == []

    async def test_invalid_repo_url(self) -> None:
        """Search with invalid repo URL returns empty list."""
        result = await search_github_issues(["test"], "https://gitlab.com/org/repo")
        assert result == []

    async def test_api_error_returns_empty(self) -> None:
        """GitHub API errors return empty list (never raises)."""
        mock_response = httpx.Response(
            403,
            json={"message": "rate limit exceeded"},
            request=httpx.Request("GET", "https://api.github.com/search/issues"),
        )

        with patch("jenkins_job_insight.github_issues.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await search_github_issues(["test"], "https://github.com/org/repo")

        assert result == []

        # Verify the outbound query still included repo scope and issue filter
        call_kwargs = mock_client.get.call_args
        query = call_kwargs.kwargs["params"]["q"]
        assert "repo:org/repo" in query
        assert "is:issue" in query

    async def test_network_error_returns_empty(self) -> None:
        """Network errors return empty list (never raises)."""
        with patch("jenkins_job_insight.github_issues.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await search_github_issues(["test"], "https://github.com/org/repo")

        assert result == []

    async def test_sends_auth_header_when_token_provided(self) -> None:
        """Includes Authorization header when github_token is provided."""
        mock_response = httpx.Response(
            200,
            json={"items": []},
            request=httpx.Request("GET", "https://api.github.com/search/issues"),
        )

        with patch("jenkins_job_insight.github_issues.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await search_github_issues(
                ["test"], "https://github.com/org/repo", _TEST_GITHUB_TOKEN
            )

            call_kwargs = mock_client.get.call_args
            headers = call_kwargs.kwargs.get("headers", {})
            assert headers["Authorization"] == f"Bearer {_TEST_GITHUB_TOKEN}"


class TestCollectCodeFixReports:
    """Tests for _collect_code_fix_reports."""

    def test_collects_code_issues(self, code_issue_failure) -> None:
        reports = _collect_code_fix_reports([code_issue_failure])
        assert len(reports) == 1
        assert reports[0][1].file == "tests/test_login.py"

    def test_skips_product_bugs(self, product_bug_failure) -> None:
        reports = _collect_code_fix_reports([product_bug_failure])
        assert len(reports) == 0

    def test_mixed_failures(self, code_issue_failure, product_bug_failure) -> None:
        reports = _collect_code_fix_reports([code_issue_failure, product_bug_failure])
        assert len(reports) == 1

    def test_empty_list(self) -> None:
        reports = _collect_code_fix_reports([])
        assert len(reports) == 0

    def test_code_issue_without_code_fix(self) -> None:
        """CODE ISSUE without a CodeFix object is skipped."""
        failure = FailureAnalysis(
            test_name="test_x",
            error="err",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="some issue",
            ),
        )
        reports = _collect_code_fix_reports([failure])
        assert len(reports) == 0

    def test_classification_case_insensitive(self) -> None:
        """Classification matching is case-insensitive."""
        failure = FailureAnalysis(
            test_name="test_x",
            error="err",
            analysis=AnalysisDetail(
                classification="code issue",
                details="some issue",
                code_fix=CodeFix(file="f.py", line="1", change="fix"),
            ),
        )
        reports = _collect_code_fix_reports([failure])
        assert len(reports) == 1


class TestEnrichWithTestsRepoMatches:
    """Tests for enrich_with_tests_repo_matches."""

    async def test_skips_when_no_tests_repo(
        self, code_issue_failure, settings_no_tests_repo
    ) -> None:
        """Does nothing when TESTS_REPO_URL is not configured."""
        await enrich_with_tests_repo_matches(
            [code_issue_failure], settings_no_tests_repo
        )
        assert code_issue_failure.analysis.code_fix.tests_repo_matches == []

    async def test_skips_when_no_code_issues(
        self, product_bug_failure, settings_with_tests_repo
    ) -> None:
        """Does nothing when no CODE ISSUE failures exist."""
        await enrich_with_tests_repo_matches(
            [product_bug_failure], settings_with_tests_repo
        )

    async def test_skips_when_no_keywords(
        self, code_issue_no_keywords, settings_with_tests_repo
    ) -> None:
        """Does nothing when CODE ISSUE has no search keywords."""
        with patch(
            "jenkins_job_insight.github_issues.search_github_issues"
        ) as mock_search:
            await enrich_with_tests_repo_matches(
                [code_issue_no_keywords], settings_with_tests_repo
            )
            mock_search.assert_not_called()

    async def test_enriches_with_ai_filtering(
        self, code_issue_failure, settings_with_tests_repo
    ) -> None:
        """Searches GitHub then uses AI to filter relevant matches."""
        mock_candidates = [
            {
                "key": "42",
                "title": "Login selector broken",
                "summary": "Login selector broken",
                "description": "The CSS selector is outdated",
                "status": "open",
                "url": "https://github.com/org/repo/issues/42",
                "number": 42,
            },
        ]
        mock_evaluations = [
            {"key": "42", "relevant": True, "score": 0.85},
        ]

        with (
            patch(
                "jenkins_job_insight.github_issues.search_github_issues",
                new_callable=AsyncMock,
                return_value=mock_candidates,
            ),
            patch(
                "jenkins_job_insight.github_issues.filter_issue_matches_with_ai",
                new_callable=AsyncMock,
                return_value=mock_evaluations,
            ),
        ):
            await enrich_with_tests_repo_matches(
                [code_issue_failure],
                settings_with_tests_repo,
                "claude",
                "test-model",
            )

        code_fix = code_issue_failure.analysis.code_fix
        assert len(code_fix.tests_repo_matches) == 1
        assert code_fix.tests_repo_matches[0].number == 42
        assert code_fix.tests_repo_matches[0].title == "Login selector broken"
        assert code_fix.tests_repo_matches[0].status == "open"

    async def test_fallback_without_ai_config(
        self, code_issue_failure, settings_with_tests_repo
    ) -> None:
        """Returns all candidates when no AI provider is configured."""
        mock_candidates = [
            {
                "key": "10",
                "title": "Some issue",
                "summary": "Some issue",
                "description": "Details",
                "status": "open",
                "url": "https://github.com/org/repo/issues/10",
                "number": 10,
            },
        ]

        with patch(
            "jenkins_job_insight.github_issues.search_github_issues",
            new_callable=AsyncMock,
            return_value=mock_candidates,
        ):
            # No ai_provider/ai_model — should fall back
            await enrich_with_tests_repo_matches(
                [code_issue_failure], settings_with_tests_repo
            )

        code_fix = code_issue_failure.analysis.code_fix
        assert len(code_fix.tests_repo_matches) == 1
        assert code_fix.tests_repo_matches[0].number == 10

    async def test_deduplicates_by_keyword_set(self, settings_with_tests_repo) -> None:
        """Same keyword set causes only one GitHub search."""
        failure1 = FailureAnalysis(
            test_name="test_a",
            error="err",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="issue",
                code_fix=CodeFix(
                    file="a.py",
                    line="1",
                    change="fix",
                    tests_repo_search_keywords=["login", "selector"],
                ),
            ),
        )
        failure2 = FailureAnalysis(
            test_name="test_b",
            error="err",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="issue",
                code_fix=CodeFix(
                    file="b.py",
                    line="2",
                    change="fix",
                    tests_repo_search_keywords=["selector", "login"],
                ),
            ),
        )

        with patch(
            "jenkins_job_insight.github_issues.search_github_issues",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_search:
            await enrich_with_tests_repo_matches(
                [failure1, failure2], settings_with_tests_repo
            )
            assert mock_search.call_count == 1

    async def test_never_raises(
        self, code_issue_failure, settings_with_tests_repo
    ) -> None:
        """GitHub errors are caught and logged, never raised."""
        with patch(
            "jenkins_job_insight.github_issues.search_github_issues",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            # Should not raise
            await enrich_with_tests_repo_matches(
                [code_issue_failure], settings_with_tests_repo
            )

        assert code_issue_failure.analysis.code_fix.tests_repo_matches == []

    async def test_shared_matches_across_same_keyword_failures(
        self, settings_with_tests_repo
    ) -> None:
        """Matches are attached to all code_fix objects with the same keywords."""
        fix_kwargs = {
            "file": "x.py",
            "line": "1",
            "change": "fix",
            "tests_repo_search_keywords": ["keyword_a", "keyword_b"],
        }
        failure1 = FailureAnalysis(
            test_name="test_a",
            error="err",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="issue",
                code_fix=CodeFix(**fix_kwargs),
            ),
        )
        failure2 = FailureAnalysis(
            test_name="test_b",
            error="err",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="issue",
                code_fix=CodeFix(**fix_kwargs),
            ),
        )

        mock_candidates = [
            {
                "key": "5",
                "title": "Related issue",
                "summary": "Related issue",
                "description": "desc",
                "status": "open",
                "url": "https://github.com/org/repo/issues/5",
                "number": 5,
            },
        ]

        with patch(
            "jenkins_job_insight.github_issues.search_github_issues",
            new_callable=AsyncMock,
            return_value=mock_candidates,
        ):
            await enrich_with_tests_repo_matches(
                [failure1, failure2], settings_with_tests_repo
            )

        # Both failures should have the same matches
        assert len(failure1.analysis.code_fix.tests_repo_matches) == 1
        assert len(failure2.analysis.code_fix.tests_repo_matches) == 1
        assert failure1.analysis.code_fix.tests_repo_matches[0].number == 5
        assert failure2.analysis.code_fix.tests_repo_matches[0].number == 5


class TestCodeIssueClassifications:
    """Tests for classification matching."""

    def test_code_issue_in_classifications(self) -> None:
        assert "CODE ISSUE" in _CODE_ISSUE_CLASSIFICATIONS
