"""Explicit chat Start selects a user-owned credential source, not job defaults."""

import asyncio
import contextlib
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from rootcoz import ai_client, main, storage
from rootcoz.models import ChatInitRequest, ChatMessageRequest


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
    with pytest.raises(ai_client.SessionCreationRefused) as exc:
        await ai_client.create_session_safely(
            client,
            provider="openai",
            model="undeclared",
            system_prompt="hi",
            server_api_key=refusal_secret_key,
        )
    sentence = str(exc.value)
    assert sentence.startswith(f"{ai_client.SESSION_REFUSAL_MARKER}: ")
    assert "could not be created for openai/undeclared" in sentence
    assert "upstream said no" not in sentence
    assert exc.value.__context__ is None
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


async def _contended_once(monkeypatch: pytest.MonkeyPatch, counter: dict) -> None:
    """Make the next chat-row write lose the lock once, then work normally."""
    real_connect = storage._connect_db

    @contextlib.asynccontextmanager
    async def flaky_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        counter["connect"] = counter.get("connect", 0) + 1
        if counter["connect"] == 1:
            counter["contended"] = counter.get("contended", 0) + 1
            raise sqlite3.OperationalError("database is locked")
        async with real_connect(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(storage, "_connect_db", flaky_connect)
    monkeypatch.setattr(storage, "_LOCK_RETRY_BACKOFF_SECONDS", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["message", "source"])
async def test_a_session_row_survives_one_contended_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write: str
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
    await _contended_once(monkeypatch, counter)

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

    def __init__(self) -> None:
        self.awaiting_retry: set[str] = set()
        self.contended = 0

    def wrap(self, db):
        return _LockedConnection(db, self)


class _LockedConnection:
    def __init__(self, db, held: _HeldLock) -> None:
        self._db = db
        self._held = held
        #: This connection is a retry, so every write on it may commit.
        self._retried = False

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def execute(self, sql, *args, **kwargs):
        self._contend(sql)
        return await self._db.execute(sql, *args, **kwargs)

    async def executemany(self, sql, *args, **kwargs):
        self._contend(sql)
        return await self._db.executemany(sql, *args, **kwargs)

    def _contend(self, sql: str) -> None:
        if self._retried or not _is_lock_statement(sql):
            return
        if sql in self._held.awaiting_retry:
            self._held.awaiting_retry.discard(sql)
            self._retried = True
            return
        self._held.awaiting_retry.add(sql)
        self._held.contended += 1
        raise sqlite3.OperationalError("database is locked")


@pytest.mark.asyncio
async def test_the_whole_preparation_survives_a_lock_on_every_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """R5, closed as a class: prepare a chat while every write loses the lock once.

    Two rounds pinned one write each and the run then failed on the next write
    along — ``create_ai_session_with_source``, which the preparation reaches
    before either of them. So this drives the real preparation end to end
    (``/init`` preflight, session create with its provenance row, session marker,
    welcome prune, welcome row) under one held lock and asserts it ends ready with
    its rows present. It fails if *any* write on that path stops being retried.
    """
    from rootcoz.engine import chat, graft_http
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_result(
        "job", "", "completed", {"status": "completed", "result": {}}
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: tmp_path)
    monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(graft_http, "cloned_graph_roots", lambda workspace: {})
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main,
        "_resolve_chat_credentials",
        AsyncMock(return_value=("", "", None, "", "")),
    )
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(
        chat, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(ai_client, "server_provider_key", AsyncMock(return_value=None))

    async def create_session(**kwargs):
        return "sess-1"

    monkeypatch.setattr(
        chat,
        "get_sidecar_client",
        lambda: SimpleNamespace(
            create_session=create_session, delete_session=AsyncMock()
        ),
    )

    async def validate(body, username):
        ai_client._selected_credential_source.set("server")
        return body.ai_provider, body.ai_model

    monkeypatch.setattr(main, "_validate_chat_selection", validate)
    # The internal tools token is a bare INSERT, and the preparation swallows its
    # failure: a lock there costs the chat its tools without failing the Start, so
    # record what it actually returned instead of only asserting readiness.
    headers: list[str] = []
    real_header = main._create_ai_auth_header

    async def capture_header(username: str, is_admin: bool = False) -> str:
        value = await real_header(username, is_admin)
        headers.append(value)
        return value

    monkeypatch.setattr(main, "_create_ai_auth_header", capture_header)

    held = _HeldLock()
    real_connect = storage._connect_db

    @contextlib.asynccontextmanager
    async def locked_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        async with real_connect(*args, **kwargs) as db:
            yield held.wrap(db)

    monkeypatch.setattr(storage, "_connect_db", locked_connect)
    monkeypatch.setattr(storage, "_LOCK_RETRY_BACKOFF_SECONDS", 0)

    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=False, role="reviewer")
    )
    started = await main.init_chat(
        "job", request, ChatInitRequest(ai_provider="openai", ai_model="m")
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
