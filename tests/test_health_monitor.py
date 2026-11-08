"""Tests for the in-process health monitor (#464).

Time is **data** here: every :class:`HealthSample` carries its own ``now``, so a
transition is asserted at an exact instant with no ``sleep``, no ``freezegun``
and no monkeypatched ``time.monotonic``. The loop's cadence is driven by an
injected clock and an injected ``sleep``, and the test synchronises on an
:class:`asyncio.Event` — never on elapsed time.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from rootcoz import health_monitor as hm
from rootcoz import storage
from rootcoz.health_monitor import (
    CONDITIONS,
    HealthMonitor,
    HealthSample,
    Thresholds,
    _format_duration,
    collect_health_sample,
)

THRESHOLDS = Thresholds(
    health_down_seconds=300,
    error_rate=0.1,
    error_rate_seconds=600,
    stuck_analysis_seconds=7200,
)


def _settings(**overrides: Any) -> SimpleNamespace:
    """The six monitor settings, without loading the real Settings store."""
    values: dict[str, Any] = {
        "enable_health_monitor": False,
        "health_monitor_interval_seconds": 60,
        "health_alert_unhealthy_seconds": 300,
        "health_alert_error_rate_percent": 10,
        "health_alert_error_rate_seconds": 600,
        "health_alert_stuck_analysis_seconds": 7200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _sample(
    now: float,
    *,
    health_up: int = 1,
    failing: tuple[str, ...] = (),
    rate: float = 0.0,
    requests: int = 0,
    age: int | None = None,
    age_known: bool = True,
) -> HealthSample:
    return HealthSample(
        now=now,
        health_up=health_up,
        failing_checks=failing,
        server_error_rate=rate,
        requests_in_window=requests,
        running_analysis_age_seconds=age,
        age_known=age_known,
    )


@pytest.fixture(autouse=True)
def _fresh_monitor(monkeypatch: pytest.MonkeyPatch) -> HealthMonitor:
    """A per-test monitor: the module singleton is shared with other suites."""
    monitor = HealthMonitor()
    monkeypatch.setattr(hm, "health_monitor", monitor)
    return monitor


class _FakeTime:
    """Deterministic loop time: only the injected ``sleep`` advances it.

    A clock that advanced on every *read* would put the loop's cadence a step
    ahead of its samples, so ``now`` and the sampler only ever read ``value``.
    """

    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step
        self.slices = 0

    def now(self) -> float:
        return self.value

    async def sleep(self, _seconds: float) -> None:
        self.slices += 1
        self.value += self.step
        await asyncio.sleep(0)


async def _stop(task: asyncio.Task[None], event: asyncio.Event) -> None:
    """Await the loop's signal, cancel it, and prove it finished cancelled."""
    await asyncio.wait_for(event.wait(), 2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert task.cancelled()


# ---------------------------------------------------------------------------
# Duration conditions (tests 1-4)
# ---------------------------------------------------------------------------


def test_health_down_does_not_fire_before_the_duration(
    _fresh_monitor: HealthMonitor,
) -> None:
    """A condition must *hold* for its duration: 299 s is not 5 minutes."""
    for now in (0.0, 60.0, 120.0, 240.0, 299.0):
        events = _fresh_monitor.evaluate(
            _sample(now, health_up=0, failing=("database",)), THRESHOLDS
        )
        assert events == []


def test_health_down_fires_at_the_duration_and_only_once(
    _fresh_monitor: HealthMonitor,
) -> None:
    """The first sample whose held time reaches the duration fires; later ones do not."""
    _fresh_monitor.evaluate(
        _sample(0.0, health_up=0, failing=("database",)), THRESHOLDS
    )
    fired = _fresh_monitor.evaluate(
        _sample(300.0, health_up=0, failing=("database",)), THRESHOLDS
    )
    assert [(event.key, event.firing) for event in fired] == [("health_down", True)]
    assert "unhealthy for 5m" in fired[0].message(), (
        "the firing message states the duration it held"
    )
    for now in (301.0, 360.0, 600.0):
        assert (
            _fresh_monitor.evaluate(
                _sample(now, health_up=0, failing=("database",)), THRESHOLDS
            )
            == []
        )


def test_recovery_is_announced_once_with_its_duration(
    _fresh_monitor: HealthMonitor,
) -> None:
    """A recovery is a transition too, and it carries the total time held."""
    _fresh_monitor.evaluate(
        _sample(0.0, health_up=0, failing=("database",)), THRESHOLDS
    )
    firing = _fresh_monitor.evaluate(
        _sample(300.0, health_up=0, failing=("database",)), THRESHOLDS
    )
    assert firing[0].held_seconds == 300

    recovered = _fresh_monitor.evaluate(_sample(420.0), THRESHOLDS)
    assert [(event.key, event.firing) for event in recovered] == [
        ("health_down", False)
    ]
    assert recovered[0].held_seconds == 420
    assert "recovered after 7m" in recovered[0].message()

    assert _fresh_monitor.evaluate(_sample(480.0), THRESHOLDS) == []


def test_a_condition_that_clears_before_the_duration_never_fires(
    _fresh_monitor: HealthMonitor,
) -> None:
    """Clearing drops the arm: the next true sample starts a fresh hold.

    A monotonic-total implementation would keep the arm from t=0 and fire at
    t=300 — 90 s before this burst has held for its duration.
    """
    assert _fresh_monitor.evaluate(_sample(0.0, health_up=0), THRESHOLDS) == []
    assert _fresh_monitor.evaluate(_sample(200.0), THRESHOLDS) == []
    assert _fresh_monitor.evaluate(_sample(210.0, health_up=0), THRESHOLDS) == []
    assert _fresh_monitor.evaluate(_sample(300.0, health_up=0), THRESHOLDS) == []
    fired = _fresh_monitor.evaluate(_sample(510.0, health_up=0), THRESHOLDS)
    assert [(event.key, event.held_seconds) for event in fired] == [
        ("health_down", 300)
    ]


# ---------------------------------------------------------------------------
# The stuck-analysis condition (tests 5, 8, 9)
# ---------------------------------------------------------------------------


def test_stuck_analysis_fires_when_the_age_crosses_the_threshold_and_not_a_duration_later(
    _fresh_monitor: HealthMonitor,
) -> None:
    """The age *is* the duration: a hold on top of it would count 2 h twice."""
    assert _fresh_monitor.evaluate(_sample(0.0, age=7100), THRESHOLDS) == []
    fired = _fresh_monitor.evaluate(_sample(60.0, age=7300), THRESHOLDS)
    assert [(event.key, event.firing) for event in fired] == [("stuck_analysis", True)]
    assert fired[0].message() == (
        "🔴 an analysis has been running for 2h 1m (threshold 2h)"
    )
    assert _fresh_monitor.evaluate(_sample(120.0, age=7360), THRESHOLDS) == []

    recovered = _fresh_monitor.evaluate(_sample(180.0, age=None), THRESHOLDS)
    assert [(event.key, event.firing) for event in recovered] == [
        ("stuck_analysis", False)
    ]
    assert recovered[0].message() == (
        "✅ no analysis has been running longer than the 2h threshold any more"
    )


def test_a_waiting_or_pending_analysis_is_not_stuck(
    _fresh_monitor: HealthMonitor,
) -> None:
    """``waiting`` covers the whole Jenkins build wait: nothing running, no alert."""
    for now in (0.0, 3600.0, 7200.0, 14400.0):
        assert _fresh_monitor.evaluate(_sample(now, age=None), THRESHOLDS) == []
    fired = _fresh_monitor.evaluate(_sample(14460.0, age=7300), THRESHOLDS)
    assert [(event.key, event.firing) for event in fired] == [("stuck_analysis", True)]


def test_an_unknown_age_does_not_clear_a_firing_stuck_alert(
    _fresh_monitor: HealthMonitor,
) -> None:
    """An unreadable age is unknown, never "nothing is running"."""
    _fresh_monitor.evaluate(_sample(0.0, age=7300), THRESHOLDS)
    assert _fresh_monitor.state("stuck_analysis").firing

    assert _fresh_monitor.evaluate(_sample(60.0, age_known=False), THRESHOLDS) == []
    assert _fresh_monitor.state("stuck_analysis").firing

    recovered = _fresh_monitor.evaluate(_sample(120.0, age=None), THRESHOLDS)
    assert [(event.key, event.firing) for event in recovered] == [
        ("stuck_analysis", False)
    ]


# ---------------------------------------------------------------------------
# Pacing and thresholds (tests 6, 7, 10, 11)
# ---------------------------------------------------------------------------


def test_a_flapping_condition_cannot_pace_notifications(
    _fresh_monitor: HealthMonitor,
) -> None:
    """One firing and one recovery per 420 s cycle — never one per tick."""
    firings: list[float] = []
    recoveries: list[float] = []
    # A 300 s true phase (00:00-05:00 of the cycle) then a 60 s false phase.
    for step in range(43):
        now = float(step * 60)
        phase = now % 420
        up = 1 if phase >= 360 else 0
        for event in _fresh_monitor.evaluate(_sample(now, health_up=up), THRESHOLDS):
            (firings if event.firing else recoveries).append(now)

    assert len(firings) == 6
    assert len(recoveries) == 6
    assert firings == [float(c * 420 + 300) for c in range(6)]
    assert recoveries == [float(c * 420 + 360) for c in range(6)]


def test_error_rate_needs_the_minimum_request_volume_and_names_its_threshold(
    _fresh_monitor: HealthMonitor,
) -> None:
    """A lone 5xx is not a rate, and a draining window is not a recovery."""
    # One 5xx is a 100 % share and still not a condition.
    for now in range(0, 3601, 60):
        assert (
            _fresh_monitor.evaluate(
                _sample(float(now), rate=1.0, requests=1), THRESHOLDS
            )
            == []
        )

    monitor = HealthMonitor()
    for now in range(0, 540, 60):
        assert (
            monitor.evaluate(_sample(float(now), rate=0.14, requests=10), THRESHOLDS)
            == []
        )
    events = monitor.evaluate(_sample(600.0, rate=0.14, requests=10), THRESHOLDS)
    assert [(event.key, event.firing) for event in events] == [("error_rate", True)]
    assert events[0].message() == (
        "🔴 rootcoz server error rate 14% above the 10% threshold for 10m"
    )

    recovered = monitor.evaluate(_sample(720.0, rate=0.05, requests=10), THRESHOLDS)
    assert [(event.key, event.firing) for event in recovered] == [("error_rate", False)]
    assert recovered[0].message() == (
        "✅ rootcoz server error rate back below the 10% threshold after 12m"
    )

    # The window edge: 9 requests at 100 % cannot judge, an empty window cannot
    # either, and 9 requests at 0 % is the one reading that clears.
    monitor = HealthMonitor()
    for now in range(0, 601, 60):
        monitor.evaluate(_sample(float(now), rate=0.14, requests=10), THRESHOLDS)
    assert monitor.state("error_rate").firing
    assert monitor.evaluate(_sample(660.0, rate=1.0, requests=9), THRESHOLDS) == []
    assert monitor.state("error_rate").firing
    assert monitor.evaluate(_sample(720.0, rate=0.0, requests=0), THRESHOLDS) == []
    assert monitor.state("error_rate").firing
    cleared = monitor.evaluate(_sample(780.0, rate=0.0, requests=9), THRESHOLDS)
    assert [(event.key, event.firing) for event in cleared] == [("error_rate", False)]

    # An unjudgeable stretch neither starts, accrues nor clears a hold.
    monitor = HealthMonitor()
    assert monitor.evaluate(_sample(0.0, rate=0.14, requests=10), THRESHOLDS) == []
    for now in range(60, 601, 60):
        assert (
            monitor.evaluate(_sample(float(now), rate=0.0, requests=0), THRESHOLDS)
            == []
        )
    assert monitor.evaluate(_sample(660.0, rate=0.14, requests=10), THRESHOLDS) == []
    fired = monitor.evaluate(_sample(1260.0, rate=0.14, requests=10), THRESHOLDS)
    assert [(event.key, event.held_seconds) for event in fired] == [("error_rate", 600)]


def test_thresholds_from_settings_converts_percent_to_fraction() -> None:
    """Percent is a setting, a fraction is the predicate: converted once, here."""
    thresholds = Thresholds.from_settings(_settings(health_alert_error_rate_percent=10))
    assert thresholds.error_rate == pytest.approx(0.1)
    assert thresholds.health_down_seconds == float(300)
    assert thresholds.error_rate_seconds == float(600)
    assert thresholds.stuck_analysis_seconds == float(7200)


def test_health_message_never_claims_degraded_is_down(
    _fresh_monitor: HealthMonitor,
) -> None:
    """``degraded`` is up: it never fires the health condition."""
    for now in (0.0, 600.0, 3600.0):
        assert (
            _fresh_monitor.evaluate(
                _sample(now, health_up=1, failing=("jenkins",)), THRESHOLDS
            )
            == []
        )


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (30.0, "30s"),
        (90.0, "1m 30s"),
        (420.0, "7m"),
        (7200.0, "2h"),
        (7300.0, "2h 1m"),
        (7500.0, "2h 5m"),
    ],
)
def test_duration_formatting_is_at_most_two_units(
    seconds: float, expected: str
) -> None:
    """The documented outputs the messages are asserted against."""
    assert _format_duration(seconds) == expected


# ---------------------------------------------------------------------------
# The loop (tests 12-15)
# ---------------------------------------------------------------------------


async def test_a_disabled_monitor_neither_samples_nor_dispatches_and_resets_state(
    monkeypatch: pytest.MonkeyPatch, _fresh_monitor: HealthMonitor
) -> None:
    """Opt-in is the gate: a disabled monitor probes nothing and alerts on nothing."""
    _fresh_monitor.evaluate(
        _sample(0.0, health_up=0, failing=("database",)), THRESHOLDS
    )
    fired = _fresh_monitor.evaluate(
        _sample(300.0, health_up=0, failing=("database",)), THRESHOLDS
    )
    assert [event.key for event in fired] == ["health_down"]

    settings = _settings(
        enable_health_monitor=False, health_monitor_interval_seconds=3600
    )
    monkeypatch.setattr(hm, "get_settings", lambda: settings)
    dispatched = AsyncMock()
    monkeypatch.setattr(hm, "dispatch_alert", dispatched)

    calls: list[float] = []
    fake = _FakeTime(5.0)

    async def _sampler() -> HealthSample:
        calls.append(fake.now())
        return _sample(fake.now(), health_up=0, failing=("database",))

    observed: dict[str, Any] = {}
    enabled = asyncio.Event()
    original_sleep = fake.sleep

    async def _sleep(seconds: float) -> None:
        await original_sleep(seconds)
        if fake.slices == 3:
            # Still disabled: record what the operator would see, then flip it.
            observed["status"] = hm.monitor_status(now=fake.now())
            observed["calls_while_disabled"] = len(calls)
            observed["dispatched_while_disabled"] = dispatched.await_count
            settings.enable_health_monitor = True
        if fake.slices == 6:
            enabled.set()

    task = asyncio.create_task(
        hm.run_health_monitor(sampler=_sampler, sleep=_sleep, clock=fake.now)
    )
    await _stop(task, enabled)

    assert observed["calls_while_disabled"] == 0
    assert observed["dispatched_while_disabled"] == 0
    status = observed["status"]
    assert status["enabled"] is False
    assert [(c["state"], c["held_seconds"]) for c in status["conditions"]] == [
        ("ok", None),
        ("ok", None),
        ("ok", None),
    ], "a disabled monitor drops the state it held, and announces no recovery for it"

    # Re-enabling inside one interval samples exactly once (next_due is reset on
    # the transition, so a 3600 s interval cannot leave it waiting an hour) and
    # a 300 s hold does not fire on that first sample.
    assert len(calls) == 1
    assert dispatched.await_count == 0


async def test_a_raising_sample_does_not_kill_the_loop(
    monkeypatch: pytest.MonkeyPatch, _fresh_monitor: HealthMonitor
) -> None:
    """One bad sample must not end all alerting and freeze the card."""
    settings = _settings(enable_health_monitor=True)
    monkeypatch.setattr(hm, "get_settings", lambda: settings)
    dispatched = AsyncMock()
    monkeypatch.setattr(hm, "dispatch_alert", dispatched)

    fake = _FakeTime(60.0)
    calls: list[float] = []

    async def _sampler() -> HealthSample:
        calls.append(fake.now())
        if len(calls) == 1:
            raise aiosqlite.Error("database went away")
        return _sample(fake.now(), health_up=0, failing=("database",))

    fired = asyncio.Event()

    async def _sleep(seconds: float) -> None:
        await fake.sleep(seconds)
        if dispatched.await_count:
            fired.set()

    task = asyncio.create_task(
        hm.run_health_monitor(sampler=_sampler, sleep=_sleep, clock=fake.now)
    )
    await _stop(task, fired)

    assert calls[0] == 0.0
    assert len(calls) > 1, "the loop kept running after the failing tick"
    assert _fresh_monitor.last_sample is not None
    assert dispatched.await_count == 1
    assert dispatched.await_args.kwargs == {"bypass_throttle": True}

    # Second case: the settings read itself raises for one slice.
    _fresh_monitor.reset()
    dispatched.reset_mock()
    fake = _FakeTime(60.0)
    calls.clear()
    reads = {"count": 0}

    def _get_settings() -> SimpleNamespace:
        reads["count"] += 1
        if reads["count"] == 1:
            raise RuntimeError("settings store is locked")
        return settings

    monkeypatch.setattr(hm, "get_settings", _get_settings)

    async def _sampler2() -> HealthSample:
        calls.append(fake.now())
        return _sample(fake.now(), health_up=0, failing=("database",))

    fired2 = asyncio.Event()

    async def _sleep2(seconds: float) -> None:
        await fake.sleep(seconds)
        if dispatched.await_count:
            fired2.set()

    task = asyncio.create_task(
        hm.run_health_monitor(sampler=_sampler2, sleep=_sleep2, clock=fake.now)
    )
    await _stop(task, fired2)

    assert reads["count"] > 1
    assert _fresh_monitor.last_sample is not None
    assert dispatched.await_count == 1


async def test_run_loop_dispatches_transitions_and_stops_on_cancel(
    monkeypatch: pytest.MonkeyPatch, _fresh_monitor: HealthMonitor
) -> None:
    """Samples land on 0, 60, …, 300; the transition at 300 dispatches once."""
    settings = _settings(enable_health_monitor=True, health_monitor_interval_seconds=60)
    monkeypatch.setattr(hm, "get_settings", lambda: settings)
    dispatched = AsyncMock()
    monkeypatch.setattr(hm, "dispatch_alert", dispatched)

    fake = _FakeTime(60.0)
    reached = asyncio.Event()
    sampled: list[float] = []

    async def _sampler() -> HealthSample:
        sampled.append(fake.now())
        return _sample(fake.now(), health_up=0, failing=("database",))

    async def _sleep(seconds: float) -> None:
        await fake.sleep(seconds)
        # Signalled from the tick that *followed* t=300, so its dispatch landed.
        if sampled and sampled[-1] == 300.0:
            reached.set()

    task = asyncio.create_task(
        hm.run_health_monitor(sampler=_sampler, sleep=_sleep, clock=fake.now)
    )
    await _stop(task, reached)

    # The cancel lands after the next suspension point, so the loop may take one
    # more tick; the ticks that matter are these six.
    assert sampled[:6] == [0.0, 60.0, 120.0, 180.0, 240.0, 300.0]
    assert dispatched.await_count == 1
    assert dispatched.await_args.args[0] == "health_monitor_health_down"
    assert dispatched.await_args.kwargs == {"bypass_throttle": True}
    assert "unhealthy" in dispatched.await_args.args[1]

    after = dispatched.await_count
    await asyncio.sleep(0)
    assert dispatched.await_count == after, "a cancelled loop dispatches nothing more"


async def test_the_sampling_cadence_is_the_interval_not_the_slice(
    monkeypatch: pytest.MonkeyPatch, _fresh_monitor: HealthMonitor
) -> None:
    """37 five-second slices at a 60 s interval sample exactly 4 times."""
    settings = _settings(enable_health_monitor=True, health_monitor_interval_seconds=60)
    monkeypatch.setattr(hm, "get_settings", lambda: settings)
    monkeypatch.setattr(hm, "dispatch_alert", AsyncMock())

    fake = _FakeTime(5.0)
    calls: list[float] = []
    done = asyncio.Event()

    async def _sampler() -> HealthSample:
        calls.append(fake.now())
        return _sample(fake.now())

    async def _sleep(seconds: float) -> None:
        await fake.sleep(seconds)
        if fake.slices >= 37:
            done.set()

    task = asyncio.create_task(
        hm.run_health_monitor(sampler=_sampler, sleep=_sleep, clock=fake.now)
    )
    await _stop(task, done)

    assert fake.slices >= 37
    assert calls == [0.0, 60.0, 120.0, 180.0], "t=180 was reached, and sampled once"


# ---------------------------------------------------------------------------
# What the admin surface reads (tests 16-18)
# ---------------------------------------------------------------------------


def test_monitor_status_reports_holding_progress(_fresh_monitor: HealthMonitor) -> None:
    """An operator sees "held 120 s of 300 s" before anything fires."""
    _fresh_monitor.evaluate(
        _sample(0.0, health_up=0, failing=("database",)), THRESHOLDS
    )
    row = _row(hm.monitor_status(now=120.0), "health_down")
    assert row["state"] == "ok"
    assert row["held_seconds"] == 120
    assert row["threshold_seconds"] == 300


def test_monitor_status_reports_the_age_for_a_firing_stuck_condition(
    _fresh_monitor: HealthMonitor,
) -> None:
    """The stuck row shows its age, never "held 0s of 7200s"."""
    _fresh_monitor.evaluate(_sample(0.0, age=7500), THRESHOLDS)
    row = _row(hm.monitor_status(now=10.0), "stuck_analysis")
    assert row["state"] == "firing"
    assert row["age_seconds"] == 7500
    assert row["age_known"] is True
    assert row["threshold_seconds"] == 7200


def test_every_condition_row_carries_a_human_label() -> None:
    """One key→label list, so no consumer invents its own map."""
    status = hm.monitor_status(now=0.0)
    assert set(status) == {
        "enabled",
        "available",
        "interval_seconds",
        "last_sample_ago_seconds",
        "conditions",
    }
    labels = {row["key"]: row["label"] for row in status["conditions"]}
    assert labels == {
        "health_down": "Health down",
        "error_rate": "Error rate",
        "stuck_analysis": "Analysis stuck",
    }
    assert [key for key, _label in CONDITIONS] == [
        "health_down",
        "error_rate",
        "stuck_analysis",
    ]
    for row in status["conditions"]:
        assert row["label"] and row["label"] != row["key"]


def _row(status: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in status["conditions"] if row["key"] == key)


# ---------------------------------------------------------------------------
# The sampler (tests 19-21)
# ---------------------------------------------------------------------------


async def test_collect_sample_degrades_each_source_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB outage is exactly what ``health_down`` must announce."""

    async def _boom(_settings: Any, _db_path: str) -> dict[str, Any]:
        raise RuntimeError("health exploded")

    async def _age() -> str | None:
        raise aiosqlite.Error("database went away")

    monkeypatch.setattr(hm, "build_health_response", _boom)
    monkeypatch.setattr(storage, "oldest_running_analysis_started_at", _age)
    sample = await collect_health_sample()
    assert sample.health_up == 0
    assert sample.failing_checks == ("health_probe",)
    assert sample.age_known is False
    assert sample.running_analysis_age_seconds is None

    # A healthy probe with an unreadable age: the age is unknown, never "idle".
    async def _unhealthy(_settings: Any, _db_path: str) -> dict[str, Any]:
        return {
            "status": "unhealthy",
            "checks": {"database": {"status": "error"}, "jenkins": {"status": "ok"}},
        }

    monkeypatch.setattr(hm, "build_health_response", _unhealthy)
    sample = await collect_health_sample()
    assert sample.health_up == 0
    assert sample.age_known is False

    # A row that exists but does not parse is unknown too: reading it as
    # "nothing running" would clear a firing stuck alert with a false recovery.
    async def _garbage() -> str:
        return "not-a-timestamp"

    monkeypatch.setattr(storage, "oldest_running_analysis_started_at", _garbage)
    monkeypatch.setattr(hm, "build_health_response", _healthy)
    sample = await collect_health_sample()
    assert sample.health_up == 1
    assert sample.age_known is False
    assert sample.running_analysis_age_seconds is None


async def _healthy(_settings: Any, _db_path: str) -> dict[str, Any]:
    return {"status": "healthy", "checks": {"database": {"status": "ok"}}}


async def test_collect_sample_uses_the_shared_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sample carries the health status, the failing check names and the age."""
    three_hours_ago = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10900)
    ).strftime("%Y-%m-%d %H:%M:%S")

    async def _unhealthy(_settings: Any, _db_path: str) -> dict[str, Any]:
        return {
            "status": "unhealthy",
            "checks": {
                "database": {"status": "error", "detail": "locked"},
                "jenkins": {"status": "ok"},
            },
            "version": "9.9.9",
        }

    async def _age() -> str:
        return three_hours_ago

    monkeypatch.setattr(hm, "build_health_response", _unhealthy)
    monkeypatch.setattr(storage, "oldest_running_analysis_started_at", _age)
    sample = await collect_health_sample()

    assert sample.health_up == 0
    assert sample.failing_checks == ("database",)
    assert sample.running_analysis_age_seconds is not None
    assert sample.running_analysis_age_seconds == pytest.approx(10900, abs=5)
    assert sample.age_known is True
    # The health payload carries a version; the sample must not.
    assert "9.9.9" not in repr(sample)
    assert "version" not in repr(sample)


def test_alert_messages_never_contain_a_version_or_a_url(
    _fresh_monitor: HealthMonitor,
) -> None:
    """No URL and no version shape in any message (the two disclosure reviews)."""
    monitor = HealthMonitor()
    runs = [
        # Each run ends on the sample that completes the condition's hold.
        [
            _sample(0.0, health_up=0, failing=("database", "ai_provider")),
            _sample(300.0, health_up=0, failing=("database", "ai_provider")),
        ],
        [
            _sample(0.0, rate=0.14, requests=10),
            _sample(600.0, rate=0.14, requests=10),
        ],
        [_sample(0.0, age=7500)],
    ]
    messages: list[str] = []
    for samples in runs:
        events: list[Any] = []
        for sample in samples:
            events = monitor.evaluate(sample, THRESHOLDS)
        assert [event.firing for event in events] == [True], "each condition fires"
        messages.append(events[0].message())

    assert len(messages) == 3
    for message in messages:
        assert "http" not in message.lower()
        assert not re.search(r"\d+\.\d+", message)
    assert "(checks: database, ai_provider)" in messages[0]
