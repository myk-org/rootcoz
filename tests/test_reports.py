"""Tests for reports API endpoints and storage functions."""

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


@pytest.fixture
async def populated_db(setup_test_db: Path):
    """DB with sample data for reports tests."""
    with patch.object(storage, "DB_PATH", setup_test_db):
        # Insert a completed result with result_json
        result_data = {
            "job_name": "test-job",
            "build_number": 42,
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "AssertionError",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            job_id="job-1",
            jenkins_url="https://jenkins.example.com/job/test/1/",
            status="completed",
            result=result_data,
        )

        # Insert failure_history via direct SQL (populate_failure_history
        # takes a full result dict, but we just need one row for testing)
        async with storage._connect_db() as conn:
            await conn.execute(
                """INSERT INTO failure_history
                   (job_id, job_name, build_number, test_name,
                    error_message, error_signature, classification)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "job-1",
                    "test-job",
                    42,
                    "test_foo",
                    "AssertionError",
                    "sig1",
                    "CODE ISSUE",
                ),
            )
            await conn.commit()

        # Insert a review
        async with storage._connect_db() as conn:
            await conn.execute(
                "INSERT INTO failure_reviews (job_id, test_name, reviewed, username) VALUES (?, ?, ?, ?)",
                ("job-1", "test_foo", 1, "tester"),
            )
            await conn.commit()

        # Insert a test_classification override
        async with storage._connect_db() as conn:
            await conn.execute(
                """INSERT INTO test_classifications
                   (test_name, job_name, classification, original_classification,
                    created_by, job_id, visible)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "test_foo",
                    "test-job",
                    "PRODUCT BUG",
                    "CODE ISSUE",
                    "reviewer",
                    "job-1",
                    1,
                ),
            )
            await conn.commit()

        # Insert a comment with a GitHub issue link
        await storage.add_comment(
            job_id="job-1",
            test_name="test_foo",
            comment="GitHub Issue [GH-123]: [Fix the bug](https://github.com/org/repo/issues/123)",
            username="creator",
        )

        # Insert job metadata with labels for tag filtering
        await storage.set_job_metadata("tagged-job", labels=["nightly", "smoke"])

        # Insert a failed result with tags
        failed_result_data = {
            "job_name": "tagged-job",
            "build_number": 99,
            "tags": ["nightly", "smoke"],
            "failures": [
                {
                    "test_name": "test_bar",
                    "error": "TimeoutError",
                    "analysis": {"classification": "INFRASTRUCTURE"},
                }
            ],
        }
        await storage.save_result(
            job_id="job-2",
            jenkins_url="https://jenkins.example.com/job/test/2/",
            status="failed",
            result=failed_result_data,
        )

        yield setup_test_db


class TestReportTotals:
    @pytest.mark.asyncio
    async def test_empty_db(self, setup_test_db: Path):
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.get_report_totals()
            assert result["total_jobs"] == 0
            assert result["total_failures"] == 0
            assert result["total_reviewed"] == 0
            assert result["jobs"] == []

    @pytest.mark.asyncio
    async def test_with_data(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_totals()
            assert result["total_jobs"] == 1
            assert result["total_failures"] == 1
            assert result["total_reviewed"] == 1
            assert len(result["jobs"]) == 1
            job = result["jobs"][0]
            assert job["job_name"] == "test-job"
            assert job["build_number"] == 42

    @pytest.mark.asyncio
    async def test_date_filter_excludes(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_totals(date_from="2099-01-01")
            assert result["total_jobs"] == 0

    @pytest.mark.asyncio
    async def test_team_filter(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_totals(team="nonexistent")
            assert result["total_jobs"] == 0

    @pytest.mark.asyncio
    async def test_status_filter(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            # Default (completed only)
            result = await storage.get_report_totals()
            assert result["total_jobs"] == 1

            # Explicit completed
            result = await storage.get_report_totals(status=["completed"])
            assert result["total_jobs"] == 1

            # Failed status
            result = await storage.get_report_totals(status=["failed"])
            assert result["total_jobs"] == 1
            assert result["jobs"][0]["job_name"] == "tagged-job"

            # Multi-status
            result = await storage.get_report_totals(status=["completed", "failed"])
            assert result["total_jobs"] == 2

            # Non-existent status
            result = await storage.get_report_totals(status=["running"])
            assert result["total_jobs"] == 0

    @pytest.mark.asyncio
    async def test_tags_filter(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            # Tags filter on failed job
            result = await storage.get_report_totals(
                status=["failed"], tags=["nightly"]
            )
            assert result["total_jobs"] == 1
            assert result["jobs"][0]["job_name"] == "tagged-job"

            # Non-matching tag
            result = await storage.get_report_totals(
                status=["failed"], tags=["nonexistent"]
            )
            assert result["total_jobs"] == 0


class TestReportOverrides:
    @pytest.mark.asyncio
    async def test_empty_db(self, setup_test_db: Path):
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.get_report_classification_overrides()
            assert result["total"] == 0
            assert result["groups"] == []
            assert result["details"] == []

    @pytest.mark.asyncio
    async def test_with_data(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_classification_overrides()
            assert result["total"] >= 1
            assert len(result["groups"]) >= 1
            detail = result["details"][0]
            assert detail["test_name"] == "test_foo"
            assert detail["from_classification"] == "CODE ISSUE"
            assert detail["to_classification"] == "PRODUCT BUG"
            assert detail["overridden_by"] == "reviewer"

    @pytest.mark.asyncio
    async def test_ai_accuracy(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_classification_overrides()
            # 1 reviewed test, 1 override → ai_correct = 0
            assert "total_reviewed" in result
            assert result["total_reviewed"] >= 1
            assert "ai_correct" in result
            assert "ai_accuracy_pct" in result
            assert result["ai_accuracy_pct"] >= 0
            assert result["ai_accuracy_pct"] <= 100

    @pytest.mark.asyncio
    async def test_same_classification_excluded(self, populated_db: Path):
        """Overrides where AI classification == user classification are excluded."""
        with patch.object(storage, "DB_PATH", populated_db):
            # Add a same→same classification (user confirms AI's choice)
            async with storage._connect_db() as conn:
                await conn.execute(
                    """INSERT INTO test_classifications
                       (test_name, job_name, classification, original_classification,
                        created_by, job_id, visible)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "test_foo",
                        "test-job",
                        "CODE ISSUE",
                        "CODE ISSUE",
                        "confirmer",
                        "job-1",
                        1,
                    ),
                )
                await conn.commit()
            result = await storage.get_report_classification_overrides()
            # Only the real override (CODE ISSUE → PRODUCT BUG) should appear,
            # not the same→same (CODE ISSUE → CODE ISSUE)
            for detail in result["details"]:
                assert detail["from_classification"] != detail["to_classification"], (
                    f"Same→same override should be excluded: {detail['from_classification']} → {detail['to_classification']}"
                )

    @pytest.mark.asyncio
    async def test_multiple_overrides_keeps_latest(self, populated_db: Path):
        """When multiple overrides exist for the same test, only the latest appears."""
        with patch.object(storage, "DB_PATH", populated_db):
            # populated_db already has CODE ISSUE → PRODUCT BUG for test_foo.
            # Add a second override: PRODUCT BUG → INFRASTRUCTURE.
            async with storage._connect_db() as conn:
                await conn.execute(
                    """INSERT INTO test_classifications
                       (test_name, job_name, classification, original_classification,
                        created_by, job_id, visible, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+1 minute'))""",
                    (
                        "test_foo",
                        "test-job",
                        "INFRASTRUCTURE",
                        "PRODUCT BUG",
                        "reviewer2",
                        "job-1",
                        1,
                    ),
                )
                await conn.commit()
            result = await storage.get_report_classification_overrides()
            # Only the latest override (→ INFRASTRUCTURE) should appear for test_foo
            test_foo_details = [
                d for d in result["details"] if d["test_name"] == "test_foo"
            ]
            assert len(test_foo_details) == 1, (
                f"Expected 1 override for test_foo, got {len(test_foo_details)}"
            )
            assert test_foo_details[0]["to_classification"] == "INFRASTRUCTURE"
            assert test_foo_details[0]["overridden_by"] == "reviewer2"

    @pytest.mark.asyncio
    async def test_date_filter_excludes(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_classification_overrides(
                date_from="2099-01-01"
            )
            assert result["total"] == 0


class TestReportIssues:
    @pytest.mark.asyncio
    async def test_empty_db(self, setup_test_db: Path):
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.get_report_issues_created()
            assert result["total"] == 0
            assert result["issues"] == []

    @pytest.mark.asyncio
    async def test_with_data(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_issues_created()
            assert result["total"] == 1
            assert result["github_total"] == 1
            assert result["jira_total"] == 0
            issue = result["issues"][0]
            assert issue["issue_type"] == "GitHub Issue"
            assert issue["title"] == "Fix the bug"
            assert issue["url"] == "https://github.com/org/repo/issues/123"
            assert issue["test_name"] == "test_foo"
            assert issue["created_by"] == "creator"

    @pytest.mark.asyncio
    async def test_date_filter_excludes(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_issues_created(date_from="2099-01-01")
            assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_status_filter(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            # Issues only exist for completed jobs
            result = await storage.get_report_issues_created(status=["completed"])
            assert result["total"] == 1

            # No issues for failed jobs
            result = await storage.get_report_issues_created(status=["failed"])
            assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_xss_javascript_url_rejected(self, setup_test_db: Path):
        """Verify javascript: URLs are filtered out to prevent XSS."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.save_result(
                job_id="job-xss",
                jenkins_url="https://jenkins.example.com/job/test/3/",
                status="completed",
                result={"job_name": "xss-job", "build_number": 1, "failures": []},
            )
            await storage.add_comment(
                job_id="job-xss",
                test_name="test_xss",
                comment="GitHub Issue [GH-1]: [Click me](javascript:alert(document.cookie))",
                username="attacker",
            )
            result = await storage.get_report_issues_created()
            assert result["total"] == 0
            assert result["issues"] == []

    @pytest.mark.asyncio
    async def test_jira_bug_pattern(self, setup_test_db: Path):
        """Test that Jira Bug comments are also detected."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            result_data = {
                "job_name": "jira-job",
                "build_number": 10,
                "failures": [],
            }
            await storage.save_result(
                job_id="job-jira",
                jenkins_url="https://jenkins.example.com/job/test/2/",
                status="completed",
                result=result_data,
            )
            await storage.add_comment(
                job_id="job-jira",
                test_name="test_bar",
                comment="Jira Bug [PROJ-456]: [Login fails](https://jira.example.com/browse/PROJ-456)",
                username="jira_user",
            )
            result = await storage.get_report_issues_created()
            assert result["total"] == 1
            assert result["github_total"] == 0
            assert result["jira_total"] == 1
            issue = result["issues"][0]
            assert issue["issue_type"] == "Jira Bug"
            assert issue["title"] == "Login fails"


class TestDateFilterHelper:
    def test_build_date_filter(self):
        conditions: list[str] = []
        params: list = []
        storage._build_date_filter(
            "col", "2025-01-01", "2025-12-31", conditions, params
        )
        assert len(conditions) == 2
        assert "date(col) >= ?" in conditions[0]
        assert "date(col) <= ?" in conditions[1]
        assert params[0] == "2025-01-01"
        assert params[1] == "2025-12-31"

    def test_build_date_filter_empty(self):
        conditions: list[str] = []
        params: list = []
        storage._build_date_filter("col", "", "", conditions, params)
        assert conditions == []
        assert params == []

    def test_build_metadata_join_empty(self):
        conditions: list[str] = []
        params: list = []
        result = storage._build_metadata_join(
            None, None, None, "t.job_name", conditions, params
        )
        assert result == ""
        assert conditions == []

    def test_build_metadata_join_with_filters(self):
        conditions: list[str] = []
        params: list = []
        result = storage._build_metadata_join(
            ["teamA"], ["1"], None, "t.job_name", conditions, params
        )
        assert "JOIN" in result
        assert len(conditions) == 2
        assert params == ["teamA", "1"]

    def test_build_metadata_join_multi_value(self):
        conditions: list[str] = []
        params: list = []
        result = storage._build_metadata_join(
            ["storage", "core"], ["1", "2"], None, "t.job_name", conditions, params
        )
        assert "JOIN" in result
        assert len(conditions) == 2
        assert "IN" in conditions[0]
        assert "IN" in conditions[1]
        assert params == ["storage", "core", "1", "2"]


class TestReportPagination:
    @pytest.mark.asyncio
    async def test_totals_pagination(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            # Without pagination — all results
            result = await storage.get_report_totals()
            assert len(result["jobs"]) == 1
            assert result["total_details"] == 1

            # With limit=1, offset=0 — first page
            result = await storage.get_report_totals(limit=1, offset=0)
            assert len(result["jobs"]) == 1
            assert result["total_details"] == 1

            # With offset beyond data
            result = await storage.get_report_totals(limit=1, offset=10)
            assert len(result["jobs"]) == 0
            assert result["total_jobs"] == 1  # totals are unaffected

    @pytest.mark.asyncio
    async def test_issues_pagination(self, populated_db: Path):
        with patch.object(storage, "DB_PATH", populated_db):
            result = await storage.get_report_issues_created(limit=10, offset=0)
            assert result["total"] == 1
            assert len(result["issues"]) == 1

            result = await storage.get_report_issues_created(limit=10, offset=10)
            assert result["total"] == 1  # total unaffected
            assert len(result["issues"]) == 0  # but page is empty


class TestDateFilterBoundary:
    @pytest.mark.asyncio
    async def test_same_day_boundary_included(self, populated_db: Path):
        """Records on the exact from-date should be included."""
        with patch.object(storage, "DB_PATH", populated_db):
            # Get today's date from the DB record
            all_results = await storage.get_report_totals()
            assert all_results["total_jobs"] == 1
            created = all_results["jobs"][0]["created_at"]
            today = created[:10]  # YYYY-MM-DD

            # Filtering with from=today should include the record
            result = await storage.get_report_totals(date_from=today)
            assert result["total_jobs"] == 1


class TestHistoryDateFilter:
    @pytest.mark.asyncio
    async def test_get_all_failures_with_dates(self, populated_db: Path):
        """Verify date filtering in get_all_failures."""
        with patch.object(storage, "DB_PATH", populated_db):
            # Without date filter — should find results
            result = await storage.get_all_failures()
            assert result["total"] >= 1

            # With future date filter — should find nothing
            result = await storage.get_all_failures(date_from="2099-01-01")
            assert result["total"] == 0


class TestEffectiveClassification:
    @pytest.mark.asyncio
    async def test_get_all_failures_uses_effective_classification(
        self, setup_test_db: Path
    ):
        """Classification filter uses effective (user override), not AI original."""
        with patch.object(storage, "DB_PATH", setup_test_db):
            # Insert a failure_history row with AI classification
            async with storage._connect_db() as conn:
                await conn.execute(
                    """INSERT INTO failure_history
                       (job_id, job_name, build_number, test_name,
                        error_message, error_signature, classification,
                        child_job_name, child_build_number)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "job-eff",
                        "test-job",
                        1,
                        "test_eff",
                        "AssertionError",
                        "sig-eff",
                        "CODE ISSUE",
                        "",
                        0,
                    ),
                )
                # Insert a user override to FLAKY
                # (job_name='' for top-level, matching fh.child_job_name='')
                await conn.execute(
                    """INSERT INTO test_classifications
                       (test_name, job_name, classification, created_by, job_id,
                        child_build_number, visible)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("test_eff", "", "FLAKY", "user1", "job-eff", 0, 1),
                )
                await conn.commit()

            # Filtering by the user override should find the row
            result = await storage.get_all_failures(classification="FLAKY")
            assert result["total"] >= 1
            assert any(f["test_name"] == "test_eff" for f in result["failures"])

            # Filtering by the AI original should NOT find the row
            result = await storage.get_all_failures(classification="CODE ISSUE")
            test_eff_rows = [
                f for f in result["failures"] if f["test_name"] == "test_eff"
            ]
            assert len(test_eff_rows) == 0
