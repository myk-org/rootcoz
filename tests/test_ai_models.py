"""Tests for the AI model listing and caching module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jenkins_job_insight.ai_models import AIModelCache, _model_id_to_display_name


# -- Display name helper ------------------------------------------------------


class TestModelIdToDisplayName:
    def test_simple_model(self):
        assert _model_id_to_display_name("claude-sonnet-4") == "Claude Sonnet 4"

    def test_with_version_numbers(self):
        assert _model_id_to_display_name("claude-opus-4-6") == "Claude Opus 4 6"

    def test_with_slash(self):
        assert _model_id_to_display_name("gemini-2.5-pro") == "Gemini 2.5 Pro"


# -- Cursor model parsing -----------------------------------------------------


class TestCursorModelParsing:
    def test_parse_standard_output(self):
        output = (
            "Available models\n"
            "claude-4-opus - Claude 4 Opus\n"
            "gpt-5.4 - GPT 5.4\n"
            "gemini-2.5-pro - Gemini 2.5 Pro\n"
            "Tip: use --model to select a model\n"
        )
        cache = AIModelCache()
        result = cache._parse_cursor_output(output)
        assert len(result) == 3
        assert result[0] == {"id": "claude-4-opus", "name": "Claude 4 Opus"}
        assert result[1] == {"id": "gpt-5.4", "name": "GPT 5.4"}
        assert result[2] == {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"}

    def test_parse_empty_output(self):
        cache = AIModelCache()
        result = cache._parse_cursor_output("")
        assert result == []

    def test_parse_only_header_and_footer(self):
        output = "Available models\nTip: use --model\n"
        cache = AIModelCache()
        result = cache._parse_cursor_output(output)
        assert result == []

    def test_parse_model_without_separator(self):
        output = "Available models\nsome-model\nTip: use --model\n"
        cache = AIModelCache()
        result = cache._parse_cursor_output(output)
        assert len(result) == 1
        assert result[0]["id"] == "some-model"
        assert result[0]["name"] == "Some Model"


# -- Claude model filtering ---------------------------------------------------


class TestClaudeModelFiltering:
    def _make_cache_with_pricing(self, data: dict) -> AIModelCache:
        cache = AIModelCache()
        pricing = MagicMock()
        pricing._data = data
        cache.set_pricing_cache(pricing)
        return cache

    def test_filters_bare_claude_models(self):
        data = {
            "claude-sonnet-4": {"input_cost_per_token": 0.001},
            "claude-opus-4-6": {"input_cost_per_token": 0.002},
            "claude-3-haiku-20240307": {"input_cost_per_token": 0.0003},
            "gpt-4": {"input_cost_per_token": 0.003},  # not Claude
        }
        cache = self._make_cache_with_pricing(data)
        result = cache._list_claude_models()
        model_ids = [m["id"] for m in result]
        assert "claude-sonnet-4" in model_ids
        assert "claude-opus-4-6" in model_ids
        assert "claude-3-haiku-20240307" in model_ids
        assert "gpt-4" not in model_ids

    def test_excludes_provider_prefixed_models(self):
        data = {
            "claude-sonnet-4": {"input_cost_per_token": 0.001},
            "anthropic.claude-sonnet-4": {"input_cost_per_token": 0.001},
            "vertex_ai/claude-sonnet-4": {"input_cost_per_token": 0.001},
            "bedrock/claude-sonnet-4": {"input_cost_per_token": 0.001},
            "openrouter/claude-sonnet-4": {"input_cost_per_token": 0.001},
        }
        cache = self._make_cache_with_pricing(data)
        result = cache._list_claude_models()
        model_ids = [m["id"] for m in result]
        assert model_ids == ["claude-sonnet-4"]

    def test_empty_pricing_cache(self):
        cache = AIModelCache()
        # No pricing cache set
        result = cache._list_claude_models()
        assert result == []

    def test_empty_pricing_data(self):
        cache = self._make_cache_with_pricing({})
        result = cache._list_claude_models()
        assert result == []


# -- Gemini model filtering ----------------------------------------------------


class TestGeminiModelFiltering:
    def _make_cache_with_pricing(self, data: dict) -> AIModelCache:
        cache = AIModelCache()
        pricing = MagicMock()
        pricing._data = data
        cache.set_pricing_cache(pricing)
        return cache

    def test_filters_gemini_slash_models(self):
        data = {
            "gemini/gemini-2.5-pro": {"input_cost_per_token": 0.001},
            "gemini/gemini-2.5-flash": {"input_cost_per_token": 0.0005},
            "gpt-4": {"input_cost_per_token": 0.003},
        }
        cache = self._make_cache_with_pricing(data)
        result = cache._list_gemini_models()
        model_ids = [m["id"] for m in result]
        assert "gemini-2.5-pro" in model_ids
        assert "gemini-2.5-flash" in model_ids
        assert "gpt-4" not in model_ids

    def test_filters_bare_gemini_models(self):
        data = {
            "gemini-1.5-pro": {"input_cost_per_token": 0.001},
        }
        cache = self._make_cache_with_pricing(data)
        result = cache._list_gemini_models()
        assert len(result) == 1
        assert result[0]["id"] == "gemini-1.5-pro"

    def test_excludes_provider_prefixed(self):
        data = {
            "gemini/gemini-2.5-pro": {"input_cost_per_token": 0.001},
            "vertex_ai/gemini-2.5-pro": {"input_cost_per_token": 0.001},
            "openrouter/gemini-2.5-pro": {"input_cost_per_token": 0.001},
        }
        cache = self._make_cache_with_pricing(data)
        result = cache._list_gemini_models()
        model_ids = [m["id"] for m in result]
        assert model_ids == ["gemini-2.5-pro"]

    def test_deduplicates_slash_and_bare(self):
        data = {
            "gemini-2.5-pro": {"input_cost_per_token": 0.001},
            "gemini/gemini-2.5-pro": {"input_cost_per_token": 0.001},
        }
        cache = self._make_cache_with_pricing(data)
        result = cache._list_gemini_models()
        model_ids = [m["id"] for m in result]
        # Only one entry for gemini-2.5-pro
        assert model_ids.count("gemini-2.5-pro") == 1

    def test_empty_pricing_cache(self):
        cache = AIModelCache()
        result = cache._list_gemini_models()
        assert result == []


# -- Cursor subprocess ---------------------------------------------------------


class TestCursorSubprocess:
    @pytest.mark.asyncio
    async def test_successful_subprocess(self):
        cache = AIModelCache()
        stdout = (
            "Available models\n"
            "claude-4-opus - Claude 4 Opus\n"
            "gpt-5 - GPT 5\n"
            "Tip: use --model to select\n"
        )
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (stdout.encode(), b"")
        mock_proc.returncode = 0

        with patch(
            "jenkins_job_insight.ai_models.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await cache._list_cursor_models()

        assert len(result) == 2
        assert result[0]["id"] == "claude-4-opus"

    @pytest.mark.asyncio
    async def test_subprocess_timeout(self):
        cache = AIModelCache()
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()

        with patch(
            "jenkins_job_insight.ai_models.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await cache._list_cursor_models()

        assert result == []

    @pytest.mark.asyncio
    async def test_subprocess_not_found(self):
        cache = AIModelCache()

        with patch(
            "jenkins_job_insight.ai_models.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("agent not found"),
        ):
            result = await cache._list_cursor_models()

        assert result == []

    @pytest.mark.asyncio
    async def test_subprocess_failure_nonzero_exit(self):
        cache = AIModelCache()
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"some error")
        mock_proc.returncode = 1

        with patch(
            "jenkins_job_insight.ai_models.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await cache._list_cursor_models()

        assert result == []

    @pytest.mark.asyncio
    async def test_subprocess_generic_exception(self):
        cache = AIModelCache()

        with patch(
            "jenkins_job_insight.ai_models.asyncio.create_subprocess_exec",
            side_effect=OSError("unexpected"),
        ):
            result = await cache._list_cursor_models()

        assert result == []


# -- Unknown provider ----------------------------------------------------------


class TestUnknownProvider:
    @pytest.mark.asyncio
    async def test_unknown_provider_returns_empty(self):
        cache = AIModelCache()
        result = await cache.list_models("unknown-provider")
        assert result == []


# -- is_valid_model ------------------------------------------------------------


class TestIsValidModel:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_cache(self):
        cache = AIModelCache()
        # No cache populated
        assert cache.is_valid_model("claude", "claude-sonnet-4") is None

    @pytest.mark.asyncio
    async def test_returns_true_when_model_found(self):
        cache = AIModelCache()
        import time

        cache._cache["claude"] = {
            "models": [{"id": "claude-sonnet-4", "name": "Claude Sonnet 4"}],
            "fetched_at": time.monotonic(),
        }
        assert cache.is_valid_model("claude", "claude-sonnet-4") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_model_not_found(self):
        cache = AIModelCache()
        import time

        cache._cache["claude"] = {
            "models": [{"id": "claude-sonnet-4", "name": "Claude Sonnet 4"}],
            "fetched_at": time.monotonic(),
        }
        assert cache.is_valid_model("claude", "nonexistent-model") is False


# -- Caching behaviour ---------------------------------------------------------


class TestCachingBehaviour:
    @pytest.mark.asyncio
    async def test_list_models_caches_result(self):
        cache = AIModelCache()
        pricing = MagicMock()
        pricing._data = {
            "claude-sonnet-4": {"input_cost_per_token": 0.001},
        }
        cache.set_pricing_cache(pricing)

        result1 = await cache.list_models("claude")
        result2 = await cache.list_models("claude")
        # Same object from cache
        assert result1 is result2

    @pytest.mark.asyncio
    async def test_refresh_clears_cache(self):
        cache = AIModelCache()
        pricing = MagicMock()
        pricing._data = {
            "claude-sonnet-4": {"input_cost_per_token": 0.001},
        }
        cache.set_pricing_cache(pricing)

        result1 = await cache.list_models("claude")
        await cache.refresh("claude")
        result2 = await cache.list_models("claude")
        # Different object after refresh
        assert result1 is not result2
        assert result1 == result2
