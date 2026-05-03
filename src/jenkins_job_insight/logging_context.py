"""Request-scoped logging context using contextvars.

Provides a ``job_id`` context variable and a logging filter that
automatically prepends ``[job_id=<value>]`` to log messages during
request processing.
"""

import contextvars
import logging

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
