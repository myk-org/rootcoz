"""Tests for user feedback endpoint and scrubbing logic."""

import json
import os
from unittest.mock import patch

import pytest
from ai_cli_runner import AIResult
from fastapi.testclient import TestClient

from jenkins_job_insight import storage
from jenkins_job_insight.config import get_settings
from jenkins_job_insight.feedback import (
    _build_fallback_feedback,
    _derive_fallback_labels,
    _parse_json_response,
    create_feedback_from_preview,
    create_feedback_issue,
    format_feedback_with_ai,
    generate_feedback_preview,
    scrub_sensitive_data,
)
from jenkins_job_insight.models import (
    FailedApiCall,
    FeedbackPreviewResponse,
    FeedbackRequest,
    FeedbackResponse,
    PageState,
)

_TEST_GITHUB_TOKEN = "test-token-placeholder"  # noqa: S105

_GITHUB_FOOTER_MARKER = (
    "Generated using AI with [JJI](https://github.com/myk-org/jenkins-job-insight)"
)


# ---------------------------------------------------------------------------
# scrub_sensitive_data tests
# ---------------------------------------------------------------------------


class TestScrubSensitiveData:
    def test_bearer_token(self):
        text = "Authorization: Bearer ghp_abc123XYZ"
        result = scrub_sensitive_data(text)
        assert "ghp_abc123XYZ" not in result
        assert "[REDACTED]" in result

    def test_basic_auth_header(self):
        text = "Authorization: Basic dXNlcjpwYXNz"
        result = scrub_sensitive_data(text)
        assert "dXNlcjpwYXNz" not in result
        assert "[REDACTED]" in result

    def test_api_key_param(self):
        text = "api_key=test-key-placeholder"  # pragma: allowlist secret
        result = scrub_sensitive_data(text)
        assert "test-key-placeholder" not in result
        assert "[REDACTED]" in result

    def test_token_param(self):
        text = "token=mySecretToken123"
        result = scrub_sensitive_data(text)
        assert "mySecretToken123" not in result
        assert "[REDACTED]" in result

    def test_password_param(self):
        text = "password=SuperS3cret!"
        result = scrub_sensitive_data(text)
        assert "SuperS3cret!" not in result
        assert "[REDACTED]" in result

    def test_jwt_token(self):
        jwt = "eyJhbGci.eyJzdWIi.dummy-test-sig"  # pragma: allowlist secret
        # JWT embedded in a sentence (not preceded by a key= pattern)
        text = f"The auth header contained {jwt} which expired"
        result = scrub_sensitive_data(text)
        assert jwt not in result
        assert "[REDACTED_JWT]" in result

    def test_jwt_token_after_key(self):
        jwt = "eyJhbGci.eyJzdWIi.dummy-test-sig"  # pragma: allowlist secret
        text = f"Token: {jwt}"
        result = scrub_sensitive_data(text)
        assert jwt not in result
        assert "[REDACTED]" in result

    def test_github_token_patterns(self):
        for prefix in ("ghp_", "gho_", "ghs_", "ghr_", "github_pat_"):
            text = f"token={prefix}abcdefghij1234567890"
            result = scrub_sensitive_data(text)
            assert f"{prefix}abcdefghij1234567890" not in result

    def test_preserves_normal_text(self):
        text = "Test test_login_flow failed with AssertionError at line 42"
        result = scrub_sensitive_data(text)
        assert result == text

    def test_preserves_urls(self):
        text = "Failed request to https://api.example.com/v1/users"
        result = scrub_sensitive_data(text)
        assert "https://api.example.com/v1/users" in result

    def test_preserves_test_names(self):
        text = "tests.auth.test_login.TestLogin.test_valid_credentials"
        result = scrub_sensitive_data(text)
        assert result == text

    def test_multiple_sensitive_patterns(self):
        text = "Bearer fake-token-abc password=fake-pass api_key=mykey"
        result = scrub_sensitive_data(text)
        assert "fake-token-abc" not in result
        assert "fake-pass" not in result
        assert "mykey" not in result

    def test_empty_string(self):
        assert scrub_sensitive_data("") == ""

    def test_authorization_header_json(self):
        text = """{"Authorization": "Bearer super-secret-token"}"""
        result = scrub_sensitive_data(text)
        assert "super-secret-token" not in result


# ---------------------------------------------------------------------------
# fallback formatting tests
# ---------------------------------------------------------------------------


class TestBuildFallbackFeedback:
    def test_bug_fallback(self):
        req = FeedbackRequest(
            description="Button does not work",
            console_errors=["TypeError: undefined is not a function"],
            failed_api_calls=[
                FailedApiCall(
                    status=500,
                    endpoint="/api/analyze",
                    error="Internal Server Error",
                )
            ],
            page_state=PageState(url="/report/123"),
            user_agent="Mozilla/5.0",
        )
        title, body = _build_fallback_feedback(req)
        assert "Feedback:" in title
        assert "## Feedback" in body
        assert "Button does not work" in body
        assert "TypeError" in body
        assert "/api/analyze" in body
        assert "Mozilla/5.0" in body

    def test_feature_fallback(self):
        req = FeedbackRequest(
            description="Add dark mode support",
        )
        title, body = _build_fallback_feedback(req)
        assert "Feedback:" in title
        assert "## Feedback" in body
        assert "Add dark mode support" in body

    def test_fallback_scrubs_console_errors(self):
        req = FeedbackRequest(
            description="Auth error",
            console_errors=["Bearer my-secret-token leaked"],
        )
        _, body = _build_fallback_feedback(req)
        assert "my-secret-token" not in body
        assert "[REDACTED]" in body


# ---------------------------------------------------------------------------
# _derive_fallback_labels tests
# ---------------------------------------------------------------------------


class TestDeriveFallbackLabels:
    def test_enhancement_when_no_errors(self):
        req = FeedbackRequest(description="Add dark mode")
        assert _derive_fallback_labels(req) == ["enhancement"]

    def test_bug_when_console_errors(self):
        req = FeedbackRequest(
            description="Page crashed",
            console_errors=["TypeError: x is not a function"],
        )
        assert _derive_fallback_labels(req) == ["bug"]

    def test_bug_when_failed_api_calls(self):
        req = FeedbackRequest(
            description="API broken",
            failed_api_calls=[
                FailedApiCall(status=500, endpoint="/api/x", error="err")
            ],
        )
        assert _derive_fallback_labels(req) == ["bug"]

    def test_bug_when_both_errors(self):
        req = FeedbackRequest(
            description="Everything broke",
            console_errors=["err"],
            failed_api_calls=[
                FailedApiCall(status=500, endpoint="/api/x", error="err")
            ],
        )
        assert _derive_fallback_labels(req) == ["bug"]


# ---------------------------------------------------------------------------
# _parse_json_response tests
# ---------------------------------------------------------------------------


class TestParseJsonResponse:
    def test_valid_response(self):
        text = json.dumps({"title": "Bug report", "body": "Details here"})
        result = _parse_json_response(text)
        assert result == {"title": "Bug report", "body": "Details here"}

    def test_empty_title_rejected(self):
        text = json.dumps({"title": "", "body": "Details here"})
        assert _parse_json_response(text) is None

    def test_whitespace_title_rejected(self):
        text = json.dumps({"title": "   ", "body": "Details here"})
        assert _parse_json_response(text) is None

    def test_empty_body_rejected(self):
        text = json.dumps({"title": "Bug report", "body": ""})
        assert _parse_json_response(text) is None

    def test_whitespace_body_rejected(self):
        text = json.dumps({"title": "Bug report", "body": "  \n  "})
        assert _parse_json_response(text) is None

    def test_non_string_title_rejected(self):
        text = json.dumps({"title": 123, "body": "Details"})
        assert _parse_json_response(text) is None

    def test_non_string_body_rejected(self):
        text = json.dumps({"title": "Bug", "body": ["line1"]})
        assert _parse_json_response(text) is None

    def test_null_title_rejected(self):
        text = json.dumps({"title": None, "body": "Details"})
        assert _parse_json_response(text) is None

    def test_markdown_fences_stripped(self):
        text = "```json\n" + json.dumps({"title": "T", "body": "B"}) + "\n```"
        result = _parse_json_response(text)
        assert result == {"title": "T", "body": "B"}

    def test_invalid_json(self):
        assert _parse_json_response("not json") is None

    def test_missing_keys(self):
        assert _parse_json_response(json.dumps({"title": "only title"})) is None


# ---------------------------------------------------------------------------
# format_feedback_with_ai tests
# ---------------------------------------------------------------------------


class TestFormatFeedbackWithAi:
    @pytest.fixture
    def settings(self):
        env = {
            "JENKINS_URL": "https://jenkins.example.com",
            "JENKINS_USER": "user",
            "JENKINS_PASSWORD": "pass",  # pragma: allowlist secret
        }
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            s = get_settings()
            get_settings.cache_clear()
            return s

    async def test_ai_success(self, settings):
        req = FeedbackRequest(
            description="The analyze button is broken",
        )
        ai_response = json.dumps(
            {
                "title": "Analyze button not responding",
                "body": "## Description\n\nThe analyze button fails to trigger analysis.",
                "labels": ["bug"],
            }
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, body, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert title == "Analyze button not responding"
        assert "## Description" in body
        assert labels == ["bug"]

    async def test_ai_success_with_enhancement_label(self, settings):
        req = FeedbackRequest(
            description="Add export to CSV",
        )
        ai_response = json.dumps(
            {
                "title": "Add CSV export feature",
                "body": "## Feature\n\nExport support.",
                "labels": ["enhancement"],
            }
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert title == "Add CSV export feature"
        assert labels == ["enhancement"]

    async def test_ai_failure_uses_fallback(self, settings):
        req = FeedbackRequest(
            description="Add export to CSV",
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="CLI error")
            title, body, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert "Feedback:" in title
        assert "Add export to CSV" in body
        assert labels == ["enhancement"]

    async def test_ai_returns_invalid_json_uses_fallback(self, settings):
        req = FeedbackRequest(
            description="Something broke",
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text="not json at all")
            title, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert "Feedback:" in title
        assert labels == ["enhancement"]

    async def test_ai_response_with_markdown_fences(self, settings):
        req = FeedbackRequest(
            description="Error on page load",
        )
        ai_response = (
            "```json\n"
            + json.dumps(
                {
                    "title": "Page load error",
                    "body": "## Bug\n\nPage fails to load.",
                    "labels": ["bug"],
                }
            )
            + "\n```"
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert title == "Page load error"
        assert labels == ["bug"]

    async def test_scrubs_sensitive_data_in_context(self, settings):
        req = FeedbackRequest(
            description="Auth failed",
            console_errors=["Bearer my-secret-token-123"],
            failed_api_calls=[FailedApiCall(error="password=hunter2")],
        )
        captured_prompt = None

        async def capture_call(prompt, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return AIResult(success=False, text="fail")

        with patch(
            "jenkins_job_insight.feedback.call_ai_cli", side_effect=capture_call
        ):
            await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )

        # Verify sensitive data was scrubbed in the prompt sent to AI
        assert "my-secret-token-123" not in captured_prompt
        assert "hunter2" not in captured_prompt

    async def test_ai_returns_no_labels_defaults_to_enhancement(self, settings):
        req = FeedbackRequest(
            description="Some feedback",
        )
        ai_response = json.dumps(
            {
                "title": "Some title",
                "body": "Some body",
            }
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            _, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert labels == ["enhancement"]

    async def test_ai_exception_uses_fallback(self, settings):
        req = FeedbackRequest(
            description="Something broke",
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.side_effect = RuntimeError("AI down")
            title, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert "Feedback:" in title
        assert labels == ["enhancement"]

    async def test_fallback_labels_bug_when_console_errors(self, settings):
        req = FeedbackRequest(
            description="Page crashed",
            console_errors=["TypeError: x is not a function"],
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="fail")
            _, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert labels == ["bug"]

    async def test_fallback_labels_bug_when_failed_api_calls(self, settings):
        req = FeedbackRequest(
            description="API error",
            failed_api_calls=[
                FailedApiCall(status=500, endpoint="/api/x", error="err")
            ],
        )
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.side_effect = RuntimeError("AI down")
            _, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert labels == ["bug"]

    async def test_ai_returns_blank_title_uses_fallback(self, settings):
        req = FeedbackRequest(description="Some feedback")
        ai_response = json.dumps({"title": "", "body": "Details", "labels": ["bug"]})
        with patch("jenkins_job_insight.feedback.call_ai_cli") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, _, labels = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert "Feedback:" in title
        assert labels == ["enhancement"]


# ---------------------------------------------------------------------------
# generate_feedback_preview tests
# ---------------------------------------------------------------------------


class TestGenerateFeedbackPreview:
    @pytest.fixture
    def settings(self):
        env = {
            "JENKINS_URL": "https://jenkins.example.com",
            "JENKINS_USER": "user",
            "JENKINS_PASSWORD": "pass",  # pragma: allowlist secret
        }
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            s = get_settings()
            get_settings.cache_clear()
            return s

    async def test_bug_preview_returns_correct_labels(self, settings):
        req = FeedbackRequest(
            description="Dashboard crashes",
        )
        with patch(
            "jenkins_job_insight.feedback.format_feedback_with_ai"
        ) as mock_format:
            mock_format.return_value = (
                "Dashboard crash on load",
                "## Bug\n\nDetails...",
                ["bug"],
            )
            result = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="test-model"
            )

        assert isinstance(result, FeedbackPreviewResponse)
        assert result.title == "Dashboard crash on load"
        assert _GITHUB_FOOTER_MARKER in result.body
        assert result.labels == ["bug"]

    async def test_feature_preview_returns_correct_labels(self, settings):
        req = FeedbackRequest(
            description="Add dark mode",
        )
        with patch(
            "jenkins_job_insight.feedback.format_feedback_with_ai"
        ) as mock_format:
            mock_format.return_value = (
                "Add dark mode support",
                "## Feature\n\nDark mode...",
                ["enhancement"],
            )
            result = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="test-model"
            )

        assert isinstance(result, FeedbackPreviewResponse)
        assert result.title == "Add dark mode support"
        assert _GITHUB_FOOTER_MARKER in result.body
        assert result.labels == ["enhancement"]


# ---------------------------------------------------------------------------
# create_feedback_from_preview tests
# ---------------------------------------------------------------------------


class TestCreateFeedbackFromPreview:
    @pytest.fixture
    def settings(self):
        env = {
            "JENKINS_URL": "https://jenkins.example.com",
            "JENKINS_USER": "user",
            "JENKINS_PASSWORD": "pass",  # pragma: allowlist secret
            "GITHUB_TOKEN": _TEST_GITHUB_TOKEN,
        }
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            s = get_settings()
            get_settings.cache_clear()
            return s

    async def test_creates_issue_with_labels(self, settings):
        with patch("jenkins_job_insight.feedback.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/myk-org/jenkins-job-insight/issues/42",
                "number": 42,
                "title": "Dashboard crash on load",
            }
            result = await create_feedback_from_preview(
                title="Dashboard crash on load",
                body="## Bug\n\nDetails...",
                labels=["bug"],
                settings=settings,
            )

        assert isinstance(result, FeedbackResponse)
        assert result.issue_number == 42
        assert result.title == "Dashboard crash on load"
        assert "issues/42" in result.issue_url

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert (
            call_kwargs["repo_url"] == "https://github.com/myk-org/jenkins-job-insight"
        )
        assert call_kwargs["labels"] == ["bug"]

    async def test_uses_correct_repo_url(self, settings):
        with patch("jenkins_job_insight.feedback.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/x/y/issues/1",
                "number": 1,
                "title": "Title",
            }
            await create_feedback_from_preview(
                title="Title",
                body="Body",
                labels=["enhancement"],
                settings=settings,
            )

        mock_create.assert_called_once()
        assert (
            mock_create.call_args.kwargs["repo_url"]
            == "https://github.com/myk-org/jenkins-job-insight"
        )


# ---------------------------------------------------------------------------
# create_feedback_issue (legacy) tests
# ---------------------------------------------------------------------------


class TestCreateFeedbackIssue:
    @pytest.fixture
    def settings(self):
        env = {
            "JENKINS_URL": "https://jenkins.example.com",
            "JENKINS_USER": "user",
            "JENKINS_PASSWORD": "pass",  # pragma: allowlist secret
            "GITHUB_TOKEN": _TEST_GITHUB_TOKEN,
        }
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            s = get_settings()
            get_settings.cache_clear()
            return s

    @staticmethod
    def _assert_create_issue_kwargs(mock_create, *, expected_labels=None):
        """Extract and validate common call_args from create_github_issue mock."""
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert (
            call_kwargs.get("repo_url")
            == "https://github.com/myk-org/jenkins-job-insight"
        )
        assert call_kwargs.get("github_token") is not None
        if expected_labels is not None:
            assert call_kwargs.get("labels") == expected_labels
        return call_kwargs

    async def test_creates_bug_issue(self, settings):
        req = FeedbackRequest(
            description="Dashboard crashes",
        )
        with (
            patch(
                "jenkins_job_insight.feedback.format_feedback_with_ai"
            ) as mock_format,
            patch("jenkins_job_insight.feedback.create_github_issue") as mock_create,
        ):
            mock_format.return_value = (
                "Dashboard crash on load",
                "## Bug\n\nDetails...",
                ["bug"],
            )
            mock_create.return_value = {
                "url": "https://github.com/myk-org/jenkins-job-insight/issues/42",
                "number": 42,
                "title": "Dashboard crash on load",
            }
            result = await create_feedback_issue(req, settings)

        assert isinstance(result, FeedbackResponse)
        assert result.issue_number == 42
        assert result.title == "Dashboard crash on load"
        assert "issues/42" in result.issue_url

        self._assert_create_issue_kwargs(mock_create, expected_labels=["bug"])

    async def test_creates_feature_issue_with_enhancement_label(self, settings):
        req = FeedbackRequest(
            description="Add dark mode",
        )
        with (
            patch(
                "jenkins_job_insight.feedback.format_feedback_with_ai"
            ) as mock_format,
            patch("jenkins_job_insight.feedback.create_github_issue") as mock_create,
        ):
            mock_format.return_value = (
                "Add dark mode support",
                "## Feature\n\nDark mode...",
                ["enhancement"],
            )
            mock_create.return_value = {
                "url": "https://github.com/myk-org/jenkins-job-insight/issues/99",
                "number": 99,
                "title": "Add dark mode support",
            }
            result = await create_feedback_issue(req, settings)

        assert result.issue_number == 99
        self._assert_create_issue_kwargs(mock_create, expected_labels=["enhancement"])

    async def test_uses_correct_repo_url(self, settings):
        req = FeedbackRequest(
            description="test",
        )
        with (
            patch(
                "jenkins_job_insight.feedback.format_feedback_with_ai"
            ) as mock_format,
            patch("jenkins_job_insight.feedback.create_github_issue") as mock_create,
        ):
            mock_format.return_value = ("Title", "Body", ["enhancement"])
            mock_create.return_value = {
                "url": "https://github.com/x/y/issues/1",
                "number": 1,
                "title": "Title",
            }
            await create_feedback_issue(req, settings)

        self._assert_create_issue_kwargs(mock_create)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestFeedbackEndpoint:
    @pytest.fixture
    def _init_db(self, temp_db_path):
        """Initialize an empty database for endpoint tests."""
        import asyncio

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(storage.init_db())
            yield

    def _make_client(
        self,
        temp_db_path,
        github_token: str = "",
        enable_github_issues: str = "",
        ai_provider: str = "claude",
        ai_model: str = "test-model",
    ):
        """Create a TestClient with optional GITHUB_TOKEN."""
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "GITHUB_TOKEN",
                "ADMIN_KEY",
                "JJI_ENCRYPTION_KEY",
                "ALLOWED_USERS",
                "ENABLE_GITHUB_ISSUES",
                "AI_PROVIDER",
                "AI_MODEL",
            }
        }
        env["SECURE_COOKIES"] = "false"
        env["DB_PATH"] = str(temp_db_path)
        if github_token:
            env["GITHUB_TOKEN"] = github_token
        if enable_github_issues:
            env["ENABLE_GITHUB_ISSUES"] = enable_github_issues
        if ai_provider:
            env["AI_PROVIDER"] = ai_provider
        if ai_model:
            env["AI_MODEL"] = ai_model
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            import jenkins_job_insight.main as _main_mod

            with (
                patch.object(storage, "DB_PATH", temp_db_path),
                patch.object(_main_mod, "AI_PROVIDER", ai_provider),
                patch.object(_main_mod, "AI_MODEL", ai_model),
            ):
                from jenkins_job_insight.main import app

                with TestClient(app) as c:
                    yield c
            get_settings.cache_clear()

    # -- Preview endpoint tests -----------------------------------------------

    def test_preview_missing_github_token_returns_503(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=""):
            resp = client.post(
                "/api/feedback/preview",
                json={
                    "description": "Something broke",
                },
            )
            assert resp.status_code == 503
            assert "disabled" in resp.json()["detail"]

    def test_preview_missing_ai_provider_returns_503(self, _init_db, temp_db_path):
        for client in self._make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            ai_provider="",
            ai_model="",
        ):
            resp = client.post(
                "/api/feedback/preview",
                json={
                    "description": "Something broke",
                },
            )
            assert resp.status_code == 503
            assert "AI provider not configured" in resp.json()["detail"]

    def test_preview_successful(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=_TEST_GITHUB_TOKEN):
            with patch(
                "jenkins_job_insight.feedback.format_feedback_with_ai"
            ) as mock_format:
                mock_format.return_value = ("Test title", "Test body", ["bug"])
                resp = client.post(
                    "/api/feedback/preview",
                    json={
                        "description": "The button is broken",
                        "console_errors": ["TypeError: x is not a function"],
                    },
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["title"] == "Test title"
            assert "Test body" in data["body"]
            assert _GITHUB_FOOTER_MARKER in data["body"]
            assert data["labels"] == ["bug"]

    def test_preview_feature_returns_enhancement_label(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=_TEST_GITHUB_TOKEN):
            with patch(
                "jenkins_job_insight.feedback.format_feedback_with_ai"
            ) as mock_format:
                mock_format.return_value = (
                    "Feature title",
                    "Feature body",
                    ["enhancement"],
                )
                resp = client.post(
                    "/api/feedback/preview",
                    json={
                        "description": "Add dark mode",
                    },
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["labels"] == ["enhancement"]

    # -- Create endpoint tests ------------------------------------------------

    def test_create_missing_github_token_returns_503(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=""):
            resp = client.post(
                "/api/feedback/create",
                json={
                    "title": "Test title",
                    "body": "Test body",
                    "labels": ["bug"],
                },
            )
            assert resp.status_code == 503
            assert "disabled" in resp.json()["detail"]

    def test_create_successful(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=_TEST_GITHUB_TOKEN):
            with patch(
                "jenkins_job_insight.feedback.create_github_issue"
            ) as mock_create:
                mock_create.return_value = {
                    "url": "https://github.com/myk-org/jenkins-job-insight/issues/10",
                    "number": 10,
                    "title": "Test title",
                }
                resp = client.post(
                    "/api/feedback/create",
                    json={
                        "title": "Test title",
                        "body": "Test body",
                        "labels": ["bug"],
                    },
                )
            assert resp.status_code == 201
            data = resp.json()
            assert data["issue_number"] == 10
            assert data["title"] == "Test title"
            assert "issues/10" in data["issue_url"]

    def test_create_with_empty_labels(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=_TEST_GITHUB_TOKEN):
            with patch(
                "jenkins_job_insight.feedback.create_github_issue"
            ) as mock_create:
                mock_create.return_value = {
                    "url": "https://github.com/myk-org/jenkins-job-insight/issues/11",
                    "number": 11,
                    "title": "No labels",
                }
                resp = client.post(
                    "/api/feedback/create",
                    json={
                        "title": "No labels",
                        "body": "Test body",
                    },
                )
            assert resp.status_code == 201
            # Verify create_github_issue was called with empty labels
            mock_create.assert_called_once()
            assert mock_create.call_args.kwargs["labels"] == []

    # -- Capabilities tests ---------------------------------------------------

    def test_capabilities_includes_feedback_enabled(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=_TEST_GITHUB_TOKEN):
            resp = client.get("/api/capabilities")
            assert resp.status_code == 200
            data = resp.json()
            assert "feedback_enabled" in data
            assert data["feedback_enabled"] is True

    def test_capabilities_feedback_disabled_without_token(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=""):
            resp = client.get("/api/capabilities")
            assert resp.status_code == 200
            data = resp.json()
            assert data["feedback_enabled"] is False

    def test_feedback_disabled_when_enable_github_issues_false(
        self, _init_db, temp_db_path
    ):
        for client in self._make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            enable_github_issues="false",
        ):
            resp = client.post(
                "/api/feedback/preview",
                json={
                    "description": "Something broke",
                },
            )
            assert resp.status_code == 503
            assert "disabled" in resp.json()["detail"]

    def test_create_disabled_when_enable_github_issues_false(
        self, _init_db, temp_db_path
    ):
        for client in self._make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            enable_github_issues="false",
        ):
            resp = client.post(
                "/api/feedback/create",
                json={
                    "title": "Test title",
                    "body": "Test body",
                    "labels": ["bug"],
                },
            )
            assert resp.status_code == 503
            assert "disabled" in resp.json()["detail"]

    def test_capabilities_feedback_disabled_when_github_issues_false(
        self, _init_db, temp_db_path
    ):
        for client in self._make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            enable_github_issues="false",
        ):
            resp = client.get("/api/capabilities")
            assert resp.status_code == 200
            assert resp.json()["feedback_enabled"] is False

    def test_capabilities_feedback_disabled_without_ai_provider(
        self, _init_db, temp_db_path
    ):
        for client in self._make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            ai_provider="",
        ):
            resp = client.get("/api/capabilities")
            assert resp.status_code == 200
            assert resp.json()["feedback_enabled"] is False

    def test_capabilities_feedback_disabled_without_ai_model(
        self, _init_db, temp_db_path
    ):
        for client in self._make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            ai_model="",
        ):
            resp = client.get("/api/capabilities")
            assert resp.status_code == 200
            assert resp.json()["feedback_enabled"] is False
