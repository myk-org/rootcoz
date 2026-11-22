"""Regression tests for #640: non-blocking chat history reads and turn timing logs."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rootcoz import main, storage
from rootcoz.engine import chat
from tests.conftest import capture_main_logs


def _request(
    *, username: str = "alice", is_admin: bool = False, role: str = "reviewer"
):
    return SimpleNamespace(
        state=SimpleNamespace(username=username, is_admin=is_admin, role=role)
    )


async def _seed_chat_session(
    monkeypatch,
    tmp_path,
    *,
    job_id: str,
    username: str = "alice",
    provider: str = "openai",
    model: str = "gpt",
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat504.db")
    await storage.init_db()
    await storage.create_admin_user(username)
    await storage.update_user_ai_credential(
        username,
        provider,
        "user-key",  # pragma: allowlist secret
    )
    if job_id != main.ADMIN_CHAT_JOB_ID:
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "status": "completed",
                "result": {"request_params": {}},
                "request_params": {},
            },
        )
    await storage.save_ai_session_source("sid-1", username, provider, "user")
    await storage.add_chat_message(
        job_id=job_id,
        role="assistant",
        content="",
        username=username,
        ai_provider=provider,
        ai_model=model,
        session_id="sid-1",
        status="completed",
    )


@pytest.mark.asyncio
async def test_get_chat_history_does_not_block_during_active_turn(
    monkeypatch, tmp_path, install_turn_rig, caplog
):
    """GET /api/chat/{job_id} must return immediately while a slow turn holds the chat lock."""
    job_id = "slow-turn-job"
    await _seed_chat_session(monkeypatch, tmp_path, job_id=job_id)
    await install_turn_rig()

    ai_entered = asyncio.Event()
    ai_release = asyncio.Event()

    async def slow_chat_with_ai(**_kwargs):
        ai_entered.set()
        await ai_release.wait()
        return True, "Six of the 10 failures are infrastructure.", "sid-1"

    monkeypatch.setattr(chat, "chat_with_ai", slow_chat_with_ai)
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id,
        "Are these failures infra?",
        username="alice",
        ai_provider="openai",
        ai_model="gpt",
    )

    with capture_main_logs(caplog, logging.INFO, "rootcoz.main"):
        turn = asyncio.create_task(
            main._process_chat_message(
                job_id=job_id,
                user_msg_id=user_id,
                assistant_msg_id=assistant_id,
                message="Are these failures infra?",
                ai_provider_override="openai",
                ai_model_override="gpt",
                username="alice",
                credential_source="user",
            )
        )
        await asyncio.wait_for(ai_entered.wait(), timeout=2)

        # While the turn holds `_hold_chat_lock`, GET must return immediately (not hang).
        mid_turn = await asyncio.wait_for(
            main.get_chat_history(job_id, _request(), limit=200, offset=0),
            timeout=1,
        )
        assert [m["status"] for m in mid_turn["messages"]] == ["completed", "pending"]
        assert mid_turn["active_session"] is not None
        assert mid_turn["active_session"]["ai_provider"] == "openai"

        ai_release.set()
        await asyncio.wait_for(turn, timeout=5)

    after = await main.get_chat_history(job_id, _request(), limit=200, offset=0)
    assert len(after["messages"]) == 2
    assert after["messages"][0]["role"] == "user"
    assert after["messages"][1]["role"] == "assistant"
    assert after["messages"][1]["status"] == "completed"
    assert (
        after["messages"][1]["content"] == "Six of the 10 failures are infrastructure."
    )
    assert "provider=openai, model=gpt, elapsed=" in caplog.text


@pytest.mark.asyncio
async def test_get_admin_chat_history_does_not_block_and_reads_one_snapshot(
    monkeypatch, tmp_path, install_turn_rig
):
    """GET /api/admin/chat skips the chat lock and reads one WAL snapshot during an active turn."""
    await _seed_chat_session(
        monkeypatch, tmp_path, job_id=main.ADMIN_CHAT_JOB_ID, username="alice"
    )
    await install_turn_rig(admin=True)
    monkeypatch.setattr(main, "_ai_admin_role_current", AsyncMock(return_value=True))

    ai_entered = asyncio.Event()
    ai_release = asyncio.Event()

    async def slow_admin_chat(**_kwargs):
        ai_entered.set()
        await ai_release.wait()
        return True, "Admin summary complete.", "sid-1"

    monkeypatch.setattr(chat, "admin_chat_with_ai", slow_admin_chat)
    user_id, assistant_id = await storage.add_chat_message_pair(
        main.ADMIN_CHAT_JOB_ID,
        "Summarize recent failures",
        username="alice",
        ai_provider="openai",
        ai_model="gpt",
    )

    turn = asyncio.create_task(
        main._process_admin_chat_message(
            user_msg_id=user_id,
            assistant_msg_id=assistant_id,
            message="Summarize recent failures",
            ai_provider_override="openai",
            ai_model_override="gpt",
            username="alice",
            credential_source="user",
        )
    )
    await asyncio.wait_for(ai_entered.wait(), timeout=2)

    snapshot_calls = 0
    original_snapshot = storage.get_chat_history_snapshot

    async def counted_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return await original_snapshot(*args, **kwargs)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "admin history must read one snapshot, not separate queries"
        )

    monkeypatch.setattr(storage, "get_chat_history_snapshot", counted_snapshot)
    monkeypatch.setattr(storage, "get_chat_messages", forbidden)
    monkeypatch.setattr(storage, "count_chat_messages", forbidden)
    monkeypatch.setattr(storage, "get_latest_chat_session", forbidden)

    mid_turn = await asyncio.wait_for(
        main.get_admin_chat_history(
            _request(username="alice", is_admin=True, role="admin"),
            limit=200,
            offset=0,
        ),
        timeout=1,
    )
    assert snapshot_calls == 1
    assert [m["status"] for m in mid_turn["messages"]] == ["completed", "pending"]

    ai_release.set()
    await asyncio.wait_for(turn, timeout=5)


@pytest.mark.asyncio
async def test_chat_turn_logs_provider_model_and_elapsed_on_completion_and_failure(
    monkeypatch, caplog
):
    """Engine logs must carry provider, model, and elapsed time on both success and failure."""
    from types import SimpleNamespace as _NS

    async def _record_usage(**_kwargs):
        return None

    async def ok_call_ai(_prompt, **_kwargs):
        return _NS(
            success=True,
            text="Done.",
            error=None,
            session_id="s-ok",
            status_code=200,
            credential_source="server",
            record_usage=_record_usage,
        )

    monkeypatch.setattr(chat, "call_ai", ok_call_ai)
    with capture_main_logs(caplog, logging.INFO, "rootcoz.engine.chat"):
        ok, text, sid = await chat.chat_with_ai(
            job_id="job-log",
            job_name="suite",
            build_number="1",
            message="hi",
            history=[],
            ai_provider="enmaas",
            ai_model="rits/deepseek-ai/DeepSeek-V4.1-Flash-1017",
            session_id="s-ok",
        )
    assert (ok, text, sid) == (True, "Done.", "s-ok")
    assert (
        "AI turn completed (provider=enmaas, model=rits/deepseek-ai/DeepSeek-V4.1-Flash-1017, elapsed="
        in caplog.text
    )

    caplog.clear()

    async def fail_call_ai(_prompt, **_kwargs):
        return _NS(
            success=False,
            text="504 Gateway Time-out",
            error="504 Gateway Time-out",
            session_id=None,
            status_code=504,
            credential_source="server",
            record_usage=_record_usage,
        )

    monkeypatch.setattr(chat, "call_ai", fail_call_ai)
    with capture_main_logs(caplog, logging.ERROR, "rootcoz.engine.chat"):
        ok, text, sid = await chat.chat_with_ai(
            job_id="job-log",
            job_name="suite",
            build_number="1",
            message="hi",
            history=[],
            ai_provider="enmaas",
            ai_model="rits/deepseek-ai/DeepSeek-V4.1-Flash-1017",
            session_id="s-ok",
        )
    assert ok is False
    assert sid is None
    assert (
        "AI call failed (provider=enmaas, model=rits/deepseek-ai/DeepSeek-V4.1-Flash-1017, elapsed="
        in caplog.text
    )
    assert "504 Gateway Time-out" in caplog.text
