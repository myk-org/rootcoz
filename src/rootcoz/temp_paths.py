"""Process-temp directories for workspaces and extracted artifacts.

Also owns the reaper: the single worker that deletes workspaces **off** the event
loop. `/health` is served by the same loop that runs the analysis ``finally``
blocks, so a delete inline there stalls the probes (#402).
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

logger = logging.getLogger(__name__)

# Under process temp root so chat symlink cleanup (honors TMPDIR) can delete it.
ROOTCOZ_TEMP_DIRNAME = "rootcoz"

#: One worker, deliberately: deleting in order is what keeps a burst of job
#: completions from stampeding the filesystem. A delete is bounded by nothing the
#: app controls, which is exactly why it is not on the loop. Non-daemon, so the
#: interpreter waits for queued deletes instead of leaking them.
_CLEANUP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rootcoz-reaper")

#: Locks for *reusable* workspaces only (``chat-{job}/{user}`` and the admin chat
#: workspace), where a queued delete can race the next create of the same path.
#: Keyed by ``realpath``; uuid workspaces are unique per use and need nothing, so
#: this dict is bounded by users, not by jobs.
_path_locks: dict[str, asyncio.Lock] = {}


def rootcoz_temp_base() -> Path:
    """Return ``{tempdir}/rootcoz`` (``tempfile.gettempdir()`` honors ``TMPDIR``)."""
    return Path(tempfile.gettempdir()) / ROOTCOZ_TEMP_DIRNAME


def delete_marker(path: Path) -> Path:
    """Marker written beside ``path`` while a delete of it is in flight.

    Beside, never inside: the workspace is the AI agent's cwd, so a marker there
    would be a "wipe my session" primitive a prompt-injected agent could plant.
    Outside it, the workspace's own users cannot state it and it stays invisible
    to ``read``/``ls``/``find``/``grep``.
    """
    return path.parent / f".{path.name}.rootcoz-deleting"


def _human(nbytes: int) -> str:
    return f"{nbytes / 1048576:.1f} MiB"


def _step_name(step: Callable[..., object]) -> str:
    func = getattr(step, "func", step)
    return getattr(func, "__name__", type(func).__name__)


def _mark_deleting(path: Path) -> None:
    try:
        delete_marker(path).write_text("", encoding="utf-8")
    except OSError as exc:
        # A missing parent just means there is nothing left to delete.
        logger.debug(
            "cleanup: could not write delete marker for %s (%s)",
            path,
            type(exc).__name__,
        )


def _drop_deleting(path: Path) -> None:
    """Drop the marker, but only once the tree is really gone."""
    if path.exists() or path.is_symlink():
        return
    try:
        delete_marker(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(
            "cleanup: could not drop delete marker for %s (%s)",
            path,
            type(exc).__name__,
        )


def delete_tree(path: Path) -> tuple[int, int]:
    """Delete ``path`` in one post-order walk; return ``(bytes, entries)``.

    Symlinks are unlinked, never descended into, and every directory is
    realpath-checked against the root before it is walked — ``DirEntry.is_dir()``
    follows links, so a link planted in a workspace would otherwise delete outside
    it. An unreadable entry is recorded once per distinct errno and skipped; the
    rest of the tree is still deleted, where ``shutil.rmtree`` aborts at the first
    error and silently leaks everything after it.

    No second pass and no ``statvfs``: the size is accumulated by the walk itself,
    so it is attributable to this tree even on a shared volume.
    """
    root = os.path.realpath(path)
    if not os.path.lexists(root):
        return (0, 0)
    size = 0
    entries = 0
    reported: set[int] = set()

    def record(p: str, exc: OSError) -> None:
        code = exc.errno or 0
        if code in reported:
            return
        reported.add(code)
        logger.warning(
            "cleanup: %s error=%s errno=%s (%s)",
            p,
            type(exc).__name__,
            code,
            exc.strerror,
        )

    def remove(p: str) -> None:
        nonlocal size, entries
        try:
            st = os.lstat(p)
        except OSError as exc:
            record(p, exc)
            return
        entries += 1
        if not stat.S_ISDIR(st.st_mode):
            size += st.st_size
            try:
                os.unlink(p)
            except OSError as exc:
                record(p, exc)
            return
        real = os.path.realpath(p)
        if real != root and not real.startswith(root + os.sep):
            logger.warning("cleanup: refusing to walk %s outside %s", p, root)
            return
        try:
            children = [entry.path for entry in os.scandir(p)]
        except OSError as exc:
            record(p, exc)
            return
        for child in children:
            remove(child)
        try:
            os.rmdir(p)
        except OSError as exc:
            record(p, exc)

    remove(root)
    return (size, entries)


def _run_cleanup(what: str, *steps: Callable[[], object]) -> None:
    """Run ``steps`` in the caller's order, one failure not losing the rest."""
    for step in steps:
        name = _step_name(step)
        started = time.monotonic()
        try:
            result = step()
        except Exception as exc:  # noqa: BLE001 - a delete failure must not lose later steps
            # Type and errno only: the message can carry a repo URL before
            # ``_scrub_credentials`` has run, and this log is not a place to leak it.
            logger.warning(
                "cleanup_failed what=%s step=%s error=%s errno=%s ms=%d",
                what,
                name,
                type(exc).__name__,
                getattr(exc, "errno", None),
                int((time.monotonic() - started) * 1000),
            )
            continue
        elapsed = int((time.monotonic() - started) * 1000)
        if isinstance(result, tuple) and len(result) == 2:
            nbytes, entries = result
            logger.log(
                logging.INFO if nbytes else logging.DEBUG,
                "cleanup what=%s step=%s reclaimed=%d (%s) entries=%d ms=%d",
                what,
                name,
                nbytes,
                _human(nbytes),
                entries,
                elapsed,
            )
        else:
            logger.debug("cleanup what=%s step=%s ms=%d", what, name, elapsed)


async def cleanup_off_loop(what: str, *steps: Callable[[], object]) -> None:
    """Delete workspace artefacts off the loop; order preserved, survives cancellation.

    ``asyncio.shield`` is deliberate: both analysis ``finally`` blocks also run on
    the cancellation path, where a plain ``await`` would skip the delete entirely
    and leak the workspace.

    Post-cancel ordering bites: after a cancellation the caller's *later* awaits
    are cancelled too, so anything non-filesystem in that ``finally`` (a DB call,
    say) needs its own ``asyncio.shield(asyncio.create_task(...))``.

    ``what`` is a server-generated id (job id), never a user-supplied name, so a
    crafted job name cannot forge a log line.
    """
    loop = asyncio.get_running_loop()
    await asyncio.shield(
        loop.run_in_executor(_CLEANUP_POOL, _run_cleanup, what, *steps)
    )


def _lock_for(path: Path) -> asyncio.Lock:
    key = os.path.realpath(path)
    lock = _path_locks.get(key)
    if lock is None:
        lock = _path_locks[key] = asyncio.Lock()
    return lock


async def cleanup_path_off_loop(
    what: str, path: Path, *steps: Callable[[], object]
) -> None:
    """Run delete ``steps`` for a reusable ``path`` under its lock, off the loop.

    The lock is taken **on the loop** before submitting and released from the
    future's done callback: an ``asyncio.Lock`` cannot be acquired in the reaper
    thread, and a ``threading.Lock`` taken there while the creator waits on the
    loop would reintroduce the very stall this module exists to avoid.
    """
    loop = asyncio.get_running_loop()
    lock = _lock_for(path)
    await lock.acquire()
    try:
        future = loop.run_in_executor(_CLEANUP_POOL, _run_cleanup, what, *steps)
    except BaseException:
        lock.release()
        raise
    future.add_done_callback(lambda _f: lock.release() if lock.locked() else None)
    await asyncio.shield(future)


async def delete_reusable_path(
    what: str, path: Path, *prepare: Callable[[], object]
) -> None:
    """Whole-tree delete of a reusable path: marker, ``prepare`` steps, ``delete_tree``.

    The marker outside the tree is what tells a later creator that a delete died
    mid-tree, instead of "wipe before create" which would destroy a live session.
    """
    await cleanup_path_off_loop(
        what,
        path,
        partial(_mark_deleting, path),
        *prepare,
        partial(delete_tree, path),
        partial(_drop_deleting, path),
    )


@asynccontextmanager
async def reusable_path_lock(what: str, path: Path) -> AsyncIterator[None]:
    """Hold ``path``'s lock for a create/reuse decision, re-creating after a dead delete.

    Only the marker decides re-creation: an intact workspace is reused untouched,
    because chat workspaces deliberately persist sessions and clones across
    messages.
    """
    lock = _lock_for(path)
    async with lock:
        if delete_marker(path).exists():
            logger.info("cleanup: re-creating %s after an interrupted delete", what)
            await cleanup_off_loop(
                what, partial(delete_tree, path), partial(_drop_deleting, path)
            )
        yield
