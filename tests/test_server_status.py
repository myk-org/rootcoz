"""Tests for the admin server-status probes, endpoint and CLI command.

Every probe is exercised against a fake cgroup/``/proc`` tree so no test reads
the host's real ``/proc`` or cgroup files, and no test touches the network.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rootcoz import server_status, storage
from rootcoz.config import get_settings

# ---------------------------------------------------------------------------
# Fake cgroup / proc trees
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _fake_roots(
    tmp_path: Path,
    *,
    cpu_max: str | None = "200000 100000",
    memory_current: str | None = "50000000",
    memory_max: str | None = "100000000",
    memory_stat: str | None = "inactive_file 0\nactive_file 0\n",
    rss_kb: str | None = "1234",
    cpu_ticks: tuple[int, int] | None = (100, 50),
) -> tuple[Path, Path]:
    """Build a cgroup root and a proc root holding the files the probes read."""
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir(exist_ok=True)
    if cpu_max is not None:
        _write(cgroup / "cpu.max", cpu_max + "\n")
    if memory_current is not None:
        _write(cgroup / "memory.current", memory_current + "\n")
    if memory_max is not None:
        _write(cgroup / "memory.max", memory_max + "\n")
    if memory_stat is not None:
        _write(cgroup / "memory.stat", memory_stat)

    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    if rss_kb is not None:
        _write(proc / "status", f"Name:\tpython3\nVmRSS:\t{rss_kb} kB\n")
    if cpu_ticks is not None:
        # comm carries spaces/parens on purpose: the parser must count from the
        # last ')' or every following field shifts.
        fields = ["S", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
        fields += [str(cpu_ticks[0]), str(cpu_ticks[1])]
        fields += ["0"] * 20
        _write(proc / "stat", "1234 (my python3) " + " ".join(fields) + "\n")
    return cgroup, proc


class _Clock:
    """Stand-in for ``server_status.time``: sleeping advances a fake clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------


class TestCpu:
    def test_quota_and_unlimited(self) -> None:
        assert server_status._parse_cpu_max("200000 100000") == (2.0, 100000)
        assert server_status._parse_cpu_max("max 100000") == (None, 100000)
        assert server_status._parse_cpu_max("") == (None, None)
        assert server_status._parse_cpu_max("garbage") == (None, None)
        assert server_status._parse_cpu_max("1000 notanint") == (None, None)

    def test_process_percent_from_two_samples(self, tmp_path, monkeypatch) -> None:
        cgroup, proc = _fake_roots(tmp_path)
        samples = iter([10.0, 10.05])
        monkeypatch.setattr(server_status, "time", _Clock())

        with patch.object(
            server_status,
            "_parse_proc_stat_cpu",
            lambda _text: next(samples),
        ):
            block = server_status.read_cpu(cgroup, proc, sample_seconds=0.1)

        assert block["available"] is True
        # 0.05 CPU-seconds over a 0.1 s window is half a core.
        assert block["process_percent"] == 50.0
        assert block["quota_cores"] == 2.0
        assert block["quota_state"] == "set"
        assert block["period_us"] == 100000

    def test_percent_divides_by_the_measured_window(
        self, tmp_path, monkeypatch
    ) -> None:
        """A late sample must not inflate the percentage."""
        cgroup, proc = _fake_roots(tmp_path)
        samples = iter([10.0, 10.1])

        class _SlowClock(_Clock):
            def sleep(self, seconds: float) -> None:
                # The thread slept twice as long as asked.
                self.now += seconds * 2

        monkeypatch.setattr(server_status, "time", _SlowClock())
        with patch.object(
            server_status, "_parse_proc_stat_cpu", lambda _text: next(samples)
        ):
            block = server_status.read_cpu(cgroup, proc, sample_seconds=0.1)
        # 0.1 CPU-seconds over the 0.2 s that actually elapsed.
        assert block["process_percent"] == 50.0

    def test_parses_proc_stat_with_parentheses_in_comm(self, tmp_path) -> None:
        _cgroup, proc = _fake_roots(tmp_path, cpu_ticks=(100, 50))
        ticks = (100 + 50) / os.sysconf("SC_CLK_TCK")
        assert server_status._parse_proc_stat_cpu((proc / "stat").read_text()) == ticks

    def test_unreadable_proc_stat_degrades(self, tmp_path) -> None:
        cgroup, proc = _fake_roots(tmp_path, cpu_ticks=None)
        block = server_status.read_cpu(cgroup, proc)
        assert block["available"] is False
        assert "not readable" in block["reason"]

    def test_missing_cpu_max_keeps_percent(self, tmp_path, monkeypatch) -> None:
        cgroup, proc = _fake_roots(tmp_path, cpu_max=None)
        samples = iter([1.0, 1.1])
        monkeypatch.setattr(server_status, "time", _Clock())
        with patch.object(
            server_status, "_parse_proc_stat_cpu", lambda _text: next(samples)
        ):
            block = server_status.read_cpu(cgroup, proc, sample_seconds=0.1)
        assert block["available"] is True
        assert block["quota_cores"] is None
        # An unreadable cpu.max is unknown, never "no quota — unlimited".
        assert block["quota_state"] == "unknown"
        assert block["period_us"] is None

    def test_unlimited_cpu_max_is_unlimited(self, tmp_path, monkeypatch) -> None:
        cgroup, proc = _fake_roots(tmp_path, cpu_max="max 100000")
        samples = iter([1.0, 1.1])
        monkeypatch.setattr(server_status, "time", _Clock())
        with patch.object(
            server_status, "_parse_proc_stat_cpu", lambda _text: next(samples)
        ):
            block = server_status.read_cpu(cgroup, proc, sample_seconds=0.1)
        assert block["quota_cores"] is None
        assert block["quota_state"] == "unlimited"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class TestMemory:
    def test_usage_against_limit(self, tmp_path) -> None:
        cgroup, proc = _fake_roots(
            tmp_path, memory_current="90000000", memory_max="100000000"
        )
        block = server_status.read_memory(cgroup, proc)
        assert block["available"] is True
        assert block["current_bytes"] == 90000000
        assert block["working_set_bytes"] == 90000000
        assert block["limit_bytes"] == 100000000
        assert block["limit_state"] == "set"
        assert block["percent_of_limit"] == 90.0
        assert block["rss_bytes"] == 1234 * 1024

    def test_page_cache_is_not_part_of_the_working_set(self, tmp_path) -> None:
        """Reclaimable page cache must not read as memory pressure."""
        cgroup, proc = _fake_roots(
            tmp_path,
            memory_current="95000000",
            memory_max="100000000",
            memory_stat="inactive_file 50000000\n",
        )
        block = server_status.read_memory(cgroup, proc)
        assert block["current_bytes"] == 95000000
        assert block["working_set_bytes"] == 45000000
        assert block["percent_of_limit"] == 45.0

    def test_unlimited_cgroup(self, tmp_path) -> None:
        cgroup, proc = _fake_roots(tmp_path, memory_max="max")
        block = server_status.read_memory(cgroup, proc)
        assert block["available"] is True
        assert block["limit_bytes"] is None
        assert block["limit_state"] == "unlimited"
        assert block["percent_of_limit"] is None

    def test_unreadable_limit_is_unknown_not_unlimited(self, tmp_path) -> None:
        cgroup, proc = _fake_roots(tmp_path, memory_max=None)
        block = server_status.read_memory(cgroup, proc)
        assert block["available"] is True
        assert block["limit_bytes"] is None
        assert block["limit_state"] == "unknown"
        assert block["percent_of_limit"] is None

    def test_missing_memory_current_degrades(self, tmp_path) -> None:
        cgroup, proc = _fake_roots(tmp_path, memory_current=None)
        block = server_status.read_memory(cgroup, proc)
        assert block["available"] is False
        assert "memory accounting not readable" in block["reason"]

    def test_missing_proc_status_leaves_rss_unknown(self, tmp_path) -> None:
        cgroup, proc = _fake_roots(tmp_path, rss_kb=None)
        block = server_status.read_memory(cgroup, proc)
        assert block["available"] is True
        assert block["rss_bytes"] is None


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------


class TestDisk:
    def test_breakdown_of_the_data_directory(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        db_path = _write(data_dir / "results.db", "x" * 100)
        transcripts = data_dir / "pi-sidecar" / "transcripts"
        _write(transcripts / "a.jsonl", "y" * 40)
        _write(transcripts / "nested" / "b.jsonl", "z" * 10)
        logs = data_dir / "logs"
        _write(logs / "rootcoz.log", "w" * 30)

        block = server_status.read_disk(db_path, transcripts, logs)

        assert block["available"] is True
        assert block["database_bytes"] == 100
        assert block["transcripts_bytes"] == 50
        assert block["logs_bytes"] == 30
        assert 0 < block["used_ratio"] <= 1
        assert block["used_bytes"] <= block["capacity_bytes"]
        # Generality: the note says which filesystem, never which backend.
        assert "/data" not in block["filesystem_note"]

    def test_absent_directories_report_none(self, tmp_path) -> None:
        db_path = tmp_path / "results.db"
        block = server_status.read_disk(db_path, tmp_path / "no-transcripts", None)
        assert block["available"] is True
        assert block["database_bytes"] is None
        assert block["transcripts_bytes"] is None
        assert block["logs_bytes"] is None

    def test_unreadable_filesystem_degrades(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("rootcoz.server_status.read_data_usage", lambda _path: None)
        block = server_status.read_disk(tmp_path / "results.db", tmp_path, None)
        assert block["available"] is False
        assert "not readable" in block["reason"]

    def test_unreadable_subtree_reports_unknown_size(
        self, tmp_path, monkeypatch
    ) -> None:
        """A partial total must not be shown as the directory's real size."""
        transcripts = tmp_path / "transcripts"
        _write(transcripts / "a.jsonl", "y" * 40)
        _write(transcripts / "b.jsonl", "y" * 10)
        real_getsize = os.path.getsize

        def _getsize(path):
            if str(path).endswith("b.jsonl"):
                raise OSError("permission denied")
            return real_getsize(path)

        monkeypatch.setattr(server_status.os.path, "getsize", _getsize)
        block = server_status.read_disk(tmp_path / "results.db", transcripts, None)
        assert block["transcripts_bytes"] is None

    def test_a_file_removed_mid_scan_is_skipped(self, tmp_path, monkeypatch) -> None:
        """Rotation deleting a file between the walk and the stat is not an
        unreadable subtree: the rest of the total still counts."""
        transcripts = tmp_path / "transcripts"
        _write(transcripts / "a.jsonl", "y" * 40)
        _write(transcripts / "b.jsonl", "y" * 10)
        real_getsize = os.path.getsize

        def _getsize(path):
            if str(path).endswith("b.jsonl"):
                raise FileNotFoundError(str(path))
            return real_getsize(path)

        monkeypatch.setattr(server_status.os.path, "getsize", _getsize)
        block = server_status.read_disk(tmp_path / "results.db", transcripts, None)
        assert block["transcripts_bytes"] == 40

    def test_database_size_includes_the_wal(self, tmp_path) -> None:
        """Committed pages can live in -wal until a checkpoint."""
        db_path = _write(tmp_path / "results.db", "x" * 100)
        _write(Path(f"{db_path}-wal"), "w" * 40)
        _write(Path(f"{db_path}-shm"), "s" * 10)
        block = server_status.read_disk(db_path, tmp_path / "none", None)
        assert block["database_bytes"] == 150

    def test_note_states_the_contract(self) -> None:
        note = server_status._FILESYSTEM_NOTE
        assert "not a per-volume quota" in note
        assert "other tenants" in note


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def _warnings(disk_ratio=None, memory_percent=None, lag=None, reachable=True):
    return server_status.build_warnings(
        disk={"used_ratio": disk_ratio},
        memory={"percent_of_limit": memory_percent},
        process={"loop_lag_ms": lag},
        sidecar={"reachable": reachable},
    )


def _codes(warnings: list[dict[str, str]]) -> list[str]:
    return [warning["code"] for warning in warnings]


class TestWarnings:
    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [
            (0.0, []),
            (0.7499, []),
            (0.75, ["disk_high"]),
            (0.8999, ["disk_high"]),
            (0.90, ["disk_critical"]),
            (0.99, ["disk_critical"]),
        ],
    )
    def test_disk_thresholds(self, ratio, expected) -> None:
        assert _codes(_warnings(disk_ratio=ratio)) == expected

    @pytest.mark.parametrize(
        ("percent", "expected"),
        [(0.0, []), (89.9, []), (90.0, ["memory_critical"])],
    )
    def test_memory_thresholds(self, percent, expected) -> None:
        assert _codes(_warnings(memory_percent=percent)) == expected

    @pytest.mark.parametrize(
        ("lag", "expected"),
        [
            (0.0, []),
            (5000.0, []),
            (5000.1, ["loop_lag_high"]),
            (9000.0, ["loop_lag_high"]),
        ],
    )
    def test_loop_lag_thresholds(self, lag, expected) -> None:
        assert _codes(_warnings(lag=lag)) == expected

    def test_sidecar_unreachable_is_a_warning(self) -> None:
        assert _codes(_warnings(reachable=False)) == ["sidecar_unreachable"]

    def test_unavailable_numbers_raise_no_threshold_warning(self) -> None:
        warnings = server_status.build_warnings(
            disk={"available": False, "reason": "nope"},
            memory={"available": False, "reason": "nope"},
            process={"loop_lag_ms": None},
            sidecar={"reachable": False},
        )
        assert _codes(warnings) == ["sidecar_unreachable"]

    def test_levels_and_short_messages(self) -> None:
        warnings = _warnings(
            disk_ratio=0.95, memory_percent=95.0, lag=9000.0, reachable=False
        )
        assert [warning["level"] for warning in warnings] == [
            "critical",
            "critical",
            "warn",
            "warn",
        ]
        for warning in warnings:
            assert warning["code"]
            assert 0 < len(warning["message"]) <= 100


# ---------------------------------------------------------------------------
# Database / sidecar / process
# ---------------------------------------------------------------------------


class TestDatabaseProbe:
    def test_counts_the_object_tables(self, temp_db_path) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(storage.init_db())
            block = asyncio.run(server_status.read_database(temp_db_path))
        assert block["available"] is True
        assert block["results"] == 0
        assert block["failures"] == 0
        assert block["test_entries"] == 0
        assert block["size_bytes"] == temp_db_path.stat().st_size

    def test_failures_counts_recorded_failures_not_review_flags(
        self, temp_db_path
    ) -> None:
        async def _seed() -> None:
            async with storage._connect_db() as db:
                for test_name in ("t1", "t2"):
                    await db.execute(
                        "INSERT INTO failure_history "
                        "(job_id, job_name, build_number, test_name) "
                        "VALUES ('j1', 'job', 1, ?)",
                        (test_name,),
                    )
                await db.execute(
                    "INSERT INTO failure_reviews (job_id, test_name) VALUES ('j1', 't1')"
                )
                await db.commit()

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(storage.init_db())
            asyncio.run(_seed())
            block = asyncio.run(server_status.read_database(temp_db_path))
        assert block["failures"] == 2

    def test_missing_database_degrades(self, tmp_path) -> None:
        block = asyncio.run(server_status.read_database(tmp_path / "nope.db"))
        assert block["available"] is False
        assert block["reason"]


class TestSidecarProbe:
    def test_unreachable_sidecar_is_not_an_error(self, monkeypatch) -> None:
        async def _down(_url, **_kwargs):
            return {"status": "error", "detail": "connection refused"}

        monkeypatch.setattr("rootcoz.server_status._check_http_service", _down)
        block = asyncio.run(server_status.read_sidecar())
        assert block["reachable"] is False
        assert block["available"] is False
        assert block["reason"] == "connection refused"

    def test_reachable_sidecar_reports_model_counts(self, monkeypatch) -> None:
        async def _up(_url, **_kwargs):
            return {"status": "ok"}

        async def _models(_provider=""):
            return [{"provider": "vertex"}, {"provider": "cursor"}]

        async def _free():
            return [{"provider": "vertex", "model": "flash"}]

        monkeypatch.setattr("rootcoz.server_status._check_http_service", _up)
        monkeypatch.setattr("rootcoz.ai_client.list_models", _models)
        monkeypatch.setattr(storage, "list_free_ai_models", _free)
        block = asyncio.run(server_status.read_sidecar())
        assert block["reachable"] is True
        # Availability and reachability cannot disagree, or a consumer that
        # trusts ``available`` reads a healthy sidecar as unavailable.
        assert block["available"] is True
        assert block["models"] == 2
        assert block["free_models"] == 1


class TestProcessProbe:
    def test_uptime_and_loop_lag(self, monkeypatch) -> None:
        server_status.reset_loop_lag()
        block = server_status.read_process()
        assert block["pid"] == os.getpid()
        assert block["uptime_seconds"] >= 0
        assert block["loop_lag_ms"] is None
        # A container cannot read its own restart count: unknown, not zero.
        assert block["restart_count"] is None

        monkeypatch.setattr(server_status, "_loop_lag_peak", 42.0)
        assert server_status.read_process()["loop_lag_ms"] == 42.0
        server_status.reset_loop_lag()


class TestLoopLagWatchdog:
    def test_samples_then_stops(self, monkeypatch) -> None:
        server_status.reset_loop_lag()
        monkeypatch.setattr(server_status, "_LOOP_LAG_INTERVAL_SECONDS", 0.01)

        async def _run() -> None:
            server_status.start_loop_lag_watchdog()
            await asyncio.sleep(0.05)
            server_status.stop_loop_lag_watchdog()

        asyncio.run(_run())
        assert server_status.loop_lag_ms() is not None
        assert server_status.loop_lag_ms() >= 0
        server_status.reset_loop_lag()

    def test_a_blocked_loop_is_reported_as_lag(self) -> None:
        """A callback that runs late reports the delay, not zero."""
        server_status.reset_loop_lag()

        class _Loop:
            """Loop stub: time() jumps 1.5 s past the expected tick."""

            def __init__(self) -> None:
                self.calls: list[tuple[float, object]] = []
                self.now = 0.0

            def time(self) -> float:
                return self.now

            def call_later(self, delay: float, callback) -> object:
                self.calls.append((delay, callback))
                return "handle"

        loop = _Loop()
        server_status._schedule_loop_lag_sample(loop, expected=0.0)
        _delay, tick = loop.calls[0]
        loop.now = 1.5
        tick()
        assert server_status.loop_lag_ms() == 1500.0
        # The next sample is on time; the stall stays reportable, so a status
        # request every 30 s still sees it.
        loop.now = 2.5
        _delay, next_tick = loop.calls[1]
        next_tick()
        assert server_status.loop_lag_ms() == 1500.0
        # Re-armed for the next sample.
        assert len(loop.calls) == 3
        server_status.reset_loop_lag()


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


class TestCollectServerStatus:
    def _status(self, tmp_path, **kwargs):
        async def _snapshot():
            # Generous outer bound: a probe that holds the snapshot open fails
            # the test instead of hanging it. Only a real hang reaches it.
            return await asyncio.wait_for(
                server_status.collect_server_status(
                    db_path=tmp_path / "results.db",
                    transcripts_dir=tmp_path / "transcripts",
                    log_dir=tmp_path / "logs",
                    **kwargs,
                ),
                timeout=60,
            )

        return asyncio.run(_snapshot())

    def test_shape(self, tmp_path, monkeypatch) -> None:
        cgroup, proc = _fake_roots(tmp_path)
        monkeypatch.setattr("rootcoz.server_status.read_data_usage", lambda _p: (1, 4))
        monkeypatch.setattr(storage, "list_active_analyses", _async(_running_rows))
        monkeypatch.setattr(storage, "count_active_analyses", _async(2))
        monkeypatch.setattr(server_status, "read_sidecar", _sidecar_up)

        status = self._status(
            tmp_path, sse_clients=2, chat_turns=1, cgroup_root=cgroup, proc_self=proc
        )

        assert set(status) == {
            "disk",
            "cpu",
            "memory",
            "process",
            "database",
            "sidecar",
            "activity",
            "warnings",
        }
        assert status["disk"]["used_ratio"] == 0.25
        activity = status["activity"]
        assert activity["analyses_running"] == 2
        assert activity["sse_clients"] == 2
        assert activity["chat_turns_in_flight"] == 1
        # Each running analysis carries its own elapsed time.
        assert [run["job_id"] for run in activity["analyses"]] == ["job-1", "job-2"]
        assert activity["analyses"][0]["elapsed_seconds"] >= 3600
        assert activity["analyses"][1]["elapsed_seconds"] is None
        assert status["warnings"] == []
        # No secrets, no credential material anywhere in the payload.
        dumped = json.dumps(status).lower()
        for needle in ("api_key", "token", "password", "secret"):
            assert needle not in dumped

    def test_a_crashed_sidecar_probe_still_raises_the_warning(
        self, tmp_path, monkeypatch
    ) -> None:
        cgroup, proc = _fake_roots(tmp_path)

        async def _boom() -> dict:
            raise RuntimeError("sidecar exploded")

        monkeypatch.setattr(server_status, "read_sidecar", _boom)

        status = self._status(tmp_path, cgroup_root=cgroup, proc_self=proc)
        assert status["sidecar"]["reachable"] is False
        assert "sidecar_unreachable" in [
            warning["code"] for warning in status["warnings"]
        ]

    def test_a_crashing_probe_degrades_only_its_block(
        self, tmp_path, monkeypatch
    ) -> None:
        cgroup, proc = _fake_roots(tmp_path)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(server_status, "read_cpu", _boom)
        monkeypatch.setattr("rootcoz.server_status.read_data_usage", lambda _p: (1, 4))
        monkeypatch.setattr(server_status, "read_sidecar", _sidecar_up)

        status = self._status(tmp_path, cgroup_root=cgroup, proc_self=proc)
        assert status["cpu"]["available"] is False
        assert "boom" in status["cpu"]["reason"]
        # The other blocks still answer.
        assert status["memory"]["available"] is True
        assert status["sidecar"]["reachable"] is True

    def test_a_hanging_probe_times_out(self, tmp_path, monkeypatch) -> None:
        """A probe that never returns degrades only its own block.

        The stalled probe waits on an event this test never sets, so it cannot
        finish before its deadline however loaded the runner is, and every other
        probe keeps the deadline production uses. Nothing here is decided by a
        10 ms window a descheduled runner can miss (issue #451).

        The stalled probe is the async database read, not the threaded disk one:
        a thread that never returns cannot be cancelled, only released.
        """
        cgroup, proc = _fake_roots(tmp_path)
        never = asyncio.Event()
        monkeypatch.setitem(server_status._PROBE_DEADLINES, "database", 0.01)
        monkeypatch.setattr("rootcoz.server_status.read_data_usage", lambda _p: (1, 4))
        monkeypatch.setattr(server_status, "read_sidecar", _sidecar_up)

        async def stalled_database(*_args, **_kwargs) -> dict:
            await never.wait()
            return {}

        monkeypatch.setattr(server_status, "read_database", stalled_database)

        status = self._status(tmp_path, cgroup_root=cgroup, proc_self=proc)

        assert status["database"]["available"] is False
        assert "timed out" in status["database"]["reason"]
        # The other blocks still answer: their deadlines were never shortened.
        assert status["disk"]["available"] is True
        assert status["cpu"]["available"] is True
        assert status["memory"]["available"] is True

    def test_activity_omits_numbers_storage_cannot_give(
        self, tmp_path, monkeypatch
    ) -> None:
        cgroup, proc = _fake_roots(tmp_path)

        async def _broken():
            raise OSError("db gone")

        monkeypatch.setattr(storage, "count_active_analyses", _broken)
        monkeypatch.setattr(storage, "list_active_analyses", _broken)
        monkeypatch.setattr(server_status, "read_sidecar", _sidecar_up)

        status = self._status(tmp_path, cgroup_root=cgroup, proc_self=proc)
        assert status["activity"] == {}

    def test_running_count_is_not_the_display_limit(
        self, tmp_path, monkeypatch
    ) -> None:
        """The list is capped at 20 rows; the count must still be the backlog."""
        cgroup, proc = _fake_roots(tmp_path)
        monkeypatch.setattr("rootcoz.server_status.read_data_usage", lambda _p: (1, 4))
        monkeypatch.setattr(storage, "list_active_analyses", _async(_running_rows))
        monkeypatch.setattr(storage, "count_active_analyses", _async(37))
        monkeypatch.setattr(server_status, "read_sidecar", _sidecar_up)

        status = self._status(tmp_path, cgroup_root=cgroup, proc_self=proc)
        assert status["activity"]["analyses_running"] == 37
        # The rows are still the bounded detail list.
        assert len(status["activity"]["analyses"]) == 2


def _running_rows() -> list[dict]:
    """Two active analyses: one with a start time, one without."""
    return [
        {
            "job_id": "job-1",
            "job_name": "nightly",
            "build_number": 7,
            "status": "running",
            "created_at": "2020-01-01 00:00:00",
            "analysis_started_at": "2020-01-01 00:00:00",
        },
        {
            "job_id": "job-2",
            "job_name": "gating",
            "build_number": 8,
            "status": "pending",
            "created_at": "not a timestamp",
            "analysis_started_at": None,
        },
    ]


def _async(value):
    """Wrap a plain value or zero-arg callable in an async function."""

    async def _inner(*_args, **_kwargs):
        return value() if callable(value) else value

    return _inner


async def _sidecar_up() -> dict:
    return {"reachable": True, "models": 1, "free_models": 0}


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(temp_db_path):
    """Test client with an admin key, a temp database and no real probes."""
    with (
        patch.dict(
            os.environ,
            {
                "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
                "SECURE_COOKIES": "false",
                "DB_PATH": str(temp_db_path),
                "REQUIRE_APPROVAL": "false",
            },
        ),
        patch.object(storage, "DB_PATH", temp_db_path),
    ):
        get_settings.cache_clear()
        asyncio.run(storage.init_db())
        from rootcoz.main import app

        with TestClient(app) as test_client:
            yield test_client
        get_settings.cache_clear()


@pytest.fixture
def stub_probes(monkeypatch):
    """Keep the HTTP tests off /proc, cgroup and the network."""
    monkeypatch.setattr(
        server_status,
        "read_cpu",
        lambda *_a, **_k: {
            "available": True,
            "process_percent": 1.0,
            "quota_cores": 2.0,
            "period_us": 100000,
        },
    )
    monkeypatch.setattr(
        server_status,
        "read_memory",
        lambda *_a, **_k: {
            "available": True,
            "rss_bytes": 10,
            "current_bytes": 10,
            "limit_bytes": 100,
            "percent_of_limit": 10.0,
        },
    )
    monkeypatch.setattr(
        server_status,
        "read_disk",
        lambda *_a, **_k: {
            "available": True,
            "used_bytes": 75,
            "capacity_bytes": 100,
            "used_ratio": 0.75,
            "filesystem_note": "note",
            "database_bytes": 1,
            "transcripts_bytes": 1,
            "logs_bytes": 1,
        },
    )
    monkeypatch.setattr(server_status, "read_sidecar", _sidecar_up)


class TestServerStatusEndpoint:
    def test_admin_gets_the_snapshot(self, client, stub_probes) -> None:
        cookies = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        ).cookies
        resp = client.get("/api/admin/server-status", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["disk"]["used_ratio"] == 0.75
        assert data["process"]["pid"] > 0
        assert data["database"]["results"] == 0
        assert [w["code"] for w in data["warnings"]] == ["disk_high"]
        assert "sse_clients" in data["activity"]
        assert "chat_turns_in_flight" in data["activity"]

    def test_disk_probe_measures_the_resolved_transcripts_dir(
        self, client, stub_probes, monkeypatch, tmp_path
    ) -> None:
        """A host install records under the sidecar default, not /data.

        The block must report the size of the directory the sidecar was
        actually told to use, and only that directory.
        """
        from rootcoz import transcripts as transcripts_module

        seeded = tmp_path / "home" / ".pi" / "sidecar" / "transcripts"
        seeded.mkdir(parents=True)
        (seeded / "a.jsonl").write_text("y" * 40)
        monkeypatch.delenv("PI_SIDECAR_TRANSCRIPTS_DIR", raising=False)
        monkeypatch.setattr(
            transcripts_module, "SIDECAR_DEFAULT_TRANSCRIPTS_DIR", seeded
        )
        probed: list[Path] = []

        def _read_disk(_db_path, transcripts_dir, _log_dir):
            probed.append(transcripts_dir)
            return {
                "available": True,
                "used_ratio": 0.75,
                "transcripts_bytes": server_status._tree_size(transcripts_dir),
            }

        monkeypatch.setattr(server_status, "read_disk", _read_disk)
        cookies = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        ).cookies
        resp = client.get("/api/admin/server-status", cookies=cookies)
        assert resp.status_code == 200
        assert probed == [seeded]
        assert resp.json()["disk"]["transcripts_bytes"] == 40

    def test_operation_id_follows_the_handler_name(self, client) -> None:
        from rootcoz.main import app

        operation = app.openapi()["paths"]["/api/admin/server-status"]["get"]
        assert operation["operationId"] == "adminServerStatus"

    def test_response_goes_through_the_sensitive_filter(
        self, client, stub_probes, monkeypatch
    ) -> None:
        """Same pattern as the sibling admin handlers: never returned raw."""
        from rootcoz import main as main_module

        seen: list[dict] = []

        def _spy(payload):
            seen.append(payload)
            return payload

        monkeypatch.setattr(main_module, "strip_sensitive_from_response", _spy)
        cookies = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        ).cookies
        resp = client.get("/api/admin/server-status", cookies=cookies)
        assert resp.status_code == 200
        assert seen and "disk" in seen[0]

    def test_anonymous_is_rejected(self, client, stub_probes) -> None:
        resp = client.get("/api/admin/server-status")
        assert resp.status_code in (401, 403)

    def test_viewer_is_forbidden(self, client, stub_probes) -> None:
        client.post(
            "/api/auth/register",
            json={
                "username": "viewer1",
                "api_key": "viewer-key-16chars",  # pragma: allowlist secret
            },
        )
        cookies = client.post(
            "/api/auth/login",
            json={
                "username": "viewer1",
                "api_key": "viewer-key-16chars",  # pragma: allowlist secret
            },
        ).cookies
        resp = client.get("/api/admin/server-status", cookies=cookies)
        assert resp.status_code == 403

    def test_a_raising_probe_still_answers_200(
        self, client, stub_probes, monkeypatch
    ) -> None:
        def _boom(*_args, **_kwargs):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(server_status, "read_disk", _boom)
        cookies = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        ).cookies
        resp = client.get("/api/admin/server-status", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["disk"]["available"] is False
