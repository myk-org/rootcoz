#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""CLI tool for interacting with Jira during rootcoz chat sessions.

Provides search, issue lookup, and related-failure commands using
environment variables injected by the chat workspace setup.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

# JQL reserved characters to strip from search keywords
_JQL_SPECIAL_CHARS = set(r""""'{}[]()~^&|!?*%+-:""")


def _sanitize_jql_keyword(keyword: str) -> str:
    """Strip JQL-reserved characters from a search keyword."""
    return "".join(c for c in keyword if c not in _JQL_SPECIAL_CHARS).strip()


def _get_jira_env() -> tuple[str, str | None, str]:
    """Return (jira_url, jira_email_or_none, jira_token) or exit."""
    jira_url = os.environ.get("ROOTCOZ_JIRA_URL", "").rstrip("/")
    jira_token = os.environ.get("ROOTCOZ_JIRA_TOKEN", "")

    if not jira_url or not jira_token:
        print("Jira is not configured for this chat session")
        sys.exit(1)

    jira_email = os.environ.get("ROOTCOZ_JIRA_EMAIL") or None
    return jira_url, jira_email, jira_token


def _jira_client(
    jira_url: str, jira_email: str | None, jira_token: str
) -> httpx.Client:
    """Build an httpx client with the right auth for Cloud vs Server/DC."""
    if jira_email:
        # Cloud: Basic auth (email:token), API v3
        auth = httpx.BasicAuth(username=jira_email, password=jira_token)
        return httpx.Client(base_url=jira_url, auth=auth, timeout=30)

    # Server/DC: Bearer token, API v2
    return httpx.Client(
        base_url=jira_url,
        headers={"Authorization": f"Bearer {jira_token}"},
        timeout=30,
    )


def _api_version(jira_email: str | None) -> str:
    return "3" if jira_email else "2"


def _search_path(jira_email: str | None) -> str:
    """Return the Jira search endpoint path (Cloud uses /jql suffix)."""
    if jira_email:
        return "/rest/api/3/search/jql"
    return "/rest/api/2/search"


def _extract_assignee(issue: dict, version: str) -> str:
    """Extract assignee display name from an issue dict."""
    assignee = issue.get("fields", {}).get("assignee")
    if not assignee:
        return "Unassigned"
    return assignee.get("displayName") or assignee.get("name", "Unassigned")


def _extract_reporter(issue: dict) -> str:
    reporter = issue.get("fields", {}).get("reporter")
    if not reporter:
        return "Unknown"
    return reporter.get("displayName") or reporter.get("name", "Unknown")


def _extract_description(issue: dict, version: str) -> str:
    """Extract description text, handling v3 ADF format."""
    desc = issue.get("fields", {}).get("description")
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc[:2000]
    # API v3 returns Atlassian Document Format (ADF)
    return _adf_to_text(desc)[:2000]


def _adf_to_text(node: dict | list | str) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(item) for item in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        children = node.get("content", [])
        text = "".join(_adf_to_text(child) for child in children)
        # Add newline after block-level nodes
        if node.get("type") in (
            "paragraph",
            "heading",
            "bulletList",
            "orderedList",
            "listItem",
            "blockquote",
            "codeBlock",
            "rule",
            "table",
            "tableRow",
            "tableCell",
            "tableHeader",
        ):
            text += "\n"
        return text
    return ""


# ── Commands ──────────────────────────────────────────────────────────


def cmd_search(args: argparse.Namespace) -> None:
    """Search Jira issues by text query."""
    jira_url, jira_email, jira_token = _get_jira_env()
    version = _api_version(jira_email)

    sanitized = _sanitize_jql_keyword(args.query)
    if not sanitized:
        print("Error: query is empty after sanitization")
        sys.exit(1)

    jql_parts = [f'text ~ "{sanitized}"']
    if args.project:
        jql_parts.append(f"project = {args.project}")
    if args.issue_type:
        jql_parts.append(f"issuetype = {args.issue_type}")

    jql = " AND ".join(jql_parts)

    with _jira_client(jira_url, jira_email, jira_token) as client:
        resp = client.get(
            _search_path(jira_email),
            params={
                "jql": jql,
                "maxResults": args.max_results,
                "fields": "summary,status,assignee,created",
            },
        )
        if resp.status_code != 200:
            print(f"Error: Jira API returned {resp.status_code}")
            print(resp.text)
            sys.exit(1)

        data = resp.json()
        issues = data.get("issues", [])
        total = data.get("total", 0)

    print(f"Search results for: {sanitized}")
    print(f"Total matches: {total} (showing {len(issues)})")
    print("-" * 60)

    if not issues:
        print("No issues found.")
        return

    for i, issue in enumerate(issues, 1):
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name", "Unknown")
        assignee = _extract_assignee(issue, version)
        created = fields.get("created", "")[:10]
        url = f"{jira_url}/browse/{key}"

        print(f"\n{i}. [{key}] {summary}")
        print(f"   Status: {status}  |  Assignee: {assignee}  |  Created: {created}")
        print(f"   URL: {url}")


def cmd_issue(args: argparse.Namespace) -> None:
    """Get details for a specific Jira issue."""
    jira_url, jira_email, jira_token = _get_jira_env()
    version = _api_version(jira_email)

    with _jira_client(jira_url, jira_email, jira_token) as client:
        resp = client.get(
            f"/rest/api/{version}/issue/{args.key}",
            params={
                "fields": "summary,status,priority,assignee,reporter,description,labels,components,created,updated",
            },
        )
        if resp.status_code != 200:
            print(f"Error: Jira API returned {resp.status_code}")
            print(resp.text)
            sys.exit(1)

        issue = resp.json()

    key = issue["key"]
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    status = fields.get("status", {}).get("name", "Unknown")
    priority = fields.get("priority", {})
    priority_name = priority.get("name", "Unknown") if priority else "Unknown"
    assignee = _extract_assignee(issue, version)
    reporter = _extract_reporter(issue)
    description = _extract_description(issue, version)
    labels = ", ".join(fields.get("labels", [])) or "None"
    components = (
        ", ".join(c.get("name", "") for c in fields.get("components", [])) or "None"
    )
    created = fields.get("created", "")[:10]
    updated = fields.get("updated", "")[:10]
    url = f"{jira_url}/browse/{key}"

    print(f"Issue: {key}")
    print("=" * 60)
    print(f"Summary:    {summary}")
    print(f"Status:     {status}")
    print(f"Priority:   {priority_name}")
    print(f"Assignee:   {assignee}")
    print(f"Reporter:   {reporter}")
    print(f"Labels:     {labels}")
    print(f"Components: {components}")
    print(f"Created:    {created}")
    print(f"Updated:    {updated}")
    print(f"URL:        {url}")
    print("-" * 60)
    print("Description:")
    if description:
        print(description)
    else:
        print("(no description)")


def cmd_related(args: argparse.Namespace) -> None:
    """Find Jira tickets related to a failure via its jira_matches."""
    server_url = os.environ.get("ROOTCOZ_SERVER_URL", "").rstrip("/")
    auth_token = os.environ.get("ROOTCOZ_AUTH_TOKEN", "")
    job_id = os.environ.get("ROOTCOZ_JOB_ID", "")

    if not server_url or not auth_token or not job_id:
        print(
            "Error: ROOTCOZ_SERVER_URL, ROOTCOZ_AUTH_TOKEN, and ROOTCOZ_JOB_ID must be set"
        )
        sys.exit(1)

    jira_url = os.environ.get("ROOTCOZ_JIRA_URL", "").rstrip("/")

    with httpx.Client(
        base_url=server_url,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=30,
    ) as client:
        resp = client.get(f"/results/{job_id}")
        if resp.status_code != 200:
            print(f"Error: rootcoz server returned {resp.status_code}")
            print(resp.text)
            sys.exit(1)

        result = resp.json()

    # Search for the failure UUID in top-level failures and child job failures
    failure = _find_failure(result, args.failure_uuid)
    if failure is None:
        print(f"Error: failure with UUID {args.failure_uuid} not found in job {job_id}")
        sys.exit(1)

    analysis = failure.get("analysis", {})
    bug_report = analysis.get("product_bug_report")
    if not bug_report or not isinstance(bug_report, dict):
        print("No Jira matches found for this failure")
        return

    matches = bug_report.get("jira_matches", [])
    if not matches:
        print("No Jira matches found for this failure")
        return

    test_name = failure.get("test_name", "Unknown")
    print(f"Jira matches for failure: {test_name}")
    print(f"Failure UUID: {args.failure_uuid}")
    print("=" * 60)

    for i, match in enumerate(matches, 1):
        key = match.get("key", "")
        summary = match.get("summary", "")
        status = match.get("status", "")
        priority = match.get("priority", "")
        score = match.get("score", 0.0)
        url = match.get("url", "")
        if not url and jira_url and key:
            url = f"{jira_url}/browse/{key}"

        print(f"\n{i}. [{key}] {summary}")
        print(f"   Status: {status}  |  Priority: {priority}  |  Score: {score:.2f}")
        if url:
            print(f"   URL: {url}")


def _find_failure(result: dict, failure_uuid: str) -> dict | None:
    """Find a failure by UUID in the analysis result."""
    # Check top-level failures
    for failure in result.get("failures", []):
        if failure.get("id") == failure_uuid:
            return failure

    # Check child job failures
    for child in result.get("child_job_analyses", []):
        for failure in child.get("failures", []):
            if failure.get("id") == failure_uuid:
                return failure

    return None


# ── CLI ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="rootcoz-chat-jira",
        description="Interact with Jira during rootcoz chat sessions",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search
    sp_search = subparsers.add_parser("search", help="Search Jira issues by text query")
    sp_search.add_argument("query", help="Text query to search for")
    sp_search.add_argument(
        "--project", default=None, help="Filter by project key (e.g., CNV)"
    )
    sp_search.add_argument(
        "--max-results", type=int, default=10, help="Max results (default: 10)"
    )
    sp_search.add_argument(
        "--issue-type", default=None, help="Filter by issue type (e.g., Bug)"
    )

    # issue
    sp_issue = subparsers.add_parser(
        "issue", help="Get details for a specific Jira issue"
    )
    sp_issue.add_argument("key", help="Jira issue key (e.g., CNV-12345)")

    # related
    sp_related = subparsers.add_parser(
        "related", help="Find Jira tickets related to a failure"
    )
    sp_related.add_argument("failure_uuid", help="Failure UUID from analysis result")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "issue":
        cmd_issue(args)
    elif args.command == "related":
        cmd_related(args)


if __name__ == "__main__":
    main()
