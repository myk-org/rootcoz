"""Bounded Graft snapshots and read-only query routing."""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from rootcoz.engine import graft


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_index_only_git_visible_files_and_external_graph(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / ".gitignore").write_text("ignored.py\n")
    (repo / "tracked.py").write_text("def tracked(): pass\n")
    (repo / "ignored.py").write_text("secret\n")
    (repo / "untracked.py").write_text("def untracked(): pass\n")
    (repo / "link.py").symlink_to(repo / "tracked.py")
    git(repo, "add", "tracked.py", ".gitignore")
    calls = []

    def fake_run(argv, *, cwd, timeout, disk=None):
        calls.append((argv, cwd, timeout))
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"hits": [{"file": "tracked.py"}]}), ""
        )

    monkeypatch.setattr(graft, "_run", fake_run)
    assert (
        graft.index_repositories(workspace, {"code": repo})["code"]["status"]
        == "indexed"
    )
    snapshot = graft._root(workspace) / "code" / "snapshot"
    assert sorted(p.name for p in snapshot.iterdir()) == [
        ".gitignore",
        "tracked.py",
        "untracked.py",
    ]
    assert not snapshot.is_relative_to(workspace)
    assert calls[0][0][1] == "--dir"
    assert calls[0][1] == snapshot
    result = graft.query_repo(workspace, "code", "find_code", {"query": "tracked"})
    assert result == {"status": "ok", "result": {"hits": [{"file": "tracked.py"}]}}
    assert "--no-refresh" in calls[1][0]


def test_rejects_outside_results_and_invalid_commands(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "ok.py").write_text("pass")

    def fake_run(argv, **kwargs):
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"hits": [{"file": "../escape.py"}]}), ""
        )

    monkeypatch.setattr(graft, "_run", fake_run)
    graft.index_repositories(workspace, {"code": repo})
    assert (
        graft.query_repo(workspace, "code", "find_code", {"query": "x"})["status"]
        == "failed"
    )
    assert graft.query_repo(workspace, "../bad", "repo_map", {})["status"] == "skipped"
    assert (
        graft.query_repo(
            workspace, "code", "trace_calls", {"symbol": "x", "depth": "all"}
        )["status"]
        == "failed"
    )
    assert graft.query_repo(workspace, "code", "unknown", {})["status"] == "skipped"


def test_colon_filename_and_line_suffix(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a:b.py").write_text("pass")

    def fake_run(argv, **kwargs):
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"hits": [{"file": "a:b.py"}, {"pointer": "a:b.py:3"}]}),
            "",
        )

    monkeypatch.setattr(graft, "_run", fake_run)
    graft.index_repositories(workspace, {"code": repo})
    assert graft.query_repo(workspace, "code", "find_code", {"query": "a"}) == {
        "status": "ok",
        "result": {"hits": [{"file": "a:b.py"}, {"pointer": "a:b.py:3"}]},
    }


def test_dedup_and_source_invalidation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a.py").write_text("old")
    builds = []

    def fake_run(argv, **kwargs):
        if "build" in argv:
            builds.append(argv)
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"hits": [{"file": "a.py:1"}]}), ""
        )

    monkeypatch.setattr(graft, "_run", fake_run)
    assert (
        graft.index_repositories(workspace, {"code": repo})["code"]["status"]
        == "indexed"
    )
    assert (
        graft.index_repositories(workspace, {"code": repo})["code"]["status"]
        == "unchanged"
    )
    assert len(builds) == 1
    (repo / "a.py").write_text("new")  # same size, possibly same mtime
    assert (
        graft.query_repo(workspace, "code", "find_code", {"query": "a"})["status"]
        == "stale"
    )
    assert (
        graft.index_repositories(workspace, {"code": repo})["code"]["status"]
        == "indexed"
    )
    assert len(builds) == 2
    (repo / "added.py").write_text("more")
    assert graft.query_repo(workspace, "code", "repo_map", {})["status"] == "stale"


def test_failed_build_retry_and_marker(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    source = repo / "a.py"
    source.write_text("old")
    builds = []

    def failing_run(argv, **kwargs):
        builds.append(argv)
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(graft, "_run", failing_run)
    roots = {"code": repo}
    assert graft.index_repositories(workspace, roots)["code"]["status"] == "failed"
    marker = graft._root(workspace) / "code" / "failed.json"
    failed = json.loads(marker.read_text())
    assert failed["manifest"] == graft._sources(repo)
    assert failed["retry_after"] > time.time()
    assert not (marker.parent / "graph").exists()
    assert graft.roots_needing_index(workspace, roots) == {}
    assert len(builds) == 1

    source.write_text("new")
    assert graft.roots_needing_index(workspace, roots) == roots
    assert graft.index_repositories(workspace, roots)["code"]["status"] == "failed"
    assert len(builds) == 2
    assert graft.roots_needing_index(workspace, roots) == {}
    marker.unlink()
    assert graft.roots_needing_index(workspace, roots) == roots


def test_oversized_graph_followup_backoff_and_source_change(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    source = repo / "a.py"
    source.write_text("old")
    roots = {"code": repo}
    builds = []

    def fake_run(argv, **kwargs):
        builds.append(argv)
        (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", fake_run)
    assert graft.index_repositories(workspace, roots)["code"]["status"] == "indexed"
    destination = graft._root(workspace) / "code"
    monkeypatch.setattr(graft, "MAX_GRAPH_BYTES", graft._disk_size(destination) + 10)
    assert graft.roots_needing_index(workspace, roots) == {}
    assert graft.index_repositories(workspace, roots)["code"]["status"] == "unchanged"
    assert len(builds) == 1

    (destination / "graph" / "extra").write_bytes(b"x" * 20)
    assert graft.query_repo(workspace, "code", "repo_map", {})["status"] == "failed"
    assert graft.roots_needing_index(workspace, roots) == roots
    assert graft.index_repositories(workspace, roots)["code"]["status"] == "indexed"
    assert len(builds) == 2
    assert graft.roots_needing_index(workspace, roots) == {}
    source.write_text("new")
    assert graft.roots_needing_index(workspace, roots) == roots
    assert graft.index_repositories(workspace, roots)["code"]["status"] == "indexed"
    assert len(builds) == 3


def test_oversized_build_marker_bounds_followup_retries(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    source = repo / "a.py"
    source.write_text("old")
    roots = {"code": repo}
    builds = []

    def oversized_run(argv, **kwargs):
        builds.append(argv)
        graph = graft._root(workspace) / "code" / "graph"
        graph.mkdir()
        (graph / "large").write_bytes(b"x" * 1024)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", oversized_run)
    monkeypatch.setattr(graft, "MAX_GRAPH_BYTES", 500)
    assert graft.index_repositories(workspace, roots)["code"] == {
        "status": "failed",
        "reason": "_Limit",
        "truncated": True,
    }
    destination = graft._root(workspace) / "code"
    assert json.loads((destination / "failed.json").read_text())[
        "manifest"
    ] == graft._sources(repo)
    assert not (destination / "graph").exists()
    assert graft.indexed_roots(workspace) == {}
    assert graft.roots_needing_index(workspace, roots) == {}
    assert len(builds) == 1

    source.write_text("new")
    assert graft.roots_needing_index(workspace, roots) == roots
    assert graft.index_repositories(workspace, roots)["code"]["status"] == "failed"
    assert graft.roots_needing_index(workspace, roots) == {}
    assert len(builds) == 2
    marker = destination / "failed.json"
    failed = json.loads(marker.read_text())
    failed["retry_after"] = 0
    marker.write_text(json.dumps(failed))
    assert graft.roots_needing_index(workspace, roots) == roots


def test_failed_source_scan_backoff_and_new_repo(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    source = repo / "a.py"
    source.write_text("old")
    roots = {"code": repo}
    monkeypatch.setattr(graft, "MAX_BYTES", 1)
    outcome = graft.index_repositories(workspace, roots)["code"]
    assert outcome == {"status": "failed", "reason": "_Limit", "truncated": True}
    marker = graft._root(workspace) / "code" / "failed.json"
    assert json.loads(marker.read_text())["stamp"] == graft._source_stamp(repo)
    assert graft.roots_needing_index(workspace, roots) == {}
    source.write_text("changed")
    assert graft.roots_needing_index(workspace, roots) == roots
    assert graft.index_repositories(workspace, roots)["code"] == outcome
    assert graft.roots_needing_index(workspace, roots) == {}
    failed = json.loads(marker.read_text())
    failed["retry_after"] = 0
    marker.write_text(json.dumps(failed))
    assert graft.roots_needing_index(workspace, roots) == roots

    other = workspace / "other"
    other.mkdir()
    git(other, "init")
    (other / "a.py").write_text("new")
    assert graft.roots_needing_index(workspace, {**roots, "other": other}) == {
        "code": repo,
        "other": other,
    }


def test_parallel_builds_and_isolation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repos = {}
    for name in ("a", "b", "c"):
        repo = workspace / name
        repo.mkdir(parents=True)
        git(repo, "init")
        (repo / "source.py").write_text(name)
        repos[name] = repo
    active = 0
    peak = 0
    lock = threading.Lock()
    overlapping = threading.Barrier(2)
    started = 0

    def fake_run(argv, **kwargs):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
            first_pair = started <= 2
        if first_pair:
            overlapping.wait(timeout=2)
        time.sleep(0.05)
        Path(argv[2]).mkdir()
        with lock:
            active -= 1
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", fake_run)
    assert all(
        item["status"] == "indexed"
        for item in graft.index_repositories(workspace, repos).values()
    )
    assert peak == 2


def test_effective_repositories_all_indexed_discoverable_and_refreshable(
    tmp_path, monkeypatch
):
    from rootcoz.engine.core import clone_additional_repos
    from rootcoz.models import AdditionalRepo
    from rootcoz.sources.base import setup_analysis_workspace

    class Manager:
        def create_workspace(self):
            path = tmp_path / "workspace"
            path.mkdir()
            return path

        def clone_into(self, url, target, **kwargs):
            target.mkdir(parents=True)
            git(target, "init")
            (target / "a.py").write_text("pass")

    manager = Manager()
    initial = [
        AdditionalRepo(name=f"initial-{i}", url=f"https://example.com/initial-{i}")
        for i in range(13)
    ]
    effective = [
        AdditionalRepo(name=f"effective-{i}", url=f"https://example.com/effective-{i}")
        for i in range(13)
    ]

    async def exercise():
        setup, _ = await setup_analysis_workspace(
            manager,
            tests_repo_url="https://example.com/tests",
            tests_repo_ref="",
            tests_repo_token="",
            additional_repos=initial,
            extract_path=None,
            artifacts_context="",
            job_id="test",
        )
        assert len(setup.cloned_repos) == 1
        cloned, _ = await clone_additional_repos(manager, effective, setup.repo_path)
        setup.cloned_repos.update(cloned)
        return setup

    import asyncio

    setup = asyncio.run(exercise())

    def fake_run(argv, **kwargs):
        if "build" in argv:
            Path(argv[2]).mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(graft, "_run", fake_run)
    roots = setup.cloned_repos
    assert len(roots) == 14
    assert {effective_repo.name for effective_repo in effective} <= roots.keys()
    assert graft.roots_needing_index(setup.repo_path, roots) == roots
    assert {
        name: result["status"]
        for name, result in graft.index_repositories(setup.repo_path, roots).items()
    } == dict.fromkeys(roots, "indexed")
    assert graft.indexed_roots(setup.repo_path) == roots
    assert graft.roots_needing_index(setup.repo_path, roots) == {}
    for repo in roots.values():
        (repo / "a.py").write_text("changed")
    assert graft.roots_needing_index(setup.repo_path, roots) == roots
    assert {
        name: result["status"]
        for name, result in graft.index_repositories(setup.repo_path, roots).items()
    } == dict.fromkeys(roots, "indexed")
    assert graft.indexed_roots(setup.repo_path) == roots
    assert graft.roots_needing_index(setup.repo_path, roots) == {}


def test_all_repositories_indexed_and_unrelated_workspaces(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def index(workspace, root, name, repo, *args):
        calls.append(name)
        if workspace.name == "slow":
            entered.set()
            assert release.wait(3)
        return {"status": "indexed"}

    monkeypatch.setattr(graft, "_index_one", index)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(graft.index_repositories, tmp_path / "slow", {"a": tmp_path})
        assert entered.wait(2)
        fast = pool.submit(
            graft.index_repositories,
            tmp_path / "fast",
            {str(i): tmp_path for i in range(14)},
        )
        try:
            result = fast.result(timeout=2)
            assert len(result) == 14
            assert all(r["status"] == "indexed" for r in result.values())
        finally:
            release.set()
        slow.result(timeout=2)
    assert len(calls) == 15


def test_workspace_budget_skips_then_retries_after_cleanup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repos = {}
    for i in range(14):
        repo = workspace / f"repo-{i}"
        repo.mkdir(parents=True)
        git(repo, "init")
        (repo / "a.py").write_text("pass")
        repos[repo.name] = repo

    def run(argv, **kwargs):
        if "build" in argv:
            graph = Path(argv[2])
            graph.mkdir()
            (graph / "data").write_bytes(b"x" * 200)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(graft, "_run", run)
    monkeypatch.setattr(graft, "MAX_GRAPH_BYTES", 4096)
    monkeypatch.setattr(graft, "MAX_WORKSPACE_GRAPH_BYTES", 7000)
    outcomes = graft.index_repositories(workspace, repos)
    assert any(result["status"] == "skipped" for result in outcomes.values())
    assert any(result["status"] == "indexed" for result in outcomes.values())
    assert graft._disk_size(graft._root(workspace), limit=7000) <= 7000
    skipped = {
        name: repo
        for name, repo in repos.items()
        if outcomes[name]["status"] == "skipped"
    }
    assert skipped.items() <= graft.roots_needing_index(workspace, repos).items()
    assert not any(
        (graft._root(workspace) / name / "graph").exists() for name in skipped
    )

    # Refreshing an existing graph releases its old space rather than counting it twice.
    kept = next(
        name for name, result in outcomes.items() if result["status"] == "indexed"
    )
    (repos[kept] / "a.py").write_text("changed")
    assert (
        graft.index_repositories(workspace, {kept: repos[kept]})[kept]["status"]
        == "indexed"
    )
    for name in list(repos):
        if name not in skipped:
            shutil.rmtree(graft._root(workspace) / name)
    retry = dict(list(skipped.items())[:1])
    assert all(
        result["status"] == "indexed"
        for result in graft.index_repositories(workspace, retry).values()
    )
    graft.cleanup_graph(workspace)
    assert not graft._root(workspace).exists()


def test_same_workspace_builds_do_not_overlap(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def index(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        assert release.wait(3)
        active -= 1
        return {"status": "indexed"}

    monkeypatch.setattr(graft, "_index_one", index)
    workspace = tmp_path / "work"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(graft.index_repositories, workspace, {"a": tmp_path})
        assert entered.wait(2)
        second = pool.submit(graft.index_repositories, workspace, {"a": tmp_path})
        try:
            time.sleep(0.05)
            assert peak == 1
        finally:
            release.set()
        assert first.result(timeout=2) == second.result(timeout=2)
    assert peak == 1


def test_query_waits_for_reindex_and_uses_new_snapshot(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a.py").write_text("first")

    def run(argv, **kwargs):
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(graft, "_run", run)
    graft.index_repositories(workspace, {"code": repo})
    (repo / "a.py").write_text("second")
    entered = threading.Event()
    release = threading.Event()
    original = graft._index_one

    def paused(*args):
        result = original(*args)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(graft, "_index_one", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(graft.index_repositories, workspace, {"code": repo})
        assert entered.wait(2)
        reader = pool.submit(graft.query_repo, workspace, "code", "repo_map", {})
        try:
            time.sleep(0.05)
            assert not reader.done()
        finally:
            release.set()
        assert writer.result(timeout=2)["code"]["status"] == "indexed"
        assert reader.result(timeout=2)["status"] == "ok"


def test_indexed_roots_waits_for_reindex(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a.py").write_text("first")

    def run(argv, **kwargs):
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(graft, "_run", run)
    graft.index_repositories(workspace, {"code": repo})
    (repo / "a.py").write_text("second")
    entered = threading.Event()
    release = threading.Event()
    original = graft._index_one

    def paused(*args):
        result = original(*args)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(graft, "_index_one", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(graft.index_repositories, workspace, {"code": repo})
        assert entered.wait(2)
        reader = pool.submit(graft.indexed_roots, workspace)
        try:
            time.sleep(0.05)
            assert not reader.done()
        finally:
            release.set()
        writer.result(timeout=2)
        assert reader.result(timeout=2) == {"code": repo}


def test_query_lock_deadline_boundary_returns_timeout(tmp_path, monkeypatch):
    import fcntl

    from rootcoz.engine import http_mcp

    workspace = tmp_path / "workspace"
    (workspace / "code").mkdir(parents=True)
    git(workspace / "code", "init")
    graft._root(workspace).mkdir()
    monkeypatch.setattr(http_mcp.tempfile, "gettempdir", lambda: str(tmp_path))
    path = http_mcp._install_lock_path(workspace)
    path.parent.mkdir(mode=0o700)
    with path.open("a+") as holder:
        path.chmod(0o600)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            return 99.99 if calls == 1 else 100.01

        def sleep(seconds):
            if seconds < 0:
                raise ValueError("sleep length must be non-negative")

        monkeypatch.setattr(http_mcp.time, "monotonic", clock)
        monkeypatch.setattr(http_mcp.time, "sleep", sleep)
        assert graft.query_repo(workspace, "code", "repo_map", {}, deadline=100.0) == {
            "status": "failed",
            "reason": "TimeoutError",
            "truncated": False,
        }
        assert calls == 2


def test_query_lock_wait_is_bounded(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    workspace = tmp_path / "workspace"
    (workspace / "code").mkdir(parents=True)
    git(workspace / "code", "init")
    graft._root(workspace).mkdir()
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(graft, "QUERY_SECONDS", 0.05)

    def hold():
        from rootcoz.engine.http_mcp import _workspace_install_lock

        with _workspace_install_lock(workspace):
            entered.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        assert entered.wait(2)
        try:
            start = time.monotonic()
            result = graft.query_repo(workspace, "code", "repo_map", {})
            assert time.monotonic() - start < 0.5
            assert result["reason"] == "TimeoutError"
        finally:
            release.set()
        holder.result(timeout=2)


def test_index_outcome_logs_are_sanitized(caplog):
    graft.log_index_outcomes(
        {
            "code": {"status": "indexed", "files": 1},
            "/private/token": {"status": "failed", "reason": "/secret/path"},
            "limited": {"status": "failed", "reason": "_Limit", "truncated": True},
        }
    )
    assert "scope=code status=indexed" in caplog.text
    assert "scope=invalid status=failed category=error" in caplog.text
    assert "scope=limited status=failed category=limit" in caplog.text
    assert "/private" not in caplog.text and "/secret" not in caplog.text


def test_query_deadline_covers_scans(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a.py").write_text("pass")

    def fake_run(argv, **kwargs):
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(graft, "_run", fake_run)
    graft.index_repositories(workspace, {"code": repo})
    monkeypatch.setattr(graft, "QUERY_SECONDS", -1)
    assert (
        graft.query_repo(workspace, "code", "repo_map", {})["reason"] == "TimeoutError"
    )


def test_query_deadline_includes_graph_disk_walk(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a.py").write_text("pass")

    def run(argv, **kwargs):
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(graft, "_run", run)
    graft.index_repositories(workspace, {"code": repo})
    original = graft._disk_size

    def slow_walk(path, deadline=None):
        if deadline is not None:
            time.sleep(0.06)
        return original(path, deadline)

    monkeypatch.setattr(graft, "_disk_size", slow_walk)
    monkeypatch.setattr(graft, "QUERY_SECONDS", 0.05)
    assert (
        graft.query_repo(workspace, "code", "repo_map", {})["reason"] == "TimeoutError"
    )


def test_rejects_pointer_symlink_and_scrubs_errors(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a.py").write_text("pass")
    result = {"hits": [{"file": "a.py", "pointer": "/private/host:1"}]}

    def fake_run(argv, **kwargs):
        if "build" in argv:
            (graft._root(workspace) / "code" / "graph").mkdir()
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    monkeypatch.setattr(graft, "_run", fake_run)
    graft.index_repositories(workspace, {"code": repo})
    assert (
        graft.query_repo(workspace, "code", "find_code", {"query": "a"})["status"]
        == "failed"
    )
    assert (
        graft.query_repo(workspace, "code", "file_api", {"path": "../a.py"})["status"]
        == "failed"
    )
    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to(tmp_path / "secret")
    assert graft.query_repo(workspace, "code", "repo_map", {})["status"] == "stale"
    monkeypatch.setattr(
        graft,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "secret", output="/private/host")
        ),
    )
    (repo / "a.py").unlink()
    (repo / "a.py").write_text("pass")
    response = graft.query_repo(workspace, "code", "repo_map", {})
    assert "/private" not in str(response) and "secret" not in str(response)


def test_bounded_subprocess_and_no_telemetry(tmp_path, monkeypatch):
    import sys

    import pytest

    monkeypatch.setattr(graft, "MAX_OUTPUT", 32)
    with pytest.raises(ValueError):
        graft._bounded(
            [sys.executable, "-c", "print('x' * 1000)"], cwd=tmp_path, timeout=2
        )
    assert (
        graft._bounded(
            [sys.executable, "-c", "import os; print(os.environ['DO_NOT_TRACK'])"],
            cwd=tmp_path,
            timeout=2,
        ).strip()
        == b"1"
    )


def test_rejects_symlinked_graph_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "source.py").write_text("pass")
    outside = tmp_path / "outside"
    outside.mkdir()
    graft._root(workspace).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        graft,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Graft must not run")
        ),
    )
    assert (
        graft.index_repositories(workspace, {"code": repo})["code"]["status"]
        == "failed"
    )
    assert graft.query_repo(workspace, "code", "repo_map", {})["status"] == "skipped"
    assert list(outside.iterdir()) == []


def test_over_limit_cleans_snapshot(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "code"
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "too_big.py").write_text("x" * 20)
    monkeypatch.setattr(graft, "MAX_BYTES", 10)
    assert (
        graft.index_repositories(workspace, {"code": repo})["code"]["status"]
        == "failed"
    )
    destination = graft._root(workspace) / "code"
    assert sorted(p.name for p in destination.iterdir()) == ["failed.json"]
    assert json.loads((destination / "failed.json").read_text())["stamp"]
