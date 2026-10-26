import asyncio
import contextlib
import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import logging.handlers
import math
import os
import re
import sqlite3
import threading
import time as _time
import uuid
from collections import Counter, defaultdict
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import quote as _urlquote
from urllib.parse import urlparse

import aiosqlite
import httpx
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as ApiPath
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
from git.exc import GitCommandError
from pi_sidecar_client import (
    check_sidecar_available,
    run_parallel_with_limit,
)
from pydantic import BaseModel, Field, SecretStr, ValidationError
from simple_logger.logger import get_logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from rootcoz import storage
from rootcoz import transcripts as sidecar_transcripts
from rootcoz.ai_client import (
    _get_model_catalog,
    _setup_usage_recorder,
    ai_username,
    call_ai_once,
    clear_cursor_auth_cache,
    force_server_credentials,
    format_chat_ai_user_error,
    is_cursor_provider,
    list_models,
    model_listing_status,
    normalize_provider,
    probe_cursor_auth,
    registered_provider_ids,
    resolve_catalog_pair,
    scoped_models,
    supported_key_providers,
    update_model_catalog,
)
from rootcoz.attribution import AiProvenance
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
    atlassian_cloud_id_error,
    get_settings,
    jira_method_missing_fields,
    normalize_jira_auth_mode,
    parse_additional_repos,
    parse_peer_configs,
    parse_repo_ref,
    remove_from_db_settings_cache,
    update_db_settings_cache,
)
from rootcoz.encryption import (
    SENSITIVE_KEYS,
    decrypt_sensitive_fields,
    decrypt_value_or_none,
    encrypt_sensitive_fields,
    get_hmac_secret,
    strip_sensitive_from_response,
)
from rootcoz.engine.core import (
    ROOTCOZ_ISSUE_PROMPT_FILENAME,
    _failed_group_analyses,
    analyze_failure_group,
    clone_additional_repos,
    copy_rootcoz_pi_resources,
    extract_json_dict,
    get_failure_signature,
    resolve_additional_repos,
    run_orchestrated_analysis,
    safe_update_clone_progress,
    safe_update_progress,
    set_progress_callback,
)
from rootcoz.error_messages import make_user_friendly_error
from rootcoz.exporters.base import ExportContext, Exporter
from rootcoz.exporters.datadog import (
    _CREDENTIAL_APPLICATION_KEY,
    APPLICATION_KEY_ERROR,
    DatadogExporter,
    _auth_headers,
    _normalize_site,
    classify_datadog_credential,
)
from rootcoz.exporters.reportportal import (
    INVALID_STORED_FAILURES,
    ReportPortalClient,
    format_rp_error,
)
from rootcoz.feedback import (
    create_feedback_from_preview,
    generate_feedback_preview,
)
from rootcoz.github_issues import enrich_with_tests_repo_matches
from rootcoz.jenkins import JenkinsClient
from rootcoz.jira import (
    JiraClient,
    JiraConnection,
    JiraOAuthMintError,
    classify_jira_secret,
    complete_jira_connection,
    enrich_with_jira_matches,
    filter_matches_with_ai,
    jira_method_url,
    resolve_jira_connection,
    resolve_jira_connection_from_params,
)
from rootcoz.logging_context import JobIdFilter, get_log_file, job_id_var
from rootcoz.metadata_rules import match_job_metadata
from rootcoz.models import (
    _SYSTEM_TAGS,
    AddCommentRequest,
    AdditionalRepo,
    AdminCreateUserRequest,
    AnalyzeCommentRequest,
    AnalyzeCommentResponse,
    BaseAnalysisRequest,
    BulkDeleteRequest,
    BulkJobMetadataRequest,
    ChatInitRequest,
    ChatMessageRequest,
    ChildJobAnalysis,
    ClassifyTestRequest,
    CreateFilterPresetRequest,
    CreateIssueRequest,
    CrossFailurePattern,
    ExporterInfo,
    FailedTest,
    FailureAnalysis,
    FailureAnalysisResult,
    FeedbackCreateRequest,
    FeedbackPreviewResponse,
    FeedbackRequest,
    FeedbackResponse,
    FilterPresetResponse,
    JobMetadataInput,
    OverrideClassificationRequest,
    OverridePatternRequest,
    PreviewIssueRequest,
    ProductBugReport,
    PushSubscriptionRequest,
    ReAnalyzeFailureRequest,
    ReAnalyzeRequest,
    ReportPortalPushResult,
    ServerProviderKeyInput,
    SetCanUseServerProvidersRequest,
    SetCanViewReportsRequest,
    SetFreeAiModelsRequest,
    SetReviewedRequest,
    SetServerProviderModelsRequest,
    SetTrackedInRequest,
    UnifiedAnalyzeRequest,
    UnsubscribeRequest,
    UpdateFilterPresetRequest,
    _JenkinsParamsMixin,
)
from rootcoz.monitoring import (
    _get_app_version,
    build_health_response,
    dispatch_alert,
    error_tracker,
    get_component_versions,
    render_prometheus_metrics,
    validate_startup_config,
)
from rootcoz.notifications import send_mention_notifications
from rootcoz.repository import (
    RepositoryManager,
    derive_test_repo_name,
    redact_url,
)
from rootcoz.request_resolution import resolve_tests_repo_token
from rootcoz.result_fields import (
    RESULT_FIELD_PATHS,
    filter_result_fields,
    parse_fields_param,
)
from rootcoz.rootcoz_repo_settings import (
    EffectiveRepoAnalysisSettings,
    RootcozSettingsError,
    assert_no_tests_repo_name_collision,
    resolve_repo_analysis_settings,
    resolve_tests_repo_url,
    tests_repo_available,
)
from rootcoz.sources import (
    CI_SOURCE_REGISTRY,
    CISource,
    append_repo_context,
    apply_source_workspace_files,
    create_source_from_request,
    link_refetched_artifacts,
    run_console_only_analysis,
    setup_analysis_workspace,
)
from rootcoz.sources import chat_workspace as source_chat_workspace
from rootcoz.sources.base import (
    INGEST_COMPLETE_SUMMARY,
    CISourceResult,
    resolve_display_build_id,
)
from rootcoz.storage import (
    ACTIVE_STATUSES,
    AI_SYSTEM_USERNAME,
    ANALYSIS_STATE_ANALYZED,
    ANALYSIS_STATE_SUBMITTED,
    DB_PATH,
    claim_submitted_job_for_analyze,
    copy_stable_analysis_ids,
    get_effective_classification,
    get_history_classification,
    get_result,
    init_db,
    list_distinct_job_names,
    list_results,
    list_results_for_dashboard,
    list_results_for_dashboard_filtered,
    patch_result_json,
    populate_failure_history,
    release_submitted_job_analyze_claim,
    replace_job_test_entries,
    save_result,
    stamp_build_url,
    update_status,
)
from rootcoz.token_tracking import (
    build_token_usage_summary,
    child_usage_scope,
    reanalysis_usage_scope,
    set_usage_callback,
)
from rootcoz.transcripts import (
    transcript_path,
    transcript_summary,
    write_sidecar_env_bridge,
)
from rootcoz.url_utils import (
    mask_url_credentials_in_text,
    redact_urls_in_text,
    strip_url_credentials,
    strip_url_userinfo,
)
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
        "jira_oauth_client_secret",
        "jira_atst_token",
        "jira_email",
        "github_token",
        "tests_repo_token",
        "reportportal_api_token",
        "datadog_api_key",
        "admin_key",
        "vapid_private_key",
        "smtp_password",
        "slack_webhook_url",
    }
)

# Server-only settings that should not be exposed to API clients.
# Everything else is admin-settable via Server Settings (#347); these are
# process/deployment bootstrap with no Settings field at all, plus
# ADMIN_KEY — a Settings field, but a server-only bootstrap secret that
# must never become settable or persistable at runtime (AGENTS.md).
_SERVER_ONLY_SETTINGS: frozenset[str] = frozenset(
    {
        "db_path",
        "debug",
        "admin_key",
    }
)

# Settings whose value is derived from another setting at boot: the
# entrypoint/startup derives SIDECAR_URL from SIDECAR_PORT and exports it
# into the process env, so it shows as env-sourced — but persisting it
# would pin an explicit URL that silently beats the port derivation, and a
# later sidecar_port change would then break AI calls. Persist the source
# setting instead.
_DERIVED_SETTINGS: frozenset[str] = frozenset({"sidecar_url"})

# Admin-management settings hidden from the any-role
# /api/default-server-settings defaults payload: they configure the server,
# not per-request analysis defaults, and several (allowed_users,
# admin_wait_approve_msg) reveal information non-admins never saw before
# #347 unblocked them for admin management.
_DEFAULTS_HIDDEN_SETTINGS: frozenset[str] = frozenset(
    {
        "default_user_role",
        "secure_cookies",
        "trust_proxy_headers",
        "require_approval",
        "admin_wait_approve_msg",
        "allowed_users",
        "log_level",
        "metadata_rules_file",
        "enable_github_issues",
        "enable_reportportal",
        "enable_datadog",
        "enable_auto_review",
        "auto_push_exporters",
        "public_base_url",
        "rp_push_classifications",
        "rp_push_rootcoz_url",
        "rp_push_tracker_links",
        "rp_allow_unreviewed_push",
        "dd_push_classifications",
        "dd_push_rootcoz_url",
        "dd_push_tracker_links",
        "dd_allow_unreviewed_push",
        "vapid_claim_email",
        "vapid_public_key",
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "slack_webhook_url",
        "alert_email_to",
        "session_ttl_hours",
        "sidecar_host",
        "sidecar_port",
        "sidecar_url",
        "acpx_agents",
        "cli_agents",
        "pi_sidecar_log_level",
        "node_binary",
        "pi_sidecar_transcripts_dir",
    }
)

# Fields that require server restart to take effect
_RESTART_REQUIRED_SETTINGS: frozenset[str] = frozenset(
    {
        "default_user_role",
        "secure_cookies",
        "trust_proxy_headers",
        "metadata_rules_file",
        "log_level",
        "vapid_public_key",
        "vapid_private_key",
        "vapid_claim_email",
        "sidecar_host",
        "sidecar_port",
        "sidecar_url",
        "acpx_agents",
        "cli_agents",
        "pi_sidecar_log_level",
        "node_binary",
        "pi_sidecar_transcripts_dir",
    }
)

# Updating any of these invalidates the recorded jira_verified_method —
# the record reflects the last successful test of the *previous* setup.
_JIRA_VERIFIED_INVALIDATING_KEYS: frozenset[str] = frozenset(
    {
        "jira_auth_mode",
        "jira_email",
        "jira_api_token",
        "jira_pat",
        "jira_oauth_client_secret",
        "jira_atst_token",
        "atlassian_oauth_client_id",
        "atlassian_oauth_cloud_id",
        "jira_cloud_url",
        "jira_server_url",
        "jira_oauth_ssl_verify",
        "jira_cloud_ssl_verify",
        "jira_server_ssl_verify",
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
        "ai_provider",
        "ai_model",
        "force_server_credentials",
        "ai_call_timeout",
        "max_concurrent_ai_calls",
        "peer_ai_configs",
        "peer_analysis_max_rounds",
        "enable_free_models",
        "ambient_auth_providers",
        "active_server_providers",
        "record_transcripts",
    ],
    "Jira": [
        "jira_auth_mode",
        "jira_email",
        "jira_api_token",
        "jira_cloud_url",
        "jira_cloud_ssl_verify",
        "jira_pat",
        "jira_server_url",
        "jira_server_ssl_verify",
        "jira_oauth_client_secret",
        "atlassian_oauth_cloud_id",
        "atlassian_oauth_client_id",
        "jira_atst_token",
        "jira_oauth_ssl_verify",
        "jira_site_url",
        "jira_project_key",
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
        "rp_push_classifications",
        "rp_push_rootcoz_url",
        "rp_push_tracker_links",
        "rp_allow_unreviewed_push",
    ],
    "Datadog": [
        "datadog_api_key",
        "datadog_site",
        "datadog_service",
        "datadog_env",
        "enable_datadog",
        "dd_push_classifications",
        "dd_push_rootcoz_url",
        "dd_push_tracker_links",
        "dd_allow_unreviewed_push",
    ],
    "Auth & Security": [
        "admin_key",
        "secure_cookies",
        "trust_proxy_headers",
        "require_approval",
        "admin_wait_approve_msg",
        "allowed_users",
        "default_user_role",
        "session_ttl_hours",
    ],
    "Prow": [
        "prow_url",
        "gcs_bucket",
        "prow_artifacts_max_size_mb",
        "prow_artifact_max_file_size_mb",
        "prow_artifacts_max_objects",
    ],
    "Server": [
        "public_base_url",
        "additional_repos",
        "max_concurrent_repo_clones",
        "wait_for_completion",
        "poll_interval_minutes",
        "max_wait_minutes",
        "metadata_rules_file",
        "log_level",
        "enable_auto_review",  # runtime toggle, no restart required
        "auto_push_exporters",  # runtime toggle, no restart required
    ],
    "Web Push": [
        "vapid_public_key",
        "vapid_private_key",
        "vapid_claim_email",
    ],
    "Notifications": [
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "alert_email_to",
        "slack_webhook_url",
    ],
    # Restart-required: the sidecar starts once at container boot and reads
    # these from the /data/pi-sidecar/env bridge the entrypoint sources.
    "Sidecar": [
        "sidecar_host",
        "sidecar_port",
        "sidecar_url",
        "acpx_agents",
        "cli_agents",
        "pi_sidecar_log_level",
        "node_binary",
        "pi_sidecar_transcripts_dir",
    ],
}

# Reverse lookup: field -> category
_FIELD_TO_CATEGORY: dict[str, str] = {}
for _cat, _fields in _SETTINGS_CATEGORIES.items():
    for _f in _fields:
        _FIELD_TO_CATEGORY[_f] = _cat


def _get_settings_metadata() -> list[dict[str, Any]]:
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
                # jira_auth_mode is DB-only (Server Settings) — no env var
                "env_var": "" if field_name == "jira_auth_mode" else field_name.upper(),
                "value": display_value,
                "default": default_str,
                "description": description,
                "type": field_type,
                "category": _FIELD_TO_CATEGORY.get(field_name, "Other"),
                "sensitive": is_sensitive,
                "restart_required": field_name in _RESTART_REQUIRED_SETTINGS,
                "source": "default",  # Will be overridden by caller
                # Persistability contract (#347): server-only settings are
                # env bootstrap the persist/reset/PUT paths always reject,
                # and derived settings must not be pinned (see
                # _DERIVED_SETTINGS) — the UI hides "Save to DB" for both.
                "server_only": field_name in _SERVER_ONLY_SETTINGS,
                "derived": field_name in _DERIVED_SETTINGS,
                "persistable": (
                    field_name not in _SERVER_ONLY_SETTINGS
                    and field_name not in _DERIVED_SETTINGS
                ),
            }
        )

    return result


# Inline favicon


# --- SSE broadcast for navbar badges ---
_active_count_listeners: set[asyncio.Event] = set()
_mention_listeners: dict[str, set[asyncio.Event]] = {}
_pending_count_listeners: set[asyncio.Event] = set()
# Seconds an SSE stream waits before emitting a keepalive.
_SSE_KEEPALIVE_SECONDS = 30
# Seconds before a failed pending-count lookup is role-checked again.  The
# navbar streams wait for whichever comes first: this retry or the keepalive,
# so unrelated events cannot postpone the retry.
_PENDING_RETRY_SECONDS = 10


async def _periodic_session_cleanup() -> None:
    """Periodically clean up expired sessions."""
    while True:
        await asyncio.sleep(3600)  # Every hour
        try:
            count = await storage.cleanup_expired_sessions()
            if count:
                logger.info("Periodic cleanup: removed %d expired sessions", count)
            await _cleanup_revoked_ai_sessions()
        except Exception:
            logger.debug("Periodic session cleanup failed", exc_info=True)


def notify_active_count_changed() -> None:
    """Signal all SSE listeners that the active analysis count has changed."""
    for event in _active_count_listeners:
        event.set()


def notify_pending_count_changed() -> None:
    """Signal all navbar SSE listeners that the pending user count has changed."""
    for event in _pending_count_listeners:
        event.set()


async def _pending_user_count() -> int | None:
    """Pending approval count for navbar SSE; ``None`` when storage cannot answer."""
    try:
        return len(await storage.list_pending_users())
    except aiosqlite.Error, OSError, TypeError, ValueError:
        logger.debug("Failed to fetch pending user count for navbar SSE", exc_info=True)
        return None


class _PendingFeed:
    """Per-connection pending-count state for a navbar SSE stream.

    Owns the admin re-check, the recount, the dedup bookkeeping and the retry
    deadline so the navbar and multiplexed streams share one implementation.
    """

    def __init__(self, event: asyncio.Event | None, username: str, name: str):
        self.event = event
        self.username = username
        self.name = name
        # ``None`` = no count delivered to this stream yet.
        self.last: int | None = None
        # ``None`` = nothing to retry; otherwise the monotonic retry deadline.
        self.retry_at: float | None = None

    async def step(self) -> str | None:
        """Re-verify the admin role and recount; return the chunk to emit."""
        if self.event is None:
            return None
        try:
            still_admin = await _ai_admin_role_current(self.username)
        except aiosqlite.Error, OSError, TypeError, ValueError:
            # Unverifiable is not revoked: send nothing, but stay registered and
            # retry, so a transient database error does not freeze the badge.
            logger.debug("Failed to re-check admin role for navbar SSE", exc_info=True)
            self._defer()
            return None
        if not still_admin:
            # Confirmed revocation: unregister for good and clear the badge.
            _pending_count_listeners.discard(self.event)
            self.event = None
            self.last = None
            self.retry_at = None
            return f"event: {self.name}\ndata: 0\n\n"
        count = await _pending_user_count()
        if count is None:
            # Storage failure: keep the last good count on the badge and retry
            # instead of waiting for the next pending-user mutation.
            self._defer()
            return None
        self.retry_at = None
        if count != self.last:
            self.last = count
            return f"event: {self.name}\ndata: {count}\n\n"
        return None

    def _defer(self) -> None:
        """Schedule a role-checked retry; nothing is sent to the stream."""
        self.retry_at = _time.monotonic() + _PENDING_RETRY_SECONDS

    def retry_due(self) -> bool:
        """True when a scheduled retry has come due."""
        return self.retry_at is not None and _time.monotonic() >= self.retry_at

    def wait_timeout(self) -> float:
        """Seconds to wait: the earliest of the keepalive and the retry."""
        if self.retry_at is None:
            return _SSE_KEEPALIVE_SECONDS
        return min(_SSE_KEEPALIVE_SECONDS, max(0.0, self.retry_at - _time.monotonic()))


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

# Regex for validating job IDs in multiplexed SSE topics — prevents
# SSE header injection via control characters in the event prefix.
_VALID_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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
set_usage_callback(notify_job_status_changed)


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


# Workspace-preparation phase per job+user, reported to the chat UI while
# /init runs. In-memory only: init is request-scoped and the chat UI refetches
# it from GET /api/chat/{job_id} whenever notify_chat_changed fires.
_chat_prep_phases: dict[str, dict[str, Any]] = {}


def set_chat_prep_phase(
    job_id: str, username: str, phase: str, detail: str = ""
) -> None:
    """Record the current chat workspace-preparation phase and notify listeners."""
    _chat_prep_phases[f"{job_id}:{username}"] = {"phase": phase, "detail": detail}
    notify_chat_changed(job_id, username=username)


def clear_chat_prep_phase(job_id: str, username: str) -> None:
    """Drop the preparation phase once init finished (or failed)."""
    _chat_prep_phases.pop(f"{job_id}:{username}", None)


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

    async def event_generator() -> AsyncIterator[str]:
        my_event = asyncio.Event()
        wait_task: asyncio.Task[Any] | None = None

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
                        timeout=_SSE_KEEPALIVE_SECONDS,
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
    level=logging.NOTSET,  # level authority: the rootcoz parent logger (#347)
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


async def _attach_token_usage(
    job_id: str, result_data: dict[str, Any], *, detailed: bool = True
) -> None:
    """Attach token usage summary to result data. Best-effort \u2014 never raises."""
    try:
        token_summary = await build_token_usage_summary(job_id, detailed=detailed)
        if token_summary:
            result_data["token_usage"] = token_summary.model_dump(mode="json")
    except Exception:  # best-effort token tracking must never fail the job
        logger.debug("Failed to attach token usage for job %s", job_id, exc_info=True)


async def _bind_job_id(job_id: str) -> None:
    """FastAPI dependency that binds job_id to the logging context."""
    job_id_var.set(job_id)


# Statuses that indicate the analysis is still in progress.
IN_PROGRESS_STATUSES = ("pending", "running", "waiting")


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


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_control_chars(value: str) -> str:
    """Strip control characters from a string."""
    return _CONTROL_CHAR_RE.sub("", value)


def resolve_failure_review(
    reviews: dict[str, dict[str, Any]],
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> dict[str, Any] | None:
    """Resolve the review entry for a single test, honoring child scoping.

    Exact-build child reviews take precedence over wildcard (build 0) ones.
    A child-scoped test never matches a top-level (bare-name) review and
    vice versa, matching report-page semantics. Returns the review dict only
    when it represents a completed review carrying a username.
    """
    keys: tuple[str, ...]
    if child_job_name:
        keys = (
            f"{child_job_name}#{child_build_number}::{test_name}",
            f"{child_job_name}#0::{test_name}",
        )
    else:
        keys = (test_name,)
    for key in keys:
        entry = reviews.get(key)
        if entry and entry.get("reviewed") and entry.get("username"):
            return entry
    return None


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
    result_data: dict[str, Any],
    child_job_name: str = "",
    child_build_number: int = 0,
    matched_child: dict[str, Any] | None = None,
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
    child: dict[str, Any] | None = matched_child
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


def _attach_result_links(
    payload: dict[str, Any], base_url: str, job_id: str
) -> dict[str, Any]:
    """Attach ``base_url`` and ``result_url`` to a response payload."""
    payload["base_url"] = base_url
    result_url = f"{base_url}/results/{job_id}"
    payload["result_url"] = result_url
    return payload


async def _attach_origin_job_info(result: dict[str, Any]) -> None:
    """Attach re-analysis links (both directions) to the top-level response.

    ``reanalyzed_to_job_ids`` lists the re-analyses of this job (forward
    links, appended in enqueue order); ``reanalyzed_from_job_id`` and
    ``origin_job_name`` describe this job's origin.  Prefers the
    denormalized ``reanalyzed_from_job_name`` stored at creation time;
    falls back to a DB lookup for legacy data.
    """
    params = (result.get("result") or {}).get("request_params", {})
    forward_links = params.get("reanalyzed_to_job_ids") or []
    if isinstance(forward_links, list) and forward_links:
        result["reanalyzed_to_job_ids"] = forward_links
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


def _validate_decrypted_sensitive_fields(decrypted_params: dict[str, Any]) -> None:
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
    "force_server_credentials",
    "raw_prompt",
    "issue_prompt",
    "tests_repo_url",
    "tests_repo_token",
    "peer_analysis_max_rounds",
    "enable_jira",
    "jira_project_key",
    "jira_max_results",
    "github_token",
    "ai_call_timeout",
    "max_concurrent_ai_calls",
    "labels",
)


def _copy_analysis_settings(
    decrypted_params: dict[str, Any], unified_fields: dict[str, Any]
) -> None:
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

    # Legacy fallback: old stored params may use metadata_labels instead of labels
    if "labels" not in unified_fields and "metadata_labels" in decrypted_params:
        val = decrypted_params["metadata_labels"]
        if val is not None:
            unified_fields["labels"] = val


def _reconstruct_from_params(
    result_data: dict[str, Any],
) -> tuple[UnifiedAnalyzeRequest, Settings]:
    """Reconstruct a UnifiedAnalyzeRequest and Settings from stored request_params.

    Used to resume waiting Jenkins jobs after a server restart.

    Args:
        result_data: Stored result dict containing ``job_name``,
            ``build_number``, and ``request_params``.

    Returns:
        Tuple of (UnifiedAnalyzeRequest, Settings).
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
                    "Cannot resume waiting job: stored additional_repos token "
                    "could not be decrypted"
                )

    analysis_type = params.get("analysis_type", "jenkins")
    unified_fields: dict[str, Any] = {
        "type": analysis_type,
        "job_name": result_data.get("job_name") or params.get("job_name"),
        "build_number": result_data.get("build_number") or params.get("build_number"),
        "name": params.get("name") or None,
        "ai_provider": params.get("ai_provider", ""),
        "ai_model": params.get("ai_model", ""),
        "force_server_credentials": params.get("force_server_credentials"),
        "wait_for_completion": params.get("wait_for_completion", True),
        "poll_interval_minutes": params.get("poll_interval_minutes", 2),
        "max_wait_minutes": params.get("max_wait_minutes", 0),
        "enable_jira": params.get("enable_jira"),
        "raw_prompt": params.get("raw_prompt") or None,
        "issue_prompt": params.get("issue_prompt") or None,
        "tests_repo_url": _recompose_repo_spec(
            params.get("tests_repo_url", ""), params.get("tests_repo_ref", "")
        )
        or None,
        "peer_ai_configs": params.get("peer_ai_configs", []),
        "peer_analysis_max_rounds": params.get("peer_analysis_max_rounds", 3),
        "additional_repos": params.get("additional_repos", None),
        "tests_repo_token": params.get("tests_repo_token", None),
        "labels": params.get("labels", params.get("metadata_labels", [])),
    }
    for jenkins_field in (
        "jenkins_url",
        "jenkins_user",
        "jenkins_password",
        "jenkins_ssl_verify",
        "jenkins_timeout",
        "jenkins_artifacts_max_size_mb",
        "get_job_artifacts",
    ):
        if jenkins_field in params:
            unified_fields[jenkins_field] = params[jenkins_field]

    body = UnifiedAnalyzeRequest(**unified_fields)

    # Build Settings from env defaults, then layer stored overrides
    base_settings = get_settings()
    overrides: dict[str, Any] = {}
    settings_fields = [
        "jenkins_url",
        "jenkins_user",
        "jenkins_password",
        "jenkins_ssl_verify",
        "jenkins_timeout",
        "wait_for_completion",
        "poll_interval_minutes",
        "max_wait_minutes",
        "jira_email",
        "jira_project_key",
        "jira_max_results",
        "atlassian_oauth_cloud_id",
        "jira_site_url",
        "ai_call_timeout",
        "max_concurrent_ai_calls",
        "force_server_credentials",
        "jenkins_artifacts_max_size_mb",
        "get_job_artifacts",
        "peer_analysis_max_rounds",
    ]
    for field in settings_fields:
        if field in params:
            overrides[field] = params[field]

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
    if params.get("jira_atst_token") is not None:
        overrides["jira_atst_token"] = (
            SecretStr(params["jira_atst_token"]) if params["jira_atst_token"] else None
        )
    # New key wins; the old jira_oauth_access_token key is accepted so
    # request_params stored before the rename still replay exactly.
    stored_oauth_secret = params.get(
        "jira_oauth_client_secret", params.get("jira_oauth_access_token")
    )
    if stored_oauth_secret is not None:
        overrides["jira_oauth_client_secret"] = (
            SecretStr(stored_oauth_secret) if stored_oauth_secret else None
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


_background_tasks: set[asyncio.Task[Any]] = set()

# Track analysis background tasks by job_id for abort support
_job_tasks: dict[str, asyncio.Task[Any]] = {}


def _remove_job_task(job_id: str) -> Callable[[asyncio.Task[object]], None]:
    """Create a done-callback that removes a job from the task tracker."""

    def _callback(_task: asyncio.Task[object]) -> None:
        _job_tasks.pop(job_id, None)

    return _callback


def _register_job_task(job_id: str, task: asyncio.Task[Any]) -> None:
    """Register an analysis task for tracking and abort support."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_remove_job_task(job_id))
    _job_tasks[job_id] = task


async def _preserve_request_params(job_id: str, result_data: dict[str, Any]) -> None:
    """Copy persisted enqueue-time fields from the stored result into result_data.

    The initial ``save_result`` persists ``request_params``, ``tags``,
    ``display_name``, identity fields (``job_name``, ``build_number``,
    ``build_id``), and ``analyzed_by`` / ``analyzed_at`` but the
    ``AnalysisResult`` model dump produced when analysis finishes does not
    include those keys.  Without this merge the fields would be silently
    lost when ``update_status`` overwrites ``result_json``.

    Args:
        job_id: The analysis job identifier.
        result_data: Mutable dict that will be written to ``result_json``.
            Modified in place to add preserved fields when available.
    """
    stored = await get_result(job_id, strip_sensitive=False)
    if stored and stored.get("result"):
        stored_result = stored["result"]
        for key in (
            "request_params",
            "tags",
            "display_name",
            "job_name",
            "build_number",
            "build_id",
            "analyzed_by",
            "analyzed_at",
        ):
            if key in stored_result and key not in result_data:
                result_data[key] = stored_result[key]
        copy_stable_analysis_ids(stored_result, result_data)


async def _fail_resumed_waiting_job(
    job_id: str, result_data: dict[str, Any], error: str
) -> None:
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
        "build_number": resolve_display_build_id(result_data),
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


async def _resume_waiting_jobs(waiting_jobs: list[dict[str, Any]]) -> None:
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
        except (ValueError, KeyError, TypeError, ValidationError, RuntimeError) as exc:
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
            except TypeError, ValueError:
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

        token = ai_username.set(resumed_username)
        try:
            ai_provider, ai_model = await _resolve_ai_config_allow_defer(body, merged)
            resolved_peers = await _validate_peer_configs(body, merged)
        except Exception as exc:
            reason = (
                exc.detail
                if isinstance(exc, HTTPException)
                else "AI/peer validation unavailable (grant or catalog lookup failed)"
            )
            logger.exception(
                "Cannot resume waiting job %s", job["job_id"], exc_info=False
            )
            await _fail_resumed_waiting_job(
                job["job_id"],
                result_data,
                f"Cannot resume: AI/peer validation failed: {reason}",
            )
            continue
        finally:
            ai_username.reset(token)
        tests_repo_url_raw = resolve_tests_repo_url(body, merged)
        tests_repo_url, tests_repo_ref = parse_repo_ref(tests_repo_url_raw)
        resolved_tests_repo_token = (
            resolve_tests_repo_token(body, merged) if tests_repo_url else ""
        )
        additional_repos_list = resolve_additional_repos(body, merged)
        display_name = (
            result_data.get("display_name")
            or result_data.get("job_name")
            or body.job_name
            or ""
        )

        task = asyncio.create_task(
            _process_ci_source_analysis(
                job_id=job["job_id"],
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
                base_url=_build_internal_server_url(),
                username=resumed_username,
            )
        )
        _register_job_task(job["job_id"], task)
        logger.info(
            f"Resumed waiting job {job['job_id']} "
            f"({result_data.get('display_name') or result_data.get('job_name')} #{result_data.get('build_number')})"
        )


async def _safe_preload_cursor_models() -> None:
    """Pre-populate the sidecar model catalog in background. Best-effort."""
    try:
        await list_models()
    except Exception:
        logger.debug("Failed to preload sidecar models", exc_info=True)


async def _backfill_job_metadata(rules: list[dict[str, Any]]) -> None:
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


async def _deferred_resume_waiting_jobs(waiting_jobs: list[dict[str, Any]]) -> None:
    """Resume waiting jobs after startup is complete.

    Waits briefly so uvicorn finishes binding and the app is ready to
    serve internal API requests before any resumed job transitions to
    the "running" phase.
    """
    await asyncio.sleep(1)
    try:
        await _resume_waiting_jobs(waiting_jobs)
    except Exception:
        logger.exception("Failed to resume waiting jobs after startup")


# Settings whose DB overrides reach the sidecar through the
# /data/pi-sidecar/env bridge the entrypoint sources at container start
# (#347). The sidecar starts once per boot, so these are restart-required.
_SIDECAR_BRIDGE_SETTINGS: tuple[str, ...] = (
    "sidecar_host",
    "sidecar_port",
    "sidecar_url",
    "acpx_agents",
    "cli_agents",
    "pi_sidecar_log_level",
    "node_binary",
    "pi_sidecar_transcripts_dir",
)


def _is_loopback_host(host: str) -> bool:
    """True for loopback bind addresses (127.0.0.0/8, ::1, localhost)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sidecar_host_supports_loopback_probe(host: str) -> bool:
    """True when a sidecar bind host keeps the loopback health probe working.

    The container entrypoint probes ``http://127.0.0.1:$SIDECAR_PORT`` and
    aborts startup when it fails, and the probe result persists under
    ``/data`` — a bind address that can never answer on 127.0.0.1 would
    brick every subsequent restart before the UI could undo it. The probe
    is a fixed IPv4 call to 127.0.0.1, so only the deployment default
    (empty), ``0.0.0.0`` (answers on 127.0.0.1 too), and ``127.0.0.1``
    itself are known-safe (#347 r4): every other spelling — ``::1``,
    ``127.0.0.2``, ``localhost``, ``::`` — can bind an address the probe
    never reaches.
    """
    return host.strip() in ("", "0.0.0.0", "127.0.0.1")


def _derive_sidecar_url_from_port() -> None:
    """Point the sidecar client at SIDECAR_PORT when no URL is configured.

    The Python client defaults to ``127.0.0.1:9100`` regardless of
    SIDECAR_PORT, so a port override would change the listener but not
    the client (#347). An explicitly configured SIDECAR_URL always wins;
    the container entrypoint applies the same derivation at boot.
    """
    if os.environ.get("SIDECAR_URL"):
        return
    port = (os.environ.get("SIDECAR_PORT") or "").strip()
    if not port or port == "9100":
        return
    os.environ["SIDECAR_URL"] = f"http://127.0.0.1:{port}"
    logger.info("Derived SIDECAR_URL=http://127.0.0.1:%s from SIDECAR_PORT", port)


def _write_sidecar_env_bridge() -> None:
    """Write the full sidecar env bridge from the current DB overrides.

    Every bridged var is written in one pass: a var with no DB override is
    left unassigned so the deployment environment governs. Callers must
    always write the full set — a partial write would drop the others.
    """
    from rootcoz.config import db_setting_is_set, get_db_setting

    values: dict[str, str | None] = {"RECORD_TRANSCRIPTS": None}
    if db_setting_is_set("record_transcripts"):
        values["RECORD_TRANSCRIPTS"] = (
            "true" if get_settings().record_transcripts else "false"
        )
    for key in _SIDECAR_BRIDGE_SETTINGS:
        values[key.upper()] = get_db_setting(key) if db_setting_is_set(key) else None
    write_sidecar_env_bridge(values)


def _apply_log_level_setting() -> None:
    """Apply a DB-configured log_level to the rootcoz logger tree (restart-required).

    Every rootcoz module logger is created NOTSET and inherits from the
    ``rootcoz`` parent logger (see rootcoz.get_child_logger), so setting
    the parent level covers the whole tree — including modules imported
    lazily after this ran, which the old walk-existing-loggers approach
    missed.
    """
    from rootcoz.config import db_setting_is_set, get_settings

    if not db_setting_is_set("log_level"):
        return
    name = get_settings().log_level.strip().upper()
    if not name:
        # Explicit empty override (#352) — nothing to apply; the boot env
        # level (or the logger default) governs.
        return
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        logger.warning("Ignoring invalid log_level setting: %r", name)
        return
    logging.getLogger("rootcoz").setLevel(level)
    logger.info("Applied DB log_level=%s to the rootcoz logger tree", name)


async def _apply_transcript_recording_setting() -> bool:
    """Persist + live-apply the record_transcripts DB state.

    An explicit DB override is bridged to the entrypoint AND pushed to the
    running sidecar through its runtime toggle (pi-sidecar >= 4.14.0 — no
    restart needed). Without an override nothing is assigned and the
    deployment environment governs, both on disk and at runtime.

    The bridge write carries every sidecar setting (#347), not just
    RECORD_TRANSCRIPTS — the other sidecar vars have no runtime toggle and
    apply at the next restart.

    Returns True when the runtime toggle reached the sidecar (or had nothing
    to push); False when the sidecar was unreachable/rejected it — callers
    surface that so admins know the change applies only after a restart or
    a later successful sync.
    """
    from rootcoz.config import db_setting_is_set

    enabled = (
        get_settings().record_transcripts
        if db_setting_is_set("record_transcripts")
        else None
    )
    _write_sidecar_env_bridge()
    return await sidecar_transcripts.sync_transcript_recording(enabled)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _install_job_id_filter()
    _install_sse_log_handler()
    _setup_usage_recorder()
    _derive_sidecar_url_from_port()

    await init_db()
    await storage.cleanup_expired_sessions()
    await _cleanup_revoked_ai_sessions()

    # Load DB setting overrides BEFORE config validation so
    # validate_startup_config() sees merged env + DB values.
    from rootcoz.config import load_db_settings

    await load_db_settings()
    _apply_log_level_setting()

    # Keep the transcript recording state in sync everywhere: the env bridge
    # persists the DB value for the next boot, and the sidecar runtime toggle
    # applies it to the running sidecar immediately (pi-sidecar >= 4.14.0).
    await _apply_transcript_recording_setting()

    # Startup config validation (after DB settings loaded)
    config_result = validate_startup_config()
    for error in config_result.errors:
        logger.error("[startup] %s", error)
    for warning in config_result.warnings:
        logger.warning("[startup] %s", warning)
    if config_result.errors:
        raise RuntimeError("Startup configuration validation failed")

    try:
        cleanup_task = asyncio.create_task(_periodic_session_cleanup())

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

        waiting_jobs, recovered_jobs = await storage.mark_stale_results_failed()
        for rj in recovered_jobs:
            logger.info(
                "Recovered orphaned job %s (was %s) — marked failed",
                rj["job_id"],
                rj["previous_status"],
            )
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
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


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

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
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


def _maybe_add_custom_approval_msg(content: dict[str, Any], settings: Settings) -> None:
    """Append custom admin approval message to response content if configured."""
    if settings.admin_wait_approve_msg:
        content["custom_message"] = settings.admin_wait_approve_msg


def _blocked_user_status_response(user_status: str | None) -> JSONResponse | None:
    """Return a 403 JSONResponse if user_status is pending/rejected, else None."""
    detail = _APPROVAL_STATUS_RESPONSES.get(user_status or "")
    if detail is None:
        return None
    settings = get_settings()
    content: dict[str, Any] = {"detail": detail, "status": user_status}
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
            # Intentional public API-discovery surfaces (issue #200 / AGENTS.md):
            # OpenAPI schema + Swagger/ReDoc UIs only — no job or user data.
            # Project compliance allowlist source of truth for these paths.
            "/openapi.json",
            "/docs",
            "/redoc",
        }
    )

    @classmethod
    def _is_public_path(cls, path: str) -> bool:
        """True when path (or its non-root trailing-slash form) is public."""
        if path in cls._PUBLIC_PATHS:
            return True
        # Treat /docs/ and /redoc/ (etc.) as public without turning "/" into "".
        return path != "/" and path.rstrip("/") in cls._PUBLIC_PATHS

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # CORS preflight requests must pass through without authentication
        if request.method == "OPTIONS":
            request.state.username = ""
            request.state.is_admin = False
            request.state.role = "reviewer"
            request.state.can_view_reports = False
            request.state.can_use_server_providers = False
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
        request.state.role = "reviewer"
        request.state.can_view_reports = False
        request.state.can_use_server_providers = False

        # Public paths and static assets — pass through
        # (but /login may need SSO redirect, handled below;
        #  it stays in _PUBLIC_PATHS for non-SSO users who need the page)
        path_key = path if path == "/" else path.rstrip("/")
        if path.startswith("/assets/") or (
            self._is_public_path(path) and path_key != "/login"
        ):
            return await call_next(request)

        settings = get_settings()

        # SSO: when trust_proxy_headers is enabled and X-Forwarded-User is
        # present, auto-identify the user and redirect /login → /
        proxy_username = ""
        if settings.trust_proxy_headers:
            proxy_username = request.headers.get("x-forwarded-user", "").strip().lower()

        if path.startswith("/login"):
            if proxy_username and proxy_username.lower() != "admin":
                # Session auth takes precedence over X-Forwarded-User —
                # only check when an SSO redirect would otherwise fire.
                session_token = _read_cookie(request, "rootcoz_session")
                if session_token and await storage.get_session(session_token):
                    return await call_next(request)
                # SSO user hitting /login — redirect to dashboard
                response: Response = RedirectResponse(url="/", status_code=303)
                if _read_cookie(request, "rootcoz_username") != proxy_username:
                    _set_username_cookie(
                        response, proxy_username, secure=settings.secure_cookies
                    )
                return response
            return await call_next(request)

        is_admin = False
        username = ""
        resolved_role = get_settings().default_user_role  # default role until resolved
        authenticated_admin = False
        has_valid_session = False
        # User row already loaded via Bearer key or SSO (reuse for can_view_reports).
        fetched_user: dict[str, Any] | None = None

        # 1. Check session cookie (rootcoz_session) — user or admin session
        session_token = _read_cookie(request, "rootcoz_session")
        if session_token:
            session = await storage.get_session(session_token)
            if session:
                is_admin = bool(session["is_admin"])
                username = str(session["username"])
                authenticated_admin = is_admin
                has_valid_session = True
                if is_admin:
                    resolved_role = "admin"
                else:
                    resolved_role = str(session.get("role", "reviewer"))

                # Renew session (sliding window) — only when <50% TTL remains
                expires_at_str = session.get("expires_at", "")
                if expires_at_str:
                    try:
                        expires_at = datetime.strptime(
                            str(expires_at_str), "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=UTC)
                        remaining = expires_at - datetime.now(UTC)
                        if remaining < timedelta(
                            hours=storage.current_session_ttl_hours() / 2
                        ):
                            # Await renewal so cookie refresh is only set after confirmed DB update
                            try:
                                renewed = await storage.renew_session(session_token)
                                if renewed:
                                    request.state.renew_session_token = session_token
                            except Exception:
                                logger.debug("Session renewal failed", exc_info=True)
                    except ValueError, TypeError:
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
                    resolved_role = "admin"
                    authenticated_admin = True
                    has_valid_session = True
                else:
                    user = await storage.get_user_by_key(token)
                    if user:
                        fetched_user = user
                        username = str(user["username"])
                        resolved_role = str(user.get("role", "reviewer"))
                        has_valid_session = True
                        if resolved_role == "admin":
                            is_admin = True
                            authenticated_admin = True
                    else:
                        # Fall back to session token via Bearer header
                        # (used by AI for internal API calls)
                        session = await storage.get_session(token)
                        if session:
                            is_admin = bool(session["is_admin"])
                            username = str(session["username"])
                            resolved_role = str(session.get("role", "reviewer"))
                            if is_admin:
                                resolved_role = "admin"
                            authenticated_admin = is_admin
                            has_valid_session = True

        # 3. Check X-Forwarded-User header (SSO via trusted proxy)
        if not username and proxy_username and proxy_username.lower() != "admin":
            username = proxy_username
            has_valid_session = True
            # Resolve the user's actual role from DB
            proxy_user = await storage.get_user_by_username(username)
            if proxy_user:
                fetched_user = proxy_user
                resolved_role = str(proxy_user.get("role", "reviewer"))
                if resolved_role == "admin":
                    is_admin = True
                    authenticated_admin = True
            # Flag that we need to set the rootcoz_username cookie on the response
            request.state.set_proxy_cookie = proxy_username

        # 4. Fall back to rootcoz_username cookie (regular users)
        if not username:
            cookie_username = _read_cookie(request, "rootcoz_username").strip().lower()
            if cookie_username == "admin":
                # Reserved username — only valid via session/bearer auth
                cookie_username = ""
            username = cookie_username

        request.state.username = username
        request.state.is_admin = is_admin
        request.state.role = resolved_role
        # can_view_reports: admins effective True; non-admins reload stored flag
        # from users table each request (no session invalidation needed).
        # Reuse a user row already loaded via Bearer/SSO when available.
        if is_admin:
            request.state.can_view_reports = effective_can_view_reports(True)
        elif has_valid_session and username:
            db_user = fetched_user
            if db_user is None:
                db_user = await storage.get_user_by_username(username)
            request.state.can_view_reports = effective_can_view_reports(
                False, bool(db_user and db_user.get("can_view_reports"))
            )
        else:
            request.state.can_view_reports = False

        if has_valid_session and username:
            try:
                request.state.can_use_server_providers = (
                    await storage.can_user_use_server_providers(username)
                )
                request.state.can_use_free_providers = (
                    await storage.can_user_use_free_providers(username)
                )
            except Exception:
                logger.exception("Unable to load server provider grant", exc_info=False)

        # Track user activity only for authenticated identities
        if has_valid_session and username:
            task = asyncio.create_task(_safe_track_user(username))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        # Admin-only path enforcement
        if path.startswith("/api/admin/") and not authenticated_admin:
            return JSONResponse(
                status_code=403, content={"detail": "Admin access required"}
            )

        # Require authentication for all non-public, non-optional paths
        if not has_valid_session and not self._is_public_path(path):
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
            and not self._is_public_path(path)
        ):
            user_status = await storage.get_user_status(username)
            blocked = _blocked_user_status_response(user_status)
            if blocked is not None:
                return blocked

        username_token = ai_username.set(username)
        force_token = force_server_credentials.set(settings.force_server_credentials)
        try:
            response = await call_next(request)
        finally:
            ai_username.reset(username_token)
            force_server_credentials.reset(force_token)

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
                    samesite="lax",
                    secure=settings.secure_cookies,
                    max_age=storage.current_session_ttl_seconds(),
                )
                # Clear legacy cookie after migration
                response.delete_cookie(
                    "jji_session",
                    httponly=True,
                    samesite="lax",
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

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in _BODY_LOGGING_SKIP_PATHS or request.url.path.startswith(
            ("/api/chat/", "/api/user/ai-credentials")
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
                except json.JSONDecodeError, UnicodeDecodeError:
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

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if (
            request.url.path.startswith("/assets/")
            and 200 <= response.status_code < 400
        ):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.add_middleware(CacheControlMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)


def _mask_pydantic_error(error: dict[str, Any]) -> dict[str, Any]:
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
        except TypeError, ValueError, AttributeError, RecursionError:
            # masking must never break the 422 response
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


def _ai_not_configured_message(
    request: Request | None,
    what: str,
    *,
    is_admin: bool | None = None,
) -> str:
    """Build a role-aware error message when AI provider/model is not configured."""
    from rootcoz.error_messages import ai_not_configured_message

    if is_admin is None:
        is_admin = bool(
            getattr(getattr(request, "state", None), "is_admin", False)
            if request
            else False
        )
    return ai_not_configured_message(what, is_admin=bool(is_admin))


def _resolve_ai_provider_model(
    ai_provider: str | None = None,
    ai_model: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Resolve AI provider and model without validation.

    Resolution order (first non-empty wins):
    1. Per-request value (ai_provider/ai_model arguments)
    2. Merged settings (env var base + DB overrides via get_settings())

    Returns:
        Tuple of (resolved_provider, resolved_model). May be empty strings.
    """
    if settings is None:
        settings = get_settings()
    provider = normalize_provider(ai_provider or settings.ai_provider)
    model = ai_model or settings.ai_model
    return provider, model


def _resolve_ai_config_values(
    ai_provider: str | None, ai_model: str | None, *, request: Request | None = None
) -> tuple[str, str]:
    """Resolve and validate AI provider and model.

    Uses ``_resolve_ai_provider_model`` for resolution, then validates
    that provider and model are configured and the provider is supported.

    Args:
        ai_provider: Provider from request body (or None).
        ai_model: Model from request body (or None).

    Returns:
        Tuple of (ai_provider, ai_model).

    Raises:
        HTTPException: If provider or model is not configured.
    """
    provider, model = _resolve_ai_provider_model(ai_provider, ai_model)
    if not provider:
        raise HTTPException(
            status_code=400,
            detail=_ai_not_configured_message(request, "AI provider"),
        )
    if not model:
        raise HTTPException(
            status_code=400,
            detail=_ai_not_configured_message(request, "AI model"),
        )
    return provider, model


async def _validate_catalog_pair(provider: str, model: str) -> tuple[str, str]:
    """Return a catalog-backed AI pair or a clear client error."""
    from rootcoz.ai_client import InactiveServerProviderError

    try:
        return await resolve_catalog_pair(provider, model)
    except InactiveServerProviderError as exc:
        logger.info(
            "Rejected server credentials for deactivated provider: %s/%s",
            provider,
            model,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        logger.info("Rejected unknown AI provider/model pair: %s/%s", provider, model)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Unable to validate AI provider/model pair", exc_info=True)
        raise HTTPException(
            status_code=503, detail="AI model catalog is temporarily unavailable"
        ) from exc


async def _resolve_ai_config(
    body: BaseAnalysisRequest, request: Request | None = None
) -> tuple[str, str]:
    """Resolve AI config from an AnalyzeRequest."""
    provider, model = _resolve_ai_config_values(
        body.ai_provider, body.ai_model, request=request
    )
    return await _validate_catalog_pair(provider, model)


async def _resolve_ai_config_allow_defer(
    body: BaseAnalysisRequest,
    settings: Settings,
    request: Request | None = None,
) -> tuple[str, str]:
    """Resolve AI config, deferring when a tests repo can supply ``settings.json``.

    Priority after clone is request → ``.rootcoz/settings.json`` → server.
    When neither request nor server provides AI but a tests repo URL exists,
    return empty strings so analysis can load ``settings.json`` after clone.

    Unsupported providers fail immediately even when model is missing (defer
    only applies to incomplete config, not invalid providers).
    """
    force_server_credentials.set(
        body.force_server_credentials
        if body.force_server_credentials is not None
        else settings.force_server_credentials
    )
    provider, model = _resolve_ai_provider_model(
        body.ai_provider, body.ai_model, settings=settings
    )
    request_pair = bool(body.ai_provider and body.ai_model)
    if request_pair:
        return await _validate_catalog_pair(provider, model)
    if tests_repo_available(body, settings):
        logger.info(
            "AI provider/model not explicitly set; "
            "deferring to .rootcoz/settings.json after tests repo clone"
        )
        return "", ""
    if provider and model:
        return await _validate_catalog_pair(provider, model)
    # Validate using the already-resolved values from *settings* — do not
    # re-resolve via _resolve_ai_config_values (that uses global get_settings()).
    if not provider:
        raise HTTPException(
            status_code=400,
            detail=_ai_not_configured_message(request, "AI provider"),
        )
    if not model:
        raise HTTPException(
            status_code=400,
            detail=_ai_not_configured_message(request, "AI model"),
        )
    return provider, model


def _resolve_peer_ai_configs(
    body: BaseAnalysisRequest, settings: Settings
) -> list[Any] | None:
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


def _peer_catalog_pair(peer: Any) -> tuple[str, str]:
    """Return a peer provider/model pair from request models or server dictionaries."""
    if hasattr(peer, "ai_provider"):
        return peer.ai_provider, peer.ai_model
    return peer["ai_provider"], peer["ai_model"]


async def _validate_peer_configs(
    body: BaseAnalysisRequest, settings: Settings
) -> list[Any] | None:
    """Resolve and catalog-validate peer AI configs."""
    if tests_repo_available(body, settings) and body.peer_ai_configs is None:
        logger.info(
            "Deferring server peer validation to .rootcoz/settings.json overlay"
        )
        return None
    try:
        peers = _resolve_peer_ai_configs(body, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for peer in peers or []:
        await _validate_catalog_pair(*_peer_catalog_pair(peer))
    return peers


def _resolve_enable_jira(body: BaseAnalysisRequest, settings: Settings) -> bool:
    """Resolve enable_jira flag from request or settings.

    Priority order:
    1. Request body field (highest)
    2. settings.jira_enabled property (requires a selected jira_auth_mode)

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

    Always returns a **new** ``Settings`` instance so later in-place mutations
    (e.g. ``propagate_repo_settings_overlay``) cannot alter the cached
    ``get_settings()`` object.

    Args:
        body: The analysis request with optional override fields.
        settings: Base application settings from environment.

    Returns:
        New Settings instance with overrides applied (copy even when empty).
    """
    overrides: dict[str, Any] = {}

    # Direct field mappings (request field name == settings field name).
    # Keep in sync with BaseAnalysisRequest and Settings when adding new overrides.
    # Fields intentionally NOT listed here are handled by their own resolvers:
    #   tests_repo_url   - HttpUrl vs str type mismatch; resolved in endpoint code
    #   ai_provider      - resolved + validated by _resolve_ai_config()
    #   ai_model         - resolved + validated by _resolve_ai_config()
    #   additional_repos - list vs str type mismatch; resolved by resolve_additional_repos()
    # Jira credentials (jira_email/jira_api_token/jira_pat/
    # jira_oauth_client_secret/jira_atst_token) are intentionally absent: they
    # are Server Settings only and can never be supplied per request.
    direct_fields = [
        "jira_project_key",
        "jira_max_results",
        "atlassian_oauth_cloud_id",
        "jira_site_url",
        "ai_call_timeout",
        "max_concurrent_ai_calls",
        "force_server_credentials",
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
    if body.github_token is not None:
        overrides["github_token"] = SecretStr(body.github_token)
    if body.tests_repo_token is not None:
        overrides["tests_repo_token"] = SecretStr(body.tests_repo_token)

    # Jenkins connection + polling overrides (AnalyzeRequest and
    # UnifiedAnalyzeRequest both inherit _JenkinsParamsMixin).
    if isinstance(body, _JenkinsParamsMixin):
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

    # UnifiedAnalyzeRequest-specific fields (Prow overrides)
    if isinstance(body, UnifiedAnalyzeRequest):
        if "prow_url" in body.model_fields_set and body.prow_url:
            overrides["prow_url"] = body.prow_url
        if "gcs_bucket" in body.model_fields_set and body.gcs_bucket:
            overrides["gcs_bucket"] = body.gcs_bucket

    merged_data = settings.model_dump(mode="python") | overrides
    return Settings.model_validate(merged_data)


# Truncation length for error signatures in log messages (SHA-256 prefix for readability)
_SIG_PREVIEW_LEN = 12


async def _apply_auto_review(
    job_id: str,
    test_name: str,
    error_sig: str,
    prev_job_id: str,
    prev_build: int | str,
    child_job_name: str = "",
    child_build_number: int = 0,
    error_sig_v2: str = "",
) -> None:
    """Apply auto-review to a single failure: mark reviewed, add comment, log.

    Args:
        job_id: Current analysis job ID.
        test_name: Name of the test being auto-reviewed.
        error_sig: Anchor signature that matched.
        error_sig_v2: Current-rules signature of the same failure, dual-written
            onto the comment so signature lookups find it either way.
        prev_job_id: Job ID of the previous matching analysis.
        prev_build: Build number of the previous matching analysis.
        child_job_name: Child job name (empty for top-level failures).
        child_build_number: Child build number (0 for top-level failures).
    """
    base_url = _extract_base_url()
    if base_url:
        job_link = f"{base_url}/results/{prev_job_id}"
        comment = (
            f"Auto-reviewed: identical failure signature found in previous "
            f"analysis {job_link} (build #{prev_build})"
        )
    else:
        comment = (
            f"Auto-reviewed: identical failure signature found in previous "
            f"analysis `{prev_job_id}` (build #{prev_build})"
        )
    await storage.set_reviewed(
        job_id,
        test_name,
        reviewed=True,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        username=AI_SYSTEM_USERNAME,
    )
    await storage.add_comment(
        job_id,
        test_name,
        comment=comment,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        error_signature=error_sig,
        error_signature_v2=error_sig_v2,
        username=AI_SYSTEM_USERNAME,
    )

    if child_job_name:
        logger.info(
            "Auto-reviewed %s (child %s#%d): identical signature %s found in "
            "previous analysis %s",
            test_name,
            child_job_name,
            child_build_number,
            error_sig[:_SIG_PREVIEW_LEN],
            prev_job_id,
        )
    else:
        logger.info(
            "Auto-reviewed %s: identical signature %s found in previous analysis %s",
            test_name,
            error_sig[:_SIG_PREVIEW_LEN],
            prev_job_id,
        )


async def _match_and_auto_review_failures(
    job_id: str,
    job_name: str,
    failures: list[dict[str, Any]],
    child_job_name: str = "",
    child_build_number: int = 0,
) -> tuple[int, int]:
    """Check failures against previous analyses and auto-review matches.

    For each failure, looks up the same test_name in the same job_name from a
    previous analysis. If the signatures match (either column -- see
    storage.previous_analysis_matches), applies auto-review.

    Args:
        job_id: Current analysis job ID.
        job_name: Jenkins job name.
        failures: List of failure dicts to check.
        child_job_name: Child job name (empty for top-level failures).
        child_build_number: Child build number (0 for top-level failures).

    Returns:
        Tuple of (auto_reviewed_count, total_failure_count).
    """
    reviewed = 0
    total = 0

    for failure in failures:
        total += 1
        test_name = failure.get("test_name", "")
        if not test_name or not storage.signature_hashes(failure):
            continue

        previous = await storage.find_matching_previous_analysis(
            job_name,
            test_name,
            job_id,
            child_job_name=child_job_name,
        )
        if previous is None:
            continue
        # Hash-set match, not equality on one column: the previous row may predate
        # the v2 rules and carry only its anchor, while this failure carries both.
        # Comparing a single column is what would silently stop auto-review from
        # chaining across a deploy. That helper, not signatures_match, carries
        # the wider "same normalized message" last resort -- a human confirms the
        # chain on the report, so only the history lookup may lean on it.
        if not storage.previous_analysis_matches(previous, failure):
            continue

        prev_job_id = previous["job_id"]
        prev_build = previous["build_number"] or previous.get("build_id") or 0
        await _apply_auto_review(
            job_id,
            test_name,
            failure.get("error_signature", ""),
            prev_job_id,
            prev_build,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
            error_sig_v2=failure.get("error_signature_v2", ""),
        )
        reviewed += 1

    return reviewed, total


async def _auto_review_matching_failures(
    job_id: str,
    job_name: str,
    build_number: int | str,
    result_data: dict[str, Any],
    settings: Settings,
) -> None:
    """Auto-review failures with identical signatures from previous analyses.

    For each failure in the result, looks up the same test_name in the same
    job_name from a previous analysis. If the signatures match (either column --
    see storage.previous_analysis_matches), marks the failure as reviewed with
    username=AI_SYSTEM_USERNAME and adds an explanatory comment.

    If all failures end up reviewed, pushes to exporters configured in
    AUTO_PUSH_EXPORTERS.

    Args:
        job_id: Current analysis job ID.
        job_name: Jenkins job name.
        build_number: Build number being analyzed.
        result_data: Stored result dict containing failures.
        settings: Application settings.
    """
    if not settings.enable_auto_review:
        logger.debug("Auto-review disabled, skipping for job_id=%s", job_id)
        return
    auto_reviewed_count = 0
    total_failures = 0

    # Process top-level failures
    reviewed, total = await _match_and_auto_review_failures(
        job_id, job_name, result_data.get("failures", [])
    )
    auto_reviewed_count += reviewed
    total_failures += total

    # Process child job failures (recursive)
    for child in result_data.get("child_job_analyses", []):
        child_reviewed, child_total = await _auto_review_child_failures(
            job_id, job_name, child
        )
        auto_reviewed_count += child_reviewed
        total_failures += child_total

    if auto_reviewed_count > 0:
        logger.info(
            "Auto-reviewed %d/%d failures for job %s (build #%s)",
            auto_reviewed_count,
            total_failures,
            job_name,
            build_number,
        )
        notify_job_status_changed(job_id)

        # Check if ALL failures are now reviewed → auto-push to configured exporters
        _raw_names = [
            _sanitize_control_chars(n.strip())
            for n in (settings.auto_push_exporters or "").split(",")
        ]
        auto_push_names = list(
            dict.fromkeys(n for n in _raw_names if n and n in _EXPORTER_CLASSES)
        )
        if (
            auto_push_names
            and total_failures > 0
            and await _unreviewed_failure_count(job_id, total_failures) == 0
        ):
            # Auto-push must emit the same date_happened as the manual
            # push endpoint: reuse the stored analysis timestamp instead
            # of push time.
            stored = await get_result(job_id)
            context = await _build_export_context(
                job_id=job_id,
                result_data=result_data,
                settings=settings,
                pushed_by=AI_SYSTEM_USERNAME,
                fetch_history=_exporter_needs_history_classifications(auto_push_names),
                fetch_tracked_in_links=_exporter_needs_tracked_in_links(
                    auto_push_names, settings
                ),
                analysis_timestamp=(
                    _stored_analysis_timestamp(stored) if stored else None
                ),
            )
            for plugin_name in auto_push_names:
                logger.info(
                    "All failures auto-reviewed for job %s, pushing to "
                    "'%s' (pushed_by=%s)",
                    job_id,
                    plugin_name,
                    AI_SYSTEM_USERNAME,
                )
                try:
                    exporter = _create_exporter(plugin_name, settings)
                except (
                    ValueError,
                    TimeoutError,
                    OSError,
                    TypeError,
                    RuntimeError,
                ) as exc:
                    logger.warning(
                        "Auto-push skipped for exporter '%s', job_id=%s: %s",
                        plugin_name,
                        job_id,
                        exc,
                    )
                    continue
                try:
                    with exporter:
                        result = await exporter.push(context)
                    if not result.success:
                        logger.warning(
                            "Auto-push to '%s' returned failure for job_id=%s: %s",
                            plugin_name,
                            job_id,
                            result.message,
                        )
                except Exception:
                    logger.warning(
                        "Auto-push to '%s' failed for job_id=%s",
                        plugin_name,
                        job_id,
                        exc_info=True,
                    )


async def _auto_review_child_failures(
    job_id: str,
    job_name: str,
    child: dict[str, Any],
) -> tuple[int, int]:
    """Auto-review failures within a child job analysis.

    Returns:
        Tuple of (auto_reviewed_count, total_failure_count).
    """
    child_job_name = child.get("job_name", "")
    child_build_number = child.get("build_number", 0)

    reviewed, total = await _match_and_auto_review_failures(
        job_id,
        job_name,
        child.get("failures", []),
        child_job_name=child_job_name,
        child_build_number=child_build_number,
    )

    # Recurse into nested failed_children
    for nested_child in child.get("failed_children", []):
        nested_reviewed, nested_total = await _auto_review_child_failures(
            job_id, job_name, nested_child
        )
        reviewed += nested_reviewed
        total += nested_total

    return reviewed, total


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
    failures: Sequence[FailureAnalysis | ChildJobAnalysis],
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
    if not any(
        isinstance(failure.analysis.product_bug_report, ProductBugReport)
        and failure.analysis.product_bug_report.jira_search_keywords
        for failure in all_failures
    ):
        logger.debug(
            "Skipping Jira enrichment without product bug keywords for job=%s", job_id
        )
        return
    await safe_update_progress(job_id, "enriching_jira")
    logger.info("Enriching failures with Jira for job=%s", job_id)
    await enrich_with_jira_matches(
        all_failures,
        settings,
        ai_provider,
        ai_model,
        job_id=job_id,
    )


async def _enrich_result_with_tests_repo_matches(
    failures: Sequence[FailureAnalysis | ChildJobAnalysis],
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


async def _auto_assign_metadata(
    display_name: str,
    metadata_rules: list[dict[str, Any]] | None,
    extra_labels: list[str] | None = None,
) -> None:
    """Best-effort metadata auto-assignment and optional label merge."""
    rules = metadata_rules or []
    extras = extra_labels or []
    if not rules and not extras:
        return
    try:
        await storage.auto_assign_job_metadata(
            display_name, rules, extra_labels=extras or None
        )
    except Exception:
        logger.warning(
            "Failed to auto-assign metadata for job '%s'",
            display_name,
            exc_info=True,
        )


async def _ai_admin_role_current(username: str) -> bool:
    """Check the live role, not a role captured when chat was queued."""
    if username == "admin":  # Bootstrap admin has no users row.
        return True
    user = await storage.get_user_by_username(username)
    return bool(user and user["role"] == "admin")


async def _create_ai_auth_header(username: str, is_admin: bool = False) -> str:
    """Create a short-lived session token for AI internal API calls.

    Returns a Bearer auth header string, or empty string on failure/no user.
    """
    if not username:
        return ""
    try:
        if is_admin and not await _ai_admin_role_current(username):
            logger.info("AI admin session denied: user %s is no longer admin", username)
            return ""
        session_token = await storage.create_session(
            username,
            is_admin=is_admin,
            ttl_hours=_AI_SESSION_TTL_HOURS,
        )
        if is_admin and not await _ai_admin_role_current(username):
            await storage.delete_session(session_token)
            logger.info(
                "AI admin session discarded: user %s is no longer admin", username
            )
            return ""
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
    build_number: int | str | None = None,
    jenkins_url: str = "",
    job_name: str = "",
    source_warnings: list[str] | None = None,
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
        stamp_build_url(fail_data, jenkins_url)
    if source_warnings:
        fail_data["source_warnings"] = source_warnings
    await _preserve_request_params(job_id, fail_data)
    await update_status(job_id, "failed", fail_data)
    notify_active_count_changed()
    notify_dashboard_changed()
    notify_job_status_changed(job_id)
    return False


async def _carry_forward_overrides(job_id: str, result_data: dict[str, Any]) -> None:
    """Carry forward user classification overrides from previous jobs. Best-effort."""
    try:
        carried = await storage.carry_forward_user_overrides(job_id, result_data)
        if carried:
            logger.info(
                "Carried forward %d user classification override(s) for job_id=%s",
                carried,
                job_id,
            )
    except Exception:
        logger.warning(
            "Failed to carry forward user overrides for job_id=%s",
            job_id,
            exc_info=True,
        )


async def _resolve_settings_json_before_analysis(
    job_id: str,
    body: BaseAnalysisRequest,
    settings: Settings,
    ai_provider: str,
    ai_model: str,
    display_name: str,
    *,
    build_number: int | None = None,
    jenkins_url: str = "",
    job_name: str = "",
    is_admin: bool = False,
) -> tuple[EffectiveRepoAnalysisSettings, bool] | None:
    """Shallow-clone tests repo and resolve ``.rootcoz/settings.json`` once.

    Runs for every analysis that has a tests repo (not only when AI is deferred)
    so request → settings.json → server is applied before Jenkins wait / preflight.

    Returns:
        ``(effective, clone_ok)`` when the caller should continue.
        ``clone_ok`` is True when there was no tests repo, or the early clone
        succeeded (analysis need not re-merge). False means analysis must
        re-resolve after its full workspace clone.
        ``None`` when the job was already marked failed (caller must return).
    """

    async def _fail_job(error: str) -> None:
        fail_data: dict[str, Any] = {
            "job_name": job_name or display_name,
            "display_name": display_name,
            "error": error,
        }
        if build_number is not None:
            fail_data["build_number"] = build_number
        if jenkins_url:
            fail_data["jenkins_url"] = jenkins_url
        await _preserve_request_params(job_id, fail_data)
        await update_status(job_id, "failed", fail_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)

    tests_repo_url_raw = resolve_tests_repo_url(body, settings)
    peer_ai_configs = _resolve_peer_ai_configs(body, settings)
    additional_repos_list = resolve_additional_repos(body, settings)

    if not tests_repo_url_raw:
        effective = await resolve_repo_analysis_settings(
            job_id,
            None,
            body,
            settings,
            ai_provider=ai_provider,
            ai_model=ai_model,
            peer_ai_configs=peer_ai_configs,
            additional_repos=additional_repos_list,
        )
        if not effective.ai_provider or not effective.ai_model:
            await _fail_job(
                _ai_not_configured_message(None, "AI provider/model", is_admin=is_admin)
            )
            return None
        return effective, True

    clean_url, tests_ref = parse_repo_ref(tests_repo_url_raw)
    token = resolve_tests_repo_token(body, settings)

    logger.info(
        "Resolving .rootcoz/settings.json before analysis (job_id=%s)",
        job_id,
    )
    clone_ok = False
    with RepositoryManager() as repo_manager:
        workspace = repo_manager.create_workspace()
        target = workspace / "tests-repo-early"
        tests_path: Path | None = target
        try:
            await asyncio.to_thread(
                repo_manager.clone_into,
                clean_url,
                target,
                depth=1,
                branch=tests_ref,
                token=token or None,
            )
            clone_ok = True
        except (GitCommandError, OSError, ValueError) as exc:
            logger.warning(
                "Early settings.json clone failed (%s); "
                "continuing until analysis workspace clone",
                type(exc).__name__,
            )
            tests_path = None

        try:
            effective = await resolve_repo_analysis_settings(
                job_id,
                tests_path,
                body,
                settings,
                ai_provider=ai_provider,
                ai_model=ai_model,
                peer_ai_configs=peer_ai_configs,
                additional_repos=additional_repos_list,
            )
        except RootcozSettingsError as exc:
            logger.error("Invalid .rootcoz/settings.json (details redacted)")
            await _fail_job(str(exc))
            return None

    if not effective.ai_provider or not effective.ai_model:
        if not clone_ok:
            # Retry after full workspace clone in CI source analysis.
            return effective, False
        await _fail_job(
            _ai_not_configured_message(None, "AI provider/model", is_admin=is_admin)
        )
        return None

    logger.info(
        "Resolved analysis settings: %s/%s",
        effective.ai_provider,
        effective.ai_model,
    )
    return effective, clone_ok


def _build_base_request_params(
    ai_provider: str,
    ai_model: str,
    peer_ai_configs_resolved: list[Any] | None = None,
    *,
    tests_repo_url: str = "",
    tests_repo_token: str = "",
    tests_repo_ref: str = "",
    additional_repos: list[Any] | None = None,
) -> dict[str, Any]:
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
    params: dict[str, Any],
    body: BaseAnalysisRequest,
    merged: Settings,
) -> None:
    """Apply BaseAnalysisRequest overrides to a base params dict.

    Centralises the serialisation of per-request overrides that both the
    unified ``/analyze`` endpoint and the file/raw re-analyze path need.
    Mutates *params* in place.
    """
    params["force_server_credentials"] = merged.force_server_credentials
    params["enable_jira"] = _resolve_enable_jira(body, merged)
    # Jira credentials are Server Settings only — persist the effective
    # (server) values so stored request_params replay exactly what the
    # analysis ran with; requests cannot supply or override them.
    params["jira_email"] = merged.jira_email or ""
    params["jira_api_token"] = (
        merged.jira_api_token.get_secret_value() if merged.jira_api_token else ""
    )
    params["jira_pat"] = merged.jira_pat.get_secret_value() if merged.jira_pat else ""
    params["jira_oauth_client_secret"] = (
        merged.jira_oauth_client_secret.get_secret_value()
        if merged.jira_oauth_client_secret
        else ""
    )
    params["jira_atst_token"] = (
        merged.jira_atst_token.get_secret_value() if merged.jira_atst_token else ""
    )
    params["atlassian_oauth_cloud_id"] = (
        body.atlassian_oauth_cloud_id
        if body.atlassian_oauth_cloud_id is not None
        else (merged.atlassian_oauth_cloud_id or "")
    )
    params["jira_site_url"] = (
        body.jira_site_url
        if body.jira_site_url is not None
        else (merged.jira_site_url or "")
    )
    params["jira_project_key"] = (
        body.jira_project_key
        if body.jira_project_key is not None
        else (merged.jira_project_key or "")
    )
    params["jira_max_results"] = (
        body.jira_max_results
        if body.jira_max_results is not None
        else merged.jira_max_results
    )
    # Only persist explicitly submitted tokens — never the server deployment
    # default. Chat sessions must not inherit GITHUB_TOKEN from job params.
    params["github_token"] = body.github_token if body.github_token is not None else ""
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
    # Persist labels for resume/re-analyze round-trips
    if body.labels:
        params["labels"] = body.labels


def _stamp_reanalysis_metadata(
    request_params: dict[str, Any],
    reanalyzed_from_job_id: str,
    reanalyzed_from_job_name: str,
) -> None:
    """Write re-analysis origin fields into *request_params* when present."""
    if reanalyzed_from_job_id:
        request_params["reanalyzed_from_job_id"] = reanalyzed_from_job_id
        if reanalyzed_from_job_name:
            request_params["reanalyzed_from_job_name"] = reanalyzed_from_job_name


def _append_reanalysis_forward_link(
    result_data: dict[str, Any], new_job_id: str
) -> None:
    """Record *new_job_id* in the original job's ``reanalyzed_to_job_ids``."""
    params = result_data.get("request_params")
    if not new_job_id or not isinstance(params, dict):
        return
    links = params.get("reanalyzed_to_job_ids") or []
    if not isinstance(links, list):
        links = []
    if new_job_id not in links:
        links.append(new_job_id)
    params["reanalyzed_to_job_ids"] = links


def _remove_reanalysis_forward_link(result_data: dict[str, Any], job_id: str) -> None:
    """Drop *job_id* from the original job's ``reanalyzed_to_job_ids``."""
    params = result_data.get("request_params")
    if not isinstance(params, dict):
        return
    links = params.get("reanalyzed_to_job_ids")
    if isinstance(links, list) and job_id in links:
        params["reanalyzed_to_job_ids"] = [j for j in links if j != job_id]


def _make_unlinker(job_id: str) -> Callable[[dict[str, Any]], None]:
    """Return a ``patch_result_json`` callback unlinking *job_id*.

    A factory rather than an inline lambda: the value is bound instead of the
    loop variable (ruff B023) and the callback type is inferable by mypy.
    """

    def _unlink(data: dict[str, Any]) -> None:
        _remove_reanalysis_forward_link(data, job_id)

    return _unlink


async def _reanalysis_origins(job_ids: Iterable[str]) -> dict[str, str]:
    """Map each re-analysis job id to the origin job it points back to.

    Read before the rows are deleted.  A failed lookup is logged, never
    raised — the deletion itself must still succeed.
    """
    origins: dict[str, str] = {}
    for job_id in job_ids:
        origin_id = ""
        try:
            stored = await storage.get_result(job_id)
            params = ((stored or {}).get("result") or {}).get("request_params") or {}
            origin_id = str(params.get("reanalyzed_from_job_id") or "")
        except Exception:
            logger.warning(
                f"Failed to look up the origin of deleted job {job_id}",
                exc_info=True,
            )
        if origin_id:
            origins[job_id] = origin_id
    return origins


async def _unlink_reanalysis_origins(origins: Mapping[str, str]) -> None:
    """Remove the deleted job ids in *origins* from their origins' links.

    Called *after* the rows are deleted so a link committed while the deletion
    was in flight is unlinked too, and a link created afterwards is skipped by
    the job-existence check on the enqueue patch.  A failed patch is logged,
    never raised — the deletion itself must still succeed.
    """
    for job_id, origin_id in origins.items():
        try:
            await patch_result_json(origin_id, _make_unlinker(job_id))
            notify_job_status_changed(origin_id)
        except Exception:
            logger.warning(
                f"Failed to remove deleted re-analysis {job_id} from origin {origin_id}",
                exc_info=True,
            )


def _ensure_submitter_tag(tags: list[str] | None, username: str) -> list[str]:
    """Return *tags* with *username* included (lowercased, deduplicated)."""
    result = list(tags) if tags else []
    normalized = username.strip().lower()
    if not normalized or normalized in _SYSTEM_TAGS:
        return result
    # If any existing tag matches case-insensitively, keep list as-is
    if any(isinstance(t, str) and t.lower() == normalized for t in result):
        return result
    result.append(normalized)
    return result


def _strip_old_submitter_tag(tags: list[str], result_data: dict[str, Any]) -> list[str]:
    """Remove the old submitter's username tag so re-analyze adds only the new one."""
    old_submitter = (result_data.get("request_params") or {}).get("submitted_by", "")
    if not old_submitter:
        return tags
    old_normalized = old_submitter.strip().lower()
    if old_normalized in _SYSTEM_TAGS:
        return tags
    return [t for t in tags if not (isinstance(t, str) and t.lower() == old_normalized)]


def _analysis_state_for_ingest(
    ingest_only: bool,
) -> Literal["submitted", "analyzed"]:
    return ANALYSIS_STATE_SUBMITTED if ingest_only else ANALYSIS_STATE_ANALYZED


async def _finish_ci_ingest(
    *,
    job_id: str,
    display_name: str,
    source_result: CISourceResult,
    extra_labels: list[str] | None,
    metadata_job_name: str,
    metadata_rules: Any,
    ingest_only: bool,
    source: CISource | None,
) -> None:
    """Persist fetched CI results and stop before AI when ingest_only."""
    _all_entries = source_result.test_entry_dicts()
    passed_count, skipped_count, failed_count = source_result.test_counts()
    analysis_state = _analysis_state_for_ingest(ingest_only)
    if ingest_only:
        summary = INGEST_COMPLETE_SUMMARY
        failures = source_result.unanalyzed_failure_analyses()
    else:
        summary = "No test failures found in the provided input."
        failures = []
    analysis_result = FailureAnalysisResult(
        job_id=job_id,
        status="completed",
        summary=summary,
        enriched_xml=getattr(source, "raw_xml", None),
        passed_count=passed_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        failures=failures,
        analysis_state=analysis_state,
    )
    result_data = analysis_result.model_dump(mode="json")
    result_data["job_name"] = display_name
    _stamp_result_metadata(result_data, source_result)
    child_job_analyses: list[Any] = []
    child_test_scopes: list[tuple[str, int, list[dict[str, Any]]]] = []
    if ingest_only and source is not None and source_result.child_job_infos:
        child_job_analyses, child_test_scopes = await source.analyze_children(
            source_result,
            settings=getattr(source, "settings", None),
            ingest_only=True,
        )
        if child_job_analyses:
            result_data["child_job_analyses"] = [
                c.model_dump(mode="json") for c in child_job_analyses
            ]
        _apply_cached_test_counts(result_data, _all_entries, child_test_scopes)
    await replace_job_test_entries(job_id, _all_entries, child_test_scopes)
    await _preserve_request_params(job_id, result_data)
    logger.info(
        "Job %s: ingest complete (%s), %d failed, %d passed, %d skipped",
        job_id,
        analysis_state,
        result_data.get("failed_count", failed_count),
        result_data.get("passed_count", passed_count),
        result_data.get("skipped_count", skipped_count),
    )
    await update_status(job_id, "completed", result_data)
    notify_active_count_changed()
    notify_dashboard_changed()
    notify_job_status_changed(job_id)
    await _auto_assign_metadata(
        metadata_job_name,
        metadata_rules,
        extra_labels=extra_labels,
    )


async def _enqueue_ci_source_analysis(
    body: UnifiedAnalyzeRequest,
    merged: Settings,
    resolved_peers: list[Any] | None,
    display_name: str,
    analysis_type: str,
    base_url: str,
    username: str,
    *,
    tags: list[str] | None = None,
    message_prefix: str = "Analysis",
    reanalyzed_from_job_id: str = "",
    reanalyzed_from_job_name: str = "",
    is_admin: bool = False,
    ingest_only: bool = False,
    existing_job_id: str = "",
) -> dict[str, Any]:
    """Build params, persist initial state, spawn task, and return response.

    Shared enqueue path for all CISource plugins (file/raw/prow/jenkins).

    Args:
        body: The analysis request (``UnifiedAnalyzeRequest`` or similar).
        merged: Merged settings.
        resolved_peers: Validated peer AI configs.
        display_name: Human-readable job name.
        analysis_type: Source type key (``"file"``, ``"raw"``, ``"prow"``,
            ``"jenkins"``).
        base_url: Server base URL for result links.
        username: Authenticated user who submitted the request.
        tags: Optional pre-built tags list. When ``None``, uses ``body.tags``.
        message_prefix: Prefix for the queued-message ("Analysis" or
            "Re-analysis").

    Returns:
        JSON-serialisable response dict with ``status``, ``job_id``, links.
    """
    persist_merged = merged
    if ingest_only:
        ai_provider, ai_model = "", ""
        # Skip artifact download during ingest only; keep the caller's artifact
        # preference in stored request_params for a later in-place analyze.
        merged = merged.model_copy(update={"get_job_artifacts": False})
        resolved_peers = None
    else:
        token = ai_username.set(username)
        try:
            ai_provider, ai_model = await _resolve_ai_config_allow_defer(
                body, persist_merged, request=None
            )
        finally:
            ai_username.reset(token)

    # Resolve repos
    tests_repo_url_raw = resolve_tests_repo_url(body, persist_merged)
    tests_repo_url, tests_repo_ref = parse_repo_ref(tests_repo_url_raw)
    resolved_tests_repo_token = (
        resolve_tests_repo_token(body, persist_merged) if tests_repo_url else ""
    )
    additional_repos_list = resolve_additional_repos(body, persist_merged)

    job_id = existing_job_id or str(uuid.uuid4())
    job_id_var.set(job_id)

    # Append short job_id suffix to generic fallback names for uniqueness
    _GENERIC_FALLBACK_NAMES = {
        "file-analysis",
        "raw-analysis",
        "prow-analysis",
        "jenkins-analysis",
        "prow-re-analysis",
        "file-re-analysis",
        "raw-re-analysis",
        "jenkins-re-analysis",
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
    _apply_base_analysis_overrides(base_params, body, persist_merged)

    source_cls = CI_SOURCE_REGISTRY.get(analysis_type)
    if source_cls is None:
        raise HTTPException(
            status_code=422, detail=f"Unsupported analysis type: {analysis_type}"
        )
    try:
        source_cls.validate_request(body, persist_merged)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    source_cls.build_request_params(body, persist_merged, base_params)

    initial_result: dict[str, Any] = {
        "job_name": display_name,
        "display_name": display_name,
        "request_params": encrypt_sensitive_fields(base_params),
    }
    # Persist real identity for history matching and auto-review
    initial_result.update(source_cls.pre_persist_identity_from_request(body))
    prior: dict[str, Any] | None = None
    if existing_job_id:
        stored = await get_result(existing_job_id, strip_sensitive=False)
        prior_raw = stored.get("result") if stored else None
        if isinstance(prior_raw, dict):
            prior = prior_raw
    prior_submitted = ""
    if prior:
        prior_submitted = str(
            (prior.get("request_params") or {}).get("submitted_by") or ""
        ).strip()
    submitter = prior_submitted or username
    initial_result["request_params"]["submitted_by"] = submitter
    _stamp_reanalysis_metadata(
        initial_result["request_params"],
        reanalyzed_from_job_id,
        reanalyzed_from_job_name,
    )
    effective_tags = tags if tags is not None else (body.tags or None)
    initial_result["tags"] = _ensure_submitter_tag(effective_tags, submitter)
    initial_result["analysis_state"] = _analysis_state_for_ingest(ingest_only)
    if ingest_only:
        initial_result["analyzed_by"] = ""
        initial_result["analyzed_at"] = ""
    elif existing_job_id:
        initial_result["analyzed_by"] = username
        initial_result["analyzed_at"] = datetime.now(UTC).isoformat()
    initial_status = source_cls.initial_status(body, persist_merged)
    # Prefer a known build URL at enqueue time (e.g. Jenkins waiting jobs).
    initial_build_url = source_cls.pre_enqueue_build_url(body, persist_merged)
    if prior:
        merged_result = dict(prior)
        for key, value in initial_result.items():
            if key == "failures" and not value:
                continue
            merged_result[key] = value
        initial_result = merged_result
        # request_params was rebuilt above, so carry the recorded re-analysis
        # links of the job analyzed in place over to the new params.
        prior_links = (prior.get("request_params") or {}).get("reanalyzed_to_job_ids")
        if isinstance(prior_links, list) and prior_links:
            initial_result["request_params"]["reanalyzed_to_job_ids"] = list(
                prior_links
            )
    await save_result(job_id, initial_build_url, initial_status, initial_result)
    notify_active_count_changed()
    notify_dashboard_changed()

    # Spawn background task
    task = asyncio.create_task(
        _process_ci_source_analysis(
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
            is_admin=is_admin,
            ingest_only=ingest_only,
        )
    )
    _register_job_task(job_id, task)

    if reanalyzed_from_job_id:
        # Point the original job at this re-analysis (accumulating list) and
        # wake its open report.  The patch is conditional on this job still
        # existing, so a concurrent deletion can never be left with a dead link.
        # The job is already running, so a failed patch is logged rather than
        # raised — it must not orphan the new job.
        try:
            await patch_result_json(
                reanalyzed_from_job_id,
                lambda data: _append_reanalysis_forward_link(data, job_id),
                require_job_id=job_id,
            )
            notify_job_status_changed(reanalyzed_from_job_id)
        except Exception:
            logger.warning(
                f"Failed to record re-analysis {job_id} on origin "
                f"{reanalyzed_from_job_id}",
                exc_info=True,
            )

    response: dict[str, Any] = {
        "status": "queued",
        "job_id": job_id,
        "message": f"{message_prefix} job queued. Poll /results/{job_id} for status.",
    }
    return _attach_result_links(response, base_url, job_id)


# Keys that CISourceResult.identity is allowed to override on result dicts.
_ALLOWED_IDENTITY_KEYS = {"job_name", "build_number", "build_id"}


def _stamp_source_identity(
    data: dict[str, Any], source_result: CISourceResult | None
) -> None:
    """Apply source identity overrides (e.g. Prow job_name/build_number)."""
    if source_result is not None and source_result.identity:
        for key, value in source_result.identity.items():
            if key in _ALLOWED_IDENTITY_KEYS:
                data[key] = value


def _stamp_source_warnings(
    data: dict[str, Any], source_result: CISourceResult | None
) -> None:
    """Attach source warnings (GCS errors, oversize artifacts) to result."""
    if source_result is not None and source_result.warnings:
        data["source_warnings"] = source_result.warnings


def _stamp_source_metadata(
    data: dict[str, Any], source_result: CISourceResult | None
) -> None:
    """Attach Prow job metadata from the source plugin when available."""
    if source_result is not None and source_result.source_metadata:
        data["source_metadata"] = source_result.source_metadata


def _stamp_result_metadata(
    data: dict[str, Any], source_result: CISourceResult | None
) -> None:
    """Apply source identity, warnings, metadata, and build URL to result dict."""
    _stamp_source_identity(data, source_result)
    _stamp_source_warnings(data, source_result)
    _stamp_source_metadata(data, source_result)
    if source_result is not None and source_result.build_url:
        stamp_build_url(data, source_result.build_url)


def _count_test_entry_statuses(entries: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Count passed/skipped/failed in normalized test entry dicts."""
    passed = skipped = failed = 0
    for entry in entries:
        status = entry.get("status")
        if status == "passed":
            passed += 1
        elif status == "skipped":
            skipped += 1
        elif status == "failed":
            failed += 1
    return passed, skipped, failed


def _apply_cached_test_counts(
    result_data: dict[str, Any],
    top_entries: list[dict[str, Any]],
    child_scopes: list[tuple[str, int, list[dict[str, Any]]]],
) -> None:
    """Cache job-level passed/skipped/failed counts on ``result_data``."""
    passed, skipped, failed = _count_test_entry_statuses(top_entries)
    for _, _, entries in child_scopes:
        c_passed, c_skipped, c_failed = _count_test_entry_statuses(entries)
        passed += c_passed
        skipped += c_skipped
        failed += c_failed
    result_data["passed_count"] = passed
    result_data["skipped_count"] = skipped
    result_data["failed_count"] = failed


async def _persist_test_entries_and_cache_counts(
    job_id: str,
    source_result: CISourceResult | None,
    child_test_scopes: list[tuple[str, int, list[dict[str, Any]]]] | None,
    result_data: dict[str, Any],
) -> None:
    """Persist test outcomes and cache counts BEFORE update_status.

    Used by every early-exit completion path so the stored result carries
    the same cached counts as the normal completion path. On persist
    failure, counts are NOT cached — the stored result must never claim
    test outcomes the DB does not have.
    """
    all_test_entries = (
        source_result.test_entry_dicts() if source_result is not None else []
    )
    scopes = child_test_scopes or []
    try:
        await replace_job_test_entries(job_id, all_test_entries, scopes)
    except Exception:
        logger.warning(
            "Failed to persist test entries for job_id=%s",
            job_id,
            exc_info=True,
        )
    else:
        _apply_cached_test_counts(result_data, all_test_entries, scopes)


async def _run_per_group_analysis(
    *,
    groups: dict[str, list[Any]],
    console_context: str,
    repo_path: Path | None,
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None,
    custom_prompt: str,
    artifacts_context: str,
    server_url: str,
    job_id: str,
    additional_repos: dict[str, Path] | None,
    max_concurrent_ai_calls: int,
    auth_header: str,
    peer_ai_configs: list[Any] | None = None,
    peer_analysis_max_rounds: int = 3,
) -> list[Any]:
    """Run per-group analysis with parallel execution.

    Shared by the peer analysis path and the orchestrator fallback path.
    Creates one ``analyze_failure_group`` coroutine per failure group and
    runs them in parallel via ``run_parallel_with_limit``.

    Args:
        groups: Failure groups keyed by error signature.
        Other args: Forwarded to ``analyze_failure_group``.

    Returns:
        Flat list of FailureAnalysis objects from all groups.
    """

    async def _analyze_group(group_failures: list[Any], number: int) -> list[Any]:
        await safe_update_progress(
            job_id, f"analyzing_failures (group {number}/{len(groups)})"
        )
        logger.info(
            "Analyzing fallback group %d/%d for job=%s", number, len(groups), job_id
        )
        return await analyze_failure_group(
            failures=group_failures,
            console_context=console_context,
            repo_path=repo_path,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=ai_call_timeout,
            custom_prompt=custom_prompt,
            artifacts_context=artifacts_context,
            server_url=server_url,
            job_id=job_id,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=peer_analysis_max_rounds,
            additional_repos=additional_repos,
            max_concurrent_ai_calls=max_concurrent_ai_calls,
            auth_header=auth_header,
            all_groups=groups,
        )

    coroutines: list[Coroutine[Any, Any, Any]] = [
        _analyze_group(group_failures, number)
        for number, group_failures in enumerate(groups.values(), 1)
    ]

    results = await run_parallel_with_limit(
        coroutines, max_concurrency=max_concurrent_ai_calls
    )
    logger.debug(
        "AI analysis complete: %d results from %d groups",
        len(results),
        len(groups),
    )

    from rootcoz.engine.chat import safe_exception_frames

    all_analyses: list[FailureAnalysis] = []
    for (signature, failures), result in zip(groups.items(), results):
        if isinstance(result, Exception):
            logger.error(
                "Failed to analyze fallback group %s: %s\n%s",
                signature,
                type(result).__name__,
                safe_exception_frames(result),
            )
            all_analyses.extend(_failed_group_analyses(signature, failures))
        elif result:
            all_analyses.extend(result)
        else:
            logger.warning("Fallback group %s returned no analyses", signature)
            all_analyses.extend(_failed_group_analyses(signature, failures))
    return all_analyses


async def _analyze_failures_or_exit(
    *,
    job_id: str,
    test_failures: list[Any],
    console_context: str,
    artifacts_context: str,
    metadata_job_name: str,
    display_name: str,
    repo_path: Path | None,
    ai_provider: str,
    ai_model: str,
    merged: Settings,
    custom_prompt: str,
    server_url: str,
    peer_ai_configs: list[Any] | None,
    cloned_repos: dict[str, Any],
    auth_header: str,
    groups: dict[str, list[Any]],
    source_result: CISourceResult | None,
    child_job_analyses: list[ChildJobAnalysis] | None = None,
    child_test_scopes: list[tuple[str, int, list[dict[str, Any]]]] | None = None,
    extra_labels: list[str] | None = None,
) -> tuple[list[Any], list[Any], int, list[CrossFailurePattern]] | None:
    """Resolve console-only / no-failure / junit analysis paths.

    Returns ``(all_analyses, test_failures, unique_errors, cross_failure_patterns)``,
    or ``None`` when an early-exit path already updated job status.
    """
    # Console-only analysis when no JUnit failures found but console
    # context exists (e.g. Prow build with no JUnit artifacts)
    if not test_failures and console_context:
        success, console_results, error_text = await run_console_only_analysis(
            test_name=metadata_job_name,
            console_context=console_context,
            artifacts_context=artifacts_context,
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
            call_type="console",
        )
        if not success:
            logger.error("Console-only analysis failed: %s", error_text)
            fail_result = FailureAnalysisResult(
                job_id=job_id,
                status="failed",
                summary=make_user_friendly_error(error_text),
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            fail_data = fail_result.model_dump(mode="json")
            fail_data["error"] = fail_result.summary
            fail_data["job_name"] = display_name
            _stamp_result_metadata(fail_data, source_result)
            await _preserve_request_params(job_id, fail_data)
            await _attach_token_usage(job_id, fail_data)
            await update_status(job_id, "failed", fail_data)
            notify_active_count_changed()
            notify_dashboard_changed()
            notify_job_status_changed(job_id)
            notify_token_usage_changed()
            return None

        all_analyses = list(console_results)
        synthetic_failure = FailedTest(
            test_name=metadata_job_name,
            error_message=console_context,
        )
        return all_analyses, [synthetic_failure], 1, []

    if not test_failures:
        if source_result is not None and source_result.warnings and not console_context:
            # All data sources errored — report as failed, not clean
            analysis_result = FailureAnalysisResult(
                job_id=job_id,
                status="failed",
                summary=f"Could not fetch test data: {'; '.join(source_result.warnings)}",
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            result_data = analysis_result.model_dump(mode="json")
            result_data["error"] = analysis_result.summary
            result_data["job_name"] = display_name
            _stamp_result_metadata(result_data, source_result)
            await _preserve_request_params(job_id, result_data)
            await update_status(job_id, "failed", result_data)
            notify_active_count_changed()
            notify_dashboard_changed()
            notify_job_status_changed(job_id)
            return None

        # No failures and no console context — nothing to analyze
        analysis_result = FailureAnalysisResult(
            job_id=job_id,
            status="completed",
            summary="No test failures found and no console output to analyze.",
        )
        result_data = analysis_result.model_dump(mode="json")
        result_data["job_name"] = display_name
        _stamp_result_metadata(result_data, source_result)
        await _preserve_request_params(job_id, result_data)
        # Persist test outcomes and cache counts so /full and the dashboard
        # report real numbers instead of zeros on this no-failure path.
        await _persist_test_entries_and_cache_counts(
            job_id, source_result, child_test_scopes, result_data
        )
        await update_status(job_id, "completed", result_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        await _auto_assign_metadata(
            metadata_job_name, merged.metadata_rules, extra_labels=extra_labels
        )
        return None

    # Normal path: structured test failures
    logger.info(
        f"Starting AI analysis for {len(groups)} failure groups (provider={ai_provider}, model={ai_model})"
    )

    cross_failure_patterns: list[CrossFailurePattern] = []

    try:
        all_analyses, cross_failure_patterns = await run_orchestrated_analysis(
            groups=groups,
            console_context=console_context,
            repo_path=repo_path,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=merged.ai_call_timeout,
            custom_prompt=custom_prompt,
            artifacts_context=artifacts_context,
            server_url=server_url,
            job_id=job_id,
            additional_repos=cloned_repos or None,
            auth_header=auth_header,
            max_concurrent_ai_calls=merged.max_concurrent_ai_calls,
            peer_ai_configs=peer_ai_configs,
            peer_analysis_max_rounds=merged.peer_analysis_max_rounds,
        )
    except (RuntimeError, ValueError, OSError, TypeError) as exc:
        from rootcoz.engine.chat import safe_exception_frames

        logger.warning(
            "Orchestrated analysis failed for job_id=%s; falling back to per-group analysis (%s)\n%s",
            job_id,
            type(exc).__name__,
            safe_exception_frames(exc),
        )
        logger.debug("Retrying analysis per group for job_id=%s", job_id)
        try:
            all_analyses = await _run_per_group_analysis(
                groups=groups,
                console_context=console_context,
                repo_path=repo_path,
                ai_provider=ai_provider,
                ai_model=ai_model,
                ai_call_timeout=merged.ai_call_timeout,
                custom_prompt=custom_prompt,
                artifacts_context=artifacts_context,
                server_url=server_url,
                job_id=job_id,
                peer_ai_configs=peer_ai_configs,
                peer_analysis_max_rounds=merged.peer_analysis_max_rounds,
                additional_repos=cloned_repos or None,
                max_concurrent_ai_calls=merged.max_concurrent_ai_calls,
                auth_header=auth_header,
            )
        except (RuntimeError, ValueError, OSError, TypeError) as fallback_exc:
            logger.error(
                "Fallback per-group analysis failed for job_id=%s (%s)\n%s",
                job_id,
                type(fallback_exc).__name__,
                safe_exception_frames(fallback_exc),
            )
            all_analyses = [
                analysis
                for signature, failures in groups.items()
                for analysis in _failed_group_analyses(signature, failures)
            ]

    unique_errors = len(groups)

    # Placeholders keep failed tests visible but are not successful analyses.
    from rootcoz.sources.jenkins_source import child_has_successful_analysis

    if not any(not _is_failed_analysis(a) for a in all_analyses) and not any(
        child_has_successful_analysis(c) for c in (child_job_analyses or [])
    ):
        error_msg = (
            f"All failure group(s) failed during analysis "
            f"({len(test_failures)} test failures, {unique_errors} unique errors)"
        )
        logger.error(f"Analysis fully failed for job_id={job_id}: {error_msg}")
        fail_result = FailureAnalysisResult(
            job_id=job_id,
            status="failed",
            summary=error_msg,
            ai_provider=ai_provider,
            ai_model=ai_model,
            failures=all_analyses,
        )
        fail_data = fail_result.model_dump(mode="json")
        fail_data["error"] = fail_result.summary
        fail_data["job_name"] = display_name
        if child_job_analyses:
            fail_data["child_job_analyses"] = [
                c.model_dump(mode="json") for c in child_job_analyses
            ]
        fail_data["failed_analysis_groups"] = len(groups) + _count_failed_child_groups(
            child_job_analyses or []
        )
        _stamp_result_metadata(fail_data, source_result)
        await _preserve_request_params(job_id, fail_data)
        await _attach_token_usage(job_id, fail_data)
        # Persist test outcomes and cache counts BEFORE update_status, so the
        # stored result carries them. Mirrors the normal completion path.
        await _persist_test_entries_and_cache_counts(
            job_id, source_result, child_test_scopes, fail_data
        )
        await update_status(job_id, "failed", fail_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        notify_token_usage_changed()
        return None

    return all_analyses, test_failures, unique_errors, cross_failure_patterns


def _is_failed_analysis(failure: FailureAnalysis) -> bool:
    """Identify the placeholder produced when a failure group cannot be analyzed."""
    return (
        failure.analysis.details == "Analysis failed; check server logs for details"
        and not failure.analysis.classification
    )


def _count_failed_child_groups(children: list[ChildJobAnalysis]) -> int:
    """Count failed signatures within each child build, including nested builds."""
    total = 0
    for child in children:
        signatures = {
            storage.resolve_signature(failure)
            for failure in child.failures
            if _is_failed_analysis(failure)
        }
        total += len(signatures)
        if child.all_groups_failed and not child.failures:
            total += 1  # Console-only child analysis has no failure signature.
        total += _count_failed_child_groups(child.failed_children)
    return total


async def _process_ci_source_analysis(
    *,
    job_id: str,
    body: UnifiedAnalyzeRequest,
    merged: Settings,
    display_name: str,
    ai_provider: str,
    ai_model: str,
    peer_ai_configs: list[Any] | None,
    tests_repo_url: str,
    tests_repo_ref: str,
    resolved_tests_repo_token: str,
    additional_repos_list: list[Any],
    base_url: str,
    username: str = "",
    is_admin: bool = False,
    ingest_only: bool = False,
) -> None:
    """Background task for CISource plugin analysis (file/raw/prow/jenkins)."""
    job_id_var.set(job_id)
    ai_username.set(username)
    force_server_credentials.set(merged.force_server_credentials)

    auth_header = ""
    repo_manager: RepositoryManager | None = None
    repo_path: Path | None = None
    source: CISource | None = None  # set after source creation; used by cleanup
    source_result = None  # set after source.fetch(); used by _stamp_source_warnings
    metadata_job_name = display_name  # updated from source_result.identity after fetch
    extra_labels = body.labels or None

    try:
        logger.info(
            f"Starting {body.type} analysis for job_id={job_id}, display_name={display_name}"
        )

        # Create source plugin via registry
        source = create_source_from_request(body.type, body, merged)

        # Pre-fetch hook (e.g. wait for Jenkins build completion)
        if source.requires_pre_fetch():
            await update_status(job_id, "waiting")
            notify_active_count_changed()
            notify_dashboard_changed()
            notify_job_status_changed(job_id)
            await safe_update_progress(job_id, source.pre_fetch_phase())
            notify_job_status_changed(job_id)

            pre_fetch_error = await source.pre_fetch(job_id)
            if pre_fetch_error:
                fail_data = {
                    "job_name": display_name,
                    "display_name": display_name,
                    "error": pre_fetch_error,
                    "analysis_state": _analysis_state_for_ingest(ingest_only),
                }
                _stamp_result_metadata(fail_data, None)
                await _preserve_request_params(job_id, fail_data)
                await update_status(job_id, "failed", fail_data)
                notify_active_count_changed()
                notify_dashboard_changed()
                notify_job_status_changed(job_id)
                return

        # Fetch failures from source. Waiting jobs switch only after pre-fetch completes.
        await update_status(job_id, "running")
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        await safe_update_progress(job_id, "fetching")
        source_result = await source.fetch()
        logger.debug(
            f"Source fetch complete: {len(source_result.failures)} failures, "
            f"{len(source_result.passed_tests)} passed, {len(source_result.skipped_tests)} skipped, "
            f"skip_analysis={source_result.skip_analysis}"
        )

        # Use source-provided job identity for metadata matching
        identity_job = source_result.identity.get("job_name")
        if identity_job:
            metadata_job_name = identity_job

        # Log and track source warnings (e.g. GCS access errors, oversize artifacts)
        for warning in source_result.warnings:
            logger.warning("Source warning for job %s: %s", job_id, warning)

        # Persist build URL to DB column (available after source fetch)
        if source_result.build_url:
            await storage.update_build_url(job_id, source_result.build_url)

        await source.persist_fetch_metadata(job_id, source_result)

        if ingest_only or source_result.skip_analysis:
            await _finish_ci_ingest(
                job_id=job_id,
                display_name=display_name,
                source_result=source_result,
                extra_labels=extra_labels,
                metadata_job_name=metadata_job_name,
                metadata_rules=merged.metadata_rules,
                ingest_only=ingest_only,
                source=source,
            )
            return

        test_failures = source_result.failures
        console_context = source_result.console_context

        await update_status(job_id, "running")
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)

        # Pre-flight: verify AI is reachable before spawning parallel tasks
        _preflight_build_number: int | str | None = source_result.identity.get(
            "build_number"
        )
        if not await _preflight_sidecar_check(
            job_id,
            ai_provider,
            ai_model,
            display_name,
            job_name=metadata_job_name,
            build_number=_preflight_build_number,
            jenkins_url=source_result.build_url or "",
            source_warnings=source_result.warnings or None,
        ):
            return

        auth_header = await _create_ai_auth_header(username)

        # Group failures by error signature
        groups: dict[str, list[Any]] = defaultdict(list)
        for failure in test_failures:
            sig = get_failure_signature(failure)
            groups[sig].append(failure)

        logger.info(
            f"Grouped {len(test_failures)} failures into {len(groups)} unique error signatures (job_id: {job_id})"
        )
        logger.debug(
            f"Failure grouping complete: {len(groups)} unique signatures from {len(test_failures)} failures"
        )

        # Track whether AI was already resolved (settings.json cannot change it).
        # Post-overlay preflight below runs only when this is False.
        ai_resolved_before_overlay = bool(ai_provider and ai_model)

        # Clone repos, copy resources, link artifacts
        repo_manager = RepositoryManager()
        ws_result, artifacts_context = await setup_analysis_workspace(
            repo_manager,
            tests_repo_url=str(tests_repo_url) if tests_repo_url else "",
            tests_repo_ref=tests_repo_ref,
            tests_repo_token=resolved_tests_repo_token or "",
            additional_repos=additional_repos_list or None,
            extract_path=source_result.extract_path,
            artifacts_context=source_result.artifacts_context,
            job_id=job_id,
        )
        repo_path = ws_result.repo_path
        cloned_repos = ws_result.cloned_repos

        async def _fail_settings_overlay(error: str) -> None:
            fail_data = {
                "job_name": display_name,
                "display_name": display_name,
                "error": error,
            }
            await _preserve_request_params(job_id, fail_data)
            await update_status(job_id, "failed", fail_data)
            notify_active_count_changed()
            notify_dashboard_changed()
            notify_job_status_changed(job_id)

        # Derive tests_cloned_path from workspace result (already cloned
        # by setup_analysis_workspace above — no second clone needed).
        tests_cloned_path: Path | None = None
        if tests_repo_url:
            repo_name = derive_test_repo_name(
                str(tests_repo_url), additional_repos_list
            )
            tests_cloned_path = cloned_repos.get(repo_name)

        try:
            effective = await resolve_repo_analysis_settings(
                job_id,
                tests_cloned_path,
                body,
                merged,
                ai_provider=ai_provider,
                ai_model=ai_model,
                peer_ai_configs=peer_ai_configs,
                additional_repos=additional_repos_list,
            )
        except RootcozSettingsError as exc:
            logger.error("Invalid .rootcoz/settings.json (details redacted)")
            await _fail_settings_overlay(str(exc))
            return

        ai_provider = effective.ai_provider
        ai_model = effective.ai_model
        peer_ai_configs = effective.peer_ai_configs
        additional_repos_list = effective.additional_repos
        merged = effective.settings
        force_server_credentials.set(merged.force_server_credentials)

        tests_dir_name = (
            tests_cloned_path.name if tests_cloned_path is not None else None
        )
        try:
            assert_no_tests_repo_name_collision(tests_dir_name, additional_repos_list)
        except RootcozSettingsError as exc:
            logger.error(
                "Invalid .rootcoz/settings.json after overlay (details redacted)"
            )
            await _fail_settings_overlay(str(exc))
            return

        if not ai_provider or not ai_model:
            await _fail_settings_overlay(
                _ai_not_configured_message(None, "AI provider/model", is_admin=is_admin)
            )
            return

        ai_provider, ai_model = await _validate_catalog_pair(ai_provider, ai_model)
        for peer in peer_ai_configs or []:
            await _validate_catalog_pair(*_peer_catalog_pair(peer))

        # Post-overlay preflight only when AI was deferred / changed by settings.json
        if not ai_resolved_before_overlay and not await _preflight_sidecar_check(
            job_id,
            ai_provider,
            ai_model,
            display_name,
            job_name=body.job_name or "",
        ):
            return

        if additional_repos_list:
            additional_repos_cloned, repo_path = await clone_additional_repos(
                repo_manager,
                additional_repos_list,
                repo_path,
                job_id=job_id,
            )
            cloned_repos.update(additional_repos_cloned)

        await safe_update_progress(job_id, "analyzing")

        # Copy .rootcoz/{agents,skills,extensions}/ to workspace .pi/
        if cloned_repos:
            copy_rootcoz_pi_resources(cloned_repos, repo_path)

        custom_prompt = append_repo_context(
            (body.raw_prompt or "").strip(), ws_result.repo_context
        )
        server_url = _build_internal_server_url()

        # Write source-specific workspace context files via the plugin hook.
        if source is not None and repo_path:
            try:
                github_token = (
                    merged.github_token.get_secret_value()
                    if merged.github_token
                    else ""
                )
                custom_prompt = await apply_source_workspace_files(
                    source,
                    repo_path,
                    custom_prompt,
                    job_id,
                    github_token=github_token,
                )
            except Exception:
                logger.warning(
                    "Failed to prepare workspace files for job %s",
                    job_id,
                    exc_info=True,
                )

        # Handle child jobs (Jenkins pipeline sub-jobs)
        from rootcoz.sources.jenkins_source import child_has_successful_analysis

        child_job_analyses: list[ChildJobAnalysis] = []
        child_test_scopes: list[tuple[str, int, list[dict[str, Any]]]] = []
        if source_result.child_job_infos and source is not None:
            await safe_update_progress(job_id, "analyzing_child_jobs")
            notify_job_status_changed(job_id)
            child_job_analyses, child_test_scopes = await source.analyze_children(
                source_result,
                settings=merged,
                repo_path=repo_path,
                ai_provider=ai_provider,
                ai_model=ai_model,
                custom_prompt=custom_prompt,
                server_url=server_url,
                job_id=job_id,
                peer_ai_configs=peer_ai_configs,
                cloned_repos=cloned_repos,
                auth_header=auth_header,
            )

            # Pipeline/orchestrator: failed children, no direct test failures
            if child_job_analyses and not test_failures:
                analyzed_children = [
                    c for c in child_job_analyses if child_has_successful_analysis(c)
                ]
                if not analyzed_children:
                    summary = f"All {len(child_job_analyses)} child job analyses failed"
                    fail_result = FailureAnalysisResult(
                        job_id=job_id,
                        status="failed",
                        summary=summary,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                    )
                    result_data = fail_result.model_dump(mode="json")
                    result_data["error"] = summary
                    result_data["job_name"] = display_name
                    result_data["child_job_analyses"] = [
                        c.model_dump(mode="json") for c in child_job_analyses
                    ]
                    _stamp_result_metadata(result_data, source_result)
                    await _preserve_request_params(job_id, result_data)
                    await _attach_token_usage(job_id, result_data)
                    # Aggregate available child counts so /full and the
                    # dashboard report real numbers on this path too.
                    await _persist_test_entries_and_cache_counts(
                        job_id, source_result, child_test_scopes, result_data
                    )
                    await update_status(job_id, "failed", result_data)
                    notify_active_count_changed()
                    notify_dashboard_changed()
                    notify_job_status_changed(job_id)
                    notify_token_usage_changed()
                    return

                total_failures = sum(
                    len(child.failures) for child in child_job_analyses
                )
                summary = (
                    f"Pipeline failed due to {len(child_job_analyses)} child job(s)."
                )
                if total_failures > 0:
                    summary += (
                        f" Total: {total_failures} failure(s) analyzed. "
                        "See child analyses below."
                    )

                # Enrich child failures before saving
                if _resolve_enable_jira(body, merged) and merged.jira_enabled:
                    await _enrich_result_with_jira(
                        child_job_analyses,
                        merged,
                        ai_provider,
                        ai_model,
                        job_id=job_id,
                    )
                if merged.tests_repo_url or tests_repo_url:
                    await _enrich_result_with_tests_repo_matches(
                        child_job_analyses,
                        merged,
                        ai_provider,
                        ai_model,
                        job_id=job_id,
                        tests_repo_url=tests_repo_url,
                    )

                analysis_result = FailureAnalysisResult(
                    job_id=job_id,
                    status="completed",
                    summary=summary,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    failures=[],
                )
                result_data = analysis_result.model_dump(mode="json")
                result_data["job_name"] = display_name
                result_data["child_job_analyses"] = [
                    c.model_dump(mode="json") for c in child_job_analyses
                ]
                result_data["failed_analysis_groups"] = _count_failed_child_groups(
                    child_job_analyses
                )
                if result_data["failed_analysis_groups"]:
                    logger.warning(
                        "Analysis job %s: %d child group(s) failed",
                        job_id,
                        result_data["failed_analysis_groups"],
                    )
                _stamp_result_metadata(result_data, source_result)
                await _preserve_request_params(job_id, result_data)
                await _attach_token_usage(job_id, result_data)
                try:
                    await populate_failure_history(job_id, result_data)
                except Exception:
                    logger.warning(
                        "Failed to populate failure_history for job_id=%s",
                        job_id,
                        exc_info=True,
                    )

                # Persist top-level + child-scoped test entries
                await safe_update_progress(job_id, "saving")
                logger.info("Saving child job analysis for job=%s", job_id)
                _top_entries = source_result.test_entry_dicts()
                await replace_job_test_entries(job_id, _top_entries, child_test_scopes)
                _apply_cached_test_counts(result_data, _top_entries, child_test_scopes)

                await _carry_forward_overrides(job_id, result_data)
                try:
                    await _auto_review_matching_failures(
                        job_id,
                        metadata_job_name,
                        resolve_display_build_id(result_data),
                        result_data,
                        merged,
                    )
                except Exception:
                    logger.warning(
                        "Auto-review failed for job_id=%s",
                        job_id,
                        exc_info=True,
                    )
                await update_status(job_id, "completed", result_data)
                notify_active_count_changed()
                notify_dashboard_changed()
                notify_job_status_changed(job_id)
                notify_token_usage_changed()
                await _auto_assign_metadata(
                    metadata_job_name,
                    merged.metadata_rules,
                    extra_labels=extra_labels,
                )
                await storage.make_classifications_visible(job_id)
                return

        analysis_result_tuple = await _analyze_failures_or_exit(
            job_id=job_id,
            test_failures=test_failures,
            console_context=console_context,
            artifacts_context=artifacts_context,
            metadata_job_name=metadata_job_name,
            display_name=display_name,
            repo_path=repo_path,
            ai_provider=ai_provider,
            ai_model=ai_model,
            merged=merged,
            custom_prompt=custom_prompt,
            server_url=server_url,
            peer_ai_configs=peer_ai_configs,
            cloned_repos=cloned_repos,
            auth_header=auth_header,
            groups=groups,
            source_result=source_result,
            child_job_analyses=child_job_analyses,
            child_test_scopes=child_test_scopes,
            extra_labels=extra_labels,
        )
        if analysis_result_tuple is None:
            return
        all_analyses, test_failures, unique_errors, cross_failure_patterns = (
            analysis_result_tuple
        )

        failed_tests = [a for a in all_analyses if _is_failed_analysis(a)]
        failed_analyses = len({storage.resolve_signature(a) for a in failed_tests})
        analysis_status: Literal["completed", "failed"] = (
            "completed"
            if len(all_analyses) > len(failed_tests)
            or any(child_has_successful_analysis(child) for child in child_job_analyses)
            else "failed"
        )
        if failed_analyses and not child_job_analyses:
            logger.warning(
                "Analysis job %s: %d group(s) failed; status=%s",
                job_id,
                failed_analyses,
                analysis_status,
            )
        summary = (
            f"Analyzed {len(test_failures)} test failures "
            f"({unique_errors} unique errors). "
            f"{len(all_analyses) - len(failed_tests)} analyzed successfully."
        )
        if failed_analyses:
            summary += f" {failed_analyses} group(s) failed; check server logs."
        if child_job_analyses:
            summary = (
                f"{summary} Additionally, {len(child_job_analyses)} failed child "
                f"job(s) were analyzed recursively."
            )

        enrich_targets: list[Any] = list(all_analyses) + list(child_job_analyses)

        # Enrich with Jira matches
        logger.debug(
            f"Enriching with Jira matches (enable_jira={_resolve_enable_jira(body, merged)})"
        )
        if _resolve_enable_jira(body, merged) and merged.jira_enabled:
            await _enrich_result_with_jira(
                enrich_targets, merged, ai_provider, ai_model, job_id=job_id
            )

        # Enrich with tests repo matches
        logger.debug(
            f"Enriching with tests repo matches (tests_repo_url={redact_url(str(tests_repo_url or ''))})"
        )
        if merged.tests_repo_url or tests_repo_url:
            await _enrich_result_with_tests_repo_matches(
                enrich_targets,
                merged,
                ai_provider,
                ai_model,
                job_id=job_id,
                tests_repo_url=tests_repo_url,
            )

        # Build enriched XML if applicable (FileSource exposes raw_xml)
        enriched_xml = None
        source_raw_xml = getattr(source, "raw_xml", None)
        if source_raw_xml is not None:
            enriched_xml = build_enriched_xml(
                source_raw_xml, all_analyses, f"{base_url}/results/{job_id}"
            )

        analysis_result = FailureAnalysisResult(
            job_id=job_id,
            status=analysis_status,
            summary=summary,
            ai_provider=ai_provider,
            ai_model=ai_model,
            failures=all_analyses,
            enriched_xml=enriched_xml,
            cross_failure_patterns=cross_failure_patterns,
        )

        result_data = analysis_result.model_dump(mode="json")
        result_data["failed_analysis_groups"] = (
            failed_analyses + _count_failed_child_groups(child_job_analyses)
        )
        if child_job_analyses and result_data["failed_analysis_groups"]:
            logger.warning(
                "Analysis job %s: %d failed group(s) across direct and child scopes",
                job_id,
                result_data["failed_analysis_groups"],
            )
        result_data["job_name"] = display_name
        if child_job_analyses:
            result_data["child_job_analyses"] = [
                c.model_dump(mode="json") for c in child_job_analyses
            ]
        _stamp_result_metadata(result_data, source_result)
        logger.info(f"Analysis completed for job_id={job_id}: {summary}")
        await _preserve_request_params(job_id, result_data)

        # Attach token usage summary before persisting
        await _attach_token_usage(job_id, result_data)

        # Populate failure history and auto-review BEFORE marking completed
        await safe_update_progress(job_id, "saving")
        logger.info("Saving analysis for job=%s", job_id)
        try:
            await populate_failure_history(job_id, result_data)
        except Exception:
            logger.warning(
                "Failed to populate failure_history for job_id=%s",
                job_id,
                exc_info=True,
            )

        # Save ALL test entries (passed/skipped/failed) to test_entries table
        _all_test_entries = source_result.test_entry_dicts()
        await replace_job_test_entries(job_id, _all_test_entries, child_test_scopes)

        # Cache job-level test counts in result_json for dashboard display
        _apply_cached_test_counts(result_data, _all_test_entries, child_test_scopes)

        await _carry_forward_overrides(job_id, result_data)

        # Auto-review failures with matching signatures from previous analyses.
        # enable_auto_review toggle is enforced inside the function (early return)
        # to keep a single enforcement point and avoid DRY violation at call sites.
        try:
            await _auto_review_matching_failures(
                job_id,
                metadata_job_name,
                resolve_display_build_id(result_data),
                result_data,
                merged,
            )
        except Exception:
            _build_number = resolve_display_build_id(result_data)
            logger.warning(
                "Auto-review failed for job_id=%s, job_name=%s, build=%s",
                job_id,
                metadata_job_name,
                _build_number,
                exc_info=True,
            )

        await update_status(job_id, analysis_status, result_data)
        notify_active_count_changed()
        notify_dashboard_changed()
        notify_job_status_changed(job_id)
        notify_token_usage_changed()

        # Auto-assign job metadata from name pattern rules / request labels
        await _auto_assign_metadata(
            metadata_job_name,
            merged.metadata_rules,
            extra_labels=extra_labels,
        )

        # Reveal classifications created during analysis
        await storage.make_classifications_visible(job_id)

    except asyncio.CancelledError:
        logger.info(f"Analysis task cancelled for job_id={job_id}")
        return

    except Exception as e:
        logger.exception(f"Analysis failed for job {job_id}")
        user_error = make_user_friendly_error(e)
        fail_result = FailureAnalysisResult(
            job_id=job_id,
            status="failed",
            summary=user_error,
            ai_provider=ai_provider,
            ai_model=ai_model,
            analysis_state=_analysis_state_for_ingest(ingest_only),
        )
        fail_data = fail_result.model_dump(mode="json")
        fail_data["error"] = fail_result.summary
        fail_data["job_name"] = display_name
        _stamp_result_metadata(fail_data, source_result)
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
            from rootcoz.engine.graft import cleanup_graph
            from rootcoz.engine.graft_http import unregister_workspace

            if repo_path is not None:
                unregister_workspace(repo_path)
                cleanup_graph(repo_path)
            repo_manager.cleanup()
        if source is not None:
            source.cleanup()
        await _cleanup_ai_session(auth_header)


def _resolve_request_display_name(body: UnifiedAnalyzeRequest) -> str:
    """Resolve dashboard display name from the request or the source plugin."""
    display_name: str = body.name or ""
    if display_name:
        return display_name
    from rootcoz.sources.registry import SOURCE_REGISTRY

    source_cls = SOURCE_REGISTRY.get(body.type)
    return (
        source_cls.default_display_name(body)
        if source_cls is not None
        else f"{body.type}-analysis"
    )


@app.post("/analyze", status_code=202, response_model=None, operation_id="analyze")
async def analyze(
    request: Request,
    body: UnifiedAnalyzeRequest,
    *,
    settings: Settings = _SETTINGS_DEP,
) -> dict[str, Any]:
    """Queue an end-to-end analysis job (CI ingest + AI).

    Dispatches to the appropriate CI source plugin based on the ``type`` field.
    All types return 202 with a job_id for async polling.
    Requires operator or admin role.
    """
    _require_operator(request)
    _check_allow_list(request)
    base_url = _extract_base_url()

    token = ai_username.set(request.state.username)
    try:
        await _resolve_ai_config_allow_defer(body, settings, request)
        merged = _merge_settings(body, settings)
        resolved_peers = await _validate_peer_configs(body, merged)
    finally:
        ai_username.reset(token)

    display_name = _resolve_request_display_name(body)
    return await _enqueue_ci_source_analysis(
        body=body,
        merged=merged,
        resolved_peers=resolved_peers,
        display_name=display_name,
        analysis_type=body.type,
        base_url=base_url,
        username=request.state.username,
        message_prefix="Analysis",
        is_admin=bool(request.state.is_admin),
    )


@app.post("/submit", status_code=202, response_model=None, operation_id="submit")
async def submit(
    request: Request,
    body: UnifiedAnalyzeRequest,
    *,
    settings: Settings = _SETTINGS_DEP,
) -> dict[str, Any]:
    """Queue CI ingest without AI analysis.

    Same source validation and test-result collection as ``POST /analyze``,
    then stops before repository clone and AI. Requires operator or admin.
    """
    _require_operator(request)
    _check_allow_list(request)
    base_url = _extract_base_url()

    display_name = _resolve_request_display_name(body)
    merged = _merge_settings(body, settings)
    return await _enqueue_ci_source_analysis(
        body=body,
        merged=merged,
        resolved_peers=None,
        display_name=display_name,
        analysis_type=body.type,
        base_url=base_url,
        username=request.state.username,
        message_prefix="Submit",
        is_admin=bool(request.state.is_admin),
        ingest_only=True,
    )


@app.post(
    "/results/{job_id}/analyze",
    status_code=202,
    response_model=None,
    operation_id="analyzeSubmittedJob",
)
async def analyze_submitted_job(
    job_id: str,
    request: Request,
    body: ReAnalyzeRequest | None = None,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Run the AI pipeline in place on a submitted (ingest-only) job."""
    _check_allow_list(request)
    _require_operator(request)

    stored = await get_result(job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Result {job_id} not found")

    result_data = stored["result"]
    state = result_data.get("analysis_state") or stored.get("analysis_state")
    if state != ANALYSIS_STATE_SUBMITTED:
        raise HTTPException(
            status_code=409,
            detail="Only submitted jobs can be analyzed in place. Use re-analyze for analyzed jobs.",
        )
    if stored.get("status") in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Job is still ingesting; wait until submit completes.",
        )

    if "request_params" not in result_data:
        raise HTTPException(
            status_code=400,
            detail="Submitted job has no stored request_params; cannot analyze",
        )

    params = result_data.get("request_params", {})
    analysis_type = params.get("analysis_type", "jenkins")
    try:
        decrypted_params = decrypt_sensitive_fields(dict(params))
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to decrypt stored params: {exc}",
        ) from exc
    _validate_decrypted_sensitive_fields(decrypted_params)

    unified_fields = _unified_fields_from_stored_params(decrypted_params, analysis_type)
    if body is not None:
        for field_name in body.model_fields_set:
            unified_fields[field_name] = getattr(body, field_name)
    if "get_job_artifacts" not in unified_fields:
        unified_fields["get_job_artifacts"] = True
    unified_body = UnifiedAnalyzeRequest(**unified_fields)
    source_cls = CI_SOURCE_REGISTRY.get(analysis_type)
    if source_cls is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported analysis type: {analysis_type}",
        )

    await _resolve_ai_config_allow_defer(unified_body, get_settings(), request)
    merged = _merge_settings(unified_body, get_settings())
    resolved_peers = await _validate_peer_configs(unified_body, merged)
    display_name = (
        unified_body.name
        or result_data.get("display_name")
        or result_data.get("job_name")
        or source_cls.default_display_name(unified_body)
    )
    previous_status = str(stored.get("status") or "completed")
    claimed = await claim_submitted_job_for_analyze(job_id)
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail="Job is already being analyzed or is no longer submitted.",
        )
    try:
        return await _enqueue_ci_source_analysis(
            body=unified_body,
            merged=merged,
            resolved_peers=resolved_peers,
            display_name=display_name,
            analysis_type=analysis_type,
            base_url=_extract_base_url(),
            username=request.state.username,
            tags=list(result_data.get("tags") or []),
            message_prefix="Analysis",
            is_admin=bool(request.state.is_admin),
            existing_job_id=job_id,
        )
    except Exception:
        await release_submitted_job_analyze_claim(job_id, previous_status)
        raise


def _unified_fields_from_stored_params(
    decrypted_params: dict[str, Any], analysis_type: str
) -> dict[str, Any]:
    """Rebuild UnifiedAnalyzeRequest fields from stored request_params."""
    unified_fields: dict[str, Any] = {"type": analysis_type}
    stored_name = decrypted_params.get("original_name", "")
    if stored_name:
        unified_fields["name"] = stored_name
    source_cls = CI_SOURCE_REGISTRY.get(analysis_type)
    if source_cls is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported analysis type for re-analysis: {analysis_type}",
        )
    try:
        unified_fields.update(source_cls.restore_reanalyze_fields(decrypted_params))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _copy_analysis_settings(decrypted_params, unified_fields)
    return unified_fields


@app.post(
    "/re-analyze/{job_id}",
    status_code=202,
    response_model=None,
    operation_id="reAnalyze",
)
async def re_analyze(
    job_id: str,
    request: Request,
    body: ReAnalyzeRequest,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Re-analyze a previously analyzed job with the same (or overridden) settings.

    Loads stored request_params from the original analysis, applies any
    overrides from the request body, and queues a new analysis with a
    fresh job_id.
    """
    _check_allow_list(request)
    _require_operator(request)
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

    if "request_params" not in result_data:
        raise HTTPException(
            status_code=400,
            detail="Original analysis has no stored request_params; cannot re-analyze",
        )

    # Detect analysis type from stored params
    params = result_data.get("request_params", {})
    analysis_type = params.get(
        "analysis_type", "jenkins"
    )  # default to jenkins for backward compat

    # Rebuild a UnifiedAnalyzeRequest and re-submit via the shared CI source path
    try:
        decrypted_params = decrypt_sensitive_fields(dict(params))
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to decrypt stored params: {exc}",
        ) from exc

    _validate_decrypted_sensitive_fields(decrypted_params)

    unified_fields = _unified_fields_from_stored_params(decrypted_params, analysis_type)
    source_cls = CI_SOURCE_REGISTRY.get(analysis_type)
    if source_cls is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported analysis type for re-analysis: {analysis_type}",
        )

    # Apply overrides from request body
    for field_name in body.model_fields_set:
        unified_fields[field_name] = getattr(body, field_name)

    unified_body = UnifiedAnalyzeRequest(**unified_fields)

    # Tags: carry forward + auto-add re-analyze
    existing_tags = list(result_data.get("tags", []))
    if "re-analyze" not in existing_tags:
        existing_tags.append("re-analyze")
    # Remove old submitter tag — enqueue will add the current submitter
    existing_tags = _strip_old_submitter_tag(existing_tags, result_data)
    if "tags" in body.model_fields_set:
        for t in getattr(body, "tags", None) or []:
            if t not in existing_tags:
                existing_tags.append(t)
    unified_body.tags = existing_tags

    # Validate and merge settings
    await _resolve_ai_config_allow_defer(unified_body, get_settings(), request)
    merged = _merge_settings(unified_body, get_settings())
    resolved_peers = await _validate_peer_configs(unified_body, merged)

    # Resolve display name — prefer original name, then plugin fallback
    display_name = unified_body.name or source_cls.default_display_name(unified_body)

    return await _enqueue_ci_source_analysis(
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
        is_admin=bool(request.state.is_admin),
    )


def _iter_scoped_failures(
    result_data: dict[str, Any],
) -> Iterator[tuple[dict[str, Any], str, int]]:
    """Yield ``(failure, child_job_name, child_build_number)`` across the tree.

    Walks the top-level ``failures`` list (child scope ``""``/``0``), then
    ``child_job_analyses`` and nested ``failed_children`` recursively.
    """
    for failure in result_data.get("failures", []):
        if isinstance(failure, dict):
            yield failure, "", 0

    def _walk_children(
        children: list[Any],
    ) -> Iterator[tuple[dict[str, Any], str, int]]:
        for child in children:
            if not isinstance(child, dict):
                continue
            child_job = child.get("job_name", "")
            child_build = child.get("build_number", 0)
            for failure in child.get("failures", []):
                if isinstance(failure, dict):
                    yield failure, child_job, child_build
            # Recurse into nested failed_children
            yield from _walk_children(child.get("failed_children", []))

    yield from _walk_children(result_data.get("child_job_analyses", []))


async def _apply_effective_classifications(
    job_id: str, result_data: dict[str, Any]
) -> None:
    """Apply user classification overrides to failures in result_data.

    Batch-queries all overrides for the job, then walks all failures
    (top-level, children, and nested failed_children) applying them.
    Clears stale subtype fields consistent with _resolve_effective_failure.
    """
    overrides = await storage.get_all_effective_classifications(job_id)
    if not overrides:
        return

    def _apply_override(
        failure: dict[str, Any], child_job_name: str, child_build_number: int
    ) -> None:
        if not isinstance(failure, dict):
            return
        test_name = failure.get("test_name", "")
        if not test_name:
            return
        analysis = failure.get("analysis")
        if not isinstance(analysis, dict):
            return
        key = (test_name, child_job_name, child_build_number)
        effective = overrides.get(key)
        if not effective:
            return
        current = analysis.get("classification", "")
        if effective == current:
            return
        analysis["classification"] = effective
        analysis["_original_classification"] = current
        # Clear stale subtype fields (consistent with _resolve_effective_failure)
        if effective == "CODE ISSUE":
            analysis["product_bug_report"] = False
        elif effective == "PRODUCT BUG":
            analysis["code_fix"] = False
        elif effective == "INFRASTRUCTURE":
            analysis["code_fix"] = False
            analysis["product_bug_report"] = False

    for failure, child_job_name, child_build_number in _iter_scoped_failures(
        result_data
    ):
        _apply_override(failure, child_job_name, child_build_number)


@app.get("/api/results/fields", operation_id="listResultFields")
async def list_result_fields() -> dict[str, Any]:
    """Return the allowlist of field paths for sparse GET /results/{job_id}."""
    return {"fields": sorted(RESULT_FIELD_PATHS)}


@app.get("/results/{job_id}", response_model=None, operation_id="getJobResult")
async def get_job_result(
    request: Request,
    job_id: str,
    response: Response,
    _: None = Depends(_bind_job_id),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated allowlisted field paths to return (full values, "
            "never truncated). Omit for the full response. Unknown paths return "
            "400. Discover paths via GET /api/results/fields."
        ),
    ),
) -> Any:
    """Retrieve stored result by job_id, or serve SPA for browser requests.

    Optional ``fields`` query selects allowlisted paths only (no truncation).
    Unknown fields yield HTTP 400. See ``GET /api/results/fields``.
    """
    # Content negotiation: browsers requesting HTML get the SPA
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        result = await get_result(job_id)
        if result and result.get("status") in IN_PROGRESS_STATUSES:
            return RedirectResponse(url=f"/status/{job_id}", status_code=302)
        return _serve_spa()

    try:
        field_list = parse_fields_param(fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.debug(f"GET /results/{job_id}")
    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    # Apply user classification overrides so the UI shows effective classifications
    if result.get("result"):
        await _apply_effective_classifications(job_id, result["result"])
        status = result.get("status")
        if status in IN_PROGRESS_STATUSES:
            await _attach_token_usage(job_id, result["result"], detailed=False)
        elif (
            status in ("failed", "aborted")
            and result["result"].get("token_usage") is None
        ):
            await _attach_token_usage(job_id, result["result"])
    _attach_result_links(result, _extract_base_url(), job_id)
    await _attach_origin_job_info(result)
    # Attach tracked-in data per failure
    result["tracked_in"] = await storage.get_tracked_in_for_job(job_id)
    settings = get_settings()
    result["capabilities"] = _build_capabilities(settings)
    if result.get("status") in IN_PROGRESS_STATUSES:
        response.status_code = 202
    if field_list is not None:
        return filter_result_fields(result, field_list)
    return result


def _validate_test_status_filter(status: list[str] | None) -> list[str] | None:
    """Sanitize sidecar placeholders and validate test status filter values.

    Returns the sanitized list, or ``None`` when nothing remains. Raises
    HTTP 400 for invalid status values.
    """
    if not status:
        return None
    # Sidecar may leave unsubstituted "{status}" when optional tool arg omitted
    sanitized = [
        s for raw in status if (s := _sanitize_sidecar_placeholder(raw))
    ] or None
    if not sanitized:
        return None
    valid_statuses = {"passed", "skipped", "failed"}
    invalid = [s for s in sanitized if s not in valid_statuses]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status values: {', '.join(invalid)}. "
            f"Valid values are: {', '.join(sorted(valid_statuses))}",
        )
    return sanitized


@app.get("/api/results/{job_id}/tests", operation_id="getJobTests")
async def get_job_tests(
    job_id: str,
    status: Annotated[
        list[str] | None,
        Query(
            description="Filter by test status: passed, skipped, failed (repeatable)"
        ),
    ] = None,
    child_job_name: str | None = Query(
        default=None,
        description="Filter by child job name",
    ),
    child_build_number: int | None = Query(
        default=None,
        description="Filter by child build number",
    ),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
) -> dict[str, Any]:
    """Get paginated test entries for a job (viewer+)."""
    status = _validate_test_status_filter(status)

    # Verify job exists
    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")

    data = await storage.get_test_entries(
        job_id,
        status=status,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        offset=offset,
        limit=limit,
    )
    return data


@app.get("/api/results/{job_id}/full", operation_id="getJobResultFull")
async def get_job_result_full(
    response: Response,
    job_id: str,
    _: None = Depends(_bind_job_id),
    status: Annotated[
        list[str] | None,
        Query(
            description="Filter the tests section by status: "
            "passed, skipped, failed (repeatable)"
        ),
    ] = None,
    child_job_name: str | None = Query(
        default=None,
        description="Filter the tests section by child job name",
    ),
    child_build_number: int | None = Query(
        default=None,
        description="Filter the tests section by child build number",
    ),
    offset: int = Query(default=0, ge=0, description="Tests section pagination offset"),
    limit: int = Query(default=50, ge=1, le=200, description="Tests section page size"),
) -> dict[str, Any]:
    """Get complete job data in one response (viewer+).

    Combines what today needs several calls into a single payload:
    job metadata, test counts, failures (flattened across child jobs,
    with effective classifications and review state joined server-side),
    a precomputed summary, a paginated ``tests`` page, comments, and
    tracked-in links.

    The ``tests`` section accepts the same filters and pagination as
    ``GET /api/results/{job_id}/tests`` and reports ``total`` so
    consumers can page through large jobs.
    """
    status = _validate_test_status_filter(status)

    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")

    result_data = result.get("result") or {}
    await _apply_effective_classifications(job_id, result_data)

    reviews = await storage.get_reviews_for_job(job_id)

    failures: list[dict[str, Any]] = []
    reviewed_count = 0
    classification_counts: Counter[str] = Counter()
    for failure, child_job, child_build in _iter_scoped_failures(result_data):
        analysis = failure.get("analysis")
        if not isinstance(analysis, dict):
            # Legacy results stored the analysis as plain text — no
            # classification/pattern to extract (raw text stays available
            # via GET /results/{job_id}).
            analysis = {}
        classification = analysis.get("classification", "")
        if classification:
            classification_counts[classification] += 1
        entry = resolve_failure_review(
            reviews, failure.get("test_name", ""), child_job, child_build
        )
        reviewed = entry is not None
        if reviewed:
            reviewed_count += 1
        failures.append(
            {
                "test_name": failure.get("test_name", ""),
                "classification": classification,
                "pattern": analysis.get("pattern", ""),
                "error": failure.get("error", ""),
                "error_signature": failure.get("error_signature", ""),
                "child_job_name": child_job,
                "child_build_number": child_build,
                "reviewed": reviewed,
                "reviewed_by": (
                    _sanitize_control_chars(entry["username"]) if entry else ""
                ),
            }
        )

    tests = await storage.get_test_entries(
        job_id,
        status=status,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        offset=offset,
        limit=limit,
    )
    comments = await storage.get_comments_for_job(job_id)
    tracked_in = await storage.get_tracked_in_for_job(job_id)

    payload: dict[str, Any] = {
        "job": {
            "job_id": result.get("job_id", job_id),
            "job_name": result_data.get("job_name", ""),
            "build_number": resolve_display_build_id(result_data),
            "status": result.get("status", ""),
            "analysis_state": result_data.get("analysis_state", "analyzed"),
            "created_at": result.get("created_at", ""),
        },
        "counts": {
            "passed": result_data.get("passed_count", 0),
            "skipped": result_data.get("skipped_count", 0),
            "failed": result_data.get("failed_count", 0),
        },
        "failures": failures,
        "summary": {
            "total_failures": len(failures),
            "reviewed_count": reviewed_count,
            "classifications": dict(classification_counts),
        },
        "tests": tests,
        "comments": comments,
        "tracked_in": tracked_in,
    }

    if result.get("status") in IN_PROGRESS_STATUSES:
        response.status_code = 202

    return strip_sensitive_from_response(payload)


@app.get("/api/failures/{failure_uuid}", operation_id="getFailureByUuid")
async def get_failure_by_uuid(failure_uuid: str) -> dict[str, Any]:
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
    failure_dict: dict[str, Any],
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None,
    raw_prompt: str,
    peer_ai_configs: list[Any] | None,
    peer_analysis_max_rounds: int,
    tests_repo_url: str,
    tests_repo_ref: str,
    tests_repo_token: str,
    additional_repos_list: list[Any] | None,
    username: str,
    max_concurrent_ai_calls: int,
    analysis_type: str = "",
    source_params: dict[str, Any] | None = None,
    settings: Settings | None = None,
    child_job_name: str = "",
    child_build_number: int = 0,
    *,
    is_admin: bool = False,
) -> None:
    """Background task: re-analyze a single failure in-place."""
    job_id_var.set(job_id)
    ai_username.set(username)
    force_server_credentials.set(
        (source_params or {}).get(
            "force_server_credentials",
            (settings or get_settings()).force_server_credentials,
        )
    )
    auth_header = ""
    repo_manager: RepositoryManager | None = None
    repo_path: Path | None = None
    source: CISource | None = None

    try:
        auth_header = await _create_ai_auth_header(username)

        # Clone repos if configured (no artifact linking here — done after refetch)
        repo_manager = RepositoryManager()
        cloned_repos: dict[str, Path] = {}
        repo_path = repo_manager.create_workspace()

        if tests_repo_url:
            try:
                repo_name = derive_test_repo_name(
                    str(tests_repo_url), additional_repos_list or []
                )
                await safe_update_clone_progress(
                    job_id,
                    repo_name,
                    True,
                    reanalysis=True,
                    operation_id=f"{failure_uuid}:tests",
                    url=str(tests_repo_url),
                    ref=tests_repo_ref,
                )
                outcome = "cancelled"
                try:
                    clone_task = asyncio.create_task(
                        asyncio.to_thread(
                            repo_manager.clone_into,
                            str(tests_repo_url),
                            repo_path / repo_name,
                            depth=50,
                            branch=tests_repo_ref,
                            token=tests_repo_token or None,
                        )
                    )
                    try:
                        # wait() leaves the worker running if this await is cancelled.
                        await asyncio.wait({clone_task})
                        clone_task.result()
                    except asyncio.CancelledError:
                        # A cancelled await does not stop the thread. Keep the workspace
                        # until the worker exits, even if cancellation is repeated.
                        while not clone_task.done():
                            try:
                                await asyncio.wait({clone_task})
                            except asyncio.CancelledError:
                                continue
                        if not clone_task.cancelled() and (
                            clone_error := clone_task.exception()
                        ):
                            # Exception text, stderr and traceback filenames can all
                            # contain attacker-controlled URLs, headers or paths.
                            if isinstance(clone_error, GitCommandError):
                                # Match only fixed phrases; stderr may embed credentials.
                                stderr = (clone_error.stderr or "").lower()
                                cause = "Git command failed"
                                for patterns, label in (
                                    (
                                        ("authentication failed",),
                                        "Authentication failed",
                                    ),
                                    (
                                        (
                                            "remote branch",
                                            "pathspec",
                                            "bad revision",
                                            "not a valid ref",
                                        ),
                                        "Branch or ref not found",
                                    ),
                                    (
                                        (
                                            "ssl certificate",
                                            "certificate verify",
                                            "server verification failed",
                                        ),
                                        "SSL verification failed",
                                    ),
                                    (("permission denied",), "Permission denied"),
                                    (
                                        (
                                            "could not resolve host",
                                            "name or service not known",
                                        ),
                                        "Network DNS resolution failed",
                                    ),
                                    (
                                        (
                                            "repository not found",
                                            "not a git repository",
                                        ),
                                        "Repository not found",
                                    ),
                                ):
                                    if any(pattern in stderr for pattern in patterns):
                                        cause = label
                                        break
                            elif isinstance(clone_error, ValueError) and str(
                                clone_error
                            ).startswith("Invalid repository URL scheme."):
                                cause = "Invalid repository URL scheme"
                            else:
                                cause = "Clone worker failed"
                            frames = []
                            tb = clone_error.__traceback__
                            while tb:
                                name = tb.tb_frame.f_code.co_name
                                if name not in {
                                    "clone",
                                    "clone_into",
                                    "_validate_repo_url",
                                }:
                                    name = "frame"
                                filename = tb.tb_frame.f_code.co_filename
                                source_dir = str(Path(__file__).parent) + os.sep
                                location = (
                                    filename.removeprefix(source_dir)
                                    if filename.startswith(source_dir)
                                    else "[path]"
                                )
                                frames.append(f"{location}:{tb.tb_lineno} in {name}")
                                tb = tb.tb_next
                            logger.warning(
                                "Test repository clone failed during cancellation: %s: %s\n%s",
                                type(clone_error).__name__,
                                cause,
                                "\n".join(frames),
                            )
                        logger.info(
                            "Test repository clone worker joined after cancellation"
                        )
                        raise
                    outcome = "cloned"
                except Exception:
                    outcome = "failed"
                    raise
                finally:
                    await safe_update_clone_progress(
                        job_id,
                        repo_name,
                        False,
                        state=outcome,
                        reanalysis=True,
                        operation_id=f"{failure_uuid}:tests",
                    )
                cloned_repos[repo_name] = repo_path / repo_name
            except Exception:
                logger.warning(
                    "Failed to clone test repository for failure re-analysis",
                    exc_info=True,
                )

        tests_cloned_path = (
            next(iter(cloned_repos.values()), None) if cloned_repos else None
        )
        # Non-empty parent/override values act as request-tier (win over settings.json).
        # Unset fields stay off the shim so settings.json can fill gaps.
        shim_data: dict[str, Any] = {}
        if ai_provider:
            shim_data["ai_provider"] = ai_provider
        if ai_model:
            shim_data["ai_model"] = ai_model
        if ai_call_timeout is not None:
            shim_data["ai_call_timeout"] = ai_call_timeout
        if peer_ai_configs is not None:
            shim_data["peer_ai_configs"] = peer_ai_configs
        if (
            source_params is not None
            and source_params.get("force_server_credentials") is not None
        ):
            shim_data["force_server_credentials"] = source_params[
                "force_server_credentials"
            ]
        # Stored/override concurrency + peer rounds are request-tier
        shim_data["peer_analysis_max_rounds"] = peer_analysis_max_rounds
        shim_data["max_concurrent_ai_calls"] = max_concurrent_ai_calls
        # Preserve [] (explicit disable) vs None (unset / inherit settings.json)
        if additional_repos_list is not None:
            shim_data["additional_repos"] = additional_repos_list
        shim = BaseAnalysisRequest(**shim_data)
        try:
            effective = await resolve_repo_analysis_settings(
                job_id,
                tests_cloned_path,
                shim,
                get_settings(),
                ai_provider=ai_provider,
                ai_model=ai_model,
                peer_ai_configs=peer_ai_configs,
                additional_repos=additional_repos_list,
            )
            ai_provider = effective.ai_provider
            ai_model = effective.ai_model
            peer_ai_configs = effective.peer_ai_configs
            additional_repos_list = effective.additional_repos
            force_server_credentials.set(effective.settings.force_server_credentials)
            # Request-tier values win; effective mirrors shim for these fields
            peer_analysis_max_rounds = effective.settings.peer_analysis_max_rounds
            if ai_call_timeout is None:
                ai_call_timeout = effective.settings.ai_call_timeout
            max_concurrent_ai_calls = effective.settings.max_concurrent_ai_calls
            tests_dir_name = (
                tests_cloned_path.name if tests_cloned_path is not None else None
            )
            assert_no_tests_repo_name_collision(tests_dir_name, additional_repos_list)
        except RootcozSettingsError as exc:
            logger.error(
                "Invalid .rootcoz/settings.json during failure re-analysis "
                "(details redacted)"
            )
            fail_data = {"error": str(exc)}
            await _preserve_request_params(job_id, fail_data)
            await update_status(job_id, "failed", fail_data)
            return

        if not ai_provider or not ai_model:
            fail_data = {
                "error": _ai_not_configured_message(
                    None, "AI provider/model", is_admin=is_admin
                ),
            }
            await _preserve_request_params(job_id, fail_data)
            await update_status(job_id, "failed", fail_data)
            return

        if additional_repos_list:
            additional_repos_cloned, repo_path = await clone_additional_repos(
                repo_manager,
                additional_repos_list,
                repo_path,
                job_id=job_id,
                reanalysis=True,
                operation_id=failure_uuid,
            )
            cloned_repos.update(additional_repos_cloned)

        # Copy .rootcoz/{agents,skills,extensions}/ to workspace .pi/
        if cloned_repos:
            copy_rootcoz_pi_resources(cloned_repos, repo_path)

        # Re-download console output and artifacts from the original CI source
        console_context = ""
        artifacts_context = ""
        if source_params and analysis_type and settings:
            try:
                source = source_chat_workspace.reconstruct_source(
                    analysis_type,
                    source_params,
                    settings,
                    child_job_name=child_job_name,
                    child_build_number=child_build_number,
                )
                if source is None:
                    logger.warning(
                        "Could not reconstruct %s source; "
                        "proceeding without console/artifacts",
                        analysis_type,
                    )
                else:
                    ctx = await source.refetch_context()
                    console_context = ctx.console_context
                    artifacts_context = link_refetched_artifacts(
                        repo_path,
                        ctx.extract_path,
                        ctx.artifacts_context,
                        job_id,
                    )
                    for warning in ctx.warnings:
                        logger.warning(
                            "Refetch warning for job %s: %s", job_id, warning
                        )
            except Exception:
                logger.warning(
                    "Failed to refetch source context for reanalysis; "
                    "proceeding without console/artifacts",
                    exc_info=True,
                )

        if source is not None and repo_path:
            try:
                github_token = (
                    settings.github_token.get_secret_value()
                    if settings and settings.github_token
                    else ""
                )
                raw_prompt = await apply_source_workspace_files(
                    source,
                    repo_path,
                    raw_prompt,
                    job_id,
                    github_token=github_token,
                )
            except Exception:
                logger.warning(
                    "Failed to prepare workspace files for reanalysis",
                    exc_info=True,
                )

        # Build a FailedTest from the failure dict
        _ft_kwargs: dict[str, Any] = {
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

        # Scope only this attempt's AI calls, not clone/refetch or result writes.
        with (
            reanalysis_usage_scope(failure_uuid) as usage_attempt,
            child_usage_scope(child_job_name, child_build_number)
            if child_job_name
            else contextlib.nullcontext(),
        ):
            analyses = await analyze_failure_group(
                failures=[test_failure],
                console_context=console_context,
                repo_path=repo_path,
                ai_provider=ai_provider,
                ai_model=ai_model,
                ai_call_timeout=ai_call_timeout,
                custom_prompt=raw_prompt,
                artifacts_context=artifacts_context,
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
        # get_result attaches the original card's usage without changing storage.
        current = await get_result(job_id, strip_sensitive=False)
        previous_usage = None
        if current and current.get("result"):
            previous = _find_failure_by_uuid_in_result(current["result"], failure_uuid)
            if previous:
                previous_usage = previous.get("token_usage")
        if previous_usage is None:
            logger.info(
                "Previous analysis usage unavailable for failure %s in job %s",
                failure_uuid,
                job_id,
            )

        # Patch the failure in the parent job result on success
        def _patch_success(result_data: dict[str, Any]) -> None:
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
                # Raw stored snapshots can contain legacy mixed primary totals.
                # Only archive usage recalculated from attributable call rows.
                prev_entry.pop("token_usage", None)
                if previous_usage is not None:
                    prev_entry["token_usage"] = previous_usage
                # Keep the previous attempt ID for archived usage, if any.
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
            failure["usage_attempt"] = usage_attempt
            # The persisted usage summary belongs to the previous analysis.
            failure.pop("token_usage", None)
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
        logger.exception(
            "Failure %s re-analysis failed in job %s: %s",
            failure_uuid,
            job_id,
            type(exc).__name__,
        )

        # Patch failure status to "failed" with error message
        def _patch_error(result_data: dict[str, Any]) -> None:
            failure = _find_failure_by_uuid_in_result(result_data, failure_uuid)
            if failure:
                failure["reanalysis_status"] = "failed"
                failure["reanalysis_error"] = error_msg

        try:
            await patch_result_json(job_id, _patch_error)
        except Exception:
            logger.exception(
                "Failed to patch error status for failure %s",
                failure_uuid,
            )

    finally:
        notify_job_status_changed(job_id)
        await _cleanup_ai_session(auth_header)
        if repo_manager:
            try:
                from rootcoz.engine.graft import cleanup_graph
                from rootcoz.engine.graft_http import unregister_workspace

                if repo_path is not None:
                    unregister_workspace(repo_path)
                    cleanup_graph(repo_path)
                repo_manager.cleanup()
            except Exception:
                logger.warning("Failed to cleanup repos", exc_info=True)
        # Clean up downloaded source artifacts
        if source is not None:
            try:
                source.cleanup()
            except Exception:
                logger.warning("Failed to cleanup source artifacts", exc_info=True)


@app.post(
    "/api/failures/{failure_uuid}/re-analyze",
    status_code=202,
    operation_id="reAnalyzeFailure",
)
async def re_analyze_failure(
    failure_uuid: str,
    request: Request,
) -> dict[str, Any]:
    """Re-analyze a single failure in-place within its parent job.

    Patches the failure directly in the parent job's stored result instead
    of creating a new job.  Accepts an optional JSON body with settings
    overrides; defaults come from the parent job's request_params.
    """
    _check_allow_list(request)
    _require_operator(request)

    # Parse optional body
    body_data: dict[str, Any] = {}
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

    # Defer AI validation when a tests repo can supply .rootcoz/settings.json
    # (same as submit-time deferred AI). Background applies settings.json after clone.
    settings = get_settings()
    token = ai_username.set(request.state.username)
    try:
        ai_provider, ai_model = await _resolve_ai_config_allow_defer(
            BaseAnalysisRequest(
                ai_provider=ai_provider or None,
                ai_model=ai_model or None,
                tests_repo_url=tests_repo_url or None,
                force_server_credentials=(
                    overrides.force_server_credentials
                    if overrides.force_server_credentials is not None
                    else decrypted_params.get("force_server_credentials")
                ),
            ),
            settings,
            request=request,
        )
        for peer in peer_ai_configs or []:
            await _validate_catalog_pair(*_peer_catalog_pair(peer))
    finally:
        ai_username.reset(token)

    if "additional_repos" not in decrypted_params:
        additional_repos_list: list[AdditionalRepo] | None = None
    else:
        additional_repos_list_raw = decrypted_params.get("additional_repos")
        if additional_repos_list_raw is None:
            additional_repos_list = None
        else:
            additional_repos_list = [
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

    def _patch_running(result_data: dict[str, Any]) -> None:
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
            analysis_type=decrypted_params.get("analysis_type", "jenkins"),
            source_params={
                **decrypted_params,
                "force_server_credentials": (
                    overrides.force_server_credentials
                    if overrides.force_server_credentials is not None
                    else decrypted_params.get(
                        "force_server_credentials", settings.force_server_credentials
                    )
                ),
            },
            settings=get_settings(),
            child_job_name=match.get("child_job_name", ""),
            child_build_number=match.get("child_build_number", 0),
            is_admin=bool(request.state.is_admin),
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
    children: list[dict[str, Any]],
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


def _child_matches(child: dict[str, Any], job_name: str, build_number: int = 0) -> bool:
    """Return True if *child* matches the given job name and optional build number.

    When ``build_number`` is 0 the match is by name only (wildcard).
    """
    return child.get("job_name") == job_name and (
        build_number == 0 or child.get("build_number") == build_number
    )


def _find_failure_in_children(
    children: list[dict[str, Any]],
    test_name: str,
    child_job_name: str,
    child_build_number: int = 0,
) -> dict[str, Any] | None:
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
    result_data: dict[str, Any],
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> dict[str, Any] | None:
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


def _find_failure_by_uuid_in_child(
    child: dict[str, Any], failure_uuid: str
) -> dict[str, Any] | None:
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
    result_data: dict[str, Any], failure_uuid: str
) -> dict[str, Any] | None:
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
    children: list[dict[str, Any]],
    child_job_name: str,
    child_build_number: int = 0,
) -> dict[str, Any] | None:
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
    result_data: dict[str, Any],
    child_job_name: str,
    child_build_number: int = 0,
) -> dict[str, Any] | None:
    """Find a child job dict in the stored result data."""
    return _find_child_job_in_children(
        result_data.get("child_job_analyses", []),
        child_job_name,
        child_build_number,
    )


async def _get_failure_signatures(
    job_id: str,
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> tuple[str, str]:
    """Return the stored ``(error_signature, error_signature_v2)`` pair for a test.

    Both are written together on new failures (see rootcoz.storage's signature
    notes); a failure stored before the v2 rules existed returns an empty v2.
    """
    stored = await storage.get_result(job_id)
    if not stored or not stored.get("result"):
        return "", ""
    failure = _find_failure_in_result(
        stored["result"],
        test_name,
        child_job_name,
        child_build_number,
    )
    if not failure:
        return "", ""
    return failure.get("error_signature", ""), failure.get("error_signature_v2", "")


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
    updates: dict[str, Any] = {"classification": effective_cls}
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


@app.get("/results/{job_id}/comments", operation_id="getComments")
async def get_comments(job_id: str, _: None = Depends(_bind_job_id)) -> dict[str, Any]:
    """Get all comments and review states for a job."""
    logger.debug(f"GET /results/{job_id}/comments")
    comments = await storage.get_comments_for_job(job_id)
    reviews = await storage.get_reviews_for_job(job_id)
    return {"comments": comments, "reviews": reviews}


@app.post("/results/{job_id}/comments", status_code=201, operation_id="addComment")
async def add_comment(
    job_id: str,
    body: AddCommentRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Add a comment to a test failure."""
    _check_allow_list(request)
    _require_reviewer(request)
    logger.debug(f"POST /results/{job_id}/comments: test_name={body.test_name}")
    await _validate_test_name_in_result(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    error_signature, error_signature_v2 = await _get_failure_signatures(
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
            error_signature_v2=error_signature_v2,
            username=username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Detect @mentions once and reuse for both web-push and SSE notifications
    mentioned = detect_mentions(body.comment)

    settings = get_settings()
    if settings.web_push_enabled and username and mentioned:
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


@app.delete(
    "/results/{job_id}/comments/{comment_id}", operation_id="deleteCommentEndpoint"
)
async def delete_comment_endpoint(
    job_id: str, comment_id: int, request: Request, _: None = Depends(_bind_job_id)
) -> dict[str, Any]:
    """Delete a comment. Username scoping is a UI courtesy.

    Admin users can delete any comment. Regular users can only delete
    their own comments (matched by username).
    """
    _check_allow_list(request)
    _require_reviewer(request)
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


@app.put("/results/{job_id}/reviewed", operation_id="setReviewed")
async def set_reviewed(
    job_id: str,
    body: SetReviewedRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Toggle the reviewed state for a test failure."""
    _require_reviewer(request)
    _check_allow_list(request)
    _require_reviewer(request)
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


@app.post("/results/{job_id}/enrich-comments", operation_id="enrichComments")
async def enrich_comments(
    job_id: str,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Fetch live statuses for GitHub PRs and Jira tickets found in comments.

    Read-only: no storage writes, no external mutations. Any authenticated user
    may call it, since the statuses are rendered beside the comment links a
    viewer can already read.
    """
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/enrich-comments")

    comments = await storage.get_comments_for_job(job_id)
    logger.debug(f"enrich_comments: job_id={job_id}, comments_count={len(comments)}")

    # Jira connection resolution (single source of truth): the selected
    # `jira_auth_mode` decides oauth (scoped gateway) / basic (Cloud) /
    # pat (Server/DC Bearer) / atst (scoped gateway, raw Bearer). Unset
    # mode → jira_enabled is False and the connection is skipped entirely.
    jira_connection: JiraConnection | None = (
        resolve_jira_connection(settings) if settings.jira_enabled else None
    )
    if jira_connection is not None and jira_connection.mode == "none":
        jira_connection = None
    if jira_connection is not None and jira_connection.mode in ("sa", "atst"):
        # Complete the cloud ID and mint the access token ONCE here —
        # otherwise every per-key status task would fire its own
        # tenant_info lookup / token mint on a cold cache. (ATST needs no
        # minting — only the cloud-ID resolution.)
        try:
            jira_connection = await complete_jira_connection(jira_connection)
        except JiraOAuthMintError as exc:
            logger.warning("Comment enrichment: %s; skipping Jira keys", exc)
            jira_connection = None
        if jira_connection is not None and not jira_connection.api_base:
            logger.warning(
                "Comment enrichment: gateway cloud ID unresolvable; skipping Jira keys"
            )
            jira_connection = None

    github_token = (
        settings.github_token.get_secret_value() if settings.github_token else None
    )

    # Collect all enrichment tasks for parallel execution
    tasks: list[Coroutine[Any, Any, Any]] = []
    task_map: dict[int, tuple[str, dict[str, Any]]] = {}

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

        if jira_connection:
            for key in detect_jira_keys(c["comment"]):
                idx = len(tasks)
                tasks.append(fetch_jira_ticket_status(jira_connection, key))
                task_map[idx] = (str(c["id"]), {"type": "jira", "key": key})

    enrichments: dict[str, list[dict[str, Any]]] = {}
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
    settings: Settings, result_data: dict[str, Any]
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
) -> tuple[FailureAnalysis, dict[str, Any], dict[str, Any] | None]:
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
    matched_child: dict[str, Any] | None = None
    if child_job_name:
        matched_child = _find_child_job_in_result(
            result_data, child_job_name, child_build_number
        )
    failure = FailureAnalysis.model_validate(failure_dict)
    failure = await _resolve_effective_failure(
        job_id, failure, child_job_name, child_build_number
    )
    return failure, result_data, matched_child


@app.get("/results/{job_id}/issue-prompt", operation_id="getIssuePrompt")
async def get_issue_prompt(
    job_id: str,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Return the issue generation prompt for a job.

    Resolution order:
    1. Stored ``issue_prompt`` from analysis request params (if provided at submission time)
    2. ``.rootcoz/ROOTCOZ_ISSUE_PROMPT.md`` fetched from the test repo via GitHub Contents API

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

    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/.rootcoz/{ROOTCOZ_ISSUE_PROMPT_FILENAME}"
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
@app.post("/results/{job_id}/preview-github-issue", operation_id="previewGithubIssue")
async def preview_github_issue(
    job_id: str,
    body: PreviewIssueRequest,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Generate preview content for a GitHub issue from a failure analysis."""
    _require_reviewer(request)
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
    ai_provider = body.ai_provider or get_settings().ai_provider
    ai_model = body.ai_model or get_settings().ai_model
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
    token = ai_username.set(request.state.username)
    try:
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
    finally:
        ai_username.reset(token)

    # Duplicate detection (best-effort: failures must not break preview)
    # Uses only user-provided token — no server token fallback.
    # If no user token, skip duplicate detection (preview still works).
    tests_repo_url = _resolve_github_repo_url(settings)
    github_token = (body.github_token or "").strip()
    similar: list[dict[str, Any]] = []
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


@app.post("/results/{job_id}/preview-jira-bug", operation_id="previewJiraBug")
async def preview_jira_bug(
    job_id: str,
    body: PreviewIssueRequest,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Generate preview content for a Jira bug from a failure analysis."""
    _require_reviewer(request)
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/preview-jira-bug: test_name={body.test_name}")
    if not _jira_issue_creation_enabled(settings):
        raise HTTPException(
            status_code=403,
            detail="Jira issue creation is disabled on this server",
        )
    # A legacy (non-ATOA) service-account secret must be rejected, not
    # silently dropped (which would fall back to the server credentials).
    _require_usable_jira_sa_token(body.jira_token, body.jira_sa_token)
    # Effective settings first (Finding 9): the user's credential pins the
    # auth method, so the method-configured check must run against the
    # EFFECTIVE settings — a user-supplied credential can satisfy a method
    # the server's own secret cannot.
    effective_jira_settings = _build_effective_jira_settings(
        settings,
        body.jira_token,
        body.jira_email,
        body.jira_project_key,
        user_jira_sa_token=body.jira_sa_token,
    )
    _require_jira_method_configured(effective_jira_settings)
    failure, result_data, matched_child = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    # AI config is best-effort for preview — fallback content is generated if not configured
    ai_provider = body.ai_provider or get_settings().ai_provider
    ai_model = body.ai_model or get_settings().ai_model
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
    token = ai_username.set(request.state.username)
    try:
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
    finally:
        ai_username.reset(token)

    # Duplicate detection (best-effort: failures must not break preview)
    # Uses only user-provided token — no server token fallback.
    # If no user token, skip duplicate detection (preview still works).
    similar: list[dict[str, Any]] = []
    user_jira_token = (body.jira_token or "").strip()
    user_jira_sa_token = (body.jira_sa_token or "").strip()
    if (
        (user_jira_token or user_jira_sa_token)
        and _has_jira_credentials(effective_jira_settings)
        and jira_method_url(effective_jira_settings)
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
                body.ai_provider
                or request_params.get("ai_provider", "")
                or get_settings().ai_provider
            )
            ai_model = (
                body.ai_model
                or request_params.get("ai_model", "")
                or get_settings().ai_model
            )
            if candidates and ai_provider and ai_model:
                try:
                    token = ai_username.set(request.state.username)
                    try:
                        matches = await filter_matches_with_ai(
                            bug_title=content["title"],
                            bug_description=content["body"],
                            candidates=candidates,
                            ai_provider=ai_provider,
                            ai_model=ai_model,
                            job_id=job_id,
                        )
                    finally:
                        ai_username.reset(token)
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
    """True when an explicitly selected Jira auth method is fully configured.

    Mirrors the resolver: with ``jira_auth_mode`` set, any non-empty token
    for the selected method is usable (no value sniffing), and an unset
    mode disables Jira entirely regardless of stored credentials.
    """
    return bool(
        normalize_jira_auth_mode(settings.jira_auth_mode)
        and not jira_method_missing_fields(settings)
    )


def _require_jira_method_configured(settings: Settings) -> None:
    """400 unless a Jira auth method is selected with all its fields.

    Replaces the old generic "Jira URL is not configured" gate: each
    method is self-contained, so the per-method missing-fields check is
    the only pre-check (URLs included — basic needs ``jira_cloud_url``,
    pat needs ``jira_server_url``, oauth needs the cloud ID).
    """
    mode = normalize_jira_auth_mode(settings.jira_auth_mode)
    if not mode:
        raise HTTPException(
            status_code=400,
            detail="No Jira auth method selected — choose one in Server Settings",
        )
    missing = jira_method_missing_fields(settings)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Jira auth method '{mode}' is selected but missing: "
                f"{', '.join(missing)}"
            ),
        )


def _jira_issue_creation_enabled(settings: Settings) -> bool:
    """Check whether Jira issue creation is enabled.

    Controlled only by ``ENABLE_JIRA_ISSUES``.  Defaults to enabled
    when not explicitly set.  Independent of ``ENABLE_JIRA`` (which
    controls Jira enrichment during analysis).
    """
    return settings.enable_jira_issues is not False


def _rp_has_push_content(settings: Settings) -> bool:
    """True when at least one Report Portal push content toggle is enabled."""
    rp = settings.rp
    return bool(rp.push_classifications or rp.push_rootcoz_url or rp.push_tracker_links)


def _available_exporters(settings: Settings) -> list[ExporterInfo]:
    """Build the list of available exporter plugins with their status."""
    rp_enabled = settings.reportportal_enabled
    if rp_enabled and settings.rp.push_rootcoz_url and not _extract_base_url():
        rp_enabled = False
    if rp_enabled and not _rp_has_push_content(settings):
        # Match ReportPortalClient.push() readiness — avoid advertising a
        # plugin that will hard-fail every push due to all toggles off.
        rp_enabled = False
    return [
        ExporterInfo(
            name=ReportPortalClient.NAME,
            display_name=ReportPortalClient.DISPLAY_NAME,
            enabled=rp_enabled,
        ),
        ExporterInfo(
            name=DatadogExporter.NAME,
            display_name=DatadogExporter.DISPLAY_NAME,
            enabled=settings.datadog_enabled,
        ),
    ]


def _build_capabilities(settings: Settings) -> dict[str, Any]:
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
            or (
                settings.jira_oauth_client_secret
                and settings.jira_oauth_client_secret.get_secret_value()
            )
            or (
                settings.jira_atst_token and settings.jira_atst_token.get_secret_value()
            )
        ),
        "server_jira_email": bool(settings.jira_email),
        "server_jira_project_key": settings.jira_project_key or "",
        "reportportal": settings.reportportal_enabled,
        "datadog": settings.datadog_enabled,
        "exporters": _available_exporters(settings),
        "reportportal_project": settings.reportportal_project or "",
        "feedback_enabled": settings.feedback_enabled
        and bool(settings.ai_provider)
        and bool(settings.ai_model),
    }


def _jira_auth_mode_for_credentials(
    *,
    oauth_token: str | None = None,
    api_token: str | None = None,
    pat: str | None = None,
    email: str | None = None,
) -> str | None:
    """Auth mode pinned by present Jira credentials; None when none supplied.

    Field-presence only — no value sniffing, no fallback chain. Shared by
    ``_build_effective_jira_settings`` (user credentials) and
    ``_merge_settings`` (per-request credentials) so a server-pinned
    ``jira_auth_mode`` cannot silently override or discard them: an OAuth
    2.0 client secret pins ``oauth``, an api token with an email pins
    ``basic``, a bare token (no email / the pat field) pins ``pat``.
    """
    if oauth_token and oauth_token.strip():
        return "oauth"
    if api_token and api_token.strip():
        return "basic" if email and email.strip() else "pat"
    if pat and pat.strip():
        return "pat"
    return None


def _build_effective_jira_settings(
    settings: Settings,
    user_jira_token: str,
    user_jira_email: str,
    user_jira_project_key: str = "",
    user_jira_sa_token: str = "",
) -> Settings:
    """Build effective settings with user Jira credentials overriding server defaults.

    Uses ``model_copy()`` to follow the same pattern as ``_merge_settings()``.

    The auth mode is pinned to the user's credential choice (which field
    they filled in the credentials UI), with one exception:

    Prefix detection wins over field placement: an ATOA-prefixed value
    (OAuth 2.0 credential client secret) routes to ``oauth`` mode and an
    ATST-prefixed scoped token routes to ``atst`` mode (Bearer against
    the api.atlassian.com gateway, no minting) — whichever field they
    arrived in, so validation (which detects the prefixes) and actual
    use agree. The service-account field takes precedence when both
    fields carry classified values.

    A service-account OAuth 2.0 credential client secret replaces the
    server's secret and clears the other credentials so the effective
    credentials are unambiguous; ``jira_auth_mode`` is set to ``oauth``.
    The client ID stays whatever the server configures
    (``atlassian_oauth_client_id``) — the ID and secret must belong to
    the same Atlassian OAuth 2.0 credential.

    Regular token mode: with an email, ``jira_auth_mode`` is ``basic`` and
    the token goes to ``jira_api_token``; without an email the token is a
    bearer PAT — mode ``pat`` with the token in ``jira_pat``. The other
    methods' fields are cleared so the user token takes precedence in all
    auth resolution paths.

    When the user provides a token but no email, ``jira_email`` is also cleared
    to prevent pairing the server's email with the user's token (which would
    incorrectly trigger Cloud Basic auth). Cloud users must explicitly provide
    their email; without it, the bearer path is used, which may fail against
    Cloud-only Jira hosts.

    An optional *user_jira_project_key* overrides the server-level project key
    so that duplicate searches and bug creation target the user's chosen project.
    """
    overrides: dict[str, Any] = {}
    sa_value = (user_jira_sa_token or "").strip()
    token_value = (user_jira_token or "").strip()
    sa_kind = classify_jira_secret(sa_value)
    token_kind = classify_jira_secret(token_value)
    if sa_kind:
        kind, value = sa_kind, sa_value
    elif token_kind:
        kind, value = token_kind, token_value
    else:
        kind, value = "", ""
    if kind == "oauth":
        # OAuth 2.0 credential override — only an ATOA-prefixed client
        # secret selects oauth mode; the client ID stays whatever the
        # server configures (the ID and secret must belong to the same
        # Atlassian OAuth 2.0 credential).
        overrides["jira_oauth_client_secret"] = SecretStr(value)
        overrides["jira_api_token"] = None
        overrides["jira_pat"] = None
        overrides["jira_email"] = None
        overrides["jira_atst_token"] = None
        overrides["jira_auth_mode"] = _jira_auth_mode_for_credentials(oauth_token=value)
    elif kind == "atst":
        # ATST scoped-token override — an already-minted access token sent
        # as Bearer against the api.atlassian.com gateway (atst mode). No
        # minting, so no client ID is needed; the cloud ID and site URL
        # stay whatever the server configures.
        overrides["jira_atst_token"] = SecretStr(value)
        overrides["jira_api_token"] = None
        overrides["jira_pat"] = None
        overrides["jira_email"] = None
        overrides["jira_oauth_client_secret"] = None
        overrides["jira_auth_mode"] = "atst"
    elif token_value:
        email = (
            user_jira_email.strip()
            if user_jira_email and user_jira_email.strip()
            else None
        )
        if email:
            overrides["jira_api_token"] = SecretStr(token_value)
            overrides["jira_pat"] = None
        else:
            # No email: the user token is a bearer PAT, not a basic-auth pair.
            overrides["jira_pat"] = SecretStr(token_value)
            overrides["jira_api_token"] = None
        overrides["jira_auth_mode"] = _jira_auth_mode_for_credentials(
            api_token=token_value, email=email
        )
        overrides["jira_oauth_client_secret"] = None
        overrides["jira_atst_token"] = None
        overrides["jira_email"] = email
    if user_jira_project_key and user_jira_project_key.strip():
        overrides["jira_project_key"] = user_jira_project_key.strip()
    if not overrides:
        return settings
    return settings.model_copy(update=overrides)


def _require_usable_jira_sa_token(jira_token: str, jira_sa_token: str) -> None:
    """400 when an unrecognized SA token is supplied — fail closed.

    The service-account field accepts exactly two token kinds: an ATOA
    OAuth 2.0 credential client secret (routes to the oauth method) and
    an ATST scoped access token (routes to the atst method). Any other
    value is ALWAYS rejected, even when a regular ``jira_token`` is also
    supplied: invalid input is never silently dropped in favor of the
    other credential (which could mask the mistake and run with a
    credential the user did not verify). Shared by every endpoint that
    consumes ``jira_sa_token`` from a request body.

    Raises:
        HTTPException: 400 when the SA token is set but neither
            ATOA- nor ATST-prefixed, regardless of any regular token.
    """
    token = (jira_token or "").strip()
    sa_token = (jira_sa_token or "").strip()
    if sa_token and classify_jira_secret(sa_token) is None:
        logger.info(
            "Rejecting request with an unrecognized Jira service account token%s",
            " alongside a regular token" if token else "",
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "The saved Jira service account token is not a recognized "
                "Atlassian service-account credential (client secrets start "
                "with 'ATOA', scoped tokens with 'ATST'). Re-enter your "
                "token in Profile Settings."
            ),
        )


def _require_tracker_url(result: dict[str, Any], tracker_name: str) -> str:
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
    result: dict[str, Any],
    username: str,
    *,
    tracked_type: str = "",
) -> int:
    """Best-effort auto-add a comment linking to the created tracker issue.

    Also sets the tracked-in URL on the failure_history row when
    *tracked_type* is provided (``"github"`` or ``"jira"``).

    Args:
        tracker_label: Human-readable tracker name (e.g. "GitHub Issue", "Jira Bug").
        job_id: Analysis job ID.
        body: The create-issue request (carries test_name, child_job_name, etc.).
        result: The tracker API response (must contain ``url`` and optionally ``key``).
        username: Username from the request cookie.
        tracked_type: Tracker type for failure_history (``"github"`` or ``"jira"``).

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
        error_signature, error_signature_v2 = await _get_failure_signatures(
            job_id, body.test_name, body.child_job_name, body.child_build_number
        )
        comment_id = await storage.add_comment(
            job_id=job_id,
            test_name=body.test_name,
            comment=comment_text,
            child_job_name=body.child_job_name,
            child_build_number=body.child_build_number,
            error_signature=error_signature,
            error_signature_v2=error_signature_v2,
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

    # Auto-add tracked-in link
    if tracked_type and issue_url:
        try:
            await storage.add_tracked_in_link(
                job_id,
                body.test_name,
                issue_url,
                tracked_type,
                child_job_name=body.child_job_name,
                child_build_number=body.child_build_number,
                tracked_by=username,
            )
        except Exception:
            logger.warning(
                f"Failed to set tracked-in after {tracker_label} creation "
                f"for job_id={job_id}, test_name={body.test_name}",
                exc_info=True,
            )

    return comment_id


@app.post(
    "/results/{job_id}/create-github-issue",
    status_code=201,
    operation_id="createGithubIssueEndpoint",
)
async def create_github_issue_endpoint(
    job_id: str,
    body: CreateIssueRequest,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Create a GitHub issue from a failure analysis."""
    _require_reviewer(request)
    _check_allow_list(request)
    logger.debug(
        f"POST /results/{job_id}/create-github-issue: test_name={body.test_name}"
    )
    if settings.enable_github_issues is False:
        raise HTTPException(
            status_code=403,
            detail="GitHub issue creation is disabled on this server",
        )
    github_token = (body.github_token or "").strip()
    if not github_token:
        raise HTTPException(
            status_code=400,
            detail=("GitHub token is required. Set up your token in Profile Settings."),
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
        # This path always creates from rootcoz's own failure analysis, so the
        # server states the attribution explicitly; a model-specific line in a
        # client-supplied --body is stripped and never decides the footer.
        result = await create_github_issue(
            title=body.title,
            body=issue_body,
            repo_url=tests_repo_url,
            github_token=github_token,
            attribution=AiProvenance(ai_used=True),
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
        "GitHub Issue", job_id, body, result, username, tracked_type="github"
    )

    return {
        "url": issue_url,
        "number": result.get("number", 0),
        "key": "",
        "title": body.title,
        "comment_id": comment_id,
    }


@app.post(
    "/results/{job_id}/create-jira-bug",
    status_code=201,
    operation_id="createJiraBugEndpoint",
)
async def create_jira_bug_endpoint(
    job_id: str,
    body: CreateIssueRequest,
    request: Request,
    settings: Settings = _SETTINGS_DEP,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Create a Jira bug from a failure analysis."""
    _require_reviewer(request)
    _check_allow_list(request)
    logger.debug(f"POST /results/{job_id}/create-jira-bug: test_name={body.test_name}")

    if not _jira_issue_creation_enabled(settings):
        raise HTTPException(
            status_code=403,
            detail="Jira issue creation is disabled on this server",
        )

    # User must provide their own Jira token (regular or service account) —
    # fail fast before DB work
    user_jira_token = (body.jira_token or "").strip()
    user_jira_sa_token = (body.jira_sa_token or "").strip()
    if not user_jira_token and not user_jira_sa_token:
        raise HTTPException(
            status_code=400,
            detail=("Jira token is required. Set up your token in Profile Settings."),
        )
    # A legacy (non-ATOA) service-account secret must be rejected, not
    # silently dropped (which would file the bug under the SERVER's Jira
    # identity).
    _require_usable_jira_sa_token(body.jira_token, body.jira_sa_token)
    # Effective settings first (Finding 9): the user's credential pins the
    # auth method, so the method-configured check must run against the
    # EFFECTIVE settings — a user-supplied credential can satisfy a method
    # the server's own secret cannot.
    effective_jira_settings = _build_effective_jira_settings(
        settings,
        body.jira_token,
        body.jira_email,
        body.jira_project_key,
        user_jira_sa_token=body.jira_sa_token,
    )
    _require_jira_method_configured(effective_jira_settings)

    _failure, _result_data, _matched_child = await _load_effective_failure(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )

    username = request.state.username
    bug_body = body.body
    if username:
        bug_body += f"\n\n----\nReported by: {username} via rootcoz"

    try:
        if not effective_jira_settings.jira_project_key:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Jira project key is required. Provide it in the"
                    " request or configure JIRA_PROJECT_KEY on the server."
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
    except JiraOAuthMintError as exc:
        # OAuth 2.0 credential could not mint an access token — a
        # configuration error, surfaced as a controlled 400.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # Unresolvable SA cloud ID / missing site URL — a configuration
        # error, surfaced as a controlled 400 rather than an unhandled 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    comment_id = await _add_tracker_comment(
        "Jira Bug", job_id, body, result, username, tracked_type="jira"
    )

    return {
        "url": issue_url,
        "key": result.get("key", ""),
        "title": body.title,
        "comment_id": comment_id,
    }


def _detect_tracker_type(url: str) -> str:
    """Detect tracker type from URL string patterns.

    Returns ``'github'``, ``'jira'``, or ``''`` when unrecognized.
    String-only — no HTTP requests (SSRF-safe).
    """
    url_lower = url.lower()
    if "github.com" in url_lower:
        return "github"
    if "jira" in url_lower or "atlassian" in url_lower:
        return "jira"
    return ""


@app.put("/results/{job_id}/tracked-in", operation_id="setTrackedInEndpoint")
async def set_tracked_in_endpoint(
    job_id: str,
    body: SetTrackedInRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Add a tracked-in link for a failure.

    Reviewers+ can add tracked-in links. Use DELETE to remove.
    """
    _require_reviewer(request)
    _check_allow_list(request)
    logger.debug(f"PUT /results/{job_id}/tracked-in: test_name={body.test_name}")
    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")

    # Auto-detect type from URL if not provided
    if not body.url:
        raise HTTPException(
            status_code=400,
            detail="URL is required. Use DELETE to remove links.",
        )

    tracked_type = body.type
    if not tracked_type:
        tracked_type = _detect_tracker_type(body.url)

    link_id = await storage.add_tracked_in_link(
        job_id,
        body.test_name,
        body.url,
        tracked_type,
        child_job_name=body.child_job_name,
        child_build_number=body.child_build_number,
        tracked_by=request.state.username,
    )
    return {
        "status": "ok",
        "id": link_id,
        "test_name": body.test_name,
        "tracked_in_url": body.url,
        "tracked_in_type": tracked_type,
        "tracked_in_by": request.state.username,
    }


@app.get("/results/{job_id}/tracked-in", operation_id="getTrackedInEndpoint")
async def get_tracked_in_endpoint(
    job_id: str,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Return tracked-in data for all failures in a job."""
    _require_reviewer(request)
    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    tracked = await storage.get_tracked_in_for_job(job_id)
    return {"job_id": job_id, "tracked_in": tracked}


@app.delete(
    "/results/{job_id}/tracked-in/{link_id}", operation_id="deleteTrackedInEndpoint"
)
async def delete_tracked_in_endpoint(
    job_id: str,
    link_id: int,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Delete a tracked-in link by ID.

    Creators can delete their own links. Admins can delete any link.
    """
    _require_reviewer(request)
    _check_allow_list(request)
    link = await storage.get_tracked_in_link(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    if link.get("job_id") != job_id:
        raise HTTPException(status_code=404, detail="Link not found")
    if not request.state.is_admin and link["created_by"] != request.state.username:
        raise HTTPException(
            status_code=403,
            detail="You can only delete your own tracked links",
        )
    await storage.delete_tracked_in_link(link_id)
    return {"status": "ok", "deleted_id": link_id}


def _count_result_failures(result_data: dict[str, Any]) -> int:
    """Count failures in a stored result dict, recursing into child analyses.

    Mirrors the auto-review counting (top-level ``failures`` plus every
    ``child_job_analyses`` entry, including nested ``failed_children``).
    """
    total = len(result_data.get("failures", []) or [])
    for child in result_data.get("child_job_analyses", []) or []:
        total += _count_child_result_failures(child)
    return total


def _count_child_result_failures(child: dict[str, Any]) -> int:
    """Count failures in one stored child job analysis, recursively."""
    total = len(child.get("failures", []) or [])
    for nested in child.get("failed_children", []) or []:
        total += _count_child_result_failures(nested)
    return total


async def _unreviewed_failure_count(job_id: str, total_failures: int) -> int:
    """Count a job's failures not yet marked reviewed.

    Shared review-completion check: the auto-push path and the manual push
    endpoints both gate on this. Uses the same count comparison the
    auto-push path has always used — reviewed review entries versus total
    failures.

    Args:
        job_id: Analysis job identifier.
        total_failures: Total failure count (from auto-review counting or
            :func:`_count_result_failures`).

    Returns:
        Number of unreviewed failures (never negative).
    """
    if total_failures <= 0:
        return 0
    reviews = await storage.get_reviews_for_job(job_id)
    reviewed_count = sum(1 for r in reviews.values() if r.get("reviewed"))
    return max(total_failures - reviewed_count, 0)


# Exporter name → Settings field allowing manual pushes of unreviewed jobs.
# Unknown/future exporters have no entry, so they stay gated by default.
_UNREVIEWED_PUSH_TOGGLE_FIELDS: dict[str, str] = {
    ReportPortalClient.NAME: "rp_allow_unreviewed_push",
    DatadogExporter.NAME: "dd_allow_unreviewed_push",
}


def _allows_unreviewed_push(plugin_name: str, settings: Settings) -> bool:
    """Whether the named exporter may push jobs with unreviewed failures.

    Per-exporter escape hatch (``rp_allow_unreviewed_push`` /
    ``dd_allow_unreviewed_push``); exporters without a toggle are gated.
    """
    toggle_field = _UNREVIEWED_PUSH_TOGGLE_FIELDS.get(plugin_name)
    if not toggle_field:
        return False
    return bool(getattr(settings, toggle_field))


async def _gate_unreviewed_push(
    job_id: str,
    result_data: dict[str, Any],
    plugin_name: str,
    settings: Settings,
) -> None:
    """Reject a manual exporter push when the job has unreviewed failures.

    Raises:
        HTTPException: 409 when any failure is unreviewed and the
            exporter's per-plugin toggle does not allow unreviewed pushes.
    """
    unreviewed = await _unreviewed_failure_count(
        job_id, _count_result_failures(result_data)
    )
    if unreviewed and not _allows_unreviewed_push(plugin_name, settings):
        logger.warning(
            "Blocking manual push to '%s' for job %s: %d unreviewed failure(s)",
            plugin_name,
            job_id,
            unreviewed,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"job has {unreviewed} unreviewed failure(s)"
                " — review them before pushing"
            ),
        )


@app.post(
    "/results/{job_id}/push-reportportal",
    response_model=ReportPortalPushResult,
    operation_id="pushToReportportal",
)
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
) -> dict[str, Any]:
    """Push rootcoz classifications into Report Portal test items.

    Finds the matching RP launch, matches failed items to rootcoz failures,
    and updates each item's defect type and comment.
    """
    _require_operator(request)
    _check_allow_list(request)
    username = getattr(request.state, "username", "")
    # Sanitize control characters to prevent log forging (trusted-proxy
    # mode populates username from X-Forwarded-User with minimal validation)
    safe_username = _sanitize_control_chars(username)
    logger.info("RP push requested by '%s' for job %s", safe_username, job_id)
    if not settings.reportportal_enabled:
        raise HTTPException(
            status_code=400,
            detail="Report Portal integration is disabled or not configured",
        )

    stored = await get_result(job_id)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result_data = stored["result"]

    await _gate_unreviewed_push(
        # Same literal _create_exporter matches for the legacy RP endpoint —
        # resilient to the ReportPortalClient symbol being patched in tests.
        job_id,
        result_data,
        "reportportal",
        settings,
    )

    try:
        push_result = await _execute_rp_push(
            job_id,
            result_data,
            settings,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
            pushed_by=safe_username,
        )
        if push_result.get("error_type") == INVALID_STORED_FAILURES:
            errors = push_result.get("errors") or []
            detail = errors[0] if errors else push_result.get("message", "")
            raise HTTPException(
                status_code=422, detail=detail or "Invalid stored failures"
            )
        return push_result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/results/{job_id}/push/{plugin_name}",
    operation_id="pushToExporter",
)
async def push_to_exporter(
    job_id: str,
    request: Request,
    plugin_name: str = ApiPath(pattern=r"^[a-z][a-z0-9_-]{0,49}$"),
    child_job_name: str | None = Query(
        default=None, description="Child job name for pipeline child push"
    ),
    child_build_number: int | None = Query(
        default=None, description="Child build number for pipeline child push"
    ),
    settings: Settings = _SETTINGS_DEP,
    _job: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Push analysis results to an exporter plugin.

    Generic endpoint that dispatches to the named exporter.
    Requires operator role.
    """
    _require_operator(request)
    _check_allow_list(request)
    username = getattr(request.state, "username", "")
    safe_username = _sanitize_control_chars(username)
    logger.info(
        "Exporter push requested by '%s' for job %s, plugin=%s",
        safe_username,
        job_id,
        plugin_name,
    )

    stored = await get_result(job_id)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result_data = stored["result"]

    await _gate_unreviewed_push(job_id, result_data, plugin_name, settings)

    try:
        context = await _build_export_context(
            job_id=job_id,
            result_data=result_data,
            settings=settings,
            child_job_name=child_job_name,
            child_build_number=child_build_number,
            pushed_by=safe_username,
            fetch_history=_exporter_needs_history_classifications([plugin_name]),
            fetch_tracked_in_links=_exporter_needs_tracked_in_links(
                [plugin_name], settings
            ),
            analysis_timestamp=_stored_analysis_timestamp(stored),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        exporter = _create_exporter(plugin_name, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TimeoutError, OSError, TypeError, RuntimeError) as exc:
        # Constructor/transport failures are server-side, not bad client input.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        with exporter:
            result = await exporter.push(context)
    except Exception as exc:
        # Guard against unexpected exporter failures leaking as raw 500s.
        # Expected error modes are already returned inside push() as a
        # failed ExporterResult; this catches genuinely unforeseen faults.
        logger.exception(
            "Exporter '%s' push raised unexpectedly for job %s",
            plugin_name,
            job_id,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Exporter '{plugin_name}' failed unexpectedly",
        ) from exc

    return {**result.details, "success": result.success, "message": result.message}


def _rp_push_error_result(
    message: str,
    *,
    launch_id: int | None = None,
) -> dict[str, Any]:
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
    build_number: int | str | None = None,
    jenkins_url: str = "",
    launch_id: int | None = None,
) -> dict[str, Any]:
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

    Thin wrapper around :func:`rootcoz.exporters.reportportal.format_rp_error`
    kept for call-sites and unit tests in ``main``.
    """
    return format_rp_error(exc, operation)


def _stored_analysis_timestamp(stored: dict[str, Any]) -> int | None:
    """Unix timestamp of a stored job's analysis, or ``None`` when unknown.

    Prefers the completion time, then the analysis start, then job creation.
    SQLite ``CURRENT_TIMESTAMP`` values are UTC; naive strings are parsed
    as UTC so the timestamp is absolute.
    """
    for key in ("completed_at", "analysis_started_at", "created_at"):
        value = stored.get(key)
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=UTC)
            return int(dt.timestamp())
        if isinstance(value, str) and value.strip():
            try:
                dt = datetime.fromisoformat(value.strip())
            except ValueError:
                continue
            dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            return int(dt.timestamp())
    return None


async def _build_export_context(
    *,
    job_id: str,
    result_data: dict[str, Any],
    settings: Settings,
    child_job_name: str | None = None,
    child_build_number: int | None = None,
    pushed_by: str = "",
    fetch_history: bool = True,
    fetch_tracked_in_links: bool = True,
    analysis_timestamp: int | None = None,
) -> ExportContext:
    """Build an ExportContext from stored result data.

    Handles child job scoping, report URL, history classifications,
    tracked-in links, and reviewer resolution.

    Args:
        job_id: The analysis job identifier.
        result_data: Stored result dict containing failures and Jenkins metadata.
        settings: Application settings.
        child_job_name: Optional child job name for scoped push.
        child_build_number: Optional child build number.
        pushed_by: Username of the user who triggered the push.
        fetch_history: When ``False``, skip the per-test history classification
            lookups (they hit the DB once per unique test name).  Set to
            ``False`` when no target exporter reads
            ``ExportContext.history_classifications`` — see
            :func:`_exporter_needs_history_classifications`.
        fetch_tracked_in_links: When ``False``, skip
            ``storage.get_tracked_in_for_scope``.  Set to ``False`` when no
            target exporter will read ``ExportContext.tracked_in_links`` —
            see :func:`_exporter_needs_tracked_in_links`.
        analysis_timestamp: Unix timestamp of the analysis, when the caller
            knows it (e.g. from the stored results row).  ``None`` lets
            exporters fall back to push time.

    Returns:
        Populated ExportContext.

    Raises:
        ValueError: Invalid child job parameters.
    """
    base_url = _extract_base_url()
    report_url = f"{base_url}/results/{job_id}" if base_url else ""

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
        result_data = child
        anchor = f"child-{_urlquote(child_job_name, safe='')}-{child_build_number}"
        report_url = f"{report_url}#{anchor}"
    elif child_build_number is not None:
        raise ValueError("child_build_number requires child_job_name to be set")

    failures_data = result_data.get("failures", [])

    # Build history classifications, tracked-in links, and reviewer data
    history_classifications: dict[str, str] = {}
    tracked_in_data: dict[str, list[dict[str, Any]]] = {}
    reviewed_by: dict[str, str] = {}

    if failures_data:
        if fetch_history:
            seen: set[str] = set()
            test_names: list[str] = []
            for f in failures_data:
                if not isinstance(f, dict):
                    continue
                tn = f.get("test_name", "")
                if tn and tn not in seen:
                    seen.add(tn)
                    test_names.append(tn)

            scope_name = child_job_name or ""
            scope_build = child_build_number or 0
            classification_results = await run_parallel_with_limit(
                [
                    get_history_classification(job_id, name, scope_name, scope_build)
                    for name in test_names
                ]
            )
            for name, result in zip(test_names, classification_results, strict=True):
                if isinstance(result, BaseException):
                    logger.debug(
                        "Export: failed to fetch history classification"
                        " for test='%s', job_id='%s'",
                        name,
                        job_id,
                        exc_info=result,
                    )
                    continue
                if result:
                    history_classifications[name] = result

        if fetch_tracked_in_links:
            try:
                tracked_in_data = await storage.get_tracked_in_for_scope(
                    job_id,
                    child_job_name=child_job_name or "",
                    child_build_number=child_build_number or 0,
                )
            except Exception:
                logger.warning(
                    "Failed to fetch tracked-in links for export push, job_id=%s",
                    job_id,
                    exc_info=True,
                )

        try:
            reviews = await storage.get_reviews_for_job(job_id)
            scope_name = child_job_name or ""
            scope_build = child_build_number or 0
            for f in failures_data:
                if not isinstance(f, dict):
                    continue
                name = f.get("test_name", "")
                if not name or name in reviewed_by:
                    continue
                entry = resolve_failure_review(reviews, name, scope_name, scope_build)
                if entry:
                    safe = _sanitize_control_chars(entry["username"])
                    if safe:
                        reviewed_by[name] = safe
        except Exception:
            logger.debug("Failed to fetch reviews for job %s", job_id, exc_info=True)

        if reviewed_by:
            logger.debug("Export push reviewed_by for job %s: %s", job_id, reviewed_by)

    return ExportContext(
        job_id=job_id,
        job_name=result_data.get("job_name", ""),
        build_number=str(resolve_display_build_id(result_data)),
        jenkins_url=result_data.get("jenkins_url", ""),
        failures=failures_data,
        report_url=report_url,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        pushed_by=pushed_by,
        history_classifications=history_classifications,
        tracked_in_links=tracked_in_data,
        reviewed_by=reviewed_by,
        analysis_timestamp=analysis_timestamp,
    )


# Exporter plugin classes by machine name, used to read static capabilities
# (e.g. ``needs_history_classifications``) without instantiating an exporter.
# Instance construction with per-exporter config validation lives in
# ``_create_exporter()``.
_EXPORTER_CLASSES: dict[str, type[Exporter]] = {
    ReportPortalClient.NAME: ReportPortalClient,
    DatadogExporter.NAME: DatadogExporter,
}


def _exporter_needs_history_classifications(plugin_names: Iterable[str]) -> bool:
    """Whether any named exporter consumes per-test history classifications.

    Lets the push pipeline skip the per-test, DB-backed history classification
    lookups in :func:`_build_export_context` when no target exporter will use
    them.  Unknown names are ignored here — :func:`_create_exporter` raises for
    them when the push is actually attempted.

    Args:
        plugin_names: Exporter identifiers to check.

    Returns:
        ``True`` if at least one known exporter needs history classifications.
    """
    return any(
        name in _EXPORTER_CLASSES
        and _EXPORTER_CLASSES[name].needs_history_classifications
        for name in plugin_names
    )


# Exporter name → Settings field gating tracked-in link fetches: when the
# exporter's tracker-links content toggle is off, the push pipeline skips
# ``storage.get_tracked_in_for_scope`` for it.
_TRACKED_IN_LINKS_TOGGLE_FIELDS: dict[str, str] = {
    ReportPortalClient.NAME: "rp_push_tracker_links",
    DatadogExporter.NAME: "dd_push_tracker_links",
}


def _exporter_needs_tracked_in_links(
    plugin_names: Iterable[str], settings: Settings
) -> bool:
    """Whether any named exporter consumes tracked-in link lookups.

    Lets the push pipeline skip ``storage.get_tracked_in_for_scope`` when no
    target exporter will read ``ExportContext.tracked_in_links``. Exporters
    with a tracker-links content toggle additionally require it to be on
    (``rp_push_tracker_links`` / ``dd_push_tracker_links``).

    Args:
        plugin_names: Exporter identifiers to check.
        settings: Application settings (exporter content toggles).

    Returns:
        ``True`` if at least one target exporter needs tracked-in links.
    """
    for name in plugin_names:
        if name not in _EXPORTER_CLASSES:
            continue
        if not _EXPORTER_CLASSES[name].needs_tracked_in_links:
            continue
        toggle_field = _TRACKED_IN_LINKS_TOGGLE_FIELDS.get(name)
        if toggle_field and not getattr(settings, toggle_field):
            continue
        return True
    return False


def _create_exporter(plugin_name: str, settings: Settings) -> Exporter:
    """Create an exporter instance by plugin name.

    Args:
        plugin_name: Exporter identifier (e.g. ``'reportportal'``).
        settings: Application settings for exporter configuration.

    Returns:
        Configured exporter instance.

    Raises:
        ValueError: Unknown plugin name or missing configuration.
    """
    if plugin_name == "reportportal":
        if not settings.reportportal_enabled:
            raise ValueError("Report Portal integration is disabled or not configured")
        rp = settings.rp
        if not _rp_has_push_content(settings):
            raise ValueError(
                "All Report Portal push content toggles are disabled. "
                "Enable at least one of: rp_push_classifications, "
                "rp_push_rootcoz_url, rp_push_tracker_links."
            )
        if rp.push_rootcoz_url and not _extract_base_url():
            raise ValueError(
                "PUBLIC_BASE_URL must be set to push rootcoz URL to Report Portal"
                " (relative URLs resolve against the RP domain)"
            )
        if rp.url is None:
            raise ValueError("reportportal_url is required")
        if rp.api_token is None or not rp.api_token.get_secret_value():
            raise ValueError("reportportal_api_token is required")
        if rp.project is None:
            raise ValueError("reportportal_project is required")
        return ReportPortalClient(
            url=rp.url,
            token=rp.api_token.get_secret_value(),
            project=rp.project,
            verify_ssl=rp.verify_ssl,
            push_classifications=rp.push_classifications,
            push_rootcoz_url=rp.push_rootcoz_url,
            push_tracker_links=rp.push_tracker_links,
        )
    if plugin_name == DatadogExporter.NAME:
        if not settings.datadog_enabled:
            raise ValueError("Datadog integration is disabled or not configured")
        api_key = settings.datadog_api_key
        if api_key is None or not api_key.get_secret_value():
            raise ValueError("datadog_api_key is required")
        if not settings.datadog_site:
            raise ValueError("datadog_site is required")
        return DatadogExporter(
            api_key=api_key.get_secret_value(),
            site=settings.datadog_site,
            service=settings.datadog_service,
            env=settings.datadog_env,
            push_classifications=settings.dd_push_classifications,
            push_rootcoz_url=settings.dd_push_rootcoz_url,
            push_tracker_links=settings.dd_push_tracker_links,
        )
    raise ValueError(f"Unknown exporter plugin: '{plugin_name}'")


async def _execute_rp_push(
    job_id: str,
    result_data: dict[str, Any],
    settings: Settings,
    *,
    child_job_name: str | None = None,
    child_build_number: int | None = None,
    pushed_by: str = "",
) -> dict[str, Any]:
    """Shared logic for pushing classifications to Report Portal.

    Builds an ExportContext and delegates to ReportPortalClient.push().

    Args:
        job_id: The analysis job identifier.
        result_data: Stored result dict containing failures and Jenkins metadata.
        settings: Application settings with Report Portal configuration.
        child_job_name: Optional child job name for scoping push to a child.
        child_build_number: Optional child build number (required with child_job_name).
        pushed_by: Username of the user who triggered the push.

    Returns:
        Dict with keys: ``pushed``, ``unmatched``, ``errors``, ``launch_id``.
    """
    context = await _build_export_context(
        job_id=job_id,
        result_data=result_data,
        settings=settings,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        pushed_by=pushed_by,
        fetch_history=_exporter_needs_history_classifications(["reportportal"]),
        fetch_tracked_in_links=_exporter_needs_tracked_in_links(
            ["reportportal"], settings
        ),
    )

    try:
        exporter = _create_exporter("reportportal", settings)
    except ValueError as exc:
        return _log_and_return_rp_error(str(exc))
    except (TimeoutError, OSError, TypeError, RuntimeError) as exc:
        # Preserve legacy constructor-failure shaping for the RP endpoint.
        user_msg, log_msg = _rp_error_message(
            exc,
            "connecting to Report Portal",
        )
        try:
            rp_host = (
                urlparse(settings.rp.url).netloc.rsplit("@", 1)[-1] or "unknown"
                if settings.rp.url
                else "unknown"
            )
        except ValueError, TypeError, AttributeError:
            rp_host = "unknown"
        log_msg = f"{log_msg}, reportportal_host='{rp_host}'"
        return _log_and_return_rp_error(user_msg, log_msg=log_msg)

    with exporter:
        exporter_result = await exporter.push(context)

    return exporter_result.details


def _patch_failures(
    failures: list[dict[str, Any]],
    test_name: str,
    field: str,
    value: str,
    *,
    extra_patch: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Patch ``analysis[field] = value`` for matching failures in a list.

    Uses ``setdefault`` so the analysis dict is created and attached to the
    failure when the key is missing.

    Args:
        extra_patch: Optional callback for classification-specific cleanup
            (clearing stale subtype fields like code_fix / product_bug_report).
    """
    for f in failures:
        if f.get("test_name") == test_name:
            analysis = f.setdefault("analysis", {})
            if isinstance(analysis, dict):
                analysis[field] = value
                if extra_patch:
                    extra_patch(analysis)


def _classification_extra_patch(
    classification: str,
) -> Callable[[dict[str, Any]], None] | None:
    """Return an extra_patch callback that clears stale subtype fields."""

    def _patch(analysis: dict[str, Any]) -> None:
        if classification == "CODE ISSUE":
            analysis.pop("product_bug_report", None)
        elif classification == "PRODUCT BUG":
            analysis.pop("code_fix", None)
        elif classification == "INFRASTRUCTURE":
            analysis.pop("product_bug_report", None)
            analysis.pop("code_fix", None)

    return _patch


def _apply_override_to_failures(
    result_data: dict[str, Any],
    test_name: str,
    field: str,
    value: str,
    child_job_name: str,
    child_build_number: int,
    *,
    extra_patch: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Mutate *result_data* to apply an override to matching failures.

    Traverses top-level failures (when *child_job_name* is empty) or
    child_job_analyses / nested failed_children recursively.
    """
    if child_job_name:
        for child in result_data.get("child_job_analyses", []):
            if _child_matches(child, child_job_name, child_build_number):
                _patch_failures(
                    child.get("failures", []),
                    test_name,
                    field,
                    value,
                    extra_patch=extra_patch,
                )
            _apply_override_to_children(
                child.get("failed_children", []),
                test_name,
                field,
                value,
                child_job_name,
                child_build_number,
                extra_patch=extra_patch,
            )
    else:
        _patch_failures(
            result_data.get("failures", []),
            test_name,
            field,
            value,
            extra_patch=extra_patch,
        )


def _apply_override_to_children(
    children: list[dict[str, Any]],
    test_name: str,
    field: str,
    value: str,
    child_job_name: str,
    child_build_number: int,
    *,
    extra_patch: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Recursively patch ``analysis[field]`` in nested children."""
    for child in children:
        if _child_matches(child, child_job_name, child_build_number):
            _patch_failures(
                child.get("failures", []),
                test_name,
                field,
                value,
                extra_patch=extra_patch,
            )
        _apply_override_to_children(
            child.get("failed_children", []),
            test_name,
            field,
            value,
            child_job_name,
            child_build_number,
            extra_patch=extra_patch,
        )


def _apply_classification_override(
    result_data: dict[str, Any],
    test_name: str,
    classification: str,
    child_job_name: str,
    child_build_number: int,
) -> None:
    """Mutate result_data to apply a classification override to matching failures."""
    _apply_override_to_failures(
        result_data,
        test_name,
        "classification",
        classification,
        child_job_name,
        child_build_number,
        extra_patch=_classification_extra_patch(classification),
    )


def _apply_pattern_override(
    result_data: dict[str, Any],
    test_name: str,
    pattern: str,
    child_job_name: str,
    child_build_number: int,
) -> None:
    """Mutate result_data to apply a pattern override to matching failures."""
    _apply_override_to_failures(
        result_data,
        test_name,
        "pattern",
        pattern,
        child_job_name,
        child_build_number,
    )


@app.put("/results/{job_id}/tags", operation_id="updateTags")
async def update_tags(
    job_id: str,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Update tags on an existing result. System tags (re-analyze, submitter username) cannot be removed."""
    _require_reviewer(request)
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
        if normalized and normalized not in seen and normalized not in _SYSTEM_TAGS:
            seen.add(normalized)
            tags.append(normalized)

    stored = await get_result(job_id)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail=f"Result {job_id} not found")

    result_data = stored["result"]
    old_tags = result_data.get("tags", [])

    # Preserve system tags
    # re-analyze tag
    if "re-analyze" in [str(t).lower() for t in old_tags] and "re-analyze" not in tags:
        tags.append("re-analyze")
    # Submitter tag
    submitted_by = (result_data.get("request_params") or {}).get("submitted_by", "")
    if submitted_by:
        tags = _ensure_submitter_tag(tags, submitted_by)

    await patch_result_json(job_id, lambda d: d.update({"tags": tags}))
    notify_dashboard_changed()
    return {"job_id": job_id, "tags": tags}


@app.put(
    "/results/{job_id}/override-classification",
    operation_id="overrideClassificationEndpoint",
)
async def override_classification_endpoint(
    job_id: str,
    body: OverrideClassificationRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Override the classification of a failure (CODE ISSUE, PRODUCT BUG, or INFRASTRUCTURE)."""
    _check_allow_list(request)
    _require_reviewer(request)
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
    def _patch_group(rd: dict[str, Any]) -> None:
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


@app.put("/results/{job_id}/override-pattern", operation_id="overridePatternEndpoint")
async def override_pattern_endpoint(
    job_id: str,
    body: OverridePatternRequest,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Override the pattern axis of a failure (NEW, REGRESSION, FLAKY, etc.)."""
    _check_allow_list(request)
    _require_reviewer(request)
    logger.debug(
        f"PUT /results/{job_id}/override-pattern: test_name={body.test_name}, "
        f"pattern={body.pattern}"
    )
    await _validate_test_name_in_result(
        job_id, body.test_name, body.child_job_name, body.child_build_number
    )
    username = request.state.username

    parent_job_name = await storage.get_parent_job_name_for_test(
        body.test_name, job_id=job_id
    )

    group_tests = await storage.override_pattern(
        job_id=job_id,
        test_name=body.test_name,
        pattern=body.pattern,
        child_job_name=body.child_job_name,
        child_build_number=body.child_build_number,
        username=username,
        parent_job_name=parent_job_name,
    )

    def _patch_group(rd: dict[str, Any]) -> None:
        for t in group_tests:
            _apply_pattern_override(
                rd,
                t,
                body.pattern,
                body.child_job_name,
                body.child_build_number,
            )

    try:
        await patch_result_json(job_id, _patch_group)
    except Exception:
        logger.warning(
            f"Failed to patch stored result_json after pattern override for job_id={job_id}",
            exc_info=True,
        )

    return {"status": "ok", "pattern": body.pattern}


@app.get("/results/{job_id}/review-status", operation_id="getReviewStatus")
async def get_review_status(
    job_id: str, _: None = Depends(_bind_job_id)
) -> dict[str, Any]:
    """Get review summary for a job (used by dashboard)."""
    logger.debug(f"GET /results/{job_id}/review-status")
    return await storage.get_review_status(job_id)


@app.get("/results", operation_id="listJobResults")
async def list_job_results(
    limit: int = Query(50, le=100),
    analysis_state: str = Query(
        default="",
        description="Filter: submitted, analyzed (empty = all)",
    ),
) -> list[dict[str, Any]]:
    """List recent analysis jobs."""
    logger.debug(f"GET /results: limit={limit}")
    try:
        return await list_results(limit, analysis_state=analysis_state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/results/bulk", operation_id="bulkDeleteJobsEndpoint")
async def bulk_delete_jobs_endpoint(
    body: BulkDeleteRequest, request: Request
) -> dict[str, Any]:
    """Delete multiple jobs and all related data. Operator+ only.

    Operators can only delete their own jobs; admins can delete any.
    """
    _check_allow_list(request)
    _require_operator(request)

    # Operators can only delete their own jobs; filter unauthorized ones
    job_ids = body.job_ids
    unauthorized_ids: list[str] = []
    if not request.state.is_admin:
        username = request.state.username
        submitters = await storage.get_job_submitters(job_ids)
        job_ids = [jid for jid in job_ids if submitters.get(jid) == username]
        unauthorized_ids = [jid for jid in body.job_ids if jid not in job_ids]

    origins = await _reanalysis_origins(job_ids)
    result = await storage.delete_jobs_bulk(job_ids)
    # A job whose deletion failed keeps its row, so its origin keeps the link.
    deleted_ids = set(result["deleted"])
    await _unlink_reanalysis_origins(
        {jid: origin_id for jid, origin_id in origins.items() if jid in deleted_ids}
    )
    await _cleanup_revoked_ai_sessions()
    result["unauthorized"] = unauthorized_ids

    for job_id in result["deleted"]:
        await _cleanup_deleted_job_chat_workspaces(job_id)

    notify_active_count_changed()
    notify_dashboard_changed()

    # Audit log each deletion individually
    actor = request.state.username
    for job_id in result["deleted"]:
        logger.info(f"[AUDIT] User '{actor}' deleted job {job_id}")

    return result


@app.delete("/results/{job_id}", operation_id="deleteJobEndpoint")
async def delete_job_endpoint(
    job_id: str, request: Request, _: None = Depends(_bind_job_id)
) -> dict[str, Any]:
    """Delete an analyzed job and all related data.

    Operators can delete their own jobs; admins can delete any.
    """
    _check_allow_list(request)
    _require_operator(request)

    result = await storage.get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Operators can only delete their own jobs
    if (
        not request.state.is_admin
        and _get_job_submitter(result) != request.state.username
    ):
        raise HTTPException(
            status_code=403,
            detail="You can only delete jobs you submitted",
        )

    origins = await _reanalysis_origins([job_id])
    await storage.delete_job(job_id)
    await _unlink_reanalysis_origins(origins)
    await _cleanup_revoked_ai_sessions()
    await _cleanup_deleted_job_chat_workspaces(job_id)

    notify_active_count_changed()
    notify_dashboard_changed()
    logger.info(f"[AUDIT] User '{request.state.username}' deleted job {job_id}")
    return {"status": "deleted", "job_id": job_id}


@app.post("/results/{job_id}/abort", operation_id="abortAnalysis")
async def abort_analysis(
    job_id: str,
    request: Request,
    _: None = Depends(_bind_job_id),
) -> dict[str, Any]:
    """Abort a running or waiting analysis."""
    _check_allow_list(request)
    _require_reviewer(request)

    result = await storage.get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")

    # Enforce ownership: only submitter or admin can abort
    username = request.state.username
    is_admin_user = getattr(request.state, "is_admin", False)
    submitter = _get_job_submitter(result)

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
    abort_data: dict[str, Any] = {
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


@app.get("/api/dashboard/active-count", operation_id="getActiveAnalysisCount")
async def get_active_analysis_count() -> dict[str, Any]:
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


@app.get("/api/navbar/stream", operation_id="streamNavbarCounts")
async def stream_navbar_counts(request: Request) -> StreamingResponse:
    """SSE stream that pushes active analysis count, unread mention count and
    (admins only) the pending user approval count."""
    username = request.state.username
    is_admin = getattr(request.state, "is_admin", False)
    _check_allow_list(request)

    async def event_generator() -> AsyncIterator[str]:
        # Per-connection events
        active_event = asyncio.Event()
        mention_event = asyncio.Event() if username else None
        pending_event = asyncio.Event() if is_admin else None

        # Register
        _active_count_listeners.add(active_event)
        if username and mention_event is not None:
            _mention_listeners.setdefault(username, set()).add(mention_event)
        if pending_event is not None:
            _pending_count_listeners.add(pending_event)
        pending_feed = _PendingFeed(pending_event, username, "pending-count")

        try:
            # Send both counts immediately on connect
            try:
                active = await storage.count_active_analyses()
                last_active = active
                yield f"event: active-count\ndata: {active}\n\n"
            except aiosqlite.Error, OSError, TypeError, ValueError:
                last_active = 0
                yield "event: active-count\ndata: 0\n\n"

            last_unread = -1
            if username:
                try:
                    unread = await storage.get_unread_mention_count(username)
                    last_unread = unread
                    yield f"event: unread-count\ndata: {unread}\n\n"
                except aiosqlite.Error, OSError, TypeError, ValueError:
                    last_unread = 0
                    yield "event: unread-count\ndata: 0\n\n"

            if chunk := await pending_feed.step():
                yield chunk

            active_wait_tasks: list[asyncio.Task[Any]] = []
            while True:
                # Wait for either event or timeout
                active_wait_tasks = [asyncio.create_task(active_event.wait())]
                if mention_event is not None:
                    active_wait_tasks.append(asyncio.create_task(mention_event.wait()))
                if pending_event is not None:
                    active_wait_tasks.append(asyncio.create_task(pending_event.wait()))

                try:
                    done, pending = await asyncio.wait(
                        active_wait_tasks,
                        timeout=pending_feed.wait_timeout(),
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

                if not done and not pending_feed.retry_due():
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

                if pending_event is not None and (
                    pending_event.is_set() or pending_feed.retry_due()
                ):
                    pending_event.clear()
                    if chunk := await pending_feed.step():
                        yield chunk
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
            if pending_event is not None:
                _pending_count_listeners.discard(pending_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/dashboard/stream", operation_id="streamDashboard")
async def stream_dashboard(request: Request) -> StreamingResponse:
    """SSE stream that notifies when the dashboard job list changes."""
    _check_allow_list(request)
    return _make_sse_stream(request, _dashboard_listeners, "dashboard-changed")


@app.get("/api/results/{job_id}/stream", operation_id="streamJobStatus")
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


@app.get("/api/results/{job_id}/comments/stream", operation_id="streamComments")
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


@app.get("/api/admin/token-usage/stream", operation_id="streamTokenUsage")
async def stream_token_usage(request: Request) -> StreamingResponse:
    """SSE stream that notifies when token usage data changes."""
    _require_admin(request)
    return _make_sse_stream(request, _token_usage_listeners, "usage-changed")


@app.get("/api/stream", operation_id="streamMultiplexed")
async def stream_multiplexed(
    request: Request,
    topics: str = Query("", description="Comma-separated topic subscriptions"),
) -> StreamingResponse:
    """Multiplexed SSE endpoint — one connection for all event topics.

    Accepts a comma-separated ``topics`` query parameter.  Each topic
    registers the connection into the matching listener set so that a
    single SSE connection can receive events from multiple sources.

    Supported topics:

    - ``navbar`` — active analysis count + unread mention count + pending
      user approval count (pending count is admin only)
    - ``dashboard`` — job list changes
    - ``results:{job_id}`` — per-job status changes
    - ``comments:{job_id}`` — per-job comment changes
    - ``chat:{job_id}`` — per-job chat message changes
    - ``token-usage`` — token usage changes (admin only)
    - ``settings`` — server settings changes (admin only)
    """
    _check_allow_list(request)
    username = getattr(request.state, "username", "")
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    is_admin = getattr(request.state, "is_admin", False)

    topic_list = list(dict.fromkeys(t.strip() for t in topics.split(",") if t.strip()))
    if not topic_list:
        raise HTTPException(status_code=400, detail="No topics specified")
    if len(topic_list) > 50:
        raise HTTPException(status_code=400, detail="Too many topics (max 50)")

    # Validate topics and build registration plan
    # Each entry: (event_prefix, asyncio.Event, listener_set_or_dict, key_or_none)
    registrations: list[
        tuple[
            str,
            asyncio.Event,
            set[asyncio.Event] | None,
            dict[str, set[asyncio.Event]] | None,
            str,
        ]
    ] = []
    navbar_requested = False

    for topic in topic_list:
        if topic == "navbar":
            navbar_requested = True
            # navbar has custom handling (active + mention events)
            continue
        elif topic == "dashboard":
            ev = asyncio.Event()
            registrations.append(("dashboard", ev, _dashboard_listeners, None, ""))
        elif topic.startswith("results:"):
            job_id = topic[len("results:") :]
            if not job_id or not _VALID_JOB_ID_RE.match(job_id):
                continue
            ev = asyncio.Event()
            registrations.append(
                (f"results:{job_id}", ev, None, _job_status_listeners, job_id)
            )
        elif topic.startswith("comments:"):
            job_id = topic[len("comments:") :]
            if not job_id or not _VALID_JOB_ID_RE.match(job_id):
                continue
            ev = asyncio.Event()
            registrations.append(
                (f"comments:{job_id}", ev, None, _comment_listeners, job_id)
            )
        elif topic.startswith("chat:"):
            job_id = topic[len("chat:") :]
            if not job_id or not _VALID_JOB_ID_RE.match(job_id):
                continue
            listener_key = f"{job_id}:{username}" if username else job_id
            ev = asyncio.Event()
            registrations.append(
                (f"chat:{job_id}", ev, None, _chat_listeners, listener_key)
            )
        elif topic == "token-usage":
            if not is_admin:
                continue  # silently skip admin-only topics for non-admins
            ev = asyncio.Event()
            registrations.append(("token-usage", ev, _token_usage_listeners, None, ""))
        elif topic == "settings":
            if not is_admin:
                continue
            ev = asyncio.Event()
            registrations.append(("settings", ev, _settings_listeners, None, ""))
        elif topic == "admin-chat":
            if not is_admin:
                continue
            listener_key = (
                f"{ADMIN_CHAT_JOB_ID}:{username}" if username else ADMIN_CHAT_JOB_ID
            )
            ev = asyncio.Event()
            registrations.append(
                ("admin-chat", ev, None, _chat_listeners, listener_key)
            )
        # Unknown topics are silently ignored

    if not registrations and not navbar_requested:
        raise HTTPException(status_code=400, detail="No valid topics specified")

    async def event_generator() -> AsyncIterator[str]:
        # Register all events in their listener sets
        for _prefix, ev, global_set, per_key_dict, key in registrations:
            if per_key_dict is not None:
                per_key_dict.setdefault(key, set()).add(ev)
            elif global_set is not None:
                global_set.add(ev)

        # Navbar events (special handling)
        active_event: asyncio.Event | None = None
        mention_event: asyncio.Event | None = None
        pending_event: asyncio.Event | None = None
        last_active = -1
        last_unread = -1

        if navbar_requested:
            active_event = asyncio.Event()
            _active_count_listeners.add(active_event)
            if username:
                mention_event = asyncio.Event()
                _mention_listeners.setdefault(username, set()).add(mention_event)
            if is_admin:
                pending_event = asyncio.Event()
                _pending_count_listeners.add(pending_event)
        pending_feed = _PendingFeed(pending_event, username, "navbar:pending-count")

        wait_tasks: list[asyncio.Task[Any]] = []

        try:
            # Send initial navbar data on connect
            if navbar_requested:
                try:
                    active = await storage.count_active_analyses()
                    last_active = active
                    yield f"event: navbar:active-count\ndata: {active}\n\n"
                except aiosqlite.Error, OSError, TypeError, ValueError:
                    last_active = 0
                    yield "event: navbar:active-count\ndata: 0\n\n"
                if username:
                    try:
                        unread = await storage.get_unread_mention_count(username)
                        last_unread = unread
                        yield f"event: navbar:unread-count\ndata: {unread}\n\n"
                    except aiosqlite.Error, OSError, TypeError, ValueError:
                        last_unread = 0
                        yield "event: navbar:unread-count\ndata: 0\n\n"
                if chunk := await pending_feed.step():
                    yield chunk

            while True:
                # Build wait list from all registered events
                wait_tasks = []
                all_events: list[tuple[str, asyncio.Event]] = []
                for prefix, ev, _gs, _pkd, _k in registrations:
                    all_events.append((prefix, ev))
                if active_event is not None:
                    all_events.append(("navbar:active", active_event))
                if mention_event is not None:
                    all_events.append(("navbar:mention", mention_event))
                if pending_event is not None:
                    all_events.append(("navbar:pending", pending_event))

                wait_tasks = [asyncio.create_task(ev.wait()) for _, ev in all_events]

                try:
                    done, pending = await asyncio.wait(
                        wait_tasks,
                        timeout=pending_feed.wait_timeout(),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    wait_tasks = []
                except asyncio.CancelledError:
                    for task in wait_tasks:
                        task.cancel()
                    wait_tasks = []
                    break

                if not done and not pending_feed.retry_due():
                    yield ": keepalive\n\n"
                    continue

                if await request.is_disconnected():
                    break

                # Emit events for all fired topics, plus a due pending retry
                retry = pending_feed.retry_due()
                for prefix, ev in all_events:
                    if not ev.is_set() and not (retry and prefix == "navbar:pending"):
                        continue
                    ev.clear()

                    if prefix == "navbar:active":
                        try:
                            active = await storage.count_active_analyses()
                            if active != last_active:
                                yield f"event: navbar:active-count\ndata: {active}\n\n"
                                last_active = active
                        except Exception:
                            logger.debug(
                                "Failed to fetch active count for multiplexed SSE",
                                exc_info=True,
                            )
                    elif prefix == "navbar:mention":
                        try:
                            unread = await storage.get_unread_mention_count(username)
                            if unread != last_unread:
                                yield f"event: navbar:unread-count\ndata: {unread}\n\n"
                                last_unread = unread
                        except Exception:
                            logger.debug(
                                "Failed to fetch unread count for multiplexed SSE",
                                exc_info=True,
                            )
                    elif prefix == "navbar:pending":
                        if chunk := await pending_feed.step():
                            yield chunk
                    else:
                        # Simple notification topics — emit event with topic prefix
                        event_map = {
                            "dashboard": "dashboard-changed",
                            "token-usage": "usage-changed",
                            "settings": "settings-changed",
                            "admin-chat": "chat-changed",
                        }
                        # For results:X, comments:X, chat:X — extract the base topic
                        base = prefix.split(":")[0] if ":" in prefix else prefix
                        if base == "results":
                            event_name = "status-changed"
                        elif base == "comments":
                            event_name = "comments-changed"
                        elif base == "chat":
                            event_name = "chat-changed"
                        else:
                            event_name = event_map.get(prefix, "refresh")
                        yield f"event: {prefix}:{event_name}\ndata: refresh\n\n"

        finally:
            for task in wait_tasks:
                task.cancel()

            # Cleanup all registrations
            for _prefix, ev, global_set, per_key_dict, key in registrations:
                if per_key_dict is not None:
                    bucket = per_key_dict.get(key)
                    if bucket is not None:
                        bucket.discard(ev)
                        if not bucket:
                            per_key_dict.pop(key, None)
                elif global_set is not None:
                    global_set.discard(ev)

            # Cleanup navbar events
            if active_event is not None:
                _active_count_listeners.discard(active_event)
            if mention_event is not None and username:
                listeners = _mention_listeners.get(username)
                if listeners is not None:
                    listeners.discard(mention_event)
                    if not listeners:
                        _mention_listeners.pop(username, None)
            if pending_event is not None:
                _pending_count_listeners.discard(pending_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/dashboard", operation_id="apiDashboard")
async def api_dashboard(
    limit: int = Query(default=500, ge=0, description="Max results (0 = no limit)"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
) -> list[dict[str, Any]]:
    """Return dashboard job list as JSON for the React frontend."""
    return await list_results_for_dashboard(limit=limit, offset=offset)


@app.get("/api/exporters", operation_id="listExporters")
async def list_exporters(
    settings: Settings = _SETTINGS_DEP,
) -> list[ExporterInfo]:
    """List available exporter plugins and their status."""
    return _available_exporters(settings)


@app.get("/api/capabilities", operation_id="getCapabilities")
async def get_capabilities(settings: Settings = _SETTINGS_DEP) -> dict[str, Any]:
    """Report server-level feature toggles and credential availability.

    Feature toggles (ENABLE_GITHUB_ISSUES, ENABLE_JIRA_ISSUES) control
    whether issue creation is available at all.  Credential flags tell
    the frontend whether the server has its own tokens configured so the
    UI can decide if user-supplied tokens are required or optional.
    """
    return _build_capabilities(settings)


def _strip_url_userinfo(url: str) -> str:
    """Remove userinfo (username/password) from a URL for safe API responses."""
    return strip_url_userinfo(url)


@app.get("/api/default-server-settings", operation_id="getDefaultServerSettings")
async def get_default_server_settings(
    settings: Settings = _SETTINGS_DEP,
) -> dict[str, Any]:
    """Return all non-sensitive server settings.

    Authenticated (any role). Iterates all Settings model fields and
    excludes sensitive fields (passwords, tokens, credentials, SecretStr).
    Structured string fields (peer_ai_configs, additional_repos,
    tests_repo_url) are parsed into their rich representations.
    """
    from pydantic import SecretStr as _SecretStr

    result: dict[str, Any] = {}
    for field_name in type(settings).model_fields:
        if field_name in _SENSITIVE_SETTINGS or field_name in _DEFAULTS_HIDDEN_SETTINGS:
            continue
        value = getattr(settings, field_name)
        # Skip SecretStr fields (defensive — all should be in _SENSITIVE_SETTINGS)
        if isinstance(value, _SecretStr):
            continue
        result[field_name] = value

    # Resolve AI provider/model using the shared resolution helper
    ai_provider, ai_model = _resolve_ai_provider_model(settings=settings)
    result["ai_provider"] = ai_provider
    result["ai_model"] = ai_model

    # Parse peer_ai_configs string → list of dicts
    if settings.peer_ai_configs:
        try:
            result["peer_ai_configs"] = parse_peer_configs(settings.peer_ai_configs)
        except ValueError:
            logger.warning(
                "Failed to parse PEER_AI_CONFIGS for default-server-settings"
            )
            result["peer_ai_configs"] = []
    else:
        result["peer_ai_configs"] = []

    # Parse additional_repos string → list of dicts (tokens + userinfo stripped)
    if settings.additional_repos:
        try:
            parsed = parse_additional_repos(settings.additional_repos)
            result["additional_repos"] = [
                {
                    **{k: v for k, v in repo.items() if k != "token"},
                    "url": _strip_url_userinfo(repo["url"]),
                }
                for repo in parsed
            ]
        except ValueError:
            logger.warning(
                "Failed to parse ADDITIONAL_REPOS for default-server-settings"
            )
            result["additional_repos"] = []
    else:
        result["additional_repos"] = []

    # Split tests_repo_url into URL and ref, strip userinfo
    tests_url, tests_ref = parse_repo_ref(settings.tests_repo_url or "")
    result["tests_repo_url"] = _strip_url_userinfo(tests_url)
    result["tests_repo_ref"] = tests_ref

    # Coerce None → "" only for optional string fields; keep None for booleans/others
    _OPTIONAL_STRING_FIELDS = frozenset(
        {
            "tests_repo_url",
            "jira_cloud_url",
            "jira_server_url",
            "jira_project_key",
            "jenkins_url",
            "prow_url",
            "gcs_bucket",
            "reportportal_url",
            "reportportal_project",
        }
    )
    for key, value in result.items():
        if value is None and key in _OPTIONAL_STRING_FIELDS:
            result[key] = ""

    # Add computed jira_enabled for UI display (safe: no secrets)
    result["jira_enabled"] = settings.jira_enabled

    # Strip userinfo from all URL-like values
    for key, value in result.items():
        if isinstance(value, str) and "://" in value:
            result[key] = _strip_url_userinfo(value)

    # Belt-and-suspenders: pop any sensitive keys that might have slipped through
    for key in SENSITIVE_KEYS:
        result.pop(key, None)
    # strip_sensitive_from_response is a no-op for this dict shape (no request_params key),
    # but required by compliance rule 805017 for all API response paths.
    return strip_sensitive_from_response(result)


class JiraProjectsRequest(BaseModel):
    """Request body for listing Jira projects with user credentials."""

    jira_token: str = Field(default="", description="User's Jira token")
    jira_email: str = Field(default="", description="User's Jira email for Cloud auth")
    jira_sa_token: str = Field(
        default="",
        description=("User's Atlassian OAuth 2.0 credential client secret (ATOA)"),
    )
    query: str = Field(default="", description="Search query to filter projects")


def _jira_client_from_body(
    settings: Settings, jira_token: str, jira_email: str, jira_sa_token: str = ""
) -> tuple[Settings, str] | None:
    """Normalize user Jira credentials and return effective settings.

    Returns ``(effective_settings, stripped_token)`` when the user supplied a
    non-empty token (regular or service account), or *None* when no usable
    token is present.

    Raises:
        HTTPException: 400 when a service-account secret with an
            unrecognized prefix is supplied — it must be rejected, not
            silently dropped in favor of another credential.
    """
    _require_usable_jira_sa_token(jira_token, jira_sa_token)
    token = jira_token.strip() if jira_token else ""
    sa_token = jira_sa_token.strip() if jira_sa_token else ""
    if not token and not sa_token:
        return None
    effective = _build_effective_jira_settings(
        settings, token, jira_email, user_jira_sa_token=sa_token
    )
    return effective, token or sa_token


@app.post("/api/jira-projects", operation_id="listJiraProjects")
async def list_jira_projects(
    body: JiraProjectsRequest,
    settings: Settings = _SETTINGS_DEP,
) -> list[dict[str, Any]]:
    """List Jira projects accessible to the user.

    Uses the user's Jira token to list projects they can see.
    Always includes the server's configured project key.
    """
    result = _jira_client_from_body(
        settings, body.jira_token, body.jira_email, body.jira_sa_token
    )
    # Effective settings first: the user's credential pins the auth method,
    # so the method-URL gate must check the EFFECTIVE method's URL — the
    # server-selected method's URL being empty must not blank the list for
    # a user whose own method is fully configured (and vice versa).
    effective_settings = result[0] if result is not None else settings
    if not jira_method_url(effective_settings):
        return []
    if result is None:
        # No user token — return just the server's configured project
        if settings.jira_project_key:
            return [
                {"key": settings.jira_project_key, "name": settings.jira_project_key}
            ]
        return []

    projects: list[dict[str, Any]] = []
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
    jira_sa_token: str = Field(
        default="",
        description=("User's Atlassian OAuth 2.0 credential client secret (ATOA)"),
    )
    project_key: str = Field(description="Jira project key")


@app.post("/api/jira-security-levels", operation_id="listJiraSecurityLevels")
async def list_jira_security_levels(
    body: JiraSecurityLevelsRequest,
    settings: Settings = _SETTINGS_DEP,
) -> list[dict[str, Any]]:
    """List available security levels for a Jira project."""
    if not body.project_key:
        return []

    result = _jira_client_from_body(
        settings, body.jira_token, body.jira_email, body.jira_sa_token
    )
    if result is None:
        return []

    effective_settings, _ = result
    # Effective settings first: check the EFFECTIVE method's URL — the
    # server-selected method's URL must not decide for a user whose own
    # method is (not) configured.
    if not jira_method_url(effective_settings):
        return []

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


@app.post("/api/validate-token", operation_id="validateToken")
async def validate_token(
    body: ValidateTokenRequest,
    settings: Settings = _SETTINGS_DEP,
) -> dict[str, Any]:
    """Validate a GitHub or Jira token by making a lightweight API call.

    GitHub: GET /user (returns authenticated user info)
    Jira: GET /rest/api/2/myself (Cloud basic / Server Bearer), or project
    visibility for OAuth 2.0 credential secrets (read:jira-work scope only)
    """
    token = body.token.strip()
    if not token:
        return {"valid": False, "username": "", "message": "Token is required"}

    def _invalid(msg: str) -> dict[str, Any]:
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
                except ValueError, json.JSONDecodeError:
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
        # ATOA-prefixed values are OAuth 2.0 credential client secrets —
        # they authenticate (via a minted access token) against the scoped
        # api.atlassian.com gateway (REST v3); ATST-prefixed values are
        # already-minted service-account scoped tokens sent as Bearer to
        # the same gateway; other tokens follow the email → Cloud basic /
        # no-email → Server Bearer split. Each method reads its own
        # server-configured URL and TLS verification.
        email = body.email.strip()
        kind = classify_jira_secret(token)
        is_sa_secret = kind == "oauth"
        is_atst_token = kind == "atst"
        connection = resolve_jira_connection_from_params(
            jira_cloud_url=settings.jira_cloud_url,
            jira_server_url=settings.jira_server_url,
            jira_email=email,
            jira_token="" if is_sa_secret or is_atst_token else token,
            jira_oauth_client_secret=token if is_sa_secret else "",
            atlassian_oauth_client_id=settings.atlassian_oauth_client_id or "",
            atlassian_oauth_cloud_id=settings.atlassian_oauth_cloud_id or "",
            jira_site_url=settings.jira_site_url or "",
            jira_atst_token=token if is_atst_token else "",
            jira_cloud_ssl_verify=settings.jira_cloud_ssl_verify,
            jira_server_ssl_verify=settings.jira_server_ssl_verify,
            jira_oauth_ssl_verify=settings.jira_oauth_ssl_verify,
        )
        if is_sa_secret:
            if not settings.atlassian_oauth_client_id:
                return _invalid(
                    "Server has no atlassian_oauth_client_id configured — "
                    "it is required to use an OAuth 2.0 credential secret"
                )
            try:
                connection = await complete_jira_connection(connection)
            except JiraOAuthMintError as exc:
                return _invalid(str(exc))
            if not connection.api_base:
                return _invalid(
                    "Could not resolve the Atlassian cloud ID for the OAuth "
                    "2.0 credential (set atlassian_oauth_cloud_id or "
                    "jira_site_url on the server)"
                )
        elif is_atst_token:
            # No minting — the ATST token is already an access token; only
            # the cloud ID may still need resolving from the site URL.
            connection = await complete_jira_connection(connection)
            if not connection.api_base:
                return _invalid(
                    "Could not resolve the Atlassian cloud ID for the ATST "
                    "token (set atlassian_oauth_cloud_id or jira_site_url "
                    "on the server)"
                )
        elif not connection.api_base:
            if email:
                return _invalid(
                    "Jira Cloud URL (jira_cloud_url) not configured on server"
                )
            return _invalid(
                "Jira Server URL (jira_server_url) not configured on server"
            )
        # OAuth 2.0 credentials carry read:jira-work only — /myself needs
        # read:jira-users and 401s — so SA connections probe project
        # visibility instead of the current-user endpoint. ATST scoped
        # tokens get the same probe: their scopes are admin-chosen.
        if connection.mode in ("sa", "atst"):
            probe_path = "/rest/api/3/project"
            probe_params = {"maxResults": 1}
        else:
            probe_path = "/rest/api/2/myself"
            probe_params = {}
        try:
            async with httpx.AsyncClient(
                verify=connection.ssl_verify, timeout=10, auth=connection.auth
            ) as client:
                resp = await client.get(
                    f"{connection.api_base}{probe_path}",
                    params=probe_params,
                    headers={"Accept": "application/json", **connection.headers},
                )
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError, json.JSONDecodeError:
                    return _invalid("Tracker API returned an unexpected response")
                if connection.mode == "sa":
                    if not isinstance(data, list):
                        return _invalid("Tracker API returned an unexpected response")
                    return {
                        "valid": True,
                        "username": "service account",
                        "message": "OAuth 2.0 credential valid (service account)",
                    }
                if connection.mode == "atst":
                    if not isinstance(data, list):
                        return _invalid("Tracker API returned an unexpected response")
                    return {
                        "valid": True,
                        "username": "service account",
                        "message": "ATST service-account token valid",
                    }
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


def _sanitize_sidecar_placeholder(value: str) -> str:
    """Clear unsubstituted sidecar placeholders (e.g. ``{job_name}`` → ``\"\"``).

    When the model omits an optional tool arg, the sidecar may leave the
    literal ``{param}`` string in query/body fields. Treat that as empty.
    """
    if not value:
        return ""
    stripped = value.strip()
    if stripped.startswith("{") and stripped.endswith("}") and len(stripped) > 2:
        inner = stripped[1:-1]
        if inner.isidentifier():
            return ""
    return value


@app.get("/history/failures", operation_id="getAllFailuresEndpoint")
async def get_all_failures_endpoint(
    search: str = Query(default=""),
    job_name: str = Query(default=""),
    classification: str = Query(default=""),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
) -> dict[str, Any]:
    """Get paginated failure history."""
    logger.debug(
        f"GET /history/failures: search={search!r}, "
        f"job_name={job_name!r}, classification={classification!r}, "
        f"limit={limit}, offset={offset}, from={date_from!r}, to={date_to!r}"
    )
    return await storage.get_all_failures(
        search=search,
        job_name=job_name,
        classification=classification,
        limit=limit,
        offset=offset,
        date_from=date_from,
        date_to=date_to,
    )


@app.get("/history/test/{test_name:path}", operation_id="getTestHistoryEndpoint")
async def get_test_history_endpoint(
    request: Request,
    test_name: str,
    limit: int = Query(default=20, le=100),
    job_name: str = Query(default=""),
    exclude_job_id: str = Query(
        default="", description="Exclude results from this job ID"
    ),
) -> dict[str, Any]:
    """Get pass/fail history for a specific test."""
    _require_authenticated(request)
    job_name = _sanitize_sidecar_placeholder(job_name)
    exclude_job_id = _sanitize_sidecar_placeholder(exclude_job_id)
    logger.debug(f"GET /history/test/{test_name}: limit={limit}, job_name={job_name!r}")
    result = await storage.get_test_history(
        test_name, limit=limit, job_name=job_name, exclude_job_id=exclude_job_id
    )
    return strip_sensitive_from_response(result)


@app.get("/history/search", operation_id="searchBySignatureEndpoint")
async def search_by_signature_endpoint(
    request: Request,
    signature: str = Query(...),
    exclude_job_id: str = Query(
        default="", description="Exclude results from this job ID"
    ),
) -> dict[str, Any]:
    """Find all tests that failed with the same error signature.

    ``signature`` accepts a comma-separated set, so one request covers every
    hash of a failure group.
    """
    _require_authenticated(request)
    logger.debug(f"GET /history/search: signature={signature}")
    result = await storage.search_by_signature(signature, exclude_job_id=exclude_job_id)
    return strip_sensitive_from_response(result)


@app.get("/history/stats/{job_name:path}", operation_id="getJobStatsEndpoint")
async def get_job_stats_endpoint(
    request: Request,
    job_name: str,
    exclude_job_id: str = Query(
        default="", description="Exclude results from this job ID"
    ),
) -> dict[str, Any]:
    """Get aggregate statistics for a specific job."""
    _require_authenticated(request)
    logger.debug(f"GET /history/stats/{job_name}")
    result = await storage.get_job_stats(job_name, exclude_job_id=exclude_job_id)
    return strip_sensitive_from_response(result)


@app.post("/history/classify", status_code=201, operation_id="classifyTest")
async def classify_test(request: Request, body: ClassifyTestRequest) -> dict[str, Any]:
    """Classify a test as FLAKY, REGRESSION, etc. Used by AI and humans."""
    _check_allow_list(request)
    _require_reviewer(request)
    logger.debug(
        f"POST /history/classify: test_name={body.test_name!r}, classification={body.classification!r}"
    )
    test_name = body.test_name.strip()
    classification = body.classification
    reason = body.reason
    job_name = body.job_name
    # Sidecar body_template leaves "{references}" when the model omits the arg.
    references = _sanitize_sidecar_placeholder(body.references)
    classify_job_id = body.job_id

    if not test_name:
        raise HTTPException(status_code=400, detail="test_name is required")

    if classification == "KNOWN_BUG" and not str(references).strip():
        raise HTTPException(
            status_code=400,
            detail="KNOWN_BUG requires non-empty references (e.g., Jira tickets or historical bug URLs).",
        )

    # Detect AI caller: the AI prompt includes source="ai" in the request body.
    # AI_SYSTEM_USERNAME is a reserved system identity (blocked from registration)
    # used consistently for all AI-originated actions (auto-review, classification).
    is_ai_caller = body.source == "ai"

    # Force created_by: AI callers are always attributed to AI_SYSTEM_USERNAME,
    # regardless of the authenticated session username.
    # Human callers must have an authenticated username — reject if missing
    # to prevent misattribution as AI_SYSTEM_USERNAME.
    if not is_ai_caller and not request.state.username:
        raise HTTPException(
            status_code=401, detail="Authentication required to classify tests"
        )
    created_by = AI_SYSTEM_USERNAME if is_ai_caller else request.state.username

    # Human classifications are visible immediately.
    # AI classifications become visible after analysis completes
    # and calls make_classifications_visible().
    visible = 0 if is_ai_caller else 1

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


@app.get("/history/classifications", operation_id="getClassifications")
async def get_classifications(
    request: Request,
    test_name: str = Query(default=""),
    classification: str = Query(default=""),
    job_name: str = Query(default=""),
    parent_job_name: str = Query(default=""),
    job_id: str = Query(default=""),
) -> dict[str, Any]:
    """Get test classifications."""
    _require_authenticated(request)
    test_name = _sanitize_sidecar_placeholder(test_name)
    classification = _sanitize_sidecar_placeholder(classification)
    job_name = _sanitize_sidecar_placeholder(job_name)
    parent_job_name = _sanitize_sidecar_placeholder(parent_job_name)
    job_id = _sanitize_sidecar_placeholder(job_id)
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
    return strip_sensitive_from_response({"classifications": classifications})


def _cursor_status_for_client(
    status: dict[str, Any], *, is_admin: bool
) -> dict[str, Any]:
    """Return Cursor provider_status safe for the caller's role.

    Non-admins must not learn whether ``CURSOR_API_KEY`` is configured
    (``has_api_key`` or credential-revealing reasons/hints).
    """
    out = dict(status)
    if is_admin:
        return out
    out.pop("has_api_key", None)
    if out.get("reason") in ("auth_expired", "api_key_not_applied"):
        out["reason"] = "unavailable"
    if not out.get("ok"):
        out["hint"] = "Cursor is unavailable. Contact an administrator."
    return out


def _cursor_status_from_model_count(model_count: int) -> dict[str, Any]:
    """Coarse Cursor status for non-admins (no subprocess / credential probe)."""
    if model_count > 0:
        return {"ok": True, "reason": None, "hint": None, "model_count": model_count}
    return {
        "ok": False,
        "reason": "unavailable",
        "hint": "Cursor is unavailable. Contact an administrator.",
        "model_count": model_count,
    }


async def scoped_models_for_request(
    request: Request,
    force_server: bool | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Discover models using the authenticated caller's credential scope."""
    token = ai_username.set(request.state.username)
    force_token = force_server_credentials.set(
        get_settings().force_server_credentials
        if force_server is None
        else force_server
    )
    try:
        return await scoped_models()
    finally:
        force_server_credentials.reset(force_token)
        ai_username.reset(token)


@app.get("/api/ai-models", operation_id="listAiModels")
async def list_ai_models(
    request: Request,
    provider: str = Query(
        "",
        description="Filter by an exact Pi-sidecar provider identifier.",
    ),
    force_server_credentials: bool | None = None,
) -> dict[str, Any]:
    """List available AI models for one or all configured providers.

    When listing all providers (or a Cursor provider), includes ``provider_status``
    with a Cursor auth probe (browser ``agent login`` expires; ``CURSOR_API_KEY``
    does not expire and always works when set in the server env).
    Credential-state fields (``has_api_key``) are admin-only.
    Non-admins get a coarse ok/unavailable status from model_count only
    (no ``agent status`` subprocess).
    """
    _require_authenticated(request)
    logger.debug("GET /api/ai-models provider=%s", provider)
    is_admin = bool(getattr(request.state, "is_admin", False))
    try:
        scoped = await scoped_models_for_request(request, force_server_credentials)
        listing_status = model_listing_status.get() or {}
        if provider:
            provider = normalize_provider(provider)
            # Custom providers may be registered with mixed case (e.g.
            # "EnMaaS"): resolve the query case-insensitively and adopt the
            # catalog's exact spelling so per-provider consumers (settings
            # page pickers) see the models and persist the right ID.
            if provider not in scoped:
                exact = next((k for k in scoped if k.lower() == provider), None)
                if exact is not None:
                    provider = exact
            models = scoped.get(provider, [])
            if not models and provider not in listing_status:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown Pi-sidecar provider: {provider}",
                )
            payload: dict[str, Any] = {"provider": provider, "models": models}
            if provider in listing_status:
                payload["modelListingSupported"] = listing_status[provider][
                    "modelListingSupported"
                ]
                if listing_status[provider].get("unavailable"):
                    payload["unavailable"] = True
            if is_cursor_provider(provider):
                if is_admin:
                    cursor_raw = await probe_cursor_auth(model_count=len(models))
                    cursor_status = _cursor_status_for_client(cursor_raw, is_admin=True)
                else:
                    cursor_status = _cursor_status_from_model_count(len(models))
                payload["provider_status"] = {"cursor": cursor_status}
            return strip_sensitive_from_response(payload)

        # No provider specified — use the shared successful sidecar catalog.
        all_models = scoped
        cursor_count = sum(
            len(models)
            for provider_id, models in all_models.items()
            if is_cursor_provider(provider_id)
        )
        if is_admin:
            cursor_status = _cursor_status_for_client(
                await probe_cursor_auth(model_count=cursor_count),
                is_admin=True,
            )
        else:
            cursor_status = _cursor_status_from_model_count(cursor_count)
        return strip_sensitive_from_response(
            {
                "providers": all_models,
                "provider_status": {"cursor": cursor_status, **listing_status},
            }
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except Exception:
        logger.warning(
            "Failed to list AI models for provider=%s", provider, exc_info=True
        )
        if provider:
            return strip_sensitive_from_response({"provider": provider, "models": []})
        return strip_sensitive_from_response({"providers": {}, "provider_status": {}})


@app.post("/api/admin/ai-models/refresh", operation_id="refreshAiModels")
async def refresh_ai_models(request: Request) -> dict[str, Any]:
    """Trigger model re-discovery on the sidecar and return updated list."""
    _require_admin(request)
    logger.info("POST /api/admin/ai-models/refresh: refreshing models")
    try:
        from pi_sidecar_client import get_sidecar_client

        client = get_sidecar_client()
        models = await client.refresh_models()
        update_model_catalog(models)
        clear_cursor_auth_cache()
        cursor_status = await probe_cursor_auth(force=True)
        logger.info(
            "Model refresh complete: %d models available (cursor_ok=%s)",
            len(models),
            cursor_status.get("ok"),
        )
        return strip_sensitive_from_response(
            {
                "models": models,
                "count": len(models),
                "provider_status": {"cursor": cursor_status},
            }
        )
    except Exception:
        logger.exception("Failed to refresh AI models")
        raise HTTPException(
            status_code=502, detail="Failed to refresh AI models from sidecar"
        )


@app.get("/health", operation_id="healthCheck")
async def health_check() -> dict[str, Any]:
    """Basic health check endpoint (legacy, lightweight)."""
    return {"status": "healthy"}


@app.get("/api/version", operation_id="getVersion")
async def get_version() -> dict[str, Any]:
    """Return the application version (lightweight, no dependency checks)."""
    return {"version": _get_app_version()}


@app.get("/api/health", operation_id="healthCheckDetailed")
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


@app.get("/api/admin/component-versions", operation_id="adminGetComponentVersions")
async def admin_get_component_versions(request: Request) -> dict[str, Any]:
    """Installed AI-sidecar component versions (admin only).

    Split from the public /api/health response so exact versions are never
    disclosed without authentication (fingerprinting surface).
    """
    _require_admin(request)
    return strip_sensitive_from_response({"components": await get_component_versions()})


@app.get("/metrics", operation_id="prometheusMetrics")
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


# Module-level cache for GitHub release data (raw filtered release list)
_release_cache: dict[str, Any] = {}
_RELEASE_CACHE_TTL = 3600  # 1 hour
_release_cache_lock = asyncio.Lock()

# The release list endpoint and payload shaping constants (#341)
_RELEASES_LIST_URL = "https://api.github.com/repos/myk-org/rootcoz/releases"
_RELEASES_PER_PAGE = 20
_RELEASES_PAYLOAD_BOUND = 15
# Enough eligible releases to fill the aggregate plus the latest slot
_RELEASES_FETCH_TARGET = _RELEASES_PAYLOAD_BOUND + 1
# Hard cap on the page walk so a repo of only drafts/prereleases terminates
_RELEASES_MAX_PAGES = 5


_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:([.\-])(.+))?(?:\+(.*))?$")
# Suffixes that make a version a PRERELEASE of its numbers (semver §9 + dev)
_PRERELEASE_PREFIXES = ("dev", "a", "alpha", "b", "beta", "rc", "pre")

# A parsed version: numeric components + prerelease identifiers.
# ``prerelease`` is None for releases (and for post/local builds, which are
# never older than their release); a tuple of sortable identifiers for
# prereleases — numeric identifiers sort below alphanumeric ones (semver §11.4).
ParsedVersion = tuple[tuple[int, ...], tuple[tuple[int, int, str], ...] | None]


def _prerelease_identifier(identifier: str) -> tuple[int, int, str]:
    """Sortable semver prerelease identifier (numeric < alphanumeric)."""
    if identifier.isdigit():
        return (0, int(identifier), "")
    return (1, 0, identifier)


def _parse_version(version: str) -> ParsedVersion | None:
    """Parse a semver-ish version; None when unparseable.

    ``v4.7.0`` → ``((4, 7, 0), None)``; ``4.7.0-rc2`` → ``((4, 7, 0), ((1, 0, "rc"), (0, 2, "")))``.
    Post-releases (``.post1``) and local/build labels (``+local``) are not
    prereleases — a server running them is not older than the release.
    """
    match = _VERSION_RE.match(version.strip().lstrip("vV"))
    if not match:
        return None
    nums = tuple(int(part) for part in match.group(1).split("."))
    # Build metadata ("+local", "+build.5") is ignored entirely; only a
    # "."/"-" separated suffix can make this a prerelease or post-release
    suffix = (match.group(3) or "").lower()
    if not suffix or suffix.startswith("post"):
        return nums, None
    identifiers = suffix.split(".")
    if identifiers[0].isdigit() or any(
        suffix.startswith(prefix) for prefix in _PRERELEASE_PREFIXES
    ):
        return nums, tuple(_prerelease_identifier(i) for i in identifiers)
    # Unrecognized suffix — treat as the plain release, never older
    return nums, None


def _padded(numbers: tuple[int, ...], width: int) -> tuple[int, ...]:
    """Zero-pad numeric components so 4.7 compares equal to 4.7.0."""
    return numbers + (0,) * (width - len(numbers))


def _compare_prerelease(
    first: tuple[tuple[int, int, str], ...] | None,
    second: tuple[tuple[int, int, str], ...] | None,
) -> int:
    """Semver prerelease precedence: negative when first < second.

    A release (None) sorts above any prerelease; identifiers compare
    left-to-right (numeric numerically, alphanumeric lexically), and a
    shorter identifier list sorts below a longer one when it is a prefix.
    """
    if first is None and second is None:
        return 0
    if first is None:
        return 1
    if second is None:
        return -1
    for left, right in zip(first, second):
        if left != right:
            return -1 if left < right else 1
    if len(first) == len(second):
        return 0
    return -1 if len(first) < len(second) else 1


def _is_newer_release(release_version: str, server_version: str) -> bool | None:
    """True when the release is strictly newer than the running version.

    None when either side is unparseable (e.g. ``"unknown"``) — an
    indeterminate comparison must never become a definite update notice.
    """
    release = _parse_version(release_version)
    server = _parse_version(server_version)
    if release is None or server is None:
        return None
    width = max(len(release[0]), len(server[0]))
    rel = _padded(release[0], width)
    srv = _padded(server[0], width)
    if rel != srv:
        return rel > srv
    return _compare_prerelease(release[1], server[1]) > 0


def _is_same_release(release_version: str, server_version: str) -> bool | None:
    """True when both sides denote the same release (equivalent spellings).

    ``4.7`` == ``4.7.0`` == ``4.7.0.post1``; ``4.7.0-rc1`` != ``4.7.0``.
    None when either side is unparseable.
    """
    release = _parse_version(release_version)
    server = _parse_version(server_version)
    if release is None or server is None:
        return None
    width = max(len(release[0]), len(server[0]))
    if _padded(release[0], width) != _padded(server[0], width):
        return False
    return _compare_prerelease(release[1], server[1]) == 0


def _release_sort_key(
    entry: dict[str, Any], width: int
) -> tuple[int, tuple[int, ...], tuple[Any, ...]]:
    """Rank a release entry for a newest-first sort; unparseable last.

    GitHub lists releases by publication date, not version order (#341) —
    an out-of-order publish must not misreport the latest release.
    """
    parsed = _parse_version(entry["version"])
    if parsed is None:
        # Rank 0 loses to any parseable version under the descending sort
        return (0, (), ())
    numbers, prerelease = parsed
    # A release (no prerelease) sorts above any prerelease of the same numbers
    pre_key = (1,) if prerelease is None else (0, *prerelease)
    return (1, _padded(numbers, width), pre_key)


def _release_payload(
    releases: list[dict[str, Any]], since: str | None = None
) -> dict[str, Any]:
    """Build the /api/releases/latest payload from a release list.

    Top-level fields describe the newest release (unchanged shape, #333);
    ``releases`` aggregates every release strictly newer than the baseline,
    oldest→newest, bounded at 15 entries (#341). The baseline is the running
    server version in update-nag mode (a stale ``since`` must not resurface
    already-installed releases); otherwise the older of ``since`` (when
    parseable) and the server version — a ``since`` newer than the server
    (browser saved a higher version, server rolled back) is clamped down so
    releases between the two are not skipped.
    """
    server_version = _get_app_version()
    entries = [
        {
            # GitHub can send explicit JSON nulls — normalize so the
            # frontend never trips on them (#341)
            "version": (r.get("tag_name") or "").lstrip("v"),
            "name": r.get("name") or "",
            "body": r.get("body") or "",
            "published_at": r.get("published_at") or "",
            "html_url": r.get("html_url") or "",
        }
        for r in releases
    ]
    width = max(
        (len(p[0]) for e in entries if (p := _parse_version(e["version"]))),
        default=0,
    )
    # Stable newest-first sort: unparseable and equal versions keep API order
    entries.sort(key=lambda e: _release_sort_key(e, width), reverse=True)
    latest = (
        entries[0]
        if entries
        else {
            "version": "",
            "name": "",
            "body": "",
            "published_at": "",
            "html_url": "",
        }
    )
    if _is_newer_release(latest["version"], server_version):
        # Update nag: only what the running server is missing (#341)
        baseline = server_version
    elif (
        since and _parse_version(since) and not _is_newer_release(since, server_version)
    ):
        baseline = since
    else:
        baseline = server_version
    newer = [e for e in entries if _is_newer_release(e["version"], baseline)]
    # The list is newest-first: keep the newest 15, then flip to oldest-first
    newer = list(reversed(newer[:_RELEASES_PAYLOAD_BOUND]))
    return {
        "version": latest["version"],
        "name": latest["name"],
        "body": latest["body"],
        "published_at": latest["published_at"],
        "html_url": latest["html_url"],
        "server_version": server_version,
        # The What's New dialog must not advertise features the running
        # server does not have (#333) — compare against the running version.
        # None = indeterminate (unparseable version), never a definite nag.
        "update_available": _is_newer_release(latest["version"], server_version),
        # Explicit equivalence decision so the dialog never string-compares
        # versions: 4.7 and 4.7.0 are the same release. None = indeterminate.
        "running_latest": _is_same_release(latest["version"], server_version),
        "releases": newer,
    }


@app.get("/api/releases/latest", operation_id="getLatestRelease")
async def get_latest_release(since: str | None = None) -> JSONResponse:
    """Return the latest GitHub release info (cached for 1 hour).

    Used by the What's New dialog. ``since`` sets the aggregation baseline
    for the ``releases`` array (#341); it defaults to the running server
    version. Falls back gracefully if GitHub is unreachable.
    """

    def _cached_response(now: float) -> JSONResponse | None:
        cached = _release_cache.get("releases")
        if cached is not None and (
            now - _release_cache.get("fetched_at", 0) < _RELEASE_CACHE_TTL
        ):
            return JSONResponse(content=_release_payload(cached, since))
        return None

    now = _time.monotonic()
    if (hit := _cached_response(now)) is not None:
        return hit

    async with _release_cache_lock:
        # Re-check after acquiring lock (another request may have refreshed)
        now = _time.monotonic()
        if (hit := _cached_response(now)) is not None:
            return hit

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Walk pages until enough ELIGIBLE (non-draft, non-prerelease)
                # releases are collected: a page full of drafts/prereleases
                # must not starve the stable list (#341)
                releases: list[dict[str, Any]] = []
                for page in range(1, _RELEASES_MAX_PAGES + 1):
                    resp = await client.get(
                        _RELEASES_LIST_URL,
                        params={
                            "per_page": _RELEASES_PER_PAGE,
                            "page": page,
                        },
                        headers={"Accept": "application/vnd.github.v3+json"},
                    )
                    resp.raise_for_status()
                    batch = resp.json()
                    releases.extend(
                        r
                        for r in batch
                        if not r.get("draft") and not r.get("prerelease")
                    )
                    if (
                        len(batch) < _RELEASES_PER_PAGE
                        or len(releases) >= _RELEASES_FETCH_TARGET
                    ):
                        break
            _release_cache["releases"] = releases
            _release_cache["fetched_at"] = now
            logger.info("Fetched %d GitHub releases", len(releases))
            return JSONResponse(content=_release_payload(releases, since))
        except Exception:
            logger.debug("Failed to fetch GitHub releases", exc_info=True)
            if _release_cache.get("releases") is not None:
                return JSONResponse(
                    content=_release_payload(_release_cache["releases"], since)
                )
            return JSONResponse(content=_release_payload([]))


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


async def _read_json_object(request: Request) -> dict[str, Any]:
    """Parse request body as a JSON object. Raises HTTPException on invalid input."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return body


def effective_can_view_reports(is_admin: bool, stored_flag: bool = False) -> bool:
    """Effective reports access: admins always True; others use the stored DB flag."""
    return True if is_admin else stored_flag


def _require_admin(request: Request) -> None:
    """Raise 403 if the request is not from an authenticated admin."""
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


def _require_can_view_reports(request: Request) -> None:
    """Raise 403 unless the caller may access /api/reports/*.

    AuthMiddleware sets ``request.state.can_view_reports`` to True for admins
    and for users with the DB flag; handlers trust that single source of truth.
    """
    if getattr(request.state, "can_view_reports", False):
        return
    raise HTTPException(
        status_code=403,
        detail="Reports access required. Ask an administrator to grant can_view_reports.",
    )


def _require_authenticated(request: Request) -> None:
    """Raise 401 if the request has no authenticated username.

    Middleware already blocks anonymous access for non-public paths; this is an
    explicit handler-level gate for endpoints that return non-public data.
    Uses 401 (not 403) so frontend auth refresh treats missing auth as logout.
    """
    username = getattr(request.state, "username", "")
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")


def _require_reviewer(request: Request) -> None:
    """Raise 403 if the request is not from at least a reviewer."""
    username = getattr(request.state, "username", "")
    if not username:
        raise HTTPException(status_code=403, detail="Authentication required")
    role = getattr(request.state, "role", "")
    if role not in ("reviewer", "operator", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Reviewer access required. Viewers cannot perform this action.",
        )


def _require_operator(request: Request) -> None:
    """Raise 403 if the request is not from an operator or admin."""
    role = getattr(request.state, "role", "reviewer")
    if role not in ("operator", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Operator access required. Reviewers cannot perform this action.",
        )


def _get_job_submitter(result: dict[str, Any]) -> str:
    """Extract the submitted_by username from a stored result dict."""
    result_data = result.get("result") or {}
    return (result_data.get("request_params") or {}).get("submitted_by", "")


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


@app.post("/api/auth/login", operation_id="login")
async def login(request: Request) -> JSONResponse:
    """Authenticate any user with username + API key.

    Returns a session cookie and user info including ``can_view_reports``
    (True for admins; otherwise the stored DB flag).
    """
    body = await _read_json_object(request)

    username = str(body.get("username", "")).strip().lower()
    api_key = str(body.get("api_key", ""))

    if not username or not api_key:
        raise HTTPException(status_code=400, detail="Username and api_key are required")

    settings = get_settings()
    is_admin = False
    resolved_role = "reviewer"
    authenticated = False
    can_view_reports = False
    can_use_server_providers = False
    can_use_free_providers = False

    # Check admin_key — username must be "admin"
    if (
        username == "admin"
        and settings.admin_key
        and hmac.compare_digest(api_key, settings.admin_key)
    ):
        is_admin = True
        resolved_role = "admin"
        authenticated = True
        can_view_reports = effective_can_view_reports(True)
        can_use_server_providers = True
        can_use_free_providers = True
    else:
        # Check user API key
        user = await storage.get_user_by_key(api_key)
        if user and user["username"] == username:
            authenticated = True
            resolved_role = str(user.get("role", "reviewer"))
            if resolved_role == "admin":
                is_admin = True
            can_view_reports = effective_can_view_reports(
                is_admin, bool(user.get("can_view_reports"))
            )
            can_use_server_providers = bool(user.get("can_use_server_providers"))
            # Effective free access — same rule as /api/auth/me (toggle,
            # role admin, server grant); get_user_by_key has no free column.
            can_use_free_providers = await storage.can_user_use_free_providers(username)

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

    session_token = await storage.create_session(
        username, is_admin=is_admin, role=resolved_role
    )
    response = JSONResponse(
        content={
            "username": username,
            "role": resolved_role,
            "is_admin": is_admin,
            "can_view_reports": can_view_reports,
            "can_use_server_providers": can_use_server_providers,
            "can_use_free_providers": can_use_free_providers,
        }
    )
    response.set_cookie(
        "rootcoz_session",
        session_token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=storage.current_session_ttl_seconds(),
    )
    # Clear legacy cookie after migration
    response.delete_cookie(
        "jji_session",
        httponly=True,
        samesite="lax",
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


@app.post("/api/auth/logout", operation_id="logout")
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
        samesite="lax",
        secure=settings.secure_cookies,
    )
    response.delete_cookie(
        "jji_session",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    return response


@app.get("/api/auth/me", operation_id="authMe")
async def auth_me(request: Request) -> JSONResponse:
    """Return current user info, including ``can_view_reports``.

    ``can_view_reports`` is True for admins and for users granted the flag
    (see AuthMiddleware / ``request.state.can_view_reports``).
    """
    return JSONResponse(
        content={
            "username": request.state.username,
            "role": request.state.role,
            "is_admin": request.state.is_admin,
            "can_view_reports": bool(getattr(request.state, "can_view_reports", False)),
            "can_use_server_providers": bool(
                getattr(request.state, "can_use_server_providers", False)
            ),
            "can_use_free_providers": bool(
                getattr(request.state, "can_use_free_providers", False)
            ),
        }
    )


@app.post("/api/auth/rotate-key", operation_id="rotateOwnKeyEndpoint")
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
    current_role = request.state.role
    session_token = await storage.create_session(
        username, is_admin=is_admin, role=current_role
    )
    settings = get_settings()

    response = JSONResponse(
        content={"username": username, "new_api_key": new_key},
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        "rootcoz_session",
        session_token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=storage.current_session_ttl_seconds(),
    )

    logger.info(f"[AUDIT] User '{username}' rotated their own API key")
    return response


@app.post("/api/auth/register", operation_id="registerUser")
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
    username = raw_username.strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    # Block reserved username prefixes (e.g., rootcoz-ai)
    if username.startswith("rootcoz"):
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Usernames starting with 'rootcoz' are reserved for system use"
            },
            headers={"Cache-Control": "no-store"},
        )

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

    if user_status == "pending":
        notify_pending_count_changed()

    # Create a session for the user with the default role
    default_role = settings.default_user_role
    session_token = await storage.create_session(
        username, is_admin=False, role=default_role
    )

    content: dict[str, Any] = {
        "username": username,
        "api_key": raw_key,
        "role": default_role,
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
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        "rootcoz_session",
        session_token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=storage.current_session_ttl_seconds(),
    )
    # Clear legacy session cookie after migration
    response.delete_cookie(
        "jji_session",
        httponly=True,
        samesite="lax",
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


@app.get("/api/auth/needs-key", operation_id="checkNeedsKey")
async def check_needs_key(request: Request) -> JSONResponse:
    """Check if the current user needs to generate an API key.

    Uses session or cookie username to determine if the user needs registration.
    This is a public endpoint — does not require authentication.
    """
    cache_headers = {"Cache-Control": "no-store"}

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


@app.get("/api/auth/pending-status", operation_id="pendingStatus")
async def pending_status(request: Request) -> JSONResponse:
    """Return pending status info for unauthenticated users."""
    settings = get_settings()
    content: dict[str, Any] = {
        "status": "pending",
        "message": "Your account is awaiting admin approval. Please wait for an admin to approve your registration.",
    }
    _maybe_add_custom_approval_msg(content, settings)
    return JSONResponse(content=content)


# --- User AI credentials ---


async def _credential_user(request: Request) -> str:
    _require_authenticated(request)
    username = request.state.username
    if not await storage.get_user_by_username(username):
        raise HTTPException(
            status_code=403,
            detail="AI credentials require a database user; bootstrap admin has no credential store.",
        )
    return username


@app.get("/api/user/ai-credentials", operation_id="getUserAiCredentials")
async def get_user_ai_credentials(request: Request) -> JSONResponse:
    """List supported provider IDs and configured flags; never return keys."""
    username = await _credential_user(request)
    # Advertise the sidecar's exact-case provider ids; a key stored under
    # any case spelling folds into the catalog entry it belongs to (the
    # write path canonicalizes, but older rows and cross-case duplicates
    # must not split one provider into two rows).
    supported = await supported_key_providers()
    try:
        configured = await storage.get_user_ai_credentials(username)
    except storage.UnreadableAiCredentialsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    supported_set = set(supported)

    # A legacy row stored under a non-canonical spelling folds into the
    # exact-case catalog entry when one exists; only providers that left
    # the catalog entirely keep their stored spelling (configured, no
    # longer key-supported) so the UI can still render and clear them.
    def _fold(provider: str) -> str:
        # Exact registered spelling wins — two case-differing registered
        # providers must never fold into each other.
        if provider in supported_set:
            return provider
        return next(
            (s for s in supported_set if s.lower() == provider.lower()), provider
        )

    # Drifted legacy spellings (stored under a case variant that is no
    # longer registered) belong to the catalog entry; a stored spelling
    # that is itself registered belongs to that entry alone — the user
    # credential store keeps case-distinct registered providers as
    # separate credentials, so one must never mark the other configured.
    drifted_lower = {p.lower() for p in configured if p not in supported_set}

    providers: list[dict[str, Any]] = [
        {
            "provider": p,
            "configured": p in configured or p.lower() in drifted_lower,
            "supports_user_keys": p in supported_set,
        }
        for p in sorted(supported_set | {_fold(p) for p in configured})
    ]
    return JSONResponse(
        content=strip_sensitive_from_response({"providers": providers}),
        headers={"Cache-Control": "no-store"},
    )


async def _discard_unsaved_chat_response(
    message_id: int,
    job_id: str,
    username: str,
    provider: str,
    session_id: str | None,
    label: str,
) -> None:
    """Revoke an unsaved response and fail only an open placeholder."""
    if session_id:
        await _discard_unsaved_ai_session(session_id, username, provider)
    logger.info(
        "%s: discarded response for message %d (stale credentials or closed placeholder)",
        label,
        message_id,
    )
    if await storage.fail_pending_chat_message(
        message_id, "AI credentials changed during processing. Please try again."
    ):
        notify_chat_changed(job_id, username=username)


async def _discard_unsaved_ai_session(
    session_id: str, username: str, provider: str
) -> None:
    """Tombstone an owned unsaved session before best-effort sidecar deletion."""
    if await storage.revoke_ai_session_source(
        session_id, username=username, provider=provider
    ):
        await _revoke_ai_sessions([session_id])


async def _revoke_ai_sessions(sessions: list[str]) -> None:
    """Delete revoked sessions without racing a replacement using the same ID."""
    from pi_sidecar_client import get_sidecar_client

    for session_id in sessions:
        try:
            await storage.delete_revoked_ai_session(
                session_id,
                get_sidecar_client().delete_session,
            )
        except Exception:
            logger.exception("Failed to delete revoked AI session", exc_info=False)
    if sessions:
        logger.info("Revoked %d AI sessions", len(sessions))


async def _cleanup_revoked_ai_sessions() -> None:
    """Retry tombstones left by a failed sidecar deletion on the next cleanup."""
    await _revoke_ai_sessions(await storage.list_revoked_ai_sessions())


class AiCredentialInput(BaseModel):
    api_key: SecretStr
    model: str


def _validate_submitted_api_key(key: str) -> str | None:
    """Rejection detail for a submitted provider key, or None when valid.

    Shared by the user and admin (server provider) credential flows: strip
    Unicode whitespace (including the BOM), cap at 1024 UTF-16 code units.
    """
    try:
        valid = (
            bool(
                key.strip(
                    "\ufeff\u0009\u000a\u000b\u000c\u000d \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
                )
            )
            and len(key.encode("utf-16-le")) // 2 <= 1024
        )
    except UnicodeEncodeError:
        valid = False
    if valid and len(key) < 8:
        valid = False
    return None if valid else "API key must be 8-1024 characters"


async def _canonical_key_provider_id(provider: str) -> str | None:
    """The sidecar's exact-case id for a provider.

    The sidecar's catalog — and its ``/sessions`` endpoint — is
    case-sensitive: it reports mixed-case IDs like ``EnMaaS`` and rejects
    a lowercase spelling, so a caller posting ``enmaas`` passes the
    support gate yet fails sidecar verification as "inconclusive" (#347).
    Verification and storage must therefore use the canonical id this
    returns. ``None`` means the catalog does not list the provider;
    callers gate on that.

    Exact match wins: when the catalog registers two providers that
    differ only by case (``Foo`` and ``foo``), a request for ``foo`` must
    target ``foo`` — case-insensitive fallback only when the submitted
    spelling is not itself registered.
    """
    supported = await supported_key_providers()
    if provider in supported:
        return provider
    lowered = provider.lower()
    for candidate in supported:
        if candidate.lower() == lowered:
            return candidate
    return None


@app.put("/api/user/ai-credentials/{provider:path}", operation_id="setUserAiCredential")
async def set_user_ai_credential(
    provider: str, body: AiCredentialInput, request: Request
) -> JSONResponse:
    """Encrypt and save a key for a sidecar-confirmed provider."""
    _require_reviewer(request)
    _check_allow_list(request)
    username = await _credential_user(request)
    # Resolve to the sidecar's exact-case id: the gate below accepts any
    # caller spelling (#347), but verification and storage must use the
    # canonical id or the sidecar rejects the session and the stored row
    # drifts from the catalog's spelling.
    canonical = await _canonical_key_provider_id(provider)
    if canonical is None:
        raise HTTPException(
            status_code=400, detail="Provider does not support session API keys"
        )
    provider = canonical
    key = body.api_key.get_secret_value()
    if error := _validate_submitted_api_key(key):
        raise HTTPException(status_code=400, detail=error)
    from rootcoz.ai_client import verify_ai_key

    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Select a model to verify the key")
    outcome = await verify_ai_key(provider, model, key)
    if outcome == "accepted":
        try:
            sessions = await storage.update_user_ai_credential(
                username,
                provider,
                key,
                registered_providers=await registered_provider_ids(),
            )
        except storage.UnreadableAiCredentialsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _revoke_ai_sessions(sessions)
    message = {
        "rejected": "Check the key and selected model; existing credential unchanged.",
        "inconclusive": "Try again when the provider is available; existing credential unchanged.",
    }.get(outcome)
    return JSONResponse(
        content=strip_sensitive_from_response(
            {
                "outcome": outcome,
                "ok": outcome == "accepted",
                **({"message": message} if message else {}),
            }
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/user/ai-credentials/{provider:path}", operation_id="deleteUserAiCredential"
)
async def delete_user_ai_credential(provider: str, request: Request) -> JSONResponse:
    """Remove one credential; do not reveal whether a key was present.

    Deliberately not allow-list gated. This is self-service revocation: a user
    removed from ALLOWED_USERS must still be able to delete the key they
    previously stored, otherwise taking them off the list leaves a live
    credential they have no way to remove. Adding a key is gated; removing
    one is not.
    """
    _require_reviewer(request)
    username = await _credential_user(request)
    try:
        sessions = await storage.update_user_ai_credential(
            username,
            provider,
            None,
            registered_providers=await registered_provider_ids(),
        )
    except storage.UnreadableAiCredentialsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _revoke_ai_sessions(sessions)
    return JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})


# --- Filter presets (#179) ---

# URL query param names each page's presets may carry (validated on save).
_FILTER_PRESET_KEYS: dict[str, set[str]] = {
    "dashboard": {
        "team",
        "tier",
        "version",
        "label",
        "exclude_label",
        "status",
        "review_status",
        "analysis_state",
        "date_from",
        "date_to",
    },
    "reports": {
        "team",
        "tier",
        "version",
        "label",
        "exclude_label",
        "status",
        "review_status",
        "from",
        "to",
    },
}


def _validate_filter_preset_filters(page: str, filters: dict[str, Any]) -> None:
    """Reject unknown filter keys for the page with 422 (never silently drop)."""
    unknown = set(filters) - _FILTER_PRESET_KEYS[page]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown filter key(s) for {page}: " + ", ".join(sorted(unknown)),
        )


# SQLite integers are signed 64-bit; an out-of-range preset id cannot exist.
_SQLITE_INT_MAX = 2**63 - 1


def _validate_preset_id(preset_id: int) -> None:
    """Reject ids SQLite cannot store (404) instead of raising OverflowError."""
    if preset_id < 1 or preset_id > _SQLITE_INT_MAX:
        raise HTTPException(status_code=404, detail="Preset not found")


@app.get(
    "/api/user/filter-presets",
    operation_id="listFilterPresets",
    response_model=list[FilterPresetResponse],
)
async def list_filter_presets(
    page: str, request: Request, response: Response
) -> list[FilterPresetResponse]:
    """List the user's personal presets plus all server presets for a page."""
    _require_authenticated(request)
    if page not in storage.FILTER_PRESET_PAGES:
        raise HTTPException(
            status_code=422,
            detail="page must be one of: " + ", ".join(storage.FILTER_PRESET_PAGES),
        )
    presets = await storage.list_filter_presets(request.state.username, page)
    response.headers["Cache-Control"] = "no-store"
    return [FilterPresetResponse(**preset) for preset in presets]


@app.post(
    "/api/user/filter-presets",
    operation_id="createFilterPreset",
    response_model=FilterPresetResponse,
    status_code=201,
)
async def create_filter_preset(
    body: CreateFilterPresetRequest, request: Request
) -> FilterPresetResponse:
    """Save a filter combination as a named preset (server scope: operator+)."""
    _require_authenticated(request)
    _check_allow_list(request)
    if body.scope == "server":
        _require_operator(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Preset name cannot be empty")
    _validate_filter_preset_filters(body.page, body.filters)
    try:
        preset = await storage.create_filter_preset(
            request.state.username, body.page, body.scope, name, body.filters
        )
    except storage.FilterPresetLimitExceeded as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except storage.FilterPresetNameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FilterPresetResponse(**preset)


@app.put(
    "/api/user/filter-presets/{preset_id}",
    operation_id="updateFilterPresetEndpoint",
    response_model=FilterPresetResponse,
)
async def update_filter_preset_endpoint(
    preset_id: int, body: UpdateFilterPresetRequest, request: Request
) -> FilterPresetResponse:
    """Rename and/or replace the filters of a preset the user may manage."""
    _require_authenticated(request)
    _check_allow_list(request)
    _validate_preset_id(preset_id)
    # Ownership-scoped fetch: another user's personal preset is
    # indistinguishable from a missing one, before any body validation.
    existing = await storage.get_manageable_filter_preset(
        preset_id, request.state.username
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    if existing["scope"] == "server":
        _require_operator(request)
    name = body.name.strip() if body.name is not None else None
    if name == "":
        raise HTTPException(status_code=422, detail="Preset name cannot be empty")
    if body.filters is not None:
        _validate_filter_preset_filters(existing["page"], body.filters)
    try:
        preset = await storage.update_filter_preset(
            preset_id, request.state.username, name, body.filters
        )
    except storage.FilterPresetNotFound as exc:
        raise HTTPException(status_code=404, detail="Preset not found") from exc
    except storage.FilterPresetNameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FilterPresetResponse(**preset)


@app.delete(
    "/api/user/filter-presets/{preset_id}",
    operation_id="deleteFilterPresetEndpoint",
    status_code=204,
)
async def delete_filter_preset_endpoint(preset_id: int, request: Request) -> None:
    """Delete a preset the user may manage (own personal or server)."""
    _require_authenticated(request)
    _check_allow_list(request)
    _validate_preset_id(preset_id)
    existing = await storage.get_manageable_filter_preset(
        preset_id, request.state.username
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    if existing["scope"] == "server":
        _require_operator(request)
    try:
        await storage.delete_filter_preset(preset_id, request.state.username)
    except storage.FilterPresetNotFound as exc:
        raise HTTPException(status_code=404, detail="Preset not found") from exc


# --- User token endpoints ---


@app.get("/api/user/tokens", operation_id="getUserTokensEndpoint")
async def get_user_tokens_endpoint(request: Request) -> JSONResponse:
    """Get the current user's saved tokens."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    # Verify user exists in DB (prevents reading tokens for unregistered usernames)
    user = await storage.get_user_by_username(username)
    if not user:
        return JSONResponse(
            content={
                "github_token": "",
                "jira_email": "",
                "jira_token": "",
                "jira_sa_token": "",
            },
            headers={"Cache-Control": "no-store"},
        )
    tokens = await storage.get_user_tokens(username)
    return JSONResponse(
        content=tokens,
        headers={"Cache-Control": "no-store"},
    )


@app.put("/api/user/tokens", operation_id="saveUserTokensEndpoint")
async def save_user_tokens_endpoint(request: Request) -> JSONResponse:
    """Save tokens for the current user. Tokens are encrypted at rest.

    Each field is set independently: an omitted field is left unchanged, an
    empty string or ``null`` clears that one field, and a string sets it.
    Clearing one field never touches the others.
    """
    _require_reviewer(request)
    _check_allow_list(request)
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    # Verify user exists in DB
    user = await storage.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Register first.")
    body = await _read_json_object(request)

    # Presence-based, so an omitted field keeps its stored value and an empty
    # one clears it. Stringifying first would store a null as the literal "None".
    kwargs: dict[str, str] = {}
    for field in ("github_token", "jira_email", "jira_token", "jira_sa_token"):
        if field not in body:
            continue
        value = body[field]
        if value is None:
            kwargs[field] = ""
        elif isinstance(value, str):
            kwargs[field] = value.strip()
        else:
            raise HTTPException(
                status_code=400, detail=f"{field} must be a string or null"
            )
        if (
            field == "jira_sa_token"
            and kwargs[field]
            and classify_jira_secret(kwargs[field]) is None
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "jira_sa_token must be an ATOA-prefixed Atlassian OAuth "
                    "2.0 credential client secret or an ATST-prefixed "
                    "service-account scoped token — use jira_token for "
                    "regular credentials"
                ),
            )

    if not kwargs:
        return JSONResponse(content={"ok": True, "saved": False})

    await storage.save_user_tokens(username, **kwargs)
    logger.debug(f"Saved tokens for user '{username}': {sorted(kwargs)}")
    return JSONResponse(content={"ok": True, "saved": True})


# --- Admin endpoints ---


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# --- Live log broadcast for SSE streaming ---
# Each listener is (owning_loop, queue) so emit() can wake the correct loop
# from non-asyncio logging threads via call_soon_threadsafe.
_log_listeners: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = set()
_log_listeners_lock = threading.Lock()


class _SSELogHandler(logging.Handler):
    """Logging handler that broadcasts formatted records to SSE listeners."""

    def emit(self, record: logging.LogRecord) -> None:
        with _log_listeners_lock:
            if not _log_listeners:
                return
            listeners = list(_log_listeners)
        try:
            msg = self.format(record)
            clean = _ANSI_RE.sub("", msg)

            def _enqueue(q: asyncio.Queue[str], line: str) -> None:
                try:
                    q.put_nowait(line)
                except asyncio.QueueFull:
                    pass

            for loop, q in listeners:
                try:
                    loop.call_soon_threadsafe(_enqueue, q, clean)
                except Exception:
                    logger.debug("SSE log enqueue failed for a listener", exc_info=True)
        except Exception:
            logger.debug("SSE log emit failed", exc_info=True)


# Singleton handler instance — attached to all loggers once.
_sse_log_handler: _SSELogHandler | None = None


def _install_sse_log_handler() -> None:
    """Install SSE broadcast handler on all known loggers.

    simple_logger creates per-module loggers with propagate=False,
    so attaching to the root logger alone catches nothing.
    This mirrors _install_job_id_filter's approach of walking
    every registered logger.
    """
    global _sse_log_handler
    if _sse_log_handler is not None:
        return
    handler = _SSELogHandler()
    handler.setLevel(logging.DEBUG)
    # Copy formatter from the first RotatingFileHandler we find
    for name in [None, *list(logging.Logger.manager.loggerDict)]:
        lg = logging.getLogger(name)
        for h in getattr(lg, "handlers", []):
            if isinstance(h, logging.handlers.RotatingFileHandler) and h.formatter:
                handler.setFormatter(h.formatter)
                break
        if handler.formatter:
            break
    # Attach to every logger
    for name in [None, *list(logging.Logger.manager.loggerDict)]:
        lg = logging.getLogger(name)
        if hasattr(lg, "addHandler"):
            lg.addHandler(handler)
    _sse_log_handler = handler


_MAX_LOG_TAIL_BYTES = 8 * 1024 * 1024  # 8 MiB hard cap independent of line count
_LOG_TAIL_CHUNK_BYTES = 256 * 1024  # 256 KiB backwards reads


def _read_log_tail(
    path: Path,
    n: int,
    *,
    max_bytes: int = _MAX_LOG_TAIL_BYTES,
    chunk_size: int = _LOG_TAIL_CHUNK_BYTES,
) -> list[str]:
    """Read last ``n`` lines from a log file with a hard max-byte budget.

    Reads backwards in fixed-size chunks until ``n`` lines are collected or
    ``max_bytes`` is reached (whichever first). Intended for ``asyncio.to_thread``.
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return []
        parts: list[bytes] = []
        pos = size
        bytes_read = 0
        newline_count = 0
        while pos > 0 and bytes_read < max_bytes and newline_count <= n:
            to_read = min(chunk_size, pos, max_bytes - bytes_read)
            pos -= to_read
            f.seek(pos)
            chunk = f.read(to_read)
            parts.append(chunk)
            bytes_read += to_read
            newline_count += chunk.count(b"\n")
        raw = b"".join(reversed(parts))
        text = raw.decode("utf-8", errors="replace")
        return text.splitlines()[-n:]


@app.get("/api/admin/logs/stream", operation_id="streamLogs")
async def stream_logs(
    request: Request,
    lines: int = Query(100, ge=1, le=2000, description="Initial tail lines"),
    level: str = Query(
        "", description="Filter by log level (INFO, WARNING, ERROR, DEBUG)"
    ),
) -> StreamingResponse:
    """Stream server logs via SSE (admin only)."""
    _require_admin(request)

    level_filter = level.upper().strip() if level else ""

    def _matches_level(line: str) -> bool:
        if not level_filter:
            return True
        return level_filter in line

    async def event_generator() -> AsyncIterator[str]:
        # Send initial tail from log file
        if _LOG_FILE:
            log_path = Path(_LOG_FILE)
            if log_path.exists():
                try:
                    tail_lines = await asyncio.to_thread(
                        _read_log_tail, log_path, lines
                    )
                    for line in tail_lines:
                        clean = _ANSI_RE.sub("", line)
                        if _matches_level(clean):
                            yield f"event: log\ndata: {clean.replace(chr(10), '  ')}\n\n"
                except OSError:
                    pass

        # Stream live logs via broadcast queue
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()
        listener = (loop, queue)
        with _log_listeners_lock:
            _log_listeners.add(listener)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=30)
                    if _matches_level(line):
                        yield f"event: log\ndata: {line.replace(chr(10), '  ')}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            with _log_listeners_lock:
                _log_listeners.discard(listener)

    logger.info(
        "GET /api/admin/logs/stream: streaming logs (lines=%s, level=%s)",
        lines,
        level_filter or "ALL",
    )
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/admin/token-usage", operation_id="getTokenUsage")
async def get_token_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
    call_type: str | None = None,
    group_by: str | None = None,
) -> dict[str, Any]:
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


@app.get("/api/admin/token-usage/summary", operation_id="getTokenUsageDashboard")
async def get_token_usage_dashboard(request: Request) -> dict[str, Any]:
    """Get high-level token usage summary for dashboard. Admin only."""
    _require_admin(request)
    return await storage.get_token_usage_dashboard_summary()


@app.get("/api/admin/token-usage/{job_id}", operation_id="getTokenUsageForJob")
async def get_token_usage_for_job(request: Request, job_id: str) -> dict[str, Any]:
    """Get token usage breakdown for a specific job. Admin only."""
    _require_admin(request)
    records = await storage.get_token_usage_for_job(job_id)
    if not records:
        raise HTTPException(
            status_code=404, detail="No token usage records found for this job"
        )
    return {"job_id": job_id, "records": records}


@app.get("/api/admin/transcripts", operation_id="listSessionTranscripts")
async def list_session_transcripts(
    request: Request,
    job_id: str = Query("", description="Filter to transcripts of one run"),
) -> dict[str, Any]:
    """List recorded pi-sidecar session transcripts with business context.

    Admin only — transcripts contain full (redacted) prompt/response/tool
    content. ``job_id`` filters to sessions whose ``ai_token_usage`` rows
    belong to that run (join via session_id, #328).
    """
    _require_admin(request)

    infos = await asyncio.to_thread(sidecar_transcripts.list_transcripts)
    if job_id:
        usage_rows = await storage.get_token_usage_for_job(job_id)
        job_sessions = {
            row["session_id"] for row in usage_rows if row.get("session_id")
        }
        infos = [info for info in infos if info.session_id in job_sessions]

    usage_context = await storage.get_usage_context_for_sessions(
        [info.session_id for info in infos]
    )
    chat_context = await storage.get_chat_context_for_sessions(
        [info.session_id for info in infos]
    )

    items = []
    for info in infos:
        context: dict[str, Any] = {}
        usage = usage_context.get(info.session_id)
        if usage:
            context.update(
                {
                    "job_id": usage.get("job_id", ""),
                    "call_type": usage.get("call_type", ""),
                    "ai_provider": usage.get("ai_provider", ""),
                    "ai_model": usage.get("ai_model", ""),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            )
        chat = chat_context.get(info.session_id)
        if chat:
            context.update(
                {
                    "chat_username": chat.get("username", ""),
                    "chat_last_activity": chat.get("last_activity", ""),
                }
            )
        items.append(transcript_summary(info, context))

    live_recording = await sidecar_transcripts.live_recording_enabled()
    return strip_sensitive_from_response(
        {
            # Live sidecar state when reachable; the setting value otherwise.
            # None (unreachable) is distinct from False (recording off).
            "recording_enabled": (
                get_settings().record_transcripts
                if live_recording is None
                else live_recording
            ),
            "count": len(items),
            "items": items,
        }
    )


@app.get(
    "/api/admin/transcripts/{session_id}", operation_id="downloadSessionTranscript"
)
async def download_session_transcript(
    request: Request,
    session_id: str,
) -> Response:
    """Download one session transcript JSONL file. Admin only, streaming.

    The session_id is validated against the strict UUID pattern before any
    filesystem access — a malformed value can never traverse paths. Full
    content, never truncated.
    """
    _require_admin(request)

    path = transcript_path(session_id)
    if path is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid session id — expected a sidecar session UUID",
        )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Transcript not found")

    # Sync generator: Starlette iterates sync generators in a threadpool, so
    # blocking chunk reads never stall the event loop.
    def file_stream() -> Iterator[bytes]:
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        file_stream(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{session_id}.jsonl"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/admin/users/create", operation_id="adminCreateUserEndpoint")
async def admin_create_user_endpoint(
    request: Request, body: AdminCreateUserRequest
) -> JSONResponse:
    """Admin creates a new user. Does NOT set session cookies (admin stays logged in).

    Response ``can_view_reports`` is the *effective* value (True for admins),
    matching ``GET /api/auth/me``. The stored DB flag remains the request value
    and is not force-set for admins.
    """
    _require_admin(request)

    username = body.username.strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    # Block reserved username prefixes (e.g., rootcoz-ai)
    if username.startswith("rootcoz"):
        raise HTTPException(
            status_code=400,
            detail="Usernames starting with 'rootcoz' are reserved for system use",
        )

    role = body.role
    if role not in storage.VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Role must be one of: {', '.join(sorted(storage.VALID_ROLES))}",
        )

    # Store the request flag as-is (do not force True for admins — demotion
    # must not leave an accidental grant). Response uses effective value.
    stored_can_view_reports = body.can_view_reports
    server_grant = body.can_use_server_providers

    try:
        if role == "admin":
            username, raw_key = await storage.create_admin_user(
                username,
                can_view_reports=stored_can_view_reports,
                can_use_server_providers=server_grant,
            )
        else:
            _, raw_key = await storage.create_user(
                username,
                status="active",
                role=role,
                can_view_reports=stored_can_view_reports,
                can_use_server_providers=server_grant,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to create user '%s'", username)
        raise HTTPException(status_code=500, detail="Internal server error") from exc

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' created {role} user '{username}'"
        f" (can_view_reports={stored_can_view_reports}, can_use_server_providers={server_grant})"
    )
    return JSONResponse(
        content={
            "username": username,
            "api_key": raw_key,
            "role": role,
            "can_use_server_providers": role == "admin" or server_grant,
            "can_view_reports": effective_can_view_reports(
                role == "admin", stored_can_view_reports
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/admin/users/{username}", operation_id="deleteUserEndpoint")
async def delete_user_endpoint(request: Request, username: str) -> dict[str, Any]:
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

    notify_pending_count_changed()
    await _cleanup_revoked_ai_sessions()
    logger.info(f"[AUDIT] Admin '{request.state.username}' deleted user '{username}'")
    return {"deleted": username}


@app.put("/api/admin/users/{username}/role", operation_id="changeUserRoleEndpoint")
async def change_user_role_endpoint(request: Request, username: str) -> JSONResponse:
    """Change a user's role (reviewer, operator, or admin).

    When promoting to admin, an API key is generated and returned only if
    the user doesn't already have one.
    For other role changes, the existing API key is preserved.
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

    # Promoting a pending user removes them from the pending list.
    notify_pending_count_changed()

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' changed role of '{username}' to '{new_role}'"
    )

    content: dict[str, Any] = {"username": username, "role": new_role}
    if raw_key:
        content["api_key"] = raw_key
    return JSONResponse(
        content=content,
        headers={"Cache-Control": "no-store"},
    )


@app.put(
    "/api/admin/users/{username}/can-view-reports",
    operation_id="setUserCanViewReports",
)
async def set_user_can_view_reports_endpoint(
    request: Request, username: str, body: SetCanViewReportsRequest
) -> JSONResponse:
    """Set whether a user can access /api/reports/* (orthogonal to role)."""
    _require_admin(request)
    value = body.can_view_reports

    try:
        updated = await storage.set_user_can_view_reports(username, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    logger.info(
        f"[AUDIT] Admin '{request.state.username}' set can_view_reports={value}"
        f" for user '{username}'"
    )
    return JSONResponse(
        content={"username": username, "can_view_reports": value},
    )


@app.put(
    "/api/admin/users/{username}/can-use-server-providers",
    operation_id="setUserCanUseServerProviders",
)
async def set_user_can_use_server_providers(
    request: Request, username: str, body: SetCanUseServerProvidersRequest
) -> JSONResponse:
    """Grant or revoke access to server AI credentials."""
    _require_admin(request)
    try:
        updated = await storage.set_user_can_use_server_providers(
            username, body.can_use_server_providers
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    logger.info(
        "[AUDIT] Admin '%s' set can_use_server_providers=%s for user '%s'",
        request.state.username,
        body.can_use_server_providers,
        username,
    )
    return JSONResponse(
        content={
            "username": username,
            "can_use_server_providers": body.can_use_server_providers,
        }
    )


@app.get("/api/admin/free-models", operation_id="listFreeAiModelsEndpoint")
async def list_free_ai_models_endpoint(request: Request) -> dict[str, Any]:
    """List admin-declared free provider+model pairs (admin only)."""
    _require_admin(request)
    models = await storage.list_free_ai_models()
    return {"models": models, "version": storage.free_models_version(models)}


@app.put("/api/admin/free-models", operation_id="setFreeAiModelsEndpoint")
async def set_free_ai_models_endpoint(
    request: Request, body: SetFreeAiModelsRequest
) -> dict[str, Any]:
    """Replace the free provider+model list (admin only).

    New pairs are validated against the current sidecar catalog (provider
    case-insensitively, model id exactly) so typos cannot silently create
    free entries that never match anything. Pairs already stored are
    passed through unvalidated: their model may have left the catalog
    since they were saved, and a stale survivor must not block unrelated
    edits — the CLI and Users page resubmit the existing list for every
    add/remove. Stale pairs are ignored gracefully at runtime.

    ``free ⊆ included`` is normalized here: a free pair on a managed
    provider (stored key or selection) is auto-included in that
    provider's selection, mirroring the server-providers PUT — otherwise
    scoped_models would filter it out of the restricted catalog.
    """
    _require_admin(request)
    existing = {
        (row["provider"].lower(), row["model"])
        for row in await storage.list_free_ai_models()
    }
    catalog = await _get_model_catalog()
    catalog_pairs = {
        (str(entry.get("provider") or "").lower(), str(entry.get("id") or ""))
        for entry in catalog
    }
    # Key-scoped pairs (#350): a keyed provider's captured listing is a
    # valid free-model target even for models the static catalog lacks.
    key_scoped_pairs = {
        (row["provider"].lower(), row["model"])
        for row in await storage.list_server_provider_key_models()
    }
    unknown: list[dict[str, str]] = []
    for entry in body.models:
        if (entry.provider.lower(), entry.model) in existing:
            continue
        if (entry.provider.lower(), entry.model) not in catalog_pairs and (
            entry.provider.lower(),
            entry.model,
        ) not in key_scoped_pairs:
            unknown.append({"provider": entry.provider, "model": entry.model})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Unknown provider/model pairs in free-models list",
                "unknown": unknown,
            },
        )
    try:
        stored = await storage.set_free_ai_models(
            [(entry.provider, entry.model) for entry in body.models],
            request.state.username,
            expected_version=body.expected_version,
        )
    except storage.FreeModelsConflict as exc:
        # Surface the current list so the losing client can re-merge
        # instead of guessing what changed underneath it.
        current = await storage.list_free_ai_models()
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(exc),
                "models": current,
                "version": storage.free_models_version(current),
            },
        ) from exc
    logger.info(
        "[AUDIT] Admin '%s' set free AI models: %d pair(s)",
        request.state.username,
        len(stored),
    )
    # free ⊆ included (#347): a managed provider (stored key or selection)
    # only exposes its selection, so a free pair outside it would vanish
    # from the restricted catalog. Auto-include it — the same normalization
    # the server-providers PUT applies.
    auto_included = await storage.auto_include_free_pairs(
        [(entry.provider, entry.model) for entry in body.models],
        request.state.username or "admin",
    )
    if auto_included:
        logger.info(
            "[AUDIT] Admin '%s' auto-included %d free pair(s) into managed "
            "provider selections",
            request.state.username,
            len(auto_included),
        )
    return {
        "models": stored,
        "version": storage.free_models_version(stored),
    }


# --- Admin server provider credentials (#347) ---


async def _seed_server_provider_selection(provider: str, updated_by: str) -> None:
    """Seed a selection for a newly-keyed provider (#347 r4, #350).

    A stored key makes a provider *managed* (see
    ``ai_client._load_server_pool_selection``), and a managed provider with
    no selection rows exposes nothing — so adding a verified key to a
    provider that served its whole catalog via deployment credentials
    would silently withdraw every model from discovery and
    ``require_selected_server_pair``. Seeding one row per model keeps the
    exposure exactly as it was: the key-scoped listing when the last
    add/rotate captured one (#350), the full catalog otherwise. An existing
    admin selection stays authoritative — a restriction must survive a key
    add — and the rotate path never reseeds for the same reason.
    """
    if any(
        row["provider"].lower() == provider.lower()
        for row in await storage.list_server_provider_models()
    ):
        return
    free_models = {
        row["model"]
        for row in await storage.list_free_ai_models()
        if row["provider"].lower() == provider.lower()
    }
    key_scoped_rows = [
        row
        for row in await storage.list_server_provider_key_models()
        if row["provider"].lower() == provider.lower()
    ]
    if key_scoped_rows:
        # Key-scoped (#350): seed exactly what the key can access — the
        # static catalog may list models this key cannot use, and may lack
        # models it can. A sentinel-only listing (verified empty) seeds
        # nothing: the key can access no models at all.
        base_models = [row["model"] for row in key_scoped_rows if row["model"]]
    else:
        base_models = [
            entry["id"]
            for entry in await _get_model_catalog()
            if isinstance(entry.get("provider"), str)
            and entry["provider"].lower() == provider.lower()
            and isinstance(entry.get("id"), str)
            and entry["id"]
        ]
    entries = [{"id": model, "free": model in free_models} for model in base_models]
    # Free pairs whose model left the catalog survive the seeding (stale
    # pairs are ignored at runtime) so a key add never rewrites unrelated
    # free-model state.
    entries.extend(
        {"id": model, "free": True} for model in sorted(free_models - set(base_models))
    )
    if entries:
        await storage.set_server_provider_models(provider, entries, updated_by)
        logger.info(
            "[AUDIT] Seeded full-catalog model selection for newly-keyed "
            "provider '%s' (%d models)",
            provider,
            len(entries),
        )


async def _key_scoped_capture_superseded(
    provider: str, key: str, generation: int | None
) -> bool:
    """Whether the credential moved on from the one a discovery used (#350 r2).

    A capture is superseded when the stored key no longer equals the key
    the discovery ran with (unreadable counts as moved on), or when the
    credential generation changed — which also covers a rotation to the
    same key value and a concurrent removal.
    """
    try:
        current_key = await storage.get_server_ai_credential(provider)
    except storage.UnreadableAiCredentialsError:
        return True
    if current_key != key:
        return True
    return await storage.get_server_credential_generation(provider) != generation


async def _capture_key_scoped_models(
    provider: str,
    key: str,
    *,
    keep_on_failure: bool,
    generation: int | None,
) -> tuple[list[dict[str, Any]], str]:
    """Run key-scoped discovery and persist its result (#350).

    Returns ``(models, status)`` where status is ``"supported"`` (rows
    stored), ``"unsupported"`` (no listing endpoint — rows cleared),
    ``"unavailable"`` (discovery failed — rows kept on refresh
    (``keep_on_failure=True``, key unchanged) so a sidecar hiccup never
    reverts the provider to the static catalog; cleared after add/rotate
    (``keep_on_failure=False``) so the previous key's listing is never
    exposed as the new key's), or ``"superseded"`` (the credential changed
    during discovery — the capture is discarded so one key never wears
    another key's listing; a rotation to the same key value still bumps
    the generation and supersedes). Every row write is bound to
    ``generation`` — the credential generation this discovery ran against —
    so a capture that loses the race to a concurrent add/rotate/remove
    writes nothing — and a rotation landing between the superseded check
    and the write makes the conditional write itself return ``False``, which
    this function reports as ``"superseded"`` too (#350 r3): a skipped
    write is never reported as a stored capture. Only ``source: "listing"`` models are stored:
    manifest-only models are not key-verified. A supported listing with
    zero accessible models is stored as a sentinel row — "verified empty"
    — so the pool exposes nothing instead of falling back to the catalog.
    """
    try:
        from rootcoz.ai_client import models_for_api_key

        discovery = await models_for_api_key(provider, key)
    except httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError:
        logger.warning(
            "Key-scoped model discovery unavailable for provider=%s", provider
        )
        # A failed capture after add/rotate must not leave the previous
        # key's rows exposed as the new key's verified listing; a failed
        # refresh (same key) keeps the last captured listing instead.
        if not await _key_scoped_capture_superseded(provider, key, generation):
            if keep_on_failure:
                stored = await storage.set_server_provider_key_capture_status(
                    provider, "unavailable", expected_generation=generation
                )
            else:
                stored = await storage.set_server_provider_key_models(
                    provider,
                    [],
                    status="unavailable",
                    expected_generation=generation,
                )
            # A skipped write (rotation landed between the superseded check
            # and the conditional write) is a superseded capture, never a
            # success — nothing was stored (#350 r3).
            if not stored:
                return [], "superseded"
        return [], "unavailable"
    if await _key_scoped_capture_superseded(provider, key, generation):
        logger.warning(
            "Key-scoped capture superseded for provider=%s (credential changed"
            " during discovery); discarding",
            provider,
        )
        return [], "superseded"
    if not discovery["modelListingSupported"]:
        stored = await storage.set_server_provider_key_models(
            provider,
            [],
            status="unsupported",
            expected_generation=generation,
        )
        return ([], "unsupported") if stored else ([], "superseded")
    # Manifest-only models are the provider manifest's, not the key's — a
    # restricted key must never see them as verified (#350 r1).
    listing_models = [
        entry for entry in discovery["models"] if entry.get("source") != "manifest"
    ]
    if listing_models:
        stored = await storage.set_server_provider_key_models(
            provider,
            listing_models,
            status="supported",
            expected_generation=generation,
        )
        return (listing_models, "supported") if stored else ([], "superseded")
    # Supported listing, zero key-accessible models: the sentinel row keeps
    # "verified empty" distinct from "no listing" so the pool exposes
    # nothing instead of falling back to the static catalog (#350 r1).
    stored = await storage.set_server_provider_key_models(
        provider,
        [{"id": "", "sentinel": True}],
        status="supported",
        expected_generation=generation,
    )
    return ([], "supported") if stored else ([], "superseded")


async def _verify_and_store_server_provider_key(
    provider: str,
    body: ServerProviderKeyInput,
    request: Request,
    *,
    replacing: bool,
) -> JSONResponse:
    """Shared add/rotate path: verify the key with a real turn, then store."""
    # Resolve to the sidecar's exact-case id up front (#347): verification,
    # the duplicate check, and storage all see the canonical spelling, so
    # a lowercase add behaves identically to the catalog's ``EnMaaS`` and
    # cannot create a mixed-case duplicate row.
    canonical = await _canonical_key_provider_id(provider)
    if canonical is None:
        raise HTTPException(
            status_code=400, detail="Provider does not support session API keys"
        )
    provider = canonical
    key = body.api_key.get_secret_value()
    if error := _validate_submitted_api_key(key):
        raise HTTPException(status_code=400, detail=error)
    from rootcoz.ai_client import verify_ai_key

    model = body.verification_model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Select a model to verify the key")
    outcome = await verify_ai_key(provider, model, key)
    if outcome != "accepted":
        # Mirror the user flow: report the outcome, never store an untested key.
        message = {
            "rejected": "Check the key and selected model; existing credential unchanged.",
            "inconclusive": "Try again when the provider is available; existing credential unchanged.",
        }.get(outcome)
        return JSONResponse(
            content=strip_sensitive_from_response(
                {
                    "outcome": outcome,
                    "ok": False,
                    **({"message": message} if message else {}),
                }
            ),
            headers={"Cache-Control": "no-store"},
        )
    # Re-check the credential AFTER verification and BEFORE the write: the
    # sidecar round-trip above can take seconds, and a concurrent add during
    # it must not be silently overwritten by this one, nor a concurrent
    # removal resurrected without its model selections.
    exists = provider.lower() in {
        row["provider"].lower() for row in await storage.list_server_ai_providers()
    }
    if exists and not replacing:
        raise HTTPException(
            status_code=409,
            detail=f"Provider '{provider}' already has a stored key; use rotate-key",
        )
    if replacing and not exists:
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' has no stored key"
        )
    # The generation this write assigned, returned from inside the upsert's
    # transaction — never re-read: an overlapping rotation (even to the same
    # key) landing before a separate read would hand this capture the newer
    # credential's generation and let its stale snapshot through (#350 r3).
    generation = await storage.set_server_ai_credential(
        provider, key, request.state.username or "admin"
    )
    # Key-scoped capture (#350): after a successful verification, record the
    # models this key can actually access. A listing-unsupported provider or
    # a transient discovery failure never blocks the add — a failed
    # discovery clears any previous key's rows (this key's listing is
    # unknown, never the old key's), and the server pool falls back to the
    # static catalog for this provider. The capture is bound to the
    # credential generation just written (#350 r2): a concurrent rotate
    # bumps it and this capture's writes are skipped.
    key_models, capture_status = await _capture_key_scoped_models(
        provider,
        key,
        keep_on_failure=False,
        generation=generation,
    )
    if not replacing:
        await _seed_server_provider_selection(
            provider, request.state.username or "admin"
        )
    logger.info(
        "[AUDIT] Admin '%s' stored server provider key for '%s' (verified%s)",
        request.state.username,
        provider,
        f", {len(key_models)} key-scoped models"
        if capture_status == "supported"
        else "",
    )
    return JSONResponse(
        content=strip_sensitive_from_response({"outcome": "accepted", "ok": True}),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/admin/server-providers", operation_id="listServerProvidersEndpoint")
async def list_server_providers_endpoint(request: Request) -> JSONResponse:
    """List server provider key state and per-model selection (admin only).

    Never returns keys. One entry per catalog provider (plus stored
    providers that left the catalog), each with key presence, verification
    state, a ``managed`` flag, and its models with ``included`` + ``free``
    flags — unmanaged providers (no stored key, no selection rows) report
    their whole catalog as included, matching what the server pool
    actually exposes for them.
    """
    _require_admin(request)
    catalog = await _get_model_catalog()
    # Case-insensitive join (#347): storage matches providers ignoring case,
    # so a key stored as "Google" must key the catalog's "google" entry —
    # an exact-name lookup would split it into keyed + unkeyed rows.
    stored = {
        normalize_provider(row["provider"]): row
        for row in await storage.list_server_ai_providers()
    }
    selections: dict[str, dict[str, bool]] = {}
    for row in await storage.list_server_provider_models():
        selections.setdefault(row["provider"].lower(), {})[row["model"]] = True
    free_by_provider: dict[str, set[str]] = {}
    for row in await storage.list_free_ai_models():
        free_by_provider.setdefault(row["provider"].lower(), set()).add(row["model"])
    supported = {p.lower() for p in await supported_key_providers()}
    # Key-scoped listings captured at add/rotate (#350): for a keyed
    # provider these replace the static catalog as the offered model set —
    # a restricted key must not offer models it cannot use. Providers with
    # no rows (listing unsupported / discovery failed) keep the catalog.
    key_scoped: dict[str, dict[str, dict[str, Any]]] = {}
    # Stored spelling per lowercase join key (#350 r2): fallback entries for
    # providers the catalog no longer lists must keep the stored casing —
    # pi-config matches session providers by their exact id.
    key_scoped_ids: dict[str, str] = {}
    for row in await storage.list_server_provider_key_models():
        key_scoped.setdefault(row["provider"].lower(), {})[row["model"]] = row
        key_scoped_ids.setdefault(row["provider"].lower(), row["provider"])

    models_by_provider: dict[str, list[dict[str, Any]]] = {}
    for entry in catalog:
        provider = entry.get("provider")
        model = entry.get("id")
        if not isinstance(provider, str) or not isinstance(model, str) or not model:
            continue
        scoped_rows = key_scoped.get(provider.lower())
        if scoped_rows is not None and model not in scoped_rows:
            # Key-scoped (#350): the catalog model is not accessible to the
            # stored key — do not offer it.
            continue
        models_by_provider.setdefault(provider, []).append(
            {"id": model, "name": entry.get("name") or model}
        )
    # Key-scoped models the static catalog lacks still belong to the offered
    # set (#350) — the listing is the source of truth for a keyed provider.
    # The empty-string sentinel (verified-empty listing) contributes no
    # offered model but still marks the provider key-scoped.
    for provider_lower, scoped_rows in key_scoped.items():
        provider = next(
            (p for p in models_by_provider if p.lower() == provider_lower),
            key_scoped_ids.get(provider_lower, provider_lower),
        )
        known = {m["id"] for m in models_by_provider.get(provider, [])}
        for model, row in sorted(scoped_rows.items()):
            if model and model not in known:
                models_by_provider.setdefault(provider, []).append(
                    {"id": model, "name": row.get("name") or model}
                )
    # Stored selections whose model left the offered set stay visible so
    # the UI can render and clear them (stale pairs are ignored at runtime).
    for provider_lower, selected in selections.items():
        provider = next(
            (p for p in models_by_provider if p.lower() == provider_lower),
            provider_lower,
        )
        known = {m["id"] for m in models_by_provider.get(provider, [])}
        for model in selected:
            if model not in known:
                models_by_provider.setdefault(provider, []).append({"id": model})

    providers = []
    # Case-insensitive union (#347): the stored map is keyed by the
    # normalized (lowercase) spelling, so its keys must fold into the
    # catalog's mixed-case entry (e.g. ``EnMaaS``) instead of splitting
    # the listing into a second lowercase row.
    listed_ids = {p.lower(): p for p in models_by_provider}
    for stored_id in stored:
        listed_ids.setdefault(stored_id.lower(), stored_id)
    for provider in sorted(listed_ids.values()):
        provider_lower = provider.lower()
        # Managed = stored selection rows or a stored key — the same rule
        # ``ai_client._load_server_pool_selection`` scopes the server pool
        # with. An unmanaged provider exposes its whole catalog, so the
        # listing must report every catalog model included: reporting them
        # unchecked would let one Free-pill click PUT a draft with a single
        # model and silently withdraw the rest of the catalog (#347 r4).
        managed = provider_lower in selections or normalize_provider(provider) in stored
        selected = selections.get(provider_lower, {})
        models = [
            {
                "id": m["id"],
                "name": m.get("name") or m["id"],
                "included": (m["id"] in selected) if managed else True,
                "free": m["id"] in free_by_provider.get(provider_lower, set()),
            }
            for m in models_by_provider.get(provider, [])
        ]
        record = stored.get(normalize_provider(provider))
        providers.append(
            {
                "provider": provider,
                "managed": managed,
                "has_key": record is not None,
                "verified": record is not None,
                "verified_at": record.get("verified_at", "") if record else "",
                "supports_key": provider.lower() in supported,
                # True when the last add/rotate captured a key-scoped listing
                # (#350) — the models above are exactly what the key can
                # access. A keyed provider without it falls back to the
                # static catalog (no listing endpoint / failed discovery).
                "key_scoped": provider_lower in key_scoped,
                # Outcome of the last key-scoped capture (#350 r2):
                # supported / unsupported / unavailable. "unavailable" can
                # coexist with key_scoped true — a failed refresh kept the
                # last snapshot — telling the admin the listed models are
                # stale, not that no listing endpoint exists.
                "key_scoped_status": record.get("key_capture_status")
                if record
                else None,
                "models": models,
            }
        )
    return JSONResponse(
        content=strip_sensitive_from_response({"providers": providers}),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/admin/server-providers", operation_id="addServerProvider")
async def add_server_provider(
    request: Request, body: ServerProviderKeyInput
) -> JSONResponse:
    """Add a server provider key (admin only) — verified before storing."""
    _require_admin(request)
    provider = body.provider.strip()
    if provider.lower() in {
        row["provider"].lower() for row in await storage.list_server_ai_providers()
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Provider '{provider}' already has a stored key; use rotate-key",
        )
    return await _verify_and_store_server_provider_key(
        provider, body, request, replacing=False
    )


@app.post(
    "/api/admin/server-providers/{provider:path}/rotate-key",
    operation_id="rotateServerProviderKey",
)
async def rotate_server_provider_key(
    provider: str, request: Request, body: ServerProviderKeyInput
) -> JSONResponse:
    """Replace a server provider key (admin only) — re-verified before storing."""
    _require_admin(request)
    if provider.lower() not in {
        row["provider"].lower() for row in await storage.list_server_ai_providers()
    }:
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' has no stored key"
        )
    # The rotate body carries the provider again; the path is authoritative.
    return await _verify_and_store_server_provider_key(
        provider, body, request, replacing=True
    )


@app.post(
    "/api/admin/server-providers/{provider:path}/refresh-models",
    operation_id="refreshServerProviderModels",
)
async def refresh_server_provider_models(
    provider: str, request: Request
) -> JSONResponse:
    """Re-run key-scoped discovery with the stored key (admin only).

    Captures permission drift since add/rotate without re-entering the key:
    the refreshed listing replaces the stored rows, existing admin
    selections survive (runtime filtering withdraws newly inaccessible
    models), and the selection is NEVER reseeded — a deliberately empty
    selection (an admin who deselected every model) must survive a
    refresh instead of quietly re-including the whole listing. Never
    re-verifies with an LLM call; a dead key simply reports
    ``unavailable`` and keeps the last captured listing.
    """
    _require_admin(request)
    canonical = await _canonical_key_provider_id(provider)
    if canonical is None:
        raise HTTPException(
            status_code=400, detail="Provider does not support session API keys"
        )
    provider = canonical
    try:
        key = await storage.get_server_ai_credential(provider)
    except storage.UnreadableAiCredentialsError:
        raise HTTPException(
            status_code=500, detail="Stored server AI credentials are unreadable"
        ) from None
    if key is None:
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' has no stored key"
        )
    # A None generation means the provider was removed between the key read
    # and this read — never bind a capture to "no credential" (#350 r3): an
    # unbound write would re-insert the rows the removal just deleted.
    generation = await storage.get_server_credential_generation(provider)
    if generation is None:
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' has no stored key"
        )
    # Same key, so a failed discovery keeps the last captured listing
    # (#350 r2); the generation binding still discards the refresh when a
    # concurrent rotation moves the credential on.
    models, status = await _capture_key_scoped_models(
        provider,
        key,
        keep_on_failure=True,
        generation=generation,
    )
    logger.info(
        "[AUDIT] Admin '%s' refreshed key-scoped models for '%s' (%s, %d models)",
        request.state.username,
        provider,
        status,
        len(models),
    )
    return JSONResponse(
        content=strip_sensitive_from_response(
            {"ok": True, "status": status, "model_count": len(models)}
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.put(
    "/api/admin/server-providers/{provider:path}/models",
    operation_id="setServerProviderModelsEndpoint",
)
async def set_server_provider_models_endpoint(
    provider: str, request: Request, body: SetServerProviderModelsRequest
) -> JSONResponse:
    """Replace one provider's selected models and free pairs (admin only).

    ``free ⊆ included`` is normalized here: marking a model free auto-includes
    it, unchecking include clears the free flag. New pairs are validated
    against the current sidecar catalog (provider case-insensitively, model
    id exactly) like the standalone free-models save; stored selections and
    free pairs pass through unvalidated so stale survivors never block an
    unrelated edit.
    """
    _require_admin(request)
    # Last entry per model id wins; flags are normalized after the merge.
    merged: dict[str, dict[str, bool]] = {}
    for entry in body.models:
        merged[entry.id] = {"included": entry.included, "free": entry.free}
    entries = [
        {
            "id": model_id,
            "included": flags["included"] or flags["free"],
            "free": flags["free"] and (flags["included"] or flags["free"]),
        }
        for model_id, flags in merged.items()
    ]

    existing = {
        row["model"]
        for row in await storage.list_server_provider_models()
        if row["provider"].lower() == provider.lower()
    } | {
        row["model"]
        for row in await storage.list_free_ai_models()
        if row["provider"].lower() == provider.lower()
    }
    catalog_pairs = {
        (
            str(entry.get("provider") or "").lower(),
            str(entry.get("id") or ""),
        )
        for entry in await _get_model_catalog()
    }
    # Key-scoped pairs (#350): a keyed provider's captured listing is a
    # valid selection target even for models the static catalog lacks.
    key_scoped_pairs = {
        (row["provider"].lower(), row["model"])
        for row in await storage.list_server_provider_key_models()
    }
    unknown = [
        {"provider": provider, "model": e["id"]}
        for e in entries
        if e["id"] not in existing
        and (provider.lower(), e["id"]) not in catalog_pairs
        and (provider.lower(), e["id"]) not in key_scoped_pairs
    ]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Unknown models in server provider selection",
                "unknown": unknown,
            },
        )
    await storage.set_server_provider_models(
        provider, entries, request.state.username or "admin"
    )
    logger.info(
        "[AUDIT] Admin '%s' set server provider models for '%s': %d included, %d free",
        request.state.username,
        provider,
        sum(1 for e in entries if e["included"]),
        sum(1 for e in entries if e["free"]),
    )
    return JSONResponse(
        content=strip_sensitive_from_response(
            {"provider": provider, "models": entries}
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/admin/server-providers/{provider:path}",
    operation_id="deleteServerProviderEndpoint",
)
async def delete_server_provider_endpoint(
    provider: str, request: Request
) -> JSONResponse:
    """Remove a server provider's key, selection, and free pairs (admin only).

    Returns 200 with a warning payload listing the affected selected/free
    models — the frontend renders the pre-delete confirm from the GET list,
    and the response records what the deletion actually removed.
    """
    _require_admin(request)
    has_key = provider.lower() in {
        row["provider"].lower() for row in await storage.list_server_ai_providers()
    }
    current = await storage.list_server_provider_models()
    has_selection = any(row["provider"].lower() == provider.lower() for row in current)
    # Free pairs alone configure a provider too (#347 r4): deletion removes
    # them, so the existence check must see them or a free-pairs-only
    # provider 404s while its free pairs survive untouched.
    has_free = any(
        row["provider"].lower() == provider.lower()
        for row in await storage.list_free_ai_models()
    )
    if not has_key and not has_selection and not has_free:
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' is not configured"
        )
    affected = await storage.delete_server_ai_provider(provider)
    logger.info(
        "[AUDIT] Admin '%s' deleted server provider '%s' (selected=%d, free=%d)",
        request.state.username,
        provider,
        len(affected["selected_models"]),
        len(affected["free_models"]),
    )
    return JSONResponse(
        content=strip_sensitive_from_response({"ok": True, "warnings": affected}),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/admin/users", operation_id="listUsersEndpoint")
async def list_users_endpoint(request: Request) -> dict[str, Any]:
    """List all users (admin and regular)."""
    _require_admin(request)
    users = await storage.list_users()
    return {"users": users}


@app.get("/api/admin/users/pending", operation_id="listPendingUsersEndpoint")
async def list_pending_users_endpoint(request: Request) -> dict[str, Any]:
    """List users awaiting approval."""
    _require_admin(request)
    users = await storage.list_pending_users()
    return {"users": users}


@app.post("/api/admin/users/{username}/approve", operation_id="approveUser")
async def approve_user(
    username: str, request: Request, body: SetCanUseServerProvidersRequest | None = None
) -> dict[str, Any]:
    """Approve a pending user registration."""
    _require_admin(request)
    grant = body.can_use_server_providers if body else None
    if not await storage.approve_pending_user(username, grant):
        status = await storage.get_user_status(username)
        if status is None:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")
        raise HTTPException(
            status_code=400,
            detail=f"User '{username}' is not pending (current status: {status})",
        )
    effective_grant = await storage.can_user_use_server_providers(username)
    notify_pending_count_changed()
    logger.info(
        "[AUDIT] Admin '%s' approved user '%s' (can_use_server_providers=%s)",
        request.state.username,
        username,
        effective_grant,
    )
    return {
        "username": username,
        "status": "active",
        "can_use_server_providers": effective_grant,
        "message": f"User '{username}' has been approved.",
    }


@app.post("/api/admin/users/{username}/reject", operation_id="rejectUser")
async def reject_user(username: str, request: Request) -> dict[str, Any]:
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
    notify_pending_count_changed()
    logger.info(f"[AUDIT] Admin '{request.state.username}' rejected user '{username}'")
    return {
        "username": username,
        "status": "rejected",
        "message": f"User '{username}' has been rejected.",
    }


@app.post("/api/admin/users/{username}/rotate-key", operation_id="rotateKeyEndpoint")
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
        headers={"Cache-Control": "no-store"},
    )


# --- SSE for settings changes ---
_settings_listeners: set[asyncio.Event] = set()


def _broadcast_settings_change() -> None:
    """Signal all SSE listeners that settings changed."""
    for ev in _settings_listeners:
        ev.set()


@app.get("/api/admin/settings/stream", operation_id="settingsStream")
async def settings_stream(request: Request) -> StreamingResponse:
    """SSE stream for server settings changes."""
    _require_admin(request)
    return _make_sse_stream(request, _settings_listeners, "settings-changed")


@app.get("/api/admin/settings", operation_id="getAdminSettings")
async def get_admin_settings(
    request: Request,
    reveal_key: str = Query(
        "", description="Setting key to reveal (or '__all__' for all)"
    ),
) -> JSONResponse:
    """Get all server settings with metadata, current values, and sources."""
    _require_admin(request)
    metadata = _get_settings_metadata()

    # The admin picker for server providers lists what the sidecar can serve.
    for item in metadata:
        if item["key"] == "active_server_providers":
            from rootcoz.ai_client import server_capable_providers

            item["options"] = await server_capable_providers()

    # Internal jira_verified_method row on the jira_auth_mode item (last
    # method whose test SUCCEEDED, "" when never verified; recorded only
    # for successful tests of a SELECTED method; not user-editable).
    db_overrides = await storage.get_server_settings()
    verified_method = db_overrides.get("jira_verified_method", {}).get("value", "")
    for item in metadata:
        if item["key"] == "jira_auth_mode":
            item["jira_verified_method"] = verified_method

    # Load the dotenv source once for the source calculation below.
    dotenv = _settings_dotenv_values()

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
                except (RuntimeError, TypeError, ValueError, UnicodeError) as exc:
                    logger.debug(
                        "Failed to decrypt settings value for %s: %s", key, exc
                    )
            item["value"] = db_value
            item["updated_by"] = override.get("updated_by", "")
            item["updated_at"] = override.get("updated_at", "")
            # Flag if an OS env var also exists (from configmap/container),
            # distinct from DB-injected env vars loaded at startup.
            # Since DB values are NOT written to os.environ, checking
            # os.environ directly tells us if an OS env var also exists.
            env_val = os.environ.get(item["env_var"])
            if env_val is not None:
                item["has_env_var"] = True
                if not item["sensitive"]:
                    # Strip userinfo from URL-like values for safety
                    item["env_var_value"] = (
                        _strip_url_userinfo(env_val) if "://" in env_val else env_val
                    )
        elif _settings_env_value(key, dotenv) is not None:
            # Env-sourced means resolvable from the same sources Settings
            # merges (dotenv included) — those values are persistable, so
            # the UI must offer Save to DB for them, not label them default.
            item["source"] = "env"
        else:
            item["source"] = "default"

    # Mask sensitive values — only reveal the specifically requested key
    reveal_all = reveal_key == "__all__"
    for item in metadata:
        if (
            item["sensitive"]
            and item["value"]
            and not reveal_all
            and item["key"] != reveal_key
        ):
            item["value"] = "••••••••"

    return JSONResponse(content=metadata)


# Valid log levels shared by the PUT allowlist and the env-persist path
# (#347): Settings.log_level itself is unconstrained, so both write paths
# must enforce the same set.
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _log_level_error(value: object) -> str | None:
    """Validate a log level; return the error message, or None when valid."""
    if str(value).strip().upper() not in _VALID_LOG_LEVELS:
        return "log_level: must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL"
    return None


def _settings_dotenv_values() -> dict[str, str]:
    """The dotenv values Settings merges (DotEnvSettingsSource), by field.

    Unreadable dotenv sources resolve to an empty mapping — Settings would
    merge nothing from them either.
    """
    from pydantic_settings import DotEnvSettingsSource

    try:
        return DotEnvSettingsSource(
            Settings,
            env_file=Settings.model_config.get("env_file"),
            env_file_encoding=Settings.model_config.get("env_file_encoding"),
        )()
    except OSError:
        logger.debug("Unable to read dotenv sources", exc_info=True)
        return {}


def _settings_env_value(key: str, dotenv: dict[str, str] | None = None) -> str | None:
    """Resolve a setting's environment value the same way Settings does.

    os.environ first, then the configured dotenv file — the sources
    pydantic-settings merges for ``Settings``. Pass ``dotenv`` to reuse an
    already-loaded mapping (e.g. once per request); ``None`` loads it on
    demand. DB overrides are deliberately excluded: when the effective
    value only exists in the DB store there is no env value to persist, and
    the caller must report no-env-value rather than re-persisting the DB's
    own value.
    """
    env_var = key.upper()
    if key == "jira_auth_mode":
        # DB-only setting (#346): no env var ever maps to it — a stray
        # JIRA_AUTH_MODE in the environment is ignored, matching the
        # pydantic-settings source that drops it.
        return None
    if os.environ.get(env_var) is not None:
        return os.environ[env_var]
    return (dotenv if dotenv is not None else _settings_dotenv_values()).get(key)


def _validate_settings_updates(settings_updates: dict[str, Any]) -> list[str]:
    """Validate admin settings updates; return per-entry error messages.

    Shared by the PUT update and the settings import endpoints so every
    write path applies the same rules. Mutates *settings_updates* the same
    way the PUT path always has: fields with model-level validators are
    normalized in place.
    """
    errors: list[str] = []

    # Validate keys exist in Settings model
    valid_keys = set(Settings.model_fields.keys())
    invalid_keys = set(settings_updates.keys()) - valid_keys
    if invalid_keys:
        errors.append(f"Unknown settings: {', '.join(sorted(invalid_keys))}")

    # Block writes to server-only settings — these are env-only toggles
    server_only_keys = set(settings_updates.keys()) & _SERVER_ONLY_SETTINGS
    if server_only_keys:
        errors.append(
            f"Server-only settings (env var only): "
            f"{', '.join(sorted(server_only_keys))}"
        )

    # Pre-validate fields with Pydantic model-level validators
    for key in ("prow_url", "gcs_bucket", "jira_auth_mode", "default_user_role"):
        if key not in settings_updates:
            continue
        value = settings_updates[key]
        if value is None or value == "":
            continue
        try:
            validated = Settings.model_validate({key: value})
            settings_updates[key] = getattr(validated, key)
        except ValidationError as exc:
            for err in exc.errors():
                ctx_err = err.get("ctx", {}).get("error")
                errors.append(str(ctx_err) if ctx_err else err.get("msg", str(err)))

    # An account-ID-shaped cloud ID is rejected with a direct 422 — the
    # same status and clear message the per-request model validator
    # produces, on every write path (PUT and settings import) that shares
    # this function.
    cloud_id = settings_updates.get("atlassian_oauth_cloud_id")
    if cloud_id and (cloud_id_error := atlassian_cloud_id_error(str(cloud_id))):
        raise HTTPException(status_code=422, detail=cloud_id_error)

    for key, value in settings_updates.items():
        if key not in valid_keys:
            continue
        field_info = Settings.model_fields[key]
        # Skip validation for None/empty — those reset to default
        if value is None or value == "":
            continue
        if key == "log_level" and (message := _log_level_error(value)):
            errors.append(message)
        if key in ("prow_url", "gcs_bucket", "default_user_role"):
            continue
        # Sidecar listener overrides persist under /data and every restart
        # re-probes them — reject values that would brick container boot
        # (#347): a non-numeric/out-of-range port, or a bind address the
        # loopback health probe can never reach.
        if key == "sidecar_port":
            try:
                port = int(str(value).strip())
            except ValueError, TypeError:
                port = 0
            if not 1 <= port <= 65535:
                errors.append("sidecar_port: must be an integer between 1 and 65535")
        if key == "sidecar_host":
            host = str(value).strip()
            if host and not _sidecar_host_supports_loopback_probe(host):
                errors.append(
                    f"sidecar_host: '{host}' would break the container loopback "
                    "health probe (startup would fail on every restart) — use "
                    "127.0.0.1, 0.0.0.0, or leave it empty"
                )
            # Save the stripped host — the env bridge exports the stored
            # value verbatim, so padding would reach pi-config's listen host.
            settings_updates[key] = host
        # Integer fields get explicit bounds first — some clamp in
        # mode="before" validators (session_ttl_hours), so model_validate
        # alone would accept out-of-range values. When this check already
        # flagged the key, model_validate is skipped: a violation must
        # produce exactly one message, not the manual one plus pydantic's
        # duplicate (#361 Qodo r2).
        annotation = field_info.annotation
        is_int = annotation is int or (
            hasattr(annotation, "__args__")
            and int in getattr(annotation, "__args__", ())
        )
        int_error = False
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
                        int_error = True
                    if (
                        hasattr(meta, "ge")
                        and meta.ge is not None
                        and int_val < meta.ge
                    ):
                        errors.append(f"{key}: must be >= {meta.ge}")
                        int_error = True
                    if (
                        hasattr(meta, "le")
                        and meta.le is not None
                        and int_val > meta.le
                    ):
                        errors.append(f"{key}: must be <= {meta.le}")
                        int_error = True
            except ValueError, TypeError:
                errors.append(f"{key}: must be an integer")
                int_error = True
        if int_error:
            continue
        # Type-check every remaining value against its Settings field — an
        # invalid value (e.g. "not-a-bool" for a bool field) would otherwise
        # be persisted, and once get_settings() cannot validate the merged
        # overrides it silently falls back to the base env settings.
        try:
            Settings.model_validate({key: value})
        except ValidationError as exc:
            for err in exc.errors():
                ctx_err = err.get("ctx", {}).get("error")
                errors.append(
                    f"{key}: {ctx_err if ctx_err else err.get('msg', str(err))}"
                )
    return errors


@app.put("/api/admin/settings", operation_id="updateAdminSettings")
async def update_admin_settings(request: Request) -> JSONResponse:
    """Update one or more server settings. Body: {"settings": {"key": "value", ...}}"""
    _require_admin(request)
    body = await request.json()
    settings_updates = body.get("settings", {})

    if not settings_updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    errors = _validate_settings_updates(settings_updates)
    if errors:
        raise HTTPException(
            status_code=400, detail=f"Invalid settings: {'; '.join(errors)}"
        )

    username = request.state.username or "admin"
    warnings = await _save_settings_to_db(settings_updates, username)

    logger.info(
        "Admin settings updated: keys=%s, by=%s",
        list(settings_updates.keys()),
        username,
    )

    content: dict[str, Any] = {"updated": list(settings_updates.keys())}
    if warnings:
        content["warnings"] = warnings
    return JSONResponse(
        content=content,
        status_code=200,
    )


# Schema version of the settings export document — bumped on breaking
# changes to the export shape so imports can reject mismatched documents.
_SETTINGS_EXPORT_VERSION = 1

# Warning shown when an export includes decrypted sensitive values.
_SETTINGS_EXPORT_SECRETS_WARNING = (
    "This export contains decrypted secrets in plaintext — store and "
    "transfer it securely and delete it once imported."
)


def _transform_additional_repos(
    raw: str, url_fn: Callable[[str], str], token_suffix: str
) -> str | None:
    """Rebuild an ``additional_repos`` value, transforming each repo URL.

    ``url_fn`` is applied to every URL (strip or mask credentials). An
    embedded ``@token`` is replaced by ``token_suffix`` — empty drops the
    credential entirely, ``"***"`` masks it for display. Returns None for
    a malformed stored value: its credentials could sit anywhere a parser
    cannot find, so callers must skip (export) or mask it whole (diff).
    """
    try:
        repos = parse_additional_repos(raw)
    except ValueError:
        return None
    parts = []
    for repo in repos:
        part = f"{repo['name']}:{url_fn(repo['url'])}"
        if repo.get("ref"):
            part += f":{repo['ref']}"
        if repo.get("token") and token_suffix:
            part += f"@{token_suffix}"
        parts.append(part)
    return ",".join(parts)


def _sanitize_export_value(key: str, value: str) -> str | None:
    """Strip credentials from a non-sensitive value on default exports.

    ``additional_repos`` entries can carry an embedded ``@token`` and repo
    URLs can carry userinfo or a query/fragment token — those must not
    leave without the ``include_sensitive`` opt-in. Returns None when the
    stored value is too malformed to sanitize (fail closed: the key is
    skipped).
    """
    if key == "additional_repos":
        return _transform_additional_repos(value, strip_url_credentials, "")
    return redact_urls_in_text(value)


def _mask_diff_value(key: str, value: str | None) -> str | None:
    """Mask credential substrings (URL userinfo, repo tokens) in diff display."""
    if value is None:
        return None
    if key == "additional_repos":
        masked = _transform_additional_repos(value, mask_url_credentials_in_text, "***")
        # A malformed value is masked whole — its credentials could sit
        # anywhere a parser cannot find (#361 Qodo r2).
        return "***" if masked is None else masked
    return mask_url_credentials_in_text(value)


def _diff_import_updates(
    updates: dict[str, str], db_settings: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Diff import values against current DB overrides.

    Returns ``(applied, unchanged, restart_required)`` — sensitive values
    are masked as ``"***"`` and never appear in the diff.
    """
    applied: dict[str, dict[str, Any]] = {}
    unchanged: list[str] = []
    restart_required: list[str] = []
    for key, value in updates.items():
        old = db_settings[key]["value"] if key in db_settings else None
        if old and key in _SENSITIVE_SETTINGS:
            # Compare against decrypted plaintext; an undecryptable value
            # (key rotation) counts as changed so the import overwrites it.
            old = decrypt_value_or_none(old)
        if old == value or (
            old is not None
            and key not in _SENSITIVE_SETTINGS
            # A default (sanitized) export drops credentials; importing it
            # must not wipe them from the target — when the document carries
            # nothing new for a key, the target's credential-bearing value
            # wins (#361 Qodo r2).
            and value == _sanitize_export_value(key, old)
        ):
            unchanged.append(key)
            continue
        masked = key in _SENSITIVE_SETTINGS
        applied[key] = {
            "from": "***" if masked else _mask_diff_value(key, old),
            "to": "***" if masked else _mask_diff_value(key, value),
        }
        if key in _RESTART_REQUIRED_SETTINGS:
            restart_required.append(key)
    return applied, unchanged, restart_required


@app.get("/api/admin/settings/export", operation_id="exportServerSettings")
async def export_server_settings(
    request: Request,
    include_sensitive: bool = Query(
        False, description="Include decrypted sensitive settings in the export"
    ),
) -> JSONResponse:
    """Export all DB-stored server settings as a JSON document.

    Only database overrides are exported — env-only values are deployment
    config, not server settings. Sensitive settings are excluded unless
    ``include_sensitive`` is set; when included they are decrypted plaintext
    (re-encrypted values would only be usable with the same
    ``ROOTCOZ_ENCRYPTION_KEY``), and the response carries a warning.
    """
    _require_admin(request)

    settings: dict[str, str] = {}
    sensitive_included = False
    for key, row in sorted((await storage.get_server_settings()).items()):
        # Internal bookkeeping rows (e.g. jira_verified_method), server-only
        # bootstrap values, and derived settings are not transferable
        # settings — a derived value (e.g. sidecar_url from sidecar_port) is
        # instance-computed, and shipping it would pin a stale URL on
        # another instance (same rule the persist endpoint enforces).
        if (
            key not in _FIELD_TO_CATEGORY
            or key in _SERVER_ONLY_SETTINGS
            or key in _DERIVED_SETTINGS
        ):
            continue
        value = row["value"]
        if key in _SENSITIVE_SETTINGS:
            if not include_sensitive:
                continue
            if value == "":
                # An explicit empty override is meaningful (it deliberately
                # beats the env value) — export it as-is; there is nothing
                # to decrypt.
                settings[key] = ""
                continue
            sensitive_included = True
            decrypted = decrypt_value_or_none(value)
            if decrypted is None:
                # Undecryptable ciphertext (key rotation) is useless in an
                # export — skip rather than ship garbage.
                logger.debug("Cannot decrypt setting %s for export; skipping", key)
                continue
            value = decrypted
        elif not include_sensitive:
            # Default exports must not leak embedded credentials — repo
            # tokens and URL userinfo leave only with the opt-in. A value
            # too malformed to sanitize is skipped, never shipped raw.
            sanitized = _sanitize_export_value(key, value)
            if sanitized is None:
                logger.warning(
                    "Skipping setting %s from default export: unparseable stored value",
                    key,
                )
                continue
            value = sanitized
        settings[key] = value

    content: dict[str, Any] = {
        "version": _SETTINGS_EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "settings": settings,
    }
    if sensitive_included:
        content["warning"] = _SETTINGS_EXPORT_SECRETS_WARNING

    logger.info(
        "Admin settings exported: keys=%d, include_sensitive=%s, by=%s",
        len(settings),
        include_sensitive,
        request.state.username,
    )
    return JSONResponse(content=content, headers={"Cache-Control": "no-store"})


@app.post("/api/admin/settings/import", operation_id="importServerSettings")
async def import_server_settings(request: Request) -> JSONResponse:
    """Import a settings export document (validated, atomic, with a diff).

    Body: an export document, optionally with ``"dry_run": true`` to
    validate and report the diff without applying anything. Every key and
    value is validated before anything is written — one invalid entry
    rejects the whole import (422 with the offending entries listed).
    """
    _require_admin(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Body must be a JSON settings export document"
        )
    if not isinstance(body, dict) or body.get("version") != _SETTINGS_EXPORT_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unsupported export document: expected version "
                f"{_SETTINGS_EXPORT_VERSION}"
            ),
        )
    raw_settings = body.get("settings")
    if not isinstance(raw_settings, dict) or not raw_settings:
        raise HTTPException(status_code=422, detail="No settings in export document")
    updates = {str(k): "" if v is None else str(v) for k, v in raw_settings.items()}

    # Validate the whole batch before anything is applied — the same
    # rules the per-setting PUT enforces, plus the empty-override gates
    # the shared save path applies (checked here so a rejection is a 422
    # before any DB write, keeping the import atomic).
    errors = _validate_settings_updates(updates)
    for key, value in updates.items():
        if key in _DERIVED_SETTINGS:
            # Same rule the persist endpoint enforces: a derived value is
            # computed from sidecar_port at boot — importing one would pin
            # a stale URL from the exporting instance.
            errors.append(
                f"{key} is derived from sidecar_port — set sidecar_port instead"
            )
        if not value and (error := _empty_override_error(key)):
            errors.append(error)
    if errors:
        raise HTTPException(
            status_code=422, detail=f"Invalid settings: {'; '.join(errors)}"
        )

    applied, unchanged, restart_required = _diff_import_updates(
        updates, await storage.get_server_settings()
    )
    content: dict[str, Any] = {
        "applied": applied,
        "unchanged": unchanged,
        "restart_required": restart_required,
    }
    if body.get("dry_run"):
        content["dry_run"] = True
        return JSONResponse(content=content)

    username = request.state.username or "admin"
    # Only keys the diff marked as applied are written — unchanged keys
    # (including sanitized-equal ones whose credentials the target keeps)
    # must not be overwritten (#361 Qodo r2).
    warnings = await _save_settings_to_db(
        {key: value for key, value in updates.items() if key in applied}, username
    )
    if warnings:
        content["warnings"] = warnings
    logger.info(
        "Admin settings imported: applied=%d, unchanged=%d, by=%s",
        len(applied),
        len(unchanged),
        username,
    )
    return JSONResponse(content=content)


# Settings where an explicit empty override is unsafe regardless of type:
# the sidecar env bridge would export SIDECAR_HOST=''/SIDECAR_PORT='' and
# pi-config's nullish coalescing selects the empty string over its loopback/
# default fallbacks — an empty host binds an unspecified address (exposure)
# and an empty port breaks the entrypoint's startup probe (#352 Qodo r1).
_EMPTY_OVERRIDE_FORBIDDEN: frozenset[str] = frozenset({"sidecar_host", "sidecar_port"})


def _empty_override_error(key: str) -> str | None:
    """User-facing error when an explicit empty override is invalid for key.

    Returns None when saving an empty string is a valid override. Three
    gates, all of which protect ``get_settings()`` from storing a value
    that fails pydantic validation (which would silently drop the entire
    DB override set) or that would break the sidecar bridge (#352 Qodo r1):
    string-typed fields only (str / SecretStr), field validators must
    accept the empty value (``default_user_role`` is str-typed but its
    validator rejects ""), and infrastructure settings with unsafe empty
    semantics are denied outright.
    """
    if key not in Settings.model_fields:
        # Unknown keys are already reported by the shared validator —
        # indexing model_fields here would turn a 422 into a 500.
        return None
    if key in _EMPTY_OVERRIDE_FORBIDDEN:
        return (
            f"Setting '{key}' cannot be empty — the sidecar bridge would "
            "export a blank value. Use Reset to fall back to the "
            "env/default value."
        )
    annotation = Settings.model_fields[key].annotation
    types = get_args(annotation) or (annotation,)
    if not all(t is str or t is SecretStr or t is type(None) for t in types):
        return (
            f"Setting '{key}' cannot be empty — its type does not accept "
            "an empty override. Use Reset to fall back to the env/default "
            "value."
        )
    try:
        Settings.model_validate({key: ""})
    except ValidationError:
        return (
            f"Setting '{key}' cannot be empty — its validator rejects an "
            "empty value. Use Reset to fall back to the env/default value."
        )
    return None


async def _save_settings_to_db(
    settings_updates: dict[str, Any], username: str
) -> list[str]:
    """Persist validated settings updates to the DB store and apply them.

    Shared save path for the PUT update and the env→DB persist endpoints.
    A saved value — empty or not — is an explicit DB override: the row's
    presence beats the OS env (request → DB → env). To fall back to the
    env/default value, use the reset endpoint (DELETE) instead of saving
    an empty value (#352). Invalid empty overrides are rejected for ALL
    keys before ANY key is written — a rejected save never applies a
    partial update. Returns user-facing warnings; values are never logged.
    """
    # Pre-flight: reject invalid empty overrides for the whole batch
    # before the first DB write, so a 400 never leaves a partial save.
    for key, value in settings_updates.items():
        str_value = str(value) if value is not None else ""
        if not str_value:
            error = _empty_override_error(key)
            if error:
                raise HTTPException(status_code=400, detail=error)

    db_values: dict[str, str] = {}
    for key, value in settings_updates.items():
        str_value = str(value) if value is not None else ""
        # Upsert (empty allowed — an explicit empty override beats env).
        # Sensitive values are encrypted only when non-empty; the empty
        # override stays a plain empty string (nothing to protect).
        if str_value and key in _SENSITIVE_SETTINGS:
            from rootcoz.encryption import encrypt_value

            db_values[key] = encrypt_value(str_value)
        else:
            db_values[key] = str_value
    # One atomic batch write — a mid-batch failure must not leave earlier
    # keys applied while later ones are missing (half-applied import).
    await storage.set_server_settings_batch(db_values, updated_by=username)

    # Changed Jira credentials or auth mode invalidate the verified-method
    # record — it reflects the last successful test of the previous setup.
    if settings_updates.keys() & _JIRA_VERIFIED_INVALIDATING_KEYS:
        try:
            await storage.delete_server_setting(
                "jira_verified_method", deleted_by=username
            )
            remove_from_db_settings_cache(["jira_verified_method"])
        except Exception:
            logger.warning("Failed to clear jira_verified_method", exc_info=True)

    # Update in-memory cache so get_settings() picks up changes. Saved
    # values — empty included — are explicit overrides that beat env;
    # falling back to env/default is the reset endpoint's job (DELETE).
    update_db_settings_cache(
        {k: str(v) if v is not None else "" for k, v in settings_updates.items()}
    )

    # Persist the transcript recording switch (and the other sidecar
    # settings) for the container entrypoint — the sidecar reads these from
    # its process env at start, so the bridge file is how a DB toggle
    # reaches it after a restart.
    warnings: list[str] = []
    if "record_transcripts" in settings_updates or any(
        key in settings_updates for key in _SIDECAR_BRIDGE_SETTINGS
    ):
        synced = await _apply_transcript_recording_setting()
        if not synced and "record_transcripts" in settings_updates:
            warnings.append(
                "record_transcripts was saved, but the running sidecar could "
                "not be updated — it applies after a restart or a later "
                "successful sync"
            )
    # A non-loopback sidecar bind is exposed to the network — same trust
    # class as trust_proxy_headers: only safe behind a trusted network.
    if "sidecar_host" in settings_updates:
        host = str(settings_updates.get("sidecar_host") or "").strip()
        if host and not _is_loopback_host(host):
            warnings.append(
                f"sidecar_host '{host}' is not a loopback address — the sidecar "
                "will listen on an external interface. Only do this on a "
                "trusted network (same trust class as trust_proxy_headers)."
            )

    _broadcast_settings_change()
    return warnings


@app.post("/api/admin/settings/{key}/persist", operation_id="persistAdminSetting")
async def persist_admin_setting(request: Request, key: str) -> JSONResponse:
    """Persist an env-sourced setting's current value to the DB store.

    The migration path for env ↔ Server Settings parity (#347): deployments
    save their current env values once, then drop the envs — the setting no
    longer reverts to its default on restart. Runs server-side because the
    UI never sees decrypted sensitive env values.
    """
    _require_admin(request)
    if key not in Settings.model_fields:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
    if key in _SERVER_ONLY_SETTINGS:
        raise HTTPException(
            status_code=400, detail=f"Server-only setting (env var only): {key}"
        )
    if key in _DERIVED_SETTINGS:
        # The entrypoint exports the DERIVED SIDECAR_URL into the process
        # env, so it shows as env-sourced — persisting it would pin an
        # explicit URL that beats the port derivation (#347).
        raise HTTPException(
            status_code=400,
            detail=f"{key} is derived from sidecar_port — persist sidecar_port instead",
        )
    # Env var name per the Settings field mapping (see _get_settings_metadata)
    # — resolved through the same sources Settings reads (dotenv included);
    # a DB-only value correctly reports no env value.
    env_value = _settings_env_value(key)
    if not env_value:
        raise HTTPException(
            status_code=400,
            detail=f"Setting '{key}' has no env value set",
        )
    # log_level has no Settings constraint — apply the shared PUT allowlist
    # so an invalid level cannot survive the persist path either.
    if key == "log_level" and (message := _log_level_error(env_value)) is not None:
        raise HTTPException(
            status_code=400, detail=f"Invalid env value for '{key}': {message}"
        )
    # Same validation the PUT path applies to admin-typed values
    try:
        Settings.model_validate({key: env_value})
    except ValidationError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid env value for '{key}'"
        ) from exc

    username = request.state.username or "admin"
    await _save_settings_to_db({key: env_value}, username)

    logger.info(
        "[AUDIT] Admin setting persisted from env: key=%s, by=%s", key, username
    )
    return JSONResponse(content={"key": key, "source": "db"})


@app.get("/api/admin/settings/history", operation_id="getSettingsHistory")
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


@app.delete("/api/admin/settings/{key}", operation_id="resetAdminSetting")
async def reset_admin_setting(request: Request, key: str) -> JSONResponse:
    """Reset a server setting to its env/default value (removes DB override)."""
    _require_admin(request)
    if key not in Settings.model_fields:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    if key in _SERVER_ONLY_SETTINGS:
        raise HTTPException(
            status_code=400, detail=f"Server-only setting (env var only): {key}"
        )

    deleted = await storage.delete_server_setting(
        key, deleted_by=request.state.username or "admin"
    )
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Setting '{key}' has no DB override"
        )

    # Remove from in-memory cache so get_settings() falls back to env/default
    remove_from_db_settings_cache([key])

    # Keep the sidecar transcript env bridge in sync with the reset — the
    # bridge must stop assigning the removed override so the deployment
    # environment controls the sidecar again, and the runtime override is
    # dropped so the boot env governs immediately.
    content: dict[str, Any] = {"reset": key}
    if key == "record_transcripts" or key in _SIDECAR_BRIDGE_SETTINGS:
        synced = await _apply_transcript_recording_setting()
        if not synced and key == "record_transcripts":
            content["warnings"] = [
                (
                    "record_transcripts was reset, but the running sidecar could "
                    "not be updated — it keeps its current recording state until "
                    "a restart or a later successful sync"
                )
            ]

    _broadcast_settings_change()

    logger.info("Admin setting reset: key=%s, by=%s", key, request.state.username)

    return JSONResponse(content=content)


# -- Integration connection tests --------------------------------------------


class _IntegrationTestError(Exception):
    """A failed integration connection test — message is safe to return."""


#: Short timeout so an admin "Test connection" click never hangs the UI.
_INTEGRATION_TEST_TIMEOUT = 10.0


def _test_result(ok: bool, message: str, **details: Any) -> dict[str, Any]:
    """Build the wire shape of a connection test result."""
    result: dict[str, Any] = {"ok": ok, "message": message}
    if details:
        result["details"] = details
    return result


async def _integration_test_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    verify: bool = True,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """GET a URL for a connection test; never follows redirects into errors.

    Raises :class:`_IntegrationTestError` with a human-readable, secret-free
    message on transport failure — the caller turns that into ``ok: false``.
    """
    try:
        async with httpx.AsyncClient(
            verify=verify,
            timeout=_INTEGRATION_TEST_TIMEOUT,
            auth=auth,
            headers=headers,
            follow_redirects=True,
        ) as client:
            return await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise _IntegrationTestError(
            redact_urls_in_text(f"connection failed ({type(exc).__name__}): {exc}")
        ) from exc


async def _integration_test_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
    """GET a JSON payload for a connection test.

    Raises :class:`_IntegrationTestError` on HTTP >= 400 or non-JSON bodies.
    """
    response = await _integration_test_request(url, **kwargs)
    if response.status_code in (401, 403):
        raise _IntegrationTestError(
            f"authentication failed (HTTP {response.status_code})"
        )
    if response.status_code >= 400:
        raise _IntegrationTestError(
            f"HTTP {response.status_code} from {redact_urls_in_text(url)}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise _IntegrationTestError("response was not valid JSON") from exc


async def _test_jenkins_connection(settings: Settings) -> dict[str, Any]:
    """Authenticated whoAmI ping against the effective Jenkins settings."""
    if not settings.jenkins_url:
        return _test_result(False, "Jenkins URL is not configured")
    try:
        client = JenkinsClient(
            url=settings.jenkins_url,
            username=settings.jenkins_user,
            password=settings.jenkins_password,
            ssl_verify=settings.jenkins_ssl_verify,
            timeout=int(_INTEGRATION_TEST_TIMEOUT),
        )
    except Exception as exc:
        logger.debug("Jenkins client init failed", exc_info=True)
        return _test_result(
            False,
            redact_urls_in_text(
                f"Failed to initialize Jenkins client: {type(exc).__name__}: {exc}"
            ),
        )
    try:
        whoami = await asyncio.to_thread(client.get_whoami_security)
    except Exception as exc:
        logger.debug("Jenkins whoAmI ping failed", exc_info=True)
        if "authentication failed" in str(exc):
            return _test_result(
                False,
                "Jenkins reachable but credentials are invalid "
                f"({exc} — check the configured username/password)",
            )
        return _test_result(
            False, f"Jenkins connection failed: {type(exc).__name__}: {exc}"
        )
    if not isinstance(whoami, dict):
        return _test_result(
            False, "Jenkins connection failed: unexpected whoAmI response"
        )
    user = str(whoami.get("name") or "")
    # SSO Jenkins reports authenticated:true even for anonymous requests, so
    # only the anonymous flag and the reported name are trusted. A missing
    # anonymous flag is treated as anonymous (conservative).
    if whoami.get("anonymous") is not False or user == "anonymous":
        return _test_result(
            False,
            "Jenkins reachable but credentials are invalid "
            "(anonymous access — check the configured username/password)",
            authenticated_user=user,
        )
    message = "Jenkins reachable and credentials valid"
    if not user:
        message += " (server did not report an authenticated user)"
    return _test_result(True, message, authenticated_user=user)


def _jira_settings_fingerprint(settings: Settings) -> str:
    """Cheap fingerprint of the Jira settings a connection test exercises.

    Covers the selected auth mode, that method's URL and TLS-verification
    fields, and its credential fields (the same fields
    ``jira_method_missing_fields`` checks). Values are SHA-256 hashed so
    the fingerprint can be compared but never decoded or logged — no
    secrets in comparisons or logs.
    """
    mode = normalize_jira_auth_mode(settings.jira_auth_mode)

    def _secret(value: SecretStr | None) -> str:
        return value.get_secret_value() if value else ""

    parts = [mode]
    if mode == "oauth":
        parts.append(_secret(settings.jira_oauth_client_secret))
        parts.append((settings.atlassian_oauth_client_id or "").strip())
        parts.append((settings.atlassian_oauth_cloud_id or "").strip())
        parts.append(str(settings.jira_oauth_ssl_verify))
    elif mode == "basic":
        parts.append((settings.jira_email or "").strip())
        parts.append(_secret(settings.jira_api_token))
        parts.append(settings.jira_cloud_url.strip())
        parts.append(str(settings.jira_cloud_ssl_verify))
    elif mode == "pat":
        parts.append(_secret(settings.jira_pat))
        parts.append(settings.jira_server_url.strip())
        parts.append(str(settings.jira_server_ssl_verify))
    elif mode == "atst":
        parts.append(_secret(settings.jira_atst_token))
        parts.append((settings.atlassian_oauth_cloud_id or "").strip())
        parts.append(str(settings.jira_oauth_ssl_verify))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


async def _test_jira_connection(settings: Settings) -> dict[str, Any]:
    """Current-user ping against the effective Jira connection resolution."""
    if not normalize_jira_auth_mode(settings.jira_auth_mode):
        return _test_result(
            False,
            "No Jira auth method selected — choose one in Server Settings",
        )
    missing = jira_method_missing_fields(settings)
    if missing:
        return _test_result(
            False,
            f"Jira auth method '{settings.jira_auth_mode}' is selected "
            f"but missing: {', '.join(missing)}",
        )
    connection = resolve_jira_connection(settings)
    # Snapshot of the Jira settings this test exercises — compared after
    # the network call so a mid-flight admin update cannot leave a
    # verification record for the untested configuration.
    tested_fingerprint = _jira_settings_fingerprint(settings)
    try:
        connection = await complete_jira_connection(connection)
    except JiraOAuthMintError as exc:
        return _test_result(False, f"Jira connection failed: {exc}")
    if not connection.api_base:
        return _test_result(
            False,
            "Jira service account mode could not resolve the Atlassian "
            "cloud ID (set atlassian_oauth_cloud_id or jira_site_url)",
        )
    if connection.mode in ("sa", "atst"):
        # OAuth 2.0 credentials carry read:jira-work only — /myself needs
        # read:jira-users and 401s with "scope does not match" — so probe
        # project visibility instead (any 200 proves auth + gateway access).
        # ATST scoped tokens get the same treatment: their scopes are
        # admin-chosen and must not be assumed to include read:jira-users.
        try:
            data = await _integration_test_get_json(
                f"{connection.api_base}{connection.api_path}/project",
                params={"maxResults": 1},
                headers=dict(connection.headers),
                auth=connection.auth,
                verify=connection.ssl_verify,
            )
        except _IntegrationTestError as exc:
            return _test_result(False, f"Jira connection failed: {exc}")
        if not isinstance(data, list):
            return _test_result(
                False, "Jira connection failed: unexpected /project response"
            )
        user = "service account"
    else:
        try:
            data = await _integration_test_get_json(
                f"{connection.api_base}{connection.api_path}/myself",
                headers=dict(connection.headers),
                auth=connection.auth,
                verify=connection.ssl_verify,
            )
        except _IntegrationTestError as exc:
            return _test_result(False, f"Jira connection failed: {exc}")
        # Validate the payload before recording anything: HTTP 200 with a
        # non-object body is a failed test, not a verified method.
        if not isinstance(data, dict):
            return _test_result(
                False, "Jira connection failed: unexpected /myself response"
            )
        user = str(
            data.get("displayName") or data.get("name") or data.get("accountId") or ""
        )
    auth_mode = {"sa": "oauth", "cloud": "basic", "server": "pat", "atst": "atst"}[
        connection.mode
    ]
    # A method counts as configured only once its test succeeded — record
    # the tested (selected) method as an internal DB row. Not cleared on
    # failure: the record reflects the last success, not the latest attempt.
    # Written only on change so repeated tests don't flood settings history.
    # But not when the Jira settings changed while the test was in flight:
    # update_admin_settings already deleted the record for the previous
    # setup, and this test proved the OLD credentials — re-writing the
    # marker would verify the untested new configuration.
    if _jira_settings_fingerprint(get_settings()) != tested_fingerprint:
        logger.info(
            "Jira settings changed during the connection test — "
            "verified method not recorded"
        )
        return _test_result(
            True,
            "Jira connection successful",
            auth_mode=auth_mode,
            user=user,
            note=(
                "Jira settings changed during the test — verification not "
                "recorded; re-run the test for the current settings"
            ),
        )
    try:
        existing = await storage.get_server_setting("jira_verified_method")
        if existing is None or existing.get("value") != auth_mode:
            await storage.set_server_setting("jira_verified_method", auth_mode)
            logger.info("Jira verified method recorded: %s", auth_mode)
    except Exception:
        logger.warning("Failed to record jira_verified_method", exc_info=True)
        # The confirmation gate must match the reported result — a marker
        # that could not be persisted would leave the UI inconsistent.
        return _test_result(
            False,
            "Jira connection succeeded but verification could not be recorded",
        )
    return _test_result(
        True, "Jira connection successful", auth_mode=auth_mode, user=user
    )


async def _test_github_connection(settings: Settings) -> dict[str, Any]:
    """Current-user ping against api.github.com with the effective token."""
    if not settings.github_token:
        return _test_result(False, "GitHub token is not configured")
    token = settings.github_token.get_secret_value()
    try:
        data = await _integration_test_get_json(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}"},
        )
    except _IntegrationTestError as exc:
        return _test_result(False, f"GitHub connection failed: {exc}")
    return _test_result(
        True, "GitHub connection successful", login=str(data.get("login") or "")
    )


async def _test_reportportal_connection(settings: Settings) -> dict[str, Any]:
    """Project-visibility ping against the effective Report Portal settings."""
    token = settings.reportportal_api_token
    missing = [
        name
        for name, value in (
            ("reportportal_url", settings.reportportal_url),
            ("reportportal_api_token", token),
            ("reportportal_project", settings.reportportal_project),
        )
        if not value
    ]
    if missing:
        return _test_result(
            False, f"Report Portal is not configured (missing: {', '.join(missing)})"
        )
    assert token is not None  # guaranteed by the missing-settings check above
    url = str(settings.reportportal_url).rstrip("/")
    project = str(settings.reportportal_project)
    try:
        await _integration_test_get_json(
            f"{url}/api/v1/project/{project}",
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            verify=settings.reportportal_verify_ssl,
        )
    except _IntegrationTestError as exc:
        return _test_result(False, f"Report Portal connection failed: {exc}")
    return _test_result(
        True, f"Report Portal project '{project}' is visible", project=project
    )


async def _test_datadog_connection(settings: Settings) -> dict[str, Any]:
    """Credential validation ping against the effective Datadog settings.

    Uses the same read-only ``GET /api/v1/validate`` call the Datadog
    events push path relies on — a 200 with ``{"valid": true}`` proves
    both reachability of the configured site and the credential.
    Credentials are classified exactly like the push path (API key vs
    Service Access Token vs read-only application key): application keys
    fail fast with the shared explanatory message instead of a bare 403.
    """
    api_key = settings.datadog_api_key
    missing = [
        name
        for name, value in (
            ("datadog_api_key", api_key and api_key.get_secret_value()),
            ("datadog_site", settings.datadog_site),
        )
        if not value
    ]
    if missing:
        return _test_result(
            False, f"Datadog is not configured (missing: {', '.join(missing)})"
        )
    assert api_key is not None  # guaranteed by the missing-settings check above
    credential = api_key.get_secret_value()
    if classify_datadog_credential(credential) == _CREDENTIAL_APPLICATION_KEY:
        return _test_result(False, APPLICATION_KEY_ERROR)
    site = _normalize_site(settings.datadog_site)
    if not site:
        return _test_result(
            False,
            f"Datadog connection failed: invalid site {settings.datadog_site!r} "
            "— set DATADOG_SITE to a Datadog hostname (e.g. datadoghq.eu)",
        )
    try:
        data = await _integration_test_get_json(
            f"https://api.{site}/api/v1/validate",
            headers=_auth_headers(credential),
        )
    except _IntegrationTestError as exc:
        message = str(exc)
        if "authentication failed" in message:
            message = "API key rejected (invalid or unauthorized)"
        return _test_result(False, f"Datadog connection failed: {message}")
    if not isinstance(data, dict) or data.get("valid") is not True:
        return _test_result(
            False, "Datadog connection failed: API key rejected (invalid key)"
        )
    return _test_result(True, f"Datadog API key is valid (site {site})", site=site)


async def _test_prow_connection(settings: Settings) -> dict[str, Any]:
    """Prow deck reachability plus GCS bucket listability check.

    The analysis path reads artifacts from the GCS bucket via public HTTP,
    so the bucket check is the authoritative one; the deck check is a
    lightweight reachability ping (any HTTP response below 500 counts).
    """
    if not settings.prow_url:
        return _test_result(False, "Prow URL is not configured")
    if not settings.gcs_bucket:
        return _test_result(False, "GCS bucket is not configured")
    prow_url = str(settings.prow_url).rstrip("/")
    try:
        deck_response = await _integration_test_request(f"{prow_url}/api/jobs")
    except _IntegrationTestError as exc:
        return _test_result(False, f"Prow unreachable: {exc}")
    deck_reachable = deck_response.status_code < 500
    try:
        # Same public-HTTP GCS mechanism the Prow source uses; maxResults=1
        # keeps it a visibility probe, not a listing.
        await _integration_test_get_json(
            f"https://storage.googleapis.com/storage/v1/b/{settings.gcs_bucket}/o",
            params={"maxResults": 1},
        )
    except _IntegrationTestError as exc:
        return _test_result(
            False,
            f"GCS bucket '{settings.gcs_bucket}' is not listable: {exc}",
            prow_http_status=deck_response.status_code,
        )
    message = f"GCS bucket '{settings.gcs_bucket}' is listable"
    if deck_reachable:
        message += f" and Prow is reachable (HTTP {deck_response.status_code})"
    else:
        message += (
            f"; Prow deck returned HTTP {deck_response.status_code} but the "
            "GCS artifact path (used for analysis) works"
        )
    return _test_result(True, message, prow_http_status=deck_response.status_code)


_INTEGRATION_TESTERS: dict[str, Callable[[Settings], Awaitable[dict[str, Any]]]] = {
    "jenkins": _test_jenkins_connection,
    "jira": _test_jira_connection,
    "github": _test_github_connection,
    "reportportal": _test_reportportal_connection,
    "datadog": _test_datadog_connection,
    "prow": _test_prow_connection,
}


@app.post(
    "/api/admin/settings/test/{integration}", operation_id="testIntegrationConnection"
)
async def test_integration_connection(
    request: Request, integration: str
) -> JSONResponse:
    """Test connectivity and credentials for an external integration.

    Always returns HTTP 200 with ``{ok, message, details?}`` — a failed
    connection is a test result, not a transport error. Tests run against
    the current effective settings (DB overrides applied).
    """
    _require_admin(request)
    key = integration.lower().strip()
    tester = _INTEGRATION_TESTERS.get(key)
    if tester is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown integration: {integration}"
        )
    settings = get_settings()
    try:
        # Overall deadline: testers may make several sequential requests
        # (Prow deck + GCS, Jira cloud-ID resolution), each with its own
        # timeout — one click must never consume several timeout periods.
        result = await asyncio.wait_for(
            tester(settings), timeout=_INTEGRATION_TEST_TIMEOUT
        )
    except TimeoutError:
        result = _test_result(
            False,
            f"Connection test timed out after {_INTEGRATION_TEST_TIMEOUT:.0f} seconds",
        )
    except Exception as exc:  # noqa: BLE001 - a broken integration must never 500
        message = redact_urls_in_text(
            f"Connection test failed unexpectedly: {type(exc).__name__}: {exc}"
        )
        logger.warning(
            "Integration connection test crashed for %s: %s: %s",
            key,
            type(exc).__name__,
            message,
        )
        result = _test_result(False, message)
    logger.info(
        "Integration connection test: integration=%s ok=%s by=%s",
        key,
        result["ok"],
        request.state.username,
    )
    return JSONResponse(content=result)


# -- Job Metadata Endpoints ---------------------------------------------------


async def _metadata_filters(
    team: Annotated[list[str] | None, Query()] = None,
    tier: Annotated[list[str] | None, Query()] = None,
    version: Annotated[list[str] | None, Query()] = None,
    label: Annotated[list[str] | None, Query()] = None,
    exclude_label: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    """Shared dependency for metadata filter query parameters."""
    return {
        "team": team or [],
        "tier": tier or [],
        "version": version or [],
        "label": label or [],
        "exclude_label": exclude_label or [],
    }


def _unpack_metadata_filters(
    filters: dict[str, Any], endpoint: str
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


@app.get("/api/jobs/metadata", operation_id="listJobsMetadata")
async def list_jobs_metadata(
    filters: Annotated[dict[str, Any], Depends(_metadata_filters)],
) -> list[dict[str, Any]]:
    """List all job metadata, optionally filtered by team, tier, version, or labels."""
    team, tier, version, label, _exclude_label = _unpack_metadata_filters(
        filters, "GET /api/jobs/metadata"
    )
    return await storage.list_jobs_with_metadata(
        team=team, tier=tier, version=version, labels=label or None
    )


@app.get("/api/jobs/{job_name:path}/metadata", operation_id="getJobMetadataEndpoint")
async def get_job_metadata_endpoint(job_name: str) -> dict[str, Any]:
    """Get metadata for a specific job."""
    logger.debug(f"GET /api/jobs/{job_name}/metadata")
    result = await storage.get_job_metadata(job_name)
    if not result:
        raise HTTPException(status_code=404, detail=f"No metadata for job '{job_name}'")
    return result


@app.put("/api/jobs/{job_name:path}/metadata", operation_id="setJobMetadataEndpoint")
async def set_job_metadata_endpoint(
    request: Request,
    job_name: str,
    body: JobMetadataInput,
) -> dict[str, Any]:
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


@app.delete(
    "/api/jobs/{job_name:path}/metadata", operation_id="deleteJobMetadataEndpoint"
)
async def delete_job_metadata_endpoint(
    request: Request, job_name: str
) -> dict[str, Any]:
    """Delete metadata for a job."""
    _require_admin(request)
    logger.debug(f"DELETE /api/jobs/{job_name}/metadata")
    deleted = await storage.delete_job_metadata(job_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No metadata for job '{job_name}'")
    return {"status": "deleted", "job_name": job_name}


@app.put("/api/jobs/metadata/bulk", operation_id="bulkSetJobMetadata")
async def bulk_set_job_metadata(
    request: Request,
    body: BulkJobMetadataRequest,
) -> dict[str, Any]:
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


@app.get("/api/jobs/metadata/rules", operation_id="listMetadataRules")
async def list_metadata_rules() -> dict[str, Any]:
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


@app.post("/api/jobs/metadata/rules/preview", operation_id="previewMetadataRules")
async def preview_metadata_rules(body: dict[str, Any]) -> dict[str, Any]:
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


@app.get("/api/dashboard/filtered", operation_id="apiDashboardFiltered")
async def api_dashboard_filtered(
    filters: Annotated[dict[str, Any], Depends(_metadata_filters)],
    search: str = Query(default="", description="Search job name or ID"),
    status: Annotated[list[str] | None, Query()] = None,
    date_from: str = Query(default="", description="Start date (ISO)"),
    date_to: str = Query(default="", description="End date (ISO)"),
    review_status: str = Query(
        default="all", description="Filter: all, reviewed, not_reviewed"
    ),
    analysis_state: str = Query(
        default="",
        description="Filter: submitted, analyzed (empty = all)",
    ),
    limit: int = Query(default=500, ge=0, description="Max results (0 = no limit)"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
) -> dict[str, Any]:
    """Return dashboard job list filtered by metadata.

    Filters are applied in SQL before LIMIT, so older jobs are reachable
    when filters match them. Returns ``{jobs, total}`` for pagination.
    """
    if review_status not in ("all", "reviewed", "not_reviewed"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid review_status '{review_status}'. Must be one of: all, not_reviewed, reviewed",
        )

    team, tier, version, label, exclude_label = _unpack_metadata_filters(
        filters, "GET /api/dashboard/filtered"
    )

    has_include_filters = bool(team or tier or version or label)

    # Resolve metadata filters to job name sets
    job_names: set[str] | None = None
    exclude_job_names: set[str] | None = None

    # Fetch all metadata once (unfiltered) — used for both include/exclude and display
    all_metadata_unfiltered = await storage.list_jobs_with_metadata()
    all_metadata_by_name = {m["job_name"]: m for m in all_metadata_unfiltered}

    if has_include_filters:
        filtered_metadata = await storage.list_jobs_with_metadata(
            team=team,
            tier=tier,
            version=version,
            labels=label if label else None,
        )
        job_names = {m["job_name"] for m in filtered_metadata}

    if exclude_label:
        exclude_set = set(exclude_label)
        exclude_job_names = set()
        for m in all_metadata_unfiltered:
            job_labels = set(m.get("labels") or [])
            if job_labels & exclude_set:
                exclude_job_names.add(m["job_name"])

    try:
        result = await list_results_for_dashboard_filtered(
            job_names=job_names,
            exclude_job_names=exclude_job_names,
            search=search,
            status=status,
            date_from=date_from,
            date_to=date_to,
            review_status=review_status,
            analysis_state=analysis_state,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    jobs = result["jobs"]

    # Attach metadata to each job
    for job in jobs:
        jn = job.get("job_name", "")
        job["metadata"] = all_metadata_by_name.get(jn, None)

    return {"jobs": jobs, "total": result["total"]}


# --- Reports endpoints ---


def _parse_csv_list(value: str, *, max_length: int = 2000) -> list[str] | None:
    """Parse comma-separated string into deduplicated list, or None if empty."""
    if not value:
        return None
    if len(value) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"Filter value too long ({len(value)} chars, max {max_length})",
        )
    items = list(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))
    return items if items else None


def _parse_report_metadata(
    team: str, tier: str, version: str
) -> tuple[list[str] | None, list[str] | None, list[str] | None]:
    """Parse CSV metadata filters for report endpoints."""
    return _parse_csv_list(team), _parse_csv_list(tier), _parse_csv_list(version)


_VALID_REVIEW_STATUSES = {"", "reviewed", "not_reviewed"}


def _validate_review_status(review_status: str) -> None:
    """Reject invalid review_status values with HTTP 400."""
    if review_status not in _VALID_REVIEW_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid review_status '{review_status}'. Must be 'reviewed', 'not_reviewed', or empty.",
        )


@app.get("/api/reports/totals", operation_id="reportsTotals")
async def reports_totals(
    request: Request,
    team: str = Query(default=""),
    tier: str = Query(default=""),
    version: str = Query(default=""),
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    status: str = Query(default=""),
    tags: str = Query(default=""),
    exclude_tags: str = Query(default=""),
    review_status: str = Query(default=""),
    limit: int = Query(default=0, ge=0, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Aggregate totals: total jobs, failures, reviewed, with per-job detail list.

    Requires admin or can_view_reports.
    """
    _require_can_view_reports(request)
    _validate_review_status(review_status)
    team_list, tier_list, version_list = _parse_report_metadata(team, tier, version)
    tag_list = _parse_csv_list(tags)
    exclude_tag_list = _parse_csv_list(exclude_tags)
    status_list = _parse_csv_list(status)
    logger.debug(
        "GET /api/reports/totals: team=%r, tier=%r, version=%r, from=%r, to=%r, status=%r, tags=%r, exclude_tags=%r, review_status=%r",
        team_list,
        tier_list,
        version_list,
        date_from,
        date_to,
        status_list,
        tag_list,
        exclude_tag_list,
        review_status,
    )
    return await storage.get_report_totals(
        team=team_list,
        tier=tier_list,
        version=version_list,
        date_from=date_from,
        date_to=date_to,
        status=status_list,
        tags=tag_list,
        exclude_tags=exclude_tag_list,
        review_status=review_status,
        limit=limit,
        offset=offset,
    )


@app.get(
    "/api/reports/classification-overrides",
    operation_id="reportsClassificationOverrides",
)
async def reports_classification_overrides(
    request: Request,
    team: str = Query(default=""),
    tier: str = Query(default=""),
    version: str = Query(default=""),
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    status: str = Query(default=""),
    tags: str = Query(default=""),
    exclude_tags: str = Query(default=""),
    review_status: str = Query(default=""),
    limit: int = Query(default=0, ge=0, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Classification overrides grouped by from→to transition.

    Requires admin or can_view_reports.
    """
    _require_can_view_reports(request)
    _validate_review_status(review_status)
    team_list, tier_list, version_list = _parse_report_metadata(team, tier, version)
    tag_list = _parse_csv_list(tags)
    exclude_tag_list = _parse_csv_list(exclude_tags)
    status_list = _parse_csv_list(status)
    logger.debug(
        "GET /api/reports/classification-overrides: team=%r, tier=%r, version=%r, from=%r, to=%r, status=%r, tags=%r, exclude_tags=%r, review_status=%r",
        team_list,
        tier_list,
        version_list,
        date_from,
        date_to,
        status_list,
        tag_list,
        exclude_tag_list,
        review_status,
    )
    return await storage.get_report_classification_overrides(
        team=team_list,
        tier=tier_list,
        version=version_list,
        date_from=date_from,
        date_to=date_to,
        status=status_list,
        tags=tag_list,
        exclude_tags=exclude_tag_list,
        review_status=review_status,
        limit=limit,
        offset=offset,
    )


@app.get("/api/reports/issues-created", operation_id="reportsIssuesCreated")
async def reports_issues_created(
    request: Request,
    team: str = Query(default=""),
    tier: str = Query(default=""),
    version: str = Query(default=""),
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    status: str = Query(default=""),
    tags: str = Query(default=""),
    exclude_tags: str = Query(default=""),
    review_status: str = Query(default=""),
    limit: int = Query(default=0, ge=0, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """GitHub/Jira issues created from analysis results.

    Requires admin or can_view_reports.
    """
    _require_can_view_reports(request)
    _validate_review_status(review_status)
    team_list, tier_list, version_list = _parse_report_metadata(team, tier, version)
    tag_list = _parse_csv_list(tags)
    exclude_tag_list = _parse_csv_list(exclude_tags)
    status_list = _parse_csv_list(status)
    logger.debug(
        "GET /api/reports/issues-created: team=%r, tier=%r, version=%r, from=%r, to=%r, status=%r, tags=%r, exclude_tags=%r, review_status=%r",
        team_list,
        tier_list,
        version_list,
        date_from,
        date_to,
        status_list,
        tag_list,
        exclude_tag_list,
        review_status,
    )
    return await storage.get_report_issues_created(
        team=team_list,
        tier=tier_list,
        version=version_list,
        date_from=date_from,
        date_to=date_to,
        status=status_list,
        tags=tag_list,
        exclude_tags=exclude_tag_list,
        review_status=review_status,
        limit=limit,
        offset=offset,
    )


# --- Notification endpoints ---


@app.get("/api/notifications/vapid-public-key", operation_id="getVapidPublicKey")
async def get_vapid_public_key() -> dict[str, Any]:
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


@app.post("/api/notifications/subscribe", operation_id="subscribeNotifications")
async def subscribe_notifications(
    body: PushSubscriptionRequest, request: Request
) -> dict[str, Any]:
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


@app.post("/api/notifications/unsubscribe", operation_id="unsubscribeNotifications")
async def unsubscribe_notifications(
    body: UnsubscribeRequest, request: Request
) -> dict[str, Any]:
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


@app.get("/api/users/mentions", operation_id="getUserMentions")
async def get_user_mentions(request: Request) -> dict[str, Any]:
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


@app.post("/api/users/mentions/read-all", operation_id="markAllMentionsReadEndpoint")
async def mark_all_mentions_read_endpoint(request: Request) -> dict[str, Any]:
    """Mark ALL mentions as read for the current user."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    count = await storage.mark_all_mentions_read(username)
    notify_mentions_changed(username)
    return {"marked_read": count}


@app.post("/api/users/mentions/read", operation_id="markMentionsAsRead")
async def mark_mentions_as_read(request: Request) -> dict[str, Any]:
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


@app.get("/api/users/mentions/unread-count", operation_id="getUnreadMentionsCount")
async def get_unread_mentions_count(request: Request) -> dict[str, Any]:
    """Get count of unread mentions for navbar badge."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    count = await storage.get_unread_mention_count(username)
    return {"count": count}


@app.get("/api/users/mentionable", operation_id="getMentionableUsers")
async def get_mentionable_users(request: Request) -> dict[str, Any]:
    """Return list of usernames that can be mentioned in comments."""
    username = request.state.username
    if not username:
        raise HTTPException(status_code=401, detail="Username required")
    _check_allow_list(request)
    users = await storage.list_users()
    return {"usernames": [u["username"] for u in users]}


@app.post(
    "/api/analyze-comment-intent",
    response_model=AnalyzeCommentResponse,
    operation_id="analyzeCommentIntent",
)
async def analyze_comment_intent(
    request: Request, body: AnalyzeCommentRequest
) -> AnalyzeCommentResponse:
    """Analyze a comment to determine if it implies a failure has been reviewed/resolved."""
    _check_allow_list(request)
    _require_reviewer(request)

    ai_provider = body.ai_provider or get_settings().ai_provider
    ai_model = body.ai_model or get_settings().ai_model
    # Fall back to the job's stored AI config as an atomic pair
    # Only use stored config when request doesn't set either field
    if not ai_provider and not ai_model and body.job_id:
        stored = await storage.get_result(body.job_id)
        if stored and stored.get("result"):
            params = stored["result"].get("request_params", {})
            ai_provider = params.get("ai_provider", "")
            ai_model = params.get("ai_model", "")
    ai_provider, ai_model = _resolve_ai_config_values(
        ai_provider, ai_model, request=request
    )
    token = ai_username.set(request.state.username)
    try:
        ai_provider, ai_model = await _validate_catalog_pair(ai_provider, ai_model)
    finally:
        ai_username.reset(token)

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
    token = ai_username.set(request.state.username)
    try:
        result = await call_ai_once(
            prompt,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=None,
            tools=[],
        )
    finally:
        ai_username.reset(token)

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
    operation_id="previewFeedback",
)
async def preview_feedback(
    request: Request, body: FeedbackRequest
) -> FeedbackPreviewResponse:
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
    ai_provider, ai_model = _resolve_ai_config_values(
        body.ai_provider, body.ai_model, request=request
    )
    token = ai_username.set(request.state.username)
    try:
        ai_provider, ai_model = await _validate_catalog_pair(ai_provider, ai_model)
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
    finally:
        ai_username.reset(token)


@app.post(
    "/api/feedback/create",
    status_code=201,
    response_model=FeedbackResponse,
    operation_id="createFeedback",
)
async def create_feedback(
    request: Request, body: FeedbackCreateRequest
) -> FeedbackResponse:
    """Create a GitHub issue from a previewed feedback.

    Takes a title, body, and labels (typically from the preview endpoint)
    and creates the GitHub issue using the authenticated user's GitHub token.
    """
    _check_allow_list(request)
    settings = get_settings()
    if not settings.feedback_enabled:
        raise HTTPException(
            status_code=503, detail="Feedback submission is disabled on this server"
        )

    username = request.state.username
    user_tokens = await storage.get_user_tokens(username)
    github_token = user_tokens.get("github_token", "")

    # Bootstrap admin (ADMIN_KEY auth) has no DB entry, so get_user_tokens
    # returns empty. Fall back to the server-level token for this special user
    # only — regular users must configure their own GitHub token.
    if not github_token and username == "admin" and settings.github_token:
        github_token = settings.github_token.get_secret_value()

    if not github_token:
        raise HTTPException(
            status_code=400,
            detail="GitHub token is required. Set up your token in Profile Settings.",
        )

    try:
        # Attribution comes from the signed provenance the preview embedded in
        # its body — never from the request (which carries no provider/model)
        # and never re-resolved here, so a settings change between preview and
        # create cannot miscredit the model that wrote the body.
        return await create_feedback_from_preview(
            title=body.title,
            body=body.body,
            labels=body.labels,
            github_token=github_token,
        )
    except httpx.HTTPStatusError as exc:
        # 401 means the token is bad; 403 means it is valid but either lacks
        # the repo scope or is rate limited. A classic PAT authenticates fine
        # without `repo`, so identity checks cannot tell these apart. Reporting
        # both as "invalid or expired" made a scope problem look like a lost
        # key. Neither is a gateway failure: 502 stays reserved for an
        # unreachable GitHub API.
        #
        # Rate limiting is classified up front, before any status branch, so a
        # new rate-limit status cannot fall through to the generic 502. GitHub
        # uses 403 for the primary limit and 429 for the secondary limit; 429
        # is definitionally a rate limit, while 403 needs header/body evidence.
        response = exc.response
        if response.status_code == 429 or (
            response.status_code == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in response.text.lower()
            )
        ):
            # A new token cannot fix a spent quota, so never point at the token.
            raise HTTPException(
                status_code=429,
                detail="GitHub API rate limit reached. Try again later.",
            ) from exc
        if exc.response.status_code == 401:
            raise HTTPException(
                status_code=403,
                detail="GitHub token is invalid or expired. "
                "Generate a new token in Profile Settings.",
            ) from exc
        if exc.response.status_code == 403:
            # Reaching here means no rate-limit signal, so it is a real scope
            # failure and the token is the thing to fix.
            raise HTTPException(
                status_code=403,
                detail="GitHub token is missing the 'repo' scope. "
                "Regenerate it in Profile Settings with repo access.",
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


def _build_ci_workspace_params(
    decrypted_params: dict[str, Any], result_data: dict[str, Any]
) -> dict[str, Any]:
    """Build params dict for CI chat workspace setup from stored request params."""
    return {
        **decrypted_params,
        "job_name": result_data.get("job_name", ""),
        "build_number": resolve_display_build_id(result_data),
        "analysis_type": decrypted_params.get(
            "analysis_type", result_data.get("analysis_type", "jenkins")
        ),
    }


def _require_explicit_chat_source(source: str, force_server: bool) -> None:
    """Reject a server-only model when chat explicitly selected user credentials."""
    if source == "server" and not force_server:
        raise HTTPException(
            status_code=422,
            detail="This model requires server credentials; select Server and Start again",
        )


async def _validate_chat_selection(
    body: ChatInitRequest, username: str
) -> tuple[str, str]:
    """Validate the chosen pair as this user, never as the analysis job."""
    provider, model = await _normalize_and_validate_ai_params(
        body.ai_provider,
        body.ai_model,
        username=username,
        force_server=body.force_server_credentials,
    )
    if not provider or not model:
        raise HTTPException(status_code=422, detail="Select an AI provider and model")
    from rootcoz.ai_client import _selected_credential_source

    source = _selected_credential_source.get()
    if body.force_server_credentials and source != "server":
        raise HTTPException(
            status_code=422, detail="Server credential selection unavailable"
        )
    _require_explicit_chat_source(source, body.force_server_credentials)
    if source == "server":
        token = ai_username.set(username)
        try:
            from rootcoz.ai_client import (
                InactiveServerProviderError,
                require_server_provider_grant,
            )

            await require_server_provider_grant(provider, model)
        except InactiveServerProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            ai_username.reset(token)
    elif source != "user":
        raise HTTPException(
            status_code=422, detail="Chat credential source unavailable"
        )
    logger.debug(
        "Chat selection validated for user=%s provider=%s source=%s",
        username,
        provider,
        source,
    )
    return provider, model


# Sentinel for "caller did not supply the latest-session row".
_UNSET_SESSION: Any = object()


async def _available_chat_session(
    job_id: str, username: str, session: Any = _UNSET_SESSION
) -> dict[str, str] | None:
    """Describe only a session the current owner can still use.

    ``session`` reuses an already-read latest-session row (including "no
    session") so a history read does not re-query it and risk straddling a
    concurrent Start.
    """
    active = (
        await storage.get_latest_chat_session(job_id, username)
        if session is _UNSET_SESSION
        else session
    )
    if not active:
        return None
    try:
        source = await storage.get_ai_session_source(
            active["session_id"], username, active["ai_provider"]
        )
        if source == "server":
            # Same rule Start applies, rechecked per read: the full server
            # grant, or the free grant for a pair that is still declared
            # free (a removed pair retires its sessions too).
            from rootcoz.ai_client import require_server_provider_grant

            token = ai_username.set(username)
            try:
                await require_server_provider_grant(
                    active["ai_provider"], active["ai_model"]
                )
            except ValueError:
                return None
            finally:
                ai_username.reset(token)
        elif source == "user":
            # A provider can stop accepting user keys after the session started
            # (e.g. it was added to AMBIENT_AUTH_PROVIDERS). Retire the session
            # only on a confirmed loss: a failed discovery (None) preserves it
            # instead of failing every user session during a sidecar hiccup.
            from rootcoz.ai_client import provider_accepts_user_keys

            if await provider_accepts_user_keys(active["ai_provider"]) is False:
                return None
            from pi_sidecar_client import _validate_api_key

            key, generation = await storage.get_user_ai_credential_with_generation(
                username, active["ai_provider"]
            )
            if not key or generation is None or _validate_api_key(key):
                return None
        else:
            return None
    except ValueError, LookupError, OSError:
        logger.debug("Chat session unavailable for user=%s", username)
        return None
    return {
        "ai_provider": active["ai_provider"],
        "ai_model": active["ai_model"],
        "credential_source": source,
    }


async def _current_chat_choice(
    job_id: str, username: str
) -> tuple[dict[str, Any], bool]:
    """Load the selected session and its persisted credential source."""
    active = await storage.get_latest_chat_session(job_id, username)
    if not active:
        raise HTTPException(status_code=409, detail="Start a new chat session first")
    try:
        source = await storage.get_ai_session_source(
            active["session_id"], username, active["ai_provider"]
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Chat session unavailable; start a new chat"
        ) from exc
    if source not in ("user", "server"):
        raise HTTPException(
            status_code=409, detail="Chat session unavailable; start a new chat"
        )
    return active, source == "server"


async def _check_chat_session_choice(
    job_id: str,
    username: str,
    provider: str,
    model: str,
    force_server: bool | None,
    *,
    allow_empty: bool = False,
) -> tuple[str, bool]:
    """Pin chat turns to the last Start's owner, model and credential source."""
    if allow_empty and not await _available_chat_session(job_id, username):
        return "", False
    active, selected_force = await _current_chat_choice(job_id, username)
    if (provider, model) != (active["ai_provider"], active["ai_model"]) or (
        force_server is not None and force_server != selected_force
    ):
        raise HTTPException(
            status_code=409,
            detail="Clear chat and Start a new session to change model or credentials",
        )
    return active["session_id"], selected_force


# Sentinel for "the user SELECTED a Jira credential but it is unusable"
# (mode "none" connection). Distinct from None (= no selection at all):
# build_chat_custom_tools treats None as permission to resolve the user's
# other stored credentials — a failed selected connection must not fall
# through to that fallback.
_JIRA_CHAT_SELECTION_FAILED = JiraConnection(
    mode="none",
    api_base="",
    browse_base="",
    site_url="",
    auth=None,
)


async def _resolve_chat_credentials(
    decrypted_params: dict[str, Any], username: str
) -> tuple[str, str, JiraConnection | None, str, str]:
    """Resolve Jira and GitHub credentials for chat.

    Jira uses user-scoped tokens only. GitHub uses user-scoped tokens
    only. Server deployment tokens and job-stored tokens are
    never exposed to chat sessions.

    Returns:
        (jira_email, jira_token, jira_connection, github_token,
        github_repo). ``jira_connection`` is the resolved (and, for service
        accounts, cloud-ID-completed) connection — ``None`` when the user
        selected NO Jira credential at all, or the
        ``_JIRA_CHAT_SELECTION_FAILED`` sentinel (a mode-``"none"``
        connection) when a selected credential exists but is unusable —
        chat tool building must not fall back to the user's OTHER stored
        credential after the selected one failed. Each method reads its
        own server-level URL and TLS verification (URLs are not
        credentials).
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

    # User-scoped credentials only — never job-stored or server deployment tokens
    jira_email = user_tokens.get("jira_email", "")
    jira_token = user_tokens.get("jira_token", "")
    jira_sa_token = user_tokens.get("jira_sa_token", "")
    github_token = (user_tokens.get("github_token") or "").strip()

    jira_connection: JiraConnection | None = None
    if jira_token or jira_sa_token:
        # Cloud ID, site URL, per-method URLs/SSL flags, and the OAuth 2.0
        # credential's client ID are server-level non-secrets — safe to
        # reuse for user-scoped secret resolution (the user secret +
        # server client ID must belong to the same Atlassian OAuth 2.0
        # credential). The user's field choice pins the method; each
        # method then reads its own URL. Prefix detection wins over field
        # placement (#327 semantics): an ATOA-prefixed secret or an
        # ATST-prefixed scoped token saved in the regular jira_token field
        # routes to the oauth/atst path so chat agrees with validation and
        # effective settings (which detect the prefixes). The
        # service-account field wins when both fields carry classified
        # values.
        regular_token = jira_token.strip()
        sa_secret = jira_sa_token.strip()
        atst_token = ""
        sa_kind = classify_jira_secret(sa_secret)
        token_kind = classify_jira_secret(regular_token)
        if sa_kind == "atst":
            atst_token = sa_secret
            sa_secret = ""
        elif not sa_kind and token_kind:
            if token_kind == "oauth":
                sa_secret = regular_token
            else:
                atst_token = regular_token
            regular_token = ""
        jira_connection = resolve_jira_connection_from_params(
            jira_cloud_url=settings.jira_cloud_url,
            jira_server_url=settings.jira_server_url,
            jira_email=jira_email,
            jira_token=regular_token,
            jira_oauth_client_secret=sa_secret,
            atlassian_oauth_client_id=str(settings.atlassian_oauth_client_id or ""),
            atlassian_oauth_cloud_id=str(settings.atlassian_oauth_cloud_id or ""),
            jira_site_url=str(settings.jira_site_url or ""),
            jira_atst_token=atst_token,
            jira_cloud_ssl_verify=settings.jira_cloud_ssl_verify,
            jira_server_ssl_verify=settings.jira_server_ssl_verify,
            jira_oauth_ssl_verify=settings.jira_oauth_ssl_verify,
        )
        if jira_connection.mode == "none":
            # The selected fields cannot form a usable method (e.g. an
            # OAuth secret without a server client ID) — a FAILED
            # selection, not an absent one.
            jira_connection = _JIRA_CHAT_SELECTION_FAILED
        else:
            if jira_connection.mode in ("sa", "atst"):
                # Complete BEFORE treating an empty api_base as unusable:
                # complete_jira_connection resolves the cloud ID from the
                # site URL (tenant_info) when none is configured (and mints
                # the access token for oauth-mode SA connections).
                try:
                    jira_connection = await complete_jira_connection(jira_connection)
                except JiraOAuthMintError as exc:
                    logger.warning("Chat credentials: %s; Jira tools unavailable", exc)
                    jira_connection = _JIRA_CHAT_SELECTION_FAILED
            if jira_connection.mode != "none" and not jira_connection.api_base:
                logger.warning(
                    "Chat credentials: could not resolve the selected Jira "
                    "method's API base; Jira tools unavailable"
                )
                jira_connection = _JIRA_CHAT_SELECTION_FAILED

    return (
        jira_email,
        jira_token,
        jira_connection,
        github_token,
        github_repo,
    )


# -- Chat endpoints --


def _chat_session_version(job_id: str, username: str, session_id: str | None) -> str:
    """Opaque, scoped version for conditional chat clears; never expose session IDs."""
    payload = json.dumps([job_id, username, session_id], separators=(",", ":"))
    return hmac.new(
        get_hmac_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


async def _current_chat_session_version(job_id: str, username: str) -> str:
    active = await storage.get_latest_chat_session(job_id, username)
    return _chat_session_version(
        job_id, username, active["session_id"] if active else None
    )


async def _require_chat_clear_version(
    job_id: str, username: str, request: Request
) -> None:
    """Call while holding the per-user chat lock shared with Start."""
    supplied = request.headers.get("if-match", "")
    current = await _current_chat_session_version(job_id, username)
    if not supplied or not hmac.compare_digest(supplied, current):
        logger.info("Chat clear conflict for job %s, user %s", job_id, username)
        raise HTTPException(
            status_code=409,
            detail="Chat session changed; refresh history before clearing",
        )


@app.get("/api/chat/{job_id}", operation_id="getChatHistory")
async def get_chat_history(
    job_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Get chat message history for an analyzed job."""
    _check_allow_list(request)
    result = await get_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")

    username = getattr(request.state, "username", "")
    key = f"{job_id}:{username}"
    preparing = _chat_prep_phases.get(key)
    # /init holds the per-user chat lock for the whole workspace preparation.
    # Blocking on it here would hide the very phase we report, so reads skip it
    # while preparation is in flight — the snapshot below keeps them consistent
    # instead, since a Start writes its session marker and welcome there.
    lock: AbstractAsyncContextManager[Any] = (
        nullcontext() if preparing else _hold_chat_lock(key)
    )
    async with lock:
        messages, total, session = await storage.get_chat_history_snapshot(
            job_id, limit=limit, offset=offset, username=username
        )
        # Filter out hidden init messages (empty content + completed status, used for session_id storage)
        # Keep pending messages even if empty (they show "Thinking..." in the UI)
        messages = [
            {k: v for k, v in m.items() if k != "session_id"}
            for m in messages
            if m.get("content") or m.get("status") in ("pending", "failed")
        ]
        active = await _available_chat_session(job_id, username, session=session)
        version = _chat_session_version(
            job_id, username, session["session_id"] if session else None
        )
    return strip_sensitive_from_response(
        {
            "messages": messages,
            "total": total,
            "active_session": active,
            "active_session_version": version,
            "preparing": preparing,
        }
    )


async def _index_chat_repositories(
    workspace: Path, repos: dict[str, Path] | None = None
) -> None:
    """Index cloned chat roots off the event loop and record outcomes."""
    from rootcoz.engine.graft import index_repositories, log_index_outcomes
    from rootcoz.engine.graft_http import cloned_graph_roots

    if repos is None:
        repos = await asyncio.to_thread(cloned_graph_roots, workspace)
    log_index_outcomes(await asyncio.to_thread(index_repositories, workspace, repos))


async def _init_chat_under_barrier(
    job_id: str, username: str, body: ChatInitRequest
) -> dict[str, Any]:
    """Initialize chat workspace; hold the lifecycle barrier only for short checks.

    Slow clone/session work runs without the barrier so job deletion can cancel
    this task instead of waiting for I/O. Recheck deletion before filesystem
    commits so a late init cannot recreate MCP state after cleanup.
    """
    from rootcoz.engine.chat import (
        build_chat_custom_tools,
        build_welcome_message,
        clone_chat_repos,
        ensure_chat_workspace,
        init_chat_session,
    )
    from rootcoz.engine.graft_http import cloned_graph_roots
    from rootcoz.sources.chat_workspace import setup_ci_build_workspace

    async with _get_chat_job_barrier(job_id):
        _raise_if_chat_job_deleted(job_id)
        stored = await get_result(job_id, strip_sensitive=False)
        if not stored or not stored.get("result"):
            raise HTTPException(status_code=404, detail="Job not found")
        result_data = stored["result"]
        params = result_data.get("request_params", {})
        ai_provider, ai_model = await _validate_chat_selection(body, username)
        existing_session_id, _ = await _check_chat_session_choice(
            job_id,
            username,
            ai_provider,
            ai_model,
            body.force_server_credentials,
            allow_empty=True,
        )
        workspace = ensure_chat_workspace(job_id, username=username)

    decrypted_params: dict[str, Any] = {}
    try:
        decrypted_params = decrypt_sensitive_fields(dict(params))
    except Exception:
        logger.warning("Failed to decrypt request_params for chat init", exc_info=True)

    _settings = get_settings()

    (
        jira_email,
        jira_token,
        jira_connection,
        github_token,
        github_repo,
    ) = await _resolve_chat_credentials(decrypted_params, username)

    session_id: str | None = existing_session_id
    _raise_if_chat_job_deleted(job_id)
    set_chat_prep_phase(job_id, username, "preparing")
    await clone_chat_repos(
        workspace,
        decrypted_params,
        user_repo_token=github_token,
        on_repo=lambda name: set_chat_prep_phase(job_id, username, "cloning", name),
    )
    repos_available = bool(await asyncio.to_thread(cloned_graph_roots, workspace))
    if repos_available:
        set_chat_prep_phase(job_id, username, "indexing")
        await _index_chat_repositories(workspace)
    _raise_if_chat_job_deleted(job_id)
    set_chat_prep_phase(job_id, username, "fetching_build_data")
    ci_build_data_available = await setup_ci_build_workspace(
        workspace,
        _build_ci_workspace_params(decrypted_params, result_data),
        github_token=github_token,
        settings=_settings,
    )
    _raise_if_chat_job_deleted(job_id)
    if not await _available_chat_session(job_id, username):
        custom_tools: list[dict[str, Any]] = []
        auth_header = await _create_ai_auth_header(username)
        if auth_header:
            server_url = _build_internal_server_url()
            custom_tools = build_chat_custom_tools(
                server_url=server_url,
                auth_token=auth_header.removeprefix("Bearer ").strip(),
                job_id=job_id,
                jira_cloud_url=_settings.jira_cloud_url,
                jira_server_url=_settings.jira_server_url,
                jira_email=jira_email,
                jira_token=jira_token,
                github_token=github_token,
                github_repo=github_repo,
                jira_connection=jira_connection,
            )
        else:
            logger.warning(
                "Chat init: no auth token for %s — tools unavailable in system prompt",
                username,
            )

        _raise_if_chat_job_deleted(job_id)
        from rootcoz.engine.chat import graph_http_tools

        custom_tools.extend(graph_http_tools(workspace, job_id))
        credential_generation = (
            None
            if body.force_server_credentials
            else await storage.get_user_ai_credential_generation(username, ai_provider)
        )
        from rootcoz.ai_client import _selected_credential_source, chat_session_source

        token = ai_username.set(username)
        force_token = force_server_credentials.set(body.force_server_credentials)
        source_token = chat_session_source.set(_selected_credential_source.get())
        set_chat_prep_phase(job_id, username, "starting_session")
        try:
            session_id = await init_chat_session(
                job_id=job_id,
                job_name=result_data.get("job_name", "unknown"),
                build_number=resolve_display_build_id(result_data),
                ai_provider=ai_provider,
                ai_model=ai_model,
                repo_path=workspace,
                custom_tools=custom_tools,
                repos_available=repos_available,
                ci_build_data_available=ci_build_data_available,
            )
        finally:
            chat_session_source.reset(source_token)
            ai_username.reset(token)
            force_server_credentials.reset(force_token)
        if job_id in _chat_jobs_deleting and session_id:
            await _discard_unsaved_ai_session(session_id, username, ai_provider)
        _raise_if_chat_job_deleted(job_id)
        if session_id:
            saved = await storage.add_chat_message(
                job_id=job_id,
                role="assistant",
                content="",
                username=username,
                ai_provider=ai_provider,
                ai_model=ai_model,
                session_id=session_id,
                status="completed",
                credential_generation=credential_generation,
            )
            if not saved:
                await _discard_unsaved_ai_session(session_id, username, ai_provider)
                session_id = None
        welcome_text = build_welcome_message(
            job_name=result_data.get("job_name", "unknown"),
            build_number=resolve_display_build_id(result_data),
            repos_available=repos_available,
            ci_build_data_available=ci_build_data_available,
            jira_available=bool(jira_connection),
            github_available=bool(github_token and github_repo),
        )
        if session_id:
            # The previous session is gone, so its welcome is stale chrome.
            # A dead session's welcome would otherwise accumulate in history on
            # every credential or grant change. No session_id here: the init
            # placeholder above is the session marker, and a welcome carrying one
            # would take over as the latest Start.
            await storage.delete_chat_welcome_messages(job_id, username)
            await storage.add_chat_message(
                job_id=job_id,
                role="assistant",
                content=welcome_text,
                username=username,
                status="completed",
            )

    logger.info(
        "Chat init for job %s: workspace=%s, repos=%s, session=%s",
        job_id,
        workspace,
        repos_available,
        session_id,
    )
    repo_names = []
    if workspace.exists():
        for item in workspace.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                repo_names.append(item.name)
    return {
        "ready": True,
        "repos_cloned": repos_available,
        "repo_names": repo_names,
        "ci_build_data_available": ci_build_data_available,
        "job_name": result_data.get("job_name", ""),
        "build_number": resolve_display_build_id(result_data),
        "session_id": session_id or "",
    }


@app.post("/api/chat/{job_id}/init", operation_id="initChat")
async def init_chat(
    job_id: str, request: Request, body: ChatInitRequest | None = None
) -> dict[str, Any]:
    """Initialize chat workspace: create directory, clone repos, and start AI session."""
    _check_allow_list(request)
    _require_reviewer(request)
    if body is None:
        raise HTTPException(
            status_code=422, detail="Select provider, model and credential source"
        )
    username = getattr(request.state, "username", "")
    try:
        async with (
            _track_chat_job_task(job_id),
            _hold_chat_lock(f"{job_id}:{username}"),
        ):
            result = await _init_chat_under_barrier(job_id, username, body)
    finally:
        clear_chat_prep_phase(job_id, username)
    if result["session_id"]:
        notify_chat_changed(job_id, username=username)
    return result


@app.post("/api/chat/{job_id}/close", operation_id="closeChat")
async def close_chat(job_id: str, request: Request) -> dict[str, Any]:
    """Signal that a user left the chat page.

    Does NOT clean up the workspace — other users/tabs may still be active.
    Workspace cleanup only happens on DELETE /api/chat/{job_id} (clear history).
    """
    _check_allow_list(request)
    _require_reviewer(request)
    logger.info("Chat: user left chat page for job %s", job_id)
    return {"status": "ok"}


@app.post("/api/chat/{job_id}/abort", operation_id="abortChat")
async def abort_chat(job_id: str, request: Request) -> dict[str, Any]:
    """Abort the currently processing chat message for this user."""
    _check_allow_list(request)
    _require_reviewer(request)
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
_chat_lock_refs: dict[str, int] = {}
# Per-job lifecycle barrier: init, message processing, and deletion share this
# so a late init cannot create MCP state after job deletion has begun.
_chat_job_barriers: dict[str, asyncio.Lock] = {}
_chat_jobs_deleting: set[str] = set()
_chat_job_tasks: dict[str, set[asyncio.Task[Any]]] = {}

ADMIN_CHAT_JOB_ID = "__admin_chat__"


def _get_chat_lock(job_id: str) -> asyncio.Lock:
    """Get or create a per-job chat processing lock."""
    if job_id not in _chat_locks:
        _chat_locks[job_id] = asyncio.Lock()
        _chat_lock_refs[job_id] = 0
    return _chat_locks[job_id]


@asynccontextmanager
async def _hold_chat_lock(key: str) -> AsyncIterator[asyncio.Lock]:
    """Acquire the per-user chat lock without dropping waiters from the registry.

    Callers increment the refcount before ``acquire``, so a later ``_get_chat_lock``
    cannot replace the lock object while another task is queued on it.
    """
    lock = _get_chat_lock(key)
    _chat_lock_refs[key] = _chat_lock_refs.get(key, 0) + 1
    try:
        async with lock:
            yield lock
    finally:
        refs = _chat_lock_refs.get(key, 1) - 1
        if refs > 0:
            _chat_lock_refs[key] = refs
        else:
            _chat_lock_refs.pop(key, None)
            stored = _chat_locks.get(key)
            if stored is lock and not lock.locked():
                _chat_locks.pop(key, None)


def _get_chat_job_barrier(job_id: str) -> asyncio.Lock:
    """Lock that serializes chat init/messages against job deletion."""
    if job_id not in _chat_job_barriers:
        _chat_job_barriers[job_id] = asyncio.Lock()
    return _chat_job_barriers[job_id]


def _discard_idle_chat_job_barrier(job_id: str) -> None:
    """Drop unused per-job barriers so the registry cannot grow forever."""
    lock = _chat_job_barriers.get(job_id)
    if lock is not None and not lock.locked():
        _chat_job_barriers.pop(job_id, None)


def _raise_if_chat_job_deleted(job_id: str) -> None:
    if job_id in _chat_jobs_deleting:
        raise HTTPException(status_code=404, detail="Job not found")


@asynccontextmanager
async def _track_chat_job_task(job_id: str) -> AsyncIterator[None]:
    """Register the current task so job deletion can cancel in-flight chat work."""
    task = asyncio.current_task()
    if task is None:
        yield
        return
    _chat_job_tasks.setdefault(job_id, set()).add(task)
    try:
        yield
    finally:
        remaining = _chat_job_tasks.get(job_id)
        if remaining is not None:
            remaining.discard(task)
            if not remaining:
                _chat_job_tasks.pop(job_id, None)


async def _cancel_tracked_chat_job_tasks(job_id: str) -> None:
    """Cancel in-flight chat init/message tasks so deletion is not blocked."""
    current = asyncio.current_task()
    tasks = [
        task
        for task in tuple(_chat_job_tasks.get(job_id, ()))
        if task is not current and not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _fail_chat_assistant_placeholder(
    assistant_msg_id: int,
    job_id: str,
    username: str,
    content: str,
) -> None:
    """Best-effort: mark a pending assistant row failed so it is never left pending."""
    try:
        await storage.update_chat_message_content(assistant_msg_id, content)
        await storage.update_chat_message_status(assistant_msg_id, "failed")
        notify_chat_changed(job_id, username=username)
    except Exception:
        logger.exception(
            "Failed to update error status for chat msg %d",
            assistant_msg_id,
        )


async def _cleanup_deleted_job_chat_workspaces(job_id: str) -> None:
    """Remove chat MCP state after a job delete, serialized with in-flight chat.

    Marks the job as deleting first so init/message work that has not yet
    entered the lifecycle barrier will 404. In-flight init and AI tasks are
    cancelled (instead of holding the barrier across clone/AI timeouts), then
    filesystem cleanup runs in a worker thread so contended MCP ``flock`` does
    not block the event loop. After cleanup the deleting marker and idle
    barrier are dropped; the job row is already gone so a later init cannot
    recreate workspace state.
    """
    from rootcoz.engine.chat import cleanup_chat_workspace

    _chat_jobs_deleting.add(job_id)
    try:
        await _cancel_tracked_chat_job_tasks(job_id)
        async with _get_chat_job_barrier(job_id):
            await asyncio.to_thread(cleanup_chat_workspace, job_id)
    except Exception:
        logger.warning("Failed to cleanup chat workspace for %s", job_id, exc_info=True)
    finally:
        _chat_jobs_deleting.discard(job_id)
        _chat_job_tasks.pop(job_id, None)
        _discard_idle_chat_job_barrier(job_id)


# Per-job:user abort signals — set to cancel in-progress chat processing
_chat_abort_signals: dict[str, asyncio.Event] = {}


def _get_chat_abort_signal(key: str) -> asyncio.Event:
    """Get or create an abort signal for a job:user chat."""
    if key not in _chat_abort_signals:
        _chat_abort_signals[key] = asyncio.Event()
    return _chat_abort_signals[key]


def _cleanup_chat_state(key: str) -> None:
    """Remove the abort signal for a key to prevent unbounded dict growth.

    Per-user locks stay in ``_chat_locks`` until ``_hold_chat_lock`` refcount
    hits zero, so waiters keep the same lock object.
    """
    _chat_abort_signals.pop(key, None)


async def _normalize_and_validate_ai_params(
    ai_provider: str | None,
    ai_model: str | None,
    *,
    username: str,
    force_server: bool,
) -> tuple[str | None, str | None]:
    """Normalize and validate AI provider/model from request body.

    Returns (provider, model) with whitespace stripped, legacy aliases
    normalized (e.g. ``cursor-cli`` / ``CURSOR`` → ``cursor``), and blanks
    as None. Raises HTTPException 422 for invalid providers, or when only
    one of provider/model is set (avoids pairing UI provider with settings
    model).
    """
    provider_raw = (ai_provider or "").strip()
    provider = (normalize_provider(provider_raw) or None) if provider_raw else None
    model = (ai_model or "").strip() or None
    if (provider and not model) or (model and not provider):
        raise HTTPException(
            status_code=422,
            detail="Both ai_provider and ai_model are required when either is set",
        )
    if provider and model:
        from rootcoz.ai_client import _selected_credential_source

        _selected_credential_source.set("")
        user_token = ai_username.set(username)
        force_token = force_server_credentials.set(force_server)
        try:
            return await _validate_catalog_pair(provider, model)
        finally:
            force_server_credentials.reset(force_token)
            ai_username.reset(user_token)
    return provider, model


def _resolve_chat_ai_config(
    *,
    override_provider: str | None,
    override_model: str | None,
    settings_provider: str,
    settings_model: str,
    result_data: dict[str, Any] | None = None,
    request_params: dict[str, Any] | None = None,
    is_admin: bool = False,
) -> tuple[str, str]:
    """Resolve provider/model for job and admin chat background processors.

    Prefer an explicit override pair; otherwise fall back to result /
    request_params / settings. Raises RuntimeError on partial override or
    missing final config. Missing-config errors are role-aware.
    """
    if override_provider and override_model:
        return override_provider, override_model
    if override_provider or override_model:
        raise RuntimeError(
            "Both AI provider and model are required. Select both before sending."
        )
    result = result_data or {}
    params = request_params or {}
    provider = (
        result.get("ai_provider", "")
        or params.get("ai_provider", "")
        or settings_provider
    )
    model = result.get("ai_model", "") or params.get("ai_model", "") or settings_model
    if not provider or not model:
        if is_admin:
            raise RuntimeError(
                "No AI provider/model configured. Select provider and model "
                "in chat, or set defaults in Server Settings → AI."
            )
        raise RuntimeError(
            "No AI provider/model configured. Select provider and model "
            "in chat, or contact a server administrator to configure AI settings."
        )
    return provider, model


@app.get("/api/chat/{job_id}/stream", operation_id="chatStream")
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


@app.post("/api/chat/{job_id}", status_code=202, operation_id="sendChatMessage")
async def send_chat_message(
    job_id: str,
    body: ChatMessageRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Queue a chat message for AI processing.

    Saves the user message immediately and kicks off background AI processing.
    The response is delivered via SSE on /api/chat/{job_id}/stream.
    """
    _check_allow_list(request)
    _require_reviewer(request)
    stored = await get_result(job_id, strip_sensitive=False)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail="Job not found")
    async with _hold_chat_lock(f"{job_id}:{request.state.username}"):
        if bool(body.ai_provider) != bool(body.ai_model):
            raise HTTPException(
                status_code=422, detail="Select both provider and model"
            )
        session, selected_force = await _current_chat_choice(
            job_id, request.state.username
        )
        ai_provider, ai_model = await _normalize_and_validate_ai_params(
            body.ai_provider or session["ai_provider"],
            body.ai_model or session["ai_model"],
            username=request.state.username,
            force_server=selected_force,
        )
        if ai_provider is None or ai_model is None:
            raise HTTPException(
                status_code=422, detail="Select both provider and model"
            )
        from rootcoz.ai_client import _selected_credential_source

        _require_explicit_chat_source(_selected_credential_source.get(), selected_force)
        await _check_chat_session_choice(
            job_id,
            request.state.username,
            ai_provider,
            ai_model,
            body.force_server_credentials,
        )
        user_msg_id, assistant_msg_id = await storage.add_chat_message_pair(
            job_id=job_id,
            user_content=body.message,
            username=request.state.username,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )
    logger.info("Chat: queued user message %d for job %s", user_msg_id, job_id)

    notify_chat_changed(job_id, username=request.state.username)

    # Kick off background processing with the pre-created assistant placeholder
    background_tasks.add_task(
        _process_chat_message,
        job_id=job_id,
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        message=body.message,
        ai_provider_override=ai_provider,
        ai_model_override=ai_model,
        username=request.state.username,
        is_admin=bool(getattr(request.state, "is_admin", False)),
        force_server=selected_force,
    )

    return {
        "user_message": {
            "id": user_msg_id,
            "role": "user",
            "content": body.message,
            "username": request.state.username,
            "status": "completed",
        },
        "assistant_message_id": assistant_msg_id,
    }


async def _process_chat_message(
    *,
    job_id: str,
    user_msg_id: int,
    assistant_msg_id: int,
    message: str,
    ai_provider_override: str | None,
    ai_model_override: str | None,
    username: str,
    is_admin: bool = False,
    force_server: bool | None = None,
) -> None:
    """Background task: process a single chat message with AI."""
    ai_username.set(username)
    from rootcoz.ai_client import chat_session_source
    from rootcoz.engine.chat import (
        build_chat_custom_tools,
        chat_with_ai,
        clone_chat_repos,
        ensure_chat_workspace,
        install_http_tools_mcp_best_effort_async,
    )
    from rootcoz.sources.chat_workspace import setup_ci_build_workspace

    auth_header = ""
    source_token = None

    async with _track_chat_job_task(job_id):
        try:
            async with _hold_chat_lock(f"{job_id}:{username}"):
                try:
                    async with _get_chat_job_barrier(job_id):
                        if job_id in _chat_jobs_deleting:
                            logger.info(
                                "Chat: skip processing for deleted job %s user %s",
                                job_id,
                                username,
                            )
                            await _fail_chat_assistant_placeholder(
                                assistant_msg_id,
                                job_id,
                                username,
                                "Job was deleted.",
                            )
                            return
                        stored = await get_result(job_id, strip_sensitive=False)
                        if not stored or not stored.get("result"):
                            raise RuntimeError(
                                f"Job {job_id} not found during chat processing"
                            )

                        result_data = stored["result"]
                        params = result_data.get("request_params", {})

                        _chat_settings = get_settings()
                        ai_provider, ai_model = ai_provider_override, ai_model_override
                        if not ai_provider or not ai_model:
                            raise ValueError("Chat selection missing")
                        session_id, selected_force = await _check_chat_session_choice(
                            job_id, username, ai_provider, ai_model, force_server
                        )
                        force_server_credentials.set(selected_force)
                        source_token = chat_session_source.set(
                            "server" if selected_force else "user"
                        )

                        credential_generation = (
                            None
                            if selected_force
                            else await storage.get_user_ai_credential_generation(
                                username, ai_provider
                            )
                        )
                        # Get conversation history
                        msg_count = await storage.count_chat_messages(
                            job_id, username=username
                        )
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
                        # Scan all_history (not filtered history) because the init
                        # message has empty content but carries the session_id
                        last_session_id = session_id
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
                                "Failed to decrypt request_params for chat context",
                                exc_info=True,
                            )
                        (
                            jira_email,
                            jira_token,
                            jira_connection,
                            github_token,
                            github_repo,
                        ) = await _resolve_chat_credentials(decrypted_params, username)

                        repos_available = await clone_chat_repos(
                            workspace, decrypted_params, user_repo_token=github_token
                        )
                        from rootcoz.engine.graft import roots_needing_index
                        from rootcoz.engine.graft_http import cloned_graph_roots

                        cloned_roots = await asyncio.to_thread(
                            cloned_graph_roots, workspace
                        )
                        stale_roots = await asyncio.to_thread(
                            roots_needing_index, workspace, cloned_roots
                        )
                        if stale_roots:
                            await _index_chat_repositories(workspace, stale_roots)

                        settings = get_settings()

                        # Populate workspace with CI build data
                        ci_build_data_available = await setup_ci_build_workspace(
                            workspace,
                            _build_ci_workspace_params(decrypted_params, result_data),
                            github_token=github_token,
                            settings=settings,
                        )

                        server_url = _build_internal_server_url()
                        auth_header = await _create_ai_auth_header(username)

                        custom_tools: list[dict[str, Any]] = []
                        if auth_header:
                            custom_tools = build_chat_custom_tools(
                                server_url=server_url,
                                auth_token=auth_header.removeprefix("Bearer ").strip(),
                                job_id=job_id,
                                jira_cloud_url=settings.jira_cloud_url,
                                jira_server_url=settings.jira_server_url,
                                jira_email=jira_email,
                                jira_token=jira_token,
                                github_token=github_token,
                                github_repo=github_repo,
                                jira_connection=jira_connection,
                            )
                        else:
                            logger.warning(
                                "Chat: no auth token for %s — tools unavailable",
                                username,
                            )

                        from rootcoz.engine.chat import graph_http_tools

                        custom_tools.extend(graph_http_tools(workspace, job_id))
                        await install_http_tools_mcp_best_effort_async(
                            workspace, custom_tools
                        )

                        abort_key = f"{job_id}:{username}"
                        abort_signal = _get_chat_abort_signal(abort_key)
                        if abort_signal.is_set():
                            abort_signal.clear()
                            await _fail_chat_assistant_placeholder(
                                assistant_msg_id,
                                job_id,
                                username,
                                "Aborted by user.",
                            )
                            logger.info(
                                "Chat: aborted before AI call for job %s, user %s",
                                job_id,
                                username,
                            )
                            return

                    # Lifecycle barrier is released before the AI call so job
                    # deletion is not blocked behind chat_with_ai timeouts.
                    if job_id in _chat_jobs_deleting:
                        await _fail_chat_assistant_placeholder(
                            assistant_msg_id,
                            job_id,
                            username,
                            "Job was deleted.",
                        )
                        return

                    success, response_text, new_session_id = await chat_with_ai(
                        job_id=job_id,
                        job_name=result_data.get("job_name", "unknown"),
                        build_number=resolve_display_build_id(result_data),
                        message=message,
                        history=history,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        repo_path=workspace,
                        ai_call_timeout=settings.ai_call_timeout,
                        session_id=last_session_id,
                        custom_tools=custom_tools,
                        repos_available=repos_available,
                        ci_build_data_available=ci_build_data_available,
                        install_mcp=False,
                    )

                    if abort_signal.is_set():
                        abort_signal.clear()
                        if new_session_id and new_session_id != last_session_id:
                            await _discard_unsaved_ai_session(
                                new_session_id, username, ai_provider
                            )
                        await _fail_chat_assistant_placeholder(
                            assistant_msg_id,
                            job_id,
                            username,
                            "Aborted by user.",
                        )
                        logger.info(
                            "Chat: aborted after AI call for job %s, user %s",
                            job_id,
                            username,
                        )
                        return

                    if not success:
                        logger.error(
                            "Chat AI call failed for job %s: %s", job_id, response_text
                        )
                        user_error = format_chat_ai_user_error(
                            response_text, is_admin=is_admin, ai_provider=ai_provider
                        )
                        await _fail_chat_assistant_placeholder(
                            assistant_msg_id,
                            job_id,
                            username,
                            f"Error: {user_error}",
                        )
                        return

                    if (
                        selected_force
                        and not await storage.can_user_use_server_providers(username)
                    ):
                        logger.info(
                            "Chat: server grant revoked before completion for user=%s",
                            username,
                        )
                        await _fail_chat_assistant_placeholder(
                            assistant_msg_id,
                            job_id,
                            username,
                            "Server provider access requires an administrator grant",
                        )
                        return

                    current_status = await storage.get_chat_message_status(
                        assistant_msg_id
                    )
                    if current_status == "failed":
                        if new_session_id and new_session_id != last_session_id:
                            await _discard_unsaved_ai_session(
                                new_session_id, username, ai_provider
                            )
                        logger.info(
                            "Chat: message %d was aborted during processing, discarding response",
                            assistant_msg_id,
                        )
                        return

                    saved = await storage.complete_chat_message_if_generation(
                        assistant_msg_id,
                        content=response_text,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        session_id=new_session_id or "",
                        credential_generation=credential_generation,
                        server_source=selected_force,
                    )
                    if not saved:
                        await _discard_unsaved_chat_response(
                            assistant_msg_id,
                            job_id,
                            username,
                            ai_provider,
                            new_session_id
                            if new_session_id != last_session_id
                            else None,
                            "Chat",
                        )
                        return
                    logger.info(
                        "Chat: message %d processed for job %s (session=%s)",
                        assistant_msg_id,
                        job_id,
                        new_session_id,
                    )
                    notify_chat_changed(job_id, username=username)

                except Exception:
                    logger.exception(
                        "Chat processing failed for job %s, msg %d",
                        job_id,
                        assistant_msg_id,
                    )
                    await _fail_chat_assistant_placeholder(
                        assistant_msg_id,
                        job_id,
                        username,
                        "An error occurred while processing your message. Please try again.",
                    )
        except asyncio.CancelledError:
            await _fail_chat_assistant_placeholder(
                assistant_msg_id,
                job_id,
                username,
                "Chat processing was cancelled.",
            )
            raise
        finally:
            if source_token is not None:
                chat_session_source.reset(source_token)
            _cleanup_chat_state(f"{job_id}:{username}")
            _discard_idle_chat_job_barrier(job_id)
            # Do NOT revoke auth_header — it's embedded in custom tool HTTP headers
            # and must stay alive for the sidecar session lifetime


@app.delete("/api/chat/{job_id}", operation_id="clearChatHistory")
async def clear_chat_history(job_id: str, request: Request) -> dict[str, Any]:
    """Clear chat messages for the current user on a job."""
    _check_allow_list(request)
    _require_reviewer(request)
    from rootcoz.engine.chat import cleanup_chat_repos

    stored = await get_result(job_id)
    if not stored:
        raise HTTPException(status_code=404, detail="Job not found")

    username = request.state.username

    # Acquire the per-user chat lock to prevent tearing down workspace
    # while a background worker is still processing
    async with _hold_chat_lock(f"{job_id}:{username}"):
        await _require_chat_clear_version(job_id, username, request)
        count = await storage.delete_chat_messages(job_id, username=username)
        await _cleanup_revoked_ai_sessions()
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


@app.get("/api/admin/db/schema", operation_id="adminDbSchema")
async def admin_db_schema(request: Request) -> dict[str, Any]:
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


@app.post("/api/admin/db/query", operation_id="adminDbQuery")
async def admin_db_query(request: Request) -> dict[str, Any]:
    """Execute a read-only SQL query. Admin only."""
    _require_admin(request)

    body = await request.json()
    if not await _ai_admin_role_current(request.state.username):
        logger.info(
            "Admin DB query denied after role change for %s", request.state.username
        )
        raise HTTPException(status_code=403, detail="Admin access required")
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


@app.get("/api/admin/chat", operation_id="getAdminChatHistory")
async def get_admin_chat_history(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Get admin chat history."""
    _require_admin(request)
    username = getattr(request.state, "username", "")
    async with _hold_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}"):
        messages = await storage.get_chat_messages(
            ADMIN_CHAT_JOB_ID, limit=limit, offset=offset, username=username
        )
        messages = [
            {k: v for k, v in m.items() if k != "session_id"}
            for m in messages
            if m.get("content") or m.get("status") in ("pending", "failed")
        ]
        total = await storage.count_chat_messages(ADMIN_CHAT_JOB_ID, username=username)
        active = await _available_chat_session(ADMIN_CHAT_JOB_ID, username)
        version = await _current_chat_session_version(ADMIN_CHAT_JOB_ID, username)
    return strip_sensitive_from_response(
        {
            "messages": messages,
            "total": total,
            "active_session": active,
            "active_session_version": version,
        }
    )


@app.post("/api/admin/chat/init", operation_id="initAdminChat")
async def init_admin_chat(
    request: Request, body: ChatInitRequest | None = None
) -> dict[str, Any]:
    """Initialize admin chat workspace and AI session."""
    _require_admin(request)
    if body is None:
        raise HTTPException(
            status_code=422, detail="Select provider, model and credential source"
        )
    from rootcoz.engine.chat import (
        build_admin_custom_tools,
        ensure_chat_workspace,
        init_admin_chat_session,
    )

    username = getattr(request.state, "username", "")
    session_id: str | None = ""
    async with _hold_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}"):
        ai_provider, ai_model = await _validate_chat_selection(body, username)
        session_id, _ = await _check_chat_session_choice(
            ADMIN_CHAT_JOB_ID,
            username,
            ai_provider,
            ai_model,
            body.force_server_credentials,
            allow_empty=True,
        )
        workspace = ensure_chat_workspace(ADMIN_CHAT_JOB_ID, username=username)
        if not await _available_chat_session(ADMIN_CHAT_JOB_ID, username):
            custom_tools: list[dict[str, Any]] = []
            auth_header = await _create_ai_auth_header(username, is_admin=True)
            if auth_header:
                server_url = _build_internal_server_url()
                custom_tools = build_admin_custom_tools(
                    server_url=server_url,
                    auth_token=auth_header.removeprefix("Bearer ").strip(),
                )
            else:
                logger.warning("Admin chat init: no auth token for %s", username)
                if not await _ai_admin_role_current(username):
                    raise HTTPException(status_code=403, detail="Admin access required")

            credential_generation = (
                None
                if body.force_server_credentials
                else await storage.get_user_ai_credential_generation(
                    username, ai_provider
                )
            )
            from rootcoz.ai_client import (
                _selected_credential_source,
                chat_session_source,
            )

            token = ai_username.set(username)
            force_token = force_server_credentials.set(body.force_server_credentials)
            source_token = chat_session_source.set(_selected_credential_source.get())
            try:
                session_id = await init_admin_chat_session(
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    repo_path=workspace,
                    custom_tools=custom_tools,
                )
            finally:
                chat_session_source.reset(source_token)
                force_server_credentials.reset(force_token)
                ai_username.reset(token)
            if session_id:
                saved = await storage.add_chat_message(
                    job_id=ADMIN_CHAT_JOB_ID,
                    role="assistant",
                    content="",
                    username=username,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    session_id=session_id,
                    status="completed",
                    credential_generation=credential_generation,
                )
                if not saved:
                    await _discard_unsaved_ai_session(session_id, username, ai_provider)
                    session_id = None
    logger.info("Admin chat init: workspace=%s, session=%s", workspace, session_id)
    if session_id:
        notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
    return {"ready": True, "session_id": session_id or ""}


@app.post("/api/admin/chat/close", operation_id="closeAdminChat")
async def close_admin_chat(request: Request) -> dict[str, Any]:
    """Signal that a user left the admin chat page."""
    _require_admin(request)
    logger.info("Admin chat: user left admin chat page")
    return {"status": "ok"}


@app.post("/api/admin/chat/abort", operation_id="abortAdminChat")
async def abort_admin_chat(request: Request) -> dict[str, Any]:
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


@app.get("/api/admin/chat/stream", operation_id="adminChatStream")
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


@app.post("/api/admin/chat", status_code=202, operation_id="sendAdminChatMessage")
async def send_admin_chat_message(
    body: ChatMessageRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Queue an admin chat message for AI processing."""
    _require_admin(request)
    async with _hold_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{request.state.username}"):
        if bool(body.ai_provider) != bool(body.ai_model):
            raise HTTPException(
                status_code=422, detail="Select both provider and model"
            )
        session, selected_force = await _current_chat_choice(
            ADMIN_CHAT_JOB_ID, request.state.username
        )
        ai_provider, ai_model = await _normalize_and_validate_ai_params(
            body.ai_provider or session["ai_provider"],
            body.ai_model or session["ai_model"],
            username=request.state.username,
            force_server=selected_force,
        )
        if ai_provider is None or ai_model is None:
            raise HTTPException(
                status_code=422, detail="Select both provider and model"
            )
        from rootcoz.ai_client import _selected_credential_source

        _require_explicit_chat_source(_selected_credential_source.get(), selected_force)
        await _check_chat_session_choice(
            ADMIN_CHAT_JOB_ID,
            request.state.username,
            ai_provider,
            ai_model,
            body.force_server_credentials,
        )
        user_msg_id, assistant_msg_id = await storage.add_chat_message_pair(
            job_id=ADMIN_CHAT_JOB_ID,
            user_content=body.message,
            username=request.state.username,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )
    logger.info("Admin chat: queued user message %d", user_msg_id)

    notify_chat_changed(ADMIN_CHAT_JOB_ID, username=request.state.username)

    background_tasks.add_task(
        _process_admin_chat_message,
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        message=body.message,
        ai_provider_override=ai_provider,
        ai_model_override=ai_model,
        username=request.state.username,
        force_server=selected_force,
    )

    return {
        "user_message": {
            "id": user_msg_id,
            "role": "user",
            "content": body.message,
            "username": request.state.username,
            "status": "completed",
        },
        "assistant_message_id": assistant_msg_id,
    }


async def _process_admin_chat_message(
    *,
    user_msg_id: int,
    assistant_msg_id: int,
    message: str,
    ai_provider_override: str | None,
    ai_model_override: str | None,
    username: str,
    force_server: bool | None = None,
) -> None:
    """Background task: process a single admin chat message with AI."""
    from rootcoz.ai_client import chat_session_source
    from rootcoz.engine.chat import (
        admin_chat_with_ai,
        build_admin_custom_tools,
        ensure_chat_workspace,
    )

    auth_header = ""

    async with _hold_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}"):
        _admin_settings = get_settings()
        user_token = ai_username.set(username)
        force_token = force_server_credentials.set(False)
        source_token = None
        try:
            ai_provider, ai_model = ai_provider_override, ai_model_override
            if not ai_provider or not ai_model:
                raise ValueError("Chat selection missing")
            session_id, selected_force = await _check_chat_session_choice(
                ADMIN_CHAT_JOB_ID, username, ai_provider, ai_model, force_server
            )
            force_server_credentials.set(selected_force)
            source_token = chat_session_source.set(
                "server" if selected_force else "user"
            )

            credential_generation = (
                None
                if selected_force
                else await storage.get_user_ai_credential_generation(
                    username, ai_provider
                )
            )
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

            last_session_id = session_id
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
            auth_header = await _create_ai_auth_header(username, is_admin=True)
            if not await _ai_admin_role_current(username):
                await _cleanup_ai_session(auth_header)
                logger.info(
                    "Admin chat: access revoked before AI call for user %s", username
                )
                await _fail_chat_assistant_placeholder(
                    assistant_msg_id,
                    ADMIN_CHAT_JOB_ID,
                    username,
                    "Admin access required",
                )
                return

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
                if new_session_id and new_session_id != last_session_id:
                    await _discard_unsaved_ai_session(
                        new_session_id, username, ai_provider
                    )
                await storage.update_chat_message_content(
                    assistant_msg_id, "Aborted by user."
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
                logger.info("Admin chat: aborted after AI call, user %s", username)
                return

            if not success:
                logger.error("Admin chat AI call failed: %s", response_text)
                user_error = format_chat_ai_user_error(
                    response_text, is_admin=True, ai_provider=ai_provider
                )
                await storage.update_chat_message_content(
                    assistant_msg_id, f"Error: {user_error}"
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
                return

            if not await _ai_admin_role_current(username):
                if new_session_id and new_session_id != last_session_id:
                    await _discard_unsaved_ai_session(
                        new_session_id, username, ai_provider
                    )
                logger.info(
                    "Admin chat: access revoked during AI call for user %s", username
                )
                await _fail_chat_assistant_placeholder(
                    assistant_msg_id,
                    ADMIN_CHAT_JOB_ID,
                    username,
                    "Admin access required",
                )
                return

            if selected_force and not await storage.can_user_use_server_providers(
                username
            ):
                logger.info(
                    "Admin chat: server grant revoked before completion for user=%s",
                    username,
                )
                await _fail_chat_assistant_placeholder(
                    assistant_msg_id,
                    ADMIN_CHAT_JOB_ID,
                    username,
                    "Server provider access requires an administrator grant",
                )
                return

            # Check if message was aborted while AI was processing
            current_status = await storage.get_chat_message_status(assistant_msg_id)
            if current_status == "failed":
                if new_session_id and new_session_id != last_session_id:
                    await _discard_unsaved_ai_session(
                        new_session_id, username, ai_provider
                    )
                logger.info(
                    "Chat: message %d was aborted during processing, discarding response",
                    assistant_msg_id,
                )
                return

            saved = await storage.complete_chat_message_if_generation(
                assistant_msg_id,
                content=response_text,
                ai_provider=ai_provider,
                ai_model=ai_model,
                session_id=new_session_id or "",
                credential_generation=credential_generation,
                server_source=selected_force,
            )
            if not saved:
                await _discard_unsaved_chat_response(
                    assistant_msg_id,
                    ADMIN_CHAT_JOB_ID,
                    username,
                    ai_provider,
                    new_session_id if new_session_id != last_session_id else None,
                    "Admin chat",
                )
                return
            logger.info(
                "Admin chat: message %d processed (session=%s)",
                assistant_msg_id,
                new_session_id,
            )
            notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)

        except Exception:
            logger.exception(
                "Admin chat processing failed for msg %d",
                assistant_msg_id,
            )
            try:
                await storage.update_chat_message_content(
                    assistant_msg_id,
                    "An error occurred while processing your message. Please try again.",
                )
                await storage.update_chat_message_status(assistant_msg_id, "failed")
                notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
            except Exception:
                logger.exception(
                    "Failed to update error status for admin chat msg %d",
                    assistant_msg_id,
                )
        finally:
            if source_token is not None:
                chat_session_source.reset(source_token)
            force_server_credentials.reset(force_token)
            ai_username.reset(user_token)
            _cleanup_chat_state(f"{ADMIN_CHAT_JOB_ID}:{username}")
            # Do NOT revoke auth_header — it's embedded in custom tool HTTP headers


# -- Admin chat artifact helpers --

_ADMIN_ARTIFACTS_BASE = Path("/tmp/rootcoz-admin-artifacts")


def _get_admin_artifacts_dir(username: str) -> Path:
    """Get the artifacts directory for an admin user."""
    safe_user = username.replace("/", "_").replace("..", "_").replace("\\", "_")
    return _ADMIN_ARTIFACTS_BASE / safe_user


def _cleanup_admin_artifacts(username: str) -> None:
    """Delete all artifacts for an admin user."""
    import shutil

    artifacts_dir = _get_admin_artifacts_dir(username)
    if artifacts_dir.exists():
        try:
            shutil.rmtree(artifacts_dir)
            logger.info("Admin chat: cleaned up artifacts for %s", username)
        except OSError:
            logger.warning(
                "Admin chat: failed to clean up artifacts for %s",
                username,
                exc_info=True,
            )


class SaveArtifactRequest(BaseModel):
    html_content: str = Field(..., min_length=1, max_length=5_000_000)
    filename: str = Field(..., min_length=1, max_length=255)


@app.post("/api/admin-chat/artifacts", operation_id="saveAdminChatArtifact")
async def save_admin_chat_artifact(
    body: SaveArtifactRequest,
    request: Request,
) -> dict[str, Any]:
    """Save an HTML report artifact from admin chat. Returns a download URL."""
    _require_admin(request)
    username = request.state.username

    # Sanitize filename — strip path separators, keep only the basename
    safe_filename = (
        body.filename.replace("/", "_").replace("\\", "_").replace("..", "_").strip()
    )
    if not safe_filename:
        raise HTTPException(status_code=422, detail="Invalid filename")
    # Ensure .html extension
    if not safe_filename.endswith(".html"):
        safe_filename += ".html"

    artifact_id = str(uuid.uuid4())
    artifacts_dir = _get_admin_artifacts_dir(username)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.chmod(0o700)

    artifact_path = artifacts_dir / f"{artifact_id}.html"
    try:
        await asyncio.to_thread(
            artifact_path.write_text, body.html_content, encoding="utf-8"
        )
    except OSError as exc:
        logger.exception("Failed to save artifact")
        raise HTTPException(status_code=500, detail="Failed to save artifact") from exc

    logger.info(
        "Admin chat: saved artifact %s (%d chars) for %s",
        artifact_id,
        len(body.html_content),
        username,
    )

    return {
        "artifact_id": artifact_id,
        "download_url": f"/api/admin-chat/artifacts/{artifact_id}",
        "filename": safe_filename,
    }


@app.get("/api/admin-chat/artifacts/{artifact_id}", operation_id="getAdminChatArtifact")
async def get_admin_chat_artifact(
    artifact_id: str,
    request: Request,
) -> Response:
    """Download an admin chat HTML report artifact."""
    _require_admin(request)
    username = request.state.username

    # Validate artifact_id is a valid UUID to prevent path traversal
    try:
        uuid.UUID(artifact_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Artifact not found")

    artifacts_dir = _get_admin_artifacts_dir(username)
    artifact_path = (artifacts_dir / f"{artifact_id}.html").resolve()
    # Belt-and-suspenders: ensure resolved path stays under the artifacts dir
    if not artifact_path.is_relative_to(artifacts_dir.resolve()):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")

    try:
        content = await asyncio.to_thread(artifact_path.read_text, encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.exception("Failed to read artifact %s", artifact_id)
        raise HTTPException(status_code=500, detail="Failed to read artifact") from exc

    download_filename = f"report-{artifact_id[:8]}.html"

    return Response(
        content=content,
        media_type="text/html",
        headers={
            "Content-Disposition": f'attachment; filename="{download_filename}"',
        },
    )


@app.delete("/api/admin/chat", operation_id="clearAdminChatHistory")
async def clear_admin_chat_history(request: Request) -> dict[str, Any]:
    """Clear admin chat messages for the current user."""
    _require_admin(request)
    from rootcoz.engine.chat import cleanup_chat_repos

    username = request.state.username

    async with _hold_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}"):
        await _require_chat_clear_version(ADMIN_CHAT_JOB_ID, username, request)
        count = await storage.delete_chat_messages(ADMIN_CHAT_JOB_ID, username=username)
        await _cleanup_revoked_ai_sessions()
        notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
        try:
            cleanup_chat_repos(ADMIN_CHAT_JOB_ID, username=username)
        except Exception:
            logger.warning(
                "Failed to cleanup admin chat repos for %s",
                username,
                exc_info=True,
            )

    _cleanup_admin_artifacts(username)
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
    reload = os.getenv("DEV_MODE", os.getenv("DEBUG", "")).lower() == "true"
    uvicorn.run("rootcoz.main:app", host="0.0.0.0", port=APP_PORT, reload=reload)
