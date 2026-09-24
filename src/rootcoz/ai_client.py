"""AI client adapter — rootcoz-specific AI setup."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pi_sidecar_client import (
    AIResult,
    AITokenUsage,
    SidecarClient,
    _validate_api_key,
    get_sidecar_client,
    set_usage_recorder,
)
from pi_sidecar_client import call_ai as _call_ai
from pi_sidecar_client import call_ai_once as _call_ai_once
from pi_sidecar_client import list_models as _list_models_raw
from simple_logger.logger import get_logger

logger = get_logger(name=__name__)

# Only explicitly initiated AI tasks set this; unrelated request tasks never inherit credentials.
ai_username: ContextVar[str] = ContextVar("ai_username", default="")
force_server_credentials: ContextVar[bool] = ContextVar(
    "force_server_credentials", default=False
)
_selected_credential_source: ContextVar[str] = ContextVar(
    "selected_credential_source", default=""
)
model_listing_status: ContextVar[dict[str, dict[str, bool]] | None] = ContextVar(
    "model_listing_status", default=None
)


async def supported_key_providers() -> list[str]:
    """List registered providers with affirmative session-key capability."""
    try:
        providers = await get_sidecar_client().get_providers()
    except Exception:  # noqa: BLE001 - unavailable discovery must fail closed
        logger.warning("Unable to discover session-key providers")
        return []
    return sorted(
        {
            entry["provider"]
            for entry in providers
            if isinstance(entry.get("provider"), str)
            and entry.get("supportsSessionApiKey") is True
        }
    )


async def session_key(provider: str) -> str | None:
    """Resolve the current initiator's exact provider key; missing means server auth."""
    username = ai_username.get()
    if force_server_credentials.get() or not username:
        return None
    from rootcoz.storage import get_user_ai_credentials

    key = (await get_user_ai_credentials(username)).get(provider)
    if key is not None:
        if provider not in await supported_key_providers():
            raise ValueError("Provider session API-key capability unavailable")
        logger.info("Using user AI credential for provider=%s", provider)
    return key


# Pi-sidecar's catalog is the provider/model contract.  These aliases only
# preserve unambiguous legacy spelling; friendly provider names are resolved
# against the selected model below and never choose a default route.
_LEGACY_PROVIDER_ALIASES: dict[str, str] = {
    "cursor-cli": "cli-cursor",
    "claude-cli": "cli-claude",
    "gemini-cli": "cli-gemini",
}

# Last successful sidecar catalog.  AI calls share this with the catalog API so
# a transient refresh failure never rejects a pair already known to be valid.
_model_catalog_cache: list[dict[str, Any]] | None = None
_model_catalog_generation = 0
_model_catalog_lock = asyncio.Lock()

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


async def _get_model_catalog(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Return the shared successful sidecar catalog, refreshing only when asked."""
    global _model_catalog_cache
    if _model_catalog_cache is not None and not refresh:
        return _model_catalog_cache
    async with _model_catalog_lock:
        if _model_catalog_cache is not None and not refresh:
            return _model_catalog_cache
        generation = _model_catalog_generation
        catalog = await _list_models_raw("")
        if generation != _model_catalog_generation:
            return _model_catalog_cache if _model_catalog_cache is not None else catalog
        _model_catalog_cache = catalog
        logger.debug("Loaded Pi-sidecar model catalog: %d models", len(catalog))
        return catalog


def update_model_catalog(models: list[dict[str, Any]] | None = None) -> None:
    """Replace or clear the successful catalog after model discovery refresh."""
    global _model_catalog_cache, _model_catalog_generation
    _model_catalog_generation += 1
    _model_catalog_cache = models


async def list_models(provider: str = "") -> list[dict[str, Any]]:
    """List sidecar catalog models, optionally for an exact provider ID."""
    catalog = await _get_model_catalog()
    provider = normalize_provider(provider)
    return [
        entry for entry in catalog if not provider or entry.get("provider") == provider
    ]


# Snapshot of @mariozechner/pi-ai 0.84.4 generated model IDs; never key-verified.
_PI_MODEL_SUGGESTIONS: dict[str, list[str]] = json.loads(
    Path(__file__).with_name("pi_model_suggestions.json").read_text()
)


async def models_for_api_key(provider: str, api_key: str) -> dict[str, Any]:
    """Ask Pi for key-scoped models and listing capability; never retain the key."""
    client = get_sidecar_client()
    # The released client has no public keyed discovery method yet. Reuse its
    # configured HTTP transport rather than making a second, unauthenticated client.
    method = getattr(client, "get_models_for_api_key", None)
    if method is not None:
        response = await method(provider, api_key)
    else:
        resp = await client._client.post(
            "/models/for-api-key", json={"provider": provider, "api_key": api_key}
        )
        if resp.status_code >= 400:
            raise RuntimeError("Key-scoped model discovery failed") from None
        response = resp.json()
    if (
        not isinstance(response, dict)
        or type(response.get("modelListingSupported")) is not bool
        or not isinstance(response.get("models"), list)
    ):
        raise ValueError("Invalid key-scoped model discovery response")
    models = response["models"]
    if any(
        not isinstance(m, dict)
        or m.get("provider") != provider
        or not isinstance(m.get("id"), str)
        or not m["id"].strip()
        for m in models
    ):
        raise ValueError("Invalid key-scoped model discovery response")
    if not response["modelListingSupported"] and models:
        raise ValueError("Invalid key-scoped model discovery response")
    return {
        "modelListingSupported": response["modelListingSupported"],
        "models": [
            {"id": m["id"], "name": m.get("name") or m["id"], "provider": provider}
            for m in models
        ],
    }


async def scoped_models() -> dict[str, list[dict[str, Any]]]:
    """Return models usable by the active user or server, with source per pair."""
    catalog = await _get_model_catalog()
    status: dict[str, dict[str, bool]] = {}
    model_listing_status.set(status)
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for provider, entries in build_friendly_catalog(catalog).items():
        for entry in entries:
            pairs[(provider, entry["id"])] = {
                **entry,
                "credential_sources": ["server"],
                "verified": True,
            }
    if ai_username.get() and not force_server_credentials.get():
        from rootcoz.storage import get_user_ai_credentials

        credentials = await get_user_ai_credentials(ai_username.get())
        supported = await supported_key_providers() if credentials else []
        for provider, key in credentials.items():
            if provider not in supported:
                continue
            try:
                discovery = await models_for_api_key(provider, key)
            except Exception:  # noqa: BLE001 - never fall back to server credentials
                logger.warning(
                    "Key-scoped model discovery unavailable for provider=%s", provider
                )
                raise ValueError(
                    f"Key-scoped model discovery failed for {provider}. "
                    "Retry or select server credentials explicitly."
                ) from None
            listing = discovery["modelListingSupported"]
            status[provider] = {"has_api_key": True, "modelListingSupported": listing}
            entries = (
                discovery["models"]
                if listing
                else [
                    {"provider": provider, "id": model, "name": model}
                    for model in _PI_MODEL_SUGGESTIONS.get(provider, ())
                ]
            )
            if not listing:
                pairs.setdefault((provider, ""), {})  # Preserve manual-only provider.
            for entry in entries:
                pair = (provider, entry["id"])
                if pair in pairs:
                    pairs[pair]["credential_sources"].insert(0, "user")
                    pairs[pair]["verified"] = listing
                else:
                    pairs[pair] = {
                        **entry,
                        "source": _source_for_sidecar(provider),
                        "credential_sources": ["user"],
                        "verified": listing,
                    }
    result: dict[str, list[dict[str, Any]]] = {}
    for (provider, model), entry in pairs.items():
        bucket = result.setdefault(provider, [])
        if model:
            bucket.append(entry)
    return result


async def resolve_catalog_pair(provider: str, model: str) -> tuple[str, str]:
    """Validate an exact pair, retaining only unambiguous legacy routes."""
    provider, model = normalize_provider(provider), (model or "").strip()
    if ai_username.get():
        scoped = await scoped_models()
        status = model_listing_status.get() or {}
        for entry in scoped.get(provider, []):
            if entry["id"] == model and (
                entry["verified"] or "server" in entry["credential_sources"]
            ):
                source = (
                    "server"
                    if force_server_credentials.get()
                    else entry["credential_sources"][0]
                )
                _selected_credential_source.set(source)
                return provider, model
        if provider in status and status[provider]["modelListingSupported"]:
            raise ValueError(
                f"Unknown Pi-sidecar provider/model pair: {provider}/{model}"
            )
        # Manual IDs require an affirmative no-listing response for this key.
        if (
            model
            and provider in status
            and not status[provider]["modelListingSupported"]
        ):
            _selected_credential_source.set("user")
            return provider, model
        raise ValueError(f"Unknown Pi-sidecar provider/model pair: {provider}/{model}")
    catalog = await _get_model_catalog()
    pairs = {(entry.get("provider"), entry.get("id")) for entry in catalog}
    if (provider, model) in pairs:
        _selected_credential_source.set("server")
        return provider, model

    # A stale catalog may not yet contain a newly discovered pair. Refresh once;
    # a warm catalog remains usable if that refresh is temporarily unavailable.
    catalog = await _get_model_catalog(refresh=True)
    pairs = {(entry.get("provider"), entry.get("id")) for entry in catalog}
    if (provider, model) in pairs:
        _selected_credential_source.set("server")
        return provider, model

    # Legacy friendly IDs have one explicit destination each.  Mapping only when
    # precisely one catalog provider has this model prevents google/Vertex guesses.
    targets = {
        "gemini": lambda p: p == "google",
        "claude": lambda p: p == "google-vertex-claude",
        "cursor": lambda p: p.endswith("-cursor"),
    }
    target = targets.get(provider)
    providers_for_model = {p for p, m in pairs if p and m == model}
    matches = [p for p in providers_for_model if target and target(p)]
    if len(providers_for_model) == 1 and len(matches) == 1:
        _selected_credential_source.set("server")
        return matches[0], model
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


def _redact_key_echo(text: str | None, key: str) -> str | None:
    """Remove predictable renderings of a key from successful AI output."""
    if text is None or not key:
        return text
    encoded = key.encode()
    variants = {
        key,
        quote(key, safe=""),
        quote(key, safe="").lower(),
        json.dumps(key, ensure_ascii=True)[1:-1],
        base64.b64encode(encoded).decode(),
        base64.urlsafe_b64encode(encoded).decode(),
    }
    variants |= {v.rstrip("=") for v in variants if v.endswith("=")}
    variants |= {v.replace("/", "\\/") for v in variants if "/" in v}
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


async def _call_with_safe_error(
    sidecar_call: Any, *args: Any, redact_key: str | None = None, **kwargs: Any
) -> AIResult:
    """Keep sidecar credential-bearing errors out of caller logs and tracebacks."""
    try:
        result = await sidecar_call(*args, **kwargs)
        if redact_key is None:
            return result
        if result.success:
            return replace(
                result,
                text=_redact_key_echo(result.text, redact_key),
                error=_redact_key_echo(result.error, redact_key),
            )
        # Encoded keys (URL, JSON, base64) and rotated keys cannot be safely
        # removed by literal replacement. Keep only fixed retry/auth signals.
        detail = f"{result.text} {result.error or ''}".lower()
        text = (
            "session not found"
            if "session not found" in detail
            else "authentication required"
            if any(
                marker in detail
                for marker in (
                    "authentication required",
                    "not authenticated",
                    "not logged in",
                )
            )
            else "AI call failed"
        )
        return replace(result, text=text, error=text if result.error else None)
    except Exception as exc:
        if redact_key is None:
            raise
        # Raise outside the except block; __context__ would retain the raw error.
        safe_type = (
            type(exc)
            if type(exc) in (ValueError, RuntimeError, OSError, TimeoutError, TypeError)
            else RuntimeError
        )
        safe_text = f"{type(exc).__name__}: sidecar call failed"
    raise safe_type(safe_text)


async def create_session_safely(client: SidecarClient, **kwargs: Any) -> str:
    """Create keyed sessions without upstream logging untrusted HTTP errors."""
    key = kwargs.get("api_key")
    if key is None:
        from rootcoz.storage import create_ai_session_with_source

        return await create_ai_session_with_source(
            lambda: client.create_session(**kwargs),
            ai_username.get(),
            kwargs["provider"],
            "server",
        )
    if error := _validate_api_key(key):
        raise ValueError(error)
    # Keyed sessions use the exact registered provider, never a friendly alias
    # that could route the credential to a different endpoint.
    provider, model = kwargs["provider"], kwargs["model"]
    body = {
        "provider": provider,
        "model": model,
        "system_prompt": kwargs["system_prompt"],
        "cwd": kwargs.get("cwd") or tempfile.gettempdir(),
        "api_key": key,
    }
    for field in ("agent_dir", "custom_tools", "tools"):
        if kwargs.get(field) is not None:
            body[field] = kwargs[field]
    from rootcoz.storage import create_ai_session_with_source

    async def create() -> str:
        response = await client._client.post("/sessions", json=body)
        response.raise_for_status()
        return response.json()["session_id"]

    return await create_ai_session_with_source(
        create, ai_username.get(), provider, "user"
    )


async def _remember_session_source(session_id: str, provider: str, source: str) -> None:
    """Persist ownership before a new session can receive a prompt."""
    try:
        from rootcoz.storage import save_ai_session_source

        await save_ai_session_source(session_id, ai_username.get(), provider, source)
    except Exception:
        logger.warning("Unable to save AI session source")
        raise


async def delete_ai_session(client: SidecarClient, session_id: str) -> None:
    """Delete a sidecar session and invalidate its local ownership record."""
    await client.delete_session(session_id)
    from rootcoz.storage import revoke_ai_session_source

    try:
        await revoke_ai_session_source(session_id)
    except Exception:  # noqa: BLE001 - stale mapping denies reuse until replaced
        logger.warning("Unable to revoke AI session mapping")


async def _prompt_safely(
    client: SidecarClient, session_id: str, message: str, timeout: float | None
) -> AIResult:
    """Mirror the sidecar prompt result without logging untrusted response fields."""
    response = await client._client.post(
        f"/sessions/{session_id}/prompt",
        json={"message": message},
        timeout=timeout or client._client.timeout,
    )
    if response.status_code != 200:
        try:
            payload = response.json()
            error = (
                payload.get("error", response.text)
                if isinstance(payload, dict)
                else response.text
            )
        except ValueError:
            error = response.text or f"HTTP {response.status_code}"
        return AIResult(success=False, text=error, error=error)
    data = response.json()
    usage_data = data.get("usage", {})
    usage = AITokenUsage(
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        cache_read_tokens=usage_data.get("cache_read_tokens", 0),
        cache_write_tokens=usage_data.get("cache_write_tokens", 0),
        cost_usd=usage_data.get("cost_usd"),
        duration_ms=usage_data.get("duration_ms"),
    )
    error = data.get("error")
    if error:
        return AIResult(
            success=False, text=data.get("text") or error, usage=usage, error=error
        )
    return AIResult(success=True, text=data.get("text", ""), usage=usage)


async def _call_user_session(
    prompt: str,
    *,
    ai_provider: str,
    ai_model: str,
    api_key: str | None = None,
    session_id: str | None = None,
    once: bool = False,
    **kwargs: Any,
) -> AIResult:
    """Avoid the sidecar convenience wrapper, which logs raw resumed-session errors."""
    client = get_sidecar_client()
    created = False
    try:
        if not session_id:
            session_id = await create_session_safely(
                client,
                provider=ai_provider,
                model=ai_model,
                system_prompt=kwargs.get("system_prompt")
                or "You are a helpful assistant.",
                cwd=kwargs.get("cwd") or tempfile.gettempdir(),
                agent_dir=kwargs.get("agent_dir"),
                custom_tools=kwargs.get("custom_tools"),
                tools=kwargs.get("tools"),
                api_key=api_key,
            )
            created = True
        if not session_id:
            raise ValueError("Missing AI session ID")
        timeout = kwargs.get("ai_call_timeout")
        result = await _prompt_safely(
            client, session_id, prompt, timeout * 60.0 if timeout else None
        )
        result.session_id = session_id
    except Exception as exc:  # noqa: BLE001 - sidecar/transport errors may contain credentials
        if created and session_id:
            try:
                await delete_ai_session(client, session_id)
                session_id = None
            except Exception:  # noqa: BLE001, S110 - never log credential-bearing cleanup errors
                pass
        # The outer wrapper replaces all failure details for keyed sessions.
        return AIResult(
            success=False, text=str(exc), error=str(exc), session_id=session_id
        )
    if once or (created and not result.success):
        try:
            await delete_ai_session(client, session_id)
            result.session_id = None
        except Exception:  # noqa: BLE001, S110 - retain session, never log raw errors
            pass
    return result


async def call_ai(
    *args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any
) -> AIResult:
    """Call Pi-sidecar with a validated, unchanged catalog pair."""
    provider, model = await resolve_catalog_pair(ai_provider, ai_model)
    key = None
    if not kwargs.get("session_id"):
        if _selected_credential_source.get() != "server":
            key = await session_key(provider)
        if key is not None:
            kwargs["api_key"] = key
    else:
        kwargs.pop("api_key", None)
    username = ai_username.get()
    sidecar_call = _call_user_session if key is not None else _call_ai
    session_id = kwargs.get("session_id")
    if session_id:
        from rootcoz.storage import get_ai_session_source

        try:
            source = await get_ai_session_source(session_id, username, provider)
        except ValueError:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
        except Exception:  # noqa: BLE001 - failed ownership lookup must never prompt
            logger.warning("Unable to verify AI session ownership")
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
    else:
        source = "user" if key is not None else "server"
    if session_id and source == "user":
        # Only the current owner's provider key can redact a keyed session.
        key = await session_key(provider)
        if key is None:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
        try:
            await get_ai_session_source(session_id, username, provider)
        except ValueError:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
        sidecar_call = _call_user_session
    result = await _call_with_safe_error(
        sidecar_call,
        *args,
        redact_key=key if key is not None else "" if session_id and username else None,
        ai_provider=provider,
        ai_model=model,
        **kwargs,
    )
    if session_id and source == "user" and result.success:
        try:
            await get_ai_session_source(session_id, username, provider)
        except ValueError:
            return AIResult(
                success=False,
                text="AI session unavailable",
                error="AI session unavailable",
            )
    result.credential_source = source
    if not session_id and result.session_id and key is None:
        await _remember_session_source(result.session_id, provider, source)
    return result


async def call_ai_once(
    *args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any
) -> AIResult:
    """Call Pi-sidecar once with a validated, unchanged catalog pair."""
    provider, model = await resolve_catalog_pair(ai_provider, ai_model)
    key = None
    if _selected_credential_source.get() != "server":
        key = await session_key(provider)
    if key is not None:
        kwargs["api_key"] = key
    sidecar_call = _call_user_session if key is not None else _call_ai_once
    if key is not None:
        kwargs["once"] = True
    result = await _call_with_safe_error(
        sidecar_call,
        *args,
        redact_key=key,
        ai_provider=provider,
        ai_model=model,
        **kwargs,
    )
    result.credential_source = "user" if key is not None else "server"
    return result


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
