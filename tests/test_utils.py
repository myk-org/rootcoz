"""Unit tests for rootcoz.utils pure helpers."""

from __future__ import annotations

import pytest

from rootcoz.utils import (
    normalize_optional_text,
    parse_exporter_names,
    sanitize_control_chars,
)


class TestSanitizeControlCharsUtil:
    """Tests for the canonical sanitize_control_chars in rootcoz.utils."""

    @pytest.mark.parametrize(
        "input_val, expected",
        [
            ("alice", "alice"),
            ("", ""),
            ("alice\nfake", "alicefake"),
            ("bob\r\nINFO", "bobINFO"),
            ("tab\there", "tabhere"),
            ("null\x00here", "nullhere"),
            ("del\x7fhere", "delhere"),
            ("\x01\x02test\x1f", "test"),
        ],
        ids=[
            "normal",
            "empty",
            "newline",
            "crlf",
            "tab",
            "null_byte",
            "del_char",
            "mixed_control",
        ],
    )
    def test_sanitize_control_chars(self, input_val: str, expected: str) -> None:
        assert sanitize_control_chars(input_val) == expected


class TestNormalizeOptionalText:
    """normalize_optional_text: control-char sanitization + strip + None-collapse."""

    def test_none_stays_none(self) -> None:
        assert normalize_optional_text(None) is None

    def test_plain_text_returned(self) -> None:
        assert normalize_optional_text("hello") == "hello"

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_optional_text("   ") is None

    def test_strips_surrounding_whitespace(self) -> None:
        assert normalize_optional_text("  hello  ") == "hello"

    def test_control_char_only_returns_none(self) -> None:
        assert normalize_optional_text("\x00") is None
        assert normalize_optional_text("\x01\x02") is None

    def test_control_chars_stripped_from_mixed_value(self) -> None:
        assert normalize_optional_text("build\x00nvr") == "buildnvr"

    def test_control_chars_plus_whitespace_only_returns_none(self) -> None:
        # After stripping control chars the remaining whitespace collapses to None.
        assert normalize_optional_text("\x01  \x02") is None

    def test_newline_in_value_stripped(self) -> None:
        # Newline is a control char; surrounding whitespace also trimmed.
        assert normalize_optional_text("\nhello\n") == "hello"

    def test_empty_string_returns_none(self) -> None:
        assert normalize_optional_text("") is None


class TestParseExporterNames:
    """Tests for the parse_exporter_names pure helper."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # None input → empty list
            (None, []),
            # Empty / whitespace-only string → empty list
            ("", []),
            ("   ", []),
            # Single name
            ("reportportal", ["reportportal"]),
            # Strip whitespace + lowercase
            (" ReportPortal , GREENWAVE ", ["reportportal", "greenwave"]),
            # Control char inside name: \x01 removed → 'greenwave'
            ("green\x01wave", ["greenwave"]),
            # Trailing null byte stripped → 'greenwave'
            ("greenwave\x00", ["greenwave"]),
            # Empty entries dropped (consecutive commas, trailing comma)
            ("reportportal,,greenwave,", ["reportportal", "greenwave"]),
            # Order preserved + first-occurrence dedup (case-insensitive)
            (
                "greenwave,GREENWAVE,reportportal,greenwave",
                ["greenwave", "reportportal"],
            ),
        ],
        ids=[
            "none_input",
            "empty_string",
            "whitespace_only",
            "single_name",
            "strip_and_lowercase",
            "control_char_mid",
            "trailing_null",
            "empty_entries_dropped",
            "dedup_order_preserved",
        ],
    )
    def test_parse_exporter_names(self, raw: str | None, expected: list[str]) -> None:
        assert parse_exporter_names(raw) == expected
