"""rootcoz - AI-powered Jenkins failure analysis."""

from __future__ import annotations

import logging
import os

__version__ = "4.14.1"

# The `rootcoz` parent logger is the single log-level authority: module
# loggers are created NOTSET (see get_child_logger) and inherit this level
# dynamically — at import time and later. A DB log_level override applied
# by main._apply_log_level_setting therefore reaches modules imported after
# it was applied, instead of only the loggers that existed at startup.
logging.getLogger("rootcoz").setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def get_child_logger(name: str) -> logging.Logger:
    """A simple_logger module logger that inherits the rootcoz parent level.

    simple_logger pins an explicit level on every logger it creates, so the
    historical ``get_logger(name=__name__, level=os.environ.get(...))``
    pattern froze each module at its import-time env snapshot. NOTSET hands
    level authority to the ``rootcoz`` parent logger instead.

    Handlers are simple_logger's console handler plus — in the server, once
    ``rootcoz.main`` enabled it — the single shared application file handler
    (``logging_context.enable_log_file``), so every module's records reach the
    same log file. ``propagate`` stays False: duplication is prevented by the
    one shared handler instance, not by bubbling.

    simple_logger's duplicate filter is swapped for
    ``logging_context.RootcozDuplicateFilter`` here, the one place every module
    logger is created, so an audit record (``dedup_exempt``) is never collapsed
    while ordinary repeats still are (#489).
    """
    from simple_logger.logger import get_logger

    from rootcoz.logging_context import (
        get_log_file_handler,
        install_dedup_exempt_filter,
    )

    child = get_logger(name=name)
    child.setLevel(logging.NOTSET)
    install_dedup_exempt_filter(child)
    handler = get_log_file_handler()
    if handler is not None and handler not in child.handlers:
        child.addHandler(handler)
    return child
