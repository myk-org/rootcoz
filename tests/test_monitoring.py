"""Tests for the monitoring module (health, error tracking, alerting, metrics)."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from rootcoz.monitoring import (
    AlertThrottler,
    ErrorRateTracker,
    _RollingCounter,
    build_health_response,
    check_ai_provider,
    check_db,
    check_jenkins,
    check_reportportal,
    dispatch_alert,
    render_prometheus_metrics,
    send_email_alert,
    send_slack_alert,
    validate_startup_config,
)

# ---------------------------------------------------------------------------
# Rolling counter
# ---------------------------------------------------------------------------


class TestRollingCounter:
    """Tests for _RollingCounter."""

    def test_empty_counter(self):
        counter = _RollingCounter(window_seconds=10.0)
        assert counter.count(now=100.0) == 0

    def test_record_and_count(self):
        counter = _RollingCounter(window_seconds=10.0)
        counter.record(ts=100.0)
        counter.record(ts=101.0)
        counter.record(ts=102.0)
        assert counter.count(now=105.0) == 3

    def test_eviction(self):
        counter = _RollingCounter(window_seconds=5.0)
        counter.record(ts=100.0)
        counter.record(ts=101.0)
        counter.record(ts=107.0)
        # At t=107, only ts=107 should survive (100 and 101 are > 5s old)
        assert counter.count(now=107.0) == 1

    def test_window_boundary(self):
        counter = _RollingCounter(window_seconds=5.0)
        counter.record(ts=100.0)
        # Exactly at boundary
        assert counter.count(now=105.0) == 1
        # Just past boundary
        assert counter.count(now=105.1) == 0


# ---------------------------------------------------------------------------
# Error rate tracker
# ---------------------------------------------------------------------------


class TestErrorRateTracker:
    """Tests for ErrorRateTracker."""

    def test_record_success(self):
        tracker = ErrorRateTracker(window_seconds=60.0)
        tracker.record_request(200)
        tracker.record_request(201)
        snap = tracker.snapshot()
        assert snap["total_requests"] == 2
        assert snap["total_errors"] == 0
        assert snap["error_rate"] == 0.0

    def test_record_client_error(self):
        tracker = ErrorRateTracker(window_seconds=60.0)
        tracker.record_request(200)
        tracker.record_request(404)
        snap = tracker.snapshot()
        assert snap["total_requests"] == 2
        assert snap["error_counts"].get("4xx", 0) == 1
        assert snap["total_errors"] == 1

    def test_record_server_error(self):
        tracker = ErrorRateTracker(window_seconds=60.0)
        tracker.record_request(500)
        tracker.record_request(502)
        snap = tracker.snapshot()
        assert snap["error_counts"].get("5xx", 0) == 2
        assert snap["total_errors"] == 2
        assert snap["error_rate"] == 1.0

    def test_error_rate_calculation(self):
        tracker = ErrorRateTracker(window_seconds=60.0)
        for _ in range(8):
            tracker.record_request(200)
        for _ in range(2):
            tracker.record_request(500)
        snap = tracker.snapshot()
        assert snap["error_rate"] == 0.2

    def test_snapshot_includes_window(self):
        tracker = ErrorRateTracker(window_seconds=120.0)
        snap = tracker.snapshot()
        assert snap["window_seconds"] == 120.0

    def test_options_excluded_from_tracking(self):
        tracker = ErrorRateTracker(window_seconds=60.0)
        tracker.record_request(200, method="OPTIONS")
        tracker.record_request(404, method="OPTIONS")
        snap = tracker.snapshot()
        assert snap["total_requests"] == 0
        assert snap["total_errors"] == 0

    def test_non_options_still_tracked(self):
        tracker = ErrorRateTracker(window_seconds=60.0)
        tracker.record_request(200, method="GET")
        tracker.record_request(500, method="POST")
        snap = tracker.snapshot()
        assert snap["total_requests"] == 2
        assert snap["total_errors"] == 1


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


class TestHealthChecks:
    """Tests for individual health check functions."""

    async def test_check_db_success(self, temp_db_path):
        import aiosqlite

        async with aiosqlite.connect(str(temp_db_path)) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")
        result = await check_db(str(temp_db_path))
        assert result["status"] == "ok"

    async def test_check_db_failure(self):
        result = await check_db("/nonexistent/path/db.sqlite")
        assert result["status"] == "error"
        assert "detail" in result

    async def test_check_jenkins_not_configured(self):
        settings = MagicMock()
        settings.jenkins_url = ""
        result = await check_jenkins(settings)
        assert result["status"] == "not_configured"

    async def test_check_jenkins_ok(self):
        settings = MagicMock()
        settings.jenkins_url = "https://jenkins.example.com"
        settings.jenkins_ssl_verify = False

        mock_resp = httpx.Response(200)

        async def mock_get(url, **kwargs):
            return mock_resp

        with patch("rootcoz.monitoring.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client
            result = await check_jenkins(settings)
        assert result["status"] == "ok"

    async def test_check_jenkins_error(self):
        settings = MagicMock()
        settings.jenkins_url = "https://jenkins.example.com"
        settings.jenkins_ssl_verify = False

        with patch("rootcoz.monitoring.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client
            result = await check_jenkins(settings)
        assert result["status"] == "error"

    @staticmethod
    def _mock_sidecar(*, status_code: int = 200, side_effect=None):
        """Return a context manager that patches httpx.AsyncClient for sidecar checks."""
        mock_client = patch("rootcoz.monitoring.httpx.AsyncClient")

        class _Ctx:
            def __enter__(self_ctx):
                cls_mock = mock_client.__enter__()
                if side_effect:
                    get_mock = AsyncMock(side_effect=side_effect)
                else:
                    resp = MagicMock(status_code=status_code)
                    get_mock = AsyncMock(return_value=resp)
                cls_mock.return_value.__aenter__ = AsyncMock(
                    return_value=MagicMock(get=get_mock)
                )
                cls_mock.return_value.__aexit__ = AsyncMock(return_value=False)
                return cls_mock

            def __exit__(self_ctx, *args):
                mock_client.__exit__(*args)

        return _Ctx()

    @staticmethod
    def _env_without_ai():
        """Return a patch.dict that removes AI_PROVIDER and AI_MODEL."""
        env = {
            k: v for k, v in os.environ.items() if k not in ("AI_PROVIDER", "AI_MODEL")
        }
        return patch.dict(os.environ, env, clear=True)

    async def test_check_ai_provider_configured(self):
        with patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}):
            with self._mock_sidecar():
                result = await check_ai_provider()
        assert result["status"] == "ok"
        assert result["provider"] == "claude"

    async def test_check_ai_provider_missing(self):
        with self._env_without_ai():
            with self._mock_sidecar():
                result = await check_ai_provider()
        assert result["status"] == "not_configured"
        assert "AI_PROVIDER" in result["detail"]
        assert "AI_MODEL" in result["detail"]

    async def test_check_ai_provider_sidecar_unreachable_and_env_missing(self):
        with self._env_without_ai():
            with self._mock_sidecar(side_effect=Exception("Connection refused")):
                result = await check_ai_provider()
        assert result["status"] == "error"
        assert "AI_PROVIDER" in result["detail"]
        assert "AI_MODEL" in result["detail"]
        assert "Sidecar unreachable" in result["detail"]

    async def test_check_reportportal_not_configured(self):
        settings = MagicMock()
        settings.reportportal_enabled = False
        result = await check_reportportal(settings)
        assert result["status"] == "not_configured"

    async def test_check_reportportal_missing_project(self):
        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.reportportal_project = None
        result = await check_reportportal(settings)
        assert result["status"] == "error"
        assert "REPORTPORTAL_PROJECT" in result["detail"]

    async def test_check_reportportal_uses_project_launch_url(self):
        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.reportportal_url = "https://rp.example.com"
        settings.reportportal_project = "my_project"
        settings.reportportal_verify_ssl = False
        settings.reportportal_api_token = MagicMock()
        settings.reportportal_api_token.get_secret_value.return_value = "tok"

        mock_resp = httpx.Response(200)
        captured_urls: list[str] = []

        async def mock_get(url, **kwargs):
            captured_urls.append(url)
            return mock_resp

        with patch("rootcoz.monitoring.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client
            result = await check_reportportal(settings)
        assert result["status"] == "ok"
        assert len(captured_urls) == 1
        called_url = captured_urls[0]
        assert "/api/v1/my_project/launch" in called_url
        assert "page.size=1" in called_url

    async def test_build_health_response_healthy(self, temp_db_path):
        import aiosqlite

        async with aiosqlite.connect(str(temp_db_path)) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")

        settings = MagicMock()
        settings.jenkins_url = ""
        settings.reportportal_enabled = False

        ai_ok = {"status": "ok", "provider": "claude", "model": "test"}
        with patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}):
            with patch(
                "rootcoz.monitoring.check_ai_provider",
                new_callable=AsyncMock,
                return_value=ai_ok,
            ):
                result = await build_health_response(settings, str(temp_db_path))

        assert result["status"] == "healthy"
        assert "checks" in result
        assert "error_rates" in result
        assert result["checks"]["database"]["status"] == "ok"

    async def test_build_health_response_unhealthy_db(self):
        settings = MagicMock()
        settings.jenkins_url = ""
        settings.reportportal_enabled = False

        ai_ok = {"status": "ok", "provider": "claude", "model": "test"}
        with patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}):
            with patch(
                "rootcoz.monitoring.check_ai_provider",
                new_callable=AsyncMock,
                return_value=ai_ok,
            ):
                result = await build_health_response(settings, "/nonexistent/db.sqlite")

        assert result["status"] == "unhealthy"
        assert result["checks"]["database"]["status"] == "error"


# ---------------------------------------------------------------------------
# Startup config validation
# ---------------------------------------------------------------------------


class TestStartupConfigValidation:
    """Tests for validate_startup_config."""

    def test_no_warnings_with_good_config(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        env = {
            "AI_PROVIDER": "claude",
            "AI_MODEL": "test-model",
            "ROOTCOZ_ENCRYPTION_KEY": "test-key",
            "DB_PATH": db_path,
            "JENKINS_URL": "https://jenkins.example.com",
            "JIRA_URL": "https://jira.example.com",
            "REPORTPORTAL_URL": "https://rp.example.com",
            "PEER_AI_CONFIGS": "claude:model1",
            "ADMIN_KEY": "test-admin-key",
        }
        with patch.dict(os.environ, env, clear=True):
            result = validate_startup_config()
        ai_warnings = [
            w for w in result.warnings if "AI_PROVIDER" in w or "AI_MODEL" in w
        ]
        assert len(ai_warnings) == 0
        assert len(result.errors) == 0

    def test_missing_ai_provider(self):
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k != "AI_PROVIDER"}
            with patch.dict(os.environ, env, clear=True):
                result = validate_startup_config()
        assert any("AI_PROVIDER" in w for w in result.warnings)

    def test_missing_encryption_key(self):
        with patch.dict(
            os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}, clear=True
        ):
            result = validate_startup_config()
        assert any("ROOTCOZ_ENCRYPTION_KEY" in w for w in result.warnings)

    def test_bad_slack_url(self):
        with patch.dict(
            os.environ,
            {
                "AI_PROVIDER": "claude",
                "AI_MODEL": "test",
                "SLACK_WEBHOOK_URL": "http://not-https",
            },
            clear=True,
        ):
            result = validate_startup_config()
        assert any("SLACK_WEBHOOK_URL" in w for w in result.warnings)

    def test_smtp_without_alert_to(self):
        with patch.dict(
            os.environ,
            {
                "AI_PROVIDER": "claude",
                "AI_MODEL": "test",
                "SMTP_HOST": "smtp.example.com",
            },
            clear=True,
        ):
            result = validate_startup_config()
        assert any("ALERT_EMAIL_TO" in w for w in result.warnings)

    def test_db_path_missing_dir_is_error(self):
        with patch.dict(
            os.environ,
            {
                "AI_PROVIDER": "claude",
                "AI_MODEL": "test",
                "DB_PATH": "/nonexistent/path/test.db",
            },
            clear=True,
        ):
            result = validate_startup_config()
        assert any("DB_PATH" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Alert throttling
# ---------------------------------------------------------------------------


class TestAlertThrottler:
    """Tests for AlertThrottler."""

    def test_first_alert_passes(self):
        throttler = AlertThrottler(cooldown_seconds=60.0)
        assert throttler.should_alert("test_event") is True

    def test_duplicate_throttled(self):
        throttler = AlertThrottler(cooldown_seconds=60.0)
        assert throttler.should_alert("test_event") is True
        assert throttler.should_alert("test_event") is False

    def test_different_events_not_throttled(self):
        throttler = AlertThrottler(cooldown_seconds=60.0)
        assert throttler.should_alert("event_a") is True
        assert throttler.should_alert("event_b") is True

    def test_reset_specific(self):
        throttler = AlertThrottler(cooldown_seconds=60.0)
        throttler.should_alert("event_a")
        throttler.reset("event_a")
        assert throttler.should_alert("event_a") is True

    def test_reset_all(self):
        throttler = AlertThrottler(cooldown_seconds=60.0)
        throttler.should_alert("event_a")
        throttler.should_alert("event_b")
        throttler.reset()
        assert throttler.should_alert("event_a") is True
        assert throttler.should_alert("event_b") is True


# ---------------------------------------------------------------------------
# Slack notifications
# ---------------------------------------------------------------------------


class TestSlackAlert:
    """Tests for send_slack_alert."""

    async def test_no_url_returns_false(self):
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k != "SLACK_WEBHOOK_URL"}
            with patch.dict(os.environ, env, clear=True):
                result = await send_slack_alert("test")
        assert result is False

    async def test_success(self):
        with patch("rootcoz.monitoring.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client
            result = await send_slack_alert(
                "test", webhook_url="https://hooks.slack.com/test"
            )
        assert result is True

    async def test_failure_swallowed(self):
        with patch("rootcoz.monitoring.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("network error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client
            result = await send_slack_alert(
                "test", webhook_url="https://hooks.slack.com/test"
            )
        assert result is False


# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------


class TestEmailAlert:
    """Tests for send_email_alert."""

    def test_no_host_returns_false(self):
        with patch.dict(os.environ, {}, clear=True):
            result = send_email_alert("subject", "body")
        assert result is False

    def test_no_recipient_returns_false(self):
        result = send_email_alert(
            "subject", "body", smtp_host="smtp.example.com", alert_email_to=""
        )
        assert result is False

    def test_smtp_failure_swallowed(self):
        with patch("rootcoz.monitoring.smtplib.SMTP") as mock_smtp:
            mock_smtp.side_effect = Exception("SMTP error")
            result = send_email_alert(
                "subject",
                "body",
                smtp_host="smtp.example.com",
                alert_email_to="admin@example.com",
            )
        assert result is False


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------


class TestDispatchAlert:
    """Tests for dispatch_alert."""

    async def test_dispatch_sends_to_all_channels(self):
        from rootcoz import monitoring

        monitoring.alert_throttler.reset()
        original_slack = monitoring.send_slack_alert
        original_email = monitoring.send_email_alert_async
        mock_slack = AsyncMock(return_value=True)
        mock_email = AsyncMock(return_value=True)
        monitoring.send_slack_alert = mock_slack
        monitoring.send_email_alert_async = mock_email
        try:
            await dispatch_alert("test_dispatch_event", "test message")
            mock_slack.assert_called_once_with("test message")
            mock_email.assert_called_once()
        finally:
            monitoring.send_slack_alert = original_slack
            monitoring.send_email_alert_async = original_email
            monitoring.alert_throttler.reset()

    async def test_dispatch_throttled(self, monkeypatch):
        from rootcoz import monitoring

        monitoring.alert_throttler.reset()
        monkeypatch.setattr(monitoring.alert_throttler, "cooldown_seconds", 9999.0)
        mock_slack = AsyncMock()
        monkeypatch.setattr(monitoring, "send_slack_alert", mock_slack)
        monkeypatch.setattr(monitoring, "send_email_alert_async", AsyncMock())
        try:
            await dispatch_alert("test_throttle_direct", "first")
            await dispatch_alert("test_throttle_direct", "second")
            mock_slack.assert_called_once()
        finally:
            monitoring.alert_throttler.reset()


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------


class TestPrometheusMetrics:
    """Tests for render_prometheus_metrics."""

    def test_basic_output(self):
        text = render_prometheus_metrics()
        assert "rootcoz_requests_total" in text
        assert "rootcoz_errors_total" in text
        assert "rootcoz_error_rate" in text

    def test_format_with_errors(self):
        tracker = ErrorRateTracker(window_seconds=60.0)
        tracker.record_request(200)
        tracker.record_request(500)
        with patch("rootcoz.monitoring.error_tracker", tracker):
            text = render_prometheus_metrics()
        assert "rootcoz_errors_by_class" in text
        assert "5xx" in text


# ---------------------------------------------------------------------------
# Integration: health endpoint via test client
# ---------------------------------------------------------------------------


class TestHealthEndpointIntegration:
    """Tests for /api/health and /metrics endpoints via the FastAPI test client."""

    @pytest.fixture
    def test_client(self, temp_db_path):
        from unittest.mock import patch as mock_patch

        from rootcoz import storage

        env = {
            "JENKINS_USER": "testuser",
            "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
            "GEMINI_API_KEY": "test-key",  # pragma: allowlist secret
            "DB_PATH": str(temp_db_path),
        }
        with mock_patch.dict(os.environ, env, clear=True):
            from rootcoz.config import get_settings

            get_settings.cache_clear()
            try:
                with (
                    mock_patch.object(storage, "DB_PATH", temp_db_path),
                    mock_patch(
                        "rootcoz.monitoring.check_jenkins",
                        new_callable=AsyncMock,
                        return_value={"status": "not_configured"},
                    ),
                    mock_patch(
                        "rootcoz.monitoring.check_ai_provider",
                        new_callable=AsyncMock,
                        return_value={
                            "status": "ok",
                            "provider": "claude",
                            "model": "test",
                        },
                    ),
                ):
                    from starlette.testclient import TestClient

                    from rootcoz.main import app

                    with TestClient(app) as client:
                        yield client
            finally:
                get_settings.cache_clear()

    def test_api_health_returns_status(self, test_client):
        response = test_client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ("healthy", "degraded", "unhealthy")
        assert "checks" in data
        assert "error_rates" in data
        assert "database" in data["checks"]

    def test_legacy_health_still_works(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_metrics_endpoint(self, test_client):
        response = test_client.get("/metrics")
        assert response.status_code == 200
        assert "rootcoz_requests_total" in response.text
        assert "text/plain" in response.headers["content-type"]
