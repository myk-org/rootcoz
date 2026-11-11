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
import logging
import os
import re
import shutil
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from git.exc import GitCommandError
from pi_sidecar_client import run_parallel_with_limit

from rootcoz import get_child_logger
from rootcoz.ai_client import (
    ANALYSIS_BUILTIN_TOOLS,
    RESOURCE_REPO_BROWSE_HINT,
    SESSION_REFUSAL_MARKER,
    AIResult,
    _is_refusal_text,
    call_ai_once,
)
from rootcoz.config import Settings, get_settings, parse_additional_repos
from rootcoz.engine.chat import analysis_http_tools
from rootcoz.engine.session_tools import build_session_tools
from rootcoz.feedback import scrub_sensitive_data
from rootcoz.models import (
    ANALYSIS_PLACEHOLDER,
    AdditionalRepo,
    AiConfigEntry,
    AnalysisDetail,
    BaseAnalysisRequest,
    CodeFix,
    CrossFailurePattern,
    FailedTest,
    FailureAnalysis,
    PeerDebate,
    ProductBugReport,
)
from rootcoz.repository import RepositoryManager
from rootcoz.storage import (
    PRIMARY_CLASSIFICATIONS,
    update_clone_progress,
    update_progress_phase,
)
from rootcoz.token_tracking import failure_group_usage

logger = get_child_logger(__name__)


def require_workspace(repo_path: Path | None) -> Path:
    """Return the analysis workspace, or refuse. No caller may analyse without one (#443)."""
    if repo_path is None:
        raise RuntimeError(
            "Analysis requires a workspace path; every caller creates one before analysing "
            "(see #443). Pass repo_manager.create_workspace()."
        )
    return repo_path


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


# A thread lock keeps the process-wide count safe even across event loops.
_clone_lock = threading.Lock()
_active_clones = 0


def _job_clone_share(settings: Settings) -> int:
    """How many clones one caller (analysis or chat) may run at once.

    The clone ceiling itself. #466 capped this at ``max_concurrent_ai_calls`` as
    a fairness proxy, and the effect was that ``max_concurrent_repo_clones``
    went inert above the AI width: a deployment with
    ``max_concurrent_repo_clones=5`` and the default ``max_concurrent_ai_calls=3``
    observed 3, so raising the clone ceiling changed nothing (#547). The AI
    width describes LLM calls, not disk and network, and it has no bearing on
    cloning.

    Fairness is not lost: ``_clone_lock``/``_active_clones`` enforce the one
    process-wide ceiling every caller competes for, so a single caller cannot
    occupy more than the host budget by construction. Per-caller fairness would
    need a setting that says so -- not an LLM call width.

    The caller passes the settings it already read for this admission attempt,
    so one decision costs one ``get_settings()`` call and a live change applies
    on the next attempt. A repo-side or per-submission override does not widen
    it: the clone budget is a host resource, not a job preference.
    """
    return settings.max_concurrent_repo_clones


@dataclass(frozen=True, slots=True)
class CloneRequest:
    """One repository clone, as the shared fan-out needs it."""

    name: str
    url: str
    target: Path
    depth: int
    branch: str = ""
    token: str | None = None


async def _admit_clone(active_here: list[int]) -> Callable[[], None]:
    """Wait for a free clone slot, then return the callback that frees it.

    ``active_here[0]`` is this batch's own count, so one batch cannot occupy the
    host's whole budget. The budget itself is process-wide and shared by every
    caller, analysis and chat alike (#461), and it is the clone ceiling alone
    -- never the AI call width (#547).
    """
    global _active_clones
    while True:
        # One snapshot per attempt, *before* the lock: the decision needs both
        # numbers and re-reading settings while holding the lock pays for the
        # same read twice on the hot path.
        settings = get_settings()
        share = _job_clone_share(settings)
        with _clone_lock:
            admitted = (
                _active_clones < settings.max_concurrent_repo_clones
                and active_here[0] < share
            )
            if admitted:
                _active_clones += 1
                active_here[0] += 1
        if admitted:
            break
        # ponytail: poll while full; replace with per-loop notifications if
        # clone admission latency becomes material.
        await asyncio.sleep(0.05)

    def _release() -> None:
        global _active_clones
        with _clone_lock:
            _active_clones -= 1
            active_here[0] -= 1

    return _release


async def clone_repo_batch(
    repo_manager: RepositoryManager,
    requests: Sequence[CloneRequest],
    *,
    on_start: Callable[[CloneRequest], Awaitable[None]] | None = None,
    on_finish: Callable[[CloneRequest, str], Awaitable[None]] | None = None,
    on_error: Callable[[str, BaseException], None] | None = None,
) -> list[tuple[CloneRequest, BaseException | None]]:
    """Clone every request concurrently, under the one clone admission budget.

    The only clone fan-out in the codebase: analysis and chat both come through
    here, so N repos cost one clone's latency instead of the sum, and both
    compete for the same slots.

    ``on_start`` runs once admitted and before the clone starts, ``on_finish``
    receives the outcome (``cloned`` / ``failed`` / ``cancelled``), ``on_error``
    is the per-repo failure hook (a cancelled clone is not a failure, so it is
    not reported). Returns (request, error) per request, in declaration order:
    the caller decides what a failure means -- chat warns and carries on,
    analysis re-raises the unexpected ones.
    """
    active_here = [0]

    async def _clone_one(request: CloneRequest) -> Path:
        release = await _admit_clone(active_here)
        clone_task: asyncio.Task[Path] | None = None
        outcome = "cancelled"
        try:
            if on_start is not None:
                await on_start(request)
            clone_task = asyncio.create_task(
                asyncio.to_thread(
                    repo_manager.clone_into,
                    request.url,
                    request.target,
                    depth=request.depth,
                    branch=request.branch,
                    token=request.token or None,
                )
            )

            def release_slot(task: asyncio.Task[Path]) -> None:
                if not task.cancelled():
                    task.exception()  # Observe failures even when the caller was cancelled.
                release()

            clone_task.add_done_callback(release_slot)
            await asyncio.shield(clone_task)
            outcome = "cloned"
            return request.target
        except Exception:
            outcome = "failed"
            raise
        finally:
            if clone_task is None:
                release()
            if on_finish is not None:
                await on_finish(request, outcome)

    results = await asyncio.gather(
        *[_clone_one(request) for request in requests], return_exceptions=True
    )
    outcomes: list[tuple[CloneRequest, BaseException | None]] = []
    for request, result in zip(requests, results, strict=True):
        error = result if isinstance(result, BaseException) else None
        if (
            error is not None
            and not isinstance(error, asyncio.CancelledError)
            and on_error is not None
        ):
            on_error(request.name, error)
        outcomes.append((request, error))
    return outcomes


async def clone_additional_repos(
    repo_manager: RepositoryManager,
    additional_repos_list: list[AdditionalRepo],
    repo_path: Path,
    job_id: str = "",
    *,
    reanalysis: bool = False,
    operation_id: str = "",
) -> tuple[dict[str, Path], Path]:
    """Clone additional repositories for AI analysis context.

    Clones all repos as subdirectories of the provided workspace path.

    Every declared repo is attempted up front, before the first AI call: never
    lazily on a read miss, and never filtered to a subset for speed (#402 C4).
    One repo may run out of admission or fail to clone -- the failure is warned
    about and the analysis continues, because a missing repo is worse than a
    slow one.

    Args:
        repo_manager: Repository manager for cloning.
        additional_repos_list: List of AdditionalRepo objects.
        repo_path: Workspace path (always provided by caller).

    Returns:
        Tuple of (cloned repos dict mapping name to path, repo_path).

    Raises:
        ValueError: two declared repos share a name. The name is the clone
            directory, so a second repo would overwrite the first and one of
            them would vanish from the workspace and the prompt.
    """
    names = [ar.name for ar in additional_repos_list]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        # Every declaration path rejects duplicates already; this is the one
        # place a collapse into ``cloned`` could happen, so report, never drop.
        raise ValueError(f"Duplicate additional repo names: {', '.join(dupes)}")

    cloned: dict[str, Path] = {}
    settings = get_settings()
    logger.info(
        "Cloning %d additional repo(s) at most %d at a time "
        "(max_concurrent_repo_clones=%d, job_id=%s)",
        len(additional_repos_list),
        _job_clone_share(settings),
        settings.max_concurrent_repo_clones,
        job_id or "-",
    )

    def _operation_id(name: str) -> str:
        return f"{operation_id}:additional:{name}" if operation_id else ""

    async def _started(request: CloneRequest) -> None:
        if reanalysis:
            await safe_update_clone_progress(
                job_id,
                request.name,
                True,
                reanalysis=True,
                operation_id=_operation_id(request.name),
                url=request.url,
                ref=request.branch,
            )
        else:
            await safe_update_clone_progress(
                job_id, request.name, True, url=request.url, ref=request.branch
            )

    async def _finished(request: CloneRequest, outcome: str) -> None:
        if reanalysis:
            await safe_update_clone_progress(
                job_id,
                request.name,
                False,
                state=outcome,
                reanalysis=True,
                operation_id=_operation_id(request.name),
            )
        elif outcome == "cloned":
            await safe_update_clone_progress(job_id, request.name, False)
        else:
            await safe_update_clone_progress(job_id, request.name, False, state=outcome)
        if outcome == "cloned":
            logger.info(
                "Cloned additional repo '%s' into %s", request.name, request.target
            )

    outcomes = await clone_repo_batch(
        repo_manager,
        [
            CloneRequest(
                name=ar.name,
                url=str(ar.url),
                target=repo_path / ar.name,
                depth=1,
                branch=ar.ref,
                token=ar.token or None,
            )
            for ar in additional_repos_list
        ],
        on_start=_started,
        on_finish=_finished,
    )

    for request, error in outcomes:
        if error is None:
            cloned[request.name] = request.target
            continue
        if isinstance(error, asyncio.CancelledError):
            continue
        if isinstance(error, (GitCommandError, ValueError, OSError, RuntimeError)):
            # non-fatal additional repo clone failure
            logger.warning(
                "Failed to clone additional repo '%s' (%s)",
                request.name,
                type(error).__name__,
            )
            continue
        # An exception outside the known set is unexpected, so fail the job
        # instead of analyzing with that repo silently missing.
        logger.error(
            "Additional repo '%s' failed unexpectedly: %s",
            request.name,
            type(error).__name__,
        )
        raise error

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


async def safe_update_clone_progress(
    job_id: str | None,
    repo_name: str,
    started: bool,
    *,
    state: str = "cloned",
    reanalysis: bool = False,
    url: str = "",
    ref: str = "",
    operation_id: str = "",
) -> None:
    """Persist a named clone transition and notify SSE listeners."""
    if not job_id:
        return
    try:
        if reanalysis:
            await update_clone_progress(
                job_id,
                repo_name,
                started,
                state=state,
                reanalysis=True,
                url=url,
                ref=ref,
                operation_id=operation_id,
            )
        elif started:
            await update_clone_progress(job_id, repo_name, started, url=url, ref=ref)
        elif state != "cloned":
            await update_clone_progress(job_id, repo_name, started, state=state)
        else:
            await update_clone_progress(job_id, repo_name, started)
        if _on_progress_updated:
            _on_progress_updated(job_id)
        logger.info(
            "Repository clone %s: job=%s repo=%s",
            "cloning" if started else state,
            job_id,
            repo_name,
        )
    except Exception:
        logger.warning(
            "Failed to update clone progress for job=%s", job_id, exc_info=True
        )


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


# Cap fingerprint hashing for huge repo-controlled prompt files (AI still reads
# the full file via the read tool; this only limits hash CPU/memory).
_MAX_ROOTCOZ_PROMPT_FINGERPRINT_BYTES = 16 * 1024 * 1024
_FINGERPRINT_READ_CHUNK = 64 * 1024


def _rootcoz_prompt_fingerprint(path: Path) -> str:
    """Open a ``.rootcoz`` prompt file and return a content fingerprint.

    Hashes the file in chunks (never ``read_bytes()`` of the whole file).
    Advertises only sha256/size — never the body — so the AI must use the
    ``read`` tool (AGENTS.md file-based data policy / issue #74).

    Files larger than ``_MAX_ROOTCOZ_PROMPT_FINGERPRINT_BYTES`` are still
    advertised with ``bytes=`` but ``sha256=skipped`` to avoid worst-case
    hash cost; the AI must still open the full file with ``read``.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size > _MAX_ROOTCOZ_PROMPT_FINGERPRINT_BYTES:
        return f", sha256=skipped, bytes={size}"
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_FINGERPRINT_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return ""
    return f", sha256={digest.hexdigest()}, bytes={size}"


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
    except OSError, UnicodeDecodeError:
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
    """Build a top-of-prompt gate so models read project agent files first.

    Analysis sessions have no ``subagent`` tool — agents are guidance files
    under ``.pi/agents/`` that must be read with the ``read`` tool.
    """
    if not agent_names:
        return ""
    names_str = ", ".join(agent_names)
    return f"""
STEP 0 — PROJECT AGENT CONTEXT (READ BEFORE ANALYSIS):
The following project agents are available: {names_str}
MANDATORY: Read each agent's file in .pi/agents/ to understand project-specific analysis instructions.
Incorporate their guidance into your analysis.
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
                    subdir: str = subdir,
                    dest: Path = dest,
                    repo_name: str = repo_name,
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
    "Tool calls (read, ls, find, grep) are required BEFORE that final JSON whenever AVAILABLE RESOURCES"
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


def _canonical_header(match: re.Match[str]) -> str:
    """Normalize an HTTP header line to a case-stable placeholder."""
    return f"{match.group(1).lower()}: <HEADER>"


# Allowlist of HTTP response headers whose *values* are per-request noise, so
# header churn (ids, lengths, encodings, routing, trace propagation) never
# splits a signature. Deliberately not a blanket ``x-*``: custom headers such as
# ``X-Error-Code`` describe the failure itself, and discarding their values would
# merge distinct failures into one signature.
_HEADER_NAMES = (
    r"date|content-length|content-encoding|content-range|transfer-encoding"
    r"|connection|keep-alive|vary|etag|last-modified|age|expires|cache-control"
    r"|retry-after|via|request-id|cf-ray|traceparent|tracestate"
    r"|x-request-id|x-amzn-request-id|x-amzn-trace-id|x-correlation-id"
    r"|x-trace-id|x-github-request-id|x-cache|x-served-by|x-timer"
    r"|x-oauth-scopes|x-ratelimit-[a-z0-9-]+"
    r"|x-b3-[a-z0-9-]+|x-envoy-[a-z0-9-]+|x-forwarded-[a-z0-9-]+"
)

# Frozen copy of the normalization rules in force before the v2 rules were added
# (issue #239). Never edit, never reorder, never "fix" -- these hashes are the
# anchor every stored row is filed under, and recomputing them differently would
# make historical rows unmatchable (the one thing this design must never do).
_LEGACY_NORMALIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<TIMESTAMP>",
    ),
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
    (
        re.compile(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        ),
        "<UUID>",
    ),
    (re.compile(r"(?<=[a-zA-Z])-[0-9a-f]{5,10}\b"), "-<SUFFIX>"),
    (re.compile(r"#\d+"), "#<BUILD>"),
    (re.compile(r"(?:build|run)[/\-]\d+", re.IGNORECASE), "<BUILD_REF>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<DATE>"),
]


# Pre-compiled patterns for signature normalization.
# Strips run-specific data so the same underlying failure produces identical hashes.
_NORMALIZE_PATTERNS: list[
    tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]
] = [
    # HTTP response header lines: "Date: Sun, 31 May 2026 06:50:48 GMT",
    # "Content-Length: 812", "X-Request-Id: 9c1f...". Must run before the date
    # rules below, otherwise the date part is replaced and the time-of-day
    # survives as new signature noise.
    (
        re.compile(rf"(?im)^[ \t]*({_HEADER_NAMES})[ \t]*:[^\r\n]*$"),
        _canonical_header,
    ),
    # RFC 1123 date-time (surviving time-of-day of a partially normalized date):
    # "Sun, 31 May 2026 06:50:48 GMT"
    (
        re.compile(
            r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} "
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} "
            r"\d{2}:\d{2}:\d{2}(?: \w+)?"
        ),
        "<DATE>",
    ),
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
    # Runtime pointers: 0x7f3c8a1b2c40 (must run after UUID/pod-suffix rules).
    (re.compile(r"\b0[xX][0-9a-fA-F]{6,}\b"), "<PTR>"),
    # Bare long hex tokens: heap/object addresses, 7f3c8a1b2c40, commit SHAs.
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<HEX>"),
]


def normalize_for_signature(text: str) -> str:
    """Strip run-specific data from text before signature hashing.

    Removes timestamps, dates, HTTP response-header noise, UUIDs, pod name
    suffixes, build numbers, and runtime pointer/hex tokens so that the same
    underlying failure produces the same hash across different runs.

    Args:
        text: Error message or stack trace text.

    Returns:
        Normalized text with run-specific data replaced by placeholders.
    """
    for pattern, replacement in _NORMALIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _hash_normalized(
    error_message: str, stack_trace: str, patterns: list[tuple[Any, Any]]
) -> str:
    """Apply *patterns* to both texts and hash the joined result."""
    texts = []
    for text in (error_message, stack_trace):
        for pattern, replacement in patterns:
            text = pattern.sub(replacement, text)
        texts.append(text)
    return hashlib.sha256(f"{texts[0]}|{texts[1]}".encode()).hexdigest()


def compute_signature(error_message: str, stack_trace: str) -> str:
    """Hash an error message and stack trace with the current rules.

    This is the v2 hash: stored in ``error_signature_v2`` and used to group
    failures within a run. It is deliberately NOT the value stored in
    ``error_signature`` -- see :func:`compute_legacy_signature`.

    Args:
        error_message: Error message text.
        stack_trace: Stack trace text.

    Returns:
        SHA-256 hash string representing the failure signature.
    """
    return _hash_normalized(error_message, stack_trace, _NORMALIZE_PATTERNS)


def compute_legacy_signature(error_message: str, stack_trace: str) -> str:
    """Hash an error message and stack trace with the frozen pre-v2 rules.

    The anchor hash: every row ever written carries this value in
    ``error_signature``, so history matching and auto-review keep reaching rows
    stored before the v2 rules existed. Rows are never re-signed, only added to.

    Args:
        error_message: Error message text.
        stack_trace: Stack trace text.

    Returns:
        SHA-256 hash string, stable across all future rule changes.
    """
    return _hash_normalized(error_message, stack_trace, _LEGACY_NORMALIZE_PATTERNS)


def get_failure_signature(failure: FailedTest) -> str:
    """Create the current-rules (v2) signature for grouping identical failures.

    Uses the full error message and stack trace to identify failures that
    are essentially the same issue. Text is normalized to strip
    run-specific data (timestamps, UUIDs, pod names, build numbers, HTTP
    header noise, pointer/hex tokens) before hashing. This is the grouping
    key within one analysis; it is stored in ``error_signature_v2`` and is
    never used as a history-match key on its own.

    Args:
        failure: The test failure to create a signature for.

    Returns:
        SHA-256 hash string representing the failure signature.
    """
    return compute_signature(failure.error_message, failure.stack_trace)


def get_legacy_signature(failure: FailedTest) -> str:
    """Create the frozen anchor signature for *failure*.

    Stored in ``error_signature``, which every row written before the v2 rules
    also carries -- so a row written today still matches its own history from
    last month. See :func:`compute_legacy_signature`.

    Args:
        failure: The test failure to create a signature for.

    Returns:
        SHA-256 hash string representing the failure's anchor signature.
    """
    return compute_legacy_signature(failure.error_message, failure.stack_trace)


def build_signature_section(failures: list[FailedTest], group_signature: str) -> str:
    """Prompt line(s) naming every hash the AI must search history for.

    A v2 group's members carry *different* frozen anchors -- that is exactly why
    they grouped. Naming only the representative's anchor drops the rest of the
    group's history, so the whole set goes in the prompt: each member's anchor
    (which reaches rows stored before the v2 rules) plus the group's v2 hash.

    The whole set is one comma-separated ``signature`` value, not one line per
    hash: ``search_error_signature`` takes them all in a single call, so a group
    of any size still costs the AI exactly one history search. A per-hash line
    list made that cost scale with the group, and the groups the v2 rules merge
    are the large ones.
    """
    hashes = list(
        dict.fromkeys(
            h
            for failure in failures
            for h in (get_legacy_signature(failure), group_signature)
            if h
        )
    )
    if len(hashes) <= 1:
        return f"ERROR SIGNATURE: {hashes[0] if hashes else group_signature}\n"
    return (
        "ERROR SIGNATURES: this failure group has several hashes. Call "
        "search_error_signature ONCE with every hash below as a single "
        "comma-separated 'signature' value and read the combined results:\n"
        + ", ".join(hashes)
        + "\n"
    )


def extract_json_dict(raw_text: str) -> dict[str, Any] | None:
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
        except json.JSONDecodeError as e:
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


def _drop_ai_supplied_tracker_matches(analysis: AnalysisDetail) -> None:
    """Blank tracker matches an AI answer supplied — only enrichment sets them.

    ``jira_matches``/``github_matches`` are documented as populated in
    post-processing, but the answer is parsed into the model as-is, so nothing
    stops a model from filling them. Attached matches must always be ones a
    tracker actually returned.
    """
    report = analysis.product_bug_report
    if not isinstance(report, ProductBugReport):
        return
    if not (report.jira_matches or report.github_matches):
        return
    logger.warning(
        "AI answer supplied tracker matches (jira=%d, github=%d); dropping them",
        len(report.jira_matches),
        len(report.github_matches),
    )
    report.jira_matches = []
    report.github_matches = []


def parse_json_response(raw_text: str) -> AnalysisDetail | None:
    """Parse AI JSON response into an AnalysisDetail.

    Attempts to extract a JSON object from the AI response text.
    The AI may wrap the JSON in markdown code blocks, add
    surrounding text, or embed code blocks inside JSON string values.

    Uses a multi-strategy approach:
    1. Try parsing the raw text directly as JSON
    2. Try extracting JSON from brace-matching ({...})
    3. Try extracting from markdown code blocks
    4. Fallback: regex/markdown recovery, only for text that did not parse as
       JSON — a structured answer that names no usable classification stands

    A reply is only an analysis when it names a classification from the
    taxonomy (``PRIMARY_CLASSIFICATIONS``). Narration without a final answer is
    not one: it used to be wrapped into ``details`` and stored as a successful
    analysis, which is how an empty classification reached the exporters.

    Args:
        raw_text: The raw text output from the AI.

    Returns:
        An AnalysisDetail parsed from the JSON, or None when the answer carries
        no usable classification.
    """
    data = extract_json_dict(raw_text)
    if data is not None:
        try:
            parsed = AnalysisDetail(**data)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "AI JSON validated as object but failed schema parsing: %s", exc
            )
        else:
            _drop_ai_supplied_tracker_matches(parsed)
            classification = _usable_classification(parsed.classification)
            if classification:
                return parsed.model_copy(update={"classification": classification})
            # The structured answer is the model's answer. Salvaging a string it
            # merely quoted (a log line, a previous run) would store a
            # diagnosis the model never gave.
            logger.warning(
                "AI JSON answer has no usable classification (%r); not an analysis",
                parsed.classification,
            )
            return None

    # Regex/markdown recovery for an answer that did not parse as JSON
    recovered = recover_from_details(AnalysisDetail(details=raw_text))
    classification = _usable_classification(recovered.classification)
    if classification:
        return recovered.model_copy(update={"classification": classification})
    return None


_USABLE_CLASSIFICATIONS = frozenset(PRIMARY_CLASSIFICATIONS)


class AnalysisIncompleteError(RuntimeError):
    """The AI answered, but without a usable classification.

    Distinct from a provider failure: the tests stay reviewable and the group
    can be re-analysed, but there is no diagnosis to store. It carries what the
    caller can still use: the group signature (so a peer call keeps its
    workspace file) and the raw narration (peer prompt).
    """

    def __init__(
        self, message: str, *, error_signature: str = "", raw_text: str = ""
    ) -> None:
        super().__init__(message)
        self.error_signature = error_signature
        self.raw_text = raw_text


def incomplete_reason(error: object) -> str:
    """``analysis_error`` for a failed group: our message, or ``""``.

    Only ``AnalysisIncompleteError`` is stored. Any other error is a provider
    failure whose text may echo a credential, and it must never reach a result.
    """
    return str(error) if isinstance(error, AnalysisIncompleteError) else ""


def _usable_classification(value: str) -> str:
    """Return the canonical taxonomy classification, or ``""`` if unusable."""
    normalized = " ".join((value or "").split()).upper()
    return normalized if normalized in _USABLE_CLASSIFICATIONS else ""


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
        logger.warning("Cannot recover a classification: the AI answer is empty")
        return result

    # Try markdown classification recovery (e.g. "**Classification: INFRASTRUCTURE**")
    if '"classification"' not in details:
        md_match = re.search(r"\*\*Classification:\s*([A-Z][A-Z _]+?)\*\*", details)
        if not md_match:
            logger.warning(
                "Cannot recover a classification from the AI answer: no JSON "
                "field and no '**Classification: X**' marker in %d chars of text",
                len(details),
            )
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
        logger.warning(
            "Cannot recover a classification from the AI answer: the JSON "
            "'classification' field is missing or truncated"
        )
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


def _extract_json_by_braces(text: str) -> dict[str, Any] | None:
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
    except json.JSONDecodeError as e:
        logger.debug("JSON parse strategy 2 (brace matching) failed: %s", e)
    return None


def _extract_json_from_code_blocks(text: str) -> dict[str, Any] | None:
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
        except json.JSONDecodeError as e:
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
            for _path in additional_repos.values():
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
                        f"{names_str} — read their files in .pi/agents/ for analysis guidance"
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
    The header names every hash the group is filed under, so an AI that reads
    only this file still searches the whole group's history.
    """
    representative = failures[0]
    test_names = [f.test_name for f in failures]
    content = (
        f"{build_signature_section(failures, error_signature)}"
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


# Auth and session faults do not resolve themselves; a second attempt only
# repeats the failure. A redacted user-key failure arrives as "AI call failed",
# which is deliberately absent here so a recoverable failure stays retryable.
# Both spellings of the invalid-key marker are listed because the sidecar and the
# credential-verification path use different ones.
_NON_RETRYABLE_AI_ERRORS = (
    "session not found",
    "authentication required",
    "not authenticated",
    "not logged in",
    "invalid api key",
    "invalid_api_key",
    # A session refusal is a second doomed POST /sessions, not a transient
    # blip: the retry decision lowercases the detail before matching.
    SESSION_REFUSAL_MARKER.casefold(),
)


#: Leading hex characters of the answer's SHA-256 kept in the WARNING line. The
#: width is part of the log format: same answer, same fingerprint, every run.
#: The answer itself is never written at WARNING — it reaches DEBUG scrubbed and
#: whole (see ``log_scrubbed_answer``): model narration can quote CI logs,
#: artifact contents or tokens, and this engine runs on server credentials.
AI_LOG_DIGEST_CHARS = 12


def log_scrubbed_answer(answer: str, *, why: str) -> None:
    """Write the *whole* scrubbed answer at DEBUG, the one record of model output.

    WARNING carries only a length and a fingerprint, so this is what answers
    "what did the model actually say?" for a job that failed at the default INFO
    log level. Nothing is truncated — a cut answer hides the part that explains
    the failure — and nothing is raw: the engine runs on server credentials, so
    the text is scrubbed before it is logged. Guarded because the call is eager.
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("%s (scrubbed): %s", why, scrub_sensitive_data(answer))


async def _call_ai_with_retry(
    prompt: str,
    *,
    ai_provider: str,
    ai_model: str,
    workspace_dir: Path,
    ai_call_timeout: int | None,
    server_url: str,
    job_id: str,
    auth_header: str,
    call_type: str = "primary",
    system_prompt: str = "",
    repos: dict[str, Path] | None = None,
    require_usable_analysis: bool = False,
) -> AIResult:
    """Call AI with retry on empty response and record usage.

    Builds call kwargs, retries once on empty text, and records token
    usage for each attempt. Used by both per-group and orchestrated
    analysis paths.

    Args:
        prompt: The prompt to send.
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        workspace_dir: Working directory for the sidecar session.
        ai_call_timeout: Timeout in minutes.
        server_url: Base URL for history API access.
        job_id: Current job ID.
        auth_header: Bearer token for history tools.
        call_type: Token usage call type label.
        system_prompt: Optional system prompt (e.g. agent file body).
        require_usable_analysis: Treat an answer without a taxonomy
            classification exactly like an empty one -- retry it.

    Returns:
        The AIResult from the AI call.
    """
    tools = await build_session_tools(
        builtin=list(ANALYSIS_BUILTIN_TOOLS),
        custom_base=analysis_http_tools(
            server_url=server_url,
            job_id=job_id,
            auth_header=auth_header,
        ),
        workspace=workspace_dir,
        job_id=job_id,
        repos=repos,
    )
    custom_tools = tools.custom
    if custom_tools:
        # A tool nobody advertises is a tool nobody calls: the first live run
        # had all six graph tools and made zero queries.
        from rootcoz.engine.session_tools import graph_prompt_hint

        prompt += graph_prompt_hint(tools)

    call_kwargs: dict[str, Any] = {
        "ai_provider": ai_provider,
        "ai_model": ai_model,
        "cwd": str(workspace_dir),
        "ai_call_timeout": ai_call_timeout,
        "tools": list(ANALYSIS_BUILTIN_TOOLS),
    }
    if system_prompt:
        call_kwargs["system_prompt"] = system_prompt
    if custom_tools:
        call_kwargs["custom_tools"] = custom_tools

    max_attempts = 2
    result = AIResult(success=False, text="AI call failed unexpectedly")
    for attempt in range(1, max_attempts + 1):
        try:
            result = await call_ai_once(prompt, **call_kwargs)
        except (ValueError, RuntimeError, OSError, TimeoutError, TypeError) as exc:
            from rootcoz.engine.chat import safe_exception_frames

            logger.error(
                "AI call raised %s: provider=%s, model=%s, attempt=%d\n%s",
                type(exc).__name__,
                ai_provider,
                ai_model,
                attempt,
                safe_exception_frames(exc),
            )
            result = AIResult(success=False, text="AI call failed unexpectedly")

        logger.info(
            "AI call result: success=%s, text_length=%d, provider=%s, model=%s, "
            "call_type=%s, attempt=%d",
            result.success,
            len(result.text),
            ai_provider,
            ai_model,
            call_type,
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
            call_type=call_type,
            prompt_chars=len(prompt),
            ai_provider=ai_provider,
            ai_model=ai_model,
        )

        if not result.success:
            # A provider stream can end without a terminal event after the model
            # has already emitted a full analysis, discarding it as a failure.
            # That is transport-level, and a second attempt usually completes.
            # Only the transport error decides recoverability: result.text is
            # model-authored analysis and may quote a phrase that reads like an
            # auth failure without the call being one.
            detail = (result.error or "").lower()
            recoverable = not any(
                marker in detail for marker in _NON_RETRYABLE_AI_ERRORS
            )
            if not recoverable or attempt >= max_attempts:
                break
            # Never log the provider error text. On a server-credential call it
            # passes through unredacted and may echo a key, and this engine is
            # shared by the user-key path too.
            logger.warning(
                "AI call failed (attempt=%d/%d); retrying recoverable failure: "
                "provider=%s, model=%s, job_id=%s",
                attempt,
                max_attempts,
                ai_provider,
                ai_model,
                job_id,
            )
            continue

        if not _is_empty_ai_text(result):
            if (
                not require_usable_analysis
                or parse_json_response(result.text) is not None
            ):
                break
            # Narration without a final answer is not an analysis: retry it.
            # The text itself stays out of WARNING (see log_scrubbed_answer).
            logger.warning(
                "AI answer has no usable classification (attempt=%d/%d) "
                "provider=%s, model=%s, call_type=%s, job_id=%s, "
                "text_length=%d, text_sha256=%s",
                attempt,
                max_attempts,
                ai_provider,
                ai_model,
                call_type,
                job_id,
                len(result.text or ""),
                hashlib.sha256((result.text or "").encode()).hexdigest()[
                    :AI_LOG_DIGEST_CHARS
                ],
            )
            log_scrubbed_answer(
                result.text or "", why="AI answer with no usable classification"
            )
            if attempt >= max_attempts:
                break
            continue

        logger.debug(
            "AI returned empty response (attempt=%d/%d), provider=%s, "
            "model=%s, call_type=%s, job_id=%s",
            attempt,
            max_attempts,
            ai_provider,
            ai_model,
            call_type,
            job_id,
        )
        if attempt >= max_attempts:
            break

    return result


async def run_single_ai_analysis(
    *,
    failures: list[FailedTest],
    console_context: str,
    repo_path: Path,
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
    system_prompt: str = "",
) -> tuple[AnalysisDetail, str]:
    """Run single-AI analysis on a failure group. Returns (parsed_analysis, error_signature).

    Shared by both single-AI and peer analysis paths. Builds the per-group
    prompt, calls the AI, and parses the response.

    If the AI returns success with empty text, retries once. A second empty
    response fails the group with a clear error in ``details`` (never a blank
    AnalysisDetail).

    Args:
        failures: List of test failures with the same error signature.
        console_context: Relevant console lines for context.
        repo_path: Path to the analysis workspace (all repos are subdirectories).
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        ai_call_timeout: Timeout in minutes for the AI call.
        custom_prompt: Additional user instructions.
        artifacts_context: Build artifacts context.
        server_url: Base URL of this server for AI history API access.
        job_id: Current job ID to exclude from history queries.
        all_groups: All failure groups keyed by error signature. When provided,
            cross-reference data is written to a workspace file for the AI to read.
        system_prompt: Optional system prompt (e.g. test-analyzer agent body).

    Returns:
        Tuple of (parsed AnalysisDetail, error_signature string).
    """
    representative = failures[0]
    error_signature = get_failure_signature(representative)
    # The mandatory history search matches one hash at a time, so the prompt
    # gets the WHOLE group's hash set: every member's anchor (the value every
    # stored row carries, including rows written before the v2 rules existed)
    # plus the v2 group hash. Naming only failures[0]'s anchor would drop the
    # other members' history -- the v2 rules merged them precisely because their
    # anchors differ.
    anchor_signature = get_legacy_signature(representative)
    signature_section = build_signature_section(failures, error_signature)

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
    if console_context:
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

    # Write failure-details / cross-reference files into the analysis workspace
    workspace_dir = repo_path

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
{signature_section}
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
    # Both hashes: the anchor for legacy attribution and the v2 group hash
    # so every member of a v2 group -- whose own anchors differ from it --
    # resolves to the same usage summary (see attach_failure_usage).
    with failure_group_usage(job_id, anchor_signature, error_signature):
        result = await _call_ai_with_retry(
            prompt,
            ai_provider=ai_provider,
            ai_model=ai_model,
            workspace_dir=workspace_dir,
            repos=additional_repos,
            ai_call_timeout=ai_call_timeout,
            server_url=server_url,
            job_id=job_id,
            auth_header=auth_header,
            call_type="primary",
            system_prompt=system_prompt,
            require_usable_analysis=True,
        )

    if not result.success:
        # A refusal keeps its sentence all the way to the stored error: it names
        # the cause and the fix, unlike the generic provider-failure line.
        if _is_refusal_text(result.text or ""):
            raise RuntimeError(result.text)
        raise RuntimeError("AI call failed. Check provider credentials and try again.")
    if _is_empty_ai_text(result):
        raise RuntimeError("AI returned empty response after retry.")
    parsed = parse_json_response(result.text)
    if parsed is None:
        logger.error(
            "AI analysis incomplete: no usable classification for job=%s "
            "(text_length=%d); see the raw text logged by the retry loop",
            job_id,
            len(result.text),
        )
        raise AnalysisIncompleteError(
            "AI analysis incomplete: response has no usable classification; "
            "re-analyze the job",
            error_signature=error_signature,
            raw_text=result.text,
        )
    return parsed, error_signature


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
    peer_ai_configs: list[AiConfigEntry] | None = None,
    peer_analysis_max_rounds: int = 3,
    group_label: str = "",
    additional_repos: dict[str, Path] | None = None,
    max_concurrent_ai_calls: int = 3,
    auth_header: str = "",
    all_groups: dict[str, list[FailedTest]] | None = None,
    system_prompt: str = "",
) -> list[FailureAnalysis]:
    """Analyze a group of failures with the same error signature.

    Uses a single AI call for the group (or multi-AI peer consensus
    when peers are configured), then applies the analysis to all
    failures in the group.

    Args:
        failures: List of test failures with the same error signature.
        console_context: Relevant console lines for context.
        repo_path: Path to the analysis workspace (all repos are subdirectories).
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
        system_prompt: Optional system prompt (e.g. test-analyzer agent body).
            Passed on the non-peer path only.

    Returns:
        List of FailureAnalysis objects, one per failure in the group.
    """
    logger.debug(
        f"analyze_failure_group called with server_url='{server_url}', job_id='{job_id}'"
    )
    repo_path = require_workspace(repo_path)

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
            system_prompt=system_prompt,
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
        system_prompt=system_prompt,
    )

    # Apply the same analysis to all failures in the group.
    # All failures share the same v2 signature (that's how they were grouped),
    # so reuse the already-computed value instead of calling get_failure_signature()
    # again. The anchor is per failure -- two failures the v2 rules merge can
    # still differ under the frozen rules -- so each one stores its own.
    return _expand_group_to_analyses(error_signature, failures, parsed)


# Path to built-in agents shipped with rootcoz
ORCHESTRATOR_AGENTS_DIR = Path(__file__).parent.parent / "agents"


def copy_builtin_agents_to_workspace(workspace: Path) -> None:
    """Copy built-in rootcoz agents to workspace .pi/agents/ for sidecar discovery.

    Built-in agents (e.g. test-analyzer) ship in ``src/rootcoz/agents/`` and are
    copied to the workspace ``.pi/agents/`` directory alongside any user-provided
    agents from ``.rootcoz/agents/``. Existing files with the same name are NOT
    overwritten — user agents take precedence.

    Args:
        workspace: Root workspace directory.
    """
    if not ORCHESTRATOR_AGENTS_DIR.is_dir():
        logger.warning(
            "Built-in agents directory not found: %s", ORCHESTRATOR_AGENTS_DIR
        )
        return

    dest = workspace / ".pi" / "agents"
    dest.mkdir(parents=True, exist_ok=True)

    for agent_file in sorted(ORCHESTRATOR_AGENTS_DIR.glob("*.md")):
        target = dest / agent_file.name
        if target.exists():
            logger.debug(
                "Skipping built-in agent '%s' — user agent already exists at %s",
                agent_file.stem,
                target,
            )
            continue
        try:
            shutil.copy2(agent_file, target)
            logger.info("Copied built-in agent '%s' to %s", agent_file.stem, target)
        except OSError:
            logger.warning(
                "Failed to copy built-in agent '%s' to workspace; continuing",
                agent_file.stem,
                exc_info=True,
            )


def _strip_frontmatter(text: str) -> str:
    """Strip YAML frontmatter from agent markdown content.

    Returns the body after the closing ``---`` line marker, or the
    full text if no frontmatter is present.  Uses line-based
    delimiter matching so that ``---`` appearing *within* a line
    (e.g. inside content) is not mistaken for the closing marker.
    """
    lines = text.splitlines(True)  # keep line endings
    if not lines or lines[0].strip() != "---":
        return text
    # Search for the closing --- on its own line (allow surrounding whitespace)
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            # Return everything after the closing delimiter
            return "".join(lines[idx + 1 :]).lstrip("\n")
    # No closing delimiter found — return original text unchanged
    return text


def resolve_agent_prompt(workspace: Path | None) -> str:
    """Resolve the test-analyzer agent prompt for use as system_prompt.

    Priority:
    1. User override: ``<workspace>/.pi/agents/test-analyzer.md``
    2. Built-in: ``src/rootcoz/agents/test-analyzer.md``

    Returns the agent file body with frontmatter stripped.
    Returns empty string if no agent file is found.
    """
    # Check user override first
    if workspace:
        user_agent = workspace / ".pi" / "agents" / "test-analyzer.md"
        if user_agent.is_file():
            try:
                content = user_agent.read_text(encoding="utf-8")
                logger.info(
                    "Using user-provided test-analyzer agent from %s", user_agent
                )
                return _strip_frontmatter(content)
            except OSError, UnicodeDecodeError:
                logger.warning(
                    "Failed to read user agent %s; falling back to built-in",
                    user_agent,
                    exc_info=True,
                )

    # Fall back to built-in
    builtin = ORCHESTRATOR_AGENTS_DIR / "test-analyzer.md"
    if builtin.is_file():
        try:
            content = builtin.read_text(encoding="utf-8")
            return _strip_frontmatter(content)
        except OSError, UnicodeDecodeError:
            logger.warning("Failed to read built-in agent %s", builtin, exc_info=True)

    return ""


def discover_custom_agents(workspace: Path | None) -> list[str]:
    """Discover custom agent names in the workspace (excluding test-analyzer).

    Returns a list of agent names found in ``<workspace>/.pi/agents/``.
    The built-in ``test-analyzer`` is excluded since it's the base.
    """
    if not workspace:
        return []
    agents_dir = workspace / ".pi" / "agents"
    if not agents_dir.is_dir():
        return []

    names: list[str] = []
    for agent_file in sorted(agents_dir.glob("*.md")):
        name = _extract_agent_name(agent_file)
        if not name:
            if _SAFE_AGENT_NAME_RE.match(agent_file.stem):
                name = agent_file.stem
            else:
                continue
        if name == "test-analyzer":
            continue
        names.append(name)
    return names


def build_agent_routing_prompt(
    groups: dict[str, list[FailedTest]],
    agent_names: list[str],
    workspace_files: dict[str, dict[str, Path]],
) -> str:
    """Build a prompt asking the AI which agent to use for each failure group.

    Returns a prompt that expects a JSON response mapping each error
    signature to an agent name (or null for base-only analysis).
    """
    group_lines: list[str] = []
    for sig, failures in groups.items():
        paths = workspace_files.get(sig, {})
        failure_path = paths.get("failure_details", "")
        group_lines.append(
            f"- signature {sig}: {len(failures)} test(s), "
            f"failure details at {failure_path}"
        )

    groups_listing = "\n".join(group_lines)
    agents_listing = ", ".join(agent_names)

    return f"""You are a routing assistant. Given failure groups and available specialist agents, decide which agent (if any) should handle each group.

Available specialist agents: {agents_listing}
Read each agent's file in .pi/agents/ to understand what they specialize in.

Failure groups:
{groups_listing}

For each group, read the failure-details file to understand the error, then decide:
- If a specialist agent is relevant, assign it
- If no specialist fits, assign null (base analyzer will handle it)

Return ONLY a JSON object mapping each signature to an agent name or null:
{{
  "routing": {{
    "<signature>": "<agent-name>" or null
  }}
}}

No text before or after. No markdown code blocks. Just the JSON."""


def parse_agent_routing_response(
    raw_text: str,
    groups: dict[str, list[FailedTest]],
) -> dict[str, str | None]:
    """Parse the routing AI response into a signature-to-agent mapping.

    Returns a dict mapping each error signature to an agent name string
    or None (use base analyzer only). Unknown signatures are ignored;
    missing signatures default to None.
    """
    data = extract_json_dict(raw_text)
    if data is None:
        logger.warning(
            "Failed to parse agent routing response; using base agent for all groups"
        )
        return {sig: None for sig in groups}

    routing = data.get("routing", data)  # accept top-level or nested
    if not isinstance(routing, dict):
        logger.warning(
            "Agent routing response 'routing' is not a dict (got %s); "
            "using base agent for all groups",
            type(routing).__name__,
        )
        return {sig: None for sig in groups}
    result: dict[str, str | None] = {}
    for sig in groups:
        agent = routing.get(sig)
        if isinstance(agent, str) and agent.strip():
            result[sig] = agent.strip()
        else:
            result[sig] = None
    return result


def prepare_orchestrator_workspace(
    groups: dict[str, list[FailedTest]],
    console_context: str,
    workspace_dir: Path,
) -> dict[str, dict[str, Path]]:
    """Prepare workspace files for all failure groups before orchestrated analysis.

    Writes failure-details and cross-reference files for every group, plus a
    single shared console-output file. Returns a mapping of
    ``error_signature → {"failure_details": Path, "console_output": Path | None,
    "cross_ref": Path | None}``.

    Args:
        groups: Failure groups keyed by error signature.
        console_context: Shared console output (written once for all groups).
        workspace_dir: Root workspace directory.

    Returns:
        Dict mapping each error signature to its workspace file paths.
    """
    result: dict[str, dict[str, Path]] = {}

    # Write console output once (shared across all groups)
    console_file: Path | None = None
    if console_context:
        console_file = workspace_dir / "console-output.txt"
        console_file.write_text(console_context)

    for sig, failures in groups.items():
        paths: dict[str, Path] = {}
        paths["failure_details"] = write_failure_details_file(
            failures, sig, workspace_dir
        )
        if console_file:
            paths["console_output"] = console_file
        if len(groups) > 1:
            cross_ref = write_other_groups_file(groups, sig, workspace_dir)
            if cross_ref:
                paths["cross_ref"] = cross_ref
        result[sig] = paths

    return result


def _expand_group_to_analyses(
    sig: str,
    failures: list[FailedTest],
    analysis: AnalysisDetail,
    peer_debate: PeerDebate | None = None,
    analysis_error: str = "",
) -> list[FailureAnalysis]:
    """Create FailureAnalysis objects for all failures in a group.

    Dual-writes the group's v2 signature (``error_signature_v2``) and each
    failure's own frozen anchor (``error_signature``).

    The peer path passes *peer_debate* and comes back here too. Both write paths
    must stay in step -- a per-failure field added here and not there is a
    signature assigned in one analysis and missing from the other -- so this is
    the only mapping from a grouped failure to its stored record.

    *analysis_error* carries the reason a group has no diagnosis, so an
    unclassified row explains itself instead of looking like an empty success.
    """
    return [
        FailureAnalysis(
            test_name=f.test_name,
            error=f.error_message,
            stack_trace=f.stack_trace,
            analysis=analysis,
            analysis_error=analysis_error,
            error_signature=get_legacy_signature(f),
            error_signature_v2=sig,
            peer_debate=peer_debate,
        )
        for f in failures
    ]


def _build_cross_failure_prompt(
    group_results: list[tuple[str, AnalysisDetail]],
    workspace_dir: Path,
) -> str:
    """Build a prompt for cross-failure pattern detection.

    Writes group analysis results to a workspace file and returns a prompt
    pointing the AI to read it (AGENTS.md file-based data policy).

    Args:
        group_results: List of (error_signature, AnalysisDetail) tuples.
        workspace_dir: Workspace directory for writing the results file.

    Returns:
        Prompt string for the cross-failure detection call.
    """
    results_lines: list[str] = []
    for sig, analysis in group_results:
        results_lines.append(
            f"Group {sig}:\n"
            f"  Classification: {analysis.classification}\n"
            f"  Pattern: {analysis.pattern}\n"
            f"  Affected tests: {analysis.affected_tests}\n"
            f"  Details: {analysis.details}\n"
        )

    results_file = workspace_dir / "group-analyses.txt"
    results_file.write_text("\n".join(results_lines))

    return f"""You are analyzing the results of {len(group_results)} failure group analyses from a single CI job.
Your task is to detect CROSS-FAILURE PATTERNS \u2014 correlations across groups that individual analyses cannot see.

MANDATORY: Read the analysis results file at {results_file} before responding.
It contains the classification, pattern, affected tests, and details for each failure group.

Look for:
- Multiple groups sharing the same infrastructure issue
- Several failures caused by the same component
- Common threads (e.g., same timeout, same service down, same resource exhaustion)

Only report GENUINE patterns. Do NOT force patterns where none exist.
If all failures are independent, return an empty array.

Return ONLY a JSON object:
{{
  "cross_failure_patterns": [
    {{
      "pattern": "Description of the pattern",
      "affected_tests": ["test_a", "test_b"],
      "suggested_root_cause": "What ties these failures together"
    }}
  ]
}}

No text before or after. No markdown code blocks. Just the JSON.
"cross_failure_patterns" should be an empty array if no patterns are detected."""


def _parse_cross_failure_response(
    raw_text: str,
) -> list[CrossFailurePattern]:
    """Parse cross-failure pattern detection response.

    Args:
        raw_text: Raw AI response text.

    Returns:
        List of CrossFailurePattern objects.
    """
    data = extract_json_dict(raw_text)
    if data is None:
        logger.warning("Failed to parse cross-failure response as JSON")
        return []

    patterns: list[CrossFailurePattern] = []
    for p in data.get("cross_failure_patterns", []):
        if isinstance(p, dict):
            try:
                patterns.append(CrossFailurePattern(**p))
            except (ValueError, TypeError) as exc:
                logger.warning("Failed to parse cross-failure pattern: %s", exc)
    return patterns


def _failed_group_analyses(
    signature: str,
    failures: list[FailedTest],
    analysis_error: str = "",
    error_text: str | None = None,
) -> list[FailureAnalysis]:
    """Keep failed tests visible without exposing the provider error as analysis.

    ``analysis_error`` marks a group whose answer carried no usable analysis
    (e.g. narration only) so the row is reviewable and re-analysable.

    The only builder of a placeholder, and therefore the only writer of the
    typed ``placeholder`` flag ``models.is_failed_analysis`` reads (never the
    ``details`` text). ``error_text`` is stored only when it is one of our own
    refusal sentences: a raw provider/library text can carry a whole response
    body and this is a persisted report.
    """
    details = ANALYSIS_PLACEHOLDER
    if (
        error_text
        and _is_refusal_text(error_text)
        and error_text.lstrip().startswith(SESSION_REFUSAL_MARKER)
    ):
        details = error_text.strip()
    return [
        FailureAnalysis(
            test_name=f.test_name,
            error=f.error_message,
            stack_trace=f.stack_trace,
            error_signature=get_legacy_signature(f),
            error_signature_v2=signature,
            analysis_error=analysis_error,
            analysis=AnalysisDetail(details=details, placeholder=True),
        )
        for f in failures
    ]


async def run_orchestrated_analysis(
    *,
    groups: dict[str, list[FailedTest]],
    console_context: str,
    repo_path: Path,
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None = None,
    custom_prompt: str = "",
    artifacts_context: str = "",
    server_url: str = "",
    job_id: str = "",
    additional_repos: dict[str, Path] | None = None,
    auth_header: str = "",
    max_concurrent_ai_calls: int = 3,
    peer_ai_configs: list[Any] | None = None,
    peer_analysis_max_rounds: int = 3,
) -> tuple[list[FailureAnalysis], list[CrossFailurePattern]]:
    """Run Python-orchestrated analysis with agent prompts and cross-failure detection.

    For each failure group, calls ``analyze_failure_group`` (single-AI or peer
    debate) with the ``test-analyzer`` agent prompt as ``system_prompt`` on the
    non-peer path, plus full custom tool access (history API). After all groups
    complete, runs one more AI call to detect cross-failure patterns across
    all results.

    When custom agents exist in the workspace, a routing call assigns
    specialist agents to groups; the AI is pointed at the agent file to read.

    Failed groups produce per-test placeholders; successful groups remain intact.

    Args:
        groups: Failure groups keyed by error signature.
        console_context: Console output from the CI job.
        repo_path: Path to the analysis workspace (all repos are subdirectories).
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        ai_call_timeout: Timeout in minutes for the AI call.
        custom_prompt: Additional user instructions.
        artifacts_context: Build artifacts context string.
        server_url: Base URL for history API access.
        job_id: Current job ID.
        additional_repos: Extra cloned repositories.
        auth_header: Bearer token for history tools.
        max_concurrent_ai_calls: Max parallel per-group AI calls.
        peer_ai_configs: Optional peer AI configs for multi-AI debate.
        peer_analysis_max_rounds: Max peer debate rounds.

    Returns:
        Tuple of (list of FailureAnalysis, list of CrossFailurePattern).
    """
    # Every caller resolves a workspace before analysis; there is no
    # repo-less mode (see #443).
    workspace_dir = repo_path

    # Copy built-in agents to workspace
    copy_builtin_agents_to_workspace(workspace_dir)

    # Resolve agent system prompt
    system_prompt = resolve_agent_prompt(workspace_dir)

    # Discover custom agents and route if any exist
    custom_agents = discover_custom_agents(workspace_dir)
    agent_routing: dict[str, str | None] = {sig: None for sig in groups}

    if custom_agents:
        await safe_update_progress(job_id, "agent_routing")
        logger.info("Agent routing progress started for job=%s", job_id)
        # Prepare workspace files for routing call context
        workspace_files = prepare_orchestrator_workspace(
            groups, console_context, workspace_dir
        )
        routing_prompt = build_agent_routing_prompt(
            groups, custom_agents, workspace_files
        )
        logger.info(
            "Running agent routing call: %d groups, %d custom agents",
            len(groups),
            len(custom_agents),
        )
        routing_result = await _call_ai_with_retry(
            routing_prompt,
            ai_provider=ai_provider,
            ai_model=ai_model,
            workspace_dir=workspace_dir,
            repos=additional_repos,
            ai_call_timeout=ai_call_timeout,
            server_url="",  # routing doesn't need history
            job_id=job_id,
            auth_header="",
            call_type="agent_routing",
        )
        if routing_result.success and not _is_empty_ai_text(routing_result):
            agent_routing = parse_agent_routing_response(routing_result.text, groups)
            logger.info("Agent routing result: %s", agent_routing)
        else:
            logger.warning("Agent routing call failed; using base agent for all groups")

    # Run per-group analysis in parallel via analyze_failure_group
    # (single-AI or peer) with system_prompt from agent file
    logger.info(
        "Starting orchestrated analysis: %d groups, provider=%s, model=%s, job_id=%s",
        len(groups),
        ai_provider,
        ai_model,
        job_id,
    )

    async def _analyze_group(
        sig: str, failures: list[FailedTest], group_number: int
    ) -> list[FailureAnalysis]:
        """Analyze a single failure group with agent prompt."""
        await safe_update_progress(
            job_id, f"analyzing_failures (group {group_number}/{len(groups)})"
        )
        logger.info(
            "Analyzing failure group %d/%d for job=%s",
            group_number,
            len(groups),
            job_id,
        )
        # Point AI at specialist agent file (do not embed body)
        agent_appendix = ""
        routed_agent = agent_routing.get(sig)
        if routed_agent:
            agent_file = workspace_dir / ".pi" / "agents" / f"{routed_agent}.md"
            if agent_file.is_file():
                agent_appendix = (
                    f"\n\n=== SPECIALIST AGENT ({routed_agent}) ===\n"
                    f"A specialist agent has been assigned to this failure group.\n"
                    f"MANDATORY: Read the agent instructions at {agent_file} "
                    f"and follow them before analyzing.\n"
                )

        # Build custom_prompt with agent appendix
        effective_custom_prompt = custom_prompt
        if agent_appendix:
            effective_custom_prompt = (
                f"{custom_prompt}\n{agent_appendix}"
                if custom_prompt
                else agent_appendix
            )

        results = await analyze_failure_group(
            failures=failures,
            console_context=console_context,
            repo_path=repo_path,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=ai_call_timeout,
            custom_prompt=effective_custom_prompt,
            artifacts_context=artifacts_context,
            server_url=server_url,
            job_id=job_id,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=peer_analysis_max_rounds,
            additional_repos=additional_repos,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
            auth_header=auth_header,
            all_groups=groups if len(groups) > 1 else None,
            system_prompt=system_prompt,
        )
        return results

    # Run all groups in parallel
    coroutines = [
        _analyze_group(sig, failures, number)
        for number, (sig, failures) in enumerate(groups.items(), 1)
    ]
    results = await run_parallel_with_limit(
        coroutines, max_concurrency=max_concurrent_ai_calls
    )

    # Collect results
    all_analyses: list[FailureAnalysis] = []
    group_results: list[tuple[str, AnalysisDetail]] = []
    from rootcoz.engine.chat import safe_exception_frames

    for (sig, failures), result in zip(groups.items(), results):
        if isinstance(result, Exception):
            logger.error(
                "Failed to analyze group %s: %s\n%s",
                sig,
                type(result).__name__,
                safe_exception_frames(result),
            )
            # Only our own message: a provider error may echo a credential
            # and must never be persisted into the result.
            all_analyses.extend(
                _failed_group_analyses(
                    sig,
                    failures,
                    analysis_error=incomplete_reason(result),
                    error_text=str(result),
                )
            )
            continue
        all_analyses.extend(result)
        if not result:
            logger.warning("Analysis group %s returned no analyses", sig)
            all_analyses.extend(_failed_group_analyses(sig, failures))
        elif not result[0].analysis.classification:
            # Only the peer path returns rows with no classification (the
            # single-AI path raises and lands in the branch above). Such a
            # group has no diagnosis to correlate, so feeding it to
            # cross-failure only dilutes the other groups' patterns (#399).
            logger.info(
                "Analysis group %s has no classification; excluded from cross-failure",
                sig,
            )
        else:
            group_results.append((sig, result[0].analysis))

    if not group_results:
        logger.warning("No successful direct analysis groups for job=%s", job_id)

    # Cross-failure pattern detection (only when multiple successful groups)
    cross_patterns: list[CrossFailurePattern] = []
    if len(group_results) > 1:
        await safe_update_progress(job_id, "cross_failure")
        logger.info(
            "Running cross-failure pattern detection for %d groups",
            len(group_results),
        )
        cross_prompt = _build_cross_failure_prompt(group_results, workspace_dir)
        cross_result = await _call_ai_with_retry(
            cross_prompt,
            ai_provider=ai_provider,
            ai_model=ai_model,
            workspace_dir=workspace_dir,
            repos=additional_repos,
            ai_call_timeout=ai_call_timeout,
            server_url="",  # cross-failure doesn't need history
            job_id=job_id,
            auth_header="",
            call_type="cross_failure",
        )
        if cross_result.success and not _is_empty_ai_text(cross_result):
            cross_patterns = _parse_cross_failure_response(cross_result.text)
            logger.info("Detected %d cross-failure patterns", len(cross_patterns))
        else:
            logger.warning("Cross-failure pattern detection failed or returned empty")

    return all_analyses, cross_patterns
