"""CI source plugin for JUnit XML file input."""

from __future__ import annotations

from typing import Any

from rootcoz.sources.base import CISource, CISourceResult
from rootcoz.xml_enrichment import extract_all_tests_from_xml


class FileSource(CISource):
    """CI source plugin for JUnit XML file input.

    Parses raw JUnit XML content and extracts test failures.
    No child-job, artifact, or console-context semantics.
    """

    def __init__(self, *, raw_xml: str) -> None:
        """Initialize with raw JUnit XML content.

        Args:
            raw_xml: Raw JUnit XML to extract failures from.
        """
        self._raw_xml = raw_xml

    async def fetch(self) -> CISourceResult:
        """Parse XML and extract all test outcomes.

        Returns:
            CISourceResult with extracted failures, passed, and skipped tests.
            Sets skip_analysis=True when no failures are found.

        Raises:
            xml.etree.ElementTree.ParseError: If the XML is malformed.
        """
        extraction = extract_all_tests_from_xml(self._raw_xml)
        return CISourceResult(
            failures=extraction.failures,
            passed_tests=extraction.passed,
            skipped_tests=extraction.skipped,
            skip_analysis=not extraction.failures,
        )

    @property
    def raw_xml(self) -> str:
        """Access the raw XML for enrichment after analysis."""
        return self._raw_xml

    @classmethod
    def build_request_params(
        cls, body: Any, merged: Any, base_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist raw XML on the job request params."""
        _ = merged
        base_params["raw_xml"] = body.raw_xml
        return base_params

    @classmethod
    def from_analyze_request(cls, body: Any, merged: Any) -> FileSource:
        """Construct from an analyze request."""
        _ = merged
        assert body.raw_xml is not None
        return cls(raw_xml=body.raw_xml)

    @classmethod
    def default_display_name(cls, body: Any) -> str:
        """Default display name for file analyses."""
        _ = body
        return "file-analysis"

    @classmethod
    def restore_reanalyze_fields(
        cls, decrypted_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Restore raw XML for file re-analysis."""
        stored_xml = decrypted_params.get("raw_xml")
        if not stored_xml:
            raise ValueError(
                "Original file analysis has no stored raw_xml; cannot re-analyze"
            )
        return {"raw_xml": stored_xml}
