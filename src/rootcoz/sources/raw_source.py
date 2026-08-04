"""CI source plugin for raw text failure input."""

from __future__ import annotations

from typing import Any

from rootcoz.models import BaseTestEntry, FailedTest
from rootcoz.sources.base import CISource, CISourceResult


class RawSource(CISource):
    """CI source plugin for raw text failure input.

    Passes through a pre-parsed list of FailedTest objects.
    No child-job, artifact, or console-context semantics.
    """

    def __init__(
        self,
        *,
        failures: list[FailedTest] | None = None,
        passed_tests: list[BaseTestEntry] | None = None,
        skipped_tests: list[BaseTestEntry] | None = None,
    ) -> None:
        """Initialize with test lists.

        Args:
            failures: Pre-parsed test failures.
            passed_tests: Pre-parsed passed tests.
            skipped_tests: Pre-parsed skipped tests.

        Raises:
            ValueError: If all lists are empty/None.
        """
        self._failures = failures or []
        self._passed_tests = passed_tests or []
        self._skipped_tests = skipped_tests or []
        if not (self._failures or self._passed_tests or self._skipped_tests):
            raise ValueError(
                "At least one test list (failures, passed_tests, or skipped_tests) must not be empty"
            )

    async def fetch(self) -> CISourceResult:
        """Return the test lists as-is.

        Returns:
            CISourceResult with provided test entries.
            Sets skip_analysis=True when no failures are present.
        """
        return CISourceResult(
            failures=self._failures,
            passed_tests=self._passed_tests,
            skipped_tests=self._skipped_tests,
            skip_analysis=not self._failures,
        )

    @classmethod
    def build_request_params(
        cls, body: Any, merged: Any, base_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist raw test lists on the job request params."""
        _ = merged
        if body.failures:
            base_params["failures"] = [f.model_dump() for f in body.failures]
        if body.passed_tests:
            base_params["passed_tests"] = [t.model_dump() for t in body.passed_tests]
        if body.skipped_tests:
            base_params["skipped_tests"] = [t.model_dump() for t in body.skipped_tests]
        return base_params

    @classmethod
    def from_analyze_request(cls, body: Any, merged: Any) -> RawSource:
        """Construct from an analyze request."""
        _ = merged
        return cls(
            failures=body.failures,
            passed_tests=body.passed_tests,
            skipped_tests=body.skipped_tests,
        )

    @classmethod
    def default_display_name(cls, body: Any) -> str:
        """Default display name for raw analyses."""
        _ = body
        return "raw-analysis"

    @classmethod
    def restore_reanalyze_fields(
        cls, decrypted_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Restore test lists for raw re-analysis."""
        fields: dict[str, Any] = {}
        stored_failures = decrypted_params.get("failures")
        stored_passed = decrypted_params.get("passed_tests")
        stored_skipped = decrypted_params.get("skipped_tests")
        if stored_failures is not None:
            fields["failures"] = stored_failures
        if stored_passed is not None:
            fields["passed_tests"] = stored_passed
        if stored_skipped is not None:
            fields["skipped_tests"] = stored_skipped
        if not fields:
            raise ValueError(
                "Original raw analysis has no stored test lists; cannot re-analyze"
            )
        return fields
