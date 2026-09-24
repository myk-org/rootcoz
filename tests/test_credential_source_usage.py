"""Credential provenance survives session reuse and persisted result snapshots."""

import asyncio
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
from pi_sidecar_client import AIResult

from rootcoz import ai_client, main, storage
from rootcoz.ai_client import call_ai as real_call_ai
from rootcoz.cli.main import _print_job_token_usage
from rootcoz.token_tracking import build_token_usage_summary


@pytest.mark.asyncio
async def test_mixed_calls_resume_and_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "usage.db")
    await storage.init_db()
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "model"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="secret"))
    monkeypatch.setattr(
        ai_client,
        "_call_user_session",
        AsyncMock(return_value=AIResult(True, "ok", session_id="user-sid")),
    )
    monkeypatch.setattr(
        ai_client,
        "_call_ai",
        AsyncMock(return_value=AIResult(True, "ok", session_id="server-sid")),
    )
    ai_client._setup_usage_recorder()
    token = ai_client.ai_username.set("alice")
    try:
        user = await real_call_ai("one", ai_provider="openai", ai_model="model")
        await storage.save_ai_session_source("user-sid", "alice", "openai", "user")
        ai_client._selected_credential_source.set("server")
        server = await real_call_ai("two", ai_provider="openai", ai_model="model")
        # The current key no longer determines a resumed session's source.
        resumed = await real_call_ai(
            "three", ai_provider="openai", ai_model="model", session_id="user-sid"
        )
        assert resumed.credential_source == "user"
        for result in (user, server, resumed):
            await result.record_usage(request_id="job", call_type="analysis")
        with pytest.raises(ValueError, match="another user"):
            await storage.get_ai_session_source("user-sid", "bob", "openai")
        assert (
            await real_call_ai(
                "wrong", ai_provider="openai", ai_model="model", session_id="server-sid"
            )
        ).credential_source == "server"
    finally:
        ai_client.ai_username.reset(token)

    await storage.record_token_usage("job", "openai", "model", "analysis")
    records = await storage.get_token_usage_for_job("job")
    assert [r["credential_source"] for r in records] == [
        "user",
        "server",
        "user",
        "unknown",
    ]
    result_data = {}
    await main._attach_token_usage("job", result_data)
    assert [r["credential_source"] for r in result_data["token_usage"]["calls"]] == [
        "user",
        "server",
        "user",
        "unknown",
    ]


@pytest.mark.asyncio
async def test_legacy_usage_migration_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(storage, "DB_PATH", path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE ai_token_usage (id TEXT PRIMARY KEY, job_id TEXT, "
            "ai_provider TEXT, ai_model TEXT, call_type TEXT, input_tokens INTEGER, "
            "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
            "total_tokens INTEGER, cost_usd REAL, duration_ms INTEGER, "
            "prompt_chars INTEGER, response_chars INTEGER, created_at TIMESTAMP)"
        )
        await db.execute(
            "INSERT INTO ai_token_usage (id, job_id) VALUES ('old', 'job')"
        )
        await db.commit()
    await storage.init_db()
    assert (await storage.get_token_usage_for_job("job"))[0][
        "credential_source"
    ] == "unknown"


@pytest.mark.asyncio
async def test_direct_session_source_and_cross_user_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "sessions.db")
    await storage.init_db()
    client = AsyncMock()
    client.create_session.return_value = "direct-sid"
    token = ai_client.ai_username.set("alice")
    try:
        assert (
            await ai_client.create_session_safely(
                client, provider="openai", model="m", system_prompt="test"
            )
            == "direct-sid"
        )
    finally:
        ai_client.ai_username.reset(token)
    assert (
        await storage.get_ai_session_source("direct-sid", "alice", "openai") == "server"
    )
    with pytest.raises(ValueError):
        await storage.get_ai_session_source("direct-sid", "bob", "openai")


@pytest.mark.asyncio
async def test_keyed_session_creation_survives_key_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "keyed.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "user-key")
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("openai", "m"))
    )
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value="user-key"))

    async def sidecar(request):
        if request.url.path == "/sessions":
            expected_key = b'"api_key":"user-key"'  # pragma: allowlist secret
            assert expected_key in request.content
            return httpx.Response(200, json={"session_id": "keyed-sid"})
        return httpx.Response(200, json={"text": "reply"})

    client = ai_client.SidecarClient.__new__(ai_client.SidecarClient)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(sidecar), base_url="http://sidecar"
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    token = ai_client.ai_username.set("alice")
    try:
        first = await real_call_ai("first", ai_provider="openai", ai_model="m")
        assert first.credential_source == "user"
        await storage.update_user_ai_credential("alice", "openai", "new-key")
        resumed = await real_call_ai(
            "second", ai_provider="openai", ai_model="m", session_id="keyed-sid"
        )
        assert not resumed.success  # generation changed after creation
        ai_client.ai_username.set("bob")
        denied = await real_call_ai(
            "third", ai_provider="openai", ai_model="m", session_id="keyed-sid"
        )
        assert not denied.success
        assert denied.session_id is None
        with pytest.raises(ValueError, match="revoked"):
            await storage.get_ai_session_source("keyed-sid", "alice", "openai")
    finally:
        ai_client.ai_username.reset(token)
        await client._client.aclose()


@pytest.mark.asyncio
async def test_reused_session_id_replaces_tombstone_but_not_active_owner(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "reuse.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    with pytest.raises(ValueError, match="already active"):
        await storage.save_ai_session_source("sid", "bob", "openai", "server")
    await storage.revoke_ai_session_source("sid")
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "alice", "openai")
    await storage.save_ai_session_source("sid", "bob", "openai", "server")
    assert await storage.get_ai_session_source("sid", "bob", "openai") == "server"
    with pytest.raises(ValueError):
        await storage.get_ai_session_source("sid", "alice", "openai")


@pytest.mark.asyncio
async def test_rotation_revokes_keyed_peer_and_chat_but_not_server(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "rotation.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old-key")
    for sid, source in (("peer", "user"), ("chat", "user"), ("server", "server")):
        await storage.save_ai_session_source(sid, "alice", "openai", source)
    await storage.add_chat_message(
        "job",
        "assistant",
        "hello",
        username="alice",
        ai_provider="openai",
        session_id="chat",
    )
    assert set(await storage.update_user_ai_credential("alice", "openai", None)) == {
        "peer",
        "chat",
    }
    for sid in ("peer", "chat"):
        with pytest.raises(ValueError, match="revoked"):
            await storage.get_ai_session_source(sid, "alice", "openai")
    assert await storage.get_ai_session_source("server", "alice", "openai") == "server"
    await storage.save_ai_session_source("peer", "bob", "openai", "server")
    assert await storage.get_ai_session_source("peer", "bob", "openai") == "server"


@pytest.mark.asyncio
async def test_rotation_cannot_revoke_reused_session(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "rotate-reuse.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.update_user_ai_credential("alice", "openai", "old")
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    sessions = await storage.update_user_ai_credential("alice", "openai", "new")
    await storage.save_ai_session_source("sid", "bob", "openai", "server")
    sidecar = AsyncMock()
    monkeypatch.setattr("pi_sidecar_client.get_sidecar_client", lambda: sidecar)
    await main._revoke_ai_sessions(sessions)
    sidecar.delete_session.assert_not_awaited()
    assert await storage.get_ai_session_source("sid", "bob", "openai") == "server"


@pytest.mark.asyncio
async def test_discard_never_deletes_replacement_session(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "discard-reuse.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    await storage.save_ai_session_source("sid", "bob", "openai", "server")
    sidecar = AsyncMock()
    monkeypatch.setattr("pi_sidecar_client.get_sidecar_client", lambda: sidecar)
    await main._revoke_ai_sessions(["sid"])
    sidecar.delete_session.assert_not_awaited()
    assert await storage.get_ai_session_source("sid", "bob", "openai") == "server"


@pytest.mark.asyncio
async def test_revocation_deletes_old_sidecar_before_id_can_be_reused(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "locked.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    deleting = asyncio.Event()
    continue_delete = asyncio.Event()
    sidecar = AsyncMock()

    async def slow_delete(sid):
        deleting.set()
        await continue_delete.wait()

    sidecar.delete_session.side_effect = slow_delete
    monkeypatch.setattr("pi_sidecar_client.get_sidecar_client", lambda: sidecar)
    revocation = asyncio.create_task(main._revoke_ai_sessions(["sid"]))
    await asyncio.wait_for(deleting.wait(), 10)
    replacement = asyncio.create_task(
        storage.save_ai_session_source("sid", "bob", "openai", "server")
    )
    try:
        await asyncio.sleep(0.05)
        assert not replacement.done()
    finally:
        continue_delete.set()
        await revocation
        await replacement
    sidecar.delete_session.assert_awaited_once_with("sid")
    assert await storage.get_ai_session_source("sid", "bob", "openai") == "server"


@pytest.mark.asyncio
async def test_new_sidecar_create_waits_for_old_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "create-race.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    deleting = asyncio.Event()
    continue_delete = asyncio.Event()
    client = AsyncMock()

    async def slow_delete(sid):
        deleting.set()
        await continue_delete.wait()

    client.delete_session.side_effect = slow_delete
    client.create_session.return_value = "sid"
    monkeypatch.setattr("pi_sidecar_client.get_sidecar_client", lambda: client)
    revocation = asyncio.create_task(main._revoke_ai_sessions(["sid"]))
    await asyncio.wait_for(deleting.wait(), 10)
    token = ai_client.ai_username.set("bob")
    creation = asyncio.create_task(
        ai_client.create_session_safely(
            client, provider="openai", model="m", system_prompt="test"
        )
    )
    try:
        await asyncio.sleep(0.05)
        client.create_session.assert_not_awaited()
    finally:
        continue_delete.set()
        await revocation
        await creation
        ai_client.ai_username.reset(token)
    assert await storage.get_ai_session_source("sid", "bob", "openai") == "server"


@pytest.mark.asyncio
async def test_sidecar_delete_failure_keeps_tombstone(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "delete-failure.db")
    await storage.init_db()
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.revoke_ai_session_source("sid")
    sidecar = AsyncMock()
    sidecar.delete_session.side_effect = OSError("unavailable")
    monkeypatch.setattr("pi_sidecar_client.get_sidecar_client", lambda: sidecar)
    await main._revoke_ai_sessions(["sid"])
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "alice", "openai")


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_job_delete_revokes_session_source(tmp_path, monkeypatch, bulk):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "delete.db")
    await storage.init_db()
    await storage.save_result("job", status="completed", result={})
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        "job",
        "assistant",
        "reply",
        username="alice",
        ai_provider="openai",
        session_id="sid",
    )
    if bulk:
        await storage.delete_jobs_bulk(["job"])
    else:
        await storage.delete_job("job")
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("sid", "alice", "openai")
    await storage.save_ai_session_source("sid", "bob", "openai", "server")


@pytest.mark.asyncio
async def test_clear_chat_revokes_only_deleted_user_session(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "clear.db")
    await storage.init_db()
    for user in ("alice", "bob"):
        await storage.save_ai_session_source(user, user, "openai", "user")
        await storage.add_chat_message(
            "job",
            "assistant",
            "reply",
            username=user,
            ai_provider="openai",
            session_id=user,
        )
    await storage.delete_chat_messages("job", username="alice")
    with pytest.raises(ValueError, match="revoked"):
        await storage.get_ai_session_source("alice", "alice", "openai")
    assert await storage.get_ai_session_source("bob", "bob", "openai") == "user"


@pytest.mark.asyncio
async def test_lookup_failure_never_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "missing.db")
    await storage.init_db()
    monkeypatch.setattr(
        ai_client, "resolve_catalog_pair", AsyncMock(return_value=("p", "m"))
    )
    monkeypatch.setattr(
        storage, "get_ai_session_source", AsyncMock(side_effect=OSError("db down"))
    )
    prompt = AsyncMock()
    monkeypatch.setattr(ai_client, "_call_user_session", prompt)
    monkeypatch.setattr(ai_client, "_call_ai", prompt)
    token = ai_client.ai_username.set("alice")
    try:
        result = await real_call_ai(
            "secret prompt", ai_provider="p", ai_model="m", session_id="sid"
        )
        assert not result.success and result.text == "AI session unavailable"
        prompt.assert_not_awaited()
    finally:
        ai_client.ai_username.reset(token)


@pytest.mark.asyncio
async def test_live_report_usage_includes_chat(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "report.db")
    await storage.init_db()
    await storage.save_result(
        "job", status="completed", result={"token_usage": {"calls": []}}
    )
    await storage.record_token_usage(
        "job", "openai", "m", "analysis", credential_source="server"
    )
    await storage.record_token_usage(
        "job", "openai", "m", "chat", credential_source="user"
    )
    result = await storage.get_result("job")
    await main._attach_token_usage("job", result["result"])
    assert [
        c["credential_source"] for c in result["result"]["token_usage"]["calls"]
    ] == ["server", "user"]
    assert (await build_token_usage_summary("job")).total_calls == 2
    _print_job_token_usage(
        {"job_id": "job", "records": await storage.get_token_usage_for_job("job")}
    )
    assert "[chat]" in capsys.readouterr().out
    assert [
        c["credential_source"]
        for c in (await storage.get_result("job"))["result"]["token_usage"]["calls"]
    ] == ["server", "user"]


@pytest.mark.asyncio
async def test_usage_snapshot_survives_concurrent_analysis_save(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "snapshot.db")
    await storage.init_db()
    await storage.save_result("job", status="running", result={"summary": "before"})
    await storage.record_token_usage(
        "job", "openai", "m", "chat", credential_source="user"
    )
    await storage.update_status("job", "completed", {"summary": "after"})
    saved = (await storage.get_result("job"))["result"]
    assert saved["summary"] == "after"
    assert saved["token_usage"]["calls"][0]["credential_source"] == "user"


@pytest.mark.asyncio
async def test_usage_snapshot_preserves_other_result_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "snapshot-fields.db")
    await storage.init_db()
    await storage.save_result(
        "job", status="completed", result={"summary": "keep", "failures": [1]}
    )
    await storage.record_token_usage(
        "job", "openai", "m", "chat", credential_source="user"
    )
    saved = (await storage.get_result("job"))["result"]
    assert saved["summary"] == "keep"
    assert saved["failures"] == [1]
    assert saved["token_usage"]["calls"][0]["credential_source"] == "user"
