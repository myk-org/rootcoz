"""Chat engine for interactive AI sessions about analyzed jobs.

Builds job-scoped system prompts and manages AI CLI conversations.
"""

import asyncio
import shutil
from pathlib import Path

from simple_logger.logger import get_logger

from rootcoz.engine.core import (
    PROVIDER_CLI_FLAGS,
    call_ai_and_record,
)

logger = get_logger(name=__name__)

_CHAT_WORKSPACE_PREFIX = "rootcoz-chat-"


def get_chat_workspace(job_id: str) -> Path:
    """Get the chat workspace path for a job."""
    # Sanitize job_id to prevent path traversal
    safe_id = job_id.replace("/", "_").replace("..", "_").replace("\\", "_")
    workspace = Path(f"/tmp/{_CHAT_WORKSPACE_PREFIX}{safe_id}")
    # Verify the resolved path is still under /tmp/
    resolved = workspace.resolve()
    tmp_resolved = Path("/tmp").resolve()
    if not resolved.is_relative_to(tmp_resolved):
        raise ValueError(f"Invalid job_id for workspace: {job_id}")
    return workspace


def ensure_chat_workspace(job_id: str) -> Path:
    """Create the chat workspace directory if it doesn't exist."""
    workspace = get_chat_workspace(job_id)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o700)
    logger.info("Chat workspace created: %s", workspace)
    return workspace


async def clone_chat_repos(
    workspace: Path,
    request_params: dict,
) -> bool:
    """Clone repos into the chat workspace.

    Skips repos already present in the workspace.
    Returns True if any repos are available.

    Note:
        Repo tokens may be embedded in git remote URLs within the workspace.
        The workspace is in /tmp/ owned by the server process. Tokens are
        cleaned up when repos are deleted via cleanup_chat_repos().
    """
    from rootcoz.config import parse_repo_ref
    from rootcoz.repository import RepositoryManager, derive_test_repo_name
    from rootcoz.models import AdditionalRepo

    tests_repo_url = request_params.get("tests_repo_url", "")
    additional_repos = request_params.get("additional_repos") or []

    if not tests_repo_url and not additional_repos:
        return False

    repo_manager = RepositoryManager()
    cloned_any = False

    try:
        if tests_repo_url:
            try:
                clean_url, ref = parse_repo_ref(str(tests_repo_url))
                raw_name = derive_test_repo_name(clean_url, additional_repos)
                repo_name = (
                    raw_name.replace("/", "_").replace("..", "_").replace("\\", "_")
                )
                target = workspace / repo_name
                if not target.resolve().is_relative_to(workspace.resolve()):
                    logger.warning("Skipping test repo with unsafe name: %s", raw_name)
                elif target.exists():
                    logger.debug(
                        "Chat: repo %s already cloned in %s", repo_name, workspace
                    )
                    cloned_any = True
                else:
                    logger.info("Chat: cloning repo %s into %s", repo_name, workspace)
                    token = request_params.get("tests_repo_token", "")
                    await asyncio.to_thread(
                        repo_manager.clone_into,
                        clean_url,
                        target,
                        depth=50,
                        branch=ref,
                        token=token or None,
                    )
                    cloned_any = True
            except Exception:
                logger.warning("Failed to clone test repo for chat", exc_info=True)

        if additional_repos:
            repos = [
                AdditionalRepo(**r) if isinstance(r, dict) else r
                for r in additional_repos
            ]
            for repo in repos:
                try:
                    safe_name = (
                        repo.name.replace("/", "_")
                        .replace("..", "_")
                        .replace("\\", "_")
                    )
                    target = workspace / safe_name
                    if not target.resolve().is_relative_to(workspace.resolve()):
                        logger.warning("Skipping repo with unsafe name: %s", repo.name)
                        continue
                    if target.exists():
                        logger.debug(
                            "Chat: repo %s already cloned in %s", safe_name, workspace
                        )
                        cloned_any = True
                        continue
                    logger.info("Chat: cloning repo %s into %s", safe_name, workspace)
                    token = getattr(repo, "token", None) or ""
                    await asyncio.to_thread(
                        repo_manager.clone_into,
                        str(repo.url),
                        target,
                        depth=50,
                        branch=getattr(repo, "ref", "") or "",
                        token=token or None,
                    )
                    cloned_any = True
                except Exception:
                    logger.warning(
                        f"Failed to clone repo {repo.name} for chat", exc_info=True
                    )
    except Exception:
        logger.warning("Chat repo cloning failed", exc_info=True)

    return cloned_any


def cleanup_chat_repos(job_id: str) -> None:
    """Delete cloned repos from chat workspace but keep session files."""
    workspace = get_chat_workspace(job_id)
    if not workspace.exists():
        return

    # Delete everything except hidden dirs (which contain session data)
    for item in workspace.iterdir():
        if item.name.startswith("."):
            continue  # Keep .claude/, .cursor/ etc (session data)
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)

    logger.info(f"Cleaned up chat repos for job {job_id} (kept sessions)")


def cleanup_chat_workspace(job_id: str) -> None:
    """Delete the entire chat workspace including sessions."""
    workspace = get_chat_workspace(job_id)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
        logger.info(f"Deleted chat workspace for job {job_id}")


def _get_classification(failure: dict) -> str:
    """Safely extract classification from a failure dict."""
    analysis = failure.get("analysis")
    if isinstance(analysis, dict):
        return analysis.get("classification", "?")
    return "?"


def _collect_child_failures(children: list[dict], lines: list[str]) -> None:
    """Recursively collect failure lines from child job analyses."""
    for child in children:
        child_name = child.get("job_name", "?")
        child_num = child.get("build_number", 0)
        for f in child.get("failures", []):
            fid = f.get("id", "?")
            name = f.get("test_name", "unknown")
            classification = _get_classification(f)
            lines.append(
                f"  - [{child_name}#{child_num}] {name} (UUID: {fid}) \u2014 {classification}"
            )
        _collect_child_failures(child.get("failed_children", []), lines)


def build_system_prompt(
    result_data: dict,
    job_id: str,
    server_url: str,
    auth_header: str = "",
    jira_configured: bool = False,
    jira_url: str = "",
    jira_project_key: str = "",
    github_issues_enabled: bool = False,
    repos_available: bool = False,
) -> str:
    """Build a system prompt that scopes the AI to a specific analyzed job.

    The prompt provides:
    - Job metadata (name, build number, status, summary)
    - List of failures with test_name and UUID for reference
    - API endpoints the AI can use to query details
    - Instructions constraining the AI to read-only, job-scoped behavior
    """
    job_name = result_data.get("job_name", "unknown")
    build_number = result_data.get("build_number", 0)
    summary = result_data.get("summary", "")
    ai_provider = result_data.get("ai_provider", "")
    ai_model = result_data.get("ai_model", "")
    jenkins_url = result_data.get("jenkins_url", "")

    # Build failure reference list
    failure_lines = []
    for i, f in enumerate(result_data.get("failures", []), 1):
        fid = f.get("id", "?")
        name = f.get("test_name", "unknown")
        classification = _get_classification(f)
        failure_lines.append(f"  {i}. {name} (UUID: {fid}) — {classification}")

    # Include child job failures (recursive)
    _collect_child_failures(result_data.get("child_job_analyses", []), failure_lines)

    failures_section = "\n".join(failure_lines) if failure_lines else "  (no failures)"

    # API endpoints the AI can use
    api_section = ""
    if server_url:
        auth_flag = f" -H 'Authorization: {auth_header}'" if auth_header else ""
        api_section = f"""
## Available API Endpoints (read-only)
You can use curl to query these endpoints for more details:
- `curl{auth_flag} {server_url}/api/results/{job_id}` — Full job result with all failure analyses
- `curl{auth_flag} {server_url}/api/results/{job_id}/comments` — Get comments on this job
"""

    # Build integrations section
    integration_lines = []
    if jira_configured:
        integration_lines.append(
            f"Jira: Configured (URL: {jira_url}, Project: {jira_project_key}). "
            "Check failure's product_bug_report.jira_matches for existing tickets."
        )
    else:
        integration_lines.append(
            "Jira: Not configured. Tell user to configure Jira credentials in Settings."
        )
    if github_issues_enabled:
        integration_lines.append("GitHub Issues: Enabled.")
    else:
        integration_lines.append(
            "GitHub Issues: Not configured. Tell user to configure GitHub token in Settings."
        )
    if repos_available:
        integration_lines.append(
            "Source Repositories: Cloned in your working directory. Explore test/product code."
        )
    else:
        integration_lines.append("Source Repositories: Not available.")
    integrations_section = "\n".join(f"- {line}" for line in integration_lines)

    return f"""You are a read-only assistant helping a user understand a CI/CD failure analysis.

## Scope — STRICT RULES
- You MUST only discuss this specific analyzed job and its failures.
- If the user asks something unrelated to this job (e.g., "what's the time?", general coding questions, anything not about this analysis), respond ONLY with: "I can only discuss the analysis results for this job. How can I help you understand the failures?"
- Do NOT explore the filesystem, curl endpoints, or run any tools unless the user specifically asks about a failure or test in this job.
- Do NOT modify any data, create issues, or perform any mutations.
- Keep responses concise and focused. Do NOT dump entire analysis results unless explicitly asked.

## Job Context
- **Job:** {job_name} #{build_number}
- **Job ID:** {job_id}
- **Summary:** {summary}
- **AI Provider/Model:** {ai_provider} / {ai_model}
- **Jenkins URL:** {jenkins_url or "N/A"}

## Failures in this job
{failures_section}

When the user references a test, use the UUID to look up its details.
{api_section}
## Integrations
{integrations_section}

## Instructions
- Answer questions about the failures, their root causes, classifications, and suggested fixes
- If the user asks about a specific test, use the UUID to reference it
- You can explore the cloned repository (if available in your working directory) to understand test code
- Be concise and technical
- If you don't have enough information, say so and suggest what data would help
"""


def build_chat_prompt(
    system_prompt: str,
    history: list[dict],
    new_message: str,
) -> str:
    """Build a complete prompt from system prompt + conversation history + new message.

    Since AI CLI is stateless (no conversation mode), we build the full
    context each time by concatenating system prompt + history + new message.
    """
    parts = [system_prompt, "\n## Conversation History\n"]

    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            parts.append(f"**User:** {content}\n")
        else:
            parts.append(f"**Assistant:** {content}\n")

    parts.append(f"**User:** {new_message}\n")
    parts.append("\n**Assistant:** ")

    return "\n".join(parts)


async def chat_with_ai(
    *,
    job_id: str,
    result_data: dict,
    message: str,
    history: list[dict],
    ai_provider: str,
    ai_model: str,
    server_url: str = "",
    repo_path: Path | None = None,
    ai_cli_timeout: int | None = None,
    auth_header: str = "",
    session_id: str | None = None,
    jira_configured: bool = False,
    jira_url: str = "",
    jira_project_key: str = "",
    github_issues_enabled: bool = False,
    repos_available: bool = False,
) -> tuple[bool, str, str | None]:
    """Send a chat message and get an AI response.

    Args:
        job_id: The analyzed job ID.
        result_data: The job's result data (stripped of sensitive fields).
        message: The user's new message.
        history: Previous chat messages (list of dicts with role/content).
        ai_provider: AI provider to use.
        ai_model: AI model to use.
        server_url: Internal server URL for AI to query APIs.
        repo_path: Path to cloned repos (if available).
        ai_cli_timeout: Timeout for AI CLI call.
        auth_header: Bearer auth header for AI to use when curling API.
        session_id: Session ID for conversation continuity.

    Returns:
        Tuple of (success, response_text, session_id).
        session_id is returned from the AI CLI for session continuity.
    """
    logger.info(
        "Chat: %s session for job %s (provider=%s, model=%s)",
        "resuming" if session_id else "new",
        job_id,
        ai_provider,
        ai_model,
    )

    if session_id:
        # Continue existing session — just send the new message
        prompt = message
    else:
        # First message — build full system prompt
        system_prompt = build_system_prompt(
            result_data,
            job_id,
            server_url,
            auth_header=auth_header,
            jira_configured=jira_configured,
            jira_url=jira_url,
            jira_project_key=jira_project_key,
            github_issues_enabled=github_issues_enabled,
            repos_available=repos_available,
        )
        prompt = build_chat_prompt(system_prompt, history, message)

    logger.info(
        f"Chat message for job {job_id}: {len(message)} chars, "
        f"session_id={'yes' if session_id else 'new'}, prompt: {len(prompt)} chars"
    )

    result, _ = await call_ai_and_record(
        prompt,
        job_id=job_id,
        call_type="chat",
        cwd=repo_path,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
        session_id=session_id,
    )

    if not result.success:
        logger.error(f"Chat AI call failed for job {job_id}: {result.text}")
        return False, result.text, None

    return True, result.text, result.session_id
