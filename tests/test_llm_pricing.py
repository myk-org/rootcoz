"""Tests for llm_pricing module."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from ai_cli_runner import AIResult, AITokenUsage

from jenkins_job_insight.llm_pricing import LLMPricingCache
from jenkins_job_insight.token_tracking import record_ai_usage


SAMPLE_PRICING_DATA = {
    "claude-opus-4": {
        "input_cost_per_token": 0.000015,
        "output_cost_per_token": 0.000075,
        "cache_read_input_token_cost": 0.0000015,
        "cache_creation_input_token_cost": 0.00001875,
    },
    "claude-opus-4-6": {
        "input_cost_per_token": 0.000016,
        "output_cost_per_token": 0.000080,
    },
    "anthropic/claude-sonnet-4": {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_read_input_token_cost": 0.0000003,
        "cache_creation_input_token_cost": 0.00000375,
    },
    "claude-haiku-4-5-20251001": {
        "input_cost_per_token": 0.0000008,
        "output_cost_per_token": 0.000004,
    },
    "gemini/gemini-2.5-pro": {
        "input_cost_per_token": 0.00000125,
        "output_cost_per_token": 0.00001,
    },
    "gemini-2.5-pro": {
        "input_cost_per_token": 0.00000125,
        "output_cost_per_token": 0.00001,
    },
    "gpt-5.4": {
        "input_cost_per_token": 0.000005,
        "output_cost_per_token": 0.000015,
    },
    "no-costs-model": {
        "max_tokens": 4096,
    },
}


class TestLLMPricingCache:
    """Tests for LLMPricingCache."""

    def test_calculate_cost_direct_model_name(self) -> None:
        """Cost calculation with direct model name lookup."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="claude-opus-4",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is not None
        expected = (1000 * 0.000015) + (500 * 0.000075)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_provider_prefixed(self) -> None:
        """Cost calculation with provider-prefixed model name."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="claude-sonnet-4",
            input_tokens=2000,
            output_tokens=1000,
        )

        assert cost is not None
        expected = (2000 * 0.000003) + (1000 * 0.000015)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_gemini_prefixed(self) -> None:
        """Cost calculation with gemini provider prefix."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="gemini",
            model="gemini-2.5-pro",
            input_tokens=5000,
            output_tokens=2000,
        )

        assert cost is not None
        expected = (5000 * 0.00000125) + (2000 * 0.00001)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_model_with_slash(self) -> None:
        """Model name containing slash falls back to suffix lookup."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        # Model already has provider prefix "gemini/gemini-2.5-pro"
        cost = cache.calculate_cost(
            provider="gemini",
            model="gemini/gemini-2.5-pro",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is not None
        expected = (1000 * 0.00000125) + (500 * 0.00001)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_with_cache_tokens(self) -> None:
        """Cost calculation includes cache read and write costs."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="claude-opus-4",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_write_tokens=100,
        )

        assert cost is not None
        expected = (
            (1000 * 0.000015)
            + (500 * 0.000075)
            + (200 * 0.0000015)
            + (100 * 0.00001875)
        )
        assert cost == pytest.approx(expected)

    def test_calculate_cost_no_cache_costs_in_pricing(self) -> None:
        """Cache tokens ignored when pricing doesn't have cache costs."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="gemini",
            model="gemini-2.5-pro",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_write_tokens=100,
        )

        assert cost is not None
        # Cache costs not in gemini pricing, so only input + output
        expected = (1000 * 0.00000125) + (500 * 0.00001)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_model_not_found(self) -> None:
        """Returns None when model is not in pricing data."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="nonexistent-model",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is None

    def test_calculate_cost_empty_cache(self) -> None:
        """Returns None when cache is empty (never fetched)."""
        cache = LLMPricingCache()

        cost = cache.calculate_cost(
            provider="claude",
            model="claude-opus-4",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is None

    def test_calculate_cost_missing_cost_fields(self) -> None:
        """Returns None when model entry lacks cost fields."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="no-costs-model",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is None

    def test_calculate_cost_empty_model(self) -> None:
        """Returns None when model name is empty."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is None

    def test_calculate_cost_bracketed_suffix_stripped(self) -> None:
        """CLI model name with [1m] suffix matches after stripping."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="claude-opus-4-6[1m]",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is not None
        expected = (1000 * 0.000016) + (500 * 0.000080)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_at_sign_replaced(self) -> None:
        """CLI model name with @ separator matches after replacing with -."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="claude",
            model="claude-haiku-4-5@20251001",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is not None
        expected = (1000 * 0.0000008) + (500 * 0.000004)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_cursor_routing_suffix_stripped(self) -> None:
        """Cursor model with routing suffix matches after stripping."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="cursor",
            model="gpt-5.4-xhigh-fast",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is not None
        expected = (1000 * 0.000005) + (500 * 0.000015)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_cursor_claude_resolved(self) -> None:
        """Cursor claude model with reordered name resolves to canonical key."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="cursor",
            model="claude-4.6-opus-max-thinking",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is not None
        expected = (1000 * 0.000016) + (500 * 0.000080)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_cursor_gemini_resolved(self) -> None:
        """Cursor gemini model with routing suffix resolves to canonical key."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="cursor",
            model="gemini-2.5-pro-fast",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is not None
        expected = (1000 * 0.00000125) + (500 * 0.00001)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_cursor_proprietary_model_returns_none(self) -> None:
        """Cursor proprietary model with no LiteLLM match returns None."""
        cache = LLMPricingCache()
        cache._data = SAMPLE_PRICING_DATA

        cost = cache.calculate_cost(
            provider="cursor",
            model="composer-2-fast",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost is None

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        """Successfully loads pricing data from URL."""
        cache = LLMPricingCache()

        mock_response = httpx.Response(
            status_code=200,
            json=SAMPLE_PRICING_DATA,
            request=httpx.Request("GET", "https://example.com"),
        )

        with patch("jenkins_job_insight.llm_pricing.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value.get = AsyncMock(return_value=mock_response)

            await cache.load()

        assert len(cache._data) == len(SAMPLE_PRICING_DATA)
        assert "claude-opus-4" in cache._data

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_raise(self) -> None:
        """Fetch failure is logged but never raises."""
        cache = LLMPricingCache()

        with patch("jenkins_job_insight.llm_pricing.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )

            # Should NOT raise
            await cache.load()

        assert cache._data == {}

    @pytest.mark.asyncio
    async def test_fetch_bad_json_does_not_raise(self) -> None:
        """Non-dict JSON response is handled gracefully."""
        cache = LLMPricingCache()

        mock_response = httpx.Response(
            status_code=200,
            json=["not", "a", "dict"],
            request=httpx.Request("GET", "https://example.com"),
        )

        with patch("jenkins_job_insight.llm_pricing.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client.return_value
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value.get = AsyncMock(return_value=mock_response)

            await cache.load()

        assert cache._data == {}


class TestRecordAiUsageCostCalculation:
    """Tests for cost calculation integration in record_ai_usage."""

    @pytest.mark.asyncio
    async def test_cli_provided_cost_not_overridden(self) -> None:
        """CLI-provided cost_usd is used as-is, not recalculated."""
        usage = AITokenUsage(
            input_tokens=1000,
            output_tokens=500,
            provider="claude",
            model="claude-opus-4",
            cost_usd=0.42,
        )
        result = AIResult(success=True, text="output", usage=usage)

        with (
            patch(
                "jenkins_job_insight.token_tracking.storage.record_token_usage",
                new_callable=AsyncMock,
            ) as mock_record,
            patch("jenkins_job_insight.token_tracking.pricing_cache") as mock_cache,
        ):
            await record_ai_usage(job_id="job-123", result=result, call_type="analysis")

            # pricing_cache.calculate_cost should NOT be called
            mock_cache.calculate_cost.assert_not_called()
            # CLI-provided cost should be passed through
            assert mock_record.call_args.kwargs["cost_usd"] == 0.42

    @pytest.mark.asyncio
    async def test_cost_calculated_when_cli_provides_none(self) -> None:
        """When CLI provides no cost, pricing cache is used."""
        usage = AITokenUsage(
            input_tokens=1000,
            output_tokens=500,
            provider="claude",
            model="claude-opus-4",
            cost_usd=None,
        )
        result = AIResult(success=True, text="output", usage=usage)

        with (
            patch(
                "jenkins_job_insight.token_tracking.storage.record_token_usage",
                new_callable=AsyncMock,
            ) as mock_record,
            patch("jenkins_job_insight.token_tracking.pricing_cache") as mock_cache,
        ):
            mock_cache.calculate_cost.return_value = 0.055
            await record_ai_usage(job_id="job-123", result=result, call_type="analysis")

            mock_cache.calculate_cost.assert_called_once_with(
                provider="claude",
                model="claude-opus-4",
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            assert mock_record.call_args.kwargs["cost_usd"] == 0.055

    @pytest.mark.asyncio
    async def test_cost_stays_none_when_cache_returns_none(self) -> None:
        """Cost stays None when pricing cache can't find the model."""
        usage = AITokenUsage(
            input_tokens=1000,
            output_tokens=500,
            provider="gemini",
            model="unknown-model",
            cost_usd=None,
        )
        result = AIResult(success=True, text="output", usage=usage)

        with (
            patch(
                "jenkins_job_insight.token_tracking.storage.record_token_usage",
                new_callable=AsyncMock,
            ) as mock_record,
            patch("jenkins_job_insight.token_tracking.pricing_cache") as mock_cache,
        ):
            mock_cache.calculate_cost.return_value = None
            await record_ai_usage(job_id="job-123", result=result, call_type="analysis")

            assert mock_record.call_args.kwargs["cost_usd"] is None

    @pytest.mark.asyncio
    async def test_cost_calculation_error_stays_none(self) -> None:
        """Cost stays None when pricing cache raises an exception."""
        usage = AITokenUsage(
            input_tokens=1000,
            output_tokens=500,
            provider="claude",
            model="claude-opus-4",
            cost_usd=None,
        )
        result = AIResult(success=True, text="output", usage=usage)

        with (
            patch(
                "jenkins_job_insight.token_tracking.storage.record_token_usage",
                new_callable=AsyncMock,
            ) as mock_record,
            patch("jenkins_job_insight.token_tracking.pricing_cache") as mock_cache,
        ):
            mock_cache.calculate_cost.side_effect = RuntimeError("unexpected")
            await record_ai_usage(job_id="job-123", result=result, call_type="analysis")

            assert mock_record.call_args.kwargs["cost_usd"] is None

    @pytest.mark.asyncio
    async def test_no_cost_calculation_when_usage_is_none(self) -> None:
        """No cost calculation attempted when usage is None."""
        result = AIResult(success=True, text="output")
        assert result.usage is None

        with (
            patch(
                "jenkins_job_insight.token_tracking.storage.record_token_usage",
                new_callable=AsyncMock,
            ) as mock_record,
            patch("jenkins_job_insight.token_tracking.pricing_cache") as mock_cache,
        ):
            await record_ai_usage(
                job_id="job-123",
                result=result,
                call_type="analysis",
                ai_provider="claude",
                ai_model="opus-4",
            )

            mock_cache.calculate_cost.assert_not_called()
            assert mock_record.call_args.kwargs["cost_usd"] is None

    @pytest.mark.asyncio
    async def test_cost_with_cache_tokens(self) -> None:
        """Cache read/write tokens are passed to cost calculation."""
        usage = AITokenUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_write_tokens=100,
            provider="claude",
            model="claude-opus-4",
            cost_usd=None,
        )
        result = AIResult(success=True, text="output", usage=usage)

        with (
            patch(
                "jenkins_job_insight.token_tracking.storage.record_token_usage",
                new_callable=AsyncMock,
            ),
            patch("jenkins_job_insight.token_tracking.pricing_cache") as mock_cache,
        ):
            mock_cache.calculate_cost.return_value = 0.07
            await record_ai_usage(job_id="job-123", result=result, call_type="analysis")

            mock_cache.calculate_cost.assert_called_once_with(
                provider="claude",
                model="claude-opus-4",
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=200,
                cache_write_tokens=100,
            )
