"""Tests for job_id logging context and the shared application log file."""

import asyncio
import io
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jenkins
import pytest

import rootcoz.main
from rootcoz import logging_context
from rootcoz.config import clear_db_settings_cache
from rootcoz.logging_context import JobIdFilter, get_log_file, job_id_var
from rootcoz.sources.jenkins_source import (
    JenkinsError,
    handle_jenkins_exception,
    wait_for_jenkins_completion,
)

pytestmark = pytest.mark.xdist_group("logging_context")


def _clear_file_logging_state() -> None:
    """Detach and close the shared file handler, leaving no file logging wired.

    Cannot re-create the server wiring: ``rootcoz.main`` is imported once at
    collection, so its module-level ``enable_log_file()`` never runs again.
    """
    handler = getattr(logging_context, "_FILE_HANDLER", None)
    known = getattr(logging_context, "_known_loggers", None)
    if handler is not None and known is not None:
        for known_logger in known():
            if handler in known_logger.handlers:
                known_logger.removeHandler(handler)
        handler.close()
    logging_context._ENABLED_LOG_FILE = None  # type: ignore[attr-defined]
    logging_context._FILE_HANDLER = None  # type: ignore[attr-defined]


def _require_plan_symbols() -> None:
    """Skip while enable_log_file() does not exist yet (RED guard at HEAD)."""
    if getattr(logging_context, "enable_log_file", None) is None:
        pytest.skip("enable_log_file() does not exist yet — RED guard")


def _enable() -> logging.Handler:
    """Enable file logging and return the shared handler."""
    _require_plan_symbols()
    handler = logging_context.enable_log_file()
    assert handler is not None
    return handler


def _file_text() -> str:
    path = get_log_file()
    assert path is not None
    return Path(path).read_text()


def _file_text_or_empty() -> str:
    """The log file so far, or "" when nothing has opened it yet."""
    path = get_log_file()
    return Path(path).read_text() if path and Path(path).exists() else ""


def _line_with(marker: str) -> str:
    return next(line for line in _file_text().splitlines() if marker in line)


def _console_handler(logger: logging.Logger) -> logging.Handler:
    return next(
        handler
        for handler in logger.handlers
        if not isinstance(handler, logging.FileHandler)
    )


def _from_logger_name(line: str) -> str:
    clean = rootcoz.main._ANSI_RE.sub("", line)
    return clean[clean.index("rootcoz.") :]


@pytest.fixture
def file_logging(tmp_path: Path, monkeypatch) -> Path:
    """Clear the wiring before and after, with DB_PATH pointed at tmp_path.

    The settings cache is cleared too: ``get_settings()`` is ``@lru_cache``d, so
    a test that changes an env var would otherwise read (and leak) a stale
    snapshot of the log-file settings.
    """
    _clear_file_logging_state()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "results.db"))
    clear_db_settings_cache()
    try:
        yield tmp_path
    finally:
        _clear_file_logging_state()
        clear_db_settings_cache()


class TestFileLoggingBaseline:
    """The defect itself: absent before the shared handler, present after it."""

    def test_a_child_logger_reaches_the_log_file_only_once_enabled(
        self, file_logging: Path
    ) -> None:
        """Without enable_log_file() a get_child_logger record stays off the file."""
        from rootcoz import get_child_logger

        before, after = "baseline-before-marker", "baseline-after-marker"

        # The measured state: rootcoz.storage 0, rootcoz.ai_client 0.
        get_child_logger("rootcoz.tests_child").warning(before)
        assert before not in _file_text_or_empty()

        _enable()
        get_child_logger("rootcoz.tests_child").warning(after)
        assert after in _file_text()


class TestJobIdFilter:
    def test_filter_prepends_job_id_when_set(self) -> None:
        """Filter prepends [job_id=...] to log messages when job_id is set."""
        filt = JobIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        job_id_var.set("abc-123")
        try:
            filt.filter(record)
            assert record.msg == "[job_id=abc-123] hello world"
        finally:
            job_id_var.set("")

    def test_filter_no_prefix_when_empty(self) -> None:
        """Filter does not modify messages when job_id is not set."""
        filt = JobIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        job_id_var.set("")
        filt.filter(record)
        assert record.msg == "hello world"

    def test_filter_always_returns_true(self) -> None:
        """Filter never suppresses records."""
        filt = JobIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        assert filt.filter(record) is True
        job_id_var.set("xyz")
        try:
            assert filt.filter(record) is True
        finally:
            job_id_var.set("")

    def test_filter_on_handler_works_with_named_logger(self, capfd) -> None:
        """When filter is on a handler, named loggers with propagate=False get the prefix."""
        filt = JobIdFilter()
        test_logger = logging.getLogger("test_jid_handler")
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False

        handler = logging.StreamHandler()
        handler.addFilter(filt)
        handler.setFormatter(logging.Formatter("%(message)s"))
        test_logger.addHandler(handler)

        try:
            job_id_var.set("job-42")
            test_logger.info("test message")
            captured = capfd.readouterr()
            assert "[job_id=job-42] test message" in captured.err
        finally:
            job_id_var.set("")
            test_logger.removeHandler(handler)


class TestGetLogFile:
    def test_creates_writable_log_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("DB_PATH", str(tmp_path / "results.db"))
        path = get_log_file()
        assert path == str(tmp_path / "logs" / "rootcoz.log")
        assert Path(path).is_file()

    def test_returns_none_when_log_dir_not_writable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("DB_PATH", str(tmp_path / "results.db"))
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_dir.chmod(0o555)
        try:
            assert get_log_file() is None
        finally:
            log_dir.chmod(0o755)


class TestLogLevelAuthority:
    def test_engine_core_debug_reaches_handler_with_debug_parent(self) -> None:
        """The rootcoz parent is the level authority, so DEBUG must reach
        rootcoz.engine.core's handler — no intermediate logger may cap it."""
        from rootcoz.engine.core import logger as engine_logger

        parent = logging.getLogger("rootcoz")
        previous_level = parent.level
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _Capture()
        capture.setLevel(logging.DEBUG)
        engine_logger.addHandler(capture)
        parent.setLevel(logging.DEBUG)
        try:
            engine_logger.debug("engine debug probe")
            levels = {
                name: logging.getLogger(name).getEffectiveLevel()
                for name in ("rootcoz", "rootcoz.engine", "rootcoz.engine.core")
            }
        finally:
            parent.setLevel(previous_level)
            engine_logger.removeHandler(capture)

        assert any(record.levelno == logging.DEBUG for record in records), (
            f"DEBUG record never reached a handler; effective levels={levels}"
        )


class TestFileLogging:
    """Every module's records reach the one shared file handler."""

    def test_a_pre_imported_module_writes_to_the_log_file(
        self, file_logging: Path
    ) -> None:
        """rootcoz.storage was imported long before enable() — the walk covers it."""
        _enable()
        from rootcoz import storage

        marker = "pre-imported-storage-marker"
        storage.logger.warning(marker)
        assert marker in _file_text()

    def test_a_lazily_created_child_logger_writes_to_the_log_file(
        self, file_logging: Path
    ) -> None:
        """A logger created after enable() receives the handler at creation."""
        handler = _enable()
        from rootcoz import get_child_logger

        lazy = get_child_logger("rootcoz.tests_lazy")
        assert lazy.handlers.count(handler) == 1
        marker = "lazy-child-logger-marker"
        lazy.warning(marker)
        assert marker in _file_text()

    def test_a_former_plain_logging_module_reaches_the_file_exactly_once(
        self, file_logging: Path
    ) -> None:
        """The three plain-logging modules write through one handler only."""
        handler = _enable()
        parent = logging.getLogger("rootcoz")
        for name in (
            "rootcoz.sources.base",
            "rootcoz.sources.chat_workspace",
            "rootcoz.xml_enrichment",
        ):
            marker = f"plain-logging-marker-{name.rsplit('.', 1)[-1]}"
            module_logger = logging.getLogger(name)
            module_logger.warning(marker)
            assert _file_text().count(marker) == 1, name
            holders = [
                held
                for chain_logger in (module_logger, parent, logging.root)
                for held in chain_logger.handlers
                if held is handler
            ]
            assert len(holders) == 1, name

    def test_one_file_handler_per_log_file(self, file_logging: Path) -> None:
        """No rootcoz logger holds any RotatingFileHandler but the shared one."""
        handler = _enable()
        for known_logger in logging_context._known_loggers():  # type: ignore[attr-defined]
            if known_logger.name != "rootcoz" and not known_logger.name.startswith(
                "rootcoz."
            ):
                continue
            for held in known_logger.handlers:
                if isinstance(held, logging.handlers.RotatingFileHandler):
                    assert held is handler, known_logger.name
                    assert known_logger.handlers.count(held) == 1
        for name in ("rootcoz.main", "rootcoz.engine.core", "rootcoz.storage"):
            assert handler in logging.getLogger(name).handlers, name

    def test_the_two_server_loggers_write_each_line_once(
        self, file_logging: Path
    ) -> None:
        """The two loggers that used to attach their own file handler do not duplicate."""
        _enable()
        main_marker = "server-main-marker-once"
        engine_marker = "server-engine-marker-once"
        logging.getLogger("rootcoz.main").warning(main_marker)
        logging.getLogger("rootcoz.engine.core").warning(engine_marker)
        assert _file_text().count(main_marker) == 1
        assert _file_text().count(engine_marker) == 1

    def test_a_child_line_is_written_once_to_the_file_and_once_to_the_console(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """One event ⇒ one file line and one console line, identical in shape."""
        _enable()
        from rootcoz import storage

        buffer = io.StringIO()
        monkeypatch.setattr(_console_handler(storage.logger), "stream", buffer)
        marker = "one-line-each-marker"
        storage.logger.warning(marker)
        assert _file_text().count(marker) == 1
        console_lines = [
            line for line in buffer.getvalue().splitlines() if marker in line
        ]
        assert len(console_lines) == 1
        assert _from_logger_name(_line_with(marker)) == _from_logger_name(
            console_lines[0]
        )

    def test_a_former_plain_logging_module_keeps_its_console_line(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """The plain-logging module's console line is in the app format."""
        _enable()
        module_logger = logging.getLogger("rootcoz.sources.base")
        buffer = io.StringIO()
        monkeypatch.setattr(_console_handler(module_logger), "stream", buffer)
        marker = "plain-console-marker"
        module_logger.warning(marker)
        assert " rootcoz.sources.base " in buffer.getvalue()
        assert marker in buffer.getvalue()

    def test_the_parent_level_reaches_the_former_plain_logging_module(
        self, file_logging: Path
    ) -> None:
        """Population 3 inherits the rootcoz parent level, including DB overrides."""
        from rootcoz.config import clear_db_settings_cache, update_db_settings_cache

        _enable()
        module_logger = logging.getLogger("rootcoz.sources.base")
        assert module_logger.level == logging.NOTSET
        parent = logging.getLogger("rootcoz")
        previous_level = parent.level
        parent.setLevel(logging.DEBUG)
        try:
            module_logger.debug("plain-module-debug-marker")
            assert "plain-module-debug-marker" in _file_text()
            update_db_settings_cache({"log_level": "ERROR"})
            try:
                rootcoz.main._apply_log_level_setting()
                assert module_logger.getEffectiveLevel() == logging.ERROR
            finally:
                clear_db_settings_cache()
        finally:
            parent.setLevel(previous_level)

    def test_the_job_id_prefix_is_written_to_the_file(self, file_logging: Path) -> None:
        """The shared handler carries JobIdFilter, so file lines keep the prefix."""
        _enable()
        rootcoz.main._install_job_id_filter()
        from rootcoz import storage

        marker = "job-id-file-marker"
        job_id_var.set("job-42")
        try:
            storage.logger.warning(marker)
        finally:
            job_id_var.set("")
        assert "[job_id=job-42]" in _line_with(marker)

    def test_no_file_handler_when_the_log_file_is_disabled_and_one_warning_is_logged(
        self, file_logging: Path, monkeypatch, caplog
    ) -> None:
        """An unwritable log dir degrades to console with exactly one warning."""
        _require_plan_symbols()
        _clear_file_logging_state()
        blocker = file_logging / "f"
        blocker.write_text("not a directory")
        monkeypatch.setenv("DB_PATH", str(blocker / "results.db"))
        with caplog.at_level(logging.WARNING):
            assert logging_context.enable_log_file() is None
        assert getattr(logging_context, "_FILE_HANDLER", None) is None
        from rootcoz import get_child_logger

        child = get_child_logger("rootcoz.tests_disabled")
        assert not any(
            isinstance(held, logging.handlers.RotatingFileHandler)
            for held in child.handlers
        )
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and record.name == "root"
        ]
        assert len(warnings) == 1
        assert str(blocker / "logs") in warnings[0].getMessage()

    def test_disabling_after_enabling_detaches_the_old_file(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """A re-pointed DB_PATH that is unwritable stops the old file, not keep it."""
        handler = _enable()
        from rootcoz import get_child_logger

        child = get_child_logger("rootcoz.tests_was_enabled")
        assert handler in child.handlers

        blocker = file_logging / "gone"
        blocker.write_text("not a directory")
        monkeypatch.setenv("DB_PATH", str(blocker / "results.db"))
        assert logging_context.enable_log_file() is None

        assert getattr(logging_context, "_FILE_HANDLER", None) is None
        assert handler not in child.handlers

    def test_an_enabled_logger_never_touches_the_path_again(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """get_child_logger short-circuits on the cached handler, no filesystem call."""
        handler = _enable()

        def _no_filesystem() -> str:
            raise AssertionError("get_log_file() called after enable")

        monkeypatch.setattr(logging_context, "get_log_file", _no_filesystem)
        from rootcoz import get_child_logger

        child = get_child_logger("rootcoz.tests_after_enable")
        assert handler in child.handlers
        assert logging_context.get_log_file_handler() is handler

    def test_a_logger_created_after_enable_still_gets_the_job_id_prefix(
        self, file_logging: Path
    ) -> None:
        """A late logger needs no filter of its own: the handler carries it."""
        _enable()
        rootcoz.main._install_job_id_filter()
        from rootcoz import get_child_logger

        late = get_child_logger("rootcoz.tests_late")
        marker = "late-logger-jid-marker"
        job_id_var.set("job-7")
        try:
            late.warning(marker)
        finally:
            job_id_var.set("")
        assert "[job_id=job-7]" in _line_with(marker)

    def test_only_enable_log_file_creates_a_file_handler(
        self, file_logging: Path
    ) -> None:
        """Handler creation happens in enable_log_file(), nowhere else."""
        _require_plan_symbols()
        _clear_file_logging_state()
        from rootcoz import get_child_logger

        def _handlers() -> set[int]:
            return {
                id(held)
                for known_logger in logging_context._known_loggers()  # type: ignore[attr-defined]
                for held in known_logger.handlers
                if isinstance(held, logging.handlers.RotatingFileHandler)
            }

        before = _handlers()
        get_child_logger("rootcoz.tests_no_enable")
        assert logging_context.get_log_file_handler() is None
        assert _handlers() == before

    async def test_the_sse_handler_still_serves_the_file_text(
        self, file_logging: Path
    ) -> None:
        """The admin Logs stream publishes the same text the file received."""
        handler = _enable()
        rootcoz.main._sse_log_handler = None
        rootcoz.main._install_sse_log_handler()
        sse_handler = rootcoz.main._sse_log_handler
        try:
            assert sse_handler is not None
            assert sse_handler.formatter is handler.formatter
            from rootcoz import storage

            marker = "sse-file-text-marker"
            async with rootcoz.main._log_broadcast.subscribe() as subscription:
                storage.logger.warning(marker)
                for _ in range(3):
                    await asyncio.sleep(0)
                payload = subscription.queue.get_nowait()
            assert payload == rootcoz.main._ANSI_RE.sub("", _line_with(marker))
        finally:
            for known_logger in logging_context._known_loggers():  # type: ignore[attr-defined]
                if sse_handler is not None and sse_handler in known_logger.handlers:
                    known_logger.removeHandler(sse_handler)
            rootcoz.main._sse_log_handler = None


# ---------------------------------------------------------------------------
# #481 — the log file's bound, its modes and its free-space floor
# ---------------------------------------------------------------------------


def _usage(total: int, free: int) -> SimpleNamespace:
    """A ``shutil.disk_usage``-shaped result with the given capacity."""
    return SimpleNamespace(total=total, used=total - free, free=free)


def _set_log_settings(monkeypatch, **values: int) -> None:
    """Set the log-file settings as env vars, dropping the cached snapshot."""
    for key, value in values.items():
        monkeypatch.setenv(key.upper(), str(value))
    clear_db_settings_cache()


class _RecordSink(logging.Handler):
    """Collect the records a module logger actually emitted."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _CaughtRecords:
    """Attach a sink to an existing module logger (propagate=False)."""

    def __init__(self, logger_name: str) -> None:
        self._logger = logging.getLogger(logger_name)
        self.sink = _RecordSink()

    def __enter__(self) -> _RecordSink:
        self._logger.addHandler(self.sink)
        return self.sink

    def __exit__(self, *exc_info: object) -> None:
        self._logger.removeHandler(self.sink)


class TestLogBudget:
    """The file's bound comes from the filesystem, never from a constant."""

    def test_derives_a_small_filesystem_budget(self) -> None:
        """A 1 GiB filesystem gets 1 % of it, split over 5 files."""
        max_bytes, backups = logging_context._derive_log_budget(1 << 30)
        assert (max_bytes, backups) == (2_147_483, 4)
        assert max_bytes * (backups + 1) <= (1 << 30) * 0.01

    def test_derives_a_large_filesystem_budget_at_todays_cap(self) -> None:
        """Above ~205 GiB the budget is today's 100 MiB x 21, never larger."""
        assert logging_context._derive_log_budget(220 << 30) == (104_857_600, 20)

    def test_the_fraction_below_the_cap_is_one_percent(self) -> None:
        """100 GiB still derives 1 % — it is not the cap."""
        assert logging_context._derive_log_budget(100 << 30) == (51_130_563, 20)

    def test_a_tiny_filesystem_still_rotates_within_the_whole_tree_bound(
        self,
    ) -> None:
        """The whole tree, not the per-file size, is the bound that never breaks."""
        capacity = 50 << 20
        max_bytes, backups = logging_context._derive_log_budget(capacity)
        assert max_bytes == 104_857
        assert max_bytes > 0
        assert max_bytes * (backups + 1) <= capacity * 0.01

    def test_the_floor_is_a_clamped_fraction(self) -> None:
        """The floor is 5 % of capacity, clamped to [32 MiB, 512 MiB]."""
        floor = logging_context._derive_min_free_bytes
        assert floor(1 << 30) == 53_687_091
        assert floor(5 << 30) == 268_435_456
        assert floor(100 << 30) == 536_870_912

    def test_the_handler_uses_the_derived_numbers(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """enable_log_file() sizes the handler from the data filesystem."""
        monkeypatch.setattr(
            logging_context.shutil, "disk_usage", lambda _p: _usage(1 << 30, 1 << 30)
        )
        handler = _enable()
        assert handler.maxBytes == 2_147_483
        assert handler.backupCount == 4
        floor = next(
            held for held in handler.filters if isinstance(held, logging.Filter)
        )
        assert floor.min_free_bytes == 53_687_091  # type: ignore[attr-defined]

    def test_explicit_settings_override_the_derivation(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """A set log_max_bytes/log_backup_count wins over the derived value."""
        monkeypatch.setattr(
            logging_context.shutil, "disk_usage", lambda _p: _usage(1 << 30, 1 << 30)
        )
        _set_log_settings(monkeypatch, log_max_bytes=1_000_000, log_backup_count=3)
        handler = _enable()
        assert handler.maxBytes == 1_000_000
        assert handler.backupCount == 3


class TestFreeSpaceFloor:
    """The file stops accepting records before the filesystem fills."""

    def test_a_record_below_the_floor_reaches_the_console_and_not_the_file(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """The floor drops file records only — the console keeps flowing."""
        _set_log_settings(monkeypatch, log_min_free_bytes=1 << 62)
        _enable()
        from rootcoz import get_child_logger

        child = get_child_logger("rootcoz.tests_floor")
        buffer = io.StringIO()
        monkeypatch.setattr(_console_handler(child), "stream", buffer)
        marker = "below-floor-marker"
        child.warning(marker)
        assert marker not in _file_text()
        assert marker in buffer.getvalue()

    def test_the_floor_warns_once_per_crossing(self, file_logging: Path) -> None:
        """One WARNING per crossing and one INFO on recovery, never per record."""
        floor_filter = logging_context._FreeSpaceFilter(
            file_logging, 1 << 62, probe_interval=0.0
        )
        # The announcements go through the rootcoz tree (console-carrying,
        # propagate=False), so they are read from that logger — not caplog.
        with _CaughtRecords("rootcoz.logging_context") as sink:
            assert [floor_filter.check() for _ in range(3)] == [False, False, False]
            warnings = [
                record for record in sink.records if record.levelno == logging.WARNING
            ]
            assert len(warnings) == 1
            assert "File logging paused" in warnings[0].getMessage()
            assert "log_min_free_bytes" in warnings[0].getMessage()
            floor_filter.min_free_bytes = 1
            assert floor_filter.check() is True
            infos = [
                record for record in sink.records if record.levelno == logging.INFO
            ]
            assert len(infos) == 1
            assert "resumed" in infos[0].getMessage()

    def test_a_failing_probe_keeps_logging(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """A probe that cannot stat the filesystem must never silence logging."""

        def _boom(_path: object) -> None:
            raise OSError("cannot stat the data directory")

        _set_log_settings(monkeypatch, log_min_free_bytes=1 << 62)
        monkeypatch.setattr(logging_context.shutil, "disk_usage", _boom)
        _enable()
        from rootcoz import get_child_logger

        marker = "failing-probe-marker"
        get_child_logger("rootcoz.tests_probe").warning(marker)
        assert marker in _file_text()


class TestLogFilePermissions:
    """Group-write is what survives a restart under a different UID."""

    def test_the_log_dir_and_file_are_group_writable_under_a_hostile_umask(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Creation sets 0770/0660 even when the umask asks for 0600."""
        monkeypatch.setenv("DB_PATH", str(tmp_path / "results.db"))
        previous = os.umask(0o077)
        try:
            path = get_log_file()
        finally:
            os.umask(previous)
        assert path is not None
        log_dir = (tmp_path / "logs").stat().st_mode & 0o777
        log_file = Path(path).stat().st_mode & 0o777
        assert log_dir == 0o770
        assert log_file == 0o660

    def test_a_symlinked_log_path_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        """A planted symlink must not make us append to, and widen, a private file."""
        victim = tmp_path / "victim"
        victim.write_text("secret\n")
        victim.chmod(0o600)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "rootcoz.log").symlink_to(victim)
        monkeypatch.setenv("DB_PATH", str(tmp_path / "results.db"))
        assert get_log_file() is None
        assert victim.read_text() == "secret\n"
        assert victim.stat().st_mode & 0o777 == 0o600

    def test_the_rotated_file_keeps_the_mode(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """A rollover re-creates the file 0660 — the mode the deployment needs."""
        _set_log_settings(monkeypatch, log_max_bytes=1024, log_backup_count=2)
        previous = os.umask(0o022)
        try:
            handler = _enable()
            for index in range(40):
                handler.emit(
                    logging.LogRecord(
                        name="rootcoz.tests_rotate",
                        level=logging.WARNING,
                        pathname="",
                        lineno=0,
                        msg="rotation-filler-%d %s",  # type: ignore[arg-type]
                        args=(index, "x" * 100),
                        exc_info=None,
                    )
                )
        finally:
            os.umask(previous)
        log_dir = file_logging / "logs"
        assert (log_dir / "rootcoz.log.1").exists()
        assert (log_dir / "rootcoz.log").stat().st_mode & 0o777 == 0o660


class TestLogFileReconfigure:
    """A settings change applies live, without dropping a #477 invariant."""

    def test_reconfigure_applies_a_new_budget(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """reconfigure=True rebuilds the one handler with the new numbers."""
        monkeypatch.setattr(
            logging_context.shutil, "disk_usage", lambda _p: _usage(1 << 30, 1 << 30)
        )
        before = _enable()
        _set_log_settings(monkeypatch, log_max_bytes=2_000_000)
        after = logging_context.enable_log_file(reconfigure=True)
        assert after is not None
        assert after is not before
        assert after.maxBytes == 2_000_000
        holders = [
            held
            for known_logger in logging_context._known_loggers()  # type: ignore[attr-defined]
            for held in known_logger.handlers
            if isinstance(held, logging.handlers.RotatingFileHandler)
        ]
        assert {id(held) for held in holders} == {id(after)}

    def test_a_lower_budget_prunes_the_leftovers_of_a_higher_one(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """Rotation only manages the first N suffixes — the rest must go."""
        log_dir = file_logging / "logs"
        log_dir.mkdir(exist_ok=True)
        for suffix in (1, 2, 9, 21):
            (log_dir / f"rootcoz.log.{suffix}").write_text("old\n")
        _set_log_settings(monkeypatch, log_backup_count=2)
        assert _enable() is not None
        assert sorted(p.name for p in log_dir.glob("rootcoz.log.*")) == [
            "rootcoz.log.1",
            "rootcoz.log.2",
        ]

    def test_a_handler_that_cannot_open_does_not_raise(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """A settings save must survive the file becoming unwritable mid-flight."""
        _enable()

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr(logging_context, "_GroupWritableRotatingFileHandler", _boom)
        assert logging_context.enable_log_file(reconfigure=True) is None
        assert logging_context.get_log_file_handler() is None

    def test_the_job_id_prefix_survives_a_reconfigure(self, file_logging: Path) -> None:
        """The rebuilt handler still carries JobIdFilter."""
        _enable()
        rootcoz.main._apply_log_file_settings()
        from rootcoz import storage

        marker = "reconfigured-jid-marker"
        job_id_var.set("job-reconf")
        try:
            storage.logger.warning(marker)
        finally:
            job_id_var.set("")
        assert "[job_id=job-reconf]" in _line_with(marker)

    def test_the_three_settings_are_server_settings_applied_at_runtime(self) -> None:
        """Server Settings metadata + labels for the live-applied keys (#481)."""
        metadata = {
            entry["key"]: entry for entry in rootcoz.main._get_settings_metadata()
        }
        assert rootcoz.main._LOG_FILE_SETTINGS == {
            "log_max_bytes",
            "log_backup_count",
            "log_min_free_bytes",
        }
        for key in sorted(rootcoz.main._LOG_FILE_SETTINGS):
            entry = metadata[key]
            assert entry["category"] == "Server", key
            assert entry["type"] == "integer", key
            assert entry["restart_required"] is False, key
            assert entry["default"] == "0", key
            assert entry["label"] != key and entry["description"], key

    def test_the_job_id_prefix_still_reaches_the_console_when_file_logging_is_off(
        self, file_logging: Path, monkeypatch
    ) -> None:
        """An unwritable data dir must not cost the console its [job_id=…]."""
        from rootcoz import get_child_logger

        # Built before the sweep, like any module logger: _install_job_id_filter
        # only reaches handlers that already exist.
        child = get_child_logger("rootcoz.tests_console_only")
        buffer = io.StringIO()
        monkeypatch.setattr(_console_handler(child), "stream", buffer)
        for known_logger in logging_context._known_loggers():  # type: ignore[attr-defined]
            for held in known_logger.handlers:
                while rootcoz.main._job_id_filter in held.filters:
                    held.removeFilter(rootcoz.main._job_id_filter)
        read_only = file_logging / "read-only"
        read_only.mkdir()
        read_only.chmod(0o555)
        monkeypatch.setenv("DB_PATH", str(read_only / "results.db"))
        try:
            rootcoz.main._apply_log_file_settings()
            assert logging_context.get_log_file_handler() is None
            job_id_var.set("job-console")
            try:
                child.warning("console-only-marker")
            finally:
                job_id_var.set("")
        finally:
            read_only.chmod(0o755)
        assert "[job_id=job-console]" in buffer.getvalue()


class TestRedaction:
    """The call sites that feed the shared log file must not render credentials."""

    _USERINFO_URL = (
        "https://ci-user:s3cr3t@jenkins.example.com/"  # pragma: allowlist secret
    )
    _TOKENISED = f"Error communicating with server[{_USERINFO_URL}]"

    def test_a_generic_jenkins_exception_does_not_leak_credentials(self) -> None:
        """handle_jenkins_exception's message keeps host, drops userinfo."""
        with pytest.raises(JenkinsError) as raised:
            handle_jenkins_exception(
                jenkins.JenkinsException(self._TOKENISED), "my-job", 7
            )
        message = str(raised.value)
        assert "jenkins.example.com" in message
        assert "ci-user" not in message
        assert "s3cr3t" not in message

    def test_the_unreachable_log_does_not_leak_credentials(self) -> None:
        """The connectivity log line keeps the host and drops the userinfo."""
        error = OSError(f"Connection refused for {self._TOKENISED}")
        with (
            _CaughtRecords("rootcoz.sources.jenkins_source") as sink,
            pytest.raises(JenkinsError),
        ):
            handle_jenkins_exception(error, "my-job", 7)
        lines = [record.getMessage() for record in sink.records]
        assert any("jenkins.example.com" in line for line in lines)
        assert not any("s3cr3t" in line or "ci-user" in line for line in lines)

    async def test_the_status_poll_logs_the_type_not_the_text(self) -> None:
        """A non-transient poll failure logs the type name and no traceback."""
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls:
            mock_cls.return_value.get_build_info_safe.side_effect = (
                jenkins.JenkinsException(self._TOKENISED)
            )
            with _CaughtRecords("rootcoz.sources.jenkins_source") as sink:
                result, _error = await wait_for_jenkins_completion(
                    jenkins_url="https://jenkins.example.com",
                    job_name="my-job",
                    build_number=1,
                    jenkins_user="user",
                    jenkins_password="",
                    jenkins_ssl_verify=True,
                    poll_interval_minutes=1,
                    max_wait_minutes=5,
                )
        assert result is False
        failures = [
            record
            for record in sink.records
            if record.levelno == logging.ERROR
            and "JenkinsException" in record.getMessage()
        ]
        assert len(failures) == 1
        assert "s3cr3t" not in failures[0].getMessage()
        assert failures[0].exc_info is None

    async def test_the_preflight_log_redacts_the_jenkins_url(self) -> None:
        """The preflight branch interpolates the URL — the userinfo must not leak."""
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls:
            mock_cls.return_value.get_whoami.side_effect = OSError("timed out")
            with _CaughtRecords("rootcoz.sources.jenkins_source") as sink:
                result, _error = await wait_for_jenkins_completion(
                    jenkins_url=self._USERINFO_URL,
                    job_name="my-job",
                    build_number=1,
                    jenkins_user="user",
                    jenkins_password="",
                    jenkins_ssl_verify=True,
                    poll_interval_minutes=1,
                    max_wait_minutes=5,
                )
        assert result is False
        preflight = [
            record
            for record in sink.records
            if "Cannot reach Jenkins at" in record.getMessage()
        ]
        assert len(preflight) == 1
        line = preflight[0].getMessage()
        assert "jenkins.example.com" in line
        assert "ci-user" not in line
        assert "s3cr3t" not in line

    async def test_the_exhaustion_log_redacts_the_jenkins_url(self) -> None:
        """The consecutive-failures branch interpolates the URL too."""
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls:
            mock_cls.return_value.get_build_info_safe.side_effect = OSError(
                "connection refused"
            )
            with _CaughtRecords("rootcoz.sources.jenkins_source") as sink:
                result, _error = await wait_for_jenkins_completion(
                    jenkins_url=self._USERINFO_URL,
                    job_name="my-job",
                    build_number=1,
                    jenkins_user="user",
                    jenkins_password="",
                    jenkins_ssl_verify=True,
                    poll_interval_minutes=1,
                    max_wait_minutes=5,
                    max_consecutive_failures=1,
                )
        assert result is False
        exhaustion = [
            record
            for record in sink.records
            if "Cannot reach Jenkins at" in record.getMessage()
        ]
        assert len(exhaustion) == 1
        line = exhaustion[0].getMessage()
        assert "jenkins.example.com" in line
        assert "ci-user" not in line
        assert "s3cr3t" not in line

    def test_an_invalid_repo_url_reports_the_url_without_credentials(self) -> None:
        """The ValueError keeps the scheme prefix and drops userinfo and query."""
        from rootcoz.repository import _validate_repo_url

        with pytest.raises(ValueError) as raised:
            _validate_repo_url("ftp://u:p@example.com/x?access_token=s")
        message = str(raised.value)
        assert message.startswith("Invalid repository URL scheme.")
        assert "ftp://example.com/x" in message
        assert "u:p" not in message
        assert "access_token" not in message
