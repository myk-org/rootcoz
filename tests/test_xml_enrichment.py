"""Tests for XML extraction and enrichment functions."""

from xml.etree import ElementTree as ET

import pytest

from jenkins_job_insight.xml_enrichment import (
    apply_analysis_to_xml,
    extract_failures_from_xml,
)


JUNIT_XML_WITH_FAILURES = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="3" failures="2" errors="0">
    <testcase classname="com.example.Tests" name="test_pass" time="0.1"/>
    <testcase classname="com.example.Tests" name="test_fail_with_message" time="0.5">
        <failure message="Expected true but got false" type="AssertionError">
            at com.example.Tests.test_fail_with_message(Tests.java:42)
        </failure>
    </testcase>
    <testcase classname="com.example.Tests" name="test_fail_no_message" time="1.2">
        <failure type="Failure">tests/storage/datavolume.go:229
Timed out after 500.055s.
Expected Running but got Scheduling</failure>
    </testcase>
</testsuite>"""

JUNIT_XML_NO_FAILURES = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="2" failures="0" errors="0">
    <testcase classname="com.example.Tests" name="test_a" time="0.1"/>
    <testcase classname="com.example.Tests" name="test_b" time="0.2"/>
</testsuite>"""

JUNIT_XML_WITH_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="1" failures="0" errors="1">
    <testcase classname="com.example.Tests" name="test_error" time="0.3">
        <error message="NullPointerException" type="java.lang.NullPointerException">
            at com.example.Tests.test_error(Tests.java:55)
        </error>
    </testcase>
</testsuite>"""


class TestExtractFailuresFromXml:
    """Tests for extract_failures_from_xml()."""

    def test_extracts_failures(self) -> None:
        failures = extract_failures_from_xml(JUNIT_XML_WITH_FAILURES)
        assert len(failures) == 2
        names = [f["test_name"] for f in failures]
        assert "com.example.Tests.test_fail_with_message" in names
        assert "com.example.Tests.test_fail_no_message" in names

    def test_message_attribute_used_when_present(self) -> None:
        failures = extract_failures_from_xml(JUNIT_XML_WITH_FAILURES)
        msg_failure = next(
            f for f in failures if "test_fail_with_message" in f["test_name"]
        )
        assert msg_failure["error_message"] == "Expected true but got false"
        assert msg_failure["status"] == "FAILED"

    def test_first_line_fallback_when_no_message(self) -> None:
        failures = extract_failures_from_xml(JUNIT_XML_WITH_FAILURES)
        no_msg_failure = next(
            f for f in failures if "test_fail_no_message" in f["test_name"]
        )
        assert no_msg_failure["error_message"] == "tests/storage/datavolume.go:229"
        assert "Timed out after 500.055s" in no_msg_failure["stack_trace"]

    def test_body_text_with_leading_newlines_extracts_first_nonempty_line(
        self,
    ) -> None:
        """When body text starts with whitespace/newlines, skip blank lines."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="1" failures="1">
    <testcase classname="tests.network" name="test_id:1514">
        <failure type="Failure">
            tests/network/networkpolicy.go:136
Timed out after 15.001s.
Expected failure, but got no error.
</failure>
    </testcase>
</testsuite>"""
        failures = extract_failures_from_xml(xml)
        assert len(failures) == 1
        assert failures[0]["error_message"] == "tests/network/networkpolicy.go:136"
        assert "Timed out after 15.001s" in failures[0]["stack_trace"]

    def test_message_attribute_takes_precedence_over_body(self) -> None:
        """When both message attribute and body text exist, use attribute."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="1" failures="1">
    <testcase classname="com.example" name="test_both">
        <failure message="Attribute message" type="AssertionError">Body text line one
Body text line two</failure>
    </testcase>
</testsuite>"""
        failures = extract_failures_from_xml(xml)
        assert len(failures) == 1
        assert failures[0]["error_message"] == "Attribute message"
        assert "Body text line one" in failures[0]["stack_trace"]

    def test_empty_message_attribute_falls_back_to_body(self) -> None:
        """When message attribute is empty string, use body text."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="1" failures="1">
    <testcase classname="com.example" name="test_empty_msg">
        <failure message="" type="Failure">actual error text here</failure>
    </testcase>
</testsuite>"""
        failures = extract_failures_from_xml(xml)
        assert len(failures) == 1
        assert failures[0]["error_message"] == "actual error text here"

    def test_no_message_no_body_yields_empty_error_message(self) -> None:
        """Self-closing failure with no message/body gives empty error."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="1" failures="1">
    <testcase classname="com.example" name="test_bare">
        <failure type="Failure"/>
    </testcase>
</testsuite>"""
        failures = extract_failures_from_xml(xml)
        assert len(failures) == 1
        assert failures[0]["error_message"] == ""
        assert failures[0]["stack_trace"] == ""

    def test_no_failures_returns_empty_list(self) -> None:
        failures = extract_failures_from_xml(JUNIT_XML_NO_FAILURES)
        assert failures == []

    def test_error_element_sets_error_status(self) -> None:
        failures = extract_failures_from_xml(JUNIT_XML_WITH_ERROR)
        assert len(failures) == 1
        assert failures[0]["status"] == "ERROR"
        assert failures[0]["error_message"] == "NullPointerException"

    def test_invalid_xml_raises(self) -> None:
        with pytest.raises(ET.ParseError):
            extract_failures_from_xml("this is not xml <<<<")

    def test_classname_missing_uses_name_only(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="1" failures="1">
    <testcase name="test_no_class" time="0.1">
        <failure message="failed">stack trace</failure>
    </testcase>
</testsuite>"""
        failures = extract_failures_from_xml(xml)
        assert len(failures) == 1
        assert failures[0]["test_name"] == "test_no_class"


class TestApplyAnalysisToXml:
    """Tests for apply_analysis_to_xml()."""

    def test_injects_analysis_properties(self) -> None:
        analysis_map = {
            ("com.example.Tests", "test_fail_with_message"): {
                "classification": "CODE ISSUE",
                "details": "Assertion failed",
                "affected_tests": ["test_fail_with_message"],
            },
        }
        enriched = apply_analysis_to_xml(JUNIT_XML_WITH_FAILURES, analysis_map)
        root = ET.fromstring(enriched)
        for testcase in root.iter("testcase"):
            if testcase.get("name") == "test_fail_with_message":
                props = testcase.find("properties")
                assert props is not None
                prop_names = [p.get("name") for p in props]
                assert "ai_classification" in prop_names
                assert "ai_details" in prop_names

    def test_adds_report_url_property(self) -> None:
        analysis_map = {
            ("com.example.Tests", "test_fail_with_message"): {
                "classification": "CODE ISSUE",
                "details": "test",
            },
        }
        enriched = apply_analysis_to_xml(
            JUNIT_XML_WITH_FAILURES, analysis_map, "http://server/results/job-1"
        )
        root = ET.fromstring(enriched)
        for testsuite in root.iter("testsuite"):
            ts_props = testsuite.find("properties")
            assert ts_props is not None
            report_props = [p for p in ts_props if p.get("name") == "report_url"]
            assert len(report_props) == 1
            assert report_props[0].get("value") == "http://server/results/job-1"

    def test_returns_string(self) -> None:
        enriched = apply_analysis_to_xml(JUNIT_XML_WITH_FAILURES, {})
        assert isinstance(enriched, str)
        assert "<?xml" in enriched

    def test_empty_analysis_map_returns_valid_xml(self) -> None:
        enriched = apply_analysis_to_xml(JUNIT_XML_WITH_FAILURES, {})
        # Should still be valid XML
        ET.fromstring(enriched)

    def test_unmatched_analysis_logged(self) -> None:
        analysis_map = {
            ("nonexistent.Class", "test_missing"): {
                "classification": "CODE ISSUE",
                "details": "test",
            },
        }
        # Should not raise, just logs a warning
        enriched = apply_analysis_to_xml(JUNIT_XML_WITH_FAILURES, analysis_map)
        assert isinstance(enriched, str)

    def test_product_bug_properties_injected(self) -> None:
        analysis_map = {
            ("com.example.Tests", "test_fail_with_message"): {
                "classification": "PRODUCT BUG",
                "details": "VM scheduling issue",
                "product_bug_report": {
                    "title": "VM fails to schedule",
                    "severity": "high",
                    "component": "scheduler",
                    "description": "VMs are not being scheduled properly",
                },
            },
        }
        enriched = apply_analysis_to_xml(JUNIT_XML_WITH_FAILURES, analysis_map)
        root = ET.fromstring(enriched)
        for testcase in root.iter("testcase"):
            if testcase.get("name") == "test_fail_with_message":
                props = testcase.find("properties")
                assert props is not None
                prop_names = [p.get("name") for p in props]
                assert "ai_bug_title" in prop_names
                assert "ai_bug_severity" in prop_names

    def test_system_out_contains_analysis_text(self) -> None:
        analysis_map = {
            ("com.example.Tests", "test_fail_with_message"): {
                "classification": "CODE ISSUE",
                "details": "The assertion is incorrect",
            },
        }
        enriched = apply_analysis_to_xml(JUNIT_XML_WITH_FAILURES, analysis_map)
        root = ET.fromstring(enriched)
        for testcase in root.iter("testcase"):
            if testcase.get("name") == "test_fail_with_message":
                system_out = testcase.find("system-out")
                assert system_out is not None
                assert "Classification: CODE ISSUE" in system_out.text
                assert "The assertion is incorrect" in system_out.text
