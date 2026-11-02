"""The code graph serves analysis too (#384).

Analysis ran on exactly four builtin tools with no map, so the workload that
reads the most code was the one without one. These tests pin the wiring: the
graph arrives as read-only *custom* tools beside the unchanged builtins, one
builder feeds chat and analysis alike, indexing happens once per workspace, and
a workspace with no graph is simply left alone.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from rootcoz.ai_client import ANALYSIS_BUILTIN_TOOLS
from rootcoz.engine import core, graft_http, session_tools
from rootcoz.models import FailedTest

GRAPH_TOOLS = [
    {"name": name, "description": "graph", "parameters": {}}
    for name in (
        "graft_find_code",
        "graft_find_all",
        "graft_file_api",
        "graft_trace_calls",
        "graft_check_freshness",
        "graft_repo_map",
    )
]


def _analysis_result() -> str:
    return json.dumps({"classification": "CODE ISSUE", "details": "test"})


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch):
    """Run one analysis and capture the kwargs the AI session receives."""
    seen: dict[str, object] = {}

    async def fake_call_ai_once(prompt, **kwargs):
        seen.update(kwargs)
        return core.AIResult(success=True, text=_analysis_result())

    monkeypatch.setattr(core, "call_ai_once", fake_call_ai_once)
    monkeypatch.setattr(core, "update_progress_phase", AsyncMock())

    async def run(tmp_path: Path) -> dict[str, object]:
        await core.run_single_ai_analysis(
            failures=[
                FailedTest(
                    test_name="test_example",
                    error_message="AssertionError",
                    stack_trace="trace",
                )
            ],
            console_context="console output",
            repo_path=tmp_path,
            ai_provider="gemini",
            ai_model="pro",
            ai_call_timeout=None,
            custom_prompt="",
            artifacts_context="",
            server_url="",
            job_id="job-1",
        )
        return seen

    return run


@pytest.mark.asyncio
async def test_analysis_session_receives_the_graph_tools(
    monkeypatch: pytest.MonkeyPatch, captured, tmp_path: Path
) -> None:
    """The six read-only graph tools reach analysis as custom tools."""
    monkeypatch.setattr(
        graft_http, "graph_http_tools", lambda workspace, job_id: list(GRAPH_TOOLS)
    )
    seen = await captured(tmp_path)

    custom = seen.get("custom_tools") or []
    names = {tool["name"] for tool in custom if isinstance(tool, dict)}
    assert {tool["name"] for tool in GRAPH_TOOLS} <= names


@pytest.mark.asyncio
async def test_builtin_tools_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, captured, tmp_path: Path
) -> None:
    """The graph never widens the builtin list — no bash, no subagent."""
    monkeypatch.setattr(
        graft_http, "graph_http_tools", lambda workspace, job_id: list(GRAPH_TOOLS)
    )
    seen = await captured(tmp_path)

    assert seen.get("tools") == list(ANALYSIS_BUILTIN_TOOLS)
    assert "bash" not in ANALYSIS_BUILTIN_TOOLS
    assert "subagent" not in ANALYSIS_BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_workspace_without_a_graph_gets_no_graph_tools(captured, tmp_path):
    """No indexed clone → no tools, and analysis still runs."""
    seen = await captured(tmp_path)
    assert not seen.get("custom_tools")


def test_chat_and_analysis_share_one_builder() -> None:
    """One builder, so the two tool lists cannot drift apart."""
    from rootcoz import main, peer_analysis

    for module in (core, peer_analysis, main):
        assert "build_session_tools(" in inspect.getsource(module), module.__name__


@pytest.mark.asyncio
async def test_indexing_happens_once_per_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Analysis calls the AI per failure group; indexing must not repeat."""
    calls: list[object] = []

    monkeypatch.setattr(
        graft_http, "cloned_graph_roots", lambda workspace: {"code": tmp_path / "code"}
    )
    monkeypatch.setattr(
        "rootcoz.engine.graft.roots_needing_index",
        lambda workspace, roots: dict(roots),
    )
    monkeypatch.setattr(
        "rootcoz.engine.graft.index_repositories",
        lambda workspace, roots: calls.append(roots) or {},
    )
    monkeypatch.setattr("rootcoz.engine.graft.log_index_outcomes", lambda outcome: None)

    session_tools._GRAPH_INDEX_TASKS.clear()

    # Concurrent callers (one AI call per failure group, peers alongside) share a
    # single build.
    await asyncio.gather(
        session_tools.index_workspace_graph(tmp_path),
        session_tools.index_workspace_graph(tmp_path),
    )
    assert len(calls) == 1, calls

    # A finished build is not cached forever: a reused chat workspace must pick up
    # new clones, cleaned graphs and expired budget markers.
    await session_tools.index_workspace_graph(tmp_path)
    assert len(calls) == 2, calls


@pytest.mark.asyncio
async def test_indexing_failure_never_breaks_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken graph is a warning, not a failed analysis."""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("index exploded")

    monkeypatch.setattr(graft_http, "cloned_graph_roots", boom)
    session_tools._GRAPH_INDEX_TASKS.clear()

    await session_tools.index_workspace_graph(tmp_path)  # must not raise


@pytest.mark.asyncio
async def test_analysis_prompt_advertises_the_graph(
    monkeypatch: pytest.MonkeyPatch, captured, tmp_path: Path
) -> None:
    """The prompt must name the tools: the first live run had all six and made
    zero calls, because nothing told the model they existed."""
    prompts: list[str] = []

    async def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        return core.AIResult(
            success=True, text=json.dumps({"classification": "CODE ISSUE"})
        )

    monkeypatch.setattr(core, "call_ai_once", fake_call)
    monkeypatch.setattr(core, "update_progress_phase", AsyncMock())
    monkeypatch.setattr(
        graft_http, "graph_http_tools", lambda workspace, job_id: list(GRAPH_TOOLS)
    )
    monkeypatch.setattr(session_tools, "index_workspace_graph", AsyncMock())
    monkeypatch.setattr(
        session_tools, "install_http_tools_mcp_best_effort_async", AsyncMock()
    )

    await core.run_single_ai_analysis(
        failures=[
            FailedTest(test_name="test_x", error_message="boom", stack_trace="trace")
        ],
        console_context="out",
        repo_path=tmp_path,
        ai_provider="gemini",
        ai_model="pro",
        ai_call_timeout=None,
        custom_prompt="",
        artifacts_context="",
        server_url="",
        job_id="job-1",
    )
    assert prompts, "the AI was not called"
    assert "CODE GRAPH" in prompts[0]
    assert "graft_find_code" in prompts[0]
