"""Tests for comment and review storage functions."""

from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from jenkins_job_insight import storage


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


class TestCommentTables:
    async def test_comments_table_exists(self, setup_test_db, temp_db_path):
        async with aiosqlite.connect(temp_db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='comments'"
            )
            row = await cursor.fetchone()
            assert row is not None, "comments table should exist"

    async def test_failure_reviews_table_exists(self, setup_test_db, temp_db_path):
        async with aiosqlite.connect(temp_db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='failure_reviews'"
            )
            row = await cursor.fetchone()
            assert row is not None, "failure_reviews table should exist"


class TestCommentCRUD:
    async def test_add_comment(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            comment_id = await storage.add_comment(
                job_id="job-1",
                test_name="tests.TestFoo.test_bar",
                comment="Opened bug: OCPBUGS-123",
            )
            assert isinstance(comment_id, int)
            assert comment_id > 0

    async def test_add_comment_with_child_job(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            comment_id = await storage.add_comment(
                job_id="job-1",
                test_name="tests.TestFoo.test_bar",
                comment="Fix PR merged",
                child_job_name="child-job-1",
                child_build_number=42,
            )
            assert isinstance(comment_id, int)

    async def test_get_comments_for_job(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.add_comment("job-1", "test_a", "comment 1")
            await storage.add_comment("job-1", "test_b", "comment 2")
            await storage.add_comment("job-2", "test_a", "other job comment")

            comments = await storage.get_comments_for_job("job-1")
            assert len(comments) == 2
            assert comments[0]["test_name"] == "test_a"
            assert comments[0]["comment"] == "comment 1"

    async def test_get_comments_empty(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            comments = await storage.get_comments_for_job("nonexistent")
            assert comments == []


class TestReviewedToggle:
    async def test_set_reviewed(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed(
                "job-1", "test_a", reviewed=True, username="alice"
            )
            reviews = await storage.get_reviews_for_job("job-1")
            assert "test_a" in reviews
            assert reviews["test_a"]["reviewed"] is True
            assert reviews["test_a"]["username"] == "alice"

    async def test_set_reviewed_without_username(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed("job-1", "test_a", reviewed=True)
            reviews = await storage.get_reviews_for_job("job-1")
            assert "test_a" in reviews
            assert reviews["test_a"]["reviewed"] is True
            assert reviews["test_a"]["username"] == ""

    async def test_unset_reviewed(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed(
                "job-1", "test_a", reviewed=True, username="alice"
            )
            await storage.set_reviewed(
                "job-1", "test_a", reviewed=False, username="bob"
            )
            reviews = await storage.get_reviews_for_job("job-1")
            assert reviews["test_a"]["reviewed"] is False
            assert reviews["test_a"]["username"] == "bob"

    async def test_set_reviewed_with_child_job(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed(
                "job-1",
                "test_a",
                reviewed=True,
                child_job_name="child-1",
                child_build_number=42,
                username="carol",
            )
            reviews = await storage.get_reviews_for_job("job-1")
            key = "child-1#42::test_a"
            assert key in reviews
            assert reviews[key]["reviewed"] is True
            assert reviews[key]["username"] == "carol"

    async def test_get_reviews_empty(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            reviews = await storage.get_reviews_for_job("nonexistent")
            assert reviews == {}


class TestReviewStatus:
    async def test_get_review_status(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            from jenkins_job_insight.models import (
                AnalysisDetail,
                AnalysisResult,
                FailureAnalysis,
            )

            result = AnalysisResult(
                job_id="job-1",
                job_name="test-job",
                build_number=1,
                status="completed",
                summary="test",
                failures=[
                    FailureAnalysis(
                        test_name="test_a",
                        error="err",
                        analysis=AnalysisDetail(classification="CODE ISSUE"),
                    ),
                    FailureAnalysis(
                        test_name="test_b",
                        error="err",
                        analysis=AnalysisDetail(classification="PRODUCT BUG"),
                    ),
                ],
            )
            await storage.save_result(
                "job-1", "http://jenkins", "completed", result.model_dump()
            )

            await storage.set_reviewed("job-1", "test_a", reviewed=True)
            await storage.add_comment("job-1", "test_a", "bug opened")
            await storage.add_comment("job-1", "test_b", "investigating")

            status = await storage.get_review_status("job-1")
            assert status["total_failures"] == 2
            assert status["reviewed_count"] == 1
            assert status["comment_count"] == 2

    async def test_get_review_status_no_data(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            status = await storage.get_review_status("nonexistent")
            assert status["total_failures"] == 0
            assert status["reviewed_count"] == 0
            assert status["comment_count"] == 0


class TestHistoricalComments:
    async def test_get_historical_comments_by_test_name(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.add_comment("job-1", "test_a", "bug: OCPBUGS-100")
            await storage.add_comment("job-2", "test_a", "fix merged: PR #50")
            await storage.add_comment("job-3", "test_b", "unrelated")

            comments = await storage.get_historical_comments(test_names=["test_a"])
            assert len(comments) == 2

    async def test_get_historical_comments_by_error_signature(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.add_comment(
                "job-1", "test_a", "bug opened", error_signature="sig123"
            )
            await storage.add_comment(
                "job-2", "test_b", "same error different test", error_signature="sig123"
            )

            comments = await storage.get_historical_comments(
                error_signatures=["sig123"],
            )
            assert len(comments) == 2

    async def test_get_historical_comments_excludes_current_job(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.add_comment("job-1", "test_a", "old comment")
            await storage.add_comment("job-current", "test_a", "current job comment")

            comments = await storage.get_historical_comments(
                test_names=["test_a"],
                exclude_job_id="job-current",
            )
            assert len(comments) == 1
            assert comments[0]["job_id"] == "job-1"
