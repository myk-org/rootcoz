"""Tests for the anchor/v2 failure signature pair (issue #239).

Changing the normalization rules changes the hash a new analysis produces.
Stored rows keep the hashes they were written with -- nothing is ever
re-signed -- so every row carries two columns:

    error_signature      anchor, frozen pre-v2 rules, never rewritten
    error_signature_v2   current rules, NULL on rows written before them

and every comparison goes through ``storage.signatures_match`` /
``storage.resolve_signature`` so a failure analysed today still matches its own
history from before the rules changed.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from pi_sidecar_client import AIResult

from rootcoz import storage
from rootcoz.engine.core import (
    compute_legacy_signature,
    compute_signature,
    get_failure_signature,
    get_legacy_signature,
    run_single_ai_analysis,
)
from rootcoz.models import FailedTest
from rootcoz.peer_analysis import _build_failure_summary
from rootcoz.token_tracking import (
    attach_failure_usage,
    failure_group_usage,
    record_ai_usage,
)

# A failure whose text the v2 rules normalize away and the frozen rules do not:
# two runs of it differ only in per-request HTTP header noise.
HEADER_NOISE_A = (
    "HTTPError 502 Bad Gateway\nDate: Sun, 31 May 2026 06:50:48 GMT\n"
    "Content-Length: 812\nX-Request-Id: 9c1f7d2e-1111-2222-3333-444455556666"
)
HEADER_NOISE_B = (
    "HTTPError 502 Bad Gateway\nDate: Mon, 02 Jan 2027 23:59:01 GMT\n"
    "Content-Length: 4096\nX-Request-Id: aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"
)


def failure_a() -> FailedTest:
    return FailedTest(
        test_name="test_gateway", error_message=HEADER_NOISE_A, stack_trace="await r"
    )


def failure_b() -> FailedTest:
    """The same failure one run later: same text, different header noise."""
    return FailedTest(
        test_name="test_gateway", error_message=HEADER_NOISE_B, stack_trace="await r"
    )


def dual_written_row(failure: FailedTest) -> dict[str, str]:
    """A row as written after the v2 rules landed."""
    return {
        "error_signature": get_legacy_signature(failure),
        "error_signature_v2": get_failure_signature(failure),
    }


def legacy_row(failure: FailedTest) -> dict[str, str]:
    """A row as written before them: anchor only."""
    return {"error_signature": get_legacy_signature(failure)}


@pytest.fixture
async def db(temp_db_path: Path):
    """Initialized database on a tmp file; function-scoped, no shared state."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


async def _add_history_row(
    db_path: Path,
    job_id: str,
    test_name: str,
    *,
    anchor: str,
    v2: str | None,
    username: str = "",
    message: str = "boom",
) -> None:
    """Insert a failure_history row, dual-written or legacy-only."""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO failure_history (job_id, job_name, build_number, test_name,"
            " error_message, error_signature, error_signature_v2, classification)"
            " VALUES (?, 'my-job', 1, ?, ?, ?, ?, 'CODE ISSUE')",
            (job_id, test_name, message, anchor, v2),
        )
        if username:
            await conn.execute(
                "INSERT INTO failure_reviews (job_id, test_name, reviewed, username)"
                " VALUES (?, ?, 1, ?)",
                (job_id, test_name, username),
            )
        await conn.commit()


# ---------------------------------------------------------------------------
# The two hashes
# ---------------------------------------------------------------------------
class TestAnchorAndV2Differ:
    def test_header_noise_changes_the_v2_hash_only(self):
        """The v2 rules collapse this failure; the frozen anchor keeps it apart."""
        assert compute_signature(HEADER_NOISE_A, "await r") == compute_signature(
            HEADER_NOISE_B, "await r"
        )
        assert compute_legacy_signature(HEADER_NOISE_A, "await r") != (
            compute_legacy_signature(HEADER_NOISE_B, "await r")
        )

    def test_anchor_is_identical_for_identical_text(self):
        """Same text, same anchor -- that is what makes old rows match new ones."""
        assert get_legacy_signature(failure_a()) == get_legacy_signature(failure_a())

    def test_frozen_rules_still_normalize_what_they_always_did(self):
        """The anchor is not 'raw text': it keeps the original normalizations."""
        stamped = "Error on pod my-pod-abc123 build/123"
        moved = "Error on pod my-pod-def456 build/456"
        assert get_legacy_signature(
            FailedTest(test_name="t", error_message=stamped)
        ) == get_legacy_signature(FailedTest(test_name="t", error_message=moved))


# ---------------------------------------------------------------------------
# Resolution: the single rule
# ---------------------------------------------------------------------------
class TestResolveSignature:
    def test_dual_written_row_resolves_to_v2(self):
        assert storage.resolve_signature(dual_written_row(failure_a())) == (
            get_failure_signature(failure_a())
        )

    def test_legacy_row_resolves_to_its_anchor(self):
        assert storage.resolve_signature(legacy_row(failure_a())) == (
            get_legacy_signature(failure_a())
        )

    def test_resolves_a_sqlite_row_too(self):
        """A Row is not a dict; reading one as if it were yields "" silently."""
        assert storage.resolve_signature({"error_signature": "a"}) == "a"
        assert storage.signature_hashes(
            {"error_signature": "a", "error_signature_v2": None}
        ) == ["a"]


# ---------------------------------------------------------------------------
# Matching: legacy row and new row for the SAME failure
# ---------------------------------------------------------------------------
class TestSignaturesMatch:
    def test_legacy_row_and_new_row_match(self):
        """The history-preservation guarantee, at the helper level."""
        assert storage.signatures_match(
            legacy_row(failure_a()), dual_written_row(failure_a())
        )

    def test_legacy_row_matches_another_legacy_row(self):
        assert storage.signatures_match(
            legacy_row(failure_a()), legacy_row(failure_a())
        )

    def test_v2_only_row_matches_on_v2(self):
        v2 = get_failure_signature(failure_b())
        row = {"error_signature_v2": v2}
        assert storage.resolve_signature(row) == v2
        assert storage.signatures_match(row, dual_written_row(failure_b()))

    def test_unrelated_rows_do_not_match(self):
        other = FailedTest(test_name="test_other", error_message="NullPointerException")
        assert not storage.signatures_match(
            legacy_row(failure_a()), dual_written_row(other)
        )

    def test_row_without_any_signature_never_matches(self):
        assert not storage.signatures_match({}, {"error_signature": "a"})
        assert storage.signature_hashes({}) == []


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
class TestSchema:
    async def test_v2_column_exists_on_every_signature_table(self, db: Path):
        async with aiosqlite.connect(db) as conn:
            for table in ("comments", "failure_history", "ai_token_usage"):
                cursor = await conn.execute(f"PRAGMA table_info({table})")
                columns = {row[1] for row in await cursor.fetchall()}
                assert "error_signature" in columns
                assert "error_signature_v2" in columns, table

    async def test_v2_is_indexed(self, db: Path):
        """The v2 branch of the OR needs its own index or it table-scans."""
        async with aiosqlite.connect(db) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
            indexes = {row[0] for row in await cursor.fetchall()}
        assert {
            "idx_comments_error_signature_v2",
            "idx_fh_error_signature_v2",
        } <= indexes

    async def test_migration_is_idempotent(self, temp_db_path: Path):
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as conn:
                cursor = await conn.execute(
                    "SELECT error_signature_v2 FROM failure_history"
                )
                assert await cursor.fetchall() == []

    async def test_existing_row_keeps_its_stored_anchor(self, temp_db_path: Path):
        """A database written before the v2 rules keeps every stored hash."""
        async with aiosqlite.connect(temp_db_path) as conn:
            await conn.execute(
                "CREATE TABLE failure_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " job_id TEXT NOT NULL, job_name TEXT NOT NULL, build_number INTEGER,"
                " test_name TEXT NOT NULL, error_message TEXT NOT NULL DEFAULT '',"
                " error_signature TEXT NOT NULL DEFAULT '',"
                " classification TEXT NOT NULL DEFAULT '', pattern TEXT NOT NULL DEFAULT '',"
                " child_job_name TEXT NOT NULL DEFAULT '',"
                " child_build_number INTEGER NOT NULL DEFAULT 0,"
                " analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            await conn.execute(
                "INSERT INTO failure_history (job_id, job_name, build_number, test_name,"
                " error_signature) VALUES ('old', 'my-job', 1, 'test_x', 'anchor-only')"
            )
            # The pre-v2 "clear failure_history and re-derive signatures"
            # migration is long applied in every real database; mark it so this
            # test observes only what init_db does to the v2 columns.
            await conn.execute(
                "CREATE TABLE _migrations_applied (key TEXT PRIMARY KEY,"
                " applied_at TIMESTAMP)"
            )
            await conn.execute(
                "INSERT INTO _migrations_applied (key)"
                " VALUES ('recompute_normalized_signatures_v1')"
            )
            await conn.commit()

        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as conn:
                cursor = await conn.execute(
                    "SELECT error_signature, error_signature_v2 FROM failure_history"
                )
                assert [tuple(row) for row in await cursor.fetchall()] == [
                    ("anchor-only", None)
                ]


# ---------------------------------------------------------------------------
# Writes are dual
# ---------------------------------------------------------------------------
class TestDualWrite:
    async def test_populate_failure_history_writes_both_columns(self, db: Path):
        failure = failure_a()
        with patch.object(storage, "DB_PATH", db):
            await storage.populate_failure_history(
                "job-new",
                {
                    "job_name": "my-job",
                    "build_number": 2,
                    "failures": [
                        dual_written_row(failure) | {"test_name": failure.test_name}
                    ],
                },
            )
        async with aiosqlite.connect(db) as conn:
            cursor = await conn.execute(
                "SELECT error_signature, error_signature_v2 FROM failure_history"
            )
            assert await cursor.fetchall() == [
                (get_legacy_signature(failure), get_failure_signature(failure))
            ]

    async def test_add_comment_writes_both_columns(self, db: Path):
        failure = failure_a()
        with patch.object(storage, "DB_PATH", db):
            await storage.add_comment(
                "job-new",
                failure.test_name,
                comment="looks bad",
                error_signature=get_legacy_signature(failure),
                error_signature_v2=get_failure_signature(failure),
            )
        async with aiosqlite.connect(db) as conn:
            cursor = await conn.execute(
                "SELECT error_signature, error_signature_v2 FROM comments"
            )
            assert await cursor.fetchall() == [
                (get_legacy_signature(failure), get_failure_signature(failure))
            ]

    async def test_analysis_stamps_anchor_and_group_hash(self) -> None:
        from rootcoz.engine.core import _expand_group_to_analyses
        from rootcoz.models import AnalysisDetail

        group = [failure_a(), failure_b()]
        analyses = _expand_group_to_analyses(
            get_failure_signature(failure_a()), group, AnalysisDetail(details="d")
        )
        assert [a.error_signature_v2 for a in analyses] == [
            get_failure_signature(failure_a())
        ] * 2
        # The v2 rules merged them into one group; the frozen anchors differ, so
        # each row keeps the anchor that reaches its own older history.
        assert analyses[0].error_signature == get_legacy_signature(failure_a())
        assert analyses[1].error_signature == get_legacy_signature(failure_b())
        assert analyses[0].error_signature != analyses[1].error_signature

    async def test_unanalyzed_failures_are_dual_written(self) -> None:
        """A source builds its FailureAnalysis rows before any AI call."""
        from rootcoz.sources.base import CISourceResult

        result = CISourceResult(
            build_url="https://ci.example/job/1",
            failures=[failure_a(), failure_b()],
        )
        analyses = result.unanalyzed_failure_analyses()
        assert [a.error_signature_v2 for a in analyses] == [
            get_failure_signature(failure_a())
        ] * 2
        assert [a.error_signature for a in analyses] == [
            get_legacy_signature(failure_a()),
            get_legacy_signature(failure_b()),
        ]


# ---------------------------------------------------------------------------
# Cross-boundary reads
# ---------------------------------------------------------------------------
class TestHistoryAcrossTheRuleChange:
    async def test_search_by_anchor_finds_both_eras(self, db: Path):
        """A failure analysed today, searched by its anchor, still finds history."""
        failure = failure_a()
        anchor = get_legacy_signature(failure)
        await _add_history_row(db, "job-old", failure.test_name, anchor=anchor, v2=None)
        await _add_history_row(
            db,
            "job-new",
            failure.test_name,
            anchor=anchor,
            v2=get_failure_signature(failure),
        )
        with patch.object(storage, "DB_PATH", db):
            result = await storage.search_by_signature(anchor)
        assert result["total_occurrences"] == 2

    async def test_search_by_v2_finds_the_new_era(self, db: Path):
        failure = failure_a()
        await _add_history_row(
            db,
            "job-old",
            failure.test_name,
            anchor=get_legacy_signature(failure),
            v2=None,
        )
        await _add_history_row(
            db,
            "job-new",
            failure.test_name,
            anchor=get_legacy_signature(failure),
            v2=get_failure_signature(failure),
        )
        with patch.object(storage, "DB_PATH", db):
            result = await storage.search_by_signature(get_failure_signature(failure))
        assert result["total_occurrences"] == 1

    async def test_historical_comments_reach_a_legacy_comment(self, db: Path):
        failure = failure_a()
        anchor = get_legacy_signature(failure)
        with patch.object(storage, "DB_PATH", db):
            await storage.add_comment(
                "job-old",
                failure.test_name,
                comment="legacy note",
                error_signature=anchor,
            )
            await storage.add_comment(
                "job-new",
                failure.test_name,
                comment="new note",
                error_signature=anchor,
                error_signature_v2=get_failure_signature(failure),
            )
            comments = await storage.get_historical_comments(error_signatures=[anchor])
        assert {c["comment"] for c in comments} == {"legacy note", "new note"}

    async def test_previous_analysis_matched_across_the_boundary(self, db: Path):
        """find_matching_previous_analysis returns both columns for the matcher."""
        failure = failure_a()
        await _add_history_row(
            db,
            "job-old",
            failure.test_name,
            anchor=get_legacy_signature(failure),
            v2=None,
            username="human-reviewer",
        )
        with patch.object(storage, "DB_PATH", db):
            previous = await storage.find_matching_previous_analysis(
                "my-job", failure.test_name, "job-new"
            )
        assert previous is not None
        assert previous["error_signature_v2"] is None
        assert storage.signatures_match(previous, dual_written_row(failure))


class TestAutoReviewAcrossTheRuleChange:
    async def test_legacy_review_still_auto_reviews_a_new_failure(
        self, db: Path
    ) -> None:
        """The headline guarantee: today's failure chains to last month's review."""
        from rootcoz import main as main_mod

        failure = failure_a()
        await _add_history_row(
            db,
            "job-old",
            failure.test_name,
            anchor=get_legacy_signature(failure),
            v2=None,
            username="human-reviewer",
        )
        with patch.object(storage, "DB_PATH", db):
            reviewed, total = await main_mod._match_and_auto_review_failures(
                "job-new",
                "my-job",
                [dual_written_row(failure) | {"test_name": failure.test_name}],
            )
            assert (reviewed, total) == (1, 1)
            async with storage._connect_db() as conn:
                cursor = await conn.execute(
                    "SELECT error_signature, error_signature_v2 FROM comments"
                )
                assert [tuple(row) for row in await cursor.fetchall()] == [
                    (
                        get_legacy_signature(failure),
                        get_failure_signature(failure),
                    )
                ]

    async def test_unrelated_failure_is_not_auto_reviewed(self, db: Path) -> None:
        from rootcoz import main as main_mod

        await _add_history_row(
            db,
            "job-old",
            "test_gateway",
            anchor=get_legacy_signature(failure_a()),
            v2=None,
            username="human-reviewer",
        )
        other = FailedTest(
            test_name="test_gateway", error_message="NullPointerException"
        )
        with patch.object(storage, "DB_PATH", db):
            reviewed, total = await main_mod._match_and_auto_review_failures(
                "job-new",
                "my-job",
                [dual_written_row(other) | {"test_name": other.test_name}],
            )
        assert (reviewed, total) == (0, 1)

    async def test_next_run_of_the_same_failure_still_chains(self, db: Path) -> None:
        """The v2 hash is what makes the *next* run of a noisy failure match.

        Header noise differs between runs, so the frozen anchors differ too --
        an anchor-only comparison stops chaining on exactly the failures the v2
        rules exist to merge.
        """
        from rootcoz import main as main_mod

        earlier = failure_a()
        later = failure_b()
        assert get_failure_signature(earlier) == get_failure_signature(later)
        assert get_legacy_signature(earlier) != get_legacy_signature(later)

        await _add_history_row(
            db,
            "job-old",
            earlier.test_name,
            anchor=get_legacy_signature(earlier),
            v2=get_failure_signature(earlier),
            username="human-reviewer",
        )
        with patch.object(storage, "DB_PATH", db):
            reviewed, _ = await main_mod._match_and_auto_review_failures(
                "job-new",
                "my-job",
                [dual_written_row(later) | {"test_name": later.test_name}],
            )
        assert reviewed == 1


# ---------------------------------------------------------------------------
# An empty signature has no WHERE to interpolate
# ---------------------------------------------------------------------------
class TestEmptySignature:
    """``signature_match`` returns ("", []) on empty input -- by contract.

    Interpolated anyway, the fragment leaves ``WHERE`` with nothing after it.
    """

    def test_empty_input_yields_no_fragment(self) -> None:
        assert storage.signature_match([]) == ("", [])
        assert storage.signature_match(["", None]) == ("", [])

    async def test_search_by_empty_signature_matches_nothing(self, db: Path) -> None:
        failure = failure_a()
        await _add_history_row(
            db,
            "job-old",
            failure.test_name,
            anchor=get_legacy_signature(failure),
            v2=get_failure_signature(failure),
        )
        with patch.object(storage, "DB_PATH", db):
            result = await storage.search_by_signature("")
        assert result["total_occurrences"] == 0
        assert result["unique_tests"] == 0
        assert result["tests"] == []
        assert result["comments"] == []

    async def test_search_by_empty_signature_respects_exclude_job(
        self, db: Path
    ) -> None:
        """The guard returns before any query, so the filter is moot -- not a crash."""
        with patch.object(storage, "DB_PATH", db):
            result = await storage.search_by_signature("", exclude_job_id="job-old")
        assert result["total_occurrences"] == 0

    async def test_no_other_call_site_interpolates_an_empty_fragment(
        self, db: Path
    ) -> None:
        """The three call sites the guard does not live in, all covered.

        Guards historical comments, related comments, and the override group --
        the reviewers' other interpolation points.
        """
        failure = failure_a()
        with patch.object(storage, "DB_PATH", db):
            await storage.add_comment("job-old", failure.test_name, comment="note")
            assert await storage.get_historical_comments(error_signatures=[""]) == []
            assert await storage.get_historical_comments(
                test_names=[failure.test_name], error_signatures=[""]
            ) == [
                c
                for c in await storage.get_historical_comments(
                    test_names=[failure.test_name]
                )
            ]
            # A run whose rows carry no signature at all: the related-comments
            # fragment is empty and the query must fall back to the test name.
            await storage.populate_failure_history(
                "job-group",
                {
                    "job_name": "my-job",
                    "build_number": 2,
                    "failures": [{"test_name": failure.test_name}],
                },
            )
            history = await storage.get_test_history(failure.test_name)
            assert [c["comment"] for c in history["comments"]] == ["note"]
            # No signature on the row -> no group -> the single test stands alone.
            group = await storage.override_classification(
                job_id="job-group",
                test_name=failure.test_name,
                classification="PRODUCT BUG",
                username="tester",
            )
            assert group == [failure.test_name]


# ---------------------------------------------------------------------------
# History written before the v2 rules, seen after the noise moved
# ---------------------------------------------------------------------------
class TestLegacyHistoryWhenTheAnchorMoved:
    """A legacy row has no v2 hash, so the hash union alone misses it.

    A run whose only difference is header/pointer noise gets a different frozen
    anchor every time, so the anchor a legacy row is filed under is never the
    one today's run produces. ``signatures_match`` never sees past the hashes;
    the message fallback lives in ``previous_analysis_matches``, the rule of the
    history lookup, and must not widen into a message-only match anywhere else.
    """

    @staticmethod
    def _history_row(failure: FailedTest) -> dict[str, str]:
        return legacy_row(failure) | {"error_message": failure.error_message}

    @staticmethod
    def _current_row(failure: FailedTest) -> dict[str, str]:
        return dual_written_row(failure) | {"error": failure.error_message}

    def test_the_history_lookup_still_matches_a_later_run(self) -> None:
        """The gap that fix exists for -- closed by the lookup, not by hashes."""
        earlier, later = failure_a(), failure_b()
        # The premise: the anchors share nothing, so the union finds no match.
        assert get_legacy_signature(earlier) != get_legacy_signature(later)
        assert not (
            set(storage.signature_hashes(self._history_row(earlier)))
            & set(storage.signature_hashes(self._current_row(later)))
        )
        assert storage.previous_analysis_matches(
            self._history_row(earlier), self._current_row(later)
        )

    def test_the_universal_comparison_stops_at_the_hashes(self) -> None:
        """Same pair, universal rule: no match. The fallback is not in here."""
        assert not storage.signatures_match(
            self._history_row(failure_a()), self._current_row(failure_b())
        )

    def test_a_similar_but_distinct_defect_does_not_match(self) -> None:
        """The guard: same shape of message, different defect, no match."""
        left = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
        )
        right = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 5 rows in report, got 6",
        )
        assert not storage.signatures_match(
            self._history_row(left), self._current_row(right)
        )
        assert not storage.previous_analysis_matches(
            self._history_row(left), self._current_row(right)
        )

    def test_a_different_defect_entirely_does_not_match(self) -> None:
        other = FailedTest(
            test_name="test_gateway",
            error_message="NullPointerException at Frame.java:12",
        )
        assert not storage.signatures_match(
            self._history_row(failure_a()), self._current_row(other)
        )
        assert not storage.previous_analysis_matches(
            self._history_row(failure_a()), self._current_row(other)
        )

    def test_a_row_with_no_message_is_never_matched_on_message(self) -> None:
        """A trace-only failure stores no message; it must not match everything."""
        trace_only = legacy_row(failure_a())
        assert not storage.signatures_match(trace_only, {"error_message": ""})
        assert not storage.signatures_match(trace_only, {"error": "anything"})
        assert not storage.previous_analysis_matches(trace_only, {"error": "anything"})

    def test_one_message_and_two_traces_match_only_in_the_lookup(
        self,
    ) -> None:
        """The safety property, as a pair: same message, different stack trace.

        ``failure_history`` stores no stack, so the message fallback cannot tell
        these two apart -- which is exactly why it is not allowed to answer for
        the universal comparison, where a wrong answer merges two defects.
        """
        left = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.json()",
        )
        right = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.text()",
        )
        # Distinct failures by every stored hash, identical by message.
        assert get_failure_signature(left) != get_failure_signature(right)
        assert get_legacy_signature(left) != get_legacy_signature(right)
        assert not storage.signatures_match(
            self._history_row(left), self._current_row(right)
        )
        assert storage.previous_analysis_matches(
            self._history_row(left), self._current_row(right)
        )

    async def test_that_pair_still_matches_through_the_lookup(self, db: Path) -> None:
        """End to end: a legacy row the hash union misses is found by the lookup."""
        left = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.json()",
        )
        right = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.text()",
        )
        await _add_history_row(
            db,
            "job-old",
            left.test_name,
            anchor=get_legacy_signature(left),
            v2=None,
            username="human-reviewer",
            message=left.error_message,
        )
        with patch.object(storage, "DB_PATH", db):
            previous = await storage.find_matching_previous_analysis(
                "my-job", left.test_name, "job-new"
            )
        assert previous is not None
        current = self._current_row(right) | {"test_name": right.test_name}
        assert not storage.signatures_match(previous, current)
        assert storage.previous_analysis_matches(previous, current)

    async def test_auto_review_chains_to_a_legacy_row(self, db: Path) -> None:
        """The lookup's widened rule, reached the only way it is reachable."""
        from rootcoz import main as main_mod

        earlier, later = failure_a(), failure_b()
        await _add_history_row(
            db,
            "job-old",
            earlier.test_name,
            anchor=get_legacy_signature(earlier),
            v2=None,
            username="human-reviewer",
            message=earlier.error_message,
        )
        with patch.object(storage, "DB_PATH", db):
            reviewed, total = await main_mod._match_and_auto_review_failures(
                "job-new",
                "my-job",
                [self._current_row(later) | {"test_name": later.test_name}],
            )
        assert (reviewed, total) == (1, 1)

    async def test_a_similar_defect_is_not_auto_reviewed_from_it(
        self, db: Path
    ) -> None:
        from rootcoz import main as main_mod

        earlier = failure_a()
        other = FailedTest(
            test_name=earlier.test_name,
            error_message="HTTPError 502 Bad Gateway\nX-Request-Id: deadbeefcafe0001",
        )
        await _add_history_row(
            db,
            "job-old",
            earlier.test_name,
            anchor=get_legacy_signature(earlier),
            v2=None,
            username="human-reviewer",
            message=earlier.error_message,
        )
        with patch.object(storage, "DB_PATH", db):
            reviewed, total = await main_mod._match_and_auto_review_failures(
                "job-new",
                "my-job",
                [self._current_row(other) | {"test_name": other.test_name}],
            )
        assert (reviewed, total) == (0, 1)


# ---------------------------------------------------------------------------
# Grouping / dedup uses the resolved value
# ---------------------------------------------------------------------------
class TestGroupingUsesResolvedValue:
    async def test_override_groups_failures_merged_by_the_v2_rules(
        self, db: Path
    ) -> None:
        """Two failures the v2 rules merged form one override group."""
        with patch.object(storage, "DB_PATH", db):
            await _add_history_row(
                db,
                "job-group",
                "test_one",
                anchor=get_legacy_signature(failure_a()),
                v2=get_failure_signature(failure_a()),
            )
            await _add_history_row(
                db,
                "job-group",
                "test_two",
                anchor=get_legacy_signature(failure_b()),
                v2=get_failure_signature(failure_b()),
            )
            # Anchors differ, v2 is shared: grouping on the anchor alone would
            # only update one row.
            assert get_legacy_signature(failure_a()) != get_legacy_signature(
                failure_b()
            )

            group = await storage.override_classification(
                job_id="job-group",
                test_name="test_one",
                classification="PRODUCT BUG",
                username="tester",
            )
            assert group == ["test_one", "test_two"]

        async with aiosqlite.connect(db) as conn:
            cursor = await conn.execute(
                "SELECT classification FROM failure_history WHERE job_id = 'job-group'"
            )
            assert await cursor.fetchall() == [("PRODUCT BUG",), ("PRODUCT BUG",)]

    def test_failure_ids_survive_a_rule_change(self) -> None:
        """Re-analysis keeps a stored failure's id even when the rules changed."""
        from rootcoz.storage import _copy_failure_ids

        prior = [
            dual_written_row(failure_a())
            | {"test_name": "test_gateway", "id": "old-id"}
        ]
        current = [
            dual_written_row(failure_b())
            | {"test_name": "test_gateway", "id": "fresh-id"}
        ]
        _copy_failure_ids(prior, current)
        assert current[0]["id"] == "old-id"

    def test_failure_ids_survive_for_a_legacy_prior_result(self) -> None:
        from rootcoz.storage import _copy_failure_ids

        prior = [
            legacy_row(failure_a()) | {"test_name": "test_gateway", "id": "old-id"}
        ]
        current = [
            dual_written_row(failure_a())
            | {"test_name": "test_gateway", "id": "fresh-id"}
        ]
        _copy_failure_ids(prior, current)
        assert current[0]["id"] == "old-id"


# ---------------------------------------------------------------------------
# The prompt must name every hash the group is filed under
# ---------------------------------------------------------------------------
def v2_group() -> list[FailedTest]:
    """A v2 group: one shared v2 hash, one frozen anchor per member."""
    return [failure_a(), failure_b()]


def plain_failure() -> FailedTest:
    """A failure the v2 rules do not separate from the frozen ones."""
    return FailedTest(
        test_name="test_plain", error_message="AssertionError: boom", stack_trace="42"
    )


async def _prompt_for(failures: list[FailedTest], tmp_path: Path) -> str:
    """The prompt run_single_ai_analysis builds, with the AI call stubbed out."""
    captured: dict[str, str] = {}

    async def fake_call(prompt: str, **kwargs):
        captured["prompt"] = prompt
        return AIResult(
            success=True,
            text='{"classification": "CODE ISSUE", "affected_tests": ["t"],'
            ' "details": "d"}',
        )

    with patch("rootcoz.engine.core.call_ai_once", fake_call):
        await run_single_ai_analysis(
            failures=failures,
            console_context="",
            repo_path=tmp_path,
            ai_provider="claude",
            ai_model="opus",
            ai_call_timeout=None,
            custom_prompt="",
            artifacts_context="",
            server_url="",
            job_id="",
        )
    return captured["prompt"]


class TestGroupHistorySearchCoversEveryMember:
    """One search per group must reach every member hash, in one call."""

    async def test_prompt_names_every_member_anchor_and_the_v2_hash(
        self, tmp_path: Path
    ) -> None:
        group = v2_group()
        assert get_legacy_signature(group[0]) != get_legacy_signature(group[1])
        prompt = await _prompt_for(group, tmp_path)
        for failure in group:
            assert get_legacy_signature(failure) in prompt
        assert get_failure_signature(group[0]) in prompt

    async def test_the_failure_details_file_names_every_hash_too(
        self, tmp_path: Path
    ) -> None:
        """The file the AI is told to read first carries the same set."""
        group = v2_group()
        await _prompt_for(group, tmp_path)
        details = next(tmp_path.glob("failure-details-*.txt")).read_text()
        for failure in group:
            assert get_legacy_signature(failure) in details
        assert get_failure_signature(group[0]) in details

    async def test_anchor_leads_so_legacy_attribution_comes_first(
        self, tmp_path: Path
    ) -> None:
        """The first hash listed is the representative's frozen anchor."""
        group = v2_group()
        prompt = await _prompt_for(group, tmp_path)
        section = prompt[prompt.index("ERROR SIGNATURES:") :]
        assert section.index(get_legacy_signature(group[0])) < section.index(
            get_failure_signature(group[0])
        )

    async def test_a_group_with_one_hash_keeps_the_one_line_form(
        self, tmp_path: Path
    ) -> None:
        """Nothing the v2 rules strip, so both rules hash the same text."""
        failure = plain_failure()
        assert get_failure_signature(failure) == get_legacy_signature(failure)
        prompt = await _prompt_for([failure], tmp_path)
        assert f"ERROR SIGNATURE: {get_legacy_signature(failure)}\n" in prompt
        assert "ERROR SIGNATURES" not in prompt


# ---------------------------------------------------------------------------
# Per-failure token usage for a v2 group
# ---------------------------------------------------------------------------
def _usage_record(**overrides) -> dict:
    record = {
        "ai_provider": "claude",
        "ai_model": "opus",
        "call_type": "primary",
        "credential_source": "user",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 15,
        "cost_usd": 0.01,
        "duration_ms": 100,
        "error_signature": "",
        "error_signature_v2": "",
        "child_job_name": "",
        "child_build_number": 0,
    }
    record.update(overrides)
    return record


class TestPerFailureUsageReachesEveryGroupMember:
    def test_every_member_gets_the_summary_its_anchors_differ(self) -> None:
        """The call is counted in the job total; each member must see it too."""
        group = v2_group()
        v2 = get_failure_signature(group[0])
        result = {
            "failures": [
                dual_written_row(f) | {"test_name": f.test_name} for f in group
            ]
        }
        attach_failure_usage(
            result,
            [
                _usage_record(
                    error_signature=get_legacy_signature(group[0]),
                    error_signature_v2=v2,
                )
            ],
        )
        assert all(f["token_usage"]["total_calls"] == 1 for f in result["failures"])

    def test_a_legacy_record_still_lands_on_its_anchor(self) -> None:
        """No v2 on the row (written before the rules) or on the failure."""
        failure = failure_a()
        result = {"failures": [legacy_row(failure) | {"test_name": "test_gateway"}]}
        attach_failure_usage(
            result, [_usage_record(error_signature=get_legacy_signature(failure))]
        )
        assert result["failures"][0]["token_usage"]["total_calls"] == 1

    async def test_the_recorded_row_carries_both_hashes(self) -> None:
        """Anchor for legacy attribution, v2 so the members can find it."""
        usage = SimpleNamespace(
            provider="claude",
            model="opus",
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=0.01,
            duration_ms=100,
        )
        with (
            patch(
                "rootcoz.token_tracking.storage.record_token_usage",
                new_callable=AsyncMock,
            ) as record,
            failure_group_usage("job-1", "anchor-hash", "v2-hash"),
        ):
            await record_ai_usage(
                "job-1",
                SimpleNamespace(success=True, usage=usage, text="ok"),
                "primary",
            )
        kwargs = record.call_args.kwargs
        assert kwargs["error_signature"] == "anchor-hash"
        assert kwargs["error_signature_v2"] == "v2-hash"


# ---------------------------------------------------------------------------
# Peer prompts search the same set
# ---------------------------------------------------------------------------
class TestPeerPromptSearchesTheSameSet:
    def test_peer_summary_names_every_member_anchor(self, tmp_path: Path) -> None:
        group = v2_group()
        summary = _build_failure_summary(
            group, get_failure_signature(group[0]), tmp_path
        )
        for failure in group:
            assert get_legacy_signature(failure) in summary

    def test_peer_summary_for_one_hash_keeps_the_one_line_form(
        self, tmp_path: Path
    ) -> None:
        failure = plain_failure()
        summary = _build_failure_summary(
            [failure], get_failure_signature(failure), tmp_path
        )
        assert f"ERROR SIGNATURE: {get_failure_signature(failure)}\n" in summary
        assert "ERROR SIGNATURES" not in summary


# ---------------------------------------------------------------------------
# The message fallback must not answer for a row that has a v2 hash
# ---------------------------------------------------------------------------
class TestMessageFallbackStopsAtTheFirstV2Hash:
    """The fallback is for pre-v2 rows only.

    ``failure_history`` stores no stack trace, so the message-only hash cannot
    tell two failures apart -- and ``main.py``'s auto-review gate uses
    ``previous_analysis_matches`` to decide a new failure is already-seen. A row
    that carries a v2 hash was compared under the current rules and disagreed;
    letting the fallback answer for it auto-reviews a different defect.
    """

    @staticmethod
    def _previous_v2_row(failure: FailedTest) -> dict[str, str]:
        return dual_written_row(failure) | {"error_message": failure.error_message}

    @staticmethod
    def _current_row(failure: FailedTest) -> dict[str, str]:
        return dual_written_row(failure) | {"error": failure.error_message}

    def test_same_message_different_trace_does_not_match_a_v2_row(self) -> None:
        earlier = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.json()",
        )
        later = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.text()",
        )
        previous, current = self._previous_v2_row(earlier), self._current_row(later)
        # The premise: distinct by every stored hash, identical by message.
        assert get_legacy_signature(earlier) != get_legacy_signature(later)
        assert not storage.signatures_match(previous, current)
        # The guard: no auto-review for a different failure behind one message.
        assert not storage.previous_analysis_matches(previous, current)

    def test_the_pre_v2_row_still_reaches_the_fallback(self) -> None:
        """The case the fallback exists for is untouched by the guard."""
        earlier = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.json()",
        )
        later = FailedTest(
            test_name="test_gateway",
            error_message="AssertionError: expected 3 rows in report, got 4",
            stack_trace="await response.text()",
        )
        previous = legacy_row(earlier) | {"error_message": earlier.error_message}
        assert not storage.signatures_match(previous, self._current_row(later))
        assert storage.previous_analysis_matches(previous, self._current_row(later))


# ---------------------------------------------------------------------------
# One search per group, however many hashes it has
# ---------------------------------------------------------------------------
class TestOneSearchCoversTheWholeGroup:
    async def test_a_comma_separated_search_returns_every_hash_s_rows(
        self, db: Path
    ) -> None:
        left, right = failure_a(), failure_b()
        await _add_history_row(
            db,
            "job-old",
            left.test_name,
            anchor=get_legacy_signature(left),
            v2=None,
        )
        await _add_history_row(
            db,
            "job-older",
            right.test_name,
            anchor=get_legacy_signature(right),
            v2=None,
        )
        both = f"{get_legacy_signature(left)}, {get_legacy_signature(right)}"
        with patch.object(storage, "DB_PATH", db):
            result = await storage.search_by_signature(both)
        assert result["total_occurrences"] == 2
        assert {t["test_name"] for t in result["tests"]} == {left.test_name}

    async def test_a_single_hash_still_searches_that_hash_only(self, db: Path) -> None:
        """One value and a set of one are the same query."""
        left, right = failure_a(), failure_b()
        await _add_history_row(
            db, "job-old", left.test_name, anchor=get_legacy_signature(left), v2=None
        )
        with patch.object(storage, "DB_PATH", db):
            result = await storage.search_by_signature(get_legacy_signature(left))
        assert result["total_occurrences"] == 1
        assert get_legacy_signature(right) not in result["signature"]

    async def test_the_prompt_asks_for_one_call_not_one_per_hash(
        self, tmp_path: Path
    ) -> None:
        group = v2_group()
        prompt = await _prompt_for(group, tmp_path)
        assert "ONCE" in prompt
        assert "comma-separated" in prompt
        # One line carrying the whole set -- not a bullet list to walk.
        signature_lines = [
            line
            for line in prompt.splitlines()
            if get_legacy_signature(group[0]) in line
            and get_legacy_signature(group[1]) in line
        ]
        assert len(signature_lines) == 1


# ---------------------------------------------------------------------------
# A trace-only failure stores no message; its trace is the comparison
# ---------------------------------------------------------------------------
class TestTraceOnlyFailuresReachTheFallback:
    """``failure_history.error_message`` holds the trace when there is no message.

    ``error`` is a signature input and persists verbatim, so a trace-only
    failure stores an empty message and the history copy keeps the trace
    standing in for it. Both sides of the comparison must therefore hash the
    trace -- reading only the message compared a trace against nothing.
    """

    @staticmethod
    def _trace_only(trace: str) -> FailedTest:
        return FailedTest(test_name="test_gateway", error_message="", stack_trace=trace)

    def test_a_trace_only_rerun_matches_its_own_pre_v2_history(self) -> None:
        failure = self._trace_only("  at Frame.java:12")
        history = legacy_row(failure) | {"error_message": failure.stack_trace}
        current = {
            "error": "",
            "stack_trace": failure.stack_trace,
            "error_signature": get_legacy_signature(failure),
        }
        assert storage.failure_error_text(current) == failure.stack_trace
        assert storage.previous_analysis_matches(history, current)

    def test_the_same_trace_under_a_moved_anchor_still_matches(self) -> None:
        """The v2 rules normalize pointer noise out of the trace, the anchor keeps it."""
        earlier = self._trace_only("  at Frame.java:12  (0x7f3c9a10)")
        later = self._trace_only("  at Frame.java:12  (0x118ab430)")
        assert get_legacy_signature(earlier) != get_legacy_signature(later)
        history = legacy_row(earlier) | {"error_message": earlier.stack_trace}
        current = {
            "error": "",
            "stack_trace": later.stack_trace,
            "error_signature": get_legacy_signature(later),
        }
        assert storage.previous_analysis_matches(history, current)

    def test_a_different_trace_still_does_not_match(self) -> None:
        """The guard on the new fallback: it reads the trace, it does not ignore it."""
        history = legacy_row(self._trace_only("  at Frame.java:12")) | {
            "error_message": "  at Frame.java:12"
        }
        current = {"error": "", "stack_trace": "  at Other.java:99"}
        assert not storage.previous_analysis_matches(history, current)
