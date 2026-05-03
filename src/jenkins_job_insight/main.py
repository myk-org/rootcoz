import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
import math
import os
import time as _time
import urllib.parse
import uuid
from collections import defaultdict
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Coroutine, Literal
from xml.etree.ElementTree import ParseError

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field, SecretStr, ValidationError
from simple_logger.logger import get_logger

from jenkins_job_insight.logging_context import JobIdFilter, job_id_var
from jenkins_job_insight.ai_models import model_cache
from jenkins_job_insight.llm_pricing import pricing_cache

from ai_cli_runner import VALID_AI_PROVIDERS, run_parallel_with_limit
from jenkins_job_insight.analyzer import (
    JOB_INSIGHT_ISSUE_PROMPT_FILENAME,
    clone_additional_repos,
    resolve_additional_repos,
    analyze_failure_group,
    analyze_job,
    format_exception_with_type,
    get_failure_signature,
)
from jenkins_job_insight.config import (
    Settings,
    get_settings,
    parse_peer_configs,
    parse_repo_ref,
)
from jenkins_job_insight.encryption import (
    SENSITIVE_KEYS,
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
)
from jenkins_job_insight.github_issues import enrich_with_tests_repo_matches
from jenkins_job_insight.jira import enrich_with_jira_matches
from jenkins_job_insight.token_tracking import build_token_usage_summary
from jenkins_job_insight.monitoring import (
    build_health_response,
    dispatch_alert,
    error_tracker,
    render_prometheus_metrics,
    validate_startup_config,
)
from jenkins_job_insight.reportportal import AmbiguousLaunchError, ReportPortalClient
from jenkins_job_insight.bug_creation import (
    _parse_github_repo_url,
    create_github_issue,
    create_jira_bug,
    generate_github_issue_content,
    generate_jira_bug_content,
    search_github_duplicates,
    search_jira_duplicates,
)
from jenkins_job_insight.feedback import (
    create_feedback_from_preview,
    generate_feedback_preview,
)
from jenkins_job_insight.comment_enrichment import detect_mentions
from jenkins_job_insight.notifications import send_mention_notifications
from jenkins_job_insight.vapid import get_vapid_config
from jenkins_job_insight.models import (
    AddCommentRequest,
    AnalyzeCommentRequest,
    AnalyzeCommentResponse,
    AnalyzeFailuresRequest,
    AnalyzeRequest,
    BaseAnalysisRequest,
    BulkDeleteRequest,
    BulkJobMetadataRequest,
    ChildJobAnalysis,
    ClassifyTestRequest,
    CreateIssueRequest,
    FailureAnalysis,
    FailureAnalysisResult,
    FeedbackCreateRequest,
    FeedbackPreviewResponse,
    FeedbackRequest,
    FeedbackResponse,
    JobMetadataInput,
    OverrideClassificationRequest,
    PreviewIssueRequest,
    PushSubscriptionRequest,
    ReportPortalPushResult,
    SetReviewedRequest,
    UnsubscribeRequest,
)
from jenkins_job_insight.utils import (
    _is_sensitive_key,
    mask_sensitive_fields,
)
from jenkins_job_insight.xml_enrichment import (
    build_enriched_xml,
    extract_test_failures,
)
from jenkins_job_insight.repository import (
    RepositoryManager,
    _redact_url,
    derive_test_repo_name,
)
from jenkins_job_insight.request_resolution import resolve_tests_repo_token
from jenkins_job_insight import storage
from jenkins_job_insight.storage import (
    get_ai_configs,
    get_effective_classification,
    get_history_classification,
    get_result,
    init_db,
    list_results,
    list_results_for_dashboard,
    patch_result_json,
    populate_failure_history,
    save_result,
    update_progress_phase,
    update_status,
)

# Inline favicon


# Semaphore to limit concurrent track_user tasks
_track_user_semaphore = asyncio.Semaphore(10)


async def _safe_track_user(username: str) -> None:
    """Track user activity with bounded concurrency, swallowing any errors."""
    try:
        async with _track_user_semaphore:
            await storage.track_user(username)
    except Exception:
        logger.debug("Failed to track user activity for %s", username, exc_info=True)


FAVICON_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y="0.9em" font-size="90">\xf0\x9f\x94\x8d</text></svg>'

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

# Install job_id filter on ALL logger handlers so module loggers
# (which use propagate=False via python-simple-logger) get the prefix.
_job_id_filter = JobIdFilter()


def _install_job_id_filter() -> None:
    """Attach JobIdFilter to every handler on every known logger."""
    for name in [None, *list(logging.Logger.manager.loggerDict)]:
        _logger = logging.getLogger(name)
        for handler in getattr(_logger, "handlers", []):
            if _job_id_filter not in handler.filters:
                handler.addFilter(_job_id_filter)


_install_job_id_filter()


async def _attach_token_usage(job_id: str, result_data: dict) -> None:
    """Attach token usage summary to result data. Best-effort \u2014 never raises."""
    try:
        token_summary = await build_token_usage_summary(job_id)
        if token_summary:
            result_data["token_usage"] = token_summary.model_dump(mode="json")
    except Exception:  # noqa: BLE001 — best-effort token tracking must never fail the job
        logger.debug("Failed to attach token usage for job %s", job_id, exc_info=True)


async def _bind_job_id(job_id: str) -> None:
    """FastAPI dependency that binds job_id to the logging context."""
    job_id_var.set(job_id)


# Statuses that indicate the analysis is still in progress.
IN_PROGRESS_STATUSES = ("pending", "running", "waiting")

AI_PROVIDER = os.getenv("AI_PROVIDER", "").lower()
AI_MODEL = os.getenv("AI_MODEL", "")

_VALID_GROUP_BY = frozenset(
    {"provider", "model", "call_type", "day", "week", "month", "job"}
)


def _read_app_port() -> int:
    """Parse and validate the PORT environment variable.

    Returns:
        The validated integer port number.

    Raises:
        SystemExit: If PORT is not a valid integer or is out of range.
    """
    raw_port = os.environ.get("PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid PORT environment variable: {raw_port!r}. Must be an integer."
        ) from exc
    if not 1 <= port <= 65535:
        raise SystemExit(
            f"Invalid PORT environment variable: {raw_port!r}. Must be between 1 and 65535."
        )
    return port


# APP_PORT is the single source of truth for the server port.
# Used by both uvicorn bind (run()) and internal AI self-calls (_build_internal_server_url()).
# If overriding, set the PORT env var — the Dockerfile's --port should match.
APP_PORT = _read_app_port()


def _build_internal_server_url() -> str:
    """Build the internal server URL for AI tool access."""
    url = f"http://localhost:{APP_PORT}"
    logger.debug(f"Built internal server_url={url} for AI tool access")
    return url


def build_jenkins_url(base_url: str, job_name: str, build_number: int) -> str:
    """Construct full Jenkins build URL from job name and build number.

    Args:
        base_url: Base Jenkins URL from settings.
        job_name: Job name (can include folders like "folder/job-name").
        build_number: Build number.

    Returns:
        Full Jenkins build URL.
    """
    # Handle folder-style job names by URL-encoding each segment and joining with '/job/'
    segments = job_name.split("/")
    encoded_segments = [urllib.parse.quote(segment, safe="") for segment in segments]
    job_path = "/job/".join(encoded_segments)
    return f"{base_url.rstrip('/')}/job/{job_path}/{build_number}/"


def _extract_base_url() -> str:
    """Extract the external base URL for building public-facing links.

    When ``PUBLIC_BASE_URL`` is set, it is used directly as the trusted
    origin.  Otherwise the function returns an empty string so that
    callers produce relative URLs, avoiding host-header injection.

    Returns:
        Base URL without trailing slash (e.g. "https://example.com"),
        or an empty string when no trusted origin is configured.
    """
    settings = get_settings()
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")

    logger.debug(
        "PUBLIC_BASE_URL is not set; returning empty base URL (relative paths)"
    )
    return ""


def _build_report_context(
    include_links: bool,
    base_url: str,
    job_id: str,
    result_data: dict,
) -> tuple[str, str]:
    """Build report URL and Jenkins URL for bug preview endpoints.

    When ``include_links`` is *True* the returned URLs are fully-qualified
    hyperlinks.  Otherwise plain-text identifiers are returned so that
    previews remain useful without clickable links.

    Args:
        include_links: Whether to produce full hyperlinks.
        base_url: The external base URL of the service.
        job_id: The stored job identifier.
        result_data: Raw result dict from storage (contains jenkins_url, job_name, etc.).

    Returns:
        A ``(report_url, jenkins_url)`` tuple.
    """
    jenkins_url = result_data.get("jenkins_url", "")

    if include_links and base_url:
        report_url = f"{base_url}/results/{job_id}"
    else:
        report_url = f"/results/{job_id}"
        job_name = result_data.get("job_name", "")
        build_number = result_data.get("build_number", 0)
        jenkins_url = f"{job_name} #{build_number}" if job_name else ""

    return report_url, jenkins_url


def _attach_result_links(payload: dict, base_url: str, job_id: str) -> dict:
    """Attach ``base_url`` and ``result_url`` to a response payload."""
    payload["base_url"] = base_url
    result_url = f"{base_url}/results/{job_id}"
    payload["result_url"] = result_url
    return payload


def _recompose_repo_spec(url: str, ref: str) -> str:
    """Recompose 'url:ref' from stored components. Returns url alone when ref is empty."""
    if not url:
        return ""
    return f"{url}:{ref}" if ref else url


def _is_encrypted_value(value: Any) -> bool:
    """Return True if *value* looks like an undecrypted encrypted field."""
    return isinstance(value, str) and value.startswith("enc:")


def _reconstruct_from_params(
    result_data: dict,
) -> tuple[AnalyzeRequest, Settings]:
    """Reconstruct an AnalyzeRequest and Settings from stored request_params.

    Args:
        result_data: Stored result dict containing ``job_name``,
            ``build_number``, and ``request_params``.

    Returns:
        Tuple of (AnalyzeRequest, Settings).
    """
    params = decrypt_sensitive_fields(result_data["request_params"])
    # Fail fast if any sensitive field is still encrypted (key changed / corrupt)
    for _key in SENSITIVE_KEYS:
        _val = params.get(_key)
        if _is_encrypted_value(_val):
            raise ValueError(
                f"Cannot resume waiting job: stored {_key} could not be decrypted"
            )
    for _repo in params.get("additional_repos") or []:
        if isinstance(_repo, dict):
            _token = _repo.get("token")
            if _is_encrypted_value(_token):
                raise ValueError(
                    "Cannot resume waiting job: stored additional_repos token could not be decrypted"
                )
    body = AnalyzeRequest(
        job_name=result_data["job_name"],
        build_number=result_data["build_number"],
        ai_provider=params.get("ai_provider", ""),
        ai_model=params.get("ai_model", ""),
        wait_for_completion=params.get("wait_for_completion", True),
        poll_interval_minutes=params.get("poll_interval_minutes", 2),
        max_wait_minutes=params.get("max_wait_minutes", 0),
        enable_jira=params.get("enable_jira"),
        raw_prompt=params.get("raw_prompt") or None,
        tests_repo_url=_recompose_repo_spec(
            params.get("tests_repo_url", ""), params.get("tests_repo_ref", "")
        )
        or None,
        peer_ai_configs=(
            params["peer_ai_configs"] if "peer_ai_configs" in params else []
        ),
        peer_analysis_max_rounds=params.get("peer_analysis_max_rounds", 3),
        additional_repos=(
            params["additional_repos"] if "additional_repos" in params else None
        ),
        tests_repo_token=(
            params["tests_repo_token"] if "tests_repo_token" in params else None
        ),
        **({"force": params["force"]} if "force" in params else {}),
    )
    # Build Settings from env defaults, then layer stored overrides
    base_settings = get_settings()
    overrides: dict = {}
    settings_fields = [
        "jenkins_url",
        "jenkins_user",
        "jenkins_password",
        "jenkins_ssl_verify",
        "jenkins_timeout",
        "wait_for_completion",
        "poll_interval_minutes",
        "max_wait_minutes",
        "jira_url",
        "jira_email",
        "jira_project_key",
        "jira_ssl_verify",
        "jira_max_results",
        "ai_cli_timeout",
        "max_concurrent_ai_calls",
        "jenkins_artifacts_max_size_mb",
        "get_job_artifacts",
        "peer_analysis_max_rounds",
        "force_analysis",
    ]
    for field in settings_fields:
        if field in params:
            overrides[field] = params[field]

    # Map stored 'force' flag to Settings.force_analysis
    if "force" in params:
        overrides["force_analysis"] = params["force"]

    # Tests repo URL
    recomposed = _recompose_repo_spec(
        params.get("tests_repo_url", ""), params.get("tests_repo_ref", "")
    )
    if recomposed:
        overrides["tests_repo_url"] = recomposed

    # SecretStr fields
    if params.get("jira_api_token"):
        overrides["jira_api_token"] = SecretStr(params["jira_api_token"])
    if params.get("jira_pat"):
        overrides["jira_pat"] = SecretStr(params["jira_pat"])
    if params.get("github_token"):
        overrides["github_token"] = SecretStr(params["github_token"])
    if "tests_repo_token" in params:
        token_value = params["tests_repo_token"]
        overrides["tests_repo_token"] = (
            SecretStr(token_value) if token_value is not None else None
        )

    # Enable jira
    if params.get("enable_jira") is not None:
        overrides["enable_jira"] = params["enable_jira"]

    if overrides:
        merged_data = base_settings.model_dump(mode="python") | overrides
        merged = Settings.model_validate(merged_data)
    else:
        merged = base_settings

    return body, merged


_background_tasks: set[asyncio.Task] = set()


async def _preserve_request_params(job_id: str, result_data: dict) -> None:
    """Copy ``request_params`` from the stored result into *result_data*.

    The initial ``save_result`` persists ``request_params`` (ai_provider,
    ai_model, peer_ai_configs, etc.) but the ``AnalysisResult`` model dump
    produced when analysis finishes does not include that key.  Without this
    merge the params would be silently lost when ``update_status`` overwrites
    ``result_json``.

    Args:
        job_id: The analysis job identifier.
        result_data: Mutable dict that will be written to ``result_json``.
            Modified in place to add ``request_params`` when available.
    """
    stored = await get_result(job_id, strip_sensitive=False)
    if stored and stored.get("result") and "request_params" in stored["result"]:
        result_data["request_params"] = stored["result"]["request_params"]


async def _fail_resumed_waiting_job(job_id: str, result_data: dict, error: str) -> None:
    """Mark a resumed waiting job as failed with a standard payload.

    Args:
        job_id: The job identifier.
        result_data: The stored result data dict for the job.
        error: Human-readable error message.
    """
    fail_data = {
        "job_name": result_data.get("job_name", ""),
        "build_number": result_data.get("build_number", 0),
        "error": error,
    }
    if "request_params" in result_data:
        fail_data["request_params"] = result_data["request_params"]
    await storage.update_status(job_id, "failed", fail_data)


async def _resume_waiting_jobs(waiting_jobs: list[dict]) -> None:
    """Resume waiting jobs by re-creating their background tasks.

    Args:
        waiting_jobs: List of dicts with ``job_id`` and ``result_data``
            returned by ``mark_stale_results_failed``.
    """
    for job in waiting_jobs:
        result_data = job["result_data"]
        params = result_data.get("request_params")
        if not params:
            logger.warning(
                f"Waiting job {job['job_id']} has no request_params, marking as failed"
            )
            await _fail_resumed_waiting_job(
                job["job_id"],
                result_data,
                "Cannot resume: no request_params stored (queued before resume support)",
            )
            continue

        try:
            body, merged = _reconstruct_from_params(result_data)
        except Exception as exc:
            logger.warning(
                f"Failed to reconstruct params for waiting job {job['job_id']}: {exc}"
            )
            await _fail_resumed_waiting_job(
                job["job_id"],
                result_data,
                f"Cannot resume: failed to reconstruct request params: {exc}",
            )
            continue

        # Adjust max_wait_minutes to account for time already elapsed before
        # the restart, so the original deadline is honoured.
        raw_wait_started_at = params.get("wait_started_at")
        wait_started_at: float | None = None
        if raw_wait_started_at is not None:
            try:
                wait_started_at = float(raw_wait_started_at)
            except (TypeError, ValueError):
                await _fail_resumed_waiting_job(
                    job["job_id"],
                    result_data,
                    f"Cannot resume: malformed wait_started_at value: {raw_wait_started_at!r}",
                )
                continue
            if not math.isfinite(wait_started_at):
                await _fail_resumed_waiting_job(
                    job["job_id"],
                    result_data,
                    f"Cannot resume: non-finite wait_started_at value: {raw_wait_started_at!r}",
                )
                continue
        if merged.max_wait_minutes > 0 and wait_started_at is not None:
            elapsed_minutes = (_time.time() - wait_started_at) / 60
            remaining = merged.max_wait_minutes - elapsed_minutes
            if remaining <= 0:
                await _fail_resumed_waiting_job(
                    job["job_id"],
                    result_data,
                    (
                        f"Timed out waiting for Jenkins job "
                        f"{result_data.get('job_name')} #{result_data.get('build_number')} "
                        f"after {merged.max_wait_minutes} minutes (deadline passed during restart)"
                    ),
                )
                continue
            merged_data = merged.model_dump(mode="python")
            merged_data["max_wait_minutes"] = max(1, math.ceil(remaining))
            merged = Settings.model_validate(merged_data)

        task = asyncio.create_task(
            process_analysis_with_id(job["job_id"], body, merged)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        logger.info(
            f"Resumed waiting job {job['job_id']} "
            f"({result_data.get('job_name')} #{result_data.get('build_number')})"
        )


async def _safe_preload_cursor_models() -> None:
    """Pre-populate cursor model cache in background. Best-effort."""
    try:
        await model_cache.list_models("cursor")
    except Exception:
        logger.debug("Failed to preload cursor models", exc_info=True)


async def _deferred_resume_waiting_jobs(waiting_jobs: list[dict]) -> None:
    """Resume waiting jobs after startup is complete.

    Waits briefly so uvicorn finishes binding and the app is ready to
    serve internal API requests before any resumed job transitions to
    the "running" phase.
    """
    await asyncio.sleep(1)
    await _resume_waiting_jobs(waiting_jobs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _install_job_id_filter()

    # Startup config validation
    config_result = validate_startup_config()
    for error in config_result.errors:
        logger.error("[startup] %s", error)
    for warning in config_result.warnings:
        logger.warning("[startup] %s", warning)
    if config_result.errors:
        raise RuntimeError("Startup configuration validation failed")

    await init_db()
    await storage.cleanup_expired_sessions()

    # Load LLM pricing cache asynchronously (best-effort, non-blocking)
    _warmup = asyncio.create_task(pricing_cache.load())
    _background_tasks.add(_warmup)
    _warmup.add_done_callback(_background_tasks.discard)

    try:
        pricing_cache.start_background_refresh()

        # Wire AI model cache to pricing data and pre-populate cursor models
        model_cache.set_pricing_cache(pricing_cache)
        # Pre-populate cursor models in background (don't block startup)
        task = asyncio.create_task(_safe_preload_cursor_models())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        waiting_jobs = await storage.mark_stale_results_failed()
        if waiting_jobs:
            # Schedule resumption as a background task so it runs after the
            # app is fully started and ready to serve internal API requests.
            task = asyncio.create_task(_deferred_resume_waiting_jobs(waiting_jobs))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        yield
    finally:
        pricing_cache.stop_background_refresh()


app = FastAPI(
    title="Jenkins Job Insight",
    description="Analyzes Jenkins job failures and classifies them as code or product issues",
    version="0.1.0",
    lifespan=lifespan,
)

# React frontend static assets
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _FRONTEND_DIR.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(_FRONTEND_DIR / "assets")),
        name="frontend-assets",
    )


class ErrorTrackingMiddleware(BaseHTTPMiddleware):
    """Track request counts and error rates for monitoring."""

    _SKIP_PATHS = frozenset({"/health", "/api/health", "/metrics", "/favicon.ico"})

    def _schedule_high_error_rate_alert(self) -> None:
        """Check 5xx error rate and schedule an alert if it exceeds the threshold."""
        try:
            snap = error_tracker.snapshot()
            total_requests = snap["total_requests"]
            server_errors = snap.get("error_counts", {}).get("5xx", 0)
            server_error_rate = server_errors / total_requests if total_requests else 0
            if server_error_rate > 0.5 and total_requests >= 10:
                task = asyncio.create_task(
                    dispatch_alert(
                        "high_error_rate",
                        f"\u26a0\ufe0f JJI high 5xx error rate: {server_error_rate:.0%} "
                        f"({server_errors}/{total_requests} requests "
                        f"in {snap['window_seconds']}s window)",
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        except Exception:  # noqa: BLE001 — alert scheduling must never break request handling
            logger.debug("Failed to schedule high-error-rate alert", exc_info=True)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            error_tracker.record_request(500)
            self._schedule_high_error_rate_alert()
            raise
        error_tracker.record_request(response.status_code)
        if response.status_code >= 500:
            self._schedule_high_error_rate_alert()
        return response


def _set_username_cookie(response: Response, username: str, *, secure: bool) -> None:
    """Set the jji_username cookie with consistent attributes."""
    response.set_cookie(
        "jji_username",
        username,
        path="/",
        max_age=365 * 24 * 60 * 60,
        samesite="lax",
        secure=secure,
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate requests: admin via session/Bearer, regular users via cookie."""

    _PUBLIC_PATHS = frozenset(
        {
            "/register",
            "/health",
            "/api/health",
            "/metrics",
            "/favicon.ico",
            "/sw.js",
        }
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Set defaults
        request.state.username = ""
        request.state.is_admin = False
        request.state.role = "user"

        # Public paths and static assets — pass through
        # (but /register may need SSO redirect, handled below;
        #  it stays in _PUBLIC_PATHS for non-SSO users who need the page)
        if path.startswith("/assets/") or (
            path in self._PUBLIC_PATHS and path != "/register"
        ):
            return await call_next(request)

        settings = get_settings()

        # SSO: when trust_proxy_headers is enabled and X-Forwarded-User is
        # present, auto-identify the user and redirect /register → /
        proxy_username = ""
        if settings.trust_proxy_headers:
            proxy_username = request.headers.get("x-forwarded-user", "").strip()

        if path.startswith("/register"):
            if proxy_username and proxy_username.lower() != "admin":
                # Session auth takes precedence over X-Forwarded-User —
                # only check when an SSO redirect would otherwise fire.
                session_token = request.cookies.get("jji_session")
                if session_token and await storage.get_session(session_token):
                    return await call_next(request)
                # SSO user hitting /register — redirect to dashboard
                response = RedirectResponse(url="/", status_code=303)
                if request.cookies.get("jji_username", "") != proxy_username:
                    _set_username_cookie(
                        response, proxy_username, secure=settings.secure_cookies
                    )
                return response
            return await call_next(request)

        is_admin = False
        username = ""
        authenticated_admin = False

        # 1. Check session cookie (jji_session) — admin session
        session_token = request.cookies.get("jji_session")
        if session_token:
            session = await storage.get_session(session_token)
            if session:
                is_admin = bool(session["is_admin"])
                username = str(session["username"])
                authenticated_admin = is_admin

                # Renew session (sliding window) — only when <50% TTL remains
                expires_at_str = session.get("expires_at", "")
                if expires_at_str:
                    try:
                        expires_at = datetime.strptime(
                            str(expires_at_str), "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        remaining = expires_at - datetime.now(timezone.utc)
                        if remaining < timedelta(hours=storage.SESSION_TTL_HOURS / 2):
                            # Await renewal so cookie refresh is only set after confirmed DB update
                            try:
                                renewed = await storage.renew_session(session_token)
                                if renewed:
                                    request.state.renew_session_token = session_token
                            except Exception:
                                logger.debug("Session renewal failed", exc_info=True)
                    except (ValueError, TypeError):
                        logger.debug(
                            "Failed to parse session expires_at for renewal",
                            exc_info=True,
                        )

        # 2. Check Bearer token — admin API key or admin_key
        if not authenticated_admin:
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                if settings.admin_key and hmac.compare_digest(
                    token, settings.admin_key
                ):
                    is_admin = True
                    username = "admin"
                    authenticated_admin = True
                else:
                    user = await storage.get_user_by_key(token)
                    if user:
                        username = str(user["username"])
                        if user.get("role") == "admin":
                            is_admin = True
                            authenticated_admin = True

        # 3. Check X-Forwarded-User header (SSO via trusted proxy)
        if not username and proxy_username:
            if proxy_username.lower() != "admin":
                username = proxy_username
                # Flag that we need to set the jji_username cookie on the response
                request.state.set_proxy_cookie = proxy_username

        # 4. Fall back to jji_username cookie (regular users)
        if not username:
            cookie_username = request.cookies.get("jji_username", "")
            if cookie_username.lower() == "admin":
                # Reserved username — only valid via session/bearer auth
                cookie_username = ""
            username = cookie_username

        request.state.username = username
        request.state.is_admin = is_admin
        request.state.role = "admin" if is_admin else "user"

        # Track user activity (update last_seen for all users)
        if username:
            task = asyncio.create_task(_safe_track_user(username))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Admin-only path enforcement
        if path.startswith("/api/admin/"):
            if not authenticated_admin:
                return JSONResponse(
                    status_code=403, content={"detail": "Admin access required"}
                )

        # Redirect to /register if no username for browser HTML requests (keep existing behavior)
        if not username and not path.startswith("/api/"):
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                return RedirectResponse(url="/register", status_code=303)

        response = await call_next(request)

        # Set jji_username cookie from X-Forwarded-User header (SSO)
        if getattr(request.state, "set_proxy_cookie", None):
            proxy_cookie_value = request.state.set_proxy_cookie
            if request.cookies.get("jji_username", "") != proxy_cookie_value:
                _set_username_cookie(
                    response, proxy_cookie_value, secure=settings.secure_cookies
                )

        # Refresh session cookie max_age if session was renewed
        if getattr(request.state, "renew_session_token", None):
            # Skip if downstream handler already set/cleared jji_session
            # (e.g., login sets a new session, logout deletes it)
            path = request.url.path
            if path not in ("/api/auth/login", "/api/auth/logout"):
                settings = get_settings()
                response.set_cookie(
                    "jji_session",
                    request.state.renew_session_token,
                    httponly=True,
                    samesite="strict",
                    secure=settings.secure_cookies,
                    max_age=storage.SESSION_TTL_SECONDS,
                )

        return response


app.add_middleware(AuthMiddleware)
app.add_middleware(ErrorTrackingMiddleware)


_BODY_LOGGING_SKIP_PATHS = frozenset({"/api/feedback/preview", "/api/feedback/create"})


class RequestBodyLoggingMiddleware(BaseHTTPMiddleware):
    """Log incoming request bodies at DEBUG level with sensitive data masked."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _BODY_LOGGING_SKIP_PATHS:
            return await call_next(request)
        if logger.isEnabledFor(logging.DEBUG) and request.method in (
            "POST",
            "PUT",
            "PATCH",
        ):
            content_type = request.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                return await call_next(request)
            body_bytes = await request.body()
            if body_bytes:
                try:
                    body_json = json.loads(body_bytes)
                    masked = mask_sensitive_fields(body_json)
                    logger.debug(
                        "Incoming %s %s body: %s",
                        request.method,
                        request.url.path,
                        json.dumps(masked),
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.debug(
                        "Incoming %s %s body: <non-JSON, %d bytes>",
                        request.method,
                        request.url.path,
                        len(body_bytes),
                    )
        return await call_next(request)


app.add_middleware(RequestBodyLoggingMiddleware)


def _mask_pydantic_error(error: dict) -> dict:
    """Mask sensitive input values in a Pydantic validation error dict."""
    result = dict(error)
    loc = error.get("loc") or ()
    field = loc[-1] if loc else ""
    if isinstance(field, str) and _is_sensitive_key(field) and "input" in result:
        result["input"] = "***"
    elif "input" in result:
        result["input"] = mask_sensitive_fields(result["input"])
    return result


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Log 422 validation error details at DEBUG level, then return standard response."""
    if request.url.path in _BODY_LOGGING_SKIP_PATHS:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors())},
        )
    if logger.isEnabledFor(logging.DEBUG):
        masked_body = None
        if exc.body is not None:
            try:
                if isinstance(exc.body, (dict, list)):
                    masked_body = mask_sensitive_fields(exc.body)
                elif isinstance(exc.body, (str, bytes, bytearray)):
                    size = (
                        len(exc.body.encode("utf-8"))
                        if isinstance(exc.body, str)
                        else len(exc.body)
                    )
                    masked_body = f"<non-JSON, {size} bytes>"
                else:
                    masked_body = f"<non-JSON body: {type(exc.body).__name__}>"
            except Exception:  # noqa: BLE001 — masking must never break the 422 response
                masked_body = "<unable to mask>"
        raw_errors = jsonable_encoder(exc.errors())
        masked_errors = [_mask_pydantic_error(e) for e in raw_errors]
        logger.debug(
            "RequestValidationError on %s %s: errors=%s body=%s",
            request.method,
            request.url.path,
            masked_errors,
            masked_body,
        )
    # Response body uses raw (unmasked) errors — only the DEBUG log path is masked.
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


@app.get("/", include_in_schema=False)
async def root() -> HTMLResponse:
    """Serve the React SPA."""
    return _serve_spa()


def _resolve_ai_config_values(
    ai_provider: str | None, ai_model: str | None
) -> tuple[str, str]:
    """Resolve and validate AI provider and model from given values or env defaults.

    Args:
        ai_provider: Provider from request body (or None).
        ai_model: Model from request body (or None).

    Returns:
        Tuple of (ai_provider, ai_model).

    Raises:
        HTTPException: If provider or model is not configured.
    """
    provider = ai_provider or AI_PROVIDER
    model = ai_model or AI_MODEL
    if not provider:
        raise HTTPException(
            status_code=400,
            detail=f"No AI provider configured. Set AI_PROVIDER env var or pass ai_provider in request body. Valid providers: {', '.join(sorted(VALID_AI_PROVIDERS))}",
        )
    if provider not in VALID_AI_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported AI provider: {provider}. "
                f"Valid providers: {', '.join(sorted(VALID_AI_PROVIDERS))}"
            ),
        )
    if not model:
        raise HTTPException(
            status_code=400,
            detail="No AI model configured. Set AI_MODEL env var or pass ai_model in request body.",
        )
    return provider, model


def _resolve_ai_config(body: AnalyzeRequest) -> tuple[str, str]:
    """Resolve AI config from an AnalyzeRequest."""
    return _resolve_ai_config_values(body.ai_provider, body.ai_model)


def _resolve_peer_ai_configs(
    body: BaseAnalysisRequest, settings: Settings
) -> list | None:
    """Resolve peer AI configs from request body or env var default.

    Priority:
    - Request field absent (None) -> use server default from PEER_AI_CONFIGS env var
    - Request field present and empty ([]) -> explicitly disable peers
    - Request field present and non-empty -> use request value

    Returns:
        List of peer config dicts/AiConfigEntry, or None if no peers configured.
    """
    if body.peer_ai_configs is not None:
        return body.peer_ai_configs or None  # [] -> None (disable)
    # Fall back to env var default (string format)
    if settings.peer_ai_configs:
        return parse_peer_configs(settings.peer_ai_configs) or None
    return None


def _validate_peer_configs(
    body: BaseAnalysisRequest, settings: Settings
) -> list | None:
    """Resolve and validate peer AI configs. Raises HTTPException(400) on invalid input."""
    try:
        return _resolve_peer_ai_configs(body, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_enable_jira(body: BaseAnalysisRequest, settings: Settings) -> bool:
    """Resolve enable_jira flag from request, env var, or auto-detection.

    Priority order:
    1. Request body field (highest)
    2. settings.jira_enabled property (env var + auto-detect fallback)

    Args:
        body: The analysis request (AnalyzeRequest or AnalyzeFailuresRequest).
        settings: Application settings (should be merged settings).

    Returns:
        True if Jira enrichment should run, False otherwise.
    """
    if body.enable_jira is not None:
        return body.enable_jira
    return settings.jira_enabled


def _merge_settings(body: BaseAnalysisRequest, settings: Settings) -> Settings:
    """Create a copy of settings with per-request overrides applied.

    Request values take precedence over environment variable defaults.
    Only non-None request values are applied as overrides.

    Args:
        body: The analysis request with optional override fields.
        settings: Base application settings from environment.

    Returns:
        Settings instance with overrides applied (or original if no overrides).
    """
    overrides: dict = {}

    # Direct field mappings (request field name == settings field name).
    # Keep in sync with BaseAnalysisRequest and Settings when adding new overrides.
    # Fields intentionally NOT listed here are handled by their own resolvers:
    #   tests_repo_url   - HttpUrl vs str type mismatch; resolved in endpoint code
    #   ai_provider      - resolved + validated by _resolve_ai_config()
    #   ai_model         - resolved + validated by _resolve_ai_config()
    #   additional_repos - list vs str type mismatch; resolved by resolve_additional_repos()
    direct_fields = [
        "jira_url",
        "jira_email",
        "jira_project_key",
        "jira_ssl_verify",
        "jira_max_results",
        "ai_cli_timeout",
        "max_concurrent_ai_calls",
        "enable_jira",
        "jenkins_artifacts_max_size_mb",
        "get_job_artifacts",
    ]
    for field in direct_fields:
        value = getattr(body, field, None)
        if value is not None:
            overrides[field] = value

    # peer_analysis_max_rounds has a non-None default in the model;
    # only apply as override when explicitly sent by the caller.
    if "peer_analysis_max_rounds" in body.model_fields_set:
        overrides["peer_analysis_max_rounds"] = body.peer_analysis_max_rounds

    # SecretStr fields need wrapping
    if body.jira_api_token is not None:
        overrides["jira_api_token"] = SecretStr(body.jira_api_token)
    if body.jira_pat is not None:
        overrides["jira_pat"] = SecretStr(body.jira_pat)
    if body.github_token is not None:
        overrides["github_token"] = SecretStr(body.github_token)
    if body.tests_repo_token is not None:
        overrides["tests_repo_token"] = SecretStr(body.tests_repo_token)

    # AnalyzeRequest-specific fields (Jenkins overrides + monitoring)
    if isinstance(body, AnalyzeRequest):
        jenkins_fields = [
            "jenkins_url",
            "jenkins_user",
            "jenkins_password",
            "jenkins_ssl_verify",
            "jenkins_timeout",
        ]
        for field in jenkins_fields:
            value = getattr(body, field, None)
            if value is not None:
                overrides[field] = value

        # Monitoring fields have non-None defaults in the model.  Only
        # apply them as overrides when explicitly sent by the caller
        # (present in ``model_fields_set``) so that omitted fields fall
        # back to the environment/settings default instead of always
        # overriding with the model default.
        for field in (
            "wait_for_completion",
            "poll_interval_minutes",
            "max_wait_minutes",
        ):
            if field in body.model_fields_set:
                overrides[field] = getattr(body, field)

        # force has a non-None default (False); only override when
        # explicitly sent so that omitted requests inherit from env/settings.
        if "force" in body.model_fields_set:
            overrides["force_analysis"] = body.force

    if overrides:
        merged_data = settings.model_dump(mode="python") | overrides
        return Settings.model_validate(merged_data)
    return settings


def _collect_all_failures(
    items: Sequence[FailureAnalysis | ChildJobAnalysis],
) -> list[FailureAnalysis]:
    """Recursively collect all FailureAnalysis objects from a mixed list.

    Recurses into ChildJobAnalysis objects to find nested failures.

    Args:
        items: Mixed list of FailureAnalysis and ChildJobAnalysis objects.

    Returns:
        Flat list of all FailureAnalysis objects found.
    """
    result: list[FailureAnalysis] = []
    for item in items:
        if isinstance(item, FailureAnalysis):
            result.append(item)
        elif isinstance(item, ChildJobAnalysis):
            result.extend(_collect_all_failures(item.failures))
            result.extend(_collect_all_failures(item.failed_children))
    return result


async def _enrich_result_with_jira(
    failures: list[FailureAnalysis | ChildJobAnalysis],
    settings: Settings,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
) -> None:
    """Enrich PRODUCT BUG failures with Jira matches.

    Collects all FailureAnalysis objects from the provided list,
    recursing into ChildJobAnalysis objects, then searches Jira
    for matching issues. Results are attached in-place.

    Args:
        failures: Mixed list of FailureAnalysis and ChildJobAnalysis objects.
        settings: Application settings with Jira configuration.
        ai_provider: AI provider for Jira relevance filtering.
        ai_model: AI model for Jira relevance filtering.
        job_id: Job identifier for token usage tracking.
    """
    if not settings.jira_enabled:
        return

    all_failures = _collect_all_failures(failures)
    await enrich_with_jira_matches(
        all_failures, settings, ai_provider, ai_model, job_id=job_id
    )


async def _enrich_result_with_tests_repo_matches(
    failures: list[FailureAnalysis | ChildJobAnalysis],
    settings: Settings,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
    tests_repo_url: str = "",
) -> None:
    """Enrich CODE ISSUE failures with tests repo issue matches.

    Collects all FailureAnalysis objects from the provided list,
    recursing into ChildJobAnalysis objects, then searches GitHub
    Issues for matching issues. Results are attached in-place.

    Args:
        failures: Mixed list of FailureAnalysis and ChildJobAnalysis objects.
        settings: Application settings with tests repo configuration.
        ai_provider: AI provider for relevance filtering.
        ai_model: AI model for relevance filtering.
        job_id: Job identifier for token usage tracking.
        tests_repo_url: Per-request tests repo URL override.  When
            non-empty and ``settings.tests_repo_url`` is unset, a
            temporary settings copy is created so that
            ``enrich_with_tests_repo_matches`` sees the URL.
    """
    effective_url = tests_repo_url or str(settings.tests_repo_url or "")
    effective_url, _ = parse_repo_ref(effective_url)
    if not effective_url:
        return

    # When the URL came from the request (not env), inject it into a
    # settings copy so downstream helpers see it.
    if effective_url != str(settings.tests_repo_url or ""):
        merged_data = settings.model_dump(mode="python")
        merged_data["tests_repo_url"] = effective_url
        settings = Settings.model_validate(merged_data)

    all_failures = _collect_all_failures(failures)
    await enrich_with_tests_repo_matches(
        all_failures, settings, ai_provider, ai_model, job_id=job_id
    )


async def _wait_for_jenkins_completion(
    jenkins_url: str,
    job_name: str,
    build_number: int,
    jenkins_user: str,
    jenkins_password: str,
    jenkins_ssl_verify: bool,
    poll_interval_minutes: int,
    max_wait_minutes: int,
    jenkins_timeout: int = 30,
    max_consecutive_failures: int = 5,
) -> tuple[bool, str]:
    """Poll Jenkins until the build finishes.

    Args:
        jenkins_url: Jenkins server base URL.
        job_name: Name of the Jenkins job.
        build_number: Build number to monitor.
        jenkins_user: Jenkins username for authentication.
        jenkins_password: Jenkins password or API token.
        jenkins_ssl_verify: Whether to verify SSL certificates.
        poll_interval_minutes: Minutes between polls.
        max_wait_minutes: Maximum minutes to wait before timing out.
            0 means no limit (poll forever until job finishes).
        jenkins_timeout: Jenkins API request timeout in seconds.
        max_consecutive_failures: Number of consecutive transient errors
            allowed before giving up. Defaults to 5.

    Returns:
        A tuple of (success, error_message). success is True if the build
        completed, False otherwise. error_message is empty on success.
    """
    import jenkins

    from jenkins_job_insight.jenkins import JenkinsClient
    from jenkins_job_insight.utils import is_jenkins_connectivity_error

    client = JenkinsClient(
        url=jenkins_url,
        username=jenkins_user,
        password=jenkins_password,
        ssl_verify=jenkins_ssl_verify,
        timeout=jenkins_timeout,
    )

    unreachable_error = (
        "Cannot reach Jenkins; please verify the Jenkins URL, credentials, "
        "and network connectivity"
    )

    try:
        await asyncio.to_thread(client.get_whoami)
    except Exception as e:
        if not is_jenkins_connectivity_error(e):
            raise
        logger.error("Cannot reach Jenkins at %s: %s", jenkins_url, e, exc_info=True)
        return False, (
            "Jenkins reachability check failed; please verify the Jenkins URL, "
            "credentials, and network connectivity"
        )

    if max_wait_minutes > 0:
        deadline: float | None = _time.monotonic() + max_wait_minutes * 60
    else:
        deadline = None  # No limit

    consecutive_failures = 0

    while True:
        try:
            build_info = await asyncio.to_thread(
                client.get_build_info_safe, job_name, build_number
            )
            consecutive_failures = 0

            if build_info and not build_info.get("building", True):
                logger.info(
                    f"Jenkins job {job_name} #{build_number} completed "
                    f"with result: {build_info.get('result')}"
                )
                return True, ""

            logger.info(f"Jenkins job {job_name} #{build_number} still running")

        except jenkins.NotFoundException:
            logger.error(
                f"Jenkins job {job_name} #{build_number} not found (404). "
                "Stopping poll."
            )
            return False, f"Jenkins job {job_name} #{build_number} not found (404)"

        except Exception as e:
            if not is_jenkins_connectivity_error(e):
                logger.error(
                    "Non-transient error checking Jenkins status", exc_info=True
                )
                return False, "Jenkins poll failed; check server logs for details"
            consecutive_failures += 1
            logger.warning(
                "Transient error checking Jenkins status (%d/%d): %s",
                consecutive_failures,
                max_consecutive_failures,
                e,
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "Cannot reach Jenkins at %s after %d consecutive failures",
                    jenkins_url,
                    consecutive_failures,
                    exc_info=True,
                )
                return False, unreachable_error

        if deadline is not None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval_minutes * 60, remaining))
        else:
            await asyncio.sleep(poll_interval_minutes * 60)

    error_msg = (
        f"Timed out waiting for Jenkins job {job_name} #{build_number} "
        f"after {max_wait_minutes} minutes"
    )
    logger.warning(error_msg)
    return False, error_msg


async def process_analysis_with_id(
    job_id: str, body: AnalyzeRequest, settings: Settings
) -> None:
    """Background task to process analysis with a pre-generated job_id.

    Args:
        job_id: Pre-generated job ID for tracking.
        body: The analysis request.
        settings: Application settings.
    """
    job_id_var.set(job_id)
    logger.info(
        f"Analysis request received for {body.job_name} #{body.build_number} "
        f"(job_id: {job_id})"
    )

    async def _safe_update_progress_phase(phase: str) -> None:
        try:
            await update_progress_phase(job_id, phase)
        except Exception:
            logger.debug(
                f"Failed to update progress phase for job_id={job_id}, phase={phase}",
                exc_info=True,
            )

    try:
        # Validate AI config early -- before potentially waiting hours for Jenkins.
        # This ensures invalid provider/model fails fast instead of after a long wait.
        ai_provider, ai_model = _resolve_ai_config(body)

        # Wait for Jenkins job to finish if requested and Jenkins is configured
        if settings.wait_for_completion and not settings.jenkins_url:
            logger.info(
                f"Wait requested for job {job_id} but jenkins_url not configured, skipping wait"
            )

        if settings.wait_for_completion and settings.jenkins_url:
            await update_status(job_id, "waiting")
            await _safe_update_progress_phase("waiting_for_jenkins")

            completed, wait_error = await _wait_for_jenkins_completion(
                jenkins_url=settings.jenkins_url,
                job_name=body.job_name,
                build_number=body.build_number,
                jenkins_user=settings.jenkins_user,
                jenkins_password=settings.jenkins_password,
                jenkins_ssl_verify=settings.jenkins_ssl_verify,
                poll_interval_minutes=settings.poll_interval_minutes,
                max_wait_minutes=settings.max_wait_minutes,
                jenkins_timeout=settings.jenkins_timeout,
            )

            if not completed:
                fail_data = {
                    "job_name": body.job_name,
                    "build_number": body.build_number,
                    "error": wait_error,
                }
                await _preserve_request_params(job_id, fail_data)
                await update_status(
                    job_id,
                    "failed",
                    fail_data,
                )
                return

        logger.debug(
            f"process_analysis_with_id: updating status to running, job_id={job_id}"
        )
        await update_status(job_id, "running")
        await _safe_update_progress_phase("analyzing")

        logger.debug(
            f"process_analysis_with_id: ai_provider={ai_provider}, ai_model={ai_model}"
        )

        server_url = _build_internal_server_url()

        # Resolve peer AI configs: request body (JSON list) takes precedence
        # over env var default (parsed from "provider:model" string).
        # None = not sent → use env default; [] = explicitly disable peers.
        peer_ai_configs = _resolve_peer_ai_configs(body, settings)

        result = await analyze_job(
            body,
            settings,
            ai_provider=ai_provider,
            ai_model=ai_model,
            job_id=job_id,
            server_url=server_url,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=settings.peer_analysis_max_rounds,
        )

        # Enrich PRODUCT BUG failures with Jira matches
        if _resolve_enable_jira(body, settings):
            await _safe_update_progress_phase("enriching_jira")
            logger.debug(
                f"process_analysis_with_id: enriching with Jira matches, job_id={job_id}"
            )
            await _enrich_result_with_jira(
                result.failures + list(result.child_job_analyses),
                settings,
                ai_provider,
                ai_model,
                job_id=job_id,
            )

        # Enrich CODE ISSUE failures with tests repo issue matches
        request_tests_repo_url = str(body.tests_repo_url or "")
        if settings.tests_repo_url or request_tests_repo_url:
            await _safe_update_progress_phase("enriching_tests_repo")
            logger.debug(
                f"process_analysis_with_id: enriching with tests repo matches, job_id={job_id}"
            )
            await _enrich_result_with_tests_repo_matches(
                result.failures + list(result.child_job_analyses),
                settings,
                ai_provider,
                ai_model,
                job_id=job_id,
                tests_repo_url=request_tests_repo_url,
            )

        await _safe_update_progress_phase("saving")
        logger.debug(
            f"process_analysis_with_id: saving completed result, job_id={job_id}"
        )
        result_data = result.model_dump(mode="json")
        await _preserve_request_params(job_id, result_data)

        # Attach token usage summary before persisting
        await _attach_token_usage(job_id, result_data)

        # Save to storage — do NOT persist base_url / result_url as they are
        # request-derived and re-generated on every GET to avoid host-header
        # injection from being stored.
        await update_status(job_id, result.status, result_data)
        logger.info(
            f"Analysis completed for {body.job_name} #{body.build_number} "
            f"(job_id: {job_id})"
        )

        # Populate failure history for completed analyses
        if result.status == "completed":
            try:
                await populate_failure_history(job_id, result_data)
            except Exception:
                logger.warning(
                    "Failed to populate failure_history for job_id=%s",
                    job_id,
                    exc_info=True,
                )

        # Auto-assign job metadata from name pattern rules
        try:
            await storage.auto_assign_job_metadata(
                body.job_name, settings.metadata_rules
            )
        except Exception:  # noqa: BLE001 — metadata auto-assignment is best-effort
            logger.warning(
                "Failed to auto-assign metadata for job '%s'",
                body.job_name,
                exc_info=True,
            )

        # Reveal classifications created during analysis
        await storage.make_classifications_visible(job_id)

    except Exception as e:
        logger.exception(f"Analysis failed for job {job_id}")
        error_detail = format_exception_with_type(e)
        error_data: dict = {
            "job_name": body.job_name,
            "build_number": body.build_number,
            "error": error_detail,
        }
        await _preserve_request_params(job_id, error_data)

        # Attach token usage even on failure — partial AI calls may have been recorded
        await _attach_token_usage(job_id, error_data)

        await update_status(job_id, "failed", error_data)


def _build_base_request_params(
    ai_provider: str,
    ai_model: str,
    peer_ai_configs_resolved: list | None = None,
    *,
    tests_repo_url: str = "",
    tests_repo_token: str = "",
    tests_repo_ref: str = "",
    additional_repos: list | None = None,
) -> dict:
    """Serialize the common request parameters shared by all analysis endpoints.

    Captures the AI configuration, peer configs, tests repo, and additional
    repos.  Callers pass the **resolved** (effective) values so that env-var
    and config-file defaults are persisted, not just request-body values.

    Args:
        ai_provider: Resolved AI provider name.
        ai_model: Resolved AI model name.
        peer_ai_configs_resolved: Resolved peer AI configs (already validated).
        tests_repo_url: Effective tests repo URL (already resolved from
            request body / env / config).
        tests_repo_token: Authentication token for cloning private tests repo.
        tests_repo_ref: Git ref (branch/tag) for tests repo checkout.
        additional_repos: Effective additional repos list (already resolved).

    Returns:
        Dict of serializable base request parameters.
    """
    return {
        "ai_provider": ai_provider,
        "ai_model": ai_model,
        "peer_ai_configs": [
            c.model_dump() if hasattr(c, "model_dump") else c
            for c in (peer_ai_configs_resolved or [])
        ],
        "additional_repos": [
            ar.model_dump(mode="json") if hasattr(ar, "model_dump") else ar
            for ar in additional_repos
        ]
        if additional_repos is not None
        else None,
        "tests_repo_url": tests_repo_url,
        "tests_repo_token": tests_repo_token,
        "tests_repo_ref": tests_repo_ref,
    }


def _build_request_params(
    body: AnalyzeRequest,
    merged: Settings,
    ai_provider: str,
    ai_model: str,
    peer_ai_configs_resolved: list | None = None,
) -> dict:
    """Serialize the request parameters needed to resume a waiting job.

    Captures everything ``process_analysis_with_id`` needs so that the
    background task can be re-created after a server restart.

    Args:
        body: The original analysis request.
        merged: Settings with per-request overrides applied.
        ai_provider: Resolved AI provider name.
        ai_model: Resolved AI model name.

    Returns:
        Dict of serializable request parameters.
    """
    resolved_tests_repo = (
        str(body.tests_repo_url)
        if body.tests_repo_url is not None
        else str(merged.tests_repo_url)
        if merged.tests_repo_url
        else ""
    )
    resolved_tests_repo, tests_repo_ref = parse_repo_ref(resolved_tests_repo)
    resolved_tests_repo_token = resolve_tests_repo_token(body, merged)
    resolved_additional = resolve_additional_repos(body, merged)
    params = _build_base_request_params(
        ai_provider,
        ai_model,
        peer_ai_configs_resolved,
        tests_repo_url=resolved_tests_repo,
        tests_repo_token=resolved_tests_repo_token,
        tests_repo_ref=tests_repo_ref,
        additional_repos=resolved_additional,
    )
    params.update(
        {
            "jenkins_url": merged.jenkins_url,
            "jenkins_user": merged.jenkins_user,
            "jenkins_password": merged.jenkins_password,
            "jenkins_ssl_verify": merged.jenkins_ssl_verify,
            "jenkins_timeout": merged.jenkins_timeout,
            "wait_for_completion": merged.wait_for_completion,
            "poll_interval_minutes": merged.poll_interval_minutes,
            "max_wait_minutes": merged.max_wait_minutes,
            "enable_jira": body.enable_jira
            if body.enable_jira is not None
            else merged.enable_jira,
            "jira_url": merged.jira_url or "",
            "jira_email": merged.jira_email or "",
            "jira_api_token": merged.jira_api_token.get_secret_value()
            if merged.jira_api_token
            else "",
            "jira_pat": merged.jira_pat.get_secret_value() if merged.jira_pat else "",
            "jira_project_key": merged.jira_project_key or "",
            "jira_ssl_verify": merged.jira_ssl_verify,
            "jira_max_results": merged.jira_max_results,
            "github_token": merged.github_token.get_secret_value()
            if merged.github_token
            else "",
            "ai_cli_timeout": merged.ai_cli_timeout,
            "max_concurrent_ai_calls": merged.max_concurrent_ai_calls,
            "jenkins_artifacts_max_size_mb": merged.jenkins_artifacts_max_size_mb,
            "get_job_artifacts": merged.get_job_artifacts,
            "raw_prompt": body.raw_prompt or "",
            "peer_analysis_max_rounds": merged.peer_analysis_max_rounds,
            "force": merged.force_analysis,
            "wait_started_at": _time.time(),
        }
    )
    return encrypt_sensitive_fields(params)


async def _enqueue_analysis_job(
    body: AnalyzeRequest,
    merged: Settings,
    resolved_peers: list | None,
    background_tasks: BackgroundTasks,
    base_url: str,
    *,
    message_prefix: str = "Analysis",
) -> dict:
    """Create, save, and enqueue a new analysis job.

    Shared by ``/analyze`` and ``/re-analyze`` to avoid duplicating
    job setup, persistence, and response shaping.
    """
    job_id = str(uuid.uuid4())
    job_id_var.set(job_id)
    jenkins_url = build_jenkins_url(
        merged.jenkins_url, body.job_name, body.build_number
    )
    initial_result: dict = {
        "job_name": body.job_name,
        "build_number": body.build_number,
        "request_params": _build_request_params(
            body,
            merged,
            body.ai_provider or AI_PROVIDER,
            body.ai_model or AI_MODEL,
            peer_ai_configs_resolved=resolved_peers,
        ),
    }
    can_resume_wait = merged.wait_for_completion and bool(merged.jenkins_url)
    await save_result(
        job_id,
        jenkins_url,
        "waiting" if can_resume_wait else "pending",
        initial_result,
    )
    background_tasks.add_task(process_analysis_with_id, job_id, body, merged)
    response: dict = {
        "status": "queued",
        "job_id": job_id,
        "message": f"{message_prefix} job queued. Poll /results/{job_id} for status.",
    }
    return _attach_result_links(response, base_url, job_id)


@app.post("/analyze", status_code=202, response_model=None)
async def analyze(
    request: Request,
    body: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    *,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Submit a Jenkins job for analysis.

    Returns immediately with a job_id. Poll /results/{job_id} for status.
    """
    _check_allow_list(request)
    logger.debug(f"Starting analysis for {body.job_name} #{body.build_number}")
    base_url = _extract_base_url()

    # Validate AI config early -- fail fast before queuing invalid jobs.
    _resolve_ai_config(body)

    merged = _merge_settings(body, settings)

    # Validate peer configs early -- fail fast before returning 202.
    resolved_peers = _validate_peer_configs(body, merged)
    return await _enqueue_analysis_job(
        body,
        merged,
        resolved_peers,
        background_tasks,
        base_url,
    )


@app.post("/analyze-failures", response_model=None)
async def analyze_failures(
    request: Request,
    body: AnalyzeFailuresRequest,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Analyze raw test failures directly without Jenkins.

    Accepts test failure data (or raw JUnit XML) and returns AI analysis.
    Sync only. Reuses the same deduplication and analysis logic as Jenkins-based analysis.

    When raw_xml is provided, failures are extracted from the XML and the enriched
    XML with analysis results is included in the response.
    """
    _check_allow_list(request)
    logger.debug(
        f"POST /analyze-failures: failures_count={len(body.failures) if body.failures else 0}, has_raw_xml={body.raw_xml is not None}"
    )
    base_url = _extract_base_url()

    if raw_xml := body.raw_xml:
        try:
            test_failures = extract_test_failures(raw_xml)
        except ParseError as e:
            raise HTTPException(status_code=400, detail=f"Invalid XML: {e}") from e

        if not test_failures:
            job_id = str(uuid.uuid4())
            job_id_var.set(job_id)
            analysis_result = FailureAnalysisResult(
                job_id=job_id,
                status="completed",
                summary="No test failures found in the provided XML.",
                enriched_xml=raw_xml,
            )
            result_data = analysis_result.model_dump(mode="json")
            await save_result(job_id, "", "completed", result_data)
            return JSONResponse(
                content=_attach_result_links(result_data, base_url, job_id)
            )
    else:
        if not body.failures:
            raise HTTPException(status_code=400, detail="No failures provided")
        test_failures = body.failures

    merged = _merge_settings(body, settings)
    ai_provider, ai_model = _resolve_ai_config_values(body.ai_provider, body.ai_model)

    # Validate/resolve peer configs early -- fail fast before saving result.
    peer_ai_configs = _validate_peer_configs(body, merged)

    # Resolve repos early so _build_base_request_params captures effective values
    # (including env-var / config-file defaults, not just request-body values).
    tests_repo_url_raw = str(body.tests_repo_url or merged.tests_repo_url or "")
    tests_repo_url, tests_repo_ref = parse_repo_ref(tests_repo_url_raw)
    resolved_tests_repo_token = resolve_tests_repo_token(body, merged)
    additional_repos_list = resolve_additional_repos(body, merged)

    job_id = str(uuid.uuid4())
    job_id_var.set(job_id)
    logger.info(
        f"Direct failure analysis request received with {len(test_failures)} failures (job_id: {job_id})"
    )

    # Save initial pending state with request_params so GET /results/{job_id}
    # works immediately and _preserve_request_params can find them later.
    initial_result: dict = {
        "request_params": encrypt_sensitive_fields(
            _build_base_request_params(
                ai_provider,
                ai_model,
                peer_ai_configs,
                tests_repo_url=tests_repo_url,
                tests_repo_token=resolved_tests_repo_token,
                tests_repo_ref=tests_repo_ref,
                additional_repos=additional_repos_list or None,
            )
        ),
    }
    await save_result(job_id, "", "pending", initial_result)

    # Group failures by error signature for deduplication
    groups: dict[str, list] = defaultdict(list)
    for failure in test_failures:
        sig = get_failure_signature(failure)
        groups[sig].append(failure)

    logger.info(
        f"Grouped {len(test_failures)} failures into {len(groups)} unique error signatures"
    )

    # Always create a workspace for AI to work in
    repo_manager = RepositoryManager()
    custom_prompt = ""
    cloned_repos: dict[str, Path] = {}
    try:
        await update_status(job_id, "running")

        repo_path = repo_manager.create_workspace()

        if tests_repo_url:
            try:
                repo_name = derive_test_repo_name(
                    str(tests_repo_url), additional_repos_list
                )
                logger.info(
                    f"Cloning test repository: {_redact_url(str(tests_repo_url))}"
                    + (f" (ref={tests_repo_ref})" if tests_repo_ref else "")
                )

                await asyncio.to_thread(
                    repo_manager.clone_into,
                    str(tests_repo_url),
                    repo_path / repo_name,
                    depth=50,
                    branch=tests_repo_ref,
                    token=resolved_tests_repo_token or None,
                )
                cloned_repos[repo_name] = repo_path / repo_name
                logger.info(f"Successfully cloned test repository into {repo_name}/")
            except Exception as e:  # noqa: BLE001 — non-fatal tests repo clone failure
                logger.warning(
                    "Failed to clone test repository (%s)",
                    type(e).__name__,
                )

        # Clone additional repositories for AI context
        if additional_repos_list:
            additional_repos_cloned, repo_path = await clone_additional_repos(
                repo_manager, additional_repos_list, repo_path
            )
            cloned_repos.update(additional_repos_cloned)

        custom_prompt = (body.raw_prompt or "").strip()

        server_url = _build_internal_server_url()

        # Analyze each unique failure group in parallel
        coroutines: list[Coroutine[Any, Any, Any]] = [
            analyze_failure_group(
                failures=group_failures,
                console_context="",
                repo_path=repo_path,
                ai_provider=ai_provider,
                ai_model=ai_model,
                ai_cli_timeout=merged.ai_cli_timeout,
                custom_prompt=custom_prompt,
                server_url=server_url,
                job_id=job_id,
                peer_ai_configs=peer_ai_configs,
                peer_analysis_max_rounds=merged.peer_analysis_max_rounds,
                additional_repos=cloned_repos or None,
                max_concurrent_ai_calls=merged.max_concurrent_ai_calls,
            )
            for group_failures in groups.values()
        ]

        results = await run_parallel_with_limit(
            coroutines, max_concurrency=merged.max_concurrent_ai_calls
        )

        # Flatten results and filter out exceptions
        all_analyses = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    f"Failed to analyze failure group: {result}", exc_info=result
                )
            else:
                all_analyses.extend(result)

        unique_errors = len(groups)
        summary = f"Analyzed {len(test_failures)} test failures ({unique_errors} unique errors). {len(all_analyses)} analyzed successfully."

        # Enrich PRODUCT BUG failures with Jira matches
        if _resolve_enable_jira(body, merged):
            await enrich_with_jira_matches(
                all_analyses, merged, ai_provider, ai_model, job_id=job_id
            )

        # Enrich CODE ISSUE failures with tests repo issue matches
        if merged.tests_repo_url or tests_repo_url:
            await _enrich_result_with_tests_repo_matches(
                all_analyses,
                merged,
                ai_provider,
                ai_model,
                job_id=job_id,
                tests_repo_url=tests_repo_url,
            )

        # If raw_xml was provided, produce enriched XML
        enriched_xml = None
        if raw_xml is not None:
            enriched_xml = build_enriched_xml(
                raw_xml, all_analyses, f"{base_url}/results/{job_id}"
            )

        analysis_result = FailureAnalysisResult(
            job_id=job_id,
            status="completed",
            summary=summary,
            ai_provider=ai_provider,
            ai_model=ai_model,
            failures=all_analyses,
            enriched_xml=enriched_xml,
        )

        result_data = analysis_result.model_dump(mode="json")
        await _preserve_request_params(job_id, result_data)

        # Attach token usage summary before persisting
        await _attach_token_usage(job_id, result_data)

        await update_status(job_id, "completed", result_data)

        # Populate failure history
        try:
            await populate_failure_history(job_id, result_data)
        except Exception:
            logger.warning(
                "Failed to populate failure_history for job_id=%s",
                job_id,
                exc_info=True,
            )

        # Reveal classifications created during analysis
        await storage.make_classifications_visible(job_id)

        return JSONResponse(content=_attach_result_links(result_data, base_url, job_id))

    except Exception as e:
        logger.exception(f"Direct failure analysis failed for job {job_id}")
        error_detail = format_exception_with_type(e)
        analysis_result = FailureAnalysisResult(
            job_id=job_id,
            status="failed",
            summary=f"Analysis failed: {error_detail}",
            ai_provider=ai_provider,
            ai_model=ai_model,
        )
        fail_data = analysis_result.model_dump(mode="json")
        await _preserve_request_params(job_id, fail_data)

        # Attach token usage even on failure — partial AI calls may have been recorded
        await _attach_token_usage(job_id, fail_data)

        await update_status(job_id, "failed", fail_data)
        return JSONResponse(content=_attach_result_links(fail_data, base_url, job_id))

    finally:
        repo_manager.cleanup()


@app.post("/re-analyze/{job_id}", status_code=202, response_model=None)
async def re_analyze(
    job_id: str,
    request: Request,
    body: BaseAnalysisRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Re-analyze a previously analyzed job with the same (or overridden) settings.

    Loads stored request_params from the original analysis, applies any
    overrides from the request body, and queues a new analysis with a
    fresh job_id.
    """
    _check_allow_list(request)
    base_url = _extract_base_url()

    # Load the original result (with sensitive fields for credential reuse)
    stored = await get_result(job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Result {job_id} not found")

    result_data = stored["result"]
    if "request_params" not in result_data:
        raise HTTPException(
            status_code=400,
            detail="Original analysis has no stored request_params; cannot re-analyze",
        )

    # Reconstruct the original AnalyzeRequest + Settings
    try:
        original_body, original_settings = _reconstruct_from_params(result_data)
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to reconstruct original request: {exc}",
        ) from exc

    # Apply overrides from request body onto the reconstructed request
    # For each non-None field in the override body, set it on original_body
    for field_name in body.model_fields_set:
        setattr(original_body, field_name, getattr(body, field_name))

    # Re-merge settings with overrides applied
    merged = _merge_settings(original_body, original_settings)

    # Validate AI config and peers
    _resolve_ai_config(original_body)
    resolved_peers = _validate_peer_configs(original_body, merged)

    return await _enqueue_analysis_job(
        original_body,
        merged,
        resolved_peers,
        background_tasks,
        base_url,
        message_prefix="Re-analysis",
    )


@app.get("/results/{job_id}", response_model=None)
async def get_job_result(
    request: Request, job_id: str, response: Response, _: None = Depends(_bind_job_id)
):
    """Retrieve stored result by job_id, or serve SPA for browser requests."""
    # Content negotiation: browsers requesting HTML get the SPA
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        result = await get_result(job_id)
        if result and result.get("status") in IN_PROGRESS_STATUSES:
            return RedirectResponse(url=f"/status/{job_id}", status_code=302)
        return _serve_spa()

    logger.debug(f"GET /results/{job_id}")
    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    _attach_result_links(result, _extract_base_url(), job_id)
    settings = get_settings()
    result["capabilities"] = _build_capabilities(settings)
    if result.get("status") in IN_PROGRESS_STATUSES:
        response.status_code = 202
    return result


def _find_test_in_children(
    children: list[dict],
    test_name: str,
    child_job_name: str,
    child_build_number: int = 0,
) -> bool:
    """Recursively search child job analyses for a test."""
    for child in children:
        if child.get("job_name") == child_job_name and (
            child_build_number == 0 or child.get("build_number") == child_build_number
        ):
            for f in child.get("failures", []):
                if f.get("test_name") == test_name:
                    return True
        if _find_test_in_children(
            child.get("failed_children", []),
            test_name,
            child_job_name,
            child_build_number,
        ):
            return True
    return False


async def _validate_test_name_in_result(
    job_id: str, test_name: str, child_job_name: str = "", child_build_number: int = 0
) -> None:
    """Validate that a test_name exists in the stored result.

    Also checks job status before looking for the test -- if the job is still
    pending/running or has failed, the caller gets a clear status-based error
    instead of a misleading "Test not found".
    """
    logger.debug(
        f"_validate_test_name_in_result: job_id={job_id}, test_name={test_name}, child_job_name={child_job_name}"
    )
    stored = await storage.get_result(job_id)
    if not stored:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    status = stored.get("status", "unknown")
    if status in IN_PROGRESS_STATUSES:
        raise HTTPException(status_code=202, detail=f"Job {job_id} is still pending")
    if status == "failed":
        raise HTTPException(status_code=409, detail=f"Job {job_id} failed")

    result_data = stored.get("result") or {}

    if child_job_name:
        if _find_test_in_children(
            result_data.get("child_job_analyses", []),
            test_name,
            child_job_name,
            child_build_number,
        ):
            return
        raise HTTPException(
            status_code=400,
            detail=f"Test '{test_name}' not found in child job '{child_job_name}' of job {job_id}",
        )
    else:
        for f in result_data.get("failures", []):
            if f.get("test_name") == test_name:
                return
        raise HTTPException(
            status_code=400, detail=f"Test '{test_name}' not found in job {job_id}"
        )


def _find_failure_in_children(
    children: list[dict],
    test_name: str,
    child_job_name: str,
    child_build_number: int = 0,
) -> dict | None:
    """Recursively find a failure dict in child job analyses."""
    for child in children:
        if child.get("job_name") == child_job_name and (
            child_build_number == 0 or child.get("build_number") == child_build_number
        ):
            for f in child.get("failures", []):
                if f.get("test_name") == test_name:
                    return f
        result = _find_failure_in_children(
            child.get("failed_children", []),
            test_name,
            child_job_name,
            child_build_number,
        )
        if result is not None:
            return result
    return None


def _find_failure_in_result(
    result_data: dict,
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> dict | None:
    """Find a specific failure dict in the stored result data."""
    if child_job_name:
        return _find_failure_in_children(
            result_data.get("child_job_analyses", []),
            test_name,
            child_job_name,
            child_build_number,
        )
    for f in result_data.get("failures", []):
        if f.get("test_name") == test_name:
            return f
    return None


async def _get_error_signature(
    job_id: str,
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> str:
    """Look up the error_signature for a test from stored result data."""
    stored = await storage.get_result(job_id)
    if not stored or not stored.get("result"):
        return ""
    failure = _find_failure_in_result(
        stored["result"],
        test_name,
        child_job_name,
        child_build_number,
    )
    return failure.get("error_signature", "") if failure else ""


async def _resolve_effective_failure(
    job_id: str,
    failure: FailureAnalysis,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> FailureAnalysis:
    """Resolve the effective classification and return an updated failure.

    Checks test_classifications for overrides. If an override exists,
    updates the failure's classification and clears stale subtype data.
    Falls back to the original classification if no override found.
    """
    effective_cls = await get_effective_classification(
        job_id, failure.test_name, child_job_name, child_build_number
    )
    if not effective_cls or effective_cls == failure.analysis.classification:
        return failure
    updates: dict = {"classification": effective_cls}
    if effective_cls == "CODE ISSUE":
        updates["product_bug_report"] = False
    elif effective_cls == "PRODUCT BUG":
        updates["code_fix"] = False
    elif effective_cls == "INFRASTRUCTURE":
        updates["code_fix"] = False
        updates["product_bug_report"] = False
    return failure.model_copy(
        update={"analysis": failure.analysis.model_copy(update=updates)}
    )


@app.get("/results/{job_id}/comments")
async def get_comments(job_id: str, _: None = Depends(_bind_job_id)) -> dict:
    """Get all comments and review states for a job."""
    logger.debug(f"GET /results/{job_id}/comments")
    comments = await storage.get_comments_for_job(job_id)
    reviews = await storage.get_reviews_for_job(job_id)
    return {"comments": comments, "reviews": reviews}


@app.post("/results/{job_id}/comments", status_code=201)
async def add_comment(
    job_id: str,
    body: AddCommentRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Add a comment to a test failure."""
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/comments: test_name={body.test_name}")
    await _validate_test_name_in_result(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    error_signature = await _get_error_signature(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    username = request.state.username
    try:
        comment_id = await storage.add_comment(
            job_id=job_id,
            test_name=body.test_name,
            comment=body.comment,
            child_job_name=body.child_job_name,
            child_build_number=body.child_build_number,
            error_signature=error_signature,
            username=username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Detect @mentions and send notifications (best-effort, fire-and-forget)
    settings = get_settings()
    if settings.web_push_enabled and username:
        mentioned = detect_mentions(body.comment)
        if mentioned:
            vapid_cfg = get_vapid_config()
            if vapid_cfg and "private_key" in vapid_cfg and "claim_email" in vapid_cfg:
                task = asyncio.create_task(
                    send_mention_notifications(
                        mentioned_usernames=mentioned,
                        comment_author=username,
                        job_id=job_id,
                        test_name=body.test_name,
                        vapid_private_key=vapid_cfg["private_key"],
                        vapid_claim_email=vapid_cfg["claim_email"],
                        public_base_url=settings.public_base_url,
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

    return {"id": comment_id}


@app.delete("/results/{job_id}/comments/{comment_id}")
async def delete_comment_endpoint(
    job_id: str, comment_id: int, request: Request, _: None = Depends(_bind_job_id)
) -> dict:
    """Delete a comment. Username scoping is a UI courtesy.

    Admin users can delete any comment. Regular users can only delete
    their own comments (matched by username).
    """
    _check_allow_list(request)
    logger.debug(f"DELETE /results/{job_id}/comments/{comment_id}")
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")

    # Admins can delete any comment; regular users only their own
    delete_username = "" if request.state.is_admin else username
    deleted = await storage.delete_comment(comment_id, delete_username, job_id=job_id)
    if not deleted:
        detail = (
            "Comment not found"
            if request.state.is_admin
            else "Comment not found or not owned by you"
        )
        raise HTTPException(status_code=404, detail=detail)

    return {"status": "deleted"}


@app.put("/results/{job_id}/reviewed")
async def set_reviewed(
    job_id: str,
    body: SetReviewedRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Toggle the reviewed state for a test failure."""
    _check_allow_list(request)
    logger.debug(
        f"PUT /results/{job_id}/reviewed: test_name={body.test_name}, reviewed={body.reviewed}"
    )
    await _validate_test_name_in_result(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )
    username = request.state.username
    try:
        await storage.set_reviewed(
            job_id=job_id,
            test_name=body.test_name,
            reviewed=body.reviewed,
            child_job_name=body.child_job_name,
            child_build_number=body.child_build_number,
            username=username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "reviewed_by": username if body.reviewed else "",
    }


@app.post("/results/{job_id}/enrich-comments")
async def enrich_comments(
    job_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(_bind_job_id),
) -> dict:
    """Fetch live statuses for GitHub PRs and Jira tickets found in comments."""
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/enrich-comments")
    from jenkins_job_insight.comment_enrichment import (
        detect_github_issues,
        detect_github_prs,
        detect_jira_keys,
        fetch_github_issue_status,
        fetch_github_pr_status,
        fetch_jira_ticket_status,
    )

    comments = await storage.get_comments_for_job(job_id)
    logger.debug(f"enrich_comments: job_id={job_id}, comments_count={len(comments)}")

    # Detect Cloud vs Server/DC auth once, matching JiraClient logic:
    # - Cloud: jira_email is set -> Basic auth with email:token
    # - Server/DC: no email -> Bearer PAT
    # Token resolution: prefer jira_api_token, fall back to jira_pat
    auth: tuple[str, str] | None = None
    auth_headers: dict[str, str] = {}
    jira_url: str | None = settings.jira_url if settings.jira_enabled else None
    jira_active = bool(jira_url)

    if jira_active and jira_url:
        jira_token = ""
        if settings.jira_api_token:
            jira_token = settings.jira_api_token.get_secret_value()
        elif settings.jira_pat:
            jira_token = settings.jira_pat.get_secret_value()

        if settings.jira_email and jira_token:
            # Cloud: Basic auth
            auth = (settings.jira_email, jira_token)
        elif jira_token:
            # Server/DC: Bearer
            auth_headers["Authorization"] = f"Bearer {jira_token}"

    github_token = (
        settings.github_token.get_secret_value() if settings.github_token else None
    )

    # Collect all enrichment tasks for parallel execution
    tasks: list[Coroutine[Any, Any, Any]] = []
    task_map: dict[int, tuple[str, dict]] = {}

    for c in comments:
        for pr in detect_github_prs(c["comment"]):
            idx = len(tasks)
            tasks.append(
                fetch_github_pr_status(
                    pr["owner"],
                    pr["repo"],
                    pr["number"],
                    token=github_token,
                )
            )
            task_map[idx] = (
                str(c["id"]),
                {
                    "type": "github_pr",
                    "key": f"{pr['owner']}/{pr['repo']}#{pr['number']}",
                },
            )

        for issue in detect_github_issues(c["comment"]):
            idx = len(tasks)
            tasks.append(
                fetch_github_issue_status(
                    issue["owner"],
                    issue["repo"],
                    issue["number"],
                    token=github_token,
                )
            )
            task_map[idx] = (
                str(c["id"]),
                {
                    "type": "github_issue",
                    "key": f"{issue['owner']}/{issue['repo']}#{issue['number']}",
                },
            )

        if jira_active and jira_url:
            for key in detect_jira_keys(c["comment"]):
                idx = len(tasks)
                tasks.append(
                    fetch_jira_ticket_status(
                        jira_url,
                        key,
                        auth_headers,
                        ssl_verify=settings.jira_ssl_verify,
                        auth=auth,
                    )
                )
                task_map[idx] = (str(c["id"]), {"type": "jira", "key": key})

    enrichments: dict[str, list[dict]] = {}
    logger.debug(f"enrich_comments: job_id={job_id}, enrichment_tasks={len(tasks)}")

    if tasks:
        results = await run_parallel_with_limit(tasks)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.debug("Enrichment task %d failed: %s", i, result)
                continue
            if result is None:
                continue
            comment_id, info = task_map[i]
            info["status"] = result
            enrichments.setdefault(comment_id, []).append(info)

    logger.debug(
        f"enrich_comments: job_id={job_id}, enrichments_count={len(enrichments)}"
    )
    return {"enrichments": enrichments}


def _resolve_analyzed_repo(
    settings: Settings, result_data: dict
) -> tuple[str, str, str]:
    """Resolve tests repo URL, ref, and token from stored result and settings.

    Resolution order for each component:
      - **URL**: stored ``request_params.tests_repo_url`` → ``settings.tests_repo_url``
      - **ref**: stored ``request_params.tests_repo_ref`` → ref parsed from URL suffix
      - **token**: decrypted ``request_params.tests_repo_token`` → ``settings.tests_repo_token``

    Returns:
        Tuple of ``(url, ref, token)`` — any element may be an empty string.
    """
    request_params = result_data.get("request_params", {})

    # URL: prefer stored, fall back to server default
    repo_spec = str(request_params.get("tests_repo_url", ""))
    if not repo_spec:
        repo_spec = str(settings.tests_repo_url or "")
    url, parsed_ref = parse_repo_ref(repo_spec)

    # Ref: prefer explicit stored ref, then parsed from URL suffix
    ref = str(request_params.get("tests_repo_ref", ""))
    if not ref:
        ref = parsed_ref

    # Token: decrypt stored value, fall back to server token
    token = ""
    if request_params:
        decrypted = decrypt_sensitive_fields(request_params)
        stored_token = decrypted.get("tests_repo_token", "")
        if not _is_encrypted_value(stored_token):
            token = stored_token
    if not token and settings.tests_repo_token:
        token = settings.tests_repo_token.get_secret_value()

    return url, ref, token


def _resolve_github_repo_url(
    body_repo_url: str, settings: Settings, result_data: dict
) -> str:
    """Validate and resolve GitHub repo URL from request body or fallback.

    If *body_repo_url* is provided it is validated via ``_parse_github_repo_url``
    (which raises ``ValueError`` on bad input).  Otherwise falls back to
    ``_resolve_analyzed_repo``.

    Raises:
        HTTPException: 400 when *body_repo_url* is invalid.
    """
    if body_repo_url:
        try:
            _parse_github_repo_url(body_repo_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid github_repo_url: {exc}"
            ) from exc
        return body_repo_url
    url, _ref, _token = _resolve_analyzed_repo(settings, result_data)
    return url


async def _load_effective_failure(
    job_id: str,
    test_name: str,
    child_job_name: str,
    child_build_number: int,
) -> tuple[FailureAnalysis, dict]:
    """Shared lookup for preview/create endpoints: validate, load, and resolve a failure.

    Returns:
        Tuple of (resolved FailureAnalysis, result_data dict).

    Raises:
        HTTPException: 404 if the job is not found, 400 if the test is not found.
    """
    await _validate_test_name_in_result(
        job_id, test_name, child_job_name, child_build_number
    )
    stored = await storage.get_result(job_id)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    result_data = stored["result"]
    failure_dict = _find_failure_in_result(
        result_data, test_name, child_job_name, child_build_number
    )
    if not failure_dict:
        raise HTTPException(
            status_code=400,
            detail=f"Test '{test_name}' not found in job {job_id}",
        )
    failure = FailureAnalysis.model_validate(failure_dict)
    failure = await _resolve_effective_failure(
        job_id, failure, child_job_name, child_build_number
    )
    return failure, result_data


@app.get("/results/{job_id}/issue-prompt")
async def get_issue_prompt(
    job_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(_bind_job_id),
) -> dict:
    """Fetch JOB_INSIGHT_ISSUE_PROMPT.md from the test repo via the GitHub Contents API.

    Makes a single HTTP call instead of cloning the repo.  Returns
    ``{"prompt": ""}`` when no repo is configured, the file does not exist,
    or any error occurs.
    """
    _check_allow_list(request)

    stored = await storage.get_result(job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        return {"prompt": ""}

    result_data = stored["result"]

    tests_repo_url, tests_repo_ref, tests_repo_token = _resolve_analyzed_repo(
        settings, result_data
    )
    if not tests_repo_url:
        return {"prompt": ""}

    try:
        owner, repo = _parse_github_repo_url(tests_repo_url)
    except ValueError:
        logger.warning(
            "Cannot parse GitHub repo URL for issue prompt: %s",
            _redact_url(tests_repo_url),
        )
        return {"prompt": ""}

    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{JOB_INSIGHT_ISSUE_PROMPT_FILENAME}"
    if tests_repo_ref:
        api_url += f"?ref={tests_repo_ref}"

    headers: dict[str, str] = {"Accept": "application/vnd.github.raw+json"}
    if tests_repo_token:
        headers["Authorization"] = f"Bearer {tests_repo_token}"

    logger.debug(
        "Fetching issue prompt from %s/%s ref=%s",
        owner,
        repo,
        tests_repo_ref or "(default)",
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(api_url, headers=headers)

        if resp.status_code == 200:
            content = resp.text
            logger.debug(
                "Issue prompt found (%d chars) for job %s", len(content), job_id
            )
            return {"prompt": content}

        if resp.status_code == 404:
            logger.debug("No issue prompt file in %s/%s", owner, repo)
            return {"prompt": ""}

        logger.warning(
            "Failed to fetch issue prompt from %s/%s: HTTP %d",
            owner,
            repo,
            resp.status_code,
        )
        return {"prompt": ""}

    except httpx.TimeoutException:
        logger.warning("Timeout fetching issue prompt from %s/%s", owner, repo)
        return {"prompt": ""}
    except Exception:  # noqa: BLE001 — never crash; empty prompt is safe
        logger.warning(
            "Failed to fetch issue prompt from %s/%s",
            owner,
            repo,
            exc_info=True,
        )
        return {"prompt": ""}


# NOTE: Preview/create bug endpoints intentionally bypass _merge_settings().
# These are server-level operations (GITHUB_TOKEN, TESTS_REPO_URL, Jira config)
# that act on behalf of the server, not per-request analysis overrides. The
# credentials and repo targets are fixed at deployment, not caller-supplied.
@app.post("/results/{job_id}/preview-github-issue")
async def preview_github_issue(
    job_id: str,
    body: PreviewIssueRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(_bind_job_id),
) -> dict:
    """Generate preview content for a GitHub issue from a failure analysis."""
    _check_allow_list(request)
    logger.debug(
        f"POST /results/{job_id}/preview-github-issue: test_name={body.test_name}"
    )
    if settings.enable_github_issues is False:
        raise HTTPException(
            status_code=403,
            detail="GitHub issue creation is disabled on this server",
        )
    failure, result_data = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    # AI config is best-effort for preview — fallback content is generated if not configured
    ai_provider = body.ai_provider or AI_PROVIDER
    ai_model = body.ai_model or AI_MODEL
    base_url = _extract_base_url()
    effective_include_links = body.include_links and bool(base_url)
    report_url, jenkins_url = _build_report_context(
        include_links=effective_include_links,
        base_url=base_url,
        job_id=job_id,
        result_data=result_data,
    )

    issue_prompt = (body.issue_prompt or "").strip()
    content = await generate_github_issue_content(
        failure=failure,
        report_url=report_url,
        ai_provider=ai_provider,
        ai_model=ai_model,
        jenkins_url=jenkins_url,
        include_links=effective_include_links,
        job_id=job_id,
        issue_prompt=issue_prompt,
    )

    # Duplicate detection (best-effort: failures must not break preview)
    tests_repo_url = _resolve_github_repo_url(
        body.github_repo_url, settings, result_data
    )
    github_token = (body.github_token or "").strip() or (
        settings.github_token.get_secret_value() if settings.github_token else ""
    )
    similar: list[dict] = []
    if tests_repo_url and github_token:
        try:
            similar = await search_github_duplicates(
                title=content["title"],
                repo_url=tests_repo_url,
                github_token=github_token,
            )
        except Exception:
            logger.warning(
                "GitHub duplicate search failed for job_id=%s",
                job_id,
                exc_info=True,
            )

    return {
        "title": content["title"],
        "body": content["body"],
        "similar_issues": similar,
    }


@app.post("/results/{job_id}/preview-jira-bug")
async def preview_jira_bug(
    job_id: str,
    body: PreviewIssueRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(_bind_job_id),
) -> dict:
    """Generate preview content for a Jira bug from a failure analysis."""
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/preview-jira-bug: test_name={body.test_name}")
    if not _jira_issue_creation_enabled(settings):
        raise HTTPException(
            status_code=403,
            detail="Jira issue creation is disabled on this server",
        )
    if not settings.jira_url:
        raise HTTPException(
            status_code=400,
            detail="Jira URL is not configured on the server",
        )
    failure, result_data = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    # AI config is best-effort for preview — fallback content is generated if not configured
    ai_provider = body.ai_provider or AI_PROVIDER
    ai_model = body.ai_model or AI_MODEL
    base_url = _extract_base_url()
    effective_include_links = body.include_links and bool(base_url)
    report_url, jenkins_url = _build_report_context(
        include_links=effective_include_links,
        base_url=base_url,
        job_id=job_id,
        result_data=result_data,
    )

    issue_prompt = (body.issue_prompt or "").strip()
    content = await generate_jira_bug_content(
        failure=failure,
        report_url=report_url,
        ai_provider=ai_provider,
        ai_model=ai_model,
        jenkins_url=jenkins_url,
        include_links=effective_include_links,
        job_id=job_id,
        issue_prompt=issue_prompt,
    )

    # Duplicate detection (best-effort: failures must not break preview)
    similar: list[dict] = []
    effective_jira_settings = _build_effective_jira_settings(
        settings, body.jira_token, body.jira_email, body.jira_project_key
    )
    if (
        _has_jira_credentials(effective_jira_settings)
        and effective_jira_settings.jira_url
        and effective_jira_settings.jira_project_key
    ):
        try:
            similar = await search_jira_duplicates(
                title=content["title"],
                settings=effective_jira_settings,
            )
        except Exception:
            logger.warning(
                "Jira duplicate search failed for job_id=%s",
                job_id,
                exc_info=True,
            )

    return {
        "title": content["title"],
        "body": content["body"],
        "similar_issues": similar,
    }


def _has_jira_credentials(settings: Settings) -> bool:
    """Return True if the given settings contain usable Jira credentials."""
    return bool(
        (settings.jira_api_token and settings.jira_api_token.get_secret_value())
        or (settings.jira_pat and settings.jira_pat.get_secret_value())
    )


def _jira_issue_creation_enabled(settings: Settings) -> bool:
    """Check whether Jira issue creation is enabled.

    Controlled only by ``ENABLE_JIRA_ISSUES``.  Defaults to enabled
    when not explicitly set.  Independent of ``ENABLE_JIRA`` (which
    controls Jira enrichment during analysis).
    """
    return settings.enable_jira_issues is not False


def _build_capabilities(settings: Settings) -> dict[str, bool | str]:
    """Build the capabilities dict for API responses."""
    return {
        "github_issues_enabled": settings.enable_github_issues is not False,
        "jira_issues_enabled": _jira_issue_creation_enabled(settings),
        "server_github_token": bool(
            settings.github_token and settings.github_token.get_secret_value()
        ),
        "server_jira_token": bool(
            (settings.jira_api_token and settings.jira_api_token.get_secret_value())
            or (settings.jira_pat and settings.jira_pat.get_secret_value())
        ),
        "server_jira_email": bool(settings.jira_email),
        "server_jira_project_key": settings.jira_project_key or "",
        "reportportal": settings.reportportal_enabled,
        "reportportal_project": settings.reportportal_project or "",
        "feedback_enabled": settings.feedback_enabled
        and bool(AI_PROVIDER)
        and bool(AI_MODEL),
    }


def _build_effective_jira_settings(
    settings: Settings,
    user_jira_token: str,
    user_jira_email: str,
    user_jira_project_key: str = "",
) -> Settings:
    """Build effective settings with user Jira credentials overriding server defaults.

    Uses ``model_copy()`` to follow the same pattern as ``_merge_settings()``.
    The user token is set as ``jira_api_token`` and ``jira_pat`` is cleared so
    the user token takes precedence in all auth resolution paths
    (``_resolve_jira_auth`` prefers PAT over API token, so leaving server PAT
    intact would bypass the user override).

    When the user provides a token but no email, ``jira_email`` is also cleared
    to prevent pairing the server's email with the user's token (which would
    incorrectly trigger Cloud Basic auth). Cloud users must explicitly provide
    their email; without it, the non-Cloud auth path is used, which may
    fail against Cloud-only Jira hosts.

    An optional *user_jira_project_key* overrides the server-level project key
    so that duplicate searches and bug creation target the user's chosen project.
    """
    overrides: dict = {}
    if user_jira_token and user_jira_token.strip():
        overrides["jira_api_token"] = SecretStr(user_jira_token.strip())
        overrides["jira_pat"] = None
        overrides["jira_email"] = (
            user_jira_email.strip()
            if user_jira_email and user_jira_email.strip()
            else None
        )
    if user_jira_project_key and user_jira_project_key.strip():
        overrides["jira_project_key"] = user_jira_project_key.strip()
    if not overrides:
        return settings
    return settings.model_copy(update=overrides)


def _require_tracker_url(result: dict, tracker_name: str) -> str:
    """Extract and validate the issue URL from a tracker API response.

    Raises:
        HTTPException: 502 when the response does not contain a ``url`` field.
    """
    issue_url = str(result.get("url", ""))
    if not issue_url:
        raise HTTPException(
            status_code=502,
            detail=f"{tracker_name} API returned unexpected response: missing url",
        )
    return issue_url


async def _add_tracker_comment(
    tracker_label: str,
    job_id: str,
    body: CreateIssueRequest,
    result: dict,
    username: str,
) -> int:
    """Best-effort auto-add a comment linking to the created tracker issue.

    Args:
        tracker_label: Human-readable tracker name (e.g. "GitHub Issue", "Jira Bug").
        job_id: Analysis job ID.
        body: The create-issue request (carries test_name, child_job_name, etc.).
        result: The tracker API response (must contain ``url`` and optionally ``key``).
        username: Username from the request cookie.

    Returns:
        The comment ID on success, or ``0`` on failure.
    """
    comment_id = 0
    issue_url = str(result.get("url", ""))
    try:
        if not issue_url:
            raise ValueError("Tracker response missing url")
        key = result.get("key", "")
        key_suffix = f" [{key}]" if key else ""
        comment_text = f"{tracker_label}{key_suffix}: [{body.title}]({issue_url})"
        error_signature = await _get_error_signature(
            job_id, body.test_name, body.child_job_name, body.child_build_number
        )
        comment_id = await storage.add_comment(
            job_id=job_id,
            test_name=body.test_name,
            comment=comment_text,
            child_job_name=body.child_job_name,
            child_build_number=body.child_build_number,
            error_signature=error_signature,
            username=username,
        )
    except Exception:
        logger.warning(
            f"Failed to add comment after {tracker_label} creation "
            f"for job_id={job_id}, issue url={issue_url or '<missing>'}",
            exc_info=True,
        )
    return comment_id


@app.post("/results/{job_id}/create-github-issue", status_code=201)
async def create_github_issue_endpoint(
    job_id: str,
    body: CreateIssueRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(_bind_job_id),
) -> dict:
    """Create a GitHub issue from a failure analysis."""
    _check_allow_list(request)
    logger.debug(
        f"POST /results/{job_id}/create-github-issue: test_name={body.test_name}"
    )
    if settings.enable_github_issues is False:
        raise HTTPException(
            status_code=403,
            detail="GitHub issue creation is disabled on this server",
        )
    github_token = (body.github_token or "").strip() or (
        settings.github_token.get_secret_value() if settings.github_token else ""
    )
    if not github_token:
        raise HTTPException(
            status_code=400,
            detail="GitHub token is required. Provide a token in your profile settings or configure GITHUB_TOKEN on the server.",
        )

    _failure, _result_data = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    username = request.state.username
    issue_body = body.body
    if username:
        issue_body += f"\n\n---\n_Reported by: {username} via jenkins-job-insight_"

    tests_repo_url = _resolve_github_repo_url(
        body.github_repo_url, settings, _result_data
    )
    if not tests_repo_url:
        raise HTTPException(
            status_code=400,
            detail="No test repository URL available. The job was analyzed without tests_repo_url.",
        )

    try:
        result = await create_github_issue(
            title=body.title,
            body=issue_body,
            repo_url=tests_repo_url,
            github_token=github_token,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid TESTS_REPO_URL: {exc}",
        ) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise HTTPException(
                status_code=401,
                detail="GitHub token is invalid or expired. Update your token in settings.",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API error: {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API unreachable: {exc}",
        ) from exc
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API returned unexpected response: {exc}",
        ) from exc

    issue_url = _require_tracker_url(result, "GitHub")

    comment_id = await _add_tracker_comment(
        "GitHub Issue", job_id, body, result, username
    )

    return {
        "url": issue_url,
        "number": result.get("number", 0),
        "key": "",
        "title": body.title,
        "comment_id": comment_id,
    }


@app.post("/results/{job_id}/create-jira-bug", status_code=201)
async def create_jira_bug_endpoint(
    job_id: str,
    body: CreateIssueRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    _: None = Depends(_bind_job_id),
) -> dict:
    """Create a Jira bug from a failure analysis."""
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/create-jira-bug: test_name={body.test_name}")

    if not _jira_issue_creation_enabled(settings):
        raise HTTPException(
            status_code=403,
            detail="Jira issue creation is disabled on this server",
        )
    if not settings.jira_url:
        raise HTTPException(
            status_code=400,
            detail="Jira URL is not configured on the server",
        )

    _failure, _result_data = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    username = request.state.username
    bug_body = body.body
    if username:
        bug_body += f"\n\n----\nReported by: {username} via jenkins-job-insight"

    try:
        effective_jira_settings = _build_effective_jira_settings(
            settings, body.jira_token, body.jira_email, body.jira_project_key
        )
        if not effective_jira_settings.jira_project_key:
            raise HTTPException(
                status_code=400,
                detail="Jira project key is required. Provide it in the request or configure JIRA_PROJECT_KEY on the server.",
            )
        if not _has_jira_credentials(effective_jira_settings):
            raise HTTPException(
                status_code=400,
                detail="Jira token is required. Provide a token in your profile settings or configure Jira credentials on the server.",
            )
        result = await create_jira_bug(
            title=body.title,
            body=bug_body,
            settings=effective_jira_settings,
            project_key=body.jira_project_key,
            security_level=body.jira_security_level,
            issue_type=body.jira_issue_type,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise HTTPException(
                status_code=401,
                detail="Jira token is invalid or expired. Update your token in settings.",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"Jira API error: {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Jira API unreachable: {exc}",
        ) from exc
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Jira API returned unexpected response: {exc}",
        ) from exc

    issue_url = _require_tracker_url(result, "Jira")

    comment_id = await _add_tracker_comment("Jira Bug", job_id, body, result, username)

    return {
        "url": issue_url,
        "key": result.get("key", ""),
        "title": body.title,
        "comment_id": comment_id,
    }


@app.post("/results/{job_id}/push-reportportal", response_model=ReportPortalPushResult)
async def push_to_reportportal(
    job_id: str,
    request: Request,
    child_job_name: str | None = Query(
        default=None, description="Child job name for pipeline child push"
    ),
    child_build_number: int | None = Query(
        default=None, description="Child build number for pipeline child push"
    ),
    settings: Settings = Depends(get_settings),
    _: None = Depends(_bind_job_id),
) -> dict:
    """Push JJI classifications into Report Portal test items.

    Finds the matching RP launch, matches failed items to JJI failures,
    and updates each item's defect type and comment.
    """
    _check_allow_list(request)
    if not settings.reportportal_enabled:
        raise HTTPException(
            status_code=400,
            detail="Report Portal integration is disabled or not configured",
        )

    stored = await get_result(job_id)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result_data = stored["result"]

    try:
        push_result = await _execute_rp_push(
            job_id,
            result_data,
            settings,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
        )
        return push_result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _rp_push_error_result(
    message: str,
    *,
    launch_id: int | None = None,
) -> dict:
    """Build a standard RP push failure response."""
    return {
        "pushed": 0,
        "unmatched": [],
        "errors": [message],
        "launch_id": launch_id,
    }


def _log_and_return_rp_error(
    user_msg: str,
    *,
    log_msg: str = "",
    job_name: str = "",
    build_number: int | None = None,
    jenkins_url: str = "",
    launch_id: int | None = None,
) -> dict:
    """Log an RP push error and return the standardised error dict.

    Centralises the repeated log-then-return pattern so each call-site
    is a single expression instead of a multi-line logger.error + return
    block.

    Args:
        user_msg: Short, user-facing error for the API response.
        log_msg: Detailed message for the server log.  Falls back to
            *user_msg* when empty.
    """
    detail = log_msg or user_msg
    if launch_id is not None:
        logger.error(
            f"RP push failed: {detail}, job='{job_name}' #{build_number}, launch_id={launch_id}"
        )
    elif jenkins_url:
        logger.error(
            f"RP push failed: {detail}, job='{job_name}' #{build_number}, jenkins_url='{jenkins_url}'"
        )
    elif build_number is not None:
        logger.error(f"RP push failed: {detail}, job='{job_name}' #{build_number}")
    else:
        logger.error(f"RP push failed: {detail}")
    return _rp_push_error_result(
        user_msg,
        launch_id=launch_id,
    )


def _rp_error_message(exc: Exception, operation: str) -> tuple[str, str]:
    """Build a short user-facing message and a detailed log message.

    Returns:
        Tuple of ``(user_message, log_detail)``.
        *user_message* is short and suitable for API responses.
        *log_detail* contains the full exception context for server logs.
    """
    detail = ""
    rp_message = ""
    status = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = str(resp.status_code)
        try:
            rp_body = resp.json()
            raw = rp_body.get("message") if isinstance(rp_body, dict) else None
            # RP JSON "message" field — short, user-friendly
            rp_message = raw if isinstance(raw, str) else ""
            # Full response text — log only
            detail = resp.text or ""
        except Exception:
            detail = resp.text or ""
    else:
        detail = str(exc) if str(exc) else ""

    # User message: short — operation + status + RP message (if any)
    if status:
        user_msg = f"Error {operation} (HTTP {status})"
        if rp_message:
            user_msg += f": {rp_message}"
    else:
        user_msg = f"Error {operation}"

    # Log message: full technical detail
    log_msg = f"{type(exc).__name__} {operation}"
    if status:
        log_msg = f"{status} ({type(exc).__name__}) {operation}"
    if detail:
        log_msg += f": {detail}"

    return user_msg, log_msg


async def _execute_rp_push(
    job_id: str,
    result_data: dict,
    settings: Settings,
    *,
    child_job_name: str | None = None,
    child_build_number: int | None = None,
) -> dict:
    """Shared logic for pushing classifications to Report Portal.

    Creates a ReportPortalClient, finds the matching launch, matches
    failed items to JJI failures, and pushes classifications.

    Args:
        job_id: The analysis job identifier.
        result_data: Stored result dict containing failures and Jenkins metadata.
        settings: Application settings with Report Portal configuration.
        child_job_name: Optional child job name for scoping push to a child.
        child_build_number: Optional child build number (required with child_job_name).

    Returns:
        Dict with keys: ``pushed``, ``unmatched``, ``errors``, ``launch_id``.
    """
    base_url = _extract_base_url()
    if not base_url:
        raise ValueError(
            "PUBLIC_BASE_URL must be set to push to Report Portal"
            " (relative URLs resolve against the RP domain)"
        )
    report_url = f"{base_url}/results/{job_id}"

    # Scope to child job when requested
    if child_job_name is not None:
        if child_build_number is None:
            raise ValueError(
                "child_build_number is required when child_job_name is provided"
            )
        child = _find_child_job(
            result_data.get("child_job_analyses", []),
            child_job_name,
            child_build_number,
        )
        if not child:
            raise ValueError(
                f"Child job '{child_job_name}' #{child_build_number} not found"
            )
        # Use child job's data for RP push
        result_data = child
        # Build anchor fragment for the child section (URL-encoded job name)
        anchor = (
            f"child-{urllib.parse.quote(child_job_name, safe='')}-{child_build_number}"
        )
        report_url = f"{report_url}#{anchor}"

    failures_data = result_data.get("failures", [])
    if not failures_data:
        return _rp_push_error_result(
            "No failures to push to Report Portal.",
        )

    # Called only when reportportal_enabled is True, which guarantees these
    # fields are set (see Settings.reportportal_enabled property).  Explicit
    # checks narrow the Optional types for mypy and survive python -O.
    if settings.reportportal_url is None:
        raise RuntimeError("reportportal_url is required when Report Portal is enabled")
    if settings.reportportal_api_token is None:
        raise RuntimeError(
            "reportportal_api_token is required when Report Portal is enabled"
        )
    if settings.reportportal_project is None:
        raise RuntimeError(
            "reportportal_project is required when Report Portal is enabled"
        )

    try:
        rp_client_ctx = ReportPortalClient(
            url=settings.reportportal_url,
            token=settings.reportportal_api_token.get_secret_value(),
            project=settings.reportportal_project,
            verify_ssl=settings.reportportal_verify_ssl,
        )
    except Exception as exc:
        user_msg, log_msg = _rp_error_message(
            exc,
            "connecting to Report Portal",
        )
        # Include the RP URL in the log message (not user-facing) so
        # operators can identify which RP instance failed.
        log_msg = f"{log_msg}, reportportal_url='{settings.reportportal_url}'"
        return _log_and_return_rp_error(user_msg, log_msg=log_msg)

    with rp_client_ctx as rp_client:
        jenkins_url = result_data.get("jenkins_url", "")
        job_name = result_data.get("job_name", "")
        build_number = result_data.get("build_number", 0)

        logger.debug(
            "RP push: searching for launch job='%s' #%s, jenkins_url='%s'",
            job_name,
            build_number,
            jenkins_url,
        )
        try:
            launch_id = await asyncio.to_thread(
                rp_client.find_launch, job_name, jenkins_url
            )
        except AmbiguousLaunchError as exc:
            logger.warning(
                "RP push: %s",
                exc,
            )
            return _rp_push_error_result(
                f"Ambiguous RP launch: found {exc.count} launches."
                f" Remove duplicate launches to disambiguate."
            )
        except Exception as exc:
            user_msg, log_msg = _rp_error_message(exc, "searching RP launches")
            return _log_and_return_rp_error(
                user_msg,
                log_msg=log_msg,
                job_name=job_name,
                build_number=build_number,
                jenkins_url=jenkins_url,
            )

        if launch_id is None:
            return _log_and_return_rp_error(
                "No Report Portal launch found. "
                "Ensure the Jenkins build URL is in the RP launch description.",
                job_name=job_name,
                build_number=build_number,
                jenkins_url=jenkins_url,
            )

        try:
            failed_items = await asyncio.to_thread(
                rp_client.get_failed_items, launch_id
            )
        except Exception as exc:
            user_msg, log_msg = _rp_error_message(exc, "fetching failed items from RP")
            return _log_and_return_rp_error(
                user_msg,
                log_msg=log_msg,
                job_name=job_name,
                build_number=build_number,
                launch_id=launch_id,
            )
        if not failed_items:
            logger.debug(
                "RP push: no failed items in launch_id=%d for job='%s'",
                launch_id,
                job_name,
            )
            return _rp_push_error_result(
                "No failed test items found in RP launch.",
                launch_id=launch_id,
            )

        # Build FailureAnalysis objects from stored result
        try:
            jji_failures = [FailureAnalysis.model_validate(f) for f in failures_data]
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Stored result contains invalid failure data: {exc.error_count()} validation error(s)",
            ) from exc

        try:
            matched = await asyncio.to_thread(
                rp_client.match_failures, failed_items, jji_failures
            )
        except Exception as exc:
            user_msg, log_msg = _rp_error_message(exc, "matching RP items to failures")
            return _log_and_return_rp_error(
                user_msg,
                log_msg=log_msg,
                job_name=job_name,
                build_number=build_number,
                launch_id=launch_id,
            )

        if not matched and failed_items and jji_failures:
            rp_names = [item.get("name", "") for item in failed_items]
            jji_names = [f.test_name for f in jji_failures]
            # Full diagnostic detail for server logs only
            log_detail = (
                f"No overlap between {len(failed_items)} RP item(s)"
                f" and {len(jji_failures)} JJI failure(s)."
                f" RP items: {', '.join(rp_names)}."
                f" JJI tests: {', '.join(jji_names)}."
            )
            return _log_and_return_rp_error(
                f"No overlap between {len(failed_items)} RP item(s)"
                f" and {len(jji_failures)} JJI failure(s).",
                log_msg=log_detail,
                job_name=job_name,
                build_number=build_number,
                launch_id=launch_id,
            )

        # Get history classifications for matched tests (concurrent queries)
        unique_test_names = list(
            dict.fromkeys(failure.test_name for _, failure in matched)
        )
        scope_name = child_job_name or ""
        scope_build = child_build_number or 0
        classification_results = await run_parallel_with_limit(
            [
                get_history_classification(job_id, name, scope_name, scope_build)
                for name in unique_test_names
            ]
        )
        history_classifications: dict[str, str] = {}
        for name, result in zip(unique_test_names, classification_results, strict=True):
            if isinstance(result, BaseException):
                logger.debug(
                    "RP push: failed to fetch history classification"
                    " for test='%s', job='%s'",
                    name,
                    job_name,
                )
                continue
            if result:
                history_classifications[name] = result

        try:
            push_result = await asyncio.to_thread(
                rp_client.push_classifications,
                matched,
                report_url,
                history_classifications,
            )
        except Exception as exc:
            user_msg, log_msg = _rp_error_message(
                exc,
                "pushing classifications to RP",
            )
            return _log_and_return_rp_error(
                user_msg,
                log_msg=log_msg,
                job_name=job_name,
                build_number=build_number,
                launch_id=launch_id,
            )

        push_result["launch_id"] = launch_id
        return push_result


def _find_child_job(
    children: list[dict],
    child_job_name: str,
    child_build_number: int,
) -> dict | None:
    """Recursively find a child job by name and build number.

    Args:
        children: List of child job dicts to search.
        child_job_name: Job name to match.
        child_build_number: Build number to match.

    Returns:
        The matching child job dict, or ``None`` if not found.
    """
    for child in children:
        if (
            child.get("job_name") == child_job_name
            and child.get("build_number") == child_build_number
        ):
            return child
        found = _find_child_job(
            child.get("failed_children", []),
            child_job_name,
            child_build_number,
        )
        if found:
            return found
    return None


def _patch_failure_classification(
    failures: list[dict], test_name: str, classification: str
) -> None:
    """Patch classification for matching failures in a list.

    Also clears stale subtype fields:
    - CODE ISSUE: clears product_bug_report
    - PRODUCT BUG: clears code_fix
    - INFRASTRUCTURE: clears both product_bug_report and code_fix
    """
    for f in failures:
        if f.get("test_name") == test_name:
            analysis = f.get("analysis", {})
            if isinstance(analysis, dict):
                analysis["classification"] = classification
                if classification == "CODE ISSUE":
                    analysis.pop("product_bug_report", None)
                elif classification == "PRODUCT BUG":
                    analysis.pop("code_fix", None)
                elif classification == "INFRASTRUCTURE":
                    analysis.pop("product_bug_report", None)
                    analysis.pop("code_fix", None)


def _apply_classification_override(
    result_data: dict,
    test_name: str,
    classification: str,
    child_job_name: str,
    child_build_number: int,
) -> None:
    """Mutate result_data to apply a classification override to matching failures."""
    if child_job_name:
        # Override in child job failures
        for child in result_data.get("child_job_analyses", []):
            if child.get("job_name") == child_job_name and (
                child_build_number == 0
                or child.get("build_number") == child_build_number
            ):
                _patch_failure_classification(
                    child.get("failures", []), test_name, classification
                )
            # Also check nested failed_children recursively
            _patch_children(
                child.get("failed_children", []),
                test_name,
                classification,
                child_job_name,
                child_build_number,
            )
    else:
        # Override in top-level failures
        _patch_failure_classification(
            result_data.get("failures", []), test_name, classification
        )


def _patch_children(
    children: list[dict],
    test_name: str,
    classification: str,
    child_job_name: str,
    child_build_number: int,
) -> None:
    """Recursively patch classification in nested children."""
    for child in children:
        if child.get("job_name") == child_job_name and (
            child_build_number == 0 or child.get("build_number") == child_build_number
        ):
            _patch_failure_classification(
                child.get("failures", []), test_name, classification
            )
        _patch_children(
            child.get("failed_children", []),
            test_name,
            classification,
            child_job_name,
            child_build_number,
        )


@app.put("/results/{job_id}/override-classification")
async def override_classification_endpoint(
    job_id: str,
    body: OverrideClassificationRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Override the classification of a failure (CODE ISSUE, PRODUCT BUG, or INFRASTRUCTURE)."""
    _check_allow_list(request)
    logger.debug(
        f"PUT /results/{job_id}/override-classification: test_name={body.test_name}, "
        f"classification={body.classification}"
    )
    await _validate_test_name_in_result(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )
    username = request.state.username

    # Look up parent_job_name for the test_classifications entry
    parent_job_name = await storage.get_parent_job_name_for_test(
        body.test_name, job_id=job_id
    )

    group_tests = await storage.override_classification(
        job_id=job_id,
        test_name=body.test_name,
        classification=body.classification,
        child_job_name=body.child_job_name,
        child_build_number=body.child_build_number,
        username=username,
        parent_job_name=parent_job_name,
    )

    # Persist the override into result_json so page refresh reflects it.
    # Uses an atomic read-modify-write inside a single SQLite transaction
    # so concurrent overrides by different reviewers cannot clobber each other.
    # Wrapped in try/except: the authoritative override is already committed
    # above; a failure here should not turn the response into a 500.
    # Patch ALL tests in the signature group so grouped siblings also update.
    def _patch_group(rd: dict) -> None:
        for t in group_tests:
            _apply_classification_override(
                rd,
                t,
                body.classification,
                body.child_job_name,
                body.child_build_number,
            )

    try:
        await patch_result_json(job_id, _patch_group)
    except Exception:
        logger.warning(
            f"Failed to patch stored result_json after override for job_id={job_id}",
            exc_info=True,
        )

    return {"status": "ok", "classification": body.classification}


@app.get("/results/{job_id}/review-status")
async def get_review_status(job_id: str, _: None = Depends(_bind_job_id)) -> dict:
    """Get review summary for a job (used by dashboard)."""
    logger.debug(f"GET /results/{job_id}/review-status")
    return await storage.get_review_status(job_id)


@app.get("/results")
async def list_job_results(limit: int = Query(50, le=100)) -> list[dict]:
    """List recent analysis jobs."""
    logger.debug(f"GET /results: limit={limit}")
    return await list_results(limit)


@app.delete("/api/results/bulk")
async def bulk_delete_jobs_endpoint(body: BulkDeleteRequest, request: Request) -> dict:
    """Delete multiple jobs and all related data. Admin only."""
    _require_admin(request)

    result = await storage.delete_jobs_bulk(body.job_ids)

    # Audit log each deletion individually
    for job_id in result["deleted"]:
        logger.info(f"[AUDIT] Admin '{request.state.username}' deleted job {job_id}")

    return result


@app.delete("/results/{job_id}")
async def delete_job_endpoint(
    job_id: str, request: Request, _: None = Depends(_bind_job_id)
) -> dict:
    """Delete an analyzed job and all related data. Admin only."""
    _require_admin(request)

    result = await storage.get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    await storage.delete_job(job_id)
    logger.info(f"[AUDIT] Admin '{request.state.username}' deleted job {job_id}")
    return {"status": "deleted", "job_id": job_id}


@app.get("/api/dashboard/active-count")
async def get_active_analysis_count() -> dict:
    """Get count of currently active analyses (running/pending/waiting)."""
    logger.debug("GET /api/dashboard/active-count")
    try:
        count = await storage.count_active_analyses()
    except Exception as exc:
        logger.warning("Failed to get active analysis count", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Failed to get active analysis count",
        ) from exc
    return {"count": count}


@app.get("/api/dashboard")
async def api_dashboard() -> list[dict]:
    """Return dashboard job list as JSON for the React frontend."""
    return await list_results_for_dashboard()


@app.get("/api/capabilities")
async def get_capabilities(settings: Settings = Depends(get_settings)) -> dict:
    """Report server-level feature toggles and credential availability.

    Feature toggles (ENABLE_GITHUB_ISSUES, ENABLE_JIRA_ISSUES) control
    whether issue creation is available at all.  Credential flags tell
    the frontend whether the server has its own tokens configured so the
    UI can decide if user-supplied tokens are required or optional.
    """
    return _build_capabilities(settings)


class JiraProjectsRequest(BaseModel):
    """Request body for listing Jira projects with user credentials."""

    jira_token: str = Field(default="", description="User's Jira token")
    jira_email: str = Field(default="", description="User's Jira email for Cloud auth")
    query: str = Field(default="", description="Search query to filter projects")


def _jira_client_from_body(
    settings: Settings, jira_token: str, jira_email: str
) -> tuple[Settings, str] | None:
    """Normalize user Jira credentials and return effective settings.

    Returns ``(effective_settings, stripped_token)`` when the user supplied a
    non-empty token, or *None* when no usable token is present.
    """
    token = jira_token.strip() if jira_token else ""
    if not token:
        return None
    effective = _build_effective_jira_settings(settings, token, jira_email)
    return effective, token


@app.post("/api/jira-projects")
async def list_jira_projects(
    body: JiraProjectsRequest,
    settings: Settings = Depends(get_settings),
) -> list[dict]:
    """List Jira projects accessible to the user.

    Uses the user's Jira token to list projects they can see.
    Always includes the server's configured project key.
    """
    if not settings.jira_url:
        return []

    result = _jira_client_from_body(settings, body.jira_token, body.jira_email)
    if result is None:
        # No user token — return just the server's configured project
        if settings.jira_project_key:
            return [
                {"key": settings.jira_project_key, "name": settings.jira_project_key}
            ]
        return []

    effective_settings, _ = result

    from jenkins_job_insight.jira import JiraClient

    projects: list[dict] = []
    try:
        async with JiraClient(effective_settings) as client:
            projects = await client.list_projects(query=body.query)
    except Exception:
        logger.warning("Failed to list Jira projects", exc_info=True)

    # Ensure the server's configured project is always included
    if settings.jira_project_key:
        configured_key = settings.jira_project_key
        if not any(p["key"] == configured_key for p in projects):
            projects.insert(0, {"key": configured_key, "name": configured_key})

    return projects


class JiraSecurityLevelsRequest(BaseModel):
    jira_token: str = Field(default="", description="User's Jira token")
    jira_email: str = Field(default="", description="User's Jira email")
    project_key: str = Field(description="Jira project key")


@app.post("/api/jira-security-levels")
async def list_jira_security_levels(
    body: JiraSecurityLevelsRequest,
    settings: Settings = Depends(get_settings),
) -> list[dict]:
    """List available security levels for a Jira project."""
    if not settings.jira_url or not body.project_key:
        return []

    result = _jira_client_from_body(settings, body.jira_token, body.jira_email)
    if result is None:
        return []

    effective_settings, _ = result

    from jenkins_job_insight.jira import JiraClient

    try:
        async with JiraClient(effective_settings) as client:
            return await client.list_security_levels(body.project_key)
    except Exception:
        logger.warning("Failed to list Jira security levels", exc_info=True)
        return []


class ValidateTokenRequest(BaseModel):
    """Request body for validating a tracker token."""

    token_type: Literal["github", "jira"] = Field(description="Token type")
    token: str = Field(description="Token value to validate")
    email: str = Field(default="", description="Email for Jira Cloud auth")


@app.post("/api/validate-token")
async def validate_token(
    body: ValidateTokenRequest,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Validate a GitHub or Jira token by making a lightweight API call.

    GitHub: GET /user (returns authenticated user info)
    Jira: GET /rest/api/2/myself (returns authenticated user info)
    """
    token = body.token.strip()
    if not token:
        return {"valid": False, "username": "", "message": "Token is required"}

    def _invalid(msg: str) -> dict:
        return {"valid": False, "username": "", "message": msg}

    def _status_message(status_code: int) -> str:
        if status_code in (401, 403):
            return f"Invalid token (HTTP {status_code})"
        return f"Tracker API returned HTTP {status_code}"

    if body.token_type == "github":
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Accept": "application/vnd.github.v3+json",
                        "Authorization": f"Bearer {token}",
                    },
                )
                resp.raise_for_status()
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError):
                    return _invalid("Tracker API returned an unexpected response")
                return {
                    "valid": True,
                    "username": data.get("login", ""),
                    "message": f"Authenticated as {data.get('login', 'unknown')}",
                }
        except httpx.HTTPStatusError as exc:
            return _invalid(_status_message(exc.response.status_code))
        except httpx.RequestError:
            return _invalid("Could not reach GitHub API")

    elif body.token_type == "jira":
        jira_url = (settings.jira_url or "").rstrip("/")
        if not jira_url:
            return _invalid("Jira URL not configured on server")
        # Build auth based on whether email is provided (Cloud vs DC)
        email = body.email.strip()
        auth: tuple[str, str] | None = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if email:
            auth = (email, token)
        else:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(
                verify=settings.jira_ssl_verify, timeout=10, auth=auth
            ) as client:
                resp = await client.get(
                    f"{jira_url}/rest/api/2/myself",
                    headers=headers,
                )
                resp.raise_for_status()
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError):
                    return _invalid("Tracker API returned an unexpected response")
                display = data.get("displayName", data.get("name", ""))
                return {
                    "valid": True,
                    "username": display,
                    "message": f"Authenticated as {display}",
                }
        except httpx.HTTPStatusError as exc:
            return _invalid(_status_message(exc.response.status_code))
        except httpx.RequestError:
            return _invalid("Could not reach Jira API")


@app.get("/history/failures")
async def get_all_failures_endpoint(
    search: str = Query(default=""),
    job_name: str = Query(default=""),
    classification: str = Query(default=""),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Get paginated failure history."""
    logger.debug(
        f"GET /history/failures: search={search!r}, job_name={job_name!r}, classification={classification!r}, limit={limit}, offset={offset}"
    )
    return await storage.get_all_failures(
        search=search,
        job_name=job_name,
        classification=classification,
        limit=limit,
        offset=offset,
    )


@app.get("/history/test/{test_name:path}")
async def get_test_history_endpoint(
    test_name: str,
    limit: int = Query(default=20, le=100),
    job_name: str = Query(default=""),
    exclude_job_id: str = Query(
        default="", description="Exclude results from this job ID"
    ),
) -> dict:
    """Get pass/fail history for a specific test."""
    logger.debug(f"GET /history/test/{test_name}: limit={limit}, job_name={job_name!r}")
    return await storage.get_test_history(
        test_name, limit=limit, job_name=job_name, exclude_job_id=exclude_job_id
    )


@app.get("/history/search")
async def search_by_signature_endpoint(
    signature: str = Query(...),
    exclude_job_id: str = Query(
        default="", description="Exclude results from this job ID"
    ),
) -> dict:
    """Find all tests that failed with the same error signature."""
    logger.debug(f"GET /history/search: signature={signature}")
    return await storage.search_by_signature(signature, exclude_job_id=exclude_job_id)


@app.get("/history/stats/{job_name:path}")
async def get_job_stats_endpoint(
    job_name: str,
    exclude_job_id: str = Query(
        default="", description="Exclude results from this job ID"
    ),
) -> dict:
    """Get aggregate statistics for a specific job."""
    logger.debug(f"GET /history/stats/{job_name}")
    return await storage.get_job_stats(job_name, exclude_job_id=exclude_job_id)


@app.post("/history/classify", status_code=201)
async def classify_test(request: Request, body: ClassifyTestRequest) -> dict:
    """Classify a test as FLAKY, REGRESSION, etc. Used by AI and humans."""
    _check_allow_list(request)
    logger.debug(
        f"POST /history/classify: test_name={body.test_name!r}, classification={body.classification!r}"
    )
    test_name = body.test_name.strip()
    classification = body.classification
    reason = body.reason
    job_name = body.job_name
    references = body.references
    classify_job_id = body.job_id

    if not test_name:
        raise HTTPException(status_code=400, detail="test_name is required")

    if classification == "KNOWN_BUG" and not str(references).strip():
        raise HTTPException(
            status_code=400,
            detail="KNOWN_BUG requires non-empty references (e.g., Jira tickets or historical bug URLs).",
        )

    created_by = request.state.username or "ai"

    # Human classifications are visible immediately.
    # AI classifications become visible after analysis completes
    # and calls make_classifications_visible().
    visible = 0 if created_by == "ai" else 1

    # Look up parent job name from failure_history, scoped to this job
    parent_job_name = await storage.get_parent_job_name_for_test(
        test_name, job_id=classify_job_id
    )
    if not parent_job_name and classify_job_id:
        # Job might not be in failure_history yet (analysis in progress)
        result = await storage.get_result(classify_job_id)
        if result and result.get("result"):
            parent_job_name = result["result"].get("job_name", "")

    try:
        classification_id = await storage.set_test_classification(
            test_name=test_name,
            classification=classification,
            reason=reason,
            job_name=job_name,
            parent_job_name=parent_job_name,
            created_by=created_by,
            references=references,
            job_id=classify_job_id,
            child_build_number=body.child_build_number,
            visible=visible,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": classification_id}


@app.get("/history/classifications")
async def get_classifications(
    test_name: str = Query(default=""),
    classification: str = Query(default=""),
    job_name: str = Query(default=""),
    parent_job_name: str = Query(default=""),
    job_id: str = Query(default=""),
) -> dict:
    """Get test classifications."""
    logger.debug(
        f"GET /history/classifications: test_name={test_name!r}, classification={classification!r}, "
        f"job_name={job_name!r}, parent_job_name={parent_job_name!r}, job_id={job_id!r}"
    )
    classifications = await storage.get_test_classifications(
        test_name=test_name,
        classification=classification,
        job_name=job_name,
        parent_job_name=parent_job_name,
        job_id=job_id,
    )
    return {"classifications": classifications}


@app.get("/ai-configs")
async def get_ai_configs_endpoint() -> list[dict]:
    """Get distinct AI provider/model pairs from completed analyses."""
    logger.debug("GET /ai-configs")
    return await get_ai_configs()


@app.get("/api/ai-models")
async def list_ai_models(
    provider: str = Query(
        "", description="Filter by AI provider (e.g. cursor, claude, gemini)"
    ),
) -> dict:
    """List available AI models for one or all configured providers."""
    logger.debug("GET /api/ai-models provider=%s", provider)
    try:
        if provider:
            provider = provider.lower().strip()
            if provider not in VALID_AI_PROVIDERS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported AI provider: {provider}. "
                        f"Valid providers: {', '.join(sorted(VALID_AI_PROVIDERS))}"
                    ),
                )
            models = await model_cache.list_models(provider)
            return {"provider": provider, "models": models}

        # No provider specified — return models for all known providers
        all_models: dict[str, list[dict]] = {}
        for p in sorted(VALID_AI_PROVIDERS):
            try:
                models = await model_cache.list_models(p)
                all_models[p] = models
            except Exception:
                logger.warning(
                    "Failed to list models for provider=%s", p, exc_info=True
                )
                all_models[p] = []
        return {"providers": all_models}
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            "Failed to list AI models for provider=%s", provider, exc_info=True
        )
        if provider:
            return {"provider": provider, "models": []}
        return {"providers": {}}


@app.get("/health")
async def health_check() -> dict:
    """Basic health check endpoint (legacy, lightweight)."""
    return {"status": "healthy"}


@app.get("/api/health")
async def health_check_detailed() -> Response:
    """Detailed health endpoint with dependency checks and error rates.

    Returns:
        200 for healthy/degraded, 503 for unhealthy.
    """
    settings = get_settings()
    db_path = str(storage.DB_PATH)
    result = await build_health_response(settings, db_path)
    status_code = 503 if result["status"] == "unhealthy" else 200
    return JSONResponse(content=result, status_code=status_code)


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus metrics endpoint."""
    # Compute health_up from a lightweight health check
    settings = get_settings()
    db_path = str(storage.DB_PATH)
    try:
        health = await build_health_response(settings, db_path)
        health_up = 0 if health["status"] == "unhealthy" else 1
    except Exception:  # noqa: BLE001
        logger.debug("Failed to compute health status for metrics", exc_info=True)
        health_up = 0

    # Count active analyses
    active_analyses: int | None = None
    try:
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM results WHERE status IN ('pending', 'running', 'waiting')"
            )
            row = await cursor.fetchone()
            active_analyses = row[0] if row else 0
    except Exception:  # noqa: BLE001
        logger.debug("Failed to compute active analyses for metrics", exc_info=True)

    return Response(
        content=render_prometheus_metrics(
            health_up=health_up, active_analyses=active_analyses
        ),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Serve the application favicon as an SVG image."""
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """Serve the service worker for push notifications."""
    sw_file = _FRONTEND_DIR / "sw.js"
    if not sw_file.is_file():
        # Fallback to public/ during development
        sw_file = _FRONTEND_DIR.parent / "public" / "sw.js"
    if not sw_file.is_file():
        raise HTTPException(status_code=404, detail="Service worker not found")
    return Response(
        content=sw_file.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


def _serve_spa() -> HTMLResponse:
    """Read and serve the React SPA index.html."""
    index_file = _FRONTEND_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="Frontend not built")
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


# --- Auth endpoints ---


async def _read_json_object(request: Request) -> dict:
    """Parse request body as a JSON object. Raises HTTPException on invalid input."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return body


def _require_admin(request: Request) -> None:
    """Raise 403 if the request is not from an authenticated admin."""
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


def _check_allow_list(request: Request) -> None:
    """Raise 403 if the requesting user is not on the allow list.

    When the allow list is empty (default), all users are permitted.
    Admin users always bypass the allow list check.
    """
    settings = get_settings()
    allowed = settings.allowed_users_set
    if not allowed:
        return  # Open access — no restriction
    if request.state.is_admin:
        return  # Admins always bypass
    username = (request.state.username or "").strip().lower()
    if not username or username not in allowed:
        raise HTTPException(
            status_code=403,
            detail="User not allowed. Contact an administrator to be added to the allow list.",
        )


@app.post("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    """Authenticate admin with username + API key. Returns session cookie."""
    body = await _read_json_object(request)

    username = str(body.get("username", ""))
    api_key = str(body.get("api_key", ""))

    if not username or not api_key:
        raise HTTPException(status_code=400, detail="Username and api_key are required")

    settings = get_settings()
    is_admin = False
    authenticated = False

    # Check admin_key — username must be "admin"
    if (
        username == "admin"
        and settings.admin_key
        and hmac.compare_digest(api_key, settings.admin_key)
    ):
        is_admin = True
        authenticated = True
    else:
        # Check user API key
        user = await storage.get_user_by_key(api_key)
        if user and user["username"] == username:
            authenticated = True
            if user.get("role") == "admin":
                is_admin = True

    if not authenticated:
        logger.info(f"[AUDIT] Failed login attempt for username '{username}'")
        raise HTTPException(status_code=401, detail="Invalid username or API key")

    session_token = await storage.create_session(username, is_admin=is_admin)
    response = JSONResponse(
        content={
            "username": username,
            "role": "admin" if is_admin else "user",
            "is_admin": is_admin,
        }
    )
    response.set_cookie(
        "jji_session",
        session_token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=storage.SESSION_TTL_SECONDS,
    )
    # Also set jji_username cookie for compatibility
    response.set_cookie(
        "jji_username",
        username,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=365 * 24 * 60 * 60,
    )
    logger.info(f"[AUDIT] Login success: user='{username}' is_admin={is_admin}")
    return response


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Clear admin session."""
    session_token = request.cookies.get("jji_session")
    if session_token:
        await storage.delete_session(session_token)
    settings = get_settings()
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(
        "jji_session",
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
    )
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    """Return current user info."""
    return JSONResponse(
        content={
            "username": request.state.username,
            "role": request.state.role,
            "is_admin": request.state.is_admin,
        }
    )


# --- User token endpoints ---


@app.get("/api/user/tokens")
async def get_user_tokens_endpoint(request: Request) -> JSONResponse:
    """Get the current user's saved tokens."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    # Verify user exists in DB (prevents reading tokens for unregistered usernames)
    user = await storage.get_user_by_username(username)
    if not user:
        return JSONResponse(
            content={"github_token": "", "jira_email": "", "jira_token": ""},
            headers={"Cache-Control": "no-store"},
        )
    tokens = await storage.get_user_tokens(username)
    return JSONResponse(
        content=tokens,
        headers={"Cache-Control": "no-store"},
    )


@app.put("/api/user/tokens")
async def save_user_tokens_endpoint(request: Request) -> JSONResponse:
    """Save tokens for the current user. Tokens are encrypted at rest.

    Only fields present in the JSON body are updated. Omitted fields are left unchanged.
    Pass empty string to clear a field.
    """
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    # Verify user exists in DB
    user = await storage.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Register first.")
    body = await _read_json_object(request)

    gh = str(body.get("github_token", "")).strip()
    je = str(body.get("jira_email", "")).strip()
    jt = str(body.get("jira_token", "")).strip()

    # If all empty, skip save — don't overwrite existing tokens
    if not gh and not je and not jt:
        return JSONResponse(content={"ok": True})

    # Merge with existing: only overwrite fields that have new values
    existing = await storage.get_user_tokens(username)
    kwargs: dict[str, str | None] = {
        "github_token": gh if gh else existing.get("github_token", ""),
        "jira_email": je if je else existing.get("jira_email", ""),
        "jira_token": jt if jt else existing.get("jira_token", ""),
    }

    await storage.save_user_tokens(username, **kwargs)
    logger.debug(f"Saved tokens for user '{username}'")
    return JSONResponse(content={"ok": True})


# --- Admin endpoints ---


@app.get("/api/admin/token-usage")
async def get_token_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
    call_type: str | None = None,
    group_by: str | None = None,
) -> dict:
    """Get aggregated token usage with optional filters and grouping. Admin only."""
    _require_admin(request)
    if group_by and group_by not in _VALID_GROUP_BY:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid group_by value. Valid: {', '.join(sorted(_VALID_GROUP_BY))}",
        )
    return await storage.get_token_usage_summary(
        start_date=start_date,
        end_date=end_date,
        ai_provider=ai_provider,
        ai_model=ai_model,
        call_type=call_type,
        group_by=group_by,
    )


@app.get("/api/admin/token-usage/summary")
async def get_token_usage_dashboard(request: Request) -> dict:
    """Get high-level token usage summary for dashboard. Admin only."""
    _require_admin(request)
    return await storage.get_token_usage_dashboard_summary()


@app.get("/api/admin/token-usage/{job_id}")
async def get_token_usage_for_job(request: Request, job_id: str) -> dict:
    """Get token usage breakdown for a specific job. Admin only."""
    _require_admin(request)
    records = await storage.get_token_usage_for_job(job_id)
    if not records:
        raise HTTPException(
            status_code=404, detail="No token usage records found for this job"
        )
    return {"job_id": job_id, "records": records}


@app.post("/api/admin/users")
async def create_admin_user_endpoint(request: Request) -> JSONResponse:
    """Create a new admin user. Returns the generated API key."""
    _require_admin(request)
    body = await _read_json_object(request)

    username = body.get("username", "")
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    try:
        username, raw_key = await storage.create_admin_user(username)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' created admin user '{username}'"
    )
    return JSONResponse(
        content={"username": username, "api_key": raw_key, "role": "admin"},
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/admin/users/{username}")
async def delete_admin_user_endpoint(request: Request, username: str) -> dict:
    """Delete an admin user."""
    _require_admin(request)
    if username == request.state.username:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    try:
        deleted = await storage.delete_admin_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Admin user '{username}' not found"
        )

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' deleted admin user '{username}'"
    )
    return {"deleted": username}


@app.put("/api/admin/users/{username}/role")
async def change_user_role_endpoint(request: Request, username: str) -> JSONResponse:
    """Change a user's role (promote to admin or demote to user).

    When promoting to admin, an API key is generated and returned.
    When demoting to user, the API key is removed and sessions invalidated.
    """
    _require_admin(request)
    if username == request.state.username:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    body = await _read_json_object(request)

    new_role = body.get("role", "")
    if not new_role:
        raise HTTPException(status_code=400, detail="Role is required")

    try:
        username, raw_key = await storage.change_user_role(username, new_role)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail) from exc

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' changed role of '{username}' to '{new_role}'"
    )

    content: dict = {"username": username, "role": new_role}
    if raw_key:
        content["api_key"] = raw_key
    return JSONResponse(
        content=content,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/admin/users")
async def list_users_endpoint(request: Request) -> dict:
    """List all users (admin and regular)."""
    _require_admin(request)
    users = await storage.list_users()
    return {"users": users}


@app.post("/api/admin/users/{username}/rotate-key")
async def rotate_key_endpoint(request: Request, username: str) -> JSONResponse:
    """Rotate an admin user's API key."""
    _require_admin(request)
    try:
        body_bytes = await request.body()
        if body_bytes and body_bytes.strip():
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400, detail="JSON body must be an object"
                )
            custom_key = body.get("new_key")
        else:
            custom_key = None
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid JSON body: {exc}"
        ) from exc

    try:
        new_key = await storage.rotate_admin_key(username, custom_key=custom_key)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail) from exc

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' rotated key for '{username}'"
    )
    return JSONResponse(
        content={"username": username, "new_api_key": new_key},
        headers={"Cache-Control": "no-store"},
    )


# -- Job Metadata Endpoints ---------------------------------------------------


async def _metadata_filters(
    team: Annotated[str, Query()] = "",
    tier: Annotated[str, Query()] = "",
    version: Annotated[str, Query()] = "",
    label: Annotated[list[str] | None, Query()] = None,
) -> dict:
    """Shared dependency for metadata filter query parameters."""
    return {"team": team, "tier": tier, "version": version, "label": label or []}


def _unpack_metadata_filters(
    filters: dict, endpoint: str
) -> tuple[str, str, str, list[str]]:
    """Unpack metadata filter dict and log at DEBUG level."""
    team, tier, version, label = (
        filters["team"],
        filters["tier"],
        filters["version"],
        filters["label"],
    )
    logger.debug(
        "%s: team=%r, tier=%r, version=%r, label=%r",
        endpoint,
        team,
        tier,
        version,
        label,
    )
    return team, tier, version, label


@app.get("/api/jobs/metadata")
async def list_jobs_metadata(
    filters: Annotated[dict, Depends(_metadata_filters)],
) -> list[dict]:
    """List all job metadata, optionally filtered by team, tier, version, or labels."""
    team, tier, version, label = _unpack_metadata_filters(
        filters, "GET /api/jobs/metadata"
    )
    return await storage.list_jobs_with_metadata(
        team=team, tier=tier, version=version, labels=label or None
    )


@app.get("/api/jobs/{job_name:path}/metadata")
async def get_job_metadata_endpoint(job_name: str) -> dict:
    """Get metadata for a specific job."""
    logger.debug(f"GET /api/jobs/{job_name}/metadata")
    result = await storage.get_job_metadata(job_name)
    if not result:
        raise HTTPException(status_code=404, detail=f"No metadata for job '{job_name}'")
    return result


@app.put("/api/jobs/{job_name:path}/metadata")
async def set_job_metadata_endpoint(
    request: Request,
    job_name: str,
    body: JobMetadataInput,
) -> dict:
    """Set or update metadata for a job."""
    _require_admin(request)
    logger.debug(f"PUT /api/jobs/{job_name}/metadata")
    current = await storage.get_job_metadata(job_name) or {}
    return await storage.set_job_metadata(
        job_name,
        team=body.team if "team" in body.model_fields_set else current.get("team"),
        tier=body.tier if "tier" in body.model_fields_set else current.get("tier"),
        version=body.version
        if "version" in body.model_fields_set
        else current.get("version"),
        labels=body.labels
        if "labels" in body.model_fields_set
        else current.get("labels", []),
    )


@app.delete("/api/jobs/{job_name:path}/metadata")
async def delete_job_metadata_endpoint(request: Request, job_name: str) -> dict:
    """Delete metadata for a job."""
    _require_admin(request)
    logger.debug(f"DELETE /api/jobs/{job_name}/metadata")
    deleted = await storage.delete_job_metadata(job_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No metadata for job '{job_name}'")
    return {"status": "deleted", "job_name": job_name}


@app.put("/api/jobs/metadata/bulk")
async def bulk_set_job_metadata(
    request: Request,
    body: BulkJobMetadataRequest,
) -> dict:
    """Bulk import job metadata.

    Unlike PUT /api/jobs/{job_name}/metadata which preserves omitted fields,
    bulk import performs a full replace — omitted optional fields are set to
    their defaults (None/empty list).
    """
    _require_admin(request)
    logger.debug(f"PUT /api/jobs/metadata/bulk: {len(body.items)} items")
    try:
        items = [item.model_dump() for item in body.items]
        return await storage.bulk_set_metadata(items)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/jobs/metadata/rules")
async def list_metadata_rules() -> dict:
    """List configured metadata rules for auto-assignment."""
    logger.debug("GET /api/jobs/metadata/rules")
    settings = get_settings()
    rules = settings.metadata_rules
    return {
        "rules_file": (
            Path(settings.metadata_rules_file).name
            if settings.metadata_rules_file
            else None
        ),
        "rules": rules,
    }


@app.post("/api/jobs/metadata/rules/preview")
async def preview_metadata_rules(body: dict) -> dict:
    """Preview what metadata would be assigned to a job name by rules.

    Request body: {"job_name": "..."}
    """
    logger.debug("POST /api/jobs/metadata/rules/preview")
    job_name = body.get("job_name", "")
    if not isinstance(job_name, str) or not job_name.strip():
        raise HTTPException(status_code=422, detail="job_name is required")
    job_name = job_name.strip()

    from jenkins_job_insight.metadata_rules import match_job_metadata

    settings = get_settings()
    rules = settings.metadata_rules
    matched = match_job_metadata(job_name, rules)
    return {
        "job_name": job_name,
        "matched": matched is not None,
        "metadata": matched,
    }


@app.get("/api/dashboard/filtered")
async def api_dashboard_filtered(
    filters: Annotated[dict, Depends(_metadata_filters)],
) -> list[dict]:
    """Return dashboard job list filtered by metadata.

    Joins dashboard results with job_metadata. When no filters are
    provided, returns all jobs (same as /api/dashboard but with
    metadata attached).
    """
    team, tier, version, label = _unpack_metadata_filters(
        filters, "GET /api/dashboard/filtered"
    )
    jobs = await list_results_for_dashboard()

    # If no filters, attach metadata and return all
    has_filters = bool(team or tier or version or label)

    # Build a lookup of job metadata
    all_metadata = await storage.list_jobs_with_metadata(
        team=team if has_filters else "",
        tier=tier if has_filters else "",
        version=version if has_filters else "",
        labels=label if has_filters and label else None,
    )
    metadata_by_name = {m["job_name"]: m for m in all_metadata}

    if has_filters:
        # Only include jobs whose job_name matches filtered metadata
        filtered_names = set(metadata_by_name.keys())
        jobs = [j for j in jobs if j.get("job_name", "") in filtered_names]

    # Attach metadata to each job
    for job in jobs:
        jn = job.get("job_name", "")
        if jn in metadata_by_name:
            job["metadata"] = metadata_by_name[jn]
        else:
            job["metadata"] = None

    return jobs


# --- Notification endpoints ---


@app.get("/api/notifications/vapid-public-key")
async def get_vapid_public_key():
    """Return the VAPID public key for frontend push subscription."""
    settings = get_settings()
    if not settings.web_push_enabled:
        raise HTTPException(
            status_code=404, detail="Web Push notifications not configured"
        )
    vapid_cfg = get_vapid_config()
    if not vapid_cfg or "public_key" not in vapid_cfg:
        raise HTTPException(status_code=503, detail="VAPID keys unavailable")
    return {"vapid_public_key": vapid_cfg["public_key"]}


@app.post("/api/notifications/subscribe")
async def subscribe_notifications(body: PushSubscriptionRequest, request: Request):
    """Register a push subscription for the current user."""
    settings = get_settings()
    if not settings.web_push_enabled:
        raise HTTPException(
            status_code=404, detail="Web Push notifications not configured"
        )
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    await storage.save_push_subscription(
        username=username,
        endpoint=body.endpoint,
        p256dh_key=body.p256dh_key,
        auth_key=body.auth_key,
    )
    return {"status": "subscribed"}


@app.post("/api/notifications/unsubscribe")
async def unsubscribe_notifications(body: UnsubscribeRequest, request: Request):
    """Remove a push subscription."""
    settings = get_settings()
    if not settings.web_push_enabled:
        raise HTTPException(
            status_code=404, detail="Web Push notifications not configured"
        )
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    deleted = await storage.delete_push_subscription(body.endpoint, username)
    if not deleted:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"status": "unsubscribed"}


@app.get("/api/users/mentions")
async def get_user_mentions(request: Request):
    """Get comments that mention the current user."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
        limit = min(200, max(1, int(request.query_params.get("limit", "50"))))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="offset and limit must be integers"
        ) from exc
    unread_only = request.query_params.get("unread_only", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    result = await storage.get_mentions_for_user(
        username=username, offset=offset, limit=limit, unread_only=unread_only
    )
    return {
        "mentions": result["mentions"],
        "total": result["total"],
        "unread_count": result["unread_count"],
    }


@app.post("/api/users/mentions/read-all")
async def mark_all_mentions_read_endpoint(request: Request):
    """Mark ALL mentions as read for the current user."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    count = await storage.mark_all_mentions_read(username)
    return {"marked_read": count}


@app.post("/api/users/mentions/read")
async def mark_mentions_as_read(request: Request):
    """Mark specific mentions as read."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    body = await _read_json_object(request)
    comment_ids = body.get("comment_ids", [])
    if (
        not isinstance(comment_ids, list)
        or not comment_ids
        or not all(
            isinstance(cid, int) and not isinstance(cid, bool) for cid in comment_ids
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="comment_ids must be a non-empty list of integers",
        )
    await storage.mark_mentions_read(username, comment_ids)
    return {"ok": True}


@app.get("/api/users/mentions/unread-count")
async def get_unread_mentions_count(request: Request):
    """Get count of unread mentions for navbar badge."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    count = await storage.get_unread_mention_count(username)
    return {"count": count}


@app.get("/api/users/mentionable")
async def get_mentionable_users(request: Request):
    """Return list of usernames that can be mentioned in comments."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    users = await storage.list_users()
    return {"usernames": [u["username"] for u in users]}


@app.post("/api/analyze-comment-intent", response_model=AnalyzeCommentResponse)
async def analyze_comment_intent(
    request: Request, body: AnalyzeCommentRequest
) -> AnalyzeCommentResponse:
    """Analyze a comment to determine if it implies a failure has been reviewed/resolved."""
    _check_allow_list(request)

    ai_provider = body.ai_provider or AI_PROVIDER
    ai_model = body.ai_model or AI_MODEL
    if (not ai_provider or not ai_model) and body.job_id:
        stored = await storage.get_result(body.job_id)
        if stored and stored.get("result"):
            params = stored["result"].get("request_params", {})
            if not ai_provider:
                ai_provider = params.get("ai_provider", "")
            if not ai_model:
                ai_model = params.get("ai_model", "")
    ai_provider, ai_model = _resolve_ai_config_values(ai_provider, ai_model)

    from ai_cli_runner import call_ai_cli

    from jenkins_job_insight.analyzer import PROVIDER_CLI_FLAGS

    prompt = """You are analyzing a comment left on a test failure report.
Does this comment imply the failure has been reviewed or resolved?

Examples that SUGGEST reviewed/resolved:
- Bug filed with a link (e.g., "Filed JIRA-123 for this")
- Root cause identified (e.g., "This is caused by the config change in PR #456")
- Known issue noted (e.g., "Known flaky test, tracked in BUG-789")
- Fix merged (e.g., "Fixed in commit abc123")

Examples that DO NOT suggest reviewed/resolved:
- Asking for more info (e.g., "Can someone check the logs?")
- Sharing logs for context (e.g., "Here's the full stack trace: ...")
- Linking docs (e.g., "See the troubleshooting guide: ...")
- General discussion (e.g., "This started happening after the last deploy")

Comment:
"""
    prompt += body.comment
    prompt += """

Respond with ONLY a JSON object:
{"suggests_reviewed": true/false, "reason": "brief explanation"}"""

    result = await call_ai_cli(
        prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=2,
        cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
        output_format="json",
    )

    from jenkins_job_insight.token_tracking import record_ai_usage

    await record_ai_usage(
        job_id="comment-intent",
        result=result,
        call_type="comment_intent",
        prompt_chars=len(prompt),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )

    if not result.success:
        logger.debug("AI CLI call failed for comment intent analysis: %s", result.text)
        return AnalyzeCommentResponse(suggests_reviewed=False)

    try:
        parsed = json.loads(result.text)
        return AnalyzeCommentResponse(
            suggests_reviewed=bool(parsed.get("suggests_reviewed", False)),
            reason=str(parsed.get("reason", "")),
        )
    except (json.JSONDecodeError, AttributeError):
        logger.debug("Failed to parse AI response for comment intent: %s", result.text)
        return AnalyzeCommentResponse(suggests_reviewed=False)


@app.post(
    "/api/feedback/preview",
    status_code=200,
    response_model=FeedbackPreviewResponse,
)
async def preview_feedback(request: Request, body: FeedbackRequest):
    """Preview user feedback as a formatted GitHub issue.

    Accepts bug reports or feature requests, uses AI to format them
    into well-structured GitHub issues, scrubs sensitive data from
    attached logs, and returns the preview without creating the issue.
    """
    _check_allow_list(request)
    settings = get_settings()
    if not settings.feedback_enabled:
        raise HTTPException(
            status_code=503, detail="Feedback submission is disabled on this server"
        )
    try:
        ai_provider, ai_model = _resolve_ai_config_values(None, None)
    except HTTPException as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "AI provider not configured on this server. "
                "Configure AI_PROVIDER and AI_MODEL environment variables "
                "to enable AI-powered feedback."
            ),
        ) from exc
    try:
        return await generate_feedback_preview(
            body, settings, ai_provider=ai_provider, ai_model=ai_model
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal feedback preview
        logger.exception("Failed to generate feedback preview")
        raise HTTPException(
            status_code=500,
            detail="Failed to generate feedback preview",
        ) from exc


@app.post("/api/feedback/create", status_code=201, response_model=FeedbackResponse)
async def create_feedback(request: Request, body: FeedbackCreateRequest):
    """Create a GitHub issue from a previewed feedback.

    Takes a title, body, and labels (typically from the preview endpoint)
    and creates the GitHub issue.
    """
    _check_allow_list(request)
    settings = get_settings()
    if not settings.feedback_enabled:
        raise HTTPException(
            status_code=503, detail="Feedback submission is disabled on this server"
        )
    try:
        return await create_feedback_from_preview(
            title=body.title,
            body=body.body,
            labels=body.labels,
            settings=settings,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise HTTPException(
                status_code=502,
                detail="GitHub token is invalid or expired",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API error: {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API unreachable: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — non-fatal feedback submission
        logger.exception("Failed to create feedback issue")
        raise HTTPException(
            status_code=500,
            detail="Failed to create feedback issue",
        ) from exc


# SPA catch-all routes — must be AFTER all API routes
@app.get("/register", include_in_schema=False)
async def serve_spa_known_routes() -> HTMLResponse:
    """Serve the React SPA for known frontend routes."""
    return _serve_spa()


@app.get("/{path:path}", include_in_schema=False)
async def serve_frontend_catchall(request: Request, path: str) -> HTMLResponse:
    """Catch-all: serve the React SPA for any unmatched route."""
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    accept = request.headers.get("accept", "")
    if "text/html" not in accept or "application/json" in accept:
        raise HTTPException(status_code=404, detail="Not found")
    return _serve_spa()


def run() -> None:
    """Entry point for the CLI."""
    import uvicorn

    reload = os.getenv("DEBUG", "").lower() == "true"
    uvicorn.run(
        "jenkins_job_insight.main:app", host="0.0.0.0", port=APP_PORT, reload=reload
    )
