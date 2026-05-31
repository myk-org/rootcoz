#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Query rootcoz server-wide data during admin AI chat sessions.

This script is copied into the chat workspace and executed with `uv run`
by the AI assistant. It provides read-only access to cross-job data:
job listings, failure statistics, user activity, test history, and
server configuration.

Environment variables (injected by workspace setup):
    ROOTCOZ_SERVER_URL  — server base URL (e.g. http://localhost:8080)
    ROOTCOZ_AUTH_TOKEN   — Bearer token for authentication
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _get_env() -> tuple[str, str]:
    """Read required environment variables or exit with a helpful message."""
    server_url = os.environ.get("ROOTCOZ_SERVER_URL", "").rstrip("/")
    auth_token = os.environ.get("ROOTCOZ_AUTH_TOKEN", "")

    missing: list[str] = []
    if not server_url:
        missing.append("ROOTCOZ_SERVER_URL")
    if not auth_token:
        missing.append("ROOTCOZ_AUTH_TOKEN")

    if missing:
        print(
            f"Error: missing required environment variable(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "These are normally set by the rootcoz chat workspace setup.",
            file=sys.stderr,
        )
        sys.exit(1)

    return server_url, auth_token


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _client(server_url: str, auth_token: str) -> httpx.Client:
    return httpx.Client(
        base_url=server_url,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=30.0,
    )


def _get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    """GET *path* and return parsed JSON.  Prints error and exits on failure."""
    try:
        resp = client.get(path, params=params)
    except httpx.RequestError as exc:
        print(f"Error: cannot connect to server — {exc}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("detail", str(body))
        except Exception:
            detail = resp.text
        print(
            f"Error: API returned HTTP {resp.status_code}: {detail}",
            file=sys.stderr,
        )
        sys.exit(1)

    return resp.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_json(data: Any) -> None:
    """Pretty-print a JSON-serialisable object."""
    import json

    print(json.dumps(data, indent=2, default=str))


def _get_classification(failure: dict[str, Any]) -> str:
    analysis = failure.get("analysis")
    if isinstance(analysis, dict):
        return analysis.get("classification", "unknown")
    return "unknown"


def _collect_all_failures(
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Collect all failures from top-level and child jobs."""
    results: list[dict[str, Any]] = []
    for f in data.get("failures", []):
        results.append(f)
    _collect_child_failures(data.get("child_job_analyses", []), results)
    return results


def _collect_child_failures(
    children: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    for child in children:
        for f in child.get("failures", []):
            results.append(f)
        _collect_child_failures(child.get("failed_children", []), results)


def _unwrap_result(data: Any) -> dict[str, Any]:
    """Unwrap the 'result' envelope from a job response."""
    if isinstance(data, dict):
        return data.get("result", data)
    return data


def _fetch_job_details(client: httpx.Client, limit: int = 100) -> list[dict[str, Any]]:
    """Fetch full details for recent jobs (list endpoint only has summaries)."""
    data = _get(client, "/results", params={"limit": limit})
    job_list = data if isinstance(data, list) else data.get("results", [])
    jobs: list[dict[str, Any]] = []
    for j in job_list:
        jid = j.get("job_id", "")
        if jid:
            try:
                detail = _get(client, f"/results/{jid}")
                jobs.append(detail)
            except Exception:
                pass
    return jobs


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_list_jobs(client: httpx.Client, args: argparse.Namespace) -> None:
    """List recent jobs."""
    data = _get(client, "/results", params={"limit": args.limit})

    jobs = data if isinstance(data, list) else data.get("results", [])

    if not jobs:
        print("No jobs found.")
        return

    print("=" * 60)
    print(f"JOBS ({len(jobs)})")
    print("=" * 60)

    for i, job in enumerate(jobs, 1):
        print(f"\n--- Job {i} ---")
        print(f"  Job ID:       {job.get('job_id', 'N/A')}")
        print(f"  Job Name:     {job.get('job_name', 'N/A')}")
        print(f"  Build Number: {job.get('build_number', 'N/A')}")
        print(f"  Status:       {job.get('status', 'N/A')}")
        print(f"  Created At:   {job.get('created_at', 'N/A')}")


def cmd_get_job_summary(client: httpx.Client, args: argparse.Namespace) -> None:
    """Print summary for a specific job."""
    data = _get(client, f"/results/{args.job_id}")
    result = _unwrap_result(data)

    all_failures = _collect_all_failures(result)
    classifications: dict[str, int] = {}
    for f in all_failures:
        cls = _get_classification(f)
        classifications[cls] = classifications.get(cls, 0) + 1

    print("=" * 60)
    print("JOB SUMMARY")
    print("=" * 60)
    print(f"  Job Name:      {result.get('job_name', 'N/A')}")
    print(f"  Build Number:  {result.get('build_number', 'N/A')}")
    print(f"  Status:        {result.get('status', 'N/A')}")
    print(f"  Job ID:        {args.job_id}")
    print(f"  AI Provider:   {result.get('ai_provider', 'N/A')}")
    print(f"  AI Model:      {result.get('ai_model', 'N/A')}")
    print(f"  Jenkins URL:   {result.get('jenkins_url', 'N/A')}")
    print()
    print(f"  Summary:       {result.get('summary', 'N/A')}")
    print()
    print(f"  Total Failures: {len(all_failures)}")

    if classifications:
        print()
        print("  Classifications:")
        for cls, count in sorted(classifications.items()):
            print(f"    {cls}: {count}")


def cmd_failure_stats(client: httpx.Client, _args: argparse.Namespace) -> None:
    """Aggregate failure counts by classification across jobs."""
    jobs = _fetch_job_details(client)

    classifications: dict[str, int] = {}
    total_failures = 0
    jobs_with_failures = 0

    for job in jobs:
        result = _unwrap_result(job)
        failures = _collect_all_failures(result)
        if failures:
            jobs_with_failures += 1
        total_failures += len(failures)
        for f in failures:
            cls = _get_classification(f)
            classifications[cls] = classifications.get(cls, 0) + 1

    print("=" * 60)
    print("FAILURE STATISTICS")
    print("=" * 60)
    print(f"  Jobs scanned:       {len(jobs)}")
    print(f"  Jobs with failures: {jobs_with_failures}")
    print(f"  Total failures:     {total_failures}")

    if classifications:
        print()
        print("  By Classification:")
        for cls, count in sorted(classifications.items(), key=lambda x: -x[1]):
            pct = (count / total_failures * 100) if total_failures else 0
            print(f"    {cls}: {count} ({pct:.1f}%)")


def cmd_user_stats(client: httpx.Client, _args: argparse.Namespace) -> None:
    """Aggregate comments and reviews per username."""
    jobs = _fetch_job_details(client)

    user_comments: dict[str, int] = {}
    user_reviews: dict[str, int] = {}

    for job in jobs:
        job_id = job.get("job_id", "")
        if not job_id:
            continue

        # Fetch comments for each job
        try:
            comments_data = _get(client, f"/results/{job_id}/comments")
        except SystemExit:
            continue

        comments = (
            comments_data
            if isinstance(comments_data, list)
            else comments_data.get("comments", [])
        )

        for c in comments:
            username = c.get("username", "unknown")
            user_comments[username] = user_comments.get(username, 0) + 1

        # Count reviews from job result
        result = _unwrap_result(job)
        reviewed_by = result.get("reviewed_by", "")
        if reviewed_by:
            user_reviews[reviewed_by] = user_reviews.get(reviewed_by, 0) + 1

    print("=" * 60)
    print("USER STATISTICS")
    print("=" * 60)

    if user_comments:
        print()
        print("  Comments by User:")
        for username, count in sorted(user_comments.items(), key=lambda x: -x[1]):
            print(f"    {username}: {count}")
    else:
        print()
        print("  No comments found.")

    if user_reviews:
        print()
        print("  Reviews by User:")
        for username, count in sorted(user_reviews.items(), key=lambda x: -x[1]):
            print(f"    {username}: {count}")


def cmd_test_history(client: httpx.Client, args: argparse.Namespace) -> None:
    """Print failure history for a specific test."""
    data = _get(client, f"/history/test/{args.test_name}")

    entries = data if isinstance(data, list) else data.get("history", [])

    if not entries:
        print(f"No history found for test: {args.test_name}")
        return

    print("=" * 60)
    print(f"FAILURE HISTORY: {args.test_name}")
    print("=" * 60)

    if isinstance(data, dict):
        total = data.get("total", len(entries))
        print(f"  Total entries: {total}")

    for i, entry in enumerate(entries, 1):
        print(f"\n--- Entry {i} ---")
        print(f"  Job Name:       {entry.get('job_name', 'N/A')}")
        print(f"  Build Number:   {entry.get('build_number', 'N/A')}")
        print(f"  Job ID:         {entry.get('job_id', 'N/A')}")
        print(f"  Classification: {entry.get('classification', 'N/A')}")
        print(f"  Status:         {entry.get('status', 'N/A')}")
        print(f"  Analyzed At:    {entry.get('analyzed_at', 'N/A')}")
        error = entry.get("error", "")
        if error:
            print(f"  Error:          {error}")


def cmd_search_failures(client: httpx.Client, args: argparse.Namespace) -> None:
    """Search failure messages matching a query across jobs."""
    jobs = _fetch_job_details(client)
    query_lower = args.query.lower()
    matches: list[dict[str, Any]] = []

    for job in jobs:
        result = _unwrap_result(job)
        job_name = result.get("job_name", "N/A")
        build_number = result.get("build_number", "N/A")
        job_id = job.get("job_id", result.get("job_id", "N/A"))

        for f in _collect_all_failures(result):
            test_name = f.get("test_name", "")
            analysis = f.get("analysis")
            error_msg = ""
            if isinstance(analysis, dict):
                error_msg = analysis.get("error", "") or ""

            if query_lower in test_name.lower() or query_lower in error_msg.lower():
                matches.append(
                    {
                        "job_name": job_name,
                        "build_number": build_number,
                        "job_id": job_id,
                        "test_name": test_name,
                        "error": error_msg,
                        "classification": _get_classification(f),
                    }
                )

    if not matches:
        print(f"No failures matching '{args.query}' found.")
        return

    print("=" * 60)
    print(f"SEARCH RESULTS for '{args.query}' ({len(matches)} matches)")
    print("=" * 60)

    for i, m in enumerate(matches, 1):
        print(f"\n--- Match {i} ---")
        print(f"  Job:            {m['job_name']}#{m['build_number']}")
        print(f"  Job ID:         {m['job_id']}")
        print(f"  Test:           {m['test_name']}")
        print(f"  Classification: {m['classification']}")
        if m["error"]:
            print(f"  Error:          {m['error']}")


def cmd_server_settings(client: httpx.Client, _args: argparse.Namespace) -> None:
    """Show non-sensitive server configuration."""
    data = _get(client, "/admin/settings")

    print("=" * 60)
    print("SERVER SETTINGS")
    print("=" * 60)

    if isinstance(data, dict):
        categories = data.get("categories", data)
        if isinstance(categories, list):
            for category in categories:
                cat_name = category.get("name", "Unknown")
                settings = category.get("settings", [])
                print(f"\n  [{cat_name}]")
                for setting in settings:
                    name = setting.get("name", "?")
                    value = setting.get("value", "")
                    sensitive = setting.get("sensitive", False)
                    if sensitive:
                        print(f"    {name}: ********")
                    else:
                        print(f"    {name}: {value}")
        else:
            _print_json(data)
    else:
        _print_json(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rootcoz-chat-server",
        description=(
            "Query rootcoz server-wide data during admin chat sessions.\n\n"
            "Environment variables (set by workspace setup):\n"
            "  ROOTCOZ_SERVER_URL  — server base URL\n"
            "  ROOTCOZ_AUTH_TOKEN  — Bearer token"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    list_jobs_parser = sub.add_parser(
        "list-jobs",
        help="List recent jobs (job_id, name, build_number, status, created_at)",
    )
    list_jobs_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of jobs to return (default: 20)",
    )

    get_job_parser = sub.add_parser(
        "get-job-summary",
        help="Summary for a specific job (name, status, failures, classifications)",
    )
    get_job_parser.add_argument(
        "job_id",
        help="Job ID to inspect",
    )

    sub.add_parser(
        "failure-stats",
        help="Aggregate failure counts by classification across recent jobs",
    )

    sub.add_parser(
        "user-stats",
        help="Aggregate comments and reviews per username across recent jobs",
    )

    history_parser = sub.add_parser(
        "test-history",
        help="Failure history for a specific test across past jobs",
    )
    history_parser.add_argument(
        "test_name",
        help="Name of the test to look up history for",
    )

    search_parser = sub.add_parser(
        "search-failures",
        help="Search failure messages matching a query across recent jobs",
    )
    search_parser.add_argument(
        "query",
        help="Search query to match against test names and error messages",
    )

    sub.add_parser(
        "server-settings",
        help="Show non-sensitive server configuration (admin only)",
    )

    return parser


COMMAND_HANDLERS: dict[str, Any] = {
    "list-jobs": cmd_list_jobs,
    "get-job-summary": cmd_get_job_summary,
    "failure-stats": cmd_failure_stats,
    "user-stats": cmd_user_stats,
    "test-history": cmd_test_history,
    "search-failures": cmd_search_failures,
    "server-settings": cmd_server_settings,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    server_url, auth_token = _get_env()

    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    with _client(server_url, auth_token) as client:
        handler(client, args)


if __name__ == "__main__":
    main()
