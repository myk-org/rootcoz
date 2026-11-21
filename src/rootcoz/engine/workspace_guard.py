"""Workspace path confinement guard for Pi AI sidecar sessions.

Writes a self-contained ESM Pi extension into ``<workspace>/.pi/extensions/``
that blocks builtin filesystem tools (``read``, ``ls``, ``grep``, ``find``)
and ``subagent`` from accessing paths outside the session workspace.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from rootcoz import get_child_logger
from rootcoz.temp_paths import rootcoz_temp_base

logger = get_child_logger(__name__)

GUARD_FILENAME = "_rootcoz_path_guard.js"
GUARDED_TOOLS: frozenset[str] = frozenset(
    {"read", "ls", "grep", "find", "write", "edit", "bash", "powershell", "subagent"}
)

_HISTORY_PROMPT_DOC = (
    Path(__file__).resolve().parent.parent
    / "ai-prompts"
    / "FAILURE_HISTORY_ANALYSIS.md"
)
_ROOTCOZ_ARTIFACT_PREFIXES = ("jenkins-artifacts-", "artifacts-")
_PROW_ARTIFACT_PREFIX = "prow-artifacts-"

_GUARD_JS_TEMPLATE = """\
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const EXTRA_ALLOWED_DIRS = __EXTRA_ALLOWED_DIRS__;
const ALLOWED_READ_FILES = __ALLOWED_READ_FILES__;
const FS_TOOLS = new Set(["read", "ls", "grep", "find", "write", "edit"]);
const REQUIRED_PATH_TOOLS = new Set(["read", "write", "edit"]);
const BLOCKED_SHELL_TOOLS = new Set(["bash", "powershell"]);
const UNICODE_SPACES = /[\\u00A0\\u2000-\\u200A\\u202F\\u205F\\u3000]/g;
const NARROW_NO_BREAK_SPACE = "\\u202F";

function isWithin(target, root) {
  const rel = path.relative(root, target);
  return (
    rel === "" ||
    (rel !== ".." && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel))
  );
}

function canonicalizePath(targetPath) {
  const resolved = path.resolve(targetPath);
  const tail = [];
  let current = resolved;
  while (true) {
    try {
      const real = fs.realpathSync(current);
      return tail.length ? path.join(real, ...tail.reverse()) : real;
    } catch (err) {
      if (err && err.code === "ENOENT") {
        // Fail closed on dangling symlinks so a race or unresolvable link cannot bypass confinement.
        try {
          if (fs.lstatSync(current).isSymbolicLink()) {
            throw new Error("Dangling symlink is not allowed");
          }
        } catch (lstatErr) {
          if (!lstatErr || lstatErr.code !== "ENOENT") {
            throw lstatErr;
          }
        }
        const parent = path.dirname(current);
        if (parent === current) {
          return path.join(current, ...tail.reverse());
        }
        tail.push(path.basename(current));
        current = parent;
        continue;
      }
      throw err;
    }
  }
}

function normalizeFsInput(rawPath, cwdReal) {
  if (rawPath.includes("\\0")) {
    throw new Error("Null byte in path is not allowed");
  }
  let p = rawPath.replace(UNICODE_SPACES, " ");
  if (p.startsWith("@")) {
    p = p.slice(1);
  }
  if (p === "~") {
    p = os.homedir();
  } else if (p.startsWith("~/") || p.startsWith("~\\\\")) {
    p = path.join(os.homedir(), p.slice(2));
  } else if (p.startsWith("~")) {
    throw new Error("Tilde user expansion (~user) is not allowed");
  }
  if (/^file:\\/\\//i.test(p)) {
    p = fileURLToPath(p);
  }
  return path.isAbsolute(p) ? path.resolve(p) : path.resolve(cwdReal, p);
}

function readPathVariants(resolved) {
  const candidates = [resolved];
  const amPm = resolved.replace(/ (AM|PM)\\./gi, `${NARROW_NO_BREAK_SPACE}$1.`);
  const nfd = resolved.normalize("NFD");
  const curly = resolved.replace(/'/g, "\\u2019");
  const nfdCurly = nfd.replace(/'/g, "\\u2019");
  for (const variant of [amPm, nfd, curly, nfdCurly]) {
    if (variant !== resolved && !candidates.includes(variant)) {
      try {
        fs.lstatSync(variant);
        candidates.push(variant);
      } catch {
        // Variant does not exist on disk.
      }
    }
  }
  return candidates;
}

function checkFsPath(toolName, rawPath, cwdReal, allowedDirs, allowedReadFiles) {
  if (typeof rawPath !== "string") {
    return `Tool '${toolName}' requires a string path`;
  }
  if (REQUIRED_PATH_TOOLS.has(toolName) && rawPath.trim() === "") {
    return `Tool '${toolName}' requires a non-empty path`;
  }
  if (rawPath === "") {
    return null;
  }
  let resolved;
  try {
    resolved = normalizeFsInput(rawPath, cwdReal);
  } catch (err) {
    return `Path blocked for '${toolName}': ${err instanceof Error ? err.message : "invalid path"}`;
  }
  const candidates = toolName === "read" ? readPathVariants(resolved) : [resolved];
  for (const candidate of candidates) {
    let canonical;
    try {
      canonical = canonicalizePath(candidate);
    } catch (err) {
      return `Path blocked for '${toolName}': ${err instanceof Error ? err.message : "cannot resolve path"}`;
    }
    const inAllowedDir = allowedDirs.some((root) => isWithin(canonical, root));
    const isAllowedFile =
      toolName === "read" && allowedReadFiles.includes(canonical);
    if (!inAllowedDir && !isAllowedFile) {
      return `Access denied: '${rawPath}' resolves outside the session workspace`;
    }
  }
  return null;
}

function checkSubagentCwd(rawCwd, fieldName, cwdReal) {
  if (typeof rawCwd !== "string" || rawCwd.trim() === "") {
    return `Tool 'subagent' ${fieldName} must be a non-empty string`;
  }
  if (rawCwd.includes("\\0")) {
    return `Tool 'subagent' ${fieldName} contains null byte`;
  }
  if (rawCwd.startsWith("~") || /^file:\\/\\//i.test(rawCwd)) {
    return `Access denied: subagent ${fieldName} '${rawCwd}' is outside the session workspace`;
  }
  // child_process.spawn() resolves relative cwd against process.cwd(), not ctx.cwd,
  // and DefaultResourceLoader only loads {cwd}/.pi/extensions/. Require cwd to
  // resolve to cwdReal so a spawned subagent cannot drop this path guard.
  const targets = path.isAbsolute(rawCwd)
    ? [path.resolve(rawCwd)]
    : [path.resolve(cwdReal, rawCwd), path.resolve(process.cwd(), rawCwd)];
  for (const target of targets) {
    let canonical;
    try {
      canonical = canonicalizePath(target);
    } catch (err) {
      return `Access denied: subagent ${fieldName} '${rawCwd}' cannot be resolved (${err instanceof Error ? err.message : "error"})`;
    }
    if (canonical !== cwdReal) {
      return `Access denied: subagent ${fieldName} '${rawCwd}' must resolve to the session workspace root`;
    }
  }
  return null;
}

export default function rootcozPathGuard(pi) {
  pi.on("tool_call", async (event, ctx) => {
    try {
      const toolName = event?.toolName;
      if (BLOCKED_SHELL_TOOLS.has(toolName)) {
        return {
          block: true,
          reason: `Access denied: shell tool '${toolName}' is not allowed in confined workspace sessions`,
        };
      }
      if (!FS_TOOLS.has(toolName) && toolName !== "subagent") {
        return undefined;
      }
      const input = event?.input;
      if (!input || typeof input !== "object") {
        return {
          block: true,
          reason: `Tool '${toolName}' called with invalid input`,
        };
      }
      if (!ctx || typeof ctx.cwd !== "string" || !ctx.cwd) {
        return { block: true, reason: "Session workspace cwd is unavailable" };
      }
      const cwdReal = fs.realpathSync(ctx.cwd);
      const allowedDirs = [cwdReal];
      for (const dir of EXTRA_ALLOWED_DIRS) {
        try {
          allowedDirs.push(fs.realpathSync(dir));
        } catch {
          // Extra directory missing or removed; fail closed for it.
        }
      }
      const allowedReadFiles = [];
      for (const file of ALLOWED_READ_FILES) {
        try {
          allowedReadFiles.push(fs.realpathSync(file));
        } catch {
          // Prompt file missing; ignore.
        }
      }

      if (FS_TOOLS.has(toolName)) {
        if (REQUIRED_PATH_TOOLS.has(toolName)) {
          const reason = checkFsPath(
            toolName,
            input.path,
            cwdReal,
            allowedDirs,
            allowedReadFiles,
          );
          if (reason) return { block: true, reason };
          return undefined;
        }
        if ("path" in input && input.path !== undefined) {
          const reason = checkFsPath(
            toolName,
            input.path,
            cwdReal,
            allowedDirs,
            allowedReadFiles,
          );
          if (reason) return { block: true, reason };
        }
        return undefined;
      }

      if (toolName === "subagent") {
        if ("cwd" in input && input.cwd !== undefined) {
          const reason = checkSubagentCwd(input.cwd, "cwd", cwdReal);
          if (reason) return { block: true, reason };
        }
        if ("tasks" in input && input.tasks !== undefined) {
          if (!Array.isArray(input.tasks)) {
            return {
              block: true,
              reason: "Tool 'subagent' tasks must be an array",
            };
          }
          for (let i = 0; i < input.tasks.length; i++) {
            const item = input.tasks[i];
            if (
              item &&
              typeof item === "object" &&
              "cwd" in item &&
              item.cwd !== undefined
            ) {
              const reason = checkSubagentCwd(
                item.cwd,
                `tasks[${i}].cwd`,
                cwdReal,
              );
              if (reason) return { block: true, reason };
            }
          }
        }
        if ("chain" in input && input.chain !== undefined) {
          if (!Array.isArray(input.chain)) {
            return {
              block: true,
              reason: "Tool 'subagent' chain must be an array",
            };
          }
          for (let i = 0; i < input.chain.length; i++) {
            const item = input.chain[i];
            if (
              item &&
              typeof item === "object" &&
              "cwd" in item &&
              item.cwd !== undefined
            ) {
              const reason = checkSubagentCwd(
                item.cwd,
                `chain[${i}].cwd`,
                cwdReal,
              );
              if (reason) return { block: true, reason };
            }
          }
        }
        return undefined;
      }
      return undefined;
    } catch (err) {
      return {
        block: true,
        reason: `Workspace path guard error: ${err instanceof Error ? err.message : String(err)}`,
      };
    }
  });
}
"""


def needs_workspace_path_guard(tools: Sequence[str] | None) -> bool:
    """Return True when the requested session tool list can access the filesystem or subagents."""
    if tools is None:
        return True
    return any(tool in GUARDED_TOOLS for tool in tools)


def _forbidden_roots() -> set[Path]:
    roots = {
        Path("/").resolve(),
        Path("/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
        rootcoz_temp_base().resolve(),
    }
    try:
        roots.add(Path.home().resolve())
    except RuntimeError:
        pass
    return roots


def is_dedicated_workspace(path: Path) -> bool:
    """Return True only when *path* is an existing non-symlink dedicated directory."""
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    if resolved == resolved.parent:
        return False
    return resolved not in _forbidden_roots()


def _trusted_artifacts_target(workspace: Path) -> Path | None:
    """Return resolved ``build-artifacts`` symlink target only if it is a rootcoz extract dir."""
    link = workspace / "build-artifacts"
    if not link.is_symlink():
        return None
    try:
        target = link.resolve(strict=True)
    except OSError:
        return None
    if not target.is_dir():
        return None
    rootcoz_base = rootcoz_temp_base().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        target.parent == rootcoz_base
        and target.name.startswith(_ROOTCOZ_ARTIFACT_PREFIXES)
    ) or (target.parent == temp_root and target.name.startswith(_PROW_ARTIFACT_PREFIX)):
        return target
    logger.warning(
        "Workspace path guard: ignoring untrusted build-artifacts symlink target %s",
        target,
    )
    return None


def _ensure_real_subdir(parent: Path, name: str) -> Path:
    child = parent / name
    if child.is_symlink():
        raise ValueError(
            f"Refusing to install workspace path guard through symlink: {child}"
        )
    if child.exists() and not child.is_dir():
        raise ValueError(
            f"Refusing to install workspace path guard: non-directory at {child}"
        )
    child.mkdir(exist_ok=True)
    if child.is_symlink() or not child.is_dir():
        raise ValueError(
            f"Refusing to install workspace path guard through unsafe path: {child}"
        )
    return child


def install_workspace_path_guard(
    workspace: Path,
    *,
    extra_allowed_dirs: Sequence[Path] = (),
) -> Path:
    """Install ``_rootcoz_path_guard.js`` into ``<workspace>/.pi/extensions/``.

    Refuses symlinks on *workspace*, ``.pi``, ``.pi/extensions``, and the guard
    file itself, refuses shared roots such as bare ``tempfile.gettempdir()``,
    and always overwrites the guard file so repository ``.rootcoz/extensions/``
    cannot shadow it.
    """
    workspace = Path(workspace)
    if workspace.is_symlink():
        raise ValueError(
            f"Refusing to install workspace path guard into symlinked workspace: {workspace}"
        )
    if not is_dedicated_workspace(workspace):
        raise ValueError(
            f"Refusing to install workspace path guard: '{workspace}' is not a dedicated workspace directory"
        )

    workspace_resolved = workspace.resolve(strict=True)
    pi_dir = _ensure_real_subdir(workspace_resolved, ".pi")
    ext_dir = _ensure_real_subdir(pi_dir, "extensions")
    guard_path = ext_dir / GUARD_FILENAME

    if guard_path.is_symlink():
        raise ValueError(
            f"Refusing to overwrite symlinked workspace path guard: {guard_path}"
        )
    if guard_path.exists() and guard_path.is_dir():
        raise ValueError(
            f"Refusing to overwrite directory at workspace path guard: {guard_path}"
        )

    allowed_extra_dirs: list[str] = []
    seen_dirs: set[Path] = {workspace_resolved}

    artifacts_target = _trusted_artifacts_target(workspace_resolved)
    if artifacts_target is not None and not artifacts_target.is_relative_to(
        workspace_resolved
    ):
        seen_dirs.add(artifacts_target)
        allowed_extra_dirs.append(str(artifacts_target))

    for extra in extra_allowed_dirs:
        candidate = Path(extra)
        if not is_dedicated_workspace(candidate):
            continue
        resolved_extra = candidate.resolve(strict=True)
        if resolved_extra in seen_dirs or resolved_extra.is_relative_to(
            workspace_resolved
        ):
            continue
        seen_dirs.add(resolved_extra)
        allowed_extra_dirs.append(str(resolved_extra))

    allowed_read_files: list[str] = []
    if _HISTORY_PROMPT_DOC.is_file() and not _HISTORY_PROMPT_DOC.is_symlink():
        allowed_read_files.append(str(_HISTORY_PROMPT_DOC.resolve(strict=True)))

    script = _GUARD_JS_TEMPLATE.replace(
        "__EXTRA_ALLOWED_DIRS__", json.dumps(allowed_extra_dirs)
    ).replace("__ALLOWED_READ_FILES__", json.dumps(allowed_read_files))
    script_bytes = script.encode("utf-8")

    # Skip rewrite when an existing regular guard file already has identical
    # content so concurrent sessions sharing one workspace never race on it.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        read_fd = os.open(guard_path, os.O_RDONLY | nofollow)
    except FileNotFoundError:
        read_fd = -1
    except OSError as exc:
        raise ValueError(
            f"Refusing to read workspace path guard at {guard_path}: {exc}"
        ) from exc
    if read_fd >= 0:
        with os.fdopen(read_fd, "rb") as existing_fh:
            if existing_fh.read() == script_bytes:
                return guard_path

    # Atomic temp-file + os.replace so concurrent Pi DefaultResourceLoader reads
    # never observe a truncated or partially written guard file.
    tmp_path = ext_dir / f".{GUARD_FILENAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    try:
        fd = os.open(tmp_path, create_flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(script_bytes)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        if guard_path.is_symlink():
            tmp_path.unlink(missing_ok=True)
            raise ValueError(
                f"Refusing to overwrite symlinked workspace path guard: {guard_path}"
            )
        if guard_path.exists() and guard_path.is_dir():
            tmp_path.unlink(missing_ok=True)
            raise ValueError(
                f"Refusing to overwrite directory at workspace path guard: {guard_path}"
            )
        os.replace(tmp_path, guard_path)
    except ValueError:
        raise
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(
            f"Refusing to write workspace path guard at {guard_path}: {exc}"
        ) from exc

    logger.debug(
        "Installed workspace path guard at %s (extra_dirs=%d, allowed_files=%d)",
        guard_path,
        len(allowed_extra_dirs),
        len(allowed_read_files),
    )
    return guard_path
