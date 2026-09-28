"""Server AI credentials require an explicit grant, including resumed sessions."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest
from pi_sidecar_client import AIResult

from rootcoz import ai_client, storage
from rootcoz.ai_client import call_ai as real_call_ai


@pytest.mark.asyncio
async def test_old_users_backfill_once_and_keep_revocations(tmp_path, monkeypatch):
    path = tmp_path / "old-users.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, role TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, api_key_hash TEXT)"
        )
        db.execute("INSERT INTO users (username, role) VALUES ('legacy', 'operator')")
        db.execute("INSERT INTO users (username, role) VALUES ('pending', 'reviewer')")
        db.execute(
            "INSERT INTO users (username, role) VALUES ('legacy-admin', 'admin')"
        )
    monkeypatch.setattr(storage, "DB_PATH", path)
    await storage.init_db()
    assert (await storage.get_user_by_username("legacy"))[
        "can_use_server_providers"
    ] is True
    assert (await storage.get_user_by_username("pending"))[
        "can_use_server_providers"
    ] is True
    await storage.set_user_can_use_server_providers("legacy", False)
    await storage.set_user_status("pending", "pending")
    assert await storage.approve_pending_user("pending")
    assert await storage.can_user_use_server_providers("pending")
    await storage.change_user_role("legacy-admin", "operator")
    assert await storage.can_user_use_server_providers("legacy-admin")
    await storage.init_db()
    assert not await storage.can_user_use_server_providers("legacy")
    assert await storage.can_user_use_server_providers("pending")


@pytest.mark.asyncio
async def test_managed_users_default_denied_even_when_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "grant.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.register_user_with_status("bob", storage.hash_api_key("bob-key"))
    assert (await storage.get_user_by_username("alice"))[
        "can_use_server_providers"
    ] is True
    assert (await storage.get_user_by_username("bob"))[
        "can_use_server_providers"
    ] is False
    with pytest.raises(ValueError, match="Cannot revoke"):
        await storage.set_user_can_use_server_providers("alice", False)
    assert await storage.can_user_use_server_providers("alice")
    assert (await storage.list_users())[0]["can_use_server_providers"] is True
    assert not await storage.set_user_can_use_server_providers("missing", True)
    await storage.track_user("sso-new")
    assert not await storage.can_user_use_server_providers("sso-new")


@pytest.mark.asyncio
async def test_concurrent_opposite_approvals_only_winner_sets_grant(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "approval.db")
    await storage.init_db()
    await storage.register_user_with_status("waiting", "fake-hash", status="pending")

    results = await asyncio.gather(
        storage.approve_pending_user("waiting", True),
        storage.approve_pending_user("waiting", False),
    )
    assert results.count(True) == 1
    assert results.count(False) == 1
    assert await storage.get_user_status("waiting") == "active"
    assert (await storage.get_user_by_username("waiting"))[
        "can_use_server_providers"
    ] is results[0]

    assert not await storage.approve_pending_user("waiting", not results[0])
    assert (await storage.get_user_by_username("waiting"))[
        "can_use_server_providers"
    ] is results[0]


@pytest.mark.asyncio
async def test_admin_server_pair_and_nonadmin_revocation_blocks_resumed_session(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "grant.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    monkeypatch.setattr(
        ai_client,
        "_get_model_catalog",
        AsyncMock(return_value=[{"provider": "p", "id": "m"}]),
    )
    sidecar = AsyncMock(return_value=AIResult(success=True, text="ok"))
    monkeypatch.setattr(ai_client, "_call_ai", sidecar)
    token = ai_client.ai_username.set("alice")
    try:
        assert (await ai_client.scoped_models())["p"][0][
            "can_use_server_providers"
        ] is True
        assert await ai_client.resolve_catalog_pair("p", "m") == ("p", "m")
        await storage.save_ai_session_source("sid", "alice", "p", "server")
        await real_call_ai("hi", ai_provider="p", ai_model="m", session_id="sid")
        sidecar.assert_awaited_once()
        await storage.create_user("bob")
        ai_client.ai_username.set("bob")
        await storage.set_user_can_use_server_providers("bob", True)
        await storage.save_ai_session_source("bob-sid", "bob", "p", "server")
        await storage.set_user_can_use_server_providers("bob", False)
        with pytest.raises(ValueError, match="Server provider"):
            await real_call_ai(
                "hi", ai_provider="p", ai_model="m", session_id="bob-sid"
            )
        sidecar.assert_awaited_once()
    finally:
        ai_client.ai_username.reset(token)
