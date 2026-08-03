"""Tests for auto-review feature (issue #95).

Tests cover:
- Signature normalization (timestamps, UUIDs, pod names, build numbers)
- find_matching_previous_analysis() storage query
- Auto-review logic in the analysis flow
- rootcoz-* username prefix blocked at registration
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from starlette.testclient import TestClient

from rootcoz import storage
from rootcoz.config import Settings, get_settings
from rootcoz.engine.core import (
    get_failure_signature,
    normalize_for_signature,
)
from rootcoz.models import FailedTest
from rootcoz.storage import AI_SYSTEM_USERNAME
from tests.conftest import build_test_env


async def _insert_human_review(
    db: aiosqlite.Connection,
    job_id: str,
    test_name: str,
    child_job_name: str = "",
    child_build_number: int = 0,
    username: str = "human-reviewer",
) -> None:
    """Insert a human review record into failure_reviews."""
    await db.execute(
        "INSERT INTO failure_reviews "
        "(job_id, test_name, child_job_name, child_build_number, reviewed, username) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (job_id, test_name, child_job_name, child_build_number, username),
    )


# ---------------------------------------------------------------------------
# Signature normalization
# ---------------------------------------------------------------------------
class TestNormalizeForSignature:
    """Tests for normalize_for_signature()."""

    def test_strips_iso_timestamp(self):
        text = "Error at 2026-05-31T06:50:48.123Z in module"
        result = normalize_for_signature(text)
        assert "2026-05-31T06:50:48.123Z" not in result
        assert "<TIMESTAMP>" in result

    def test_strips_iso_timestamp_with_offset(self):
        text = "Error at 2026-05-31T06:50:48+05:30 in module"
        result = normalize_for_signature(text)
        assert "2026-05-31T06:50:48+05:30" not in result
        assert "<TIMESTAMP>" in result

    def test_strips_datetime_with_space(self):
        text = "Occurred on 2026-05-31 06:50:48.123 UTC"
        result = normalize_for_signature(text)
        assert "2026-05-31 06:50:48" not in result
        assert "<TIMESTAMP>" in result

    def test_strips_month_day_year_date(self):
        text = "Failed on May 31 2026"
        result = normalize_for_signature(text)
        assert "May 31 2026" not in result
        assert "<DATE>" in result

    def test_strips_day_month_year_date(self):
        text = "Failed on 31 May 2026"
        result = normalize_for_signature(text)
        assert "31 May 2026" not in result
        assert "<DATE>" in result

    def test_strips_uuid(self):
        text = "Pod fd18d967-0f31-4c8d-ab74-b8cf463aa04f crashed"
        result = normalize_for_signature(text)
        assert "fd18d967-0f31-4c8d-ab74-b8cf463aa04f" not in result
        assert "<UUID>" in result

    def test_strips_pod_suffix(self):
        text = "virt-launcher-7f8b9c failed"
        result = normalize_for_signature(text)
        assert "-7f8b9c" not in result
        assert "-<SUFFIX>" in result

    def test_strips_build_number(self):
        text = "Build #1234 failed"
        result = normalize_for_signature(text)
        assert "#1234" not in result
        assert "#<BUILD>" in result

    def test_strips_build_ref(self):
        text = "See build/456 and run/789"
        result = normalize_for_signature(text)
        assert "build/456" not in result
        assert "run/789" not in result

    def test_strips_standalone_date(self):
        text = "Date 2026-05-31 was the day"
        result = normalize_for_signature(text)
        assert "2026-05-31" not in result

    def test_preserves_non_matching_text(self):
        text = "NullPointerException at com.example.MyClass.method(MyClass.java:42)"
        result = normalize_for_signature(text)
        assert result == text

    def test_multiple_replacements(self):
        text = (
            "Error at 2026-01-15T12:00:00Z on pod my-pod-abc123 "
            "uuid=fd18d967-0f31-4c8d-ab74-b8cf463aa04f build #42"
        )
        result = normalize_for_signature(text)
        assert "2026-01-15T12:00:00Z" not in result
        assert "fd18d967-0f31-4c8d-ab74-b8cf463aa04f" not in result
        assert "#42" not in result


class TestGetFailureSignature:
    """Tests for get_failure_signature() with normalization."""

    def test_identical_errors_different_timestamps_same_signature(self):
        """Same error with different timestamps should produce identical signatures."""
        f1 = FailedTest(
            test_name="test_a",
            error_message="Error at 2026-05-31T06:50:48Z in pod",
            stack_trace="Exception at 2026-05-31T06:50:48Z\n  at main()",
        )
        f2 = FailedTest(
            test_name="test_a",
            error_message="Error at 2026-06-15T12:00:00Z in pod",
            stack_trace="Exception at 2026-06-15T12:00:00Z\n  at main()",
        )
        assert get_failure_signature(f1) == get_failure_signature(f2)

    def test_identical_errors_different_uuids_same_signature(self):
        """Same error with different UUIDs should produce identical signatures."""
        f1 = FailedTest(
            test_name="test_a",
            error_message="Pod fd18d967-0f31-4c8d-ab74-b8cf463aa04f crashed",
            stack_trace="trace",
        )
        f2 = FailedTest(
            test_name="test_a",
            error_message="Pod 11111111-2222-3333-4444-555555555555 crashed",
            stack_trace="trace",
        )
        assert get_failure_signature(f1) == get_failure_signature(f2)

    def test_different_errors_different_signatures(self):
        """Different error messages should produce different signatures."""
        f1 = FailedTest(
            test_name="test_a",
            error_message="NullPointerException",
            stack_trace="at main()",
        )
        f2 = FailedTest(
            test_name="test_a",
            error_message="TimeoutException",
            stack_trace="at main()",
        )
        assert get_failure_signature(f1) != get_failure_signature(f2)

    def test_signature_is_sha256(self):
        f = FailedTest(
            test_name="test_a",
            error_message="error",
            stack_trace="trace",
        )
        sig = get_failure_signature(f)
        assert len(sig) == 64  # SHA-256 hex digest length
        # Should be valid hex
        int(sig, 16)


# ---------------------------------------------------------------------------
# find_matching_previous_analysis
# ---------------------------------------------------------------------------
class TestFindMatchingPreviousAnalysis:
    """Tests for find_matching_previous_analysis() in storage."""

    @pytest.fixture
    async def setup_test_db(self, temp_db_path: Path):
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            yield temp_db_path

    async def test_finds_previous_matching_test(self, setup_test_db):
        """Should find a previous human-reviewed analysis."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "prev-job",
                        "my-job",
                        100,
                        "test_module.test_something",
                        "error msg",
                        "sig123",
                        "PRODUCT BUG",
                        "KNOWN_BUG",
                    ),
                )
                await _insert_human_review(db, "prev-job", "test_module.test_something")
                await db.commit()

            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_module.test_something",
                current_job_id="current-job",
            )
            assert result is not None
            assert result["job_id"] == "prev-job"
            assert result["build_number"] == 100
            assert result["error_signature"] == "sig123"

    async def test_skips_ai_only_reviewed(self, setup_test_db):
        """Should NOT match a failure reviewed only by rootcoz-ai."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "my-job", 100, "test_a", "", "sig", "", ""),
                )
                await _insert_human_review(
                    db, "prev-job", "test_a", username=AI_SYSTEM_USERNAME
                )
                await db.commit()

            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
            )
            assert result is None

    async def test_skips_unreviewed(self, setup_test_db):
        """Should NOT match a failure that was never reviewed."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "my-job", 100, "test_a", "", "sig", "", ""),
                )
                # No review record at all
                await db.commit()

            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
            )
            assert result is None

    async def test_skips_blank_username_review(self, setup_test_db):
        """Should NOT match when the only review has a blank username (legacy migration)."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "my-job", 100, "test_a", "", "sig", "", ""),
                )
                await db.execute(
                    "INSERT INTO failure_reviews "
                    "(job_id, test_name, child_job_name, child_build_number, reviewed, username) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    ("prev-job", "test_a", "", 0, ""),
                )
                await db.commit()

            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
            )
            assert result is None

    async def test_excludes_current_job_id(self, setup_test_db):
        """Should not return the current job's own history."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("same-job", "my-job", 100, "test_a", "", "sig", "", ""),
                )
                await _insert_human_review(db, "same-job", "test_a")
                await db.commit()

            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="same-job",
            )
            assert result is None

    async def test_returns_none_for_different_job_name(self, setup_test_db):
        """Should not match across different job names."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "other-job", 100, "test_a", "", "sig", "", ""),
                )
                await _insert_human_review(db, "prev-job", "test_a")
                await db.commit()

            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
            )
            assert result is None

    async def test_returns_none_when_no_history(self, setup_test_db):
        """Should return None when no previous analysis exists."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
            )
            assert result is None

    async def test_returns_most_recent(self, setup_test_db):
        """Should return the most recent human-reviewed previous analysis."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern, analyzed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "old-job",
                        "my-job",
                        99,
                        "test_a",
                        "",
                        "old-sig",
                        "",
                        "",
                        "2026-01-01 00:00:00",
                    ),
                )
                await _insert_human_review(db, "old-job", "test_a")
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern, analyzed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "new-job",
                        "my-job",
                        101,
                        "test_a",
                        "",
                        "new-sig",
                        "",
                        "",
                        "2026-06-01 00:00:00",
                    ),
                )
                await _insert_human_review(db, "new-job", "test_a")
                await db.commit()

            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
            )
            assert result is not None
            assert result["job_id"] == "new-job"
            assert result["error_signature"] == "new-sig"

    async def test_scopes_by_child_job_name(self, setup_test_db):
        """Should only match within the same child_job_name context."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                # Same test_name in two different child jobs
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern, "
                    "child_job_name, child_build_number) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "prev-job",
                        "my-job",
                        100,
                        "test_a",
                        "",
                        "sig-child-A",
                        "",
                        "",
                        "child-job-A",
                        1,
                    ),
                )
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern, "
                    "child_job_name, child_build_number) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "prev-job",
                        "my-job",
                        100,
                        "test_a",
                        "",
                        "sig-child-B",
                        "",
                        "",
                        "child-job-B",
                        2,
                    ),
                )
                await _insert_human_review(
                    db,
                    "prev-job",
                    "test_a",
                    child_job_name="child-job-A",
                    child_build_number=1,
                )
                await _insert_human_review(
                    db,
                    "prev-job",
                    "test_a",
                    child_job_name="child-job-B",
                    child_build_number=2,
                )
                await db.commit()

            # Query for child-job-A should return sig-child-A
            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
                child_job_name="child-job-A",
            )
            assert result is not None
            assert result["error_signature"] == "sig-child-A"

            # Query for child-job-B should return sig-child-B
            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
                child_job_name="child-job-B",
            )
            assert result is not None
            assert result["error_signature"] == "sig-child-B"

    async def test_wildcard_child_build_number_matches(self, setup_test_db):
        """A review with child_build_number=0 (wildcard) should match any child build."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                # failure_history has a specific child build number
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern, "
                    "child_job_name, child_build_number) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "prev-job",
                        "my-job",
                        100,
                        "test_a",
                        "",
                        "sig-child",
                        "",
                        "",
                        "child-job-A",
                        5,
                    ),
                )
                # Human review stored with child_build_number=0 (wildcard)
                await _insert_human_review(
                    db,
                    "prev-job",
                    "test_a",
                    child_job_name="child-job-A",
                    child_build_number=0,
                )
                await db.commit()

            # Wildcard review (build=0) should match failure_history (build=5)
            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
                child_job_name="child-job-A",
            )
            assert result is not None
            assert result["error_signature"] == "sig-child"

    async def test_top_level_does_not_match_child(self, setup_test_db):
        """Top-level lookup (empty child_job_name) should not match child rows."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern, "
                    "child_job_name, child_build_number) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "prev-job",
                        "my-job",
                        100,
                        "test_a",
                        "",
                        "child-sig",
                        "",
                        "",
                        "child-job-A",
                        1,
                    ),
                )
                await db.commit()

            # Top-level query should not find child rows
            result = await storage.find_matching_previous_analysis(
                job_name="my-job",
                test_name="test_a",
                current_job_id="current-job",
            )
            assert result is None


# ---------------------------------------------------------------------------
# Username reservation
# ---------------------------------------------------------------------------
class TestReservedUsername:
    """Test that rootcoz-* usernames are blocked at registration."""

    @pytest.fixture
    def client(self, temp_db_path):
        with patch.dict(
            os.environ,
            {
                "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
                "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
                "SECURE_COOKIES": "false",
                "DB_PATH": str(temp_db_path),
                "REQUIRE_APPROVAL": "false",
            },
        ):
            get_settings.cache_clear()
            with patch.object(storage, "DB_PATH", temp_db_path):
                from rootcoz.main import app

                with TestClient(app) as c:
                    yield c
            get_settings.cache_clear()

    def test_blocks_rootcoz_prefix(self, client):
        """Should reject username starting with 'rootcoz'."""
        resp = client.post("/api/auth/register", json={"username": "rootcoz-ai"})
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()

    def test_blocks_rootcoz_exact(self, client):
        """Should reject 'rootcoz' exact username."""
        resp = client.post("/api/auth/register", json={"username": "rootcoz"})
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()

    def test_blocks_rootcoz_uppercase(self, client):
        """Should reject case-insensitive rootcoz prefix."""
        resp = client.post("/api/auth/register", json={"username": "RootCoz-Bot"})
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()

    def test_allows_normal_username(self, client):
        """Should allow normal usernames."""
        resp = client.post("/api/auth/register", json={"username": "normaluser"})
        assert resp.status_code == 200

    def test_register_reserved_has_no_store_header(self, client):
        """Reserved username rejection should include Cache-Control: no-store."""
        resp = client.post("/api/auth/register", json={"username": "rootcoz-ai"})
        assert resp.status_code == 400
        assert resp.headers.get("cache-control") == "no-store"

    def _admin_login(self, client):
        """Login as admin and return session cookies."""
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "api_key": "test-admin-key-16chars",  # pragma: allowlist secret
            },
        )
        assert resp.status_code == 200
        return resp.cookies

    def test_admin_blocks_rootcoz_prefix(self, client):
        """Admin create user should also reject rootcoz-* usernames."""
        cookies = self._admin_login(client)
        resp = client.post(
            "/api/admin/users/create",
            json={"username": "rootcoz-ai", "role": "reviewer"},
            cookies=cookies,
        )
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()

    def test_admin_blocks_rootcoz_exact(self, client):
        """Admin create user should reject 'rootcoz' exact username."""
        cookies = self._admin_login(client)
        resp = client.post(
            "/api/admin/users/create",
            json={"username": "rootcoz", "role": "operator"},
            cookies=cookies,
        )
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Auto-review logic
# ---------------------------------------------------------------------------
class TestAutoReviewMatchingFailures:
    """Tests for _auto_review_matching_failures()."""

    @pytest.fixture
    async def setup_test_db(self, temp_db_path: Path):
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            yield temp_db_path

    async def test_auto_reviews_matching_failure(self, setup_test_db):
        """Should auto-review a failure when previous analysis has matching signature."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert previous failure history with human review
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern, analyzed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "prev-job",
                        "my-job",
                        100,
                        "test_module.TestClass.test_method",
                        "Connection timeout",
                        "matching-sig-hash",
                        "INFRASTRUCTURE",
                        "PERSISTENT",
                        "2026-01-01 00:00:00",
                    ),
                )
                await _insert_human_review(
                    db, "prev-job", "test_module.TestClass.test_method"
                )
                await db.commit()

            # Current result data with matching signature
            result_data = {
                "job_name": "my-job",
                "build_number": 101,
                "failures": [
                    {
                        "test_name": "test_module.TestClass.test_method",
                        "error": "Connection timeout",
                        "error_signature": "matching-sig-hash",
                        "analysis": {"classification": "INFRASTRUCTURE"},
                    }
                ],
            }

            settings = Settings(**{k.lower(): v for k, v in build_test_env().items()})

            from rootcoz.main import _auto_review_matching_failures

            await _auto_review_matching_failures(
                "current-job", "my-job", 101, result_data, settings
            )

            # Verify review was set
            reviews = await storage.get_reviews_for_job("current-job")
            assert "test_module.TestClass.test_method" in reviews
            review = reviews["test_module.TestClass.test_method"]
            assert review["reviewed"] is True
            assert review["username"] == AI_SYSTEM_USERNAME

            # Verify comment was added
            comments = await storage.get_comments_for_job("current-job")
            assert len(comments) == 1
            assert "Auto-reviewed" in comments[0]["comment"]
            assert "prev-job" in comments[0]["comment"]
            assert comments[0]["username"] == AI_SYSTEM_USERNAME

    async def test_skips_when_signature_differs(self, setup_test_db):
        """Should NOT auto-review when signatures don't match."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "prev-job",
                        "my-job",
                        100,
                        "test_a",
                        "",
                        "old-sig",
                        "",
                        "",
                    ),
                )
                await _insert_human_review(db, "prev-job", "test_a")
                await db.commit()

            result_data = {
                "job_name": "my-job",
                "build_number": 101,
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "Different error",
                        "error_signature": "new-different-sig",
                        "analysis": {"classification": "CODE ISSUE"},
                    }
                ],
            }

            settings = Settings(**{k.lower(): v for k, v in build_test_env().items()})

            from rootcoz.main import _auto_review_matching_failures

            await _auto_review_matching_failures(
                "current-job", "my-job", 101, result_data, settings
            )

            reviews = await storage.get_reviews_for_job("current-job")
            assert len(reviews) == 0

    async def test_skips_when_no_previous_analysis(self, setup_test_db):
        """Should NOT auto-review when no previous analysis exists."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "my-job",
                "build_number": 101,
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "error",
                        "error_signature": "sig",
                        "analysis": {"classification": "CODE ISSUE"},
                    }
                ],
            }

            settings = Settings(**{k.lower(): v for k, v in build_test_env().items()})

            from rootcoz.main import _auto_review_matching_failures

            await _auto_review_matching_failures(
                "current-job", "my-job", 101, result_data, settings
            )

            reviews = await storage.get_reviews_for_job("current-job")
            assert len(reviews) == 0

    async def test_auto_push_reportportal_when_all_reviewed(self, setup_test_db):
        """Should auto-push to Report Portal when all failures are reviewed."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "my-job", 100, "test_a", "", "sig-a", "", ""),
                )
                await _insert_human_review(db, "prev-job", "test_a")
                await db.commit()

            result_data = {
                "job_name": "my-job",
                "build_number": 101,
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "error_signature": "sig-a",
                        "analysis": {"classification": "INFRASTRUCTURE"},
                    }
                ],
            }

            settings = Settings(
                **{
                    k.lower(): v
                    for k, v in build_test_env(
                        ENABLE_REPORTPORTAL="true",
                        REPORTPORTAL_URL="https://rp.example.com",
                        REPORTPORTAL_API_TOKEN="test-token",
                        REPORTPORTAL_PROJECT="test-project",
                    ).items()
                }
            )

            from rootcoz.main import _auto_review_matching_failures

            with patch(
                "rootcoz.main._execute_rp_push", new_callable=AsyncMock
            ) as mock_push:
                await _auto_review_matching_failures(
                    "current-job", "my-job", 101, result_data, settings
                )

                mock_push.assert_called_once_with(
                    "current-job", result_data, settings, pushed_by=AI_SYSTEM_USERNAME
                )

    @patch("rootcoz.main.logger")
    async def test_auto_push_log_includes_system_username(
        self, mock_logger, setup_test_db
    ):
        """Auto-push INFO log includes AI_SYSTEM_USERNAME for traceability."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "my-job", 100, "test_a", "", "sig-a", "", ""),
                )
                await _insert_human_review(db, "prev-job", "test_a")
                await db.commit()

            result_data = {
                "job_name": "my-job",
                "build_number": 101,
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "error_signature": "sig-a",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "d",
                        },
                    }
                ],
            }
            settings = Settings(
                ai_provider="claude",
                ai_model="test",
                enable_reportportal=True,
                reportportal_url="http://rp.example.com",
                reportportal_api_token="rp-token",
                reportportal_project="proj",
            )

            from rootcoz.main import _auto_review_matching_failures

            with patch("rootcoz.main._execute_rp_push", new_callable=AsyncMock):
                await _auto_review_matching_failures(
                    "current-job", "my-job", 101, result_data, settings
                )

            info_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args
                and "auto-reviewed" in c.args[0].lower()
                and "Report Portal" in c.args[0]
            ]
            assert info_calls, "Expected INFO log for auto-push trigger"
            log_args = info_calls[0].args
            # Format: "All failures auto-reviewed for job %s, pushing ... (pushed_by=%s)"
            # args[0] = format string, args[1] = job_id, args[2] = AI_SYSTEM_USERNAME
            assert log_args[1] == "current-job", (
                f"Expected job_id 'current-job', got '{log_args[1]}'"
            )
            assert log_args[2] == AI_SYSTEM_USERNAME, (
                f"Expected AI_SYSTEM_USERNAME, got '{log_args[2]}'"
            )

    async def test_no_push_when_reportportal_disabled(self, setup_test_db):
        """Should NOT push to RP when ENABLE_REPORTPORTAL is disabled."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "my-job", 100, "test_a", "", "sig-a", "", ""),
                )
                await _insert_human_review(db, "prev-job", "test_a")
                await db.commit()

            result_data = {
                "job_name": "my-job",
                "build_number": 101,
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "error_signature": "sig-a",
                        "analysis": {"classification": "INFRASTRUCTURE"},
                    }
                ],
            }

            settings = Settings(**{k.lower(): v for k, v in build_test_env().items()})

            from rootcoz.main import _auto_review_matching_failures

            with patch(
                "rootcoz.main._execute_rp_push", new_callable=AsyncMock
            ) as mock_push:
                await _auto_review_matching_failures(
                    "current-job", "my-job", 101, result_data, settings
                )
                mock_push.assert_not_called()

    async def test_skipped_when_enable_auto_review_false(self, setup_test_db):
        """Should NOT auto-review when enable_auto_review is False."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert a previous failure with human review so auto-review *would* trigger
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO failure_history "
                    "(job_id, job_name, build_number, test_name, error_message, "
                    "error_signature, classification, pattern) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("prev-job", "my-job", 100, "test_a", "", "sig-a", "", ""),
                )
                await _insert_human_review(db, "prev-job", "test_a")
                await db.commit()

            result_data = {
                "job_name": "my-job",
                "build_number": 101,
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "error_signature": "sig-a",
                        "analysis": {"classification": "INFRASTRUCTURE"},
                    }
                ],
            }

            env = build_test_env()
            env["ENABLE_AUTO_REVIEW"] = "false"
            settings = Settings(**{k.lower(): v for k, v in env.items()})
            assert settings.enable_auto_review is False

            from rootcoz.main import _auto_review_matching_failures

            await _auto_review_matching_failures(
                "current-job", "my-job", 101, result_data, settings
            )

            # No reviews should be written — auto-review was disabled
            reviews = await storage.get_reviews_for_job("current-job")
            assert len(reviews) == 0
