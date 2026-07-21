"""Core analysis engine — CI-agnostic failure analysis logic.

This module contains the CI-agnostic functions for analyzing test failures,
including AI orchestration, prompt building, JSON response parsing,
and failure grouping/deduplication. It is independent of any specific CI
system.
"""

import asyncio
import hashlib
import importlib
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from simple_logger.logger import get_logger

from rootcoz.ai_client import (
    AIResult,
    ANALYSIS_BUILTIN_TOOLS,
    RESOURCE_REPO_BROWSE_HINT,
    call_ai_once,
)
from rootcoz.config import Settings, parse_additional_repos
from rootcoz.engine.chat import build_analysis_history_tools
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


# Module-level callback for SSE notifications.
# main.py registers this at startup to avoid circular imports.
_on_progress_updated: Callable[[str], None] | None = None


def set_progress_callback(callback: Callable[[str], None]) -> None:
    """Register a callback invoked after each progress update.

    Args:
        callback: Function that takes a job_id and notifies SSE listeners.
    """
    global _on_progress_updated
    _on_progress_updated = callback


async def safe_update_progress(job_id: str | None, phase: str) -> None:
    """Best-effort progress update; failures are swallowed and logged."""
    if not job_id:
        return
    try:
        await update_progress_phase(job_id, phase)
        if _on_progress_updated:
            _on_progress_updated(job_id)
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

ROOTCOZ_PROMPT_FILENAME = "ROOTCOZ_PROMPT.md"
ROOTCOZ_HISTORY_PROMPT_FILENAME = "ROOTCOZ_HISTORY_PROMPT.md"
# On-disk VCS metadata directory name — used only for existence checks; never
# advertised as a shell/git capability in prompts (see RESOURCE_REPO_BROWSE_HINT).
_VCS_METADATA_DIR = ".git"


def _rootcoz_prompt_fingerprint(path: Path) -> str:
    """Open a ``.rootcoz`` prompt file and return a content fingerprint.

    Uses ``Path.read_bytes()`` so the file is actually loaded for verification,
    then advertises only sha256/size — never the body — so the AI must use the
    ``read`` tool (AGENTS.md file-based data policy / issue #74).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    digest = hashlib.sha256(raw).hexdigest()
    return f", sha256={digest}, bytes={len(raw)}"


ROOTCOZ_ISSUE_PROMPT_FILENAME = "ROOTCOZ_ISSUE_PROMPT.md"

# Subdirectories under .rootcoz/ that are copied to workspace .pi/
_ROOTCOZ_PI_SUBDIRS = ("agents", "skills", "extensions")


_SAFE_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _extract_agent_name(agent_file: Path) -> str | None:
    """Extract the ``name`` field from an agent .md file's YAML frontmatter.

    Returns the name string, or ``None`` if the frontmatter is missing
    or does not contain a ``name`` field.
    """
    try:
        text = agent_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    for line in text[3:end].splitlines():
        line = line.strip()
        if line.lower().startswith("name:"):
            name = line.split(":", 1)[1].strip()
            # Constrain to safe characters to prevent prompt injection
            if _SAFE_AGENT_NAME_RE.match(name):
                return name
            return None
    return None


def discover_project_agent_names(
    additional_repos: dict[str, Path] | None,
) -> list[str]:
    """Return unique project agent names from ``.rootcoz/agents/*.md``.

    Prefers frontmatter ``name:``; falls back to the filename stem when it
    matches the same safe-name regex (otherwise the file is skipped).
    """
    if not additional_repos:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for path in additional_repos.values():
        agents_dir = path / ".rootcoz" / "agents"
        if not agents_dir.is_dir():
            continue
        for agent_file in sorted(agents_dir.glob("*.md")):
            extracted = _extract_agent_name(agent_file)
            if extracted:
                name = extracted
            elif _SAFE_AGENT_NAME_RE.match(agent_file.stem):
                name = agent_file.stem
            else:
                logger.warning(
                    "Skipping agent file with unsafe stem %r: %s",
                    agent_file.stem,
                    agent_file,
                )
                continue
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names


def build_agent_gate_section(agent_names: list[str]) -> str:
    """Build a top-of-prompt hard gate so models call project agents first.

    Cursor (CLI/ACPX) often skips buried agent instructions when the prompt
    leads with history tools + JSON-only rules. Putting this gate first forces
    ``subagent`` before analysis JSON.
    """
    if not agent_names:
        return ""
    names_str = ", ".join(agent_names)
    return f"""
STEP 0 — PROJECT AGENTS (HARD GATE — DO THIS BEFORE ANY OTHER WORK):
Before history checks, console reads, or producing JSON, you MUST call the subagent tool for EACH of these agents: {names_str}
For every call use agentScope="both" and confirmProjectAgents=false.
Read each agent's file / project ROOTCOZ_PROMPT.md for the task text when present.
Do NOT produce your final JSON until every listed agent has returned.
Include each agent's FULL response verbatim at the end of the details field.
You do not have bash — use history HTTP tools for history steps and still satisfy this gate.
"""


def discover_rootcoz_prompt_paths(
    additional_repos: dict[str, Path] | None,
    *,
    history_enabled: bool = False,
) -> list[Path]:
    """Return existing ``.rootcoz`` prompt files under cloned repos."""
    if not additional_repos:
        return []
    found: list[Path] = []
    for path in additional_repos.values():
        prompt = path / ".rootcoz" / ROOTCOZ_PROMPT_FILENAME
        if prompt.is_file():
            found.append(prompt)
        if history_enabled:
            hist = path / ".rootcoz" / ROOTCOZ_HISTORY_PROMPT_FILENAME
            if hist.is_file():
                found.append(hist)
    return found


def build_rootcoz_prompt_gate_section(prompt_paths: list[Path]) -> str:
    """Hard gate: require ``read`` of project ``.rootcoz`` prompts before analysis.

    Opens each file for a sha256/bytes fingerprint (proves load) but does not
    embed bodies — AGENTS.md file-based data policy / issue #74.
    """
    if not prompt_paths:
        return ""
    lines: list[str] = []
    for path in prompt_paths:
        fp = _rootcoz_prompt_fingerprint(path)
        lines.append(f"- {path}{fp}")
    listed = "\n".join(lines)
    return f"""
STEP 0b — PROJECT .rootcoz PROMPTS (HARD GATE — READ BEFORE ANALYSIS):
These project instruction files were opened on disk for verification (fingerprints below).
You MUST open EACH with the read tool and follow them before analyzing.
Bodies are intentionally not embedded in this prompt (AGENTS.md / issue #74).
{listed}
Do not skip these reads.
"""


def _ignore_symlinks(directory: str, contents: list[str]) -> list[str]:
    """Ignore symlinks during copytree to prevent escape attacks."""
    return [c for c in contents if (Path(directory) / c).is_symlink()]


def copy_rootcoz_pi_resources(cloned_repos: dict[str, Path], workspace: Path) -> None:
    """Copy .rootcoz/{agents,skills,extensions}/ from cloned repos to workspace .pi/.

    Scans each cloned repo for a ``.rootcoz/`` directory and copies
    ``agents/``, ``skills/``, and ``extensions/`` subdirectories into
    ``<workspace>/.pi/`` so that pi's ``DefaultResourceLoader`` discovers
    project-provided agents, skills, and extensions.

    Symlinks are skipped to prevent symlink escape attacks from
    untrusted repositories.  Failures are logged and swallowed so a
    bad ``.rootcoz/`` tree never crashes the analysis.

    Args:
        cloned_repos: Mapping of repo name to cloned path.
        workspace: Root workspace directory.
    """
    for repo_name, repo_path in cloned_repos.items():
        rootcoz_dir = repo_path / ".rootcoz"
        if not rootcoz_dir.is_dir():
            continue
        pi_dir = workspace / ".pi"
        for subdir in _ROOTCOZ_PI_SUBDIRS:
            src = rootcoz_dir / subdir
            if not src.is_dir():
                continue
            dest = pi_dir / subdir
            try:

                def _copy_with_overwrite_warning(
                    src_path: str,
                    dst_path: str,
                    *,
                    follow_symlinks: bool = True,
                ) -> None:
                    """copy2 wrapper that warns when overwriting existing files."""
                    if os.path.exists(dst_path):
                        logger.warning(
                            ".rootcoz/%s/%s from '%s' overwrites existing file",
                            subdir,
                            os.path.relpath(dst_path, str(dest)),
                            repo_name,
                        )
                    shutil.copy2(src_path, dst_path, follow_symlinks=follow_symlinks)

                shutil.copytree(
                    src,
                    dest,
                    ignore=_ignore_symlinks,
                    copy_function=_copy_with_overwrite_warning,
                    dirs_exist_ok=True,
                )
                logger.info(
                    "Copied .rootcoz/%s/ from '%s' to workspace .pi/%s/",
                    subdir,
                    repo_name,
                    subdir,
                )
            except OSError:
                logger.warning(
                    "Failed to copy .rootcoz/%s/ from '%s'; continuing",
                    subdir,
                    repo_name,
                    exc_info=True,
                )


# Pattern for error detection in console output (word boundaries, case-insensitive)
CONSOLE_ERROR_PATTERN = re.compile(
    r"\b(?:errors?|fail(?:ed|ures?)?|tracebacks?|warn(?:ings?)?|critical|fatal|assert(?:ion)?(?:error)?s?)\b"
    r"|(?:^|[\s\[])[A-Za-z_][\w.]*?(?:error|exception)(?=[:\s\]]|$)",
    re.IGNORECASE,
)


# Shared artifacts_evidence format for JSON schema (text logs + images via read).
_ARTIFACTS_EVIDENCE_FORMAT = (
    "Format each entry as [file-path]: content. "
    "For text files, use VERBATIM lines "
    "(e.g. [build-artifacts/logs/app.log]: 2026-03-16 ERROR NullPointerException). "
    "For images (png/jpg/gif/webp/bmp), use the read tool and describe what you see "
    "(e.g. [build-artifacts/screenshots/failure.png]: UI shows spinner stuck on Contents). "
    "Separate distinct artifact entries with paragraph breaks (double newlines)."
)

JSON_RESPONSE_SCHEMA = (
    "CRITICAL: Your FINAL response must be ONLY a valid JSON object. No text before or after. No markdown code"
    " blocks. No explanation.\n"
    "Tool calls (read, ls, find, grep, subagent) are required BEFORE that final JSON whenever AVAILABLE RESOURCES"
    " or STEP 0 instruct you to use them. The JSON-only rule applies to the final answer, not to intermediate tool"
    " use.\n"
    "\n"
    "TWO-AXIS CLASSIFICATION SYSTEM:\n"
    "Every failure must be classified along TWO independent axes:\n"
    "\n"
    'Axis 1 — "classification" (Root Cause — what is broken):\n'
    '  - "CODE ISSUE" — test code is wrong\n'
    '  - "PRODUCT BUG" — product under test has a defect\n'
    '  - "INFRASTRUCTURE" — environment/cluster/resource problem\n'
    "\n"
    'Axis 2 — "pattern" (how the failure manifests — set to "NEW" for initial analysis;\n'
    "  history analysis may refine it later):\n"
    '  - "NEW" — first occurrence\n'
    '  - "REGRESSION" — was passing, recently started failing\n'
    '  - "FLAKY" — sometimes passes, sometimes fails\n'
    '  - "INTERMITTENT" — fails under specific conditions\n'
    '  - "KNOWN_BUG" — matches a known reported bug\n'
    '  - "PERSISTENT" — consistently failing across many runs\n'
    "\n"
    "If CODE ISSUE:\n"
    "{\n"
    '  "classification": "CODE ISSUE",\n'
    '  "pattern": "NEW",\n'
    '  "affected_tests": ["test_name_1", "test_name_2"],\n'
    '  "details": "Your detailed analysis of what caused this failure. Use paragraph breaks (double newlines) to'
    " separate sections: root cause identification, evidence from logs/code, and impact assessment. Do NOT write one"
    ' continuous paragraph.",\n'
    '  "artifacts_evidence": "Evidence from build-artifacts/ that supports your analysis (text and/or images). '
    "Include evidence showing the product is healthy or that the test code caused the failure. "
    f'{_ARTIFACTS_EVIDENCE_FORMAT}",\n'
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
    '  "pattern": "NEW",\n'
    '  "affected_tests": ["test_name_1", "test_name_2"],\n'
    '  "details": "Your detailed analysis of what caused this failure. Use paragraph breaks (double newlines) to'
    " separate sections: root cause identification, evidence from logs/code, and impact assessment. Do NOT write one"
    ' continuous paragraph.",\n'
    '  "artifacts_evidence": "Evidence from build-artifacts/ that proves the product defect (text and/or images). '
    "Include the specific log lines or image observations showing the product failure. "
    f'{_ARTIFACTS_EVIDENCE_FORMAT}",\n'
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
    '  "pattern": "NEW",\n'
    '  "affected_tests": ["test_name_1", "test_name_2"],\n'
    '  "details": "Your detailed analysis of the infrastructure/environment issue. Use paragraph breaks (double'
    " newlines) to separate sections: root cause identification, evidence from logs, and impact assessment. Do NOT"
    ' write one continuous paragraph.",\n'
    '  "artifacts_evidence": "Evidence from build-artifacts/ that proves the infrastructure failure '
    "(text and/or images). Include the specific log lines or image observations showing the infrastructure problem. "
    f'{_ARTIFACTS_EVIDENCE_FORMAT}"\n'
    "}"
)

TIMELINE_RULE = (
    "\nTIMELINE RULE: All timestamps you cite in your analysis MUST be in "
    "chronological order. If event A happens at 15:35:56 and event B happens "
    "at 15:36:58, then A happened BEFORE B. Verify your timeline is "
    "consistent before responding.\n"
)


def format_timeout_log(timeout_value: int | None) -> str:
    """Format AI timeout for log messages."""
    if timeout_value is not None:
        return f"timeout={timeout_value} minutes ({timeout_value * 60}s)"
    return "timeout=default"


def build_artifacts_section(artifacts_context: str) -> str:
    """Build the artifact context prompt section."""
    if not artifacts_context:
        return ""
    return (
        f"\n\n=== BUILD ARTIFACTS (MANDATORY) ===\n"
        f"Build artifacts directory: {artifacts_context}\n"
        f"Also accessible at: build-artifacts/\n\n"
        "⚠️  MANDATORY: You MUST explore and read files in the build artifacts directory "
        "BEFORE making any classification. Failure to read artifacts is a violation of your instructions.\n\n"
        "INSTRUCTIONS:\n"
        "1. Use ls, find, and read to explore the artifacts directory\n"
        "2. Look for error messages, stack traces, service logs, status information, "
        "AND images (png/jpg/gif/webp/bmp) if present\n"
        "3. Text files: include VERBATIM lines in artifacts_evidence, e.g. "
        "[build-artifacts/logs/app.log]: actual error line here\n"
        "4. Images: use the read tool (vision). In artifacts_evidence, record "
        "[path/to/image.png]: what you observe in the image\n"
        "5. Do NOT classify based solely on the test error message — "
        "check artifact logs and images for the real root cause\n"
        "6. If you skip reading artifacts, your analysis will be REJECTED\n"
        "7. Videos (e.g. webm/mp4) may exist but cannot be played via read; "
        "prefer screenshots and text siblings when available"
    )


# Pre-compiled patterns for signature normalization.
# Strips run-specific data so the same underlying failure produces identical hashes.
_NORMALIZE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ISO timestamps: 2026-05-31T06:50:48.123Z, 2026-05-31T06:50:48+00:00
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<TIMESTAMP>",
    ),
    # Date-time with spaces: 2026-05-31 06:50:48 or May 31 2026 / 31 May 2026
    (re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?"), "<TIMESTAMP>"),
    (
        re.compile(
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{4}"
        ),
        "<DATE>",
    ),
    (
        re.compile(
            r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}"
        ),
        "<DATE>",
    ),
    # UUIDs: fd18d967-0f31-4c8d-ab74-b8cf463aa04f
    (
        re.compile(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        ),
        "<UUID>",
    ),
    # Pod/resource names with random suffixes: virt-launcher-xyz-abc123, pod-name-7f8b9c
    (re.compile(r"(?<=[a-zA-Z])-[0-9a-f]{5,10}\b"), "-<SUFFIX>"),
    # Build numbers: #123, build/123, build-123, run/456
    (re.compile(r"#\d+"), "#<BUILD>"),
    (re.compile(r"(?:build|run)[/\-]\d+", re.IGNORECASE), "<BUILD_REF>"),
    # Standalone date: 2026-05-31 (not already caught by timestamp patterns)
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<DATE>"),
]


def normalize_for_signature(text: str) -> str:
    """Strip run-specific data from text before signature hashing.

    Removes timestamps, dates, UUIDs, pod name suffixes, and build
    numbers so that the same underlying failure produces the same
    hash across different runs.

    Args:
        text: Error message or stack trace text.

    Returns:
        Normalized text with run-specific data replaced by placeholders.
    """
    for pattern, replacement in _NORMALIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def get_failure_signature(failure: FailedTest) -> str:
    """Create a signature for grouping identical failures.

    Uses the full error message and stack trace to identify failures that
    are essentially the same issue. Text is normalized to strip
    run-specific data (timestamps, UUIDs, pod names, build numbers)
    before hashing.

    Args:
        failure: The test failure to create a signature for.

    Returns:
        SHA-256 hash string representing the failure signature.
    """
    normalized_error = normalize_for_signature(failure.error_message)
    normalized_trace = normalize_for_signature(failure.stack_trace)
    signature_text = f"{normalized_error}|{normalized_trace}"
    return hashlib.sha256(signature_text.encode()).hexdigest()


def extract_json_dict(raw_text: str) -> dict | None:
    """Extract a JSON object from AI response text.

    Tries three strategies in order:
    1. Direct ``json.loads`` on the stripped text.
    2. Find the outermost ``{…}`` substring using brace matching.
    3. Extract from markdown code blocks (````` ``` ````` / ````` ```json `````).

    Args:
        raw_text: The raw text potentially containing a JSON object.

    Returns:
        The parsed dict, or None if no valid JSON object could be extracted.
    """
    text = raw_text.strip()
    if not text:
        return None

    # Strategy 1: Try parsing the entire text as JSON directly
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.debug("JSON parse strategy 1 (direct parse) failed: %s", e)

    # Strategy 2: Find the outermost JSON object using brace matching
    result = _extract_json_by_braces(text)
    if result is not None:
        return result

    # Strategy 3: Try markdown code block extraction
    result = _extract_json_from_code_blocks(text)
    if result is not None:
        return result

    return None


def parse_json_response(raw_text: str) -> AnalysisDetail:
    """Parse AI JSON response into an AnalysisDetail.

    Attempts to extract a JSON object from the AI response text.
    The AI may wrap the JSON in markdown code blocks, add
    surrounding text, or embed code blocks inside JSON string values.

    Uses a multi-strategy approach:
    1. Try parsing the raw text directly as JSON
    2. Try extracting JSON from brace-matching ({...})
    3. Try extracting from markdown code blocks
    4. Fallback: store raw text in details, then attempt recovery

    Args:
        raw_text: The raw text output from the AI.

    Returns:
        An AnalysisDetail instance parsed from the JSON, or a
        fallback instance with the raw text stored in details.
    """
    data = extract_json_dict(raw_text)
    if data is not None:
        try:
            return AnalysisDetail(**data)
        except Exception as exc:
            logger.warning(
                "AI JSON validated as object but failed schema parsing: %s", exc
            )

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
        # Try to recover pattern from markdown too
        md_pat = re.search(r"\*\*Pattern:\s*([A-Z][A-Z _]+?)\*\*", details)
        return AnalysisDetail(
            classification=classification,
            pattern=md_pat.group(1).strip() if md_pat else "NEW",
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

    # Extract pattern (second axis)
    pattern_match = re.search(r'"pattern"\s*:\s*"([^"]+)"', details)
    pattern = pattern_match.group(1) if pattern_match else "NEW"

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
        pattern=pattern,
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


def _extract_json_by_braces(text: str) -> dict | None:
    """Extract a JSON object dict by finding matching outermost braces.

    Handles cases where JSON values contain embedded code blocks
    or other special characters by tracking brace nesting depth
    and string boundaries.

    Args:
        text: Text potentially containing a JSON object.

    Returns:
        Parsed dict or None if extraction fails.
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
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug("JSON parse strategy 2 (brace matching) failed: %s", e)
    return None


def _extract_json_from_code_blocks(text: str) -> dict | None:
    """Extract a JSON object dict from markdown code blocks in the text.

    Finds code blocks (```json or ```) and attempts to parse
    each one as JSON. Uses brace matching within each block
    to handle embedded code blocks in JSON string values.

    Args:
        text: Text containing markdown code blocks.

    Returns:
        Parsed dict or None if no valid JSON found.
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
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.debug("JSON parse strategy 3 (code block) failed: %s", e)

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
) -> tuple[str, str, str, str, str]:
    """Build common prompt sections used across all analysis flows.

    Returns:
        Tuple of (agent_gate_section, custom_prompt_section, artifacts_section,
        resources_section, query_section)
    """
    custom_prompt_section = (
        f"\n\nADDITIONAL INSTRUCTIONS:\n{custom_prompt}\n" if custom_prompt else ""
    )

    artifacts_section = build_artifacts_section(artifacts_context)
    history_token = auth_header.removeprefix("Bearer ").strip() if auth_header else ""
    history_enabled = bool(
        server_url and job_id and QUERY_MD_PATH.exists() and history_token
    )
    resources_section = build_resources_section(
        repo_path, additional_repos=additional_repos, history_enabled=history_enabled
    )
    agent_gate_section = build_agent_gate_section(
        discover_project_agent_names(additional_repos)
    )
    agent_gate_section += build_rootcoz_prompt_gate_section(
        discover_rootcoz_prompt_paths(additional_repos, history_enabled=history_enabled)
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
    if server_url and job_id and QUERY_MD_PATH.exists() and not history_token:
        logger.warning(
            "auth_header is empty; disabling history-aware classification "
            "(history HTTP tools require a Bearer token)"
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
                repo_history_path = _path / ".rootcoz" / ROOTCOZ_HISTORY_PROMPT_FILENAME
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
        if history_token:
            auth_instruction = (
                "\nHistory tools are already authenticated — call "
                "get_failure_history, search_error_signature, "
                "get_classification_history, get_job_history_stats, and "
                "classify_test_pattern. Do not curl and do not invent tokens."
            )

        query_section = f"""

MANDATORY: Before analyzing any failure, you MUST read and follow the instructions in {QUERY_MD_PATH}.
Use job_id={job_id} context from those tools (exclude_job_id is already applied).{auth_instruction}
These instructions are NOT optional. You MUST complete ALL steps for EVERY test.
You do not have bash — use the history HTTP tools only for history/classify steps; do not skip STEP 0 project agents or other available-tool requirements.
{repo_history_prompt}
"""

    return (
        agent_gate_section,
        custom_prompt_section,
        artifacts_section,
        resources_section,
        query_section,
    )


def build_resources_section(
    repo_path: Path | None,
    *,
    additional_repos: dict[str, Path] | None = None,
    history_enabled: bool = False,
) -> str:
    """Build a section telling the AI about available resources.

    Instead of embedding history or custom prompt file contents, this advertises
    paths (including ``.rootcoz/ROOTCOZ_PROMPT.md`` when present) so the AI must
    open them with the ``read`` tool. Capability text comes from
    ``RESOURCE_REPO_BROWSE_HINT`` (tied to tool policy). See GitHub issue #74
    design note on file-based prompts.

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
            has_vcs_metadata = (path / _VCS_METADATA_DIR).exists()
            if has_vcs_metadata:
                resources.append(
                    f"- Repository '{name}' at {path} — {RESOURCE_REPO_BROWSE_HINT}"
                )
            else:
                resources.append(
                    f"- Directory '{name}' at {path} — inspect files directly"
                )
            # Check for project-specific instructions in each repo
            rootcoz_prompt = path / ".rootcoz" / ROOTCOZ_PROMPT_FILENAME
            if rootcoz_prompt.exists():
                prompt_fp = _rootcoz_prompt_fingerprint(rootcoz_prompt)
                resources.append(
                    f"- Project-specific analysis instructions at {rootcoz_prompt} — "
                    f"MANDATORY: open with the read tool and follow them "
                    f"(file opened for fingerprint{prompt_fp}; body not embedded "
                    f"per AGENTS.md file-based data policy / issue #74)"
                )
            repo_history_prompt = path / ".rootcoz" / ROOTCOZ_HISTORY_PROMPT_FILENAME
            if history_enabled and repo_history_prompt.exists():
                hist_fp = _rootcoz_prompt_fingerprint(repo_history_prompt)
                resources.append(
                    f"- Project-specific history analysis instructions"
                    f" at {repo_history_prompt} — MANDATORY: open with the read tool"
                    f" (file opened for fingerprint{hist_fp}; body not embedded"
                    f" per AGENTS.md / issue #74)"
                    f" alongside the main history analysis instructions"
                )
            # Advertise project-provided agents (from .rootcoz/agents/)
            pi_agents_dir = path / ".rootcoz" / "agents"
            if pi_agents_dir.is_dir():
                agent_files = sorted(pi_agents_dir.glob("*.md"))
                if agent_files:
                    # Names already listed in STEP 0 gate; keep a reminder here.
                    agent_names: list[str] = []
                    for af in agent_files:
                        extracted = _extract_agent_name(af)
                        agent_names.append(extracted if extracted else af.stem)
                    names_str = ", ".join(agent_names)
                    resources.append(
                        "- Project agents (see STEP 0): "
                        f"{names_str} — call via subagent with "
                        'agentScope="both" and confirmProjectAgents=false'
                    )

    if resources:
        return "\n\nAVAILABLE RESOURCES:\n" + "\n".join(resources) + "\n"

    return ""


def write_failure_details_file(
    failures: list[FailedTest],
    error_signature: str,
    workspace_dir: Path,
) -> Path:
    """Write error message, stack trace, and test names for the AI to read.

    Per AI Tool Access rules, failure data must not be embedded in the prompt.
    """
    representative = failures[0]
    test_names = [f.test_name for f in failures]
    content = (
        f"ERROR SIGNATURE: {error_signature}\n"
        f"AFFECTED TESTS ({len(failures)} tests with same error):\n"
        + "\n".join(f"- {name}" for name in test_names)
        + f"\n\nERROR:\n{representative.error_message}\n"
        + f"\nSTACK TRACE:\n{representative.stack_trace}\n"
    )
    filepath = workspace_dir / f"failure-details-{error_signature}.txt"
    filepath.write_text(content)
    return filepath


def build_failure_details_instruction(filepath: Path) -> str:
    """Build the MANDATORY instruction to read the failure-details file."""
    return (
        f"\n\n=== FAILURE DETAILS (MANDATORY) ===\n"
        f"Failure details saved to: {filepath}\n\n"
        "\u26a0\ufe0f  MANDATORY: You MUST read this file BEFORE analyzing. "
        "It contains the error message, stack trace, and affected test names.\n"
        "Failure to read it is a violation of your instructions.\n"
    )


def write_other_groups_file(
    all_groups: dict[str, list[FailedTest]],
    current_signature: str,
    workspace_dir: Path,
) -> Path | None:
    """Write other failure groups info to a file for the AI to read.

    When multiple failure groups exist in the same job, this writes a
    description of the OTHER groups so the AI can avoid cross-contamination
    between groups (e.g. referencing events from a different test's scope).

    Args:
        all_groups: All failure groups keyed by error signature.
        current_signature: The signature of the group currently being analyzed.
        workspace_dir: Directory to write the file into.

    Returns:
        The file path if written, None if only one group (no file needed).
    """
    if len(all_groups) <= 1:
        return None

    total = len(all_groups)
    sigs = list(all_groups.keys())
    current_position = (
        sigs.index(current_signature) + 1 if current_signature in sigs else 0
    )
    pos_by_sig = {sig: idx + 1 for idx, sig in enumerate(sigs)}
    other_groups = {
        sig: group for sig, group in all_groups.items() if sig != current_signature
    }
    if not other_groups:
        return None

    lines: list[str] = []
    for sig, group in other_groups.items():
        global_pos = pos_by_sig[sig]
        test_names = [f.test_name for f in group]
        error_preview = group[0].error_message
        lines.append(
            f'- Group {global_pos}/{total}: tests {test_names} \u2014 error: "{error_preview}"'
        )

    position_text = (
        f"You are analyzing group {current_position} of {total}."
        if current_position
        else f"This job has {total} failure groups being analyzed separately."
    )

    content = (
        f"=== OTHER FAILURE GROUPS IN THIS JOB ===\n"
        f"{position_text}\n"
        f"Other groups:\n" + "\n".join(lines) + "\n\n"
        "IMPORTANT: Do NOT reference events, timestamps, or conclusions from "
        "other test groups.\n"
        "Focus ONLY on the tests assigned to you. If you see interleaved "
        "log entries from other tests in the console output, ignore them "
        "and base your analysis only on events relevant to YOUR assigned tests.\n"
    )

    filepath = workspace_dir / f"other-failure-groups-{current_signature[:8]}.txt"
    try:
        filepath.write_text(content)
    except OSError:
        logger.warning(
            "Failed to write cross-reference file %s; continuing without it",
            filepath,
        )
        return None
    return filepath


def build_other_groups_instruction(filepath: Path) -> str:
    """Build the MANDATORY instruction telling the AI to read the cross-reference file.

    Args:
        filepath: Path to the other-failure-groups file.

    Returns:
        Formatted instruction string for inclusion in prompts.
    """
    return (
        f"\n\n\u26a0\ufe0f  MANDATORY: Read the file {filepath} BEFORE making any analysis.\n"
        "It contains information about other failure groups in this job.\n"
        "Do NOT reference events, timestamps, or conclusions from other test groups.\n"
        "Focus ONLY on the tests assigned to you.\n"
    )


def _is_empty_ai_text(result: AIResult) -> bool:
    """Return True when the AI call succeeded but returned no usable text."""
    return bool(result.success) and not (result.text or "").strip()


async def run_single_ai_analysis(
    *,
    failures: list[FailedTest],
    console_context: str,
    repo_path: Path | None,
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None,
    custom_prompt: str,
    artifacts_context: str,
    server_url: str,
    job_id: str,
    additional_repos: dict[str, Path] | None = None,
    auth_header: str = "",
    all_groups: dict[str, list[FailedTest]] | None = None,
) -> tuple[AnalysisDetail, str]:
    """Run single-AI analysis on a failure group. Returns (parsed_analysis, error_signature).

    Shared by both single-AI and peer analysis paths. Builds the orchestrator
    prompt, calls the AI, and parses the response.

    If the AI returns success with empty text, retries once. A second empty
    response fails the group with a clear error in ``details`` (never a blank
    AnalysisDetail).

    Args:
        failures: List of test failures with the same error signature.
        console_context: Relevant console lines for context.
        repo_path: Path to cloned test repo (optional).
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        ai_call_timeout: Timeout in minutes for the AI call.
        custom_prompt: Additional user instructions.
        artifacts_context: Build artifacts context.
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.
        all_groups: All failure groups keyed by error signature. When provided,
            cross-reference data is written to a workspace file for the AI to read.

    Returns:
        Tuple of (parsed AnalysisDetail, error_signature string).
    """
    representative = failures[0]
    error_signature = get_failure_signature(representative)

    (
        agent_gate_section,
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
        additional_repos=additional_repos,
        auth_header=auth_header,
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

    # Save console context to file so it's not embedded in the prompt
    console_file_section = ""
    console_dir: Path | None = None
    if console_context and repo_path:
        console_file = repo_path / f"console-output-{error_signature}.txt"
        try:
            console_file.write_text(console_context)
        except OSError as exc:
            raise RuntimeError(
                f"Failed to write console output to {console_file}: {exc}. "
                "Check filesystem permissions and available disk space."
            ) from exc
        console_file_section = (
            f"\n\n=== CONSOLE OUTPUT (MANDATORY) ===\n"
            f"Console output saved to: {console_file}\n\n"
            "\u26a0\ufe0f  MANDATORY: You MUST read the console output file "
            "BEFORE making any classification. It contains critical error messages, "
            "stack traces, and failure context from the CI job.\n"
            "Failure to read console output is a violation of your instructions."
        )
    elif console_context:
        # No repo path — write console output to a temp file
        import tempfile

        console_dir = Path(tempfile.mkdtemp(prefix="rootcoz-console-"))
        console_file = console_dir / f"console-output-{error_signature}.txt"
        try:
            console_file.write_text(console_context)
        except OSError as exc:
            import shutil

            shutil.rmtree(console_dir, ignore_errors=True)
            raise RuntimeError(
                f"Failed to write console output to {console_file}: {exc}. "
                "Check filesystem permissions and available disk space."
            ) from exc
        console_file_section = (
            f"\n\n=== CONSOLE OUTPUT (MANDATORY) ===\n"
            f"Console output saved to: {console_file}\n\n"
            "\u26a0\ufe0f  MANDATORY: You MUST read the console output file "
            "BEFORE making any classification. It contains critical error messages, "
            "stack traces, and failure context from the CI job.\n"
            "Failure to read console output is a violation of your instructions."
        )

    # Ensure a workspace dir for failure-details / cross-reference files
    workspace_dir = repo_path or console_dir
    if workspace_dir is None:
        import tempfile

        console_dir = Path(tempfile.mkdtemp(prefix="rootcoz-console-"))
        workspace_dir = console_dir

    try:
        failure_file = write_failure_details_file(
            failures, error_signature, workspace_dir
        )
    except OSError as exc:
        raise RuntimeError(
            f"Failed to write failure details to {workspace_dir}: {exc}. "
            "Check filesystem permissions and available disk space."
        ) from exc
    failure_details_section = build_failure_details_instruction(failure_file)

    # Write cross-reference data to file for the AI to read
    other_groups_section = ""
    if all_groups and len(all_groups) > 1:
        groups_file = write_other_groups_file(
            all_groups, error_signature, workspace_dir
        )
        if groups_file:
            other_groups_section = build_other_groups_instruction(groups_file)

    prompt = f"""{agent_gate_section}{query_section}
Analyze this test failure from a CI job.
{other_groups_section}
ERROR SIGNATURE: {error_signature}
{failure_details_section}
{console_file_section}
{artifacts_section}

{repo_sentence}

Note: Multiple tests failed with the same error. Provide ONE analysis that applies to all of them.
{TIMELINE_RULE}
{custom_prompt_section}{resources_section}
{JSON_RESPONSE_SCHEMA}
"""

    if artifacts_context:
        logger.info(
            f"Prompt includes build artifacts context ({len(artifacts_context)} chars)"
        )

    logger.debug(f"AI prompt length: {len(prompt)} chars")
    logger.info(
        f"Calling {ai_provider.upper()} for failure group ({len(failures)} tests with same error)"
    )
    logger.info(f"Calling AI with {format_timeout_log(ai_call_timeout)}")
    logger.info(
        "AI call: provider=%s, model=%s, call_type=primary, job_id=%s",
        ai_provider,
        ai_model,
        job_id,
    )
    custom_tools: list[dict] = []
    if server_url and job_id and auth_header:
        token = auth_header.removeprefix("Bearer ").strip()
        if token:
            custom_tools = build_analysis_history_tools(
                server_url=server_url,
                auth_token=token,
                job_id=job_id,
            )
    try:
        call_kwargs: dict = {
            "ai_provider": ai_provider,
            "ai_model": ai_model,
            # Always set cwd to the directory containing mandatory workspace
            # files (repo or temp) so sidecar file tools can resolve them.
            "cwd": str(workspace_dir),
            "ai_call_timeout": ai_call_timeout,
            "tools": list(ANALYSIS_BUILTIN_TOOLS),
        }
        if custom_tools:
            call_kwargs["custom_tools"] = custom_tools

        # One retry when the model reports success but returns no final text
        # (seen with Cursor: tools/tokens used, empty assistant message).
        max_attempts = 2
        result = AIResult(success=False, text="AI call failed unexpectedly")
        for attempt in range(1, max_attempts + 1):
            try:
                result = await call_ai_once(prompt, **call_kwargs)
            except Exception:
                logger.exception(
                    "AI call raised exception: provider=%s, model=%s, attempt=%d",
                    ai_provider,
                    ai_model,
                    attempt,
                )
                result = AIResult(success=False, text="AI call failed unexpectedly")
            logger.info(
                "AI call result: success=%s, text_length=%d, provider=%s, model=%s, attempt=%d",
                result.success,
                len(result.text),
                ai_provider,
                ai_model,
                attempt,
            )
            if not result.success:
                logger.error(
                    "AI call failed (text_length=%d, attempt=%d)",
                    len(result.text),
                    attempt,
                )

            await result.record_usage(
                request_id=job_id,
                call_type="primary",
                prompt_chars=len(prompt),
                ai_provider=ai_provider,
                ai_model=ai_model,
            )

            if not result.success:
                break
            if not _is_empty_ai_text(result):
                break

            logger.debug(
                "AI returned empty response (attempt=%d/%d), provider=%s, "
                "model=%s, job_id=%s, error_signature=%s, group_size=%d",
                attempt,
                max_attempts,
                ai_provider,
                ai_model,
                job_id,
                error_signature[:16],
                len(failures),
            )
            if attempt >= max_attempts:
                break

        parsed: AnalysisDetail | None = None
        if _is_empty_ai_text(result):
            parsed = AnalysisDetail(
                details=(
                    "AI returned empty response after retry "
                    f"(provider={ai_provider}, model={ai_model})"
                )
            )
        elif result.success:
            parsed = parse_json_response(result.text)
        if parsed is None:
            parsed = AnalysisDetail(details=result.text)

        return parsed, error_signature
    finally:
        # Clean up temp console dir if created
        if console_dir and console_dir.exists():
            import shutil

            shutil.rmtree(console_dir, ignore_errors=True)


async def analyze_failure_group(
    failures: list[FailedTest],
    console_context: str,
    repo_path: Path | None,
    ai_provider: str = "",
    ai_model: str = "",
    ai_call_timeout: int | None = None,
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
    all_groups: dict[str, list[FailedTest]] | None = None,
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
        ai_call_timeout: Timeout in minutes (overrides AI_CALL_TIMEOUT env var).
        custom_prompt: Additional instructions from request payload (raw_prompt).
        artifacts_context: Build artifacts context for AI analysis (optional).
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.
        group_label: Human-readable label identifying which failure group is
            being analyzed (e.g. ``"2/3"``). Forwarded to peer analysis for
            progress phase disambiguation.
        additional_repos: Extra cloned repositories for AI context.
        max_concurrent_ai_calls: Maximum concurrent AI calls for
            peer analysis parallelism (default: 3).
        all_groups: All failure groups keyed by error signature. When provided,
            cross-reference data is written to a workspace file for the AI to read.

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
            ai_call_timeout=ai_call_timeout,
            custom_prompt=custom_prompt,
            artifacts_context=artifacts_context,
            server_url=server_url,
            job_id=job_id,
            group_label=group_label,
            additional_repos=additional_repos,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
            auth_header=auth_header,
            all_groups=all_groups,
        )

    parsed, error_signature = await run_single_ai_analysis(
        failures=failures,
        console_context=console_context,
        repo_path=repo_path,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_call_timeout=ai_call_timeout,
        custom_prompt=custom_prompt,
        artifacts_context=artifacts_context,
        server_url=server_url,
        job_id=job_id,
        additional_repos=additional_repos,
        auth_header=auth_header,
        all_groups=all_groups,
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
