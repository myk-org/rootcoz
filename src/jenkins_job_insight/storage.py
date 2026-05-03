"""SQLite storage for analysis results."""

import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from typing import get_args

import aiosqlite
from simple_logger.logger import get_logger

from jenkins_job_insight.comment_enrichment import detect_mentions
from jenkins_job_insight.encryption import (
    get_hmac_secret,
    strip_sensitive_from_response,
)
from jenkins_job_insight.models import (
    HistoryClassificationLiteral,
    OverrideClassificationLiteral,
)

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

DB_PATH = Path(os.getenv("DB_PATH", "/data/results.db"))

# Primary (override) classifications — derived from the OverrideClassificationLiteral
# type so the SQL filter stays in sync with the model definition.
PRIMARY_CLASSIFICATIONS: tuple[str, ...] = get_args(OverrideClassificationLiteral)

# History classifications — derived from HistoryClassificationLiteral so the
# write-side validation in set_test_classification stays in sync with the model.
HISTORY_CLASSIFICATIONS: tuple[str, ...] = get_args(HistoryClassificationLiteral)
_PRIMARY_CLASSIFICATIONS_SQL = (
    "(" + ", ".join(f"'{c}'" for c in PRIMARY_CLASSIFICATIONS) + ")"
)
_HISTORY_CLASSIFICATIONS_SQL = (
    "(" + ", ".join(f"'{c}'" for c in HISTORY_CLASSIFICATIONS) + ")"
)

# --- Auth constants and helpers ---
SESSION_TTL_HOURS = 8
SESSION_TTL_SECONDS = SESSION_TTL_HOURS * 3600
MIN_KEY_LENGTH = 16


def validate_api_key(key: str) -> None:
    """Validate API key meets minimum requirements."""
    if len(key) < MIN_KEY_LENGTH:
        msg = f"API key must be at least {MIN_KEY_LENGTH} characters long"
        raise ValueError(msg)


def hash_api_key(key: str) -> str:
    """Hash an API key with HMAC-SHA256 for storage.

    Uses the encryption key (JJI_ENCRYPTION_KEY) as the HMAC secret,
    which is stable across ADMIN_KEY rotations.

    Args:
        key: The raw API key to hash.

    Returns:
        Hex-encoded HMAC-SHA256 digest.
    """
    secret = get_hmac_secret()
    return hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()


def generate_api_key() -> str:
    """Generate a random API key."""
    return f"jji_{secrets.token_urlsafe(32)}"


def _hash_session_token(token: str) -> str:
    """Hash a session token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def parse_result_json(raw: str | None, *, job_id: str = "") -> dict | None:
    """Decode and validate a ``result_json`` blob.

    Args:
        raw: The raw JSON string from the database, or None.
        job_id: Optional job_id for log messages.

    Returns:
        Parsed dict when valid, None when *raw* is empty/malformed.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            f"parse_result_json: malformed JSON for job_id={job_id}, skipping"
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            f"parse_result_json: result_json is not a dict for job_id={job_id}, skipping"
        )
        return None
    return data


async def _migrate_add_column(
    db: aiosqlite.Connection,
    table: str,
    column: str,
    column_def: str,
) -> None:
    """Add a column to a table if it does not already exist.

    Args:
        db: Active database connection.
        table: Table name.
        column: Column name to check/add.
        column_def: Full column definition (e.g. "TEXT NOT NULL DEFAULT ''").
    """
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in await cursor.fetchall()}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
        logger.info(f"Migration: added {column} column to {table}")
    else:
        logger.debug(f"Migration: {table} already has {column} column")


async def init_db() -> None:
    """Initialize the database schema.

    Creates the results table if it does not exist.
    """
    logger.info(f"Initializing database at {DB_PATH}")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                job_id TEXT PRIMARY KEY,
                jenkins_url TEXT,
                status TEXT,
                result_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                analysis_started_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                test_name TEXT NOT NULL,
                child_job_name TEXT NOT NULL DEFAULT '',
                child_build_number INTEGER NOT NULL DEFAULT 0,
                comment TEXT NOT NULL,
                error_signature TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_job_id ON comments (job_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_test_name ON comments (test_name)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_error_signature ON comments (error_signature)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS failure_reviews (
                job_id TEXT NOT NULL,
                test_name TEXT NOT NULL,
                child_job_name TEXT NOT NULL DEFAULT '',
                child_build_number INTEGER NOT NULL DEFAULT 0,
                reviewed BOOLEAN DEFAULT 0,
                username TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (job_id, test_name, child_job_name, child_build_number)
            )
        """)

        # Migration: add child_build_number to existing tables
        # (needed when upgrading from versions without this column)
        logger.info("Running database migrations...")
        for table in ("comments", "failure_reviews"):
            await _migrate_add_column(
                db, table, "child_build_number", "INTEGER NOT NULL DEFAULT 0"
            )

        # Migration: add username to comments and failure_reviews
        for table in ("comments", "failure_reviews"):
            await _migrate_add_column(db, table, "username", "TEXT NOT NULL DEFAULT ''")

        # Migration: add error_signature to comments table
        await _migrate_add_column(
            db, "comments", "error_signature", "TEXT NOT NULL DEFAULT ''"
        )

        # Migration: rebuild failure_reviews with correct 4-column PRIMARY KEY
        # ALTER TABLE cannot change PKs in SQLite, so we need a full rebuild
        cursor = await db.execute("PRAGMA table_info(failure_reviews)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "child_build_number" in columns:
            # Check if PK includes child_build_number by inspecting table SQL
            cursor = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='failure_reviews'"
            )
            create_sql = (await cursor.fetchone())[0]
            if (
                "child_build_number" not in create_sql.split("PRIMARY KEY")[1]
                if "PRIMARY KEY" in create_sql
                else ""
            ):
                logger.info(
                    "Migration: rebuilding failure_reviews table with 4-column PRIMARY KEY"
                )
                await db.execute(
                    "ALTER TABLE failure_reviews RENAME TO failure_reviews_old"
                )
                await db.execute("""
                    CREATE TABLE failure_reviews (
                        job_id TEXT NOT NULL,
                        test_name TEXT NOT NULL,
                        child_job_name TEXT NOT NULL DEFAULT '',
                        child_build_number INTEGER NOT NULL DEFAULT 0,
                        reviewed BOOLEAN DEFAULT 0,
                        username TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (job_id, test_name, child_job_name, child_build_number)
                    )
                """)
                await db.execute("""
                    INSERT INTO failure_reviews (job_id, test_name, child_job_name, child_build_number, reviewed, username, updated_at)
                    SELECT job_id, test_name, child_job_name, child_build_number, reviewed, COALESCE(username, ''), updated_at
                    FROM failure_reviews_old
                """)
                await db.execute("DROP TABLE failure_reviews_old")
                logger.info("Migration: failure_reviews table rebuilt successfully")

        # Ensure test_classifications table exists before running migrations.
        # On a fresh DB the table may not exist yet, so CREATE TABLE must come
        # before any ALTER TABLE migrations.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS test_classifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_name TEXT NOT NULL,
                job_name TEXT NOT NULL DEFAULT '',
                parent_job_name TEXT NOT NULL DEFAULT '',
                classification TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                references_info TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                job_id TEXT NOT NULL DEFAULT '',
                child_build_number INTEGER NOT NULL DEFAULT 0,
                visible INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrations: add columns to test_classifications table
        await _migrate_add_column(
            db, "test_classifications", "parent_job_name", "TEXT NOT NULL DEFAULT ''"
        )
        await _migrate_add_column(
            db, "test_classifications", "references_info", "TEXT NOT NULL DEFAULT ''"
        )
        await _migrate_add_column(
            db, "test_classifications", "job_id", "TEXT NOT NULL DEFAULT ''"
        )
        await _migrate_add_column(
            db, "test_classifications", "visible", "INTEGER NOT NULL DEFAULT 1"
        )
        await _migrate_add_column(
            db,
            "test_classifications",
            "child_build_number",
            "INTEGER NOT NULL DEFAULT 0",
        )

        # Migrations: add columns to results table
        await _migrate_add_column(db, "results", "completed_at", "TIMESTAMP")
        await _migrate_add_column(db, "results", "analysis_started_at", "TIMESTAMP")

        # failure_history: denormalized table for fast history queries
        await db.execute("""
            CREATE TABLE IF NOT EXISTS failure_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                job_name TEXT NOT NULL,
                build_number INTEGER NOT NULL,
                test_name TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT '',
                error_signature TEXT NOT NULL DEFAULT '',
                classification TEXT NOT NULL DEFAULT '',
                child_job_name TEXT NOT NULL DEFAULT '',
                child_build_number INTEGER NOT NULL DEFAULT 0,
                analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_test_name ON failure_history (test_name)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_error_signature ON failure_history (error_signature)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_job_name ON failure_history (job_name)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_analyzed_at ON failure_history (analyzed_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_job_test ON failure_history (job_name, test_name)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_job_context ON failure_history (job_id, test_name, child_job_name, child_build_number)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_classification ON failure_history (classification)"
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tc_test_name ON test_classifications (test_name)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tc_job_id_visible ON test_classifications (job_id, visible)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tc_classification ON test_classifications (classification)"
        )

        # Users table — tracks all users (regular and admin)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                api_key_hash TEXT UNIQUE,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migration: add encrypted token columns to users table
        for col in ("github_token_enc", "jira_email_enc", "jira_token_enc"):
            await _migrate_add_column(db, "users", col, "TEXT NOT NULL DEFAULT ''")

        # Sessions table — admin session tokens
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
        """)

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions (username)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at)"
        )

        # Job metadata table for filtering and organization
        await db.execute("""
            CREATE TABLE IF NOT EXISTS job_metadata (
                job_name TEXT PRIMARY KEY,
                team TEXT,
                tier TEXT,
                version TEXT,
                labels TEXT NOT NULL DEFAULT '[]'
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_jm_team ON job_metadata (team)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_jm_tier ON job_metadata (tier)"
        )

        # AI token usage tracking table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_token_usage (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ai_provider TEXT NOT NULL DEFAULT '',
                ai_model TEXT NOT NULL DEFAULT '',
                call_type TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL,
                duration_ms INTEGER,
                prompt_chars INTEGER NOT NULL DEFAULT 0,
                response_chars INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_job_id ON ai_token_usage (job_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_created_at ON ai_token_usage (created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_provider ON ai_token_usage (ai_provider)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_model ON ai_token_usage (ai_model)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_call_type ON ai_token_usage (call_type)"
        )

        # Push notification subscriptions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh_key TEXT NOT NULL,
                auth_key TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_subscriptions_username ON push_subscriptions (username)"
        )

        # Mention read tracking
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mention_reads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                comment_id INTEGER NOT NULL,
                read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, comment_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mention_reads_username ON mention_reads (username)"
        )

        await db.commit()

    # Backfill failure_history from existing results (runs once when table is empty).
    # This runs synchronously in the lifespan hook, which means the server does not
    # accept requests until it finishes.  This is acceptable because:
    #  1. The backfill only runs once — when failure_history is empty but results exist.
    #  2. Subsequent startups skip it instantly (the table is no longer empty).
    #  3. The expected data volume (hundreds to low-thousands of results) completes
    #     in under a second on typical hardware.
    await backfill_failure_history()


def _validate_child_identifier_pairing(
    child_job_name: str, child_build_number: int
) -> None:
    """Validate child_job_name / child_build_number pairing.

    This validator only rejects *structurally invalid* combinations.
    Callers are responsible for giving semantic meaning to the valid ones.

    Valid combinations (structural):
    - Both empty  (``""``, ``0``) -- top-level (no child context).
    - Name set, build ``0``       -- accepted; callers decide the semantics
      (e.g. ``_mirror_classification_to_failure_history`` treats this as a wildcard
      targeting all builds of that child job, while ``add_comment`` and
      ``set_reviewed`` store it literally).
    - Both set    (name, N>0)     -- specific child build.

    Invalid:
    - Name empty, build > 0       -- a build number without a job name is meaningless.
    - Any negative build number.
    """
    if child_build_number < 0:
        raise ValueError("child_build_number must not be negative")
    if not child_job_name and child_build_number > 0:
        raise ValueError("child_job_name is required when child_build_number is set")


async def add_comment(
    job_id: str,
    test_name: str,
    comment: str,
    child_job_name: str = "",
    child_build_number: int = 0,
    error_signature: str = "",
    username: str = "",
) -> int:
    """Add a comment to a test failure."""
    logger.debug(
        f"add_comment: job_id={job_id}, test_name={test_name}, comment_len={len(comment)}"
    )
    _validate_child_identifier_pairing(child_job_name, child_build_number)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO comments (job_id, test_name, child_job_name, child_build_number, comment, error_signature, username) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                test_name,
                child_job_name,
                child_build_number,
                comment,
                error_signature,
                username,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def delete_comment(comment_id: int, username: str, job_id: str = "") -> bool:
    """Delete a comment by ID, optionally scoped to username and job_id.

    When username is empty, the delete is not scoped by owner — the caller
    is responsible for ensuring this is only used for admin-authorized requests.
    When username is non-empty, only comments matching that username are deleted.

    Returns True if deleted, False if not found.
    """
    logger.debug(f"delete_comment: comment_id={comment_id}, job_id={job_id}")
    async with aiosqlite.connect(DB_PATH) as db:
        # Build query with optional scoping filters
        query = "DELETE FROM comments WHERE id = ?"
        params: list = [comment_id]
        if username:
            query += " AND username = ?"
            params.append(username)
        if job_id:
            query += " AND job_id = ?"
            params.append(job_id)
        cursor = await db.execute(query, params)
        if cursor.rowcount > 0:
            await db.execute(
                "DELETE FROM mention_reads WHERE comment_id = ?", (comment_id,)
            )
        await db.commit()
        deleted = cursor.rowcount > 0
        logger.debug(f"delete_comment: comment_id={comment_id}, deleted={deleted}")
        return deleted


async def get_comments_for_job(job_id: str) -> list[dict]:
    """Get all comments for a specific job."""
    logger.debug(f"get_comments_for_job: job_id={job_id}")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, job_id, test_name, child_job_name, child_build_number, comment, error_signature, username, created_at "
            "FROM comments WHERE job_id = ? ORDER BY created_at ASC",
            (job_id,),
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        logger.debug(f"get_comments_for_job: job_id={job_id}, count={len(result)}")
        return result


async def set_reviewed(
    job_id: str,
    test_name: str,
    reviewed: bool,
    child_job_name: str = "",
    child_build_number: int = 0,
    username: str = "",
) -> None:
    """Set or update the reviewed state for a test failure."""
    logger.debug(
        f"set_reviewed: job_id={job_id}, test_name={test_name}, reviewed={reviewed}"
    )
    _validate_child_identifier_pairing(child_job_name, child_build_number)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO failure_reviews (job_id, test_name, child_job_name, child_build_number, reviewed, username, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (job_id, test_name, child_job_name, child_build_number, reviewed, username),
        )
        await db.commit()


async def get_reviews_for_job(job_id: str) -> dict[str, dict]:
    """Get all review states for a specific job."""
    logger.debug(f"get_reviews_for_job: job_id={job_id}")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT test_name, child_job_name, child_build_number, reviewed, username, updated_at "
            "FROM failure_reviews WHERE job_id = ?",
            (job_id,),
        )
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            if row["child_job_name"] != "":
                key = f"{row['child_job_name']}#{row['child_build_number']}::{row['test_name']}"
            else:
                key = row["test_name"]
            result[key] = {
                "reviewed": bool(row["reviewed"]),
                "username": row["username"],
                "updated_at": row["updated_at"],
            }
        logger.debug(f"get_reviews_for_job: job_id={job_id}, count={len(result)}")
        return result


async def get_review_status(job_id: str) -> dict:
    """Get review summary for a job (used by dashboard)."""
    logger.debug(f"get_review_status: job_id={job_id}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT result_json FROM results WHERE job_id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        total_failures = 0
        if row and row[0]:
            result_data = parse_result_json(row[0], job_id=job_id)
            if result_data is not None:
                total_failures = count_all_failures(result_data)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM failure_reviews WHERE job_id = ? AND reviewed = 1",
            (job_id,),
        )
        reviewed_count = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM comments WHERE job_id = ?", (job_id,)
        )
        comment_count = (await cursor.fetchone())[0]

        logger.debug(
            f"get_review_status: job_id={job_id}, total_failures={total_failures}, reviewed_count={reviewed_count}, comment_count={comment_count}"
        )
        return {
            "total_failures": total_failures,
            "reviewed_count": reviewed_count,
            "comment_count": comment_count,
        }


async def get_historical_comments(
    test_names: list[str] | None = None,
    error_signatures: list[str] | None = None,
    exclude_job_id: str | None = None,
) -> list[dict]:
    """Get historical comments for similar failures across jobs.

    Matches by test name OR by error signature.
    No arbitrary limit -- returns all matching comments.
    """
    logger.debug(
        f"get_historical_comments: test_names_count={len(test_names) if test_names else 0}, signatures_count={len(error_signatures) if error_signatures else 0}, exclude_job_id={exclude_job_id}"
    )
    conditions: list[str] = []
    params: list[str] = []

    if test_names:
        placeholders = ",".join("?" for _ in test_names)
        conditions.append(f"test_name IN ({placeholders})")
        params.extend(test_names)

    if error_signatures:
        placeholders = ",".join("?" for _ in error_signatures)
        conditions.append(f"error_signature IN ({placeholders})")
        params.extend(error_signatures)

    if not conditions:
        return []

    where = " OR ".join(conditions)
    if exclude_job_id:
        where = f"({where}) AND job_id != ?"
        params.append(exclude_job_id)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT id, job_id, test_name, child_job_name, child_build_number, comment, error_signature, username, created_at "
            f"FROM comments WHERE {where} ORDER BY created_at DESC",
            params,
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        logger.debug(f"get_historical_comments: count={len(result)}")
        return result


def _build_status_update_clause(
    status: str,
    result_json: str | None = None,
) -> tuple[list[str], list]:
    """Build the SET clause parts and params for a status update.

    Returns the set-clause fragments and the corresponding parameter list.
    The caller must append the trailing ``job_id`` parameter.

    Args:
        status: New status value.
        result_json: Serialized result JSON. When not None, ``result_json``
            is included in the update.

    Returns:
        Tuple of (set_parts, params).
    """
    set_parts = ["status = ?"]
    params: list = [status]

    if result_json is not None:
        set_parts.append("result_json = ?")
        params.append(result_json)

    if status == "running":
        set_parts.append(
            "analysis_started_at = COALESCE(analysis_started_at, CURRENT_TIMESTAMP)"
        )
    if status == "completed":
        set_parts.append("completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)")

    return set_parts, params


async def save_result(
    job_id: str,
    jenkins_url: str,
    status: str,
    result: dict | None = None,
) -> None:
    """Save or update an analysis result.

    Args:
        job_id: Unique identifier for the analysis job.
        jenkins_url: URL of the analyzed Jenkins build.
        status: Current status of the analysis.
        result: Optional result data to store.
    """
    logger.debug(f"Saving result for job_id: {job_id} (status: {status})")
    result_json = json.dumps(result) if result is not None else None
    async with aiosqlite.connect(DB_PATH) as db:
        # Insert the row if it doesn't exist yet (preserves created_at / analysis_started_at).
        await db.execute(
            """
            INSERT OR IGNORE INTO results (job_id, jenkins_url, status, result_json)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, jenkins_url, status, result_json),
        )
        # Update the row (handles both fresh inserts and existing rows).
        set_parts, params = _build_status_update_clause(status, result_json)
        set_parts.insert(0, "jenkins_url = COALESCE(NULLIF(?, ''), jenkins_url)")
        params.insert(0, jenkins_url)
        params.append(job_id)
        sql = f"UPDATE results SET {', '.join(set_parts)} WHERE job_id = ?"
        await db.execute(sql, params)
        await db.commit()


async def update_status(
    job_id: str,
    status: str,
    result: dict | None = None,
) -> None:
    """Update the status of an existing analysis result.

    Unlike save_result, this uses UPDATE to preserve the original created_at timestamp.
    Only updates result_json when result is explicitly provided.

    Args:
        job_id: Unique identifier for the analysis job.
        status: New status for the analysis.
        result: Optional result data to store. When None, result_json is not modified.
    """
    logger.debug(f"Updating status for job_id: {job_id} (status: {status})")
    async with aiosqlite.connect(DB_PATH) as db:
        result_json = json.dumps(result) if result is not None else None
        set_parts, params = _build_status_update_clause(status, result_json)
        params.append(job_id)
        sql = f"UPDATE results SET {', '.join(set_parts)} WHERE job_id = ?"
        cursor = await db.execute(sql, params)

        if cursor.rowcount == 0:
            logger.warning(f"update_status: no row found for job_id={job_id}")
        await db.commit()


def _make_progress_phase_patcher(phase: str) -> Callable[[dict], None]:
    """Create a patch function that sets ``progress_phase`` and appends to ``progress_log``.

    This is a convenience wrapper for :func:`patch_result_json` so callers
    can update the progress phase without writing a lambda each time.

    Each call appends a ``{"phase": ..., "timestamp": ...}`` entry to the
    ``progress_log`` list so the full phase history is persisted server-side
    and survives page refreshes.

    Args:
        phase: The progress phase string to set.

    Returns:
        A callable that mutates a dict in place, suitable for ``patch_result_json``.
    """
    import time

    def _patcher(d: dict) -> None:
        d["progress_phase"] = phase
        progress_log = d.get("progress_log")
        if not isinstance(progress_log, list):
            progress_log = []
            d["progress_log"] = progress_log
        progress_log.append(
            {
                "phase": phase,
                "timestamp": time.time(),
            }
        )

    return _patcher


async def update_progress_phase(job_id: str, phase: str) -> None:
    """Update the ``progress_phase`` field in the stored result JSON.

    Convenience wrapper around :func:`patch_result_json` for the common
    pattern of setting a single progress phase string.

    Args:
        job_id: The analysis job identifier.
        phase: The progress phase string to set (e.g. ``"analyzing"``).
    """
    await patch_result_json(job_id, _make_progress_phase_patcher(phase))


async def patch_result_json(
    job_id: str,
    patch_fn: Callable[[dict], None],
) -> None:
    """Atomically read-modify-write the ``result_json`` blob for *job_id*.

    The *patch_fn* is called with the parsed ``result`` dict and is expected
    to mutate it in place.  The read and write happen inside a single
    ``BEGIN IMMEDIATE`` transaction so concurrent patches are serialized
    by SQLite's write lock.

    If the row does not exist or ``result_json`` is empty, this is a no-op.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT result_json FROM results WHERE job_id = ?", (job_id,)
            )
            row = await cursor.fetchone()
            if not row or not row[0]:
                await db.execute("ROLLBACK")
                return
            result_data = parse_result_json(row[0], job_id=job_id)
            if result_data is None:
                await db.execute("ROLLBACK")
                return
            patch_fn(result_data)
            await db.execute(
                "UPDATE results SET result_json = ? WHERE job_id = ?",
                (json.dumps(result_data), job_id),
            )
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def get_result(job_id: str, *, strip_sensitive: bool = True) -> dict | None:
    """Retrieve an analysis result by job ID.

    Args:
        job_id: Unique identifier for the analysis job.
        strip_sensitive: When ``True`` (the default), credential fields
            inside ``request_params`` are removed so they never reach API
            consumers.  Pass ``False`` only when the caller needs to
            read-modify-write the full ``result_json`` back to the database.

    Returns:
        Result dictionary if found, None otherwise.
    """
    logger.debug(f"get_result: job_id={job_id}")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM results WHERE job_id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row:
            logger.debug(
                f"get_result: job_id={job_id}, found=True, status={row['status']}"
            )
            parsed = parse_result_json(row["result_json"], job_id=job_id)
            if parsed and strip_sensitive:
                parsed = strip_sensitive_from_response(parsed)
            return {
                "job_id": row["job_id"],
                "jenkins_url": row["jenkins_url"],
                "status": row["status"],
                "result": parsed,
                "created_at": row["created_at"],
                "completed_at": row["completed_at"]
                if "completed_at" in row.keys()
                else None,
                "analysis_started_at": row["analysis_started_at"]
                if "analysis_started_at" in row.keys()
                else None,
            }
        logger.debug(f"get_result: job_id={job_id}, found=False")
        return None


async def list_results(limit: int = 50) -> list[dict]:
    """List recent analysis results.

    Args:
        limit: Maximum number of results to return.

    Returns:
        List of result summary dictionaries.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT job_id, jenkins_url, status, created_at
            FROM results
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


def count_all_failures(result_data: dict) -> int:
    """Count all failures including those in nested child job analyses.

    Walks the top-level ``failures`` list, then recursively counts failures in
    ``child_job_analyses`` (top-level key) and ``failed_children`` (nested key
    inside each child).

    Args:
        result_data: Parsed result dictionary from result_json.

    Returns:
        Total number of failures across all levels.
    """
    count = len(result_data.get("failures", []))
    for child in result_data.get("child_job_analyses", []):
        count += _count_child_failures_recursive(child)
    return count


def _count_child_failures_recursive(child: dict) -> int:
    """Recursively count failures in a child job analysis dict.

    Each child has a ``failures`` list and a ``failed_children`` list that can
    nest arbitrarily deep.

    Args:
        child: A single child job analysis dictionary.

    Returns:
        Total number of failures for this child and its descendants.
    """
    count = len(child.get("failures", []))
    for nested in child.get("failed_children", []):
        count += _count_child_failures_recursive(nested)
    return count


def _failure_to_history_row(
    failure: dict,
    job_id: str,
    job_name: str,
    build_number: int,
    child_job_name: str = "",
    child_build_number: int = 0,
    analyzed_at: str = "",
) -> tuple:
    """Convert a single failure dict to a failure_history row tuple.

    Args:
        analyzed_at: Timestamp for when the job was originally analyzed.
            If empty, the DB column default (CURRENT_TIMESTAMP) is used.
    """
    analysis = failure.get("analysis", {})
    classification = (
        "" if isinstance(analysis, str) else analysis.get("classification", "")
    )
    return (
        job_id,
        job_name,
        build_number,
        failure.get("test_name", ""),
        failure.get("error", ""),
        failure.get("error_signature", ""),
        classification,
        child_job_name,
        child_build_number,
        analyzed_at,
    )


def _extract_failures_for_history(
    result_data: dict,
    job_id: str,
    job_name: str,
    build_number: int,
    analyzed_at: str = "",
) -> list[tuple]:
    """Extract all failures from result_data into flat tuples for insertion.

    Walks top-level failures and recursively walks child_job_analyses
    and nested failed_children, using the same traversal as
    count_all_failures().

    Args:
        result_data: Parsed result dictionary from result_json.
        job_id: The job identifier.
        job_name: Top-level job name.
        build_number: Top-level build number.
        analyzed_at: Original analysis timestamp from results.created_at.
            Used during backfill to preserve historical chronology.

    Returns:
        List of tuples ready for INSERT:
        (job_id, job_name, build_number, test_name, error_message,
         error_signature, classification, child_job_name, child_build_number, analyzed_at)
    """
    rows: list[tuple] = []

    # Top-level failures (no child context)
    for f in result_data.get("failures", []):
        rows.append(
            _failure_to_history_row(
                f, job_id, job_name, build_number, analyzed_at=analyzed_at
            )
        )

    # Child job analyses (recursive)
    for child in result_data.get("child_job_analyses", []):
        _extract_child_failures_for_history(
            child, job_id, job_name, build_number, rows, analyzed_at=analyzed_at
        )

    return rows


def _extract_child_failures_for_history(
    child: dict,
    job_id: str,
    job_name: str,
    build_number: int,
    rows: list[tuple],
    analyzed_at: str = "",
) -> None:
    """Recursively extract failures from a child job analysis dict.

    Args:
        child: A single child job analysis dictionary.
        job_id: The top-level job identifier.
        job_name: Top-level job name.
        build_number: Top-level build number.
        rows: Accumulator list for insertion tuples.
        analyzed_at: Original analysis timestamp for historical chronology.
    """
    child_job = child.get("job_name", "")
    child_build = child.get("build_number", 0)

    for f in child.get("failures", []):
        rows.append(
            _failure_to_history_row(
                f,
                job_id,
                job_name,
                build_number,
                child_job,
                child_build,
                analyzed_at=analyzed_at,
            )
        )

    for nested in child.get("failed_children", []):
        _extract_child_failures_for_history(
            nested, job_id, job_name, build_number, rows, analyzed_at=analyzed_at
        )


async def populate_failure_history(
    job_id: str, result_data: dict, analyzed_at: str = ""
) -> None:
    """Populate failure_history from a completed analysis result.

    Extracts all failures (top-level and nested children) and inserts
    them into the failure_history table. Idempotent: skips if rows
    already exist for this job_id.

    Args:
        job_id: Unique identifier for the analysis job.
        result_data: Parsed result dictionary from result_json.
        analyzed_at: Original analysis timestamp (results.created_at).
            Used during backfill to preserve historical chronology.
            If empty, the DB default (CURRENT_TIMESTAMP) is used.
    """
    logger.debug(f"populate_failure_history: job_id={job_id}")
    job_name = result_data.get("job_name", "")
    build_number = result_data.get("build_number", 0)

    rows = _extract_failures_for_history(
        result_data, job_id, job_name, build_number, analyzed_at=analyzed_at
    )
    if not rows:
        logger.debug(
            f"populate_failure_history: job_id={job_id}, no failures to insert"
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Delete existing rows for this job_id (supports re-analysis)
        await db.execute(
            "DELETE FROM failure_history WHERE job_id = ?",
            (job_id,),
        )

        # Use analyzed_at when provided (backfill), otherwise let the DB default apply
        if analyzed_at:
            await db.executemany(
                """
                INSERT INTO failure_history
                    (job_id, job_name, build_number, test_name, error_message,
                     error_signature, classification, child_job_name, child_build_number, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        else:
            await db.executemany(
                """
                INSERT INTO failure_history
                    (job_id, job_name, build_number, test_name, error_message,
                     error_signature, classification, child_job_name, child_build_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                # Strip the analyzed_at field (last element) when not backfilling
                [row[:-1] for row in rows],
            )
        await db.commit()
        logger.info(
            f"Populated failure_history with {len(rows)} rows for job_id={job_id}"
        )


async def backfill_failure_history() -> None:
    """Backfill failure_history from existing completed results.

    Runs once at startup when the failure_history table is empty but
    the results table has completed rows. Uses the same extraction
    logic as populate_failure_history().
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Find completed results that are NOT yet in failure_history.
        # This makes the backfill resumable: if it crashes mid-way,
        # remaining jobs are picked up on next startup.
        cursor = await db.execute(
            "SELECT r.job_id, r.result_json, r.created_at FROM results r "
            "LEFT JOIN failure_history fh ON r.job_id = fh.job_id "
            "WHERE r.status = 'completed' AND r.result_json IS NOT NULL AND fh.job_id IS NULL"
        )
        rows = await cursor.fetchall()

    if not rows:
        logger.info(
            "All completed results already in failure_history, nothing to backfill"
        )
        return

    logger.info(f"Backfilling failure_history from {len(rows)} missing results")
    backfilled = 0
    for job_id, result_json_str, created_at in rows:
        result_data = parse_result_json(result_json_str, job_id=job_id)
        if result_data is None:
            continue
        # Skip completed results with zero failures — they have nothing to
        # insert into failure_history, so without this guard the LEFT JOIN
        # would find them "missing" on every startup and reprocess them.
        if count_all_failures(result_data) == 0:
            continue
        # Use the original created_at timestamp to preserve historical chronology
        await populate_failure_history(
            job_id, result_data, analyzed_at=created_at or ""
        )
        backfilled += 1

    logger.info(f"Backfill complete: processed {backfilled}/{len(rows)} results")


async def _get_failure_stats(
    db: aiosqlite.Connection,
    job_filter: str,
    params: list,
) -> tuple[int, str | None, str | None, str]:
    """Return (failure_count, first_seen, last_seen, last_classification).

    Args:
        db: Open aiosqlite connection with row_factory set.
        job_filter: SQL fragment for optional job_name/exclude_job_id filtering.
        params: Bind parameters matching the job_filter placeholders
                (first element is always test_name).
    """
    # Failure count — count distinct builds (job_ids) where this test
    # failed, not raw rows. A test can fail multiple times in different
    # child jobs within the same build, and counting rows would inflate
    # the failure count relative to total_runs (which counts builds).
    cursor = await db.execute(
        f"SELECT COUNT(DISTINCT job_id) FROM failure_history WHERE test_name = ?{job_filter}",
        params,
    )
    failures = (await cursor.fetchone())[0]

    if failures == 0:
        return 0, None, None, ""

    # First and last seen
    cursor = await db.execute(
        f"SELECT MIN(analyzed_at), MAX(analyzed_at) FROM failure_history WHERE test_name = ?{job_filter}",
        params,
    )
    row = await cursor.fetchone()
    first_seen = row[0]
    last_seen = row[1]

    # Last classification (most recent failure)
    cursor = await db.execute(
        f"SELECT classification FROM failure_history WHERE test_name = ?{job_filter} ORDER BY analyzed_at DESC, id DESC LIMIT 1",
        params,
    )
    last_classification = (await cursor.fetchone())[0] or ""

    return failures, first_seen, last_seen, last_classification


async def _get_classification_breakdown(
    db: aiosqlite.Connection,
    job_filter: str,
    params: list,
) -> dict[str, int]:
    """Return a dict mapping classification labels to their counts.

    Args:
        db: Open aiosqlite connection with row_factory set.
        job_filter: SQL fragment for optional job_name/exclude_job_id filtering.
        params: Bind parameters matching the job_filter placeholders
                (first element is always test_name).
    """
    cursor = await db.execute(
        f"SELECT classification, COUNT(*) FROM failure_history WHERE test_name = ?{job_filter} GROUP BY classification",
        params,
    )
    classifications: dict[str, int] = {}
    for row in await cursor.fetchall():
        if row[0]:
            classifications[row[0]] = row[1]
    return classifications


async def _get_related_comments(
    db: aiosqlite.Connection,
    test_name: str,
    signatures: set[str],
    exclude_job_id: str,
) -> list[dict]:
    """Return comments related to a test by name or error signature.

    Args:
        db: Open aiosqlite connection with row_factory set.
        test_name: Full test name to look up.
        signatures: Set of error_signature hashes from recent runs.
        exclude_job_id: Exclude comments from this job ID.
    """
    comment_conditions = ["test_name = ?"]
    comment_params: list = [test_name]
    if signatures:
        placeholders = ",".join("?" for _ in signatures)
        comment_conditions.append(f"error_signature IN ({placeholders})")
        comment_params.extend(signatures)

    comment_where = " OR ".join(comment_conditions)
    if exclude_job_id:
        comment_where = f"({comment_where}) AND job_id != ?"
        comment_params.append(exclude_job_id)
    cursor = await db.execute(
        f"SELECT comment, username, created_at FROM comments WHERE {comment_where} ORDER BY created_at DESC",
        comment_params,
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_test_history(
    test_name: str,
    limit: int = 20,
    job_name: str = "",
    exclude_job_id: str = "",
) -> dict:
    """Get pass/fail history for a specific test.

    Args:
        test_name: Full test name to look up.
        limit: Maximum number of recent runs to return.
        job_name: Optional filter by job name.
        exclude_job_id: Exclude results from this job ID.

    Returns:
        Dict with test_name, total_runs, failures, passes, failure_rate,
        first_seen, last_seen, last_classification, classifications,
        recent_runs, comments, consecutive_failures, note.
    """
    logger.debug(
        f"get_test_history: test_name={test_name}, limit={limit}, job_name={job_name}"
    )
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Build optional job_name filter
        job_filter = ""
        params: list = [test_name]
        if job_name:
            job_filter = " AND job_name = ?"
            params.append(job_name)
        if exclude_job_id:
            job_filter += " AND job_id != ?"
            params.append(exclude_job_id)

        failures, first_seen, last_seen, last_classification = await _get_failure_stats(
            db, job_filter, params
        )

        if failures == 0:
            return {
                "test_name": test_name,
                "total_runs": 0,
                "failures": 0,
                "passes": 0,
                "failure_rate": 0.0,
                "first_seen": None,
                "last_seen": None,
                "last_classification": "",
                "classifications": {},
                "recent_runs": [],
                "comments": [],
                "consecutive_failures": 0,
                "note": "No failure records found for this test.",
            }

        classifications = await _get_classification_breakdown(db, job_filter, params)

        # Recent runs (failures only, since we only track failures)
        cursor = await db.execute(
            f"""SELECT job_id, job_name, build_number, error_message, error_signature,
                       classification, child_job_name, child_build_number, analyzed_at
                FROM failure_history WHERE test_name = ?{job_filter}
                ORDER BY analyzed_at DESC, id DESC LIMIT ?""",
            [*params, limit],
        )
        recent_runs = [dict(row) for row in await cursor.fetchall()]

        # Total failure record count — computed with a separate unbounded query
        # so the value is not capped by the `limit` parameter used for recent_runs.
        # NOTE: failure_history only records failures (not passes), so this is
        # the total number of recorded failure events, not a true consecutive
        # streak (an intervening pass would not be detected).
        # Adding pass tracking is deferred to a future enhancement.
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM failure_history WHERE test_name = ?{job_filter}",
            params,
        )
        consecutive_failures = (await cursor.fetchone())[0]

        # Count only completed results for the denominator so that
        # pending/running/failed analyses don't inflate total_runs.
        if job_name:
            total_query = (
                "SELECT COUNT(DISTINCT job_id) FROM results "
                "WHERE status = 'completed' "
                "AND json_extract(result_json, '$.job_name') = ?"
            )
            total_params: list = [job_name]
        else:
            # Without job_name filtering, pass count cannot be accurately derived.
            # failure_history only records failures, not total test executions,
            # so total_runs == failures and passes would always be 0 (100% failure).
            total_query = None
            total_params = []
        if total_query is not None:
            if exclude_job_id:
                total_query += " AND job_id != ?"
                total_params.append(exclude_job_id)
            cursor = await db.execute(total_query, total_params)
            total_runs = (await cursor.fetchone())[0]
            passes = max(0, total_runs - failures)
            failure_rate = round(failures / total_runs, 4) if total_runs > 0 else 0.0
        else:
            total_runs = failures
            passes = None
            failure_rate = None

        # Collect error signatures for comment lookup
        signatures = {
            r["error_signature"] for r in recent_runs if r.get("error_signature")
        }

        comments = await _get_related_comments(
            db, test_name, signatures, exclude_job_id
        )

    logger.debug(
        f"get_test_history: test_name={test_name}, failures={failures}, passes={passes}, recent_runs={len(recent_runs)}"
    )
    note = (
        "Pass count is estimated from total analyzed builds minus recorded failures."
        if passes is not None
        else "Pass/fail stats unavailable without job_name — failure_history only records failures."
    )
    return {
        "test_name": test_name,
        "total_runs": total_runs,
        "failures": failures,
        "passes": passes,
        "failure_rate": failure_rate,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "last_classification": last_classification,
        "classifications": classifications,
        "recent_runs": recent_runs,
        "comments": comments,
        "consecutive_failures": consecutive_failures,
        "note": note,
    }


async def search_by_signature(signature: str, exclude_job_id: str = "") -> dict:
    """Find all tests that failed with the same error signature.

    Args:
        signature: Error signature hash to search for.
        exclude_job_id: Exclude results from this job ID.

    Returns:
        Dict with signature, total_occurrences, unique_tests, tests list,
        last_classification, and comments.
    """
    logger.debug(
        f"search_by_signature: signature={signature}, exclude_job_id={exclude_job_id}"
    )
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Build optional exclude filter
        exclude_filter = ""
        base_params: list = [signature]
        if exclude_job_id:
            exclude_filter = " AND job_id != ?"
            base_params.append(exclude_job_id)

        # Total occurrences
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM failure_history WHERE error_signature = ?{exclude_filter}",
            base_params,
        )
        total_occurrences = (await cursor.fetchone())[0]

        if total_occurrences == 0:
            return {
                "signature": signature,
                "total_occurrences": 0,
                "unique_tests": 0,
                "tests": [],
                "last_classification": "",
                "comments": [],
            }

        # Tests with this signature and their occurrence counts
        cursor = await db.execute(
            f"SELECT test_name, COUNT(*) as occurrences FROM failure_history "
            f"WHERE error_signature = ?{exclude_filter} GROUP BY test_name ORDER BY occurrences DESC",
            base_params,
        )
        tests = [dict(row) for row in await cursor.fetchall()]
        unique_tests = len(tests)

        # Last classification
        cursor = await db.execute(
            f"SELECT classification FROM failure_history "
            f"WHERE error_signature = ?{exclude_filter} ORDER BY analyzed_at DESC, id DESC LIMIT 1",
            base_params,
        )
        last_classification = (await cursor.fetchone())[0] or ""

        # Comments related to this signature
        comments_query = (
            "SELECT comment, username, created_at FROM comments "
            "WHERE error_signature = ?"
        )
        comments_params: list[str] = [signature]
        if exclude_job_id:
            comments_query += " AND job_id != ?"
            comments_params.append(exclude_job_id)
        comments_query += " ORDER BY created_at DESC"
        cursor = await db.execute(comments_query, comments_params)
        comments = [dict(row) for row in await cursor.fetchall()]

    logger.debug(
        f"search_by_signature: signature={signature}, total_occurrences={total_occurrences}, unique_tests={unique_tests}"
    )
    return {
        "signature": signature,
        "total_occurrences": total_occurrences,
        "unique_tests": unique_tests,
        "tests": tests,
        "last_classification": last_classification,
        "comments": comments,
    }


async def get_job_stats(job_name: str, exclude_job_id: str = "") -> dict:
    """Get aggregate statistics for a specific job name.

    Args:
        job_name: The job name to get statistics for.
        exclude_job_id: Exclude results from this job ID.

    Returns:
        Dict with job_name, total_builds_analyzed, builds_with_failures,
        overall_failure_rate, most_common_failures, and recent_trend.
    """
    logger.debug(f"get_job_stats: job_name={job_name}, exclude_job_id={exclude_job_id}")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Build optional exclude filter
        exclude_filter = ""
        exclude_params: list = []
        if exclude_job_id:
            exclude_filter = " AND job_id != ?"
            exclude_params = [exclude_job_id]

        # Total completed builds — count from results table (not failure_history)
        # so that builds with zero failures are included in the denominator.
        # Uses json_extract to match job_name stored in result_json.
        total_builds_query = (
            "SELECT COUNT(DISTINCT job_id) FROM results "
            "WHERE status = 'completed' AND "
            "json_extract(result_json, '$.job_name') = ?"
        )
        total_builds_params: list = [job_name]
        if exclude_job_id:
            total_builds_query += " AND job_id != ?"
            total_builds_params.append(exclude_job_id)
        cursor = await db.execute(total_builds_query, total_builds_params)
        total_builds = (await cursor.fetchone())[0]

        if total_builds == 0:
            return {
                "job_name": job_name,
                "total_builds_analyzed": 0,
                "builds_with_failures": 0,
                "overall_failure_rate": 0.0,
                "most_common_failures": [],
                "recent_trend": "stable",
            }

        # Builds with failures (distinct job_ids in failure_history for this job)
        cursor = await db.execute(
            f"SELECT COUNT(DISTINCT job_id) FROM failure_history WHERE job_name = ?{exclude_filter}",
            [job_name] + exclude_params,
        )
        builds_with_failures = (await cursor.fetchone())[0]

        overall_failure_rate = (
            builds_with_failures / total_builds if total_builds > 0 else 0.0
        )

        # Most common failures
        # GROUP BY test_name, classification to avoid non-deterministic
        # classification values when a test has been reclassified over time.
        cursor = await db.execute(
            f"SELECT test_name, COUNT(*) as count, classification "
            f"FROM failure_history WHERE job_name = ?{exclude_filter} "
            f"GROUP BY test_name, classification ORDER BY count DESC LIMIT 10",
            [job_name, *exclude_params],
        )
        most_common = [dict(row) for row in await cursor.fetchall()]

        # Recent trend: compare last 7 days vs previous 7 days
        now = datetime.now(tz=timezone.utc)
        seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        fourteen_days_ago = (now - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")

        cursor = await db.execute(
            f"SELECT COUNT(DISTINCT job_id) FROM failure_history "
            f"WHERE job_name = ? AND analyzed_at >= ?{exclude_filter}",
            [job_name, seven_days_ago] + exclude_params,
        )
        recent_failures = (await cursor.fetchone())[0]

        cursor = await db.execute(
            f"SELECT COUNT(DISTINCT job_id) FROM failure_history "
            f"WHERE job_name = ? AND analyzed_at >= ? AND analyzed_at < ?{exclude_filter}",
            [job_name, fourteen_days_ago, seven_days_ago] + exclude_params,
        )
        previous_failures = (await cursor.fetchone())[0]

        if recent_failures < previous_failures:
            recent_trend = "improving"
        elif recent_failures > previous_failures:
            recent_trend = "worsening"
        else:
            recent_trend = "stable"

    return {
        "job_name": job_name,
        "total_builds_analyzed": total_builds,
        "builds_with_failures": builds_with_failures,
        "overall_failure_rate": round(overall_failure_rate, 4),
        "most_common_failures": most_common,
        "recent_trend": recent_trend,
    }


ACTIVE_STATUSES = ("running", "pending", "waiting")

DEFAULT_DASHBOARD_LIMIT = 500


async def count_active_analyses() -> int:
    """Return the number of analyses with an active status.

    Active statuses are: running, pending, waiting.
    Uses a lightweight COUNT query — no result_json is fetched.
    """
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM results WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def list_results_for_dashboard(
    limit: int = DEFAULT_DASHBOARD_LIMIT,
) -> list[dict]:
    """List analysis results with summary data for dashboard display.

    Unlike list_results, this function also extracts key fields from result_json
    for any row that has a stored result (job_name, build_number, failure_count).

    Args:
        limit: Maximum number of results to return.  ``0`` means no limit —
            all rows are returned.  Defaults to :data:`DEFAULT_DASHBOARD_LIMIT`.

    Returns:
        List of result dictionaries enriched with summary data from result_json.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT r.job_id, r.jenkins_url, r.status, r.result_json,
                r.created_at, r.completed_at, r.analysis_started_at,
                (SELECT COUNT(*) FROM failure_reviews fr
                 WHERE fr.job_id = r.job_id AND fr.reviewed = 1) AS reviewed_count,
                (SELECT COUNT(*) FROM comments c
                 WHERE c.job_id = r.job_id) AS comment_count
            FROM results r
            ORDER BY r.created_at DESC
        """
        params: tuple = ()
        if limit > 0:
            sql += " LIMIT ?"
            params = (limit,)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            entry: dict = {
                "job_id": row["job_id"],
                "jenkins_url": row["jenkins_url"],
                "status": row["status"],
                "created_at": row["created_at"],
                "completed_at": row["completed_at"]
                if "completed_at" in row.keys()
                else None,
                "analysis_started_at": row["analysis_started_at"]
                if "analysis_started_at" in row.keys()
                else None,
                "reviewed_count": row["reviewed_count"],
                "comment_count": row["comment_count"],
            }
            result_data = parse_result_json(row["result_json"], job_id=row["job_id"])
            if result_data:
                entry["job_name"] = result_data.get("job_name", "")
                if "build_number" in result_data:
                    entry["build_number"] = result_data["build_number"]
                entry["failure_count"] = count_all_failures(result_data)
                child_jobs = result_data.get("child_job_analyses", [])
                if child_jobs:
                    entry["child_job_count"] = len(child_jobs)
                if result_data.get("summary"):
                    entry["summary"] = result_data["summary"]
                if result_data.get("error"):
                    entry["error"] = result_data["error"]
            results.append(entry)
        return results


async def get_parent_job_name_for_test(test_name: str, job_id: str = "") -> str:
    """Look up the parent pipeline job name for a test from failure_history.

    Args:
        test_name: The test name to look up.
        job_id: When provided, scopes the lookup to a specific analysis job
                to avoid cross-job leakage.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if job_id:
            query = (
                "SELECT job_name FROM failure_history "
                "WHERE test_name = ? AND job_id = ? "
                "ORDER BY analyzed_at DESC, id DESC LIMIT 1"
            )
            params: tuple = (test_name, job_id)
        else:
            query = (
                "SELECT job_name FROM failure_history "
                "WHERE test_name = ? "
                "ORDER BY analyzed_at DESC, id DESC LIMIT 1"
            )
            params = (test_name,)
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        return row[0] if row else ""


async def _mirror_classification_to_failure_history(
    db: aiosqlite.Connection,
    *,
    classification: str,
    test_name: str,
    job_name: str,
    child_build_number: int,
    job_id: str,
) -> None:
    """Mirror a classification into the failure_history table.

    When child_build_number is 0 and job_name is set, it acts as a wildcard:
    all builds (child_build_number > 0) for that job_name are updated.
    Otherwise, only the exact (test_name, job_name, child_build_number, job_id)
    row is updated.
    """
    if child_build_number == 0 and job_name:
        await db.execute(
            "UPDATE failure_history SET classification = ? "
            "WHERE test_name = ? AND child_job_name = ? AND child_build_number > 0 AND job_id = ?",
            [classification, test_name, job_name, job_id],
        )
    else:
        await db.execute(
            "UPDATE failure_history SET classification = ? "
            "WHERE test_name = ? AND child_job_name = ? AND child_build_number = ? AND job_id = ?",
            [classification, test_name, job_name, child_build_number, job_id],
        )


async def set_test_classification(
    test_name: str,
    classification: str,
    *,
    job_id: str,
    reason: str = "",
    job_name: str = "",
    parent_job_name: str = "",
    created_by: str = "",
    references: str = "",
    child_build_number: int = 0,
    visible: int = 1,
) -> int:
    """Set a classification for a test (e.g., FLAKY, REGRESSION).

    Can be set by the AI during analysis or by humans.

    Args:
        job_id: Required — scopes the classification to a specific analysis job.
        visible: Whether the classification is immediately visible.
            Set to 0 during AI analysis; revealed after analysis completes.
    """
    if classification not in HISTORY_CLASSIFICATIONS:
        raise ValueError(
            f"Invalid classification: {classification}. "
            f"Valid: {', '.join(sorted(HISTORY_CLASSIFICATIONS))}"
        )
    if visible not in (0, 1):
        raise ValueError(f"visible must be 0 or 1, got {visible}")
    if not job_id or not job_id.strip():
        raise ValueError("job_id is required for test classification")
    _validate_child_identifier_pairing(job_name, child_build_number)
    logger.debug(
        f"set_test_classification: test_name={test_name}, classification={classification}, "
        f"parent_job_name={parent_job_name}, job_id={job_id}, visible={visible}"
    )
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO test_classifications (test_name, job_name, parent_job_name, classification, reason, references_info, created_by, job_id, child_build_number, visible) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                test_name,
                job_name,
                parent_job_name,
                classification,
                reason,
                references,
                created_by,
                job_id,
                child_build_number,
                visible,
            ),
        )

        # Mirror classification into failure_history so that filters on
        # failure_history.classification (used by get_all_failures, get_test_history)
        # reflect manual/AI reclassifications from test_classifications.
        # Only mirror when visible=1 to prevent hidden AI classifications from
        # leaking into failure_history before analysis completes.
        # When visible=0, make_classifications_visible() handles the mirror.
        if visible:
            await _mirror_classification_to_failure_history(
                db,
                classification=classification,
                test_name=test_name,
                job_name=job_name,
                child_build_number=child_build_number,
                job_id=job_id,
            )

        await db.commit()
        return cursor.lastrowid


async def get_test_classifications(
    test_name: str = "",
    classification: str = "",
    job_name: str = "",
    parent_job_name: str = "",
    job_id: str = "",
) -> list[dict]:
    """Get visible test classifications in the primary (override) domain.

    Only returns classifications with visible=1 **and** a primary
    classification (CODE ISSUE / PRODUCT BUG).  History-system labels
    (FLAKY, REGRESSION, etc.) written by ``set_test_classification()``
    are intentionally excluded because they belong to the history
    domain and are consumed via ``failure_history`` queries (e.g.
    ``get_all_failures()``, ``get_test_history()``), not here.

    The ``_PRIMARY_CLASSIFICATIONS_SQL`` filter is intentional: the
    ``POST /history/classify`` endpoint writes history labels that are
    never meant to appear in this reader.  History labels are consumed
    via ``GET /history/failures`` instead.

    During AI analysis, classifications are created with visible=0 and
    revealed after analysis completes via make_classifications_visible().
    """
    logger.debug(
        f"get_test_classifications: test_name={test_name!r}, classification={classification!r}, "
        f"job_name={job_name!r}, parent_job_name={parent_job_name!r}, job_id={job_id!r}"
    )
    conditions = [
        "tc.visible = 1",
        f"tc.classification IN {_PRIMARY_CLASSIFICATIONS_SQL}",
    ]
    params: list[str] = []

    if test_name:
        conditions.append("tc.test_name = ?")
        params.append(test_name)
    if classification:
        conditions.append("tc.classification = ?")
        params.append(classification)
    if job_name:
        conditions.append("tc.job_name = ?")
        params.append(job_name)
    if parent_job_name:
        conditions.append("tc.parent_job_name = ?")
        params.append(parent_job_name)
    if job_id:
        conditions.append("tc.job_id = ?")
        params.append(job_id)

    where = " AND ".join(conditions)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT tc.id, tc.test_name, tc.job_name, tc.parent_job_name, tc.classification, "
            f"tc.reason, tc.references_info, tc.created_by, tc.job_id, tc.child_build_number, tc.created_at "
            f"FROM test_classifications tc "
            f"WHERE {where} "
            f"ORDER BY tc.created_at DESC, tc.id DESC",
            params,
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        logger.debug(f"get_test_classifications: count={len(result)}")
        return result


async def make_classifications_visible(job_id: str) -> None:
    """Make all classifications for a job visible after analysis completes.

    Also mirrors classifications into failure_history. The mirror is deferred
    from set_test_classification (which creates rows with visible=0 during
    analysis) to here so that failure_history doesn't leak hidden AI labels.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Fetch hidden classifications before flipping so we can mirror them.
        # ORDER BY created_at DESC ensures latest-wins when deduplicating
        # by (test_name, job_name, child_build_number) below.
        cursor = await db.execute(
            "SELECT tc.id, test_name, job_name, child_build_number, classification "
            "FROM test_classifications tc WHERE job_id = ? AND visible = 0 "
            "ORDER BY tc.created_at DESC, tc.id DESC",
            (job_id,),
        )
        all_rows = await cursor.fetchall()

        # Deduplicate: keep only the latest classification per key
        # (latest-wins, since we ordered by created_at DESC).
        seen: set[tuple[str, str, int]] = set()
        rows = []
        for row in all_rows:
            key = (row["test_name"], row["job_name"], row["child_build_number"])
            if key not in seen:
                seen.add(key)
                rows.append(row)

        await db.execute(
            "UPDATE test_classifications SET visible = 1 WHERE job_id = ? AND visible = 0",
            (job_id,),
        )

        # Mirror each newly-visible classification into failure_history
        for row in rows:
            await _mirror_classification_to_failure_history(
                db,
                classification=row["classification"],
                test_name=row["test_name"],
                job_name=row["job_name"],
                child_build_number=row["child_build_number"],
                job_id=job_id,
            )

        await db.commit()
    logger.debug(
        f"make_classifications_visible: job_id={job_id}, mirrored={len(rows)} classifications"
    )


async def get_all_failures(
    search: str = "",
    job_name: str = "",
    classification: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Get paginated failure history with optional filters.

    Returns dict with 'failures' list and 'total' count.

    Args:
        search: Free-text search across test_name, error_message, and job_name.
        job_name: Exact match filter on job_name column.
        classification: Exact match filter on classification column.
        limit: Maximum number of rows to return.
        offset: Number of rows to skip for pagination.

    Returns:
        Dict with ``failures`` (list of row dicts) and ``total`` (int).
    """
    logger.debug(
        f"get_all_failures: search={search!r}, job_name={job_name!r}, classification={classification!r}, limit={limit}, offset={offset}"
    )
    conditions: list[str] = []
    params: list[str | int] = []

    if search:
        conditions.append(
            "(test_name LIKE ? OR error_message LIKE ? OR job_name LIKE ?)"
        )
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if job_name:
        conditions.append("job_name = ?")
        params.append(job_name)
    if classification:
        conditions.append("classification = ?")
        params.append(classification)

    where = " AND ".join(conditions) if conditions else "1=1"

    async with aiosqlite.connect(DB_PATH) as db:
        # Get total count
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM failure_history WHERE {where}",
            params,
        )
        total = (await cursor.fetchone())[0]

        # Get paginated results
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT id, job_id, job_name, build_number, test_name, error_message, "
            f"error_signature, classification, child_job_name, child_build_number, analyzed_at "
            f"FROM failure_history WHERE {where} ORDER BY analyzed_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        logger.debug(f"get_all_failures: total={total}, returned={len(rows)}")
        return {
            "failures": [dict(row) for row in rows],
            "total": total,
        }


async def _delete_job_rows(db: aiosqlite.Connection, job_id: str) -> bool:
    """Delete all rows for a job across related tables. Returns True if the job existed."""
    await db.execute(
        "DELETE FROM mention_reads WHERE comment_id IN "
        "(SELECT id FROM comments WHERE job_id = ?)",
        (job_id,),
    )
    await db.execute("DELETE FROM comments WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM failure_reviews WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM failure_history WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM test_classifications WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM ai_token_usage WHERE job_id = ?", (job_id,))
    cursor = await db.execute("DELETE FROM results WHERE job_id = ?", (job_id,))
    return cursor.rowcount > 0


async def delete_job(job_id: str) -> bool:
    """Delete an analyzed job and all its related data."""
    async with aiosqlite.connect(DB_PATH) as db:
        job_existed = await _delete_job_rows(db, job_id)
        await db.commit()
        return job_existed


async def delete_jobs_bulk(job_ids: list[str]) -> dict:
    """Delete multiple jobs and all their related data in a single transaction.

    Returns dict with 'deleted' (list of successfully deleted job_ids) and
    'failed' (list of dicts with 'job_id' and 'reason' for failures).
    """
    deleted = []
    failed = []
    # Preserve order while dropping duplicates
    unique_ids = list(dict.fromkeys(job_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for idx, job_id in enumerate(unique_ids):
                savepoint = f"delete_job_{idx}"
                await db.execute(f"SAVEPOINT {savepoint}")
                try:
                    if await _delete_job_rows(db, job_id):
                        deleted.append(job_id)
                    else:
                        failed.append({"job_id": job_id, "reason": "not found"})
                    await db.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    logger.exception("delete_jobs_bulk: failed to delete %s", job_id)
                    await db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    await db.execute(f"RELEASE SAVEPOINT {savepoint}")
                    failed.append({"job_id": job_id, "reason": "deletion failed"})
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    return {"deleted": deleted, "failed": failed, "total": len(unique_ids)}


async def override_classification(
    job_id: str,
    test_name: str,
    classification: str,
    child_job_name: str = "",
    child_build_number: int = 0,
    username: str = "",
    parent_job_name: str = "",
) -> list[str]:
    """Override the classification of a failure in failure_history.

    Updates ALL failure_history rows sharing the same error_signature
    (within the same job) so that grouped failures stay in sync.
    Also inserts a test_classifications entry so the AI can learn from
    human overrides.

    Args:
        job_id: The analysis job ID.
        test_name: Fully qualified test name (representative test from the group).
        classification: New classification ("CODE ISSUE" or "PRODUCT BUG").
        child_job_name: Child job name (for pipeline analyses).
        child_build_number: Child build number.
        username: User who made the override.
        parent_job_name: Parent pipeline job name (for test_classifications).

    Returns:
        List of all test names in the affected signature group.
    """
    logger.debug(
        f"override_classification: job_id={job_id}, test_name={test_name}, "
        f"classification={classification}, username={username}"
    )
    _validate_child_identifier_pairing(child_job_name, child_build_number)
    if child_job_name and child_build_number == 0:
        raise ValueError(
            "override_classification requires child_build_number when child_job_name is set"
        )
    async with aiosqlite.connect(DB_PATH) as db:
        # Look up the error_signature for this test so we can update
        # ALL tests in the same group (same signature, same job).
        # Scope by child context when provided so that identically-named
        # tests in different child jobs resolve the correct signature.
        sig_query = (
            "SELECT error_signature FROM failure_history "
            "WHERE job_id = ? AND test_name = ?"
        )
        sig_params: list = [job_id, test_name]
        if child_job_name:
            sig_query += " AND child_job_name = ? AND child_build_number = ?"
            sig_params.extend([child_job_name, child_build_number])
        else:
            sig_query += " AND child_job_name = '' AND child_build_number = 0"
        sig_query += " LIMIT 1"

        cursor = await db.execute(sig_query, sig_params)
        row = await cursor.fetchone()
        error_signature = row[0] if row and row[0] else ""

        if error_signature:
            # Update ALL tests sharing the same error_signature in this job
            if child_job_name:
                await db.execute(
                    """UPDATE failure_history
                       SET classification = ?
                       WHERE job_id = ? AND error_signature = ?
                       AND child_job_name = ? AND child_build_number = ?""",
                    (
                        classification,
                        job_id,
                        error_signature,
                        child_job_name,
                        child_build_number,
                    ),
                )
            else:
                await db.execute(
                    """UPDATE failure_history
                       SET classification = ?
                       WHERE job_id = ? AND error_signature = ?
                       AND child_job_name = '' AND child_build_number = 0""",
                    (classification, job_id, error_signature),
                )
        else:
            # No signature -- fall back to exact test_name match
            if child_job_name:
                await db.execute(
                    """UPDATE failure_history
                       SET classification = ?
                       WHERE job_id = ? AND test_name = ?
                       AND child_job_name = ? AND child_build_number = ?""",
                    (
                        classification,
                        job_id,
                        test_name,
                        child_job_name,
                        child_build_number,
                    ),
                )
            else:
                await db.execute(
                    """UPDATE failure_history
                       SET classification = ?
                       WHERE job_id = ? AND test_name = ?
                       AND child_job_name = '' AND child_build_number = 0""",
                    (classification, job_id, test_name),
                )

        # Find all test_names in this signature group
        if error_signature:
            if child_job_name:
                group_cursor = await db.execute(
                    "SELECT DISTINCT test_name FROM failure_history "
                    "WHERE job_id = ? AND error_signature = ? "
                    "AND child_job_name = ? AND child_build_number = ?",
                    (job_id, error_signature, child_job_name, child_build_number),
                )
            else:
                group_cursor = await db.execute(
                    "SELECT DISTINCT test_name FROM failure_history "
                    "WHERE job_id = ? AND error_signature = ? "
                    "AND child_job_name = '' AND child_build_number = 0",
                    (job_id, error_signature),
                )
            group_tests = [row[0] for row in await group_cursor.fetchall()]
        else:
            group_tests = [test_name]

        # Persist override for ALL tests in the group
        for t in group_tests:
            await db.execute(
                "INSERT INTO test_classifications "
                "(test_name, job_name, parent_job_name, job_id, classification, "
                "reason, created_by, visible, child_build_number) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    t,
                    child_job_name,
                    parent_job_name,
                    job_id,
                    classification,
                    "User override",
                    username,
                    child_build_number,
                ),
            )

        await db.commit()
    logger.info(
        f"Classification overridden: job_id={job_id}, test_name={test_name}, "
        f"classification={classification}, by={username or 'unknown'}"
    )
    return group_tests


async def get_history_classification(
    job_id: str,
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> str:
    """Return the history-domain classification for a test.

    History classifications are ``FLAKY``, ``REGRESSION``,
    ``INFRASTRUCTURE``, ``KNOWN_BUG``, and ``INTERMITTENT``.
    Primary classifications (``CODE ISSUE`` / ``PRODUCT BUG``) are
    intentionally excluded — use :func:`get_effective_classification`
    for those.

    Checks ``test_classifications`` first (visible entries only),
    then falls back to ``failure_history``.

    Args:
        job_id: Analysis job identifier.
        test_name: Fully qualified test name.
        child_job_name: Optional child job name for scoping.
        child_build_number: Optional child build number for scoping.

    Returns:
        The history classification string (e.g. ``"INFRASTRUCTURE"``),
        or ``""`` if no history classification exists.
    """
    _child_job_name = child_job_name or ""
    _child_build_number = child_build_number or 0

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Prefer visible entry from test_classifications
        override_row = await (
            await db.execute(
                "SELECT classification FROM test_classifications"
                " WHERE test_name = ? AND job_id = ? AND job_name = ?"
                " AND child_build_number = ? AND visible = 1"
                f" AND classification IN {_HISTORY_CLASSIFICATIONS_SQL}"
                " ORDER BY id DESC LIMIT 1",
                [test_name, job_id, _child_job_name, _child_build_number],
            )
        ).fetchone()
        if override_row and override_row[0]:
            return override_row[0]

        # 2. Fall back to failure_history
        fh_query = (
            "SELECT classification FROM failure_history"
            " WHERE job_id = ? AND test_name = ?"
            f" AND classification IN {_HISTORY_CLASSIFICATIONS_SQL}"
        )
        fh_params: list = [job_id, test_name]
        if child_job_name:
            fh_query += " AND child_job_name = ? AND child_build_number = ?"
            fh_params.extend([child_job_name, child_build_number])
        else:
            fh_query += " AND child_job_name = '' AND child_build_number = 0"
        fh_query += " ORDER BY analyzed_at DESC, id DESC LIMIT 1"

        fh_row = await (await db.execute(fh_query, fh_params)).fetchone()
        return fh_row[0] if fh_row and fh_row[0] else ""


async def get_effective_classification(
    job_id: str,
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> str:
    """Return the primary classification override for a failure.

    Only considers the primary override domain: ``CODE ISSUE`` and
    ``PRODUCT BUG``.  History-system classifications (``FLAKY``,
    ``REGRESSION``, ``KNOWN_BUG``, etc.) stored in
    ``test_classifications`` are intentionally ignored.

    Checks ``test_classifications`` first for a visible user override
    (latest by ``id``, limited to ``CODE ISSUE`` / ``PRODUCT BUG``).
    If no matching override exists, falls back to the
    ``failure_history`` row.  This two-step lookup ensures overrides
    survive even when ``failure_history`` rows are missing or rebuilt
    from ``result_json``.

    Returns:
        The classification string (``"CODE ISSUE"`` or
        ``"PRODUCT BUG"``), or ``""`` if no row exists in either table.
    """
    _child_job_name = child_job_name or ""
    _child_build_number = child_build_number or 0

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Prefer visible override from test_classifications
        override_row = await (
            await db.execute(
                "SELECT classification FROM test_classifications"
                " WHERE test_name = ? AND job_id = ? AND job_name = ?"
                " AND child_build_number = ? AND visible = 1"
                f" AND classification IN {_PRIMARY_CLASSIFICATIONS_SQL}"
                " ORDER BY id DESC LIMIT 1",
                [test_name, job_id, _child_job_name, _child_build_number],
            )
        ).fetchone()
        if override_row and override_row[0]:
            return override_row[0]

        # 2. Fall back to failure_history (same domain filter so that
        #    mirrored history-system labels like FLAKY don't leak through)
        fh_query = (
            "SELECT classification FROM failure_history"
            " WHERE job_id = ? AND test_name = ?"
            f" AND classification IN {_PRIMARY_CLASSIFICATIONS_SQL}"
        )
        fh_params: list = [job_id, test_name]
        if child_job_name:
            fh_query += " AND child_job_name = ? AND child_build_number = ?"
            fh_params.extend([child_job_name, child_build_number])
        else:
            fh_query += " AND child_job_name = '' AND child_build_number = 0"
        fh_query += " ORDER BY analyzed_at DESC, id DESC LIMIT 1"

        fh_row = await (await db.execute(fh_query, fh_params)).fetchone()
        return fh_row[0] if fh_row and fh_row[0] else ""


async def mark_stale_results_failed() -> list[dict]:
    """Mark orphaned pending/running jobs as failed. Return waiting jobs for resumption.

    Pending and running jobs have lost their background task and cannot recover,
    so they are marked as failed.  Waiting jobs were polling Jenkins and can be
    safely resumed by re-creating their background task.

    Returns:
        List of dicts with ``job_id`` and ``result_data`` for each waiting job.
    """
    waiting_jobs: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Mark pending/running as failed (background task is gone)
        cursor = await db.execute(
            "UPDATE results SET status = 'failed' "
            "WHERE status IN ('pending', 'running')"
        )
        if cursor.rowcount > 0:
            logger.warning(
                f"Marked {cursor.rowcount} stale pending/running job(s) as failed on startup"
            )

        # Collect waiting jobs for resumption instead of failing them
        cursor = await db.execute(
            "SELECT job_id, result_json FROM results WHERE status = 'waiting'"
        )
        rows = await cursor.fetchall()
        for row in rows:
            if row["result_json"]:
                result_data = parse_result_json(
                    row["result_json"], job_id=row["job_id"]
                )
                stored_params = (
                    result_data.get("request_params") if result_data else None
                )
                is_resumable = (
                    result_data is not None
                    and isinstance(stored_params, dict)
                    and bool(stored_params)
                    and "job_name" in result_data
                    and "build_number" in result_data
                )
                if is_resumable:
                    waiting_jobs.append(
                        {
                            "job_id": row["job_id"],
                            "result_data": result_data,
                        }
                    )
                else:
                    logger.warning(
                        f"Marking unrecoverable waiting job {row['job_id']} as failed"
                    )
                    await db.execute(
                        "UPDATE results SET status = 'failed' WHERE job_id = ?",
                        (row["job_id"],),
                    )

        # Mark waiting jobs without result_json as failed (unrecoverable)
        cursor = await db.execute(
            "UPDATE results SET status = 'failed' "
            "WHERE status = 'waiting' AND (result_json IS NULL OR result_json = '')"
        )
        if cursor.rowcount > 0:
            logger.warning(
                f"Marked {cursor.rowcount} unrecoverable waiting job(s) as failed (missing result data)"
            )

        await db.commit()

    if waiting_jobs:
        logger.info(f"Found {len(waiting_jobs)} waiting job(s) to resume")

    return waiting_jobs


async def get_ai_configs() -> list[dict]:
    """Get distinct AI provider/model pairs from completed analysis results.

    Queries the results table for unique (ai_provider, ai_model) combinations
    from successfully completed analyses. These represent known-working configs.

    Returns:
        List of dicts with 'ai_provider' and 'ai_model' keys.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT
                json_extract(result_json, '$.ai_provider') as ai_provider,
                json_extract(result_json, '$.ai_model') as ai_model
            FROM results
            WHERE status = 'completed'
              AND json_extract(result_json, '$.ai_provider') IS NOT NULL
              AND json_extract(result_json, '$.ai_provider') != ''
              AND json_extract(result_json, '$.ai_model') IS NOT NULL
              AND json_extract(result_json, '$.ai_model') != ''
            ORDER BY ai_provider, ai_model
            """
        )
        rows = await cursor.fetchall()
        return [{"ai_provider": row[0], "ai_model": row[1]} for row in rows]


# --- Auth storage functions ---


async def create_admin_user(username: str) -> tuple[str, str]:
    """Create an admin user and return (username, raw_api_key).
    Raises ValueError if username is invalid or taken."""
    if username.lower() == "admin":
        msg = "Username 'admin' is reserved"
        raise ValueError(msg)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,49}$", username):
        msg = f"Invalid username: '{username}'. Must be 2-50 alphanumeric characters, dots, hyphens, underscores."
        raise ValueError(msg)
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (username, api_key_hash, role) VALUES (?, ?, 'admin')",
            (username, key_hash),
        )
        await db.commit()
    return username, raw_key


async def get_user_by_key(api_key: str) -> dict | None:
    """Look up a user by their raw API key."""
    key_hash = hash_api_key(api_key)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, username, role, created_at, last_seen FROM users WHERE api_key_hash = ?",
            (key_hash,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_user_by_username(username: str) -> dict | None:
    """Look up a user by username."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, username, role, created_at, last_seen FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_admin_user(username: str) -> bool:
    """Delete an admin user. Returns True if deleted.

    Raises ValueError if this would delete the last admin user.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
            admin_count = (await cursor.fetchone())[0]
            if admin_count <= 1:
                cursor = await db.execute(
                    "SELECT role FROM users WHERE username = ?", (username,)
                )
                row = await cursor.fetchone()
                if row and row[0] == "admin":
                    await db.execute("ROLLBACK")
                    raise ValueError("Cannot delete the last admin user")

            await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
            cursor = await db.execute(
                "DELETE FROM users WHERE username = ? AND role = 'admin'",
                (username,),
            )
            await db.commit()
            return cursor.rowcount > 0
        except ValueError:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def change_user_role(username: str, new_role: str) -> tuple[str, str]:
    """Change a user's role. Returns (username, raw_api_key).

    When promoting to admin, generates a new API key.
    When demoting to user, removes the API key and invalidates sessions.

    Args:
        username: The user to change.
        new_role: The new role ('admin' or 'user').

    Returns:
        Tuple of (username, raw_api_key). raw_api_key is empty when demoting.

    Raises:
        ValueError: If username not found, role is invalid, or already has the role.
    """
    if new_role not in ("admin", "user"):
        msg = f"Invalid role: '{new_role}'. Must be 'admin' or 'user'."
        raise ValueError(msg)
    if username.lower() == "admin":
        msg = "Cannot change role of reserved 'admin' user"
        raise ValueError(msg)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT username, role FROM users WHERE username = ?", (username,)
        )
        user = await cursor.fetchone()
        if not user:
            msg = f"User '{username}' not found"
            raise ValueError(msg)
        if user["role"] == new_role:
            msg = f"User '{username}' already has role '{new_role}'"
            raise ValueError(msg)

        raw_key = ""
        if new_role == "admin":
            # Promoting to admin — use transaction for atomicity
            raw_key = generate_api_key()
            key_hash = hash_api_key(raw_key)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE users SET role = 'admin', api_key_hash = ? WHERE username = ?",
                    (key_hash, username),
                )
                if cursor.rowcount == 0:
                    await db.execute("ROLLBACK")
                    raise ValueError(f"User '{username}' not found")
                await db.commit()
            except ValueError:
                raise
            except Exception:
                await db.execute("ROLLBACK")
                raise
        else:
            # Demoting to user — use transaction for atomic last-admin check
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND username != ?",
                    (username,),
                )
                other_admins = (await cursor.fetchone())[0]
                if other_admins == 0:
                    await db.execute("ROLLBACK")
                    raise ValueError("Cannot demote the last admin user")
                await db.execute(
                    "UPDATE users SET role = 'user', api_key_hash = NULL WHERE username = ?",
                    (username,),
                )
                await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
                await db.commit()
            except ValueError:
                raise
            except Exception:
                await db.execute("ROLLBACK")
                raise

            return username, raw_key

    return username, raw_key


async def list_users() -> list[dict]:
    """List all users (without key hashes)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, username, role, created_at, last_seen FROM users ORDER BY created_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]


async def track_user(username: str) -> None:
    """Track user activity — insert if new, update last_seen if existing.

    Skips the reserved 'admin' username (bootstrap superuser).
    """
    if username.lower() == "admin":
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (username, role) VALUES (?, 'user') "
            "ON CONFLICT(username) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
            (username,),
        )
        await db.commit()


async def create_session(
    username: str, is_admin: bool = False, ttl_hours: int = SESSION_TTL_HOURS
) -> str:
    """Create an opaque session token. Returns raw token."""
    token = secrets.token_urlsafe(32)
    token_hash = _hash_session_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (token, username, is_admin, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, username, 1 if is_admin else 0, expires_str),
        )
        await db.commit()
    return token


async def get_session(token: str) -> dict | None:
    """Look up a session. Returns None if expired or not found."""
    token_hash = _hash_session_token(token)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT username, is_admin, created_at, expires_at FROM sessions WHERE token = ? AND expires_at > datetime('now')",
            (token_hash,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def renew_session(token: str) -> bool:
    """Extend a session's expiry by SESSION_TTL_HOURS (sliding window).

    Called on each authenticated request to keep active sessions alive.

    Returns True if the session was found and renewed, False otherwise.
    """
    token_hash = _hash_session_token(token)
    new_expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    expires_str = new_expires.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE sessions SET expires_at = ? "
            "WHERE token = ? AND expires_at > datetime('now')",
            (expires_str, token_hash),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_session(token: str) -> None:
    """Delete a session (logout)."""
    token_hash = _hash_session_token(token)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token_hash,))
        await db.commit()


async def rotate_admin_key(username: str, custom_key: str | None = None) -> str:
    """Generate or set a new API key for an admin user. Returns the raw new key."""
    if custom_key:
        validate_api_key(custom_key)
        raw_key = custom_key
    else:
        raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET api_key_hash = ? WHERE username = ? AND role = 'admin'",
            (key_hash, username),
        )
        if cursor.rowcount == 0:
            msg = f"Admin user '{username}' not found"
            raise ValueError(msg)
        # Invalidate all existing sessions for this user
        await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        await db.commit()
    return raw_key


async def cleanup_expired_sessions() -> None:
    """Remove expired sessions."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        await db.commit()


async def save_user_tokens(
    username: str,
    *,
    github_token: str | None = None,
    jira_email: str | None = None,
    jira_token: str | None = None,
) -> None:
    """Save encrypted user tokens. Only updates fields that are explicitly provided (not None).

    Pass empty string to clear a field. Omit (None) to leave unchanged.
    """
    from jenkins_job_insight.encryption import encrypt_value

    updates = []
    params: list[str] = []
    if github_token is not None:
        updates.append("github_token_enc = ?")
        params.append(encrypt_value(github_token))
    if jira_email is not None:
        updates.append("jira_email_enc = ?")
        params.append(encrypt_value(jira_email))
    if jira_token is not None:
        updates.append("jira_token_enc = ?")
        params.append(encrypt_value(jira_token))

    if not updates:
        return

    params.append(username)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE username = ?",  # noqa: S608 — columns are hardcoded literals
            params,
        )
        await db.commit()


async def get_user_tokens(username: str) -> dict[str, str]:
    """Get decrypted user tokens. Returns dict with github_token, jira_email, jira_token."""
    from jenkins_job_insight.encryption import decrypt_value

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT github_token_enc, jira_email_enc, jira_token_enc FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"github_token": "", "jira_email": "", "jira_token": ""}
        return {
            "github_token": decrypt_value(row[0] or ""),
            "jira_email": decrypt_value(row[1] or ""),
            "jira_token": decrypt_value(row[2] or ""),
        }


# --- Job Metadata ---


async def get_job_metadata(job_name: str) -> dict | None:
    """Get metadata for a specific job.

    Args:
        job_name: The Jenkins job name.

    Returns:
        Metadata dict if found, None otherwise.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT job_name, team, tier, version, labels FROM job_metadata WHERE job_name = ?",
            (job_name,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _job_metadata_row_to_dict(row)


def _job_metadata_row_to_dict(row) -> dict:
    """Convert a job_metadata row to a dict, parsing the labels JSON."""
    d = dict(row)
    labels_raw = d.get("labels", "[]")
    try:
        parsed = json.loads(labels_raw) if labels_raw else []
        d["labels"] = parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        d["labels"] = []
    return d


async def _upsert_job_metadata_row(db: aiosqlite.Connection, item: dict) -> None:
    """Upsert a single job metadata row."""
    labels_json = json.dumps(item.get("labels") or [])
    await db.execute(
        "INSERT OR REPLACE INTO job_metadata (job_name, team, tier, version, labels) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            item["job_name"],
            item.get("team"),
            item.get("tier"),
            item.get("version"),
            labels_json,
        ),
    )


async def set_job_metadata(
    job_name: str,
    *,
    team: str | None = None,
    tier: str | None = None,
    version: str | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Set or update metadata for a job.

    Uses INSERT OR REPLACE to upsert.

    Args:
        job_name: The Jenkins job name.
        team: Team owning this job.
        tier: Service tier.
        version: Version or release label.
        labels: Arbitrary labels.

    Returns:
        The stored metadata dict.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await _upsert_job_metadata_row(
            db,
            {
                "job_name": job_name,
                "team": team,
                "tier": tier,
                "version": version,
                "labels": labels or [],
            },
        )
        await db.commit()
    return {
        "job_name": job_name,
        "team": team,
        "tier": tier,
        "version": version,
        "labels": labels or [],
    }


async def delete_job_metadata(job_name: str) -> bool:
    """Delete metadata for a job.

    Returns:
        True if deleted, False if not found.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM job_metadata WHERE job_name = ?",
            (job_name,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_jobs_with_metadata(
    *,
    team: str = "",
    tier: str = "",
    version: str = "",
    labels: list[str] | None = None,
) -> list[dict]:
    """List all job metadata entries, optionally filtered.

    Filters combine with AND logic. Multiple labels require all to match.

    Args:
        team: Filter by team (exact match).
        tier: Filter by tier (exact match).
        version: Filter by version (exact match).
        labels: Filter by labels (all must be present).

    Returns:
        List of metadata dicts.
    """
    conditions: list[str] = []
    params: list[str] = []

    if team:
        conditions.append("team = ?")
        params.append(team)
    if tier:
        conditions.append("tier = ?")
        params.append(tier)
    if version:
        conditions.append("version = ?")
        params.append(version)

    where = " AND ".join(conditions) if conditions else "1=1"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT job_name, team, tier, version, labels FROM job_metadata WHERE {where} ORDER BY job_name",  # noqa: S608
            params,
        )
        rows = await cursor.fetchall()

    result = [_job_metadata_row_to_dict(row) for row in rows]

    # Filter by labels in Python (JSON array matching)
    if labels:
        result = [
            r for r in result if all(lbl in r.get("labels", []) for lbl in labels)
        ]

    return result


async def bulk_set_metadata(items: list[dict]) -> dict:
    """Bulk upsert job metadata.

    Args:
        items: List of dicts with job_name, team, tier, version, labels.

    Returns:
        Dict with 'updated' count.
    """
    for idx, item in enumerate(items):
        if not item.get("job_name"):
            raise ValueError(
                f"bulk_set_metadata: item at index {idx} is missing 'job_name'"
            )
    async with aiosqlite.connect(DB_PATH) as db:
        for item in items:
            await _upsert_job_metadata_row(db, item)
        await db.commit()
    return {"updated": len(items)}


async def auto_assign_job_metadata(
    job_name: str,
    rules: list[dict],
) -> dict | None:
    """Auto-assign metadata to a job from name pattern rules if it has no existing metadata.

    This is called when a new analysis result is stored. If the job already has
    metadata, this is a no-op (manual metadata always takes precedence).

    Args:
        job_name: The Jenkins job name.
        rules: Ordered list of metadata rule dicts.

    Returns:
        The assigned metadata dict, or None if no match or metadata already exists.
    """
    if not rules or not job_name:
        return None

    # Note: small TOCTOU window between check and set. Duplicate
    # auto-assignment is idempotent (same values), so this is acceptable.
    existing = await get_job_metadata(job_name)
    if existing is not None:
        logger.debug(
            f"auto_assign_job_metadata: job '{job_name}' already has metadata, skipping"
        )
        return None

    from jenkins_job_insight.metadata_rules import match_job_metadata

    matched = match_job_metadata(job_name, rules)
    if matched is None:
        logger.debug(f"auto_assign_job_metadata: no rule matched job '{job_name}'")
        return None

    result = await set_job_metadata(
        job_name,
        team=matched.get("team"),
        tier=matched.get("tier"),
        version=matched.get("version"),
        labels=matched.get("labels", []),
    )
    logger.info(
        f"Auto-assigned metadata to job '{job_name}': "
        f"team={matched.get('team')}, tier={matched.get('tier')}"
    )
    return result


async def record_token_usage(
    job_id: str,
    ai_provider: str,
    ai_model: str,
    call_type: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    prompt_chars: int = 0,
    response_chars: int = 0,
) -> str:
    """Record a single AI CLI call's token usage. Returns the record ID."""
    record_id = str(uuid.uuid4())
    total_tokens = input_tokens + output_tokens
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO ai_token_usage "
            "(id, job_id, ai_provider, ai_model, call_type, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, duration_ms, "
            "prompt_chars, response_chars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                job_id,
                ai_provider,
                ai_model,
                call_type,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                total_tokens,
                cost_usd,
                duration_ms,
                prompt_chars,
                response_chars,
            ),
        )
        await db.commit()
    return record_id


async def get_token_usage_for_job(job_id: str) -> list[dict]:
    """Get all token usage records for a specific job."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM ai_token_usage WHERE job_id = ? ORDER BY created_at ASC",
            (job_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_token_usage_summary(
    start_date: str | None = None,
    end_date: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
    call_type: str | None = None,
    group_by: str | None = None,
) -> dict:
    """Get aggregated token usage with optional filters and grouping.

    group_by can be: provider, model, call_type, day, week, month, job
    """
    conditions: list[str] = []
    params: list = []

    if start_date:
        conditions.append("created_at >= ?")
        params.append(start_date)
    if end_date:
        # Normalize date-only to end of day so records from that day are included
        if len(end_date) == 10:  # YYYY-MM-DD
            end_date = f"{end_date} 23:59:59"
        conditions.append("created_at <= ?")
        params.append(end_date)
    if ai_provider:
        conditions.append("ai_provider = ?")
        params.append(ai_provider)
    if ai_model:
        conditions.append("ai_model = ?")
        params.append(ai_model)
    if call_type:
        conditions.append("call_type = ?")
        params.append(call_type)

    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Totals
        totals_query = (
            "SELECT "
            "COALESCE(SUM(input_tokens), 0) as total_input_tokens, "
            "COALESCE(SUM(output_tokens), 0) as total_output_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) as total_cache_read_tokens, "
            "COALESCE(SUM(cache_write_tokens), 0) as total_cache_write_tokens, "
            "COALESCE(SUM(cost_usd), 0) as total_cost_usd, "
            "COUNT(*) as total_calls, "
            "COALESCE(SUM(duration_ms), 0) as total_duration_ms "
            f"FROM ai_token_usage{where_clause}"  # noqa: S608
        )
        cursor = await db.execute(totals_query, params)
        totals = dict(await cursor.fetchone())

        # Breakdown by group
        breakdown: list[dict] = []
        if group_by:
            group_column = {
                "provider": "ai_provider",
                "model": "ai_provider || ' / ' || ai_model",
                "call_type": "call_type",
                "day": "date(created_at)",
                "week": "strftime('%Y-W%W', created_at)",
                "month": "strftime('%Y-%m', created_at)",
                "job": "job_id",
            }.get(group_by)

            if group_column:
                breakdown_query = (
                    f"SELECT {group_column} as group_key, "
                    "COALESCE(SUM(input_tokens), 0) as input_tokens, "
                    "COALESCE(SUM(output_tokens), 0) as output_tokens, "
                    "COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens, "
                    "COALESCE(SUM(cache_write_tokens), 0) as cache_write_tokens, "
                    "COALESCE(SUM(cost_usd), 0) as cost_usd, "
                    "COUNT(*) as call_count, "
                    "CASE WHEN COUNT(duration_ms) > 0 THEN COALESCE(SUM(duration_ms), 0) / COUNT(duration_ms) ELSE 0 END as avg_duration_ms "
                    f"FROM ai_token_usage{where_clause} "  # noqa: S608
                    f"GROUP BY {group_column} "
                    "ORDER BY COALESCE(SUM(cost_usd), 0) DESC"
                )
                cursor = await db.execute(breakdown_query, params)
                breakdown = [dict(row) for row in await cursor.fetchall()]

        return {
            **totals,
            "breakdown": breakdown,
        }


async def get_token_usage_dashboard_summary() -> dict:
    """Get high-level summary for dashboard cards.

    Period keys use rolling windows:
    - ``today``: records created today (date match)
    - ``this_week``: last 7 rolling days (not calendar week)
    - ``this_month``: last 30 rolling days (not calendar month)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        periods = {
            "today": "date(created_at) = date('now')",
            "this_week": "created_at >= datetime('now', '-7 days')",
            "this_month": "created_at >= datetime('now', '-30 days')",
        }

        result: dict = {}
        for period_name, condition in periods.items():
            cursor = await db.execute(
                f"SELECT COUNT(*) as calls, "  # noqa: S608
                f"COALESCE(SUM(total_tokens), 0) as tokens, "
                f"COALESCE(SUM(input_tokens), 0) as input_tokens, "
                f"COALESCE(SUM(output_tokens), 0) as output_tokens, "
                f"COALESCE(SUM(cost_usd), 0) as cost_usd "
                f"FROM ai_token_usage WHERE {condition}"
            )
            result[period_name] = dict(await cursor.fetchone())

        # Top models by cost
        cursor = await db.execute(
            "SELECT ai_provider || ' / ' || ai_model as model, COUNT(*) as calls, "
            "COALESCE(SUM(cost_usd), 0) as cost_usd "
            "FROM ai_token_usage "
            "WHERE created_at >= datetime('now', '-30 days') "
            "GROUP BY ai_provider, ai_model ORDER BY cost_usd DESC LIMIT 5"
        )
        result["top_models"] = [dict(row) for row in await cursor.fetchall()]

        # Top jobs by cost
        cursor = await db.execute(
            "SELECT job_id, COUNT(*) as calls, "
            "COALESCE(SUM(cost_usd), 0) as cost_usd "
            "FROM ai_token_usage "
            "WHERE created_at >= datetime('now', '-30 days') "
            "GROUP BY job_id ORDER BY cost_usd DESC LIMIT 5"
        )
        result["top_jobs"] = [dict(row) for row in await cursor.fetchall()]

        return result


# --- Push Subscriptions ---

MAX_PUSH_SUBSCRIPTIONS_PER_USER = 10


async def save_push_subscription(
    username: str, endpoint: str, p256dh_key: str, auth_key: str
) -> None:
    """Save or update a push subscription for a user.

    Upserts by endpoint — a user can have multiple subscriptions (multiple browsers/devices).
    """
    logger.debug(f"save_push_subscription: username={username}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "INSERT INTO push_subscriptions (username, endpoint, p256dh_key, auth_key) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET "
                "username = excluded.username, "
                "p256dh_key = excluded.p256dh_key, "
                "auth_key = excluded.auth_key, "
                "created_at = CURRENT_TIMESTAMP",
                (username, endpoint, p256dh_key, auth_key),
            )
            # Enforce per-user subscription limit: delete oldest beyond the cap
            await db.execute(
                "DELETE FROM push_subscriptions WHERE username = ? AND id NOT IN "
                "(SELECT id FROM push_subscriptions WHERE username = ? ORDER BY created_at DESC, id DESC LIMIT ?)",
                (username, username, MAX_PUSH_SUBSCRIPTIONS_PER_USER),
            )
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def delete_push_subscription(endpoint: str, username: str) -> bool:
    """Remove a push subscription by endpoint, scoped to the owning user.

    Returns True if deleted, False if not found or not owned by username.
    """
    logger.debug(f"delete_push_subscription: username={username}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND username = ?",
            (endpoint, username),
        )
        await db.commit()
        deleted = cursor.rowcount > 0
        logger.debug(f"delete_push_subscription: deleted={deleted}")
        return deleted


async def get_push_subscriptions_for_users(usernames: list[str]) -> list[dict]:
    """Get all push subscriptions for a list of usernames.

    Returns list of dicts with: username, endpoint, p256dh_key, auth_key.
    """
    if not usernames:
        return []
    logger.debug(f"get_push_subscriptions_for_users: usernames_count={len(usernames)}")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in usernames)
        cursor = await db.execute(
            f"SELECT username, endpoint, p256dh_key, auth_key "  # noqa: S608
            f"FROM push_subscriptions WHERE username IN ({placeholders})",  # noqa: S608
            usernames,
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        logger.debug(f"get_push_subscriptions_for_users: count={len(result)}")
        return result


async def delete_stale_push_subscriptions(endpoints: list[str]) -> None:
    """Remove expired/invalid push subscriptions by endpoint."""
    if not endpoints:
        return
    logger.debug(f"delete_stale_push_subscriptions: endpoints_count={len(endpoints)}")
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in endpoints)
        await db.execute(
            f"DELETE FROM push_subscriptions WHERE endpoint IN ({placeholders})",  # noqa: S608
            endpoints,
        )
        await db.commit()


async def _fetch_mention_candidates(
    username: str,
    unread_only: bool = False,
) -> list[dict]:
    """Fetch and filter mention candidates for a user.

    Uses SQL LIKE for initial candidate filtering, then refines
    with Python-side regex (detect_mentions) to enforce word-boundary
    semantics. SQLite lacks native regex/word-boundary support.

    Performance note: LIKE '%@user%' is a full table scan (leading wildcard
    precludes index use). For current scale (hundreds to low-thousands of
    comments) this is acceptable. If the comments table grows significantly,
    consider: (1) caching unread counts with TTL invalidated on add_comment,
    (2) pushing LIMIT into SQL for paginated queries, or (3) a denormalized
    mentions table populated on comment creation.
    """
    like_pattern = f"%@{username}%"

    base_where = "c.comment LIKE ?"
    base_params: list = [like_pattern]

    if unread_only:
        base_where += " AND mr.id IS NULL"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            f"SELECT c.id, c.job_id, c.test_name, c.child_job_name, "
            f"c.child_build_number, c.comment, c.username, c.created_at, "
            f"CASE WHEN mr.id IS NOT NULL THEN 1 ELSE 0 END AS is_read "
            f"FROM comments c "
            f"LEFT JOIN mention_reads mr ON mr.comment_id = c.id AND mr.username = ? "
            f"WHERE {base_where} "
            f"ORDER BY c.created_at DESC",
            [username, *base_params],
        )
        rows = await cursor.fetchall()

    # Python-side word-boundary filtering using detect_mentions.
    # SQL LIKE '%@user%' over-matches (e.g. '@username_extra'), so we
    # verify each candidate with regex-based detect_mentions().
    filtered: list[dict] = []
    for row in rows:
        mentioned_users = detect_mentions(row["comment"])
        if username in mentioned_users:
            filtered.append(
                {
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "test_name": row["test_name"],
                    "child_job_name": row["child_job_name"],
                    "child_build_number": row["child_build_number"],
                    "comment": row["comment"],
                    "username": row["username"],
                    "created_at": row["created_at"],
                    "is_read": bool(row["is_read"]),
                }
            )

    return filtered


async def get_mentions_for_user(
    username: str,
    offset: int = 0,
    limit: int = 50,
    unread_only: bool = False,
) -> dict:
    """Get comments that mention @username.

    Returns dict with 'mentions' list, 'total' count, and 'unread_count'.
    When unread_only=True, 'total' reflects the count of unread mentions only
    (matching the filtered result set). 'unread_count' always reflects the
    global unread count for the user (regardless of unread_only filter).

    Each mention includes: id, job_id, test_name, child_job_name,
    child_build_number, comment, username (author), created_at, is_read.
    """
    logger.debug(
        f"get_mentions_for_user: username={username}, offset={offset}, limit={limit}, unread_only={unread_only}"
    )
    filtered = await _fetch_mention_candidates(username, unread_only=unread_only)

    total = len(filtered)
    unread_count = sum(1 for m in filtered if not m["is_read"])
    mentions = filtered[offset : offset + limit]
    logger.debug(
        f"get_mentions_for_user: username={username}, total={total}, returned={len(mentions)}"
    )
    return {"mentions": mentions, "total": total, "unread_count": unread_count}


async def mark_mentions_read(username: str, comment_ids: list[int]) -> None:
    """Mark specific mentions as read for a user."""
    # Note: comment_ids are not validated against actual mentions for this user.
    # Junk rows may accumulate but are harmless — _fetch_mention_candidates
    # re-checks detect_mentions, so non-mentioned comments never surface.
    if not comment_ids:
        return
    logger.debug(
        f"mark_mentions_read: username={username}, comment_ids_count={len(comment_ids)}"
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO mention_reads (username, comment_id) VALUES (?, ?)",
            [(username, cid) for cid in comment_ids],
        )
        await db.commit()


async def get_unread_mention_count(username: str) -> int:
    """Get count of unread mentions for a user."""
    logger.debug(f"get_unread_mention_count: username={username}")
    candidates = await _fetch_mention_candidates(username, unread_only=True)
    count = len(candidates)
    logger.debug(f"get_unread_mention_count: username={username}, count={count}")
    return count


async def mark_all_mentions_read(username: str) -> int:
    """Mark all unread mentions as read for a user. Returns count marked."""
    # Note: small race window between fetch and insert (separate connections).
    # New mentions arriving between steps won't be marked, but the next poll
    # or mark-all call will catch them. This is standard "mark all as of now" semantics.
    logger.debug(f"mark_all_mentions_read: username={username}")
    candidates = await _fetch_mention_candidates(username, unread_only=True)
    if not candidates:
        return 0

    comment_ids = [c["id"] for c in candidates]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO mention_reads (username, comment_id) VALUES (?, ?)",
            [(username, cid) for cid in comment_ids],
        )
        await db.commit()

    logger.info(
        f"mark_all_mentions_read: username={username}, marked={len(comment_ids)}"
    )
    return len(comment_ids)
