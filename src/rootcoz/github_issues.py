"""GitHub Issues integration for duplicate detection during analysis.

Two questions, two destinations:

- **code fix** — searches the tests repository for issues describing the
  test-side change (``CodeFix.tests_repo_matches``);
- **duplicate** — searches the run's own repositories for an already-filed
  issue matching a ``PRODUCT BUG`` (``ProductBugReport.github_matches``).

Both use the same transport (:func:`search_github_issues`) and the same shared
AI relevance filter (``issue_matching.filter_issue_matches_with_ai``).

Mirrors the Jira integration pattern in ``jira.py``.
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from rootcoz import get_child_logger
from rootcoz.bug_creation import parse_github_repo_url
from rootcoz.config import Settings, parse_repo_ref
from rootcoz.issue_matching import (
    MAX_FILTER_PROMPT_CHARS,
    TrackerSearch,
    build_filter_prompt,
    collect_product_bug_reports,
    filter_issue_matches_with_ai,
    search_failure_reason,
    tracker_search,
)
from rootcoz.models import (
    AnalysisDetail,
    CodeFix,
    FailureAnalysis,
    ProductBugReport,
    SimilarIssue,
)
from rootcoz.url_utils import strip_url_credentials

logger = get_child_logger(__name__)

# Classifications considered "code issues" for tests repo search
_CODE_ISSUE_CLASSIFICATIONS = frozenset({"CODE ISSUE"})

# AI token-tracking label for each question, kept distinct so spend per
# question stays readable in the usage report.
_CODE_FIX_CALL_TYPE = "tests_repo_filter"
_DUPLICATE_CALL_TYPE = "github_duplicate_filter"


class GitHubSearchError(RuntimeError):
    """A GitHub issue lookup could not be completed (transport or non-200).

    An empty result and a failed lookup must stay distinguishable: ``[]``
    means "searched, found nothing", this exception means "could not ask".
    """


async def search_github_issues(
    keywords: list[str],
    repo_url: str,
    github_token: str = "",
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Search GitHub Issues API for issues matching the given keywords.

    Args:
        keywords: Search terms to look for.
        repo_url: GitHub repository URL (e.g. https://github.com/owner/repo).
        github_token: Optional GitHub token for authenticated requests.
        max_results: Maximum number of results to return.

    Returns:
        List of candidate dicts with key, title, description, status, url.
        Empty list when there is nothing to search (no keywords, unparseable
        repository URL).

    Raises:
        GitHubSearchError: On a transport error or a non-200 response.
    """
    if not keywords:
        return []

    try:
        owner, repo = parse_github_repo_url(repo_url)
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
                raise GitHubSearchError(
                    f"GitHub Issues search returned status {resp.status_code}"
                )
            data = resp.json()
            candidates: list[dict[str, Any]] = []
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
    except GitHubSearchError:
        raise
    except Exception as exc:
        raise GitHubSearchError("GitHub Issues search failed") from exc


def run_repo_urls(
    tests_repo_url: str, additional_repos: Sequence[Any]
) -> tuple[list[str], int]:
    """GitHub repository URLs the duplicate search may search.

    The run's own repositories: the effective ``TESTS_REPO_URL`` plus every
    ``additional_repos`` entry, filtered to GitHub and deduplicated.

    Credentials are stripped *before* parsing: a clone URL may carry userinfo
    (a ``user:secret`` pair before the host), which the GitHub URL pattern
    rejects — stripping first keeps the repository searchable with the
    deployment credential and leaves nothing secret in the recorded scope.

    Returns:
        The searchable URLs and the number of configured entries that were not
        GitHub-searchable, so the record can name that instead of pretending
        nothing was configured.
    """
    raw_urls: list[str] = [tests_repo_url] if tests_repo_url else []
    for repo in additional_repos or ():
        url = repo.get("url") if isinstance(repo, dict) else getattr(repo, "url", "")
        raw_urls.append(str(url or ""))

    urls: list[str] = []
    skipped = 0
    for raw in raw_urls:
        without_ref, _ = parse_repo_ref(str(raw).strip())
        clean = strip_url_credentials(without_ref)
        try:
            owner, repo_name = parse_github_repo_url(clean)
        except ValueError:
            logger.debug(
                "Not a GitHub repository, skipping duplicate search: %s", clean
            )
            skipped += 1
            continue
        canonical = f"https://github.com/{owner}/{repo_name}"
        if canonical not in urls:
            urls.append(canonical)
    return urls, skipped


def duplicate_search_blocker(
    deployment: Settings,
    github_token: str,
    repo_urls: Sequence[str],
    skipped_repos: int = 0,
) -> str:
    """Why the GitHub duplicate search cannot run; ``""`` when it can.

    Read-only: the gate is ``enable_github_issues is not False`` (an explicit
    false disables it, the ``feedback_enabled`` precedent), a deployment token
    and at least one repository — never the issue-creation preconditions of
    ``Settings.github_issues_enabled``.
    """
    if deployment.enable_github_issues is False:
        return (
            "GitHub duplicate search is disabled on this deployment "
            "(ENABLE_GITHUB_ISSUES is false)"
        )
    if not github_token:
        return (
            "GitHub duplicate search cannot run: GITHUB_TOKEN is not "
            "configured on this deployment"
        )
    if not repo_urls:
        if skipped_repos:
            return (
                "GitHub duplicate search cannot run: "
                f"{skipped_repos} configured repository/repositories "
                "is/are not GitHub URLs"
            )
        return (
            "GitHub duplicate search cannot run: no repository is in scope "
            "(configure TESTS_REPO_URL or additional_repos)"
        )
    return ""


def _to_similar_issues(candidates: Sequence[dict[str, Any]]) -> list[SimilarIssue]:
    """Convert candidates to SimilarIssue objects, deduplicated by url."""
    seen: set[str] = set()
    matches: list[SimilarIssue] = []
    for candidate in candidates:
        url = candidate.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        matches.append(
            SimilarIssue(
                number=candidate.get("number"),
                title=candidate.get("title", ""),
                url=url,
                status=candidate.get("status", ""),
            )
        )
    return matches


def _evaluations_to_similar_issues(
    evaluations: Sequence[dict[str, Any]], candidates: Sequence[dict[str, Any]]
) -> list[SimilarIssue]:
    """Keep the relevant candidates, in the AI's order, deduplicated by url."""
    candidate_by_key = {c["key"]: c for c in candidates}
    return _to_similar_issues(
        [
            candidate_by_key[ev["key"]]
            for ev in evaluations
            if ev["key"] in candidate_by_key
        ]
    )


def _namespace_candidates(
    candidates: Sequence[dict[str, Any]], repo_url: str
) -> list[dict[str, Any]]:
    """Give each candidate a repository-unique ``key``.

    The shared AI filter uses ``key`` as the sole candidate identity, but
    GitHub issue numbers are per repository — ``repo-a#42`` and ``repo-b#42``
    would collide in one filter call. Returns copies; the key is internal and
    never reaches ``SimilarIssue``.
    """
    try:
        owner, repo = parse_github_repo_url(repo_url)
    except ValueError:
        return list(candidates)
    return [
        {
            **candidate,
            "key": f"{owner}/{repo}#{candidate.get('number', candidate['key'])}",
        }
        for candidate in candidates
    ]


async def _search_and_filter(
    targets_by_keywords: dict[tuple[str, ...], list[Any]],
    *,
    repo_urls: Sequence[str],
    github_token: str,
    bug_context: Callable[[Any], tuple[str, str]],
    attach: Callable[[Any, list[SimilarIssue]], None],
    call_type: str,
    ai_provider: str,
    ai_model: str,
    ai_call_timeout: int | None,
    job_id: str,
) -> TrackerSearch:
    """Search every repository per keyword set, filter, attach, record.

    Shared by both questions; the callers differ only in the keyword source,
    the repositories, the AI bug context, the ``call_type`` and where the
    matches land. Never raises: a failed search is counted, not propagated.
    """
    if not repo_urls:
        return tracker_search(
            reason=(
                "GitHub duplicate search cannot run: no GitHub repository is in scope"
            )
        )
    if not targets_by_keywords:
        return tracker_search(reason="no failure carried search keywords")

    keyword_sets = len(targets_by_keywords)
    total_searches = keyword_sets * len(repo_urls)

    async def _search_safe(
        keywords: tuple[str, ...], repo_url: str
    ) -> list[dict[str, Any]] | None:
        """One search; ``None`` marks a failure (distinct from an empty list)."""
        try:
            return await search_github_issues(list(keywords), repo_url, github_token)
        except Exception:
            logger.exception(
                "GitHub Issues search failed for %s with keywords: %s",
                repo_url,
                list(keywords),
            )
            return None

    search_results = await asyncio.gather(
        *(
            _search_safe(keywords, repo_url)
            for keywords in targets_by_keywords
            for repo_url in repo_urls
        )
    )

    failed_searches = 0
    filter_failures = 0
    oversized_filters = 0
    total_candidates = 0
    total_matches = 0
    for index, (keywords, targets) in enumerate(targets_by_keywords.items()):
        candidates: list[dict[str, Any]] = []
        for offset, repo_url in enumerate(repo_urls):
            found = search_results[index * len(repo_urls) + offset]
            if found is None:
                failed_searches += 1
                continue
            candidates.extend(_namespace_candidates(found, repo_url))
        total_candidates += len(candidates)
        if not candidates:
            continue

        bug_title, bug_description = bug_context(targets[0])
        if ai_provider and ai_model:
            prompt_chars = len(
                build_filter_prompt(bug_title, bug_description, candidates)
            )
            if prompt_chars > MAX_FILTER_PROMPT_CHARS:
                # Not sent: the overflow would come back as "no verdict" and be
                # recorded as a failed verdict instead of an unrun one.
                logger.warning(
                    "GitHub %s relevance filter not run for %d candidate(s) "
                    "for job=%s: prompt is %d chars, over the %d char limit",
                    call_type,
                    len(candidates),
                    job_id,
                    prompt_chars,
                    MAX_FILTER_PROMPT_CHARS,
                )
                oversized_filters += 1
                continue
            try:
                evaluations = await filter_issue_matches_with_ai(
                    bug_title=bug_title,
                    bug_description=bug_description,
                    candidates=candidates,
                    ai_provider=ai_provider,
                    ai_model=ai_model,
                    ai_call_timeout=ai_call_timeout,
                    job_id=job_id,
                    call_type=call_type,
                )
                # Conversion is inside the guard too: a malformed AI key must
                # cost this keyword set, not abandon the whole record.
                if evaluations is not None:
                    matches = _evaluations_to_similar_issues(evaluations, candidates)
            except Exception:
                # One failed filter must not discard the keyword sets after it:
                # their searches already succeeded.
                logger.exception(
                    "GitHub %s relevance filtering failed for %d candidate(s) "
                    "for job=%s",
                    call_type,
                    len(candidates),
                    job_id,
                )
                filter_failures += 1
                continue
            if evaluations is None:
                # The filter could not judge — not the same as judging every
                # candidate irrelevant.
                logger.warning(
                    "GitHub %s relevance filter returned no verdict for %d "
                    "candidate(s) for job=%s",
                    call_type,
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
                call_type,
            )
            matches = _to_similar_issues(candidates)

        for target in targets:
            attach(target, matches)
        total_matches += len(matches)

    # A failed search is a warning, a configuration reason is not.
    reason = search_failure_reason(
        search_label="GitHub issue search(es)",
        filter_label="GitHub relevance filter",
        failed_searches=failed_searches,
        total_searches=total_searches,
        filter_failures=filter_failures,
        prompt_overflows=oversized_filters,
    )
    if reason:
        logger.warning("GitHub %s: %s for job=%s", call_type, reason, job_id)
    logger.info(
        "GitHub %s complete for job=%s: %d keyword set(s), %d candidate(s), "
        "%d match(es)",
        call_type,
        job_id,
        keyword_sets,
        total_candidates,
        total_matches,
    )
    return tracker_search(
        searched=failed_searches < total_searches,
        reason=reason,
        scope=list(repo_urls),
        keyword_sets=keyword_sets,
        candidates=total_candidates,
        matches=total_matches,
    )


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


def _code_fix_bug_context(pair: tuple[FailureAnalysis, CodeFix]) -> tuple[str, str]:
    """AI context for the code-fix question: the proposed change and the test."""
    failure, code_fix = pair
    return (
        f"{code_fix.file}: {code_fix.change}",
        (
            f"Test: {failure.test_name}\n"
            f"Error: {failure.display_error}\n"
            f"Analysis: {failure.analysis.details}"
        ),
    )


async def enrich_with_tests_repo_matches(
    failures: Sequence[FailureAnalysis],
    settings: Settings,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
) -> None:
    """Search GitHub Issues for matching issues and attach results in-place.

    Collects CODE ISSUE failures with tests_repo_search_keywords,
    deduplicates by keyword set, searches the tests repository in parallel,
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

    def attach(
        pair: tuple[FailureAnalysis, CodeFix], matches: list[SimilarIssue]
    ) -> None:
        pair[1].tests_repo_matches = matches

    try:
        # This search answers the code-fix question, so it is not part of the
        # per-tracker duplicate record the caller stamps on the result.
        await _search_and_filter(
            keyword_to_pairs,
            repo_urls=[str(tests_repo_url)],
            github_token=github_token,
            bug_context=_code_fix_bug_context,
            attach=attach,
            call_type=_CODE_FIX_CALL_TYPE,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=settings.ai_call_timeout,
            job_id=job_id,
        )
    except Exception:
        logger.exception("GitHub Issues enrichment failed unexpectedly")


async def enrich_with_github_duplicate_matches(
    failures: Sequence[FailureAnalysis],
    settings: Settings,
    github_token: str,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
    repo_urls: Sequence[str] = (),
) -> TrackerSearch:
    """Search the run's repositories for an already-filed copy of each bug.

    Collects ``PRODUCT BUG`` failures carrying ``jira_search_keywords`` (the
    same population and the same tracker-neutral terms the Jira search uses),
    searches every repository in ``repo_urls`` in parallel, filters with the
    shared AI relevance step and attaches the survivors to
    ``ProductBugReport.github_matches``.

    Never raises: a broken tracker is recorded in the returned
    :class:`~rootcoz.issue_matching.TrackerSearch`, never propagated.

    Args:
        failures: Failure analyses to enrich (modified in-place).
        settings: Settings the AI call is configured from.
        github_token: The *deployment* token — read by the caller from
            ``config.get_settings()``, never from the request.
        ai_provider: AI provider for relevance filtering.
        ai_model: AI model for relevance filtering.
        job_id: Job identifier for token usage tracking.
        repo_urls: GitHub repository URLs in scope.
    """
    scope = list(repo_urls)
    blocker = duplicate_search_blocker(settings, github_token, scope)
    if blocker:
        logger.info("GitHub duplicate search not run for job=%s: %s", job_id, blocker)
        return tracker_search(reason=blocker, scope=scope)

    keyword_to_reports: dict[tuple[str, ...], list[ProductBugReport]] = {}
    for report in collect_product_bug_reports(failures):
        if not report.jira_search_keywords:
            continue
        key = tuple(sorted(report.jira_search_keywords))
        keyword_to_reports.setdefault(key, []).append(report)

    if not keyword_to_reports:
        reason = "no PRODUCT BUG failure carried search keywords"
        logger.info("GitHub duplicate search not run for job=%s: %s", job_id, reason)
        return tracker_search(reason=reason, scope=scope)

    logger.info(
        "Searching GitHub for %d unique keyword set(s) across %d repository(ies) "
        "for job=%s",
        len(keyword_to_reports),
        len(scope),
        job_id,
    )

    def attach(report: ProductBugReport, matches: list[SimilarIssue]) -> None:
        report.github_matches = matches

    try:
        return await _search_and_filter(
            keyword_to_reports,
            repo_urls=scope,
            github_token=github_token,
            bug_context=lambda report: (report.title, report.description),
            attach=attach,
            call_type=_DUPLICATE_CALL_TYPE,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_call_timeout=settings.ai_call_timeout,
            job_id=job_id,
        )
    except Exception:
        logger.exception("GitHub duplicate enrichment failed unexpectedly")
        return tracker_search(
            reason="GitHub duplicate search failed unexpectedly", scope=scope
        )
