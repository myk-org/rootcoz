"""Structural guard for ALLOWED_USERS enforcement (#299).

``ALLOWED_USERS`` is enforced per handler, which makes it forgettable: a
mutating handler that calls ``_require_reviewer`` but not
``_check_allow_list`` lets an off-list user write data. That is exactly the
gap this module locks down — the check itself lives in the handlers, and
this test fails when a new one is forgotten.
"""

import ast
import re
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parents[1] / "src" / "rootcoz" / "main.py"

# Mutating routes that do not create or modify stored data, and so are not
# gated by the allow list. Each is listed because it is a deliberate decision,
# not because it was overlooked.
#
# - ``/api/auth/*``: authentication itself, which every user must be able to
#   reach regardless of list membership.
# - ``/api/jira-projects``, ``/api/jira-security-levels``,
#   ``/api/validate-token``: POST used to carry a request body, but they only
#   read from Jira/GitHub and persist nothing.
# - ``/api/jobs/metadata/rules/preview``: evaluates rules without writing.
ALLOW_LIST_EXEMPT_ROUTES = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/jira-projects"),
    ("POST", "/api/jira-security-levels"),
    ("POST", "/api/validate-token"),
    ("POST", "/api/jobs/metadata/rules/preview"),
}

MUTATING_METHODS = {"post", "put", "delete", "patch"}


def _mutating_routes() -> list[tuple[str, str, ast.FunctionDef]]:
    """Return (verb, path, handler) for every mutating route in main.py."""
    tree = ast.parse(MAIN_PY.read_text())
    routes: list[tuple[str, str, ast.FunctionDef]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            verb = dec.func.attr.lower()
            if verb not in MUTATING_METHODS or not dec.args:
                continue
            first = dec.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                routes.append((verb.upper(), first.value, node))
    return routes


def _calls(func: ast.FunctionDef, name: str) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        for n in ast.walk(func)
    )


@pytest.mark.parametrize(
    "verb,path,handler", _mutating_routes(), ids=lambda v: str(v)[:60]
)
def test_mutating_route_enforces_allow_list_or_is_exempt(
    verb: str, path: str, handler: ast.FunctionDef
):
    """Every mutating route must gate on the allow list, or be a listed exemption.

    Admin-only handlers need no check: ``_require_admin`` already rejects a
    non-admin, and ``_check_allow_list`` bypasses admins. Public paths are
    unreachable by an authenticated off-list user.
    """
    if _calls(handler, "_require_admin") or path.startswith("/api/auth"):
        return
    if (verb, path) in ALLOW_LIST_EXEMPT_ROUTES:
        return
    assert _calls(handler, "_check_allow_list"), (
        f"{verb} {path} ({handler.name}) can modify data but does not call "
        f"_check_allow_list, so an off-list user can write. Call it, or add the "
        f"route to ALLOW_LIST_EXEMPT_ROUTES with a reason."
    )


def test_exemptions_are_all_real_routes():
    """An exemption that names a route which no longer exists hides a real gap."""
    actual = {(v, p) for v, p, _ in _mutating_routes()}
    assert ALLOW_LIST_EXEMPT_ROUTES <= actual, (
        f"stale exemptions: {ALLOW_LIST_EXEMPT_ROUTES - actual}"
    )


def test_handler_count_is_sane():
    """Guards against the parser silently finding nothing and passing vacuously."""
    assert len(_mutating_routes()) > 50, "route discovery broke; guard is vacuous"
    assert re.search(r'@app\.post\("/analyze"', MAIN_PY.read_text())
