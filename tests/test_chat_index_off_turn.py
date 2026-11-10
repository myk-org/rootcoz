"""A chat turn never waits for the code graph.

Indexing twelve clones took 132 s before the first answer, against 5 s for the
model call that followed, and nine of those graphs were then discarded by the
workspace budget. The graph is an optimisation every session already degrades
without, so chat starts the build after it has answered and picks the graphs up
on the next turn.
"""

import asyncio
import inspect

import pytest

from rootcoz import main
from rootcoz.engine import session_tools


def _quiet(monkeypatch) -> None:
    """Silence the two real side effects: no MCP install, no graph server."""

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(session_tools, "install_http_tools_mcp_best_effort_async", noop)
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.graph_http_tools", lambda *_a, **_k: []
    )


@pytest.mark.asyncio
async def test_a_turn_answers_while_indexing_is_still_running(tmp_path, monkeypatch):
    """The tool build returns with the build in flight, not after it."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_index(*_args, **_kwargs):
        started.set()
        await release.wait()

    _quiet(monkeypatch)
    monkeypatch.setattr(session_tools, "index_workspace_graph", slow_index)

    session_tools.schedule_workspace_index(tmp_path, job_id="job")
    await asyncio.wait_for(started.wait(), 5)

    tools = await asyncio.wait_for(
        session_tools.build_session_tools(
            builtin=["read"],
            custom_base=[],
            workspace=tmp_path,
            job_id="job",
            wait_for_index=False,
        ),
        5,
    )

    assert not release.is_set(), "the turn waited for the index"
    assert tools.custom == []

    release.set()
    await asyncio.sleep(0.01)


def test_both_chat_paths_schedule_the_index_instead_of_awaiting_it():
    """Nothing can block on the graph here: the entry point is not awaitable."""
    assert not inspect.iscoroutinefunction(main._schedule_chat_index)
    for path in (main._init_chat_under_barrier, main._process_chat_message):
        assert "_schedule_chat_index" in inspect.getsource(path)
    message_turn = inspect.getsource(main._process_chat_message)
    assert message_turn.index("chat_with_ai(") < message_turn.index(
        "_schedule_chat_index("
    ), "the build takes the workspace lock the turn's own install needs"


def test_an_unchanged_head_is_not_rebuilt(tmp_path, monkeypatch):
    """Same content, same graph: the second pass over a workspace builds nothing.

    The manifests are content hashes, so this holds for a re-index for free; what
    it does not cover is a *new* workspace for the same clones, where the graph is
    rebuilt because graphs live under their workspace.
    """
    import subprocess
    from pathlib import Path

    from rootcoz.engine import graft

    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.py").write_text("pass")
    builds: list[str] = []

    def run(argv, **_kwargs):
        builds.append(argv[0])
        graph = Path(argv[2])
        graph.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", run)

    assert graft.index_repositories(workspace, {"repo": repo})["repo"]["status"] == (
        "indexed"
    )
    second = graft.index_repositories(workspace, {"repo": repo})["repo"]

    assert second == {"status": "unchanged", "files": 1}
    assert len(builds) == 1, "rebuilt a graph whose sources did not change"
