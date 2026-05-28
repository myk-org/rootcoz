"""Tests for DEBUG-level request body logging and sensitive data masking."""

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz.utils import mask_sensitive_fields

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
        assert result["jenkins_user"] == "***"
        assert result["jira_api_token"] == "***"  # noqa: S105
        assert result["jira_pat"] == "***"
        assert result["jira_email"] == "***"
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
        assert result["encryption_key"] == "***"
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
        from rootcoz.main import _mask_pydantic_error

        masked = [_mask_pydantic_error(e) for e in pydantic_errors]
        # Sensitive field input should be masked
        assert masked[0]["input"] == "***"
        # Non-sensitive field input should be preserved
        assert masked[1]["input"] is None


# ---------------------------------------------------------------------------
# Integration tests for request body logging middleware
# ---------------------------------------------------------------------------


_TEST_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret


@pytest.fixture
def _mock_settings(temp_db_path):
    """Provide minimal env for Settings, matching test_main.py pattern."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "AI_MODEL": "test-model",
        "DB_PATH": str(temp_db_path),
        "ADMIN_KEY": _TEST_ADMIN_KEY,  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "REQUIRE_APPROVAL": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def test_client(_mock_settings, temp_db_path: Path):
    """Create a synchronous test client with mocked DB path."""
    from starlette.testclient import TestClient

    from rootcoz import storage
    from rootcoz.main import app

    with patch.object(storage, "DB_PATH", temp_db_path):
        with TestClient(
            app, headers={"Authorization": f"Bearer {_TEST_ADMIN_KEY}"}
        ) as client:
            yield client


def _capture_debug_logs(caplog):
    """Enable caplog to capture DEBUG logs from the main module logger.

    simple_logger sets propagate=False, so caplog.at_level alone won't
    capture them.  We temporarily add the caplog handler and set the
    logger level to DEBUG.
    """
    from rootcoz.main import logger as main_logger

    original_level = main_logger.level
    main_logger.setLevel(logging.DEBUG)
    main_logger.addHandler(caplog.handler)
    return main_logger, original_level


def _restore_logger(main_logger, original_level, caplog):
    main_logger.removeHandler(caplog.handler)
    main_logger.setLevel(original_level)


def _get_messages_by_level(caplog, *, level: int, containing: str = "") -> list[str]:
    """Extract messages at a given level from caplog, optionally filtered by substring."""
    messages = [r.message for r in caplog.records if r.levelno == level]
    if containing:
        messages = [m for m in messages if containing in m]
    return messages


def _get_debug_messages(caplog, *, containing: str = "") -> list[str]:
    return _get_messages_by_level(caplog, level=logging.DEBUG, containing=containing)


def _get_error_messages(caplog, *, containing: str = "") -> list[str]:
    return _get_messages_by_level(caplog, level=logging.WARNING, containing=containing)


def test_middleware_logs_masked_body(test_client, caplog):
    """POST request body is logged at DEBUG with sensitive fields masked.

    Uses /api/validate-token (not in _BODY_LOGGING_SKIP_PATHS) to verify
    that body logging + masking works correctly.
    """
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        payload = {
            "token_type": "github",
            "token": "super-secret-token-value",  # pragma: allowlist secret
            "email": "user@example.com",
        }
        with caplog.at_level(logging.DEBUG):
            test_client.post(
                "/api/validate-token",
                json=payload,
                cookies={"rootcoz_username": "testuser"},
            )

        body_log = _get_debug_messages(
            caplog, containing="Incoming POST /api/validate-token body:"
        )
        assert body_log, "Expected a DEBUG log for the incoming request body"
        log_entry = body_log[0]
        # Sensitive values must be masked
        assert "super-secret-token-value" not in log_entry
        assert "***" in log_entry
        # Non-sensitive values should be present
        assert "user@example.com" in log_entry
    finally:
        _restore_logger(main_logger, orig_level, caplog)


def test_skip_path_body_not_logged(test_client, caplog):
    """POST to a _BODY_LOGGING_SKIP_PATHS endpoint must NOT log the body."""
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        payload = {
            "type": "jenkins",
            "job_name": "my-job",
            "build_number": 42,
        }
        with caplog.at_level(logging.DEBUG):
            test_client.post(
                "/analyze",
                json=payload,
                cookies={"rootcoz_username": "testuser"},
            )

        body_log = _get_debug_messages(
            caplog, containing="Incoming POST /analyze body:"
        )
        assert not body_log, "/analyze is in skip-paths; body should NOT be logged"
    finally:
        _restore_logger(main_logger, orig_level, caplog)


def test_validation_error_logged_at_error(test_client, caplog):
    """422 validation errors are logged at WARNING with masked body.

    Uses /api/validate-token (not in _BODY_LOGGING_SKIP_PATHS) with an
    invalid payload to trigger RequestValidationError and verify the
    error log contains masked sensitive values.
    """
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        # Send a payload missing the required 'token' field
        payload = {
            "token_type": "invalid-type",
            "token": "oops-secret",  # pragma: allowlist secret
        }
        with caplog.at_level(logging.DEBUG):
            resp = test_client.post(
                "/api/validate-token",
                json=payload,
            )

        assert resp.status_code == 422

        validation_logs = _get_error_messages(
            caplog, containing="RequestValidationError"
        )
        assert validation_logs, "Expected a WARNING log for the validation error"
        log_entry = validation_logs[0]
        # Sensitive values must be masked
        assert "oops-secret" not in log_entry
        assert "***" in log_entry
    finally:
        _restore_logger(main_logger, orig_level, caplog)


def test_validation_error_skip_path_no_debug_body(test_client, caplog):
    """422 on a skip-path endpoint must log errors with redacted input and body=<skipped>."""
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        # Missing required fields triggers 422 on /analyze
        payload = {
            "type": "jenkins",
            "jenkins_password": "oops-secret",  # pragma: allowlist secret
        }
        with caplog.at_level(logging.DEBUG):
            resp = test_client.post(
                "/analyze",
                json=payload,
            )

        assert resp.status_code == 422

        validation_logs = _get_error_messages(
            caplog, containing="RequestValidationError"
        )
        assert validation_logs, (
            "/analyze is in skip-paths; validation errors should still be logged"
        )
        log_text = " ".join(validation_logs)
        assert "body=<skipped>" in log_text, (
            "skip-path log must show body=<skipped>, not the actual body"
        )
        assert "oops-secret" not in log_text, (
            "sensitive input must not appear in skip-path validation log"
        )
        assert "<redacted>" in log_text, "skip-path log must redact input values"
    finally:
        _restore_logger(main_logger, orig_level, caplog)


def test_get_requests_not_logged(test_client, caplog):
    """GET requests should NOT produce body logging."""
    main_logger, orig_level = _capture_debug_logs(caplog)
    try:
        with caplog.at_level(logging.DEBUG):
            test_client.get(
                "/health",
                cookies={"rootcoz_username": "testuser"},
            )

        body_logs = _get_debug_messages(caplog, containing="Incoming GET")
        body_logs = [m for m in body_logs if "body:" in m]
        assert not body_logs, "GET requests should not log a request body"
    finally:
        _restore_logger(main_logger, orig_level, caplog)
