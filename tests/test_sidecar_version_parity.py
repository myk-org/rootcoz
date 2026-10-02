"""Guards for the pi-sidecar dependency contract.

The sidecar ships as two published artifacts — `@myk-org/pi-sidecar` (npm, used
by ``sidecar-helper``) and `pi-sidecar-client` (PyPI). They are released from one
source and must always be required at the same version: a half-applied bump
leaves the Node side and the Python side disagreeing with nothing reporting it.
"""

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

NPM_REQUIREMENT = re.compile(r'"@myk-org/pi-sidecar"\s*:\s*"([^"]+)"')
PYPI_REQUIREMENT = re.compile(r"^pi-sidecar-client\s*(?P<spec>[^\s]+)\s*$")


def _npm_requirement() -> str:
    manifest = json.loads(
        (REPO_ROOT / "sidecar-helper" / "package.json").read_text(encoding="utf-8")
    )
    match = NPM_REQUIREMENT.search(json.dumps(manifest.get("dependencies", {})))
    assert match, "@myk-org/pi-sidecar requirement not found in sidecar-helper"
    return match.group(1)


def _pypi_requirement() -> str:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    for dependency in pyproject["project"]["dependencies"]:
        if dependency.startswith("pi-sidecar-client"):
            match = PYPI_REQUIREMENT.match(dependency)
            assert match, f"unparsable pi-sidecar-client requirement: {dependency}"
            return match.group("spec")
    raise AssertionError("pi-sidecar-client requirement not found in pyproject.toml")


def test_sidecar_requirements_match_across_registries() -> None:
    """npm and PyPI must require the same sidecar version range.

    Bumping one without the other is the failure this guards against. Update
    sidecar-helper/package.json and pyproject.toml together, then regenerate
    both lockfiles.
    """
    npm = _npm_requirement()
    pypi = _pypi_requirement()
    assert npm == pypi, (
        f"pi-sidecar requirements diverged: npm {npm!r} vs PyPI {pypi!r}. "
        "Bump both in the same change and regenerate both lockfiles."
    )


def test_sidecar_requirements_are_ranges_not_exact_pins() -> None:
    """An exact pin never moves, so no upstream fix can ever reach this repo."""
    for requirement, source in (
        (_npm_requirement(), "sidecar-helper/package.json"),
        (_pypi_requirement(), "pyproject.toml"),
    ):
        assert not requirement.startswith(("=", "==", "~=")), (
            f"pi-sidecar pinned exactly ({requirement!r}) in {source}; use a range."
        )
        assert ">=" in requirement or "^" in requirement, (
            f"pi-sidecar requirement {requirement!r} in {source} is not a range."
        )
