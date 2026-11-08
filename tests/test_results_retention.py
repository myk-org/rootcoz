"""Tests for optional results retention, default off (#455).

The unit of retention is the whole analysis (job): the row, its test entries,
its discussion, its failure history and its chat workspace. Both limits are ``0``
by default, so an upgrade changes no behaviour.
"""

import asyncio
import contextlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from rootcoz import main, storage
from tests.conftest import make_app_client


@pytest.fixture
def db(temp_db_path: Path):
    """A real SQLite database the retention code can write to."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield temp_db_path


def _settings(retention_days: int = 0, max_rows: int = 0) -> SimpleNamespace:
    """The two settings ``prune_results_periodically`` reads."""
    return SimpleNamespace(
        results_retention_days=retention_days, results_max_rows=max_rows
    )


def _ts(days_ago: float) -> str:
    """The UTC ``%Y-%m-%d %H:%M:%S`` form ``results.created_at`` is stored in."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_job(
    db_path: Path,
    job_id: str,
    *,
    days_ago: float | None = 1.0,
    status: str = "completed",
    analysis_state: str = "analyzed",
    request_params: dict[str, Any] | None = None,
    activity_days_ago: float | None = None,
) -> None:
    """Insert one job row; ``days_ago=None`` leaves the clock columns NULL.

    ``activity_days_ago`` overrides ``analysis_started_at`` alone, which is what a
    re-analysis of an old job looks like: an old ``created_at``, fresh activity.
    """
    created = _ts(days_ago) if days_ago is not None else None
    started = (
        _ts(activity_days_ago if activity_days_ago is not None else days_ago)
        if created is not None
        else None
    )
    payload: dict[str, Any] = {"job_id": job_id, "summary": "s", "failures": []}
    if request_params is not None:
        payload["request_params"] = request_params
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO results (job_id, build_url, status, result_json,"
            " created_at, analysis_started_at, analysis_state)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                f"https://ci.example.com/job/{job_id}/1/",
                status,
                json.dumps(payload),
                created,
                started,
                analysis_state,
            ),
        )


def _seed_children(db_path: Path, job_id: str) -> None:
    """One row in every job-scoped table, so an empty table cannot pass a test."""
    with sqlite3.connect(db_path) as conn:
        comment = conn.execute(
            "INSERT INTO comments (job_id, test_name, comment, username)"
            " VALUES (?, ?, ?, ?)",
            (job_id, "t1", "hello", "alice"),
        )
        conn.execute(
            "INSERT INTO mention_reads (username, comment_id) VALUES (?, ?)",
            ("bob", comment.lastrowid),
        )
        conn.execute(
            "INSERT INTO failure_reviews (job_id, test_name, reviewed, username)"
            " VALUES (?, ?, ?, ?)",
            (job_id, "t1", 1, "alice"),
        )
        conn.execute(
            "INSERT INTO failure_history"
            " (job_id, job_name, build_number, test_name, error_message)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, job_id, 1, "t1", "boom"),
        )
        conn.execute(
            "INSERT INTO test_classifications"
            " (test_name, classification, created_by, job_id)"
            " VALUES (?, ?, ?, ?)",
            ("t1", "PRODUCT BUG", "alice", job_id),
        )
        conn.execute(
            "INSERT INTO ai_token_usage (id, job_id, ai_provider, ai_model)"
            " VALUES (?, ?, ?, ?)",
            (f"usage-{job_id}", job_id, "p", "m"),
        )
        conn.execute(
            "INSERT INTO chat_messages (job_id, role, content, username)"
            " VALUES (?, ?, ?, ?)",
            (job_id, "user", "hi", "alice"),
        )
        conn.execute(
            "INSERT INTO test_entries (job_id, test_name, status) VALUES (?, ?, ?)",
            (job_id, "t1", "failed"),
        )
        conn.execute(
            "INSERT INTO tracked_in_links (job_id, test_name, url) VALUES (?, ?, ?)",
            (job_id, "t1", "https://tracker.example.com/1"),
        )


def _count_scoped(db_path: Path, table: str, predicate: str, job_id: str) -> int:
    """Count rows the deletion spec would remove for *job_id*."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {predicate.format(jobs='?')}",
            (job_id,),
        ).fetchone()
    return int(row[0])


def _count_results(db_path: Path, job_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM results WHERE job_id = ?", (job_id,)
        ).fetchone()
    return int(row[0])


def _count_table(db_path: Path, table: str, job_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", (job_id,)
        ).fetchone()
    return int(row[0])


# --- disabled, and the whole-job unit ---------------------------------------


async def test_disabled_deletes_nothing(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")
    with patch.object(main, "get_settings", return_value=_settings()):
        assert await main.prune_results_periodically() == 0
    assert _count_results(db, "old") == 1
    assert _count_table(db, "test_entries", "old") == 1


async def test_deletes_whole_old_job(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")
    for table, predicate in storage._JOB_SCOPED_DELETES:
        assert _count_scoped(db, table, predicate, "old") > 0, table
    assert _count_results(db, "old") == 1

    result = await storage.prune_results(retention_days=30)

    assert result["deleted"] == 1
    assert result["deleted_ids"] == ["old"]
    for table, predicate in storage._JOB_SCOPED_DELETES:
        assert _count_scoped(db, table, predicate, "old") == 0, table
    assert _count_results(db, "old") == 0


async def test_keeps_job_inside_the_window(db: Path) -> None:
    _seed_job(db, "young", days_ago=29)
    _seed_children(db, "young")
    result = await storage.prune_results(retention_days=30)
    assert result["deleted"] == 0
    assert _count_results(db, "young") == 1
    assert _count_table(db, "test_entries", "young") == 1


async def test_keeps_audit_metadata_and_other_jobs(db: Path) -> None:
    """§3.2: the audit trail, ``job_metadata*`` and other jobs survive."""
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")
    _seed_job(db, "other", days_ago=1)
    _seed_children(db, "other")
    await storage.insert_audit_event(
        action="analysis.submit", actor="alice", target_type="job", target_id="old"
    )
    await storage.set_job_metadata("OLD-JOB", team="cnv", labels=["ci", "smoke"])

    await storage.prune_results(retention_days=30)

    assert _count_results(db, "old") == 0
    events = (await storage.list_audit_events(action="analysis.submit"))["events"]
    assert [event["target_id"] for event in events] == ["old"]
    metadata = await storage.get_job_metadata("OLD-JOB")
    assert metadata is not None
    assert metadata["labels"] == ["ci", "smoke"]
    assert _count_results(db, "other") == 1
    assert _count_table(db, "test_entries", "other") == 1


async def test_pruned_override_is_not_inherited(db: Path) -> None:
    """§3.4.4: the inheritance reads other jobs, so it stops working for a pruned job."""
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")

    def _result(job_id: str) -> dict[str, Any]:
        return {
            "job_name": "",
            "failures": [{"test_name": "t1"}],
            "child_job_analyses": [],
        }

    assert await storage.carry_forward_user_overrides("new", _result("new")) == 1
    # Drop the copy the first call made, so the second call can only see the
    # pruned job's own row.
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM test_classifications WHERE job_id = 'new'")
    await storage.prune_results(retention_days=30)
    assert await storage.carry_forward_user_overrides("newer", _result("newer")) == 0


# --- the two limits, OR-ed -------------------------------------------------


async def test_both_limits_are_ored(db: Path) -> None:
    """The age policy is not floored by the cap, and the cap still bounds a young set.

    Both rules require the *activity* clock to be past the cutoff too (a job that
    was re-analysed yesterday is never aged by its submission), so the pair is
    exercised on an all-old population where the age rule must delete the job the
    cap's keep-set would have kept.
    """
    for index, days in enumerate((45, 44, 43, 42, 41)):
        _seed_job(db, f"old-{index}", days_ago=days)
    _seed_job(db, "young", days_ago=1)

    preview = await storage.preview_results_retention(30, 2)
    result = await storage.prune_results(30, 2)

    # old-4 is the newest old job, so the cap alone keeps it: the OR deletes it.
    assert result["deleted_ids"] == [f"old-{i}" for i in range(5)]
    assert preview["jobs"] == 5
    assert preview["rows"]["results"] == len(result["deleted_ids"])
    assert _count_results(db, "young") == 1

    # The cap alone still bounds a young set: only the newest two survive.
    for index, days in enumerate((5, 4, 3)):
        _seed_job(db, f"new-{index}", days_ago=days)
    capped = await storage.prune_results(0, 2)
    assert sorted(capped["deleted_ids"]) == ["new-0", "new-1"]
    assert _count_results(db, "young") == 1
    assert _count_results(db, "new-2") == 1


async def test_age_only_ignores_the_row_cap(db: Path) -> None:
    _seed_job(db, "aged", days_ago=40)
    _seed_job(db, "young", days_ago=1)
    result = await storage.prune_results(retention_days=30, max_rows=0)
    assert result["deleted_ids"] == ["aged"]
    assert _count_results(db, "young") == 1


async def test_row_cap_only_ignores_age(db: Path) -> None:
    _seed_job(db, "ancient-but-newest", days_ago=400)
    _seed_job(db, "older", days_ago=500)
    _seed_job(db, "oldest", days_ago=600)
    result = await storage.prune_results(retention_days=0, max_rows=2)
    assert result["deleted_ids"] == ["oldest"]
    # A 400-day-old job inside the newest two survives: age is off.
    assert _count_results(db, "ancient-but-newest") == 1
    assert _count_results(db, "older") == 1


async def test_row_cap_keeps_newest(db: Path) -> None:
    for index, days in enumerate((4, 3, 2, 1)):
        _seed_job(db, f"job-{index}", days_ago=days)
    result = await storage.prune_results(retention_days=0, max_rows=2)
    assert sorted(result["deleted_ids"]) == ["job-0", "job-1"]
    assert _count_results(db, "job-2") == 1
    assert _count_results(db, "job-3") == 1


async def test_row_cap_counts_only_analysed_jobs(db: Path) -> None:
    _seed_job(db, "sub-1", days_ago=10, status="completed", analysis_state="submitted")
    _seed_job(db, "sub-2", days_ago=10, status="completed", analysis_state="submitted")
    _seed_job(db, "run-1", days_ago=9, status="running")
    _seed_job(db, "run-2", days_ago=9, status="pending")
    _seed_job(db, "analysed", days_ago=8)
    _seed_children(db, "analysed")

    result = await storage.prune_results(retention_days=0, max_rows=1)

    assert result["deleted"] == 0
    assert _count_results(db, "analysed") == 1
    assert _count_table(db, "test_entries", "analysed") == 1


async def test_row_cap_never_deletes_an_in_progress_job(db: Path) -> None:
    _seed_job(db, "running-oldest", days_ago=10, status="running")
    _seed_job(db, "completed-newer", days_ago=1)
    result = await storage.prune_results(retention_days=0, max_rows=1)
    assert result["deleted"] == 0
    assert _count_results(db, "running-oldest") == 1
    assert _count_results(db, "completed-newer") == 1


async def test_never_deletes_an_in_progress_job(db: Path) -> None:
    _seed_job(db, "running", days_ago=400, status="running")
    result = await storage.prune_results(retention_days=1)
    assert result["deleted"] == 0
    assert _count_results(db, "running") == 1


async def test_never_deletes_a_submitted_job(db: Path) -> None:
    _seed_job(
        db, "ingest", days_ago=400, status="completed", analysis_state="submitted"
    )
    result = await storage.prune_results(retention_days=1)
    assert result["deleted"] == 0
    aged_cap = await storage.prune_results(retention_days=0, max_rows=1)
    assert aged_cap["deleted"] == 0
    assert _count_results(db, "ingest") == 1


async def test_never_deletes_an_undated_job(db: Path) -> None:
    _seed_job(db, "undated", days_ago=None)
    _seed_job(db, "dated", days_ago=1)
    assert (await storage.prune_results(retention_days=1))["deleted"] == 0
    assert (await storage.prune_results(retention_days=0, max_rows=1))["deleted"] == 0
    assert _count_results(db, "undated") == 1


async def test_late_analysis_of_an_old_submission_survives(db: Path) -> None:
    """The claim moves ``analysis_started_at``, so a late analysis is not aged."""
    _seed_job(db, "late", days_ago=400, status="completed", analysis_state="submitted")
    claim = await storage.claim_submitted_job_for_analyze("late")
    assert claim is not None
    await storage.update_status("late", "completed", {"job_id": "late"})

    result = await storage.prune_results(retention_days=30)
    assert result["deleted"] == 0
    cap = await storage.prune_results(retention_days=0, max_rows=1)
    assert cap["deleted"] == 0
    assert _count_results(db, "late") == 1


async def test_activity_columns_stay_comparable(db: Path) -> None:
    _seed_job(db, "job", days_ago=400, status="pending")
    await storage.update_status("job", "running", {"job_id": "job"})
    with sqlite3.connect(db) as conn:
        created, started = conn.execute(
            "SELECT created_at, analysis_started_at FROM results WHERE job_id = 'job'"
        ).fetchone()
    for value in (created, started):
        assert len(str(value)) == 19
        assert str(value)[4] == "-" and str(value)[13] == ":"


# --- the settings map to the right roles -----------------------------------


async def test_settings_map_to_the_right_roles(db: Path) -> None:
    """A swapped argument order fails this and nothing else."""
    _seed_job(db, "aged", days_ago=40)
    _seed_job(db, "young-1", days_ago=3)
    _seed_job(db, "young-2", days_ago=2)
    _seed_job(db, "young-3", days_ago=1)

    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        assert await main.prune_results_periodically() == 1
    assert _count_results(db, "aged") == 0
    assert _count_results(db, "young-1") == 1

    main._last_results_prune_at = 0.0
    with patch.object(main, "get_settings", return_value=_settings(0, 2)):
        assert await main.prune_results_periodically() == 1
    assert _count_results(db, "young-1") == 0
    assert _count_results(db, "young-2") == 1
    assert _count_results(db, "young-3") == 1


# --- post-delete side effects ----------------------------------------------


async def test_prune_removes_the_chat_workspace(db: Path) -> None:
    from rootcoz.engine.chat import ensure_chat_workspace

    _seed_job(db, "old", days_ago=400)
    workspace = ensure_chat_workspace("old")
    assert workspace.is_dir()

    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        await main.prune_results_periodically()

    assert not workspace.exists()


async def test_prune_calls_finish_job_deletion(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    _seed_job(db, "busy", days_ago=400)
    _seed_job(db, "origin", days_ago=400)
    _seed_job(
        db, "child", days_ago=400, request_params={"reanalyzed_from_job_id": "origin"}
    )
    finish = AsyncMock(return_value=[])
    busy = {"busy"}

    def _protected(job_id: str) -> bool:
        return job_id in busy

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(main, "_finish_job_deletion", finish),
        patch.object(main, "_retention_is_protected", _protected),
    ):
        await main.prune_results_periodically()

    assert finish.await_count == 1
    requested, deleted, origins = finish.await_args.args
    assert set(requested) >= set(deleted)
    assert "busy" not in deleted
    assert "old" in deleted
    assert origins.get("child") == "origin"


async def test_prune_unlinks_a_surviving_origin(db: Path) -> None:
    _seed_job(
        db, "origin", days_ago=1, request_params={"reanalyzed_to_job_ids": ["child"]}
    )
    _seed_job(
        db, "child", days_ago=400, request_params={"reanalyzed_from_job_id": "origin"}
    )

    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        await main.prune_results_periodically()

    stored = await storage.get_result("origin")
    assert stored is not None
    params = (stored.get("result") or {}).get("request_params") or {}
    assert params.get("reanalyzed_to_job_ids") == []


async def test_a_skipped_job_keeps_its_workspace_and_origin_link(db: Path) -> None:
    from rootcoz.engine.chat import ensure_chat_workspace

    _seed_job(
        db, "origin", days_ago=1, request_params={"reanalyzed_to_job_ids": ["child"]}
    )
    _seed_job(
        db, "child", days_ago=400, request_params={"reanalyzed_from_job_id": "origin"}
    )
    workspace = ensure_chat_workspace("child")

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(main, "_retention_is_protected", lambda _job: True),
    ):
        await main.prune_results_periodically()

    assert workspace.is_dir()
    stored = await storage.get_result("origin")
    assert stored is not None
    params = (stored.get("result") or {}).get("request_params") or {}
    assert params.get("reanalyzed_to_job_ids") == ["child"]
    assert _count_results(db, "child") == 1


# --- the live-task guards ---------------------------------------------------


async def test_in_flight_reanalysis_is_protected(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")
    task = asyncio.current_task()
    assert task is not None
    main._failure_reanalysis_jobs.setdefault("old", set()).add(task)
    try:
        with patch.object(main, "get_settings", return_value=_settings(30, 0)):
            assert await main.prune_results_periodically() == 0
    finally:
        main._failure_reanalysis_jobs.pop("old", None)
    assert _count_results(db, "old") == 1
    assert _count_table(db, "chat_messages", "old") == 1


async def test_two_concurrent_reanalyses_keep_the_job_protected() -> None:
    """The first task's completion must not release protection for the second."""
    job_id = "job-1"
    first: asyncio.Task[Any] = asyncio.get_running_loop().create_future()
    second: asyncio.Task[Any] = asyncio.get_running_loop().create_future()
    main._track_failure_reanalysis(job_id, first, None)
    main._track_failure_reanalysis(job_id, second, None)
    assert main._retention_is_protected(job_id)

    first.set_result(None)
    await asyncio.sleep(0)
    assert main._retention_is_protected(job_id)

    second.set_result(None)
    await asyncio.sleep(0)
    assert not main._retention_is_protected(job_id)


async def test_chat_turn_holds_the_job_busy(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")
    task = asyncio.current_task()
    assert task is not None
    main._chat_job_tasks.setdefault("old", set()).add(task)
    try:
        with patch.object(main, "get_settings", return_value=_settings(30, 0)):
            assert await main.prune_results_periodically() == 0
    finally:
        main._chat_job_tasks.pop("old", None)
    assert _count_results(db, "old") == 1
    assert _count_table(db, "chat_messages", "old") == 1


async def test_aborted_job_with_a_live_task_is_not_deleted(db: Path) -> None:
    _seed_job(db, "aborted", days_ago=400, status="aborted")
    _seed_children(db, "aborted")
    main._job_tasks["aborted"] = asyncio.current_task()  # type: ignore[assignment]
    try:
        with patch.object(main, "get_settings", return_value=_settings(30, 0)):
            assert await main.prune_results_periodically() == 0
    finally:
        main._job_tasks.pop("aborted", None)
    assert _count_results(db, "aborted") == 1
    assert _count_table(db, "test_entries", "aborted") == 1


async def test_protected_is_evaluated_at_delete_time(db: Path) -> None:
    """A guard that flips after selection skips the job; it is never a failure."""
    _seed_job(db, "old", days_ago=400)
    calls: list[str] = []

    def _protected(job_id: str) -> bool:
        calls.append(job_id)
        return True

    result = await storage.prune_results(retention_days=30, is_protected=_protected)
    assert calls == ["old"]
    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == []


async def test_job_that_left_the_ended_set_is_skipped(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    seen: list[str] = []

    async def _prepare(job_ids: list[str]) -> dict[str, str]:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE results SET status = 'running' WHERE job_id = 'old'")
        seen.extend(job_ids)
        return {}

    result = await storage.prune_results(prepare=_prepare, retention_days=30)

    assert seen == ["old"]
    assert result["skipped"] == 1
    assert result["deleted"] == 0
    assert _count_results(db, "old") == 1


# --- failures, bounds, and the clock ---------------------------------------


async def test_a_failing_old_job_does_not_block_newer_candidates(db: Path) -> None:
    for index, days in enumerate((400, 300, 200)):
        _seed_job(db, f"job-{index}", days_ago=days)

    async def _prepare(job_ids: list[str]) -> dict[str, str]:
        if "job-0" in job_ids:
            raise RuntimeError("old job is stuck")
        return {}

    # One job per chunk, so the stuck job fails alone.
    result = await storage.prune_results(
        prepare=_prepare, retention_days=30, batch_size=1
    )

    assert result["deleted_ids"] == ["job-1", "job-2"]
    assert [item["job_id"] for item in result["failed"]] == ["job-0"]
    assert _count_results(db, "job-0") == 1


async def test_a_stuck_job_costs_one_failure_row_per_process(db: Path) -> None:
    _seed_job(db, "stuck", days_ago=400)

    async def _prepare(job_ids: list[str]) -> dict[str, str]:
        raise RuntimeError("cannot read the payload")

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(main, "_retention_prepare", _prepare),
    ):
        assert await main.prune_results_periodically() == 0
        assert "stuck" in main._results_retention_stuck_ids
        # The next hourly wake excludes it: nothing attempted, nothing written,
        # and the slot is stamped.
        assert await main.prune_results_periodically() == 0
        assert main._last_results_prune_at > 0

    rows = (await storage.list_audit_events(action="storage.results_retention"))[
        "events"
    ]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"


async def test_run_is_bounded_and_says_so(db: Path) -> None:
    for index, days in enumerate((400, 300, 200)):
        _seed_job(db, f"job-{index}", days_ago=days)

    real_prune = storage.prune_results

    async def _bounded(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return await real_prune(retention_days=30, max_per_run=2, stats=kwargs["stats"])

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(storage, "prune_results", _bounded),
    ):
        assert await main.prune_results_periodically() == 2

    assert _count_results(db, "job-0") == 0
    assert _count_results(db, "job-1") == 0
    # The newest survives, and a truncated pass leaves the daily slot open.
    assert _count_results(db, "job-2") == 1
    assert main._last_results_prune_at == 0.0


async def test_candidate_sql_binds_every_id(db: Path) -> None:
    """Nothing is interpolated, and an empty exclude composes a runnable statement."""
    sql, params = storage._results_retention_candidates_sql(30, 2)
    assert "job-0" not in sql
    result = await storage.prune_results(
        retention_days=30, max_rows=2, exclude=("job-0", "job-1")
    )
    assert result["error"] == ""
    assert params  # the composed statement binds its limits


async def test_cutoff_is_utc_space_format(db: Path, monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    _seed_job(db, "inside", days_ago=29 + 23 / 24)
    _seed_job(db, "outside", days_ago=30 + 1 / 24)

    preview = await storage.preview_results_retention(30, 0)
    assert preview["jobs"] == 1
    assert preview["oldest_created_at"] == _ts(30 + 1 / 24)

    result = await storage.prune_results(retention_days=30)
    assert result["deleted_ids"] == ["outside"]
    assert _count_results(db, "inside") == 1
    assert len(result["oldest_deleted_at"]) == 19
    assert result["oldest_deleted_at"][4] == "-"


# --- the audit row ----------------------------------------------------------


def _retention_rows(db: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT action, actor, outcome, target_type, details"
            " FROM audit_events WHERE action = 'storage.results_retention'"
            " ORDER BY id"
        ).fetchall()
    return [
        {
            "action": row["action"],
            "actor": row["actor"],
            "outcome": row["outcome"],
            "target_type": row["target_type"],
            "details": json.loads(row["details"] or "{}"),
        }
        for row in rows
    ]


async def test_prune_writes_exactly_one_audit_row(db: Path) -> None:
    _seed_job(db, "old-1", days_ago=400)
    _seed_job(db, "old-2", days_ago=300)

    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        await main.prune_results_periodically()

    rows = _retention_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "storage.results_retention"
    assert row["actor"] == "system"
    assert row["outcome"] == "success"
    assert row["target_type"] == "storage"
    assert row["details"]["deleted_jobs"] == 2
    assert sorted(row["details"]["job_ids"]) == ["old-1", "old-2"]
    assert row["details"]["oldest_deleted_at"]
    assert row["details"]["newest_deleted_at"]


async def test_pruned_job_stays_explainable_in_the_trail(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    await storage.insert_audit_event(
        action="analysis.submit", actor="alice", target_type="job", target_id="old"
    )

    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        await main.prune_results_periodically()

    events = (await storage.list_audit_events(action="analysis.submit"))["events"]
    assert len(events) == 1
    assert events[0]["target_gone"] is True
    assert events[0]["target_deleted_by"] == "system"


async def test_no_audit_row_when_no_candidate_was_selected(db: Path) -> None:
    _seed_job(db, "young", days_ago=1)
    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        await main.prune_results_periodically()
    assert _retention_rows(db) == []


async def test_skipped_only_pass_is_audited(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(main, "_retention_is_protected", lambda _job: True),
    ):
        await main.prune_results_periodically()

    rows = _retention_rows(db)
    assert len(rows) == 1
    assert rows[0]["details"]["deleted_jobs"] == 0
    assert rows[0]["details"]["skipped_jobs"] == 1
    assert rows[0]["outcome"] == "success"


async def test_raising_prepare_is_a_deletion_failure(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")

    async def _prepare(job_ids: list[str]) -> dict[str, str]:
        raise RuntimeError("cannot read the payload")

    result = await storage.prune_results(prepare=_prepare, retention_days=30)

    assert [item["reason"] for item in result["failed"]] == ["hook_failed"]
    assert result["deleted"] == 0
    assert _count_results(db, "old") == 1
    assert _count_table(db, "test_entries", "old") == 1


async def test_raising_finish_is_a_cleanup_failure(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)

    async def _finish(requested, deleted, origins):
        raise RuntimeError("workspace is gone already")

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(main, "_finish_job_deletion", _finish),
    ):
        assert await main.prune_results_periodically() == 1

    rows = _retention_rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["details"]["cleanup_failed_jobs"] == 1
    assert rows[0]["details"]["deleted_jobs"] == 1
    assert "old" in main._results_retention_cleanup_pending
    assert "old" not in main._results_retention_stuck_ids
    assert main._last_results_prune_at == 0.0

    # The next hourly wake retries the cleanup, not a second deletion.
    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        await main.prune_results_periodically()
    assert main._results_retention_cleanup_pending == set()
    assert len(_retention_rows(db)) == 1


async def test_cancelled_pass_still_audits_what_it_deleted(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    real_prune = storage.prune_results

    async def _cancelled(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        # Commit a chunk for real, then behave like a pass cancelled mid-run.
        await real_prune(retention_days=30, stats=kwargs["stats"])
        raise asyncio.CancelledError

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(storage, "prune_results", _cancelled),
        pytest.raises(asyncio.CancelledError),
    ):
        await main.prune_results_periodically()

    assert _count_results(db, "old") == 0
    rows = _retention_rows(db)
    assert len(rows) == 1
    assert rows[0]["details"]["job_ids"] == ["old"]
    assert rows[0]["details"]["deleted_jobs"] == 1
    assert main._results_prune_in_flight is False


async def test_aborted_pass_reports_what_it_deleted(db: Path) -> None:
    _seed_job(db, "job-0", days_ago=400)
    _seed_job(db, "job-1", days_ago=300)
    real_prune = storage.prune_results
    real_bulk = storage.delete_jobs_bulk
    calls = {"n": 0}

    async def _flaky(job_ids: list[str], **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return await real_bulk(job_ids, **kwargs)

    async def _one_job_per_chunk(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return await real_prune(retention_days=30, batch_size=1, stats=kwargs["stats"])

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(storage, "prune_results", _one_job_per_chunk),
        patch.object(storage, "delete_jobs_bulk", _flaky),
    ):
        await main.prune_results_periodically()

    assert _count_results(db, "job-0") == 0
    assert _count_results(db, "job-1") == 1
    rows = _retention_rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["details"]["reason"] == "commit_failed"
    assert rows[0]["details"]["job_ids"] == ["job-0"]
    assert main._last_results_prune_at == 0.0


async def test_failed_prune_writes_one_failure_row(db: Path) -> None:
    async def _no_candidates(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise sqlite3.OperationalError("database is locked")

    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(storage, "_retention_candidate_rows", _no_candidates),
    ):
        assert await main.prune_results_periodically() == 0

    rows = _retention_rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["details"]["reason"] == "select_failed"
    assert main._last_results_prune_at == 0.0


async def test_dropped_audit_row_does_not_stamp_the_slot(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    with (
        patch.object(main, "get_settings", return_value=_settings(30, 0)),
        patch.object(main, "record_audit", AsyncMock(return_value=None)),
    ):
        assert await main.prune_results_periodically() == 1
    assert main._last_results_prune_at == 0.0
    assert _retention_rows(db) == []


# --- the schedule -----------------------------------------------------------


async def test_prune_runs_at_most_daily(db: Path) -> None:
    _seed_job(db, "old-1", days_ago=400)
    _seed_job(db, "old-2", days_ago=300)
    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        assert await main.prune_results_periodically() == 2
        assert await main.prune_results_periodically() == 0
    assert _count_results(db, "old-2") == 0


async def test_concurrent_wake_is_skipped(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    main._results_prune_in_flight = True
    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        assert await main.prune_results_periodically() == 0
    assert _count_results(db, "old") == 1


async def test_hourly_loop_calls_the_prune(db: Path) -> None:
    prune = AsyncMock(return_value=0)
    audit = AsyncMock(return_value=0)
    cleanup = AsyncMock(return_value=0)
    revoke = AsyncMock(return_value=None)

    async def _sleep(_seconds: float) -> None:
        if _sleep.calls:
            raise asyncio.CancelledError
        _sleep.calls += 1

    _sleep.calls = 0
    with (
        patch.object(main.asyncio, "sleep", _sleep),
        patch.object(main, "prune_audit_events_periodically", audit),
        patch.object(main, "prune_results_periodically", prune),
        patch.object(main.storage, "cleanup_expired_sessions", cleanup),
        patch.object(main, "_cleanup_revoked_ai_sessions", revoke),
        pytest.raises(asyncio.CancelledError),
    ):
        await main._periodic_session_cleanup()

    assert audit.await_count == 1
    assert prune.await_count == 1


async def test_lifespan_spawns_the_prune(temp_db_path: Path) -> None:
    """Startup spawns the pass (never awaits it) after the DB settings load."""
    started: list[bool] = []

    async def _prune() -> int:
        started.append(True)
        return 0

    with patch.object(main, "prune_results_periodically", _prune):
        gen = make_app_client(temp_db_path)
        next(gen)  # app startup runs the lifespan
        try:
            await asyncio.sleep(0)
            assert started == [True]
        finally:
            with contextlib.suppress(StopIteration):
                next(gen)


async def test_startup_prune_respects_saved_retention(
    temp_db_path: Path, monkeypatch
) -> None:
    """A saved ``results_retention_days=0`` survives a restart."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        _seed_job(temp_db_path, "old", days_ago=400)
        await storage.set_server_setting("results_retention_days", "0")
    monkeypatch.setenv("RESULTS_RETENTION_DAYS", "30")
    monkeypatch.setattr(main, "_last_results_prune_at", 0.0)

    gen = make_app_client(temp_db_path)
    next(gen)
    try:
        await asyncio.sleep(0.2)
        assert _count_results(temp_db_path, "old") == 1
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)


# --- the dry run ------------------------------------------------------------


async def test_preview_matches_the_prune(db: Path) -> None:
    _seed_job(db, "old-1", days_ago=400)
    _seed_children(db, "old-1")
    _seed_job(db, "old-2", days_ago=300)
    _seed_children(db, "old-2")
    _seed_job(db, "young", days_ago=1)
    _seed_job(db, "excluded", days_ago=350)
    main._results_retention_stuck_ids.add("excluded")
    protected = {"old-2"}

    with patch.object(main, "get_settings", return_value=_settings(30, 0)):
        preview = await storage.preview_results_retention(
            30,
            0,
            exclude=main._results_retention_stuck_ids,
            is_protected=lambda job: job in protected,
        )
        result = await storage.prune_results(
            30,
            0,
            exclude=main._results_retention_stuck_ids,
            is_protected=lambda job: job in protected,
        )

    assert preview["jobs"] == len(result["deleted_ids"]) == 1
    assert result["deleted_ids"] == ["old-1"]
    assert preview["rows"]["results"] == 1
    assert preview["rows"]["test_entries"] == 1
    assert preview["rows"]["comments"] == 1
    assert preview["rows"]["mention_reads"] == 1


async def test_preview_deletes_nothing(db: Path) -> None:
    _seed_job(db, "old", days_ago=400)
    _seed_children(db, "old")
    preview = await storage.preview_results_retention(30, 0)
    assert preview["jobs"] == 1
    assert preview["rows"]["results"] == 1
    assert _count_results(db, "old") == 1
    assert _count_table(db, "test_entries", "old") == 1
    assert _retention_rows(db) == []


async def test_preview_honours_the_live_guard(db: Path) -> None:
    _seed_job(db, "analysing", days_ago=400)
    _seed_job(db, "chatting", days_ago=400)
    _seed_job(db, "free", days_ago=400)
    task = asyncio.current_task()
    assert task is not None
    main._job_tasks["analysing"] = task  # type: ignore[assignment]
    main._chat_job_tasks.setdefault("chatting", set()).add(task)
    try:
        preview = await storage.preview_results_retention(
            30, 0, is_protected=main._retention_is_protected
        )
    finally:
        main._job_tasks.pop("analysing", None)
        main._chat_job_tasks.pop("chatting", None)
    assert preview["jobs"] == 1
    assert preview["rows"]["results"] == 1


async def test_preview_honours_max_per_run(db: Path) -> None:
    for index, days in enumerate((400, 300, 200)):
        _seed_job(db, f"job-{index}", days_ago=days)
    preview = await storage.preview_results_retention(30, 0, max_per_run=2)
    assert preview["jobs"] == 2
    assert preview["truncated"] is True


async def test_preview_survives_more_than_the_variable_cap(db: Path) -> None:
    """40 000 ids is past SQLite's 32 766 bound variables: a number, not a 500."""
    total = 40000
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO results (job_id, status, result_json, created_at,"
            " analysis_started_at, analysis_state)"
            " VALUES (?, 'completed', '{}', ?, ?, 'analyzed')",
            [(f"job-{i}", _ts(400), _ts(400)) for i in range(total)],
        )
        conn.execute("DELETE FROM results WHERE rowid % 4 != 0")
    with sqlite3.connect(db) as conn:
        expected = conn.execute(
            "SELECT COUNT(*) FROM results WHERE status = 'completed'"
        ).fetchone()[0]
    preview = await storage.preview_results_retention(30, 0)
    assert preview["jobs"] == expected
    assert preview["rows"]["results"] == expected


async def test_preview_disabled_reports_nothing(client) -> None:
    response = client.get("/api/admin/results-retention", headers=_ADMIN_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["jobs"] == 0
    assert body["rows"] == {}


def test_preview_route_is_admin_only(client) -> None:
    response = client.get("/api/admin/results-retention")
    assert response.status_code in {401, 403}


def test_preview_route_honours_the_guard(client, temp_db_path: Path) -> None:
    _seed_job(temp_db_path, "analysing", days_ago=400)
    _seed_job(temp_db_path, "chatting", days_ago=400)
    _seed_job(temp_db_path, "free", days_ago=400)
    with (
        patch.object(main, "_job_tasks", {"analysing": object()}),
        patch.object(main, "_chat_job_tasks", {"chatting": set()}),
    ):
        response = client.get(
            "/api/admin/results-retention?retention_days=30",
            headers=_ADMIN_HEADERS,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["jobs"] == 1
    assert body["rows"]["results"] == 1


@pytest.fixture
def client(temp_db_path: Path):
    """TestClient whose env/DB patches stay active for the whole test body."""
    yield from make_app_client(temp_db_path)


_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret  # gitleaks:allow
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}
