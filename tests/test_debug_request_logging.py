"""Tests for DEBUG-level request body logging and sensitive data masking."""

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from jenkins_job_insight.utils import mask_sensitive_fields


# ---------------------------------------------------------------------------
# Unit tests for mask_sensitive_fields
# ---------------------------------------------------------------------------


class TestMaskSensitiveFields:
    """Unit tests for the mask_sensitive_fields utility."""

    def test_masks_known_sensitive_keys(self):
        data = {
            "jenkins_password": "s3cret",  # pragma: allowlist secret
            "jenkins_user": "admin",
            "jira_api_token": "tok-abc",  # pragma: allowlist secret
            "jira_pat": "pat-xyz",  # pragma: allowlist secret
            "jira_email": "user@example.com",
            "github_token": "ghp_abc123",  # pragma: allowlist secret
            "reportportal_api_token": "rp-token",  # pragma: allowlist secret
            "job_name": "my-job",
        }
        result = mask_sensitive_fields(data)
        assert result["jenkins_password"] == "***"  # noqa: S105
        assert result["jenkins_user"] == "***"  # noqa: S105
        assert result["jira_api_token"] == "***"  # noqa: S105
        assert result["jira_pat"] == "***"  # noqa: S105
        assert result["jira_email"] == "***"  # noqa: S105
        assert result["github_token"] == "***"  # noqa: S105
        assert result["reportportal_api_token"] == "***"  # noqa: S105
        # Non-sensitive field preserved
        assert result["job_name"] == "my-job"

    def test_masks_generic_pattern_fields(self):
        data = {
            "custom_password": "hidden",  # pragma: allowlist secret
            "my_token": "hidden",  # pragma: allowlist secret
            "api_secret": "hidden",  # pragma: allowlist secret
            "encryption_key": "hidden",  # pragma: allowlist secret
            "safe_field": "visible",
        }
        result = mask_sensitive_fields(data)
        assert result["custom_password"] == "***"  # noqa: S105
        assert result["my_token"] == "***"  # noqa: S105
        assert result["api_secret"] == "***"  # noqa: S105
        assert result["encryption_key"] == "***"  # noqa: S105
        assert result["safe_field"] == "visible"

    def test_handles_nested_dicts(self):
        data = {
            "outer": "ok",
            "nested": {
                "jenkins_password": "deep-secret",  # pragma: allowlist secret
                "name": "visible",
            },
        }
        result = mask_sensitive_fields(data)
        assert result["outer"] == "ok"
        assert result["nested"]["jenkins_password"] == "***"  # noqa: S105
        assert result["nested"]["name"] == "visible"

    def test_handles_lists(self):
        data = {
            "additional_repos": [
                {
                    "name": "repo1",
                    "url": "https://example.com",
                    "token": "ghp_abc",  # pragma: allowlist secret
                },
                {
                    "name": "repo2",
                    "url": "https://example.com",
                    "token": "ghp_def",  # pragma: allowlist secret
                },
            ]
        }
        result = mask_sensitive_fields(data)
        assert result["additional_repos"][0]["name"] == "repo1"
        assert result["additional_repos"][0]["token"] == "***"  # noqa: S105
        assert result["additional_repos"][1]["token"] == "***"  # noqa: S105

    def test_handles_deeply_nested_structures(self):
        data = {
            "level1": {
                "level2": [
                    {
                        "level3": {
                            "secret_key": "deep-value",  # pragma: allowlist secret
                            "name": "ok",
                        }
                    }
                ]
            }
        }
        result = mask_sensitive_fields(data)
        assert result["level1"]["level2"][0]["level3"]["secret_key"] == "***"  # noqa: S105
        assert result["level1"]["level2"][0]["level3"]["name"] == "ok"

    def test_preserves_empty_and_falsy_values(self):
        data = {
            "jenkins_password": "",  # pragma: allowlist secret
            "github_token": None,  # pragma: allowlist secret
            "jira_pat": 0,  # pragma: allowlist secret
            "job_name": "test",
        }
        result = mask_sensitive_fields(data)
        # Empty/falsy sensitive values are NOT masked (nothing to hide)
        assert result["jenkins_password"] == ""
        assert result["github_token"] is None
        assert result["jira_pat"] == 0
        assert result["job_name"] == "test"

    def test_non_dict_non_list_passthrough(self):
        assert mask_sensitive_fields("hello") == "hello"
        assert mask_sensitive_fields(42) == 42
        assert mask_sensitive_fields(None) is None

    def test_original_data_not_mutated(self):
        original = {
            "jenkins_password": "secret",  # pragma: allowlist secret
            "name": "test",
        }
        _ = mask_sensitive_fields(original)
        assert original["jenkins_password"] == "secret"  # noqa: S105  # pragma: allowlist secret

    def test_empty_dict(self):
        assert mask_sensitive_fields({}) == {}

    def test_empty_list(self):
        assert mask_sensitive_fields([]) == []

    def test_masks_pydantic_error_input_for_sensitive_fields(self):
        """Pydantic error input values for sensitive fields are masked."""
        # Simulate a Pydantic v2 error dict with a sensitive input
        pydantic_errors = [
            {
                "type": "string_too_short",
                "loc": ["body", "github_token"],
                "msg": "String should have at least 10 characters",
                "input": "ghp_secret123",  # pragma: allowlist secret
            },
            {
                "type": "missing",
                "loc": ["body", "job_name"],
                "msg": "Field required",
                "input": None,
            },
        ]
        from jenkins_job_insight.main import _mask_pydantic_error

        masked = [_mask_pydantic_error(e) for e in pydantic_errors]
        # Sensitive field input should be masked
        assert masked[0]["input"] == "***"  # noqa: S105
        # Non-sensitive field input should be preserved
        assert masked[1]["input"] is None


# ---------------------------------------------------------------------------
# Integration tests for request body logging middleware
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_settings(temp_db_path):
    """Provide minimal env for Settings, matching test_main.py pattern."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "AI_PROVIDER": "claude",
        "AI_MODEL": "test-model",
        "DB_PATH": str(temp_db_path),
    }
    with patch.dict(os.environ, env, clear=True):
        from jenkins_job_insight.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def test_client(_mock_settings, temp_db_path: Path):
    """Create a synchronous test client with mocked DB path."""
    from starlette.testclient import TestClient

    from jenkins_job_insight import storage
    from jenkins_job_insight.main import app

    with patch.object(storage, "DB_PATH", temp_db_path):
        with TestClient(app) as client:
            yield client


def _capture_debug_logs(caplog):
    """Enable caplog to capture DEBUG logs from the main module logger.

    simple_logger sets propagate=False, so caplog.at_level alone won't
    capture them.  We temporarily add the caplog handler and set the
    logger level to DEBUG.
    """
    from jenkins_job_insight.main import logger as main_logger

    original_level = main_logger.level
    main_logger.setLevel(logging.DEBUG)
    main_logger.addHandler(caplog.handler)
    return main_logger, original_level


def _restore_logger(main_logger, original_level, caplog):
    main_logger.removeHandler(caplog.handler)
    main_logger.setLevel(original_level)


def test_middleware_logs_masked_body(test_client, caplog):
    """POST request body is logged at DEBUG with sensitive fields masked."""
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        payload = {
            "job_name": "my-job",
            "build_number": 42,
            "jenkins_password": "super-secret",  # pragma: allowlist secret
            "github_token": "test-github-value",  # pragma: allowlist secret
        }
        with caplog.at_level(logging.DEBUG):
            test_client.post(
                "/analyze",
                json=payload,
                cookies={"jji_username": "testuser"},
            )

        debug_messages = [
            r.message for r in caplog.records if r.levelno == logging.DEBUG
        ]
        body_log = [m for m in debug_messages if "Incoming POST /analyze body:" in m]
        assert body_log, "Expected a DEBUG log for the incoming request body"
        log_entry = body_log[0]
        # Sensitive values must be masked
        assert "super-secret" not in log_entry
        assert "test-github-value" not in log_entry
        assert "***" in log_entry
        # Non-sensitive values should be present
        assert "my-job" in log_entry
    finally:
        _restore_logger(main_logger, orig_level, caplog)


def test_validation_error_logged_at_debug(test_client, caplog):
    """422 validation errors are logged at DEBUG with masked body."""
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        # Send a payload missing required fields to trigger RequestValidationError
        payload = {
            "jenkins_password": "oops-secret",  # pragma: allowlist secret
        }
        with caplog.at_level(logging.DEBUG):
            resp = test_client.post(
                "/analyze",
                json=payload,
                cookies={"jji_username": "testuser"},
            )

        assert resp.status_code == 422

        debug_messages = [
            r.message for r in caplog.records if r.levelno == logging.DEBUG
        ]
        validation_logs = [m for m in debug_messages if "RequestValidationError" in m]
        assert validation_logs, "Expected a DEBUG log for the validation error"
        log_entry = validation_logs[0]
        # Sensitive values must be masked
        assert "oops-secret" not in log_entry
        assert "***" in log_entry
    finally:
        _restore_logger(main_logger, orig_level, caplog)


def test_get_requests_not_logged(test_client, caplog):
    """GET requests should NOT produce body logging."""
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        with caplog.at_level(logging.DEBUG):
            test_client.get(
                "/health",
                cookies={"jji_username": "testuser"},
            )

        debug_messages = [
            r.message for r in caplog.records if r.levelno == logging.DEBUG
        ]
        body_logs = [m for m in debug_messages if "Incoming GET" in m and "body:" in m]
        assert not body_logs, "GET requests should not log a request body"
    finally:
        _restore_logger(main_logger, orig_level, caplog)
