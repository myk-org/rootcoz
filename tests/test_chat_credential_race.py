"""In-flight chat results cannot restore a session after credential rotation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import BackgroundTasks

from rootcoz import main, storage
from rootcoz.models import ChatInitRequest, ChatMessageRequest


@pytest.mark.asyncio
async def test_queued_admin_chat_demotion_cannot_mint_admin_token_or_call_ai(
    tmp_path, monkeypatch
):
    from rootcoz.ai_client import _selected_credential_source
    from rootcoz.engine import chat

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "admin-role.db")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "admin-role.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("sid", "alice", "p", "server")
    await storage.add_chat_message(
        main.ADMIN_CHAT_JOB_ID,
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
    )

    async def validate(provider, model, **kwargs):
        _selected_credential_source.set("server")
        return provider, model

    monkeypatch.setattr(main, "_normalize_and_validate_ai_params", validate)
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **kw: tmp_path)
    tools = []
    monkeypatch.setattr(chat, "build_admin_custom_tools", lambda **kw: tools.append(kw))
    minted = []
    create_session = storage.create_session

    async def track_session(*args, **kwargs):
        token = await create_session(*args, **kwargs)
        minted.append(token)
        return token

    monkeypatch.setattr(storage, "create_session", track_session)
    ai = AsyncMock(return_value=(True, "reply", "sid"))
    monkeypatch.setattr(chat, "admin_chat_with_ai", ai)
    request = SimpleNamespace(state=SimpleNamespace(username="alice", is_admin=True))
    tasks = BackgroundTasks()
    queued = await main.send_admin_chat_message(
        ChatMessageRequest(message="hello"), request, tasks
    )
    await storage.change_user_role("alice", "reviewer")
    await tasks()

    msg = (await storage.get_chat_messages(main.ADMIN_CHAT_JOB_ID, username="alice"))[
        -1
    ]
    assert msg["id"] == queued["assistant_message_id"]
    # If the worker minted a token, it must not grant the demoted user schema access.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        for token in minted:
            response = await client.get(
                "/api/admin/db/schema", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 403
    assert not minted
    assert msg["status"] == "failed" and msg["content"]
    assert msg["session_id"] == ""
    ai.assert_not_awaited()
    assert not tools
    assert await main._create_ai_auth_header("alice", is_admin=True) == ""


@pytest.mark.asyncio
async def test_admin_chat_demotion_during_ai_discards_reply(tmp_path, monkeypatch):
    from rootcoz.engine import chat

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "admin-call.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("sid", "alice", "p", "server")
    await storage.add_chat_message(
        main.ADMIN_CHAT_JOB_ID,
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(chat, "build_admin_custom_tools", lambda **kw: [])
    reached = asyncio.Event()
    resume = asyncio.Event()

    async def delayed_ai(**kwargs):
        reached.set()
        await resume.wait()
        return True, "privileged reply", "new-session"

    monkeypatch.setattr(chat, "admin_chat_with_ai", delayed_ai)
    user_id, pending = await storage.add_chat_message_pair(
        main.ADMIN_CHAT_JOB_ID, "hi", username="alice", ai_provider="p", ai_model="m"
    )
    task = asyncio.create_task(
        main._process_admin_chat_message(
            user_msg_id=user_id,
            assistant_msg_id=pending,
            message="hi",
            ai_provider_override="p",
            ai_model_override="m",
            username="alice",
            force_server=True,
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), 10)
        await storage.change_user_role("alice", "reviewer")
    finally:
        resume.set()
        await task
    message = (
        await storage.get_chat_messages(main.ADMIN_CHAT_JOB_ID, username="alice")
    )[-1]
    assert message["status"] == "failed"
    assert "privileged reply" not in message["content"]
    assert message["session_id"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("force_server", [False, True])
async def test_admin_chat_demotion_after_role_check_cannot_commit_reply(
    tmp_path, monkeypatch, force_server
):
    from rootcoz.engine import chat

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "commit-role.db")
    await storage.init_db()
    await storage.create_admin_user("alice", can_use_server_providers=True)
    await storage.save_ai_session_source(
        "sid", "alice", "p", "server" if force_server else "user"
    )
    await storage.add_chat_message(
        main.ADMIN_CHAT_JOB_ID,
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(chat, "build_admin_custom_tools", lambda **kw: [])
    monkeypatch.setattr(
        chat,
        "admin_chat_with_ai",
        AsyncMock(return_value=(True, "secret reply", "new-sid")),
    )
    _, pending = await storage.add_chat_message_pair(
        main.ADMIN_CHAT_JOB_ID, "hi", username="alice", ai_provider="p", ai_model="m"
    )
    reached = asyncio.Event()
    resume = asyncio.Event()
    complete = storage.complete_chat_message_if_generation

    async def delayed_complete(*args, **kwargs):
        reached.set()
        await resume.wait()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(
        storage, "complete_chat_message_if_generation", delayed_complete
    )
    task = asyncio.create_task(
        main._process_admin_chat_message(
            user_msg_id=0,
            assistant_msg_id=pending,
            message="hi",
            ai_provider_override="p",
            ai_model_override="m",
            username="alice",
            force_server=force_server,
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), 10)
        await storage.change_user_role("alice", "reviewer")
    finally:
        resume.set()
        await task
    message = (
        await storage.get_chat_messages(main.ADMIN_CHAT_JOB_ID, username="alice")
    )[-1]
    assert message["status"] == "failed"
    assert "secret reply" not in message["content"]
    assert message["session_id"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_id,username,server_source,allowed",
    [
        ("job", "alice", False, True),
        (main.ADMIN_CHAT_JOB_ID, "alice", False, False),
        (main.ADMIN_CHAT_JOB_ID, "alice", True, False),
        (main.ADMIN_CHAT_JOB_ID, "admin", False, True),
    ],
)
async def test_completion_role_gate_only_affects_managed_admin_chat(
    tmp_path, monkeypatch, job_id, username, server_source, allowed
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "roles.db")
    await storage.init_db()
    await storage.create_admin_user("alice", can_use_server_providers=True)
    _, pending = await storage.add_chat_message_pair(job_id, "hi", username=username)
    if username == "alice":
        await storage.change_user_role("alice", "reviewer")
    saved = await storage.complete_chat_message_if_generation(
        pending,
        content="reply",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
        credential_generation=None,
        server_source=server_source,
    )
    message = (await storage.get_chat_messages(job_id, username=username))[-1]
    assert saved is allowed
    assert message["status"] == ("completed" if allowed else "pending")
    assert message["content"] == ("reply" if allowed else "")


@pytest.mark.asyncio
async def test_admin_db_query_demotion_while_reading_body_rejects_sql(
    tmp_path, monkeypatch
):
    from fastapi import HTTPException

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "query-role.db")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "query-role.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    reading = asyncio.Event()
    resume = asyncio.Event()

    async def delayed_json():
        reading.set()
        await resume.wait()
        return {"sql": "SELECT 1 AS result"}

    request = SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=True), json=delayed_json
    )
    task = asyncio.create_task(main.admin_db_query(request))
    try:
        await asyncio.wait_for(reading.wait(), 10)
        await storage.change_user_role("alice", "reviewer")
    finally:
        resume.set()
    with pytest.raises(HTTPException) as exc:
        await task
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_db_query_still_allows_bootstrap_and_live_admin(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "query-allowed.db")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "query-allowed.db")
    await storage.init_db()
    await storage.create_admin_user("alice")

    async def body():
        return {"sql": "SELECT 1 AS result"}

    for username in ("alice", "admin"):
        request = SimpleNamespace(
            state=SimpleNamespace(username=username, is_admin=True), json=body
        )
        assert (await main.admin_db_query(request))["rows"] == [{"result": 1}]


@pytest.mark.asyncio
async def test_admin_chat_demotion_while_preparing_tools_stops_ai(
    tmp_path, monkeypatch
):
    from rootcoz.engine import chat

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "admin-tools.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("sid", "alice", "p", "server")
    await storage.add_chat_message(
        main.ADMIN_CHAT_JOB_ID,
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **kw: tmp_path)
    tools = []

    def build_tools(**kw):
        tools.append(kw)
        return []

    monkeypatch.setattr(chat, "build_admin_custom_tools", build_tools)
    ai = AsyncMock()
    monkeypatch.setattr(chat, "admin_chat_with_ai", ai)
    created = asyncio.Event()
    resume = asyncio.Event()
    create = main._create_ai_auth_header

    async def paused_header(*args, **kwargs):
        header = await create(*args, **kwargs)
        created.set()
        await resume.wait()
        return header

    monkeypatch.setattr(main, "_create_ai_auth_header", paused_header)
    user_id, pending = await storage.add_chat_message_pair(
        main.ADMIN_CHAT_JOB_ID, "hi", username="alice", ai_provider="p", ai_model="m"
    )
    task = asyncio.create_task(
        main._process_admin_chat_message(
            user_msg_id=user_id,
            assistant_msg_id=pending,
            message="hi",
            ai_provider_override="p",
            ai_model_override="m",
            username="alice",
            force_server=True,
        )
    )
    try:
        await asyncio.wait_for(created.wait(), 10)
        await storage.change_user_role("alice", "reviewer")
    finally:
        resume.set()
        await task
    message = (
        await storage.get_chat_messages(main.ADMIN_CHAT_JOB_ID, username="alice")
    )[-1]
    assert message["status"] == "failed"
    assert message["session_id"] == ""
    ai.assert_not_awaited()
    assert not tools


@pytest.mark.asyncio
async def test_admin_token_revoked_if_demoted_while_minting(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "mint.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    minted = []
    create = storage.create_session

    async def demote_before_insert(*args, **kwargs):
        await storage.change_user_role("alice", "reviewer")
        token = await create(*args, **kwargs)
        minted.append(token)
        return token

    monkeypatch.setattr(storage, "create_session", demote_before_insert)
    assert await main._create_ai_auth_header("alice", is_admin=True) == ""
    assert len(minted) == 1
    assert await storage.get_session(minted[0]) is None


@pytest.mark.asyncio
async def test_current_admin_chat_auth_token_can_read_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "admin-role.db")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "admin-role.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    header = await main._create_ai_auth_header("alice", is_admin=True)
    assert header.startswith("Bearer ")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/admin/db/schema", headers={"Authorization": header}
        )
    assert response.status_code == 200


async def _start_user_chat(job_id: str) -> None:
    from rootcoz.ai_client import _selected_credential_source

    _selected_credential_source.set("user")
    await storage.save_ai_session_source("old-session", "alice", "p", "user")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="old-session",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["job", "__admin_chat__"])
@pytest.mark.parametrize("replacement", ["new-key", None])
async def test_in_flight_chat_cannot_restore_revoked_session(
    tmp_path, monkeypatch, job_id, replacement
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    generation = await storage.get_user_ai_credential_generation("alice", "p")
    await storage.save_ai_session_source("old-session", "alice", "p", "user")
    await storage.add_chat_message(
        job_id=job_id,
        role="assistant",
        content="old",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="old-session",
    )
    _, pending = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="p", ai_model="m"
    )
    assert await storage.update_user_ai_credential("alice", "p", replacement) == [
        "old-session"
    ]
    await storage.update_chat_message_status(pending, "completed")
    assert not await storage.update_chat_message_ai_fields(
        pending,
        ai_provider="p",
        ai_model="m",
        session_id="old-session",
        credential_generation=generation,
    )
    assert all(
        not msg["session_id"]
        for msg in await storage.get_chat_messages(job_id, username="alice")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_case", ["current", "rotated", "none"])
async def test_complete_chat_message_is_atomic_and_generation_gated(
    tmp_path, monkeypatch, generation_case
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "complete.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    _, pending = await storage.add_chat_message_pair("job", "hello", username="alice")
    generation = await storage.get_user_ai_credential_generation("alice", "p")
    if generation_case == "rotated":
        await storage.update_user_ai_credential("alice", "p", "new-key")
    if generation_case == "none":
        generation = None

    saved = await storage.complete_chat_message_if_generation(
        pending,
        content="reply",
        ai_provider="p",
        ai_model="m",
        session_id="session",
        credential_generation=generation,
    )
    message = (await storage.get_chat_messages("job", username="alice"))[-1]
    assert saved is (generation_case != "rotated")
    assert (
        message["content"],
        message["status"],
        message["ai_provider"],
        message["ai_model"],
        message["session_id"],
    ) == (
        ("reply", "completed", "p", "m", "session")
        if saved
        else ("", "pending", "", "", "")
    )
    assert not await storage.complete_chat_message_if_generation(
        -1,
        content="reply",
        ai_provider="p",
        ai_model="m",
        session_id="session",
        credential_generation=generation,
    )


@pytest.mark.asyncio
async def test_rotating_user_key_only_invalidates_user_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "rotation.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    for job, sid, source in (
        ("job-user", "user-sid", "user"),
        ("job-server", "server-sid", "server"),
        ("job-legacy", "legacy-sid", None),
        ("job-other", "other-sid", None),
    ):
        if source:
            await storage.save_ai_session_source(sid, "alice", "p", source)
        await storage.add_chat_message(
            job,
            "assistant",
            "",
            username="alice",
            ai_provider="other" if job == "job-other" else "p",
            ai_model="m",
            session_id=sid,
        )
    assert set(await storage.update_user_ai_credential("alice", "p", "new-key")) == {
        "user-sid",
        "legacy-sid",
    }
    assert await storage.get_latest_chat_session("job-user", "alice") is None
    assert await storage.get_latest_chat_session("job-legacy", "alice") is None
    assert (await storage.get_latest_chat_session("job-server", "alice"))[
        "session_id"
    ] == "server-sid"
    assert (await storage.get_latest_chat_session("job-other", "alice"))[
        "session_id"
    ] == "other-sid"
    assert await storage.get_ai_session_source("server-sid", "alice", "p") == "server"
    for sid in ("user-sid", "legacy-sid"):
        with pytest.raises(ValueError, match="revoked"):
            await storage.get_ai_session_source(sid, "alice", "p")
    delete = AsyncMock()
    assert await storage.delete_revoked_ai_session("legacy-sid", delete)
    delete.assert_awaited_once_with("legacy-sid")


@pytest.mark.asyncio
async def test_rotated_latest_start_blocks_older_server_but_not_other_chats(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "latest.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    for job, sid, source in (
        ("same-job", "earlier-server", "server"),
        ("other-job", "other-server", "server"),
        ("same-job", "newer-user", "user"),
    ):
        await storage.save_ai_session_source(sid, "alice", "p", source)
        await storage.add_chat_message(
            job,
            "assistant",
            "",
            username="alice",
            ai_provider="p",
            ai_model="m",
            session_id=sid,
        )
    await storage.update_user_ai_credential("alice", "p", "new-key")
    assert await storage.get_latest_chat_session("same-job", "alice") is None
    assert (await storage.get_latest_chat_session("other-job", "alice"))[
        "session_id"
    ] == "other-server"
    assert (
        await storage.get_ai_session_source("earlier-server", "alice", "p") == "server"
    )
    await storage.save_ai_session_source("fresh-server", "alice", "p", "server")
    await storage.add_chat_message(
        "same-job",
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="fresh-server",
    )
    assert (await storage.get_latest_chat_session("same-job", "alice"))[
        "session_id"
    ] == "fresh-server"


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_chat_call_rotated_mid_flight_cannot_publish_session(
    tmp_path, monkeypatch, admin
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id,
            "",
            "completed",
            {"status": "completed", "summary": "test", "failures": []},
        )
        monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
        monkeypatch.setattr(
            chat, "install_http_tools_mcp_best_effort_async", AsyncMock()
        )
        monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(
            main,
            "_resolve_chat_credentials",
            AsyncMock(return_value=("", "", "", "", "")),
        )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def rotate_during_call(**kwargs):
        await storage.update_user_ai_credential("alice", "p", None)
        return True, "reply", "stale-session"

    monkeypatch.setattr(
        chat, "admin_chat_with_ai" if admin else "chat_with_ai", rotate_during_call
    )
    await _start_user_chat(job_id)
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="p", ai_model="m"
    )
    revoked = AsyncMock()
    monkeypatch.setattr(main, "_discard_unsaved_ai_session", revoked)
    process = main._process_admin_chat_message if admin else main._process_chat_message
    await process(
        **({} if admin else {"job_id": job_id}),
        user_msg_id=user_id,
        assistant_msg_id=assistant_id,
        message="hello",
        ai_provider_override="p",
        ai_model_override="m",
        username="alice",
    )
    message = (await storage.get_chat_messages(job_id, username="alice"))[-1]
    assert message["status"] == "failed"
    assert message["content"] != "reply"
    assert message["session_id"] == ""
    revoked.assert_awaited_once_with("stale-session", "alice", "p")


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_revoked_server_grant_mid_call_does_not_publish_reply(
    tmp_path, monkeypatch, admin
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "grant.db")
    await storage.init_db()
    username = "admin" if admin else "alice"
    if not admin:
        await storage.create_user(
            username, role="reviewer", can_use_server_providers=True
        )
        await storage.save_result(
            "job", "", "completed", {"status": "completed", "result": {}}
        )
        monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
        monkeypatch.setattr(
            chat, "install_http_tools_mcp_best_effort_async", AsyncMock()
        )
        monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(
            main,
            "_resolve_chat_credentials",
            AsyncMock(return_value=("", "", "", "", "")),
        )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    await storage.save_ai_session_source("server-sid", username, "p", "server")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username=username,
        ai_provider="p",
        ai_model="m",
        session_id="server-sid",
    )
    _, pending = await storage.add_chat_message_pair(
        job_id, "hello", username=username, ai_provider="p", ai_model="m"
    )

    async def revoke(**kwargs):
        if not admin:
            await storage.set_user_can_use_server_providers(username, False)
        return True, "SECRET REPLY", "server-sid"

    monkeypatch.setattr(chat, "admin_chat_with_ai" if admin else "chat_with_ai", revoke)
    process = main._process_admin_chat_message if admin else main._process_chat_message
    await process(
        **({} if admin else {"job_id": job_id}),
        user_msg_id=1,
        assistant_msg_id=pending,
        message="hello",
        ai_provider_override="p",
        ai_model_override="m",
        username=username,
        force_server=True,
    )
    msg = (await storage.get_chat_messages(job_id, username=username))[-1]
    if admin:
        assert msg["content"] == "SECRET REPLY" and msg["status"] == "completed"
    else:
        assert msg["status"] == "failed"
        assert "SECRET REPLY" not in msg["content"]


@pytest.mark.asyncio
async def test_server_reply_survives_unrelated_user_key_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "server-rotation.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    await storage.update_user_ai_credential("alice", "p", "old-key")
    generation = await storage.get_user_ai_credential_generation("alice", "p")
    _, pending = await storage.add_chat_message_pair("job", "hi", username="alice")
    await storage.update_user_ai_credential("alice", "p", "new-key")
    assert await storage.complete_chat_message_if_generation(
        pending,
        content="reply",
        ai_provider="p",
        ai_model="m",
        session_id="server-sid",
        credential_generation=generation,
        server_source=True,
    )
    assert (await storage.get_chat_messages("job", username="alice"))[-1][
        "content"
    ] == "reply"


@pytest.mark.asyncio
async def test_credential_rotation_preserves_server_reply_in_flight(
    tmp_path, monkeypatch
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "server-inflight.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    await storage.update_user_ai_credential("alice", "p", "old-key")
    await storage.save_result(
        "job", "", "completed", {"status": "completed", "result": {}}
    )
    await storage.save_ai_session_source("server-sid", "alice", "p", "server")
    await storage.add_chat_message(
        "job",
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="server-sid",
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: tmp_path)
    monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(chat, "install_http_tools_mcp_best_effort_async", AsyncMock())
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main, "_resolve_chat_credentials", AsyncMock(return_value=("", "", "", "", ""))
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def rotate(**kwargs):
        await storage.update_user_ai_credential("alice", "p", "new-key")
        return True, "SERVER REPLY", "server-sid"

    monkeypatch.setattr(chat, "chat_with_ai", rotate)
    user_id, pending = await storage.add_chat_message_pair(
        "job", "hi", username="alice", ai_provider="p", ai_model="m"
    )
    await main._process_chat_message(
        job_id="job",
        user_msg_id=user_id,
        assistant_msg_id=pending,
        message="hi",
        ai_provider_override="p",
        ai_model_override="m",
        username="alice",
        force_server=True,
    )
    msg = (await storage.get_chat_messages("job", username="alice"))[-1]
    assert (msg["status"], msg["content"], msg["session_id"]) == (
        "completed",
        "SERVER REPLY",
        "server-sid",
    )


@pytest.mark.asyncio
async def test_credential_rotation_does_not_revoke_foreign_session_record(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "foreign.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.create_user("bob", role="reviewer")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    await storage.save_ai_session_source("foreign-sid", "bob", "p", "server")
    await storage.add_chat_message(
        "job",
        "assistant",
        "",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="foreign-sid",
    )
    assert await storage.update_user_ai_credential("alice", "p", "new-key") == []
    assert await storage.get_ai_session_source("foreign-sid", "bob", "p") == "server"


@pytest.mark.asyncio
async def test_server_grant_revoked_while_completion_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "grant-commit.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    _, pending = await storage.add_chat_message_pair("job", "hi", username="alice")
    await storage.set_user_can_use_server_providers("alice", False)
    assert not await storage.complete_chat_message_if_generation(
        pending,
        content="SECRET REPLY",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
        credential_generation=None,
        server_source=True,
    )
    assert (
        "SECRET REPLY"
        not in (await storage.get_chat_messages("job", username="alice"))[-1]["content"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("change", ["other", "same", "absent"])
async def test_unrelated_or_noop_change_does_not_abort_chat(
    tmp_path, monkeypatch, admin, change
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "unrelated.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id,
            "",
            "completed",
            {"status": "completed", "summary": "test", "failures": []},
        )
        monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
        monkeypatch.setattr(
            chat, "install_http_tools_mcp_best_effort_async", AsyncMock()
        )
        monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(
            main,
            "_resolve_chat_credentials",
            AsyncMock(return_value=("", "", "", "", "")),
        )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def change_during_call(**kwargs):
        if change == "other":
            await storage.update_user_ai_credential("alice", "other", "other-key")
        elif change == "same":
            await storage.update_user_ai_credential("alice", "p", "old-key")
        else:
            await storage.update_user_ai_credential("alice", "other", None)
        return True, "reply", "new-session"

    monkeypatch.setattr(
        chat, "admin_chat_with_ai" if admin else "chat_with_ai", change_during_call
    )
    await _start_user_chat(job_id)
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="p", ai_model="m"
    )
    monkeypatch.setattr(main, "_revoke_ai_sessions", revoked := AsyncMock())
    process = main._process_admin_chat_message if admin else main._process_chat_message
    await process(
        **({} if admin else {"job_id": job_id}),
        user_msg_id=user_id,
        assistant_msg_id=assistant_id,
        message="hello",
        ai_provider_override="p",
        ai_model_override="m",
        username="alice",
    )
    message = (await storage.get_chat_messages(job_id, username="alice"))[-1]
    assert (message["status"], message["content"], message["session_id"]) == (
        "completed",
        "reply",
        "new-session",
    )
    revoked.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_id,label", [("job", "Chat"), (main.ADMIN_CHAT_JOB_ID, "Admin chat")]
)
async def test_discard_notifies_only_pending_placeholder(
    tmp_path, monkeypatch, job_id, label
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "discard.db")
    await storage.init_db()
    _, pending = await storage.add_chat_message_pair(job_id, "hello", username="alice")
    monkeypatch.setattr(main, "_discard_unsaved_ai_session", revoked := AsyncMock())
    notifications = []
    monkeypatch.setattr(
        main,
        "notify_chat_changed",
        lambda job_id, *, username: notifications.append((job_id, username)),
    )
    await main._discard_unsaved_chat_response(
        pending, job_id, "alice", "p", "sid", label
    )
    revoked.assert_awaited_once_with("sid", "alice", "p")
    assert notifications == [(job_id, "alice")]
    await main._discard_unsaved_chat_response(
        pending, job_id, "alice", "p", None, label
    )
    assert notifications == [(job_id, "alice")]
    revoked.assert_awaited_once()


async def test_credential_failure_only_changes_pending_message(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "pending-only.db")
    await storage.init_db()
    _, pending = await storage.add_chat_message_pair("job", "hello", username="alice")
    assert await storage.fail_pending_chat_message(pending, "Aborted by user.")
    assert not await storage.fail_pending_chat_message(pending, "Credentials changed")
    message = (await storage.get_chat_messages("job", username="alice"))[-1]
    assert (message["content"], message["status"]) == ("Aborted by user.", "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_abort_between_status_check_and_completion_preserves_reason(
    tmp_path, monkeypatch, admin
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "abort.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    job_id = main.ADMIN_CHAT_JOB_ID if admin else "job"
    if not admin:
        await storage.save_result(
            job_id,
            "",
            "completed",
            {"status": "completed", "summary": "test", "failures": []},
        )
        monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
        monkeypatch.setattr(
            chat, "install_http_tools_mcp_best_effort_async", AsyncMock()
        )
        monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(
            main,
            "_resolve_chat_credentials",
            AsyncMock(return_value=("", "", "", "", "")),
        )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))
    monkeypatch.setattr(
        chat,
        "admin_chat_with_ai" if admin else "chat_with_ai",
        AsyncMock(return_value=(True, "reply", "new-session")),
    )
    await _start_user_chat(job_id)
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice", ai_provider="p", ai_model="m"
    )
    reached = asyncio.Event()
    resume = asyncio.Event()
    complete = storage.complete_chat_message_if_generation

    async def paused_complete(*args, **kwargs):
        reached.set()
        await resume.wait()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(storage, "complete_chat_message_if_generation", paused_complete)
    monkeypatch.setattr(main, "_discard_unsaved_ai_session", revoked := AsyncMock())
    process = main._process_admin_chat_message if admin else main._process_chat_message
    task = asyncio.create_task(
        process(
            **({} if admin else {"job_id": job_id}),
            user_msg_id=user_id,
            assistant_msg_id=assistant_id,
            message="hello",
            ai_provider_override="p",
            ai_model_override="m",
            username="alice",
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), 10)
        # The abort request wins after the handler's status read but before its UPDATE.
        await storage.update_chat_message_content(assistant_id, "Aborted by user.")
        await storage.update_chat_message_status(assistant_id, "failed")
    finally:
        resume.set()
        await task
    message = (await storage.get_chat_messages(job_id, username="alice"))[-1]
    assert (message["status"], message["content"], message["session_id"]) == (
        "failed",
        "Aborted by user.",
        "",
    )
    revoked.assert_awaited_once_with("new-session", "alice", "p")


@pytest.mark.asyncio
async def test_initial_session_rotation_discards_and_revokes(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "init.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    generation = await storage.get_user_ai_credential_generation("alice", "p")
    await storage.update_user_ai_credential("alice", "p", "rotated")
    assert not await storage.add_chat_message(
        job_id="job",
        role="assistant",
        content="",
        username="alice",
        ai_provider="p",
        ai_model="m",
        session_id="stale",
        credential_generation=generation,
    )
    assert await storage.get_chat_messages("job", username="alice") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_initial_chat_handler_revokes_session_rotated_before_insert(
    tmp_path, monkeypatch, admin
):
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "init-handler.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "p", "old-key")
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)

    async def validate(provider, model):
        from rootcoz.ai_client import _selected_credential_source

        _selected_credential_source.set("user")
        return provider, model

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)
    deleted = AsyncMock(side_effect=OSError("sidecar offline"))
    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(delete_session=deleted),
    )

    async def rotate_during_init(**_kwargs):
        await storage.save_ai_session_source("stale-session", "alice", "p", "user")
        await storage.update_user_ai_credential("alice", "p", "new-key")
        return "stale-session"

    if admin:
        monkeypatch.setattr(chat, "init_admin_chat_session", rotate_during_init)
        monkeypatch.setattr(
            main,
            "get_settings",
            lambda: SimpleNamespace(
                ai_provider="p", ai_model="m", force_server_credentials=False
            ),
        )
        result = await main.init_admin_chat(
            SimpleNamespace(state=SimpleNamespace(username="alice", is_admin=True)),
            ChatInitRequest(
                ai_provider="p", ai_model="m", force_server_credentials=False
            ),
        )
        job_id = main.ADMIN_CHAT_JOB_ID
    else:
        job_id = "job"
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "status": "completed",
                "summary": "test",
                "failures": [],
                "ai_provider": "p",
                "ai_model": "m",
            },
        )
        monkeypatch.setattr(chat, "clone_chat_repos", AsyncMock(return_value=False))
        monkeypatch.setattr(
            chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(
            main,
            "_resolve_chat_credentials",
            AsyncMock(return_value=("", "", "", "", "")),
        )
        monkeypatch.setattr(chat, "init_chat_session", rotate_during_init)
        result = await main._init_chat_under_barrier(
            job_id,
            "alice",
            ChatInitRequest(
                ai_provider="p", ai_model="m", force_server_credentials=False
            ),
        )

    assert result["session_id"] == ""
    assert all(
        not msg["session_id"]
        for msg in await storage.get_chat_messages(job_id, username="alice")
    )
    assert await storage.list_revoked_ai_sessions() == ["stale-session"]
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("stale-session", "alice", "p")
    deleted.assert_awaited_once_with("stale-session")


@pytest.mark.asyncio
async def test_managed_admin_server_reply_survives_role_grant(tmp_path, monkeypatch):
    """A managed admin has effective server access through the role, not the stored flag."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "admin-role-grant.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    assert await storage.can_user_use_server_providers("alice")
    _, pending = await storage.add_chat_message_pair("job", "hi", username="alice")
    assert await storage.complete_chat_message_if_generation(
        pending,
        content="SERVER REPLY",
        ai_provider="p",
        ai_model="m",
        session_id="sid",
        credential_generation=None,
        server_source=True,
    )
    assert (await storage.get_chat_messages("job", username="alice"))[-1][
        "content"
    ] == "SERVER REPLY"
