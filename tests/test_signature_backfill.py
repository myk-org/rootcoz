"""Tests for the failure-signature backfill (issue #239 follow-up)."""

import asyncio

import pytest

from rootcoz import signature_backfill
from rootcoz.engine.core import (
    compute_signature,
    get_failure_signature,
    normalization_rules_version,
)
from rootcoz.models import AnalysisDetail, AnalysisResult, FailedTest, FailureAnalysis
from rootcoz.signature_backfill import (
    MIGRATION_KEY_PREFIX,
    backfill_signatures,
    ensure_signatures_current,
    iter_stored_failures,
    signature_inputs,
)
from rootcoz.sources.base import CISourceResult

MESSAGE_WITH_NOISE = "HTTPError 502\nDate: Sun, 31 May 2026 06:50:48 GMT"
TRACE = "at t.py:1"


def _failed_test(test_name: str, message: str, trace: str = TRACE) -> FailedTest:
    return FailedTest(test_name=test_name, error_message=message, stack_trace=trace)


def _failure(test_name: str, message: str, trace: str = TRACE) -> FailureAnalysis:
    """Build a FailureAnalysis with the fields the analyzer sets."""
    failure = _failed_test(test_name, message, trace)
    return FailureAnalysis(
        test_name=failure.test_name,
        error=failure.error_message,
        stack_trace=failure.stack_trace,
        analysis=AnalysisDetail(classification="CODE ISSUE", details="d", pattern="p"),
        error_signature=get_failure_signature(failure),
    )


def _result(
    job_id: str = "job",
    failures: list[FailureAnalysis] | None = None,
    children: list[dict] | None = None,
) -> dict:
    """Serialize through the production model, as main.py does before saving."""
    data = AnalysisResult(
        job_id=job_id,
        job_name="job",
        status="completed",
        summary="",
        failures=failures or [],
    ).model_dump(mode="json")
    if children:
        data["child_job_analyses"] = children
    return data


class TestComputeSignatureSharedFormula:
    """The analyzer and the backfill must share one hash definition."""

    def test_get_failure_signature_matches_compute_signature(self):
        failure = _failed_test("t", "boom")
        assert get_failure_signature(failure) == compute_signature("boom", TRACE)


class TestPersistedSchema:
    """The backfill must read what the normal persistence path actually writes."""

    def test_failure_analysis_persists_both_signature_inputs(self):
        stored = _failure("t", MESSAGE_WITH_NOISE).model_dump(mode="json")
        assert stored["error"] == MESSAGE_WITH_NOISE
        assert stored["stack_trace"] == TRACE
        assert signature_inputs(stored) == (MESSAGE_WITH_NOISE, TRACE)

    def test_a_real_persisted_result_exposes_its_inputs(self):
        result = _result(failures=[_failure("t", MESSAGE_WITH_NOISE)])
        failure = next(iter(result["failures"]))
        assert "error_message" not in failure
        assert signature_inputs(failure) == (MESSAGE_WITH_NOISE, TRACE)

    def test_row_without_a_stored_trace_is_not_recoverable(self):
        legacy = _failure("t", "boom").model_dump(mode="json")
        del legacy["stack_trace"]
        assert signature_inputs(legacy) is None

    def test_non_string_inputs_are_not_recoverable(self):
        assert signature_inputs({"error": "boom"}) is None
        assert signature_inputs({"error": "boom", "stack_trace": 3}) is None


class TestTraceOnlyFailures:
    """A failure with no message must store the message it was signed with."""

    def _ingested(self, failure: FailedTest) -> FailureAnalysis:
        (analysis,) = CISourceResult(failures=[failure]).unanalyzed_failure_analyses()
        return analysis

    def test_ingest_keeps_the_signature_inputs_it_hashed(self):
        analysis = self._ingested(_failed_test("t", "", TRACE))

        stored = analysis.model_dump(mode="json")
        assert stored["error"] == ""
        assert stored["stack_trace"] == TRACE
        assert signature_inputs(stored) == ("", TRACE)
        assert stored["error_signature"] == compute_signature("", TRACE)

    def test_display_falls_back_to_the_trace(self):
        assert self._ingested(_failed_test("t", "", TRACE)).display_error == TRACE
        assert self._ingested(_failed_test("t", "boom")).display_error == "boom"

    async def test_backfill_leaves_a_trace_only_failure_alone(self, db):
        await _store_job(
            db, "job1", _result(failures=[self._ingested(_failed_test("t", "", TRACE))])
        )

        stats = await backfill_signatures(dry_run=False)

        assert await _signatures(db, "job1") == [compute_signature("", TRACE)]
        assert stats["failures_changed"] == 0
        assert stats["unrecoverable_failures_count"] == 0

    async def test_history_stores_the_trace_so_search_can_find_it(self, db):
        """error is a signature input and stays empty; the history copy shows it."""
        await _store_job(
            db, "job1", _result(failures=[self._ingested(_failed_test("t", "", TRACE))])
        )

        assert (await _history(db, "job1"))[0]["error_message"] == TRACE
        found = await db.get_all_failures(search="t.py:1")
        assert [row["job_id"] for row in found["failures"]] == ["job1"]

    async def test_backfill_still_matches_a_trace_only_history_row(self, db):
        """The history discriminator is the same text the row was written with."""
        stored = self._ingested(_failed_test("t", "", TRACE))
        stored.error_signature = "stale-hash"
        await _store_job(db, "job1", _result(failures=[stored]))

        stats = await backfill_signatures(dry_run=False)

        assert stats["history_rows_changed"] == 1
        assert (await _history(db, "job1"))[0]["error_signature"] == compute_signature(
            "", TRACE
        )


class TestIterStoredFailures:
    def test_walks_top_level_and_nested_children(self):
        result = {
            "failures": [{"test_name": "top"}],
            "child_job_analyses": [
                {
                    "job_name": "child-a",
                    "build_number": 7,
                    "failures": [{"test_name": "nested"}],
                    "failed_children": [
                        {
                            "job_name": "grandchild",
                            "build_number": 9,
                            "failures": [{"test_name": "deep"}],
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
        result = {"failures": [{"test_name": "top"}]}
        list(iter_stored_failures(result))
        assert "_child_job_name" not in result["failures"][0]


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Isolated DB with a couple of stored jobs."""
    from rootcoz import storage

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "results.db")
    await storage.init_db()
    yield storage


async def _store_job(db, job_id: str, result: dict) -> None:
    """Persist a result the way main.py does, then seed its history rows."""
    await db.save_result(job_id, "http://x", "completed", result)
    await db.populate_failure_history(job_id, result)


async def _signatures(db, job_id: str) -> list[str]:
    result = await db.get_result(job_id)
    return [f["error_signature"] for f in result["result"]["failures"]]


async def _history(db, job_id: str) -> list[dict]:
    async with db._connect_db() as conn:
        cursor = await conn.execute(
            "SELECT test_name, error_message, error_signature FROM failure_history "
            "WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
        rows = await cursor.fetchall()
    return [
        {"test_name": r[0], "error_message": r[1], "error_signature": r[2]}
        for r in rows
    ]


def _stale(*failures: FailureAnalysis) -> list[FailureAnalysis]:
    for failure in failures:
        failure.error_signature = "stale-hash"
    return list(failures)


async def _set_status(db, job_id: str, status: str) -> None:
    async with db._connect_db() as conn:
        await conn.execute(
            "UPDATE results SET status = ? WHERE job_id = ?", (status, job_id)
        )
        await conn.commit()


class TestBackfillSignatures:
    async def test_dry_run_changes_nothing(self, db):
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )

        stats = await backfill_signatures(dry_run=True)

        assert stats["dry_run"] is True
        assert stats["jobs_scanned"] == 1
        assert stats["failures_scanned"] == 1
        assert stats["failures_changed"] == 1
        assert await _signatures(db, "job1") == ["stale-hash"]

    async def test_apply_recomputes_to_current_rules(self, db):
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )

        stats = await backfill_signatures(dry_run=False)

        assert stats["dry_run"] is False
        assert stats["jobs_changed"] == 1
        assert await _signatures(db, "job1") == [
            compute_signature(MESSAGE_WITH_NOISE, TRACE)
        ]

    async def test_apply_updates_failure_history_and_comments(self, db):
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("test_thing", "boom")))
        )
        await db.add_comment(
            "job1", "test_thing", "seen it", error_signature="stale-hash"
        )

        stats = await backfill_signatures(dry_run=False)

        expected = compute_signature("boom", TRACE)
        comments = await db.get_comments_for_job("job1")
        history = await _history(db, "job1")
        assert comments[0]["error_signature"] == expected
        assert history[0]["error_signature"] == expected
        assert stats["comment_rows_changed"] == 1
        assert stats["history_rows_changed"] == 1

    async def test_already_current_signature_is_left_alone(self, db):
        await _store_job(db, "job1", _result(failures=[_failure("t", "boom")]))

        stats = await backfill_signatures(dry_run=False)

        assert stats["failures_changed"] == 0
        assert stats["jobs_changed"] == 0

    async def test_noise_only_difference_now_collapses_to_one_signature(self, db):
        """The whole point: two runs differing only in header noise share a hash."""
        await _store_job(
            db,
            "job1",
            _result(
                failures=_stale(
                    _failure("t", MESSAGE_WITH_NOISE),
                    _failure("t", "HTTPError 502\nDate: Mon, 01 Jun 2026 11:22:33 GMT"),
                )
            ),
        )

        await backfill_signatures(dry_run=False)

        stored = await _signatures(db, "job1")
        assert stored[0] == stored[1] != "stale-hash"

    async def test_unrecoverable_input_is_reported_not_guessed(self, db):
        """A row with no stored trace keeps its hash and is reported by id."""
        legacy = _failure("t", "boom").model_dump(mode="json")
        del legacy["stack_trace"]
        await _store_job(db, "job1", {"failures": [legacy], "job_name": "job"})

        stats = await backfill_signatures(dry_run=False)

        original = get_failure_signature(_failed_test("t", "boom"))
        assert await _signatures(db, "job1") == [original]
        assert stats["failures_changed"] == 0
        assert stats["unrecoverable_failures"] == [
            {
                "job_id": "job1",
                "test_name": "t",
                "child_job_name": "",
                "child_build_number": 0,
                "error_signature": original,
            }
        ]

    async def test_unrecoverable_report_stays_bounded(self, db, monkeypatch):
        """The first run reports a sample, not one record per stored failure."""
        monkeypatch.setattr(signature_backfill, "UNRECOVERABLE_SAMPLE_SIZE", 2)
        for index in range(5):
            legacy = _failure(f"t{index}", "boom").model_dump(mode="json")
            del legacy["stack_trace"]
            await _store_job(db, f"job{index}", {"failures": [legacy]})

        stats = await backfill_signatures(dry_run=False)

        assert stats["unrecoverable_failures_count"] == 5
        assert len(stats["unrecoverable_failures"]) == 2

    async def test_unparsable_job_is_reported_and_skipped(self, db):
        await _store_job(db, "good", _result())
        async with db._connect_db() as conn:
            await conn.execute(
                "INSERT INTO results (job_id, result_json) VALUES (?, ?)",
                ("broken", "{not json"),
            )
            await conn.commit()

        stats = await backfill_signatures(dry_run=False)

        assert stats["unparsable_jobs"] == ["broken"]

    async def test_child_job_failures_are_backfilled_with_identity(self, db):
        child_failure = _stale(_failure("child_test", MESSAGE_WITH_NOISE))[0]
        await _store_job(
            db,
            "job1",
            _result(
                children=[
                    {
                        "job_name": "child-a",
                        "build_number": 7,
                        "failures": [child_failure.model_dump(mode="json")],
                    }
                ]
            ),
        )

        stats = await backfill_signatures(dry_run=False)

        stored = (await db.get_result("job1"))["result"]
        assert stored["child_job_analyses"][0]["failures"][0][
            "error_signature"
        ] == compute_signature(MESSAGE_WITH_NOISE, TRACE)
        assert stats["unrecoverable_failures"] == []


class TestConcurrentUpdateSafety:
    async def test_newer_job_fields_are_not_clobbered(self, db):
        """A job updated after the scan keeps its newer fields."""
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )
        # Simulate a reanalysis landing between the scan and the patch.
        stored = (await db.get_result("job1"))["result"]
        stored["failures"].append(
            _failure("fresh", "brand new").model_dump(mode="json")
        )
        stored["summary"] = "re-analyzed"
        await db.save_result("job1", "http://x", "completed", stored)

        await backfill_signatures(dry_run=False)

        after = (await db.get_result("job1"))["result"]
        assert after["summary"] == "re-analyzed"
        assert [f["test_name"] for f in after["failures"]] == ["t", "fresh"]
        assert after["failures"][0]["error_signature"] == compute_signature(
            MESSAGE_WITH_NOISE, TRACE
        )
        assert after["failures"][1]["error_signature"] == get_failure_signature(
            _failed_test("fresh", "brand new")
        )

    async def test_repeated_test_names_keep_their_own_signature(self, db):
        """Two failures sharing a test name get their own history/comment hash."""
        await _store_job(
            db,
            "job1",
            _result(failures=_stale(_failure("t", "first"), _failure("t", "second"))),
        )
        await db.add_comment("job1", "t", "about first", error_signature="stale-hash")

        await backfill_signatures(dry_run=False)

        history = await _history(db, "job1")
        by_message = {row["error_message"]: row["error_signature"] for row in history}
        assert by_message["first"] == compute_signature("first", TRACE)
        assert by_message["second"] == compute_signature("second", TRACE)
        comments = await db.get_comments_for_job("job1")
        assert comments[0]["error_signature"] == compute_signature("first", TRACE)

    async def test_same_name_and_message_different_traces_keep_their_own_hash(self, db):
        """History rows are matched by the hash they carry, not by message."""
        failures = []
        for trace, previous in (("at a.py:1", "old-a"), ("at b.py:2", "old-b")):
            failure = _failure("t", "boom", trace)
            failure.error_signature = previous
            failures.append(failure)
        await _store_job(db, "job1", _result(failures=failures))
        await db.add_comment("job1", "t", "about a", error_signature="old-a")

        stats = await backfill_signatures(dry_run=False)

        history = await _history(db, "job1")
        assert {row["error_signature"] for row in history} == {
            compute_signature("boom", "at a.py:1"),
            compute_signature("boom", "at b.py:2"),
        }
        comments = await db.get_comments_for_job("job1")
        assert comments[0]["error_signature"] == compute_signature("boom", "at a.py:1")
        assert stats["history_rows_changed"] == 2

    async def test_a_shared_old_hash_does_not_give_both_rows_the_first_hash(self, db):
        """A rollback splits one old hash into two: each history row gets its own."""
        failures = []
        for trace in ("at a.py:1", "at b.py:2"):
            failure = _failure("t", "boom", trace)
            # The old rules hashed both traces to this one value.
            failure.error_signature = "shared-old-hash"
            failures.append(failure)
        await _store_job(db, "job1", _result(failures=failures))
        await db.add_comment("job1", "t", "about a", error_signature="shared-old-hash")

        stats = await backfill_signatures(dry_run=False)

        stored = await _signatures(db, "job1")
        assert len(set(stored)) == 2
        assert stored == [
            compute_signature("boom", "at a.py:1"),
            compute_signature("boom", "at b.py:2"),
        ]
        # Rows are seeded in stored-failure order, so each keeps its own hash.
        assert [row["error_signature"] for row in await _history(db, "job1")] == stored
        # The comment row is ambiguous; it leaves with the first failure's hash,
        # never with the dead one.
        assert (await db.get_comments_for_job("job1"))[0]["error_signature"] == stored[
            0
        ]
        assert stats["history_rows_changed"] == 2

    async def test_every_comment_of_one_failure_keeps_its_new_hash(self, db):
        """A failure can hold several comments; they are rewritten as a group."""
        failure = _failure("t", "boom")
        failure.error_signature = "stale-hash"
        await _store_job(db, "job1", _result(failures=[failure]))
        for index in range(3):
            await db.add_comment(
                "job1", "t", f"note {index}", error_signature="stale-hash"
            )

        stats = await backfill_signatures(dry_run=False)

        expected = compute_signature("boom", TRACE)
        assert {
            row["error_signature"] for row in await db.get_comments_for_job("job1")
        } == {expected}
        assert stats["comment_rows_changed"] == 3

    async def test_result_and_history_are_rewritten_atomically(self, db):
        """An interrupted run leaves neither the result nor history half-written."""
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )
        original = db._apply_denormalized_signatures

        async def boom(*_args, **_kwargs):
            raise RuntimeError("interrupted")

        db._apply_denormalized_signatures = boom
        with pytest.raises(RuntimeError):
            await backfill_signatures(dry_run=False)
        db._apply_denormalized_signatures = original

        assert await _signatures(db, "job1") == ["stale-hash"]
        # The retry still sees the row as stale, so it repairs both tables.
        stats = await backfill_signatures(dry_run=False)
        assert stats["failures_changed"] == 1
        assert stats["history_rows_changed"] == 1
        history = await _history(db, "job1")
        assert history[0]["error_signature"] == compute_signature(
            MESSAGE_WITH_NOISE, TRACE
        )

    async def test_a_job_whose_analysis_is_running_is_left_alone(self, db):
        """The running analysis writes current-rule signatures when it saves."""
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )
        await _set_status(db, "job1", "running")

        stats = await backfill_signatures(dry_run=False)

        assert await _signatures(db, "job1") == ["stale-hash"]
        assert stats["jobs_changed"] == 0
        assert stats["history_rows_changed"] == 0
        assert stats["deferred_jobs"] == ["job1"]

    async def test_a_job_that_goes_in_flight_reports_no_rewrite(self, db, monkeypatch):
        """The probe found it stale; the write lock found it already running."""
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )
        real_patch = signature_backfill.patch_result_json

        async def in_flight_patch(job_id, *args, **kwargs):
            await _set_status(db, job_id, "running")
            return await real_patch(job_id, *args, **kwargs)

        monkeypatch.setattr(signature_backfill, "patch_result_json", in_flight_patch)

        stats = await backfill_signatures(dry_run=False)

        # Nothing was rewritten, so nothing is counted as rewritten.
        assert await _signatures(db, "job1") == ["stale-hash"]
        assert stats["failures_changed"] == 0
        assert stats["jobs_changed"] == 0
        assert stats["deferred_jobs"] == ["job1"]

    async def test_backfill_between_history_and_result_save_changes_nothing(self, db):
        """A live analysis writes history first, its result in a later write."""
        stored = _result(failures=_stale(_failure("t", "boom", "at old.py:1")))
        await _store_job(db, "job1", stored)
        await _set_status(db, "job1", "running")

        # The analysis repopulates history with its own signatures, then the
        # backfill walks the job before the analysis saves its result.
        fresh = _result(failures=[_failure("t", "boom", "at new.py:2")])
        await db.populate_failure_history("job1", fresh)
        await backfill_signatures(dry_run=False)

        # The stored result still holds what the running analysis will replace.
        assert await _signatures(db, "job1") == ["stale-hash"]

        await db.update_status("job1", "completed", fresh)

        history = await _history(db, "job1")
        assert history[0]["error_signature"] == compute_signature("boom", "at new.py:2")
        assert await _signatures(db, "job1") == [history[0]["error_signature"]]


class TestWriteLockAvoidance:
    async def test_only_stale_jobs_are_patched(self, db, monkeypatch):
        """patch_result_json takes BEGIN IMMEDIATE; unchanged rows skip it."""
        await _store_job(db, "job1", _result(failures=[_failure("t", "boom")]))
        await _store_job(db, "job2", _result(failures=_stale(_failure("t", "boom"))))
        legacy = _failure("t", "legacy").model_dump(mode="json")
        del legacy["stack_trace"]
        await _store_job(db, "job3", {"failures": [legacy]})

        patched: list[str] = []
        real_patch = db.patch_result_json

        async def spy(job_id, *args, **kwargs):
            patched.append(job_id)
            return await real_patch(job_id, *args, **kwargs)

        monkeypatch.setattr(signature_backfill, "patch_result_json", spy)

        stats = await backfill_signatures(dry_run=False)

        assert patched == ["job2"]
        assert stats["jobs_scanned"] == 3
        assert stats["jobs_changed"] == 1
        assert stats["failures_changed"] == 1
        assert stats["unrecoverable_failures_count"] == 1


class TestBoundedIteration:
    async def test_scan_reads_in_bounded_batches(self, db):
        for index in range(5):
            await _store_job(db, f"job{index}", _result())

        batches = [batch async for batch in db.iter_result_json_batches(batch_size=2)]

        assert [len(batch) for batch in batches] == [2, 2, 1]
        assert sorted(row[0] for batch in batches for row in batch) == [
            f"job{i}" for i in range(5)
        ]

    async def test_backfill_applies_every_batch(self, db):
        for index in range(5):
            await _store_job(
                db,
                f"job{index}",
                _result(failures=_stale(_failure(f"t{index}", MESSAGE_WITH_NOISE))),
            )

        stats = await backfill_signatures(dry_run=False, batch_size=2)

        assert stats["jobs_scanned"] == 5
        assert stats["jobs_changed"] == 5
        for index in range(5):
            assert (await _signatures(db, f"job{index}"))[0] != "stale-hash"

    async def test_live_writes_are_not_blocked_by_a_large_backfill(self, db):
        """Normal writes keep working while the backfill walks the database."""
        for index in range(10):
            await _store_job(
                db,
                f"job{index}",
                _result(failures=_stale(_failure(f"t{index}", MESSAGE_WITH_NOISE))),
            )
        await _store_job(
            db, "job-live", _result(failures=[_failure("new", "brand new")])
        )

        writes = []

        async def write_loop() -> None:
            for i in range(20):
                await db.add_comment("job-live", "new", f"comment {i}")
                writes.append(i)
                await asyncio.sleep(0)

        await asyncio.gather(
            backfill_signatures(dry_run=False, batch_size=2), write_loop()
        )

        assert len(writes) == 20
        assert len(await db.get_comments_for_job("job-live")) == 20


class TestNormalizationRulesVersion:
    def test_is_stable_across_calls(self):
        assert normalization_rules_version() == normalization_rules_version()

    def test_is_the_full_digest(self):
        version = normalization_rules_version()
        assert len(version) == 64
        assert version != version[:16]

    def test_changes_when_a_rule_changes(self, monkeypatch):
        import re

        from rootcoz.engine import core

        before = normalization_rules_version()
        monkeypatch.setattr(
            core,
            "_NORMALIZE_PATTERNS",
            core._NORMALIZE_PATTERNS + [(re.compile(r"NEW-NOISE-\d+"), "<NEW>")],
        )
        assert normalization_rules_version() != before

    def test_changes_when_a_replacement_callable_body_changes(self, monkeypatch):
        """Same pattern, different replacement behaviour -> new fingerprint."""
        import re

        from rootcoz.engine import core

        def _versioned(replacement):
            monkeypatch.setattr(
                core,
                "_NORMALIZE_PATTERNS",
                core._NORMALIZE_PATTERNS + [(re.compile(r"\d+"), replacement)],
            )
            return normalization_rules_version()

        assert _versioned(lambda match: "one") != _versioned(lambda match: "two")

    def test_changes_when_a_pattern_flag_changes(self, monkeypatch):
        import re

        from rootcoz.engine import core

        before = normalization_rules_version()
        monkeypatch.setattr(
            core,
            "_NORMALIZE_PATTERNS",
            core._NORMALIZE_PATTERNS + [(re.compile(r"build"), "<B>")],
        )
        insensitive = normalization_rules_version()
        monkeypatch.setattr(
            core,
            "_NORMALIZE_PATTERNS",
            core._NORMALIZE_PATTERNS + [(re.compile(r"build", re.IGNORECASE), "<B>")],
        )
        assert insensitive != before
        assert insensitive != normalization_rules_version()

    def test_docstring_edits_do_not_trigger_a_backfill(self, monkeypatch):
        """Bytecode/consts hashing made a comment change a migration."""
        from rootcoz.engine import core

        before = normalization_rules_version()
        monkeypatch.setattr(core.normalize_for_signature, "__doc__", "rewritten")
        assert normalization_rules_version() == before


class TestHeaderNormalization:
    def test_noisy_header_values_are_discarded(self):
        assert compute_signature(
            "GET /x\nDate: Sun, 31 May 2026 06:50:48 GMT", ""
        ) == compute_signature("GET /x\nDate: Mon, 01 Jun 2026 11:22:33 GMT", "")

    def test_per_request_trace_headers_are_ignored(self):
        assert compute_signature("GET /x\nx-request-id: aaa", "") == compute_signature(
            "GET /x\nx-request-id: bbb", ""
        )

    def test_distinct_custom_header_values_keep_distinct_signatures(self):
        assert compute_signature(
            "GET /x\nX-Error-Code: quota-exceeded", ""
        ) != compute_signature("GET /x\nX-Error-Code: invalid-token", "")


class TestEnsureSignaturesCurrent:
    async def test_runs_when_stale_and_records_the_version(self, db):
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )

        stats = await ensure_signatures_current()

        version = normalization_rules_version()
        assert stats is not None
        assert stats["failures_changed"] == 1
        assert await db.migration_applied(MIGRATION_KEY_PREFIX + version)
        assert await db.get_signature_versions() == (version, "")

    async def test_second_call_is_a_noop(self, db):
        await _store_job(db, "job1", _result(failures=[_failure("t", "boom")]))
        assert await ensure_signatures_current() is not None
        assert await ensure_signatures_current() is None

    async def test_a_deferred_job_keeps_the_version_unapplied(self, db):
        """A job left to a running analysis is revisited by the next start."""
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )
        await _set_status(db, "job1", "pending")

        stats = await ensure_signatures_current()

        version = normalization_rules_version()
        assert stats is not None
        assert stats["jobs_deferred"] == 1
        assert await _signatures(db, "job1") == ["stale-hash"]
        assert not await db.migration_applied(MIGRATION_KEY_PREFIX + version)
        assert await db.get_signature_versions() == ("", version)

        # The analysis gave up and restored the job: the next start finishes it.
        await _set_status(db, "job1", "completed")
        stats = await ensure_signatures_current()

        assert stats is not None
        assert stats["jobs_deferred"] == 0
        assert (await _signatures(db, "job1"))[0] == compute_signature(
            MESSAGE_WITH_NOISE, TRACE
        )
        assert await db.migration_applied(MIGRATION_KEY_PREFIX + version)

    async def test_rollback_to_an_earlier_version_rehashes_again(self, db):
        """A stale-but-present migration key must not gate the backfill."""
        await _store_job(
            db, "job1", _result(failures=_stale(_failure("t", MESSAGE_WITH_NOISE)))
        )
        version_a = normalization_rules_version()
        await ensure_signatures_current()
        await db.claim_signature_migration("version-b")
        await db.complete_signature_migration("version-b")
        stored = (await db.get_result("job1"))["result"]
        stored["failures"][0]["error_signature"] = "version-b-hash"
        await db.save_result("job1", "http://x", "completed", stored)
        # Both migration keys are on disk; only the applied version gates.
        assert await db.migration_applied(MIGRATION_KEY_PREFIX + version_a)

        stats = await ensure_signatures_current()

        assert stats is not None
        assert stats["failures_changed"] == 1
        assert (await _signatures(db, "job1"))[0] == compute_signature(
            MESSAGE_WITH_NOISE, TRACE
        )

    async def test_a_newer_version_takes_the_migration_over(self, db):
        """An older process cannot record itself applied once taken over."""
        await db.claim_signature_migration("version-a")
        await db.claim_signature_migration("version-b")

        assert await db.complete_signature_migration("version-a") is False
        assert await db.get_signature_versions() == ("", "version-b")
        assert await db.complete_signature_migration("version-b") is True
        assert await db.get_signature_versions() == ("version-b", "")

    async def test_backfill_stops_between_batches_when_preempted(self, db):
        for index in range(4):
            await _store_job(
                db,
                f"job{index}",
                _result(failures=_stale(_failure(f"t{index}", MESSAGE_WITH_NOISE))),
            )

        async def guard() -> bool:
            await db.claim_signature_migration("version-newer")
            return False

        stats = await backfill_signatures(dry_run=False, batch_size=2, guard=guard)

        assert stats["migration_preempted"] is True
        assert stats["jobs_scanned"] == 0
        assert await _signatures(db, "job0") == ["stale-hash"]
