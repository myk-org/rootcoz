"""Tests for FastAPI main application."""

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import jenkins
import pytest
from pydantic import SecretStr

from rootcoz import storage
from rootcoz.config import Settings
from rootcoz.encryption import encrypt_sensitive_fields
from rootcoz.models import (
    AiConfigEntry,
    AnalysisDetail,
    AnalysisResult,
    FailureAnalysis,
)
from rootcoz.sources.jenkins_source import JenkinsError

# Fake credentials for tests — annotated once to suppress Ruff S105/S106 globally.
FAKE_JENKINS_PASSWORD = "not-a-real-password"  # noqa: S105  # pragma: allowlist secret
FAKE_GITHUB_TOKEN = "not-a-real-token"  # noqa: S105


def _patch_preflight():
    """Patch _preflight_sidecar_check to always return True."""
    return patch(
        "rootcoz.main._preflight_sidecar_check",
        new_callable=AsyncMock,
        return_value=True,
    )


@contextmanager
def _with_github_issue_config():
    """Temporarily enable GitHub issue creation settings for tests."""
    from rootcoz.config import get_settings

    with patch.dict(
        os.environ,
        {
            "TESTS_REPO_URL": "https://github.com/org/repo",
            "GITHUB_TOKEN": "ghp_test",  # pragma: allowlist secret
        },
    ):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@contextmanager
def _enable_feature(prop_name: str):
    """Context manager to enable a Settings boolean property for tests.

    Also patches the underlying raw fields that endpoint guards check directly
    (e.g. ``settings.tests_repo_url`` for GitHub, ``settings.jira_url`` for Jira).

    Usage::

        with _enable_feature("github_issues_enabled"):
            response = test_client.post(...)
    """
    from contextlib import ExitStack

    from rootcoz.config import get_settings

    # Map computed properties to the env vars that endpoint guards check
    raw_env_patches: dict[str, dict[str, str]] = {
        "github_issues_enabled": {
            "TESTS_REPO_URL": "https://github.com/test-org/test-repo",
            "GITHUB_TOKEN": "ghp_test_token",
            "ENABLE_GITHUB_ISSUES": "true",
        },
        "jira_enabled": {
            "JIRA_URL": "https://jira.example.com",
            "JIRA_PROJECT_KEY": "TEST",
            "JIRA_API_TOKEN": "test_jira_token",
            "ENABLE_JIRA_ISSUES": "true",
        },
    }

    with ExitStack() as stack:
        # Patch the computed property
        stack.enter_context(
            patch.object(
                Settings,
                prop_name,
                new_callable=PropertyMock,
                return_value=True,
            )
        )
        # Also patch env vars so newly-created Settings instances have raw fields set
        env_overrides = raw_env_patches.get(prop_name, {})
        if env_overrides:
            stack.enter_context(patch.dict(os.environ, env_overrides))
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def _build_wait_settings(**overrides) -> Settings:
    """Build a Settings instance with common waiting-test defaults.

    Accepts keyword overrides that are applied on top of a fresh Settings dump.

    Usage::

        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            wait_for_completion=True,
        )
    """
    settings_dict = Settings().model_dump(mode="python")
    settings_dict.update(overrides)
    return Settings.model_validate(settings_dict)


_TEST_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_TEST_ENCRYPTION_KEY = "test-encryption-key-for-hmac"  # pragma: allowlist secret


@pytest.fixture
def mock_settings(temp_db_path: Path):
    """Mock settings for tests."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "GEMINI_API_KEY": "test-key",  # pragma: allowlist secret
        "DB_PATH": str(temp_db_path),
        "ADMIN_KEY": _TEST_ADMIN_KEY,  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": _TEST_ENCRYPTION_KEY,  # pragma: allowlist secret
        "REQUIRE_APPROVAL": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        # Clear the lru_cache to use fresh settings
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


_ADMIN_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_ADMIN_KEY}"}


@pytest.fixture
def test_client(mock_settings, temp_db_path: Path):
    """Create a test client with mocked dependencies.

    Includes admin Bearer auth headers so endpoints that require
    authentication (all non-public paths) work out of the box.
    Mocks list_models and check_sidecar_available to prevent tests
    from hitting a real sidecar service.
    """
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient

        from rootcoz.main import app

        with TestClient(app, headers=_ADMIN_AUTH_HEADERS) as client:
            yield client


class TestHealthEndpoint:
    """Tests for the /health endpoint."""

    def test_health_check_returns_healthy(self, test_client) -> None:
        """Test that health check returns healthy status."""
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_health_check_method_not_allowed(self, test_client) -> None:
        """Test that POST to health returns 405."""
        response = test_client.post("/health")
        assert response.status_code == 405


class TestAnalyzeEndpoint:
    """Tests for the /analyze endpoint."""

    def test_analyze_async_returns_queued(self, test_client) -> None:
        """Test that async analyze returns queued status."""
        with patch("rootcoz.main.process_analysis_with_id"):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test",
                    "build_number": 123,
                    "tests_repo_url": "https://github.com/example/repo",
                    "ai_provider": "claude",
                    "ai_model": "test-model",
                },
            )
            assert response.status_code == 202
            data = response.json()
            assert data["status"] == "queued"
            assert data["base_url"] == ""
            assert data["result_url"].startswith("/results/")

    def test_analyze_invalid_build_number(self, test_client) -> None:
        """Test that invalid build number returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "jenkins",
                "job_name": "test",
                "build_number": "not-a-number",
                "tests_repo_url": "https://github.com/example/repo",
            },
        )
        assert response.status_code == 422

    def test_analyze_accepts_tests_repo_url_with_ref(self, test_client) -> None:
        """Test that tests_repo_url with ':ref' suffix is accepted (no URL validation)."""
        with (
            patch("rootcoz.main.AI_PROVIDER", ""),
            patch("rootcoz.main.AI_MODEL", ""),
        ):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test",
                    "build_number": 123,
                    "tests_repo_url": "https://github.com/org/repo:develop",
                },
            )
            # 400 from missing AI config, not 422 from URL validation
            assert response.status_code == 400
            assert "AI provider" in response.json()["detail"]

    def test_analyze_missing_required_field(self, test_client) -> None:
        """Test that missing required field returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "jenkins",
                "job_name": "test",
            },
        )
        assert response.status_code == 422

    def test_analyze_missing_ai_provider_returns_400(self, test_client) -> None:
        """Test that missing AI provider returns 400 before queuing."""
        with (
            patch("rootcoz.main.AI_PROVIDER", ""),
            patch("rootcoz.main.AI_MODEL", ""),
        ):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test",
                    "build_number": 123,
                    "ai_model": "test-model",
                },
            )
            assert response.status_code == 400
            assert "AI provider" in response.json()["detail"]

    def test_analyze_always_saves_request_params(self, test_client) -> None:
        """request_params is persisted even when wait_for_completion is False.

        The status page needs AI provider/model and peer configs from
        request_params regardless of whether the job is resumable.
        """
        with patch("rootcoz.main.process_analysis_with_id"):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test-job",
                    "build_number": 42,
                    "ai_provider": "claude",
                    "ai_model": "opus",
                    "wait_for_completion": False,
                },
            )
            assert response.status_code == 202
            job_id = response.json()["job_id"]

            result_resp = test_client.get(f"/results/{job_id}")
            assert result_resp.status_code in (200, 202)
            result_data = result_resp.json()["result"]
            assert "request_params" in result_data, (
                "request_params must always be saved, not only for waiting jobs"
            )
            assert result_data["request_params"]["ai_provider"] == "claude"
            assert result_data["request_params"]["ai_model"] == "opus"


class TestBaseUrlDetection:
    """Tests for base URL detection using PUBLIC_BASE_URL and header fallbacks."""

    @staticmethod
    def _analyze_body() -> dict[str, object]:
        return {
            "type": "jenkins",
            "job_name": "test",
            "build_number": 1,
            "ai_provider": "claude",
            "ai_model": "test-model",
        }

    def test_base_url_from_public_base_url(self, mock_settings, temp_db_path) -> None:
        """PUBLIC_BASE_URL takes precedence over any request header."""
        from rootcoz.config import get_settings

        os.environ["PUBLIC_BASE_URL"] = "https://myapp.example.com"
        get_settings.cache_clear()
        try:
            with patch.object(storage, "DB_PATH", temp_db_path):
                from starlette.testclient import TestClient

                from rootcoz.main import app

                with (
                    TestClient(app, headers=_ADMIN_AUTH_HEADERS) as client,
                    patch("rootcoz.main.process_analysis_with_id"),
                ):
                    response = client.post(
                        "/analyze",
                        json=self._analyze_body(),
                        headers={
                            "X-Forwarded-Proto": "https",
                            "X-Forwarded-Host": "other.example.com",
                        },
                    )
                    assert response.status_code == 202
                    data = response.json()
                    assert data["base_url"] == "https://myapp.example.com"
                    assert data["result_url"].startswith(
                        "https://myapp.example.com/results/"
                    )
        finally:
            os.environ.pop("PUBLIC_BASE_URL", None)
            get_settings.cache_clear()

    def test_base_url_from_public_base_url_strips_trailing_slash(
        self, mock_settings, temp_db_path
    ) -> None:
        """PUBLIC_BASE_URL trailing slash is stripped."""
        from rootcoz.config import get_settings

        os.environ["PUBLIC_BASE_URL"] = "https://myapp.example.com:8443/"
        get_settings.cache_clear()
        try:
            with patch.object(storage, "DB_PATH", temp_db_path):
                from starlette.testclient import TestClient

                from rootcoz.main import app

                with (
                    TestClient(app, headers=_ADMIN_AUTH_HEADERS) as client,
                    patch("rootcoz.main.process_analysis_with_id"),
                ):
                    response = client.post(
                        "/analyze",
                        json=self._analyze_body(),
                    )
                    assert response.status_code == 202
                    data = response.json()
                    assert data["base_url"] == "https://myapp.example.com:8443"
        finally:
            os.environ.pop("PUBLIC_BASE_URL", None)
            get_settings.cache_clear()

    def test_base_url_empty_without_public_base_url(self, test_client) -> None:
        """Without PUBLIC_BASE_URL, base_url is empty (relative paths)."""
        with patch("rootcoz.main.process_analysis_with_id"):
            response = test_client.post(
                "/analyze",
                json=self._analyze_body(),
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "evil.com/<script>alert(1)</script>",
                },
            )
            assert response.status_code == 202
            data = response.json()
            # Should NOT contain any host-derived URL
            assert data["base_url"] == ""
            assert data["result_url"].startswith("/results/")

    def test_base_url_ignores_forwarded_headers(self, test_client) -> None:
        """Request headers are not trusted for building public URLs."""
        with patch("rootcoz.main.process_analysis_with_id"):
            response = test_client.post(
                "/analyze",
                json=self._analyze_body(),
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "attacker.example.com",
                    "X-Forwarded-Port": "443",
                    "X-Forwarded-Prefix": "/hijacked",
                },
            )
            assert response.status_code == 202
            data = response.json()
            assert data["base_url"] == ""
            assert data["result_url"].startswith("/results/")


def _post_analyze_queued(test_client, payload: dict) -> tuple[dict, AsyncMock]:
    """Post to /analyze, assert 202/queued, and return (response_data, mock).

    Patches ``_process_file_raw_analysis`` with an ``AsyncMock``, sends the
    *payload* to ``POST /analyze``, asserts the response is 202 with
    ``status == "queued"``, and returns the parsed JSON **and** the mock so
    callers can inspect call args when needed.
    """
    with patch(
        "rootcoz.main._process_file_raw_analysis", new_callable=AsyncMock
    ) as mock_process:
        response = test_client.post("/analyze", json=payload)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        return data, mock_process


class TestAnalyzeFailuresEndpoint:
    """Tests for the unified POST /analyze endpoint with type=raw."""

    def test_analyze_failures_success(self, test_client) -> None:
        """Test that valid raw failures return 202 (async queued)."""
        data, _mock = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_foo",
                        "error_message": "assert False",
                        "stack_trace": "File test.py, line 10",
                    }
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert "job_id" in data
        assert data["base_url"] == ""
        assert data["result_url"].startswith("/results/")

    def test_analyze_failures_empty_failures(self, test_client) -> None:
        """Test that empty failures list returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "raw",
                "failures": [],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    def test_analyze_failures_missing_ai_provider(self, test_client) -> None:
        """Test that missing AI provider (no env var, no body param) returns 400."""
        with (
            patch("rootcoz.main.AI_PROVIDER", ""),
            patch("rootcoz.main.AI_MODEL", ""),
        ):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "raw",
                    "failures": [
                        {
                            "test_name": "test_foo",
                            "error_message": "assert False",
                        }
                    ],
                    "ai_model": "test-model",
                },
            )
            assert response.status_code == 400
            assert "AI provider" in response.json()["detail"]

    def test_analyze_failures_missing_ai_model(self, test_client) -> None:
        """Test that missing AI model returns 400."""
        with (
            patch("rootcoz.main.AI_PROVIDER", ""),
            patch("rootcoz.main.AI_MODEL", ""),
        ):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "raw",
                    "failures": [
                        {
                            "test_name": "test_foo",
                            "error_message": "assert False",
                        }
                    ],
                    "ai_provider": "claude",
                },
            )
            assert response.status_code == 400
            assert "AI model" in response.json()["detail"]

    def test_analyze_failures_handles_analysis_exception(self, test_client) -> None:
        """Test that when background task raises, job is still queued (202)."""
        with patch(
            "rootcoz.main._process_file_raw_analysis",
            new_callable=AsyncMock,
        ) as mock_process:
            mock_process.side_effect = RuntimeError("boom")
            data, _ = _post_analyze_queued(
                test_client,
                {
                    "type": "raw",
                    "failures": [
                        {
                            "test_name": "test_foo",
                            "error_message": "assert False",
                        }
                    ],
                    "ai_provider": "claude",
                    "ai_model": "test-model",
                },
            )
            assert "job_id" in data

    def test_analyze_failures_partial_failure(self, test_client) -> None:
        """Test that raw analysis is accepted and queued."""
        data, _mock = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error_message": "err",
                        "stack_trace": "File a.py, line 1",
                    },
                    {
                        "test_name": "test_b",
                        "error_message": "different err",
                        "stack_trace": "File b.py, line 2",
                    },
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert "job_id" in data

    def test_analyze_failures_deduplication(self, test_client) -> None:
        """Test that raw analysis request with multiple failures is accepted."""
        _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_foo",
                        "error_message": "assert False",
                        "stack_trace": "File test.py, line 10",
                    },
                    {
                        "test_name": "test_baz",
                        "error_message": "assert False",
                        "stack_trace": "File test.py, line 10",
                    },
                    {
                        "test_name": "test_bar",
                        "error_message": "KeyError: x",
                        "stack_trace": "File test.py, line 20",
                    },
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )


class TestAnalyzeFailuresRawXml:
    """Tests for the unified POST /analyze endpoint with type=file."""

    SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="2" failures="1" errors="0">
    <testcase classname="tests.test_auth" name="test_login" time="0.5">
        <failure message="assert False" type="AssertionError">
            at tests/test_auth.py:42
        </failure>
    </testcase>
    <testcase classname="tests.test_auth" name="test_logout" time="0.1"/>
</testsuite>"""

    SAMPLE_XML_NO_FAILURES = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="1" failures="0" errors="0">
    <testcase classname="tests.test_auth" name="test_ok" time="0.1"/>
</testsuite>"""

    def test_raw_xml_success(self, test_client) -> None:
        """Test that raw_xml with failures returns 202 (queued)."""
        data, _mock = _post_analyze_queued(
            test_client,
            {
                "type": "file",
                "raw_xml": self.SAMPLE_XML,
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert "job_id" in data

    def test_raw_xml_no_failures(self, test_client) -> None:
        """Test that raw_xml with no failures is still accepted (queued)."""
        _post_analyze_queued(
            test_client,
            {
                "type": "file",
                "raw_xml": self.SAMPLE_XML_NO_FAILURES,
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )

    def test_raw_xml_invalid_xml(self, test_client) -> None:
        """Test that invalid XML is still accepted for async processing (validation happens in background)."""
        # Invalid XML is now detected in background task, not at request time
        _post_analyze_queued(
            test_client,
            {
                "type": "file",
                "raw_xml": "this is not valid xml <<<<",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )

    def test_raw_xml_and_failures_mutual_exclusion(self, test_client) -> None:
        """Test that providing both raw_xml and failures for type=file returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "file",
                "raw_xml": self.SAMPLE_XML,
                "failures": [
                    {
                        "test_name": "test_foo",
                        "error_message": "assert False",
                    }
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    def test_neither_raw_xml_nor_failures_returns_422(self, test_client) -> None:
        """Test that type=raw without failures returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "raw",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    def test_failures_mode_still_works(self, test_client) -> None:
        """Test that type=raw with failures is accepted."""
        _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_foo",
                        "error_message": "assert False",
                        "stack_trace": "File test.py, line 10",
                    }
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )

    def test_raw_xml_enriched_xml_contains_analysis(self, test_client) -> None:
        """Test that type=file request is accepted for async processing."""
        _post_analyze_queued(
            test_client,
            {
                "type": "file",
                "raw_xml": self.SAMPLE_XML,
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )


class TestResultsEndpoints:
    """Tests for the /results endpoints."""

    async def test_get_result_existing(self, test_client, temp_db_path: Path) -> None:
        """Test retrieving an existing result."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-123",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="completed",
                result={"summary": "Done"},
            )

            response = test_client.get("/results/job-123")
            assert response.status_code == 200
            data = response.json()
            assert data["job_id"] == "job-123"
            assert data["base_url"] == ""
            assert data["result_url"] == "/results/job-123"

    def test_get_result_not_found(self, test_client) -> None:
        """Test retrieving non-existent result returns 404."""
        response = test_client.get("/results/non-existent")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    async def test_list_results(self, test_client, temp_db_path: Path) -> None:
        """Test listing results."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            for i in range(3):
                await storage.save_result(
                    job_id=f"job-{i}",
                    jenkins_url=f"https://jenkins.example.com/job/test/{i}/",
                    status="completed",
                )

            response = test_client.get("/results")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 3

    async def test_list_results_with_limit(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Test listing results with limit parameter."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            for i in range(10):
                await storage.save_result(
                    job_id=f"job-limit-{i}",
                    jenkins_url=f"https://jenkins.example.com/job/test/{i}/",
                    status="completed",
                )

            response = test_client.get("/results?limit=5")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 5

    def test_list_results_limit_max(self, test_client) -> None:
        """Test that limit is capped at 100."""
        response = test_client.get("/results?limit=200")
        assert response.status_code == 422  # Validation error

    def test_list_results_empty(self, test_client) -> None:
        """Test listing results when empty."""
        response = test_client.get("/results")
        assert response.status_code == 200
        assert response.json() == []

    async def test_get_result_json_format_default(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Test that default format returns JSON."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="json-job-456",
                jenkins_url="https://jenkins.example.com/job/test/2/",
                status="completed",
                result={"summary": "Done"},
            )
            response = test_client.get("/results/json-job-456")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "json-job-456"


class TestAbortEndpoint:
    """Tests for POST /results/{job_id}/abort."""

    @pytest.mark.asyncio
    async def test_abort_running_job(self, test_client, temp_db_path):
        """Aborting a running job returns status aborted."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-abort-test",
                jenkins_url="http://jenkins",
                status="running",
                result={
                    "job_name": "test",
                    "build_number": 1,
                    "request_params": {"submitted_by": "testuser"},
                },
            )
            resp = test_client.post(
                "/results/job-abort-test/abort",
                cookies={"rootcoz_username": "testuser"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "aborted"

            # Verify persisted status
            stored = await storage.get_result("job-abort-test")
            assert stored["status"] == "aborted"

    @pytest.mark.asyncio
    async def test_abort_completed_job_is_noop(self, test_client, temp_db_path):
        """Aborting a completed job returns its current status."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-completed",
                jenkins_url="http://jenkins",
                status="completed",
                result={
                    "job_name": "test",
                    "build_number": 1,
                    "request_params": {"submitted_by": "testuser"},
                },
            )
            resp = test_client.post(
                "/results/job-completed/abort",
                cookies={"rootcoz_username": "testuser"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "completed"
            assert "already" in resp.json().get("message", "").lower()

            # Verify persisted status is unchanged
            stored = await storage.get_result("job-completed")
            assert stored["status"] == "completed"

    async def test_abort_nonexistent_job(self, test_client, temp_db_path):
        """Aborting a non-existent job returns 404."""
        resp = test_client.post("/results/nonexistent-job/abort")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_abort_forbidden_for_non_owner(self, test_client, temp_db_path):
        """Non-owner non-admin gets 403 when trying to abort."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-other-user",
                jenkins_url="http://jenkins",
                status="running",
                result={
                    "job_name": "test",
                    "build_number": 1,
                    "request_params": {"submitted_by": "alice"},
                },
            )
            # Register bob and get a session cookie (non-admin user)
            reg_resp = test_client.post("/api/auth/register", json={"username": "bob"})
            assert reg_resp.status_code == 200
            bob_session = reg_resp.cookies.get("rootcoz_session")
            # Override Authorization to remove admin Bearer token
            resp = test_client.post(
                "/results/job-other-user/abort",
                headers={"Authorization": ""},
                cookies={"rootcoz_session": bob_session},
            )
            assert resp.status_code == 403


class TestAppLifespan:
    """Tests for application lifespan events."""

    def test_app_initializes_db_on_startup(
        self, mock_settings, temp_db_path: Path
    ) -> None:
        """Test that database is initialized on app startup."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            from starlette.testclient import TestClient

            from rootcoz.main import app

            with TestClient(app):
                # After startup, DB should exist with results table
                assert temp_db_path.exists()


class TestOpenAPISchema:
    """Tests for OpenAPI schema."""

    def test_openapi_schema_available(self, test_client) -> None:
        """Test that OpenAPI schema is available."""
        response = test_client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "rootcoz"
        assert schema["info"]["version"] == "0.1.0"

    def test_docs_available(self, test_client) -> None:
        """Test that docs endpoint is available."""
        response = test_client.get("/docs")
        assert response.status_code == 200


class TestSpaRoutes:
    """Tests for the React SPA route handlers."""

    @pytest.mark.parametrize(
        "path",
        [
            "/dashboard",
            "/login",
            "/",
            "/some/unknown/route",
        ],
    )
    def test_spa_route_serves_spa_or_404(
        self,
        test_client,
        path: str,
    ) -> None:
        response = test_client.get(path, follow_redirects=False)
        assert response.status_code in (200, 404)


class TestApiDashboardEndpoint:
    """Tests for the GET /api/dashboard endpoint."""

    def test_api_dashboard_returns_empty_list(self, test_client) -> None:
        """Test that GET /api/dashboard returns an empty list when no jobs exist."""
        response = test_client.get("/api/dashboard", headers=_ADMIN_AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert data == []

    @pytest.mark.parametrize("count", [3, 10])
    async def test_api_dashboard_returns_seeded_jobs(
        self, test_client, temp_db_path: Path, count: int
    ) -> None:
        """Test that GET /api/dashboard returns all seeded jobs."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            for i in range(count):
                await storage.save_result(
                    job_id=f"api-dash-{count}-{i}",
                    jenkins_url=f"https://jenkins.example.com/job/test/{i}/",
                    status="completed",
                )

            response = test_client.get("/api/dashboard", headers=_ADMIN_AUTH_HEADERS)
            assert response.status_code == 200
            data = response.json()
            assert isinstance(data, list)
            assert len(data) == count

    def test_api_dashboard_calls_storage(self, test_client) -> None:
        """Test that the endpoint delegates to list_results_for_dashboard."""
        with patch(
            "rootcoz.main.list_results_for_dashboard",
            new_callable=AsyncMock,
        ) as mock_list:
            mock_list.return_value = []
            response = test_client.get("/api/dashboard", headers=_ADMIN_AUTH_HEADERS)
            assert response.status_code == 200
            mock_list.assert_called_once_with()

    async def test_api_dashboard_includes_job_metadata(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Test that returned items include expected metadata fields."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="api-dash-meta",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="completed",
                result={
                    "job_name": "my-pipeline",
                    "build_number": 42,
                    "failures": [
                        {
                            "test_name": "test_fail",
                            "error": "assert False",
                            "analysis": {"classification": "CODE ISSUE"},
                        }
                    ],
                },
            )

            response = test_client.get("/api/dashboard", headers=_ADMIN_AUTH_HEADERS)
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            item = data[0]
            assert item["job_id"] == "api-dash-meta"
            assert item["status"] == "completed"
            assert "created_at" in item
            assert item["job_name"] == "my-pipeline"
            assert item["build_number"] == 42


class TestApiDashboardFilteredExcludeLabel:
    """Tests for exclude-tag filtering on GET /api/dashboard/filtered."""

    async def _seed_jobs_with_labels(self, temp_db_path: Path) -> None:
        """Seed three jobs with different labels for exclude tests."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            # job-a has labels ["red", "blue"]
            await storage.save_result(
                job_id="excl-a",
                jenkins_url="https://jenkins.example.com/job/a/1/",
                status="completed",
                result={"job_name": "job-a", "build_number": 1, "failures": []},
            )
            await storage.set_job_metadata("job-a", labels=["red", "blue"])
            # job-b has labels ["green"]
            await storage.save_result(
                job_id="excl-b",
                jenkins_url="https://jenkins.example.com/job/b/1/",
                status="completed",
                result={"job_name": "job-b", "build_number": 2, "failures": []},
            )
            await storage.set_job_metadata("job-b", labels=["green"])
            # job-c has labels ["blue", "green"]
            await storage.save_result(
                job_id="excl-c",
                jenkins_url="https://jenkins.example.com/job/c/1/",
                status="completed",
                result={"job_name": "job-c", "build_number": 3, "failures": []},
            )
            await storage.set_job_metadata("job-c", labels=["blue", "green"])

    async def test_single_exclude_tag_removes_matching_jobs(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Excluding 'red' should remove job-a only."""
        await self._seed_jobs_with_labels(temp_db_path)
        with patch.object(storage, "DB_PATH", temp_db_path):
            response = test_client.get(
                "/api/dashboard/filtered",
                params={"exclude_label": ["red"]},
                headers=_ADMIN_AUTH_HEADERS,
            )
        assert response.status_code == 200
        names = {j["job_name"] for j in response.json()}
        assert "job-a" not in names
        assert names == {"job-b", "job-c"}

    async def test_multiple_exclude_tags_use_or_semantics(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Excluding 'red' and 'green' should remove jobs matching ANY label."""
        await self._seed_jobs_with_labels(temp_db_path)
        with patch.object(storage, "DB_PATH", temp_db_path):
            response = test_client.get(
                "/api/dashboard/filtered",
                params={"exclude_label": ["red", "green"]},
                headers=_ADMIN_AUTH_HEADERS,
            )
        assert response.status_code == 200
        names = {j["job_name"] for j in response.json()}
        # job-a has "red", job-b has "green", job-c has both → all excluded
        assert names == set()

    async def test_include_and_exclude_together(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Include label 'blue' then exclude 'red' should keep only job-c."""
        await self._seed_jobs_with_labels(temp_db_path)
        with patch.object(storage, "DB_PATH", temp_db_path):
            response = test_client.get(
                "/api/dashboard/filtered",
                params={"label": ["blue"], "exclude_label": ["red"]},
                headers=_ADMIN_AUTH_HEADERS,
            )
        assert response.status_code == 200
        names = {j["job_name"] for j in response.json()}
        # "blue" matches job-a and job-c; exclude "red" removes job-a
        assert names == {"job-c"}

    async def test_exclude_tag_matching_no_jobs_returns_all(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Excluding a label that no job has should return all jobs."""
        await self._seed_jobs_with_labels(temp_db_path)
        with patch.object(storage, "DB_PATH", temp_db_path):
            response = test_client.get(
                "/api/dashboard/filtered",
                params={"exclude_label": ["nonexistent"]},
                headers=_ADMIN_AUTH_HEADERS,
            )
        assert response.status_code == 200
        names = {j["job_name"] for j in response.json()}
        assert names == {"job-a", "job-b", "job-c"}


class TestFaviconEndpoint:
    """Tests for the GET /favicon.ico endpoint."""

    def test_favicon_returns_svg(self, test_client) -> None:
        """Test that GET /favicon.ico returns 200 with image/svg+xml content type."""
        response = test_client.get("/favicon.ico")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"

    def test_favicon_contains_svg_content(self, test_client) -> None:
        """Test that the favicon response body contains a valid SVG tag."""
        response = test_client.get("/favicon.ico")
        assert response.status_code == 200
        assert "<svg" in response.text

    def test_favicon_has_cache_control(self, test_client) -> None:
        """Test that the favicon response has a Cache-Control header with max-age."""
        response = test_client.get("/favicon.ico")
        assert response.status_code == 200
        cache_control = response.headers.get("cache-control", "")
        assert "max-age" in cache_control


class TestCommentEndpoints:
    @pytest.mark.asyncio
    async def test_add_comment(self, test_client):
        from rootcoz import storage

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "some error",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-test-1", "http://jenkins", "completed", result_data
        )
        response = test_client.post(
            "/results/job-test-1/comments",
            json={"test_name": "test_foo", "comment": "opened bug"},
        )
        assert response.status_code == 201
        assert "id" in response.json()

    @pytest.mark.asyncio
    async def test_get_comments(self, test_client):
        from rootcoz import storage

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-test-2", "http://jenkins", "completed", result_data
        )
        await storage.add_comment("job-test-2", "test_foo", "comment 1")
        response = test_client.get("/results/job-test-2/comments")
        assert response.status_code == 200
        data = response.json()
        assert "comments" in data
        assert "reviews" in data
        assert len(data["comments"]) == 1

    @pytest.mark.asyncio
    async def test_add_comment_nonexistent_job(self, test_client):
        response = test_client.post(
            "/results/nonexistent/comments",
            json={"test_name": "test_foo", "comment": "test"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_add_comment_invalid_test_name(self, test_client):
        from rootcoz import storage

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-test-3", "http://jenkins", "completed", result_data
        )
        response = test_client.post(
            "/results/job-test-3/comments",
            json={"test_name": "nonexistent_test", "comment": "test"},
        )
        assert response.status_code == 400


class TestReviewedEndpoint:
    @pytest.mark.asyncio
    async def test_set_reviewed(self, test_client):
        from rootcoz import storage

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-rev-1", "http://jenkins", "completed", result_data
        )
        # Create a dedicated reviewer user and authenticate via Bearer token
        # so the middleware picks up their identity.
        reviewer_name = "test-reviewer"
        _, reviewer_key = await storage.create_user(reviewer_name)
        response = test_client.put(
            "/results/job-rev-1/reviewed",
            json={"test_name": "test_foo", "reviewed": True},
            headers={"Authorization": f"Bearer {reviewer_key}"},
        )
        assert response.status_code == 200
        put_data = response.json()
        assert put_data["status"] == "ok"
        assert put_data["reviewed_by"] == reviewer_name
        response = test_client.get("/results/job-rev-1/comments")
        data = response.json()
        assert "test_foo" in data["reviews"]
        assert data["reviews"]["test_foo"]["reviewed"] is True
        assert data["reviews"]["test_foo"]["username"] == reviewer_name

    @pytest.mark.asyncio
    async def test_set_reviewed_nonexistent_job(self, test_client):
        response = test_client.put(
            "/results/nonexistent/reviewed",
            json={"test_name": "test_foo", "reviewed": True},
        )
        assert response.status_code == 404


class TestReviewStatusEndpoint:
    @pytest.mark.asyncio
    async def test_get_review_status(self, test_client):
        from rootcoz import storage

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_a",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                },
                {
                    "test_name": "test_b",
                    "error": "err",
                    "analysis": {"classification": "PRODUCT BUG"},
                },
            ],
        }
        await storage.save_result(
            "job-rs-1", "http://jenkins", "completed", result_data
        )
        await storage.set_reviewed("job-rs-1", "test_a", reviewed=True)
        await storage.add_comment("job-rs-1", "test_a", "bug opened")
        response = test_client.get("/results/job-rs-1/review-status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_failures"] == 2
        assert data["reviewed_count"] == 1
        assert data["comment_count"] == 1


class TestChildScopeValidation:
    @pytest.mark.asyncio
    async def test_comment_child_job_without_build_number_accepted(self, test_client):
        """child_job_name with child_build_number=0 should be accepted and persisted."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "child_job_analyses": [
                {
                    "job_name": "child-1",
                    "build_number": 5,
                    "failures": [
                        {
                            "test_name": "test_foo",
                            "error": "err",
                            "analysis": {"classification": "CODE ISSUE"},
                        }
                    ],
                    "failed_children": [],
                }
            ],
        }
        await storage.save_result(
            "job-val-1", "http://jenkins", "completed", result_data
        )
        response = test_client.post(
            "/results/job-val-1/comments",
            json={
                "test_name": "test_foo",
                "child_job_name": "child-1",
                "child_build_number": 0,
                "comment": "test",
            },
        )
        assert response.status_code == 201
        # Verify the wildcard child scope round-tripped through storage
        comments_resp = test_client.get("/results/job-val-1/comments")
        stored_comments = comments_resp.json()["comments"]
        matching = [
            c
            for c in stored_comments
            if c["test_name"] == "test_foo" and c["child_job_name"] == "child-1"
        ]
        assert len(matching) == 1
        assert matching[0]["child_build_number"] == 0

    @pytest.mark.asyncio
    async def test_comment_build_number_without_child_job_rejected(self, test_client):
        """child_build_number without child_job_name should be rejected (422)."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-val-2", "http://jenkins", "completed", result_data
        )
        response = test_client.post(
            "/results/job-val-2/comments",
            json={
                "test_name": "test_foo",
                "child_build_number": 42,
                "comment": "test",
            },
        )
        assert response.status_code == 422


class TestGetIssuePrompt:
    """Tests for GET /results/{job_id}/issue-prompt."""

    @pytest.mark.asyncio
    async def test_returns_prompt_when_file_exists(self, test_client):
        """GET /results/{job_id}/issue-prompt returns prompt content via GitHub API."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "request_params": encrypt_sensitive_fields(
                {
                    "tests_repo_url": "https://github.com/org/repo:release-4.19",
                    "tests_repo_token": FAKE_GITHUB_TOKEN,
                }
            ),
        }
        await storage.save_result(
            "job-ip-exists", "http://jenkins", "completed", result_data
        )

        mock_response = httpx.Response(
            200,
            text="Include product version info",
            request=httpx.Request("GET", "https://api.github.com"),
        )
        with patch("rootcoz.main.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.return_value = mock_response

            response = test_client.get("/results/job-ip-exists/issue-prompt")

        assert response.status_code == 200
        assert response.json()["prompt"] == "Include product version info"
        mock_client_instance.get.assert_called_once()
        call_args = mock_client_instance.get.call_args
        assert "ref=release-4.19" in call_args.args[0]
        assert (
            call_args.kwargs["headers"]["Authorization"]
            == f"Bearer {FAKE_GITHUB_TOKEN}"
        )

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_repo_configured(self, test_client):
        """GET /results/{job_id}/issue-prompt returns empty when no repo configured."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "request_params": {
                "tests_repo_url": "",
            },
        }
        await storage.save_result(
            "job-ip-norepo", "http://jenkins", "completed", result_data
        )
        response = test_client.get("/results/job-ip-norepo/issue-prompt")

        assert response.status_code == 200
        assert response.json()["prompt"] == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self, test_client):
        """GET /results/{job_id}/issue-prompt returns empty on network error with warning."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "request_params": {
                "tests_repo_url": "https://github.com/org/repo",
                "tests_repo_token": "",
            },
        }
        await storage.save_result(
            "job-ip-neterr", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.side_effect = httpx.ConnectError(
                "connection refused"
            )

            response = test_client.get("/results/job-ip-neterr/issue-prompt")

        assert response.status_code == 200
        assert response.json()["prompt"] == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_file_not_found(self, test_client):
        """GET /results/{job_id}/issue-prompt returns empty when prompt file missing (404)."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "request_params": {
                "tests_repo_url": "https://github.com/org/repo",
                "tests_repo_token": "",
            },
        }
        await storage.save_result(
            "job-ip-nofile", "http://jenkins", "completed", result_data
        )

        mock_response = httpx.Response(
            404,
            text="Not Found",
            request=httpx.Request("GET", "https://api.github.com"),
        )
        with patch("rootcoz.main.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.return_value = mock_response

            response = test_client.get("/results/job-ip-nofile/issue-prompt")

        assert response.status_code == 200
        assert response.json()["prompt"] == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_job_not_found(self, test_client):
        """GET /results/{job_id}/issue-prompt returns empty for nonexistent job."""
        response = test_client.get("/results/nonexistent-job/issue-prompt")
        assert response.status_code == 200
        assert response.json()["prompt"] == ""

    @pytest.mark.asyncio
    async def test_private_repo_with_token(self, test_client):
        """GET /results/{job_id}/issue-prompt passes token in Authorization header."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "request_params": encrypt_sensitive_fields(
                {
                    "tests_repo_url": "https://github.com/private-org/private-repo",
                    "tests_repo_token": "ghp_secret123",
                }
            ),
        }
        await storage.save_result(
            "job-ip-private", "http://jenkins", "completed", result_data
        )

        mock_response = httpx.Response(
            200,
            text="Private repo prompt",
            request=httpx.Request("GET", "https://api.github.com"),
        )
        with patch("rootcoz.main.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.return_value = mock_response

            response = test_client.get("/results/job-ip-private/issue-prompt")

        assert response.status_code == 200
        assert response.json()["prompt"] == "Private repo prompt"
        call_args = mock_client_instance.get.call_args
        assert call_args.kwargs["headers"]["Authorization"] == "Bearer ghp_secret123"
        # URL should target the correct repo
        assert "/private-org/private-repo/" in call_args.args[0]


def _make_child_preview_result(
    *,
    child_a_error: str = "AssertionError",
    child_a_classification: str = "CODE ISSUE",
    child_a_details: str = "Child A failure details",
    child_a_evidence: str = "child-A artifact log line",
    child_b_evidence: str = "child-B artifact log line",
    child_b_details: str = "Child B failure details",
) -> dict:
    """Build a result_data dict with two sibling child jobs.

    This helper eliminates duplication across GitHub and Jira preview tests.
    """
    return {
        "status": "completed",
        "summary": "Pipeline failed",
        "jenkins_url": "http://jenkins/parent/1/",
        "failures": [],
        "child_job_analyses": [
            {
                "job_name": "child-A",
                "build_number": 10,
                "jenkins_url": "http://jenkins/child-A/10/",
                "failures": [
                    {
                        "test_name": "test_alpha",
                        "error": child_a_error,
                        "analysis": {
                            "classification": child_a_classification,
                            "details": child_a_details,
                            "artifacts_evidence": child_a_evidence,
                        },
                    }
                ],
            },
            {
                "job_name": "child-B",
                "build_number": 20,
                "jenkins_url": "http://jenkins/child-B/20/",
                "failures": [
                    {
                        "test_name": "test_beta",
                        "error": "RuntimeError",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": child_b_details,
                            "artifacts_evidence": child_b_evidence,
                        },
                    }
                ],
            },
        ],
    }


class TestPreviewGithubIssue:
    """Tests for POST /results/{job_id}/preview-github-issue."""

    @pytest.mark.asyncio
    async def test_preview_returns_title_and_body(self, test_client):
        """POST /results/{job_id}/preview-github-issue returns generated content."""

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "AssertionError: Expected 200, got 500",
                    "analysis": {
                        "classification": "CODE ISSUE",
                        "details": "Login handler missing catch",
                    },
                }
            ],
        }
        await storage.save_result(
            "job-preview-gh", "http://jenkins", "completed", result_data
        )
        with _enable_feature("github_issues_enabled"):
            with patch("rootcoz.main.generate_github_issue_content") as mock_gen:
                mock_gen.return_value = {
                    "title": "Fix: login handler missing catch",
                    "body": "## Test Failure\n\nDetails...",
                }
                with patch("rootcoz.main.search_github_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-preview-gh/preview-github-issue",
                        json={
                            "test_name": "test_login_success",
                            "ai_provider": "claude",
                            "ai_model": "opus",
                        },
                    )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Fix: login handler missing catch"
        assert "body" in data
        assert "similar_issues" in data

    @pytest.mark.asyncio
    async def test_preview_disabled_returns_403(self, test_client):
        """Preview returns 403 when GitHub issues are disabled."""
        from rootcoz.config import get_settings

        with patch.dict(
            os.environ,
            {
                "ENABLE_GITHUB_ISSUES": "false",
                "TESTS_REPO_URL": "https://github.com/test-org/test-repo",
                "GITHUB_TOKEN": "ghp_test_token",
            },
        ):
            get_settings.cache_clear()
            try:
                response = test_client.post(
                    "/results/any-job/preview-github-issue",
                    json={
                        "test_name": "test_foo",
                        "ai_provider": "claude",
                        "ai_model": "opus",
                    },
                )
            finally:
                get_settings.cache_clear()
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_preview_not_found(self, test_client):

        with _enable_feature("github_issues_enabled"):
            response = test_client.post(
                "/results/nonexistent/preview-github-issue",
                json={
                    "test_name": "tests.TestA.test_one",
                    "ai_provider": "claude",
                    "ai_model": "opus",
                },
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_preview_invalid_test(self, test_client):

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-preview-gh-2", "http://jenkins", "completed", result_data
        )
        with _enable_feature("github_issues_enabled"):
            response = test_client.post(
                "/results/job-preview-gh-2/preview-github-issue",
                json={
                    "test_name": "nonexistent_test",
                    "ai_provider": "claude",
                    "ai_model": "opus",
                },
            )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_issue_prompt_passed_to_generate(self, test_client):
        """issue_prompt field is forwarded to generate_github_issue_content."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login",
                    "error": "AssertionError",
                    "analysis": {"classification": "CODE ISSUE", "details": "x"},
                }
            ],
        }
        await storage.save_result(
            "job-prompt-gh", "http://jenkins", "completed", result_data
        )
        with _enable_feature("github_issues_enabled"):
            with patch("rootcoz.main.generate_github_issue_content") as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch("rootcoz.main.search_github_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-prompt-gh/preview-github-issue",
                        json={
                            "test_name": "test_login",
                            "issue_prompt": "Include CNV version",
                        },
                    )
        assert response.status_code == 200
        _, kwargs = mock_gen.call_args
        assert kwargs["issue_prompt"] == "Include CNV version"

    @pytest.mark.asyncio
    async def test_preview_child_job_uses_child_jenkins_url(self, test_client):
        """Preview for a child job failure uses the child's jenkins_url, not the parent's."""
        result_data = _make_child_preview_result()
        await storage.save_result(
            "job-child-preview-gh", "http://jenkins/parent/1/", "completed", result_data
        )
        with _enable_feature("github_issues_enabled"):
            with patch("rootcoz.main.generate_github_issue_content") as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch("rootcoz.main.search_github_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-child-preview-gh/preview-github-issue",
                        json={
                            "test_name": "test_alpha",
                            "child_job_name": "child-A",
                            "child_build_number": 10,
                        },
                    )
        assert response.status_code == 200
        _, kwargs = mock_gen.call_args
        # The jenkins_url should be the child's URL, not the parent's
        assert kwargs["jenkins_url"] == "http://jenkins/child-A/10/"
        # The failure should be child-A's failure, not child-B's
        assert kwargs["failure"].test_name == "test_alpha"
        assert (
            kwargs["failure"].analysis.artifacts_evidence == "child-A artifact log line"
        )

    @pytest.mark.asyncio
    async def test_preview_child_job_sibling_artifacts_not_leaked(self, test_client):
        """Sibling child job artifacts must not appear in the preview for a specific child."""
        result_data = _make_child_preview_result(
            child_a_details="Child A failure",
            child_a_evidence="child-A evidence only",
            child_b_details="Child B failure",
            child_b_evidence="child-B evidence only",
        )
        await storage.save_result(
            "job-sibling-gh", "http://jenkins/parent/1/", "completed", result_data
        )
        with _enable_feature("github_issues_enabled"):
            with patch("rootcoz.main.generate_github_issue_content") as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch("rootcoz.main.search_github_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-sibling-gh/preview-github-issue",
                        json={
                            "test_name": "test_alpha",
                            "child_job_name": "child-A",
                            "child_build_number": 10,
                        },
                    )
        assert response.status_code == 200
        _, kwargs = mock_gen.call_args
        failure = kwargs["failure"]
        # Verify only child-A's evidence is present, NOT child-B's
        assert failure.analysis.artifacts_evidence == "child-A evidence only"
        assert "child-B" not in failure.analysis.artifacts_evidence
        # Verify the jenkins_url is child-A's, not parent's or child-B's
        assert kwargs["jenkins_url"] == "http://jenkins/child-A/10/"
        assert "parent" not in kwargs["jenkins_url"]
        assert "child-B" not in kwargs["jenkins_url"]


class TestPreviewJiraBug:
    """Tests for POST /results/{job_id}/preview-jira-bug."""

    @pytest.mark.asyncio
    async def test_preview_returns_title_and_body(self, test_client):

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "TimeoutError",
                    "analysis": {
                        "classification": "PRODUCT BUG",
                        "details": "DNS timeout",
                    },
                }
            ],
        }
        await storage.save_result(
            "job-preview-jira", "http://jenkins", "completed", result_data
        )
        with _enable_feature("jira_enabled"):
            with patch("rootcoz.main.generate_jira_bug_content") as mock_gen:
                mock_gen.return_value = {
                    "title": "DNS timeout on internal resolver",
                    "body": "h2. Summary\n\nDNS resolution fails",
                }
                with patch("rootcoz.main.search_jira_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-preview-jira/preview-jira-bug",
                        json={
                            "test_name": "test_login_success",
                            "ai_provider": "claude",
                            "ai_model": "opus",
                        },
                    )
        assert response.status_code == 200
        data = response.json()
        assert data["title"]
        assert data["body"]

    @pytest.mark.asyncio
    async def test_preview_disabled_returns_403(self, test_client):
        """Preview returns 403 when Jira is disabled."""
        from rootcoz.config import get_settings

        with patch.dict(
            os.environ,
            {
                "ENABLE_JIRA_ISSUES": "false",
                "JIRA_URL": "https://jira.example.com",
                "JIRA_PROJECT_KEY": "TEST",
                "JIRA_API_TOKEN": "test_jira_token",
            },
        ):
            get_settings.cache_clear()
            try:
                response = test_client.post(
                    "/results/any-job/preview-jira-bug",
                    json={
                        "test_name": "test_foo",
                        "ai_provider": "claude",
                        "ai_model": "opus",
                    },
                )
            finally:
                get_settings.cache_clear()
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_issue_prompt_passed_to_generate(self, test_client):
        """issue_prompt field is forwarded to generate_jira_bug_content."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_dns",
                    "error": "TimeoutError",
                    "analysis": {"classification": "PRODUCT BUG", "details": "x"},
                }
            ],
        }
        await storage.save_result(
            "job-prompt-jira", "http://jenkins", "completed", result_data
        )
        with _enable_feature("jira_enabled"):
            with patch("rootcoz.main.generate_jira_bug_content") as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch("rootcoz.main.search_jira_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-prompt-jira/preview-jira-bug",
                        json={
                            "test_name": "test_dns",
                            "issue_prompt": "Include OCP version",
                        },
                    )
        assert response.status_code == 200
        _, kwargs = mock_gen.call_args
        assert kwargs["issue_prompt"] == "Include OCP version"

    @pytest.mark.asyncio
    async def test_preview_with_ai_filter_returns_normalized_schema(self, test_client):
        """AI-filtered similar issues use the same schema as unfiltered ones."""
        from rootcoz.models import JiraMatch

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_network",
                    "error": "ConnectionError",
                    "analysis": {
                        "classification": "PRODUCT BUG",
                        "details": "Connection refused",
                    },
                }
            ],
        }
        await storage.save_result(
            "job-ai-filter", "http://jenkins", "completed", result_data
        )

        unfiltered_candidates = [
            {
                "key": "TEST-100",
                "title": "Connection refused in prod",
                "summary": "Connection refused in prod",
                "description": "Detailed description",
                "url": "https://jira.example.com/browse/TEST-100",
                "status": "Open",
            },
            {
                "key": "TEST-200",
                "title": "Unrelated issue",
                "summary": "Unrelated issue",
                "description": "Not relevant",
                "url": "https://jira.example.com/browse/TEST-200",
                "status": "Closed",
            },
        ]

        ai_filtered = [
            JiraMatch(
                key="TEST-100",
                summary="Connection refused in prod",
                status="Open",
                priority="High",
                url="https://jira.example.com/browse/TEST-100",
                score=0.85,
            ),
        ]

        with _enable_feature("jira_enabled"):
            with patch("rootcoz.main.generate_jira_bug_content") as mock_gen:
                mock_gen.return_value = {
                    "title": "Connection refused",
                    "body": "h2. Summary\n\nConnection fails",
                }
                with patch("rootcoz.main.search_jira_duplicates") as mock_dup:
                    mock_dup.return_value = unfiltered_candidates
                    with patch(
                        "rootcoz.main.filter_matches_with_ai",
                        new_callable=AsyncMock,
                        return_value=ai_filtered,
                    ):
                        response = test_client.post(
                            "/results/job-ai-filter/preview-jira-bug",
                            json={
                                "test_name": "test_network",
                                "ai_provider": "claude",
                                "ai_model": "opus",
                            },
                        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["similar_issues"]) == 1
        match = data["similar_issues"][0]
        # Verify normalized schema: both 'title' and 'summary' present
        assert match["key"] == "TEST-100"
        assert match["title"] == "Connection refused in prod"
        assert match["summary"] == "Connection refused in prod"
        assert match["url"] == "https://jira.example.com/browse/TEST-100"
        assert match["status"] == "Open"
        assert match["score"] == 0.85
        assert "description" in match

    @pytest.mark.asyncio
    async def test_preview_child_job_uses_child_jenkins_url(self, test_client):
        """Preview for a child job failure uses the child's jenkins_url, not the parent's."""
        result_data = _make_child_preview_result(
            child_a_error="TimeoutError",
            child_a_classification="PRODUCT BUG",
        )
        await storage.save_result(
            "job-child-preview-jira",
            "http://jenkins/parent/1/",
            "completed",
            result_data,
        )
        with _enable_feature("jira_enabled"):
            with patch("rootcoz.main.generate_jira_bug_content") as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch("rootcoz.main.search_jira_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-child-preview-jira/preview-jira-bug",
                        json={
                            "test_name": "test_alpha",
                            "child_job_name": "child-A",
                            "child_build_number": 10,
                        },
                    )
        assert response.status_code == 200
        _, kwargs = mock_gen.call_args
        # The jenkins_url should be the child's URL, not the parent's
        assert kwargs["jenkins_url"] == "http://jenkins/child-A/10/"
        # The failure should be child-A's failure, not child-B's
        assert kwargs["failure"].test_name == "test_alpha"
        assert (
            kwargs["failure"].analysis.artifacts_evidence == "child-A artifact log line"
        )

    @pytest.mark.asyncio
    async def test_preview_child_job_sibling_artifacts_not_leaked(self, test_client):
        """Sibling child job artifacts must not appear in the Jira preview."""
        result_data = _make_child_preview_result(
            child_a_details="Child A failure",
            child_a_evidence="child-A evidence only",
            child_b_details="Child B failure",
            child_b_evidence="child-B evidence only",
        )
        await storage.save_result(
            "job-sibling-jira", "http://jenkins/parent/1/", "completed", result_data
        )
        with _enable_feature("jira_enabled"):
            with patch("rootcoz.main.generate_jira_bug_content") as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch("rootcoz.main.search_jira_duplicates") as mock_dup:
                    mock_dup.return_value = []
                    response = test_client.post(
                        "/results/job-sibling-jira/preview-jira-bug",
                        json={
                            "test_name": "test_alpha",
                            "child_job_name": "child-A",
                            "child_build_number": 10,
                        },
                    )
        assert response.status_code == 200
        _, kwargs = mock_gen.call_args
        failure = kwargs["failure"]
        # Verify only child-A's evidence is present
        assert failure.analysis.artifacts_evidence == "child-A evidence only"
        assert "child-B" not in failure.analysis.artifacts_evidence
        # Verify the jenkins_url is child-A's
        assert kwargs["jenkins_url"] == "http://jenkins/child-A/10/"
        assert "parent" not in kwargs["jenkins_url"]


class TestCreateGithubIssue:
    """Tests for POST /results/{job_id}/create-github-issue."""

    @pytest.mark.asyncio
    async def test_creates_issue_and_adds_comment(self, test_client):
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "err",
                    "error_signature": "sig123",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-create-gh", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/99",
                "number": 99,
            }
            with _with_github_issue_config():
                _, user_key = await storage.create_user("testuser")
                response = test_client.post(
                    "/results/job-create-gh/create-github-issue",
                    json={
                        "test_name": "test_login_success",
                        "title": "Bug: login fails",
                        "body": "## Details\nLogin returns 500",
                    },
                    headers={"Authorization": f"Bearer {user_key}"},
                )
        assert response.status_code == 201
        data = response.json()
        assert "https://github.com" in data["url"]
        assert data["comment_id"] > 0
        # Verify the auto-added tracker comment content and attribution
        all_comments = await storage.get_comments_for_job("job-create-gh")
        tracker_comment = next(c for c in all_comments if c["id"] == data["comment_id"])
        assert "https://github.com/org/repo/issues/99" in tracker_comment["comment"]
        assert "Bug: login fails" in tracker_comment["comment"]
        assert tracker_comment["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_create_disabled_returns_403(self, test_client):
        """Creating a GitHub issue when disabled returns 403."""
        from rootcoz.config import get_settings

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-create-gh-noconfig", "http://jenkins", "completed", result_data
        )
        with patch.dict(
            os.environ,
            {
                "ENABLE_GITHUB_ISSUES": "false",
                "TESTS_REPO_URL": "https://github.com/test-org/test-repo",
                "GITHUB_TOKEN": "ghp_test_token",
            },
        ):
            get_settings.cache_clear()
            try:
                response = test_client.post(
                    "/results/job-create-gh-noconfig/create-github-issue",
                    json={
                        "test_name": "test_foo",
                        "title": "Bug",
                        "body": "Details",
                    },
                )
            finally:
                get_settings.cache_clear()
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()


class TestCreateJiraBug:
    """Tests for POST /results/{job_id}/create-jira-bug."""

    @pytest.mark.asyncio
    async def test_creates_bug_and_adds_comment(self, test_client):

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "err",
                    "error_signature": "sig456",
                    "analysis": {"classification": "PRODUCT BUG"},
                }
            ],
        }
        await storage.save_result(
            "job-create-jira", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.create_jira_bug") as mock_create:
            mock_create.return_value = {
                "key": "PROJ-456",
                "url": "https://jira.example.com/browse/PROJ-456",
            }
            # Mock settings to have jira_enabled=True
            with _enable_feature("jira_enabled"):
                _, user_key = await storage.create_user("testuser-jira")
                response = test_client.post(
                    "/results/job-create-jira/create-jira-bug",
                    json={
                        "test_name": "test_login_success",
                        "title": "DNS timeout",
                        "body": "DNS resolution fails",
                    },
                    headers={"Authorization": f"Bearer {user_key}"},
                )
        assert response.status_code == 201
        data = response.json()
        assert data["key"] == "PROJ-456"
        assert data["comment_id"] > 0
        # Verify the auto-added tracker comment content and attribution
        all_comments = await storage.get_comments_for_job("job-create-jira")
        tracker_comment = next(c for c in all_comments if c["id"] == data["comment_id"])
        assert "PROJ-456" in tracker_comment["comment"]
        assert "DNS timeout" in tracker_comment["comment"]
        assert tracker_comment["username"] == "testuser-jira"

    @pytest.mark.asyncio
    async def test_create_jira_disabled_returns_403(self, test_client):
        """Creating a Jira bug when Jira is disabled returns 403."""
        from rootcoz.config import get_settings

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "PRODUCT BUG"},
                }
            ],
        }
        await storage.save_result(
            "job-create-jira-noconfig", "http://jenkins", "completed", result_data
        )
        with patch.dict(
            os.environ,
            {
                "ENABLE_JIRA_ISSUES": "false",
                "JIRA_URL": "https://jira.example.com",
                "JIRA_PROJECT_KEY": "TEST",
                "JIRA_API_TOKEN": "test_jira_token",
            },
        ):
            get_settings.cache_clear()
            try:
                response = test_client.post(
                    "/results/job-create-jira-noconfig/create-jira-bug",
                    json={
                        "test_name": "test_foo",
                        "title": "Bug",
                        "body": "Details",
                    },
                )
            finally:
                get_settings.cache_clear()
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()


class TestOverrideClassification:
    """Tests for PUT /results/{job_id}/override-classification."""

    @pytest.mark.asyncio
    async def test_overrides_classification(self, test_client):
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-override-1", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.storage.override_classification") as mock_override:
            response = test_client.put(
                "/results/job-override-1/override-classification",
                json={
                    "test_name": "test_login_success",
                    "classification": "PRODUCT BUG",
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["classification"] == "PRODUCT BUG"
        mock_override.assert_called_once()

    @pytest.mark.asyncio
    async def test_override_invalid_test(self, test_client):
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-override-2", "http://jenkins", "completed", result_data
        )
        response = test_client.put(
            "/results/job-override-2/override-classification",
            json={
                "test_name": "nonexistent_test",
                "classification": "CODE ISSUE",
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_override_nonexistent_job(self, test_client):
        response = test_client.put(
            "/results/nonexistent-job/override-classification",
            json={
                "test_name": "test_foo",
                "classification": "PRODUCT BUG",
            },
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_override_invalid_classification(self, test_client):
        """Invalid classification values should be rejected by Pydantic validation."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-override-3", "http://jenkins", "completed", result_data
        )
        response = test_client.put(
            "/results/job-override-3/override-classification",
            json={
                "test_name": "test_foo",
                "classification": "UNKNOWN",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_override_to_infrastructure(self, test_client):
        """Override classification to INFRASTRUCTURE clears code_fix and product_bug_report."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_deploy_check",
                    "error": "timeout",
                    "analysis": {
                        "classification": "CODE ISSUE",
                        "code_fix": {
                            "file": "deploy.py",
                            "line": "42",
                            "change": "fix timeout",
                        },
                        "product_bug_report": {
                            "title": "stale report",
                            "severity": "high",
                            "component": "deploy",
                            "description": "leftover",
                            "evidence": "none",
                        },
                    },
                }
            ],
        }
        await storage.save_result(
            "job-override-infra", "http://jenkins", "completed", result_data
        )
        with patch(
            "rootcoz.storage.override_classification",
            return_value=["test_deploy_check"],
        ) as mock_override:
            response = test_client.put(
                "/results/job-override-infra/override-classification",
                json={
                    "test_name": "test_deploy_check",
                    "classification": "INFRASTRUCTURE",
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["classification"] == "INFRASTRUCTURE"
        mock_override.assert_called_once()

        # Verify code_fix and product_bug_report are cleared from persisted result
        stored = await storage.get_result("job-override-infra")
        failure_analysis = stored["result"]["failures"][0]["analysis"]
        assert "code_fix" not in failure_analysis
        assert "product_bug_report" not in failure_analysis


class TestBugCreationIntegration:
    """Integration tests for the full bug creation flow."""

    @pytest.mark.asyncio
    async def test_full_flow_github(self, test_client):
        """Test full flow: preview -> create -> verify comment."""

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "err",
                    "error_signature": "sig789",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-integ-gh", "http://jenkins", "completed", result_data
        )
        with (
            patch("rootcoz.main.generate_github_issue_content") as mock_gen,
            patch("rootcoz.main.search_github_duplicates") as mock_dup,
            patch("rootcoz.main.create_github_issue") as mock_create,
            _enable_feature("github_issues_enabled"),
        ):
            mock_gen.return_value = {"title": "Bug title", "body": "Bug body"}
            mock_dup.return_value = []
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/1",
                "number": 1,
            }

            # Preview
            preview_resp = test_client.post(
                "/results/job-integ-gh/preview-github-issue",
                json={
                    "test_name": "test_login_success",
                    "ai_provider": "claude",
                    "ai_model": "opus",
                },
            )
            assert preview_resp.status_code == 200

            # Create (need settings with TESTS_REPO_URL and GITHUB_TOKEN)
            with _with_github_issue_config():
                create_resp = test_client.post(
                    "/results/job-integ-gh/create-github-issue",
                    json={
                        "test_name": "test_login_success",
                        "title": "Bug title",
                        "body": "Bug body",
                    },
                )
            assert create_resp.status_code == 201
            data = create_resp.json()
            assert data["comment_id"] > 0

            # Verify comment was added
            comments_resp = test_client.get("/results/job-integ-gh/comments")
            assert comments_resp.status_code == 200
            comments = comments_resp.json()["comments"]
            assert any("github.com" in c["comment"] for c in comments)

    @pytest.mark.asyncio
    async def test_override_then_verify(self, test_client):
        """Test: override classification persists and is visible on GET."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_login_success",
                    "error": "err",
                    "analysis": {"classification": "PRODUCT BUG"},
                }
            ],
        }
        await storage.save_result(
            "job-integ-override", "http://jenkins", "completed", result_data
        )
        resp = test_client.put(
            "/results/job-integ-override/override-classification",
            json={
                "test_name": "test_login_success",
                "classification": "CODE ISSUE",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["classification"] == "CODE ISSUE"

        # Verify the override persisted by fetching the result
        get_resp = test_client.get("/results/job-integ-override")
        assert get_resp.status_code == 200
        failures = get_resp.json()["result"]["failures"]
        assert failures[0]["analysis"]["classification"] == "CODE ISSUE"


class TestCreateGithubIssueApiErrors:
    """Finding 4: create-github-issue should catch external API errors and return 502."""

    @pytest.mark.asyncio
    async def test_github_api_http_error_returns_502(self, test_client):
        """HTTPStatusError from GitHub API (non-auth) should surface as 502."""
        import httpx

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-gh-err", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.HTTPStatusError(
                "Internal Server Error",
                request=httpx.Request("POST", "https://api.github.com"),
                response=httpx.Response(500),
            )
            with _with_github_issue_config():
                response = test_client.post(
                    "/results/job-gh-err/create-github-issue",
                    json={
                        "test_name": "test_foo",
                        "title": "Bug",
                        "body": "Details",
                    },
                )
        assert response.status_code == 502
        assert "GitHub API error" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_github_api_request_error_returns_502(self, test_client):
        """RequestError (network unreachable) from GitHub should surface as 502."""
        import httpx

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-gh-net-err", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.RequestError(
                "Connection refused",
                request=httpx.Request("POST", "https://api.github.com"),
            )
            with _with_github_issue_config():
                response = test_client.post(
                    "/results/job-gh-net-err/create-github-issue",
                    json={
                        "test_name": "test_foo",
                        "title": "Bug",
                        "body": "Details",
                    },
                )
        assert response.status_code == 502
        assert "GitHub API unreachable" in response.json()["detail"]


class TestCreateJiraBugApiErrors:
    """Finding 4: create-jira-bug should catch external API errors and return 502."""

    @pytest.mark.asyncio
    async def test_jira_api_http_error_returns_502(self, test_client):
        """HTTPStatusError from Jira API (non-auth) should surface as 502."""
        import httpx

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "PRODUCT BUG"},
                }
            ],
        }
        await storage.save_result(
            "job-jira-err", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.create_jira_bug") as mock_create:
            mock_create.side_effect = httpx.HTTPStatusError(
                "Internal Server Error",
                request=httpx.Request("POST", "https://jira.example.com"),
                response=httpx.Response(500),
            )
            with _enable_feature("jira_enabled"):
                response = test_client.post(
                    "/results/job-jira-err/create-jira-bug",
                    json={
                        "test_name": "test_foo",
                        "title": "Bug",
                        "body": "Details",
                    },
                )
        assert response.status_code == 502
        assert "Jira API error" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_jira_api_request_error_returns_502(self, test_client):
        """RequestError (network unreachable) from Jira should surface as 502."""
        import httpx

        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_foo",
                    "error": "err",
                    "analysis": {"classification": "PRODUCT BUG"},
                }
            ],
        }
        await storage.save_result(
            "job-jira-net-err", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.create_jira_bug") as mock_create:
            mock_create.side_effect = httpx.RequestError(
                "Connection refused",
                request=httpx.Request("POST", "https://jira.example.com"),
            )
            with _enable_feature("jira_enabled"):
                response = test_client.post(
                    "/results/job-jira-net-err/create-jira-bug",
                    json={
                        "test_name": "test_foo",
                        "title": "Bug",
                        "body": "Details",
                    },
                )
        assert response.status_code == 502
        assert "Jira API unreachable" in response.json()["detail"]


class TestHistoryEndpoints:
    """Tests for the /history/* endpoints."""

    @pytest.mark.asyncio
    async def test_get_test_history(self, test_client) -> None:
        """Test that /history/test/{test_name} returns expected structure and values."""
        response = test_client.get("/history/test/some.test.name")
        assert response.status_code == 200
        data = response.json()
        assert data["test_name"] == "some.test.name"
        # Verify all expected keys are present with correct default values
        assert data["total_runs"] == 0
        assert data["failures"] == 0
        assert data["passes"] == 0
        assert data["failure_rate"] == 0.0
        assert data["consecutive_failures"] == 0
        assert isinstance(data["recent_runs"], list)
        assert isinstance(data["comments"], list)
        assert isinstance(data["classifications"], dict)

    @pytest.mark.asyncio
    async def test_search_by_signature(self, test_client) -> None:
        """Test that /history/search returns expected structure."""
        response = test_client.get("/history/search?signature=abc123")
        assert response.status_code == 200
        data = response.json()
        assert data["signature"] == "abc123"
        assert isinstance(data.get("matches", []), list)

    @pytest.mark.asyncio
    async def test_search_by_signature_requires_param(self, test_client) -> None:
        """Test that /history/search requires signature parameter."""
        response = test_client.get("/history/search")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_job_stats(self, test_client) -> None:
        """Test that /history/stats/{job_name} returns expected structure and values."""
        response = test_client.get("/history/stats/my-job")
        assert response.status_code == 200
        data = response.json()
        assert data["job_name"] == "my-job"
        # Verify default values for a job with no history
        assert data["total_builds_analyzed"] == 0
        assert data["builds_with_failures"] == 0
        assert data["overall_failure_rate"] == 0.0
        assert isinstance(data["most_common_failures"], list)
        assert data["recent_trend"] in ("stable", "improving", "worsening")


class TestClassifyEndpoint:
    """Regression tests for POST /history/classify."""

    @pytest.mark.asyncio
    async def test_classify_child_job_with_zero_build_number(self, test_client):
        """Regression: job_name + child_build_number=0 must not raise and must persist."""
        resp = test_client.post(
            "/history/classify",
            json={
                "test_name": "some_test",
                "classification": "FLAKY",
                "job_name": "parent-job",
                "child_build_number": 0,
                "job_id": "job-classify-zero",
            },
        )
        assert resp.status_code == 201
        classification_id = resp.json()["id"]
        assert classification_id is not None
        assert isinstance(classification_id, int)
        assert classification_id > 0
        # Verify the wildcard scope was actually stored by reading the record back.
        import aiosqlite

        async with aiosqlite.connect(storage.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT child_build_number FROM test_classifications WHERE id = ?",
                (classification_id,),
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row["child_build_number"] == 0

    def test_classify_storage_value_error_returns_400(self, test_client, monkeypatch):
        """ValueError from storage layer surfaces as 400."""

        async def _boom(*args, **kwargs):
            raise ValueError("bad value")

        monkeypatch.setattr("rootcoz.main.storage.set_test_classification", _boom)
        resp = test_client.post(
            "/history/classify",
            json={
                "test_name": "t",
                "classification": "FLAKY",
                "job_id": "job-classify-err",
            },
        )
        assert resp.status_code == 400
        assert "bad value" in resp.json()["detail"]


class TestWaitForJenkinsCompletion:
    """Tests for the wait_for_jenkins_completion function."""

    @pytest.mark.asyncio
    async def test_already_completed_returns_true(self) -> None:
        """Job that is already finished returns True on first poll."""
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.return_value = {
                "building": False,
                "result": "SUCCESS",
            }

            from rootcoz.sources.jenkins_source import wait_for_jenkins_completion

            result, error = await wait_for_jenkins_completion(
                jenkins_url="https://jenkins.example.com",
                job_name="my-job",
                build_number=1,
                jenkins_user="user",
                jenkins_password=FAKE_JENKINS_PASSWORD,
                jenkins_ssl_verify=True,
                poll_interval_minutes=1,
                max_wait_minutes=5,
            )
            assert result is True
            assert error == ""
            mock_client.get_build_info_safe.assert_called_once_with("my-job", 1)

    @pytest.mark.asyncio
    async def test_running_then_completed(self, fake_clock: tuple) -> None:
        """Job that is running then completes returns True after polls."""
        fake_monotonic, fake_sleep = fake_clock

        with (
            patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls,
            patch(
                "rootcoz.sources.jenkins_source.asyncio.sleep", side_effect=fake_sleep
            ),
            patch(
                "rootcoz.sources.jenkins_source._time.monotonic",
                side_effect=fake_monotonic,
            ),
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = [
                {"building": True},
                {"building": True},
                {"building": False, "result": "FAILURE"},
            ]

            from rootcoz.sources.jenkins_source import wait_for_jenkins_completion

            result, error = await wait_for_jenkins_completion(
                jenkins_url="https://jenkins.example.com",
                job_name="my-job",
                build_number=42,
                jenkins_user="user",
                jenkins_password=FAKE_JENKINS_PASSWORD,
                jenkins_ssl_verify=False,
                poll_interval_minutes=2,
                max_wait_minutes=10,
            )
            assert result is True
            assert error == ""
            assert mock_client.get_build_info_safe.call_count == 3
            # Verify JenkinsClient was constructed with the passed-through config
            mock_cls.assert_called_once_with(
                url="https://jenkins.example.com",
                username="user",
                password=FAKE_JENKINS_PASSWORD,
                ssl_verify=False,
                timeout=30,
            )

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self, fake_clock: tuple) -> None:
        """Job that never completes returns False after deadline."""
        fake_monotonic, fake_sleep = fake_clock

        with (
            patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls,
            patch(
                "rootcoz.sources.jenkins_source.asyncio.sleep", side_effect=fake_sleep
            ),
            patch(
                "rootcoz.sources.jenkins_source._time.monotonic",
                side_effect=fake_monotonic,
            ),
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.return_value = {"building": True}

            from rootcoz.sources.jenkins_source import wait_for_jenkins_completion

            result, error = await wait_for_jenkins_completion(
                jenkins_url="https://jenkins.example.com",
                job_name="my-job",
                build_number=1,
                jenkins_user="user",
                jenkins_password=FAKE_JENKINS_PASSWORD,
                jenkins_ssl_verify=True,
                poll_interval_minutes=2,
                max_wait_minutes=6,
            )
            assert result is False
            assert "Timed out" in error
            assert "my-job" in error
            assert "6 minutes" in error
            # 6 min deadline with 2 min intervals: polls at t=0, 120, 240, 360
            # then remaining=0 breaks the loop
            assert mock_client.get_build_info_safe.call_count == 4

    @pytest.mark.asyncio
    async def test_jenkins_error_continues_polling(self, fake_clock: tuple) -> None:
        """Transient Jenkins errors do not stop polling."""
        fake_monotonic, fake_sleep = fake_clock

        with (
            patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls,
            patch(
                "rootcoz.sources.jenkins_source.asyncio.sleep", side_effect=fake_sleep
            ),
            patch(
                "rootcoz.sources.jenkins_source._time.monotonic",
                side_effect=fake_monotonic,
            ),
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = [
                OSError("connection refused"),
                {"building": False, "result": "SUCCESS"},
            ]

            from rootcoz.sources.jenkins_source import wait_for_jenkins_completion

            result, error = await wait_for_jenkins_completion(
                jenkins_url="https://jenkins.example.com",
                job_name="my-job",
                build_number=1,
                jenkins_user="user",
                jenkins_password=FAKE_JENKINS_PASSWORD,
                jenkins_ssl_verify=True,
                poll_interval_minutes=1,
                max_wait_minutes=5,
            )
            assert result is True
            assert error == ""

    @pytest.mark.asyncio
    async def test_non_transient_error_stops_polling(self) -> None:
        """Non-transient errors (e.g. bad credentials) stop polling immediately."""
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = JenkinsError(
                "bad credentials", status_code=502
            )

            from rootcoz.sources.jenkins_source import wait_for_jenkins_completion

            result, error = await wait_for_jenkins_completion(
                jenkins_url="https://jenkins.example.com",
                job_name="my-job",
                build_number=1,
                jenkins_user="user",
                jenkins_password=FAKE_JENKINS_PASSWORD,
                jenkins_ssl_verify=True,
                poll_interval_minutes=1,
                max_wait_minutes=5,
            )
            assert result is False
            assert error == "Jenkins poll failed; check server logs for details"
            mock_client.get_build_info_safe.assert_called_once()

    @pytest.mark.asyncio
    async def test_job_not_found_returns_false_immediately(self) -> None:
        """NotFoundException (404) is permanent and stops polling immediately."""
        with patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = jenkins.NotFoundException(
                "job[my-job] does not exist"
            )

            from rootcoz.sources.jenkins_source import wait_for_jenkins_completion

            result, error = await wait_for_jenkins_completion(
                jenkins_url="https://jenkins.example.com",
                job_name="my-job",
                build_number=999,
                jenkins_user="user",
                jenkins_password=FAKE_JENKINS_PASSWORD,
                jenkins_ssl_verify=True,
                poll_interval_minutes=1,
                max_wait_minutes=5,
            )
            assert result is False
            assert "not found (404)" in error
            assert "my-job" in error
            assert "999" in error
            # Should stop after the first call — no retries for 404
            mock_client.get_build_info_safe.assert_called_once_with("my-job", 999)

    @pytest.mark.asyncio
    async def test_unlimited_wait_polls_until_complete(self) -> None:
        """max_wait_minutes=0 polls indefinitely until job completes."""
        with (
            patch("rootcoz.sources.jenkins_source.JenkinsClient") as mock_cls,
            patch(
                "rootcoz.sources.jenkins_source.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = [
                {"building": True},
                {"building": True},
                {"building": True},
                {"building": False, "result": "SUCCESS"},
            ]

            from rootcoz.sources.jenkins_source import wait_for_jenkins_completion

            result, error = await wait_for_jenkins_completion(
                jenkins_url="https://jenkins.example.com",
                job_name="my-job",
                build_number=1,
                jenkins_user="user",
                jenkins_password=FAKE_JENKINS_PASSWORD,
                jenkins_ssl_verify=True,
                poll_interval_minutes=2,
                max_wait_minutes=0,
            )
            assert result is True
            assert error == ""
            assert mock_client.get_build_info_safe.call_count == 4
            assert mock_sleep.call_count == 3
            mock_sleep.assert_called_with(120)  # 2 * 60


class TestProcessAnalysisWaiting:
    """Tests for the waiting logic in process_analysis_with_id."""

    @pytest.mark.asyncio
    async def test_wait_for_completion_true_waits(self) -> None:
        """When wait_for_completion=True, sets status to 'waiting' and polls."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=True,
            poll_interval_minutes=1,
            max_wait_minutes=5,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            jenkins_user="user",
            jenkins_password=FAKE_JENKINS_PASSWORD,
            wait_for_completion=True,
            poll_interval_minutes=1,
            max_wait_minutes=5,
        )

        statuses: list[str] = []

        async def capture_status(job_id, status, result=None):
            statuses.append(status)

        with (
            patch(
                "rootcoz.main.wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ) as mock_wait,
            patch("rootcoz.main.update_status", side_effect=capture_status),
            patch(
                "rootcoz.main.safe_update_progress",
                new_callable=AsyncMock,
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch("rootcoz.main._resolve_enable_jira", return_value=False),
            patch(
                "rootcoz.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
            _patch_preflight(),
        ):
            mock_analyze.return_value = AnalysisResult(
                job_id="test-id",
                status="completed",
                summary="ok",
            )
            await process_analysis_with_id("test-id", body, merged)
            mock_wait.assert_called_once()
            assert "waiting" in statuses
            assert "running" in statuses

    @pytest.mark.asyncio
    async def test_wait_for_completion_false_skips_waiting(self) -> None:
        """When wait_for_completion=False, skip waiting entirely."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=False,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            wait_for_completion=False,
        )

        statuses: list[str] = []

        async def capture_status(job_id, status, result=None):
            statuses.append(status)

        with (
            patch(
                "rootcoz.main.wait_for_jenkins_completion",
                new_callable=AsyncMock,
            ) as mock_wait,
            patch("rootcoz.main.update_status", side_effect=capture_status),
            patch(
                "rootcoz.main.safe_update_progress",
                new_callable=AsyncMock,
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch("rootcoz.main._resolve_enable_jira", return_value=False),
            patch(
                "rootcoz.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
            _patch_preflight(),
        ):
            mock_analyze.return_value = AnalysisResult(
                job_id="test-id",
                status="completed",
                summary="ok",
            )
            await process_analysis_with_id("test-id", body, merged)
            mock_wait.assert_not_called()
            assert "waiting" not in statuses
            assert "running" in statuses

    @pytest.mark.asyncio
    async def test_wait_timeout_marks_failed(self) -> None:
        """When waiting times out, the job is marked as failed."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=True,
            max_wait_minutes=10,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            jenkins_user="user",
            jenkins_password=FAKE_JENKINS_PASSWORD,
            wait_for_completion=True,
            poll_interval_minutes=1,
            max_wait_minutes=10,
        )

        stored: list[tuple[str, dict | None]] = []

        async def capture_status(job_id, status, result=None):
            stored.append((status, result))

        with (
            patch(
                "rootcoz.main.wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(
                    False,
                    "Timed out waiting for Jenkins job my-job #1 after 10 minutes",
                ),
            ),
            patch("rootcoz.main.update_status", side_effect=capture_status),
            patch(
                "rootcoz.main.safe_update_progress",
                new_callable=AsyncMock,
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ) as mock_preserve,
            _patch_preflight(),
        ):
            await process_analysis_with_id("test-id", body, merged)
            mock_analyze.assert_not_called()
            # _preserve_request_params should have been called with fail_data
            mock_preserve.assert_called_once()
            preserve_args = mock_preserve.call_args
            assert preserve_args[0][0] == "test-id"
            assert "error" in preserve_args[0][1]
            # The last update should be a failed status with timeout error
            last_status, last_result = stored[-1]
            assert last_status == "failed"
            assert last_result is not None
            assert "Timed out" in last_result["error"]
            assert "10 minutes" in last_result["error"]

    @pytest.mark.asyncio
    async def test_no_jenkins_url_skips_waiting(self) -> None:
        """When jenkins_url is empty, skip waiting even if wait_for_completion=True."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=True,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings = _build_wait_settings(
            jenkins_url="",
            wait_for_completion=True,
            poll_interval_minutes=1,
            max_wait_minutes=5,
        )

        statuses: list[str] = []

        async def capture_status(job_id, status, result=None):
            statuses.append(status)

        with (
            patch(
                "rootcoz.main.wait_for_jenkins_completion",
                new_callable=AsyncMock,
            ) as mock_wait,
            patch("rootcoz.main.update_status", side_effect=capture_status),
            patch(
                "rootcoz.main.safe_update_progress",
                new_callable=AsyncMock,
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch("rootcoz.main._resolve_enable_jira", return_value=False),
            patch(
                "rootcoz.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
            _patch_preflight(),
        ):
            mock_analyze.return_value = AnalysisResult(
                job_id="test-id",
                status="completed",
                summary="ok",
            )
            await process_analysis_with_id("test-id", body, settings)
            mock_wait.assert_not_called()
            assert "waiting" not in statuses
            mock_analyze.assert_called_once()
            assert "running" in statuses


class TestBuildRequestParams:
    """Tests for _build_request_params helper."""

    def test_serializes_all_fields(self, mock_settings) -> None:
        """All expected fields are present in the returned dict."""
        from rootcoz.main import _build_request_params
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            ai_provider="gemini",
            ai_model="gemini-pro",
        )
        settings = Settings()
        params = _build_request_params(body, settings, "gemini", "gemini-pro")
        assert params["ai_provider"] == "gemini"
        assert params["ai_model"] == "gemini-pro"
        assert "base_url" not in params
        assert params["jenkins_url"] == settings.jenkins_url
        assert params["wait_for_completion"] == settings.wait_for_completion
        # SecretStr fields should be plain strings
        assert isinstance(params["jira_api_token"], str)
        assert isinstance(params["github_token"], str)

    def test_secrets_are_encrypted(self, mock_settings) -> None:
        """SecretStr values are encrypted, not stored as plaintext."""
        from pydantic import SecretStr

        from rootcoz.encryption import _ENCRYPTED_PREFIX, SENSITIVE_KEYS
        from rootcoz.main import _build_request_params
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="j",
            build_number=1,
            github_token=FAKE_GITHUB_TOKEN,
        )
        settings = Settings()
        merged_data = settings.model_dump(mode="python")
        merged_data["github_token"] = SecretStr(FAKE_GITHUB_TOKEN)
        merged = Settings.model_validate(merged_data)
        params = _build_request_params(body, merged, "", "")
        # Sensitive fields must carry the encryption prefix
        for key in SENSITIVE_KEYS:
            if params.get(key):
                assert params[key].startswith(_ENCRYPTED_PREFIX)
        # Specifically, the github_token must NOT be plaintext
        assert params["github_token"] != FAKE_GITHUB_TOKEN
        assert params["github_token"].startswith(_ENCRYPTED_PREFIX)


class TestReconstructFromParams:
    """Tests for _reconstruct_from_params helper."""

    def test_reconstructs_body_and_settings(self, mock_settings) -> None:
        """AnalyzeRequest and Settings are reconstructed from stored params.

        Uses _build_request_params to produce the persisted payload, validating
        the round-trip serializer/encryption contract.
        """
        from rootcoz.config import get_settings
        from rootcoz.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from rootcoz.models import AnalyzeRequest

        settings = get_settings()
        body_in = AnalyzeRequest(
            job_name="my-job",
            build_number=42,
            tests_repo_url="https://github.com/org/repo",
            ai_provider="claude",
            ai_model="opus",
            wait_for_completion=True,
            poll_interval_minutes=5,
            max_wait_minutes=60,
            enable_jira=False,
        )
        merged_in = _merge_settings(body_in, settings)
        request_params = _build_request_params(body_in, merged_in, "claude", "opus")
        result_data = {
            "job_name": "my-job",
            "build_number": 42,
            "request_params": request_params,
        }
        body, merged = _reconstruct_from_params(result_data)
        assert body.job_name == "my-job"
        assert body.build_number == 42
        assert str(body.tests_repo_url) == "https://github.com/org/repo"
        assert body.ai_provider == "claude"
        assert body.ai_model == "opus"
        assert merged.wait_for_completion is True
        assert merged.poll_interval_minutes == 5
        assert merged.max_wait_minutes == 60
        assert merged.jenkins_ssl_verify is True  # from settings default

    def test_missing_optional_fields_use_defaults(self, mock_settings) -> None:
        """Minimal request_params still produce valid objects."""
        from rootcoz.main import _reconstruct_from_params

        result_data = {
            "job_name": "j",
            "build_number": 1,
            "request_params": {
                "ai_provider": "gemini",
                "ai_model": "m",
            },
        }
        body, merged = _reconstruct_from_params(result_data)
        assert body.job_name == "j"
        assert merged.jenkins_url  # Falls back to env default

    def test_reconstruct_rehydrates_tests_repo_ref(self, mock_settings) -> None:
        """tests_repo_ref is recomposed with tests_repo_url during reconstruction."""
        from rootcoz.main import _reconstruct_from_params

        result_data = {
            "job_name": "j",
            "build_number": 1,
            "request_params": {
                "ai_provider": "claude",
                "ai_model": "m",
                "tests_repo_url": "https://github.com/org/repo",
                "tests_repo_ref": "feature/bar",
            },
        }
        body, merged = _reconstruct_from_params(result_data)
        # Body should have the recomposed url:ref format
        assert body.tests_repo_url == "https://github.com/org/repo:feature/bar"
        # Settings should also have the recomposed format
        assert merged.tests_repo_url == "https://github.com/org/repo:feature/bar"


class TestResumeWaitingJobs:
    """Tests for _resume_waiting_jobs helper."""

    async def test_resumes_valid_waiting_job(self, mock_settings) -> None:
        """A waiting job with valid request_params spawns a background task."""
        from rootcoz.config import get_settings
        from rootcoz.main import _build_request_params, _resume_waiting_jobs
        from rootcoz.models import AnalyzeRequest

        settings = get_settings()
        body_in = AnalyzeRequest(
            job_name="my-job",
            build_number=10,
            tests_repo_url="https://github.com/org/repo",
            ai_provider="gemini",
            ai_model="m",
            wait_for_completion=True,
            poll_interval_minutes=2,
            max_wait_minutes=0,
        )
        request_params = _build_request_params(body_in, settings, "gemini", "m")
        waiting_jobs = [
            {
                "job_id": "w-1",
                "result_data": {
                    "job_name": "my-job",
                    "build_number": 10,
                    "request_params": request_params,
                },
            }
        ]
        with (
            patch(
                "rootcoz.main.process_analysis_with_id",
                new_callable=AsyncMock,
            ) as mock_process,
            patch(
                "rootcoz.main.storage.get_result",
                new_callable=AsyncMock,
                return_value={"status": "waiting"},
            ),
        ):
            await _resume_waiting_jobs(waiting_jobs)
            # asyncio.create_task wraps the coroutine; give it a tick to start
            import asyncio

            await asyncio.sleep(0)
            mock_process.assert_called_once()
            call_args = mock_process.call_args
            assert call_args[0][0] == "w-1"  # job_id
            resumed_body = call_args[0][1]
            assert str(resumed_body.tests_repo_url) == "https://github.com/org/repo"

    async def test_marks_failed_when_no_request_params(
        self, mock_settings, temp_db_path: Path
    ) -> None:
        """Waiting job without request_params is marked as failed."""
        from rootcoz.main import _resume_waiting_jobs

        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                "w-old", "http://j/1", "waiting", {"job_name": "j", "build_number": 1}
            )

            waiting_jobs = [
                {
                    "job_id": "w-old",
                    "result_data": {"job_name": "j", "build_number": 1},
                }
            ]
            await _resume_waiting_jobs(waiting_jobs)

            result = await storage.get_result("w-old")
            assert result["status"] == "failed"
            assert "no request_params" in result["result"]["error"]


class TestLifespanResumesWaitingJobs:
    """Tests for waiting job resumption during lifespan startup."""

    @staticmethod
    def _prepopulate_db(
        db_path: Path, rows: list[tuple[str, str, str, str | None]]
    ) -> None:
        """Pre-populate the DB synchronously before lifespan runs.

        Uses the production ``storage.init_db()`` to create the schema so
        tests stay in sync with real startup behaviour, then inserts seed
        rows via plain sqlite3.

        Args:
            db_path: Path to the SQLite database file.
            rows: List of (job_id, jenkins_url, status, result_json) tuples.
        """
        import asyncio
        import sqlite3

        with patch.object(storage, "DB_PATH", db_path):
            asyncio.run(storage.init_db())

        conn = sqlite3.connect(str(db_path))
        for job_id, jenkins_url, status, result_json in rows:
            conn.execute(
                "INSERT INTO results (job_id, jenkins_url, status, result_json) VALUES (?, ?, ?, ?)",
                (job_id, jenkins_url, status, result_json),
            )
        conn.commit()
        conn.close()

    def test_lifespan_resumes_waiting_jobs(
        self, mock_settings, temp_db_path: Path
    ) -> None:
        """Waiting jobs are resumed (not failed) when the app starts."""
        import json

        from rootcoz.config import get_settings
        from rootcoz.main import _build_request_params
        from rootcoz.models import AnalyzeRequest

        settings = get_settings()
        body_in = AnalyzeRequest(
            job_name="my-job",
            build_number=5,
            ai_provider="gemini",
            ai_model="m",
            wait_for_completion=True,
            poll_interval_minutes=2,
            max_wait_minutes=0,
        )
        request_params = _build_request_params(body_in, settings, "gemini", "m")
        result_data = json.dumps(
            {
                "job_name": "my-job",
                "build_number": 5,
                "request_params": request_params,
            }
        )
        self._prepopulate_db(
            temp_db_path,
            [
                ("resume-1", "http://j/1", "waiting", result_data),
            ],
        )

        with patch.object(storage, "DB_PATH", temp_db_path):
            with patch(
                "rootcoz.main.process_analysis_with_id",
                new_callable=AsyncMock,
            ) as mock_process:
                import threading

                called_event = threading.Event()
                original_side_effect = mock_process.side_effect

                async def _signal_and_call(*args, **kwargs):
                    called_event.set()
                    if original_side_effect:
                        return await original_side_effect(*args, **kwargs)

                mock_process.side_effect = _signal_and_call
                # Patch away the startup delay so the deferred task runs immediately
                with patch("rootcoz.main.asyncio.sleep", new_callable=AsyncMock):
                    from starlette.testclient import TestClient

                    from rootcoz.main import app

                    with TestClient(app):
                        called_event.wait(timeout=5)
                    # The process_analysis_with_id should have been called via create_task
                    assert mock_process.called
                # Verify the waiting row was NOT flipped to failed during startup
                import sqlite3

                conn = sqlite3.connect(str(temp_db_path))
                status = conn.execute(
                    "SELECT status FROM results WHERE job_id = 'resume-1'"
                ).fetchone()[0]
                conn.close()
                assert status == "waiting"

    def test_lifespan_marks_pending_running_as_failed(
        self, mock_settings, temp_db_path: Path
    ) -> None:
        """Pending and running jobs are marked failed; waiting jobs are not."""
        import sqlite3

        self._prepopulate_db(
            temp_db_path,
            [
                ("p1", "http://j/1", "pending", None),
                ("r1", "http://j/2", "running", None),
            ],
        )

        with patch.object(storage, "DB_PATH", temp_db_path):
            from starlette.testclient import TestClient

            from rootcoz.main import app

            with TestClient(app):
                pass

            # Pending and running should be failed
            conn = sqlite3.connect(str(temp_db_path))
            conn.row_factory = sqlite3.Row
            p1 = conn.execute(
                "SELECT status FROM results WHERE job_id = 'p1'"
            ).fetchone()
            r1 = conn.execute(
                "SELECT status FROM results WHERE job_id = 'r1'"
            ).fetchone()
            conn.close()
            assert p1["status"] == "failed"
            assert r1["status"] == "failed"


class TestPeerAnalysisParams:
    """Tests for peer analysis parameter pass-through."""

    def test_analyze_with_peer_ai_configs_in_body(self, test_client) -> None:
        """POST /analyze with peer_ai_configs passes them to process_analysis_with_id."""
        with patch("rootcoz.main.process_analysis_with_id") as mock_process:
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test",
                    "build_number": 123,
                    "ai_provider": "claude",
                    "ai_model": "test-model",
                    "peer_ai_configs": [
                        {"ai_provider": "gemini", "ai_model": "pro"},
                    ],
                    "peer_analysis_max_rounds": 5,
                },
            )
            assert response.status_code == 202
            # Verify process_analysis_with_id was called
            assert mock_process.called
            # The body arg should have peer fields set
            call_args = mock_process.call_args
            body_arg = call_args[0][1]  # second positional arg
            assert body_arg.peer_ai_configs == [
                AiConfigEntry(ai_provider="gemini", ai_model="pro"),
            ]
            assert body_arg.peer_analysis_max_rounds == 5

    def test_analyze_without_peers_backward_compatible(self, test_client) -> None:
        """POST /analyze without peer fields works unchanged."""
        with patch("rootcoz.main.process_analysis_with_id") as mock_process:
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test",
                    "build_number": 123,
                    "ai_provider": "claude",
                    "ai_model": "test-model",
                },
            )
            assert response.status_code == 202
            assert mock_process.called
            body_arg = mock_process.call_args[0][1]
            assert body_arg.peer_ai_configs is None
            assert body_arg.peer_analysis_max_rounds == 3  # default

    def test_analyze_merge_settings_peer_analysis_max_rounds(self, test_client) -> None:
        """peer_analysis_max_rounds in request body overrides env default via _merge_settings."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
            peer_analysis_max_rounds=7,
        )
        settings = Settings()
        merged = _merge_settings(body, settings)
        assert merged.peer_analysis_max_rounds == 7

    def test_merge_settings_preserves_server_peer_analysis_max_rounds_when_omitted(
        self,
    ) -> None:
        """Omitted peer_analysis_max_rounds in request preserves non-default server setting."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings_data = Settings().model_dump(mode="python")
        settings_data["peer_analysis_max_rounds"] = 9
        merged = _merge_settings(body, Settings.model_validate(settings_data))

        assert merged.peer_analysis_max_rounds == 9

    def test_merge_settings_max_concurrent_ai_calls_override(self) -> None:
        """max_concurrent_ai_calls in request body overrides env default via _merge_settings."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
            max_concurrent_ai_calls=7,
        )
        settings = Settings()
        merged = _merge_settings(body, settings)
        assert merged.max_concurrent_ai_calls == 7

    def test_merge_settings_max_concurrent_ai_calls_preserves_server_setting_when_omitted(
        self,
    ) -> None:
        """Omitted max_concurrent_ai_calls preserves the existing server setting."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings = Settings(max_concurrent_ai_calls=9)
        merged = _merge_settings(body, settings)
        assert merged.max_concurrent_ai_calls == 9

    def test_resolve_peer_ai_configs_none_uses_env(self, test_client) -> None:
        """When peer_ai_configs is None in request, _resolve_peer_ai_configs falls back to env default."""
        from rootcoz.main import _merge_settings, _resolve_peer_ai_configs
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings = Settings()
        merged = _merge_settings(body, settings)
        # Default env is "", so _resolve_peer_ai_configs returns None
        result = _resolve_peer_ai_configs(body, merged)
        assert result is None

    def test_resolve_peer_ai_configs_uses_env_when_set(self, test_client) -> None:
        """When PEER_AI_CONFIGS env var is set and request omits peer_ai_configs, env default is used."""
        from rootcoz.main import _resolve_peer_ai_configs
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings_data = Settings().model_dump(mode="python")
        settings_data["peer_ai_configs"] = "gemini:pro"
        merged = Settings.model_validate(settings_data)
        result = _resolve_peer_ai_configs(body, merged)
        assert result is not None
        assert len(result) == 1
        assert result[0]["ai_provider"] == "gemini"
        assert result[0]["ai_model"] == "pro"

    def test_resolve_peer_ai_configs_explicit_empty_disables_peers(self) -> None:
        """Explicit peer_ai_configs=[] disables peers even when PEER_AI_CONFIGS env var is set."""
        from rootcoz.main import _resolve_peer_ai_configs
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
            peer_ai_configs=[],
        )
        settings_data = Settings().model_dump(mode="python")
        settings_data["peer_ai_configs"] = "gemini:pro"
        merged = Settings.model_validate(settings_data)
        result = _resolve_peer_ai_configs(body, merged)
        assert result is None

    def test_build_reconstruct_roundtrip_peer_params(self, mock_settings) -> None:
        """peer_ai_configs and peer_analysis_max_rounds round-trip through build/reconstruct."""
        from rootcoz.config import get_settings
        from rootcoz.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from rootcoz.models import AnalyzeRequest

        settings = get_settings()
        peer_configs = [
            AiConfigEntry(ai_provider="gemini", ai_model="pro"),
        ]
        body_in = AnalyzeRequest(
            job_name="my-job",
            build_number=42,
            ai_provider="claude",
            ai_model="opus",
            peer_ai_configs=peer_configs,
            peer_analysis_max_rounds=5,
        )
        merged_in = _merge_settings(body_in, settings)
        request_params = _build_request_params(
            body_in,
            merged_in,
            "claude",
            "opus",
            peer_ai_configs_resolved=peer_configs,
        )
        result_data = {
            "job_name": "my-job",
            "build_number": 42,
            "request_params": request_params,
        }
        body_out, merged_out = _reconstruct_from_params(result_data)
        assert body_out.peer_ai_configs == [
            AiConfigEntry(ai_provider="gemini", ai_model="pro"),
        ]
        assert body_out.peer_analysis_max_rounds == 5
        assert merged_out.peer_analysis_max_rounds == 5

    def test_analyze_failures_with_peer_ai_configs(self, test_client) -> None:
        """POST /analyze with type=raw and peer_ai_configs returns 202 and forwards peer settings."""
        data, mock_process = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_foo",
                        "error_message": "assert False",
                        "stack_trace": "File test.py, line 10",
                    }
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
                "peer_ai_configs": [
                    {"ai_provider": "gemini", "ai_model": "pro"},
                ],
                "peer_analysis_max_rounds": 7,
            },
        )
        assert "job_id" in data

        # Verify peer-analysis settings survive the queuing
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args.kwargs
        peer_configs = call_kwargs["peer_ai_configs"]
        assert peer_configs is not None
        assert len(peer_configs) == 1
        assert peer_configs[0].ai_provider == "gemini"
        assert peer_configs[0].ai_model == "pro"
        merged = call_kwargs["merged"]
        assert merged.peer_analysis_max_rounds == 7

    def test_build_request_params_stores_resolved_peer_configs(
        self, mock_settings
    ) -> None:
        """_build_request_params stores the resolved peer configs, not raw body."""
        from rootcoz.main import _build_request_params
        from rootcoz.models import AnalyzeRequest

        # Body has peer_ai_configs=None (not provided by caller)
        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            ai_provider="claude",
            ai_model="opus",
            # peer_ai_configs is None (not provided)
        )
        settings = Settings()
        resolved = [
            AiConfigEntry(ai_provider="gemini", ai_model="pro"),
        ]
        params = _build_request_params(
            body, settings, "claude", "opus", peer_ai_configs_resolved=resolved
        )
        # Stored value should be the resolved list, not the raw body value
        assert len(params["peer_ai_configs"]) == 1
        stored = params["peer_ai_configs"][0]
        if isinstance(stored, dict):
            assert stored["ai_provider"] == "gemini"
            assert stored["ai_model"] == "pro"
        else:
            assert stored.ai_provider == "gemini"

    def test_reconstruct_uses_stored_peer_configs_directly(self, mock_settings) -> None:
        """_reconstruct_from_params uses stored peer_ai_configs without re-resolving from env."""
        from rootcoz.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from rootcoz.models import AnalyzeRequest

        settings = Settings()
        body_in = AnalyzeRequest(
            job_name="my-job",
            build_number=42,
            ai_provider="claude",
            ai_model="opus",
            # peer_ai_configs=None in original request
        )
        merged = _merge_settings(body_in, settings)
        resolved = [
            AiConfigEntry(ai_provider="gemini", ai_model="pro"),
        ]
        request_params = _build_request_params(
            body_in, merged, "claude", "opus", peer_ai_configs_resolved=resolved
        )
        result_data = {
            "job_name": "my-job",
            "build_number": 42,
            "request_params": request_params,
        }
        body_out, _ = _reconstruct_from_params(result_data)
        # Reconstructed body should have the resolved peer configs
        assert body_out.peer_ai_configs is not None
        assert len(body_out.peer_ai_configs) == 1
        assert body_out.peer_ai_configs[0].ai_provider == "gemini"

    def test_reconstruct_empty_peer_configs_preserved(self, mock_settings) -> None:
        """When peer_ai_configs was explicitly disabled ([]), reconstruction preserves empty list."""
        from rootcoz.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from rootcoz.models import AnalyzeRequest

        settings = Settings()
        body_in = AnalyzeRequest(
            job_name="my-job",
            build_number=42,
            ai_provider="claude",
            ai_model="opus",
            peer_ai_configs=[],  # Explicitly disabled
        )
        merged = _merge_settings(body_in, settings)
        # Resolved is None because [] means explicitly disabled
        request_params = _build_request_params(
            body_in, merged, "claude", "opus", peer_ai_configs_resolved=None
        )
        result_data = {
            "job_name": "my-job",
            "build_number": 42,
            "request_params": request_params,
        }
        body_out, _ = _reconstruct_from_params(result_data)
        # peer_ai_configs should be [] (explicitly disabled, preserved on resume)
        assert body_out.peer_ai_configs == []

    def test_reconstruct_legacy_job_missing_peer_key(self, mock_settings) -> None:
        """Legacy waiting jobs without peer_ai_configs key get [] (disabled), not None."""
        from rootcoz.main import _reconstruct_from_params

        # Simulate a legacy stored job that predates the peer analysis feature
        legacy_params = {
            "ai_provider": "claude",
            "ai_model": "opus",
            "wait_for_completion": True,
            "poll_interval_minutes": 2,
            "max_wait_minutes": 0,
            # No peer_ai_configs key at all — legacy job
        }
        result_data = {
            "job_name": "legacy-job",
            "build_number": 99,
            "request_params": legacy_params,
        }
        body_out, _ = _reconstruct_from_params(result_data)
        # Must be [] (disable peers), not None (which would use server default)
        assert body_out.peer_ai_configs == []


class TestProgressPhaseTracking:
    """Tests for progress_phase updates during process_analysis_with_id."""

    @pytest.mark.asyncio
    async def test_progress_phases_with_jenkins_wait(self) -> None:
        """When waiting for Jenkins, progress phases include waiting_for_jenkins and analyzing."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=True,
            poll_interval_minutes=1,
            max_wait_minutes=5,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            jenkins_user="user",
            jenkins_password=FAKE_JENKINS_PASSWORD,
            wait_for_completion=True,
            poll_interval_minutes=1,
            max_wait_minutes=5,
        )

        phases: list[str] = []

        async def capture_phase(job_id, phase):
            phases.append(phase)

        with (
            patch(
                "rootcoz.main.wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch("rootcoz.main.update_status", new_callable=AsyncMock),
            patch(
                "rootcoz.main.safe_update_progress",
                side_effect=capture_phase,
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch("rootcoz.main._resolve_enable_jira", return_value=False),
            patch(
                "rootcoz.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
            _patch_preflight(),
        ):
            mock_analyze.return_value = AnalysisResult(
                job_id="test-id",
                status="completed",
                summary="ok",
            )
            await process_analysis_with_id("test-id", body, merged)

        assert "waiting_for_jenkins" in phases
        assert "analyzing" in phases
        assert "saving" in phases
        # waiting_for_jenkins comes before analyzing
        assert phases.index("waiting_for_jenkins") < phases.index("analyzing")

    @pytest.mark.asyncio
    async def test_progress_phases_without_jenkins_wait(self) -> None:
        """When not waiting for Jenkins, phases skip waiting_for_jenkins."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=False,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            wait_for_completion=False,
        )

        phases: list[str] = []

        async def capture_phase(job_id, phase):
            phases.append(phase)

        with (
            patch("rootcoz.main.update_status", new_callable=AsyncMock),
            patch(
                "rootcoz.main.safe_update_progress",
                side_effect=capture_phase,
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch("rootcoz.main._resolve_enable_jira", return_value=False),
            patch(
                "rootcoz.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
            _patch_preflight(),
        ):
            mock_analyze.return_value = AnalysisResult(
                job_id="test-id",
                status="completed",
                summary="ok",
            )
            await process_analysis_with_id("test-id", body, merged)

        assert "waiting_for_jenkins" not in phases
        assert "analyzing" in phases
        assert "saving" in phases

    @pytest.mark.asyncio
    async def test_progress_phases_with_jira_enrichment(self) -> None:
        """When Jira enrichment is enabled, progress includes enriching_jira phase."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=False,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            wait_for_completion=False,
        )

        phases: list[str] = []

        async def capture_phase(job_id, phase):
            phases.append(phase)

        with (
            patch("rootcoz.main.update_status", new_callable=AsyncMock),
            patch(
                "rootcoz.main.safe_update_progress",
                side_effect=capture_phase,
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch("rootcoz.main._resolve_enable_jira", return_value=True),
            patch(
                "rootcoz.main._enrich_result_with_jira",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
            _patch_preflight(),
        ):
            mock_analyze.return_value = AnalysisResult(
                job_id="test-id",
                status="completed",
                summary="ok",
            )
            await process_analysis_with_id("test-id", body, merged)

        assert "enriching_jira" in phases
        assert "saving" in phases
        assert phases.index("enriching_jira") < phases.index("saving")

    @pytest.mark.asyncio
    async def test_progress_phase_exception_does_not_crash_analysis(self) -> None:
        """update_progress_phase raising an exception must not abort the analysis."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            wait_for_completion=False,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            wait_for_completion=False,
        )

        with (
            patch("rootcoz.main.update_status", new_callable=AsyncMock) as mock_status,
            patch(
                "rootcoz.engine.core.update_progress_phase",
                side_effect=RuntimeError("DB connection lost"),
            ),
            patch("rootcoz.main.analyze_job", new_callable=AsyncMock) as mock_analyze,
            patch("rootcoz.main._resolve_enable_jira", return_value=False),
            patch(
                "rootcoz.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "rootcoz.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
            _patch_preflight(),
        ):
            mock_analyze.return_value = AnalysisResult(
                job_id="test-id",
                status="completed",
                summary="ok",
            )
            # Should complete without raising despite update_progress_phase failing
            await process_analysis_with_id("test-id", body, merged)

        # Analysis completed: update_status was called with the completed result
        mock_analyze.assert_called_once()
        status_calls = [c.args[1] for c in mock_status.call_args_list]
        assert "completed" in status_calls


class TestRequestParamsPreservation:
    """Tests for request_params preservation across update_status calls.

    The initial save_result includes request_params (ai_provider, ai_model,
    peer_ai_configs). When analysis completes, update_status must preserve
    request_params in the final result_data.
    """

    @pytest.mark.asyncio
    async def test_process_analysis_preserves_request_params_on_success(
        self, temp_db_path: Path
    ) -> None:
        """request_params saved initially must survive when analysis completes."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=42,
            ai_provider="claude",
            ai_model="opus",
            wait_for_completion=False,
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            wait_for_completion=False,
        )

        job_id = "preserve-params-success"
        initial_request_params = {
            "ai_provider": "claude",
            "ai_model": "opus",
            "peer_ai_configs": [{"ai_provider": "gemini", "ai_model": "flash"}],
            "tests_repo_url": "https://github.com/org/tests",
            "additional_repos": [
                {"name": "infra", "url": "https://github.com/org/infra"}
            ],
        }

        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            # Save initial result with request_params
            await storage.save_result(
                job_id,
                "https://jenkins.example.com/job/my-job/42/",
                "pending",
                {
                    "job_name": "my-job",
                    "build_number": 42,
                    "request_params": initial_request_params,
                },
            )

            with (
                patch(
                    "rootcoz.main.analyze_job",
                    new_callable=AsyncMock,
                ) as mock_analyze,
                patch(
                    "rootcoz.main._resolve_enable_jira",
                    return_value=False,
                ),
                patch(
                    "rootcoz.main.populate_failure_history",
                    new_callable=AsyncMock,
                ),
                patch(
                    "rootcoz.main.storage.make_classifications_visible",
                    new_callable=AsyncMock,
                ),
            ):
                mock_analyze.return_value = AnalysisResult(
                    job_id=job_id,
                    status="completed",
                    summary="1 failure analyzed",
                    ai_provider="claude",
                    ai_model="opus",
                    failures=[
                        FailureAnalysis(
                            test_name="test_foo",
                            error="assert False",
                            analysis=AnalysisDetail(
                                classification="CODE ISSUE",
                                details="Test failed",
                            ),
                        )
                    ],
                )
                await process_analysis_with_id(job_id, body, merged)

            # Verify request_params survived in the stored result
            stored = await storage.get_result(job_id, strip_sensitive=False)
            assert stored is not None
            result = stored["result"]
            assert "request_params" in result, (
                "request_params must be preserved after analysis completes"
            )
            assert result["request_params"]["ai_provider"] == "claude"
            assert result["request_params"]["ai_model"] == "opus"
            assert result["request_params"]["peer_ai_configs"] == [
                {"ai_provider": "gemini", "ai_model": "flash"}
            ]
            assert (
                result["request_params"]["tests_repo_url"]
                == "https://github.com/org/tests"
            )
            assert result["request_params"]["additional_repos"] == [
                {"name": "infra", "url": "https://github.com/org/infra"}
            ]

    @pytest.mark.asyncio
    async def test_process_analysis_preserves_request_params_on_failure(
        self, temp_db_path: Path
    ) -> None:
        """request_params saved initially must survive when analysis fails."""
        from rootcoz.main import process_analysis_with_id
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=42,
            ai_provider="claude",
            ai_model="opus",
            wait_for_completion=False,
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            wait_for_completion=False,
        )

        job_id = "preserve-params-failure"
        initial_request_params = {
            "ai_provider": "claude",
            "ai_model": "opus",
        }

        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id,
                "https://jenkins.example.com/job/my-job/42/",
                "pending",
                {
                    "job_name": "my-job",
                    "build_number": 42,
                    "request_params": initial_request_params,
                },
            )

            with patch(
                "rootcoz.main.analyze_job",
                new_callable=AsyncMock,
                side_effect=RuntimeError("AI CLI crashed"),
            ):
                await process_analysis_with_id(job_id, body, merged)

            stored = await storage.get_result(job_id, strip_sensitive=False)
            assert stored is not None
            result = stored["result"]
            assert "request_params" in result, (
                "request_params must be preserved even when analysis fails"
            )
            assert result["request_params"]["ai_provider"] == "claude"

    def test_analyze_failures_preserves_request_params_on_success(
        self, test_client, temp_db_path: Path
    ) -> None:
        """POST /analyze with type=raw seeds request_params in pending state."""
        with patch("rootcoz.main._process_file_raw_analysis", new_callable=AsyncMock):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "raw",
                    "failures": [
                        {
                            "test_name": "test_foo",
                            "error_message": "assert False",
                            "stack_trace": "File test.py, line 10",
                        }
                    ],
                    "ai_provider": "cursor",
                    "ai_model": "test-model",
                },
            )
            assert response.status_code == 202
            data = response.json()
            job_id = data["job_id"]

        # Fetch the stored result and verify request_params were seeded
        result_response = test_client.get(
            f"/results/{job_id}",
            headers={"accept": "application/json"},
        )
        assert result_response.status_code in (200, 202)
        result_data = result_response.json()
        assert "result" in result_data
        result = result_data["result"]
        assert "request_params" in result, (
            "request_params must be seeded when the job is queued"
        )
        rp = result["request_params"]
        assert rp["ai_provider"] == "cursor"
        assert rp["ai_model"] == "test-model"


class TestReAnalyzeEndpoint:
    """Tests for POST /re-analyze/{job_id}."""

    @staticmethod
    async def _create_origin_job(
        job_id: str,
        jenkins_url: str,
        result_data: dict,
    ) -> None:
        """Save a completed origin job into storage for re-analysis tests."""
        from rootcoz import storage

        await storage.save_result(job_id, jenkins_url, "completed", result_data)

    def test_re_analyze_not_found(self, test_client) -> None:
        """Re-analyze returns 404 when job_id does not exist."""
        response = test_client.post("/re-analyze/nonexistent", json={})
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_re_analyze_no_request_params(self, test_client) -> None:
        """Re-analyze returns 400 when original has no request_params."""
        from rootcoz import storage

        # Save a result WITHOUT request_params
        await storage.save_result(
            "job-no-params",
            "http://jenkins/job/test/1/",
            "completed",
            {"summary": "done", "failures": []},
        )
        response = test_client.post("/re-analyze/job-no-params", json={})
        assert response.status_code == 400
        assert "request_params" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_re_analyze_success(self, test_client) -> None:
        """Re-analyze returns 202 with new job_id when original has request_params."""
        from rootcoz import storage

        # Save a result WITH request_params (mimicking a completed analysis)
        result_data = {
            "summary": "1 failure",
            "job_name": "my-job",
            "build_number": 42,
            "failures": [],
            "request_params": encrypt_sensitive_fields(
                {
                    "job_name": "my-job",
                    "build_number": 42,
                    "ai_provider": "claude",
                    "ai_model": "opus",
                    "jenkins_url": "https://jenkins.example.com",
                    "jenkins_user": "testuser",
                    "jenkins_password": "testpw",  # pragma: allowlist secret
                }
            ),
        }
        await storage.save_result(
            "job-reanalyze-ok",
            "http://jenkins/job/my-job/42/",
            "completed",
            result_data,
        )
        with patch("rootcoz.main.process_analysis_with_id") as mock_process:
            response = test_client.post("/re-analyze/job-reanalyze-ok", json={})
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert "job_id" in data
        assert data["job_id"] != "job-reanalyze-ok"  # New job_id
        assert "result_url" in data
        mock_process.assert_called_once()
        assert data["result_url"].endswith(f"/results/{data['job_id']}")

    @pytest.mark.asyncio
    async def test_re_analyze_stores_reanalyzed_from_job_id(self, test_client) -> None:
        """Re-analyze stores reanalyzed_from_job_id in the new job's request_params."""
        result_data = {
            "summary": "1 failure",
            "job_name": "my-job",
            "display_name": "My Job",
            "build_number": 42,
            "failures": [],
            "request_params": encrypt_sensitive_fields(
                {
                    "job_name": "my-job",
                    "build_number": 42,
                    "ai_provider": "claude",
                    "ai_model": "opus",
                    "jenkins_url": "https://jenkins.example.com",
                    "jenkins_user": "testuser",
                    "jenkins_password": "testpw",  # pragma: allowlist secret
                }
            ),
        }
        await self._create_origin_job(
            "job-origin",
            "http://jenkins/job/my-job/42/",
            result_data,
        )
        with patch("rootcoz.main.process_analysis_with_id"):
            response = test_client.post("/re-analyze/job-origin", json={})
        assert response.status_code == 202
        new_job_id = response.json()["job_id"]
        # Verify the new job has reanalyzed_from_job_id stored
        stored = await storage.get_result(new_job_id)
        assert stored is not None
        params = stored["result"]["request_params"]
        assert params["reanalyzed_from_job_id"] == "job-origin"
        assert params["reanalyzed_from_job_name"] == "My Job"

    @pytest.mark.asyncio
    async def test_re_analyze_file_stores_reanalyzed_metadata(
        self, test_client
    ) -> None:
        """Re-analyze a file/raw job stores reanalyzed_from_job_id and _job_name."""
        result_data = {
            "summary": "file failure",
            "job_name": "file-job",
            "display_name": "File Job",
            "request_params": encrypt_sensitive_fields(
                {
                    "analysis_type": "file",
                    "raw_xml": "<testsuite><testcase name='t1'><failure>err</failure></testcase></testsuite>",
                    "ai_provider": "claude",
                    "ai_model": "opus",
                }
            ),
        }
        await self._create_origin_job(
            "file-origin",
            "",
            result_data,
        )
        with patch("rootcoz.main._process_file_raw_analysis"):
            response = test_client.post("/re-analyze/file-origin", json={})
        assert response.status_code == 202
        new_job_id = response.json()["job_id"]
        stored = await storage.get_result(new_job_id)
        assert stored is not None
        params = stored["result"]["request_params"]
        assert params["reanalyzed_from_job_id"] == "file-origin"
        assert params["reanalyzed_from_job_name"] == "File Job"

    @pytest.mark.asyncio
    async def test_results_endpoint_returns_origin_info(self, test_client) -> None:
        """GET /results/{job_id} includes origin job info for re-analyzed jobs."""
        # Create original job
        await storage.save_result(
            "origin-job",
            "http://jenkins/job/orig/1/",
            "completed",
            {"summary": "original", "display_name": "Original Job", "job_name": "orig"},
        )
        # Create re-analyzed job with reanalyzed_from_job_id
        await storage.save_result(
            "reanalyzed-job",
            "",
            "completed",
            {
                "summary": "re-done",
                "request_params": {"reanalyzed_from_job_id": "origin-job"},
            },
        )
        response = test_client.get("/results/reanalyzed-job")
        assert response.status_code == 200
        data = response.json()
        assert data["reanalyzed_from_job_id"] == "origin-job"
        assert data["origin_job_name"] == "Original Job"

    @pytest.mark.asyncio
    async def test_results_endpoint_returns_denormalized_origin_name(
        self, test_client
    ) -> None:
        """GET /results/{job_id} uses denormalized name without DB lookup."""
        # No origin job stored – the denormalized name should be used directly
        await storage.save_result(
            "reanalyzed-job-fast",
            "",
            "completed",
            {
                "summary": "re-done",
                "request_params": {
                    "reanalyzed_from_job_id": "missing-origin",
                    "reanalyzed_from_job_name": "Denormalized Name",
                },
            },
        )
        response = test_client.get("/results/reanalyzed-job-fast")
        assert response.status_code == 200
        data = response.json()
        assert data["reanalyzed_from_job_id"] == "missing-origin"
        assert data["origin_job_name"] == "Denormalized Name"

    @pytest.mark.asyncio
    async def test_results_endpoint_no_origin_for_normal_jobs(
        self, test_client
    ) -> None:
        """GET /results/{job_id} does not include origin fields for normal jobs."""
        await storage.save_result(
            "normal-job",
            "",
            "completed",
            {"summary": "normal", "request_params": {}},
        )
        response = test_client.get("/results/normal-job")
        assert response.status_code == 200
        data = response.json()
        assert "reanalyzed_from_job_id" not in data
        assert "origin_job_name" not in data


class TestGetFailureByUUID:
    """Tests for GET /api/failures/{failure_uuid}."""

    @pytest.mark.asyncio
    async def test_get_failure_by_uuid_found(self, test_client) -> None:
        """Returns the failure and parent job_id when found."""
        from rootcoz import storage

        fa = FailureAnalysis(
            test_name="tests.TestFoo.test_bar",
            error="AssertionError",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
        )
        result_data = {
            "summary": "1 failure",
            "failures": [fa.model_dump(mode="json")],
        }
        await storage.save_result("job-uuid-1", "", "completed", result_data)

        response = test_client.get(f"/api/failures/{fa.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-uuid-1"
        assert data["failure"]["test_name"] == "tests.TestFoo.test_bar"
        assert data["failure"]["id"] == fa.id

    def test_get_failure_by_uuid_not_found(self, test_client) -> None:
        """Returns 404 for unknown UUID."""
        response = test_client.get("/api/failures/nonexistent-uuid")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_failure_by_uuid_in_child_job(self, test_client) -> None:
        """Finds failure inside a child job analysis."""
        from rootcoz import storage
        from rootcoz.models import ChildJobAnalysis

        fa = FailureAnalysis(
            test_name="tests.TestChild.test_nested",
            error="RuntimeError",
            analysis=AnalysisDetail(classification="PRODUCT BUG"),
        )
        cja = ChildJobAnalysis(
            job_name="child-runner",
            build_number=5,
            failures=[fa],
        )
        result_data = {
            "summary": "nested failure",
            "failures": [],
            "child_job_analyses": [cja.model_dump(mode="json")],
        }
        await storage.save_result("job-uuid-child", "", "completed", result_data)

        response = test_client.get(f"/api/failures/{fa.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-uuid-child"
        assert data["child_job_name"] == "child-runner"
        assert data["child_build_number"] == 5


class TestReAnalyzeFailure:
    """Tests for POST /api/failures/{failure_uuid}/re-analyze."""

    def test_re_analyze_failure_not_found(self, test_client) -> None:
        """Returns 404 for unknown failure UUID."""
        response = test_client.post("/api/failures/nonexistent-uuid/re-analyze")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_re_analyze_failure_success(self, test_client) -> None:
        """Patches the failure in-place in the parent job."""
        from rootcoz import storage

        fa = FailureAnalysis(
            test_name="tests.TestReAnalyze.test_one",
            error="ValueError",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
        )
        fa_dict = fa.model_dump(mode="json")
        # Add extra fields that should survive the in-place patch
        fa_dict["stack_trace"] = "Traceback (most recent call last):\n  ValueError"
        fa_dict["duration"] = 12.5
        fa_dict["status"] = "FAILED"
        result_data = {
            "summary": "1 failure",
            "failures": [fa_dict],
            "request_params": encrypt_sensitive_fields(
                {
                    "ai_provider": "claude",
                    "ai_model": "opus",
                    "submitted_by": "admin",
                }
            ),
        }
        result_data["request_params"]["submitted_by"] = "admin"
        await storage.save_result("job-reanalyze-f", "", "completed", result_data)

        # Mock analyze_failure_group to return a new analysis
        new_analysis = FailureAnalysis(
            test_name="tests.TestReAnalyze.test_one",
            error="ValueError",
            analysis=AnalysisDetail(classification="PRODUCT ISSUE"),
        )
        with (
            patch(
                "rootcoz.main.analyze_failure_group",
                new_callable=AsyncMock,
                return_value=[new_analysis],
            ),
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "rootcoz.main.RepositoryManager",
            ),
        ):
            response = test_client.post(f"/api/failures/{fa.id}/re-analyze")
            # Wait for background task to complete
            import asyncio

            for _ in range(50):
                await asyncio.sleep(0.1)
                _stored = await storage.get_result("job-reanalyze-f")
                if _stored:
                    _failures = _stored.get("result", {}).get("failures", [])
                    if _failures and _failures[0].get("reanalysis_status") is not None:
                        break
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["job_id"] == "job-reanalyze-f"
        assert data["failure_uuid"] == fa.id

        # Verify the failure was patched in-place
        stored = await storage.get_result("job-reanalyze-f")
        result = stored["result"]
        failure = result["failures"][0]
        assert failure["analysis"]["classification"] == "PRODUCT ISSUE"
        assert "previous_analyses" in failure
        assert len(failure["previous_analyses"]) == 1
        assert (
            failure["previous_analyses"][0]["analysis"]["classification"]
            == "CODE ISSUE"
        )
        assert "reanalysis_status" not in failure
        assert failure["reanalyzed_with"] == {
            "ai_provider": "claude",
            "ai_model": "opus",
        }
        # Verify metadata fields survived the in-place patch
        assert (
            failure["stack_trace"] == "Traceback (most recent call last):\n  ValueError"
        )
        assert failure["duration"] == 12.5
        assert failure["status"] == "FAILED"


class TestBuildEffectiveJiraSettings:
    """Tests for _build_effective_jira_settings helper."""

    def test_no_user_token_returns_original(self):
        """When no user token, original settings returned unchanged."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings()
        result = _build_effective_jira_settings(settings, "", "")
        assert result is settings

    def test_user_token_clears_server_pat(self):
        """User token clears server PAT so it takes precedence."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_url="https://jira.example.com",
            jira_pat=SecretStr("server-pat"),
            jira_api_token=SecretStr("server-api-token"),
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(settings, "user-token", "")
        assert result.jira_pat is None
        assert result.jira_api_token.get_secret_value() == "user-token"

    def test_user_token_without_email_clears_server_email(self):
        """User token without email clears server email to avoid mismatched Cloud auth."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_url="https://jira.example.com",
            jira_email="server@example.com",
            jira_api_token=SecretStr("server-api-token"),
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(settings, "user-token", "")
        assert result.jira_email is None
        assert result.jira_api_token.get_secret_value() == "user-token"

    def test_user_token_with_email_sets_both(self):
        """User token with email sets both for Cloud auth."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_url="https://jira.example.com",
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "user-token", "user@example.com"
        )
        assert result.jira_api_token.get_secret_value() == "user-token"
        assert result.jira_email == "user@example.com"
        assert result.jira_pat is None

    def test_original_settings_not_mutated(self):
        """model_copy must not mutate the original Settings instance."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_url="https://jira.example.com",
            jira_pat=SecretStr("server-pat"),
            jira_email="server@example.com",
            jira_project_key="TEST",
        )
        _build_effective_jira_settings(settings, "user-token", "user@example.com")
        # Original must be untouched
        assert settings.jira_pat.get_secret_value() == "server-pat"
        assert settings.jira_email == "server@example.com"


class TestValidateToken:
    """Tests for POST /api/validate-token."""

    @pytest.mark.asyncio
    async def test_github_valid_token(self, test_client):
        with patch("rootcoz.main.httpx.AsyncClient") as mock_client_class:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"login": "testuser"}
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_class.return_value = mock_client
            response = test_client.post(
                "/api/validate-token",
                json={"token_type": "github", "token": "ghp_valid"},
                headers=_ADMIN_AUTH_HEADERS,
            )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_github_invalid_token(self, test_client):
        import httpx

        with patch("rootcoz.main.httpx.AsyncClient") as mock_client_class:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Unauthorized", request=MagicMock(), response=mock_resp
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_class.return_value = mock_client
            response = test_client.post(
                "/api/validate-token",
                json={"token_type": "github", "token": "ghp_invalid"},
                headers=_ADMIN_AUTH_HEADERS,
            )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert "Invalid token" in data["message"]

    @pytest.mark.asyncio
    async def test_unknown_token_type(self, test_client):
        response = test_client.post(
            "/api/validate-token",
            json={"token_type": "bitbucket", "token": "some-token"},
            headers=_ADMIN_AUTH_HEADERS,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_jira_no_url_configured(self, test_client):
        response = test_client.post(
            "/api/validate-token",
            json={"token_type": "jira", "token": "jira-token"},
            headers=_ADMIN_AUTH_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert "not configured" in data["message"]


class TestJiraProjectsEndpoint:
    """Tests for POST /api/jira-projects."""

    def test_no_jira_url_returns_empty(self, test_client):
        """No JIRA_URL configured returns empty list."""
        response = test_client.post(
            "/api/jira-projects", json={}, headers=_ADMIN_AUTH_HEADERS
        )
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_returns_projects(self, test_client):
        """Returns project list from JiraClient.list_projects."""
        from rootcoz.main import app, get_settings

        projects = [{"key": "PROJ", "name": "My Project"}]
        jira_settings = _build_wait_settings(jira_url="https://jira.example.com")
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            with patch("rootcoz.main.JiraClient") as MockJiraClient:
                mock_client = AsyncMock()
                mock_client.list_projects = AsyncMock(return_value=projects)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                MockJiraClient.return_value = mock_client
                response = test_client.post(
                    "/api/jira-projects",
                    json={"jira_token": "tok", "jira_email": "u@e.com"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            data = response.json()
            assert any(p["key"] == "PROJ" for p in data)
        finally:
            app.dependency_overrides.pop(get_settings, None)


class TestJiraSecurityLevelsEndpoint:
    """Tests for POST /api/jira-security-levels."""

    def test_no_jira_url_returns_empty(self, test_client):
        """No JIRA_URL configured returns empty list."""
        response = test_client.post(
            "/api/jira-security-levels",
            json={"project_key": "PROJ"},
            headers=_ADMIN_AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_no_token_returns_empty(self, test_client):
        """No jira_token returns empty list."""
        from rootcoz.main import app, get_settings

        jira_settings = _build_wait_settings(jira_url="https://jira.example.com")
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            response = test_client.post(
                "/api/jira-security-levels",
                json={"project_key": "PROJ"},
                headers=_ADMIN_AUTH_HEADERS,
            )
            assert response.status_code == 200
            assert response.json() == []
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_returns_security_levels(self, test_client):
        """Returns security levels from JiraClient.list_security_levels."""
        from rootcoz.main import app, get_settings

        levels = [{"id": "10", "name": "Internal", "description": "Internal only"}]
        jira_settings = _build_wait_settings(jira_url="https://jira.example.com")
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            with patch("rootcoz.main.JiraClient") as MockJiraClient:
                mock_client = AsyncMock()
                mock_client.list_security_levels = AsyncMock(return_value=levels)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                MockJiraClient.return_value = mock_client
                response = test_client.post(
                    "/api/jira-security-levels",
                    json={
                        "project_key": "PROJ",
                        "jira_token": "tok",
                        "jira_email": "u@e.com",
                    },
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["name"] == "Internal"
        finally:
            app.dependency_overrides.pop(get_settings, None)


class TestMergeSettingsForce:
    """Tests for force -> force_analysis mapping in _merge_settings."""

    def test_force_true_in_request_sets_force_analysis(self) -> None:
        """When request.force=True is explicitly set, merged settings have force_analysis=True."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            force=True,
        )
        settings = Settings()
        merged = _merge_settings(body, settings)
        assert merged.force_analysis is True

    def test_force_false_in_request_sets_force_analysis_false(self) -> None:
        """When request.force=False is explicitly set, merged settings have force_analysis=False."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            force=False,
        )
        # Start with settings that have force_analysis=True
        settings_data = Settings().model_dump(mode="python")
        settings_data["force_analysis"] = True
        settings = Settings.model_validate(settings_data)
        merged = _merge_settings(body, settings)
        assert merged.force_analysis is False

    def test_force_omitted_preserves_settings_default(self) -> None:
        """When force is omitted from request, settings.force_analysis is preserved."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
        )
        settings_data = Settings().model_dump(mode="python")
        settings_data["force_analysis"] = True
        settings = Settings.model_validate(settings_data)
        merged = _merge_settings(body, settings)
        assert merged.force_analysis is True

    def test_force_omitted_default_false(self) -> None:
        """When force is omitted and settings default, force_analysis is False."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
        )
        settings = Settings()
        merged = _merge_settings(body, settings)
        assert merged.force_analysis is False


@pytest.mark.asyncio
class TestGetIssuePromptStoredPriority:
    """Tests for GET /results/{job_id}/issue-prompt stored prompt priority."""

    async def test_returns_stored_issue_prompt_from_request_params(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Stored issue_prompt in request_params is returned directly."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-prompt-test",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="completed",
                result={
                    "job_name": "test-job",
                    "build_number": 1,
                    "request_params": {
                        "issue_prompt": "My custom issue prompt",
                        "tests_repo_url": "https://github.com/org/repo",
                    },
                },
            )

            with patch("rootcoz.main.httpx.AsyncClient") as mock_http:
                resp = test_client.get("/results/job-prompt-test/issue-prompt")
                assert resp.status_code == 200
                assert resp.json()["prompt"] == "My custom issue prompt"
                mock_http.assert_not_called()  # GitHub API should not be called

    async def test_falls_through_when_issue_prompt_empty(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Empty issue_prompt falls through to repo fetch."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-empty-prompt",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="completed",
                result={
                    "job_name": "test-job",
                    "build_number": 1,
                    "request_params": {
                        "issue_prompt": "",
                        "tests_repo_url": "",
                    },
                },
            )

            resp = test_client.get("/results/job-empty-prompt/issue-prompt")
            assert resp.status_code == 200
            # Falls through — no repo configured either, returns empty
            assert resp.json()["prompt"] == ""

    async def test_falls_through_when_issue_prompt_whitespace(
        self, test_client, temp_db_path: Path
    ) -> None:
        """Whitespace-only issue_prompt is stripped and falls through."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-ws-prompt",
                jenkins_url="https://jenkins.example.com/job/test/1/",
                status="completed",
                result={
                    "job_name": "test-job",
                    "build_number": 1,
                    "request_params": {
                        "issue_prompt": "   \n  ",
                        "tests_repo_url": "",
                    },
                },
            )

            resp = test_client.get("/results/job-ws-prompt/issue-prompt")
            assert resp.status_code == 200
            assert resp.json()["prompt"] == ""


class TestBuildReportContextChildScope:
    """Tests for _build_report_context child job scoping."""

    def test_parent_jenkins_url_used_without_child(self):
        """Without child params, parent jenkins_url is returned."""
        from rootcoz.main import _build_report_context

        result_data = {
            "jenkins_url": "http://jenkins/parent/1/",
            "child_job_analyses": [
                {
                    "job_name": "child-A",
                    "build_number": 10,
                    "jenkins_url": "http://jenkins/child-A/10/",
                    "failures": [],
                },
            ],
        }
        _, jenkins_url = _build_report_context(
            include_links=False,
            base_url="",
            job_id="job-1",
            result_data=result_data,
        )
        assert jenkins_url == "http://jenkins/parent/1/"

    def test_child_jenkins_url_used_when_child_specified(self):
        """With child params, child's jenkins_url is returned."""
        from rootcoz.main import _build_report_context

        result_data = {
            "jenkins_url": "http://jenkins/parent/1/",
            "child_job_analyses": [
                {
                    "job_name": "child-A",
                    "build_number": 10,
                    "jenkins_url": "http://jenkins/child-A/10/",
                    "failures": [],
                },
                {
                    "job_name": "child-B",
                    "build_number": 20,
                    "jenkins_url": "http://jenkins/child-B/20/",
                    "failures": [],
                },
            ],
        }
        _, jenkins_url = _build_report_context(
            include_links=False,
            base_url="",
            job_id="job-1",
            result_data=result_data,
            child_job_name="child-A",
            child_build_number=10,
        )
        assert jenkins_url == "http://jenkins/child-A/10/"

    def test_child_fallback_uses_child_job_name(self):
        """When child has no jenkins_url, fallback uses child's job_name."""
        from rootcoz.main import _build_report_context

        result_data = {
            "jenkins_url": "",
            "job_name": "parent-pipeline",
            "build_number": 1,
            "child_job_analyses": [
                {
                    "job_name": "child-A",
                    "build_number": 10,
                    "jenkins_url": None,
                    "failures": [],
                },
            ],
        }
        _, jenkins_url = _build_report_context(
            include_links=False,
            base_url="",
            job_id="job-1",
            result_data=result_data,
            child_job_name="child-A",
            child_build_number=10,
        )
        # Should use child's job_name, not parent's
        assert jenkins_url == "child-A #10"

    def test_child_not_found_falls_back_to_parent(self):
        """When child is not found, falls back to parent jenkins_url."""
        from rootcoz.main import _build_report_context

        result_data = {
            "jenkins_url": "http://jenkins/parent/1/",
            "child_job_analyses": [],
        }
        _, jenkins_url = _build_report_context(
            include_links=False,
            base_url="",
            job_id="job-1",
            result_data=result_data,
            child_job_name="nonexistent",
            child_build_number=10,
        )
        assert jenkins_url == "http://jenkins/parent/1/"

    def test_child_build_number_zero_matches_by_name(self):
        """child_build_number=0 matches by job name only."""
        from rootcoz.main import _build_report_context

        result_data = {
            "jenkins_url": "http://jenkins/parent/1/",
            "child_job_analyses": [
                {
                    "job_name": "child-A",
                    "build_number": 99,
                    "jenkins_url": "http://jenkins/child-A/99/",
                    "failures": [],
                },
            ],
        }
        _, jenkins_url = _build_report_context(
            include_links=False,
            base_url="",
            job_id="job-1",
            result_data=result_data,
            child_job_name="child-A",
            child_build_number=0,
        )
        assert jenkins_url == "http://jenkins/child-A/99/"

    def test_nested_child_job_found(self):
        """Child jobs nested in failed_children are found."""
        from rootcoz.main import _build_report_context

        result_data = {
            "jenkins_url": "http://jenkins/parent/1/",
            "child_job_analyses": [
                {
                    "job_name": "intermediate",
                    "build_number": 5,
                    "jenkins_url": "http://jenkins/intermediate/5/",
                    "failures": [],
                    "failed_children": [
                        {
                            "job_name": "leaf-child",
                            "build_number": 42,
                            "jenkins_url": "http://jenkins/leaf-child/42/",
                            "failures": [],
                        },
                    ],
                },
            ],
        }
        _, jenkins_url = _build_report_context(
            include_links=False,
            base_url="",
            job_id="job-1",
            result_data=result_data,
            child_job_name="leaf-child",
            child_build_number=42,
        )
        assert jenkins_url == "http://jenkins/leaf-child/42/"


class TestStaticAssetHeaders:
    """Tests for GZip compression and cache headers on static assets."""

    def test_gzip_middleware_registered(self, test_client):
        """Verify GZipMiddleware compresses responses above minimum size."""
        response = test_client.get(
            "/api/health",
            headers={"Accept-Encoding": "gzip"},
        )
        # Health endpoint returns small JSON, may not be compressed
        # Just verify the middleware doesn't break anything
        assert response.status_code == 200

    def test_assets_404_no_immutable_cache(self, test_client):
        """Verify /assets/ 404 responses don't get long-lived cache headers."""
        response = test_client.get("/assets/nonexistent-hash123.js")
        assert response.status_code == 404
        cache_control = response.headers.get("cache-control", "")
        assert "immutable" not in cache_control

    def test_non_assets_no_immutable_cache(self, test_client):
        """Verify non-asset paths don't get immutable cache headers."""
        response = test_client.get("/api/health")
        cache_control = response.headers.get("cache-control", "")
        assert "immutable" not in cache_control


class TestAdminSettingsEndpoints:
    """Tests for /api/admin/settings endpoints."""

    _NO_ADMIN_HEADERS = {"Authorization": ""}

    def test_get_settings_returns_metadata(self, test_client) -> None:
        """GET /api/admin/settings returns all settings with metadata."""
        response = test_client.get("/api/admin/settings")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0
        # Check structure of first item
        item = data[0]
        assert "key" in item
        assert "env_var" in item
        assert "value" in item
        assert "source" in item
        assert "category" in item
        assert "type" in item
        assert "sensitive" in item
        assert "description" in item
        # Verify sensitive values are masked
        sensitive_items = [i for i in data if i["sensitive"] and i["value"]]
        for si in sensitive_items:
            assert si["value"] == "••••••••", f"Sensitive field {si['key']} not masked"

    def test_get_settings_non_admin_forbidden(self, test_client) -> None:
        """Non-admin users cannot access settings."""
        response = test_client.get(
            "/api/admin/settings",
            headers=self._NO_ADMIN_HEADERS,
        )
        assert response.status_code in (401, 403)

    def test_put_settings_updates_value(self, test_client) -> None:
        """PUT /api/admin/settings stores and applies settings."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "20"}},
        )
        assert response.status_code == 200
        assert "ai_call_timeout" in response.json()["updated"]

        # Verify the setting is now returned with source=db
        get_resp = test_client.get("/api/admin/settings")
        settings = get_resp.json()
        ai_timeout = next(s for s in settings if s["key"] == "ai_call_timeout")
        assert ai_timeout["source"] == "db"
        assert ai_timeout["value"] == "20"

    def test_put_settings_invalid_key(self, test_client) -> None:
        """PUT with unknown key returns 400."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"nonexistent_key": "value"}},
        )
        assert response.status_code == 400
        assert "Unknown settings" in response.json()["detail"]

    def test_put_settings_empty_body(self, test_client) -> None:
        """PUT with no settings returns 400."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {}},
        )
        assert response.status_code == 400

    def test_delete_setting_resets(self, test_client) -> None:
        """DELETE removes DB override."""
        # First set a value
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "30"}},
        )
        # Then reset
        response = test_client.delete("/api/admin/settings/ai_call_timeout")
        assert response.status_code == 200
        assert response.json()["reset"] == "ai_call_timeout"

        # Verify it's no longer source=db
        get_resp = test_client.get("/api/admin/settings")
        settings = get_resp.json()
        ai_timeout = next(s for s in settings if s["key"] == "ai_call_timeout")
        assert ai_timeout["source"] != "db"

    def test_delete_unknown_key(self, test_client) -> None:
        """DELETE with unknown key returns 404."""
        response = test_client.delete("/api/admin/settings/nonexistent_key")
        assert response.status_code == 404

    def test_delete_no_override(self, test_client) -> None:
        """DELETE when no DB override exists returns 404."""
        response = test_client.delete("/api/admin/settings/jenkins_url")
        assert response.status_code == 404

    def test_put_settings_non_admin_forbidden(self, test_client) -> None:
        """Non-admin users cannot update settings."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "20"}},
            headers=self._NO_ADMIN_HEADERS,
        )
        assert response.status_code in (401, 403)
