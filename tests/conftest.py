"""Shared fixtures for jenkins-job-insight tests."""

import os
import tempfile
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Generator
from unittest.mock import MagicMock, patch

import httpx
from ai_cli_runner import AIResult
import pytest

from jenkins_job_insight.cli.client import JJIClient
from jenkins_job_insight.config import Settings
from jenkins_job_insight.models import (
    AnalysisDetail,
    AnalysisResult,
    AnalyzeRequest,
    FailureAnalysis,
    ProductBugReport,
)

CLI_TEST_BASE_URL = "http://localhost:8700"


def build_test_env(**overrides: str) -> dict[str, str]:
    """Return baseline Jenkins env with per-test overrides applied.

    Shared by test_config.py, test_reportportal_config.py, and any test
    module that needs a minimal environment for ``Settings``.
    """
    base = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
    }
    base.update(overrides)
    return base


def make_test_client(
    handler: Callable[[httpx.Request], httpx.Response],
    username: str = "",
    api_key: str = "",
) -> JJIClient:
    """Create a JJIClient with a mock transport for testing.

    The mock httpx.Client is created with base_url set so that
    relative paths (e.g. "/health") resolve correctly.

    Shared by test_cli_client.py and test_reportportal_cli.py.
    """
    cookies = {}
    if username:
        cookies["jji_username"] = username
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    mock_http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=CLI_TEST_BASE_URL,
        cookies=cookies,
        headers=headers,
    )
    client = JJIClient(CLI_TEST_BASE_URL, username=username, api_key=api_key)
    client._client.close()
    client._client = mock_http
    return client


@pytest.fixture
def mock_env_vars() -> Generator[dict[str, str], None, None]:
    """Provide minimal environment variables for Settings."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
    }
    with patch.dict(os.environ, env, clear=False):
        yield env


@pytest.fixture
def full_env_vars() -> Generator[dict[str, str], None, None]:
    """Provide full environment variables including AI config."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
    }
    with patch.dict(os.environ, env, clear=False):
        yield env


@pytest.fixture
def settings(mock_env_vars: dict[str, str]) -> Settings:
    """Create Settings instance with mocked environment."""
    return Settings()


@pytest.fixture
def temp_db_path() -> Generator[Path, None, None]:
    """Create a temporary database path for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def sample_analyze_request() -> AnalyzeRequest:
    """Create a sample analyze request for testing."""
    return AnalyzeRequest(
        job_name="my-job",
        build_number=123,
        tests_repo_url="https://github.com/example/repo",
    )


@pytest.fixture
def sample_failure_analysis() -> FailureAnalysis:
    """Create a sample failure analysis for testing."""
    return FailureAnalysis(
        test_name="test_login_success",
        error="AssertionError: Expected 200, got 500",
        analysis=AnalysisDetail(
            classification="PRODUCT BUG",
            affected_tests=["test_login_success"],
            details="The authentication service is returning an error.",
            product_bug_report=ProductBugReport(
                title="Login fails with valid credentials",
                severity="high",
                component="auth",
                description="Users cannot log in even with correct username and password",
                evidence="Error: Authentication service returned 500",
            ),
        ),
    )


@pytest.fixture
def sample_analysis_result(
    sample_failure_analysis: FailureAnalysis,
) -> AnalysisResult:
    """Create a sample analysis result for testing."""
    return AnalysisResult(
        job_id="test-job-123",
        job_name="my-job",
        build_number=123,
        jenkins_url="https://jenkins.example.com/job/my-job/123/",
        status="completed",
        summary="1 failure analyzed: 1 product bug found",
        ai_provider="claude",
        ai_model="test-model",
        failures=[sample_failure_analysis],
    )


@pytest.fixture
def fake_clock() -> tuple[Callable[[], float], Callable[[float], Awaitable[None]]]:
    """Provide a controllable monotonic clock and async sleep for timer tests."""
    clock = [0.0]

    def monotonic() -> float:
        return clock[0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    return monotonic, sleep


@pytest.fixture
def mock_jenkins_client() -> MagicMock:
    """Create a mock Jenkins client."""
    mock = MagicMock()
    mock.get_build_console.return_value = (
        "Build started\nTest failed: test_example\nBuild finished"
    )
    mock.get_build_info_safe.return_value = {
        "result": "FAILURE",
        "building": False,
        "number": 123,
    }
    return mock


@pytest.fixture
def mock_ai_cli() -> Generator[MagicMock, None, None]:
    """Mock the call_ai_cli function."""
    with patch("jenkins_job_insight.analyzer.call_ai_cli") as mock:
        mock.return_value = AIResult(
            success=True,
            text='{"classification": "CODE ISSUE", "affected_tests": ["test_example"], "details": "The test failed due to a missing configuration.", "code_fix": {"file": "tests/test_example.py", "line": "42", "change": "Add the missing import statement"}}',
        )
        yield mock
