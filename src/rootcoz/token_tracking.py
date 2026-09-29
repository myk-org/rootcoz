"""Token usage tracking utilities.

Provides helpers to record AI token usage to the database
and build token usage summaries for analysis results.
"""

import os
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pi_sidecar_client import AIResult
from simple_logger.logger import get_logger

from rootcoz import storage
from rootcoz.models import TokenUsageEntry, TokenUsageSummary

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

_on_usage_recorded: Callable[[str], None] | None = None
_group_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "failure_group_usage", default=None
)
_child_context: ContextVar[tuple[str, int] | None] = ContextVar(
    "child_usage_scope", default=None
)


@contextmanager
def child_usage_scope(job_name: str, build_number: int) -> Iterator[None]:
    """Scope primary usage to a Jenkins child build, including nested analyses."""
    token = _child_context.set((job_name, build_number))
    try:
        yield
    finally:
        _child_context.reset(token)


@contextmanager
def failure_group_usage(job_id: str, error_signature: str) -> Iterator[None]:
    """Scope primary call attribution to this job and failure group."""
    token = _group_context.set((job_id, error_signature))
    try:
        yield
    finally:
        _group_context.reset(token)


def attach_failure_usage(result: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """Attach per-signature primary usage to every matching failure, without splitting it."""
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["call_type"] == "primary" and record.get("error_signature"):
            key = (
                record.get("child_job_name") or "",
                record.get("child_build_number") or 0,
                record["error_signature"],
            )
            grouped[key].append(record)

    summaries = {
        key: summarize_token_usage(calls).model_dump(mode="json")
        for key, calls in grouped.items()
    }

    def apply(
        node: dict[str, Any], child_name: str = "", build_number: int = 0
    ) -> None:
        for failure in node.get("failures") or []:
            if isinstance(failure, dict):
                signature: str = failure.get("error_signature") or ""
                failure["token_usage"] = summaries.get(
                    (child_name, build_number, signature)
                )
        for child in (node.get("child_job_analyses") or []) + (
            node.get("failed_children") or []
        ):
            apply(child, child.get("job_name") or "", child.get("build_number") or 0)

    apply(result)


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
        if not job_id or (not result.success and result.usage is None):
            return

        usage = result.usage
        resolved_provider = (usage.provider if usage else "") or ai_provider
        resolved_model = (usage.model if usage else "") or ai_model

        group = _group_context.get() if call_type == "primary" else None
        child = _child_context.get() if group and group[0] == job_id else None
        await storage.record_token_usage(
            job_id=job_id,
            ai_provider=resolved_provider,
            ai_model=resolved_model,
            call_type=call_type,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            cost_usd=usage.cost_usd if usage else None,
            duration_ms=usage.duration_ms if usage else None,
            prompt_chars=prompt_chars,
            response_chars=len(result.text),
            credential_source=getattr(result, "credential_source", "unknown"),
            error_signature=group[1] if group and group[0] == job_id else "",
            child_job_name=child[0] if child else "",
            child_build_number=child[1] if child else 0,
        )
        if group and group[0] == job_id:
            logger.info("Recorded primary usage for job %s group %s", job_id, group[1])
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

        calls = [
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
            for rec in records
        ]
        return TokenUsageSummary(**totals, calls=calls)

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
                credential_source=rec.get("credential_source") or "unknown",
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

    sources = {rec.get("credential_source") or "unknown" for rec in records}
    if {"user", "server"} <= sources:
        credential_source = "mixed"
    elif sources == {"user"}:
        credential_source = "user"
    elif sources == {"server"}:
        credential_source = "server"
    else:
        credential_source = "unknown"
    return TokenUsageSummary(
        credential_source=credential_source,
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
