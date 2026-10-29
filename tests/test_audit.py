"""Tests for the structured audit trail (#379).

Two layers: the storage helpers, and ``main.record_audit`` — the single
emission point that writes the row, emits the ``[AUDIT]`` log line and keeps
secrets out of both.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from rootcoz import main, monitoring, storage
from rootcoz.encryption import encrypt_sensitive_fields
from tests.conftest import admin_login, make_app_client

_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret  # gitleaks:allow
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}
_SECRET = "sk-live-0a1b2c3d0a1b2c3d"  # pragma: allowlist secret  # gitleaks:allow


@pytest.fixture
def audit_db(tmp_path, monkeypatch) -> Path:
    """Isolated, schema-initialized DB for the audit tests."""
    db_path = tmp_path / "results.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    asyncio.run(storage.init_db())
    return db_path


def _events(**filters: Any) -> list[dict[str, Any]]:
    """Synchronous read of the recorded events (endpoint tests are sync)."""
    return asyncio.run(storage.list_audit_events(**filters))["events"]


def _fake_request(
    *,
    role: str = "admin",
    is_admin: bool = False,
    headers: dict[str, str] | None = None,
    client_host: str | None = "10.0.0.7",
) -> Any:
    """Minimal stand-in for a Starlette Request on the audit code paths."""
    return SimpleNamespace(
        state=SimpleNamespace(role=role, is_admin=is_admin),
        headers=headers or {},
        client=SimpleNamespace(host=client_host) if client_host else None,
    )


# --- storage round-trip ----------------------------------------------------


async def test_insert_and_list_round_trip(audit_db: Path) -> None:
    await storage.insert_audit_event(
        action="user.job_delete",
        actor="alice",
        actor_role="operator",
        target_type="job",
        target_id="job-1",
        details={"reason": "cleanup"},
    )
    await storage.insert_audit_event(
        action="auth.login",
        actor="bob",
        is_admin=True,
        actor_role="admin",
        outcome="success",
        request_ip="203.0.113.9",
    )

    result = await storage.list_audit_events()
    assert result["total"] == 2
    events = result["events"]
    # Newest first.
    assert [e["action"] for e in events] == ["auth.login", "user.job_delete"]
    login = events[0]
    assert login["actor"] == "bob"
    assert login["is_admin"] is True
    assert login["request_ip"] == "203.0.113.9"
    assert login["details"] == {}
    assert events[1]["details"] == {"reason": "cleanup"}
    assert events[1]["target_type"] == "job"
    assert events[1]["target_id"] == "job-1"


async def test_details_stored_compact_and_non_ascii_unescaped(audit_db: Path) -> None:
    """The stored text is exactly what the browser's JSON.stringify produces."""
    await storage.insert_audit_event(
        action="admin.setting_persist",
        actor="alice",
        details={"key": "value", "note": "naïve"},
    )

    with sqlite3.connect(audit_db) as conn:
        (raw,) = conn.execute(
            "SELECT details FROM audit_events WHERE action = 'admin.setting_persist'"
        ).fetchone()
    assert raw == '{"key":"value","note":"naïve"}'


async def test_details_search_covers_compact_and_legacy_rows(audit_db: Path) -> None:
    await storage.insert_audit_event(
        action="action.compact", actor="alice", details={"key": "value"}
    )
    # Pre-compact rows kept json.dumps' spaced defaults.
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "INSERT INTO audit_events (actor, action, details) VALUES ('alice', ?, ?)",
            ("action.legacy", json.dumps({"key": "value"})),
        )

    compact = await storage.list_audit_events(q='"key":"value"')
    assert sorted(event["action"] for event in compact["events"]) == [
        "action.compact",
        "action.legacy",
    ]

    # A spaced fragment matches the legacy row (and only it).
    spaced = await storage.list_audit_events(q='"key": "')
    assert [event["action"] for event in spaced["events"]] == ["action.legacy"]


@pytest.mark.parametrize(
    ("filters", "expected_action"),
    [
        ({"actor": "alice"}, "user.job_delete"),
        ({"action": "admin.setting_persist"}, "admin.setting_persist"),
        ({"target_type": "setting"}, "admin.setting_persist"),
        ({"target_id": "job-1"}, "user.job_delete"),
        ({"outcome": "denied"}, "auth.login_blocked"),
    ],
)
async def test_list_filters(
    audit_db: Path, filters: dict[str, str], expected_action: str
) -> None:
    await storage.insert_audit_event(
        action="user.job_delete", actor="alice", target_type="job", target_id="job-1"
    )
    await storage.insert_audit_event(
        action="admin.setting_persist",
        actor="admin",
        target_type="setting",
        target_id="ai_model",
    )
    await storage.insert_audit_event(
        action="auth.login_blocked",
        actor="mallory",
        outcome="denied",
    )

    result = await storage.list_audit_events(**filters)
    assert result["total"] == 1
    assert result["events"][0]["action"] == expected_action


async def test_list_pagination_and_total(audit_db: Path) -> None:
    for index in range(5):
        await storage.insert_audit_event(
            action=f"action.{index}", actor="alice", target_id=str(index)
        )

    page = await storage.list_audit_events(limit=2, offset=1)
    assert page["total"] == 5
    assert [e["action"] for e in page["events"]] == ["action.3", "action.2"]


async def test_list_date_range_filter(audit_db: Path) -> None:
    old = await storage.insert_audit_event(action="action.old", actor="alice")
    await storage.insert_audit_event(action="action.new", actor="alice")
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
            (old["id"],),
        )

    recent = await storage.list_audit_events(date_from="2021-01-01T00:00:00")
    assert [e["action"] for e in recent["events"]] == ["action.new"]
    assert recent["total"] == 1


async def test_distinct_audit_actions(audit_db: Path) -> None:
    for _ in range(3):
        await storage.insert_audit_event(action="auth.login", actor="alice")
    await storage.insert_audit_event(action="job.delete", actor="bob")

    actions = await storage.distinct_audit_actions()
    assert [(entry["action"], entry["count"]) for entry in actions] == [
        ("auth.login", 3),
        ("job.delete", 1),
    ]
    assert all(entry["last_seen"] for entry in actions)


async def test_prune_audit_events_by_cutoff(audit_db: Path) -> None:
    old = await storage.insert_audit_event(action="action.old", actor="alice")
    await storage.insert_audit_event(action="action.new", actor="alice")
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
            (old["id"],),
        )

    deleted = await storage.prune_audit_events("2021-01-01 00:00:00")
    assert deleted == 1
    remaining = await storage.list_audit_events()
    assert [e["action"] for e in remaining["events"]] == ["action.new"]


# --- record_audit ----------------------------------------------------------


def _capture_main_logs(caplog: pytest.LogCaptureFixture) -> Any:
    """Attach caplog to the main module logger (simple_logger sets propagate=False)."""
    main_logger = main.logger
    main_logger.setLevel(logging.INFO)
    main_logger.addHandler(caplog.handler)
    return main_logger


async def test_record_audit_writes_row_and_log_line(
    audit_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    main_logger = _capture_main_logs(caplog)

    try:
        await main.record_audit(
            action="admin.user_create",
            actor="admin",
            message="Admin 'admin' created reviewer user 'carol'",
            target_type="user",
            target_id="carol",
            details={"role": "reviewer"},
            request=_fake_request(),
        )
    finally:
        main_logger.removeHandler(caplog.handler)

    assert "[AUDIT] Admin 'admin' created reviewer user 'carol'" in caplog.text

    result = await storage.list_audit_events(action="admin.user_create")
    assert result["total"] == 1
    event = result["events"][0]
    assert event["actor"] == "admin"
    assert event["target_id"] == "carol"
    assert event["details"] == {"role": "reviewer"}
    assert event["outcome"] == "success"


async def test_record_audit_stores_compact_details(
    audit_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _capture_main_logs(caplog)

    await main.record_audit(
        action="admin.setting_persist",
        actor="admin",
        message="Admin 'admin' stored a setting",
        details={"key": "value", "note": "naïve"},
    )

    with sqlite3.connect(audit_db) as conn:
        (raw,) = conn.execute(
            "SELECT details FROM audit_events WHERE action = 'admin.setting_persist'"
        ).fetchone()
    assert raw == '{"key":"value","note":"naïve"}'


async def test_record_audit_swallows_db_failure(
    audit_db: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure counter is process-wide; reset it so health tests stay hermetic.
    monkeypatch.setattr(monitoring, "_audit_write_failures", 0)
    caplog.set_level(logging.INFO)
    main_logger = _capture_main_logs(caplog)

    try:
        with patch.object(
            storage,
            "insert_audit_event",
            AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
        ):
            # Must not raise — an audit problem cannot fail the caller's action.
            await main.record_audit(
                action="auth.login",
                actor="alice",
                message="Login success: user='alice' is_admin=False",
                request=_fake_request(),
            )
    finally:
        main_logger.removeHandler(caplog.handler)

    assert "Failed to persist audit event 'auth.login'" in caplog.text
    assert "[AUDIT] Login success: user='alice' is_admin=False" in caplog.text


async def test_record_audit_never_stores_swallowed_secret(
    audit_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    main_logger = _capture_main_logs(caplog)

    try:
        await main.record_audit(
            action="provider.key.store",
            actor="admin",
            message="Admin 'admin' stored server provider key for 'claude'",
            target_type="provider",
            target_id="claude",
            details={
                "provider": "claude",
                "api_key": _SECRET,
                "nested": {"token": _SECRET},
            },
            request=_fake_request(),
        )
    finally:
        main_logger.removeHandler(caplog.handler)

    event = (await storage.list_audit_events(action="provider.key.store"))["events"][0]
    assert _SECRET not in json.dumps(event)
    assert _SECRET not in caplog.text
    assert event["details"] == {"provider": "claude", "nested": {}}


def test_redact_details_drops_sensitive_names() -> None:
    redacted = main._redact_details(
        {
            "provider": "claude",
            "api_key": _SECRET,
            "jenkins_password": _SECRET,
            "github_token": _SECRET,
            "keys": ["ai_model"],
            "redacted_keys": ["jenkins_password"],
            "nested": {"secret": _SECRET, "test_name": "t"},
        }
    )
    assert redacted == {
        "provider": "claude",
        "keys": ["ai_model"],
        "redacted_keys": ["jenkins_password"],
        "nested": {"test_name": "t"},
    }


def test_audit_setting_details_flags_sensitive_keys() -> None:
    assert main._audit_setting_details("ai_model") == {
        "key": "ai_model",
        "changed": True,
    }
    assert main._audit_setting_details("jenkins_password") == {
        "key": "jenkins_password",
        "changed": True,
        "redacted": True,
    }


# --- request IP ------------------------------------------------------------


def test_request_ip_without_trusted_proxy_is_not_recorded() -> None:
    request = _fake_request(headers={"x-forwarded-for": "203.0.113.9"})
    with patch.object(
        main, "get_settings", return_value=SimpleNamespace(trust_proxy_headers=False)
    ):
        assert main._audit_request_ip(request) is None


def test_request_ip_uses_forwarded_header_behind_trusted_proxy() -> None:
    request = _fake_request(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    with patch.object(
        main, "get_settings", return_value=SimpleNamespace(trust_proxy_headers=True)
    ):
        assert main._audit_request_ip(request) == "203.0.113.9"


def test_request_ip_falls_back_to_client_host() -> None:
    request = _fake_request(client_host="10.0.0.7")
    with patch.object(
        main, "get_settings", return_value=SimpleNamespace(trust_proxy_headers=True)
    ):
        assert main._audit_request_ip(request) == "10.0.0.7"


async def test_record_audit_stores_request_ip_only_behind_trusted_proxy(
    audit_db: Path,
) -> None:
    request = _fake_request(headers={"x-forwarded-for": "203.0.113.9"})
    with patch.object(
        main, "get_settings", return_value=SimpleNamespace(trust_proxy_headers=True)
    ):
        await main.record_audit(
            action="auth.login", actor="alice", message="m", request=request
        )
    with patch.object(
        main, "get_settings", return_value=SimpleNamespace(trust_proxy_headers=False)
    ):
        await main.record_audit(
            action="auth.login", actor="bob", message="m", request=request
        )

    events = (await storage.list_audit_events(action="auth.login"))["events"]
    by_actor = {event["actor"]: event for event in events}
    assert by_actor["alice"]["request_ip"] == "203.0.113.9"
    assert by_actor["bob"]["request_ip"] is None


# --- retention pruning -----------------------------------------------------


async def test_prune_periodically_deletes_old_keeps_recent(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await storage.insert_audit_event(action="action.old", actor="alice")
    await storage.insert_audit_event(action="action.new", actor="alice")
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
            (old["id"],),
        )
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(audit_retention_days=90, audit_max_rows=0),
    )

    assert await main.prune_audit_events_periodically() == 1
    remaining = await storage.list_audit_events()
    assert [e["action"] for e in remaining["events"]] == ["action.new"]


async def test_prune_failure_does_not_consume_the_daily_slot(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed prune must leave the timestamp unset so the next call retries."""
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(audit_retention_days=90, audit_max_rows=0),
    )
    with patch.object(
        storage,
        "prune_audit_events",
        AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
    ):
        assert await main.prune_audit_events_periodically() == 0
    assert main._last_audit_prune_at == 0.0

    old = await storage.insert_audit_event(action="action.old", actor="alice")
    await storage.insert_audit_event(action="action.new", actor="alice")
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
            (old["id"],),
        )

    # No 24h wait: the retry happens on the very next call.
    assert await main.prune_audit_events_periodically() == 1
    assert main._last_audit_prune_at > 0


async def test_prune_periodically_runs_at_most_daily(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await storage.insert_audit_event(action="action.keep", actor="alice")
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(audit_retention_days=90, audit_max_rows=0),
    )

    first = await main.prune_audit_events_periodically()
    second = await main.prune_audit_events_periodically()
    assert first == 0
    assert second == 0
    assert main._last_audit_prune_at > 0


async def test_prune_disabled_when_retention_is_zero(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await storage.insert_audit_event(action="action.keep", actor="alice")
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(audit_retention_days=0, audit_max_rows=0),
    )

    assert await main.prune_audit_events_periodically() == 0
    assert main._last_audit_prune_at == 0
    assert (await storage.list_audit_events())["total"] == 1


# --- migrated and new call sites -------------------------------------------
# --- migrated and new call sites -------------------------------------------


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


def test_startup_prune_respects_saved_retention(tmp_path: Path, monkeypatch) -> None:
    """A saved ``audit_retention_days=0`` (keep forever) survives a restart."""
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    asyncio.run(storage.init_db())
    asyncio.run(storage.set_server_setting("audit_retention_days", "0"))
    old = asyncio.run(storage.insert_audit_event(action="action.old", actor="alice"))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
            (old["id"],),
        )
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)

    gen = make_app_client(db_path)
    next(gen)  # app startup runs the lifespan (and the startup prune)
    try:
        events = asyncio.run(storage.list_audit_events())["events"]
        assert [event["action"] for event in events] == ["action.old"]
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)


async def test_log_stream_is_audited(audit_db: Path) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(role="admin", is_admin=True, username="admin"),
        headers={},
        client=None,
    )

    response = await main.stream_logs(request, lines=5, level="error")
    await response.body_iterator.aclose()

    events = (await storage.list_audit_events(action="logs.stream"))["events"]
    assert len(events) == 1
    assert events[0]["target_type"] == "logs"
    assert events[0]["actor"] == "admin"
    assert events[0]["details"] == {"lines": 5, "level": "ERROR"}


@pytest.fixture
def app_client(temp_db_path: Path):
    """App client whose env/DB patches stay active for the whole test body."""
    yield from make_app_client(temp_db_path)


def test_login_success_is_audited(app_client) -> None:
    admin_login(app_client)

    events = _events(action="auth.login")
    assert len(events) == 1
    event = events[0]
    assert event["actor"] == "admin"
    assert event["is_admin"] is True
    assert event["outcome"] == "success"
    assert event["target_type"] == "user"


def test_login_failure_is_audited(app_client) -> None:
    response = app_client.post(
        "/api/auth/login",
        json={
            "username": "nobody",
            "api_key": "wrong-key-0123456789",  # pragma: allowlist secret  # gitleaks:allow
        },
    )
    assert response.status_code == 401

    events = _events(action="auth.login_failed")
    assert len(events) == 1
    assert events[0]["actor"] == "nobody"
    assert events[0]["outcome"] == "failure"


def test_logout_is_audited(app_client) -> None:
    admin_login(app_client)
    response = app_client.post("/api/auth/logout")
    assert response.status_code == 200, response.text

    events = _events(action="auth.logout")
    assert len(events) == 1
    event = events[0]
    assert event["actor"] == "admin"
    assert event["target_type"] == "user"
    assert event["target_id"] == "admin"
    assert event["outcome"] == "success"
    # The session cookie is cleared, but its value is never a detail.
    assert event["details"] == {"session_cleared": True}


def test_settings_update_audits_keys_without_values(app_client) -> None:
    admin_login(app_client)
    response = app_client.put(
        "/api/admin/settings",
        json={"settings": {"ai_model": "test-model", "jenkins_password": _SECRET}},
    )
    assert response.status_code == 200, response.text

    events = _events(action="settings.update")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["changed_keys"] == ["ai_model", "jenkins_password"]
    assert details["redacted_keys"] == ["jenkins_password"]
    # The pre-existing key list stays for consumers of the original shape.
    assert details["keys"] == ["ai_model", "jenkins_password"]
    assert _SECRET not in json.dumps(events[0])
    assert events[0]["target_type"] == "setting"


def test_settings_import_audits_changed_keys(app_client) -> None:
    admin_login(app_client)
    response = app_client.post(
        "/api/admin/settings/import",
        json={"version": 1, "settings": {"ai_call_timeout": "25"}},
    )
    assert response.status_code == 200, response.text

    details = _events(action="settings.import")[0]["details"]
    assert details == {
        "changed_keys": ["ai_call_timeout"],
        "redacted_keys": [],
        "keys": ["ai_call_timeout"],
    }


def test_settings_reset_is_audited(app_client) -> None:
    admin_login(app_client)
    response = app_client.put(
        "/api/admin/settings", json={"settings": {"ai_model": "test-model"}}
    )
    assert response.status_code == 200, response.text
    response = app_client.delete("/api/admin/settings/ai_model")
    assert response.status_code == 200, response.text

    events = _events(action="settings.reset")
    assert len(events) == 1
    assert events[0]["target_id"] == "ai_model"
    assert events[0]["details"] == {"key": "ai_model", "changed": True}


def test_provider_key_store_audits_provider_not_key(app_client) -> None:
    admin_login(app_client)
    with (
        patch.object(
            main, "_canonical_key_provider_id", AsyncMock(return_value="claude")
        ),
        patch("rootcoz.ai_client.verify_ai_key", AsyncMock(return_value="accepted")),
        patch.object(
            main,
            "_capture_key_scoped_models",
            AsyncMock(return_value=([], "unsupported")),
        ),
        patch.object(main, "_seed_server_provider_selection", AsyncMock()),
    ):
        response = app_client.post(
            "/api/admin/server-providers",
            json={
                "provider": "claude",
                "api_key": _SECRET,
                "verification_model": "test-model",
            },
        )
    assert response.status_code == 200, response.text

    events = _events(action="provider.key.store")
    assert len(events) == 1
    assert events[0]["target_type"] == "provider"
    assert events[0]["target_id"] == "claude"
    assert events[0]["details"] == {"provider": "claude", "outcome": "accepted"}
    assert _SECRET not in json.dumps(events[0])


def test_deployment_key_promotion_has_its_own_action(app_client) -> None:
    """A promoted deployment credential is not a manually entered key (#379)."""
    admin_login(app_client)
    with (
        patch.object(
            main, "_canonical_key_provider_id", AsyncMock(return_value="claude")
        ),
        patch("rootcoz.ai_client.verify_ai_key", AsyncMock(return_value="accepted")),
        patch(
            "rootcoz.ai_client.deployment_provider_credentials",
            lambda: {"claude": _SECRET},
        ),
        patch.object(
            main,
            "_capture_key_scoped_models",
            AsyncMock(return_value=([], "unsupported")),
        ),
        patch.object(main, "_seed_server_provider_selection", AsyncMock()),
    ):
        response = app_client.post(
            "/api/admin/server-providers/claude/store-deployment-key",
            json={"verification_model": "test-model"},
        )
    assert response.status_code == 200, response.text

    events = _events(action="provider.key.promote")
    assert len(events) == 1
    event = events[0]
    assert event["target_type"] == "provider"
    assert event["target_id"] == "claude"
    assert event["outcome"] == "success"
    assert event["details"] == {"provider": "claude", "outcome": "accepted"}
    assert _SECRET not in json.dumps(event)
    # The manual-entry action is not written for a promotion.
    assert _events(action="provider.key.store") == []


def test_analysis_rerun_is_audited_with_previous_classification(
    app_client,
) -> None:
    admin_login(app_client)
    asyncio.run(
        storage.save_result(
            "job-rerun",
            "",
            "completed",
            {
                "job_name": "my-job",
                "analysis_state": "submitted",
                "failures": [
                    {"test_name": "t1", "analysis": {"classification": "CODE"}},
                    {"test_name": "t2", "analysis": {"classification": "CODE"}},
                    {"test_name": "t3", "analysis": {"classification": "INFRA"}},
                ],
                "request_params": encrypt_sensitive_fields(
                    {
                        "analysis_type": "raw",
                        "failures": [{"test_name": "t", "error_message": "e"}],
                        "ai_provider": "claude",
                        "ai_model": "test-model",
                    }
                ),
            },
        )
    )
    with patch("rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock):
        response = app_client.post(
            "/results/job-rerun/analyze",
            json={"ai_provider": "claude", "ai_model": "test-model"},
        )
    assert response.status_code == 202, response.text

    events = _events(action="analysis.rerun")
    assert len(events) == 1
    event = events[0]
    assert event["target_type"] == "job"
    assert event["target_id"] == "job-rerun"
    assert event["outcome"] == "success"
    assert event["actor"] == "admin"
    assert event["details"] == {
        "provider": "claude",
        "model": "test-model",
        "trigger": "manual",
        "previous_classification": "CODE",
    }


def test_analysis_rerun_without_a_classification_omits_the_detail(
    app_client,
) -> None:
    admin_login(app_client)
    asyncio.run(
        storage.save_result(
            "job-fresh",
            "",
            "completed",
            {
                "job_name": "my-job",
                "analysis_state": "submitted",
                "failures": [],
                "request_params": encrypt_sensitive_fields(
                    {
                        "analysis_type": "raw",
                        "failures": [{"test_name": "t", "error_message": "e"}],
                        "ai_provider": "claude",
                        "ai_model": "test-model",
                    }
                ),
            },
        )
    )
    with patch("rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock):
        response = app_client.post(
            "/results/job-fresh/analyze",
            json={"ai_provider": "claude", "ai_model": "test-model"},
        )
    assert response.status_code == 202, response.text
    assert _events(action="analysis.rerun")[0]["details"] == {
        "provider": "claude",
        "model": "test-model",
        "trigger": "manual",
    }


def test_analysis_rerun_failure_is_audited(app_client) -> None:
    admin_login(app_client)
    asyncio.run(
        storage.save_result(
            "job-boom",
            "",
            "completed",
            {
                "job_name": "my-job",
                "analysis_state": "submitted",
                "failures": [],
                "request_params": encrypt_sensitive_fields(
                    {
                        "analysis_type": "raw",
                        "failures": [{"test_name": "t", "error_message": "e"}],
                        "ai_provider": "claude",
                        "ai_model": "test-model",
                    }
                ),
            },
        )
    )
    with (
        patch(
            "rootcoz.main._enqueue_ci_source_analysis",
            AsyncMock(side_effect=RuntimeError("queue down")),
        ),
        # The client re-raises: the audit row is written before the error escapes.
        pytest.raises(RuntimeError),
    ):
        app_client.post(
            "/results/job-boom/analyze",
            json={"ai_provider": "claude", "ai_model": "test-model"},
        )
    assert _events(action="analysis.rerun")[0]["outcome"] == "failure"


def test_tracker_token_set_and_delete_audit_the_tracker_only(app_client) -> None:
    registered = app_client.post("/api/auth/register", json={"username": "carol"})
    assert registered.status_code == 200, registered.text
    session = registered.cookies.get("rootcoz_session")
    headers = {"Authorization": ""}
    cookies = {"rootcoz_session": session}

    token = "jira-token-0123456789abcdef"  # pragma: allowlist secret
    response = app_client.put(
        "/api/user/tokens",
        json={"jira_token": token, "github_token": "gh-token-0123456789"},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    response = app_client.put(
        "/api/user/tokens",
        json={"jira_token": ""},
        headers=headers,
        cookies=cookies,
    )
    assert response.status_code == 200, response.text

    sets = _events(action="user.tracker_token_set")
    assert {event["details"]["tracker"] for event in sets} == {"github", "jira"}
    assert all(event["target_type"] == "user" for event in sets)
    assert all(event["target_id"] == "carol" for event in sets)
    deletes = _events(action="user.tracker_token_delete")
    assert len(deletes) == 1
    assert deletes[0]["details"] == {"tracker": "jira"}
    stored = json.dumps(sets + deletes)
    assert token not in stored
    assert "gh-token-0123456789" not in stored


def test_comment_delete_is_audited_without_the_body(app_client) -> None:
    admin_login(app_client)
    asyncio.run(
        storage.save_result(
            "job-comments",
            "",
            "completed",
            {
                "job_name": "my-job",
                "failures": [{"test_name": "test_a", "analysis": {}}],
            },
        )
    )
    created = app_client.post(
        "/results/job-comments/comments",
        json={"test_name": "test_a", "comment": "body text that must not be audited"},
    )
    assert created.status_code == 201, created.text
    comment_id = created.json()["id"]

    deleted = app_client.delete(f"/results/job-comments/comments/{comment_id}")
    assert deleted.status_code == 200, deleted.text

    events = _events(action="comment.delete")
    assert len(events) == 1
    event = events[0]
    assert event["target_type"] == "comment"
    assert event["target_id"] == str(comment_id)
    assert event["details"] == {"job_id": "job-comments"}
    assert "body text" not in json.dumps(event)


def test_job_delete_is_audited(app_client) -> None:
    admin_login(app_client)
    asyncio.run(
        storage.save_result(
            "job-1",
            status="completed",
            result={"job_name": "my-job", "submitted_by": "admin"},
        )
    )
    response = app_client.delete("/results/job-1")
    assert response.status_code == 200, response.text

    events = _events(action="admin.job_delete")
    assert len(events) == 1
    assert events[0]["target_type"] == "job"
    assert events[0]["target_id"] == "job-1"


def test_export_push_is_audited(app_client) -> None:
    exporter = AsyncMock()
    exporter.push = AsyncMock(
        return_value=SimpleNamespace(
            success=True, message="Pushed 2 items", details={"pushed": 2}
        )
    )
    exporter.__enter__ = lambda self: exporter
    exporter.__exit__ = lambda self, *exc: False

    admin_login(app_client)
    with (
        patch.object(
            main,
            "get_result",
            AsyncMock(return_value={"result": {"failures": [{"test_name": "t"}]}}),
        ),
        patch.object(main, "_build_export_context", AsyncMock(return_value=None)),
        patch.object(main, "_create_exporter", return_value=exporter),
    ):
        response = app_client.post("/results/job-1/push/reportportal")
    assert response.status_code == 200, response.text

    events = _events(action="export.push")
    assert len(events) == 1
    assert events[0]["details"] == {"plugin": "reportportal"}
    assert events[0]["outcome"] == "success"
    assert events[0]["target_id"] == "job-1"


def test_transcript_download_is_audited(app_client, tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"message"}\n')
    session_id = "123e4567-e89b-12d3-a456-426614174000"

    admin_login(app_client)
    with patch.object(main, "transcript_path", return_value=transcript):
        response = app_client.get(f"/api/admin/transcripts/{session_id}")
    assert response.status_code == 200, response.text

    with patch.object(main, "transcript_path", return_value=None):
        missing = app_client.get("/api/admin/transcripts/not-a-uuid")
    assert missing.status_code == 400

    events = _events(action="transcript.download")
    assert [e["outcome"] for e in events] == ["failure", "success"]
    assert events[1]["target_id"] == session_id
    assert events[0]["target_id"] == "not-a-uuid"


def test_issue_create_is_audited(app_client) -> None:
    admin_login(app_client)
    with (
        patch.object(
            main, "_load_effective_failure", AsyncMock(return_value=(None, {}, None))
        ),
        patch.object(
            main, "_resolve_github_repo_url", return_value="https://github.com/o/r"
        ),
        patch.object(
            main,
            "create_github_issue",
            AsyncMock(return_value={"url": "https://github.com/o/r/issues/1"}),
        ),
        patch.object(main, "_add_tracker_comment", AsyncMock(return_value=1)),
    ):
        response = app_client.post(
            "/results/job-1/create-github-issue",
            json={
                "test_name": "test_a",
                "title": "T",
                "body": "B",
                "github_token": "gh-token-0123456789",
            },
        )
    assert response.status_code == 201, response.text

    events = _events(action="issue.create")
    assert len(events) == 1
    assert events[0]["details"]["tracker"] == "github"
    assert events[0]["target_id"] == "job-1"


def test_admin_db_query_is_audited_without_raw_sql(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    with patch.object(main, "DB_PATH", temp_db_path):
        response = app_client.post(
            "/api/admin/db/query", json={"sql": "SELECT 'secret-value' AS v"}
        )
    assert response.status_code == 200, response.text

    events = _events(action="admin.db_query")
    assert len(events) == 1
    details = events[0]["details"]
    assert set(details) == {"sql_hash", "rows"}
    assert details["rows"] == 1
    assert len(details["sql_hash"]) == 64
    assert "secret-value" not in json.dumps(events[0])


# --- GET /api/admin/audit --------------------------------------------------


def _insert_audit(**fields: Any) -> dict[str, Any]:
    """Synchronous insert helper for the endpoint tests."""
    return asyncio.run(storage.insert_audit_event(**fields))


def _clear_audit(db_path: Path) -> None:
    """Drop the events the login itself recorded, so a test starts from zero."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM audit_events")


def test_audit_endpoint_exposes_event_fields(app_client) -> None:
    admin_login(app_client)
    _insert_audit(
        action="admin.setting_persist",
        actor="alice",
        actor_role="admin",
        is_admin=True,
        target_type="setting",
        target_id="ai_model",
        details={"key": "ai_model"},
        request_ip="203.0.113.9",
    )

    response = app_client.get(
        "/api/admin/audit", params={"action": "admin.setting_persist"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (1, 50, 0)
    event = body["events"][0]
    assert event["actor"] == "alice"
    assert event["actor_role"] == "admin"
    assert event["is_admin"] is True
    assert event["action"] == "admin.setting_persist"
    assert event["target_type"] == "setting"
    assert event["target_id"] == "ai_model"
    assert event["outcome"] == "success"
    assert event["details"] == {"key": "ai_model"}
    assert event["request_ip"] == "203.0.113.9"
    assert event["created_at"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"actor": "ALICE"}, ["user.job_delete"]),
        (
            {"action": ["user.job_delete", "auth.login"]},
            ["auth.login", "user.job_delete"],
        ),
        (
            {"action": "auth.login,admin.setting_persist"},
            ["admin.setting_persist", "auth.login"],
        ),
        ({"outcome": "denied"}, ["auth.login_blocked"]),
        ({"target_type": "job"}, ["user.job_delete"]),
        ({"target_id": "job-7"}, ["user.job_delete"]),
        ({"q": "cleanup"}, ["user.job_delete"]),
    ],
)
def test_audit_endpoint_filters(
    app_client, temp_db_path: Path, params: dict[str, Any], expected: list[str]
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    _insert_audit(
        action="user.job_delete",
        actor="alice",
        target_type="job",
        target_id="job-7",
        details={"reason": "cleanup"},
    )
    _insert_audit(action="auth.login", actor="bob")
    _insert_audit(
        action="admin.setting_persist",
        actor="admin",
        target_type="setting",
        target_id="ai_model",
    )
    _insert_audit(action="auth.login_blocked", actor="mallory", outcome="denied")

    response = app_client.get("/api/admin/audit", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [event["action"] for event in body["events"]] == expected
    assert body["total"] == len(expected)


def test_audit_endpoint_date_range(app_client, temp_db_path: Path) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    old = _insert_audit(action="action.old", actor="alice")
    _insert_audit(action="action.new", actor="alice")
    with sqlite3.connect(temp_db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
            (old["id"],),
        )

    since = app_client.get(
        "/api/admin/audit", params={"since": "2021-01-01T00:00:00Z"}
    ).json()
    assert [event["action"] for event in since["events"]] == ["action.new"]

    until = app_client.get(
        "/api/admin/audit", params={"until": "2020-12-31T23:59:59+00:00"}
    ).json()
    assert [event["action"] for event in until["events"]] == ["action.old"]

    bad = app_client.get("/api/admin/audit", params={"since": "yesterday"})
    assert bad.status_code == 400

    # A date-only bound covers the whole day, both ends.
    midday = _insert_audit(action="action.midday", actor="alice")
    with sqlite3.connect(temp_db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = '2022-06-15T15:00:00.000000Z' WHERE id = ?",
            (midday["id"],),
        )
    same_day = app_client.get(
        "/api/admin/audit", params={"since": "2022-06-15", "until": "2022-06-15"}
    ).json()
    assert [event["action"] for event in same_day["events"]] == ["action.midday"]


def test_audit_endpoint_strips_sensitive_detail_fields(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    # Written straight to storage to simulate a row that escaped write-time
    # redaction (older version, manual edit).
    _insert_audit(
        action="admin.db_query",
        actor="alice",
        details={
            "sql_hash": "abc",
            "request_params": {"github_token": _SECRET, "job_name": "keep"},
        },
    )

    response = app_client.get("/api/admin/audit")
    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    assert response.json()["events"][0]["details"] == {
        "sql_hash": "abc",
        "request_params": {"job_name": "keep"},
    }


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("100%", ["action.literal"]),
        ("naïve", ["action.unicode", "action.legacy"]),
        ("cleanup", ["action.literal"]),
    ],
)
async def test_list_details_search_treats_input_literally(
    audit_db: Path, term: str, expected: list[str]
) -> None:
    await storage.insert_audit_event(
        action="action.literal", actor="alice", details={"reason": "cleanup 100% done"}
    )
    await storage.insert_audit_event(
        action="action.unicode", actor="alice", details={"note": "naïve path"}
    )
    # Pre-#379 rows stored json.dumps' ASCII-escaped form.
    await storage.insert_audit_event(
        action="action.other", actor="alice", details={"reason": "nothing here"}
    )
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "INSERT INTO audit_events (actor, action, details)"
            " VALUES ('alice', 'action.legacy', ?)",
            (json.dumps({"note": "naïve path"}),),
        )

    result = await storage.list_audit_events(q=term)
    assert sorted(event["action"] for event in result["events"]) == sorted(expected)


def test_audit_endpoint_pagination_and_ordering(app_client, temp_db_path: Path) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    for index in range(5):
        _insert_audit(action=f"action.{index}", actor="alice", target_id=str(index))

    first = app_client.get("/api/admin/audit", params={"limit": 2}).json()
    assert first["total"] == 5
    assert [event["action"] for event in first["events"]] == ["action.4", "action.3"]

    last = app_client.get("/api/admin/audit", params={"limit": 2, "offset": 4}).json()
    assert [event["action"] for event in last["events"]] == ["action.0"]


def test_audit_endpoint_limit_cap(app_client) -> None:
    admin_login(app_client)
    assert app_client.get("/api/admin/audit", params={"limit": 501}).status_code == 422
    assert app_client.get("/api/admin/audit", params={"limit": 500}).status_code == 200


# --- /api/admin/audit/actions ----------------------------------------------


def test_audit_actions_returns_counts_and_labels(app_client) -> None:
    admin_login(app_client)
    for _ in range(3):
        _insert_audit(action="auth.login", actor="alice")
    _insert_audit(action="user.job_delete", actor="bob")

    response = app_client.get("/api/admin/audit/actions")
    assert response.status_code == 200, response.text
    actions = {entry["action"]: entry for entry in response.json()["actions"]}
    # admin_login itself records one auth.login event.
    assert actions["auth.login"]["count"] == 4
    assert actions["auth.login"]["label"] == "Auth Login"
    assert actions["auth.login"]["last_seen"]
    assert actions["user.job_delete"]["count"] == 1
    assert actions["user.job_delete"]["label"] == "User Job Delete"


# --- Admin gating ----------------------------------------------------------


def test_audit_endpoints_reject_non_admin(app_client) -> None:
    registered = app_client.post("/api/auth/register", json={"username": "bob"})
    assert registered.status_code == 200
    session = registered.cookies.get("rootcoz_session")

    for path in ("/api/admin/audit", "/api/admin/audit/actions"):
        response = app_client.get(
            path, headers={"Authorization": ""}, cookies={"rootcoz_session": session}
        )
        assert response.status_code == 403, path


def test_audit_endpoints_reject_unauthenticated(app_client) -> None:
    """No credentials at all: the admin gate rejects, as on every /api/admin/*.

    The middleware's admin gate runs before the authentication check, so the
    status is 403 here (not 401) — consistent with the rest of the admin API,
    and no audit data is returned either way.
    """
    app_client.cookies.clear()
    for path in (
        "/api/admin/audit",
        "/api/admin/audit/actions",
        "/api/admin/audit/stream",
    ):
        assert app_client.get(path).status_code == 403, path


# --- SSE stream ------------------------------------------------------------


async def test_audit_stream_emits_new_event(audit_db: Path) -> None:
    request = AsyncMock()
    request.state.is_admin = True
    request.is_disconnected = AsyncMock(return_value=False)

    response = await main.stream_audit_events(request)
    body = response.body_iterator
    # First chunk is the immediate connect comment so the browser fires
    # EventSource.onopen without waiting for an event or the keepalive.
    assert await asyncio.wait_for(anext(body), timeout=1) == ": connected\n\n"
    pending = asyncio.create_task(anext(body))
    await asyncio.sleep(0)  # let the generator register its listener
    try:
        await main.record_audit(
            action="admin.user_create",
            actor="admin",
            message="Admin 'admin' created reviewer user 'carol'",
            target_type="user",
            target_id="carol",
        )
        chunk = await asyncio.wait_for(pending, timeout=1)
    finally:
        await body.aclose()

    assert chunk.startswith("event: audit:event\ndata: ")
    payload = json.loads(chunk.split("data: ", 1)[1].removesuffix("\n\n"))
    assert payload["action"] == "admin.user_create"
    assert payload["target_id"] == "carol"
    assert payload["created_at"]


async def test_audit_stream_requires_admin() -> None:
    request = AsyncMock()
    request.state.is_admin = False

    with pytest.raises(Exception) as exc_info:
        await main.stream_audit_events(request)
    assert getattr(exc_info.value, "status_code", None) == 403


# --- tamper evidence: the hash chain --------------------------------------

_LEGACY_AUDIT_SCHEMA = """
    CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        actor TEXT NOT NULL,
        actor_role TEXT,
        is_admin INTEGER NOT NULL DEFAULT 0,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        outcome TEXT NOT NULL DEFAULT 'success',
        details TEXT DEFAULT '{}',
        request_ip TEXT
    )
"""


def _chain_rows(db_path: Path) -> list[tuple[int, str | None, str | None]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id, prev_hash, entry_hash FROM audit_events ORDER BY id"
        ).fetchall()


async def test_chain_links_each_row_to_its_predecessor(audit_db: Path) -> None:
    for index in range(3):
        await storage.insert_audit_event(action=f"action.{index}", actor="alice")

    rows = _chain_rows(audit_db)
    assert rows[0][1] == storage._AUDIT_GENESIS_HASH
    assert rows[1][1] == rows[0][2]
    assert rows[2][1] == rows[1][2]
    assert all(row[2] for row in rows)

    assert await storage.verify_audit_chain() == {
        "checked": 3,
        "legacy": 0,
        "ok": True,
        "first_broken_id": None,
    }


async def test_verify_chain_flags_a_tampered_row(audit_db: Path) -> None:
    await storage.insert_audit_event(action="action.one", actor="alice")
    middle = await storage.insert_audit_event(action="action.two", actor="alice")
    await storage.insert_audit_event(action="action.three", actor="alice")
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_events SET actor = 'mallory' WHERE id = ?", (middle["id"],)
        )

    result = await storage.verify_audit_chain()
    assert result["ok"] is False
    assert result["first_broken_id"] == middle["id"]
    assert result["checked"] == 3


async def test_verify_chain_flags_a_deleted_row(audit_db: Path) -> None:
    await storage.insert_audit_event(action="action.one", actor="alice")
    middle = await storage.insert_audit_event(action="action.two", actor="alice")
    last = await storage.insert_audit_event(action="action.three", actor="alice")
    with sqlite3.connect(audit_db) as conn:
        conn.execute("DELETE FROM audit_events WHERE id = ?", (middle["id"],))

    # The gap breaks the successor's prev_hash link, not the deleted row.
    result = await storage.verify_audit_chain()
    assert result["ok"] is False
    assert result["first_broken_id"] == last["id"]


async def test_verify_chain_reports_legacy_rows_separately(audit_db: Path) -> None:
    with sqlite3.connect(audit_db) as conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO audit_events (created_at, actor, action)"
                " VALUES ('2021-03-04T05:06:07.000000Z', 'alice', 'auth.login')"
            )
    await storage.insert_audit_event(action="action.chained", actor="alice")

    # Pre-chain rows are counted, not called broken, and not faked into it.
    result = await storage.verify_audit_chain()
    assert result == {
        "checked": 1,
        "legacy": 2,
        "ok": True,
        "first_broken_id": None,
    }
    assert _chain_rows(audit_db)[0][2] is None


async def test_verify_chain_limit_checks_only_the_newest_rows(audit_db: Path) -> None:
    first = await storage.insert_audit_event(action="action.one", actor="alice")
    for index in range(2, 6):
        await storage.insert_audit_event(action=f"action.{index}", actor="alice")
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "UPDATE audit_events SET actor = 'mallory' WHERE id = ?", (first["id"],)
        )

    window = await storage.verify_audit_chain(limit=2)
    assert window == {"checked": 2, "legacy": 0, "ok": True, "first_broken_id": None}

    everything = await storage.verify_audit_chain()
    assert everything["ok"] is False
    assert everything["first_broken_id"] == first["id"]


async def test_concurrent_inserts_keep_one_linear_chain(audit_db: Path) -> None:
    await asyncio.gather(
        *(
            storage.insert_audit_event(action=f"action.{index}", actor="alice")
            for index in range(6)
        )
    )

    result = await storage.verify_audit_chain()
    assert result["checked"] == 6
    assert result["ok"] is True


# --- created_at is explicit UTC -------------------------------------------


async def test_stored_created_at_carries_an_explicit_utc_marker(
    audit_db: Path,
) -> None:
    event = await storage.insert_audit_event(action="auth.login", actor="alice")
    assert event["created_at"].endswith("Z")
    parsed = datetime.fromisoformat(event["created_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_init_db_migrates_a_legacy_audit_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing DB gets the chain columns; naive UTC timestamps are rewritten."""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_LEGACY_AUDIT_SCHEMA)
        conn.execute(
            "INSERT INTO audit_events (created_at, actor, action)"
            " VALUES ('2021-03-04 05:06:07', 'alice', 'auth.login')"
        )

    asyncio.run(storage.init_db())
    with sqlite3.connect(db_path) as conn:
        migrated = conn.execute(
            "SELECT created_at, prev_hash, entry_hash FROM audit_events"
        ).fetchone()
    assert migrated == ("2021-03-04T05:06:07.000000Z", None, None)

    # Idempotent: a second startup neither re-adds nor re-rewrites anything.
    asyncio.run(storage.init_db())
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT created_at FROM audit_events").fetchone()[0]
            == "2021-03-04T05:06:07.000000Z"
        )

    assert asyncio.run(storage.verify_audit_chain())["legacy"] == 1


def test_audit_timestamp_treats_naive_input_as_utc() -> None:
    assert storage.audit_timestamp(datetime(2026, 6, 15, 12)) == (
        "2026-06-15T12:00:00.000000Z"
    )
    # An explicit offset is converted, not dropped.
    assert (
        storage.audit_timestamp(
            datetime(2026, 6, 15, 14, tzinfo=timezone(timedelta(hours=2)))
        )
        == "2026-06-15T12:00:00.000000Z"
    )


# --- timezone-correct filtering -------------------------------------------


def _set_created_at(db_path: Path, event_id: int, value: str) -> None:
    """Rewrite one row's ``created_at`` (to place it on a known instant)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET created_at = ? WHERE id = ?", (value, event_id)
        )


def test_audit_endpoint_honours_explicit_offsets(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    late = _insert_audit(action="action.late", actor="alice")
    _set_created_at(temp_db_path, late["id"], "2026-06-15T22:30:00.000000Z")

    # 2026-06-16T00:30+02:00 is the same instant as the row: included.
    same = app_client.get(
        "/api/admin/audit",
        params={
            "since": "2026-06-16T00:30:00+02:00",
            "until": "2026-06-16T01:00:00+02:00",
        },
    ).json()
    assert [event["action"] for event in same["events"]] == ["action.late"]

    # An hour later in the same offset excludes it.
    after = app_client.get(
        "/api/admin/audit", params={"since": "2026-06-16T01:30:00+02:00"}
    ).json()
    assert after["events"] == []


def test_audit_endpoint_date_only_bounds_are_utc_days(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    first = _insert_audit(action="action.early", actor="alice")
    _set_created_at(temp_db_path, first["id"], "2026-06-15T00:00:00.000000Z")
    last = _insert_audit(action="action.late", actor="alice")
    _set_created_at(temp_db_path, last["id"], "2026-06-15T23:59:59.999999Z")

    day = app_client.get(
        "/api/admin/audit", params={"since": "2026-06-15", "until": "2026-06-15"}
    ).json()
    assert [event["action"] for event in day["events"]] == [
        "action.late",
        "action.early",
    ]


def test_audit_endpoint_reads_a_naive_bound_as_utc(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    event = _insert_audit(action="action.only", actor="alice")
    _set_created_at(temp_db_path, event["id"], "2026-06-15T22:30:00.000000Z")

    naive = app_client.get(
        "/api/admin/audit", params={"until": "2026-06-15T22:30:00"}
    ).json()
    assert [entry["action"] for entry in naive["events"]] == ["action.only"]


# --- GET /api/admin/audit/verify ------------------------------------------


def test_audit_verify_endpoint_reports_an_intact_and_a_broken_chain(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    first = _insert_audit(action="action.one", actor="alice")
    _insert_audit(action="action.two", actor="alice")

    intact = app_client.get("/api/admin/audit/verify")
    assert intact.status_code == 200, intact.text
    assert intact.json() == {
        "checked": 2,
        "legacy": 0,
        "ok": True,
        "first_broken_id": None,
    }

    with sqlite3.connect(temp_db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET outcome = 'denied' WHERE id = ?", (first["id"],)
        )
    broken = app_client.get("/api/admin/audit/verify").json()
    assert broken["ok"] is False
    assert broken["first_broken_id"] == first["id"]

    # A window that excludes the tampered row is intact; its link is seeded.
    window = app_client.get("/api/admin/audit/verify", params={"limit": 1}).json()
    assert window == {"checked": 1, "legacy": 0, "ok": True, "first_broken_id": None}


def test_audit_verify_requires_admin(app_client) -> None:
    app_client.cookies.clear()
    assert app_client.get("/api/admin/audit/verify").status_code == 403


# --- GET /api/admin/audit/export ------------------------------------------


def _export(client, **params: Any) -> Any:
    return client.get("/api/admin/audit/export", params=params)


def test_audit_export_json_returns_every_match_in_order(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    for index in range(3):
        _insert_audit(action=f"action.{index}", actor="alice", details={"n": index})

    response = _export(app_client, format="json")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"] == (
        "attachment; filename=audit-export.json"
    )
    events = json.loads(response.text)
    assert [event["action"] for event in events] == [
        "action.2",
        "action.1",
        "action.0",
    ]
    assert all(len(event["entry_hash"]) == 64 for event in events)
    assert events[0]["prev_hash"] == events[1]["entry_hash"]
    assert events[2]["details"] == {"n": 0}


def test_audit_export_streams_past_the_batch_size(
    app_client, temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    for index in range(5):
        _insert_audit(action=f"action.{index}", actor="alice")
    monkeypatch.setattr(main, "_AUDIT_EXPORT_BATCH", 2)

    events = _export(app_client, format="json").json()
    assert len(events) == 5
    assert [event["action"] for event in events] == [
        f"action.{index}" for index in range(4, -1, -1)
    ]


def test_audit_export_csv_has_the_documented_columns(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    _insert_audit(
        action="user.job_delete",
        actor="alice",
        actor_role="operator",
        target_type="job",
        target_id="job-1",
        details={"reason": "cleanup"},
        request_ip="203.0.113.9",
    )

    response = _export(app_client, format="csv")
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        "attachment; filename=audit-export.csv"
    )
    reader = csv.DictReader(io.StringIO(response.text))
    rows = list(reader)
    assert reader.fieldnames == [
        "id",
        "created_at",
        "actor",
        "actor_role",
        "is_admin",
        "action",
        "target_type",
        "target_id",
        "outcome",
        "request_ip",
        "details",
        "entry_hash",
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "alice"
    assert row["actor_role"] == "operator"
    assert row["is_admin"] == "0"
    assert row["action"] == "user.job_delete"
    assert row["target_id"] == "job-1"
    assert row["outcome"] == "success"
    assert row["request_ip"] == "203.0.113.9"
    assert row["details"] == '{"reason":"cleanup"}'
    assert len(row["entry_hash"]) == 64


def test_audit_export_honours_the_list_filters(app_client, temp_db_path: Path) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    keep = _insert_audit(action="action.keep", actor="alice", outcome="denied")
    _insert_audit(action="action.skip", actor="bob")

    events = _export(app_client, format="json", outcome="denied").json()
    assert [event["id"] for event in events] == [keep["id"]]


def test_audit_export_strips_sensitive_detail_fields(
    app_client, temp_db_path: Path
) -> None:
    admin_login(app_client)
    _clear_audit(temp_db_path)
    _insert_audit(
        action="admin.db_query",
        actor="alice",
        details={"request_params": {"github_token": _SECRET, "job_name": "keep"}},
    )

    response = _export(app_client, format="csv")
    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["details"] == '{"request_params":{"job_name":"keep"}}'


def test_audit_export_rejects_non_admin(app_client) -> None:
    registered = app_client.post("/api/auth/register", json={"username": "bob"})
    assert registered.status_code == 200
    session = registered.cookies.get("rootcoz_session")

    response = app_client.get(
        "/api/admin/audit/export",
        params={"format": "json"},
        headers={"Authorization": ""},
        cookies={"rootcoz_session": session},
    )
    assert response.status_code == 403


def test_audit_export_rejects_an_unknown_format(app_client) -> None:
    admin_login(app_client)
    response = app_client.get("/api/admin/audit/export", params={"format": "xml"})
    assert response.status_code == 422


# --- AUDIT_MAX_ROWS size ceiling ------------------------------------------


async def test_row_cap_deletes_the_oldest_rows(audit_db: Path) -> None:
    for index in range(5):
        await storage.insert_audit_event(action=f"action.{index}", actor="alice")

    assert await storage.enforce_audit_row_cap(3) == 2
    remaining = await storage.list_audit_events()
    assert [event["action"] for event in remaining["events"]] == [
        "action.4",
        "action.3",
        "action.2",
    ]


async def test_row_cap_zero_disables_the_ceiling(audit_db: Path) -> None:
    for index in range(3):
        await storage.insert_audit_event(action=f"action.{index}", actor="alice")

    assert await storage.enforce_audit_row_cap(0) == 0
    assert (await storage.list_audit_events())["total"] == 3


async def test_prune_enforces_the_row_cap_with_retention_off(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(4):
        await storage.insert_audit_event(action=f"action.{index}", actor="alice")
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(audit_retention_days=0, audit_max_rows=2),
    )

    assert await main.prune_audit_events_periodically() == 2
    remaining = await storage.list_audit_events()
    assert [event["action"] for event in remaining["events"]] == [
        "action.3",
        "action.2",
    ]


# --- audit write failure signal -------------------------------------------


def _health_settings() -> SimpleNamespace:
    return SimpleNamespace(jenkins_url="", reportportal_enabled=False)


async def test_audit_write_failure_degrades_health(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitoring, "_audit_write_failures", 0)
    monkeypatch.setattr(monitoring, "_audit_suppressed_auth_events", 0)
    with patch.object(
        storage,
        "insert_audit_event",
        AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
    ):
        await main.record_audit(action="auth.login", actor="alice", message="m")
    assert monitoring._audit_write_failures == 1

    with patch.object(
        monitoring, "check_ai_provider", AsyncMock(return_value={"status": "ok"})
    ):
        health = await monitoring.build_health_response(
            _health_settings(), str(audit_db)
        )
    assert health["checks"]["audit"] == {
        "status": "degraded",
        "write_failures": 1,
        "suppressed_auth_events": 0,
    }
    assert health["status"] == "degraded"


async def test_audit_write_failure_is_logged_at_error(
    audit_db: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitoring, "_audit_write_failures", 0)
    caplog.set_level(logging.INFO)
    main_logger = _capture_main_logs(caplog)
    try:
        with patch.object(
            storage,
            "insert_audit_event",
            AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
        ):
            await main.record_audit(action="auth.login", actor="alice", message="m")
    finally:
        main_logger.removeHandler(caplog.handler)

    record = next(
        entry
        for entry in caplog.records
        if "Failed to persist audit event" in entry.getMessage()
    )
    assert record.levelno == logging.ERROR
    assert "1 audit write failure(s)" in record.getMessage()


async def test_successful_audit_write_keeps_health_ok(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitoring, "_audit_write_failures", 0)
    monkeypatch.setattr(monitoring, "_audit_suppressed_auth_events", 0)
    await main.record_audit(action="auth.login", actor="alice", message="m")

    with patch.object(
        monitoring, "check_ai_provider", AsyncMock(return_value={"status": "ok"})
    ):
        health = await monitoring.build_health_response(
            _health_settings(), str(audit_db)
        )
    assert health["checks"]["audit"] == {
        "status": "ok",
        "write_failures": 0,
        "suppressed_auth_events": 0,
    }
    assert health["status"] == "healthy"


def test_metrics_expose_the_audit_write_failure_gauge(
    app_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitoring, "_audit_write_failures", 3)
    text = app_client.get("/metrics").text
    assert "rootcoz_audit_write_failures 3" in text


# --- redaction of credential-shaped text ----------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-live-4f9c2b7d1e6a8c3f5b0d2e7a9c4b6f8d",  # pragma: allowlist secret  # gitleaks:allow
        "ddsat_9f8e7d6c5b4a39281706f5e4d3c2b1a0",  # pragma: allowlist secret  # gitleaks:allow
        "ddapp_4f9c2b7d1e6a8c3f5b0d",  # pragma: allowlist secret  # gitleaks:allow
        "ghp_9f8e7d6c5b4a39281706f5e4d3c2b1a0ffee",  # pragma: allowlist secret  # gitleaks:allow
        "github_pat_11ABCDEFG0abcdefghijklmnop",  # pragma: allowlist secret  # gitleaks:allow
        "xoxb-123456789012-abcdefghijkl",  # pragma: allowlist secret  # gitleaks:allow
        "AKIAIOSFODNN7EXAMPLE",  # pragma: allowlist secret  # gitleaks:allow
        "dGhpcy1pcy1hLXZlcnktbG9uZy1zZWNyZXQtdG9rZW4",
        # Split so the private-key scanner does not treat this sample as a key.
        "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
        "-----END RSA " + "PRIVATE KEY-----",
    ],
)
def test_redact_details_scrubs_credential_shaped_text(secret: str) -> None:
    redacted = main._redact_details({"reason": f"rotated {secret} for alice"})
    # Only the matched substring goes; the sentence around it survives.
    assert redacted == {"reason": "rotated [redacted] for alice"}


def test_redact_details_keeps_normal_text_and_non_strings() -> None:
    details = {
        "reason": "Admin alice deleted job 42 after the review finished",
        "count": 3,
        "is_admin": True,
        "nothing": None,
        "keys": ["ai_model"],
        "notes": ["no secret here either"],
    }
    assert main._redact_details(details) == details


def test_redact_details_keeps_long_identifiers_without_digits() -> None:
    """A long CamelCase/node id is an identifier, not a credential."""
    details = {
        "test_name": "com.example.PaymentServiceTest.testChargeDeclinesWhenCardExpired"
    }
    assert main._redact_details(details) == details


def test_redact_details_scrubs_long_opaque_token_with_digits() -> None:
    token = (
        "AbCdEf0123456789AbCdEf0123456789"  # pragma: allowlist secret  # gitleaks:allow
    )
    redacted = main._redact_details({"note": f"pasted {token} here"})
    assert token not in redacted["note"]
    assert "[redacted]" in redacted["note"]


def test_redact_details_scrubs_secrets_inside_lists() -> None:
    redacted = main._redact_details(
        {"notes": ["first is fine", "second has sk-live-abc123def456ghi789jkl012"]}
    )
    assert redacted == {"notes": ["first is fine", "second has [redacted]"]}


async def test_record_audit_scrubs_a_secret_pasted_into_free_text(
    audit_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    main_logger = _capture_main_logs(caplog)
    secret = "ghp_9f8e7d6c5b4a39281706f5e4d3c2b1a0ffee"  # pragma: allowlist secret  # gitleaks:allow
    try:
        await main.record_audit(
            action="provider.key.rotate",
            actor="admin",
            message="Admin 'admin' rotated a server provider key",
            details={"reason": f"rotated {secret} manually"},
            request=_fake_request(),
        )
    finally:
        main_logger.removeHandler(caplog.handler)

    event = (await storage.list_audit_events(action="provider.key.rotate"))["events"][0]
    assert event["details"] == {"reason": "rotated [redacted] manually"}
    assert secret not in json.dumps(event)
    assert secret not in caplog.text


# --- volume guard for auth failures ---------------------------------------


def _throttle_settings(limit: int) -> SimpleNamespace:
    """Settings stub with only what the guard reads."""
    return SimpleNamespace(audit_auth_failure_max_per_minute=limit)


def _reset_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitoring, "_audit_suppressed_auth_events", 0)
    monkeypatch.setattr(monitoring, "_audit_auth_suppression_windows", {})


async def _failed_logins(count: int, action: str = "auth.login_failed") -> None:
    for index in range(count):
        await main.record_audit(
            action=action,
            actor="mallory",
            message=f"Failed login attempt #{index} for username 'mallory'",
            outcome="failure" if action == "auth.login_failed" else "denied",
        )


async def test_auth_failure_guard_caps_stored_rows_per_actor(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_suppression(monkeypatch)
    monkeypatch.setattr(main, "get_settings", lambda: _throttle_settings(3))

    await _failed_logins(5)

    stored = await storage.list_audit_events(action="auth.login_failed")
    assert stored["total"] == 3
    assert monitoring._audit_suppressed_auth_events == 2

    with patch.object(
        monitoring, "check_ai_provider", AsyncMock(return_value={"status": "ok"})
    ):
        health = await monitoring.build_health_response(
            _health_settings(), str(audit_db)
        )
    assert health["checks"]["audit"]["suppressed_auth_events"] == 2


async def test_auth_failure_guard_counts_failed_and_blocked_together(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One budget for the actor, not one per action id."""
    _reset_suppression(monkeypatch)
    monkeypatch.setattr(main, "get_settings", lambda: _throttle_settings(2))

    await _failed_logins(2, "auth.login_failed")
    await _failed_logins(2, "auth.login_blocked")

    failed = await storage.list_audit_events(action="auth.login_failed")
    blocked = await storage.list_audit_events(action="auth.login_blocked")
    assert (failed["total"], blocked["total"]) == (2, 0)
    assert monitoring._audit_suppressed_auth_events == 2


async def test_auth_failure_guard_zero_disables_it(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_suppression(monkeypatch)
    monkeypatch.setattr(main, "get_settings", lambda: _throttle_settings(0))

    await _failed_logins(5)

    assert (await storage.list_audit_events(action="auth.login_failed"))["total"] == 5
    assert monitoring._audit_suppressed_auth_events == 0


async def test_volume_guard_never_throttles_other_actions(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_suppression(monkeypatch)
    monkeypatch.setattr(main, "get_settings", lambda: _throttle_settings(1))

    for index in range(4):
        await main.record_audit(
            action="user.job_delete", actor="mallory", message=f"delete {index}"
        )
        await main.record_audit(
            action="auth.login", actor="mallory", message=f"login {index}"
        )

    assert (await storage.list_audit_events(action="user.job_delete"))["total"] == 4
    assert (await storage.list_audit_events(action="auth.login"))["total"] == 4
    assert monitoring._audit_suppressed_auth_events == 0


async def test_suppressed_auth_failures_still_log_once_per_window(
    audit_db: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_suppression(monkeypatch)
    monkeypatch.setattr(main, "get_settings", lambda: _throttle_settings(1))
    caplog.set_level(logging.INFO)
    main_logger = _capture_main_logs(caplog)
    # The one-per-window summary is logged by the monitoring module.
    monitoring_logger = monitoring.logger
    monitoring_logger.setLevel(logging.INFO)
    monitoring_logger.addHandler(caplog.handler)
    try:
        await _failed_logins(4)
    finally:
        main_logger.removeHandler(caplog.handler)
        monitoring_logger.removeHandler(caplog.handler)

    # The existing per-event line survives for every attempt, stored or not.
    for index in range(4):
        assert f"Failed login attempt #{index}" in caplog.text
    summaries = [r for r in caplog.records if "volume guard" in r.getMessage()]
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.WARNING
    assert "beyond 1 per minute" in summaries[0].getMessage()
