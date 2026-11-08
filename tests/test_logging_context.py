"""Tests for job_id logging context and the shared application log file."""

import asyncio
import io
import logging
from pathlib import Path

import pytest

import rootcoz.main
from rootcoz import logging_context
from rootcoz.logging_context import JobIdFilter, get_log_file, job_id_var

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
    """Clear the wiring before and after, with DB_PATH pointed at tmp_path."""
    _clear_file_logging_state()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "results.db"))
    try:
        yield tmp_path
    finally:
        _clear_file_logging_state()


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
