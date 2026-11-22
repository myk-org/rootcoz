"""CI source plugin for JUnit XML file input."""

from __future__ import annotations

from typing import Any

from rootcoz import get_child_logger
from rootcoz.sources.base import CISource, CISourceResult
from rootcoz.xml_enrichment import (
    extract_all_tests_from_xml,
    extract_suite_name_from_xml,
)

logger = get_child_logger(__name__)


class FileSource(CISource):
    """CI source plugin for JUnit XML file input.

    Parses raw JUnit XML content and extracts test failures.
    No child-job, artifact, or console-context semantics.
    """

    def __init__(self, *, raw_xml: str, job_name: str | None = None) -> None:
        """Initialize with raw JUnit XML content.

        Args:
            raw_xml: Raw JUnit XML to extract failures from.
            job_name: Optional explicit job name for this file analysis.
        """
        self._raw_xml = raw_xml
        self._job_name = job_name

    async def fetch(self) -> CISourceResult:
        """Parse XML and extract all test outcomes.

        Returns:
            CISourceResult with extracted failures, passed, and skipped tests.
            Sets skip_analysis=True when no failures are found.

        Raises:
            xml.etree.ElementTree.ParseError: If the XML is malformed.
        """
        extraction = extract_all_tests_from_xml(self._raw_xml)
        identity = {"job_name": self._job_name} if self._job_name else {}
        return CISourceResult(
            failures=extraction.failures,
            passed_tests=extraction.passed,
            skipped_tests=extraction.skipped,
            skip_analysis=not extraction.failures,
            identity=identity,
        )

    @property
    def raw_xml(self) -> str:
        """Access the raw XML for enrichment after analysis."""
        return self._raw_xml

    @classmethod
    def build_request_params(
        cls, body: Any, merged: Any, base_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist raw XML (and optional explicit job_name) on the job request params."""
        _ = merged
        base_params["raw_xml"] = body.raw_xml
        if getattr(body, "job_name", None):
            base_params["job_name"] = body.job_name
        return base_params

    @classmethod
    def pre_persist_identity_from_request(cls, body: Any) -> dict[str, Any]:
        """Stamp explicit job_name on initial result when supplied."""
        if getattr(body, "job_name", None):
            return {"job_name": body.job_name}
        return {}

    @classmethod
    def from_analyze_request(cls, body: Any, merged: Any) -> FileSource:
        """Construct from an analyze request."""
        _ = merged
        assert body.raw_xml is not None
        return cls(
            raw_xml=body.raw_xml,
            job_name=getattr(body, "job_name", None),
        )

    @classmethod
    def resolve_display_name(cls, body: Any) -> tuple[str, bool]:
        """Resolve ``(display_name, is_generic_fallback)`` for file analyses."""
        explicit = getattr(body, "job_name", None)
        if explicit:
            return explicit, False
        raw_xml = getattr(body, "raw_xml", None)
        if isinstance(raw_xml, str) and raw_xml:
            suite_name = extract_suite_name_from_xml(raw_xml)
            if suite_name:
                logger.info(
                    "Derived file-analysis job name %r from JUnit XML suite",
                    suite_name,
                )
                return suite_name, False
        return "file-analysis", True

    @classmethod
    def default_display_name(cls, body: Any) -> str:
        """Resolve display name from explicit job_name, XML suite name, or fallback."""
        name, _ = cls.resolve_display_name(body)
        return name

    @classmethod
    def restore_reanalyze_fields(
        cls, decrypted_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Restore raw XML (and optional explicit job_name) for file re-analysis."""
        stored_xml = decrypted_params.get("raw_xml")
        if not stored_xml:
            raise ValueError(
                "Original file analysis has no stored raw_xml; cannot re-analyze"
            )
        fields: dict[str, Any] = {"raw_xml": stored_xml}
        if decrypted_params.get("job_name"):
            fields["job_name"] = decrypted_params["job_name"]
        return fields
