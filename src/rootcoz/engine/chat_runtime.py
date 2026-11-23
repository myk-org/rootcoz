"""Unified chat runtime: session lifecycle, locks, barriers, aborts, streaming, and turn execution.

This module owns all chat orchestration for both per-job chat and admin chat,
parameterized by two mode adapters (``JobChatAdapter`` and ``AdminChatAdapter``)
and an injectable ``ChatRuntimeHost`` port.

Dependency invariant (``AGENTS.md``): this module lives under ``engine/`` and
MUST NEVER import ``rootcoz.sources``, ``rootcoz.exporters``, or ``rootcoz.main``.
CI workspace population and HTTP/SSE framing are injected from the host layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import threading
import time as _time
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Sequence,
)
from collections.abc import (
    Set as AbstractSet,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, HTTPException

from rootcoz import ai_client, get_child_logger, storage
from rootcoz.ai_client import (
    CHAT_BUILTIN_TOOLS,
    SessionCreationRefused,
    ai_username,
    format_chat_ai_user_error,
    normalize_provider,
)
from rootcoz.config import Settings, get_settings
from rootcoz.encryption import decrypt_sensitive_fields, get_hmac_secret
from rootcoz.engine import chat as chat_engine
from rootcoz.engine import graft_http, session_tools
from rootcoz.jira import (
    JiraConnection,
    JiraOAuthMintError,
    classify_jira_secret,
    complete_jira_connection,
    resolve_jira_connection_from_params,
)
from rootcoz.models import ChatInitRequest, ChatMessageRequest
from rootcoz.temp_paths import (
    CleanupOutcome,
    PathStateUnknown,
    cleanup_path_off_loop,
)
from rootcoz.url_utils import mask_url_credentials_in_text, strip_nonprintable

logger = get_child_logger(__name__)

ADMIN_CHAT_JOB_ID = "__admin_chat__"
CHAT_INDEX_DEADLINE_SECONDS = 180

# Payload-carrying SSE broadcast queue cap (shared with log/audit streams).
_SSE_QUEUE_MAXSIZE = 1000

# Sentinel for "caller did not supply the latest-session row".
_UNSET_SESSION: Any = object()

#: The fingerprint entry a session carries when it was created with the code
#: graph. Chat indexes after the first turn, so a session started before the
#: graph existed can never see it — this is what makes it stale and lets the
#: next turn start one that can.
_GRAPH_TOOL_FINGERPRINT = "code-graph"

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

_TRANSIENT_CHAT_START_DETAIL = "The AI service is rate limited or timed out. Try again."
_CHAT_PAIR_CONFLICT_DETAIL = (
    "Clear chat and Start a new session to change model or credentials"
)
_CHAT_PREP_FAILED_DETAIL = "The chat workspace could not be prepared. Start it again."
_CHAT_PREP_CANCELLED_DETAIL = (
    "Chat preparation was interrupted before it started. Try again."
)
_CHAT_PREPARING_DETAIL = "Chat is being prepared ({phase}). Wait for it to be ready."


class _PayloadSubscription:
    """One subscriber's queue plus how many payloads it had to drop.

    ``dropped`` is only touched on the subscriber's own loop, so it needs no
    lock; the reader drains it with :meth:`take_dropped` once it catches up.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
        self.dropped = 0

    def take_dropped(self) -> int:
        """Return the drops since the last call and reset the counter."""
        dropped, self.dropped = self.dropped, 0
        return dropped


class _PayloadBroadcast:
    """One payload broadcast channel: the SSE subscribers of a single stream."""

    def __init__(self) -> None:
        self._listeners: set[tuple[asyncio.AbstractEventLoop, _PayloadSubscription]] = (
            set()
        )
        self._lock = threading.Lock()

    @property
    def has_listeners(self) -> bool:
        """Cheap check so a hot producer skips work when nobody is listening."""
        return bool(self._listeners)

    @property
    def listener_count(self) -> int:
        """Number of live subscribers (server status reports this)."""
        with self._lock:
            return len(self._listeners)

    def publish(self, payload: Any) -> None:
        """Push one payload to every subscriber (thread-safe)."""
        with self._lock:
            if not self._listeners:
                return
            listeners = list(self._listeners)

        def _enqueue(subscription: _PayloadSubscription) -> None:
            try:
                subscription.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Counted, not silently forgotten: the reader reports the gap
                # as its own event so a slow admin sees the stream is missing
                # payloads instead of trusting a gapped feed.
                subscription.dropped += 1

        for loop, subscription in listeners:
            try:
                loop.call_soon_threadsafe(_enqueue, subscription)
            except RuntimeError:
                logger.debug("SSE payload dropped: subscriber loop is closed")

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[_PayloadSubscription]:
        """Register the calling connection for this channel's payloads."""
        subscription = _PayloadSubscription()
        listener = (asyncio.get_running_loop(), subscription)
        with self._lock:
            self._listeners.add(listener)
        try:
            yield subscription
        finally:
            with self._lock:
                self._listeners.discard(listener)


@dataclass
class ChatInitHandshake:
    """What a Start request waits on: the slow phase, or the end of the task."""

    settled: asyncio.Event = dataclass_field(default_factory=asyncio.Event)
    error: BaseException | None = None
    #: The preflight finished and the slow work began — the one state in which
    #: the route answers ``202``. Without it, "settled without a signal" and
    #: "signalled and finished" would be the same observation.
    started: bool = False


@dataclass
class ChatPreparation:
    """One live preparation per job+user, as the refusals and dedupe see it."""

    pair: tuple[str, str]
    task: asyncio.Task[Any]


@dataclass
class PreparedTurn:
    """Mode-specific turn inputs prepared under the setup barrier."""

    custom_tools: list[dict[str, Any]]
    tool_fingerprint: tuple[str, ...]
    tracker_unavailable: dict[str, str]
    ai_call_timeout: int | None
    extra: dict[str, Any] = dataclass_field(default_factory=dict)
    refusal_message: str | None = None


@asynccontextmanager
async def _noop_async_cm() -> AsyncIterator[None]:
    """Async no-op context manager for adapters without a job barrier or task tracker."""
    yield


def render_chat_frame(frame: Any) -> str | None:
    """One connection carries both frames: a change notice, or a fragment."""
    if isinstance(frame, dict) and frame.get("type") == "refresh":
        return "event: chat-changed\ndata: refresh\n\n"
    return f"event: chat-delta\ndata: {json.dumps(frame)}\n\n"


def redacted_error(exc: BaseException) -> str:
    """Class, errno and a credential-free message — never a raw traceback."""
    errno_value = getattr(exc, "errno", None)
    suffix = f" (errno {errno_value})" if errno_value else ""
    # Free text with possibly several URLs, so the all-occurrence helper (not
    # ``mask_url_value``), pre-stripped like every other masked site.
    # shortcut: the free-text regex is lowercase-scheme-only, upgrade if git or
    # httpx ever echo an uppercase scheme.
    return (
        f"{type(exc).__name__}{suffix}: "
        f"{mask_url_credentials_in_text(strip_nonprintable(str(exc)))}"
    )


def chat_start_sidecar_error(exc: httpx.HTTPStatusError) -> HTTPException:
    """Shape a transient session-creation failure for both chat-start routes.

    A 408/425/429 never becomes a refusal sentence, because that marker is the
    non-retryable signal the analysis engine stops on. Chat start has no retry
    loop of its own, so it is the caller that has to name one: the user is told
    to try again instead of receiving a 500. Any other status is a real error
    and keeps the path it had before.
    """
    from rootcoz.ai_client import _TRANSIENT_SESSION_STATUSES

    if exc.response.status_code not in _TRANSIENT_SESSION_STATUSES:
        raise exc
    logger.warning(
        "Chat start: sidecar answered a transient status %s",
        exc.response.status_code,
    )
    return HTTPException(status_code=503, detail=_TRANSIENT_CHAT_START_DETAIL)


def resolve_chat_ai_config(
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


async def _default_setup_ci_workspace(
    workspace: Path,
    params: dict[str, Any],
    *,
    github_token: str = "",
    settings: Settings | None = None,
) -> bool:
    return False


async def _default_create_ai_auth_header(
    username: str,
    *,
    is_admin: bool = False,
    refuse_storage_fault: bool = False,
) -> str:
    return ""


async def _default_cleanup_ai_session(auth_header: str) -> None:
    return None


async def _default_ai_admin_role_current(username: str) -> bool:
    return True


async def _default_account_has_profile(username: str) -> bool:
    return True


def _default_chat_tracker_state(
    settings: Settings,
    user_tokens: dict[str, str],
    *,
    github_repo_available: bool,
    has_profile: bool = True,
    job_chat: bool = False,
) -> tuple[tuple[str, ...], dict[str, str]]:
    return (), {}


def _default_settings_github_repo_available(settings: Settings) -> bool:
    return False


def _default_job_issue_repo_slugs(
    params: dict[str, Any],
) -> tuple[str, set[str]]:
    return "", set()


async def _default_issue_blocked_notice(
    *,
    message: str,
    username: str,
    settings: Settings,
    github_repo_available: bool,
    job_chat: bool = False,
) -> str:
    return ""


async def _default_post_server_notice(
    *, conversation: str, username: str, content: str
) -> int:
    return await storage.add_chat_message(
        conversation, role="system", content=content, username=username
    )


async def _default_validate_catalog_pair(provider: str, model: str) -> tuple[str, str]:
    try:
        return await ai_client.resolve_catalog_pair(provider, model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _default_resolve_admin_artifacts_owner_id(
    username: str,
) -> tuple[bool, int | None]:
    return True, None


async def _default_cleanup_admin_artifacts(username: str, owner_id: int | None) -> None:
    return None


def _default_resolve_display_build_id(result_data: dict[str, Any]) -> str:
    return str(result_data.get("build_number") or result_data.get("build_id") or "")


@dataclass
class ChatRuntimeHost:
    """Host capabilities injected into ``ChatRuntime``.

    Keeps ``engine/chat_runtime.py`` independent of ``main.py``, ``sources/``,
    and ``exporters/`` while allowing production wiring and tests to supply or
    intercept host dependencies cleanly.
    """

    get_settings: Callable[[], Settings] = get_settings
    get_result: Callable[..., Awaitable[dict[str, Any] | None]] = storage.get_result
    resolve_display_build_id: Callable[[dict[str, Any]], str | int] = (
        _default_resolve_display_build_id
    )
    setup_ci_workspace: Callable[..., Awaitable[bool]] = _default_setup_ci_workspace
    create_ai_auth_header: Callable[..., Awaitable[str]] = (
        _default_create_ai_auth_header
    )
    cleanup_ai_session: Callable[[str], Awaitable[None]] = _default_cleanup_ai_session
    ai_admin_role_current: Callable[[str], Awaitable[bool]] = (
        _default_ai_admin_role_current
    )
    account_has_profile: Callable[[str], Awaitable[bool]] = _default_account_has_profile
    build_internal_server_url: Callable[[], str] = lambda: "http://127.0.0.1:8000"
    chat_tracker_state: Callable[..., tuple[tuple[str, ...], dict[str, str]]] = (
        _default_chat_tracker_state
    )
    settings_github_repo_available: Callable[[Settings], bool] = (
        _default_settings_github_repo_available
    )
    job_issue_repo_slugs: Callable[[dict[str, Any]], tuple[Any, AbstractSet[str]]] = (
        _default_job_issue_repo_slugs
    )
    issue_blocked_notice: Callable[..., Awaitable[str]] = _default_issue_blocked_notice
    post_server_notice: Callable[..., Awaitable[int]] = _default_post_server_notice
    validate_catalog_pair: Callable[[str, str], Awaitable[tuple[str, str]]] = (
        _default_validate_catalog_pair
    )
    spawn_background: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]] = (
        asyncio.create_task
    )
    resolve_admin_artifacts_owner_id: Callable[
        [str], Awaitable[tuple[bool, int | None]]
    ] = _default_resolve_admin_artifacts_owner_id
    cleanup_admin_artifacts: Callable[[str, int | None], Awaitable[Any]] = (
        _default_cleanup_admin_artifacts
    )
    cleanup_path_off_loop: Callable[..., Awaitable[CleanupOutcome]] = (
        cleanup_path_off_loop
    )

    # Optional dynamic dispatch hooks so callers/tests that monkeypatch top-level
    # helpers in main.py still intercept internal runtime calls cleanly.
    on_notify_chat_changed: Callable[..., None] | None = None
    on_set_chat_prep_phase: Callable[..., None] | None = None
    on_schedule_chat_index: Callable[..., None] | None = None
    on_validate_chat_selection: Callable[..., Awaitable[tuple[str, str]]] | None = None
    on_normalize_and_validate_ai_params: (
        Callable[..., Awaitable[tuple[str | None, str | None]]] | None
    ) = None
    on_resolve_chat_credentials: (
        Callable[..., Awaitable[tuple[str, str, JiraConnection | None, str, str]]]
        | None
    ) = None
    on_enter_chat_workspace: Callable[..., Awaitable[Path | None]] | None = None
    on_current_chat_choice: (
        Callable[[str, str], Awaitable[tuple[dict[str, Any], str]]] | None
    ) = None
    on_check_chat_session_choice: Callable[..., Awaitable[tuple[str, str]]] | None = (
        None
    )
    on_discard_unsaved_ai_session: Callable[..., Awaitable[None]] | None = None
    on_revoke_ai_sessions: Callable[[list[str]], Awaitable[None]] | None = None


class ChatModeAdapter(ABC):
    """Mode-specific workspace, tools, access checks, and AI invocation."""

    conversation_id: str
    log_label: str
    is_admin_mode: bool

    def track_turn(self, runtime: ChatRuntime) -> AbstractAsyncContextManager[None]:
        return _noop_async_cm()

    def setup_barrier(self, runtime: ChatRuntime) -> AbstractAsyncContextManager[None]:
        return _noop_async_cm()

    def check_deleted_message(self, runtime: ChatRuntime) -> str | None:
        return None

    def before_enqueue(self, runtime: ChatRuntime, username: str) -> None:
        return None

    async def before_clear(self, runtime: ChatRuntime, username: str) -> Any:
        return None

    async def after_clear(
        self, runtime: ChatRuntime, username: str, pre_clear_state: Any
    ) -> None:
        return None

    @abstractmethod
    async def issue_blocked_notice(
        self, runtime: ChatRuntime, *, message: str, username: str
    ) -> str: ...

    async def load_turn_context(self, runtime: ChatRuntime) -> dict[str, Any]:
        return {}

    @abstractmethod
    async def prepare_turn(
        self,
        runtime: ChatRuntime,
        *,
        workspace: Path,
        username: str,
        context: dict[str, Any],
    ) -> PreparedTurn: ...

    @abstractmethod
    async def call_ai(
        self,
        prepared: PreparedTurn,
        *,
        message: str,
        history: list[dict[str, Any]],
        ai_provider: str,
        ai_model: str,
        workspace: Path,
        session_id: str | None,
        on_delta: Callable[[str], None],
        on_warning: Callable[[str], None],
    ) -> tuple[bool, str, str | None]: ...

    async def verify_post_ai_access(
        self, runtime: ChatRuntime, username: str
    ) -> str | None:
        return None

    def after_turn_completed(self, runtime: ChatRuntime, workspace: Path) -> None:
        return None

    def cleanup_after_turn(self, runtime: ChatRuntime) -> None:
        return None


class JobChatAdapter(ChatModeAdapter):
    """Mode adapter for per-job failure analysis chat."""

    log_label = "Chat"
    is_admin_mode = False

    def __init__(
        self, job_id: str, *, stored_result: dict[str, Any] | None = None
    ) -> None:
        self.conversation_id = job_id
        self._stored_result = stored_result

    def track_turn(self, runtime: ChatRuntime) -> AbstractAsyncContextManager[None]:
        return runtime.track_chat_job_task(self.conversation_id)

    def setup_barrier(self, runtime: ChatRuntime) -> AbstractAsyncContextManager[None]:
        return runtime.get_chat_job_barrier(self.conversation_id)

    def check_deleted_message(self, runtime: ChatRuntime) -> str | None:
        if self.conversation_id in runtime.chat_jobs_deleting:
            return "Job was deleted."
        return None

    def before_enqueue(self, runtime: ChatRuntime, username: str) -> None:
        runtime.refuse_while_preparing(self.conversation_id, username)

    async def before_clear(self, runtime: ChatRuntime, username: str) -> Any:
        runtime.refuse_while_preparing(self.conversation_id, username)
        return None

    async def issue_blocked_notice(
        self, runtime: ChatRuntime, *, message: str, username: str
    ) -> str:
        request_params = ((self._stored_result or {}).get("result") or {}).get(
            "request_params", {}
        )
        return await runtime.host.issue_blocked_notice(
            message=message,
            username=username,
            settings=runtime.host.get_settings(),
            # This run's own repos are a destination too, exactly as the
            # background task reads them for the tool list.
            github_repo_available=bool(
                runtime.host.job_issue_repo_slugs(request_params)[1]
            ),
            job_chat=True,
        )

    async def load_turn_context(self, runtime: ChatRuntime) -> dict[str, Any]:
        stored = await runtime.host.get_result(
            self.conversation_id, strip_sensitive=False
        )
        if not stored or not stored.get("result"):
            raise RuntimeError(
                f"Job {self.conversation_id} not found during chat processing"
            )
        return {"result_data": stored["result"]}

    async def prepare_turn(
        self,
        runtime: ChatRuntime,
        *,
        workspace: Path,
        username: str,
        context: dict[str, Any],
    ) -> PreparedTurn:
        result_data = context["result_data"]
        params = result_data.get("request_params", {})

        decrypted_params: dict[str, Any] = {}
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
        ) = await runtime._call_resolve_chat_credentials(decrypted_params, username)

        repos_available = await chat_engine.clone_chat_repos(
            workspace, decrypted_params, user_repo_token=github_token
        )

        settings = runtime.host.get_settings()

        # Populate workspace with CI build data via injected source adapter
        ci_build_data_available = await runtime.host.setup_ci_workspace(
            workspace,
            runtime.build_ci_workspace_params(decrypted_params, result_data),
            github_token=github_token,
            settings=settings,
        )

        server_url = runtime.host.build_internal_server_url()
        auth_header = await runtime.host.create_ai_auth_header(username)

        issue_repos = runtime.host.job_issue_repo_slugs(decrypted_params)[1]
        tracker_targets, tracker_unavailable = runtime.host.chat_tracker_state(
            settings,
            await storage.get_user_tokens(username),
            github_repo_available=bool(issue_repos),
            has_profile=await runtime.host.account_has_profile(username),
            job_chat=True,
        )
        custom_tools: list[dict[str, Any]] = []
        if auth_header:
            custom_tools = chat_engine.build_chat_custom_tools(
                server_url=server_url,
                auth_token=auth_header.removeprefix("Bearer ").strip(),
                job_id=self.conversation_id,
                jira_cloud_url=settings.jira_cloud_url,
                jira_server_url=settings.jira_server_url,
                jira_email=jira_email,
                jira_token=jira_token,
                github_token=github_token,
                github_repo=github_repo,
                jira_connection=jira_connection,
                tracker_targets=tracker_targets,
                issue_repos=tuple(sorted(issue_repos)),
            )
        else:
            logger.warning(
                "Chat: no auth token for %s — tools unavailable",
                username,
            )

        session = await session_tools.build_session_tools(
            builtin=list(CHAT_BUILTIN_TOOLS),
            custom_base=custom_tools,
            workspace=workspace,
            job_id=self.conversation_id,
            # Never build a graph on the turn: it was 132 s of indexing before
            # a 5 s answer. This turn uses the graphs already on disk and the
            # build runs after it.
            wait_for_index=False,
        )
        custom_tools = session.custom
        fingerprint = runtime.chat_tool_fingerprint(tracker_targets, custom_tools)
        return PreparedTurn(
            custom_tools=custom_tools,
            tool_fingerprint=fingerprint,
            tracker_unavailable=tracker_unavailable,
            ai_call_timeout=settings.ai_call_timeout,
            extra={
                "job_name": result_data.get("job_name", "unknown"),
                "build_number": runtime.host.resolve_display_build_id(result_data),
                "repos_available": repos_available,
                "ci_build_data_available": ci_build_data_available,
                "issue_repos": tuple(sorted(issue_repos)),
            },
        )

    async def call_ai(
        self,
        prepared: PreparedTurn,
        *,
        message: str,
        history: list[dict[str, Any]],
        ai_provider: str,
        ai_model: str,
        workspace: Path,
        session_id: str | None,
        on_delta: Callable[[str], None],
        on_warning: Callable[[str], None],
    ) -> tuple[bool, str, str | None]:
        return await chat_engine.chat_with_ai(
            job_id=self.conversation_id,
            job_name=prepared.extra["job_name"],
            build_number=prepared.extra["build_number"],
            message=message,
            history=history,
            ai_provider=ai_provider,
            ai_model=ai_model,
            repo_path=workspace,
            ai_call_timeout=prepared.ai_call_timeout,
            session_id=session_id,
            custom_tools=prepared.custom_tools,
            repos_available=prepared.extra["repos_available"],
            ci_build_data_available=prepared.extra["ci_build_data_available"],
            tracker_unavailable=prepared.tracker_unavailable,
            issue_repos=prepared.extra["issue_repos"],
            # Display only: the stored reply is still the whole text.
            on_delta=on_delta,
            on_warning=on_warning,
        )

    def after_turn_completed(self, runtime: ChatRuntime, workspace: Path) -> None:
        # The answer is stored and pushed; the graph is built from here on, off
        # the turn. Nothing the user waits for queues behind it, and the next
        # turn finds the tools. Scheduled unconditionally: the build itself is
        # what knows whether this workspace has anything to index.
        runtime._call_schedule_chat_index(
            workspace,
            job_id=self.conversation_id,
            deadline=_time.monotonic() + CHAT_INDEX_DEADLINE_SECONDS,
        )

    def cleanup_after_turn(self, runtime: ChatRuntime) -> None:
        runtime.discard_idle_chat_job_barrier(self.conversation_id)


class AdminChatAdapter(ChatModeAdapter):
    """Mode adapter for admin database/analytics chat."""

    conversation_id = ADMIN_CHAT_JOB_ID
    log_label = "Admin chat"
    is_admin_mode = True

    async def before_clear(self, runtime: ChatRuntime, username: str) -> int | None:
        _ok, owner_id = await runtime.host.resolve_admin_artifacts_owner_id(username)
        return owner_id

    async def after_clear(
        self, runtime: ChatRuntime, username: str, pre_clear_state: Any
    ) -> None:
        await runtime.host.cleanup_admin_artifacts(username, pre_clear_state)

    async def issue_blocked_notice(
        self, runtime: ChatRuntime, *, message: str, username: str
    ) -> str:
        settings = runtime.host.get_settings()
        return await runtime.host.issue_blocked_notice(
            message=message,
            username=username,
            settings=settings,
            github_repo_available=runtime.host.settings_github_repo_available(settings),
        )

    async def prepare_turn(
        self,
        runtime: ChatRuntime,
        *,
        workspace: Path,
        username: str,
        context: dict[str, Any],
    ) -> PreparedTurn:
        settings = runtime.host.get_settings()
        server_url = runtime.host.build_internal_server_url()
        auth_header = await runtime.host.create_ai_auth_header(username, is_admin=True)
        if not await runtime.host.ai_admin_role_current(username):
            await runtime.host.cleanup_ai_session(auth_header)
            logger.info(
                "Admin chat: access revoked before AI call for user %s", username
            )
            return PreparedTurn(
                custom_tools=[],
                tool_fingerprint=(),
                tracker_unavailable={},
                ai_call_timeout=settings.ai_call_timeout,
                refusal_message="Admin access required",
            )

        tracker_targets, tracker_unavailable = runtime.host.chat_tracker_state(
            settings,
            await storage.get_user_tokens(username),
            github_repo_available=runtime.host.settings_github_repo_available(settings),
            has_profile=await runtime.host.account_has_profile(username),
        )
        custom_tools = chat_engine.build_admin_custom_tools(
            server_url=server_url,
            auth_token=auth_header.removeprefix("Bearer ").strip()
            if auth_header
            else "",
            tracker_targets=tracker_targets,
            conversation=ADMIN_CHAT_JOB_ID,
        )
        return PreparedTurn(
            custom_tools=custom_tools,
            tool_fingerprint=tracker_targets,
            tracker_unavailable=tracker_unavailable,
            ai_call_timeout=settings.ai_call_timeout,
        )

    async def call_ai(
        self,
        prepared: PreparedTurn,
        *,
        message: str,
        history: list[dict[str, Any]],
        ai_provider: str,
        ai_model: str,
        workspace: Path,
        session_id: str | None,
        on_delta: Callable[[str], None],
        on_warning: Callable[[str], None],
    ) -> tuple[bool, str, str | None]:
        return await chat_engine.admin_chat_with_ai(
            message=message,
            history=history,
            ai_provider=ai_provider,
            ai_model=ai_model,
            repo_path=workspace,
            ai_call_timeout=prepared.ai_call_timeout,
            session_id=session_id,
            custom_tools=prepared.custom_tools,
            tracker_unavailable=prepared.tracker_unavailable,
            on_delta=on_delta,
            on_warning=on_warning,
        )

    async def verify_post_ai_access(
        self, runtime: ChatRuntime, username: str
    ) -> str | None:
        if not await runtime.host.ai_admin_role_current(username):
            logger.info(
                "Admin chat: access revoked during AI call for user %s", username
            )
            return "Admin access required"
        return None


class ChatRuntime:
    """Single deep runtime owning all chat concurrency, lifecycle, streaming, and turns."""

    def __init__(self, host: ChatRuntimeHost | None = None) -> None:
        self.host: ChatRuntimeHost = host or ChatRuntimeHost()

        # Per-job chat change notifications
        self.chat_listeners: dict[str, set[asyncio.Event]] = {}
        # One channel per authenticated (job_id, username) — never per job:
        # model output must not be shared between users.
        self.chat_deltas: dict[str, _PayloadBroadcast] = {}
        # Running turn buffer: (assistant_msg_id, accumulated_text) per channel.
        self.chat_partials: dict[str, tuple[int, str]] = {}
        # Workspace-preparation phase per job+user.
        self.chat_prep_phases: dict[str, dict[str, Any]] = {}
        # Live preparations keyed `<job_id>:<username>`.
        self.chat_prep_tasks: dict[str, ChatPreparation] = {}
        # Per-user chat serialization locks & refcounts.
        self.chat_locks: dict[str, asyncio.Lock] = {}
        self.chat_lock_refs: dict[str, int] = {}
        # Per-job lifecycle barrier & deletion coordination.
        self.chat_job_barriers: dict[str, asyncio.Lock] = {}
        self.chat_jobs_deleting: set[str] = set()
        self.chat_job_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        # Per-job:user abort signals.
        self.chat_abort_signals: dict[str, asyncio.Event] = {}
        # Tracker/tool fingerprint a chat session was created with, keyed by session_id.
        self.chat_session_tracker_targets: dict[str, tuple[str, ...]] = {}

    # -- Dynamic dispatch helpers (for host / monkeypatch compatibility) --

    def _call_notify_chat_changed(self, job_id: str, username: str = "") -> None:
        if self.host.on_notify_chat_changed is not None:
            self.host.on_notify_chat_changed(job_id, username=username)
        else:
            self.notify_chat_changed(job_id, username=username)

    def _call_set_chat_prep_phase(
        self, job_id: str, username: str, phase: str, detail: str = ""
    ) -> None:
        if self.host.on_set_chat_prep_phase is not None:
            self.host.on_set_chat_prep_phase(job_id, username, phase, detail)
        else:
            self.set_chat_prep_phase(job_id, username, phase, detail)

    def _call_schedule_chat_index(
        self,
        workspace: Path,
        repos: dict[str, Path] | None = None,
        job_id: str = "",
        deadline: float | None = None,
    ) -> None:
        if self.host.on_schedule_chat_index is not None:
            self.host.on_schedule_chat_index(
                workspace, repos=repos, job_id=job_id, deadline=deadline
            )
        else:
            self.schedule_chat_index(
                workspace, repos=repos, job_id=job_id, deadline=deadline
            )

    async def _call_validate_chat_selection(
        self, body: ChatInitRequest, username: str
    ) -> tuple[str, str]:
        if self.host.on_validate_chat_selection is not None:
            return await self.host.on_validate_chat_selection(body, username)
        return await self.validate_chat_selection(body, username)

    async def _call_normalize_and_validate_ai_params(
        self,
        ai_provider: str | None,
        ai_model: str | None,
        *,
        username: str,
    ) -> tuple[str | None, str | None]:
        if self.host.on_normalize_and_validate_ai_params is not None:
            return await self.host.on_normalize_and_validate_ai_params(
                ai_provider, ai_model, username=username
            )
        return await self.normalize_and_validate_ai_params(
            ai_provider, ai_model, username=username
        )

    async def _call_resolve_chat_credentials(
        self, decrypted_params: dict[str, Any], username: str
    ) -> tuple[str, str, JiraConnection | None, str, str]:
        if self.host.on_resolve_chat_credentials is not None:
            return await self.host.on_resolve_chat_credentials(
                decrypted_params, username
            )
        return await self.resolve_chat_credentials(decrypted_params, username)

    async def _call_enter_chat_workspace(
        self, job_id: str, username: str, what: str
    ) -> Path | None:
        if self.host.on_enter_chat_workspace is not None:
            return await self.host.on_enter_chat_workspace(job_id, username, what)
        return await self.enter_chat_workspace(job_id, username, what)

    async def _call_current_chat_choice(
        self, job_id: str, username: str
    ) -> tuple[dict[str, Any], str]:
        if self.host.on_current_chat_choice is not None:
            return await self.host.on_current_chat_choice(job_id, username)
        return await self.current_chat_choice(job_id, username)

    async def _call_check_chat_session_choice(
        self,
        job_id: str,
        username: str,
        provider: str,
        model: str,
        source: str | None,
        *,
        allow_empty: bool = False,
    ) -> tuple[str, str]:
        if self.host.on_check_chat_session_choice is not None:
            return await self.host.on_check_chat_session_choice(
                job_id,
                username,
                provider,
                model,
                source,
                allow_empty=allow_empty,
            )
        return await self.check_chat_session_choice(
            job_id,
            username,
            provider,
            model,
            source,
            allow_empty=allow_empty,
        )

    async def _call_discard_unsaved_ai_session(
        self, session_id: str, username: str, provider: str
    ) -> None:
        if self.host.on_discard_unsaved_ai_session is not None:
            await self.host.on_discard_unsaved_ai_session(
                session_id, username, provider
            )
        else:
            await self.discard_unsaved_ai_session(session_id, username, provider)

    async def _call_revoke_ai_sessions(self, sessions: list[str]) -> None:
        if self.host.on_revoke_ai_sessions is not None:
            await self.host.on_revoke_ai_sessions(sessions)
        else:
            await self.revoke_ai_sessions(sessions)

    # -- Channel, delta streaming, and status metrics --

    def chat_turns_in_flight(self) -> int:
        """Chat init/message tasks running right now (one per turn)."""
        return sum(len(tasks) for tasks in self.chat_job_tasks.values())

    def chat_delta_listener_count(self) -> int:
        """Total active SSE subscribers across all chat delta channels."""
        return sum(channel.listener_count for channel in self.chat_deltas.values())

    @staticmethod
    def chat_channel_key(job_id: str, username: str) -> str:
        """The one key both chat channels use: listeners (notifications) and fragments.

        This is the *SSE channel* key only. The per-user work serialization sites
        (``hold_chat_lock``, ``chat_prep_phases``, ``get_chat_abort_signal``,
        ``cleanup_chat_state``) are a different namespace and keep their
        unconditional ``f"{job_id}:{username}"``.
        """
        return f"{job_id}:{username}" if username else job_id

    def notify_chat_changed(self, job_id: str, username: str = "") -> None:
        """Signal SSE listeners for a specific job+user that chat messages changed."""
        key = self.chat_channel_key(job_id, username)
        for event in self.chat_listeners.get(key, set()).copy():
            event.set()
        # The dedicated chat streams carry the same notice on their payload
        # channel, so a tab that lost a fragment burst still learns the reply
        # is stored.
        self.publish_chat_delta(key, {"type": "refresh"})

    def chat_delta_channel(self, job_id: str, username: str) -> _PayloadBroadcast:
        """The fragment channel for one authenticated user's turn."""
        return self.chat_deltas.setdefault(
            self.chat_channel_key(job_id, username), _PayloadBroadcast()
        )

    def publish_chat_delta(self, key: str, frame: dict[str, Any]) -> None:
        """Push one chat frame to that channel's streams; no channel means no delivery.

        The running turn's text is kept whether or not a stream is attached, so
        a tab that lost its connection mid-turn is caught up when it comes back.
        """
        message_id = frame.get("id")
        if isinstance(message_id, int) and isinstance(frame.get("delta"), str):
            held_id, held = self.chat_partials.get(key, (message_id, ""))
            self.chat_partials[key] = (
                message_id,
                held + frame["delta"] if held_id == message_id else frame["delta"],
            )
        else:
            self.chat_partials.pop(key, None)  # a change notice: the turn is over
        channel = self.chat_deltas.get(key)
        if channel is not None:
            channel.publish(frame)

    def chat_delta_publisher(
        self, job_id: str, username: str, message_id: int
    ) -> Callable[[str], None]:
        """The ``on_delta`` callable for one turn: fragments tagged with their row id."""
        key = self.chat_channel_key(job_id, username)
        # Claim the channel before the first fragment, so a browser stream that
        # subscribes after the turn started still has something to catch up
        # from, and drop the previous turn's text: this turn's buffer starts empty.
        self.chat_delta_channel(job_id, username)
        self.chat_partials.pop(key, None)
        return lambda delta: self.publish_chat_delta(
            key, {"id": message_id, "delta": delta}
        )

    @asynccontextmanager
    async def subscribe_chat_stream(
        self, job_id: str, username: str
    ) -> AsyncIterator[_PayloadSubscription]:
        """Claim, subscribe, replay any in-flight partial snapshot, and release."""
        key = self.chat_channel_key(job_id, username)
        # First statement, and no ``await`` between the claim and the subscribe:
        # ``chat_delta_channel`` is sync and ``subscribe()``'s ``__aenter__``
        # runs to its yield without awaiting, so no sibling stream's ``finally``
        # can observe this channel as listener-less and evict it in between.
        channel = self.chat_delta_channel(job_id, username)
        try:
            async with channel.subscribe() as subscription:
                held = self.chat_partials.get(key)
                if held and held[1]:
                    # One frame, not the deltas: a reconnect must not double the text.
                    subscription.queue.put_nowait({"id": held[0], "snapshot": held[1]})
                yield subscription
        finally:
            # Drop the channel only when the last subscriber has gone, and
            # nothing awaits in between. The partial text stays so a reconnect
            # mid-turn catches up instead of losing fragments published while
            # nobody was listening.
            if not channel.has_listeners and self.chat_deltas.get(key) is channel:
                self.chat_deltas.pop(key, None)

    # -- Locks, barriers, tasks, and abort signals --

    def get_chat_lock(self, key: str) -> asyncio.Lock:
        """Get or create a per-user chat processing lock."""
        if key not in self.chat_locks:
            self.chat_locks[key] = asyncio.Lock()
            self.chat_lock_refs[key] = 0
        return self.chat_locks[key]

    @asynccontextmanager
    async def hold_chat_lock(self, key: str) -> AsyncIterator[asyncio.Lock]:
        """Acquire the per-user chat lock without dropping waiters from the registry.

        Callers increment the refcount before ``acquire``, so a later
        ``get_chat_lock`` cannot replace the lock object while another task is
        queued on it.
        """
        lock = self.get_chat_lock(key)
        self.chat_lock_refs[key] = self.chat_lock_refs.get(key, 0) + 1
        try:
            async with lock:
                yield lock
        finally:
            refs = self.chat_lock_refs.get(key, 1) - 1
            if refs > 0:
                self.chat_lock_refs[key] = refs
            else:
                self.chat_lock_refs.pop(key, None)
                stored = self.chat_locks.get(key)
                if stored is lock and not lock.locked():
                    self.chat_locks.pop(key, None)

    def get_chat_job_barrier(self, job_id: str) -> asyncio.Lock:
        """Lock that serializes chat init/messages against job deletion."""
        if job_id not in self.chat_job_barriers:
            self.chat_job_barriers[job_id] = asyncio.Lock()
        return self.chat_job_barriers[job_id]

    def discard_idle_chat_job_barrier(self, job_id: str) -> None:
        """Drop unused per-job barriers so the registry cannot grow forever."""
        lock = self.chat_job_barriers.get(job_id)
        if lock is not None and not lock.locked():
            self.chat_job_barriers.pop(job_id, None)

    def raise_if_chat_job_deleted(self, job_id: str) -> None:
        if job_id in self.chat_jobs_deleting:
            raise HTTPException(status_code=404, detail="Job not found")

    @asynccontextmanager
    async def track_chat_job_task(self, job_id: str) -> AsyncIterator[None]:
        """Register the current task so job deletion can cancel in-flight chat work."""
        task = asyncio.current_task()
        if task is None:
            yield
            return
        self.chat_job_tasks.setdefault(job_id, set()).add(task)
        try:
            yield
        finally:
            remaining = self.chat_job_tasks.get(job_id)
            if remaining is not None:
                remaining.discard(task)
                if not remaining:
                    self.chat_job_tasks.pop(job_id, None)

    async def cancel_tracked_chat_job_tasks(self, job_id: str) -> None:
        """Cancel in-flight chat init/message tasks so deletion is not blocked."""
        current = asyncio.current_task()
        tasks = [
            task
            for task in tuple(self.chat_job_tasks.get(job_id, ()))
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_chat_abort_signal(self, key: str) -> asyncio.Event:
        """Get or create an abort signal for a job:user chat."""
        if key not in self.chat_abort_signals:
            self.chat_abort_signals[key] = asyncio.Event()
        return self.chat_abort_signals[key]

    def cleanup_chat_state(self, key: str) -> None:
        """Remove the abort signal and finished partial buffer for a key.

        Per-user locks stay in ``chat_locks`` until ``hold_chat_lock`` refcount
        hits zero, so waiters keep the same lock object.
        """
        self.chat_abort_signals.pop(key, None)
        self.chat_partials.pop(key, None)
        channel = self.chat_deltas.get(key)
        if channel is not None and not channel.has_listeners:
            self.chat_deltas.pop(key, None)

    async def cleanup_deleted_job_chat_workspaces(self, job_id: str) -> bool:
        """Remove chat workspaces after a job delete, serialized with in-flight chat."""
        self.chat_jobs_deleting.add(job_id)
        try:
            await self.cancel_tracked_chat_job_tasks(job_id)
            async with self.get_chat_job_barrier(job_id):
                outcome = await self.host.cleanup_path_off_loop(
                    job_id,
                    chat_engine.get_chat_workspace(job_id),
                    partial(chat_engine.cleanup_chat_workspace, job_id),
                )
        except Exception:
            logger.warning(
                "Failed to cleanup chat workspace for %s", job_id, exc_info=True
            )
            return False
        finally:
            self.chat_jobs_deleting.discard(job_id)
            self.chat_job_tasks.pop(job_id, None)
            self.discard_idle_chat_job_barrier(job_id)
        return outcome is CleanupOutcome.CLEAN

    # -- Preparation phase & handshake tracking --

    def set_chat_prep_phase(
        self, job_id: str, username: str, phase: str, detail: str = ""
    ) -> None:
        """Record the current chat workspace-preparation phase and notify listeners."""
        self.chat_prep_phases[f"{job_id}:{username}"] = {
            "phase": phase,
            "detail": detail,
        }
        self._call_notify_chat_changed(job_id, username=username)

    def clear_chat_prep_phase(self, job_id: str, username: str) -> None:
        """Drop the preparation phase once init finished (or failed)."""
        self.chat_prep_phases.pop(f"{job_id}:{username}", None)

    def live_chat_prep_phase(self, key: str) -> dict[str, Any]:
        """The phase a client may act on; a live preparation always has one."""
        return self.chat_prep_phases.get(key) or {"phase": "preparing", "detail": ""}

    def end_chat_preparation(
        self, job_id: str, username: str, task: asyncio.Task[Any] | None
    ) -> None:
        """Drop a finished preparation's registry entry and phase — only its own."""
        key = f"{job_id}:{username}"
        entry = self.chat_prep_tasks.get(key)
        if entry is None or entry.task is not task:
            return
        self.chat_prep_tasks.pop(key, None)
        self.clear_chat_prep_phase(job_id, username)

    def settle_chat_preparation(
        self,
        job_id: str,
        username: str,
        handshake: ChatInitHandshake,
        task: asyncio.Task[Any],
    ) -> None:
        """Settle every exit a preparation can take, then end it."""
        if not handshake.settled.is_set() and not task.cancelled():
            handshake.error = task.exception()
        handshake.settled.set()
        self.end_chat_preparation(job_id, username, task)

    def refuse_while_preparing(self, job_id: str, username: str) -> None:
        """Answer ``425`` while a preparation owns this job's workspace."""
        key = f"{job_id}:{username}"
        if self.chat_prep_tasks.get(key) is None:
            return
        phase = self.live_chat_prep_phase(key)
        label = phase["phase"] + (f" {phase['detail']}" if phase.get("detail") else "")
        raise HTTPException(
            status_code=425, detail=_CHAT_PREPARING_DETAIL.format(phase=label)
        )

    # -- Tool fingerprint tracking --

    @staticmethod
    def chat_tool_fingerprint(
        targets: tuple[str, ...], tools: list[dict[str, Any]]
    ) -> tuple[str, ...]:
        """The creation-time tool set a resumed chat session has to match."""
        if any(tool.get("name", "").startswith("graft_") for tool in tools):
            return (*targets, _GRAPH_TOOL_FINGERPRINT)
        return targets

    def enable_tools_for_session(
        self, session_id: str | None, targets: tuple[str, ...]
    ) -> str | None:
        """The session to resume, or ``None`` when its tools are out of date."""
        if not session_id:
            return None
        known = self.chat_session_tracker_targets.get(session_id)
        if known is None:
            self.chat_session_tracker_targets[session_id] = targets
            return session_id
        if known == targets:
            return session_id
        if (
            _GRAPH_TOOL_FINGERPRINT in known
            and _GRAPH_TOOL_FINGERPRINT not in targets
            and tuple(item for item in known if item != _GRAPH_TOOL_FINGERPRINT)
            == targets
        ):
            logger.info(
                "Chat: no graph tools this turn (%s); keeping session %s rather than "
                "rebuilding it — the graph read is inconclusive while a build runs",
                targets,
                session_id,
            )
            return session_id
        logger.info(
            "Chat: tool set changed (%s -> %s); starting a fresh session instead of "
            "resuming %s",
            known,
            targets,
            session_id,
        )
        return None

    def remember_chat_session_tools(
        self, session_id: str | None, targets: tuple[str, ...]
    ) -> None:
        """Record the tracker/graph tools a fresh chat session was created with."""
        if not session_id:
            return
        if len(self.chat_session_tracker_targets) > 512:
            self.chat_session_tracker_targets.clear()
        self.chat_session_tracker_targets[session_id] = targets

    # -- Workspace helpers & off-turn indexing --

    def build_ci_workspace_params(
        self, decrypted_params: dict[str, Any], result_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Build params dict for CI chat workspace setup from stored request params."""
        return {
            **decrypted_params,
            "job_name": result_data.get("job_name", ""),
            "build_number": self.host.resolve_display_build_id(result_data),
            "analysis_type": decrypted_params.get(
                "analysis_type", result_data.get("analysis_type", "jenkins")
            ),
        }

    def schedule_chat_index(
        self,
        workspace: Path,
        repos: dict[str, Path] | None = None,
        job_id: str = "",
        deadline: float | None = None,
    ) -> None:
        """Start a chat's graph build in the background and return at once."""
        session_tools.schedule_workspace_index(
            workspace, job_id=job_id, repos=repos, deadline=deadline
        )

    @staticmethod
    def chat_workspace_unknown_message(job_id: str, username: str) -> str:
        """What to tell a user whose chat workspace could not be established."""
        return (
            "The chat workspace could not be prepared: "
            f"{chat_engine.get_chat_workspace(job_id, username)} is still in use by a running "
            "workspace build, or a cleanup could not delete all of it. Wait for the "
            "build to finish and try again. If it keeps failing, an administrator has "
            "to remove that directory."
        )

    async def enter_chat_workspace(
        self, job_id: str, username: str, what: str
    ) -> Path | None:
        """Create the chat workspace under its path lock, or ``None`` if it cannot be."""
        try:
            async with chat_engine.chat_workspace_lock(job_id, username):
                return chat_engine.ensure_chat_workspace(job_id, username=username)
        except PathStateUnknown:
            logger.warning(
                "%s blocked by an unestablished workspace for job %s, user %s",
                what,
                job_id,
                username,
            )
            return None

    # -- Session selection, availability, versioning, and history reads --

    async def normalize_and_validate_ai_params(
        self,
        ai_provider: str | None,
        ai_model: str | None,
        *,
        username: str,
    ) -> tuple[str | None, str | None]:
        """Normalize and validate AI provider/model from request body."""
        provider_raw = (ai_provider or "").strip()
        provider = (normalize_provider(provider_raw) or None) if provider_raw else None
        model = (ai_model or "").strip() or None
        if (provider and not model) or (model and not provider):
            raise HTTPException(
                status_code=422,
                detail="Both ai_provider and ai_model are required when either is set",
            )
        if provider and model:
            ai_client._selected_credential_source.set("")
            user_token = ai_username.set(username)
            try:
                return await self.host.validate_catalog_pair(provider, model)
            finally:
                ai_username.reset(user_token)
        return provider, model

    async def validate_chat_selection(
        self, body: ChatInitRequest, username: str
    ) -> tuple[str, str]:
        """Validate the chosen pair as this user, never as the analysis job."""
        provider, model = await self._call_normalize_and_validate_ai_params(
            body.ai_provider,
            body.ai_model,
            username=username,
        )
        if not provider or not model:
            raise HTTPException(
                status_code=422, detail="Select an AI provider and model"
            )
        source = ai_client._selected_credential_source.get()
        if source == "server":
            token = ai_username.set(username)
            try:
                await ai_client.require_server_provider_grant(provider, model)
            except ai_client.InactiveServerProviderError as exc:
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

    async def available_chat_session(
        self, job_id: str, username: str, session: Any = _UNSET_SESSION
    ) -> dict[str, str] | None:
        """Describe only a session the current owner can still use."""
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
                token = ai_username.set(username)
                try:
                    await ai_client.require_server_provider_grant(
                        active["ai_provider"], active["ai_model"]
                    )
                except ValueError:
                    return None
                finally:
                    ai_username.reset(token)
            elif source == "user":
                if (
                    await ai_client.provider_accepts_user_keys(active["ai_provider"])
                    is False
                ):
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

    async def current_chat_choice(
        self, job_id: str, username: str
    ) -> tuple[dict[str, Any], str]:
        """Load the selected session and its persisted credential source."""
        active = await storage.get_latest_chat_session(job_id, username)
        if not active:
            raise HTTPException(
                status_code=409, detail="Start a new chat session first"
            )
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
        return active, source

    async def check_chat_session_choice(
        self,
        job_id: str,
        username: str,
        provider: str,
        model: str,
        source: str | None,
        *,
        allow_empty: bool = False,
    ) -> tuple[str, str]:
        """Pin chat turns to the last Start's owner, model and credential source."""
        if allow_empty and not await self.available_chat_session(job_id, username):
            return "", ""
        active, active_source = await self.current_chat_choice(job_id, username)
        if (provider, model) != (active["ai_provider"], active["ai_model"]) or (
            source is not None and source != active_source
        ):
            raise HTTPException(
                status_code=409,
                detail=_CHAT_PAIR_CONFLICT_DETAIL,
            )
        return active["session_id"], active_source

    async def resolve_chat_credentials(
        self, decrypted_params: dict[str, Any], username: str
    ) -> tuple[str, str, JiraConnection | None, str, str]:
        """Resolve user-scoped Jira and GitHub credentials for chat."""
        settings = self.host.get_settings()
        user_tokens = await storage.get_user_tokens(username)

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
                jira_connection = _JIRA_CHAT_SELECTION_FAILED
            else:
                if jira_connection.mode in ("sa", "atst"):
                    try:
                        jira_connection = await complete_jira_connection(
                            jira_connection
                        )
                    except JiraOAuthMintError as exc:
                        logger.warning(
                            "Chat credentials: %s; Jira tools unavailable", exc
                        )
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

    @staticmethod
    def chat_session_version(job_id: str, username: str, session_id: str | None) -> str:
        """Opaque, scoped version for conditional chat clears; never expose session IDs."""
        payload = json.dumps([job_id, username, session_id], separators=(",", ":"))
        return hmac.new(
            get_hmac_secret().encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    async def current_chat_session_version(self, job_id: str, username: str) -> str:
        active = await storage.get_latest_chat_session(job_id, username)
        return self.chat_session_version(
            job_id, username, active["session_id"] if active else None
        )

    async def require_chat_clear_version(
        self, job_id: str, username: str, supplied: str
    ) -> None:
        """Call while holding the per-user chat lock shared with Start."""
        current = await self.current_chat_session_version(job_id, username)
        if not supplied or not hmac.compare_digest(supplied, current):
            logger.info("Chat clear conflict for job %s, user %s", job_id, username)
            raise HTTPException(
                status_code=409,
                detail="Chat session changed; refresh history before clearing",
            )

    async def read_chat_history_payload(
        self, job_id: str, username: str, *, limit: int, offset: int
    ) -> dict[str, Any]:
        """Read chat history from one SQLite WAL snapshot without blocking on chat locks.

        Active turns and workspace preparations hold ``hold_chat_lock`` across
        multi-minute AI calls and git clones. Acquiring that lock here blocked
        ``GET /api/chat/{job_id}`` and ``GET /api/admin/chat`` until reverse
        proxies returned 504 Gateway Time-out. ``get_chat_history_snapshot``
        pins a single WAL read transaction so messages, total, active_session,
        and version stay consistent while reads return immediately.
        """
        messages, total, session = await storage.get_chat_history_snapshot(
            job_id, limit=limit, offset=offset, username=username
        )
        # Filter out hidden init messages (empty content + completed status,
        # used for session_id storage). Keep pending/failed rows even if empty.
        messages = [
            {k: v for k, v in m.items() if k != "session_id"}
            for m in messages
            if m.get("content") or m.get("status") in ("pending", "failed")
        ]
        active = await self.available_chat_session(job_id, username, session=session)
        version = self.chat_session_version(
            job_id, username, session["session_id"] if session else None
        )
        return {
            "messages": messages,
            "total": total,
            "active_session": active,
            "active_session_version": version,
        }

    # -- Failure rows, warnings, and session revocation --

    async def fail_chat_assistant_placeholder(
        self,
        assistant_msg_id: int,
        job_id: str,
        username: str,
        content: str,
    ) -> None:
        """Best-effort: mark a pending assistant row failed so it is never left pending."""
        try:
            await storage.update_chat_message_content(assistant_msg_id, content)
            await storage.update_chat_message_status(assistant_msg_id, "failed")
            self._call_notify_chat_changed(job_id, username=username)
        except Exception:
            logger.exception(
                "Failed to update error status for chat msg %d",
                assistant_msg_id,
            )

    async def store_chat_turn_warning(
        self, job_id: str, username: str, warnings: Sequence[str]
    ) -> None:
        """Best-effort: record an accepted-but-incomplete turn as a visible notice."""
        if not warnings:
            return
        try:
            await self.host.post_server_notice(
                conversation=job_id,
                username=username,
                content=(
                    "This reply may be incomplete — the AI provider reported: "
                    + "; ".join(warnings)
                ),
            )
        except Exception:
            logger.exception("Failed to store the turn warning for %s", job_id)

    async def chat_prep_failure_row(
        self, job_id: str, username: str, sentence: str
    ) -> None:
        """One failed assistant row carrying a constant, redacted sentence."""
        try:
            await storage.add_chat_message(
                job_id=job_id,
                role="assistant",
                content=sentence,
                username=username,
                status="failed",
            )
        except Exception:
            logger.exception(
                "Chat preparation could not record its failed row for job %s, user %s",
                job_id,
                username,
            )

    async def discard_unsaved_chat_response(
        self,
        message_id: int,
        job_id: str,
        username: str,
        provider: str,
        session_id: str | None,
        label: str,
    ) -> None:
        """Revoke an unsaved response and fail only an open placeholder."""
        if session_id:
            await self._call_discard_unsaved_ai_session(session_id, username, provider)
        logger.info(
            "%s: discarded response for message %d (stale credentials or closed placeholder)",
            label,
            message_id,
        )
        if await storage.fail_pending_chat_message(
            message_id, "AI credentials changed during processing. Please try again."
        ):
            self._call_notify_chat_changed(job_id, username=username)

    async def discard_unsaved_ai_session(
        self, session_id: str, username: str, provider: str
    ) -> None:
        """Tombstone an owned unsaved session before best-effort sidecar deletion."""
        if await storage.revoke_ai_session_source(
            session_id, username=username, provider=provider
        ):
            await self._call_revoke_ai_sessions([session_id])

    async def revoke_ai_sessions(self, sessions: list[str]) -> None:
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

    async def cleanup_revoked_ai_sessions(self) -> None:
        """Retry tombstones left by a failed sidecar deletion on the next cleanup."""
        await self._call_revoke_ai_sessions(await storage.list_revoked_ai_sessions())

    # -- Shared credential scoping and session persistence for init & turns --

    @staticmethod
    async def _credential_generation_for_source(
        username: str, ai_provider: str, credential_source: str
    ) -> int | None:
        """Resolve the user's credential generation for user-keyed sessions."""
        if credential_source == "server":
            return None
        return await storage.get_user_ai_credential_generation(username, ai_provider)

    @asynccontextmanager
    async def _scoped_chat_init_context(
        self, username: str, ai_provider: str, credential_source: str
    ) -> AsyncIterator[int | None]:
        """Resolve credential generation and scope ai_username + chat_session_source."""
        credential_generation = await self._credential_generation_for_source(
            username, ai_provider, credential_source
        )
        token = ai_username.set(username)
        source_token = ai_client.chat_session_source.set(credential_source)
        try:
            yield credential_generation
        finally:
            ai_client.chat_session_source.reset(source_token)
            ai_username.reset(token)

    async def _persist_init_session(
        self,
        *,
        conversation_id: str,
        username: str,
        ai_provider: str,
        ai_model: str,
        session_id: str | None,
        credential_generation: int | None,
        tool_fingerprint: tuple[str, ...],
    ) -> str | None:
        if not session_id:
            return session_id
        self.remember_chat_session_tools(session_id, tool_fingerprint)
        saved = await storage.add_chat_message(
            job_id=conversation_id,
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
            await self._call_discard_unsaved_ai_session(
                session_id, username, ai_provider
            )
            return None
        return session_id

    # -- Job chat initialization (async background preparation + handshake) --

    async def init_job_chat_under_barrier(
        self,
        job_id: str,
        username: str,
        body: ChatInitRequest,
        *,
        handshake: ChatInitHandshake | None = None,
    ) -> dict[str, Any]:
        """Run job-chat preflight inline and workspace preparation under the chat lock."""
        self.raise_if_chat_job_deleted(job_id)
        stored = await self.host.get_result(job_id, strip_sensitive=False)
        if not stored or not stored.get("result"):
            raise HTTPException(status_code=404, detail="Job not found")
        result_data = stored["result"]
        params = result_data.get("request_params", {})
        ai_provider, ai_model = await self._call_validate_chat_selection(body, username)
        credential_source = ai_client._selected_credential_source.get()
        existing_session_id, _ = await self._call_check_chat_session_choice(
            job_id,
            username,
            ai_provider,
            ai_model,
            credential_source,
            allow_empty=True,
        )

        self.raise_if_chat_job_deleted(job_id)
        workspace = await self._call_enter_chat_workspace(job_id, username, "Chat init")
        if workspace is None:
            raise HTTPException(
                status_code=409,
                detail=self.chat_workspace_unknown_message(job_id, username),
            )

        decrypted_params: dict[str, Any] = {}
        try:
            decrypted_params = decrypt_sensitive_fields(dict(params))
        except Exception:
            logger.warning(
                "Failed to decrypt request_params for chat init", exc_info=True
            )

        settings = self.host.get_settings()

        (
            jira_email,
            jira_token,
            jira_connection,
            github_token,
            github_repo,
        ) = await self._call_resolve_chat_credentials(decrypted_params, username)

        # Preflight is over: the caller can answer 202, everything below is slow.
        if handshake is not None:
            handshake.started = True
            handshake.settled.set()

        async with self.hold_chat_lock(f"{job_id}:{username}"):
            session_id: str | None = existing_session_id
            self.raise_if_chat_job_deleted(job_id)
            failed_repos: list[str] = []
            await chat_engine.clone_chat_repos(
                workspace,
                decrypted_params,
                user_repo_token=github_token,
                on_repo=lambda name: self._call_set_chat_prep_phase(
                    job_id, username, "cloning", name
                ),
                failed=failed_repos,
            )
            repos_available = bool(
                await asyncio.to_thread(graft_http.cloned_graph_roots, workspace)
            )
            self.raise_if_chat_job_deleted(job_id)
            self._call_set_chat_prep_phase(job_id, username, "fetching_build_data")
            ci_build_data_available = await self.host.setup_ci_workspace(
                workspace,
                self.build_ci_workspace_params(decrypted_params, result_data),
                github_token=github_token,
                settings=settings,
            )
            self.raise_if_chat_job_deleted(job_id)
            if not await self.available_chat_session(job_id, username):
                custom_tools: list[dict[str, Any]] = []
                issue_repos = self.host.job_issue_repo_slugs(decrypted_params)[1]
                tracker_targets, tracker_unavailable = self.host.chat_tracker_state(
                    settings,
                    await storage.get_user_tokens(username),
                    github_repo_available=bool(issue_repos),
                    has_profile=await self.host.account_has_profile(username),
                    job_chat=True,
                )
                auth_header = await self.host.create_ai_auth_header(
                    username, refuse_storage_fault=True
                )
                if auth_header:
                    server_url = self.host.build_internal_server_url()
                    custom_tools = chat_engine.build_chat_custom_tools(
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
                        tracker_targets=tracker_targets,
                        issue_repos=tuple(sorted(issue_repos)),
                    )
                else:
                    logger.warning(
                        "Chat init: internal tools token unavailable for %s — "
                        "the chat starts without internal tools",
                        username,
                    )

                self.raise_if_chat_job_deleted(job_id)
                session = await session_tools.build_session_tools(
                    builtin=list(CHAT_BUILTIN_TOOLS),
                    custom_base=custom_tools,
                    workspace=workspace,
                    job_id=job_id,
                    # The graph is built after this preparation, not before it.
                    wait_for_index=False,
                )
                custom_tools = session.custom
                async with self._scoped_chat_init_context(
                    username, ai_provider, credential_source
                ) as credential_generation:
                    self._call_set_chat_prep_phase(job_id, username, "starting_session")
                    session_id = await chat_engine.init_chat_session(
                        job_id=job_id,
                        job_name=result_data.get("job_name", "unknown"),
                        build_number=self.host.resolve_display_build_id(result_data),
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        repo_path=workspace,
                        custom_tools=custom_tools,
                        repos_available=repos_available,
                        ci_build_data_available=ci_build_data_available,
                        issue_repos=tuple(sorted(issue_repos)),
                        tracker_unavailable=tracker_unavailable,
                    )
                if job_id in self.chat_jobs_deleting and session_id:
                    await self._call_discard_unsaved_ai_session(
                        session_id, username, ai_provider
                    )
                self.raise_if_chat_job_deleted(job_id)
                session_id = await self._persist_init_session(
                    conversation_id=job_id,
                    username=username,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    session_id=session_id,
                    credential_generation=credential_generation,
                    tool_fingerprint=self.chat_tool_fingerprint(
                        tracker_targets, custom_tools
                    ),
                )
                welcome_text = chat_engine.build_welcome_message(
                    job_name=result_data.get("job_name", "unknown"),
                    build_number=self.host.resolve_display_build_id(result_data),
                    repos_available=repos_available,
                    ci_build_data_available=ci_build_data_available,
                    jira_available=bool(jira_connection),
                    github_available=bool(github_token and github_repo),
                )
                if session_id:
                    await storage.delete_chat_welcome_messages(job_id, username)
                    await storage.add_chat_message(
                        job_id=job_id,
                        role="assistant",
                        content=welcome_text,
                        username=username,
                        status="completed",
                    )

        if repos_available:
            self._call_schedule_chat_index(
                workspace,
                job_id=job_id,
                deadline=_time.monotonic() + CHAT_INDEX_DEADLINE_SECONDS,
            )

        if session_id and failed_repos:
            await storage.add_chat_message(
                job_id=job_id,
                role="system",
                content=(
                    "Not in this workspace: "
                    + ", ".join(sorted(failed_repos))
                    + ". The chat still works; start a new session once it can be cloned."
                ),
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
        return {"session_id": session_id or ""}

    async def run_chat_preparation(
        self,
        job_id: str,
        username: str,
        body: ChatInitRequest,
        handshake: ChatInitHandshake,
    ) -> None:
        """Prepare one chat workspace off the request, and report how it ended."""
        notify = False
        async with self.track_chat_job_task(job_id):
            try:
                try:
                    prepared = await self.init_job_chat_under_barrier(
                        job_id, username, body, handshake=handshake
                    )
                    session_id = prepared["session_id"]
                except asyncio.CancelledError:
                    logger.info(
                        "Chat preparation cancelled for job %s, user %s (phase=%s)",
                        job_id,
                        username,
                        self.chat_prep_phases.get(f"{job_id}:{username}", {}).get(
                            "phase", "none"
                        ),
                    )
                    raise
                except HTTPException as exc:
                    if not handshake.settled.is_set():
                        logger.info(
                            "Chat preparation refused before it started for job %s, "
                            "user %s: %s",
                            job_id,
                            username,
                            exc.status_code,
                        )
                        handshake.error = exc
                        handshake.settled.set()
                    elif job_id in self.chat_jobs_deleting:
                        logger.info(
                            "Chat preparation stopped for deleted job %s, user %s",
                            job_id,
                            username,
                        )
                    else:
                        logger.warning(
                            "Chat preparation failed for job %s, user %s: %s",
                            job_id,
                            username,
                            exc.status_code,
                        )
                        await self.chat_prep_failure_row(
                            job_id, username, _CHAT_PREP_FAILED_DETAIL
                        )
                        notify = True
                except SessionCreationRefused as exc:
                    logger.warning(
                        "Chat preparation refused for job %s, user %s: %s",
                        job_id,
                        username,
                        redacted_error(exc),
                    )
                    await self.chat_prep_failure_row(job_id, username, str(exc))
                    notify = True
                except httpx.HTTPStatusError as exc:
                    await self.chat_prep_failure_row(
                        job_id, username, chat_start_sidecar_error(exc).detail
                    )
                    notify = True
                except Exception as exc:  # noqa: BLE001 - report, never escape silently
                    if not handshake.settled.is_set():
                        handshake.error = exc
                        handshake.settled.set()
                    else:
                        logger.warning(
                            "Chat preparation failed for job %s, user %s: %s",
                            job_id,
                            username,
                            redacted_error(exc),
                        )
                        await self.chat_prep_failure_row(
                            job_id, username, _CHAT_PREP_FAILED_DETAIL
                        )
                        notify = True
                else:
                    if not session_id:
                        logger.warning(
                            "Chat preparation for job %s, user %s produced no session",
                            job_id,
                            username,
                        )
                        await self.chat_prep_failure_row(
                            job_id, username, _CHAT_PREP_FAILED_DETAIL
                        )
                    notify = True
            finally:
                self.end_chat_preparation(job_id, username, asyncio.current_task())
        if notify:
            self._call_notify_chat_changed(job_id, username=username)

    async def start_job_chat_init(
        self,
        job_id: str,
        username: str,
        body: ChatInitRequest,
    ) -> dict[str, Any]:
        """Start preparing a job chat workspace in the background with handshake."""
        key = f"{job_id}:{username}"
        pair = (ai_client.public_provider_name(body.ai_provider), body.ai_model)
        live = self.chat_prep_tasks.get(key)
        if live is not None and not live.task.done():
            if live.pair != pair:
                raise HTTPException(status_code=409, detail=_CHAT_PAIR_CONFLICT_DETAIL)
            return {"ready": False, "preparing": self.live_chat_prep_phase(key)}

        self._call_set_chat_prep_phase(job_id, username, "preparing")
        seeded = self.live_chat_prep_phase(key)
        handshake = ChatInitHandshake()
        task = self.host.spawn_background(
            self.run_chat_preparation(job_id, username, body, handshake)
        )
        self.chat_prep_tasks[key] = ChatPreparation(pair=pair, task=task)
        task.add_done_callback(
            partial(self.settle_chat_preparation, job_id, username, handshake)
        )

        await handshake.settled.wait()
        if handshake.error is not None:
            if isinstance(handshake.error, HTTPException):
                raise handshake.error
            logger.warning(
                "Chat init failed before preparation for job %s, user %s: %s",
                job_id,
                username,
                redacted_error(handshake.error),
            )
            raise HTTPException(status_code=500, detail=_CHAT_PREP_FAILED_DETAIL)
        if not handshake.started:
            if job_id in self.chat_jobs_deleting:
                raise HTTPException(status_code=404, detail="Job not found")
            raise HTTPException(status_code=503, detail=_CHAT_PREP_CANCELLED_DETAIL)
        return {"ready": False, "preparing": seeded}

    # -- Admin chat initialization (synchronous under chat lock) --

    async def init_admin_chat(
        self, username: str, body: ChatInitRequest
    ) -> dict[str, Any]:
        """Initialize admin chat workspace and AI session."""
        session_id: str | None = ""
        async with self.hold_chat_lock(f"{ADMIN_CHAT_JOB_ID}:{username}"):
            ai_provider, ai_model = await self._call_validate_chat_selection(
                body, username
            )
            credential_source = ai_client._selected_credential_source.get()
            session_id, _ = await self._call_check_chat_session_choice(
                ADMIN_CHAT_JOB_ID,
                username,
                ai_provider,
                ai_model,
                credential_source,
                allow_empty=True,
            )
            workspace = await self._call_enter_chat_workspace(
                ADMIN_CHAT_JOB_ID, username, "Admin chat init"
            )
            if workspace is None:
                raise HTTPException(
                    status_code=409,
                    detail=self.chat_workspace_unknown_message(
                        ADMIN_CHAT_JOB_ID, username
                    ),
                )
            if not await self.available_chat_session(ADMIN_CHAT_JOB_ID, username):
                custom_tools: list[dict[str, Any]] = []
                settings = self.host.get_settings()
                tracker_targets, tracker_unavailable = self.host.chat_tracker_state(
                    settings,
                    await storage.get_user_tokens(username),
                    github_repo_available=self.host.settings_github_repo_available(
                        settings
                    ),
                    has_profile=await self.host.account_has_profile(username),
                )
                try:
                    auth_header = await self.host.create_ai_auth_header(
                        username, is_admin=True, refuse_storage_fault=True
                    )
                except Exception as exc:  # noqa: BLE001 - a fault, never a lock
                    logger.warning(
                        "Admin chat init: internal tools token failed for %s: %s",
                        username,
                        redacted_error(exc),
                    )
                    raise HTTPException(
                        status_code=500, detail=_CHAT_PREP_FAILED_DETAIL
                    ) from None
                if auth_header:
                    server_url = self.host.build_internal_server_url()
                    custom_tools = chat_engine.build_admin_custom_tools(
                        server_url=server_url,
                        auth_token=auth_header.removeprefix("Bearer ").strip(),
                        tracker_targets=tracker_targets,
                        conversation=ADMIN_CHAT_JOB_ID,
                    )
                else:
                    logger.warning("Admin chat init: no auth token for %s", username)
                    if not await self.host.ai_admin_role_current(username):
                        raise HTTPException(
                            status_code=403, detail="Admin access required"
                        )

                async with self._scoped_chat_init_context(
                    username, ai_provider, credential_source
                ) as credential_generation:
                    try:
                        session_id = await chat_engine.init_admin_chat_session(
                            ai_provider=ai_provider,
                            ai_model=ai_model,
                            repo_path=workspace,
                            custom_tools=custom_tools,
                            tracker_unavailable=tracker_unavailable,
                        )
                    except SessionCreationRefused as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from None
                    except httpx.HTTPStatusError as exc:
                        raise chat_start_sidecar_error(exc) from None
                session_id = await self._persist_init_session(
                    conversation_id=ADMIN_CHAT_JOB_ID,
                    username=username,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    session_id=session_id,
                    credential_generation=credential_generation,
                    tool_fingerprint=tracker_targets,
                )
        logger.info("Admin chat init: workspace=%s, session=%s", workspace, session_id)
        if session_id:
            self._call_notify_chat_changed(ADMIN_CHAT_JOB_ID, username=username)
        return {"ready": True, "session_id": session_id or ""}

    # -- Unified abort --

    async def abort_chat_turn(
        self, job_id: str, username: str, *, admin: bool = False
    ) -> int:
        """Abort one user's live turn — shared by both job chat and admin chat."""
        self.get_chat_abort_signal(f"{job_id}:{username}").set()

        pending = await storage.get_pending_chat_messages(job_id, username=username)
        for msg in pending:
            await self.fail_chat_assistant_placeholder(
                msg["id"], job_id, username, "Aborted by user."
            )

        target_session_id = None
        for msg in pending:
            if msg.get("session_id"):
                target_session_id = msg["session_id"]
                break
        if not target_session_id:
            active = await storage.get_latest_chat_session(job_id, username)
            if active:
                target_session_id = active["session_id"]
        if not target_session_id:
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
            try:
                from pi_sidecar_client import get_sidecar_client

                client = get_sidecar_client()
                await client.abort(target_session_id)
                if admin:
                    logger.debug("Admin chat: aborted sidecar session")
                else:
                    logger.debug("Chat: aborted sidecar session for job %s", job_id)
            except Exception:
                logger.debug("Chat: sidecar abort best-effort failed", exc_info=True)
        return len(pending)

    # -- Unified message enqueue --

    async def enqueue_message(
        self,
        adapter: ChatModeAdapter,
        *,
        body: ChatMessageRequest,
        username: str,
        is_admin: bool,
        background_tasks: BackgroundTasks,
        background_runner: Callable[..., Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Queue a chat message and schedule background turn execution."""
        conversation_id = adapter.conversation_id
        adapter.before_enqueue(self, username)
        async with self.hold_chat_lock(f"{conversation_id}:{username}"):
            if bool(body.ai_provider) != bool(body.ai_model):
                raise HTTPException(
                    status_code=422, detail="Select both provider and model"
                )
            session, _ = await self._call_current_chat_choice(conversation_id, username)
            ai_provider, ai_model = await self._call_normalize_and_validate_ai_params(
                body.ai_provider or session["ai_provider"],
                body.ai_model or session["ai_model"],
                username=username,
            )
            if ai_provider is None or ai_model is None:
                raise HTTPException(
                    status_code=422, detail="Select both provider and model"
                )
            credential_source = ai_client._selected_credential_source.get()
            await self._call_check_chat_session_choice(
                conversation_id,
                username,
                ai_provider,
                ai_model,
                credential_source,
            )
            notice = await adapter.issue_blocked_notice(
                self, message=body.message, username=username
            )
            user_msg_id, assistant_msg_id = await storage.add_chat_message_pair(
                job_id=conversation_id,
                user_content=body.message,
                username=username,
                ai_provider=ai_provider,
                ai_model=ai_model,
                notice=notice,
            )

        if adapter.is_admin_mode:
            logger.info("Admin chat: queued user message %d", user_msg_id)
        else:
            logger.info(
                "Chat: queued user message %d for job %s", user_msg_id, conversation_id
            )

        self._call_notify_chat_changed(conversation_id, username=username)

        if background_runner is not None:
            background_tasks.add_task(
                background_runner,
                user_msg_id=user_msg_id,
                assistant_msg_id=assistant_msg_id,
                message=body.message,
                ai_provider_override=ai_provider,
                ai_model_override=ai_model,
                username=username,
                is_admin=is_admin,
                credential_source=credential_source,
            )
        else:
            background_tasks.add_task(
                self.process_turn,
                adapter,
                user_msg_id=user_msg_id,
                assistant_msg_id=assistant_msg_id,
                message=body.message,
                ai_provider_override=ai_provider,
                ai_model_override=ai_model,
                username=username,
                is_admin=is_admin,
                credential_source=credential_source,
            )

        return {
            "user_message": {
                "id": user_msg_id,
                "role": "user",
                "content": body.message,
                "username": username,
                "status": "completed",
            },
            "assistant_message_id": assistant_msg_id,
        }

    # -- Unified turn execution (job chat + admin chat) --

    async def process_turn(
        self,
        adapter: ChatModeAdapter,
        *,
        user_msg_id: int,
        assistant_msg_id: int,
        message: str,
        ai_provider_override: str | None,
        ai_model_override: str | None,
        username: str,
        is_admin: bool = False,
        credential_source: str | None = None,
    ) -> None:
        """Execute one chat turn end-to-end for either job chat or admin chat."""
        conversation_id = adapter.conversation_id
        lock_key = f"{conversation_id}:{username}"
        label = adapter.log_label
        effective_admin = is_admin or adapter.is_admin_mode
        user_token = ai_username.set(username)
        source_token = None

        async with adapter.track_turn(self):
            try:
                async with self.hold_chat_lock(lock_key):
                    try:
                        async with adapter.setup_barrier(self):
                            deleted_msg = adapter.check_deleted_message(self)
                            if deleted_msg is not None:
                                logger.info(
                                    "%s: skip processing for deleted job %s user %s",
                                    label,
                                    conversation_id,
                                    username,
                                )
                                await self.fail_chat_assistant_placeholder(
                                    assistant_msg_id,
                                    conversation_id,
                                    username,
                                    deleted_msg,
                                )
                                return

                            context = await adapter.load_turn_context(self)

                            ai_provider, ai_model = (
                                ai_provider_override,
                                ai_model_override,
                            )
                            if not ai_provider or not ai_model:
                                raise ValueError("Chat selection missing")
                            (
                                session_id,
                                credential_source,
                            ) = await self._call_check_chat_session_choice(
                                conversation_id,
                                username,
                                ai_provider,
                                ai_model,
                                credential_source,
                            )
                            source_token = ai_client.chat_session_source.set(
                                credential_source
                            )

                            credential_generation = (
                                await self._credential_generation_for_source(
                                    username, ai_provider, credential_source
                                )
                            )

                            msg_count = await storage.count_chat_messages(
                                conversation_id, username=username
                            )
                            all_history = await storage.get_chat_messages(
                                conversation_id,
                                username=username,
                                limit=max(msg_count, 1),
                            )
                            history = [
                                m
                                for m in all_history
                                if m.get("status") != "pending" and m.get("content")
                            ]

                            # Scan all_history (not filtered history) because the
                            # init message has empty content but carries session_id.
                            last_session_id: str | None = session_id
                            for msg in reversed(all_history):
                                if (
                                    msg.get("role") == "assistant"
                                    and msg.get("session_id")
                                    and msg.get("ai_provider") == ai_provider
                                    and msg.get("ai_model") == ai_model
                                ):
                                    last_session_id = msg["session_id"]
                                    logger.debug(
                                        "%s: found session %s from history (provider=%s, model=%s)",
                                        label,
                                        last_session_id,
                                        ai_provider,
                                        ai_model,
                                    )
                                    break

                            workspace = await self._call_enter_chat_workspace(
                                conversation_id, username, f"{label} message"
                            )
                            if workspace is None:
                                await self.fail_chat_assistant_placeholder(
                                    assistant_msg_id,
                                    conversation_id,
                                    username,
                                    f"Error: {self.chat_workspace_unknown_message(conversation_id, username)}",
                                )
                                return

                            prepared = await adapter.prepare_turn(
                                self,
                                workspace=workspace,
                                username=username,
                                context=context,
                            )
                            if prepared.refusal_message is not None:
                                await self.fail_chat_assistant_placeholder(
                                    assistant_msg_id,
                                    conversation_id,
                                    username,
                                    prepared.refusal_message,
                                )
                                return

                            # A resumed session keeps its creation-time tools, so
                            # rebuild when the tool/graph fingerprint changed.
                            last_session_id = self.enable_tools_for_session(
                                last_session_id, prepared.tool_fingerprint
                            )

                            abort_signal = self.get_chat_abort_signal(lock_key)
                            if abort_signal.is_set():
                                abort_signal.clear()
                                await self.fail_chat_assistant_placeholder(
                                    assistant_msg_id,
                                    conversation_id,
                                    username,
                                    "Aborted by user.",
                                )
                                logger.info(
                                    "%s: aborted before AI call for %s, user %s",
                                    label,
                                    conversation_id,
                                    username,
                                )
                                return

                        # Lifecycle barrier is released before the AI call so job
                        # deletion is not blocked behind AI timeouts.
                        deleted_msg = adapter.check_deleted_message(self)
                        if deleted_msg is not None:
                            await self.fail_chat_assistant_placeholder(
                                assistant_msg_id,
                                conversation_id,
                                username,
                                deleted_msg,
                            )
                            return

                        turn_warnings: list[str] = []
                        turn_started = _time.monotonic()
                        success, response_text, new_session_id = await adapter.call_ai(
                            prepared,
                            message=message,
                            history=history,
                            ai_provider=ai_provider,
                            ai_model=ai_model,
                            workspace=workspace,
                            session_id=last_session_id,
                            on_delta=self.chat_delta_publisher(
                                conversation_id, username, assistant_msg_id
                            ),
                            on_warning=turn_warnings.append,
                        )
                        turn_elapsed = _time.monotonic() - turn_started

                        if abort_signal.is_set():
                            abort_signal.clear()
                            if new_session_id and new_session_id != last_session_id:
                                await self._call_discard_unsaved_ai_session(
                                    new_session_id, username, ai_provider
                                )
                            await self.fail_chat_assistant_placeholder(
                                assistant_msg_id,
                                conversation_id,
                                username,
                                "Aborted by user.",
                            )
                            logger.info(
                                "%s: aborted after AI call for %s, user %s",
                                label,
                                conversation_id,
                                username,
                            )
                            return

                        if not success:
                            logger.error(
                                "%s AI call failed for %s (provider=%s, model=%s, elapsed=%.1fs): %s",
                                label,
                                conversation_id,
                                ai_provider,
                                ai_model,
                                turn_elapsed,
                                response_text,
                            )
                            user_error = format_chat_ai_user_error(
                                response_text,
                                is_admin=effective_admin,
                                ai_provider=ai_provider,
                                ai_model=ai_model,
                            )
                            await self.fail_chat_assistant_placeholder(
                                assistant_msg_id,
                                conversation_id,
                                username,
                                f"Error: {user_error}",
                            )
                            return

                        post_access_error = await adapter.verify_post_ai_access(
                            self, username
                        )
                        if post_access_error is not None:
                            if new_session_id and new_session_id != last_session_id:
                                await self._call_discard_unsaved_ai_session(
                                    new_session_id, username, ai_provider
                                )
                            await self.fail_chat_assistant_placeholder(
                                assistant_msg_id,
                                conversation_id,
                                username,
                                post_access_error,
                            )
                            return

                        if credential_source == "server":
                            # Same predicate Start applied (#582): the full grant,
                            # or the free grant for a pair still declared free.
                            try:
                                await ai_client.require_server_provider_grant(
                                    ai_provider, ai_model
                                )
                            except ValueError as exc:
                                logger.info(
                                    "%s: %s/%s refused on server credentials for user=%s: %s",
                                    label,
                                    ai_provider,
                                    ai_model,
                                    username,
                                    exc,
                                )
                                await self.fail_chat_assistant_placeholder(
                                    assistant_msg_id,
                                    conversation_id,
                                    username,
                                    str(exc),
                                )
                                return

                        current_status = await storage.get_chat_message_status(
                            assistant_msg_id
                        )
                        if current_status == "failed":
                            if new_session_id and new_session_id != last_session_id:
                                await self._call_discard_unsaved_ai_session(
                                    new_session_id, username, ai_provider
                                )
                            logger.info(
                                "Chat: message %d was aborted during processing, discarding response",
                                assistant_msg_id,
                            )
                            return

                        self.remember_chat_session_tools(
                            new_session_id, prepared.tool_fingerprint
                        )
                        saved = await storage.complete_chat_message_if_generation(
                            assistant_msg_id,
                            content=response_text,
                            ai_provider=ai_provider,
                            ai_model=ai_model,
                            session_id=new_session_id or "",
                            credential_generation=credential_generation,
                            server_source=credential_source == "server",
                        )
                        if not saved:
                            await self.discard_unsaved_chat_response(
                                assistant_msg_id,
                                conversation_id,
                                username,
                                ai_provider,
                                new_session_id
                                if new_session_id != last_session_id
                                else None,
                                label,
                            )
                            return

                        logger.info(
                            "%s: message %d processed for %s (session=%s, provider=%s, model=%s, elapsed=%.1fs)",
                            label,
                            assistant_msg_id,
                            conversation_id,
                            new_session_id,
                            ai_provider,
                            ai_model,
                            turn_elapsed,
                        )
                        await self.store_chat_turn_warning(
                            conversation_id, username, turn_warnings
                        )
                        adapter.after_turn_completed(self, workspace)
                        self._call_notify_chat_changed(
                            conversation_id, username=username
                        )

                    except Exception:
                        logger.exception(
                            "%s processing failed for %s, msg %d",
                            label,
                            conversation_id,
                            assistant_msg_id,
                        )
                        await self.fail_chat_assistant_placeholder(
                            assistant_msg_id,
                            conversation_id,
                            username,
                            "An error occurred while processing your message. Please try again.",
                        )
            except asyncio.CancelledError:
                await self.fail_chat_assistant_placeholder(
                    assistant_msg_id,
                    conversation_id,
                    username,
                    "Chat processing was cancelled.",
                )
                raise
            finally:
                if source_token is not None:
                    ai_client.chat_session_source.reset(source_token)
                ai_username.reset(user_token)
                self.cleanup_chat_state(lock_key)
                adapter.cleanup_after_turn(self)
                # Do NOT revoke auth_header — it's embedded in custom tool HTTP
                # headers and must stay alive for the sidecar session lifetime.

    # -- Unified clear history --

    async def clear_history(
        self,
        adapter: ChatModeAdapter,
        *,
        username: str,
        if_match: str | Callable[[], str],
    ) -> dict[str, Any]:
        """Clear chat history, revoke tombstoned sessions, and clean workspaces."""
        conversation_id = adapter.conversation_id
        lock_key = f"{conversation_id}:{username}"
        pre_clear_state = await adapter.before_clear(self, username)

        async with self.hold_chat_lock(lock_key):
            supplied = if_match() if callable(if_match) else if_match
            await self.require_chat_clear_version(conversation_id, username, supplied)
            count = await storage.delete_chat_messages(
                conversation_id, username=username
            )
            await self.cleanup_revoked_ai_sessions()
            self._call_notify_chat_changed(conversation_id, username=username)
            await self.host.cleanup_path_off_loop(
                conversation_id,
                chat_engine.get_chat_workspace(conversation_id, username),
                partial(
                    chat_engine.cleanup_chat_repos, conversation_id, username=username
                ),
            )

        await adapter.after_clear(self, username, pre_clear_state)
        self.cleanup_chat_state(lock_key)
        if adapter.is_admin_mode:
            logger.info("Admin chat: cleared %d messages for user %s", count, username)
        else:
            logger.info(
                "Chat: cleared %d messages for job %s, user %s",
                count,
                conversation_id,
                username,
            )
        return {"deleted": count}
