"""Tests for failure history storage and query functions."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jenkins_job_insight import storage


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


class TestFailureHistoryTable:
    async def test_failure_history_table_exists(self, setup_test_db, temp_db_path):
        import aiosqlite

        async with aiosqlite.connect(temp_db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='failure_history'"
            )
            row = await cursor.fetchone()
            assert row is not None, "failure_history table should exist"

    async def test_failure_history_indexes_exist(self, setup_test_db, temp_db_path):
        import aiosqlite

        expected_indexes = [
            "idx_fh_test_name",
            "idx_fh_error_signature",
            "idx_fh_job_name",
            "idx_fh_analyzed_at",
            "idx_fh_job_test",
        ]
        async with aiosqlite.connect(temp_db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
            rows = await cursor.fetchall()
            existing = {row[0] for row in rows}
            for idx_name in expected_indexes:
                assert idx_name in existing, f"Index {idx_name} should exist"


class TestPopulateFailureHistory:
    async def test_populate_from_top_level_failures(self, setup_test_db):
        """Test populating from a result with top-level failures."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "ocp-4.16-e2e",
                "build_number": 247,
                "failures": [
                    {
                        "test_name": "tests.network.TestDNS.test_lookup",
                        "error": "DNS resolution failed",
                        "error_signature": "sig-abc123",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "DNS service is down",
                        },
                    },
                    {
                        "test_name": "tests.storage.TestPV.test_create",
                        "error": "PV creation timeout",
                        "error_signature": "sig-def456",
                        "analysis": {
                            "classification": "CODE ISSUE",
                            "details": "Test timeout too short",
                        },
                    },
                ],
                "child_job_analyses": [],
            }
            await storage.populate_failure_history("job-1", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM failure_history WHERE job_id = ?",
                    ("job-1",),
                )
                count = (await cursor.fetchone())[0]
                assert count == 2

                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT * FROM failure_history WHERE job_id = ? ORDER BY test_name",
                    ("job-1",),
                )
                rows = await cursor.fetchall()
                row0 = dict(rows[0])
                assert row0["test_name"] == "tests.network.TestDNS.test_lookup"
                assert row0["job_name"] == "ocp-4.16-e2e"
                assert row0["build_number"] == 247
                assert row0["error_signature"] == "sig-abc123"
                assert row0["classification"] == "PRODUCT BUG"
                assert row0["child_job_name"] == ""
                assert row0["child_build_number"] == 0

    async def test_populate_from_child_job_analyses(self, setup_test_db):
        """Test populating from a result with child job failures."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "pipeline-main",
                "build_number": 100,
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-e2e",
                        "build_number": 50,
                        "failures": [
                            {
                                "test_name": "tests.TestA.test_one",
                                "error": "Assertion failed",
                                "error_signature": "sig-child1",
                                "analysis": {
                                    "classification": "CODE ISSUE",
                                    "details": "...",
                                },
                            },
                        ],
                        "failed_children": [],
                    },
                ],
            }
            await storage.populate_failure_history("job-2", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT * FROM failure_history WHERE job_id = ?",
                    ("job-2",),
                )
                rows = await cursor.fetchall()
                assert len(rows) == 1
                row = dict(rows[0])
                assert row["test_name"] == "tests.TestA.test_one"
                assert row["child_job_name"] == "child-e2e"
                assert row["child_build_number"] == 50
                assert row["job_name"] == "pipeline-main"

    async def test_populate_from_nested_failed_children(self, setup_test_db):
        """Test populating from deeply nested child job failures."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "pipeline-main",
                "build_number": 200,
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "mid-pipeline",
                        "build_number": 10,
                        "failures": [],
                        "failed_children": [
                            {
                                "job_name": "leaf-job",
                                "build_number": 5,
                                "failures": [
                                    {
                                        "test_name": "tests.Deep.test_nested",
                                        "error": "deep error",
                                        "error_signature": "sig-deep",
                                        "analysis": {
                                            "classification": "PRODUCT BUG",
                                            "details": "...",
                                        },
                                    },
                                ],
                                "failed_children": [],
                            },
                        ],
                    },
                ],
            }
            await storage.populate_failure_history("job-3", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM failure_history WHERE job_id = ?",
                    ("job-3",),
                )
                count = (await cursor.fetchone())[0]
                assert count == 1

    async def test_populate_idempotent(self, setup_test_db):
        """Calling populate twice should not create duplicate rows."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "ocp-4.16-e2e",
                "build_number": 247,
                "failures": [
                    {
                        "test_name": "tests.TestA.test_one",
                        "error": "err",
                        "error_signature": "sig-1",
                        "analysis": {"classification": "CODE ISSUE", "details": "..."},
                    },
                ],
                "child_job_analyses": [],
            }
            await storage.populate_failure_history("job-idem", result_data)
            await storage.populate_failure_history("job-idem", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM failure_history WHERE job_id = ?",
                    ("job-idem",),
                )
                count = (await cursor.fetchone())[0]
                assert count == 1

    async def test_populate_empty_failures(self, setup_test_db):
        """Passing a result with no failures should be a no-op."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "ocp-4.16-e2e",
                "build_number": 1,
                "failures": [],
                "child_job_analyses": [],
            }
            await storage.populate_failure_history("job-empty", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM failure_history WHERE job_id = ?",
                    ("job-empty",),
                )
                count = (await cursor.fetchone())[0]
                assert count == 0


class TestBackfillFailureHistory:
    async def test_backfill_populates_from_existing_results(self, setup_test_db):
        """Backfill should populate from existing completed results when table is empty."""
        import aiosqlite

        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert a completed result directly into the results table
            result_data = {
                "job_name": "ocp-4.16-e2e",
                "build_number": 100,
                "failures": [
                    {
                        "test_name": "tests.TestA.test_one",
                        "error": "some error",
                        "error_signature": "sig-backfill",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "...",
                        },
                    },
                ],
                "child_job_analyses": [],
            }
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                    (
                        "backfill-1",
                        "https://jenkins.example.com/job/test/100/",
                        "completed",
                        json.dumps(result_data),
                    ),
                )
                await db.commit()

            # Run backfill
            await storage.backfill_failure_history()

            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM failure_history WHERE job_id = ?",
                    ("backfill-1",),
                )
                count = (await cursor.fetchone())[0]
                assert count == 1

    async def test_backfill_processes_missing_results_when_table_not_empty(
        self, setup_test_db
    ):
        """Backfill should still process missing results even when failure_history has data."""
        import aiosqlite

        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert existing failure_history row (table is non-empty)
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    "INSERT INTO failure_history (job_id, job_name, build_number, test_name) VALUES (?, ?, ?, ?)",
                    ("existing-1", "some-job", 1, "test_existing"),
                )
                # Insert a completed result whose job_id is already in failure_history
                await db.execute(
                    "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                    (
                        "existing-1",
                        "https://jenkins.example.com/job/test/100/",
                        "completed",
                        json.dumps(
                            {
                                "job_name": "some-job",
                                "build_number": 1,
                                "failures": [
                                    {
                                        "test_name": "test_existing",
                                        "error": "e",
                                        "error_signature": "s",
                                        "analysis": {
                                            "classification": "CODE ISSUE",
                                            "details": "d",
                                        },
                                    }
                                ],
                                "child_job_analyses": [],
                            }
                        ),
                    ),
                )
                await db.commit()

            # Insert a NEW completed result not yet in failure_history
            result_data = {
                "job_name": "ocp-4.16-e2e",
                "build_number": 200,
                "failures": [
                    {
                        "test_name": "tests.TestB.test_two",
                        "error": "err",
                        "error_signature": "sig-skip",
                        "analysis": {
                            "classification": "CODE ISSUE",
                            "details": "...",
                        },
                    },
                ],
                "child_job_analyses": [],
            }
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                    (
                        "backfill-2",
                        "https://jenkins.example.com/job/test/200/",
                        "completed",
                        json.dumps(result_data),
                    ),
                )
                await db.commit()

            await storage.backfill_failure_history()

            # The missing result (backfill-2) SHOULD have been backfilled
            # even though the table already had data — the backfill is resumable
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM failure_history WHERE job_id = ?",
                    ("backfill-2",),
                )
                count = (await cursor.fetchone())[0]
                assert count == 1

            # The already-present result (existing-1) should NOT be duplicated
            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM failure_history WHERE job_id = ?",
                    ("existing-1",),
                )
                count = (await cursor.fetchone())[0]
                assert count == 1

    async def test_backfill_skips_non_completed_results(self, setup_test_db):
        """Backfill should only process completed results."""
        import aiosqlite

        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                    (
                        "pending-1",
                        "https://jenkins.example.com/job/test/1/",
                        "pending",
                        None,
                    ),
                )
                await db.execute(
                    "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                    (
                        "failed-1",
                        "https://jenkins.example.com/job/test/2/",
                        "failed",
                        json.dumps({"error": "boom"}),
                    ),
                )
                await db.commit()

            await storage.backfill_failure_history()

            async with aiosqlite.connect(setup_test_db) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM failure_history")
                count = (await cursor.fetchone())[0]
                assert count == 0


class TestGetTestHistory:
    async def _seed_failures(self, db_path):
        """Helper to seed failure_history with test data."""
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            # Insert results rows for pass inference
            for i in range(5):
                await db.execute(
                    "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                    (
                        f"job-{i}",
                        "https://jenkins.example.com/job/test/1/",
                        "completed",
                        json.dumps(
                            {
                                "job_name": "ocp-4.16-e2e",
                                "build_number": i + 1,
                                "failures": [],
                                "child_job_analyses": [],
                            }
                        ),
                    ),
                )

            # Insert failure_history rows: test_lookup failed in jobs 0,1,2 (passed in 3,4)
            for i in range(3):
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, error_message, error_signature, classification, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"job-{i}",
                        "ocp-4.16-e2e",
                        i + 1,
                        "tests.network.TestDNS.test_lookup",
                        "DNS resolution failed",
                        "sig-dns",
                        "PRODUCT BUG",
                        f"2026-03-{15 + i:02d} 10:00:00",
                    ),
                )

            # Insert a comment for the test
            await db.execute(
                "INSERT INTO comments (job_id, test_name, comment, error_signature, username, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "job-0",
                    "tests.network.TestDNS.test_lookup",
                    "Opened bug: OCPBUGS-12345",
                    "sig-dns",
                    "motti",
                    "2026-03-15 09:00:00",
                ),
            )
            await db.commit()

    async def test_get_test_history_basic(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await self._seed_failures(setup_test_db)
            result = await storage.get_test_history("tests.network.TestDNS.test_lookup")

            assert result["test_name"] == "tests.network.TestDNS.test_lookup"
            assert result["failures"] == 3
            assert result["last_classification"] == "PRODUCT BUG"
            assert result["classifications"]["PRODUCT BUG"] == 3
            assert len(result["recent_runs"]) == 3
            assert result["comments"][0]["comment"] == "Opened bug: OCPBUGS-12345"

    async def test_get_test_history_with_job_name_filter(self, setup_test_db):
        import aiosqlite

        with patch.object(storage, "DB_PATH", setup_test_db):
            await self._seed_failures(setup_test_db)
            # Add a failure for a different job
            async with aiosqlite.connect(setup_test_db) as db:
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name, error_signature, classification)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "job-other",
                        "other-job",
                        1,
                        "tests.network.TestDNS.test_lookup",
                        "sig-dns",
                        "CODE ISSUE",
                    ),
                )
                await db.commit()

            result = await storage.get_test_history(
                "tests.network.TestDNS.test_lookup", job_name="ocp-4.16-e2e"
            )
            assert result["failures"] == 3  # Only from ocp-4.16-e2e

    async def test_get_test_history_nonexistent(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.get_test_history("nonexistent.test")
            assert result["test_name"] == "nonexistent.test"
            assert result["failures"] == 0
            assert result["recent_runs"] == []


class TestSearchBySignature:
    async def test_search_finds_matching_tests(self, setup_test_db):
        import aiosqlite

        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                # Two different tests with the same error signature
                for test_name, count in [
                    ("tests.TestA.test_one", 3),
                    ("tests.TestB.test_two", 2),
                ]:
                    for i in range(count):
                        await db.execute(
                            """INSERT INTO failure_history
                               (job_id, job_name, build_number, test_name, error_signature, classification)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                f"job-sig-{test_name}-{i}",
                                "ocp-e2e",
                                i + 1,
                                test_name,
                                "sig-shared",
                                "PRODUCT BUG",
                            ),
                        )
                await db.commit()

            result = await storage.search_by_signature("sig-shared")
            assert result["signature"] == "sig-shared"
            assert result["total_occurrences"] == 5
            assert result["unique_tests"] == 2
            assert len(result["tests"]) == 2

    async def test_search_nonexistent_signature(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.search_by_signature("nonexistent-sig")
            assert result["total_occurrences"] == 0
            assert result["tests"] == []


class TestGetJobStats:
    async def test_get_job_stats_basic(self, setup_test_db):
        import aiosqlite

        with patch.object(storage, "DB_PATH", setup_test_db):
            async with aiosqlite.connect(setup_test_db) as db:
                # 3 builds, 2 with failures
                for i in range(3):
                    await db.execute(
                        "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                        (
                            f"stats-{i}",
                            "https://j.example.com/job/test/1/",
                            "completed",
                            json.dumps(
                                {
                                    "job_name": "ocp-e2e",
                                    "build_number": i + 1,
                                    "failures": [],
                                    "child_job_analyses": [],
                                }
                            ),
                        ),
                    )

                # Failures in builds 0 and 1
                for i in range(2):
                    await db.execute(
                        """INSERT INTO failure_history
                           (job_id, job_name, build_number, test_name, error_signature, classification)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            f"stats-{i}",
                            "ocp-e2e",
                            i + 1,
                            "tests.TestA.test_one",
                            "sig-a",
                            "PRODUCT BUG",
                        ),
                    )
                await db.commit()

            result = await storage.get_job_stats("ocp-e2e")
            assert result["job_name"] == "ocp-e2e"
            assert result["total_builds_analyzed"] == 3
            assert result["builds_with_failures"] == 2
            assert len(result["most_common_failures"]) >= 1

    async def test_get_job_stats_nonexistent(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.get_job_stats("nonexistent-job")
            assert result["total_builds_analyzed"] == 0
