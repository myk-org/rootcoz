"""Loopback graph adapter isolation and sidecar tool wiring."""

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rootcoz.engine.graft_http import (
    install_graph_skill,
    register_workspace,
    unregister_workspace,
)


def test_graph_bridge(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "work"
    repo = ws / "source"
    repo.mkdir(parents=True)
    other = tmp_path / "other"
    other_repo = other / "other_repo"
    other_repo.mkdir(parents=True)
    calls = []
    from rootcoz.engine import graft

    monkeypatch.setattr(
        graft,
        "query_repo",
        lambda workspace, scope, name, params: (
            calls.append((workspace, scope, name, params)) or {"ok": True}
        ),
    )
    tools = register_workspace(ws, {"source": repo})
    assert len(tools) == 6
    assert all(tool["http"]["method"] == "POST" for tool in tools)
    skill_paths = [
        ws / location / "skills" / "rootcoz-graft" / "SKILL.md"
        for location in (".pi", ".agents", ".cursor", ".claude", ".gemini")
    ]
    assert all(
        path.read_text().startswith("---\nname: rootcoz-graft\n")
        for path in skill_paths
    )
    assert len({path.read_bytes() for path in skill_paths}) == 1
    assert all(
        "source" in tool["parameters"]["properties"]["repo"]["enum"] for tool in tools
    )
    url = next(t for t in tools if t["name"] == "graft_find_code")["http"]["url"]
    token = tools[0]["http"]["headers"]["Authorization"]

    def post(path: str, payload: object, bearer: str = token) -> int:
        req = Request(
            path, json.dumps(payload).encode(), {"Authorization": bearer}, method="POST"
        )
        try:
            with urlopen(req, timeout=2) as response:
                return response.status
        except HTTPError as error:
            return error.code

    try:
        body = {"repo": "source", "query": "hello", "limit": "", "path": ""}
        assert post(url, body) == 200
        assert calls == [(ws.resolve(), "source", "find_code", {"query": "hello"})]
        assert post(url, body, "Bearer invalid") == 401
        assert post(url + "?query=hello", body) == 404
        assert post(url.replace("graft_find_code", "run"), body) == 404
        assert post(url, {**body, "repo": str(repo)}) == 400
        assert post(url, {**body, "path": "../private"}) == 400
        assert post(url, {**body, "command": "id"}) == 400
        assert register_workspace(other, {"other_repo": other_repo})
        assert post(url, {**body, "repo": "other_repo"}) == 400
        assert post(url, body) == 200
        new_tools = register_workspace(ws, {"source": repo})
        assert (
            new_tools[0]["http"]["headers"]["Authorization"] == token
        )  # same clone keeps active sessions valid
        assert post(url, body) == 200
        unregister_workspace(ws)
        assert post(url, body, token) == 401
    finally:
        unregister_workspace(ws)
        unregister_workspace(other)


def test_scope_change_revokes_old_token(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    first = ws / "first"
    second = ws / "second"
    first.mkdir(parents=True)
    second.mkdir()
    initial = register_workspace(ws, {"first": first})
    url = initial[0]["http"]["url"]
    old_token = initial[0]["http"]["headers"]["Authorization"]
    try:
        changed = register_workspace(ws, {"second": second})
        assert changed[0]["http"]["headers"]["Authorization"] != old_token
        request = Request(
            url,
            json.dumps(
                {"repo": "first", "query": "x", "limit": "", "path": ""}
            ).encode(),
            {"Authorization": old_token},
            method="POST",
        )
        try:
            urlopen(request, timeout=2)
            assert False, "old scope token must be rejected"
        except HTTPError as error:
            assert error.code == 401
    finally:
        unregister_workspace(ws)


def test_graph_skill_rejects_unsafe_or_custom_discovery_paths(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (ws / ".cursor").symlink_to(external, target_is_directory=True)
    assert register_workspace(ws, {"repo": repo}) == []
    assert list(external.iterdir()) == []
    (ws / ".cursor").unlink()
    assert install_graph_skill(ws)
    skill = ws / ".pi" / "skills" / "rootcoz-graft" / "SKILL.md"
    skill.write_text("custom")
    assert register_workspace(ws, {"repo": repo}) == []
    assert skill.read_text() == "custom"


def test_repo_names_with_hyphens_and_dots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "my-repo.v2"
    repo.mkdir(parents=True)
    tools = register_workspace(workspace, {repo.name: repo})
    try:
        assert len(tools) == 6
        assert tools[0]["parameters"]["properties"]["repo"]["enum"] == [repo.name]
    finally:
        unregister_workspace(workspace)


def test_reject_unregistered_roots_and_listener_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from rootcoz.engine import graft_http

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    assert register_workspace(ws, {"outside": outside}) == []
    assert register_workspace(ws, {"ws": ws}) == []

    def fail(*args, **kwargs):
        raise OSError("loopback disabled")

    monkeypatch.setattr(graft_http, "ThreadingHTTPServer", fail)
    assert register_workspace(ws, {"repo": (ws / "repo")}) == []
    (ws / "repo").mkdir()
    assert register_workspace(ws, {"repo": ws / "repo"}) == []
    unregister_workspace(ws)
