"""Tests for shared issue matching (AI relevance filtering)."""

from unittest.mock import AsyncMock, patch

import pytest
from pi_sidecar_client import AIResult

from rootcoz.issue_matching import (
    collect_product_bug_reports,
    filter_issue_matches_with_ai,
    tracker_search,
)
from rootcoz.models import (
    AnalysisDetail,
    FailureAnalysis,
    ProductBugReport,
)


@pytest.fixture
def product_bug_failure() -> FailureAnalysis:
    """A PRODUCT BUG failure carrying a bug report."""
    return FailureAnalysis(
        test_name="test_login",
        error="HTTP 500",
        analysis=AnalysisDetail(
            classification="PRODUCT BUG",
            details="Auth service broken",
            product_bug_report=ProductBugReport(title="Login fails"),
        ),
    )


@pytest.fixture
def code_issue_failure() -> FailureAnalysis:
    """A CODE ISSUE failure with no bug report."""
    return FailureAnalysis(
        test_name="test_config",
        error="ImportError",
        analysis=AnalysisDetail(classification="CODE ISSUE", details="Missing import"),
    )


class TestCollectProductBugReports:
    """Tests for collect_product_bug_reports (shared by both trackers)."""

    def test_collects_product_bugs(self, product_bug_failure) -> None:
        reports = collect_product_bug_reports([product_bug_failure])
        assert len(reports) == 1
        assert reports[0].title == "Login fails"

    def test_skips_code_issues(self, code_issue_failure) -> None:
        reports = collect_product_bug_reports([code_issue_failure])
        assert len(reports) == 0

    def test_mixed_failures(self, product_bug_failure, code_issue_failure) -> None:
        reports = collect_product_bug_reports([product_bug_failure, code_issue_failure])
        assert len(reports) == 1

    def test_empty_list(self) -> None:
        reports = collect_product_bug_reports([])
        assert len(reports) == 0


class TestTrackerSearchBuilder:
    """Tests for the single TrackerSearch builder (D6)."""

    def test_defaults_are_a_refusal_with_no_scope(self) -> None:
        record = tracker_search(reason="because")
        assert record == {
            "searched": False,
            "reason": "because",
            "scope": [],
            "keyword_sets": 0,
            "candidates": 0,
            "matches": 0,
        }

    def test_carries_every_documented_key(self) -> None:
        record = tracker_search(
            searched=True,
            scope=["https://github.com/org/repo"],
            keyword_sets=2,
            candidates=3,
            matches=1,
        )
        assert set(record) == {
            "searched",
            "reason",
            "scope",
            "keyword_sets",
            "candidates",
            "matches",
        }
        assert record["searched"] is True
        assert record["reason"] == ""


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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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

    async def test_ai_failure_returns_none(self) -> None:
        """Returns None when the AI call fails, so "could not judge" is visible."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(success=False, text="AI error")

        with (
            patch(
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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

        assert results is None

    async def test_unparseable_response_returns_none(self) -> None:
        """Returns None when the AI response can't be parsed."""
        candidates = [
            {"key": "X-1", "summary": "Bug", "description": "d", "status": "Open"}
        ]

        ai_response = AIResult(success=True, text="not valid json at all")

        with (
            patch(
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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

        assert results is None

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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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
                "rootcoz.issue_matching.call_ai_once",
                new_callable=AsyncMock,
                return_value=ai_response,
            ),
            patch(
                "rootcoz.token_tracking.record_ai_usage",
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
