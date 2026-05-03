"""Tests for the /api/analyze-comment-intent endpoint."""

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from ai_cli_runner import AIResult

from jenkins_job_insight import storage
from tests.conftest import build_test_env


@pytest.fixture
def _mock_settings(temp_db_path: Path):
    """Mock settings with AI provider configured."""
    env = build_test_env(
        AI_PROVIDER="gemini",
        AI_MODEL="gemini-2.5-flash",
        DB_PATH=str(temp_db_path),
        GEMINI_API_KEY="test-key",  # noqa: S106  # pragma: allowlist secret
    )
    with patch.dict(os.environ, env, clear=True):
        from jenkins_job_insight.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def client(_mock_settings, temp_db_path: Path):
    """Create a test client with mocked dependencies."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient

        from jenkins_job_insight.main import app

        with TestClient(app) as c:
            yield c


class TestAnalyzeCommentIntent:
    """Tests for /api/analyze-comment-intent endpoint."""

    def test_comment_suggests_reviewed(self, client) -> None:
        """Comment with a bug link implies reviewed."""
        ai_response = AIResult(
            success=True,
            text='{"suggests_reviewed": true, "reason": "Bug filed with Jira link"}',
        )
        with patch("ai_cli_runner.call_ai_cli", return_value=ai_response) as mock_ai:
            response = client.post(
                "/api/analyze-comment-intent",
                json={"comment": "Filed JIRA-123 for this failure"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["suggests_reviewed"] is True
        assert data["reason"] == "Bug filed with Jira link"
        mock_ai.assert_called_once()

    def test_comment_does_not_suggest_reviewed(self, client) -> None:
        """Comment sharing a URL for context does not imply reviewed."""
        ai_response = AIResult(
            success=True,
            text='{"suggests_reviewed": false, "reason": "Sharing a URL for context, no resolution indicated"}',
        )
        with patch("ai_cli_runner.call_ai_cli", return_value=ai_response):
            response = client.post(
                "/api/analyze-comment-intent",
                json={
                    "comment": "Here's the docs link: https://docs.example.com/troubleshooting"
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["suggests_reviewed"] is False

    def test_ai_failure_returns_false(self, client) -> None:
        """AI call failure returns safe default (suggests_reviewed=False)."""
        ai_response = AIResult(
            success=False,
            text="AI service unavailable",
        )
        with patch("ai_cli_runner.call_ai_cli", return_value=ai_response):
            response = client.post(
                "/api/analyze-comment-intent",
                json={"comment": "Fixed in commit abc123"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["suggests_reviewed"] is False
        assert data["reason"] == ""

    def test_ai_returns_invalid_json(self, client) -> None:
        """Unparseable AI response returns safe default."""
        ai_response = AIResult(
            success=True,
            text="This is not valid JSON at all",
        )
        with patch("ai_cli_runner.call_ai_cli", return_value=ai_response):
            response = client.post(
                "/api/analyze-comment-intent",
                json={"comment": "some comment"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["suggests_reviewed"] is False

    def test_records_ai_usage(self, client) -> None:
        """Token usage is recorded for comment intent calls."""
        ai_response = AIResult(
            success=True,
            text='{"suggests_reviewed": false, "reason": "test"}',
        )
        with (
            patch("ai_cli_runner.call_ai_cli", return_value=ai_response),
            patch("jenkins_job_insight.token_tracking.record_ai_usage") as mock_record,
        ):
            response = client.post(
                "/api/analyze-comment-intent",
                json={"comment": "test comment"},
            )

        assert response.status_code == 200
        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args
        assert call_kwargs.kwargs["job_id"] == "comment-intent"
        assert call_kwargs.kwargs["call_type"] == "comment_intent"

    def test_missing_comment_field(self, client) -> None:
        """Missing comment field returns 422."""
        response = client.post(
            "/api/analyze-comment-intent",
            json={},
        )
        assert response.status_code == 422

    def test_accepts_ai_provider_and_model(self, client) -> None:
        """Request body can include optional ai_provider and ai_model."""
        ai_response = AIResult(
            success=True,
            text='{"suggests_reviewed": true, "reason": "Bug filed"}',
        )
        with patch("ai_cli_runner.call_ai_cli", return_value=ai_response) as mock_ai:
            response = client.post(
                "/api/analyze-comment-intent",
                json={
                    "comment": "Filed JIRA-123",
                    "ai_provider": "claude",
                    "ai_model": "claude-sonnet-4-20250514",
                },
            )

        assert response.status_code == 200
        assert response.json()["suggests_reviewed"] is True
        call_kwargs = mock_ai.call_args
        assert call_kwargs.kwargs["ai_provider"] == "claude"
        assert call_kwargs.kwargs["ai_model"] == "claude-sonnet-4-20250514"


class TestAnalyzeCommentIntentJobFallback:
    """Tests for AI config fallback from the analyzed job's stored config."""

    @pytest.fixture
    def _mock_settings_no_ai(self, temp_db_path: Path):
        """Mock settings with NO AI provider/model env vars."""
        env = build_test_env(
            DB_PATH=str(temp_db_path),
        )
        # Remove AI_PROVIDER and AI_MODEL so env fallback is empty
        env.pop("AI_PROVIDER", None)
        env.pop("AI_MODEL", None)
        with patch.dict(os.environ, env, clear=True):
            from jenkins_job_insight.config import get_settings

            get_settings.cache_clear()
            try:
                yield
            finally:
                get_settings.cache_clear()

    @pytest.fixture
    def client_no_ai(self, _mock_settings_no_ai, temp_db_path: Path):
        """Test client without server-level AI config."""
        from jenkins_job_insight import main as main_mod

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch.object(main_mod, "AI_PROVIDER", ""),
            patch.object(main_mod, "AI_MODEL", ""),
        ):
            from starlette.testclient import TestClient

            from jenkins_job_insight.main import app

            with TestClient(app) as c:
                yield c

    @staticmethod
    def _store_job_with_ai_config(
        temp_db_path: Path, job_id: str, ai_provider: str, ai_model: str
    ):
        """Store a job result with AI config in request_params."""
        result_data = {
            "job_id": job_id,
            "status": "completed",
            "summary": "test",
            "request_params": {
                "ai_provider": ai_provider,
                "ai_model": ai_model,
            },
            "failures": [],
        }

        async def _save():
            await storage.init_db()
            await storage.save_result(
                job_id, "http://jenkins/1", "completed", result_data
            )

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(_save())

    def test_fallback_to_job_ai_config(self, client_no_ai, temp_db_path: Path) -> None:
        """When no env vars or body AI config, fall back to job's stored config."""
        self._store_job_with_ai_config(
            temp_db_path, "job-123", "claude", "claude-sonnet-4-20250514"
        )
        ai_response = AIResult(
            success=True,
            text='{"suggests_reviewed": true, "reason": "Bug filed"}',
        )
        with patch("ai_cli_runner.call_ai_cli", return_value=ai_response) as mock_ai:
            response = client_no_ai.post(
                "/api/analyze-comment-intent",
                json={"comment": "Filed JIRA-123", "job_id": "job-123"},
            )

        assert response.status_code == 200
        assert response.json()["suggests_reviewed"] is True
        call_kwargs = mock_ai.call_args
        assert call_kwargs.kwargs["ai_provider"] == "claude"
        assert call_kwargs.kwargs["ai_model"] == "claude-sonnet-4-20250514"

    def test_no_ai_config_anywhere_returns_400(
        self, client_no_ai, temp_db_path: Path
    ) -> None:
        """When no AI config available (no env, no body, no job), return 400."""
        response = client_no_ai.post(
            "/api/analyze-comment-intent",
            json={"comment": "Filed JIRA-123"},
        )
        assert response.status_code == 400

    def test_body_ai_config_takes_precedence_over_job(
        self, client_no_ai, temp_db_path: Path
    ) -> None:
        """Body-level AI config takes precedence over job's stored config."""
        self._store_job_with_ai_config(
            temp_db_path, "job-456", "gemini", "gemini-2.5-flash"
        )
        ai_response = AIResult(
            success=True,
            text='{"suggests_reviewed": false, "reason": "No resolution"}',
        )
        with patch("ai_cli_runner.call_ai_cli", return_value=ai_response) as mock_ai:
            response = client_no_ai.post(
                "/api/analyze-comment-intent",
                json={
                    "comment": "Checking logs",
                    "job_id": "job-456",
                    "ai_provider": "claude",
                    "ai_model": "claude-sonnet-4-20250514",
                },
            )

        assert response.status_code == 200
        call_kwargs = mock_ai.call_args
        assert call_kwargs.kwargs["ai_provider"] == "claude"
        assert call_kwargs.kwargs["ai_model"] == "claude-sonnet-4-20250514"

    def test_nonexistent_job_id_no_env_returns_400(
        self, client_no_ai, temp_db_path: Path
    ) -> None:
        """Non-existent job_id with no other AI config returns 400."""
        response = client_no_ai.post(
            "/api/analyze-comment-intent",
            json={"comment": "Filed JIRA-123", "job_id": "nonexistent"},
        )
        assert response.status_code == 400

    def test_env_ai_config_takes_precedence_over_job(
        self, _mock_settings, temp_db_path: Path
    ) -> None:
        """Server-level env AI config takes precedence over job's stored config."""
        from jenkins_job_insight import main as main_mod

        self._store_job_with_ai_config(
            temp_db_path, "job-789", "claude", "claude-sonnet-4-20250514"
        )
        ai_response = AIResult(
            success=True,
            text='{"suggests_reviewed": true, "reason": "Bug filed"}',
        )
        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch.object(main_mod, "AI_PROVIDER", "gemini"),
            patch.object(main_mod, "AI_MODEL", "gemini-2.5-flash"),
        ):
            from starlette.testclient import TestClient

            from jenkins_job_insight.main import app

            with TestClient(app) as client_env:
                with patch(
                    "ai_cli_runner.call_ai_cli", return_value=ai_response
                ) as mock_ai:
                    response = client_env.post(
                        "/api/analyze-comment-intent",
                        json={
                            "comment": "Filed JIRA-123",
                            "job_id": "job-789",
                        },
                    )

        assert response.status_code == 200
        call_kwargs = mock_ai.call_args
        # env vars are gemini/gemini-2.5-flash (from patched module constants)
        assert call_kwargs.kwargs["ai_provider"] == "gemini"
        assert call_kwargs.kwargs["ai_model"] == "gemini-2.5-flash"
