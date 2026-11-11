"""Opening a chat answers 202 and prepares the workspace in the background (#511).

The request must never carry the clone/index/CI/session work: that is what made a
17-repository workspace a router timeout. What it may still carry is the preflight,
because a permission refusal has to arrive as a refusal, not as a retry hint.
"""

import asyncio
import errno
import logging
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from rootcoz import ai_client, main, storage
from rootcoz.models import ChatInitRequest
from tests.conftest import capture_main_logs, record_chat_pushes

KEY = "job:alice"


def _request(username="alice", role="reviewer", admin=False):
    return SimpleNamespace(
        state=SimpleNamespace(username=username, is_admin=admin, role=role)
    )


def _body(provider="openai", model="chosen"):
    return ChatInitRequest(ai_provider=provider, ai_model=model)


class Rig:
    """A Start wired down to fakes, so one test asserts one behaviour."""

    def __init__(self, monkeypatch, tmp_path):
        from rootcoz.engine import chat

        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.chat = chat
        self.clone_gate: asyncio.Event | None = None
        self.clone_entered = asyncio.Event()
        self.clone_returns = True
        self.clone_error: BaseException | None = None
        self.report_repos: list[str] = []
        self.failed_repos: list[str] = []
        self.session_id: str | None = "sid"
        self.session_observed: dict[str, object] = {}
        self.session_error: BaseException | None = None
        self.phases: list[tuple[str, str]] = []
        self.index_calls: list[object] = []
        self.session_tools_calls: list[object] = []
        self.request_params: dict[str, object] = {
            "tests_repo_url": "https://example.com/tests-repo"
        }
        self.clone_repo_dirs = True

    async def install(self, *, job_id="job"):
        from rootcoz.engine import session_tools
        from rootcoz.sources import chat_workspace

        self.job_id = job_id
        self.monkeypatch.setattr(storage, "DB_PATH", self.tmp_path / "chat.db")
        await storage.init_db()
        await storage.create_admin_user("alice")
        await storage.update_user_ai_credential(
            "alice",
            "openai",
            "user-key",  # pragma: allowlist secret
        )
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "status": "completed",
                "result": {"request_params": self.request_params},
                "request_params": self.request_params,
            },
        )
        self.monkeypatch.setattr(
            self.chat, "ensure_chat_workspace", lambda *a, **k: self.workspace
        )

        async def clone(
            workspace, params, user_repo_token="", on_repo=None, *, failed=None
        ):
            for name in self.report_repos:
                if on_repo:
                    on_repo(name)
                if self.clone_repo_dirs:
                    (self.workspace / name / ".git").mkdir(parents=True, exist_ok=True)
            self.clone_entered.set()
            if self.clone_gate is not None:
                await self.clone_gate.wait()
            if self.clone_error is not None:
                raise self.clone_error
            if failed is not None:
                failed.extend(self.failed_repos)
            return self.clone_returns

        self.monkeypatch.setattr(self.chat, "clone_chat_repos", clone)
        self.monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
        self.monkeypatch.setattr(
            main,
            "_resolve_chat_credentials",
            AsyncMock(return_value=("", "", None, "", "")),
        )
        self.monkeypatch.setattr(
            main, "_create_ai_auth_header", AsyncMock(return_value="")
        )

        async def validate(provider, model):
            ai_client._selected_credential_source.set("user")
            return provider, model

        self.monkeypatch.setattr(main, "_validate_catalog_pair", validate)

        async def create_session(**kwargs):
            self.session_observed = dict(kwargs)
            self.session_observed["source"] = ai_client.chat_session_source.get()
            if self.session_error is not None:
                raise self.session_error
            if self.session_id:
                await storage.save_ai_session_source(
                    self.session_id, "alice", kwargs["ai_provider"], "user"
                )
            return self.session_id

        self.monkeypatch.setattr(self.chat, "init_chat_session", create_session)

        def index(workspace, repos=None, job_id="", deadline=None):
            self.index_calls.append(deadline)

        self.monkeypatch.setattr(main, "_schedule_chat_index", index)

        async def build_tools(**kwargs):
            self.session_tools_calls.append(kwargs.get("wait_for_index"))
            return SimpleNamespace(
                custom=[], builtin=list(kwargs["builtin"]), graph_root=None
            )

        self.monkeypatch.setattr(session_tools, "build_session_tools", build_tools)

        original = main.set_chat_prep_phase

        def record_phase(job_id, username, phase, detail=""):
            self.phases.append((phase, detail))
            original(job_id, username, phase, detail)

        self.monkeypatch.setattr(main, "set_chat_prep_phase", record_phase)
        return self

    def history(self):
        return main.get_chat_history(self.job_id, _request(), limit=200, offset=0)

    def rows(self):
        return storage.get_chat_messages(self.job_id, username="alice")

    async def settle(self):
        """Wait for the spawned preparation, if one is still running."""
        entry = main._chat_prep_tasks.get(f"{self.job_id}:alice")
        if entry is None:
            return None
        try:
            return await entry.task
        except asyncio.CancelledError:
            return None


async def _cancel_live_preparation(job_id="job"):
    entry = main._chat_prep_tasks.get(f"{job_id}:alice")
    if entry is not None and not entry.task.done():
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)


# -- A1: the 202 is a promise, and the 4xx still arrive inline ------------------


@pytest.mark.asyncio
async def test_init_returns_202_before_clone(monkeypatch, tmp_path):
    """The response is out while the gate that holds the clone is still closed."""
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    rig.report_repos = ["repo"]
    try:
        response = await asyncio.wait_for(
            main.init_chat("job", _request(), _body()), timeout=2
        )
        assert response["ready"] is False
        assert response["preparing"] == {"phase": "preparing", "detail": ""}
        assert "session_id" not in response
        assert not rig.clone_gate.is_set(), "the clone was entered before the 202"
        assert await rig.rows() == [], "a preparation wrote a chat row before the 202"
    finally:
        await _cancel_live_preparation()
    history = await rig.history()
    assert history["preparing"] is None


@pytest.mark.asyncio
async def test_init_still_answers_its_preflight_errors_inline(monkeypatch, tmp_path):
    """Every precondition keeps answering inline, with no chat row behind it."""
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()

    rig.monkeypatch.setattr(
        main,
        "_enter_chat_workspace",
        AsyncMock(side_effect=HTTPException(status_code=409, detail="nope")),
    )
    with pytest.raises(HTTPException) as unknown:
        await asyncio.wait_for(main.init_chat("job", _request(), _body()), 2)
    assert unknown.value.status_code == 409
    assert main._chat_prep_phases.get(KEY) is None
    assert await rig.rows() == []

    # …and a Start that passes the preflight is still a 202.
    rig.monkeypatch.setattr(
        main, "_enter_chat_workspace", AsyncMock(return_value=rig.workspace)
    )
    started = await main.init_chat("job", _request(), _body())
    assert started["ready"] is False
    await _cancel_live_preparation()
    assert main._chat_prep_phases.get(KEY) is None


@pytest.mark.asyncio
async def test_deactivated_provider_and_missing_grant_stay_inline(
    monkeypatch, tmp_path
):
    """A permission refusal is never a 202 with a retry hint."""
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()

    async def deactivated(*_args, **_kwargs):
        raise HTTPException(status_code=400, detail="provider is deactivated")

    rig.monkeypatch.setattr(main, "_validate_chat_selection", deactivated)
    with pytest.raises(HTTPException) as refused:
        await main.init_chat("job", _request(), _body())
    assert refused.value.status_code == 400

    async def no_grant(*_args, **_kwargs):
        ai_client._selected_credential_source.set("")
        raise HTTPException(status_code=422, detail="grant required")

    rig.monkeypatch.setattr(main, "_validate_chat_selection", no_grant)
    with pytest.raises(HTTPException) as grant:
        await main.init_chat("job", _request(), _body())
    assert grant.value.status_code == 422

    main._chat_jobs_deleting.add("job")
    try:
        with pytest.raises(HTTPException) as missing:
            await main.init_chat("job", _request(), _body())
        assert missing.value.status_code == 404
    finally:
        main._chat_jobs_deleting.discard("job")
    assert await rig.rows() == []
    await _cancel_live_preparation()


@pytest.mark.asyncio
async def test_init_answers_when_preparation_is_cancelled_before_settle(
    monkeypatch, tmp_path
):
    """A job deleted under the preflight answers 404, and never waits for it."""
    rig = await Rig(monkeypatch, tmp_path).install()
    entered = asyncio.Event()

    async def hanging(*_args, **_kwargs):
        entered.set()
        await asyncio.sleep(30)

    rig.monkeypatch.setattr(main, "_enter_chat_workspace", hanging)

    async def delete_under_it():
        await entered.wait()
        main._chat_jobs_deleting.add("job")
        await _cancel_live_preparation()

    deleter = asyncio.create_task(delete_under_it())
    try:
        with pytest.raises(HTTPException) as gone:
            await asyncio.wait_for(main.init_chat("job", _request(), _body()), 5)
        assert gone.value.status_code == 404
    finally:
        main._chat_jobs_deleting.discard("job")
        # Bounded: this task only finishes if the preparation reached the
        # workspace, so an unbounded wait hangs the suite when it does not
        # (a RED run against the previous ``main.py``) instead of failing.
        await asyncio.wait_for(deleter, 5)
    assert main._chat_prep_tasks.get(KEY) is None
    assert main._chat_prep_phases.get(KEY) is None


@pytest.mark.asyncio
async def test_cancelled_preparation_without_a_delete_answers_503(
    monkeypatch, tmp_path
):
    """The only 503: a preparation cancelled before it settled, on a live job."""
    rig = await Rig(monkeypatch, tmp_path).install()
    entered = asyncio.Event()

    async def hanging(*_args, **_kwargs):
        entered.set()
        await asyncio.sleep(30)

    rig.monkeypatch.setattr(main, "_enter_chat_workspace", hanging)

    async def cancel_under_it():
        await entered.wait()
        await _cancel_live_preparation()

    canceller = asyncio.create_task(cancel_under_it())
    with pytest.raises(HTTPException) as cancelled:
        await asyncio.wait_for(main.init_chat("job", _request(), _body()), 5)
    await asyncio.wait_for(canceller, 5)
    assert cancelled.value.status_code == 503
    assert cancelled.value.detail == main._CHAT_PREP_CANCELLED_DETAIL
    assert main._chat_prep_tasks.get(KEY) is None
    assert main._chat_prep_phases.get(KEY) is None


@pytest.mark.asyncio
async def test_pre_settle_non_http_error_is_a_500_not_a_retry(
    monkeypatch, tmp_path, caplog
):
    """A bare ENOSPC is a real failure: the cause is logged, not swallowed."""
    rig = await Rig(monkeypatch, tmp_path).install()

    async def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    rig.monkeypatch.setattr(main, "_enter_chat_workspace", disk_full)
    with capture_main_logs(caplog), pytest.raises(HTTPException) as failed:
        await main.init_chat("job", _request(), _body())

    assert failed.value.status_code == 500
    assert "OSError" in caplog.text
    assert str(errno.ENOSPC) in caplog.text
    assert "job" in caplog.text and "alice" in caplog.text
    assert await rig.rows() == []
    assert main._chat_prep_phases.get(KEY) is None


@pytest.mark.asyncio
async def test_init_answers_202_while_a_turn_holds_the_job_barrier(
    monkeypatch, tmp_path
):
    """A turn's clone phase must not put the router timeout back on Start."""
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    try:
        async with main._get_chat_job_barrier("job"):
            response = await asyncio.wait_for(
                main.init_chat("job", _request(), _body()), timeout=1
            )
        assert response["ready"] is False
        assert response["preparing"]["phase"] == "preparing"
    finally:
        await _cancel_live_preparation()


# -- A2: progress, the push, and a registry that does not outlive its task ------


@pytest.mark.asyncio
async def test_phases_advance_and_session_appears(monkeypatch, tmp_path):
    """Every phase the UI renders is observable between the 202 and the session."""
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    rig.report_repos = ["one", "two", "three"]
    response = await main.init_chat("job", _request(), _body())
    assert response["ready"] is False
    rig.clone_gate.set()
    await rig.settle()

    assert [phase for phase, _detail in rig.phases] == [
        "preparing",
        "cloning",
        "cloning",
        "cloning",
        "fetching_build_data",
        "starting_session",
    ]
    history = await rig.history()
    assert history["preparing"] is None
    assert history["active_session"] is not None


@pytest.mark.asyncio
async def test_preparation_notifies_when_the_session_appears(monkeypatch, tmp_path):
    """The push is what enables the composer without a reload."""
    rig = await Rig(monkeypatch, tmp_path).install()
    notes: list[dict[str, object]] = []
    original = main.notify_chat_changed

    def record(job_id, username=""):
        notes.append({"rows": [r["status"] for r in _ROWS], "job": job_id})
        original(job_id, username=username)

    _ROWS = []
    rig.monkeypatch.setattr(main, "notify_chat_changed", record)
    response = await main.init_chat("job", _request(), _body())
    assert response["ready"] is False
    await rig.settle()

    history = await rig.history()
    assert history["active_session"] is not None
    assert notes, "a preparation that created a session never pushed a change"
    assert notes[-1]["job"] == "job"


@pytest.mark.asyncio
async def test_repreparation_with_an_existing_session_notifies(monkeypatch, tmp_path):
    """The re-used-session branch is a distinct ending, and it still pushes."""
    rig = await Rig(monkeypatch, tmp_path).install()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        "job",
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="chosen",
        session_id="sid",
    )
    seen: list[tuple[bool, dict[str, object] | None]] = []
    original = main.notify_chat_changed

    def record(job_id, username=""):
        seen.append((main._chat_prep_phases.get(f"{job_id}:{username}") is None, None))
        original(job_id, username=username)

    rig.monkeypatch.setattr(main, "notify_chat_changed", record)
    before = len(await rig.rows())

    response = await main.init_chat("job", _request(), _body())
    assert response["ready"] is False
    await rig.settle()

    assert ("starting_session", "") not in rig.phases, "the re-used branch ran"
    assert seen[-1][0] is True, "the final push arrived while the phase was live"
    assert len(await rig.rows()) == before, "the re-used branch writes no rows"


@pytest.mark.asyncio
async def test_registry_entry_is_dropped_on_every_ending(monkeypatch, tmp_path):
    """Success and failure both free the key; nothing answers 425 afterwards."""
    rig = await Rig(monkeypatch, tmp_path).install()
    await main.init_chat("job", _request(), _body())
    await rig.settle()
    assert main._chat_prep_tasks.get(KEY) is None

    rig.session_error = ai_client.SessionCreationRefused("no session for you")
    await main.init_chat("job", _request(), _body())
    await rig.settle()
    assert main._chat_prep_tasks.get(KEY) is None

    # Not a refusal any more: a fresh Start after a failure is allowed.
    rig.session_error = None
    response = await main.init_chat("job", _request(), _body())
    assert response["ready"] is False
    await rig.settle()
    assert main._refuse_while_preparing("job", "alice") is None


@pytest.mark.asyncio
async def test_a_late_ending_never_drops_a_newer_preparation(monkeypatch, tmp_path):
    """A's queued callback must not pop B's entry or clear B's phase."""
    await Rig(monkeypatch, tmp_path).install()
    handshake = main._ChatInitHandshake()
    task_a = asyncio.create_task(
        main._run_chat_preparation("job", "alice", _body(), handshake)
    )
    await asyncio.gather(task_a, return_exceptions=True)

    newer = asyncio.create_task(asyncio.sleep(30))
    main._chat_prep_tasks[KEY] = main._ChatPreparation(
        pair=("openai", "chosen"), task=newer
    )
    main.set_chat_prep_phase("job", "alice", "preparing")
    try:
        main._settle_chat_preparation("job", "alice", handshake, task_a)

        assert main._chat_prep_tasks[KEY].task is newer
        assert main._chat_prep_phases[KEY] == {"phase": "preparing", "detail": ""}
        # Its dedupe still answers 202 with the live phase …
        deduped = await main.init_chat("job", _request(), _body())
        assert deduped == {
            "ready": False,
            "preparing": {"phase": "preparing", "detail": ""},
        }
        # … and Clear is still refused for it.
        with pytest.raises(HTTPException) as refused:
            await main.clear_chat_history("job", _request())
        assert refused.value.status_code == 425
    finally:
        newer.cancel()
        await asyncio.gather(newer, return_exceptions=True)
        main._chat_prep_tasks.pop(KEY, None)
        main.clear_chat_prep_phase("job", "alice")


@pytest.mark.asyncio
async def test_init_answers_202_while_a_turn_holds_the_lock(monkeypatch, tmp_path):
    """The per-user lock covers the slow body only, so Start does not queue on it."""
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    try:
        async with main._hold_chat_lock(KEY):
            response = await asyncio.wait_for(
                main.init_chat("job", _request(), _body()), timeout=1
            )
            assert response["ready"] is False
            assert not rig.clone_entered.is_set(), "the clone ran under the turn"
        assert await asyncio.wait_for(rig.clone_entered.wait(), 2) is not None
    finally:
        rig.clone_gate.set()
        await rig.settle()


# -- A4: a second Start -------------------------------------------------------


@pytest.mark.asyncio
async def test_second_init_same_pair_is_idempotent(monkeypatch, tmp_path):
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    rig.report_repos = ["repo"]
    try:
        first = await main.init_chat("job", _request(), _body())
        entered = await asyncio.wait_for(rig.clone_entered.wait(), 2)
        assert entered is not False
        second = await main.init_chat("job", _request(), _body())
        assert second["ready"] is False
        assert second["preparing"] == {"phase": "cloning", "detail": "repo"}
        assert first["preparing"] == {"phase": "preparing", "detail": ""}
        assert rig.phases.count(("cloning", "repo")) == 1
    finally:
        rig.clone_gate.set()
        await rig.settle()
    history = await rig.history()
    assert history["active_session"] is not None
    assert (
        sum(1 for r in await rig.rows() if (r["content"] or "").startswith("👋")) == 1
    )


@pytest.mark.asyncio
async def test_second_init_with_other_pair_is_refused(monkeypatch, tmp_path):
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    try:
        await main.init_chat("job", _request(), _body())
        with pytest.raises(HTTPException) as conflict:
            await main.init_chat("job", _request(), _body(model="other"))
        assert conflict.value.status_code == 409
        assert conflict.value.detail == main._CHAT_PAIR_CONFLICT_DETAIL
        assert main._chat_prep_tasks[KEY] is not None, "the preparation was disturbed"
    finally:
        rig.clone_gate.set()
        await rig.settle()


@pytest.mark.asyncio
async def test_normalised_alias_is_not_a_conflict(monkeypatch, tmp_path):
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    try:
        await main.init_chat("job", _request(), _body(provider="claude"))
        deduped = await main.init_chat("job", _request(), _body(provider="cli-claude"))
        assert deduped["ready"] is False
    finally:
        rig.clone_gate.set()
        await rig.settle()


# -- A6: how failures reach the user -----------------------------------------


@pytest.mark.asyncio
async def test_unexpected_failure_writes_constant_sentence(
    monkeypatch, tmp_path, caplog
):
    rig = await Rig(monkeypatch, tmp_path).install()
    secret = "TOKEN-in-a-remote"  # pragma: allowlist secret
    rig.clone_gate = asyncio.Event()
    rig.clone_gate.set()
    rig.clone_error = OSError(
        f"fatal: could not read from https://user:{secret}@example/x"
    )

    with capture_main_logs(caplog, logging.DEBUG):
        response = await main.init_chat("job", _request(), _body())
        assert response["ready"] is False
        await rig.settle()

    failed = [r for r in await rig.rows() if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["content"] == main._CHAT_PREP_FAILED_DETAIL
    assert secret not in failed[0]["content"]
    assert secret not in caplog.text
    assert (await rig.history())["preparing"] is None


@pytest.mark.asyncio
async def test_session_refusal_becomes_chat_error(monkeypatch, tmp_path):
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.session_error = ai_client.SessionCreationRefused(
        f"{ai_client.SESSION_REFUSAL_MARKER}: no session"
    )
    await main.init_chat("job", _request(), _body())
    await rig.settle()

    failed = [r for r in await rig.rows() if r["status"] == "failed"]
    assert [r["content"] for r in failed] == [str(rig.session_error)]
    assert (await rig.history())["preparing"] is None


@pytest.mark.asyncio
async def test_transient_sidecar_status_becomes_chat_error(monkeypatch, tmp_path):
    import httpx

    rig = await Rig(monkeypatch, tmp_path).install()
    request = httpx.Request("POST", "https://sidecar/sessions")
    rig.session_error = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )
    await main.init_chat("job", _request(), _body())
    await rig.settle()

    failed = [r for r in await rig.rows() if r["status"] == "failed"]
    assert [r["content"] for r in failed] == [main._TRANSIENT_CHAT_START_DETAIL]
    assert (await rig.history())["preparing"] is None


@pytest.mark.asyncio
async def test_missing_repositories_are_named(monkeypatch, tmp_path):
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.failed_repos = ["broken-one", "broken-two"]
    await main.init_chat("job", _request(), _body())
    await rig.settle()

    system = [r for r in await rig.rows() if r["role"] == "system"]
    assert len(system) == 1
    assert "broken-one" in system[0]["content"]
    assert "broken-two" in system[0]["content"]
    assert (await rig.history())["active_session"] is not None
    assert (await rig.history())["preparing"] is None


@pytest.mark.asyncio
async def test_clone_failure_logs_no_token(monkeypatch, tmp_path, caplog):
    """The real clone loop, with a token-bearing remote in the error."""
    from git.exc import GitCommandError

    from rootcoz.engine.chat import clone_chat_repos
    from rootcoz.repository import RepositoryManager

    secret = "sekrit-token"  # pragma: allowlist secret

    def failing(*_args, **_kwargs):
        raise GitCommandError(
            "git clone",
            128,
            stderr=(
                "fatal: unable to access "
                f"'https://x-token-auth:{secret}@example.com/org/repo'"
            ),
        )

    monkeypatch.setattr(RepositoryManager, "clone_into", failing)
    failed: list[str] = []
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with capture_main_logs(caplog, logging.DEBUG, logger="rootcoz.engine.chat"):
        cloned = await clone_chat_repos(
            workspace,
            {"tests_repo_url": "https://example.com/org/tests-repo"},
            failed=failed,
        )
    assert cloned is False
    # Positive control: the capture does see this logger, so the absence below is
    # a real absence and not an empty capture.
    assert "failed to clone repo tests-repo" in caplog.text
    assert secret not in caplog.text
    assert failed == ["tests-repo"]
    assert all(secret not in name for name in failed)


@pytest.mark.asyncio
async def test_prep_task_pins_the_database_it_started_on(monkeypatch, tmp_path):
    """A preparation outlives its request and must not follow a DB rebind (#409)."""
    rig = await Rig(monkeypatch, tmp_path).install()
    started_on = storage.current_db_path()
    observed: list[object] = []
    gate = asyncio.Event()
    rig.clone_gate = gate

    original = rig.chat.clone_chat_repos

    async def clone(*args, **kwargs):
        observed.append(storage.current_db_path())
        return await original(*args, **kwargs)

    monkeypatch.setattr(rig.chat, "clone_chat_repos", clone)
    await main.init_chat("job", _request(), _body())
    storage.DB_PATH = tmp_path / "rebound.db"
    try:
        gate.set()
        await rig.settle()
    finally:
        storage.DB_PATH = started_on
    assert observed == [started_on]


@pytest.mark.asyncio
async def test_prep_uses_the_selection_it_validated(monkeypatch, tmp_path):
    """The credential source is derived inside the coroutine, and used there."""
    rig = await Rig(monkeypatch, tmp_path).install()
    await main.init_chat("job", _request(), _body())
    await rig.settle()
    assert rig.session_observed["source"] == "user"


# -- A9: deletion, cancellation and Clear ------------------------------------


@pytest.mark.asyncio
async def test_job_delete_cancels_a_real_preparation(monkeypatch, tmp_path):
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    rig.report_repos = ["repo"]
    monkeypatch.setattr(
        "rootcoz.engine.chat.cleanup_chat_workspace", lambda *a, **k: None
    )
    await main.init_chat("job", _request(), _body())
    await asyncio.wait_for(rig.clone_entered.wait(), 2)
    task = main._chat_prep_tasks[KEY].task
    assert task in main._chat_job_tasks["job"], "deletion cannot reach the preparation"

    await main._cleanup_deleted_job_chat_workspaces("job")

    assert task.cancelled() or task.done()
    assert main._chat_prep_tasks.get(KEY) is None
    assert main._chat_prep_phases.get(KEY) is None
    assert [r for r in await rig.rows() if r["session_id"]] == []


@pytest.mark.asyncio
async def test_cancelled_preparation_logs_and_reraises(monkeypatch, tmp_path, caplog):
    # A unique job: the project logger drops a repeated rendered line, so a
    # shared job id would make this assertion pass or fail on test order.
    rig = await Rig(monkeypatch, tmp_path).install(job_id="cancel-attribution-job")
    rig.clone_gate = asyncio.Event()
    rig.report_repos = ["repo"]
    handshake = main._ChatInitHandshake()
    task = asyncio.create_task(
        main._run_chat_preparation(
            "cancel-attribution-job", "alice", _body(), handshake
        )
    )
    # The route always registers the entry before spawning; without it the ending
    # helper has nothing of its own to end.
    main._chat_prep_tasks["cancel-attribution-job:alice"] = main._ChatPreparation(
        pair=("openai", "chosen"), task=task
    )
    await asyncio.wait_for(rig.clone_entered.wait(), 2)
    with capture_main_logs(caplog, logging.INFO):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "cancelled" in caplog.text.lower()
    assert "cancel-attribution-job" in caplog.text and "alice" in caplog.text
    assert "cloning" in caplog.text
    assert main._chat_prep_phases.get("cancel-attribution-job:alice") is None


@pytest.mark.asyncio
async def test_clear_is_refused_while_preparing(monkeypatch, tmp_path):
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.clone_gate = asyncio.Event()
    try:
        await main.init_chat("job", _request(), _body())
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(main.clear_chat_history("job", _request()), 2)
        assert refused.value.status_code == 425
        assert "preparing" in refused.value.detail
        assert rig.workspace.exists()
    finally:
        rig.clone_gate.set()
        await rig.settle()
    assert (await rig.history())["active_session"] is not None


# -- A8: an interrupted preparation resumes ----------------------------------


@pytest.mark.asyncio
async def test_reinit_after_interruption_reclones_only_missing(monkeypatch, tmp_path):
    """A complete repo is reused; a stale staging tree is removed and re-cloned."""
    from rootcoz.engine.chat import clone_chat_repos
    from rootcoz.repository import RepositoryManager

    workspace = tmp_path / "workspace"
    (workspace / "done-repo" / ".git").mkdir(parents=True)
    (workspace / ".staging" / "missing-repo" / ".git").mkdir(parents=True)
    cloned: list[str] = []

    def clone_into(_self, _url, target, **_kwargs):
        cloned.append(target.name)
        (target / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(RepositoryManager, "clone_into", clone_into)
    await clone_chat_repos(
        workspace,
        {
            "additional_repos": [
                {"name": "done-repo", "url": "https://example.com/done-repo"},
                {"name": "missing-repo", "url": "https://example.com/missing-repo"},
            ]
        },
    )

    assert cloned == ["missing-repo"]
    assert not (workspace / ".staging").exists()
    assert (workspace / "missing-repo" / ".git").is_dir()


# -- A3: a refused graph still yields a chat ---------------------------------


@pytest.mark.asyncio
async def test_deadline_does_not_fail_chat_init(monkeypatch, tmp_path):
    """A preparation whose index was refused still ends with a session."""
    rig = await Rig(monkeypatch, tmp_path).install()
    await main.init_chat("job", _request(), _body())
    await rig.settle()
    history = await rig.history()
    assert history["active_session"] is not None
    assert history["preparing"] is None


@pytest.mark.asyncio
async def test_index_and_session_tools_share_one_deadline(monkeypatch, tmp_path):
    """No turn waits for the graph: the index is scheduled, the tools are not."""
    rig = await Rig(monkeypatch, tmp_path).install()
    rig.report_repos = ["repo"]
    before = time.monotonic()
    await main.init_chat("job", _request(), _body())
    await rig.settle()

    assert len(rig.index_calls) == 1
    deadline = rig.index_calls[0]
    assert isinstance(deadline, float)
    assert before < deadline <= time.monotonic() + main.CHAT_INDEX_DEADLINE_SECONDS
    assert rig.session_tools_calls == [False], "the session build must not index"


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["refused", "transient"])
async def test_a_failed_row_write_does_not_lose_a_refusal_or_sidecar_sentence(
    monkeypatch, tmp_path, caplog, arm
):
    """#534: both arms must say why in the log when the failed row cannot be written.

    ``_chat_prep_failure_row`` is best-effort, so the transcript can legitimately
    carry no sentence — the server log is then the only record of the cause. The
    refusal arm added that line for this reason; the sidecar arm already had one
    and must keep exactly it.
    """
    import httpx

    rig = await Rig(monkeypatch, tmp_path).install()
    if arm == "refused":
        rig.session_error = ai_client.SessionCreationRefused(
            f"{ai_client.SESSION_REFUSAL_MARKER}: no session"
        )
    else:
        # 429, not an arbitrary status: ``_chat_start_sidecar_error`` re-raises
        # anything outside the transient set, so the row would never be attempted.
        request = httpx.Request("POST", "https://sidecar/sessions")
        rig.session_error = httpx.HTTPStatusError(
            "429", request=request, response=httpx.Response(429, request=request)
        )

    async def refuse(**_kwargs):
        raise sqlite3.OperationalError("no such table: chat_messages")

    monkeypatch.setattr(storage, "add_chat_message", refuse)

    with capture_main_logs(caplog), record_chat_pushes(monkeypatch) as pushes:
        caplog.clear()
        await main.init_chat("job", _request(), _body())
        await rig.settle()

    assert any(gone for gone, _ in pushes), "the push was silenced by the lost row"
    if arm == "refused":
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith(
                "Chat preparation refused for job job, user alice"
            )
        ]
        assert len(lines) == 1, "the refusal sentence was lost with the row"
        assert main._redacted_error(rig.session_error) in lines[0]
    else:
        assert any(
            "Chat start: sidecar answered a transient status 429" in r.getMessage()
            for r in caplog.records
        )
