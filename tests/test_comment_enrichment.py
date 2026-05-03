"""Tests for comment enrichment."""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jenkins_job_insight.comment_enrichment import (
    detect_github_issues,
    detect_github_prs,
    detect_jira_keys,
    detect_mentions,
    fetch_github_issue_status,
    fetch_github_pr_status,
    fetch_jira_ticket_status,
)


class TestDetectGitHubPRs:
    def test_detect_pr_url(self):
        prs = detect_github_prs("Fix merged: https://github.com/org/repo/pull/123")
        assert len(prs) == 1
        assert prs[0] == {"owner": "org", "repo": "repo", "number": 123}

    def test_detect_multiple_prs(self):
        text = "See https://github.com/a/b/pull/1 and https://github.com/c/d/pull/2"
        prs = detect_github_prs(text)
        assert len(prs) == 2

    def test_no_prs(self):
        prs = detect_github_prs("just a regular comment")
        assert prs == []


class TestDetectGitHubIssues:
    def test_detect_issue_url(self):
        issues = detect_github_issues(
            "See https://github.com/RedHatQE/mtv-api-tests/issues/359"
        )
        assert len(issues) == 1
        assert issues[0] == {
            "owner": "RedHatQE",
            "repo": "mtv-api-tests",
            "number": 359,
        }

    def test_detect_multiple_issues(self):
        text = "See https://github.com/a/b/issues/1 and https://github.com/c/d/issues/2"
        issues = detect_github_issues(text)
        assert len(issues) == 2

    def test_no_issues(self):
        issues = detect_github_issues("just a regular comment")
        assert issues == []

    def test_does_not_match_pr_urls(self):
        issues = detect_github_issues("https://github.com/org/repo/pull/42")
        assert issues == []


class TestDetectJiraKeys:
    def test_detect_jira_key(self):
        keys = detect_jira_keys("Opened bug: OCPBUGS-12345")
        assert "OCPBUGS-12345" in keys

    def test_detect_multiple_keys(self):
        keys = detect_jira_keys("See OCPBUGS-100 and CNV-200")
        assert len(keys) == 2

    def test_no_keys(self):
        keys = detect_jira_keys("no jira here")
        assert keys == []


class TestDetectMentions:
    def test_detect_single_mention(self):
        result = detect_mentions("Hey @alice, can you check this?")
        assert result == ["alice"]

    def test_detect_multiple_mentions(self):
        result = detect_mentions("@bob and @carol please review")
        assert result == ["bob", "carol"]

    def test_deduplication_preserves_order(self):
        result = detect_mentions("@alice @bob @alice @carol @bob")
        assert result == ["alice", "bob", "carol"]

    def test_no_mentions(self):
        result = detect_mentions("just a regular comment")
        assert result == []

    def test_excludes_email_addresses(self):
        result = detect_mentions("Contact user@domain.com for help")
        assert result == []

    def test_mention_with_hyphens_and_underscores(self):
        result = detect_mentions("@my-user and @another_user")
        assert result == ["my-user", "another_user"]

    def test_mention_with_numbers(self):
        result = detect_mentions("@user123 did this")
        assert result == ["user123"]

    def test_mention_at_start_of_text(self):
        result = detect_mentions("@admin please fix")
        assert result == ["admin"]

    def test_mixed_mentions_and_emails(self):
        result = detect_mentions("@alice and user@domain.com and @bob")
        assert result == ["alice", "bob"]


class TestFetchGitHubPRStatus:
    @pytest.mark.asyncio
    async def test_fetch_merged_pr(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"state": "closed", "merged": True}

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_pr_status("org", "repo", 123)
            assert status == "merged"

    @pytest.mark.asyncio
    async def test_fetch_open_pr(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"state": "open", "merged": False}

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_pr_status("org", "repo", 123)
            assert status == "open"

    @pytest.mark.asyncio
    async def test_fetch_pr_not_found(self):
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_pr_status("org", "repo", 999)
            assert status is None


class TestFetchGitHubIssueStatus:
    @pytest.mark.asyncio
    async def test_fetch_open_issue(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"state": "open"}

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_issue_status("org", "repo", 42)
            assert status == "open"

    @pytest.mark.asyncio
    async def test_fetch_closed_issue(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"state": "closed"}

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_issue_status("org", "repo", 42)
            assert status == "closed"

    @pytest.mark.asyncio
    async def test_fetch_issue_not_found(self):
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_issue_status("org", "repo", 999)
            assert status is None

    @pytest.mark.asyncio
    async def test_fetch_issue_with_token(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"state": "open"}

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_issue_status(
                "org", "repo", 42, token="ghp_test123"
            )
            assert status == "open"
            call_args = mock_instance.get.call_args
            headers = call_args.kwargs.get("headers", call_args[1].get("headers", {}))
            assert headers.get("Authorization") == "Bearer ghp_test123"

    @pytest.mark.asyncio
    async def test_fetch_issue_network_error(self):
        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.side_effect = httpx.ConnectError("connection refused")
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_github_issue_status("org", "repo", 42)
            assert status is None


class TestFetchJiraTicketStatus:
    @pytest.mark.asyncio
    async def test_fetch_status_cloud_v3(self):
        """Cloud API v3 endpoint returns ticket status."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "issues": [
                {
                    "key": "PROJ-100",
                    "fields": {"status": {"name": "Closed"}},
                }
            ]
        }

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_jira_ticket_status(
                "https://myorg.atlassian.net",
                "PROJ-100",
                {},
                auth=("user@example.com", "api-token"),
            )
            assert status == "Closed"

    @pytest.mark.asyncio
    async def test_fallback_from_v3_to_v2(self):
        """When v3 returns 410 Gone, falls back to v2 endpoint."""
        v3_response = MagicMock()
        v3_response.status_code = 410

        v2_response = MagicMock()
        v2_response.status_code = 200
        v2_response.json.return_value = {
            "issues": [
                {
                    "key": "PROJ-200",
                    "fields": {"status": {"name": "Open"}},
                }
            ]
        }

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.side_effect = [v3_response, v2_response]
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_jira_ticket_status(
                "https://jira-server.example.com",
                "PROJ-200",
                {"Authorization": "Bearer pat-token"},
            )
            assert status == "Open"
            assert mock_instance.get.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200_non_410(self):
        """Non-200/non-410 status returns None without trying fallback."""
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_jira_ticket_status(
                "https://jira.example.com",
                "PROJ-300",
                {"Authorization": "Bearer bad-token"},
            )
            assert status is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        """Network errors return None gracefully."""
        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.side_effect = httpx.ConnectError("connection refused")
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_jira_ticket_status(
                "https://jira.example.com",
                "PROJ-400",
                {},
            )
            assert status is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_issues(self):
        """Empty issues list returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"issues": []}

        with patch(
            "jenkins_job_insight.comment_enrichment.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            status = await fetch_jira_ticket_status(
                "https://jira.example.com",
                "PROJ-999",
                {},
            )
            assert status is None
