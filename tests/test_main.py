"""Tests for FastAPI main application."""

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import jenkins
import pytest

from jenkins_job_insight import storage
from pydantic import SecretStr

from jenkins_job_insight.config import Settings
from jenkins_job_insight.encryption import encrypt_sensitive_fields
from jenkins_job_insight.models import (
    AiConfigEntry,
    AnalysisDetail,
    AnalysisResult,
    FailureAnalysis,
)

# Fake credentials for tests — annotated once to suppress Ruff S105/S106 globally.
FAKE_JENKINS_PASSWORD = "not-a-real-password"  # noqa: S105  # pragma: allowlist secret
FAKE_GITHUB_TOKEN = "not-a-real-token"  # noqa: S105  # pragma: allowlist secret


@contextmanager
def _with_github_issue_config():
    """Temporarily enable GitHub issue creation settings for tests."""
    from jenkins_job_insight.config import get_settings

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

    from jenkins_job_insight.config import get_settings

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


@pytest.fixture
def mock_settings(temp_db_path: Path):
    """Mock settings for tests."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "GEMINI_API_KEY": "test-key",  # pragma: allowlist secret
        "DB_PATH": str(temp_db_path),
    }
    with patch.dict(os.environ, env, clear=True):
        # Clear the lru_cache to use fresh settings
        from jenkins_job_insight.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def test_client(mock_settings, temp_db_path: Path):
    """Create a test client with mocked dependencies."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient
        from jenkins_job_insight.main import app

        with TestClient(app) as client:
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
        with patch("jenkins_job_insight.main.process_analysis_with_id"):
            response = test_client.post(
                "/analyze",
                json={
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
                "job_name": "test",
                "build_number": "not-a-number",
                "tests_repo_url": "https://github.com/example/repo",
            },
        )
        assert response.status_code == 422

    def test_analyze_accepts_tests_repo_url_with_ref(self, test_client) -> None:
        """Test that tests_repo_url with ':ref' suffix is accepted (no URL validation)."""
        response = test_client.post(
            "/analyze",
            json={
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
                "job_name": "test",
            },
        )
        assert response.status_code == 422

    def test_analyze_missing_ai_provider_returns_400(self, test_client) -> None:
        """Test that missing AI provider returns 400 before queuing."""
        response = test_client.post(
            "/analyze",
            json={
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
        with patch("jenkins_job_insight.main.process_analysis_with_id"):
            response = test_client.post(
                "/analyze",
                json={
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
            "job_name": "test",
            "build_number": 1,
            "ai_provider": "claude",
            "ai_model": "test-model",
        }

    def test_base_url_from_public_base_url(self, mock_settings, temp_db_path) -> None:
        """PUBLIC_BASE_URL takes precedence over any request header."""
        from jenkins_job_insight.config import get_settings

        os.environ["PUBLIC_BASE_URL"] = "https://myapp.example.com"
        get_settings.cache_clear()
        try:
            with patch.object(storage, "DB_PATH", temp_db_path):
                from starlette.testclient import TestClient
                from jenkins_job_insight.main import app

                with (
                    TestClient(app) as client,
                    patch("jenkins_job_insight.main.process_analysis_with_id"),
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
        from jenkins_job_insight.config import get_settings

        os.environ["PUBLIC_BASE_URL"] = "https://myapp.example.com:8443/"
        get_settings.cache_clear()
        try:
            with patch.object(storage, "DB_PATH", temp_db_path):
                from starlette.testclient import TestClient
                from jenkins_job_insight.main import app

                with (
                    TestClient(app) as client,
                    patch("jenkins_job_insight.main.process_analysis_with_id"),
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
        with patch("jenkins_job_insight.main.process_analysis_with_id"):
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
        with patch("jenkins_job_insight.main.process_analysis_with_id"):
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


class TestAnalyzeFailuresEndpoint:
    """Tests for the POST /analyze-failures endpoint."""

    def test_analyze_failures_success(self, test_client) -> None:
        """Test that valid failures return 200 with correct structure."""
        mock_analysis = FailureAnalysis(
            test_name="test_foo",
            error="assert False",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="Test assertion failed",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with patch(
                "jenkins_job_insight.main.analyze_failure_group",
                new_callable=AsyncMock,
            ) as mock_analyze_group:
                mock_analyze_group.return_value = [mock_analysis]

                with patch(
                    "jenkins_job_insight.main.run_parallel_with_limit",
                    new_callable=AsyncMock,
                ) as mock_parallel:

                    async def run_coroutines(coroutines, **kwargs):
                        return [await coro for coro in coroutines]

                    mock_parallel.side_effect = run_coroutines

                    response = test_client.post(
                        "/analyze-failures",
                        json={
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
                    assert response.status_code == 200
                    data = response.json()
                    assert data["status"] == "completed"
                    assert data["ai_provider"] == "claude"
                    assert data["ai_model"] == "test-model"
                    assert "job_id" in data
                    assert len(data["failures"]) == 1
                    assert data["failures"][0]["test_name"] == "test_foo"
                    assert data["base_url"] == ""
                    assert data["result_url"].startswith("/results/")

    def test_analyze_failures_empty_failures(self, test_client) -> None:
        """Test that empty failures list returns 422 (validator rejects empty list without raw_xml)."""
        response = test_client.post(
            "/analyze-failures",
            json={
                "failures": [],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    def test_analyze_failures_missing_ai_provider(self, test_client) -> None:
        """Test that missing AI provider (no env var, no body param) returns 400."""
        with (
            patch("jenkins_job_insight.main.AI_PROVIDER", ""),
            patch("jenkins_job_insight.main.AI_MODEL", ""),
        ):
            response = test_client.post(
                "/analyze-failures",
                json={
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
            patch("jenkins_job_insight.main.AI_PROVIDER", ""),
            patch("jenkins_job_insight.main.AI_MODEL", ""),
        ):
            response = test_client.post(
                "/analyze-failures",
                json={
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
        """Test that when analyze_failure_group raises, endpoint returns status 'failed'."""
        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with patch(
                "jenkins_job_insight.main.get_failure_signature",
                return_value="sig-a",
            ):
                with patch(
                    "jenkins_job_insight.main.run_parallel_with_limit",
                    new_callable=AsyncMock,
                ) as mock_parallel:

                    async def raise_after_closing(coroutines, **kwargs):
                        for coro in coroutines:
                            coro.close()
                        raise RuntimeError("AI CLI crashed")

                    mock_parallel.side_effect = raise_after_closing

                    response = test_client.post(
                        "/analyze-failures",
                        json={
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
                    assert response.status_code == 200
                    data = response.json()
                    assert data["status"] == "failed"
                    assert "AI CLI crashed" in data["summary"]

    def test_analyze_failures_partial_failure(self, test_client) -> None:
        """Test that when some failure groups succeed and others raise, the endpoint returns partial results.

        Posts 2 failures with different signatures. run_parallel_with_limit returns
        one successful analysis list and one RuntimeError exception. Verifies the
        endpoint returns status 'completed' with only the successful analysis and
        the summary reflects the correct counts.
        """
        mock_analysis = FailureAnalysis(
            test_name="test_a",
            error="err",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="analysis",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with patch(
                "jenkins_job_insight.main.get_failure_signature",
                side_effect=["sig-a", "sig-b"],
            ):
                with patch(
                    "jenkins_job_insight.main.run_parallel_with_limit",
                    new_callable=AsyncMock,
                ) as mock_parallel:

                    async def run_partial_failure(coroutines, **kwargs):
                        for coro in coroutines:
                            coro.close()
                        return [
                            [mock_analysis],
                            RuntimeError("AI CLI crashed"),
                        ]

                    mock_parallel.side_effect = run_partial_failure

                    with patch(
                        "jenkins_job_insight.main.save_result",
                        new_callable=AsyncMock,
                    ):
                        with patch(
                            "jenkins_job_insight.main.update_status",
                            new_callable=AsyncMock,
                        ):
                            response = test_client.post(
                                "/analyze-failures",
                                json={
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
                            assert response.status_code == 200
                            data = response.json()
                            assert data["status"] == "completed"
                            assert len(data["failures"]) == 1
                            assert data["failures"][0]["test_name"] == "test_a"
                            assert "2 test failures" in data["summary"]
                            assert "2 unique errors" in data["summary"]
                            assert "1 analyzed successfully" in data["summary"]

    def test_analyze_failures_deduplication(self, test_client) -> None:
        """Test that failures sharing the same signature are deduplicated.

        Three failures are submitted where two share a signature. Verify
        analyze_failure_group is called twice (once per unique signature),
        not three times.
        """
        mock_analysis_a = FailureAnalysis(
            test_name="test_foo",
            error="assert False",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="assertion failure",
            ),
        )
        mock_analysis_b = FailureAnalysis(
            test_name="test_bar",
            error="KeyError: x",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="missing key",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            # Return same signature for first two failures, different for third
            signatures = iter(["sig-a", "sig-a", "sig-b"])
            with patch(
                "jenkins_job_insight.main.get_failure_signature",
                side_effect=lambda f: next(signatures),
            ):
                with patch(
                    "jenkins_job_insight.main.analyze_failure_group",
                    new_callable=AsyncMock,
                ) as mock_analyze_group:
                    mock_analyze_group.side_effect = [
                        [mock_analysis_a, mock_analysis_a],
                        [mock_analysis_b],
                    ]

                    with patch(
                        "jenkins_job_insight.main.run_parallel_with_limit",
                        new_callable=AsyncMock,
                    ) as mock_parallel:
                        # Simulate run_parallel_with_limit calling the coroutines
                        async def run_coroutines(coroutines, **kwargs):
                            results = []
                            for coro in coroutines:
                                results.append(await coro)
                            return results

                        mock_parallel.side_effect = run_coroutines

                        response = test_client.post(
                            "/analyze-failures",
                            json={
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
                        assert response.status_code == 200
                        data = response.json()
                        assert data["status"] == "completed"
                        # analyze_failure_group called twice: once for sig-a group, once for sig-b
                        assert mock_analyze_group.call_count == 2


class TestAnalyzeFailuresRawXml:
    """Tests for the POST /analyze-failures endpoint with raw_xml input."""

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
        """Test that raw_xml with failures returns enriched XML."""
        mock_analysis = FailureAnalysis(
            test_name="tests.test_auth.test_login",
            error="assert False",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="Test assertion failed",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with patch(
                "jenkins_job_insight.main.run_parallel_with_limit",
                new_callable=AsyncMock,
            ) as mock_parallel:

                async def return_mock_results(coroutines, **kwargs):
                    for coro in coroutines:
                        coro.close()
                    return [[mock_analysis]]

                mock_parallel.side_effect = return_mock_results

                response = test_client.post(
                    "/analyze-failures",
                    json={
                        "raw_xml": self.SAMPLE_XML,
                        "ai_provider": "claude",
                        "ai_model": "test-model",
                    },
                )
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "completed"
                assert data["enriched_xml"] is not None
                assert "<?xml" in data["enriched_xml"]
                assert len(data["failures"]) == 1

    def test_raw_xml_no_failures(self, test_client) -> None:
        """Test that raw_xml with no failures returns the original XML."""
        response = test_client.post(
            "/analyze-failures",
            json={
                "raw_xml": self.SAMPLE_XML_NO_FAILURES,
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert "No test failures" in data["summary"]
        assert data["enriched_xml"] is not None

    def test_raw_xml_invalid_xml(self, test_client) -> None:
        """Test that invalid XML returns 400."""
        response = test_client.post(
            "/analyze-failures",
            json={
                "raw_xml": "this is not valid xml <<<<",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 400
        assert "Invalid XML" in response.json()["detail"]

    def test_raw_xml_and_failures_mutual_exclusion(self, test_client) -> None:
        """Test that providing both raw_xml and failures returns 422."""
        response = test_client.post(
            "/analyze-failures",
            json={
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
        """Test that providing neither raw_xml nor failures returns 422."""
        response = test_client.post(
            "/analyze-failures",
            json={
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    def test_failures_mode_still_works(self, test_client) -> None:
        """Test that existing failures mode is backwards compatible."""
        mock_analysis = FailureAnalysis(
            test_name="test_foo",
            error="assert False",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="Test assertion failed",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with patch(
                "jenkins_job_insight.main.run_parallel_with_limit",
                new_callable=AsyncMock,
            ) as mock_parallel:

                async def return_mock_results(coroutines, **kwargs):
                    for coro in coroutines:
                        coro.close()
                    return [[mock_analysis]]

                mock_parallel.side_effect = return_mock_results

                response = test_client.post(
                    "/analyze-failures",
                    json={
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
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "completed"
                assert data["enriched_xml"] is None  # No enriched_xml in failures mode

    def test_raw_xml_enriched_xml_contains_analysis(self, test_client) -> None:
        """Test that enriched_xml contains ai_classification properties."""
        mock_analysis = FailureAnalysis(
            test_name="tests.test_auth.test_login",
            error="assert False",
            analysis=AnalysisDetail(
                classification="PRODUCT BUG",
                details="Auth service down",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with patch(
                "jenkins_job_insight.main.run_parallel_with_limit",
                new_callable=AsyncMock,
            ) as mock_parallel:

                async def return_mock_results(coroutines, **kwargs):
                    for coro in coroutines:
                        coro.close()
                    return [[mock_analysis]]

                mock_parallel.side_effect = return_mock_results

                response = test_client.post(
                    "/analyze-failures",
                    json={
                        "raw_xml": self.SAMPLE_XML,
                        "ai_provider": "claude",
                        "ai_model": "test-model",
                    },
                )
                data = response.json()
                assert data["enriched_xml"] is not None
                assert "ai_classification" in data["enriched_xml"]
                assert "PRODUCT BUG" in data["enriched_xml"]


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


class TestAppLifespan:
    """Tests for application lifespan events."""

    def test_app_initializes_db_on_startup(
        self, mock_settings, temp_db_path: Path
    ) -> None:
        """Test that database is initialized on app startup."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            from starlette.testclient import TestClient
            from jenkins_job_insight.main import app

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
        assert schema["info"]["title"] == "Jenkins Job Insight"
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
            "/register",
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
        response = test_client.get("/api/dashboard")
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

            response = test_client.get("/api/dashboard")
            assert response.status_code == 200
            data = response.json()
            assert isinstance(data, list)
            assert len(data) == count

    def test_api_dashboard_calls_storage(self, test_client) -> None:
        """Test that the endpoint delegates to list_results_for_dashboard."""
        with patch(
            "jenkins_job_insight.main.list_results_for_dashboard",
            new_callable=AsyncMock,
        ) as mock_list:
            mock_list.return_value = []
            response = test_client.get("/api/dashboard")
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

            response = test_client.get("/api/dashboard")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            item = data[0]
            assert item["job_id"] == "api-dash-meta"
            assert item["status"] == "completed"
            assert "created_at" in item
            assert item["job_name"] == "my-pipeline"
            assert item["build_number"] == 42


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
        from jenkins_job_insight import storage

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
        from jenkins_job_insight import storage

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
        from jenkins_job_insight import storage

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
        from jenkins_job_insight import storage

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
        reviewer_name = "test-reviewer"
        test_client.cookies.set("jji_username", reviewer_name)
        response = test_client.put(
            "/results/job-rev-1/reviewed",
            json={"test_name": "test_foo", "reviewed": True},
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
        test_client.cookies.clear()

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
        from jenkins_job_insight import storage

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
        with patch("jenkins_job_insight.main.httpx.AsyncClient") as MockClient:
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
        with patch("jenkins_job_insight.main.httpx.AsyncClient") as MockClient:
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
        with patch("jenkins_job_insight.main.httpx.AsyncClient") as MockClient:
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
        with patch("jenkins_job_insight.main.httpx.AsyncClient") as MockClient:
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
            with patch(
                "jenkins_job_insight.main.generate_github_issue_content"
            ) as mock_gen:
                mock_gen.return_value = {
                    "title": "Fix: login handler missing catch",
                    "body": "## Test Failure\n\nDetails...",
                }
                with patch(
                    "jenkins_job_insight.main.search_github_duplicates"
                ) as mock_dup:
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
        from jenkins_job_insight.config import get_settings

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
            with patch(
                "jenkins_job_insight.main.generate_github_issue_content"
            ) as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch(
                    "jenkins_job_insight.main.search_github_duplicates"
                ) as mock_dup:
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
            with patch(
                "jenkins_job_insight.main.generate_jira_bug_content"
            ) as mock_gen:
                mock_gen.return_value = {
                    "title": "DNS timeout on internal resolver",
                    "body": "h2. Summary\n\nDNS resolution fails",
                }
                with patch(
                    "jenkins_job_insight.main.search_jira_duplicates"
                ) as mock_dup:
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
        from jenkins_job_insight.config import get_settings

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
            with patch(
                "jenkins_job_insight.main.generate_jira_bug_content"
            ) as mock_gen:
                mock_gen.return_value = {"title": "T", "body": "B"}
                with patch(
                    "jenkins_job_insight.main.search_jira_duplicates"
                ) as mock_dup:
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
        with patch("jenkins_job_insight.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/99",
                "number": 99,
            }
            with _with_github_issue_config():
                test_client.cookies.set("jji_username", "testuser")
                response = test_client.post(
                    "/results/job-create-gh/create-github-issue",
                    json={
                        "test_name": "test_login_success",
                        "title": "Bug: login fails",
                        "body": "## Details\nLogin returns 500",
                    },
                )
                test_client.cookies.clear()
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
        from jenkins_job_insight.config import get_settings

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
        with patch("jenkins_job_insight.main.create_jira_bug") as mock_create:
            mock_create.return_value = {
                "key": "PROJ-456",
                "url": "https://jira.example.com/browse/PROJ-456",
            }
            # Mock settings to have jira_enabled=True
            with _enable_feature("jira_enabled"):
                test_client.cookies.set("jji_username", "testuser")
                response = test_client.post(
                    "/results/job-create-jira/create-jira-bug",
                    json={
                        "test_name": "test_login_success",
                        "title": "DNS timeout",
                        "body": "DNS resolution fails",
                    },
                )
                test_client.cookies.clear()
        assert response.status_code == 201
        data = response.json()
        assert data["key"] == "PROJ-456"
        assert data["comment_id"] > 0
        # Verify the auto-added tracker comment content and attribution
        all_comments = await storage.get_comments_for_job("job-create-jira")
        tracker_comment = next(c for c in all_comments if c["id"] == data["comment_id"])
        assert "PROJ-456" in tracker_comment["comment"]
        assert "DNS timeout" in tracker_comment["comment"]
        assert tracker_comment["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_create_jira_disabled_returns_403(self, test_client):
        """Creating a Jira bug when Jira is disabled returns 403."""
        from jenkins_job_insight.config import get_settings

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
        with patch(
            "jenkins_job_insight.storage.override_classification"
        ) as mock_override:
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
            "jenkins_job_insight.storage.override_classification",
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
            patch("jenkins_job_insight.main.generate_github_issue_content") as mock_gen,
            patch("jenkins_job_insight.main.search_github_duplicates") as mock_dup,
            patch("jenkins_job_insight.main.create_github_issue") as mock_create,
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
        with patch("jenkins_job_insight.main.create_github_issue") as mock_create:
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
        with patch("jenkins_job_insight.main.create_github_issue") as mock_create:
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
        with patch("jenkins_job_insight.main.create_jira_bug") as mock_create:
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
        with patch("jenkins_job_insight.main.create_jira_bug") as mock_create:
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

        monkeypatch.setattr(
            "jenkins_job_insight.main.storage.set_test_classification", _boom
        )
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
    """Tests for the _wait_for_jenkins_completion function."""

    @pytest.mark.asyncio
    async def test_already_completed_returns_true(self) -> None:
        """Job that is already finished returns True on first poll."""
        with patch("jenkins_job_insight.jenkins.JenkinsClient") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.return_value = {
                "building": False,
                "result": "SUCCESS",
            }

            from jenkins_job_insight.main import _wait_for_jenkins_completion

            result, error = await _wait_for_jenkins_completion(
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
            patch("jenkins_job_insight.jenkins.JenkinsClient") as mock_cls,
            patch("jenkins_job_insight.main.asyncio.sleep", side_effect=fake_sleep),
            patch(
                "jenkins_job_insight.main._time.monotonic", side_effect=fake_monotonic
            ),
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = [
                {"building": True},
                {"building": True},
                {"building": False, "result": "FAILURE"},
            ]

            from jenkins_job_insight.main import _wait_for_jenkins_completion

            result, error = await _wait_for_jenkins_completion(
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
            patch("jenkins_job_insight.jenkins.JenkinsClient") as mock_cls,
            patch("jenkins_job_insight.main.asyncio.sleep", side_effect=fake_sleep),
            patch(
                "jenkins_job_insight.main._time.monotonic", side_effect=fake_monotonic
            ),
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.return_value = {"building": True}

            from jenkins_job_insight.main import _wait_for_jenkins_completion

            result, error = await _wait_for_jenkins_completion(
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
            patch("jenkins_job_insight.jenkins.JenkinsClient") as mock_cls,
            patch("jenkins_job_insight.main.asyncio.sleep", side_effect=fake_sleep),
            patch(
                "jenkins_job_insight.main._time.monotonic", side_effect=fake_monotonic
            ),
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = [
                OSError("connection refused"),
                {"building": False, "result": "SUCCESS"},
            ]

            from jenkins_job_insight.main import _wait_for_jenkins_completion

            result, error = await _wait_for_jenkins_completion(
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
        with patch("jenkins_job_insight.jenkins.JenkinsClient") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = ValueError("bad credentials")

            from jenkins_job_insight.main import _wait_for_jenkins_completion

            result, error = await _wait_for_jenkins_completion(
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
        with patch("jenkins_job_insight.jenkins.JenkinsClient") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = jenkins.NotFoundException(
                "job[my-job] does not exist"
            )

            from jenkins_job_insight.main import _wait_for_jenkins_completion

            result, error = await _wait_for_jenkins_completion(
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
            patch("jenkins_job_insight.jenkins.JenkinsClient") as mock_cls,
            patch(
                "jenkins_job_insight.main.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            mock_client = mock_cls.return_value
            mock_client.get_build_info_safe.side_effect = [
                {"building": True},
                {"building": True},
                {"building": True},
                {"building": False, "result": "SUCCESS"},
            ]

            from jenkins_job_insight.main import _wait_for_jenkins_completion

            result, error = await _wait_for_jenkins_completion(
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
                "jenkins_job_insight.main._wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ) as mock_wait,
            patch("jenkins_job_insight.main.update_status", side_effect=capture_status),
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch("jenkins_job_insight.main._resolve_enable_jira", return_value=False),
            patch(
                "jenkins_job_insight.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
                "jenkins_job_insight.main._wait_for_jenkins_completion",
                new_callable=AsyncMock,
            ) as mock_wait,
            patch("jenkins_job_insight.main.update_status", side_effect=capture_status),
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch("jenkins_job_insight.main._resolve_enable_jira", return_value=False),
            patch(
                "jenkins_job_insight.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
                "jenkins_job_insight.main._wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(
                    False,
                    "Timed out waiting for Jenkins job my-job #1 after 10 minutes",
                ),
            ),
            patch("jenkins_job_insight.main.update_status", side_effect=capture_status),
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ) as mock_preserve,
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
                "jenkins_job_insight.main._wait_for_jenkins_completion",
                new_callable=AsyncMock,
            ) as mock_wait,
            patch("jenkins_job_insight.main.update_status", side_effect=capture_status),
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch("jenkins_job_insight.main._resolve_enable_jira", return_value=False),
            patch(
                "jenkins_job_insight.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
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
        from jenkins_job_insight.main import _build_request_params
        from jenkins_job_insight.models import AnalyzeRequest

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

        from jenkins_job_insight.encryption import SENSITIVE_KEYS, _ENCRYPTED_PREFIX
        from jenkins_job_insight.main import _build_request_params
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.config import get_settings
        from jenkins_job_insight.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _reconstruct_from_params

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
        from jenkins_job_insight.main import _reconstruct_from_params

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
        from jenkins_job_insight.config import get_settings
        from jenkins_job_insight.main import _build_request_params, _resume_waiting_jobs
        from jenkins_job_insight.models import AnalyzeRequest

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
        with patch(
            "jenkins_job_insight.main.process_analysis_with_id",
            new_callable=AsyncMock,
        ) as mock_process:
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
        from jenkins_job_insight.main import _resume_waiting_jobs

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

        from jenkins_job_insight.config import get_settings
        from jenkins_job_insight.main import _build_request_params
        from jenkins_job_insight.models import AnalyzeRequest

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
                "jenkins_job_insight.main.process_analysis_with_id",
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
                with patch(
                    "jenkins_job_insight.main.asyncio.sleep", new_callable=AsyncMock
                ):
                    from starlette.testclient import TestClient
                    from jenkins_job_insight.main import app

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
            from jenkins_job_insight.main import app

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
        with patch("jenkins_job_insight.main.process_analysis_with_id") as mock_process:
            response = test_client.post(
                "/analyze",
                json={
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
        with patch("jenkins_job_insight.main.process_analysis_with_id") as mock_process:
            response = test_client.post(
                "/analyze",
                json={
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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _merge_settings, _resolve_peer_ai_configs
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _resolve_peer_ai_configs
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _resolve_peer_ai_configs
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.config import get_settings
        from jenkins_job_insight.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from jenkins_job_insight.models import AnalyzeRequest

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
        """POST /analyze-failures with peer_ai_configs passes them to analyze_failure_group."""
        mock_analysis = FailureAnalysis(
            test_name="test_foo",
            error="assert False",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="Test assertion failed",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with patch(
                "jenkins_job_insight.main.analyze_failure_group",
                new_callable=AsyncMock,
            ) as mock_analyze_group:
                mock_analyze_group.return_value = [mock_analysis]

                with patch(
                    "jenkins_job_insight.main.run_parallel_with_limit",
                    new_callable=AsyncMock,
                ) as mock_parallel:

                    async def run_coroutines(coroutines, **kwargs):
                        return [await coro for coro in coroutines]

                    mock_parallel.side_effect = run_coroutines

                    response = test_client.post(
                        "/analyze-failures",
                        json={
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
                    assert response.status_code == 200
                    # Verify analyze_failure_group was called with peer params
                    call_kwargs = mock_analyze_group.call_args[1]
                    passed_peers = call_kwargs["peer_ai_configs"]
                    assert len(passed_peers) == 1
                    # Normalize to dict for comparison (may be AiConfigEntry or dict)
                    peer = (
                        passed_peers[0]
                        if isinstance(passed_peers[0], dict)
                        else passed_peers[0].model_dump()
                    )
                    assert peer == {"ai_provider": "gemini", "ai_model": "pro"}
                    assert call_kwargs["peer_analysis_max_rounds"] == 7

    def test_build_request_params_stores_resolved_peer_configs(
        self, mock_settings
    ) -> None:
        """_build_request_params stores the resolved peer configs, not raw body."""
        from jenkins_job_insight.main import _build_request_params
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import (
            _build_request_params,
            _merge_settings,
            _reconstruct_from_params,
        )
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _reconstruct_from_params

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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
                "jenkins_job_insight.main._wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch("jenkins_job_insight.main.update_status", new_callable=AsyncMock),
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                side_effect=capture_phase,
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch("jenkins_job_insight.main._resolve_enable_jira", return_value=False),
            patch(
                "jenkins_job_insight.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
            patch("jenkins_job_insight.main.update_status", new_callable=AsyncMock),
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                side_effect=capture_phase,
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch("jenkins_job_insight.main._resolve_enable_jira", return_value=False),
            patch(
                "jenkins_job_insight.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
            patch("jenkins_job_insight.main.update_status", new_callable=AsyncMock),
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                side_effect=capture_phase,
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch("jenkins_job_insight.main._resolve_enable_jira", return_value=True),
            patch(
                "jenkins_job_insight.main._enrich_result_with_jira",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
            patch(
                "jenkins_job_insight.main.update_status", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "jenkins_job_insight.main.update_progress_phase",
                side_effect=RuntimeError("DB connection lost"),
            ),
            patch(
                "jenkins_job_insight.main.analyze_job", new_callable=AsyncMock
            ) as mock_analyze,
            patch("jenkins_job_insight.main._resolve_enable_jira", return_value=False),
            patch(
                "jenkins_job_insight.main.populate_failure_history",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main.storage.make_classifications_visible",
                new_callable=AsyncMock,
            ),
            patch(
                "jenkins_job_insight.main._preserve_request_params",
                new_callable=AsyncMock,
            ),
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
                    "jenkins_job_insight.main.analyze_job",
                    new_callable=AsyncMock,
                ) as mock_analyze,
                patch(
                    "jenkins_job_insight.main._resolve_enable_jira",
                    return_value=False,
                ),
                patch(
                    "jenkins_job_insight.main.populate_failure_history",
                    new_callable=AsyncMock,
                ),
                patch(
                    "jenkins_job_insight.main.storage.make_classifications_visible",
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
        from jenkins_job_insight.main import process_analysis_with_id
        from jenkins_job_insight.models import AnalyzeRequest

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
                "jenkins_job_insight.main.analyze_job",
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
        """POST /analyze-failures must seed and preserve request_params."""
        mock_analysis = FailureAnalysis(
            test_name="test_foo",
            error="assert False",
            analysis=AnalysisDetail(
                classification="CODE ISSUE",
                details="Test failed",
            ),
        )

        with patch("jenkins_job_insight.main.RepositoryManager") as mock_repo_cls:
            mock_repo_instance = mock_repo_cls.return_value
            mock_repo_instance.clone.return_value = None
            mock_repo_instance.cleanup.return_value = None

            with (
                patch(
                    "jenkins_job_insight.main.analyze_failure_group",
                    new_callable=AsyncMock,
                ) as mock_analyze_group,
                patch(
                    "jenkins_job_insight.main.run_parallel_with_limit",
                    new_callable=AsyncMock,
                ) as mock_parallel,
            ):
                mock_analyze_group.return_value = [mock_analysis]

                async def run_coroutines(coroutines, **kwargs):
                    return [await coro for coro in coroutines]

                mock_parallel.side_effect = run_coroutines

                response = test_client.post(
                    "/analyze-failures",
                    json={
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
                assert response.status_code == 200
                data = response.json()
                job_id = data["job_id"]

        # Fetch the stored result and verify request_params survived
        result_response = test_client.get(
            f"/results/{job_id}",
            headers={"accept": "application/json"},
        )
        assert result_response.status_code == 200
        result_data = result_response.json()
        assert "result" in result_data
        result = result_data["result"]
        assert "request_params" in result, (
            "request_params must be preserved after analyze-failures completes"
        )
        rp = result["request_params"]
        assert rp["ai_provider"] == "cursor"
        assert rp["ai_model"] == "test-model"


class TestReAnalyzeEndpoint:
    """Tests for POST /re-analyze/{job_id}."""

    def test_re_analyze_not_found(self, test_client) -> None:
        """Re-analyze returns 404 when job_id does not exist."""
        response = test_client.post("/re-analyze/nonexistent", json={})
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_re_analyze_no_request_params(self, test_client) -> None:
        """Re-analyze returns 400 when original has no request_params."""
        from jenkins_job_insight import storage

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
        from jenkins_job_insight import storage

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
        with patch("jenkins_job_insight.main.process_analysis_with_id") as mock_process:
            response = test_client.post("/re-analyze/job-reanalyze-ok", json={})
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert "job_id" in data
        assert data["job_id"] != "job-reanalyze-ok"  # New job_id
        assert "result_url" in data
        mock_process.assert_called_once()
        assert data["result_url"].endswith(f"/results/{data['job_id']}")


class TestBuildEffectiveJiraSettings:
    """Tests for _build_effective_jira_settings helper."""

    def test_no_user_token_returns_original(self):
        """When no user token, original settings returned unchanged."""
        from jenkins_job_insight.main import _build_effective_jira_settings

        settings = Settings()
        result = _build_effective_jira_settings(settings, "", "")
        assert result is settings

    def test_user_token_clears_server_pat(self):
        """User token clears server PAT so it takes precedence."""
        from jenkins_job_insight.main import _build_effective_jira_settings

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
        from jenkins_job_insight.main import _build_effective_jira_settings

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
        from jenkins_job_insight.main import _build_effective_jira_settings

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
        from jenkins_job_insight.main import _build_effective_jira_settings

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
        with patch("jenkins_job_insight.main.httpx.AsyncClient") as mock_client_class:
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
            )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_github_invalid_token(self, test_client):
        import httpx

        with patch("jenkins_job_insight.main.httpx.AsyncClient") as mock_client_class:
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
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_jira_no_url_configured(self, test_client):
        response = test_client.post(
            "/api/validate-token",
            json={"token_type": "jira", "token": "jira-token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert "not configured" in data["message"]


class TestJiraProjectsEndpoint:
    """Tests for POST /api/jira-projects."""

    def test_no_jira_url_returns_empty(self, test_client):
        """No JIRA_URL configured returns empty list."""
        response = test_client.post("/api/jira-projects", json={})
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_returns_projects(self, test_client):
        """Returns project list from JiraClient.list_projects."""
        from jenkins_job_insight.main import app, get_settings

        projects = [{"key": "PROJ", "name": "My Project"}]
        jira_settings = _build_wait_settings(jira_url="https://jira.example.com")
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            with patch("jenkins_job_insight.jira.JiraClient") as MockJiraClient:
                mock_client = AsyncMock()
                mock_client.list_projects = AsyncMock(return_value=projects)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                MockJiraClient.return_value = mock_client
                response = test_client.post(
                    "/api/jira-projects",
                    json={"jira_token": "tok", "jira_email": "u@e.com"},  # noqa: S106
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
            "/api/jira-security-levels", json={"project_key": "PROJ"}
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_no_token_returns_empty(self, test_client):
        """No jira_token returns empty list."""
        from jenkins_job_insight.main import app, get_settings

        jira_settings = _build_wait_settings(jira_url="https://jira.example.com")
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            response = test_client.post(
                "/api/jira-security-levels", json={"project_key": "PROJ"}
            )
            assert response.status_code == 200
            assert response.json() == []
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_returns_security_levels(self, test_client):
        """Returns security levels from JiraClient.list_security_levels."""
        from jenkins_job_insight.main import app, get_settings

        levels = [{"id": "10", "name": "Internal", "description": "Internal only"}]
        jira_settings = _build_wait_settings(jira_url="https://jira.example.com")
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            with patch("jenkins_job_insight.jira.JiraClient") as MockJiraClient:
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
                    },  # noqa: S106
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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

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
        from jenkins_job_insight.main import _merge_settings
        from jenkins_job_insight.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
        )
        settings = Settings()
        merged = _merge_settings(body, settings)
        assert merged.force_analysis is False
