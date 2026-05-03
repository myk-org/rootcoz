"""AI model listing and caching per provider.

Provides model discovery for configured AI providers:
- cursor: runs ``agent models`` subprocess and parses output
- claude: filters LiteLLM pricing cache for Anthropic models
- gemini: filters LiteLLM pricing cache for Google Gemini models

All operations are best-effort — errors are logged at WARNING and
empty results returned. The admin must always know what happened.
"""

from __future__ import annotations

import asyncio
import os
import time

from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

# Provider prefixes to exclude when filtering LiteLLM pricing data.
# These indicate provider-specific model entries that duplicate the
# canonical (bare) model name.
_PROVIDER_PREFIXES = (
    "anthropic.",
    "vertex_ai/",
    "azure_ai/",
    "openrouter/",
    "bedrock/",
    "amazon.",
    "together_ai/",
    "deepinfra/",
    "fireworks_ai/",
    "voyage/",
    "sagemaker/",
    "cohere_chat/",
    "replicate/",
    "ai21/",
    "mistral/",
    "perplexity/",
    "anyscale/",
    "cloudflare/",
    "palm/",
    "text-completion-openai/",
    "text-completion-codestral/",
    "deepseek/",
    "hosted_vllm/",
    "databricks/",
    "friendliai/",
    "groq/",
    "cerebras/",
    "sambanova/",
    "github/",
)

_CURSOR_SUBPROCESS_TIMEOUT = 10  # seconds
_MODEL_CACHE_TTL_SECONDS = 3600  # 1 hour


def _model_id_to_display_name(model_id: str) -> str:
    """Convert a model id like ``claude-sonnet-4`` to ``Claude Sonnet 4``."""
    return model_id.replace("-", " ").replace("/", " ").title()


class AIModelCache:
    """Cache for AI model listings per provider."""

    def __init__(self) -> None:
        self._cache: dict[
            str, dict
        ] = {}  # {provider: {"models": [...], "fetched_at": float}}
        self._pricing_cache: object | None = None

    def set_pricing_cache(self, pricing_cache: object) -> None:
        """Set the LLM pricing cache instance for LiteLLM-based lookups."""
        self._pricing_cache = pricing_cache
        # Invalidate cached providers that depend on pricing data
        for p in ("claude", "gemini"):
            self._cache.pop(p, None)

    async def list_models(self, provider: str) -> list[dict]:
        """Return cached models for *provider*, fetching if needed.

        Returns a list of ``{"id": "...", "name": "..."}`` dicts.
        On any error, logs WARNING and returns ``[]``.
        """
        provider = provider.lower().strip()
        entry = self._cache.get(provider)
        if entry is not None:
            age = time.monotonic() - entry["fetched_at"]
            if age < _MODEL_CACHE_TTL_SECONDS:
                logger.debug(
                    "Returning cached models for provider=%s (%d models, age=%.0fs)",
                    provider,
                    len(entry["models"]),
                    age,
                )
                return entry["models"]
            logger.debug("Cache expired for provider=%s (age=%.0fs)", provider, age)

        models = await self._fetch_models(provider)
        self._cache[provider] = {"models": models, "fetched_at": time.monotonic()}
        logger.debug("Fetched %d models for provider=%s", len(models), provider)
        return models

    async def refresh(self, provider: str | None = None) -> None:
        """Refresh the cache for one or all providers."""
        if provider:
            provider = provider.lower().strip()
            self._cache.pop(provider, None)
            await self.list_models(provider)
        else:
            providers = list(self._cache.keys())
            self._cache.clear()
            for p in providers:
                await self.list_models(p)

    def is_valid_model(self, provider: str, model: str) -> bool | None:
        """Check whether *model* is valid for *provider*.

        Returns ``True``/``False`` when the cache is populated,
        ``None`` when the cache is empty (can't validate).
        """
        provider = provider.lower().strip()
        entry = self._cache.get(provider)
        if entry is None:
            return None
        model_ids = {m["id"] for m in entry["models"]}
        return model in model_ids

    async def _fetch_models(self, provider: str) -> list[dict]:
        """Dispatch model listing to the correct provider handler."""
        if provider == "cursor":
            return await self._list_cursor_models()
        elif provider == "claude":
            return self._list_claude_models()
        elif provider == "gemini":
            return self._list_gemini_models()
        else:
            logger.warning(
                "Unknown provider for model listing: %s — returning empty list",
                provider,
            )
            return []

    # -- Cursor ---------------------------------------------------------------

    async def _list_cursor_models(self) -> list[dict]:
        """Run ``agent models`` subprocess and parse output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "agent",
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CURSOR_SUBPROCESS_TIMEOUT
            )
        except FileNotFoundError:
            logger.warning(
                "Cursor CLI binary 'agent' not found — cannot list cursor models"
            )
            return []
        except asyncio.TimeoutError:
            logger.warning(
                "Cursor 'agent models' subprocess timed out after %ds",
                _CURSOR_SUBPROCESS_TIMEOUT,
            )
            try:
                proc.kill()  # type: ignore[union-attr]
                await proc.wait()  # reap process to avoid zombies
            except ProcessLookupError:
                pass
            return []
        except Exception:
            logger.warning("Failed to run 'agent models' subprocess", exc_info=True)
            return []

        if proc.returncode != 0:
            logger.warning(
                "Cursor 'agent models' exited with code %d: %s",
                proc.returncode,
                stderr.decode(errors="replace").strip() if stderr else "(no stderr)",
            )
            return []

        return self._parse_cursor_output(stdout.decode(errors="replace"))

    @staticmethod
    def _parse_cursor_output(output: str) -> list[dict]:
        """Parse ``agent models`` output.

        Expected format::

            Available models
            model-id - Display Name
            model-id2 - Display Name 2
            Tip: use ...

        Skips the first line (header) and last line (tip).
        """
        lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
        if len(lines) <= 2:
            return []

        # Skip header (first line) and footer (last line)
        model_lines = lines[1:-1]
        models: list[dict] = []
        for line in model_lines:
            if " - " in line:
                model_id, display_name = line.split(" - ", 1)
                models.append(
                    {
                        "id": model_id.strip(),
                        "name": display_name.strip(),
                    }
                )
            elif line.strip():
                # Line without separator — treat as model id only
                models.append(
                    {
                        "id": line.strip(),
                        "name": _model_id_to_display_name(line.strip()),
                    }
                )
        return models

    # -- Claude (LiteLLM) ----------------------------------------------------

    def _list_claude_models(self) -> list[dict]:
        """Filter LiteLLM pricing cache for Anthropic Claude models."""
        pricing_data = self._get_pricing_data()
        if pricing_data is None:
            logger.warning(
                "LiteLLM pricing cache not available — cannot list Claude models"
            )
            return []

        models: list[dict] = []
        for key in sorted(pricing_data.keys()):
            if not isinstance(key, str):
                continue

            # Must start with "claude-"
            if not key.startswith("claude-"):
                continue

            # Exclude provider-prefixed entries
            if any(key.startswith(prefix) for prefix in _PROVIDER_PREFIXES):
                continue

            # Exclude entries that contain "/" (provider-scoped keys)
            if "/" in key:
                continue

            models.append(
                {
                    "id": key,
                    "name": _model_id_to_display_name(key),
                }
            )

        return models

    # -- Gemini (LiteLLM) ----------------------------------------------------

    def _list_gemini_models(self) -> list[dict]:
        """Filter LiteLLM pricing cache for Google Gemini models."""
        pricing_data = self._get_pricing_data()
        if pricing_data is None:
            logger.warning(
                "LiteLLM pricing cache not available — cannot list Gemini models"
            )
            return []

        models: list[dict] = []
        seen_ids: set[str] = set()
        for key in sorted(pricing_data.keys()):
            if not isinstance(key, str):
                continue

            # Exclude provider-prefixed entries
            if any(key.startswith(prefix) for prefix in _PROVIDER_PREFIXES):
                continue

            model_id: str | None = None
            if key.startswith("gemini/"):
                model_id = key[len("gemini/") :]
            elif key.startswith("gemini-"):
                model_id = key
            else:
                continue

            # Skip if contains additional provider prefix
            if "/" in model_id:
                continue

            if model_id in seen_ids:
                continue
            seen_ids.add(model_id)

            models.append(
                {
                    "id": model_id,
                    "name": _model_id_to_display_name(model_id),
                }
            )

        return models

    # -- Helpers --------------------------------------------------------------

    def _get_pricing_data(self) -> dict | None:
        """Return the raw pricing data dict from the pricing cache, or None."""
        if self._pricing_cache is None:
            return None
        data = getattr(self._pricing_cache, "_data", None)
        if not data or not isinstance(data, dict):
            return None
        return data


# Module-level singleton
model_cache = AIModelCache()
