"""Indexing is deadline-bounded at every stage and degrades instead of gating (#511).

The deadline belongs to the chat index step: past it no repository work starts,
no admitted repository starts another stage, and what was refused leaves a
backoff marker so the next session does not rebuild it.
"""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from rootcoz.engine import graft


def _repo(workspace: Path, name: str = "repo", files: dict[str, bytes] | None = None):
    repo = workspace / name
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    payload = files or {"a.py": b"a", "b.py": b"b"}
    manifest = {}
    for path, content in payload.items():
        (repo / path).write_bytes(content)
        manifest[path] = hashlib.sha256(content).hexdigest()
    return repo, manifest


def _built_graph(workspace: Path, name: str = "repo") -> Path:
    """A graph directory that ``_manifest`` accepts as complete."""
    destination = graft._root(workspace) / name
    (destination / "graph").mkdir(parents=True)
    (destination / "snapshot").mkdir(parents=True)
    (destination / "manifest.json").write_text(
        json.dumps({"a.py": "0" * 64, "b.py": "1" * 64})
    )
    return destination


def _marker(destination: Path) -> dict[str, object]:
    return json.loads((destination / "budget.json").read_text())


#: A deadline far enough ahead that the workspace lock can be taken under it.
NOT_YET = 3600.0


def _later() -> float:
    return time.monotonic() + NOT_YET


def _spent_clock(monkeypatch) -> dict[str, bool]:
    """Report the index clock as spent, and return the switch that unsets it.

    A wall-clock deadline in the past is spent *before* the lock is asked for, and
    the lock file is shared by every process on the machine: a sibling test holding
    the directory gate for a moment would answer ``TimeoutError`` instead of the
    outcome under test. ``_deadline_fired`` is the one classifier the code asks.
    """
    spent = {"on": True}
    monkeypatch.setattr(
        graft, "_deadline_fired", lambda deadline: spent["on"] and deadline is not None
    )
    return spent


def test_no_repo_is_admitted_after_the_deadline(tmp_path, monkeypatch):
    """A spent deadline refuses at admission and says so in the outcome."""
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)
    _spent_clock(monkeypatch)
    deadline = _later()

    def fake_run(*_args, **_kwargs):
        raise AssertionError("a build started after the deadline")

    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(manifest))
    monkeypatch.setattr(graft, "_run", fake_run)
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)

    outcomes = graft.index_repositories(workspace, {"repo": repo}, deadline=deadline)

    assert outcomes["repo"] == {"status": "skipped", "reason": "deadline"}
    marker = _marker(graft._root(workspace) / "repo")
    assert marker["reason"] == "deadline"
    assert marker["retry_after"] > time.time()


def test_deadline_reaches_every_stage(tmp_path, monkeypatch):
    """Both hashes, the copy loop, the build and the funnel get the same clock."""
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)
    deadline = time.monotonic() + 100
    hashed: list[float | None] = []

    def fake_sources(_path, deadline=None):
        hashed.append(deadline)
        return dict(manifest)

    ran: dict[str, float] = {}

    def fake_run(argv, *, cwd, timeout, **_kwargs):
        ran["timeout"] = timeout
        (Path(cwd).parent / "graph").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    remaining: list[float] = []

    def fake_remaining(value):
        remaining.append(value)
        return 42.0

    monkeypatch.setattr(graft, "_sources", fake_sources)
    monkeypatch.setattr(graft, "_run", fake_run)
    monkeypatch.setattr(graft, "_remaining", fake_remaining)
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)
    monkeypatch.setattr(graft, "max_workspace_graph_bytes", lambda: 10**9)

    outcomes = graft.index_repositories(workspace, {"repo": repo}, deadline=deadline)

    assert outcomes["repo"]["status"] == "indexed", outcomes
    assert len(hashed) == 2, "the pre-build and the post-build hash both got it"
    assert set(hashed) == {deadline}
    assert ran["timeout"] == 42.0, "the build is capped by what is left"
    # The streamed copy checks the clock once per file, plus the build cap.
    assert remaining == [deadline, deadline, deadline], remaining

    # The funnel hashes with the same clock.
    hashed.clear()
    graft.roots_needing_index(workspace, {"repo": repo}, deadline=deadline)
    assert hashed == [deadline]


def test_deadline_is_not_a_failure(tmp_path, monkeypatch):
    """The clock decides: past it is a skip, before it a plain TimeoutError fails."""
    workspace = tmp_path / "workspace"
    repo, _manifest_hashes = _repo(workspace)
    destination = graft._root(workspace) / "repo"

    def aborting_sources(_path, deadline=None):
        raise TimeoutError("source scan timed out")

    monkeypatch.setattr(graft, "_sources", aborting_sources)
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)

    spent = _spent_clock(monkeypatch)
    past = graft.index_repositories(workspace, {"repo": repo}, deadline=_later())
    assert past["repo"] == {"status": "skipped", "reason": "deadline"}
    assert _marker(destination)["reason"] == "deadline"
    assert not (destination / "failed.json").exists(), "a skip is not a failure"

    spent["on"] = False
    future = graft.index_repositories(
        workspace, {"repo": repo}, deadline=time.monotonic() + 60
    )
    assert future["repo"]["status"] == "failed"
    assert future["repo"]["reason"] == "TimeoutError"
    assert (destination / "failed.json").exists()
    assert not (destination / "budget.json").exists()


def test_a_lock_wait_is_bounded_by_the_deadline(tmp_path):
    """The clock bounds the wait for the workspace lock, not only the work.

    A preparation that queues behind a held workspace lock used to wait there
    with no clock at all, so the deadline never got to refuse anything.
    """
    from rootcoz.engine import http_mcp

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _repo(workspace)

    with http_mcp._workspace_install_lock(workspace):
        for call in (
            lambda deadline: graft.roots_needing_index(
                workspace, {}, deadline=deadline
            ),
            lambda deadline: graft.index_repositories(workspace, {}, deadline=deadline),
        ):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                call(time.monotonic() + 0.2)
            assert time.monotonic() - started < 5, "the wait outlived its deadline"


def test_a_deadline_never_deletes_a_graph_that_was_already_there(tmp_path, monkeypatch):
    """A spent clock skips the repository, it does not destroy its working graph."""
    workspace = tmp_path / "workspace"
    repo, _hashes = _repo(workspace)
    destination = _built_graph(workspace)

    def timed_out(_path, deadline=None):
        raise TimeoutError("source scan timed out")

    monkeypatch.setattr(graft, "_sources", timed_out)
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)
    _spent_clock(monkeypatch)

    outcomes = graft.index_repositories(workspace, {"repo": repo}, deadline=_later())

    assert outcomes["repo"] == {"status": "skipped", "reason": "deadline"}
    assert (destination / "manifest.json").exists(), "an intact graph was deleted"
    assert not (destination / "budget.json").exists(), "a usable graph was withheld"


def test_a_symlinked_marker_is_never_written(tmp_path, monkeypatch):
    """The readers refuse symlinked markers; the writers match them."""
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)
    destination = graft._root(workspace) / "repo"
    destination.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("untouched")
    (destination / "budget.json").symlink_to(outside)

    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(manifest))
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)
    _spent_clock(monkeypatch)
    graft.index_repositories(workspace, {"repo": repo}, deadline=_later())

    assert outside.read_text() == "untouched"


def test_an_over_budget_marker_is_not_written_at_admission(tmp_path, monkeypatch):
    """The admission guard stays: a marker that overflows the budget is skipped."""
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)

    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(manifest))
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)
    monkeypatch.setattr(graft, "max_workspace_graph_bytes", lambda: 1)
    _spent_clock(monkeypatch)

    graft.index_repositories(workspace, {"repo": repo}, deadline=_later())

    assert not (graft._root(workspace) / "repo" / "budget.json").exists()


def test_all_three_marker_writers_name_a_reason(tmp_path, monkeypatch):
    """One writer for the admission, deadline and post-build-drop markers."""
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)
    written: list[tuple[Path, str]] = []

    def record(destination, used, reason, **kwargs):
        written.append((Path(destination), reason))

    monkeypatch.setattr(graft, "_write_budget_marker", record)
    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(manifest))
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)

    # admission refusal (budget)
    monkeypatch.setattr(graft, "max_workspace_graph_bytes", lambda: 0)
    graft.index_repositories(workspace, {"repo": repo})
    # admission refusal (deadline)
    monkeypatch.setattr(graft, "max_workspace_graph_bytes", lambda: 10**9)
    _spent_clock(monkeypatch)
    graft.index_repositories(workspace, {"repo": repo}, deadline=_later())

    # post-build drop
    def fake_run(argv, *, cwd, **_kwargs):
        (Path(cwd).parent / "graph").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", fake_run)
    monkeypatch.setattr(graft, "_workspace_graph_size", lambda _root: 10**9)
    grant = graft.index_repositories(workspace, {"repo": repo})
    assert grant["repo"] == {"status": "skipped", "reason": "workspace graph budget"}

    assert [reason for _d, reason in written] == [
        "workspace graph budget",
        "deadline",
        "workspace graph budget",
    ]
    assert all(isinstance(d, Path) for d, _r in written)


def test_deadline_marker_is_respected(tmp_path, monkeypatch):
    """The funnel backs off while the marker is live and retries after it."""
    workspace = tmp_path / "workspace"
    repo, _hashes = _repo(workspace)
    destination = _built_graph(workspace)
    stale = {"a.py": "f" * 64}
    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(stale))

    (destination / "budget.json").write_text(
        json.dumps(
            {
                "budget_used": 10,
                "retry_after": time.time() + 300,
                "reason": "deadline",
            }
        )
    )
    assert graft.roots_needing_index(workspace, {"repo": repo}) == {}

    (destination / "budget.json").write_text(
        json.dumps(
            {"budget_used": 10, "retry_after": time.time() - 1, "reason": "deadline"}
        )
    )
    assert graft.roots_needing_index(workspace, {"repo": repo}) == {"repo": repo}


def test_marker_survives_a_new_session(tmp_path, monkeypatch):
    """Two consecutive preparations: the second builds nothing."""
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)
    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(manifest))
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)

    _spent_clock(monkeypatch)
    first = graft.index_repositories(workspace, {"repo": repo}, deadline=_later())
    assert first["repo"] == {"status": "skipped", "reason": "deadline"}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("rebuilt a repository the marker withheld")

    monkeypatch.setattr(graft, "_run", forbidden)
    pending = graft.roots_needing_index(workspace, {"repo": repo})
    assert pending == {}
    assert graft.index_repositories(workspace, pending) == {}


def test_a_reached_and_built_repo_is_never_marked(tmp_path, monkeypatch):
    """The funnel must not brand a successfully built graph as withheld."""
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)
    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(manifest))
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)
    monkeypatch.setattr(graft, "max_workspace_graph_bytes", lambda: 10**9)

    def fake_run(argv, *, cwd, **_kwargs):
        graph = Path(cwd).parent / "graph"
        graph.mkdir(exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", fake_run)
    assert graft.index_repositories(workspace, {"repo": repo})["repo"]["status"] == (
        "indexed"
    )

    # A second funnel with an already-spent clock reaches nothing, and the graph
    # that exists is not relabelled as withheld.
    _spent_clock(monkeypatch)
    assert graft.roots_needing_index(workspace, {"repo": repo}, deadline=_later()) == {}
    assert not (graft._root(workspace) / "repo" / "budget.json").exists()


@pytest.mark.asyncio
async def test_session_tools_without_graph_keep_file_tools(tmp_path, monkeypatch):
    """A refused graph degrades to file tools and an empty hint, not a failure."""
    from rootcoz.engine import session_tools
    from rootcoz.main import CHAT_BUILTIN_TOOLS

    async def no_index(*_args, **_kwargs):
        return None

    monkeypatch.setattr(session_tools, "index_workspace_graph", no_index)
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.graph_http_tools", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.cloned_graph_roots", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        session_tools, "install_http_tools_mcp_best_effort_async", no_index
    )

    tools = await session_tools.build_session_tools(
        builtin=list(CHAT_BUILTIN_TOOLS),
        custom_base=[],
        workspace=tmp_path,
        job_id="job",
    )

    assert not [t for t in tools.custom if t.get("name", "").startswith("graft_")]
    assert session_tools.graph_prompt_hint(tools) == ""
    assert {"read", "ls", "find", "grep"} <= set(CHAT_BUILTIN_TOOLS)


def test_a_deadline_marker_does_not_scan_the_workspace(tmp_path, monkeypatch):
    """A spent clock must not walk every graph to fill an informational field.

    The refusal marker is the only thing a timed-out preparation writes, and the
    workspace scan that used to feed its budget number was the one piece of work
    still running after the clock fired.
    """
    workspace = tmp_path / "workspace"
    repo, _manifest_hashes = _repo(workspace)
    destination = graft._root(workspace) / "repo"

    def forbidden(_root):
        raise AssertionError("scanned the workspace after the deadline")

    def timed_out(_path, deadline=None):
        raise TimeoutError("source scan timed out")

    monkeypatch.setattr(graft, "_workspace_graph_size", forbidden)
    monkeypatch.setattr(graft, "_sources", timed_out)
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)
    _spent_clock(monkeypatch)

    outcomes = graft.index_repositories(workspace, {"repo": repo}, deadline=_later())

    assert outcomes["repo"] == {"status": "skipped", "reason": "deadline"}
    assert _marker(destination)["reason"] == "deadline"


def test_a_graph_built_under_a_spent_clock_is_still_budget_checked(
    tmp_path, monkeypatch
):
    """The deadline must not smuggle an over-budget graph onto disk.

    An ``indexed`` early return on a spent clock left the graph in place with a
    current manifest: every query refused it on size while the next run treated
    it as unchanged, so nothing ever rebuilt or dropped it.
    """
    workspace = tmp_path / "workspace"
    repo, manifest = _repo(workspace)
    destination = graft._root(workspace) / "repo"
    used = {"bytes": 0}
    clock = {"calls": 0}

    def spent_after_admission(deadline):
        clock["calls"] += 1
        return deadline is not None and clock["calls"] > 1

    def fake_run(argv, *, cwd, **_kwargs):
        graph = Path(cwd).parent / "graph"
        graph.mkdir(exist_ok=True)
        used["bytes"] = 10**9
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_deadline_fired", spent_after_admission)
    monkeypatch.setattr(graft, "_sources", lambda path, deadline=None: dict(manifest))
    monkeypatch.setattr(graft, "_run", fake_run)
    monkeypatch.setattr(graft, "_disk_size", lambda *a, **k: 0)
    monkeypatch.setattr(graft, "_workspace_graph_size", lambda _root: used["bytes"])
    monkeypatch.setattr(graft, "max_workspace_graph_bytes", lambda: 10**6)

    outcomes = graft.index_repositories(workspace, {"repo": repo}, deadline=_later())

    assert outcomes["repo"] == {"status": "skipped", "reason": "workspace graph budget"}
    assert not (destination / "manifest.json").exists(), "an over-budget graph stayed"
    assert _marker(destination)["reason"] == "workspace graph budget"


def test_a_repo_that_cannot_fit_is_never_built(tmp_path, monkeypatch):
    """The budget refuses at admission, before the build it would throw away.

    Chat paid for the whole build and only then measured the workspace, so nine
    of twelve repositories were built to completion (up to ``BUILD_SECONDS``
    each, four at a time) and deleted. The estimate is the snapshot's exact cost
    plus a tenth for the index: the graph cannot be smaller than the sources it
    copies, so a repository refused here could not have fitted either.
    """
    workspace = tmp_path / "workspace"
    repo, _manifest_hashes = _repo(workspace, files={"big.py": b"x" * 5000})
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    built: list[str] = []

    def forbidden_run(argv, **_kwargs):
        built.append(argv[0])
        raise AssertionError("built a graph the workspace budget cannot hold")

    monkeypatch.setattr(graft, "_run", forbidden_run)
    monkeypatch.setattr(graft, "max_workspace_graph_bytes", lambda: 2048)

    outcomes = graft.index_repositories(workspace, {"repo": repo})

    assert outcomes["repo"] == {"status": "skipped", "reason": "workspace graph budget"}
    assert built == [], "the build ran for a repository that could not fit"
    marker = _marker(graft._root(workspace) / "repo")
    assert marker["reason"] == "workspace graph budget"
    assert marker["retry_after"] > time.time()


@pytest.mark.asyncio
async def test_analysis_sessions_still_wait_for_their_graph(tmp_path, monkeypatch):
    """Analysis keeps the blocking index and its unbounded clock, unchanged."""
    from rootcoz.engine import session_tools

    seen: list[float | None] = []

    async def index(_workspace, job_id="", repos=None, *, deadline=None):
        seen.append(deadline)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(session_tools, "index_workspace_graph", index)
    monkeypatch.setattr(session_tools, "install_http_tools_mcp_best_effort_async", noop)
    monkeypatch.setattr(
        "rootcoz.engine.graft_http.graph_http_tools", lambda *_a, **_k: []
    )

    await session_tools.build_session_tools(
        builtin=["read"], custom_base=[], workspace=tmp_path, job_id="job"
    )

    assert seen == [None], "analysis indexes before its tools, with no deadline"
