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
        totals = await storage.get_job_token_usage_totals(job_id)
        if not totals:
            return None
        if not detailed:
            return TokenUsageSummary(**totals)

        records = await storage.get_token_usage_for_job(job_id)
        calls = [
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
            for rec in records
        ]
        return TokenUsageSummary(**totals, calls=calls)
    except Exception:
        logger.debug(
            "Failed to build token usage summary for job %s", job_id, exc_info=True
        )
        return None
