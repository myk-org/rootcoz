"""Tests for the monitoring module (health, error tracking, alerting, metrics)."""

import asyncio
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from rootcoz import monitoring
from rootcoz.encryption import strip_sensitive_from_response
from rootcoz.monitoring import (
    ERROR_RATE_MIN_REQUESTS,
    AlertThrottler,
    ErrorRateTracker,
    _RollingCounter,
    build_health_response,
    check_ai_provider,
    check_db,
    check_jenkins,
    check_reportportal,
    dispatch_alert,
    health_up_from_status,
    read_data_usage,
    render_prometheus_metrics,
    send_email_alert,
    send_slack_alert,
    server_error_share,
    validate_startup_config,
)
from tests.conftest import host_env

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
        env = host_env()
        return patch.dict(os.environ, env, clear=True)

    async def test_check_ai_provider_configured(self):
        with (
            patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}),
            self._mock_sidecar(),
        ):
            result = await check_ai_provider()
        assert result["status"] == "ok"
        assert result["provider"] == "claude"

    async def test_check_ai_provider_missing(self):
        from rootcoz.config import get_settings

        with self._env_without_ai():
            get_settings.cache_clear()
            try:
                with self._mock_sidecar():
                    result = await check_ai_provider()
            finally:
                get_settings.cache_clear()
        assert result["status"] == "not_configured"
        assert "AI_PROVIDER" in result["detail"]
        assert "AI_MODEL" in result["detail"]

    async def test_check_ai_provider_sidecar_unreachable_and_env_missing(self):
        from rootcoz.config import get_settings

        with self._env_without_ai():
            get_settings.cache_clear()
            try:
                with self._mock_sidecar(side_effect=OSError("Connection refused")):
                    result = await check_ai_provider()
            finally:
                get_settings.cache_clear()
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

    async def test_build_health_response_healthy(self, temp_db_path, monkeypatch):
        import aiosqlite

        from rootcoz import monitoring

        # The audit entry reads a process-wide counter: a failed audit write
        # anywhere earlier in this worker reports the response degraded, which
        # is exactly how this test flaked under `pytest -n auto`. Control it
        # here so the assertion never depends on what ran before.
        monkeypatch.setattr(monitoring, "_audit_write_failures", 0)

        async with aiosqlite.connect(str(temp_db_path)) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")

        settings = MagicMock()
        settings.jenkins_url = ""
        settings.reportportal_enabled = False

        ai_ok = {"status": "ok", "provider": "claude", "model": "test"}
        with (
            patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}),
            patch(
                "rootcoz.monitoring.check_ai_provider",
                new_callable=AsyncMock,
                return_value=ai_ok,
            ),
        ):
            result = await build_health_response(settings, str(temp_db_path))

        assert result["status"] == "healthy"
        assert "checks" in result
        assert "error_rates" in result
        assert result["checks"]["database"]["status"] == "ok"

    async def test_build_health_response_reports_undecodable_rows(
        self, temp_db_path, monkeypatch
    ):
        """Skipped rows show as a degraded-data count, never as a 503."""
        import aiosqlite

        from rootcoz import storage

        monkeypatch.setattr(monitoring, "_audit_write_failures", 0)

        async with aiosqlite.connect(str(temp_db_path)) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")

        settings = MagicMock()
        settings.jenkins_url = ""
        settings.reportportal_enabled = False
        ai_ok = {"status": "ok", "provider": "claude", "model": "test"}

        async def _health():
            with (
                patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}),
                patch(
                    "rootcoz.monitoring.check_ai_provider",
                    new_callable=AsyncMock,
                    return_value=ai_ok,
                ),
            ):
                return await build_health_response(settings, str(temp_db_path))

        monkeypatch.setattr(storage, "_undecodable_result_ids", set())
        clean = await _health()
        assert clean["checks"]["result_json"] == {
            "status": "ok",
            "undecodable_rows": 0,
        }

        monkeypatch.setattr(storage, "_undecodable_result_ids", {"job-a", "job-b"})
        degraded = await _health()
        assert degraded["checks"]["result_json"] == {
            "status": "degraded",
            "undecodable_rows": 2,
        }
        assert degraded["status"] == "degraded"

    async def test_build_health_response_unhealthy_db(self):
        settings = MagicMock()
        settings.jenkins_url = ""
        settings.reportportal_enabled = False

        ai_ok = {"status": "ok", "provider": "claude", "model": "test"}
        with (
            patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}),
            patch(
                "rootcoz.monitoring.check_ai_provider",
                new_callable=AsyncMock,
                return_value=ai_ok,
            ),
        ):
            result = await build_health_response(settings, "/nonexistent/db.sqlite")

        assert result["status"] == "unhealthy"
        assert result["checks"]["database"]["status"] == "error"

    async def test_build_health_response_unhealthy_sidecar(self, temp_db_path):
        """An unreachable sidecar is unhealthy (503), not degraded (#347 r3).

        Deployment startup probes point at /api/health precisely so they
        validate the sidecar via its effective port without depending on
        the listener port — that only works when a dead sidecar fails the
        endpoint.
        """
        import aiosqlite

        async with aiosqlite.connect(str(temp_db_path)) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")

        settings = MagicMock()
        settings.jenkins_url = ""
        settings.reportportal_enabled = False

        ai_error = {"status": "error", "detail": "Sidecar unreachable: refused"}
        with (
            patch.dict(os.environ, {"AI_PROVIDER": "claude", "AI_MODEL": "test"}),
            patch(
                "rootcoz.monitoring.check_ai_provider",
                new_callable=AsyncMock,
                return_value=ai_error,
            ),
        ):
            result = await build_health_response(settings, str(temp_db_path))

        assert result["status"] == "unhealthy"
        assert result["checks"]["ai_provider"]["status"] == "error"

    async def test_check_ai_provider_probes_effective_sidecar_url(self):
        """The sidecar probe follows SIDECAR_URL / SIDECAR_PORT, never a hardcoded port."""
        cases = [
            {"SIDECAR_URL": "http://127.0.0.1:9200", "SIDECAR_PORT": "9100"},
            {"SIDECAR_URL": "", "SIDECAR_PORT": "9200"},
            {"SIDECAR_URL": "", "SIDECAR_PORT": ""},
        ]
        expected = [
            "http://127.0.0.1:9200/health",
            "http://127.0.0.1:9200/health",
            "http://127.0.0.1:9100/health",
        ]
        for env, url in zip(cases, expected, strict=True):
            with (
                patch.dict(os.environ, env),
                patch(
                    "rootcoz.monitoring._check_http_service",
                    new_callable=AsyncMock,
                    return_value={"status": "ok"},
                ) as probe,
            ):
                await check_ai_provider()
            probe.assert_awaited_once_with(url, ok_below=300)


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
        from rootcoz.config import get_settings

        with patch.dict(os.environ, {}, clear=True):
            get_settings.cache_clear()
            try:
                result = validate_startup_config()
            finally:
                get_settings.cache_clear()
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

    async def test_dispatch_alert_bypassing_the_throttle(self, monkeypatch):
        """The monitor's state machine is its dedup; a cooldown would drop recoveries."""
        from rootcoz import monitoring

        monitoring.alert_throttler.reset()
        monkeypatch.setattr(monitoring.alert_throttler, "cooldown_seconds", 9999.0)
        mock_slack = AsyncMock()
        monkeypatch.setattr(monitoring, "send_slack_alert", mock_slack)
        monkeypatch.setattr(monitoring, "send_email_alert_async", AsyncMock())
        try:
            await dispatch_alert(
                "health_monitor_health_down", "firing", bypass_throttle=True
            )
            await dispatch_alert(
                "health_monitor_health_down", "recovered", bypass_throttle=True
            )
            assert mock_slack.call_count == 2

            # The default path inside the same cooldown still sends once.
            await dispatch_alert("high_error_rate", "first")
            await dispatch_alert("high_error_rate", "second")
            assert mock_slack.call_count == 3
        finally:
            monitoring.alert_throttler.reset()


class TestServerErrorShare:
    """One definition of "the server is erroring" (#464)."""

    def test_server_error_share(self):
        def snapshot(**counts: int) -> dict:
            total = sum(counts.values())
            return {
                "window_seconds": 300.0,
                "total_requests": total,
                "error_counts": {k: v for k, v in counts.items() if v},
                "total_errors": total,
                "error_rate": total / total if total else 0.0,
            }

        # 4xx is not a server fault: the share is 0 and the counts say so.
        assert server_error_share(
            {"total_requests": 20, "error_counts": {"4xx": 20}}
        ) == (
            0.0,
            0,
            20,
        )
        assert server_error_share(
            {"total_requests": 20, "error_counts": {"5xx": 4}}
        ) == (
            pytest.approx(0.2),
            4,
            20,
        )
        # An idle window has no share, and no ZeroDivisionError.
        assert server_error_share({"total_requests": 0, "error_counts": {}}) == (
            0.0,
            0,
            0,
        )
        assert ERROR_RATE_MIN_REQUESTS == 10


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

    def test_data_gauges_rendered_unlabelled(self):
        text = render_prometheus_metrics(
            data_used_bytes=2_400_000_000, data_capacity_bytes=6_442_450_944
        )
        assert "\nrootcoz_data_used_bytes 2400000000\n" in text
        assert "\nrootcoz_data_capacity_bytes 6442450944\n" in text
        assert "\nrootcoz_data_used_ratio 0.3725\n" in text
        # Cardinality is fixed at three series: no labels on any data gauge.
        data_sample_lines = [
            line for line in text.splitlines() if line.startswith("rootcoz_data_")
        ]
        assert len(data_sample_lines) == 3
        assert not any("{" in line for line in data_sample_lines)

    def test_zero_capacity_omits_the_ratio(self):
        text = render_prometheus_metrics(data_used_bytes=10, data_capacity_bytes=0)
        assert "rootcoz_data_used_bytes 10" in text
        assert "rootcoz_data_capacity_bytes 0" in text
        assert "rootcoz_data_used_ratio" not in text

    def test_the_age_gauge_renders_and_omits(self):
        """The age of the longest-running analysis: one number, never a 0."""
        text = render_prometheus_metrics(oldest_analysis_age_seconds=7500)
        assert "\nrootcoz_oldest_analysis_age_seconds 7500\n" in text
        assert "rootcoz_oldest_analysis_age_seconds" in text.split("\n", 1)[1]

        without = render_prometheus_metrics(oldest_analysis_age_seconds=None)
        assert "rootcoz_oldest_analysis_age_seconds" not in without

    def test_missing_data_values_omit_every_data_gauge(self):
        text = render_prometheus_metrics(
            health_up=1,
            active_analyses=0,
            audit_write_failures=0,
            data_used_bytes=None,
            data_capacity_bytes=None,
        )
        assert "rootcoz_data_" not in text
        for family in (
            "rootcoz_requests_total",
            "rootcoz_error_rate",
            "rootcoz_health_up",
            "rootcoz_active_analyses",
            "rootcoz_audit_write_failures",
        ):
            assert family in text


class TestReadDataUsage:
    """Tests for the statvfs probe behind the data gauges."""

    def test_probe_failure_returns_none(self):
        with patch("rootcoz.monitoring.shutil.disk_usage", side_effect=OSError("boom")):
            assert read_data_usage("/data/results.db") is None

    def test_reads_the_database_directory(self):
        stub = MagicMock(used=1000, total=4000)
        with patch(
            "rootcoz.monitoring.shutil.disk_usage", return_value=stub
        ) as disk_usage:
            assert read_data_usage("/data/results.db") == (1000, 4000)
        disk_usage.assert_called_once_with(Path("/data"))


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

    def test_api_health_omits_component_versions(self, test_client):
        """Exact component versions must not leak via the public endpoint."""
        response = test_client.get("/api/health")
        assert response.status_code == 200
        assert "components" not in response.json()

    def test_legacy_health_still_works(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_degraded_health_is_up(self, test_client) -> None:
        """Only ``unhealthy`` is down: ``degraded`` keeps /metrics and /api/health up."""
        from rootcoz import main as main_module

        assert health_up_from_status("degraded") == 1
        assert health_up_from_status("healthy") == 1
        assert health_up_from_status("unhealthy") == 0

        async def _unhealthy(_settings, _db_path):
            return {"status": "unhealthy", "checks": {}, "error_rates": {}}

        async def _degraded(_settings, _db_path):
            return {"status": "degraded", "checks": {}, "error_rates": {}}

        with patch.object(main_module, "build_health_response", _unhealthy):
            metrics = test_client.get("/metrics")
            health = test_client.get("/api/health")
        assert "\nrootcoz_health_up 0\n" in metrics.text
        assert health.status_code == 503

        with patch.object(main_module, "build_health_response", _degraded):
            metrics = test_client.get("/metrics")
            health = test_client.get("/api/health")
        assert "\nrootcoz_health_up 1\n" in metrics.text
        assert health.status_code == 200

    def test_metrics_endpoint(self, test_client):
        response = test_client.get("/metrics")
        assert response.status_code == 200
        assert "rootcoz_requests_total" in response.text
        assert "text/plain" in response.headers["content-type"]

    def test_a_raising_age_read_drops_only_the_age_family(self, test_client):
        """A failed age read must never read as "health down"."""

        async def _boom() -> str:
            raise OSError("database went away")

        with patch("rootcoz.storage.oldest_running_analysis_started_at", _boom):
            response = test_client.get("/metrics")

        assert response.status_code == 200
        assert "rootcoz_oldest_analysis_age_seconds" not in response.text
        assert "rootcoz_health_up 1" in response.text
        assert "rootcoz_active_analyses" in response.text

    def test_metrics_probe_failure_keeps_the_scrape_healthy(self, test_client):
        def raising_probe(db_path):
            raise OSError("boom")

        with patch("rootcoz.main.read_data_usage", raising_probe):
            response = test_client.get("/metrics")

        assert response.status_code == 200
        assert "rootcoz_data_" not in response.text
        assert "rootcoz_requests_total" in response.text
        assert "rootcoz_active_analyses" in response.text

    def test_metrics_probe_absent_value_keeps_the_scrape_healthy(self, test_client):
        with patch("rootcoz.main.read_data_usage", return_value=None):
            response = test_client.get("/metrics")

        assert response.status_code == 200
        assert "rootcoz_data_" not in response.text

    def test_metrics_probe_runs_outside_the_event_loop(self, test_client):
        observed: dict[str, bool] = {}

        def probe(db_path):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                observed["off_loop"] = True
                return (1000, 4000)
            observed["off_loop"] = False
            return (1000, 4000)

        with patch("rootcoz.main.read_data_usage", probe):
            response = test_client.get("/metrics")

        assert observed.get("off_loop") is True
        assert response.status_code == 200
        assert "rootcoz_data_used_ratio 0.2500" in response.text

    def test_stuck_probe_times_out_and_leaves_the_shared_pool_free(self, test_client):
        # Bound the wait: the probe runs on its own daemon thread, which is never
        # joined at exit, so release it instead of leaving a 5 s block behind.
        release = threading.Event()
        calls: list[str] = []

        def stuck_probe(db_path):
            calls.append("stuck")
            release.wait(5)
            return (1, 2)

        def healthy_probe(db_path):
            calls.append("healthy")
            return (1000, 4000)

        async def unrelated_to_thread():
            return await asyncio.to_thread(lambda: 42)

        try:
            with (
                patch("rootcoz.main._DATA_PROBE_TIMEOUT_SECONDS", 0.2),
                patch("rootcoz.main.read_data_usage", stuck_probe),
            ):
                started = time.monotonic()
                response = test_client.get("/metrics")
                elapsed = time.monotonic() - started

                assert response.status_code == 200
                assert "rootcoz_data_" not in response.text
                # The probe timeout, not the probe's own 5 s block, ended the scrape.
                assert elapsed < 1
                # The hung probe is parked on its own daemon thread; the shared default
                # pool still serves other asyncio.to_thread callers on the app loop.
                assert test_client.portal.call(unrelated_to_thread) == 42

                # Two more scrapes abandon their probes while the mount is still hung.
                for _ in range(2):
                    assert "rootcoz_data_" not in test_client.get("/metrics").text

                calls.clear()
                release.set()
                started = time.monotonic()
                with patch("rootcoz.main.read_data_usage", healthy_probe):
                    response = test_client.get("/metrics")
                elapsed = time.monotonic() - started

            # The mount is back: the next scrape reports real gauges promptly, and the
            # abandoned probes were overwritten in the mailbox instead of replayed.
            assert "rootcoz_data_used_ratio 0.2500" in response.text
            assert elapsed < 1
            assert calls == ["healthy"]
        finally:
            release.set()

    def test_overlapping_scrapes_answer_the_displaced_one(self, test_client):
        """Two overlapping scrapes: the displaced one returns at once with the gauges
        omitted, and the surviving one still reports them."""
        from rootcoz import main as main_module

        release = threading.Event()
        calls: list[str] = []
        results: dict[str, tuple[float, str]] = {}

        def probe(db_path):
            calls.append("probe")
            if len(calls) == 1:
                release.wait(5)
                return (1, 2)
            return (1000, 4000)

        def scrape(name):
            started = time.monotonic()
            body = test_client.get("/metrics").text
            results[name] = (time.monotonic() - started, body)

        try:
            with (
                patch("rootcoz.main._DATA_PROBE_TIMEOUT_SECONDS", 5.0),
                patch("rootcoz.main.read_data_usage", probe),
            ):
                # Park the worker on the first probe, so the next request stays queued.
                parked = threading.Thread(target=scrape, args=("parked",))
                parked.start()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and len(calls) < 1:
                    time.sleep(0.01)
                assert len(calls) == 1

                displaced = threading.Thread(target=scrape, args=("displaced",))
                displaced.start()
                deadline = time.monotonic() + 2
                while (
                    time.monotonic() < deadline and not main_module._data_probe_mailbox
                ):
                    time.sleep(0.01)
                assert main_module._data_probe_mailbox

                # The second scrape displaces the queued request; the displaced scrape
                # must return on that eviction instead of waiting out the timeout.
                survivor = threading.Thread(target=scrape, args=("survivor",))
                survivor.start()
                displaced.join(3)
                assert not displaced.is_alive()
                release.set()
                survivor.join(5)
                parked.join(5)

            displaced_elapsed, displaced_body = results["displaced"]
            survivor_elapsed, survivor_body = results["survivor"]
            assert displaced_elapsed < 1
            assert "rootcoz_data_" not in displaced_body
            assert "rootcoz_data_used_ratio 0.2500" in survivor_body
            assert survivor_elapsed < 1
            # The displaced request was never run: only the parked and survivor probes.
            assert len(calls) == 2
        finally:
            release.set()


# ---------------------------------------------------------------------------
# Admin component-versions endpoint
# ---------------------------------------------------------------------------


class TestComponentVersionsEndpoint:
    """Tests for GET /api/admin/component-versions (admin only)."""

    @pytest.fixture
    def client(self, temp_db_path):
        from rootcoz import storage

        env = {
            "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
            "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
            "SECURE_COOKIES": "false",
            "DB_PATH": str(temp_db_path),
            "REQUIRE_APPROVAL": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            from rootcoz.config import get_settings

            get_settings.cache_clear()
            try:
                with patch.object(storage, "DB_PATH", temp_db_path):
                    from starlette.testclient import TestClient

                    from rootcoz.main import app

                    with TestClient(app) as test_client:
                        yield test_client
            finally:
                get_settings.cache_clear()

    def _admin_login(self, client):
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert resp.status_code == 200
        return resp.cookies

    def test_admin_gets_component_versions(self, client):
        cookies = self._admin_login(client)
        with patch(
            "rootcoz.main.get_component_versions",
            new_callable=AsyncMock,
            return_value={"pi": "1.2.3", "node": None},
        ):
            resp = client.get("/api/admin/component-versions", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json() == {"components": {"pi": "1.2.3", "node": None}}

    def test_admin_component_versions_uses_response_stripping(self, client):
        cookies = self._admin_login(client)
        with (
            patch(
                "rootcoz.main.get_component_versions",
                new_callable=AsyncMock,
                return_value={"pi": "9.9.9"},
            ),
            patch(
                "rootcoz.main.strip_sensitive_from_response",
                wraps=strip_sensitive_from_response,
            ) as strip_fn,
        ):
            resp = client.get("/api/admin/component-versions", cookies=cookies)
        assert resp.status_code == 200
        strip_fn.assert_called_once_with({"components": {"pi": "9.9.9"}})
        assert resp.json() == {"components": {"pi": "9.9.9"}}

    def test_forbidden_without_auth(self, client):
        resp = client.get("/api/admin/component-versions")
        assert resp.status_code == 403

    def test_forbidden_for_non_admin(self, client):
        reg = client.post("/api/auth/register", json={"username": "viewer1"})
        assert reg.status_code == 200
        session = reg.cookies.get("rootcoz_session")
        assert session
        resp = client.get(
            "/api/admin/component-versions",
            cookies={"rootcoz_session": session},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Component version manifest parsing
# ---------------------------------------------------------------------------


def test_compute_component_versions_handles_non_object_manifests(tmp_path, monkeypatch):
    """A valid-but-non-object package.json reports None instead of raising."""
    from rootcoz import monitoring as monitoring_module

    node_modules = tmp_path / "node_modules"
    broken = node_modules / "@myk-org" / "pi-sidecar"
    broken.mkdir(parents=True)
    (broken / "package.json").write_text("null")
    good = node_modules / "@earendil-works" / "pi-coding-agent"
    good.mkdir(parents=True)
    (good / "package.json").write_text('{"version": "1.2.3"}')

    monkeypatch.setattr(monitoring_module, "_SIDECAR_DIR_CANDIDATES", (tmp_path,))
    monkeypatch.setattr(monitoring_module, "_component_versions_cache", None)

    versions = monitoring_module._compute_component_versions()

    assert versions["pi_sidecar"] is None
    assert versions["pi"] == "1.2.3"


async def test_the_instantaneous_trigger_is_untouched(monkeypatch) -> None:
    """The 50 %-instant 5xx trigger is not part of the monitor's ownership (#464).

    Nothing yields to the monitor: a 50 % burst in one window is a signal the
    10 %/10 min condition does not carry, so the trigger keeps dispatching while
    the monitor is enabled, with its own ``(5xx/total)`` wording.
    """
    from rootcoz import main as main_module

    async def _noop_app(
        scope, receive, send
    ) -> None:  # pragma: no cover - never called
        return None

    tracker = ErrorRateTracker(window_seconds=300.0)
    for _ in range(6):
        tracker.record_request(200)
    for _ in range(18):
        tracker.record_request(500)
    monkeypatch.setattr(main_module, "error_tracker", tracker)
    dispatched = AsyncMock()
    monkeypatch.setattr(main_module, "dispatch_alert", dispatched)

    env = {
        "ENABLE_HEALTH_MONITOR": "true",
        "JENKINS_USER": "u",
        "JENKINS_PASSWORD": "p",
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        try:
            assert get_settings().enable_health_monitor is True
            middleware = main_module.ErrorTrackingMiddleware(_noop_app)
            middleware._schedule_high_error_rate_alert()
            await asyncio.gather(*list(main_module._background_tasks))
        finally:
            get_settings.cache_clear()

    dispatched.assert_awaited_once()
    event_type, message = dispatched.await_args.args
    assert event_type == "high_error_rate"
    assert "75%" in message
    assert "(18/24 requests" in message, "the trigger's own 5xx/total wording survives"
