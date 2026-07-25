"""CI source plugin for raw text failure input."""

from __future__ import annotations

from typing import Any

from rootcoz.models import FailedTest
from rootcoz.sources.base import CISource, CISourceResult


class RawSource(CISource):
    """CI source plugin for raw text failure input.

    Passes through a pre-parsed list of FailedTest objects.
    No child-job, artifact, or console-context semantics.
    """

    def __init__(self, *, failures: list[FailedTest]) -> None:
        """Initialize with a list of test failures.

        Args:
            failures: Pre-parsed test failures.

        Raises:
            ValueError: If failures list is empty.
        """
        if not failures:
            raise ValueError("failures list must not be empty")
        self._failures = failures

    async def fetch(self) -> CISourceResult:
        """Return the failures list as-is.

        Returns:
            CISourceResult with the provided failures.
        """
        return CISourceResult(failures=self._failures)

    @classmethod
    def build_request_params(
        cls, body: Any, merged: Any, base_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist raw failures on the job request params."""
        _ = merged
        assert body.failures is not None
        base_params["failures"] = [f.model_dump() for f in body.failures]
        return base_params

    @classmethod
    def from_analyze_request(cls, body: Any, merged: Any) -> RawSource:
        """Construct from an analyze request."""
        _ = merged
        assert body.failures is not None
        return cls(failures=body.failures)
