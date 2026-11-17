"""Mechanical audit-coverage gate (issue #379).

The audit trail is the only place an admin can learn what happened on the
server, so "we remembered to audit that endpoint" must not be a matter of
memory. This module parses ``main.py`` and fails when a mutating route has a
non-raising return path that writes no audit row and is not in the explicit,
reasoned exemption list below.

The decision is made on *control flow*, not on whether the word ``record_audit``
appears somewhere in the body (#566): a handler that audits one branch and
returns 200 on another would otherwise pass while the trail missed half its
attempts.

A route counts as audited when every one of its non-raising return paths
executes ``record_audit`` (directly or through a helper that always does).
Adding a new endpoint without deciding whether it is audited therefore breaks
the build — the decision cannot be skipped, only made.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[1] / "src" / "rootcoz" / "main.py"
MUTATING = {"post", "put", "delete", "patch"}

#: The empty "nothing audits unconditionally" set, shared to keep defaults immutable.
_NO_AUDIT: frozenset[str] = frozenset()

#: Helpers that audit an attempt only when it is refused or fails. A route that
#: calls one of these but never reaches a success-auditing path is not covered:
#: the trail would show only the denials of an endpoint whose happy path is
#: invisible.
REFUSAL_ONLY_HELPERS = frozenset(
    {"_record_refused_attempt", "_record_failed_attempt", "_refusal_exception"}
)

#: Mutating endpoints that intentionally write no audit row, each with the
#: reason recorded in the issue's "Documented skips" table.
AUDIT_EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/api/validate-token"): "validates a credential, stores nothing",
    # A POST only because the ticket is a credential and must not travel in a
    # query string: the route reads the ticket and returns what it stands for.
    ("POST", "/api/auth/sso/ticket"): "reads a ticket, stores nothing",
    # Same reason for the device flow's lookup: POST only so a live user code
    # never reaches an access log. It reads a code and stores nothing — only a
    # refused lookup writes a row, and that is the shared approve-refusal row.
    ("POST", "/api/auth/sso/device/approve/lookup"): "reads a code, stores nothing",
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
    (
        "POST",
        "/api/chat-issue/propose",
    ): "issue draft held in memory, files nothing",
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
    (
        "POST",
        "/api/user/tokens/test/{integration}",
    ): "connectivity probe, stores nothing",
}

#: Mutating endpoints that audit some attempts and legitimately write no row on
#: another (a no-op, a rejected upstream verification, a dry run). Each reason
#: has to say why the row-less path changes nothing an admin needs to trace.
AUDIT_BRANCH_EXEMPT: dict[tuple[str, str], str] = {
    (
        "DELETE",
        "/api/results/bulk",
    ): "audits one row per deleted job; zero-match bulk delete changes no state",
    (
        "POST",
        "/results/{job_id}/abort",
    ): "early return when job already ended or lost status race without aborting",
    (
        "POST",
        "/history/classify",
    ): "AI classifications belong to the analysis (#379); human overrides are audited",
    (
        "PUT",
        "/api/user/tokens",
    ): "empty payload or unchanged resubmitted fields write no state change",
    (
        "POST",
        "/api/admin/server-providers",
    ): "rejected or inconclusive key verification returns ok=false before storing a key",
    (
        "POST",
        "/api/admin/server-providers/{provider:path}/rotate-key",
    ): "rejected or inconclusive key verification returns ok=false before rotating the key",
    (
        # The deployment-credential entry point runs the same shared add path, so
        # it has the same ``ok=false`` verification branch.
        "POST",
        "/api/admin/server-providers/{provider:path}/store-deployment-key",
    ): "rejected or inconclusive key verification returns ok=false before storing a key",
    (
        "POST",
        "/api/admin/settings/import",
    ): "dry_run=true validates and diffs without writing settings",
    (
        # Public route: a call with no cookie, or naming a session row that is
        # already gone, clears nothing — auditing those would let anyone fill
        # the trail by probing the endpoint.
        "POST",
        "/api/auth/logout",
    ): "a cookie-less call, or one whose row is already gone, clears no session",
    (
        # The terminal's poll: pending, slow_down, expired and never-claimed all
        # return without minting anything. The mint, every refusal and the
        # approval are audited.
        "POST",
        "/api/auth/sso/device/poll",
    ): "pending, slow_down and expired polls hand out no session to audit",
}


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _parse() -> tuple[list[tuple[str, str, str]], dict[str, ast.AST]]:
    """Return ``[(method, path, handler)]`` and every module-level function."""
    tree = ast.parse(MAIN.read_text())
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
    return routes, _functions(tree)


def _definite_calls(node: ast.AST) -> set[str]:
    """Call names guaranteed to run while evaluating *node*.

    Short-circuited operands (``a or record_audit()``) and lazy bodies
    (lambdas, comprehensions) may never run, so they are not counted: a route
    whose only audit call sits behind them can return without writing a row.
    """
    if isinstance(node, ast.BoolOp):
        return _definite_calls(node.values[0])
    if isinstance(node, ast.IfExp):
        return _definite_calls(node.test)
    if isinstance(
        node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    ):
        return set()
    names: set[str] = set()
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    for child in ast.iter_child_nodes(node):
        names |= _definite_calls(child)
    return names


class _Outcomes:
    """The set of ``{False, True}`` outcomes over a function's return paths.

    ``False`` means "this return path ran no auditing call yet", ``True`` that
    one has definitely run. The union is the whole picture: ``{True}`` covers
    every path, ``{False, True}`` means some path returns unaudited.
    """

    def __init__(
        self,
        functions: dict[str, ast.AST],
        assume_audit: frozenset[str] = _NO_AUDIT,
    ) -> None:
        self.functions = functions
        self.assume_audit = assume_audit
        self._memo: dict[str, set[bool]] = {}

    def function_outcomes(self, name: str) -> set[bool]:
        node = self.functions.get(name)
        if node is None:
            return {True} if name in self.assume_audit else {False}
        if name in self._memo:
            return self._memo[name]
        # Recursion guard: a self-recursive helper is not evidence of coverage.
        self._memo[name] = {False}
        outcomes: set[bool] = set()
        live = self.block(node.body, {False}, outcomes)
        self._memo[name] = outcomes | live or {False}
        return self._memo[name]

    def audits(self, node: ast.AST, states: set[bool]) -> set[bool]:
        for name in _definite_calls(node):
            if name in REFUSAL_ONLY_HELPERS:
                continue
            if name == "record_audit":
                return {True}
            helper = self.function_outcomes(name)
            if helper == {True}:
                # Every incoming path just wrote a row.
                return {True}
            if helper == {False, True}:
                # The helper audits on some of its paths only, so this call
                # site inherits the ambiguity.
                states = states | {True}
        return states

    def block(
        self, stmts: list[ast.stmt], states: set[bool], outcomes: set[bool]
    ) -> set[bool]:
        for stmt in stmts:
            if not states:
                break
            states = self.step(stmt, states, outcomes)
        return states

    def step(self, stmt: ast.stmt, states: set[bool], outcomes: set[bool]) -> set[bool]:
        if isinstance(stmt, ast.Return):
            if stmt.value is not None:
                states = self.audits(stmt.value, states)
            outcomes |= states
            return set()
        if isinstance(stmt, ast.Raise):
            # A refusal that raises is not a coverage gap: the client sees the
            # error and the handler-level audits own those rows.
            return set()
        if isinstance(stmt, ast.If):
            body = self.block(stmt.body, set(states), outcomes)
            orelse = self.block(stmt.orelse, set(states), outcomes)
            return body | orelse
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            # A loop may run zero times, and ``break``/``else`` add paths.
            body = self.block(stmt.body, set(states), outcomes)
            orelse = self.block(stmt.orelse, set(states), outcomes)
            return body | orelse | states
        if isinstance(stmt, (ast.Try, ast.TryStar)):
            body = self.block(stmt.body, set(states), outcomes)
            # A try body that always returns or raises leaves nothing live, so
            # its start state must not survive here.
            after = set(body)
            for handler in stmt.handlers:
                # A handler starts from the state the try block began in.
                after |= self.block(handler.body, set(states), outcomes)
            if body:
                after |= self.block(stmt.orelse, set(body), outcomes)
            return self.block(stmt.finalbody, after, outcomes)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return self.block(stmt.body, states, outcomes)
        if isinstance(stmt, ast.Match):
            result: set[bool] = set()
            irrefutable = False
            for case in stmt.cases:
                if (
                    case.guard is None
                    and isinstance(case.pattern, ast.MatchAs)
                    and case.pattern.pattern is None
                ):
                    irrefutable = True
                result |= self.block(case.body, set(states), outcomes)
            if not irrefutable:
                result |= states
            return result
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return states
        return self.audits(stmt, states)


def _route_outcomes(
    fn: ast.AST,
    functions: dict[str, ast.AST] | None = None,
    assume_audit: frozenset[str] = _NO_AUDIT,
) -> set[bool]:
    """The ``{False, True}`` outcomes of every non-raising return path of *fn*."""
    outcomes: set[bool] = set()
    analyzer = _Outcomes(functions or {}, assume_audit)
    live = analyzer.block(fn.body, {False}, outcomes)  # type: ignore[attr-defined]
    # Falling off the end is an implicit ``return None`` path.
    return outcomes | live or {False}


def _audited(node: ast.AST, helpers: set[str]) -> bool:
    """Whether every non-raising return path of *node* writes an audit row."""
    return _route_outcomes(node, {}, frozenset(helpers)) == {True}


def test_every_mutating_route_is_audited_or_explicitly_exempt() -> None:
    routes, functions = _parse()
    missing = []
    for method, path, handler in routes:
        outcomes = _route_outcomes(functions[handler], functions)
        if outcomes == {True}:
            continue
        if (method, path) in AUDIT_EXEMPT:
            continue
        if (method, path) in AUDIT_BRANCH_EXEMPT and outcomes == {False, True}:
            continue
        missing.append(f"{method} {path} ({handler}) outcomes={sorted(outcomes)}")
    assert not missing, (
        "These mutating endpoints have a non-raising return path that writes no "
        "audit row and are not exempt.\n"
        "Either audit every path (one row per attempt, outcome + reason), or "
        "add them to\nAUDIT_EXEMPT (no row at all) or AUDIT_BRANCH_EXEMPT (some "
        "paths write no row) with the reason recorded in issue #379:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_a_refusal_only_helper_does_not_count_as_coverage() -> None:
    """Catching an HTTPException and auditing it is not the same as auditing."""
    node = ast.parse(
        "async def only_refuses(request):\n"
        "    try:\n"
        "        await do_the_thing()\n"
        "    except HTTPException as exc:\n"
        "        await _record_refused_attempt(exc=exc)\n"
        "        raise\n"
    ).body[0]

    assert not _audited(node, {"_record_refused_attempt"})


def test_a_success_auditing_helper_still_counts() -> None:
    node = ast.parse(
        "async def audited(request):\n    await _enqueue_audited_analysis(run=run)\n"
    ).body[0]

    assert _audited(node, {"_enqueue_audited_analysis"})
    assert not _audited(node, REFUSAL_ONLY_HELPERS)


def test_one_branch_audit_does_not_count_as_coverage() -> None:
    """The #566 defect: the success path returned while only the denial audited."""
    node = ast.parse(
        "async def handler(request):\n"
        "    if body.reviewed:\n"
        "        return {'status': 'ok'}\n"
        "    else:\n"
        "        await record_audit(action='review.delete')\n"
    ).body[0]

    assert _route_outcomes(node) == {False, True}
    assert not _audited(node, set())


def test_try_except_swallowing_without_audit_fails_gate() -> None:
    """A normal ``return`` inside ``try`` is a return path like any other."""
    node = ast.parse(
        "async def handler(request):\n"
        "    try:\n"
        "        return await work()\n"
        "    except ValueError:\n"
        "        return JSONResponse(status_code=400)\n"
    ).body[0]

    assert _route_outcomes(node) == {False}


def test_loop_only_audit_requires_branch_exemption() -> None:
    """A ``for`` body may run zero times, so the fall-through is unaudited."""
    node = ast.parse(
        "async def handler(request):\n"
        "    for job in jobs:\n"
        "        await record_audit(action='user.job_delete')\n"
        "    return {'ok': True}\n"
    ).body[0]

    assert _route_outcomes(node) == {False, True}


def test_a_short_circuited_audit_call_does_not_count() -> None:
    node = ast.parse(
        "async def handler(request):\n"
        "    return audited() and await record_audit(action='x')\n"
    ).body[0]

    assert _route_outcomes(node) == {False}


def test_a_helper_that_audits_only_one_branch_is_not_coverage() -> None:
    """Helpers are analysed too: one audited branch inside one is still a gap."""
    source = (
        "async def _maybe(action):\n"
        "    if action:\n"
        "        await record_audit(action=action)\n"
        "    return {'ok': True}\n"
        "\n"
        "async def handler(request):\n"
        "    return await _maybe('x')\n"
    )
    tree = ast.parse(source)
    functions = _functions(tree)
    assert _route_outcomes(functions["_maybe"], functions) == {False, True}
    assert _route_outcomes(functions["handler"], functions) == {False, True}
    assert not _audited(functions["handler"], set())


def test_exemptions_are_not_stale() -> None:
    """A stale exemption hides a route that has since been renamed or removed."""
    routes, functions = _parse()
    known = {(method, path) for method, path, _ in routes}
    outcomes = {
        (method, path): _route_outcomes(functions[handler], functions)
        for method, path, handler in routes
    }

    stale = sorted(f"{m} {p}" for (m, p) in AUDIT_EXEMPT if (m, p) not in known)
    assert not stale, (
        "AUDIT_EXEMPT names endpoints that no longer exist; remove them so the "
        "list keeps meaning something:\n  " + "\n  ".join(stale)
    )
    unbranched = sorted(
        f"{m} {p} outcomes={sorted(outcomes[(m, p)])}"
        for (m, p) in AUDIT_EXEMPT
        if (m, p) in known and outcomes[(m, p)] != {False}
    )
    assert not unbranched, (
        "AUDIT_EXEMPT routes are expected to audit nothing at all; these now "
        "write a row, so they belong in the contract instead:\n  "
        + "\n  ".join(unbranched)
    )

    stale_branch = sorted(
        f"{m} {p}" for (m, p) in AUDIT_BRANCH_EXEMPT if (m, p) not in known
    )
    assert not stale_branch, (
        "AUDIT_BRANCH_EXEMPT names endpoints that no longer exist:\n  "
        + "\n  ".join(stale_branch)
    )
    assert not [
        f"{m} {p}" for (m, p), reason in AUDIT_BRANCH_EXEMPT.items() if not reason
    ], "Every AUDIT_BRANCH_EXEMPT entry needs a reason."
    overlap = sorted(
        f"{m} {p}" for (m, p) in AUDIT_BRANCH_EXEMPT if (m, p) in AUDIT_EXEMPT
    )
    assert not overlap, (
        "A route cannot be both fully and partially exempt:\n  " + "\n  ".join(overlap)
    )
    misclassified = sorted(
        f"{m} {p} outcomes={sorted(outcomes[(m, p)])}"
        for (m, p) in AUDIT_BRANCH_EXEMPT
        if (m, p) in known and outcomes[(m, p)] != {False, True}
    )
    assert not misclassified, (
        "AUDIT_BRANCH_EXEMPT is for routes that audit some paths only; these are "
        "either fully unaudited (AUDIT_EXEMPT) or fully covered (remove the "
        "exemption):\n  " + "\n  ".join(misclassified)
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
        # A filed issue, and (from #502) the credential-policy denial that
        # refuses one — same action, ``outcome: denied``, no issue content.
        "issue.create",
        "issue_attachment.download",
        "issue_attachment.delete",
        "provider.models_refresh",
        "admin.settings_export",
        "audit.export",
        # The server reporting a document it did not store (#456).
        "result.enriched_xml_omitted",
        # §I account lockout, disable and unlock.
        "auth.account_locked",
        "admin.user_unlock",
        "admin.user_disabled",
        "admin.user_enabled",
        # A background startup action has no route for the walk above to see.
        "startup.workspace_reclaim",
        # A background results-retention pass (#455) has no route either.
        "storage.results_retention",
        # Both directions of the review toggle (#566).
        "review.marked",
        "review.delete",
        # §SSO first-party login (issue #197): the start, the success and every
        # refusal of the browser flow. The routes are GETs, so the mutating-route
        # gate never sees them — the contract list is what keeps them covered.
        "auth.sso_login_start",
        "auth.sso_login",
        "auth.sso_login_blocked",
        # First-login choice screen (Stage 6): held-open ticket, create, link.
        "auth.sso_choice_required",
        "auth.sso_create",
        "auth.sso_link",
        "auth.sso_login_failed",
        # CLI device flow (issue #197 Stage 8): the code is started, approved by
        # a signed-in person, and redeemed once. Start and poll are GET/POST on
        # a public path, so the mutating-route gate alone would not cover them.
        "auth.sso_device_started",
        "auth.sso_device_approved",
        # Every approval that is refused, including a code that is not there: the
        # refusal is the only trace of a code-guessing run.
        "auth.sso_device_approve_refused",
        "auth.sso_device_redeemed",
        # §Linking another provider (Stage 6c): the attach, and the detach by
        # the account owner and by an admin.
        "auth.identity_linked",
        "auth.identity_unlinked",
        "admin.identity_unlinked",
        # A removal that is refused (#Qodo round 5): retrying the last method, or
        # probing another account's providers, is otherwise invisible.
        "auth.identity_unlink_refused",
        # §Proxy auto-provision (Stage 5): the first request of a proxy-vouched
        # account creates its row, and that is the only record of when it appeared.
        "auth.proxy_provisioned",
        # A proxy identity the deployment refuses to create an account for is not
        # authenticated at all, so the refusal is the only trace of the attempt.
        "auth.proxy_provision_refused",
    ],
)
def test_contract_action_is_implemented(action: str) -> None:
    """Every action id promised in the coverage contract exists in the code."""
    assert f'"{action}"' in MAIN.read_text(), (
        f"{action} is promised by the #379 coverage contract but is not recorded "
        "anywhere in main.py"
    )
