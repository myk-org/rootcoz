#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Read-only SQL query tool for admin chat sessions.

Provides schema discovery and read-only query execution against the
rootcoz SQLite database. The AI uses this to answer any analytics question.

Environment variables (injected by workspace setup):
    ROOTCOZ_DB_PATH — path to the SQLite database file
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def _get_db_path() -> str:
    db_path = os.environ.get("ROOTCOZ_DB_PATH", "")
    if not db_path:
        print("Error: ROOTCOZ_DB_PATH not set", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(db_path):
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    return db_path


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """Connect to SQLite in read-only mode."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_schema(args: argparse.Namespace) -> None:
    """Print database schema — all tables with columns and types."""
    db_path = _get_db_path()
    conn = _connect_readonly(db_path)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row["name"] for row in cursor.fetchall()]

        for table in tables:
            print(f"\n=== {table} ===")
            cursor = conn.execute(f"PRAGMA table_info({table})")
            for col in cursor.fetchall():
                nullable = "" if col["notnull"] else " (nullable)"
                pk = " PRIMARY KEY" if col["pk"] else ""
                print(f"  {col['name']}: {col['type']}{pk}{nullable}")

            # Show row count
            count = conn.execute(f"SELECT COUNT(*) as c FROM [{table}]").fetchone()["c"]
            print(f"  ({count} rows)")
    finally:
        conn.close()


def cmd_query(args: argparse.Namespace) -> None:
    """Execute a read-only SQL query."""
    db_path = _get_db_path()
    sql = args.sql.strip()

    if not sql:
        print("Error: empty query", file=sys.stderr)
        sys.exit(1)

    # Basic safety check — block obvious write attempts
    # (the read-only connection already prevents writes at the SQLite level)
    first_word = sql.split()[0].upper() if sql.split() else ""
    if first_word in (
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "REPLACE",
    ):
        print(
            f"Error: write operations not allowed (got {first_word})",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = _connect_readonly(db_path)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchall()

        if not rows:
            print("No results.")
            return

        # Print as formatted table
        columns = [desc[0] for desc in cursor.description]
        print(" | ".join(columns))
        print("-" * len(" | ".join(columns)))
        for row in rows:
            values = [
                str(row[col]) if row[col] is not None else "NULL" for col in columns
            ]
            print(" | ".join(values))

        print(f"\n({len(rows)} rows)")
    except sqlite3.OperationalError as e:
        print(f"SQL Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="rootcoz-chat-db",
        description="Read-only SQL query tool for rootcoz database.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser(
        "schema", help="Show database schema (tables, columns, types)"
    )

    query_parser = subparsers.add_parser("query", help="Execute a read-only SQL query")
    query_parser.add_argument("sql", help="SQL query to execute")

    args = parser.parse_args()

    if args.command == "schema":
        cmd_schema(args)
    elif args.command == "query":
        cmd_query(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
