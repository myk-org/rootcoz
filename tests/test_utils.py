"""Tests for shared helpers in ``rootcoz.utils``."""

from __future__ import annotations

import pytest

from rootcoz import repository
from rootcoz.utils import single_line_diagnostic


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain cause", "plain cause"),
        # ANSI CSI: the whole sequence goes, not only the ESC byte.
        ("\x1b[31mred\x1b[0m cause", "red cause"),
        # ANSI OSC with a BEL and with an ST terminator.
        ("\x1b]0;title\x07cause", "cause"),
        ("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\", "link"),
        # NUL and other non-printables are dropped.
        ("a\x00b", "ab"),
        # Every Unicode line separator collapses into one line.
        ("a\nb", "a b"),
        ("a\r\nb", "a b"),
        ("a\rb", "a b"),
        ("a\x85b", "a b"),
        ("a\u2028b", "a b"),
        ("a\u2029b", "a b"),
        # Runs of whitespace collapse; the message stays readable.
        ("Cannot  clone   repo", "Cannot clone repo"),
        ("", ""),
    ],
)
def test_single_line_diagnostic(raw: str, expected: str) -> None:
    result = single_line_diagnostic(raw)
    assert result == expected
    assert "\n" not in result
    assert all(ch.isprintable() for ch in result)


def test_single_line_diagnostic_keeps_spaces_between_words() -> None:
    """A collapsed line, not a concatenation: the cause must stay readable."""
    assert single_line_diagnostic("the remote refused the ref") == (
        "the remote refused the ref"
    )


def test_repository_has_no_private_printable_copy() -> None:
    """One shared printable helper: ``repository._printable`` is gone."""
    assert not hasattr(repository, "_printable")
