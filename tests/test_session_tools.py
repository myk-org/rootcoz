"""One builder decides every session's tools (#384) — proven without a container.

The bug that started this: analysis, peer analysis, chat and admin chat each
assembled tools by hand, so the code graph reached some paths and not others.
These tests pin the invariant that a single function produces the tool set, that
the graph adds exactly six read-only tools on top of the endpoint-specific ones,
and that the builtin lists are untouched.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from rootcoz.ai_client import ANALYSIS_BUILTIN_TOOLS, CHAT_BUILTIN_TOOLS
from rootcoz.engine import session_tools

HISTORY_TOOLS = [{"name": f"history_{index}"} for index in range(5)]
GRAPH_TOOLS = [
    {"name": name}
    for name in (
        "graft_find_code",
        "graft_find_all",
        "graft_file_api",
        "graft_trace_calls",
        "graft_check_freshness",
        "graft_repo_map",
    )
]


@pytest.fixture()
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Indexing and the MCP install are stubbed: this is about assembly only."""
    indexed: list[Path] = []
    installed: list[tuple[Path, list[dict]]] = []

    async def fake_index(workspace: Path) -> None:
        indexed.append(workspace)

    async def fake_install(workspace: Path, tools: list[dict]) -> None:
        installed.append((workspace, list(tools)))

    monkeypatch.setattr(session_tools, "index_workspace_graph", fake_index)
    monkeypatch.setattr(
        session_tools, "install_http_tools_mcp_best_effort_async", fake_install
    )
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.graph_http_tools",
        lambda workspace, job_id: list(GRAPH_TOOLS),
    )
    return indexed, installed


@pytest.mark.asyncio
async def test_graph_tools_are_added_on_top_of_endpoint_tools(
    stubbed, tmp_path: Path
) -> None:
    indexed, installed = stubbed
    tools = await session_tools.build_session_tools(
        builtin=list(ANALYSIS_BUILTIN_TOOLS),
        custom_base=list(HISTORY_TOOLS),
        workspace=tmp_path,
        job_id="job-1",
    )

    names = [tool["name"] for tool in tools.custom]
    assert [tool["name"] for tool in HISTORY_TOOLS] == names[: len(HISTORY_TOOLS)]
    assert set(GRAPH_TOOLS[0].keys())  # shape sanity
    assert {tool["name"] for tool in GRAPH_TOOLS} <= set(names)
    assert len(tools.custom) == len(HISTORY_TOOLS) + len(GRAPH_TOOLS)
    assert indexed == [tmp_path]
    assert installed and installed[0][1] == tools.custom


@pytest.mark.asyncio
async def test_builtins_are_not_widened(stubbed, tmp_path: Path) -> None:
    tools = await session_tools.build_session_tools(
        builtin=list(ANALYSIS_BUILTIN_TOOLS),
        custom_base=list(HISTORY_TOOLS),
        workspace=tmp_path,
        job_id="job-1",
    )
    assert tools.builtin == list(ANALYSIS_BUILTIN_TOOLS)
    assert "bash" not in tools.builtin
    assert "subagent" not in tools.builtin
    # The graph arrives as custom tools, never as builtins.
    assert not any("graft_" in name for name in tools.builtin)


@pytest.mark.asyncio
async def test_graph_workspace_is_the_clone_parent(stubbed, tmp_path: Path) -> None:
    """A session cwd that is not the clone workspace still indexes the clones."""
    workspace = tmp_path / "workspace"
    clone = workspace / "repo"
    clone.mkdir(parents=True)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    tools = await session_tools.build_session_tools(
        builtin=list(ANALYSIS_BUILTIN_TOOLS),
        custom_base=[],
        workspace=artifacts,
        job_id="job-1",
        repos={"repo": clone},
    )
    assert tools.graph_root == workspace


@pytest.mark.asyncio
async def test_chat_keeps_its_builtins_and_gets_the_graph(
    stubbed, tmp_path: Path
) -> None:
    tools = await session_tools.build_session_tools(
        builtin=list(CHAT_BUILTIN_TOOLS),
        custom_base=[{"name": "chat_tool"}],
        workspace=tmp_path,
        job_id="job-1",
    )
    assert tools.builtin == list(CHAT_BUILTIN_TOOLS)
    assert {"chat_tool"} | {tool["name"] for tool in GRAPH_TOOLS} == {
        tool["name"] for tool in tools.custom
    }


@pytest.mark.asyncio
async def test_graph_can_be_disabled(stubbed, tmp_path: Path) -> None:
    indexed, _ = stubbed
    tools = await session_tools.build_session_tools(
        builtin=list(ANALYSIS_BUILTIN_TOOLS),
        custom_base=list(HISTORY_TOOLS),
        workspace=tmp_path,
        job_id="job-1",
        graph=False,
    )
    assert tools.custom == HISTORY_TOOLS
    assert tools.graph_root is None
    assert indexed == []


def test_every_session_kind_uses_the_one_builder() -> None:
    """The invariant: no endpoint assembles tools by hand any more."""
    from rootcoz import main, peer_analysis
    from rootcoz.engine import core

    for module in (core, peer_analysis, main):
        source = inspect.getsource(module)
        assert "build_session_tools(" in source, module.__name__

    # The graph is reached only through the builder: no module outside it turns
    # graft_http's tools into a session's tool list on its own.
    for module in (core, peer_analysis, main):
        source = inspect.getsource(module)
        assert "graph_http_tools" not in source, module.__name__


def test_fixture_graph_payloads_are_json() -> None:
    """Guard the captured real-output fixtures the shape tests rely on."""
    fixtures = Path(__file__).parent / "fixtures" / "graft"
    for path in sorted(fixtures.glob("*.json")):
        json.loads(path.read_text())


def test_graph_hint_only_when_graph_tools_are_present() -> None:
    """The prompt must advertise the graph exactly when the session has it."""
    with_graph = session_tools.SessionTools(
        builtin=["read"],
        custom=[{"name": "graft_find_code"}],
        graph_root=None,
    )
    without = session_tools.SessionTools(builtin=["read"], custom=[{"name": "x"}])

    assert "graft_find_code" in session_tools.graph_prompt_hint(with_graph)
    assert session_tools.graph_prompt_hint(without) == ""


@pytest.mark.asyncio
async def test_skill_is_installed_where_the_session_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chat installed the skill in the clone workspace; the session runs elsewhere.

    Analysis sessions run in their artifacts dir, so a skill written only into
    the clone workspace is never discovered — the model then had the tools and
    made zero calls.
    """
    installed: list[Path] = []
    clone_parent = tmp_path / "workspace"
    clone_parent.mkdir()
    session_cwd = tmp_path / "artifacts"
    session_cwd.mkdir()

    async def fake_index(workspace: Path) -> None:
        return None

    monkeypatch.setattr(session_tools, "index_workspace_graph", fake_index)
    monkeypatch.setattr(
        session_tools, "install_http_tools_mcp_best_effort_async", AsyncMock()
    )
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.graph_http_tools",
        lambda workspace, job_id: list(GRAPH_TOOLS),
    )
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.install_graph_skill",
        lambda workspace: installed.append(workspace) or True,
    )

    await session_tools.build_session_tools(
        builtin=list(ANALYSIS_BUILTIN_TOOLS),
        custom_base=[],
        workspace=session_cwd,
        job_id="job-1",
    )
    assert installed == [session_cwd]
