"""Bounded Graft snapshots and read-only query routing."""

import json
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
    assert not (graft._root(workspace) / "code").exists()
