"""Tests for failure history storage and query functions."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz import storage


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

    async def test_populate_stores_prow_build_id(self, setup_test_db):
        """Prow snowflake build IDs must be stored in failure_history.build_id."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            prow_build_id = "2072319655766134784"
            result_data = {
                "job_name": "periodic-ci-e2e-aws",
                "build_number": 0,
                "build_id": prow_build_id,
                "failures": [
                    {
                        "test_name": "tests.TestA.test_one",
                        "error": "failed",
                        "error_signature": "sig-prow",
                        "analysis": {"classification": "CODE ISSUE"},
                    },
                ],
                "child_job_analyses": [],
            }
            await storage.populate_failure_history("job-prow", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT build_id, build_number FROM failure_history WHERE job_id = ?",
                    ("job-prow",),
                )
                row = dict(await cursor.fetchone())
                assert row["build_id"] == prow_build_id
                assert row["build_number"] == 0

    async def test_populate_avoids_sqlite_int_overflow(self, setup_test_db):
        """Large Prow build_number strings must not overflow failure_history INTEGER."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            prow_build_id = "9223372036854775808"
            result_data = {
                "job_name": "periodic-ci-e2e-aws",
                "build_number": prow_build_id,
                "build_id": prow_build_id,
                "failures": [
                    {
                        "test_name": "tests.TestA.test_one",
                        "error": "failed",
                        "error_signature": "sig-overflow",
                        "analysis": {"classification": "CODE ISSUE"},
                    },
                ],
                "child_job_analyses": [],
            }
            await storage.populate_failure_history("job-overflow", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT build_id, build_number FROM failure_history WHERE job_id = ?",
                    ("job-overflow",),
                )
                row = dict(await cursor.fetchone())
                assert row["build_id"] == prow_build_id
                assert row["build_number"] == 0

    async def test_populate_rejects_bool_build_number(self, setup_test_db):
        """JSON bool build_number must not coerce to 1 or stamp build_id."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "periodic-ci-e2e-aws",
                "build_number": True,
                "failures": [
                    {
                        "test_name": "tests.TestA.test_one",
                        "error": "failed",
                        "error_signature": "sig-bool",
                        "analysis": {"classification": "CODE ISSUE"},
                    },
                ],
                "child_job_analyses": [],
            }
            await storage.populate_failure_history("job-bool", result_data)

            import aiosqlite

            async with aiosqlite.connect(setup_test_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT build_id, build_number FROM failure_history WHERE job_id = ?",
                    ("job-bool",),
                )
                row = dict(await cursor.fetchone())
                assert row["build_number"] == 0
                assert row["build_id"] == ""

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
                    "INSERT INTO results (job_id, build_url, status, result_json) VALUES (?, ?, ?, ?)",
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
                    "INSERT INTO results (job_id, build_url, status, result_json) VALUES (?, ?, ?, ?)",
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
                    "INSERT INTO results (job_id, build_url, status, result_json) VALUES (?, ?, ?, ?)",
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
                    "INSERT INTO results (job_id, build_url, status, result_json) VALUES (?, ?, ?, ?)",
                    (
                        "pending-1",
                        "https://jenkins.example.com/job/test/1/",
                        "pending",
                        None,
                    ),
                )
                await db.execute(
                    "INSERT INTO results (job_id, build_url, status, result_json) VALUES (?, ?, ?, ?)",
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
                    "INSERT INTO results"
                    " (job_id, build_url, status, result_json, job_name)"
                    " VALUES (?, ?, ?, ?, ?)",
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
                        "ocp-4.16-e2e",
                    ),
                )

            # Insert failure_history rows: test_lookup failed in jobs 0,1,2 (passed in 3,4)
            for i in range(3):
                await db.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name,
                        error_message, error_signature,
                        classification, analyzed_at)
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
                "INSERT INTO comments"
                " (job_id, test_name, comment, error_signature, username, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
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
            # Add a failure and a completed result for a different job
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
                await db.execute(
                    "INSERT INTO results"
                    " (job_id, status, result_json, job_name)"
                    " VALUES (?, 'completed', ?, ?)",
                    (
                        "job-other-res",
                        json.dumps({"job_name": "other-job", "failures": []}),
                        "other-job",
                    ),
                )
                await db.commit()

            result = await storage.get_test_history(
                "tests.network.TestDNS.test_lookup", job_name="ocp-4.16-e2e"
            )
            assert result["failures"] == 3  # Only from ocp-4.16-e2e
            # Denominator counts only the filtered job's completed results.
            assert result["total_runs"] == 5
            assert result["passes"] == 2

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
                        "INSERT INTO results"
                        " (job_id, build_url, status, result_json, job_name)"
                        " VALUES (?, ?, ?, ?, ?)",
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
                            "ocp-e2e",
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


class TestClassifySourceSpoofing:
    """Regression tests for POST /history/classify source='ai' spoofing (#618)."""

    @pytest.fixture
    def classify_client(self, temp_db_path: Path):
        import asyncio
        import os

        from starlette.testclient import TestClient

        from rootcoz.config import clear_settings_cache
        from rootcoz.main import app

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch.dict(
                os.environ,
                {
                    "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
                    "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
                    "SECURE_COOKIES": "false",
                    "DB_PATH": str(temp_db_path),
                    "REQUIRE_APPROVAL": "false",
                },
            ),
        ):
            clear_settings_cache()
            asyncio.run(storage.init_db())
            with TestClient(app) as client:
                yield client
            clear_settings_cache()

    @staticmethod
    async def _fetch_classification_and_audits(
        db_path: Path, classification_id: int, job_id: str
    ) -> tuple[dict, list[dict]]:
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            row_cur = await db.execute(
                "SELECT id, test_name, pattern, created_by, visible"
                " FROM test_classifications WHERE id = ?",
                (classification_id,),
            )
            row = await row_cur.fetchone()
            assert row is not None
            audit_cur = await db.execute(
                "SELECT actor, action, outcome, target_id, details"
                " FROM audit_events"
                " WHERE action = 'classification.override' AND target_id = ?"
                " ORDER BY id",
                (job_id,),
            )
            audits = [dict(r) for r in await audit_cur.fetchall()]
        return dict(row), audits

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["reviewer", "operator", "admin"])
    async def test_human_roles_cannot_spoof_ai_source(
        self, classify_client, temp_db_path: Path, role: str
    ):
        """Human reviewer/operator/admin sending source='ai' is attributed to self, visible=1, and audited."""
        username = f"human_{role}"
        _, api_key = await storage.create_user(username, role=role)
        login_resp = classify_client.post(
            "/api/auth/login",
            json={"username": username, "api_key": api_key},
        )
        assert login_resp.status_code == 200
        job_id = f"job-spoof-{role}"

        resp = classify_client.post(
            "/history/classify",
            cookies=dict(login_resp.cookies),
            json={
                "test_name": "test_spoofed_ai",
                "classification": "FLAKY",
                "reason": "attempting spoof",
                "job_id": job_id,
                "source": "ai",
            },
        )
        assert resp.status_code == 201, resp.text
        row, audits = await self._fetch_classification_and_audits(
            temp_db_path, resp.json()["id"], job_id
        )
        assert row["created_by"] == username
        assert row["created_by"] != storage.AI_SYSTEM_USERNAME
        assert row["visible"] == 1
        assert len(audits) == 1
        assert audits[0]["actor"] == username
        assert audits[0]["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_bootstrap_admin_and_api_key_cannot_spoof_ai_source(
        self, classify_client, temp_db_path: Path
    ):
        """Bootstrap admin key and user API key sending source='ai' use human attribution and audit."""
        _, reviewer_key = await storage.create_user("api_alice", role="reviewer")
        cases = [
            (
                "Bearer test-admin-key-16chars",  # pragma: allowlist secret
                "admin",
                "job-spoof-bootstrap-admin",
            ),
            (f"Bearer {reviewer_key}", "api_alice", "job-spoof-api-key"),
        ]
        for auth_value, expected_actor, job_id in cases:
            resp = classify_client.post(
                "/history/classify",
                headers={"Authorization": auth_value},
                json={
                    "test_name": "test_spoofed_key",
                    "classification": "FLAKY",
                    "job_id": job_id,
                    "source": "ai",
                },
            )
            assert resp.status_code == 201, resp.text
            row, audits = await self._fetch_classification_and_audits(
                temp_db_path, resp.json()["id"], job_id
            )
            assert row["created_by"] == expected_actor
            assert row["visible"] == 1
            assert len(audits) == 1
            assert audits[0]["actor"] == expected_actor

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_admin", [False, True])
    async def test_chat_minted_session_cannot_spoof_ai_source(
        self, classify_client, temp_db_path: Path, is_admin: bool
    ):
        """Chat-minted sessions (is_ai_session=False) cannot forge rootcoz-ai attribution."""
        from rootcoz import main

        username = "chat_admin" if is_admin else "chat_reviewer"
        role = "admin" if is_admin else "reviewer"
        await storage.create_user(username, role=role)
        chat_auth_header = await main._create_ai_auth_header(
            username, is_admin=is_admin
        )
        assert chat_auth_header.startswith("Bearer ")
        job_id = f"job-chat-spoof-{role}"

        resp = classify_client.post(
            "/history/classify",
            headers={"Authorization": chat_auth_header},
            json={
                "test_name": "test_chat_spoof",
                "classification": "FLAKY",
                "job_id": job_id,
                "source": "ai",
            },
        )
        assert resp.status_code == 201, resp.text
        row, audits = await self._fetch_classification_and_audits(
            temp_db_path, resp.json()["id"], job_id
        )
        assert row["created_by"] == username
        assert row["created_by"] != storage.AI_SYSTEM_USERNAME
        assert row["visible"] == 1
        assert len(audits) == 1
        assert audits[0]["actor"] == username

    @pytest.mark.asyncio
    async def test_analysis_ai_session_legitimate_and_human_source_paths(
        self, classify_client, temp_db_path: Path
    ):
        """Server-minted analysis session (is_ai_session=True) respects source='ai' vs source=''."""
        from rootcoz import main

        await storage.create_user("op_user", role="operator")
        ai_auth_header = await main._create_ai_auth_header(
            "op_user", is_ai_session=True
        )
        assert ai_auth_header.startswith("Bearer ")

        # 1. With source="ai": attributed to rootcoz-ai, hidden until revealed, no override audit.
        ai_job_id = "job-legit-ai"
        resp_ai = classify_client.post(
            "/history/classify",
            headers={"Authorization": ai_auth_header},
            json={
                "test_name": "test_legit_ai",
                "classification": "FLAKY",
                "reason": "Intermittent across recent builds",
                "job_id": ai_job_id,
                "source": "ai",
            },
        )
        assert resp_ai.status_code == 201, resp_ai.text
        row_ai, audits_ai = await self._fetch_classification_and_audits(
            temp_db_path, resp_ai.json()["id"], ai_job_id
        )
        assert row_ai["created_by"] == storage.AI_SYSTEM_USERNAME
        assert row_ai["visible"] == 0
        assert audits_ai == []
        assert (
            await storage.get_history_classification(ai_job_id, "test_legit_ai") == ""
        )

        await storage.make_classifications_visible(ai_job_id)
        assert (
            await storage.get_history_classification(ai_job_id, "test_legit_ai")
            == "FLAKY"
        )

        # 2. Same AI-scoped session with source="": uses human attribution, visible=1, audited.
        human_job_id = "job-ai-session-empty-source"
        resp_human = classify_client.post(
            "/history/classify",
            headers={"Authorization": ai_auth_header},
            json={
                "test_name": "test_empty_source",
                "classification": "REGRESSION",
                "job_id": human_job_id,
                "source": "",
            },
        )
        assert resp_human.status_code == 201, resp_human.text
        row_human, audits_human = await self._fetch_classification_and_audits(
            temp_db_path, resp_human.json()["id"], human_job_id
        )
        assert row_human["created_by"] == "op_user"
        assert row_human["visible"] == 1
        assert len(audits_human) == 1
        assert audits_human[0]["actor"] == "op_user"

    def test_only_analysis_callers_mint_ai_sessions(self):
        """Guard: only the two analysis pipeline callers pass is_ai_session=True."""
        import inspect

        from rootcoz import main

        assert inspect.getsource(main).count("is_ai_session=True") == 2
