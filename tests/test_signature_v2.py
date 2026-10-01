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
from unittest.mock import patch

import aiosqlite
import pytest

from rootcoz import storage
from rootcoz.engine.core import (
    compute_legacy_signature,
    compute_signature,
    get_failure_signature,
    get_legacy_signature,
)
from rootcoz.models import FailedTest

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
) -> None:
    """Insert a failure_history row, dual-written or legacy-only."""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO failure_history (job_id, job_name, build_number, test_name,"
            " error_message, error_signature, error_signature_v2, classification)"
            " VALUES (?, 'my-job', 1, ?, 'boom', ?, ?, 'CODE ISSUE')",
            (job_id, test_name, anchor, v2),
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
