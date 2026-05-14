#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Query rootcoz job data during AI chat sessions.

This script is copied into the chat workspace at /tmp/rootcoz-chat-{job_id}/
and executed with `uv run` by the AI assistant. It provides read-only access
to job analysis results, failure details, comments, and test history.

Environment variables (injected by workspace setup):
    ROOTCOZ_SERVER_URL  — server base URL (e.g. http://localhost:8080)
    ROOTCOZ_AUTH_TOKEN   — Bearer token for authentication
    ROOTCOZ_JOB_ID       — the job ID being discussed
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


def _get_env() -> tuple[str, str, str]:
    """Read required environment variables or exit with a helpful message."""
    server_url = os.environ.get("ROOTCOZ_SERVER_URL", "").rstrip("/")
    auth_token = os.environ.get("ROOTCOZ_AUTH_TOKEN", "")
    job_id = os.environ.get("ROOTCOZ_JOB_ID", "")

    missing: list[str] = []
    if not server_url:
        missing.append("ROOTCOZ_SERVER_URL")
    if not auth_token:
        missing.append("ROOTCOZ_AUTH_TOKEN")
    if not job_id:
        missing.append("ROOTCOZ_JOB_ID")

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

    return server_url, auth_token, job_id


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _client(server_url: str, auth_token: str) -> httpx.Client:
    return httpx.Client(
        base_url=server_url,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=30.0,
    )


def _get(client: httpx.Client, path: str) -> Any:
    """GET *path* and return parsed JSON.  Prints error and exits on failure."""
    try:
        resp = client.get(path)
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


def _get_result(client: httpx.Client, job_id: str) -> dict[str, Any]:
    """Fetch job result, unwrapping the 'result' envelope."""
    data = _get(client, f"/results/{job_id}")
    return data.get("result", data) if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# Failure helpers
# ---------------------------------------------------------------------------


def _get_classification(failure: dict[str, Any]) -> str:
    analysis = failure.get("analysis")
    if isinstance(analysis, dict):
        return analysis.get("classification", "unknown")
    return "unknown"


def _get_error_message(failure: dict[str, Any]) -> str:
    analysis = failure.get("analysis")
    if isinstance(analysis, dict):
        return analysis.get("error", "") or ""
    return ""


def _has_analysis(failure: dict[str, Any]) -> bool:
    analysis = failure.get("analysis")
    return isinstance(analysis, dict) and bool(analysis)


def _collect_all_failures(
    data: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Collect all failures from top-level and child jobs.

    Returns a list of (scope_label, failure_dict) tuples.
    """
    results: list[tuple[str, dict[str, Any]]] = []

    job_name = data.get("job_name", "")
    build_number = data.get("build_number", 0)
    top_label = f"{job_name}#{build_number}" if job_name else "top-level"

    for f in data.get("failures", []):
        results.append((top_label, f))

    _collect_child_failures(data.get("child_job_analyses", []), results)
    return results


def _collect_child_failures(
    children: list[dict[str, Any]],
    results: list[tuple[str, dict[str, Any]]],
) -> None:
    for child in children:
        label = f"{child.get('job_name', '?')}#{child.get('build_number', 0)}"
        for f in child.get("failures", []):
            results.append((label, f))
        _collect_child_failures(child.get("failed_children", []), results)


def _find_failure_by_uuid(
    data: dict[str, Any], uuid: str
) -> tuple[str, dict[str, Any]] | None:
    """Find a failure by UUID across all levels."""
    for scope, failure in _collect_all_failures(data):
        if failure.get("id") == uuid:
            return scope, failure
    return None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_summary(client: httpx.Client, job_id: str, _args: argparse.Namespace) -> None:
    """Print job summary."""
    data = _get_result(client, job_id)

    all_failures = _collect_all_failures(data)
    classifications: dict[str, int] = {}
    for _, f in all_failures:
        cls = _get_classification(f)
        classifications[cls] = classifications.get(cls, 0) + 1

    print("=" * 60)
    print("JOB SUMMARY")
    print("=" * 60)
    print(f"  Job Name:      {data.get('job_name', 'N/A')}")
    print(f"  Build Number:  {data.get('build_number', 'N/A')}")
    print(f"  Status:        {data.get('status', 'N/A')}")
    print(f"  Job ID:        {job_id}")
    print(f"  AI Provider:   {data.get('ai_provider', 'N/A')}")
    print(f"  AI Model:      {data.get('ai_model', 'N/A')}")
    print(f"  Jenkins URL:   {data.get('jenkins_url', 'N/A')}")
    print()
    print(f"  Summary:       {data.get('summary', 'N/A')}")
    print()
    print(f"  Total Failures: {len(all_failures)}")

    if classifications:
        print()
        print("  Classifications:")
        for cls, count in sorted(classifications.items()):
            print(f"    {cls}: {count}")


def cmd_failures(client: httpx.Client, job_id: str, _args: argparse.Namespace) -> None:
    """List all failures."""
    data = _get_result(client, job_id)
    all_failures = _collect_all_failures(data)

    if not all_failures:
        print("No failures found in this job.")
        return

    print("=" * 60)
    print(f"FAILURES ({len(all_failures)} total)")
    print("=" * 60)

    for i, (scope, f) in enumerate(all_failures, 1):
        test_name = f.get("test_name", "unknown")
        uuid = f.get("id", "?")
        classification = _get_classification(f)
        error = _get_error_message(f)
        has_anal = _has_analysis(f)

        print(f"\n--- Failure {i} ---")
        print(f"  Test:           {test_name}")
        print(f"  UUID:           {uuid}")
        print(f"  Scope:          {scope}")
        print(f"  Classification: {classification}")
        print(f"  Has Analysis:   {'Yes' if has_anal else 'No'}")
        if error:
            truncated = error[:500]
            if len(error) > 500:
                truncated += "... (truncated, use `failure <uuid>` for full details)"
            print(f"  Error:          {truncated}")


def cmd_failure(client: httpx.Client, job_id: str, args: argparse.Namespace) -> None:
    """Print full details for a specific failure."""
    data = _get_result(client, job_id)
    match = _find_failure_by_uuid(data, args.uuid)

    if match is None:
        print(f"Error: no failure with UUID '{args.uuid}' found in this job.")
        sys.exit(1)

    scope, f = match
    analysis = f.get("analysis") or {}

    print("=" * 60)
    print("FAILURE DETAILS")
    print("=" * 60)
    print(f"  Test Name:      {f.get('test_name', 'unknown')}")
    print(f"  UUID:           {f.get('id', '?')}")
    print(f"  Scope:          {scope}")
    print(f"  Classification: {_get_classification(f)}")
    print(f"  Duration:       {f.get('duration', 'N/A')}")

    # Error
    error = analysis.get("error", "")
    if error:
        print(f"\n  Error:\n{_indent(error, 4)}")

    # Stack trace
    stack_trace = analysis.get("stack_trace", "")
    if stack_trace:
        print(f"\n  Stack Trace:\n{_indent(stack_trace, 4)}")

    # Root cause
    root_cause = analysis.get("root_cause", "")
    if root_cause:
        print(f"\n  Root Cause:\n{_indent(root_cause, 4)}")

    # Suggested fix
    suggested_fix = analysis.get("suggested_fix", "")
    if suggested_fix:
        print(f"\n  Suggested Fix:\n{_indent(suggested_fix, 4)}")

    # Confidence
    confidence = analysis.get("confidence", "")
    if confidence:
        print(f"\n  Confidence:     {confidence}")

    # Relevant code
    relevant_code = analysis.get("relevant_code", "")
    if relevant_code:
        print(f"\n  Relevant Code:\n{_indent(relevant_code, 4)}")

    # Failure history context
    history_context = analysis.get("failure_history_context", "")
    if history_context:
        print(f"\n  Failure History Context:\n{_indent(history_context, 4)}")

    # Peer debate
    peer_debate = analysis.get("peer_debate") or f.get("peer_debate")
    if peer_debate:
        print("\n  Peer Debate:")
        if isinstance(peer_debate, dict):
            _print_peer_debate(peer_debate)
        elif isinstance(peer_debate, str):
            print(_indent(peer_debate, 4))

    # Product bug report
    bug_report = analysis.get("product_bug_report") or f.get("product_bug_report")
    if bug_report and isinstance(bug_report, dict):
        print("\n  Product Bug Report:")
        _print_bug_report(bug_report)

    # Any remaining analysis fields
    printed_keys = {
        "classification",
        "error",
        "stack_trace",
        "root_cause",
        "suggested_fix",
        "confidence",
        "relevant_code",
        "failure_history_context",
        "peer_debate",
        "product_bug_report",
    }
    extra = {k: v for k, v in analysis.items() if k not in printed_keys and v}
    if extra:
        print("\n  Additional Analysis Fields:")
        for key, value in extra.items():
            if isinstance(value, str):
                print(f"    {key}:\n{_indent(value, 6)}")
            else:
                print(f"    {key}: {value}")


def _print_peer_debate(debate: dict[str, Any]) -> None:
    """Pretty-print a peer debate section."""
    for key in ("summary", "consensus", "disagreements"):
        val = debate.get(key)
        if val:
            print(f"    {key.title()}:\n{_indent(str(val), 6)}")

    peers = debate.get("peer_analyses") or debate.get("peers") or []
    if peers:
        print("    Peer Analyses:")
        for j, peer in enumerate(peers, 1):
            provider = peer.get("ai_provider", "?")
            model = peer.get("ai_model", "?")
            print(f"      Peer {j} ({provider}/{model}):")
            for field in (
                "classification",
                "root_cause",
                "suggested_fix",
                "confidence",
            ):
                val = peer.get(field)
                if val:
                    print(f"        {field}: {val}")

    # Print any other fields
    printed = {"summary", "consensus", "disagreements", "peer_analyses", "peers"}
    for key, val in debate.items():
        if key not in printed and val:
            print(f"    {key}: {val}")


def _print_bug_report(report: dict[str, Any]) -> None:
    """Pretty-print a product bug report section."""
    for field in ("is_product_bug", "title", "description", "severity", "component"):
        val = report.get(field)
        if val is not None:
            print(f"    {field}: {val}")

    jira_matches = report.get("jira_matches") or []
    if jira_matches:
        print("    Jira Matches:")
        for match in jira_matches:
            if isinstance(match, dict):
                key = match.get("key", "?")
                summary = match.get("summary", "")
                status = match.get("status", "")
                score = match.get("score", "")
                print(f"      {key}: {summary} [status={status}, score={score}]")
            else:
                print(f"      {match}")

    # Print any other fields
    printed = {
        "is_product_bug",
        "title",
        "description",
        "severity",
        "component",
        "jira_matches",
    }
    for key, val in report.items():
        if key not in printed and val:
            if isinstance(val, str):
                print(f"    {key}:\n{_indent(val, 6)}")
            else:
                print(f"    {key}: {val}")


def cmd_comments(client: httpx.Client, job_id: str, _args: argparse.Namespace) -> None:
    """Print all comments on this job."""
    data = _get(client, f"/results/{job_id}/comments")

    comments = data if isinstance(data, list) else data.get("comments", [])

    if not comments:
        print("No comments on this job.")
        return

    print("=" * 60)
    print(f"COMMENTS ({len(comments)})")
    print("=" * 60)

    for i, c in enumerate(comments, 1):
        print(f"\n--- Comment {i} ---")
        print(f"  Author:    {c.get('username', 'unknown')}")
        print(f"  Test:      {c.get('test_name', 'N/A')}")
        print(f"  Created:   {c.get('created_at', 'N/A')}")
        if c.get("child_job_name"):
            print(
                f"  Child Job: {c['child_job_name']}#{c.get('child_build_number', 0)}"
            )
        print(f"  Comment:\n{_indent(c.get('comment', ''), 4)}")


def cmd_history(client: httpx.Client, _job_id: str, args: argparse.Namespace) -> None:
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
            truncated = error[:300]
            if len(error) > 300:
                truncated += "..."
            print(f"  Error:          {truncated}")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _indent(text: str, spaces: int) -> str:
    """Indent every line of *text* by *spaces* spaces."""
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rootcoz-chat-job",
        description=(
            "Query rootcoz job data during chat sessions.\n\n"
            "Environment variables (set by workspace setup):\n"
            "  ROOTCOZ_SERVER_URL  — server base URL\n"
            "  ROOTCOZ_AUTH_TOKEN  — Bearer token\n"
            "  ROOTCOZ_JOB_ID     — job ID being discussed"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    sub.add_parser(
        "summary",
        help="Job summary (name, status, failure count, classifications)",
    )

    sub.add_parser(
        "failures",
        help="List all failures with test names, classifications, errors",
    )

    failure_parser = sub.add_parser(
        "failure",
        help="Full details for a specific failure (analysis, stack trace, root cause)",
    )
    failure_parser.add_argument(
        "uuid",
        help="UUID of the failure to inspect",
    )

    sub.add_parser(
        "comments",
        help="Comments on this job",
    )

    history_parser = sub.add_parser(
        "history",
        help="Failure history for a specific test across past jobs",
    )
    history_parser.add_argument(
        "test_name",
        help="Name of the test to look up history for",
    )

    return parser


COMMAND_HANDLERS = {
    "summary": cmd_summary,
    "failures": cmd_failures,
    "failure": cmd_failure,
    "comments": cmd_comments,
    "history": cmd_history,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    server_url, auth_token, job_id = _get_env()

    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    with _client(server_url, auth_token) as client:
        handler(client, job_id, args)


if __name__ == "__main__":
    main()
