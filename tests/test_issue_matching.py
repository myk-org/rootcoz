"""Tests for shared issue matching (AI relevance filtering)."""

from unittest.mock import AsyncMock, patch

from ai_cli_runner import AIResult
from jenkins_job_insight.issue_matching import filter_issue_matches_with_ai


class TestFilterIssueMatchesWithAi:
    """Tests for filter_issue_matches_with_ai."""

    async def test_returns_relevant_candidates(self) -> None:
        """Returns only relevant candidates with scores."""
        candidates = [
            {
                "key": "PROJ-1",
                "summary": "Login broken",
                "description": "Login fails",
                "status": "Open",
            },
            {
                "key": "PROJ-2",
                "summary": "Unrelated",
                "description": "Different thing",
                "status": "Closed",
            },
        ]

        ai_response = AIResult(
            success=True,
            text='[{"key": "PROJ-1", "relevant": true, "score": 0.9}, '
            '{"key": "PROJ-2", "relevant": false, "score": 0.1}]',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Login fails",
                bug_description="Login returns 500",
                candidates=candidates,
                ai_provider="claude",
                ai_model="test-model",
                job_id="job-1",
            )

        assert len(results) == 1
        assert results[0]["key"] == "PROJ-1"
        assert results[0]["score"] == 0.9
        assert results[0]["relevant"] is True

    async def test_empty_candidates(self) -> None:
        """Returns empty list for empty candidates."""
        results = await filter_issue_matches_with_ai(
            bug_title="Title",
            bug_description="Desc",
            candidates=[],
            ai_provider="claude",
            ai_model="model",
        )
        assert results == []

    async def test_ai_failure_returns_empty(self) -> None:
        """Returns empty list when AI call fails."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(success=False, text="AI error")

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
                job_id="job-1",
            )

        assert results == []

    async def test_unparseable_response_returns_empty(self) -> None:
        """Returns empty list when AI response can't be parsed."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(success=True, text="not valid json at all")

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
            )

        assert results == []

    async def test_handles_markdown_wrapped_json(self) -> None:
        """Extracts JSON from markdown code blocks."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(
            success=True,
            text='```json\n[{"key": "X-1", "relevant": true, "score": 0.7}]\n```',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
            )

        assert len(results) == 1
        assert results[0]["key"] == "X-1"
        assert results[0]["score"] == 0.7

    async def test_sorted_by_score_descending(self) -> None:
        """Results are sorted by score in descending order."""
        candidates = [
            {"key": "A", "summary": "A", "description": "a", "status": "Open"},
            {"key": "B", "summary": "B", "description": "b", "status": "Open"},
            {"key": "C", "summary": "C", "description": "c", "status": "Open"},
        ]

        ai_response = AIResult(
            success=True,
            text='[{"key": "A", "relevant": true, "score": 0.5}, '
            '{"key": "B", "relevant": true, "score": 0.9}, '
            '{"key": "C", "relevant": true, "score": 0.7}]',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
            )

        assert [r["key"] for r in results] == ["B", "C", "A"]

    async def test_uses_title_field_for_candidates(self) -> None:
        """Candidates with 'title' instead of 'summary' are handled."""
        candidates = [
            {"key": "1", "title": "Issue title", "description": "d", "status": "open"}
        ]

        ai_response = AIResult(
            success=True,
            text='[{"key": "1", "relevant": true, "score": 0.8}]',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
            )

        assert len(results) == 1

    async def test_invalid_score_defaults_to_zero(self) -> None:
        """Invalid score values default to 0.0."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(
            success=True,
            text='[{"key": "X-1", "relevant": true, "score": "invalid"}]',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
            )

        assert len(results) == 1
        assert results[0]["score"] == 0.0

    async def test_skips_non_dict_evaluations(self) -> None:
        """Non-dict entries in evaluations are skipped."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(
            success=True,
            text='["invalid_entry", {"key": "X-1", "relevant": true, "score": 0.5}]',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ),
        ):
            results = await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
            )

        assert len(results) == 1

    async def test_records_token_usage_when_job_id(self) -> None:
        """Token usage is recorded when job_id is provided."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(
            success=True,
            text='[{"key": "X-1", "relevant": false, "score": 0.1}]',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
                job_id="job-123",
                call_type="test_filter",
            )

            mock_record.assert_called_once()
            call_kwargs = mock_record.call_args.kwargs
            assert call_kwargs["job_id"] == "job-123"
            assert call_kwargs["call_type"] == "test_filter"

    async def test_no_token_usage_without_job_id(self) -> None:
        """Token usage is not recorded when no job_id."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(
            success=True,
            text='[{"key": "X-1", "relevant": false, "score": 0.1}]',
        )

        with (
            patch(
                "jenkins_job_insight.issue_matching.call_ai_cli",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "jenkins_job_insight.issue_matching.record_ai_usage",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            await filter_issue_matches_with_ai(
                bug_title="Title",
                bug_description="Desc",
                candidates=candidates,
                ai_provider="claude",
                ai_model="model",
            )

            mock_record.assert_not_called()
