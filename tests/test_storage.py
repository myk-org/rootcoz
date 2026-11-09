"""Tests for SQLite storage."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest

from rootcoz import storage


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


async def test_bootstrap_admin_flag_rejects_missing_normal_user(
    setup_test_db: Path,
) -> None:
    with patch.object(storage, "DB_PATH", setup_test_db):
        with pytest.raises(ValueError, match="Invalid bootstrap admin"):
            await storage.save_ai_session_source(
                "new-session", "missing", "openai", "server", bootstrap_admin=True
            )
        assert (
            await storage.get_ai_session_source("new-session", "missing", "openai")
            == "unknown"
        )


@pytest.mark.parametrize("recreate", [False, True])
async def test_server_provenance_refuses_deleted_or_reused_account(
    setup_test_db: Path, recreate: bool
) -> None:
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_admin_user("alice")
        original = await storage.get_user_by_username("alice")
        assert original is not None
        await storage.delete_user("alice")
        if recreate:
            await storage.create_admin_user("alice")
        with pytest.raises(LookupError, match="account changed"):
            await storage.save_ai_session_source(
                "new-session", "alice", "openai", "server", original["id"]
            )
        assert (
            await storage.get_ai_session_source("new-session", "alice", "openai")
            == "unknown"
        )


@pytest.mark.parametrize("recreate", [False, True])
async def test_server_creation_refuses_deleted_or_reused_account(
    setup_test_db: Path, recreate: bool
) -> None:
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.create_admin_user("alice")
        deleted = []

        async def create() -> str:
            await storage.delete_user("alice")
            if recreate:
                await storage.create_admin_user("alice")
            return "new-session"

        async def delete(session_id: str) -> None:
            deleted.append(session_id)

        with pytest.raises(ValueError, match="credential changed"):
            await storage.create_ai_session_with_source(
                create, "alice", "openai", "server", delete
            )
        assert deleted == ["new-session"]
        assert (
            await storage.get_ai_session_source("new-session", "alice", "openai")
            == "unknown"
        )


class TestInitDb:
    """Tests for the init_db function."""

    async def test_init_db_creates_table(self, temp_db_path: Path) -> None:
        """Test that init_db creates the results table."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()

            # Verify table exists by trying to query it
            import aiosqlite

            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='results'"
                )
                result = await cursor.fetchone()
                assert result is not None
                assert result[0] == "results"

    async def test_init_db_creates_parent_directory(self, tmp_path: Path) -> None:
        """Test that init_db creates parent directories if needed."""
        nested_path = tmp_path / "nested" / "dir" / "test.db"
        with patch.object(storage, "DB_PATH", nested_path):
            await storage.init_db()
            assert nested_path.parent.exists()

    async def test_init_db_idempotent(self, temp_db_path: Path) -> None:
        """Test that init_db can be called multiple times."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.init_db()  # Should not raise


async def test_graft_estimate_rejects_missing_job(setup_test_db: Path) -> None:
    with patch.object(storage, "DB_PATH", setup_test_db):
        with pytest.raises(LookupError):
            await storage.add_graft_estimated_tokens_saved("deleted", 100)
        await storage.save_result("live", "https://example.test", "pending")
        await storage.add_graft_estimated_tokens_saved("live", 100)
        assert (await storage.get_result("live"))["graft_estimated_tokens_saved"] == 100


class TestSaveResult:
    """Tests for the save_result function."""

    async def test_save_result_new_entry(self, setup_test_db: Path) -> None:
        """Test saving a new result entry."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-123",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="pending",
            )

            result = await storage.get_result("job-123")
            assert result is not None
            assert result["job_id"] == "job-123"
            assert result["status"] == "pending"

    async def test_save_result_with_result_data(self, setup_test_db: Path) -> None:
        """Test saving result with JSON data."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {"summary": "Test complete", "failures": []}
            await storage.save_result(
                job_id="job-456",
                jenkins_url="https://jenkins.example.com/job/test/2/",
                status="completed",
                result=result_data,
            )

            result = await storage.get_result("job-456")
            assert result is not None
            assert result["result"] == result_data

    async def test_save_result_update_existing(self, setup_test_db: Path) -> None:
        """Test updating an existing result."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Save initial result
            await storage.save_result(
                job_id="job-789",
                jenkins_url="https://jenkins.example.com/job/test/3/",
                status="pending",
            )

            # Update result
            await storage.save_result(
                job_id="job-789",
                jenkins_url="https://jenkins.example.com/job/test/3/",
                status="completed",
                result={"summary": "Done"},
            )

            result = await storage.get_result("job-789")
            assert result is not None
            assert result["status"] == "completed"
            assert result["result"]["summary"] == "Done"

    async def test_save_result_none_result(self, setup_test_db: Path) -> None:
        """Test saving result with None result data."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-none",
                jenkins_url="https://jenkins.example.com/job/test/4/",
                status="pending",
                result=None,
            )

            result = await storage.get_result("job-none")
            assert result is not None
            assert result["result"] is None


class TestGetResult:
    """Tests for the get_result function."""

    async def test_get_result_existing(self, setup_test_db: Path) -> None:
        """Test retrieving an existing result."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-get",
                jenkins_url="https://jenkins.example.com/job/test/5/",
                status="completed",
                result={"test": "data"},
            )

            result = await storage.get_result("job-get")
            assert result is not None
            assert result["job_id"] == "job-get"
            assert result["jenkins_url"] == "https://jenkins.example.com/job/test/5/"
            assert result["status"] == "completed"
            assert result["result"]["test"] == "data"
            assert "created_at" in result

    async def test_get_result_not_found(self, setup_test_db: Path) -> None:
        """Test retrieving a non-existent result returns None."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.get_result("non-existent-job")
            assert result is None

    async def test_get_result_parses_json(self, setup_test_db: Path) -> None:
        """Test that get_result properly parses JSON result."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            complex_result = {
                "summary": "Analysis complete",
                "failures": [
                    {"test_name": "test_1", "error": "Error 1"},
                    {"test_name": "test_2", "error": "Error 2"},
                ],
            }
            await storage.save_result(
                job_id="job-json",
                jenkins_url="https://jenkins.example.com/job/test/6/",
                status="completed",
                result=complex_result,
            )

            result = await storage.get_result("job-json")
            assert result is not None
            assert len(result["result"]["failures"]) == 2
            # Failures without 'id' get backfilled UUIDs on read
            for f in result["result"]["failures"]:
                assert "id" in f
            assert result["result"]["failures"][0]["test_name"] == "test_1"
            assert result["result"]["failures"][1]["test_name"] == "test_2"


class TestBackfillFailureUuids:
    """Tests for UUID backward compatibility with legacy data."""

    async def test_legacy_failures_without_ids_get_stable_uuids(
        self, setup_test_db: Path
    ) -> None:
        """Legacy result_json without 'id' fields gets stable UUIDs on read."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            legacy_result = {
                "summary": "Legacy analysis",
                "failures": [
                    {"test_name": "test_a", "error": "Error A"},
                    {"test_name": "test_b", "error": "Error B"},
                ],
            }
            await storage.save_result(
                job_id="legacy-job",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="completed",
                result=legacy_result,
            )

            # First read: backfills and persists UUIDs
            result1 = await storage.get_result("legacy-job")
            assert result1 is not None
            ids_first = [f["id"] for f in result1["result"]["failures"]]
            assert len(ids_first) == 2
            assert all(ids_first)  # non-empty
            assert ids_first[0] != ids_first[1]  # unique

            # Second read: UUIDs are stable (same values)
            result2 = await storage.get_result("legacy-job")
            ids_second = [f["id"] for f in result2["result"]["failures"]]
            assert ids_first == ids_second

    async def test_legacy_child_job_analyses_get_stable_uuids(
        self, setup_test_db: Path
    ) -> None:
        """Legacy child_job_analyses and their failures get backfilled UUIDs."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            legacy_result = {
                "summary": "Pipeline analysis",
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-1",
                        "build_number": 10,
                        "failures": [
                            {"test_name": "child_test_1", "error": "Err"},
                        ],
                        "failed_children": [
                            {
                                "job_name": "grandchild-1",
                                "build_number": 5,
                                "failures": [
                                    {"test_name": "gc_test_1", "error": "GC Err"},
                                ],
                                "failed_children": [],
                            }
                        ],
                    }
                ],
            }
            await storage.save_result(
                job_id="legacy-pipeline",
                jenkins_url="https://jenkins.example.com/job/pipe/1/",
                status="completed",
                result=legacy_result,
            )

            result = await storage.get_result("legacy-pipeline")
            assert result is not None
            children = result["result"]["child_job_analyses"]
            # Child itself gets an id
            assert "id" in children[0]
            # Child failure gets an id
            assert "id" in children[0]["failures"][0]
            # Grandchild gets an id
            grandchild = children[0]["failed_children"][0]
            assert "id" in grandchild
            # Grandchild failure gets an id
            assert "id" in grandchild["failures"][0]

            # Verify stability on second read
            result2 = await storage.get_result("legacy-pipeline")
            children2 = result2["result"]["child_job_analyses"]
            assert children[0]["id"] == children2[0]["id"]
            assert children[0]["failures"][0]["id"] == children2[0]["failures"][0]["id"]

    async def test_results_with_existing_ids_not_modified(
        self, setup_test_db: Path
    ) -> None:
        """Results that already have 'id' fields are not re-written."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            existing_id = "existing-uuid-1234"
            result_data = {
                "summary": "Modern analysis",
                "failures": [
                    {
                        "id": existing_id,
                        "test_name": "test_x",
                        "error": "Error X",
                    },
                ],
            }
            await storage.save_result(
                job_id="modern-job",
                jenkins_url="https://jenkins.example.com/job/test/2/",
                status="completed",
                result=result_data,
            )

            result = await storage.get_result("modern-job")
            assert result is not None
            assert result["result"]["failures"][0]["id"] == existing_id

    async def test_backfill_no_failures_is_noop(self, setup_test_db: Path) -> None:
        """Result with no failures list triggers no backfill."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="no-failures",
                jenkins_url="https://jenkins.example.com/job/test/3/",
                status="completed",
                result={"summary": "All passed"},
            )

            result = await storage.get_result("no-failures")
            assert result is not None
            assert "failures" not in result["result"]


class TestListResults:
    """Tests for the list_results function."""

    async def test_list_results_empty(self, setup_test_db: Path) -> None:
        """Test listing results when database is empty."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            results = await storage.list_results()
            assert results == []

    async def test_list_results_returns_all(self, setup_test_db: Path) -> None:
        """Test listing all results."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            for i in range(3):
                await storage.save_result(
                    job_id=f"job-list-{i}",
                    jenkins_url=f"https://jenkins.example.com/job/test/{i}/",
                    status="completed",
                )

            results = await storage.list_results()
            assert len(results) == 3

    async def test_list_results_ordered_by_created_at_desc(
        self, setup_test_db: Path
    ) -> None:
        """Test that results are ordered by created_at descending."""
        import aiosqlite

        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert with explicit timestamps to ensure ordering
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO results (job_id, build_url, status, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        "job-order-0",
                        "https://jenkins.example.com/job/test/0/",
                        "completed",
                        "2024-01-01 10:00:00",
                    ),
                )
                await db.execute(
                    """INSERT INTO results (job_id, build_url, status, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        "job-order-1",
                        "https://jenkins.example.com/job/test/1/",
                        "completed",
                        "2024-01-01 11:00:00",
                    ),
                )
                await db.execute(
                    """INSERT INTO results (job_id, build_url, status, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        "job-order-2",
                        "https://jenkins.example.com/job/test/2/",
                        "completed",
                        "2024-01-01 12:00:00",
                    ),
                )
                await db.commit()

            results = await storage.list_results()
            # Most recent should be first
            assert results[0]["job_id"] == "job-order-2"
            assert results[2]["job_id"] == "job-order-0"

    async def test_list_results_respects_limit(self, setup_test_db: Path) -> None:
        """Test that limit parameter is respected."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            for i in range(10):
                await storage.save_result(
                    job_id=f"job-limit-{i}",
                    jenkins_url=f"https://jenkins.example.com/job/test/{i}/",
                    status="completed",
                )

            results = await storage.list_results(limit=5)
            assert len(results) == 5

    async def test_list_results_default_limit(self, setup_test_db: Path) -> None:
        """Test that default limit is 50."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Just verify we can call with no arguments
            results = await storage.list_results()
            assert isinstance(results, list)

    async def test_list_results_summary_fields(self, setup_test_db: Path) -> None:
        """Test that list_results returns only summary fields."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-fields",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="completed",
                result={"large": "data" * 1000},  # Large result data
            )

            results = await storage.list_results()
            assert len(results) == 1
            result = results[0]
            # Should have summary fields
            assert "job_id" in result
            assert "jenkins_url" in result
            assert "status" in result
            assert "created_at" in result
            # Should NOT have result_json (it's not in the select)
            assert "result_json" not in result
            assert "result" not in result


class TestOverrideClassification:
    """Tests for the override_classification function."""

    async def test_override_updates_failure_history(self, setup_test_db: Path) -> None:
        """Override classification updates the failure_history table."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert a failure_history row first
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-1",
                        "my-job",
                        1,
                        "tests.TestA.test_one",
                        "CODE ISSUE",
                        "error msg",
                    ),
                )
                await db.commit()

            await storage.override_classification(
                job_id="job-1",
                test_name="tests.TestA.test_one",
                classification="PRODUCT BUG",
            )

            # Verify updated
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT classification FROM failure_history WHERE job_id=? AND test_name=?",
                    ("job-1", "tests.TestA.test_one"),
                )
                row = await cursor.fetchone()
                assert row[0] == "PRODUCT BUG"

    async def test_override_with_child_job(self, setup_test_db: Path) -> None:
        """Override classification with child job scoping."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert two rows: one with child job, one without
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, child_job_name, child_build_number, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-2",
                        "parent-job",
                        10,
                        "tests.TestB.test_two",
                        "CODE ISSUE",
                        "error",
                        "child-job",
                        5,
                    ),
                )
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-2",
                        "parent-job",
                        10,
                        "tests.TestB.test_two",
                        "CODE ISSUE",
                        "error",
                    ),
                )
                await db.commit()

            # Override only the child job row
            await storage.override_classification(
                job_id="job-2",
                test_name="tests.TestB.test_two",
                classification="PRODUCT BUG",
                child_job_name="child-job",
                child_build_number=5,
            )

            # Verify only the child job row was updated
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT classification, child_job_name FROM failure_history "
                    "WHERE job_id=? AND test_name=? ORDER BY child_job_name",
                    ("job-2", "tests.TestB.test_two"),
                )
                rows = await cursor.fetchall()
                # Row without child_job_name should remain unchanged
                assert rows[0][0] == "CODE ISSUE"
                assert rows[0][1] == ""
                # Row with child_job_name should be updated
                assert rows[1][0] == "PRODUCT BUG"
                assert rows[1][1] == "child-job"

    async def test_override_no_matching_row(self, setup_test_db: Path) -> None:
        """Override with no matching row completes without error."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Should not raise even if no rows match
            await storage.override_classification(
                job_id="nonexistent-job",
                test_name="tests.TestX.test_missing",
                classification="CODE ISSUE",
            )

    async def test_override_updates_all_tests_with_same_error_signature(
        self, setup_test_db: Path
    ) -> None:
        """Finding 1: Override should update ALL tests sharing the same error_signature in the same job."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert multiple failure_history rows with the same error_signature
            async with aiosqlite.connect(setup_test_db) as db:
                for test_name in [
                    "tests.TestA.test_one",
                    "tests.TestA.test_two",
                    "tests.TestA.test_three",
                ]:
                    await db.execute(
                        """INSERT INTO failure_history
                           (job_id, job_name, build_number, test_name, classification,
                            error_message, error_signature, analyzed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                        (
                            "job-group",
                            "my-job",
                            1,
                            test_name,
                            "CODE ISSUE",
                            "same error",
                            "sig-shared-abc",
                        ),
                    )
                await db.commit()

            # Override using just the first test (representative test)
            await storage.override_classification(
                job_id="job-group",
                test_name="tests.TestA.test_one",
                classification="PRODUCT BUG",
                username="tester",
            )

            # ALL tests with the same error_signature should be updated
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT test_name, classification FROM failure_history "
                    "WHERE job_id='job-group' ORDER BY test_name",
                )
                rows = await cursor.fetchall()
                assert len(rows) == 3
                for row in rows:
                    assert row[1] == "PRODUCT BUG", (
                        f"Test {row[0]} should be PRODUCT BUG but got {row[1]}"
                    )

    async def test_override_creates_test_classification_entry(
        self, setup_test_db: Path
    ) -> None:
        """Finding 2: Override should also insert into test_classifications for AI learning."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert a failure_history row
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, child_job_name, child_build_number, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-tc",
                        "parent-pipeline",
                        10,
                        "tests.TestC.test_classify",
                        "CODE ISSUE",
                        "some error",
                        "child-job-1",
                        5,
                    ),
                )
                await db.commit()

            await storage.override_classification(
                job_id="job-tc",
                test_name="tests.TestC.test_classify",
                classification="PRODUCT BUG",
                child_job_name="child-job-1",
                child_build_number=5,
                username="reviewer",
            )

            # Verify test_classifications entry was created
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT test_name, classification, created_by, visible, "
                    "job_id, child_build_number "
                    "FROM test_classifications WHERE test_name=?",
                    ("tests.TestC.test_classify",),
                )
                row = await cursor.fetchone()
                assert row is not None, "test_classifications entry should exist"
                assert row[0] == "tests.TestC.test_classify"
                assert row[1] == "PRODUCT BUG"
                assert row[2] == "reviewer"
                assert row[3] == 1  # visible
                assert row[4] == "job-tc"
                assert row[5] == 5


class TestSetTestClassification:
    """Tests for the set_test_classification function."""

    async def test_child_job_with_zero_build_number_succeeds(
        self, setup_test_db: Path
    ) -> None:
        """job_name + child_build_number=0 must not raise; failure_history stays unchanged."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Seed a failure_history row
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, child_job_name, child_build_number, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-cls-zero",
                        "pipeline",
                        1,
                        "tests.TestA.test_one",
                        "CODE ISSUE",
                        "error",
                        "parent-job",
                        7,
                    ),
                )
                await db.commit()

            classification_id = await storage.set_test_classification(
                test_name="tests.TestA.test_one",
                classification="FLAKY",
                job_name="parent-job",
                child_build_number=0,
                job_id="job-cls-zero",
            )
            assert classification_id > 0

            # failure_history.classification must NOT be updated (no mirroring)
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT classification FROM failure_history "
                    "WHERE job_id=? AND test_name=? AND child_job_name=? AND child_build_number=?",
                    ("job-cls-zero", "tests.TestA.test_one", "parent-job", 7),
                )
                row = await cursor.fetchone()
                assert row is not None, "failure_history row should exist"
                assert row[0] == "CODE ISSUE", (
                    "failure_history must retain original AI classification"
                )

    async def test_defaults_only_classification_succeeds(
        self, setup_test_db: Path
    ) -> None:
        """Classification with only required fields succeeds."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            classification_id = await storage.set_test_classification(
                test_name="tests.TestA.test_one",
                classification="REGRESSION",
                job_id="job-cls-defaults",
            )
            assert classification_id > 0

    async def test_invalid_classification_raises_value_error(
        self, setup_test_db: Path
    ) -> None:
        """Invalid pattern classification raises ValueError."""
        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            pytest.raises(ValueError, match="Invalid pattern classification"),
        ):
            await storage.set_test_classification(
                test_name="tests.TestA.test_one",
                classification="INVALID",
                job_id="job-cls-invalid",
            )

    async def test_empty_job_id_raises_value_error(self, setup_test_db: Path) -> None:
        """Empty job_id raises ValueError."""
        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            pytest.raises(ValueError, match="job_id is required"),
        ):
            await storage.set_test_classification(
                test_name="tests.TestA.test_one", classification="FLAKY", job_id=""
            )


class TestMarkStaleResultsFailed:
    """Tests for the mark_stale_results_failed function."""

    async def test_marks_pending_as_failed(self, setup_test_db: Path) -> None:
        """Pending jobs are marked failed on startup."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("pending-1", "http://j/1", "pending")
            waiting, recovered = await storage.mark_stale_results_failed()
            assert waiting == []
            assert len(recovered) == 1
            assert recovered[0] == {"job_id": "pending-1", "previous_status": "pending"}
            result = await storage.get_result("pending-1")
            assert result["status"] == "failed"
            assert (
                result["error"]
                == "Analysis interrupted by server restart. Please re-submit."
            )
            assert result["completed_at"] is not None

    async def test_marks_running_as_failed(self, setup_test_db: Path) -> None:
        """Running jobs are marked failed on startup."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("running-1", "http://j/2", "running")
            waiting, recovered = await storage.mark_stale_results_failed()
            assert waiting == []
            assert len(recovered) == 1
            assert recovered[0] == {"job_id": "running-1", "previous_status": "running"}
            result = await storage.get_result("running-1")
            assert result["status"] == "failed"
            assert (
                result["error"]
                == "Analysis interrupted by server restart. Please re-submit."
            )
            assert result["completed_at"] is not None

    async def test_returns_waiting_jobs(self, setup_test_db: Path) -> None:
        """Waiting jobs are returned for resumption, not marked failed."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "my-job",
                "build_number": 42,
                "request_params": {
                    "ai_provider": "claude",
                    "tests_repo_url": "https://example.invalid/tests",
                },
            }
            await storage.save_result("waiting-1", "http://j/3", "waiting", result_data)
            waiting, recovered = await storage.mark_stale_results_failed()
            assert len(waiting) == 1
            assert waiting[0]["job_id"] == "waiting-1"
            assert waiting[0]["result_data"]["job_name"] == "my-job"
            assert (
                waiting[0]["result_data"]["request_params"]["tests_repo_url"]
                == "https://example.invalid/tests"
            )
            assert recovered == []
            # Status should still be 'waiting' (not failed)
            result = await storage.get_result("waiting-1")
            assert result["status"] == "waiting"

    async def test_mixed_statuses(self, setup_test_db: Path) -> None:
        """Pending/running are failed; waiting is returned; completed is untouched."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("p1", "http://j/1", "pending")
            await storage.save_result("r1", "http://j/2", "running")
            await storage.save_result(
                "w1",
                "http://j/3",
                "waiting",
                {
                    "job_name": "w",
                    "build_number": 1,
                    "request_params": {
                        "tests_repo_url": "https://example.invalid/tests",
                    },
                },
            )
            await storage.save_result(
                "c1", "http://j/4", "completed", {"summary": "ok"}
            )

            waiting, recovered = await storage.mark_stale_results_failed()
            assert len(waiting) == 1
            assert waiting[0]["job_id"] == "w1"

            # Check recovered jobs contain both pending and running
            recovered_ids = {r["job_id"]: r["previous_status"] for r in recovered}
            assert recovered_ids == {"p1": "pending", "r1": "running"}

            assert (await storage.get_result("p1"))["status"] == "failed"
            assert (await storage.get_result("r1"))["status"] == "failed"
            assert (await storage.get_result("w1"))["status"] == "waiting"
            assert (await storage.get_result("c1"))["status"] == "completed"

    async def test_waiting_without_result_json_marked_failed(
        self, setup_test_db: Path
    ) -> None:
        """Waiting rows without result_json are marked as failed (unrecoverable)."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("w-empty", "http://j/5", "waiting", None)
            waiting, recovered = await storage.mark_stale_results_failed()
            assert waiting == []
            assert recovered == []
            # Verify it was marked as failed
            result = await storage.get_result("w-empty")
            assert result["status"] == "failed"

    @pytest.mark.parametrize(
        "result_data",
        [
            pytest.param(
                {"job_name": "j"},
                id="missing-build_number-and-request_params",
            ),
            pytest.param(
                {"job_name": "j", "build_number": 1},
                id="missing-request_params",
            ),
            pytest.param(
                {"build_number": 1, "request_params": {}},
                id="missing-job_name",
            ),
            pytest.param(
                {"job_name": "j", "build_number": 1, "request_params": "bad"},
                id="request_params-not-dict",
            ),
        ],
    )
    async def test_waiting_with_incomplete_payload_marked_failed(
        self, setup_test_db: Path, result_data: dict
    ) -> None:
        """Waiting rows with valid JSON but missing required keys are failed."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "w-incomplete", "http://j/6", "waiting", result_data
            )
            waiting, recovered = await storage.mark_stale_results_failed()
            assert waiting == []
            assert recovered == []
            result = await storage.get_result("w-incomplete")
            assert result["status"] == "failed"

    async def test_waiting_with_malformed_json_marked_failed(
        self, setup_test_db: Path
    ) -> None:
        """Waiting rows with malformed JSON are marked as failed (unrecoverable)."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    "INSERT INTO results (job_id, build_url, status, result_json) "
                    "VALUES (?, ?, ?, ?)",
                    ("w-bad-json", "http://j/7", "waiting", "{not-json"),
                )
                await db.commit()

            waiting, recovered = await storage.mark_stale_results_failed()
            assert waiting == []
            assert recovered == []
            assert (await storage.get_result("w-bad-json"))["status"] == "failed"


class TestProgressPhaseHelpers:
    """Tests for _make_progress_phase_patcher and update_progress_phase."""

    def test_make_progress_phase_patcher_sets_field(self) -> None:
        """_make_progress_phase_patcher creates a callable that sets progress_phase."""
        patcher = storage._make_progress_phase_patcher("analyzing")
        d: dict = {"existing_key": "value"}
        patcher(d)
        assert d["progress_phase"] == "analyzing"
        assert d["existing_key"] == "value"

    def test_make_progress_phase_patcher_overwrites_existing(self) -> None:
        """Patcher overwrites an existing progress_phase value."""
        patcher = storage._make_progress_phase_patcher("saving")
        d: dict = {"progress_phase": "analyzing"}
        patcher(d)
        assert d["progress_phase"] == "saving"

    async def test_update_progress_phase_persists(self, setup_test_db: Path) -> None:
        """update_progress_phase writes the phase into result_json."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "phase-job",
                "http://jenkins/1",
                "running",
                {"job_name": "test", "build_number": 1},
            )
            await storage.update_progress_phase("phase-job", "analyzing")

            result = await storage.get_result("phase-job")
            assert result is not None
            assert result["result"]["progress_phase"] == "analyzing"

    async def test_update_progress_phase_overwrites(self, setup_test_db: Path) -> None:
        """Calling update_progress_phase twice overwrites the phase."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "phase-job-2",
                "http://jenkins/2",
                "running",
                {"job_name": "test", "build_number": 2},
            )
            await storage.update_progress_phase("phase-job-2", "analyzing")
            await storage.update_progress_phase("phase-job-2", "saving")

            result = await storage.get_result("phase-job-2")
            assert result is not None
            assert result["result"]["progress_phase"] == "saving"

    async def test_update_progress_phase_noop_for_missing_job(
        self, setup_test_db: Path
    ) -> None:
        """update_progress_phase is a no-op for non-existent job_id."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Should not raise
            await storage.update_progress_phase("nonexistent-job", "analyzing")

    async def test_update_progress_phase_appends_to_progress_log(
        self, setup_test_db: Path
    ) -> None:
        """Each call to update_progress_phase appends an entry to progress_log."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "phase-log-job",
                "http://jenkins/10",
                "running",
                {"job_name": "test", "build_number": 10},
            )
            await storage.update_progress_phase("phase-log-job", "analyzing")
            await storage.update_progress_phase("phase-log-job", "saving")

            result = await storage.get_result("phase-log-job")
            assert result is not None
            log = result["result"]["progress_log"]
            assert isinstance(log, list)
            assert len(log) == 2
            assert log[0]["phase"] == "analyzing"
            assert log[1]["phase"] == "saving"
            # Each entry must have a timestamp
            assert isinstance(log[0]["timestamp"], (int, float))
            assert isinstance(log[1]["timestamp"], (int, float))
            # Timestamps should be ordered
            assert log[1]["timestamp"] >= log[0]["timestamp"]

    async def test_update_progress_phase_initializes_progress_log(
        self, setup_test_db: Path
    ) -> None:
        """First call creates the progress_log array."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "phase-init-job",
                "http://jenkins/11",
                "running",
                {"job_name": "test", "build_number": 11},
            )
            await storage.update_progress_phase("phase-init-job", "waiting_for_jenkins")

            result = await storage.get_result("phase-init-job")
            assert result is not None
            log = result["result"]["progress_log"]
            assert isinstance(log, list)
            assert len(log) == 1
            assert log[0]["phase"] == "waiting_for_jenkins"

    async def test_clone_progress_tracks_parallel_repos_and_refreshes(
        self, setup_test_db: Path
    ) -> None:
        """Clone changes serialize current names without losing the stage history."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("clone-job", "", "running", {"job_name": "test"})
            await storage.update_clone_progress("clone-job", "tests", True)
            await asyncio.gather(
                storage.update_clone_progress("clone-job", "one", True),
                storage.update_clone_progress("clone-job", "two", True),
            )
            result = (await storage.get_result("clone-job"))["result"]
            assert result["progress_phase"] == "cloning"
            assert set(result["cloning_repos"]) == {"tests", "one", "two"}
            assert set(result["progress_log"][-1]["repos"]) == set(
                result["cloning_repos"]
            )
            await storage.update_clone_progress("clone-job", "one", False)
            refreshed = (await storage.get_result("clone-job"))["result"]
            assert set(refreshed["cloning_repos"]) == {"tests", "two"}
            assert refreshed["progress_log"][-1]["repos"] == refreshed["cloning_repos"]

    async def test_terminal_update_preserves_clone_history(
        self, setup_test_db: Path
    ) -> None:
        """A fresh result payload cannot erase persisted progress on failure."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("clone-fail", "", "running", {"job_name": "test"})
            await storage.update_clone_progress("clone-fail", "tests", True)
            await storage.update_clone_progress("clone-fail", "tests", False)
            await storage.update_status(
                "clone-fail", "failed", {"error": "clone failed"}
            )
            result = (await storage.get_result("clone-fail"))["result"]
            assert result["progress_log"][0]["repos"] == ["tests"]
            assert result["cloning_repos"] == []
            assert result["progress_phase"] == "failed"

    async def test_cancelled_clone_clears_current_names_and_keeps_log(
        self, setup_test_db: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "clone-abort", "", "running", {"job_name": "test"}
            )
            await storage.update_clone_progress("clone-abort", "slow", True)
            await storage.update_status(
                "clone-abort", "aborted", {"error": "cancelled"}
            )
            await storage.update_clone_progress("clone-abort", "slow", False)
            result = (await storage.get_result("clone-abort"))["result"]
            assert result["cloning_repos"] == []
            assert result["progress_phase"] == "aborted"
            assert result["progress_log"][0]["repos"] == ["slow"]

    async def test_clone_progress_done_does_not_restore_clone_phase(
        self, setup_test_db: Path
    ) -> None:
        """Late clone cleanup after cancellation cannot overwrite the terminal stage."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("clone-done", "", "running", {"job_name": "test"})
            await storage.update_clone_progress("clone-done", "tests", True)
            await storage.update_progress_phase("clone-done", "analyzing")
            await storage.update_clone_progress("clone-done", "tests", False)
            result = (await storage.get_result("clone-done"))["result"]
            assert result["progress_phase"] == "analyzing"
            assert result["cloning_repos"] == []

    async def test_update_progress_phase_preserves_existing_progress_log(
        self, setup_test_db: Path
    ) -> None:
        """progress_log is preserved across updates and not reset."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "phase-preserve-job",
                "http://jenkins/12",
                "running",
                {"job_name": "test", "build_number": 12},
            )
            phases = ["waiting_for_jenkins", "analyzing", "enriching_jira", "saving"]
            for phase in phases:
                await storage.update_progress_phase("phase-preserve-job", phase)

            result = await storage.get_result("phase-preserve-job")
            assert result is not None
            log = result["result"]["progress_log"]
            assert len(log) == len(phases)
            for i, phase in enumerate(phases):
                assert log[i]["phase"] == phase


class TestGetHistoryClassification:
    """Tests for the get_history_classification function."""

    async def test_returns_empty_string_when_no_data(self, setup_test_db: Path) -> None:
        """Returns empty string when no classification data exists."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            cls = await storage.get_history_classification("job-missing", "test_x")
            assert cls == ""

    async def test_returns_persistent_from_test_classifications(
        self, setup_test_db: Path
    ) -> None:
        """Returns PERSISTENT from visible test_classifications entries."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_test_classification(
                test_name="tests.TestA.test_infra",
                classification="PERSISTENT",
                job_id="job-infra-1",
                visible=1,
            )
            cls = await storage.get_history_classification(
                "job-infra-1", "tests.TestA.test_infra"
            )
            assert cls == "PERSISTENT"

    async def test_returns_flaky_from_test_classifications(
        self, setup_test_db: Path
    ) -> None:
        """Returns FLAKY from visible test_classifications entries."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_test_classification(
                test_name="tests.TestA.test_flaky",
                classification="FLAKY",
                job_id="job-flaky-1",
                visible=1,
            )
            cls = await storage.get_history_classification(
                "job-flaky-1", "tests.TestA.test_flaky"
            )
            assert cls == "FLAKY"

    async def test_ignores_primary_classifications(self, setup_test_db: Path) -> None:
        """Does NOT return CODE ISSUE or PRODUCT BUG (those are primary, not history)."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert a failure_history row with CODE ISSUE
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-pri",
                        "my-job",
                        1,
                        "tests.TestA.test_pri",
                        "CODE ISSUE",
                        "err",
                    ),
                )
                await db.commit()
            cls = await storage.get_history_classification(
                "job-pri", "tests.TestA.test_pri"
            )
            assert cls == ""

    async def test_falls_back_to_failure_history(self, setup_test_db: Path) -> None:
        """Falls back to failure_history pattern when no test_classifications entry."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        pattern, error_message, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-fh",
                        "my-job",
                        1,
                        "tests.TestA.test_fh",
                        "INFRASTRUCTURE",
                        "PERSISTENT",
                        "err",
                    ),
                )
                await db.commit()
            cls = await storage.get_history_classification(
                "job-fh", "tests.TestA.test_fh"
            )
            assert cls == "PERSISTENT"

    async def test_prefers_test_classifications_over_failure_history(
        self, setup_test_db: Path
    ) -> None:
        """test_classifications takes precedence over failure_history."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # failure_history says INFRASTRUCTURE
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-pref",
                        "my-job",
                        1,
                        "tests.TestA.test_pref",
                        "INFRASTRUCTURE",
                        "err",
                    ),
                )
                await db.commit()
            # test_classifications says FLAKY
            await storage.set_test_classification(
                test_name="tests.TestA.test_pref",
                classification="FLAKY",
                job_id="job-pref",
                visible=1,
            )
            cls = await storage.get_history_classification(
                "job-pref", "tests.TestA.test_pref"
            )
            assert cls == "FLAKY"

    async def test_ignores_invisible_classifications(self, setup_test_db: Path) -> None:
        """Does not return classifications with visible=0."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_test_classification(
                test_name="tests.TestA.test_hidden",
                classification="PERSISTENT",
                job_id="job-hidden",
                visible=0,
            )
            cls = await storage.get_history_classification(
                "job-hidden", "tests.TestA.test_hidden"
            )
            assert cls == ""

    async def test_with_child_job_context(self, setup_test_db: Path) -> None:
        """Scopes lookup by child_job_name and child_build_number."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_test_classification(
                test_name="tests.TestA.test_child",
                classification="PERSISTENT",
                job_id="job-child",
                job_name="child-job-1",
                child_build_number=5,
                visible=1,
            )
            # Insert a second row with different child scope
            await storage.set_test_classification(
                test_name="tests.TestA.test_child",
                classification="REGRESSION",
                job_id="job-child",
                job_name="child-job-2",
                child_build_number=6,
                visible=1,
            )
            # Should return PERSISTENT for child-job-1
            cls = await storage.get_history_classification(
                "job-child",
                "tests.TestA.test_child",
                child_job_name="child-job-1",
                child_build_number=5,
            )
            assert cls == "PERSISTENT"
            # Should return REGRESSION for child-job-2
            cls2 = await storage.get_history_classification(
                "job-child",
                "tests.TestA.test_child",
                child_job_name="child-job-2",
                child_build_number=6,
            )
            assert cls2 == "REGRESSION"


class TestFindFailureByUuid:
    """Tests for find_failure_by_uuid."""

    async def test_find_in_flat_failures(self, setup_test_db: Path) -> None:
        """Test finding a failure by UUID in a flat failures list."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-flat",
                jenkins_url="http://jenkins/flat",
                status="completed",
                result={
                    "failures": [
                        {"id": "uuid-aaa", "test_name": "test_one", "error": "boom"},
                        {"id": "uuid-bbb", "test_name": "test_two", "error": "crash"},
                    ],
                },
            )
            result = await storage.find_failure_by_uuid("uuid-bbb")
            assert result is not None
            assert result["job_id"] == "job-flat"
            assert result["failure"]["test_name"] == "test_two"
            assert result["child_job_name"] == ""
            assert result["child_build_number"] == 0

    async def test_find_in_nested_children(self, setup_test_db: Path) -> None:
        """Test finding a failure in nested child job analyses."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-parent",
                jenkins_url="http://jenkins/parent",
                status="completed",
                result={
                    "failures": [],
                    "child_job_analyses": [
                        {
                            "job_name": "child-1",
                            "build_number": 10,
                            "failures": [
                                {
                                    "id": "uuid-child-1",
                                    "test_name": "test_child",
                                    "error": "err",
                                },
                            ],
                            "failed_children": [],
                        },
                    ],
                },
            )
            result = await storage.find_failure_by_uuid("uuid-child-1")
            assert result is not None
            assert result["job_id"] == "job-parent"
            assert result["failure"]["test_name"] == "test_child"
            assert result["child_job_name"] == "child-1"
            assert result["child_build_number"] == 10

    async def test_returns_none_for_unknown_uuid(self, setup_test_db: Path) -> None:
        """Test returning None for unknown UUID."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-exists",
                jenkins_url="http://jenkins/exists",
                status="completed",
                result={
                    "failures": [
                        {"id": "uuid-known", "test_name": "test_known", "error": "ok"},
                    ],
                },
            )
            result = await storage.find_failure_by_uuid("uuid-does-not-exist")
            assert result is None


async def test_fail_job_without_result_write_keeps_a_newer_terminal_status(
    setup_test_db: Path,
) -> None:
    """The status-only fallback must not end a job an abort already ended (#419).

    It exists because a write to an unsupported payload was refused, so it is a
    consequence of the refusal and not a decision about the job: a terminal status
    that landed first is newer than that refusal and keeps its error. A job still
    running is still marked failed. Both go through the guarded write path every
    other status write uses.
    """
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result("job-aborted-first", "", "running")
        await storage.update_status("job-aborted-first", "aborted")
        await storage.fail_job_without_result_write("job-aborted-first", "refused")

        await storage.save_result("job-running", "", "running")
        await storage.fail_job_without_result_write("job-running", "refused")

        async def stored(job_id: str) -> Any:
            async with storage._connect_db() as db:
                return await (
                    await db.execute(
                        "SELECT status, error, completed_at FROM results"
                        " WHERE job_id = ?",
                        (job_id,),
                    )
                ).fetchone()

        aborted = await stored("job-aborted-first")
        failed = await stored("job-running")

    assert aborted["status"] == "aborted"
    assert not aborted["error"]
    assert not aborted["completed_at"], "the fallback stamped an unrelated job"
    assert failed["status"] == "failed"
    assert failed["error"] == "refused"
    assert failed["completed_at"]


class TestMarkJobsFailedForRestart:
    """The mark the shutdown writes before it drains (#458)."""

    async def test_a_restart_marked_job_is_not_recovered_as_an_orphan(
        self, setup_test_db: Path
    ) -> None:
        """The next boot does not report it: it is failed, not an orphan."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("job-x", "", "running")
            assert await storage.mark_jobs_failed_for_restart(["job-x"]) == ["job-x"]

            waiting, recovered = await storage.mark_stale_results_failed()
            assert waiting == []
            assert recovered == []
            row = await storage.get_result("job-x")

        assert row["status"] == "failed"
        assert row["error"] == storage.RESTART_ERROR_MSG
        assert row["failure_reason"] == "server_restart"
        assert row["completed_at"] is not None

    async def test_the_restart_mark_keeps_a_job_that_already_ended(
        self, setup_test_db: Path
    ) -> None:
        """A job that finished inside the shutdown window keeps its own status."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("job-done", "", "running")
            await storage.update_status("job-done", "completed")
            assert await storage.mark_jobs_failed_for_restart(["job-done"]) == []
            row = await storage.get_result("job-done")

        assert row["status"] == "completed"
        assert row["failure_reason"] == ""

    async def test_a_waiting_row_is_refused_by_the_storage_guard(
        self, setup_test_db: Path
    ) -> None:
        """``waiting`` is resumed by the next boot, so the write must refuse it."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("job-w", "", "waiting")
            landed = await storage.fail_job_without_result_write(
                "job-w",
                storage.RESTART_ERROR_MSG,
                failure_reason=storage.SERVER_RESTART_FAILURE_REASON,
                only_statuses=storage.ORPHAN_STATUSES,
            )
            row = await storage.get_result("job-w")

        assert landed is False
        assert row["status"] == "waiting"
        assert row["failure_reason"] == ""

    async def test_an_in_place_analyze_clears_the_restart_marker(
        self, setup_test_db: Path
    ) -> None:
        """The reused row must not keep a marker from the run it replaces.

        ``POST /results/{job_id}/analyze`` claims this row in place, so a marker
        written by the shutdown would survive into the new run and be reported
        beside its ``completed`` status (#458).
        """
        from rootcoz.storage import ANALYSIS_STATE_SUBMITTED

        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "job-sub",
                "",
                "running",
                {"analysis_state": ANALYSIS_STATE_SUBMITTED, "job_name": "raw"},
            )
            await storage.mark_jobs_failed_for_restart(["job-sub"])
            claim = await storage.claim_submitted_job_for_analyze("job-sub")
            row = await storage.get_result("job-sub")

        # The claim reports what it overwrote, so a release can put it back.
        assert claim == ("failed", storage.SERVER_RESTART_FAILURE_REASON)
        assert row["status"] == "pending"
        assert row["failure_reason"] == ""

    @pytest.mark.parametrize(
        ("restart_marked", "status", "reason"),
        [
            (False, "completed", ""),
            (True, "failed", storage.SERVER_RESTART_FAILURE_REASON),
        ],
    )
    async def test_a_released_claim_restores_the_status_and_the_reason(
        self, setup_test_db: Path, restart_marked: bool, status: str, reason: str
    ) -> None:
        """Claim -> release puts back **both** columns the claim cleared (#458).

        A release that restores only the status leaves a ``failed`` row whose
        reason is empty -- the combination the docs read as a crash -- even though
        the claim cleared a reason that was there.
        """
        from rootcoz.storage import ANALYSIS_STATE_SUBMITTED

        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "job-release",
                "",
                "running",
                {"analysis_state": ANALYSIS_STATE_SUBMITTED, "job_name": "raw"},
            )
            if restart_marked:
                await storage.mark_jobs_failed_for_restart(["job-release"])
            else:
                await storage.update_status("job-release", "completed")
            claim = await storage.claim_submitted_job_for_analyze("job-release")
            assert claim is not None
            assert claim == (status, reason)
            await storage.release_submitted_job_analyze_claim("job-release", *claim)
            row = await storage.get_result("job-release")

        assert row["status"] == status
        assert row["failure_reason"] == reason

    async def test_a_budget_cut_mark_logs_how_far_it_got(
        self, setup_test_db: Path, storage_logs: Any
    ) -> None:
        """A cancelled mark must say why only some jobs carry the marker (#458).

        ``asyncio.timeout`` cancels the await inside the mark; CancelledError is a
        ``BaseException``, so the per-row ``except Exception`` cannot record it and
        this warning is the only trace an operator gets. The cancellation itself
        must still propagate.
        """
        real = storage.fail_job_without_result_write
        seen: list[str] = []

        async def _one_then_the_budget_ends(job_id: str, *args: Any, **kwargs: Any):
            seen.append(job_id)
            if len(seen) > 1:
                raise asyncio.CancelledError
            return await real(job_id, *args, **kwargs)

        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            patch.object(
                storage, "fail_job_without_result_write", _one_then_the_budget_ends
            ),
        ):
            for job_id in ("job-marked", "job-unmarked"):
                await storage.save_result(job_id, "", "running")
            with pytest.raises(asyncio.CancelledError):
                await storage.mark_jobs_failed_for_restart(
                    ["job-marked", "job-unmarked"]
                )
            marked = await storage.get_result("job-marked")
            unmarked = await storage.get_result("job-unmarked")

        assert marked["failure_reason"] == storage.SERVER_RESTART_FAILURE_REASON
        assert unmarked["failure_reason"] == ""
        warnings = [
            record.getMessage()
            for record in storage_logs.records
            if record.levelno == logging.WARNING
        ]
        assert any("1 of 2 in-flight job(s)" in line for line in warnings), warnings
        assert any("job-marked" in line for line in warnings), warnings


@pytest.mark.asyncio
async def test_list_distinct_job_names(setup_test_db: Path):
    """list_distinct_job_names returns unique non-empty job names."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        # Store results with various job names
        for job_id, job_name in [
            ("job-a", "pipeline-alpha"),
            ("job-b", "pipeline-beta"),
            ("job-c", "pipeline-alpha"),  # duplicate
            ("job-d", ""),  # empty — should be filtered
        ]:
            await storage.save_result(
                job_id=job_id,
                jenkins_url="",
                status="completed",
                result={"job_name": job_name, "failures": []},
            )

        names = await storage.list_distinct_job_names()
        assert isinstance(names, set)
        assert "pipeline-alpha" in names
        assert "pipeline-beta" in names
        assert "" not in names
        # Duplicates should be deduplicated
        assert len([n for n in names if n == "pipeline-alpha"]) == 1


class _PlanRecorder:
    """Proxy an aiosqlite connection, recording the query plan of each SELECT."""

    def __init__(self, db: aiosqlite.Connection, plans: list[tuple[str, list[str]]]):
        self._db = db
        self._plans = plans

    async def execute(self, sql: str, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            cursor = await self._db.execute("EXPLAIN QUERY PLAN " + sql, params)
            self._plans.append((sql, [row[-1] for row in await cursor.fetchall()]))
        return await self._db.execute(sql, params)

    def __getattr__(self, name: str):
        return getattr(self._db, name)


@asynccontextmanager
async def _recording_connect(plans: list[tuple[str, list[str]]]):
    """Drop-in replacement for ``storage._connect_db`` that collects plans."""
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield _PlanRecorder(db, plans)


def _plan_for(plans: list[tuple[str, list[str]]], marker: str) -> list[str]:
    """Return the recorded plan for the first query containing *marker*."""
    for sql, plan in plans:
        if marker in sql:
            return plan
    raise AssertionError(f"no recorded query containing {marker!r}: {plans}")


class TestDashboardQueryPlan:
    """The dashboard page query must be index-served, not temp-B-tree sorted."""

    async def _seed(self, db_path: Path, count: int = 400) -> None:
        """Seed rows the way save_result writes them (job_name denormalized)."""
        with patch.object(storage, "DB_PATH", db_path):
            await storage.init_db()
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                "INSERT INTO results (job_id, build_url, status, result_json, job_name) "
                "VALUES (?, '', 'completed', ?, ?)",
                [
                    (
                        f"plan-{i}",
                        json.dumps({"job_name": f"plan-job-{i % 20}", "failures": []}),
                        f"plan-job-{i % 20}",
                    )
                    for i in range(count)
                ],
            )
            await db.execute("ANALYZE")
            await db.commit()

    async def test_page_query_uses_created_at_index(self, setup_test_db: Path) -> None:
        await self._seed(setup_test_db)
        plans: list[tuple[str, list[str]]] = []
        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            patch.object(storage, "_connect_db", lambda: _recording_connect(plans)),
        ):
            await storage.list_results_for_dashboard(limit=20, offset=0)

        plan = _plan_for(plans, "ORDER BY r.created_at DESC")
        assert any("idx_results_created_at" in step for step in plan), plan
        assert not any("TEMP B-TREE" in step for step in plan), plan

    async def test_job_name_include_filter_uses_job_name_index(
        self, setup_test_db: Path
    ) -> None:
        await self._seed(setup_test_db)
        plans: list[tuple[str, list[str]]] = []
        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            patch.object(storage, "_connect_db", lambda: _recording_connect(plans)),
        ):
            await storage.list_results_for_dashboard_filtered(
                job_names={"plan-job-1", "plan-job-2"}, limit=20
            )

        plan = _plan_for(plans, "WHERE r.job_name IN")
        assert any("idx_results_job_name" in step for step in plan), plan

    async def test_job_name_equality_uses_job_name_index(
        self, setup_test_db: Path
    ) -> None:
        await self._seed(setup_test_db)
        plans: list[tuple[str, list[str]]] = []
        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            patch.object(storage, "_connect_db", lambda: _recording_connect(plans)),
        ):
            await storage.get_job_stats("plan-job-1")

        plan = _plan_for(plans, "COUNT(DISTINCT job_id) FROM results")
        assert any("idx_results_job_name" in step for step in plan), plan

    async def test_substring_search_remains_a_scan(self, setup_test_db: Path) -> None:
        """LIKE '%term%' cannot use the index — the search path stays a scan."""
        await self._seed(setup_test_db)
        plans: list[tuple[str, list[str]]] = []
        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            patch.object(storage, "_connect_db", lambda: _recording_connect(plans)),
        ):
            await storage.list_results_for_dashboard_filtered(search="plan", limit=20)

        plan = _plan_for(plans, "LIKE")
        assert any(step.startswith("SCAN r") for step in plan), plan

    async def test_offset_paging_is_deterministic_for_equal_created_at(
        self, setup_test_db: Path
    ) -> None:
        """Rows sharing created_at tie-break on rowid, so OFFSET paging is stable."""
        await self._seed(setup_test_db, count=12)
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    "UPDATE results SET created_at = '2026-01-01 00:00:00'"
                )
                await db.commit()

            pages = [
                await storage.list_results_for_dashboard(limit=5, offset=offset)
                for offset in (0, 5, 10)
            ]
            first_page_again = await storage.list_results_for_dashboard(
                limit=5, offset=0
            )

        ids = [job["job_id"] for page in pages for job in page]
        assert len(ids) == len(set(ids)) == 12
        assert [job["job_id"] for job in first_page_again] == [
            job["job_id"] for job in pages[0]
        ]


class TestDashboardJobNamePredicates:
    """Job-name filters read the denormalized column, keeping the same sets."""

    async def _seed(self, db_path: Path) -> None:
        with patch.object(storage, "DB_PATH", db_path):
            for job_id, job_name in [
                ("job-ocp16", "ocp-4.16-e2e"),
                ("job-ocp17", "ocp-4.17-e2e"),
                ("job-storage", "storage-nightly"),
                ("job-noname", ""),
            ]:
                await storage.save_result(
                    job_id=job_id,
                    jenkins_url="",
                    status="completed",
                    result={"job_name": job_name, "failures": []},
                )

    async def _job_ids(self, db_path: Path, **filters) -> set[str]:
        with patch.object(storage, "DB_PATH", db_path):
            result = await storage.list_results_for_dashboard_filtered(
                limit=0, **filters
            )
        return {job["job_id"] for job in result["jobs"]}

    async def test_search_matches_job_name_and_job_id(
        self, setup_test_db: Path
    ) -> None:
        await self._seed(setup_test_db)
        assert await self._job_ids(setup_test_db, search="ocp-4.1") == {
            "job-ocp16",
            "job-ocp17",
        }
        assert await self._job_ids(setup_test_db, search="job-storage") == {
            "job-storage"
        }

    async def test_include_job_names(self, setup_test_db: Path) -> None:
        await self._seed(setup_test_db)
        assert await self._job_ids(
            setup_test_db, job_names={"ocp-4.16-e2e", "storage-nightly"}
        ) == {"job-ocp16", "job-storage"}

    async def test_exclude_job_names_keeps_rows_without_a_job_name(
        self, setup_test_db: Path
    ) -> None:
        await self._seed(setup_test_db)
        assert await self._job_ids(
            setup_test_db, exclude_job_names={"ocp-4.17-e2e"}
        ) == {"job-ocp16", "job-storage", "job-noname"}


class TestRBACMigration:
    """Tests for the role='user' -> role='operator' migration."""

    async def test_migration_converts_user_to_operator(
        self, temp_db_path: Path
    ) -> None:
        """Existing role='user' rows are migrated to role='operator'."""
        # Create DB with old schema: insert user with role='user'
        async with aiosqlite.connect(temp_db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, username TEXT UNIQUE, "
                "api_key_hash TEXT, role TEXT DEFAULT 'user', "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            await db.execute(
                "INSERT INTO users (username, role) VALUES ('olduser1', 'user')"
            )
            await db.execute(
                "INSERT INTO users (username, role) VALUES ('oldadmin1', 'admin')"
            )
            await db.commit()

        # Run init_db which triggers migration
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()

        # Verify migration
        async with aiosqlite.connect(temp_db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT username, role FROM users ORDER BY username"
            )
            rows = {r["username"]: r["role"] for r in await cursor.fetchall()}

        assert rows["oldadmin1"] == "admin"  # admin unchanged
        assert rows["olduser1"] == "operator"  # user -> operator

    async def test_valid_roles_constant(self) -> None:
        """VALID_ROLES contains exactly the four expected roles."""
        assert storage.VALID_ROLES == frozenset(
            {"viewer", "reviewer", "operator", "admin"}
        )

    async def test_change_role_accepts_all_roles(self, setup_test_db: Path) -> None:
        """change_user_role accepts viewer, reviewer, operator, and admin."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.create_admin_user("roletest")
            # admin -> operator
            username, _ = await storage.change_user_role("roletest", "operator")
            assert username == "roletest"
            user = await storage.get_user_by_username("roletest")
            assert user["role"] == "operator"
            # operator -> reviewer
            await storage.change_user_role("roletest", "reviewer")
            user = await storage.get_user_by_username("roletest")
            assert user["role"] == "reviewer"
            # reviewer -> viewer
            await storage.change_user_role("roletest", "viewer")
            user = await storage.get_user_by_username("roletest")
            assert user["role"] == "viewer"
            # viewer -> admin
            await storage.change_user_role("roletest", "admin")
            user = await storage.get_user_by_username("roletest")
            assert user["role"] == "admin"

    async def test_change_role_rejects_invalid(self, setup_test_db: Path) -> None:
        """change_user_role rejects invalid role values."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.create_admin_user("badtest")
            with pytest.raises(ValueError, match="Invalid role"):
                await storage.change_user_role("badtest", "user")
            with pytest.raises(ValueError, match="Invalid role"):
                await storage.change_user_role("badtest", "superuser")


class TestFailureHistoryBuildIdMigration:
    """Tests for _migrate_failure_history_build_id validation."""

    async def test_backfill_accepts_only_numeric_text_build_ids(
        self, setup_test_db: Path
    ) -> None:
        """Migration keeps only JSON-text digit build IDs; rejects coerced types."""
        import json

        cases = [
            # job_id, result_json, expected build_id after migrate
            (
                "job-top-text",
                {"build_id": "2072319655766134784"},
                "2072319655766134784",
            ),
            (
                "job-params-text",
                {"request_params": {"build_id": "42"}},
                "42",
            ),
            (
                "job-bool",
                {"build_id": True},
                "",
            ),
            (
                "job-int",
                {"build_id": 123},
                "",
            ),
            (
                "job-mixed",
                {"build_id": "12ab"},
                "",
            ),
            (
                "job-empty",
                {"build_id": ""},
                "",
            ),
            (
                "job-prefer-top",
                {
                    "build_id": "99",
                    "request_params": {"build_id": "true"},
                },
                "99",
            ),
            (
                "job-fallback-params",
                {
                    "build_id": True,
                    "request_params": {"build_id": "77"},
                },
                "77",
            ),
        ]

        async with aiosqlite.connect(setup_test_db) as db:
            for job_id, result_json, _expected in cases:
                await db.execute(
                    """INSERT INTO results (job_id, status, result_json)
                       VALUES (?, 'completed', ?)""",
                    (job_id, json.dumps(result_json)),
                )
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, build_id, test_name,
                        classification, error_message)
                       VALUES (?, ?, 1, '', ?, 'CODE ISSUE', 'err')""",
                    (job_id, f"job-{job_id}", f"test-{job_id}"),
                )
            await db.commit()
            await storage._migrate_failure_history_build_id(db)
            await db.commit()

            db.row_factory = aiosqlite.Row
            for job_id, _result_json, expected in cases:
                cursor = await db.execute(
                    "SELECT build_id FROM failure_history WHERE job_id = ?",
                    (job_id,),
                )
                row = await cursor.fetchone()
                assert row is not None
                assert row["build_id"] == expected, (
                    f"{job_id}: expected {expected!r}, got {row['build_id']!r}"
                )

    async def test_backfill_skips_malformed_result_json(
        self, setup_test_db: Path
    ) -> None:
        """Malformed result_json must not abort migration; build_id stays empty."""
        async with aiosqlite.connect(setup_test_db) as db:
            await db.execute(
                """INSERT INTO results (job_id, status, result_json)
                   VALUES (?, 'completed', ?)""",
                ("job-bad-json", "{not-json"),
            )
            await db.execute(
                """INSERT INTO failure_history
                   (job_id, job_name, build_number, build_id, test_name,
                    classification, error_message)
                   VALUES (?, 'job', 1, '', 'test-bad', 'CODE ISSUE', 'err')""",
                ("job-bad-json",),
            )
            await db.commit()
            await storage._migrate_failure_history_build_id(db)
            await db.commit()

            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT build_id FROM failure_history WHERE job_id = ?",
                ("job-bad-json",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["build_id"] == ""

    async def test_backfill_skips_when_no_eligible_rows(
        self, setup_test_db: Path
    ) -> None:
        """Ineligible empty build_ids must not keep the migration re-running."""
        import json

        async with aiosqlite.connect(setup_test_db) as db:
            # Eligible: numeric text build_id in result_json
            await db.execute(
                """INSERT INTO results (job_id, status, result_json)
                   VALUES (?, 'completed', ?)""",
                ("job-eligible", json.dumps({"build_id": "12345"})),
            )
            await db.execute(
                """INSERT INTO failure_history
                   (job_id, job_name, build_number, build_id, test_name,
                    classification, error_message)
                   VALUES (?, 'prow-job', 0, '', 'test-e', 'CODE ISSUE', 'err')""",
                ("job-eligible",),
            )
            # Ineligible: Jenkins-style result with no build_id (stays empty forever)
            await db.execute(
                """INSERT INTO results (job_id, status, result_json)
                   VALUES (?, 'completed', ?)""",
                ("job-jenkins", json.dumps({"build_number": 99})),
            )
            await db.execute(
                """INSERT INTO failure_history
                   (job_id, job_name, build_number, build_id, test_name,
                    classification, error_message)
                   VALUES (?, 'jenkins-job', 99, '', 'test-j', 'CODE ISSUE', 'err')""",
                ("job-jenkins",),
            )
            await db.commit()

            await storage._migrate_failure_history_build_id(db)
            await db.commit()

            db.row_factory = aiosqlite.Row
            eligible_row = await (
                await db.execute(
                    "SELECT build_id FROM failure_history WHERE job_id = ?",
                    ("job-eligible",),
                )
            ).fetchone()
            jenkins_row = await (
                await db.execute(
                    "SELECT build_id FROM failure_history WHERE job_id = ?",
                    ("job-jenkins",),
                )
            ).fetchone()
            assert eligible_row is not None and jenkins_row is not None
            assert eligible_row["build_id"] == "12345"
            assert jenkins_row["build_id"] == ""

            # Second run must be a no-op (no eligible empty rows left).
            before = db.total_changes
            await storage._migrate_failure_history_build_id(db)
            await db.commit()
            assert db.total_changes == before


class TestUpdateBuildUrl:
    """Tests for update_build_url()."""

    async def test_url_persisted(self, setup_test_db: Path) -> None:
        """Happy path: build URL is persisted and retrievable."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("job-url", "", "pending", {})
            await storage.update_build_url(
                "job-url", "https://prow.example.com/view/job/42"
            )
            row = await storage.get_result("job-url")
            assert row is not None
            assert row["jenkins_url"] == "https://prow.example.com/view/job/42"

    async def test_empty_string_noop(self, setup_test_db: Path) -> None:
        """Empty string is a no-op — existing URL stays unchanged."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "job-noop", "https://original.url/job/1", "pending", {}
            )
            await storage.update_build_url("job-noop", "")
            row = await storage.get_result("job-noop")
            assert row is not None
            assert row["jenkins_url"] == "https://original.url/job/1"

    async def test_nonexistent_job_logs_warning(self, setup_test_db: Path) -> None:
        """Updating a nonexistent job_id logs a warning."""
        with (
            patch.object(storage, "DB_PATH", setup_test_db),
            patch.object(storage, "logger") as mock_logger,
        ):
            await storage.update_build_url("nonexistent-id", "https://example.com")
        mock_logger.warning.assert_called_once()
        args = mock_logger.warning.call_args[0]
        assert "no row found" in args[0]
        assert "nonexistent-id" in args


class TestSaveTestEntriesChildScope:
    """Child-scoped test_entries persistence and queries."""

    async def test_child_scope_isolated_from_top_level(
        self, setup_test_db: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-child-scope",
                jenkins_url="https://jenkins.example.com/job/pipe/1/",
                status="completed",
            )
            await storage.save_test_entries(
                "job-child-scope",
                [
                    {
                        "test_name": "top.pass",
                        "duration": 0.1,
                        "status": "passed",
                    }
                ],
            )
            await storage.save_test_entries(
                "job-child-scope",
                [
                    {
                        "test_name": "child.pass",
                        "duration": 0.2,
                        "status": "passed",
                    },
                    {
                        "test_name": "child.skip",
                        "duration": 0.0,
                        "status": "skipped",
                    },
                ],
                child_job_name="leaf-child",
                child_build_number=42,
            )

            top = await storage.get_test_entries(
                "job-child-scope",
                status=["passed"],
                child_job_name="",
                child_build_number=0,
            )
            child = await storage.get_test_entries(
                "job-child-scope",
                status=["passed", "skipped"],
                child_job_name="leaf-child",
                child_build_number=42,
            )

            assert top["total"] == 1
            assert top["entries"][0]["test_name"] == "top.pass"
            assert child["total"] == 2
            names = {e["test_name"] for e in child["entries"]}
            assert names == {"child.pass", "child.skip"}

    async def test_empty_save_clears_scope(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-empty-scope",
                jenkins_url="https://jenkins.example.com/job/pipe/1/",
                status="completed",
            )
            await storage.save_test_entries(
                "job-empty-scope",
                [{"test_name": "gone", "duration": 0.1, "status": "passed"}],
            )
            await storage.save_test_entries("job-empty-scope", [])
            remaining = await storage.get_test_entries("job-empty-scope")
            assert remaining["total"] == 0

    async def test_replace_job_test_entries_drops_missing_children(
        self, setup_test_db: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-replace-entries",
                jenkins_url="https://jenkins.example.com/job/pipe/1/",
                status="completed",
            )
            await storage.replace_job_test_entries(
                "job-replace-entries",
                [{"test_name": "top", "duration": 0.1, "status": "passed"}],
                [
                    (
                        "old-child",
                        1,
                        [{"test_name": "c", "duration": 0.0, "status": "failed"}],
                    )
                ],
            )
            await storage.replace_job_test_entries("job-replace-entries", [], [])
            remaining = await storage.get_test_entries("job-replace-entries")
            assert remaining["total"] == 0

    def test_copy_stable_analysis_ids(self) -> None:
        prior = {
            "failures": [
                {
                    "id": "fail-1",
                    "test_name": "t",
                    "error_signature": "sig",
                }
            ],
            "child_job_analyses": [
                {
                    "id": "child-1",
                    "job_name": "leaf",
                    "build_number": 9,
                    "failures": [
                        {
                            "id": "fail-child",
                            "test_name": "ct",
                            "error_signature": "csig",
                        }
                    ],
                }
            ],
        }
        current = {
            "failures": [
                {
                    "id": "new-fail",
                    "test_name": "t",
                    "error_signature": "sig",
                }
            ],
            "child_job_analyses": [
                {
                    "id": "new-child",
                    "job_name": "leaf",
                    "build_number": 9,
                    "failures": [
                        {
                            "id": "new-fail-child",
                            "test_name": "ct",
                            "error_signature": "csig",
                        }
                    ],
                }
            ],
        }
        storage.copy_stable_analysis_ids(prior, current)
        assert current["failures"][0]["id"] == "fail-1"
        assert current["child_job_analyses"][0]["id"] == "child-1"
        assert current["child_job_analyses"][0]["failures"][0]["id"] == "fail-child"


class TestWithBuildUrlAliases:
    """Tests for with_build_url_aliases sanitization."""

    def test_clears_unsafe_urls(self) -> None:
        record = {
            "build_url": "javascript:alert(1)",
            "jenkins_url": "javascript:alert(1)",
        }
        out = storage.with_build_url_aliases(record)
        assert out["build_url"] == ""
        assert out["jenkins_url"] == ""

    def test_prefers_build_url_and_aliases(self) -> None:
        record = {
            "build_url": "https://prow.example.com/job/1",
            "jenkins_url": "https://jenkins.example.com/job/1",
        }
        out = storage.with_build_url_aliases(record)
        assert out["build_url"] == "https://prow.example.com/job/1"
        assert out["jenkins_url"] == "https://prow.example.com/job/1"

    def test_falls_back_to_jenkins_url(self) -> None:
        record = {"jenkins_url": "https://jenkins.example.com/job/1"}
        out = storage.with_build_url_aliases(record)
        assert out["build_url"] == "https://jenkins.example.com/job/1"
        assert out["jenkins_url"] == "https://jenkins.example.com/job/1"


@pytest.mark.anyio
async def test_submit_ingest_persists_failures(setup_test_db):
    from unittest.mock import AsyncMock

    from rootcoz.config import Settings
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import FailedTest, UnifiedAnalyzeRequest
    from rootcoz.sources.base import CISourceResult
    from rootcoz.storage import ANALYSIS_STATE_SUBMITTED

    job_id = "submit-ingest-1"
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result(
            job_id,
            "",
            "pending",
            {"analysis_state": ANALYSIS_STATE_SUBMITTED, "job_name": "raw"},
        )
        body = UnifiedAnalyzeRequest(
            type="raw",
            failures=[FailedTest(test_name="t1", error_message="boom")],
        )
        source_result = CISourceResult(
            failures=list(body.failures),
            skip_analysis=False,
            identity={"job_name": "raw"},
        )
        with patch(
            "rootcoz.sources.raw_source.RawSource.fetch",
            new_callable=AsyncMock,
            return_value=source_result,
        ):
            await _process_ci_source_analysis(
                job_id=job_id,
                body=body,
                merged=Settings(),
                display_name="raw",
                ai_provider="",
                ai_model="",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
                ingest_only=True,
            )
        row = await storage.get_result(job_id)
        assert row is not None
        result = row["result"]
        assert result["analysis_state"] == ANALYSIS_STATE_SUBMITTED
        assert result["failed_count"] == 1
        assert result["failures"][0]["test_name"] == "t1"
        assert row["status"] == "completed"


@pytest.mark.anyio
async def test_submit_ingest_fetch_error_keeps_submitted_state(setup_test_db):
    from unittest.mock import AsyncMock

    from rootcoz.config import Settings
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import FailedTest, UnifiedAnalyzeRequest
    from rootcoz.storage import ANALYSIS_STATE_SUBMITTED

    job_id = "submit-ingest-fail-1"
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result(
            job_id,
            "",
            "pending",
            {"analysis_state": ANALYSIS_STATE_SUBMITTED, "job_name": "raw"},
        )
        body = UnifiedAnalyzeRequest(
            type="raw",
            failures=[FailedTest(test_name="t1", error_message="boom")],
        )
        with patch(
            "rootcoz.sources.raw_source.RawSource.fetch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("fetch boom"),
        ):
            await _process_ci_source_analysis(
                job_id=job_id,
                body=body,
                merged=Settings(),
                display_name="raw",
                ai_provider="",
                ai_model="",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
                ingest_only=True,
            )
        row = await storage.get_result(job_id)
        assert row is not None
        assert row["status"] == "failed"
        assert row["result"]["analysis_state"] == ANALYSIS_STATE_SUBMITTED


@pytest.mark.anyio
async def test_submit_ingest_persists_pipeline_children(setup_test_db):
    from unittest.mock import AsyncMock

    from rootcoz.config import Settings
    from rootcoz.main import _process_ci_source_analysis
    from rootcoz.models import (
        AnalysisDetail,
        ChildJobAnalysis,
        FailureAnalysis,
        UnifiedAnalyzeRequest,
    )
    from rootcoz.sources.base import CISourceResult
    from rootcoz.storage import ANALYSIS_STATE_SUBMITTED

    job_id = "submit-ingest-pipe-1"
    child = ChildJobAnalysis(
        job_name="leaf",
        build_number=2,
        summary="collected",
        failures=[
            FailureAnalysis(
                test_name="pkg.test_fail",
                error="boom",
                analysis=AnalysisDetail(),
            )
        ],
        failed_count=1,
    )
    source_result = CISourceResult(
        failures=[],
        child_job_infos=[("leaf", 2)],
        identity={"job_name": "pipe", "build_number": 1},
    )
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result(
            job_id,
            "",
            "pending",
            {"analysis_state": ANALYSIS_STATE_SUBMITTED, "job_name": "pipe"},
        )
        body = UnifiedAnalyzeRequest(
            type="jenkins",
            job_name="pipe",
            build_number=1,
            jenkins_url="https://jenkins.example.com",
        )
        with (
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.requires_pre_fetch",
                return_value=False,
            ),
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
                new_callable=AsyncMock,
                return_value=source_result,
            ),
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.analyze_children",
                new_callable=AsyncMock,
                return_value=(
                    [child],
                    [
                        (
                            "leaf",
                            2,
                            [
                                {
                                    "test_name": "pkg.test_fail",
                                    "duration": 0.5,
                                    "status": "failed",
                                }
                            ],
                        )
                    ],
                ),
            ) as mock_children,
        ):
            await _process_ci_source_analysis(
                job_id=job_id,
                body=body,
                merged=Settings(
                    jenkins_url="https://jenkins.example.com",
                    jenkins_user="u",
                    jenkins_password="p",
                ),
                display_name="pipe",
                ai_provider="",
                ai_model="",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
                ingest_only=True,
            )
        mock_children.assert_awaited()
        assert mock_children.await_args.kwargs["ingest_only"] is True
        row = await storage.get_result(job_id)
        assert row is not None
        result = row["result"]
        assert result["analysis_state"] == ANALYSIS_STATE_SUBMITTED
        assert result["failed_count"] == 1
        assert result["child_job_analyses"][0]["job_name"] == "leaf"
        assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_partial_update_status_preserves_build_id(setup_test_db):
    """Partial update_status must not clobber existing denormalized fields."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        # Save with all identity fields
        await storage.save_result(
            job_id="preserve-test",
            status="running",
            result={"job_name": "my-job", "build_number": 42, "build_id": "999"},
        )

        # Update with partial result (no build_id key)
        await storage.update_status(
            "preserve-test", "failed", result={"job_name": "my-job", "error": "boom"}
        )

        # Verify build_id was preserved
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT job_name, build_number, build_id FROM results WHERE job_id = ?",
                ("preserve-test",),
            )
            row = await cursor.fetchone()
            assert row["job_name"] == "my-job"
            assert row["build_number"] == 42
            assert row["build_id"] == "999", "build_id was clobbered by partial update"


class TestExtractDenormalizedFields:
    """Tests for _extract_denormalized_fields helper."""

    def test_none_input(self):
        from rootcoz.storage import _extract_denormalized_fields

        assert _extract_denormalized_fields(None) == (
            "",
            0,
            "",
            "",
            "",
            "",
            "",
        )

    def test_empty_dict(self):
        from rootcoz.storage import _extract_denormalized_fields

        assert _extract_denormalized_fields({}) == ("", 0, "", "", "", "", "")

    def test_valid_fields(self):
        from rootcoz.storage import _extract_denormalized_fields

        result = {
            "job_name": "my-job",
            "build_number": 42,
            "build_id": "999",
            "request_params": {"submitted_by": "  yuval  "},
            "summary": "two failures",
            "display_name": "Human Name",
            "failures": [{"id": "f-1"}, {"id": "f-2"}],
            "child_job_analyses": [
                {"id": "c-1", "failures": [{"id": "f-3"}]},
                {"failed_children": [{"failures": [{"id": "f-4"}]}]},
            ],
        }
        assert _extract_denormalized_fields(result) == (
            "my-job",
            42,
            "999",
            "yuval",
            "two failures",
            "Human Name",
            "f-1 f-2 c-1 f-3 f-4",
        )

    def test_failure_ids_are_unique_and_ordered(self):
        from rootcoz.storage import _extract_denormalized_fields

        result = {"failures": [{"id": "f-1"}, {"id": "f-1"}, {"no_id": 1}]}
        assert _extract_denormalized_fields(result)[6] == "f-1"

    def test_invalid_build_number(self):
        from rootcoz.storage import _extract_denormalized_fields

        result = {"job_name": "x", "build_number": "not-a-number", "build_id": "1"}
        assert _extract_denormalized_fields(result)[:3] == ("x", 0, "1")

    def test_numeric_build_id_coerced_to_string(self):
        from rootcoz.storage import _extract_denormalized_fields

        result = {"job_name": "x", "build_number": 1, "build_id": 12345}
        assert _extract_denormalized_fields(result)[:3] == ("x", 1, "12345")

    def test_missing_fields_default(self):
        from rootcoz.storage import _extract_denormalized_fields

        result = {"other_key": "value"}
        assert _extract_denormalized_fields(result) == ("", 0, "", "", "", "", "")


@pytest.mark.asyncio
async def test_save_result_populates_denorm_columns(setup_test_db):
    """save_result writes denormalized job_name/build_number/build_id."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result(
            job_id="denorm-test",
            status="completed",
            result={"job_name": "test-job", "build_number": 7, "build_id": "abc"},
        )
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT job_name, build_number, build_id FROM results WHERE job_id = ?",
                ("denorm-test",),
            )
            row = await cursor.fetchone()
            assert row["job_name"] == "test-job"
            assert row["build_number"] == 7
            assert row["build_id"] == "abc"


@pytest.mark.asyncio
async def test_set_job_metadata_syncs_labels(setup_test_db):
    """set_job_metadata inserts into job_metadata_labels."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.set_job_metadata("sync-job", labels=["smoke", "nightly"])
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT label FROM job_metadata_labels WHERE job_name = ? ORDER BY label",
                ("sync-job",),
            )
            labels = [row["label"] for row in await cursor.fetchall()]
            assert labels == ["nightly", "smoke"]


@pytest.mark.asyncio
async def test_delete_job_metadata_cleans_labels(setup_test_db):
    """delete_job_metadata removes job_metadata_labels rows."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.set_job_metadata("del-job", labels=["a", "b"])
        await storage.delete_job_metadata("del-job")
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM job_metadata_labels WHERE job_name = ?",
                ("del-job",),
            )
            count = (await cursor.fetchone())[0]
            assert count == 0


class TestReanalysisForwardLinkPreserved:
    """update_status must not erase a forward link appended while the job ran."""

    async def test_completion_write_keeps_newer_forward_links(
        self, setup_test_db: Path
    ) -> None:
        """A finishing job's stale request_params cannot drop the stored links."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="origin-running",
                status="running",
                result={"request_params": {"ai_provider": "claude"}},
            )

            def _append(data: dict) -> None:
                params = data["request_params"]
                params["reanalyzed_to_job_ids"] = ["re-1", "re-2"]

            await storage.patch_result_json("origin-running", _append)

            # Completion write carries the params read *before* the append.
            await storage.update_status(
                "origin-running",
                "completed",
                {"summary": "done", "request_params": {"ai_provider": "claude"}},
            )
            stored = await storage.get_result("origin-running")
            assert stored["result"]["request_params"]["reanalyzed_to_job_ids"] == [
                "re-1",
                "re-2",
            ]

    async def test_completion_write_keeps_link_removal(
        self, setup_test_db: Path
    ) -> None:
        """A removed forward link is not resurrected by a stale completion write."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="origin-deleted",
                status="running",
                result={
                    "request_params": {
                        "reanalyzed_to_job_ids": ["re-1", "re-2"],
                    }
                },
            )

            def _drop(data: dict) -> None:
                params = data["request_params"]
                params["reanalyzed_to_job_ids"] = ["re-1"]

            await storage.patch_result_json("origin-deleted", _drop)
            await storage.update_status(
                "origin-deleted",
                "completed",
                {
                    "summary": "done",
                    "request_params": {"reanalyzed_to_job_ids": ["re-1", "re-2"]},
                },
            )
            stored = await storage.get_result("origin-deleted")
            assert stored["result"]["request_params"]["reanalyzed_to_job_ids"] == [
                "re-1"
            ]

    async def test_save_merge_keeps_newer_forward_links(
        self, setup_test_db: Path
    ) -> None:
        """save_result merges the stored links inside its own transaction."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="origin-inplace",
                status="running",
                result={
                    "request_params": {"reanalyzed_to_job_ids": ["re-1"]},
                },
            )
            # The caller read the job, then a concurrent re-analysis appended.
            stale = await storage.get_result("origin-inplace")
            await storage.patch_result_json(
                "origin-inplace",
                lambda data: data["request_params"]["reanalyzed_to_job_ids"].append(
                    "re-2"
                ),
            )

            await storage.save_result(
                job_id="origin-inplace",
                status="running",
                result=stale["result"],
            )
            stored = await storage.get_result("origin-inplace")
            assert stored["result"]["request_params"]["reanalyzed_to_job_ids"] == [
                "re-1",
                "re-2",
            ]

    async def test_concurrent_save_and_append_keep_both_links(
        self, setup_test_db: Path
    ) -> None:
        """Racing save_result and append writes converge on one full link list."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="origin-race",
                status="running",
                result={"request_params": {"reanalyzed_to_job_ids": ["re-1"]}},
            )
            stale = await storage.get_result("origin-race")

            async def _append() -> None:
                await asyncio.sleep(0)
                await storage.patch_result_json(
                    "origin-race",
                    lambda data: data["request_params"]["reanalyzed_to_job_ids"].append(
                        "re-2"
                    ),
                )

            async def _save() -> None:
                await asyncio.sleep(0)
                await storage.save_result(
                    job_id="origin-race",
                    status="running",
                    result=stale["result"],
                )

            await asyncio.gather(_save(), _append())
            stored = await storage.get_result("origin-race")
            assert stored["result"]["request_params"]["reanalyzed_to_job_ids"] == [
                "re-1",
                "re-2",
            ]

    async def test_patch_skipped_when_required_job_is_missing(
        self, setup_test_db: Path
    ) -> None:
        """require_job_id makes a link patch conditional on the job existing."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="origin-guard",
                status="running",
                result={"request_params": {"reanalyzed_to_job_ids": []}},
            )

            def _append(data: dict) -> None:
                data["request_params"]["reanalyzed_to_job_ids"].append("re-1")

            # Job still exists -> the link is recorded.
            await storage.save_result(
                job_id="re-1",
                status="running",
                result={"request_params": {}},
            )
            await storage.patch_result_json(
                "origin-guard", _append, require_job_id="re-1"
            )
            stored = await storage.get_result("origin-guard")
            assert stored["result"]["request_params"]["reanalyzed_to_job_ids"] == [
                "re-1"
            ]

            # Job already deleted -> no dead link is created.
            await storage.delete_job("re-1")
            await storage.patch_result_json(
                "origin-guard", _append, require_job_id="re-1"
            )
            stored = await storage.get_result("origin-guard")
            assert stored["result"]["request_params"]["reanalyzed_to_job_ids"] == [
                "re-1"
            ]


class TestPatchResultJsonKeyPresence:
    """Regression: patch_result_json must not clobber denorm columns on partial patches."""

    async def test_partial_patch_preserves_denorm_columns(
        self, setup_test_db: Path
    ) -> None:
        """Patching non-identity keys (tags) leaves job_name/build_number/build_id unchanged."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="patch-preserve",
                status="completed",
                result={
                    "job_name": "original-job",
                    "build_number": 42,
                    "build_id": "bid-1",
                    "tags": ["old"],
                },
            )

            def _set_tags(result_data: dict) -> None:
                result_data["tags"] = ["new-tag"]

            await storage.patch_result_json("patch-preserve", _set_tags)

            async with storage._connect_db() as db:
                cursor = await db.execute(
                    "SELECT job_name, build_number, build_id, result_json "
                    "FROM results WHERE job_id = ?",
                    ("patch-preserve",),
                )
                row = await cursor.fetchone()
            assert row["job_name"] == "original-job"
            assert row["build_number"] == 42
            assert row["build_id"] == "bid-1"
            parsed = storage.parse_result_json(row["result_json"])
            assert parsed is not None
            assert parsed["tags"] == ["new-tag"]
            assert parsed["job_name"] == "original-job"

    async def test_patch_updating_job_name_updates_denorm_column(
        self, setup_test_db: Path
    ) -> None:
        """When patch_fn modifies job_name, the denormalized column is updated."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="patch-rename",
                status="completed",
                result={
                    "job_name": "old-name",
                    "build_number": 7,
                    "build_id": "bid-2",
                },
            )

            def _rename(result_data: dict) -> None:
                result_data["job_name"] = "new-name"

            await storage.patch_result_json("patch-rename", _rename)

            async with storage._connect_db() as db:
                cursor = await db.execute(
                    "SELECT job_name, build_number, build_id FROM results WHERE job_id = ?",
                    ("patch-rename",),
                )
                row = await cursor.fetchone()
            assert row["job_name"] == "new-name"
            assert row["build_number"] == 7
            assert row["build_id"] == "bid-2"


class TestCoerceSqliteBuildNumber:
    """Unit tests for _coerce_sqlite_build_number edge cases."""

    def test_numeric_string(self) -> None:
        from rootcoz.storage import _coerce_sqlite_build_number

        assert _coerce_sqlite_build_number("123") == 123

    def test_non_numeric_string(self) -> None:
        from rootcoz.storage import _coerce_sqlite_build_number

        assert _coerce_sqlite_build_number("abc") == 0

    def test_negative_int(self) -> None:
        from rootcoz.storage import _coerce_sqlite_build_number

        assert _coerce_sqlite_build_number(-5) == 0

    def test_over_sqlite_int_max(self) -> None:
        from rootcoz.storage import SQLITE_INT_MAX, _coerce_sqlite_build_number

        assert _coerce_sqlite_build_number(SQLITE_INT_MAX + 1) == 0

    def test_none(self) -> None:
        from rootcoz.storage import _coerce_sqlite_build_number

        assert _coerce_sqlite_build_number(None) == 0


class TestMigrationsAppliedSchema:
    """Regression: _migrations_applied must include applied_at."""

    async def test_migrations_applied_has_applied_at_column(
        self, temp_db_path: Path
    ) -> None:
        """After init_db(), _migrations_applied has both key and applied_at."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute("PRAGMA table_info(_migrations_applied)")
                columns = {row[1] for row in await cursor.fetchall()}
            assert "key" in columns
            assert "applied_at" in columns

    async def test_legacy_migrations_applied_gets_applied_at(
        self, temp_db_path: Path
    ) -> None:
        """Legacy DBs with only 'key' column get applied_at added."""
        # Create legacy table without applied_at
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE _migrations_applied (key TEXT PRIMARY KEY)")
            await db.commit()
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute("PRAGMA table_info(_migrations_applied)")
                columns = {row[1] for row in await cursor.fetchall()}
            assert "key" in columns
            assert "applied_at" in columns


class TestJiraOauthSecretRenameMigration:
    """One-time copy of jira_oauth_access_token → jira_oauth_client_secret."""

    @staticmethod
    async def _insert_setting(db_path: Path, key: str, value: str) -> None:
        """Seed a server_settings row before init_db runs its migrations."""
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS server_settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "updated_by TEXT NOT NULL DEFAULT '', "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            await db.execute(
                "INSERT INTO server_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            await db.commit()

    async def test_old_key_row_copied_to_new_key(self, temp_db_path: Path) -> None:
        """An old-key row is copied to the new key; the old row is kept."""
        await self._insert_setting(
            temp_db_path, "jira_oauth_access_token", "enc:old-value"
        )
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            new = await storage.get_server_setting("jira_oauth_client_secret")
            old = await storage.get_server_setting("jira_oauth_access_token")
        assert new is not None
        assert new["value"] == "enc:old-value"
        # The old row is deliberately NOT deleted — the new key wins once set
        assert old is not None
        assert old["value"] == "enc:old-value"

    async def test_new_key_set_old_row_ignored(self, temp_db_path: Path) -> None:
        """When the new key already has a row, the old row is not copied over it."""
        await self._insert_setting(
            temp_db_path, "jira_oauth_access_token", "enc:old-value"
        )
        await self._insert_setting(
            temp_db_path, "jira_oauth_client_secret", "enc:new-value"
        )
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            new = await storage.get_server_setting("jira_oauth_client_secret")
        assert new is not None
        assert new["value"] == "enc:new-value"

    async def test_migration_runs_only_once(self, temp_db_path: Path) -> None:
        """After the one-time migration, a deleted new-key row stays deleted."""
        await self._insert_setting(
            temp_db_path, "jira_oauth_access_token", "enc:old-value"
        )
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.delete_server_setting("jira_oauth_client_secret")
            # A later boot must not resurrect the new key from the old row
            await storage.init_db()
            resurrected = await storage.get_server_setting("jira_oauth_client_secret")
        assert resurrected is None

    async def test_no_old_row_no_new_row(self, temp_db_path: Path) -> None:
        """Without an old-key row, nothing is created under the new key."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            new = await storage.get_server_setting("jira_oauth_client_secret")
        assert new is None


class TestServerSettingsBatchWrite:
    """Atomic multi-key server settings writes (settings import, #361)."""

    async def test_batch_write_is_atomic_on_mid_batch_failure(
        self, setup_test_db: Path
    ) -> None:
        """A write that fails mid-batch leaves nothing applied."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_server_settings_batch(
                {"ai_call_timeout": "25", "log_level": "DEBUG"}, updated_by="admin"
            )
            original_connect = storage._connect_db

            @asynccontextmanager
            async def failing_connect():
                async with original_connect() as db:
                    real_execute = db.execute

                    async def execute(sql, parameters=()):
                        # Fail on the second key's very first statement
                        if parameters and parameters[0] == "log_level":
                            raise RuntimeError("boom")
                        return await real_execute(sql, parameters)

                    db.execute = execute
                    yield db

            with (
                patch.object(storage, "_connect_db", failing_connect),
                pytest.raises(RuntimeError, match="boom"),
            ):
                await storage.set_server_settings_batch(
                    {"jenkins_url": "https://x", "log_level": "INFO"},
                    updated_by="admin",
                )

            # Neither key of the failed batch was applied (no commit →
            # rollback); the earlier successful batch is untouched.
            settings = await storage.get_server_settings()
            assert set(settings) == {"ai_call_timeout", "log_level"}
            assert settings["log_level"]["value"] == "DEBUG"

            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM server_settings_history "
                    "WHERE key IN ('jenkins_url', 'log_level') AND value = 'INFO'"
                )
                assert await cursor.fetchone() == (0,)


class TestJiraUrlPerMethodSplitMigration:
    """One-time split of the shared jira_url/jira_ssl_verify settings per method."""

    @staticmethod
    async def _insert_setting(db_path: Path, key: str, value: str) -> None:
        """Seed a server_settings row before init_db runs its migrations."""
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS server_settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "updated_by TEXT NOT NULL DEFAULT '', "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            await db.execute(
                "INSERT INTO server_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            await db.commit()

    async def test_jira_url_copied_to_both_per_method_urls(
        self, temp_db_path: Path
    ) -> None:
        """A jira_url row is copied to jira_cloud_url AND jira_server_url."""
        await self._insert_setting(temp_db_path, "jira_url", "https://jira.example.com")
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            cloud = await storage.get_server_setting("jira_cloud_url")
            server = await storage.get_server_setting("jira_server_url")
            old = await storage.get_server_setting("jira_url")
        assert cloud is not None and cloud["value"] == "https://jira.example.com"
        assert server is not None and server["value"] == "https://jira.example.com"
        # The old row is deliberately NOT deleted — new keys win once set
        assert old is not None and old["value"] == "https://jira.example.com"

    async def test_jira_ssl_verify_copied_to_all_three_flags(
        self, temp_db_path: Path
    ) -> None:
        """A jira_ssl_verify row is copied to all three per-method flags."""
        await self._insert_setting(temp_db_path, "jira_ssl_verify", "false")
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            flags = [
                await storage.get_server_setting(key)
                for key in (
                    "jira_oauth_ssl_verify",
                    "jira_cloud_ssl_verify",
                    "jira_server_ssl_verify",
                )
            ]
            old = await storage.get_server_setting("jira_ssl_verify")
        assert all(f is not None and f["value"] == "false" for f in flags)
        assert old is not None and old["value"] == "false"

    async def test_new_keys_win_over_old_rows(self, temp_db_path: Path) -> None:
        """Existing per-method rows are never overwritten by the old shared rows."""
        await self._insert_setting(
            temp_db_path, "jira_url", "https://shared.example.com"
        )
        await self._insert_setting(
            temp_db_path, "jira_cloud_url", "https://cloud.example.com"
        )
        await self._insert_setting(
            temp_db_path, "jira_server_url", "https://server.example.com"
        )
        await self._insert_setting(temp_db_path, "jira_ssl_verify", "true")
        await self._insert_setting(temp_db_path, "jira_server_ssl_verify", "false")
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            cloud = await storage.get_server_setting("jira_cloud_url")
            server = await storage.get_server_setting("jira_server_url")
            oauth_flag = await storage.get_server_setting("jira_oauth_ssl_verify")
            server_flag = await storage.get_server_setting("jira_server_ssl_verify")
        assert cloud is not None and cloud["value"] == "https://cloud.example.com"
        assert server is not None and server["value"] == "https://server.example.com"
        # jira_ssl_verify=true still fills the unset oauth/cloud flags…
        assert oauth_flag is not None and oauth_flag["value"] == "true"
        # …but never an existing per-method row
        assert server_flag is not None and server_flag["value"] == "false"

    async def test_migration_runs_only_once(self, temp_db_path: Path) -> None:
        """After the one-time migration, a deleted new-key row stays deleted."""
        await self._insert_setting(temp_db_path, "jira_url", "https://jira.example.com")
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.delete_server_setting("jira_cloud_url")
            # A later boot must not resurrect the new key from the old row
            await storage.init_db()
            resurrected = await storage.get_server_setting("jira_cloud_url")
        assert resurrected is None

    async def test_no_old_rows_no_new_rows(self, temp_db_path: Path) -> None:
        """Without old shared rows, nothing is created under the new keys."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            for key in (
                "jira_cloud_url",
                "jira_server_url",
                "jira_oauth_ssl_verify",
                "jira_cloud_ssl_verify",
                "jira_server_ssl_verify",
            ):
                assert await storage.get_server_setting(key) is None


# A zstd frame header followed by bytes that never inflate: damage, the state a
# truncated or corrupt stored row is in. 0xB5 in position 1 is the byte that used
# to blow up json.loads in a downgrade.
_DAMAGED_FRAME = b"\x28\xb5\x2f\xfd" + b"\x00\x01\x02\x03rest-of-frame"


@pytest.fixture
def storage_logs(caplog: pytest.LogCaptureFixture) -> Any:
    """Attach caplog to the storage logger (simple_logger sets propagate=False)."""
    storage.logger.setLevel(logging.DEBUG)
    storage.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        storage.logger.removeHandler(caplog.handler)


def _one_failure_result(
    job_name: str,
    *,
    pattern: str | None = None,
    classification: str = "PRODUCT BUG",
    children: list[dict[str, Any]] | None = None,
    build_number: int = 1,
    build_id: str = "",
) -> str:
    """Return a valid result_json payload carrying exactly one top-level failure.

    ``pattern`` adds that key to the failure's ``analysis``, ``classification``
    replaces the default AI value, ``children`` fills ``child_job_analyses``
    (each child carries its own failures), and ``build_number``/``build_id``
    replace the defaults -- ``build_id=''`` still derives the id from the build
    number, as ``_resolve_history_build_fields`` does.
    """
    analysis: dict[str, Any] = {"classification": classification, "details": "..."}
    if pattern is not None:
        analysis["pattern"] = pattern
    return json.dumps(
        {
            "job_name": job_name,
            "build_number": build_number,
            "build_id": build_id,
            "failures": [
                {
                    "test_name": f"{job_name}.test_a",
                    "error": "boom",
                    "error_signature": f"sig-{job_name}",
                    "analysis": analysis,
                }
            ],
            "child_job_analyses": children or [],
        }
    )


async def _insert_completed_result(
    db_path: Path, job_id: str, result_json: str | bytes
) -> None:
    """Insert a completed results row verbatim (bytes land as a BLOB)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO results (job_id, build_url, status, result_json) "
            "VALUES (?, ?, 'completed', ?)",
            (job_id, "https://jenkins.example.test/job/x/1/", result_json),
        )
        await db.commit()


async def _raw_result_json(db_path: Path, job_id: str) -> Any:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT result_json FROM results WHERE job_id = ?", (job_id,)
        )
        return (await cursor.fetchone())[0]


async def _failure_history_job_ids(db_path: Path) -> set[str]:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT DISTINCT job_id FROM failure_history")
        return {row[0] for row in await cursor.fetchall()}


async def _failure_history_row_ids(db_path: Path) -> dict[str, list[int]]:
    """Map job_id -> failure_history row ids.

    A repopulated row is deleted and re-inserted, so its AUTOINCREMENT id
    changes: equal ids across two runs prove no write happened.
    """
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT job_id, id FROM failure_history ORDER BY job_id, id"
        )
        rows: dict[str, list[int]] = {}
        for job_id, row_id in await cursor.fetchall():
            rows.setdefault(job_id, []).append(row_id)
        return rows


async def _seed_mirrored_override(
    db_path: Path, job_id: str, *, classification: str = "CODE ISSUE"
) -> None:
    """Seed the state the restore-AI migration repairs.

    failure_history holds a user override the old mirroring code copied in,
    while result_json still carries the original AI classification
    (``PRODUCT BUG``, see ``_one_failure_result``).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO failure_history (job_id, job_name, build_number, "
            "test_name, classification, error_message, analyzed_at) "
            "VALUES (?, ?, 1, ?, ?, 'e', datetime('now'))",
            (job_id, job_id, f"{job_id}.test_a", classification),
        )
        await db.execute(
            "INSERT INTO test_classifications "
            "(test_name, job_name, classification, created_by, job_id) "
            "VALUES (?, ?, ?, 'user', ?)",
            (f"{job_id}.test_a", job_id, classification, job_id),
        )
        await db.commit()


async def _failure_history_classifications(db_path: Path, job_id: str) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT classification FROM failure_history WHERE job_id = ?", (job_id,)
        )
        return [row[0] for row in await cursor.fetchall()]


async def _failure_history_patterns(db_path: Path) -> dict[str, list[str]]:
    """Map job_id -> the pattern of each of its failure_history rows."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT job_id, pattern FROM failure_history ORDER BY job_id, id"
        )
        rows: dict[str, list[str]] = {}
        for job_id, pattern in await cursor.fetchall():
            rows.setdefault(job_id, []).append(pattern)
        return rows


async def _set_failure_history_column(
    db_path: Path, job_id: str, column: str, value: Any
) -> None:
    """Write one failure_history column directly, bypassing the writer."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE failure_history SET {column} = ? WHERE job_id = ?",
            (value, job_id),
        )
        await db.commit()


async def _migration_recorded(db_path: Path, key: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM _migrations_applied WHERE key = ?", (key,)
        )
        return await cursor.fetchone() is not None


class TestUnreadableResultJson:
    """A result_json this build cannot decode must not abort startup."""

    def test_frame_raises_actionable_error(self) -> None:
        """A frame our own codec refuses is damage, and says so."""
        with pytest.raises(storage.DamagedResultEncoding) as excinfo:
            storage.parse_result_json(_DAMAGED_FRAME, job_id="frame-1")
        message = str(excinfo.value)
        assert "frame-1" in message
        assert "zstd frame" in message
        assert "result_json compression" in message
        assert "truncated or corrupt" in message
        assert "restore a pre-compression copy of the database" in message
        # Damage is not an encoding this build lacks: those get their own error.
        assert not isinstance(excinfo.value, storage.UnsupportedResultEncoding)

    def test_non_utf8_bytes_also_raise(self) -> None:
        """Bytes that are neither text nor a frame are an encoding problem too."""
        with pytest.raises(storage.UnsupportedResultEncoding, match="not UTF-8 text"):
            storage.parse_result_json(b"\xff\xfe\x00not text", job_id="blob-1")

    def test_plain_text_still_parses(self) -> None:
        """A text row is unaffected by the encoding guard."""
        parsed = storage.parse_result_json(_one_failure_result("job-a"), job_id="a")
        assert parsed is not None
        assert parsed["job_name"] == "job-a"

    async def test_init_db_survives_zstd_framed_row(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """init_db completes, counts the row, names the cause, leaves it alone."""
        storage._undecodable_result_ids.clear()
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await _insert_completed_result(temp_db_path, "frame-1", _DAMAGED_FRAME)

            await storage.init_db()  # must not raise

            assert await _raw_result_json(temp_db_path, "frame-1") == _DAMAGED_FRAME
            assert await _failure_history_job_ids(temp_db_path) == set()
            # Reads degrade to an empty result instead of failing.
            detail = await storage.get_result("frame-1")
            assert detail is not None
            assert detail["result"] is None
            # Degraded data is counted for /api/health, which stays 200.
            assert storage.undecodable_result_count() == 1

        assert "Backfill skipped 1 result(s)" in storage_logs.text
        assert "result_json compression" in storage_logs.text
        assert "restore a pre-compression copy of the database" in storage_logs.text
        # A frame our own codec refuses is reported as damage, not as a missing
        # encoding: the row is not intact and upgrading rootcoz will not fix it.
        assert "truncated or corrupt" in storage_logs.text
        assert "The row is intact" not in storage_logs.text

    async def test_backfill_processes_good_rows_and_counts_bad_ones(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """Two frames + three good rows: the good three are backfilled, count is 2."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            for index in range(2):
                await _insert_completed_result(
                    temp_db_path, f"frame-{index}", _DAMAGED_FRAME
                )
            for index in range(3):
                await _insert_completed_result(
                    temp_db_path, f"good-{index}", _one_failure_result(f"job-{index}")
                )

            await storage.backfill_failure_history()

            assert await _failure_history_job_ids(temp_db_path) == {
                "good-0",
                "good-1",
                "good-2",
            }
            # The frames must survive untouched, and reads degrade, not raise.
            for index in range(2):
                assert (
                    await _raw_result_json(temp_db_path, f"frame-{index}")
                    == _DAMAGED_FRAME
                )
                detail = await storage.get_result(f"frame-{index}")
                assert detail is not None
                assert detail["result"] is None

        assert "Backfill skipped 2 result(s)" in storage_logs.text

    async def test_non_json_row_reported_separately_from_encoding(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """Corruption keeps its own counter and message."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await _insert_completed_result(temp_db_path, "frame-1", _DAMAGED_FRAME)
            await _insert_completed_result(temp_db_path, "corrupt-1", b"not json")

            await storage.backfill_failure_history()

            assert await _failure_history_job_ids(temp_db_path) == set()

        text = storage_logs.text
        assert "Backfill skipped 1 result(s) whose result_json decoded" in text
        assert "not a JSON object" in text
        assert "Backfill skipped 1 result(s) whose result_json was written" in text

    async def test_genuine_parse_error_is_not_swallowed(
        self, temp_db_path: Path
    ) -> None:
        """Only undecodable payloads are skipped; a parsing bug still raises."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await _insert_completed_result(
                temp_db_path, "good-1", _one_failure_result("job-1")
            )
            with (
                patch.object(
                    storage, "parse_result_json", side_effect=ValueError("genuine bug")
                ),
                pytest.raises(ValueError, match="genuine bug"),
            ):
                await storage.backfill_failure_history()

            assert await _failure_history_job_ids(temp_db_path) == set()

    async def test_waiting_job_with_damaged_frame_is_failed(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """A damaged waiting payload can never be resumed, so the job is failed.

        Damage is terminal -- no codec upgrade inflates these bytes -- so leaving
        the row 'waiting' stranded it forever with no task and no failure.
        """
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "INSERT INTO results (job_id, build_url, status, result_json) "
                    "VALUES (?, ?, 'waiting', ?)",
                    ("waiting-frame", "http://j/1", _DAMAGED_FRAME),
                )
                await db.commit()

            waiting, recovered = await storage.mark_stale_results_failed()

            assert waiting == []
            assert recovered == []
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT status, error, completed_at, result_json FROM results "
                    "WHERE job_id = ?",
                    ("waiting-frame",),
                )
                row = await cursor.fetchone()
            # Failed, with the damage named as the reason, payload untouched.
            assert row[0] == "failed"
            assert "truncated or corrupt" in row[1]
            assert row[1] != storage.RESTART_ERROR_MSG
            assert row[2] is not None
            assert row[3] == _DAMAGED_FRAME

        assert "Startup skipped job_id=waiting-frame" in storage_logs.text
        assert (
            "Marking waiting job waiting-frame as failed: its result_json is damaged"
            in storage_logs.text
        )

    async def test_waiting_job_with_newer_encoding_stays_waiting(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """An intact payload from a newer build is left waiting, untouched.

        A build with the result_json codec can still resume it, so this one must
        not fail the job or write anything into the row.
        """
        blob = b"\xff\xfe\x00a payload from a newer build"
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "INSERT INTO results (job_id, build_url, status, result_json) "
                    "VALUES (?, ?, 'waiting', ?)",
                    ("waiting-newer", "http://j/2", blob),
                )
                await db.commit()

            waiting, recovered = await storage.mark_stale_results_failed()

            assert waiting == []
            assert recovered == []
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT status, error, completed_at, result_json FROM results "
                    "WHERE job_id = ?",
                    ("waiting-newer",),
                )
                row = await cursor.fetchone()
            assert row[0] == "waiting"
            assert row[1] == ""
            assert row[2] is None
            assert row[3] == blob

        assert "Startup skipped job_id=waiting-newer" in storage_logs.text
        assert "Marking waiting job waiting-newer" not in storage_logs.text

    async def test_unknown_encoding_not_blamed_on_compression(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """A non-UTF-8 blob gets the neutral diagnosis, counted, not zstd."""
        blob = b"\xff\xfe\x00not text"
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await _insert_completed_result(temp_db_path, "blob-1", blob)

            await storage.backfill_failure_history()

            assert await _raw_result_json(temp_db_path, "blob-1") == blob
            assert storage.undecodable_result_count() == 1

        text = storage_logs.text
        assert "Backfill skipped 1 result(s) whose result_json uses an encoding" in text
        assert "a compressed frame or a newer encoding" in text
        assert "written by a build with result_json compression" not in text

    async def test_each_skipped_row_logged_with_its_job_id(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """Per-row warnings name every job; the aggregate stays a summary."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            for index in range(2):
                await _insert_completed_result(
                    temp_db_path, f"frame-{index}", _DAMAGED_FRAME
                )

            await storage.backfill_failure_history()

            assert storage.undecodable_result_count() == 2

        text = storage_logs.text
        assert "Startup skipped job_id=frame-0" in text
        assert "Startup skipped job_id=frame-1" in text
        assert "Backfill skipped 2 result(s)" in text

    async def test_pattern_axis_migration_retries_while_row_undecodable(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """A pattern label on an undecodable row keeps the migration unapplied."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                # Pretend this build is running before the migration shipped.
                await db.execute(
                    "DELETE FROM _migrations_applied "
                    "WHERE key = 'backfill_pattern_axis_v1'"
                )
                await db.execute(
                    "INSERT INTO failure_history (job_id, job_name, build_number, "
                    "test_name, classification, error_message, analyzed_at) "
                    "VALUES ('frame-1', 'j', 1, 't', 'REGRESSION', 'e', "
                    "datetime('now'))"
                )
                await db.commit()
            await _insert_completed_result(temp_db_path, "frame-1", _DAMAGED_FRAME)

            await storage._migrate_backfill_pattern_axis()

            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT 1 FROM _migrations_applied "
                    "WHERE key = 'backfill_pattern_axis_v1'"
                )
                assert await cursor.fetchone() is None
                cursor = await db.execute(
                    "SELECT classification FROM failure_history "
                    "WHERE job_id = 'frame-1'"
                )
                # Still the pattern label: not restored, so not recorded as done.
                assert (await cursor.fetchone())[0] == "REGRESSION"

        assert "pattern axis backfill skipped 1 job(s)" in storage_logs.text

    async def test_restore_ai_migration_retries_while_row_undecodable(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """The AI-classification restore is not recorded done with rows skipped."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "DELETE FROM _migrations_applied "
                    "WHERE key = 'restore_ai_classifications_v1'"
                )
                await db.execute(
                    "INSERT INTO failure_history (job_id, job_name, build_number, "
                    "test_name, classification, error_message, analyzed_at) "
                    "VALUES ('frame-1', 'j', 1, 't', 'CODE ISSUE', 'e', "
                    "datetime('now'))"
                )
                await db.execute(
                    "INSERT INTO test_classifications "
                    "(test_name, job_name, classification, created_by, job_id) "
                    "VALUES ('t', 'j', 'CODE ISSUE', 'user', 'frame-1')"
                )
                await db.commit()
            await _insert_completed_result(temp_db_path, "frame-1", _DAMAGED_FRAME)

            await storage._migrate_restore_ai_classifications()

            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT 1 FROM _migrations_applied "
                    "WHERE key = 'restore_ai_classifications_v1'"
                )
                assert await cursor.fetchone() is None

        assert "Migration: 1 job(s) skipped (frame-1)" in storage_logs.text

    async def test_restore_ai_migration_rewrites_readable_history_once(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """Restarts skip restored jobs and retry only the unreadable one."""
        key = "restore_ai_classifications_v1"
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "DELETE FROM _migrations_applied WHERE key = ?", (key,)
                )
                await db.commit()
            for index in range(3):
                job_id = f"good-{index}"
                await _insert_completed_result(
                    temp_db_path, job_id, _one_failure_result(job_id)
                )
                await _seed_mirrored_override(temp_db_path, job_id)
            await _insert_completed_result(temp_db_path, "frame-1", _DAMAGED_FRAME)
            await _seed_mirrored_override(temp_db_path, "frame-1")

            # Run 1: the readable jobs are restored, the frame stays deferred.
            await storage._migrate_restore_ai_classifications()
            after_first = await _failure_history_row_ids(temp_db_path)
            for index in range(3):
                assert await _failure_history_classifications(
                    temp_db_path, f"good-{index}"
                ) == ["PRODUCT BUG"]
            assert "frame-1" in after_first
            assert await _migration_recorded(temp_db_path, key) is False

            # Runs 2 and 3: no row is deleted/re-inserted, still pending.
            for _ in range(2):
                await storage._migrate_restore_ai_classifications()
                assert await _failure_history_row_ids(temp_db_path) == after_first
                assert await _migration_recorded(temp_db_path, key) is False
                assert storage.undecodable_result_count() == 1
            assert await _failure_history_classifications(temp_db_path, "frame-1") == [
                "CODE ISSUE"
            ]

            # A build with the codec reads the row: the next run finishes it.
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "UPDATE results SET result_json = ? WHERE job_id = 'frame-1'",
                    (_one_failure_result("frame-1"),),
                )
                await db.commit()
            storage._undecodable_result_ids.clear()

            await storage._migrate_restore_ai_classifications()

            assert await _failure_history_classifications(temp_db_path, "frame-1") == [
                "PRODUCT BUG"
            ]
            assert await _migration_recorded(temp_db_path, key) is True
            assert storage.undecodable_result_count() == 0

            # Once applied, later restarts touch nothing at all.
            settled = await _failure_history_row_ids(temp_db_path)
            await storage._migrate_restore_ai_classifications()
            assert await _failure_history_row_ids(temp_db_path) == settled

        assert "Migration: 1 job(s) skipped (frame-1)" in storage_logs.text

    async def test_restore_ai_migration_rewrites_an_equal_value_job_once(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """A job whose AI value equals its override is restored once, not every run."""
        key = "restore_ai_classifications_v1"
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "DELETE FROM _migrations_applied WHERE key = ?", (key,)
                )
                await db.commit()
            storage._undecodable_result_ids.clear()

            for job_id, result in (
                ("equal-1", _one_failure_result("equal-1")),
                ("equal-new", _one_failure_result("equal-new", pattern="NEW")),
            ):
                await _insert_completed_result(temp_db_path, job_id, result)
                # The override equals the AI value, so the candidate predicate
                # still matches this job after it was restored.
                await _seed_mirrored_override(
                    temp_db_path, job_id, classification="PRODUCT BUG"
                )
            await _insert_completed_result(temp_db_path, "frame-1", _DAMAGED_FRAME)
            await _seed_mirrored_override(temp_db_path, "frame-1")

            before = await _failure_history_row_ids(temp_db_path)

            # Run 1: the seeded rows are repaired (their ids change).
            await storage._migrate_restore_ai_classifications()
            after_first = await _failure_history_row_ids(temp_db_path)
            assert after_first["equal-1"] != before["equal-1"]
            assert after_first["equal-new"] != before["equal-new"]
            assert await _failure_history_classifications(temp_db_path, "equal-1") == [
                "PRODUCT BUG"
            ]
            assert await _failure_history_classifications(
                temp_db_path, "equal-new"
            ) == ["PRODUCT BUG"]
            assert await _migration_recorded(temp_db_path, key) is False

            # Run 2: nothing disagrees with the payload, so nothing is rewritten.
            await storage._migrate_restore_ai_classifications()
            assert await _failure_history_row_ids(temp_db_path) == after_first
            assert await _migration_recorded(temp_db_path, key) is False
            assert storage.undecodable_result_count() == 1

            # Run 3 with the deferred row readable: it is repaired, the settled
            # jobs are left alone and the gate key is finally recorded.
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "UPDATE results SET result_json = ? WHERE job_id = 'frame-1'",
                    (_one_failure_result("frame-1"),),
                )
                await db.commit()
            storage._undecodable_result_ids.clear()

            await storage._migrate_restore_ai_classifications()

            settled = await _failure_history_row_ids(temp_db_path)
            assert settled["equal-1"] == after_first["equal-1"]
            assert settled["equal-new"] == after_first["equal-new"]
            assert await _failure_history_classifications(temp_db_path, "frame-1") == [
                "PRODUCT BUG"
            ]
            assert await _migration_recorded(temp_db_path, key) is True

        assert "Migration: 1 job(s) skipped (frame-1)" in storage_logs.text

    @pytest.mark.parametrize("column", storage._HISTORY_COMPARED_COLUMNS)
    async def test_history_disagrees_with_payload_true_when_any_compared_column_differs(
        self, temp_db_path: Path, column: str
    ) -> None:
        """Every compared column can make the guard report a difference."""
        differing: dict[str, Any] = {
            "job_name": "other",
            "build_number": 7,
            "build_id": "9",
            "test_name": "job-a.test_b",
            "error_message": "different",
            "error_signature": "sig-other",
            "error_signature_v2": "sig2-other",
            "classification": "CODE ISSUE",
            # Never NEW: that direction is the pattern marker's (test C).
            "pattern": "REGRESSION",
            "child_job_name": "child",
            "child_build_number": 7,
        }
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            payload = json.loads(_one_failure_result("job-a"))
            await storage.populate_failure_history("job-a", payload)
            assert (
                await storage._history_disagrees_with_payload("job-a", payload) is False
            )

            await _set_failure_history_column(
                temp_db_path, "job-a", column, differing[column]
            )

            assert (
                await storage._history_disagrees_with_payload("job-a", payload) is True
            )

    async def test_history_disagrees_with_payload_treats_the_pattern_marker_as_empty_when_classified(
        self, temp_db_path: Path
    ) -> None:
        """The pattern axis' "nothing assigned yet" shapes are not differences."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()

            # (a) stored NEW against a payload with no pattern.
            payload = json.loads(_one_failure_result("marker-a"))
            await storage.populate_failure_history("marker-a", payload)
            await _set_failure_history_column(
                temp_db_path, "marker-a", "pattern", "NEW"
            )
            assert (
                await storage._history_disagrees_with_payload("marker-a", payload)
                is False
            )

            # (b) stored '' against the engine's default payload pattern.
            payload_new = json.loads(_one_failure_result("marker-b", pattern="NEW"))
            await storage.populate_failure_history("marker-b", payload_new)
            await _set_failure_history_column(temp_db_path, "marker-b", "pattern", "")
            assert (
                await storage._history_disagrees_with_payload("marker-b", payload_new)
                is False
            )

            # (c) the raw TEXT round-trip keeps its value, and a payload 0 must
            # still be reported as a difference instead of equated to it.
            payload_text = json.loads(
                _one_failure_result(
                    "text-job",
                    children=[
                        {
                            "job_name": "child-job",
                            "build_number": "n/a",
                            "failures": [
                                {
                                    "test_name": "text-job.child",
                                    "error": "boom-child",
                                    "analysis": {"classification": "CODE ISSUE"},
                                }
                            ],
                        }
                    ],
                )
            )
            await storage.populate_failure_history("text-job", payload_text)
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    "SELECT typeof(child_build_number) FROM failure_history "
                    "WHERE job_id = 'text-job' AND child_job_name = 'child-job'"
                )
                assert (await cursor.fetchone())[0] == "text"
            assert (
                await storage._history_disagrees_with_payload("text-job", payload_text)
                is False
            )

            payload_text["child_job_analyses"][0]["build_number"] = 0
            assert (
                await storage._history_disagrees_with_payload("text-job", payload_text)
                is True
            )

            # (d) with both classifications empty, pattern NEW is the only
            # difference left -- the marker is not a synonym for '' there.
            payload_blank = json.loads(
                _one_failure_result("blank-job", classification="")
            )
            await storage.populate_failure_history("blank-job", payload_blank)
            await _set_failure_history_column(
                temp_db_path, "blank-job", "classification", ""
            )
            await _set_failure_history_column(
                temp_db_path, "blank-job", "pattern", "NEW"
            )
            assert (
                await storage._history_disagrees_with_payload(
                    "blank-job", payload_blank
                )
                is True
            )

            # (e) step 1 of the pattern axis copies a pattern label into
            # pattern: that value equals the row, a different label does not.
            payload_label = json.loads(
                _one_failure_result("label-job", classification="REGRESSION")
            )
            await storage.populate_failure_history("label-job", payload_label)
            await _set_failure_history_column(
                temp_db_path, "label-job", "pattern", "REGRESSION"
            )
            assert (
                await storage._history_disagrees_with_payload(
                    "label-job", payload_label
                )
                is False
            )

            await _set_failure_history_column(
                temp_db_path, "label-job", "pattern", "FLAKY"
            )
            assert (
                await storage._history_disagrees_with_payload(
                    "label-job", payload_label
                )
                is True
            )

    async def test_init_db_does_not_rewrite_a_settled_job_across_three_real_boots(
        self, temp_db_path: Path, storage_logs: Any
    ) -> None:
        """Three real boots: boot 1 repairs, boots 2 and 3 leave the rows alone."""
        ai_key = "restore_ai_classifications_v1"
        pattern_key = "backfill_pattern_axis_v1"
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            for job_id, result in (
                ("equal-1", _one_failure_result("equal-1")),
                ("equal-new", _one_failure_result("equal-new", pattern="NEW")),
            ):
                await _insert_completed_result(temp_db_path, job_id, result)
                # Override equals the AI value: still a candidate after repair.
                await _seed_mirrored_override(
                    temp_db_path, job_id, classification="PRODUCT BUG"
                )
            await _insert_completed_result(temp_db_path, "frame-1", _DAMAGED_FRAME)
            # A pattern label keeps backfill_pattern_axis_v1 unapplied too.
            await _seed_mirrored_override(
                temp_db_path, "frame-1", classification="REGRESSION"
            )
            async with aiosqlite.connect(temp_db_path) as db:
                await db.execute(
                    "DELETE FROM _migrations_applied WHERE key IN (?, ?)",
                    (ai_key, pattern_key),
                )
                await db.commit()
            storage._undecodable_result_ids.clear()
            ids_before = await _failure_history_row_ids(temp_db_path)

            # Boot 1: both readable jobs are repaired.
            await storage.init_db()
            ids_1 = await _failure_history_row_ids(temp_db_path)
            assert ids_1["equal-1"] != ids_before["equal-1"]
            assert ids_1["equal-new"] != ids_before["equal-new"]
            for job_id in ("equal-1", "equal-new"):
                assert await _failure_history_classifications(temp_db_path, job_id) == [
                    "PRODUCT BUG"
                ]
            assert await _migration_recorded(temp_db_path, ai_key) is False
            assert await _migration_recorded(temp_db_path, pattern_key) is False
            assert storage.undecodable_result_count() == 1
            # The pattern axis supplies NEW on the row the restore wrote with the
            # payload's empty pattern: the interaction is exercised, not assumed.
            patterns_1 = await _failure_history_patterns(temp_db_path)
            assert patterns_1["equal-1"] == ["NEW"]

            # Boots 2 and 3: settled rows keep their ids and their pattern.
            for _ in range(2):
                await storage.init_db()
                assert await _failure_history_row_ids(temp_db_path) == ids_1
                assert await _failure_history_patterns(temp_db_path) == patterns_1
                assert await _migration_recorded(temp_db_path, ai_key) is False
                assert await _migration_recorded(temp_db_path, pattern_key) is False
                assert storage.undecodable_result_count() == 1

        text = storage_logs.text
        assert "Migration: 1 job(s) skipped (frame-1)" in text
        for job_id in ("equal-1", "equal-new"):
            assert (
                text.count(f"Populated failure_history with 1 rows for job_id={job_id}")
                == 1
            )

    async def test_failure_history_row_order_matches_the_column_constant(
        self, temp_db_path: Path
    ) -> None:
        """The extractor, the constant and both INSERTs agree on the row order."""
        columns = storage._FAILURE_HISTORY_COLUMNS
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            payload = json.loads(
                _one_failure_result(
                    "order-job",
                    classification="PRODUCT BUG",
                    pattern="REGRESSION",
                    build_number=7,
                    build_id="9",
                    children=[
                        {
                            "job_name": "child-job",
                            "build_number": 3,
                            "failures": [
                                {
                                    "test_name": "child.test_b",
                                    "error": "err-b",
                                    "error_signature": "sig-b",
                                    "error_signature_v2": "sig2-b",
                                    "analysis": {
                                        "classification": "CODE ISSUE",
                                        "pattern": "FLAKY",
                                    },
                                }
                            ],
                        }
                    ],
                )
            )
            expected: list[dict[str, Any]] = [
                {
                    "job_id": "order-job",
                    "job_name": "order-job",
                    "build_number": 7,
                    "build_id": "9",
                    "test_name": "order-job.test_a",
                    "error_message": storage.failure_error_text(payload["failures"][0]),
                    "error_signature": "sig-order-job",
                    "error_signature_v2": None,
                    "classification": "PRODUCT BUG",
                    "pattern": "REGRESSION",
                    "child_job_name": "",
                    "child_build_number": 0,
                    "analyzed_at": "",
                },
                {
                    "job_id": "order-job",
                    "job_name": "order-job",
                    "build_number": 7,
                    "build_id": "9",
                    "test_name": "child.test_b",
                    "error_message": storage.failure_error_text(
                        payload["child_job_analyses"][0]["failures"][0]
                    ),
                    "error_signature": "sig-b",
                    "error_signature_v2": "sig2-b",
                    "classification": "CODE ISSUE",
                    "pattern": "FLAKY",
                    "child_job_name": "child-job",
                    "child_build_number": 3,
                    "analyzed_at": "",
                },
            ]
            assert list(expected[0]) == list(columns)

            rows = storage._history_rows_for_result("order-job", payload)
            assert len(rows) == 2
            assert len(rows[0]) == len(columns) == 13

            # Every position, by name: zip(strict=True) only catches a length.
            for row, want in zip(rows, expected, strict=True):
                for name, value in want.items():
                    assert row[columns.index(name)] == value

            # The DB half: constant -> INSERT -> stored column names.
            analyzed_at = "2026-01-01T00:00:00"
            await storage.populate_failure_history(
                "order-job", payload, analyzed_at=analyzed_at
            )
            async with aiosqlite.connect(temp_db_path) as db:
                cursor = await db.execute(
                    f"SELECT {', '.join(columns)} FROM failure_history "
                    "WHERE job_id = 'order-job' ORDER BY id"
                )
                stored = await cursor.fetchall()
            assert len(stored) == 2
            for row, want in zip(stored, expected, strict=True):
                for name, value in {**want, "analyzed_at": analyzed_at}.items():
                    assert row[columns.index(name)] == value

            # The guard compares exactly the constant minus its two exclusions.
            assert storage._HISTORY_COMPARED_COLUMNS == tuple(
                column for column in columns if column not in ("job_id", "analyzed_at")
            )


class TestStaleRetryStartsFromTheCallersPayload:
    """A stale retry must not re-write what a newer stored payload dropped."""

    async def test_retry_does_not_carry_a_field_the_newer_payload_dropped(
        self, setup_test_db: Path, monkeypatch
    ) -> None:
        """A field merged on attempt one cannot survive into attempt two.

        ``_write_result`` re-runs the same attempt closure after a lost race, and
        the closure merges stored progress fields into the dict it encodes. Without
        a per-attempt copy that merge lands in the caller's payload, so the second
        attempt re-writes a ``progress_phase`` the newer stored result no longer
        has.
        """
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                "job-retry",
                status="running",
                result={"summary": "older", "progress_phase": "cloning"},
            )
            real_guard = storage._begin_guarded_write
            attempts = 0

            async def _lose_the_first_race(db: Any, job_id: str, expect: Any) -> bool:
                nonlocal attempts
                attempts += 1
                if attempts > 1:
                    return await real_guard(db, job_id, expect)
                # The rival write drops the phase the first attempt just merged in.
                async with storage._connect_db() as rival:
                    await rival.execute(
                        "UPDATE results SET result_json = ? WHERE job_id = ?",
                        (json.dumps({"summary": "newer"}), job_id),
                    )
                    await rival.commit()
                return False

            monkeypatch.setattr(storage, "_begin_guarded_write", _lose_the_first_race)

            payload: dict[str, Any] = {"summary": "final"}
            assert await storage.update_status("job-retry", "completed", payload)
            assert attempts == 2

            stored = await storage.get_result("job-retry")

        assert "progress_phase" not in stored["result"], (
            "the retry restored a progress_phase the newer payload had dropped"
        )
        assert "progress_phase" not in payload, (
            "the attempt closure mutated the caller's payload"
        )


async def test_pin_db_path_without_a_path_keeps_the_inherited_pin(
    tmp_path: Path,
) -> None:
    """A no-argument re-pin keeps the current pin; it does not re-read the global.

    ``_spawn_background`` re-pins inside the context it copies, so a task spawned
    from an already pinned task must stay on its parent's database (#409).
    """
    started_on = tmp_path / "started-on.db"
    moved_to = tmp_path / "moved-to.db"
    storage.pin_db_path(started_on)
    with patch.object(storage, "DB_PATH", moved_to):
        storage.pin_db_path()
        assert storage.current_db_path() == started_on


# --- the shared job-scoped deletion spec (#455) -----------------------------


async def test_delete_job_rows_removes_tracked_in_links(setup_test_db: Path) -> None:
    """The interactive delete leaked tracker links; the one spec now covers them."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result(
            "job-til", status="completed", result={"summary": "s"}
        )
        await storage.add_tracked_in_link(
            job_id="job-til",
            test_name="t1",
            url="https://tracker.example.com/1",
            tracked_type="jira",
        )
        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM tracked_in_links WHERE job_id = ?", ("job-til",)
            )
            assert (await cursor.fetchone())[0] == 1

        assert await storage.delete_job("job-til") is True

        async with storage._connect_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM tracked_in_links WHERE job_id = ?", ("job-til",)
            )
            assert (await cursor.fetchone())[0] == 0


async def test_every_job_scoped_table_is_in_the_deletion_spec(
    setup_test_db: Path,
) -> None:
    """A new job-scoped table must be added here, or a delete orphans its rows."""
    allowed = {"results"}
    exempt = {
        "mention_reads",
        "ai_session_sources",
        "job_metadata",
        "job_metadata_labels",
    }
    with patch.object(storage, "DB_PATH", setup_test_db):
        async with storage._connect_db() as db:
            tables = [
                row[0]
                for row in await (
                    await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ).fetchall()
            ]
            job_scoped = set()
            for table in tables:
                columns = await (
                    await db.execute(f"PRAGMA table_info({table})")
                ).fetchall()
                if any(column[1] == "job_id" for column in columns):
                    job_scoped.add(table)
    spec = {table for table, _ in storage._JOB_SCOPED_DELETES}
    # ``mention_reads`` is keyed by ``comment_id``, so it is job-scoped through
    # the ``comments`` row and has no ``job_id`` column of its own.
    exempt = exempt | {"mention_reads"}
    missing = job_scoped - spec - allowed - exempt
    assert not missing, f"job-scoped tables missing from _JOB_SCOPED_DELETES: {missing}"
    stale = spec - set(tables)
    assert not stale, f"_JOB_SCOPED_DELETES names tables that do not exist: {stale}"


async def test_delete_jobs_bulk_require_ended_skips_a_running_job(
    setup_test_db: Path,
) -> None:
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result("running-job", status="running", result={"a": 1})
        result = await storage.delete_jobs_bulk(["running-job"], require_ended=True)
        assert result["deleted"] == []
        assert result["failed"] == [{"job_id": "running-job", "reason": "not ended"}]
        assert await storage.get_result("running-job") is not None


async def test_delete_jobs_bulk_default_still_deletes_a_running_job(
    setup_test_db: Path,
) -> None:
    """The shipped endpoints delete exactly what they named."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result("running-job", status="running", result={"a": 1})
        result = await storage.delete_jobs_bulk(["running-job"])
        assert result["deleted"] == ["running-job"]
        assert await storage.get_result("running-job") is None


async def test_delete_jobs_bulk_is_protected_skips_a_job(setup_test_db: Path) -> None:
    with patch.object(storage, "DB_PATH", setup_test_db):
        await storage.save_result("busy-job", status="completed", result={"a": 1})
        result = await storage.delete_jobs_bulk(
            ["busy-job"], is_protected=lambda job_id: job_id == "busy-job"
        )
        assert result["deleted"] == []
        assert result["failed"] == [{"job_id": "busy-job", "reason": "busy"}]
        assert await storage.get_result("busy-job") is not None
