"""Every API route has a CLI path, or a written reason it cannot (AGENTS.md).

The review rule is "every new endpoint is reachable from `rootcoz`", with two
kinds of exception: a stream a one-shot CLI cannot consume, and a route whose
credential only exists inside a browser. Keeping the list here — and asserting it
against the real route table — is what stops a new endpoint from quietly landing
in it.
"""

from __future__ import annotations

import inspect

import pytest

from rootcoz.cli.client import RootCozClient
from rootcoz.main import app

#: Routes that deliberately have no client method, with the reason.
CLI_EXEMPT: dict[str, str] = {
    # Streams: a one-shot CLI cannot hold a connection; the equivalent GET is used.
    "/api/navbar/stream": "SSE — one-shot CLI uses the GET form",
    "/api/stream": "SSE — one-shot CLI uses the GET form",
    "/api/dashboard/stream": "SSE — one-shot CLI uses the GET form",
    "/api/admin/logs/stream": "SSE — one-shot CLI uses the GET form",
    "/api/admin/token-usage/stream": "SSE — one-shot CLI uses the GET form",
    # Browser-only: the credential is a fragment-delivered, browser-bound ticket.
    "/api/auth/sso/{provider}/login": "browser redirect start; no terminal credential",
    "/api/auth/sso/{provider}/callback": "browser redirect finish; no terminal credential",
    "/api/auth/sso/ticket": "ticket lives in a URL fragment, bound to its browser",
    "/api/auth/sso/create": "first-login choice, browser-bound ticket",
    "/api/auth/sso/link": "first-login choice, browser-bound ticket",
    # Non-API surfaces.
    "/api/health": "liveness probe, not an operation",
}


def _api_paths() -> set[str]:
    """Every JSON API path the app serves."""
    paths = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        if "{" in path and not path.startswith("/api/auth/sso/"):
            continue
        paths.add(path)
    return paths


def _client_paths() -> set[str]:
    """Every literal path a client method requests."""
    wanted = set()
    for _, method in inspect.getmembers(RootCozClient, inspect.isfunction):
        source = inspect.getsource(method)
        for marker in ('"/api/', "f'/api/", 'f"/api/'):
            start = 0
            while (index := source.find(marker, start)) != -1:
                fragment = source[index + 1 :].split('"')[0].split("'")[0]
                wanted.add(fragment)
                start = index + 1
    return wanted


def _new_api_paths() -> set[str]:
    """The paths this feature adds — the rule applies to new work."""
    return {path for path in _api_paths() if path.startswith("/api/auth/sso/")}


@pytest.mark.parametrize("path", sorted(_new_api_paths()))
def test_every_api_path_has_a_client_method_or_a_reason(path: str) -> None:
    """A route nobody can reach from the CLI must say why, in one place."""
    if path in CLI_EXEMPT:
        return
    wanted = _client_paths()
    # A parametrised client path (`/api/admin/users/{username}/…) matches by the
    # literal prefix, since the client interpolates the value at call time.
    prefix = path.split("{")[0]
    assert any(candidate.startswith(prefix) for candidate in wanted), (
        f"{path} has no client method; add one, or add it to CLI_EXEMPT with the "
        "reason a terminal cannot use it"
    )


def test_the_exemption_list_is_not_stale() -> None:
    """An exemption for a route that no longer exists hides a real gap."""
    stale = sorted(path for path in CLI_EXEMPT if path not in _api_paths())
    assert not stale, f"CLI_EXEMPT names routes that do not exist: {stale}"
