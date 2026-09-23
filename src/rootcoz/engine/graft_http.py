"""Process-local, loopback-only HTTP bridge for read-only Graft queries.

The bearer token lives in memory and in the existing private HTTP MCP dump,
never in an agent prompt or a workspace file. Only registered cloned roots may
be queried. No Rootcoz API route is installed.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Contract with graft.py: query_repo(workspace, scope, tool, params).
# The graph module enforces snapshot containment and the fixed command allowlist.
_FIELDS: dict[str, tuple[str, ...]] = {
    "graft_find_code": ("query", "limit", "path"),
    "graft_find_all": ("pattern", "path"),
    "graft_file_api": ("path",),
    "graft_trace_calls": ("symbol", "direction", "depth"),
    "graft_repo_map": ("max_dirs",),
    "graft_check_freshness": (),
}
_REQUIRED = {
    "graft_find_code": ("query",),
    "graft_find_all": ("pattern",),
    "graft_file_api": ("path",),
    "graft_trace_calls": ("symbol",),
}
_SKILL_LOCATIONS = (".pi", ".agents", ".cursor", ".claude", ".gemini")
logger = logging.getLogger(__name__)
_MAX_BODY = 8192
_MAX_RESULT = 65536
_lock = threading.RLock()
_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
# workspace -> (token, {public repo name: resolved clone root})
_sessions: dict[Path, tuple[str, dict[str, Path]]] = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Never log request paths, headers, bodies, tokens or query text.
        pass

    def _reply(self, status: int, payload: object) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        if len(data) > _MAX_RESULT:
            status, data = (
                413,
                b'{"status":"fallback","reason":"graph output exceeds 64 KiB; use read/ls/find/grep","truncated":true}',
            )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path not in {f"/query/{name}" for name in _FIELDS}:
            self._reply(404, {"error": "not found"})
            return
        bearer = self.headers.get("Authorization", "")
        with _lock:
            session = next(
                (
                    (workspace, *entry)
                    for workspace, entry in _sessions.items()
                    if hmac.compare_digest(bearer, f"Bearer {entry[0]}")
                ),
                None,
            )
        if session is None:
            self._reply(401, {"error": "unauthorized"})
            return
        length = self.headers.get("Content-Length", "")
        if (
            not length.isdecimal()
            or not 0 < int(length) <= _MAX_BODY
            or self.headers.get("Transfer-Encoding")
        ):
            self._reply(400, {"error": "invalid body"})
            return
        try:
            body = json.loads(self.rfile.read(int(length)))
            name = self.path.removeprefix("/query/")
            fields = _FIELDS[name]
            if (
                not isinstance(body, dict)
                or set(body) != {"repo", *fields}
                or not isinstance(body["repo"], str)
                or any(
                    not isinstance(body[field], str) or len(body[field]) > 2048
                    for field in fields
                )
                or any(not body[field].strip() for field in _REQUIRED.get(name, ()))
            ):
                raise ValueError
            for field in fields:
                if (
                    field not in _REQUIRED.get(name, ())
                    and body[field] == "{" + field + "}"
                ):
                    body[field] = ""
            root = session[2].get(body["repo"])
            if (
                root is None
                or not root.is_dir()
                or (session[0] / body["repo"]).is_symlink()
                or (session[0] / body["repo"]).resolve() != root
            ):
                raise ValueError
            # Never let a path parameter escape the registered clone. Graft also
            # checks symlinks and containment before it reads the file.
            for field in ("path",):
                if (
                    field in body
                    and body[field]
                    and (
                        Path(body[field]).is_absolute()
                        or ".." in Path(body[field]).parts
                    )
                ):
                    raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "invalid request"})
            return
        try:
            from rootcoz.engine import graft

            params = {field: body[field] for field in fields if body[field]}
            for field in ("limit", "depth", "max_dirs"):
                if field in params:
                    params[field] = int(params[field])
            result = graft.query_repo(
                session[0], body["repo"], name.removeprefix("graft_"), params
            )
            self._reply(200, result)
        except (ValueError, TypeError):
            self._reply(400, {"error": "invalid request"})
        except Exception:  # noqa: BLE001 - never expose graph errors containing source text or host paths
            self._reply(500, {"error": "graph query failed"})


def build_graph_tools(
    base_url: str, token: str, repos: list[str]
) -> list[dict[str, Any]]:
    """HTTP tool definitions; pass only to sidecar and the private MCP dump."""
    if not base_url or not token or not repos:
        return []
    tools = []
    for name, fields in _FIELDS.items():
        tools.append(
            {
                "name": name,
                "description": f"Read-only graph query {name} for a registered repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "enum": repos},
                        **{field: {"type": "string"} for field in fields},
                    },
                    "required": ["repo", *_REQUIRED.get(name, ())],
                },
                "http": {
                    "method": "POST",
                    "url": f"{base_url}/query/{name}",
                    "headers": {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    "body_template": {
                        "repo": "{repo}",
                        **{field: "{" + field + "}" for field in fields},
                    },
                    "timeout_ms": 10000,
                },
            }
        )
    return tools


def _skill_source() -> Path:
    return Path(__file__).resolve().parents[1] / "agents" / "graft" / "SKILL.md"


def install_graph_skill(workspace: Path) -> bool:
    """Install service-owned guidance in native Pi and nested CLI discovery paths.

    Never follow workspace symlinks or replace an existing customized skill.
    If any location is unsafe, graph tools stay unavailable for this session.
    """
    source = _skill_source().read_bytes()
    destinations = [
        workspace / location / "skills" / "rootcoz-graft" / "SKILL.md"
        for location in _SKILL_LOCATIONS
    ]
    try:
        if workspace.is_symlink() or not workspace.is_dir():
            return False
        for destination in destinations:
            for directory in (
                destination.parent.parent.parent,
                destination.parent.parent,
                destination.parent,
            ):
                if directory.is_symlink() or (
                    directory.exists() and not directory.is_dir()
                ):
                    return False
            if destination.is_symlink() or (
                destination.exists() and destination.read_bytes() != source
            ):
                return False
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                destination.write_bytes(source)
        return True
    except OSError:
        logger.warning("Unable to install Graft skill; graph tools disabled")
        return False


def graph_guidance() -> str:
    """Load fixed service-owned guidance, never cloned-repository instructions."""
    return _skill_source().read_text(encoding="utf-8").split("---", 2)[-1].strip()


def cloned_graph_roots(workspace: Path) -> dict[str, Path]:
    """Only direct Git clones in this session workspace are eligible."""
    if not workspace.is_dir():
        return {}
    return {
        item.name: item
        for item in workspace.iterdir()
        if item.is_dir() and not item.is_symlink() and (item / ".git").exists()
    }


def register_workspace(workspace: Path, repos: dict[str, Path]) -> list[dict[str, Any]]:
    """Register cloned roots and return tools, or [] when loopback is unavailable."""
    global _server, _thread
    try:
        workspace = workspace.resolve(strict=True)
        roots = {name: path.resolve(strict=True) for name, path in repos.items()}
        if not roots or any(
            not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or not root.is_dir()
            or root != workspace / name
            or (workspace / name).is_symlink()
            for name, root in roots.items()
        ):
            return []
        if not install_graph_skill(workspace):
            return []
        with _lock:
            if _server is None:
                _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
                _server.daemon_threads = True
                _thread = threading.Thread(target=_server.serve_forever, daemon=True)
                try:
                    _thread.start()
                except RuntimeError:
                    _server.server_close()
                    _server, _thread = None, None
                    return []
            existing = _sessions.get(workspace)
            token = (
                existing[0]
                if existing is not None and existing[1] == roots
                else secrets.token_urlsafe(32)
            )
            _sessions[workspace] = (token, roots)
            return build_graph_tools(
                f"http://127.0.0.1:{_server.server_port}", token, list(roots)
            )
    except (OSError, RuntimeError, ValueError):
        return []


def unregister_workspace(workspace: Path) -> None:
    """Revoke a workspace token and stop the listener after the last session."""
    global _server, _thread
    with _lock:
        _sessions.pop(workspace.resolve(), None)
        if not _sessions and _server is not None:
            server, thread = _server, _thread
            _server, _thread = None, None
            server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=2)
