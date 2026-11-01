"""Graph tools validated against **captured** upstream output, not invented JSON.

``tests/test_graft.py`` stubs ``_run`` to exercise the validator's security
rules. That is right for rejecting bad input, but it cannot prove the validator
accepts what upstream actually emits — and it did not: ``repo_map`` returned
``ValueError`` for every request because upstream groups output by directory,
and ``find_code`` rejected the ``path:L10-L20`` pointers upstream has always
produced. Both shipped broken because the fixtures were fiction.

The fixtures in ``tests/fixtures/graft/`` are real output, captured once from
the installed ``trail`` binary against a small Python repo. Tests stay fast and
hermetic — no binary, no graph build, no network — while still failing when an
upstream upgrade changes a shape we parse. Recapture them with:

    trail --dir <graph> map --json --max-dirs 5 --no-refresh <snapshot>
    trail --dir <graph> ask --json --limit 3 --no-refresh <snapshot> <query>
    trail --dir <graph> grep --json --fixed --no-refresh <pattern> <snapshot>
    trail --dir <graph> skeleton --json --no-refresh <path> <snapshot>
    trail --dir <graph> callers --json <symbol> --direction in --depth 1 \
        --no-refresh <snapshot>
    trail --dir <graph> build --check --json <snapshot>
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rootcoz.engine import graft

_FIXTURES = Path(__file__).parent / "fixtures" / "graft"

#: The files the capture repo had; the manifest the validator allows.
_MANIFEST = {
    "simple_logger/__init__.py",
    "simple_logger/logger.py",
    "simple_logger/tests/__init__.py",
    "simple_logger/tests/test_logger.py",
}


def _fixture(tool: str) -> dict:
    return json.loads((_FIXTURES / f"{tool}.json").read_text())


@pytest.fixture()
def snapshot(tmp_path: Path) -> Path:
    """A snapshot dir holding exactly the captured repo's files."""
    for relative in _MANIFEST:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def placeholder():\n    return 1\n")
    return tmp_path


@pytest.fixture()
def repo(snapshot: Path) -> Path:
    return snapshot


@pytest.mark.parametrize(
    "tool",
    ["repo_map", "find_code", "find_all", "file_api", "trace_calls", "check_freshness"],
)
def test_validator_accepts_real_output(tool: str, snapshot: Path, repo: Path) -> None:
    """Every tool's real payload passes validation untouched."""
    payload = _fixture(tool)
    assert graft._validate_paths(payload, snapshot, repo, _MANIFEST) == payload


def test_map_directory_locations_are_accepted(snapshot: Path, repo: Path) -> None:
    """``map`` groups by directory; ``dirs[].path`` is not a manifest entry."""
    payload = _fixture("repo_map")
    directories = [
        entry["path"] for entry in payload["dirs"] if not entry.get("isFile")
    ]
    assert directories, "fixture must contain a directory group"
    validated = graft._validate_paths(payload, snapshot, repo, _MANIFEST)
    assert [
        entry["path"] for entry in validated["dirs"] if not entry.get("isFile")
    ] == (directories)


def test_pointer_line_ranges_survive_validation(snapshot: Path, repo: Path) -> None:
    """Pointers carry ``path:L10-L20`` spans, and keep them verbatim."""
    payload = _fixture("find_code")
    pointers = [hit["pointer"] for hit in payload["hits"]]
    assert any("L" in pointer for pointer in pointers), pointers
    validated = graft._validate_paths(payload, snapshot, repo, _MANIFEST)
    assert [hit["pointer"] for hit in validated["hits"]] == pointers


def test_foreign_paths_are_still_rejected(snapshot: Path, repo: Path) -> None:
    """The relaxation must not open a hole."""
    with pytest.raises(ValueError):
        graft._validate_paths({"path": "../outside.py"}, snapshot, repo, _MANIFEST)


def test_absolute_paths_outside_the_snapshot_are_rejected(
    snapshot: Path, repo: Path
) -> None:
    with pytest.raises(ValueError):
        graft._validate_paths({"file": "/etc/passwd"}, snapshot, repo, _MANIFEST)
