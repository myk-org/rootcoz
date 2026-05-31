"""Chat engine for interactive AI sessions about analyzed jobs.

Builds job-scoped system prompts and manages AI CLI conversations.
"""

import asyncio
import importlib.resources
import shlex
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from simple_logger.logger import get_logger

from pi_sidecar_client import call_ai, get_sidecar_client

logger = get_logger(name=__name__)

_CHAT_WORKSPACE_PREFIX = "rootcoz-chat-"

_CHAT_SCRIPTS = [
    "rootcoz_chat_job.py",
    "rootcoz_chat_jira.py",
    "rootcoz_chat_github.py",
    "rootcoz_chat_server.py",
]

# Maps wrapper name -> (script file, condition checker)
# condition checker receives the kwargs and returns True if the script should be available
_SCRIPT_WRAPPERS: dict[str, str] = {
    "rootcoz-chat-job": "rootcoz_chat_job.py",
    "rootcoz-chat-jira": "rootcoz_chat_jira.py",
    "rootcoz-chat-github": "rootcoz_chat_github.py",
    "rootcoz-chat-server": "rootcoz_chat_server.py",
}


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
    scripts: list[str] | None = None,
) -> list[str]:
    """Copy chat scripts into workspace and write env config.

    Args:
        scripts: If provided, install only these wrapper scripts (by wrapper name,
                 e.g. ["rootcoz-chat-server"]). If None, use the default
                 job-scoped script selection (job + optional jira/github).

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

    if scripts is not None:
        # Explicit script list — install exactly what was requested
        for wrapper_name in scripts:
            script_file = _SCRIPT_WRAPPERS.get(wrapper_name)
            if script_file:
                _write_wrapper(scripts_dir, wrapper_name, script_file, workspace)
                available.append(wrapper_name)
    else:
        # Default job-scoped selection
        _write_wrapper(
            scripts_dir, "rootcoz-chat-job", "rootcoz_chat_job.py", workspace
        )
        available.append("rootcoz-chat-job")

        if jira_url and jira_token:
            _write_wrapper(
                scripts_dir, "rootcoz-chat-jira", "rootcoz_chat_jira.py", workspace
            )
            available.append("rootcoz-chat-jira")

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


# -- Shared prompt building helpers --

_SCRIPT_DESCRIPTIONS: dict[str, str] = {
    "rootcoz-chat-job": "Query job data (failures, analyses, comments, history)",
    "rootcoz-chat-jira": "Search Jira issues, get issue details, find related tickets",
    "rootcoz-chat-github": "Search GitHub issues/PRs, get details",
    "rootcoz-chat-server": "Query server-wide data: list jobs, failure stats, user activity, test history, search failures, server settings",
}


def _build_tools_section(available_scripts: list[str]) -> str:
    """Build the tools section listing available scripts."""
    lines = []
    for script in available_scripts:
        desc = _SCRIPT_DESCRIPTIONS.get(script, "Run for details")
        lines.append(
            f"- `./bin/{script}` — {desc}. "
            f"Run `./bin/{script} --help` for all commands."
        )
    return "\n".join(lines)


def _build_unavailable_section(available_scripts: list[str]) -> str:
    """Build notice about unavailable tools (Jira/GitHub not configured)."""
    jira_configured = "rootcoz-chat-jira" in available_scripts
    github_configured = "rootcoz-chat-github" in available_scripts

    lines = []
    if not jira_configured:
        lines.append(
            "- **Jira search** is not available. If the user asks about Jira tickets, "
            'tell them: "Jira search is not available for your account. To enable it, '
            "go to your User Settings page and configure your Jira credentials "
            '(URL, email, and API token), then start a new chat session."'
        )
    if not github_configured:
        lines.append(
            "- **GitHub search** is not available. If the user asks about GitHub issues or PRs, "
            'tell them: "GitHub search is not available. To enable it, '
            "configure your GitHub token on the User Settings page, "
            'then start a new chat session."'
        )

    if not lines:
        return ""
    return "\n\n## Unavailable Tools\n" + "\n".join(lines)


_COMMON_RULES = """- Do NOT modify any data — read-only access only
- Do NOT run arbitrary curl commands — use the provided scripts"""


def build_system_prompt(
    job_name: str,
    build_number: int,
    job_id: str,
    available_scripts: list[str],
    repos_available: bool = False,
) -> str:
    """Build a system prompt that scopes the AI to a specific analyzed job."""
    tools_section = _build_tools_section(available_scripts)
    unavailable_section = _build_unavailable_section(available_scripts)

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

**IMPORTANT:** Use these scripts to get data when the user asks a question. Do NOT run scripts proactively — only fetch data that's relevant to what the user is asking about.{repos_note}{unavailable_section}

## Rules — STRICT
- You MUST only discuss this specific job and its failures
- If the user asks something unrelated to this job (e.g., "what's the time?", general coding questions, weather, anything not about this analysis), respond ONLY with: "I can only discuss the analysis results for this job. How can I help you understand the failures?"
- Do NOT answer off-topic questions. Do NOT be helpful about non-job topics. Reject them immediately.
{_COMMON_RULES}
"""


def build_admin_system_prompt(
    available_scripts: list[str],
) -> str:
    """Build system prompt for admin global chat — server-wide scope."""
    tools_section = _build_tools_section(available_scripts)
    unavailable_section = _build_unavailable_section(available_scripts)

    return f"""You are a CI/CD analytics expert with access to the entire rootcoz server data.

## Your Role
- Answer questions about test failure trends across all jobs
- Provide cross-job analytics (failure counts, classification distributions)
- Show user activity and review statistics
- Help identify recurring failures and patterns
- Query server configuration and settings

## Scope — STRICT
- You provide server-wide analytics: failure counts, trends, user activity, job statistics
- You do NOT deep-dive into individual test failures, root causes, or classifications
- If the user asks about a specific failure's details (e.g., "why did test X fail?", "what's the root cause of..."), respond ONLY with:
  "For detailed failure analysis, use the job-specific chat at /chat/{{job_id}}. I can help you find the job ID if you describe the test or job name."
- You CAN answer aggregate questions about failures (e.g., "how many infrastructure failures in the last week", "which tests fail most often")

## Available Tools
Scripts in your working directory (under `bin/`) — use these to access data:
{tools_section}

**IMPORTANT:** Use these scripts to get data when the user asks a question. Do NOT run scripts proactively — only fetch data that's relevant to what the user is asking about.{unavailable_section}

## Rules — STRICT
- You MUST only discuss server data and CI/CD analytics
- If the user asks something unrelated (e.g., general coding, weather), respond ONLY with: "I can only discuss server analytics and CI/CD data. How can I help you with failure analysis?"
- Do NOT answer off-topic questions. Reject them immediately.
{_COMMON_RULES}
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


async def _create_chat_session(
    *,
    system_prompt: str,
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    log_prefix: str = "Chat",
) -> str | None:
    """Create a sidecar session with the given system prompt. Returns session_id or None."""
    logger.info(
        "%s: creating session (provider=%s, model=%s)",
        log_prefix,
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
        logger.info("%s: session created: %s", log_prefix, session_id)
        return session_id
    except Exception:
        logger.warning("%s: failed to create session", log_prefix, exc_info=True)
        return None


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
    """Initialize a chat session via the sidecar. Returns session_id or None."""
    system_prompt = build_system_prompt(
        job_name=job_name,
        build_number=build_number,
        job_id=job_id,
        available_scripts=available_scripts or [],
        repos_available=repos_available,
    )
    return await _create_chat_session(
        system_prompt=system_prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        repo_path=repo_path,
        log_prefix=f"Chat(job={job_id})",
    )


async def init_admin_chat_session(
    *,
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    available_scripts: list[str] | None = None,
) -> str | None:
    """Initialize an admin chat session via the sidecar. Returns session_id or None."""
    system_prompt = build_admin_system_prompt(available_scripts=available_scripts or [])
    return await _create_chat_session(
        system_prompt=system_prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        repo_path=repo_path,
        log_prefix="Admin chat",
    )


async def _chat_with_ai_impl(
    *,
    message: str,
    history: list[dict],
    ai_provider: str,
    ai_model: str,
    build_prompt_fn: Callable[[], str],
    repo_path: Path | None = None,
    ai_call_timeout: int | None = None,
    session_id: str | None = None,
    log_prefix: str = "Chat",
    request_id: str = "",
    call_type: str = "chat",
) -> tuple[bool, str, str | None]:
    """Shared AI chat implementation used by both job and admin chat."""
    logger.info(
        "%s: %s session (provider=%s, model=%s)",
        log_prefix,
        "resuming" if session_id else "new",
        ai_provider,
        ai_model,
    )

    if session_id:
        prompt = message
    else:
        system_prompt = build_prompt_fn()
        prompt = build_chat_prompt(system_prompt, history, message)

    logger.info(
        "%s: %d chars, session=%s, prompt: %d chars",
        log_prefix,
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

    # If session was lost, retry with fresh session
    if not result.success and session_id and "not found" in result.text.lower():
        logger.warning("%s: session lost, rebuilding with full context", log_prefix)
        await result.record_usage(
            request_id=request_id,
            call_type=call_type,
            prompt_chars=len(prompt),
            ai_provider=ai_provider,
            ai_model=ai_model,
        )
        system_prompt = build_prompt_fn()
        prompt = build_chat_prompt(system_prompt, history, message)
        result = await call_ai(
            prompt,
            ai_provider=ai_provider,
            ai_model=ai_model,
            cwd=str(repo_path) if repo_path else None,
            ai_call_timeout=ai_call_timeout,
            session_id=None,
        )

    await result.record_usage(
        request_id=request_id,
        call_type=call_type,
        prompt_chars=len(prompt),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )

    if not result.success:
        logger.error("%s: AI call failed: %s", log_prefix, result.text)
        return False, result.text, None

    return True, result.text, result.session_id


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
    """Send a chat message and get an AI response via the sidecar."""

    def _build_prompt() -> str:
        return build_system_prompt(
            job_name=job_name,
            build_number=build_number,
            job_id=job_id,
            available_scripts=available_scripts or [],
            repos_available=repos_available,
        )

    return await _chat_with_ai_impl(
        message=message,
        history=history,
        ai_provider=ai_provider,
        ai_model=ai_model,
        build_prompt_fn=_build_prompt,
        repo_path=repo_path,
        ai_call_timeout=ai_call_timeout,
        session_id=session_id,
        log_prefix=f"Chat(job={job_id})",
        request_id=job_id,
        call_type="chat",
    )


async def admin_chat_with_ai(
    *,
    message: str,
    history: list[dict],
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    ai_call_timeout: int | None = None,
    session_id: str | None = None,
    available_scripts: list[str] | None = None,
) -> tuple[bool, str, str | None]:
    """Send an admin chat message and get an AI response."""

    def _build_prompt() -> str:
        return build_admin_system_prompt(available_scripts=available_scripts or [])

    return await _chat_with_ai_impl(
        message=message,
        history=history,
        ai_provider=ai_provider,
        ai_model=ai_model,
        build_prompt_fn=_build_prompt,
        repo_path=repo_path,
        ai_call_timeout=ai_call_timeout,
        session_id=session_id,
        log_prefix="Admin chat",
        request_id="__admin_chat__",
        call_type="admin_chat",
    )
