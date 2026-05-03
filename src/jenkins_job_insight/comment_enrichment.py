"""Comment enrichment: detect and fetch status for GitHub PRs and Jira tickets in comments."""

import os
import re

import httpx
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

_GITHUB_PR_PATTERN = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")

_GITHUB_ISSUE_PATTERN = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)")

_JIRA_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

_MENTION_PATTERN = re.compile(r"(?<![a-zA-Z0-9.])@([a-zA-Z0-9_-]+)")


def _detect_github_links(text: str, pattern: re.Pattern, label: str) -> list[dict]:
    """Detect GitHub links matching a pattern.

    Args:
        text: The comment text to scan.
        pattern: Compiled regex with (owner, repo, number) groups.
        label: Label for debug logging.

    Returns:
        List of dicts with 'owner', 'repo', and 'number' keys.
    """
    matches = pattern.findall(text)
    result = [{"owner": m[0], "repo": m[1], "number": int(m[2])} for m in matches]
    logger.debug(f"{label}: found={len(result)}")
    return result


def detect_github_prs(text: str) -> list[dict]:
    """Detect GitHub PR URLs in text."""
    return _detect_github_links(text, _GITHUB_PR_PATTERN, "detect_github_prs")


def detect_github_issues(text: str) -> list[dict]:
    """Detect GitHub issue URLs in text."""
    return _detect_github_links(text, _GITHUB_ISSUE_PATTERN, "detect_github_issues")


def detect_jira_keys(text: str) -> list[str]:
    """Detect Jira ticket keys in text.

    Args:
        text: The comment text to scan.

    Returns:
        List of Jira ticket keys (e.g. ['OCPBUGS-12345', 'CNV-200']).
    """
    keys = _JIRA_KEY_PATTERN.findall(text)
    logger.debug(f"detect_jira_keys: found={len(keys)}")
    return keys


def detect_mentions(text: str) -> list[str]:
    """Detect @username mentions in text.

    Returns a deduplicated list of mentioned usernames (without the @ prefix).
    Email addresses (e.g. user@domain.com) are excluded.

    Args:
        text: The comment text to scan.

    Returns:
        List of unique usernames in order of first occurrence.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _MENTION_PATTERN.finditer(text):
        username = match.group(1)
        if username not in seen:
            seen.add(username)
            result.append(username)
    logger.debug(f"detect_mentions: found={len(result)}")
    return result


async def fetch_github_pr_status(
    owner: str,
    repo: str,
    number: int,
    token: str | None = None,
) -> str | None:
    """Fetch the status of a GitHub PR.

    Args:
        owner: Repository owner.
        repo: Repository name.
        number: Pull request number.
        token: Optional GitHub personal access token for authentication.

    Returns:
        'open', 'closed', 'merged', or None if fetch fails.
    """
    logger.debug(f"fetch_github_pr_status: owner={owner}, repo={repo}, number={number}")
    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}",
                headers=headers,
            )
            if resp.status_code != 200:
                logger.debug(
                    f"fetch_github_pr_status: {owner}/{repo}#{number} returned status {resp.status_code}"
                )
                return None
            data = resp.json()
            if data.get("merged"):
                logger.debug(
                    f"fetch_github_pr_status: {owner}/{repo}#{number} status=merged"
                )
                return "merged"
            status = data.get("state")
            logger.debug(
                f"fetch_github_pr_status: {owner}/{repo}#{number} status={status}"
            )
            return status
    except Exception:
        logger.debug(
            f"fetch_github_pr_status: {owner}/{repo}#{number} failed", exc_info=True
        )
        return None


async def fetch_github_issue_status(
    owner: str,
    repo: str,
    number: int,
    *,
    token: str | None = None,
) -> str | None:
    """Fetch the status of a GitHub issue.

    Args:
        owner: Repository owner.
        repo: Repository name.
        number: Issue number.
        token: Optional GitHub personal access token for authentication.

    Returns:
        Lowercase state string (e.g. 'open', 'closed') or None if fetch fails.
    """
    logger.debug(
        f"fetch_github_issue_status: owner={owner}, repo={repo}, number={number}"
    )
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug(
                    f"fetch_github_issue_status: {owner}/{repo}#{number} returned status {resp.status_code}"
                )
                return None
            data = resp.json()
            status = data.get("state", "unknown")
            logger.debug(
                f"fetch_github_issue_status: {owner}/{repo}#{number} status={status}"
            )
            return status
    except Exception:
        logger.debug(
            f"fetch_github_issue_status: {owner}/{repo}#{number} failed", exc_info=True
        )
        return None


def _extract_status_from_issues(data: dict) -> str | None:
    """Extract the status name from a Jira search response.

    Args:
        data: Parsed JSON response from the Jira search API.

    Returns:
        Status name string or None if not found.
    """
    issues = data.get("issues", [])
    if issues:
        status_obj = issues[0].get("fields", {}).get("status", {})
        return status_obj.get("name") if isinstance(status_obj, dict) else None
    return None


async def fetch_jira_ticket_status(
    jira_url: str,
    ticket_key: str,
    auth_headers: dict[str, str],
    ssl_verify: bool = True,
    auth: tuple[str, str] | None = None,
) -> str | None:
    """Fetch Jira ticket status by key using direct REST API with JQL key lookup.

    Tries the Cloud API v3 endpoint first (``/rest/api/3/search/jql``), then
    falls back to the legacy v2 endpoint (``/rest/api/2/search``) for
    Server/Data Center instances.

    Args:
        jira_url: Base Jira URL (e.g. 'https://jira.example.com').
        ticket_key: Jira ticket key (e.g. 'OCPBUGS-12345').
        auth_headers: Authorization headers for Jira API (used when *auth* is None).
        ssl_verify: Whether to verify SSL certificates.
        auth: Optional ``(username, token)`` tuple for httpx Basic auth (Cloud).
              When provided, *auth_headers* are ignored for authentication.

    Returns:
        Status string (e.g. 'Open', 'Closed') or None if fetch fails.
    """
    logger.debug(
        f"fetch_jira_ticket_status: jira_url={jira_url}, ticket_key={ticket_key}"
    )
    base = jira_url.rstrip("/")
    jql = f'key = "{ticket_key}"'
    search_params = {"jql": jql, "maxResults": 1, "fields": "status"}

    # Endpoints to try in order: Cloud v3 first, then Server/DC v2
    endpoints = [
        f"{base}/rest/api/3/search/jql",
        f"{base}/rest/api/2/search",
    ]

    try:
        async with httpx.AsyncClient(
            verify=ssl_verify, timeout=10, auth=auth, headers=auth_headers
        ) as client:
            for i, url in enumerate(endpoints):
                resp = await client.get(url, params=search_params)
                if resp.status_code == 200:
                    status = _extract_status_from_issues(resp.json())
                    logger.debug(
                        f"fetch_jira_ticket_status: ticket_key={ticket_key}, status={status}"
                    )
                    return status
                logger.debug(
                    "Jira endpoint %s returned status %d", url, resp.status_code
                )
                # If this is the last endpoint, give up
                if i == len(endpoints) - 1:
                    return None
                # Otherwise, try the next endpoint
    except Exception:
        logger.debug(
            "Jira ticket status fetch failed for %s", ticket_key, exc_info=True
        )
        return None
    return None
