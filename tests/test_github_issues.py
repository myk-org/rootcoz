"""Tests for GitHub Issues integration.

Two questions, two destinations: the code-fix search (``tests_repo_filter`` →
``CodeFix.tests_repo_matches``) and the duplicate search
(``github_duplicate_filter`` → ``ProductBugReport.github_matches``).
"""

import os
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from rootcoz.config import Settings
from rootcoz.github_issues import (
    _CODE_ISSUE_CLASSIFICATIONS,
    GitHubSearchError,
    _collect_code_fix_reports,
    duplicate_search_blocker,
    enrich_with_github_duplicate_matches,
    enrich_with_tests_repo_matches,
    parse_github_repo_url,
    run_repo_urls,
    search_github_issues,
)
from rootcoz.models import (
    AdditionalRepo,
    AnalysisDetail,
    CodeFix,
    FailureAnalysis,
    ProductBugReport,
)

_TEST_GITHUB_TOKEN = "ghp_test_token_123"  # pragma: allowlist secret

_BASE_ENV = {
    "JENKINS_URL": "https://jenkins.example.com",
    "JENKINS_USER": "testuser",
    "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
}


@pytest.fixture
def settings_with_tests_repo() -> Generator[Settings]:
    """Create Settings with TESTS_REPO_URL and GITHUB_TOKEN configured."""
    env = {
        **_BASE_ENV,
        "TESTS_REPO_URL": "https://github.com/org/test-repo",
        "GITHUB_TOKEN": _TEST_GITHUB_TOKEN,
    }
    with patch.dict(os.environ, env, clear=True):
        yield Settings(_env_file=None)


@pytest.fixture
def settings_no_tests_repo() -> Generator[Settings]:
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
    """A failure classified as PRODUCT BUG (should be skipped by the code-fix search)."""
    return FailureAnalysis(
        test_name="test_api",
        error="HTTP 500",
        analysis=AnalysisDetail(
            classification="PRODUCT BUG",
            details="API broken",
        ),
    )


@pytest.fixture
def product_bug_with_keywords() -> FailureAnalysis:
    """A PRODUCT BUG carrying the tracker-neutral AI search keywords."""
    return FailureAnalysis(
        test_name="test_api",
        error="HTTP 500",
        analysis=AnalysisDetail(
            classification="PRODUCT BUG",
            details="API broken",
            product_bug_report=ProductBugReport(
                title="API returns 500 on login",
                description="The login endpoint returns 500",
                jira_search_keywords=["login 500", "api error"],
            ),
        ),
    )


@pytest.fixture
def settings_github_issues_disabled() -> Generator[Settings]:
    """Settings with a token and a repo, but ENABLE_GITHUB_ISSUES explicitly false."""
    env = {
        **_BASE_ENV,
        "TESTS_REPO_URL": "https://github.com/org/test-repo",
        "GITHUB_TOKEN": _TEST_GITHUB_TOKEN,
        "ENABLE_GITHUB_ISSUES": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        yield Settings(_env_file=None)


def _candidate(
    number: int, repo: str = "org/test-repo", title: str | None = None
) -> dict:
    """A GitHub search candidate dict as ``search_github_issues`` returns it."""
    title = title or f"Issue {number}"
    return {
        "key": str(number),
        "title": title,
        "summary": title,
        "description": "details",
        "status": "open",
        "url": f"https://github.com/{repo}/issues/{number}",
        "number": number,
    }


class TestParseGitHubRepoUrl:
    """Tests for parse_github_repo_url."""

    def test_parses_https_url(self) -> None:
        owner, repo = parse_github_repo_url("https://github.com/org/my-repo")
        assert owner == "org"
        assert repo == "my-repo"

    def test_parses_git_url(self) -> None:
        owner, repo = parse_github_repo_url("https://github.com/org/my-repo.git")
        assert owner == "org"
        assert repo == "my-repo"

    def test_parses_trailing_slash(self) -> None:
        owner, repo = parse_github_repo_url("https://github.com/org/my-repo/")
        assert owner == "org"
        assert repo == "my-repo"

    def test_raises_on_invalid_url(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_github_repo_url("https://gitlab.com/org/repo")

    def test_raises_on_non_github_url(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_github_repo_url("not-a-url")


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

        with patch("rootcoz.github_issues.httpx.AsyncClient") as mock_cls:
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

    async def test_api_error_raises(self) -> None:
        """A non-200 is a failed lookup, not an empty result set (#501)."""
        mock_response = httpx.Response(
            403,
            json={"message": "rate limit exceeded"},
            request=httpx.Request("GET", "https://api.github.com/search/issues"),
        )

        with patch("rootcoz.github_issues.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            with pytest.raises(GitHubSearchError, match="403"):
                await search_github_issues(["test"], "https://github.com/org/repo")

        # Verify the outbound query still included repo scope and issue filter
        call_kwargs = mock_client.get.call_args
        query = call_kwargs.kwargs["params"]["q"]
        assert "repo:org/repo" in query
        assert "is:issue" in query

    async def test_network_error_raises(self) -> None:
        """A transport error is a failed lookup, not an empty result set."""
        with patch("rootcoz.github_issues.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            with pytest.raises(GitHubSearchError):
                await search_github_issues(["test"], "https://github.com/org/repo")

    async def test_sends_auth_header_when_token_provided(self) -> None:
        """Includes Authorization header when github_token is provided."""
        mock_response = httpx.Response(
            200,
            json={"items": []},
            request=httpx.Request("GET", "https://api.github.com/search/issues"),
        )

        with patch("rootcoz.github_issues.httpx.AsyncClient") as mock_cls:
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
        with patch("rootcoz.github_issues.search_github_issues") as mock_search:
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
        # The shared runner namespaces candidate keys per repository, so the
        # filter's answer comes back under ``owner/repo#number``.
        mock_evaluations = [
            {"key": "org/test-repo#42", "relevant": True, "score": 0.85},
        ]

        with (
            patch(
                "rootcoz.github_issues.search_github_issues",
                new_callable=AsyncMock,
                return_value=mock_candidates,
            ),
            patch(
                "rootcoz.github_issues.filter_issue_matches_with_ai",
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
            "rootcoz.github_issues.search_github_issues",
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
            "rootcoz.github_issues.search_github_issues",
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
            "rootcoz.github_issues.search_github_issues",
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
            "rootcoz.github_issues.search_github_issues",
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


class TestRunRepoUrls:
    """Tests for run_repo_urls — what the duplicate search may search."""

    def test_strips_credentials_before_parsing(self) -> None:
        """A clone URL carrying userinfo is still searchable, and clean in scope."""
        urls, skipped = run_repo_urls(
            "https://user:tok@github.com/org/repo",  # pragma: allowlist secret
            [],
        )
        assert urls == ["https://github.com/org/repo"]
        assert skipped == 0

    def test_strips_credentials_from_additional_repos(self) -> None:
        urls, skipped = run_repo_urls(
            "",
            [
                AdditionalRepo(
                    name="other",
                    url="https://user:tok@github.com/org/other-repo",  # pragma: allowlist secret
                )
            ],
        )
        assert urls == ["https://github.com/org/other-repo"]
        assert skipped == 0

    def test_strips_clone_ref(self) -> None:
        urls, _ = run_repo_urls("https://github.com/org/repo:develop", [])
        assert urls == ["https://github.com/org/repo"]

    def test_skips_non_github_urls_and_counts_them(self) -> None:
        urls, skipped = run_repo_urls(
            "https://gitlab.com/org/repo",
            [
                AdditionalRepo(name="other", url="https://git.example.com/org/other"),
                {"url": "/local/path"},
            ],
        )
        assert urls == []
        assert skipped == 3

    def test_deduplicates_preserving_order(self) -> None:
        urls, _ = run_repo_urls(
            "https://github.com/org/repo",
            [
                AdditionalRepo(name="same", url="https://github.com/org/repo.git"),
                AdditionalRepo(name="other", url="https://github.com/org/other"),
            ],
        )
        assert urls == [
            "https://github.com/org/repo",
            "https://github.com/org/other",
        ]

    def test_empty_input(self) -> None:
        assert run_repo_urls("", []) == ([], 0)


class TestDuplicateSearchBlocker:
    """Tests for duplicate_search_blocker — the configuration refusals (D8)."""

    def test_names_unsearchable_repositories(self, settings_with_tests_repo) -> None:
        """A configured but non-GitHub scope is not reported as "nothing configured"."""
        urls, skipped = run_repo_urls(
            "https://gitlab.com/org/repo",
            [AdditionalRepo(name="other", url="https://git.example.com/org/other")],
        )
        reason = duplicate_search_blocker(
            settings_with_tests_repo,
            _TEST_GITHUB_TOKEN,
            urls,
            skipped_repos=skipped,
        )
        assert reason
        assert "2" in reason
        assert "not GitHub URLs" in reason

    def test_empty_when_searchable(self, settings_with_tests_repo) -> None:
        assert (
            duplicate_search_blocker(
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                ["https://github.com/org/repo"],
            )
            == ""
        )

    def test_names_missing_token(self, settings_no_tests_repo) -> None:
        reason = duplicate_search_blocker(
            settings_no_tests_repo, "", ["https://github.com/org/repo"]
        )
        assert "GITHUB_TOKEN" in reason

    def test_names_disabled_setting(self, settings_github_issues_disabled) -> None:
        reason = duplicate_search_blocker(
            settings_github_issues_disabled,
            _TEST_GITHUB_TOKEN,
            ["https://github.com/org/repo"],
        )
        assert "ENABLE_GITHUB_ISSUES" in reason


class TestEnrichWithGithubDuplicateMatches:
    """Tests for enrich_with_github_duplicate_matches (the duplicate question)."""

    async def test_attaches_matches_to_product_bug_report(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """Matches land on github_matches, never on the code-fix field (D1)."""
        candidates = [_candidate(42)]

        with (
            patch(
                "rootcoz.github_issues.search_github_issues",
                new_callable=AsyncMock,
                return_value=candidates,
            ),
            patch(
                "rootcoz.github_issues.filter_issue_matches_with_ai",
                new_callable=AsyncMock,
                return_value=[
                    {"key": "org/test-repo#42", "relevant": True, "score": 0.9}
                ],
            ) as filter_mock,
        ):
            record = await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=["https://github.com/org/test-repo"],
            )

        report = product_bug_with_keywords.analysis.product_bug_report
        assert len(report.github_matches) == 1
        assert report.github_matches[0].number == 42
        assert report.github_matches[0].url.endswith("/issues/42")
        # The two fields answer different questions and are never merged.
        assert not product_bug_with_keywords.analysis.code_fix
        # The shared AI filter is the mechanism, with the duplicate call type.
        assert filter_mock.call_args.kwargs["call_type"] == "github_duplicate_filter"
        assert record["searched"] is True
        assert record["matches"] == 1
        assert record["scope"] == ["https://github.com/org/test-repo"]

    async def test_searches_every_run_repository(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """One search per GitHub repository per keyword set; matches deduped by url."""
        shared = _candidate(7)

        async def search(_keywords, _repo_url, _token=""):
            return [shared]

        with (
            patch(
                "rootcoz.github_issues.search_github_issues", side_effect=search
            ) as search_mock,
            patch(
                "rootcoz.github_issues.filter_issue_matches_with_ai",
                new_callable=AsyncMock,
                return_value=[
                    {"key": "org/test-repo#7", "relevant": True, "score": 0.9}
                ],
            ),
        ):
            await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=[
                    "https://github.com/org/test-repo",
                    "https://github.com/org/other-repo",
                ],
            )

        assert search_mock.await_count == 2
        report = product_bug_with_keywords.analysis.product_bug_report
        assert len(report.github_matches) == 1

    async def test_same_issue_number_in_two_repositories_does_not_collide(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """Per-repo issue numbers collide without namespaced candidate keys."""
        repo_a = _candidate(42, repo="org/test-repo", title="A issue")
        repo_b = _candidate(42, repo="org/other-repo", title="B issue")

        async def search(keywords, repo_url, token=""):
            return [repo_a] if repo_url.endswith("/test-repo") else [repo_b]

        with (
            patch("rootcoz.github_issues.search_github_issues", side_effect=search),
            patch(
                "rootcoz.github_issues.filter_issue_matches_with_ai",
                new_callable=AsyncMock,
                return_value=[
                    {"key": "org/test-repo#42", "relevant": True, "score": 0.9},
                    {"key": "org/other-repo#42", "relevant": True, "score": 0.9},
                ],
            ) as filter_mock,
        ):
            await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=[
                    "https://github.com/org/test-repo",
                    "https://github.com/org/other-repo",
                ],
            )

        keys = {c["key"] for c in filter_mock.call_args.kwargs["candidates"]}
        assert keys == {"org/test-repo#42", "org/other-repo#42"}
        report = product_bug_with_keywords.analysis.product_bug_report
        urls = {m.url for m in report.github_matches}
        assert urls == {
            "https://github.com/org/test-repo/issues/42",
            "https://github.com/org/other-repo/issues/42",
        }

    async def test_reports_no_token_reason(
        self, product_bug_with_keywords, settings_no_tests_repo
    ) -> None:
        """No deployment credential: a reason, no request, no raise (D8)."""
        with patch(
            "rootcoz.github_issues.search_github_issues", new_callable=AsyncMock
        ) as search_mock:
            record = await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_no_tests_repo,
                "",
                "claude",
                "test-model",
                repo_urls=["https://github.com/org/test-repo"],
            )

        assert record["searched"] is False
        assert "GITHUB_TOKEN" in record["reason"]
        search_mock.assert_not_called()

    async def test_successful_search_with_no_candidates_is_not_a_failure(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """Looking and finding nothing is not the same as not looking (#501)."""
        with patch(
            "rootcoz.github_issues.search_github_issues",
            new_callable=AsyncMock,
            return_value=[],
        ):
            record = await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=["https://github.com/org/test-repo"],
            )

        assert record == {
            "searched": True,
            "reason": "",
            "scope": ["https://github.com/org/test-repo"],
            "keyword_sets": 1,
            "candidates": 0,
            "matches": 0,
        }
        report = product_bug_with_keywords.analysis.product_bug_report
        assert report.github_matches == []

    async def test_reports_no_repository_reason(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """No repository in scope: the reason names the scope."""
        with patch(
            "rootcoz.github_issues.search_github_issues", new_callable=AsyncMock
        ) as search_mock:
            record = await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=[],
            )

        assert record["searched"] is False
        assert "repository" in record["reason"]
        search_mock.assert_not_called()

    async def test_reports_no_keywords_reason(
        self, product_bug_failure, settings_with_tests_repo
    ) -> None:
        """A CODE-ISSUE-only analysis records why nothing was searched."""
        with patch(
            "rootcoz.github_issues.search_github_issues", new_callable=AsyncMock
        ) as search_mock:
            record = await enrich_with_github_duplicate_matches(
                [product_bug_failure],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=["https://github.com/org/test-repo"],
            )

        assert record["searched"] is False
        assert "keywords" in record["reason"]
        search_mock.assert_not_called()

    async def test_reports_disabled_reason(
        self, product_bug_with_keywords, settings_github_issues_disabled
    ) -> None:
        """ENABLE_GITHUB_ISSUES=false disables the read-only search too."""
        record = await enrich_with_github_duplicate_matches(
            [product_bug_with_keywords],
            settings_github_issues_disabled,
            _TEST_GITHUB_TOKEN,
            "claude",
            "test-model",
            repo_urls=["https://github.com/org/test-repo"],
        )
        assert record["searched"] is False
        assert "ENABLE_GITHUB_ISSUES" in record["reason"]

    async def test_all_searches_failing_sets_searched_false(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """Every search failing is "could not look", never "no duplicates"."""
        with patch(
            "rootcoz.github_issues.search_github_issues",
            new_callable=AsyncMock,
            side_effect=GitHubSearchError("403 rate limited"),
        ):
            record = await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=["https://github.com/org/test-repo"],
            )

        assert record["searched"] is False
        assert "1" in record["reason"]
        assert "failed" in record["reason"]

    async def test_partial_search_failure_still_attaches(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """A partial failure keeps searched=True and names the fraction."""
        good = _candidate(11)

        async def search(keywords, repo_url, token=""):
            if repo_url.endswith("/test-repo"):
                raise GitHubSearchError("boom")
            return [good]

        with (
            patch("rootcoz.github_issues.search_github_issues", side_effect=search),
            patch(
                "rootcoz.github_issues.filter_issue_matches_with_ai",
                new_callable=AsyncMock,
                return_value=[
                    {"key": "org/other-repo#11", "relevant": True, "score": 0.9}
                ],
            ),
        ):
            record = await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=[
                    "https://github.com/org/test-repo",
                    "https://github.com/org/other-repo",
                ],
            )

        assert record["searched"] is True
        assert record["matches"] == 1
        assert "1 of 2" in record["reason"]

    async def test_filter_failure_does_not_discard_later_keyword_sets(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """A raising relevance filter costs its own keyword set only."""
        other = FailureAnalysis(
            test_name="test_other",
            error="timeout",
            analysis=AnalysisDetail(
                classification="PRODUCT BUG",
                product_bug_report=ProductBugReport(
                    title="VM start fails",
                    description="Migration start times out",
                    jira_search_keywords=["vm start"],
                ),
            ),
        )

        async def search(keywords, _repo_url, _token=""):
            return [_candidate(1 if "login 500" in keywords else 2)]

        with (
            patch("rootcoz.github_issues.search_github_issues", side_effect=search),
            patch(
                "rootcoz.github_issues.filter_issue_matches_with_ai",
                new_callable=AsyncMock,
                side_effect=[
                    RuntimeError("usage recording failed"),
                    [{"key": "org/test-repo#2", "relevant": True, "score": 0.9}],
                ],
            ),
        ):
            record = await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords, other],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=["https://github.com/org/test-repo"],
            )

        # The searches succeeded, so the record must not claim otherwise.
        assert record["searched"] is True
        assert record["candidates"] == 2
        assert "filter" in record["reason"]
        # The later keyword set still ran and its matches were attached.
        assert other.analysis.product_bug_report.github_matches[0].number == 2
        assert (
            product_bug_with_keywords.analysis.product_bug_report.github_matches == []
        )

    async def test_no_write_request_is_made(
        self, product_bug_with_keywords, settings_with_tests_repo
    ) -> None:
        """The search is read-only: one GET to the search API, nothing else (D9)."""
        requests: list[tuple[str, str]] = []

        class _RecordingClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc) -> bool:
                return False

            async def get(self, url, **kwargs):
                requests.append(("GET", url))
                return httpx.Response(
                    200,
                    json={"items": []},
                    request=httpx.Request("GET", url),
                )

        with (
            patch("rootcoz.github_issues.httpx.AsyncClient", _RecordingClient),
            patch(
                "rootcoz.github_issues.filter_issue_matches_with_ai",
                new_callable=AsyncMock,
            ),
        ):
            await enrich_with_github_duplicate_matches(
                [product_bug_with_keywords],
                settings_with_tests_repo,
                _TEST_GITHUB_TOKEN,
                "claude",
                "test-model",
                repo_urls=["https://github.com/org/test-repo"],
            )

        assert requests == [("GET", "https://api.github.com/search/issues")]
