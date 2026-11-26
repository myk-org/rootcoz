"""Regression guards for Dockerfile image size and OpenShift compatibility (#679).

Locks in the multi-stage layout that eliminates ~1.5+ GB of container bloat:
- P1: No recursive `chmod -R g+w /app` in the final runtime stage (avoids the
      1.17 GB OverlayFS copy-on-write duplicate layer); group-write bits are
      applied in each builder stage before `COPY --chown=appuser:0`.
- P3: Node.js runtime copied from pinned `node:>=22.23.4-bookworm-slim`
      (keeping the nodejs/node#63989 keep-alive fix), without installing
      `gnupg`, `/usr/include/node` headers, or a redundant global `npm@` layer.
- P4: Runtime stage uses plain `python:3.14-slim-bookworm` and invokes
      `/app/.venv/bin/uvicorn` directly without shipping `uv` at runtime.
- P5: `sidecar-builder` prunes `*.map`, `node-gyp` intermediates, non-target
      arch `prebuilds`, and package `test`/`tests`/`docs` directories before
      `COPY` while leaving `onnxruntime-*`, `third_party/graft-LICENSE`, and
      `/app/sidecar-helper/bin/{trail,pi}` intact.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"

_FROM_RE = re.compile(
    r"^FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<name>\S+))?", re.IGNORECASE
)
_NODE_TAG_RE = re.compile(r"^node:(\d+)\.(\d+)\.(\d+)-bookworm-slim$")


def _parse_stages() -> list[tuple[str | None, str, str]]:
    """Split Dockerfile into ``(stage_name, base_image, body)`` tuples."""
    stages: list[tuple[str | None, str, list[str]]] = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = _FROM_RE.match(line.strip())
        if match:
            stages.append((match.group("name"), match.group("image"), [line]))
        elif stages:
            stages[-1][2].append(line)
    assert stages, "No FROM instructions found in Dockerfile"
    return [(name, image, "\n".join(lines)) for name, image, lines in stages]


def _stage_by_name(name: str) -> tuple[str, str]:
    for stage_name, image, body in _parse_stages():
        if stage_name == name:
            return image, body
    raise AssertionError(f"Stage {name!r} not found in Dockerfile")


def _active_lines(stage_body: str) -> str:
    """Return non-comment lines from a stage body."""
    return "\n".join(
        line for line in stage_body.splitlines() if not line.lstrip().startswith("#")
    )


def test_p1_no_recursive_chmod_over_app_in_final_stage() -> None:
    """Final stage must not run `chmod -R` over `/app` (avoids 1.17 GB CoW layer)."""
    _, _, final_body = _parse_stages()[-1]
    active = _active_lines(final_body)
    assert not re.search(r"chmod\s+-R\s+[^\n]*/app\b", active), (
        "Final stage must not run `chmod -R ... /app`; apply g+w inside builder stages "
        "before COPY to prevent OverlayFS copy-on-write duplication."
    )
    # Parent directories under /app must still be pre-created with appuser:0 and g+w
    assert "chown appuser:0" in active
    assert "chmod g+w /data /app" in active

    # Builder stages must set group-write bits before COPY
    for stage in ("frontend-builder", "sidecar-builder", "builder"):
        _, body = _stage_by_name(stage)
        assert "chmod g+w" in _active_lines(body), (
            f"Stage {stage!r} must apply `chmod g+w` before COPY for OpenShift GID 0"
        )


def test_p3_slim_node_runtime_without_gnupg_or_global_npm_upgrade() -> None:
    """Node.js must come from node >=22.23.4-bookworm-slim with no gnupg or npm@ upgrade."""
    node_image, _ = _stage_by_name("node-runtime")
    match = _NODE_TAG_RE.match(node_image)
    assert match, f"Expected pinned node:<ver>-bookworm-slim stage, got {node_image!r}"
    version = tuple(int(part) for part in match.groups())
    assert version >= (22, 23, 4), (
        f"node-runtime {node_image!r} is below 22.23.4 (nodejs/node#63989 keep-alive fix)"
    )

    _, _, final_body = _parse_stages()[-1]
    active = _active_lines(final_body)
    assert "gnupg" not in active, "Final stage must not install gnupg"
    assert "npm install -g" not in active, (
        "Final stage must not add a global npm upgrade layer"
    )
    assert "/usr/include/node" not in active, (
        "Final stage must not copy Node C/C++ headers"
    )
    assert "COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node" in active
    assert (
        "COPY --from=node-runtime /usr/local/lib/node_modules/npm "
        "/usr/local/lib/node_modules/npm"
    ) in active


def test_p4_runtime_uses_python_slim_and_direct_uvicorn() -> None:
    """Final stage must use python:3.14-slim-bookworm and invoke uvicorn directly."""
    builder_image, _ = _stage_by_name("builder")
    assert builder_image == "ghcr.io/astral-sh/uv:python3.14-bookworm-slim"

    _, final_image, final_body = _parse_stages()[-1]
    assert final_image == "python:3.14-slim-bookworm", (
        f"Final stage should use python:3.14-slim-bookworm without uv, got {final_image!r}"
    )

    active = _active_lines(final_body)
    assert (
        'CMD ["/app/.venv/bin/uvicorn", "rootcoz.main:app", "--host", "0.0.0.0"]'
        in active
    )
    assert '"uv"' not in active, "Final stage CMD must not invoke uv"
    assert 'PATH="/app/.venv/bin:${PATH}"' in active
    assert 'PATH="/app/sidecar-helper/bin:${PATH}"' in active


def test_p5_sidecar_builder_prunes_build_bloat_and_preserves_runtime_assets() -> None:
    """sidecar-builder must strip maps/intermediates/docs/tests and non-target prebuilds."""
    _, sidecar_body = _stage_by_name("sidecar-builder")
    active = _active_lines(sidecar_body)
    for token in (
        "-path '*/onnxruntime-*' -prune",
        "-name '*.map'",
        "-name '*.o'",
        "-name 'obj.target'",
        "-name 'test'",
        "-name 'tests'",
        "-name 'docs'",
        "-name 'prebuilds'",
    ):
        assert token in active, f"sidecar-builder pruning missing {token!r}"

    # Must never prune `doc` (yaml/dist/doc is runtime code)
    assert "-name 'doc'" not in active, (
        "Do not prune `doc` dirs (yaml/dist/doc is runtime code)"
    )
    # Must never combine -prune with -delete (GNU find -delete implies -depth, disabling -prune)
    assert "-delete" not in active, "Do not use find -delete with -prune"

    _, _, final_body = _parse_stages()[-1]
    final_active = _active_lines(final_body)
    assert (
        "COPY --chown=appuser:0 third_party/graft-LICENSE /app/third_party/graft-LICENSE"
        in final_active
    )
    assert "/app/sidecar-helper/bin/trail" in final_active
    assert "/app/sidecar-helper/bin/pi" in final_active


def test_build_identity_args_follow_last_run() -> None:
    """CI build-arg contract must remain declared after the last RUN in the final stage."""
    _, _, final_body = _parse_stages()[-1]
    active = _active_lines(final_body)
    last_run_pos = active.rfind("\nRUN ")
    for arg in ("ARG GIT_COMMIT=", "ARG GIT_REF=", "ARG BUILT_AT="):
        pos = active.find(arg)
        assert pos > last_run_pos, (
            f"{arg} must remain after the last RUN for layer caching"
        )
