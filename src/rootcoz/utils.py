"""Shared utilities for rootcoz."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import jenkins
import requests.exceptions

from rootcoz.encryption import SENSITIVE_KEYS
from rootcoz.url_utils import mask_url_value, strip_nonprintable

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

# A terminal escape sequence: CSI (``ESC [ ... final``) and OSC
# (``ESC ] ... BEL|ST``). One definition, so every printable diagnostic in
# rootcoz drops exactly the same sequences.
_ANSI_SEQUENCE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)


def single_line_diagnostic(text: str) -> str:
    """One printable line of remote-controlled text, for a log or a terminal.

    Strips ANSI CSI/OSC sequences, drops non-printable characters, and collapses
    every run of whitespace (including ``\\r\\n``, ``\\x85``, ``\\u2028`` and
    ``\\u2029``) into one space.
    """
    printable = "".join(
        ch
        for ch in _ANSI_SEQUENCE_RE.sub(" ", text)
        if ch.isprintable() or ch.isspace()
    )
    return " ".join(printable.split())


_MASK = "***"


def mask_sensitive_fields(data: Any, *, sanitize_text: bool = True) -> Any:
    """Return a deep copy of *data* with sensitive field values masked.

    Handles nested dicts and lists.  A field is considered sensitive when
    its key appears in :data:`~rootcoz.encryption.SENSITIVE_KEYS`
    or when the key contains ``password``, ``token``, ``secret``, or ``key``
    (case-insensitive).

    Non-dict/list values are returned unchanged, except that a string is
    stripped of non-printable characters and has the credentials of a
    URL-shaped value masked (#562).

    ``sanitize_text=False`` masks sensitive *keys* only and leaves every
    string value exactly as it is: a database row is data, and masking belongs
    to URL-valued fields, not to arbitrary cell text (#581 Qodo r3).
    """
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and is_sensitive_key(k) and v:
                masked[k] = _MASK
            else:
                masked[k] = mask_sensitive_fields(v, sanitize_text=sanitize_text)
        return masked
    if isinstance(data, list):
        return [
            mask_sensitive_fields(item, sanitize_text=sanitize_text) for item in data
        ]
    if isinstance(data, str) and sanitize_text:
        # Unconditional: ``mask_url_value`` leaves non-URL text alone, so a
        # scheme-less control-bearing value would otherwise reach a 422 body.
        return strip_nonprintable(mask_url_value(data))
    return data


def is_sensitive_key(key: str) -> bool:
    """Return True if *key* names a sensitive field."""
    return key in SENSITIVE_KEYS or bool(_GENERIC_SENSITIVE_RE.search(key))


#: ASCII control characters — stripped from identifiers and free-text
#: fields built from CI input (job names, test names, error messages).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


#: Character-set selection across the sanitizers (so a caller picks the right
#: one instead of stacking them blindly):
#: * :func:`strip_control_chars` — ASCII ``Cc``/DEL only. Cheap, and enough for
#:   a field that reaches a log line or an ASCII-only identifier guard.
#: * :func:`strip_invisible_chars` — every Unicode ``Cc``/``Cf``/``Cs``/``Co``/
#:   ``Cn`` code point, i.e. ASCII control PLUS zero-width, soft hyphen, bidi
#:   overrides and other invisible characters; ordinary ASCII spaces are
#:   preserved, while tabs, newlines, and other ``Cc`` characters are removed.
#:   It is a strict superset of :func:`strip_control_chars`, so the two are
#:   never stacked.
#: * :func:`strip_gating_text` — :func:`strip_invisible_chars`, for text that
#:   reaches a gating system (ResultsDB note, WaiverDB comment).
#: * ``rootcoz.url_utils.strip_nonprintable`` — keeps only printable code
#:   points (ordinary spaces are printable and kept; tabs, newlines and other
#:   non-printables are dropped): a one-line display value, not free text.
def strip_control_chars(value: str) -> str:
    """Return *value* with ASCII control characters removed.

    Empty input is returned unchanged. This is the ASCII-only half of the
    sanitizer set — see the selection rule above for when to use
    :func:`strip_invisible_chars` / :func:`strip_gating_text` instead.
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
    with invisible-only input.  Ordinary ASCII spaces are preserved (tabs,
    newlines, and other ``Cc`` characters are removed); callers apply
    ``.strip()`` as needed.  See the selection rule on
    :func:`strip_control_chars` for the full sanitizer set.
    """
    if not value:
        return value
    return "".join(
        ch for ch in value if unicodedata.category(ch) not in _INVISIBLE_CHAR_CATEGORIES
    )


def strip_gating_text(value: str) -> str:
    """Sanitize text that reaches a gating system: no control or invisible chars.

    The single rule for a ResultsDB note, a WaiverDB comment, or an actor name
    carried into either (including a proxy-supplied ``X-Forwarded-User``
    username): ASCII control characters and Unicode invisible/bidi characters
    are removed, so forged log lines and reordered comment text cannot enter a
    gating record. Ordinary ASCII spaces are preserved; tabs, newlines and
    other ``Cc`` characters are removed.

    ``strip_invisible_chars`` already covers ASCII ``Cc``/DEL (every ASCII
    control char categorises as ``Cc``) plus ``Cf``/``Cs``/``Co``/``Cn``, so it
    is a strict superset of ``strip_control_chars`` — no second pass needed.
    """
    return strip_invisible_chars(value)


def normalize_identifier(value: str | None) -> str | None:
    """Normalize an optional identifier: strip invisible chars + whitespace.

    Returns ``None`` for empty, whitespace-only, or invisible-only input so
    downstream guards treat it as absent.
    """
    if not value:
        return None
    normalized = strip_invisible_chars(value).strip()
    return normalized or None
