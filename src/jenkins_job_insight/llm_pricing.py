"""LLM pricing cache using LiteLLM pricing data.

Fetches model pricing from LiteLLM's GitHub repository and caches it
in memory. All methods are best-effort — errors are logged but never raised.
"""

import asyncio
import os
import re

import httpx
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # 24 hours


class LLMPricingCache:
    """In-memory cache for LLM model pricing data from LiteLLM."""

    def __init__(self) -> None:
        self._data: dict = {}
        self._refresh_task: asyncio.Task | None = None

    async def load(self) -> None:
        """Initial fetch at startup. Best-effort — logs and continues on failure."""
        await self._fetch()

    async def refresh(self) -> None:
        """Re-fetch pricing data. Best-effort — logs and continues on failure."""
        await self._fetch()

    async def _fetch(self) -> None:
        """Fetch pricing JSON from LiteLLM GitHub. Never raises."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(LITELLM_PRICING_URL)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    self._data = data
                    logger.debug("LLM pricing cache loaded: %d models", len(self._data))
                else:
                    logger.warning("LLM pricing data is not a dict, ignoring")
        except Exception:
            logger.warning("Failed to fetch LLM pricing data", exc_info=True)

    _CURSOR_ROUTING_SUFFIXES = ("-xhigh-fast", "-fast", "-xhigh", "-max-thinking")
    _KNOWN_MODEL_PREFIXES = ("claude", "gpt", "gemini")
    _KNOWN_VARIANTS = frozenset(
        {"opus", "sonnet", "haiku", "pro", "flash", "nano", "mini"}
    )

    def _resolve_cursor_model(self, model: str) -> str | None:
        """Resolve a Cursor model name to its canonical LiteLLM key.

        Cursor wraps upstream models with custom naming (e.g.
        ``claude-4.6-opus-max-thinking``).  This method extracts the
        base-model prefix, version, and variant to reconstruct the
        canonical LiteLLM key.  Returns ``None`` if the model does
        not match any known upstream prefix.  Never raises.
        """
        try:
            prefix: str | None = None
            rest: str = ""
            for p in self._KNOWN_MODEL_PREFIXES:
                if model.startswith(f"{p}-"):
                    prefix = p
                    rest = model[len(p) + 1 :]
                    break

            if prefix is None:
                return None

            # Extract version number (e.g. 4.6, 5.4, 2.5)
            version_match = re.match(r"(\d+(?:\.\d+)*)", rest)
            if not version_match:
                return None

            version = version_match.group(1)
            remaining = rest[version_match.end() :]

            # Extract variant if present (first token after version)
            variant: str | None = None
            if remaining.startswith("-"):
                parts = remaining[1:].split("-")
                if parts and parts[0] in self._KNOWN_VARIANTS:
                    variant = parts[0]

            # Reconstruct canonical LiteLLM key per model family
            if prefix == "claude":
                version_dashed = version.replace(".", "-")
                if variant:
                    return f"claude-{variant}-{version_dashed}"
                return f"claude-{version_dashed}"

            if prefix == "gpt":
                if variant:
                    return f"gpt-{version}-{variant}"
                return f"gpt-{version}"

            if prefix == "gemini":
                if variant:
                    return f"gemini-{version}-{variant}"
                return f"gemini-{version}"
        except Exception:
            logger.debug("Failed to resolve cursor model: %s", model, exc_info=True)

        return None

    def _lookup_model(self, provider: str, model: str) -> dict | None:
        """Look up model pricing data trying multiple key formats.

        Tries normalized model names in order:
        1. As-is
        2. With bracketed suffixes stripped (e.g. ``model[1m]`` → ``model``)
        3. With ``@`` replaced by ``-`` (e.g. ``model@date`` → ``model-date``)
        4. (cursor only) With routing suffixes stripped (``-xhigh-fast``, etc.)

        For each normalized name, tries direct lookup, provider-prefixed
        lookup, and (if the name contains ``/``) suffix-only lookup.

        Returns the pricing dict or None if not found.
        """
        if not self._data or not model:
            return None

        # Normalize provider name for LiteLLM keys
        provider_map = {
            "claude": "anthropic",
            "gemini": "gemini",
            "cursor": "cursor",
        }
        litellm_provider = provider_map.get(provider, provider)

        def _try_candidates(name: str) -> dict | None:
            candidates = [name, f"{litellm_provider}/{name}"]
            if "/" in name:
                candidates.append(name.split("/", 1)[1])
            for key in candidates:
                entry = self._data.get(key)
                if isinstance(entry, dict):
                    return entry
            return None

        # Build normalized model names to try
        names_to_try: list[str] = [model]

        # Strip bracketed suffixes: claude-opus-4-6[1m] → claude-opus-4-6
        stripped_brackets = re.sub(r"\[.*?\]$", "", model)
        if stripped_brackets != model:
            names_to_try.append(stripped_brackets)

        # Replace @ with - (on each name so far)
        for name in list(names_to_try):
            at_replaced = name.replace("@", "-")
            if at_replaced != name and at_replaced not in names_to_try:
                names_to_try.append(at_replaced)

        # Strip Cursor routing suffixes (on each name so far)
        if provider == "cursor":
            for name in list(names_to_try):
                for suffix in self._CURSOR_ROUTING_SUFFIXES:
                    if name.endswith(suffix):
                        candidate = name[: -len(suffix)]
                        if candidate and candidate not in names_to_try:
                            names_to_try.append(candidate)

        for name in names_to_try:
            result = _try_candidates(name)
            if result is not None:
                return result

        # Last resort for cursor: resolve upstream model name
        if provider == "cursor":
            for name in names_to_try:
                resolved = self._resolve_cursor_model(name)
                if resolved:
                    result = _try_candidates(resolved)
                    if result is not None:
                        return result

        return None

    def calculate_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float | None:
        """Calculate cost for a model invocation. Returns None if model not found or on error.

        Never raises — all errors are logged and None is returned.
        """
        try:
            entry = self._lookup_model(provider, model)
            if entry is None:
                logger.debug(
                    "Model not found in pricing cache: provider=%s model=%s",
                    provider,
                    model,
                )
                return None

            input_cost_per_token = entry.get("input_cost_per_token")
            output_cost_per_token = entry.get("output_cost_per_token")

            if input_cost_per_token is None or output_cost_per_token is None:
                logger.debug("Missing cost fields for model %s/%s", provider, model)
                return None

            cost = (input_tokens * input_cost_per_token) + (
                output_tokens * output_cost_per_token
            )

            # Add cache costs if available in pricing data
            cache_read_cost = entry.get("cache_read_input_token_cost")
            if cache_read_cost is not None and cache_read_tokens > 0:
                cost += cache_read_tokens * cache_read_cost

            cache_write_cost = entry.get("cache_creation_input_token_cost")
            if cache_write_cost is not None and cache_write_tokens > 0:
                cost += cache_write_tokens * cache_write_cost

            return cost

        except Exception:
            logger.debug(
                "Failed to calculate cost for %s/%s",
                provider,
                model,
                exc_info=True,
            )
            return None

    def start_background_refresh(self) -> None:
        """Start a background task that refreshes pricing data every 24 hours.

        Safe to call multiple times — cancels any existing task first.
        """
        self.stop_background_refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    def stop_background_refresh(self) -> None:
        """Cancel the background refresh task if running."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh_loop(self) -> None:
        """Periodically refresh pricing data. Never raises."""
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Pricing refresh loop error", exc_info=True)
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)


# Module-level singleton
pricing_cache = LLMPricingCache()
