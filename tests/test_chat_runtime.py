"""Dedicated unit and integration tests for the unified ChatRuntime module (#660).

Covers:
- Architecture invariants (engine/ never imports sources/, exporters/, or main.py;
  job & admin chat share one turn execution loop via JobChatAdapter / AdminChatAdapter)
- Unified turn execution across both job and admin modes
- Per-user lock refcounting and FIFO serialization
- Job lifecycle barrier release before AI and task cancellation on deletion
- Pre-AI and post-AI abort handling across both modes
- Credential-generation rotation, server-grant revocation, and admin demotion races
- Delta streaming, per-user isolation, mid-turn reconnect snapshots, and channel eviction
- Non-blocking WAL history reads while chat locks are held
- Async job-chat preparation handshake, deduplication, pair conflict, and 425 refusal
- Tool/graph session fingerprinting
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from rootcoz import ai_client, main, storage
from rootcoz.config import Settings
from rootcoz.engine import chat as chat_engine
from rootcoz.engine import chat_runtime, graft_http, session_tools
from rootcoz.engine.chat_runtime import (
    ADMIN_CHAT_JOB_ID,
    AdminChatAdapter,
    ChatRuntime,
    ChatRuntimeHost,
    JobChatAdapter,
)
from rootcoz.models import ChatInitRequest

# ---------------------------------------------------------------------------
# Architecture & dependency-direction invariants
# ---------------------------------------------------------------------------


def test_engine_chat_runtime_has_no_forbidden_imports() -> None:
    """AGENTS.md: engine/ must NEVER import sources/, exporters/, or main.py."""
    engine_dir = Path(chat_runtime.__file__).resolve().parent
    forbidden_prefixes = ("rootcoz.sources", "rootcoz.exporters", "rootcoz.main")

    for py_file in sorted(engine_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"{py_file.name} imports forbidden module {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"{py_file.name} imports from forbidden module {node.module}"
                )


def test_main_delegates_both_chat_modes_to_unified_turn_loop() -> None:
    """Both job and admin processors in main.py delegate to ChatRuntime.process_turn."""
    for fn in (main._process_chat_message, main._process_admin_chat_message):
        src = inspect.getsource(fn)
        assert "_chat_runtime.process_turn(" in src
        assert "chat_with_ai(" not in src
        assert "admin_chat_with_ai(" not in src


# ---------------------------------------------------------------------------
# Helpers for isolated ChatRuntime tests
# ---------------------------------------------------------------------------


async def _setup_db_and_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    conversation_id: str,
    username: str = "alice",
    provider: str = "openai",
    model: str = "gpt-test",
    credential_source: str = "user",
    session_id: str = "sess-1",
    is_admin: bool = True,
) -> tuple[int, int]:
    """Initialize an isolated SQLite DB, user, optional job, session, and message pair."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat-runtime.db")
    await storage.init_db()
    if is_admin:
        await storage.create_admin_user(username)
    else:
        await storage.create_user(username, role="reviewer")
    if credential_source == "user":
        await storage.update_user_ai_credential(
            username, provider, "sk-test-valid-key-1234567890"
        )
    if conversation_id != ADMIN_CHAT_JOB_ID:
        await storage.save_result(
            conversation_id,
            "https://jenkins.example/job/demo/1",
            "completed",
            {
                "status": "completed",
                "job_name": "demo-job",
                "build_number": 42,
                "result": {
                    "job_name": "demo-job",
                    "build_number": 42,
                    "request_params": {"analysis_type": "jenkins"},
                },
            },
        )
    await storage.save_ai_session_source(
        session_id, username, provider, credential_source
    )
    gen = (
        None
        if credential_source == "server"
        else await storage.get_user_ai_credential_generation(username, provider)
    )
    await storage.add_chat_message(
        job_id=conversation_id,
        role="assistant",
        content="",
        username=username,
        ai_provider=provider,
        ai_model=model,
        session_id=session_id,
        status="completed",
        credential_generation=gen,
    )
    return await storage.add_chat_message_pair(
        job_id=conversation_id,
        user_content="Why did test_foo fail?",
        username=username,
        ai_provider=provider,
        ai_model=model,
    )


def _make_runtime(
    workspace: Path,
    *,
    admin_role_current: bool = True,
    indexed_calls: list[tuple[Path, str]] | None = None,
    revoked_sessions: list[list[str]] | None = None,
) -> ChatRuntime:
    """Create an isolated ChatRuntime with lightweight host callbacks."""
    indexed = indexed_calls if indexed_calls is not None else []
    revoked = revoked_sessions if revoked_sessions is not None else []

    async def _role_check(_username: str) -> bool:
        return admin_role_current

    async def _revoke(sessions: list[str]) -> None:
        revoked.append(list(sessions))

    async def _enter(_job_id: str, _username: str, _what: str) -> Path:
        return workspace

    host = ChatRuntimeHost(
        get_settings=lambda: Settings(ai_call_timeout=30),
        ai_admin_role_current=_role_check,
        on_enter_chat_workspace=_enter,
        on_schedule_chat_index=lambda ws, repos=None, job_id="", deadline=None: (
            indexed.append((ws, job_id))
        ),
        on_revoke_ai_sessions=_revoke,
    )
    return ChatRuntime(host)


def _adapter_for(admin: bool, job_id: str = "job-1"):
    return AdminChatAdapter() if admin else JobChatAdapter(job_id)


# ---------------------------------------------------------------------------
# Unified turn execution across both job & admin modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_unified_process_turn_happy_path_and_context_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin: bool
) -> None:
    conversation_id = ADMIN_CHAT_JOB_ID if admin else "job-happy"
    user_msg_id, assistant_msg_id = await _setup_db_and_session(
        monkeypatch, tmp_path, conversation_id=conversation_id
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    indexed: list[tuple[Path, str]] = []
    runtime = _make_runtime(workspace, indexed_calls=indexed)

    monkeypatch.setattr(chat_engine, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        session_tools,
        "build_session_tools",
        AsyncMock(return_value=SimpleNamespace(custom=[])),
    )

    observed_ctx: list[tuple[str, str]] = []

    async def fake_ai(**kwargs):
        observed_ctx.append(
            (ai_client.ai_username.get(), ai_client.chat_session_source.get())
        )
        kwargs["on_delta"]("Hello ")
        kwargs["on_delta"]("world!")
        return True, "Hello world!", "sess-1"

    monkeypatch.setattr(
        chat_engine, "admin_chat_with_ai" if admin else "chat_with_ai", fake_ai
    )

    received_frames: list[dict] = []
    orig_user = ai_client.ai_username.set("outer-user")
    orig_source = ai_client.chat_session_source.set("server")
    try:
        async with runtime.subscribe_chat_stream(conversation_id, "alice") as sub:
            await runtime.process_turn(
                _adapter_for(admin, conversation_id),
                user_msg_id=user_msg_id,
                assistant_msg_id=assistant_msg_id,
                message="Why did test_foo fail?",
                ai_provider_override="openai",
                ai_model_override="gpt-test",
                username="alice",
                is_admin=admin,
                credential_source="user",
            )
            await asyncio.sleep(0)
            while not sub.queue.empty():
                received_frames.append(sub.queue.get_nowait())

        assert observed_ctx == [("alice", "user")]
        assert ai_client.ai_username.get() == "outer-user"
        assert ai_client.chat_session_source.get() == "server"
    finally:
        ai_client.ai_username.reset(orig_user)
        ai_client.chat_session_source.reset(orig_source)

    rows = await storage.get_chat_messages(conversation_id, username="alice")
    assistant = next(r for r in rows if r["id"] == assistant_msg_id)
    assert assistant["status"] == "completed"
    assert assistant["content"] == "Hello world!"
    assert received_frames == [
        {"id": assistant_msg_id, "delta": "Hello "},
        {"id": assistant_msg_id, "delta": "world!"},
        {"type": "refresh"},
    ]
    if admin:
        assert indexed == []
    else:
        assert indexed == [(workspace, conversation_id)]
    assert runtime.chat_locks == {}
    assert runtime.chat_lock_refs == {}
    assert runtime.chat_abort_signals == {}
    assert runtime.chat_partials == {}


# ---------------------------------------------------------------------------
# Locks, barriers, and deletion cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hold_chat_lock_preserves_lock_identity_and_cleans_up() -> None:
    runtime = ChatRuntime()
    order: list[str] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    locks_seen: list[asyncio.Lock] = []

    async def worker(name: str, wait_release: bool) -> None:
        async with runtime.hold_chat_lock("job-lock:alice") as lock:
            locks_seen.append(lock)
            order.append(f"enter-{name}")
            if wait_release:
                first_entered.set()
                await release_first.wait()
            order.append(f"exit-{name}")

    t1 = asyncio.create_task(worker("1", True))
    await first_entered.wait()
    t2 = asyncio.create_task(worker("2", False))
    await asyncio.sleep(0)
    assert runtime.chat_lock_refs["job-lock:alice"] == 2

    release_first.set()
    await asyncio.gather(t1, t2)

    assert order == ["enter-1", "exit-1", "enter-2", "exit-2"]
    assert locks_seen[0] is locks_seen[1]
    assert "job-lock:alice" not in runtime.chat_locks
    assert "job-lock:alice" not in runtime.chat_lock_refs


@pytest.mark.asyncio
async def test_job_turn_releases_barrier_before_ai_and_cancels_on_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_msg_id, assistant_msg_id = await _setup_db_and_session(
        monkeypatch, tmp_path, conversation_id="job-del"
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _make_runtime(workspace)
    monkeypatch.setattr(chat_engine, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        session_tools,
        "build_session_tools",
        AsyncMock(return_value=SimpleNamespace(custom=[])),
    )
    monkeypatch.setattr(
        chat_engine, "get_chat_workspace", lambda job_id, username="": workspace
    )
    monkeypatch.setattr(
        chat_engine,
        "cleanup_chat_workspace",
        lambda job_id: SimpleNamespace(removed=1, failed=0),
    )

    in_ai = asyncio.Event()

    async def slow_ai(**_kwargs):
        # Barrier MUST already be released when AI runs.
        assert not runtime.get_chat_job_barrier("job-del").locked()
        in_ai.set()
        await asyncio.Future()  # wait forever until cancelled

    monkeypatch.setattr(chat_engine, "chat_with_ai", slow_ai)

    turn_task = asyncio.create_task(
        runtime.process_turn(
            JobChatAdapter("job-del"),
            user_msg_id=user_msg_id,
            assistant_msg_id=assistant_msg_id,
            message="hi",
            ai_provider_override="openai",
            ai_model_override="gpt-test",
            username="alice",
            credential_source="user",
        )
    )
    await asyncio.wait_for(in_ai.wait(), 5)
    assert runtime.chat_turns_in_flight() == 1

    # Deleting the job cancels the tracked task cleanly without deadlocking.
    cleaned = await runtime.cleanup_deleted_job_chat_workspaces("job-del")
    assert cleaned is True
    assert turn_task.cancelled() or turn_task.done()
    assert runtime.chat_turns_in_flight() == 0
    assert "job-del" not in runtime.chat_jobs_deleting
    assert "job-del" not in runtime.chat_job_barriers

    rows = await storage.get_chat_messages("job-del", username="alice")
    assistant = next(r for r in rows if r["id"] == assistant_msg_id)
    assert assistant["status"] == "failed"
    assert "cancelled" in assistant["content"].lower()


# ---------------------------------------------------------------------------
# Aborts (pre-AI and post-AI) across both modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("stage", ["before_ai", "during_ai"])
async def test_abort_handling_both_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin: bool, stage: str
) -> None:
    conversation_id = ADMIN_CHAT_JOB_ID if admin else f"job-abort-{stage}"
    user_msg_id, assistant_msg_id = await _setup_db_and_session(
        monkeypatch, tmp_path, conversation_id=conversation_id
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    revoked: list[list[str]] = []
    runtime = _make_runtime(workspace, revoked_sessions=revoked)

    monkeypatch.setattr(chat_engine, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        session_tools,
        "build_session_tools",
        AsyncMock(return_value=SimpleNamespace(custom=[])),
    )

    ai_called = False

    async def fake_ai(**_kwargs):
        nonlocal ai_called
        ai_called = True
        if stage == "during_ai":
            await storage.save_ai_session_source("sess-new", "alice", "openai", "user")
            runtime.get_chat_abort_signal(f"{conversation_id}:alice").set()
        return True, "late answer", "sess-new"

    monkeypatch.setattr(
        chat_engine, "admin_chat_with_ai" if admin else "chat_with_ai", fake_ai
    )

    if stage == "before_ai":
        runtime.get_chat_abort_signal(f"{conversation_id}:alice").set()

    await runtime.process_turn(
        _adapter_for(admin, conversation_id),
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        message="abort me",
        ai_provider_override="openai",
        ai_model_override="gpt-test",
        username="alice",
        is_admin=admin,
        credential_source="user",
    )

    rows = await storage.get_chat_messages(conversation_id, username="alice")
    assistant = next(r for r in rows if r["id"] == assistant_msg_id)
    assert assistant["status"] == "failed"
    assert assistant["content"] == "Aborted by user."

    if stage == "before_ai":
        assert ai_called is False
    else:
        assert ai_called is True
        assert revoked == [["sess-new"]]


# ---------------------------------------------------------------------------
# Credential-generation rotation & server grant revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_credential_rotation_mid_turn_discards_reply_both_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin: bool
) -> None:
    conversation_id = ADMIN_CHAT_JOB_ID if admin else "job-rot"
    user_msg_id, assistant_msg_id = await _setup_db_and_session(
        monkeypatch, tmp_path, conversation_id=conversation_id
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    revoked: list[list[str]] = []
    runtime = _make_runtime(workspace, revoked_sessions=revoked)

    monkeypatch.setattr(chat_engine, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        session_tools,
        "build_session_tools",
        AsyncMock(return_value=SimpleNamespace(custom=[])),
    )

    async def rotating_ai(**_kwargs):
        await storage.save_ai_session_source("sess-rotated", "alice", "openai", "user")
        # Rotate user key mid-flight so credential_generation bumps
        await storage.update_user_ai_credential(
            "alice", "openai", "sk-new-rotated-key-9876543210"
        )
        return True, "stale reply", "sess-rotated"

    monkeypatch.setattr(
        chat_engine, "admin_chat_with_ai" if admin else "chat_with_ai", rotating_ai
    )

    await runtime.process_turn(
        _adapter_for(admin, conversation_id),
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        message="check rotation",
        ai_provider_override="openai",
        ai_model_override="gpt-test",
        username="alice",
        is_admin=admin,
        credential_source="user",
    )

    rows = await storage.get_chat_messages(conversation_id, username="alice")
    assistant = next(r for r in rows if r["id"] == assistant_msg_id)
    assert assistant["status"] == "failed"
    assert "AI credentials changed" in assistant["content"]
    assert revoked == [["sess-rotated"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_server_grant_revoked_mid_turn_refuses_reply_both_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin: bool
) -> None:
    conversation_id = ADMIN_CHAT_JOB_ID if admin else "job-grant"
    user_msg_id, assistant_msg_id = await _setup_db_and_session(
        monkeypatch,
        tmp_path,
        conversation_id=conversation_id,
        credential_source="server",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _make_runtime(workspace)

    monkeypatch.setattr(chat_engine, "clone_chat_repos", AsyncMock(return_value=False))
    monkeypatch.setattr(
        session_tools,
        "build_session_tools",
        AsyncMock(return_value=SimpleNamespace(custom=[])),
    )
    from rootcoz import ai_catalog

    monkeypatch.setattr(
        ai_catalog,
        "check_server_provider_grant",
        AsyncMock(side_effect=ValueError("Server grant revoked")),
    )
    monkeypatch.setattr(
        chat_engine,
        "admin_chat_with_ai" if admin else "chat_with_ai",
        AsyncMock(return_value=(True, "should not commit", "sess-1")),
    )

    await runtime.process_turn(
        _adapter_for(admin, conversation_id),
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        message="check grant",
        ai_provider_override="openai",
        ai_model_override="gpt-test",
        username="alice",
        is_admin=admin,
        credential_source="server",
    )

    rows = await storage.get_chat_messages(conversation_id, username="alice")
    assistant = next(r for r in rows if r["id"] == assistant_msg_id)
    assert assistant["status"] == "failed"
    assert assistant["content"] == "Server grant revoked"


@pytest.mark.asyncio
async def test_admin_role_revoked_before_ai_blocks_admin_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_msg_id, assistant_msg_id = await _setup_db_and_session(
        monkeypatch, tmp_path, conversation_id=ADMIN_CHAT_JOB_ID
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _make_runtime(workspace, admin_role_current=False)

    ai_mock = AsyncMock(return_value=(True, "should never run", "sess-1"))
    monkeypatch.setattr(chat_engine, "admin_chat_with_ai", ai_mock)

    await runtime.process_turn(
        AdminChatAdapter(),
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        message="select * from jobs",
        ai_provider_override="openai",
        ai_model_override="gpt-test",
        username="alice",
        is_admin=True,
        credential_source="user",
    )

    ai_mock.assert_not_called()
    rows = await storage.get_chat_messages(ADMIN_CHAT_JOB_ID, username="alice")
    assistant = next(r for r in rows if r["id"] == assistant_msg_id)
    assert assistant["status"] == "failed"
    assert assistant["content"] == "Admin access required"


# ---------------------------------------------------------------------------
# Delta streaming, reconnect snapshot, and channel eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_streaming_reconnect_snapshot_and_user_isolation() -> None:
    runtime = ChatRuntime()
    publish = runtime.chat_delta_publisher("job-stream", "alice", 77)
    publish("Part 1. ")
    publish("Part 2.")

    # Late subscriber for alice gets one snapshot with accumulated text.
    async with runtime.subscribe_chat_stream("job-stream", "alice") as alice_sub:
        snapshot = alice_sub.queue.get_nowait()
        assert snapshot == {"id": 77, "snapshot": "Part 1. Part 2."}

        # Another user on the same job must NOT see alice's partial text.
        async with runtime.subscribe_chat_stream("job-stream", "bob") as bob_sub:
            assert bob_sub.queue.empty()
            publish(" Part 3.")
            assert await asyncio.wait_for(alice_sub.queue.get(), 1.0) == {
                "id": 77,
                "delta": " Part 3.",
            }
            assert bob_sub.queue.empty()

        runtime.notify_chat_changed("job-stream", username="alice")
        assert await asyncio.wait_for(alice_sub.queue.get(), 1.0) == {"type": "refresh"}

    assert "job-stream:alice" not in runtime.chat_partials
    assert "job-stream:alice" not in runtime.chat_deltas
    assert "job-stream:bob" not in runtime.chat_deltas


# ---------------------------------------------------------------------------
# Non-blocking history reads under active chat locks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_read_chat_history_does_not_block_on_active_chat_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin: bool
) -> None:
    conversation_id = ADMIN_CHAT_JOB_ID if admin else "job-nonblock"
    await _setup_db_and_session(monkeypatch, tmp_path, conversation_id=conversation_id)
    runtime = ChatRuntime()
    monkeypatch.setattr(
        ai_client, "provider_accepts_user_keys", AsyncMock(return_value=True)
    )

    async with runtime.hold_chat_lock(f"{conversation_id}:alice"):
        payload = await asyncio.wait_for(
            runtime.read_chat_history_payload(
                conversation_id, "alice", limit=50, offset=0
            ),
            timeout=2.0,
        )

    assert len(payload["messages"]) == 2
    assert [m["status"] for m in payload["messages"]] == ["completed", "pending"]
    assert payload["active_session"] == {
        "ai_provider": "openai",
        "ai_model": "gpt-test",
        "credential_source": "user",
    }
    assert payload["active_session_version"]
    assert all("session_id" not in m for m in payload["messages"])


# ---------------------------------------------------------------------------
# Async job-chat preparation handshake, dedupe, conflict, and 425 refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_job_chat_init_handshake_dedupe_conflict_and_425(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prep.db")
    await storage.init_db()
    await storage.create_user("alice", role="reviewer")
    await storage.update_user_ai_credential(
        "alice", "openai", "sk-test-valid-key-1234567890"
    )
    await storage.save_result(
        "job-prep",
        "https://jenkins.example/job/demo/1",
        "completed",
        {"status": "completed", "result": {"job_name": "demo", "request_params": {}}},
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    clone_gate = asyncio.Event()
    clone_entered = asyncio.Event()

    async def slow_clone(*_args, **_kwargs):
        clone_entered.set()
        await clone_gate.wait()
        return False

    monkeypatch.setattr(chat_engine, "clone_chat_repos", slow_clone)
    monkeypatch.setattr(graft_http, "cloned_graph_roots", lambda _ws: {})
    monkeypatch.setattr(
        session_tools,
        "build_session_tools",
        AsyncMock(return_value=SimpleNamespace(custom=[])),
    )
    monkeypatch.setattr(
        chat_engine, "init_chat_session", AsyncMock(return_value="sess-prep")
    )

    from rootcoz import ai_catalog

    async def validate_selection(
        provider: str, model: str, *, username: str | None = None
    ) -> ai_catalog.ResolvedAiSelection:
        return ai_catalog.ResolvedAiSelection(provider, model, "user")

    runtime = ChatRuntime(
        ChatRuntimeHost(
            get_settings=lambda: Settings(ai_call_timeout=30),
            validate_catalog_selection=validate_selection,
            on_enter_chat_workspace=AsyncMock(return_value=workspace),
        )
    )

    body = ChatInitRequest(ai_provider="openai", ai_model="gpt-test")
    first = await runtime.start_job_chat_init("job-prep", "alice", body)
    assert first["ready"] is False
    assert first["preparing"]["phase"] == "preparing"
    await asyncio.wait_for(clone_entered.wait(), 5)

    # Same pair is idempotent while running
    second = await runtime.start_job_chat_init("job-prep", "alice", body)
    assert second["ready"] is False

    # Different pair raises 409
    with pytest.raises(HTTPException) as conflict:
        await runtime.start_job_chat_init(
            "job-prep", "alice", ChatInitRequest(ai_provider="openai", ai_model="other")
        )
    assert conflict.value.status_code == 409

    # Enqueue / clear refusal raises 425 while preparing
    with pytest.raises(HTTPException) as too_early:
        runtime.refuse_while_preparing("job-prep", "alice")
    assert too_early.value.status_code == 425

    # Release clone and let background preparation finish
    prep_task = runtime.chat_prep_tasks["job-prep:alice"].task
    clone_gate.set()
    await asyncio.wait_for(prep_task, 5)
    await asyncio.sleep(0)

    assert "job-prep:alice" not in runtime.chat_prep_tasks
    assert "job-prep:alice" not in runtime.chat_prep_phases
    assert runtime.refuse_while_preparing("job-prep", "alice") is None


# ---------------------------------------------------------------------------
# Tool / graph fingerprinting
# ---------------------------------------------------------------------------


def test_tool_fingerprint_and_session_reuse_rules() -> None:
    runtime = ChatRuntime()

    fp_plain = runtime.chat_tool_fingerprint(("github",), [{"name": "get_job_result"}])
    fp_graph = runtime.chat_tool_fingerprint(
        ("github",), [{"name": "get_job_result"}, {"name": "graft_find_code"}]
    )
    assert fp_plain == ("github",)
    assert fp_graph == ("github", "code-graph")

    # Initial adoption
    assert runtime.enable_tools_for_session("s1", fp_plain) == "s1"
    # Graph appears -> rebuild session
    assert runtime.enable_tools_for_session("s1", fp_graph) is None

    # Record graph-enabled session
    runtime.remember_chat_session_tools("s2", fp_graph)
    # Inconclusive graph read (graph omitted, trackers unchanged) -> keep session
    assert runtime.enable_tools_for_session("s2", fp_plain) == "s2"
    # Tracker target changed -> rebuild session
    assert runtime.enable_tools_for_session("s2", ("github", "jira")) is None
