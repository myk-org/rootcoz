"""Tests for comment and review storage functions."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from rootcoz import storage
from rootcoz.models import (
    AddCommentRequest,
    CreateIssueRequest,
    OverrideClassificationRequest,
    OverridePatternRequest,
    PreviewIssueRequest,
    SetReviewedRequest,
    SetTrackedInRequest,
)


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


class TestFirstReviewedAt:
    """``first_reviewed_at`` is stamped once and never rewritten (#566)."""

    async def test_written_once_and_kept_across_unmark_and_remark(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed(
                "job-1", "test_a", reviewed=True, username="alice"
            )
            first = (await storage.get_reviews_for_job("job-1"))["test_a"][
                "reviewed_at"
            ]
            assert first and first.endswith("Z")

            await storage.set_reviewed(
                "job-1", "test_a", reviewed=False, username="bob"
            )
            unmarked = (await storage.get_reviews_for_job("job-1"))["test_a"]
            assert unmarked["reviewed"] is False
            # Retained on the row, so a re-mark restores the original moment.
            assert unmarked["reviewed_at"] == first

            await storage.set_reviewed("job-1", "test_a", reviewed=True, username="bob")
            remarked = (await storage.get_reviews_for_job("job-1"))["test_a"]
            assert remarked["reviewed"] is True
            assert remarked["reviewed_at"] == first

    async def test_review_dict_wire_shape(self, setup_test_db):
        """The GET /results/{job_id}/comments review entry shape (#566).

        Pinned here because ``models.ReviewState`` is not a response model of
        that route (it returns a plain dict), so the dict *is* the contract.
        """
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed(
                "job-1", "test_a", reviewed=True, username="alice"
            )
            reviews = await storage.get_reviews_for_job("job-1")
            assert set(reviews["test_a"]) == {
                "reviewed",
                "username",
                "updated_at",
                "reviewed_at",
            }

    async def test_legacy_null_first_reviewed_at_never_fabricated(self, setup_test_db):
        """A pre-#566 row keeps ``NULL`` even when re-marked (no backfill)."""
        with sqlite3.connect(setup_test_db) as conn:
            conn.execute(
                "INSERT INTO failure_reviews"
                " (job_id, test_name, reviewed, username)"
                " VALUES ('job-legacy', 'test_a', 1, 'alice')"
            )

        with patch.object(storage, "DB_PATH", setup_test_db):
            before = (await storage.get_reviews_for_job("job-legacy"))["test_a"]
            assert before["reviewed"] is True
            assert before["reviewed_at"] is None

            # An idempotent / bulk re-mark is not evidence of when it happened.
            await storage.set_reviewed(
                "job-legacy", "test_a", reviewed=True, username="bob"
            )
            after = (await storage.get_reviews_for_job("job-legacy"))["test_a"]
            assert after["reviewed"] is True
            assert after["username"] == "bob"
            assert after["reviewed_at"] is None

    async def test_legacy_null_anchor_survives_an_unmark_and_remark(
        self, setup_test_db
    ):
        """Unmarking a legacy review must not let the re-mark invent a moment."""
        with sqlite3.connect(setup_test_db) as conn:
            conn.execute(
                "INSERT INTO failure_reviews"
                " (job_id, test_name, reviewed, username)"
                " VALUES ('job-legacy', 'test_a', 1, 'alice')"
            )

        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed(
                "job-legacy", "test_a", reviewed=False, username="bob"
            )
            unmarked = (await storage.get_reviews_for_job("job-legacy"))["test_a"]
            assert unmarked["reviewed"] is False
            assert unmarked["reviewed_at"] is None

            await storage.set_reviewed(
                "job-legacy", "test_a", reviewed=True, username="bob"
            )
            remarked = (await storage.get_reviews_for_job("job-legacy"))["test_a"]
            assert remarked["reviewed"] is True
            assert remarked["reviewed_at"] is None

    async def test_born_unreviewed_row_stamps_on_first_mark(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_reviewed("job-1", "test_a", reviewed=False, username="a")
            assert (await storage.get_reviews_for_job("job-1"))["test_a"][
                "reviewed_at"
            ] is None

            await storage.set_reviewed("job-1", "test_a", reviewed=True, username="a")
            stamped = (await storage.get_reviews_for_job("job-1"))["test_a"][
                "reviewed_at"
            ]
            assert stamped and stamped.endswith("Z")


class TestReviewIdentifierBounds:
    """``child_job_name``/``test_name`` bounds are shared by every body (#566)."""

    @pytest.mark.parametrize(
        "model",
        [
            AddCommentRequest,
            PreviewIssueRequest,
            CreateIssueRequest,
            OverrideClassificationRequest,
            OverridePatternRequest,
            SetTrackedInRequest,
        ],
    )
    def test_child_job_name_is_bounded_on_every_body(self, model):
        from pydantic import ValidationError

        base = {"child_job_name": "c" * 500}
        if model is AddCommentRequest:
            base |= {"test_name": "t", "comment": "c"}
        elif model is PreviewIssueRequest:
            base |= {"test_name": "t"}
        elif model is CreateIssueRequest:
            base |= {"test_name": "t", "title": "T", "body": "b"}
        elif model is OverrideClassificationRequest:
            base |= {"test_name": "t", "classification": "CODE ISSUE"}
        elif model is OverridePatternRequest:
            base |= {"test_name": "t", "pattern": "NEW"}
        elif model is SetTrackedInRequest:
            base |= {"test_name": "t", "url": "https://x/y", "type": "github"}
        model(**base)

        with pytest.raises(ValidationError):
            model(**{**base, "child_job_name": "c" * 501})

    def test_review_body_bounds(self):
        from pydantic import ValidationError

        # A stored failure name has no length limit, so the review body must be
        # able to address any of them (a 2000-character cap made longer names
        # permanently unreviewable).
        SetReviewedRequest(test_name="t" * 5000, reviewed=True)
        with pytest.raises(ValidationError):
            SetReviewedRequest(test_name="t", reviewed=True, child_job_name="c" * 501)


class TestReviewStatus:
    async def test_get_review_status(self, setup_test_db):
        with patch.object(storage, "DB_PATH", setup_test_db):
            from rootcoz.models import (
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
