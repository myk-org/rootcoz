#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""GitHub interaction script for rootcoz chat sessions.

Provides CLI commands to search issues/PRs and fetch details from GitHub
repositories during AI chat sessions.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import httpx

GITHUB_API = "https://api.github.com"


def get_config() -> tuple[str, str, str]:
    """Return (token, owner, repo) from environment variables."""
    token = os.environ.get("ROOTCOZ_GITHUB_TOKEN", "")
    repo_full = os.environ.get("ROOTCOZ_GITHUB_REPO", "")

    if not token or not repo_full:
        print("GitHub is not configured for this chat session")
        sys.exit(1)

    if "/" not in repo_full:
        print("GitHub is not configured for this chat session")
        sys.exit(1)

    owner, repo = repo_full.split("/", 1)
    return token, owner, repo


def make_client(token: str) -> httpx.Client:
    """Create an httpx client with GitHub auth headers."""
    return httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )


def handle_response_error(resp: httpx.Response) -> None:
    """Check response status and print user-friendly errors."""
    if resp.status_code == 404:
        print("Issue/PR not found")
        sys.exit(1)
    if resp.status_code in (401, 403):
        print("GitHub authentication failed")
        sys.exit(1)
    resp.raise_for_status()


def format_date(date_str: str | None) -> str:
    """Format an ISO date string to a readable format."""
    if not date_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return date_str


def format_labels(labels: list[dict]) -> str:
    """Format a list of label dicts to a comma-separated string."""
    if not labels:
        return "None"
    return ", ".join(label.get("name", "") for label in labels)


def cmd_search(args: argparse.Namespace) -> None:
    """Search GitHub issues/PRs in the repository."""
    token, owner, repo = get_config()
    repo_full = f"{owner}/{repo}"

    query_parts = [args.query, f"repo:{repo_full}"]
    if args.state and args.state != "all":
        query_parts.append(f"state:{args.state}")

    query = "+".join(query_parts)

    with make_client(token) as client:
        resp = client.get(
            "/search/issues", params={"q": query, "per_page": args.max_results}
        )
        handle_response_error(resp)

    data = resp.json()
    items = data.get("items", [])
    total = data.get("total_count", 0)

    print(f"Search results for '{args.query}' in {repo_full}")
    print(f"Total matches: {total} (showing up to {args.max_results})")
    print("=" * 60)

    if not items:
        print("No results found.")
        return

    for i, item in enumerate(items, 1):
        item_type = "PR" if "pull_request" in item else "Issue"
        labels = format_labels(item.get("labels", []))
        print(
            f"\n{i}. [{item_type}] #{item['number']} — {item['title']}\n"
            f"   State: {item['state']}  |  Author: {item['user']['login']}\n"
            f"   Created: {format_date(item.get('created_at'))}\n"
            f"   Labels: {labels}\n"
            f"   URL: {item['html_url']}"
        )


def cmd_issue(args: argparse.Namespace) -> None:
    """Get details for a specific GitHub issue."""
    token, owner, repo = get_config()

    with make_client(token) as client:
        resp = client.get(f"/repos/{owner}/{repo}/issues/{args.number}")
        handle_response_error(resp)

    issue = resp.json()

    # GitHub API returns PRs via the issues endpoint too; detect that
    if "pull_request" in issue:
        print(f"Note: #{args.number} is a pull request, not an issue.\n")

    assignees = ", ".join(a["login"] for a in issue.get("assignees", [])) or "None"
    labels = format_labels(issue.get("labels", []))
    body = issue.get("body") or ""
    if len(body) > 2000:
        body = body[:2000] + "\n... (truncated)"

    print(f"Issue #{issue['number']}: {issue['title']}")
    print("=" * 60)
    print(f"State:      {issue['state']}")
    print(f"Author:     {issue['user']['login']}")
    print(f"Assignees:  {assignees}")
    print(f"Labels:     {labels}")
    print(f"Comments:   {issue.get('comments', 0)}")
    print(f"Created:    {format_date(issue.get('created_at'))}")
    print(f"Updated:    {format_date(issue.get('updated_at'))}")
    print(f"URL:        {issue['html_url']}")
    if body:
        print(f"\n--- Body ---\n{body}")


def cmd_pr(args: argparse.Namespace) -> None:
    """Get details for a specific GitHub pull request."""
    token, owner, repo = get_config()

    with make_client(token) as client:
        resp = client.get(f"/repos/{owner}/{repo}/pulls/{args.number}")
        handle_response_error(resp)

    pr = resp.json()

    body = pr.get("body") or ""
    if len(body) > 2000:
        body = body[:2000] + "\n... (truncated)"

    merged = pr.get("merged", False)
    merged_str = f"Yes (at {format_date(pr.get('merged_at'))})" if merged else "No"

    head = pr.get("head", {})
    base = pr.get("base", {})

    print(f"Pull Request #{pr['number']}: {pr['title']}")
    print("=" * 60)
    print(f"State:       {pr['state']}")
    print(f"Author:      {pr['user']['login']}")
    print(f"Head Branch: {head.get('label', 'N/A')}")
    print(f"Base Branch: {base.get('label', 'N/A')}")
    print(f"Merged:      {merged_str}")
    print(f"Created:     {format_date(pr.get('created_at'))}")
    print(f"Updated:     {format_date(pr.get('updated_at'))}")
    print(f"URL:         {pr['html_url']}")

    # Fetch review status
    with make_client(token) as client:
        reviews_resp = client.get(f"/repos/{owner}/{repo}/pulls/{args.number}/reviews")
    if reviews_resp.status_code == 200:
        reviews = reviews_resp.json()
        if reviews:
            # Show latest review per reviewer
            reviewer_states: dict[str, str] = {}
            for review in reviews:
                user = review.get("user", {}).get("login", "unknown")
                state = review.get("state", "")
                if state:
                    reviewer_states[user] = state
            if reviewer_states:
                print("\n--- Reviews ---")
                for reviewer, state in reviewer_states.items():
                    print(f"  {reviewer}: {state}")
        else:
            print("\nReviews:     No reviews yet")
    else:
        print("\nReviews:     Unable to fetch")

    if body:
        print(f"\n--- Body ---\n{body}")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="rootcoz-chat-github",
        description="Interact with GitHub issues and PRs for rootcoz chat sessions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search command
    search_parser = subparsers.add_parser(
        "search", help="Search GitHub issues/PRs in the repository"
    )
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument(
        "--state",
        choices=["open", "closed", "all"],
        default="all",
        help="Filter by state (default: all)",
    )
    search_parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="Maximum number of results (default: 10)",
    )
    search_parser.set_defaults(func=cmd_search)

    # issue command
    issue_parser = subparsers.add_parser(
        "issue", help="Get details for a specific GitHub issue"
    )
    issue_parser.add_argument("number", type=int, help="Issue number")
    issue_parser.set_defaults(func=cmd_issue)

    # pr command
    pr_parser = subparsers.add_parser(
        "pr", help="Get details for a specific GitHub pull request"
    )
    pr_parser.add_argument("number", type=int, help="Pull request number")
    pr_parser.set_defaults(func=cmd_pr)

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
