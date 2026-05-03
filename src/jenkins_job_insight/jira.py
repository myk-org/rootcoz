"""Jira integration for product bug deduplication.

Searches Jira for existing issues that match product bug failures,
then uses AI to determine actual relevance, helping teams avoid
filing duplicate bug reports.
"""

import asyncio
import os
from collections.abc import Sequence

import httpx
from simple_logger.logger import get_logger

from jenkins_job_insight.config import Settings, _resolve_jira_auth
from jenkins_job_insight.issue_matching import filter_issue_matches_with_ai
from jenkins_job_insight.models import (
    AnalysisDetail,
    FailureAnalysis,
    JiraMatch,
    ProductBugReport,
)

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

# JQL reserved characters that need to be stripped from search keywords
_JQL_SPECIAL_CHARS = set(r'"\'{}[]()~^&|!?*%+-:')


def _sanitize_jql_keyword(keyword: str) -> str:
    """Strip JQL-reserved characters from a search keyword.

    Args:
        keyword: Raw keyword from AI output.

    Returns:
        Sanitized keyword safe for JQL text search.
    """
    return "".join(c for c in keyword if c not in _JQL_SPECIAL_CHARS).strip()


class JiraClient:
    """HTTP client for Jira REST API.

    Auto-detects Cloud (email + API token, REST API v3) vs
    Server/DC (PAT or API token, REST API v2) based on provided credentials.

    Cloud detection: ``jira_email`` present → Cloud mode.
    The token is ``jira_api_token`` (preferred) or ``jira_pat``
    (fallback).

    Server/DC detection (no ``jira_email``): uses Bearer auth
    with ``jira_pat`` (preferred) or ``jira_api_token`` as
    fallback.
    """

    def __init__(self, settings: Settings) -> None:
        self._base_url = (settings.jira_url or "").rstrip("/")
        self._project_key = settings.jira_project_key
        self._max_results = settings.jira_max_results

        is_cloud, token_value = _resolve_jira_auth(settings)

        # Configure Cloud vs Server/DC auth
        self._auth: tuple[str, str] | None
        if is_cloud and settings.jira_email:
            # Cloud: _resolve_jira_auth() selected email + token (api_token or pat).
            self._auth = (settings.jira_email, token_value)
            self._search_path = "/rest/api/3/search/jql"
            self._api_path = "/rest/api/3"
            self._headers: dict[str, str] = {}
        elif token_value:
            # Server/DC: _resolve_jira_auth() already selected the credential.
            self._auth = None
            self._search_path = "/rest/api/2/search"
            self._api_path = "/rest/api/2"
            self._headers = {"Authorization": f"Bearer {token_value}"}
        else:
            # No valid auth configured
            self._auth = None
            self._search_path = "/rest/api/2/search"
            self._api_path = "/rest/api/2"
            self._headers = {}

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=self._auth,
            headers=self._headers,
            verify=settings.jira_ssl_verify,
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def list_projects(self, query: str = "") -> list[dict]:
        """List all accessible Jira projects.

        Tries ``/project/search`` first (returns only projects the caller
        can see) and falls back to ``/project`` (requires broader
        permissions).  Paginates ``/project/search`` to fetch all results.
        """
        # /project/search returns paginated results scoped to the caller;
        # /project requires global Browse Projects and may 403 on Cloud.
        paths_to_try = [
            f"{self._api_path}/project/search",
            f"{self._api_path}/project",
        ]

        for path in paths_to_try:
            try:
                if "/project/search" in path:
                    return await self._list_projects_paginated(path, query=query)
                response = await self._client.get(path)
                response.raise_for_status()
                data = response.json()
                projects = data if isinstance(data, list) else data.get("values", [])
                result = [
                    {"key": p["key"], "name": p.get("name", p["key"])}
                    for p in projects
                    if isinstance(p, dict) and "key" in p
                ]
                if query:
                    _q = query.lower()
                    result = [
                        p
                        for p in result
                        if _q in p["key"].lower() or _q in p["name"].lower()
                    ]
                if result:
                    return result
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Failed to fetch Jira projects from %s: %s",
                    path,
                    exc,
                )
        return []

    async def _list_projects_paginated(self, path: str, query: str = "") -> list[dict]:
        """Fetch projects via paginated /project/search endpoint."""
        all_projects: list[dict] = []
        start_at = 0
        page_size = 50

        while True:
            params: dict = {"startAt": start_at, "maxResults": page_size}
            if query:
                params["query"] = query
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            data = response.json()
            values = data if isinstance(data, list) else data.get("values", [])
            for p in values:
                if isinstance(p, dict) and "key" in p:
                    all_projects.append(
                        {"key": p["key"], "name": p.get("name", p["key"])}
                    )
            # Use Jira pagination metadata when available
            if isinstance(data, dict) and "isLast" in data:
                if data["isLast"]:
                    break
                start_at = data.get("startAt", start_at) + data.get(
                    "maxResults", page_size
                )
            elif len(values) < page_size:
                break
            else:
                start_at += page_size

        return all_projects

    async def list_security_levels(self, project_key: str) -> list[dict]:
        """List available security levels for a Jira project."""
        if not project_key:
            return []
        try:
            response = await self._client.get(
                f"{self._api_path}/project/{project_key}/securitylevel"
            )
            response.raise_for_status()
            data = response.json()
            levels = data.get("levels", [])
            return [
                {
                    "id": lv["id"],
                    "name": lv["name"],
                    "description": lv.get("description", ""),
                }
                for lv in levels
                if isinstance(lv, dict) and "id" in lv and "name" in lv
            ]
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Failed to fetch security levels for project %s: %s",
                project_key,
                exc,
            )
            return []

    async def search(self, keywords: list[str]) -> list[dict]:
        """Search Jira for Bug issues matching the given keywords.

        Builds a JQL query using ``summary ~ "keyword"`` clauses joined
        with OR, filtered to ``issuetype = Bug``, optionally scoped to
        a project.

        Args:
            keywords: Search terms to look for in Jira issue summaries.

        Returns:
            List of dicts with key, summary, description, status, priority, url.
        """
        if not keywords:
            return []

        # Build JQL: summary ~ "kw1" OR summary ~ "kw2" ...
        text_clauses = " OR ".join(
            f'summary ~ "{_sanitize_jql_keyword(kw)}"' for kw in keywords
        )
        jql = f"issuetype = Bug AND ({text_clauses})"
        if self._project_key:
            jql = f'project = "{self._project_key}" AND {jql}'

        jql += " ORDER BY updated DESC"

        params = {
            "jql": jql,
            "maxResults": self._max_results,
            "fields": "summary,description,status,priority",
        }

        response = await self._client.get(
            self._search_path,
            params=params,
        )
        response.raise_for_status()
        data = response.json()

        candidates: list[dict] = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})

            # Extract status name (handles both Cloud and Server response shapes)
            status_obj = fields.get("status") or {}
            status_name = (
                status_obj.get("name", "") if isinstance(status_obj, dict) else ""
            )

            # Extract priority name
            priority_obj = fields.get("priority") or {}
            priority_name = (
                priority_obj.get("name", "") if isinstance(priority_obj, dict) else ""
            )

            # Extract description text
            desc = fields.get("description") or ""
            # Cloud API v3 returns description as ADF (Atlassian Document Format)
            if isinstance(desc, dict):
                desc = _extract_text_from_adf(desc)

            issue_key = issue.get("key", "")
            candidates.append(
                {
                    "key": issue_key,
                    "summary": fields.get("summary", ""),
                    "description": desc,
                    "status": status_name,
                    "priority": priority_name,
                    "url": f"{self._base_url}/browse/{issue_key}",
                }
            )

        return candidates


def _extract_text_from_adf(adf: dict) -> str:
    """Extract plain text from Atlassian Document Format (ADF).

    Jira Cloud API v3 returns descriptions as ADF JSON.
    This recursively extracts all text nodes.

    Args:
        adf: ADF document dict.

    Returns:
        Plain text content.
    """
    parts: list[str] = []

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                _walk(child)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(adf)
    return " ".join(parts)


async def _filter_matches_with_ai(
    bug_title: str,
    bug_description: str,
    candidates: list[dict],
    ai_provider: str,
    ai_model: str,
    ai_cli_timeout: int | None = None,
    job_id: str = "",
) -> list[JiraMatch]:
    """Use AI to determine which Jira candidates are relevant to the bug.

    Delegates to the shared :func:`filter_issue_matches_with_ai` and
    converts the results into Jira-specific ``JiraMatch`` objects.

    Args:
        bug_title: The product bug report title.
        bug_description: The product bug report description.
        candidates: List of candidate dicts from Jira search.
        ai_provider: AI provider name.
        ai_model: AI model identifier.
        ai_cli_timeout: Timeout in minutes (overrides AI_CLI_TIMEOUT env var).
        job_id: Job identifier for token usage tracking.

    Returns:
        List of JiraMatch objects for relevant candidates only.
    """
    # Ensure candidates have a consistent 'key' field for the shared filter
    evaluations = await filter_issue_matches_with_ai(
        bug_title=bug_title,
        bug_description=bug_description,
        candidates=candidates,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_cli_timeout=ai_cli_timeout,
        job_id=job_id,
        call_type="jira_filter",
    )

    # Convert evaluations to JiraMatch objects
    candidate_by_key = {c["key"]: c for c in candidates}
    relevant_matches: list[JiraMatch] = []
    for ev in evaluations:
        key = ev["key"]
        if key in candidate_by_key:
            c = candidate_by_key[key]
            relevant_matches.append(
                JiraMatch(
                    key=key,
                    summary=c["summary"],
                    status=c["status"],
                    priority=c["priority"],
                    url=c["url"],
                    score=ev["score"],
                )
            )

    return relevant_matches


def _collect_product_bug_reports(
    failures: Sequence[FailureAnalysis],
) -> list[ProductBugReport]:
    """Collect all ProductBugReport instances from a list of failures.

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


async def enrich_with_jira_matches(
    failures: Sequence[FailureAnalysis],
    settings: Settings,
    ai_provider: str = "",
    ai_model: str = "",
    job_id: str = "",
) -> None:
    """Search Jira for matching issues and attach results in-place.

    Collects PRODUCT BUG failures, deduplicates by keyword set,
    searches Jira in parallel, uses AI to filter relevant matches,
    and attaches results to each ``ProductBugReport.jira_matches``.

    This function never raises — all errors are logged and swallowed
    so the analysis pipeline is never interrupted.

    Args:
        failures: Failure analyses to enrich (modified in-place).
        settings: Application settings with Jira configuration.
        ai_provider: AI provider for relevance filtering.
        ai_model: AI model for relevance filtering.
        job_id: Job identifier for token usage tracking.
    """
    if not settings.jira_enabled:
        return

    reports = _collect_product_bug_reports(failures)
    if not reports:
        return

    # Deduplicate by keyword set — same keywords = one Jira search
    keyword_to_reports: dict[tuple[str, ...], list[ProductBugReport]] = {}
    for report in reports:
        if not report.jira_search_keywords:
            continue
        key = tuple(sorted(report.jira_search_keywords))
        keyword_to_reports.setdefault(key, []).append(report)

    if not keyword_to_reports:
        logger.debug(
            "No PRODUCT BUG failures with jira_search_keywords, skipping Jira lookup"
        )
        return

    logger.info(
        "Searching Jira for %d unique keyword set(s) across %d PRODUCT BUG failure(s)",
        len(keyword_to_reports),
        len(reports),
    )

    total_matches = 0
    async with JiraClient(settings) as client:
        try:
            # Search Jira for each unique keyword set in parallel
            async def _search_safe(keywords: list[str]) -> list[dict]:
                try:
                    return await client.search(keywords)
                except Exception:
                    logger.exception("Jira search failed for keywords: %s", keywords)
                    return []

            tasks = [_search_safe(list(kw_tuple)) for kw_tuple in keyword_to_reports]
            search_results = await asyncio.gather(*tasks)

            # AI relevance filtering for each keyword set
            for kw_tuple, candidates in zip(keyword_to_reports, search_results):
                if not candidates:
                    continue

                # Use the first report's title/description as context for AI filtering
                representative = keyword_to_reports[kw_tuple][0]

                if ai_provider and ai_model:
                    matches = await _filter_matches_with_ai(
                        bug_title=representative.title,
                        bug_description=representative.description,
                        candidates=candidates,
                        ai_provider=ai_provider,
                        ai_model=ai_model,
                        ai_cli_timeout=settings.ai_cli_timeout,
                        job_id=job_id,
                    )
                else:
                    # No AI config — fall back to returning all candidates as matches
                    logger.debug(
                        "No AI provider configured for Jira relevance filtering, returning all candidates"
                    )
                    matches = [
                        JiraMatch(
                            key=c["key"],
                            summary=c["summary"],
                            status=c["status"],
                            priority=c["priority"],
                            url=c["url"],
                            score=0.0,
                        )
                        for c in candidates
                    ]

                # Attach matches to all reports sharing the same keyword set
                for report in keyword_to_reports[kw_tuple]:
                    report.jira_matches = matches

                total_matches += len(matches)
            logger.info(
                "Jira search complete: %d relevant match(es) found", total_matches
            )

        except Exception:
            logger.exception("Jira enrichment failed unexpectedly")
