"""AI client adapter — rootcoz-specific AI setup."""

from __future__ import annotations

from typing import Any

from pi_sidecar_client import AIResult, set_usage_recorder
from pi_sidecar_client import call_ai as _call_ai
from pi_sidecar_client import call_ai_once as _call_ai_once
from pi_sidecar_client import get_sidecar_client
from pi_sidecar_client import list_models as _list_models_raw

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

# Builtin tools for AI sessions — no bash access (MANDATORY per project rules)
# Separate constants to allow independent evolution: analysis may gain tools
# (e.g. write) that chat should never have.
CHAT_BUILTIN_TOOLS: tuple[str, ...] = ("read", "ls", "find", "grep", "subagent")
ANALYSIS_BUILTIN_TOOLS: tuple[str, ...] = ("read", "ls", "find", "grep", "subagent")


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


def map_provider_from_sidecar(provider: str) -> str:
    """Map sidecar provider ids back to rootcoz friendly names."""
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


def map_provider_for_sidecar(provider: str) -> str:
    """Map friendly provider to default sidecar id (ACPX/API).

    Prefer ``map_provider_model_for_sidecar`` when a model id is known so CLI
    models route correctly.
    """
    friendly = normalize_provider(provider)
    return _DEFAULT_SIDECAR.get(friendly, friendly)


async def _ensure_route_cache(friendly: str) -> None:
    """Populate model→sidecar cache for a friendly provider if empty."""
    friendly = normalize_provider(friendly)
    if any(fp == friendly for fp, _ in _model_route_cache):
        return
    await list_models(friendly)


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
            _model_route_cache[(friendly, mid)] = sidecar_id
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            result.append(
                {
                    "id": mid,
                    "name": m.get("name") or mid,
                    "provider": friendly,
                    "source": source,
                }
            )
    return result


async def call_ai(*args: Any, ai_provider: str = "", ai_model: str = "", **kwargs: Any):
    """call_ai with rootcoz friendly→sidecar provider/model routing."""
    await _ensure_route_cache(ai_provider)
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
    await _ensure_route_cache(ai_provider)
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
            ai_provider=map_provider_from_sidecar(ai_provider),
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
    "list_models",
    "map_provider_for_sidecar",
    "map_provider_from_sidecar",
    "map_provider_model_for_sidecar",
    "normalize_provider",
]
