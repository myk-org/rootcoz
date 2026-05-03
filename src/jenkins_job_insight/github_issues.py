"""GitHub Issues integration for code issue deduplication.

Searches the tests repository's GitHub Issues for existing issues that
match code issue failures (AUTOMATION BUG, TEST CODE BUG, CODE ISSUE),
then uses AI to determine actual relevance, helping teams avoid filing
duplicate issues.

Mirrors the Jira integration pattern in ``jira.py``.
"""

import asyncio
import os
import re
from collections.abc import Sequence

import httpx
from simple_logger.logger import get_logger

from jenkins_job_insight.config import Settings
from jenkins_job_insight.issue_matching import filter_issue_matches_with_ai
from jenkins_job_insight.models import (
    AnalysisDetail,
    CodeFix,
    FailureAnalysis,
    SimilarIssue,
)

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

# Classifications considered "code issues" for tests repo search
_CODE_ISSUE_CLASSIFICATIONS = frozenset({"CODE ISSUE"})


def _parse_github_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract owner and repo from a GitHub repository URL.

    Reuses the same regex as ``bug_creation._parse_github_repo_url``
    but is defined here to avoid circular imports.

    Returns (owner, repo) tuple.

    Raises ValueError if the URL cannot be parsed.
    """
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    if not match:
        raise ValueError(f"Cannot parse GitHub repo URL: {repo_url}")
    return match.group(1), match.group(2)


async def search_github_issues(
    keywords: list[str],
    repo_url: str,
    github_token: str = "",
    max_results: int = 10,
) -> list[dict]:
    """Search GitHub Issues API for issues matching the given keywords.

    Args:
        keywords: Search terms to look for.
        repo_url: GitHub repository URL (e.g. https://github.com/owner/repo).
        github_token: Optional GitHub token for authenticated requests.
        max_results: Maximum number of results to return.

    Returns:
        List of candidate dicts with key, title, description, status, url.
        Returns empty list on any error.
    """
    if not keywords:
        return []

    try:
        owner, repo = _parse_github_repo_url(repo_url)
    except ValueError:
        logger.warning("Could not parse GitHub repo URL for issue search: %s", repo_url)
        return []

    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    # Build search query from keywords
    query_parts = " ".join(keywords)
    search_query = f"{query_parts} repo:{owner}/{repo} is:issue"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.github.com/search/issues",
                params={
                    "q": search_query,
                    "per_page": max_results,
                    "sort": "updated",
                    "order": "desc",
                },
                headers=headers,
            )
            if resp.status_code != 200:
                logger.debug(
                    "GitHub Issues search returned status %d", resp.status_code
                )
                return []
            data = resp.json()
            candidates: list[dict] = []
            for item in data.get("items", []):
                candidates.append(
                    {
                        "key": str(item["number"]),
                        "title": item.get("title", ""),
                        "summary": item.get("title", ""),
                        "description": item.get("body", "") or "",
                        "status": item.get("state", ""),
                        "url": item.get("html_url", ""),
                        "number": item["number"],
                    }
                )
            return candidates
    except Exception:
        logger.debug("GitHub Issues search failed", exc_info=True)
        return []


def _collect_code_fix_reports(
    failures: Sequence[FailureAnalysis],
) -> list[tuple[FailureAnalysis, CodeFix]]:
    """Collect all failures with CodeFix instances that have search keywords.

    Args:
        failures: List of failure analyses to scan.

    Returns:
        List of (failure, code_fix) tuples for code issue failures.
    """
    results: list[tuple[FailureAnalysis, CodeFix]] = []
    for failure in failures:
        detail: AnalysisDetail = failure.analysis
        if (
            isinstance(detail.code_fix, CodeFix)
            and detail.classification.upper() in _CODE_ISSUE_CLASSIFICATIONS
        ):
            results.append((failure, detail.code_fix))
    return results


async def enrich_with_tests_repo_matches(
    failures: Sequence[FailureAnalysis],
    settings: Settings,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
) -> None:
    """Search GitHub Issues for matching issues and attach results in-place.

    Collects CODE ISSUE failures with tests_repo_search_keywords,
    deduplicates by keyword set, searches GitHub in parallel,
    uses AI to filter relevant matches, and attaches results to
    each ``CodeFix.tests_repo_matches``.

    This function never raises — all errors are logged and swallowed
    so the analysis pipeline is never interrupted.

    Args:
        failures: Failure analyses to enrich (modified in-place).
        settings: Application settings with tests repo configuration.
        ai_provider: AI provider for relevance filtering.
        ai_model: AI model for relevance filtering.
        job_id: Job identifier for token usage tracking.
    """
    tests_repo_url = settings.tests_repo_url
    if not tests_repo_url:
        return

    github_token = (
        settings.github_token.get_secret_value() if settings.github_token else ""
    )

    code_fix_pairs = _collect_code_fix_reports(failures)
    if not code_fix_pairs:
        return

    # Deduplicate by keyword set — same keywords = one GitHub search
    keyword_to_pairs: dict[tuple[str, ...], list[tuple[FailureAnalysis, CodeFix]]] = {}
    for failure, code_fix in code_fix_pairs:
        if not code_fix.tests_repo_search_keywords:
            continue
        key = tuple(sorted(code_fix.tests_repo_search_keywords))
        keyword_to_pairs.setdefault(key, []).append((failure, code_fix))

    if not keyword_to_pairs:
        logger.debug(
            "No CODE ISSUE failures with tests_repo_search_keywords, "
            "skipping GitHub Issues lookup"
        )
        return

    logger.info(
        "Searching GitHub Issues for %d unique keyword set(s) across %d CODE ISSUE failure(s)",
        len(keyword_to_pairs),
        len(code_fix_pairs),
    )

    total_matches = 0
    try:
        # Search GitHub for each unique keyword set in parallel
        async def _search_safe(keywords: list[str]) -> list[dict]:
            try:
                return await search_github_issues(
                    keywords, tests_repo_url, github_token
                )
            except Exception:
                logger.exception(
                    "GitHub Issues search failed for keywords: %s", keywords
                )
                return []

        tasks = [_search_safe(list(kw_tuple)) for kw_tuple in keyword_to_pairs]
        search_results = await asyncio.gather(*tasks)

        # AI relevance filtering for each keyword set
        for kw_tuple, candidates in zip(keyword_to_pairs, search_results):
            if not candidates:
                continue

            # Use the first failure's details as context for AI filtering
            representative_failure, representative_fix = keyword_to_pairs[kw_tuple][0]

            if ai_provider and ai_model:
                evaluations = await filter_issue_matches_with_ai(
                    bug_title=f"{representative_fix.file}: {representative_fix.change}",
                    bug_description=(
                        f"Test: {representative_failure.test_name}\n"
                        f"Error: {representative_failure.error}\n"
                        f"Analysis: {representative_failure.analysis.details}"
                    ),
                    candidates=candidates,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    ai_cli_timeout=settings.ai_cli_timeout,
                    job_id=job_id,
                    call_type="tests_repo_filter",
                )

                # Convert evaluations to SimilarIssue objects
                candidate_by_key = {c["key"]: c for c in candidates}
                matches = [
                    SimilarIssue(
                        number=candidate_by_key[ev["key"]].get("number"),
                        title=candidate_by_key[ev["key"]].get("title", ""),
                        url=candidate_by_key[ev["key"]].get("url", ""),
                        status=candidate_by_key[ev["key"]].get("status", ""),
                    )
                    for ev in evaluations
                    if ev["key"] in candidate_by_key
                ]
            else:
                # No AI config — fall back to returning all candidates as matches
                logger.debug(
                    "No AI provider configured for GitHub Issues relevance filtering, "
                    "returning all candidates"
                )
                matches = [
                    SimilarIssue(
                        number=c.get("number"),
                        title=c.get("title", ""),
                        url=c.get("url", ""),
                        status=c.get("status", ""),
                    )
                    for c in candidates
                ]

            # Attach matches to all code_fix objects sharing the same keyword set
            for _failure, code_fix in keyword_to_pairs[kw_tuple]:
                code_fix.tests_repo_matches = matches

            total_matches += len(matches)

        logger.info(
            "GitHub Issues search complete: %d relevant match(es) found",
            total_matches,
        )

    except Exception:
        logger.exception("GitHub Issues enrichment failed unexpectedly")
