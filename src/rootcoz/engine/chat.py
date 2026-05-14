"""Chat engine for interactive AI sessions about analyzed jobs.

Builds job-scoped system prompts and manages AI CLI conversations.
"""

import asyncio
import importlib.resources
import shutil
import stat
from pathlib import Path

from simple_logger.logger import get_logger

from rootcoz.engine.core import (
    PROVIDER_CLI_FLAGS,
    call_ai_and_record,
)

logger = get_logger(name=__name__)

_CHAT_WORKSPACE_PREFIX = "rootcoz-chat-"

_CHAT_SCRIPTS = [
    "rootcoz_chat_job.py",
    "rootcoz_chat_jira.py",
    "rootcoz_chat_github.py",
]


def get_chat_workspace(job_id: str, username: str = "") -> Path:
    """Get the chat workspace path for a job and user."""
    # Sanitize job_id to prevent path traversal
    safe_id = job_id.replace("/", "_").replace("..", "_").replace("\\", "_")
    safe_user = (
        username.replace("/", "_").replace("..", "_").replace("\\", "_")
        if username
        else ""
    )
    if safe_user:
        workspace = Path(f"/tmp/{_CHAT_WORKSPACE_PREFIX}{safe_id}/{safe_user}")
    else:
        workspace = Path(f"/tmp/{_CHAT_WORKSPACE_PREFIX}{safe_id}")
    # Verify the resolved path is still under /tmp/
    resolved = workspace.resolve()
    tmp_resolved = Path("/tmp").resolve()
    if not resolved.is_relative_to(tmp_resolved):
        raise ValueError(f"Invalid job_id/username for workspace: {job_id}/{username}")
    return workspace


def ensure_chat_workspace(job_id: str, username: str = "") -> Path:
    """Create the chat workspace directory if it doesn't exist."""
    workspace = get_chat_workspace(job_id, username)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o700)
    logger.info("Chat workspace created: %s", workspace)
    return workspace


def _resolve_chat_repo_target(workspace: Path, repo_name: str) -> tuple[str, Path]:
    """Sanitize and validate a repo name for the chat workspace.

    Returns (safe_name, target_path). Raises ValueError if the name is
    invalid (empty, dot-prefixed, or escapes the workspace).
    """
    safe_name = repo_name.replace("/", "_").replace("..", "_").replace("\\", "_")
    if not safe_name or safe_name.startswith("."):
        raise ValueError(f"Invalid repo name for chat workspace: {repo_name}")
    target = (workspace / safe_name).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise ValueError(f"Repo target escapes chat workspace: {repo_name}")
    return safe_name, target


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
                try:
                    repo_name, target = _resolve_chat_repo_target(workspace, raw_name)
                except ValueError:
                    logger.warning("Skipping test repo with unsafe name: %s", raw_name)
                else:
                    if target.exists():
                        logger.debug(
                            "Chat: repo %s already cloned in %s", repo_name, workspace
                        )
                        cloned_any = True
                    else:
                        logger.info(
                            "Chat: cloning repo %s into %s", repo_name, workspace
                        )
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
                    try:
                        safe_name, target = _resolve_chat_repo_target(
                            workspace, repo.name
                        )
                    except ValueError:
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


def setup_chat_scripts(
    workspace: Path,
    *,
    server_url: str,
    auth_token: str,
    job_id: str,
    jira_url: str = "",
    jira_email: str = "",
    jira_token: str = "",
    github_token: str = "",
    github_repo: str = "",
) -> list[str]:
    """Copy chat scripts into workspace and write env config.

    Returns list of available script names (only scripts whose required
    env vars are configured).
    """
    scripts_dir = workspace / "bin"
    scripts_dir.mkdir(exist_ok=True)

    # Copy scripts from package
    scripts_pkg = importlib.resources.files("rootcoz.chat_scripts")
    for script_name in _CHAT_SCRIPTS:
        source = scripts_pkg.joinpath(script_name)
        target = scripts_dir / script_name
        target.write_bytes(source.read_bytes())
        target.chmod(target.stat().st_mode | stat.S_IEXEC)

    # Write env file that scripts read
    env_lines = [
        f"ROOTCOZ_SERVER_URL={server_url}",
        f"ROOTCOZ_AUTH_TOKEN={auth_token}",
        f"ROOTCOZ_JOB_ID={job_id}",
    ]
    if jira_url:
        env_lines.append(f"ROOTCOZ_JIRA_URL={jira_url}")
    if jira_email:
        env_lines.append(f"ROOTCOZ_JIRA_EMAIL={jira_email}")
    if jira_token:
        env_lines.append(f"ROOTCOZ_JIRA_TOKEN={jira_token}")
    if github_token:
        env_lines.append(f"ROOTCOZ_GITHUB_TOKEN={github_token}")
    if github_repo:
        env_lines.append(f"ROOTCOZ_GITHUB_REPO={github_repo}")

    env_file = workspace / ".chat_env"
    env_file.write_text("\n".join(env_lines) + "\n")
    env_file.chmod(0o600)  # Restrict access — contains tokens

    # Write wrapper scripts that source the env and call uv run
    available = []

    # Job script is always available
    _write_wrapper(scripts_dir, "rootcoz-chat-job", "rootcoz_chat_job.py", workspace)
    available.append("rootcoz-chat-job")

    # Jira script only if configured
    if jira_url and jira_token:
        _write_wrapper(
            scripts_dir, "rootcoz-chat-jira", "rootcoz_chat_jira.py", workspace
        )
        available.append("rootcoz-chat-jira")

    # GitHub script only if configured
    if github_token and github_repo:
        _write_wrapper(
            scripts_dir, "rootcoz-chat-github", "rootcoz_chat_github.py", workspace
        )
        available.append("rootcoz-chat-github")

    logger.info("Chat scripts setup in %s: %s", workspace, ", ".join(available))
    return available


def _write_wrapper(
    scripts_dir: Path, name: str, script_file: str, workspace: Path
) -> None:
    """Write a shell wrapper that sources env and runs the script via uv."""
    wrapper = scripts_dir / name
    wrapper.write_text(
        f"#!/bin/bash\n"
        f"set -a\n"
        f"source {workspace}/.chat_env\n"
        f"set +a\n"
        f'exec uv run "{scripts_dir / script_file}" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)


def cleanup_chat_repos(job_id: str, username: str = "") -> None:
    """Delete cloned repos from chat workspace but keep session files."""
    workspace = get_chat_workspace(job_id, username)
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


def cleanup_chat_workspace(job_id: str, username: str = "") -> None:
    """Delete the entire chat workspace including sessions."""
    workspace = get_chat_workspace(job_id, username)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
        logger.info(f"Deleted chat workspace for job {job_id}")


def build_system_prompt(
    job_name: str,
    build_number: int,
    job_id: str,
    available_scripts: list[str],
    repos_available: bool = False,
) -> str:
    """Build a system prompt that scopes the AI to a specific analyzed job.

    The prompt only defines the AI's role and lists available tools.
    All data access happens through scripts — no static data in the prompt.
    """
    # Build tools section
    tools_lines = []
    for script in available_scripts:
        if script == "rootcoz-chat-job":
            tools_lines.append(
                f"- `./bin/{script}` — Query job data (failures, analyses, comments, history). "
                f"Run `./bin/{script} --help` for all commands."
            )
        elif script == "rootcoz-chat-jira":
            tools_lines.append(
                f"- `./bin/{script}` — Search Jira issues, get issue details, find related tickets. "
                f"Run `./bin/{script} --help` for all commands."
            )
        elif script == "rootcoz-chat-github":
            tools_lines.append(
                f"- `./bin/{script}` — Search GitHub issues/PRs, get details. "
                f"Run `./bin/{script} --help` for all commands."
            )
    tools_section = "\n".join(tools_lines)

    repos_note = ""
    if repos_available:
        repos_note = (
            "\n\nSource repositories are cloned in your working directory. "
            "You can explore test and product code directly."
        )

    return f"""You are a CI/CD failure analysis expert. You are helping a user understand the analysis results for job **{job_name} #{build_number}** (job ID: {job_id}).

## Your Role
- Help the user understand test failures, their root causes, and classifications
- Suggest fixes and identify patterns across failures
- Search for related Jira tickets or GitHub issues when asked
- Explore cloned source code to understand test logic when relevant
- Be concise and technical

## Available Tools
Scripts in your working directory (under `bin/`) — use these to access data:
{tools_section}

**IMPORTANT:** Use these scripts to get data when the user asks a question. Do NOT run scripts proactively — only fetch data that's relevant to what the user is asking about.{repos_note}

## Rules — STRICT
- You MUST only discuss this specific job and its failures
- If the user asks something unrelated to this job (e.g., "what's the time?", general coding questions, weather, anything not about this analysis), respond ONLY with: "I can only discuss the analysis results for this job. How can I help you understand the failures?"
- Do NOT answer off-topic questions. Do NOT be helpful about non-job topics. Reject them immediately.
- Do NOT modify any data — read-only access only
- Do NOT run arbitrary curl commands — use the provided scripts
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


async def init_chat_session(
    *,
    job_id: str,
    job_name: str,
    build_number: int,
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    available_scripts: list[str] | None = None,
    repos_available: bool = False,
) -> str | None:
    """Initialize a chat session with a hidden AI call to obtain a session_id.

    Sends the system prompt + a trivial message with output_format="json"
    to get the session_id. The user never sees this exchange.

    Returns the session_id, or None if the call failed.
    """
    system_prompt = build_system_prompt(
        job_name=job_name,
        build_number=build_number,
        job_id=job_id,
        available_scripts=available_scripts or [],
        repos_available=repos_available,
    )
    # Build init prompt — establish session with system context but NO tool execution
    init_prompt = (
        system_prompt
        + "\n\n**IMPORTANT OVERRIDE FOR THIS MESSAGE ONLY:** Do NOT run any scripts or tools right now. "
        "Do NOT analyze the job yet. Do NOT fetch any data. Simply acknowledge that you understand "
        "your role and are ready to help. Reply with only: "
        '"I\'m ready to help you understand the analysis for this job. What would you like to know?"'
        "\n\n**User:** hi\n\n**Assistant:** "
    )

    logger.info(
        "Chat: initializing session for job %s (provider=%s)", job_id, ai_provider
    )

    result, _ = await call_ai_and_record(
        init_prompt,
        job_id=job_id,
        call_type="chat_init",
        cwd=repo_path,
        ai_provider=ai_provider,
        ai_model=ai_model,
        cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
        output_format="json",  # Need JSON to extract session_id
    )

    if result.success and result.session_id:
        logger.info(
            "Chat: session initialized for job %s: %s", job_id, result.session_id
        )
        return result.session_id

    logger.warning("Chat: failed to initialize session for job %s", job_id)
    return None


async def chat_with_ai(
    *,
    job_id: str,
    job_name: str,
    build_number: int,
    message: str,
    history: list[dict],
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    ai_cli_timeout: int | None = None,
    session_id: str | None = None,
    available_scripts: list[str] | None = None,
    repos_available: bool = False,
) -> tuple[bool, str, str | None]:
    """Send a chat message and get an AI response.

    Args:
        job_id: The analyzed job ID.
        job_name: The job name for display in the prompt.
        build_number: The build number for display in the prompt.
        message: The user's new message.
        history: Previous chat messages (list of dicts with role/content).
        ai_provider: AI provider to use.
        ai_model: AI model to use.
        repo_path: Path to cloned repos (if available).
        ai_cli_timeout: Timeout for AI CLI call.
        session_id: Session ID for conversation continuity.
        available_scripts: List of available script names.
        repos_available: Whether source repos are cloned.

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
            job_name=job_name,
            build_number=build_number,
            job_id=job_id,
            available_scripts=available_scripts or [],
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
        output_format=None,  # Clean output, no chain-of-thought
    )

    if not result.success:
        logger.error(f"Chat AI call failed for job {job_id}: {result.text}")
        return False, result.text, None

    return True, result.text, result.session_id
