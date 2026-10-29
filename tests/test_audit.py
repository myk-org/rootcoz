"""Tests for the structured audit trail (#379).

Two layers: the storage helpers, and ``main.record_audit`` — the single
emission point that writes the row, emits the ``[AUDIT]`` log line and keeps
secrets out of both.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from rootcoz import main, storage
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
            "UPDATE audit_events SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
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
            "UPDATE audit_events SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
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


async def test_record_audit_swallows_db_failure(
    audit_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
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
    assert main._audit_setting_details("ai_model") == {"key": "ai_model"}
    assert main._audit_setting_details("jenkins_password") == {
        "key": "jenkins_password",
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
            "UPDATE audit_events SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
            (old["id"],),
        )
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)
    monkeypatch.setattr(
        main, "get_settings", lambda: SimpleNamespace(audit_retention_days=90)
    )

    assert await main.prune_audit_events_periodically() == 1
    remaining = await storage.list_audit_events()
    assert [e["action"] for e in remaining["events"]] == ["action.new"]


async def test_prune_periodically_runs_at_most_daily(
    audit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await storage.insert_audit_event(action="action.keep", actor="alice")
    monkeypatch.setattr(main, "_last_audit_prune_at", 0.0)
    monkeypatch.setattr(
        main, "get_settings", lambda: SimpleNamespace(audit_retention_days=90)
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
        main, "get_settings", lambda: SimpleNamespace(audit_retention_days=0)
    )

    assert await main.prune_audit_events_periodically() == 0
    assert main._last_audit_prune_at == 0
    assert (await storage.list_audit_events())["total"] == 1


# --- migrated and new call sites -------------------------------------------
# --- migrated and new call sites -------------------------------------------


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


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
    assert details["keys"] == ["ai_model", "jenkins_password"]
    assert details["redacted_keys"] == ["jenkins_password"]
    assert _SECRET not in json.dumps(events[0])
    assert events[0]["target_type"] == "setting"


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
    assert events[0]["details"] == {"key": "ai_model"}


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
    assert len(details["sql_hash"]) == 12
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
            "UPDATE audit_events SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
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
