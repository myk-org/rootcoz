"""Admission control for analyses, capped by ``max_concurrent_analyses`` (#544).

One slot per running AI pipeline. A submission above the cap is **not** refused:
it is queued (its stored row already shows ``pending``) and admitted when a slot
frees, oldest first, so a saturated server accepts work instead of dropping it.

No lock and no storage import. Every state transition below is a plain
dict/list mutation with no ``await`` inside it, so on the single event loop the
server runs on it is atomic -- another coroutine cannot interleave in the middle
of one. A lock would only add a suspension point, and in :func:`release` a
suspension point is actively harmful: a release reached from a ``finally`` on the
cancellation path can be killed by a second ``CancelledError`` exactly where the
slot must be freed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from rootcoz import get_child_logger
from rootcoz.config import get_settings

logger = get_child_logger(__name__)

#: ``key -> how many tasks of that key hold a slot``. A counted map, not a set:
#: in-place analysis of a submitted job reuses a job id, so two tasks legitimately
#: share one key and each must release only the unit it took.
_holders: dict[str, int] = {}
#: ``(created_at, arrival_seq, key)``, ascending: oldest job first.
_waiters: list[tuple[str, int, str]] = []
_arrival_seq = 0
_released: asyncio.Event | None = None
_bound_loop: asyncio.AbstractEventLoop | None = None

#: Wake-up ceiling: ``release`` wakes waiters immediately, so this exists only so
#: a *raised* limit reaches a sleeping waiter without a settings->queue hook.
_RECHECK_SECONDS = 1.0


def _limit() -> int:
    """The live cap: a Settings change applies on the next admission attempt."""
    return get_settings().max_concurrent_analyses


def running_count() -> int:
    """How many analyses hold a slot right now."""
    return sum(_holders.values())


def waiter_count() -> int:
    """How many analyses are queued for a slot."""
    return len(_waiters)


def _event() -> asyncio.Event:
    """The wake event, rebound when the running loop changes.

    The server runs one loop for its lifetime; the rebinding is for the tests,
    where every test gets a fresh loop and an ``Event`` from a previous one raises
    when waited on.
    """
    global _released, _bound_loop
    loop = asyncio.get_running_loop()
    if _released is None or _bound_loop is not loop:
        _released = asyncio.Event()
        _bound_loop = loop
    return _released


def _enqueue(key: str, created_at: str) -> None:
    """Insert ``key`` into the queue in ``(created_at, arrival)`` order, once."""
    global _arrival_seq
    if any(waiter[2] == key for waiter in _waiters):
        return
    _arrival_seq += 1
    _waiters.append((created_at, _arrival_seq, key))
    _waiters.sort(key=lambda waiter: (waiter[0], waiter[1]))


def _dequeue(key: str) -> None:
    """Drop ``key`` from the queue, wherever it is -- no ghost head can wedge us."""
    _waiters[:] = [waiter for waiter in _waiters if waiter[2] != key]


def _try_admit(key: str, created_at: str) -> bool:
    """One synchronous admission attempt. True when a slot was taken."""
    # Only a queue head may take a free slot, so a job that arrives while another
    # is waiting cannot steal the slot that waiting job is about to get.
    if _waiters and _waiters[0][2] != key:
        _enqueue(key, created_at)
        return False
    if running_count() >= _limit():
        _enqueue(key, created_at)
        return False
    _dequeue(key)
    _holders[key] = _holders.get(key, 0) + 1
    logger.info(
        "Admitted analysis %s to a slot (%d running, limit %d)",
        key,
        running_count(),
        _limit(),
    )
    return True


async def _wait_for_wake() -> None:
    """Sleep until a release or the recheck ceiling, then leave the event clear."""
    event = _event()
    try:
        await asyncio.wait_for(event.wait(), _RECHECK_SECONDS)
    except TimeoutError:
        pass
    finally:
        # On *every* wake, not only on timeout: one shared event, so an event left
        # set makes every waiter re-loop with zero delay and burn a core.
        event.clear()


async def acquire(
    job_key: str,
    created_at: str | None = None,
    on_queued: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Take an analysis slot for ``job_key``, waiting in the queue if the cap is full.

    ``created_at`` is the job row's own timestamp, so the oldest pending analysis
    starts first even when it reaches this gate late; ``None`` means now.
    ``on_queued`` runs once, on the first wait, and is how the caller marks the
    job as queued for the user.
    """
    if created_at is None:
        # The same shape as ``results.created_at`` (SQLite ``CURRENT_TIMESTAMP``),
        # because the queue orders these as strings: an ISO ``T`` sorts after the
        # space every stored row carries, which would put a job that started
        # waiting now behind every same-day row regardless of its time.
        created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    announced = False
    warned = False
    try:
        while not _try_admit(job_key, created_at):
            if on_queued is not None and not announced:
                try:
                    await on_queued()
                    announced = True
                except Exception:
                    # A status write must never kill an analysis or the queue.
                    # ``Exception``, never ``BaseException``: a cancellation that
                    # lands here has to reach the cleanup below and be re-raised,
                    # or the aborted job would be admitted and run.
                    # Retried on the next wake, because a row left ``running``
                    # while it waits is the state the user is shown -- but warned
                    # once, so a storage outage cannot flood the log every second.
                    logger.log(
                        logging.DEBUG if warned else logging.WARNING,
                        "Could not announce the queue wait for %s",
                        job_key,
                        exc_info=not warned,
                    )
                    warned = True
            await _wait_for_wake()
    except BaseException:
        _dequeue(job_key)
        raise


def release(job_key: str) -> None:
    """Give back one slot held by ``job_key``. Synchronous on purpose.

    A ``finally`` reaches this on the cancellation path, where an ``await`` would
    be a suspension point that a second cancellation can kill -- losing the slot
    for the life of the process.
    """
    held = _holders.get(job_key, 0)
    if held <= 0:
        # The ``finally`` also runs for a task cancelled while it was queued.
        logger.debug("release for %s, which holds no analysis slot", job_key)
        return
    if held == 1:
        del _holders[job_key]
    else:
        _holders[job_key] = held - 1
    logger.info(
        "Released an analysis slot for %s (%d running, limit %d)",
        job_key,
        running_count(),
        _limit(),
    )
    if (
        _released is not None
        and _bound_loop is not None
        and not _bound_loop.is_closed()
    ):
        _released.set()


def snapshot(job_key: str) -> dict[str, Any]:
    """This job's place in the queue (``position`` is ``None`` when not queued)."""
    position = None
    for index, waiter in enumerate(_waiters):
        if waiter[2] == job_key:
            position = index + 1
            break
    return {"position": position, "running": running_count(), "limit": _limit()}


def reset() -> None:
    """Clear every piece of module state. A test seam: no caller uses it."""
    global _released, _bound_loop, _arrival_seq
    _holders.clear()
    _waiters.clear()
    _released = None
    _bound_loop = None
    _arrival_seq = 0


# shortcut: the holder map is per process; a multi-worker uvicorn would need
# count_active_analyses() as the truth, add when the CMD gains --workers.
# shortcut: a queued per-failure re-analysis sorts by the time it started waiting,
# not by its parent's created_at, because it has no row of its own to read.
