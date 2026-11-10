"""A message never reaches a turn on a half-cloned workspace (#511, A5).

The refusal is a ``425`` naming the phase, evaluated from the registry the route
writes synchronously — and the guarantee behind it is the per-user chat lock the
preparation holds for its whole body.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from rootcoz import main, storage
from rootcoz.models import ChatMessageRequest

KEY = "job:alice"


def _request(username="alice", role="reviewer"):
    return SimpleNamespace(state=SimpleNamespace(username=username, role=role))


async def _job(monkeypatch, tmp_path, job_id="job"):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / f"{job_id}.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer", can_use_server_providers=True)
    await storage.create_admin_user("root")
    await storage.save_result(
        job_id,
        "",
        "completed",
        {
            "status": "completed",
            "result": {"request_params": {}},
            "request_params": {},
            "ai_provider": "openai",
            "ai_model": "chosen",
        },
    )
    await storage.update_user_ai_credential(
        "alice",
        "openai",
        "user-key",  # pragma: allowlist secret
    )
    await storage.save_ai_session_source("sid", "alice", "openai", "user")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="chosen",
        session_id="sid",
    )


async def _wait_for(
    events: list[str], name: str, handshake=None, timeout: float = 5
) -> None:
    """Poll the shared event log instead of hanging on a missing entry.

    ``handshake`` is reported with the failure: when a preparation ends in its
    preflight it never reaches the step being awaited, and its error is the only
    thing that says why. Without it this wait failed silently for 5 h 20 min.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while name not in events:
        assert asyncio.get_running_loop().time() < deadline, (
            f"{name!r} never happened within {timeout}s: events={events}",
            f"preparation_error={getattr(handshake, 'error', None)!r}",
        )
        await asyncio.sleep(0.01)


def _live_preparation(phase="cloning", detail="repo"):
    """Register a preparation the way the route does, without running one."""
    task = asyncio.create_task(asyncio.sleep(30))
    main._chat_prep_tasks[KEY] = main._ChatPreparation(
        pair=("openai", "chosen"), task=task
    )
    main.set_chat_prep_phase("job", "alice", phase, detail)
    return task


def _forget_preparation(task):
    task.cancel()
    main._chat_prep_tasks.pop(KEY, None)
    main.clear_chat_prep_phase("job", "alice")


@pytest.mark.asyncio
async def test_send_is_refused_while_preparing(monkeypatch, tmp_path):
    """425 names the phase, and it is answered without waiting for the lock."""
    await _job(monkeypatch, tmp_path)
    task = _live_preparation()
    try:
        async with main._hold_chat_lock(KEY):
            with pytest.raises(HTTPException) as refused:
                await asyncio.wait_for(
                    main.send_chat_message(
                        "job",
                        ChatMessageRequest(message="hi"),
                        _request(),
                        BackgroundTasks(),
                    ),
                    1,
                )
        assert refused.value.status_code == 425
        assert refused.value.detail == (
            "Chat is being prepared (cloning repo). Wait for it to be ready."
        )
    finally:
        _forget_preparation(task)
    assert await storage.count_chat_messages("job", username="alice") == 1


@pytest.mark.asyncio
async def test_refusal_comes_after_authorization(monkeypatch, tmp_path):
    """A denied caller gets its own answer; a repository name is never leaked."""
    await _job(monkeypatch, tmp_path)
    task = _live_preparation()
    try:
        with pytest.raises(HTTPException) as viewer:
            await main.send_chat_message(
                "job",
                ChatMessageRequest(message="hi"),
                _request(role="viewer"),
                BackgroundTasks(),
            )
        assert viewer.value.status_code == 403
        assert "cloning" not in str(viewer.value.detail)

        with pytest.raises(HTTPException) as missing:
            await main.send_chat_message(
                "deleted-job",
                ChatMessageRequest(message="hi"),
                _request(),
                BackgroundTasks(),
            )
        assert missing.value.status_code == 404
        assert "cloning" not in str(missing.value.detail)
    finally:
        _forget_preparation(task)


def _wire_turn(monkeypatch, job_id, events, workspace):
    """A chat turn down to fakes, recording when the clone and the AI are entered."""
    from rootcoz.engine import chat, session_tools
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: workspace)

    async def clone(*_args, **_kwargs):
        events.append("clone")

    monkeypatch.setattr(chat, "clone_chat_repos", clone)
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main,
        "_resolve_chat_credentials",
        AsyncMock(return_value=("", "", None, "", "")),
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def tools(**_kwargs):
        return SimpleNamespace(custom=[])

    monkeypatch.setattr(session_tools, "build_session_tools", tools)
    monkeypatch.setattr(main, "_index_chat_repositories", AsyncMock())

    async def validate(provider, model):
        from rootcoz import ai_client

        ai_client._selected_credential_source.set("user")
        return provider, model

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)


@pytest.mark.asyncio
async def test_turn_and_preparation_never_interleave(monkeypatch, tmp_path):
    """The lock orders the two in both directions; nothing else is claimed."""
    from rootcoz.engine import chat

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events: list[str] = []
    ai_gate = asyncio.Event()
    clone_gate = asyncio.Event()

    await _job(monkeypatch, tmp_path, "lock-job")
    _wire_turn(monkeypatch, "lock-job", events, workspace)

    async def chat_with_ai(**_kwargs):
        events.append("chat_with_ai")
        await ai_gate.wait()
        return True, "ok", "sid"

    monkeypatch.setattr(chat, "chat_with_ai", chat_with_ai)

    state: dict[str, object] = {"preparation": None}

    async def clone(*_args, **_kwargs):
        if asyncio.current_task() is not state["preparation"]:
            return  # the turn's own re-clone is not what this test measures
        events.append("clone")
        await clone_gate.wait()

    monkeypatch.setattr(chat, "clone_chat_repos", clone)
    _user_id, assistant_id = await storage.add_chat_message_pair(
        "lock-job", "hello", username="alice", ai_provider="openai", ai_model="chosen"
    )
    turn = asyncio.create_task(
        main._process_chat_message(
            job_id="lock-job",
            user_msg_id=_user_id,
            assistant_msg_id=assistant_id,
            message="hello",
            ai_provider_override="openai",
            ai_model_override="chosen",
            username="alice",
        )
    )
    await _wait_for(events, "chat_with_ai")

    # The turn holds the lock and is inside its body: the preparation's clone
    # waits for it. (The API refuses a send during a preparation, so a turn
    # arriving first is the direction that has to be driven directly.)
    handshake = main._ChatInitHandshake()
    preparation = asyncio.create_task(
        main._run_chat_preparation(
            "lock-job",
            "alice",
            SimpleNamespace(ai_provider="openai", ai_model="chosen"),
            handshake,
        )
    )
    state["preparation"] = preparation
    main._chat_prep_tasks["lock-job:alice"] = main._ChatPreparation(
        pair=("openai", "chosen"), task=preparation
    )
    await asyncio.sleep(0.05)
    assert "clone" not in events, "the preparation cloned under a running turn"

    ai_gate.set()
    await asyncio.wait_for(turn, 5)
    await _wait_for(events, "clone", handshake)
    clone_gate.set()
    await asyncio.wait_for(preparation, 5)
    assert events[:2] == ["chat_with_ai", "clone"]


@pytest.mark.asyncio
async def test_abort_while_a_preparation_runs(monkeypatch, tmp_path):
    """Abort still fails the placeholder, and the AI call never starts."""
    from rootcoz.engine import chat

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events: list[str] = []
    await _job(monkeypatch, tmp_path, "abort-job")
    _wire_turn(monkeypatch, "abort-job", events, workspace)

    async def chat_with_ai(**_kwargs):
        events.append("chat_with_ai")
        return True, "ok", "sid"

    monkeypatch.setattr(chat, "chat_with_ai", chat_with_ai)
    _, assistant_id = await storage.add_chat_message_pair(
        "abort-job", "hello", username="alice", ai_provider="openai", ai_model="chosen"
    )
    main._chat_prep_tasks["abort-job:alice"] = main._ChatPreparation(
        pair=("openai", "chosen"),
        task=asyncio.create_task(asyncio.sleep(30)),
    )
    main.set_chat_prep_phase("abort-job", "alice", "cloning", "repo")
    try:
        aborted = await main.abort_chat("abort-job", _request())
        assert aborted == {"aborted": 1}
        assert await storage.get_chat_message_status(assistant_id) == "failed"
        # Bounded like every other wait here: a turn that blocks would otherwise
        # hold the suite until the CI job's own timeout.
        await asyncio.wait_for(
            main._process_chat_message(
                job_id="abort-job",
                user_msg_id=0,
                assistant_msg_id=assistant_id,
                message="hello",
                ai_provider_override="openai",
                ai_model_override="chosen",
                username="alice",
            ),
            5,
        )
    finally:
        task = main._chat_prep_tasks.pop("abort-job:alice", None)
        if task is not None:
            task.task.cancel()
        main.clear_chat_prep_phase("abort-job", "alice")
    assert "chat_with_ai" not in events, "the aborted turn still reached the AI"
