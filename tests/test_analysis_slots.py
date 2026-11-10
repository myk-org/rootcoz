"""Unit tests for the analysis slot queue (#544). No server, no storage."""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from rootcoz import analysis_slots


@pytest.fixture(autouse=True)
def fast_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the wake-up ceiling: the value is a latency knob, not the logic."""
    monkeypatch.setattr(analysis_slots, "_RECHECK_SECONDS", 0.02)


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


async def _drain(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class _AdmissionLog:
    """``acquire`` calls that record the order in which they were admitted."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.tasks: list[asyncio.Task] = []

    def spawn(self, key: str, created_at: str | None = None) -> asyncio.Task:
        async def _take() -> None:
            await analysis_slots.acquire(key, created_at=created_at)
            self.order.append(key)

        task = asyncio.create_task(_take())
        self.tasks.append(task)
        return task


async def test_cap_admits_exactly_the_limit(slot_limit) -> None:
    """T1: the cap holds; the submission beyond it waits instead of running."""
    slot_limit(2)
    await analysis_slots.acquire("a")
    await analysis_slots.acquire("b")
    third = asyncio.create_task(analysis_slots.acquire("c"))
    await _settle()

    assert analysis_slots.running_count() == 2
    assert analysis_slots.waiter_count() == 1
    assert analysis_slots.snapshot("c")["position"] == 1

    analysis_slots.release("a")
    await _settle()
    assert analysis_slots.running_count() == 2
    assert analysis_slots.waiter_count() == 0
    await _drain(third)


async def test_queue_is_ordered_by_created_at(slot_limit) -> None:
    """T2: oldest ``created_at`` first, and a recheck never re-queues."""
    slot_limit(1)
    log = _AdmissionLog()
    await analysis_slots.acquire("a")
    log.order.append("a")

    log.spawn("b", created_at="2026-01-01 10:00:00")
    await _settle()
    log.spawn("c", created_at="2026-01-01 10:05:00")
    await _settle()

    assert analysis_slots.snapshot("b")["position"] == 1
    assert analysis_slots.snapshot("c")["position"] == 2
    # Two recheck windows later nothing has grown: a repeat insert would.
    await _settle(0.1)
    assert analysis_slots.waiter_count() == 2
    assert analysis_slots.snapshot("b")["position"] == 1

    analysis_slots.release("a")
    await _settle()
    assert log.order == ["a", "b"]
    analysis_slots.release("b")
    await _settle()
    assert log.order == ["a", "b", "c"]
    analysis_slots.release("c")
    await _drain(*log.tasks)


async def test_same_second_arrivals_keep_arrival_order(slot_limit) -> None:
    """T2 tie case: one-second ``created_at`` resolution must not reorder a burst."""
    slot_limit(1)
    log = _AdmissionLog()
    await analysis_slots.acquire("a")

    log.spawn("first", created_at="2026-01-01 10:00:00")
    await _settle()
    log.spawn("second", created_at="2026-01-01 10:00:00")
    await _settle()
    log.spawn("third", created_at="2026-01-01 10:00:00")
    await _settle()

    analysis_slots.release("a")
    log.order.append("a")
    for key in ("first", "second", "third"):
        await _settle()
        analysis_slots.release(key)
    await _settle()

    assert log.order == ["a", "first", "second", "third"]
    await _drain(*log.tasks)


async def test_a_job_with_no_row_sorts_against_stored_created_at(
    slot_limit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2c: the fallback stamp must be the stored shape, not ISO.

    A per-failure re-analysis has no row of its own, so ``acquire`` stamps it now.
    As ISO it carries a ``T``, which sorts after the space every stored
    ``created_at`` carries -- so a *later* same-day job would overtake it.
    """

    class _Now(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 1, 1, 10, 0, 0, tzinfo=tz)

    monkeypatch.setattr(analysis_slots, "datetime", _Now)
    slot_limit(1)
    log = _AdmissionLog()
    await analysis_slots.acquire("holder")

    log.spawn("reanalysis")  # no created_at: the "now" fallback
    await _settle()
    log.spawn("stored", created_at="2026-01-01 12:00:00")
    await _settle()

    assert analysis_slots.snapshot("reanalysis")["position"] == 1
    assert analysis_slots.snapshot("stored")["position"] == 2
    await _drain(*log.tasks)


async def test_an_older_job_queues_ahead_of_a_younger_one(slot_limit) -> None:
    """T2b: the issue's literal rule — the *oldest* analysis starts first."""
    slot_limit(1)
    log = _AdmissionLog()
    await analysis_slots.acquire("a")

    log.spawn("young", created_at="2026-01-01 12:00:00")
    await _settle()
    log.spawn("old", created_at="2026-01-01 09:00:00")
    await _settle()

    assert analysis_slots.snapshot("old")["position"] == 1
    analysis_slots.release("a")
    await _settle()
    assert log.order == ["old"]
    await _drain(*log.tasks)


async def test_a_later_arrival_cannot_steal_a_free_slot(
    slot_limit, monkeypatch
) -> None:
    """T3: released capacity goes to the waiting job, never to a new arrival."""
    # A real ceiling, so b is still asleep when c arrives and cannot win by waking.
    monkeypatch.setattr(analysis_slots, "_RECHECK_SECONDS", 5.0)
    slot_limit(1)
    log = _AdmissionLog()
    await analysis_slots.acquire("a")
    log.spawn("b")
    await _settle()

    slot_limit(2)  # a slot frees while b sleeps
    log.spawn("c")
    await _settle()
    assert log.order == []
    assert analysis_slots.running_count() == 1
    assert analysis_slots.waiter_count() == 2

    analysis_slots.release("a")
    log.order = ["a", *log.order]
    await _settle()
    assert log.order == ["a", "b", "c"]
    await _drain(*log.tasks)


async def test_on_queued_runs_once_and_its_failure_is_not_fatal(slot_limit) -> None:
    """T4: announce once; a raising status write neither kills nor wedges the wait."""
    slot_limit(1)
    calls = {"ok": 0, "broken": 0}

    async def _ok() -> None:
        calls["ok"] += 1

    async def _broken() -> None:
        calls["broken"] += 1
        raise RuntimeError("storage is unhappy")

    await analysis_slots.acquire("a")
    admitted = []
    ok_task = asyncio.create_task(
        _announce(analysis_slots.acquire("b", on_queued=_ok), admitted, "b")
    )
    broken_task = asyncio.create_task(
        _announce(analysis_slots.acquire("c", on_queued=_broken), admitted, "c")
    )
    await _settle(0.15)

    assert calls["ok"] == 1
    assert calls["broken"] == 1
    assert analysis_slots.waiter_count() == 2

    analysis_slots.release("a")
    await _settle()
    assert admitted == ["b"]
    analysis_slots.release("b")
    await _settle()
    assert admitted == ["b", "c"]
    await _drain(ok_task, broken_task)


async def _announce(coro, admitted: list[str], key: str) -> None:
    await coro
    admitted.append(key)


async def test_cancellation_during_on_queued_propagates(slot_limit) -> None:
    """T4b: a cancel while announcing must not be swallowed and resurrect the job."""
    slot_limit(1)
    entered = asyncio.Event()

    async def _block() -> None:
        entered.set()
        await asyncio.Event().wait()

    await analysis_slots.acquire("a")
    task = asyncio.create_task(analysis_slots.acquire("b", on_queued=_block))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert analysis_slots.waiter_count() == 0
    assert analysis_slots.running_count() == 1


async def test_a_waiter_does_not_spin(slot_limit, monkeypatch) -> None:
    """T4c: the shared wake event is cleared on every wake, so nobody busy-loops."""
    slot_limit(1)
    real_event = analysis_slots._event
    inner = real_event()

    class _CountingEvent:
        waits = 0

        async def wait(self) -> None:
            _CountingEvent.waits += 1
            await inner.wait()

        def set(self) -> None:
            inner.set()

        def clear(self) -> None:
            inner.clear()

    counting = _CountingEvent()
    monkeypatch.setattr(analysis_slots, "_event", lambda: counting)

    await analysis_slots.acquire("a")
    first = asyncio.create_task(analysis_slots.acquire("b"))
    second = asyncio.create_task(analysis_slots.acquire("c"))
    await _settle()
    analysis_slots.release("a")
    await _settle(0.2)

    # One waiter was admitted; the other rechecks at the ceiling, ~10 times in
    # 0.2 s. An event left set would make it thousands.
    assert _CountingEvent.waits <= 40
    await _drain(first, second)


async def test_a_raised_limit_reaches_a_sleeping_waiter(slot_limit) -> None:
    """T5: raising the cap admits sleepers with no release and no settings hook."""
    slot_limit(1)
    log = _AdmissionLog()
    await analysis_slots.acquire("a")
    log.spawn("b")
    await _settle()
    log.spawn("c")
    await _settle()
    assert analysis_slots.waiter_count() == 2

    slot_limit(3)
    await _settle(0.1)
    assert analysis_slots.waiter_count() == 0
    assert analysis_slots.running_count() == 3
    assert log.order == ["b", "c"]
    await _drain(*log.tasks)


async def test_cancelling_a_waiter_leaves_no_ghost_head(slot_limit) -> None:
    """T6: a cancelled waiter is dropped, so the queue head stays admissible."""
    slot_limit(1)
    log = _AdmissionLog()
    await analysis_slots.acquire("a")
    doomed = asyncio.create_task(analysis_slots.acquire("b"))
    await _settle()
    doomed.cancel()
    await asyncio.gather(doomed, return_exceptions=True)

    assert analysis_slots.waiter_count() == 0
    assert analysis_slots.running_count() == 1

    log.spawn("c")
    await _settle()
    analysis_slots.release("a")
    await _settle()
    assert log.order == ["c"]
    await _drain(*log.tasks)


async def test_release_of_an_unheld_key_frees_nothing(slot_limit) -> None:
    """T7: a release for a key that never held a slot must not over-admit."""
    slot_limit(1)
    await analysis_slots.acquire("a")
    queued = asyncio.create_task(analysis_slots.acquire("b"))
    await _settle()
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)

    analysis_slots.release("b")
    assert analysis_slots.running_count() == 1

    newcomer = asyncio.create_task(analysis_slots.acquire("c"))
    await _settle()
    assert analysis_slots.running_count() == 1
    assert analysis_slots.snapshot("c")["position"] == 1

    analysis_slots.release("a")
    await _settle()
    assert analysis_slots.running_count() == 1
    assert analysis_slots.snapshot("c")["position"] is None
    await _drain(newcomer)


async def test_two_holders_on_one_key_balance_each_other(slot_limit) -> None:
    """T8: a counted map, not a set — two tasks on one key hold two slot units."""
    slot_limit(2)
    await analysis_slots.acquire("k")
    await analysis_slots.acquire("k")
    assert analysis_slots.running_count() == 2

    slot_limit(1)
    analysis_slots.release("k")
    assert analysis_slots.running_count() == 1  # still at the limit

    newcomer = asyncio.create_task(analysis_slots.acquire("other"))
    await _settle()
    assert analysis_slots.snapshot("other")["position"] == 1

    analysis_slots.release("k")
    assert analysis_slots.running_count() == 0
    await _settle()
    assert analysis_slots.snapshot("other")["position"] is None
    await _drain(newcomer)


def test_a_second_event_loop_can_take_and_release(slot_limit) -> None:
    """T9: the wake event is rebound per loop, so a new loop never raises."""
    slot_limit(1)

    async def _cycle() -> None:
        await analysis_slots.acquire("holder")
        waiter = asyncio.create_task(analysis_slots.acquire("waiter"))
        await _settle()
        analysis_slots.release("holder")
        await waiter
        analysis_slots.release("waiter")

    asyncio.run(_cycle())
    asyncio.run(_cycle())
    assert analysis_slots.running_count() == 0
