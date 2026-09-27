"""Token usage tracking utilities.

Provides helpers to record AI token usage to the database
and build token usage summaries for analysis results.
"""

import os
from collections.abc import Callable
from typing import Any

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
    Uses provider/model from result.usage when present, falls back to parameters.
    """
    try:
        if not job_id or result.usage is None:
            return

        usage = result.usage
        resolved_provider = usage.provider or ai_provider
        resolved_model = usage.model or ai_model

        await storage.record_token_usage(
            job_id=job_id,
            ai_provider=resolved_provider,
            ai_model=resolved_model,
            call_type=call_type,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=usage.cost_usd,
            duration_ms=usage.duration_ms,
            prompt_chars=prompt_chars,
            response_chars=len(result.text),
            credential_source=getattr(result, "credential_source", "unknown"),
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
        if detailed:
            totals, records = await storage.get_job_token_usage_details(job_id)
        else:
            totals = await storage.get_job_token_usage_totals(job_id)
        if not totals:
            return None
        if not detailed:
            return TokenUsageSummary(**totals)

        return TokenUsageSummary(
            **totals,
            calls=summarize_token_usage(records).calls,
        )
    except Exception:
        logger.debug(
            "Failed to build token usage summary for job %s", job_id, exc_info=True
        )
        return None


def summarize_token_usage(records: list[dict[str, Any]]) -> TokenUsageSummary:
    """Summarize stored per-call usage without reading or writing the database."""
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
                credential_source=rec.get("credential_source", "unknown"),
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
