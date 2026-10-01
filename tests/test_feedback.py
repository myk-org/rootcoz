"""Tests for user feedback endpoint and scrubbing logic."""

import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pi_sidecar_client import AIResult

from rootcoz import storage
from rootcoz.attribution import AiProvenance
from rootcoz.config import get_settings
from rootcoz.feedback import (
    _build_fallback_feedback,
    _derive_fallback_labels,
    _parse_json_response,
    create_feedback_from_preview,
    create_feedback_issue,
    format_feedback_with_ai,
    generate_feedback_preview,
    scrub_sensitive_data,
)
from rootcoz.models import (
    FailedApiCall,
    FeedbackPreviewResponse,
    FeedbackRequest,
    FeedbackResponse,
    PageState,
)

_TEST_GITHUB_TOKEN = "test-token-placeholder"

_GITHUB_FOOTER_MARKER = (
    "Generated using AI with [rootcoz](https://github.com/myk-org/rootcoz)"
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
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, body, labels, _ai = await format_feedback_with_ai(
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
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, _, labels, _ai = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert title == "Add CSV export feature"
        assert labels == ["enhancement"]

    async def test_ai_failure_uses_fallback(self, settings):
        req = FeedbackRequest(
            description="Add export to CSV",
        )
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="CLI error")
            title, body, labels, _ai = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert "Feedback:" in title
        assert "Add export to CSV" in body
        assert labels == ["enhancement"]

    async def test_ai_returns_invalid_json_uses_fallback(self, settings):
        req = FeedbackRequest(
            description="Something broke",
        )
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text="not json at all")
            title, _, labels, _ai = await format_feedback_with_ai(
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
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, _, labels, _ai = await format_feedback_with_ai(
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

        with patch("rootcoz.feedback.call_ai_once", side_effect=capture_call):
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
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            _, _, labels, _ai = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert labels == ["enhancement"]

    async def test_ai_exception_uses_fallback(self, settings):
        req = FeedbackRequest(
            description="Something broke",
        )
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.side_effect = RuntimeError("AI down")
            title, _, labels, _ai = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert "Feedback:" in title
        assert labels == ["enhancement"]

    async def test_fallback_labels_bug_when_console_errors(self, settings):
        req = FeedbackRequest(
            description="Page crashed",
            console_errors=["TypeError: x is not a function"],
        )
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=False, text="fail")
            _, _, labels, _ai = await format_feedback_with_ai(
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
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.side_effect = RuntimeError("AI down")
            _, _, labels, _ai = await format_feedback_with_ai(
                req, settings, ai_provider="claude", ai_model="test-model"
            )
        assert labels == ["bug"]

    async def test_ai_returns_blank_title_uses_fallback(self, settings):
        req = FeedbackRequest(description="Some feedback")
        ai_response = json.dumps({"title": "", "body": "Details", "labels": ["bug"]})
        with patch("rootcoz.feedback.call_ai_once") as mock_ai:
            mock_ai.return_value = AIResult(success=True, text=ai_response)
            title, _, labels, _ai = await format_feedback_with_ai(
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
        with patch("rootcoz.feedback.format_feedback_with_ai") as mock_format:
            mock_format.return_value = (
                "Dashboard crash on load",
                "## Bug\n\nDetails...",
                ["bug"],
                True,
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
        with patch("rootcoz.feedback.format_feedback_with_ai") as mock_format:
            mock_format.return_value = (
                "Add dark mode support",
                "## Feature\n\nDark mode...",
                ["enhancement"],
                True,
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
    async def test_creates_issue_with_labels(self):
        with patch("rootcoz.feedback.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/myk-org/rootcoz/issues/42",
                "number": 42,
                "title": "Dashboard crash on load",
            }
            result = await create_feedback_from_preview(
                title="Dashboard crash on load",
                body="## Bug\n\nDetails...",
                labels=["bug"],
                github_token=_TEST_GITHUB_TOKEN,
            )

        assert isinstance(result, FeedbackResponse)
        assert result.issue_number == 42
        assert result.title == "Dashboard crash on load"
        assert "issues/42" in result.issue_url

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["repo_url"] == "https://github.com/myk-org/rootcoz"
        assert call_kwargs["labels"] == ["bug"]

    async def test_uses_correct_repo_url(self):
        with patch("rootcoz.feedback.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/x/y/issues/1",
                "number": 1,
                "title": "Title",
            }
            await create_feedback_from_preview(
                title="Title",
                body="Body",
                labels=["enhancement"],
                github_token=_TEST_GITHUB_TOKEN,
            )

        mock_create.assert_called_once()
        assert (
            mock_create.call_args.kwargs["repo_url"]
            == "https://github.com/myk-org/rootcoz"
        )

    async def test_empty_github_token_raises_value_error(self):
        with pytest.raises(ValueError, match="GitHub token is required"):
            await create_feedback_from_preview(
                title="Title",
                body="Body",
                labels=["bug"],
                github_token="",
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
        assert call_kwargs.get("repo_url") == "https://github.com/myk-org/rootcoz"
        assert call_kwargs.get("github_token") is not None
        if expected_labels is not None:
            assert call_kwargs.get("labels") == expected_labels
        return call_kwargs

    async def test_creates_bug_issue(self, settings):
        req = FeedbackRequest(
            description="Dashboard crashes",
        )
        with (
            patch("rootcoz.feedback.format_feedback_with_ai") as mock_format,
            patch("rootcoz.feedback.create_github_issue") as mock_create,
        ):
            mock_format.return_value = (
                "Dashboard crash on load",
                "## Bug\n\nDetails...",
                ["bug"],
                True,
            )
            mock_create.return_value = {
                "url": "https://github.com/myk-org/rootcoz/issues/42",
                "number": 42,
                "title": "Dashboard crash on load",
            }
            result = await create_feedback_issue(
                req, settings, github_token=_TEST_GITHUB_TOKEN
            )

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
            patch("rootcoz.feedback.format_feedback_with_ai") as mock_format,
            patch("rootcoz.feedback.create_github_issue") as mock_create,
        ):
            mock_format.return_value = (
                "Add dark mode support",
                "## Feature\n\nDark mode...",
                ["enhancement"],
                True,
            )
            mock_create.return_value = {
                "url": "https://github.com/myk-org/rootcoz/issues/99",
                "number": 99,
                "title": "Add dark mode support",
            }
            result = await create_feedback_issue(
                req, settings, github_token=_TEST_GITHUB_TOKEN
            )

        assert result.issue_number == 99
        self._assert_create_issue_kwargs(mock_create, expected_labels=["enhancement"])

    async def test_uses_correct_repo_url(self, settings):
        req = FeedbackRequest(
            description="test",
        )
        with (
            patch("rootcoz.feedback.format_feedback_with_ai") as mock_format,
            patch("rootcoz.feedback.create_github_issue") as mock_create,
        ):
            mock_format.return_value = ("Title", "Body", ["enhancement"], True)
            mock_create.return_value = {
                "url": "https://github.com/x/y/issues/1",
                "number": 1,
                "title": "Title",
            }
            await create_feedback_issue(req, settings, github_token=_TEST_GITHUB_TOKEN)

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
                "ROOTCOZ_ENCRYPTION_KEY",
                "ALLOWED_USERS",
                "ENABLE_GITHUB_ISSUES",
                "AI_PROVIDER",
                "AI_MODEL",
            }
        }
        env["SECURE_COOKIES"] = "false"
        env["DB_PATH"] = str(temp_db_path)
        env["ADMIN_KEY"] = "test-admin-key-16chars"  # pragma: allowlist secret
        env["ROOTCOZ_ENCRYPTION_KEY"] = (
            "test-encryption-key-for-hmac"  # pragma: allowlist secret
        )
        env["REQUIRE_APPROVAL"] = "false"
        if github_token:
            env["GITHUB_TOKEN"] = github_token
        if enable_github_issues:
            env["ENABLE_GITHUB_ISSUES"] = enable_github_issues
        if ai_provider:
            env["AI_PROVIDER"] = ai_provider
        if ai_model:
            env["AI_MODEL"] = ai_model
        with patch.dict(os.environ, env, clear=True):
            from rootcoz.config import clear_db_settings_cache

            clear_db_settings_cache()
            with patch.object(storage, "DB_PATH", temp_db_path):
                from rootcoz.main import app

                with TestClient(
                    app, headers={"Authorization": "Bearer test-admin-key-16chars"}
                ) as c:
                    yield c
            clear_db_settings_cache()

    # -- Preview endpoint tests -----------------------------------------------

    def test_preview_works_without_server_github_token(self, _init_db, temp_db_path):
        """Preview does not need a GitHub token (AI-only formatting)."""
        for client in self._make_client(temp_db_path, github_token=""):
            with patch("rootcoz.feedback.format_feedback_with_ai") as mock_format:
                mock_format.return_value = ("Test title", "Test body", ["bug"], True)
                resp = client.post(
                    "/api/feedback/preview",
                    json={
                        "description": "Something broke",
                    },
                )
            assert resp.status_code == 200

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
            assert resp.status_code == 400
            assert "AI provider" in resp.json()["detail"]
            assert "not configured" in resp.json()["detail"]

    def test_preview_successful(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path, github_token=_TEST_GITHUB_TOKEN):
            with patch("rootcoz.feedback.format_feedback_with_ai") as mock_format:
                mock_format.return_value = ("Test title", "Test body", ["bug"], True)
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
            with patch("rootcoz.feedback.format_feedback_with_ai") as mock_format:
                mock_format.return_value = (
                    "Feature title",
                    "Feature body",
                    ["enhancement"],
                    True,
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

    def test_create_without_user_github_token_returns_400(self, _init_db, temp_db_path):
        """User without a stored GitHub token gets 400 on create."""
        for client in self._make_client(temp_db_path):
            with patch.object(storage, "get_user_tokens", return_value={}):
                resp = client.post(
                    "/api/feedback/create",
                    json={
                        "title": "Test title",
                        "body": "Test body",
                        "labels": ["bug"],
                    },
                )
            assert resp.status_code == 400
            assert "GitHub token is required" in resp.json()["detail"]
            assert "Profile Settings" in resp.json()["detail"]

    def test_create_successful(self, _init_db, temp_db_path):
        for client in self._make_client(temp_db_path):
            with (
                patch.object(
                    storage,
                    "get_user_tokens",
                    return_value={"github_token": _TEST_GITHUB_TOKEN},
                ),
                patch("rootcoz.feedback.create_github_issue") as mock_create,
            ):
                mock_create.return_value = {
                    "url": "https://github.com/myk-org/rootcoz/issues/10",
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
        for client in self._make_client(temp_db_path):
            with (
                patch.object(
                    storage,
                    "get_user_tokens",
                    return_value={"github_token": _TEST_GITHUB_TOKEN},
                ),
                patch("rootcoz.feedback.create_github_issue") as mock_create,
            ):
                mock_create.return_value = {
                    "url": "https://github.com/myk-org/rootcoz/issues/11",
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

    def test_capabilities_feedback_enabled_without_server_token(
        self, _init_db, temp_db_path
    ):
        """feedback_enabled is True even without server GITHUB_TOKEN (user tokens used)."""
        for client in self._make_client(temp_db_path, github_token=""):
            resp = client.get("/api/capabilities")
            assert resp.status_code == 200
            data = resp.json()
            assert data["feedback_enabled"] is True

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


# ---------------------------------------------------------------------------
# AI model attribution (issue #282)
# ---------------------------------------------------------------------------


class TestAiAttribution:
    _AI_MARKER = _GITHUB_FOOTER_MARKER
    _NO_AI_MARKER = "No AI model generated this issue"

    @pytest.fixture
    def settings(self):
        env = {
            "JENKINS_URL": "https://jenkins.example.com",
            "JENKINS_USER": "user",
            "JENKINS_PASSWORD": "pass",  # pragma: allowlist secret
            "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        }
        with patch.dict(os.environ, env, clear=True):
            get_settings.cache_clear()
            s = get_settings()
            get_settings.cache_clear()
            return s

    @staticmethod
    def _ai_response() -> AIResult:
        return AIResult(
            success=True,
            text=json.dumps(
                {"title": "Broken", "body": "## Bug\n\nBroken.", "labels": ["bug"]}
            ),
        )

    @staticmethod
    async def _posted_body(title: str, body: str, labels: list[str]) -> str:
        """Create the issue through the real creator and return what GitHub got."""
        response = httpx.Response(
            201,
            json={
                "number": 7,
                "title": title,
                "html_url": "https://github.com/myk-org/rootcoz/issues/7",
            },
            request=httpx.Request("POST", "https://api.github.com/repos/o/r/issues"),
        )
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("rootcoz.bug_creation.httpx.AsyncClient", return_value=client):
            await create_feedback_from_preview(
                title=title,
                body=body,
                labels=labels,
                github_token=_TEST_GITHUB_TOKEN,
            )
        return client.post.call_args.kwargs["json"]["body"]

    async def test_preview_names_provider_and_model(self, settings):
        req = FeedbackRequest(description="The button is broken")
        with patch("rootcoz.feedback.call_ai_once", return_value=self._ai_response()):
            result = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="sonnet-4-5"
            )
        assert self._AI_MARKER in result.body
        assert "(claude / sonnet-4-5)" in result.body

    async def test_preview_fallback_states_no_ai_model(self, settings):
        req = FeedbackRequest(description="The button is broken")
        with patch(
            "rootcoz.feedback.call_ai_once",
            return_value=AIResult(success=False, text="sidecar down"),
        ):
            result = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="sonnet-4-5"
            )
        assert self._NO_AI_MARKER in result.body
        assert "sonnet-4-5" not in result.body

    async def test_preview_credits_the_call_that_wrote_it_without_config(
        self, settings
    ):
        """No resolved pair still credits the AI that answered (no model name)."""
        req = FeedbackRequest(description="The button is broken")
        with patch("rootcoz.feedback.call_ai_once", return_value=self._ai_response()):
            result = await generate_feedback_preview(req, settings)
        assert self._AI_MARKER in result.body
        assert self._NO_AI_MARKER not in result.body

    async def test_cli_provider_id_is_published_as_its_public_name(self, settings):
        """Catalog `cli-*` IDs never leak; the public API names the provider."""
        req = FeedbackRequest(description="The button is broken")
        with patch("rootcoz.feedback.call_ai_once", return_value=self._ai_response()):
            preview = await generate_feedback_preview(
                req, settings, ai_provider="cli-claude", ai_model="sonnet-4-5"
            )
        assert "(claude / sonnet-4-5)" in preview.body
        assert "cli-" not in preview.body
        body = await self._posted_body(preview.title, preview.body, preview.labels)
        assert "(claude / sonnet-4-5)" in body
        assert "cli-" not in body

    async def test_create_replaces_client_supplied_attribution(self):
        spoofed = (
            "## Bug\n\nBroken.\n\n---\n*Generated using AI with "
            f"{_GITHUB_FOOTER_MARKER} (evil / spoofed-model)*"
        )
        with patch("rootcoz.feedback.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/myk-org/rootcoz/issues/7",
                "number": 7,
                "title": "Broken",
            }
            await create_feedback_from_preview(
                title="Broken",
                body=spoofed,
                labels=["bug"],
                github_token=_TEST_GITHUB_TOKEN,
                provenance=AiProvenance(
                    ai_used=True, provider="claude", model="sonnet-4-5"
                ),
            )
        body = mock_create.call_args.kwargs["body"]
        assert "spoofed-model" not in body
        assert "(claude / sonnet-4-5)" in body
        assert body.count(self._AI_MARKER) == 1

    async def test_mid_body_spoofed_footer_is_stripped(self):
        """A fake footer anywhere in the body must not survive (issue #282)."""
        spoofed = (
            "## Bug\n\nBroken.\n\n---\n*Generated using AI with "
            f"{_GITHUB_FOOTER_MARKER} (evil / spoofed-model)*\n\nExtra detail."
        )
        body = await self._posted_body("Broken", spoofed, ["bug"])
        assert "spoofed-model" not in body
        assert "Extra detail." in body
        # Exactly one footer remains: the server's (no verified provenance).
        assert body.count(self._NO_AI_MARKER) == 1
        assert self._AI_MARKER not in body

    async def test_forged_provenance_token_is_ignored(self):
        """A hand-written provenance token fails signature verification."""
        forged = (
            "## Bug\n\nBroken.\n\n---\n*Generated using AI with "
            f"{_GITHUB_FOOTER_MARKER} (evil / spoofed-model)*\n"
            '<!--rootcoz-ai:deadbeef:[true,"evil","spoofed-model","deadbeef"]-->'
        )
        body = await self._posted_body("Broken", forged, ["bug"])
        assert "spoofed-model" not in body
        assert self._NO_AI_MARKER in body
        assert body.count(self._AI_MARKER) == 0

    async def test_replayed_provenance_token_credits_no_model(self, settings):
        """A token copied from another preview cannot credit an unrelated body."""
        req = FeedbackRequest(description="The button is broken")
        with patch("rootcoz.feedback.call_ai_once", return_value=self._ai_response()):
            preview = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="sonnet-4-5"
            )
        attribution_region = preview.body[preview.body.index("\n\n---\n") :]
        replayed = (
            "## Bug\n\nSomething else entirely, hand-written.\n" + attribution_region
        )
        body = await self._posted_body("Broken", replayed, ["bug"])
        assert "sonnet-4-5" not in body
        assert body.count(self._NO_AI_MARKER) == 1
        assert self._AI_MARKER not in body

    async def test_edited_attribution_region_keeps_preview_credit(self, settings):
        """Editing inside the attribution region does not cost the credit."""
        req = FeedbackRequest(description="The button is broken")
        with patch("rootcoz.feedback.call_ai_once", return_value=self._ai_response()):
            preview = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="sonnet-4-5"
            )
        edited = preview.body.replace(
            "(claude / sonnet-4-5)*",
            "(claude / sonnet-4-5) — see the linked run*",
        )
        body = await self._posted_body(preview.title, edited, preview.labels)
        assert "(claude / sonnet-4-5)" in body
        assert body.count(self._AI_MARKER) == 1

    async def test_ai_preview_issues_exactly_one_footer(self, settings):
        """The final GitHub body carries one footer, not the legacy + model one."""
        req = FeedbackRequest(description="The button is broken")
        with patch("rootcoz.feedback.call_ai_once", return_value=self._ai_response()):
            preview = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="sonnet-4-5"
            )
        body = await self._posted_body(preview.title, preview.body, preview.labels)
        assert body.count(self._AI_MARKER) == 1
        assert "(claude / sonnet-4-5)" in body

    async def test_fallback_preview_issues_exactly_one_no_ai_footer(self, settings):
        """Raw fallback content is published as raw, with one footer."""
        req = FeedbackRequest(description="The button is broken")
        with patch(
            "rootcoz.feedback.call_ai_once",
            return_value=AIResult(success=False, text="sidecar down"),
        ):
            preview = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="sonnet-4-5"
            )
        body = await self._posted_body(preview.title, preview.body, preview.labels)
        assert body.count(self._NO_AI_MARKER) == 1
        assert body.count(self._AI_MARKER) == 0
        assert "sonnet-4-5" not in body

    async def test_create_uses_preview_provenance_only(self, settings):
        """Create credits the preview's model without any provider/model input."""
        req = FeedbackRequest(description="The button is broken")
        with patch("rootcoz.feedback.call_ai_once", return_value=self._ai_response()):
            preview = await generate_feedback_preview(
                req, settings, ai_provider="claude", ai_model="sonnet-4-5"
            )
        body = await self._posted_body(preview.title, preview.body, preview.labels)
        assert "(claude / sonnet-4-5)" in body

    async def test_create_without_ai_config_states_no_ai_model(self):
        with patch("rootcoz.feedback.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/myk-org/rootcoz/issues/8",
                "number": 8,
                "title": "Broken",
            }
            await create_feedback_from_preview(
                title="Broken",
                body="## Bug\n\nBroken.",
                labels=["bug"],
                github_token=_TEST_GITHUB_TOKEN,
            )
        body = mock_create.call_args.kwargs["body"]
        assert self._NO_AI_MARKER in body


class TestFeedbackAttributionEndpoints:
    @pytest.fixture
    def _init_db(self, temp_db_path):
        return temp_db_path

    @pytest.fixture
    def _make_client(self, _init_db, temp_db_path):
        from rootcoz.config import clear_db_settings_cache

        def _factory(
            temp_db_path, github_token="", ai_provider="claude", ai_model="test-model"
        ):
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in {
                    "GITHUB_TOKEN",
                    "ADMIN_KEY",
                    "ROOTCOZ_ENCRYPTION_KEY",
                    "ALLOWED_USERS",
                    "ENABLE_GITHUB_ISSUES",
                    "AI_PROVIDER",
                    "AI_MODEL",
                }
            }
            env["SECURE_COOKIES"] = "false"
            env["DB_PATH"] = str(temp_db_path)
            env["ADMIN_KEY"] = "test-admin-key-16chars"  # pragma: allowlist secret
            env["ROOTCOZ_ENCRYPTION_KEY"] = (
                "test-encryption-key-for-hmac"  # pragma: allowlist secret
            )
            env["REQUIRE_APPROVAL"] = "false"
            if github_token:
                env["GITHUB_TOKEN"] = github_token
            if ai_provider:
                env["AI_PROVIDER"] = ai_provider
            if ai_model:
                env["AI_MODEL"] = ai_model
            with patch.dict(os.environ, env, clear=True):
                clear_db_settings_cache()
                with patch.object(storage, "DB_PATH", temp_db_path):
                    from rootcoz.main import app

                    with TestClient(
                        app, headers={"Authorization": "Bearer test-admin-key-16chars"}
                    ) as c:
                        yield c
                clear_db_settings_cache()

        return _factory

    def test_preview_endpoint_names_server_resolved_model(
        self, _init_db, temp_db_path, _make_client
    ):
        for client in _make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            ai_provider="claude",
            ai_model="test-model",
        ):
            with patch("rootcoz.feedback.call_ai_once") as mock_ai:
                mock_ai.return_value = AIResult(
                    success=True,
                    text=json.dumps({"title": "T", "body": "B", "labels": ["bug"]}),
                )
                resp = client.post(
                    "/api/feedback/preview", json={"description": "broke"}
                )
        assert resp.status_code == 200
        assert "(claude / test-model)" in resp.json()["body"]

    def test_preview_endpoint_never_publishes_a_cli_provider_id(
        self, _init_db, temp_db_path, _make_client
    ):
        """The public API exposes `claude`, never the catalog `cli-claude`."""
        for client in _make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            ai_provider="cli-claude",
            ai_model="test-model",
        ):
            with (
                patch(
                    "rootcoz.main._validate_catalog_pair",
                    AsyncMock(return_value=("cli-claude", "test-model")),
                ) as _pair,
                patch("rootcoz.feedback.call_ai_once") as mock_ai,
            ):
                mock_ai.return_value = AIResult(
                    success=True,
                    text=json.dumps({"title": "T", "body": "B", "labels": ["bug"]}),
                )
                resp = client.post(
                    "/api/feedback/preview", json={"description": "broke"}
                )
        assert resp.status_code == 200
        body = resp.json()["body"]
        assert "(claude / test-model)" in body
        assert "cli-" not in body

    def test_preview_endpoint_ignores_body_supplied_model(
        self, _init_db, temp_db_path, _make_client
    ):
        for client in _make_client(temp_db_path, github_token=_TEST_GITHUB_TOKEN):
            with patch("rootcoz.feedback.call_ai_once") as mock_ai:
                mock_ai.return_value = AIResult(
                    success=True,
                    text=json.dumps({"title": "T", "body": "B", "labels": ["bug"]}),
                )
                resp = client.post(
                    "/api/feedback/preview",
                    json={"description": "broke", "ai_model": "spoofed-model"},
                )
        assert resp.status_code == 200
        assert "spoofed-model" not in resp.json()["body"]
        assert "(claude / test-model)" in resp.json()["body"]

    def test_create_endpoint_attribution_is_server_resolved(
        self, _init_db, temp_db_path, _make_client
    ):
        spoofed = (
            "Body\n\n---\n*Generated using AI with "
            f"{_GITHUB_FOOTER_MARKER} (evil / spoofed-model)*"
        )
        for client in _make_client(temp_db_path):
            with (
                patch.object(
                    storage,
                    "get_user_tokens",
                    return_value={"github_token": _TEST_GITHUB_TOKEN},
                ),
                patch("rootcoz.feedback.create_github_issue") as mock_create,
            ):
                mock_create.return_value = {
                    "url": "https://github.com/myk-org/rootcoz/issues/12",
                    "number": 12,
                    "title": "T",
                }
                resp = client.post(
                    "/api/feedback/create",
                    json={"title": "T", "body": spoofed, "labels": ["bug"]},
                )
            assert resp.status_code == 201
        created_body = mock_create.call_args.kwargs["body"]
        assert "spoofed-model" not in created_body
        # No server-verified provenance in the request → nothing is credited.
        assert "No AI model generated this issue" in created_body

    def test_create_endpoint_credits_preview_time_model(
        self, _init_db, temp_db_path, _make_client
    ):
        """Server Settings changing between preview and create is ignored (D)."""
        from rootcoz.config import update_db_settings_cache

        for client in _make_client(
            temp_db_path,
            github_token=_TEST_GITHUB_TOKEN,
            ai_provider="claude",
            ai_model="test-model",
        ):
            with patch("rootcoz.feedback.call_ai_once") as mock_ai:
                mock_ai.return_value = AIResult(
                    success=True,
                    text=json.dumps({"title": "T", "body": "B", "labels": ["bug"]}),
                )
                preview = client.post(
                    "/api/feedback/preview", json={"description": "broke"}
                )
            assert preview.status_code == 200
            # Admin switches the configured model before the user submits.
            update_db_settings_cache({"ai_model": "other-model"})
            with (
                patch.object(
                    storage,
                    "get_user_tokens",
                    return_value={"github_token": _TEST_GITHUB_TOKEN},
                ),
                patch("rootcoz.feedback.create_github_issue") as mock_create,
            ):
                mock_create.return_value = {
                    "url": "https://github.com/myk-org/rootcoz/issues/13",
                    "number": 13,
                    "title": "T",
                }
                resp = client.post(
                    "/api/feedback/create",
                    json={
                        "title": preview.json()["title"],
                        "body": preview.json()["body"],
                        "labels": ["bug"],
                        "ai_provider": "evil",
                        "ai_model": "evil-model",
                    },
                )
            assert resp.status_code == 201
        created_body = mock_create.call_args.kwargs["body"]
        assert "(claude / test-model)" in created_body
        assert "other-model" not in created_body
        assert "evil" not in created_body
        assert created_body.count(_GITHUB_FOOTER_MARKER) == 1
