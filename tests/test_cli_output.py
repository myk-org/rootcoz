"""Tests for CLI output formatting helpers."""

import json
from io import StringIO
from unittest.mock import patch

from jenkins_job_insight.cli.output import format_table, format_json, print_output


class TestFormatTable:
    def test_basic_table(self):
        data = [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ]
        result = format_table(data, columns=["name", "age"])
        assert "Alice" in result
        assert "Bob" in result
        assert "NAME" in result
        assert "AGE" in result

    def test_empty_data(self):
        result = format_table([], columns=["name"])
        assert "No results" in result

    def test_dict_input_single_row(self):
        data = {"status": "healthy"}
        result = format_table([data], columns=["status"])
        assert "healthy" in result

    def test_missing_key_shows_empty(self):
        data = [{"name": "Alice"}]
        result = format_table(data, columns=["name", "missing"])
        assert "Alice" in result

    def test_column_width_adapts(self):
        data = [
            {"short": "a", "long": "a" * 50},
        ]
        result = format_table(data, columns=["short", "long"])
        lines = result.strip().split("\n")
        # Header and separator should exist
        assert len(lines) >= 3

    def test_column_labels(self):
        data = [{"job_id": "abc"}]
        result = format_table(data, columns=["job_id"], labels={"job_id": "Job ID"})
        assert "Job ID" in result
        assert "abc" in result

    def test_no_truncation(self):
        data = [{"val": "x" * 200}]
        result = format_table(data, columns=["val"])
        # Full value is preserved, no truncation
        assert "x" * 200 in result
        assert "..." not in result


class TestFormatJson:
    def test_json_output(self):
        data = {"status": "healthy"}
        result = format_json(data)
        parsed = json.loads(result)
        assert parsed == {"status": "healthy"}

    def test_json_indented(self):
        data = {"a": 1, "b": 2}
        result = format_json(data)
        assert "\n" in result  # Should be pretty-printed


class TestPrintOutput:
    def test_print_output_table_mode(self):
        data = [{"name": "test"}]
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            print_output(data, columns=["name"], as_json=False)
            output = mock_out.getvalue()
            assert "test" in output

    def test_print_output_json_mode(self):
        data = {"status": "ok"}
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            print_output(data, columns=[], as_json=True)
            output = mock_out.getvalue()
            parsed = json.loads(output)
            assert parsed["status"] == "ok"
