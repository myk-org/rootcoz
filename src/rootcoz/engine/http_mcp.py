"""Install sidecar HTTP custom tools as cwd MCP configs for CLI/acpx agents.

The MCP server (`sidecar-helper` ``http-tools-mcp.js``) executes the same
HTTP tool defs pi-sidecar uses. Each analysis/chat/admin path passes its
existing builder output — no second allowlist.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_logger.logger import get_logger

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms only
    fcntl = None

logger = get_logger(name=__name__)

MCP_SERVER_NAME = "rootcoz-http"
_TOOLS_FILE_ENV = "ROOTCOZ_HTTP_TOOLS_FILE"
_MCP_JS_ENV = "ROOTCOZ_HTTP_TOOLS_MCP"
_MAX_CONFIG_MODE = 0o644
_NEW_CONFIG_MODE = stat.S_IRUSR | stat.S_IWUSR
_DUMP_MODE = stat.S_IRUSR | stat.S_IWUSR
_UNRESOLVABLE_PATH = (OSError, RuntimeError)
_MCP_RELATIVES = (
    Path(".cursor") / "mcp.json",
    Path(".mcp.json"),
    Path(".claude") / "settings.json",
    Path(".gemini") / "settings.json",
)


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


def http_tools_dump_path(workspace: Path) -> Path:
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


def _dir_inside_workspace(
    directory: Path, workspace: Path, *, create: bool
) -> Path | None:
    """Resolve *directory* if it is inside *workspace*; otherwise None."""
    try:
        workspace_resolved = workspace.resolve()
        if directory.exists():
            if not directory.is_dir():
                logger.warning(
                    "Refusing MCP path that is not a directory: %s", directory
                )
                return None
            resolved = directory.resolve()
        elif create:
            directory.mkdir(parents=True, exist_ok=True)
            resolved = directory.resolve()
        else:
            return None
    except _UNRESOLVABLE_PATH:
        return None
    if resolved == workspace_resolved or resolved.is_relative_to(workspace_resolved):
        return resolved
    logger.warning(
        "Refusing MCP path outside workspace %s: %s -> %s",
        workspace,
        directory,
        resolved,
    )
    return None


def _atomic_write_bytes(dest: Path, data: bytes, *, mode: int, parent: Path) -> None:
    """Write *dest* via rename in *parent* so readers never see a truncated file.

    ``os.replace`` replaces a destination symlink instead of following it.
    """
    if parent.exists() and not parent.is_dir():
        raise NotADirectoryError(f"MCP config parent is not a directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".rootcoz-mcp-", dir=str(parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_text(dest: Path, text: str, *, mode: int, parent: Path) -> None:
    _atomic_write_bytes(dest, text.encode("utf-8"), mode=mode, parent=parent)


def _unlink_if_symlink(path: Path) -> None:
    if path.is_symlink():
        path.unlink()


def _load_json_object(path: Path) -> tuple[dict[str, Any], bool]:
    """Load a JSON object. Returns ``(data, malformed)``.

    Missing files are ``({}, False)``. Unreadable, non-object, or invalid JSON
    is ``({}, True)`` so cleanup can leave the original file in place.
    """
    _unlink_if_symlink(path)
    if not path.is_file():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Ignoring malformed MCP JSON at %s", path)
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return data, False


def _config_file_mode(dest: Path) -> int:
    """Keep an existing file's mode, with bits outside 0644 stripped. New files are 0600."""
    if dest.is_file() and not dest.is_symlink():
        return dest.stat().st_mode & _MAX_CONFIG_MODE
    return _NEW_CONFIG_MODE


def _with_enabled_rootcoz_server(settings: dict[str, Any]) -> dict[str, Any]:
    merged = dict(settings)
    enabled = merged.get("enabledMcpjsonServers")
    names = (
        [item for item in enabled if isinstance(item, str)]
        if isinstance(enabled, list)
        else []
    )
    if MCP_SERVER_NAME not in names:
        names.append(MCP_SERVER_NAME)
    merged["enabledMcpjsonServers"] = names
    return merged


def _without_enabled_rootcoz_server(settings: dict[str, Any]) -> dict[str, Any]:
    merged = dict(settings)
    enabled = merged.get("enabledMcpjsonServers")
    if isinstance(enabled, list):
        names = [item for item in enabled if item != MCP_SERVER_NAME]
        if names:
            merged["enabledMcpjsonServers"] = names
        else:
            merged.pop("enabledMcpjsonServers", None)
    return merged


def _merge_mcp_servers(
    existing: dict[str, Any], server_entry: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(existing)
    servers = merged.get("mcpServers")
    servers = dict(servers) if isinstance(servers, dict) else {}
    servers[MCP_SERVER_NAME] = server_entry
    merged["mcpServers"] = servers
    return merged


def _drop_rootcoz_server(existing: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    servers = merged.get("mcpServers")
    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        servers = dict(servers)
        del servers[MCP_SERVER_NAME]
        if servers:
            merged["mcpServers"] = servers
        else:
            merged.pop("mcpServers", None)
    return merged


def _config_dest(workspace: Path, relative: Path, *, create: bool) -> Path | None:
    parent = _dir_inside_workspace(
        workspace / relative.parent, workspace, create=create
    )
    if parent is None:
        return None
    dest = parent / relative.name
    _unlink_if_symlink(dest)
    return dest


def _write_merged_json(dest: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(
        dest,
        json.dumps(payload, indent=2) + "\n",
        mode=_config_file_mode(dest),
        parent=dest.parent,
    )


@dataclass(frozen=True)
class _PathSnapshot:
    path: Path
    existed: bool
    is_symlink: bool
    link_target: str | None
    data: bytes | None
    mode: int | None
    skipped: bool
    require_inside: bool


def _destination_parent_in_workspace(path: Path, workspace: Path) -> bool:
    """True when each existing parent component stays inside *workspace*.

    Missing intermediate directories are allowed. A parent symlink that
    resolves outside the workspace is not.
    """
    try:
        workspace_resolved = workspace.resolve()
        relative = path.parent.relative_to(workspace)
    except (ValueError, *_UNRESOLVABLE_PATH):
        return False
    current = workspace_resolved
    for part in relative.parts:
        candidate = current / part
        if candidate.is_symlink() or candidate.exists():
            try:
                resolved = candidate.resolve()
            except _UNRESOLVABLE_PATH:
                return False
            if resolved != workspace_resolved and not resolved.is_relative_to(
                workspace_resolved
            ):
                return False
            current = resolved
        else:
            current = candidate
    return True


def _snapshot_file(
    path: Path, workspace: Path, *, require_inside: bool
) -> _PathSnapshot:
    empty = _PathSnapshot(path, False, False, None, None, None, False, require_inside)
    if require_inside and not _destination_parent_in_workspace(path, workspace):
        logger.warning("Skipping MCP snapshot outside workspace: %s", path)
        return _PathSnapshot(path, False, False, None, None, None, True, True)
    if path.is_symlink():
        return _PathSnapshot(
            path, True, True, os.readlink(path), None, None, False, require_inside
        )
    if not path.is_file():
        return empty
    return _PathSnapshot(
        path,
        True,
        False,
        None,
        path.read_bytes(),
        path.stat().st_mode & 0o777,
        False,
        require_inside,
    )


def _restore_file(snap: _PathSnapshot, workspace: Path) -> None:
    if snap.skipped:
        return
    if snap.require_inside and not _destination_parent_in_workspace(
        snap.path, workspace
    ):
        logger.warning("Skipping MCP restore outside workspace: %s", snap.path)
        return
    if snap.path.is_symlink() or snap.path.is_file():
        snap.path.unlink(missing_ok=True)
    if not snap.existed:
        return
    if snap.is_symlink:
        if snap.link_target is None:
            return
        snap.path.symlink_to(snap.link_target)
        return
    if snap.data is None:
        return
    mode = snap.mode if snap.mode is not None else _NEW_CONFIG_MODE
    _atomic_write_bytes(snap.path, snap.data, mode=mode, parent=snap.path.parent)


def _snapshot_install_paths(workspace: Path) -> list[_PathSnapshot]:
    return [
        _snapshot_file(
            http_tools_dump_path(workspace), workspace, require_inside=False
        ),
        *(
            _snapshot_file(workspace / relative, workspace, require_inside=True)
            for relative in _MCP_RELATIVES
        ),
    ]


def _write_merged_if_valid(
    dest: Path | None,
    payload_from: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    if dest is None:
        return
    existing, malformed = _load_json_object(dest)
    if malformed:
        logger.warning("Skipping MCP merge; malformed JSON at %s", dest)
        return
    _write_merged_json(dest, payload_from(existing))


def _install_mcp_configs(
    workspace: Path,
    cursor_entry: dict[str, Any],
    claude_entry: dict[str, Any],
    gemini_entry: dict[str, Any],
) -> None:
    cursor_dest = _config_dest(workspace, Path(".cursor") / "mcp.json", create=True)
    _write_merged_if_valid(
        cursor_dest, lambda existing: _merge_mcp_servers(existing, cursor_entry)
    )

    claude_dest = _config_dest(workspace, Path(".mcp.json"), create=True)
    _write_merged_if_valid(
        claude_dest, lambda existing: _merge_mcp_servers(existing, claude_entry)
    )

    claude_settings = _config_dest(
        workspace, Path(".claude") / "settings.json", create=True
    )
    _write_merged_if_valid(claude_settings, _with_enabled_rootcoz_server)

    gemini_dest = _config_dest(
        workspace, Path(".gemini") / "settings.json", create=True
    )
    _write_merged_if_valid(
        gemini_dest, lambda existing: _merge_mcp_servers(existing, gemini_entry)
    )


def _remove_managed_mcp_configs(workspace: Path) -> None:
    for relative in (
        Path(".cursor") / "mcp.json",
        Path(".mcp.json"),
        Path(".gemini") / "settings.json",
    ):
        dest = _config_dest(workspace, relative, create=False)
        if dest is None or not dest.is_file():
            continue
        existing, malformed = _load_json_object(dest)
        if malformed:
            continue
        remaining = _drop_rootcoz_server(existing)
        if remaining:
            _write_merged_json(dest, remaining)
        else:
            dest.unlink(missing_ok=True)

    claude_settings = _config_dest(
        workspace, Path(".claude") / "settings.json", create=False
    )
    if claude_settings is None or not claude_settings.is_file():
        return
    settings, malformed = _load_json_object(claude_settings)
    if malformed:
        return
    remaining = _without_enabled_rootcoz_server(settings)
    if remaining:
        _write_merged_json(claude_settings, remaining)
    else:
        claude_settings.unlink(missing_ok=True)


def _remove_tools_dump(workspace: Path) -> None:
    dump = http_tools_dump_path(workspace)
    _unlink_if_symlink(dump)
    dump.unlink(missing_ok=True)


def cleanup_http_tools_mcp(workspace: Path | None) -> None:
    """Remove the credential dump and managed ``rootcoz-http`` MCP entries."""
    if workspace is None:
        return
    _remove_tools_dump(workspace)
    if workspace.exists():
        _remove_managed_mcp_configs(workspace)


def install_http_tools_mcp(
    workspace: Path | None,
    custom_tools: list[dict[str, Any]] | None,
    *,
    mcp_js: Path | None = None,
) -> Path | None:
    """Write per-CLI MCP configs for this session's HTTP ``custom_tools``.

    Empty or unusable tool lists remove a previous Rootcoz MCP install.
    Workspace MCP JSON never embeds Bearer tokens — only a path to the dump.

    Returns:
        Path to the tools dump, or None when install was skipped.
    """
    if workspace is None:
        return None
    resolved_js = mcp_js if mcp_js is not None else resolve_http_tools_mcp_js()
    http_tools = [t for t in (custom_tools or []) if t.get("name") and t.get("http")]
    if not http_tools or resolved_js is None:
        if resolved_js is None and http_tools:
            logger.warning(
                "HTTP MCP server binary not found; CLI/acpx sessions will not "
                "see sidecar HTTP tools"
            )
        cleanup_http_tools_mcp(workspace)
        return None

    tools_file = http_tools_dump_path(workspace)
    snapshots = _snapshot_install_paths(workspace)
    try:
        _unlink_if_symlink(tools_file)
        _atomic_write_text(
            tools_file,
            json.dumps(http_tools),
            mode=_DUMP_MODE,
            parent=tools_file.parent,
        )

        names = [str(t["name"]) for t in http_tools]
        cursor_entry = _server_entry(resolved_js, tools_file)
        claude_entry = {
            "type": "stdio",
            **_server_entry(resolved_js, tools_file),
        }
        gemini_entry = _server_entry(resolved_js, tools_file, include_tools=names)
        _install_mcp_configs(workspace, cursor_entry, claude_entry, gemini_entry)
    except Exception:
        for snap in reversed(snapshots):
            _restore_file(snap, workspace)
        raise
    logger.info(
        "Installed HTTP MCP (%d tools) for CLI/acpx in %s",
        len(http_tools),
        workspace,
    )
    return tools_file


def _best_effort_install(
    workspace: Path | None,
    custom_tools: list[dict[str, Any]] | None,
) -> None:
    """Run the installer under a per-workspace lock, logging failures."""
    if workspace is None:
        return
    try:
        with _workspace_install_lock(workspace):
            install_http_tools_mcp(workspace, custom_tools)
    except Exception:
        logger.warning(
            "HTTP tools MCP install failed for %s; continuing without MCP",
            workspace,
            exc_info=True,
        )


def _install_lock_path(workspace: Path) -> Path:
    """Lock file next to the tools dump; serializes installs per workspace."""
    return workspace.parent / f".{workspace.name}.rootcoz-http-mcp.lock"


@contextmanager
def _workspace_install_lock(workspace: Path):
    """Serialize MCP installs per workspace across threads and processes.

    Install rollback restores snapshot contents, so overlapping installs on
    one workspace can clobber each other's configs and tools dump (parallel
    failure-group analysis shares ``workspace_dir``). The lock covers the
    whole snapshot-write-rollback cycle so exactly one install runs at a time.
    POSIX platforms use an flock'd lock file; without :mod:`fcntl` (Windows),
    falls back to process-local serialization keyed by workspace.
    """
    if fcntl is None:
        with _fallback_workspace_lock(workspace):
            yield
        return
    path = _install_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# Process-local fallback locks for platforms without :mod:`fcntl` (Windows).
# Weaker than flock — no cross-process protection — but still serializes the
# concurrent thread-offloaded installs that make rollback race.
_fallback_locks_guard = threading.Lock()
_fallback_locks: dict[str, threading.Lock] = {}


def _fallback_workspace_lock(workspace: Path) -> threading.Lock:
    key = str(workspace.resolve())
    with _fallback_locks_guard:
        lock = _fallback_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _fallback_locks[key] = lock
        return lock


async def install_http_tools_mcp_best_effort_async(
    workspace: Path | None,
    custom_tools: list[dict[str, Any]] | None,
) -> None:
    """Awaitable best-effort install that keeps blocking I/O off the loop.

    The installer performs synchronous filesystem work (JSON writes with
    ``fsync``), so async analysis/chat flows offload it to a worker thread.
    The work is awaited — MCP configs must exist before sessions start —
    never fire-and-forget.
    """
    if workspace is None:
        return
    await asyncio.to_thread(_best_effort_install, workspace, custom_tools)
