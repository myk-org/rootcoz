"""Abstract base class for CI source plugins.

Every CI source (Jenkins, raw input, future integrations) implements `CISource`
to fetch build data and return a normalized `CISourceResult`.  The core analysis
engine works exclusively with these abstractions, keeping CI-specific details
out of the pipeline logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from rootcoz.models import FailedTest


@dataclass
class CISourceResult:
    """Normalized result returned by every CI source plugin.

    Attributes:
        failures: Extracted test failures — the main payload for analysis.
        console_context: Relevant console output provided as AI context.
        artifacts_context: Artifact content provided as AI context.
        build_url: URL to the CI build (e.g. Jenkins build URL).
        build_passed: When True the core engine short-circuits with a
            "build passed" result instead of running analysis.
        extract_path: Temporary directory holding fetched artifacts;
            passed to ``cleanup`` for removal.
        child_job_infos: Metadata about failed child jobs as
            ``(job_name, build_number)`` tuples for recursive analysis.
    """

    failures: list[FailedTest]
    console_context: str = ""
    artifacts_context: str = ""
    build_url: str = ""
    build_passed: bool = False
    extract_path: Path | None = None
    child_job_infos: list[tuple[str, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class CISource(ABC):
    """Abstract base class that every CI source plugin must implement.

    Subclasses override ``fetch`` to pull data from a specific CI system and
    return a ``CISourceResult``.  Optional hooks (``create_child_source``,
    ``cleanup``) have sensible defaults so simple sources only need ``fetch``.
    """

    @abstractmethod
    async def fetch(self) -> CISourceResult:
        """Fetch build data from the CI source.

        Returns:
            A ``CISourceResult`` containing failures, context strings,
            and optional child-job metadata.
        """

    def create_child_source(
        self, _job_name: str, _build_number: int
    ) -> CISource | None:
        """Create a child source for a downstream job.

        Jenkins overrides this to spawn ``JenkinsSource`` instances for
        pipeline child jobs.  Sources without child-job semantics (e.g. raw
        input) return ``None``.

        Returns:
            A new ``CISource`` for the child job, or ``None`` if the source
            does not support child jobs.
        """
        return None

    def cleanup(self) -> None:
        """Release temporary resources (e.g. artifact directories).

        Called by the core engine after analysis completes.  The default
        implementation is a no-op.
        """
        return  # intentional no-op; subclasses override when needed
