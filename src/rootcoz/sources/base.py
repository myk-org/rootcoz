"""Abstract base class for CI source plugins.

Every CI source (Jenkins, raw input, future integrations) implements `CISource`
to fetch build data and return a normalized `CISourceResult`.  The core analysis
engine works exclusively with these abstractions, keeping CI-specific details
out of the pipeline logic.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rootcoz.models import FailedTest

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceFile:
    """A file to write to the AI workspace for analysis context.

    Attributes:
        filename: Name of the file (e.g. ``prow-context.txt``).
        content: Text content to write.
        instruction: Instruction appended to the AI prompt explaining
            what the file contains and why the AI must read it.
    """

    filename: str
    content: str
    instruction: str


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
        source_metadata: Optional metadata from the CI source plugin
            (e.g. Prow job type, PR number, repo info from prowjob.json).
        identity: Override fields for the result dict (e.g. job_name, build_number for Prow).
    """

    failures: list[FailedTest]
    console_context: str = ""
    artifacts_context: str = ""
    build_url: str = ""
    build_passed: bool = False
    extract_path: Path | None = None
    child_job_infos: list[tuple[str, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_metadata: dict = field(default_factory=dict)
    identity: dict = field(default_factory=dict)


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

    async def prepare_workspace(
        self,
        repo_path: Path | None = None,
        github_token: str = "",
    ) -> list[WorkspaceFile]:
        """Prepare workspace context files for AI analysis.

        Called after ``fetch`` with the cloned repository path. Plugins
        override this to produce files the AI should read before analysing
        failures (e.g. CI job context, PR diffs).

        Args:
            repo_path: Path to the cloned test repository workspace.
            github_token: Optional GitHub token for API calls (PR diffs).

        Returns:
            List of ``WorkspaceFile`` entries to write to the workspace.
        """
        return []

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

    async def refetch_context(self) -> CISourceResult:
        """Re-download console output and artifacts for reanalysis.

        Called by per-failure reanalysis to provide the same evidence
        the original analysis had.  Returns a ``CISourceResult`` with
        at least ``console_context`` and ``artifacts_context`` populated;
        other fields (``failures``, ``child_job_infos``) may be empty.

        The caller is responsible for calling ``cleanup()`` when done.

        The default implementation returns an empty result — suitable
        for sources without remote console/artifact data (e.g. file, raw).
        Subclasses that fetch from a CI system should override this.
        """
        return CISourceResult(failures=[])

    @classmethod
    def from_stored_params(
        cls,
        params: dict[str, Any],
        settings: Any = None,
        *,
        child_job_name: str = "",
        child_build_number: int = 0,
    ) -> CISource | None:
        """Reconstruct a source plugin from stored request params.

        Used by per-failure reanalysis to recreate the source that
        produced the original analysis, so it can refetch context.

        Args:
            params: Decrypted ``request_params`` from the stored result.
            settings: Server ``Settings`` object (needed for Jenkins
                connection info, artifact config, etc.).
            child_job_name: For Jenkins child job failures — overrides
                the job name from params.
            child_build_number: For Jenkins child job failures — overrides
                the build number from params.

        Returns:
            A ``CISource`` instance, or ``None`` if the source type
            cannot be reconstructed (e.g. file/raw — no remote data).
        """
        return None

    def cleanup(self) -> None:
        """Release temporary resources (e.g. artifact directories).

        Called by the core engine after analysis completes.  The default
        implementation is a no-op.
        """
        return  # intentional no-op; subclasses override when needed


def link_artifacts_to_workspace(
    repo_path: Path, extract_path: Path, job_id: str
) -> bool:
    """Symlink downloaded artifacts into the AI workspace.

    Creates a ``build-artifacts`` symlink inside *repo_path* pointing to
    *extract_path* so the AI can explore artifacts via a stable relative path.

    Returns:
        ``True`` if the link was created successfully, ``False`` on failure.
    """
    link = repo_path / "build-artifacts"
    try:
        link.symlink_to(extract_path)
        logger.info("Linked artifacts into workspace: %s (job %s)", link, job_id)
        return True
    except OSError:
        logger.warning(
            "Could not link artifacts into workspace for job %s",
            job_id,
            exc_info=True,
        )
        return False
