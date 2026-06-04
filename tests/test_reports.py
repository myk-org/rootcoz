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
                   (test_name, job_name, classification, created_by, job_id, visible)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("test_foo", "test-job", "PRODUCT BUG", "reviewer", "job-1", 1),
            )
            await conn.commit()

        # Insert a comment with a GitHub issue link
        await storage.add_comment(
            job_id="job-1",
            test_name="test_foo",
            comment="GitHub Issue [GH-123]: [Fix the bug](https://github.com/org/repo/issues/123)",
            username="creator",
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
            assert detail["to_classification"] == "PRODUCT BUG"
            assert detail["overridden_by"] == "reviewer"

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
        assert "col >= ?" in conditions[0]
        assert "col <= ?" in conditions[1]
        assert params[0] == "2025-01-01T00:00:00"
        assert params[1] == "2025-12-31T23:59:59"

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
            "", "", "", "t.job_name", conditions, params
        )
        assert result == ""
        assert conditions == []

    def test_build_metadata_join_with_filters(self):
        conditions: list[str] = []
        params: list = []
        result = storage._build_metadata_join(
            "teamA", "1", "", "t.job_name", conditions, params
        )
        assert "JOIN" in result
        assert len(conditions) == 2
        assert params == ["teamA", "1"]


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
