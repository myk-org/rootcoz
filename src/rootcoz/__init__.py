"""rootcoz - AI-powered Jenkins failure analysis."""

from __future__ import annotations

import logging
import os

__version__ = "4.11.0"

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
    level authority to the ``rootcoz`` parent logger instead; handlers,
    formatting and propagate=False stay exactly as simple_logger builds
    them.
    """
    from simple_logger.logger import get_logger

    child = get_logger(name=name)
    child.setLevel(logging.NOTSET)
    return child
