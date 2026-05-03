import asyncio
import contextlib
import hashlib
import json
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Coroutine, NoReturn

from ai_cli_runner import (
    AIResult,
    call_ai_cli,
    check_ai_cli_available,
    run_parallel_with_limit,
)

import jenkins
from fastapi import HTTPException
from simple_logger.logger import get_logger

from jenkins_job_insight.config import Settings, parse_additional_repos, parse_repo_ref
from jenkins_job_insight.request_resolution import resolve_tests_repo_token
from jenkins_job_insight.jenkins_artifacts import (
    cleanup_extract_dir,
    process_build_artifacts,
)
from jenkins_job_insight.jenkins import JenkinsClient
from pydantic import HttpUrl

from jenkins_job_insight.models import (
    AdditionalRepo,
    AnalysisDetail,
    AnalysisResult,
    AnalyzeRequest,
    BaseAnalysisRequest,
    ChildJobAnalysis,
    CodeFix,
    FailureAnalysis,
    ProductBugReport,
    TestFailure,
)
from jenkins_job_insight.repository import (
    RepositoryManager,
    _redact_url,
    derive_test_repo_name,
)
from jenkins_job_insight.storage import update_progress_phase
from jenkins_job_insight.token_tracking import record_ai_usage

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


def resolve_additional_repos(
    request: BaseAnalysisRequest, settings: Settings
) -> list[AdditionalRepo]:
    """Resolve additional repos from request or settings.

    Request value takes priority over settings env var.
    Returns list of AdditionalRepo objects, or empty list.
    """
    if request.additional_repos is not None:
        return request.additional_repos

    parsed = parse_additional_repos(settings.additional_repos)
    return [AdditionalRepo(**r) for r in parsed] if parsed else []


async def clone_additional_repos(
    repo_manager: RepositoryManager,
    additional_repos_list: list[AdditionalRepo],
    repo_path: Path,
) -> tuple[dict[str, Path], Path]:
    """Clone additional repositories for AI analysis context.

    Clones all repos as subdirectories of the provided workspace path.

    Args:
        repo_manager: Repository manager for cloning.
        additional_repos_list: List of AdditionalRepo objects.
        repo_path: Workspace path (always provided by caller).

    Returns:
        Tuple of (cloned repos dict mapping name to path, repo_path).
    """
    cloned: dict[str, Path] = {}

    async def _clone_into_subdir(ar: AdditionalRepo) -> None:
        target = repo_path / ar.name
        try:
            await asyncio.to_thread(
                repo_manager.clone_into,
                str(ar.url),
                target,
                depth=1,
                branch=ar.ref,
                token=ar.token or None,
            )
            cloned[ar.name] = target
            logger.info(f"Cloned additional repo '{ar.name}' into {target}")
        except Exception as e:  # noqa: BLE001 — non-fatal additional repo clone failure
            logger.warning(
                "Failed to clone additional repo '%s' (%s)",
                ar.name,
                type(e).__name__,
            )

    await asyncio.gather(*[_clone_into_subdir(ar) for ar in additional_repos_list])

    return cloned, repo_path


async def _safe_update_progress(job_id: str | None, phase: str) -> None:
    """Best-effort progress update; failures are swallowed and logged."""
    if not job_id:
        return
    try:
        await update_progress_phase(job_id, phase)
    except Exception:
        logger.debug("Failed to update progress phase", exc_info=True)


def format_exception_with_type(exc: Exception) -> str:
    """Format an exception to always include its type name.

    Bare exceptions like ``FileNotFoundError("[Errno 2] No such file or
    directory")`` are ambiguous without the type.  This helper prefixes the
    message with the class name so log entries and stored error messages
    always identify *what kind* of error occurred.

    Args:
        exc: The exception to format.

    Returns:
        String in the form ``"ExceptionType: message"``.
    """
    return f"{type(exc).__name__}: {exc}"


FALLBACK_TAIL_LINES = 200

# Path to FAILURE_HISTORY_ANALYSIS.md — the AI reads it at runtime instead of injecting content into the prompt
_QUERY_MD_PATH = Path(__file__).parent / "ai-prompts" / "FAILURE_HISTORY_ANALYSIS.md"

JOB_INSIGHT_PROMPT_FILENAME = "JOB_INSIGHT_PROMPT.md"
JOB_INSIGHT_ISSUE_PROMPT_FILENAME = "JOB_INSIGHT_ISSUE_PROMPT.md"
JOB_INSIGHT_FAILURE_HISTORY_PROMPT_FILENAME = (
    "JOB_INSIGHT_FAILURE_HISTORY_ANALYSIS_PROMPT.md"
)


# CLI flags that were previously hardcoded in provider command builders.
# The ai-cli-runner package handles structural flags (-p for claude, --print
# for cursor) internally; these are the extra per-provider flags.
PROVIDER_CLI_FLAGS: dict[str, list[str]] = {
    "claude": ["--dangerously-skip-permissions"],
    "gemini": ["--yolo"],
    "cursor": ["--force"],
}

# Known transient AI CLI errors that are retried (up to max_retries times).
# Add new patterns here when new transient failures are discovered.
RETRYABLE_AI_CLI_PATTERNS: list[str] = [
    "ENOENT: no such file or directory",  # Cursor CLI config race condition
]

# Pattern for error detection in console output (word boundaries, case-insensitive)
_CONSOLE_ERROR_PATTERN = re.compile(
    r"\b(?:errors?|fail(?:ed|ures?)?|tracebacks?|warn(?:ings?)?|critical|fatal|assert(?:ion)?(?:error)?s?)\b"
    r"|(?:^|[\s\[])[A-Za-z_][\w.]*?(?:error|exception)(?=[:\s\]]|$)",
    re.IGNORECASE,
)


async def _call_ai_and_record(
    prompt: str,
    *,
    job_id: str,
    call_type: str,
    cwd: Path | None = None,
    ai_provider: str = "",
    ai_model: str = "",
    ai_cli_timeout: int | None = None,
    cli_flags: list[str] | None = None,
) -> tuple[AIResult, AnalysisDetail | None]:
    """Call AI CLI with retry, record token usage, and parse the response.

    Returns ``(result, parsed_analysis)``.  ``parsed_analysis`` is ``None``
    when the AI call failed (``result.success is False``).
    """
    result = await _call_ai_cli_with_retry(
        prompt,
        cwd=cwd,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        cli_flags=cli_flags,
    )

    await record_ai_usage(
        job_id=job_id,
        result=result,
        call_type=call_type,
        prompt_chars=len(prompt),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )

    parsed: AnalysisDetail | None = None
    if result.success:
        parsed = _parse_json_response(result.text)
        if parsed is None:
            parsed = AnalysisDetail(details=result.text)

    return result, parsed


async def _call_ai_cli_with_retry(
    prompt: str,
    *,
    cwd: Path | None = None,
    ai_provider: str = "",
    ai_model: str = "",
    ai_cli_timeout: int | None = None,
    cli_flags: list[str] | None = None,
    max_retries: int = 3,
) -> AIResult:
    """Call AI CLI with retry on known transient errors.

    Wraps :func:`call_ai_cli` with a simple retry loop that re-attempts the
    call when the output matches one of :data:`RETRYABLE_AI_CLI_PATTERNS`.

    Args:
        prompt: The prompt to send to the AI CLI.
        cwd: Working directory for the CLI process.
        ai_provider: AI provider name (e.g. ``"claude"``).
        ai_model: Model identifier passed to the CLI.
        ai_cli_timeout: Timeout in minutes for the CLI process.
        cli_flags: Extra CLI flags forwarded to the provider.
        max_retries: Maximum number of retry attempts after the initial call.

    Returns:
        AIResult from the final attempt.
    """
    result = AIResult(success=False, text="")
    for attempt in range(max_retries + 1):
        result = await call_ai_cli(
            prompt,
            cwd=cwd,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_cli_timeout=ai_cli_timeout,
            cli_flags=cli_flags,
            output_format="json",
        )
        if result.success:
            return result
        # Check if the error matches a known retryable pattern
        if attempt < max_retries and any(
            pattern in result.text for pattern in RETRYABLE_AI_CLI_PATTERNS
        ):
            logger.warning(
                f"AI CLI transient error (attempt {attempt + 1}/{max_retries + 1}), retrying: {result.text}"
            )
            await asyncio.sleep(2**attempt)  # Exponential backoff: 1s, 2s, 4s
            continue
        return result
    return result  # Should not reach here, but satisfy type checker


_JSON_RESPONSE_SCHEMA = """CRITICAL: Your response must be ONLY a valid JSON object. No text before or after. No markdown code blocks. No explanation.

If CODE ISSUE:
{
  "classification": "CODE ISSUE",
  "affected_tests": ["test_name_1", "test_name_2"],
  "details": "Your detailed analysis of what caused this failure. Use paragraph breaks (double newlines) to separate sections: root cause identification, evidence from logs/code, and impact assessment. Do NOT write one continuous paragraph.",
  "artifacts_evidence": "VERBATIM lines from files under build-artifacts/ that support your analysis. Format each line as [file-path]: content. Example: [build-artifacts/logs/app.log]: 2026-03-16 INFO Service started successfully. Include evidence showing the product is healthy or that the test code caused the failure. Separate distinct artifact entries with paragraph breaks (double newlines).",
  "code_fix": {
    "file": "exact/file/path.py",
    "line": "line number",
    "change": "specific code change that fixes all affected tests",
    "original_code": "optional complete current contents of exact/file/path.py for diff/editor display (raw code string, NO markdown formatting)",
    "suggested_code": "complete replacement contents of exact/file/path.py after applying the fix (raw code string, NO markdown formatting)",
    "tests_repo_search_keywords": ["specific error symptom", "component + behavior", "error type"]
  }
}

tests_repo_search_keywords rules:
- Generate 3-5 SHORT specific keywords for finding matching issues in the tests repository
- Focus on the specific error symptom and broken behavior from the test code perspective
- Combine component name with the specific failure (e.g. "fixture setup timeout", "API mock validation error")
- AVOID generic/broad terms alone like "timeout", "failure", "error"
- Each keyword should be specific enough to narrow GitHub issue search results to relevant issues
- Think: "what would someone title a GitHub issue for this exact test code problem?"

If PRODUCT BUG:
{
  "classification": "PRODUCT BUG",
  "affected_tests": ["test_name_1", "test_name_2"],
  "details": "Your detailed analysis of what caused this failure. Use paragraph breaks (double newlines) to separate sections: root cause identification, evidence from logs/code, and impact assessment. Do NOT write one continuous paragraph.",
  "artifacts_evidence": "VERBATIM lines from files under build-artifacts/ that prove the product defect. Format each line as [file-path]: content. Example: [build-artifacts/logs/error.log]: 2026-03-16 ERROR NullPointerException in AuthService. Include the specific log lines showing the product failure. Separate distinct artifact entries with paragraph breaks (double newlines).",
  "product_bug_report": {
    "title": "concise bug title",
    "severity": "critical/high/medium/low",
    "component": "affected component",
    "description": "what product behavior is broken. Use paragraph breaks between sections.",
    "evidence": "relevant log snippets",
    "jira_search_keywords": ["specific error symptom", "component + behavior", "error type"]
  }
}

jira_search_keywords rules:
- Generate 3-5 SHORT specific keywords for finding matching bugs in Jira
- Focus on the specific error symptom and broken behavior, NOT test infrastructure
- Combine component name with the specific failure (e.g. "VM start failure migration", "API timeout authentication")
- AVOID generic/broad terms alone like "timeout", "failure", "error"
- Each keyword should be specific enough to narrow Jira search results to relevant bugs
- Think: "what would someone title a Jira bug for this exact issue?\""""


def _format_timeout_log(timeout_value: int | None) -> str:
    """Format AI CLI timeout for log messages."""
    if timeout_value is not None:
        return f"timeout={timeout_value} minutes ({timeout_value * 60}s)"
    return "timeout=default"


def _build_artifacts_section(artifacts_context: str) -> str:
    """Build the artifact context prompt section."""
    if not artifacts_context:
        return ""
    return f"""

=== BUILD ARTIFACTS ===
The following is a PREVIEW of the build-artifacts/ directory structure and file listing. File contents are not inlined here; open the files under build-artifacts/ in your working directory to inspect them.

{artifacts_context}

IMPORTANT INSTRUCTIONS FOR ARTIFACT ANALYSIS:
1. READ the actual files under build-artifacts/ — the listing above is incomplete and does not include file contents
2. Look for error messages, stack traces, service logs, and status information
3. In your artifacts_evidence field, include VERBATIM lines with the file path, e.g.: [build-artifacts/logs/app.log]: actual error line here
4. Do NOT classify based solely on the test error message — check the artifact logs for the real root cause"""


def get_failure_signature(failure: TestFailure) -> str:
    """Create a signature for grouping identical failures.

    Uses error message and first few lines of stack trace to identify
    failures that are essentially the same issue.

    Args:
        failure: The test failure to create a signature for.

    Returns:
        SHA-256 hash string representing the failure signature.
    """
    # Use error message and first 5 lines of stack trace for deduplication.
    # Intentionally limited to 5 lines: different stack depths for the same
    # root cause (e.g., varying call-site depth) should still collapse into
    # one group so the AI analyzes each unique error only once.
    stack_lines = failure.stack_trace.split("\n")[:5]
    signature_text = f"{failure.error_message}|{'|'.join(stack_lines)}"
    return hashlib.sha256(signature_text.encode()).hexdigest()


def _parse_json_response(raw_text: str) -> AnalysisDetail:
    """Parse AI CLI JSON response into an AnalysisDetail.

    Attempts to extract a JSON object from the AI response text.
    The AI may wrap the JSON in markdown code blocks, add
    surrounding text, or embed code blocks inside JSON string values.

    Uses a multi-strategy approach:
    1. Try parsing the raw text directly as JSON
    2. Try extracting JSON from brace-matching ({...})
    3. Try extracting from markdown code blocks
    4. Fallback: store raw text in details, then attempt recovery

    Args:
        raw_text: The raw text output from the AI CLI.

    Returns:
        An AnalysisDetail instance parsed from the JSON, or a
        fallback instance with the raw text stored in details.
    """
    text = raw_text.strip()

    # Strategy 1: Try parsing the entire text as JSON directly
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return AnalysisDetail(**data)
        except Exception:
            pass

    # Strategy 2: Find the outermost JSON object using brace matching
    result = _extract_json_by_braces(text)
    if result is not None:
        return result

    # Strategy 3: Try markdown code block extraction
    # Find ALL ```json or ``` blocks and try each one
    result = _extract_json_from_code_blocks(text)
    if result is not None:
        return result

    # Fallback: store raw text in details, then attempt recovery
    fallback = AnalysisDetail(details=raw_text)
    return _recover_from_details(fallback)


def _decode_recovered_json_string(value: str) -> str:
    """Decode a JSON string fragment captured by regex recovery."""
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\n", "\n")


def _extract_string_array_field(details: str, field_name: str) -> list[str]:
    """Extract a JSON string-array field from raw AI response text.

    Searches for ``"field_name": [...]`` via regex, parses the array
    content, strips whitespace, removes empty/whitespace-only entries,
    and deduplicates while preserving order.

    Args:
        details: Raw AI response text.
        field_name: JSON field name whose value is a string array.

    Returns:
        Deduplicated list of non-empty strings, or ``[]`` on failure.
    """
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*\[([^\]]*)\]', details)
    if not match:
        return []
    raw = re.findall(r'"([^"]+)"', match.group(1))
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        stripped = item.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            result.append(stripped)
    return result


def _recover_from_details(result: AnalysisDetail) -> AnalysisDetail:
    """Attempt to recover structured fields from a fallback result.

    When the main parsing strategies fail and raw text is stored in
    the details field, this function checks if that text contains
    JSON field patterns and extracts them via regex.

    This handles cases where the AI returned JSON with formatting
    issues (unescaped newlines, embedded code blocks) that broke
    standard JSON parsing.

    Args:
        result: An AnalysisDetail with raw text in the details field.

    Returns:
        Either a recovered AnalysisDetail with populated fields,
        or the original fallback result unchanged.
    """
    if result.classification:
        return result

    details = result.details
    if not details or '"classification"' not in details:
        return result

    # Extract classification
    class_match = re.search(r'"classification"\s*:\s*"([^"]+)"', details)
    if not class_match:
        return result

    classification = class_match.group(1)

    # Extract affected_tests
    affected_tests: list[str] = []
    tests_match = re.search(r'"affected_tests"\s*:\s*\[([^\]]*)\]', details)
    if tests_match:
        affected_tests = re.findall(r'"([^"]+)"', tests_match.group(1))

    # Extract details text from within the JSON
    details_match = re.search(
        r'"details"\s*:\s*"((?:[^"\\]|\\.)*)"', details, re.DOTALL
    )
    analysis_text = (
        details_match.group(1).replace("\\n", "\n") if details_match else details
    )

    # Extract code_fix if present
    code_fix: CodeFix | bool | None = False
    file_match = re.search(r'"file"\s*:\s*"([^"]*)"', details)
    change_match = re.search(r'"change"\s*:\s*"((?:[^"\\]|\\.)*)"', details)
    if file_match and change_match:
        line_match = re.search(r'"line"\s*:\s*"([^"]*)"', details)
        original_code_match = re.search(
            r'"original_code"\s*:\s*"((?:[^"\\]|\\.)*)"', details, re.DOTALL
        )
        suggested_code_match = re.search(
            r'"suggested_code"\s*:\s*"((?:[^"\\]|\\.)*)"', details, re.DOTALL
        )
        tests_repo_keywords = _extract_string_array_field(
            details, "tests_repo_search_keywords"
        )
        code_fix = CodeFix(
            file=file_match.group(1),
            line=line_match.group(1) if line_match else "",
            change=change_match.group(1).replace("\\n", "\n"),
            original_code=(
                _decode_recovered_json_string(original_code_match.group(1))
                if original_code_match
                else None
            ),
            suggested_code=(
                _decode_recovered_json_string(suggested_code_match.group(1))
                if suggested_code_match
                else None
            ),
            tests_repo_search_keywords=tests_repo_keywords,
        )

    # Extract artifacts_evidence (top-level field)
    artifacts_evidence_match = re.search(
        r'"artifacts_evidence"\s*:\s*"((?:[^"\\]|\\.)*)"', details, re.DOTALL
    )

    # Extract product_bug_report if present
    product_bug_report: ProductBugReport | bool | None = False
    title_match = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', details)
    if title_match and "PRODUCT BUG" in classification.upper():
        severity_match = re.search(r'"severity"\s*:\s*"([^"]*)"', details)
        component_match = re.search(r'"component"\s*:\s*"([^"]*)"', details)
        desc_match = re.search(
            r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', details, re.DOTALL
        )
        evidence_match = re.search(
            r'"evidence"\s*:\s*"((?:[^"\\]|\\.)*)"', details, re.DOTALL
        )
        jira_keywords = _extract_string_array_field(details, "jira_search_keywords")

        product_bug_report = ProductBugReport(
            title=title_match.group(1),
            severity=severity_match.group(1) if severity_match else "",
            component=component_match.group(1) if component_match else "",
            description=(
                desc_match.group(1).replace("\\n", "\n") if desc_match else ""
            ),
            evidence=(
                evidence_match.group(1).replace("\\n", "\n") if evidence_match else ""
            ),
            jira_search_keywords=jira_keywords,
        )

    logger.warning(
        "Recovered classification '%s' from unparseable AI response via regex extraction",
        classification,
    )
    return AnalysisDetail(
        classification=classification,
        affected_tests=affected_tests,
        details=analysis_text,
        artifacts_evidence=(
            artifacts_evidence_match.group(1).replace("\\n", "\n")
            if artifacts_evidence_match
            else ""
        ),
        code_fix=code_fix,
        product_bug_report=product_bug_report,
    )


def _extract_json_by_braces(text: str) -> AnalysisDetail | None:
    """Extract JSON by finding matching outermost braces.

    Handles cases where JSON values contain embedded code blocks
    or other special characters by tracking brace nesting depth
    and string boundaries.

    Args:
        text: Text potentially containing a JSON object.

    Returns:
        Parsed AnalysisDetail or None if extraction fails.
    """
    first_brace = text.find("{")
    if first_brace == -1:
        return None

    # Track brace depth to find the matching closing brace
    depth = 0
    in_string = False
    escape_next = False
    end_pos = -1

    for i in range(first_brace, len(text)):
        char = text[i]

        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            if in_string:
                escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end_pos = i
                break

    if end_pos == -1:
        return None

    json_str = text[first_brace : end_pos + 1]
    try:
        data = json.loads(json_str)
        return AnalysisDetail(**data)
    except Exception:
        return None


def _extract_json_from_code_blocks(text: str) -> AnalysisDetail | None:
    """Extract JSON from markdown code blocks in the text.

    Finds code blocks (```json or ```) and attempts to parse
    each one as JSON. Uses brace matching within each block
    to handle embedded code blocks in JSON string values.

    Args:
        text: Text containing markdown code blocks.

    Returns:
        Parsed AnalysisDetail or None if no valid JSON found.
    """
    # Find all code block positions using a pattern that matches
    # opening ``` markers (with optional language tag)
    blocks = re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)

    for block_content in blocks:
        block_content = block_content.strip()
        if not block_content or "{" not in block_content:
            continue

        # Try parsing the block content directly
        try:
            data = json.loads(block_content)
            return AnalysisDetail(**data)
        except Exception:
            pass

        # Try brace matching within the block
        result = _extract_json_by_braces(block_content)
        if result is not None:
            return result

    return None


def extract_relevant_console_lines(console_output: str) -> str:
    """Extract only error, failure, and warning lines from console output.

    When no structured test report is available, we need to extract
    relevant information from the console without sending the entire log.

    Args:
        console_output: Full Jenkins console output.

    Returns:
        Extracted relevant lines (errors, failures, warnings, exceptions).
    """
    relevant_lines: list[str] = []
    lines = console_output.splitlines()

    # Track lines we've already added to avoid duplicates
    seen_indices: set[int] = set()
    in_traceback = False

    for i, line in enumerate(lines):
        # Check if line matches error pattern (word boundaries, case-insensitive)
        if _CONSOLE_ERROR_PATTERN.search(line):
            # Add some context: 2 lines before
            start = max(0, i - 2)
            for j in range(start, i):
                if j not in seen_indices:
                    relevant_lines.append(lines[j])
                    seen_indices.add(j)
            # Add the error line itself (with duplicate check)
            if i not in seen_indices:
                relevant_lines.append(line)
                seen_indices.add(i)
            in_traceback = True
        elif in_traceback:
            # Continue capturing indented lines (stack trace)
            if line.startswith((" ", "\t")) or line.strip() == "":
                if i not in seen_indices:
                    relevant_lines.append(line)
                    seen_indices.add(i)
            else:
                in_traceback = False

    if relevant_lines:
        return "\n".join(relevant_lines)

    # Fallback: if nothing found, return last N lines (likely has the failure info)
    return (
        "\n".join(lines[-FALLBACK_TAIL_LINES:])
        if len(lines) > FALLBACK_TAIL_LINES
        else console_output
    )


def handle_jenkins_exception(
    e: Exception, job_name: str, build_number: int
) -> NoReturn:
    """Convert Jenkins exceptions to appropriate HTTPExceptions.

    Args:
        e: The exception raised by the Jenkins client.
        job_name: Name of the Jenkins job being accessed.
        build_number: Build number being accessed.

    Raises:
        HTTPException: With appropriate status code and detail message.
    """
    if isinstance(e, jenkins.NotFoundException):
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_name}' build #{build_number} not found in Jenkins",
        )

    if isinstance(e, jenkins.JenkinsException):
        error_msg = str(e).lower()
        if (
            "does not exist" in error_msg
            or "not found" in error_msg
            or "404" in error_msg
        ):
            raise HTTPException(
                status_code=404,
                detail=f"Job '{job_name}' build #{build_number} not found in Jenkins",
            )
        elif "unauthorized" in error_msg or "401" in error_msg:
            raise HTTPException(
                status_code=502,
                detail="Jenkins authentication failed. Check JENKINS_USER and JENKINS_PASSWORD.",
            )
        elif "forbidden" in error_msg or "403" in error_msg:
            raise HTTPException(
                status_code=502,
                detail=f"Access denied to job '{job_name}'. Check Jenkins permissions.",
            )
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Jenkins error: {e!s}",
            )

    from jenkins_job_insight.utils import is_jenkins_connectivity_error

    if is_jenkins_connectivity_error(e):
        logger.error(f"Jenkins unreachable for {job_name} #{build_number}: {e!s}")
        raise HTTPException(
            status_code=504,
            detail="Jenkins is unreachable or timed out. Check server connectivity.",
        )

    # For any other exception type
    raise HTTPException(
        status_code=502,
        detail=f"Failed to connect to Jenkins: {e!s}",
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

    # Check for subBuilds in pipeline (Blue Ocean / Pipeline plugin)
    sub_builds = build_info.get("subBuilds", [])
    for sub in sub_builds:
        if sub.get("result") in ("FAILURE", "UNSTABLE"):
            job_name = sub.get("jobName", "")
            build_num = sub.get("buildNumber", 0)
            if job_name and build_num:
                failed_jobs.append((job_name, build_num))

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
                    if job_name and build_num:
                        failed_jobs.append((job_name, build_num))

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


def _derive_error_details(error_details: str, stack_trace: str) -> str:
    """Derive a usable error message from *error_details* and *stack_trace*.

    1. Strip whitespace from *error_details*; if non-empty, return it.
    2. Otherwise, collect all non-file:line lines from *stack_trace* and join
       them with spaces to produce a synthetic error summary.
    3. Return the result (may still be empty if both inputs are blank).
    """
    stripped = error_details.strip()
    if stripped:
        return stripped

    if stack_trace:
        parts: list[str] = []
        for line in stack_trace.split("\n"):
            line_stripped = line.strip()
            if line_stripped and not re.match(r"^[\w/._-]+\.\w+:\d+$", line_stripped):
                parts.append(line_stripped)
        if parts:
            return " ".join(parts)

    return stripped


def extract_failures_from_test_report(test_report: dict) -> list[TestFailure]:
    """Extract failed test cases from Jenkins test report.

    Parses the structured test report from Jenkins /testReport/api/json endpoint
    and extracts all failed and regression tests.

    Args:
        test_report: Jenkins test report dictionary from the API.

    Returns:
        List of TestFailure objects containing test details.
    """
    failures: list[TestFailure] = []

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

                error_details = _derive_error_details(
                    case.get("errorDetails", "") or "",
                    case.get("errorStackTrace", "") or "",
                )
                stack_trace = case.get("errorStackTrace", "") or ""

                failures.append(
                    TestFailure(
                        test_name=full_name,
                        error_message=error_details,
                        stack_trace=stack_trace,
                        duration=case.get("duration", 0.0) or 0.0,
                        status=status,
                    )
                )

    return failures


def _build_prompt_sections(
    custom_prompt: str,
    artifacts_context: str,
    repo_path: Path | None,
    server_url: str,
    job_id: str,
    *,
    additional_repos: dict[str, Path] | None = None,
) -> tuple[str, str, str, str]:
    """Build common prompt sections used across all analysis flows.

    Returns:
        Tuple of (custom_prompt_section, artifacts_section, resources_section, query_section)
    """
    custom_prompt_section = (
        f"\n\nADDITIONAL INSTRUCTIONS:\n{custom_prompt}\n" if custom_prompt else ""
    )

    artifacts_section = _build_artifacts_section(artifacts_context)
    history_enabled = bool(server_url and job_id and _QUERY_MD_PATH.exists())
    resources_section = _build_resources_section(
        repo_path, additional_repos=additional_repos, history_enabled=history_enabled
    )

    if not _QUERY_MD_PATH.exists():
        logger.warning(
            f"History analysis prompt file not found at {_QUERY_MD_PATH}; "
            "analysis will proceed without history-aware classification"
        )
    if not server_url:
        logger.warning(
            "server_url is empty; analysis will proceed without history-aware classification"
        )
    if server_url and not job_id:
        logger.warning(
            "job_id is empty; disabling history-aware classification to avoid unscoped history queries"
        )

    query_section = ""
    if history_enabled:
        logger.info(
            f"Pointing AI to FAILURE_HISTORY_ANALYSIS.md with server_url={server_url}"
        )
        repo_history_prompt = ""
        # Scan cloned repos (not workspace root) for history prompt
        if additional_repos:
            for _name, _path in additional_repos.items():
                repo_history_path = _path / JOB_INSIGHT_FAILURE_HISTORY_PROMPT_FILENAME
                logger.debug(
                    f"Repo history analysis prompt exists at {_path}: {repo_history_path.exists()}"
                )
                if repo_history_path.exists():
                    logger.info(
                        f"Found repo-level history analysis prompt at {repo_history_path}"
                    )
                    repo_history_prompt += f"""
Also read and follow the project-specific history analysis instructions at {repo_history_path}.
These instructions complement (do not replace) the main instructions above.
"""
        elif repo_path:
            logger.debug(
                "No additional repos provided, skipping repo history prompt check"
            )
        else:
            logger.debug("No repo path provided, skipping repo history prompt check")

        query_section = f"""

MANDATORY: Before analyzing any failure, you MUST read and follow the instructions in {_QUERY_MD_PATH}.
When executing curl commands from that file, use server_url={server_url} and job_id={job_id}.
These instructions are NOT optional. You MUST complete ALL steps for EVERY test.
{repo_history_prompt}
"""

    return custom_prompt_section, artifacts_section, resources_section, query_section


def _build_resources_section(
    repo_path: Path | None,
    *,
    additional_repos: dict[str, Path] | None = None,
    history_enabled: bool = False,
) -> str:
    """Build a section telling the AI about available resources.

    Instead of pre-fetching data (git log, custom prompt files), this tells the
    AI what tools and files are available so it can access them on its own.

    Args:
        repo_path: Path to workspace root (all repos are subdirectories), or None.
        additional_repos: Mapping of repo name to cloned path.
        history_enabled: Whether failure history analysis is active.
            When False, the history prompt file is not advertised.

    Returns:
        Formatted resources section for the AI prompt, or empty string.
    """
    if not repo_path:
        return ""

    resources: list[str] = []

    # Workspace directory
    resources.append(
        f"- Workspace at {repo_path} — all repositories are cloned as subdirectories here"
    )

    # Advertise each cloned repo
    if additional_repos:
        for name, path in additional_repos.items():
            is_git = (path / ".git").exists()
            if is_git:
                resources.append(
                    f"- Repository '{name}' at {path} — explore source code, run git commands"
                )
            else:
                resources.append(
                    f"- Directory '{name}' at {path} — inspect files directly"
                )
            # Check for project-specific instructions in each repo
            job_insight_prompt = path / JOB_INSIGHT_PROMPT_FILENAME
            if job_insight_prompt.exists():
                resources.append(
                    f"- Project-specific analysis instructions at {job_insight_prompt} — read and follow them"
                )
            repo_history_prompt = path / JOB_INSIGHT_FAILURE_HISTORY_PROMPT_FILENAME
            if history_enabled and repo_history_prompt.exists():
                resources.append(
                    f"- Project-specific history analysis instructions at {repo_history_prompt} — read and follow alongside the main history analysis instructions"
                )

    if resources:
        return "\n\nAVAILABLE RESOURCES:\n" + "\n".join(resources) + "\n"

    return ""


async def _run_single_ai_analysis(
    *,
    failures: list[TestFailure],
    console_context: str,
    repo_path: Path | None,
    ai_provider: str,
    ai_model: str,
    ai_cli_timeout: int | None,
    custom_prompt: str,
    artifacts_context: str,
    server_url: str,
    job_id: str,
    additional_repos: dict[str, Path] | None = None,
) -> tuple[AnalysisDetail, str]:
    """Run single-AI analysis on a failure group. Returns (parsed_analysis, error_signature).

    Shared by both single-AI and peer analysis paths. Builds the orchestrator
    prompt, calls the AI CLI, and parses the response.

    Args:
        failures: List of test failures with the same error signature.
        console_context: Relevant console lines for context.
        repo_path: Path to cloned test repo (optional).
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        ai_cli_timeout: Timeout in minutes for the CLI process.
        custom_prompt: Additional user instructions.
        artifacts_context: Jenkins artifacts context.
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.

    Returns:
        Tuple of (parsed AnalysisDetail, error_signature string).
    """
    representative = failures[0]
    error_signature = get_failure_signature(representative)
    test_names = [f.test_name for f in failures]

    custom_prompt_section, artifacts_section, resources_section, query_section = (
        _build_prompt_sections(
            custom_prompt,
            artifacts_context,
            repo_path,
            server_url,
            job_id,
            additional_repos=additional_repos,
        )
    )

    has_git_repo = bool(
        additional_repos
        and any((p / ".git").exists() for p in additional_repos.values())
    )
    repo_sentence = (
        "You have access to the test repository. Explore the code to understand the failure."
        if has_git_repo
        else "No test repository is available. Base your analysis on the console output and artifacts context provided."
    )

    prompt = f"""{query_section}
Analyze this test failure from a Jenkins CI job.

ERROR SIGNATURE: {error_signature}

AFFECTED TESTS ({len(failures)} tests with same error):
{chr(10).join(f"- {name}" for name in test_names)}

ERROR: {representative.error_message}
STACK TRACE:
{representative.stack_trace}

CONSOLE CONTEXT:
{console_context}
{artifacts_section}

{repo_sentence}

Note: Multiple tests failed with the same error. Provide ONE analysis that applies to all of them.
{custom_prompt_section}{resources_section}
{_JSON_RESPONSE_SCHEMA}
"""

    if artifacts_context:
        logger.info(
            f"Prompt includes Jenkins artifacts context ({len(artifacts_context)} chars)"
        )

    logger.debug(f"AI prompt length: {len(prompt)} chars")
    logger.info(
        f"Calling {ai_provider.upper()} CLI for failure group ({len(failures)} tests with same error)"
    )
    logger.info(f"Calling AI CLI with {_format_timeout_log(ai_cli_timeout)}")
    result, parsed = await _call_ai_and_record(
        prompt,
        job_id=job_id,
        call_type="primary",
        cwd=repo_path,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
    )

    if parsed is None:
        parsed = AnalysisDetail(details=result.text)

    return parsed, error_signature


async def analyze_failure_group(
    failures: list[TestFailure],
    console_context: str,
    repo_path: Path | None,
    ai_provider: str = "",
    ai_model: str = "",
    ai_cli_timeout: int | None = None,
    custom_prompt: str = "",
    artifacts_context: str = "",
    server_url: str = "",
    job_id: str = "",
    peer_ai_configs: list | None = None,
    peer_analysis_max_rounds: int = 3,
    group_label: str = "",
    additional_repos: dict[str, Path] | None = None,
    max_concurrent_ai_calls: int = 3,
) -> list[FailureAnalysis]:
    """Analyze a group of failures with the same error signature.

    Uses a single AI call for the group (or multi-AI peer consensus
    when peers are configured), then applies the analysis to all
    failures in the group.

    Args:
        failures: List of test failures with the same error signature.
        console_context: Relevant console lines for context.
        repo_path: Path to cloned test repo (optional).
        ai_provider: AI provider to use.
        ai_model: AI model to use.
        ai_cli_timeout: Timeout in minutes (overrides AI_CLI_TIMEOUT env var).
        custom_prompt: Additional instructions from request payload (raw_prompt).
        artifacts_context: Jenkins artifacts context for AI analysis (optional).
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.
        group_label: Human-readable label identifying which failure group is
            being analyzed (e.g. ``"2/3"``). Forwarded to peer analysis for
            progress phase disambiguation.
        additional_repos: Extra cloned repositories for AI context.
        max_concurrent_ai_calls: Maximum concurrent AI CLI processes for
            peer analysis parallelism (default: 3).

    Returns:
        List of FailureAnalysis objects, one per failure in the group.
    """
    logger.debug(
        f"analyze_failure_group called with server_url='{server_url}', job_id='{job_id}'"
    )

    if peer_ai_configs:
        from jenkins_job_insight.models import AiConfigEntry
        from jenkins_job_insight.peer_analysis import analyze_failure_group_with_peers

        configs = [
            AiConfigEntry(**c) if isinstance(c, dict) else c for c in peer_ai_configs
        ]
        return await analyze_failure_group_with_peers(
            failures=failures,
            console_context=console_context,
            repo_path=repo_path,
            main_ai_provider=ai_provider,
            main_ai_model=ai_model,
            peer_ai_configs=configs,
            max_rounds=peer_analysis_max_rounds,
            ai_cli_timeout=ai_cli_timeout,
            custom_prompt=custom_prompt,
            artifacts_context=artifacts_context,
            server_url=server_url,
            job_id=job_id,
            group_label=group_label,
            additional_repos=additional_repos,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
        )

    parsed, error_signature = await _run_single_ai_analysis(
        failures=failures,
        console_context=console_context,
        repo_path=repo_path,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        custom_prompt=custom_prompt,
        artifacts_context=artifacts_context,
        server_url=server_url,
        job_id=job_id,
        additional_repos=additional_repos,
    )

    # Apply the same analysis to all failures in the group.
    # All failures share the same signature (that's how they were grouped),
    # so reuse the already-computed value instead of calling get_failure_signature() again.
    return [
        FailureAnalysis(
            test_name=f.test_name,
            error=f.error_message,
            analysis=parsed,
            error_signature=error_signature,
        )
        for f in failures
    ]


async def analyze_child_job(
    job_name: str,
    build_number: int,
    jenkins_client: JenkinsClient,
    jenkins_base_url: str,
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
) -> ChildJobAnalysis:
    """Analyze a single child job, recursively analyzing its failed children.

    Each child job gets its own Claude CLI call to manage context size.

    Args:
        job_name: Name of the Jenkins job to analyze.
        build_number: Build number to analyze.
        jenkins_client: Jenkins API client.
        jenkins_base_url: Base URL of Jenkins server.
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
    # Construct Jenkins URL for this child job
    job_path = "/job/".join(job_name.split("/"))
    jenkins_url = f"{jenkins_base_url.rstrip('/')}/job/{job_path}/{build_number}/"

    if depth >= max_depth:
        return ChildJobAnalysis(
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            note="Max depth reached - analysis stopped to prevent infinite recursion",
        )

    # Get build info for this child
    try:
        build_info = await asyncio.to_thread(
            jenkins_client.get_build_info_safe, job_name, build_number
        )
    except Exception as e:
        return ChildJobAnalysis(
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            note=f"Failed to get build info: {e}",
        )

    # Fetch console output first (needed for fallback child detection and analysis)
    try:
        console_output = await asyncio.to_thread(
            jenkins_client.get_build_console, job_name, build_number
        )
    except Exception as e:
        return ChildJobAnalysis(
            job_name=job_name,
            build_number=build_number,
            jenkins_url=jenkins_url,
            note=f"Failed to get console output: {e}",
        )

    # Check if this child also has failed children
    # Try to extract from build_info first
    failed_children = extract_failed_child_jobs(build_info)

    # Fallback to console parsing if none found from build_info
    if not failed_children:
        failed_children = extract_failed_child_jobs_from_console(console_output)

    if failed_children:
        # Recursively analyze failed children IN PARALLEL with bounded concurrency
        child_tasks: list[Coroutine[Any, Any, Any]] = [
            analyze_child_job(
                child_name,
                child_num,
                jenkins_client,
                jenkins_base_url,
                depth + 1,
                max_depth,
                repo_path,
                ai_provider,
                ai_model,
                ai_cli_timeout,
                custom_prompt,
                artifacts_context="",
                server_url=server_url,
                job_id=job_id,
                peer_ai_configs=peer_ai_configs,
                peer_analysis_max_rounds=peer_analysis_max_rounds,
                additional_repos=additional_repos,
                max_concurrent_ai_calls=max_concurrent_ai_calls,
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

    # Try to get structured test report first (much cleaner than parsing console output)
    test_report = await asyncio.to_thread(
        jenkins_client.get_test_report, job_name, build_number
    )

    test_failures = (
        extract_failures_from_test_report(test_report) if test_report else []
    )

    # Extract relevant console lines for context
    console_context = extract_relevant_console_lines(console_output)

    # If we have test failures, group by signature and analyze unique groups
    if test_failures:
        # Group failures by signature to avoid analyzing identical errors multiple times
        failure_groups: dict[str, list[TestFailure]] = defaultdict(list)
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
                    artifacts_context=artifacts_context,
                    server_url=server_url,
                    job_id=job_id,
                    peer_ai_configs=peer_ai_configs,
                    peer_analysis_max_rounds=peer_analysis_max_rounds,
                    group_label=f"{job_name}:{group_idx}/{total_groups}"
                    if total_groups > 1
                    else "",
                    additional_repos=additional_repos,
                    max_concurrent_ai_calls=max_concurrent_ai_calls,
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
        _build_prompt_sections(
            custom_prompt,
            artifacts_context,
            repo_path,
            server_url,
            job_id,
            additional_repos=additional_repos,
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
{_JSON_RESPONSE_SCHEMA}
"""
    logger.debug(f"AI prompt length: {len(prompt)} chars")
    logger.info(f"Calling AI CLI with {_format_timeout_log(ai_cli_timeout)}")
    result, parsed_analysis = await _call_ai_and_record(
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
    # Get Jenkins data
    jenkins_client = JenkinsClient(
        url=settings.jenkins_url,
        username=settings.jenkins_user,
        password=settings.jenkins_password,
        ssl_verify=settings.jenkins_ssl_verify,
        timeout=settings.jenkins_timeout,
    )

    # Construct full Jenkins URL for the result
    # Handle folder-style job names by replacing '/' with '/job/'
    job_path = "/job/".join(job_name.split("/"))
    jenkins_build_url = (
        f"{settings.jenkins_url.rstrip('/')}/job/{job_path}/{build_number}/"
    )

    # First get build_info (quick call) to check if build passed
    build_info: dict = {}
    try:
        build_info = await asyncio.to_thread(
            jenkins_client.get_build_info_safe, job_name, build_number
        )
    except Exception as e:
        handle_jenkins_exception(e, job_name, build_number)

    # Check if build passed - return early if yes (unless force is set)
    build_result = build_info.get("result")
    force = getattr(request, "force", False) or settings.force_analysis
    if build_result == "SUCCESS" and not force:
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
    if build_result == "SUCCESS" and force:
        logger.info(
            f"Build {job_name} #{build_number} passed but force=True, continuing analysis"
        )

    # Download build artifacts for context
    artifacts_context = ""
    extract_path: Path | None = None
    try:
        if settings.get_job_artifacts:
            artifacts = build_info.get("artifacts", [])
            build_url = build_info.get("url", "").rstrip("/")
            if artifacts and build_url:
                try:
                    artifacts_context, extract_path = await asyncio.to_thread(
                        process_build_artifacts,
                        jenkins_client.session,
                        build_url,
                        artifacts,
                        settings.jenkins_artifacts_max_size_mb,
                    )
                except Exception as exc:
                    logger.warning(f"Failed to process artifacts: {exc}")

        # Only fetch console output if build failed
        console_output: str = ""
        try:
            console_output = await asyncio.to_thread(
                jenkins_client.get_build_console, job_name, build_number
            )
        except Exception as e:
            handle_jenkins_exception(e, job_name, build_number)

        # Check for failed child jobs in pipeline
        # Try to extract from build_info first
        failed_child_jobs = extract_failed_child_jobs(build_info)

        # Fallback to console parsing if none found from build_info
        if not failed_child_jobs:
            failed_child_jobs = extract_failed_child_jobs_from_console(console_output)

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
                        f"Cloning test repository: {_redact_url(clean_tests_url)}"
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
                    repo_context = f"\nTest repository cloned from: {clean_tests_url} (at {repo_name}/)"
                except Exception as e:  # noqa: BLE001 — non-fatal tests repo clone failure
                    logger.warning(
                        "Failed to clone repository (%s)",
                        type(e).__name__,
                    )
                    repo_context = "\nFailed to clone repository (details redacted)"

            custom_prompt = (request.raw_prompt or "").strip()

            # Make artifacts accessible in the AI working directory
            if extract_path:
                artifacts_link = repo_path / "build-artifacts"
                try:
                    artifacts_link.symlink_to(extract_path)
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
                await _safe_update_progress(progress_job_id, "analyzing_child_jobs")
                child_tasks: list[Coroutine[Any, Any, Any]] = [
                    analyze_child_job(
                        job_name=child_name,
                        build_number=child_num,
                        jenkins_client=jenkins_client,
                        jenkins_base_url=settings.jenkins_url,
                        depth=0,
                        max_depth=3,
                        repo_path=repo_path,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        ai_cli_timeout=settings.ai_cli_timeout,
                        custom_prompt=custom_prompt,
                        artifacts_context="",
                        server_url=server_url,
                        job_id=job_id,
                        peer_ai_configs=peer_ai_configs,
                        peer_analysis_max_rounds=peer_analysis_max_rounds,
                        additional_repos=cloned_repos or None,
                        max_concurrent_ai_calls=settings.max_concurrent_ai_calls,
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

            # Try to get structured test report first (much cleaner than parsing console output)
            test_report = await asyncio.to_thread(
                jenkins_client.get_test_report, job_name, build_number
            )

            test_failures = (
                extract_failures_from_test_report(test_report) if test_report else []
            )
            logger.info(f"Found {len(test_failures)} test failures to analyze")

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

            # Extract relevant console lines for context
            console_context = extract_relevant_console_lines(console_output)

            # Analyze main job test failures, grouping by signature to deduplicate
            unique_errors = 0
            if test_failures:
                await _safe_update_progress(progress_job_id, "analyzing_failures")
                # Group failures by signature to avoid analyzing identical errors multiple times
                failure_groups: dict[str, list[TestFailure]] = defaultdict(list)
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
                ) = _build_prompt_sections(
                    custom_prompt,
                    artifacts_context,
                    repo_path,
                    server_url,
                    job_id,
                    additional_repos=cloned_repos or None,
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
{_JSON_RESPONSE_SCHEMA}
"""
                logger.debug(f"AI prompt length: {len(prompt)} chars")
                logger.info(
                    f"Calling AI CLI with {_format_timeout_log(settings.ai_cli_timeout)}"
                )
                result, parsed_console = await _call_ai_and_record(
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
        if extract_path:
            cleanup_extract_dir(extract_path)
