"""Shared utilities for rootcoz."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import jenkins
import requests.exceptions

from rootcoz.encryption import SENSITIVE_KEYS

#: Combined tuple of exception types that indicate a transient Jenkins
#: connectivity problem (network outage, DNS failure, timeout, etc.).
#: Used by the pre-flight check and analysis logic in
#: *sources/jenkins_source.py*.
#:
#: Note: ``jenkins.JenkinsException`` is intentionally excluded — it is
#: the base class for many non-transient errors (auth failures, 5xx,
#: malformed responses).  Only ``jenkins.TimeoutException`` (a subclass)
#: represents a true connectivity/timeout problem.
JENKINS_CONNECTIVITY_EXCEPTIONS: tuple[type[Exception], ...] = (
    OSError,
    TimeoutError,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    jenkins.TimeoutException,
)


def is_jenkins_connectivity_error(exc: Exception) -> bool:
    """Return ``True`` if *exc* looks like a transient Jenkins connectivity error."""
    return isinstance(exc, JENKINS_CONNECTIVITY_EXCEPTIONS)


# Pattern for detecting field names that likely contain secrets,
# regardless of whether they appear in SENSITIVE_KEYS.
_GENERIC_SENSITIVE_RE = re.compile(r"(password|token|secret|key)", re.IGNORECASE)

_MASK = "***"


def mask_sensitive_fields(data: Any) -> Any:
    """Return a deep copy of *data* with sensitive field values masked.

    Handles nested dicts and lists.  A field is considered sensitive when
    its key appears in :data:`~rootcoz.encryption.SENSITIVE_KEYS`
    or when the key contains ``password``, ``token``, ``secret``, or ``key``
    (case-insensitive).

    Non-dict/list values are returned unchanged.
    """
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and is_sensitive_key(k) and v:
                masked[k] = _MASK
            else:
                masked[k] = mask_sensitive_fields(v)
        return masked
    if isinstance(data, list):
        return [mask_sensitive_fields(item) for item in data]
    return data


def is_sensitive_key(key: str) -> bool:
    """Return True if *key* names a sensitive field."""
    return key in SENSITIVE_KEYS or bool(_GENERIC_SENSITIVE_RE.search(key))


#: ASCII control characters — stripped from identifiers and free-text
#: fields built from CI input (job names, test names, error messages).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def strip_control_chars(value: str) -> str:
    """Return *value* with ASCII control characters removed.

    Empty input is returned unchanged.
    """
    if not value:
        return value
    return _CONTROL_CHAR_RE.sub("", value)


#: Unicode general categories of invisible characters — control (Cc),
#: format (Cf: zero-width space, soft hyphen, bidi overrides), surrogate
#: (Cs), private-use (Co), and unassigned (Cn) code points.  Stripping
#: these closes the gap left by the ASCII-only ``_CONTROL_CHAR_RE``.
_INVISIBLE_CHAR_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def strip_invisible_chars(value: str) -> str:
    """Return *value* with Unicode invisible characters removed.

    Extends :func:`strip_control_chars` beyond ASCII C0/DEL to Unicode
    invisible code points (e.g. ``U+200B`` ZERO WIDTH SPACE, ``U+00AD``
    SOFT HYPHEN, bidi overrides), so identifier guards cannot be bypassed
    with invisible-only input.  Whitespace is preserved; callers apply
    ``.strip()`` as needed.
    """
    if not value:
        return value
    return "".join(
        ch for ch in value if unicodedata.category(ch) not in _INVISIBLE_CHAR_CATEGORIES
    )


def normalize_identifier(value: str | None) -> str | None:
    """Normalize an optional identifier: strip invisible chars + whitespace.

    Returns ``None`` for empty, whitespace-only, or invisible-only input so
    downstream guards treat it as absent.
    """
    if not value:
        return None
    normalized = strip_invisible_chars(value).strip()
    return normalized or None
