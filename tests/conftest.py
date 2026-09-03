"""Shared fixtures for rootcoz tests."""

import os
import tempfile
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# pi_sidecar_client reads PI_SIDECAR_LOG_LEVEL at import time and passes the
# raw value to logging.setLevel(); Python >=3.12 rejects lowercase level names
# such as "debug". Normalize before that import runs.
os.environ["PI_SIDECAR_LOG_LEVEL"] = (
    os.environ.get("PI_SIDECAR_LOG_LEVEL", "INFO").upper() or "INFO"
)

import httpx
import pytest
from fastapi.testclient import TestClient
from pi_sidecar_client import AIResult

from rootcoz import storage
from rootcoz.ai_client import _setup_usage_recorder
from rootcoz.cli.client import RootCozClient
from rootcoz.config import Settings
from rootcoz.models import (
    AnalysisDetail,
    AnalysisResult,
    AnalyzeRequest,
    FailureAnalysis,
    ProductBugReport,
)


@pytest.fixture(scope="session", autouse=True)
def _register_usage_recorder() -> None:
    """Ensure the pi-sidecar usage recorder is wired up for all tests."""
    _setup_usage_recorder()


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
        "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "REQUIRE_APPROVAL": "false",
    }
    base.update(overrides)
    return base


def make_test_client(
    handler: Callable[[httpx.Request], httpx.Response],
    username: str = "",
    api_key: str = "",
) -> RootCozClient:
    """Create a RootCozClient with a mock transport for testing.

    The mock httpx.Client is created with base_url set so that
    relative paths (e.g. "/health") resolve correctly.

    Shared by test_cli_client.py and test_reportportal_cli.py.
    """
    cookies = {}
    if username:
        cookies["rootcoz_username"] = username
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    mock_http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=CLI_TEST_BASE_URL,
        cookies=cookies,
        headers=headers,
    )
    client = RootCozClient(CLI_TEST_BASE_URL, username=username, api_key=api_key)
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


def make_app_client(temp_db_path: Path, env_overrides: dict[str, str] | None = None):
    """Generator factory to create a FastAPI TestClient with patched env and DB.

    Yields a ``TestClient`` instance with the given environment overrides
    applied on top of sensible defaults (admin key, encryption key, secure
    cookies off, approval off).  Clears the ``get_settings`` LRU cache on
    entry and exit.

    Usage in fixtures::

        @pytest.fixture
        def client(_init_db, temp_db_path):
            yield from make_app_client(temp_db_path)
    """
    from rootcoz.config import clear_db_settings_cache

    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "SECURE_COOKIES": "false",
        "DB_PATH": str(temp_db_path),
        "REQUIRE_APPROVAL": "false",
    }
    if env_overrides:
        env.update(env_overrides)
    with patch.dict(os.environ, env, clear=True):
        clear_db_settings_cache()
        with patch.object(storage, "DB_PATH", temp_db_path):
            from rootcoz.main import app

            with TestClient(app) as c:
                yield c
    clear_db_settings_cache()


def admin_login(
    client,
    username: str = "admin",
    api_key: str = "test-admin-key-16chars",  # pragma: allowlist secret
):
    """Login as admin and return session cookies."""
    resp = client.post(
        "/api/auth/login", json={"username": username, "api_key": api_key}
    )
    assert resp.status_code == 200
    return resp.cookies


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
def mock_ai() -> Generator[MagicMock, None, None]:
    """Mock the call_ai function."""
    with patch("rootcoz.engine.core.call_ai_once") as mock:
        mock.return_value = AIResult(
            success=True,
            text=(
                '{"classification": "CODE ISSUE", "affected_tests": ["test_example"],'
                ' "details": "The test failed due to a missing configuration.",'
                ' "code_fix": {"file": "tests/test_example.py", "line": "42",'
                ' "change": "Add the missing import statement"}}'
            ),
        )
        yield mock


@pytest.fixture(autouse=True)
def _clear_db_settings_cache():
    """Clear DB settings cache before each test to prevent state leakage."""
    from rootcoz.config import clear_db_settings_cache

    clear_db_settings_cache()
    yield
    clear_db_settings_cache()


@pytest.fixture(autouse=True)
def _mock_sidecar_calls():
    """Prevent ALL tests from hitting a real sidecar or any AI HTTP client.

    Patches every import site that can reach pi-sidecar (including
    ``rootcoz.ai_client`` bound names). Accidental AI calls raise
    AssertionError so tests fail loudly instead of talking to :9100.
    """

    def _deny_sidecar(*_a, **_kw):
        raise AssertionError(
            "Unexpected real sidecar/AI call in unit test — mock it explicitly"
        )

    mock_client = MagicMock()
    mock_client.delete_session = AsyncMock()
    mock_client.get_models = AsyncMock(return_value=[])
    mock_client.refresh_models = AsyncMock(return_value=[])
    mock_client.health = AsyncMock(return_value={"status": "ok"})
    mock_client.create_session = AsyncMock(return_value="mock-session")
    mock_client.prompt = AsyncMock(side_effect=_deny_sidecar)
    mock_client.abort = AsyncMock()

    deny_ai = AsyncMock(side_effect=_deny_sidecar)

    with (
        # --- clients (bound imports + package) ---
        patch("pi_sidecar_client.get_sidecar_client", return_value=mock_client),
        patch("rootcoz.engine.chat.get_sidecar_client", return_value=mock_client),
        patch("rootcoz.peer_analysis.get_sidecar_client", return_value=mock_client),
        # list_models is NOT stubbed: it runs against the mock client above
        # (get_models → []) so catalog/routing tests can still exercise it.
        # --- low-level AI calls inside ai_client ---
        patch("rootcoz.ai_client._call_ai", deny_ai),
        patch("rootcoz.ai_client._call_ai_once", deny_ai),
        patch(
            "rootcoz.ai_client._list_models_raw",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("rootcoz.ai_client.call_ai", deny_ai),
        patch("rootcoz.ai_client.call_ai_once", deny_ai),
        # --- call sites that import call_ai* by name ---
        patch("rootcoz.engine.chat.call_ai", deny_ai),
        patch("rootcoz.engine.core.call_ai_once", deny_ai),
        patch("rootcoz.peer_analysis.call_ai", deny_ai),
        patch("rootcoz.peer_analysis.call_ai_once", deny_ai),
        patch("pi_sidecar_client.call_ai", deny_ai),
        patch("pi_sidecar_client.call_ai_once", deny_ai),
        # --- health checks ---
        patch(
            "rootcoz.main.check_sidecar_available",
            new_callable=AsyncMock,
            return_value=(True, "mocked"),
        ),
        patch(
            "pi_sidecar_client.check_sidecar_available",
            new_callable=AsyncMock,
            return_value=(True, "mocked"),
        ),
    ):
        yield mock_client
