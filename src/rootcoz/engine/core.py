"""Core analysis engine — CI-agnostic failure analysis logic.

This module contains the CI-agnostic functions for analyzing test failures,
including AI CLI orchestration, prompt building, JSON response parsing,
and failure grouping/deduplication. It is independent of any specific CI
system.
"""

import asyncio
import hashlib
import importlib
import json
import os
import re
from pathlib import Path

from ai_cli_runner import AIResult, call_ai_cli
from simple_logger.logger import get_logger

from rootcoz.config import Settings, parse_additional_repos
from rootcoz.logging_context import get_log_file
from rootcoz.models import (
    AdditionalRepo,
    AiConfigEntry,
    AnalysisDetail,
    BaseAnalysisRequest,
    CodeFix,
    FailedTest,
    FailureAnalysis,
    ProductBugReport,
)
from rootcoz.repository import RepositoryManager
from rootcoz.storage import update_progress_phase
from rootcoz.token_tracking import record_ai_usage

_log_file = get_log_file()

logger = get_logger(
    name=__name__,
    level=os.environ.get("LOG_LEVEL", "INFO"),
    filename=_log_file,
)


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
        except Exception as e:  # non-fatal additional repo clone failure
            logger.warning(
                "Failed to clone additional repo '%s' (%s)",
                ar.name,
                type(e).__name__,
            )

    await asyncio.gather(*[_clone_into_subdir(ar) for ar in additional_repos_list])

    return cloned, repo_path


async def safe_update_progress(job_id: str | None, phase: str) -> None:
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


# Path to FAILURE_HISTORY_ANALYSIS.md — the AI reads it at runtime instead of injecting content into the prompt
QUERY_MD_PATH = (
    Path(__file__).parent.parent / "ai-prompts" / "FAILURE_HISTORY_ANALYSIS.md"
)

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
CONSOLE_ERROR_PATTERN = re.compile(
    r"\b(?:errors?|fail(?:ed|ures?)?|tracebacks?|warn(?:ings?)?|critical|fatal|assert(?:ion)?(?:error)?s?)\b"
    r"|(?:^|[\s\[])[A-Za-z_][\w.]*?(?:error|exception)(?=[:\s\]]|$)",
    re.IGNORECASE,
)


async def call_ai_and_record(
    prompt: str,
    *,
    job_id: str,
    call_type: str,
    cwd: Path | None = None,
    ai_provider: str = "",
    ai_model: str = "",
    ai_cli_timeout: int | None = None,
    cli_flags: list[str] | None = None,
    session_id: str | None = None,
    output_format: str | None = "json",
) -> tuple[AIResult, AnalysisDetail | None]:
    """Call AI CLI with retry, record token usage, and parse the response.

    Returns ``(result, parsed_analysis)``.  ``parsed_analysis`` is ``None``
    when the AI call failed (``result.success is False``).
    """
    result = await call_ai_cli_with_retry(
        prompt,
        cwd=cwd,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        cli_flags=cli_flags,
        session_id=session_id,
        output_format=output_format,
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
        parsed = parse_json_response(result.text)

    return result, parsed


async def call_ai_cli_with_retry(
    prompt: str,
    *,
    cwd: Path | None = None,
    ai_provider: str = "",
    ai_model: str = "",
    ai_cli_timeout: int | None = None,
    cli_flags: list[str] | None = None,
    max_retries: int = 3,
    session_id: str | None = None,
    output_format: str | None = "json",
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
        session_id: Optional session ID to resume a prior conversation.

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
            output_format=output_format,
            session_id=session_id,
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


JSON_RESPONSE_SCHEMA = (
    "CRITICAL: Your response must be ONLY a valid JSON object. No text before or after. No markdown code blocks. No"
    " explanation.\n"
    "\n"
    "If CODE ISSUE:\n"
    "{\n"
    '  "classification": "CODE ISSUE",\n'
    '  "affected_tests": ["test_name_1", "test_name_2"],\n'
    '  "details": "Your detailed analysis of what caused this failure. Use paragraph breaks (double newlines) to'
    " separate sections: root cause identification, evidence from logs/code, and impact assessment. Do NOT write one"
    ' continuous paragraph.",\n'
    '  "artifacts_evidence": "VERBATIM lines from files under build-artifacts/ that support your analysis. Format'
    " each line as [file-path]: content. Example: [build-artifacts/logs/app.log]: 2026-03-16 INFO Service started"
    " successfully. Include evidence showing the product is healthy or that the test code caused the failure. Separate"
    ' distinct artifact entries with paragraph breaks (double newlines).",\n'
    '  "code_fix": {\n'
    '    "file": "exact/file/path.py",\n'
    '    "line": "line number",\n'
    '    "change": "specific code change that fixes all affected tests",\n'
    '    "original_code": "optional complete current contents of exact/file/path.py for diff/editor display (raw'
    ' code string, NO markdown formatting)",\n'
    '    "suggested_code": "complete replacement contents of exact/file/path.py after applying the fix (raw code'
    ' string, NO markdown formatting)",\n'
    '    "tests_repo_search_keywords": ["specific error symptom", "component + behavior", "error type"]\n'
    "  }\n"
    "}\n"
    "\n"
    "tests_repo_search_keywords rules:\n"
    "- Generate 3-5 SHORT specific keywords for finding matching issues in the tests repository\n"
    "- Focus on the specific error symptom and broken behavior from the test code perspective\n"
    '- Combine component name with the specific failure (e.g. "fixture setup timeout", "API mock validation'
    ' error")\n'
    '- AVOID generic/broad terms alone like "timeout", "failure", "error"\n'
    "- Each keyword should be specific enough to narrow GitHub issue search results to relevant issues\n"
    '- Think: "what would someone title a GitHub issue for this exact test code problem?"\n'
    "\n"
    "If PRODUCT BUG:\n"
    "{\n"
    '  "classification": "PRODUCT BUG",\n'
    '  "affected_tests": ["test_name_1", "test_name_2"],\n'
    '  "details": "Your detailed analysis of what caused this failure. Use paragraph breaks (double newlines) to'
    " separate sections: root cause identification, evidence from logs/code, and impact assessment. Do NOT write one"
    ' continuous paragraph.",\n'
    '  "artifacts_evidence": "VERBATIM lines from files under build-artifacts/ that prove the product defect.'
    " Format each line as [file-path]: content. Example: [build-artifacts/logs/error.log]: 2026-03-16 ERROR"
    " NullPointerException in AuthService. Include the specific log lines showing the product failure. Separate"
    ' distinct artifact entries with paragraph breaks (double newlines).",\n'
    '  "product_bug_report": {\n'
    '    "title": "concise bug title",\n'
    '    "severity": "critical/high/medium/low",\n'
    '    "component": "affected component",\n'
    '    "description": "what product behavior is broken. Use paragraph breaks between sections.",\n'
    '    "evidence": "relevant log snippets",\n'
    '    "jira_search_keywords": ["specific error symptom", "component + behavior", "error type"]\n'
    "  }\n"
    "}\n"
    "\n"
    "jira_search_keywords rules:\n"
    "- Generate 3-5 SHORT specific keywords for finding matching bugs in Jira\n"
    "- Focus on the specific error symptom and broken behavior, NOT test infrastructure\n"
    '- Combine component name with the specific failure (e.g. "VM start failure migration", "API timeout'
    ' authentication")\n'
    '- AVOID generic/broad terms alone like "timeout", "failure", "error"\n'
    "- Each keyword should be specific enough to narrow Jira search results to relevant bugs\n"
    '- Think: "what would someone title a Jira bug for this exact issue?"\n'
    "\n"
    "If INFRASTRUCTURE:\n"
    "{\n"
    '  "classification": "INFRASTRUCTURE",\n'
    '  "affected_tests": ["test_name_1", "test_name_2"],\n'
    '  "details": "Your detailed analysis of the infrastructure/environment issue. Use paragraph breaks (double'
    " newlines) to separate sections: root cause identification, evidence from logs, and impact assessment. Do NOT"
    ' write one continuous paragraph.",\n'
    '  "artifacts_evidence": "VERBATIM lines from files under build-artifacts/ that prove the infrastructure'
    " failure. Format each line as [file-path]: content. Example: [build-artifacts/logs/cluster.log]: 2026-03-16 ERROR"
    " Node not ready. Include the specific log lines showing the infrastructure problem. Separate distinct artifact"
    ' entries with paragraph breaks (double newlines)."\n'
    "}"
)


def format_timeout_log(timeout_value: int | None) -> str:
    """Format AI CLI timeout for log messages."""
    if timeout_value is not None:
        return f"timeout={timeout_value} minutes ({timeout_value * 60}s)"
    return "timeout=default"


def build_artifacts_section(artifacts_context: str) -> str:
    """Build the artifact context prompt section."""
    if not artifacts_context:
        return ""
    return (
        "\n\n=== BUILD ARTIFACTS ===\n"
        "The following is a PREVIEW of the build-artifacts/ directory "
        "structure and file listing. File contents are not inlined here; "
        "open the files under build-artifacts/ in your working directory "
        "to inspect them.\n\n"
        f"{artifacts_context}\n\n"
        "IMPORTANT INSTRUCTIONS FOR ARTIFACT ANALYSIS:\n"
        "1. READ the actual files under build-artifacts/ — the listing "
        "above is incomplete and does not include file contents\n"
        "2. Look for error messages, stack traces, service logs, and "
        "status information\n"
        "3. In your artifacts_evidence field, include VERBATIM lines "
        "with the file path, e.g.: [build-artifacts/logs/app.log]: "
        "actual error line here\n"
        "4. Do NOT classify based solely on the test error message — "
        "check the artifact logs for the real root cause"
    )


def get_failure_signature(failure: FailedTest) -> str:
    """Create a signature for grouping identical failures.

    Uses the full error message and stack trace to identify failures that
    are essentially the same issue.

    Args:
        failure: The test failure to create a signature for.

    Returns:
        SHA-256 hash string representing the failure signature.
    """
    # Use error message and full stack trace for deduplication.
    signature_text = f"{failure.error_message}|{failure.stack_trace}"
    return hashlib.sha256(signature_text.encode()).hexdigest()


def parse_json_response(raw_text: str) -> AnalysisDetail:
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
    # TODO: This only handles JSON objects; top-level JSON arrays are not
    # supported.  Current callers always expect an object schema so this is
    # fine, but revisit if array responses become possible.
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
    return recover_from_details(fallback)


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
    # TODO: This regex is fragile for values containing escaped quotes or
    # nested brackets.  It works for the current use case (3-5 short
    # keywords) but should be replaced with a proper JSON parser if the
    # expected payloads grow more complex.
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


def recover_from_details(result: AnalysisDetail) -> AnalysisDetail:
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
    if not details:
        return result

    # Try markdown classification recovery (e.g. "**Classification: INFRASTRUCTURE**")
    if '"classification"' not in details:
        md_match = re.search(r"\*\*Classification:\s*([A-Z][A-Z _]+?)\*\*", details)
        if not md_match:
            return result
        # Found markdown classification — extract what we can
        classification = md_match.group(1).strip()
        logger.warning(
            "Recovered classification '%s' from markdown-formatted AI response",
            classification,
        )
        # Strip the markdown classification header from details
        clean_details = re.sub(
            r"\*\*Classification:\s*[A-Z][A-Z _]+?\*\*\s*", "", details
        ).strip()
        return AnalysisDetail(
            classification=classification,
            affected_tests=result.affected_tests,
            details=clean_details or details,
            artifacts_evidence=result.artifacts_evidence,
            code_fix=result.code_fix,
            product_bug_report=result.product_bug_report,
        )

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
        console_output: Full CI console output.

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
        if CONSOLE_ERROR_PATTERN.search(line):
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

    # Fallback: nothing matched the error pattern; return full console output
    # so downstream consumers (AI prompt, etc.) can decide their own limits.
    return console_output


def derive_error_details(error_details: str, stack_trace: str) -> str:
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


def build_prompt_sections(
    custom_prompt: str,
    artifacts_context: str,
    repo_path: Path | None,
    server_url: str,
    job_id: str,
    *,
    additional_repos: dict[str, Path] | None = None,
    auth_header: str = "",
) -> tuple[str, str, str, str]:
    """Build common prompt sections used across all analysis flows.

    Returns:
        Tuple of (custom_prompt_section, artifacts_section, resources_section, query_section)
    """
    custom_prompt_section = (
        f"\n\nADDITIONAL INSTRUCTIONS:\n{custom_prompt}\n" if custom_prompt else ""
    )

    artifacts_section = build_artifacts_section(artifacts_context)
    history_enabled = bool(server_url and job_id and QUERY_MD_PATH.exists())
    resources_section = build_resources_section(
        repo_path, additional_repos=additional_repos, history_enabled=history_enabled
    )

    if not QUERY_MD_PATH.exists():
        logger.warning(
            f"History analysis prompt file not found at {QUERY_MD_PATH}; "
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

        auth_instruction = ""
        if auth_header:
            auth_instruction = (
                "\nFor ALL curl commands, include this authentication"
                f' header: -H "Authorization: {auth_header}"'
            )

        query_section = f"""

MANDATORY: Before analyzing any failure, you MUST read and follow the instructions in {QUERY_MD_PATH}.
When executing curl commands from that file, use server_url={server_url} and job_id={job_id}.{auth_instruction}
These instructions are NOT optional. You MUST complete ALL steps for EVERY test.
{repo_history_prompt}
"""

    return custom_prompt_section, artifacts_section, resources_section, query_section


def build_resources_section(
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
                    f"- Project-specific history analysis instructions"
                    f" at {repo_history_prompt} — read and follow"
                    f" alongside the main history analysis instructions"
                )

    if resources:
        return "\n\nAVAILABLE RESOURCES:\n" + "\n".join(resources) + "\n"

    return ""


async def run_single_ai_analysis(
    *,
    failures: list[FailedTest],
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
    auth_header: str = "",
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
        artifacts_context: Build artifacts context.
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.

    Returns:
        Tuple of (parsed AnalysisDetail, error_signature string).
    """
    representative = failures[0]
    error_signature = get_failure_signature(representative)
    test_names = [f.test_name for f in failures]

    custom_prompt_section, artifacts_section, resources_section, query_section = (
        build_prompt_sections(
            custom_prompt,
            artifacts_context,
            repo_path,
            server_url,
            job_id,
            additional_repos=additional_repos,
            auth_header=auth_header,
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
Analyze this test failure from a CI job.

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
{JSON_RESPONSE_SCHEMA}
"""

    if artifacts_context:
        logger.info(
            f"Prompt includes build artifacts context ({len(artifacts_context)} chars)"
        )

    logger.debug(f"AI prompt length: {len(prompt)} chars")
    logger.info(
        f"Calling {ai_provider.upper()} CLI for failure group ({len(failures)} tests with same error)"
    )
    logger.info(f"Calling AI CLI with {format_timeout_log(ai_cli_timeout)}")
    result, parsed = await call_ai_and_record(
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
    failures: list[FailedTest],
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
    auth_header: str = "",
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
        artifacts_context: Build artifacts context for AI analysis (optional).
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
        _peer_mod = importlib.import_module("rootcoz.peer_analysis")
        configs = [
            AiConfigEntry(**c) if isinstance(c, dict) else c for c in peer_ai_configs
        ]
        return await _peer_mod.analyze_failure_group_with_peers(
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
            auth_header=auth_header,
        )

    parsed, error_signature = await run_single_ai_analysis(
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
        auth_header=auth_header,
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
