"""AI client adapter — rootcoz-specific AI setup."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from pi_sidecar_client import AIResult, set_usage_recorder
from pi_sidecar_client import call_ai as _call_ai
from pi_sidecar_client import call_ai_once as _call_ai_once
from pi_sidecar_client import list_models as _list_models_raw
from simple_logger.logger import get_logger

logger = get_logger(name=__name__)

# Pi-sidecar's catalog is the provider/model contract.  These aliases only
# preserve unambiguous legacy spelling; friendly provider names are resolved
# against the selected model below and never choose a default route.
_LEGACY_PROVIDER_ALIASES: dict[str, str] = {
    "cursor-cli": "cli-cursor",
    "claude-cli": "cli-claude",
    "gemini-cli": "cli-gemini",
}

# Cached cursor auth probe: (monotonic_ts, status_dict)
_cursor_auth_cache: tuple[float, dict[str, Any]] | None = None
_CURSOR_AUTH_CACHE_TTL_SEC = 60.0
# Browser `agent login` expires. CURSOR_API_KEY does NOT — when set in env it keeps working.
_CURSOR_BROWSER_LOGIN_EXPIRED_HINT = (
    "Cursor browser login (`agent login`) expired or is missing. "
    "Set CURSOR_API_KEY on the server (does not expire; always works when set), "
    "or re-run `agent login` on the host and restart the sidecar. "
    "Browser login cannot be auto-refreshed."
)
_CURSOR_KEY_SET_BUT_UNAVAILABLE_HINT = (
    "CURSOR_API_KEY is set (that key does not expire) but Cursor models are "
    "unavailable. Check the key is visible to the sidecar process, restart "
    "the sidecar, and verify network to Cursor APIs."
)

# Builtin tools for AI sessions — no bash access (MANDATORY per project rules)
# Separate constants to allow independent evolution: analysis may gain tools
# (e.g. write) that chat should never have.
# Prompts must not claim shell/git — derive browse hint from filesystem tools.
_FS_BROWSE_TOOLS: tuple[str, ...] = ("read", "ls", "find", "grep")
CHAT_BUILTIN_TOOLS: tuple[str, ...] = (*_FS_BROWSE_TOOLS, "subagent")
ANALYSIS_BUILTIN_TOOLS: tuple[str, ...] = _FS_BROWSE_TOOLS
# Prompt wording for cloned repos — must match tool policy (no shell/git).
RESOURCE_REPO_BROWSE_HINT = (
    f"browse with {', '.join(_FS_BROWSE_TOOLS[:-1])}, and {_FS_BROWSE_TOOLS[-1]} only "
    "(no shell or bash execution)"
)


def normalize_provider(provider: str) -> str:
    """Normalize provider name (lowercase + legacy *-cli aliases → canonical)."""
    p = (provider or "").lower().strip()
    return _LEGACY_PROVIDER_ALIASES.get(p, p)


def _source_for_sidecar(provider: str) -> str:
    if provider.startswith("cli-"):
        return "cli"
    if provider.startswith("acpx-"):
        return "acpx"
    return "api"


def is_cursor_provider(provider: str) -> bool:
    """Whether a sidecar provider uses Cursor diagnostics."""
    return provider == "cursor" or provider.endswith("-cursor")


def build_friendly_catalog(
    all_models: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group the sidecar catalog by its exact provider identifiers."""
    result: dict[str, list[dict[str, Any]]] = {}
    for entry in all_models:
        provider, model = entry.get("provider"), entry.get("id")
        if not isinstance(provider, str) or not isinstance(model, str) or not model:
            continue
        result.setdefault(provider, []).append(
            {
                "id": model,
                "name": entry.get("name") or model,
                "provider": provider,
                "source": _source_for_sidecar(provider),
            }
        )
    return result


async def list_models(provider: str = "") -> list[dict[str, Any]]:
    """List sidecar catalog models, optionally for an exact provider ID."""
    catalog = await _list_models_raw("")
    provider = normalize_provider(provider)
    return [
        entry for entry in catalog if not provider or entry.get("provider") == provider
    ]


async def resolve_catalog_pair(provider: str, model: str) -> tuple[str, str]:
    """Validate and return an exact catalog pair, with safe legacy aliases only."""
    provider, model = normalize_provider(provider), (model or "").strip()
    catalog = await _list_models_raw("")
    pairs = {(entry.get("provider"), entry.get("id")) for entry in catalog}
    if (provider, model) in pairs:
        return provider, model

    # Old friendly values can be retained only when this model identifies one
    # catalog route.  In particular, never guess between google and vertex.
    legacy_matches = [p for p, m in pairs if m == model and p and p.endswith(provider)]
    if provider in {"claude", "cursor", "gemini"} and len(legacy_matches) == 1:
        return legacy_matches[0], model
    raise ValueError(f"Unknown Pi-sidecar provider/model pair: {provider}/{model}")


def _parse_agent_status_text(text: str) -> str | None:
    """Return auth reason from `agent status` output, or None if looks OK."""
    lower = text.lower()
    if any(
        s in lower
        for s in (
            "authentication required",
            "not authenticated",
            "not logged in",
            "please run 'agent login'",
            'please run "agent login"',
            "agent login' first",
        )
    ):
        return "auth_expired"
    if "logged in" in lower or "authenticated" in lower:
        return None
    return "unavailable"


async def probe_cursor_auth(
    *, force: bool = False, model_count: int | None = None
) -> dict[str, Any]:
    """Probe Cursor CLI/ACPX auth health for admin UI.

    Browser ``agent login`` expires and cannot be auto-refreshed.
    ``CURSOR_API_KEY`` does **not** expire — when set in the server/sidecar
    env it keeps working. Prefer the API key for Dev/prod.

    Args:
        force: Bypass the in-process probe cache.
        model_count: When provided (e.g. from a concurrent ``list_models``
            call), skip a second sidecar model enumeration.

    Returns dict: ok, reason, hint, has_api_key, model_count.
    """
    global _cursor_auth_cache
    now = time.monotonic()
    if (
        not force
        and model_count is None
        and _cursor_auth_cache is not None
        and (now - _cursor_auth_cache[0]) < _CURSOR_AUTH_CACHE_TTL_SEC
    ):
        return dict(_cursor_auth_cache[1])

    # When the caller already enumerated models, prefer that count over a
    # cached probe that may reflect a previous catalog size.
    if (
        not force
        and model_count is not None
        and _cursor_auth_cache is not None
        and (now - _cursor_auth_cache[0]) < _CURSOR_AUTH_CACHE_TTL_SEC
        and _cursor_auth_cache[1].get("model_count") == model_count
    ):
        return dict(_cursor_auth_cache[1])

    has_api_key = bool(os.environ.get("CURSOR_API_KEY", "").strip())
    if model_count is None:
        models = await list_models()
        model_count = sum(
            is_cursor_provider(str(model.get("provider", ""))) for model in models
        )
    if model_count > 0:
        status: dict[str, Any] = {
            "ok": True,
            "reason": None,
            "hint": None,
            "has_api_key": has_api_key,
            "model_count": model_count,
        }
        _cursor_auth_cache = (now, status)
        return dict(status)

    reason = "no_models"
    try:
        proc = await asyncio.create_subprocess_exec(
            "agent",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            reason = "unavailable"
            logger.warning("Cursor auth probe: agent status timed out")
        else:
            text = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(
                errors="replace"
            )
            parsed = _parse_agent_status_text(text)
            if parsed:
                reason = parsed
            elif proc.returncode not in (0, None):
                reason = "unavailable"
            logger.info(
                "Cursor auth probe: models=0 reason=%s returncode=%s has_api_key=%s",
                reason,
                proc.returncode,
                has_api_key,
            )
    except FileNotFoundError:
        reason = "agent_missing"
        logger.warning("Cursor auth probe: agent binary not found on PATH")
    except Exception:
        reason = "unavailable"
        logger.warning("Cursor auth probe failed", exc_info=True)

    # Empty catalog without API key → browser login likely expired.
    # CURSOR_API_KEY never expires; if key is set, do not label auth_expired.
    if reason == "no_models" and not has_api_key:
        reason = "auth_expired"
    if reason == "auth_expired" and has_api_key:
        reason = "api_key_not_applied"

    if has_api_key:
        hint = _CURSOR_KEY_SET_BUT_UNAVAILABLE_HINT
    else:
        hint = _CURSOR_BROWSER_LOGIN_EXPIRED_HINT

    status = {
        "ok": False,
        "reason": reason,
        "hint": hint,
        "has_api_key": has_api_key,
        "model_count": model_count,
    }
    _cursor_auth_cache = (now, status)
    return dict(status)


def clear_cursor_auth_cache() -> None:
    """Clear cached cursor auth probe (e.g. after model refresh)."""
    global _cursor_auth_cache
    _cursor_auth_cache = None


def format_chat_ai_user_error(
    response_text: str, *, is_admin: bool = False, ai_provider: str = ""
) -> str:
    """Map raw sidecar/AI errors to user-friendly chat messages.

    Avoid matching the URL path ``/sessions`` as a lost chat session.
    Cursor-specific remediation applies only to Cursor sidecar providers
    (or Cursor-only markers like ``agent login`` / ``cursor_api_key`` appear).
    Credential-state hints (whether ``CURSOR_API_KEY`` is set) are admin-only.
    """
    text = (response_text or "").strip()
    lower = text.lower()
    if not text:
        return "AI call failed. Please try again."

    friendly = normalize_provider(ai_provider)
    cursor_only_markers = ("agent login", "cursor_api_key")
    generic_auth_markers = (
        "authentication required",
        "not authenticated",
        "not logged in",
    )
    is_cursor_marker = any(s in lower for s in cursor_only_markers)
    is_generic_auth = any(s in lower for s in generic_auth_markers)

    if is_cursor_marker or (is_generic_auth and is_cursor_provider(friendly)):
        if not is_admin:
            return "Cursor is unavailable. Contact an administrator."
        has_api_key = bool(os.environ.get("CURSOR_API_KEY", "").strip())
        if has_api_key:
            return _CURSOR_KEY_SET_BUT_UNAVAILABLE_HINT
        return _CURSOR_BROWSER_LOGIN_EXPIRED_HINT

    if is_generic_auth:
        label = friendly or "AI"
        if not is_admin:
            return f"{label} authentication failed. Contact an administrator."
        return (
            f"{label} authentication failed. Check provider credentials in "
            "Server Settings → AI."
        )

    if "400" in lower and "/sessions" in lower:
        if is_cursor_provider(friendly):
            if not is_admin:
                return (
                    "Failed to create AI session. Select a valid provider and model, "
                    "or contact an administrator if Cursor stays unavailable."
                )
            return (
                "Failed to create AI session (bad provider/model or Cursor auth). "
                "Select a valid provider and model. If using Cursor without "
                "CURSOR_API_KEY, browser `agent login` may have expired — set "
                "CURSOR_API_KEY (does not expire) or re-login and restart sidecar."
            )
        if not is_admin:
            return (
                "Failed to create AI session. Select a valid provider and model, "
                "or contact an administrator."
            )
        return (
            "Failed to create AI session (bad provider/model or credentials). "
            "Select a valid provider and model in Server Settings → AI."
        )

    # True lost-session cases from sidecar ("session not found"), not URL paths
    if "session not found" in lower or (
        "not found" in lower and "session" in lower and "/sessions" not in lower
    ):
        return "AI session expired. Please try sending your message again."

    return text


async def call_ai(
    *args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any
) -> AIResult:
    """Call Pi-sidecar with a validated, unchanged catalog pair."""
    provider, model = await resolve_catalog_pair(ai_provider, ai_model)
    return await _call_ai(*args, ai_provider=provider, ai_model=model, **kwargs)


async def call_ai_once(
    *args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any
) -> AIResult:
    """Call Pi-sidecar once with a validated, unchanged catalog pair."""
    provider, model = await resolve_catalog_pair(ai_provider, ai_model)
    return await _call_ai_once(*args, ai_provider=provider, ai_model=model, **kwargs)


def _setup_usage_recorder() -> None:
    """Register rootcoz's token tracking as the usage recorder.

    Must be called once at startup (main.py app lifespan).
    The callback maps pi-sidecar's request_id to rootcoz's job_id.
    """

    async def _rootcoz_recorder(
        *,
        request_id: str,
        result: AIResult,
        call_type: str,
        prompt_chars: int = 0,
        ai_provider: str = "",
        ai_model: str = "",
    ) -> None:
        # Late import so test mocks on rootcoz.token_tracking.record_ai_usage
        # are picked up at call time, not registration time.
        from rootcoz.token_tracking import record_ai_usage

        await record_ai_usage(
            job_id=request_id,  # rootcoz uses job_id
            result=result,
            call_type=call_type,
            prompt_chars=prompt_chars,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )

    set_usage_recorder(_rootcoz_recorder)


__all__ = [
    "ANALYSIS_BUILTIN_TOOLS",
    "CHAT_BUILTIN_TOOLS",
    "AIResult",
    "_setup_usage_recorder",
    "call_ai",
    "call_ai_once",
    "clear_cursor_auth_cache",
    "format_chat_ai_user_error",
    "list_models",
    "normalize_provider",
    "probe_cursor_auth",
    "resolve_catalog_pair",
]
