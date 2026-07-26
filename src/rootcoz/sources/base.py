"""Abstract base class for CI source plugins.

Every CI source (Jenkins, Prow, file/JUnit XML, raw input) implements `CISource`
to fetch build data and return a normalized `CISourceResult`.  The core analysis
engine works exclusively with these abstractions, keeping CI-specific details
out of the pipeline logic.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rootcoz.models import AdditionalRepo, FailedTest

if TYPE_CHECKING:
    from rootcoz.repository import RepositoryManager

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
        build_passed_summary: Human-readable summary when the build passed
            with no failures. Each source plugin can override the default.
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
    build_passed_summary: str = "No test failures found in the provided input."


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

    async def populate_chat_workspace(
        self,
        workspace: Path,
        *,
        github_token: str = "",
    ) -> bool:
        """Write CI build context files into a chat workspace.

        Called when a user opens chat for an analyzed job so the AI can
        read console output, artifacts, and source-specific context files.
        Sources without remote data return ``False``.

        Args:
            workspace: Per-user chat workspace directory.
            github_token: Optional GitHub token for PR diffs and repo access.

        Returns:
            True if any files were written to the workspace.
        """
        return False

    @classmethod
    def validate_request(cls, body: Any, merged: Any) -> None:
        """Validate source-specific request fields before enqueue.

        Raise ``ValueError`` (or ``HTTPException``) when required config
        is missing. Default is a no-op.
        """
        return None

    @classmethod
    def build_request_params(
        cls, body: Any, merged: Any, base_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Extend ``base_params`` with source-specific persisted fields.

        Mutates and returns ``base_params``. Default leaves params unchanged.
        """
        return base_params

    @classmethod
    def pre_persist_identity_from_request(cls, body: Any) -> dict[str, Any]:
        """Identity fields to stamp on the initial result before ``fetch``.

        Default returns an empty dict (display name only until fetch).
        """
        return {}

    @classmethod
    def from_analyze_request(cls, body: Any, merged: Any) -> CISource:
        """Construct a source plugin from an analyze/re-analyze request.

        Subclasses used by the shared CI-source analysis path must override.
        """
        raise NotImplementedError(
            f"{cls.__name__} does not support from_analyze_request"
        )

    async def persist_fetch_metadata(
        self, job_id: str, source_result: CISourceResult
    ) -> None:
        """Persist source-specific metadata after ``fetch`` (optional).

        Default is a no-op. Prow overrides to store the resolved GCS prefix.
        """
        return None

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


@dataclass
class WorkspaceSetupResult:
    """Result of setting up an analysis workspace."""

    repo_path: Path
    cloned_repos: dict[str, Path] = field(default_factory=dict)
    repo_context: str = ""


async def setup_analysis_workspace(
    repo_manager: "RepositoryManager",
    *,
    tests_repo_url: str = "",
    tests_repo_ref: str = "",
    tests_repo_token: str = "",
    additional_repos: list[AdditionalRepo] | None = None,
    extract_path: Path | None = None,
    artifacts_context: str = "",
    job_id: str = "",
) -> tuple[WorkspaceSetupResult, str]:
    """Create workspace, clone repos, copy resources, link artifacts.

    Shared helper that eliminates workspace-setup duplication across
    ``_process_ci_source_analysis``, ``_reanalyze_failure_background``,
    and ``jenkins_source.analyze_job``.

    Args:
        repo_manager: Pre-created ``RepositoryManager`` (caller owns lifecycle).
        tests_repo_url: URL of the test repository to clone.
        tests_repo_ref: Git ref (branch/tag/SHA) to check out.
        tests_repo_token: Token for authenticating to the test repo.
        additional_repos: List of additional repo dicts to clone.
        extract_path: Path to downloaded build artifacts to symlink.
        artifacts_context: Text context describing artifacts; cleared
            if artifact linking fails.
        job_id: Job identifier for log messages.

    Returns:
        Tuple of ``(WorkspaceSetupResult, effective_artifacts_context)``.
        ``artifacts_context`` may be cleared if artifact linking fails.
    """
    from rootcoz.engine.core import (
        clone_additional_repos,
        copy_rootcoz_pi_resources,
    )
    from rootcoz.repository import derive_test_repo_name, redact_url

    repo_path = repo_manager.create_workspace()
    cloned_repos: dict[str, Path] = {}
    repo_context = ""

    logger.debug("Workspace created at %s", repo_path)

    if tests_repo_url:
        logger.debug(
            "Cloning test repo: %s (ref=%s)",
            redact_url(str(tests_repo_url)),
            tests_repo_ref,
        )
        try:
            additional_repos_list = additional_repos or []
            repo_name = derive_test_repo_name(
                str(tests_repo_url), additional_repos_list
            )
            await asyncio.to_thread(
                repo_manager.clone_into,
                str(tests_repo_url),
                repo_path / repo_name,
                depth=50,
                branch=tests_repo_ref,
                token=tests_repo_token or None,
            )
            cloned_repos[repo_name] = repo_path / repo_name
            logger.info("Test repo cloned successfully into %s/", repo_name)
            repo_context = (
                f"\nTest repository cloned from: "
                f"{redact_url(str(tests_repo_url))} (at {repo_name}/)"
            )
        except Exception as exc:
            logger.warning(
                "Failed to clone test repository (%s)",
                type(exc).__name__,
                exc_info=True,
            )
            repo_context = "\nFailed to clone repository (details redacted)"

    if additional_repos:
        additional_repos_cloned, repo_path = await clone_additional_repos(
            repo_manager, additional_repos, repo_path
        )
        cloned_repos.update(additional_repos_cloned)

    if cloned_repos:
        copy_rootcoz_pi_resources(cloned_repos, repo_path)

    if extract_path:
        if not link_artifacts_to_workspace(repo_path, extract_path, job_id):
            artifacts_context = ""

    result = WorkspaceSetupResult(
        repo_path=repo_path,
        cloned_repos=cloned_repos,
        repo_context=repo_context,
    )
    return result, artifacts_context


def append_repo_context(custom_prompt: str, repo_context: str) -> str:
    """Append clone-status context to a custom prompt when present."""
    if not repo_context:
        return custom_prompt
    if custom_prompt:
        return f"{custom_prompt}{repo_context}"
    return repo_context.lstrip("\n")


def write_workspace_file_list(
    workspace: Path,
    files: list[WorkspaceFile],
    *,
    skip_existing: bool = False,
    log_prefix: str = "",
) -> bool:
    """Write workspace context files; return True if any file was written."""
    wrote_any = False
    for workspace_file in files:
        target = workspace / workspace_file.filename
        if skip_existing and target.exists():
            continue
        try:
            target.write_text(workspace_file.content)
            logger.info("%sWrote %s", log_prefix, target.name)
            wrote_any = True
        except OSError:
            logger.warning("Failed to write %s", target.name, exc_info=True)
    return wrote_any


def write_workspace_context_file(
    filepath: Path,
    content: str,
    instruction: str,
    custom_prompt: str,
    job_id: str,
) -> str:
    """Write a context file to the AI workspace and prepend a MANDATORY instruction.

    Returns the updated ``custom_prompt``, or the original on write failure.
    """
    try:
        filepath.write_text(content)
        full_instruction = (
            f"\n\nMANDATORY: Read {filepath} before analyzing. {instruction}"
        )
        return (
            full_instruction + "\n" + custom_prompt
            if custom_prompt
            else full_instruction
        )
    except OSError:
        logger.warning(
            "Failed to write %s for job %s",
            filepath.name,
            job_id,
            exc_info=True,
        )
        return custom_prompt


async def apply_source_workspace_files(
    source: CISource,
    repo_path: Path,
    custom_prompt: str,
    job_id: str,
    *,
    github_token: str = "",
) -> str:
    """Write plugin workspace files and update the analysis prompt."""
    workspace_files = await source.prepare_workspace(
        repo_path=repo_path, github_token=github_token
    )
    updated_prompt = custom_prompt
    for workspace_file in workspace_files:
        updated_prompt = write_workspace_context_file(
            filepath=repo_path / workspace_file.filename,
            content=workspace_file.content,
            instruction=workspace_file.instruction,
            custom_prompt=updated_prompt,
            job_id=job_id,
        )
    return updated_prompt


def link_refetched_artifacts(
    repo_path: Path,
    extract_path: Path | None,
    artifacts_context: str,
    job_id: str,
) -> str:
    """Link refetched artifacts into the workspace, clearing context on failure."""
    if not extract_path:
        return artifacts_context
    if link_artifacts_to_workspace(repo_path, extract_path, job_id):
        return artifacts_context
    return ""


async def run_console_only_analysis(
    *,
    test_name: str,
    console_context: str,
    artifacts_context: str,
    repo_path: Path | None,
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None,
    custom_prompt: str,
    server_url: str,
    job_id: str,
    additional_repos: dict[str, Path] | None,
    auth_header: str,
    call_type: str = "console",
    peer_ai_configs: list | None = None,
    peer_analysis_max_rounds: int = 3,
    max_concurrent_ai_calls: int = 3,
) -> tuple[bool, list, str]:
    """Run console-only AI analysis when no structured test failures exist."""
    from rootcoz.engine.core import analyze_failure_group, normalize_for_signature
    from rootcoz.models import FailedTest

    synthetic_failure = FailedTest(
        test_name=test_name,
        error_message=(
            "Console-only analysis — read the mandatory console-output section "
            "in the analysis prompt before analyzing."
        ),
        stack_trace=f"console_sha256:{hashlib.sha256(normalize_for_signature(console_context).encode()).hexdigest()}",
    )

    try:
        results = await analyze_failure_group(
            failures=[synthetic_failure],
            console_context=console_context,
            repo_path=repo_path,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=ai_call_timeout,
            custom_prompt=custom_prompt,
            artifacts_context=artifacts_context,
            server_url=server_url,
            job_id=job_id,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=peer_analysis_max_rounds,
            group_label=call_type,
            additional_repos=additional_repos,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
            auth_header=auth_header,
        )
        return True, results, ""
    except Exception as exc:
        logger.error("Console-only analysis failed: %s", exc, exc_info=True)
        return False, [], str(exc)


def resolve_display_build_id(result_data: dict) -> str | int:
    """Resolve the best display identifier for a build.

    Returns build_id (string) when build_number is missing/zero,
    otherwise build_number (int).
    """
    build_number = result_data.get("build_number")
    if build_number:
        return build_number
    build_id = result_data.get("build_id", "")
    return build_id if build_id else 0


def link_artifacts_to_workspace(
    repo_path: Path, extract_path: Path, job_id: str
) -> bool:
    """Symlink downloaded artifacts into the AI workspace.

    Creates a ``build-artifacts`` symlink inside *repo_path* pointing to
    *extract_path* so the AI can explore artifacts via a stable relative path.

    If ``build-artifacts`` already exists (common on reanalysis / chat reuse),
    treat that as success so callers do not clear ``artifacts_context``.

    Returns:
        ``True`` if the link was created or already exists, ``False`` on failure.
    """
    link = repo_path / "build-artifacts"
    # Pre-existing path (symlink or directory) means artifacts are already
    # reachable — do not report failure (callers clear context on False).
    if link.exists() or link.is_symlink():
        logger.info(
            "Artifacts path already present at %s (job %s) — keeping existing link",
            link,
            job_id,
        )
        return True
    try:
        link.symlink_to(extract_path)
        logger.info("Linked artifacts into workspace: %s (job %s)", link, job_id)
        return True
    except FileExistsError:
        # Race: created between the existence check and symlink_to.
        logger.info(
            "Artifacts path already present at %s (job %s) — keeping existing link",
            link,
            job_id,
        )
        return True
    except OSError:
        logger.warning(
            "Could not link artifacts into workspace for job %s",
            job_id,
            exc_info=True,
        )
        return False
