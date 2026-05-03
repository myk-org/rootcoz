"""Tests for SQLite storage."""

from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from jenkins_job_insight import storage


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


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
            assert result["result"] == complex_result
            assert len(result["result"]["failures"]) == 2


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
                    """INSERT INTO results (job_id, jenkins_url, status, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        "job-order-0",
                        "https://jenkins.example.com/job/test/0/",
                        "completed",
                        "2024-01-01 10:00:00",
                    ),
                )
                await db.execute(
                    """INSERT INTO results (job_id, jenkins_url, status, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        "job-order-1",
                        "https://jenkins.example.com/job/test/1/",
                        "completed",
                        "2024-01-01 11:00:00",
                    ),
                )
                await db.execute(
                    """INSERT INTO results (job_id, jenkins_url, status, created_at)
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
        """Regression: job_name + child_build_number=0 must not raise and must mirror to history."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Seed a failure_history row that the wildcard mirror should update
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

            # Verify the wildcard mirror updated the matching failure_history row
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT classification FROM failure_history "
                    "WHERE job_id=? AND test_name=? AND child_job_name=? AND child_build_number=?",
                    ("job-cls-zero", "tests.TestA.test_one", "parent-job", 7),
                )
                row = await cursor.fetchone()
                assert row is not None, "failure_history row should exist"
                assert row[0] == "FLAKY"

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
        """Invalid classification raises ValueError."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            with pytest.raises(ValueError, match="Invalid classification"):
                await storage.set_test_classification(
                    test_name="tests.TestA.test_one",
                    classification="INVALID",
                    job_id="job-cls-invalid",
                )

    async def test_empty_job_id_raises_value_error(self, setup_test_db: Path) -> None:
        """Empty job_id raises ValueError."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            with pytest.raises(ValueError, match="job_id is required"):
                await storage.set_test_classification(
                    test_name="tests.TestA.test_one",
                    classification="FLAKY",
                    job_id="",
                )


class TestMarkStaleResultsFailed:
    """Tests for the mark_stale_results_failed function."""

    async def test_marks_pending_as_failed(self, setup_test_db: Path) -> None:
        """Pending jobs are marked failed on startup."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("pending-1", "http://j/1", "pending")
            waiting = await storage.mark_stale_results_failed()
            assert waiting == []
            result = await storage.get_result("pending-1")
            assert result["status"] == "failed"

    async def test_marks_running_as_failed(self, setup_test_db: Path) -> None:
        """Running jobs are marked failed on startup."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result("running-1", "http://j/2", "running")
            waiting = await storage.mark_stale_results_failed()
            assert waiting == []
            result = await storage.get_result("running-1")
            assert result["status"] == "failed"

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
            waiting = await storage.mark_stale_results_failed()
            assert len(waiting) == 1
            assert waiting[0]["job_id"] == "waiting-1"
            assert waiting[0]["result_data"]["job_name"] == "my-job"
            assert (
                waiting[0]["result_data"]["request_params"]["tests_repo_url"]
                == "https://example.invalid/tests"
            )
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

            waiting = await storage.mark_stale_results_failed()
            assert len(waiting) == 1
            assert waiting[0]["job_id"] == "w1"

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
            waiting = await storage.mark_stale_results_failed()
            assert waiting == []
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
            waiting = await storage.mark_stale_results_failed()
            assert waiting == []
            result = await storage.get_result("w-incomplete")
            assert result["status"] == "failed"

    async def test_waiting_with_malformed_json_marked_failed(
        self, setup_test_db: Path
    ) -> None:
        """Waiting rows with malformed JSON are marked as failed (unrecoverable)."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    "INSERT INTO results (job_id, jenkins_url, status, result_json) "
                    "VALUES (?, ?, ?, ?)",
                    ("w-bad-json", "http://j/7", "waiting", "{not-json"),
                )
                await db.commit()

            waiting = await storage.mark_stale_results_failed()
            assert waiting == []
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

    async def test_returns_infrastructure_from_test_classifications(
        self, setup_test_db: Path
    ) -> None:
        """Returns INFRASTRUCTURE from visible test_classifications entries."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_test_classification(
                test_name="tests.TestA.test_infra",
                classification="INFRASTRUCTURE",
                job_id="job-infra-1",
                visible=1,
            )
            cls = await storage.get_history_classification(
                "job-infra-1", "tests.TestA.test_infra"
            )
            assert cls == "INFRASTRUCTURE"

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
        """Falls back to failure_history classification when no test_classifications entry."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, classification,
                        error_message, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        "job-fh",
                        "my-job",
                        1,
                        "tests.TestA.test_fh",
                        "INFRASTRUCTURE",
                        "err",
                    ),
                )
                await db.commit()
            cls = await storage.get_history_classification(
                "job-fh", "tests.TestA.test_fh"
            )
            assert cls == "INFRASTRUCTURE"

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
                classification="INFRASTRUCTURE",
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
                classification="INFRASTRUCTURE",
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
            # Should return INFRASTRUCTURE for child-job-1
            cls = await storage.get_history_classification(
                "job-child",
                "tests.TestA.test_child",
                child_job_name="child-job-1",
                child_build_number=5,
            )
            assert cls == "INFRASTRUCTURE"
            # Should return REGRESSION for child-job-2
            cls2 = await storage.get_history_classification(
                "job-child",
                "tests.TestA.test_child",
                child_job_name="child-job-2",
                child_build_number=6,
            )
            assert cls2 == "REGRESSION"
