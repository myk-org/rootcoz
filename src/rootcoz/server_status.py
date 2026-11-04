"""Admin server-status probes.

One read-only snapshot of the running container for the admin Server Status
page: disk, CPU, memory, process, database, sidecar and the warnings derived
from those numbers. Every probe degrades on its own — a missing cgroup file or
an unreachable sidecar becomes ``{"available": false, "reason": ...}`` in that
block, never a failed request — and everything filesystem/``/proc`` heavy runs
in a thread so the event loop keeps serving.

Nothing here is PVC-specific: the disk block measures whichever filesystem
holds the data directory (a PVC, a Docker volume, a bind mount or a plain
directory), and the note says so instead of naming one deployment's backend.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
from collections.abc import Coroutine
from typing import Any

import aiosqlite

from rootcoz import get_child_logger, storage
from rootcoz.monitoring import _check_http_service, _get_app_version, read_data_usage

logger = get_child_logger(__name__)

CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
PROC_SELF = pathlib.Path("/proc/self")

# cpu.max is "<quota> <period>" in microseconds, or "max <period>" when the
# container has no CPU limit.
_CPU_MAX_UNLIMITED = "max"

# Window for the two /proc/self/stat samples process_percent is derived from.
_CPU_SAMPLE_SECONDS = 0.1

# Thresholds the warnings are derived from, in percent of the total.
_DISK_WARN_PERCENT = 75.0
_DISK_CRITICAL_PERCENT = 90.0
_MEMORY_CRITICAL_PERCENT = 90.0
# Event-loop stalls above this mean requests are queuing behind slow work.
_LOOP_LAG_WARN_MS = 5000.0

_FILESYSTEM_NOTE = (
    "Measured on the filesystem that holds the data directory, whatever "
    "storage backs it."
)

# ---------------------------------------------------------------------------
# Event-loop lag watchdog
# ---------------------------------------------------------------------------

_LOOP_LAG_INTERVAL_SECONDS = 1.0
# Last measured stall in milliseconds; None until the watchdog took a sample.
_loop_lag_ms: float | None = None
# Pending timer handle, so the watchdog can be stopped on shutdown.
_loop_lag_handle: asyncio.Handle | None = None


def start_loop_lag_watchdog() -> None:
    """Start sampling event-loop lag once a second.

    A ``call_later`` timer rather than a sleeping task on purpose: the lag is
    how much later than scheduled the callback actually ran, which is exactly
    what a blocked loop produces, and a timer is unaffected by code that
    patches ``asyncio.sleep``.
    """
    loop = asyncio.get_running_loop()
    _schedule_loop_lag_sample(loop, loop.time() + _LOOP_LAG_INTERVAL_SECONDS)


def stop_loop_lag_watchdog() -> None:
    """Cancel the pending sample; the last measurement stays readable."""
    global _loop_lag_handle
    if _loop_lag_handle is not None:
        _loop_lag_handle.cancel()
        _loop_lag_handle = None


def _schedule_loop_lag_sample(loop: asyncio.AbstractEventLoop, expected: float) -> None:
    """Arm the next sample and record how late the previous one was."""
    global _loop_lag_handle

    def _tick() -> None:
        global _loop_lag_ms
        now = loop.time()
        _loop_lag_ms = round(max(0.0, now - expected) * 1000, 1)
        _schedule_loop_lag_sample(loop, now + _LOOP_LAG_INTERVAL_SECONDS)

    _loop_lag_handle = loop.call_later(_LOOP_LAG_INTERVAL_SECONDS, _tick)


def loop_lag_ms() -> float | None:
    """Return the last sampled event-loop lag in milliseconds."""
    return _loop_lag_ms


def reset_loop_lag() -> None:
    """Forget the last sample (tests and restarts)."""
    global _loop_lag_ms
    _loop_lag_ms = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _read_text(path: pathlib.Path) -> str | None:
    """Read a small file as stripped text; None when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _unavailable(reason: str) -> dict[str, Any]:
    """Standard block for a probe that could not read what it needs."""
    return {"available": False, "reason": reason}


def _tree_size(path: pathlib.Path) -> int | None:
    """Total size in bytes of the files under ``path``; None when it is absent."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return None
    if not path.is_dir():
        return None
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------


def _parse_cpu_max(text: str | None) -> tuple[float | None, int | None]:
    """Parse ``cpu.max`` into (quota_cores, period_us); None means unreadable.

    ``"200000 100000"`` is two cores over a 100 ms period; ``"max 100000"`` is
    an unlimited container, which has a period but no quota.
    """
    if not text:
        return None, None
    parts = text.split()
    if len(parts) != 2:
        return None, None
    quota_text, period_text = parts
    try:
        period_us = int(period_text)
    except ValueError:
        return None, None
    if quota_text == _CPU_MAX_UNLIMITED:
        return None, period_us
    try:
        quota_us = int(quota_text)
    except ValueError:
        return None, period_us
    if period_us <= 0:
        return None, period_us
    return round(quota_us / period_us, 3), period_us


def _parse_proc_stat_cpu(text: str | None) -> float | None:
    """CPU seconds consumed by this process, from ``/proc/self/stat``.

    ``comm`` is wrapped in parentheses and may itself contain spaces and
    parentheses (``(python3)``), so the fields are counted from the last ``)``.
    utime is field 14 and stime field 15, in clock ticks.
    """
    if not text:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2 :].split()
    # Field 3 (state) is the first token after "comm)", so utime/stime land at
    # offsets 11 and 12 of the remainder.
    if len(fields) < 13:
        return None
    try:
        ticks = int(fields[11]) + int(fields[12])
    except ValueError:
        return None
    return ticks / os.sysconf("SC_CLK_TCK")


def read_cpu(
    cgroup_root: pathlib.Path = CGROUP_ROOT,
    proc_self: pathlib.Path = PROC_SELF,
    sample_seconds: float = _CPU_SAMPLE_SECONDS,
) -> dict[str, Any]:
    """Process CPU percent plus the container's CPU quota. Never raises.

    Two samples of the process counters ``sample_seconds`` apart give the
    percent of one core the process used, the same scale ``top`` reports.
    """
    quota_cores, period_us = _parse_cpu_max(_read_text(cgroup_root / "cpu.max"))
    stat_path = proc_self / "stat"
    first = _parse_proc_stat_cpu(_read_text(stat_path))
    if first is None:
        return _unavailable(f"process CPU counters not readable at {stat_path}")
    time.sleep(sample_seconds)
    second = _parse_proc_stat_cpu(_read_text(stat_path))
    if second is None:
        return _unavailable(f"process CPU counters not readable at {stat_path}")
    elapsed = max(sample_seconds, 1e-6)
    percent = round(100.0 * max(0.0, second - first) / elapsed, 1)
    logger.debug(
        "Server status CPU: %.1f%% of a core, quota=%s period=%sus",
        percent,
        quota_cores,
        period_us,
    )
    return {
        "available": True,
        "process_percent": percent,
        "quota_cores": quota_cores,
        "period_us": period_us,
    }


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _parse_proc_status_rss(text: str | None) -> int | None:
    """Resident set size in bytes from ``/proc/self/status`` (``VmRSS``)."""
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) * 1024
                except ValueError:
                    return None
    return None


def _parse_bytes(text: str | None) -> int | None:
    """Parse a cgroup byte counter; ``max`` (unlimited) becomes None."""
    if not text or text == _CPU_MAX_UNLIMITED:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def read_memory(
    cgroup_root: pathlib.Path = CGROUP_ROOT,
    proc_self: pathlib.Path = PROC_SELF,
) -> dict[str, Any]:
    """Container memory use against its limit plus this process's RSS.

    ``limit_bytes`` is None when the cgroup is unlimited (``memory.max`` is
    ``max``), and then ``percent_of_limit`` is None too.
    """
    current = _parse_bytes(_read_text(cgroup_root / "memory.current"))
    if current is None:
        return _unavailable(
            f"memory accounting not readable at {cgroup_root / 'memory.current'}"
        )
    limit = _parse_bytes(_read_text(cgroup_root / "memory.max"))
    rss = _parse_proc_status_rss(_read_text(proc_self / "status"))
    percent = round(100.0 * current / limit, 1) if limit else None
    logger.debug(
        "Server status memory: %d current, limit=%s, rss=%s", current, limit, rss
    )
    return {
        "available": True,
        "rss_bytes": rss,
        "current_bytes": current,
        "limit_bytes": limit,
        "percent_of_limit": percent,
    }


# ---------------------------------------------------------------------------
# Disk and data-directory breakdown
# ---------------------------------------------------------------------------


def read_disk(
    db_path: pathlib.Path,
    transcripts_dir: pathlib.Path,
    log_dir: pathlib.Path | None,
) -> dict[str, Any]:
    """Filesystem usage plus the size of the pieces the data directory holds.

    Reuses the ``read_data_usage`` probe /metrics already relies on, so the two
    surfaces can never disagree about the same filesystem.
    """
    breakdown = {
        "database_bytes": _tree_size(pathlib.Path(db_path)),
        "transcripts_bytes": _tree_size(transcripts_dir),
        "logs_bytes": _tree_size(log_dir) if log_dir else None,
    }
    usage = read_data_usage(str(db_path))
    if usage is None:
        return {
            **_unavailable("data directory usage not readable"),
            **breakdown,
        }
    used, capacity = usage
    ratio = round(used / capacity, 4) if capacity else None
    logger.debug(
        "Server status disk: %d of %d bytes (ratio=%s) %s",
        used,
        capacity,
        ratio,
        breakdown,
    )
    return {
        "available": True,
        "used_bytes": used,
        "capacity_bytes": capacity,
        "used_ratio": ratio,
        "filesystem_note": _FILESYSTEM_NOTE,
        **breakdown,
    }


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------


# Process start, for the uptime the page shows.
_PROCESS_STARTED = time.monotonic()


def read_process() -> dict[str, Any]:
    """Version, build sha, uptime, pid and the last event-loop lag sample."""
    return {
        "version": _get_app_version(),
        # The image exports its commit; empty when built outside a CI that does.
        "git_sha": os.getenv("GIT_SHA", ""),
        "uptime_seconds": round(time.monotonic() - _PROCESS_STARTED, 1),
        "pid": os.getpid(),
        "loop_lag_ms": loop_lag_ms(),
    }


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Object tables the status page counts. Table names are literals, never input.
_COUNTED_TABLES = {
    "results": "results",
    "failures": "failure_reviews",
    "test_entries": "test_entries",
}


def _db_size_bytes(db_path: pathlib.Path) -> int | None:
    """Size of the database file in bytes; None when it does not exist yet."""
    try:
        return pathlib.Path(db_path).stat().st_size
    except OSError:
        return None


async def read_database(db_path: pathlib.Path) -> dict[str, Any]:
    """Database size and row counts for the object tables. Never raises."""
    size = await asyncio.to_thread(_db_size_bytes, db_path)
    counts: dict[str, int] = {}
    reason = ""
    try:
        async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            for name, table in _COUNTED_TABLES.items():
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cursor.fetchone()
                counts[name] = row[0] if row else 0
    except (aiosqlite.Error, OSError) as exc:
        reason = f"database not readable: {exc}"
        logger.debug("Server status database probe failed", exc_info=True)
    block: dict[str, Any] = {"available": not reason, "size_bytes": size, **counts}
    if reason:
        block["reason"] = reason
    else:
        logger.debug("Server status database: size=%s counts=%s", size, counts)
    return block


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def _sidecar_health_url() -> str:
    """Health URL of the running sidecar, following the effective listener.

    Mirrors ``monitoring.check_ai_provider``: SIDECAR_URL when set (the
    entrypoint and the DB port override keep it in sync), else SIDECAR_PORT.
    """
    port = os.getenv("SIDECAR_PORT", "").strip() or "9100"
    base = os.getenv("SIDECAR_URL", "").strip() or f"http://127.0.0.1:{port}"
    return f"{base.rstrip('/')}/health"


async def read_sidecar() -> dict[str, Any]:
    """Sidecar reachability plus catalog and free-pair counts. Never raises."""
    from rootcoz import ai_client

    result: dict[str, Any] = {
        "reachable": False,
        "models": None,
        "free_models": None,
    }
    try:
        result["free_models"] = len(await storage.list_free_ai_models())
    except aiosqlite.Error, OSError:
        logger.debug("Server status: free model count failed", exc_info=True)

    health = await _check_http_service(_sidecar_health_url(), ok_below=300)
    if health["status"] != "ok":
        result["reason"] = health.get("detail") or health["status"]
        logger.debug("Server status: sidecar not reachable (%s)", result["reason"])
        return result

    result["reachable"] = True
    try:
        result["models"] = len(await ai_client.list_models())
    except Exception:
        # The sidecar answers /health but not the catalog: still reachable,
        # the model count is simply unknown.
        logger.debug("Server status: sidecar model listing failed", exc_info=True)
    logger.debug(
        "Server status sidecar: models=%s free=%s",
        result["models"],
        result["free_models"],
    )
    return result


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def _warn(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def build_warnings(
    *,
    disk: dict[str, Any],
    memory: dict[str, Any],
    process: dict[str, Any],
    sidecar: dict[str, Any],
) -> list[dict[str, str]]:
    """Derive the page's banners from the same numbers the blocks report.

    A block that is unavailable raises no threshold warning: a probe that could
    not read the number must not claim a healthy or a critical value.
    """
    warnings: list[dict[str, str]] = []

    ratio = disk.get("used_ratio")
    if ratio is not None:
        percent = ratio * 100
        if percent >= _DISK_CRITICAL_PERCENT:
            warnings.append(
                _warn(
                    "critical",
                    "disk_critical",
                    f"Data directory is {percent:.0f}% full — free space now, "
                    "writes will start failing.",
                )
            )
        elif percent >= _DISK_WARN_PERCENT:
            warnings.append(
                _warn(
                    "warn",
                    "disk_high",
                    f"Data directory is {percent:.0f}% full — plan more space.",
                )
            )

    memory_percent = memory.get("percent_of_limit")
    if memory_percent is not None and memory_percent >= _MEMORY_CRITICAL_PERCENT:
        warnings.append(
            _warn(
                "critical",
                "memory_critical",
                f"Memory at {memory_percent:.0f}% of the container limit — "
                "the process may be OOM-killed.",
            )
        )

    lag = process.get("loop_lag_ms")
    if lag is not None and lag > _LOOP_LAG_WARN_MS:
        warnings.append(
            _warn(
                "warn",
                "loop_lag_high",
                f"Event loop stalled {lag / 1000:.1f}s — requests are queueing.",
            )
        )

    if sidecar.get("reachable") is False:
        warnings.append(
            _warn(
                "warn",
                "sidecar_unreachable",
                "AI sidecar is unreachable — analyses and AI chat will fail.",
            )
        )

    if warnings:
        # DEBUG, not WARNING: the page polls, and the banners already say it.
        logger.debug(
            "Server status warnings: %s", ", ".join(w["code"] for w in warnings)
        )
    return warnings


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


async def _guarded(
    name: str,
    probe: Coroutine[Any, Any, dict[str, Any]],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Run one probe; a crash in it must not fail the whole snapshot."""
    try:
        return await probe
    except Exception as exc:
        logger.warning("Server status probe %s failed", name, exc_info=True)
        return {**fallback, "reason": f"probe failed: {exc}"}


async def _analyses_running() -> int | None:
    """Analyses in an active status; None when storage cannot answer."""
    try:
        return await storage.count_active_analyses()
    except aiosqlite.Error, OSError:
        logger.debug("Server status: active analysis count failed", exc_info=True)
        return None


async def collect_server_status(
    *,
    db_path: pathlib.Path,
    transcripts_dir: pathlib.Path,
    log_dir: pathlib.Path | None = None,
    sse_clients: int | None = None,
    cgroup_root: pathlib.Path = CGROUP_ROOT,
    proc_self: pathlib.Path = PROC_SELF,
) -> dict[str, Any]:
    """Assemble the admin server-status snapshot. Never raises."""
    disk, cpu, memory, database, sidecar = await asyncio.gather(
        _guarded(
            "disk",
            asyncio.to_thread(read_disk, db_path, transcripts_dir, log_dir),
            _unavailable("disk probe unavailable"),
        ),
        _guarded(
            "cpu",
            asyncio.to_thread(read_cpu, cgroup_root, proc_self),
            _unavailable("CPU probe unavailable"),
        ),
        _guarded(
            "memory",
            asyncio.to_thread(read_memory, cgroup_root, proc_self),
            _unavailable("memory probe unavailable"),
        ),
        _guarded(
            "database",
            read_database(db_path),
            _unavailable("database probe unavailable"),
        ),
        _guarded("sidecar", read_sidecar(), _unavailable("sidecar probe unavailable")),
    )

    activity: dict[str, Any] = {}
    running = await _analyses_running()
    if running is not None:
        activity["analyses_running"] = running
    if sse_clients is not None:
        activity["sse_clients"] = sse_clients

    status: dict[str, Any] = {
        "disk": disk,
        "cpu": cpu,
        "memory": memory,
        "process": read_process(),
        "database": database,
        "sidecar": sidecar,
        "activity": activity,
    }
    status["warnings"] = build_warnings(
        disk=disk, memory=memory, process=status["process"], sidecar=sidecar
    )
    logger.debug(
        "Server status collected: %d warning(s), activity=%s",
        len(status["warnings"]),
        activity,
    )
    return status
