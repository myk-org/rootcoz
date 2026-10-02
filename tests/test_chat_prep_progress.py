"""Chat /init reports workspace-preparation phases while it runs (issue #279)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rootcoz import ai_client, main, storage
from rootcoz.models import ChatInitRequest


async def _setup_job(monkeypatch, tmp_path, gate: asyncio.Event | None = None):
    """Prepare an analyzed job whose chat init blocks inside clone_chat_repos."""
    from rootcoz.engine import chat
    from rootcoz.sources import chat_workspace

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_result(
        "job",
        "",
        "completed",
        {"status": "completed", "result": {}, "request_params": {}},
    )
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *a, **k: tmp_path)

    seen: list[str] = []

    async def fake_clone(workspace, params, user_repo_token="", on_repo=None):
        if on_repo:
            on_repo("tests-repo")
        seen.append("cloning")
        if gate is not None:
            await gate.wait()
        return True

    monkeypatch.setattr(chat, "clone_chat_repos", fake_clone)
    monkeypatch.setattr(
        chat_workspace, "setup_ci_build_workspace", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        main, "_resolve_chat_credentials", AsyncMock(return_value=("", "", "", "", ""))
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))

    async def validate(p, m):
        ai_client._selected_credential_source.set("user")
        return p, m

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)

    async def create(**kwargs):
        await storage.save_ai_session_source("sid", "alice", "openai", "user")
        return "sid"

    monkeypatch.setattr(chat, "init_chat_session", create)
    return seen


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(username="alice", is_admin=False, role="reviewer")
    )


@pytest.mark.asyncio
async def test_history_reports_phase_while_init_runs_and_clears_after(
    monkeypatch, tmp_path
):
    gate = asyncio.Event()
    await _setup_job(monkeypatch, tmp_path, gate=gate)
    body = ChatInitRequest(
        ai_provider="openai", ai_model="chosen", force_server_credentials=False
    )

    init = asyncio.create_task(main.init_chat("job", _request(), body))
    # Blocked inside clone_chat_repos: history must still answer with the phase.
    for _ in range(200):
        await asyncio.sleep(0.01)
        history = await asyncio.wait_for(
            main.get_chat_history("job", _request(), limit=200, offset=0), 2
        )
        if history.get("preparing"):
            break
    assert history["preparing"] == {"phase": "cloning", "detail": "tests-repo"}

    gate.set()
    assert (await init)["session_id"] == "sid"
    after = await main.get_chat_history("job", _request(), limit=200, offset=0)
    assert after["preparing"] is None


@pytest.mark.asyncio
async def test_phase_cleared_when_init_fails(monkeypatch, tmp_path):
    from fastapi import HTTPException

    from rootcoz.engine import chat

    await _setup_job(monkeypatch, tmp_path)

    async def boom(*a, **k):
        raise HTTPException(status_code=409, detail="nope")

    monkeypatch.setattr(chat, "clone_chat_repos", boom)
    with pytest.raises(HTTPException):
        await main.init_chat(
            "job",
            _request(),
            ChatInitRequest(
                ai_provider="openai", ai_model="chosen", force_server_credentials=False
            ),
        )
    history = await main.get_chat_history("job", _request(), limit=200, offset=0)
    assert history["preparing"] is None


@pytest.mark.asyncio
async def test_clone_reports_repo_name_before_cloning(monkeypatch, tmp_path):
    from rootcoz.engine.chat import clone_chat_repos
    from rootcoz.repository import RepositoryManager

    monkeypatch.setattr(RepositoryManager, "clone_into", lambda *a, **k: None)
    reported: list[str] = []
    await clone_chat_repos(
        tmp_path,
        {"tests_repo_url": "https://github.com/acme/tests-repo.git"},
        on_repo=reported.append,
    )
    assert reported == ["tests-repo"]
