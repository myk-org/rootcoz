"""Health monitoring, error rate tracking, and alerting for jenkins-job-insight.

Provides:
- Rolling-window error rate counters (thread-safe, in-memory)
- Health check logic with dependency checks
- Startup configuration validation
- Slack/email alerting with throttling
- Prometheus metrics exposition
"""

import asyncio
import os
import smtplib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httpx
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

_APP_STARTED_AT = time.monotonic()


def _get_app_version() -> str:
    """Return the application version string."""
    try:
        from importlib.metadata import version

        return version("jenkins-job-insight")
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Rolling-window error rate tracker
# ---------------------------------------------------------------------------


@dataclass
class _RollingCounter:
    """Thread-safe rolling-window counter backed by a deque of timestamps."""

    window_seconds: float = 300.0  # 5 minutes default
    _timestamps: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.monotonic()
        with self._lock:
            self._timestamps.append(ts)
            self._evict(ts)

    def count(self, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._evict(now)
            return len(self._timestamps)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


class _SingleWindowTracker:
    """In-memory rolling-window request / error counters for a single window.

    All public methods are thread-safe.
    """

    def __init__(self, window_seconds: float = 300.0) -> None:
        self.window_seconds = window_seconds
        self._total = _RollingCounter(window_seconds)
        self._errors: dict[
            str, _RollingCounter
        ] = {}  # keyed by status class "4xx"/"5xx"
        self._lock = threading.Lock()

    def record_request(self, status_code: int, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._total.record(now)
            if status_code >= 400:
                bucket = "5xx" if status_code >= 500 else "4xx"
                if bucket not in self._errors:
                    self._errors[bucket] = _RollingCounter(self.window_seconds)
                self._errors[bucket].record(now)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            total = self._total.count(now)
            error_counts: dict[str, int] = {}
            for bucket, counter in self._errors.items():
                error_counts[bucket] = counter.count(now)
        total_errors = sum(error_counts.values())
        error_rate = total_errors / total if total > 0 else 0.0
        return {
            "window_seconds": self.window_seconds,
            "total_requests": total,
            "error_counts": error_counts,
            "total_errors": total_errors,
            "error_rate": round(error_rate, 4),
        }


class ErrorRateTracker:
    """Dual rolling-window error rate tracker (5-minute and 1-hour).

    Records each request into both windows simultaneously.
    ``snapshot()`` returns the short (5m) window for backward compatibility.
    ``snapshot_all()`` returns both windows keyed as ``last_5m`` / ``last_1h``.
    """

    def __init__(
        self,
        window_seconds: float = 300.0,
        long_window_seconds: float = 3600.0,
    ) -> None:
        self._short = _SingleWindowTracker(window_seconds)
        self._long = _SingleWindowTracker(long_window_seconds)

    # Expose for backward compat (used by middleware alert logic)
    @property
    def window_seconds(self) -> float:
        return self._short.window_seconds

    def record_request(self, status_code: int) -> None:
        now = time.monotonic()
        self._short.record_request(status_code, now)
        self._long.record_request(status_code, now)

    def snapshot(self) -> dict[str, Any]:
        """Return the short-window snapshot (backward compatible)."""
        return self._short.snapshot()

    def snapshot_all(self) -> dict[str, dict[str, Any]]:
        """Return both rolling-window snapshots."""
        return {
            "last_5m": self._short.snapshot(),
            "last_1h": self._long.snapshot(),
        }


# Singleton tracker used by the middleware
error_tracker = ErrorRateTracker()


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


async def check_db(db_path: str) -> dict[str, str]:
    """Check database connectivity and writability."""
    import pathlib

    try:
        p = pathlib.Path(db_path)
        db_dir = p.parent

        # Check directory exists and is writable
        if not db_dir.is_dir():
            return {"status": "error", "detail": f"Directory does not exist: {db_dir}"}
        if not os.access(str(db_dir), os.W_OK):
            return {"status": "error", "detail": f"Directory is not writable: {db_dir}"}

        # Check file is writable if it already exists
        if p.exists() and not os.access(str(p), os.W_OK):
            return {
                "status": "error",
                "detail": f"Database file is not writable: {db_path}",
            }

        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            # Use BEGIN IMMEDIATE to verify write-lock can be acquired,
            # then rollback to avoid any side effects.
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("ROLLBACK")
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 — health check must return status, not raise
        return {"status": "error", "detail": str(exc)}


async def _check_http_service(
    url: str,
    *,
    verify_ssl: bool = True,
    headers: dict[str, str] | None = None,
    ok_below: int = 500,
) -> dict[str, str]:
    """Probe an HTTP service and return a health status dict.

    Args:
        url: URL to GET.
        verify_ssl: Whether to verify TLS certificates.
        headers: Optional request headers (e.g. auth).
        ok_below: Status codes below this value are considered healthy.

    Returns ``{"status": "ok"}``, ``{"status": "degraded", ...}``, or
    ``{"status": "error", ...}``.  Never raises.
    """
    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=5.0) as client:
            resp = await client.get(url, headers=headers or {})
            if resp.status_code < ok_below:
                return {"status": "ok"}
            return {"status": "degraded", "detail": f"HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001 — health check must return status, not raise
        return {"status": "error", "detail": str(exc)}


async def check_jenkins(settings: Any) -> dict[str, str]:
    """Check Jenkins reachability (lightweight HEAD/GET to base URL)."""
    if not settings.jenkins_url:
        return {"status": "not_configured"}
    return await _check_http_service(
        settings.jenkins_url, verify_ssl=settings.jenkins_ssl_verify
    )


async def check_ai_provider() -> dict[str, str]:
    """Check that an AI provider is configured."""
    provider = os.getenv("AI_PROVIDER", "")
    model = os.getenv("AI_MODEL", "")
    if provider and model:
        return {"status": "ok", "provider": provider, "model": model}
    missing = []
    if not provider:
        missing.append("AI_PROVIDER")
    if not model:
        missing.append("AI_MODEL")
    return {"status": "not_configured", "detail": f"Missing: {', '.join(missing)}"}


async def check_reportportal(settings: Any) -> dict[str, str]:
    """Check Report Portal configuration."""
    if not settings.reportportal_enabled:
        return {"status": "not_configured"}
    token = (
        settings.reportportal_api_token.get_secret_value()
        if settings.reportportal_api_token
        else ""
    )
    return await _check_http_service(
        f"{settings.reportportal_url.rstrip('/')}/api/v1/user",
        verify_ssl=settings.reportportal_verify_ssl,
        headers={"Authorization": f"Bearer {token}"},
        ok_below=400,
    )


async def build_health_response(settings: Any, db_path: str) -> dict[str, Any]:
    """Build full health response with checks and error rates.

    Returns a dict with:
    - status: "healthy", "degraded", or "unhealthy"
    - checks: results from individual dependency checks
    - error_rates: current rolling-window error statistics
    """
    checks: dict[str, dict] = {}

    # Run all checks concurrently
    _HEALTH_CHECK_TIMEOUT = 6.0
    try:
        db_check, jenkins_check, ai_check, rp_check = await asyncio.wait_for(
            asyncio.gather(
                check_db(db_path),
                check_jenkins(settings),
                check_ai_provider(),
                check_reportportal(settings),
                return_exceptions=True,
            ),
            timeout=_HEALTH_CHECK_TIMEOUT,
        )
    except TimeoutError:
        _timeout_result: dict = {"status": "error", "detail": "health check timed out"}
        db_check = jenkins_check = ai_check = rp_check = _timeout_result

    def _safe(result: Any) -> dict:
        if isinstance(result, Exception):
            return {"status": "error", "detail": str(result)}
        return result

    checks["database"] = _safe(db_check)
    checks["jenkins"] = _safe(jenkins_check)
    checks["ai_provider"] = _safe(ai_check)
    checks["reportportal"] = _safe(rp_check)

    # Determine overall status
    statuses = [c["status"] for c in checks.values()]
    if checks["database"]["status"] == "error":
        overall = "unhealthy"
    elif any(s == "error" for s in statuses):
        overall = "degraded"
    elif any(s == "degraded" for s in statuses):
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "uptime_seconds": round(time.monotonic() - _APP_STARTED_AT, 3),
        "version": _get_app_version(),
        "checks": checks,
        "error_rates": error_tracker.snapshot_all(),
    }


# ---------------------------------------------------------------------------
# Startup config validation
# ---------------------------------------------------------------------------


@dataclass
class _ConfigFinding:
    """A single startup configuration finding with severity."""

    severity: str  # "error" or "warning"
    message: str


@dataclass
class StartupConfigResult:
    """Structured result from startup configuration validation."""

    findings: list[_ConfigFinding]

    @property
    def errors(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "warning"]


def validate_startup_config() -> StartupConfigResult:
    """Validate configuration at startup and return structured results.

    Returns a StartupConfigResult with findings categorised by severity.
    - 'error': critical issues (e.g. missing DB directory)
    - 'warning': optional integrations not configured
    Does NOT raise — startup should continue even with errors.
    """
    findings: list[_ConfigFinding] = []

    # AI provider (optional — can be passed per-request)
    provider = os.getenv("AI_PROVIDER", "")
    model = os.getenv("AI_MODEL", "")
    if not provider:
        findings.append(
            _ConfigFinding(
                "warning",
                "AI_PROVIDER is not set. Analysis requests will require ai_provider in the request body.",
            )
        )
    if not model:
        findings.append(
            _ConfigFinding(
                "warning",
                "AI_MODEL is not set. Analysis requests will require ai_model in the request body.",
            )
        )

    # Database path (critical)
    db_path = os.getenv("DB_PATH", "/data/results.db")
    db_dir = os.path.dirname(db_path) or "."
    if db_dir and not os.path.isdir(db_dir):
        findings.append(
            _ConfigFinding(
                "error",
                f"DB_PATH directory does not exist: {db_dir}. "
                "The database will be created but the parent directory must exist.",
            )
        )

    # Encryption key (critical for production)
    if not os.getenv("JJI_ENCRYPTION_KEY"):
        findings.append(
            _ConfigFinding(
                "warning",
                "JJI_ENCRYPTION_KEY is not set. A file-based key will be auto-generated. "
                "Set this env var for production deployments.",
            )
        )

    # Slack webhook (optional integration)
    slack_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if slack_url and not slack_url.startswith("https://"):
        findings.append(
            _ConfigFinding(
                "warning",
                "SLACK_WEBHOOK_URL does not start with https://. "
                "Slack webhooks should use HTTPS.",
            )
        )

    # SMTP config partial check (optional integration)
    smtp_host = os.getenv("SMTP_HOST", "")
    alert_email_to = os.getenv("ALERT_EMAIL_TO", "")
    if smtp_host and not alert_email_to:
        findings.append(
            _ConfigFinding(
                "warning",
                "SMTP_HOST is set but ALERT_EMAIL_TO is not. Email alerts will not be sent.",
            )
        )
    if alert_email_to and not smtp_host:
        findings.append(
            _ConfigFinding(
                "warning",
                "ALERT_EMAIL_TO is set but SMTP_HOST is not. Email alerts will not be sent.",
            )
        )

    # Optional integration URLs
    if not os.getenv("JENKINS_URL", ""):
        findings.append(
            _ConfigFinding(
                "warning",
                "JENKINS_URL is not set. Jenkins-based analysis will require jenkins_url in the request body.",
            )
        )
    if not os.getenv("JIRA_URL", ""):
        findings.append(
            _ConfigFinding(
                "warning",
                "JIRA_URL is not set. Jira enrichment will be disabled unless provided per-request.",
            )
        )
    if not os.getenv("REPORTPORTAL_URL", ""):
        findings.append(
            _ConfigFinding(
                "warning",
                "REPORTPORTAL_URL is not set. Report Portal integration is disabled.",
            )
        )

    # Peer AI configs (optional)
    if not os.getenv("PEER_AI_CONFIGS", ""):
        findings.append(
            _ConfigFinding(
                "warning",
                "PEER_AI_CONFIGS is not set. Peer analysis will be disabled unless provided per-request.",
            )
        )

    # Admin key (optional but recommended)
    if not os.getenv("ADMIN_KEY", ""):
        findings.append(
            _ConfigFinding(
                "warning",
                "ADMIN_KEY is not set. Admin API access will require user-based admin authentication.",
            )
        )

    return StartupConfigResult(findings=findings)


# ---------------------------------------------------------------------------
# Alert throttling
# ---------------------------------------------------------------------------


class AlertThrottler:
    """Per-event-type cooldown to prevent alert storms.

    Tracks the last time an alert was sent for each event type and
    suppresses duplicates within the cooldown window.
    """

    def __init__(self, cooldown_seconds: float = 300.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_alert(self, event_type: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(event_type)
            if last is None or now - last >= self.cooldown_seconds:
                self._last_sent[event_type] = now
                return True
        return False

    def reset(self, event_type: str | None = None) -> None:
        with self._lock:
            if event_type:
                self._last_sent.pop(event_type, None)
            else:
                self._last_sent.clear()


# Singleton throttler
alert_throttler = AlertThrottler()


# ---------------------------------------------------------------------------
# Slack notifications
# ---------------------------------------------------------------------------


async def send_slack_alert(message: str, webhook_url: str | None = None) -> bool:
    """Send an alert to Slack via webhook.

    Args:
        message: Text message to send.
        webhook_url: Slack webhook URL. Falls back to SLACK_WEBHOOK_URL env var.

    Returns:
        True if sent successfully, False otherwise.
        Never raises — alerting failures are swallowed.
    """
    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"text": message})
            if resp.status_code == 200:
                logger.debug("Slack alert sent successfully")
                return True
            logger.warning("Slack webhook returned HTTP %d", resp.status_code)
            return False
    except Exception:  # noqa: BLE001 — alerting failures must never propagate
        logger.debug("Failed to send Slack alert", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------


def send_email_alert(
    subject: str,
    body: str,
    *,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
    smtp_from: str | None = None,
    alert_email_to: str | None = None,
) -> bool:
    """Send an email alert via SMTP.

    All parameters fall back to environment variables when not provided.
    Never raises — alerting failures are swallowed.

    Returns:
        True if sent successfully, False otherwise.
    """
    host = smtp_host or os.getenv("SMTP_HOST", "")
    if not host:
        return False
    to_addr = alert_email_to or os.getenv("ALERT_EMAIL_TO", "")
    if not to_addr:
        return False
    try:
        port = smtp_port or int(os.getenv("SMTP_PORT", "587"))
        user = smtp_user or os.getenv("SMTP_USER", "")
        password = smtp_password or os.getenv("SMTP_PASSWORD", "")
        from_addr = smtp_from or os.getenv("SMTP_FROM", user or f"jji@{host}")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if port == 587:
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        logger.debug("Email alert sent successfully")
        return True
    except Exception:  # noqa: BLE001 — alerting failures must never propagate
        logger.debug("Failed to send email alert", exc_info=True)
        return False


async def send_email_alert_async(
    subject: str,
    body: str,
    **kwargs: Any,
) -> bool:
    """Async wrapper around send_email_alert (runs in thread pool)."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: send_email_alert(subject, body, **kwargs)
        )
    except Exception:  # noqa: BLE001 — alerting failures must never propagate
        logger.debug("Failed to send async email alert", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Unified alert dispatch
# ---------------------------------------------------------------------------


async def dispatch_alert(
    event_type: str,
    message: str,
    *,
    subject: str | None = None,
) -> None:
    """Send alert via all configured channels, respecting throttling.

    Args:
        event_type: Event type key for throttling (e.g. "high_error_rate").
        message: Alert message body.
        subject: Email subject line (defaults to event_type).

    Never raises — all alerting failures are swallowed.
    """
    if not alert_throttler.should_alert(event_type):
        logger.debug("Alert throttled for event_type=%s", event_type)
        return
    try:
        email_subject = subject or f"[JJI Alert] {event_type}"
        await asyncio.gather(
            send_slack_alert(message),
            send_email_alert_async(email_subject, message),
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001 — alerting failures must never propagate
        logger.debug("Failed to dispatch alert", exc_info=True)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------


def render_prometheus_metrics(
    *,
    health_up: int | None = None,
    active_analyses: int | None = None,
) -> str:
    """Render metrics in Prometheus text exposition format.

    Args:
        health_up: 1 if healthy/degraded, 0 if unhealthy. When None the
            metric is omitted.
        active_analyses: Number of currently active (pending/running/waiting)
            analyses. When None the metric is omitted.

    Returns a string ready to serve from /metrics.
    """
    snap = error_tracker.snapshot()
    lines: list[str] = []

    lines.append("# HELP jji_requests_total Total requests in the rolling window.")
    lines.append("# TYPE jji_requests_total gauge")
    lines.append(f"jji_requests_total {snap['total_requests']}")

    lines.append("# HELP jji_errors_total Total errors in the rolling window.")
    lines.append("# TYPE jji_errors_total gauge")
    lines.append(f"jji_errors_total {snap['total_errors']}")

    lines.append("# HELP jji_error_rate Error rate in the rolling window (0-1).")
    lines.append("# TYPE jji_error_rate gauge")
    lines.append(f"jji_error_rate {snap['error_rate']}")

    lines.append(
        "# HELP jji_errors_by_class Errors by HTTP status class in the rolling window."
    )
    lines.append("# TYPE jji_errors_by_class gauge")
    for cls, count in sorted(snap["error_counts"].items()):
        lines.append(f'jji_errors_by_class{{status_class="{cls}"}} {count}')

    if health_up is not None:
        lines.append(
            "# HELP jji_health_up Whether the application is healthy (1) or unhealthy (0)."
        )
        lines.append("# TYPE jji_health_up gauge")
        lines.append(f"jji_health_up {health_up}")

    if active_analyses is not None:
        lines.append("# HELP jji_active_analyses Number of currently active analyses.")
        lines.append("# TYPE jji_active_analyses gauge")
        lines.append(f"jji_active_analyses {active_analyses}")

    lines.append("")
    return "\n".join(lines)
