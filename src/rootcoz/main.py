import asyncio
import copy
import hmac
import json
import logging
import math
import os
import sqlite3
import time as _time
import urllib.parse
import uuid
from collections import defaultdict
from collections.abc import Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import aiosqlite
import httpx
import uvicorn
from pi_sidecar_client import (
    call_ai_once,
    check_sidecar_available,
    list_models,
    run_parallel_with_limit,
)
from rootcoz.ai_client import VALID_AI_PROVIDERS, _setup_usage_recorder
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ValidationError
from simple_logger.logger import get_logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from rootcoz import storage
from rootcoz.bug_creation import (
    create_github_issue,
    create_jira_bug,
    generate_github_issue_content,
    generate_jira_bug_content,
    parse_github_repo_url,
    search_github_duplicates,
    search_jira_duplicates,
)
from rootcoz.comment_enrichment import (
    detect_github_issues,
    detect_github_prs,
    detect_jira_keys,
    detect_mentions,
    fetch_github_issue_status,
    fetch_github_pr_status,
    fetch_jira_ticket_status,
)
from rootcoz.config import (
    Settings,
    get_settings,
    parse_peer_configs,
    parse_repo_ref,
)
from rootcoz.encryption import (
    SENSITIVE_KEYS,
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
)
from rootcoz.engine.core import (
    JOB_INSIGHT_ISSUE_PROMPT_FILENAME,
    analyze_failure_group,
    clone_additional_repos,
    extract_json_dict,
    get_failure_signature,
    resolve_additional_repos,
    safe_update_progress,
    set_progress_callback,
)
from rootcoz.error_messages import make_user_friendly_error
from rootcoz.feedback import (
    create_feedback_from_preview,
    generate_feedback_preview,
)
from rootcoz.github_issues import enrich_with_tests_repo_matches
from rootcoz.jira import JiraClient, filter_matches_with_ai, enrich_with_jira_matches
from rootcoz.logging_context import JobIdFilter, get_log_file, job_id_var
from rootcoz.metadata_rules import match_job_metadata
from rootcoz.models import (
    AddCommentRequest,
    AdditionalRepo,
    ChatMessageRequest,
    AnalyzeCommentRequest,
    AnalyzeCommentResponse,
    AnalyzeRequest,
    BaseAnalysisRequest,
    BulkDeleteRequest,
    BulkJobMetadataRequest,
    ChildJobAnalysis,
    ClassifyTestRequest,
    CreateIssueRequest,
    FailedTest,
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
    ReAnalyzeFailureRequest,
    ReportPortalPushResult,
    SetReviewedRequest,
    UnifiedAnalyzeRequest,
    UnsubscribeRequest,
)
from rootcoz.monitoring import (
    build_health_response,
    dispatch_alert,
    error_tracker,
    render_prometheus_metrics,
    validate_startup_config,
)
from rootcoz.notifications import send_mention_notifications
from rootcoz.reportportal import AmbiguousLaunchError, ReportPortalClient
from rootcoz.repository import (
    RepositoryManager,
    derive_test_repo_name,
    redact_url,
)
from rootcoz.request_resolution import resolve_tests_repo_token
from rootcoz.sources import CISource, FileSource, RawSource
from rootcoz.sources.jenkins_source import analyze_job, wait_for_jenkins_completion
from rootcoz.storage import (
    DB_PATH,
    get_effective_classification,
    get_history_classification,
    get_result,
    init_db,
    list_results,
    list_distinct_job_names,
    list_results_for_dashboard,
    patch_result_json,
    populate_failure_history,
    save_result,
    update_status,
)
from rootcoz.token_tracking import build_token_usage_summary
from rootcoz.utils import (
    is_sensitive_key,
    mask_sensitive_fields,
)
from rootcoz.vapid import get_vapid_config
from rootcoz.xml_enrichment import (
    build_enriched_xml,
)

# Module-level Depends singletons (B008: avoid function calls in defaults)
_SETTINGS_DEP = Depends(get_settings)

# --- Server Settings metadata ---

# Fields that contain sensitive data (passwords, tokens, keys)
_SENSITIVE_SETTINGS: frozenset[str] = frozenset(
    {
        "jenkins_password",
        "jenkins_user",
        "jira_api_token",
        "jira_pat",
        "jira_email",
        "github_token",
        "tests_repo_token",
        "reportportal_api_token",
        "admin_key",
        "vapid_private_key",
    }
)

# Fields that require server restart to take effect
_RESTART_REQUIRED_SETTINGS: frozenset[str] = frozenset(
    {
        "secure_cookies",
        "trust_proxy_headers",
        "metadata_rules_file",
        "vapid_public_key",
        "vapid_private_key",
        "vapid_claim_email",
    }
)

# Category grouping for UI display
_SETTINGS_CATEGORIES: dict[str, list[str]] = {
    "Jenkins": [
        "jenkins_url",
        "jenkins_user",
        "jenkins_password",
        "jenkins_ssl_verify",
        "jenkins_timeout",
        "jenkins_artifacts_max_size_mb",
        "get_job_artifacts",
    ],
    "AI": [
        "ai_call_timeout",
        "max_concurrent_ai_calls",
        "peer_ai_configs",
        "peer_analysis_max_rounds",
        "force_analysis",
    ],
    "Jira": [
        "jira_url",
        "jira_email",
        "jira_api_token",
        "jira_pat",
        "jira_project_key",
        "jira_ssl_verify",
        "jira_max_results",
        "enable_jira",
        "enable_jira_issues",
    ],
    "GitHub": [
        "github_token",
        "tests_repo_url",
        "tests_repo_token",
        "enable_github_issues",
    ],
    "Report Portal": [
        "reportportal_url",
        "reportportal_api_token",
        "reportportal_project",
        "reportportal_verify_ssl",
        "enable_reportportal",
    ],
    "Auth & Security": [
        "admin_key",
        "secure_cookies",
        "trust_proxy_headers",
        "require_approval",
        "admin_wait_approve_msg",
        "allowed_users",
    ],
    "Server": [
        "public_base_url",
        "additional_repos",
        "wait_for_completion",
        "poll_interval_minutes",
        "max_wait_minutes",
        "metadata_rules_file",
    ],
    "Web Push": [
        "vapid_public_key",
        "vapid_private_key",
        "vapid_claim_email",
    ],
}

# Reverse lookup: field -> category
_FIELD_TO_CATEGORY: dict[str, str] = {}
for _cat, _fields in _SETTINGS_CATEGORIES.items():
    for _f in _fields:
        _FIELD_TO_CATEGORY[_f] = _cat


def _get_settings_metadata() -> list[dict]:
    """Build metadata for all Settings fields with current values and sources."""
    settings = get_settings()

    result = []
    for field_name, field_info in Settings.model_fields.items():
        # Get the current value
        value = getattr(settings, field_name)

        # Handle SecretStr
        if isinstance(value, SecretStr):
            value = value.get_secret_value() if value else ""

        # Convert to string for display
        if value is None:
            display_value = ""
        elif isinstance(value, bool):
            display_value = str(value).lower()
        else:
            display_value = str(value)

        # Determine the field type
        annotation = field_info.annotation
        field_type = "string"
        if annotation is bool or (
            hasattr(annotation, "__args__")
            and bool in getattr(annotation, "__args__", ())
        ):
            field_type = "boolean"
        elif annotation is int or (
            hasattr(annotation, "__args__")
            and int in getattr(annotation, "__args__", ())
        ):
            field_type = "integer"

        # Get description
        description = ""
        if field_info.description:
            description = field_info.description

        # Get default
        default = field_info.default
        if default is None:
            default_str = ""
        elif isinstance(default, bool):
            default_str = str(default).lower()
        else:
            default_str = str(default) if default != "" else ""

        is_sensitive = field_name in _SENSITIVE_SETTINGS

        result.append(
            {
                "key": field_name,
                "env_var": field_name.upper(),
                "value": display_value,
                "default": default_str,
                "description": description,
                "type": field_type,
                "category": _FIELD_TO_CATEGORY.get(field_name, "Other"),
                "sensitive": is_sensitive,
                "restart_required": field_name in _RESTART_REQUIRED_SETTINGS,
                "source": "default",  # Will be overridden by caller
            }
        )

    return result


# Inline favicon


# --- SSE broadcast for navbar badges ---
_active_count_listeners: set[asyncio.Event] = set()
_mention_listeners: dict[str, set[asyncio.Event]] = {}


def notify_active_count_changed() -> None:
    """Signal all SSE listeners that the active analysis count has changed."""
    for event in _active_count_listeners:
        event.set()


def notify_mentions_changed(username: str) -> None:
    """Signal SSE listeners for a specific user that their mention count changed."""
    listeners = _mention_listeners.get(username)
    if listeners:
        for event in listeners:
            event.set()


# Dashboard job list change notifications
_dashboard_listeners: set[asyncio.Event] = set()

# Per-job status change notifications
_job_status_listeners: dict[str, set[asyncio.Event]] = {}

# Per-job comment change notifications
_comment_listeners: dict[str, set[asyncio.Event]] = {}

# Token usage change notifications
_token_usage_listeners: set[asyncio.Event] = set()

# Per-job chat change notifications
_chat_listeners: dict[str, set[asyncio.Event]] = {}


def notify_dashboard_changed() -> None:
    """Signal all dashboard SSE listeners that the job list changed."""
    for event in _dashboard_listeners:
        event.set()


def notify_job_status_changed(job_id: str) -> None:
    """Signal SSE listeners for a specific job that its status changed."""
    listeners = _job_status_listeners.get(job_id)
    if listeners:
        for event in listeners:
            event.set()


# Register progress callback so engine/core.py can trigger SSE
# notifications without importing from main.py.
set_progress_callback(notify_job_status_changed)


def notify_comments_changed(job_id: str) -> None:
    """Signal SSE listeners for a specific job that comments changed."""
    listeners = _comment_listeners.get(job_id)
    if listeners:
        for event in listeners:
            event.set()


def notify_token_usage_changed() -> None:
    """Signal all token usage SSE listeners."""
    for event in _token_usage_listeners:
        event.set()


def notify_chat_changed(job_id: str, username: str = "") -> None:
    """Signal SSE listeners for a specific job+user that chat messages changed."""
    key = f"{job_id}:{username}" if username else job_id
    for event in _chat_listeners.get(key, set()).copy():
        event.set()


def _make_sse_stream(
    request: Request,
    listeners: set[asyncio.Event],
    event_name: str,
    per_key_listeners: dict[str, set[asyncio.Event]] | None = None,
    listener_key: str = "",
) -> StreamingResponse:
    """Create a generic SSE stream that sends a named event when signaled.

    Args:
        request: The incoming HTTP request (for disconnect detection).
        listeners: Global listener set (used when per_key_listeners is None).
        event_name: SSE event name to send (e.g. 'dashboard-changed').
        per_key_listeners: Optional per-key listener dict (e.g. per job_id).
        listener_key: Key into per_key_listeners (e.g. the job_id).
    """

    async def event_generator():
        my_event = asyncio.Event()
        wait_task: asyncio.Task | None = None

        # Register
        if per_key_listeners is not None:
            per_key_listeners.setdefault(listener_key, set()).add(my_event)
        else:
            listeners.add(my_event)

        try:
            while True:
                my_event.clear()
                try:
                    wait_task = asyncio.create_task(my_event.wait())
                    done, pending = await asyncio.wait(
                        [wait_task],
                        timeout=30,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    wait_task = None
                except asyncio.CancelledError:
                    break

                if not done:
                    yield ": keepalive\n\n"
                    continue

                if await request.is_disconnected():
                    break

                if my_event.is_set():
                    yield f"event: {event_name}\ndata: refresh\n\n"
        finally:
            if wait_task is not None:
                wait_task.cancel()
            if per_key_listeners is not None:
                bucket = per_key_listeners.get(listener_key)
                if bucket is not None:
                    bucket.discard(my_event)
                    if not bucket:
                        per_key_listeners.pop(listener_key, None)
            else:
                listeners.discard(my_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# Semaphore to limit concurrent track_user tasks
_track_user_semaphore = asyncio.Semaphore(10)


async def _safe_track_user(username: str) -> None:
    """Track user activity with bounded concurrency, swallowing any errors."""
    try:
        async with _track_user_semaphore:
            await storage.track_user(username)
    except Exception:
        logger.debug("Failed to track user activity for %s", username, exc_info=True)


FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    b'<text y="0.9em" font-size="90">\xf0\x9f\x94\x8d</text></svg>'
)

_LOG_FILE = get_log_file()

logger = get_logger(
    name=__name__,
    level=os.environ.get("LOG_LEVEL", "INFO"),
    **({"filename": _LOG_FILE} if _LOG_FILE else {}),
)

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
    except Exception:  # best-effort token tracking must never fail the job
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
    child_job_name: str = "",
    child_build_number: int = 0,
    matched_child: dict | None = None,
) -> tuple[str, str]:
    """Build report URL and Jenkins URL for bug preview endpoints.

    When ``include_links`` is *True* the returned URLs are fully-qualified
    hyperlinks.  Otherwise plain-text identifiers are returned so that
    previews remain useful without clickable links.

    When ``child_job_name`` is provided, the Jenkins URL is scoped to the
    specific child job instead of the parent pipeline.  This prevents
    sibling child job context from leaking into issue previews.

    Args:
        include_links: Whether to produce full hyperlinks.
        base_url: The external base URL of the service.
        job_id: The stored job identifier.
        result_data: Raw result dict from storage (contains jenkins_url, job_name, etc.).
        child_job_name: Optional child job name to scope the Jenkins URL.
        child_build_number: Optional child build number (0 = match by name only).
        matched_child: Pre-resolved child dict to avoid a redundant lookup.

    Returns:
        A ``(report_url, jenkins_url)`` tuple.
    """
    jenkins_url = result_data.get("jenkins_url", "")

    # Scope to child job when specified
    child: dict | None = matched_child
    if child_job_name and child is None:
        child = _find_child_job_in_result(
            result_data, child_job_name, child_build_number
        )
    if child_job_name and child:
        jenkins_url = child.get("jenkins_url") or ""
    elif child_job_name:
        logger.debug(
            "Child job '%s' not found in result; falling back to parent URL",
            child_job_name,
        )

    if include_links and base_url:
        report_url = f"{base_url}/results/{job_id}"
    else:
        report_url = f"/results/{job_id}"

    # Always fall back to display_name/job_name when there is no real
    # Jenkins URL so that file/raw analyses retain a human-readable label
    # in GitHub/Jira previews.
    if not jenkins_url:
        # Use child job context when scoped to a child, parent context otherwise
        if child_job_name and child:
            job_name = child.get("job_name", "")
            build_number = child.get("build_number")
        else:
            job_name = result_data.get("display_name") or result_data.get(
                "job_name", ""
            )
            build_number = result_data.get("build_number")
        if job_name and build_number:
            jenkins_url = f"{job_name} #{build_number}"
        else:
            jenkins_url = job_name or ""

    return report_url, jenkins_url


def _attach_result_links(payload: dict, base_url: str, job_id: str) -> dict:
    """Attach ``base_url`` and ``result_url`` to a response payload."""
    payload["base_url"] = base_url
    result_url = f"{base_url}/results/{job_id}"
    payload["result_url"] = result_url
    return payload


async def _attach_origin_job_info(result: dict) -> None:
    """Attach origin job reference when the result is a re-analysis.

    If ``request_params.reanalyzed_from_job_id`` exists, adds
    ``reanalyzed_from_job_id`` and ``origin_job_name`` to the top-level
    response.  Prefers the denormalized ``reanalyzed_from_job_name``
    stored at creation time; falls back to a DB lookup for legacy data.
    """
    params = (result.get("result") or {}).get("request_params", {})
    origin_id = params.get("reanalyzed_from_job_id", "")
    if not origin_id:
        return
    result["reanalyzed_from_job_id"] = origin_id

    # Fast path: use denormalized name stored at re-analysis creation time
    stored_name = params.get("reanalyzed_from_job_name", "")
    if stored_name:
        result["origin_job_name"] = stored_name
        return

    # Fallback for legacy data: resolve via DB lookup
    try:
        origin = await get_result(origin_id)
    except Exception:
        logger.warning("Failed to resolve origin job %s", origin_id, exc_info=True)
        origin = None
    if origin and origin.get("result"):
        origin_result = origin["result"]
        result["origin_job_name"] = (
            origin_result.get("display_name")
            or origin_result.get("job_name")
            or origin_id
        )
    else:
        result["origin_job_name"] = origin_id


def _recompose_repo_spec(url: str, ref: str) -> str:
    """Recompose 'url:ref' from stored components. Returns url alone when ref is empty."""
    if not url:
        return ""
    return f"{url}:{ref}" if ref else url


def _is_encrypted_value(value: Any) -> bool:
    """Return True if *value* looks like an undecrypted encrypted field."""
    return isinstance(value, str) and value.startswith("enc:")


def _check_reanalyze_authorization(request: Request, params: dict) -> None:
    """Check that requester is original submitter or admin."""
    original_submitter = params.get("submitted_by", "")
    requesting_user = getattr(request.state, "username", "")
    is_admin = getattr(request.state, "is_admin", False)
    if not is_admin and requesting_user != original_submitter:
        raise HTTPException(
            status_code=403,
            detail="Only the original submitter or an admin can re-analyze this job",
        )


def _validate_decrypted_sensitive_fields(decrypted_params: dict) -> None:
    """Fail fast if any sensitive field is still encrypted (key changed / corrupt)."""
    for key in SENSITIVE_KEYS:
        value = decrypted_params.get(key)
        if _is_encrypted_value(value):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot re-analyze: stored {key} could not be decrypted",
            )
    for repo in decrypted_params.get("additional_repos") or []:
        if isinstance(repo, dict) and _is_encrypted_value(repo.get("token")):
            raise HTTPException(
                status_code=400,
                detail="Cannot re-analyze: stored additional_repos token could not be decrypted",
            )


_ANALYSIS_SETTINGS_FIELDS = (
    "ai_provider",
    "ai_model",
    "raw_prompt",
    "issue_prompt",
    "tests_repo_url",
    "tests_repo_token",
    "peer_analysis_max_rounds",
    "enable_jira",
    "jira_url",
    "jira_email",
    "jira_api_token",
    "jira_pat",
    "jira_project_key",
    "jira_ssl_verify",
    "jira_max_results",
    "github_token",
    "ai_call_timeout",
    "max_concurrent_ai_calls",
)


def _copy_analysis_settings(decrypted_params: dict, unified_fields: dict) -> None:
    """Copy analysis settings from stored params to unified request fields."""
    for field in _ANALYSIS_SETTINGS_FIELDS:
        if field in decrypted_params and decrypted_params[field] is not None:
            if field == "tests_repo_url":
                ref = decrypted_params.get("tests_repo_ref", "")
                url = decrypted_params[field]
                unified_fields[field] = f"{url}:{ref}" if ref else url
            else:
                unified_fields[field] = decrypted_params[field]

    if "peer_ai_configs" in decrypted_params:
        unified_fields["peer_ai_configs"] = decrypted_params["peer_ai_configs"]
    if (
        "additional_repos" in decrypted_params
        and decrypted_params["additional_repos"] is not None
    ):
        unified_fields["additional_repos"] = decrypted_params["additional_repos"]


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
        name=params.get("name") or None,
        ai_provider=params.get("ai_provider", ""),
        ai_model=params.get("ai_model", ""),
        wait_for_completion=params.get("wait_for_completion", True),
        poll_interval_minutes=params.get("poll_interval_minutes", 2),
        max_wait_minutes=params.get("max_wait_minutes", 0),
        enable_jira=params.get("enable_jira"),
        raw_prompt=params.get("raw_prompt") or None,
        issue_prompt=params.get("issue_prompt") or None,
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
        "ai_call_timeout",
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

    # Tests repo URL — use `is not None` so an explicit empty string
    # (clearing the field) is preserved instead of silently dropped.
    stored_tests_repo_url = params.get("tests_repo_url")
    if stored_tests_repo_url is not None:
        recomposed = _recompose_repo_spec(
            stored_tests_repo_url, params.get("tests_repo_ref", "")
        )
        overrides["tests_repo_url"] = recomposed

    # SecretStr fields — use `is not None` to preserve explicit clears.
    if params.get("jira_api_token") is not None:
        overrides["jira_api_token"] = (
            SecretStr(params["jira_api_token"]) if params["jira_api_token"] else None
        )
    if params.get("jira_pat") is not None:
        overrides["jira_pat"] = (
            SecretStr(params["jira_pat"]) if params["jira_pat"] else None
        )
    if params.get("github_token") is not None:
        overrides["github_token"] = (
            SecretStr(params["github_token"]) if params["github_token"] else None
        )
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

# Track analysis background tasks by job_id for abort support
_job_tasks: dict[str, asyncio.Task] = {}


def _remove_job_task(job_id: str) -> Callable[[asyncio.Task[object]], None]:
    """Create a done-callback that removes a job from the task tracker."""

    def _callback(_task: asyncio.Task[object]) -> None:
        _job_tasks.pop(job_id, None)

    return _callback


def _register_job_task(job_id: str, task: asyncio.Task) -> None:
    """Register an analysis task for tracking and abort support."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_remove_job_task(job_id))
    _job_tasks[job_id] = task


async def _preserve_request_params(job_id: str, result_data: dict) -> None:
    """Copy persisted enqueue-time fields from the stored result into result_data.

    The initial ``save_result`` persists ``request_params``, ``tags``, and
    ``display_name`` but the ``AnalysisResult`` model dump produced when
    analysis finishes does not include those keys.  Without this merge the
    fields would be silently lost when ``update_status`` overwrites
    ``result_json``.

    Args:
        job_id: The analysis job identifier.
        result_data: Mutable dict that will be written to ``result_json``.
            Modified in place to add preserved fields when available.
    """
    stored = await get_result(job_id, strip_sensitive=False)
    if stored and stored.get("result"):
        stored_result = stored["result"]
        for key in ("request_params", "tags", "display_name"):
            if key in stored_result and key not in result_data:
                result_data[key] = stored_result[key]


async def _fail_resumed_waiting_job(job_id: str, result_data: dict, error: str) -> None:
    """Mark a resumed waiting job as failed with a standard payload.

    Args:
        job_id: The job identifier.
        result_data: The stored result data dict for the job.
        error: Human-readable error message.
    """
    fail_data = {
        "job_name": result_data.get("job_name", ""),
        "display_name": result_data.get("display_name")
        or result_data.get("job_name", ""),
        "build_number": result_data.get("build_number", 0),
        "error": error,
    }
    if "request_params" in result_data:
        fail_data["request_params"] = result_data["request_params"]
    if "tags" in result_data:
        fail_data["tags"] = result_data["tags"]
    await storage.update_status(job_id, "failed", fail_data)
    notify_active_count_changed()
    notify_dashboard_changed()
    notify_job_status_changed(job_id)


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
                        f"{result_data.get('display_name') or result_data.get('job_name')} #{result_data.get('build_number')} "
                        f"after {merged.max_wait_minutes} minutes (deadline passed during restart)"
                    ),
                )
                continue
            merged_data = merged.model_dump(mode="python")
            merged_data["max_wait_minutes"] = max(1, math.ceil(remaining))
            merged = Settings.model_validate(merged_data)

        # Re-check status in case job was aborted during startup
        current = await storage.get_result(job["job_id"])
        if current and current.get("status") in ("aborted", "completed", "failed"):
            logger.info(
                f"Skipping resumed job {job['job_id']} — status is {current['status']}"
            )
            continue

        resumed_username = result_data.get("request_params", {}).get("submitted_by", "")
        if not resumed_username:
            logger.warning(
                "Resumed job %s has no submitted_by in stored params "
                "(pre-migration job); history-aware classification will be disabled",
                job["job_id"],
            )
        task = asyncio.create_task(
            process_analysis_with_id(
                job["job_id"], body, merged, username=resumed_username
            )
        )
        _register_job_task(job["job_id"], task)
        logger.info(
            f"Resumed waiting job {job['job_id']} "
            f"({result_data.get('display_name') or result_data.get('job_name')} #{result_data.get('build_number')})"
        )


async def _safe_preload_cursor_models() -> None:
    """Pre-populate cursor model list in background. Best-effort."""
    try:
        await list_models("cursor")
    except Exception:
        logger.debug("Failed to preload cursor models", exc_info=True)


async def _backfill_job_metadata(rules: list[dict]) -> None:
    """Retroactively assign metadata to existing jobs missing metadata. Best-effort."""
    try:
        # Get all unique job names from results
        job_names = await list_distinct_job_names()
        logger.info(
            "Backfill: scanning %d distinct job name(s) for metadata", len(job_names)
        )

        assigned = 0
        for name in job_names:
            result = await storage.auto_assign_job_metadata(name, rules)
            if result is not None:
                assigned += 1

        if assigned:
            logger.info("Backfilled metadata for %d job(s)", assigned)
    except Exception:
        logger.debug("Failed to backfill job metadata", exc_info=True)


async def _deferred_resume_waiting_jobs(waiting_jobs: list[dict]) -> None:
    """Resume waiting jobs after startup is complete.

    Waits briefly so uvicorn finishes binding and the app is ready to
    serve internal API requests before any resumed job transitions to
    the "running" phase.
    """
    await asyncio.sleep(1)
    await _resume_waiting_jobs(waiting_jobs)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _install_job_id_filter()
    _setup_usage_recorder()

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

    # Load DB setting overrides into env before get_settings() is called
    from rootcoz.config import load_db_settings_into_env

    await load_db_settings_into_env()

    # Track which env vars came from DB (for safe cleanup on DELETE)
    try:
        db_settings = await storage.get_server_settings()
        for db_key in db_settings:
            _db_injected_env_vars.add(db_key.upper())
    except Exception:
        pass

    try:
        # Pre-populate cursor models in background (don't block startup)
        task = asyncio.create_task(_safe_preload_cursor_models())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        # Retroactively assign metadata to jobs that don't have it yet
        settings = get_settings()
        if settings.admin_wait_approve_msg:
            logger.info("[startup] ADMIN_WAIT_APPROVE_MSG configured")
        if settings.metadata_rules:
            task = asyncio.create_task(_backfill_job_metadata(settings.metadata_rules))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        waiting_jobs = await storage.mark_stale_results_failed()
        notify_active_count_changed()
        notify_dashboard_changed()
        if waiting_jobs:
            # Schedule resumption as a background task so it runs after the
            # app is fully started and ready to serve internal API requests.
            task = asyncio.create_task(_deferred_resume_waiting_jobs(waiting_jobs))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        yield
    finally:
        pass


app = FastAPI(
    title="rootcoz",
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
    logger.info("Static assets directory mounted at /assets/")


class ErrorTrackingMiddleware(BaseHTTPMiddleware):
    """Track request counts and error rates for monitoring."""

    _SKIP_PATHS = frozenset(
        {"/health", "/api/health", "/metrics", "/favicon.ico", "/favicon.svg"}
    )

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
                        f"\u26a0\ufe0f rootcoz high 5xx error rate: {server_error_rate:.0%} "
                        f"({server_errors}/{total_requests} requests "
                        f"in {snap['window_seconds']}s window)",
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        except Exception:  # alert scheduling must never break request handling
            logger.debug("Failed to schedule high-error-rate alert", exc_info=True)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)
        method = request.method
        try:
            response = await call_next(request)
        except Exception:
            error_tracker.record_request(500, method=method)
            self._schedule_high_error_rate_alert()
            raise
        error_tracker.record_request(response.status_code, method=method)
        if response.status_code >= 500:
            self._schedule_high_error_rate_alert()
        return response


# Legacy cookie names from the jenkins-job-insight era.
# Read as fallback so existing sessions survive the rename;
# cleared once the new cookie is set.
_LEGACY_COOKIE_MAP: dict[str, str] = {
    "rootcoz_username": "jji_username",
    "rootcoz_session": "jji_session",
}


def _read_cookie(request: Request, name: str) -> str:
    """Read cookie with legacy fallback."""
    value = request.cookies.get(name, "")
    if not value:
        legacy = _LEGACY_COOKIE_MAP.get(name)
        if legacy:
            value = request.cookies.get(legacy, "")
    return value


def _set_username_cookie(response: Response, username: str, *, secure: bool) -> None:
    """Set the rootcoz_username cookie with consistent attributes."""
    response.set_cookie(
        "rootcoz_username",
        username,
        path="/",
        max_age=365 * 24 * 60 * 60,
        samesite="lax",
        secure=secure,
    )
    # Clear legacy cookie after migration
    response.delete_cookie("jji_username", path="/")


_APPROVAL_STATUS_RESPONSES: dict[str, str] = {
    "pending": "Your account is awaiting admin approval",
    "rejected": "Your account has been rejected",
}


def _maybe_add_custom_approval_msg(content: dict, settings: Settings) -> None:
    """Append custom admin approval message to response content if configured."""
    if settings.admin_wait_approve_msg:
        content["custom_message"] = settings.admin_wait_approve_msg


def _blocked_user_status_response(user_status: str | None) -> JSONResponse | None:
    """Return a 403 JSONResponse if user_status is pending/rejected, else None."""
    detail = _APPROVAL_STATUS_RESPONSES.get(user_status or "")
    if detail is None:
        return None
    settings = get_settings()
    content: dict = {"detail": detail, "status": user_status}
    if user_status == "pending":
        _maybe_add_custom_approval_msg(content, settings)
    return JSONResponse(
        status_code=403,
        content=content,
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate all requests via session cookie, Bearer token, or trusted SSO header.

    All users (admin and regular) must authenticate. The rootcoz_username
    cookie is for display/tracking only — it does not grant API access.
    SSO deployments (trust_proxy_headers) use X-Forwarded-User as an
    alternative to API key authentication.
    """

    # Public paths that bypass authentication.
    # /api/releases/latest is intentionally public — it only proxies the
    # latest GitHub release metadata (version, changelog) with no
    # sensitive data, similar to /health.
    _PUBLIC_PATHS = frozenset(
        {
            "/login",
            "/health",
            "/api/health",
            "/api/auth/register",
            "/api/auth/login",
            "/api/auth/needs-key",
            "/api/auth/pending-status",
            "/api/releases/latest",
            "/metrics",
            "/pending",
            "/favicon.ico",
            "/favicon.svg",
            "/sw.js",
        }
    )

    async def dispatch(self, request: Request, call_next):
        # CORS preflight requests must pass through without authentication
        if request.method == "OPTIONS":
            request.state.username = ""
            request.state.is_admin = False
            request.state.role = "user"
            origin = request.headers.get("origin", "*")
            return Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": request.headers.get(
                        "access-control-request-method",
                        "GET, POST, PUT, DELETE, OPTIONS",
                    ),
                    "Access-Control-Allow-Headers": request.headers.get(
                        "access-control-request-headers", "authorization, content-type"
                    ),
                    "Access-Control-Max-Age": "86400",
                    "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
                },
            )

        path = request.url.path

        # Set defaults
        request.state.username = ""
        request.state.is_admin = False
        request.state.role = "user"

        # Public paths and static assets — pass through
        # (but /login may need SSO redirect, handled below;
        #  it stays in _PUBLIC_PATHS for non-SSO users who need the page)
        if path.startswith("/assets/") or (
            path in self._PUBLIC_PATHS and path != "/login"
        ):
            return await call_next(request)

        settings = get_settings()

        # SSO: when trust_proxy_headers is enabled and X-Forwarded-User is
        # present, auto-identify the user and redirect /login → /
        proxy_username = ""
        if settings.trust_proxy_headers:
            proxy_username = request.headers.get("x-forwarded-user", "").strip()

        if path.startswith("/login"):
            if proxy_username and proxy_username.lower() != "admin":
                # Session auth takes precedence over X-Forwarded-User —
                # only check when an SSO redirect would otherwise fire.
                session_token = _read_cookie(request, "rootcoz_session")
                if session_token and await storage.get_session(session_token):
                    return await call_next(request)
                # SSO user hitting /login — redirect to dashboard
                response = RedirectResponse(url="/", status_code=303)
                if _read_cookie(request, "rootcoz_username") != proxy_username:
                    _set_username_cookie(
                        response, proxy_username, secure=settings.secure_cookies
                    )
                return response
            return await call_next(request)

        is_admin = False
        username = ""
        authenticated_admin = False
        has_valid_session = False

        # 1. Check session cookie (rootcoz_session) — user or admin session
        session_token = _read_cookie(request, "rootcoz_session")
        if session_token:
            session = await storage.get_session(session_token)
            if session:
                is_admin = bool(session["is_admin"])
                username = str(session["username"])
                authenticated_admin = is_admin
                has_valid_session = True

                # Renew session (sliding window) — only when <50% TTL remains
                expires_at_str = session.get("expires_at", "")
                if expires_at_str:
                    try:
                        expires_at = datetime.strptime(
                            str(expires_at_str), "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=UTC)
                        remaining = expires_at - datetime.now(UTC)
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
                    has_valid_session = True
                else:
                    user = await storage.get_user_by_key(token)
                    if user:
                        username = str(user["username"])
                        has_valid_session = True
                        if user.get("role") == "admin":
                            is_admin = True
                            authenticated_admin = True
                    else:
                        # Fall back to session token via Bearer header
                        # (used by AI for internal API calls)
                        session = await storage.get_session(token)
                        if session:
                            is_admin = bool(session["is_admin"])
                            username = str(session["username"])
                            authenticated_admin = is_admin
                            has_valid_session = True

        # 3. Check X-Forwarded-User header (SSO via trusted proxy)
        if not username and proxy_username:
            if proxy_username.lower() != "admin":
                username = proxy_username
                has_valid_session = True
                # Flag that we need to set the rootcoz_username cookie on the response
                request.state.set_proxy_cookie = proxy_username

        # 4. Fall back to rootcoz_username cookie (regular users)
        if not username:
            cookie_username = _read_cookie(request, "rootcoz_username")
            if cookie_username.lower() == "admin":
                # Reserved username — only valid via session/bearer auth
                cookie_username = ""
            username = cookie_username

        request.state.username = username
        request.state.is_admin = is_admin
        request.state.role = "admin" if is_admin else "user"

        # Track user activity only for authenticated identities
        if has_valid_session and username:
            task = asyncio.create_task(_safe_track_user(username))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Admin-only path enforcement
        if path.startswith("/api/admin/"):
            if not authenticated_admin:
                return JSONResponse(
                    status_code=403, content={"detail": "Admin access required"}
                )

        # Require authentication for all non-public, non-optional paths
        if not has_valid_session and path not in self._PUBLIC_PATHS:
            accept = request.headers.get("accept", "")
            if "text/html" in accept and not path.startswith("/api/"):
                return RedirectResponse(url="/login", status_code=303)
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Authentication required. Please log in with your API key."
                },
            )

        # Check pending user status when REQUIRE_APPROVAL is enabled.
        # Admin users and the bootstrap admin always bypass this check.
        if (
            has_valid_session
            and username
            and not is_admin
            and settings.require_approval
            and path not in self._PUBLIC_PATHS
        ):
            user_status = await storage.get_user_status(username)
            blocked = _blocked_user_status_response(user_status)
            if blocked is not None:
                return blocked

        response = await call_next(request)

        # Set rootcoz_username cookie from X-Forwarded-User header (SSO)
        if getattr(request.state, "set_proxy_cookie", None):
            proxy_cookie_value = request.state.set_proxy_cookie
            if _read_cookie(request, "rootcoz_username") != proxy_cookie_value:
                _set_username_cookie(
                    response, proxy_cookie_value, secure=settings.secure_cookies
                )

        # Refresh session cookie max_age if session was renewed
        if getattr(request.state, "renew_session_token", None):
            # Skip if downstream handler already set/cleared rootcoz_session
            # (e.g., login sets a new session, logout deletes it)
            path = request.url.path
            if path not in ("/api/auth/login", "/api/auth/logout"):
                settings = get_settings()
                response.set_cookie(
                    "rootcoz_session",
                    request.state.renew_session_token,
                    httponly=True,
                    samesite="strict",
                    secure=settings.secure_cookies,
                    max_age=storage.SESSION_TTL_SECONDS,
                )
                # Clear legacy cookie after migration
                response.delete_cookie(
                    "jji_session",
                    httponly=True,
                    samesite="strict",
                    secure=settings.secure_cookies,
                )

        return response


app.add_middleware(AuthMiddleware)
app.add_middleware(ErrorTrackingMiddleware)


_BODY_LOGGING_SKIP_PATHS = frozenset(
    {"/api/feedback/preview", "/api/feedback/create", "/analyze"}
)


class RequestBodyLoggingMiddleware(BaseHTTPMiddleware):
    """Log incoming request bodies at DEBUG level with sensitive data masked."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _BODY_LOGGING_SKIP_PATHS or request.url.path.startswith(
            "/api/chat/"
        ):
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


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Add long-lived cache headers for Vite-hashed static assets."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if (
            request.url.path.startswith("/assets/")
            and 200 <= response.status_code < 400
        ):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.add_middleware(CacheControlMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)


def _mask_pydantic_error(error: dict) -> dict:
    """Mask sensitive input values in a Pydantic validation error dict."""
    result = dict(error)
    loc = error.get("loc") or ()
    field = loc[-1] if loc else ""
    if isinstance(field, str) and is_sensitive_key(field) and "input" in result:
        result["input"] = "***"
    elif "input" in result:
        result["input"] = mask_sensitive_fields(result["input"])
    return result


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Log 422 validation error details and return standard response."""
    if request.url.path in _BODY_LOGGING_SKIP_PATHS or request.url.path.startswith(
        "/api/chat/"
    ):
        raw_errors = jsonable_encoder(exc.errors())
        masked_errors = [_mask_pydantic_error(e) for e in raw_errors]
        for error in masked_errors:
            if "input" in error:
                error["input"] = "<redacted>"
        logger.warning(
            "RequestValidationError on %s %s: errors=%s body=<skipped>",
            request.method,
            request.url.path,
            masked_errors,
        )
        return JSONResponse(
            status_code=422,
            content={"detail": masked_errors},
        )
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
        except Exception:  # masking must never break the 422 response
            masked_body = "<unable to mask>"
    raw_errors = jsonable_encoder(exc.errors())
    masked_errors = [_mask_pydantic_error(e) for e in raw_errors]
    logger.warning(
        "RequestValidationError on %s %s: errors=%s",
        request.method,
        request.url.path,
        masked_errors,
    )
    logger.debug(
        "RequestValidationError body on %s %s: %s",
        request.method,
        request.url.path,
        masked_body,
    )
    # Mask sensitive values in the response body as well.
    response_errors = jsonable_encoder(exc.errors())
    masked_response = [_mask_pydantic_error(e) for e in response_errors]
    return JSONResponse(
        status_code=422,
        content={"detail": masked_response},
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
            detail=(
                f"No AI provider configured. Set AI_PROVIDER env var or"
                f" pass ai_provider in request body."
                f" Valid providers: {', '.join(sorted(VALID_AI_PROVIDERS))}"
            ),
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


def _resolve_ai_config(body: BaseAnalysisRequest) -> tuple[str, str]:
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
        body: The analysis request.
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
        "ai_call_timeout",
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
        all_failures,
        settings,
        ai_provider,
        ai_model,
        job_id=job_id,
    )


async def _enrich_result_with_tests_repo_matches(
    failures: list[FailureAnalysis | ChildJobAnalysis],
    settings: Settings,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
    tests_repo_url: str | None = None,
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
            not None and ``settings.tests_repo_url`` is unset, a
            temporary settings copy is created so that
            ``enrich_with_tests_repo_matches`` sees the URL.
    """
    effective_url = (
        tests_repo_url
        if tests_repo_url is not None
        else str(settings.tests_repo_url or "")
    )
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
        all_failures,
        settings,
        ai_provider,
        ai_model,
        job_id=job_id,
    )


_AI_SESSION_TTL_HOURS = 8  # Short-lived for AI internal API calls


def _get_display_name(body: AnalyzeRequest | UnifiedAnalyzeRequest) -> str:
    """Get display name from request, falling back to job_name."""
    return body.name or body.job_name or ""


async def _auto_assign_metadata(
    display_name: str, metadata_rules: list[dict] | None
) -> None:
    """Best-effort metadata auto-assignment."""
    if not metadata_rules:
        return
    try:
        await storage.auto_assign_job_metadata(display_name, metadata_rules)
    except Exception:
        logger.warning(
            "Failed to auto-assign metadata for job '%s'",
            display_name,
            exc_info=True,
        )


async def _create_ai_auth_header(username: str) -> str:
    """Create a short-lived session token for AI internal API calls.

    Returns a Bearer auth header string, or empty string on failure/no user.
    """
    if not username:
        return ""
    try:
        session_token = await storage.create_session(
            username,
            ttl_hours=_AI_SESSION_TTL_HOURS,
        )
        return f"Bearer {session_token}"
    except Exception:
        logger.warning("Failed to create AI session for history access", exc_info=True)
        return ""


async def _cleanup_ai_session(auth_header: str) -> None:
    """Revoke the short-lived AI session token created for an analysis run."""
    if not auth_header:
        return
    try:
        token = auth_header.removeprefix("Bearer ").strip()
        if token:
            await storage.delete_session(token)
    except Exception:
        logger.warning("Failed to delete AI session token", exc_info=True)


async def _preflight_sidecar_check(
    job_id: str,
    ai_provider: str,
    ai_model: str,
    display_name: str,
    build_number: int | None = None,
    jenkins_url: str = "",
    job_name: str = "",
) -> bool:
    """Check sidecar availability, fail the job if unreachable. Returns True if available."""
    available, msg = await check_sidecar_available()
    if available:
        return True
    logger.error(
        "AI sidecar sanity check failed for job %s (%s/%s)",
        job_id,
        ai_provider,
        ai_model,
    )
    logger.error("AI preflight failed for job %s: %s", job_id, msg)
    fail_result = FailureAnalysisResult(
        job_id=job_id,
        status="failed",
        summary=make_user_friendly_error(msg),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    fail_data = fail_result.model_dump(mode="json")
    fail_data["error"] = fail_result.summary
    fail_data["job_name"] = job_name or display_name
    if build_number is not None:
        fail_data["build_number"] = build_number
    if jenkins_url:
        fail_data["jenkins_url"] = jenkins_url
    await _preserve_request_params(job_id, fail_data)
    await update_status(job_id, "failed", fail_data)
    notify_active_count_changed()
    notify_dashboard_changed()
    notify_job_status_changed(job_id)
    return False


async def process_analysis_with_id(
    job_id: str, body: AnalyzeRequest, settings: Settings, username: str = ""
) -> None:
    """Background task to process analysis with a pre-generated job_id.

    Args:
        job_id: Pre-generated job ID for tracking.
        body: The analysis request.
        settings: Application settings.
        username: Submitting user for session-based AI auth.
    """
    job_id_var.set(job_id)

    logger.info(
        f"Analysis request received for {body.job_name} #{body.build_number} "
        f"(job_id: {job_id})"
    )

    auth_header = ""
    try:
        # Validate AI config early -- before potentially waiting hours for Jenkins.
        # This ensures invalid provider/model fails fast instead of after a long wait.
        ai_provider, ai_model = _resolve_ai_config(body)

        # Pre-flight: verify AI is reachable before expensive Jenkins wait
        display_name = _get_display_name(body)
        if not await _preflight_sidecar_check(
            job_id,
            ai_provider,
            ai_model,
            display_name,
            build_number=body.build_number,
            jenkins_url=build_jenkins_url(
                settings.jenkins_url or "", body.job_name or "", body.build_number or 0
            )
            if settings.jenkins_url
            else "",
            job_name=body.job_name or "",
        ):
            return

        # Wait for Jenkins job to finish if requested and Jenkins is configured
        if settings.wait_for_completion and not settings.jenkins_url:
            logger.info(
                f"Wait requested for job {job_id} but jenkins_url not configured, skipping wait"
            )

        if settings.wait_for_completion and settings.jenkins_url:
            await update_status(job_id, "waiting")
            notify_active_count_changed()
            notify_dashboard_changed()
            notify_job_status_changed(job_id)
            await safe_update_progress(job_id, "waiting_for_jenkins")
            notify_job_status_changed(job_id)

            completed, wait_error = await wait_for_jenkins_completion(
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
                display_name = _get_display_name(body)
                fail_data = {
                    "job_name": body.job_name,
                    "display_name": display_name,
                    "build_number": body.build_number,
                    "error": wait_error,
                }
                await _preserve_request_params(job_id, fail_data)
                await update_status(
                    job_id,
                    "failed",
                    fail_data,
                )
                notify_active_count_changed()
                notify_dashboard_changed()
                notify_job_status_changed(job_id)
                return

        auth_header = await _create_ai_auth_header(username)

        logger.debug(
            f"process_analysis_with_id: updating status to running, job_id={job_id}"
        )
        await update_status(job_id, "running")
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        await safe_update_progress(job_id, "analyzing")
        notify_job_status_changed(job_id)

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
            auth_header=auth_header,
        )

        # Enrich PRODUCT BUG failures with Jira matches
        if _resolve_enable_jira(body, settings):
            await safe_update_progress(job_id, "enriching_jira")
            notify_job_status_changed(job_id)
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
        request_tests_repo_url = (
            str(body.tests_repo_url)
            if body.tests_repo_url is not None
            else str(settings.tests_repo_url or "")
        )
        if settings.tests_repo_url or request_tests_repo_url:
            await safe_update_progress(job_id, "enriching_tests_repo")
            notify_job_status_changed(job_id)
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

        await safe_update_progress(job_id, "saving")
        notify_job_status_changed(job_id)
        logger.debug(
            f"process_analysis_with_id: saving completed result, job_id={job_id}"
        )
        result_data = result.model_dump(mode="json")
        if result.status == "failed":
            # Prefer child job error notes over the generic summary
            child_errors = [
                c.get("note", "")
                for c in result_data.get("child_job_analyses", [])
                if c.get("note")
            ]
            if child_errors:
                result_data["error"] = "; ".join(child_errors)
            else:
                result_data["error"] = result.summary
        await _preserve_request_params(job_id, result_data)

        # Attach token usage summary before persisting
        await _attach_token_usage(job_id, result_data)

        # Save to storage — do NOT persist base_url / result_url as they are
        # request-derived and re-generated on every GET to avoid host-header
        # injection from being stored.
        await update_status(job_id, result.status, result_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        notify_token_usage_changed()
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
        await _auto_assign_metadata(body.job_name, settings.metadata_rules)

        # Reveal classifications created during analysis
        await storage.make_classifications_visible(job_id)

    except asyncio.CancelledError:
        logger.info(f"Analysis task cancelled for job_id={job_id}")
        return

    except Exception as e:
        logger.exception(f"Analysis failed for job {job_id}")
        user_error = make_user_friendly_error(e)
        display_name = _get_display_name(body)
        error_data: dict = {
            "job_name": body.job_name,
            "display_name": display_name,
            "build_number": body.build_number,
            "error": user_error,
        }
        await _preserve_request_params(job_id, error_data)

        # Attach token usage even on failure — partial AI calls may have been recorded
        await _attach_token_usage(job_id, error_data)

        await update_status(job_id, "failed", error_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        notify_token_usage_changed()

    finally:
        await _cleanup_ai_session(auth_header)


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


def _apply_base_analysis_overrides(
    params: dict,
    body: "BaseAnalysisRequest",
    merged: "Settings",
) -> None:
    """Apply BaseAnalysisRequest overrides to a base params dict.

    Centralises the serialisation of per-request overrides that both the
    unified ``/analyze`` endpoint and the file/raw re-analyze path need.
    Mutates *params* in place.
    """
    params["enable_jira"] = _resolve_enable_jira(body, merged)
    params["jira_url"] = (
        body.jira_url if body.jira_url is not None else (merged.jira_url or "")
    )
    params["jira_email"] = (
        body.jira_email if body.jira_email is not None else (merged.jira_email or "")
    )
    params["jira_api_token"] = (
        body.jira_api_token
        if body.jira_api_token is not None
        else (merged.jira_api_token.get_secret_value() if merged.jira_api_token else "")
    )
    params["jira_pat"] = (
        body.jira_pat
        if body.jira_pat is not None
        else (merged.jira_pat.get_secret_value() if merged.jira_pat else "")
    )
    params["jira_project_key"] = (
        body.jira_project_key
        if body.jira_project_key is not None
        else (merged.jira_project_key or "")
    )
    params["jira_ssl_verify"] = (
        body.jira_ssl_verify
        if body.jira_ssl_verify is not None
        else merged.jira_ssl_verify
    )
    params["jira_max_results"] = (
        body.jira_max_results
        if body.jira_max_results is not None
        else merged.jira_max_results
    )
    params["github_token"] = (
        body.github_token
        if body.github_token is not None
        else (merged.github_token.get_secret_value() if merged.github_token else "")
    )
    params["ai_call_timeout"] = (
        body.ai_call_timeout
        if body.ai_call_timeout is not None
        else merged.ai_call_timeout
    )
    params["max_concurrent_ai_calls"] = (
        body.max_concurrent_ai_calls
        if body.max_concurrent_ai_calls is not None
        else merged.max_concurrent_ai_calls
    )
    # Always persist peer_analysis_max_rounds so non-default values survive
    # re-analyze round-trips.
    params["peer_analysis_max_rounds"] = merged.peer_analysis_max_rounds


def _stamp_reanalysis_metadata(
    request_params: dict,
    reanalyzed_from_job_id: str,
    reanalyzed_from_job_name: str,
) -> None:
    """Write re-analysis origin fields into *request_params* when present."""
    if reanalyzed_from_job_id:
        request_params["reanalyzed_from_job_id"] = reanalyzed_from_job_id
        if reanalyzed_from_job_name:
            request_params["reanalyzed_from_job_name"] = reanalyzed_from_job_name


async def _enqueue_file_raw_analysis(
    body: "UnifiedAnalyzeRequest",
    merged: "Settings",
    resolved_peers: list | None,
    display_name: str,
    analysis_type: str,
    base_url: str,
    username: str,
    *,
    tags: list[str] | None = None,
    message_prefix: str = "Analysis",
    reanalyzed_from_job_id: str = "",
    reanalyzed_from_job_name: str = "",
) -> dict:
    """Build params, persist initial state, spawn task, and return response.

    Shared by the ``/analyze`` file/raw path and the ``/re-analyze`` file/raw
    path.  Callers handle request-specific reconstruction/validation before
    calling this helper with a ready-to-go *body*.

    Args:
        body: The analysis request (``UnifiedAnalyzeRequest`` or similar).
        merged: Merged settings.
        resolved_peers: Validated peer AI configs.
        display_name: Human-readable job name.
        analysis_type: ``"file"`` or ``"raw"``.
        base_url: Server base URL for result links.
        username: Authenticated user who submitted the request.
        tags: Optional pre-built tags list. When ``None``, uses ``body.tags``.
        message_prefix: Prefix for the queued-message ("Analysis" or
            "Re-analysis").

    Returns:
        JSON-serialisable response dict with ``status``, ``job_id``, links.
    """
    ai_provider, ai_model = _resolve_ai_config_values(body.ai_provider, body.ai_model)

    # Resolve repos
    tests_repo_url_raw = (
        str(body.tests_repo_url)
        if body.tests_repo_url is not None
        else str(merged.tests_repo_url or "")
    )
    tests_repo_url, tests_repo_ref = parse_repo_ref(tests_repo_url_raw)
    resolved_tests_repo_token = (
        resolve_tests_repo_token(body, merged) if tests_repo_url else ""
    )
    additional_repos_list = resolve_additional_repos(body, merged)

    job_id = str(uuid.uuid4())
    job_id_var.set(job_id)

    # Append short job_id suffix to generic fallback names for uniqueness
    _GENERIC_FALLBACK_NAMES = {
        "file-analysis",
        "raw-analysis",
        "file-re-analysis",
        "raw-re-analysis",
    }
    if display_name in _GENERIC_FALLBACK_NAMES and not body.name:
        display_name = f"{display_name}-{job_id[:8]}"

    # Build and persist initial state
    base_params = _build_base_request_params(
        ai_provider,
        ai_model,
        resolved_peers,
        tests_repo_url=tests_repo_url,
        tests_repo_token=resolved_tests_repo_token,
        tests_repo_ref=tests_repo_ref,
        additional_repos=additional_repos_list,
    )
    base_params["raw_prompt"] = body.raw_prompt or ""
    base_params["issue_prompt"] = body.issue_prompt or ""
    base_params["analysis_type"] = analysis_type
    base_params["original_name"] = body.name or ""
    _apply_base_analysis_overrides(base_params, body, merged)

    if analysis_type == "file":
        base_params["raw_xml"] = body.raw_xml
    elif analysis_type == "raw":
        assert body.failures is not None
        base_params["failures"] = [f.model_dump() for f in body.failures]

    initial_result: dict = {
        "job_name": display_name,
        "display_name": display_name,
        "request_params": encrypt_sensitive_fields(base_params),
    }
    initial_result["request_params"]["submitted_by"] = username
    _stamp_reanalysis_metadata(
        initial_result["request_params"],
        reanalyzed_from_job_id,
        reanalyzed_from_job_name,
    )
    effective_tags = tags if tags is not None else (body.tags or None)
    if effective_tags:
        initial_result["tags"] = effective_tags
    await save_result(job_id, "", "pending", initial_result)
    notify_active_count_changed()
    notify_dashboard_changed()

    # Spawn background task
    task = asyncio.create_task(
        _process_file_raw_analysis(
            job_id=job_id,
            body=body,
            merged=merged,
            display_name=display_name,
            ai_provider=ai_provider,
            ai_model=ai_model,
            peer_ai_configs=resolved_peers,
            tests_repo_url=tests_repo_url,
            tests_repo_ref=tests_repo_ref,
            resolved_tests_repo_token=resolved_tests_repo_token,
            additional_repos_list=additional_repos_list,
            base_url=base_url,
            username=username,
        )
    )
    _register_job_task(job_id, task)

    response: dict = {
        "status": "queued",
        "job_id": job_id,
        "message": f"{message_prefix} job queued. Poll /results/{job_id} for status.",
    }
    return _attach_result_links(response, base_url, job_id)


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
    resolved_tests_repo_token = (
        resolve_tests_repo_token(body, merged) if resolved_tests_repo else ""
    )
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
    # Apply shared BaseAnalysisRequest fields (Jira, GitHub, AI settings).
    # _apply_base_analysis_overrides is the single source of truth for these.
    _apply_base_analysis_overrides(params, body, merged)
    # Jenkins-specific fields on top.
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
            "jenkins_artifacts_max_size_mb": merged.jenkins_artifacts_max_size_mb,
            "get_job_artifacts": merged.get_job_artifacts,
            "raw_prompt": body.raw_prompt or "",
            "issue_prompt": body.issue_prompt or "",
            "force": merged.force_analysis,
            "wait_started_at": _time.time(),
            "name": getattr(body, "name", None) or "",
        }
    )
    return encrypt_sensitive_fields(params)


async def _enqueue_analysis_job(
    body: AnalyzeRequest,
    merged: Settings,
    resolved_peers: list | None,
    base_url: str,
    *,
    message_prefix: str = "Analysis",
    username: str = "",
    reanalyzed_from_job_id: str = "",
    reanalyzed_from_job_name: str = "",
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
    display_name = _get_display_name(body)
    initial_result: dict = {
        "job_name": body.job_name,
        "display_name": display_name,
        "build_number": body.build_number,
        "request_params": _build_request_params(
            body,
            merged,
            body.ai_provider or AI_PROVIDER,
            body.ai_model or AI_MODEL,
            peer_ai_configs_resolved=resolved_peers,
        ),
    }
    initial_result["request_params"]["submitted_by"] = username
    initial_result["request_params"]["analysis_type"] = "jenkins"
    _stamp_reanalysis_metadata(
        initial_result["request_params"],
        reanalyzed_from_job_id,
        reanalyzed_from_job_name,
    )
    if body.tags:
        initial_result["tags"] = body.tags
    can_resume_wait = merged.wait_for_completion and bool(merged.jenkins_url)
    await save_result(
        job_id,
        jenkins_url,
        "waiting" if can_resume_wait else "pending",
        initial_result,
    )
    notify_active_count_changed()
    notify_dashboard_changed()
    task = asyncio.create_task(
        process_analysis_with_id(job_id, body, merged, username=username)
    )
    _register_job_task(job_id, task)
    response: dict = {
        "status": "queued",
        "job_id": job_id,
        "message": f"{message_prefix} job queued. Poll /results/{job_id} for status.",
    }
    return _attach_result_links(response, base_url, job_id)


async def _process_file_raw_analysis(
    *,
    job_id: str,
    body: UnifiedAnalyzeRequest,
    merged: Settings,
    display_name: str,
    ai_provider: str,
    ai_model: str,
    peer_ai_configs: list | None,
    tests_repo_url: str,
    tests_repo_ref: str,
    resolved_tests_repo_token: str,
    additional_repos_list: list,
    base_url: str,
    username: str = "",
) -> None:
    """Background task for file/raw analysis."""
    job_id_var.set(job_id)

    auth_header = ""
    repo_manager: RepositoryManager | None = None

    try:
        logger.info(
            f"Starting file/raw analysis for job_id={job_id}, type={body.type}, display_name={display_name}"
        )

        # Create source plugin
        source: CISource
        if body.type == "file":
            assert body.raw_xml is not None
            source = FileSource(raw_xml=body.raw_xml)
        else:
            assert body.failures is not None
            source = RawSource(failures=body.failures)

        # Fetch failures from source
        source_result = await source.fetch()
        logger.debug(
            f"Source fetch complete: {len(source_result.failures)} failures, build_passed={source_result.build_passed}"
        )

        if source_result.build_passed:
            # No failures found (XML with no failures)
            analysis_result = FailureAnalysisResult(
                job_id=job_id,
                status="completed",
                summary="No test failures found in the provided input.",
                enriched_xml=body.raw_xml if body.type == "file" else None,
            )
            result_data = analysis_result.model_dump(mode="json")
            result_data["job_name"] = display_name
            await _preserve_request_params(job_id, result_data)
            logger.info(f"No failures found for job_id={job_id}, completing early")
            await update_status(job_id, "completed", result_data)
            notify_active_count_changed()
            notify_dashboard_changed()
            notify_job_status_changed(job_id)

            # Auto-assign job metadata from name pattern rules
            await _auto_assign_metadata(display_name, merged.metadata_rules)

            return

        test_failures = source_result.failures

        await update_status(job_id, "running")
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)

        # Pre-flight: verify AI is reachable before spawning parallel tasks
        if not await _preflight_sidecar_check(
            job_id,
            ai_provider,
            ai_model,
            display_name,
            job_name=body.job_name or "",
        ):
            return

        auth_header = await _create_ai_auth_header(username)

        # Group failures by error signature
        groups: dict[str, list] = defaultdict(list)
        for failure in test_failures:
            sig = get_failure_signature(failure)
            groups[sig].append(failure)

        logger.info(
            f"Grouped {len(test_failures)} failures into {len(groups)} unique error signatures (job_id: {job_id})"
        )
        logger.debug(
            f"Failure grouping complete: {len(groups)} unique signatures from {len(test_failures)} failures"
        )

        # Clone repos
        repo_manager = RepositoryManager()
        cloned_repos: dict[str, Path] = {}
        repo_path = repo_manager.create_workspace()
        logger.debug(f"Workspace created at {repo_path}")

        if tests_repo_url:
            logger.debug(
                "Cloning test repo: %s (ref=%s)",
                redact_url(str(tests_repo_url)),
                tests_repo_ref,
            )
            try:
                repo_name = derive_test_repo_name(
                    str(tests_repo_url), additional_repos_list
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
                logger.debug(f"Test repo cloned successfully into {repo_name}/")
            except Exception:
                logger.warning("Failed to clone test repository", exc_info=True)

        if additional_repos_list:
            additional_repos_cloned, repo_path = await clone_additional_repos(
                repo_manager, additional_repos_list, repo_path
            )
            cloned_repos.update(additional_repos_cloned)

        custom_prompt = (body.raw_prompt or "").strip()
        server_url = _build_internal_server_url()

        logger.info(
            f"Starting AI analysis for {len(groups)} failure groups (provider={ai_provider}, model={ai_model})"
        )

        # Analyze each group in parallel
        coroutines: list[Coroutine[Any, Any, Any]] = [
            analyze_failure_group(
                failures=group_failures,
                console_context="",
                repo_path=repo_path,
                ai_provider=ai_provider,
                ai_model=ai_model,
                ai_call_timeout=merged.ai_call_timeout,
                custom_prompt=custom_prompt,
                server_url=server_url,
                job_id=job_id,
                peer_ai_configs=peer_ai_configs,
                peer_analysis_max_rounds=merged.peer_analysis_max_rounds,
                additional_repos=cloned_repos or None,
                max_concurrent_ai_calls=merged.max_concurrent_ai_calls,
                auth_header=auth_header,
            )
            for group_failures in groups.values()
        ]

        results = await run_parallel_with_limit(
            coroutines, max_concurrency=merged.max_concurrent_ai_calls
        )
        logger.debug(
            f"AI analysis complete: {len(results)} results from {len(groups)} groups"
        )

        all_analyses = []
        failed_group_count = 0
        for result in results:
            if isinstance(result, Exception):
                failed_group_count += 1
                logger.error(
                    f"Failed to analyze failure group: {result}", exc_info=result
                )
            else:
                all_analyses.extend(result)

        unique_errors = len(groups)

        # If every group failed, treat the entire job as failed rather than
        # saving a misleading "completed" result with zero findings.
        if not all_analyses and failed_group_count == len(results):
            error_msg = (
                f"All {failed_group_count} failure group(s) failed during analysis "
                f"({len(test_failures)} test failures, {unique_errors} unique errors)"
            )
            logger.error(
                f"File/raw analysis fully failed for job_id={job_id}: {error_msg}"
            )
            fail_result = FailureAnalysisResult(
                job_id=job_id,
                status="failed",
                summary=make_user_friendly_error(error_msg),
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            fail_data = fail_result.model_dump(mode="json")
            fail_data["error"] = fail_result.summary
            fail_data["job_name"] = display_name
            await _preserve_request_params(job_id, fail_data)
            await _attach_token_usage(job_id, fail_data)
            await update_status(job_id, "failed", fail_data)
            notify_active_count_changed()
            notify_dashboard_changed()
            notify_job_status_changed(job_id)
            notify_token_usage_changed()
            return

        summary = (
            f"Analyzed {len(test_failures)} test failures "
            f"({unique_errors} unique errors). "
            f"{len(all_analyses)} analyzed successfully."
        )

        # Enrich with Jira matches
        logger.debug(
            f"Enriching with Jira matches (enable_jira={_resolve_enable_jira(body, merged)})"
        )
        if _resolve_enable_jira(body, merged):
            await enrich_with_jira_matches(
                all_analyses, merged, ai_provider, ai_model, job_id=job_id
            )

        # Enrich with tests repo matches
        logger.debug(
            f"Enriching with tests repo matches (tests_repo_url={redact_url(str(tests_repo_url or ''))})"
        )
        if merged.tests_repo_url or tests_repo_url:
            await _enrich_result_with_tests_repo_matches(
                all_analyses,
                merged,
                ai_provider,
                ai_model,
                job_id=job_id,
                tests_repo_url=tests_repo_url,
            )

        # Build enriched XML if applicable
        enriched_xml = None
        if body.type == "file" and body.raw_xml is not None:
            enriched_xml = build_enriched_xml(
                body.raw_xml, all_analyses, f"{base_url}/results/{job_id}"
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
        result_data["job_name"] = display_name
        logger.info(f"File/raw analysis completed for job_id={job_id}: {summary}")
        await _preserve_request_params(job_id, result_data)

        # Attach token usage summary before persisting
        await _attach_token_usage(job_id, result_data)

        await update_status(job_id, "completed", result_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        notify_token_usage_changed()

        # Populate failure history
        try:
            await populate_failure_history(job_id, result_data)
        except Exception:
            logger.warning(
                "Failed to populate failure_history for job_id=%s",
                job_id,
                exc_info=True,
            )

        # Auto-assign job metadata from name pattern rules
        await _auto_assign_metadata(display_name, merged.metadata_rules)

        # Reveal classifications created during analysis
        await storage.make_classifications_visible(job_id)

    except asyncio.CancelledError:
        logger.info(f"File/raw analysis task cancelled for job_id={job_id}")
        return

    except Exception as e:
        logger.exception(f"File/raw analysis failed for job {job_id}")
        user_error = make_user_friendly_error(e)
        fail_result = FailureAnalysisResult(
            job_id=job_id,
            status="failed",
            summary=user_error,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )
        fail_data = fail_result.model_dump(mode="json")
        fail_data["error"] = fail_result.summary
        fail_data["job_name"] = display_name
        await _preserve_request_params(job_id, fail_data)

        # Attach token usage even on failure
        await _attach_token_usage(job_id, fail_data)

        await update_status(job_id, "failed", fail_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        notify_token_usage_changed()

    finally:
        logger.debug(f"Cleaning up workspace for job_id={job_id}")
        if repo_manager is not None:
            repo_manager.cleanup()
        await _cleanup_ai_session(auth_header)


@app.post("/analyze", status_code=202, response_model=None)
async def analyze(
    request: Request,
    body: UnifiedAnalyzeRequest,
    *,
    settings: Settings = _SETTINGS_DEP,
) -> dict:
    """Submit an analysis job.

    Dispatches to the appropriate CI source plugin based on the ``type`` field.
    All types return 202 with a job_id for async polling.
    """
    _check_allow_list(request)
    base_url = _extract_base_url()

    # Validate AI config early
    _resolve_ai_config(body)

    # Resolve display name
    display_name: str = body.name or ""
    if not display_name:
        if body.type == "jenkins":
            display_name = body.job_name or "jenkins-analysis"
        elif body.type == "file":
            display_name = "file-analysis"
        else:
            display_name = "raw-analysis"

    if body.type == "jenkins":
        # Build a legacy AnalyzeRequest for the existing Jenkins flow
        jenkins_fields: dict = {}
        for field_name in AnalyzeRequest.model_fields:
            if field_name in body.model_fields_set:
                val = getattr(body, field_name, None)
                if val is not None:
                    jenkins_fields[field_name] = val
        jenkins_fields["job_name"] = body.job_name
        jenkins_fields["build_number"] = body.build_number
        if body.name:
            jenkins_fields["name"] = body.name
        jenkins_body = AnalyzeRequest(**jenkins_fields)
        merged = _merge_settings(jenkins_body, settings)
        resolved_peers = _validate_peer_configs(jenkins_body, merged)
        return await _enqueue_analysis_job(
            jenkins_body,
            merged,
            resolved_peers,
            base_url,
            username=request.state.username,
        )

    # File or Raw — enqueue as async background task
    merged = _merge_settings(body, settings)
    resolved_peers = _validate_peer_configs(body, merged)
    return await _enqueue_file_raw_analysis(
        body=body,
        merged=merged,
        resolved_peers=resolved_peers,
        display_name=display_name,
        analysis_type=body.type,
        base_url=base_url,
        username=request.state.username,
        message_prefix="Analysis",
    )


@app.post("/re-analyze/{job_id}", status_code=202, response_model=None)
async def re_analyze(
    job_id: str,
    request: Request,
    body: BaseAnalysisRequest,
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

    # Resolve origin display name now so it can be denormalized into request_params
    origin_job_display_name = (
        result_data.get("display_name") or result_data.get("job_name") or job_id
    )

    # Authorization: only the original submitter or an admin may re-analyze
    if "request_params" not in result_data:
        raise HTTPException(
            status_code=400,
            detail="Original analysis has no stored request_params; cannot re-analyze",
        )

    _check_reanalyze_authorization(request, result_data["request_params"])

    # Detect analysis type from stored params
    params = result_data.get("request_params", {})
    analysis_type = params.get(
        "analysis_type", "jenkins"
    )  # default to jenkins for backward compat

    if analysis_type in ("file", "raw"):
        # File/Raw re-analysis: rebuild a UnifiedAnalyzeRequest and re-submit
        try:
            decrypted_params = decrypt_sensitive_fields(dict(params))
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to decrypt stored params: {exc}",
            ) from exc

        _validate_decrypted_sensitive_fields(decrypted_params)

        # Build unified request from stored params
        # Prefer the original user-supplied name (before UUID suffix was added)
        # over the resolved display_name / job_name.
        unified_fields: dict = {
            "type": analysis_type,
        }
        # Only restore name if user explicitly provided one;
        # leave unset so _enqueue_file_raw_analysis generates a fresh fallback.
        stored_name = decrypted_params.get("original_name", "")
        if stored_name:
            unified_fields["name"] = stored_name
        # Restore source data
        if analysis_type == "file":
            stored_xml = decrypted_params.get("raw_xml")
            if not stored_xml:
                raise HTTPException(
                    status_code=400,
                    detail="Original file analysis has no stored raw_xml; cannot re-analyze",
                )
            unified_fields["raw_xml"] = stored_xml
        else:
            stored_failures = decrypted_params.get("failures")
            if stored_failures is None:
                raise HTTPException(
                    status_code=400,
                    detail="Original raw analysis has no stored failures; cannot re-analyze",
                )
            unified_fields["failures"] = stored_failures

        _copy_analysis_settings(decrypted_params, unified_fields)

        # Apply overrides from request body
        for field_name in body.model_fields_set:
            unified_fields[field_name] = getattr(body, field_name)

        unified_body = UnifiedAnalyzeRequest(**unified_fields)

        # Tags: carry forward + auto-add re-analyze
        existing_tags = list(result_data.get("tags", []))
        if "re-analyze" not in existing_tags:
            existing_tags.append("re-analyze")
        if "tags" in body.model_fields_set:
            for t in getattr(body, "tags", None) or []:
                if t not in existing_tags:
                    existing_tags.append(t)
        unified_body.tags = existing_tags

        # Validate and merge settings
        _resolve_ai_config(unified_body)
        merged = _merge_settings(unified_body, get_settings())
        resolved_peers = _validate_peer_configs(unified_body, merged)

        # Resolve display name
        display_name = unified_body.name or f"{analysis_type}-re-analysis"

        return await _enqueue_file_raw_analysis(
            body=unified_body,
            merged=merged,
            resolved_peers=resolved_peers,
            display_name=display_name,
            analysis_type=analysis_type,
            base_url=base_url,
            username=request.state.username,
            tags=unified_body.tags,
            message_prefix="Re-analysis",
            reanalyzed_from_job_id=job_id,
            reanalyzed_from_job_name=origin_job_display_name,
        )

    # Jenkins path (existing code)
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

    # Carry forward tags from the original result + auto-add re-analyze
    existing_tags = list(result_data.get("tags", []))
    if "re-analyze" not in existing_tags:
        existing_tags.append("re-analyze")
    # Merge with any user-supplied tags from the override body
    if "tags" in body.model_fields_set:
        for t in original_body.tags or []:
            if t not in existing_tags:
                existing_tags.append(t)
    original_body.tags = existing_tags

    # Re-merge settings with overrides applied
    merged = _merge_settings(original_body, original_settings)

    # Validate AI config and peers
    _resolve_ai_config(original_body)
    resolved_peers = _validate_peer_configs(original_body, merged)

    return await _enqueue_analysis_job(
        original_body,
        merged,
        resolved_peers,
        base_url,
        message_prefix="Re-analysis",
        username=request.state.username,
        reanalyzed_from_job_id=job_id,
        reanalyzed_from_job_name=origin_job_display_name,
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
    await _attach_origin_job_info(result)
    settings = get_settings()
    result["capabilities"] = _build_capabilities(settings)
    if result.get("status") in IN_PROGRESS_STATUSES:
        response.status_code = 202
    return result


@app.get("/api/failures/{failure_uuid}")
async def get_failure_by_uuid(failure_uuid: str) -> dict:
    """Look up a single failure analysis by its UUID.

    Searches across all stored jobs for a failure with the given UUID.
    Returns the failure details plus the parent job_id.
    """
    result = await storage.find_failure_by_uuid(failure_uuid)
    if not result:
        raise HTTPException(status_code=404, detail=f"Failure {failure_uuid} not found")
    return result


async def _reanalyze_failure_background(
    job_id: str,
    failure_uuid: str,
    failure_dict: dict,
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None,
    raw_prompt: str,
    peer_ai_configs: list | None,
    peer_analysis_max_rounds: int,
    tests_repo_url: str,
    tests_repo_ref: str,
    tests_repo_token: str,
    additional_repos_list: list,
    username: str,
    max_concurrent_ai_calls: int,
) -> None:
    """Background task: re-analyze a single failure in-place."""
    job_id_var.set(job_id)
    auth_header = ""
    repo_manager: RepositoryManager | None = None

    try:
        auth_header = await _create_ai_auth_header(username)

        # Clone repos if configured
        repo_manager = RepositoryManager()
        cloned_repos: dict[str, Path] = {}
        repo_path = repo_manager.create_workspace()

        if tests_repo_url:
            try:
                repo_name = derive_test_repo_name(
                    str(tests_repo_url), additional_repos_list
                )
                await asyncio.to_thread(
                    repo_manager.clone_into,
                    str(tests_repo_url),
                    repo_path / repo_name,
                    depth=50,
                    branch=tests_repo_ref,
                    token=tests_repo_token or None,
                )
                cloned_repos[repo_name] = repo_path / repo_name
            except Exception:
                logger.warning(
                    "Failed to clone test repository for failure re-analysis",
                    exc_info=True,
                )

        if additional_repos_list:
            additional_repos_cloned, repo_path = await clone_additional_repos(
                repo_manager, additional_repos_list, repo_path
            )
            cloned_repos.update(additional_repos_cloned)

        # Build a FailedTest from the failure dict
        _ft_kwargs: dict = {
            "test_name": failure_dict.get("test_name", ""),
            "error_message": failure_dict.get("error", ""),
        }
        if "stack_trace" in failure_dict:
            _ft_kwargs["stack_trace"] = failure_dict["stack_trace"]
        if "duration" in failure_dict:
            _ft_kwargs["duration"] = failure_dict["duration"]
        if "status" in failure_dict:
            _ft_kwargs["status"] = failure_dict["status"]
        test_failure = FailedTest(**_ft_kwargs)

        server_url = _build_internal_server_url()

        # Analyze the single failure
        analyses = await analyze_failure_group(
            failures=[test_failure],
            console_context="",
            repo_path=repo_path,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=ai_call_timeout,
            custom_prompt=raw_prompt,
            server_url=server_url,
            job_id=job_id,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=peer_analysis_max_rounds,
            additional_repos=cloned_repos or None,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
            auth_header=auth_header,
        )

        if not analyses:
            raise RuntimeError("analyze_failure_group returned no results")

        new_analysis = analyses[0]

        # Patch the failure in the parent job result on success
        def _patch_success(result_data: dict) -> None:
            failure = _find_failure_by_uuid_in_result(result_data, failure_uuid)
            if not failure:
                logger.error(
                    "Failure %s not found in result during success patch", failure_uuid
                )
                return
            # Save previous analysis
            if "analysis" in failure:
                prev_entry = copy.deepcopy(failure)
                # Remove nested previous_analyses to avoid recursion
                prev_entry.pop("previous_analyses", None)
                prev_entry.pop("previous_analysis", None)
                # Strip transient re-analysis fields from archived entry
                prev_entry.pop("reanalysis_status", None)
                prev_entry.pop("reanalyzed_with", None)
                prev_entry.pop("reanalysis_error", None)
                # Tag: this analysis was superseded by a re-analysis using the new provider/model
                prev_entry["_superseded_by"] = {
                    "ai_provider": ai_provider,
                    "ai_model": ai_model,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                if "previous_analyses" not in failure:
                    # Migrate: if old single previous_analysis exists, start the list with it
                    failure["previous_analyses"] = []
                    if "previous_analysis" in failure:
                        failure["previous_analyses"].append(
                            failure.pop("previous_analysis")
                        )
                # Prepend current analysis (most recent previous first)
                failure["previous_analyses"].insert(0, prev_entry)
                # Clean up old field if still present
                failure.pop("previous_analysis", None)
            # Replace with new analysis
            new_data = new_analysis.model_dump(mode="json")
            failure["analysis"] = new_data.get("analysis")
            if new_data.get("peer_debate"):
                failure["peer_debate"] = new_data["peer_debate"]
            else:
                failure.pop("peer_debate", None)
            # Remove running status and any previous error
            failure.pop("reanalysis_status", None)
            failure.pop("reanalysis_error", None)

        await patch_result_json(job_id, _patch_success)
        logger.info(
            "Failure %s re-analysis completed successfully in job %s",
            failure_uuid,
            job_id,
        )

    except Exception as exc:
        error_msg = "Re-analysis failed unexpectedly. Check server logs for details."
        logger.error(
            "Failure %s re-analysis failed in job %s: %s: %s",
            failure_uuid,
            job_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )

        # Patch failure status to "failed" with error message
        def _patch_error(result_data: dict) -> None:
            failure = _find_failure_by_uuid_in_result(result_data, failure_uuid)
            if failure:
                failure["reanalysis_status"] = "failed"
                failure["reanalysis_error"] = error_msg

        try:
            await patch_result_json(job_id, _patch_error)
        except Exception:
            logger.error(
                "Failed to patch error status for failure %s",
                failure_uuid,
                exc_info=True,
            )

    finally:
        notify_job_status_changed(job_id)
        await _cleanup_ai_session(auth_header)
        if repo_manager:
            try:
                repo_manager.cleanup()
            except Exception:
                logger.warning("Failed to cleanup repos", exc_info=True)


@app.post("/api/failures/{failure_uuid}/re-analyze", status_code=202)
async def re_analyze_failure(
    failure_uuid: str,
    request: Request,
) -> dict:
    """Re-analyze a single failure in-place within its parent job.

    Patches the failure directly in the parent job's stored result instead
    of creating a new job.  Accepts an optional JSON body with settings
    overrides; defaults come from the parent job's request_params.
    """
    _check_allow_list(request)

    # Parse optional body
    body_data: dict = {}
    raw_body = await request.body()
    if raw_body:
        try:
            body_data = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid JSON body: {exc}"
            ) from exc
        if not isinstance(body_data, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
    overrides = ReAnalyzeFailureRequest(**body_data)

    # Find the failure across all stored results
    match = await storage.find_failure_by_uuid(failure_uuid)
    if not match:
        raise HTTPException(status_code=404, detail=f"Failure {failure_uuid} not found")

    parent_job_id = match["job_id"]
    failure_dict = match["failure"]

    # Load the parent job to get AI config
    stored = await get_result(parent_job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        raise HTTPException(
            status_code=404, detail=f"Parent job {parent_job_id} not found"
        )

    result_data = stored["result"]
    params = result_data.get("request_params", {})

    if not params:
        raise HTTPException(
            status_code=400,
            detail="Original analysis has no stored request_params; cannot re-analyze",
        )

    # Authorization: only the original submitter or an admin may re-analyze
    _check_reanalyze_authorization(request, params)

    # Decrypt parent settings
    try:
        decrypted_params = decrypt_sensitive_fields(dict(params))
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to decrypt stored params: {exc}",
        ) from exc

    _validate_decrypted_sensitive_fields(decrypted_params)

    # Resolve settings: parent defaults + overrides from request body
    ai_provider = decrypted_params.get("ai_provider", "")
    ai_model = decrypted_params.get("ai_model", "")
    if overrides.ai_provider is not None:
        ai_provider = overrides.ai_provider
    if overrides.ai_model is not None:
        ai_model = overrides.ai_model
    ai_provider, ai_model = _resolve_ai_config_values(ai_provider, ai_model)

    ai_call_timeout = decrypted_params.get("ai_call_timeout")
    if overrides.ai_call_timeout is not None:
        ai_call_timeout = overrides.ai_call_timeout

    raw_prompt = decrypted_params.get("raw_prompt", "")
    if overrides.raw_prompt is not None:
        raw_prompt = overrides.raw_prompt

    peer_ai_configs = decrypted_params.get("peer_ai_configs")
    if overrides.peer_ai_configs is not None:
        peer_ai_configs = overrides.peer_ai_configs

    peer_analysis_max_rounds = decrypted_params.get("peer_analysis_max_rounds", 3)
    if overrides.peer_analysis_max_rounds is not None:
        peer_analysis_max_rounds = overrides.peer_analysis_max_rounds

    tests_repo_url = decrypted_params.get("tests_repo_url", "")
    tests_repo_ref = decrypted_params.get("tests_repo_ref", "")
    tests_repo_token = decrypted_params.get("tests_repo_token", "")
    if overrides.tests_repo_url is not None:
        tests_repo_url, tests_repo_ref = parse_repo_ref(overrides.tests_repo_url)

    additional_repos_list_raw = decrypted_params.get("additional_repos") or []
    additional_repos_list: list[AdditionalRepo] = [
        AdditionalRepo(**r) if isinstance(r, dict) else r
        for r in additional_repos_list_raw
    ]
    if overrides.additional_repos is not None:
        additional_repos_list = [
            AdditionalRepo(**r) if isinstance(r, dict) else r
            for r in overrides.additional_repos
        ]

    max_concurrent_ai_calls = decrypted_params.get("max_concurrent_ai_calls", 3)

    # Immediately patch the failure as "running"
    already_running = False

    def _patch_running(result_data: dict) -> None:
        nonlocal already_running
        failure = _find_failure_by_uuid_in_result(result_data, failure_uuid)
        if failure:
            if failure.get("reanalysis_status") == "running":
                already_running = True
                return
            failure["reanalysis_status"] = "running"
            failure["reanalyzed_with"] = {
                "ai_provider": ai_provider,
                "ai_model": ai_model,
            }

    await patch_result_json(parent_job_id, _patch_running)

    if already_running:
        raise HTTPException(
            status_code=409,
            detail=f"Failure {failure_uuid} is already being re-analyzed",
        )
    notify_job_status_changed(parent_job_id)

    # Start background task
    task = asyncio.create_task(
        _reanalyze_failure_background(
            job_id=parent_job_id,
            failure_uuid=failure_uuid,
            failure_dict=failure_dict,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=ai_call_timeout,
            raw_prompt=raw_prompt,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=peer_analysis_max_rounds,
            tests_repo_url=tests_repo_url,
            tests_repo_ref=tests_repo_ref,
            tests_repo_token=tests_repo_token,
            additional_repos_list=additional_repos_list,
            username=request.state.username,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "status": "accepted",
        "job_id": parent_job_id,
        "failure_uuid": failure_uuid,
    }


def _find_test_in_children(
    children: list[dict],
    test_name: str,
    child_job_name: str,
    child_build_number: int = 0,
) -> bool:
    """Recursively search child job analyses for a test."""
    for child in children:
        if _child_matches(child, child_job_name, child_build_number):
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


def _child_matches(child: dict, job_name: str, build_number: int = 0) -> bool:
    """Return True if *child* matches the given job name and optional build number.

    When ``build_number`` is 0 the match is by name only (wildcard).
    """
    return child.get("job_name") == job_name and (
        build_number == 0 or child.get("build_number") == build_number
    )


def _find_failure_in_children(
    children: list[dict],
    test_name: str,
    child_job_name: str,
    child_build_number: int = 0,
) -> dict | None:
    """Recursively find a failure dict in child job analyses."""
    for child in children:
        if _child_matches(child, child_job_name, child_build_number):
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


def _find_failure_by_uuid_in_child(child: dict, failure_uuid: str) -> dict | None:
    """Recursively search a child job dict for a failure by UUID."""
    for f in child.get("failures", []):
        if f.get("id") == failure_uuid:
            return f
    for nested in child.get("failed_children", []):
        found = _find_failure_by_uuid_in_child(nested, failure_uuid)
        if found:
            return found
    return None


def _find_failure_by_uuid_in_result(
    result_data: dict, failure_uuid: str
) -> dict | None:
    """Find a failure dict by UUID in the result data (top-level + children)."""
    for f in result_data.get("failures", []):
        if f.get("id") == failure_uuid:
            return f
    for child in result_data.get("child_job_analyses", []):
        found = _find_failure_by_uuid_in_child(child, failure_uuid)
        if found:
            return found
    return None


def _find_child_job_in_children(
    children: list[dict],
    child_job_name: str,
    child_build_number: int = 0,
) -> dict | None:
    """Recursively find a child job dict by name and optional build number.

    Uses the same wildcard semantics as ``_find_failure_in_children``:
    ``child_build_number == 0`` matches by name only.
    """
    for child in children:
        if _child_matches(child, child_job_name, child_build_number):
            return child
        found = _find_child_job_in_children(
            child.get("failed_children", []),
            child_job_name,
            child_build_number,
        )
        if found:
            return found
    return None


def _find_child_job_in_result(
    result_data: dict,
    child_job_name: str,
    child_build_number: int = 0,
) -> dict | None:
    """Find a child job dict in the stored result data."""
    return _find_child_job_in_children(
        result_data.get("child_job_analyses", []),
        child_job_name,
        child_build_number,
    )


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

    # Detect @mentions once and reuse for both web-push and SSE notifications
    mentioned = detect_mentions(body.comment)

    settings = get_settings()
    if settings.web_push_enabled and username:
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

    # Notify mentioned users for SSE badge updates
    for mentioned_user in mentioned:
        notify_mentions_changed(mentioned_user)

    notify_comments_changed(job_id)

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

    # Fetch comment text before deletion to notify mentioned users
    comment_text = ""
    try:
        comments = await storage.get_comments_for_job(job_id)
        for c in comments:
            if c.get("id") == comment_id:
                comment_text = c.get("comment", "")
                break
    except Exception:
        logger.debug(
            "Failed to fetch comment text for mention notification", exc_info=True
        )

    deleted = await storage.delete_comment(comment_id, delete_username, job_id=job_id)
    if deleted:
        notify_mentions_changed(request.state.username)
        notify_comments_changed(job_id)
        if comment_text:
            for mentioned_user in detect_mentions(comment_text):
                notify_mentions_changed(mentioned_user)
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
    notify_comments_changed(job_id)
    return {
        "status": "ok",
        "reviewed_by": username if body.reviewed else "",
    }


@app.post("/results/{job_id}/enrich-comments")
async def enrich_comments(
    job_id: str,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Fetch live statuses for GitHub PRs and Jira tickets found in comments."""
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/enrich-comments")

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

    # Token: decrypt stored value, fall back to server token only when
    # the resolved URL matches the server-configured repo to prevent a
    # user-specified repo from borrowing the deployment token.
    token = ""
    if request_params:
        try:
            decrypted = decrypt_sensitive_fields(request_params)
        except Exception:
            logger.warning(
                "Failed to decrypt stored request_params; "
                "falling back to server token only",
                exc_info=True,
            )
            decrypted = {}
        stored_token = decrypted.get("tests_repo_token", "")
        if not _is_encrypted_value(stored_token):
            token = stored_token
    if not token and settings.tests_repo_token:
        server_url_normalized, _ = parse_repo_ref(str(settings.tests_repo_url or ""))
        if url == server_url_normalized:
            token = settings.tests_repo_token.get_secret_value()
        else:
            logger.debug(
                "Skipping server tests_repo_token: resolved URL %s differs from "
                "server-configured %s",
                redact_url(url),
                redact_url(server_url_normalized),
            )

    return url, ref, token


def _resolve_github_repo_url(settings: Settings) -> str:
    """Resolve GitHub repo URL from server config only.

    Only the deployment-level ``TESTS_REPO_URL`` setting is used.
    Stored analysis ``request_params`` are intentionally **not** consulted
    so that callers cannot retarget issue creation or duplicate search away
    from the deployment-configured repository while using server credentials.

    Returns:
        The resolved repo URL, or an empty string when unavailable.
    """
    url, _ref = parse_repo_ref(str(settings.tests_repo_url or ""))
    return url


async def _load_effective_failure(
    job_id: str,
    test_name: str,
    child_job_name: str,
    child_build_number: int,
) -> tuple[FailureAnalysis, dict, dict | None]:
    """Shared lookup for preview/create endpoints: validate, load, and resolve a failure.

    Returns:
        Tuple of (resolved FailureAnalysis, result_data dict, matched child dict or None).

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
    matched_child: dict | None = None
    if child_job_name:
        matched_child = _find_child_job_in_result(
            result_data, child_job_name, child_build_number
        )
    failure = FailureAnalysis.model_validate(failure_dict)
    failure = await _resolve_effective_failure(
        job_id, failure, child_job_name, child_build_number
    )
    return failure, result_data, matched_child


@app.get("/results/{job_id}/issue-prompt")
async def get_issue_prompt(
    job_id: str,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Return the issue generation prompt for a job.

    Resolution order:
    1. Stored ``issue_prompt`` from analysis request params (if provided at submission time)
    2. ``JOB_INSIGHT_ISSUE_PROMPT.md`` fetched from the test repo via GitHub Contents API

    Returns ``{"prompt": ""}`` when no prompt is available.
    """
    _check_allow_list(request)

    stored = await storage.get_result(job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        return {"prompt": ""}

    result_data = stored["result"]

    # Check for stored issue_prompt from analysis request first
    request_params = result_data.get("request_params") or {}
    stored_issue_prompt = (request_params.get("issue_prompt") or "").strip()
    if stored_issue_prompt:
        logger.debug(
            "Using stored issue_prompt (%d chars) for job %s",
            len(stored_issue_prompt),
            job_id,
        )
        return {"prompt": stored_issue_prompt}

    tests_repo_url, tests_repo_ref, tests_repo_token = _resolve_analyzed_repo(
        settings, result_data
    )
    if not tests_repo_url:
        return {"prompt": ""}

    try:
        owner, repo = parse_github_repo_url(tests_repo_url)
    except ValueError:
        logger.warning(
            "Cannot parse GitHub repo URL for issue prompt: %s",
            redact_url(tests_repo_url),
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
    except Exception:  # never crash; empty prompt is safe
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
    settings: Settings = _SETTINGS_DEP,
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
    failure, result_data, matched_child = await _load_effective_failure(
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
        child_job_name=body.child_job_name,
        child_build_number=body.child_build_number,
        matched_child=matched_child,
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
    tests_repo_url = _resolve_github_repo_url(settings)
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
    settings: Settings = _SETTINGS_DEP,
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
    failure, result_data, matched_child = await _load_effective_failure(
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
        child_job_name=body.child_job_name,
        child_build_number=body.child_build_number,
        matched_child=matched_child,
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
            candidates = await search_jira_duplicates(
                title=content["title"],
                settings=effective_jira_settings,
            )
            # AI relevance filtering — only if AI is configured and candidates exist
            request_params = result_data.get("request_params") or {}
            ai_provider = (
                body.ai_provider or request_params.get("ai_provider", "") or AI_PROVIDER
            )
            ai_model = body.ai_model or request_params.get("ai_model", "") or AI_MODEL
            if candidates and ai_provider and ai_model:
                try:
                    matches = await filter_matches_with_ai(
                        bug_title=content["title"],
                        bug_description=content["body"],
                        candidates=candidates,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        job_id=job_id,
                    )
                    # Merge AI score into original candidate data to preserve all fields
                    candidate_by_key = {c["key"]: c for c in candidates}
                    similar = [
                        {**candidate_by_key[m.key], "score": m.score}
                        for m in matches
                        if m.key in candidate_by_key
                    ]
                except Exception:
                    logger.warning(
                        "AI relevance filtering failed for job_id=%s, "
                        "returning unfiltered candidates",
                        job_id,
                        exc_info=True,
                    )
                    similar = candidates
            else:
                similar = candidates
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
    (``resolve_jira_auth`` prefers PAT over API token, so leaving server PAT
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
        for mentioned_user in detect_mentions(comment_text):
            notify_mentions_changed(mentioned_user)
        notify_comments_changed(job_id)
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
    settings: Settings = _SETTINGS_DEP,
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
            detail=(
                "GitHub token is required. Provide a token in your"
                " profile settings or configure GITHUB_TOKEN on the server."
            ),
        )

    _failure, _result_data, _matched_child = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    username = request.state.username
    issue_body = body.body
    if username:
        issue_body += f"\n\n---\n_Reported by: {username} via rootcoz_"

    tests_repo_url = _resolve_github_repo_url(settings)
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
    settings: Settings = _SETTINGS_DEP,
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

    _failure, _result_data, _matched_child = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    username = request.state.username
    bug_body = body.body
    if username:
        bug_body += f"\n\n----\nReported by: {username} via rootcoz"

    try:
        effective_jira_settings = _build_effective_jira_settings(
            settings, body.jira_token, body.jira_email, body.jira_project_key
        )
        if not effective_jira_settings.jira_project_key:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Jira project key is required. Provide it in the"
                    " request or configure JIRA_PROJECT_KEY on the server."
                ),
            )
        if not _has_jira_credentials(effective_jira_settings):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Jira token is required. Provide a token in your"
                    " profile settings or configure Jira credentials"
                    " on the server."
                ),
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
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Push rootcoz classifications into Report Portal test items.

    Finds the matching RP launch, matches failed items to rootcoz failures,
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
    failed items to rootcoz failures, and pushes classifications.

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
        if child_build_number is None or child_build_number == 0:
            raise ValueError(
                "child_build_number is required when child_job_name is provided"
            )
        child = _find_child_job_in_children(
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
    elif child_build_number is not None:
        raise ValueError("child_build_number requires child_job_name to be set")

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
            rcz_failures = [FailureAnalysis.model_validate(f) for f in failures_data]
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Stored result contains invalid failure data: {exc.error_count()} validation error(s)",
            ) from exc

        try:
            matched = await asyncio.to_thread(
                rp_client.match_failures, failed_items, rcz_failures
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

        if not matched and failed_items and rcz_failures:
            rp_names = [item.get("name", "") for item in failed_items]
            rcz_names = [f.test_name for f in rcz_failures]
            # Full diagnostic detail for server logs only
            log_detail = (
                f"No overlap between {len(failed_items)} RP item(s)"
                f" and {len(rcz_failures)} rootcoz failure(s)."
                f" RP items: {', '.join(rp_names)}."
                f" rootcoz tests: {', '.join(rcz_names)}."
            )
            return _log_and_return_rp_error(
                f"No overlap between {len(failed_items)} RP item(s)"
                f" and {len(rcz_failures)} rootcoz failure(s).",
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
            if _child_matches(child, child_job_name, child_build_number):
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
        if _child_matches(child, child_job_name, child_build_number):
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


@app.put("/results/{job_id}/tags")
async def update_tags(
    job_id: str,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Update tags on an existing result. System tags (re-analyze) cannot be removed."""
    _check_allow_list(request)
    body = await _read_json_object(request)
    raw_tags = body.get("tags")
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        raise HTTPException(status_code=400, detail="tags must be a list of strings")

    # Normalize: strip, lowercase, deduplicate, remove blanks
    seen: set[str] = set()
    tags: list[str] = []
    for t in raw_tags:
        normalized = t.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            tags.append(normalized)

    stored = await get_result(job_id)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Result {job_id} not found")

    result_data = stored["result"]
    old_tags = result_data.get("tags", [])

    # Preserve system tags that user tried to remove
    for st in old_tags:
        if st == "re-analyze" and st not in tags:
            tags.append(st)

    await patch_result_json(job_id, lambda d: d.update({"tags": tags}))
    notify_dashboard_changed()
    return {"job_id": job_id, "tags": tags}


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

    # Clean up chat workspaces for all deleted jobs
    from rootcoz.engine.chat import cleanup_chat_workspace

    for job_id in result["deleted"]:
        try:
            cleanup_chat_workspace(job_id)
        except Exception:
            logger.warning(
                "Failed to cleanup chat workspace for %s", job_id, exc_info=True
            )

    notify_active_count_changed()
    notify_dashboard_changed()

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

    # Clean up chat workspace (files, tokens, sessions)
    try:
        from rootcoz.engine.chat import cleanup_chat_workspace

        cleanup_chat_workspace(job_id)
    except Exception:
        logger.warning("Failed to cleanup chat workspace for %s", job_id, exc_info=True)

    notify_active_count_changed()
    notify_dashboard_changed()
    logger.info(f"[AUDIT] Admin '{request.state.username}' deleted job {job_id}")
    return {"status": "deleted", "job_id": job_id}


@app.post("/results/{job_id}/abort")
async def abort_analysis(
    job_id: str,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict:
    """Abort a running or waiting analysis."""
    _check_allow_list(request)

    result = await storage.get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")

    # Enforce ownership: only submitter or admin can abort
    username = request.state.username
    is_admin_user = getattr(request.state, "is_admin", False)
    result_data = result.get("result") or {}
    request_params = result_data.get("request_params") or {}
    submitter = request_params.get("submitted_by", "")

    if not is_admin_user and (not username or username != submitter):
        raise HTTPException(
            status_code=403,
            detail="Only the submitter or an admin can abort this job",
        )

    status = result.get("status", "")
    if status in ("completed", "failed", "aborted"):
        return {"status": status, "message": f"Job already {status}"}

    # Cancel the background task if it exists
    task = _job_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except asyncio.CancelledError:
            pass  # Expected — task was cancelled
        except TimeoutError:
            logger.debug(f"Timed out waiting for task cancellation for job_id={job_id}")
        except Exception:
            logger.warning(
                f"Unexpected error while waiting for task cancellation for job_id={job_id}",
                exc_info=True,
            )
        if task.done():
            _job_tasks.pop(job_id, None)
            logger.info(f"Cancelled background task for job_id={job_id}")
        else:
            logger.warning(
                f"Task for job_id={job_id} still running after cancel timeout, keeping tracked"
            )
    else:
        _job_tasks.pop(job_id, None)
        logger.warning(
            f"No active background task found for job_id={job_id}, updating status only"
        )

    # Update status to aborted
    abort_data: dict = {
        "error": "Analysis was aborted by user",
    }
    # Preserve existing result data
    existing = await storage.get_result(job_id, strip_sensitive=False)
    if existing and existing.get("result"):
        abort_data = {**existing["result"], "error": "Analysis was aborted by user"}

    # Re-check status — task may have completed between cancel and here
    current = await storage.get_result(job_id)
    if current and current.get("status") in ("completed", "failed"):
        logger.info(f"Job {job_id} completed/failed during abort — not overwriting")
        return {
            "status": current["status"],
            "message": f"Job finished as {current['status']} during abort",
        }

    await update_status(job_id, "aborted", abort_data)
    notify_active_count_changed()
    notify_dashboard_changed()
    notify_job_status_changed(job_id)

    logger.info(f"[AUDIT] User '{request.state.username}' aborted job {job_id}")
    return {"status": "aborted", "job_id": job_id}


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


@app.get("/api/navbar/stream")
async def stream_navbar_counts(request: Request) -> StreamingResponse:
    """SSE stream that pushes active analysis count and unread mention count."""
    username = request.state.username
    _check_allow_list(request)

    async def event_generator():
        # Per-connection events
        active_event = asyncio.Event()
        mention_event = asyncio.Event() if username else None

        # Register
        _active_count_listeners.add(active_event)
        if username and mention_event is not None:
            _mention_listeners.setdefault(username, set()).add(mention_event)

        try:
            # Send both counts immediately on connect
            try:
                active = await storage.count_active_analyses()
                last_active = active
                yield f"event: active-count\ndata: {active}\n\n"
            except Exception:
                last_active = 0
                yield "event: active-count\ndata: 0\n\n"

            last_unread = -1
            if username:
                try:
                    unread = await storage.get_unread_mention_count(username)
                    last_unread = unread
                    yield f"event: unread-count\ndata: {unread}\n\n"
                except Exception:
                    last_unread = 0
                    yield "event: unread-count\ndata: 0\n\n"

            active_wait_tasks: list[asyncio.Task] = []
            while True:
                # Wait for either event or timeout
                active_wait_tasks = [asyncio.create_task(active_event.wait())]
                if mention_event is not None:
                    active_wait_tasks.append(asyncio.create_task(mention_event.wait()))

                try:
                    done, pending = await asyncio.wait(
                        active_wait_tasks,
                        timeout=30,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    active_wait_tasks = []
                except asyncio.CancelledError:
                    for task in active_wait_tasks:
                        task.cancel()
                    active_wait_tasks = []
                    break

                if not done:
                    yield ": keepalive\n\n"
                    continue

                if await request.is_disconnected():
                    break

                # Re-fetch and send only if changed
                if active_event.is_set():
                    active_event.clear()
                    try:
                        active = await storage.count_active_analyses()
                        if active != last_active:
                            yield f"event: active-count\ndata: {active}\n\n"
                            last_active = active
                    except Exception:
                        logger.debug(
                            "Failed to fetch active count for SSE", exc_info=True
                        )

                if mention_event is not None and mention_event.is_set():
                    mention_event.clear()
                    try:
                        unread = await storage.get_unread_mention_count(username)
                        if unread != last_unread:
                            yield f"event: unread-count\ndata: {unread}\n\n"
                            last_unread = unread
                    except Exception:
                        logger.debug(
                            "Failed to fetch unread count for SSE", exc_info=True
                        )
        finally:
            # Cancel any pending wait tasks on disconnect
            for task in active_wait_tasks:
                task.cancel()
            # Cleanup: remove per-connection events
            _active_count_listeners.discard(active_event)
            if username and mention_event is not None:
                listeners = _mention_listeners.get(username)
                if listeners is not None:
                    listeners.discard(mention_event)
                    if not listeners:
                        _mention_listeners.pop(username, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/dashboard/stream")
async def stream_dashboard(request: Request) -> StreamingResponse:
    """SSE stream that notifies when the dashboard job list changes."""
    _check_allow_list(request)
    return _make_sse_stream(request, _dashboard_listeners, "dashboard-changed")


@app.get("/api/results/{job_id}/stream")
async def stream_job_status(job_id: str, request: Request) -> StreamingResponse:
    """SSE stream that notifies when a specific job's status changes."""
    _check_allow_list(request)
    return _make_sse_stream(
        request,
        set(),
        "status-changed",
        per_key_listeners=_job_status_listeners,
        listener_key=job_id,
    )


@app.get("/api/results/{job_id}/comments/stream")
async def stream_comments(job_id: str, request: Request) -> StreamingResponse:
    """SSE stream that notifies when comments change for a specific job."""
    _check_allow_list(request)
    return _make_sse_stream(
        request,
        set(),
        "comments-changed",
        per_key_listeners=_comment_listeners,
        listener_key=job_id,
    )


@app.get("/api/admin/token-usage/stream")
async def stream_token_usage(request: Request) -> StreamingResponse:
    """SSE stream that notifies when token usage data changes."""
    _check_allow_list(request)
    return _make_sse_stream(request, _token_usage_listeners, "usage-changed")


@app.get("/api/dashboard")
async def api_dashboard() -> list[dict]:
    """Return dashboard job list as JSON for the React frontend."""
    return await list_results_for_dashboard()


@app.get("/api/capabilities")
async def get_capabilities(settings: Settings = _SETTINGS_DEP) -> dict:
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
    settings: Settings = _SETTINGS_DEP,
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
    settings: Settings = _SETTINGS_DEP,
) -> list[dict]:
    """List available security levels for a Jira project."""
    if not settings.jira_url or not body.project_key:
        return []

    result = _jira_client_from_body(settings, body.jira_token, body.jira_email)
    if result is None:
        return []

    effective_settings, _ = result

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
    settings: Settings = _SETTINGS_DEP,
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
        f"GET /history/failures: search={search!r}, "
        f"job_name={job_name!r}, classification={classification!r}, "
        f"limit={limit}, offset={offset}"
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
    if classify_job_id:
        notify_comments_changed(classify_job_id)
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
            models = await list_models(provider)
            return {"provider": provider, "models": models}

        # No provider specified — return models for all known providers
        all_models: dict[str, list[dict]] = {}
        for p in sorted(VALID_AI_PROVIDERS):
            try:
                models = await list_models(p)
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
    except Exception:
        logger.debug("Failed to compute health status for metrics", exc_info=True)
        health_up = 0

    # Count active analyses
    active_analyses: int | None = None
    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM results WHERE status IN ('pending', 'running', 'waiting')"
            )
            row = await cursor.fetchone()
            active_analyses = row[0] if row else 0
    except Exception:
        logger.debug("Failed to compute active analyses for metrics", exc_info=True)

    return Response(
        content=render_prometheus_metrics(
            health_up=health_up, active_analyses=active_analyses
        ),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# Module-level cache for GitHub release data
_release_cache: dict = {}
_RELEASE_CACHE_TTL = 3600  # 1 hour
_release_cache_lock = asyncio.Lock()


@app.get("/api/releases/latest")
async def get_latest_release() -> JSONResponse:
    """Return the latest GitHub release info (cached for 1 hour).

    Used by the What's New dialog. Falls back gracefully if GitHub is unreachable.
    """
    now = _time.monotonic()
    if (
        _release_cache
        and now - _release_cache.get("fetched_at", 0) < _RELEASE_CACHE_TTL
    ):
        return JSONResponse(content=_release_cache["data"])

    async with _release_cache_lock:
        # Re-check after acquiring lock (another request may have refreshed)
        now = _time.monotonic()
        if (
            _release_cache
            and now - _release_cache.get("fetched_at", 0) < _RELEASE_CACHE_TTL
        ):
            return JSONResponse(content=_release_cache["data"])

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.github.com/repos/myk-org/rootcoz/releases/latest",
                    headers={"Accept": "application/vnd.github.v3+json"},
                )
                resp.raise_for_status()
                release = resp.json()
                data = {
                    "version": release.get("tag_name", "").lstrip("v"),
                    "name": release.get("name", ""),
                    "body": release.get("body", ""),
                    "published_at": release.get("published_at", ""),
                    "html_url": release.get("html_url", ""),
                }
                _release_cache["data"] = data
                _release_cache["fetched_at"] = now
                return JSONResponse(content=data)
        except Exception:
            logger.debug("Failed to fetch latest GitHub release", exc_info=True)
            if _release_cache.get("data"):
                return JSONResponse(content=_release_cache["data"])
            return JSONResponse(
                content={
                    "version": "",
                    "name": "",
                    "body": "",
                    "published_at": "",
                    "html_url": "",
                },
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

    # Check pending/rejected status before creating a session (non-admin only)
    if not is_admin and settings.require_approval:
        user_status = await storage.get_user_status(username)
        blocked = _blocked_user_status_response(user_status)
        if blocked is not None:
            logger.info(f"[AUDIT] Login blocked for {user_status} user '{username}'")
            return blocked

    session_token = await storage.create_session(username, is_admin=is_admin)
    response = JSONResponse(
        content={
            "username": username,
            "role": "admin" if is_admin else "user",
            "is_admin": is_admin,
        }
    )
    response.set_cookie(
        "rootcoz_session",
        session_token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=storage.SESSION_TTL_SECONDS,
    )
    # Clear legacy cookie after migration
    response.delete_cookie(
        "jji_session",
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
    )
    # Also set rootcoz_username cookie for compatibility
    response.set_cookie(
        "rootcoz_username",
        username,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=365 * 24 * 60 * 60,
    )
    response.delete_cookie("jji_username", path="/")
    logger.info(f"[AUDIT] Login success: user='{username}' is_admin={is_admin}")
    return response


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Clear admin session."""
    session_token = _read_cookie(request, "rootcoz_session")
    if session_token:
        await storage.delete_session(session_token)
    settings = get_settings()
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(
        "rootcoz_session",
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
    )
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


@app.post("/api/auth/rotate-key")
async def rotate_own_key_endpoint(request: Request) -> JSONResponse:
    """Rotate the current user's API key. Returns the new key (shown once).

    The old key and all sessions are invalidated immediately.
    """
    _check_allow_list(request)
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    if username == "admin":
        raise HTTPException(
            status_code=400,
            detail="Bootstrap admin cannot rotate key via this endpoint. Use ADMIN_KEY env var.",
        )

    try:
        new_key = await storage.rotate_own_key(username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Create a new session for the user so they stay logged in
    is_admin = request.state.is_admin
    session_token = await storage.create_session(username, is_admin=is_admin)
    settings = get_settings()

    response = JSONResponse(
        content={"username": username, "new_api_key": new_key},
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
    response.set_cookie(
        "rootcoz_session",
        session_token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=storage.SESSION_TTL_SECONDS,
    )

    logger.info(f"[AUDIT] User '{username}' rotated their own API key")
    return response


@app.post("/api/auth/register")
async def register_user(request: Request) -> JSONResponse:
    """Register a new user or generate API key for existing user without one.

    Returns the generated API key (shown once).
    When REQUIRE_APPROVAL is True, new users are created with 'pending' status
    and must be approved by an admin before accessing protected endpoints.
    """
    body = await _read_json_object(request)
    raw_username = body.get("username", "")
    if not isinstance(raw_username, str):
        raise HTTPException(status_code=400, detail="Username must be a string")
    username = raw_username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    settings = get_settings()
    allowed = settings.allowed_users_set
    if allowed and username.lower() not in allowed:
        raise HTTPException(
            status_code=403, detail="Registration is restricted. Contact an admin."
        )

    # Determine user status based on REQUIRE_APPROVAL setting
    user_status = "pending" if settings.require_approval else "active"

    try:
        _, raw_key = await storage.create_user(username, status=user_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Create a session for the user
    session_token = await storage.create_session(username, is_admin=False)

    content: dict = {
        "username": username,
        "api_key": raw_key,
        "role": "user",
        "is_admin": False,
        "status": user_status,
    }
    if user_status == "pending":
        content["message"] = (
            "Your account has been created and is awaiting admin approval. "
            "Save this API key \u2014 you'll need it to log in once approved."
        )
    else:
        content["message"] = "Save this API key \u2014 you won't see it again."

    response = JSONResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
    response.set_cookie(
        "rootcoz_session",
        session_token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=storage.SESSION_TTL_SECONDS,
    )
    # Clear legacy session cookie after migration
    response.delete_cookie(
        "jji_session",
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
    )
    response.set_cookie(
        "rootcoz_username",
        username,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=365 * 24 * 60 * 60,
    )
    response.delete_cookie("jji_username", path="/")

    logger.info(f"[AUDIT] User registered: {username} (status={user_status})")
    return response


@app.get("/api/auth/needs-key")
async def check_needs_key(request: Request) -> JSONResponse:
    """Check if the current user needs to generate an API key.

    Uses session or cookie username to determine if the user needs registration.
    This is a public endpoint — does not require authentication.
    """
    cache_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }

    # Try session first (authenticated identity)
    session_token = _read_cookie(request, "rootcoz_session")
    if session_token:
        session = await storage.get_session(session_token)
        if session:
            return JSONResponse(
                content={"needs_key": False, "username": str(session["username"])},
                headers=cache_headers,
            )

    # Check trusted proxy header (SSO)
    settings = get_settings()
    if settings.trust_proxy_headers:
        proxy_user = request.headers.get("x-forwarded-user", "").strip()
        if proxy_user and proxy_user.lower() != "admin":
            return JSONResponse(
                content={"needs_key": False, "username": proxy_user},
                headers=cache_headers,
            )

    # Fall back to cookie username (for migration — user may need a key)
    cookie_username = _read_cookie(request, "rootcoz_username")
    if cookie_username and cookie_username.lower() != "admin":
        has_key = await storage.user_has_key(cookie_username)
        return JSONResponse(
            content={"needs_key": not has_key, "username": cookie_username},
            headers=cache_headers,
        )

    return JSONResponse(
        content={"needs_key": True, "username": ""},
        headers=cache_headers,
    )


@app.get("/api/auth/pending-status")
async def pending_status(request: Request) -> JSONResponse:
    """Return pending status info for unauthenticated users."""
    settings = get_settings()
    content: dict = {
        "status": "pending",
        "message": "Your account is awaiting admin approval. Please wait for an admin to approve your registration.",
    }
    _maybe_add_custom_approval_msg(content, settings)
    return JSONResponse(content=content)


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


@app.post("/api/admin/users/create")
async def admin_create_user_endpoint(request: Request) -> JSONResponse:
    """Admin creates a new user. Does NOT set session cookies (admin stays logged in)."""
    _require_admin(request)
    body = await _read_json_object(request)

    username = body.get("username", "")
    if not isinstance(username, str):
        raise HTTPException(status_code=400, detail="Username must be a string")
    username = username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    role = body.get("role", "user")
    if role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Role must be 'user' or 'admin'")

    try:
        if role == "admin":
            username, raw_key = await storage.create_admin_user(username)
        else:
            _, raw_key = await storage.create_user(username, status="active")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to create user '%s'", username)
        raise HTTPException(status_code=500, detail="Internal server error") from exc

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' created {role} user '{username}'"
    )
    return JSONResponse(
        content={"username": username, "api_key": raw_key, "role": role},
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/admin/users/{username}")
async def delete_user_endpoint(request: Request, username: str) -> dict:
    """Delete a user. Bootstrap admin (ADMIN_KEY) is always available as fallback."""
    _require_admin(request)
    if username == request.state.username:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    try:
        deleted = await storage.delete_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    logger.info(f"[AUDIT] Admin '{request.state.username}' deleted user '{username}'")
    return {"deleted": username}


@app.put("/api/admin/users/{username}/role")
async def change_user_role_endpoint(request: Request, username: str) -> JSONResponse:
    """Change a user's role (promote to admin or demote to user).

    When promoting to admin, an API key is generated and returned only if
    the user doesn't already have one.
    When demoting to user, the existing API key is preserved.
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


@app.get("/api/admin/users/pending")
async def list_pending_users_endpoint(request: Request) -> dict:
    """List users awaiting approval."""
    _require_admin(request)
    users = await storage.list_pending_users()
    return {"users": users}


@app.post("/api/admin/users/{username}/approve")
async def approve_user(username: str, request: Request) -> dict:
    """Approve a pending user registration."""
    _require_admin(request)
    status = await storage.get_user_status(username)
    if status is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    if status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"User '{username}' is not pending (current status: {status})",
        )
    await storage.set_user_status(username, "active")
    logger.info(f"[AUDIT] Admin '{request.state.username}' approved user '{username}'")
    return {
        "username": username,
        "status": "active",
        "message": f"User '{username}' has been approved.",
    }


@app.post("/api/admin/users/{username}/reject")
async def reject_user(username: str, request: Request) -> dict:
    """Reject a pending user registration."""
    _require_admin(request)
    status = await storage.get_user_status(username)
    if status is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    if status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"User '{username}' is not pending (current status: {status})",
        )
    await storage.set_user_status(username, "rejected")
    logger.info(f"[AUDIT] Admin '{request.state.username}' rejected user '{username}'")
    return {
        "username": username,
        "status": "rejected",
        "message": f"User '{username}' has been rejected.",
    }


@app.post("/api/admin/users/{username}/rotate-key")
async def rotate_key_endpoint(request: Request, username: str) -> JSONResponse:
    """Rotate a user's API key. Works for both admin and regular users."""
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

    # Try admin key rotation first (supports custom keys)
    try:
        new_key = await storage.rotate_admin_key(username, custom_key=custom_key)
    except ValueError as exc:
        detail = str(exc)
        # If the user is not an admin, try rotating as regular user
        if "not found" in detail.lower() and not custom_key:
            try:
                new_key = await storage.rotate_user_key(username)
            except ValueError as user_exc:
                raise HTTPException(status_code=404, detail=str(user_exc)) from user_exc
        else:
            status = 404 if "not found" in detail.lower() else 400
            raise HTTPException(status_code=status, detail=detail) from exc

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' rotated key for '{username}'"
    )
    return JSONResponse(
        content={"username": username, "new_api_key": new_key},
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


# --- SSE for settings changes ---
_settings_listeners: set[asyncio.Event] = set()

# Track env vars injected from DB overrides (vs. actual deployment env vars)
_db_injected_env_vars: set[str] = set()


def _broadcast_settings_change() -> None:
    """Signal all SSE listeners that settings changed."""
    for ev in _settings_listeners:
        ev.set()


@app.get("/api/admin/settings/stream")
async def settings_stream(request: Request):
    """SSE stream for server settings changes."""
    _require_admin(request)
    return _make_sse_stream(request, _settings_listeners, "settings-changed")


@app.get("/api/admin/settings")
async def get_admin_settings(
    request: Request,
    reveal_key: str = Query(
        "", description="Setting key to reveal (or '__all__' for all)"
    ),
) -> JSONResponse:
    """Get all server settings with metadata, current values, and sources."""
    _require_admin(request)
    metadata = _get_settings_metadata()

    # Get DB overrides
    db_overrides = await storage.get_server_settings()

    # Update source and value for DB-overridden settings
    for item in metadata:
        key = item["key"]
        if key in db_overrides:
            override = db_overrides[key]
            item["source"] = "db"
            db_value = override["value"]
            # Decrypt sensitive values stored encrypted in DB
            if item["sensitive"] and db_value:
                try:
                    from rootcoz.encryption import decrypt_value

                    db_value = decrypt_value(db_value)
                except Exception:
                    pass  # Use as-is if decryption fails
            item["value"] = db_value
            item["updated_by"] = override.get("updated_by", "")
            item["updated_at"] = override.get("updated_at", "")
        elif os.environ.get(item["env_var"]) is not None:
            item["source"] = "env"
        else:
            item["source"] = "default"

    # Mask sensitive values — only reveal the specifically requested key
    reveal_all = reveal_key == "__all__"
    for item in metadata:
        if item["sensitive"] and item["value"]:
            if not reveal_all and item["key"] != reveal_key:
                item["value"] = "••••••••"

    return JSONResponse(content=metadata)


@app.put("/api/admin/settings")
async def update_admin_settings(request: Request) -> JSONResponse:
    """Update one or more server settings. Body: {"settings": {"key": "value", ...}}"""
    _require_admin(request)
    body = await request.json()
    settings_updates = body.get("settings", {})

    if not settings_updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    # Validate keys exist in Settings model
    valid_keys = set(Settings.model_fields.keys())
    invalid_keys = set(settings_updates.keys()) - valid_keys
    if invalid_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown settings: {', '.join(sorted(invalid_keys))}",
        )

    # Validate values against Settings field types and constraints
    errors = []
    for key, value in settings_updates.items():
        field_info = Settings.model_fields[key]
        # Skip validation for None/empty — those reset to default
        if value is None or value == "":
            continue
        # Check integer fields
        annotation = field_info.annotation
        is_int = annotation is int or (
            hasattr(annotation, "__args__")
            and int in getattr(annotation, "__args__", ())
        )
        if is_int:
            try:
                int_val = int(value)
                # Check constraints from Field metadata
                metadata = field_info.metadata
                for meta in metadata:
                    if (
                        hasattr(meta, "gt")
                        and meta.gt is not None
                        and int_val <= meta.gt
                    ):
                        errors.append(f"{key}: must be > {meta.gt}")
                    if (
                        hasattr(meta, "ge")
                        and meta.ge is not None
                        and int_val < meta.ge
                    ):
                        errors.append(f"{key}: must be >= {meta.ge}")
                    if (
                        hasattr(meta, "le")
                        and meta.le is not None
                        and int_val > meta.le
                    ):
                        errors.append(f"{key}: must be <= {meta.le}")
            except (ValueError, TypeError):
                errors.append(f"{key}: must be an integer")
    if errors:
        raise HTTPException(
            status_code=400, detail=f"Invalid settings: {'; '.join(errors)}"
        )

    username = request.state.username or "admin"

    # Save each setting to DB and apply to running process
    for key, value in settings_updates.items():
        # Handle null values — treat as empty string (reset-like)
        if value is None:
            str_value = ""
        else:
            str_value = str(value)

        # Store in DB (encrypt sensitive values)
        if key in _SENSITIVE_SETTINGS and str_value:
            from rootcoz.encryption import encrypt_value

            db_value = encrypt_value(str_value)
        else:
            db_value = str_value
        await storage.set_server_setting(key, db_value, updated_by=username)

        # Apply plain text value to env vars for runtime effect
        env_key = key.upper()
        if str_value == "" or value is None:
            os.environ.pop(env_key, None)
            _db_injected_env_vars.discard(env_key)
        else:
            os.environ[env_key] = str_value
            _db_injected_env_vars.add(env_key)

    # Clear cached settings so next get_settings() picks up env changes
    get_settings.cache_clear()

    _broadcast_settings_change()

    logger.info(
        "Admin settings updated: keys=%s, by=%s",
        list(settings_updates.keys()),
        username,
    )

    return JSONResponse(
        content={"updated": list(settings_updates.keys())},
        status_code=200,
    )


@app.get("/api/admin/settings/history")
async def get_settings_history(
    request: Request,
    key: str = Query("", description="Filter by setting key"),
    limit: int = Query(100, description="Max entries to return", gt=0, le=1000),
) -> JSONResponse:
    """Get server settings change history."""
    _require_admin(request)
    history = await storage.get_server_settings_history(key=key or None, limit=limit)
    # Mask sensitive values in history
    for entry in history:
        if entry.get("key") in _SENSITIVE_SETTINGS:
            if entry.get("value"):
                entry["value"] = "••••••••"
            if entry.get("previous_value"):
                entry["previous_value"] = "••••••••"
    return JSONResponse(content=history)


@app.delete("/api/admin/settings/{key}")
async def reset_admin_setting(request: Request, key: str) -> JSONResponse:
    """Reset a server setting to its env/default value (removes DB override)."""
    _require_admin(request)
    if key not in Settings.model_fields:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

    deleted = await storage.delete_server_setting(
        key, deleted_by=request.state.username or "admin"
    )
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Setting '{key}' has no DB override"
        )

    # Only remove the env var if it was injected by a DB override (not set by deployment)
    env_key = key.upper()
    if env_key in _db_injected_env_vars:
        os.environ.pop(env_key, None)
        _db_injected_env_vars.discard(env_key)
    get_settings.cache_clear()

    _broadcast_settings_change()

    logger.info("Admin setting reset: key=%s, by=%s", key, request.state.username)

    return JSONResponse(content={"reset": key})


# -- Job Metadata Endpoints ---------------------------------------------------


async def _metadata_filters(
    team: Annotated[list[str] | None, Query()] = None,
    tier: Annotated[list[str] | None, Query()] = None,
    version: Annotated[list[str] | None, Query()] = None,
    label: Annotated[list[str] | None, Query()] = None,
    exclude_label: Annotated[list[str] | None, Query()] = None,
) -> dict:
    """Shared dependency for metadata filter query parameters."""
    return {
        "team": team or [],
        "tier": tier or [],
        "version": version or [],
        "label": label or [],
        "exclude_label": exclude_label or [],
    }


def _unpack_metadata_filters(
    filters: dict, endpoint: str
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Unpack metadata filter dict and log at DEBUG level."""
    team, tier, version, label, exclude_label = (
        filters["team"],
        filters["tier"],
        filters["version"],
        filters["label"],
        filters["exclude_label"],
    )
    logger.debug(
        "%s: team=%r, tier=%r, version=%r, label=%r, exclude_label=%r",
        endpoint,
        team,
        tier,
        version,
        label,
        exclude_label,
    )
    return team, tier, version, label, exclude_label


@app.get("/api/jobs/metadata")
async def list_jobs_metadata(
    filters: Annotated[dict, Depends(_metadata_filters)],
) -> list[dict]:
    """List all job metadata, optionally filtered by team, tier, version, or labels."""
    team, tier, version, label, _exclude_label = _unpack_metadata_filters(
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
    team, tier, version, label, exclude_label = _unpack_metadata_filters(
        filters, "GET /api/dashboard/filtered"
    )
    jobs = await list_results_for_dashboard()

    has_include_filters = bool(team or tier or version or label)

    # Fetch all metadata once (unfiltered) — used for both include/exclude and display
    all_metadata_unfiltered = await storage.list_jobs_with_metadata()
    all_metadata_by_name = {m["job_name"]: m for m in all_metadata_unfiltered}

    if has_include_filters:
        # Build filtered set for include matching
        filtered_metadata = await storage.list_jobs_with_metadata(
            team=team,
            tier=tier,
            version=version,
            labels=label if label else None,
        )
        filtered_names = {m["job_name"] for m in filtered_metadata}
        jobs = [j for j in jobs if j.get("job_name", "") in filtered_names]

    if exclude_label:
        # Exclude jobs matching ANY excluded label (OR semantics)
        exclude_set = set(exclude_label)
        excluded_names: set[str] = set()
        for m in all_metadata_unfiltered:
            job_labels = set(m.get("labels") or [])
            if job_labels & exclude_set:
                excluded_names.add(m["job_name"])
        jobs = [j for j in jobs if j.get("job_name", "") not in excluded_names]

    # Use unfiltered metadata for display attachment
    metadata_by_name = all_metadata_by_name

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
    notify_mentions_changed(username)
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
    notify_mentions_changed(username)
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
    # Fall back to the job's stored AI config as an atomic pair
    # Only use stored config when request doesn't set either field
    if not ai_provider and not ai_model and body.job_id:
        stored = await storage.get_result(body.job_id)
        if stored and stored.get("result"):
            params = stored["result"].get("request_params", {})
            ai_provider = params.get("ai_provider", "")
            ai_model = params.get("ai_model", "")
    ai_provider, ai_model = _resolve_ai_config_values(ai_provider, ai_model)

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

    logger.info(
        "AI call: provider=%s, model=%s, call_type=comment_intent",
        ai_provider,
        ai_model,
    )
    result = await call_ai_once(
        prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_call_timeout=None,
    )

    await result.record_usage(
        request_id="comment-intent",
        call_type="comment_intent",
        prompt_chars=len(prompt),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )

    if not result.success:
        logger.debug("AI call failed for comment intent analysis: %s", result.text)
        return AnalyzeCommentResponse(suggests_reviewed=False)

    parsed = extract_json_dict(result.text)
    if parsed is not None:
        return AnalyzeCommentResponse(
            suggests_reviewed=bool(parsed.get("suggests_reviewed", False)),
            reason=str(parsed.get("reason", "")),
        )

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
    except Exception as exc:  # non-fatal feedback preview
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
    except Exception as exc:  # non-fatal feedback submission
        logger.exception("Failed to create feedback issue")
        raise HTTPException(
            status_code=500,
            detail="Failed to create feedback issue",
        ) from exc


# -- Chat helpers --


async def _resolve_chat_credentials(
    decrypted_params: dict, username: str
) -> tuple[str, str, str, str, str]:
    """Resolve Jira and GitHub credentials for chat.

    Chat uses ONLY user-scoped tokens — never global server credentials.
    If the user hasn't configured their tokens, the tools are unavailable.

    Returns:
        (jira_url, jira_email, jira_token, github_token, github_repo)
    """
    import re

    settings = get_settings()
    user_tokens = await storage.get_user_tokens(username)

    # Extract github repo from tests_repo_url (repo name is not a credential)
    github_repo = ""
    tests_repo_url = decrypted_params.get("tests_repo_url", "")
    if tests_repo_url:
        match = re.search(
            r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", str(tests_repo_url)
        )
        if match:
            github_repo = match.group(1)

    # Jira URL from job params or server settings (URL is not a credential)
    jira_url = decrypted_params.get("jira_url", "") or str(settings.jira_url or "")

    # User-scoped credentials ONLY — no fallback to server settings or job params
    jira_email = user_tokens.get("jira_email", "")
    jira_token = user_tokens.get("jira_token", "")
    github_token = user_tokens.get("github_token", "")

    return jira_url, jira_email, jira_token, github_token, github_repo


# -- Chat endpoints --


@app.get("/api/chat/{job_id}")
async def get_chat_history(
    job_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Get chat message history for an analyzed job."""
    _check_allow_list(request)
    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")

    username = getattr(request.state, "username", "")
    messages = await storage.get_chat_messages(
        job_id, limit=limit, offset=offset, username=username
    )
    # Filter out hidden init messages (empty content + completed status, used for session_id storage)
    # Keep pending messages even if empty (they show "Thinking..." in the UI)
    messages = [
        m
        for m in messages
        if m.get("content") or m.get("status") in ("pending", "failed")
    ]
    total = await storage.count_chat_messages(job_id, username=username)
    return {"messages": messages, "total": total}


@app.post("/api/chat/{job_id}/init")
async def init_chat(job_id: str, request: Request) -> dict:
    """Initialize chat workspace: create directory, clone repos, and start AI session."""
    _check_allow_list(request)
    from rootcoz.engine.chat import (
        ensure_chat_workspace,
        clone_chat_repos,
        build_chat_custom_tools,
        init_chat_session,
    )

    stored = await get_result(job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail="Job not found")

    result_data = stored["result"]
    params = result_data.get("request_params", {})
    username = getattr(request.state, "username", "")

    # Create workspace
    workspace = ensure_chat_workspace(job_id, username=username)

    # Decrypt params for repo cloning
    decrypted_params: dict = {}
    try:
        decrypted_params = decrypt_sensitive_fields(dict(params))
    except Exception:
        logger.warning("Failed to decrypt request_params for chat init", exc_info=True)

    # Resolve AI provider/model
    ai_provider = (
        result_data.get("ai_provider", "")
        or params.get("ai_provider", "")
        or AI_PROVIDER
    )
    ai_model = result_data.get("ai_model", "") or params.get("ai_model", "") or AI_MODEL

    (
        jira_url,
        jira_email,
        jira_token,
        github_token,
        github_repo,
    ) = await _resolve_chat_credentials(decrypted_params, username)

    # Only create sidecar session on first init (avoid wasting sessions on re-init)
    # Use per-user lock to serialize cloning and session creation with _process_chat_message
    session_id: str | None = ""
    lock = _get_chat_lock(f"{job_id}:{username}")
    async with lock:
        # Clone repos inside lock to prevent concurrent clones with _process_chat_message.
        # Tradeoff: this blocks message processing until cloning finishes, but the frontend
        # init has a 10s timeout so user input is never blocked indefinitely. Without the lock,
        # concurrent init + message can clone into the same workspace causing corruption.
        repos_available = await clone_chat_repos(
            workspace, decrypted_params, user_repo_token=github_token
        )

        existing = await storage.get_chat_messages(job_id, limit=1, username=username)
        if not existing:
            # Build HTTP-backed custom tools for the sidecar session.
            # The auth token is short-lived — revoked after session creation since
            # _process_chat_message creates a fresh token on every message.
            custom_tools: list[dict] = []
            auth_header = await _create_ai_auth_header(username)
            if auth_header:
                server_url = _build_internal_server_url()
                custom_tools = build_chat_custom_tools(
                    server_url=server_url,
                    auth_token=auth_header.removeprefix("Bearer ").strip(),
                    job_id=job_id,
                    jira_url=jira_url,
                    jira_email=jira_email,
                    jira_token=jira_token,
                    github_token=github_token,
                    github_repo=github_repo,
                )
            else:
                logger.warning(
                    "Chat init: no auth token for %s — tools unavailable in system prompt",
                    username,
                )

            session_id = await init_chat_session(
                job_id=job_id,
                job_name=result_data.get("job_name", "unknown"),
                build_number=result_data.get("build_number", 0),
                ai_provider=ai_provider,
                ai_model=ai_model,
                repo_path=workspace,
                custom_tools=custom_tools,
                repos_available=repos_available,
            )
            if session_id:
                await storage.add_chat_message(
                    job_id=job_id,
                    role="assistant",
                    content="",  # Hidden init message
                    username=username,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    session_id=session_id,
                    status="completed",
                )

            # Revoke the init auth token — _process_chat_message creates
            # fresh tokens on each message
            await _cleanup_ai_session(auth_header)

    logger.info(
        "Chat init for job %s: workspace=%s, repos=%s, session=%s",
        job_id,
        workspace,
        repos_available,
        session_id,
    )

    # Collect repo names that were cloned
    repo_names = []
    if workspace.exists():
        for item in workspace.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                repo_names.append(item.name)

    return {
        "ready": True,
        "repos_cloned": repos_available,
        "repo_names": repo_names,
        "job_name": result_data.get("job_name", ""),
        "build_number": result_data.get("build_number", 0),
        "session_id": session_id or "",
    }


@app.post("/api/chat/{job_id}/close")
async def close_chat(job_id: str, request: Request) -> dict:
    """Signal that a user left the chat page.

    Does NOT clean up the workspace — other users/tabs may still be active.
    Workspace cleanup only happens on DELETE /api/chat/{job_id} (clear history).
    """
    _check_allow_list(request)
    logger.info("Chat: user left chat page for job %s", job_id)
    return {"status": "ok"}


@app.post("/api/chat/{job_id}/abort")
async def abort_chat(job_id: str, request: Request) -> dict:
    """Abort the currently processing chat message for this user."""
    _check_allow_list(request)
    username = request.state.username
    key = f"{job_id}:{username}"
    signal = _get_chat_abort_signal(key)
    signal.set()

    # Also mark any pending messages as failed
    pending = await storage.get_pending_chat_messages(job_id, username=username)
    for msg in pending:
        await storage.update_chat_message_content(msg["id"], "Aborted by user.")
        await storage.update_chat_message_status(msg["id"], "failed")

    if pending:
        notify_chat_changed(job_id, username=username)

    # Abort the sidecar session to interrupt the AI call
    try:
        # Find session_id: check pending messages first, then last assistant message
        target_session_id = None
        for msg in pending:
            if msg.get("session_id"):
                target_session_id = msg["session_id"]
                break
        if not target_session_id:
            # Fetch the tail of history to find the most recent session_id
            total = await storage.count_chat_messages(job_id, username=username)
            if total > 0:
                tail_offset = max(total - 10, 0)
                recent = await storage.get_chat_messages(
                    job_id, username=username, limit=10, offset=tail_offset
                )
                for msg in reversed(recent):
                    if msg.get("role") == "assistant" and msg.get("session_id"):
                        target_session_id = msg["session_id"]
                        break

        if target_session_id:
            from pi_sidecar_client import get_sidecar_client

            client = get_sidecar_client()
            await client.abort(target_session_id)
            logger.debug("Chat: aborted sidecar session for job %s", job_id)
    except Exception:
        logger.debug("Chat: sidecar abort best-effort failed", exc_info=True)

    logger.info("Chat: user %s aborted chat for job %s", username, job_id)
    return {"aborted": len(pending)}


# Per-job chat processing locks — ensures sequential message processing
_chat_locks: dict[str, asyncio.Lock] = {}

ADMIN_CHAT_JOB_ID = "__admin_chat__"


def _get_chat_lock(job_id: str) -> asyncio.Lock:
    """Get or create a per-job chat processing lock."""
    if job_id not in _chat_locks:
        _chat_locks[job_id] = asyncio.Lock()
    return _chat_locks[job_id]


# Per-job:user abort signals — set to cancel in-progress chat processing
_chat_abort_signals: dict[str, asyncio.Event] = {}


def _get_chat_abort_signal(key: str) -> asyncio.Event:
    """Get or create an abort signal for a job:user chat."""
    if key not in _chat_abort_signals:
        _chat_abort_signals[key] = asyncio.Event()
    return _chat_abort_signals[key]


def _cleanup_chat_state(key: str) -> None:
    """Remove chat lock and abort signal for a key to prevent unbounded dict growth."""
    _chat_abort_signals.pop(key, None)
    # Only remove locks that are NOT currently held
    lock = _chat_locks.get(key)
    if lock and not lock.locked():
        _chat_locks.pop(key, None)


@app.get("/api/chat/{job_id}/stream")
async def chat_stream(job_id: str, request: Request) -> StreamingResponse:
    """SSE stream for real-time chat message updates."""
    _check_allow_list(request)
    username = getattr(request.state, "username", "")
    listener_key = f"{job_id}:{username}" if username else job_id
    return _make_sse_stream(
        request,
        set(),
        "chat-changed",
        per_key_listeners=_chat_listeners,
        listener_key=listener_key,
    )


@app.post("/api/chat/{job_id}", status_code=202)
async def send_chat_message(
    job_id: str,
    body: ChatMessageRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Queue a chat message for AI processing.

    Saves the user message immediately and kicks off background AI processing.
    The response is delivered via SSE on /api/chat/{job_id}/stream.
    """
    _check_allow_list(request)

    stored = await get_result(job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail="Job not found")

    # Save user message as completed immediately
    user_msg_id = await storage.add_chat_message(
        job_id=job_id,
        role="user",
        content=body.message,
        username=request.state.username,
        status="completed",
    )
    logger.info("Chat: queued user message %d for job %s", user_msg_id, job_id)

    notify_chat_changed(job_id, username=request.state.username)

    # Kick off background processing — assistant placeholder created inside after lock
    background_tasks.add_task(
        _process_chat_message,
        job_id=job_id,
        user_msg_id=user_msg_id,
        message=body.message,
        ai_provider_override=body.ai_provider,
        ai_model_override=body.ai_model,
        username=request.state.username,
    )

    return {
        "user_message": {
            "id": user_msg_id,
            "role": "user",
            "content": body.message,
            "username": request.state.username,
            "status": "completed",
        },
    }


async def _process_chat_message(
    *,
    job_id: str,
    user_msg_id: int,
    message: str,
    ai_provider_override: str | None,
    ai_model_override: str | None,
    username: str,
) -> None:
    """Background task: process a single chat message with AI."""
    from rootcoz.engine.chat import (
        chat_with_ai,
        ensure_chat_workspace,
        clone_chat_repos,
        build_chat_custom_tools,
    )

    lock = _get_chat_lock(f"{job_id}:{username}")
    auth_header = ""

    async with lock:
        try:
            # Create pending assistant placeholder now that we're actually processing
            assistant_msg_id = await storage.add_chat_message(
                job_id=job_id,
                role="assistant",
                content="",
                username=username,
                ai_provider=ai_provider_override or "",
                ai_model=ai_model_override or "",
                status="pending",
            )
            notify_chat_changed(job_id, username=username)

            stored = await get_result(job_id, strip_sensitive=False)
            if not stored or not stored.get("result"):
                raise RuntimeError(f"Job {job_id} not found during chat processing")

            result_data = stored["result"]
            params = result_data.get("request_params", {})

            ai_provider = (
                ai_provider_override
                or result_data.get("ai_provider", "")
                or params.get("ai_provider", "")
                or AI_PROVIDER
            )
            ai_model = (
                ai_model_override
                or result_data.get("ai_model", "")
                or params.get("ai_model", "")
                or AI_MODEL
            )

            if not ai_provider:
                raise RuntimeError("No AI provider configured")

            # Get conversation history
            msg_count = await storage.count_chat_messages(job_id, username=username)
            all_history = await storage.get_chat_messages(
                job_id, username=username, limit=max(msg_count, 1)
            )
            # Filter to only completed messages for context
            history = [
                m
                for m in all_history
                if m.get("status") != "pending" and m.get("content")
            ]

            # Find session_id from the last completed assistant message
            # Scan all_history (not filtered history) because the init message
            # has empty content but carries the session_id
            last_session_id = None
            for msg in reversed(all_history):
                if (
                    msg.get("role") == "assistant"
                    and msg.get("session_id")
                    and msg.get("ai_provider") == ai_provider
                    and msg.get("ai_model") == ai_model
                ):
                    last_session_id = msg["session_id"]
                    logger.debug(
                        "Chat: found session %s from history (provider=%s, model=%s)",
                        last_session_id,
                        ai_provider,
                        ai_model,
                    )
                    break

            workspace = ensure_chat_workspace(job_id, username=username)

            decrypted_params = {}
            try:
                decrypted_params = decrypt_sensitive_fields(dict(params))
            except Exception:
                logger.warning(
                    "Failed to decrypt request_params for chat context", exc_info=True
                )
            (
                jira_url,
                jira_email,
                jira_token,
                github_token,
                github_repo,
            ) = await _resolve_chat_credentials(decrypted_params, username)

            repos_available = await clone_chat_repos(
                workspace, decrypted_params, user_repo_token=github_token
            )

            settings = get_settings()

            server_url = _build_internal_server_url()
            auth_header = await _create_ai_auth_header(username)

            # Build HTTP-backed custom tools
            custom_tools: list[dict] = []
            if auth_header:
                custom_tools = build_chat_custom_tools(
                    server_url=server_url,
                    auth_token=auth_header.removeprefix("Bearer ").strip(),
                    job_id=job_id,
                    jira_url=jira_url,
                    jira_email=jira_email,
                    jira_token=jira_token,
                    github_token=github_token,
                    github_repo=github_repo,
                )
            else:
                logger.warning(
                    "Chat: no auth token for %s — tools unavailable", username
                )

            # Check if aborted before starting AI call
            abort_key = f"{job_id}:{username}"
            abort_signal = _get_chat_abort_signal(abort_key)
            if abort_signal.is_set():
                abort_signal.clear()
                await storage.update_chat_message_content(
                    assistant_msg_id, "Aborted by user."
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(job_id, username=username)
                logger.info(
                    "Chat: aborted before AI call for job %s, user %s", job_id, username
                )
                return

            success, response_text, new_session_id = await chat_with_ai(
                job_id=job_id,
                job_name=result_data.get("job_name", "unknown"),
                build_number=result_data.get("build_number", 0),
                message=message,
                history=history,
                ai_provider=ai_provider,
                ai_model=ai_model,
                repo_path=workspace,
                ai_call_timeout=settings.ai_call_timeout,
                session_id=last_session_id,
                custom_tools=custom_tools,
                repos_available=repos_available,
            )

            # Check if aborted during AI call
            if abort_signal.is_set():
                abort_signal.clear()
                await storage.update_chat_message_content(
                    assistant_msg_id, "Aborted by user."
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(job_id, username=username)
                logger.info(
                    "Chat: aborted after AI call for job %s, user %s", job_id, username
                )
                return

            if not success:
                logger.error(
                    "Chat AI call failed for job %s: %s", job_id, response_text
                )
                # Update assistant message with error
                # Show user-friendly error, not raw sidecar messages
                user_error = response_text
                if (
                    "not found" in response_text.lower()
                    or "session" in response_text.lower()
                ):
                    user_error = (
                        "AI session expired. Please try sending your message again."
                    )
                await storage.update_chat_message_content(
                    assistant_msg_id, f"Error: {user_error}"
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(job_id, username=username)
                return

            # Check if message was aborted while AI was processing
            current_status = await storage.get_chat_message_status(assistant_msg_id)
            if current_status == "failed":
                logger.info(
                    "Chat: message %d was aborted during processing, discarding response",
                    assistant_msg_id,
                )
                return

            # Update the pending assistant message with the real response
            await storage.update_chat_message_content(assistant_msg_id, response_text)
            await storage.update_chat_message_status(assistant_msg_id, "completed")
            # Update provider/model/session_id on the assistant message
            await storage.update_chat_message_ai_fields(
                assistant_msg_id,
                ai_provider=ai_provider,
                ai_model=ai_model,
                session_id=new_session_id or "",
            )
            logger.info(
                "Chat: message %d processed for job %s (session=%s)",
                assistant_msg_id,
                job_id,
                new_session_id,
            )
            notify_chat_changed(job_id, username=username)

        except Exception:
            logger.error(
                "Chat processing failed for job %s, msg %d",
                job_id,
                assistant_msg_id,
                exc_info=True,
            )
            try:
                await storage.update_chat_message_content(
                    assistant_msg_id,
                    "An error occurred while processing your message. Please try again.",
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(job_id, username=username)
            except Exception:
                logger.error(
                    "Failed to update error status for chat msg %d",
                    assistant_msg_id,
                    exc_info=True,
                )
        finally:
            _cleanup_chat_state(f"{job_id}:{username}")
            await _cleanup_ai_session(auth_header)


@app.delete("/api/chat/{job_id}")
async def clear_chat_history(job_id: str, request: Request) -> dict:
    """Clear chat messages for the current user on a job."""
    _check_allow_list(request)
    from rootcoz.engine.chat import cleanup_chat_repos

    stored = await get_result(job_id)
    if not stored:
        raise HTTPException(status_code=404, detail="Job not found")

    username = request.state.username

    # Acquire the per-user chat lock to prevent tearing down workspace
    # while a background worker is still processing
    lock = _get_chat_lock(f"{job_id}:{username}")
    async with lock:
        count = await storage.delete_chat_messages(job_id, username=username)
        notify_chat_changed(job_id, username=username)
        try:
            cleanup_chat_repos(job_id, username=username)
        except Exception:
            logger.warning(
                "Failed to cleanup chat repos for %s/%s",
                job_id,
                username,
                exc_info=True,
            )

    _cleanup_chat_state(f"{job_id}:{username}")
    logger.info(
        "Chat: cleared %d messages for job %s, user %s", count, job_id, username
    )
    return {"deleted": count}


# -- Admin DB endpoints (used by admin chat tools) --


@app.get("/api/admin/db/schema")
async def admin_db_schema(request: Request) -> dict:
    """Get database schema — tables, columns, types, row counts. Admin only."""
    _require_admin(request)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = []
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        for row in cursor.fetchall():
            table_name = row["name"]
            cols = conn.execute(f"PRAGMA table_info({table_name})")
            columns = [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "pk": bool(c["pk"]),
                    "notnull": bool(c["notnull"]),
                }
                for c in cols.fetchall()
            ]
            count = conn.execute(
                f"SELECT COUNT(*) as c FROM [{table_name}]"
            ).fetchone()["c"]
            tables.append({"name": table_name, "columns": columns, "row_count": count})
        return {"tables": tables}
    finally:
        conn.close()


@app.post("/api/admin/db/query")
async def admin_db_query(request: Request) -> dict:
    """Execute a read-only SQL query. Admin only."""
    _require_admin(request)

    body = await request.json()
    sql = body.get("sql", "").strip()
    if not sql:
        raise HTTPException(status_code=400, detail="Empty query")

    first_word = sql.split()[0].upper() if sql.split() else ""
    if first_word in (
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "REPLACE",
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Write operations not allowed (got {first_word})",
        )

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        results = [dict(row) for row in rows]
        return {"columns": columns, "rows": results, "count": len(results)}
    except sqlite3.OperationalError as e:
        raise HTTPException(status_code=400, detail=f"SQL Error: {e}")
    finally:
        conn.close()


# -- Admin chat endpoints --


@app.get("/api/admin/chat")
async def get_admin_chat_history(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Get admin chat history."""
    _require_admin(request)
    username = getattr(request.state, "username", "")
    messages = await storage.get_chat_messages(
        ADMIN_CHAT_JOB_ID, limit=limit, offset=offset, username=username
    )
    messages = [
        m
        for m in messages
        if m.get("content") or m.get("status") in ("pending", "failed")
    ]
    total = await storage.count_chat_messages(ADMIN_CHAT_JOB_ID, username=username)
    return {"messages": messages, "total": total}


@app.post("/api/admin/chat/init")
async def init_admin_chat(request: Request) -> dict:
    """Initialize admin chat workspace and AI session."""
    _require_admin(request)
    from rootcoz.engine.chat import (
        ensure_chat_workspace,
        build_admin_custom_tools,
        init_admin_chat_session,
    )

    username = getattr(request.state, "username", "")
    ai_provider = AI_PROVIDER
    ai_model = AI_MODEL

    workspace = ensure_chat_workspace(ADMIN_CHAT_JOB_ID, username=username)

    session_id: str | None = ""
    lock = _get_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}")
    async with lock:
        existing = await storage.get_chat_messages(
            ADMIN_CHAT_JOB_ID, limit=1, username=username
        )
        if not existing:
            custom_tools: list[dict] = []
            auth_header = await _create_ai_auth_header(username)
            if auth_header:
                server_url = _build_internal_server_url()
                custom_tools = build_admin_custom_tools(
                    server_url=server_url,
                    auth_token=auth_header.removeprefix("Bearer ").strip(),
                )
            else:
                logger.warning("Admin chat init: no auth token for %s", username)

            session_id = await init_admin_chat_session(
                ai_provider=ai_provider,
                ai_model=ai_model,
                repo_path=workspace,
                custom_tools=custom_tools,
            )
            if session_id:
                await storage.add_chat_message(
                    job_id=ADMIN_CHAT_JOB_ID,
                    role="assistant",
                    content="",
                    username=username,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    session_id=session_id,
                    status="completed",
                )
            await _cleanup_ai_session(auth_header)

    logger.info("Admin chat init: workspace=%s, session=%s", workspace, session_id)
    return {"ready": True, "session_id": session_id or ""}


@app.post("/api/admin/chat/close")
async def close_admin_chat(request: Request) -> dict:
    """Signal that a user left the admin chat page."""
    _require_admin(request)
    logger.info("Admin chat: user left admin chat page")
    return {"status": "ok"}


@app.post("/api/admin/chat/abort")
async def abort_admin_chat(request: Request) -> dict:
    """Abort the currently processing admin chat message for this user."""
    _require_admin(request)
    username = request.state.username
    key = f"{ADMIN_CHAT_JOB_ID}:{username}"
    signal = _get_chat_abort_signal(key)
    signal.set()

    pending = await storage.get_pending_chat_messages(
        ADMIN_CHAT_JOB_ID, username=username
    )
    for msg in pending:
        await storage.update_chat_message_content(msg["id"], "Aborted by user.")
        await storage.update_chat_message_status(msg["id"], "failed")

    if pending:
        notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)

    try:
        target_session_id = None
        for msg in pending:
            if msg.get("session_id"):
                target_session_id = msg["session_id"]
                break
        if not target_session_id:
            total = await storage.count_chat_messages(
                ADMIN_CHAT_JOB_ID, username=username
            )
            if total > 0:
                tail_offset = max(total - 10, 0)
                recent = await storage.get_chat_messages(
                    ADMIN_CHAT_JOB_ID, username=username, limit=10, offset=tail_offset
                )
                for msg in reversed(recent):
                    if msg.get("role") == "assistant" and msg.get("session_id"):
                        target_session_id = msg["session_id"]
                        break

        if target_session_id:
            from pi_sidecar_client import get_sidecar_client

            client = get_sidecar_client()
            await client.abort(target_session_id)
            logger.debug("Admin chat: aborted sidecar session")
    except Exception:
        logger.debug("Admin chat: sidecar abort best-effort failed", exc_info=True)

    logger.info("Admin chat: user %s aborted admin chat", username)
    return {"aborted": len(pending)}


@app.get("/api/admin/chat/stream")
async def admin_chat_stream(request: Request) -> StreamingResponse:
    """SSE stream for real-time admin chat message updates."""
    _require_admin(request)
    username = getattr(request.state, "username", "")
    listener_key = f"{ADMIN_CHAT_JOB_ID}:{username}" if username else ADMIN_CHAT_JOB_ID
    return _make_sse_stream(
        request,
        set(),
        "chat-changed",
        per_key_listeners=_chat_listeners,
        listener_key=listener_key,
    )


@app.post("/api/admin/chat", status_code=202)
async def send_admin_chat_message(
    body: ChatMessageRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Queue an admin chat message for AI processing."""
    _require_admin(request)

    user_msg_id = await storage.add_chat_message(
        job_id=ADMIN_CHAT_JOB_ID,
        role="user",
        content=body.message,
        username=request.state.username,
        status="completed",
    )
    logger.info("Admin chat: queued user message %d", user_msg_id)

    notify_chat_changed(ADMIN_CHAT_JOB_ID, username=request.state.username)

    background_tasks.add_task(
        _process_admin_chat_message,
        user_msg_id=user_msg_id,
        message=body.message,
        ai_provider_override=body.ai_provider,
        ai_model_override=body.ai_model,
        username=request.state.username,
    )

    return {
        "user_message": {
            "id": user_msg_id,
            "role": "user",
            "content": body.message,
            "username": request.state.username,
            "status": "completed",
        },
    }


async def _process_admin_chat_message(
    *,
    user_msg_id: int,
    message: str,
    ai_provider_override: str | None,
    ai_model_override: str | None,
    username: str,
) -> None:
    """Background task: process a single admin chat message with AI."""
    from rootcoz.engine.chat import (
        admin_chat_with_ai,
        ensure_chat_workspace,
        build_admin_custom_tools,
    )

    lock = _get_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}")
    auth_header = ""

    async with lock:
        try:
            # Create pending assistant placeholder now that we're actually processing
            assistant_msg_id = await storage.add_chat_message(
                job_id=ADMIN_CHAT_JOB_ID,
                role="assistant",
                content="",
                username=username,
                ai_provider=ai_provider_override or "",
                ai_model=ai_model_override or "",
                status="pending",
            )
            notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)

            ai_provider = ai_provider_override or AI_PROVIDER
            ai_model = ai_model_override or AI_MODEL

            if not ai_provider:
                raise RuntimeError("No AI provider configured")

            msg_count = await storage.count_chat_messages(
                ADMIN_CHAT_JOB_ID, username=username
            )
            all_history = await storage.get_chat_messages(
                ADMIN_CHAT_JOB_ID, username=username, limit=max(msg_count, 1)
            )
            history = [
                m
                for m in all_history
                if m.get("status") != "pending" and m.get("content")
            ]

            last_session_id = None
            for msg in reversed(all_history):
                if (
                    msg.get("role") == "assistant"
                    and msg.get("session_id")
                    and msg.get("ai_provider") == ai_provider
                    and msg.get("ai_model") == ai_model
                ):
                    last_session_id = msg["session_id"]
                    logger.debug(
                        "Admin chat: found session %s from history (provider=%s, model=%s)",
                        last_session_id,
                        ai_provider,
                        ai_model,
                    )
                    break

            workspace = ensure_chat_workspace(ADMIN_CHAT_JOB_ID, username=username)

            settings = get_settings()

            server_url = _build_internal_server_url()
            auth_header = await _create_ai_auth_header(username)

            custom_tools = build_admin_custom_tools(
                server_url=server_url,
                auth_token=auth_header.removeprefix("Bearer ").strip()
                if auth_header
                else "",
            )

            abort_key = f"{ADMIN_CHAT_JOB_ID}:{username}"
            abort_signal = _get_chat_abort_signal(abort_key)
            if abort_signal.is_set():
                abort_signal.clear()
                await storage.update_chat_message_content(
                    assistant_msg_id, "Aborted by user."
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
                logger.info("Admin chat: aborted before AI call, user %s", username)
                return

            success, response_text, new_session_id = await admin_chat_with_ai(
                message=message,
                history=history,
                ai_provider=ai_provider,
                ai_model=ai_model,
                repo_path=workspace,
                ai_call_timeout=settings.ai_call_timeout,
                session_id=last_session_id,
                custom_tools=custom_tools,
            )

            if abort_signal.is_set():
                abort_signal.clear()
                await storage.update_chat_message_content(
                    assistant_msg_id, "Aborted by user."
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
                logger.info("Admin chat: aborted after AI call, user %s", username)
                return

            if not success:
                logger.error("Admin chat AI call failed: %s", response_text)
                user_error = response_text
                if (
                    "not found" in response_text.lower()
                    or "session" in response_text.lower()
                ):
                    user_error = (
                        "AI session expired. Please try sending your message again."
                    )
                await storage.update_chat_message_content(
                    assistant_msg_id, f"Error: {user_error}"
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
                return

            # Check if message was aborted while AI was processing
            current_status = await storage.get_chat_message_status(assistant_msg_id)
            if current_status == "failed":
                logger.info(
                    "Chat: message %d was aborted during processing, discarding response",
                    assistant_msg_id,
                )
                return

            await storage.update_chat_message_content(assistant_msg_id, response_text)
            await storage.update_chat_message_status(assistant_msg_id, "completed")
            await storage.update_chat_message_ai_fields(
                assistant_msg_id,
                ai_provider=ai_provider,
                ai_model=ai_model,
                session_id=new_session_id or "",
            )
            logger.info(
                "Admin chat: message %d processed (session=%s)",
                assistant_msg_id,
                new_session_id,
            )
            notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)

        except Exception:
            logger.error(
                "Admin chat processing failed for msg %d",
                assistant_msg_id,
                exc_info=True,
            )
            try:
                await storage.update_chat_message_content(
                    assistant_msg_id,
                    "An error occurred while processing your message. Please try again.",
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
            except Exception:
                logger.error(
                    "Failed to update error status for admin chat msg %d",
                    assistant_msg_id,
                    exc_info=True,
                )
        finally:
            _cleanup_chat_state(f"{ADMIN_CHAT_JOB_ID}:{username}")
            await _cleanup_ai_session(auth_header)


@app.delete("/api/admin/chat")
async def clear_admin_chat_history(request: Request) -> dict:
    """Clear admin chat messages for the current user."""
    _require_admin(request)
    from rootcoz.engine.chat import cleanup_chat_repos

    username = request.state.username

    lock = _get_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}")
    async with lock:
        count = await storage.delete_chat_messages(ADMIN_CHAT_JOB_ID, username=username)
        notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
        try:
            cleanup_chat_repos(ADMIN_CHAT_JOB_ID, username=username)
        except Exception:
            logger.warning(
                "Failed to cleanup admin chat repos for %s",
                username,
                exc_info=True,
            )

    _cleanup_chat_state(f"{ADMIN_CHAT_JOB_ID}:{username}")
    logger.info("Admin chat: cleared %d messages for user %s", count, username)
    return {"deleted": count}


# SPA catch-all routes — must be AFTER all API routes
@app.get("/login", include_in_schema=False)
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
    reload = os.getenv("DEBUG", "").lower() == "true"
    uvicorn.run("rootcoz.main:app", host="0.0.0.0", port=APP_PORT, reload=reload)
