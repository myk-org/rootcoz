"""Chat engine for interactive AI sessions about analyzed jobs.

Builds job-scoped system prompts and manages AI CLI conversations.
"""

import asyncio
import base64
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from simple_logger.logger import get_logger

from pi_sidecar_client import get_sidecar_client

from rootcoz.ai_client import (
    CHAT_BUILTIN_TOOLS,
    _prewarm_model_routes,
    call_ai,
    map_provider_model_for_sidecar,
    normalize_provider,
)
from rootcoz.storage import AI_SYSTEM_USERNAME

logger = get_logger(name=__name__)

_CHAT_WORKSPACE_PREFIX = "rootcoz-chat-"


def _tmp_root() -> Path:
    """Return the process temp root (honors ``TMPDIR``)."""
    return Path(tempfile.gettempdir()).resolve()


def get_chat_workspace(job_id: str, username: str = "") -> Path:
    """Get the chat workspace path for a job and user."""
    # Sanitize job_id to prevent path traversal
    safe_id = job_id.replace("/", "_").replace("..", "_").replace("\\", "_")
    safe_user = (
        username.replace("/", "_").replace("..", "_").replace("\\", "_")
        if username
        else ""
    )
    tmp = _tmp_root()
    if safe_user:
        workspace = tmp / f"{_CHAT_WORKSPACE_PREFIX}{safe_id}" / safe_user
    else:
        workspace = tmp / f"{_CHAT_WORKSPACE_PREFIX}{safe_id}"
    # Verify the resolved path is still under the process temp root
    resolved = workspace.resolve()
    if not resolved.is_relative_to(tmp):
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


def _is_github_url(url: str) -> bool:
    """Check if URL is a GitHub host (github.com or common GHE patterns)."""
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).netloc.lower()
        return host.endswith("github.com") or "github" in host
    except Exception:
        return False


async def clone_chat_repos(
    workspace: Path,
    request_params: dict,
    user_repo_token: str = "",
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
                        # Only use token for GitHub hosts (prevent credential leaks to other hosts)
                        token = ""
                        if user_repo_token and _is_github_url(clean_url):
                            token = user_repo_token
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

    # Copy .rootcoz/{agents,skills,extensions}/ to workspace .pi/
    if cloned_any:
        from rootcoz.engine.core import copy_rootcoz_pi_resources

        cloned_repos = {
            item.name: item
            for item in workspace.iterdir()
            if item.is_dir() and not item.name.startswith(".") and not item.is_symlink()
        }
        if cloned_repos:
            copy_rootcoz_pi_resources(cloned_repos, workspace)

    return cloned_any


def _bearer_headers(auth_token: str) -> dict[str, str]:
    """Authorization headers for sidecar HTTP custom tools."""
    return {"Authorization": f"Bearer {auth_token}"}


def _tool_get_failure_history(
    *,
    server_url: str,
    auth_headers: dict[str, str],
    description: str,
    query_params: dict[str, str],
    properties: dict[str, dict] | None = None,
    required: list[str] | None = None,
) -> dict:
    """Shared GET /history/test/{test_name} tool definition."""
    props: dict[str, dict] = {
        "test_name": {"type": "string", "description": "Full test name"},
    }
    if properties:
        props.update(properties)
    return {
        "name": "get_failure_history",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required or ["test_name"],
        },
        "http": {
            "method": "GET",
            "url": f"{server_url.rstrip('/')}/history/test/{{test_name}}",
            "headers": auth_headers,
            "query_params": query_params,
        },
    }


def _tool_get_classification_history(
    *,
    server_url: str,
    auth_headers: dict[str, str],
    description: str,
    query_params: dict[str, str],
    properties: dict[str, dict] | None = None,
) -> dict:
    """Shared GET /history/classifications tool definition."""
    props: dict[str, dict] = {
        "test_name": {"type": "string", "description": "Full test name"},
    }
    if properties:
        props.update(properties)
    return {
        "name": "get_classification_history",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": ["test_name"],
        },
        "http": {
            "method": "GET",
            "url": f"{server_url.rstrip('/')}/history/classifications",
            "headers": auth_headers,
            "query_params": query_params,
        },
    }


def build_analysis_history_tools(
    *,
    server_url: str,
    auth_token: str,
    job_id: str,
) -> list[dict]:
    """HTTP-backed history tools for analysis sessions.

    Auth stays in tool ``http.headers`` (sidecar executes the request).
    The AI never sees the token and has no bash/curl.
    Reuses shared history GET builders with chat; adds analysis-only tools.
    """
    server_url = server_url.rstrip("/")
    auth_headers = _bearer_headers(auth_token)
    exclude = {"exclude_job_id": job_id}

    return [
        _tool_get_failure_history(
            server_url=server_url,
            auth_headers=auth_headers,
            description=(
                "MANDATORY for every failed test before classifying. "
                "Pass/fail history: failure_rate, consecutive_failures, "
                "classifications, comments, recent_runs. "
                "Always pass job_name so passes/failure_rate are computed."
            ),
            query_params={**exclude, "job_name": "{job_name}"},
            properties={
                "job_name": {
                    "type": "string",
                    "description": "CI job name (required for pass/fail rates)",
                },
            },
            required=["test_name", "job_name"],
        ),
        {
            "name": "search_error_signature",
            "description": (
                "MANDATORY for every failure group. Find other tests/jobs "
                "sharing this error signature (infrastructure vs isolated)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signature": {
                        "type": "string",
                        "description": "Error signature hash for this failure group",
                    },
                },
                "required": ["signature"],
            },
            "http": {
                "method": "GET",
                "url": f"{server_url}/history/search",
                "headers": auth_headers,
                "query_params": {
                    "signature": "{signature}",
                    **exclude,
                },
            },
        },
        _tool_get_classification_history(
            server_url=server_url,
            auth_headers=auth_headers,
            description=(
                "MANDATORY for every failed test. Prior classifications and "
                f"created_by (user vs {AI_SYSTEM_USERNAME}). Never override "
                "user classifications."
            ),
            query_params={"test_name": "{test_name}"},
        ),
        {
            "name": "get_job_history_stats",
            "description": (
                "MANDATORY once per job. Overall job health stats for pattern context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": "Jenkins / CI job name",
                    },
                },
                "required": ["job_name"],
            },
            "http": {
                "method": "GET",
                "url": f"{server_url}/history/stats/{{job_name}}",
                "headers": auth_headers,
                "query_params": exclude,
            },
        },
        {
            "name": "classify_test_pattern",
            "description": (
                "MANDATORY for every analyzed test (unless a human user already "
                "classified it). Records pattern only (NEW/REGRESSION/FLAKY/"
                "INTERMITTENT/KNOWN_BUG/PERSISTENT). source is always ai."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_name": {"type": "string"},
                    "classification": {
                        "type": "string",
                        "description": (
                            "Pattern: NEW, REGRESSION, FLAKY, INTERMITTENT, "
                            "KNOWN_BUG, or PERSISTENT"
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Evidence-backed reason citing history data",
                    },
                    "job_name": {"type": "string"},
                    "references": {
                        "type": "string",
                        "description": (
                            "Jira keys / URLs / evidence ids, or empty string if none"
                        ),
                    },
                },
                "required": [
                    "test_name",
                    "classification",
                    "reason",
                    "job_name",
                    "references",
                ],
            },
            "http": {
                "method": "POST",
                "url": f"{server_url}/history/classify",
                "headers": {**auth_headers, "Content-Type": "application/json"},
                "body_template": {
                    "test_name": "{test_name}",
                    "classification": "{classification}",
                    "reason": "{reason}",
                    "job_name": "{job_name}",
                    "job_id": job_id,
                    "references": "{references}",
                    "source": "ai",
                },
            },
        },
    ]


def build_chat_custom_tools(
    *,
    server_url: str,
    auth_token: str,
    job_id: str,
    jira_url: str = "",
    jira_email: str = "",
    jira_token: str = "",
    github_token: str = "",
    github_repo: str = "",
) -> list[dict]:
    """Build HTTP-backed custom tools for a chat session.

    Returns tool definitions with 'http' configs that the sidecar
    executes directly — no bash, no scripts on disk.
    """
    tools: list[dict] = []
    server_url = server_url.rstrip("/")
    auth_headers = _bearer_headers(auth_token)

    tools.append(
        {
            "name": "get_job_result",
            "description": "Get the full analysis result for this job including all failures, classifications, and AI analysis",
            "parameters": {"type": "object", "properties": {}},
            "http": {
                "method": "GET",
                "url": f"{server_url}/results/{job_id}",
                "headers": auth_headers,
            },
        }
    )

    tools.append(
        {
            "name": "get_job_comments",
            "description": "Get user comments and discussion on this job",
            "parameters": {"type": "object", "properties": {}},
            "http": {
                "method": "GET",
                "url": f"{server_url}/results/{job_id}/comments",
                "headers": auth_headers,
            },
        }
    )

    tools.append(
        _tool_get_failure_history(
            server_url=server_url,
            auth_headers=auth_headers,
            description=(
                "Get pass/fail history for a specific test — total runs, "
                "failure rate, classifications, recent occurrences. "
                "Always pass job_name so passes/failure_rate are computed."
            ),
            query_params={"job_name": "{job_name}"},
            properties={
                "job_name": {
                    "type": "string",
                    "description": "CI job name (required for pass/fail rates)",
                },
            },
            required=["test_name", "job_name"],
        )
    )

    tools.append(
        _tool_get_classification_history(
            server_url=server_url,
            auth_headers=auth_headers,
            description=(
                "Get classification history for a test — who changed it, "
                "when, from what to what"
            ),
            # test_name is required; omit optional {job_id} placeholder.
            query_params={"test_name": "{test_name}"},
        )
    )

    if jira_url and jira_token:
        jira_url = jira_url.rstrip("/")
        jira_auth: dict[str, str] = {"Authorization": f"Bearer {jira_token}"}
        if jira_email:
            encoded = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
            jira_auth = {"Authorization": f"Basic {encoded}"}

        # Detect Jira Cloud vs Server — Cloud deprecated API v2
        is_cloud = "atlassian.net" in jira_url.lower()
        if is_cloud:
            search_url = f"{jira_url}/rest/api/3/search/jql"
            issue_url = f"{jira_url}/rest/api/3/issue/{{issue_key}}"
            search_params = {
                "jql": 'summary ~ "{query}" ORDER BY updated DESC',
                "maxResults": "{limit}",
            }
        else:
            search_url = f"{jira_url}/rest/api/2/search"
            issue_url = f"{jira_url}/rest/api/2/issue/{{issue_key}}"
            search_params = {
                "jql": 'summary ~ "{query}" ORDER BY updated DESC',
                "maxResults": "{limit}",
                "fields": "summary,status,assignee,created,updated",
            }

        tools.append(
            {
                "name": "search_jira",
                "description": "Search Jira for issues matching keywords.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search keywords"},
                        "limit": {
                            "type": "string",
                            "description": "Max results (default 10)",
                            "default": "10",
                        },
                    },
                    "required": ["query"],
                },
                "http": {
                    "method": "GET",
                    "url": search_url,
                    "headers": {**jira_auth, "Accept": "application/json"},
                    "query_params": search_params,
                },
            }
        )

        tools.append(
            {
                "name": "get_jira_issue",
                "description": "Get full details of a specific Jira issue by key (e.g., PROJ-123)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_key": {
                            "type": "string",
                            "description": "Jira issue key",
                        },
                    },
                    "required": ["issue_key"],
                },
                "http": {
                    "method": "GET",
                    "url": issue_url,
                    "headers": {**jira_auth, "Accept": "application/json"},
                },
            }
        )

    if github_token and github_repo:
        gh_headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
        }

        tools.append(
            {
                "name": "search_github_issues",
                "description": "Search GitHub issues and PRs in the test repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search keywords"},
                        "limit": {
                            "type": "string",
                            "description": "Max results (default 10)",
                            "default": "10",
                        },
                    },
                    "required": ["query"],
                },
                "http": {
                    "method": "GET",
                    "url": "https://api.github.com/search/issues",
                    "headers": gh_headers,
                    "query_params": {
                        "q": "{query} repo:" + github_repo,
                        "per_page": "{limit}",
                    },
                },
            }
        )

        tools.append(
            {
                "name": "get_github_issue",
                "description": "Get full details of a GitHub issue or PR by number",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "number": {
                            "type": "string",
                            "description": "Issue or PR number",
                        },
                    },
                    "required": ["number"],
                },
                "http": {
                    "method": "GET",
                    "url": f"https://api.github.com/repos/{github_repo}/issues/{{number}}",
                    "headers": gh_headers,
                },
            }
        )

    return tools


_REPORT_FILTER_PARAMS = {
    "team": {"type": "string", "description": "Filter by team"},
    "tier": {"type": "string", "description": "Filter by tier"},
    "version": {"type": "string", "description": "Filter by version"},
    "from": {"type": "string", "description": "From date (YYYY-MM-DD)"},
    "to": {"type": "string", "description": "To date (YYYY-MM-DD)"},
    "status": {
        "type": "string",
        "description": "Filter by status (comma-separated)",
    },
    "tags": {
        "type": "string",
        "description": "Filter by tags (comma-separated)",
    },
    "limit": {
        "type": "integer",
        "description": "Max detail rows to return (1-1000). Omit for all rows.",
        "minimum": 1,
        "maximum": 1000,
    },
    "offset": {
        "type": "integer",
        "description": "Skip N detail rows (>= 0)",
        "minimum": 0,
    },
}


def build_admin_custom_tools(
    *,
    server_url: str,
    auth_token: str,
) -> list[dict]:
    """Build HTTP-backed custom tools for admin chat."""
    auth_headers = {"Authorization": f"Bearer {auth_token}"}

    return [
        {
            "name": "db_schema",
            "description": (
                "Get the database schema — all tables with columns, types, "
                "and row counts. Run this first to understand the data structure."
            ),
            "parameters": {"type": "object", "properties": {}},
            "http": {
                "method": "GET",
                "url": f"{server_url}/api/admin/db/schema",
                "headers": auth_headers,
            },
        },
        {
            "name": "db_query",
            "description": (
                "Execute a read-only SQL query against the database. "
                "Use this to answer analytics questions. Write SELECT queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SQL SELECT query to execute",
                    },
                },
                "required": ["sql"],
            },
            "http": {
                "method": "POST",
                "url": f"{server_url}/api/admin/db/query",
                "headers": {**auth_headers, "Content-Type": "application/json"},
                "body_template": {"sql": "{sql}"},
            },
        },
        {
            "name": "get_report_totals",
            "description": (
                "Get aggregate analytics: total jobs analyzed, total failures, "
                "total reviewed. Filter by team, tier, version, date range, "
                "status, tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_REPORT_FILTER_PARAMS},
            },
            "http": {
                "method": "GET",
                "url": f"{server_url}/api/reports/totals",
                "headers": auth_headers,
                "query_params": True,
            },
        },
        {
            "name": "get_classification_overrides",
            "description": (
                "Get classification overrides — where users changed the AI's "
                "classification. Grouped by from→to transition. Includes AI "
                "accuracy percentage."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_REPORT_FILTER_PARAMS},
            },
            "http": {
                "method": "GET",
                "url": f"{server_url}/api/reports/classification-overrides",
                "headers": auth_headers,
                "query_params": True,
            },
        },
        {
            "name": "get_issues_created",
            "description": (
                "Get GitHub issues and Jira tickets created from RootCoz "
                "analyses. Separate GitHub and Jira totals."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_REPORT_FILTER_PARAMS},
            },
            "http": {
                "method": "GET",
                "url": f"{server_url}/api/reports/issues-created",
                "headers": auth_headers,
                "query_params": True,
            },
        },
        {
            "name": "save_report",
            "description": (
                "Save a generated HTML report as a downloadable file. "
                "Use this after generating a self-contained HTML report. "
                "Returns a download URL that you should share with the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html_content": {
                        "type": "string",
                        "description": "Complete self-contained HTML with inline CSS",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Descriptive filename, e.g. failure-report-2026-06-14.html"
                        ),
                    },
                },
                "required": ["html_content", "filename"],
            },
            "http": {
                "method": "POST",
                "url": f"{server_url}/api/admin-chat/artifacts",
                "headers": {**auth_headers, "Content-Type": "application/json"},
                "body_template": {
                    "html_content": "{html_content}",
                    "filename": "{filename}",
                },
            },
        },
    ]


def _safe_remove_symlink(link: Path) -> None:
    """Remove a symlink and its target directory if safe.

    Only deletes the target when it resolves to a path under the process
    temp root (``tempfile.gettempdir()``, honors ``TMPDIR``) to prevent
    accidental deletion of system directories if the symlink was manipulated.
    """
    target = link.resolve()
    link.unlink(missing_ok=True)
    if target.exists() and target.is_dir():
        tmp_root = _tmp_root()
        if target.is_relative_to(tmp_root):
            shutil.rmtree(target, ignore_errors=True)
        else:
            logger.warning(
                "Chat cleanup: refusing to delete symlink target outside %s: %s",
                tmp_root,
                target,
            )


def cleanup_chat_repos(job_id: str, username: str = "") -> None:
    """Delete cloned repos and artifacts from chat workspace but keep session files."""
    workspace = get_chat_workspace(job_id, username)
    if not workspace.exists():
        return

    # Delete everything except hidden dirs (which contain session data)
    for item in workspace.iterdir():
        if item.name.startswith("."):
            continue  # Keep .claude/, .cursor/ etc (session data)
        if item.is_symlink():
            _safe_remove_symlink(item)
        elif item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)

    logger.info(f"Cleaned up chat repos for job {job_id} (kept sessions)")


def cleanup_chat_workspace(job_id: str, username: str = "") -> None:
    """Delete the entire chat workspace including sessions."""
    workspace = get_chat_workspace(job_id, username)
    if not workspace.exists():
        return

    # Resolve symlinks before removing workspace tree — symlinked
    # artifact dirs live outside the workspace and won't be cleaned
    # by shutil.rmtree (which removes the symlink, not its target).
    for item in workspace.iterdir():
        if item.is_symlink():
            _safe_remove_symlink(item)

    shutil.rmtree(workspace, ignore_errors=True)
    logger.info(f"Deleted chat workspace for job {job_id}")


# -- Shared prompt building helpers --


def _build_tools_section(custom_tools: list[dict]) -> str:
    """Build tools section from custom tool definitions."""
    if not custom_tools:
        return "(No tools available)"
    lines = []
    for tool in custom_tools:
        lines.append(f"- `{tool['name']}` — {tool['description']}")
    return "\n".join(lines)


def _build_unavailable_section(custom_tools: list[dict]) -> str:
    """Build notice about unavailable tools (Jira/GitHub not configured)."""
    tool_names = {t["name"] for t in custom_tools}
    jira_configured = "search_jira" in tool_names
    github_configured = "search_github_issues" in tool_names

    lines = []
    if not jira_configured:
        lines.append(
            "- **Jira search** is not available. If the user asks about Jira tickets, "
            'tell them: "Jira search is not available for your account. To enable it, '
            "go to your [User Settings](/settings) page and configure your Jira credentials "
            '(URL, email, and API token), then start a new chat session."'
        )
    if not github_configured:
        lines.append(
            "- **GitHub search** is not available. If the user asks about GitHub issues or PRs, "
            'tell them: "GitHub search is not available. To enable it, '
            "configure your GitHub token on the [User Settings](/settings) page, "
            'then start a new chat session."'
        )

    if not lines:
        return ""
    return "\n\n## Unavailable Tools\n" + "\n".join(lines)


def build_welcome_message(
    *,
    job_name: str,
    build_number: int | str,
    repos_available: bool = False,
    ci_build_data_available: bool = False,
    jira_available: bool = False,
    github_available: bool = False,
) -> str:
    """Build a dynamic welcome message listing available resources."""
    resources = [
        "📊 Job analysis results (failures, classifications, AI analysis)",
        "💬 Job comments and discussion",
    ]
    if repos_available:
        resources.append("📁 Test repository and source code")
    if ci_build_data_available:
        resources.append("🔧 Build artifacts (logs, test output, cluster data)")
        resources.append("🖥️ CI console output")
        resources.append("⚙️ Build metadata (status, params, timing)")
    resources.append("🔍 Failure history — check if this test failed before")
    resources.append("📋 Classification history — see who changed classifications")
    if jira_available:
        resources.append("🐛 Jira issue search")
    if github_available:
        resources.append("🐙 GitHub issues search")

    return (
        f"👋 Welcome! I'm here to help you understand and discuss the analysis results "
        f"for **{job_name} #{build_number}**.\n\n"
        f"**Resources available in this chat:**\n"
        + "\n".join(f"- {r}" for r in resources)
        + "\n\nAsk me anything about this analysis!"
    )


_COMMON_RULES = """- Do NOT modify any data — read-only access only
- Do NOT run arbitrary curl commands — use the provided tools"""


def build_system_prompt(
    job_name: str,
    build_number: int | str,
    job_id: str,
    custom_tools: list[dict],
    repos_available: bool = False,
    ci_build_data_available: bool = False,
) -> str:
    """Build a system prompt that scopes the AI to a specific analyzed job."""
    tools_section = _build_tools_section(custom_tools)
    unavailable_section = _build_unavailable_section(custom_tools)

    repos_note = ""
    if repos_available:
        repos_note = (
            "\n\nSource repositories are cloned in your working directory. "
            "You can explore test and product code directly."
        )
    if ci_build_data_available:
        repos_note += (
            "\n\nCI build data is available in your working directory:"
            "\n- `console-output.txt` \u2014 full CI console output"
            "\n- `build-info.json` \u2014 build result, parameters, timing, and duration"
            "\n- `build-artifacts/` \u2014 downloaded build artifacts (logs, test outputs)"
            "\nUse the `read` tool to examine these files when relevant to the user's question."
        )

    return f"""You are a CI/CD failure analysis expert. You are helping a user understand the analysis results for job **{job_name} #{build_number}** (job ID: {job_id}).

## Your Role
- Help the user understand test failures, their root causes, and classifications
- Suggest fixes and identify patterns across failures
- Search for related Jira tickets or GitHub issues when asked
- Explore cloned source code to understand test logic when relevant
- Be concise and technical

## Available Tools
You have structured tools that you can call directly:
{tools_section}

**IMPORTANT:** Use these tools to get data when the user asks a question. Do NOT call tools proactively — only fetch data that's relevant to what the user is asking about.{repos_note}{unavailable_section}

## Rules — STRICT
- You MUST only discuss this specific job and its failures
- If the user asks something unrelated to this job (e.g., "what's the time?", general coding questions, weather, anything not about this analysis), respond ONLY with: "I can only discuss the analysis results for this job. How can I help you understand the failures?"
- Do NOT answer off-topic questions. Do NOT be helpful about non-job topics. Reject them immediately.
{_COMMON_RULES}
"""


def build_admin_system_prompt(
    custom_tools: list[dict],
) -> str:
    """Build system prompt for admin global chat — server-wide scope."""
    tools_section = _build_tools_section(custom_tools)
    unavailable_section = _build_unavailable_section(custom_tools)

    return f"""You are a CI/CD analytics expert with access to the entire rootcoz server data.

## Your Role
- Answer questions about test failure trends across all jobs
- Provide cross-job analytics (failure counts, classification distributions)
- Show user activity and review statistics
- Help identify recurring failures and patterns
- Provide pre-built analytics reports (totals, classification overrides, issues created)
- Query server configuration and settings

## Scope — STRICT
- You provide server-wide analytics: failure counts, trends, user activity, job statistics
- You do NOT deep-dive into individual test failures, root causes, or classifications
- If the user asks about a specific failure's details (e.g., "why did test X fail?", "what's the root cause of..."), respond ONLY with:
  "For detailed failure analysis, use the job-specific chat at /chat/{{job_id}}. I can help you find the job ID if you describe the test or job name."
- You CAN answer aggregate questions about failures (e.g., "how many infrastructure failures in the last week", "which tests fail most often")

## Available Tools
You have structured tools — call them directly:
{tools_section}

**WORKFLOW:** For analytics questions (totals, overrides, issues), use the pre-built report tools first — they're faster and more accurate. Use `db_schema` + `db_query` only for questions the report tools can't answer.

**SENSITIVE DATA:** Some columns contain encrypted values (tokens, passwords). Never output raw encrypted field values.{unavailable_section}

## Report Generation
When the user asks for a "report", "generate a report", "export", or similar:
1. Use the analytics tools (`get_report_totals`, `get_classification_overrides`, `get_issues_created`, `db_query`) to gather the requested data
2. Generate a **self-contained HTML report** with:
   - `<!DOCTYPE html>` document with all CSS inline in a `<style>` tag (NO external stylesheets or scripts)
   - A header section with report title and generation date
   - Summary cards showing key metrics (totals, percentages, counts)
   - Styled data tables with alternating row colors
   - Color-coded status/classification badges
   - Professional, clean design using a neutral color palette
   - Responsive layout that works in any browser
3. Save the HTML via the `save_report` tool with a descriptive filename (e.g., `failure-report-2026-06-14.html`)
4. Share the download link with the user in your response

The HTML MUST be completely self-contained — no external CSS, JS, fonts, or CDN links. Everything inline.

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
    custom_tools: list[dict] | None = None,
    restrict_tools: bool = True,
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
        # Best-effort catalog warm; heuristic routing still works if this fails.
        await _prewarm_model_routes(normalize_provider(ai_provider), ai_model)
        sidecar_provider, sidecar_model = map_provider_model_for_sidecar(
            ai_provider, ai_model
        )
        create_kwargs: dict = {
            "provider": sidecar_provider,
            "model": sidecar_model,
            "system_prompt": system_prompt,
            "cwd": str(repo_path) if repo_path else "/tmp",
        }
        if custom_tools:
            create_kwargs["custom_tools"] = custom_tools
        if restrict_tools:
            create_kwargs["tools"] = list(CHAT_BUILTIN_TOOLS)
        session_id = await client.create_session(**create_kwargs)
        logger.info("%s: session created: %s", log_prefix, session_id)
        return session_id
    except Exception:
        logger.warning("%s: failed to create session", log_prefix, exc_info=True)
        return None


async def init_chat_session(
    *,
    job_id: str,
    job_name: str,
    build_number: int | str,
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    custom_tools: list[dict] | None = None,
    repos_available: bool = False,
    ci_build_data_available: bool = False,
) -> str | None:
    """Initialize a chat session via the sidecar. Returns session_id or None."""
    system_prompt = build_system_prompt(
        job_name=job_name,
        build_number=build_number,
        job_id=job_id,
        custom_tools=custom_tools or [],
        repos_available=repos_available,
        ci_build_data_available=ci_build_data_available,
    )
    return await _create_chat_session(
        system_prompt=system_prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        repo_path=repo_path,
        log_prefix=f"Chat(job={job_id})",
        custom_tools=custom_tools,
        restrict_tools=True,
    )


async def init_admin_chat_session(
    *,
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    custom_tools: list[dict] | None = None,
) -> str | None:
    """Initialize an admin chat session via the sidecar. Returns session_id or None."""
    system_prompt = build_admin_system_prompt(custom_tools=custom_tools or [])
    return await _create_chat_session(
        system_prompt=system_prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        repo_path=repo_path,
        log_prefix="Admin chat",
        custom_tools=custom_tools,
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
    custom_tools: list[dict] | None = None,
    restrict_tools: bool = True,
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

    call_kwargs: dict = {
        "ai_provider": ai_provider,
        "ai_model": ai_model,
        "cwd": str(repo_path) if repo_path else None,
        "ai_call_timeout": ai_call_timeout,
        "session_id": session_id,
    }
    if custom_tools:
        call_kwargs["custom_tools"] = custom_tools
    if not session_id and restrict_tools:
        call_kwargs["tools"] = list(CHAT_BUILTIN_TOOLS)
    result = await call_ai(prompt, **call_kwargs)

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
        retry_kwargs: dict = {
            "ai_provider": ai_provider,
            "ai_model": ai_model,
            "cwd": str(repo_path) if repo_path else None,
            "ai_call_timeout": ai_call_timeout,
            "session_id": None,
        }
        if custom_tools:
            retry_kwargs["custom_tools"] = custom_tools
        if restrict_tools:
            retry_kwargs["tools"] = list(CHAT_BUILTIN_TOOLS)
        result = await call_ai(prompt, **retry_kwargs)

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
    build_number: int | str,
    message: str,
    history: list[dict],
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    ai_call_timeout: int | None = None,
    session_id: str | None = None,
    custom_tools: list[dict] | None = None,
    repos_available: bool = False,
    ci_build_data_available: bool = False,
) -> tuple[bool, str, str | None]:
    """Send a chat message and get an AI response via the sidecar."""

    def _build_prompt() -> str:
        return build_system_prompt(
            job_name=job_name,
            build_number=build_number,
            job_id=job_id,
            custom_tools=custom_tools or [],
            repos_available=repos_available,
            ci_build_data_available=ci_build_data_available,
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
        custom_tools=custom_tools,
        restrict_tools=True,
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
    custom_tools: list[dict] | None = None,
) -> tuple[bool, str, str | None]:
    """Send an admin chat message and get an AI response."""

    def _build_prompt() -> str:
        return build_admin_system_prompt(custom_tools=custom_tools or [])

    return await _chat_with_ai_impl(
        message=message,
        history=history,
        ai_provider=ai_provider,
        ai_model=ai_model,
        build_prompt_fn=_build_prompt,
        repo_path=repo_path,
        ai_call_timeout=ai_call_timeout,
        session_id=session_id,
        custom_tools=custom_tools,
        log_prefix="Admin chat",
        request_id="__admin_chat__",
        call_type="admin_chat",
    )
