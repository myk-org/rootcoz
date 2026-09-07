"""Tests for job_id logging context."""

import logging
from pathlib import Path

from rootcoz.logging_context import JobIdFilter, get_log_file, job_id_var


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
