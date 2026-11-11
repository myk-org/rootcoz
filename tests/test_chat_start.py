"""Explicit chat Start selects a user-owned credential source, not job defaults."""

import asyncio
import contextlib
import errno
import inspect
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

from rootcoz import ai_client, main, storage
from rootcoz.models import ChatInitRequest, ChatMessageRequest
from tests.conftest import capture_main_logs, record_chat_pushes


async def _settle(job_id: str = "job") -> None:
    """Wait for the spawned preparation, if one is still registered."""
    entry = main._chat_prep_tasks.get(f"{job_id}:alice")
    if entry is None:
        return
    try:
        await entry.task
    except asyncio.CancelledError:
        pass


def _error_rows(messages):
    return [m for m in messages if m.get("status") == "failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_start_requires_pair_and_send_requires_start(
    admin, monkeypatch, tmp_path
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    if not admin:
        await storage.save_result(
            "job",
            "",
            "completed",
            {
                "status": "completed",
                "result": {},
                "request_params": {"ai_provider": "openai", "ai_model": "old"},
            },
        )
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ChatInitRequest.model_validate({})
    with pytest.raises(HTTPException) as exc:
        if admin:
            await main.send_admin_chat_message(
                ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
        else:
            await main.send_chat_message(
                "job", ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
    assert exc.value.status_code == 409
    assert (
        await storage.count_chat_messages(
            main.ADMIN_CHAT_JOB_ID if admin else "job", username="alice"
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_start_and_send_use_selected_source_not_job_or_settings(
    admin, monkeypatch, tmp_path
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "status": "completed",
                "ai_provider": "other",
                "ai_model": "old",
                "request_params": {},
            },
        )
    from rootcoz.config import Settings

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(ai_provider="other", ai_model="old", ai_call_timeout=30),
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: tmp_path)
    monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main,
        "_resolve_chat_credentials",
        AsyncMock(return_value=("", "", None, "", "")),
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def validate(p, m):
        ai_client._selected_credential_source.set("user")
        return p, m

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)

    async def create(**kwargs):
        assert (kwargs["ai_provider"], kwargs["ai_model"]) == ("openai", "chosen")
        assert ai_client.chat_session_source.get() == "user"
        await storage.save_ai_session_source("sid", "alice", "openai", "user")
        return "sid"

    monkeypatch.setattr(
        chat, "init_admin_chat_session" if admin else "init_chat_session", create
    )
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    body = {"ai_provider": "openai", "ai_model": "chosen"}
    result = await (
        main.init_admin_chat(request, ChatInitRequest(**body))
        if admin
        else main.init_chat(job_id, request, ChatInitRequest(**body))
    )
    if admin:
        assert result["session_id"] == "sid"
    else:
        # Job chat prepares in the background: the 202 carries no session.
        assert result["ready"] is False
        await _settle()
        active = await storage.get_latest_chat_session(job_id, "alice")
        assert active is not None
    tasks = BackgroundTasks()
    sent = await (
        main.send_admin_chat_message(ChatMessageRequest(message="hi"), request, tasks)
        if admin
        else main.send_chat_message(
            job_id, ChatMessageRequest(message="hi"), request, tasks
        )
    )
    assert sent["assistant_message_id"]
    assert tasks.tasks[0].kwargs.get("credential_source") == "user"
    for override in ({"ai_provider": "openai", "ai_model": "different"},):
        with pytest.raises(HTTPException) as exc:
            if admin:
                await main.send_admin_chat_message(
                    ChatMessageRequest(message="hi", **override),
                    request,
                    BackgroundTasks(),
                )
            else:
                await main.send_chat_message(
                    job_id,
                    ChatMessageRequest(message="hi", **override),
                    request,
                    BackgroundTasks(),
                )
        assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_send_after_user_key_removed_requires_new_start(
    monkeypatch, tmp_path, admin
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "send-switch.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id, "", "completed", {"status": "completed", "result": {}}
        )
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="m",
        session_id="sid",
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "m"}]),
    )
    monkeypatch.setattr(
        ai_client, "supported_key_providers", AsyncMock(return_value=["openai"])
    )
    # Simulate a lost key without clearing the session marker through the rotation endpoint.
    from rootcoz.storage import _connect_db, encrypt_value

    async with _connect_db() as db:
        await db.execute(
            "UPDATE users SET ai_credentials_enc = ? WHERE username = ?",
            (encrypt_value("{}"), "alice"),
        )
        await db.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    with pytest.raises(HTTPException) as exc:
        if admin:
            await main.send_admin_chat_message(
                ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
        else:
            await main.send_chat_message(
                job_id, ChatMessageRequest(message="hi"), request, BackgroundTasks()
            )
    assert exc.value.status_code == 409
    assert "Start" in str(exc.value.detail)
    assert await storage.count_chat_messages(job_id, username="alice") == 1


@pytest.mark.asyncio
async def test_server_grant_revoked_cannot_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    await storage.save_ai_session_source("sid", "alice", "openai", "server")
    await storage.add_chat_message(
        "job",
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="m",
        session_id="sid",
    )
    from rootcoz.ai_client import require_server_provider_grant

    token = ai_client.ai_username.set("alice")
    try:
        assert (await main._current_chat_choice("job", "alice"))[1] == "server"
        await storage.set_user_can_use_server_providers("alice", False)
        with pytest.raises(ValueError, match="grant"):
            await require_server_provider_grant()
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_revoked_key_cannot_resume_or_fall_back_to_server(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key")
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        "job",
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="m",
        session_id="sid",
    )
    await storage.update_user_ai_credential("alice", "openai", "new-key")
    with pytest.raises(HTTPException) as exc:
        await main._current_chat_choice("job", "alice")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_chat_selection_uses_server_source_when_granted(monkeypatch, tmp_path):
    """A pair no user key reaches comes from the server pool for a granted caller."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "choice.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "m"}]),
    )
    provider, model = await main._validate_chat_selection(
        ChatInitRequest(ai_provider="openai", ai_model="m"), "alice"
    )
    assert (provider, model) == ("openai", "m")
    assert ai_client._selected_credential_source.get() == "server"


@pytest.mark.asyncio
async def test_start_rejects_server_without_grant(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_result(
        "job", "", "completed", {"status": "completed", "request_params": {}}
    )
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "openai", "id": "m"}]),
    )
    await storage.create_user("bob", role="reviewer", can_use_server_providers=False)
    request = SimpleNamespace(
        state=SimpleNamespace(username="bob", is_admin=False, role="reviewer")
    )
    with pytest.raises(HTTPException) as exc:
        await main.init_chat(
            "job",
            request,
            ChatInitRequest(ai_provider="openai", ai_model="m"),
        )
    assert exc.value.status_code == 422
    assert await storage.count_chat_messages("job", username="bob") == 0


@pytest.mark.asyncio
async def test_new_session_replaces_stale_welcome_but_keeps_replies(
    monkeypatch, tmp_path
):
    """A dead session's welcome is chrome and must not stack up in history."""
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    job_id = "job"
    await storage.save_result(job_id, "", "completed", {"result": {}})
    from rootcoz.config import Settings

    monkeypatch.setattr(main, "get_settings", lambda: Settings(ai_call_timeout=30))
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: tmp_path)
    monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main,
        "_resolve_chat_credentials",
        AsyncMock(return_value=("", "", None, "", "")),
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def validate(p, m):
        ai_client._selected_credential_source.set("user")
        return p, m

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)

    sessions = iter(["sid-1", "sid-2"])

    async def create(**kwargs):
        sid = next(sessions)
        await storage.save_ai_session_source(sid, "alice", "openai", "user")
        return sid

    monkeypatch.setattr(chat, "init_chat_session", create)
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True, role="admin")
    )
    body = ChatInitRequest(ai_provider="openai", ai_model="chosen")
    await main.init_chat(job_id, request, body)
    await _settle()

    # A real exchange that must survive the next session.
    await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="openai", ai_model="chosen"
    )

    # The first session dies: its user credential is no longer available.
    await storage.update_user_ai_credential("alice", "openai", None)
    await main.init_chat(job_id, request, body)
    await _settle()

    messages = await storage.get_chat_messages(job_id, username="alice")
    assert sum(1 for m in messages if (m["content"] or "").startswith("👋")) == 1
    assert any((m["content"] or "") == "hello" for m in messages)


# -- A refused session is a chat message, never a 500 (#423, #511) ------------
#
# The sidecar needs declared token limits to create a keyed session; a
# listed-but-undeclared model carries none, so POST /sessions answers 400. On the
# job-chat route the preparation is already a background task by then, so the
# refusal is one failed chat row with the same sentence — admin chat init is still
# synchronous and still answers 422. The sidecar stand-in and the Start rig live
# in conftest (``refusing_sidecar``, ``install_start_rig``).


def _start_request(admin: bool):
    """The request the route reads: ``admin`` must reach the caller's identity."""
    return SimpleNamespace(
        state=SimpleNamespace(
            username="alice", is_admin=admin, role="admin" if admin else "reviewer"
        )
    )


@pytest.mark.asyncio
async def test_chat_init_maps_sidecar_refusal_to_a_chat_message(
    refusing_sidecar, install_start_rig, refusal_secret_key
):
    """A stored server key refused with the limits text is a followable message."""
    client = refusing_sidecar(
        400, json_body={"error": ai_client._SIDECAR_LIMITS_REFUSAL}
    )
    await install_start_rig(
        credential_source="server", client=client, job_id="refusal-job", admin=False
    )
    response = await main.init_chat(
        "refusal-job",
        _start_request(admin=False),
        ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
    )
    assert response["ready"] is False
    await _settle("refusal-job")

    rows = _error_rows(await storage.get_chat_messages("refusal-job", username="alice"))
    assert len(rows) == 1
    sentence = rows[0]["content"]
    assert sentence == (
        f"{ai_client.SESSION_REFUSAL_MARKER}: {ai_client._SIDECAR_LIMITS_REFUSAL}"
    )
    assert "models.json" in sentence
    assert refusal_secret_key not in sentence


@pytest.mark.asyncio
async def test_refusal_exception_carries_no_body_and_no_context(
    caplog, refusing_sidecar, install_start_rig, refusal_secret_key
):
    """The sentence is built inside its except and raised outside it.

    A raw non-JSON body carrying a key yields rootcoz's own sentence, the key
    reaches no log record, and ``__context__`` holds no ``HTTPStatusError``.
    """
    caplog.set_level(logging.DEBUG)
    client = refusing_sidecar(
        400, raw_text=f"upstream said no for {refusal_secret_key}"
    )
    await install_start_rig(
        credential_source="server", client=client, job_id="refusal-body", admin=False
    )
    with capture_main_logs(caplog, logging.DEBUG, logger=ai_client.logger):
        with pytest.raises(ai_client.SessionCreationRefused) as exc:
            await ai_client.create_session_safely(
                client,
                provider="openai",
                model="undeclared",
                system_prompt="hi",
                server_api_key=refusal_secret_key,
            )
        # The refusal path logs nothing on purpose, so the control has to be
        # synthetic: this record proves the capture reaches the logger at all.
        ai_client.logger.debug("capture control reached caplog")
    sentence = str(exc.value)
    assert sentence.startswith(f"{ai_client.SESSION_REFUSAL_MARKER}: ")
    assert "could not be created for openai/undeclared" in sentence
    assert "upstream said no" not in sentence
    assert exc.value.__context__ is None
    assert "capture control reached caplog" in caplog.text
    assert refusal_secret_key not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_source", "stored_server_key", "job_id"),
    [
        ("server", None, "refusal-unkeyed"),
        ("user", None, "refusal-user"),
    ],
)
async def test_chat_init_maps_unkeyed_and_user_keyed_refusals_to_a_message(
    monkeypatch,
    refusing_sidecar,
    install_start_rig,
    credential_source,
    stored_server_key,
    job_id,
):
    """The unkeyed library call and the user-keyed create map their 4xx too.

    Both branches re-raise ``httpx.HTTPError`` today, which the route turns into
    a 500 — the branches test 8's stored-server-key path does not reach.
    """
    client = refusing_sidecar(
        400, json_body={"error": ai_client._SIDECAR_LIMITS_REFUSAL}
    )
    await install_start_rig(
        credential_source=credential_source,
        client=client,
        job_id=job_id,
        admin=False,
    )
    if stored_server_key is None and credential_source == "server":
        # No stored server key: create_session_safely takes the library path,
        # which re-raises the raw httpx error.
        monkeypatch.setattr(
            ai_client, "server_provider_key", AsyncMock(return_value=None)
        )
    response = await main.init_chat(
        job_id,
        _start_request(admin=False),
        ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
    )
    assert response["ready"] is False
    await _settle(job_id)
    rows = _error_rows(await storage.get_chat_messages(job_id, username="alice"))
    assert [r["content"] for r in rows] == [
        f"{ai_client.SESSION_REFUSAL_MARKER}: {ai_client._SIDECAR_LIMITS_REFUSAL}"
    ]


@pytest.mark.asyncio
async def test_admin_chat_init_maps_refusal_to_422(refusing_sidecar, install_start_rig):
    """The admin-chat route reaches the same mapping instead of a ready session.

    Today ``_create_chat_session`` swallows the refusal into ``None`` and the
    route answers ``200 {"ready": true, "session_id": ""}``.
    """
    client = refusing_sidecar(
        400, json_body={"error": ai_client._SIDECAR_LIMITS_REFUSAL}
    )
    await install_start_rig(
        credential_source="server",
        client=client,
        job_id=main.ADMIN_CHAT_JOB_ID,
        admin=True,
    )
    with pytest.raises(HTTPException) as exc:
        await main.init_admin_chat(
            _start_request(admin=True),
            ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith(ai_client.SESSION_REFUSAL_MARKER)
    assert "session_id" not in repr(exc.value)


# -- A transient creation status is a retry, never a 500 (#423) ------------
#
# 408/425/429 stay out of the refusal sentence on purpose: that marker is the
# non-retryable signal the analysis engine stops on. Both chat-start routes have
# no retry loop of their own, so they must name the retry instead of letting the
# ``httpx.HTTPStatusError`` escape as a 500.


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 425, 429])
async def test_chat_init_answers_a_transient_creation_status_with_retry(
    refusing_sidecar, install_start_rig, status
):
    """A timeout or rate limit at Start tells the user to retry, not 500."""
    client = refusing_sidecar(status, raw_text="slow down")
    job_id = f"transient-{status}"
    await install_start_rig(
        credential_source="server", client=client, job_id=job_id, admin=False
    )
    response = await main.init_chat(
        job_id,
        _start_request(admin=False),
        ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
    )
    assert response["ready"] is False
    await _settle(job_id)
    rows = _error_rows(await storage.get_chat_messages(job_id, username="alice"))
    assert [r["content"] for r in rows] == [main._TRANSIENT_CHAT_START_DETAIL]
    # A refusal marker would make the analysis engine stop retrying.
    assert ai_client.SESSION_REFUSAL_MARKER not in rows[0]["content"]


@pytest.mark.asyncio
async def test_admin_chat_init_answers_a_transient_creation_status_with_retry(
    refusing_sidecar, install_start_rig
):
    """The admin-chat route shapes the same transient status instead of 500."""
    client = refusing_sidecar(429, raw_text="slow down")
    await install_start_rig(
        credential_source="server",
        client=client,
        job_id=main.ADMIN_CHAT_JOB_ID,
        admin=True,
    )
    with pytest.raises(HTTPException) as exc:
        await main.init_admin_chat(
            _start_request(admin=True),
            ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == main._TRANSIENT_CHAT_START_DETAIL


async def _contended_once(
    monkeypatch: pytest.MonkeyPatch, counter: dict, message: str = "database is locked"
) -> None:
    """Make the next chat-row write lose the lock once, then work normally."""
    real_connect = storage._connect_db

    @contextlib.asynccontextmanager
    async def flaky_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        counter["connect"] = counter.get("connect", 0) + 1
        if counter["connect"] == 1:
            counter["contended"] = counter.get("contended", 0) + 1
            raise sqlite3.OperationalError(message)
        async with real_connect(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(storage, "_connect_db", flaky_connect)
    monkeypatch.setattr(storage, "_LOCK_RETRY_BACKOFF_SECONDS", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["message", "source"])
@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
async def test_a_session_row_survives_one_contended_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write: str, message: str
):
    """A momentary lock must not cost the session a Start just created.

    The preparation answers 202 before this write, so a lost row is a session the
    user is told is ready and then never sees — the failure the chat-init test
    caught on CI as ``active_session: None``.
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    counter: dict = {}
    await _contended_once(monkeypatch, counter, message)

    if write == "message":
        msg_id = await storage.add_chat_message(
            "job", "assistant", "", username="alice", session_id="sess-1"
        )
        assert msg_id > 0, "the session marker row must exist"
        assert [m["session_id"] for m in await storage.get_chat_messages("job")] == [
            "sess-1"
        ]
    else:
        await storage.save_ai_session_source("sess-1", "alice", "claude", "server")
        assert (
            await storage.get_ai_session_source("sess-1", "alice", "claude") == "server"
        )
    assert counter["contended"] == 1, "the lock was retried, not surfaced"


@pytest.mark.asyncio
async def test_a_welcome_prune_survives_one_contended_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The welcome prune is a write on the preparation path, so it is retried too.

    R5: the session row landed and the preparation still reported "could not be
    prepared" one busy_timeout later, because this DELETE was the one write the
    retry did not cover — the user kept the session and lost the welcome row.
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.add_chat_message("job", "assistant", "welcome", username="alice")
    counter: dict = {}
    await _contended_once(monkeypatch, counter)

    assert await storage.delete_chat_welcome_messages("job", "alice") == 1
    assert await storage.get_chat_messages("job", username="alice") == [], (
        "the stale welcome must be gone"
    )
    assert counter["contended"] == 1, "the lock was retried, not surfaced"


#: Statements that take the write lock: the first one on a connection is where a
#: held lock answers ``database is locked``.
_WRITE_KEYWORDS = ("insert", "update", "delete", "replace")


def _is_lock_statement(sql: str) -> bool:
    words = sql.lstrip().split(None, 2)
    keyword = words[0].lower() if words else ""
    return keyword in _WRITE_KEYWORDS or (
        keyword == "begin" and len(words) > 1 and words[1].lower() == "immediate"
    )


class _HeldLock:
    """A lock held across the first attempt of *every* write, for one whole run.

    A real held lock is not repeatable in a unit test, so this hands out the same
    answer deterministically: the first attempt of a statement loses the lock, and
    the connection a retry opens for that same statement is let through. A write
    nothing retries therefore fails exactly as the R5 run failed, which is what
    makes a preparation-level test cover the path instead of one named function.
    """

    #: What a contended statement raises; the connection raises the holder's own.
    exc = sqlite3.OperationalError("database is locked")

    def __init__(self) -> None:
        self.awaiting_retry: set[str] = set()
        self.contended = 0

    def wrap(self, db):
        return _FaultedWrite(db, self)

    def should_fail(self, sql: str) -> str:
        if not _is_lock_statement(sql):
            return "pass"
        if sql in self.awaiting_retry:
            self.awaiting_retry.discard(sql)
            return "retry"
        self.awaiting_retry.add(sql)
        self.contended += 1
        return "fail"


class _FragmentFault:
    """The other holder: fail one statement, matched by a fragment of its SQL.

    ``sql`` is compared case-insensitively — the real statement is upper case
    (``INSERT INTO sessions …``). ``times`` is the budget: ``None`` fails every
    attempt (an exhausted retry), ``1`` fails the first and lets the retry
    through. ``attempts`` counts the matches that reached the write, which is what
    makes "a fault is not retried" and "a lock is retried four times" observable.
    """

    def __init__(self, sql: str, exc: BaseException, times: int | None = 1) -> None:
        self.sql = sql.lower()
        self.exc = exc
        self.times = times
        self.attempts = 0

    def wrap(self, db):
        return _FaultedWrite(db, self)

    def should_fail(self, sql: str) -> str:
        if self.sql not in sql.lower():
            return "pass"
        if self.times is not None and self.attempts >= self.times:
            return "pass"
        self.attempts += 1
        return "fail"


class _FaultedWrite:
    """Wrap one live connection; ask the holder whether this statement fails.

    The per-connection "this connection is a retry, let it through" flag lives
    here. The per-statement decision belongs to the holder — that is the split
    ``_HeldLock``'s once-per-connection / once-per-statement behaviour needs.
    """

    def __init__(self, db, holder) -> None:
        self._db = db
        self._holder = holder
        #: This connection is a retry, so every write on it may commit.
        self._retried = False

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def execute(self, sql, *args, **kwargs):
        self._gate(sql)
        return await self._db.execute(sql, *args, **kwargs)

    async def executemany(self, sql, *args, **kwargs):
        self._gate(sql)
        return await self._db.executemany(sql, *args, **kwargs)

    def _gate(self, sql: str) -> None:
        if self._retried:
            return
        verdict = self._holder.should_fail(sql)
        if verdict == "fail":
            raise self._holder.exc
        if verdict == "retry":
            self._retried = True


@pytest.mark.asyncio
async def test_the_whole_preparation_survives_a_lock_on_every_write(
    monkeypatch, tmp_path, install_start_rig
):
    """R5, closed as a class: prepare a chat while every write loses the lock once.

    Two rounds pinned one write each and the run then failed on the next write
    along — ``create_ai_session_with_source``, which the preparation reaches
    before either of them. So this drives the real preparation end to end
    (``/init`` preflight, session create with its provenance row, session marker,
    welcome prune, welcome row) under one held lock and asserts it ends ready with
    its rows present. It fails if *any* write on that path stops being retried.
    """
    created: list[dict] = []
    headers, capture_header = _capture_header()
    await install_start_rig(
        credential_source="server",
        client=_recording_sidecar(created),
        job_id="job",
        admin=False,
        header=capture_header,
    )

    held = _HeldLock()
    real_connect = storage._connect_db

    @contextlib.asynccontextmanager
    async def locked_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        async with real_connect(*args, **kwargs) as db:
            yield held.wrap(db)

    monkeypatch.setattr(storage, "_connect_db", locked_connect)
    monkeypatch.setattr(storage, "_LOCK_RETRY_BACKOFF_SECONDS", 0)

    started = await main.init_chat(
        "job",
        _start_request(admin=False),
        ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
    )
    assert started["ready"] is False
    await _settle("job")

    active = await storage.get_latest_chat_session("job", "alice")
    assert active is not None, "the preparation ended without its session row"
    assert active["session_id"] == "sess-1"
    assert headers and headers[0].startswith("Bearer "), (
        "the internal tools token was lost to the lock"
    )
    assert (
        await storage.get_ai_session_source("sess-1", "alice", "openai") == "server"
    ), "the session has no provenance row"
    contents = [m["content"] for m in await storage.get_chat_messages("job")]
    assert any(c.startswith("👋 Welcome!") for c in contents), (
        "the preparation ended without its welcome row"
    )
    assert not _error_rows(await storage.get_chat_messages("job", username="alice")), (
        "the preparation reported a failure row"
    )
    assert held.contended >= 5, "every write on the path must be exercised"


# -- A genuine storage fault refuses the preparation (#534) ------------------
#
# A lock is transient: retried, then degraded for (#526). Anything else is a real
# storage fault, and a chat that starts with no internal tools is a broken chat
# dressed as a working one — so at the job-chat preparation the mint re-raises and
# the existing failure surface tells the user. Every other caller keeps the
# graceful-degradation ``""``.


@contextlib.contextmanager
def _wrapped_connections(monkeypatch, holder):
    """Run every storage connection through a fault holder."""
    real_connect = storage._connect_db

    @contextlib.asynccontextmanager
    async def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        async with real_connect(*args, **kwargs) as db:
            yield holder.wrap(db)

    monkeypatch.setattr(storage, "_connect_db", wrapped)
    monkeypatch.setattr(storage, "_LOCK_RETRY_BACKOFF_SECONDS", 0)
    yield holder


def _recording_sidecar(created: list[dict]) -> SimpleNamespace:
    """A sidecar stand-in that records what the session create was asked for."""

    class _Http:
        async def post(self, url, json=None):  # type: ignore[no-untyped-def]
            created.append(dict(json or {}))
            return httpx.Response(
                200,
                json={"session_id": f"sess-{len(created)}"},
                request=httpx.Request("POST", f"http://sidecar.test{url}"),
            )

    async def create_session(**kwargs):
        created.append(kwargs)
        return f"sess-{len(created)}"

    return SimpleNamespace(
        _client=_Http(), create_session=create_session, delete_session=AsyncMock()
    )


def _capture_header() -> tuple[list[str], object]:
    """The real helper, wrapped so a test can see what the mint returned.

    The internal tools token is required at this call site: a lock is retried and
    degraded for, a non-transient storage fault fails the preparation instead of
    costing the chat its tools silently, so the wrapper forwards ``**kwargs`` —
    accepting and dropping ``refuse_storage_fault`` would leave the changed call
    site unexercised.
    """
    headers: list[str] = []
    real_header = main._create_ai_auth_header

    async def capture(username: str, is_admin: bool = False, **kwargs: object) -> str:
        value = await real_header(username, is_admin, **kwargs)
        headers.append(value)
        return value

    return headers, capture


@pytest.mark.asyncio
async def test_a_storage_fault_minting_the_internal_tools_token_fails_the_preparation(
    monkeypatch,
    tmp_path,
    caplog,
    install_start_rig,
):
    """#534: a non-transient fault is not silent, and Start works afterwards.

    The fault is injected at the statement (``INSERT INTO sessions``), so the
    real retry is in the path — and it must not retry a fault the way it retries a
    lock. What the user gets is the shared preparation-failure sentence and a
    ``Start Chat`` that is ready to retry; what they must not get is a chat that
    says ready with no internal tools.
    """
    created: list[dict] = []
    headers, capture = _capture_header()
    await install_start_rig(
        credential_source="server",
        client=_recording_sidecar(created),
        job_id="job",
        admin=False,
        header=capture,
    )
    fault = _FragmentFault(
        "insert into sessions",
        sqlite3.OperationalError("no such table: sessions"),
        times=1,
    )

    request = _start_request(admin=False)
    body = ChatInitRequest(ai_provider="openai", ai_model="undeclared")
    with _wrapped_connections(monkeypatch, fault):
        with capture_main_logs(caplog), record_chat_pushes(monkeypatch) as pushes:
            caplog.clear()
            started = await main.init_chat("job", request, body)
            # The refusal is reported in the transcript, not inline: init answered
            # its ordinary 202 payload before the preparation ever reached the mint.
            assert started["ready"] is False
            assert started["preparing"]
            await _settle("job")

            rows = _error_rows(await storage.get_chat_messages("job", username="alice"))
            assert [r["content"] for r in rows] == [main._CHAT_PREP_FAILED_DETAIL]
            assert await storage.get_latest_chat_session("job", "alice") is None, (
                "a refused preparation must leave no session row"
            )
            assert created == [], "a provider session was created for a refused Start"
            assert fault.attempts == 1, "a genuine fault must not be retried"
            assert any(gone for gone, _ in pushes), (
                "the ending's push never arrived with the phase cleared"
            )
            assert (
                "Chat preparation failed for job job, user alice: "
                "OperationalError: no such table: sessions" in caplog.text
            ), "the operator log does not name the fault and its job"

            history = await main.get_chat_history("job", request, limit=200, offset=0)
            assert history["preparing"] is None
            assert [(m["content"], m["status"]) for m in history["messages"]] == [
                (main._CHAT_PREP_FAILED_DETAIL, "failed")
            ]

        # The lock really let go before the retry: no session exists yet, so a
        # turn answers its ordinary 409 rather than 425 or a hang.
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(
                main.send_chat_message(
                    "job",
                    ChatMessageRequest(message="hi"),
                    request,
                    BackgroundTasks(),
                ),
                1,
            )
        assert refused.value.status_code == 409

        assert main._chat_prep_tasks.get("job:alice") is None
        assert main._chat_prep_phases.get("job:alice") is None
        assert main._chat_lock_refs.get("job:alice") is None
        assert main._chat_locks.get("job:alice") is None
        assert main._chat_job_tasks.get("job") is None

        # Start it again: the promise the transcript makes. The tools come back.
        fault.times = 0
        headers.clear()
        retried = await main.init_chat("job", request, body)
        assert retried["ready"] is False
        await _settle("job")
        active = await storage.get_latest_chat_session("job", "alice")
        assert active is not None and active["session_id"] == "sess-1"
        assert len(created) == 1
        assert [t["name"] for t in created[0]["custom_tools"]] and (
            "get_job_result" in [t["name"] for t in created[0]["custom_tools"]]
        ), "the retry started the chat without its internal tools"
        assert len(headers) == 1 and headers[0].startswith("Bearer ")
        contents = [m["content"] for m in await storage.get_chat_messages("job")]
        assert any(c.startswith("👋 Welcome!") for c in contents)
        assert not _error_rows(
            await storage.get_chat_messages("job", username="alice")
        ), "the retried Start must prune the failure row"


@pytest.mark.asyncio
async def test_the_refuse_storage_fault_flag_is_keyword_only():
    """#534: a positional third argument must be a TypeError, never a behaviour change.

    Ten tests monkeypatch this helper with ``(username, is_admin=False)``-shaped
    wrappers, so a positional third parameter would be silently absorbed instead
    of failing loudly.
    """
    with pytest.raises(TypeError):
        await main._create_ai_auth_header("alice", False, True)


@pytest.mark.asyncio
async def test_the_internal_tools_token_is_optional_except_for_storage_faults(
    monkeypatch, tmp_path, caplog
):
    """#534: the default keeps degrading for every other caller.

    Only a non-transient fault with the opt-in flag refuses; a lock is transient
    and must never become a fatal Start, and the sanctioned ``""`` exits (no
    username, a demoted admin) return before any exception exists.
    """
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")

    # The empty username logs nothing at all: it returns before the try.
    with capture_main_logs(caplog):
        caplog.clear()
        assert await main._create_ai_auth_header("", refuse_storage_fault=True) == ""
        assert caplog.records == []

    async def fail(exc: BaseException):
        async def create_session(*args, **kwargs):
            raise exc

        monkeypatch.setattr(storage, "create_session", create_session)

    lock = sqlite3.OperationalError("database is locked")
    busy = sqlite3.OperationalError("database is busy")
    fault = sqlite3.OperationalError("no such table: sessions")

    for exc in (lock, busy):
        # A lock is transient: it degrades with the flag, and warns either way.
        for flag in (False, True):
            await fail(exc)
            with capture_main_logs(caplog):
                caplog.clear()
                assert (
                    await main._create_ai_auth_header(
                        "alice", refuse_storage_fault=flag
                    )
                    == ""
                )
                assert len(caplog.records) == 1, f"{exc} raised or logged nothing"

    for flag, raises in ((False, False), (True, True)):
        await fail(fault)
        with capture_main_logs(caplog):
            caplog.clear()
            if raises:
                with pytest.raises(sqlite3.OperationalError):
                    await main._create_ai_auth_header(
                        "alice", refuse_storage_fault=True
                    )
            else:
                assert await main._create_ai_auth_header("alice") == ""
            record = caplog.records[-1]
            assert "Failed to create AI session" in record.getMessage()
            assert "for history access" not in record.getMessage(), (
                "the warning still names the wrong operation"
            )

    # ``OSError(EBUSY)`` says "busy" and is not a lock: the type gate refuses it.
    await fail(OSError(errno.EBUSY, "Device or resource busy"))
    with capture_main_logs(caplog), pytest.raises(OSError):
        await main._create_ai_auth_header("alice", refuse_storage_fault=True)

    # A demoted admin gets the sanctioned ``""`` before any exception exists, so
    # the flag is a no-op on that path (the silent admin chat is #540, not this).
    monkeypatch.setattr(main, "_ai_admin_role_current", AsyncMock(return_value=False))
    await fail(fault)
    assert (
        await main._create_ai_auth_header(
            "alice", is_admin=True, refuse_storage_fault=True
        )
        == ""
    )


def test_the_helper_classifies_through_the_shared_predicate():
    """#534: the helper must call ``storage.is_lock_failure``, not a local test.

    A second, inline spelling would pass every behavioural case here for a
    ``locked`` message and still refuse a ``busy`` one.
    """
    source = inspect.getsource(main._create_ai_auth_header)
    assert "storage.is_lock_failure" in source


def test_is_lock_failure_matches_both_spellings():
    """#534: the predicate is what the retry and the refusal both rest on."""
    assert storage.is_lock_failure(sqlite3.OperationalError("database is locked"))
    assert storage.is_lock_failure(sqlite3.OperationalError("database is busy"))
    assert not storage.is_lock_failure(sqlite3.OperationalError("no such table: x"))
    assert not storage.is_lock_failure(OSError(errno.EBUSY, "Device or resource busy"))
    # The e2e row's injected fault: ``RAISE(ABORT)`` is an IntegrityError.
    assert not storage.is_lock_failure(sqlite3.IntegrityError("e2e injected fault"))
    assert "is_lock_failure" in inspect.getsource(storage._retry_contended)


def test_only_the_job_chat_preparation_refuses_a_storage_fault():
    """#534: exactly one call site opts in, so an analysis never hard-fails."""
    assert inspect.getsource(main).count("refuse_storage_fault=True") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
async def test_an_exhausted_lock_still_starts_the_chat_without_internal_tools(
    monkeypatch, tmp_path, caplog, install_start_rig, message
):
    """The surviving ``else`` branch: a lock that outlasts the retry degrades.

    Injected at the connection, not by patching ``create_session`` — the retry
    lives inside it, so a stub would bypass the very budget this asserts. Making
    a lock fatal would turn a busy moment into a failed Start.
    """
    created: list[dict] = []
    headers, capture_header = _capture_header()
    await install_start_rig(
        credential_source="server",
        client=_recording_sidecar(created),
        job_id="job",
        admin=False,
        header=capture_header,
    )
    lock = _FragmentFault(
        "insert into sessions", sqlite3.OperationalError(message), times=None
    )
    with _wrapped_connections(monkeypatch, lock), capture_main_logs(caplog):
        caplog.clear()
        started = await main.init_chat(
            "job",
            _start_request(admin=False),
            ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
        )
        assert started["ready"] is False
        await _settle("job")
        assert "internal tools token unavailable for alice" in caplog.text, (
            "the degradation is silent"
        )

    assert headers == [""], "the lock must degrade, never refuse"
    assert lock.attempts == storage._LOCK_RETRY_ATTEMPTS, (
        "the retry budget must run out"
    )
    assert not _error_rows(await storage.get_chat_messages("job", username="alice"))
    active = await storage.get_latest_chat_session("job", "alice")
    assert active is not None, "an exhausted lock must still start the chat"
    assert "custom_tools" not in created[0], (
        "the degraded session must not claim internal tools"
    )


@pytest.mark.asyncio
async def test_a_restart_with_an_existing_session_ignores_the_internal_tools_fault(
    monkeypatch, tmp_path, install_start_rig
):
    """The mint stays inside the "no usable session" guard (#534).

    Hoisting it one line above that guard — the cheap version of "mint earlier" —
    would make a storage fault kill a *healthy* re-Start, which is strictly worse
    than the bug being fixed.
    """
    created: list[dict] = []
    headers, capture_header = _capture_header()
    await install_start_rig(
        credential_source="server",
        client=_recording_sidecar(created),
        job_id="job",
        admin=False,
        header=capture_header,
    )
    request = _start_request(admin=False)
    body = ChatInitRequest(ai_provider="openai", ai_model="undeclared")
    await main.init_chat("job", request, body)
    await _settle("job")
    assert len(created) == 1
    headers.clear()

    fault = _FragmentFault(
        "insert into sessions",
        sqlite3.OperationalError("no such table: sessions"),
        times=None,
    )
    with _wrapped_connections(monkeypatch, fault):
        restarted = await main.init_chat("job", request, body)
        assert restarted["ready"] is False, "the ordinary 202 payload"
        await _settle("job")

    active = await storage.get_latest_chat_session("job", "alice")
    assert active is not None and active["session_id"] == "sess-1", (
        "a healthy re-Start was killed by the fault"
    )
    assert fault.attempts == 0, "the mint ran for a session that already existed"
    assert headers == [], "a re-Start must not mint a second internal tools token"
    assert not _error_rows(await storage.get_chat_messages("job", username="alice"))


@pytest.mark.asyncio
async def test_admin_chat_init_still_starts_when_the_internal_tools_token_hits_a_storage_fault(
    monkeypatch, tmp_path, caplog, install_start_rig
):
    """#534 excludes the admin chat on purpose: it keeps degrading, pinned here.

    ``header="real"`` is the point — the rig's default stubs the mint to ``""``,
    so a test keeping it could not see a stray ``refuse_storage_fault=True``.
    The tool-less ``ready`` answer asserted below is the documented limitation
    (``docs/use-server-chat-for-cross-job-analysis.md``), not an endorsement: the
    issue names admin-role sessions as must-keep-degrading, and the fix that needs
    its owners' agreement is tracked as #540.
    """
    created: list[dict] = []
    await install_start_rig(
        credential_source="server",
        client=_recording_sidecar(created),
        job_id=main.ADMIN_CHAT_JOB_ID,
        admin=True,
        header="real",
    )
    fault = _FragmentFault(
        "insert into sessions",
        sqlite3.OperationalError("no such table: sessions"),
        times=1,
    )
    with _wrapped_connections(monkeypatch, fault), capture_main_logs(caplog):
        caplog.clear()
        answer = await main.init_admin_chat(
            _start_request(admin=True),
            ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
        )
    assert answer["ready"] is True, "the admin chat must keep degrading"
    assert fault.attempts == 1, "the injected fault never reached the mint"
    lines = [r.getMessage() for r in caplog.records]
    assert "Failed to create AI session" in lines
    assert "for history access" not in " ".join(lines)
    tool_names = [t["name"] for t in created[0].get("custom_tools") or []]
    assert not {"db_schema", "db_query", "save_report"} & set(tool_names)


@pytest.mark.asyncio
async def test_a_failed_preparation_still_pushes_when_the_failed_row_cannot_be_written(
    monkeypatch, tmp_path, caplog, install_start_rig
):
    """#534 routes faults into this handler, so the report must not fail.

    A store that refuses the *failed row* too must still tell the browser: a bare
    ``event.is_set()`` is unfalsifiable (every phase update notifies), so this
    records whether the phase key was already gone at each push.
    """

    async def refuse(**_kwargs):
        raise sqlite3.OperationalError("no such table: chat_messages")

    monkeypatch.setattr(storage, "add_chat_message", refuse)
    created: list[dict] = []
    _headers, capture_header = _capture_header()
    await install_start_rig(
        credential_source="server",
        client=_recording_sidecar(created),
        job_id="job",
        admin=False,
        header=capture_header,
    )
    fault = _FragmentFault(
        "insert into sessions",
        sqlite3.OperationalError("no such table: sessions"),
        times=1,
    )
    with _wrapped_connections(monkeypatch, fault):
        with capture_main_logs(caplog), record_chat_pushes(monkeypatch) as pushes:
            caplog.clear()
            await main.init_chat(
                "job",
                _start_request(admin=False),
                ChatInitRequest(ai_provider="openai", ai_model="undeclared"),
            )
            await _settle("job")
        assert any(gone for gone, _ in pushes), (
            "the ending's push never arrived with the phase cleared"
        )
        assert any(
            "Chat preparation could not record its failed row for job job, user alice"
            in record.getMessage()
            for record in caplog.records
        ), "the lost failed row was not logged"

    assert main._chat_prep_tasks.get("job:alice") is None
    assert main._chat_prep_phases.get("job:alice") is None
    assert main._chat_lock_refs.get("job:alice") is None
    assert main._chat_locks.get("job:alice") is None
    assert main._chat_job_tasks.get("job") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_a_chat_turn_is_unaffected_by_a_failed_internal_tools_mint(
    monkeypatch, tmp_path, install_turn_rig, admin
):
    """Both turn sites keep the default flag: a turn must still answer.

    The order matters — the session exists first, then the fault is installed, then
    the turn runs. Under the fault a Start would be refused by this very change,
    so a turn driven without a session would assert the wrong thing.
    """
    from rootcoz.engine import chat

    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / f"{job_id}-turn.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    if not admin:
        await storage.save_result(
            job_id, "", "completed", {"status": "completed", "result": {}}
        )
    await storage.save_ai_session_source("sid", "alice", "openai", "server")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="m",
        session_id="sid",
    )
    headers, capture_header = _capture_header()
    await install_turn_rig(
        admin=admin, header=capture_header, credential_source="server"
    )
    _user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="openai", ai_model="m"
    )

    async def answer(**_kwargs):
        return True, "the answer", "sid"

    monkeypatch.setattr(chat, "admin_chat_with_ai" if admin else "chat_with_ai", answer)
    fault = _FragmentFault(
        "insert into sessions",
        sqlite3.OperationalError("no such table: sessions"),
        times=None,
    )
    with _wrapped_connections(monkeypatch, fault):
        if admin:
            await main._process_admin_chat_message(
                user_msg_id=_user_id,
                assistant_msg_id=assistant_id,
                message="hello",
                ai_provider_override="openai",
                ai_model_override="m",
                username="alice",
                credential_source="server",
            )
        else:
            await main._process_chat_message(
                job_id=job_id,
                user_msg_id=_user_id,
                assistant_msg_id=assistant_id,
                message="hello",
                ai_provider_override="openai",
                ai_model_override="m",
                username="alice",
                credential_source="server",
            )

    assert headers == [""], "the turn site must keep the graceful default"
    assert fault.attempts == 1, "the injected fault never reached the mint"
    answer_row = next(
        m
        for m in await storage.get_chat_messages(job_id, username="alice")
        if m["id"] == assistant_id
    )
    assert answer_row["content"] == "the answer", "the turn did not complete"
    assert answer_row["status"] == "completed"
