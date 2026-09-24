"""Bounded, read-only source snapshots for Graft. No graph data lives in the AI cwd."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path
from typing import Any

from rootcoz.engine.http_mcp import _workspace_install_lock

MAX_FILES = 2000
MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 30
BUILD_SECONDS = 120
QUERY_SECONDS = 45
MAX_REPOSITORIES = 10  # includes the primary test repository
MAX_OUTPUT = 1024 * 1024
MAX_GRAPH_BYTES = 40 * 1024 * 1024
GRAFT = "/app/sidecar-helper/node_modules/.bin/graft"


class _Limit(ValueError):
    pass


def _root(workspace: Path) -> Path:
    workspace = workspace.resolve()
    return workspace.parent / f".{workspace.name}.rootcoz-graft"


def _disk_size(path: Path, deadline: float | None = None) -> int:
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for file in dirs + files:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("graph deadline")
            entry = Path(directory) / file
            if entry.is_symlink():
                raise ValueError("graph contains symlink")
            total += entry.lstat().st_size
            if total > MAX_GRAPH_BYTES:
                return total
    return total


def _bounded(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    disk: Path | None = None,
) -> bytes:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env={**os.environ, "DO_NOT_TRACK": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("subprocess timed out")
                if disk is not None and _disk_size(disk, deadline) > MAX_GRAPH_BYTES:
                    raise _Limit("graph disk limit")
                for key, _ in selector.select(
                    timeout=min(0.1, max(0.001, deadline - time.monotonic()))
                ):
                    chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(output)))
                    if chunk:
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            raise _Limit("subprocess output limit")
                    else:
                        selector.unregister(key.fileobj)
        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, argv)
        return bytes(output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        if process.stdout:
            process.stdout.close()


def _run(
    argv: list[str], *, cwd: Path, timeout: float, disk: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        argv, 0, _bounded(argv, cwd=cwd, timeout=timeout, disk=disk).decode("utf-8"), ""
    )


def _relative(path: str) -> Path:
    relative = Path(path)
    if (
        not path
        or relative.is_absolute()
        or any(part in ("..", ".") for part in relative.parts)
        or path.startswith("-")
        or "\\" in path
        or "\x00" in path
    ):
        raise ValueError("invalid path")
    return relative


def _safe_file(repo: Path, relative: Path) -> Path | None:
    try:
        current = repo
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        if not current.is_file() or not current.resolve().is_relative_to(
            repo.resolve()
        ):
            return None
        return current
    except (OSError, RuntimeError):
        return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("graph deadline")
    return remaining


def _sources(repo: Path, deadline: float | None = None) -> dict[str, str]:
    """Git-visible files only; hash actual content, including ignored-file changes in the listing."""
    listing = _bounded(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=repo,
        timeout=min(MAX_SECONDS, _remaining(deadline)) if deadline else MAX_SECONDS,
    )
    if len(listing) > MAX_OUTPUT or len(listing.split(b"\0")) - 1 > MAX_FILES:
        raise _Limit("file listing limit")
    result = {}
    total = 0
    started = time.monotonic()
    for raw in listing.split(b"\0"):
        if time.monotonic() - started > MAX_SECONDS or (
            deadline and time.monotonic() >= deadline
        ):
            raise TimeoutError("source scan timed out")
        if not raw:
            continue
        try:
            relative = _relative(os.fsdecode(raw))
        except ValueError:
            continue
        source = _safe_file(repo, relative)
        if source is None:
            continue
        with open(
            source,
            "rb",
            opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW | os.O_NONBLOCK),
        ) as src:
            info = os.fstat(src.fileno())
            if not stat.S_ISREG(info.st_mode):
                continue
            total += info.st_size
            if total > MAX_BYTES:
                raise _Limit("source byte limit")
            data = src.read(MAX_BYTES + 1)
            if len(data) != info.st_size:
                raise ValueError("source changed")
            if deadline is not None:
                _remaining(deadline)
            result[str(relative)] = hashlib.sha256(data).hexdigest()
    if deadline is not None:
        _remaining(deadline)
    return result


def _valid_repo(workspace: Path, name: str, repo: Path) -> bool:
    return (
        bool(name)
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and not repo.is_symlink()
        and repo.resolve() == workspace / name
        and repo.is_dir()
        and (repo / ".git").exists()
    )


def _manifest(destination: Path) -> dict[str, str] | None:
    try:
        if (
            destination.is_symlink()
            or (destination / "snapshot").is_symlink()
            or (destination / "graph").is_symlink()
            or (destination / "manifest.json").is_symlink()
            or not (destination / "graph").is_dir()
            or not (destination / "snapshot").is_dir()
        ):
            return None
        manifest = json.loads((destination / "manifest.json").read_text())
        if (
            not isinstance(manifest, dict)
            or not manifest
            or any(
                not isinstance(p, str) or not isinstance(h, str) or len(h) != 64
                for p, h in manifest.items()
            )
        ):
            return None
        return manifest
    except (OSError, ValueError):
        return None


def _index_one(
    workspace: Path, root: Path, name: str, repo_value: Path
) -> dict[str, Any]:
    repo = Path(repo_value)
    if not _valid_repo(workspace, name, repo):
        return {"status": "skipped", "reason": "invalid repository"}
    destination = root / name
    try:
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            return {"status": "failed", "reason": "invalid graph root"}
        if destination.is_symlink():
            return {"status": "failed", "reason": "invalid graph directory"}
        manifest = _sources(repo)
        if not manifest:
            if destination.exists():
                shutil.rmtree(destination)
            return {"status": "skipped", "reason": "no eligible files"}
        if (
            manifest == _manifest(destination)
            and _disk_size(destination) <= MAX_GRAPH_BYTES
        ):
            return {"status": "unchanged", "files": len(manifest)}
        if destination.exists():
            shutil.rmtree(destination)
        snapshot = destination / "snapshot"
        graph = destination / "graph"
        snapshot.mkdir(parents=True, mode=0o700)
        total = 0
        for path, digest in manifest.items():
            source = _safe_file(repo, _relative(path))
            if source is None:
                raise ValueError("source changed")
            target = snapshot / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(
                source,
                "rb",
                opener=lambda p, flags: os.open(
                    p, flags | os.O_NOFOLLOW | os.O_NONBLOCK
                ),
            ) as src:
                data = src.read(MAX_BYTES + 1)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("source changed")
            total += len(data)
            if total > MAX_BYTES:
                raise _Limit("source byte limit")
            target.write_bytes(data)
        _run(
            [GRAFT, "--dir", str(graph), "build", str(snapshot)],
            cwd=snapshot,
            timeout=BUILD_SECONDS,
            disk=destination,
        )
        if (
            not graph.is_dir()
            or graph.is_symlink()
            or _disk_size(destination) > MAX_GRAPH_BYTES
        ):
            raise _Limit("graph disk limit")
        if _sources(repo) != manifest:
            raise ValueError("source changed")
        (destination / "manifest.json").write_text(json.dumps(manifest))
        return {"status": "indexed", "files": len(manifest), "bytes": total}
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        if not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)
        return {
            "status": "failed",
            "reason": type(exc).__name__,
            "truncated": isinstance(exc, _Limit),
        }


def index_repositories(
    workspace: Path, cloned_repos: dict[str, Path]
) -> dict[str, Any]:
    """Build up to ten repositories per workspace, with two concurrent builds."""
    workspace = Path(workspace).resolve()
    names = iter(cloned_repos)
    selected = list(islice(names, MAX_REPOSITORIES))
    with _workspace_install_lock(workspace), ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(
                _index_one, workspace, _root(workspace), name, cloned_repos[name]
            )
            for name in selected
        }
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - isolate and sanitize unexpected worker failures
                results[name] = {"status": "failed", "reason": type(exc).__name__}
    results.update(
        {name: {"status": "skipped", "reason": "repository limit"} for name in names}
    )
    return results


def log_index_outcomes(outcomes: dict[str, Any]) -> None:
    """Log only safe repository scope and known result categories, never paths or output."""
    import logging

    logger = logging.getLogger(__name__)
    for name, result in outcomes.items():
        # Only log the name when it matches the safe clone naming convention.
        scope = (
            name
            if name.isascii()
            and name.replace("-", "").replace("_", "").replace(".", "").isalnum()
            else "invalid"
        )
        status = result.get("status", "failed")
        reason = result.get("reason", "")
        if status not in {"indexed", "unchanged", "skipped", "failed"}:
            status = "failed"
        if reason not in {
            "repository limit",
            "invalid repository",
            "no eligible files",
            "invalid graph root",
            "invalid graph directory",
            "TimeoutError",
            "FileNotFoundError",
            "CalledProcessError",
            "_Limit",
        }:
            reason = "error" if status == "failed" else "none"
        if result.get("truncated"):
            reason = "limit"
        logger.info("graft_index scope=%s status=%s category=%s", scope, status, reason)


def _validate_paths(
    value: Any,
    snapshot: Path,
    repo: Path,
    allowed: set[str],
    deadline: float | None = None,
) -> Any:
    """All location-bearing fields must stay inside the indexed snapshot."""
    if deadline is not None:
        _remaining(deadline)
    if isinstance(value, list):
        return [
            _validate_paths(item, snapshot, repo, allowed, deadline) for item in value
        ]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in (
                "file",
                "path",
                "filePath",
                "relativePath",
                "sourcePath",
                "pointer",
                "location",
            ):
                if not isinstance(item, str):
                    raise ValueError("invalid pointer")
                text, sep, suffix = item, "", ""
                if not item.startswith("/") and item not in allowed:
                    prefix, separator, line = item.rpartition(":")
                    if separator and prefix in allowed and line.isdecimal():
                        text, sep, suffix = prefix, separator, line
                candidate = Path(text)
                if candidate.is_absolute():
                    try:
                        candidate = candidate.relative_to(snapshot)
                    except ValueError:
                        raise ValueError("invalid pointer") from None
                relative = _relative(str(candidate))
                if str(relative) not in allowed or _safe_file(repo, relative) is None:
                    raise ValueError("invalid pointer")
                out[key] = str(relative) + sep + suffix
            else:
                out[key] = _validate_paths(item, snapshot, repo, allowed, deadline)
        return out
    return value


def query_repo(
    workspace: Path,
    scope: str,
    tool: str,
    params: dict[str, Any],
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Query only current indexed content. Never return stale graph data."""
    deadline = deadline if deadline is not None else time.monotonic() + QUERY_SECONDS
    workspace = Path(workspace).resolve()
    if (
        not isinstance(scope, str)
        or not scope
        or scope in (".", "..")
        or "/" in scope
        or "\\" in scope
    ):
        return {"status": "skipped", "reason": "invalid scope"}
    if tool not in (
        "find_code",
        "find_all",
        "file_api",
        "trace_calls",
        "check_freshness",
        "repo_map",
        "graft_find_code",
        "graft_find_all",
        "graft_file_api",
        "graft_trace_calls",
        "graft_check_freshness",
        "graft_repo_map",
    ):
        return {"status": "skipped", "reason": "unknown tool"}
    tool = tool.removeprefix("graft_")
    repo = workspace / scope
    root = _root(workspace)
    destination = root / scope
    snapshot, graph = destination / "snapshot", destination / "graph"
    if root.is_symlink() or not root.is_dir():
        return {"status": "skipped", "reason": "invalid graph root"}
    if not _valid_repo(workspace, scope, repo):
        return {"status": "skipped", "reason": "invalid repository"}
    try:
        with _workspace_install_lock(workspace, deadline=deadline):
            return _query_locked(
                repo, destination, snapshot, graph, tool, params, deadline
            )
    except TimeoutError:
        return {"status": "failed", "reason": "TimeoutError", "truncated": False}


def _query_locked(
    repo: Path,
    destination: Path,
    snapshot: Path,
    graph: Path,
    tool: str,
    params: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    """Read and validate one graph under the same lock as its writers."""
    try:
        _remaining(deadline)
        manifest = _manifest(destination)
        if manifest is None:
            return {"status": "skipped", "reason": "repository not indexed"}
        if _sources(repo, deadline) != manifest:
            return {"status": "stale", "reason": "source changed; use read/grep"}
        if _disk_size(destination, deadline) > MAX_GRAPH_BYTES:
            raise _Limit("graph disk limit")
        if tool == "find_code":
            argv = ["ask", "--json", params["query"]]
            limit = params.get("limit")
            if limit not in (None, ""):
                limit = int(limit)
                if not 1 <= limit <= 20:
                    raise ValueError("invalid limit")
                argv += ["--limit", str(limit)]
        elif tool == "find_all":
            argv = ["grep", "--json", "--fixed", params["pattern"]]
        elif tool == "file_api":
            path = str(_relative(params["path"]))
            if path not in manifest or _safe_file(repo, Path(path)) is None:
                raise ValueError("invalid path")
            argv = ["skeleton", "--json", path]
        elif tool == "trace_calls":
            argv = ["callers", "--json", params["symbol"]]
            direction, depth = params.get("direction", "in"), params.get("depth", 1)
            if (
                direction not in ("in", "out")
                or type(depth) is not int
                or not 1 <= depth <= 10
            ):
                raise ValueError("invalid traversal")
            argv += ["--direction", direction, "--depth", str(depth)]
        elif tool == "check_freshness":
            argv = ["check", "--json"]
        else:
            limit = params.get("max_dirs", 16)
            if type(limit) is not int or not 1 <= limit <= 30:
                raise ValueError("invalid limit")
            argv = ["map", "--json", "--max-dirs", str(limit)]
        if tool in ("find_code", "find_all") and params.get("path"):
            path = str(_relative(params["path"]))
            if not any(p == path or p.startswith(path + "/") for p in manifest):
                raise ValueError("invalid path")
            argv += ["--in", path]
        for key in ("query", "pattern", "symbol"):
            if key in params and (
                not isinstance(params[key], str)
                or not params[key]
                or len(params[key]) > 2048
                or params[key].startswith("-")
            ):
                raise ValueError("invalid query")
        output = _run(
            [
                GRAFT,
                "--dir",
                str(graph),
                *argv,
                *([] if tool == "check_freshness" else ["--no-refresh"]),
                str(snapshot),
            ],
            cwd=snapshot,
            timeout=_remaining(deadline),
        ).stdout
        if len(output.encode()) > MAX_OUTPUT:
            raise _Limit("query output limit")
        _remaining(deadline)
        data = _validate_paths(
            json.loads(output), snapshot, repo, set(manifest), deadline
        )
        if _disk_size(destination, deadline) > MAX_GRAPH_BYTES:
            raise _Limit("graph disk limit")
        if _sources(repo, deadline) != manifest:
            return {"status": "stale", "reason": "source changed; use read/grep"}
        _remaining(deadline)
        return {"status": "ok", "result": data}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as exc:
        return {
            "status": "failed",
            "reason": type(exc).__name__,
            "truncated": isinstance(exc, _Limit),
        }


def indexed_roots(workspace: Path) -> dict[str, Path]:
    """Return direct cloned roots with completed graphs; queries recheck freshness."""
    workspace = Path(workspace)
    root = _root(workspace)
    with _workspace_install_lock(workspace.resolve()):
        if not root.is_dir() or root.is_symlink() or not workspace.is_dir():
            return {}
        return {
            item.name: workspace / item.name
            for item in root.iterdir()
            if _manifest(item) is not None
            and _valid_repo(workspace.resolve(), item.name, workspace / item.name)
        }


def cleanup_graph(workspace: Path) -> None:
    """Delete graphs and snapshots outside the AI-visible workspace."""
    root = _root(workspace)
    with _workspace_install_lock(Path(workspace).resolve()):
        if root.is_dir() and not root.is_symlink():
            shutil.rmtree(root, ignore_errors=True)
