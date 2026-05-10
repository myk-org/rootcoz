"""Request-scoped logging context and shared log-directory setup.

Provides a ``job_id`` context variable and a logging filter that
automatically prepends ``[job_id=<value>]`` to log messages during
request processing.

Also provides :func:`get_log_file` — the single source of truth for
the application log file path, derived from the ``DB_PATH`` env var.
"""

import contextvars
import logging
import os
from pathlib import Path

job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")


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
    """
    log_dir = Path(os.getenv("DB_PATH", "/data/results.db")).parent / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        return str(log_dir / "rootcoz.log")
    except OSError:
        return None
