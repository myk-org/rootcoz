"""Tests for DEBUG-level request body logging and sensitive data masking."""

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz.utils import mask_sensitive_fields
from tests.conftest import capture_main_logs

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
        assert result["jenkins_password"] == "***"
        assert result["jenkins_user"] == "***"
        assert result["jira_api_token"] == "***"
        assert result["jira_pat"] == "***"
        assert result["jira_email"] == "***"
        assert result["github_token"] == "***"
        assert result["reportportal_api_token"] == "***"
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
        assert result["custom_password"] == "***"
        assert result["my_token"] == "***"
        assert result["api_secret"] == "***"
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
        assert result["nested"]["jenkins_password"] == "***"
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
        assert result["additional_repos"][0]["token"] == "***"
        assert result["additional_repos"][1]["token"] == "***"

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
        assert result["level1"]["level2"][0]["level3"]["secret_key"] == "***"
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
        assert original["jenkins_password"] == "secret"  # pragma: allowlist secret

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
        "AI_PROVIDER": "claude",
        "AI_MODEL": "test-model",
        "DB_PATH": str(temp_db_path),
        "ADMIN_KEY": _TEST_ADMIN_KEY,  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "REQUIRE_APPROVAL": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import clear_settings_cache

        clear_settings_cache()
        try:
            yield
        finally:
            clear_settings_cache()


@pytest.fixture
def test_client(_mock_settings, temp_db_path: Path):
    """Create a synchronous test client with mocked DB path."""
    from starlette.testclient import TestClient

    from rootcoz import storage
    from rootcoz.main import app

    with (
        patch.object(storage, "DB_PATH", temp_db_path),
        TestClient(
            app, headers={"Authorization": f"Bearer {_TEST_ADMIN_KEY}"}
        ) as client,
    ):
        yield client


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
    payload = {
        "token_type": "github",
        "token": "super-secret-token-value",  # pragma: allowlist secret
        "email": "user@example.com",
    }
    with capture_main_logs(caplog, logging.DEBUG):
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


def test_skip_path_body_not_logged(test_client, caplog):
    """POST to a _BODY_LOGGING_SKIP_PATHS endpoint must NOT log the body."""
    payload = {
        "type": "jenkins",
        "job_name": "my-job",
        "build_number": 42,
    }
    with capture_main_logs(caplog, logging.DEBUG):
        test_client.post(
            "/analyze",
            json=payload,
            cookies={"rootcoz_username": "testuser"},
        )
        # The skip path logs nothing of its own, so the control has to be
        # synthetic: this record proves the capture reaches the logger before the
        # negative below.
        logging.getLogger("rootcoz.main").debug("capture control reached caplog")

    assert "capture control reached caplog" in caplog.text
    body_log = _get_debug_messages(caplog, containing="Incoming POST /analyze body:")
    assert not body_log, "/analyze is in skip-paths; body should NOT be logged"


def test_chat_issue_body_is_never_logged(test_client, caplog):
    """Chat text can hold a pasted credential: no field name marks it secret."""
    with capture_main_logs(caplog, logging.DEBUG):
        test_client.post(
            "/api/chat-issue",
            json={
                "target": "github",
                "title": "token ghp_secret",
                "body": "token ghp_secret",
            },
        )
        # A skip-path request logs nothing, so the control has to be synthetic:
        # this record proves the capture reaches the logger before the negative.
        logging.getLogger("rootcoz.main").debug("capture control reached caplog")

    assert "capture control reached caplog" in caplog.text
    body_log = _get_debug_messages(
        caplog, containing="Incoming POST /api/chat-issue body:"
    )
    assert not body_log, "chat text is in skip-paths; body should NOT be logged"


def test_validation_error_logged_at_error(test_client, caplog):
    """422 validation errors are logged at WARNING with masked body.

    Uses /api/validate-token (not in _BODY_LOGGING_SKIP_PATHS) with an
    invalid payload to trigger RequestValidationError and verify the
    error log contains masked sensitive values.
    """
    # Send a payload missing the required 'token' field
    payload = {
        "token_type": "invalid-type",
        "token": "oops-secret",  # pragma: allowlist secret
    }
    with capture_main_logs(caplog, logging.DEBUG):
        resp = test_client.post(
            "/api/validate-token",
            json=payload,
        )

    assert resp.status_code == 422

    validation_logs = _get_error_messages(caplog, containing="RequestValidationError")
    assert validation_logs, "Expected a WARNING log for the validation error"
    log_entry = validation_logs[0]
    # Sensitive values must be masked
    assert "oops-secret" not in log_entry
    assert "***" in log_entry


def test_validation_error_skip_path_no_debug_body(test_client, caplog):
    """422 on a skip-path endpoint must log errors with redacted input and body=<skipped>."""
    # Missing required fields triggers 422 on /analyze
    payload = {
        "type": "jenkins",
        "jenkins_password": "oops-secret",  # pragma: allowlist secret
    }
    with capture_main_logs(caplog, logging.DEBUG):
        resp = test_client.post(
            "/analyze",
            json=payload,
        )

    assert resp.status_code == 422

    validation_logs = _get_error_messages(caplog, containing="RequestValidationError")
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


def test_get_requests_not_logged(test_client, caplog):
    """GET requests should NOT produce body logging."""
    with capture_main_logs(caplog, logging.DEBUG):
        test_client.get(
            "/health",
            cookies={"rootcoz_username": "testuser"},
        )
        # Nothing on a GET logs, so the control has to be synthetic: this record
        # proves the capture reaches the logger before the negative below.
        logging.getLogger("rootcoz.main").debug("capture control reached caplog")

    assert "capture control reached caplog" in caplog.text
    body_logs = _get_debug_messages(caplog, containing="Incoming GET")
    body_logs = [m for m in body_logs if "body:" in m]
    assert not body_logs, "GET requests should not log a request body"


# ---------------------------------------------------------------------------
# whole-value URL masking (#562 Change 4)
# ---------------------------------------------------------------------------

_CREDENTIALED_URLS = [
    "https://user:secret@[bad/repo?token=tok",  # pragma: allowlist secret
    "HTTPS://user:secret@[bad/repo?token=tok",  # pragma: allowlist secret
    "GIT://user:secret@[bad/repo",  # pragma: allowlist secret
    "ssh://user:secret@host/repo?token=tok",  # pragma: allowlist secret
    "ftp://admin:secret@jenkins.internal/job?token=tok",  # pragma: allowlist secret
    "https://github.com/org/repo with space?token=tok",  # pragma: allowlist secret
    "https:// user:secret@host/repo?token=tok",  # pragma: allowlist secret
    "https://[bad/repo@ghp_secret",
    "https://github.com/org/repo@ghp_secret?foo=1",
    "https://github.com/org/repo@ghp_secret:main?foo=1",
]

# The `@token` suffix is a legal repository spec, so the two `@ghp_secret` forms
# above are *valid* boundaries and cannot be used to pin a 422; the route test
# uses the eight a caller boundary rejects plus a spaced `@token` variant.
_REJECTED_URLS = [
    *_CREDENTIALED_URLS[:8],
    "https:// github.com/org/repo@ghp_secret?foo=1",
]


def _assert_no_credential(masked: str) -> None:
    assert "secret" not in masked
    assert "tok" not in masked
    assert "ghp_secret" not in masked
    assert "\x1b" not in masked
    assert "\x00" not in masked


class TestMaskUrlValue:
    @pytest.mark.parametrize("url", _CREDENTIALED_URLS)
    def test_masker_masks_userinfo_query_and_at_token(self, url: str) -> None:
        from rootcoz.url_utils import mask_url_value

        masked = mask_url_value(url)
        _assert_no_credential(masked)
        assert "***" in masked
        assert "?***" in masked or "?" not in url

    @pytest.mark.parametrize("url", _CREDENTIALED_URLS)
    def test_funnel_masks_the_same_values(self, url: str) -> None:
        masked = mask_sensitive_fields({"tests_repo_url": url})["tests_repo_url"]
        _assert_no_credential(masked)

    def test_plain_url_is_unchanged(self) -> None:
        from rootcoz.url_utils import mask_url_value

        assert (
            mask_url_value("https://github.com/org/repo")
            == "https://github.com/org/repo"
        )

    def test_non_url_text_is_returned_unchanged(self) -> None:
        from rootcoz.url_utils import mask_url_value

        assert mask_sensitive_fields("hello") == "hello"
        assert mask_url_value("ab\x00cd") == "ab\x00cd"

    def test_funnel_strips_scheme_less_control_bytes(self) -> None:
        assert mask_sensitive_fields({"input": "ab\x00cd"})["input"] == "abcd"
        assert (
            mask_sensitive_fields({"input": "a\n[AUDIT] forged"})["input"]
            == "a[AUDIT] forged"
        )


class TestAnalyzeRouteUrlRefusalMasking:
    """A rejected caller URL is a 422 that leaks no credential and no control byte."""

    @pytest.mark.parametrize("url", _REJECTED_URLS)
    def test_analyze_422_names_the_field_without_leaking(
        self, test_client, caplog, url: str
    ) -> None:
        with capture_main_logs(caplog, logging.DEBUG):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "file",
                    "raw_xml": "<testsuite/>",
                    "job_name": "j",
                    "build_number": 1,
                    "tests_repo_url": url,
                },
            )
        assert response.status_code == 422, response.text
        assert "tests_repo_url" in response.text
        _assert_no_credential(response.text)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        _assert_no_credential(logged)

    def test_analyze_422_strips_scheme_less_control_bytes(self, test_client) -> None:
        response = test_client.post(
            "/analyze",
            json={
                "type": "file",
                "raw_xml": "<testsuite/>",
                "job_name": "j",
                "build_number": 1,
                "tests_repo_url": "ab\x00cd",
            },
        )
        assert response.status_code == 422, response.text
        assert "tests_repo_url" in response.text
        assert "\\u0000" not in response.text
        assert "\x00" not in response.text
