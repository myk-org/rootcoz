"""AI client adapter — rootcoz-specific AI setup."""

from pi_sidecar_client import (
    AIResult,
    _PROVIDER_MAP,
    call_ai,
    call_ai_once,
    set_usage_recorder,
)
from simple_logger.logger import get_logger

logger = get_logger(name=__name__)

# Reverse map: sidecar provider name -> rootcoz provider name (module-level constant)
_REVERSE_PROVIDER_MAP: dict[str, str] = {v: k for k, v in _PROVIDER_MAP.items()}

# Cache for model→provider lookups (short TTL to avoid stale data)
_model_provider_cache: dict[str, tuple[str | None, float]] = {}
_MODEL_CACHE_TTL = 60.0  # seconds

# rootcoz-specific: valid providers for this application
VALID_AI_PROVIDERS = {"claude", "cursor", "gemini"}


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


async def get_provider_for_model(model: str) -> str | None:
    """Look up the rootcoz provider name for a model by querying the sidecar.

    Results are cached for 60 seconds to avoid repeated sidecar calls.
    Returns the rootcoz provider name (claude/gemini/cursor) or None if not found.
    """
    import time

    from pi_sidecar_client import list_models

    # Check cache
    now = time.monotonic()
    if model in _model_provider_cache:
        cached_provider, cached_at = _model_provider_cache[model]
        if now - cached_at < _MODEL_CACHE_TTL:
            return cached_provider

    try:
        all_models = await list_models()
        # Rebuild cache for all models at once
        for m in all_models:
            mid = m.get("id", "")
            sidecar_provider = m.get("provider", "")
            _model_provider_cache[mid] = (
                _REVERSE_PROVIDER_MAP.get(sidecar_provider),
                now,
            )
        # Return result for requested model
        if model in _model_provider_cache:
            return _model_provider_cache[model][0]
        return None
    except Exception:
        logger.warning("Failed to query sidecar for model '%s'", model, exc_info=True)
        return None


__all__ = [
    "AIResult",
    "VALID_AI_PROVIDERS",
    "_setup_usage_recorder",
    "call_ai",
    "call_ai_once",
    "get_provider_for_model",
]
