"""Process-temp directories for workspaces and extracted artifacts.

Also owns the reaper: the single worker that deletes workspaces **off** the event
loop. `/health` is served by the same loop that runs the analysis ``finally``
blocks, so a delete inline there stalls the probes (#402).
"""

from __future__ import annotations

import asyncio
import enum
import errno
import logging
import os
import stat
import tempfile
import time
import traceback
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from rootcoz import get_child_logger
from rootcoz.url_utils import mask_url_credentials_in_text

logger = get_child_logger(__name__)


class _Abandoned:
    """Type of :data:`ABANDON`, so a return type can be a union and narrow."""

    __slots__ = ()


#: A cleanup step returns this to stop the rest of this submission's steps.
#: The tree is left for the next sweep instead of being deleted under a live
#: builder; the runner still runs the ``always`` steps that must not be skipped.
ABANDON = _Abandoned()


class CleanupOutcome(enum.Enum):
    """How a queued cleanup ended: only ``CLEAN`` says the trees are gone.

    A bool would be read as "fine" the moment a caller skips it, and a skipped
    step (:data:`ABANDON`) is exactly the case where nothing may be assumed.
    """

    #: Every step ran and none raised — a reusable path is now really deleted.
    CLEAN = "clean"
    #: A step abandoned or raised, so what is left on disk is unknown.
    INCOMPLETE = "incomplete"


class PathState(enum.Enum):
    """Three-valued state of a reusable path, after :func:`reusable_path_lock`."""

    #: No delete in flight: the tree is the previous session's and can be reused.
    REUSABLE = "reusable"
    #: A dead delete was reaped: the caller must create the tree from scratch.
    NOT_REUSABLE = "not-reusable"
    #: A live builder owns the tree, so its contents cannot be established.
    UNKNOWN = "unknown"


class PathStateUnknown(RuntimeError):
    """A reusable path could not be established as whole, so it must not be reused.

    Raised instead of yielding :attr:`PathState.UNKNOWN`, so no caller can read
    "cannot tell" as "go ahead": an abandoned recovery leaves a half-deleted
    tree under a live builder, and creating into it destroys that builder's work.
    """


# Under process temp root so chat symlink cleanup (honors TMPDIR) can delete it.
ROOTCOZ_TEMP_DIRNAME = "rootcoz"

#: One worker, deliberately: deleting in order is what keeps a burst of job
#: completions from stampeding the filesystem. A delete is bounded by nothing the
#: app controls, which is exactly why it is not on the loop. Non-daemon, so the
#: interpreter waits for queued deletes instead of leaking them.
_CLEANUP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rootcoz-reaper")

#: Locks for *reusable* workspaces only (``chat-{job}/{user}`` and the admin chat
#: workspace), where a queued delete can race the next create of the same path.
#: Keyed by ``realpath``; uuid workspaces are unique per use and need nothing.
_path_locks: dict[str, _PathLock] = {}


class _PathLock:
    """One path's lock plus the number of holders and waiters that reference it.

    ``asyncio.Lock.locked()`` is false for a moment after ``release()`` while a
    queued waiter is still waiting to be resumed, so it cannot decide whether an
    entry is disposable: dropping the entry there hands the waiter and the next
    caller two different locks for one path, and a create runs into a delete.
    Reference counting says what the lock cannot — nobody is waiting any more.
    """

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


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


def _frames(exc: BaseException) -> str:
    """The failing call site as ``file:line:name <- ...``, never the message.

    Without it a failure whose errno says nothing (a bug, not a filesystem
    refusal) is undiagnosable. The frames, not ``exc_info``: an ``OSError`` from a
    clone path carries its URL — and the token in that URL's userinfo — in the
    message, and this log is not a place to leak it.
    """
    return " <-".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    )


def _mark_deleting(path: Path) -> _Abandoned | None:
    """Write the delete marker, or :data:`ABANDON` instead of deleting unmarked.

    Unmarked, a delete that dies mid-tree is indistinguishable from an intact
    workspace, so the next create reuses whatever is left of it. A marker we
    could not write therefore stops the delete: a tree that is still whole is
    safe to reuse, a half-deleted one nobody can recognise is not.
    """
    try:
        delete_marker(path).write_text("", encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "cleanup: could not write the delete marker for %s, leaving the tree "
            "whole rather than deleting it unmarked (error=%s at=%s: %s)",
            path,
            type(exc).__name__,
            _frames(exc),
            # The message names the path that was refused, and that path can be a
            # credentialed clone URL before ``_scrub_credentials`` has run.
            mask_url_credentials_in_text(str(exc)),
        )
        return ABANDON
    return None


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


def _collapse_ancestors(paths: set[str]) -> list[str]:
    """Keep only the deepest of ``paths``, dropping every ancestor above it.

    An ``rmdir`` that failed because something below it survived re-reports the
    failure its descendant already recorded, so both are one path and the deeper
    directory is the cause. Sorting makes a descendant follow every ancestor, so a
    single pass spots a collapse by comparing with the last surviving path: no
    prefix shares more than one ancestor in sorted order.
    """
    kept: list[str] = []
    for p in sorted(paths):
        if kept and p.startswith(kept[-1] + os.sep):
            kept[-1] = p
        else:
            kept.append(p)
    return kept


def delete_tree(path: Path) -> tuple[int, int]:
    """Delete ``path`` in one post-order walk; return ``(bytes, entries)``.

    Symlinks are unlinked, never descended into — a symlinked root included, so a
    planted link cannot turn a workspace delete into its target's delete. Every
    directory is opened ``O_NOFOLLOW`` and its children are unlinked **relative to
    that descriptor**, so replacing a directory with a symlink mid-walk cannot
    redirect the deletes outside the tree. An unreadable entry is counted and
    skipped; the rest of the tree is still deleted, where ``shutil.rmtree`` aborts
    at the first error and silently leaks everything after it. Only what was really
    removed is counted, so a failed entry is never reported as reclaimed.

    Failures are aggregated into **one** warning per walk, carrying every errno with
    its count of *distinct paths* and one example path — not one count per failed
    operation: every ancestor ``rmdir`` above a stuck file re-reports the same
    surviving subtree as ``ENOTEMPTY``, so counting operations turned one unlinkable
    file at depth 4 into "5 entries". That collapse is one sorted pass at report
    time (:func:`_collapse_ancestors`), not a scan of the growing set per insert:
    the walk runs on the single reaper worker, so an O(n²) summary over thousands
    of siblings would slow every cleanup queued behind it (#424). A directory that
    could not be opened hides an unknown number of entries, so it is reported as not
    descended alongside the count. One line per errno is not enough: simple_logger's ``DuplicateFilter``
    keys on ``(module, level, msg)`` — the *format string* — so a second errno logged
    through the same template was dropped and only surfaced as ``repeated N times``
    on the next record (#402).

    No second pass and no ``statvfs``: the size is accumulated by the walk itself,
    so it is attributable to this tree even on a shared volume.
    """
    top = os.fspath(path)
    if not os.path.lexists(top):
        return (0, 0)
    size = 0
    entries = 0
    # errno -> (distinct paths that failed, strerror); insertion order gives the example
    failures: dict[int, tuple[set[str], str]] = {}
    # Directories whose contents were never walked, so their entry count is unknown.
    not_descended = 0

    def record(p: str, exc: OSError) -> None:
        code = exc.errno or 0
        if code == errno.ENOENT:
            # Gone between our ``scandir`` and our ``unlink`` — a live agent
            # deleting in its own workspace, or a second sweep. Nothing is left
            # to delete, so this is not a path the reaper failed on.
            return
        # The OS reason, not ``exc.strerror``: the raiser may have replaced it.
        reason = os.strerror(code) if code else exc.strerror or type(exc).__name__
        # Recorded as it arrives; ancestors are collapsed once, at report time.
        failures.setdefault(code, (set(), reason))[0].add(p)

    def remove(name: str, parent_fd: int | None, display: str) -> None:
        """Remove ``name`` from ``parent_fd`` (``None``: the absolute root path)."""
        nonlocal size, entries, not_descended
        dfd = {} if parent_fd is None else {"dir_fd": parent_fd}
        try:
            st = os.stat(name, follow_symlinks=False, **dfd)
        except OSError as exc:
            record(display, exc)
            return
        if not stat.S_ISDIR(st.st_mode):
            try:
                os.unlink(name, **dfd)
            except OSError as exc:
                record(display, exc)
                return
            size += st.st_size
            entries += 1
            return
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                **dfd,
            )
        except OSError as exc:
            record(display, exc)
            not_descended += 1
            return
        try:
            try:
                with os.scandir(fd) as it:
                    children = [entry.name for entry in it]
            except OSError as exc:
                # Scanning failed on an opened directory: record it and let the
                # parent's loop carry on, instead of unwinding the whole walk.
                record(display, exc)
                not_descended += 1
                return
            for child in children:
                remove(child, fd, os.path.join(display, child))
        finally:
            os.close(fd)
        try:
            os.rmdir(name, **dfd)
        except OSError as exc:
            record(display, exc)
            return
        entries += 1

    remove(top, None, top)
    if failures:
        # Formatted inline, not %-args: the content must be in ``record.msg`` or a
        # template-keyed dedupe filter swallows the whole line.
        parts: list[str] = []
        total = 0
        for code, (paths, reason) in sorted(failures.items()):
            # Only ENOTEMPTY nests: an ancestor above a surviving entry repeats it.
            kept = (
                _collapse_ancestors(paths) if code == errno.ENOTEMPTY else list(paths)
            )
            total += len(kept)
            parts.append(f"errno={code} {reason}: {len(kept)} e.g. {kept[0]}")
        detail = ", ".join(parts)
        # A directory we could not open reads as one path otherwise, hiding the
        # thousands of entries behind it: name that gap separately.
        skipped = (
            f"; {not_descended} "
            f"director{'y' if not_descended == 1 else 'ies'} not descended"
            if not_descended
            else ""
        )
        logger.warning(f"cleanup: {total} paths not deleted ({detail}){skipped}")
    return (size, entries)


def reap_reusable_path(what: str, path: Path) -> tuple[int, int] | _Abandoned:
    """``delete_tree`` for a path whose earlier delete abandoned, under its lock.

    Recovery used to run the whole-tree delete with no lock at all — i.e. under the
    very builder whose flock is why the first delete abandoned. Bounded exactly
    like ``reap_graph``: on timeout :data:`ABANDON` leaves the tree *and* its
    marker for the next sweep, because the builder still owns them.
    """
    # Local imports: ``graft`` imports this module at import time.
    from rootcoz.engine.graft import GRAPH_LOCK_SECONDS
    from rootcoz.engine.http_mcp import _workspace_install_lock

    try:
        with _workspace_install_lock(
            path.resolve(), deadline=time.monotonic() + GRAPH_LOCK_SECONDS
        ):
            return delete_tree(path)
    except TimeoutError:
        logger.warning(
            "cleanup: %s still owned by a live builder after %ss; leaving the "
            "interrupted delete's tree and marker for the next sweep",
            what,
            GRAPH_LOCK_SECONDS,
        )
        return ABANDON


def _run_cleanup(
    what: str,
    *steps: Callable[[], object],
    always: tuple[Callable[[], object], ...] = (),
) -> CleanupOutcome:
    """Run ``steps`` in the caller's order, one failure not losing the rest.

    A step returning :data:`ABANDON` stops the remaining ``steps`` — the tree
    belongs to a live builder, so deleting it here would race that builder. The
    ``always`` steps still run: dropping a marker is safe only when the tree is
    really gone, and ``_drop_deleting`` checks exactly that.

    The returned :class:`CleanupOutcome` is what a caller that decides about
    reuse has to read: ``INCOMPLETE`` means the delete did not establish that the
    tree is gone, so it must never be treated as reusable.
    """
    clean = True

    def run(step: Callable[[], object]) -> object:
        nonlocal clean
        name = _step_name(step)
        started = time.monotonic()
        try:
            result = step()
        except Exception as exc:  # noqa: BLE001 - a delete failure must not lose later steps
            clean = False
            elapsed = int((time.monotonic() - started) * 1000)
            # Type, errno and the failing call site only: the message can carry
            # a repo URL before ``_scrub_credentials`` has run, and this log is
            # not a place to leak it. Formatted inline: the step name is data, and
            # a template-keyed dedupe filter would swallow the second failing step
            # of one submission.
            logger.warning(
                f"cleanup_failed what={what} step={name} "
                f"error={type(exc).__name__} errno={getattr(exc, 'errno', None)} "
                f"ms={elapsed} at={_frames(exc)}"
            )
            return None
        elapsed = int((time.monotonic() - started) * 1000)
        if result is ABANDON:
            return result
        if isinstance(result, tuple) and len(result) == 2:
            nbytes, entries = result
            # Inline for the same reason: two steps reclaiming bytes through one
            # template would log once and hide the other's figure.
            logger.log(
                logging.INFO if nbytes else logging.DEBUG,
                f"cleanup what={what} step={name} reclaimed={nbytes} "
                f"({_human(nbytes)}) entries={entries} ms={elapsed}",
            )
        else:
            logger.debug(f"cleanup what={what} step={name} ms={elapsed}")
        return result

    for step in steps:
        if run(step) is ABANDON:
            clean = False
            logger.warning(
                f"cleanup_abandoned what={what} step={_step_name(step)}: leaving "
                "the rest of this submission's steps to the next sweep"
            )
            break
    for step in always:
        run(step)
    return CleanupOutcome.CLEAN if clean else CleanupOutcome.INCOMPLETE


async def cleanup_off_loop(
    what: str,
    *steps: Callable[[], object],
    always: tuple[Callable[[], object], ...] = (),
) -> CleanupOutcome:
    """Delete workspace artefacts off the loop; order preserved, survives cancellation.

    ``always`` steps run even when a ``steps`` entry returns :data:`ABANDON`: they
    delete a *different* tree from the one a live builder owns (a downloaded
    artifact extract, say), so an abandoned graph delete must not leak them.

    ``asyncio.shield`` is deliberate: both analysis ``finally`` blocks also run on
    the cancellation path, where a plain ``await`` would skip the delete entirely
    and leak the workspace.

    Post-cancel ordering bites: after a cancellation the caller's *later* awaits
    are cancelled too, so anything non-filesystem in that ``finally`` (a DB call,
    say) needs its own ``asyncio.shield(asyncio.create_task(...))``.

    ``what`` is a server-generated id (job id), never a user-supplied name, so a
    crafted job name cannot forge a log line.

    Returns :class:`CleanupOutcome`; a caller deciding whether a reusable path may
    be used must read it (:func:`reusable_path_lock` does).
    """
    loop = asyncio.get_running_loop()
    return await asyncio.shield(
        loop.run_in_executor(
            _CLEANUP_POOL, partial(_run_cleanup, what, *steps, always=always)
        )
    )


def _lock_for(path: Path) -> tuple[str, _PathLock]:
    """Return ``(key, entry)`` for ``path``, counting the caller as a user."""
    key = os.path.realpath(path)
    entry = _path_locks.get(key)
    if entry is None:
        entry = _path_locks[key] = _PathLock()
    entry.users += 1
    return (key, entry)


def _drop_lock(key: str, entry: _PathLock) -> None:
    """Drop the caller's reference; the entry goes when no holder or waiter is left."""
    entry.users -= 1
    if entry.users == 0 and _path_locks.get(key) is entry:
        del _path_locks[key]


async def cleanup_path_off_loop(
    what: str, path: Path, *steps: Callable[[], object]
) -> CleanupOutcome:
    """Delete the reusable ``path``: marker, ``steps``, whole tree — one reaper job.

    The marker outside the tree is what tells a later creator that a delete died
    mid-tree, instead of "wipe before create" which would destroy a live session;
    it is written *here*, so every reusable-path delete is marker-aware by
    construction and no call site can queue a delete that a later create would
    happily reuse half of. It is dropped only once the tree is really gone.

    The lock is taken **on the loop** before submitting and released from the
    future's done callback: an ``asyncio.Lock`` cannot be acquired in the reaper
    thread, and a ``threading.Lock`` taken there while the creator waits on the
    loop would reintroduce the very stall this module exists to avoid.
    """
    loop = asyncio.get_running_loop()
    key, entry = _lock_for(path)
    try:
        await entry.lock.acquire()
    except BaseException:
        # Cancelled while queued behind a holder: we never held the lock, and
        # ``users`` was already incremented for us, so the entry would stay in
        # ``_path_locks`` for the process's lifetime.
        _drop_lock(key, entry)
        raise
    cleanup_steps: tuple[Callable[[], object], ...] = (
        partial(_mark_deleting, path),
        *steps,
        partial(delete_tree, path),
    )
    try:
        future = loop.run_in_executor(
            _CLEANUP_POOL,
            partial(
                _run_cleanup,
                what,
                *cleanup_steps,
                always=(partial(_drop_deleting, path),),
            ),
        )
    except BaseException:
        entry.lock.release()
        _drop_lock(key, entry)
        raise
    # Unlock before dropping the reference: a released lock with a queued waiter
    # is still in use, and the waiter's own reference is what keeps the entry.
    future.add_done_callback(lambda _f: _release_path_lock(key, entry))
    return await asyncio.shield(future)


def _release_path_lock(key: str, entry: _PathLock) -> None:
    if entry.lock.locked():
        entry.lock.release()
    _drop_lock(key, entry)


async def _reuse_state(what: str, path: Path) -> PathState:
    """Reap a dead delete of ``path``; ``UNKNOWN`` when the delete did not finish.

    Recovery is a reaper step too: :func:`reap_reusable_path` waits, bounded, for
    the builder's install lock and abandons — tree and marker untouched — when
    that builder is still running, and a marker we could not write stops the
    delete the same way. Both leave a tree whose contents nobody can vouch for.
    """
    if not delete_marker(path).exists():
        return PathState.REUSABLE
    logger.info("cleanup: re-creating %s after an interrupted delete", what)
    # Local import: ``graft`` imports this module at import time.
    from rootcoz.engine.graft import reap_graph

    outcome = await cleanup_off_loop(
        what,
        # The graph root is a *sibling* of the workspace, so a workspace-only
        # recovery would leak the graph tree the abandoned delete never reached.
        partial(reap_graph, what, path),
        partial(reap_reusable_path, what, path),
        partial(_drop_deleting, path),
    )
    return (
        PathState.UNKNOWN
        if outcome is CleanupOutcome.INCOMPLETE
        else PathState.NOT_REUSABLE
    )


@asynccontextmanager
async def reusable_path_lock(what: str, path: Path) -> AsyncIterator[PathState]:
    """Hold ``path``'s lock for a create/reuse decision, re-creating after a dead delete.

    Only the marker decides re-creation: an intact workspace is reused untouched
    (:attr:`PathState.REUSABLE`), because chat workspaces deliberately persist
    sessions and clones across messages. A marked one is deleted first, so the
    caller creates it from scratch (:attr:`PathState.NOT_REUSABLE`).

    Raises :class:`PathStateUnknown` instead of yielding when the delete could not
    establish the tree is gone — a live builder still owns it — because creating
    into a half-deleted tree under a builder is worse than refusing the request.
    """
    key, entry = _lock_for(path)
    try:
        async with entry.lock:
            state = await _reuse_state(what, path)
            if state is PathState.UNKNOWN:
                raise PathStateUnknown(
                    f"{what}: a live builder still owns {path.name}; refusing to "
                    "reuse a tree whose contents cannot be established"
                )
            yield state
    finally:
        _drop_lock(key, entry)
