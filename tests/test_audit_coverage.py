"""Mechanical audit-coverage gate (issue #379).

The audit trail is the only place an admin can learn what happened on the
server, so "we remembered to audit that endpoint" must not be a matter of
memory. This module parses ``main.py`` and fails when a mutating route writes
no audit row and is not in the explicit, reasoned exemption list below.

A route counts as audited when its handler body calls ``record_audit`` directly
or calls a helper whose body does. Adding a new endpoint without deciding
whether it is audited therefore breaks the build — the decision cannot be
skipped, only made.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[1] / "src" / "rootcoz" / "main.py"
MUTATING = {"post", "put", "delete", "patch"}

#: Mutating endpoints that intentionally write no audit row, each with the
#: reason recorded in the issue's "Documented skips" table.
AUDIT_EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/api/validate-token"): "validates a credential, stores nothing",
    ("POST", "/results/{job_id}/preview-github-issue"): "read-only preview",
    ("POST", "/results/{job_id}/preview-jira-bug"): "read-only preview",
    ("POST", "/api/feedback/preview"): "read-only preview",
    ("POST", "/api/jobs/metadata/rules/preview"): "read-only preview",
    ("POST", "/api/jira-projects"): "read-only lookup",
    ("POST", "/api/jira-security-levels"): "read-only lookup",
    ("POST", "/api/analyze-comment-intent"): "AI classification, stores nothing",
    # Transient user activity, not server state (see the scope rule in #379).
    ("POST", "/api/chat/{job_id}/init"): "chat session lifecycle",
    ("POST", "/api/chat/{job_id}/abort"): "chat session lifecycle",
    ("POST", "/api/chat/{job_id}/close"): "chat session lifecycle",
    ("DELETE", "/api/chat/{job_id}"): "chat session lifecycle",
    ("POST", "/api/chat/{job_id}"): "chat message activity, content never audited",
    ("POST", "/api/admin/chat/init"): "chat session lifecycle",
    ("POST", "/api/admin/chat/abort"): "chat session lifecycle",
    ("POST", "/api/admin/chat/close"): "chat session lifecycle",
    ("DELETE", "/api/admin/chat"): "chat session lifecycle",
    ("POST", "/api/admin/chat"): "chat message activity, content never audited",
    ("POST", "/api/admin-chat/artifacts"): "chat artifact, transient",
    ("POST", "/api/users/mentions/read"): "read-state marker",
    ("POST", "/api/users/mentions/read-all"): "read-state marker",
    ("POST", "/api/notifications/subscribe"): "notification toggle",
    ("POST", "/api/notifications/unsubscribe"): "notification toggle",
    ("POST", "/api/user/filter-presets"): "saved personal view",
    ("PUT", "/api/user/filter-presets/{preset_id}"): "saved personal view",
    ("DELETE", "/api/user/filter-presets/{preset_id}"): "saved personal view",
    (
        "POST",
        "/api/admin/settings/test/{integration}",
    ): "connectivity probe, stores nothing",
}


def _parse() -> tuple[list[tuple[str, str, str]], set[str]]:
    """Return ``[(method, path, handler)]`` and the names of auditing helpers."""
    tree = ast.parse(MAIN.read_text())
    helpers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "record_audit" in _called_names(node)
    }
    routes: list[tuple[str, str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if dec.func.attr not in MUTATING:
                continue
            path = ast.literal_eval(dec.args[0]) if dec.args else "?"
            routes.append((dec.func.attr.upper(), path, node.name))
    return routes, helpers


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _audited(node: ast.AST, helpers: set[str]) -> bool:
    called = _called_names(node)
    return "record_audit" in called or bool(called & helpers)


def test_every_mutating_route_is_audited_or_explicitly_exempt() -> None:
    tree = ast.parse(MAIN.read_text())
    routes, helpers = _parse()
    handlers = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [
        f"{method} {path} ({handler})"
        for method, path, handler in routes
        if not _audited(handlers[handler], helpers)
        and (method, path) not in AUDIT_EXEMPT
    ]
    assert not missing, (
        "These mutating endpoints write no audit row and are not exempt.\n"
        "Either audit them (one row per attempt, outcome + reason) or add them to\n"
        "AUDIT_EXEMPT with the reason recorded in issue #379:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_exemptions_are_not_stale() -> None:
    """A stale exemption hides a route that has since been renamed or removed."""
    routes = {(method, path) for method, path, _ in _parse()[0]}
    stale = sorted(f"{m} {p}" for (m, p) in AUDIT_EXEMPT if (m, p) not in routes)
    assert not stale, (
        "AUDIT_EXEMPT names endpoints that no longer exist; remove them so the "
        "list keeps meaning something:\n  " + "\n  ".join(stale)
    )


@pytest.mark.parametrize(
    "action",
    [
        "analysis.submit",
        "comment.add",
        "comment.enrich",
        "pattern.override",
        "job.tags_set",
        "job.metadata_set",
        "job.metadata_delete",
        "tracked_in.set",
        "tracked_in.delete",
        "feedback.create",
        "provider.models_refresh",
        "admin.settings_export",
        "audit.export",
    ],
)
def test_contract_action_is_implemented(action: str) -> None:
    """Every action id promised in the coverage contract exists in the code."""
    assert f'"{action}"' in MAIN.read_text(), (
        f"{action} is promised by the #379 coverage contract but is not recorded "
        "anywhere in main.py"
    )
