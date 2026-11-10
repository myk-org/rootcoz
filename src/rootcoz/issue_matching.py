"""Shared AI relevance filtering for issue matching (Jira, GitHub, etc.).

Provides the common logic for using AI to evaluate whether candidate
issues from any tracker match a given bug/failure description, the
per-tracker search record (``TrackerSearch``) both tracker paths fill in,
and the one search loop both trackers run (``run_tracker_search``): one
loop, three adapters — Jira duplicates, GitHub duplicates and the GitHub
tests-repo question.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
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
    prompt_overflows: int = 0,
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
        prompt_overflows: How many relevance filter calls were not made
            because their prompt was too large to send.

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
    if prompt_overflows:
        # Not run, not failed: an oversized prompt is its own outcome so it is
        # never read as "the filter judged and kept nothing".
        reasons.append(
            f"{prompt_overflows} {filter_label} call(s) not run: prompt too large"
        )
    if filter_failures:
        reasons.append(f"{filter_failures} {filter_label} call(s) failed")
    return "; ".join(reasons)


def group_targets_by_keywords[T](
    items: Sequence[T], keywords_of: Callable[[T], Sequence[str]]
) -> dict[tuple[str, ...], list[T]]:
    """Group items by their sorted keyword set — same keywords = one search.

    Items without keywords are skipped: they carry nothing to search for.
    First-appearance order is kept so the fan-out order is deterministic.

    Args:
        items: The reports/code-fixes to group.
        keywords_of: How to read an item's keywords.

    Returns:
        The sorted keyword tuple of each set mapped to its items.
    """
    grouped: dict[tuple[str, ...], list[T]] = {}
    for item in items:
        keywords = keywords_of(item)
        if not keywords:
            continue
        grouped.setdefault(tuple(sorted(keywords)), []).append(item)
    return grouped


@dataclass(frozen=True)
class TrackerSearchSpec:
    """What one tracker search differs in; the loop owns everything else.

    ``search_targets`` is the fan-out unit (GitHub repositories, or a single
    opaque unit for Jira) and is deliberately independent of ``scope`` — what
    a record says was searched. ``filter_matches`` is ``None`` when no AI
    provider/model is configured: the filter does not run and ``all_matches``
    converts every candidate instead.
    """

    label: str
    search_label: str
    filter_label: str
    scope: Sequence[str]
    search_targets: Sequence[str]
    search_one: Callable[[tuple[str, ...], str], Awaitable[list[dict[str, Any]] | None]]
    namespace: Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]
    bug_context: Callable[[Any], tuple[str, str]]
    filter_matches: Callable[[str, str, list[dict[str, Any]]], Awaitable[Any]] | None
    all_matches: Callable[[list[dict[str, Any]]], Any]
    attach: Callable[[Any, Any], None]


async def run_tracker_search(
    targets_by_keywords: dict[tuple[str, ...], list[Any]],
    spec: TrackerSearchSpec,
    job_id: str = "",
) -> TrackerSearch:
    """Search every unit per keyword set, filter, attach, record.

    The single loop behind every tracker search: the record must keep three
    outcomes apart. ``searched: True`` with ``matches: 0`` is a completed
    judgement; a refused or failed lookup is ``searched: False`` (or a
    ``reason``) and never reads as "nothing matched"; an oversized prompt is
    "not run", never a failed filter call.

    Never raises on a tracker failure — a failed search and a failed filter
    are counted, not propagated. Callers pass a non-empty
    ``targets_by_keywords`` and ``spec.search_targets`` (their preflight
    already refused otherwise) and keep their own outer guard.

    Args:
        targets_by_keywords: Targets grouped by sorted keyword set.
        spec: The tracker-specific callbacks and labels.
        job_id: Job identifier for logging and token tracking.

    Returns:
        The record to stamp on the result.
    """
    keyword_sets = len(targets_by_keywords)
    total_searches = keyword_sets * len(spec.search_targets)

    async def _search_safe(
        keywords: tuple[str, ...], target: str
    ) -> list[dict[str, Any]] | None:
        """One search; ``None`` marks a failure (distinct from an empty list)."""
        try:
            return await spec.search_one(keywords, target)
        except Exception:
            logger.exception(
                "%s failed%s with keywords: %s",
                spec.label,
                f" for {target}" if target else "",
                list(keywords),
            )
            return None

    search_results = await asyncio.gather(
        *(
            _search_safe(keywords, target)
            for keywords in targets_by_keywords
            for target in spec.search_targets
        )
    )

    failed_searches = 0
    filter_failures = 0
    oversized_filters = 0
    total_candidates = 0
    total_matches = 0
    for index, (keywords, targets) in enumerate(targets_by_keywords.items()):
        candidates: list[dict[str, Any]] = []
        for offset, target in enumerate(spec.search_targets):
            found = search_results[index * len(spec.search_targets) + offset]
            if found is None:
                failed_searches += 1
                continue
            candidates.extend(spec.namespace(found, target))
        total_candidates += len(candidates)
        if not candidates:
            continue

        bug_title, bug_description = spec.bug_context(targets[0])
        if spec.filter_matches is not None:
            prompt_chars = len(
                build_filter_prompt(bug_title, bug_description, candidates)
            )
            if prompt_chars > MAX_FILTER_PROMPT_CHARS:
                # Not sent: the overflow would come back as "no verdict" and be
                # recorded as a failed filter call instead of an unrun one.
                logger.warning(
                    "%s relevance filter not run for %d candidate(s) for job=%s: "
                    "prompt is %d chars, over the %d char limit",
                    spec.label,
                    len(candidates),
                    job_id,
                    prompt_chars,
                    MAX_FILTER_PROMPT_CHARS,
                )
                oversized_filters += 1
                continue
            try:
                matches = await spec.filter_matches(
                    bug_title, bug_description, candidates
                )
            except Exception:
                # One failed filter must not discard the keyword sets after it:
                logger.exception(
                    "%s relevance filtering failed for %d candidate(s) for job=%s",
                    spec.label,
                    len(candidates),
                    job_id,
                )
                filter_failures += 1
                continue
            if matches is None:
                # The filter could not judge — not the same as judging every
                # candidate irrelevant.
                logger.warning(
                    "%s relevance filter returned no verdict for %d candidate(s) "
                    "for job=%s",
                    spec.label,
                    len(candidates),
                    job_id,
                )
                filter_failures += 1
                continue
        else:
            # No AI config — fall back to returning all candidates as matches
            logger.debug(
                "No AI provider configured for %s relevance filtering, "
                "returning all candidates",
                spec.label,
            )
            matches = spec.all_matches(candidates)

        for target in targets:
            spec.attach(target, matches)
        total_matches += len(matches)

    reason = search_failure_reason(
        search_label=spec.search_label,
        filter_label=spec.filter_label,
        failed_searches=failed_searches,
        total_searches=total_searches,
        filter_failures=filter_failures,
        prompt_overflows=oversized_filters,
    )
    if reason:
        logger.warning("%s: %s for job=%s", spec.label, reason, job_id)
    logger.info(
        "%s complete for job=%s: %d keyword set(s), %d candidate(s), %d match(es)",
        spec.label,
        job_id,
        keyword_sets,
        total_candidates,
        total_matches,
    )
    return tracker_search(
        searched=failed_searches < total_searches,
        reason=reason,
        scope=list(spec.scope),
        keyword_sets=keyword_sets,
        candidates=total_candidates,
        matches=total_matches,
    )


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


# Per-candidate cap on the body put in the filter prompt: a keyword set can
# carry many candidates, and untruncated bodies overflow the model context.
_CANDIDATE_DESCRIPTION_CHARS = 1000

# A prompt above this size is not sent at all: the overflow would come back as
# "no verdict", indistinguishable from a filter failure.
MAX_FILTER_PROMPT_CHARS = 150_000


def _candidate_excerpt(description: str) -> str:
    """Trim one candidate body, saying plainly that it was trimmed."""
    if len(description) <= _CANDIDATE_DESCRIPTION_CHARS:
        return description
    return (
        description[:_CANDIDATE_DESCRIPTION_CHARS]
        + f" [... truncated, {len(description)} chars total]"
    )


def build_filter_prompt(
    bug_title: str,
    bug_description: str,
    candidates: list[dict[str, Any]],
) -> str:
    """The exact prompt :func:`filter_issue_matches_with_ai` sends.

    Every candidate is included (bodies capped per candidate, never dropped)
    so the candidate count stays truthful, and the caller can measure the
    prompt before paying for it.
    """
    candidate_lines = []
    for i, c in enumerate(candidates, 1):
        title = c.get("summary") or c.get("title") or ""
        desc = _candidate_excerpt(c.get("description") or "No description")
        status = c.get("status", "")
        key = c.get("key", "")
        candidate_lines.append(
            f"{i}. {key} [{status}] - {title}\n   Description: {desc}"
        )

    return f"""You are evaluating whether existing issue tickets match a newly discovered bug.

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

    prompt = build_filter_prompt(bug_title, bug_description, candidates)
    if len(prompt) > MAX_FILTER_PROMPT_CHARS:
        logger.warning(
            "AI relevance filtering not run: prompt is %d chars, over the %d "
            "char limit",
            len(prompt),
            MAX_FILTER_PROMPT_CHARS,
        )
        return None

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
