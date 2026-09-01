"""Shared utilities for rootcoz."""

from __future__ import annotations

import re
from typing import Any

import jenkins
import requests.exceptions

from rootcoz.encryption import SENSITIVE_KEYS

#: Matches ASCII control characters (U+0000–U+001F and U+007F).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_control_chars(value: str) -> str:
    """Strip ASCII control characters from *value*."""
    return _CONTROL_CHAR_RE.sub("", value)


def parse_exporter_names(raw: str | None) -> list[str]:
    """Parse a comma-separated list of exporter names into a normalised list.

    Each name is: stripped of whitespace, control-char-sanitised, and
    lower-cased.  Empty entries are dropped.  The returned list preserves
    input order and de-duplicates (first occurrence wins).
    """
    names: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        name = sanitize_control_chars(part.strip()).lower()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


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
_LOG_ONLY_SENSITIVE_KEYS = frozenset({"subject_identifier", "waiver_comment"})

_MASK = "***"


def mask_sensitive_fields(data: Any) -> Any:
    """Return a deep copy of *data* with sensitive field values masked.

    Handles nested dicts and lists. A field is considered sensitive when its
    key is a stored secret, is explicitly log-sensitive (for example
    ``waiver_comment``), or contains ``password``, ``token``, ``secret``, or
    ``key`` (case-insensitive).

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


def normalize_optional_text(value: str | None) -> str | None:
    """Strip ASCII control characters and surrounding whitespace from *value*.

    Returns ``None`` for ``None``, empty, whitespace-only, or control-char-only
    input.  This is the **single-source normalizer** shared by
    :class:`rootcoz.models.ExporterPushOptions` (via its field validator) and
    :func:`rootcoz.main._build_export_context`, so every caller gets both
    control-char sanitization and whitespace trimming without duplication.
    """
    if value is None:
        return None
    return sanitize_control_chars(value).strip() or None


def is_sensitive_key(key: str) -> bool:
    """Return True if *key* must be masked in diagnostic output."""
    return (
        key in SENSITIVE_KEYS
        or key in _LOG_ONLY_SENSITIVE_KEYS
        or bool(_GENERIC_SENSITIVE_RE.search(key))
    )
