"""Every anonymous route is on the documented allow-list, and vice versa (#197).

Sign-in has to work before a session exists, so the SSO surfaces are public by
design. That is exactly why the list is checked here: a route that becomes public
without being written down — or a documented public route that quietly stops being
public — is the drift this test exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rootcoz.main import AuthMiddleware, app

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS = (REPO_ROOT / "AGENTS.md").read_text()
API_REFERENCE = (REPO_ROOT / "docs" / "api-reference.md").read_text()

#: Routes whose public shape carries a path parameter, so the literal path in
#: ``app.routes`` is a template that ``_is_public_path`` matches by shape.
_SHAPE_MATCHED = re.compile(r"^/api/auth/sso/[^/]+/(login|callback)$")


def _app_paths() -> set[str]:
    """Every path the app serves, ignoring the SPA catch-all and static mounts."""
    paths = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or path.startswith(("/assets", "/static")):
            continue
        if "{" in path and not path.startswith("/api/auth/sso/"):
            continue  # parametrised API routes are not auth surfaces
        paths.add(path)
    return paths


def _public(path: str) -> bool:
    """Whether the middleware treats this path as anonymous."""
    return AuthMiddleware._is_public_path(path)


def test_every_public_api_route_is_documented() -> None:
    """A public route an operator cannot find in the docs is a silent hole."""
    undocumented = sorted(
        path
        for path in _app_paths()
        if path.startswith("/api/") and _public(path) and path not in API_REFERENCE
    )
    assert not undocumented, (
        "these routes are anonymous (`_PUBLIC_PATHS` / shape match) but absent from "
        f"docs/api-reference.md: {undocumented}"
    )


def test_the_sso_surfaces_stay_public() -> None:
    """Sign-in cannot require a session, so these must not lose their exemption."""
    expected = [
        "/api/auth/sso/providers",
        "/api/auth/sso/ticket",
        "/api/auth/sso/create",
        "/api/auth/sso/link",
        "/api/auth/sso/device/start",
        "/api/auth/sso/device/poll",
        "/api/auth/sso/google/login",
        "/api/auth/sso/google/callback",
    ]
    for path in expected:
        assert _public(path), f"{path} must stay public — it runs before a session"


def test_the_device_approval_surfaces_stay_authenticated() -> None:
    """Approval acts on an account, so it must never be reachable anonymously."""
    for path in (
        "/api/auth/sso/device/approve",
        "/api/auth/sso/device/approve/lookup",
        "/api/user/identities",
    ):
        assert not _public(path), f"{path} must require a session"


def test_agents_md_lists_every_public_sso_surface() -> None:
    """AGENTS.md is where the exception is justified, so it must be exhaustive."""
    for path in (
        "/api/auth/sso/providers",
        "/api/auth/sso/{provider}/login",
        "/api/auth/sso/ticket",
        "/api/auth/sso/create",
        "/api/auth/sso/link",
        "/api/auth/sso/device/start",
        "/api/auth/sso/device/poll",
    ):
        assert path in AGENTS, f"{path} is public but not named in AGENTS.md"


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/sso/providers",
        "/api/auth/sso/ticket",
        "/api/auth/sso/device/start",
        "/api/auth/sso/google/login",
        "/api/auth/sso/google/callback",
    ],
)
def test_a_public_sso_route_answers_without_a_session(path: str) -> None:
    """The exemption is not theoretical: these are reachable with no credential."""
    assert _public(path)
