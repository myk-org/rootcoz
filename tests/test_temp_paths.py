"""Reaper and ``delete_tree`` behaviour (#402 Steps 1-2).

Every test is in-memory or a tiny tree under ``tmp_path``; nothing touches the
real ``/tmp/rootcoz`` and nothing sleeps for more than 1 s.
"""

from __future__ import annotations

import asyncio
import errno
import os
import threading
import time
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from rootcoz.temp_paths import (
    cleanup_off_loop,
    delete_marker,
    delete_reusable_path,
    delete_tree,
    reusable_path_lock,
)


async def _tick_until(coro_done: asyncio.Future[object], interval: float = 0.01) -> int:
    """Count loop ticks while ``coro_done`` is pending."""
    ticks = 0
    while not coro_done.done():
        await asyncio.sleep(interval)
        ticks += 1
    return ticks


# -- Step 1: one reaper, off the loop -------------------------------------


async def test_cleanup_steps_never_run_on_the_event_loop() -> None:
    """A step must not see a running loop; a main-thread check would be a false pass."""
    seen: list[str] = []

    def step() -> None:
        # Raises in a worker thread — the proof this is not the loop.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        seen.append(threading.current_thread().name)

    await cleanup_off_loop("job-1", step)

    assert seen == ["rootcoz-reaper_0"]


async def test_cleanup_steps_run_in_the_callers_order() -> None:
    order: list[int] = []

    await cleanup_off_loop("job-1", *(partial(order.append, i) for i in range(4)))

    assert order == [0, 1, 2, 3]


async def test_a_failing_step_does_not_lose_the_rest(caplog) -> None:
    ran: list[str] = []
    secret = "https://x-token-auth:s3cr3t@example.com/repo"  # pragma: allowlist secret

    def boom() -> None:
        raise OSError(errno.EACCES, f"cannot remove {secret}")

    def second_failure() -> None:
        raise OSError(errno.EACCES, f"again {secret}")

    await cleanup_off_loop(
        "job-1",
        lambda: ran.append("first"),
        boom,
        second_failure,
        lambda: ran.append("last"),
    )

    assert ran == ["first", "last"]
    log = caplog.text
    assert "cleanup_failed what=job-1 step=boom error=PermissionError errno=13" in log
    assert "cleanup_failed what=job-1 step=second_failure" in log
    # The message can carry a repo URL before scrubbing — it is never logged.
    assert "example.com" not in log
    assert "s3cr3t" not in log


async def test_a_cancelled_caller_loses_neither_the_delete_nor_the_session_revoke() -> (
    None
):
    """Post-cancel ordering: the delete is shielded and the DB call is its own task."""
    submitted = threading.Event()
    deleted = threading.Event()
    session_calls: list[str] = []

    def slow_delete() -> None:
        submitted.set()
        time.sleep(0.3)
        deleted.set()

    async def fake_cleanup_ai_session(auth_header: str) -> None:
        session_calls.append(auth_header)

    async def body() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            session_task = asyncio.create_task(fake_cleanup_ai_session("Bearer token"))
            await cleanup_off_loop("job-1", slow_delete)
            await asyncio.shield(session_task)

    task = asyncio.create_task(body())
    await asyncio.sleep(0.05)
    task.cancel()
    assert await asyncio.to_thread(submitted.wait, 1)
    task.cancel()  # Hits the shielded await inside the finally.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)

    assert await asyncio.to_thread(deleted.wait, 1)
    for _ in range(100):
        if session_calls:
            break
        await asyncio.sleep(0.01)
    assert session_calls == ["Bearer token"]


# -- Steps 1-2: per-path lock and the partial-delete marker ---------------


async def test_create_waits_for_a_queued_delete_of_the_same_path(tmp_path) -> None:
    path = tmp_path / "chat-job" / "alice"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x" * 10)

    def slow_delete() -> None:
        time.sleep(0.3)

    started = time.monotonic()
    delete_task = asyncio.create_task(
        delete_reusable_path("job-1", path, partial(slow_delete))
    )
    await asyncio.sleep(0.05)
    async with reusable_path_lock("job-1", path):
        waited = time.monotonic() - started
        tree_visible = path.exists()
    await delete_task

    assert waited >= 0.25, "the creator did not wait for the queued delete"
    assert tree_visible is False, "the creator saw a half-deleted tree"
    assert not path.exists()


async def test_marker_outside_the_tree_decides_recreation(tmp_path) -> None:
    path = tmp_path / "chat-job" / "alice"
    path.mkdir(parents=True)
    (path / "session.json").write_text("live session")

    # No marker: an intact workspace is reused untouched.
    async with reusable_path_lock("job-1", path):
        pass
    assert (path / "session.json").read_text() == "live session"

    # A planted marker (sibling, never inside the AI's cwd) means re-create.
    marker = delete_marker(path)
    assert marker.parent == path.parent
    assert marker.name == f".{path.name}.rootcoz-deleting"
    marker.write_text("")
    async with reusable_path_lock("job-1", path):
        assert not path.exists()
    assert not marker.exists()


# -- Step 2: delete_tree ---------------------------------------------------


def test_delete_tree_reports_size_and_entries(tmp_path) -> None:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "file.bin").write_text("x" * 200)
    (root / "top.txt").write_text("y" * 50)

    nbytes, entries = delete_tree(root)

    assert nbytes == 250
    assert entries == 5  # tree, a, b, file.bin, top.txt
    assert not root.exists()


def test_delete_tree_never_follows_a_symlink_out_of_the_root(tmp_path) -> None:
    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me")
    (root / "link").symlink_to(outside)
    (root / "file.txt").write_text("hello")

    delete_tree(root)

    assert (outside / "keep.txt").read_text() == "keep me"
    assert not root.exists()


def test_unreadable_entry_does_not_abandon_the_tree(
    tmp_path, monkeypatch, caplog
) -> None:
    root = tmp_path / "tree"
    (root / "good").mkdir(parents=True)
    (root / "good" / "file.txt").write_text("x" * 100)
    poisoned = root / "poisoned"
    poisoned.mkdir()
    (poisoned / "file.txt").write_text("y")
    real_scandir = os.scandir

    def scandir(path):
        if os.fspath(path) == str(poisoned):
            raise PermissionError(errno.EACCES, "denied", str(poisoned))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    nbytes, entries = delete_tree(root)
    monkeypatch.undo()

    assert nbytes >= 100, "the readable half of the tree was not deleted"
    assert entries >= 2
    assert not (root / "good").exists()
    assert caplog.text.count("errno=13") == 1


def test_delete_tree_on_a_missing_path_is_a_no_op(tmp_path) -> None:
    assert delete_tree(tmp_path / "never-existed") == (0, 0)


# -- The two analysis ``finally`` blocks ----------------------------------


async def test_ci_source_analysis_finally_does_not_block_the_loop(
    temp_db_path: Path, monkeypatch
) -> None:
    """``main.py`` analysis ``finally``: a slow delete must not stall the loop."""
    from rootcoz import storage
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import UnifiedAnalyzeRequest
    from rootcoz.sources.base import CISource, CISourceResult

    def slow_cleanup(self) -> None:
        time.sleep(0.4)

    monkeypatch.setattr(CISource, "cleanup", slow_cleanup)
    monkeypatch.setattr(storage, "DB_PATH", temp_db_path)

    body = UnifiedAnalyzeRequest(
        type="jenkins",
        job_name="my-job",
        build_number=1,
        ai_provider="claude",
        ai_model="test-model",
    )
    with (
        patch.object(storage, "DB_PATH", temp_db_path),
        patch(
            "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
            new_callable=AsyncMock,
            return_value=CISourceResult(
                failures=[], skip_analysis=True, identity={"job_name": "my-job"}
            ),
        ),
        patch("rootcoz.main.safe_update_progress", new_callable=AsyncMock),
        patch(
            "rootcoz.main._preflight_sidecar_check",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await storage.init_db()
        await storage.save_result("job-lag", "", "pending", {"job_name": "my-job"})
        task = asyncio.create_task(
            _process_ci_source_analysis(
                job_id="job-lag",
                body=body,
                merged=type("M", (), {"model_dump": lambda self: {}})(),
                display_name="my-job",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )
        )
        ticks = await _tick_until(task)
        await task

    assert ticks >= 10, "the event loop was blocked during the analysis finally"


async def test_reanalyze_finally_does_not_block_the_loop(tmp_path, monkeypatch) -> None:
    """``main.py`` re-analyze ``finally``: same guarantee, second site."""
    from rootcoz import storage
    from rootcoz.main import _reanalyze_failure_background

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "loop-lag.db")

    def slow_cleanup() -> None:
        time.sleep(0.4)

    with (
        patch("rootcoz.main.RepositoryManager") as manager,
        patch(
            "rootcoz.main._create_ai_auth_header",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "rootcoz.main._resolve_settings_json_before_analysis",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        manager.return_value.create_workspace.return_value = tmp_path / "workspace"
        manager.return_value.clone_into.return_value = None
        manager.return_value.cleanup.side_effect = slow_cleanup
        await storage.init_db()
        await storage.save_result(
            "job-lag-2",
            "",
            "completed",
            {"failures": [{"id": "failure-id", "reanalysis_status": "running"}]},
        )
        task = asyncio.create_task(
            _reanalyze_failure_background(
                "job-lag-2",
                "failure-id",
                {},
                "claude",
                "opus",
                None,
                "",
                None,
                0,
                "https://example.com/tests",
                "main",
                "",
                None,
                "admin",
                1,
            )
        )
        ticks = await _tick_until(task)
        await task

    assert manager.return_value.cleanup.called
    assert ticks >= 10, "the event loop was blocked during the re-analyze finally"
