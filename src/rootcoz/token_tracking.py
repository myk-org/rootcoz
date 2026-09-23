"""Token usage tracking utilities.

Provides helpers to record AI token usage to the database
and build token usage summaries for analysis results.
"""

import os
from collections.abc import Callable

from pi_sidecar_client import AIResult
from simple_logger.logger import get_logger

from rootcoz import storage
from rootcoz.models import TokenUsageEntry, TokenUsageSummary

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

_on_usage_recorded: Callable[[str], None] | None = None


def set_usage_callback(callback: Callable[[str], None]) -> None:
    """Register the job-scoped SSE notifier for successful usage writes."""
    global _on_usage_recorded
    _on_usage_recorded = callback


async def record_ai_usage(
    job_id: str,
    result: AIResult,
    call_type: str,
    prompt_chars: int = 0,
    ai_provider: str = "",
    ai_model: str = "",
) -> None:
    """Record token usage from an AIResult to the database.

    Best-effort — failures are logged but never raised.
    Uses provider/model from result.usage if available, falls back to parameters.
    """
    try:
        if not job_id:
            return

        usage = result.usage
        resolved_provider = (usage.provider if usage else "") or ai_provider
        resolved_model = (usage.model if usage else "") or ai_model
        cost = usage.cost_usd if usage else None

        await storage.record_token_usage(
            job_id=job_id,
            ai_provider=resolved_provider,
            ai_model=resolved_model,
            call_type=call_type,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            cost_usd=cost,
            duration_ms=usage.duration_ms if usage else None,
            prompt_chars=prompt_chars,
            response_chars=len(result.text),
        )
        if _on_usage_recorded:
            _on_usage_recorded(job_id)
    except Exception:
        logger.debug("Failed to record token usage for job %s", job_id, exc_info=True)


async def build_token_usage_summary(
    job_id: str, *, detailed: bool = True
) -> TokenUsageSummary | None:
    """Build usage totals, including per-call details only when requested.

    Returns None if no usage records exist.
    """
    try:
        if not detailed:
            totals = await storage.get_job_token_usage_totals(job_id)
            return TokenUsageSummary(**totals) if totals else None

        records = await storage.get_token_usage_for_job(job_id)
        if not records:
            return None

        calls = []
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_cost: float | None = 0.0
        total_duration = 0

        for rec in records:
            calls.append(
                TokenUsageEntry(
                    provider=rec["ai_provider"],
                    model=rec["ai_model"],
                    call_type=rec["call_type"],
                    input_tokens=rec["input_tokens"],
                    output_tokens=rec["output_tokens"],
                    cache_read_tokens=rec["cache_read_tokens"],
                    cache_write_tokens=rec["cache_write_tokens"],
                    total_tokens=rec["total_tokens"],
                    cost_usd=rec["cost_usd"],
                    duration_ms=rec["duration_ms"],
                )
            )
            total_input += rec["input_tokens"]
            total_output += rec["output_tokens"]
            total_cache_read += rec["cache_read_tokens"]
            total_cache_write += rec["cache_write_tokens"]
            if rec["cost_usd"] is not None:
                if total_cost is not None:
                    total_cost += rec["cost_usd"]
            else:
                total_cost = None  # If any call lacks cost, total is None
            if rec["duration_ms"] is not None:
                total_duration += rec["duration_ms"]

        return TokenUsageSummary(
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_cache_read_tokens=total_cache_read,
            total_cache_write_tokens=total_cache_write,
            total_tokens=total_input + total_output,
            total_cost_usd=total_cost,
            total_duration_ms=total_duration,
            total_calls=len(calls),
            calls=calls,
        )
    except Exception:
        logger.debug(
            "Failed to build token usage summary for job %s", job_id, exc_info=True
        )
        return None
