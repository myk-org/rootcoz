"""Tests for storage token usage functions."""

import asyncio
from unittest.mock import patch

import pytest

from jenkins_job_insight import storage


@pytest.fixture
def _init_db(temp_db_path):
    """Initialize database with test path for token usage tests."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def _storage(temp_db_path, _init_db):
    """Patch DB_PATH for all storage calls in the test."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        yield


class TestRecordTokenUsage:
    @pytest.mark.asyncio
    async def test_inserts_record_and_returns_uuid(self, _storage) -> None:
        """record_token_usage inserts a record and returns a UUID."""
        record_id = await storage.record_token_usage(
            job_id="job-1",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
            cost_usd=0.05,
            duration_ms=1200,
            prompt_chars=500,
            response_chars=200,
        )
        assert isinstance(record_id, str)
        assert len(record_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_stored_fields_are_correct(self, _storage) -> None:
        """All fields are stored correctly."""
        await storage.record_token_usage(
            job_id="job-2",
            ai_provider="gemini",
            ai_model="2.5-pro",
            call_type="peer_review",
            input_tokens=200,
            output_tokens=80,
            cache_read_tokens=15,
            cache_write_tokens=3,
            cost_usd=0.03,
            duration_ms=800,
            prompt_chars=1000,
            response_chars=400,
        )
        records = await storage.get_token_usage_for_job("job-2")
        assert len(records) == 1
        rec = records[0]
        assert rec["job_id"] == "job-2"
        assert rec["ai_provider"] == "gemini"
        assert rec["ai_model"] == "2.5-pro"
        assert rec["call_type"] == "peer_review"
        assert rec["input_tokens"] == 200
        assert rec["output_tokens"] == 80
        assert rec["cache_read_tokens"] == 15
        assert rec["cache_write_tokens"] == 3
        assert rec["total_tokens"] == 280
        assert rec["cost_usd"] == pytest.approx(0.03)
        assert rec["duration_ms"] == 800
        assert rec["prompt_chars"] == 1000
        assert rec["response_chars"] == 400

    @pytest.mark.asyncio
    async def test_total_tokens_computed(self, _storage) -> None:
        """total_tokens = input_tokens + output_tokens."""
        await storage.record_token_usage(
            job_id="job-3",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=300,
            output_tokens=150,
        )
        records = await storage.get_token_usage_for_job("job-3")
        assert records[0]["total_tokens"] == 450


class TestGetTokenUsageForJob:
    @pytest.mark.asyncio
    async def test_returns_records_for_job(self, _storage) -> None:
        """Returns all records for a specific job."""
        await storage.record_token_usage(
            job_id="job-a",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
        )
        await storage.record_token_usage(
            job_id="job-a",
            ai_provider="gemini",
            ai_model="2.5-pro",
            call_type="peer_review",
            input_tokens=200,
            output_tokens=80,
        )
        records = await storage.get_token_usage_for_job("job-a")
        assert len(records) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_nonexistent_job(self, _storage) -> None:
        """Returns empty list for non-existent job."""
        records = await storage.get_token_usage_for_job("nonexistent-job")
        assert records == []

    @pytest.mark.asyncio
    async def test_does_not_return_other_jobs(self, _storage) -> None:
        """Records from other jobs are not included."""
        await storage.record_token_usage(
            job_id="job-x",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
        )
        await storage.record_token_usage(
            job_id="job-y",
            ai_provider="claude",
            ai_model="opus",
            call_type="analysis",
            input_tokens=200,
            output_tokens=80,
        )
        records = await storage.get_token_usage_for_job("job-x")
        assert len(records) == 1
        assert records[0]["job_id"] == "job-x"


class TestGetTokenUsageSummary:
    @pytest.mark.asyncio
    async def _insert_test_records(self) -> None:
        """Helper to insert test records for summary tests."""
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.05,
            duration_ms=1000,
        )
        await storage.record_token_usage(
            job_id="job-2",
            ai_provider="gemini",
            ai_model="2.5-pro",
            call_type="peer_review",
            input_tokens=200,
            output_tokens=80,
            cost_usd=0.03,
            duration_ms=800,
        )
        await storage.record_token_usage(
            job_id="job-1",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="jira_filter",
            input_tokens=50,
            output_tokens=20,
            cost_usd=0.01,
            duration_ms=300,
        )

    @pytest.mark.asyncio
    async def test_totals_correct(self, _storage) -> None:
        """Totals are correctly aggregated."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary()
        assert summary["total_calls"] == 3
        assert summary["total_input_tokens"] == 350
        assert summary["total_output_tokens"] == 150
        assert summary["total_cost_usd"] == pytest.approx(0.09)

    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self, _storage) -> None:
        """Empty database returns zero totals."""
        summary = await storage.get_token_usage_summary()
        assert summary["total_calls"] == 0
        assert summary["total_input_tokens"] == 0
        assert summary["total_cost_usd"] == 0

    @pytest.mark.asyncio
    async def test_filter_by_provider(self, _storage) -> None:
        """Filter by ai_provider works."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(ai_provider="claude")
        assert summary["total_calls"] == 2
        assert summary["total_input_tokens"] == 150

    @pytest.mark.asyncio
    async def test_filter_by_call_type(self, _storage) -> None:
        """Filter by call_type works."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(call_type="analysis")
        assert summary["total_calls"] == 1
        assert summary["total_input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_filter_by_model(self, _storage) -> None:
        """Filter by ai_model works."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(ai_model="2.5-pro")
        assert summary["total_calls"] == 1
        assert summary["total_input_tokens"] == 200

    @pytest.mark.asyncio
    async def test_group_by_provider(self, _storage) -> None:
        """group_by=provider produces breakdown by provider."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(group_by="provider")
        breakdown = summary["breakdown"]
        assert len(breakdown) == 2
        keys = {row["group_key"] for row in breakdown}
        assert "claude" in keys
        assert "gemini" in keys

    @pytest.mark.asyncio
    async def test_group_by_call_type(self, _storage) -> None:
        """group_by=call_type produces breakdown by call_type."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(group_by="call_type")
        breakdown = summary["breakdown"]
        assert len(breakdown) == 3
        keys = {row["group_key"] for row in breakdown}
        assert "analysis" in keys
        assert "peer_review" in keys
        assert "jira_filter" in keys

    @pytest.mark.asyncio
    async def test_group_by_job(self, _storage) -> None:
        """group_by=job produces breakdown by job_id."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary(group_by="job")
        breakdown = summary["breakdown"]
        assert len(breakdown) == 2
        keys = {row["group_key"] for row in breakdown}
        assert "job-1" in keys
        assert "job-2" in keys

    @pytest.mark.asyncio
    async def test_no_breakdown_without_group_by(self, _storage) -> None:
        """No breakdown returned when group_by is not specified."""
        await self._insert_test_records()
        summary = await storage.get_token_usage_summary()
        assert summary["breakdown"] == []


class TestGetTokenUsageDashboardSummary:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self, _storage) -> None:
        """Dashboard summary returns today, this_week, this_month, top_models, top_jobs."""
        result = await storage.get_token_usage_dashboard_summary()
        assert "today" in result
        assert "this_week" in result
        assert "this_month" in result
        assert "top_models" in result
        assert "top_jobs" in result

    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self, _storage) -> None:
        """Empty database returns zero values."""
        result = await storage.get_token_usage_dashboard_summary()
        assert result["today"]["calls"] == 0
        assert result["this_week"]["calls"] == 0
        assert result["this_month"]["calls"] == 0
        assert result["top_models"] == []
        assert result["top_jobs"] == []

    @pytest.mark.asyncio
    async def test_recent_records_appear_in_periods(self, _storage) -> None:
        """Records inserted now appear in today, this_week, and this_month."""
        await storage.record_token_usage(
            job_id="job-dash",
            ai_provider="claude",
            ai_model="opus-4",
            call_type="analysis",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.05,
        )
        result = await storage.get_token_usage_dashboard_summary()
        assert result["today"]["calls"] == 1
        assert result["this_week"]["calls"] == 1
        assert result["this_month"]["calls"] == 1
        assert len(result["top_models"]) == 1
        assert result["top_models"][0]["model"] == "claude / opus-4"
        assert len(result["top_jobs"]) == 1
        assert result["top_jobs"][0]["job_id"] == "job-dash"
