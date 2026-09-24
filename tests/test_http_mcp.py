"""Tests for CLI/acpx HTTP MCP install from path-specific custom_tools."""

import asyncio
import builtins
import json
import multiprocessing as mp
import multiprocessing.util
import os
import stat
import tempfile
import time
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path

import pytest

from rootcoz.engine import http_mcp as http_mcp_mod
from rootcoz.engine.chat import (
    analysis_http_tools,
    build_admin_custom_tools,
    build_chat_custom_tools,
)
from rootcoz.engine.http_mcp import (
    MCP_SERVER_NAME,
    cleanup_http_tools_mcp,
    http_tools_dump_path,
    install_http_tools_mcp,
)


def _mcp_js(tmp_path: Path) -> Path:
    js = tmp_path / "http-tools-mcp.js"
    js.write_text("// stub\n", encoding="utf-8")
    return js


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _lock_in_child(
    workspace: Path,
    lock_root: Path,
    entered: ProcessEvent | None = None,
    release: ProcessEvent | None = None,
) -> None:
    tempfile.tempdir = str(lock_root)
    with http_mcp_mod._workspace_install_lock(workspace):
        if entered is None:
            os._exit(0)
        entered.set()
        assert release is not None
        release.wait(10)


def test_install_skips_empty_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert install_http_tools_mcp(workspace, [], mcp_js=_mcp_js(tmp_path)) is None
    assert not (workspace / ".cursor" / "mcp.json").exists()


def test_install_skips_missing_binary(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.delenv("ROOTCOZ_HTTP_TOOLS_MCP", raising=False)
    monkeypatch.setattr(
        "rootcoz.engine.http_mcp.resolve_http_tools_mcp_js", lambda: None
    )
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    assert install_http_tools_mcp(workspace, tools) is None
    assert not (workspace / ".mcp.json").exists()


def test_analysis_tools_do_not_include_graph(tmp_path: Path) -> None:
    assert all(
        not tool["name"].startswith("graft_")
        for tool in analysis_http_tools(
            server_url="http://localhost:8000",
            job_id="job-1",
            auth_header="Bearer token",
        )
    )


def test_install_writes_cursor_claude_gemini_configs(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    js = _mcp_js(tmp_path)
    tools = analysis_http_tools(
        server_url="http://localhost:8000",
        job_id="job-1",
        auth_header="Bearer super-secret-token",
    )
    dump = install_http_tools_mcp(workspace, tools, mcp_js=js)
    assert dump is not None
    assert dump == http_tools_dump_path(workspace)
    assert dump.parent == tmp_path
    mode = dump.stat().st_mode
    assert mode & stat.S_IRUSR
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH

    dumped = json.loads(dump.read_text(encoding="utf-8"))
    names = {t["name"] for t in dumped}
    assert names == {
        "get_failure_history",
        "search_error_signature",
        "get_classification_history",
        "get_job_history_stats",
        "classify_test_pattern",
    }

    cursor = _read(workspace / ".cursor" / "mcp.json")
    claude = _read(workspace / ".mcp.json")
    gemini = _read(workspace / ".gemini" / "settings.json")
    claude_settings = _read(workspace / ".claude" / "settings.json")

    for cfg in (cursor, claude, gemini):
        blob = json.dumps(cfg)
        assert "super-secret-token" not in blob
        assert "Bearer" not in blob
        server = cfg["mcpServers"][MCP_SERVER_NAME]
        assert server["args"] == [str(js)]
        assert server["env"]["ROOTCOZ_HTTP_TOOLS_FILE"] == str(dump)

    assert claude["mcpServers"][MCP_SERVER_NAME]["type"] == "stdio"
    assert gemini["mcpServers"][MCP_SERVER_NAME]["trust"] is True
    assert set(gemini["mcpServers"][MCP_SERVER_NAME]["includeTools"]) == names
    assert "enableAllProjectMcpServers" not in claude_settings
    assert claude_settings["enabledMcpjsonServers"] == [MCP_SERVER_NAME]


def test_chat_mcp_omits_jira_without_creds(tmp_path: Path) -> None:
    workspace = tmp_path / "chat-ws"
    workspace.mkdir()
    tools = build_chat_custom_tools(
        server_url="http://localhost:8000",
        auth_token="chat-token",
        job_id="job-chat",
    )
    names = {t["name"] for t in tools}
    assert "search_jira" not in names
    assert "search_github_issues" not in names
    assert "get_job_result" in names

    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert dump is not None
    dumped_names = {t["name"] for t in json.loads(dump.read_text(encoding="utf-8"))}
    assert dumped_names == names
    assert "search_jira" not in dumped_names
    gemini = _read(workspace / ".gemini" / "settings.json")
    assert "search_jira" not in gemini["mcpServers"][MCP_SERVER_NAME]["includeTools"]


def test_chat_mcp_includes_jira_when_configured(tmp_path: Path) -> None:
    workspace = tmp_path / "chat-ws"
    workspace.mkdir()
    tools = build_chat_custom_tools(
        server_url="http://localhost:8000",
        auth_token="chat-token",
        job_id="job-chat",
        jira_url="https://jira.example.com",
        jira_token="jira-secret",
    )
    names = {t["name"] for t in tools}
    assert "search_jira" in names
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    dumped_names = {t["name"] for t in json.loads(dump.read_text(encoding="utf-8"))}
    assert "search_jira" in dumped_names
    assert "jira-secret" not in (workspace / ".mcp.json").read_text(encoding="utf-8")


def test_admin_mcp_uses_admin_builder_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "admin-ws"
    workspace.mkdir()
    tools = build_admin_custom_tools(
        server_url="http://localhost:8000",
        auth_token="admin-token",
    )
    names = {t["name"] for t in tools}
    assert "db_query" in names
    assert "get_report_totals" in names
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    dumped_names = {t["name"] for t in json.loads(dump.read_text(encoding="utf-8"))}
    assert dumped_names == names
    assert "get_failure_history" not in dumped_names
    assert "admin-token" not in (workspace / ".cursor" / "mcp.json").read_text(
        encoding="utf-8"
    )


def test_analysis_http_tools_empty_without_auth() -> None:
    assert analysis_http_tools(server_url="http://x", job_id="j", auth_header="") == []
    assert analysis_http_tools(server_url="", job_id="j", auth_header="Bearer t") == []


def test_empty_install_removes_prior_rootcoz_keeps_others(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cursor_dir = workspace / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "echo"}}, "keep": True}),
        encoding="utf-8",
    )
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert dump is not None
    assert dump.exists()
    assert install_http_tools_mcp(workspace, [], mcp_js=_mcp_js(tmp_path)) is None
    assert not dump.exists()
    cursor = _read(workspace / ".cursor" / "mcp.json")
    assert cursor["keep"] is True
    assert cursor["mcpServers"] == {"other": {"command": "echo"}}
    assert MCP_SERVER_NAME not in cursor["mcpServers"]


def test_missing_binary_removes_prior_install(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert dump is not None
    monkeypatch.setattr(
        "rootcoz.engine.http_mcp.resolve_http_tools_mcp_js", lambda: None
    )
    assert install_http_tools_mcp(workspace, tools) is None
    assert not dump.exists()
    assert not (workspace / ".mcp.json").exists()


def test_install_merges_existing_mcp_servers(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".cursor").mkdir()
    (workspace / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "echo"}}, "keep": True}),
        encoding="utf-8",
    )
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Read"]}}),
        encoding="utf-8",
    )
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    cursor = _read(workspace / ".cursor" / "mcp.json")
    assert cursor["keep"] is True
    assert "other" in cursor["mcpServers"]
    assert MCP_SERVER_NAME in cursor["mcpServers"]
    claude_settings = _read(workspace / ".claude" / "settings.json")
    assert claude_settings["permissions"] == {"allow": ["Read"]}
    assert "enableAllProjectMcpServers" not in claude_settings
    assert claude_settings["enabledMcpjsonServers"] == [MCP_SERVER_NAME]


def test_install_does_not_follow_config_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}\n', encoding="utf-8")
    link = workspace / ".mcp.json"
    link.symlink_to(outside)
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert json.loads(outside.read_text(encoding="utf-8")) == {"secret": True}
    assert not link.is_symlink()
    claude = _read(link)
    assert MCP_SERVER_NAME in claude["mcpServers"]
    assert "secret" not in claude


def test_install_skips_escaped_parent_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (workspace / ".cursor").symlink_to(outside_dir)
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert not (outside_dir / "mcp.json").exists()
    assert (workspace / ".mcp.json").is_file()


def test_install_skips_symlink_loop_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cursor = workspace / ".cursor"
    cursor.symlink_to(".cursor")
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert dump is not None
    assert (workspace / ".mcp.json").is_file()
    assert MCP_SERVER_NAME in _read(workspace / ".mcp.json")["mcpServers"]
    assert cursor.is_symlink()
    assert not (cursor / "mcp.json").exists()


def test_cleanup_removes_sibling_dump(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert dump is not None
    cleanup_http_tools_mcp(workspace)
    assert not dump.exists()
    assert not (workspace / ".mcp.json").exists()


def test_install_preserves_malformed_config(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    malformed = workspace / ".mcp.json"
    malformed.write_text("not-json", encoding="utf-8")
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert dump is not None
    assert malformed.read_text(encoding="utf-8") == "not-json"
    assert MCP_SERVER_NAME in _read(workspace / ".cursor" / "mcp.json")["mcpServers"]


def test_cleanup_preserves_malformed_config(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    malformed = workspace / ".mcp.json"
    malformed.write_text("not-json", encoding="utf-8")
    assert install_http_tools_mcp(workspace, [], mcp_js=_mcp_js(tmp_path)) is None
    assert malformed.read_text(encoding="utf-8") == "not-json"


def test_install_strips_group_world_write_from_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mcp = workspace / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    mcp.chmod(0o622)
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert mcp.stat().st_mode & 0o777 == 0o600
    assert mcp.stat().st_mode & 0o022 == 0


def test_new_config_files_are_private(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert (workspace / ".mcp.json").stat().st_mode & 0o777 == 0o600


def test_install_failure_restores_prior_rootcoz_entry(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    prior = {
        "mcpServers": {MCP_SERVER_NAME: {"command": "old-http"}},
        "keep": True,
    }
    mcp = workspace / ".mcp.json"
    mcp.write_text(json.dumps(prior), encoding="utf-8")
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("rootcoz.engine.http_mcp._install_mcp_configs", boom)
    try:
        install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    restored = _read(mcp)
    assert restored == prior
    assert not http_tools_dump_path(workspace).exists()


def test_install_failure_restores_config_symlink(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}\n', encoding="utf-8")
    link = workspace / ".mcp.json"
    link.symlink_to(outside)
    original_target = os.readlink(link)
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]

    real_write = http_mcp_mod._write_merged_json

    def boom_after_mcp_json(dest, payload):
        if dest.name == "settings.json" and dest.parent.name == ".gemini":
            raise OSError("disk full")
        return real_write(dest, payload)

    monkeypatch.setattr(http_mcp_mod, "_write_merged_json", boom_after_mcp_json)
    try:
        install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    assert link.is_symlink()
    assert os.readlink(link) == original_target
    assert json.loads(outside.read_text(encoding="utf-8")) == {"secret": True}
    assert not http_tools_dump_path(workspace).exists()


def test_install_failure_does_not_restore_escaped_parent(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "mcp.json"
    original = '{"keep": true}\n'
    outside_file.write_text(original, encoding="utf-8")
    inode = outside_file.stat().st_ino
    (workspace / ".cursor").symlink_to(outside_dir)
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]

    real_write = http_mcp_mod._write_merged_json

    def boom_after_mcp_json(dest, payload):
        if dest.name == "settings.json" and dest.parent.name == ".gemini":
            raise OSError("disk full")
        return real_write(dest, payload)

    monkeypatch.setattr(http_mcp_mod, "_write_merged_json", boom_after_mcp_json)
    try:
        install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    assert outside_file.read_text(encoding="utf-8") == original
    assert outside_file.stat().st_ino == inode
    assert not http_tools_dump_path(workspace).exists()


def test_install_failure_rolls_back_new_dump(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("rootcoz.engine.http_mcp._install_mcp_configs", boom)
    try:
        install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    assert not http_tools_dump_path(workspace).exists()
    assert not (workspace / ".mcp.json").exists()


def test_cleanup_removes_claude_enabled_server_keeps_other_settings(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Read"]}}),
        encoding="utf-8",
    )
    tools = [{"name": "get_job_result", "http": {"method": "GET", "url": "http://x"}}]
    install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    cleanup_http_tools_mcp(workspace)
    settings = _read(workspace / ".claude" / "settings.json")
    assert settings["permissions"] == {"allow": ["Read"]}
    assert "enabledMcpjsonServers" not in settings
    assert "enableAllProjectMcpServers" not in settings


# ---------------------------------------------------------------------------
# Non-directory parents and best-effort installs
# ---------------------------------------------------------------------------


def test_install_skips_file_where_directory_expected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".cursor").write_text("regular file", encoding="utf-8")
    tools = analysis_http_tools(
        server_url="http://localhost:8000",
        job_id="job-1",
        auth_header="Bearer super-secret-token",
    )
    dump = install_http_tools_mcp(workspace, tools, mcp_js=_mcp_js(tmp_path))
    assert dump is not None
    # The blocking file is preserved untouched; no config is written inside it.
    assert (workspace / ".cursor").is_file()
    # Other configs still install.
    assert MCP_SERVER_NAME in _read(workspace / ".mcp.json")["mcpServers"]


def test_atomic_write_raises_not_a_directory(tmp_path: Path) -> None:
    parent = tmp_path / "blocker"
    parent.write_text("regular file", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        http_mcp_mod._atomic_write_bytes(
            tmp_path / "dest.json", b"{}", mode=0o600, parent=parent
        )


def test_best_effort_install_swallows_errors(tmp_path: Path, monkeypatch) -> None:
    def boom(workspace, custom_tools):
        raise OSError("disk full")

    monkeypatch.setattr(http_mcp_mod, "install_http_tools_mcp", boom)
    # Must not raise — analysis/chat continue without MCP.
    http_mcp_mod._best_effort_install(tmp_path, [])


def test_install_lock_survives_parent_workspace_cleanup(tmp_path: Path) -> None:
    import fcntl
    import shutil

    parent = tmp_path / "job"
    child = parent / "user"
    child.mkdir(parents=True)
    with http_mcp_mod._workspace_install_lock(child):
        path = http_mcp_mod._install_lock_path(child)
        inode = path.stat().st_ino
        shutil.rmtree(parent)
        assert path.exists() and path.stat().st_ino == inode
        with open(path, "a+") as another, pytest.raises(BlockingIOError):
            fcntl.flock(another.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_analysis_does_not_index_unusable_graphs() -> None:
    import inspect

    from rootcoz import main

    for analysis in (
        main._process_ci_source_analysis,
        main._reanalyze_failure_background,
    ):
        assert "index_repositories" not in inspect.getsource(analysis)
        assert "copy_rootcoz_pi_resources" in inspect.getsource(analysis)
    assert "index_repositories" in inspect.getsource(main._init_chat_under_barrier)


def test_install_lock_reaps_short_lived_workspaces(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    for n in range(20):
        workspace = tmp_path / f"workspace-{n}"
        with http_mcp_mod._workspace_install_lock(workspace):
            assert http_mcp_mod._install_lock_path(workspace).exists()
    assert (
        list(
            (tmp_path / f"rootcoz-locks-{os.getuid()}").glob("[0-9a-f]" * 64 + ".lock")
        )
        == []
    )


def test_install_lock_reaps_after_process_exit(tmp_path: Path, monkeypatch) -> None:
    mp.util.get_temp_dir()  # Keep forkserver's AF_UNIX socket outside the long pytest path.
    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "abandoned"
    path = http_mcp_mod._install_lock_path(workspace)

    process = mp.get_context("forkserver").Process(
        target=_lock_in_child, args=(workspace, tmp_path)
    )
    process.start()
    process.join(5)
    assert process.exitcode == 0
    assert path.exists()
    with http_mcp_mod._workspace_install_lock(tmp_path / "next"):
        assert path.exists()
    assert not path.exists()


def test_install_lock_reaping_preserves_holder_and_waiter(
    tmp_path: Path, monkeypatch
) -> None:
    import threading

    mp.util.get_temp_dir()
    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "held"
    path = http_mcp_mod._install_lock_path(workspace)
    context = mp.get_context("forkserver")
    entered = context.Event()
    release = context.Event()

    holder = context.Process(
        target=_lock_in_child, args=(workspace, tmp_path, entered, release)
    )
    holder.start()
    try:
        assert entered.wait(5)
        inode = path.stat().st_ino
        waiting = threading.Event()
        acquired = threading.Event()

        def wait_for_lock() -> None:
            waiting.set()
            with http_mcp_mod._workspace_install_lock(workspace):
                assert path.stat().st_ino == inode
                acquired.set()

        waiter = threading.Thread(target=wait_for_lock)
        waiter.start()
        assert waiting.wait(5)
        reaped = threading.Event()
        other_entered = threading.Event()

        def run_other() -> None:
            with http_mcp_mod._workspace_install_lock(tmp_path / "other"):
                assert path.stat().st_ino == inode
                other_entered.set()
            reaped.set()

        other = threading.Thread(target=run_other)
        other.start()
        assert other_entered.wait(5)
        assert not acquired.is_set()
        release.set()
        holder.join(5)
        waiter.join(5)
        other.join(5)
        assert reaped.is_set()
        assert holder.exitcode == 0
        assert acquired.is_set()
        assert not path.exists()
    finally:
        release.set()
        holder.join(5)


def test_install_lock_cleanup_does_not_wait_for_other_waiters(
    tmp_path: Path, monkeypatch
) -> None:
    import fcntl
    import threading

    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "ws"
    gate_path = http_mcp_mod._install_lock_path(workspace).parent / ".directory.lock"
    with http_mcp_mod._workspace_install_lock(workspace):
        pass
    finished = threading.Event()
    errors: list[Exception] = []
    with open(gate_path, "a+") as gate:
        fcntl.flock(gate, fcntl.LOCK_SH)

        def run() -> None:
            try:
                with http_mcp_mod._workspace_install_lock(
                    workspace, deadline=time.monotonic() + 0.1
                ):
                    pass
            except OSError as exc:
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert finished.wait(0.5), "cleanup waited on a shared gate past deadline"
            assert not errors
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)
            worker.join(2)


def test_install_lock_deadline_does_not_scan_after_release(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "ws"
    directory = http_mcp_mod._install_lock_path(workspace).parent
    directory.mkdir(mode=0o700)
    stale = directory / ("c" * 64 + ".lock")
    stale.touch(mode=0o600)

    with http_mcp_mod._workspace_install_lock(workspace, deadline=time.monotonic() + 1):
        pass
    assert stale.exists(), "deadline operation should not run best-effort scan"
    with http_mcp_mod._workspace_install_lock(tmp_path / "other"):
        pass
    assert not stale.exists()


def test_install_lock_cleanup_skips_contended_gate_without_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    import fcntl
    import threading

    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "ws"
    with http_mcp_mod._workspace_install_lock(workspace):
        pass
    gate_path = http_mcp_mod._install_lock_path(workspace).parent / ".directory.lock"
    finished = threading.Event()
    with open(gate_path, "a+") as gate:
        fcntl.flock(gate, fcntl.LOCK_SH)

        def run() -> None:
            with http_mcp_mod._workspace_install_lock(workspace):
                pass
            finished.set()

        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert finished.wait(0.5), "best-effort cleanup blocked the caller"
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)
            worker.join(2)


def test_install_lock_reaper_skips_unsafe_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "ws"
    directory = http_mcp_mod._install_lock_path(workspace).parent
    directory.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("keep")
    link = directory / ("a" * 64 + ".lock")
    link.symlink_to(target)
    fifo = directory / ("b" * 64 + ".lock")
    os.mkfifo(fifo, mode=0o600)
    stale = directory / ("c" * 64 + ".lock")
    stale.touch(mode=0o600)

    with http_mcp_mod._workspace_install_lock(workspace):
        pass
    assert link.is_symlink() and target.read_text() == "keep"
    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert not stale.exists()


def test_install_lock_reaper_opens_candidates_nonblocking(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(http_mcp_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "ws"
    directory = http_mcp_mod._install_lock_path(workspace).parent
    directory.mkdir(mode=0o700)
    fifo = directory / ("b" * 64 + ".lock")
    os.mkfifo(fifo, mode=0o600)
    real_open = http_mcp_mod.os.open

    def guarded_open(path, flags, *args, **kwargs):
        if path == fifo:
            assert flags & os.O_NONBLOCK
            assert flags & os.O_NOFOLLOW
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(http_mcp_mod.os, "open", guarded_open)
    with http_mcp_mod._workspace_install_lock(workspace):
        pass
    assert fifo.exists()


def test_install_lock_rejects_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    path = http_mcp_mod._install_lock_path(workspace)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    target = tmp_path / "untouched"
    target.write_text("secret")
    path.symlink_to(target)
    try:
        with pytest.raises(OSError), http_mcp_mod._workspace_install_lock(workspace):
            pass
        assert target.read_text() == "secret"
    finally:
        path.unlink()


def test_best_effort_install_noop_on_none_workspace(monkeypatch) -> None:
    def fail(workspace, custom_tools):
        raise AssertionError("must not be called for None workspace")

    monkeypatch.setattr(http_mcp_mod, "install_http_tools_mcp", fail)
    http_mcp_mod._best_effort_install(None, [])


def test_best_effort_install_delegates_on_success(tmp_path: Path, monkeypatch) -> None:
    seen: list[tuple[Path, list | None]] = []

    def fake_install(workspace, custom_tools):
        seen.append((workspace, custom_tools))

    monkeypatch.setattr(http_mcp_mod, "install_http_tools_mcp", fake_install)
    tools = [{"name": "t", "http": {"method": "GET", "url": "http://x"}}]
    http_mcp_mod._best_effort_install(tmp_path, tools)
    assert seen == [(tmp_path, tools)]


async def test_best_effort_async_install_swallows_errors(
    tmp_path: Path, monkeypatch
) -> None:
    def boom(workspace, custom_tools):
        raise OSError("disk full")

    monkeypatch.setattr(http_mcp_mod, "install_http_tools_mcp", boom)
    # Must not raise — analysis/chat continue without MCP.
    await http_mcp_mod.install_http_tools_mcp_best_effort_async(tmp_path, [])


async def test_best_effort_async_install_noop_on_none_workspace(monkeypatch) -> None:
    def fail(workspace, custom_tools):
        raise AssertionError("must not be called for None workspace")

    monkeypatch.setattr(http_mcp_mod, "_best_effort_install", fail)
    await http_mcp_mod.install_http_tools_mcp_best_effort_async(None, [])


async def test_best_effort_async_install_delegates_on_success(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list[tuple[Path, list | None]] = []

    def fake_install(workspace, custom_tools):
        seen.append((workspace, custom_tools))

    monkeypatch.setattr(http_mcp_mod, "_best_effort_install", fake_install)
    tools = [{"name": "t", "http": {"method": "GET", "url": "http://x"}}]
    await http_mcp_mod.install_http_tools_mcp_best_effort_async(tmp_path, tools)
    assert seen == [(tmp_path, tools)]


async def test_best_effort_async_serializes_per_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """Concurrent installs on one workspace must not overlap."""
    events: list[str] = []
    real = http_mcp_mod.install_http_tools_mcp

    def tracking_install(ws, ct, **kwargs):
        events.append("start")
        time.sleep(0.05)
        try:
            return real(ws, ct, **kwargs)
        finally:
            events.append("end")

    monkeypatch.setattr(http_mcp_mod, "install_http_tools_mcp", tracking_install)
    # Keep the test hermetic: never depend on a locally built sidecar dist.
    monkeypatch.setattr(
        http_mcp_mod, "resolve_http_tools_mcp_js", lambda: _mcp_js(tmp_path)
    )
    await asyncio.gather(
        *[
            http_mcp_mod.install_http_tools_mcp_best_effort_async(tmp_path / "ws", [])
            for _ in range(4)
        ]
    )
    # Strict alternation proves exactly one install ran at a time.
    assert events == ["start", "end"] * 4


async def test_concurrent_failed_install_preserves_successful_state(
    tmp_path: Path, monkeypatch
) -> None:
    """A failing install must not clobber a concurrent successful install."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tools = analysis_http_tools(
        server_url="http://localhost:8000",
        job_id="job-1",
        auth_header="Bearer super-secret-token",
    )

    real_write = http_mcp_mod._write_merged_json
    gemini_writes = {"n": 0}

    def boom_on_first_gemini_write(dest, payload):
        # Exactly one of the two concurrent installs fails mid-way (at its
        # Gemini config write), regardless of execution order.
        if dest.name == "settings.json" and dest.parent.name == ".gemini":
            gemini_writes["n"] += 1
            if gemini_writes["n"] == 1:
                raise OSError("disk full")
        return real_write(dest, payload)

    monkeypatch.setattr(http_mcp_mod, "_write_merged_json", boom_on_first_gemini_write)
    # Hermetic: the real installer must find a stub binary, not whatever
    # happens to exist in the developer's sidecar-helper/dist.
    monkeypatch.setattr(
        http_mcp_mod, "resolve_http_tools_mcp_js", lambda: _mcp_js(tmp_path)
    )

    await asyncio.gather(
        *[
            http_mcp_mod.install_http_tools_mcp_best_effort_async(workspace, tools)
            for _ in range(2)
        ]
    )

    assert gemini_writes["n"] >= 1
    # Final state is a fully valid successful install regardless of ordering:
    # the lock serializes the two installs, so the failing one snapshots and
    # rolls back to the successful one's state instead of clobbering it.
    assert MCP_SERVER_NAME in _read(workspace / ".mcp.json")["mcpServers"]
    dump = json.loads(http_tools_dump_path(workspace).read_text(encoding="utf-8"))
    assert dump and all(t.get("name") for t in dump)


async def test_best_effort_async_fallback_lock_without_fcntl(
    tmp_path: Path, monkeypatch
) -> None:
    """Without fcntl (Windows), installs still serialize process-locally."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("fcntl blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    events: list[str] = []
    real = http_mcp_mod.install_http_tools_mcp

    def tracking_install(ws, ct, **kwargs):
        events.append("start")
        time.sleep(0.02)
        try:
            return real(ws, ct, **kwargs)
        finally:
            events.append("end")

    monkeypatch.setattr(http_mcp_mod, "install_http_tools_mcp", tracking_install)
    monkeypatch.setattr(
        http_mcp_mod, "resolve_http_tools_mcp_js", lambda: _mcp_js(tmp_path)
    )
    await asyncio.gather(
        *[
            http_mcp_mod.install_http_tools_mcp_best_effort_async(tmp_path / "ws", [])
            for _ in range(3)
        ]
    )
    assert events == ["start", "end"] * 3
