"""SQLite storage for analysis results."""

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import aiosqlite
from simple_logger.logger import get_logger

from rootcoz.comment_enrichment import detect_mentions
from rootcoz.encryption import (
    decrypt_value,
    encrypt_value,
    get_hmac_secret,
    strip_sensitive_from_response,
)
from rootcoz.metadata_rules import match_job_metadata
from rootcoz.models import (
    HistoryClassificationLiteral,
    OverrideClassificationLiteral,
    PatternLiteral,
    _SYSTEM_TAGS,
)

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

DB_PATH = Path(os.getenv("DB_PATH", "/data/results.db"))


@asynccontextmanager
async def _connect_db() -> AsyncIterator[aiosqlite.Connection]:
    """Open a database connection with WAL mode and busy_timeout."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        db.row_factory = aiosqlite.Row
        yield db


# Primary (override) classifications — derived from the OverrideClassificationLiteral
# type so the SQL filter stays in sync with the model definition.
PRIMARY_CLASSIFICATIONS: tuple[str, ...] = get_args(OverrideClassificationLiteral)

# Pattern classifications — the "how it manifests" axis (two-axis system).
PATTERN_CLASSIFICATIONS: tuple[str, ...] = get_args(PatternLiteral)

# Legacy history classifications — kept for backward compatibility with existing
# data and the HistoryClassificationLiteral type.  New code should use
# PATTERN_CLASSIFICATIONS.
HISTORY_CLASSIFICATIONS: tuple[str, ...] = get_args(HistoryClassificationLiteral)
_PRIMARY_CLASSIFICATIONS_SQL = (
    "(" + ", ".join(f"'{c}'" for c in PRIMARY_CLASSIFICATIONS) + ")"
)
_HISTORY_CLASSIFICATIONS_SQL = (
    "(" + ", ".join(f"'{c}'" for c in HISTORY_CLASSIFICATIONS) + ")"
)
_PATTERN_CLASSIFICATIONS_SQL = (
    "(" + ", ".join(f"'{c}'" for c in PATTERN_CLASSIFICATIONS) + ")"
)


# --- Auth constants and helpers ---
def _parse_session_ttl() -> int:
    """Parse SESSION_TTL_HOURS from env, defaulting to 30 days."""
    raw = os.environ.get("SESSION_TTL_HOURS", "")
    if not raw:
        return 24 * 30
    try:
        value = int(raw)
        return max(1, value)
    except ValueError as e:
        logger.warning("Invalid SESSION_TTL_HOURS value, using default 720h: %s", e)
        return 24 * 30


SESSION_TTL_HOURS = _parse_session_ttl()
SESSION_TTL_SECONDS = SESSION_TTL_HOURS * 3600
MIN_KEY_LENGTH = 16
VALID_USER_STATUSES = ("active", "pending", "rejected")
AI_SYSTEM_USERNAME = "rootcoz-ai"


def validate_api_key(key: str) -> None:
    """Validate API key meets minimum requirements."""
    if len(key) < MIN_KEY_LENGTH:
        msg = f"API key must be at least {MIN_KEY_LENGTH} characters long"
        raise ValueError(msg)


def hash_api_key(key: str) -> str:
    """Hash an API key with HMAC-SHA256 for storage.

    Uses the encryption key (ROOTCOZ_ENCRYPTION_KEY) as the HMAC secret,
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
    return f"rootcoz_{secrets.token_urlsafe(32)}"


# Valid roles for the three-tier RBAC system
VALID_ROLES: frozenset[str] = frozenset({"viewer", "reviewer", "operator", "admin"})


def _normalize_username(username: str) -> str:
    """Normalize a username to lowercase for case-insensitive uniqueness.

    All entry points (registration, tracking, admin creation) must call this
    before storing or looking up usernames.
    """
    return username.strip().lower()


def _validate_username(username: str) -> None:
    """Validate username format and reserved names.

    Normalizes to lowercase before checking, so callers that forget to
    call ``_normalize_username`` are still safe.
    """
    username = _normalize_username(username)
    if username == "admin":
        msg = "Username 'admin' is reserved"
        raise ValueError(msg)
    if username in _SYSTEM_TAGS:
        msg = f"Username '{username}' conflicts with a reserved system tag"
        raise ValueError(msg)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,49}$", username):
        msg = f"Invalid username: '{username}'. Must be 2-50 alphanumeric characters, dots, hyphens, underscores."
        raise ValueError(msg)


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


_ROLE_PRIORITY = {"viewer": 0, "reviewer": 1, "operator": 2, "admin": 3}

# Tables that store a ``username`` column which must be lowercased during
# the case-insensitive migration.  ``test_classifications`` uses
# ``created_by`` instead.
_USERNAME_TABLES = (
    "comments",
    "failure_reviews",
    "sessions",
    "push_subscriptions",
    "mention_reads",
    "chat_messages",
)

# Tables that use a differently-named column for the username.
_USERNAME_COLUMN_OVERRIDES = {
    "test_classifications": "created_by",
    "server_settings": "updated_by",
    "server_settings_history": "changed_by",
}


async def _migrate_lowercase_usernames(db: aiosqlite.Connection) -> None:
    """Lowercase all usernames and merge case-variant duplicates.

    Idempotent — the unique index ``idx_users_username_lower`` acts as the
    migration guard.  If it already exists, this function is a no-op.

    Merge strategy for duplicate case-variants:
    * Keep the row with the earliest ``created_at``.
    * Upgrade the surviving row's role to the highest privilege among
      the duplicates (admin > operator > reviewer > viewer).
    * Re-point all related rows in other tables to the surviving username.
    """
    # Guard: skip if the unique index already exists.
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='index' AND name='idx_users_username_lower'"
    )
    if await cursor.fetchone():
        logger.debug("Migration: idx_users_username_lower already exists, skipping")
        return

    logger.info("Migration: lowercasing usernames and merging case-variant duplicates")

    # 1. Find case-variant duplicate groups.
    cursor = await db.execute(
        "SELECT lower(username) AS lname, COUNT(*) AS cnt "
        "FROM users GROUP BY lower(username) HAVING cnt > 1"
    )
    duplicate_groups = await cursor.fetchall()

    for group in duplicate_groups:
        lname = group["lname"]
        cursor = await db.execute(
            "SELECT id, username, role, api_key_hash, created_at FROM users "
            "WHERE lower(username) = ? ORDER BY created_at ASC",
            (lname,),
        )
        variants = [dict(row) for row in await cursor.fetchall()]
        # Survivor = earliest created_at (first in ORDER BY).
        survivor = variants[0]
        # Highest-privilege role across all variants.
        best_role = max(
            (v["role"] for v in variants),
            key=lambda r: _ROLE_PRIORITY.get(r, 0),
        )
        updates: list[str] = []
        params: list = []
        if best_role != survivor["role"]:
            updates.append("role = ?")
            params.append(best_role)
        # Preserve API key: if survivor has no key, adopt the first
        # available key from any duplicate so no one is locked out.
        # Clear the donor's hash first to avoid UNIQUE constraint violation.
        if not survivor["api_key_hash"]:
            for v in variants[1:]:
                if v["api_key_hash"]:
                    updates.append("api_key_hash = ?")
                    params.append(v["api_key_hash"])
                    await db.execute(
                        "UPDATE users SET api_key_hash = NULL WHERE id = ?",
                        (v["id"],),
                    )
                    break
        if updates:
            params.append(survivor["id"])
            await db.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        # Re-point related tables from each duplicate to the survivor.
        for dup in variants[1:]:
            old_name = dup["username"]
            # mention_reads has UNIQUE(username, comment_id) — delete
            # rows that would collide with the survivor before re-pointing.
            # Skip when old_name == lname: the subquery would match the
            # survivor's own rows and wipe them out.
            if old_name != lname:
                await db.execute(
                    "DELETE FROM mention_reads WHERE username = ? "
                    "AND comment_id IN ("
                    "  SELECT comment_id FROM mention_reads WHERE username = ?"
                    ")",
                    (old_name, lname),
                )
            for table in _USERNAME_TABLES:
                await db.execute(
                    f"UPDATE {table} SET username = ? WHERE username = ?",
                    (lname, old_name),
                )
            for table, col in _USERNAME_COLUMN_OVERRIDES.items():
                await db.execute(
                    f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                    (lname, old_name),
                )
            # Delete the duplicate user row.
            await db.execute("DELETE FROM users WHERE id = ?", (dup["id"],))
        # Rename the survivor to lowercase.
        if survivor["username"] != lname:
            await db.execute(
                "UPDATE users SET username = ? WHERE id = ?",
                (lname, survivor["id"]),
            )

    # 2. Lowercase all remaining (non-duplicate) usernames.
    await db.execute("UPDATE users SET username = lower(username)")

    # 3. Lowercase username columns in related tables.
    for table in _USERNAME_TABLES:
        await db.execute(f"UPDATE {table} SET username = lower(username)")
    for table, col in _USERNAME_COLUMN_OVERRIDES.items():
        await db.execute(f"UPDATE {table} SET {col} = lower({col})")

    # 4. Create the case-insensitive unique index.
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower "
        "ON users(lower(username))"
    )
    logger.info("Migration: case-insensitive username migration complete")


async def init_db() -> None:
    """Initialize the database schema.

    Creates the results table if it does not exist.
    """
    logger.info(f"Initializing database at {DB_PATH}")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _connect_db() as db:
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
                    INSERT INTO failure_reviews (
                        job_id, test_name, child_job_name,
                        child_build_number, reviewed, username,
                        updated_at
                    )
                    SELECT job_id, test_name, child_job_name,
                        child_build_number, reviewed,
                        COALESCE(username, ''), updated_at
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
        await _migrate_add_column(db, "results", "error", "TEXT NOT NULL DEFAULT ''")

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
                pattern TEXT NOT NULL DEFAULT '',
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
            "CREATE INDEX IF NOT EXISTS idx_fh_job_context"
            " ON failure_history"
            " (job_id, test_name, child_job_name, child_build_number)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_classification ON failure_history (classification)"
        )

        # Migration: add pattern column to failure_history (two-axis classification)
        await _migrate_add_column(
            db, "failure_history", "pattern", "TEXT NOT NULL DEFAULT ''"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fh_pattern ON failure_history (pattern)"
        )

        # Migration: add tracked_in columns to failure_history
        await _migrate_add_column(
            db, "failure_history", "tracked_in_url", "TEXT NOT NULL DEFAULT ''"
        )
        await _migrate_add_column(
            db, "failure_history", "tracked_in_type", "TEXT NOT NULL DEFAULT ''"
        )
        await _migrate_add_column(
            db, "failure_history", "tracked_in_by", "TEXT NOT NULL DEFAULT ''"
        )

        # tracked_in_links: supports multiple tracked links per failure
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracked_in_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                test_name TEXT NOT NULL,
                child_job_name TEXT NOT NULL DEFAULT '',
                child_build_number INTEGER NOT NULL DEFAULT 0,
                url TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_til_job ON tracked_in_links (job_id, test_name)"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_til_unique "
            "ON tracked_in_links (job_id, test_name, child_job_name, child_build_number, url)"
        )

        # Migration: copy existing tracked_in data from failure_history (runs once)
        cursor = await db.execute("SELECT COUNT(*) FROM tracked_in_links")
        til_count = (await cursor.fetchone())[0]
        if til_count == 0:
            cursor = await db.execute(
                "SELECT job_id, test_name, child_job_name, child_build_number, "
                "tracked_in_url, tracked_in_type, tracked_in_by "
                "FROM failure_history WHERE tracked_in_url != ''"
            )
            migrate_rows = await cursor.fetchall()
            for row in migrate_rows:
                await db.execute(
                    "INSERT OR IGNORE INTO tracked_in_links "
                    "(job_id, test_name, child_job_name, child_build_number, url, type, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["job_id"],
                        row["test_name"],
                        row["child_job_name"],
                        row["child_build_number"],
                        row["tracked_in_url"],
                        row["tracked_in_type"],
                        row["tracked_in_by"],
                    ),
                )

        # Migration: add pattern column to test_classifications (two-axis classification)
        await _migrate_add_column(
            db, "test_classifications", "pattern", "TEXT NOT NULL DEFAULT ''"
        )

        # Migration: add original_classification and original_pattern columns
        # These store the pre-override values so reports can show from→to correctly.
        await _migrate_add_column(
            db,
            "test_classifications",
            "original_classification",
            "TEXT NOT NULL DEFAULT ''",
        )
        await _migrate_add_column(
            db,
            "test_classifications",
            "original_pattern",
            "TEXT NOT NULL DEFAULT ''",
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
                role TEXT NOT NULL DEFAULT 'reviewer',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migration: add encrypted token columns to users table
        for col in ("github_token_enc", "jira_email_enc", "jira_token_enc"):
            await _migrate_add_column(db, "users", col, "TEXT NOT NULL DEFAULT ''")

        # Migration: add status column to users table (for admin approval flow)
        await _migrate_add_column(
            db, "users", "status", "TEXT NOT NULL DEFAULT 'active'"
        )

        # Migration: role='user' → role='operator' (RBAC three-role migration)
        cursor = await db.execute(
            "UPDATE users SET role = 'operator' WHERE role = 'user'"
        )
        if cursor.rowcount > 0:
            logger.info(
                "[migration] Migrated %d user(s) from role='user' to role='operator'",
                cursor.rowcount,
            )

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

        # Migration: add role column to sessions table
        await _migrate_add_column(
            db, "sessions", "role", "TEXT NOT NULL DEFAULT 'reviewer'"
        )

        # Migration: backfill session roles from users table so existing
        # operator/admin sessions are not downgraded to reviewer.
        await db.execute("""
            UPDATE sessions SET role = (
                SELECT u.role FROM users u WHERE u.username = sessions.username
            ) WHERE EXISTS (
                SELECT 1 FROM users u WHERE u.username = sessions.username
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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS server_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS server_settings_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                previous_value TEXT,
                action TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                ai_provider TEXT NOT NULL DEFAULT '',
                ai_model TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_job_id ON chat_messages (job_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_job_user "
            "ON chat_messages (job_id, username)"
        )

        # Migration: add session_id to chat_messages
        await _migrate_add_column(
            db, "chat_messages", "session_id", "TEXT NOT NULL DEFAULT ''"
        )

        # Migration: add status to chat_messages (pending/completed/failed)
        await _migrate_add_column(
            db, "chat_messages", "status", "TEXT NOT NULL DEFAULT 'completed'"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_job_user_status "
            "ON chat_messages (job_id, username, status)"
        )

        # Migration: case-insensitive username uniqueness.
        # Lowercase all existing usernames, merge case-variant duplicates
        # (keep earliest created_at, upgrade to highest-privilege role),
        # and add a unique index on lower(username).
        await _migrate_lowercase_usernames(db)

        await db.commit()

    # Run signature recomputation migration BEFORE backfill so that:
    # 1. Migration deletes old rows with pre-normalization signatures.
    # 2. Backfill repopulates with new normalized signatures.
    await _migrate_recompute_normalized_signatures()

    # Backfill failure_history from existing results (runs once when table is empty).
    # This runs synchronously in the lifespan hook, which means the server does not
    # accept requests until it finishes.  This is acceptable because:
    #  1. The backfill only runs once — when failure_history is empty but results exist.
    #  2. Subsequent startups skip it instantly (the table is no longer empty).
    #  3. The expected data volume (hundreds to low-thousands of results) completes
    #     in under a second on typical hardware.
    await backfill_failure_history()
    await _migrate_restore_ai_classifications()
    await _migrate_backfill_pattern_axis()


async def _migrate_restore_ai_classifications() -> None:
    """One-time migration: restore original AI classifications in failure_history.

    Historical mirroring code overwrote failure_history.classification with
    user overrides.  This migration detects affected rows (where
    fh.classification matches a user override in test_classifications but
    differs from the original AI value in result_json) and re-populates
    from result_json to restore the original AI values.

    Tracked via ``_migrations_applied`` table to ensure it runs only once.
    """
    migration_key = "restore_ai_classifications_v1"
    async with _connect_db() as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS _migrations_applied "
            "(key TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor = await db.execute(
            "SELECT 1 FROM _migrations_applied WHERE key = ?", (migration_key,)
        )
        if await cursor.fetchone():
            return  # Already applied

        cursor = await db.execute(
            "SELECT COUNT(*) FROM failure_history fh "
            "JOIN test_classifications tc "
            "  ON tc.job_id = fh.job_id AND tc.test_name = fh.test_name "
            "  AND (tc.child_build_number = 0 OR fh.child_build_number = tc.child_build_number) "
            "WHERE fh.classification = tc.classification AND tc.created_by != ''"
        )
        needs_restore = (await cursor.fetchone())[0] > 0

        if not needs_restore:
            await db.execute(
                "INSERT OR IGNORE INTO _migrations_applied (key) VALUES (?)",
                (migration_key,),
            )
            await db.commit()
            return

    logger.info(
        "Migration: restoring original AI classifications in failure_history..."
    )
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT job_id, result_json, created_at FROM results "
            "WHERE status = 'completed' AND result_json IS NOT NULL"
        )
        rows = await cursor.fetchall()

    restored = 0
    for row in rows:
        result_data = parse_result_json(row["result_json"], job_id=row["job_id"])
        if result_data:
            await populate_failure_history(
                row["job_id"], result_data, analyzed_at=row["created_at"]
            )
            restored += 1

    if restored > 0:
        async with _connect_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO _migrations_applied (key) VALUES (?)",
                (migration_key,),
            )
            await db.commit()
        logger.info("Migration: restored AI classifications for %d jobs", restored)
    else:
        logger.warning("Migration: no jobs could be restored (all parse failures)")


# Set of classification values that are actually pattern labels.
# Used by the migration to move them from `classification` to `pattern`.
_PATTERN_AS_CLASSIFICATION: frozenset[str] = frozenset(
    {
        "REGRESSION",
        "FLAKY",
        "KNOWN_BUG",
        "INTERMITTENT",
    }
)


async def _migrate_backfill_pattern_axis() -> None:
    """One-time migration: backfill the ``pattern`` column.

    Existing data falls into two categories:

    1. **failure_history** rows where ``classification`` is actually a pattern
       label (REGRESSION, FLAKY, KNOWN_BUG, INTERMITTENT) — these were written
       by the old history-analysis system.  Move the value to ``pattern`` and
       restore ``classification`` from ``result_json``.

    2. **failure_history** rows where ``classification`` is a root cause label
       (CODE ISSUE, PRODUCT BUG, INFRASTRUCTURE) — set ``pattern`` to ``NEW``.

    3. **test_classifications** rows with pattern-like values — copy to
       ``pattern`` column.

    Tracked via ``_migrations_applied`` to ensure it runs only once.
    """
    migration_key = "backfill_pattern_axis_v1"
    async with _connect_db() as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS _migrations_applied "
            "(key TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor = await db.execute(
            "SELECT 1 FROM _migrations_applied WHERE key = ?", (migration_key,)
        )
        if await cursor.fetchone():
            return  # Already applied

    logger.info("Migration: backfilling pattern axis in failure_history...")

    # Step 1: Move pattern-like classifications to the pattern column.
    # Then restore the original root cause from result_json.
    pattern_placeholders = ", ".join(f"'{p}'" for p in _PATTERN_AS_CLASSIFICATION)

    async with _connect_db() as db:
        await db.execute(
            f"UPDATE failure_history SET pattern = classification "
            f"WHERE classification IN ({pattern_placeholders}) AND pattern = ''"
        )
        await db.commit()

    # Step 2: Restore original root cause classification from result_json
    # for rows where classification was a pattern label.
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT DISTINCT fh.job_id FROM failure_history fh "
            f"WHERE fh.classification IN ({pattern_placeholders})"
        )
        affected_job_ids = [row[0] for row in await cursor.fetchall()]

    for job_id in affected_job_ids:
        async with _connect_db() as db:
            cursor = await db.execute(
                "SELECT result_json FROM results WHERE job_id = ?", (job_id,)
            )
            row = await cursor.fetchone()
        if not row or not row[0]:
            continue
        result_data = parse_result_json(row[0], job_id=job_id)
        if not result_data:
            continue

        # Build a lookup: test_name -> original AI classification
        cls_map: dict[str, str] = {}
        for f in result_data.get("failures", []):
            analysis = f.get("analysis", {})
            if isinstance(analysis, dict):
                cls_map[f.get("test_name", "")] = analysis.get("classification", "")
        for child in result_data.get("child_job_analyses", []):
            for f in child.get("failures", []):
                analysis = f.get("analysis", {})
                if isinstance(analysis, dict):
                    cls_map[f.get("test_name", "")] = analysis.get("classification", "")

        async with _connect_db() as db:
            for test_name, orig_cls in cls_map.items():
                if orig_cls:
                    await db.execute(
                        f"UPDATE failure_history SET classification = ? "
                        f"WHERE job_id = ? AND test_name = ? "
                        f"AND classification IN ({pattern_placeholders})",
                        (orig_cls, job_id, test_name),
                    )
            await db.commit()

    async with _connect_db() as db:
        # Step 3: Set pattern = 'NEW' for root-cause classifications
        # that don't have a pattern yet
        await db.execute(
            "UPDATE failure_history SET pattern = 'NEW' "
            "WHERE pattern = '' AND classification != ''"
        )

        # Step 4: Copy pattern-like values in test_classifications to pattern column
        await db.execute(
            f"UPDATE test_classifications SET pattern = classification "
            f"WHERE classification IN ({pattern_placeholders}) AND pattern = ''"
        )

        await db.execute(
            "INSERT OR IGNORE INTO _migrations_applied (key) VALUES (?)",
            (migration_key,),
        )
        await db.commit()
    logger.info("Migration: pattern axis backfill complete")


async def _migrate_recompute_normalized_signatures() -> None:
    """One-time migration: clear failure_history so backfill recomputes signatures.

    The get_failure_signature() function now normalizes text (strips
    timestamps, UUIDs, pod names, build numbers) before hashing.
    Old failure_history rows have pre-normalization signatures that
    won't match new ones, breaking auto-review matching.

    This migration deletes all existing failure_history rows. The
    backfill_failure_history() function (called after migrations)
    re-populates them from stored result JSON, which triggers
    get_failure_signature() with the new normalization logic.
    """
    migration_key = "recompute_normalized_signatures_v1"
    async with _connect_db() as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS _migrations_applied "
            "(key TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor = await db.execute(
            "SELECT 1 FROM _migrations_applied WHERE key = ?", (migration_key,)
        )
        if await cursor.fetchone():
            return

        logger.info(
            "Migration: clearing failure_history for signature recomputation. "
            "Rows will be repopulated by backfill with normalized signatures."
        )

        await db.execute("DELETE FROM failure_history")
        await db.execute(
            "INSERT OR IGNORE INTO _migrations_applied (key) VALUES (?)",
            (migration_key,),
        )
        await db.commit()


def _validate_child_identifier_pairing(
    child_job_name: str, child_build_number: int
) -> None:
    """Validate child_job_name / child_build_number pairing.

    This validator only rejects *structurally invalid* combinations.
    Callers are responsible for giving semantic meaning to the valid ones.

    Valid combinations (structural):
    - Both empty  (``""``, ``0``) -- top-level (no child context).
    - Name set, build ``0``       -- accepted; callers decide the semantics
      (e.g. ``add_comment`` and ``set_reviewed`` store it literally).
    - Both set    (name, N>0)     -- specific child build.

    Invalid:
    - Name empty, build > 0       -- a build number without a job name is meaningless.
    - Any negative build number.
    """
    if child_build_number < 0:
        raise ValueError("child_build_number must not be negative")
    if not child_job_name and child_build_number > 0:
        raise ValueError("child_job_name is required when child_build_number is set")


def _child_scope_sql(
    child_job_name: str,
    child_build_number: int,
    name_column: str = "child_job_name",
    build_column: str = "child_build_number",
) -> tuple[str, list]:
    """Return a SQL WHERE-clause suffix and params for child-job scoping.

    Three modes:
    - ``child_job_name`` + ``child_build_number > 0`` → specific child build.
    - ``child_job_name`` + ``child_build_number == 0`` → wildcard (name only).
    - No ``child_job_name`` → top-level rows (empty name, build 0).

    Use *name_column* / *build_column* to target different tables
    (e.g. ``job_name`` in ``test_classifications`` vs
    ``child_job_name`` in ``failure_history``).
    """
    if child_job_name and child_build_number > 0:
        return f" AND {name_column} = ? AND {build_column} = ?", [
            child_job_name,
            child_build_number,
        ]
    if child_job_name:
        # Wildcard: match by name only, any build number
        return f" AND {name_column} = ?", [child_job_name]
    return f" AND {name_column} = '' AND {build_column} = 0", []


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
    async with _connect_db() as db:
        cursor = await db.execute(
            "INSERT INTO comments"
            " (job_id, test_name, child_job_name, child_build_number,"
            " comment, error_signature, username)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
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
    async with _connect_db() as db:
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
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, job_id, test_name, child_job_name,"
            " child_build_number, comment, error_signature,"
            " username, created_at"
            " FROM comments WHERE job_id = ? ORDER BY created_at ASC",
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
    async with _connect_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO failure_reviews"
            " (job_id, test_name, child_job_name,"
            " child_build_number, reviewed, username, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (job_id, test_name, child_job_name, child_build_number, reviewed, username),
        )
        await db.commit()


async def get_reviews_for_job(job_id: str) -> dict[str, dict]:
    """Get all review states for a specific job."""
    logger.debug(f"get_reviews_for_job: job_id={job_id}")
    async with _connect_db() as db:
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
    async with _connect_db() as db:
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
            f"get_review_status: job_id={job_id}, "
            f"total_failures={total_failures}, "
            f"reviewed_count={reviewed_count}, "
            f"comment_count={comment_count}"
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
        f"get_historical_comments: "
        f"test_names_count={len(test_names) if test_names else 0}, "
        f"signatures_count="
        f"{len(error_signatures) if error_signatures else 0}, "
        f"exclude_job_id={exclude_job_id}"
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

    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, job_id, test_name, child_job_name,"
            " child_build_number, comment, error_signature,"
            " username, created_at "
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
    async with _connect_db() as db:
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
    async with _connect_db() as db:
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
    async with _connect_db() as db:
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


def _backfill_child_uuids(child: dict) -> bool:
    """Recursively assign UUIDs to a child job analysis and its failures.

    Walks ``failures`` and ``failed_children`` (nested children).

    Returns:
        ``True`` if any UUIDs were added.
    """
    changed = False
    if "id" not in child:
        child["id"] = str(uuid.uuid4())
        changed = True
    for f in child.get("failures", []):
        if not isinstance(f, dict):
            continue
        if "id" not in f:
            f["id"] = str(uuid.uuid4())
            changed = True
    for nested in child.get("failed_children", []):
        if not isinstance(nested, dict):
            continue
        if _backfill_child_uuids(nested):
            changed = True
    return changed


async def _backfill_failure_uuids(job_id: str, result_data: dict) -> bool:
    """Assign stable UUIDs to failures and child analyses that lack them.

    Legacy ``result_json`` rows pre-date the ``id`` field on
    :class:`~rootcoz.models.FailureAnalysis` and
    :class:`~rootcoz.models.ChildJobAnalysis`.  When such data is loaded,
    Pydantic's ``default_factory`` generates a *transient* UUID that is never
    persisted — so the ID changes on every read.

    This helper detects missing ``id`` fields, assigns UUIDs, and persists them
    back so subsequent reads return stable identifiers.

    Args:
        job_id: The analysis job identifier (used for the DB update).
        result_data: Parsed result dict (mutated in place).

    Returns:
        ``True`` if any UUIDs were added (and the DB was updated).
    """
    changed = False
    for f in result_data.get("failures", []):
        if not isinstance(f, dict):
            continue
        if "id" not in f:
            f["id"] = str(uuid.uuid4())
            changed = True
    for child in result_data.get("child_job_analyses", []):
        if not isinstance(child, dict):
            continue
        if _backfill_child_uuids(child):
            changed = True
    if changed:
        async with _connect_db() as db:
            await db.execute(
                "UPDATE results SET result_json = ? WHERE job_id = ?",
                (json.dumps(result_data), job_id),
            )
            await db.commit()
        logger.info(f"Backfilled missing failure UUIDs for job_id={job_id}")
    return changed


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
    async with _connect_db() as db:
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
            if parsed:
                await _backfill_failure_uuids(job_id, parsed)
            if parsed and strip_sensitive:
                parsed = strip_sensitive_from_response(parsed)
            return {
                "job_id": row["job_id"],
                "jenkins_url": row["jenkins_url"],
                "status": row["status"],
                "error": row["error"] if "error" in row.keys() else "",
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


async def get_job_submitters(job_ids: list[str]) -> dict[str, str]:
    """Return {job_id: submitted_by} for multiple jobs in one query."""
    if not job_ids:
        return {}
    placeholders = ", ".join("?" for _ in job_ids)
    async with _connect_db() as db:
        cursor = await db.execute(
            f"SELECT job_id, json_extract(result_json, '$.request_params.submitted_by') as submitter "
            f"FROM results WHERE job_id IN ({placeholders})",
            job_ids,
        )
        rows = await cursor.fetchall()
    return {row["job_id"]: row["submitter"] or "" for row in rows}


def _find_failure_by_uuid_in_failures(
    failures: list[dict], failure_uuid: str
) -> dict | None:
    """Search a flat list of failure dicts for a matching UUID."""
    for f in failures:
        if not isinstance(f, dict):
            continue
        if f.get("id") == failure_uuid:
            return f
    return None


def _find_failure_by_uuid_in_children(
    children: list[dict], failure_uuid: str
) -> tuple[dict | None, str, int]:
    """Recursively search child job analyses for a failure with matching UUID.

    Returns:
        (failure_dict, child_job_name, child_build_number) or (None, "", 0).
    """
    for child in children:
        if not isinstance(child, dict):
            continue
        # Check direct failures of this child
        found = _find_failure_by_uuid_in_failures(
            child.get("failures", []), failure_uuid
        )
        if found is not None:
            return found, child.get("job_name", ""), child.get("build_number", 0)
        # Recurse into nested children
        found, cjn, cbn = _find_failure_by_uuid_in_children(
            child.get("failed_children", []), failure_uuid
        )
        if found is not None:
            return found, cjn, cbn
    return None, "", 0


async def find_failure_by_uuid(
    failure_uuid: str,
) -> dict | None:
    """Search all stored results for a failure with the given UUID.

    Uses SQLite ``INSTR`` to pre-filter rows that *might* contain the UUID,
    then verifies in Python.

    Returns:
        Dict with ``job_id``, ``failure``, ``child_job_name``,
        ``child_build_number``, or ``None`` if not found.
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT job_id, result_json FROM results "
            "WHERE result_json IS NOT NULL AND INSTR(result_json, ?) > 0",
            (failure_uuid,),
        )
        async for row in cursor:
            result_data = parse_result_json(row["result_json"], job_id=row["job_id"])
            if not result_data:
                continue
            # Search top-level failures
            found = _find_failure_by_uuid_in_failures(
                result_data.get("failures", []), failure_uuid
            )
            if found is not None:
                return {
                    "job_id": row["job_id"],
                    "failure": found,
                    "child_job_name": "",
                    "child_build_number": 0,
                }
            # Search child job analyses
            found, cjn, cbn = _find_failure_by_uuid_in_children(
                result_data.get("child_job_analyses", []), failure_uuid
            )
            if found is not None:
                return {
                    "job_id": row["job_id"],
                    "failure": found,
                    "child_job_name": cjn,
                    "child_build_number": cbn,
                }
    return None


async def list_results(limit: int = 50) -> list[dict]:
    """List recent analysis results.

    Args:
        limit: Maximum number of results to return.

    Returns:
        List of result summary dictionaries.
    """
    async with _connect_db() as db:
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
    pattern = "" if isinstance(analysis, str) else analysis.get("pattern", "")
    return (
        job_id,
        job_name,
        build_number,
        failure.get("test_name", ""),
        failure.get("error", ""),
        failure.get("error_signature", ""),
        classification,
        pattern,
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


async def find_matching_previous_analysis(
    job_name: str,
    test_name: str,
    current_job_id: str,
    child_job_name: str = "",
) -> dict | None:
    """Find the most recent human-reviewed previous analysis of the same test.

    Searches failure_history for a row with the same job_name and test_name
    from a different (previous) job_id that has been reviewed by a human
    (not auto-reviewed by rootcoz-ai). When child_job_name is provided,
    results are scoped to the same child job context to avoid cross-child
    matches for tests with the same name.

    Only failures with a corresponding human review in failure_reviews
    (reviewed=1, username != rootcoz-ai and not empty) are considered,
    ensuring auto-review chains don't propagate without human validation.
    Legacy review rows migrated with username='' are also excluded.

    Returns the most recent match ordered by analyzed_at descending,
    with id as tiebreaker for deterministic results.

    Args:
        job_name: Jenkins job name to match.
        test_name: Fully qualified test name to match.
        current_job_id: Current job ID to exclude from results.
        child_job_name: Child job name to scope the search (empty for
            top-level failures).

    Returns:
        Dict with previous failure_history row data if found, None otherwise.
        Includes keys: job_id, build_number, error_signature, classification,
        pattern, analyzed_at.
    """
    async with _connect_db() as db:
        # Find the most recent failure_history row for the same job+test
        # (excluding the current job) that a HUMAN has reviewed.
        # The EXISTS subquery ensures auto-review only chains from
        # human-validated reviews — not from other auto-reviews
        # (username = AI_SYSTEM_USERNAME) or legacy rows with blank
        # usernames (migrated before the username column existed).
        # child_build_number matching: fr.child_build_number=0 acts as a
        # wildcard (matches any fh.child_build_number), consistent with
        # the API model where child_build_number=0 means "not specified".
        cursor = await db.execute(
            "SELECT fh.job_id, fh.build_number, fh.error_signature, "
            "fh.classification, fh.pattern, fh.analyzed_at "
            "FROM failure_history fh "
            "WHERE fh.job_name = ? AND fh.test_name = ? AND fh.job_id != ? "
            "AND fh.child_job_name = ? "
            "AND EXISTS ("
            "  SELECT 1 FROM failure_reviews fr "
            "  WHERE fr.job_id = fh.job_id "
            "  AND fr.test_name = fh.test_name "
            "  AND fr.child_job_name = fh.child_job_name "
            "  AND (fr.child_build_number = fh.child_build_number "
            "       OR fr.child_build_number = 0) "
            "  AND fr.reviewed = 1 "
            "  AND fr.username != ? AND fr.username != ''"
            ") "
            "ORDER BY fh.analyzed_at DESC, fh.id DESC LIMIT 1",
            (job_name, test_name, current_job_id, child_job_name, AI_SYSTEM_USERNAME),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


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

    async with _connect_db() as db:
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
                     error_signature, classification, pattern,
                     child_job_name, child_build_number, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        else:
            await db.executemany(
                """
                INSERT INTO failure_history
                    (job_id, job_name, build_number, test_name, error_message,
                     error_signature, classification, pattern,
                     child_job_name, child_build_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                # Strip the analyzed_at field (last element) when not backfilling
                [row[:-1] for row in rows],
            )
        await db.commit()
        logger.info(
            f"Populated failure_history with {len(rows)} rows for job_id={job_id}"
        )


async def add_tracked_in_link(
    job_id: str,
    test_name: str,
    url: str,
    tracked_type: str,
    *,
    child_job_name: str = "",
    child_build_number: int = 0,
    tracked_by: str = "",
) -> int:
    """Add a tracked-in link for a failure.

    Args:
        job_id: Analysis job ID.
        test_name: Full test name.
        url: Tracking issue URL.
        tracked_type: Tracker type ('jira', 'github', or '').
        child_job_name: Child job name (empty for top-level failures).
        child_build_number: Child build number (0 for top-level/wildcard).
        tracked_by: Username who added the link.

    Returns:
        ID of the inserted row, or 0 if URL is empty.
    """
    if not url:
        return 0
    async with _connect_db() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO tracked_in_links "
            "(job_id, test_name, child_job_name, child_build_number, url, type, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                test_name,
                child_job_name,
                child_build_number,
                url,
                tracked_type,
                tracked_by,
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_tracked_in_link(link_id: int) -> dict | None:
    """Fetch a single tracked-in link by ID.

    Returns:
        Link dict or None if not found.
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, job_id, created_by FROM tracked_in_links WHERE id = ?",
            (link_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_tracked_in_link(link_id: int) -> int:
    """Delete a tracked-in link by ID.

    Returns:
        Number of rows deleted (0 or 1).
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM tracked_in_links WHERE id = ?", (link_id,)
        )
        await db.commit()
        return cursor.rowcount


def _tracked_in_key(
    test_name: str, child_job_name: str, child_build_number: int
) -> str:
    """Build a lookup key for tracked-in data matching the frontend reviewKey format."""
    if child_job_name:
        return f"{child_job_name}#{child_build_number}::{test_name}"
    return test_name


async def get_tracked_in_for_job(job_id: str) -> dict[str, list[dict]]:
    """Return tracked-in links grouped by composite key.

    Returns:
        Dict keyed by composite key (matching reviewKey format) to
        list of ``{tracked_in_url, tracked_in_type, tracked_in_by}`` dicts.
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, test_name, child_job_name, child_build_number, "
            "url, type, created_by "
            "FROM tracked_in_links "
            "WHERE job_id = ? ORDER BY created_at",
            (job_id,),
        )
        rows = await cursor.fetchall()
        result: dict[str, list[dict]] = {}
        for row in rows:
            key = _tracked_in_key(
                row["test_name"],
                row["child_job_name"],
                row["child_build_number"],
            )
            result.setdefault(key, []).append(
                {
                    "id": row["id"],
                    "tracked_in_url": row["url"],
                    "tracked_in_type": row["type"],
                    "tracked_in_by": row["created_by"],
                }
            )
        return result


async def backfill_failure_history() -> None:
    """Backfill failure_history from existing completed results.

    Runs once at startup when the failure_history table is empty but
    the results table has completed rows. Uses the same extraction
    logic as populate_failure_history().
    """
    async with _connect_db() as db:
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


async def carry_forward_user_overrides(job_id: str, result_data: dict) -> int:
    """Carry forward user classification overrides from previous jobs.

    When a test was previously classified by a user (not rootcoz-ai),
    copy that override to the new job so _TC_LATEST_JOIN resolves the
    user's classification as the effective one.

    Args:
        job_id: The new analysis job ID.
        result_data: Parsed result dictionary from result_json.

    Returns:
        Number of overrides carried forward.
    """
    job_name = result_data.get("job_name", "")
    carried = 0

    # Collect all test names from the new job (top-level + children)
    test_names: list[
        tuple[str, str, int]
    ] = []  # (test_name, child_job_name, child_build_number)
    for f in result_data.get("failures", []):
        tn = f.get("test_name", "")
        if tn:
            test_names.append((tn, "", 0))
    for child in result_data.get("child_job_analyses", []):
        child_job = child.get("job_name", "")
        child_build = child.get("build_number", 0)
        for f in child.get("failures", []):
            tn = f.get("test_name", "")
            if tn:
                test_names.append((tn, child_job, child_build))

    if not test_names:
        return 0

    async with _connect_db() as db:
        for test_name, child_job_name, child_build_number in test_names:
            # Find the most recent user override for this test (from any previous job)
            cursor = await db.execute(
                "SELECT classification, reason, references_info, created_by, "
                "parent_job_name, child_build_number "
                "FROM test_classifications "
                "WHERE test_name = ? AND job_id != ? AND visible = 1 "
                "AND created_by != ? "
                "AND classification != '' "
                "AND parent_job_name = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (test_name, job_id, AI_SYSTEM_USERNAME, job_name),
            )
            row = await cursor.fetchone()
            if row is None:
                continue

            # Copy the user's override to the new job
            await db.execute(
                "INSERT INTO test_classifications "
                "(test_name, job_name, parent_job_name, job_id, classification, "
                "reason, references_info, created_by, visible, child_build_number) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    test_name,
                    child_job_name,
                    row["parent_job_name"] or job_name,
                    job_id,
                    row["classification"],
                    f"Carried forward from user override{' by ' + row['created_by'] if row['created_by'] else ''}",
                    row["references_info"] or "",
                    row["created_by"] or "",
                    child_build_number,
                ),
            )
            carried += 1
            logger.info(
                "Carried forward user override for %s: %s (by %s) to job %s",
                test_name,
                row["classification"],
                row["created_by"] or "<unattributed>",
                job_id,
            )

        if carried:
            await db.commit()

    return carried


# SQL subquery for resolving effective classification.
# LEFT JOINs with the latest visible test_classification override per
# (job_id, test_name, child_build_number). Use COALESCE(tc_latest.classification,
# fh.classification) to get the effective classification.
_TC_LATEST_JOIN = """
    LEFT JOIN (
        SELECT job_id, test_name, job_name, child_build_number, classification,
               ROW_NUMBER() OVER (
                   PARTITION BY job_id, test_name, job_name, child_build_number
                   ORDER BY created_at DESC, id DESC
               ) AS rn
        FROM test_classifications
        WHERE visible = 1
    ) tc_latest ON tc_latest.job_id = fh.job_id
        AND tc_latest.test_name = fh.test_name
        AND tc_latest.job_name = fh.child_job_name
        AND tc_latest.child_build_number = fh.child_build_number
        AND tc_latest.rn = 1
"""


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
        f"SELECT COUNT(DISTINCT fh.job_id) FROM failure_history fh WHERE fh.test_name = ?{job_filter}",
        params,
    )
    failures = (await cursor.fetchone())[0]

    if failures == 0:
        return 0, None, None, ""

    # First and last seen
    cursor = await db.execute(
        f"SELECT MIN(fh.analyzed_at), MAX(fh.analyzed_at) FROM failure_history fh WHERE fh.test_name = ?{job_filter}",
        params,
    )
    row = await cursor.fetchone()
    first_seen = row[0]
    last_seen = row[1]

    # Last effective classification (most recent failure, with user override)
    cursor = await db.execute(
        f"SELECT COALESCE(tc_latest.classification, fh.classification)"
        f" FROM failure_history fh"
        f" {_TC_LATEST_JOIN}"
        f" WHERE fh.test_name = ?{job_filter}"
        f" ORDER BY fh.analyzed_at DESC, fh.id DESC LIMIT 1",
        params,
    )
    last_classification = (await cursor.fetchone())[0] or ""

    return failures, first_seen, last_seen, last_classification


async def _get_classification_breakdown(
    db: aiosqlite.Connection,
    job_filter: str,
    params: list,
) -> dict[str, int]:
    """Return a dict mapping effective classification labels to their counts.

    Uses the latest user override from test_classifications if available,
    otherwise falls back to the AI classification in failure_history.

    Args:
        db: Open aiosqlite connection with row_factory set.
        job_filter: SQL fragment for optional job_name/exclude_job_id filtering.
        params: Bind parameters matching the job_filter placeholders
                (first element is always test_name).
    """
    cursor = await db.execute(
        f"SELECT COALESCE(tc_latest.classification, fh.classification) AS eff_class, COUNT(*)"
        f" FROM failure_history fh"
        f" {_TC_LATEST_JOIN}"
        f" WHERE fh.test_name = ?{job_filter}"
        f" GROUP BY eff_class",
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
    async with _connect_db() as db:
        # Build optional job_name filter (fh-prefixed for JOINed queries)
        job_filter = ""
        params: list = [test_name]
        if job_name:
            job_filter = " AND fh.job_name = ?"
            params.append(job_name)
        if exclude_job_id:
            job_filter += " AND fh.job_id != ?"
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
            f"""SELECT fh.job_id, fh.job_name, fh.build_number, fh.error_message,
                       fh.error_signature,
                       COALESCE(tc_latest.classification, fh.classification) AS classification,
                       fh.child_job_name, fh.child_build_number, fh.analyzed_at,
                       fh.tracked_in_url, fh.tracked_in_type
                FROM failure_history fh
                {_TC_LATEST_JOIN}
                WHERE fh.test_name = ?{job_filter}
                ORDER BY fh.analyzed_at DESC, fh.id DESC LIMIT ?""",
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
            f"SELECT COUNT(*) FROM failure_history fh WHERE fh.test_name = ?{job_filter}",
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
    async with _connect_db() as db:
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
        f"search_by_signature: signature={signature}, "
        f"total_occurrences={total_occurrences}, "
        f"unique_tests={unique_tests}"
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
    async with _connect_db() as db:
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
        now = datetime.now(tz=UTC)
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

# Statuses whose background task is irrecoverably lost after a restart.
# These are a subset of ACTIVE_STATUSES — "waiting" is excluded because
# waiting jobs can be safely resumed.
ORPHAN_STATUSES = ("pending", "running")
RESTART_ERROR_MSG = "Analysis interrupted by server restart. Please re-submit."

DEFAULT_DASHBOARD_LIMIT = 500


async def count_active_analyses() -> int:
    """Return the number of analyses with an active status.

    Active statuses are: running, pending, waiting.
    Uses a lightweight COUNT query — no result_json is fetched.
    """
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    async with _connect_db() as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM results WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def list_distinct_job_names() -> set[str]:
    """Return distinct non-empty job names from results. Lightweight query for backfill."""
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT DISTINCT json_extract(result_json, '$.job_name') AS job_name "
            "FROM results WHERE result_json IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows if row[0]}


def _parse_dashboard_row(row) -> dict:
    """Parse a single dashboard result row into a dict with summary data."""
    entry: dict = {
        "job_id": row["job_id"],
        "jenkins_url": row["jenkins_url"],
        "status": row["status"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"] if "completed_at" in row.keys() else None,
        "analysis_started_at": row["analysis_started_at"]
        if "analysis_started_at" in row.keys()
        else None,
        "error": row["error"] if "error" in row.keys() else "",
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
        raw_tags = result_data.get("tags")
        if isinstance(raw_tags, list):
            entry["tags"] = [str(t) for t in raw_tags if isinstance(t, str) and t]
        # Expose submitted_by for ownership checks (delete permissions)
        request_params = result_data.get("request_params") or {}
        submitted_by = request_params.get("submitted_by", "")
        if submitted_by:
            entry["submitted_by"] = submitted_by
    return entry


_DASHBOARD_BASE_SQL = """
    SELECT r.job_id, r.jenkins_url, r.status, r.result_json,
        r.created_at, r.completed_at, r.analysis_started_at, r.error,
        (SELECT COUNT(*) FROM failure_reviews fr
         WHERE fr.job_id = r.job_id AND fr.reviewed = 1) AS reviewed_count,
        (SELECT COUNT(*) FROM comments c
         WHERE c.job_id = r.job_id) AS comment_count
    FROM results r
"""


async def list_results_for_dashboard(
    limit: int = DEFAULT_DASHBOARD_LIMIT,
    offset: int = 0,
) -> list[dict]:
    """List analysis results with summary data for dashboard display.

    Unlike list_results, this function also extracts key fields from result_json
    for any row that has a stored result (job_name, build_number, failure_count).

    Args:
        limit: Maximum number of results to return.  ``0`` means no limit —
            all rows are returned.  Defaults to :data:`DEFAULT_DASHBOARD_LIMIT`.
        offset: Number of rows to skip (for pagination).

    Returns:
        List of result dictionaries enriched with summary data from result_json.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")

    async with _connect_db() as db:
        sql = _DASHBOARD_BASE_SQL + " ORDER BY r.created_at DESC"
        params: list = []
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        elif offset > 0:
            # SQLite requires LIMIT before OFFSET; use -1 for unlimited
            sql += " LIMIT -1"
        if offset > 0:
            sql += " OFFSET ?"
            params.append(offset)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [_parse_dashboard_row(row) for row in rows]


async def list_results_for_dashboard_filtered(
    *,
    job_names: set[str] | None = None,
    exclude_job_names: set[str] | None = None,
    search: str = "",
    status: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    review_status: str = "all",
    limit: int = DEFAULT_DASHBOARD_LIMIT,
    offset: int = 0,
) -> dict:
    """List dashboard results with filters applied in SQL before LIMIT.

    Filters are pushed into the SQL query so the LIMIT applies to the
    **filtered** set, not before filtering.

    Args:
        job_names: Include only these job names (from metadata filtering).
        exclude_job_names: Exclude these job names (from metadata exclude labels).
        search: Text search on job_name or job_id (case-insensitive).
        status: Filter by status values.
        date_from: ISO date string for start of range (inclusive).
        date_to: ISO date string for end of range (inclusive, end-of-day).
        limit: Max results. 0 = no limit.
        offset: Rows to skip.

    Returns:
        Dict with 'jobs' (list of dashboard dicts) and 'total' (total matching count).
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")

    conditions: list[str] = []
    params: list = []

    if search:
        # Search job_name in result_json and job_id
        conditions.append(
            "(r.job_id LIKE ? OR json_extract(r.result_json, '$.job_name') LIKE ?)"
        )
        like_param = f"%{search}%"
        params.extend([like_param, like_param])

    if status:
        # 'timeout' is a virtual status derived from 'failed' + error/summary patterns.
        # Translate it to a SQL predicate matching the frontend isAnalysisTimeout() logic.
        real_statuses = [s for s in status if s != "timeout"]
        has_timeout = "timeout" in status
        status_parts: list[str] = []
        if real_statuses:
            placeholders = ", ".join("?" for _ in real_statuses)
            status_parts.append(f"r.status IN ({placeholders})")
            params.extend(real_statuses)
        if has_timeout:
            status_parts.append(
                "(r.status = 'failed' AND ("
                "lower(r.error) LIKE '%timed out%' OR "
                "lower(r.error) LIKE '%timeout%' OR "
                "lower(json_extract(r.result_json, '$.summary')) LIKE '%timed out%' OR "
                "lower(json_extract(r.result_json, '$.summary')) LIKE '%timeout%'"
                "))"
            )
        conditions.append(f"({' OR '.join(status_parts)})")

    if date_from:
        # Use timestamp bounds instead of date() to allow index usage
        conditions.append("r.created_at >= ?")
        params.append(f"{date_from}T00:00:00")

    if date_to:
        conditions.append("r.created_at <= ?")
        params.append(f"{date_to}T23:59:59")

    if review_status == "reviewed":
        conditions.append(
            "(SELECT COUNT(*) FROM failure_reviews fr "
            "WHERE fr.job_id = r.job_id AND fr.reviewed = 1) > 0"
        )
    elif review_status == "not_reviewed":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM failure_reviews fr "
            "WHERE fr.job_id = r.job_id AND fr.reviewed = 1)"
        )

    if job_names is not None:
        if not job_names:
            # No matching job names from metadata — return empty
            return {"jobs": [], "total": 0}
        names_list = sorted(job_names)
        placeholders = ", ".join("?" for _ in names_list)
        conditions.append(
            f"json_extract(r.result_json, '$.job_name') IN ({placeholders})"
        )
        params.extend(names_list)

    if exclude_job_names:
        exclude_list = sorted(exclude_job_names)
        placeholders = ", ".join("?" for _ in exclude_list)
        conditions.append(
            f"(json_extract(r.result_json, '$.job_name') IS NULL OR "
            f"json_extract(r.result_json, '$.job_name') NOT IN ({placeholders}))"
        )
        params.extend(exclude_list)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    async with _connect_db() as db:
        # Count total matching rows
        count_sql = f"SELECT COUNT(*) FROM results r{where}"
        cursor = await db.execute(count_sql, params)
        total = (await cursor.fetchone())[0]

        # Fetch paginated results
        sql = _DASHBOARD_BASE_SQL + where + " ORDER BY r.created_at DESC"
        fetch_params = list(params)
        if limit > 0:
            sql += " LIMIT ?"
            fetch_params.append(limit)
        elif offset > 0:
            # SQLite requires LIMIT before OFFSET; use -1 for unlimited
            sql += " LIMIT -1"
        if offset > 0:
            sql += " OFFSET ?"
            fetch_params.append(offset)

        cursor = await db.execute(sql, fetch_params)
        rows = await cursor.fetchall()
        return {"jobs": [_parse_dashboard_row(row) for row in rows], "total": total}


async def get_parent_job_name_for_test(test_name: str, job_id: str = "") -> str:
    """Look up the parent pipeline job name for a test from failure_history.

    Args:
        test_name: The test name to look up.
        job_id: When provided, scopes the lookup to a specific analysis job
                to avoid cross-job leakage.
    """
    async with _connect_db() as db:
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
    """Set a pattern classification for a test (e.g., FLAKY, REGRESSION).

    Despite the parameter name ``classification``, the value is actually
    stored in the ``pattern`` column (two-axis system).  The parameter
    name is kept for backward compatibility with the API contract.

    Can be set by the AI during analysis or by humans.

    Args:
        classification: Pattern label (NEW, REGRESSION, FLAKY, etc.).
        job_id: Required — scopes the classification to a specific analysis job.
        visible: Whether the classification is immediately visible.
            Set to 0 during AI analysis; revealed after analysis completes.
    """
    if classification not in PATTERN_CLASSIFICATIONS:
        raise ValueError(
            f"Invalid pattern classification: {classification}. "
            f"Valid: {', '.join(sorted(PATTERN_CLASSIFICATIONS))}"
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
    async with _connect_db() as db:
        cursor = await db.execute(
            "INSERT INTO test_classifications"
            " (test_name, job_name, parent_job_name, classification,"
            " pattern, reason, references_info, created_by, job_id,"
            " child_build_number, visible) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                test_name,
                job_name,
                parent_job_name,
                "",  # classification column empty — history analysis only sets pattern
                classification,  # pattern column gets the pattern label
                reason,
                references,
                created_by,
                job_id,
                child_build_number,
                visible,
            ),
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

    async with _connect_db() as db:
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

    failure_history.classification is NOT updated — it always retains the
    original AI classification.  The effective classification is resolved
    at query time via test_classifications.
    """
    async with _connect_db() as db:
        result = await db.execute(
            "UPDATE test_classifications SET visible = 1 WHERE job_id = ? AND visible = 0",
            (job_id,),
        )
        await db.commit()
    logger.debug(
        f"make_classifications_visible: job_id={job_id}, revealed={result.rowcount} classifications"
    )


async def get_all_failures(
    search: str = "",
    job_name: str = "",
    classification: str = "",
    limit: int = 50,
    offset: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Get paginated failure history with optional filters.

    Returns dict with 'failures' list and 'total' count.

    Args:
        search: Free-text search across test_name, error_message, and job_name.
        job_name: Exact match filter on job_name column.
        classification: Exact match filter on classification column.
        limit: Maximum number of rows to return.
        offset: Number of rows to skip for pagination.
        date_from: Filter failures on or after this date (YYYY-MM-DD).
        date_to: Filter failures on or before this date (YYYY-MM-DD).

    Returns:
        Dict with ``failures`` (list of row dicts) and ``total`` (int).
    """
    logger.debug(
        f"get_all_failures: search={search!r}, "
        f"job_name={job_name!r}, "
        f"classification={classification!r}, "
        f"limit={limit}, offset={offset}, "
        f"date_from={date_from!r}, date_to={date_to!r}"
    )
    conditions: list[str] = []
    params: list[str | int] = []

    if search:
        conditions.append(
            "(fh.test_name LIKE ? OR fh.error_message LIKE ? OR fh.job_name LIKE ?)"
        )
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if job_name:
        conditions.append("fh.job_name = ?")
        params.append(job_name)
    if classification:
        conditions.append("COALESCE(tc_latest.classification, fh.classification) = ?")
        params.append(classification)

    _build_date_filter("fh.analyzed_at", date_from, date_to, conditions, params)

    where = " AND ".join(conditions) if conditions else "1=1"

    async with _connect_db() as db:
        # Get total count
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM failure_history fh {_TC_LATEST_JOIN} WHERE {where}",
            params,
        )
        total = (await cursor.fetchone())[0]

        # Get paginated results
        cursor = await db.execute(
            f"SELECT fh.id, fh.job_id, fh.job_name, fh.build_number, fh.test_name, "
            f"fh.error_message, fh.error_signature, "
            f"COALESCE(tc_latest.classification, fh.classification) AS classification, "
            f"fh.child_job_name, fh.child_build_number, fh.analyzed_at "
            f"FROM failure_history fh"
            f" {_TC_LATEST_JOIN}"
            f" WHERE {where} ORDER BY fh.analyzed_at DESC, fh.id DESC LIMIT ? OFFSET ?",
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
    await db.execute("DELETE FROM chat_messages WHERE job_id = ?", (job_id,))
    cursor = await db.execute("DELETE FROM results WHERE job_id = ?", (job_id,))
    return cursor.rowcount > 0


async def delete_job(job_id: str) -> bool:
    """Delete an analyzed job and all its related data."""
    async with _connect_db() as db:
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
    async with _connect_db() as db:
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


async def _override_failure_field(
    job_id: str,
    test_name: str,
    field: str,
    value: str,
    child_job_name: str,
    child_build_number: int,
    username: str,
    parent_job_name: str,
) -> list[str]:
    """Shared logic for overriding classification or pattern in failure_history.

    1. Look up the error_signature for the test (scoped by child context).
    2. Read the current value of *field* before mutating (for original_* tracking).
    3. UPDATE all failure_history rows sharing the same signature.
    4. INSERT a test_classifications row for every test in the group.

    Args:
        job_id: The analysis job ID.
        test_name: Fully qualified test name (representative test from the group).
        field: Column to override — ``"classification"`` or ``"pattern"``.
        value: New value to set.
        child_job_name: Child job name (for pipeline analyses).
        child_build_number: Child build number (0 = wildcard by name).
        username: User who made the override.
        parent_job_name: Parent pipeline job name.

    Returns:
        List of all test names in the affected signature group.
    """
    _validate_child_identifier_pairing(child_job_name, child_build_number)
    child_sql, child_params = _child_scope_sql(child_job_name, child_build_number)
    is_wildcard = bool(child_job_name and child_build_number == 0)
    async with _connect_db() as db:
        # Look up error_signature so we can update all grouped failures.
        sig_query = (
            "SELECT error_signature FROM failure_history "
            "WHERE job_id = ? AND test_name = ?"
        )
        sig_params: list = [job_id, test_name, *child_params]
        sig_query += child_sql + " ORDER BY analyzed_at DESC LIMIT 1"

        cursor = await db.execute(sig_query, sig_params)
        row = await cursor.fetchone()
        error_signature = row[0] if row and row[0] else ""

        # Collect all test_names in the signature group BEFORE
        # the UPDATE so we can read per-test original values.
        if error_signature:
            group_cursor = await db.execute(
                "SELECT DISTINCT test_name FROM failure_history "
                f"WHERE job_id = ? AND error_signature = ?{child_sql}",
                (job_id, error_signature, *child_params),
            )
            group_tests = [r[0] for r in await group_cursor.fetchall()]
        else:
            group_tests = [test_name]

        # Read original values per-test BEFORE the UPDATE mutates them.
        orig_values: dict[str, str] = {}
        for t in group_tests:
            orig_cursor = await db.execute(
                f"SELECT {field} FROM failure_history "
                f"WHERE job_id = ? AND test_name = ?{child_sql}"
                " ORDER BY analyzed_at DESC LIMIT 1",
                [job_id, t, *child_params],
            )
            orig_row = await orig_cursor.fetchone()
            orig_values[t] = orig_row[0] if orig_row and orig_row[0] else ""

        # UPDATE failure_history rows.
        if error_signature:
            await db.execute(
                f"""UPDATE failure_history
                   SET {field} = ?
                   WHERE job_id = ? AND error_signature = ?{child_sql}""",
                (value, job_id, error_signature, *child_params),
            )
        else:
            await db.execute(
                f"""UPDATE failure_history
                   SET {field} = ?
                   WHERE job_id = ? AND test_name = ?{child_sql}""",
                (value, job_id, test_name, *child_params),
            )

        # Resolve build numbers for test_classifications INSERT.
        # Wildcard overrides must fan out to each actual build number
        # so reports JOINs on exact child_build_number still match.
        if is_wildcard and error_signature:
            builds_cursor = await db.execute(
                "SELECT DISTINCT child_build_number FROM failure_history "
                "WHERE job_id = ? AND error_signature = ? AND child_job_name = ?",
                (job_id, error_signature, child_job_name),
            )
            build_numbers = [r[0] for r in await builds_cursor.fetchall()]
        elif is_wildcard:
            builds_cursor = await db.execute(
                "SELECT DISTINCT child_build_number FROM failure_history "
                "WHERE job_id = ? AND test_name = ? AND child_job_name = ?",
                (job_id, test_name, child_job_name),
            )
            build_numbers = [r[0] for r in await builds_cursor.fetchall()]
        else:
            build_numbers = [child_build_number]

        # Build classification-specific or pattern-specific INSERT columns.
        if field == "classification":
            extra_cols = "classification, original_classification"
            extra_vals = "?, ?"
            reason = "User override"
        else:
            extra_cols = "classification, pattern, original_pattern"
            extra_vals = "?, ?, ?"
            reason = "User pattern override"

        for t in group_tests:
            original = orig_values[t]
            if field == "classification":
                extra_params: tuple = (value, original)
            else:
                extra_params = ("", value, original)

            for build_num in build_numbers:
                await db.execute(
                    f"INSERT INTO test_classifications "
                    f"(test_name, job_name, parent_job_name, job_id, {extra_cols}, "
                    f"reason, created_by, visible, child_build_number) "
                    f"VALUES (?, ?, ?, ?, {extra_vals}, ?, ?, 1, ?)",
                    (
                        t,
                        child_job_name,
                        parent_job_name,
                        job_id,
                        *extra_params,
                        reason,
                        username,
                        build_num,
                    ),
                )

        await db.commit()
    return group_tests


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

    Returns:
        List of all test names in the affected signature group.
    """
    logger.debug(
        f"override_classification: job_id={job_id}, test_name={test_name}, "
        f"classification={classification}, username={username}"
    )
    group_tests = await _override_failure_field(
        job_id=job_id,
        test_name=test_name,
        field="classification",
        value=classification,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        username=username,
        parent_job_name=parent_job_name,
    )
    logger.info(
        f"Classification overridden: job_id={job_id}, test_name={test_name}, "
        f"classification={classification}, by={username or 'unknown'}"
    )
    return group_tests


async def override_pattern(
    job_id: str,
    test_name: str,
    pattern: str,
    child_job_name: str = "",
    child_build_number: int = 0,
    username: str = "",
    parent_job_name: str = "",
) -> list[str]:
    """Override the pattern axis of a failure in failure_history.

    Updates ALL failure_history rows sharing the same error_signature
    (within the same job) so that grouped failures stay in sync.
    Also inserts a test_classifications entry for tracking.

    Returns:
        List of all test names in the affected signature group.
    """
    logger.debug(
        f"override_pattern: job_id={job_id}, test_name={test_name}, "
        f"pattern={pattern}, username={username}"
    )
    group_tests = await _override_failure_field(
        job_id=job_id,
        test_name=test_name,
        field="pattern",
        value=pattern,
        child_job_name=child_job_name,
        child_build_number=child_build_number,
        username=username,
        parent_job_name=parent_job_name,
    )
    logger.info(
        f"Pattern overridden: job_id={job_id}, test_name={test_name}, "
        f"pattern={pattern}, by={username or 'unknown'}"
    )
    return group_tests


async def get_history_classification(
    job_id: str,
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
) -> str:
    """Return the pattern classification for a test.

    Pattern values: ``NEW``, ``REGRESSION``, ``FLAKY``,
    ``INTERMITTENT``, ``KNOWN_BUG``, ``PERSISTENT``.
    Primary classifications (``CODE ISSUE`` / ``PRODUCT BUG``) are
    intentionally excluded — use :func:`get_effective_classification`
    for those.

    Checks ``test_classifications.pattern`` first (visible entries),
    then falls back to ``failure_history.pattern``.

    Args:
        job_id: Analysis job identifier.
        test_name: Fully qualified test name.
        child_job_name: Optional child job name for scoping.
        child_build_number: Optional child build number for scoping.

    Returns:
        The pattern string (e.g. ``"REGRESSION"``),
        or ``""`` if no pattern classification exists.
    """
    child_sql, child_params = _child_scope_sql(child_job_name, child_build_number)
    tc_sql, tc_params = _child_scope_sql(
        child_job_name, child_build_number, name_column="job_name"
    )

    async with _connect_db() as db:
        # 1. Prefer visible entry from test_classifications (pattern column).
        override_row = await (
            await db.execute(
                "SELECT pattern FROM test_classifications"
                " WHERE test_name = ? AND job_id = ?"
                f"{tc_sql}"
                " AND visible = 1"
                f" AND pattern IN {_PATTERN_CLASSIFICATIONS_SQL}"
                " ORDER BY id DESC LIMIT 1",
                [test_name, job_id, *tc_params],
            )
        ).fetchone()
        if override_row and override_row[0]:
            return override_row[0]

        # 2. Fall back to failure_history.pattern
        fh_query = (
            "SELECT pattern FROM failure_history"
            " WHERE job_id = ? AND test_name = ?"
            f" AND pattern IN {_PATTERN_CLASSIFICATIONS_SQL}"
        )
        fh_params: list = [job_id, test_name, *child_params]
        fh_query += child_sql + " ORDER BY analyzed_at DESC, id DESC LIMIT 1"

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
    child_sql, child_params = _child_scope_sql(child_job_name, child_build_number)
    tc_sql, tc_params = _child_scope_sql(
        child_job_name, child_build_number, name_column="job_name"
    )

    async with _connect_db() as db:
        # 1. Prefer visible override from test_classifications
        override_row = await (
            await db.execute(
                "SELECT classification FROM test_classifications"
                " WHERE test_name = ? AND job_id = ?"
                f"{tc_sql}"
                " AND visible = 1"
                f" AND classification IN {_PRIMARY_CLASSIFICATIONS_SQL}"
                " ORDER BY id DESC LIMIT 1",
                [test_name, job_id, *tc_params],
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
        fh_params: list = [job_id, test_name, *child_params]
        fh_query += child_sql + " ORDER BY analyzed_at DESC, id DESC LIMIT 1"

        fh_row = await (await db.execute(fh_query, fh_params)).fetchone()
        return fh_row[0] if fh_row and fh_row[0] else ""


async def get_all_effective_classifications(
    job_id: str,
) -> dict[tuple[str, str, int], str]:
    """Return all effective classification overrides for a job.

    Queries test_classifications once for all overrides in the given job,
    avoiding N+1 queries when applying overrides to many failures.

    Returns:
        Dict mapping (test_name, child_job_name, child_build_number) to
        the effective classification string.
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT test_name, job_name, child_build_number, classification "
            "FROM test_classifications "
            "WHERE job_id = ? AND visible = 1 "
            f"AND classification IN {_PRIMARY_CLASSIFICATIONS_SQL} "
            "ORDER BY created_at DESC, id DESC",
            (job_id,),
        )
        rows = await cursor.fetchall()

    # Keep only the latest override per (test_name, job_name, child_build_number)
    result: dict[tuple[str, str, int], str] = {}
    for row in rows:
        key = (row["test_name"], row["job_name"], row["child_build_number"])
        if key not in result:
            result[key] = row["classification"]
    return result


async def mark_stale_results_failed() -> tuple[list[dict], list[dict]]:
    """Mark orphaned pending/running jobs as failed. Return waiting jobs for resumption.

    Pending and running jobs have lost their background task and cannot recover,
    so they are marked as failed with an error message and completion timestamp.

    Waiting jobs were polling Jenkins and can be safely resumed by re-creating
    their background task.

    Returns:
        Tuple of (waiting_jobs, recovered_jobs).
        - waiting_jobs: list of dicts with ``job_id`` and ``result_data`` for
          each waiting job to resume.
        - recovered_jobs: list of dicts with ``job_id`` and ``previous_status``
          for each job that was marked failed.
    """
    waiting_jobs: list[dict] = []
    recovered_jobs: list[dict] = []
    async with _connect_db() as db:
        # Collect orphaned job details before updating
        placeholders = ", ".join("?" for _ in ORPHAN_STATUSES)
        cursor = await db.execute(
            f"SELECT job_id, status FROM results WHERE status IN ({placeholders}) AND completed_at IS NULL",
            ORPHAN_STATUSES,
        )
        orphaned_rows = await cursor.fetchall()
        for row in orphaned_rows:
            recovered_jobs.append(
                {"job_id": row["job_id"], "previous_status": row["status"]}
            )

        # Mark orphaned jobs as failed with error message and completion timestamp
        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            f"UPDATE results SET status = 'failed', "
            f"error = ?, completed_at = ? "
            f"WHERE status IN ({placeholders}) AND completed_at IS NULL",
            (RESTART_ERROR_MSG, now, *ORPHAN_STATUSES),
        )
        if cursor.rowcount > 0:
            logger.warning(
                f"Marked {cursor.rowcount} stale job(s) as failed on startup"
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
                        "UPDATE results SET status = 'failed', "
                        "error = ?, completed_at = ? WHERE job_id = ?",
                        (RESTART_ERROR_MSG, now, row["job_id"]),
                    )

        # Mark waiting jobs without result_json as failed (unrecoverable)
        cursor = await db.execute(
            "UPDATE results SET status = 'failed', "
            "error = ?, completed_at = ? "
            "WHERE status = 'waiting' AND (result_json IS NULL OR result_json = '')",
            (RESTART_ERROR_MSG, now),
        )
        if cursor.rowcount > 0:
            logger.warning(
                f"Marked {cursor.rowcount} unrecoverable waiting job(s) as failed (missing result data)"
            )

        await db.commit()

    if waiting_jobs:
        logger.info(f"Found {len(waiting_jobs)} waiting job(s) to resume")

    return waiting_jobs, recovered_jobs


# --- Auth storage functions ---


async def create_admin_user(username: str) -> tuple[str, str]:
    """Create an admin user and return (username, raw_api_key).
    Raises ValueError if username is invalid or taken."""
    username = _normalize_username(username)
    _validate_username(username)
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    async with _connect_db() as db:
        try:
            await db.execute(
                "INSERT INTO users (username, api_key_hash, role) VALUES (?, ?, 'admin')",
                (username, key_hash),
            )
            await db.commit()
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                msg = f"User '{username}' already exists"
                raise ValueError(msg) from exc
            raise
    return username, raw_key


async def get_user_by_key(api_key: str) -> dict | None:
    """Look up a user by their raw API key."""
    key_hash = hash_api_key(api_key)
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, username, role, created_at, last_seen FROM users WHERE api_key_hash = ?",
            (key_hash,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_user_by_username(username: str) -> dict | None:
    """Look up a user by username."""
    username = _normalize_username(username)
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, username, role, created_at, last_seen FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_user(username: str) -> bool:
    """Delete a user and their sessions. Returns True if deleted."""
    username = _normalize_username(username)
    async with _connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM users WHERE username = ?",
            (username,),
        )
        if cursor.rowcount > 0:
            await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        await db.commit()
        return cursor.rowcount > 0


async def change_user_role(username: str, new_role: str) -> tuple[str, str]:
    """Change a user's role. Returns (username, raw_api_key).

    When promoting to admin, generates a new API key only if the user
    doesn't already have one.
    For other role changes, the existing API key is preserved.

    Args:
        username: The user to change.
        new_role: The new role ('reviewer', 'operator', or 'admin').

    Returns:
        Tuple of (username, raw_api_key). raw_api_key is empty when
        not promoting to admin or when the user already has a key.

    Raises:
        ValueError: If username not found, role is invalid, or already has the role.
    """
    username = _normalize_username(username)
    if new_role not in VALID_ROLES:
        msg = f"Invalid role: '{new_role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}."
        raise ValueError(msg)
    if username == "admin":
        msg = "Cannot change role of reserved 'admin' user"
        raise ValueError(msg)
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT username, role, api_key_hash FROM users WHERE username = ?",
            (username,),
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
            # Promoting to admin — only generate a key if user doesn't have one
            if not user["api_key_hash"]:
                raw_key = generate_api_key()
                key_hash = hash_api_key(raw_key)
            await db.execute("BEGIN IMMEDIATE")
            try:
                if raw_key:
                    cursor = await db.execute(
                        "UPDATE users SET role = 'admin', api_key_hash = ? WHERE username = ?",
                        (key_hash, username),
                    )
                else:
                    cursor = await db.execute(
                        "UPDATE users SET role = 'admin' WHERE username = ?",
                        (username,),
                    )
                if cursor.rowcount == 0:
                    await db.execute("ROLLBACK")
                    raise ValueError(f"User '{username}' not found")
                # Invalidate all sessions so user must re-login with new role
                await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
                await db.commit()
            except ValueError:
                raise
            except Exception:
                await db.execute("ROLLBACK")
                raise
        else:
            # Changing to reviewer or operator — preserve existing API key
            # so the user can keep logging in.
            await db.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (new_role, username),
            )
            # Invalidate all sessions so user must re-login with new role
            await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
            await db.commit()

            return username, raw_key

    return username, raw_key


async def list_users() -> list[dict]:
    """List all users (without key hashes).

    Excludes the reserved 'admin' username (bootstrap superuser
    authenticated via ADMIN_KEY, not a real managed user).
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, username, role, status, created_at, last_seen FROM users WHERE username != 'admin' ORDER BY created_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]


async def track_user(username: str) -> None:
    """Track user activity — insert if new, update last_seen if existing.

    Skips the reserved 'admin' username (bootstrap superuser).
    New users are assigned the DEFAULT_USER_ROLE from settings.
    """
    username = _normalize_username(username)
    if username == "admin":
        return
    if username in _SYSTEM_TAGS:
        return
    # Skip invalid usernames (e.g. from malformed cookies)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,49}$", username):
        return

    from rootcoz.config import get_settings

    default_role = get_settings().default_user_role
    async with _connect_db() as db:
        await db.execute(
            "INSERT INTO users (username, role) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
            (username, default_role),
        )
        await db.commit()


async def create_session(
    username: str,
    is_admin: bool = False,
    ttl_hours: int = SESSION_TTL_HOURS,
    role: str = "reviewer",
) -> str:
    """Create an opaque session token. Returns raw token."""
    token = secrets.token_urlsafe(32)
    token_hash = _hash_session_token(token)
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
    async with _connect_db() as db:
        await db.execute(
            "INSERT INTO sessions (token, username, is_admin, expires_at, role)"
            " VALUES (?, ?, ?, ?, ?)",
            (token_hash, username, 1 if is_admin else 0, expires_str, role),
        )
        await db.commit()
    return token


async def get_session(token: str) -> dict | None:
    """Look up a session. Returns None if expired or not found."""
    token_hash = _hash_session_token(token)
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT username, is_admin, role, created_at, expires_at"
            " FROM sessions"
            " WHERE token = ? AND expires_at > datetime('now')",
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
    new_expires = datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)
    expires_str = new_expires.strftime("%Y-%m-%d %H:%M:%S")
    async with _connect_db() as db:
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
    async with _connect_db() as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token_hash,))
        await db.commit()


def _validate_user_status(status: str) -> None:
    """Validate that status is one of the allowed user statuses."""
    if status not in VALID_USER_STATUSES:
        msg = f"Invalid status: '{status}'. Must be one of {VALID_USER_STATUSES}."
        raise ValueError(msg)


async def create_user(
    username: str, *, status: str = "active", role: str = ""
) -> tuple[str, str]:
    """Create a new user or generate an API key for an existing user without one.

    Returns (username, raw_api_key).
    Raises ValueError if username is invalid, reserved, or user already has a key.

    Args:
        username: The username to create.
        status: Initial user status ('active', 'pending', 'rejected').
        role: Role to assign. Empty string uses DEFAULT_USER_ROLE from settings.

    Uses BEGIN IMMEDIATE to prevent TOCTOU races between the
    existence check and the INSERT.
    """
    username = _normalize_username(username)
    _validate_username(username)
    _validate_user_status(status)

    if not role:
        from rootcoz.config import get_settings

        role = get_settings().default_user_role
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)

    async with _connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT id, role, api_key_hash FROM users WHERE username = ?",
                (username,),
            )
            existing = await cursor.fetchone()

            if existing:
                if existing["role"] == "admin":
                    msg = f"User '{username}' is not eligible for self-registration"
                    raise ValueError(msg)
                if existing["api_key_hash"]:
                    msg = f"User '{username}' already has an API key. Please log in."
                    raise ValueError(msg)
                # Existing user without key — generate one
                update_cursor = await db.execute(
                    "UPDATE users SET api_key_hash = ? WHERE username = ? AND role != 'admin'",
                    (key_hash, username),
                )
                if update_cursor.rowcount == 0:
                    msg = f"User '{username}' not found or is an admin user"
                    raise ValueError(msg)
            else:
                await db.execute(
                    "INSERT INTO users (username, api_key_hash, role, status) VALUES (?, ?, ?, ?)",
                    (username, key_hash, role, status),
                )
            await db.commit()
        except ValueError:
            await db.execute("ROLLBACK")
            raise
        except Exception as exc:
            await db.execute("ROLLBACK")
            if "UNIQUE constraint" in str(exc):
                msg = f"User '{username}' already has an API key"
                raise ValueError(msg) from exc
            raise
    return username, raw_key


async def register_user_with_status(
    username: str, api_key_hash: str, status: str = "active"
) -> int:
    """Register a user with a specific status.

    Creates a new user row with the given API key hash and status.
    Returns the user's row ID.

    Raises ValueError if username is invalid, status is invalid, or already exists.
    """
    username = _normalize_username(username)
    _validate_username(username)
    _validate_user_status(status)
    from rootcoz.config import get_settings

    default_role = get_settings().default_user_role
    async with _connect_db() as db:
        try:
            cursor = await db.execute(
                "INSERT INTO users (username, api_key_hash, role, status) VALUES (?, ?, ?, ?)",
                (username, api_key_hash, default_role, status),
            )
            await db.commit()
            return cursor.lastrowid
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                msg = f"User '{username}' already exists"
                raise ValueError(msg) from exc
            raise


async def set_user_status(username: str, status: str) -> bool:
    """Set user status (active/pending/rejected).

    Returns True if the user was found and updated, False otherwise.
    """
    username = _normalize_username(username)
    _validate_user_status(status)
    async with _connect_db() as db:
        cursor = await db.execute(
            "UPDATE users SET status = ? WHERE username = ? AND role != 'admin'",
            (status, username),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user_status(username: str) -> str | None:
    """Get user status. Returns None if user not found."""
    username = _normalize_username(username)
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT status FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row["status"] if "status" in row.keys() else "active"


async def list_pending_users() -> list[dict]:
    """List users with pending status."""
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT id, username, role, status, created_at, last_seen "
            "FROM users WHERE status = 'pending' AND role != 'admin' ORDER BY created_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]


async def rotate_user_key(username: str) -> str:
    """Generate a new API key for a non-admin user. Returns the raw key.

    Raises ValueError if user not found or is an admin user.
    """
    username = _normalize_username(username)
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    async with _connect_db() as db:
        cursor = await db.execute(
            "UPDATE users SET api_key_hash = ? WHERE username = ? AND role != 'admin'",
            (key_hash, username),
        )
        if cursor.rowcount == 0:
            msg = f"User '{username}' not found or is an admin user"
            raise ValueError(msg)
        # Invalidate all existing sessions for this user
        await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        await db.commit()
    return raw_key


async def rotate_own_key(username: str) -> str:
    """Rotate the API key for any user (self-service). Returns the raw key.

    Works for both regular users and admin users.
    Raises ValueError if user not found.
    """
    username = _normalize_username(username)
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    async with _connect_db() as db:
        cursor = await db.execute(
            "UPDATE users SET api_key_hash = ? WHERE username = ?",
            (key_hash, username),
        )
        if cursor.rowcount == 0:
            msg = f"User '{username}' not found"
            raise ValueError(msg)
        # Invalidate all existing sessions for this user
        await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        await db.commit()
    return raw_key


async def user_has_key(username: str) -> bool:
    """Check if a user has an API key set."""
    username = _normalize_username(username)
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT api_key_hash FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        return bool(row and row["api_key_hash"])


async def rotate_admin_key(username: str, custom_key: str | None = None) -> str:
    """Generate or set a new API key for an admin user. Returns the raw new key."""
    username = _normalize_username(username)
    if custom_key:
        validate_api_key(custom_key)
        raw_key = custom_key
    else:
        raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    async with _connect_db() as db:
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


async def cleanup_expired_sessions() -> int:
    """Remove expired sessions. Returns count deleted."""
    async with _connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM sessions WHERE expires_at <= datetime('now')"
        )
        await db.commit()
        return cursor.rowcount


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
    async with _connect_db() as db:
        await db.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE username = ?",  # columns are hardcoded literals
            params,
        )
        await db.commit()


async def get_user_tokens(username: str) -> dict[str, str]:
    """Get decrypted user tokens. Returns dict with github_token, jira_email, jira_token."""
    async with _connect_db() as db:
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
    async with _connect_db() as db:
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
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug("Failed to parse labels JSON, defaulting to empty: %s", e)
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
    async with _connect_db() as db:
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
    async with _connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM job_metadata WHERE job_name = ?",
            (job_name,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_jobs_with_metadata(
    *,
    team: str | list[str] = "",
    tier: str | list[str] = "",
    version: str | list[str] = "",
    labels: list[str] | None = None,
) -> list[dict]:
    """List all job metadata entries, optionally filtered.

    Filters combine with AND logic. Multiple labels require all to match.
    Each of *team*, *tier*, and *version* may be a single string **or** a
    list of strings.  When a list is given the filter matches any value in
    the list (OR within that field).

    Args:
        team: Filter by team (exact match or list of values).
        tier: Filter by tier (exact match or list of values).
        version: Filter by version (exact match or list of values).
        labels: Filter by labels (all must be present).

    Returns:
        List of metadata dicts.
    """
    conditions: list[str] = []
    params: list[str] = []

    for col, val in (("team", team), ("tier", tier), ("version", version)):
        if not val:
            continue
        if isinstance(val, list):
            placeholders = ", ".join("?" for _ in val)
            conditions.append(f"{col} IN ({placeholders})")
            params.extend(val)
        else:
            conditions.append(f"{col} = ?")
            params.append(val)

    where = " AND ".join(conditions) if conditions else "1=1"

    async with _connect_db() as db:
        cursor = await db.execute(
            f"SELECT job_name, team, tier, version, labels FROM job_metadata WHERE {where} ORDER BY job_name",
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
    async with _connect_db() as db:
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
    """Record a single AI call's token usage. Returns the record ID."""
    record_id = str(uuid.uuid4())
    total_tokens = input_tokens + output_tokens
    async with _connect_db() as db:
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
    async with _connect_db() as db:
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

    async with _connect_db() as db:
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
            f"FROM ai_token_usage{where_clause}"
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
                    "CASE WHEN COUNT(duration_ms) > 0"
                    " THEN COALESCE(SUM(duration_ms), 0)"
                    " / COUNT(duration_ms)"
                    " ELSE 0 END as avg_duration_ms "
                    f"FROM ai_token_usage{where_clause} "
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
    async with _connect_db() as db:
        periods = {
            "today": "date(created_at) = date('now')",
            "this_week": "created_at >= datetime('now', '-7 days')",
            "this_month": "created_at >= datetime('now', '-30 days')",
        }

        result: dict = {}
        for period_name, condition in periods.items():
            cursor = await db.execute(
                f"SELECT COUNT(*) as calls, "
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
    async with _connect_db() as db:
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
    async with _connect_db() as db:
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
    async with _connect_db() as db:
        placeholders = ",".join("?" for _ in usernames)
        cursor = await db.execute(
            f"SELECT username, endpoint, p256dh_key, auth_key "
            f"FROM push_subscriptions WHERE username IN ({placeholders})",
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
    async with _connect_db() as db:
        placeholders = ",".join("?" for _ in endpoints)
        await db.execute(
            f"DELETE FROM push_subscriptions WHERE endpoint IN ({placeholders})",
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

    async with _connect_db() as db:
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
    async with _connect_db() as db:
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
    async with _connect_db() as db:
        await db.executemany(
            "INSERT OR IGNORE INTO mention_reads (username, comment_id) VALUES (?, ?)",
            [(username, cid) for cid in comment_ids],
        )
        await db.commit()

    logger.info(
        f"mark_all_mentions_read: username={username}, marked={len(comment_ids)}"
    )
    return len(comment_ids)


async def get_server_settings() -> dict[str, dict]:
    """Get all server setting overrides from DB.

    Returns dict of {key: {"value": str, "updated_by": str, "updated_at": str}}.
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT key, value, updated_by, updated_at FROM server_settings"
        )
        rows = await cursor.fetchall()
        return {
            row["key"]: {
                "value": row["value"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }


async def get_server_setting(key: str) -> dict | None:
    """Get a single server setting override. Returns None if not set."""
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT key, value, updated_by, updated_at FROM server_settings WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "value": row["value"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }


async def set_server_setting(key: str, value: str, updated_by: str = "") -> None:
    """Set a server setting override (upsert). Records history."""
    async with _connect_db() as db:
        # Read previous value for history
        cursor = await db.execute(
            "SELECT value FROM server_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        previous_value = row["value"] if row else None

        await db.execute(
            """INSERT INTO server_settings (key, value, updated_by, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                 value = excluded.value,
                 updated_by = excluded.updated_by,
                 updated_at = excluded.updated_at""",
            (key, value, updated_by),
        )
        # Record in history
        await db.execute(
            """INSERT INTO server_settings_history (key, value, previous_value, action, changed_by)
               VALUES (?, ?, ?, 'set', ?)""",
            (key, value, previous_value, updated_by),
        )
        await db.commit()
    logger.info("Server setting updated: key=%s, by=%s", key, updated_by)


async def delete_server_setting(key: str, deleted_by: str = "") -> bool:
    """Delete a server setting override (reset to env/default). Records history."""
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT value FROM server_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        if not row:
            return False

        previous_value = row["value"]
        await db.execute(
            """INSERT INTO server_settings_history (key, value, previous_value, action, changed_by)
               VALUES (?, '', ?, 'reset', ?)""",
            (key, previous_value, deleted_by),
        )
        await db.execute("DELETE FROM server_settings WHERE key = ?", (key,))
        await db.commit()
    logger.info("Server setting reset: key=%s, by=%s", key, deleted_by)
    return True


async def get_server_settings_history(
    key: str | None = None, limit: int = 100
) -> list[dict]:
    """Get server settings change history, optionally filtered by key."""
    async with _connect_db() as db:
        if key:
            cursor = await db.execute(
                """SELECT id, key, value, previous_value, action, changed_by, changed_at
                   FROM server_settings_history
                   WHERE key = ?
                   ORDER BY id DESC LIMIT ?""",
                (key, limit),
            )
        else:
            cursor = await db.execute(
                """SELECT id, key, value, previous_value, action, changed_by, changed_at
                   FROM server_settings_history
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def add_chat_message(
    job_id: str,
    role: str,
    content: str,
    username: str = "",
    ai_provider: str = "",
    ai_model: str = "",
    session_id: str = "",
    status: str = "completed",
) -> int:
    """Add a chat message and return its id."""
    async with _connect_db() as db:
        cursor = await db.execute(
            "INSERT INTO chat_messages (job_id, role, content, username, ai_provider, ai_model, session_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                role,
                content,
                username,
                ai_provider,
                ai_model,
                session_id,
                status,
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def add_chat_message_pair(
    job_id: str,
    user_content: str,
    username: str = "",
    ai_provider: str = "",
    ai_model: str = "",
) -> tuple[int, int]:
    """Insert user message + assistant placeholder atomically.

    Returns (user_msg_id, assistant_msg_id).
    """
    async with _connect_db() as db:
        cursor = await db.execute(
            "INSERT INTO chat_messages (job_id, role, content, username, ai_provider, ai_model, session_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, "user", user_content, username, "", "", "", "completed"),
        )
        user_msg_id = cursor.lastrowid or 0
        cursor = await db.execute(
            "INSERT INTO chat_messages (job_id, role, content, username, ai_provider, ai_model, session_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, "assistant", "", username, ai_provider, ai_model, "", "pending"),
        )
        assistant_msg_id = cursor.lastrowid or 0
        await db.commit()
        return user_msg_id, assistant_msg_id


async def get_chat_messages(
    job_id: str, limit: int | None = 200, offset: int = 0, username: str = ""
) -> list[dict]:
    """Get chat messages for a job, ordered by id ASC.

    Args:
        limit: Max messages to return. None = no limit.
    """
    async with _connect_db() as db:
        limit_clause = "LIMIT ? OFFSET ?" if limit is not None else ""
        params: tuple
        if username:
            base = (
                "SELECT id, job_id, role, content, username, ai_provider, ai_model, session_id, status, created_at "
                f"FROM chat_messages WHERE job_id = ? AND username = ? ORDER BY id ASC {limit_clause}"
            )
            params = (
                (job_id, username, limit, offset)
                if limit is not None
                else (job_id, username)
            )
            cursor = await db.execute(base, params)
        else:
            base = (
                "SELECT id, job_id, role, content, username, ai_provider, ai_model, session_id, status, created_at "
                f"FROM chat_messages WHERE job_id = ? ORDER BY id ASC {limit_clause}"
            )
            params = (job_id, limit, offset) if limit is not None else (job_id,)
            cursor = await db.execute(base, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def count_chat_messages(job_id: str, username: str = "") -> int:
    """Count total chat messages for a job."""
    async with _connect_db() as db:
        if username:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE job_id = ? AND username = ?",
                (job_id, username),
            )
        else:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE job_id = ?",
                (job_id,),
            )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def delete_chat_message_by_id(msg_id: int) -> None:
    """Delete a single chat message by its id."""
    async with _connect_db() as db:
        await db.execute("DELETE FROM chat_messages WHERE id = ?", (msg_id,))
        await db.commit()


async def delete_chat_messages(job_id: str, username: str = "") -> int:
    """Delete all chat messages for a job (optionally scoped to a user). Returns count deleted."""
    async with _connect_db() as db:
        if username:
            cursor = await db.execute(
                "DELETE FROM chat_messages WHERE job_id = ? AND username = ?",
                (job_id, username),
            )
        else:
            cursor = await db.execute(
                "DELETE FROM chat_messages WHERE job_id = ?",
                (job_id,),
            )
        await db.commit()
        return cursor.rowcount


async def get_pending_chat_messages(job_id: str, username: str = "") -> list[dict]:
    """Get pending (queued) chat messages for a job."""
    async with _connect_db() as db:
        if username:
            cursor = await db.execute(
                "SELECT id, job_id, role, content, username, ai_provider, ai_model, session_id, status, created_at "
                "FROM chat_messages WHERE job_id = ? AND username = ? AND status = 'pending' ORDER BY id ASC",
                (job_id, username),
            )
        else:
            cursor = await db.execute(
                "SELECT id, job_id, role, content, username, ai_provider, ai_model, session_id, status, created_at "
                "FROM chat_messages WHERE job_id = ? AND status = 'pending' ORDER BY id ASC",
                (job_id,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_chat_message_status(msg_id: int) -> str | None:
    """Get the status of a chat message by ID. Returns None if not found."""
    async with _connect_db() as db:
        cursor = await db.execute(
            "SELECT status FROM chat_messages WHERE id = ?",
            (msg_id,),
        )
        row = await cursor.fetchone()
        return row["status"] if row else None


async def update_chat_message_status(msg_id: int, status: str) -> None:
    """Update the status of a chat message."""
    async with _connect_db() as db:
        await db.execute(
            "UPDATE chat_messages SET status = ? WHERE id = ?",
            (status, msg_id),
        )
        await db.commit()


async def update_chat_message_content(msg_id: int, content: str) -> None:
    """Update the content of a chat message."""
    async with _connect_db() as db:
        await db.execute(
            "UPDATE chat_messages SET content = ? WHERE id = ?",
            (content, msg_id),
        )
        await db.commit()


async def update_chat_message_ai_fields(
    msg_id: int,
    *,
    ai_provider: str = "",
    ai_model: str = "",
    session_id: str = "",
) -> None:
    """Update AI-related fields on a chat message."""
    async with _connect_db() as db:
        await db.execute(
            "UPDATE chat_messages SET ai_provider = ?, ai_model = ?, session_id = ? WHERE id = ?",
            (ai_provider, ai_model, session_id, msg_id),
        )
        await db.commit()


# ─── Reports queries ────────────────────────────────────────────────────────


# Shared subquery: extracts job_name and build_number from result_json.
_RESULT_DATA_SUBQUERY = """
    SELECT job_id,
           json_extract(result_json, '$.job_name') AS job_name,
           json_extract(result_json, '$.build_number') AS build_number
    FROM results WHERE result_json IS NOT NULL
"""

# Regex for detecting GitHub Issue / Jira Bug links in comments.
_ISSUE_LINK_PATTERN = re.compile(
    r"(GitHub Issue|Jira Bug)(?:\s*\[[^\]]*\])?:\s*\[([^\]]+)\]\(([^)]+)\)"
)


def _build_date_filter(
    column: str,
    date_from: str,
    date_to: str,
    conditions: list[str],
    params: list,
) -> None:
    """Append date-range conditions and params in-place.

    Shared by all reports queries to avoid duplicating date filtering logic.
    """
    if date_from:
        conditions.append(f"date({column}) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append(f"date({column}) <= ?")
        params.append(date_to)


def _build_metadata_join(
    team: list[str] | None,
    tier: list[str] | None,
    version: list[str] | None,
    job_name_col: str,
    conditions: list[str],
    params: list,
) -> str:
    """Return a JOIN clause for job_metadata filtering.

    Appends conditions and params in-place. Returns the JOIN SQL fragment
    or an empty string when no metadata filters are active.
    Shared by all reports queries.
    """
    if not (team or tier or version):
        return ""
    join_sql = f" JOIN job_metadata jm ON jm.job_name = {job_name_col}"
    if team:
        placeholders = ", ".join("?" for _ in team)
        conditions.append(f"jm.team IN ({placeholders})")
        params.extend(team)
    if tier:
        placeholders = ", ".join("?" for _ in tier)
        conditions.append(f"jm.tier IN ({placeholders})")
        params.extend(tier)
    if version:
        placeholders = ", ".join("?" for _ in version)
        conditions.append(f"jm.version IN ({placeholders})")
        params.extend(version)
    return join_sql


def _build_tags_filter(
    tags: list[str] | None,
    job_name_col: str,
    conditions: list[str],
    params: list,
) -> None:
    """Append tag-filtering conditions in-place.

    Tags are stored in ``job_metadata.labels`` as a JSON array.
    Uses ``json_each`` to match any of the requested tags (OR semantics).
    """
    if not tags:
        return
    placeholders = ", ".join("?" for _ in tags)
    conditions.append(
        f"""{job_name_col} IN (
            SELECT jm_tags.job_name FROM job_metadata jm_tags, json_each(jm_tags.labels) jt
            WHERE jt.value IN ({placeholders})
        )"""
    )
    params.extend(tags)


def _build_exclude_tags_filter(
    exclude_tags: list[str] | None,
    job_name_col: str,
    conditions: list[str],
    params: list,
) -> None:
    """Append tag-exclusion conditions in-place.

    Excludes jobs that have ANY of the specified tags (NOT IN semantics).
    """
    if not exclude_tags:
        return
    placeholders = ", ".join("?" for _ in exclude_tags)
    conditions.append(
        f"""{job_name_col} NOT IN (
            SELECT jm_tags.job_name FROM job_metadata jm_tags, json_each(jm_tags.labels) jt
            WHERE jt.value IN ({placeholders})
        )"""
    )
    params.extend(exclude_tags)


async def get_report_totals(
    *,
    team: list[str] | None = None,
    tier: list[str] | None = None,
    version: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    status: list[str] | None = None,
    tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    review_status: str = "",
    limit: int = 0,
    offset: int = 0,
) -> dict:
    """Aggregate totals for the reports page.

    Returns total jobs, total failures, total reviewed,
    plus a paginated per-job detail list.
    """
    conditions: list[str] = []
    params: list = []

    _build_date_filter("r.created_at", date_from, date_to, conditions, params)
    meta_join = _build_metadata_join(
        team, tier, version, "r_data.job_name", conditions, params
    )
    _build_tags_filter(tags, "r_data.job_name", conditions, params)
    _build_exclude_tags_filter(exclude_tags, "r_data.job_name", conditions, params)

    effective_status = status if status else ["completed"]
    status_placeholders = ", ".join("?" for _ in effective_status)
    # Status params feed both the subquery and outer WHERE
    status_params = list(effective_status) + list(effective_status)

    where = (" AND " + " AND ".join(conditions)) if conditions else ""

    async with _connect_db() as db:
        sql = f"""
            SELECT
                r.job_id,
                r_data.job_name,
                r_data.build_number,
                r.created_at,
                COALESCE(fc.failure_count, 0) AS failure_count,
                COALESCE(rv.reviewed_count, 0) AS reviewed_count
            FROM results r
            JOIN ({_RESULT_DATA_SUBQUERY} AND status IN ({status_placeholders})
            ) r_data ON r_data.job_id = r.job_id
            {meta_join}
            LEFT JOIN (
                SELECT job_id, COUNT(*) AS failure_count
                FROM failure_history GROUP BY job_id
            ) fc ON fc.job_id = r.job_id
            LEFT JOIN (
                SELECT job_id, COUNT(*) AS reviewed_count
                FROM failure_reviews WHERE reviewed = 1 GROUP BY job_id
            ) rv ON rv.job_id = r.job_id
            WHERE r.status IN ({status_placeholders}){where}
            ORDER BY r.created_at DESC
        """

        if review_status == "reviewed":
            sql = f"SELECT * FROM ({sql}) sub WHERE sub.reviewed_count > 0 ORDER BY sub.created_at DESC, sub.job_id DESC"
        elif review_status == "not_reviewed":
            sql = f"SELECT * FROM ({sql}) sub WHERE sub.reviewed_count = 0 ORDER BY sub.created_at DESC, sub.job_id DESC"

        cursor = await db.execute(sql, status_params + params)
        rows = await cursor.fetchall()

    total_jobs = len(rows)
    total_failures = 0
    total_reviewed = 0
    details = []
    for row in rows:
        fc = row["failure_count"]
        rc = row["reviewed_count"]
        total_failures += fc
        total_reviewed += rc
        details.append(
            {
                "job_id": row["job_id"],
                "job_name": row["job_name"] or row["job_id"],
                "build_number": row["build_number"],
                "failure_count": fc,
                "reviewed_count": rc,
                "created_at": row["created_at"],
            }
        )

    # Apply pagination to the detail list (totals are always full)
    paginated = details[offset : offset + limit] if limit > 0 else details

    return {
        "total_jobs": total_jobs,
        "total_failures": total_failures,
        "total_reviewed": total_reviewed,
        "total_details": len(details),
        "jobs": paginated,
    }


async def _count_reviewed_tests(
    db: aiosqlite.Connection,
    *,
    team: list[str] | None = None,
    tier: list[str] | None = None,
    version: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    status: list[str] | None = None,
    tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    review_status: str = "",
) -> int:
    """Count reviewed tests with the given filters."""
    conditions: list[str] = []
    params: list = []
    _build_date_filter("fr.updated_at", date_from, date_to, conditions, params)
    needs_rdata = bool(team or tier or version or tags or exclude_tags)
    rdata_join = (
        f"JOIN ({_RESULT_DATA_SUBQUERY}) fr_rdata ON fr_rdata.job_id = fr.job_id"
        if needs_rdata
        else ""
    )
    meta_join = _build_metadata_join(
        team, tier, version, "fr_rdata.job_name", conditions, params
    )
    _build_tags_filter(tags, "fr_rdata.job_name", conditions, params)
    _build_exclude_tags_filter(exclude_tags, "fr_rdata.job_name", conditions, params)
    status_join = ""
    if status:
        status_join = " JOIN results r_rstatus ON r_rstatus.job_id = fr.job_id"
        placeholders = ", ".join("?" for _ in status)
        conditions.append(f"r_rstatus.status IN ({placeholders})")
        params.extend(status)
    if review_status == "reviewed":
        conditions.append(
            "EXISTS (SELECT 1 FROM failure_reviews fr_rs"
            " WHERE fr_rs.job_id = fr.job_id AND fr_rs.reviewed = 1)"
        )
    elif review_status == "not_reviewed":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM failure_reviews fr_rs"
            " WHERE fr_rs.job_id = fr.job_id AND fr_rs.reviewed = 1)"
        )
    where = (" AND " + " AND ".join(conditions)) if conditions else ""
    cursor = await db.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM failure_reviews fr
        {rdata_join}
        {meta_join}
        {status_join}
        WHERE fr.reviewed = 1{where}
        """,
        params,
    )
    return (await cursor.fetchone())["cnt"]


async def get_report_classification_overrides(
    *,
    team: list[str] | None = None,
    tier: list[str] | None = None,
    version: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    status: list[str] | None = None,
    tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    review_status: str = "",
    limit: int = 0,
    offset: int = 0,
) -> dict:
    """Count of user classification overrides grouped by from->to.

    Only includes overrides where created_by is non-empty (user-initiated)
    and the test also appears in failure_reviews (reviewed = 1).
    """
    conditions: list[str] = ["tc.created_by != ''"]
    params: list = []

    _build_date_filter("tc.created_at", date_from, date_to, conditions, params)
    meta_join = _build_metadata_join(
        team, tier, version, "tc.job_name", conditions, params
    )
    _build_tags_filter(tags, "tc.job_name", conditions, params)
    _build_exclude_tags_filter(exclude_tags, "tc.job_name", conditions, params)

    status_join = ""
    if status:
        status_join = " JOIN results r_status ON r_status.job_id = tc.job_id"
        placeholders = ", ".join("?" for _ in status)
        conditions.append(f"r_status.status IN ({placeholders})")
        params.extend(status)

    if review_status == "reviewed":
        conditions.append(
            "EXISTS (SELECT 1 FROM failure_reviews fr_rs WHERE fr_rs.job_id = tc.job_id AND fr_rs.reviewed = 1)"
        )
    elif review_status == "not_reviewed":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM failure_reviews fr_rs WHERE fr_rs.job_id = tc.job_id AND fr_rs.reviewed = 1)"
        )

    where = " AND ".join(conditions)

    async with _connect_db() as db:
        # Classification-axis overrides: original_classification stored at
        # override time differs from the user's chosen classification.
        cls_sql = f"""
            SELECT
                tc.id,
                tc.test_name,
                tc.job_name,
                tc.job_id,
                tc.child_build_number,
                tc.classification,
                tc.pattern AS tc_pattern,
                tc.created_by,
                tc.created_at,
                tc.original_classification,
                tc.original_pattern,
                fh.build_number,
                'classification' AS override_axis
            FROM test_classifications tc
            {meta_join}
            {status_join}
            JOIN failure_history fh
                ON fh.job_id = tc.job_id
                AND fh.test_name = tc.test_name
                AND fh.child_job_name = tc.job_name
                AND fh.child_build_number = tc.child_build_number
            WHERE {where}
              AND tc.visible = 1
              AND tc.classification != ''
              AND tc.original_classification != ''
              AND tc.original_classification != tc.classification
              AND EXISTS (
                  SELECT 1 FROM failure_reviews fr
                  WHERE fr.job_id = tc.job_id AND fr.test_name = tc.test_name
                    AND fr.reviewed = 1
              )
            ORDER BY tc.created_at DESC
        """
        cursor = await db.execute(cls_sql, params)
        cls_rows = await cursor.fetchall()

        # Pattern-axis overrides: original_pattern stored at override time
        # differs from the user's chosen pattern.
        pat_sql = f"""
            SELECT
                tc.id,
                tc.test_name,
                tc.job_name,
                tc.job_id,
                tc.child_build_number,
                tc.classification,
                tc.pattern AS tc_pattern,
                tc.created_by,
                tc.created_at,
                tc.original_classification,
                tc.original_pattern,
                fh.build_number,
                'pattern' AS override_axis
            FROM test_classifications tc
            {meta_join}
            {status_join}
            JOIN failure_history fh
                ON fh.job_id = tc.job_id
                AND fh.test_name = tc.test_name
                AND fh.child_job_name = tc.job_name
                AND fh.child_build_number = tc.child_build_number
            WHERE {where}
              AND tc.visible = 1
              AND tc.pattern != ''
              AND tc.original_pattern != ''
              AND tc.original_pattern != tc.pattern
              AND EXISTS (
                  SELECT 1 FROM failure_reviews fr
                  WHERE fr.job_id = tc.job_id AND fr.test_name = tc.test_name
                    AND fr.reviewed = 1
              )
            ORDER BY tc.created_at DESC
        """
        cursor = await db.execute(pat_sql, params)
        pat_rows = await cursor.fetchall()

        all_rows = list(cls_rows) + list(pat_rows)

        # Keep only the latest override per (job_id, test_name, axis)
        seen_keys: set[tuple[str, str, str]] = set()
        latest_rows = []
        for row in all_rows:
            dedup_key = (row["job_id"], row["test_name"], row["override_axis"])
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                latest_rows.append(row)
        rows = latest_rows

        # Count total reviewed tests (same filters minus override-specific ones)
        total_reviewed = await _count_reviewed_tests(
            db,
            team=team,
            tier=tier,
            version=version,
            date_from=date_from,
            date_to=date_to,
            status=status,
            tags=tags,
            exclude_tags=exclude_tags,
            review_status=review_status,
        )

    # Group by from->to
    groups: dict[str, dict] = {}
    details: list[dict] = []
    for row in rows:
        axis = row["override_axis"]
        if axis == "pattern":
            orig = row["original_pattern"] or "UNKNOWN"
            override = row["tc_pattern"]
        else:
            orig = row["original_classification"] or "UNKNOWN"
            override = row["classification"]
        key = f"{orig} → {override}"
        if key not in groups:
            groups[key] = {"from": orig, "to": override, "count": 0}
        groups[key]["count"] += 1
        details.append(
            {
                "test_name": row["test_name"],
                "job_name": row["job_name"],
                "job_id": row["job_id"],
                "build_number": row["build_number"],
                "from_classification": orig,
                "to_classification": override,
                "override_axis": axis,
                "overridden_by": row["created_by"],
                "overridden_at": row["created_at"],
            }
        )

    # Apply pagination to the detail list (groups/total are always full)
    paginated = details[offset : offset + limit] if limit > 0 else details

    unique_overridden = len({(d["job_id"], d["test_name"]) for d in details})
    ai_correct = max(total_reviewed - unique_overridden, 0)
    ai_accuracy_pct = (
        round((ai_correct / total_reviewed) * 100, 1) if total_reviewed > 0 else 0
    )

    return {
        "total": len(details),
        "total_reviewed": total_reviewed,
        "ai_correct": ai_correct,
        "ai_accuracy_pct": ai_accuracy_pct,
        "groups": list(groups.values()),
        "details": paginated,
    }


async def get_report_issues_created(
    *,
    team: list[str] | None = None,
    tier: list[str] | None = None,
    version: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    status: list[str] | None = None,
    tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    review_status: str = "",
    limit: int = 0,
    offset: int = 0,
) -> dict:
    """Find GitHub/Jira issues created from comments.

    Matches comments containing 'GitHub Issue' or 'Jira Bug' patterns
    and parses the markdown link for URL and title.
    Only http/https URLs are included (prevents javascript: XSS).
    Returns separate github_total and jira_total counts.
    """
    conditions: list[str] = [
        "(c.comment LIKE '%GitHub Issue%' OR c.comment LIKE '%Jira Bug%')"
    ]
    params: list = []

    _build_date_filter("c.created_at", date_from, date_to, conditions, params)
    meta_join = _build_metadata_join(
        team, tier, version, "r_data.job_name", conditions, params
    )
    _build_tags_filter(tags, "r_data.job_name", conditions, params)
    _build_exclude_tags_filter(exclude_tags, "r_data.job_name", conditions, params)

    status_join = ""
    if status:
        status_join = " JOIN results r_status ON r_status.job_id = c.job_id"
        placeholders = ", ".join("?" for _ in status)
        conditions.append(f"r_status.status IN ({placeholders})")
        params.extend(status)

    if review_status == "reviewed":
        conditions.append(
            "EXISTS (SELECT 1 FROM failure_reviews fr_rs WHERE fr_rs.job_id = c.job_id AND fr_rs.reviewed = 1)"
        )
    elif review_status == "not_reviewed":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM failure_reviews fr_rs WHERE fr_rs.job_id = c.job_id AND fr_rs.reviewed = 1)"
        )

    # Use JOIN when metadata/tag filters narrow results, LEFT JOIN otherwise
    join_type = (
        "JOIN" if (team or tier or version or tags or exclude_tags) else "LEFT JOIN"
    )
    where = " AND ".join(conditions)

    async with _connect_db() as db:
        sql = f"""
            SELECT c.id, c.job_id, c.test_name, c.comment,
                   c.username, c.created_at,
                   r_data.job_name, r_data.build_number
            FROM comments c
            {join_type} ({_RESULT_DATA_SUBQUERY}
            ) r_data ON r_data.job_id = c.job_id
            {meta_join}
            {status_join}
            WHERE {where}
            ORDER BY c.created_at DESC
        """
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

    issues: list[dict] = []
    github_total = 0
    jira_total = 0
    for row in rows:
        match = _ISSUE_LINK_PATTERN.search(row["comment"])
        if not match:
            continue
        issue_type = match.group(1)
        title = match.group(2)
        url = match.group(3)
        # Only allow safe URL schemes — prevents javascript: XSS
        if not url.startswith(("http://", "https://")):
            continue
        if issue_type == "GitHub Issue":
            github_total += 1
        else:
            jira_total += 1
        issues.append(
            {
                "issue_type": issue_type,
                "title": title,
                "url": url,
                "test_name": row["test_name"],
                "job_name": row["job_name"] or row["job_id"],
                "job_id": row["job_id"],
                "build_number": row["build_number"],
                "created_by": row["username"],
                "created_at": row["created_at"],
            }
        )

    # Apply pagination to the issues list (totals are always full)
    paginated = issues[offset : offset + limit] if limit > 0 else issues

    return {
        "total": len(issues),
        "github_total": github_total,
        "jira_total": jira_total,
        "issues": paginated,
    }
