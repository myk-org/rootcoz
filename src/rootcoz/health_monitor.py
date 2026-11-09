"""In-process health monitor: duration conditions with transition-only alerts (#464).

rootcoz supervises its own health on a configurable interval, in process, with no
external scheduler, no Prometheus scrape and no cluster: three conditions, each of
which must *hold* for a configured duration, and a notification only when a
condition starts or stops being true.

The clock contract is the design constraint (issues #445/#450/#451):

* :meth:`HealthMonitor.evaluate` never reads a clock. The ``now`` it works from is
  a field on the :class:`HealthSample`, stamped once at the sampler edge, so a
  transition is reproducible from data alone.
* ``monitor_status(*, now=None)`` takes the time it reports against as data.
* ``run_health_monitor(*, clock=time.monotonic)`` takes the clock it schedules with.

All three are the same domain (``time.monotonic``), stated once here so no
arithmetic crosses two clocks.

Kept out of ``monitoring.py`` on purpose: that module is exposition plus channels
and takes ``db_path`` to stay DB-free. The monitor is the opposite — a scheduler
that reads the database.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from rootcoz import get_child_logger, storage
from rootcoz.config import get_settings
from rootcoz.monitoring import (
    ERROR_RATE_MIN_REQUESTS,
    build_health_response,
    dispatch_alert,
    error_tracker,
    health_up_from_status,
    server_error_share,
)

logger = get_child_logger(__name__)

#: (key, label) — the only key→text map in the product, so the admin payload, the
#: card and the CLI name a condition the same way.
CONDITIONS: tuple[tuple[str, str], ...] = (
    ("health_down", "Health down"),
    ("error_rate", "Error rate"),
    ("stuck_analysis", "Analysis stuck"),
)

#: How often a running loop re-reads its settings, so enabling, disabling or a new
#: interval takes effect in seconds instead of after a stale sleep of up to an hour.
_SETTINGS_POLL_SECONDS = 5.0


@dataclass(frozen=True)
class HealthSample:
    """One tick's readings. Time is **data** here, never read inside the machine."""

    now: float
    health_up: int
    failing_checks: tuple[str, ...]
    server_error_rate: float
    requests_in_window: int
    running_analysis_age_seconds: int | None
    #: False means the age could not be read or parsed — which is *not* "nothing
    #: is running". Never let an unreadable source clear a real alert.
    age_known: bool = True


@dataclass(frozen=True)
class Thresholds:
    """The per-condition thresholds, and what the messages quote."""

    health_down_seconds: float
    #: Fraction (0-1): the setting is a percent and is converted here, once.
    error_rate: float
    error_rate_seconds: float
    stuck_analysis_seconds: float

    @classmethod
    def from_settings(cls, settings: Any) -> Thresholds:
        """Read the six Server Settings into the frozen shape the predicates use."""
        return cls(
            health_down_seconds=float(settings.health_alert_unhealthy_seconds),
            error_rate=settings.health_alert_error_rate_percent / 100,
            error_rate_seconds=float(settings.health_alert_error_rate_seconds),
            stuck_analysis_seconds=float(settings.health_alert_stuck_analysis_seconds),
        )


@dataclass(frozen=True)
class HealthEvent:
    """One transition: a condition started firing, or stopped."""

    key: str
    firing: bool
    #: The **total** time the condition held: ``sample.now - holding_since``. A
    #: firing event carries the hold it satisfied ("for 5m"), a recovery the whole
    #: span ("recovered after 7m").
    held_seconds: float
    #: The measured level: the failing check names, the share, the analysis age.
    detail: str
    thresholds: Thresholds

    @property
    def event_type(self) -> str:
        """The throttle/email subject key for this condition."""
        return f"health_monitor_{self.key}"

    @property
    def subject(self) -> str:
        """The email subject — a recovery must not read like the alert it clears."""
        return f"[rootcoz {'Alert' if self.firing else 'Recovery'}] {self.key}"

    def message(self) -> str:
        """The notification text, built from this event and its thresholds only."""
        held = _format_duration(self.held_seconds)
        thresholds = self.thresholds
        if self.key == "health_down":
            if self.firing:
                return (
                    f"🔴 rootcoz health has been unhealthy for {held} "
                    f"(checks: {self.detail})"
                )
            return f"✅ rootcoz health recovered after {held}"
        if self.key == "error_rate":
            threshold = _format_percent(thresholds.error_rate)
            if self.firing:
                return (
                    f"🔴 rootcoz server error rate {self.detail} above the "
                    f"{threshold} threshold for {held}"
                )
            return (
                f"✅ rootcoz server error rate back below the {threshold} "
                f"threshold after {held}"
            )
        threshold = _format_duration(thresholds.stuck_analysis_seconds)
        if self.firing:
            return f"🔴 an analysis has been running for {self.detail} (threshold {threshold})"
        return f"✅ no analysis has been running longer than the {threshold} threshold any more"


@dataclass
class _ConditionState:
    """Per-condition state: one arm timestamp, plus the flag derived from it."""

    holding_since: float | None = None
    firing: bool = False


class HealthMonitor:
    """Per-condition ``ok``/``firing`` state. Never reads a clock; takes samples.

    One transition rule for all three conditions, with a per-condition hold
    duration (see :meth:`_hold_seconds`):

    * reading unknown (``None``) while *not* firing → no event, and the arm is
      dropped: a hold is consecutive judged-true observations only.
    * reading unknown while firing → no transition, state kept, so an unreadable
      source can never falsely clear a real alert.
    * true → arm, then fall through: a ``hold == 0`` condition fires on its first
      true sample.
    * true while firing → nothing (this is the "does not repeat" guarantee).
    * false while firing → clear and emit one recovery carrying the total hold.
    * false while not firing → no event, and the arm is dropped, so a later burst
      starts a fresh hold instead of firing on the earlier one's clock.
    """

    def __init__(self) -> None:
        self.last_sample: HealthSample | None = None
        self._conditions: dict[str, _ConditionState] = {
            key: _ConditionState() for key, _label in CONDITIONS
        }

    def state(self, key: str) -> _ConditionState:
        """The state of one condition (read by :func:`monitor_status`)."""
        return self._conditions[key]

    def reset(self) -> None:
        """Clear every condition's state and the last sample (the disabled path)."""
        self._conditions = {key: _ConditionState() for key, _label in CONDITIONS}
        self.last_sample = None

    def evaluate(
        self, sample: HealthSample, thresholds: Thresholds
    ) -> list[HealthEvent]:
        """Judge the sample against every condition and return the transitions."""
        self.last_sample = sample
        events: list[HealthEvent] = []
        for key, _label in CONDITIONS:
            reading = self._reading(key, sample, thresholds)
            state = self._conditions[key]
            if reading is None:
                if not state.firing:
                    state.holding_since = None
                continue
            if reading:
                if state.holding_since is None:
                    state.holding_since = sample.now
                held = sample.now - state.holding_since
                hold = self._hold_seconds(key, thresholds)
                if not state.firing and (hold == 0 or held >= hold):
                    state.firing = True
                    events.append(self._event(key, True, sample, thresholds))
                continue
            if state.firing:
                state.firing = False
                events.append(self._event(key, False, sample, thresholds))
            state.holding_since = None
        return events

    @staticmethod
    def threshold_seconds(key: str, thresholds: Thresholds) -> float:
        """The configured threshold a condition's row shows."""
        if key == "health_down":
            return thresholds.health_down_seconds
        if key == "error_rate":
            return thresholds.error_rate_seconds
        return thresholds.stuck_analysis_seconds

    @classmethod
    def _hold_seconds(cls, key: str, thresholds: Thresholds) -> float:
        """How long a condition must hold before it fires.

        ``stuck_analysis`` holds ``0``: the analysis age *is* its duration, and a
        hold on top of it would count the threshold twice — the documented 2 h
        default would fire at ~4 h.
        """
        if key == "stuck_analysis":
            return 0.0
        return cls.threshold_seconds(key, thresholds)

    @staticmethod
    def _reading(key: str, sample: HealthSample, thresholds: Thresholds) -> bool | None:
        """``True``/``False``, or ``None`` when the reading cannot judge either way."""
        if key == "health_down":
            return sample.health_up == 0
        if key == "error_rate":
            if sample.requests_in_window < ERROR_RATE_MIN_REQUESTS:
                # Below the floor the window measures nothing either way: an idle
                # or a down server produces a handful of requests, so one lone
                # success must not clear the alert the full window raised.
                return None
            return sample.server_error_rate > thresholds.error_rate
        if not sample.age_known:
            return None
        age = sample.running_analysis_age_seconds
        return age is not None and age > thresholds.stuck_analysis_seconds

    @staticmethod
    def _detail(key: str, firing: bool, sample: HealthSample) -> str:
        """The measured level, as the log line and the message quote it."""
        if key == "health_down":
            if not firing:
                return "all checks ok"
            return ", ".join(sample.failing_checks) or "health probe failed"
        if key == "error_rate":
            return _format_percent(sample.server_error_rate)
        if not firing:
            return "no analysis over the threshold"
        age = sample.running_analysis_age_seconds or 0
        return _format_duration(age)

    def _event(
        self, key: str, firing: bool, sample: HealthSample, thresholds: Thresholds
    ) -> HealthEvent:
        state = self._conditions[key]
        holding_since = state.holding_since
        held = 0.0 if holding_since is None else sample.now - holding_since
        return HealthEvent(
            key=key,
            firing=firing,
            held_seconds=held,
            detail=self._detail(key, firing, sample),
            thresholds=thresholds,
        )


def _format_percent(fraction: float) -> str:
    """A 0-1 fraction as whole percent, the way the threshold is set."""
    return f"{round(fraction * 100)}%"


def _format_duration(seconds: float) -> str:
    """A duration in at most two units: hours+minutes, minutes+seconds, or seconds.

    No zero component is ever padded, and seconds are dropped once hours are
    present: ``30 -> "30s"``, ``90 -> "1m 30s"``, ``420 -> "7m"``, ``7200 -> "2h"``,
    ``7300 -> "2h 1m"``, ``7500 -> "2h 5m"``.
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


#: The process-wide monitor (like ``alert_throttler``). ``run_health_monitor``
#: evaluates into it and ``monitor_status`` reads it.
health_monitor = HealthMonitor()


async def collect_health_sample() -> HealthSample:
    """Take one sample. The single impure edge: every source degrades on its own.

    A helper that raised here would silence the very condition it feeds — a DB
    outage is the most likely thing ``health_down`` must announce — so each source
    produces a degraded value and logs the exception *class* only.
    """
    now = time.monotonic()
    # Synchronous on purpose: ``ErrorRateTracker.snapshot`` is a plain ``def``, and
    # passing it to ``asyncio.gather`` raises TypeError before any degradation runs.
    share, _server_errors, requests_in_window = server_error_share(
        error_tracker.snapshot()
    )
    health_result, age_row = await asyncio.gather(
        build_health_response(get_settings(), str(storage.current_db_path())),
        storage.oldest_running_analysis_started_at(),
        return_exceptions=True,
    )

    health_up = 1
    failing_checks: tuple[str, ...] = ()
    if isinstance(health_result, BaseException):
        logger.warning(
            "Health monitor: health probe raised %s; reporting unhealthy",
            type(health_result).__name__,
        )
        health_up = 0
        failing_checks = ("health_probe",)
    else:
        health_up = health_up_from_status(health_result["status"])
        failing_checks = tuple(
            name
            for name, check in health_result.get("checks", {}).items()
            if check.get("status") == "error"
        )

    age: int | None = None
    age_known = True
    if isinstance(age_row, BaseException):
        logger.warning(
            "Health monitor: analysis age read raised %s; age unknown",
            type(age_row).__name__,
        )
        age_known = False
    elif age_row is not None:
        age = storage.elapsed_seconds_since(age_row)
        # A row that exists but does not parse is unknown too: reading it as
        # "nothing running" would clear a firing stuck alert with a false recovery.
        age_known = age is not None

    return HealthSample(
        now=now,
        health_up=health_up,
        failing_checks=failing_checks,
        server_error_rate=share,
        requests_in_window=requests_in_window,
        running_analysis_age_seconds=age,
        age_known=age_known,
    )


async def run_health_monitor(
    *,
    sampler: Callable[[], Awaitable[HealthSample]] = collect_health_sample,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Sample on ``health_monitor_interval_seconds`` and notify on transitions only.

    Settings are re-read every ``min(interval, 5 s)`` so enabling, disabling or a
    new interval applies within seconds instead of after a stale sleep, and the
    sampling cadence lives in a ``next_due`` deadline measured with the injected
    ``clock`` rather than in the sleep length.

    A disabled monitor samples nothing and resets its state on every slice, so a
    toggle never announces a recovery for an alert it dropped. An observed
    disabled→enabled transition re-arms ``next_due``, so a fresh enable samples on
    its first slice instead of waiting out a deadline left from a previous interval,
    and a changed interval re-anchors it for the same reason.

    Cancelling the task is the shutdown: ``CancelledError`` is re-raised before the
    per-tick handler, and one bad sample never ends the loop.
    """
    was_enabled: bool | None = None
    last_interval: float | None = None
    next_due = clock()
    logger.debug(
        "Health monitor loop running (settings poll every %.0fs)",
        _SETTINGS_POLL_SECONDS,
    )
    while True:
        try:
            settings = get_settings()
            interval = float(settings.health_monitor_interval_seconds)
            if not settings.enable_health_monitor:
                if was_enabled is not False:
                    logger.debug("Health monitor is disabled; state cleared")
                health_monitor.reset()
                was_enabled = False
            else:
                if was_enabled is not True:
                    # Freshly enabled (or started enabled): sample on this slice.
                    next_due = clock()
                    logger.info("Health monitor enabled (interval %ds)", int(interval))
                was_enabled = True
                if interval != last_interval:
                    # A changed interval re-anchors the deadline: left from a 1 h
                    # interval, a shortened one would still wait out the hour.
                    next_due = min(next_due, clock() + interval)
                    last_interval = interval
                if clock() >= next_due:
                    sample = await sampler()
                    thresholds = Thresholds.from_settings(settings)
                    for event in health_monitor.evaluate(sample, thresholds):
                        logger.info(
                            "Health monitor: %s %s — %s",
                            event.key,
                            "firing" if event.firing else "ok",
                            event.detail,
                        )
                        # bypass_throttle: the state machine is the dedup, and a
                        # cooldown on top of it can drop a recovery.
                        await dispatch_alert(
                            event.event_type,
                            event.message(),
                            subject=event.subject,
                            bypass_throttle=True,
                        )
                    next_due = sample.now + interval
            await sleep(min(interval, _SETTINGS_POLL_SECONDS))
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad sample must not end all alerting: keep the loop, and let the
            # card's "last sample" age show that sampling stalled.
            logger.warning("Health monitor tick failed; continuing", exc_info=True)
            await sleep(_SETTINGS_POLL_SECONDS)


def monitor_status(*, now: float | None = None) -> dict[str, Any]:
    """The monitor's own state, as the admin Server Status payload carries it.

    Args:
        now: The instant to report against. ``None`` means "read the clock" — a
            test passes its own sample's instant instead.
    """
    settings = get_settings()
    at = time.monotonic() if now is None else now
    thresholds = Thresholds.from_settings(settings)
    sample = health_monitor.last_sample
    conditions: list[dict[str, Any]] = []
    for key, label in CONDITIONS:
        state = health_monitor.state(key)
        held = (
            None
            if state.holding_since is None
            else max(0, round(at - state.holding_since))
        )
        conditions.append(
            {
                "key": key,
                "label": label,
                "state": "firing" if state.firing else "ok",
                "held_seconds": held,
                "threshold_seconds": int(
                    HealthMonitor.threshold_seconds(key, thresholds)
                ),
                # The stuck condition's hold is 0, so its age is what its row shows.
                "age_seconds": (
                    sample.running_analysis_age_seconds
                    if sample is not None and key == "stuck_analysis"
                    else None
                ),
                "age_known": (
                    sample.age_known
                    if sample is not None and key == "stuck_analysis"
                    else True
                ),
            }
        )
    return {
        "enabled": bool(settings.enable_health_monitor),
        "available": True,
        "interval_seconds": int(settings.health_monitor_interval_seconds),
        "last_sample_ago_seconds": (
            None if sample is None else max(0, round(at - sample.now))
        ),
        "conditions": conditions,
    }


async def monitor_block() -> dict[str, Any]:
    """Awaitable wrapper, so ``collect_server_status`` can guard it like a probe."""
    return monitor_status()
