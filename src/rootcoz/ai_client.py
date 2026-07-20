"""AI client adapter — rootcoz-specific AI setup."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from pi_sidecar_client import AIResult, set_usage_recorder
from pi_sidecar_client import call_ai as _call_ai
from pi_sidecar_client import call_ai_once as _call_ai_once
from pi_sidecar_client import get_sidecar_client
from pi_sidecar_client import list_models as _list_models_raw
from simple_logger.logger import get_logger

logger = get_logger(name=__name__)

# Public provider names only — CLI is a model source under these, not a provider.
VALID_AI_PROVIDERS = {
    "claude",
    "cursor",
    "gemini",
}

# Accepted on input; normalized to the canonical names above.
_LEGACY_PROVIDER_ALIASES: dict[str, str] = {
    "cursor-cli": "cursor",
    "claude-cli": "claude",
    "gemini-cli": "gemini",
}

# Friendly → default sidecar (ACPX / Vertex / Google API)
_DEFAULT_SIDECAR: dict[str, str] = {
    "cursor": "acpx-cursor",
    "claude": "google-vertex-claude",
    "gemini": "google",
}

# Friendly → CLI sidecar (only populated in lists when CLI_AGENTS enables the agent)
_CLI_SIDECAR: dict[str, str] = {
    "cursor": "cli-cursor",
    "claude": "cli-claude",
    "gemini": "cli-gemini",
}

# (friendly_provider, model_id) → sidecar provider id (filled by list_models)
_model_route_cache: dict[tuple[str, str], str] = {}

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
ANALYSIS_BUILTIN_TOOLS: tuple[str, ...] = (*_FS_BROWSE_TOOLS, "subagent")
# Prompt wording for cloned repos — must match tool policy (no shell/git).
RESOURCE_REPO_BROWSE_HINT = (
    f"browse with {', '.join(_FS_BROWSE_TOOLS[:-1])}, and {_FS_BROWSE_TOOLS[-1]} only "
    "(no shell, bash, or git commands)"
)


def normalize_provider(provider: str) -> str:
    """Normalize provider name (lowercase + legacy *-cli aliases → canonical)."""
    p = (provider or "").lower().strip()
    return _LEGACY_PROVIDER_ALIASES.get(p, p)


def _source_for_sidecar(sidecar_provider: str) -> str:
    if sidecar_provider.startswith("cli-"):
        return "cli"
    if sidecar_provider.startswith("acpx-"):
        return "acpx"
    return "api"


def _friendly_provider_from_sidecar(provider: str) -> str:
    """Map sidecar provider ids back to rootcoz friendly names for usage logs."""
    if not provider:
        return provider
    if provider.startswith("cli-"):
        agent = provider.removeprefix("cli-")
        return agent if agent in VALID_AI_PROVIDERS else provider
    if provider.startswith("acpx-"):
        agent = provider.removeprefix("acpx-")
        return agent if agent in VALID_AI_PROVIDERS else provider
    reverse = {v: k for k, v in _DEFAULT_SIDECAR.items()}
    return reverse.get(provider, provider)


def _resolve_sidecar_for_model(friendly: str, model: str) -> str:
    """Pick ACPX/API vs CLI sidecar for a friendly provider + model id."""
    cached = _model_route_cache.get((friendly, model))
    if cached:
        return cached

    # Cursor id shapes: ACPX uses bracket params; CLI uses plain cursor:… ids.
    if friendly == "cursor":
        if "[" in model:
            return _DEFAULT_SIDECAR["cursor"]
        if model.startswith("cursor:"):
            return _CLI_SIDECAR["cursor"]
        return _DEFAULT_SIDECAR["cursor"]

    return _DEFAULT_SIDECAR.get(friendly, friendly)


def _map_model_for_sidecar(sidecar_provider: str, model: str) -> str:
    """Ensure cursor models keep the cursor: prefix expected by ACPX/CLI."""
    if (
        sidecar_provider in ("acpx-cursor", "cli-cursor")
        and model
        and not model.startswith("cursor:")
    ):
        return f"cursor:{model}"
    return model


def map_provider_model_for_sidecar(provider: str, model: str) -> tuple[str, str]:
    """Map friendly provider/model to sidecar ids for session create / AI calls."""
    friendly = normalize_provider(provider)
    model = (model or "").strip()
    sidecar_provider = _resolve_sidecar_for_model(friendly, model)
    return sidecar_provider, _map_model_for_sidecar(sidecar_provider, model)


def list_models_from_catalog(friendly: str, all_models: list[dict]) -> list[dict]:
    """Filter a sidecar catalog into one friendly provider's models.

    Updates ``_model_route_cache`` for each ``(friendly, model_id)``.
    Deduplicates by model id (first source wins: default/ACPX/API before CLI).
    """
    friendly = normalize_provider(friendly)
    if friendly not in VALID_AI_PROVIDERS:
        return []

    sidecar_order = [_DEFAULT_SIDECAR[friendly]]
    cli_id = _CLI_SIDECAR.get(friendly)
    if cli_id:
        sidecar_order.append(cli_id)

    result: list[dict] = []
    seen_ids: set[str] = set()
    for sidecar_id in sidecar_order:
        source = _source_for_sidecar(sidecar_id)
        for m in all_models:
            if m.get("provider") != sidecar_id:
                continue
            mid = m.get("id") or ""
            if not mid:
                continue
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            _model_route_cache[(friendly, mid)] = sidecar_id
            result.append(
                {
                    "id": mid,
                    "name": m.get("name") or mid,
                    "provider": friendly,
                    "source": source,
                }
            )
    return result


def build_friendly_catalog(all_models: list[dict]) -> dict[str, list[dict]]:
    """Build per-friendly-provider catalogs from one sidecar ``get_models()`` result."""
    return {
        p: list_models_from_catalog(p, all_models) for p in sorted(VALID_AI_PROVIDERS)
    }


async def list_models(provider: str = "") -> list[dict]:
    """List models for a friendly provider, merging ACPX/API + CLI sources.

    Each entry includes ``source``: ``acpx`` | ``cli`` | ``api``.
    Deduplicates by model id (first source wins: default/ACPX/API before CLI).
    """
    if not provider:
        return await _list_models_raw("")

    friendly = normalize_provider(provider)
    if friendly not in VALID_AI_PROVIDERS:
        return []

    client = get_sidecar_client()
    all_models = await client.get_models()
    return list_models_from_catalog(friendly, all_models)


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
        models = await list_models("cursor")
        model_count = len(models)
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


def format_chat_ai_user_error(response_text: str, *, is_admin: bool = False) -> str:
    """Map raw sidecar/AI errors to user-friendly chat messages.

    Avoid matching the URL path ``/sessions`` as a lost chat session.
    Credential-state hints (whether ``CURSOR_API_KEY`` is set) are admin-only.
    """
    text = (response_text or "").strip()
    lower = text.lower()
    if not text:
        return "AI call failed. Please try again."

    if any(
        s in lower
        for s in (
            "authentication required",
            "not authenticated",
            "not logged in",
            "agent login",
            "cursor_api_key",
        )
    ):
        if not is_admin:
            return "Cursor is unavailable. Contact an administrator."
        has_api_key = bool(os.environ.get("CURSOR_API_KEY", "").strip())
        if has_api_key:
            return _CURSOR_KEY_SET_BUT_UNAVAILABLE_HINT
        return _CURSOR_BROWSER_LOGIN_EXPIRED_HINT

    if "400" in lower and "/sessions" in lower:
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

    # True lost-session cases from sidecar ("session not found"), not URL paths
    if "session not found" in lower or (
        "not found" in lower and "session" in lower and "/sessions" not in lower
    ):
        return "AI session expired. Please try sending your message again."

    return text


async def _prewarm_model_routes(friendly: str) -> None:
    """Best-effort catalog fetch to populate ``_model_route_cache``.

    Failures are non-fatal: ``map_provider_model_for_sidecar`` still has
    heuristic defaults when the cache is empty.
    """
    if not friendly or any(fp == friendly for fp, _ in _model_route_cache):
        return
    try:
        await list_models(friendly)
    except Exception:
        logger.debug(
            "Model catalog prewarm failed for provider=%s; using heuristic routes",
            friendly,
            exc_info=True,
        )


async def call_ai(*args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any):
    """call_ai with rootcoz friendly→sidecar provider/model routing."""
    friendly = normalize_provider(ai_provider)
    await _prewarm_model_routes(friendly)
    sidecar_provider, sidecar_model = map_provider_model_for_sidecar(
        ai_provider, ai_model
    )
    return await _call_ai(
        *args, ai_provider=sidecar_provider, ai_model=sidecar_model, **kwargs
    )


async def call_ai_once(
    *args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any
):
    """call_ai_once with rootcoz friendly→sidecar provider/model routing."""
    friendly = normalize_provider(ai_provider)
    await _prewarm_model_routes(friendly)
    sidecar_provider, sidecar_model = map_provider_model_for_sidecar(
        ai_provider, ai_model
    )
    return await _call_ai_once(
        *args, ai_provider=sidecar_provider, ai_model=sidecar_model, **kwargs
    )


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
            ai_provider=_friendly_provider_from_sidecar(ai_provider),
            ai_model=ai_model,
        )

    set_usage_recorder(_rootcoz_recorder)


__all__ = [
    "AIResult",
    "ANALYSIS_BUILTIN_TOOLS",
    "CHAT_BUILTIN_TOOLS",
    "VALID_AI_PROVIDERS",
    "_setup_usage_recorder",
    "call_ai",
    "call_ai_once",
    "clear_cursor_auth_cache",
    "format_chat_ai_user_error",
    "list_models",
    "map_provider_model_for_sidecar",
    "normalize_provider",
    "probe_cursor_auth",
]
