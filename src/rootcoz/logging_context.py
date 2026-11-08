"""Request-scoped logging context, shared log file and log-directory setup.

Provides a ``job_id`` context variable and a logging filter that
automatically prepends ``[job_id=<value>]`` to log messages during
request processing.

Also provides :func:`get_log_file` — the single source of truth for
the application log file path, derived from the ``DB_PATH`` env var —
and :func:`enable_log_file`, which creates the one shared file handler the
whole application writes through (see ``rootcoz.get_child_logger``).

The module is stdlib-only at import time: ``simple_logger`` is imported
inside :func:`enable_log_file`, so importing this module never calls
``logging.setLoggerClass(SimpleLogger)`` behind ``rootcoz/__init__.py``'s
level-authority parent.
"""

import contextvars
import logging
import os
from pathlib import Path

job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")

# The server-enabled application log file. ``_ENABLED_LOG_FILE`` is the cache
# key: when it is set, ``_FILE_HANDLER`` is the one handler every module logger
# writes through. Both stay ``None`` in the CLI and in tests until
# ``enable_log_file()`` runs.
_ENABLED_LOG_FILE: str | None = None
_FILE_HANDLER: logging.Handler | None = None


class JobIdFilter(logging.Filter):
    """Logging filter that prepends ``[job_id=<value>]`` to log messages.

    When ``job_id`` is set in the current context, prepends the prefix.
    When empty (e.g. startup, health checks), the message is unchanged.
    Guards against duplicate prefixes when multiple handlers share this filter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        jid = job_id_var.get("")
        if jid:
            prefix = f"[job_id={jid}] "
            if not isinstance(record.msg, str) or not record.msg.startswith(prefix):
                record.msg = f"{prefix}{record.msg}"
        return True


def get_log_file() -> str | None:
    """Return the application log file path, or ``None`` on failure.

    The log directory is derived from the ``DB_PATH`` environment variable
    (default ``/data/results.db``) by placing a ``logs/`` directory next
    to the database file.  The directory is created if it does not exist.
    Returns ``None`` if the directory or log file cannot be written (for
    example a bind-mounted ``/data`` owned by another user).
    """
    log_dir = Path(os.getenv("DB_PATH", "/data/results.db")).parent / "logs"
    log_path = log_dir / "rootcoz.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8"):
            pass
        return str(log_path)
    except OSError:
        return None


def _known_loggers() -> list[logging.Logger]:
    """Every registered logger, plus the root — never a loggerDict PlaceHolder.

    A bare ``logging.getLogger(placeholder_name)`` builds a real logger, and
    simple_logger's logger class defaults to INFO. That newly built logger
    then parents every child of that name (``rootcoz.engine`` capping
    ``rootcoz.engine.core``), silently overriding the ``rootcoz`` parent
    level that is the single level authority (#347).
    """
    return [
        logging.root,
        *(
            obj
            for obj in logging.Logger.manager.loggerDict.values()
            if isinstance(obj, logging.Logger)
        ),
    ]


def get_log_file_handler() -> logging.Handler | None:
    """The shared file handler, or ``None`` when file logging is disabled.

    A pure lookup: it never probes the filesystem, so importing a module (or a
    CLI run) cannot create, open or rotate the log file.
    """
    return _FILE_HANDLER if _ENABLED_LOG_FILE is not None else None


def _warn_file_logging_disabled(log_dir: Path, reason: str) -> None:
    """Say once, on the root logger, why the file sink is off.

    Not on a ``rootcoz`` logger: that tree's level is pinned from ``LOG_LEVEL``
    at import, so a ``LOG_LEVEL=ERROR`` deployment would filter the very message
    explaining the missing log file. ``logging.lastResort`` prints a root-level
    WARNING to stderr regardless.
    """
    logging.getLogger().warning("File logging disabled: %s (%s)", log_dir, reason)


def enable_log_file() -> logging.Handler | None:
    """Create and attach the one shared application log file handler.

    Called by ``rootcoz.main`` at import (server only) — never by the CLI, and
    it opens the file exactly once per process. Returns ``None`` when the log
    file is unwritable, leaving the console sink intact.
    """
    global _ENABLED_LOG_FILE, _FILE_HANDLER

    log_dir = Path(os.getenv("DB_PATH", "/data/results.db")).parent / "logs"
    path = get_log_file()
    if path is None:
        _warn_file_logging_disabled(log_dir, "log directory is not writable")
        return None
    if path == _ENABLED_LOG_FILE and _FILE_HANDLER is not None:
        return _FILE_HANDLER

    # A different path must not leave the old descriptor attached: a re-pointed
    # DB_PATH (tests, a restart with a new data dir) would otherwise accumulate.
    if _FILE_HANDLER is not None:
        for known_logger in _known_loggers():
            if _FILE_HANDLER in known_logger.handlers:
                known_logger.removeHandler(_FILE_HANDLER)
        _FILE_HANDLER.close()
    _ENABLED_LOG_FILE = None
    _FILE_HANDLER = None

    try:
        from logging.handlers import RotatingFileHandler

        from simple_logger.logger import LOGGERS, get_logger

        # Build the handler through simple_logger so rotation size, backup count
        # and the formatter stay its single source of truth: the file keeps
        # exactly the console line shape (no second formatting pipeline).
        scaffold_name = "rootcoz._log_file_only"
        scaffold = get_logger(
            name=scaffold_name, level=logging.NOTSET, filename=path, console=False
        )
        handler = next(
            held for held in scaffold.handlers if isinstance(held, RotatingFileHandler)
        )
        scaffold.removeHandler(handler)
        LOGGERS.pop(scaffold_name, None)
        logging.Logger.manager.loggerDict.pop(scaffold_name, None)
    except OSError as exc:
        _warn_file_logging_disabled(log_dir, str(exc))
        return None

    # Level authority stays with the ``rootcoz`` parent logger (#347).
    handler.setLevel(logging.NOTSET)
    for known_logger in _known_loggers():
        name = known_logger.name
        if name == "rootcoz" or not name.startswith("rootcoz."):
            # The rootcoz parent propagates; a handler there would emit twice,
            # and third-party loggers stay on stdout.
            continue
        if handler not in known_logger.handlers:
            known_logger.addHandler(handler)

    _ENABLED_LOG_FILE = path
    _FILE_HANDLER = handler
    return handler
