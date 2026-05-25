"""Chat engine for interactive AI sessions about analyzed jobs.

Builds job-scoped system prompts and manages AI CLI conversations.
"""

import asyncio
import importlib.resources
import shlex
import shutil
import stat
from pathlib import Path

from simple_logger.logger import get_logger

from pi_sidecar_client import call_ai, get_sidecar_client

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
    additional_repos = [
        AdditionalRepo(**ar) if isinstance(ar, dict) else ar for ar in additional_repos
    ]

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

    # Copy raw Python scripts to a hidden dir (not directly accessible by AI)
    raw_scripts_dir = workspace / ".scripts"
    raw_scripts_dir.mkdir(exist_ok=True)

    scripts_pkg = importlib.resources.files("rootcoz.chat_scripts")
    for script_name in _CHAT_SCRIPTS:
        source = scripts_pkg.joinpath(script_name)
        target = raw_scripts_dir / script_name
        target.write_bytes(source.read_bytes())
        target.chmod(target.stat().st_mode | stat.S_IEXEC)

    # Write env file that scripts read.
    # Values are shell-quoted to prevent injection via newlines or special chars.
    def _env_line(key: str, value: str) -> str:
        return f"{key}={shlex.quote(value)}"

    env_lines = [
        _env_line("ROOTCOZ_SERVER_URL", server_url),
        _env_line("ROOTCOZ_AUTH_TOKEN", auth_token),
        _env_line("ROOTCOZ_JOB_ID", job_id),
    ]
    if jira_url:
        env_lines.append(_env_line("ROOTCOZ_JIRA_URL", jira_url))
    if jira_email:
        env_lines.append(_env_line("ROOTCOZ_JIRA_EMAIL", jira_email))
    if jira_token:
        env_lines.append(_env_line("ROOTCOZ_JIRA_TOKEN", jira_token))
    if github_token:
        env_lines.append(_env_line("ROOTCOZ_GITHUB_TOKEN", github_token))
    if github_repo:
        env_lines.append(_env_line("ROOTCOZ_GITHUB_REPO", github_repo))

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
    """Write a Python wrapper that loads env safely and runs the script via uv."""
    wrapper = scripts_dir / name
    env_file = workspace / ".chat_env"
    raw_script = workspace / ".scripts" / script_file
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        f"env_file = {str(env_file)!r}\n"
        "import shlex\n"
        "with open(env_file) as f:\n"
        "    for line in f:\n"
        "        line = line.strip()\n"
        "        if line and '=' in line and not line.startswith('#'):\n"
        "            k, _, v = line.partition('=')\n"
        "            # Strip shell quoting added by shlex.quote()\n"
        "            try:\n"
        "                v = shlex.split(v)[0] if v else v\n"
        "            except ValueError:\n"
        "                pass\n"
        "            os.environ[k] = v\n"
        f"sys.exit(subprocess.call(['uv', 'run', {str(raw_script)!r}] + sys.argv[1:]))\n"
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

    # Also delete sensitive files that are dot-prefixed but not session dirs
    for sensitive in (".chat_env", ".scripts"):
        target = workspace / sensitive
        if target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    logger.info(f"Cleaned up chat repos for job {job_id} (kept sessions)")


def cleanup_chat_workspace(job_id: str, username: str = "") -> None:
    """Delete the entire chat workspace including sessions.

    Explicitly removes sensitive files (.chat_env) first to ensure
    tokens are scrubbed even if the full rmtree partially fails.
    """
    workspace = get_chat_workspace(job_id, username)
    if workspace.exists():
        # Scrub sensitive token files first (defense in depth)
        env_file = workspace / ".chat_env"
        if env_file.is_file():
            env_file.unlink(missing_ok=True)
        scripts_dir = workspace / ".scripts"
        if scripts_dir.is_dir():
            shutil.rmtree(scripts_dir, ignore_errors=True)

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
    """Initialize a chat session via the sidecar.

    Creates a sidecar session with the system prompt. The sidecar
    session_id is the session identifier — no hidden AI call needed.

    Returns the session_id, or None if creation failed.
    """
    system_prompt = build_system_prompt(
        job_name=job_name,
        build_number=build_number,
        job_id=job_id,
        available_scripts=available_scripts or [],
        repos_available=repos_available,
    )

    logger.info(
        "Chat: creating session for job %s (provider=%s, model=%s)",
        job_id,
        ai_provider,
        ai_model,
    )

    try:
        client = get_sidecar_client()
        session_id = await client.create_session(
            provider=ai_provider,
            model=ai_model,
            system_prompt=system_prompt,
            cwd=str(repo_path) if repo_path else "/tmp",
        )
        logger.info("Chat: session created for job %s: %s", job_id, session_id)
        return session_id
    except Exception:
        logger.warning(
            "Chat: failed to create session for job %s", job_id, exc_info=True
        )
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
    ai_call_timeout: int | None = None,
    session_id: str | None = None,
    available_scripts: list[str] | None = None,
    repos_available: bool = False,
) -> tuple[bool, str, str | None]:
    """Send a chat message and get an AI response via the sidecar.

    Args:
        job_id: The analyzed job ID.
        job_name: The job name for display in the prompt.
        build_number: The build number for display in the prompt.
        message: The user's new message.
        history: Previous chat messages (list of dicts with role/content).
        ai_provider: AI provider to use.
        ai_model: AI model to use.
        repo_path: Path to cloned repos (if available).
        ai_call_timeout: Timeout for AI call.
        session_id: Sidecar session ID for conversation continuity.
        available_scripts: List of available script names.
        repos_available: Whether source repos are cloned.

    Returns:
        Tuple of (success, response_text, session_id).
    """
    logger.info(
        "Chat: %s session for job %s (provider=%s, model=%s)",
        "resuming" if session_id else "new",
        job_id,
        ai_provider,
        ai_model,
    )

    if session_id:
        # Continue existing sidecar session — just send the message
        prompt = message
    else:
        # First message without a session — build full prompt with history
        system_prompt = build_system_prompt(
            job_name=job_name,
            build_number=build_number,
            job_id=job_id,
            available_scripts=available_scripts or [],
            repos_available=repos_available,
        )
        prompt = build_chat_prompt(system_prompt, history, message)

    logger.info(
        "Chat message for job %s: %d chars, session_id=%s, prompt: %d chars",
        job_id,
        len(message),
        "yes" if session_id else "new",
        len(prompt),
    )

    result = await call_ai(
        prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        cwd=str(repo_path) if repo_path else None,
        ai_call_timeout=ai_call_timeout,
        session_id=session_id,
    )

    # Record usage
    await result.record_usage(
        job_id=job_id,
        call_type="chat",
        prompt_chars=len(prompt),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )

    if not result.success:
        logger.error("Chat AI call failed for job %s: %s", job_id, result.text)
        return False, result.text, None

    return True, result.text, result.session_id
