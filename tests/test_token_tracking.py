"""Tests for token_tracking module."""

from unittest.mock import AsyncMock, patch

import pytest
from ai_cli_runner import AIResult, AITokenUsage

from jenkins_job_insight.token_tracking import (
    build_token_usage_summary,
    record_ai_usage,
)


class TestRecordAiUsage:
    """Tests for record_ai_usage."""

    @pytest.mark.asyncio
    async def test_records_usage_when_present(self) -> None:
        """Records token usage when AIResult has usage data."""
        usage = AITokenUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
            cost_usd=0.05,
            duration_ms=1200,
            provider="claude",
            model="opus-4",
        )
        result = AIResult(success=True, text="analysis output", usage=usage)

        with patch(
            "jenkins_job_insight.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
        ) as mock_record:
            await record_ai_usage(
                job_id="job-123",
                result=result,
                call_type="analysis",
                prompt_chars=500,
            )
            mock_record.assert_called_once_with(
                job_id="job-123",
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
                response_chars=len("analysis output"),
            )

    @pytest.mark.asyncio
    async def test_records_zeros_when_usage_is_none(self) -> None:
        """Records with zero token fields when usage is None."""
        result = AIResult(success=True, text="output")
        assert result.usage is None

        with patch(
            "jenkins_job_insight.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
        ) as mock_record:
            await record_ai_usage(
                job_id="job-123",
                result=result,
                call_type="analysis",
                ai_provider="claude",
                ai_model="opus-4",
            )
            mock_record.assert_called_once_with(
                job_id="job-123",
                ai_provider="claude",
                ai_model="opus-4",
                call_type="analysis",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=None,
                duration_ms=None,
                prompt_chars=0,
                response_chars=len("output"),
            )

    @pytest.mark.asyncio
    async def test_skips_when_job_id_empty(self) -> None:
        """Does nothing when job_id is empty."""
        usage = AITokenUsage(
            input_tokens=100, output_tokens=50, provider="claude", model="opus"
        )
        result = AIResult(success=True, text="output", usage=usage)

        with patch(
            "jenkins_job_insight.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
        ) as mock_record:
            await record_ai_usage(job_id="", result=result, call_type="analysis")
            mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_best_effort_errors_dont_propagate(self) -> None:
        """Errors in storage do not propagate."""
        usage = AITokenUsage(
            input_tokens=100, output_tokens=50, provider="claude", model="opus"
        )
        result = AIResult(success=True, text="output", usage=usage)

        with patch(
            "jenkins_job_insight.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB unavailable"),
        ):
            # Should not raise
            await record_ai_usage(job_id="job-123", result=result, call_type="analysis")

    @pytest.mark.asyncio
    async def test_falls_back_to_parameter_provider_model(self) -> None:
        """Uses parameter provider/model when usage lacks them."""
        usage = AITokenUsage(input_tokens=100, output_tokens=50, provider="", model="")
        result = AIResult(success=True, text="output", usage=usage)

        with patch(
            "jenkins_job_insight.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
        ) as mock_record:
            await record_ai_usage(
                job_id="job-123",
                result=result,
                call_type="peer_review",
                ai_provider="gemini",
                ai_model="2.5-pro",
            )
            call_kwargs = mock_record.call_args.kwargs
            assert call_kwargs["ai_provider"] == "gemini"
            assert call_kwargs["ai_model"] == "2.5-pro"
            assert call_kwargs["call_type"] == "peer_review"

    @pytest.mark.asyncio
    async def test_records_correct_response_chars(self) -> None:
        """response_chars is derived from len(result.text)."""
        long_text = "x" * 12345
        usage = AITokenUsage(
            input_tokens=10, output_tokens=20, provider="claude", model="m"
        )
        result = AIResult(success=True, text=long_text, usage=usage)

        with patch(
            "jenkins_job_insight.token_tracking.storage.record_token_usage",
            new_callable=AsyncMock,
        ) as mock_record:
            await record_ai_usage(job_id="job-1", result=result, call_type="analysis")
            assert mock_record.call_args.kwargs["response_chars"] == 12345


class TestBuildTokenUsageSummary:
    """Tests for build_token_usage_summary."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_records(self) -> None:
        """Returns None when no records exist."""
        with patch(
            "jenkins_job_insight.token_tracking.storage.get_token_usage_for_job",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await build_token_usage_summary("job-123")
            assert result is None

    @pytest.mark.asyncio
    async def test_builds_correct_summary(self) -> None:
        """Builds correct summary from multiple records."""
        records = [
            {
                "ai_provider": "claude",
                "ai_model": "opus-4",
                "call_type": "analysis",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_write_tokens": 5,
                "total_tokens": 150,
                "cost_usd": 0.05,
                "duration_ms": 1200,
            },
            {
                "ai_provider": "gemini",
                "ai_model": "2.5-pro",
                "call_type": "peer_review",
                "input_tokens": 200,
                "output_tokens": 80,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 280,
                "cost_usd": 0.03,
                "duration_ms": 800,
            },
        ]
        with patch(
            "jenkins_job_insight.token_tracking.storage.get_token_usage_for_job",
            new_callable=AsyncMock,
            return_value=records,
        ):
            summary = await build_token_usage_summary("job-123")

        assert summary is not None
        assert summary.total_calls == 2
        assert summary.total_input_tokens == 300
        assert summary.total_output_tokens == 130
        assert summary.total_cache_read_tokens == 10
        assert summary.total_cache_write_tokens == 5
        assert summary.total_tokens == 430
        assert summary.total_cost_usd == pytest.approx(0.08)
        assert summary.total_duration_ms == 2000
        assert len(summary.calls) == 2
        assert summary.calls[0].provider == "claude"
        assert summary.calls[1].call_type == "peer_review"

    @pytest.mark.asyncio
    async def test_handles_none_cost_usd(self) -> None:
        """Sets total cost to None if any call lacks cost."""
        records = [
            {
                "ai_provider": "claude",
                "ai_model": "opus-4",
                "call_type": "analysis",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 150,
                "cost_usd": 0.05,
                "duration_ms": 1000,
            },
            {
                "ai_provider": "gemini",
                "ai_model": "2.5-pro",
                "call_type": "analysis",
                "input_tokens": 200,
                "output_tokens": 80,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 280,
                "cost_usd": None,
                "duration_ms": 500,
            },
        ]
        with patch(
            "jenkins_job_insight.token_tracking.storage.get_token_usage_for_job",
            new_callable=AsyncMock,
            return_value=records,
        ):
            summary = await build_token_usage_summary("job-123")

        assert summary is not None
        assert summary.total_cost_usd is None
        # Other totals should still be correct
        assert summary.total_input_tokens == 300

    @pytest.mark.asyncio
    async def test_returns_none_on_storage_error(self) -> None:
        """Returns None when storage raises an exception."""
        with patch(
            "jenkins_job_insight.token_tracking.storage.get_token_usage_for_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB error"),
        ):
            result = await build_token_usage_summary("job-123")
            assert result is None

    @pytest.mark.asyncio
    async def test_handles_none_duration_ms(self) -> None:
        """None duration_ms values are treated as 0 in totals."""
        records = [
            {
                "ai_provider": "claude",
                "ai_model": "opus-4",
                "call_type": "analysis",
                "input_tokens": 50,
                "output_tokens": 20,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 70,
                "cost_usd": 0.01,
                "duration_ms": None,
            },
        ]
        with patch(
            "jenkins_job_insight.token_tracking.storage.get_token_usage_for_job",
            new_callable=AsyncMock,
            return_value=records,
        ):
            summary = await build_token_usage_summary("job-123")

        assert summary is not None
        assert summary.total_duration_ms == 0
