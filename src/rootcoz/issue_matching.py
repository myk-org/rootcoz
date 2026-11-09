"""Shared AI relevance filtering for issue matching (Jira, GitHub, etc.).

Provides the common logic for using AI to evaluate whether candidate
issues from any tracker match a given bug/failure description, plus the
per-tracker search record (``TrackerSearch``) both tracker paths fill in.
"""

import json
from collections.abc import Sequence
from typing import Any, TypedDict

from rootcoz import get_child_logger
from rootcoz.ai_client import call_ai_once
from rootcoz.models import AnalysisDetail, FailureAnalysis, ProductBugReport

logger = get_child_logger(__name__)


class TrackerSearch(TypedDict):
    """What one tracker's duplicate search did, recorded on the result.

    ``searched`` is True only when at least one search request actually
    succeeded, so ``searched: True, candidates: 0`` means "looked and found
    nothing" while ``searched: False`` plus a ``reason`` means "could not
    look". A ``searched: False`` record always carries a non-empty reason.

    ``scope`` is what was searched (GitHub repository URLs or
    ``[jira_project_key]``), ``keyword_sets``/``candidates``/``matches`` are
    the counts the caller already computes.
    """

    searched: bool
    reason: str
    scope: list[str]
    keyword_sets: int
    candidates: int
    matches: int


def tracker_search(
    *,
    searched: bool = False,
    reason: str = "",
    scope: list[str] | None = None,
    keyword_sets: int = 0,
    candidates: int = 0,
    matches: int = 0,
) -> TrackerSearch:
    """Build a TrackerSearch — the single builder both trackers use."""
    return TrackerSearch(
        searched=searched,
        reason=reason,
        scope=list(scope or []),
        keyword_sets=keyword_sets,
        candidates=candidates,
        matches=matches,
    )


def search_failure_reason(
    *,
    search_label: str,
    filter_label: str,
    failed_searches: int,
    total_searches: int,
    filter_failures: int,
) -> str:
    """Join the reason a tracker search was partial or total.

    Shared by both trackers so the two records cannot drift apart: a failed
    search is a warning, a failed relevance filter is recorded separately so
    "could not look" and "could not judge" stay distinguishable.

    Args:
        search_label: The searches, e.g. ``"Jira search(es)"``.
        filter_label: The relevance filter calls, e.g. ``"Jira relevance filter"``.
        failed_searches: How many search requests failed.
        total_searches: How many search requests were made.
        filter_failures: How many relevance filter calls failed.

    Returns:
        The reason joined with ``"; "``, empty when nothing failed.
    """
    reasons: list[str] = []
    if failed_searches:
        reasons.append(
            f"all {total_searches} {search_label} failed"
            if failed_searches == total_searches
            else f"{failed_searches} of {total_searches} {search_label} failed"
        )
    if filter_failures:
        reasons.append(f"{filter_failures} {filter_label} call(s) failed")
    return "; ".join(reasons)


def collect_product_bug_reports(
    failures: Sequence[FailureAnalysis],
) -> list[ProductBugReport]:
    """Collect all ProductBugReport instances from a list of failures.

    Shared by the Jira and GitHub duplicate searches so both ask the same
    population of failures the same question.

    Args:
        failures: List of failure analyses to scan.

    Returns:
        List of ProductBugReport objects found.
    """
    reports: list[ProductBugReport] = []
    for failure in failures:
        detail: AnalysisDetail = failure.analysis
        if isinstance(detail.product_bug_report, ProductBugReport):
            reports.append(detail.product_bug_report)
    return reports


async def filter_issue_matches_with_ai(
    bug_title: str,
    bug_description: str,
    candidates: list[dict[str, Any]],
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None = None,
    job_id: str = "",
    call_type: str = "issue_filter",
) -> list[dict[str, Any]] | None:
    """Use AI to determine which candidate issues are relevant to a bug.

    Sends the bug context and all candidate issues to the AI, asking it
    to evaluate relevance. Returns evaluation dicts for relevant candidates
    only. The caller is responsible for converting these into the appropriate
    model type (JiraMatch, SimilarIssue, etc.).

    Each candidate dict must have a ``key`` field used as the identifier.

    Args:
        bug_title: The bug/failure report title.
        bug_description: The bug/failure report description.
        candidates: List of candidate dicts from issue search.
            Each must have ``key``, ``summary``/``title``, ``description``,
            and ``status`` fields.
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        ai_call_timeout: Timeout in minutes (overrides AI_CALL_TIMEOUT env var).
        job_id: Job identifier for token usage tracking.
        call_type: Token tracking call type label.

    Returns:
        List of dicts with ``key``, ``relevant`` (True), and ``score`` for
        relevant candidates only, sorted by score descending. ``None`` when the
        filter could not judge at all (the AI call failed or its answer could
        not be parsed) — an empty list means it judged and kept nothing, so the
        two must stay distinguishable in the tracker record.
    """
    if not candidates:
        return []

    # Build candidate list for the AI prompt
    candidate_lines = []
    for i, c in enumerate(candidates, 1):
        title = c.get("summary") or c.get("title") or ""
        desc = c.get("description") or "No description"
        status = c.get("status", "")
        key = c.get("key", "")
        candidate_lines.append(
            f"{i}. {key} [{status}] - {title}\n   Description: {desc}"
        )

    prompt = f"""You are evaluating whether existing issue tickets match a newly discovered bug.

NEW BUG:
Title: {bug_title}
Description: {bug_description}

CANDIDATES:
{chr(10).join(candidate_lines)}

For each candidate, determine if it describes the SAME bug or a closely related issue
(including regressions of previously fixed bugs).

A match means the ticket describes essentially the same broken behavior,
not just that it mentions similar components or technologies.

Respond with ONLY a JSON array. For each candidate include:
- "key": the issue key/identifier
- "relevant": true or false
- "score": relevance score 0.0 to 1.0 (1.0 = exact same bug, 0.5+ = likely related)

Example: [{{"key": "PROJ-123", "relevant": true, "score": 0.9}}, {{"key": "PROJ-456", "relevant": false, "score": 0.1}}]

Respond with ONLY the JSON array, no other text."""

    logger.info(
        "AI call: provider=%s, model=%s, call_type=%s, job_id=%s",
        ai_provider,
        ai_model,
        call_type,
        job_id,
    )
    result = await call_ai_once(
        prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_call_timeout=ai_call_timeout,
        tools=[],
    )

    if job_id:
        await result.record_usage(
            request_id=job_id,
            call_type=call_type,
            prompt_chars=len(prompt),
            ai_provider=ai_provider,
            ai_model=ai_model,
        )

    if not result.success:
        logger.warning("AI relevance filtering failed: %s", result.text)
        return None

    # Parse AI response
    try:
        text = result.text.strip()
        # Strip markdown code block if present
        if "```json" in text:
            start = text.index("```json") + len("```json")
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + len("```")
            end = text.index("```", start)
            text = text[start:end].strip()

        json_start = text.find("[")
        json_end = text.rfind("]")
        if json_start != -1 and json_end != -1:
            text = text[json_start : json_end + 1]

        evaluations = json.loads(text)
    except json.JSONDecodeError, ValueError:
        logger.warning("Failed to parse AI relevance response")
        return None

    relevant: list[dict[str, Any]] = []
    for evaluation in evaluations:
        if not isinstance(evaluation, dict):
            continue
        key = evaluation.get("key", "")
        is_relevant = evaluation.get("relevant", False)
        try:
            score = float(evaluation.get("score", 0.0))
        except (ValueError, TypeError) as e:
            logger.debug("Failed to parse relevance score, defaulting to 0.0: %s", e)
            score = 0.0

        if is_relevant and key:
            relevant.append({"key": key, "relevant": True, "score": score})

    relevant.sort(key=lambda m: m["score"], reverse=True)
    return relevant
