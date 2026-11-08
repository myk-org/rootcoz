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

from __future__ import annotations

import contextvars
import logging
import os
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from io import TextIOWrapper
    from typing import Any

job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")

# The server-enabled application log file. ``_ENABLED_LOG_FILE`` is the cache
# key: when it is set, ``_FILE_HANDLER`` is the one handler every module logger
# writes through. Both stay ``None`` in the CLI and in tests until
# ``enable_log_file()`` runs.
_ENABLED_LOG_FILE: str | None = None
_FILE_HANDLER: logging.Handler | None = None

# The file's budget is derived from the filesystem holding the data directory
# (never a volume size as a constant): the whole log tree stays inside 1 % of
# its capacity, capped at simple_logger's historical defaults — 100 MiB per
# file x 21 files, i.e. today's 2.05 GiB (#481). A budget derived from *free*
# space would rotate the history away exactly when the filesystem fills, which
# is the evidence the fill is diagnosed with.
_LOG_BUDGET_FRACTION = 0.01
_LOG_MAX_BYTES_CAP = 104_857_600  # simple_logger's per-file default
_LOG_BACKUP_COUNT_CAP = 20  # simple_logger's default
_LOG_BACKUP_COUNT_MIN = 4
_LOG_FILE_TARGET_BYTES = 8_388_608  # aim the split at 8 MiB files
_LOG_MAX_BYTES_MIN = 262_144  # a tiny filesystem still rotates

# The free-space floor stops the file before the filesystem fails SQLite writes
# on the shared data directory; the clamps keep a small filesystem's floor
# meaningful and a huge one's out of the gigabytes.
_LOG_MIN_FREE_FRACTION = 0.05
_LOG_MIN_FREE_MIN = 33_554_432  # 32 MiB
_LOG_MIN_FREE_MAX = 536_870_912  # 512 MiB

# Group-write is the only permission that survives a restart under a different
# UID (OpenShift's random UID, image GID 0).
_LOG_DIR_MODE = 0o770
_LOG_FILE_MODE = 0o660

# A statvfs per record is a measurable cost on a busy DEBUG log; 30 s of extra
# writes cannot fill a 5 % reservoir.
_FREE_SPACE_PROBE_SECONDS = 30.0


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


def _debug(message: str, *args: object) -> None:
    """DEBUG through the rootcoz tree — simple_logger is imported lazily."""
    from rootcoz import get_child_logger

    get_child_logger(__name__).debug(message, *args)


class _GroupWritableRotatingFileHandler(RotatingFileHandler):
    """A rotating file handler whose file is created 0660, every open.

    ``RotatingFileHandler`` accepts no mode: it re-creates the file with
    ``0666 & ~umask`` after every rollover, which drops the group-write the
    deployment needs. Routing creation through :func:`_open_mode` — whose
    ``fchmod`` is what actually lands 0660 over the umask — keeps the
    guarantee beyond the first rollover.
    """

    def _open(self) -> TextIOWrapper[Any]:
        fd = _open_mode(Path(self.baseFilename), _LOG_FILE_MODE)
        return cast(
            "TextIOWrapper[Any]",
            os.fdopen(fd, self.mode or "a", encoding=self.encoding, errors=self.errors),
        )


def _open_mode(path: Path, mode: int) -> int:
    """Open *path* for appending and force *mode* on it; returns the fd.

    ``os.open``'s mode argument is masked by the process umask (0660 & ~0022
    == 0640), so the ``fchmod`` is what actually lands the mode. A file owned
    by another identity cannot be chmod-ed — it was writable enough to open,
    so that stays a DEBUG line, never a reason to lose logging.
    """
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, mode)
    try:
        os.fchmod(fd, mode)
    except OSError:
        _debug("Cannot set mode %o on %s", mode, path)
    return fd


def _apply_mode(path: Path, mode: int) -> None:
    """chmod a directory we just created; a failure is never fatal."""
    try:
        os.chmod(path, mode)
    except OSError:
        _debug("Cannot set mode %o on %s", mode, path)


def _derive_log_budget(capacity: int) -> tuple[int, int]:
    """The (max_bytes, backup_count) budget for a filesystem of *capacity* bytes."""
    budget = min(
        int(capacity * _LOG_BUDGET_FRACTION),
        _LOG_MAX_BYTES_CAP * (_LOG_BACKUP_COUNT_CAP + 1),
    )
    backups = max(
        _LOG_BACKUP_COUNT_MIN,
        min(_LOG_BACKUP_COUNT_CAP, budget // _LOG_FILE_TARGET_BYTES),
    )
    max_bytes = max(
        _LOG_MAX_BYTES_MIN, min(_LOG_MAX_BYTES_CAP, budget // (backups + 1))
    )
    return max_bytes, backups


def _derive_min_free_bytes(capacity: int) -> int:
    """The free-space floor for a filesystem of *capacity* bytes."""
    return min(
        _LOG_MIN_FREE_MAX,
        max(_LOG_MIN_FREE_MIN, int(capacity * _LOG_MIN_FREE_FRACTION)),
    )


def _resolve_log_file_limits(
    log_dir: Path,
) -> tuple[int, int, int, list[str]]:
    """The effective (max_bytes, backup_count, min_free_bytes, explicit) for this run.

    Capacity comes from the data directory, never from ``logs/``: a persistent
    volume's subdirectory can be a different (or not yet existing) filesystem.
    ``0`` in a setting means "derive", and an unreadable filesystem falls back
    to today's defaults with no floor — the next reconfigure re-derives.
    """
    try:
        capacity = shutil.disk_usage(log_dir.parent).total
        max_bytes, backups = _derive_log_budget(capacity)
        min_free = _derive_min_free_bytes(capacity)
    except OSError:
        max_bytes, backups, min_free = _LOG_MAX_BYTES_CAP, _LOG_BACKUP_COUNT_CAP, 0

    try:
        from rootcoz.config import get_settings

        settings = get_settings()
    except ValueError:  # pydantic ValidationError
        # main.py runs this from its module body: an invalid env must surface as
        # the startup report it already is, never as an import crash.
        _debug("Cannot read the log-file settings; using the derived defaults")
        return max_bytes, backups, min_free, []
    explicit = [
        name
        for name, value in (
            ("log_max_bytes", settings.log_max_bytes),
            ("log_backup_count", settings.log_backup_count),
            ("log_min_free_bytes", settings.log_min_free_bytes),
        )
        if value
    ]
    return (
        settings.log_max_bytes or max_bytes,
        settings.log_backup_count or backups,
        settings.log_min_free_bytes or min_free,
        explicit,
    )


class _FreeSpaceFilter(logging.Filter):
    """Drop file records while the data filesystem is below its floor.

    Not on the console handler: the pause WARNING and the resume INFO go to the
    root logger so they survive both the drop and a ``LOG_LEVEL=ERROR`` tree,
    and console records keep flowing throughout. A probe that cannot stat the
    filesystem fails open — a metric that fails must never silence logging.
    """

    def __init__(
        self,
        log_dir: Path,
        min_free_bytes: int,
        *,
        probe_interval: float = _FREE_SPACE_PROBE_SECONDS,
    ) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.min_free_bytes = min_free_bytes
        self.probe_interval = probe_interval
        self._free: int | None = None
        self._probe_after: float | None = None
        self._blocked = False

    def _free_bytes(self) -> int | None:
        """Cached free bytes on the filesystem holding the log directory."""
        now = time.monotonic()
        if self._probe_after is not None and now < self._probe_after:
            return self._free
        self._probe_after = now + self.probe_interval
        try:
            self._free = shutil.disk_usage(self.log_dir).free
        except OSError:
            self._free = None
        return self._free

    def check(self) -> bool:
        """Whether the file may accept a record, announcing each crossing once."""
        free = self._free_bytes()
        if free is None:
            # Fail open, and say nothing about free space we do not have.
            if self._blocked:
                self._blocked = False
                _debug(
                    "Free-space probe failed for %s; file logging resumed", self.log_dir
                )
            return True
        if free >= self.min_free_bytes:
            if self._blocked:
                self._blocked = False
                logging.getLogger().info(
                    "File logging resumed: %s has %s bytes free, back above the "
                    "%d-byte floor (log_min_free_bytes)",
                    self.log_dir,
                    free,
                    self.min_free_bytes,
                )
            return True
        if not self._blocked:
            self._blocked = True
            logging.getLogger().warning(
                "File logging paused: %s has %d bytes free, below the %d-byte "
                "floor (log_min_free_bytes); console logging continues",
                self.log_dir,
                free,
                self.min_free_bytes,
            )
        return False

    def filter(self, record: logging.LogRecord) -> bool:
        return self.check()


def get_log_file() -> str | None:
    """Return the application log file path, or ``None`` on failure.

    The log directory is derived from the ``DB_PATH`` environment variable
    (default ``/data/results.db``) by placing a ``logs/`` directory next
    to the database file. The directory is created if it does not exist, and
    the file is opened and closed as a writability probe. Both are created
    with the modes the deployment needs (0770/0660) rather than whatever the
    umask leaves, so a restart under a different UID still writes them.
    Returns ``None`` if the directory or log file cannot be written (for
    example a bind-mounted data directory owned by another user).
    """
    log_dir = Path(os.getenv("DB_PATH", "/data/results.db")).parent / "logs"
    log_path = log_dir / "rootcoz.log"
    try:
        created_dir = not log_dir.exists()
        log_dir.mkdir(parents=True, exist_ok=True, mode=_LOG_DIR_MODE)
        if created_dir:
            # mkdir's mode is masked by the umask; an operator's existing mode
            # is left alone — widening a directory they tightened would be rude.
            _apply_mode(log_dir, _LOG_DIR_MODE)
        os.close(_open_mode(log_path, _LOG_FILE_MODE))
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


def enable_log_file(*, reconfigure: bool = False) -> logging.Handler | None:
    """Create and attach the one shared application log file handler.

    Called by ``rootcoz.main`` at import (server only) — never by the CLI, and
    it opens the file exactly once per process. ``reconfigure=True`` rebuilds
    it even when the path is unchanged, so the three log-file settings apply at
    runtime instead of on the next restart. Returns ``None`` when the log file
    is unwritable, leaving the console sink intact.
    """
    global _ENABLED_LOG_FILE, _FILE_HANDLER

    log_dir = Path(os.getenv("DB_PATH", "/data/results.db")).parent / "logs"
    path = get_log_file()
    if path == _ENABLED_LOG_FILE and _FILE_HANDLER is not None and not reconfigure:
        return _FILE_HANDLER

    # A different path — or no file at all — must not leave the old descriptor
    # attached: a re-pointed DB_PATH (tests, a restart with a new data dir)
    # would otherwise accumulate, and a now-disabled sink would keep writing to
    # the old file while the WARNING says it is off (requirement 6).
    if _FILE_HANDLER is not None:
        for known_logger in _known_loggers():
            if _FILE_HANDLER in known_logger.handlers:
                known_logger.removeHandler(_FILE_HANDLER)
        _FILE_HANDLER.close()
    _ENABLED_LOG_FILE = None
    _FILE_HANDLER = None

    if path is None:
        _warn_file_logging_disabled(log_dir, "log directory is not writable")
        return None

    max_bytes, backups, min_free, explicit = _resolve_log_file_limits(log_dir)

    try:
        from simple_logger.logger import LOGGERS, get_logger

        # Build the handler through simple_logger so rotation size, backup count
        # and the formatter stay its single source of truth: the file keeps
        # exactly the console line shape (no second formatting pipeline).
        scaffold_name = "rootcoz._log_file_only"
        scaffold = get_logger(
            name=scaffold_name,
            level=logging.NOTSET,
            filename=path,
            console=False,
            file_max_bytes=max_bytes,
            file_backup_count=backups,
        )
        built = next(
            held for held in scaffold.handlers if isinstance(held, RotatingFileHandler)
        )
        scaffold.removeHandler(built)
        # The scaffold only contributed its formatter: its own descriptor closes
        # here so exactly one handler (below) holds an open file.
        built.close()
        LOGGERS.pop(scaffold_name, None)
        logging.Logger.manager.loggerDict.pop(scaffold_name, None)
    except OSError as exc:
        _warn_file_logging_disabled(log_dir, str(exc))
        return None

    handler = _GroupWritableRotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding=built.encoding,
    )
    handler.setFormatter(built.formatter)
    floor = _FreeSpaceFilter(log_dir, min_free)
    handler.addFilter(floor)
    # Say so now, not at the first record: a deployment that boots on a nearly
    # full filesystem should learn it while someone is watching.
    floor.check()

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
    # Through the rootcoz tree, not the root logger: root has no handler here,
    # so an INFO there would be dropped.
    _log_file_budget(path, max_bytes, backups, min_free, explicit)
    return handler


def _log_file_budget(
    path: str,
    max_bytes: int,
    backups: int,
    min_free: int,
    explicit: list[str],
) -> None:
    """Log the effective budget once, naming what was derived (#481)."""
    from rootcoz import get_child_logger

    get_child_logger("rootcoz.logging_context").info(
        "Application log file at %s: max_bytes=%d, backup_count=%d, "
        "min_free_bytes=%d, explicit=%s",
        path,
        max_bytes,
        backups,
        min_free,
        ",".join(explicit) or "none (all derived)",
    )
