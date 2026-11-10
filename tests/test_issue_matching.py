"""Tests for shared issue matching (AI relevance filtering + the search loop)."""

import pathlib
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pi_sidecar_client import AIResult

from rootcoz.issue_matching import (
    MAX_FILTER_PROMPT_CHARS,
    TrackerSearchSpec,
    collect_product_bug_reports,
    filter_issue_matches_with_ai,
    group_targets_by_keywords,
    run_tracker_search,
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


class TestGroupTargetsByKeywords:
    """Tests for group_targets_by_keywords — the one grouping rule."""

    def test_same_keywords_in_any_order_are_one_group(self) -> None:
        reports = [
            ProductBugReport(title="A", jira_search_keywords=["login", "auth"]),
            ProductBugReport(title="B", jira_search_keywords=["auth", "login"]),
        ]
        grouped = group_targets_by_keywords(
            reports, lambda report: report.jira_search_keywords
        )
        assert list(grouped) == [("auth", "login")]
        assert [r.title for r in grouped[("auth", "login")]] == ["A", "B"]

    def test_empty_keyword_lists_are_skipped(self) -> None:
        reports = [
            ProductBugReport(title="A"),
            ProductBugReport(title="B", jira_search_keywords=["login"]),
        ]
        grouped = group_targets_by_keywords(
            reports, lambda report: report.jira_search_keywords
        )
        assert list(grouped) == [("login",)]

    def test_first_appearance_order_is_kept(self) -> None:
        reports = [
            ProductBugReport(title="A", jira_search_keywords=["z"]),
            ProductBugReport(title="B", jira_search_keywords=["a"]),
            ProductBugReport(title="C", jira_search_keywords=["z"]),
        ]
        grouped = group_targets_by_keywords(
            reports, lambda report: report.jira_search_keywords
        )
        assert list(grouped) == [("z",), ("a",)]


@dataclass
class _Target:
    """Stands in for a report or code-fix: the loop only needs an identity."""

    name: str


def _candidate(key: str) -> dict[str, Any]:
    """A candidate dict shaped like both trackers' search results."""
    return {
        "key": key,
        "title": f"issue {key}",
        "summary": f"issue {key}",
        "description": "details",
        "status": "open",
    }


class _FakeSpec:
    """Builds a TrackerSearchSpec whose callbacks record every call.

    Every attribute is what a tracker would supply; the defaults describe one
    keyword set over one search unit that finds one candidate.
    """

    def __init__(self) -> None:
        self.searches: list[tuple[tuple[str, ...], str]] = []
        self.bug_contexts: list[str] = []
        self.filtered: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.attached: list[tuple[_Target, list[str]]] = []
        self.search_returns: dict[str, list[dict[str, Any]]] = {}
        self.search_raises: set[str] = set()
        self.filter_returns: list[str] = ["kept"]
        self.filter_side_effect: list[Any] = []
        self.no_filter = False
        self.namespace_prefix = False
        self.bug_title = "Bug title"
        self.bug_description = "Bug description"
        self.label = "Fake search"
        self.search_label = "Fake search(es)"
        self.filter_label = "Fake relevance filter"
        self.scope: tuple[str, ...] = ("SCOPE",)
        self.search_targets: tuple[str, ...] = ("",)

    async def _search(
        self, keywords: tuple[str, ...], target: str
    ) -> list[dict[str, Any]]:
        self.searches.append((keywords, target))
        if target in self.search_raises:
            raise RuntimeError("search broke")
        return self.search_returns.get(target, [_candidate("1")])

    async def _filter(
        self, bug_title: str, bug_description: str, candidates: list[dict[str, Any]]
    ) -> list[str]:
        self.filtered.append((bug_title, bug_description, candidates))
        if self.filter_side_effect:
            outcome = self.filter_side_effect.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return self.filter_returns

    def _namespace(
        self, candidates: list[dict[str, Any]], target: str
    ) -> list[dict[str, Any]]:
        if not self.namespace_prefix:
            return candidates
        return [{**c, "key": f"{target}:{c['key']}"} for c in candidates]

    def _bug_context(self, target: _Target) -> tuple[str, str]:
        self.bug_contexts.append(target.name)
        return self.bug_title, self.bug_description

    def _all_matches(self, candidates: list[dict[str, Any]]) -> list[str]:
        return [f"all:{c['key']}" for c in candidates]

    def _attach(self, target: _Target, matches: list[str]) -> None:
        self.attached.append((target, matches))

    def spec(self) -> TrackerSearchSpec:  # pragma: no cover - trivial builder
        return TrackerSearchSpec(
            label=self.label,
            search_label=self.search_label,
            filter_label=self.filter_label,
            scope=self.scope,
            search_targets=self.search_targets,
            search_one=self._search,
            namespace=self._namespace,
            bug_context=self._bug_context,
            filter_matches=None if self.no_filter else self._filter,
            all_matches=self._all_matches,
            attach=self._attach,
        )


class TestRunTrackerSearch:
    """Tests for the single search loop both trackers now run.

    The record has three outcomes that must stay distinguishable: kept,
    judged-none and not-run — plus "failed", which is not judged-none.
    """

    async def test_kept_attaches_to_every_target_of_the_keyword_set(self) -> None:
        """T1: one filter verdict reaches every target sharing the keywords."""
        fake = _FakeSpec()
        fake.search_targets = ("repo-a", "repo-b")
        fake.search_returns = {"repo-a": [_candidate("1")], "repo-b": [_candidate("2")]}
        fake.filter_returns = ["matched"]
        targets = [_Target("t1"), _Target("t2")]

        record = await run_tracker_search({("kw",): targets}, fake.spec(), "job-1")

        assert record == {
            "searched": True,
            "reason": "",
            "scope": ["SCOPE"],
            "keyword_sets": 1,
            "candidates": 2,
            "matches": 1,
        }
        assert [t.name for t, _ in fake.attached] == ["t1", "t2"]
        assert all(matches == ["matched"] for _, matches in fake.attached)

    async def test_judged_none_is_not_a_failure(self) -> None:
        """T2: the filter ran and kept nothing — an empty reason, no failure."""
        fake = _FakeSpec()
        fake.filter_returns = []

        record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert record["searched"] is True
        assert record["matches"] == 0
        assert record["reason"] == ""
        # Nothing to attach: every target receives the empty judgement.
        assert all(matches == [] for _, matches in fake.attached)

    async def test_one_failed_search_keeps_the_other_units_matches(self) -> None:
        """T3: a failed search contributes no candidate and is named as a fraction."""
        fake = _FakeSpec()
        fake.search_targets = ("repo-a", "repo-b")
        fake.search_raises = {"repo-a"}
        fake.search_returns = {"repo-b": [_candidate("2")]}

        record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert record["searched"] is True
        assert record["candidates"] == 1
        assert len(fake.attached) == 1
        assert "1 of 2" in record["reason"]

    async def test_every_search_failing_is_not_nothing_matched(self) -> None:
        """T4: could not look, never "looked and found nothing"."""
        fake = _FakeSpec()
        fake.search_targets = ("repo-a", "repo-b")
        fake.search_raises = {"repo-a", "repo-b"}

        record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert record["searched"] is False
        assert record["candidates"] == 0
        assert record["matches"] == 0
        assert "all 2" in record["reason"]

    async def test_filter_failure_costs_only_its_keyword_set(self) -> None:
        """T5: a raising filter keeps the later keyword set's matches."""
        fake = _FakeSpec()
        fake.filter_side_effect = [RuntimeError("usage recording failed"), ["kept"]]
        first = [_Target("t1")]
        second = [_Target("t2")]

        record = await run_tracker_search({("a",): first, ("b",): second}, fake.spec())

        assert record["searched"] is True
        assert [t.name for t, _ in fake.attached] == ["t2"]
        assert "call(s) failed" in record["reason"]
        assert "1 Fake relevance filter" in record["reason"]

    async def test_unjudged_filter_is_a_failure_not_an_empty_judgement(self) -> None:
        """T6: a filter with no verdict is counted, never read as kept-nothing."""
        fake = _FakeSpec()
        fake.filter_returns = None  # type: ignore[assignment]

        record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert record["matches"] == 0
        assert "call(s) failed" in record["reason"]
        assert "not run" not in record["reason"]
        assert fake.attached == []

    async def test_oversized_prompt_is_not_run_and_not_a_failed_call(self) -> None:
        """T7: a prompt too large to send is skipped, and said so."""
        fake = _FakeSpec()
        fake.bug_description = "x" * (MAX_FILTER_PROMPT_CHARS + 1)

        record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert fake.filtered == []
        assert record["matches"] == 0
        assert "not run: prompt too large" in record["reason"]
        assert "call(s) failed" not in record["reason"]

    async def test_no_ai_configured_attaches_every_candidate(self) -> None:
        """T8: no filter means nothing judged — every candidate attaches."""
        fake = _FakeSpec()
        fake.no_filter = True
        fake.search_targets = ("repo-a", "repo-b")
        fake.search_returns = {"repo-a": [_candidate("1")], "repo-b": [_candidate("2")]}

        record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert record["matches"] == record["candidates"] == 2
        assert record["reason"] == ""
        assert fake.attached[0][1] == ["all:1", "all:2"]

    async def test_candidates_reach_the_filter_under_the_unit_namespace(self) -> None:
        """T9: per-unit key namespacing is the tracker's hook, applied by the loop."""
        fake = _FakeSpec()
        fake.namespace_prefix = True
        fake.search_targets = ("repo-a", "repo-b")
        fake.search_returns = {"repo-a": [_candidate("1")], "repo-b": [_candidate("1")]}

        await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert [c["key"] for c in fake.filtered[0][2]] == ["repo-a:1", "repo-b:1"]

    async def test_bug_context_is_what_the_prompt_and_filter_receive(self) -> None:
        """T10: the tracker supplies the bug side of the prompt, the loop passes it on."""
        fake = _FakeSpec()
        fake.bug_title = "VM start fails"
        fake.bug_description = "Migration times out"

        with patch(
            "rootcoz.issue_matching.build_filter_prompt", return_value="prompt"
        ) as build:
            await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        candidates = fake.filtered[0][2]
        build.assert_called_once_with(
            "VM start fails", "Migration times out", candidates
        )
        assert fake.filtered[0][:2] == ("VM start fails", "Migration times out")

    async def test_labels_reach_the_reason(self) -> None:
        """T11: the per-tracker label is a parameter, not a hardcoded wording."""
        reasons = []
        for label in ("Jira relevance filter", "GitHub relevance filter"):
            fake = _FakeSpec()
            fake.filter_label = label
            fake.filter_side_effect = [RuntimeError("boom")]
            record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())
            reasons.append(record["reason"])

        assert "Jira relevance filter" in reasons[0]
        assert "GitHub relevance filter" in reasons[1]
        assert "GitHub" not in reasons[0]

    async def test_scope_is_independent_of_the_search_units(self) -> None:
        """T12: Jira records the project key while searching one opaque unit."""
        fake = _FakeSpec()
        fake.scope = ("PROJ",)
        fake.search_targets = ("",)

        record = await run_tracker_search({("kw",): [_Target("t1")]}, fake.spec())

        assert fake.searches == [(("kw",), "")]
        assert record["scope"] == ["PROJ"]
        assert record["keyword_sets"] == 1


class TestOneLoopDrivesBothTrackers:
    """The duplication this consolidation removes must not come back (#524)."""

    def test_no_tracker_module_owns_the_loop(self) -> None:
        """Neither tracker module may fan out or build the reason itself."""
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "rootcoz"
        for name in ("jira.py", "github_issues.py"):
            source = (src / name).read_text()
            assert "asyncio.gather(" not in source, (
                f"{name} runs its own search fan-out — it belongs to "
                "issue_matching.run_tracker_search"
            )
            assert "search_failure_reason(" not in source, (
                f"{name} words its own search reason — it belongs to "
                "issue_matching.run_tracker_search"
            )
