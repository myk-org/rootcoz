"""Tests for CLI/acpx HTTP MCP install from path-specific custom_tools."""

import json
import stat
from pathlib import Path

from rootcoz.engine.chat import (
    analysis_http_tools,
    build_admin_custom_tools,
    build_chat_custom_tools,
)
from rootcoz.engine.http_mcp import MCP_SERVER_NAME, install_http_tools_mcp


def _mcp_js(tmp_path: Path) -> Path:
    js = tmp_path / "http-tools-mcp.js"
    js.write_text("// stub\n", encoding="utf-8")
    return js


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
    assert dump.parent == tmp_path
    assert dump.name == ".ws.rootcoz-http-tools.json"
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
    assert claude_settings["enableAllProjectMcpServers"] is True


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
