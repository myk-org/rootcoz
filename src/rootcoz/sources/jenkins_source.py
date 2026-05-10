"""Jenkins CI source plugin.

Implements the ``CISource`` interface to fetch build data from Jenkins.
Also houses Jenkins-specific helper functions (``handle_jenkins_exception``,
``extract_failed_child_jobs``, etc.) and the top-level ``analyze_job`` /
``analyze_child_job`` orchestration functions.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time as _time
import uuid
from collections import defaultdict
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, NoReturn

import jenkins
from ai_cli_runner import check_ai_cli_available, run_parallel_with_limit
from pydantic import HttpUrl
from simple_logger.logger import get_logger

from rootcoz.config import Settings, parse_repo_ref
from rootcoz.engine.core import (
    JSON_RESPONSE_SCHEMA,
    PROVIDER_CLI_FLAGS,
    analyze_failure_group,
    build_prompt_sections,
    call_ai_and_record,
    clone_additional_repos,
    derive_error_details,
    extract_relevant_console_lines,
    format_exception_with_type,
    format_timeout_log,
    get_failure_signature,
    resolve_additional_repos,
    safe_update_progress,
)
from rootcoz.jenkins import JenkinsClient
from rootcoz.jenkins_artifacts import cleanup_extract_dir, process_build_artifacts
from rootcoz.models import (
    AnalysisDetail,
    AnalysisResult,
    AnalyzeRequest,
    ChildJobAnalysis,
    FailedTest,
    FailureAnalysis,
)
from rootcoz.repository import RepositoryManager, derive_test_repo_name, redact_url
from rootcoz.request_resolution import resolve_tests_repo_token
from rootcoz.sources.base import CISource, CISourceResult
from rootcoz.utils import is_jenkins_connectivity_error

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Jenkins-specific helper functions
# ---------------------------------------------------------------------------


class JenkinsError(Exception):
    """Domain exception for Jenkins interaction failures."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def handle_jenkins_exception(
    e: Exception, job_name: str, build_number: int
) -> NoReturn:
    """Convert Jenkins exceptions to appropriate JenkinsErrors.

    Args:
        e: The exception raised by the Jenkins client.
        job_name: Name of the Jenkins job being accessed.
        build_number: Build number being accessed.

    Raises:
        JenkinsError: With appropriate status code and detail message.
    """
    if isinstance(e, jenkins.NotFoundException):
        raise JenkinsError(
            f"Job '{job_name}' build #{build_number} not found in Jenkins",
            status_code=404,
        )

    if isinstance(e, jenkins.JenkinsException):
        error_msg = str(e).lower()
        if (
            "does not exist" in error_msg
            or "not found" in error_msg
            or "404" in error_msg
        ):
            raise JenkinsError(
                f"Job '{job_name}' build #{build_number} not found in Jenkins",
                status_code=404,
            )
        elif "unauthorized" in error_msg or "401" in error_msg:
            raise JenkinsError(
                "Jenkins authentication failed. Check JENKINS_USER and JENKINS_PASSWORD.",
                status_code=502,
            )
        elif "forbidden" in error_msg or "403" in error_msg:
            raise JenkinsError(
                f"Access denied to job '{job_name}'. Check Jenkins permissions.",
                status_code=502,
            )
        else:
            raise JenkinsError(
                f"Jenkins error: {e!s}",
                status_code=502,
            )

    if is_jenkins_connectivity_error(e):
        logger.error(f"Jenkins unreachable for {job_name} #{build_number}: {e!s}")
        raise JenkinsError(
            "Jenkins is unreachable or timed out. Check server connectivity.",
            status_code=504,
        )

    # For any other exception type
    raise JenkinsError(
        f"Failed to connect to Jenkins: {e!s}",
        status_code=502,
    )


def extract_failed_child_jobs(build_info: dict) -> list[tuple[str, int]]:
    """Extract failed child job names and build numbers from pipeline build info.

    Looks for failed jobs in subBuilds (Pipeline plugin) and triggeredBuilds
    (older Jenkins plugins).

    Args:
        build_info: Jenkins build information dictionary.

    Returns:
        List of (job_name, build_number) tuples for failed child jobs.
    """
    failed_jobs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    # Check for subBuilds in pipeline (Blue Ocean / Pipeline plugin)
    sub_builds = build_info.get("subBuilds", [])
    for sub in sub_builds:
        if sub.get("result") in ("FAILURE", "UNSTABLE"):
            job_name = sub.get("jobName", "")
            build_num = sub.get("buildNumber", 0)
            if job_name and build_num and (job_name, build_num) not in seen:
                failed_jobs.append((job_name, build_num))
                seen.add((job_name, build_num))

    # Also check actions for triggered builds (older Jenkins plugins)
    for action in build_info.get("actions", []):
        if action is None:
            continue
        action_class = action.get("_class", "")
        triggered_builds = action.get("triggeredBuilds", [])

        # Check for BuildAction or similar action types
        if triggered_builds or "BuildAction" in action_class:
            for triggered in triggered_builds:
                if triggered.get("result") in ("FAILURE", "UNSTABLE"):
                    # Try to get job name from different possible fields
                    job_name = triggered.get("jobName", "")
                    if not job_name:
                        # Try to parse from URL if available
                        url = triggered.get("url", "")
                        if url:
                            try:
                                job_name, _ = JenkinsClient.parse_jenkins_url(url)
                            except ValueError:
                                continue
                    build_num = triggered.get("number", triggered.get("buildNumber", 0))
                    if job_name and build_num and (job_name, build_num) not in seen:
                        failed_jobs.append((job_name, build_num))
                        seen.add((job_name, build_num))

    return failed_jobs


def extract_failed_child_jobs_from_console(
    console_output: str,
) -> list[tuple[str, int]]:
    """Extract failed child jobs from console output using regex.

    Looks for patterns like:
    - Build folder » job-name #123 completed: FAILURE
    - Build job-name #123 completed: FAILURE

    Args:
        console_output: The Jenkins console output text.

    Returns:
        List of (job_name, build_number) tuples for failed child jobs.
    """
    failed_jobs: list[tuple[str, int]] = []

    # Pattern: Build [job path] #[number] completed: FAILURE/UNSTABLE
    pattern = r"Build\s+(.+?)\s+#(\d+)\s+completed:\s*(FAILURE|UNSTABLE)"
    matches = re.findall(pattern, console_output)

    for match in matches:
        job_path = match[0].strip()
        build_num = int(match[1])
        # Convert "folder » job" to "folder/job" format for Jenkins API
        # Example: "mtv-base » mtv-deploy-dynamic" -> "mtv-base/mtv-deploy-dynamic"
        # The URL construction will handle adding /job/ segments for display
        job_name = job_path.replace(" » ", "/")
        failed_jobs.append((job_name, build_num))

    return failed_jobs


def extract_failures_from_test_report(test_report: dict) -> list[FailedTest]:
    """Extract failed test cases from Jenkins test report.

    Parses the structured test report from Jenkins /testReport/api/json endpoint
    and extracts all failed and regression tests.

    Args:
        test_report: Jenkins test report dictionary from the API.

    Returns:
        List of FailedTest objects containing test details.
    """
    failures: list[FailedTest] = []

    # Handle both top-level suites and nested childReports structure
    suites = test_report.get("suites", [])

    # Some Jenkins configurations use childReports instead of suites at top level
    child_reports = test_report.get("childReports", [])
    for child_report in child_reports:
        result = child_report.get("result", {})
        suites.extend(result.get("suites", []))

    for suite in suites:
        for case in suite.get("cases", []):
            status = case.get("status", "")
            if status in ("FAILED", "REGRESSION"):
                class_name = case.get("className", "")
                test_name = case.get("name", "")
                full_name = f"{class_name}.{test_name}" if class_name else test_name

                error_details = derive_error_details(
                    case.get("errorDetails", "") or "",
                    case.get("errorStackTrace", "") or "",
                )
                stack_trace = case.get("errorStackTrace", "") or ""

                failures.append(
                    FailedTest(
                        test_name=full_name,
                        error_message=error_details,
                        stack_trace=stack_trace,
                        duration=case.get("duration", 0.0) or 0.0,
                        status=status,
                    )
                )

    return failures


# ---------------------------------------------------------------------------
# JenkinsSource CI source plugin
# ---------------------------------------------------------------------------


class JenkinsSource(CISource):
    """CI source plugin for Jenkins.

    Connects to a Jenkins instance, fetches build information, console output,
    artifacts, and test reports, then returns a normalized ``CISourceResult``
    for the core analysis engine.
    """

    def __init__(
        self,
        job_name: str,
        build_number: int,
        settings: Settings,
        *,
        force: bool = False,
    ) -> None:
        """Store config needed to fetch from Jenkins.

        Args:
            job_name: Full Jenkins job name (may include folder separators).
            build_number: Build number to analyze.
            settings: Application settings containing Jenkins connection info.
            force: When True, analyze even if the build passed.
        """
        self.job_name = job_name
        self.build_number = build_number
        self.settings = settings
        self.force = force
        self._extract_path: Path | None = None
        self._client: JenkinsClient | None = None

    @property
    def client(self) -> JenkinsClient:
        """Lazy-create JenkinsClient."""
        if self._client is None:
            self._client = JenkinsClient(
                url=self.settings.jenkins_url,
                username=self.settings.jenkins_user,
                password=self.settings.jenkins_password,
                ssl_verify=self.settings.jenkins_ssl_verify,
                timeout=self.settings.jenkins_timeout,
            )
        return self._client

    @property
    def build_url(self) -> str:
        """Construct the Jenkins build URL."""
        job_path = "/job/".join(self.job_name.split("/"))
        return f"{self.settings.jenkins_url.rstrip('/')}/job/{job_path}/{self.build_number}/"

    async def fetch(self) -> CISourceResult:
        """Fetch build data from Jenkins and return normalized result.

        Steps (extracted from analyze_job top half):
          1. Get build_info — check if build passed (early return if SUCCESS
             and not force).
          2. Download artifacts (if ``get_job_artifacts`` is enabled).
          3. Get console output.
          4. Extract failed child jobs (from build_info, fallback to console
             parsing).
          5. Get test report and extract failures.
          6. Extract relevant console lines for context.
          7. Build and return ``CISourceResult``.
        """
        # ------------------------------------------------------------------
        # 1. Get build info (quick call) to check if build passed
        # ------------------------------------------------------------------
        build_info: dict = {}
        try:
            build_info = await asyncio.to_thread(
                self.client.get_build_info_safe, self.job_name, self.build_number
            )
        except Exception as e:
            handle_jenkins_exception(e, self.job_name, self.build_number)

        # Check if build passed — return early unless force is set
        build_result = build_info.get("result")
        if build_result == "SUCCESS" and not self.force:
            return CISourceResult(
                failures=[],
                build_passed=True,
                build_url=self.build_url,
            )
        if build_result == "SUCCESS" and self.force:
            logger.info(
                f"Build {self.job_name} #{self.build_number} passed but force=True, continuing analysis"
            )

        # ------------------------------------------------------------------
        # 2. Download build artifacts for context
        # ------------------------------------------------------------------
        artifacts_context = ""
        if self.settings.get_job_artifacts:
            artifacts = build_info.get("artifacts", [])
            build_url_from_info = build_info.get("url", "").rstrip("/")
            if artifacts and build_url_from_info:
                try:
                    artifacts_context, extract_path = await asyncio.to_thread(
                        process_build_artifacts,
                        self.client.session,
                        build_url_from_info,
                        artifacts,
                        self.settings.jenkins_artifacts_max_size_mb,
                    )
                    self._extract_path = extract_path
                except Exception as exc:
                    logger.warning(f"Failed to process artifacts: {exc}")

        # ------------------------------------------------------------------
        # 3. Fetch console output
        # ------------------------------------------------------------------
        console_output: str = ""
        try:
            console_output = await asyncio.to_thread(
                self.client.get_build_console, self.job_name, self.build_number
            )
        except Exception as e:
            handle_jenkins_exception(e, self.job_name, self.build_number)

        # ------------------------------------------------------------------
        # 4. Extract failed child jobs from build info / console
        # ------------------------------------------------------------------
        failed_child_jobs = extract_failed_child_jobs(build_info)

        # Fallback to console parsing if none found from build_info
        if not failed_child_jobs:
            failed_child_jobs = extract_failed_child_jobs_from_console(console_output)

        logger.debug(f"Extracted {len(failed_child_jobs)} failed child jobs")

        # ------------------------------------------------------------------
        # 5. Get test report and extract failures
        # ------------------------------------------------------------------
        test_report = None
        try:
            test_report = await asyncio.to_thread(
                self.client.get_test_report, self.job_name, self.build_number
            )
        except jenkins.NotFoundException:
            logger.info(
                "No test report for %s #%s; falling back to console-only analysis",
                self.job_name,
                self.build_number,
            )
        except Exception as exc:
            handle_jenkins_exception(exc, self.job_name, self.build_number)
        test_failures = (
            extract_failures_from_test_report(test_report) if test_report else []
        )
        logger.info(f"Found {len(test_failures)} test failures to analyze")

        # ------------------------------------------------------------------
        # 6. Extract relevant console lines for context
        # ------------------------------------------------------------------
        console_context = extract_relevant_console_lines(console_output)

        # ------------------------------------------------------------------
        # 7. Build and return CISourceResult
        # ------------------------------------------------------------------
        return CISourceResult(
            failures=test_failures,
            console_context=console_context,
            artifacts_context=artifacts_context,
            build_url=self.build_url,
            extract_path=self._extract_path,
            child_job_infos=failed_child_jobs,
        )

    def create_child_source(self, job_name: str, build_number: int) -> JenkinsSource:
        """Create a child JenkinsSource for pipeline child job analysis."""
        return JenkinsSource(
            job_name=job_name,
            build_number=build_number,
            settings=self.settings,
            force=self.force,
        )

    def cleanup(self) -> None:
        """Clean up artifact extraction directory."""
        if self._extract_path:
            cleanup_extract_dir(self._extract_path)
            self._extract_path = None


# ---------------------------------------------------------------------------
# Top-level Jenkins analysis orchestration
# ---------------------------------------------------------------------------


async def analyze_child_job(
    job_name: str,
    build_number: int,
    settings: Settings,
    depth: int = 0,
    max_depth: int = 3,
    repo_path: Path | None = None,
    ai_provider: str = "",
    ai_model: str = "",
    ai_cli_timeout: int | None = None,
    custom_prompt: str = "",
    artifacts_context: str = "",
    server_url: str = "",
    job_id: str = "",
    peer_ai_configs: list | None = None,
    peer_analysis_max_rounds: int = 3,
    additional_repos: dict[str, Path] | None = None,
    max_concurrent_ai_calls: int = 3,
    auth_header: str = "",
) -> ChildJobAnalysis:
    """Analyze a single child job, recursively analyzing its failed children.

    Each child job gets its own Claude CLI call to manage context size.

    Args:
        job_name: Name of the Jenkins job to analyze.
        build_number: Build number to analyze.
        settings: Application settings containing Jenkins connection info.
        depth: Current recursion depth (0 = direct child of main job).
        max_depth: Maximum recursion depth to prevent infinite loops.
        repo_path: Path to cloned test repository for source code lookup.
        ai_provider: AI provider to use.
        ai_model: AI model to use.
        ai_cli_timeout: Timeout in minutes (overrides AI_CLI_TIMEOUT env var).
        custom_prompt: Additional instructions from request payload (raw_prompt).
        artifacts_context: Jenkins artifacts context for AI analysis (optional).
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.
        peer_ai_configs: Peer AI configurations for multi-AI consensus analysis.
        peer_analysis_max_rounds: Maximum debate rounds for peer analysis (default: 3).
        additional_repos: Extra cloned repositories for AI context.
        max_concurrent_ai_calls: Maximum concurrent AI CLI processes (default: 3).

    Returns:
        ChildJobAnalysis with analysis results or nested child analyses.
    """
    # Use JenkinsSource to fetch build data
    source = JenkinsSource(
        job_name=job_name,
        build_number=build_number,
        settings=settings,
        force=True,  # Always analyze child jobs regardless of result
    )
    jenkins_url = source.build_url

    if depth >= max_depth:
        return ChildJobAnalysis(
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            note="Max depth reached - analysis stopped to prevent infinite recursion",
        )

    # Fetch build data via JenkinsSource
    try:
        source_result = await source.fetch()
    except Exception as e:
        source.cleanup()
        return ChildJobAnalysis(
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            note=f"Failed to get build info: {e}",
        )

    try:
        return await _analyze_child_job_inner(
            source=source,
            source_result=source_result,
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            settings=settings,
            depth=depth,
            max_depth=max_depth,
            repo_path=repo_path,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_cli_timeout=ai_cli_timeout,
            custom_prompt=custom_prompt,
            artifacts_context=artifacts_context,
            server_url=server_url,
            job_id=job_id,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=peer_analysis_max_rounds,
            additional_repos=additional_repos,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
            auth_header=auth_header,
        )
    finally:
        source.cleanup()


async def _analyze_child_job_inner(
    *,
    source: JenkinsSource,
    source_result: CISourceResult,
    job_name: str,
    build_number: int,
    jenkins_url: str,
    settings: Settings,
    depth: int,
    max_depth: int,
    repo_path: Path | None,
    ai_provider: str,
    ai_model: str,
    ai_cli_timeout: int | None,
    custom_prompt: str,
    artifacts_context: str,
    server_url: str,
    job_id: str,
    peer_ai_configs: list | None,
    peer_analysis_max_rounds: int,
    additional_repos: dict[str, Path] | None,
    max_concurrent_ai_calls: int,
    auth_header: str,
) -> ChildJobAnalysis:
    """Inner logic for analyze_child_job, separated to allow cleanup after completion."""
    child_artifacts_context = source_result.artifacts_context or artifacts_context
    failed_children = source_result.child_job_infos

    if failed_children:
        # Recursively analyze failed children IN PARALLEL with bounded concurrency
        child_tasks: list[Coroutine[Any, Any, Any]] = [
            analyze_child_job(
                child_name,
                child_num,
                settings,
                depth + 1,
                max_depth,
                repo_path,
                ai_provider,
                ai_model,
                ai_cli_timeout,
                custom_prompt,
                artifacts_context=child_artifacts_context,
                server_url=server_url,
                job_id=job_id,
                peer_ai_configs=peer_ai_configs,
                peer_analysis_max_rounds=peer_analysis_max_rounds,
                additional_repos=additional_repos,
                max_concurrent_ai_calls=max_concurrent_ai_calls,
                auth_header=auth_header,
            )
            for child_name, child_num in failed_children
        ]
        child_results = await run_parallel_with_limit(
            child_tasks, max_concurrency=max_concurrent_ai_calls
        )

        # Handle exceptions in results
        child_analyses = []
        for i, result in enumerate(child_results):
            if isinstance(result, Exception):
                child_name, child_num = failed_children[i]
                child_analyses.append(
                    ChildJobAnalysis(
                        job_name=child_name,
                        build_number=child_num,
                        jenkins_url="",
                        note=f"Analysis failed: {format_exception_with_type(result)}",
                    )
                )
            else:
                child_analyses.append(result)

        # This job failed because children failed - skip Claude CLI analysis
        # Count failures from child analyses
        total_failures = sum(len(child.failures) for child in child_analyses)
        summary = f"Pipeline failed due to {len(child_analyses)} child job(s)."
        if total_failures > 0:
            summary += f" Total: {total_failures} failure(s) analyzed. See child analyses below."

        return ChildJobAnalysis(
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            summary=summary,
            failures=[],  # Pipeline has no direct failures
            failed_children=child_analyses,
        )

    # No failed children - this is a leaf failure, analyze it directly
    test_failures = source_result.failures
    console_context = source_result.console_context

    # If we have test failures, group by signature and analyze unique groups
    if test_failures:
        # Group failures by signature to avoid analyzing identical errors multiple times
        failure_groups: dict[str, list[FailedTest]] = defaultdict(list)
        for tf in test_failures:
            sig = get_failure_signature(tf)
            failure_groups[sig].append(tf)

        logger.info(
            f"Grouped {len(test_failures)} failures into {len(failure_groups)} unique error types"
        )

        # Analyze each unique failure group in parallel
        total_groups = len(failure_groups)
        tasks: list[Coroutine[Any, Any, Any]] = []
        for group_idx, (_sig, group) in enumerate(failure_groups.items(), 1):
            tasks.append(
                analyze_failure_group(
                    failures=group,
                    console_context=console_context,
                    repo_path=repo_path,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    ai_cli_timeout=ai_cli_timeout,
                    custom_prompt=custom_prompt,
                    artifacts_context=child_artifacts_context,
                    server_url=server_url,
                    job_id=job_id,
                    peer_ai_configs=peer_ai_configs,
                    peer_analysis_max_rounds=peer_analysis_max_rounds,
                    group_label=f"{job_name}:{group_idx}/{total_groups}"
                    if total_groups > 1
                    else "",
                    additional_repos=additional_repos,
                    max_concurrent_ai_calls=max_concurrent_ai_calls,
                    auth_header=auth_header,
                )
            )
        group_results = await run_parallel_with_limit(
            tasks, max_concurrency=max_concurrent_ai_calls
        )

        # Flatten results and handle exceptions
        failures = []
        group_list = list(failure_groups.values())
        for i, result in enumerate(group_results):
            if isinstance(result, Exception):
                # Create error entries for all failures in this group
                for tf in group_list[i]:
                    failures.append(
                        FailureAnalysis(
                            test_name=tf.test_name,
                            error=tf.error_message,
                            analysis=AnalysisDetail(
                                details=f"Analysis failed: {format_exception_with_type(result)}"
                            ),
                            error_signature=get_failure_signature(tf),
                        )
                    )
            else:
                failures.extend(result)

        # Generate summary from parallel results
        total_failures = len(failures)
        unique_errors = len(failure_groups)

        # Include deduplication info in summary if applicable
        if unique_errors < total_failures:
            summary = (
                f"{total_failures} failure(s) analyzed "
                f"({unique_errors} unique error type(s))"
            )
        else:
            summary = f"{total_failures} failure(s) analyzed"

        return ChildJobAnalysis(
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            summary=summary,
            failures=failures,
        )

    # No structured test failures - fall back to single Claude CLI analysis of console output
    if peer_ai_configs:
        logger.warning(
            "Peer analysis not supported for console-only failures (no test report)"
        )

    custom_prompt_section, artifacts_section, resources_section, query_section = (
        build_prompt_sections(
            custom_prompt,
            child_artifacts_context,
            repo_path,
            server_url,
            job_id,
            additional_repos=additional_repos,
            auth_header=auth_header,
        )
    )

    prompt = f"""{query_section}
Analyze this failed Jenkins job:

Job: {job_name} #{build_number}

CONSOLE OUTPUT (errors/failures/warnings extracted):
{console_context}
{artifacts_section}

You have access to the repository if one was cloned. Explore to understand the failure.
{custom_prompt_section}{resources_section}
{JSON_RESPONSE_SCHEMA}
"""
    logger.debug(f"AI prompt length: {len(prompt)} chars")
    logger.info(f"Calling AI CLI with {format_timeout_log(ai_cli_timeout)}")
    result, parsed_analysis = await call_ai_and_record(
        prompt,
        job_id=job_id,
        call_type="child_console",
        cwd=repo_path,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
    )

    if parsed_analysis is None:
        parsed_analysis = AnalysisDetail(details=result.text)

    return ChildJobAnalysis(
        job_name=job_name,
        build_number=build_number,
        jenkins_url=jenkins_url,
        summary="Analysis complete",
        failures=[
            FailureAnalysis(
                test_name=f"{job_name}#{build_number}",
                error="Console-only analysis",
                analysis=parsed_analysis,
            )
        ],
    )


async def analyze_job(
    request: AnalyzeRequest,
    settings: Settings,
    ai_provider: str,
    ai_model: str,
    job_id: str | None = None,
    server_url: str = "",
    peer_ai_configs: list | None = None,
    peer_analysis_max_rounds: int = 3,
    auth_header: str = "",
) -> AnalysisResult:
    """Analyze a Jenkins job failure."""
    # Track whether the caller supplied a persisted job_id so we only
    # issue progress-phase writes for jobs that actually exist in the DB.
    progress_job_id = job_id
    if job_id is None:
        job_id = str(uuid.uuid4())

    job_name = request.job_name
    build_number = request.build_number
    logger.info(f"Starting analysis for job {job_name} #{build_number}")

    force = getattr(request, "force", False) or settings.force_analysis

    # Use JenkinsSource to fetch all build data (steps 1-8)
    source = JenkinsSource(
        job_name=job_name,
        build_number=build_number,
        settings=settings,
        force=force,
    )

    try:
        source_result = await source.fetch()

        jenkins_build_url = source.build_url

        if source_result.build_passed:
            return AnalysisResult(
                job_id=job_id,
                job_name=request.job_name,
                build_number=request.build_number,
                jenkins_url=HttpUrl(jenkins_build_url),
                status="completed",
                summary="Build passed successfully. No failures to analyze.",
                ai_provider=ai_provider,
                ai_model=ai_model,
                failures=[],
            )

        test_failures = source_result.failures
        console_context = source_result.console_context
        artifacts_context = source_result.artifacts_context
        failed_child_jobs = source_result.child_job_infos

        logger.debug(f"Extracted {len(failed_child_jobs)} failed child jobs")
        child_job_analyses: list[ChildJobAnalysis] = []

        # Clone repo for context BEFORE child job analysis so it's available for all jobs
        # Use request value if provided, otherwise fall back to settings
        tests_repo_url = request.tests_repo_url or settings.tests_repo_url
        tests_repo_token = resolve_tests_repo_token(request, settings)
        repo_context = ""
        custom_prompt = ""

        # Use RepositoryManager context for entire analysis (child jobs and main job)
        cloned_repos: dict[str, Path] = {}
        async with contextlib.AsyncExitStack() as stack:
            # Resolve additional repos list early so we know if a workspace is needed
            additional_repos_list = resolve_additional_repos(request, settings)

            repo_manager = RepositoryManager()
            stack.enter_context(repo_manager)
            repo_path = repo_manager.create_workspace()

            if tests_repo_url:
                try:
                    clean_tests_url, tests_ref = parse_repo_ref(str(tests_repo_url))
                    repo_name = derive_test_repo_name(
                        clean_tests_url, additional_repos_list
                    )
                    logger.info(
                        f"Cloning test repository: {redact_url(clean_tests_url)}"
                        + (f" (ref={tests_ref})" if tests_ref else "")
                    )
                    await asyncio.to_thread(
                        repo_manager.clone_into,
                        clean_tests_url,
                        repo_path / repo_name,
                        depth=50,
                        branch=tests_ref,
                        token=tests_repo_token or None,
                    )
                    cloned_repos[repo_name] = repo_path / repo_name
                    logger.info(
                        f"Successfully cloned test repository into {repo_name}/"
                    )
                    repo_context = f"\nTest repository cloned from: {redact_url(clean_tests_url)} (at {repo_name}/)"
                except Exception as e:  # non-fatal tests repo clone failure
                    logger.warning(
                        "Failed to clone repository (%s)",
                        type(e).__name__,
                    )
                    repo_context = "\nFailed to clone repository (details redacted)"

            custom_prompt = (request.raw_prompt or "").strip()

            # Make artifacts accessible in the AI working directory
            if source_result.extract_path:
                artifacts_link = repo_path / "build-artifacts"
                try:
                    artifacts_link.symlink_to(source_result.extract_path)
                    logger.info(f"Linked artifacts into workspace: {artifacts_link}")
                except OSError as exc:
                    logger.warning(f"Could not link artifacts into workspace: {exc}")

            # Clone additional repositories for AI context
            if additional_repos_list:
                additional_repos_cloned, repo_path = await clone_additional_repos(
                    repo_manager, additional_repos_list, repo_path
                )
                cloned_repos.update(additional_repos_cloned)

            # Pre-flight: verify AI CLI is reachable before spawning parallel tasks
            preflight_result = await check_ai_cli_available(
                ai_provider, ai_model, cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, [])
            )
            if not preflight_result.success:
                logger.error(
                    "AI CLI sanity check failed for job %s (%s/%s)",
                    job_id,
                    ai_provider,
                    ai_model,
                )
                return AnalysisResult(
                    job_id=job_id,
                    job_name=request.job_name,
                    build_number=request.build_number,
                    jenkins_url=HttpUrl(jenkins_build_url),
                    status="failed",
                    summary=preflight_result.text,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    failures=[],
                )

            # Analyze failed child jobs IN PARALLEL with bounded concurrency
            if failed_child_jobs:
                await safe_update_progress(progress_job_id, "analyzing_child_jobs")
                child_tasks: list[Coroutine[Any, Any, Any]] = [
                    analyze_child_job(
                        job_name=child_name,
                        build_number=child_num,
                        settings=settings,
                        depth=0,
                        max_depth=3,
                        repo_path=repo_path,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        ai_cli_timeout=settings.ai_cli_timeout,
                        custom_prompt=custom_prompt,
                        artifacts_context=artifacts_context,
                        server_url=server_url,
                        job_id=job_id,
                        peer_ai_configs=peer_ai_configs,
                        peer_analysis_max_rounds=peer_analysis_max_rounds,
                        additional_repos=cloned_repos or None,
                        max_concurrent_ai_calls=settings.max_concurrent_ai_calls,
                        auth_header=auth_header,
                    )
                    for child_name, child_num in failed_child_jobs
                ]
                child_results = await run_parallel_with_limit(
                    child_tasks, max_concurrency=settings.max_concurrent_ai_calls
                )

                # Handle exceptions in results
                for i, result in enumerate(child_results):
                    if isinstance(result, Exception):
                        child_name, child_num = failed_child_jobs[i]
                        child_job_analyses.append(
                            ChildJobAnalysis(
                                job_name=child_name,
                                build_number=child_num,
                                jenkins_url="",
                                note=f"Analysis failed: {format_exception_with_type(result)}",
                            )
                        )
                    else:
                        child_job_analyses.append(result)

            # If this job has failed children AND no test failures, it's a pipeline/orchestrator
            # Skip Claude CLI analysis - just return the child analyses
            if child_job_analyses and not test_failures:
                total_failures = sum(
                    len(child.failures) for child in child_job_analyses
                )
                summary = (
                    f"Pipeline failed due to {len(child_job_analyses)} child job(s)."
                )
                if total_failures > 0:
                    summary += f" Total: {total_failures} failure(s) analyzed. See child analyses below."

                return AnalysisResult(
                    job_id=job_id,
                    job_name=request.job_name,
                    build_number=request.build_number,
                    jenkins_url=HttpUrl(jenkins_build_url),
                    status="completed",
                    summary=summary,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    failures=[],  # Pipeline has no direct failures
                    child_job_analyses=child_job_analyses,
                )

            # Analyze main job test failures, grouping by signature to deduplicate
            unique_errors = 0
            if test_failures:
                await safe_update_progress(progress_job_id, "analyzing_failures")
                # Group failures by signature to avoid analyzing identical errors multiple times
                failure_groups: dict[str, list[FailedTest]] = defaultdict(list)
                for tf in test_failures:
                    sig = get_failure_signature(tf)
                    failure_groups[sig].append(tf)

                unique_errors = len(failure_groups)
                logger.info(
                    f"Grouped {len(test_failures)} failures into {unique_errors} unique error types"
                )

                # Analyze each unique failure group in parallel
                total_groups = len(failure_groups)
                failure_tasks: list[Coroutine[Any, Any, Any]] = []
                for group_idx, (_sig, group) in enumerate(failure_groups.items(), 1):
                    failure_tasks.append(
                        analyze_failure_group(
                            failures=group,
                            console_context=console_context,
                            repo_path=repo_path,
                            ai_provider=ai_provider,
                            ai_model=ai_model,
                            ai_cli_timeout=settings.ai_cli_timeout,
                            custom_prompt=custom_prompt,
                            artifacts_context=artifacts_context,
                            server_url=server_url,
                            job_id=job_id,
                            peer_ai_configs=peer_ai_configs,
                            peer_analysis_max_rounds=peer_analysis_max_rounds,
                            group_label=f"{group_idx}/{total_groups}"
                            if total_groups > 1
                            else "",
                            additional_repos=cloned_repos or None,
                            max_concurrent_ai_calls=settings.max_concurrent_ai_calls,
                            auth_header=auth_header,
                        )
                    )
                group_results = await run_parallel_with_limit(
                    failure_tasks, max_concurrency=settings.max_concurrent_ai_calls
                )

                # Flatten results and handle exceptions
                failures = []
                group_list = list(failure_groups.values())
                for i, result in enumerate(group_results):
                    if isinstance(result, Exception):
                        # Create error entries for all failures in this group
                        for tf in group_list[i]:
                            failures.append(
                                FailureAnalysis(
                                    test_name=tf.test_name,
                                    error=tf.error_message,
                                    error_signature=get_failure_signature(tf),
                                    analysis=AnalysisDetail(
                                        details=f"Analysis failed: {format_exception_with_type(result)}"
                                    ),
                                )
                            )
                    else:
                        failures.extend(result)
            else:
                # No structured test failures - fall back to single Claude CLI analysis
                if peer_ai_configs:
                    logger.warning(
                        "Peer analysis not supported for console-only failures (no test report)"
                    )

                (
                    custom_prompt_section,
                    artifacts_section,
                    resources_section,
                    query_section,
                ) = build_prompt_sections(
                    custom_prompt,
                    artifacts_context,
                    repo_path,
                    server_url,
                    job_id,
                    additional_repos=cloned_repos or None,
                    auth_header=auth_header,
                )

                prompt = f"""{query_section}
Analyze this failed Jenkins job:

Job: {job_name} #{build_number}

CONSOLE OUTPUT (errors/failures/warnings extracted):
{console_context}
{repo_context}
{artifacts_section}

You have access to the repository if one was cloned. Explore to understand the failure.
{custom_prompt_section}{resources_section}
{JSON_RESPONSE_SCHEMA}
"""
                logger.debug(f"AI prompt length: {len(prompt)} chars")
                logger.info(
                    f"Calling AI CLI with {format_timeout_log(settings.ai_cli_timeout)}"
                )
                result, parsed_console = await call_ai_and_record(
                    prompt,
                    job_id=job_id,
                    call_type="main_console",
                    cwd=repo_path,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    ai_cli_timeout=settings.ai_cli_timeout,
                    cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
                )

                if not result.success:
                    return AnalysisResult(
                        job_id=job_id,
                        job_name=request.job_name,
                        build_number=request.build_number,
                        jenkins_url=HttpUrl(jenkins_build_url),
                        status="failed",
                        summary=result.text,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        failures=[],
                        child_job_analyses=child_job_analyses,
                    )

                failures = [
                    FailureAnalysis(
                        test_name=f"{job_name}#{build_number}",
                        error="Console-only analysis",
                        analysis=parsed_console or AnalysisDetail(details=result.text),
                    )
                ]

            # Build summary from parallel results
            total_failures = len(failures)
            # Include deduplication info in summary if applicable
            if unique_errors > 0 and unique_errors < total_failures:
                summary = (
                    f"{total_failures} failure(s) analyzed "
                    f"({unique_errors} unique error type(s))"
                )
            else:
                summary = f"{total_failures} failure(s) analyzed"

            if child_job_analyses:
                summary = (
                    f"{summary}. Additionally, {len(child_job_analyses)} failed child "
                    f"job(s) were analyzed recursively."
                )

            logger.info(f"Analysis complete: {len(failures)} failures analyzed")
            return AnalysisResult(
                job_id=job_id,
                job_name=request.job_name,
                build_number=request.build_number,
                jenkins_url=HttpUrl(jenkins_build_url),
                status="completed",
                summary=summary,
                ai_provider=ai_provider,
                ai_model=ai_model,
                failures=failures,
                child_job_analyses=child_job_analyses,
            )
    finally:
        source.cleanup()


async def wait_for_jenkins_completion(
    jenkins_url: str,
    job_name: str,
    build_number: int,
    jenkins_user: str,
    jenkins_password: str,
    jenkins_ssl_verify: bool,
    poll_interval_minutes: int,
    max_wait_minutes: int,
    jenkins_timeout: int = 30,
    max_consecutive_failures: int = 5,
) -> tuple[bool, str]:
    """Poll Jenkins until the build finishes.

    Args:
        jenkins_url: Jenkins server base URL.
        job_name: Name of the Jenkins job.
        build_number: Build number to monitor.
        jenkins_user: Jenkins username for authentication.
        jenkins_password: Jenkins password or API token.
        jenkins_ssl_verify: Whether to verify SSL certificates.
        poll_interval_minutes: Minutes between polls.
        max_wait_minutes: Maximum minutes to wait before timing out.
            0 means no limit (poll forever until job finishes).
        jenkins_timeout: Jenkins API request timeout in seconds.
        max_consecutive_failures: Number of consecutive transient errors
            allowed before giving up. Defaults to 5.

    Returns:
        A tuple of (success, error_message). success is True if the build
        completed, False otherwise. error_message is empty on success.
    """
    client = JenkinsClient(
        url=jenkins_url,
        username=jenkins_user,
        password=jenkins_password,
        ssl_verify=jenkins_ssl_verify,
        timeout=jenkins_timeout,
    )

    unreachable_error = (
        "Cannot reach Jenkins; please verify the Jenkins URL, credentials, "
        "and network connectivity"
    )

    try:
        await asyncio.to_thread(client.get_whoami)
    except Exception as e:
        if is_jenkins_connectivity_error(e):
            logger.error(
                "Cannot reach Jenkins at %s: %s", jenkins_url, e, exc_info=True
            )
            return False, (
                "Jenkins connectivity error: unable to reach Jenkins; "
                "please verify the Jenkins URL and network connectivity"
            )
        else:
            logger.error(
                "Jenkins auth/permission failure at %s: %s",
                jenkins_url,
                e,
                exc_info=True,
            )
            return False, (
                "Jenkins authentication/permission error: please verify "
                "your credentials and access permissions"
            )

    if max_wait_minutes > 0:
        deadline: float | None = _time.monotonic() + max_wait_minutes * 60
    else:
        deadline = None  # No limit

    consecutive_failures = 0

    while True:
        try:
            build_info = await asyncio.to_thread(
                client.get_build_info_safe, job_name, build_number
            )
            consecutive_failures = 0

            if build_info and not build_info.get("building", True):
                logger.info(
                    f"Jenkins job {job_name} #{build_number} completed "
                    f"with result: {build_info.get('result')}"
                )
                return True, ""

            logger.info(f"Jenkins job {job_name} #{build_number} still running")

        except jenkins.NotFoundException:
            logger.error(
                f"Jenkins job {job_name} #{build_number} not found (404). "
                "Stopping poll."
            )
            return False, f"Jenkins job {job_name} #{build_number} not found (404)"

        except Exception as e:
            if not is_jenkins_connectivity_error(e):
                logger.error(
                    "Non-transient error checking Jenkins status", exc_info=True
                )
                return False, "Jenkins poll failed; check server logs for details"
            consecutive_failures += 1
            logger.warning(
                "Transient error checking Jenkins status (%d/%d): %s",
                consecutive_failures,
                max_consecutive_failures,
                e,
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "Cannot reach Jenkins at %s after %d consecutive failures",
                    jenkins_url,
                    consecutive_failures,
                    exc_info=True,
                )
                return False, unreachable_error

        if deadline is not None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval_minutes * 60, remaining))
        else:
            await asyncio.sleep(poll_interval_minutes * 60)

    error_msg = (
        f"Timed out waiting for Jenkins job {job_name} #{build_number} "
        f"after {max_wait_minutes} minutes"
    )
    logger.warning(error_msg)
    return False, error_msg
