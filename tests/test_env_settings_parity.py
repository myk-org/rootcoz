"""Env ↔ Server Settings parity (#347): sidecar bridge, unblocked settings,
notifications, and session TTL reading Settings instead of raw env."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from rootcoz import main, monitoring, storage, transcripts
from rootcoz.config import (
    clear_db_settings_cache,
    get_settings,
    update_db_settings_cache,
)
from rootcoz.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "parity.db")
    monkeypatch.setenv("ADMIN_KEY", "parity-bootstrap-key")
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "parity.db"))
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()
    clear_db_settings_cache()


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    """Point the env bridge at a temp file."""
    path = tmp_path / "env"
    monkeypatch.setattr(transcripts, "ENV_BRIDGE_PATH", path)
    return path


def _admin_headers():
    return {"Authorization": "Bearer parity-bootstrap-key"}


# --- Bridge writer ---


def test_bridge_writes_only_overrides_and_quotes_values(bridge):
    transcripts.write_sidecar_env_bridge(
        {
            "SIDECAR_HOST": "127.0.0.1",
            "SIDECAR_PORT": None,
            "ACPX_AGENTS": "a b,c",  # value with a space must survive `source`
        }
    )
    content = bridge.read_text()
    assert "SIDECAR_HOST=127.0.0.1" in content
    assert "ACPX_AGENTS='a b,c'" in content
    assert "SIDECAR_PORT" not in content  # no override → no assignment


def test_bridge_empty_writes_comment_only(bridge):
    transcripts.write_sidecar_env_bridge({"SIDECAR_HOST": None})
    assert bridge.read_text() == "# no DB overrides for the sidecar\n"


def test_transcript_bridge_wrapper_still_works(bridge):
    transcripts.write_transcript_env_bridge(True)
    assert bridge.read_text() == "RECORD_TRANSCRIPTS=true\n"


# --- Save / reset flow ---


def test_save_sidecar_settings_writes_bridge_and_warns_on_non_loopback(client, bridge):
    response = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={
            "settings": {
                "sidecar_host": "0.0.0.0",
                "sidecar_port": "9200",
                "acpx_agents": "cursor",
            }
        },
    )
    assert response.status_code == 200
    warnings = response.json().get("warnings", [])
    assert any("sidecar_host" in w and "loopback" in w for w in warnings)
    content = bridge.read_text()
    assert "SIDECAR_HOST=0.0.0.0" in content
    assert "SIDECAR_PORT=9200" in content
    assert "ACPX_AGENTS=cursor" in content
    assert "RECORD_TRANSCRIPTS" not in content  # no override for it


def test_save_loopback_sidecar_host_has_no_warning(client, bridge):
    for host in ("127.0.0.1", "::1", "localhost"):
        response = client.put(
            "/api/admin/settings",
            headers=_admin_headers(),
            json={"settings": {"sidecar_host": host}},
        )
        assert response.status_code == 200
        assert not response.json().get("warnings")


def test_reset_sidecar_setting_clears_bridge_assignment(client, bridge):
    saved = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"sidecar_port": "9200"}},
    )
    assert saved.status_code == 200
    assert "SIDECAR_PORT=9200" in bridge.read_text()
    reset = client.delete("/api/admin/settings/sidecar_port", headers=_admin_headers())
    assert reset.status_code == 200
    assert "SIDECAR_PORT" not in bridge.read_text()


def test_sidecar_settings_metadata_category_and_restart(client):
    response = client.get("/api/admin/settings", headers=_admin_headers())
    assert response.status_code == 200
    by_key = {item["key"]: item for item in response.json()}
    for key in main._SIDECAR_BRIDGE_SETTINGS:
        assert by_key[key]["category"] == "Sidecar", key
        assert by_key[key]["restart_required"] is True, key
    assert by_key["sidecar_host"]["restart_required"] is True
    # Notifications category with a sensitive SMTP password.
    assert by_key["smtp_host"]["category"] == "Notifications"
    assert by_key["smtp_password"]["sensitive"] is True
    assert by_key["slack_webhook_url"]["sensitive"] is True
    assert by_key["session_ttl_hours"]["category"] == "Auth & Security"
    # Restart-required metadata for the previously blocked restart class.
    for key in (
        "default_user_role",
        "secure_cookies",
        "trust_proxy_headers",
        "log_level",
        "metadata_rules_file",
    ):
        assert by_key[key]["restart_required"] is True, key


def test_log_level_validation(client):
    bad = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"log_level": "LOUD"}},
    )
    assert bad.status_code == 400
    assert "log_level" in bad.json()["detail"]
    good = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"log_level": "DEBUG"}},
    )
    assert good.status_code == 200


def test_default_user_role_validation_on_save(client):
    bad = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"default_user_role": "superuser"}},
    )
    assert bad.status_code == 400
    good = client.put(
        "/api/admin/settings",
        headers=_admin_headers(),
        json={"settings": {"default_user_role": "operator"}},
    )
    assert good.status_code == 200


def test_apply_log_level_setting_levels_rootcoz_loggers():
    import logging

    update_db_settings_cache({"log_level": "ERROR"})
    try:
        main._apply_log_level_setting()
        assert logging.getLogger("rootcoz.storage").level == logging.ERROR
    finally:
        clear_db_settings_cache()
        logging.getLogger("rootcoz.storage").setLevel(logging.INFO)


# --- Notifications read Settings ---


def test_send_email_alert_reads_settings_over_env(monkeypatch):
    update_db_settings_cache(
        {
            "smtp_host": "smtp.example.com",
            "smtp_port": "2525",
            "alert_email_to": "oncall@example.com",
        }
    )
    try:
        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                captured["host"], captured["port"] = host, port

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def send_message(self, msg):
                captured["to"] = msg["To"]

        monkeypatch.setattr(monitoring.smtplib, "SMTP", FakeSMTP)
        assert monitoring.send_email_alert("subject", "body")
        assert captured == {
            "host": "smtp.example.com",
            "port": 2525,
            "to": "oncall@example.com",
        }
    finally:
        clear_db_settings_cache()


def test_send_email_alert_without_config_is_a_no_op():
    clear_db_settings_cache()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("SMTP_HOST", raising=False)
    try:
        assert not monitoring.send_email_alert("subject", "body")
    finally:
        monkeypatch.undo()


async def test_send_slack_alert_reads_settings(monkeypatch):
    update_db_settings_cache({"slack_webhook_url": "https://hooks.example.com/x"})
    try:
        sent = {}

        async def fake_post(self, url, json=None):
            sent["url"] = url
            return MagicMock(status_code=200)

        with patch.object(monitoring.httpx.AsyncClient, "post", fake_post):
            assert await monitoring.send_slack_alert("hello")
        assert sent["url"] == "https://hooks.example.com/x"
    finally:
        clear_db_settings_cache()


# --- Session TTL from Settings ---


async def test_session_ttl_follows_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "ttl.db")
    await storage.init_db()
    update_db_settings_cache({"session_ttl_hours": "2"})
    try:
        assert storage.current_session_ttl_hours() == 2
        assert storage.current_session_ttl_seconds() == 7200
        token = await storage.create_session("alice")
        session = await storage.get_session(token)
        expires = datetime.strptime(session["expires_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        remaining = expires - datetime.now(UTC)
        assert timedelta_hours(1.5) < remaining < timedelta_hours(2.5)
    finally:
        clear_db_settings_cache()


def timedelta_hours(hours: float):
    from datetime import timedelta

    return timedelta(hours=hours)


async def test_session_ttl_invalid_env_keeps_default(monkeypatch):
    monkeypatch.setenv("SESSION_TTL_HOURS", "not-a-number")
    get_settings.cache_clear()
    try:
        assert get_settings().session_ttl_hours == 720
    finally:
        monkeypatch.delenv("SESSION_TTL_HOURS")
        get_settings.cache_clear()


# --- Save-to-DB persist (env → DB promotion, #347 add-on) ---


def test_persist_promotes_env_value_to_db(client, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.parity.example.com")
    get_settings.cache_clear()
    before = {
        item["key"]: item
        for item in client.get("/api/admin/settings", headers=_admin_headers()).json()
    }
    assert before["smtp_host"]["source"] == "env"

    response = client.post(
        "/api/admin/settings/smtp_host/persist", headers=_admin_headers()
    )
    assert response.status_code == 200
    assert response.json() == {"key": "smtp_host", "source": "db"}

    after = {
        item["key"]: item
        for item in client.get("/api/admin/settings", headers=_admin_headers()).json()
    }
    assert after["smtp_host"]["source"] == "db"
    assert after["smtp_host"]["value"] == "smtp.parity.example.com"

    # Dropping the deployment env no longer reverts the setting — the DB
    # override (and the updated cache) keeps the persisted value.
    monkeypatch.delenv("SMTP_HOST")
    get_settings.cache_clear()
    assert get_settings().smtp_host == "smtp.parity.example.com"


async def test_persist_sensitive_key_stored_encrypted(client, monkeypatch):
    from rootcoz.encryption import decrypt_value

    secret = "https://hooks.example.com/secret-path"  # pragma: allowlist secret
    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret)
    response = client.post(
        "/api/admin/settings/slack_webhook_url/persist", headers=_admin_headers()
    )
    assert response.status_code == 200
    row = (await storage.get_server_settings())["slack_webhook_url"]
    assert row["value"] != secret  # stored encrypted, not plaintext
    assert decrypt_value(row["value"]) == secret


def test_persist_rejections(client, monkeypatch):
    # No env value set
    monkeypatch.delenv("SMTP_HOST", raising=False)
    missing = client.post(
        "/api/admin/settings/smtp_host/persist", headers=_admin_headers()
    )
    assert missing.status_code == 400
    assert "no env value" in missing.json()["detail"]
    # Unknown key
    unknown = client.post(
        "/api/admin/settings/not_a_setting/persist", headers=_admin_headers()
    )
    assert unknown.status_code == 400
    assert "Unknown setting" in unknown.json()["detail"]
    # Server-only key
    server_only = client.post(
        "/api/admin/settings/db_path/persist", headers=_admin_headers()
    )
    assert server_only.status_code == 400
