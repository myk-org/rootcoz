"""Shared AI relevance filtering for issue matching (Jira, GitHub, etc.).

Provides the common logic for using AI to evaluate whether candidate
issues from any tracker match a given bug/failure description.
"""

import json
import os

from simple_logger.logger import get_logger

from ai_cli_runner import call_ai_cli
from jenkins_job_insight.analyzer import PROVIDER_CLI_FLAGS
from jenkins_job_insight.token_tracking import record_ai_usage

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


async def filter_issue_matches_with_ai(
    bug_title: str,
    bug_description: str,
    candidates: list[dict],
    ai_provider: str,
    ai_model: str,
    ai_cli_timeout: int | None = None,
    job_id: str = "",
    call_type: str = "issue_filter",
) -> list[dict]:
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
        ai_cli_timeout: Timeout in minutes (overrides AI_CLI_TIMEOUT env var).
        job_id: Job identifier for token usage tracking.
        call_type: Token tracking call type label.

    Returns:
        List of dicts with ``key``, ``relevant`` (True), and ``score`` for
        relevant candidates only, sorted by score descending.
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

    result = await call_ai_cli(
        prompt,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        cli_flags=PROVIDER_CLI_FLAGS.get(ai_provider, []),
        output_format="json",
    )

    if job_id:
        await record_ai_usage(
            job_id=job_id,
            result=result,
            call_type=call_type,
            prompt_chars=len(prompt),
            ai_provider=ai_provider,
            ai_model=ai_model,
        )

    if not result.success:
        logger.warning("AI relevance filtering failed: %s", result.text)
        return []

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
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse AI relevance response")
        return []

    relevant: list[dict] = []
    for evaluation in evaluations:
        if not isinstance(evaluation, dict):
            continue
        key = evaluation.get("key", "")
        is_relevant = evaluation.get("relevant", False)
        try:
            score = float(evaluation.get("score", 0.0))
        except (ValueError, TypeError):
            score = 0.0

        if is_relevant and key:
            relevant.append({"key": key, "relevant": True, "score": score})

    relevant.sort(key=lambda m: m["score"], reverse=True)
    return relevant
