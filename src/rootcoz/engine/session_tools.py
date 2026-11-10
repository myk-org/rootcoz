"""One place that decides how an AI session gets its tools.

Before this module, four call sites hand-copied the same four steps — pick the
builtin list, build the endpoint-specific tools, add the read-only code graph,
install the CLI MCP config — and each copy drifted. The graph reached chat and
non-peer analysis but not peer analysis, which is why a repo with
``peer_ai_configs`` got no map at all.

Every analysis, peer, chat and admin-chat session now calls
:func:`build_session_tools`, so adding a tool is a one-line change that reaches
every path by construction.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rootcoz import get_child_logger
from rootcoz.engine.http_mcp import install_http_tools_mcp_best_effort_async

logger = get_child_logger(__name__)


@dataclass(frozen=True)
class SessionTools:
    """What a session may call: builtins, custom tools, and where the map came from."""

    builtin: list[str]
    custom: list[dict[str, Any]] = field(default_factory=list)
    graph_root: Path | None = None


#: One in-flight indexing task per workspace. Analysis calls the AI once per
#: failure group (and peer sessions do the same concurrently), so a second
#: caller waits for the build instead of skipping it and seeing no graph.
_GRAPH_INDEX_TASKS: dict[str, asyncio.Task[dict[str, object] | None]] = {}

#: Fire-and-forget index runs, held so the loop does not collect them mid-build.
_BACKGROUND_INDEX_TASKS: set[asyncio.Task[None]] = set()

#: How long a turn that is not waiting for its graph waits for the workspace
#: lock a build holds. The graphs on disk are read in milliseconds; anything
#: slower means a build owns the workspace, and the turn answers without them.
GRAPH_TOOL_WAIT_SECONDS = 2.0


def schedule_workspace_index(
    workspace: Path,
    job_id: str = "",
    repos: dict[str, Path] | None = None,
    *,
    deadline: float | None = None,
) -> None:
    """Start a workspace's graph build in the background; never wait for it.

    Chat indexes after it has answered the user. Waiting turned a 5 s model call
    into a 137 s turn: twelve clones were indexed first, nine of them refused by
    the budget after the build, and every message behind it queued on the same
    workspace lock. The graphs this produces reach the next turn's tool build,
    which does not wait either, and a session that gains them is started fresh
    (a resumed session keeps the tools it was created with).
    """
    task = asyncio.create_task(
        index_workspace_graph(workspace, job_id=job_id, repos=repos, deadline=deadline)
    )
    _BACKGROUND_INDEX_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_INDEX_TASKS.discard)


def graph_workspace_for(repos: dict[str, Path] | None) -> Path | None:
    """Directory holding the side-by-side clones, or ``None`` when there are none.

    A session's cwd is not necessarily the clone workspace (a child analysis
    runs in its artifacts directory), so the graph is indexed from the common
    parent of the clones instead.
    """
    if not repos:
        return None
    return next(iter(repos.values())).parent


async def index_workspace_graph(
    workspace: Path,
    job_id: str = "",
    repos: dict[str, Path] | None = None,
    *,
    deadline: float | None = None,
) -> None:
    """Index a workspace's clones once, waiting for an in-flight build.

    Only missing or stale graphs are built, and any failure is a warning: the
    graph is an optimisation, never a gate on the analysis. ``deadline`` is the
    caller's index clock; a joining caller waits for the build the creator
    bounded, so the deadline belongs to whoever started it.
    """
    key = str(Path(workspace).resolve())
    task = _GRAPH_INDEX_TASKS.get(key)
    if task is None or task.done():
        # A finished task must not be cached forever: a chat workspace is reused
        # across messages, so new clones, cleaned graphs and expired budget
        # markers have to be picked up on the next message.
        task = asyncio.create_task(
            _index_workspace_graph_once(Path(workspace), repos=repos, deadline=deadline)
        )
        _GRAPH_INDEX_TASKS[key] = task
    # The outcome is logged here, per caller: a session that joined an existing
    # task still needs its own attributable record, and the job id must be the
    # caller's, not the one that happened to start the build.
    outcomes = await asyncio.shield(task)
    if outcomes:
        from rootcoz.engine.graft import log_index_outcomes

        log_index_outcomes(outcomes, job_id=job_id)


async def _index_workspace_graph_once(
    workspace: Path,
    *,
    repos: dict[str, Path] | None = None,
    deadline: float | None = None,
) -> dict[str, object] | None:
    from rootcoz.engine.graft import (
        index_repositories,
        roots_needing_index,
    )
    from rootcoz.engine.graft_http import cloned_graph_roots

    logger.info("graph indexing: considering %s", workspace)
    try:
        # A caller that already knows which clones are stale passes them, so the
        # workspace is not re-enumerated and re-hashed here.
        roots = (
            dict(repos)
            if repos
            else await asyncio.to_thread(cloned_graph_roots, workspace)
        )
        if not roots:
            # Say it out loud: a silent skip looks exactly like "the graph
            # worked and the model chose not to use it".
            logger.info(
                "graph indexing: no Git clones under %s — session runs without a map",
                workspace,
            )
            return None
        pending = await asyncio.to_thread(
            roots_needing_index, workspace, roots, deadline=deadline
        )
        if not pending:
            withheld = await asyncio.to_thread(_budget_withheld, workspace, roots)
            if withheld:
                # "already current" was a lie: these are deliberately withheld by
                # a live budget marker, and no usable graph exists for them.
                logger.warning(
                    "graph indexing: %d repo(s) withheld by the %s in %s: %s",
                    len(withheld),
                    ", ".join(sorted(set(withheld.values()))),
                    workspace,
                    ", ".join(sorted(withheld)),
                )
                return None
            logger.info(
                "graph indexing: graphs already current for %s (%d repo(s))",
                workspace,
                len(roots),
            )
            return None
        logger.info(
            "graph indexing: building %d of %d repo(s): %s",
            len(pending),
            len(roots),
            ", ".join(sorted(pending)),
        )
        return await asyncio.to_thread(
            index_repositories, workspace, pending, deadline=deadline
        )
    except Exception:
        logger.warning("Graph indexing skipped for %s", workspace, exc_info=True)
        return None


async def build_session_tools(
    *,
    builtin: list[str],
    custom_base: list[dict[str, Any]] | None,
    workspace: Path,
    job_id: str,
    repos: dict[str, Path] | None = None,
    graph: bool = True,
    index_deadline: float | None = None,
    wait_for_index: bool = True,
) -> SessionTools:
    """Assemble every tool a session gets, and install the CLI MCP config.

    Args:
        builtin: Builtin tool names for this session kind.
        custom_base: Endpoint-specific tools (history/classify, chat tools, …).
        workspace: The session's working directory (used for the MCP install).
        job_id: Analysis job id; the graph server is scoped to it.
        repos: Clone mapping when the caller knows it. Falls back to the
            workspace itself, which is where chat keeps its clones.
        graph: ``False`` disables the code graph entirely (tests, small jobs).
        index_deadline: The caller's index clock, used only when this call does
            the indexing. Analysis passes ``None`` and stays unbounded.
        wait_for_index: ``True`` builds the graph first and offers what it
            built (analysis). ``False`` offers the graphs that already exist and
            returns at once; chat schedules the build itself, off the turn.

    Returns:
        ``SessionTools`` with the builtin list, the combined custom tools, and
        the directory the graph was taken from (``None`` when no map).
    """
    custom = list(custom_base or [])
    graph_root: Path | None = None
    if graph:
        graph_root = graph_workspace_for(repos) or Path(workspace)
        if wait_for_index:
            await index_workspace_graph(
                graph_root, job_id=job_id, deadline=index_deadline
            )
        from rootcoz.engine.graft_http import graph_http_tools

        graph_tools = graph_http_tools(
            graph_root,
            job_id,
            deadline=None
            if wait_for_index
            else time.monotonic() + GRAPH_TOOL_WAIT_SECONDS,
        )
        if not graph_tools and wait_for_index:
            # A job with clones but no graph is a defect, not a quiet default:
            # the container ships the indexer, so silence here is what made
            # "tools never offered" indistinguishable from "model ignored them".
            from rootcoz.engine.graft_http import cloned_graph_roots

            if await asyncio.to_thread(cloned_graph_roots, graph_root):
                logger.warning(
                    "Code graph expected but unavailable for %s — clones present,"
                    " no graph registered; analysis runs without a map",
                    graph_root,
                )
        elif not graph_tools:
            logger.info(
                "No code graph ready for %s — chat builds it off the turn, so this"
                " session starts without a map",
                graph_root,
            )
        if graph_tools:
            # register_workspace installs the skill under the clone workspace,
            # but a session runs in its own cwd (often an artifacts dir), where
            # pi looks for skills. Install it there too or the model never sees
            # the guidance and never calls the tools.
            from rootcoz.engine.graft_http import install_graph_skill

            install_graph_skill(Path(workspace))
            logger.info(
                "Session gets %d code-graph tool(s) from %s",
                len(graph_tools),
                graph_root,
            )
        custom.extend(graph_tools)
    await install_http_tools_mcp_best_effort_async(Path(workspace), custom)
    return SessionTools(builtin=list(builtin), custom=custom, graph_root=graph_root)


#: Appended to a prompt that has graph tools, because a tool nobody mentions is
#: a tool nobody calls: the session had all six and made zero queries.
GRAPH_PROMPT_HINT = """
=== CODE GRAPH (read-only, MANDATORY FIRST STEP FOR CODE QUESTIONS) ===
This workspace is indexed. Before you use grep/read to understand code, you MUST
query the graph — it answers "where is this, who calls it, what does it look like"
from an index instead of reading files one by one:
- graft_repo_map      orient yourself in a repository
- graft_find_code     where a behaviour lives (ranked; pass the symptom keywords)
- graft_find_all      every indexed match for a pattern
- graft_file_api      a file's signatures without full bodies
- graft_trace_calls   callers/callees of a symbol
- graft_check_freshness  is the graph current?
Pass the repository name as `repo`. Only after a graph answer (or a stale/partial
result that tells you to) use read/grep for the specific files it named.
"""


def graph_prompt_hint(tools: SessionTools) -> str:
    """The graph section for a prompt, or empty when no graph tools are present."""
    if any(tool.get("name", "").startswith("graft_") for tool in tools.custom):
        return GRAPH_PROMPT_HINT
    return ""


def _budget_withheld(workspace: Path, roots: dict[str, Path]) -> dict[str, str]:
    """Repos whose graph is withheld, mapped to the reason their marker names."""
    from rootcoz.engine.graft import _root, budget_marker_reason

    graph_root = _root(Path(workspace))
    withheld: dict[str, str] = {}
    for name in roots:
        reason = budget_marker_reason(graph_root / name)
        if reason is not None:
            withheld[name] = reason
    return withheld
