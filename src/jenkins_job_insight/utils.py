"""Shared utilities for jenkins-job-insight."""

from __future__ import annotations

import re
from typing import Any

import jenkins
import requests.exceptions

from jenkins_job_insight.encryption import SENSITIVE_KEYS


#: Combined tuple of exception types that indicate a transient Jenkins
#: connectivity problem (network outage, DNS failure, timeout, etc.).
#: Used by both the polling loop in *main.py* and the pre-flight check
#: in *analyzer.py*.
#:
#: Note: ``jenkins.JenkinsException`` is intentionally excluded — it is
#: the base class for many non-transient errors (auth failures, 5xx,
#: malformed responses).  Only ``jenkins.TimeoutException`` (a subclass)
#: represents a true connectivity/timeout problem.
JENKINS_CONNECTIVITY_EXCEPTIONS: tuple[type[BaseException], ...] = (
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
    its key appears in :data:`~jenkins_job_insight.encryption.SENSITIVE_KEYS`
    or when the key contains ``password``, ``token``, ``secret``, or ``key``
    (case-insensitive).

    Non-dict/list values are returned unchanged.
    """
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and _is_sensitive_key(k) and v:
                masked[k] = _MASK
            else:
                masked[k] = mask_sensitive_fields(v)
        return masked
    if isinstance(data, list):
        return [mask_sensitive_fields(item) for item in data]
    return data


def _is_sensitive_key(key: str) -> bool:
    """Return True if *key* names a sensitive field."""
    return key in SENSITIVE_KEYS or bool(_GENERIC_SENSITIVE_RE.search(key))
