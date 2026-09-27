"""Loopback graph adapter isolation and sidecar tool wiring."""

import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rootcoz.engine.graft_http import (
    install_graph_skill,
    register_workspace,
    unregister_workspace,
)


def _await_estimate(job_id: str, expected: int | None) -> None:
    """Wait for post-response best-effort persistence to finish."""
    import asyncio

    from rootcoz import storage

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        actual = asyncio.run(storage.get_result(job_id))["graft_estimated_tokens_saved"]
        if actual > 0 if expected is None else actual == expected:
            return
        time.sleep(0.01)
    assert actual > 0 if expected is None else actual == expected


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
        lambda workspace, scope, name, params, **kwargs: (
            calls.append((workspace, scope, name, params)) or {"ok": True}
        ),
    )
    tools = register_workspace(ws, {"source": repo})
    assert len(tools) == 6
    assert all(tool["http"]["method"] == "POST" for tool in tools)
    assert all(
        tool["http"]["timeout_ms"] > graft.QUERY_SECONDS * 1000 for tool in tools
    )
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
        assert post(url, {**body, "query": "{query}"}) == 400
        assert post(url, {**body, "query": ""}) == 400
        assert post(url, {**body, "repo": "{repo}"}) == 400
        for tool in ("graft_find_all", "graft_file_api", "graft_trace_calls"):
            fields = next(t for t in tools if t["name"] == tool)["http"][
                "body_template"
            ]
            assert (
                post(url.replace("graft_find_code", tool), {**fields, "repo": "source"})
                == 400
            )
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


def test_estimate_is_job_scoped_and_counts_each_successful_retrieval(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from rootcoz import storage
    from rootcoz.engine import graft

    db = tmp_path / "results.db"
    monkeypatch.setattr(storage, "DB_PATH", db)
    asyncio.run(storage.init_db())
    for job in ("job-a", "job-b"):
        asyncio.run(storage.save_result(job, "https://example.test", "pending"))

    def query(workspace, scope, name, params, **kwargs):
        if params.get("query") == "error":
            return {
                "status": "failed",
                "result": {"saved": {"files": 1, "baselineChars": 4000}},
            }
        if params.get("query") == "missing":
            return {"status": "ok", "result": {"hits": []}}
        if params.get("query") == "zero":
            return {
                "status": "ok",
                "result": {"saved": {"files": 1, "baselineChars": 1}},
            }
        return {
            "status": "ok",
            "result": {"saved": {"files": 1, "baselineChars": 4000}, "hits": []},
        }

    monkeypatch.setattr(graft, "query_repo", query)
    workspaces = []
    try:
        for job in ("job-a", "job-b"):
            ws = tmp_path / job
            repo = ws / "source"
            repo.mkdir(parents=True)
            tools = register_workspace(ws, {"source": repo}, job_id=job)
            workspaces.append(ws)
            url = next(t for t in tools if t["name"] == "graft_find_code")["http"][
                "url"
            ]
            bearer = tools[0]["http"]["headers"]["Authorization"]
            workspaces[-1] = (ws, url, bearer)

        def post(entry, query_text):
            _, url, bearer = entry
            request = Request(
                url,
                json.dumps(
                    {"repo": "source", "query": query_text, "limit": "", "path": ""}
                ).encode(),
                {"Authorization": bearer},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                return json.load(response)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda entry: post(entry, "ok"), workspaces))
        expected = [
            max(0, (4000 + 2) // 4 - (len(json.dumps(result)) + 2) // 4)
            for result in results
        ]
        post(workspaces[0], "ok")  # separate HTTP retrieval, same query
        post(workspaces[0], "missing")
        post(workspaces[0], "zero")
        post(workspaces[0], "error")
        for job, estimate in zip(("job-a", "job-b"), expected, strict=True):
            _await_estimate(job, estimate * (2 if job == "job-a" else 1))
        first_ws, first_url, first_bearer = workspaces[0]
        register_workspace(first_ws, {"source": first_ws / "source"}, job_id="job-b")
        try:
            post((first_ws, first_url, first_bearer), "different")
            assert False, "old job token must be revoked"
        except HTTPError as error:
            assert error.code == 401
        unregister_workspace(first_ws)
        assert post(workspaces[1], "ok")["status"] == "ok"
        _await_estimate("job-b", expected[1] * 2)
    finally:
        for ws, _, _ in workspaces:
            unregister_workspace(ws)


def test_estimate_counts_both_repos_and_missing_job_is_soft_failure(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    from rootcoz import storage
    from rootcoz.engine import graft

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "results.db")
    asyncio.run(storage.init_db())
    asyncio.run(storage.save_result("live", "https://example.test", "pending"))
    ws = tmp_path / "work"
    for name in ("one", "two"):
        (ws / name).mkdir(parents=True)
    monkeypatch.setattr(
        graft,
        "query_repo",
        lambda *args, **kwargs: {
            "status": "ok",
            "result": {"saved": {"files": 1, "baselineChars": 4000}},
        },
    )
    tools = register_workspace(
        ws, {name: ws / name for name in ("one", "two")}, job_id="live"
    )
    url = next(t for t in tools if t["name"] == "graft_find_code")["http"]["url"]

    def post(repo: str, token: str) -> dict:
        request = Request(
            url,
            json.dumps(
                {"repo": repo, "query": "same", "limit": "", "path": ""}
            ).encode(),
            {"Authorization": token},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        token = tools[0]["http"]["headers"]["Authorization"]
        first = post("one", token)
        post("two", token)
        estimate = (4000 + 2) // 4 - (len(json.dumps(first)) + 2) // 4
        _await_estimate("live", 2 * estimate)
        asyncio.run(storage.save_result("deleted", "https://example.test", "pending"))
        rotated = register_workspace(
            ws, {name: ws / name for name in ("one", "two")}, job_id="deleted"
        )
        asyncio.run(storage.delete_job("deleted"))
        assert post("one", rotated[0]["http"]["headers"]["Authorization"]) == first
        _await_estimate("live", 2 * estimate)
    finally:
        unregister_workspace(ws)


def test_blocked_metric_serializes_revocation_but_not_other_workspaces(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rootcoz import storage
    from rootcoz.engine import graft

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "results.db")
    asyncio.run(storage.init_db())
    for job in ("old", "new", "other"):
        asyncio.run(storage.save_result(job, "https://example.test", "pending"))
    entered, release, revoking = Event(), Event(), Event()
    original = storage.add_graft_estimated_tokens_saved

    async def blocked(job_id: str, tokens: int) -> None:
        if job_id == "old":
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        await original(job_id, tokens)

    monkeypatch.setattr(storage, "add_graft_estimated_tokens_saved", blocked)
    monkeypatch.setattr(
        graft,
        "query_repo",
        lambda *args, **kwargs: {
            "status": "ok",
            "result": {"saved": {"files": 1, "baselineChars": 4000}},
        },
    )
    ws = tmp_path / "work"
    (ws / "repo").mkdir(parents=True)
    tools = register_workspace(ws, {"repo": ws / "repo"}, job_id="old")
    url = next(t for t in tools if t["name"] == "graft_find_code")["http"]["url"]
    token = tools[0]["http"]["headers"]["Authorization"]
    other = tmp_path / "other-work"
    (other / "repo").mkdir(parents=True)
    other_tools = register_workspace(other, {"repo": other / "repo"}, job_id="other")

    def post(bearer: str, url: str = url) -> int:
        request = Request(
            url,
            json.dumps(
                {"repo": "repo", "query": "x", "limit": "", "path": ""}
            ).encode(),
            {"Authorization": bearer},
            method="POST",
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status
        except HTTPError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(post, token)
            assert entered.wait(2)
            revoke = pool.submit(lambda: (revoking.set(), unregister_workspace(ws)))
            assert revoking.wait(2)
            assert post("Bearer invalid") == 401
            assert (
                post(
                    other_tools[0]["http"]["headers"]["Authorization"],
                    other_tools[0]["http"]["url"],
                )
                == 200
            )
            assert not revoke.done()
            assert pending.result(timeout=3) == 200  # metric cannot delay delivery
            release.set()
            revoke.result(timeout=3)
            assert (
                asyncio.run(storage.get_result("old"))["graft_estimated_tokens_saved"]
                > 0
            )
            rotated = register_workspace(ws, {"repo": ws / "repo"}, job_id="new")
            assert rotated[0]["http"]["headers"]["Authorization"] != token
            assert post(token) == 401
            assert (
                post(
                    rotated[0]["http"]["headers"]["Authorization"],
                    rotated[0]["http"]["url"],
                )
                == 200
            )
            _await_estimate("new", None)
    finally:
        release.set()
        unregister_workspace(ws)
        unregister_workspace(other)


def test_revoked_query_cannot_credit_old_job(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rootcoz import storage
    from rootcoz.engine import graft

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "results.db")
    asyncio.run(storage.init_db())
    for job in ("old", "new"):
        asyncio.run(storage.save_result(job, "https://example.test", "pending"))
    entered, release = Event(), Event()

    def query(workspace, scope, name, params, **kwargs):
        if params["query"] == "old":
            entered.set()
            assert release.wait(5)
        return {
            "status": "ok",
            "result": {"saved": {"files": 1, "baselineChars": 4000}},
        }

    monkeypatch.setattr(graft, "query_repo", query)
    ws = tmp_path / "work"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    tools = register_workspace(ws, {"repo": repo}, job_id="old")

    def post(tool, text):
        request = Request(
            tool["http"]["url"],
            json.dumps(
                {"repo": "repo", "query": text, "limit": "", "path": ""}
            ).encode(),
            {"Authorization": tool["http"]["headers"]["Authorization"]},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(post, tools[0], "old")
            assert entered.wait(2)
            replacement = register_workspace(ws, {"repo": repo}, job_id="new")
            release.set()
            assert (
                replacement[0]["http"]["headers"]["Authorization"]
                != tools[0]["http"]["headers"]["Authorization"]
            )
            assert pending.result(timeout=3)["status"] == "ok"
            assert (
                asyncio.run(storage.get_result("old"))["graft_estimated_tokens_saved"]
                == 0
            )
            assert post(replacement[0], "new")["status"] == "ok"
            _await_estimate("new", None)
    finally:
        release.set()
        unregister_workspace(ws)


def test_metric_failure_logs_traceback_after_reply(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    from threading import Event

    from rootcoz import storage
    from rootcoz.engine import graft

    entered, release = Event(), Event()

    async def broken(job_id: str, tokens: int) -> None:
        entered.set()
        assert release.wait(5)
        raise LookupError("job missing")

    monkeypatch.setattr(storage, "add_graft_estimated_tokens_saved", broken)
    monkeypatch.setattr(
        graft,
        "query_repo",
        lambda *args, **kwargs: {
            "status": "ok",
            "result": {"saved": {"files": 1, "baselineChars": 4000}},
        },
    )
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    tools = register_workspace(workspace, {"repo": repo}, job_id="gone")
    request = Request(
        tools[0]["http"]["url"],
        json.dumps({"repo": "repo", "query": "x", "limit": "", "path": ""}).encode(),
        {"Authorization": tools[0]["http"]["headers"]["Authorization"]},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            assert json.load(response)["status"] == "ok"
        assert entered.wait(2)
        release.set()
        # unregister waits for the metric task, including its error logging.
        unregister_workspace(workspace)
        records = [
            r for r in caplog.records if "Unable to store Graft estimate" in r.message
        ]
        assert len(records) == 1
        assert records[0].exc_info[0] is LookupError
        assert "job missing" in records[0].exc_info[1].args
        assert "x" not in records[0].message
    finally:
        release.set()
        unregister_workspace(workspace)


def test_estimate_calculation_failure_is_logged_and_query_succeeds(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    from rootcoz.engine import graft

    class BrokenSaved(dict):
        def get(self, key, default=None):
            raise RuntimeError("estimate calculation failed")

    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(
        graft,
        "query_repo",
        lambda *args, **kwargs: {
            "status": "ok",
            "result": {"saved": BrokenSaved(files=1, baselineChars=4000)},
        },
    )
    tools = register_workspace(workspace, {"repo": repo}, job_id="job")
    request = Request(
        tools[0]["http"]["url"],
        json.dumps({"repo": "repo", "query": "x", "limit": "", "path": ""}).encode(),
        {"Authorization": tools[0]["http"]["headers"]["Authorization"]},
        method="POST",
    )
    try:
        # A failing estimate's serialization path should still deliver the graph.
        with urlopen(request, timeout=2) as response:
            assert json.load(response)["status"] == "ok"
        assert any(
            record.exc_info and record.exc_info[0] is RuntimeError
            for record in caplog.records
            if "Unable to calculate Graft estimate" in record.message
        )
    finally:
        unregister_workspace(workspace)


def test_http_deadline_starts_before_query_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    import time

    from rootcoz.engine import graft

    ws = tmp_path / "work"
    repo = ws / "source"
    repo.mkdir(parents=True)
    monkeypatch.setattr(graft, "QUERY_SECONDS", 0.1)
    deadlines = []

    def query(workspace, scope, name, params, *, deadline):
        deadlines.append(deadline)
        return {"status": "ok"}

    monkeypatch.setattr(graft, "query_repo", query)
    tools = register_workspace(ws, {"source": repo})
    try:
        url = next(t for t in tools if t["name"] == "graft_find_code")["http"]["url"]
        token = tools[0]["http"]["headers"]["Authorization"]
        request = Request(
            url,
            json.dumps(
                {"repo": "source", "query": "x", "limit": "", "path": ""}
            ).encode(),
            {"Authorization": token},
            method="POST",
        )
        started = time.monotonic()
        with urlopen(request, timeout=2) as response:
            assert response.status == 200
        assert started < deadlines[0] <= time.monotonic() + graft.QUERY_SECONDS
        assert tools[0]["http"]["timeout_ms"] >= 1000 * (graft.QUERY_SECONDS + 15)
    finally:
        unregister_workspace(ws)


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
