"""Chat engine for interactive AI sessions about analyzed jobs.

Builds job-scoped system prompts and manages AI CLI conversations.
"""

import asyncio
import base64
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from pi_sidecar_client import get_sidecar_client

from rootcoz import get_child_logger
from rootcoz.ai_client import (
    CHAT_BUILTIN_TOOLS,
    call_ai,
    create_session_safely,
    resolve_catalog_pair,
)
from rootcoz.engine.http_mcp import (
    _workspace_install_lock,
    cleanup_http_tools_mcp,
    install_http_tools_mcp_best_effort_async,
)
from rootcoz.error_messages import tracker_credential_message
from rootcoz.jira import (
    JiraConnection,
    classify_jira_secret,
    resolve_jira_connection_from_params,
)
from rootcoz.storage import AI_SYSTEM_USERNAME
from rootcoz.temp_paths import (
    ABANDON,
    DeleteResult,
    PathState,
    _Abandoned,
    delete_tree,
    reusable_path_lock,
)

logger = get_child_logger(__name__)

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


@asynccontextmanager
async def chat_workspace_lock(
    job_id: str, username: str = ""
) -> AsyncIterator[PathState]:
    """Hold the chat workspace's path lock for the create/reuse decision.

    The path is deterministic and reused across messages, so a queued delete of
    ``chat-{job}/{user}`` must finish before the next message creates into it.
    Yields the :class:`~rootcoz.temp_paths.PathState` and raises
    :class:`~rootcoz.temp_paths.PathStateUnknown` when a live builder still owns
    a marked tree, or the recovery could not delete every path in it, so no caller
    reaches repository preparation on a half-deleted workspace.
    """
    async with reusable_path_lock(
        job_id, get_chat_workspace(job_id, username)
    ) as state:
        yield state


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
    except TypeError, AttributeError, ValueError:
        return False


async def clone_chat_repos(
    workspace: Path,
    request_params: dict[str, Any],
    user_repo_token: str = "",
    on_repo: Callable[[str], None] | None = None,
) -> bool:
    """Clone repos into the chat workspace.

    Skips repos already present in the workspace.
    Returns True only when a repository was newly cloned.
    ``on_repo`` is called with each repo name right before it is cloned.

    Note:
        Repo tokens may be embedded in git remote URLs within the workspace.
        The workspace is in /tmp/ owned by the server process. Tokens are
        cleaned up when repos are deleted via cleanup_chat_repos().
    """
    from rootcoz.config import parse_repo_ref
    from rootcoz.models import AdditionalRepo
    from rootcoz.repository import RepositoryManager, derive_test_repo_name

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
                    else:
                        logger.info(
                            "Chat: cloning repo %s into %s", repo_name, workspace
                        )
                        if on_repo:
                            on_repo(repo_name)
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
                        continue
                    logger.info("Chat: cloning repo %s into %s", safe_name, workspace)
                    if on_repo:
                        on_repo(safe_name)
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
    properties: dict[str, dict[str, Any]] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Shared GET /history/test/{test_name} tool definition."""
    props: dict[str, dict[str, Any]] = {
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
    properties: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shared GET /history/classifications tool definition."""
    props: dict[str, dict[str, Any]] = {
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
) -> list[dict[str, Any]]:
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
                "MANDATORY for every failure group -- one call per group. Find "
                "other tests/jobs sharing this error signature (infrastructure "
                "vs isolated). A failure group can have several hashes: pass "
                "them ALL as one comma-separated 'signature' value rather than "
                "calling this once per hash."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signature": {
                        "type": "string",
                        "description": (
                            "Error signature hash for this failure group, or "
                            "every hash it has as one comma-separated value"
                        ),
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
                "MANDATORY for every analyzed test, including tests with human "
                "root-cause classifications. Records pattern only (NEW/REGRESSION/"
                "FLAKY/INTERMITTENT/KNOWN_BUG/PERSISTENT). source is always ai."
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


def analysis_http_tools(
    *,
    server_url: str,
    job_id: str,
    auth_header: str,
) -> list[dict[str, Any]]:
    """Optional authenticated history tools for analysis sessions."""
    tools: list[dict[str, Any]] = []
    if not (server_url and job_id and auth_header):
        return tools
    token = auth_header.removeprefix("Bearer ").strip()
    if token:
        tools.extend(
            build_analysis_history_tools(
                server_url=server_url,
                auth_token=token,
                job_id=job_id,
            )
        )
    return tools


def _tracker_issue_tools(
    *,
    server_url: str,
    auth_headers: dict[str, str],
    targets: tuple[str, ...],
    issue_repos: tuple[str, ...] = (),
    job_id: str = "",
    conversation: str = "",
) -> list[dict[str, Any]]:
    """The tracker tools chat may call, as the two steps the server enforces.

    ``propose_tracker_issue`` stores the draft the user is asked to approve and
    returns a confirmation code; ``create_<target>_issue`` files it, and needs
    that id and code — plus a reply from the USER carrying the code, which the
    server looks for in this conversation and the model cannot write. Both post
    to the server, which files under the caller's OWN saved credential.
    ``targets`` names only the trackers the server enables, can file in, *and*
    the user's account holds a token for; empty means no tool is advertised.

    ``issue_repos`` are the repos this session may file a GitHub issue in and
    ``job_id`` is the run they belong to — both empty for an admin chat, whose
    repo choice the server does not cap. ``conversation`` is hardcoded into the
    proposal so the draft belongs to THIS chat: the one the user replies in.
    """
    if not targets:
        return []
    github = "github" in targets
    repo_body = {"repo": "{repo}"} if github else {}
    repo_properties = {"repo": _issue_repo_param(issue_repos)} if github else {}
    # The run a job chat belongs to, hardcoded so the model cannot name another
    # one: the draft binds it and a create naming a different run is refused.
    job_body = {"job_id": job_id} if job_id else {}
    # The chat this draft belongs to, also hardcoded: a draft proposed in a chat
    # is only redeemable after a user message in it carries the code.
    conversation_body = {"conversation": conversation} if conversation else {}
    tools: list[dict[str, Any]] = [
        {
            "name": "propose_tracker_issue",
            "description": (
                "Step 1 of filing an issue. Draft the title and body for the "
                "issue the user asked about and call this first: it files "
                "nothing, and returns a confirmation_id, a confirmation_code "
                "and a preview naming the tracker, the destination, the exact "
                "title and body, and that code. The server also posts that "
                "preview to this chat itself, so the user sees it even if your "
                "own summary is incomplete: tell them it is below and ask "
                "them to reply in this chat with the code. Nothing is filed "
                "until they do, and only they can confirm it. Call this again "
                "for any edit — an id and code authorise one specific draft."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": list(targets),
                        "description": "Which tracker to draft the issue for",
                    },
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {
                        "type": "string",
                        "description": (
                            "Issue description summarising the failure and "
                            "context the user asked about"
                        ),
                    },
                    **repo_properties,
                },
                "required": ["target", "title", "body"],
            },
            "http": {
                "method": "POST",
                "url": f"{server_url}/api/chat-issue/propose",
                "headers": {**auth_headers, "Content-Type": "application/json"},
                "body_template": {
                    "target": "{target}",
                    "title": "{title}",
                    "body": "{body}",
                    **repo_body,
                    **job_body,
                    **conversation_body,
                },
            },
        }
    ]
    for target in targets:
        kind = "GitHub" if target == "github" else "Jira"
        if target != "github":
            repo_rule = (
                "You do not choose the Jira project: the server files it in the "
                "configured one. "
            )
        elif issue_repos:
            repo_rule = (
                "You may only name this run's test repo or one of its "
                "additional repos as repo; leave it empty for the test repo. "
            )
        else:
            repo_rule = (
                "You may name any GitHub repository as repo; leave it empty for "
                "the server's configured test repo. "
            )
        tools.append(
            {
                "name": f"create_{target}_issue",
                "description": (
                    f"Step 2 of filing a {kind} issue: file the draft the user "
                    "confirmed. Call this ONLY after the user has replied in "
                    "this chat with the confirmation code from the preview, and "
                    "pass that confirmation_id and confirmation_code with the "
                    "same title and body. A create nobody confirmed fails and "
                    "files nothing — the code has to come from the user, so you "
                    "cannot confirm your own draft. The id and code are "
                    "single-use and expire, so propose again for any change. "
                    f"{repo_rule}"
                    "Returns the created issue URL and number/key; a refusal "
                    "must be relayed to the user verbatim."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Issue title, as proposed",
                        },
                        "body": {
                            "type": "string",
                            "description": ("Issue description, exactly as proposed"),
                        },
                        "confirmation_id": {
                            "type": "string",
                            "description": (
                                "The confirmation_id propose_tracker_issue "
                                "returned for this exact title and body"
                            ),
                        },
                        "confirmation_code": {
                            "type": "string",
                            "description": (
                                "The code the preview showed, as the user "
                                "replied with it"
                            ),
                        },
                        **(repo_properties if target == "github" else {}),
                    },
                    "required": [
                        "title",
                        "body",
                        "confirmation_id",
                        "confirmation_code",
                    ],
                },
                "http": {
                    "method": "POST",
                    "url": f"{server_url}/api/chat-issue",
                    "headers": {**auth_headers, "Content-Type": "application/json"},
                    "body_template": {
                        "target": target,
                        "title": "{title}",
                        "body": "{body}",
                        "confirmation_id": "{confirmation_id}",
                        "confirmation_code": "{confirmation_code}",
                        **(repo_body if target == "github" else {}),
                        # The run that bounds the repo choice, empty for admin chat.
                        **(job_body if target == "github" else {}),
                    },
                },
            }
        )
    return tools


def build_chat_custom_tools(
    *,
    server_url: str,
    auth_token: str,
    job_id: str,
    jira_cloud_url: str = "",
    jira_server_url: str = "",
    jira_email: str = "",
    jira_token: str = "",
    github_token: str = "",
    github_repo: str = "",
    jira_connection: JiraConnection | None = None,
    tracker_targets: tuple[str, ...] = (),
    issue_repos: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Build HTTP-backed custom tools for a chat session.

    Returns tool definitions with 'http' configs that the sidecar
    executes directly — no bash, no scripts on disk.

    ``jira_connection`` overrides the raw Jira params when provided —
    the async caller resolves and completes it (service account mode
    needs an async cloud ID lookup that cannot happen here).
    """
    tools: list[dict[str, Any]] = []
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
        {
            "name": "get_job_tests",
            "description": (
                "Get paginated test entries (passed, skipped, failed) for this job. "
                "Use status filter to get specific outcomes. Returns entries with "
                "test_name, duration, status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: passed, skipped, or failed",
                    },
                    "offset": {
                        "type": "string",
                        "description": "Pagination offset (default 0)",
                        "default": "0",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Page size (default 50, max 200)",
                        "default": "50",
                    },
                },
            },
            "http": {
                "method": "GET",
                "url": f"{server_url}/api/results/{job_id}/tests",
                "headers": auth_headers,
                "query": {
                    "status": "{status}",
                    "offset": "{offset}",
                    "limit": "{limit}",
                },
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

    jira_conn = jira_connection
    if jira_conn is None and (jira_token or jira_email):
        # Fallback ONLY for an absent selection (jira_connection=None): a
        # selected-but-failed connection arrives as a mode-"none"
        # JiraConnection sentinel (never None), which blocks this fallback
        # so chat never silently switches to the user's OTHER stored
        # credential (e.g. basic after the selected OAuth failed).
        #
        # Prefix detection wins over field placement: an ATOA secret or
        # ATST scoped token in the regular field routes to its method so
        # validation and actual use agree — but this sync fallback cannot
        # mint an access token (async), and it has no cloud ID to build
        # the gateway URL, so such connections stay unusable here; async
        # callers pass a completed connection via jira_connection.
        kind = classify_jira_secret(jira_token)
        sa_fallback = jira_token if kind == "oauth" else ""
        atst_fallback = jira_token if kind == "atst" else ""
        jira_conn = resolve_jira_connection_from_params(
            jira_cloud_url=jira_cloud_url,
            jira_server_url=jira_server_url,
            jira_email=jira_email,
            jira_token="" if sa_fallback or atst_fallback else jira_token,
            jira_oauth_client_secret=sa_fallback,
            jira_atst_token=atst_fallback,
        )
    if jira_conn and jira_conn.api_base and jira_conn.mode != "none":
        # Basic auth (cloud) must be embedded as a header — tool HTTP
        # configs cannot carry httpx auth objects.
        if jira_conn.auth:
            encoded = base64.b64encode(":".join(jira_conn.auth).encode()).decode()
            jira_auth: dict[str, str] = {"Authorization": f"Basic {encoded}"}
        else:
            jira_auth = dict(jira_conn.headers)

        # REST version comes from the connection itself (single source of
        # truth): SA/cloud carry v3 paths, Server/DC carries v2 — the v2
        # search endpoint also needs an explicit fields param.
        is_v2_search = jira_conn.search_path == "/rest/api/2/search"

        search_url = f"{jira_conn.api_base}{jira_conn.search_path}"
        issue_url = f"{jira_conn.api_base}{jira_conn.api_path}/issue/{{issue_key}}"
        search_params: dict[str, str] = {
            "jql": 'summary ~ "{query}" ORDER BY updated DESC',
            "maxResults": "{limit}",
        }
        if is_v2_search:
            search_params["fields"] = "summary,status,assignee,created,updated"

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

    tools.extend(
        _tracker_issue_tools(
            server_url=server_url,
            auth_headers=auth_headers,
            targets=tracker_targets,
            issue_repos=issue_repos,
            job_id=job_id,
            # This chat: the user's reply with the code has to land here.
            conversation=job_id,
        )
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
    tracker_targets: tuple[str, ...] = (),
    conversation: str = "",
) -> list[dict[str, Any]]:
    """Build HTTP-backed custom tools for admin chat.

    ``conversation`` is the admin chat's own conversation id, passed to the
    issue tools: a draft proposed there is only redeemable after the admin
    replied in that chat with its code.
    """
    auth_headers = {"Authorization": f"Bearer {auth_token}"}

    tools: list[dict[str, Any]] = [
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
    tools.extend(
        _tracker_issue_tools(
            server_url=server_url,
            auth_headers=auth_headers,
            targets=tracker_targets,
            conversation=conversation,
        )
    )
    return tools


def _unlink_counted(path: Path) -> DeleteResult:
    """Unlink ``path`` and report what that freed; an entry already gone frees nothing.

    ``delete_tree`` on a single path, so a refusal (EPERM, EBUSY) is counted in
    ``failed`` with its errno instead of raising: a raise here aborted the rest of
    the workspace cleanup, leaving the paths behind this one untried.
    """
    return delete_tree(path)


def _safe_remove_symlink(link: Path) -> DeleteResult:
    """Remove a symlink and its target directory if safe.

    Only deletes the target when it resolves to a path under the process
    temp root (``tempfile.gettempdir()``, honors ``TMPDIR``) to prevent
    accidental deletion of system directories if the symlink was manipulated.
    The link itself is counted too: it is an entry this call really removed.
    """
    target = link.resolve()
    removed = _unlink_counted(link)
    if target.exists() and target.is_dir():
        tmp_root = _tmp_root()
        if target.is_relative_to(tmp_root):
            return removed.plus(delete_tree(target))
        logger.warning(
            "Chat cleanup: refusing to delete symlink target outside %s: %s",
            tmp_root,
            target,
        )
    return removed


def cleanup_chat_repos(job_id: str, username: str = "") -> DeleteResult | _Abandoned:
    """Delete cloned repos, graphs and symlinked artifact targets from a workspace.

    A reaper step, never inline. The workspace itself (sessions included) is the
    whole-tree ``delete_tree`` step the caller queues right after this one;
    :data:`ABANDON` stops that step when a live graph builder owns the tree.
    """
    workspace = get_chat_workspace(job_id, username)
    from rootcoz.engine.graft import reap_graph
    from rootcoz.engine.graft_http import unregister_workspace

    unregister_workspace(workspace)
    graph = reap_graph(job_id, workspace)
    if isinstance(graph, _Abandoned):
        return ABANDON
    total = graph
    if not workspace.exists():
        return total

    # Delete everything except hidden dirs (which contain session data)
    for item in workspace.iterdir():
        if item.name.startswith("."):
            continue  # Keep .claude/, .cursor/ etc (session data)
        if item.is_symlink():
            total = total.plus(_safe_remove_symlink(item))
        elif item.is_dir():
            total = total.plus(delete_tree(item))
        else:
            total = total.plus(_unlink_counted(item))

    logger.info(f"Cleaned up chat repos for job {job_id}")
    return total


def _chat_mcp_lock_targets(workspace: Path) -> list[Path]:
    """Job workspace plus nested per-user dirs that have their own MCP dumps."""
    targets: list[Path] = []
    if workspace.exists() and workspace.is_dir():
        for child in workspace.iterdir():
            if child.is_dir() and not child.is_symlink():
                targets.append(child)
    targets.append(workspace)
    return list(dict.fromkeys(targets))


def cleanup_chat_workspace(
    job_id: str, username: str = ""
) -> DeleteResult | _Abandoned:
    """Delete the entire chat workspace including sessions.

    :data:`ABANDON` means a live graph builder still owns the tree, so the
    caller's whole-tree delete is skipped and the next create's recovery picks it
    up.
    """
    workspace = get_chat_workspace(job_id, username)
    targets = _chat_mcp_lock_targets(workspace)
    from rootcoz.engine.graft import reap_graph
    from rootcoz.engine.graft_http import unregister_workspace

    unregister_workspace(workspace)
    graph = reap_graph(job_id, workspace)
    if isinstance(graph, _Abandoned):
        return ABANDON
    total = graph
    # Hold each workspace's MCP install lock for the whole delete + dump
    # unlink so an in-flight installer cannot recreate credentials after
    # the job (or nested user dir) was removed.
    with ExitStack() as stack:
        for target in sorted(targets, key=lambda p: str(p)):
            stack.enter_context(_workspace_install_lock(target))
        if workspace.exists():
            # Resolve symlinks before removing workspace tree — symlinked
            # artifact dirs live outside the workspace and are not reached by
            # the tree walk (which unlinks the symlink, not its target).
            for item in workspace.iterdir():
                if item.is_symlink():
                    total = total.plus(_safe_remove_symlink(item))
            total = total.plus(delete_tree(workspace))
        for target in targets:
            cleanup_http_tools_mcp(target)
    logger.info(f"Deleted chat workspace for job {job_id}")
    return total


# -- Shared prompt building helpers --


def _build_tools_section(custom_tools: list[dict[str, Any]]) -> str:
    """Build tools section from custom tool definitions."""
    if not custom_tools:
        return "(No tools available)"
    lines = []
    for tool in custom_tools:
        lines.append(f"- `{tool['name']}` — {tool['description']}")
    return "\n".join(lines)


def _build_unavailable_section(custom_tools: list[dict[str, Any]]) -> str:
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


_COMMON_RULES = """- Do NOT modify any data — read-only. The one exception is a tracker issue tool in your tool list (`propose_tracker_issue` plus `create_github_issue` / `create_jira_issue`), and only once the user has approved the draft you proposed
- Do NOT run arbitrary curl commands — use the provided tools"""

#: Where a user adds a tracker token. The prompt reason below and the notice
#: the server posts itself (:func:`credential_blocked_issue_notice`) read it, so
#: the page they name cannot drift apart.
_TRACKER_TOKEN_FIX = "Profile Settings → add the {kind} token."

#: What to tell the user for each reason a tracker tool is missing from the list.
#: `{kind}` is the tracker name. Only the missing-token case is fixed in Profile
#: Settings — a disabled tracker or a missing repository is not, and an account
#: with no ``users`` row has nowhere to save one at all.
_TRACKER_UNAVAILABLE_REASONS = {
    "server_disabled": (
        "issue creation is disabled on this server, so no token can enable it — "
        "an administrator has to turn it on"
    ),
    "no_repository": (
        "this server has no TESTS_REPO_URL configured, so there is no "
        "repository to file in — an administrator has to set it, and a token "
        "will not help"
    ),
    "no_run_repository": (
        "this run has no GitHub test repository and no additional repo, and a "
        "job chat files only into this run's repos — the fix is a run that names "
        "one, not a token or a server setting"
    ),
    "no_token": (
        "this account has no {kind} token saved. Tell the user the fix: "
        + _TRACKER_TOKEN_FIX
    ),
    "no_token_no_profile": (
        "this account has no {kind} token saved and no profile to store one in, "
        "so it cannot save a token at all. Tell the user the fix in these words: "
        + tracker_credential_message("{kind}", has_profile=False)
    ),
    "unusable_credential": (
        "this account's saved {kind} credential cannot be used with the "
        "server's {kind} settings, so no issue can be filed with it. Tell the "
        "user the fix: Profile Settings → re-save the {kind} credential."
    ),
}

#: The reasons a tracker is missing because THIS account never set it up — the
#: only ones a token in Profile Settings can fix. A disabled tracker or a
#: missing repository is not one of them, so no token advice is posted for it.
_CREDENTIAL_UNAVAILABLE_REASONS = ("no_token", "no_token_no_profile")

#: The forms people write "file an issue" in: a filing verb (any tense)
#: before the noun, or the participle after it ("a GitHub issue filed for this").
_FILING_VERB = (
    r"open(?:ed)?|fil(?:e|ed)|creat(?:e|ed)|submit(?:ted)?|rais(?:e|d)?|"
    r"report(?:ed)?|log(?:ged)?"
)
_FILING_PARTICIPLE = r"opened|filed|created|submitted|raised|reported|logged"
_ISSUE_NOUN = r"issue|ticket"

#: A filing request: a filing verb and an issue noun in the same sentence, in
#: either order, so "open the console log" is not one.
_ASKS_TO_FILE_ISSUE = re.compile(
    rf"\b(?:{_FILING_VERB})\b[^\n.!?]{{0,40}}?\b(?:{_ISSUE_NOUN})\b"
    rf"|\b(?:{_ISSUE_NOUN})\b[^\n.!?]{{0,20}}?\b(?:{_FILING_PARTICIPLE})\b",
    re.IGNORECASE,
)

#: A refusal or a report of an action already taken is not a request: the words
#: pinned to the verb negate it ("don't open a GitHub issue") or say it happened
#: ("I already filed a GitHub issue"). Only what touches the verb counts, so
#: "I don't have a Jira token, open a Jira issue" still asks for one.
_NOT_A_REQUEST = re.compile(
    r"\b(?:don'?t|do not|never|no need to|not|already)\b[^\n.!?,]{0,8}$",
    re.IGNORECASE,
)

#: The trackers a message can name. A filing request that names none posts no
#: notice: there is no single tracker it could be about.
_NAMED_TRACKERS: tuple[tuple[Literal["github", "jira"], re.Pattern[str]], ...] = (
    ("github", re.compile(r"\bgithub\b", re.IGNORECASE)),
    ("jira", re.compile(r"\bjira\b", re.IGNORECASE)),
)


def _mention_gap(request: tuple[int, int], mention: tuple[int, int]) -> int:
    """Characters between two spans, ``0`` when they touch or overlap."""
    return max(0, max(request[0], mention[0]) - min(request[1], mention[1]))


def asked_issue_tracker(message: str) -> Literal["", "github", "jira"]:
    """The tracker *message* asks to file an issue in, ``""`` when it asks nothing.

    The trigger half of the server's own no-credential answer: it fires only when
    the user's own words are a request to file an issue AND name the tracker, so
    an unrelated question never gets a credential notice. The other half — is
    that tracker missing a credential this account can add? — is the caller's,
    read from :func:`credential_blocked_issue_notice`'s ``reason``.

    The tracker nearest the filing request wins — inside it when the request
    carries the name ("file a Jira issue"), else the closest mention — so
    "GitHub is blocked for me, file a Jira issue" answers about Jira and not
    about the first tracker in the string. Every occurrence counts, so
    "GitHub is irrelevant. Please file a GitHub issue, not a Jira issue" answers
    about GitHub. A tie goes to the earlier mention.

    A refused phrase does not swallow the message: "I already filed a GitHub
    issue. Please create a Jira issue" answers about Jira — the first request
    that is not negated is the one read.

    shortcut: only a tracker-named request is detected ("open an issue" alone
    posts nothing, the system prompt still covers it), and only a negation
    pinned to the verb is read, so "a GitHub issue I already filed" still
    fires. Upgrade if users commonly ask this way.
    """
    text = message or ""
    request = next(
        (
            match
            for match in _ASKS_TO_FILE_ISSUE.finditer(text)
            if not _NOT_A_REQUEST.search(text[: match.start()])
        ),
        None,
    )
    if request is None:
        return ""
    named = []
    for tracker, pattern in _NAMED_TRACKERS:
        mentions = [
            (_mention_gap(request.span(), mention.span()), mention.start())
            for mention in pattern.finditer(message)
        ]
        if mentions:
            gap, start = min(mentions)
            named.append((gap, start, tracker))
    return min(named)[2] if named else ""


def credential_blocked_issue_notice(
    tracker: str, reason: str, *, token_would_file: bool
) -> str:
    """The server's own answer when this account cannot file the asked-for issue.

    The caller stores this as a ``system`` chat row (the role the model never
    writes), above the model's answer, so the cause and the fix reach the user
    even when the model improvises around the system prompt's instruction — it
    claimed it would file, then blamed a "session-level tooling gap" instead of
    the account (issue #446).

    Only :data:`_CREDENTIAL_UNAVAILABLE_REASONS` produce text: a tracker an
    administrator disabled, a server with no repository or an unusable saved
    credential is not fixed in Profile Settings by adding a token. Returns ``""``
    for those, and for a caller whose tool list already has the tracker.

    Args:
        tracker: ``"github"`` or ``"jira"`` — the one the user asked for.
        reason: Its code from the chat tracker state.
        token_would_file: Would a token this account could save make it file?
            The gate reads the credential BEFORE the destination, so it also
            answers ``no_token`` on a deployment with no repository (or no Jira
            method) configured — a blocker no token fixes. Promising the token
            there sends the user to Profile Settings to fix nothing, so the
            notice stays silent instead.
    """
    if (
        not token_would_file
        or tracker not in {"github", "jira"}
        or reason not in _CREDENTIAL_UNAVAILABLE_REASONS
    ):
        return ""
    kind = "GitHub" if tracker == "github" else "Jira"
    if reason == "no_token":
        return (
            f"No {kind} issue can be filed from this chat. This account has no "
            f"{kind} token saved; the fix is {_TRACKER_TOKEN_FIX.format(kind=kind)}"
        )
    if reason == "no_token_no_profile":
        # The bootstrap ``admin`` has no ``users`` row, so ``PUT /api/user/tokens``
        # answers 404 for it: the fix is signing in as an account that has one.
        return (
            f"No {kind} issue can be filed from this chat. "
            + tracker_credential_message(kind, has_profile=False)
        )
    return ""


def _issue_repo_param(issue_repos: tuple[str, ...]) -> dict[str, Any]:
    """The ``repo`` argument of a GitHub issue tool, with its real limits.

    A job chat is capped at the run's own repos, so the model is told them;
    an admin chat gets the unbounded wording instead.
    """
    if issue_repos:
        allowed = ", ".join(issue_repos)
        text = (
            "GitHub repository to file in, as 'owner/name'. Must be this run's "
            f"test repo or one of its additional repos: {allowed}. Leave empty "
            "to use the test repo."
        )
    else:
        text = (
            "GitHub repository to file in, as 'owner/name'. Any repository may "
            "be named here. Leave empty to use the server's configured test "
            "repo."
        )
    return {"type": "string", "description": text}


def _issue_create_rules(
    unavailable: dict[str, str] | None = None,
    *,
    issue_repos: tuple[str, ...] = (),
    admin: bool = False,
) -> str:
    """The issue-creation rules, with the real reason each tracker is absent.

    A tracker can be missing for unrelated reasons: the server turned it off,
    it has no repository to file in, this account never saved that tracker's
    token, or the saved credential cannot be used with the server's settings.
    Only the credential cases are fixed in Profile Settings, so the prompt
    states which one applies instead of always pointing at the token.

    ``issue_repos`` names the repos a job chat may file in; ``admin`` says the
    caller is not capped by any run's repo set.
    """
    unavailable = unavailable or {}
    reasons = []
    for tracker, reason in sorted(unavailable.items()):
        kind = "GitHub" if tracker == "github" else "Jira"
        detail = _TRACKER_UNAVAILABLE_REASONS.get(
            reason, "this account has no usable {kind} credential saved."
        )
        reasons.append(f"  - {kind}: {detail.format(kind=kind)}")
    absent = (
        "\n".join(reasons)
        if reasons
        else "  - none: every tracker this chat can file in is in your tool list."
    )
    if admin:
        repo_rule = (
            "- You may name any GitHub repository (`owner/name`) as `repo`; "
            "leave it empty to use the server's configured test repo. The repo "
            "is part of the draft you propose, so the user approves where the "
            "issue goes."
        )
    elif issue_repos:
        repo_rule = (
            "- You may only file a GitHub issue in this run's repos, named as "
            f"`owner/name`: {', '.join(issue_repos)}. Leave `repo` empty to use "
            "the test repo. Any other repository is refused — do not promise it."
        )
    else:
        repo_rule = (
            "- You never pick the repository or Jira project: the server files "
            "the issue where it is configured."
        )
    return f"""
## Creating Issues
- You never file an issue on your own judgement: the user files it. A create call files nothing until the user has confirmed the draft in their own words, by replying in this chat with the confirmation code the preview showed them.
- Step 1: call `propose_tracker_issue` with the target, title and body. It files nothing and returns a `confirmation_id`, a `confirmation_code` and a preview naming the tracker, the destination (repository, or Jira project and issue type), the exact title and body, and that code.
- Step 2: the server posts the whole preview to this chat itself — where it would be filed, the proposal id, the title, the body and the code. Tell the user it is below and ask them to reply in this chat with the code. Do not rewrite the preview as if your own wording were what they are approving, and never ask for the code outside this chat.
- Step 3: only after the user's reply contains that code, call `create_github_issue` / `create_jira_issue` with that `confirmation_id`, that `confirmation_code` and the same title and body — copy them from the preview the server posted, never from memory. A create without them files nothing, a create the user has not confirmed fails with the step you skipped, and a paraphrased body is refused. The id and code are single-use and expire after 10 minutes, so propose again after any edit.
- Never send the code on the user's behalf and never ask for it anywhere but this chat: the server only accepts a code that arrived in a message from the user. Do not claim a draft is confirmed until such a reply exists.
- If the user declines, do not call the create tool — acknowledge and stop.
{repo_rule}
- A tracker with no tool in your tool list cannot be used. Say you cannot open that issue there and name the reason and the fix:
{absent}
- Never claim you can open an issue that is not in your tool list, never call another tool as a substitute, and do not send the user to Profile Settings for a tracker this server disabled or has no repository for.
- If a tool you do have refuses, relay it verbatim and name the fix it gives.
- Never claim an issue exists when the tool was not called or did not succeed."""


def build_system_prompt(
    job_name: str,
    build_number: int | str,
    job_id: str,
    custom_tools: list[dict[str, Any]],
    repos_available: bool = False,
    ci_build_data_available: bool = False,
    tracker_unavailable: dict[str, str] | None = None,
    issue_repos: tuple[str, ...] = (),
) -> str:
    """Build a system prompt that scopes the AI to a specific analyzed job.

    ``issue_repos`` are the repos this run may file a GitHub issue in, so the
    model reads them instead of guessing.
    """
    tools_section = _build_tools_section(custom_tools)
    unavailable_section = _build_unavailable_section(custom_tools)
    graph_note = ""
    if any(tool["name"].startswith("graft_") for tool in custom_tools):
        from rootcoz.engine.graft_http import graph_guidance

        graph_note = "\n\n" + graph_guidance()

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
{_issue_create_rules(tracker_unavailable, issue_repos=issue_repos)}

## Rules — STRICT
- You MUST only discuss this specific job and its failures
- If the user asks something unrelated to this job (e.g., "what's the time?", general coding questions, weather, anything not about this analysis), respond ONLY with: "I can only discuss the analysis results for this job. How can I help you understand the failures?"
- Do NOT answer off-topic questions. Do NOT be helpful about non-job topics. Reject them immediately.
{_COMMON_RULES}{graph_note}
"""


def build_admin_system_prompt(
    custom_tools: list[dict[str, Any]],
    tracker_unavailable: dict[str, str] | None = None,
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
{_issue_create_rules(tracker_unavailable, admin=True)}

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
    history: list[dict[str, Any]],
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
        elif role == "system":
            # The server's own consent/notice rows are not the model's words.
            parts.append(f"**System:** {content}\n")
        else:
            parts.append(f"**Assistant:** {content}\n")

    parts.append(f"**User:** {new_message}\n")
    parts.append("\n**Assistant:** ")

    return "\n".join(parts)


def safe_exception_frames(exc: BaseException) -> str:
    """Format traceback locations without credential-bearing exception messages."""
    frames = []
    tb = exc.__traceback__
    while tb:
        frames.append(
            f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno} in {tb.tb_frame.f_code.co_name}"
        )
        tb = tb.tb_next
    return "\n".join(frames)


async def _create_chat_session(
    *,
    system_prompt: str,
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    log_prefix: str = "Chat",
    custom_tools: list[dict[str, Any]] | None = None,
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
        sidecar_provider, sidecar_model = await resolve_catalog_pair(
            ai_provider, ai_model
        )
        from rootcoz.ai_client import _selected_credential_source, chat_session_source

        if (
            chat_session_source.get()
            and _selected_credential_source.get() != chat_session_source.get()
        ):
            raise ValueError("Chat credential source changed during Start")
        create_kwargs: dict[str, Any] = {
            "provider": sidecar_provider,
            "model": sidecar_model,
            "system_prompt": system_prompt,
            "cwd": str(repo_path) if repo_path else "/tmp",
        }
        if custom_tools:
            create_kwargs["custom_tools"] = custom_tools
        if restrict_tools:
            create_kwargs["tools"] = list(CHAT_BUILTIN_TOOLS)
        await install_http_tools_mcp_best_effort_async(repo_path, custom_tools or [])
        from rootcoz.ai_client import session_key

        key = (
            await session_key(sidecar_provider)
            if _selected_credential_source.get() == "user"
            else None
        )
        if _selected_credential_source.get() == "user" and key is None:
            raise ValueError("User AI credential unavailable; Start a new chat")
        if key is not None:
            create_kwargs["api_key"] = key
        else:
            from rootcoz.ai_client import (
                require_server_provider_grant,
                server_provider_key,
            )

            await require_server_provider_grant(sidecar_provider, sidecar_model)
            # A stored server provider key (#347) reaches chat sessions too:
            # pass it as the session apiKey while keeping the server
            # credential-source classification (create_session_safely records
            # server_api_key sessions as server-owned).
            server_key = await server_provider_key(sidecar_provider)
            if server_key is not None:
                create_kwargs["server_api_key"] = server_key
        session_id = await create_session_safely(client, **create_kwargs)
        logger.info("%s: session created: %s", log_prefix, session_id)
        return session_id
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "%s: failed to create session (%s)\n%s",
            log_prefix,
            type(exc).__name__,
            safe_exception_frames(exc),
        )
        return None


async def init_chat_session(
    *,
    job_id: str,
    job_name: str,
    build_number: int | str,
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    custom_tools: list[dict[str, Any]] | None = None,
    repos_available: bool = False,
    ci_build_data_available: bool = False,
    issue_repos: tuple[str, ...] = (),
    tracker_unavailable: dict[str, str] | None = None,
) -> str | None:
    """Initialize a chat session via the sidecar. Returns session_id or None."""
    system_prompt = build_system_prompt(
        job_name=job_name,
        build_number=build_number,
        job_id=job_id,
        custom_tools=custom_tools or [],
        repos_available=repos_available,
        ci_build_data_available=ci_build_data_available,
        issue_repos=issue_repos,
        tracker_unavailable=tracker_unavailable,
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
    custom_tools: list[dict[str, Any]] | None = None,
    tracker_unavailable: dict[str, str] | None = None,
) -> str | None:
    """Initialize an admin chat session via the sidecar. Returns session_id or None."""
    system_prompt = build_admin_system_prompt(
        custom_tools=custom_tools or [],
        tracker_unavailable=tracker_unavailable,
    )
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
    history: list[dict[str, Any]],
    ai_provider: str,
    ai_model: str,
    build_prompt_fn: Callable[[], str],
    repo_path: Path | None = None,
    ai_call_timeout: int | None = None,
    session_id: str | None = None,
    custom_tools: list[dict[str, Any]] | None = None,
    restrict_tools: bool = True,
    log_prefix: str = "Chat",
    request_id: str = "",
    call_type: str = "chat",
    install_mcp: bool = True,
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

    call_kwargs: dict[str, Any] = {
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
    if install_mcp:
        await install_http_tools_mcp_best_effort_async(repo_path, custom_tools or [])
    result = await call_ai(prompt, **call_kwargs)

    # A revoked or switched chat source must never be rebuilt under another identity.
    from rootcoz.ai_client import chat_session_source

    if (
        not result.success
        and session_id
        and "not found" in result.text.lower()
        and result.text != "AI session unavailable"
        and (
            not chat_session_source.get()
            or result.credential_source == chat_session_source.get()
        )
    ):
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
        retry_kwargs: dict[str, Any] = {
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
        if install_mcp:
            await install_http_tools_mcp_best_effort_async(
                repo_path, custom_tools or []
            )
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
    history: list[dict[str, Any]],
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    ai_call_timeout: int | None = None,
    session_id: str | None = None,
    custom_tools: list[dict[str, Any]] | None = None,
    repos_available: bool = False,
    ci_build_data_available: bool = False,
    install_mcp: bool = True,
    tracker_unavailable: dict[str, str] | None = None,
    issue_repos: tuple[str, ...] = (),
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
            tracker_unavailable=tracker_unavailable,
            issue_repos=issue_repos,
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
        install_mcp=install_mcp,
    )


async def admin_chat_with_ai(
    *,
    message: str,
    history: list[dict[str, Any]],
    ai_provider: str,
    ai_model: str,
    repo_path: Path | None = None,
    ai_call_timeout: int | None = None,
    session_id: str | None = None,
    custom_tools: list[dict[str, Any]] | None = None,
    tracker_unavailable: dict[str, str] | None = None,
) -> tuple[bool, str, str | None]:
    """Send an admin chat message and get an AI response."""

    def _build_prompt() -> str:
        return build_admin_system_prompt(
            custom_tools=custom_tools or [],
            tracker_unavailable=tracker_unavailable,
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
        log_prefix="Admin chat",
        request_id="__admin_chat__",
        call_type="admin_chat",
    )
