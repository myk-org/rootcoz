"""Reaper and ``delete_tree`` behaviour (#402 Steps 1-2).

Every test is in-memory or a tiny tree under ``tmp_path``; nothing touches the
real ``/tmp/rootcoz`` and nothing sleeps for more than 1 s.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from rootcoz.temp_paths import (
    ABANDON,
    CleanupOutcome,
    PathState,
    PathStateUnknown,
    cleanup_off_loop,
    cleanup_path_off_loop,
    delete_marker,
    delete_tree,
    reusable_path_lock,
)
from rootcoz.temp_paths import logger as reaper_logger


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


def test_the_reaper_capture_counts_a_repeated_line_twice(caplog) -> None:
    """The autouse capture takes the duplicate filter off, it does not just clear it.

    The ``count(...) == 1`` assertions in this file exist to catch a line emitted
    twice, and a live filter collapses a repeat into one line before caplog sees
    it — which would make those assertions pass on the very bug they look for.
    """
    reaper_logger.warning("capture control: one repeated line")
    reaper_logger.warning("capture control: one repeated line")

    assert caplog.text.count("capture control: one repeated line") == 2


async def test_a_cancelled_caller_loses_neither_the_delete_nor_the_session_revoke() -> (
    None
):
    """Post-cancel ordering: the delete is shielded, and so is the revoke after it."""
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
            # The real sequence (``_reap_analysis_workspace``): the revoke is
            # created in the ``finally`` so a cancel cannot skip it, and after
            # the delete so the token outlives it.
            try:
                await cleanup_off_loop("job-1", slow_delete)
            finally:
                await asyncio.shield(
                    asyncio.create_task(fake_cleanup_ai_session("Bearer token"))
                )

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
        cleanup_path_off_loop("job-1", path, partial(slow_delete))
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


async def test_a_failed_reusable_delete_leaves_the_marker(
    tmp_path, monkeypatch
) -> None:
    """A delete that dies mid-tree must keep the marker, so the create re-clones."""
    path = tmp_path / "chat-job" / "alice"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "keep.txt").write_text("stuck")
    (path / "repo" / "gone.txt").write_text("x" * 10)
    real_unlink = os.unlink

    def unlink(name, *args, **kwargs):
        if os.path.basename(os.fspath(name)) == "keep.txt":
            raise PermissionError(errno.EACCES, "denied", str(name))
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    await cleanup_path_off_loop("job-1", path)

    assert delete_marker(path).exists(), "a dead delete left nothing to re-create from"
    assert not (path / "repo" / "gone.txt").exists()

    monkeypatch.undo()
    # The next create re-creates the whole workspace, so ``clone_chat_repos``
    # cannot skip a repo the failed delete took away.
    async with reusable_path_lock("job-1", path):
        assert not (path / "repo").exists()
        assert not path.exists()
    assert not delete_marker(path).exists()


async def test_the_path_lock_map_drops_idle_entries_and_keeps_held_ones(
    tmp_path,
) -> None:
    from rootcoz import temp_paths

    held_path = tmp_path / "busy"
    held_key, held = temp_paths._lock_for(held_path)
    await held.lock.acquire()
    try:
        for i in range(512):
            key, entry = temp_paths._lock_for(tmp_path / f"chat-{i}")
            # Chat workspaces are per (job, user) and jobs go away forever: an
            # entry with no holder and no waiter is dropped, so the map cannot
            # grow for the process lifetime.
            temp_paths._drop_lock(key, entry)
        assert len(temp_paths._path_locks) == 1
        assert temp_paths._path_locks[os.path.realpath(held_path)] is held
    finally:
        held.lock.release()
        temp_paths._drop_lock(held_key, held)
        temp_paths._path_locks.clear()


async def test_a_release_with_a_waiter_pending_keeps_the_entry(tmp_path) -> None:
    """``locked()`` is briefly false at a handoff: the waiter still owns the entry."""
    from rootcoz import temp_paths

    path = tmp_path / "chat-job" / "alice"
    path.mkdir(parents=True)
    key, holder = temp_paths._lock_for(path)
    await holder.lock.acquire()
    waiter_key, queued = temp_paths._lock_for(path)
    assert queued is holder, "a second caller got a different lock for one path"
    waiter = asyncio.create_task(holder.lock.acquire())
    await asyncio.sleep(0.01)  # The waiter is queued now.

    temp_paths._release_path_lock(key, holder)  # The holder finishes its delete.

    # Released, waiter not resumed yet: the old eviction (``not locked()``) would
    # drop this entry here and hand the next caller a second lock for the path.
    assert temp_paths._path_locks[key] is holder
    await waiter
    assert temp_paths._path_locks[key] is holder

    temp_paths._release_path_lock(waiter_key, queued)
    assert key not in temp_paths._path_locks


async def test_a_cancelled_waiter_does_not_leak_its_path_lock(tmp_path) -> None:
    """A waiter cancelled before it holds the lock still has a reference to drop."""
    from rootcoz import temp_paths

    path = tmp_path / "chat-job" / "alice"
    (path / "repo").mkdir(parents=True)
    key = os.path.realpath(path)
    gate = threading.Event()

    holder = asyncio.create_task(
        cleanup_path_off_loop("job-cancel", path, partial(gate.wait, 5))
    )
    try:
        for _ in range(200):
            entry = temp_paths._path_locks.get(key)
            if entry is not None and entry.lock.locked():
                break
            await asyncio.sleep(0.01)
        waiter = asyncio.create_task(cleanup_path_off_loop("job-cancel", path))
        await asyncio.sleep(0.05)  # The waiter is queued behind the holder now.
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        gate.set()
    await holder

    # Both references are gone: the entry must not survive as a stale lock that
    # no caller can ever evict (one per cancelled chat clear, for the process's
    # whole lifetime).
    assert key not in temp_paths._path_locks


async def test_an_abandoned_step_keeps_the_unrelated_always_steps() -> None:
    """ABANDON protects the builder's tree, not every other path in the job."""
    ran: list[str] = []

    def graph() -> object:
        return ABANDON

    await cleanup_off_loop(
        "job-always",
        graph,
        lambda: ran.append("repo_manager.cleanup"),
        always=(lambda: ran.append("source.cleanup"),),
    )

    assert ran == ["source.cleanup"], "the downloaded artifacts leaked past the abandon"


async def test_two_steps_do_not_swallow_each_other_in_the_log(caplog) -> None:
    """Per-step results must be formatted before logging.

    A duplicate is a repeated *rendered* line now: the filter keys on the rendered
    text, so an f-string per step puts each step's own figures on its own line. A
    silent delete must not look like a delete that never ran.
    """
    from simple_logger.logger import DuplicateFilter

    def boom() -> None:
        raise OSError(errno.EACCES, "denied")

    def bang() -> None:
        raise OSError(errno.EACCES, "denied")

    keeper = DuplicateFilter()
    reaper_logger.addFilter(keeper)
    try:
        with caplog.at_level(logging.INFO):
            await cleanup_off_loop(
                "job-dedupe", boom, bang, lambda: (10, 1), lambda: (20, 2)
            )
    finally:
        reaper_logger.removeFilter(keeper)

    assert "step=boom" in caplog.text
    assert "step=bang" in caplog.text, "the second failing step was deduped away"
    assert "reclaimed=10" in caplog.text
    assert "reclaimed=20" in caplog.text, "the second step's result was deduped away"


async def test_a_queued_waiter_keeps_its_lock_across_the_handoff(tmp_path) -> None:
    """Delete then create of one path: same lock object, no overlap."""
    from rootcoz import temp_paths

    path = tmp_path / "chat-job" / "alice"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x")
    key = os.path.realpath(path)
    gate = threading.Event()
    same_lock: list[bool] = []
    overlapped: list[bool] = []

    def gated_delete() -> None:
        gate.wait(5)

    holder = asyncio.create_task(
        cleanup_path_off_loop("job-1", path, partial(gated_delete))
    )
    entry = None
    for _ in range(200):
        entry = temp_paths._path_locks.get(key)
        if entry is not None and entry.lock.locked():
            break
        await asyncio.sleep(0.01)
    assert entry is not None
    held_lock = entry.lock

    async def creator() -> None:
        async with reusable_path_lock("job-1", path):
            same_lock.append(temp_paths._path_locks[key].lock is held_lock)
            overlapped.append(path.exists())

    waiter = asyncio.create_task(creator())
    try:
        await asyncio.sleep(0.05)  # The waiter is queued behind the delete now.
        # Map pressure during the handoff: other reusable paths pass through the
        # map, far more than the old size bound held at once.
        for i in range(512):
            other_key, other = temp_paths._lock_for(tmp_path / f"chat-{i}")
            temp_paths._drop_lock(other_key, other)
        assert temp_paths._path_locks[key].lock is held_lock
    finally:
        gate.set()
    await asyncio.gather(holder, waiter)

    assert same_lock == [True], "the waiter and the creator got different locks"
    assert overlapped == [False], "creation overlapped the queued delete"
    assert key not in temp_paths._path_locks


# -- Step 2: delete_tree ---------------------------------------------------


def test_delete_tree_reports_size_and_entries(tmp_path) -> None:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "file.bin").write_text("x" * 200)
    (root / "top.txt").write_text("y" * 50)

    nbytes, entries, failed = delete_tree(root)

    assert nbytes == 250
    assert entries == 5
    assert failed == 0  # tree, a, b, file.bin, top.txt
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


def test_delete_tree_unlinks_a_symlinked_root_without_its_target(tmp_path) -> None:
    """A symlinked workspace root is the link's problem, not its target's."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me")
    link = tmp_path / "workspace"
    link.symlink_to(outside)

    nbytes, entries, failed = delete_tree(link)

    assert (outside / "keep.txt").read_text() == "keep me"
    assert not link.is_symlink()
    assert (nbytes, entries, failed) == (len(os.fspath(outside)), 1, 0)


def test_a_directory_swapped_for_a_symlink_is_not_followed(
    tmp_path, monkeypatch, caplog
) -> None:
    """Check-then-use: a dir replaced by a link mid-walk must not redirect the walk."""
    root = tmp_path / "tree"
    victim = root / "victim"
    victim.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me")
    real_open = os.open
    swapped: list[str] = []

    def swapping_open(name, flags, *args, **kwargs):
        if not swapped and os.fspath(name) == victim.name:
            swapped.append(victim.name)
            victim.rmdir()
            victim.symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    delete_tree(root)
    monkeypatch.undo()

    assert swapped == [victim.name]
    assert (outside / "keep.txt").read_text() == "keep me"
    assert victim.is_symlink(), "the planted link was not left for the next pass"
    # O_NOFOLLOW|O_DIRECTORY refuses the swapped link (ENOTDIR or ELOOP).
    assert any(f"errno={code}" in caplog.text for code in (errno.ENOTDIR, errno.ELOOP))

    # A later pass unlinks the link itself and finishes the tree.
    delete_tree(root)
    assert not root.exists()
    assert (outside / "keep.txt").read_text() == "keep me"


def test_a_file_that_vanished_mid_walk_is_not_reported_as_undeleted(
    tmp_path, monkeypatch, caplog
) -> None:
    """A live agent deleting in its own workspace is not a failed reaper."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "gone.txt").write_text("x")
    real_unlink = os.unlink

    def unlink(name, *args, **kwargs):
        if os.path.basename(os.fspath(name)) == "gone.txt":
            # Really gone, then reported: exactly what another process's delete
            # between our ``scandir`` and our ``unlink`` looks like.
            real_unlink(name, *args, **kwargs)
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", name)
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    nbytes, entries, failed = delete_tree(root)
    monkeypatch.undo()

    assert not root.exists(), "a vanished entry stopped the walk"
    assert (nbytes, entries) == (0, 2)
    assert failed == 0, "a vanished entry is not a failed delete"
    # Positive control: the reaper's own lines do reach this capture, so the
    # absence below is a real absence and not an empty capture.
    reaper_logger.info("capture control reached caplog")
    assert "capture control reached caplog" in caplog.text
    assert "paths not deleted" not in caplog.text, caplog.text


def test_a_failed_delete_is_not_reported_as_reclaimed(
    tmp_path, monkeypatch, caplog
) -> None:
    root = tmp_path / "tree"
    (root / "good").mkdir(parents=True)
    (root / "good" / "file.txt").write_text("x" * 100)
    (root / "stuck.txt").write_text("y" * 7)
    real_unlink = os.unlink

    def unlink(name, *args, **kwargs):
        if os.path.basename(os.fspath(name)) == "stuck.txt":
            raise PermissionError(errno.EACCES, "denied", str(name))
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    nbytes, entries, failed = delete_tree(root)
    monkeypatch.undo()

    assert not (root / "good").exists(), "the rest of the tree was abandoned"
    assert (root / "stuck.txt").exists()
    assert failed == 2, "the stuck file and its root rmdir are the only failures"
    # ``reclaimed`` is what is really gone: the stuck file's 7 bytes and the root
    # directory (ENOTEMPTY) are excluded, the readable half is still counted.
    assert (nbytes, entries) == (100, 2)
    assert f"errno={errno.EACCES}" in caplog.text


def test_unreadable_entry_does_not_abandon_the_tree(
    tmp_path, monkeypatch, caplog
) -> None:
    root = tmp_path / "tree"
    (root / "good").mkdir(parents=True)
    (root / "good" / "file.txt").write_text("x" * 100)
    poisoned = root / "poisoned"
    poisoned.mkdir()
    (poisoned / "file.txt").write_text("y")
    real_open = os.open

    def open_dir(name, flags, *args, **kwargs):
        if os.fspath(name) == poisoned.name:
            raise PermissionError(errno.EACCES, "denied", str(name))
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_dir)
    nbytes, entries, failed = delete_tree(root)
    monkeypatch.undo()

    assert nbytes >= 100, "the readable half of the tree was not deleted"
    assert entries >= 2
    assert failed, "an unreadable directory is not a deleted one"
    assert not (root / "good").exists()
    assert caplog.text.count("errno=13") == 1
    # The poisoned directory's contents are unknown, not one entry.
    assert "1 directory not descended" in caplog.text, caplog.text


def test_a_scan_error_does_not_abandon_the_rest_of_the_tree(
    tmp_path, monkeypatch, caplog
) -> None:
    root = tmp_path / "tree"
    poisoned = root / "poisoned"
    poisoned.mkdir(parents=True)
    (poisoned / "stuck.txt").write_text("z")
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "file.txt").write_text("x" * 100)
    real_scandir = os.scandir
    poisoned_id = (poisoned.stat().st_dev, poisoned.stat().st_ino)

    def scandir(target):
        if isinstance(target, int):
            st = os.fstat(target)
            if (st.st_dev, st.st_ino) == poisoned_id:
                raise OSError(errno.EIO, "cannot scan")
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", scandir)
    nbytes, entries, failed = delete_tree(root)
    monkeypatch.undo()
    assert failed, "an unscannable directory leaves the tree behind"

    assert not sibling.exists(), "the sibling after the unscannable dir was abandoned"
    assert (poisoned / "stuck.txt").exists()
    assert (nbytes, entries) == (100, 2)  # sibling dir + its file, nothing else
    assert caplog.text.count(f"errno={errno.EIO}") == 1
    assert "1 directory not descended" in caplog.text, caplog.text

    delete_tree(root)
    assert not root.exists()


def _fail_unlink_of(names: set[str], monkeypatch: pytest.MonkeyPatch) -> None:
    real_unlink = os.unlink

    def unlink(name, *args, **kwargs):
        if os.path.basename(os.fspath(name)) in names:
            raise PermissionError(errno.EACCES, "denied", str(name))
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)


def _cleanup_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def test_two_distinct_errnos_are_one_aggregated_warning(
    tmp_path, monkeypatch, caplog
) -> None:
    """EACCES then ENOTEMPTY in one walk: one line naming both, not one each.

    One line per tree is the contract: the summary carries every errno of the walk
    with its distinct-path count, rather than one record per errno (#402).
    """
    root = tmp_path / "tree"
    (root / "good").mkdir(parents=True)
    (root / "good" / "file.txt").write_text("x" * 100)
    (root / "stuck.txt").write_text("y" * 7)
    _fail_unlink_of({"stuck.txt"}, monkeypatch)

    nbytes, entries, failed = delete_tree(root)
    monkeypatch.undo()

    # Accounting is unchanged: only what is really gone is counted.
    assert (nbytes, entries) == (100, 2)
    assert failed == 2, "the aggregated warning counts these same two paths"
    warnings = _cleanup_warnings(caplog)
    assert len(warnings) == 1, warnings
    line = warnings[0]
    assert line.startswith("cleanup: 2 paths not deleted ("), line
    assert f"errno={errno.EACCES} {os.strerror(errno.EACCES)}: 1" in line, line
    assert f"errno={errno.ENOTEMPTY} {os.strerror(errno.ENOTEMPTY)}: 1" in line, line
    assert "stuck.txt" in line, line
    assert "not descended" not in line, line


def test_repeated_errnos_are_counted_not_logged_per_entry(
    tmp_path, monkeypatch, caplog
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "stuck-a.txt").write_text("a")
    (root / "stuck-b.txt").write_text("b")
    _fail_unlink_of({"stuck-a.txt", "stuck-b.txt"}, monkeypatch)

    nbytes, entries, failed = delete_tree(root)
    monkeypatch.undo()

    assert (nbytes, entries) == (0, 0)
    assert failed == 3
    warnings = _cleanup_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith("cleanup: 3 paths not deleted ("), warnings[0]
    assert f"errno={errno.EACCES} {os.strerror(errno.EACCES)}: 2" in warnings[0], (
        warnings[0]
    )
    assert f"errno={errno.ENOTEMPTY} {os.strerror(errno.ENOTEMPTY)}: 1" in warnings[0]
    assert caplog.text.count("paths not deleted") == 1


def test_one_stuck_file_at_depth_four_counts_paths_not_operations(
    tmp_path, monkeypatch, caplog
) -> None:
    """The same surviving subtree re-reported by every ancestor rmdir is one path.

    One unlinkable file under a/b/c is EACCES on the file, then ENOTEMPTY on c, b,
    a and the root: four operations, one subtree. Reporting ``5 entries`` invented
    work that never existed.
    """
    root = tmp_path / "tree"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "stuck.txt").write_text("x")
    _fail_unlink_of({"stuck.txt"}, monkeypatch)

    assert delete_tree(root)[:2] == (0, 0)
    monkeypatch.undo()

    warnings = _cleanup_warnings(caplog)
    assert len(warnings) == 1, warnings
    line = warnings[0]
    assert line.startswith("cleanup: 2 paths not deleted ("), line
    assert f"errno={errno.EACCES} {os.strerror(errno.EACCES)}: 1" in line, line
    assert f"errno={errno.ENOTEMPTY} {os.strerror(errno.ENOTEMPTY)}: 1" in line, line
    # The example for the collapsed subtree is the deepest directory, the cause.
    assert os.fspath(deep) in line, line


def test_many_stuck_siblings_cost_no_more_than_the_walk_itself(
    tmp_path, monkeypatch, caplog
) -> None:
    """2 000 stuck siblings: one summary pass, not a scan of the set per insert.

    Collapsing ancestors inside ``record`` ran ``any(startswith)`` over every path
    recorded so far, so the no-match case here was O(n²) — four to five times the
    plain walk on this machine, and it runs on the single reaper worker, where it
    slows every cleanup queued behind it (#424). Measured against the same walk
    with nothing stuck, so the budget holds on a slow machine too.
    """

    def walk(name: str) -> Path:
        root = tmp_path / name
        for i in range(2000):
            directory = root / f"dir-{i:05d}"
            directory.mkdir(parents=True)
            (directory / "stuck.txt").write_text("x")
        return root

    clean_root = walk("clean-walk")
    started = time.perf_counter()
    delete_tree(clean_root)
    clean_seconds = time.perf_counter() - started

    stuck_root = walk("stuck-walk")
    _fail_unlink_of({"stuck.txt"}, monkeypatch)
    started = time.perf_counter()
    assert delete_tree(stuck_root)[:2] == (0, 0)
    stuck_seconds = time.perf_counter() - started
    monkeypatch.undo()

    assert stuck_seconds < clean_seconds * 2, (
        f"summary work dominates the walk: {stuck_seconds:.3f}s stuck vs "
        f"{clean_seconds:.3f}s clean"
    )
    warnings = _cleanup_warnings(caplog)
    assert len(warnings) == 1, warnings
    line = warnings[0]
    # 2 000 files (EACCES) + 2 000 directories (ENOTEMPTY); the root's own rmdir is
    # the same failure as the surviving siblings below it, so it is not a 4 001st.
    assert line.startswith("cleanup: 4000 paths not deleted ("), line
    assert f"errno={errno.EACCES} {os.strerror(errno.EACCES)}: 2000" in line, line
    assert f"errno={errno.ENOTEMPTY} {os.strerror(errno.ENOTEMPTY)}: 2000" in line, line


def test_a_clean_walk_logs_nothing(tmp_path, caplog) -> None:
    root = tmp_path / "tree"
    (root / "dir").mkdir(parents=True)
    (root / "dir" / "file.txt").write_text("x" * 10)

    assert delete_tree(root) == (10, 3, 0)  # dirs report 0 bytes on this fs
    assert caplog.records == []


def test_delete_tree_on_a_missing_path_is_a_no_op(tmp_path) -> None:
    assert delete_tree(tmp_path / "never-existed") == (0, 0, 0)


# -- The two analysis ``finally`` blocks ----------------------------------


def _abandoning_reap_graph(job_id: str, workspace: Path) -> object:
    return ABANDON


@contextmanager
def _builder_holding(path: Path) -> Iterator[None]:
    """Hold ``path``'s workspace install lock in another thread, as a builder does."""
    from concurrent.futures import ThreadPoolExecutor

    from rootcoz.engine.http_mcp import _workspace_install_lock

    entered, release = threading.Event(), threading.Event()

    def build() -> None:
        with _workspace_install_lock(path.resolve()):
            entered.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        builder = pool.submit(build)
        assert entered.wait(2)
        try:
            yield
        finally:
            release.set()
        builder.result(timeout=2)


async def test_an_abandoning_graph_step_skips_repo_manager_and_tree_delete(
    tmp_path, monkeypatch, caplog
) -> None:
    """The analysis caller keeps ``repo_manager.cleanup`` behind an abandon signal."""
    from rootcoz import temp_paths
    from rootcoz.engine import graft

    workspace = tmp_path / "workspace"
    (workspace / "repo").mkdir(parents=True)
    (workspace / "repo" / "file.txt").write_text("x")
    deleted: list[Path] = []
    repo_calls: list[str] = []
    monkeypatch.setattr(
        temp_paths, "delete_tree", lambda path: deleted.append(path) or (0, 0)
    )
    monkeypatch.setattr(graft, "reap_graph", _abandoning_reap_graph)

    def repo_cleanup() -> None:
        repo_calls.append("repo_manager.cleanup")

    await cleanup_off_loop(
        "job-abandon", partial(graft.reap_graph, "job-abandon", workspace), repo_cleanup
    )
    assert repo_calls == []
    assert workspace.exists()

    await cleanup_path_off_loop(
        "job-abandon",
        workspace,
        partial(graft.reap_graph, "job-abandon", workspace),
        repo_cleanup,
    )
    assert deleted == [], "the trailing whole-tree delete ran after an abandon"
    assert repo_calls == [], "repo_manager.cleanup ran after an abandon"
    assert workspace.exists()
    assert (
        "cleanup_abandoned what=job-abandon step=_abandoning_reap_graph" in caplog.text
    )


@pytest.mark.parametrize("clear", ["repos", "workspace"])
async def test_the_chat_clear_path_abandons_without_deleting(
    tmp_path, monkeypatch, clear
) -> None:
    """Both chat clear steps return the sentinel, so the queued tree delete is skipped."""
    from rootcoz import temp_paths
    from rootcoz.engine import chat, graft

    workspace = tmp_path / "chat-job"
    (workspace / "repo").mkdir(parents=True)
    (workspace / "repo" / "file.txt").write_text("x")
    deleted: list[Path] = []
    monkeypatch.setattr(
        temp_paths, "delete_tree", lambda path: deleted.append(path) or (0, 0)
    )
    monkeypatch.setattr(graft, "reap_graph", _abandoning_reap_graph)
    monkeypatch.setattr(chat, "get_chat_workspace", lambda *args, **kwargs: workspace)
    step = chat.cleanup_chat_repos if clear == "repos" else chat.cleanup_chat_workspace

    assert step("job-abandon") is ABANDON
    await cleanup_path_off_loop("job-abandon", workspace, partial(step, "job-abandon"))

    assert deleted == []
    assert workspace.exists(), "the chat clear deleted a live builder's workspace"
    assert delete_marker(workspace).exists()


async def test_the_normal_path_still_deletes_and_reports_bytes(
    tmp_path, caplog
) -> None:
    """No abandon: the tree goes and the step's reclaimed bytes are logged."""
    path = tmp_path / "chat-job"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x" * 10)

    def graph() -> tuple[int, int]:
        return (10, 1)

    with caplog.at_level(logging.INFO):
        await cleanup_path_off_loop("job-normal", path, graph)

    assert not path.exists()
    assert "cleanup what=job-normal step=graph reclaimed=10" in caplog.text
    assert not delete_marker(path).exists()


async def test_recovery_refuses_to_reuse_while_a_builder_holds_the_lock(
    tmp_path, monkeypatch
) -> None:
    """The builder that caused the abandon still owns the tree: refuse to reuse it."""
    from rootcoz import temp_paths
    from rootcoz.engine import graft

    path = tmp_path / "chat-job"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x")
    delete_marker(path).write_text("", encoding="utf-8")
    monkeypatch.setattr(graft, "GRAPH_LOCK_SECONDS", 0.05)
    deleted: list[Path] = []
    monkeypatch.setattr(
        temp_paths, "delete_tree", lambda target: deleted.append(target) or (0, 0)
    )

    with _builder_holding(path):
        with pytest.raises(PathStateUnknown):
            async with reusable_path_lock("job-recover", path):
                pytest.fail("a half-deleted tree was handed to the caller")
        assert deleted == [], "recovery deleted under a live builder"
        assert temp_paths.reap_reusable_path("job-recover", path) is ABANDON

    assert path.exists()
    assert delete_marker(path).exists(), "a refused recovery dropped the marker"


async def test_recovery_reaps_the_sibling_graph_tree(tmp_path) -> None:
    """A recovered chat must not keep the graph tree its abandoned delete skipped."""
    from rootcoz.engine.graft import _root

    path = tmp_path / "chat-job" / "alice"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x")
    graph = _root(path)
    (graph / "snapshot").mkdir(parents=True)
    (graph / "snapshot" / "source.py").write_text("x")
    delete_marker(path).write_text("", encoding="utf-8")

    async with reusable_path_lock("job-graph", path) as state:
        assert state is PathState.NOT_REUSABLE

    assert not path.exists()
    assert not graph.exists(), "recovery dropped the marker but kept the graph tree"
    assert not delete_marker(path).exists()


async def test_a_marker_that_cannot_be_written_never_deletes_the_tree(
    tmp_path, monkeypatch, caplog
) -> None:
    """No marker, no delete: an unrecognisable half-delete is worse than a leak."""
    from rootcoz import temp_paths

    path = tmp_path / "chat-job" / "alice"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x")
    monkeypatch.setattr(
        temp_paths,
        "delete_marker",
        lambda target: tmp_path / "missing-parent" / f".{target.name}.marker",
    )

    outcome = await cleanup_path_off_loop("job-mark", path)

    assert outcome is CleanupOutcome.INCOMPLETE
    assert (path / "repo" / "file.txt").read_text() == "x", (
        "the tree was deleted without a marker, so a later create would reuse "
        "whatever a dying delete left"
    )
    assert "could not write the delete marker" in caplog.text


async def test_a_permission_error_carrying_an_authenticated_url_logs_no_token(
    tmp_path, monkeypatch, caplog
) -> None:
    """The refusal names the path it could not write — never the token in its URL."""
    from rootcoz import temp_paths

    secret = "https://x-token-auth:s3cr3t@example.com/repo"  # pragma: allowlist secret
    path = tmp_path / "chat-job" / "alice"
    path.mkdir(parents=True)

    class _RefusedMarker:
        def write_text(self, *_args: object, **_kwargs: object) -> None:
            raise PermissionError(errno.EACCES, f"Permission denied: '{secret}'")

    monkeypatch.setattr(temp_paths, "delete_marker", lambda _target: _RefusedMarker())

    outcome = await cleanup_path_off_loop("job-secret", path)

    assert outcome is CleanupOutcome.INCOMPLETE
    assert "s3cr3t" not in caplog.text, "the marker failure logged the token"
    assert "x-token-auth" not in caplog.text
    # Still diagnosable: the type is the reason, the frame chain is the site.
    assert "could not write the delete marker" in caplog.text
    assert "error=PermissionError" in caplog.text
    assert "_mark_deleting" in caplog.text


async def test_a_failing_step_logs_the_failing_call_site(caplog) -> None:
    """A step that dies without an errno is only diagnosable with its frame chain."""

    def _exploding_step() -> None:
        raise RuntimeError("nothing an errno could explain")

    await cleanup_off_loop("job-traceback", _exploding_step)

    failed = [
        record
        for record in caplog.records
        if "cleanup_failed what=job-traceback" in record.getMessage()
    ]
    assert failed, "the failing step left no log line"
    message = failed[0].getMessage()
    assert "_exploding_step" in message, "the line names no failing call site"
    assert "temp_paths.py:" in message


async def test_recovery_deletes_after_the_builder_releases_the_lock(
    tmp_path, monkeypatch
) -> None:
    """No builder left: recovery deletes the marked tree and drops the marker."""
    from rootcoz.engine import graft

    path = tmp_path / "chat-job"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x")
    delete_marker(path).write_text("", encoding="utf-8")
    monkeypatch.setattr(graft, "GRAPH_LOCK_SECONDS", 0.05)

    with _builder_holding(path):
        pass  # The abandoning builder's lock, then released.

    async with reusable_path_lock("job-recover", path) as state:
        assert state is PathState.NOT_REUSABLE
        assert not path.exists(), "the marked tree was reused"
    assert not delete_marker(path).exists()


async def test_a_clean_recovery_still_reports_not_reusable(
    tmp_path, monkeypatch, caplog
) -> None:
    """Nothing left behind: the delete reports no failure, so reuse is refused.

    The guard for the case below: reporting the failure must not turn a delete
    that really removed the tree into ``UNKNOWN``.
    """
    from rootcoz.engine import chat

    workspace = tmp_path / "chat-job" / "alice"
    (workspace / "repo").mkdir(parents=True)
    (workspace / "repo" / "old.txt").write_text("x")
    delete_marker(workspace).write_text("", encoding="utf-8")
    monkeypatch.setattr(chat, "get_chat_workspace", lambda *args, **kwargs: workspace)

    async with chat.chat_workspace_lock("job-clean", "alice") as state:
        assert state is PathState.NOT_REUSABLE
        assert not workspace.exists(), "a clean recovery kept the old tree"
    assert not delete_marker(workspace).exists()
    assert delete_tree(workspace) == (0, 0, 0), "a gone path reports no failure"
    # Positive control: the reaper's own lines do reach this capture.
    reaper_logger.info("capture control reached caplog")
    assert "capture control reached caplog" in caplog.text
    assert "paths not deleted" not in caplog.text, caplog.text


async def test_a_partial_recovery_never_hands_back_the_residual_repo(
    tmp_path, monkeypatch, caplog
) -> None:
    """One unlinkable path makes the recovery incomplete, so init must not reuse.

    ``delete_tree`` logged the failed unlink and returned normally, so the step
    read as ``CLEAN``, ``_reuse_state`` answered ``NOT_REUSABLE``, and chat init
    went on to skip cloning into the repo directory the failed delete left.
    """
    from rootcoz.engine import chat

    workspace = tmp_path / "chat-job" / "alice"
    (workspace / "repo").mkdir(parents=True)
    (workspace / "repo" / "leftover.txt").write_text("stale")
    (workspace / "repo" / "stuck.txt").write_text("x")
    delete_marker(workspace).write_text("", encoding="utf-8")
    monkeypatch.setattr(chat, "get_chat_workspace", lambda *args, **kwargs: workspace)
    _fail_unlink_of({"stuck.txt"}, monkeypatch)
    reached: list[object] = []

    with pytest.raises(PathStateUnknown):
        async with chat.chat_workspace_lock("job-partial", "alice") as state:
            # ``main.py``'s chat init from here on: create, then clone.
            reached.append(state)
            reached.append(chat.ensure_chat_workspace("job-partial", "alice"))
    monkeypatch.undo()

    assert reached == [], "chat init went ahead on a half-deleted workspace"
    assert (workspace / "repo" / "stuck.txt").exists(), "the residue is still here"
    assert f"errno={errno.EACCES}" in caplog.text, caplog.text
    assert "failed=2" in caplog.text, caplog.text


async def test_an_abandoned_cleanup_keeps_the_marker_while_a_builder_holds_the_lock(
    tmp_path, monkeypatch
) -> None:
    """Recovery must not delete the tree the abandoning builder still owns."""
    from rootcoz.engine import graft

    path = tmp_path / "chat-job" / "alice"
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "file.txt").write_text("x")
    monkeypatch.setattr(graft, "reap_graph", _abandoning_reap_graph)
    monkeypatch.setattr(graft, "GRAPH_LOCK_SECONDS", 0.05)

    await cleanup_path_off_loop(
        "job-marker", path, partial(graft.reap_graph, "job-marker", path)
    )

    assert path.exists(), "the abandoned tree was deleted"
    assert delete_marker(path).exists(), "an abandoned delete dropped its marker"
    with _builder_holding(path):
        with pytest.raises(PathStateUnknown):
            async with reusable_path_lock("job-marker", path):
                pytest.fail("a live builder's tree was handed to the caller")
        assert path.exists(), "recovery deleted a live builder's tree"
        assert delete_marker(path).exists(), "recovery dropped the marker early"


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


async def test_clearing_a_chat_that_never_ran_is_a_clean_noop(tmp_path, caplog) -> None:
    """A workspace nobody ever created has nothing to mark, delete or warn about.

    ``_mark_deleting`` writes beside the tree, so a per-user ``chat-{job}/{user}``
    whose parent ``chat-{job}`` does not exist raised ENOENT, logged a warning and
    returned :data:`ABANDON`: clearing a chat that never started read as an
    incomplete cleanup with nothing left on disk (#424).
    """
    never_created = tmp_path / "chat-job-never" / "alice"

    outcome = await cleanup_path_off_loop("job-never", never_created)

    assert outcome is CleanupOutcome.CLEAN
    # Positive control: the reaper's own lines do reach this capture.
    reaper_logger.info("capture control reached caplog")
    assert "capture control reached caplog" in caplog.text
    assert "could not write the delete marker" not in caplog.text, caplog.text
    assert not never_created.parent.exists(), "the cleanup created the missing parent"
