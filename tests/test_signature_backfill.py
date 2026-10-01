"""Tests for the failure-signature backfill (issue #239 follow-up)."""

import pytest

from rootcoz.engine.core import compute_signature
from rootcoz.signature_backfill import (
    backfill_signatures,
    iter_stored_failures,
)


def _failure(test_name: str, message: str, trace: str = "trace") -> dict:
    return {
        "test_name": test_name,
        "error_message": message,
        "stack_trace": trace,
        "error_signature": "",
    }


class TestComputeSignatureSharedFormula:
    """The analyzer and the backfill must share one hash definition."""

    def test_get_failure_signature_matches_compute_signature(self):
        from rootcoz.engine.core import get_failure_signature
        from rootcoz.models import FailedTest

        failure = FailedTest(
            test_name="t", error_message="boom", stack_trace="at x.py:1"
        )
        assert get_failure_signature(failure) == compute_signature("boom", "at x.py:1")


class TestIterStoredFailures:
    def test_walks_top_level_and_nested_children(self):
        result = {
            "failures": [_failure("top", "a")],
            "child_job_analyses": [
                {
                    "job_name": "child-a",
                    "build_number": 7,
                    "failures": [_failure("nested", "b")],
                    "failed_children": [
                        {
                            "job_name": "grandchild",
                            "build_number": 9,
                            "failures": [_failure("deep", "c")],
                        }
                    ],
                }
            ],
        }
        found = [
            (f["test_name"], child, build)
            for f, child, build in iter_stored_failures(result)
        ]
        assert found == [
            ("top", "", 0),
            ("nested", "child-a", 7),
            ("deep", "grandchild", 9),
        ]

    def test_does_not_write_scratch_keys_into_failures(self):
        result = {"failures": [_failure("top", "a")]}
        list(iter_stored_failures(result))
        assert "_child_job_name" not in result["failures"][0]


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Isolated DB with a couple of stored jobs."""
    from rootcoz import storage

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "results.db")
    await storage.init_db()
    yield storage


async def _store(storage, job_id: str, result: dict) -> None:
    await storage.save_result(job_id, "http://x", "completed", result)


class TestBackfillSignatures:
    async def test_dry_run_changes_nothing(self, db):
        failure = _failure("t", "HTTPError 502\nDate: Sun, 31 May 2026 06:50:48 GMT")
        failure["error_signature"] = "stale-hash"
        await _store(db, "job1", {"failures": [failure]})

        stats = await backfill_signatures(dry_run=True)

        assert stats["dry_run"] is True
        assert stats["jobs_scanned"] == 1
        assert stats["failures_scanned"] == 1
        assert stats["failures_changed"] == 1
        stored = (await db.get_result("job1"))["result"]
        assert stored["failures"][0]["error_signature"] == "stale-hash"

    async def test_apply_recomputes_to_current_rules(self, db):
        message = "HTTPError 502\nDate: Sun, 31 May 2026 06:50:48 GMT"
        failure = _failure("t", message)
        failure["error_signature"] = "stale-hash"
        await _store(db, "job1", {"failures": [failure]})

        stats = await backfill_signatures(dry_run=False)

        assert stats["dry_run"] is False
        assert stats["jobs_changed"] == 1
        stored = (await db.get_result("job1"))["result"]
        assert stored["failures"][0]["error_signature"] == compute_signature(
            message, "trace"
        )

    async def test_apply_updates_failure_history_and_comments(self, db):
        message = "timeout after 5s\nDate: Sun, 31 May 2026 06:50:48 GMT"
        failure = _failure("test_thing", message)
        failure["error_signature"] = "stale-hash"
        await _store(db, "job1", {"failures": [failure]})
        await db.add_comment(
            "job1", "test_thing", "seen it", error_signature="stale-hash"
        )

        await backfill_signatures(dry_run=False)

        comments = await db.get_comments_for_job("job1")
        assert comments[0]["error_signature"] == compute_signature(message, "trace")

    async def test_already_current_signature_is_left_alone(self, db):
        message = "boom"
        failure = _failure("t", message)
        failure["error_signature"] = compute_signature(message, "trace")
        await _store(db, "job1", {"failures": [failure]})

        stats = await backfill_signatures(dry_run=False)

        assert stats["failures_changed"] == 0
        assert stats["jobs_changed"] == 0

    async def test_noise_only_difference_now_collapses_to_one_signature(self, db):
        """The whole point: two runs differing only in header noise share a hash."""
        first = _failure("t", "HTTPError 502\nDate: Sun, 31 May 2026 06:50:48 GMT")
        second = _failure("t", "HTTPError 502\nDate: Mon, 01 Jun 2026 11:22:33 GMT")
        for f in (first, second):
            f["error_signature"] = "stale-hash"
        await _store(db, "job1", {"failures": [first, second]})

        await backfill_signatures(dry_run=False)

        stored = (await db.get_result("job1"))["result"]["failures"]
        assert stored[0]["error_signature"] == stored[1]["error_signature"]

    async def test_missing_input_is_counted_not_destroyed(self, db):
        legacy = {"test_name": "t", "error_signature": "legacy-hash"}
        await _store(db, "job1", {"failures": [legacy]})

        stats = await backfill_signatures(dry_run=False)

        assert stats["failures_missing_input"] == 1
        stored = (await db.get_result("job1"))["result"]["failures"]
        assert stored[0]["error_signature"] == "legacy-hash"

    async def test_unparsable_job_is_reported_and_skipped(self, db):
        await _store(db, "good", {"failures": []})
        async with db._connect_db() as conn:
            await conn.execute(
                "UPDATE results SET result_json = ? WHERE job_id = ?",
                ("{not json", "broken"),
            )
            await conn.execute(
                "INSERT INTO results (job_id, result_json) VALUES (?, ?)",
                ("broken", "{not json"),
            )
            await conn.commit()

        stats = await backfill_signatures(dry_run=False)

        assert stats["unparsable_jobs"] == ["broken"]

    async def test_child_job_failures_are_backfilled_with_identity(self, db):
        message = "child boom\nDate: Sun, 31 May 2026 06:50:48 GMT"
        child_failure = _failure("child_test", message)
        child_failure["error_signature"] = "stale-hash"
        await _store(
            db,
            "job1",
            {
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-a",
                        "build_number": 7,
                        "failures": [child_failure],
                    }
                ],
            },
        )
        await db.populate_failure_history(
            "job1",
            {
                "job_name": "job",
                "build_number": 1,
                "failures": [child_failure],
                "child_job_analyses": [
                    {
                        "job_name": "child-a",
                        "build_number": 7,
                        "failures": [child_failure],
                    }
                ],
            },
        )

        await backfill_signatures(dry_run=False)

        stored = (await db.get_result("job1"))["result"]
        nested = stored["child_job_analyses"][0]["failures"][0]
        assert nested["error_signature"] == compute_signature(message, "trace")
