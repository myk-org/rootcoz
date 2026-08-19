"""Install sidecar HTTP custom tools as cwd MCP configs for CLI/acpx agents.

The MCP server (`sidecar-helper` ``http-tools-mcp.js``) executes the same
HTTP tool defs pi-sidecar uses. Each analysis/chat/admin path passes its
existing builder output — no second allowlist.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from simple_logger.logger import get_logger

logger = get_logger(name=__name__)

MCP_SERVER_NAME = "rootcoz-http"
_TOOLS_FILE_ENV = "ROOTCOZ_HTTP_TOOLS_FILE"
_MCP_JS_ENV = "ROOTCOZ_HTTP_TOOLS_MCP"


def resolve_http_tools_mcp_js() -> Path | None:
    """Locate the compiled MCP stdio server, or None if it is missing."""
    env = os.environ.get(_MCP_JS_ENV, "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/app/sidecar-helper/dist/http-tools-mcp.js"))
    repo_dist = (
        Path(__file__).resolve().parents[3]
        / "sidecar-helper"
        / "dist"
        / "http-tools-mcp.js"
    )
    candidates.append(repo_dist)
    for path in candidates:
        if path.is_file():
            return path
    return None


def _tools_dump_path(workspace: Path) -> Path:
    """Sidecar file next to the workspace (not inside cwd — AI can ``read`` cwd)."""
    return workspace.parent / f".{workspace.name}.rootcoz-http-tools.json"


def _mcp_command(mcp_js: Path) -> tuple[str, list[str]]:
    node = os.environ.get("NODE_BINARY", "").strip() or shutil.which("node") or "node"
    return node, [str(mcp_js)]


def _server_entry(
    mcp_js: Path, tools_file: Path, *, include_tools: list[str] | None = None
) -> dict[str, Any]:
    node, args = _mcp_command(mcp_js)
    entry: dict[str, Any] = {
        "command": node,
        "args": args,
        "env": {_TOOLS_FILE_ENV: str(tools_file)},
    }
    if include_tools is not None:
        entry["trust"] = True
        entry["includeTools"] = include_tools
    return entry


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def install_http_tools_mcp(
    workspace: Path | None,
    custom_tools: list[dict[str, Any]] | None,
    *,
    mcp_js: Path | None = None,
) -> Path | None:
    """Write per-CLI MCP configs for this session's HTTP ``custom_tools``.

    Empty tool lists skip install (no MCP server advertised). Workspace MCP
    JSON never embeds Bearer tokens — only a path to the tools dump.

    Returns:
        Path to the tools dump, or None when install was skipped.
    """
    if workspace is None or not custom_tools:
        return None
    resolved_js = mcp_js if mcp_js is not None else resolve_http_tools_mcp_js()
    if resolved_js is None:
        logger.warning(
            "HTTP MCP server binary not found; CLI/acpx sessions will not "
            "see sidecar HTTP tools"
        )
        return None

    http_tools = [t for t in custom_tools if t.get("name") and t.get("http")]
    if not http_tools:
        return None

    tools_file = _tools_dump_path(workspace)
    tools_file.write_text(json.dumps(http_tools), encoding="utf-8")
    tools_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    names = [str(t["name"]) for t in http_tools]
    cursor_entry = _server_entry(resolved_js, tools_file)
    claude_entry = {
        "type": "stdio",
        **_server_entry(resolved_js, tools_file),
    }
    gemini_entry = _server_entry(resolved_js, tools_file, include_tools=names)

    _write_json(
        workspace / ".cursor" / "mcp.json",
        {"mcpServers": {MCP_SERVER_NAME: cursor_entry}},
    )
    _write_json(
        workspace / ".mcp.json", {"mcpServers": {MCP_SERVER_NAME: claude_entry}}
    )
    _write_json(
        workspace / ".claude" / "settings.json",
        {"enableAllProjectMcpServers": True},
    )
    _write_json(
        workspace / ".gemini" / "settings.json",
        {"mcpServers": {MCP_SERVER_NAME: gemini_entry}},
    )
    logger.info(
        "Installed HTTP MCP (%d tools) for CLI/acpx in %s",
        len(http_tools),
        workspace,
    )
    return tools_file
