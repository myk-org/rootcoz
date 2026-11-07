"""Tests for FastAPI main application."""

import asyncio
import json
import logging
import os
import socket
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import aiosqlite
import httpx
import jenkins
import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import SecretStr

from rootcoz import storage
from rootcoz.config import Settings
from rootcoz.encryption import encrypt_sensitive_fields
from rootcoz.engine.core import AnalysisIncompleteError
from rootcoz.models import (
    AiConfigEntry,
    AnalysisDetail,
    FailureAnalysis,
)
from rootcoz.sources.jenkins_source import JenkinsError
from tests.conftest import capture_main_logs, make_test_client

# Fake credentials for tests — annotated once to suppress Ruff S105/S106 globally.
FAKE_JENKINS_PASSWORD = "not-a-real-password"  # pragma: allowlist secret
FAKE_GITHUB_TOKEN = "not-a-real-token"

# The submitted content and credential a create-github-issue test sends, so a
# log or detail that echoes either one can be asserted against.
_REPORT_GITHUB_TOKEN = "ghp_report_token_0123456789"  # pragma: allowlist secret
_REPORT_BODY = "REPORT-BODY-MUST-NOT-BE-LOGGED"
# A bare 40-hex run: no credential prefix, so only the length/shape rule of the
# audit heuristic can recognise it as a token.
_PASTED_HEX = "9f8e7d6c5b4a39281706f5e4d3c2b1a0ffee0011"  # pragma: allowlist secret


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
    (e.g. ``settings.tests_repo_url`` for GitHub, the per-method Jira fields
    for Jira — including a DB-cache ``jira_auth_mode`` pin, since the auth
    method is Server Settings only and never read from the environment).

    Usage::

        with _enable_feature("github_issues_enabled"):
            response = test_client.post(...)
    """
    from contextlib import ExitStack

    from rootcoz import config
    from rootcoz.config import get_settings

    # Map computed properties to the env vars that endpoint guards check
    raw_env_patches: dict[str, dict[str, str]] = {
        "github_issues_enabled": {
            "TESTS_REPO_URL": "https://github.com/test-org/test-repo",
            "GITHUB_TOKEN": "ghp_test_token",
            "ENABLE_GITHUB_ISSUES": "true",
        },
        "jira_enabled": {
            # JIRA_URL is the deprecated alias — it fills both per-method
            # URL fields when their canonical env vars are unset.
            "JIRA_URL": "https://jira.example.com",
            "JIRA_EMAIL": "user@example.com",
            "JIRA_PROJECT_KEY": "TEST",
            "JIRA_API_TOKEN": "test_jira_token",
            "ENABLE_JIRA_ISSUES": "true",
        },
    }
    # DB-only pins applied on top of the env patches (jira_auth_mode is
    # never read from the environment — see Settings sources).
    db_pins: dict[str, dict[str, str]] = {
        "jira_enabled": {"jira_auth_mode": "basic"},
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
        pins = db_pins.get(prop_name, {})
        if pins:
            stack.enter_context(patch.dict(config._db_settings_cache, pins))
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def _build_jenkins_request_params(
    body,
    merged,
    ai_provider: str,
    ai_model: str,
    peer_ai_configs_resolved: list | None = None,
) -> dict:
    """Build encrypted Jenkins request_params (mirrors former main helper)."""
    from rootcoz.config import parse_repo_ref
    from rootcoz.encryption import encrypt_sensitive_fields
    from rootcoz.engine.core import resolve_additional_repos
    from rootcoz.main import (
        _apply_base_analysis_overrides,
        _build_base_request_params,
    )
    from rootcoz.request_resolution import resolve_tests_repo_token
    from rootcoz.rootcoz_repo_settings import resolve_tests_repo_url
    from rootcoz.sources.jenkins_source import JenkinsSource

    resolved_tests_repo = resolve_tests_repo_url(body, merged)
    resolved_tests_repo, tests_repo_ref = parse_repo_ref(resolved_tests_repo)
    resolved_tests_repo_token = (
        resolve_tests_repo_token(body, merged) if resolved_tests_repo else ""
    )
    resolved_additional = resolve_additional_repos(body, merged)
    params = _build_base_request_params(
        ai_provider,
        ai_model,
        peer_ai_configs_resolved,
        tests_repo_url=resolved_tests_repo,
        tests_repo_token=resolved_tests_repo_token,
        tests_repo_ref=tests_repo_ref,
        additional_repos=resolved_additional,
    )
    _apply_base_analysis_overrides(params, body, merged)
    JenkinsSource.build_request_params(body, merged, params)
    params["analysis_type"] = "jenkins"
    params["raw_prompt"] = getattr(body, "raw_prompt", None) or ""
    params["issue_prompt"] = getattr(body, "issue_prompt", None) or ""
    return encrypt_sensitive_fields(params)


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
        # Clear the lru_cache and DB settings cache
        from rootcoz.config import clear_db_settings_cache

        clear_db_settings_cache()
        try:
            yield
        finally:
            clear_db_settings_cache()


_ADMIN_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_ADMIN_KEY}"}


def _gh_release(version: str, name: str = "", body: str = "body") -> dict[str, Any]:
    """A GitHub API release dict (tag_name keeps or drops the v prefix)."""
    return {
        "tag_name": version,
        "name": name or version,
        "body": body,
        "published_at": "",
        "html_url": "",
    }


def _settings_item(test_client, key: str) -> dict[str, Any]:
    """One item from GET /api/admin/settings metadata by key."""
    response = test_client.get("/api/admin/settings")
    assert response.status_code == 200
    return next(s for s in response.json() if s["key"] == key)


def _no_key_providers(monkeypatch) -> None:
    """Pin sidecar key-provider discovery to an empty answer.

    ``ai_client.get_sidecar_client`` is a module-level bound name the autouse
    sidecar fixture does not patch, so a dev machine with a live sidecar would
    otherwise feed real provider IDs into AMBIENT_AUTH_PROVIDERS validation.
    """
    monkeypatch.setattr(
        "rootcoz.main.supported_key_providers", AsyncMock(return_value=[])
    )


def _provider_auth_status(monkeypatch, configured: dict[str, bool]) -> None:
    """Pin the sidecar's per-provider auth status for AMBIENT_AUTH_PROVIDERS.

    Only the providers in *configured* are registered: any other id answers
    the sidecar's 404, exactly as an unregistered provider does in production.
    """

    async def _status(provider: str) -> dict[str, Any]:
        configured_ids = {name.lower(): state for name, state in configured.items()}
        if provider.lower() not in configured_ids:
            request = httpx.Request("GET", f"http://sidecar/models/{provider}/status")
            raise httpx.HTTPStatusError(
                "provider not registered",
                request=request,
                response=httpx.Response(404, request=request),
            )
        return {
            "provider": provider,
            "registered": True,
            "authStatus": {"configured": configured_ids[provider.lower()]},
        }

    monkeypatch.setattr(
        "pi_sidecar_client.get_sidecar_client",
        lambda: SimpleNamespace(get_model_provider_status=_status),
    )


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

        from rootcoz import ai_client
        from rootcoz.main import app

        ai_client.update_model_catalog(
            [
                {"provider": "claude", "id": "test-model"},
                {"provider": "claude", "id": "opus"},
                {"provider": "gemini", "id": "pro"},
            ]
        )
        with TestClient(app, headers=_ADMIN_AUTH_HEADERS) as client:
            yield client
        ai_client.update_model_catalog(None)


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

    def test_version_endpoint(self, test_client) -> None:
        """Test that /api/version returns version when authenticated."""
        response = test_client.get("/api/version")
        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0

    def test_version_endpoint_requires_auth(
        self, mock_settings, temp_db_path: Path
    ) -> None:
        """Unauthenticated /api/version must not be public."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            from starlette.testclient import TestClient

            from rootcoz.main import app

            with TestClient(app) as client:
                response = client.get("/api/version")
                assert response.status_code == 401

    def test_releases_latest_reports_server_version_context(self) -> None:
        """#333: payload carries server_version + update_available."""
        from rootcoz import main

        with patch.object(main, "_get_app_version", return_value="4.6.0"):
            payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
        assert payload["server_version"] == "4.6.0"
        assert payload["update_available"] is True

        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
        assert payload["server_version"] == "4.7.0"
        assert payload["update_available"] is False

    def test_releases_latest_equivalent_spellings_are_not_updates(self) -> None:
        """4.7 and 4.7.0 are the same release — no update nag."""
        from rootcoz import main

        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload([_gh_release("4.7", "v4.7")])
        assert payload["update_available"] is False

        with patch.object(main, "_get_app_version", return_value="4.7"):
            payload = main._release_payload([_gh_release("v4.7.0", "v4.7.0")])
        assert payload["update_available"] is False

    def test_releases_latest_dev_build_gets_update_notice(self) -> None:
        """A running dev prerelease is older than its own stable release."""
        from rootcoz import main

        with patch.object(main, "_get_app_version", return_value="4.7.0.dev1"):
            payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
        assert payload["update_available"] is True

        # A prerelease of the version the server already runs: no nag
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload([_gh_release("4.7.0-rc1", "v4.7.0-rc1")])
        assert payload["update_available"] is False

    def test_releases_latest_prerelease_ordering(self) -> None:
        """Later prereleases are updates; numeric identifiers compare numerically."""
        from rootcoz.main import _is_newer_release

        assert _is_newer_release("4.7.0-rc2", "4.7.0-rc1") is True
        assert _is_newer_release("4.7.0-rc1", "4.7.0-rc2") is False
        # Numeric identifiers compare numerically when dot-separated (semver §11.4)
        assert _is_newer_release("4.7.0-rc.10", "4.7.0-rc.9") is True
        assert _is_newer_release("4.7.0-rc.9", "4.7.0-rc.10") is False
        # A single identifier compares lexically: "rc10" < "rc9" (spec-correct)
        assert _is_newer_release("4.7.0-rc10", "4.7.0-rc9") is False
        assert _is_newer_release("4.7.0-beta", "4.7.0-alpha") is True
        # Numeric identifiers sort below alphanumeric ones (semver §11.4)
        assert _is_newer_release("4.7.0-alpha", "4.7.0-1") is True
        # A longer identifier list is newer when it is a prefix extension
        assert _is_newer_release("4.7.0-rc.1.2", "4.7.0-rc.1") is True
        assert _is_newer_release("4.7.0-rc.1", "4.7.0-rc.1.2") is False

    def test_releases_latest_post_and_local_builds_not_older(self) -> None:
        """Post-releases and local builds never nag for their own release."""
        from rootcoz import main

        for running in ("4.7.0.post1", "4.7.0+local", "4.7.0+build.5"):
            with patch.object(main, "_get_app_version", return_value=running):
                payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
            assert payload["update_available"] is False, running
            assert payload["running_latest"] is True, running

        # A genuinely newer release still nags a post/local build
        with patch.object(main, "_get_app_version", return_value="4.7.0.post1"):
            payload = main._release_payload([_gh_release("4.7.1", "v4.7.1")])
        assert payload["update_available"] is True

    def test_releases_latest_running_latest_equivalence(self) -> None:
        """running_latest decides What's New eligibility server-side."""
        from rootcoz import main

        # Equivalent spellings
        with patch.object(main, "_get_app_version", return_value="4.7"):
            payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
        assert payload["running_latest"] is True
        assert payload["update_available"] is False

        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload([_gh_release("v4.7", "v4.7")])
        assert payload["running_latest"] is True

        # Same prerelease on both sides
        with patch.object(main, "_get_app_version", return_value="4.7.0-rc1"):
            payload = main._release_payload([_gh_release("4.7.0-rc1", "v4.7.0-rc1")])
        assert payload["running_latest"] is True

        # Prerelease running, release out: not the same, and an update
        with patch.object(main, "_get_app_version", return_value="4.7.0-rc1"):
            payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
        assert payload["running_latest"] is False
        assert payload["update_available"] is True

        # Indeterminate
        with patch.object(main, "_get_app_version", return_value="unknown"):
            payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
        assert payload["running_latest"] is None
        assert payload["update_available"] is None

    def test_releases_latest_unknown_server_version_is_indeterminate(self) -> None:
        """Unparseable server version must never trigger a definite nag."""
        from rootcoz import main

        with patch.object(main, "_get_app_version", return_value="unknown"):
            payload = main._release_payload([_gh_release("4.7.0", "v4.7.0")])
        assert payload["server_version"] == "unknown"
        assert payload["update_available"] is None

    def test_releases_latest_unreachable_fallback_has_server_context(self) -> None:
        """GitHub-unreachable fallback still reports the running version."""
        from rootcoz import main

        with patch.object(main, "_get_app_version", return_value="4.6.0"):
            payload = main._release_payload([])
        assert payload["server_version"] == "4.6.0"
        assert payload["update_available"] is None
        assert payload["version"] == ""
        assert payload["releases"] == []

    def test_releases_payload_aggregates_newer_releases(self) -> None:
        """#341: releases array lists everything newer than the baseline."""
        from rootcoz import main

        releases = [
            _gh_release("v4.7.2", "v4.7.2", "notes 4.7.2"),
            _gh_release("v4.7.1", "v4.7.1", "notes 4.7.1"),
        ]
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases)
        # Oldest first, top-level fields describe the newest release
        assert [r["version"] for r in payload["releases"]] == ["4.7.1", "4.7.2"]
        assert payload["releases"][0]["body"] == "notes 4.7.1"
        assert payload["version"] == "4.7.2"
        assert payload["body"] == "notes 4.7.2"
        assert payload["update_available"] is True

        # Explicit since equal to the server version: same result
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases, since="4.7.0")
        assert [r["version"] for r in payload["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_payload_since_equivalent_spelling_excludes_same(self) -> None:
        """since=4.7 excludes 4.7.0 — same release, not strictly newer."""
        from rootcoz import main

        releases = [
            _gh_release("v4.7.2"),
            _gh_release("v4.7.1"),
            _gh_release("v4.7.0"),
        ]
        with patch.object(main, "_get_app_version", return_value="4.7.2"):
            payload = main._release_payload(releases, since="4.7")
        assert [r["version"] for r in payload["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_payload_unparseable_since_falls_back_to_server(self) -> None:
        from rootcoz import main

        releases = [_gh_release("v4.7.2"), _gh_release("v4.7.1")]
        with patch.object(main, "_get_app_version", return_value="4.7.1"):
            payload = main._release_payload(releases, since="garbage")
        assert [r["version"] for r in payload["releases"]] == ["4.7.2"]

    def test_releases_payload_running_latest_has_no_newer(self) -> None:
        """Server newer than all releases: empty releases array."""
        from rootcoz import main

        releases = [_gh_release("v4.7.2"), _gh_release("v4.7.1")]
        with patch.object(main, "_get_app_version", return_value="4.7.2"):
            payload = main._release_payload(releases)
        assert payload["releases"] == []
        assert payload["running_latest"] is True

    def test_releases_payload_bounded_at_fifteen(self) -> None:
        """More than 15 newer releases: newest 15 kept, oldest first."""
        from rootcoz import main

        releases = [
            _gh_release(f"v4.7.{i}")
            for i in range(17, 0, -1)  # v4.7.17 … v4.7.1
        ]
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases)
        versions = [r["version"] for r in payload["releases"]]
        assert len(versions) == 15
        assert versions == [f"4.7.{i}" for i in range(3, 18)]

    def test_releases_payload_rollback_clamps_since_down_to_server(self) -> None:
        """A since newer than the server (rollback) is clamped down (#341)."""
        from rootcoz import main

        releases = [_gh_release("v4.7.2"), _gh_release("v4.7.1")]
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases, since="4.7.2")
        # Baseline is the server version, not the saved 4.7.2
        assert [r["version"] for r in payload["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_payload_nag_mode_ignores_stale_since(self) -> None:
        """Nag mode: a since older than the server never resurfaces it (#341)."""
        from rootcoz import main

        releases = [_gh_release("v4.7.2"), _gh_release("v4.7.1"), _gh_release("v4.7.0")]
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases, since="4.6.0")
        assert payload["update_available"] is True
        # 4.7.0 is already installed — not listed despite since=4.6.0
        assert [r["version"] for r in payload["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_payload_whats_new_mode_keeps_since_aggregate(self) -> None:
        """Running latest: since older than server still aggregates (#341)."""
        from rootcoz import main

        releases = [_gh_release("v4.7.2"), _gh_release("v4.7.1"), _gh_release("v4.7.0")]
        with patch.object(main, "_get_app_version", return_value="4.7.2"):
            payload = main._release_payload(releases, since="4.7.0")
        assert payload["update_available"] is False
        assert [r["version"] for r in payload["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_payload_normalizes_null_fields(self) -> None:
        """Explicit JSON nulls become empty strings, never crash the dialog."""
        from rootcoz import main

        releases = [
            _gh_release("v4.7.2", "v4.7.2", "notes 4.7.2"),
            {
                **_gh_release("v4.7.1"),
                "body": None,
                "name": None,
                "published_at": None,
                "html_url": None,
            },
        ]
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases)
        entry = payload["releases"][0]
        assert entry["version"] == "4.7.1"
        assert entry["body"] == ""
        assert entry["name"] == ""
        assert entry["published_at"] == ""
        assert entry["html_url"] == ""

    def test_releases_payload_sorts_out_of_order_releases(self) -> None:
        """Publication order is not version order (#341)."""
        from rootcoz import main

        # 4.7.1 published AFTER 4.7.2 — API returns it first
        releases = [_gh_release("v4.7.1"), _gh_release("v4.7.2")]
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases)
        assert payload["version"] == "4.7.2"
        assert [r["version"] for r in payload["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_payload_unparseable_tag_never_becomes_latest(self) -> None:
        """An unparseable tag must not hide available updates (#341)."""
        from rootcoz import main

        releases = [_gh_release("not-a-version"), _gh_release("v4.7.2")]
        with patch.object(main, "_get_app_version", return_value="4.7.0"):
            payload = main._release_payload(releases)
        assert payload["version"] == "4.7.2"
        assert payload["update_available"] is True
        # Aggregate keeps only strictly-newer entries — the unparseable tag
        # is indeterminate, so it never appears among them
        assert [r["version"] for r in payload["releases"]] == ["4.7.2"]

    def test_releases_latest_endpoint_fetches_list_and_filters(
        self, test_client
    ) -> None:
        """#341: endpoint fetches the release list, filters drafts/prereleases."""
        from rootcoz import main

        github_releases = [
            _gh_release("v4.7.2", "v4.7.2", "notes 4.7.2"),
            {**_gh_release("v4.7.1-rc1", "v4.7.1-rc1"), "prerelease": True},
            {**_gh_release("v4.7.0-draft", "draft"), "draft": True},
            _gh_release("v4.7.1", "v4.7.1", "notes 4.7.1"),
        ]
        mock_response = httpx.Response(
            200,
            json=github_releases,
            request=httpx.Request("GET", main._RELEASES_LIST_URL),
        )
        with (
            patch.dict(main._release_cache, {}, clear=True),
            patch.object(main, "_get_app_version", return_value="4.7.0"),
            patch("rootcoz.main.httpx.AsyncClient") as MockClient,
        ):
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.return_value = mock_response

            response = test_client.get("/api/releases/latest?since=4.7.0")

        assert response.status_code == 200
        data = response.json()
        assert [r["version"] for r in data["releases"]] == ["4.7.1", "4.7.2"]
        assert data["version"] == "4.7.2"
        # List endpoint with paging, not the single-release endpoint
        call_args = mock_client_instance.get.call_args
        assert call_args.args[0] == main._RELEASES_LIST_URL
        assert call_args.kwargs["params"]["per_page"] == main._RELEASES_PER_PAGE

    def test_releases_latest_endpoint_walks_prerelease_pages(self, test_client) -> None:
        """A page full of prereleases must not starve the stable list (#341)."""
        from rootcoz import main

        prerelease_page = [
            {**_gh_release(f"v4.7.{i}-rc1"), "prerelease": True}
            for i in range(main._RELEASES_PER_PAGE)
        ]
        stable_page = [
            _gh_release("v4.7.2", "v4.7.2", "notes 4.7.2"),
            _gh_release("v4.7.1", "v4.7.1", "notes 4.7.1"),
        ]
        responses = [
            httpx.Response(
                200,
                json=prerelease_page,
                request=httpx.Request("GET", main._RELEASES_LIST_URL),
            ),
            httpx.Response(
                200,
                json=stable_page,
                request=httpx.Request("GET", main._RELEASES_LIST_URL),
            ),
        ]
        with (
            patch.dict(main._release_cache, {}, clear=True),
            patch.object(main, "_get_app_version", return_value="4.7.0"),
            patch("rootcoz.main.httpx.AsyncClient") as MockClient,
        ):
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.side_effect = responses

            response = test_client.get("/api/releases/latest?since=4.7.0")

        assert response.status_code == 200
        data = response.json()
        assert [r["version"] for r in data["releases"]] == ["4.7.1", "4.7.2"]
        assert data["version"] == "4.7.2"
        # Page 2 was fetched after page 1 yielded no eligible releases
        assert mock_client_instance.get.call_count == 2
        assert mock_client_instance.get.call_args_list[1].kwargs["params"]["page"] == 2

    def test_releases_latest_endpoint_serves_cache_per_since(self, test_client) -> None:
        """Cached list is re-filtered per request without refetching."""
        from rootcoz import main

        github_releases = [
            _gh_release("v4.7.2"),
            _gh_release("v4.7.1"),
            _gh_release("v4.7.0"),
        ]
        mock_response = httpx.Response(
            200,
            json=github_releases,
            request=httpx.Request("GET", main._RELEASES_LIST_URL),
        )
        with (
            patch.dict(main._release_cache, {}, clear=True),
            patch.object(main, "_get_app_version", return_value="4.7.2"),
            patch("rootcoz.main.httpx.AsyncClient") as MockClient,
        ):
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.return_value = mock_response

            first = test_client.get("/api/releases/latest").json()
            second = test_client.get("/api/releases/latest?since=4.7.0").json()

        mock_client_instance.get.assert_called_once()
        assert [r["version"] for r in first["releases"]] == []
        assert [r["version"] for r in second["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_latest_endpoint_unreachable_serves_cached_list(
        self, test_client
    ) -> None:
        """Expired cache + GitHub failure: stale cached list is served."""
        from rootcoz import main

        with (
            patch.dict(
                main._release_cache,
                {
                    "releases": [_gh_release("v4.7.2"), _gh_release("v4.7.1")],
                    "fetched_at": 0,
                },
                clear=True,
            ),
            patch.object(main, "_get_app_version", return_value="4.7.0"),
            patch("rootcoz.main.httpx.AsyncClient") as MockClient,
        ):
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.side_effect = httpx.ConnectError("boom")

            response = test_client.get("/api/releases/latest?since=4.7.0")

        assert response.status_code == 200
        data = response.json()
        assert [r["version"] for r in data["releases"]] == ["4.7.1", "4.7.2"]

    def test_releases_latest_endpoint_unreachable_cold_empty_payload(
        self, test_client
    ) -> None:
        """No cache + GitHub failure: empty payload with releases=[]."""
        from rootcoz import main

        with (
            patch.dict(main._release_cache, {}, clear=True),
            patch("rootcoz.main.httpx.AsyncClient") as MockClient,
        ):
            mock_client_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.get.side_effect = httpx.ConnectError("boom")

            response = test_client.get("/api/releases/latest")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == ""
        assert data["releases"] == []

    @pytest.mark.parametrize(
        ("newer", "older"),
        [
            ("4.7.0", "4.6.9"),
            ("4.10.0", "4.9.0"),
            ("5.0.0", "4.99.99"),
            ("4.7.0", "4.7.0-rc1"),
            ("4.7.0", "4.7.0.dev1"),
            ("4.7.10", "4.7.9"),
        ],
    )
    def test_is_newer_release_orders_versions(self, newer: str, older: str) -> None:
        from rootcoz.main import _is_newer_release

        assert _is_newer_release(newer, older) is True
        assert _is_newer_release(older, newer) is False

    @pytest.mark.parametrize(
        "version",
        ["4.7.0", "4.7", "v4.7.0", "4.7.0-dev1", "4.7.0+build.5", "0.0.1"],
    )
    def test_parse_version_accepts_valid_versions(self, version: str) -> None:
        from rootcoz.main import _parse_version

        assert _parse_version(version) is not None

    @pytest.mark.parametrize("version", ["", "banana", "unknown", "v"])
    def test_parse_version_rejects_unparseable(self, version: str) -> None:
        from rootcoz.main import _is_newer_release, _parse_version

        assert _parse_version(version) is None
        assert _is_newer_release(version, "4.7.0") is None
        assert _is_newer_release("4.7.0", version) is None


class TestAnalyzeEndpoint:
    """Tests for the /analyze endpoint."""

    def test_analyze_async_returns_queued(self, test_client) -> None:
        """Test that async analyze returns queued status."""
        with patch("rootcoz.main._process_ci_source_analysis"):
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

    def test_a_refused_initial_write_starts_no_analysis(self, test_client) -> None:
        """#419: nothing is started for a job whose initial state was not written.

        The initial write is refused when the row holds a payload this build
        cannot decode, and the enqueue must not spawn the analysis then: no write
        of this build could record the status it starts with.
        """
        with (
            patch(
                "rootcoz.main.save_result",
                side_effect=storage.UnsupportedResultEncoding("newer build"),
            ),
            patch("rootcoz.main._process_ci_source_analysis") as process,
            pytest.raises(storage.UnsupportedResultEncoding),
        ):
            test_client.post(
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
        process.assert_not_called()

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

    def test_analyze_account_id_shaped_cloud_id_rejected(self, test_client) -> None:
        """Account-ID-shaped (org:uuid) cloud IDs are rejected with a 422.

        The shape is a frequent mistake (account ID pasted as cloud ID) —
        without the check the analysis later fails with a confusing
        HTTP 404 from the api.atlassian.com gateway (#365).
        """
        response = test_client.post(
            "/analyze",
            json={
                "type": "jenkins",
                "job_name": "test",
                "build_number": 123,
                "tests_repo_url": "https://github.com/example/repo",
                "atlassian_oauth_cloud_id": (
                    "712020:f0a60b32-1a2b-3c4d-5e6f-789012345678"
                ),
            },
        )
        assert response.status_code == 422
        detail = str(response.json())
        assert "atlassian_oauth_cloud_id" in detail
        assert "account ID" in detail
        assert "tenant_info" in detail

    def test_analyze_plain_uuid_cloud_id_accepted(self, test_client) -> None:
        """A plain-UUID cloud ID passes request validation."""
        with patch("rootcoz.main._process_ci_source_analysis"):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test",
                    "build_number": 123,
                    "tests_repo_url": "https://github.com/example/repo",
                    "ai_provider": "claude",
                    "ai_model": "test-model",
                    "atlassian_oauth_cloud_id": (
                        "2b9e35e3-6bd3-4cec-b838-f4249ee02432"
                    ),
                },
            )
            assert response.status_code == 202

    def test_analyze_accepts_tests_repo_url_with_ref(self, test_client) -> None:
        """Test that tests_repo_url with ':ref' suffix is accepted (no URL validation).

        AI may be deferred to .rootcoz/settings.json when a tests repo is set,
        so missing server AI no longer returns 400 at submit time.
        """
        response = test_client.post(
            "/analyze",
            json={
                "type": "jenkins",
                "job_name": "test",
                "build_number": 123,
                "tests_repo_url": "https://github.com/org/repo:develop",
            },
        )
        # Queued (AI deferred) or 400 only if Jenkins/other validation fails first.
        # Must not be 422 from URL validation.
        assert response.status_code in (202, 400)
        if response.status_code == 400:
            assert "URL" not in response.json()["detail"]
        else:
            assert "job_id" in response.json()

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
        # mock_settings has no AI_PROVIDER/AI_MODEL, so settings.ai_provider is empty
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
        with patch("rootcoz.main._process_ci_source_analysis"):
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
                    patch("rootcoz.main._process_ci_source_analysis"),
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
                    patch("rootcoz.main._process_ci_source_analysis"),
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
        with patch("rootcoz.main._process_ci_source_analysis"):
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
        with patch("rootcoz.main._process_ci_source_analysis"):
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

    Patches ``_process_ci_source_analysis`` with an ``AsyncMock``, sends the
    *payload* to ``POST /analyze``, asserts the response is 202 with
    ``status == "queued"``, and returns the parsed JSON **and** the mock so
    callers can inspect call args when needed.
    """
    with patch(
        "rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock
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
        # mock_settings has no AI_PROVIDER/AI_MODEL
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
        # mock_settings has no AI_PROVIDER/AI_MODEL
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
            "rootcoz.main._process_ci_source_analysis",
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


class TestProwIdentityStamping:
    """Tests for ProwSource identity helpers."""

    def test_valid_job_and_build(self):
        from rootcoz.sources.prow_source import ProwSource

        result = ProwSource.identity_fields("my-prow-job", "42")
        assert result == {
            "job_name": "my-prow-job",
            "build_id": "42",
            "build_number": 42,
        }

    def test_oversized_build_id_omits_build_number(self):
        """Build IDs exceeding JS MAX_SAFE_INTEGER must not set build_number."""
        from rootcoz.sources.prow_source import ProwSource

        result = ProwSource.identity_fields("my-prow-job", "1234567890123456789")
        assert result == {
            "job_name": "my-prow-job",
            "build_id": "1234567890123456789",
        }
        assert "build_number" not in result

    def test_non_numeric_build_id_excluded(self):
        from rootcoz.sources.prow_source import ProwSource

        result = ProwSource.identity_fields("my-prow-job", "abc-not-numeric")
        assert result == {
            "job_name": "my-prow-job",
            "build_id": "abc-not-numeric",
        }
        assert "build_number" not in result

    def test_empty_build_id_excluded(self):
        from rootcoz.sources.prow_source import ProwSource

        result = ProwSource.identity_fields("my-prow-job", "")
        assert result == {"job_name": "my-prow-job"}

    def test_empty_job_name_excluded(self):
        from rootcoz.sources.prow_source import ProwSource

        result = ProwSource.identity_fields("", "123")
        assert "job_name" not in result

    def test_pre_persist_identity_matches_identity_fields(self):
        from rootcoz.sources.prow_source import ProwSource

        assert ProwSource.pre_persist_identity(
            "job", "42"
        ) == ProwSource.identity_fields("job", "42")

    def test_identity_dict_delegates_to_shared(self):
        """ProwSource._identity_dict() uses the same shared helper."""
        from rootcoz.sources.prow_source import ProwSource

        source = ProwSource(
            job_name="my-prow-job",
            build_id="42",
            gcs_bucket="test-bucket",
            prow_url="https://prow.example.com",
        )
        identity = source._identity_dict()
        assert identity["job_name"] == "my-prow-job"
        assert identity["build_number"] == 42


class TestAnalyzeProwEndpoint:
    """Tests for the unified POST /analyze endpoint with type=prow."""

    def test_prow_success(self, test_client) -> None:
        """Test that valid prow request returns 202 (queued)."""
        data, _mock = _post_analyze_queued(
            test_client,
            {
                "type": "prow",
                "prow_job_name": "periodic-ci-e2e-aws",
                "build_id": "1234567890",
                "prow_url": "https://prow.ci.openshift.org",
                "gcs_bucket": "test-platform-results",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert "job_id" in data
        assert data["result_url"].startswith("/results/")

    def test_prow_missing_prow_url(self, test_client) -> None:
        """Test that missing prow_url returns 422 when no server default."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "prow",
                "prow_job_name": "my-job",
                "build_id": "1",
                "gcs_bucket": "some-bucket",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422
        assert "prow_url" in response.json()["detail"]

    def test_prow_missing_gcs_bucket(self, test_client) -> None:
        """Test that missing gcs_bucket returns 422 when no server default."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "prow",
                "prow_job_name": "my-job",
                "build_id": "1",
                "prow_url": "https://prow.ci.openshift.org",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422
        assert "gcs_bucket" in response.json()["detail"]

    def test_prow_with_custom_url_and_bucket(self, test_client) -> None:
        """Test that prow_url and gcs_bucket are accepted."""
        data, _mock = _post_analyze_queued(
            test_client,
            {
                "type": "prow",
                "prow_job_name": "my-job",
                "build_id": "42",
                "prow_url": "https://prow.custom.org",
                "gcs_bucket": "custom-bucket",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert "job_id" in data

    def test_prow_missing_job_name(self, test_client) -> None:
        """Test that missing prow_job_name returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "prow",
                "build_id": "123",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    def test_prow_missing_build_id(self, test_client) -> None:
        """Test that missing build_id returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "prow",
                "prow_job_name": "my-job",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    def test_prow_invalid_build_id(self, test_client) -> None:
        """Test that non-numeric build_id returns 422."""
        response = test_client.post(
            "/analyze",
            json={
                "type": "prow",
                "prow_job_name": "my-job",
                "build_id": "not-a-number",
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        assert response.status_code == 422

    async def test_prow_gcs_errors_produce_failed_not_completed(
        self, temp_db_path: Path, monkeypatch
    ) -> None:
        """When all GCS fetches error, the result must be 'failed', not 'completed'."""
        monkeypatch.setattr(
            storage, "can_user_use_server_providers", AsyncMock(return_value=True)
        )
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        body = UnifiedAnalyzeRequest(
            type="prow",
            prow_job_name="my-prow-job",
            build_id="99",
            prow_url="https://prow.example.com",
            gcs_bucket="test-bucket",
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = Settings(
            prow_url="https://prow.example.com",
            gcs_bucket="test-bucket",
        )
        job_id = "prow-gcs-error-test"

        gcs_error_result = CISourceResult(
            failures=[],
            console_context="",
            skip_analysis=False,
            build_url="https://prow.example.com/view/gs/test-bucket/logs/my-prow-job/99",
            warnings=[
                "GCS junit-listing returned 403: https://storage.googleapis.com/storage/v1/b/test-bucket/o"
            ],
        )

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.sources.prow_source.ProwSource.fetch",
                new_callable=AsyncMock,
                return_value=gcs_error_result,
            ),
            _patch_preflight(),
        ):
            await storage.init_db()
            await storage.save_result(job_id, "", "pending", {})

            await _process_ci_source_analysis(
                job_id=job_id,
                body=body,
                merged=merged,
                display_name="my-prow-job-99",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
                username="admin",
            )

            row = await storage.get_result(job_id)
            assert row is not None
            assert row["status"] == "failed"
            import json

            result = (
                json.loads(row["result"])
                if isinstance(row["result"], str)
                else row["result"]
            )
            assert "Could not fetch test data" in result.get("summary", "")
            assert result.get("source_warnings")

    async def test_prow_build_passed_uses_prow_summary(
        self, temp_db_path: Path
    ) -> None:
        """Successful Prow builds with no failures skip AI analysis."""
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        body = UnifiedAnalyzeRequest(
            type="prow",
            prow_job_name="my-prow-job",
            build_id="99",
            prow_url="https://prow.example.com",
            gcs_bucket="test-bucket",
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = Settings(
            prow_url="https://prow.example.com",
            gcs_bucket="test-bucket",
        )
        job_id = "prow-passed-test"

        passed_result = CISourceResult(
            failures=[],
            skip_analysis=True,
            build_url="https://prow.example.com/view/gs/test-bucket/logs/my-prow-job/99",
            identity={
                "job_name": "my-prow-job",
                "build_id": "99",
                "build_number": "99",
            },
            source_metadata={
                "job_type": "periodic",
                "resolved_gcs_prefix": "logs/my-prow-job/99",
            },
        )

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.sources.prow_source.ProwSource.fetch",
                new_callable=AsyncMock,
                return_value=passed_result,
            ),
            _patch_preflight(),
        ):
            await storage.init_db()
            await storage.save_result(
                job_id,
                "",
                "pending",
                {
                    "request_params": encrypt_sensitive_fields(
                        {
                            "analysis_type": "prow",
                            "prow_job_name": "my-prow-job",
                            "build_id": "99",
                        }
                    )
                },
            )

            await _process_ci_source_analysis(
                job_id=job_id,
                body=body,
                merged=merged,
                display_name="my-prow-job-99",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )

            row = await storage.get_result(job_id, strip_sensitive=False)
            assert row is not None
            assert row["status"] == "completed"
            result = row["result"]
            assert "No test failures found" in result.get("summary", "")
            assert result.get("build_id") == "99"
            assert result.get("source_metadata", {}).get("job_type") == "periodic"
            params = result["request_params"]
            assert params["gcs_prefix"] == "logs/my-prow-job/99"


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
    async def test_abort_lands_when_the_job_started_meanwhile(
        self, test_client, temp_db_path
    ):
        """A waiting->running transition is still a legitimate abort.

        The status the abort was decided from is no longer the row's, but the job
        is still active, so refusing the abort and answering "Job finished as
        running" would report a status the abort never wrote.
        """
        from rootcoz import main

        original = main.update_status

        async def _start_the_job_first(*args, **kwargs):
            # The race the abort has to survive: the job starts after the abort
            # read the status it decides from, before its write lands.
            async with storage._connect_db() as db:
                await db.execute(
                    "UPDATE results SET status = 'running' WHERE job_id = ?",
                    ("job-started",),
                )
                await db.commit()
            return await original(*args, **kwargs)

        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-started",
                jenkins_url="http://jenkins",
                status="waiting",
                result={
                    "job_name": "test",
                    "build_number": 1,
                    "request_params": {"submitted_by": "testuser"},
                },
            )
            with patch.object(main, "update_status", _start_the_job_first):
                resp = test_client.post(
                    "/results/job-started/abort",
                    cookies={"rootcoz_username": "testuser"},
                )

            assert resp.status_code == 200
            assert resp.json()["status"] == "aborted"
            stored = await storage.get_result("job-started")
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

    @pytest.mark.asyncio
    async def test_abort_with_undecodable_payload_still_aborts(
        self, test_client, temp_db_path: Path, caplog
    ) -> None:
        """An abort whose payload this build cannot decode still ends the job.

        The task is already cancelled, so a refused payload write must not leave
        an 'active' row with nothing left to finish it: the status transition is
        the abort, the payload rewrite is not. The newer build's bytes stay
        untouched and the refused write logs once, at WARNING -- the abort
        succeeded, so nothing about it is an error.
        """
        from rootcoz import main

        class _FakeTask:
            """A tracked background task: records the cancel, then ends."""

            cancelled = False

            def done(self) -> bool:
                return self.cancelled

            def cancel(self) -> None:
                self.cancelled = True

            def __await__(self):
                async def _end() -> None:
                    return None

                return _end().__await__()

        blob = b"\xff\xfe\x00a payload from a newer build"
        task = _FakeTask()
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO results (job_id, status, result_json)"
                    " VALUES (?, 'running', ?)",
                    ("job-newer-payload", blob),
                )
                await db.commit()
            main._job_tasks["job-newer-payload"] = task
            logging.getLogger("rootcoz.main").propagate = True
            # simple_logger sets propagate=False on the storage logger too.
            storage.logger.setLevel(logging.DEBUG)
            storage.logger.addHandler(caplog.handler)
            try:
                with caplog.at_level(logging.WARNING, logger="rootcoz.main"):
                    resp = test_client.post(
                        "/results/job-newer-payload/abort",
                        cookies={"rootcoz_username": "testuser"},
                    )
            finally:
                storage.logger.removeHandler(caplog.handler)
                logging.getLogger("rootcoz.main").propagate = False
                main._job_tasks.pop("job-newer-payload", None)

            # The caller gets the abort it asked for...
            assert resp.status_code == 200
            assert resp.json()["status"] == "aborted"
            # ...no task is left to finish it...
            assert task.cancelled
            # ...and the row is terminal, not still active.
            stored = await storage.get_result("job-newer-payload")
            assert stored["status"] == "aborted"
            async with storage._connect_db() as db:
                row = await (
                    await db.execute(
                        "SELECT result_json FROM results WHERE job_id = ?",
                        ("job-newer-payload",),
                    )
                ).fetchone()
            assert row["result_json"] == blob, "the refused payload rewrite wrote"

        # One message, once, and not at ERROR: the abort itself completed.
        named = [
            record
            for record in caplog.records
            if "job-newer-payload" in record.getMessage()
        ]
        assert not [r for r in named if r.levelno >= logging.ERROR], [
            r.getMessage() for r in named if r.levelno >= logging.ERROR
        ]
        warnings = [r.getMessage() for r in named if r.levelname == "WARNING"]
        assert len(warnings) == 1, warnings
        assert "cannot decode" in warnings[0]
        assert "nothing was written over it" in warnings[0]

    @pytest.mark.asyncio
    async def test_abort_cancels_the_job_task(self, test_client, temp_db_path: Path):
        """A normal abort still cancels the tracked task and forgets it."""
        from rootcoz import main

        cancelled: list[bool] = []

        class _FakeTask:
            cancelled = False

            def done(self) -> bool:
                return self.cancelled

            def cancel(self) -> None:
                self.cancelled = True
                cancelled.append(True)

            def __await__(self):
                async def _end() -> None:
                    return None

                return _end().__await__()

        task = _FakeTask()
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-abort-task",
                jenkins_url="http://jenkins",
                status="running",
                result={
                    "job_name": "test",
                    "build_number": 1,
                    "request_params": {"submitted_by": "testuser"},
                },
            )
            main._job_tasks["job-abort-task"] = task
            try:
                resp = test_client.post(
                    "/results/job-abort-task/abort",
                    cookies={"rootcoz_username": "testuser"},
                )
                assert resp.status_code == 200
                assert cancelled == [True], "the abort did not cancel the job's task"
                assert "job-abort-task" not in main._job_tasks
            finally:
                main._job_tasks.pop("job-abort-task", None)

            stored = await storage.get_result("job-abort-task")
            assert stored["status"] == "aborted"


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

    @pytest.mark.asyncio
    async def test_shutdown_cancels_and_awaits_tracked_background_tasks(
        self, mock_settings, temp_db_path: Path, monkeypatch
    ) -> None:
        """A tracked task still running at shutdown is cancelled, not leaked.

        A task that outlives the app keeps reading the module-global DB_PATH and
        races whatever the next app (or test) does with that file (#409). Driven
        through the lifespan directly: TestClient ends its loop on exit, which
        cancels everything anyway and would hide the leak.
        """
        from rootcoz import main

        started = asyncio.Event()

        async def _stuck() -> None:
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(main, "_compress_stored_results", _stuck)
        tracked: list[asyncio.Task[Any]] = []
        try:
            with patch.object(storage, "DB_PATH", temp_db_path):
                async with main.lifespan(main.app):
                    await asyncio.wait_for(started.wait(), 5)
                    tracked = [t for t in main._background_tasks if not t.done()]
                    assert tracked
                assert not main._background_tasks
            assert all(t.cancelled() for t in tracked)
        finally:
            for task in list(main._background_tasks):
                task.cancel()
            await asyncio.gather(*main._background_tasks, return_exceptions=True)
            main._background_tasks.clear()

    @pytest.mark.asyncio
    async def test_a_backfill_writes_the_database_it_started_on(
        self, mock_settings, tmp_path: Path, monkeypatch
    ) -> None:
        """A DB_PATH rebind mid-flight cannot redirect a running background task.

        The shutdown drain only closes this window at shutdown: while the app is
        up, a task that re-reads the module-global DB_PATH per connection follows
        a rebind (a test's monkeypatch, a reload) into another file, and the drain
        then awaits a write nobody asked about (#409). The sweep must finish the
        database it started on and never touch the new one.
        """
        from rootcoz import main

        started_on = tmp_path / "started-on.db"
        moved_to = tmp_path / "moved-to.db"
        for path in (started_on, moved_to):
            with patch.object(storage, "DB_PATH", path):
                await storage.init_db()
        # A row the way a pre-compression build wrote it: inline TEXT, columns
        # underived, so both passes of the sweep have work to do.
        with patch.object(storage, "DB_PATH", started_on):
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO results (job_id, status, result_json)"
                    " VALUES ('job-legacy', 'completed', ?)",
                    (json.dumps({"summary": "legacy", "details": "x" * 4000}),),
                )
                await db.commit()

        real_sweep = main._compress_stored_results
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def _gated_sweep() -> None:
            """Hold the sweep at its start so the global can move underneath it."""
            started.set()
            await release.wait()
            try:
                await real_sweep()
            finally:
                finished.set()

        monkeypatch.setattr(main, "_compress_stored_results", _gated_sweep)
        try:
            with patch.object(storage, "DB_PATH", started_on):
                async with main.lifespan(main.app):
                    await asyncio.wait_for(started.wait(), 5)
                    monkeypatch.setattr(storage, "DB_PATH", moved_to)
                    release.set()
                    await asyncio.wait_for(finished.wait(), 10)
        finally:
            release.set()
            for task in list(main._background_tasks):
                task.cancel()
            await asyncio.gather(*main._background_tasks, return_exceptions=True)
            main._background_tasks.clear()

        with patch.object(storage, "DB_PATH", started_on):
            async with storage._connect_db() as db:
                row = await (
                    await db.execute(
                        "SELECT typeof(result_json) AS kind FROM results"
                        " WHERE job_id = 'job-legacy'"
                    )
                ).fetchone()
        assert row["kind"] == "blob", (
            "the sweep followed DB_PATH away from the database it started on"
        )
        with patch.object(storage, "DB_PATH", moved_to):
            async with storage._connect_db() as db:
                written = (
                    await (await db.execute("SELECT COUNT(*) FROM results")).fetchone()
                )[0]
        assert written == 0, "the sweep wrote into the database it was moved to"


class TestBackgroundTaskDatabasePin:
    """A background task writes the database it was spawned on (#409)."""

    async def test_a_grandchild_keeps_the_pin_its_parent_was_spawned_with(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A child spawned inside a pinned task stays on the parent's database.

        ``_spawn_background`` pins the database into the context it spawns from. A
        task that spawns its own child does that while already pinned, so the pin
        must be kept rather than re-read from the module global: a ``DB_PATH``
        rebind while the parent sleeps would otherwise move the grandchild into
        another file (#409).
        """
        from rootcoz import main

        started_on = tmp_path / "started-on.db"
        moved_to = tmp_path / "moved-to.db"
        for path in (started_on, moved_to):
            with patch.object(storage, "DB_PATH", path):
                await storage.init_db()

        spawned = asyncio.Event()
        release = asyncio.Event()

        async def _grandchild() -> None:
            async with storage._connect_db() as db:
                await db.execute(
                    "INSERT INTO results (job_id, status)"
                    " VALUES ('grandchild', 'completed')"
                )
                await db.commit()

        async def _parent() -> None:
            """Pinned when spawned; spawns its own child after the rebind."""
            spawned.set()
            await release.wait()
            await main._spawn_background(_grandchild())

        try:
            with patch.object(storage, "DB_PATH", started_on):
                parent = main._spawn_background(_parent())
                await asyncio.wait_for(spawned.wait(), 5)
                monkeypatch.setattr(storage, "DB_PATH", moved_to)
                release.set()
                await asyncio.wait_for(parent, 10)
        finally:
            release.set()
            for task in list(main._background_tasks):
                task.cancel()
            await asyncio.gather(*main._background_tasks, return_exceptions=True)
            main._background_tasks.clear()

        for path, expected in ((started_on, 1), (moved_to, 0)):
            with patch.object(storage, "DB_PATH", path):
                async with storage._connect_db() as db:
                    found = (
                        await (
                            await db.execute(
                                "SELECT COUNT(*) FROM results"
                                " WHERE job_id = 'grandchild'"
                            )
                        ).fetchone()
                    )[0]
            assert found == expected, (
                f"the grandchild wrote {path.name}, not the database its pinned "
                "parent was spawned on"
            )


class TestOpenAPISchema:
    """Tests for OpenAPI schema."""

    def test_openapi_schema_available(self, test_client) -> None:
        """Test that OpenAPI schema is available."""
        response = test_client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "rootcoz"
        assert schema["info"]["version"] == "0.1.0"
        ops = [
            op["operationId"]
            for path_item in schema["paths"].values()
            for op in path_item.values()
            if isinstance(op, dict) and "operationId" in op
        ]
        assert len(ops) == len(set(ops))
        assert all(not (o.endswith(("_get", "_post", "_put"))) for o in ops)

    def test_enrich_comments_operation_id(self, test_client) -> None:
        """The enrichment route pins its operationId for generated clients."""
        schema = test_client.get("/openapi.json").json()
        route = schema["paths"]["/results/{job_id}/enrich-comments"]
        assert route["get"]["operationId"] == "enrichComments"

    def test_docs_available(self, test_client) -> None:
        """Test that docs endpoint is available."""
        response = test_client.get("/docs")
        assert response.status_code == 200

    def test_redoc_available(self, test_client) -> None:
        """Test that redoc endpoint is available."""
        response = test_client.get("/redoc")
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
            mock_list.assert_called_once_with(limit=500, offset=0)

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
        data = response.json()
        names = {j["job_name"] for j in data["jobs"]}
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
        data = response.json()
        names = {j["job_name"] for j in data["jobs"]}
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
        data = response.json()
        names = {j["job_name"] for j in data["jobs"]}
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
        data = response.json()
        names = {j["job_name"] for j in data["jobs"]}
        assert names == {"job-a", "job-b", "job-c"}


async def _seed_submitter_results(db_path: Path, rows: list[tuple[str, str]]) -> None:
    """Seed results rows carrying request_params.submitted_by."""
    with patch.object(storage, "DB_PATH", db_path):
        for job_id, submitted_by in rows:
            await storage.save_result(
                job_id,
                f"https://jenkins.example.com/job/{job_id}/1",
                "completed",
                {
                    "job_name": job_id,
                    "build_number": 1,
                    "failures": [],
                    "request_params": {"submitted_by": submitted_by},
                },
            )


class TestDashboardSubmittersEndpoint:
    """Tests for GET /api/dashboard/submitters and submitted_by filtering (#390)."""

    def test_lists_sorted_deduped_names(self, test_client, temp_db_path: Path) -> None:
        """Yuli/yuli collapse to one option; names come back NOCASE-sorted."""
        asyncio.run(
            _seed_submitter_results(
                temp_db_path,
                [("job-a", "Yuli"), ("job-b", "yuli"), ("job-c", "avi")],
            )
        )
        with patch.object(storage, "DB_PATH", temp_db_path):
            resp = test_client.get("/api/dashboard/submitters")
        assert resp.status_code == 200
        assert resp.json()["submitters"] == ["avi", "Yuli"]

    def test_requires_auth(self, mock_settings, temp_db_path: Path) -> None:
        """The enumeration is not a public path."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            from starlette.testclient import TestClient

            from rootcoz.main import app

            with TestClient(app) as client:
                assert client.get("/api/dashboard/submitters").status_code == 401

    def test_viewer_can_list_submitters(self, test_client, temp_db_path: Path) -> None:
        """A viewer already sees submitted_by on every row, so 200 is fine."""
        asyncio.run(_seed_submitter_results(temp_db_path, [("job-a", "yuval")]))
        created = test_client.post(
            "/api/admin/users/create",
            json={"username": "submitter_viewer", "role": "viewer"},
        )
        assert created.status_code == 200
        login = test_client.post(
            "/api/auth/login",
            json={
                "username": "submitter_viewer",
                "api_key": created.json()["api_key"],
            },
        )
        assert login.status_code == 200
        with patch.object(storage, "DB_PATH", temp_db_path):
            resp = test_client.get(
                "/api/dashboard/submitters", cookies=dict(login.cookies)
            )
        assert resp.status_code == 200
        assert resp.json()["submitters"] == ["yuval"]

    def test_filtered_endpoint_filters_by_submitter(
        self, test_client, temp_db_path: Path
    ) -> None:
        """?submitted_by narrows the filtered endpoint to matching rows."""
        asyncio.run(
            _seed_submitter_results(
                temp_db_path, [("job-yuval", "yuval"), ("job-dana", "dana")]
            )
        )
        with patch.object(storage, "DB_PATH", temp_db_path):
            resp = test_client.get(
                "/api/dashboard/filtered", params={"submitted_by": "yuval"}
            )
        assert resp.status_code == 200
        data = resp.json()
        assert {j["job_name"] for j in data["jobs"]} == {"job-yuval"}
        assert data["total"] == 1

    @pytest.mark.parametrize(
        "params", [None, {"submitted_by": ""}, {"submitted_by": ["", ""]}]
    )
    def test_filtered_endpoint_empty_submitter_is_no_filter(
        self, test_client, temp_db_path: Path, params
    ) -> None:
        """An empty submitted_by query value means unfiltered."""
        asyncio.run(
            _seed_submitter_results(
                temp_db_path, [("job-yuval", "yuval"), ("job-dana", "dana")]
            )
        )
        with patch.object(storage, "DB_PATH", temp_db_path):
            resp = test_client.get("/api/dashboard/filtered", params=params)
        assert resp.status_code == 200
        data = resp.json()
        assert {j["job_name"] for j in data["jobs"]} == {"job-yuval", "job-dana"}
        assert data["total"] == 2

    def test_filtered_endpoint_rejects_too_many_submitters(self, test_client) -> None:
        """More than 50 submitted_by values is a 400."""
        values = [f"user{i}" for i in range(51)]
        resp = test_client.get(
            "/api/dashboard/filtered", params={"submitted_by": values}
        )
        assert resp.status_code == 400
        assert "50" in resp.json()["detail"]

    def test_filtered_endpoint_rejects_too_long_submitter(self, test_client) -> None:
        """A submitted_by value longer than 64 characters is a 400."""
        resp = test_client.get(
            "/api/dashboard/filtered", params={"submitted_by": "x" * 65}
        )
        assert resp.status_code == 400
        assert "64" in resp.json()["detail"]


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
    async def test_set_reviewed_viewer_forbidden(self, test_client):
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
            "job-rev-viewer", "http://jenkins", "completed", result_data
        )
        _, viewer_key = await storage.create_user("test-viewer", role="viewer")
        response = test_client.put(
            "/results/job-rev-viewer/reviewed",
            json={"test_name": "test_foo", "reviewed": True},
            headers={"Authorization": f"Bearer {viewer_key}"},
        )
        assert response.status_code == 403
        data = test_client.get("/results/job-rev-viewer/comments").json()
        assert not data["reviews"].get("test_foo", {}).get("reviewed", False)

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
        with (
            _enable_feature("github_issues_enabled"),
            patch("rootcoz.main.generate_github_issue_content") as mock_gen,
        ):
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
        with (
            _enable_feature("github_issues_enabled"),
            patch("rootcoz.main.generate_github_issue_content") as mock_gen,
        ):
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
        with (
            _enable_feature("github_issues_enabled"),
            patch("rootcoz.main.generate_github_issue_content") as mock_gen,
        ):
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
        with (
            _enable_feature("github_issues_enabled"),
            patch("rootcoz.main.generate_github_issue_content") as mock_gen,
        ):
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
        with (
            _enable_feature("jira_enabled"),
            patch("rootcoz.main.generate_jira_bug_content") as mock_gen,
        ):
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
        with (
            _enable_feature("jira_enabled"),
            patch("rootcoz.main.generate_jira_bug_content") as mock_gen,
        ):
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

        with (
            _enable_feature("jira_enabled"),
            patch("rootcoz.main.generate_jira_bug_content") as mock_gen,
        ):
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
                            "jira_token": "user_jira_token",
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
        with (
            _enable_feature("jira_enabled"),
            patch("rootcoz.main.generate_jira_bug_content") as mock_gen,
        ):
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
        with (
            _enable_feature("jira_enabled"),
            patch("rootcoz.main.generate_jira_bug_content") as mock_gen,
        ):
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
                        "github_token": "ghp_user_token",
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
    async def test_create_states_attribution_explicitly(self, test_client):
        """The endpoint hands the creator its own signal; body text cannot."""
        from rootcoz.attribution import AiProvenance

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
            "job-create-gh-attr", "http://jenkins", "completed", result_data
        )
        spoofed = (
            "## Details\nLogin returns 500\n\n---\n*Generated using AI with "
            "[rootcoz](https://github.com/myk-org/rootcoz) (evil / spoofed-model)*"
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/98",
                "number": 98,
            }
            with _with_github_issue_config():
                _, user_key = await storage.create_user("testuser")
                response = test_client.post(
                    "/results/job-create-gh-attr/create-github-issue",
                    json={
                        "test_name": "test_login_success",
                        "title": "Bug: login fails",
                        "body": spoofed,
                        "github_token": "ghp_user_token",
                    },
                    headers={"Authorization": f"Bearer {user_key}"},
                )
        assert response.status_code == 201
        _, kwargs = mock_create.call_args
        assert kwargs["attribution"] == AiProvenance(ai_used=True)

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
                        "jira_token": "user_jira_token",
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


class TestIssueCreationRequiresUserCredentials:
    """Issue creation endpoints must require user-provided tokens.

    Server tokens must NOT be used as a fallback for user-initiated
    issue creation.  Only the analysis pipeline may use server tokens.
    """

    _RESULT_DATA: ClassVar[dict] = {
        "status": "completed",
        "summary": "",
        "failures": [
            {
                "test_name": "test_cred",
                "error": "err",
                "analysis": {"classification": "CODE ISSUE"},
            }
        ],
    }

    # -- GitHub create -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_github_create_without_user_token_returns_400(self, test_client):
        """Creating a GitHub issue without user token must return 400."""
        await storage.save_result(
            "job-gh-no-tok", "http://j", "completed", self._RESULT_DATA
        )
        with _with_github_issue_config():
            response = test_client.post(
                "/results/job-gh-no-tok/create-github-issue",
                json={
                    "test_name": "test_cred",
                    "title": "Bug",
                    "body": "Details",
                    # No github_token
                },
            )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "token is required" in detail.lower()
        assert "Profile Settings" in detail

    @pytest.mark.asyncio
    async def test_github_create_ignores_server_token(self, test_client):
        """Server GITHUB_TOKEN must not be used for issue creation."""
        await storage.save_result(
            "job-gh-srv-tok", "http://j", "completed", self._RESULT_DATA
        )
        # Server has a token configured but user provides none
        with _with_github_issue_config():
            response = test_client.post(
                "/results/job-gh-srv-tok/create-github-issue",
                json={
                    "test_name": "test_cred",
                    "title": "Bug",
                    "body": "Details",
                    "github_token": "",
                },
            )
        assert response.status_code == 400
        assert "token is required" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_github_create_with_user_token_succeeds(self, test_client):
        """GitHub issue creation works when user provides their own token."""
        await storage.save_result(
            "job-gh-usr-tok", "http://j", "completed", self._RESULT_DATA
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/1",
                "number": 1,
            }
            with _with_github_issue_config():
                response = test_client.post(
                    "/results/job-gh-usr-tok/create-github-issue",
                    json={
                        "test_name": "test_cred",
                        "title": "Bug",
                        "body": "Details",
                        "github_token": "ghp_user_provided",
                    },
                )
        assert response.status_code == 201
        # Verify the user token was passed, not the server token
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["github_token"] == "ghp_user_provided"

    # -- Jira create ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_jira_create_without_user_token_returns_400(self, test_client):
        """Creating a Jira bug without user token must return 400."""
        await storage.save_result(
            "job-jira-no-tok", "http://j", "completed", self._RESULT_DATA
        )
        with _enable_feature("jira_enabled"):
            response = test_client.post(
                "/results/job-jira-no-tok/create-jira-bug",
                json={
                    "test_name": "test_cred",
                    "title": "Bug",
                    "body": "Details",
                    # No jira_token
                },
            )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "token is required" in detail.lower()
        assert "Profile Settings" in detail

    @pytest.mark.asyncio
    async def test_jira_create_ignores_server_token(self, test_client):
        """Server JIRA_API_TOKEN must not be used for issue creation."""
        await storage.save_result(
            "job-jira-srv-tok", "http://j", "completed", self._RESULT_DATA
        )
        with _enable_feature("jira_enabled"):
            response = test_client.post(
                "/results/job-jira-srv-tok/create-jira-bug",
                json={
                    "test_name": "test_cred",
                    "title": "Bug",
                    "body": "Details",
                    "jira_token": "",
                },
            )
        assert response.status_code == 400
        assert "token is required" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_jira_create_with_user_token_succeeds(self, test_client):
        """Jira bug creation works when user provides their own token."""
        await storage.save_result(
            "job-jira-usr-tok", "http://j", "completed", self._RESULT_DATA
        )
        with patch("rootcoz.main.create_jira_bug") as mock_create:
            mock_create.return_value = {
                "key": "PROJ-1",
                "url": "https://jira.example.com/browse/PROJ-1",
            }
            with _enable_feature("jira_enabled"):
                response = test_client.post(
                    "/results/job-jira-usr-tok/create-jira-bug",
                    json={
                        "test_name": "test_cred",
                        "title": "Bug",
                        "body": "Details",
                        "jira_token": "user_jira_pat",
                    },
                )
        assert response.status_code == 201

    # -- GitHub preview ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_github_preview_without_token_skips_duplicates(self, test_client):
        """Preview without user token should succeed but skip duplicate detection."""
        await storage.save_result(
            "job-gh-prev-no-tok", "http://j", "completed", self._RESULT_DATA
        )
        with (
            patch("rootcoz.main.generate_github_issue_content") as mock_gen,
            patch("rootcoz.main.search_github_duplicates") as mock_dup,
            _enable_feature("github_issues_enabled"),
        ):
            mock_gen.return_value = {"title": "Bug", "body": "Body"}
            mock_dup.return_value = [
                {"url": "https://github.com/org/repo/issues/1", "title": "dup"}
            ]
            response = test_client.post(
                "/results/job-gh-prev-no-tok/preview-github-issue",
                json={
                    "test_name": "test_cred",
                    # No github_token
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Bug"
        # Duplicate search should NOT have been called (no user token)
        mock_dup.assert_not_called()
        assert data["similar_issues"] == []

    @pytest.mark.asyncio
    async def test_github_preview_with_token_includes_duplicates(self, test_client):
        """Preview with user token should include duplicate detection."""
        await storage.save_result(
            "job-gh-prev-tok", "http://j", "completed", self._RESULT_DATA
        )
        with (
            patch("rootcoz.main.generate_github_issue_content") as mock_gen,
            patch("rootcoz.main.search_github_duplicates") as mock_dup,
            _enable_feature("github_issues_enabled"),
        ):
            mock_gen.return_value = {"title": "Bug", "body": "Body"}
            mock_dup.return_value = [
                {"url": "https://github.com/org/repo/issues/1", "title": "dup"}
            ]
            response = test_client.post(
                "/results/job-gh-prev-tok/preview-github-issue",
                json={
                    "test_name": "test_cred",
                    "github_token": "ghp_user_token",
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert len(data["similar_issues"]) == 1
        mock_dup.assert_called_once()

    # -- Jira preview --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_jira_preview_without_token_skips_duplicates(self, test_client):
        """Preview without user Jira token should succeed but skip duplicates."""
        await storage.save_result(
            "job-jira-prev-no-tok", "http://j", "completed", self._RESULT_DATA
        )
        with (
            patch("rootcoz.main.generate_jira_bug_content") as mock_gen,
            patch("rootcoz.main.search_jira_duplicates") as mock_dup,
            _enable_feature("jira_enabled"),
        ):
            mock_gen.return_value = {"title": "Bug", "body": "Body"}
            mock_dup.return_value = [
                {
                    "key": "PROJ-1",
                    "url": "https://jira.example.com/browse/PROJ-1",
                    "title": "dup",
                }
            ]
            response = test_client.post(
                "/results/job-jira-prev-no-tok/preview-jira-bug",
                json={
                    "test_name": "test_cred",
                    # No jira_token
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Bug"
        # Duplicate search should NOT have been called (no user token)
        mock_dup.assert_not_called()
        assert data["similar_issues"] == []

    # -- Legacy service-account secrets (bugs must not be filed under
    # the server's Jira identity) --------------------------------------

    @pytest.mark.asyncio
    async def test_jira_create_legacy_sa_token_rejected(self, test_client):
        """A legacy (non-ATOA) SA secret must 400, not use server creds."""
        await storage.save_result(
            "job-jira-legacy-sa", "http://j", "completed", self._RESULT_DATA
        )
        with (
            patch("rootcoz.main.create_jira_bug") as mock_create,
            _enable_feature("jira_enabled"),
        ):
            response = test_client.post(
                "/results/job-jira-legacy-sa/create-jira-bug",
                json={
                    "test_name": "test_cred",
                    "title": "Bug",
                    "body": "Details",
                    "jira_sa_token": "legacy-secret-token",  # pragma: allowlist secret
                },
            )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "not a recognized" in detail
        assert "client secret" in detail
        # The bug must NOT be created with the server's credentials
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_jira_preview_legacy_sa_token_rejected(self, test_client):
        """Preview with a legacy (non-ATOA) SA secret returns a clear 400."""
        await storage.save_result(
            "job-jira-prev-legacy-sa", "http://j", "completed", self._RESULT_DATA
        )
        with _enable_feature("jira_enabled"):
            response = test_client.post(
                "/results/job-jira-prev-legacy-sa/preview-jira-bug",
                json={
                    "test_name": "test_cred",
                    "jira_sa_token": "legacy-secret-token",  # pragma: allowlist secret
                },
            )
        assert response.status_code == 400
        assert "not a recognized" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_jira_create_legacy_sa_token_rejected_with_regular_token(
        self, test_client
    ):
        """A legacy SA secret alongside a regular token still 400s (fail closed)."""
        await storage.save_result(
            "job-jira-legacy-sa-plus-tok", "http://j", "completed", self._RESULT_DATA
        )
        with (
            patch("rootcoz.main.create_jira_bug") as mock_create,
            _enable_feature("jira_enabled"),
        ):
            response = test_client.post(
                "/results/job-jira-legacy-sa-plus-tok/create-jira-bug",
                json={
                    "test_name": "test_cred",
                    "title": "Bug",
                    "body": "Details",
                    "jira_token": "user_jira_pat",
                    "jira_sa_token": "legacy-secret-token",  # pragma: allowlist secret
                },
            )
        assert response.status_code == 400
        assert "not a recognized" in response.json()["detail"]
        # Invalid input is never silently dropped in favor of the regular
        # token — no bug is created
        mock_create.assert_not_called()


class TestJiraMethodConfiguredUsesEffectiveSettings:
    """The method-configured gate must run against EFFECTIVE settings.

    The user's credential pins the auth method — a user token can satisfy
    a method the server's own secret cannot, so checking the SERVER
    settings before building effective settings rejects valid
    user-credential requests.
    """

    _RESULT_DATA: ClassVar[dict] = {
        "status": "completed",
        "summary": "",
        "failures": [
            {
                "test_name": "test_eff",
                "error": "err",
                "analysis": {"classification": "CODE ISSUE"},
            }
        ],
    }

    def _pat_settings_without_server_secret(self) -> Settings:
        """pat method + URL + project key, but NO server jira_pat."""
        return _build_wait_settings(
            jira_auth_mode="pat",
            jira_server_url="https://jira-server.example.com",
            jira_project_key="PROJ",
        )

    @pytest.mark.asyncio
    async def test_create_proceeds_with_user_token_without_server_secret(
        self, test_client
    ):
        """User PAT + server without server secret → create proceeds."""
        from rootcoz.main import app, get_settings

        await storage.save_result(
            "job-jira-eff-create", "http://j", "completed", self._RESULT_DATA
        )
        app.dependency_overrides[get_settings] = (
            self._pat_settings_without_server_secret
        )
        try:
            with patch("rootcoz.main.create_jira_bug") as mock_create:
                mock_create.return_value = {
                    "key": "PROJ-9",
                    "url": "https://jira-server.example.com/browse/PROJ-9",
                }
                response = test_client.post(
                    "/results/job-jira-eff-create/create-jira-bug",
                    json={
                        "test_name": "test_eff",
                        "title": "Bug",
                        "body": "Details",
                        "jira_token": "user_jira_pat",
                    },
                )
        finally:
            app.dependency_overrides.pop(get_settings, None)
        assert response.status_code == 201, response.text
        # Created with the USER's credential, not the (absent) server secret
        call_settings = mock_create.call_args[1]["settings"]
        assert call_settings.jira_pat.get_secret_value() == "user_jira_pat"
        assert call_settings.jira_auth_mode == "pat"

    @pytest.mark.asyncio
    async def test_preview_proceeds_with_user_token_without_server_secret(
        self, test_client
    ):
        """User PAT + server without server secret → preview + duplicates run."""
        from rootcoz.main import app, get_settings

        await storage.save_result(
            "job-jira-eff-prev", "http://j", "completed", self._RESULT_DATA
        )
        app.dependency_overrides[get_settings] = (
            self._pat_settings_without_server_secret
        )
        try:
            with (
                patch("rootcoz.main.generate_jira_bug_content") as mock_gen,
                patch("rootcoz.main.search_jira_duplicates") as mock_dup,
            ):
                mock_gen.return_value = {"title": "Bug", "body": "Body"}
                mock_dup.return_value = [
                    {
                        "key": "PROJ-1",
                        "url": "https://jira-server.example.com/browse/PROJ-1",
                        "title": "dup",
                    }
                ]
                response = test_client.post(
                    "/results/job-jira-eff-prev/preview-jira-bug",
                    json={
                        "test_name": "test_eff",
                        "jira_token": "user_jira_pat",
                    },
                )
        finally:
            app.dependency_overrides.pop(get_settings, None)
        assert response.status_code == 200, response.text
        mock_dup.assert_called_once()
        # Duplicate search ran with the user's effective settings
        assert (
            mock_dup.call_args[1]["settings"].jira_pat.get_secret_value()
            == "user_jira_pat"
        )
        assert response.json()["similar_issues"]

    @pytest.mark.asyncio
    async def test_preview_without_any_credential_returns_400(self, test_client):
        """No user credential and no server secret → 400 (method incomplete)."""
        from rootcoz.main import app, get_settings

        await storage.save_result(
            "job-jira-eff-none", "http://j", "completed", self._RESULT_DATA
        )
        app.dependency_overrides[get_settings] = (
            self._pat_settings_without_server_secret
        )
        try:
            response = test_client.post(
                "/results/job-jira-eff-none/preview-jira-bug",
                json={"test_name": "test_eff"},
            )
        finally:
            app.dependency_overrides.pop(get_settings, None)
        assert response.status_code == 400
        assert "jira_pat" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_without_any_credential_returns_400(self, test_client):
        """No user credential and no server secret → 400 on create too."""
        from rootcoz.main import app, get_settings

        await storage.save_result(
            "job-jira-eff-none-create", "http://j", "completed", self._RESULT_DATA
        )
        app.dependency_overrides[get_settings] = (
            self._pat_settings_without_server_secret
        )
        try:
            with patch("rootcoz.main.create_jira_bug") as mock_create:
                response = test_client.post(
                    "/results/job-jira-eff-none-create/create-jira-bug",
                    json={
                        "test_name": "test_eff",
                        "title": "Bug",
                        "body": "Details",
                    },
                )
        finally:
            app.dependency_overrides.pop(get_settings, None)
        assert response.status_code == 400
        mock_create.assert_not_called()


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

            # Create (need settings with TESTS_REPO_URL)
            with _with_github_issue_config():
                create_resp = test_client.post(
                    "/results/job-integ-gh/create-github-issue",
                    json={
                        "test_name": "test_login_success",
                        "title": "Bug title",
                        "body": "Bug body",
                        "github_token": "ghp_user_token",
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
    """create-github-issue reports the same refusals as the feedback form."""

    @staticmethod
    def _raise_status(status: int, *, json: dict | None = None):
        """Build a create_github_issue side effect raising HTTPStatusError."""
        request = httpx.Request("POST", "https://api.github.com/repos/o/r/issues")
        return httpx.HTTPStatusError(
            f"GitHub returned {status}",
            request=request,
            response=httpx.Response(status, json=json, request=request),
        )

    async def _post_with_github_failure(self, test_client, job_id, exc):
        """Create an issue for a real job while GitHub answers ``exc``."""
        await storage.save_result(
            job_id,
            "http://jenkins",
            "completed",
            {
                "status": "completed",
                "summary": "",
                "failures": [
                    {
                        "test_name": "test_foo",
                        "error": "err",
                        "analysis": {"classification": "CODE ISSUE"},
                    }
                ],
            },
        )
        with (
            patch("rootcoz.main.create_github_issue", side_effect=exc),
            _with_github_issue_config(),
        ):
            return test_client.post(
                f"/results/{job_id}/create-github-issue",
                json={
                    "test_name": "test_foo",
                    "title": "Bug",
                    "body": _REPORT_BODY,
                    "github_token": _REPORT_GITHUB_TOKEN,
                },
            )

    @pytest.mark.asyncio
    async def test_github_5xx_is_a_retryable_outage(self, test_client, caplog):
        """A 5xx is an outage: retryable detail, reason in the log."""
        exc = self._raise_status(
            503,
            json={
                "message": "Server Error",
                "documentation_url": "https://docs.github.com/rest",
            },
        )
        with capture_main_logs(caplog):
            response = await self._post_with_github_failure(
                test_client, "job-gh-503", exc
            )

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail == (
            "GitHub is unavailable right now, so your bug report was not filed. "
            "Try again in a few minutes."
        )
        assert "503" not in detail
        assert "status=503" in caplog.text
        assert "Server Error" in caplog.text
        assert _REPORT_GITHUB_TOKEN not in caplog.text
        assert _REPORT_BODY not in caplog.text
        # This endpoint records no row for a refusal, so nothing there can echo
        # the submitted body either.
        assert (await storage.list_audit_events(action="issue.create"))["events"] == []

    @pytest.mark.asyncio
    async def test_github_404_points_at_the_configured_repository(
        self, test_client, caplog
    ):
        """A report's issue goes to TESTS_REPO_URL, so a 404 is not always the token.

        GitHub hides an unreadable repository behind a 404, but a mistyped or
        deleted one in the deployment setting answers the same way — that is
        not something the user can fix in Profile Settings alone.
        """
        exc = self._raise_status(404, json={"message": "Not Found"})
        with capture_main_logs(caplog):
            response = await self._post_with_github_failure(
                test_client, "job-gh-404", exc
            )

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert "TESTS_REPO_URL" in detail
        assert "cannot see the repository" in detail
        assert "unavailable" not in detail.lower()
        assert "404" not in detail
        assert "status=404" in caplog.text
        assert _REPORT_GITHUB_TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_github_401_still_names_the_token(self, test_client, caplog):
        """An upstream 401 keeps saying the token is bad, never 'unavailable'."""
        with capture_main_logs(caplog):
            response = await self._post_with_github_failure(
                test_client, "job-gh-401", self._raise_status(401)
            )

        assert response.status_code == 403
        assert response.json()["detail"] == (
            "GitHub token is invalid or expired. "
            "Generate a new token in Profile Settings."
        )
        assert "status=401" in caplog.text

    @pytest.mark.asyncio
    async def test_github_403_still_names_the_scope(self, test_client, caplog):
        """A 403 without a rate-limit signal is the missing `repo` scope."""
        exc = self._raise_status(
            403, json={"message": "Resource not accessible by personal access token"}
        )
        with capture_main_logs(caplog):
            response = await self._post_with_github_failure(
                test_client, "job-gh-403", exc
            )

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert "scope" in detail.lower()
        assert "unavailable" not in detail.lower()
        assert "status=403" in caplog.text

    @pytest.mark.asyncio
    async def test_github_429_still_names_the_rate_limit(self, test_client, caplog):
        """429 stays a rate limit, not an outage."""
        with capture_main_logs(caplog):
            response = await self._post_with_github_failure(
                test_client, "job-gh-429", self._raise_status(429)
            )

        assert response.status_code == 429
        assert response.json()["detail"] == (
            "GitHub API rate limit reached. Try again later."
        )
        assert _REPORT_GITHUB_TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_github_message_cannot_forge_a_log_line(self, test_client, caplog):
        """A tracker message is data, never a second log line."""
        exc = self._raise_status(
            503, json={"message": "Server Error\nERROR forged line by GitHub"}
        )
        with capture_main_logs(caplog):
            response = await self._post_with_github_failure(
                test_client, "job-gh-forge", exc
            )

        assert response.status_code == 502
        refused = [
            record.getMessage()
            for record in caplog.records
            if "GitHub refused" in record.getMessage()
        ]
        assert len(refused) == 1
        # The text is kept, the line break that would fabricate an entry is not.
        assert "forged line by GitHub" in refused[0]
        assert "\n" not in refused[0]

    @pytest.mark.asyncio
    async def test_github_api_http_error_returns_502(self, test_client):
        """A status outside the outage classes keeps its generic 502."""
        exc = self._raise_status(422, json={"message": "Validation Failed"})
        response = await self._post_with_github_failure(test_client, "job-gh-err", exc)

        assert response.status_code == 502
        assert response.json()["detail"] == "GitHub API error: 422"

    @pytest.mark.asyncio
    async def test_github_read_timeout_warns_the_issue_may_already_be_filed(
        self, test_client, caplog
    ):
        """A timeout may have filed the issue: check before a duplicate retry."""
        import httpx

        exc = httpx.ReadTimeout(
            "GitHub did not respond",
            request=httpx.Request("POST", "https://api.github.com"),
        )
        with capture_main_logs(caplog):
            response = await self._post_with_github_failure(
                test_client, "job-gh-timeout", exc
            )

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "the request may have been sent" in detail
        assert "may already be filed" in detail
        assert "could create a duplicate" in detail
        assert "bug report" in detail
        assert _REPORT_GITHUB_TOKEN not in caplog.text
        assert _REPORT_BODY not in caplog.text

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
            mock_create.side_effect = httpx.ConnectError(
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
                        "github_token": "ghp_user_token",
                    },
                )
        assert response.status_code == 502
        assert "GitHub API unreachable" in response.json()["detail"]


class TestCreateJiraBugApiErrors:
    """create-jira-bug keeps its own wording, but a 5xx is an outage too."""

    @pytest.mark.asyncio
    async def test_jira_5xx_is_a_retryable_outage(self, test_client, caplog):
        """A Jira 5xx says 'unavailable, retry' instead of a bare status."""
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
        request = httpx.Request("POST", "https://jira.example.com/rest/api/2/issue")
        exc = httpx.HTTPStatusError(
            "Internal Server Error",
            request=request,
            response=httpx.Response(
                503,
                json={"errorMessages": ["Jira is down for maintenance"]},
                request=request,
            ),
        )
        with (
            capture_main_logs(caplog),
            patch("rootcoz.main.create_jira_bug", side_effect=exc),
            _enable_feature("jira_enabled"),
        ):
            response = test_client.post(
                "/results/job-jira-err/create-jira-bug",
                json={
                    "test_name": "test_foo",
                    "title": "Bug",
                    "body": _REPORT_BODY,
                    "jira_token": "user_jira_token",
                },
            )
        assert response.status_code == 502
        assert response.json()["detail"] == (
            "Jira is unavailable right now, so the bug was not filed. "
            "Try again in a few minutes."
        )
        assert "status=503" in caplog.text
        assert "Jira is down for maintenance" in caplog.text
        assert _REPORT_BODY not in caplog.text

    @pytest.mark.asyncio
    async def test_jira_api_http_error_returns_502(self, test_client):
        """A non-outage, non-auth status keeps its generic 502."""
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
            "job-jira-bad-request", "http://jenkins", "completed", result_data
        )
        with patch("rootcoz.main.create_jira_bug") as mock_create:
            mock_create.side_effect = httpx.HTTPStatusError(
                "Bad Request",
                request=httpx.Request("POST", "https://jira.example.com"),
                response=httpx.Response(400),
            )
            with _enable_feature("jira_enabled"):
                response = test_client.post(
                    "/results/job-jira-bad-request/create-jira-bug",
                    json={
                        "test_name": "test_foo",
                        "title": "Bug",
                        "body": "Details",
                        "jira_token": "user_jira_token",
                    },
                )
        assert response.status_code == 502
        assert response.json()["detail"] == "Jira API error: 400"

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
            mock_create.side_effect = httpx.ConnectError(
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
                        "jira_token": "user_jira_token",
                    },
                )
        assert response.status_code == 502
        assert "Jira API unreachable" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_jira_404_is_a_project_access_refusal(self, test_client, caplog):
        """A Jira 404 is a hidden project the token cannot see, not a 502.

        Jira answers 404 (not 403) for a project key the token cannot see, so
        the detail must point at the credential and the log must keep the
        upstream status and Jira's message while leaking neither token nor body.
        """
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
            "job-jira-404", "http://jenkins", "completed", result_data
        )
        request = httpx.Request("POST", "https://jira.example.com/rest/api/2/issue")
        exc = httpx.HTTPStatusError(
            "Not Found",
            request=request,
            response=httpx.Response(
                404,
                json={"errorMessages": ["Project 'MISSING' does not exist"]},
                request=request,
            ),
        )
        with (
            capture_main_logs(caplog),
            patch("rootcoz.main.create_jira_bug", side_effect=exc),
            _enable_feature("jira_enabled"),
        ):
            response = test_client.post(
                "/results/job-jira-404/create-jira-bug",
                json={
                    "test_name": "test_foo",
                    "title": "Bug",
                    "body": _REPORT_BODY,
                    "jira_token": "secret_jira_token",
                },
            )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail == (
            "Jira token cannot access that Jira project. Check the project "
            "key and update your token in settings so it can see the project."
        )
        assert "Jira API error" not in detail
        assert "status=404" in caplog.text
        assert "Project 'MISSING' does not exist" in caplog.text
        assert "secret_jira_token" not in caplog.text
        assert _REPORT_BODY not in caplog.text
        assert "secret_jira_token" not in detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_jira_auth_statuses_stay_specific(
        self, test_client, caplog, status: int
    ):
        """A Jira 401/403 keeps the credential detail it always had."""
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
            f"job-jira-auth-{status}", "http://jenkins", "completed", result_data
        )
        request = httpx.Request("POST", "https://jira.example.com/rest/api/2/issue")
        exc = httpx.HTTPStatusError(
            "Unauthorized",
            request=request,
            response=httpx.Response(status, request=request),
        )
        with (
            capture_main_logs(caplog),
            patch("rootcoz.main.create_jira_bug", side_effect=exc),
            _enable_feature("jira_enabled"),
        ):
            response = test_client.post(
                f"/results/job-jira-auth-{status}/create-jira-bug",
                json={
                    "test_name": "test_foo",
                    "title": "Bug",
                    "body": _REPORT_BODY,
                    "jira_token": "secret_jira_token",
                },
            )
        assert response.status_code == 401
        assert response.json()["detail"] == (
            "Jira token is invalid or expired. Update your token in settings."
        )
        assert f"status={status}" in caplog.text
        assert "secret_jira_token" not in caplog.text
        assert _REPORT_BODY not in caplog.text


class TestChatIssueEndpoint:
    """POST /api/chat-issue — a tracker write driven from a chat conversation.

    Every credential here must come from the caller's account row: no
    deployment token on any path, no bootstrap-``admin`` exception.
    """

    _CHAT: ClassVar[dict[str, str]] = {
        "title": "test_login fails with DNS timeout",
        "body": "The login test timed out resolving the auth service.",
    }

    #: The admin chat's conversation id — what its issue tools hardcode.
    _ADMIN_CHAT: ClassVar[str] = "__admin_chat__"

    # -- helpers -------------------------------------------------------------

    @staticmethod
    async def _user(name: str, **tokens: str) -> dict[str, str]:
        """A registered user whose account row holds *tokens*."""
        _, api_key = await storage.create_user(name)
        if tokens:
            await storage.save_user_tokens(name, **tokens)
        return {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    async def _admin(name: str, **tokens: str) -> dict[str, str]:
        """An admin caller: the one GitHub path that needs no run to file in."""
        username, api_key = await storage.create_admin_user(name)
        if tokens:
            await storage.save_user_tokens(username, **tokens)
        return {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    async def _chat_audit() -> dict[str, Any]:
        return await storage.list_audit_events(
            action="issue.create", target_type="chat"
        )

    @staticmethod
    async def _run(job_id: str, **params: str) -> str:
        """A stored run — a non-admin GitHub write must name the run it belongs to.

        Its ``request_params`` are what bound the repositories chat may file in.
        """
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "request_params": {
                    "job_name": job_id,
                    "build_number": 1,
                    **params,
                }
            },
        )
        return job_id

    @classmethod
    async def _propose_only(
        cls,
        test_client,
        target: str,
        headers: dict[str, str] | None = None,
        job_id: str = "",
        conversation: str | None = None,
        **text: str,
    ) -> dict[str, str]:
        """Draft as the chat tool does, and return nothing the user confirmed.

        The proposal carries the conversation the chat tool hardcodes (the run
        for a job chat, the admin chat otherwise; ``conversation=""`` is a
        direct, non-chat caller such as the CLI), and this stores NO user reply
        — the caller decides whether to send one with :meth:`_user_reply`.
        """
        if conversation is None:
            conversation = job_id or "__admin_chat__"
        proposal_body = {
            **cls._CHAT,
            "target": target,
            **text,
            **({"job_id": job_id} if job_id else {}),
            **({"conversation": conversation} if conversation else {}),
        }
        # A chat with no run of its own files in the deployment's repo, so the
        # proposal resolves that repo — as a real admin deployment has it.
        github_repo = nullcontext()
        if not job_id and target == "github":
            github_repo = _with_github_issue_config()
        with github_repo:
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json=proposal_body,
                headers=headers or {},
            )
        assert proposal.status_code == 201, proposal.text
        draft = proposal.json()
        return {
            "confirmation_id": draft["confirmation_id"],
            "confirmation_code": draft["confirmation_code"],
            "title": draft["title"],
            "body": draft["body"],
            "target": target,
        }

    @staticmethod
    async def _user_reply(
        headers: dict[str, str] | None,
        code: str,
        *,
        conversation: str = "__admin_chat__",
        username: str | None = None,
        role: str = "user",
    ) -> None:
        """Store a chat message carrying *code* — the user's confirmation."""
        if username is None:
            token = (headers or {}).get("Authorization", "")
            user = await storage.get_user_by_key(token.removeprefix("Bearer "))
            username = (user or {}).get("username", "")
        await storage.add_chat_message(
            conversation,
            role=role,
            content=f"Yes, file it: {code}",
            username=username or "",
        )

    @classmethod
    async def _propose(
        cls,
        test_client,
        target: str,
        headers: dict[str, str] | None = None,
        job_id: str = "",
        conversation: str | None = None,
        **text: str,
    ) -> dict[str, str]:
        """Draft the issue and send the user's reply, as a real chat would.

        ``/api/chat-issue`` files only a proposed draft whose code the USER
        replied with, so every create call here starts from these fields. Care
        is taken to keep the reply in the draft's own conversation: a code from
        another conversation is not a confirmation.
        """
        draft = await cls._propose_only(
            test_client,
            target,
            headers,
            job_id=job_id,
            conversation=conversation,
            **text,
        )
        await cls._user_reply(
            headers,
            draft["confirmation_code"],
            conversation=(
                conversation
                if conversation is not None
                else (job_id or "__admin_chat__")
            ),
        )
        return draft

    # -- GitHub --------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_github_uses_the_callers_row_token(self, test_client):
        """The token on the outbound call is the caller's, and the attempt is audited."""
        headers = await self._user("chat-gh", github_token="ghp_row_token")
        job_id = await self._run(
            "chat-gh-run", tests_repo_url="https://github.com/org/repo"
        )
        draft = await self._propose(test_client, "github", headers, job_id=job_id)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/7",
                "number": 7,
            }
            with _with_github_issue_config():
                response = test_client.post(
                    "/api/chat-issue",
                    json={**draft, "job_id": job_id},
                    headers=headers,
                )
        assert response.status_code == 201, response.text
        assert response.json()["number"] == 7
        assert mock_create.call_args[1]["github_token"] == "ghp_row_token"
        audit = await self._chat_audit()
        assert audit["total"] == 1
        event = audit["events"][0]
        assert event["details"]["result"] == "success"
        assert event["details"]["tracker"] == "github"
        assert event["target_id"] is None
        assert event["outcome"] == "success"
        # The row names the issue it filed: a reader reaches it from the row
        # alone, and the clickable URL is derivable from repo + number.
        assert event["details"]["repo"] == "org/repo"
        assert event["details"]["number"] == 7
        assert event["details"]["url"] == "https://github.com/org/repo/issues/7"
        assert event["details"]["url"] == (
            f"https://github.com/{event['details']['repo']}"
            f"/issues/{event['details']['number']}"
        )

    @pytest.mark.asyncio
    async def test_github_without_a_row_token_refuses_and_audits(self, test_client):
        """No account token → the followable 400, nothing sent, one refusal row."""
        headers = await self._user("chat-gh-none")
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**self._CHAT, "target": "github"},
                headers=headers,
            )
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "GitHub token is required. Set up your token in Profile Settings."
        )
        mock_create.assert_not_called()
        audit = await self._chat_audit()
        assert audit["total"] == 1
        assert audit["events"][0]["details"]["result"] == "refused"
        assert audit["events"][0]["details"]["reason"] == "invalid_request"

    @pytest.mark.asyncio
    async def test_every_refused_attempt_is_audited_not_just_the_create(
        self, test_client
    ):
        """Row G2: a refusal leaves one row saying why, naming the subject — and
        never carrying a credential.

        Only ``POST /api/chat-issue`` wrote a row at #407: a refused proposal and
        a call the auth layer refuses (a disabled account) left nothing.
        """
        pasted = "ghp_pasted_by_the_user_0123456789"  # pragma: allowlist secret
        headers = await self._user("chat-audit-notoken")
        job_id = await self._run(
            "chat-audit-notoken-run", tests_repo_url="https://github.com/org/repo"
        )
        # 1. A proposal refused for the caller's missing credential.
        proposal = test_client.post(
            "/api/chat-issue/propose",
            json={
                "title": "t",
                "body": f"the token is {pasted}",
                "target": "github",
                "repo": "org/elsewhere",
                "job_id": job_id,
                "conversation": job_id,
            },
            headers=headers,
        )
        assert proposal.status_code == 400, proposal.text

        # 2. A create refused by the auth layer before the handler runs.
        disabled = await self._user("chat-audit-disabled")
        await storage.set_user_disabled("chat-audit-disabled", True)
        refused = test_client.post(
            "/api/chat-issue",
            json={**self._CHAT, "target": "github"},
            headers=disabled,
        )
        assert refused.status_code == 401, refused.text

        audit = await self._chat_audit()
        assert audit["total"] == 2, audit
        by_step = {event["details"]["step"]: event for event in audit["events"]}

        refused_proposal = by_step["propose"]
        assert refused_proposal["outcome"] == "failure"
        assert refused_proposal["actor"] == "chat-audit-notoken"
        # The reason code and the destination asked for, never the draft text.
        assert refused_proposal["details"] == {
            "reason": "invalid_request",
            "result": "refused",
            "step": "propose",
            "tracker": "github",
            "job_id": job_id,
            "repo": "org/elsewhere",
            "jira_project": "",
        }

        auth_refusal = by_step["create"]
        assert auth_refusal["outcome"] == "denied"
        assert auth_refusal["actor"] == "chat-audit-disabled"
        assert auth_refusal["details"] == {
            "reason": "account_disabled",
            "result": "refused",
            "step": "create",
        }
        assert pasted not in json.dumps(audit)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "step", "payload", "fields"),
        [
            (
                "/api/chat-issue/propose",
                "propose",
                {"target": "github", "title": "t"},
                ["body"],
            ),
            (
                "/api/chat-issue/propose",
                "propose",
                {"target": "slack", "title": "t", "body": "b"},
                ["target"],
            ),
            ("/api/chat-issue", "create", {"target": "github", "title": "t"}, ["body"]),
        ],
    )
    async def test_a_request_rejected_by_validation_leaves_an_audit_row(
        self,
        test_client,
        path: str,
        step: str,
        payload: dict[str, str],
        fields: list[str],
    ):
        """A 422 never reaches the handler, so its attempt is audited here.

        Without this row an admin could not learn a refused attempt happened:
        the handler is the only other place that writes one. The details name
        the endpoint's step and the failing fields — never a body or a value.
        """
        pasted = "ghp_pasted_by_the_user_0123456789"  # pragma: allowlist secret
        headers = await self._user(f"chat-422-{step}")
        response = test_client.post(
            path, json={**payload, "jira_security_level": pasted}, headers=headers
        )
        assert response.status_code == 422, response.text

        audit = await self._chat_audit()
        assert audit["total"] == 1, audit
        event = audit["events"][0]
        assert event["actor"] == f"chat-422-{step}"
        assert event["outcome"] == "denied"
        assert event["target_id"] == f"chat-422-{step}"
        assert event["details"] == {
            "reason": "invalid_request",
            "result": "refused",
            "step": step,
            "fields": fields,
        }
        assert pasted not in json.dumps(audit)
        assert "missing" not in json.dumps(audit)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "request_field", "detail_key"),
        [("github", "repo", "repo"), ("jira", "jira_project_key", "jira_project")],
    )
    async def test_a_refusal_drops_a_destination_that_is_not_shaped_like_one(
        self,
        test_client,
        target: str,
        request_field: str,
        detail_key: str,
    ):
        """A refusal row stores a destination, never text the caller typed.

        ``repo``/``jira_project`` are exempt from the bare-run heuristic because
        they name a destination — an exemption that must not apply to raw input,
        or a pasted 40-hex token is stored verbatim on the refusal row.
        """
        headers = await self._user(f"chat-audit-unshaped-{target}")
        refused = test_client.post(
            "/api/chat-issue/propose",
            json={**self._CHAT, "target": target, request_field: _PASTED_HEX},
            headers=headers,
        )
        assert refused.status_code == 400, refused.text

        audit = await self._chat_audit()
        assert audit["total"] == 1, audit
        assert audit["events"][0]["details"][detail_key] == ""
        assert _PASTED_HEX not in json.dumps(audit)

    @pytest.mark.asyncio
    async def test_a_refusal_keeps_a_repo_that_is_shaped_like_one(self, test_client):
        """The gate drops pasted tokens, not the subject the row exists to name."""
        headers = await self._user("chat-audit-slug-refusal")
        refused = test_client.post(
            "/api/chat-issue/propose",
            json={**self._CHAT, "target": "github", "repo": "org/elsewhere"},
            headers=headers,
        )
        assert refused.status_code == 400, refused.text

        audit = await self._chat_audit()
        assert audit["total"] == 1, audit
        assert audit["events"][0]["details"]["repo"] == "org/elsewhere"

    @pytest.mark.asyncio
    async def test_an_unauthenticated_refusal_names_no_caller_identity(
        self, test_client
    ):
        """The auth layer's refusal row must not launder a forged cookie.

        Line 3227 is reached only when no session/bearer/proxy identity was
        authenticated, so the ``rootcoz_username`` cookie is caller-chosen:
        attributing the row to it let ``Cookie: rootcoz_username=victim`` write
        a denied ``issue.create`` row naming that victim.
        """
        forged = "chat-audit-victim"
        response = test_client.post(
            "/api/chat-issue",
            json={**self._CHAT, "target": "github"},
            headers={"Authorization": ""},
            cookies={"rootcoz_username": forged},
        )
        assert response.status_code == 401, response.text

        audit = await self._chat_audit()
        assert audit["total"] == 1, audit
        event = audit["events"][0]
        assert event["outcome"] == "denied"
        assert event["actor"] == "anonymous"
        assert event["target_id"] is None
        assert event["details"] == {
            "reason": "unauthenticated",
            "result": "refused",
            "step": "create",
        }
        # Not in the actor, the target, the log line or the details.
        assert forged not in json.dumps(audit)

    @pytest.mark.asyncio
    async def test_bootstrap_admin_is_pointed_at_a_path_it_can_take(self, test_client):
        """The bootstrap admin has no users row, so Profile Settings is a dead end."""
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**self._CHAT, "target": "github"},
            )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "GitHub token is required" in detail
        assert "no profile to store one" in detail
        assert "Sign in with your own account" in detail
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_never_falls_back_to_the_server_token(self, test_client):
        """A deployment GITHUB_TOKEN is configuration, never identity."""
        headers = await self._user("chat-gh-srvtok")
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**self._CHAT, "target": "github"},
                headers=headers,
            )
        assert response.status_code == 400
        assert "token is required" in response.json()["detail"].lower()
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_disabled_returns_403(self, test_client):
        headers = await self._user("chat-gh-disabled", github_token="ghp_row_token")
        from rootcoz.config import get_settings

        with patch.dict(
            os.environ,
            {
                "ENABLE_GITHUB_ISSUES": "false",
                "TESTS_REPO_URL": "https://github.com/test-org/test-repo",
            },
        ):
            get_settings.cache_clear()
            try:
                response = test_client.post(
                    "/api/chat-issue",
                    json={**self._CHAT, "target": "github"},
                    headers=headers,
                )
            finally:
                get_settings.cache_clear()
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_disabled_tracker_reads_as_disabled_without_a_token(
        self, test_client
    ):
        """A disabled tracker answers 403, not a token error the user cannot fix."""
        from rootcoz.config import get_settings

        headers = await self._user("chat-gh-disabled-notoken")
        with patch.dict(
            os.environ,
            {
                "ENABLE_GITHUB_ISSUES": "false",
                "TESTS_REPO_URL": "https://github.com/test-org/test-repo",
            },
        ):
            get_settings.cache_clear()
            try:
                response = test_client.post(
                    "/api/chat-issue",
                    json={**self._CHAT, "target": "github"},
                    headers=headers,
                )
            finally:
                get_settings.cache_clear()
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_github_without_a_repo_url_returns_400(self, test_client):
        """Nowhere to file is refused at the proposal: there is no destination
        to show the user — and no default can change between the two steps."""
        headers = await self._user("chat-gh-norepo", github_token="ghp_row_token")
        job_id = await self._run("chat-gh-norepo-run")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "job_id": job_id,
                    "conversation": job_id,
                },
                headers=headers,
            )
        assert proposal.status_code == 400, proposal.text
        assert "this run has no GitHub test repository" in proposal.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_404_names_the_repo_the_chat_files_into(self, test_client):
        """#398's mapping wins, but it names the repository that actually failed.

        GitHub answers 404 instead of 403 for an invisible repository, so the
        chat path must send the user to their token — and to the repository the
        request used, not to a deployment setting that had nothing to do with
        it.
        """
        import httpx

        headers = await self._user("chat-gh-404", github_token="ghp_row_token")
        job_id = await self._run(
            "chat-gh-404-run", tests_repo_url="https://github.com/org/run-repo"
        )
        draft = await self._propose(test_client, "github", headers, job_id=job_id)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("POST", "https://api.github.com"),
                response=httpx.Response(404),
            )
            with _with_github_issue_config():
                response = test_client.post(
                    "/api/chat-issue",
                    json={**draft, "job_id": job_id},
                    headers=headers,
                )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail == (
            "GitHub cannot see the repository org/run-repo, so your issue was "
            "not filed. Check that the repository exists and that your token "
            "can access it in Profile Settings."
        )
        # The deployment setting was not the repository this request used.
        assert "TESTS_REPO_URL" not in detail

    @pytest.mark.asyncio
    async def test_github_404_names_tests_repo_url_when_it_is_the_repo(
        self, test_client
    ):
        """A chat with no run files in the deployment's repo — so name the setting."""
        import httpx

        headers = await self._admin("chat-gh-404-default", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("POST", "https://api.github.com"),
                response=httpx.Response(404),
            )
            with _with_github_issue_config():
                response = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert response.status_code == 403
        assert response.json()["detail"] == (
            "GitHub cannot see the repository in TESTS_REPO_URL, so your issue "
            "was not filed. Check that the repository exists and that your "
            "token can access it in Profile Settings."
        )

    # -- Jira -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_jira_uses_the_callers_row_credentials(self, test_client):
        headers = await self._user(
            "chat-jira", jira_token="user_jira_token", jira_email="user@example.com"
        )
        with patch("rootcoz.main.create_jira_bug") as mock_create:
            mock_create.return_value = {
                "key": "PROJ-7",
                "url": "https://jira.example.com/browse/PROJ-7",
            }
            with _enable_feature("jira_enabled"):
                draft = await self._propose(test_client, "jira", headers)
                response = test_client.post(
                    "/api/chat-issue",
                    json=draft,
                    headers=headers,
                )
        assert response.status_code == 201, response.text
        assert response.json()["key"] == "PROJ-7"
        # ``key``, not ``issue_key``: a ``*_key`` detail is dropped as a secret.
        # ``jira_project`` and ``site`` say what the link belongs to, also a
        # non-``*_key`` name for the same reason.
        assert (await self._chat_audit())["events"][0]["details"] == {
            "tracker": "jira",
            "result": "success",
            "job_id": "",
            "key": "PROJ-7",
            "url": "https://jira.example.com/browse/PROJ-7",
            "jira_project": "TEST",
            "site": "jira.example.com",
        }
        call_settings = mock_create.call_args[1]["settings"]
        assert call_settings.jira_api_token.get_secret_value() == "user_jira_token"
        assert call_settings.jira_auth_mode == "basic"

    @pytest.mark.asyncio
    async def test_jira_never_falls_back_to_the_server_token(self, test_client):
        """The deployment holds a working Jira credential; the caller does not.

        Without this, a weakened gate would file the ticket under the server's
        identity and every other test would still pass.
        """
        headers = await self._user("chat-jira-srvtok")
        with (
            patch("rootcoz.main.create_jira_bug") as mock_create,
            _enable_feature("jira_enabled"),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**self._CHAT, "target": "jira"},
                headers=headers,
            )
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "Jira token is required. Set up your token in Profile Settings."
        )
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_jira_disabled_returns_403(self, test_client):
        headers = await self._user("chat-jira-off", jira_token="user_jira_token")
        from rootcoz.config import get_settings

        with patch.dict(os.environ, {"ENABLE_JIRA_ISSUES": "false"}):
            get_settings.cache_clear()
            try:
                response = test_client.post(
                    "/api/chat-issue",
                    json={**self._CHAT, "target": "jira"},
                    headers=headers,
                )
            finally:
                get_settings.cache_clear()
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_jira_legacy_service_account_secret_is_refused(self, test_client):
        """A legacy SA secret is rejected, never silently dropped.

        The refusal comes at the proposal, not at the create: the tool list no
        longer offers a Jira issue for a credential the create call would
        refuse, so the model is told before it drafts anything.
        """
        headers = await self._user(
            "chat-jira-legacy", jira_sa_token="legacy-secret-token"
        )
        with (
            patch("rootcoz.main.create_jira_bug") as mock_create,
            _enable_feature("jira_enabled"),
        ):
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json={**self._CHAT, "target": "jira"},
                headers=headers,
            )
        assert proposal.status_code == 400, proposal.text
        assert "not a recognized" in proposal.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_jira_mint_error_is_a_controlled_400(self, test_client):
        """A credential that cannot mint is a configuration error, not a 500."""
        from rootcoz.jira import JiraOAuthMintError
        from rootcoz.main import app, get_settings

        headers = await self._user("chat-jira-mint", jira_sa_token="ATOAsecret")
        # oauth method: the user's ATOA secret replaces the deployment's, and the
        # client/cloud ids stay the server's.
        app.dependency_overrides[get_settings] = lambda: _build_wait_settings(
            jira_auth_mode="oauth",
            jira_project_key="PROJ",
            atlassian_oauth_client_id="client-id",
            atlassian_oauth_cloud_id="cloud-id",
        )
        try:
            with patch("rootcoz.main.create_jira_bug") as mock_create:
                mock_create.side_effect = JiraOAuthMintError("cannot mint access token")
                draft = await self._propose(test_client, "jira", headers)
                response = test_client.post(
                    "/api/chat-issue",
                    json=draft,
                    headers=headers,
                )
        finally:
            app.dependency_overrides.pop(get_settings, None)
        assert response.status_code == 400
        assert response.json()["detail"] == "cannot mint access token"

    # -- access ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_viewer_cannot_create_a_chat_issue(self, test_client):
        _, api_key = await storage.create_user("chat-viewer", role="viewer")
        response = test_client.post(
            "/api/chat-issue",
            json={**self._CHAT, "target": "github"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 403
        assert "Reviewer access required" in response.json()["detail"]
        # A denied attempt is an attempt: the chat trail has the row.
        audit = await self._chat_audit()
        assert audit["total"] == 1
        assert audit["events"][0]["outcome"] == "denied"
        assert audit["events"][0]["actor"] == "chat-viewer"

    # -- scrubbing ------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_chat_text_is_scrubbed_before_it_leaves(self, test_client):
        """A pasted token never reaches the tracker — not even as a hex SHA.

        A 40-hex commit SHA is also a legacy GitHub token shape, so the shared
        scrubber redacts it; the chat model sends what the scrubber returns, so
        the refusal names the marker and the user can retype the SHA by hand.
        """
        sha = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
        headers = await self._user("chat-scrub", github_token="ghp_row_token")
        job_id = await self._run(
            "chat-scrub-run", tests_repo_url="https://github.com/org/repo"
        )
        title = "auth broke with ghp_abcdefghijklmnopqrstuvwxyz0123"
        issue_body = f"Commit {sha} broke login."
        draft = await self._propose(
            test_client, "github", headers, job_id=job_id, title=title, body=issue_body
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/8",
                "number": 8,
            }
            with _with_github_issue_config():
                response = test_client.post(
                    "/api/chat-issue",
                    json={
                        **draft,
                        "title": title,
                        "body": issue_body,
                        "job_id": job_id,
                    },
                    headers=headers,
                )
        assert response.status_code == 201, response.text
        kwargs = mock_create.call_args[1]
        assert "ghp_" not in kwargs["title"]
        assert "[REDACTED_GITHUB_TOKEN]" in kwargs["title"]
        assert sha not in kwargs["body"]
        assert "[REDACTED_HEX_TOKEN]" in kwargs["body"]

    # -- consent gate ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_create_without_a_draft_files_nothing(self, test_client):
        """The model's own claim that the user asked is not consent."""
        headers = await self._admin("chat-nodraft", github_token="ghp_row_token")
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            missing = test_client.post(
                "/api/chat-issue",
                json={**self._CHAT, "target": "github"},
                headers=headers,
            )
            unknown = test_client.post(
                "/api/chat-issue",
                json={
                    **self._CHAT,
                    "target": "github",
                    "confirmation_id": "never-issued",
                },
                headers=headers,
            )
        for response in (missing, unknown):
            assert response.status_code == 400, response.text
            assert "confirmation id" in response.json()["detail"]
        mock_create.assert_not_called()
        audit = await self._chat_audit()
        assert audit["total"] == 2
        assert all(e["details"]["result"] == "refused" for e in audit["events"])

    @pytest.mark.asyncio
    async def test_a_draft_is_single_use_and_expires(self, test_client):
        """A confirmation id authorises one create call, within its window."""
        from rootcoz import main as main_mod

        headers = await self._admin("chat-reuse", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/9",
                "number": 9,
            }
            with _with_github_issue_config():
                first = test_client.post("/api/chat-issue", json=draft, headers=headers)
                reused = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert first.status_code == 201, first.text
        assert reused.status_code == 400
        assert "already used" in reused.json()["detail"]
        assert mock_create.call_count == 1
        # An expired draft of a fresh create is refused the same way.
        expired = await self._propose(test_client, "github", headers)
        main_mod._chat_issue_drafts[expired["confirmation_id"]]["expires_at"] = 0
        with _with_github_issue_config():
            stale = test_client.post("/api/chat-issue", json=expired, headers=headers)
        assert stale.status_code == 400
        assert "expired" in stale.json()["detail"]

    @pytest.mark.asyncio
    async def test_another_accounts_draft_files_nothing(self, test_client):
        headers = await self._admin("chat-mine", github_token="ghp_row_token")
        other = await self._admin("chat-theirs", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", other)
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert response.status_code == 400
        assert "belongs to another account" in response.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_admin_needs_a_draft_too(self, test_client):
        """No rank is exempt: the draft is about consent, not permissions."""
        headers = await self._admin("chat-admin-nodraft", github_token="ghp_row_token")
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**self._CHAT, "target": "github"},
                headers=headers,
            )
        assert response.status_code == 400
        assert "confirmation id" in response.json()["detail"]
        mock_create.assert_not_called()

    # -- the user's confirmation (the code in their own reply) ----------------

    @pytest.mark.asyncio
    async def test_the_preview_names_the_tracker_destination_and_code(
        self, test_client
    ):
        """The user approves a destination, not just text: an omitted repo is
        shown as the repo it resolves to, and the preview carries the code."""
        headers = await self._admin("chat-preview", github_token="ghp_row_token")
        with _with_github_issue_config():
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=headers,
            )
        assert proposal.status_code == 201, proposal.text
        preview = proposal.json()["preview"]
        code = proposal.json()["confirmation_code"]
        assert len(code) == 6
        assert "Tracker: GitHub" in preview
        # The default repo is named, not left as an empty field to approve.
        assert "Destination: GitHub repository org/repo" in preview
        assert self._CHAT["title"] in preview
        assert self._CHAT["body"] in preview
        assert f"Confirmation code: {code}" in preview
        assert proposal.json()["destination"]["repo"] == "org/repo"

    def test_a_non_github_tests_repo_is_not_a_github_destination(self):
        """A chat with no run of its own gates GitHub on a repo it can file in.

        ``TESTS_REPO_URL`` pointing at another host is not a GitHub destination,
        so the tool must not be offered for it — the create call parses the URL
        and would refuse every issue.
        """
        from rootcoz.main import _settings_github_repo_available

        assert (
            _settings_github_repo_available(
                SimpleNamespace(tests_repo_url="https://gitlab.com/org/repo")
            )
            is False
        )
        assert (
            _settings_github_repo_available(
                SimpleNamespace(tests_repo_url="https://github.com/org/repo")
            )
            is True
        )
        # No setting at all (the deployment this gate exists for).
        assert _settings_github_repo_available(SimpleNamespace()) is False

    @pytest.mark.asyncio
    async def test_the_consent_request_is_posted_by_the_server(self, test_client):
        """The user approves the server's own message, not the model's summary.

        The preview handed to the model is narration: a model steered by job or
        database content can misstate the destination (or say "reply with the
        code to cancel") and still hold a code that authorises the stored draft.
        So the same words are written into the chat by this server, as a role
        the model cannot author, and the draft's watermark is that row's id —
        only a reply NEWER than it confirms.
        """
        from rootcoz import main as main_mod

        headers = await self._admin("chat-consent-msg", github_token="ghp_row_token")
        with _with_github_issue_config():
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=headers,
            )
        assert proposal.status_code == 201, proposal.text
        draft = proposal.json()
        messages = await storage.get_chat_messages(
            self._ADMIN_CHAT, username="chat-consent-msg"
        )
        notices = [message for message in messages if message["role"] == "system"]
        assert len(notices) == 1, messages
        shown = notices[0]["content"]
        # What the user is asked to approve, verbatim and server-written.
        assert "Tracker: GitHub" in shown
        assert "Destination: GitHub repository org/repo" in shown
        assert self._CHAT["title"] in shown
        assert self._CHAT["body"] in shown
        assert f"Confirmation code: {draft['confirmation_code']}" in shown
        # A rebuilt AI session keeps only stored chat text: the proposal id has
        # to be in the notice too, or the confirmed draft can never be filed.
        assert f"Proposal id: {draft['confirmation_id']}" in shown
        # Consent is bound to that row: the code has to arrive after it.
        assert (
            main_mod._chat_issue_drafts[draft["confirmation_id"]]["watermark"]
            == notices[0]["id"]
        )

    @pytest.mark.asyncio
    async def test_the_jira_preview_names_the_project_and_issue_type(self, test_client):
        headers = await self._user(
            "chat-preview-jira", jira_token="user_jira_token", jira_email="u@e.com"
        )
        with _enable_feature("jira_enabled"):
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json={**self._CHAT, "target": "jira"},
                headers=headers,
            )
        assert proposal.status_code == 201, proposal.text
        preview = proposal.json()["preview"]
        assert "Tracker: Jira" in preview
        assert "Destination: Jira project TEST, issue type Bug" in preview
        assert f"Confirmation code: {proposal.json()['confirmation_code']}" in preview

    @pytest.mark.asyncio
    async def test_a_create_nobody_confirmed_files_nothing(self, test_client):
        """The model may propose and create in one turn — it gets the skipped step.

        It holds the id and the code (both come back in the proposal), so this
        is the case that proves the gate is not a shared secret: what is missing
        is a message from the user.
        """
        headers = await self._admin("chat-unconfirmed", github_token="ghp_row_token")
        draft = await self._propose_only(test_client, "github", headers)
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert response.status_code == 400, response.text
        assert (
            "the user must confirm by replying with the code shown in the preview"
            in response.json()["detail"]
        )
        mock_create.assert_not_called()
        # Nothing was spent: the same draft still files once the user replies.
        await self._user_reply(headers, draft["confirmation_code"])
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/11",
                "number": 11,
            }
            with _with_github_issue_config():
                filed = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert filed.status_code == 201, filed.text

    @pytest.mark.asyncio
    async def test_a_model_authored_message_does_not_confirm(self, test_client):
        """Only a stored USER message counts: the model writes the others."""
        headers = await self._admin("chat-selfconfirm", github_token="ghp_row_token")
        draft = await self._propose_only(test_client, "github", headers)
        # Exactly what the chat model's own turn stores — and the code in it.
        await self._user_reply(headers, draft["confirmation_code"], role="assistant")
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert response.status_code == 400, response.text
        assert (
            "the user must confirm by replying with the code shown in the preview"
            in response.json()["detail"]
        )
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_code_from_another_conversation_does_not_confirm(self, test_client):
        """The reply has to land in the chat the draft belongs to."""
        headers = await self._user("chat-other-conv", github_token="ghp_row_token")
        await self._run("chat-conv-a", tests_repo_url="https://github.com/org/a")
        await self._run("chat-conv-b", tests_repo_url="https://github.com/org/b")
        draft = await self._propose_only(
            test_client, "github", headers, job_id="chat-conv-a"
        )
        await self._user_reply(
            headers, draft["confirmation_code"], conversation="chat-conv-b"
        )
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**draft, "job_id": "chat-conv-a"},
                headers=headers,
            )
        assert response.status_code == 400, response.text
        assert "nobody confirmed this draft" in response.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_another_users_reply_does_not_confirm(self, test_client):
        """A second account's message in the same chat is not this user's consent."""
        headers = await self._user("chat-code-mine", github_token="ghp_row_token")
        await self._run("chat-code-shared", tests_repo_url="https://github.com/org/a")
        draft = await self._propose_only(
            test_client, "github", headers, job_id="chat-code-shared"
        )
        await self._user_reply(
            None,
            draft["confirmation_code"],
            conversation="chat-code-shared",
            username="chat-code-theirs",
        )
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**draft, "job_id": "chat-code-shared"},
                headers=headers,
            )
        assert response.status_code == 400, response.text
        assert "nobody confirmed this draft" in response.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_wrong_or_missing_code_is_refused(self, test_client):
        headers = await self._admin("chat-wrong-code", github_token="ghp_row_token")
        draft = await self._propose_only(test_client, "github", headers)
        await self._user_reply(headers, draft["confirmation_code"])
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            wrong = test_client.post(
                "/api/chat-issue",
                json={**draft, "confirmation_code": "ZZZZZZ"},
                headers=headers,
            )
            missing = test_client.post(
                "/api/chat-issue",
                json={**draft, "confirmation_code": ""},
                headers=headers,
            )
        for response in (wrong, missing):
            assert response.status_code == 400, response.text
            assert "confirmation code" in response.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_code_dies_with_the_id_on_use(self, test_client):
        """One file per draft: the code cannot be replayed after it worked."""
        headers = await self._admin("chat-code-once", github_token="ghp_row_token")
        draft = await self._propose_only(test_client, "github", headers)
        await self._user_reply(headers, draft["confirmation_code"])
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/12",
                "number": 12,
            }
            with _with_github_issue_config():
                first = test_client.post("/api/chat-issue", json=draft, headers=headers)
                replay = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert first.status_code == 201, first.text
        assert replay.status_code == 400
        assert "already used" in replay.json()["detail"]
        assert mock_create.call_count == 1

    @pytest.mark.asyncio
    async def test_a_direct_caller_confirms_with_the_code_alone(self, test_client):
        """No conversation (the CLI): the human running the two commands typed
        the code, and no chat tool can propose such a draft."""
        headers = await self._admin("chat-cli", github_token="ghp_row_token")
        draft = await self._propose_only(
            test_client, "github", headers, conversation=""
        )
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/13",
                "number": 13,
            }
            response = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert response.status_code == 201, response.text

    # -- the claim, the caps and retries --------------------------------------

    @pytest.fixture(autouse=True)
    def _no_leftover_drafts(self):
        """Drafts are process state: every test here starts and ends empty."""
        from rootcoz import main as main_mod

        main_mod._chat_issue_drafts.clear()
        yield
        main_mod._chat_issue_drafts.clear()

    @pytest.mark.asyncio
    async def test_two_parallel_creates_file_one_issue(self, test_client):
        """One confirmation files once, however many creates arrive together.

        Both requests are held inside the consent read at the same time: without
        an atomic claim both pass it and both file, so the second has to be
        refused by the claim itself and not by timing.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        headers = await self._admin("chat-race", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)

        scans: list[int] = []
        # Both POSTs start together, then the patched consent read keeps the
        # first one inside it long enough for the second to arrive.
        start = threading.Barrier(2, timeout=5)

        async def _slow_reply_check(**_kwargs) -> bool:
            scans.append(1)
            await asyncio.sleep(0.2)
            return True

        def _create() -> int:
            start.wait()
            return test_client.post(
                "/api/chat-issue", json=draft, headers=headers
            ).status_code

        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            patch("rootcoz.main._user_replied_with_code", new=_slow_reply_check),
            _with_github_issue_config(),
        ):
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/20",
                "number": 20,
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = [
                    future.result(timeout=30)
                    for future in [pool.submit(_create) for _ in range(2)]
                ]

        assert sorted(statuses) == [201, 400], statuses
        # Exactly one request ever reached the consent read: the loser lost the
        # claim before it could.
        assert len(scans) == 1
        assert mock_create.call_count == 1

    @pytest.mark.asyncio
    async def test_a_new_proposal_replaces_this_users_oldest_draft(self, test_client):
        """Five live previews per account: the sixth evicts the first."""
        from rootcoz import main as main_mod

        headers = await self._admin("chat-cap", github_token="ghp_row_token")
        drafts = [
            await self._propose_only(test_client, "github", headers)
            for _ in range(main_mod._ISSUE_DRAFTS_PER_USER + 1)
        ]
        live = {
            draft_id
            for draft_id, draft in main_mod._chat_issue_drafts.items()
            if draft["username"] == "chat-cap"
        }
        assert len(live) == main_mod._ISSUE_DRAFTS_PER_USER
        assert drafts[0]["confirmation_id"] not in live
        assert drafts[-1]["confirmation_id"] in live
        # The evicted draft is gone, and says so the way any spent id does.
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue", json=drafts[0], headers=headers
            )
        assert response.status_code == 400
        assert "unknown, expired or already used" in response.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_full_store_refuses_the_new_proposal(
        self, test_client, monkeypatch
    ):
        """The whole map has a ceiling, and hitting it is a refusable 503."""
        from rootcoz import main as main_mod

        monkeypatch.setattr(main_mod, "_ISSUE_DRAFTS_MAX", 1)
        first = await self._admin("chat-fill", github_token="ghp_row_token")
        await self._propose_only(test_client, "github", first)
        second = await self._admin("chat-full", github_token="ghp_row_token")
        with _with_github_issue_config():
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=second,
            )
        assert proposal.status_code == 503, proposal.text
        detail = proposal.json()["detail"]
        assert "too many unconfirmed issue previews" in detail
        assert "propose the issue again" in detail
        # Nothing was kept for the refused account.
        assert not [
            draft
            for draft in main_mod._chat_issue_drafts.values()
            if draft["username"] == "chat-full"
        ]

    @pytest.mark.asyncio
    async def test_a_failed_store_evicts_nothing(self, test_client):
        """The caps are applied only once nothing can fail any more.

        A proposal at the per-account cap whose watermark read fails must not
        cost the user the draft the old ordering evicted before that read.
        """
        from rootcoz import main as main_mod

        headers = await self._admin("chat-insert-fail", github_token="ghp_row_token")
        drafts = [
            await self._propose_only(test_client, "github", headers)
            for _ in range(main_mod._ISSUE_DRAFTS_PER_USER)
        ]
        with (
            patch(
                "rootcoz.main._chat_last_message_id",
                side_effect=RuntimeError("storage is down"),
            ),
            _with_github_issue_config(),
            pytest.raises(RuntimeError),
        ):
            test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=headers,
            )
        live = {
            draft_id
            for draft_id, draft in main_mod._chat_issue_drafts.items()
            if draft["username"] == "chat-insert-fail"
        }
        assert live == {draft["confirmation_id"] for draft in drafts}

    @pytest.mark.asyncio
    async def test_a_released_draft_cannot_exceed_the_cap(self, test_client):
        """The claim freed a slot and a new proposal took it: the caps win."""
        from rootcoz import main as main_mod

        headers = await self._admin("chat-release", github_token="ghp_row_token")
        drafts = [
            await self._propose_only(test_client, "github", headers)
            for _ in range(main_mod._ISSUE_DRAFTS_PER_USER)
        ]
        claimed = main_mod._chat_issue_drafts.pop(drafts[0]["confirmation_id"])
        await self._propose_only(test_client, "github", headers)
        main_mod._release_issue_draft(drafts[0]["confirmation_id"], claimed)
        live = [
            draft
            for draft in main_mod._chat_issue_drafts.values()
            if draft["username"] == "chat-release"
        ]
        assert len(live) == main_mod._ISSUE_DRAFTS_PER_USER
        assert drafts[0]["confirmation_id"] not in main_mod._chat_issue_drafts

    @pytest.mark.asyncio
    async def test_a_draft_evicted_while_its_notice_is_written_is_refused(
        self, test_client
    ):
        """A burst at the cap must not return a preview that can file nothing.

        The draft is inserted before the server-notice write is awaited, so a
        proposal in between can evict it. The id then answers "unknown, expired
        or already used" although the user was shown a live-looking preview: the
        proposal now takes that notice back and refuses instead.
        """
        from rootcoz import main as main_mod

        headers = await self._admin("chat-evict-post", github_token="ghp_row_token")
        for _ in range(main_mod._ISSUE_DRAFTS_PER_USER):
            await self._propose_only(test_client, "github", headers)
        real_post = main_mod._post_issue_consent_request
        posted_codes: list[str] = []

        async def _evict_during_post(**kwargs: str) -> int:
            """Write the notice, then let a newer proposal evict that draft."""
            message_id = await real_post(**kwargs)
            posted_codes.append(kwargs["code"])
            for draft_id, draft in list(main_mod._chat_issue_drafts.items()):
                if draft["code"] == kwargs["code"]:
                    main_mod._chat_issue_drafts.pop(draft_id)
            return message_id

        with (
            patch(
                "rootcoz.main._post_issue_consent_request",
                side_effect=_evict_during_post,
            ),
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=headers,
            )
        assert response.status_code == 503, response.text
        assert "propose the issue again" in response.json()["detail"].lower()
        # The confirmed preview was taken back, so no code in this chat is dead.
        messages = await storage.get_chat_messages(
            self._ADMIN_CHAT, username="chat-evict-post"
        )
        assert not [
            message for message in messages if posted_codes[0] in message["content"]
        ], messages

    @pytest.mark.asyncio
    async def test_parallel_proposals_cannot_exceed_the_per_account_cap(
        self, test_client
    ):
        """The check and the insert are one step, so N proposals keep five drafts.

        Every proposal is held inside the watermark read together: the caps used
        to be checked before that read and the insert after it, so all of them
        passed and all of them inserted. A proposal a burst evicted while its
        server notice was being written is refused (503), never returned with a
        code whose draft is already gone.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from rootcoz import main as main_mod

        headers = await self._admin("chat-propose-race", github_token="ghp_row_token")
        start = threading.Barrier(8, timeout=5)

        async def _slow_watermark(_conversation: str, _username: str) -> int:
            await asyncio.sleep(0.2)
            return 0

        def _propose() -> int:
            start.wait()
            return test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=headers,
            ).status_code

        with (
            patch("rootcoz.main._chat_last_message_id", new=_slow_watermark),
            _with_github_issue_config(),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            statuses = [
                future.result(timeout=30)
                for future in [pool.submit(_propose) for _ in range(8)]
            ]

        assert set(statuses) <= {201, 503}, statuses
        assert statuses.count(201) >= 1, statuses
        live = [
            draft
            for draft in main_mod._chat_issue_drafts.values()
            if draft["username"] == "chat-propose-race"
        ]
        assert len(live) == main_mod._ISSUE_DRAFTS_PER_USER

    @pytest.mark.asyncio
    async def test_parallel_proposals_cannot_exceed_the_global_cap(
        self, test_client, monkeypatch
    ):
        """A full store refuses the parallel proposals instead of growing."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from rootcoz import main as main_mod

        monkeypatch.setattr(main_mod, "_ISSUE_DRAFTS_MAX", 2)
        headers = await self._admin("chat-global-race", github_token="ghp_row_token")
        start = threading.Barrier(6, timeout=5)

        async def _slow_watermark(_conversation: str, _username: str) -> int:
            await asyncio.sleep(0.2)
            return 0

        def _propose() -> int:
            start.wait()
            return test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=headers,
            ).status_code

        with (
            patch("rootcoz.main._chat_last_message_id", new=_slow_watermark),
            _with_github_issue_config(),
            ThreadPoolExecutor(max_workers=6) as pool,
        ):
            statuses = [
                future.result(timeout=30)
                for future in [pool.submit(_propose) for _ in range(6)]
            ]

        assert sorted(statuses) == [201, 201, 503, 503, 503, 503], statuses
        assert len(main_mod._chat_issue_drafts) == 2

    @pytest.mark.asyncio
    async def test_a_consent_read_error_leaves_the_draft_usable(self, test_client):
        """A storage error is not a decision: no new approval is needed."""
        from rootcoz import main as main_mod

        headers = await self._admin("chat-read-error", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with (
            patch(
                "rootcoz.storage.get_chat_user_messages_since",
                side_effect=RuntimeError("storage is down"),
            ),
            _with_github_issue_config(),
            pytest.raises(RuntimeError),
        ):
            test_client.post("/api/chat-issue", json=draft, headers=headers)
        # The claim was handed back with the error, not swallowed by it.
        assert draft["confirmation_id"] in main_mod._chat_issue_drafts
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/22",
                "number": 22,
            }
            with _with_github_issue_config():
                retried = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert retried.status_code == 201, retried.text

    @pytest.mark.asyncio
    async def test_a_confirmation_buried_under_later_replies_still_counts(
        self, test_client
    ):
        """Later chat cannot hide the user's confirmation from the scan."""
        from rootcoz import main as main_mod

        headers = await self._admin("chat-buried", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        for _ in range(main_mod._CONFIRM_SCAN_MESSAGES + 5):
            await self._user_reply(headers, "no code in this one")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/23",
                "number": 23,
            }
            with _with_github_issue_config():
                response = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert response.status_code == 201, response.text

    @pytest.mark.asyncio
    async def test_an_upstream_outage_leaves_the_confirmation_usable(self, test_client):
        """A 5xx is not a decision: the same id and code file once it recovers."""
        headers = await self._admin("chat-outage", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.HTTPStatusError(
                "outage",
                request=httpx.Request("POST", "https://api.github.com"),
                response=httpx.Response(502),
            )
            with _with_github_issue_config():
                failed = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert failed.status_code == 502, failed.text
        assert "Try again in a few minutes" in failed.json()["detail"]
        # The user retries with exactly what they approved — no new preview.
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/21",
                "number": 21,
            }
            with _with_github_issue_config():
                retried = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert retried.status_code == 201, retried.text
        assert mock_create.call_count == 1
        # Filed once is spent: the retry was the single successful use.
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            again = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert again.status_code == 400
        assert "already used" in again.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_timed_out_create_spends_the_confirmation(self, test_client):
        """A timeout may have filed the issue: the code must not file a second.

        The refusal itself says the issue may already exist, so handing the draft
        back and letting the model retry the same confirmation would create a
        duplicate without the user approving a retry.
        """
        headers = await self._admin("chat-timeout", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.TimeoutException("timed out")
            with _with_github_issue_config():
                timed_out = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert timed_out.status_code == 502, timed_out.text
        assert "may already be filed" in timed_out.json()["detail"]
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/25",
                "number": 25,
            }
            retried = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert retried.status_code == 400, retried.text
        assert "unknown, expired or already used" in retried.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_create_that_never_reached_the_tracker_keeps_the_code(
        self, test_client
    ):
        """A connect error proves nothing was sent, so the confirmation stays."""
        headers = await self._admin("chat-connect", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.ConnectError("connection refused")
            with _with_github_issue_config():
                unreachable = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert unreachable.status_code == 502, unreachable.text
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/24",
                "number": 24,
            }
            with _with_github_issue_config():
                retried = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert retried.status_code == 201, retried.text

    @pytest.mark.asyncio
    async def test_a_dropped_read_spends_the_confirmation(self, test_client):
        """The POST reached GitHub and the answer was lost: the draft is spent.

        A read error happens after the request was sent, so nothing proves it
        filed nothing — releasing the draft would let the same confirmation file
        the issue a second time.
        """
        headers = await self._admin("chat-read-error", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = httpx.ReadError("connection reset")
            with _with_github_issue_config():
                failed = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert failed.status_code == 502, failed.text
        assert "may have been filed" in failed.json()["detail"]
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            retried = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert retried.status_code == 400, retried.text
        assert "unknown, expired or already used" in retried.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_spends_the_confirmation(self, test_client):
        """A 2xx whose body cannot be parsed may still have filed the issue."""
        headers = await self._admin("chat-parse-error", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.side_effect = KeyError("html_url")
            with _with_github_issue_config():
                failed = test_client.post(
                    "/api/chat-issue", json=draft, headers=headers
                )
        assert failed.status_code == 502, failed.text
        assert "may have been filed" in failed.json()["detail"]
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            retried = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert retried.status_code == 400, retried.text
        assert "unknown, expired or already used" in retried.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reply_from_before_the_draft_does_not_confirm(self, test_client):
        """The code only counts in a message stored AFTER the preview."""
        headers = await self._admin("chat-stale-reply", github_token="ghp_row_token")
        # A reply sent for an earlier draft, carrying a code this draft never had.
        await self._user_reply(headers, "ABCDEF")
        draft = await self._propose_only(test_client, "github", headers)
        await self._user_reply(headers, "ABCDEF")
        # Rewind the watermark: the only matching reply is now older than the draft.
        from rootcoz import main as main_mod

        main_mod._chat_issue_drafts[draft["confirmation_id"]]["watermark"] = 10**6
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post("/api/chat-issue", json=draft, headers=headers)
        assert response.status_code == 400, response.text
        assert "nobody confirmed this draft" in response.json()["detail"]
        mock_create.assert_not_called()

    # -- audit trail ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_an_internal_failure_still_writes_a_row(self, test_client):
        """A 500 is an attempt too: one failure row, no token and no issue text."""
        headers = await self._admin("chat-boom", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers)
        with (
            patch(
                "rootcoz.main._create_issue_from_text",
                new_callable=AsyncMock,
                side_effect=RuntimeError("storage is down"),
            ),
            _with_github_issue_config(),
            pytest.raises(RuntimeError),
        ):
            test_client.post("/api/chat-issue", json=draft, headers=headers)
        audit = await self._chat_audit()
        assert audit["total"] == 1
        event = audit["events"][0]
        assert event["outcome"] == "failure"
        assert event["details"]["reason"] == "internal_error"
        assert event["details"]["tracker"] == "github"
        assert "ghp_row_token" not in json.dumps(event)
        assert self._CHAT["title"] not in json.dumps(event)
        assert self._CHAT["body"] not in json.dumps(event)

    @pytest.mark.asyncio
    async def test_a_refusal_records_the_upstream_status(self, test_client):
        """No permission (403) and rate limited (429) are told apart in the row."""
        import httpx

        headers = await self._admin("chat-upstream", github_token="ghp_row_token")
        for upstream, response_headers in (
            (403, {}),
            (429, {"x-ratelimit-remaining": "0"}),
        ):
            draft = await self._propose(test_client, "github", headers)
            with patch("rootcoz.main.create_github_issue") as mock_create:
                mock_create.side_effect = httpx.HTTPStatusError(
                    "refused",
                    request=httpx.Request("POST", "https://api.github.com"),
                    response=httpx.Response(upstream, headers=response_headers),
                )
                with _with_github_issue_config():
                    response = test_client.post(
                        "/api/chat-issue", json=draft, headers=headers
                    )
            assert response.status_code in (403, 429), response.text
            refused = (await self._chat_audit())["events"][0]
            assert refused["details"]["upstream_status"] == upstream
            # A create that filed nothing names no issue.
            assert "url" not in refused["details"]
            assert "number" not in refused["details"]

    @pytest.mark.asyncio
    async def test_a_full_draft_store_audits_the_refused_proposal(
        self, test_client, monkeypatch
    ):
        """Storing the draft is part of the attempt, so its 503 leaves a row."""
        from rootcoz import main as main_mod

        monkeypatch.setattr(main_mod, "_ISSUE_DRAFTS_MAX", 1)
        first = await self._admin("chat-store-full-a", github_token="ghp_row_token")
        await self._propose_only(test_client, "github", first)
        second = await self._admin("chat-store-full-b", github_token="ghp_row_token")
        with _with_github_issue_config():
            proposal = test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=second,
            )
        assert proposal.status_code == 503, proposal.text
        audit = await self._chat_audit()
        assert audit["total"] == 1, audit
        event = audit["events"][0]
        assert event["outcome"] == "failure"
        assert event["details"]["step"] == "propose"
        assert event["details"]["reason"] == "http_503"

    @pytest.mark.asyncio
    async def test_a_draft_storage_failure_leaves_a_failure_row(self, test_client):
        """A storage error while drafting is an attempt too — no draft text."""
        headers = await self._admin("chat-store-boom", github_token="ghp_row_token")
        with (
            patch(
                "rootcoz.main._chat_last_message_id",
                side_effect=RuntimeError("storage is down"),
            ),
            _with_github_issue_config(),
            pytest.raises(RuntimeError),
        ):
            test_client.post(
                "/api/chat-issue/propose",
                json={
                    **self._CHAT,
                    "target": "github",
                    "conversation": self._ADMIN_CHAT,
                },
                headers=headers,
            )
        audit = await self._chat_audit()
        assert audit["total"] == 1, audit
        event = audit["events"][0]
        assert event["outcome"] == "failure"
        assert event["details"]["reason"] == "internal_error"
        assert event["details"]["tracker"] == "github"
        assert self._CHAT["title"] not in json.dumps(event)
        assert self._CHAT["body"] not in json.dumps(event)

    # -- the destination the draft binds --------------------------------------

    @pytest.mark.asyncio
    async def test_an_approved_draft_cannot_switch_the_repo(self, test_client):
        """The id approves this text for this repo — not the same text elsewhere."""
        headers = await self._admin("chat-repo-switch", github_token="ghp_row_token")
        draft = await self._propose(test_client, "github", headers, repo="org/one")
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/one/issues/1",
                "number": 1,
            }
            with _with_github_issue_config():
                elsewhere = test_client.post(
                    "/api/chat-issue",
                    json={**draft, "repo": "org/two"},
                    headers=headers,
                )
                approved = test_client.post(
                    "/api/chat-issue",
                    json={**draft, "repo": "org/one"},
                    headers=headers,
                )
        assert elsewhere.status_code == 400, elsewhere.text
        detail = elsewhere.json()["detail"]
        assert "destination" in detail
        assert "propose_tracker_issue" in detail
        # The repo the user approved still files, and only once.
        assert approved.status_code == 201, approved.text
        assert mock_create.call_count == 1
        assert mock_create.call_args[1]["repo_url"] == "https://github.com/org/one"

    @pytest.mark.asyncio
    async def test_an_approved_draft_cannot_switch_the_run(self, test_client):
        """An empty repo means the drafted run's test repo, not another run's."""
        headers = await self._user("chat-run-switch", github_token="ghp_row_token")
        await self._run("chat-run-a", tests_repo_url="https://github.com/org/a")
        await self._run("chat-run-b", tests_repo_url="https://github.com/org/b")
        draft = await self._propose(test_client, "github", headers, job_id="chat-run-a")
        with (
            patch("rootcoz.main.create_github_issue") as mock_create,
            _with_github_issue_config(),
        ):
            response = test_client.post(
                "/api/chat-issue",
                json={**draft, "job_id": "chat-run-b"},
                headers=headers,
            )
        assert response.status_code == 400, response.text
        assert "destination" in response.json()["detail"]
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_approved_draft_cannot_switch_the_jira_project(self, test_client):
        """The project, security level and issue type are the approved ones."""
        headers = await self._user(
            "chat-jira-switch", jira_token="user_jira_token", jira_email="u@example.com"
        )
        with patch("rootcoz.main.create_jira_bug") as mock_create:
            mock_create.return_value = {
                "key": "TEST-7",
                "url": "https://jira.example.com/browse/TEST-7",
            }
            with _enable_feature("jira_enabled"):
                for override in (
                    {"jira_project_key": "OTHER"},
                    {"jira_security_level": "Secret"},
                ):
                    draft = await self._propose(
                        test_client, "jira", headers, jira_project_key="TEST"
                    )
                    response = test_client.post(
                        "/api/chat-issue",
                        json={**draft, "jira_project_key": "TEST", **override},
                        headers=headers,
                    )
                    assert response.status_code == 400, response.text
                    assert "destination" in response.json()["detail"]
                approved = await self._propose(
                    test_client, "jira", headers, jira_project_key="TEST"
                )
                filed = test_client.post(
                    "/api/chat-issue",
                    json={**approved, "jira_project_key": "TEST"},
                    headers=headers,
                )
        assert filed.status_code == 201, filed.text
        assert mock_create.call_count == 1
        assert mock_create.call_args[1]["project_key"] == "TEST"

    @pytest.mark.asyncio
    async def test_the_default_repo_placeholder_is_the_default_repo(self, test_client):
        """An omitted ``repo`` reaches the server as ``{repo}``, not as a slug."""
        headers = await self._user("chat-placeholder", github_token="ghp_row_token")
        job_id = await self._run(
            "chat-placeholder-run", tests_repo_url="https://github.com/org/repo"
        )
        draft = await self._propose(
            test_client, "github", headers, job_id=job_id, repo="{repo}"
        )
        with patch("rootcoz.main.create_github_issue") as mock_create:
            mock_create.return_value = {
                "url": "https://github.com/org/repo/issues/3",
                "number": 3,
            }
            with _with_github_issue_config():
                response = test_client.post(
                    "/api/chat-issue",
                    json={**draft, "job_id": job_id, "repo": "{repo}"},
                    headers=headers,
                )
        assert response.status_code == 201, response.text
        assert mock_create.call_args[1]["repo_url"] == "https://github.com/org/repo"


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

    def test_history_and_ai_models_require_auth(
        self, mock_settings, temp_db_path: Path
    ) -> None:
        """Handler-level auth gates reject anonymous callers (middleware + explicit)."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            from starlette.testclient import TestClient

            from rootcoz.main import app

            with TestClient(app) as client:
                assert client.get("/history/test/some.test").status_code == 401
                assert client.get("/history/classifications").status_code == 401
                assert client.get("/api/ai-models").status_code == 401

    def test_history_and_ai_models_handlers_call_require_authenticated(self) -> None:
        """Source of gated handlers must invoke ``_require_authenticated``."""
        import inspect

        from rootcoz import main as main_mod

        for fn in (
            main_mod.get_test_history_endpoint,
            main_mod.get_classifications,
            main_mod.list_ai_models,
            main_mod.search_by_signature_endpoint,
            main_mod.get_job_stats_endpoint,
        ):
            src = inspect.getsource(fn)
            assert "_require_authenticated" in src, fn.__name__

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

    @pytest.mark.asyncio
    async def test_ai_pattern_classification_preserves_human_primary_override(
        self, test_client
    ):
        """AI records its hidden pattern without replacing a human root-cause override."""
        job_id = "job-ai-pattern-with-override"
        test_name = "test_user_override"
        await storage.save_result(
            job_id,
            "http://jenkins",
            "completed",
            {
                "status": "completed",
                "summary": "",
                "failures": [
                    {
                        "test_name": test_name,
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG"},
                    }
                ],
            },
        )
        override = test_client.put(
            f"/results/{job_id}/override-classification",
            json={"test_name": test_name, "classification": "CODE ISSUE"},
        )
        assert override.status_code == 200

        response = test_client.post(
            "/history/classify",
            json={
                "test_name": test_name,
                "classification": "FLAKY",
                "reason": "Repeated intermittent failure",
                "job_id": job_id,
                "source": "ai",
            },
        )
        assert response.status_code == 201
        assert response.json()["id"] is not None

        assert (
            await storage.get_effective_classification(job_id, test_name)
            == "CODE ISSUE"
        )
        assert await storage.get_history_classification(job_id, test_name) == ""

        async with aiosqlite.connect(storage.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT classification, pattern, created_by, visible "
                "FROM test_classifications WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
            classifications = await cursor.fetchall()
        assert ("CODE ISSUE", "", "admin", 1) in classifications
        assert ("", "FLAKY", "rootcoz-ai", 0) in classifications


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
    """Tests for Jenkins wait-for-completion via the shared CI source path."""

    @pytest.mark.asyncio
    async def test_wait_for_completion_true_waits(self, temp_db_path: Path) -> None:
        """When wait_for_completion=True, sets status to waiting and polls."""
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        body = UnifiedAnalyzeRequest(
            type="jenkins",
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

        passed = CISourceResult(
            failures=[],
            skip_analysis=True,
            build_url="https://jenkins.example.com/job/my-job/1/",
            identity={"job_name": "my-job", "build_number": 1},
        )

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.sources.jenkins_source.wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ) as mock_wait,
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
                new_callable=AsyncMock,
                return_value=passed,
            ),
            patch("rootcoz.main.update_status", side_effect=capture_status),
            patch("rootcoz.main.safe_update_progress", new_callable=AsyncMock),
            _patch_preflight(),
        ):
            await storage.init_db()
            await storage.save_result("test-id", "", "pending", {"job_name": "my-job"})
            await _process_ci_source_analysis(
                job_id="test-id",
                body=body,
                merged=merged,
                display_name="my-job",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )
            mock_wait.assert_called_once()
            assert "waiting" in statuses
            assert "completed" in statuses

    @pytest.mark.asyncio
    async def test_wait_failure_marks_job_failed(self, temp_db_path: Path) -> None:
        """When Jenkins wait fails, job is marked failed without fetching."""
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest

        body = UnifiedAnalyzeRequest(
            type="jenkins",
            job_name="my-job",
            build_number=1,
            wait_for_completion=True,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            jenkins_user="user",
            jenkins_password=FAKE_JENKINS_PASSWORD,
            wait_for_completion=True,
        )

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.sources.jenkins_source.wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(False, "timed out"),
            ),
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
                new_callable=AsyncMock,
            ) as mock_fetch,
            patch("rootcoz.main.safe_update_progress", new_callable=AsyncMock),
        ):
            await storage.init_db()
            await storage.save_result("test-id", "", "waiting", {"job_name": "my-job"})
            await _process_ci_source_analysis(
                job_id="test-id",
                body=body,
                merged=merged,
                display_name="my-job",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )
            mock_fetch.assert_not_called()
            row = await storage.get_result("test-id")
            assert row is not None
            assert row["status"] == "failed"
            assert "timed out" in (row["result"].get("error") or "")

    @pytest.mark.asyncio
    async def test_wait_skipped_when_disabled(self, temp_db_path: Path) -> None:
        """When wait_for_completion=False, pre_fetch wait is not invoked."""
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        body = UnifiedAnalyzeRequest(
            type="jenkins",
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
        passed = CISourceResult(
            failures=[],
            skip_analysis=True,
            build_url="https://jenkins.example.com/job/my-job/1/",
        )

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.sources.jenkins_source.wait_for_jenkins_completion",
                new_callable=AsyncMock,
            ) as mock_wait,
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
                new_callable=AsyncMock,
                return_value=passed,
            ),
            _patch_preflight(),
        ):
            await storage.init_db()
            await storage.save_result("test-id", "", "pending", {"job_name": "my-job"})
            await _process_ci_source_analysis(
                job_id="test-id",
                body=body,
                merged=merged,
                display_name="my-job",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )
            mock_wait.assert_not_called()


class TestBuildRequestParams:
    """Tests for _build_jenkins_request_params helper."""

    def test_serializes_all_fields(self, mock_settings) -> None:
        """All expected fields are present in the returned dict."""
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job",
            build_number=1,
            ai_provider="gemini",
            ai_model="gemini-pro",
        )
        settings = Settings()
        params = _build_jenkins_request_params(body, settings, "gemini", "gemini-pro")
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
        params = _build_jenkins_request_params(body, merged, "", "")
        # Sensitive fields must carry the encryption prefix
        for key in SENSITIVE_KEYS:
            if params.get(key):
                assert params[key].startswith(_ENCRYPTED_PREFIX)
        # Specifically, the github_token must NOT be plaintext
        assert params["github_token"] != FAKE_GITHUB_TOKEN
        assert params["github_token"].startswith(_ENCRYPTED_PREFIX)

    def test_build_params_persists_labels(self, mock_settings) -> None:
        """labels are persisted in request_params."""
        from rootcoz.config import get_settings
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        settings = get_settings()
        body = AnalyzeRequest(
            job_name="my-job",
            build_number=42,
            ai_provider="claude",
            ai_model="opus",
            labels=["Nightly", "CNV"],
        )
        merged = _merge_settings(body, settings)
        params = _build_jenkins_request_params(body, merged, "claude", "opus")
        assert params.get("labels") == ["Nightly", "CNV"]


class TestMergeSettingsJiraServerMode:
    """Jira credentials are Server Settings only — requests cannot touch them.

    Covers the removal of per-request Jira credentials: the request body has
    no Jira credential fields, and the server-selected ``jira_auth_mode`` and
    credentials always win.
    """

    @staticmethod
    def _server_settings(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "jira_server_url": "https://jira.example.com",
            "jira_pat": "server-pat",
            "jira_oauth_client_secret": SecretStr("server-oauth"),
        }
        base.update(overrides)
        return Settings(**base)

    @pytest.mark.parametrize("server_mode", ["", "basic", "pat", "oauth"])
    def test_no_request_creds_keep_server_mode(
        self, mock_settings, server_mode
    ) -> None:
        """Without request credentials the server mode is untouched."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        settings = self._server_settings(jira_auth_mode=server_mode)
        merged = _merge_settings(AnalyzeRequest(job_name="j", build_number=1), settings)
        assert merged.jira_auth_mode == (server_mode or None)

    @pytest.mark.parametrize(
        "cred_field",
        ["jira_email", "jira_api_token", "jira_pat", "jira_oauth_client_secret"],
    )
    def test_request_models_have_no_jira_credential_fields(
        self, cred_field: str
    ) -> None:
        """The request models no longer define Jira credential fields."""
        from rootcoz.models import AnalyzeRequest, UnifiedAnalyzeRequest

        for model in (AnalyzeRequest, UnifiedAnalyzeRequest):
            assert cred_field not in model.model_fields

    def test_request_payload_jira_creds_are_ignored(self, mock_settings) -> None:
        """Extra Jira credential keys in a payload cannot influence settings."""
        from rootcoz.models import AnalyzeRequest

        # Pydantic ignores unknown fields, so a legacy client still sending
        # Jira credentials gets them silently dropped — never applied.
        body = AnalyzeRequest.model_validate(
            {
                "job_name": "j",
                "build_number": 1,
                "jira_email": "req@example.com",
                "jira_api_token": "req-tok",
                "jira_pat": "req-pat",
                "jira_oauth_client_secret": "req-oauth",  # pragma: allowlist secret
            }
        )
        settings = self._server_settings(jira_auth_mode="basic")
        from rootcoz.main import _merge_settings

        merged = _merge_settings(body, settings)
        assert merged.jira_auth_mode == "basic"
        assert not merged.jira_email
        assert merged.jira_api_token is None
        assert merged.jira_pat.get_secret_value() == "server-pat"
        assert merged.jira_oauth_client_secret.get_secret_value() == "server-oauth"


class TestReconstructFromParams:
    """Tests for _reconstruct_from_params helper."""

    def test_reconstructs_body_and_settings(self, mock_settings) -> None:
        """AnalyzeRequest and Settings are reconstructed from stored params.

        Uses _build_jenkins_request_params to produce the persisted payload, validating
        the round-trip serializer/encryption contract.
        """
        from rootcoz.config import get_settings
        from rootcoz.main import (
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
        request_params = _build_jenkins_request_params(
            body_in, merged_in, "claude", "opus"
        )
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

    def test_reconstruct_preserves_labels(self, mock_settings) -> None:
        """labels survive persist → reconstruct round-trip."""
        from rootcoz.main import _reconstruct_from_params

        result_data = {
            "job_name": "j",
            "build_number": 1,
            "request_params": {
                "ai_provider": "claude",
                "ai_model": "m",
                "labels": ["Nightly", "CNV"],
            },
        }
        body, _ = _reconstruct_from_params(result_data)
        assert body.labels == ["Nightly", "CNV"]

    def test_reconstruct_defaults_empty_labels(self, mock_settings) -> None:
        """Missing labels in params defaults to empty list."""
        from rootcoz.main import _reconstruct_from_params

        result_data = {
            "job_name": "j",
            "build_number": 1,
            "request_params": {
                "ai_provider": "claude",
                "ai_model": "m",
            },
        }
        body, _ = _reconstruct_from_params(result_data)
        assert body.labels == []

    def test_reconstruct_reads_legacy_metadata_labels(self, mock_settings) -> None:
        """Legacy request_params with metadata_labels key are read correctly."""
        from rootcoz.main import _reconstruct_from_params

        result_data = {
            "job_name": "j",
            "build_number": 1,
            "request_params": {
                "ai_provider": "claude",
                "ai_model": "m",
                "metadata_labels": ["Nightly", "CNV"],
            },
        }
        body, _ = _reconstruct_from_params(result_data)
        assert body.labels == ["Nightly", "CNV"]

    def test_copy_analysis_settings_legacy_metadata_labels(self, mock_settings) -> None:
        """Old params with metadata_labels are copied as labels for re-analyze."""
        from rootcoz.main import _copy_analysis_settings

        unified_fields: dict = {}
        _copy_analysis_settings(
            {"metadata_labels": ["Nightly", "CNV"]},
            unified_fields,
        )
        assert unified_fields["labels"] == ["Nightly", "CNV"]


class TestResumeWaitingJobs:
    """Tests for _resume_waiting_jobs helper."""

    @pytest.mark.parametrize("failure", ["missing_submitter", "invalid_peer"])
    async def test_validation_failure_does_not_block_next_job(
        self, failure, mock_settings, temp_db_path: Path, monkeypatch
    ) -> None:
        """Fail only the invalid waiting job; retain grants for the next one."""
        from fastapi import HTTPException

        from rootcoz.config import get_settings
        from rootcoz.main import _resume_waiting_jobs
        from rootcoz.models import AnalyzeRequest

        grant = AsyncMock(return_value=True)
        monkeypatch.setattr(storage, "can_user_use_server_providers", grant)
        monkeypatch.setattr(
            storage, "get_user_ai_credentials", AsyncMock(return_value={})
        )
        body = AnalyzeRequest(
            job_name="my-job", build_number=1, ai_provider="gemini", ai_model="m"
        )
        params = _build_jenkins_request_params(body, get_settings(), "gemini", "m")
        params["submitted_by"] = "admin"
        first_params = dict(params)
        if failure == "missing_submitter":
            first_params.pop("submitted_by")
        jobs = [
            {
                "job_id": job_id,
                "result_data": {
                    "job_name": "my-job",
                    "build_number": 1,
                    "request_params": request_params,
                },
            }
            for job_id, request_params in (("bad", first_params), ("good", params))
        ]
        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock
            ) as process,
        ):
            await storage.init_db()
            for job in jobs:
                await storage.save_result(
                    job["job_id"], "http://j/1", "waiting", job["result_data"]
                )
            if failure == "invalid_peer":
                monkeypatch.setattr(
                    "rootcoz.main._validate_peer_configs",
                    AsyncMock(
                        side_effect=[HTTPException(422, "Invalid peer model"), None]
                    ),
                )
            await _resume_waiting_jobs(jobs)
            await asyncio.sleep(0)
            bad = await storage.get_result("bad")
            assert bad["status"] == "failed"
            assert "request_params" in bad["result"]
            assert (
                "grant" if failure == "missing_submitter" else "Invalid peer model"
            ) in bad["result"]["error"]
            process.assert_called_once()
            assert process.call_args.kwargs["job_id"] == "good"
            assert (await storage.get_result("good"))["status"] == "waiting"
            assert grant.await_count >= 1
            assert all(call.args == ("admin",) for call in grant.await_args_list)

    async def test_resume_validation_error_does_not_leak_exception(
        self, mock_settings, monkeypatch, caplog
    ) -> None:
        from rootcoz.config import get_settings
        from rootcoz.main import _resume_waiting_jobs
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="my-job", build_number=1, ai_provider="gemini", ai_model="m"
        )
        params = _build_jenkins_request_params(body, get_settings(), "gemini", "m")
        params["submitted_by"] = "admin"
        result_data = {"job_name": "my-job", "request_params": params}
        monkeypatch.setattr(
            "rootcoz.main._validate_peer_configs",
            AsyncMock(side_effect=RuntimeError("secret-api-key")),
        )
        with (
            patch.object(storage, "get_result", AsyncMock(return_value=None)),
            patch.object(storage, "update_status", new_callable=AsyncMock) as update,
            patch(
                "rootcoz.main._resolve_ai_config_allow_defer", new_callable=AsyncMock
            ),
        ):
            await _resume_waiting_jobs([{"job_id": "bad", "result_data": result_data}])
        assert update.await_args.args[1] == "failed"
        assert "secret-api-key" not in update.await_args.args[2]["error"]
        assert "secret-api-key" not in caplog.text

    async def test_resumes_valid_waiting_job(self, mock_settings, monkeypatch) -> None:
        """A waiting job with valid request_params spawns a background task."""
        monkeypatch.setattr(
            storage, "can_user_use_server_providers", AsyncMock(return_value=True)
        )
        from rootcoz.config import get_settings
        from rootcoz.main import _resume_waiting_jobs
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
        request_params = _build_jenkins_request_params(body_in, settings, "gemini", "m")
        request_params["submitted_by"] = "admin"
        monkeypatch.setattr(
            storage, "get_user_ai_credentials", AsyncMock(return_value={})
        )
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
                "rootcoz.main._process_ci_source_analysis",
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
            call_kwargs = mock_process.call_args.kwargs
            assert call_kwargs["job_id"] == "w-1"
            resumed_body = call_kwargs["body"]
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
                "INSERT INTO results (job_id, build_url, status, result_json) VALUES (?, ?, ?, ?)",
                (job_id, jenkins_url, status, result_json),
            )
        conn.commit()
        conn.close()

    def test_lifespan_resumes_waiting_jobs(
        self, mock_settings, temp_db_path: Path, monkeypatch
    ) -> None:
        """Waiting jobs are resumed (not failed) when the app starts."""
        monkeypatch.setattr(
            storage, "can_user_use_server_providers", AsyncMock(return_value=True)
        )
        import json

        from rootcoz.config import get_settings
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
        request_params = _build_jenkins_request_params(body_in, settings, "gemini", "m")
        request_params["submitted_by"] = "admin"
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

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.main._process_ci_source_analysis",
                new_callable=AsyncMock,
            ) as mock_process,
        ):
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


class TestStartupWorkspaceReclaim:
    """Startup is the one moment no builder can still own a workspace (#424)."""

    async def test_abandoned_workspace_and_its_graph_root_are_reclaimed(
        self, tmp_path, monkeypatch
    ) -> None:
        """An analysis whose cleanup abandoned must not survive the next start.

        ``ABANDON`` stops ``repo_manager.cleanup``, so the uuid workspace and its
        sibling graph root stay on disk with no owner: nothing in a later process
        ever looks at that name again.
        """
        from rootcoz import main as main_mod
        from rootcoz import temp_paths

        base = tmp_path / "rootcoz"
        workspace = base / "workspace-abandoned"
        (workspace / "tests-repo" / ".git").mkdir(parents=True)
        (workspace / "tests-repo" / "file.py").write_text("x")
        graph = base / ".workspace-abandoned.rootcoz-graft"
        (graph / "tests-repo" / "graph").mkdir(parents=True)
        chat = base / "chat-job-1" / "alice"
        chat.mkdir(parents=True)
        # The leftovers predate this process: only then is nothing building them.
        for stale in (workspace, graph):
            os.utime(stale, (1, 1))
        monkeypatch.setattr(temp_paths, "rootcoz_temp_base", lambda: base)

        assert await main_mod._reclaim_abandoned_workspaces() == 1

        assert not workspace.exists(), "the abandoned clone tree survived the restart"
        assert not graph.exists(), "the abandoned graph root survived the restart"
        assert chat.exists(), "a chat workspace keeps its session across a restart"

    async def test_a_workspace_created_after_start_is_left_alone(
        self, tmp_path, monkeypatch
    ) -> None:
        """A workspace this process created (or another replica's) is not a leftover."""
        from rootcoz import main as main_mod
        from rootcoz import temp_paths

        base = tmp_path / "rootcoz"
        live = base / "workspace-live"
        (live / "tests-repo").mkdir(parents=True)
        monkeypatch.setattr(temp_paths, "rootcoz_temp_base", lambda: base)

        assert await main_mod._reclaim_abandoned_workspaces() == 0
        assert live.exists(), "the reclaim deleted a workspace it does not own"

    async def test_a_workspace_symlink_into_a_chat_is_never_followed(
        self, tmp_path, monkeypatch
    ) -> None:
        """A stale ``workspace-*`` link must not aim the graph delete at a chat.

        ``_root`` resolves its argument, so reclaiming the link would delete
        ``chat-job-1/.alice.rootcoz-graft`` — the graph of a chat workspace the
        reclaim deliberately never touches.
        """
        from rootcoz import main as main_mod
        from rootcoz import temp_paths

        base = tmp_path / "rootcoz"
        chat = base / "chat-job-1" / "alice"
        chat.mkdir(parents=True)
        graph = base / "chat-job-1" / ".alice.rootcoz-graft"
        (graph / "alice" / "graph").mkdir(parents=True)
        (graph / "alice" / "graph" / "index.bin").write_text("graph")
        link = base / "workspace-fake"
        link.symlink_to(chat)
        for stale in (link, graph):
            os.utime(stale, (1, 1), follow_symlinks=False)
        monkeypatch.setattr(temp_paths, "rootcoz_temp_base", lambda: base)

        reclaimed = await main_mod._reclaim_abandoned_workspaces()

        assert graph.exists(), "startup followed a workspace link into a chat graph"
        assert link.is_symlink(), "a link is not a leftover workspace to delete"
        assert reclaimed == 0

    async def test_a_deleted_jobs_marked_chat_workspace_is_reclaimed(
        self, tmp_path, monkeypatch
    ) -> None:
        """No init can recover it: the job row is gone, so every init 404s first.

        An abandoned delete leaves the chat tree and its marker behind, and
        recovery only runs for a create of a job that still exists.
        """
        from rootcoz import main as main_mod
        from rootcoz import temp_paths
        from rootcoz.temp_paths import delete_marker

        base = tmp_path / "rootcoz"
        chat = base / "chat-job-1"
        (chat / "alice" / "repo" / ".git").mkdir(parents=True)
        marker = delete_marker(chat)
        marker.write_text("", encoding="utf-8")
        for stale in (chat, marker):
            os.utime(stale, (1, 1))
        monkeypatch.setattr(temp_paths, "rootcoz_temp_base", lambda: base)

        assert await main_mod._reclaim_abandoned_workspaces() == 1
        assert not chat.exists(), "a deleted job's marked chat survived the restart"

    async def test_a_missing_base_directory_is_not_a_listing_failure(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """A fresh container has no base yet: nothing to reclaim, no warning.

        ``rootcoz_temp_base`` only joins a path and the first ``mkdir`` happens in
        ``RepositoryManager.__init__``, i.e. after this task starts, so a first
        boot always lists a directory that does not exist.
        """
        from rootcoz import main as main_mod
        from rootcoz import temp_paths

        base = tmp_path / "rootcoz"
        monkeypatch.setattr(temp_paths, "rootcoz_temp_base", lambda: base)

        with capture_main_logs(caplog):
            assert await main_mod._reclaim_abandoned_workspaces() == 0

        assert "reclaimed" not in caplog.text
        assert "abandoned workspaces stay on disk" not in caplog.text, (
            "a first boot claimed abandoned workspaces it could not list"
        )

    async def test_an_incomplete_reclaim_is_not_reported_as_deleted(
        self, tmp_path, monkeypatch
    ) -> None:
        """A delete that leaves paths behind reclaimed nothing; the count says so."""
        from rootcoz import main as main_mod
        from rootcoz import temp_paths
        from rootcoz.temp_paths import DeleteResult

        base = tmp_path / "rootcoz"
        workspace = base / "workspace-stuck"
        workspace.mkdir(parents=True)
        os.utime(workspace, (1, 1))
        monkeypatch.setattr(temp_paths, "rootcoz_temp_base", lambda: base)
        # Paths survive: the delete reports them instead of raising (delete_tree).
        monkeypatch.setattr(
            main_mod, "delete_tree", lambda path: DeleteResult(0, 0, failed=1)
        )

        assert await main_mod._reclaim_abandoned_workspaces() == 0, (
            "a workspace whose delete failed is reported as deleted"
        )
        assert workspace.exists()


class TestAnalysisWorkspaceReap:
    """The one reap sequence both analysis ``finally`` blocks run (#424)."""

    async def test_the_ai_session_token_outlives_the_cleanup_steps(
        self, monkeypatch
    ) -> None:
        """Revoke last: a cancelled run's sidecar prompt may still call tools.

        Analyses never abort their sidecar session (only chat does), so revoking
        the few-minute-old token before the workspace delete only makes that
        prompt's history tools fail while it is still executing.
        """
        from rootcoz import main as main_mod
        from rootcoz.temp_paths import CleanupOutcome

        order: list[str] = []

        async def fake_cleanup_off_loop(what, *steps, always=()):
            # The queued delete is still running when the revoke used to be
            # scheduled, which is the window this pins.
            await asyncio.sleep(0.05)
            for step in steps:
                step()
            return CleanupOutcome.CLEAN

        async def fake_cleanup_ai_session(auth_header: str) -> None:
            order.append("revoke")

        monkeypatch.setattr(main_mod, "cleanup_off_loop", fake_cleanup_off_loop)
        monkeypatch.setattr(main_mod, "_cleanup_ai_session", fake_cleanup_ai_session)

        await main_mod._reap_analysis_workspace(
            job_id="job-1",
            auth_header="Bearer token",
            source=None,
            repo_manager=SimpleNamespace(cleanup=lambda: order.append("delete")),
            repo_path=None,
        )

        assert order == ["delete", "revoke"], (
            "the token was revoked before the workspace delete finished"
        )


class TestPeerAnalysisParams:
    """Tests for peer analysis parameter pass-through."""

    def test_analyze_with_peer_ai_configs_in_body(self, test_client) -> None:
        """POST /analyze with peer_ai_configs passes them to process_analysis_with_id."""
        with patch("rootcoz.main._process_ci_source_analysis") as mock_process:
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
            assert mock_process.called
            body_arg = mock_process.call_args.kwargs["body"]
            assert body_arg.peer_ai_configs == [
                AiConfigEntry(ai_provider="gemini", ai_model="pro"),
            ]
            assert body_arg.peer_analysis_max_rounds == 5

    def test_analyze_without_peers_backward_compatible(self, test_client) -> None:
        """POST /analyze without peer fields works unchanged."""
        with patch("rootcoz.main._process_ci_source_analysis") as mock_process:
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
            body_arg = mock_process.call_args.kwargs["body"]
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

    def test_merge_settings_preserves_server_sa_token_when_request_has_no_jira_creds(
        self,
    ) -> None:
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings = Settings(
            jira_oauth_client_secret=SecretStr("ATOA-server-secret"),
        )
        merged = _merge_settings(body, settings)
        assert (
            merged.jira_oauth_client_secret.get_secret_value() == "ATOA-server-secret"
        )

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

    def test_merge_settings_always_returns_copy(self) -> None:
        """_merge_settings always copies so overlay mutation cannot touch the cache."""
        from rootcoz.main import _merge_settings
        from rootcoz.models import AnalyzeRequest
        from rootcoz.rootcoz_repo_settings import propagate_repo_settings_overlay

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings = Settings(
            ai_call_timeout=10,
            max_concurrent_ai_calls=3,
            peer_analysis_max_rounds=3,
        )
        merged = _merge_settings(body, settings)
        assert merged is not settings
        propagate_repo_settings_overlay(
            merged,
            Settings(
                ai_call_timeout=99,
                max_concurrent_ai_calls=11,
                peer_analysis_max_rounds=7,
            ),
        )
        assert settings.ai_call_timeout == 10
        assert settings.max_concurrent_ai_calls == 3
        assert settings.peer_analysis_max_rounds == 3
        assert merged.ai_call_timeout == 99

    @pytest.mark.asyncio
    async def test_resolve_ai_config_allow_defer_respects_passed_settings(
        self, monkeypatch
    ) -> None:
        """allow_defer uses the passed Settings, not global get_settings()."""
        monkeypatch.setattr(
            storage, "can_user_use_server_providers", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            storage, "get_user_ai_credentials", AsyncMock(return_value={})
        )
        from rootcoz import ai_client

        token = ai_client.ai_username.set("admin")
        from fastapi import HTTPException

        from rootcoz.main import _resolve_ai_config_allow_defer
        from rootcoz.models import BaseAnalysisRequest

        body = BaseAnalysisRequest()
        empty = Settings(ai_provider="", ai_model="")
        # Global settings appear configured; passed settings do not and no tests repo.
        with (
            patch(
                "rootcoz.main.get_settings",
                return_value=Settings(ai_provider="claude", ai_model="opus"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _resolve_ai_config_allow_defer(body, empty)
        assert exc_info.value.status_code == 400
        assert "AI" in exc_info.value.detail

        configured = Settings(ai_provider="gemini", ai_model="flash")
        with patch(
            "rootcoz.main.get_settings",
            return_value=Settings(ai_provider="", ai_model=""),
        ):
            provider, model = await _resolve_ai_config_allow_defer(body, configured)
        assert provider == "gemini"
        assert model == "flash"
        ai_client.ai_username.reset(token)

    @pytest.mark.asyncio
    async def test_validate_catalog_pair_returns_503_when_uncached_refresh_fails(
        self,
    ) -> None:
        """A failed authoritative refresh is not an unknown-pair response."""
        from fastapi import HTTPException

        from rootcoz import ai_client
        from rootcoz.main import _validate_catalog_pair

        ai_client.update_model_catalog([{"provider": "openai", "id": "gpt-5"}])
        with (
            patch(
                "rootcoz.ai_client._list_models_raw",
                new_callable=AsyncMock,
                side_effect=RuntimeError("sidecar unavailable"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _validate_catalog_pair("openai", "gpt-5.4")
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_resolve_ai_config_allow_defer_defers_server_defaults_for_tests_repo(
        self,
    ) -> None:
        """Repository settings must beat unvalidated server defaults."""
        from rootcoz.main import _resolve_ai_config_allow_defer
        from rootcoz.models import BaseAnalysisRequest

        body = BaseAnalysisRequest(tests_repo_url="https://github.com/org/tests")
        settings = Settings(ai_provider="missing", ai_model="missing")

        assert await _resolve_ai_config_allow_defer(body, settings) == ("", "")

    @pytest.mark.asyncio
    async def test_resolve_ai_config_allow_defer_validates_explicit_pair_with_tests_repo(
        self,
    ) -> None:
        """An explicit request pair never waits for repository settings."""
        from fastapi import HTTPException

        from rootcoz.main import _resolve_ai_config_allow_defer
        from rootcoz.models import BaseAnalysisRequest

        body = BaseAnalysisRequest(
            tests_repo_url="https://github.com/org/tests",
            ai_provider="missing",
            ai_model="missing",
        )
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_ai_config_allow_defer(body, Settings())
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_resolve_ai_config_allow_defer_no_defer_on_explicit_empty_tests_repo(
        self,
    ) -> None:
        """Explicit tests_repo_url='' must not defer via server tests_repo_url."""
        from fastapi import HTTPException

        from rootcoz.main import _resolve_ai_config_allow_defer
        from rootcoz.models import BaseAnalysisRequest

        body = BaseAnalysisRequest(tests_repo_url="")
        settings = Settings(
            ai_provider="",
            ai_model="",
            tests_repo_url="https://github.com/org/tests",
        )
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_ai_config_allow_defer(body, settings)
        assert exc_info.value.status_code == 400
        assert "AI" in exc_info.value.detail

    def test_ai_not_configured_message_respects_is_admin_kwarg(self) -> None:
        from rootcoz.main import _ai_not_configured_message

        admin_msg = _ai_not_configured_message(None, "AI provider/model", is_admin=True)
        user_msg = _ai_not_configured_message(None, "AI provider/model", is_admin=False)
        assert "Server Settings" in admin_msg
        assert "contact a server administrator" in user_msg
        assert admin_msg != user_msg

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

    @pytest.mark.asyncio
    async def test_validate_peer_configs_defers_malformed_server_default_for_tests_repo(
        self,
    ) -> None:
        """A repo overlay takes priority over malformed server peer defaults."""
        from rootcoz.main import _validate_peer_configs
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
            tests_repo_url="https://github.com/org/tests",
        )
        settings = Settings(peer_ai_configs="malformed")

        assert await _validate_peer_configs(body, settings) is None

    @pytest.mark.asyncio
    async def test_validate_peer_configs_accepts_server_peer_dicts(self) -> None:
        """Server PEER_AI_CONFIGS dictionaries are catalog-validated."""
        from rootcoz.main import _validate_peer_configs
        from rootcoz.models import AnalyzeRequest

        body = AnalyzeRequest(
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        settings = Settings(peer_ai_configs="gemini:pro")
        with patch(
            "rootcoz.main._validate_catalog_pair", new_callable=AsyncMock
        ) as validate:
            peers = await _validate_peer_configs(body, settings)

        assert peers == [{"ai_provider": "gemini", "ai_model": "pro"}]
        validate.assert_awaited_once_with("gemini", "pro")

    def test_build_reconstruct_roundtrip_peer_params(self, mock_settings) -> None:
        """peer_ai_configs and peer_analysis_max_rounds round-trip through build/reconstruct."""
        from rootcoz.config import get_settings
        from rootcoz.main import (
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
        request_params = _build_jenkins_request_params(
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

    def test_build_jenkins_request_params_stores_resolved_peer_configs(
        self, mock_settings
    ) -> None:
        """_build_jenkins_request_params stores the resolved peer configs, not raw body."""
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
        params = _build_jenkins_request_params(
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
        request_params = _build_jenkins_request_params(
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
        request_params = _build_jenkins_request_params(
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
    """Tests for progress_phase updates during shared CI source analysis."""

    @pytest.mark.asyncio
    async def test_active_fetch_running_before_fetch_and_preserves_waiting(
        self, temp_db_path: Path
    ) -> None:
        """Pending work becomes running on fetch; waiting remains waiting until fetch."""
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        body = UnifiedAnalyzeRequest(
            type="jenkins",
            job_name="test",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
            wait_for_completion=True,
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            jenkins_user="user",
            jenkins_password=FAKE_JENKINS_PASSWORD,
            wait_for_completion=True,
        )
        statuses: list[str] = []

        async def wait(_source, _job_id):
            statuses.append((await storage.get_result("fetch-job"))["status"])

        async def fetch(_source):
            row = await storage.get_result("fetch-job")
            statuses.append(row["status"])
            assert row["result"]["progress_phase"] == "fetching"
            return CISourceResult(failures=[], skip_analysis=True)

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch("rootcoz.sources.jenkins_source.JenkinsSource.pre_fetch", wait),
            patch("rootcoz.sources.jenkins_source.JenkinsSource.fetch", fetch),
            _patch_preflight(),
        ):
            await storage.init_db()
            await storage.save_result("fetch-job", "", "pending", {"job_name": "test"})
            await _process_ci_source_analysis(
                job_id="fetch-job",
                body=body,
                merged=merged,
                display_name="test",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )
        assert statuses == ["waiting", "running"]

    async def test_progress_phases_with_jenkins_wait(self, temp_db_path: Path) -> None:
        """Waiting path emits waiting_for_jenkins progress phase."""
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        body = UnifiedAnalyzeRequest(
            type="jenkins",
            job_name="my-job",
            build_number=1,
            wait_for_completion=True,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com",
            jenkins_user="user",
            jenkins_password=FAKE_JENKINS_PASSWORD,
            wait_for_completion=True,
        )
        phases: list[str] = []

        async def capture_progress(job_id, phase):
            phases.append(phase)

        passed = CISourceResult(
            failures=[],
            skip_analysis=True,
            build_url="https://jenkins.example.com/job/my-job/1/",
        )

        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.sources.jenkins_source.wait_for_jenkins_completion",
                new_callable=AsyncMock,
                return_value=(True, ""),
            ),
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
                new_callable=AsyncMock,
                return_value=passed,
            ),
            patch(
                "rootcoz.main.safe_update_progress",
                side_effect=capture_progress,
            ),
            _patch_preflight(),
        ):
            await storage.init_db()
            await storage.save_result("test-id", "", "pending", {"job_name": "my-job"})
            await _process_ci_source_analysis(
                job_id="test-id",
                body=body,
                merged=merged,
                display_name="my-job",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )
            assert "waiting_for_jenkins" in phases


class TestRequestParamsPreservation:
    """Tests for request_params preservation across update_status calls."""

    @pytest.mark.asyncio
    async def test_process_analysis_preserves_request_params_on_success(
        self, temp_db_path: Path
    ) -> None:
        """request_params saved initially must survive when analysis completes."""
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        body = UnifiedAnalyzeRequest(
            type="jenkins",
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
            "analysis_type": "jenkins",
        }

        passed = CISourceResult(
            failures=[],
            skip_analysis=True,
            build_url="https://jenkins.example.com/job/my-job/42/",
            identity={"job_name": "my-job", "build_number": 42},
        )

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

            with (
                patch(
                    "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
                    new_callable=AsyncMock,
                    return_value=passed,
                ),
                patch("rootcoz.main._resolve_enable_jira", return_value=False),
                patch(
                    "rootcoz.main.populate_failure_history",
                    new_callable=AsyncMock,
                ),
                patch(
                    "rootcoz.main.storage.make_classifications_visible",
                    new_callable=AsyncMock,
                ),
                _patch_preflight(),
            ):
                await _process_ci_source_analysis(
                    job_id=job_id,
                    body=body,
                    merged=merged,
                    display_name="my-job",
                    ai_provider="claude",
                    ai_model="opus",
                    peer_ai_configs=None,
                    tests_repo_url="",
                    tests_repo_ref="",
                    resolved_tests_repo_token="",
                    additional_repos_list=[],
                    base_url="",
                )

            stored = await storage.get_result(job_id, strip_sensitive=False)
            assert stored is not None
            result = stored["result"]
            assert "request_params" in result
            assert result["request_params"]["ai_provider"] == "claude"
            assert result["request_params"]["ai_model"] == "opus"
            assert result["request_params"]["peer_ai_configs"] == [
                {"ai_provider": "gemini", "ai_model": "flash"}
            ]


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
    async def test_re_analyze_missing_jenkins_data_is_actionable(
        self, test_client
    ) -> None:
        """Missing stored Jenkins coords explain the cause and the way out."""
        from rootcoz import storage

        await storage.save_result(
            "job-no-jenkins-coords",
            "http://jenkins/job/test/1/",
            "completed",
            {
                "summary": "done",
                "failures": [],
                "request_params": encrypt_sensitive_fields(
                    {"analysis_type": "jenkins", "ai_provider": "claude"}
                ),
            },
        )
        response = test_client.post("/re-analyze/job-no-jenkins-coords", json={})
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "no stored Jenkins job name and build number" in detail
        assert "Submit the job again from Jenkins" in detail

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
        with patch("rootcoz.main._process_ci_source_analysis") as mock_process:
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
        with patch("rootcoz.main._process_ci_source_analysis"):
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
    async def test_re_analyze_notifies_origin_report(self, test_client) -> None:
        """Patching the origin wakes its open report so the banner appears."""
        await self._create_origin_job(
            "job-origin-notify",
            "http://jenkins/job/my-job/7/",
            {
                "summary": "1 failure",
                "job_name": "my-job",
                "request_params": encrypt_sensitive_fields(
                    {
                        "analysis_type": "raw",
                        "failures": [{"test_name": "t", "error_message": "e"}],
                        "ai_provider": "claude",
                        "ai_model": "opus",
                    }
                ),
            },
        )
        with (
            patch("rootcoz.main._process_ci_source_analysis"),
            patch("rootcoz.main.notify_job_status_changed") as mock_notify,
        ):
            response = test_client.post("/re-analyze/job-origin-notify", json={})
        assert response.status_code == 202
        assert "job-origin-notify" in [
            call.args[0] for call in mock_notify.call_args_list
        ]

    @pytest.mark.asyncio
    async def test_re_analyze_survives_origin_patch_failure(self, test_client) -> None:
        """A failed forward-link patch must not orphan the new job."""
        await self._create_origin_job(
            "job-origin-patchfail",
            "http://jenkins/job/my-job/8/",
            {
                "summary": "1 failure",
                "job_name": "my-job",
                "request_params": encrypt_sensitive_fields(
                    {
                        "analysis_type": "raw",
                        "failures": [{"test_name": "t", "error_message": "e"}],
                        "ai_provider": "claude",
                        "ai_model": "opus",
                    }
                ),
            },
        )
        with (
            patch("rootcoz.main._process_ci_source_analysis"),
            patch(
                "rootcoz.main.patch_result_json",
                side_effect=RuntimeError("database is locked"),
            ),
            patch("rootcoz.main._register_job_task") as mock_register,
        ):
            response = test_client.post("/re-analyze/job-origin-patchfail", json={})
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        # The new job exists, is queued, and has a worker registered.
        stored = test_client.get(f"/results/{job_id}").json()
        assert stored["status"] == "pending"
        assert mock_register.call_args.args[0] == job_id

    @pytest.mark.asyncio
    async def test_bulk_delete_survives_origin_lookup_failure(
        self, test_client
    ) -> None:
        """A failing origin lookup must not cancel the whole bulk deletion."""
        from rootcoz import storage as storage_module

        await self._create_reanalysis_chain(["re-1", "re-2"])
        real_get_result = storage_module.get_result

        async def _get_result(job_id: str, *args: object, **kwargs: object):
            if job_id == "re-1":
                raise RuntimeError("database is locked")
            return await real_get_result(job_id, *args, **kwargs)

        with patch("rootcoz.storage.get_result", _get_result):
            response = test_client.request(
                "DELETE", "/api/results/bulk", json={"job_ids": ["re-1", "re-2"]}
            )
        assert response.status_code == 200
        assert response.json()["deleted"] == ["re-1", "re-2"]
        assert test_client.get("/results/re-1").status_code == 404
        assert test_client.get("/results/re-2").status_code == 404

    @pytest.mark.asyncio
    async def test_delete_unlinks_link_appended_during_deletion(
        self, test_client
    ) -> None:
        """A link appended while the row is being deleted must not survive."""
        from rootcoz import storage as storage_module

        await self._create_reanalysis_chain(["re-1"])
        real_delete = storage_module.delete_job

        async def _delete(job_id: str) -> bool:
            # Simulates a re-analysis enqueued inside the deletion window:
            # the link is appended before the row disappears.
            await storage_module.patch_result_json(
                "origin-delete",
                lambda data: (
                    data["request_params"]
                    .setdefault("reanalyzed_to_job_ids", [])
                    .append("re-1")
                ),
                require_job_id="re-1",
            )
            return await real_delete(job_id)

        with patch("rootcoz.storage.delete_job", _delete):
            assert test_client.delete("/results/re-1").status_code == 200

        origin = await storage_module.get_result("origin-delete")
        assert origin["result"]["request_params"]["reanalyzed_to_job_ids"] == []

    @pytest.mark.asyncio
    async def test_re_analyze_skips_link_for_deleted_job(self, test_client) -> None:
        """A re-analysis whose row is gone before the patch leaves no dead link."""
        from rootcoz import storage as storage_module

        await self._create_origin_job(
            "job-origin-gone",
            "http://jenkins/job/my-job/7/",
            {
                "summary": "1 failure",
                "job_name": "my-job",
                "display_name": "My Job",
                "build_number": 7,
                "failures": [],
                "request_params": encrypt_sensitive_fields(
                    {
                        "job_name": "my-job",
                        "build_number": 7,
                        "ai_provider": "claude",
                        "ai_model": "opus",
                    }
                ),
            },
        )

        async def _save(job_id: str, *args: object, **kwargs: object) -> None:
            if job_id == "job-origin-gone":
                return
            await storage_module.save_result(job_id, *args, **kwargs)
            # Deleted again right after being saved, before the origin is patched.
            await storage_module.delete_job(job_id)

        with (
            patch("rootcoz.main._process_ci_source_analysis"),
            patch("rootcoz.main.save_result", _save),
        ):
            response = test_client.post("/re-analyze/job-origin-gone", json={})
        assert response.status_code == 202
        origin = await storage_module.get_result("job-origin-gone")
        assert "reanalyzed_to_job_ids" not in origin["result"]["request_params"]

    @pytest.mark.asyncio
    async def test_in_place_analyze_keeps_link_appended_during_save(
        self, test_client
    ) -> None:
        """A link appended between the job read and its save must survive."""
        from rootcoz import storage as storage_module

        origin_result = {
            "summary": "1 failure",
            "job_name": "my-job",
            "analysis_state": "submitted",
            "request_params": encrypt_sensitive_fields(
                {
                    "analysis_type": "raw",
                    "failures": [{"test_name": "t", "error_message": "e"}],
                    "ai_provider": "claude",
                    "ai_model": "opus",
                    "reanalyzed_to_job_ids": ["re-1"],
                }
            ),
        }
        await self._create_origin_job("race-origin", "", origin_result)
        real_save = storage_module.save_result

        async def _save(job_id: str, *args: object, **kwargs: object) -> None:
            # Another request re-analyzes the job after we read it, before we save.
            await storage_module.patch_result_json(
                job_id,
                lambda data: data["request_params"]["reanalyzed_to_job_ids"].append(
                    "re-2"
                ),
            )
            await real_save(job_id, *args, **kwargs)

        with (
            patch("rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock),
            patch("rootcoz.main.save_result", _save),
        ):
            response = test_client.post(
                "/results/race-origin/analyze",
                json={"ai_provider": "claude", "ai_model": "test-model"},
            )
        assert response.status_code == 202, response.text
        origin = await storage_module.get_result("race-origin")
        assert origin["result"]["request_params"]["reanalyzed_to_job_ids"] == [
            "re-1",
            "re-2",
        ]

    @pytest.mark.asyncio
    async def test_in_place_analyze_keeps_forward_links(self, test_client) -> None:
        """Analyzing a submitted job in place keeps its recorded re-analyses."""
        origin_result = {
            "summary": "1 failure",
            "job_name": "my-job",
            "analysis_state": "submitted",
            "request_params": encrypt_sensitive_fields(
                {
                    "analysis_type": "raw",
                    "failures": [{"test_name": "t", "error_message": "e"}],
                    "ai_provider": "claude",
                    "ai_model": "opus",
                    "reanalyzed_to_job_ids": ["re-1", "re-2"],
                }
            ),
        }
        await self._create_origin_job("in-place-origin", "", origin_result)
        with patch("rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock):
            response = test_client.post(
                "/results/in-place-origin/analyze",
                json={"ai_provider": "claude", "ai_model": "test-model"},
            )
        assert response.status_code == 202, response.text
        origin = await storage.get_result("in-place-origin")
        assert origin["result"]["request_params"]["reanalyzed_to_job_ids"] == [
            "re-1",
            "re-2",
        ]

    @pytest.mark.asyncio
    async def test_re_analyze_appends_forward_link_on_origin(self, test_client) -> None:
        """Re-analyze records the new job id on the original job's forward link."""
        await self._create_origin_job(
            "job-origin-fwd",
            "http://jenkins/job/my-job/42/",
            {
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
                    }
                ),
            },
        )
        with patch("rootcoz.main._process_ci_source_analysis"):
            first = test_client.post("/re-analyze/job-origin-fwd", json={})
            second = test_client.post("/re-analyze/job-origin-fwd", json={})
        assert first.status_code == 202
        assert second.status_code == 202
        first_id = first.json()["job_id"]
        second_id = second.json()["job_id"]
        assert first_id != second_id

        # Forward links accumulate across repeated re-analyses, in order.
        origin = await storage.get_result("job-origin-fwd")
        assert origin["result"]["request_params"]["reanalyzed_to_job_ids"] == [
            first_id,
            second_id,
        ]

        # API exposes the forward link on the original job
        data = test_client.get("/results/job-origin-fwd").json()
        assert data["reanalyzed_to_job_ids"] == [first_id, second_id]

    @pytest.mark.asyncio
    async def test_re_analyze_forward_link_exposed_via_fields(
        self, test_client
    ) -> None:
        """reanalyzed_to_job_ids is selectable via the sparse fields allowlist."""
        await storage.save_result(
            "origin-fields",
            "",
            "completed",
            {
                "summary": "orig",
                "request_params": {"reanalyzed_to_job_ids": ["re-1", "re-2"]},
            },
        )
        response = test_client.get(
            "/results/origin-fields?fields=reanalyzed_to_job_ids"
        )
        assert response.status_code == 200
        assert response.json()["reanalyzed_to_job_ids"] == ["re-1", "re-2"]

    @pytest.mark.asyncio
    async def test_delete_removes_forward_link_on_origin(self, test_client) -> None:
        """Deleting a re-analysis drops it from the origin's forward links."""
        await self._create_reanalysis_chain(["re-1", "re-2"])
        assert test_client.delete("/results/re-2").status_code == 200
        origin = await storage.get_result("origin-delete")
        assert origin["result"]["request_params"]["reanalyzed_to_job_ids"] == ["re-1"]
        data = test_client.get("/results/origin-delete").json()
        assert data["reanalyzed_to_job_ids"] == ["re-1"]

    @pytest.mark.asyncio
    async def test_bulk_delete_removes_forward_links(self, test_client) -> None:
        """Bulk delete unlinks every deleted re-analysis from its origin."""
        await self._create_reanalysis_chain(["re-1", "re-2"])
        response = test_client.request(
            "DELETE", "/api/results/bulk", json={"job_ids": ["re-1", "re-2"]}
        )
        assert response.status_code == 200
        origin = await storage.get_result("origin-delete")
        assert origin["result"]["request_params"]["reanalyzed_to_job_ids"] == []

    @pytest.mark.asyncio
    async def test_bulk_delete_keeps_link_for_failed_deletion(
        self, test_client
    ) -> None:
        """A re-analysis whose bulk deletion failed keeps its origin's link."""
        from rootcoz import storage as storage_module

        await self._create_reanalysis_chain(["re-1", "re-2"])
        real_delete_rows = storage_module._delete_job_rows

        async def _delete_rows(db: aiosqlite.Connection, job_id: str) -> bool:
            if job_id == "re-2":
                raise RuntimeError("database is locked")
            return await real_delete_rows(db, job_id)

        with patch("rootcoz.storage._delete_job_rows", _delete_rows):
            response = test_client.request(
                "DELETE", "/api/results/bulk", json={"job_ids": ["re-1", "re-2"]}
            )

        assert response.status_code == 200
        assert response.json()["deleted"] == ["re-1"]
        # The retained row keeps its forward link on the origin.
        assert test_client.get("/results/re-2").status_code == 200
        origin = await storage_module.get_result("origin-delete")
        assert origin["result"]["request_params"]["reanalyzed_to_job_ids"] == ["re-2"]

    @pytest.mark.asyncio
    async def test_delete_keeps_forward_link_when_unlink_fails(
        self, test_client
    ) -> None:
        """A failed unlink must not block the deletion."""
        await self._create_reanalysis_chain(["re-1"])
        with patch(
            "rootcoz.main.patch_result_json",
            side_effect=RuntimeError("database is locked"),
        ):
            assert test_client.delete("/results/re-1").status_code == 200
        assert test_client.get("/results/re-1").status_code == 404

    @staticmethod
    async def _create_reanalysis_chain(reanalysis_ids: list[str]) -> None:
        """Store an origin job pointing at *reanalysis_ids*, each linking back."""
        from rootcoz import storage as storage_module

        await storage_module.save_result(
            "origin-delete",
            "",
            "completed",
            {
                "summary": "orig",
                "job_name": "orig",
                "request_params": {"reanalyzed_to_job_ids": list(reanalysis_ids)},
            },
        )
        for reanalysis_id in reanalysis_ids:
            await storage_module.save_result(
                reanalysis_id,
                "",
                "completed",
                {
                    "summary": "re",
                    "request_params": {"reanalyzed_from_job_id": "origin-delete"},
                },
            )

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
        with patch("rootcoz.main._process_ci_source_analysis"):
            response = test_client.post("/re-analyze/file-origin", json={})
        assert response.status_code == 202
        new_job_id = response.json()["job_id"]
        stored = await storage.get_result(new_job_id)
        assert stored is not None
        params = stored["result"]["request_params"]
        assert params["reanalyzed_from_job_id"] == "file-origin"
        assert params["reanalyzed_from_job_name"] == "File Job"

    @pytest.mark.asyncio
    async def test_re_analyze_prow_applies_artifacts_override(
        self, test_client
    ) -> None:
        """Re-analyze accepts get_job_artifacts overrides for Prow jobs."""
        result_data = {
            "summary": "prow failure",
            "job_name": "pull-test",
            "display_name": "pull-test",
            "build_id": "1234567890",
            "request_params": encrypt_sensitive_fields(
                {
                    "analysis_type": "prow",
                    "prow_job_name": "pull-test",
                    "build_id": "1234567890",
                    "prow_url": "https://prow.example.com",
                    "gcs_bucket": "test-bucket",
                    "get_job_artifacts": False,
                    "ai_provider": "claude",
                    "ai_model": "opus",
                }
            ),
        }
        await self._create_origin_job(
            "prow-origin",
            "https://prow.example.com/view/gs/test-bucket/logs/pull-test/1234567890",
            result_data,
        )
        with patch("rootcoz.main._process_ci_source_analysis") as mock_process:
            response = test_client.post(
                "/re-analyze/prow-origin",
                json={"get_job_artifacts": True},
            )
        assert response.status_code == 202
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["body"].get_job_artifacts is True
        assert call_kwargs["body"].prow_job_name == "pull-test"
        assert call_kwargs["body"].build_id == "1234567890"

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


class TestLiveResultTokenUsage:
    @pytest.mark.asyncio
    async def test_active_replaces_persisted_usage_with_sql_totals(self, test_client):
        await storage.save_result(
            "active-stale", "", "running", {"token_usage": {"total_tokens": 999}}
        )
        await storage.record_token_usage(
            job_id="active-stale",
            ai_provider="gemini",
            ai_model="test",
            call_type="analysis",
            input_tokens=3,
            output_tokens=1,
        )
        with patch(
            "rootcoz.storage.get_token_usage_for_job", new_callable=AsyncMock
        ) as mock_calls:
            response = test_client.get("/results/active-stale")
        assert response.status_code == 202
        assert response.json()["result"]["token_usage"]["total_tokens"] == 4
        assert response.json()["result"]["token_usage"]["calls"] == []
        mock_calls.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sources", "expected"),
        [
            (["user"], "user"),
            (["user", "server"], "mixed"),
            (["user", "unknown"], "unknown"),
        ],
    )
    async def test_running_usage_credential_source_without_calls(
        self, test_client, sources, expected
    ):
        await storage.save_result("live-source", "", "running", {"summary": "working"})
        for source in sources:
            await storage.record_token_usage(
                "live-source", "gemini", "test", "analysis", credential_source=source
            )
        with patch(
            "rootcoz.storage.get_token_usage_for_job", new_callable=AsyncMock
        ) as mock_calls:
            response = test_client.get("/results/live-source")
        assert response.status_code == 202
        usage = response.json()["result"]["token_usage"]
        assert usage["credential_source"] == expected
        assert usage["calls"] == []
        mock_calls.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "aborted"])
    async def test_terminal_persisted_usage_is_not_reloaded(self, test_client, status):
        persisted = {
            "total_calls": 1,
            "total_tokens": 4,
            "calls": [{"input_tokens": 4}],
        }
        await storage.save_result(
            "persisted-usage", "", status, {"token_usage": persisted}
        )
        with patch(
            "rootcoz.storage.get_token_usage_for_job", new_callable=AsyncMock
        ) as mock_calls:
            response = test_client.get("/results/persisted-usage")
        mock_calls.assert_not_awaited()
        assert response.status_code == 200
        assert response.json()["result"]["token_usage"] == persisted

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "aborted"])
    async def test_terminal_without_usage_loads_details(self, test_client, status):
        await storage.save_result("legacy-usage", "", status, {"summary": "error"})
        await storage.record_token_usage(
            job_id="legacy-usage",
            ai_provider="gemini",
            ai_model="test",
            call_type="analysis",
            input_tokens=3,
            output_tokens=1,
        )
        response = test_client.get("/results/legacy-usage")
        assert response.status_code == 200
        usage = response.json()["result"]["token_usage"]
        assert usage["total_tokens"] == 4
        assert len(usage["calls"]) == 1

    @pytest.mark.asyncio
    async def test_aborted_null_usage_loads_recorded_details(self, test_client):
        await storage.save_result(
            "aborted-null-usage", "", "aborted", {"token_usage": None}
        )
        await storage.record_token_usage(
            job_id="aborted-null-usage",
            ai_provider="gemini",
            ai_model="test",
            call_type="analysis",
            input_tokens=3,
            output_tokens=1,
        )
        response = test_client.get("/results/aborted-null-usage")
        assert response.status_code == 200
        usage = response.json()["result"]["token_usage"]
        assert usage["total_tokens"] == 4
        assert len(usage["calls"]) == 1

    @pytest.mark.asyncio
    async def test_missing_usage_metadata_never_creates_badge(self, test_client):
        from pi_sidecar_client import AIResult

        from rootcoz.token_tracking import record_ai_usage

        await storage.save_result("missing-usage", "", "failed", {"summary": "error"})
        await record_ai_usage(
            "missing-usage", AIResult(success=False, text="error"), "analysis"
        )
        assert await storage.get_token_usage_for_job("missing-usage") == []
        response = test_client.get("/results/missing-usage")
        assert "token_usage" not in response.json()["result"]

    @pytest.mark.asyncio
    async def test_active_aggregate_and_terminal_details(self, test_client):
        await storage.save_result("usage-live", "", "running", {"summary": "analysis"})
        assert (
            "token_usage" not in test_client.get("/results/usage-live").json()["result"]
        )
        for index, cost in enumerate((0.05, 0.0, None), start=1):
            await storage.record_token_usage(
                job_id="usage-live",
                ai_provider="gemini",
                ai_model="test",
                call_type="analysis",
                input_tokens=100,
                output_tokens=20,
                cost_usd=cost,
            )
            current = test_client.get("/results/usage-live").json()["result"][
                "token_usage"
            ]
            assert current["total_calls"] == index
            assert current["calls"] == []
            # An unpriced call counts as $0 rather than discarding the priced spend.
            assert current["total_cost_usd"] == pytest.approx(0.05)
        with patch(
            "rootcoz.storage.get_token_usage_for_job",
            side_effect=AssertionError("loaded calls"),
        ):
            live = test_client.get("/results/usage-live")
        assert live.status_code == 202
        usage = live.json()["result"]["token_usage"]
        assert usage["total_calls"] == 3
        assert usage["total_tokens"] == 360
        assert usage["total_input_tokens"] == 300
        assert usage["total_output_tokens"] == 60
        assert usage["total_cost_usd"] == pytest.approx(0.05)
        assert usage["cost_partial"] is True
        assert usage["calls"] == []
        await storage.save_result("usage-live", "", "failed", {"summary": "analysis"})
        terminal = test_client.get("/results/usage-live").json()["result"][
            "token_usage"
        ]
        assert len(terminal["calls"]) == 3
        assert terminal["total_cost_usd"] == pytest.approx(0.05)
        assert terminal["cost_partial"] is True

    @pytest.mark.asyncio
    async def test_usage_write_notifies_only_its_job_without_progress(
        self, test_client
    ):
        from pi_sidecar_client import AIResult, AITokenUsage

        from rootcoz.main import _job_status_listeners, _make_sse_stream
        from rootcoz.token_tracking import record_ai_usage

        await storage.save_result("usage-event", "", "running", {"summary": "analysis"})
        other = asyncio.Event()
        _job_status_listeners["another-job"] = {other}
        request = AsyncMock()
        request.is_disconnected.return_value = False
        stream = _make_sse_stream(
            request,
            set(),
            "status-changed",
            per_key_listeners=_job_status_listeners,
            listener_key="usage-event",
        ).body_iterator
        event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)  # register the listener before writing usage
        try:
            await record_ai_usage(
                "usage-event",
                AIResult(
                    success=True,
                    text="ok",
                    usage=AITokenUsage(
                        input_tokens=10,
                        output_tokens=5,
                        cost_usd=0.25,
                    ),
                ),
                "analysis",
            )
            assert await asyncio.wait_for(event, timeout=1) == (
                "event: status-changed\ndata: refresh\n\n"
            )
            assert not other.is_set()
            usage = test_client.get("/results/usage-event").json()["result"][
                "token_usage"
            ]
            assert usage["total_cost_usd"] == 0.25
            assert usage["calls"] == []
        finally:
            await stream.aclose()
            _job_status_listeners.pop("another-job", None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["running", "failed", "aborted"])
    async def test_result_exposes_recorded_usage(self, test_client, status):
        await storage.save_result("usage-job", "", status, {"summary": "analysis"})
        await storage.record_token_usage(
            job_id="usage-job",
            ai_provider="gemini",
            ai_model="test",
            call_type="analysis",
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=0.0,
            duration_ms=100,
            prompt_chars=0,
            response_chars=0,
        )
        response = test_client.get("/results/usage-job")
        assert response.status_code == (202 if status == "running" else 200)
        assert response.json()["result"]["token_usage"]["total_cost_usd"] == 0.0
        assert response.json()["result"]["token_usage"]["total_tokens"] == 120

    @pytest.mark.asyncio
    async def test_result_reports_unpriced_cost_as_a_floor(self, test_client):
        await storage.save_result(
            "unknown-cost", "", "running", {"summary": "analysis"}
        )
        assert (
            "token_usage"
            not in test_client.get("/results/unknown-cost").json()["result"]
        )
        await storage.record_token_usage(
            job_id="unknown-cost",
            ai_provider="gemini",
            ai_model="test",
            call_type="analysis",
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=None,
            duration_ms=100,
            prompt_chars=0,
            response_chars=0,
        )
        usage = test_client.get("/results/unknown-cost").json()["result"]["token_usage"]
        assert usage["total_cost_usd"] == pytest.approx(0.0)
        # Nothing was priced, so the figure is a floor rather than a real $0 spend.
        assert usage["cost_partial"] is True


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
                    if (
                        _failures
                        and "reanalysis_status" not in _failures[0]
                        and _failures[0].get("analysis", {}).get("classification")
                        == "PRODUCT ISSUE"
                    ):
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

    @pytest.mark.asyncio
    async def test_successful_reanalysis_clears_stale_analysis_error(
        self, test_client
    ) -> None:
        """A row that now has a classification must not keep its old reason."""
        job_id = "job-reanalyze-clears-error"
        stale = FailureAnalysis(
            test_name="tests.TestReAnalyze.test_stale",
            error="ValueError",
            analysis=AnalysisDetail(),
            analysis_error="AI analysis incomplete",
        ).model_dump(mode="json")
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "summary": "1 failure",
                "failures": [stale],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                    }
                ),
            },
        )

        new_analysis = FailureAnalysis(
            test_name=stale["test_name"],
            error="ValueError",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
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
            patch("rootcoz.main.RepositoryManager"),
        ):
            response = test_client.post(f"/api/failures/{stale['id']}/re-analyze")
            assert response.status_code == 202

            import asyncio

            for _ in range(50):
                await asyncio.sleep(0.1)
                stored = await storage.get_result(job_id)
                failures = (stored or {}).get("result", {}).get("failures", [])
                if (
                    failures
                    and failures[0].get("analysis", {}).get("classification")
                    == "CODE ISSUE"
                ):
                    break

        stored = await storage.get_result(job_id)
        failure = stored["result"]["failures"][0]
        assert failure["analysis"]["classification"] == "CODE ISSUE"
        assert "analysis_error" not in failure

    @pytest.mark.asyncio
    async def test_reanalysis_that_stays_unclassified_keeps_its_new_reason(
        self, test_client
    ) -> None:
        """A pass that still has no diagnosis must keep the reason it produced."""
        job_id = "job-reanalyze-still-incomplete"
        old = FailureAnalysis(
            test_name="tests.TestReAnalyze.test_incomplete",
            error="ValueError",
            analysis=AnalysisDetail(),
            analysis_error="old reason",
        ).model_dump(mode="json")
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "summary": "1 failure",
                "failures": [old],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                    }
                ),
            },
        )

        new_analysis = FailureAnalysis(
            test_name=old["test_name"],
            error="ValueError",
            analysis=AnalysisDetail(),
            analysis_error="peers could not classify: new reason",
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
            patch("rootcoz.main.RepositoryManager"),
        ):
            response = test_client.post(f"/api/failures/{old['id']}/re-analyze")
            assert response.status_code == 202

            import asyncio

            for _ in range(50):
                await asyncio.sleep(0.1)
                stored = await storage.get_result(job_id)
                failures = (stored or {}).get("result", {}).get("failures", [])
                if failures and failures[0].get("analysis_error") == (
                    "peers could not classify: new reason"
                ):
                    break

        stored = await storage.get_result(job_id)
        failure = stored["result"]["failures"][0]
        assert failure["analysis_error"] == "peers could not classify: new reason"
        assert "reanalysis_status" not in failure

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "expected_reanalysis_error", "expected_analysis_error"),
        [
            # A crash is not an answer, so it must not be spelled like one: the
            # report reads `analysis_error` as "the model answered without a
            # classification" (frontend `countProviderFailures`).
            (
                RuntimeError("simulated analysis crash"),
                "Re-analysis failed unexpectedly. Check server logs for details.",
                # No reason on the row: an empty `analysis_error` is "not the
                # model", which is what the report's provider count reads.
                "",
            ),
            # A narration-only answer still explains why there is no diagnosis.
            (
                AnalysisIncompleteError(
                    "AI analysis incomplete: response has no usable classification"
                ),
                "AI analysis incomplete: response has no usable classification",
                "AI analysis incomplete: response has no usable classification",
            ),
        ],
    )
    async def test_reanalysis_failure_kind_comes_from_the_error_type(
        self,
        test_client,
        error: Exception,
        expected_reanalysis_error: str,
        expected_analysis_error: str,
    ) -> None:
        """Only a model-narration failure may write `analysis_error`.

        A generic crash message there would make the report blame the model for a
        server error, so the row keeps the crash in `reanalysis_error` and leaves
        the type-driven reason alone (#399).
        """
        job_id = "job-reanalyze-failure-kind"
        row = FailureAnalysis(
            test_name="tests.TestReAnalyze.test_failure_kind",
            error="ValueError",
            analysis=AnalysisDetail(),
        ).model_dump(mode="json")
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "summary": "1 failure",
                "failures": [row],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                    }
                ),
            },
        )

        with (
            patch(
                "rootcoz.main.analyze_failure_group",
                new_callable=AsyncMock,
                side_effect=error,
            ),
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("rootcoz.main.RepositoryManager"),
        ):
            response = test_client.post(f"/api/failures/{row['id']}/re-analyze")
            assert response.status_code == 202

            import asyncio

            for _ in range(50):
                await asyncio.sleep(0.1)
                stored = await storage.get_result(job_id)
                failures = (stored or {}).get("result", {}).get("failures", [])
                if failures and failures[0].get("reanalysis_status") == "failed":
                    break

        failure = (await storage.get_result(job_id))["result"]["failures"][0]
        assert failure["reanalysis_status"] == "failed"
        assert failure["reanalysis_error"] == expected_reanalysis_error
        assert failure.get("analysis_error") == expected_analysis_error

    @pytest.mark.asyncio
    async def test_re_analyze_of_a_failure_removed_before_the_patch_is_refused(
        self, test_client
    ) -> None:
        """A failure the written payload no longer holds starts no re-analysis.

        The failure is found in the stored result, then a concurrent rewrite takes
        it out before this request's patch lands. The patch marks nothing, so the
        endpoint must refuse instead of answering 202 for work it never recorded.
        """
        fa = FailureAnalysis(
            test_name="tests.TestReAnalyze.test_removed",
            error="ValueError",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
        )
        params = encrypt_sensitive_fields(
            {"ai_provider": "claude", "ai_model": "opus", "submitted_by": "admin"}
        )
        params["submitted_by"] = "admin"
        await storage.save_result(
            "job-reanalyze-removed",
            "",
            "completed",
            {
                "summary": "1 failure",
                "failures": [fa.model_dump(mode="json")],
                "request_params": params,
            },
        )

        with (
            patch("rootcoz.main._find_failure_by_uuid_in_result", return_value=None),
            patch(
                "rootcoz.main._reanalyze_failure_background", new_callable=AsyncMock
            ) as background,
        ):
            response = test_client.post(f"/api/failures/{fa.id}/re-analyze")

        assert response.status_code == 404, response.text
        background.assert_not_called()
        stored = await storage.get_result("job-reanalyze-removed")
        assert "reanalysis_status" not in stored["result"]["failures"][0]

    @pytest.mark.asyncio
    async def test_reanalyze_usage_isolated_by_attempt_and_child(
        self, test_client
    ) -> None:
        """Re-analysis changes only its card; job totals retain all attempts."""
        from pi_sidecar_client import AIResult, AITokenUsage

        from rootcoz.sources.base import CISourceResult
        from rootcoz.token_tracking import failure_group_usage, record_ai_usage

        job_id = "job-attempt-scopes"
        top = [
            FailureAnalysis(
                test_name=f"top-{index}",
                error="same error",
                error_signature="same-signature",
                analysis=AnalysisDetail(details="old"),
            ).model_dump(mode="json")
            for index in range(2)
        ]
        child_failure = FailureAnalysis(
            test_name="child",
            error="same error",
            error_signature="same-signature",
            analysis=AnalysisDetail(details="old"),
        ).model_dump(mode="json")
        await storage.save_result(
            job_id,
            "",
            "completed",
            {
                "failures": top,
                "child_job_analyses": [
                    {
                        "job_name": "runner",
                        "build_number": 7,
                        "failures": [child_failure],
                    }
                ],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                    }
                ),
            },
        )
        for child_name, child_build, tokens in (("", 0, 10), ("runner", 7, 20)):
            await storage.record_token_usage(
                job_id=job_id,
                ai_provider="claude",
                ai_model="opus",
                call_type="primary",
                input_tokens=tokens,
                error_signature="same-signature",
                child_job_name=child_name,
                child_build_number=child_build,
            )

        async def analyze_replacement(**kwargs):
            with failure_group_usage(kwargs["job_id"], "same-signature"):
                await record_ai_usage(
                    kwargs["job_id"],
                    AIResult(
                        success=True,
                        text="new",
                        usage=AITokenUsage(
                            input_tokens=5, provider="claude", model="opus"
                        ),
                    ),
                    "primary",
                )
            return [
                FailureAnalysis(
                    test_name=kwargs["failures"][0].test_name,
                    error="same error",
                    error_signature="same-signature",
                    analysis=AnalysisDetail(details="new"),
                )
            ]

        # The child failure has CI coordinates, so its re-analysis reconstructs a
        # JenkinsSource and refetches console output from the real JENKINS_URL.
        # Mock that boundary and pin egress so any outbound connection attempt
        # fails and is recorded: both hostname resolution and the connect
        # syscall, so an IP-literal connection cannot slip through unnoticed.
        egress: list[Any] = []

        class _EgressBlocked(OSError):
            """Marker so the pin's own error is never read as a connection error."""

        def _record(target: Any) -> None:
            egress.append(target)
            raise _EgressBlocked(f"test blocks network egress to {target!r}")

        def _block_resolve(host, port, *args, **kwargs):
            _record((host, port))

        def _block_connect(self, address, *args, **kwargs):
            _record(address)

        with (
            patch(
                "rootcoz.main.analyze_failure_group", side_effect=analyze_replacement
            ),
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("rootcoz.main.RepositoryManager"),
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.refetch_context",
                new_callable=AsyncMock,
                return_value=CISourceResult(failures=[]),
            ) as refetch_context,
            patch.object(socket, "getaddrinfo", _block_resolve),
            patch.object(socket.socket, "connect", _block_connect),
            patch.object(socket.socket, "connect_ex", _block_connect),
        ):
            # Self-check: an IP literal never reaches getaddrinfo, so only the
            # connect pin can catch it. Connecting to a closed local port keeps
            # this assertion offline.
            with socket.socket() as probe, pytest.raises(_EgressBlocked):
                probe.connect(("127.0.0.1", 9))
            assert ("127.0.0.1", 9) in egress
            egress.clear()

            for failure_id in (child_failure["id"], top[0]["id"]):
                response = test_client.post(f"/api/failures/{failure_id}/re-analyze")
                assert response.status_code == 202
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    current = (await storage.get_result(job_id))["result"]
                    target = (
                        current["child_job_analyses"][0]["failures"][0]
                        if failure_id == child_failure["id"]
                        else current["failures"][0]
                    )
                    if target["analysis"]["details"] == "new":
                        break
                else:
                    pytest.fail("re-analysis did not complete")
                assert target["usage_attempt"]
                assert target["token_usage"]["total_input_tokens"] == 5
                assert target["previous_analyses"][0]["token_usage"][
                    "total_input_tokens"
                ] == (20 if failure_id == child_failure["id"] else 10)
                assert "reanalysis_status" not in target["previous_analyses"][0]
                if failure_id == child_failure["id"]:
                    assert (
                        current["failures"][0]["token_usage"]["total_input_tokens"]
                        == 10
                    )
                    assert (
                        current["failures"][1]["token_usage"]["total_input_tokens"]
                        == 10
                    )
            # A second attempt on the same failure archives the first attempt's usage.
            first_attempt = current["failures"][0]["usage_attempt"]
            response = test_client.post(f"/api/failures/{top[0]['id']}/re-analyze")
            assert response.status_code == 202
            for _ in range(50):
                await asyncio.sleep(0.1)
                current = (await storage.get_result(job_id))["result"]
                if current["failures"][0].get("usage_attempt") != first_attempt:
                    break
            else:
                pytest.fail("second re-analysis did not complete")
            archived = current["failures"][0]["previous_analyses"][0]
            assert archived["usage_attempt"] == first_attempt
            assert archived["token_usage"]["total_input_tokens"] == 5
            assert "reanalysis_status" not in archived
            result = test_client.get(f"/results/{job_id}").json()["result"]
            assert result["token_usage"]["total_input_tokens"] == 45
            assert result["token_usage"]["total_calls"] == 5
            assert result["failures"][0]["token_usage"]["total_input_tokens"] == 5
            assert result["failures"][1]["token_usage"]["total_input_tokens"] == 10
            assert (
                result["child_job_analyses"][0]["failures"][0]["token_usage"][
                    "total_input_tokens"
                ]
                == 5
            )
            rows = await storage.get_token_usage_for_job(job_id)
            reanalyses = [row for row in rows if row["call_type"] == "reanalysis"]
            assert {row["failure_id"] for row in reanalyses} == {
                top[0]["id"],
                child_failure["id"],
            }
            child_call = next(
                row for row in reanalyses if row["failure_id"] == child_failure["id"]
            )
            assert (child_call["child_job_name"], child_call["child_build_number"]) == (
                "runner",
                7,
            )
            assert (
                child_call["usage_attempt"]
                == result["child_job_analyses"][0]["failures"][0]["usage_attempt"]
            )

            # A failed attempt must not replace the last successful card usage.
            async def failing_replacement(**kwargs):
                with failure_group_usage(kwargs["job_id"], "same-signature"):
                    await record_ai_usage(
                        kwargs["job_id"],
                        AIResult(
                            success=True,
                            text="partial",
                            usage=AITokenUsage(input_tokens=5),
                        ),
                        "primary",
                    )
                raise RuntimeError("simulated analysis error")

            with patch(
                "rootcoz.main.analyze_failure_group", side_effect=failing_replacement
            ):
                response = test_client.post(f"/api/failures/{top[0]['id']}/re-analyze")
                assert response.status_code == 202
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    result = (await storage.get_result(job_id))["result"]
                    if result["failures"][0].get("reanalysis_status") == "failed":
                        break
                else:
                    pytest.fail("failed re-analysis did not finish")
            assert (
                result["failures"][0]["usage_attempt"]
                == current["failures"][0]["usage_attempt"]
            )
            assert result["failures"][0]["token_usage"]["total_input_tokens"] == 5
            assert result["token_usage"]["total_calls"] == 6
            assert result["token_usage"]["total_input_tokens"] == 50

        # Only the child failure has CI coordinates, so exactly one refetch ran —
        # mocked — and nothing reached the network.
        assert refetch_context.await_count == 1
        assert egress == [], f"re-analysis opened a socket / resolved a host: {egress}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_history", [False, True])
    async def test_legacy_reanalysis_does_not_archive_mixed_primary_usage(
        self, test_client, has_history: bool
    ) -> None:
        from pi_sidecar_client import AIResult, AITokenUsage

        from rootcoz.token_tracking import failure_group_usage, record_ai_usage

        job_id = f"legacy-reanalysis-archive-{has_history}"
        failure = FailureAnalysis(
            test_name="old",
            error="same",
            error_signature="same",
            analysis=AnalysisDetail(details="prior"),
        ).model_dump(mode="json")
        if has_history:
            failure["previous_analyses"] = [{"analysis": {"details": "older"}}]
        await storage.record_token_usage(
            job_id,
            "claude",
            "test",
            "primary",
            input_tokens=10,
            error_signature="same",
        )
        await storage.record_token_usage(
            job_id,
            "claude",
            "test",
            "primary",
            input_tokens=20,
            error_signature="same",
        )
        await storage.save_result(
            job_id,
            status="completed",
            result={
                "failures": [failure, {"id": "sibling", "error_signature": "same"}],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                    }
                ),
            },
        )

        # A legacy result may already contain an inflated materialized card value.
        await storage.patch_result_json(
            job_id,
            lambda result: result["failures"][0].update(
                token_usage={"total_input_tokens": 30}
            ),
        )

        async def analyze_replacement(**kwargs):
            with failure_group_usage(kwargs["job_id"], "same"):
                await record_ai_usage(
                    kwargs["job_id"],
                    AIResult(
                        success=True, text="ok", usage=AITokenUsage(input_tokens=5)
                    ),
                    "primary",
                )
            return [
                FailureAnalysis(
                    test_name="old",
                    error="same",
                    error_signature="same",
                    analysis=AnalysisDetail(details="new"),
                )
            ]

        with (
            patch(
                "rootcoz.main.analyze_failure_group", side_effect=analyze_replacement
            ),
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("rootcoz.main.RepositoryManager"),
        ):
            response = test_client.post(f"/api/failures/{failure['id']}/re-analyze")
            assert response.status_code == 202, response.json()
            for _ in range(50):
                await asyncio.sleep(0.1)
                current = (await storage.get_result(job_id))["result"]
                if current["failures"][0].get("usage_attempt"):
                    break
            else:
                pytest.fail("re-analysis did not finish")
        target = current["failures"][0]
        assert target["token_usage"]["total_input_tokens"] == 5
        assert target["previous_analyses"][0].get("token_usage") is None
        assert current["failures"][1]["token_usage"] is None
        assert current["token_usage"]["total_input_tokens"] == 35
        assert current["token_usage"]["total_calls"] == 3

    @pytest.mark.asyncio
    async def test_reanalyze_additional_repo_persists_and_notifies_clone_progress(
        self, test_client, tmp_path, monkeypatch
    ) -> None:
        import asyncio

        from rootcoz import storage
        from rootcoz.engine.core import set_progress_callback

        fa = FailureAnalysis(
            test_name="test_clone",
            error="failure",
            analysis=AnalysisDetail(details="old"),
        )
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "reanalyze.db")
        await storage.init_db()
        await storage.save_result(
            "job-clone",
            "",
            "completed",
            {
                "failures": [fa.model_dump(mode="json")],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                        "additional_repos": [
                            {"name": "extra", "url": "https://example.com/extra"}
                        ],
                    }
                ),
            },
        )
        events = []
        set_progress_callback(lambda job: events.append(job))
        new = FailureAnalysis(
            test_name="test_clone",
            error="failure",
            analysis=AnalysisDetail(details="new"),
        )
        try:
            with (
                patch("rootcoz.main.RepositoryManager"),
                patch("rootcoz.main.copy_rootcoz_pi_resources"),
                patch(
                    "rootcoz.engine.core.asyncio.to_thread",
                    new_callable=AsyncMock,
                    return_value=tmp_path / "extra",
                ),
                patch(
                    "rootcoz.main.analyze_failure_group",
                    new_callable=AsyncMock,
                    return_value=[new],
                ),
                patch(
                    "rootcoz.main._create_ai_auth_header",
                    new_callable=AsyncMock,
                    return_value="",
                ),
            ):
                response = test_client.post(f"/api/failures/{fa.id}/re-analyze")
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if (stored := await storage.get_result("job-clone")) and (
                        stored["result"]["failures"][0]
                        .get("analysis", {})
                        .get("details")
                        == "new"
                    ):
                        break
            assert response.status_code == 202
            assert (await storage.get_result("job-clone"))["result"][
                "cloning_repos"
            ] == []
            assert any(
                entry["repos"] == ["extra"]
                for entry in (await storage.get_result("job-clone"))["result"][
                    "progress_log"
                ]
            )
            assert events.count("job-clone") >= 2
            assert (await storage.get_result("job-clone"))["result"].get(
                "progress_phase"
            ) != "cloning"
        finally:
            set_progress_callback(lambda _job: None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "failed", "aborted"])
    @pytest.mark.parametrize("clone_fails", [False, True])
    async def test_reanalyze_test_repo_records_clone_outcome(
        self, test_client, tmp_path, monkeypatch, clone_fails, status
    ) -> None:
        import asyncio

        from rootcoz import storage
        from rootcoz.engine.core import set_progress_callback

        fa = FailureAnalysis(
            test_name="test_clone",
            error="failure",
            analysis=AnalysisDetail(details="old"),
        )
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "reanalyze-tests.db")
        await storage.init_db()
        await storage.save_result(
            "job-tests-clone",
            "",
            status,
            {
                "failures": [fa.model_dump(mode="json")],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                        "tests_repo_url": "https://user:secret@example.com/tests",  # pragma: allowlist secret
                        "tests_repo_ref": "main",
                    }
                ),
            },
        )
        new = FailureAnalysis(
            test_name="test_clone",
            error="failure",
            analysis=AnalysisDetail(details="new"),
        )
        events: list[str] = []
        set_progress_callback(events.append)
        try:
            with (
                patch("rootcoz.main.RepositoryManager") as manager,
                patch("rootcoz.main.copy_rootcoz_pi_resources"),
                patch(
                    "rootcoz.main.analyze_failure_group",
                    new_callable=AsyncMock,
                    return_value=[new],
                ),
                patch(
                    "rootcoz.main._create_ai_auth_header",
                    new_callable=AsyncMock,
                    return_value="",
                ),
            ):
                manager.return_value.create_workspace.return_value = (
                    tmp_path / "workspace"
                )
                if clone_fails:
                    manager.return_value.clone_into.side_effect = RuntimeError(
                        "clone failed"
                    )
                response = test_client.post(f"/api/failures/{fa.id}/re-analyze")
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    result = (await storage.get_result("job-tests-clone"))["result"]
                    if (
                        result.get("failures", [{}])[0]
                        .get("analysis", {})
                        .get("details")
                        == "new"
                    ):
                        break
            assert response.status_code == 202
            assert result["cloning_repos"] == []
            assert (await storage.get_result("job-tests-clone"))["status"] == status
            assert [
                (e["repo"], e["state"], e["url"], e["ref"])
                for e in result["progress_log"]
            ] == [
                ("tests", "cloning", "https://example.com/tests", "main"),
                (
                    "tests",
                    "failed" if clone_fails else "cloned",
                    "https://example.com/tests",
                    "main",
                ),
            ]
            assert events.count("job-tests-clone") >= 2
            assert "secret" not in str(result["progress_log"])
        finally:
            set_progress_callback(lambda _job: None)

    @pytest.mark.asyncio
    async def test_two_failures_reanalyze_same_repo_concurrently(
        self, test_client, tmp_path, monkeypatch
    ) -> None:
        import asyncio

        from rootcoz import storage

        failures = [
            FailureAnalysis(
                test_name=name, error="failure", analysis=AnalysisDetail(details="old")
            )
            for name in ("one", "two")
        ]
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "concurrent-reanalysis.db")
        await storage.init_db()
        await storage.save_result(
            "job-concurrent",
            "",
            "failed",
            {
                "progress_phase": "failed",
                "failures": [fa.model_dump(mode="json") for fa in failures],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                        "tests_repo_url": "https://example.com/tests",
                        "tests_repo_ref": "main",
                    }
                ),
            },
        )
        from threading import Event

        entered = Event()
        release = Event()
        cloned = 0

        async def clone(*_args, **_kwargs):
            nonlocal cloned
            cloned += 1
            if cloned == 2:
                entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

        new = [
            FailureAnalysis(
                test_name=fa.test_name,
                error="failure",
                analysis=AnalysisDetail(details="new"),
            )
            for fa in failures
        ]
        with (
            patch("rootcoz.main.RepositoryManager") as manager,
            patch("rootcoz.main.copy_rootcoz_pi_resources"),
            patch("rootcoz.main.asyncio.to_thread", side_effect=clone),
            patch(
                "rootcoz.main.analyze_failure_group",
                new_callable=AsyncMock,
                side_effect=lambda *_a, **_k: new,
            ),
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            manager.return_value.create_workspace.return_value = tmp_path / "workspace"
            try:
                for fa in failures:
                    assert (
                        test_client.post(
                            f"/api/failures/{fa.id}/re-analyze"
                        ).status_code
                        == 202
                    )
                for _ in range(50):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.1)
                assert entered.is_set()
                running = (await storage.get_result("job-concurrent"))["result"]
                assert running["cloning_repos"] == ["tests"]
                assert (
                    len([e for e in running["progress_log"] if e["state"] == "cloning"])
                    == 2
                )
            finally:
                release.set()
            for _ in range(50):
                await asyncio.sleep(0.1)
                result = (await storage.get_result("job-concurrent"))["result"]
                if (
                    len([e for e in result["progress_log"] if e["state"] == "cloned"])
                    == 2
                ):
                    break
            assert (await storage.get_result("job-concurrent"))["status"] == "failed"
            assert result["cloning_repos"] == [], result["progress_log"]
            assert (
                len([e for e in result["progress_log"] if e["state"] == "cloned"]) == 2
            )

    @pytest.mark.asyncio
    async def test_reanalyze_cancelled_test_repo_records_terminal_clone(
        self, tmp_path, monkeypatch
    ) -> None:
        import asyncio

        from rootcoz import storage
        from rootcoz.main import _reanalyze_failure_background

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "cancel-tests.db")
        await storage.init_db()
        await storage.save_result(
            "job-cancel-tests",
            "",
            "completed",
            {"failures": [{"id": "failure-id", "reanalysis_status": "running"}]},
        )
        from threading import Event

        started = Event()
        release = Event()
        finished = Event()

        def clone(*_args, **_kwargs):
            started.set()
            release.wait()
            finished.set()

        task = None
        try:
            with (
                patch("rootcoz.main.RepositoryManager") as manager,
                patch(
                    "rootcoz.main._create_ai_auth_header",
                    new_callable=AsyncMock,
                    return_value="",
                ),
            ):
                manager.return_value.create_workspace.return_value = (
                    tmp_path / "workspace"
                )
                manager.return_value.clone_into.side_effect = clone
                task = asyncio.create_task(
                    _reanalyze_failure_background(
                        "job-cancel-tests",
                        "failure-id",
                        {},
                        "claude",
                        "opus",
                        None,
                        "",
                        None,
                        0,
                        "https://example.com/tests",
                        "main",
                        "",
                        None,
                        "admin",
                        1,
                    )
                )
                assert await asyncio.to_thread(started.wait, 2)
                task.cancel()
                await asyncio.sleep(0.05)
                assert not finished.is_set()
                assert not task.done()
                task.cancel()  # Repeated cancellation must not abandon the worker.
                await asyncio.sleep(0)
                assert not task.done()
                assert not manager.return_value.cleanup.called
                running = (await storage.get_result("job-cancel-tests"))["result"]
                assert running["cloning_repos"] == ["tests"]
                assert [entry["state"] for entry in running["progress_log"]] == [
                    "cloning"
                ]
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                assert finished.is_set()
                # The delete is a queued reaper step now, so wait for it instead of
                # depending on worker-thread scheduling.
                for _ in range(200):
                    if manager.return_value.cleanup.called:
                        break
                    await asyncio.sleep(0.01)
                manager.return_value.cleanup.assert_called_once()
            result = (await storage.get_result("job-cancel-tests"))["result"]
            assert result["cloning_repos"] == []
            assert [
                (entry["repo"], entry["state"]) for entry in result["progress_log"]
            ] == [
                ("tests", "cloning"),
                ("tests", "cancelled"),
            ]
        finally:
            release.set()
            if task is not None and not task.done():
                with pytest.raises(asyncio.CancelledError):
                    await task

    @pytest.mark.parametrize(
        ("failure", "stderr_message", "expected_cause"),
        [
            ("git", "Authentication failed", "Authentication failed"),
            ("basic", "Authentication failed", "Authentication failed"),
            (
                "git",
                "Remote branch missing not found in upstream origin",
                "Branch or ref not found",
            ),
            (
                "git",
                "pathspec 'missing' did not match any file(s) known to git",
                "Branch or ref not found",
            ),
            (
                "git",
                "SSL certificate problem: certificate verify failed",
                "SSL verification failed",
            ),
            ("git", "Permission denied (publickey)", "Permission denied"),
            (
                "git",
                "Could not resolve host: example.com",
                "Network DNS resolution failed",
            ),
            ("git", "repository not found", "Repository not found"),
            ("git", "unexpected failure", "Git command failed"),
            ("invalid_url", "", "Invalid repository URL scheme"),
            ("unknown", "", "Clone worker failed"),
        ],
    )
    @pytest.mark.asyncio
    async def test_reanalyze_cancelled_clone_logs_safe_cause_and_frames(
        self, tmp_path, monkeypatch, failure, stderr_message, expected_cause
    ) -> None:
        import asyncio
        from threading import Event

        from git.exc import GitCommandError

        from rootcoz import storage
        from rootcoz.main import _reanalyze_failure_background
        from rootcoz.repository import _validate_repo_url

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "cancel-failed.db")
        await storage.init_db()
        await storage.save_result(
            "job-cancel-failed",
            "",
            "completed",
            {"failures": [{"id": "failure-id", "reanalysis_status": "running"}]},
        )
        started, release = Event(), Event()
        secret = "clone-secret-12345"  # pragma: allowlist secret
        userinfo = "alice:password"  # pragma: allowlist secret
        url = f"https://{userinfo}@example.com/tests?access_token={secret}"
        if failure == "invalid_url":
            url = f"ftp://{userinfo}@example.com/tests?access_token={secret}"

        def clone(*_args, **_kwargs):
            started.set()
            release.wait(2)
            if failure == "invalid_url":
                _validate_repo_url(url)
            if failure == "unknown":
                raise RuntimeError(f"unexpected credential opaque-secret-789 in {url}")
            raise GitCommandError(
                f"git clone https://x-token-auth:{secret}@example.com/tests",
                128,
                stderr=(
                    f"fatal: {stderr_message} for '{url}'; "
                    + (
                        "Authorization: Basic dXNlcjpwYXNz; opaque-secret-789"
                        if failure == "basic"
                        else "Authorization: Bearer other-secret-456; api_key=extra-secret-789"
                    )
                ),
            )

        with (
            patch("rootcoz.main.RepositoryManager") as manager,
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("rootcoz.main.logger.warning") as warning,
        ):
            manager.return_value.create_workspace.return_value = tmp_path / "workspace"
            manager.return_value.clone_into.side_effect = clone
            task = asyncio.create_task(
                _reanalyze_failure_background(
                    "job-cancel-failed",
                    "failure-id",
                    {},
                    "claude",
                    "opus",
                    None,
                    "",
                    None,
                    0,
                    url,
                    "main",
                    secret,
                    None,
                    "admin",
                    1,
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 2)
                task.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        log = "\n".join(
            str(arg) for call in warning.call_args_list for arg in call.args
        )
        assert " in clone" in log
        assert "[path]:" in log
        assert expected_cause in log
        assert (
            "GitCommandError"
            if failure in ("git", "basic")
            else "ValueError"
            if failure == "invalid_url"
            else "RuntimeError"
        ) in log
        for leaked in (
            secret,
            "example.com",
            "ftp://",
            "opaque-secret-789",
            "dXNlcjpwYXNz",
            "alice",
            "password",
            "access_token=",
            "x-token-auth",
            "other-secret-456",
            "extra-secret-789",
        ):
            assert leaked not in log
        assert all(not call.kwargs.get("exc_info") for call in warning.call_args_list)

    @pytest.mark.asyncio
    async def test_re_analyze_failure_defers_ai_with_tests_repo(
        self, test_client
    ) -> None:
        """Empty stored AI is accepted when a tests repo can supply settings.json."""
        from rootcoz import storage

        fa = FailureAnalysis(
            test_name="tests.TestDeferAi.test_one",
            error="ValueError",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
        )
        fa_dict = fa.model_dump(mode="json")
        result_data = {
            "summary": "1 failure",
            "failures": [fa_dict],
            "request_params": encrypt_sensitive_fields(
                {
                    "ai_provider": "",
                    "ai_model": "",
                    "tests_repo_url": "https://github.com/org/tests",
                    "submitted_by": "admin",
                }
            ),
        }
        result_data["request_params"]["submitted_by"] = "admin"
        await storage.save_result("job-reanalyze-defer", "", "completed", result_data)

        with (
            patch(
                "rootcoz.main.analyze_failure_group",
                new_callable=AsyncMock,
                return_value=[fa],
            ),
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("rootcoz.main.RepositoryManager"),
        ):
            response = test_client.post(f"/api/failures/{fa.id}/re-analyze")
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_re_analyze_failure_rejects_missing_ai_without_tests_repo(
        self, test_client
    ) -> None:
        """Empty stored AI without a tests repo still returns 400."""
        from rootcoz import storage

        fa = FailureAnalysis(
            test_name="tests.TestNoDefer.test_one",
            error="ValueError",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
        )
        fa_dict = fa.model_dump(mode="json")
        result_data = {
            "summary": "1 failure",
            "failures": [fa_dict],
            "request_params": encrypt_sensitive_fields(
                {
                    "ai_provider": "",
                    "ai_model": "",
                    "submitted_by": "admin",
                }
            ),
        }
        result_data["request_params"]["submitted_by"] = "admin"
        await storage.save_result("job-reanalyze-no-ai", "", "completed", result_data)

        response = test_client.post(f"/api/failures/{fa.id}/re-analyze")
        assert response.status_code == 400
        assert "AI" in response.json()["detail"]

    """Tests for _build_effective_jira_settings helper."""

    def test_no_user_token_returns_original(self):
        """When no user token, original settings returned unchanged."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings()
        result = _build_effective_jira_settings(settings, "", "")
        assert result is settings

    def test_user_token_clears_server_pat(self):
        """User token without email pins pat mode and clears the server PAT."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_server_url="https://jira.example.com",
            jira_pat=SecretStr("server-pat"),
            jira_api_token=SecretStr("server-api-token"),
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(settings, "user-token", "")
        assert result.jira_auth_mode == "pat"
        assert result.jira_pat is not None
        assert result.jira_pat.get_secret_value() == "user-token"
        assert result.jira_api_token is None

    def test_user_token_without_email_clears_server_email(self):
        """User token without email clears server email to avoid mismatched Cloud auth."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_server_url="https://jira.example.com",
            jira_email="server@example.com",
            jira_api_token=SecretStr("server-api-token"),
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(settings, "user-token", "")
        assert result.jira_auth_mode == "pat"
        assert result.jira_email is None
        assert result.jira_pat.get_secret_value() == "user-token"

    def test_user_token_with_email_sets_both(self):
        """User token with email sets both for Cloud auth."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "user-token", "user@example.com"
        )
        assert result.jira_api_token.get_secret_value() == "user-token"
        assert result.jira_email == "user@example.com"
        assert result.jira_pat is None

    def test_user_atst_token_routes_to_atst_mode(self):
        """An ATST-prefixed SA token pins the atst method, clearing the rest."""
        from rootcoz.jira import resolve_jira_connection
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_server_url="https://jira.example.com",
            jira_email="server@example.com",
            jira_api_token=SecretStr("server-api-token"),
            jira_pat=SecretStr("server-pat"),
            jira_oauth_client_secret=SecretStr("ATOA-server-secret"),
            atlassian_oauth_cloud_id="cloud-id",
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "", "", user_jira_sa_token="ATST-user-token"
        )
        assert result.jira_auth_mode == "atst"
        assert result.jira_atst_token.get_secret_value() == "ATST-user-token"
        assert result.jira_api_token is None
        assert result.jira_pat is None
        assert result.jira_email is None
        assert result.jira_oauth_client_secret is None
        # The effective settings resolve to a usable atst connection
        connection = resolve_jira_connection(result)
        assert connection.mode == "atst"
        assert connection.headers["Authorization"] == "Bearer ATST-user-token"

    def test_atst_in_regular_field_routes_to_atst_mode(self):
        """An ATST token pasted into the regular field pins atst, not basic."""
        from rootcoz.jira import resolve_jira_connection
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_email="server@example.com",
            jira_api_token=SecretStr("server-api-token"),
            atlassian_oauth_cloud_id="cloud-id",
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "ATST-user-token", "user@example.com"
        )
        assert result.jira_auth_mode == "atst"
        assert result.jira_atst_token.get_secret_value() == "ATST-user-token"
        assert result.jira_api_token is None
        assert result.jira_pat is None
        assert result.jira_email is None
        assert result.jira_oauth_client_secret is None
        # Resolves to the gateway with the token as Bearer
        connection = resolve_jira_connection(result)
        assert connection.mode == "atst"
        assert connection.api_base == "https://api.atlassian.com/ex/jira/cloud-id"
        assert connection.headers["Authorization"] == "Bearer ATST-user-token"

    def test_atst_in_regular_field_without_email_routes_to_atst(self):
        """An ATST token without an email is atst — not a bearer PAT."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_server_url="https://jira.example.com",
            jira_pat=SecretStr("server-pat"),
            atlassian_oauth_cloud_id="cloud-id",
        )
        result = _build_effective_jira_settings(settings, "ATST-user-token", "")
        assert result.jira_auth_mode == "atst"
        assert result.jira_atst_token.get_secret_value() == "ATST-user-token"
        # The no-email branch would have put it in jira_pat — it must not
        assert result.jira_pat is None

    def test_sa_field_classified_value_wins_over_regular_field(self):
        """Both fields classified: the service-account field decides."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            atlassian_oauth_cloud_id="cloud-id",
            atlassian_oauth_client_id="client-id",
        )
        result = _build_effective_jira_settings(
            settings, "ATST-user-token", "", user_jira_sa_token="ATOA-user-secret"
        )
        assert result.jira_auth_mode == "oauth"
        assert result.jira_oauth_client_secret.get_secret_value() == "ATOA-user-secret"
        assert result.jira_atst_token is None

    def test_original_settings_not_mutated(self):
        """model_copy must not mutate the original Settings instance."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_server_url="https://jira.example.com",
            jira_pat=SecretStr("server-pat"),
            jira_email="server@example.com",
            jira_project_key="TEST",
        )
        _build_effective_jira_settings(settings, "user-token", "user@example.com")
        # Original must be untouched
        assert settings.jira_pat.get_secret_value() == "server-pat"
        assert settings.jira_email == "server@example.com"

    def test_user_sa_secret_overrides_everything(self):
        """A user ATOA secret replaces all other Jira credentials unambiguously."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_server_url="https://jira.example.com",
            jira_email="server@example.com",
            jira_api_token=SecretStr("server-api-token"),
            jira_pat=SecretStr("server-pat"),
            jira_oauth_client_secret=SecretStr("ATOA-server-secret"),
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "user-token", "user@example.com", user_jira_sa_token="ATOA-user"
        )
        assert result.jira_oauth_client_secret.get_secret_value() == "ATOA-user"
        assert result.jira_api_token is None
        assert result.jira_pat is None
        assert result.jira_email is None

    def test_user_regular_token_clears_server_sa_secret(self):
        """A user regular token must not be bypassed by the server SA secret."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_oauth_client_secret=SecretStr("ATOA-server-secret"),
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "user-token", "user@example.com"
        )
        assert result.jira_oauth_client_secret is None
        assert result.jira_api_token.get_secret_value() == "user-token"
        assert result.jira_email == "user@example.com"

    def test_non_atoa_sa_secret_falls_through_to_regular(self):
        """A non-ATOA value in the SA field must not silently disable Jira."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "user-token", "user@example.com", user_jira_sa_token="not-atoa"
        )
        # Falls through to the regular-token branch instead of selecting
        # oauth mode with a secret the resolver would ignore.
        assert result.jira_oauth_client_secret is None
        assert result.jira_api_token.get_secret_value() == "user-token"
        assert result.jira_email == "user@example.com"

    def test_atoa_in_regular_field_routes_to_oauth(self):
        """An ATOA secret in the regular field selects oauth, matching validation."""
        from rootcoz.main import _build_effective_jira_settings

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_project_key="TEST",
        )
        result = _build_effective_jira_settings(
            settings, "ATOA-user-secret", "user@example.com"
        )
        assert result.jira_oauth_client_secret.get_secret_value() == "ATOA-user-secret"
        assert result.jira_api_token is None
        assert result.jira_email is None


class TestHasJiraCredentials:
    """_has_jira_credentials is driven by the selected auth mode."""

    def test_oauth_mode_non_atoa_secret_counts(self):
        """Oauth mode with any non-empty secret (not just ATOA) has credentials."""
        from rootcoz.main import _has_jira_credentials

        settings = Settings(
            atlassian_oauth_cloud_id="cloud-id",
            jira_auth_mode="oauth",
            jira_oauth_client_secret=SecretStr("not-atoa"),
            atlassian_oauth_client_id="test-client-id",
        )
        assert _has_jira_credentials(settings) is True

    def test_oauth_mode_missing_client_id_no_credentials(self):
        """Oauth mode without a client ID has no usable credentials."""
        from rootcoz.main import _has_jira_credentials

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="oauth",
            jira_oauth_client_secret=SecretStr("not-atoa"),
        )
        assert _has_jira_credentials(settings) is False

    def test_unset_mode_false_even_with_credentials(self):
        """No selected mode → Jira disabled → no credentials, ATOA or not."""
        from rootcoz.main import _has_jira_credentials

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_oauth_client_secret=SecretStr("ATOA-token"),
            jira_email="user@example.com",
            jira_api_token=SecretStr("token"),
            jira_pat=SecretStr("pat"),
        )
        assert _has_jira_credentials(settings) is False

    def test_selected_mode_with_missing_field_false(self):
        """A selected mode with an unconfigured required field → False."""
        from rootcoz.main import _has_jira_credentials

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
        )
        assert _has_jira_credentials(settings) is False

    def test_api_token_with_selected_mode_counts(self):
        from rootcoz.main import _has_jira_credentials

        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("token"),
        )
        assert _has_jira_credentials(settings) is True


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

    @pytest.mark.asyncio
    async def test_jira_sa_secret_validates_against_gateway(self, test_client):
        """An ATOA secret is minted and validated via project visibility."""
        import rootcoz.jira as jira_module
        from rootcoz.main import app, get_settings

        jira_module._sa_token_cache.clear()
        scoped_settings = _build_wait_settings(
            atlassian_oauth_cloud_id="cloud-id-123",
            jira_site_url="https://myorg.atlassian.net",
            atlassian_oauth_client_id="test-client-id",
        )
        app.dependency_overrides[get_settings] = lambda: scoped_settings
        try:
            mint_resp = httpx.Response(
                200,
                json={"access_token": "minted-user-token", "expires_in": 3600},
                request=httpx.Request("POST", "https://auth.atlassian.com/oauth/token"),
            )
            # One patch covers both calls: minting (rootcoz.jira) and the
            # probe (rootcoz.main) share the global httpx.AsyncClient.
            with patch("rootcoz.main.httpx.AsyncClient") as mock_client_class:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.raise_for_status = MagicMock()
                mock_resp.json.return_value = [{"key": "PROJ"}]
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.post = AsyncMock(return_value=mint_resp)
                mock_client_class.return_value = mock_client
                response = test_client.post(
                    "/api/validate-token",
                    json={"token_type": "jira", "token": "ATOA-user-secret"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            data = response.json()
            assert data["valid"] is True
            assert data["username"] == "service account"
            # The mint request carries the client_credentials grant
            mint_body = mock_client.post.call_args.kwargs["json"]
            assert mint_body["grant_type"] == "client_credentials"
            client_secret = mint_body["client_secret"]
            assert client_secret == "ATOA-user-secret"  # pragma: allowlist secret
            # The probe is project visibility (read:jira-work), not /myself
            requested_url = mock_client.get.call_args.args[0]
            assert requested_url == (
                "https://api.atlassian.com/ex/jira/cloud-id-123/rest/api/3/project"
            )
            assert mock_client.get.call_args.kwargs["params"] == {"maxResults": 1}
            # The Authorization header carries the MINTED token, never the secret
            sent_headers = mock_client.get.call_args.kwargs["headers"]
            assert sent_headers["Authorization"] == "Bearer minted-user-token"
        finally:
            app.dependency_overrides.pop(get_settings, None)
            jira_module._sa_token_cache.clear()

    @pytest.mark.asyncio
    async def test_jira_sa_secret_mint_failure_reports_clear_error(self, test_client):
        """A 401 from the token endpoint surfaces as an invalid-token result."""
        import rootcoz.jira as jira_module
        from rootcoz.main import app, get_settings

        jira_module._sa_token_cache.clear()
        scoped_settings = _build_wait_settings(
            atlassian_oauth_cloud_id="cloud-id-123",
            jira_site_url="https://myorg.atlassian.net",
            atlassian_oauth_client_id="test-client-id",
        )
        app.dependency_overrides[get_settings] = lambda: scoped_settings
        try:
            mint_resp = httpx.Response(
                401,
                json={"error": "invalid_client"},
                request=httpx.Request("POST", "https://auth.atlassian.com/oauth/token"),
            )
            with patch("rootcoz.jira.httpx.AsyncClient") as mint_client_class:
                mint_client = AsyncMock()
                mint_client.post.return_value = mint_resp
                mint_client.__aenter__ = AsyncMock(return_value=mint_client)
                mint_client.__aexit__ = AsyncMock(return_value=False)
                mint_client_class.return_value = mint_client
                response = test_client.post(
                    "/api/validate-token",
                    json={"token_type": "jira", "token": "ATOA-bad-secret"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            data = response.json()
            assert data["valid"] is False
            assert "HTTP 401" in data["message"]
            assert "check client ID and secret" in data["message"]
        finally:
            app.dependency_overrides.pop(get_settings, None)
            jira_module._sa_token_cache.clear()

    @pytest.mark.asyncio
    async def test_jira_sa_secret_without_server_client_id(self, test_client):
        """An ATOA secret with no server client ID reports a clear failure."""
        from rootcoz.main import app, get_settings

        scoped_settings = _build_wait_settings(
            atlassian_oauth_cloud_id="cloud-id-123",
        )
        app.dependency_overrides[get_settings] = lambda: scoped_settings
        try:
            response = test_client.post(
                "/api/validate-token",
                json={"token_type": "jira", "token": "ATOA-user-secret"},
                headers=_ADMIN_AUTH_HEADERS,
            )
            assert response.status_code == 200
            data = response.json()
            assert data["valid"] is False
            assert "atlassian_oauth_client_id" in data["message"]
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_jira_sa_secret_unresolvable_cloud_id(self, test_client):
        """An ATOA secret with no resolvable cloud ID reports a clear failure."""
        import rootcoz.jira as jira_module
        from rootcoz.main import app, get_settings

        jira_module._cloud_id_cache.clear()
        scoped_settings = _build_wait_settings(
            jira_site_url="https://myorg.atlassian.net",
            atlassian_oauth_client_id="test-client-id",
        )
        app.dependency_overrides[get_settings] = lambda: scoped_settings
        try:
            with patch("rootcoz.jira.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.get.side_effect = httpx.ConnectError("refused")
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_class.return_value = mock_client
                response = test_client.post(
                    "/api/validate-token",
                    json={"token_type": "jira", "token": "ATOA-user-secret"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            data = response.json()
            assert data["valid"] is False
            assert "cloud ID" in data["message"]
        finally:
            app.dependency_overrides.pop(get_settings, None)
            jira_module._cloud_id_cache.clear()

    @pytest.mark.asyncio
    async def test_jira_atst_token_validates_against_gateway(self, test_client):
        """An ATST token is sent as Bearer to the gateway — no minting."""
        import rootcoz.jira as jira_module
        from rootcoz.main import app, get_settings

        jira_module._cloud_id_cache.clear()
        jira_module._sa_token_cache.clear()
        scoped_settings = _build_wait_settings(
            atlassian_oauth_cloud_id="cloud-id-123",
            jira_site_url="https://myorg.atlassian.net",
        )
        app.dependency_overrides[get_settings] = lambda: scoped_settings
        try:
            with patch("rootcoz.main.httpx.AsyncClient") as mock_client_class:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.raise_for_status = MagicMock()
                mock_resp.json.return_value = [{"key": "PROJ"}]
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.post = AsyncMock()
                mock_client_class.return_value = mock_client
                response = test_client.post(
                    "/api/validate-token",
                    json={"token_type": "jira", "token": "ATST-user-token"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            data = response.json()
            assert data["valid"] is True
            assert data["username"] == "service account"
            assert "ATST" in data["message"]
            # The probe is project visibility against the scoped gateway
            requested_url = mock_client.get.call_args.args[0]
            assert requested_url == (
                "https://api.atlassian.com/ex/jira/cloud-id-123/rest/api/3/project"
            )
            # The ATST token itself is the Bearer — nothing is minted
            sent_headers = mock_client.get.call_args.kwargs["headers"]
            assert sent_headers["Authorization"] == "Bearer ATST-user-token"
            assert mock_client.post.await_count == 0
        finally:
            app.dependency_overrides.pop(get_settings, None)
            jira_module._cloud_id_cache.clear()
            jira_module._sa_token_cache.clear()


class TestJiraProjectsEndpoint:
    """Tests for POST /api/jira-projects."""

    def test_no_jira_url_returns_empty(self, test_client):
        """No JIRA_URL configured returns empty list."""
        response = test_client.post(
            "/api/jira-projects", json={}, headers=_ADMIN_AUTH_HEADERS
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_legacy_sa_token_rejected(self, test_client):
        """A legacy (non-ATOA) SA secret is a clear 400, not a silent drop."""
        response = test_client.post(
            "/api/jira-projects",
            json={"jira_sa_token": "legacy-secret-token"},  # pragma: allowlist secret
            headers=_ADMIN_AUTH_HEADERS,
        )
        assert response.status_code == 400
        assert "not a recognized" in response.json()["detail"]

    def test_legacy_sa_token_rejected_with_regular_token(self, test_client):
        """A legacy SA secret 400s even alongside a regular token (fail closed)."""
        response = test_client.post(
            "/api/jira-projects",
            json={
                "jira_token": "user-pat",
                "jira_sa_token": "legacy-secret-token",  # pragma: allowlist secret
            },
            headers=_ADMIN_AUTH_HEADERS,
        )
        assert response.status_code == 400
        assert "not a recognized" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_atst_sa_token_routes_to_atst_mode(self, test_client):
        """An ATST-prefixed SA token is accepted and pins the atst method."""
        from rootcoz.main import app, get_settings

        projects = [{"key": "PROJ", "name": "My Project"}]
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic",
            jira_cloud_url="https://jira-cloud.example.com",
            atlassian_oauth_cloud_id="cloud-id-123",
        )
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
                    json={
                        "jira_sa_token": "ATST-user-token"
                    },  # pragma: allowlist secret
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            assert any(p["key"] == "PROJ" for p in response.json())
            # The client was built from EFFECTIVE settings: mode atst with
            # the user's token as the ATST credential
            effective = MockJiraClient.call_args.args[0]
            assert effective.jira_auth_mode == "atst"
            assert (
                effective.jira_atst_token.get_secret_value() == "ATST-user-token"
            )  # pragma: allowlist secret
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_atst_sa_token_with_regular_token_unaffected(self, test_client):
        """A recognized ATST SA token alongside a regular token is accepted."""
        from rootcoz.main import app, get_settings

        projects = [{"key": "PROJ", "name": "My Project"}]
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic",
            jira_cloud_url="https://jira-cloud.example.com",
            atlassian_oauth_cloud_id="cloud-id-123",
        )
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
                    json={
                        "jira_token": "user-pat",
                        "jira_sa_token": "ATST-user-token",  # pragma: allowlist secret
                    },
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            # The classified SA token wins over the regular token's field
            # placement — effective mode atst, never basic/pat
            effective = MockJiraClient.call_args.args[0]
            assert effective.jira_auth_mode == "atst"
            assert (
                effective.jira_atst_token.get_secret_value() == "ATST-user-token"
            )  # pragma: allowlist secret
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_atst_token_in_regular_field_routes_to_atst(self, test_client):
        """An ATST token pasted into the regular jira_token field pins atst.

        Validation recognizes the prefix in the regular field, so the
        effective settings must route it the same way — otherwise a
        token that validates fine breaks every saved-credential use.
        """
        from rootcoz.main import app, get_settings

        projects = [{"key": "PROJ", "name": "My Project"}]
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic",
            jira_cloud_url="https://jira-cloud.example.com",
            atlassian_oauth_cloud_id="cloud-id-123",
        )
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
                    json={
                        "jira_token": "ATST-user-token",  # pragma: allowlist secret
                        "jira_email": "user@example.com",
                    },
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            # Prefix detection wins over field placement: atst mode with the
            # gateway api_base, never basic with the email
            effective = MockJiraClient.call_args.args[0]
            assert effective.jira_auth_mode == "atst"
            assert (
                effective.jira_atst_token.get_secret_value() == "ATST-user-token"
            )  # pragma: allowlist secret
            assert effective.jira_email is None
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_user_method_url_used_when_server_method_url_empty(self, test_client):
        """Server basic URL empty + user pins pat → the EFFECTIVE URL decides."""
        from rootcoz.main import app, get_settings

        projects = [{"key": "PROJ", "name": "My Project"}]
        # Server-selected method (basic) has no URL; the user's bare token
        # pins pat, whose URL IS configured — the list must not be empty.
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic",
            jira_cloud_url="",
            jira_server_url="https://jira-server.example.com",
        )
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
                    json={"jira_token": "tok"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            assert any(p["key"] == "PROJ" for p in response.json())
            MockJiraClient.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_empty_effective_method_url_returns_empty(self, test_client):
        """Server basic URL set + user pins pat with no server URL → []."""
        from rootcoz.main import app, get_settings

        # The server-selected method's URL is set, but the user's pinned
        # method (pat) has none — no swallowed failure, just [].
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic",
            jira_cloud_url="https://jira-cloud.example.com",
            jira_server_url="",
        )
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            with patch("rootcoz.main.JiraClient") as MockJiraClient:
                response = test_client.post(
                    "/api/jira-projects",
                    json={"jira_token": "tok"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            assert response.json() == []
            MockJiraClient.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_returns_projects(self, test_client):
        """Returns project list from JiraClient.list_projects."""
        from rootcoz.main import app, get_settings

        projects = [{"key": "PROJ", "name": "My Project"}]
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic", jira_cloud_url="https://jira.example.com"
        )
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

        jira_settings = _build_wait_settings(
            jira_auth_mode="basic", jira_cloud_url="https://jira.example.com"
        )
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

    def test_legacy_sa_token_rejected(self, test_client):
        """A legacy (non-ATOA) SA secret is a clear 400, not a silent drop."""
        response = test_client.post(
            "/api/jira-security-levels",
            json={
                "project_key": "PROJ",
                "jira_sa_token": "legacy-secret-token",  # pragma: allowlist secret
            },
            headers=_ADMIN_AUTH_HEADERS,
        )
        assert response.status_code == 400
        assert "not a recognized" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_user_method_url_used_when_server_method_url_empty(self, test_client):
        """Server basic URL empty + user pins pat → the EFFECTIVE URL decides."""
        from rootcoz.main import app, get_settings

        levels = [{"id": "10", "name": "Internal", "description": "Internal only"}]
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic",
            jira_cloud_url="",
            jira_server_url="https://jira-server.example.com",
        )
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
                    json={"project_key": "PROJ", "jira_token": "tok"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            assert response.json() == levels
            MockJiraClient.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_empty_effective_method_url_returns_empty(self, test_client):
        """Server basic URL set + user pins pat with no server URL → []."""
        from rootcoz.main import app, get_settings

        jira_settings = _build_wait_settings(
            jira_auth_mode="basic",
            jira_cloud_url="https://jira-cloud.example.com",
            jira_server_url="",
        )
        app.dependency_overrides[get_settings] = lambda: jira_settings
        try:
            with patch("rootcoz.main.JiraClient") as MockJiraClient:
                response = test_client.post(
                    "/api/jira-security-levels",
                    json={"project_key": "PROJ", "jira_token": "tok"},
                    headers=_ADMIN_AUTH_HEADERS,
                )
            assert response.status_code == 200
            assert response.json() == []
            MockJiraClient.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_settings, None)

    @pytest.mark.asyncio
    async def test_returns_security_levels(self, test_client):
        """Returns security levels from JiraClient.list_security_levels."""
        from rootcoz.main import app, get_settings

        levels = [{"id": "10", "name": "Internal", "description": "Internal only"}]
        jira_settings = _build_wait_settings(
            jira_auth_mode="basic", jira_cloud_url="https://jira.example.com"
        )
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

    @staticmethod
    def _healthy_client(test_client):
        """Context manager: /api/health with a healthy mocked sidecar.

        An unreachable sidecar is unhealthy (503) since #347 r3, so tests
        asserting 200 from /api/health must mock the sidecar check.
        """

        @contextmanager
        def _ctx():
            with patch(
                "rootcoz.monitoring.check_ai_provider",
                new_callable=AsyncMock,
                return_value={"status": "ok", "provider": "claude", "model": "m"},
            ):
                yield test_client

        return _ctx()

    def test_gzip_middleware_registered(self, test_client):
        """Verify GZipMiddleware compresses responses above minimum size."""
        with self._healthy_client(test_client) as client:
            response = client.get(
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
        with self._healthy_client(test_client) as client:
            response = client.get("/api/health")
        cache_control = response.headers.get("cache-control", "")
        assert "immutable" not in cache_control


class TestAdminSettingsEndpoints:
    """Tests for /api/admin/settings endpoints."""

    _NO_ADMIN_HEADERS: ClassVar[dict[str, str]] = {"Authorization": ""}

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
        assert "is_set" in item
        assert "description" in item
        # Sensitive values are masked by default
        sensitive_items = [i for i in data if i["sensitive"] and i["value"]]
        for si in sensitive_items:
            assert si["value"] == "••••••••", (
                f"Sensitive field {si['key']} not masked by default"
            )

    def test_every_setting_reports_whether_it_is_set(self, test_client) -> None:
        """#379 E2E-J09: `is_set` is a boolean on every entry, never a hint."""
        data = test_client.get("/api/admin/settings").json()
        assert all(isinstance(item["is_set"], bool) for item in data)

    @pytest.mark.parametrize("key", ["github_token", "gcs_bucket"])
    def test_unset_setting_reports_is_set_false(self, test_client, key: str) -> None:
        """An empty secret must not look stored — the flag says so, not the mask."""
        item = _settings_item(test_client, key)
        assert item["value"] == ""
        assert item["is_set"] is False

    def test_configured_setting_reports_is_set_true(self, test_client) -> None:
        """A saved value flips `is_set`, and a secret stays masked either way."""
        plain = test_client.put(
            "/api/admin/settings", json={"settings": {"gcs_bucket": "my-bucket"}}
        )
        assert plain.status_code == 200, plain.json()
        assert _settings_item(test_client, "gcs_bucket")["is_set"] is True

        secret = "gh-configured-token"  # pragma: allowlist secret
        stored = test_client.put(
            "/api/admin/settings", json={"settings": {"github_token": secret}}
        )
        assert stored.status_code == 200, stored.json()
        item = _settings_item(test_client, "github_token")
        assert item["is_set"] is True
        # The flag never becomes a way to read the secret back
        assert item["value"] == "••••••••" and secret not in json.dumps(item)

    def test_login_lockout_threshold_is_a_runtime_auth_setting(
        self, test_client
    ) -> None:
        """§I.1: the threshold is an Auth & Security setting, ``0`` disables it."""
        from rootcoz.config import get_settings

        key = "login_lockout_threshold"
        item = next(
            s for s in test_client.get("/api/admin/settings").json() if s["key"] == key
        )
        assert (item["category"], item["type"], item["default"]) == (
            "Auth & Security",
            "integer",
            "5",
        )
        assert not item["sensitive"] and not item["restart_required"]
        # A negative threshold is a typo, not a policy: 400. ``0`` is legal.
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {key: "-1"}}
            ).status_code
            == 400
        )
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {key: "0"}}
            ).status_code
            == 200
        )
        assert get_settings().login_lockout_threshold == 0

    def test_login_lockout_enabled_is_a_runtime_auth_toggle(self, test_client) -> None:
        """E2E-I09/I10: an explicit switch, rendered as a switch, before the count."""
        from rootcoz.config import get_settings

        settings = test_client.get("/api/admin/settings").json()
        keys = [s["key"] for s in settings]
        item = next(s for s in settings if s["key"] == "login_lockout_enabled")
        assert (item["category"], item["type"], item["default"], item["source"]) == (
            "Auth & Security",
            "boolean",
            "true",
            "default",
        )
        assert not item["sensitive"] and not item["restart_required"]
        # The toggle reads as the off switch, so it sits directly before the count.
        assert keys.index("login_lockout_enabled") + 1 == keys.index(
            "login_lockout_threshold"
        )

        assert (
            test_client.put(
                "/api/admin/settings",
                json={"settings": {"login_lockout_enabled": "false"}},
            ).status_code
            == 200
        )
        assert get_settings().login_lockout_enabled is False

    def test_repo_clone_limit_metadata_and_db_override(self, test_client) -> None:
        """Clone concurrency is a live, validated Server setting."""
        from rootcoz.config import get_settings

        key = "max_concurrent_repo_clones"
        item = next(
            s for s in test_client.get("/api/admin/settings").json() if s["key"] == key
        )
        assert (item["category"], item["type"], item["default"]) == (
            "Server",
            "integer",
            "10",
        )
        assert not item["sensitive"] and not item["restart_required"]
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {key: "0"}}
            ).status_code
            == 400
        )
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {key: "3"}}
            ).status_code
            == 200
        )
        assert get_settings().max_concurrent_repo_clones == 3
        item = next(
            s for s in test_client.get("/api/admin/settings").json() if s["key"] == key
        )
        assert (item["value"], item["source"]) == ("3", "db")

    def test_jira_auth_mode_env_var_ignored(self, test_client, monkeypatch) -> None:
        """A JIRA_AUTH_MODE env var has no effect — Server Settings only."""
        from rootcoz.config import get_settings

        monkeypatch.setenv("JIRA_AUTH_MODE", "basic")
        get_settings.cache_clear()
        item = _settings_item(test_client, "jira_auth_mode")
        assert (item["value"], item["source"], item["env_var"]) == (
            "",
            "default",
            "",
        )
        from rootcoz.jira import resolve_jira_connection

        assert resolve_jira_connection(get_settings()).mode == "none"

    def test_jira_auth_mode_settings_item_and_round_trip(self, test_client) -> None:
        """jira_auth_mode is a standalone, non-sensitive, validated setting."""
        item = next(
            s
            for s in test_client.get("/api/admin/settings").json()
            if s["key"] == "jira_auth_mode"
        )
        assert (item["category"], item["type"], item["value"]) == (
            "Jira",
            "string",
            "",
        )
        assert not item["sensitive"] and not item["restart_required"]

        # Invalid values are rejected with a clear error
        response = test_client.put(
            "/api/admin/settings", json={"settings": {"jira_auth_mode": "nope"}}
        )
        assert response.status_code == 400
        assert "jira_auth_mode" in response.json()["detail"]
        assert "oauth" in response.json()["detail"]

        # Valid value saves and round-trips; empty resets to auto-detect
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {"jira_auth_mode": "basic"}}
            ).status_code
            == 200
        )
        item = next(
            s
            for s in test_client.get("/api/admin/settings").json()
            if s["key"] == "jira_auth_mode"
        )
        assert (item["value"], item["source"]) == ("basic", "db")
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {"jira_auth_mode": ""}}
            ).status_code
            == 200
        )

    def test_settings_provider_options_survive_sidecar_outage(
        self, test_client, monkeypatch
    ) -> None:
        """Settings load stays fast and complete when sidecar discovery fails."""
        from rootcoz import ai_client

        discover = AsyncMock(side_effect=httpx.ConnectError("sidecar down"))
        monkeypatch.setattr(ai_client, "_list_models_raw", discover)
        monkeypatch.setattr(ai_client, "_model_catalog_cache", None)
        response = test_client.get("/api/admin/settings")
        assert response.status_code == 200
        item = next(s for s in response.json() if s["key"] == "active_server_providers")
        assert item["options"] == []
        # Cached failure: a second settings load does not re-attempt discovery.
        second = test_client.get("/api/admin/settings")
        assert second.status_code == 200
        discover.assert_awaited_once()

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

    def test_ambient_auth_providers_saved_canonical_deduped(
        self, test_client, monkeypatch
    ) -> None:
        """Valid IDs save stripped, de-duplicated and in the registered spelling."""
        _no_key_providers(monkeypatch)
        _provider_auth_status(monkeypatch, {"claude": True, "gemini": True})
        monkeypatch.setenv("CLAUDE_API_KEY", "claude-key")  # pragma: allowlist secret
        response = test_client.put(
            "/api/admin/settings",
            json={
                "settings": {"ambient_auth_providers": " Claude ,  gemini,CLAUDE ,, "}
            },
        )
        assert response.status_code == 200, response.json()
        item = _settings_item(test_client, "ambient_auth_providers")
        # The catalog spells them lowercase: the stored value must too, so the
        # consumer's exclusion matches the sidecar's registry id (#378).
        assert (item["value"], item["source"]) == ("claude, gemini", "db")

    def test_ambient_auth_providers_rejects_an_id_with_no_deployment_credential(
        self, test_client, monkeypatch
    ) -> None:
        """A provider nobody can authenticate is a 400 that names it (#379 J08)."""
        _no_key_providers(monkeypatch)
        _provider_auth_status(monkeypatch, {"claude": False})
        from rootcoz import ai_client

        ai_client.update_model_catalog([{"provider": "claude", "id": "sonnet"}])
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        before = _settings_item(test_client, "ambient_auth_providers")
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ambient_auth_providers": "claude"}},
        )
        assert response.status_code == 400, response.json()
        detail = response.json()["detail"]
        assert "no deployment credential" in detail
        assert "claude" in detail
        # Nothing declared ambient, so nothing was saved
        assert _settings_item(test_client, "ambient_auth_providers") == before

    def test_ambient_auth_providers_accepts_a_deployment_env_credential(
        self, test_client, monkeypatch
    ) -> None:
        """An ambient <PROVIDER>_API_KEY in the deployment env is accepted."""
        _no_key_providers(monkeypatch)
        # No sidecar answer is needed: the deployment env already resolves it
        _provider_auth_status(monkeypatch, {})
        from rootcoz import ai_client

        ai_client.update_model_catalog([{"provider": "claude", "id": "sonnet"}])
        monkeypatch.setenv("CLAUDE_API_KEY", "claude-key")  # pragma: allowlist secret
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ambient_auth_providers": "claude"}},
        )
        assert response.status_code == 200, response.json()
        assert (
            _settings_item(test_client, "ambient_auth_providers")["value"] == "claude"
        )

    def test_ambient_auth_providers_accepts_a_sidecar_authenticated_provider(
        self, test_client, monkeypatch
    ) -> None:
        """A provider the sidecar reports as configured is accepted (#379 J08)."""
        _no_key_providers(monkeypatch)
        monkeypatch.setattr(
            "rootcoz.main.supported_key_providers",
            AsyncMock(return_value=["google-vertex-claude"]),
        )
        # ADC/Vertex authenticates it — no <PROVIDER>_API_KEY anywhere
        _provider_auth_status(monkeypatch, {"google-vertex-claude": True})
        monkeypatch.delenv("GOOGLE_VERTEX_CLAUDE_API_KEY", raising=False)
        # Start from an empty list so only discovery can make this ID known
        assert (
            test_client.put(
                "/api/admin/settings",
                json={"settings": {"ambient_auth_providers": ""}},
            ).status_code
            == 200
        )
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ambient_auth_providers": "Google-Vertex-Claude"}},
        )
        assert response.status_code == 200, response.json()
        # Stored as the sidecar registers it, not as the admin typed it (#378)
        assert (
            _settings_item(test_client, "ambient_auth_providers")["value"]
            == "google-vertex-claude"
        )

    def test_ambient_auth_providers_unknown_id_rejected(
        self, test_client, monkeypatch
    ) -> None:
        """A typo is a 400 that names it — and nothing is saved (#378)."""
        _no_key_providers(monkeypatch)
        before = _settings_item(test_client, "ambient_auth_providers")
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ambient_auth_providers": "google-vertex-claud"}},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "Unknown provider id(s) in AMBIENT_AUTH_PROVIDERS" in detail
        assert "google-vertex-claud" in detail
        # The known set is listed so the admin can pick a real ID
        assert "known: claude, gemini" in detail
        # The rejected value never reached the store
        assert _settings_item(test_client, "ambient_auth_providers") == before

    def test_ambient_auth_providers_rejection_lists_every_known_provider(
        self, test_client, monkeypatch
    ) -> None:
        """The 400 names the whole known set, never a truncated sample (#378)."""
        _no_key_providers(monkeypatch)
        from rootcoz import ai_client

        catalog = [
            {"provider": f"provider-{index}", "id": f"provider-{index}/model"}
            for index in range(15)
        ]
        ai_client.update_model_catalog(catalog)
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ambient_auth_providers": "nope"}},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        for entry in catalog:
            assert entry["provider"] in detail
        assert "more)" not in detail and "(+" not in detail

    def test_ambient_auth_providers_empty_allowed(
        self, test_client, monkeypatch
    ) -> None:
        """An empty value is valid: nothing is excluded from the key flow."""
        _no_key_providers(monkeypatch)
        response = test_client.put(
            "/api/admin/settings", json={"settings": {"ambient_auth_providers": ""}}
        )
        assert response.status_code == 200, response.json()
        assert _settings_item(test_client, "ambient_auth_providers")["value"] == ""

    def test_ambient_auth_providers_accepted_when_discovery_unavailable(
        self, test_client, monkeypatch, caplog
    ) -> None:
        """A cold catalog plus no discovery answer must not reject a save."""

        from rootcoz import ai_client

        monkeypatch.setattr(ai_client, "_model_catalog_cache", None)
        monkeypatch.setattr(
            "rootcoz.main.supported_key_providers", AsyncMock(return_value=[])
        )
        _provider_auth_status(monkeypatch, {})
        with caplog.at_level(logging.WARNING, logger="rootcoz.main"):
            logging.getLogger("rootcoz.main").propagate = True
            try:
                response = test_client.put(
                    "/api/admin/settings",
                    json={"settings": {"ambient_auth_providers": "Some New Provider"}},
                )
            finally:
                logging.getLogger("rootcoz.main").propagate = False
        assert response.status_code == 200, response.json()
        # Accepted unchanged — a transient sidecar gap is never a save failure
        assert (
            _settings_item(test_client, "ambient_auth_providers")["value"]
            == "Some New Provider"
        )
        # Fail-open is logged, so a typo accepted during an outage is traceable
        assert any("No provider catalog" in r.getMessage() for r in caplog.records)

    def test_ambient_auth_providers_accepted_when_credentials_unknown(
        self, test_client, monkeypatch, caplog
    ) -> None:
        """A warm catalog whose deployment credentials cannot be read fails open."""

        _no_key_providers(monkeypatch)
        monkeypatch.setattr(
            "rootcoz.ai_client.deployment_provider_credentials", lambda: None
        )
        with caplog.at_level(logging.WARNING, logger="rootcoz.main"):
            logging.getLogger("rootcoz.main").propagate = True
            try:
                response = test_client.put(
                    "/api/admin/settings",
                    json={"settings": {"ambient_auth_providers": "claude"}},
                )
            finally:
                logging.getLogger("rootcoz.main").propagate = False
        assert response.status_code == 200, response.json()
        assert any(
            "Deployment credentials unknown" in r.getMessage() for r in caplog.records
        )

    def test_put_settings_server_only_rejected(self, test_client) -> None:
        """PUT rejects bootstrap-only settings (db_path/debug — env-only forever, #347)."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"db_path": "/tmp/other.db", "debug": "true"}},
        )
        assert response.status_code == 400
        assert "Unknown settings" in response.json()["detail"]
        assert "db_path" in response.json()["detail"]
        assert "debug" in response.json()["detail"]

    def test_put_settings_account_id_shaped_cloud_id_rejected(
        self, test_client
    ) -> None:
        """PUT rejects account-ID-shaped (org:uuid) cloud IDs with a 422 (#365).

        The account ID is a frequent mistaken value for the cloud ID; without
        the check the connection later fails with a confusing HTTP 404 from
        the api.atlassian.com gateway.
        """
        response = test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "atlassian_oauth_cloud_id": (
                        "712020:f0a60b32-1a2b-3c4d-5e6f-789012345678"
                    )
                }
            },
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "atlassian_oauth_cloud_id" in detail
        assert "account ID" in detail
        assert "tenant_info" in detail

    def test_put_settings_plain_uuid_cloud_id_accepted(self, test_client) -> None:
        """PUT accepts a plain-UUID cloud ID and it round-trips."""
        response = test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "atlassian_oauth_cloud_id": ("2b9e35e3-6bd3-4cec-b838-f4249ee02432")
                }
            },
        )
        assert response.status_code == 200
        item = next(
            s
            for s in test_client.get("/api/admin/settings").json()
            if s["key"] == "atlassian_oauth_cloud_id"
        )
        assert item["value"] == "2b9e35e3-6bd3-4cec-b838-f4249ee02432"

    def test_put_settings_previously_blocked_now_settable(self, test_client) -> None:
        """#347: the former _SERVER_ONLY_SETTINGS are admin-settable now."""
        response = test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "auto_push_exporters": "reportportal",
                    "require_approval": "false",
                    "public_base_url": "https://rootcoz.example.com",
                }
            },
        )
        assert response.status_code == 200
        assert set(response.json()["updated"]) == {
            "auto_push_exporters",
            "require_approval",
            "public_base_url",
        }

    def test_put_settings_empty_body(self, test_client) -> None:
        """PUT with no settings returns 400."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {}},
        )
        assert response.status_code == 400

    def test_put_settings_invalid_prow_url(self, test_client) -> None:
        """PUT rejects invalid prow_url values."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"prow_url": "http://prow.example.com"}},
        )
        assert response.status_code == 400
        assert "https://" in response.json()["detail"]

    def test_put_settings_prow_url_with_credentials_rejected(self, test_client) -> None:
        response = test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "prow_url": "https://user:pass@prow.example.com",  # pragma: allowlist secret
                }
            },
        )
        assert response.status_code == 400
        assert "credentials" in response.json()["detail"]

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

    def test_has_env_var_flag_when_db_shadows_env(self, test_client) -> None:
        """DB-overridden settings flag has_env_var when OS env var also exists."""
        # JENKINS_URL is set as an OS env var in the mock_settings fixture.
        # Set a DB override for the same setting.
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"jenkins_url": "https://other.jenkins.com"}},
        )
        get_resp = test_client.get("/api/admin/settings")
        settings = get_resp.json()
        jenkins_url = next(s for s in settings if s["key"] == "jenkins_url")
        assert jenkins_url["source"] == "db"
        # JENKINS_URL env var exists in the test environment, so has_env_var should be True
        assert jenkins_url.get("has_env_var") is True

    def test_no_has_env_var_when_only_db(self, test_client) -> None:
        """DB-only settings (no OS env var) should not have has_env_var."""
        # ai_call_timeout is unlikely to have an OS env var in test
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "25"}},
        )
        get_resp = test_client.get("/api/admin/settings")
        settings = get_resp.json()
        ai_timeout = next(s for s in settings if s["key"] == "ai_call_timeout")
        assert ai_timeout["source"] == "db"
        assert "has_env_var" not in ai_timeout

    def test_delete_unknown_key(self, test_client) -> None:
        """DELETE with unknown key returns 404."""
        response = test_client.delete("/api/admin/settings/nonexistent_key")
        assert response.status_code == 404

    def test_delete_no_override(self, test_client) -> None:
        """DELETE when no DB override exists returns 404."""
        response = test_client.delete("/api/admin/settings/jenkins_url")
        assert response.status_code == 404

    def test_put_empty_ai_model_overrides_env(self, test_client, monkeypatch) -> None:
        """Empty ai_model is an explicit DB override that beats AI_MODEL env (#352)."""
        monkeypatch.setenv("AI_MODEL", "claude-opus-4-6-1m")
        from rootcoz.config import clear_db_settings_cache, get_settings

        clear_db_settings_cache()
        get_settings.cache_clear()

        # Set provider + model first
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {"ai_provider": "cursor", "ai_model": "cursor:composer-2"}
            },
        )
        # Clear model — the saved empty is an explicit override; only the
        # reset endpoint (DELETE) falls back to env
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_model": ""}},
        )
        assert response.status_code == 200

        get_resp = test_client.get("/api/admin/settings")
        settings = get_resp.json()
        ai_model = next(s for s in settings if s["key"] == "ai_model")
        assert ai_model["source"] == "db"
        assert ai_model["value"] == ""
        assert get_settings().ai_model == ""

    def test_reset_ai_model_restores_env(self, test_client, monkeypatch) -> None:
        """DELETE removes the override so the AI_MODEL env governs again."""
        monkeypatch.setenv("AI_MODEL", "claude-opus-4-6-1m")
        from rootcoz.config import clear_db_settings_cache, get_settings

        clear_db_settings_cache()
        get_settings.cache_clear()

        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_model": "cursor:composer-2"}},
        )
        response = test_client.delete("/api/admin/settings/ai_model")
        assert response.status_code == 200

        get_resp = test_client.get("/api/admin/settings")
        ai_model = next(s for s in get_resp.json() if s["key"] == "ai_model")
        assert ai_model["source"] == "env"
        assert ai_model["value"] == "claude-opus-4-6-1m"
        assert get_settings().ai_model == "claude-opus-4-6-1m"

    def test_put_empty_ai_provider_overrides_env(
        self, test_client, monkeypatch
    ) -> None:
        """Empty ai_provider is an explicit override that beats AI_PROVIDER env."""
        monkeypatch.setenv("AI_PROVIDER", "claude")
        from rootcoz.config import clear_db_settings_cache, get_settings

        clear_db_settings_cache()
        get_settings.cache_clear()

        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_provider": "cursor"}},
        )
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_provider": ""}},
        )
        assert response.status_code == 200

        get_resp = test_client.get("/api/admin/settings")
        settings = get_resp.json()
        ai_provider = next(s for s in settings if s["key"] == "ai_provider")
        assert ai_provider["source"] == "db"
        assert get_settings().ai_provider == ""

    def test_put_empty_non_string_setting_rejected(self, test_client) -> None:
        """Non-string settings reject empty saves — an empty bool/int override
        would fail pydantic validation and drop the whole DB override set."""
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "30"}},
        )
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": ""}},
        )
        assert response.status_code == 400
        assert "cannot be empty" in response.json()["detail"]

    def test_put_empty_default_user_role_rejected(self, test_client) -> None:
        """default_user_role is str-typed but its validator rejects "" — an
        empty override there would poison get_settings() and silently drop
        every DB override (Qodo r1 finding 2)."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"default_user_role": ""}},
        )
        assert response.status_code == 400
        assert "cannot be empty" in response.json()["detail"]

    def test_put_empty_sidecar_listener_rejected(self, test_client) -> None:
        """sidecar_host/sidecar_port reject empty overrides — the sidecar env
        bridge would export blank SIDECAR_HOST/SIDECAR_PORT, and pi-config's
        nullish coalescing selects the empty string over its defaults (an
        empty host binds an unspecified address; an empty port breaks the
        entrypoint startup probe) (Qodo r1 findings 3+4)."""
        for key in ("sidecar_host", "sidecar_port"):
            response = test_client.put(
                "/api/admin/settings",
                json={"settings": {key: ""}},
            )
            assert response.status_code == 400, key
            assert "cannot be empty" in response.json()["detail"]

    def test_rejected_batch_writes_nothing(self, test_client) -> None:
        """A PUT mixing a valid update with an invalid empty override is
        rejected whole — the valid key must NOT be written (no partial
        saves) (Qodo r1 finding 1)."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"jenkins_user": "bot", "sidecar_port": ""}},
        )
        assert response.status_code == 400
        # The valid key was not persisted: still falls through to env/default
        get_resp = test_client.get("/api/admin/settings")
        jenkins_user = next(s for s in get_resp.json() if s["key"] == "jenkins_user")
        assert jenkins_user["source"] != "db"

    def test_reset_non_string_setting_falls_to_default(self, test_client) -> None:
        """DELETE still resets non-string settings to env/default."""
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "30"}},
        )
        response = test_client.delete("/api/admin/settings/ai_call_timeout")
        assert response.status_code == 200
        get_resp = test_client.get("/api/admin/settings")
        ai_timeout = next(s for s in get_resp.json() if s["key"] == "ai_call_timeout")
        assert ai_timeout["source"] != "db"

    def test_active_server_providers_setting(self, test_client) -> None:
        """active_server_providers: metadata with options, PUT/GET round-trip."""
        from rootcoz.config import get_settings

        key = "active_server_providers"
        item = next(
            s for s in test_client.get("/api/admin/settings").json() if s["key"] == key
        )
        assert (item["category"], item["type"], item["default"]) == ("AI", "string", "")
        assert not item["sensitive"] and not item["restart_required"]
        # Options come from the sidecar catalog (test fixture: claude, gemini).
        assert item["options"] == ["claude", "gemini"]
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {key: "claude"}}
            ).status_code
            == 200
        )
        assert get_settings().active_server_providers == "claude"
        item = next(
            s for s in test_client.get("/api/admin/settings").json() if s["key"] == key
        )
        assert (item["value"], item["source"]) == ("claude", "db")
        # Empty value resets to the default (all providers active).
        assert (
            test_client.put(
                "/api/admin/settings", json={"settings": {key: ""}}
            ).status_code
            == 200
        )
        assert get_settings().active_server_providers == ""

    def test_put_settings_non_admin_forbidden(self, test_client) -> None:
        """Non-admin users cannot update settings."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "20"}},
            headers=self._NO_ADMIN_HEADERS,
        )
        assert response.status_code in (401, 403)

    def test_delete_settings_non_admin_forbidden(self, test_client) -> None:
        """Non-admin users cannot reset settings."""
        response = test_client.delete(
            "/api/admin/settings/jenkins_url",
            headers={"Authorization": ""},
        )
        assert response.status_code in (401, 403)


# #379: the choice contract every enumerated setting must publish, and the
# exact values the UI codes against.
_ENUM_SETTING_CHOICES: dict[str, dict[str, Any]] = {
    "default_user_role": {
        "options": ["viewer", "reviewer", "operator"],
        "options_source": None,
        "multiple": False,
        "allow_custom": False,
    },
    "jira_auth_mode": {
        "options": ["oauth", "basic", "pat", "atst"],
        "options_source": None,
        "multiple": False,
        "allow_custom": False,
    },
    "log_level": {
        "options": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "options_source": None,
        "multiple": False,
        "allow_custom": False,
    },
    "auto_push_exporters": {
        "options": None,
        "options_source": "exporters",
        "multiple": True,
        "allow_custom": False,
    },
    "ambient_auth_providers": {
        "options": None,
        "options_source": "providers",
        "multiple": True,
        "allow_custom": False,
    },
    "allowed_users": {
        "options": None,
        "options_source": "users",
        "multiple": True,
        "allow_custom": True,
    },
    # Datadog publishes a finite site list; custom stays on so a new region
    # works before the list ships an update.
    "datadog_site": {
        "options": [
            "datadoghq.com",
            "us3.datadoghq.com",
            "us5.datadoghq.com",
            "datadoghq.eu",
            "ap1.datadoghq.com",
            "ap2.datadoghq.com",
            "ddog-gov.com",
            "us3.ddog-gov.com",
        ],
        "options_source": None,
        "multiple": False,
        "allow_custom": True,
    },
}

_STATIC_ENUM_ACCEPTED_VALUES = [
    (key, value)
    for key, contract in _ENUM_SETTING_CHOICES.items()
    for value in (contract["options"] or [])
]


class TestEnumeratedSettings:
    """#379: enumerated settings publish their choices and enforce them."""

    @pytest.mark.parametrize(("key", "expected"), sorted(_ENUM_SETTING_CHOICES.items()))
    def test_metadata_describes_the_choices(
        self, test_client, key: str, expected: dict[str, Any]
    ) -> None:
        """GET /api/admin/settings carries the exact contract the UI codes to."""
        item = _settings_item(test_client, key)
        assert {field: item[field] for field in expected} == expected

    def test_every_setting_carries_the_choice_contract(self, test_client) -> None:
        """The four keys are always present, so the UI needs no special case."""
        for item in test_client.get("/api/admin/settings").json():
            assert {"options", "options_source", "multiple", "allow_custom"} <= set(
                item
            )

    @pytest.mark.parametrize(("key", "value"), _STATIC_ENUM_ACCEPTED_VALUES)
    def test_static_enum_accepts_every_allowed_value(
        self, test_client, key: str, value: str
    ) -> None:
        response = test_client.put(
            "/api/admin/settings", json={"settings": {key: value}}
        )
        assert response.status_code == 200, response.json()
        assert _settings_item(test_client, key)["value"] == value

    @pytest.mark.parametrize(
        ("key", "bad"),
        [
            ("default_user_role", "admin"),
            ("jira_auth_mode", "none"),
            ("log_level", "LOUD"),
            ("datadog_site", "https://not a site/"),
        ],
    )
    def test_static_enum_rejects_a_value_outside_the_set(
        self, test_client, key: str, bad: str
    ) -> None:
        """A bad value is a 400 naming every allowed one, and saves nothing."""
        before = _settings_item(test_client, key)
        response = test_client.put("/api/admin/settings", json={"settings": {key: bad}})
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert key.upper() in detail.upper()
        for option in _ENUM_SETTING_CHOICES[key]["options"]:
            assert option in detail
        assert _settings_item(test_client, key) == before

    @pytest.mark.parametrize(
        ("key", "typed", "stored"),
        [
            # Case-insensitive acceptance, canonical storage: the saved value is
            # always one the picker offers, and an existing LOG_LEVEL=info env
            # keeps working through the persist path.
            ("log_level", " info ", "INFO"),
            ("jira_auth_mode", "PAT", "pat"),
            # A pasted URL is stored as the bare hostname the exporter builds
            # its intake URL from, so one site has exactly one spelling.
            ("datadog_site", " https://datadoghq.eu/ ", "datadoghq.eu"),
            ("datadog_site", "api.datadoghq.com", "datadoghq.com"),
        ],
    )
    def test_enum_value_is_stored_in_canonical_form(
        self, test_client, key: str, typed: str, stored: str
    ) -> None:
        response = test_client.put(
            "/api/admin/settings", json={"settings": {key: typed}}
        )
        assert response.status_code == 200, response.json()
        assert _settings_item(test_client, key)["value"] == stored

    @pytest.mark.parametrize(
        ("key", "label"),
        [
            # Brand casing survives the humaniser: a label that reads
            # "Enable datadog" or "Dd push …" is a UI defect, not a style nit.
            ("enable_datadog", "Enable Datadog"),
            ("datadog_site", "Datadog site"),
            ("datadog_api_key", "Datadog API key"),
            ("dd_push_classifications", "DD push classifications"),
            ("dd_allow_unreviewed_push", "DD allow unreviewed push"),
            ("enable_reportportal", "Enable ReportPortal"),
            ("rp_push_rootcoz_url", "RP push RootCoz URL"),
        ],
    )
    def test_setting_labels_keep_brand_casing(
        self, test_client, key: str, label: str
    ) -> None:
        assert _settings_item(test_client, key)["label"] == label

    def test_auto_push_exporters_accepts_a_registry_plugin(self, test_client) -> None:
        """A known plugin saves in the registry's spelling (case-insensitive)."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"auto_push_exporters": " ReportPortal "}},
        )
        assert response.status_code == 200, response.json()
        assert _settings_item(test_client, "auto_push_exporters")["value"] == (
            "reportportal"
        )

    def test_auto_push_exporters_rejects_an_unknown_plugin(self, test_client) -> None:
        """A bogus plugin name is rejected — auto-push would silently skip it."""
        before = _settings_item(test_client, "auto_push_exporters")
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"auto_push_exporters": "reportportal,nope"}},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "nope" in detail and "reportportal" in detail
        assert _settings_item(test_client, "auto_push_exporters") == before

    def test_auto_push_exporters_fails_open_on_empty_registry(
        self, test_client, monkeypatch, caplog
    ) -> None:
        """A cold registry must not block an admin — accept, but warn."""

        from rootcoz import main

        monkeypatch.setattr(main, "_EXPORTER_CLASSES", {})
        with caplog.at_level(logging.WARNING, logger="rootcoz.main"):
            logging.getLogger("rootcoz.main").propagate = True
            try:
                response = test_client.put(
                    "/api/admin/settings",
                    json={"settings": {"auto_push_exporters": "not-loaded-yet"}},
                )
            finally:
                logging.getLogger("rootcoz.main").propagate = False
        assert response.status_code == 200, response.json()
        assert _settings_item(test_client, "auto_push_exporters")["value"] == (
            "not-loaded-yet"
        )
        assert any("registry is empty" in r.getMessage() for r in caplog.records)

    def test_allowed_users_accepts_unknown_usernames(self, test_client) -> None:
        """SSO users may not exist yet: unknown names save, normalised in order."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"allowed_users": "  alice ,bob,,  carol "}},
        )
        assert response.status_code == 200, response.json()
        assert _settings_item(test_client, "allowed_users")["value"] == (
            "alice, bob, carol"
        )

    def test_allowed_users_rejects_a_bad_shape(self, test_client) -> None:
        """Only the shape is checked — a name with a space is a typo, not a user."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"allowed_users": "alice smith"}},
        )
        assert response.status_code == 400
        assert "allowed_users" in response.json()["detail"]

    def test_import_rejects_an_ambient_provider_with_no_credential(
        self, test_client, monkeypatch
    ) -> None:
        """An imported file cannot declare an unauthenticated provider ambient."""
        from rootcoz import ai_client

        _no_key_providers(monkeypatch)
        _provider_auth_status(monkeypatch, {"claude": False})
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        ai_client.update_model_catalog([{"provider": "claude", "id": "claude/model"}])
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {"ambient_auth_providers": "claude"}},
        )
        assert response.status_code == 422, response.json()
        detail = response.json()["detail"]
        assert "no deployment credential" in detail and "claude" in detail
        # Nothing was written — the import is validated before any DB write
        assert _settings_item(test_client, "ambient_auth_providers")["source"] != "db"

    @pytest.mark.parametrize(
        ("key", "bad"),
        [
            ("default_user_role", "admin"),
            ("jira_auth_mode", "none"),
            ("log_level", "LOUD"),
            ("auto_push_exporters", "nope"),
            ("ambient_auth_providers", "nope-provider"),
            ("allowed_users", "alice smith"),
        ],
    )
    def test_import_rejects_an_invalid_enumerated_value(
        self, test_client, monkeypatch, key: str, bad: str
    ) -> None:
        """An imported file cannot bypass the save-path validation."""
        from rootcoz import ai_client

        _no_key_providers(monkeypatch)
        ai_client.update_model_catalog([{"provider": "claude", "id": "claude/model"}])
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {key: bad}},
        )
        assert response.status_code == 422
        assert key.upper() in response.json()["detail"].upper()
        # Nothing was written — the import is validated before any DB write
        assert _settings_item(test_client, key)["source"] != "db"


class TestAdminSettingsExportImport:
    """Tests for GET /api/admin/settings/export and POST /api/admin/settings/import."""

    _NO_ADMIN_HEADERS: ClassVar[dict[str, str]] = {"Authorization": ""}

    def test_export_contains_only_db_stored_settings(self, test_client) -> None:
        """Export holds DB overrides only — env/default values stay out."""
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"jenkins_url": "https://db.jenkins.com"}},
        )
        response = test_client.get("/api/admin/settings/export")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        doc = response.json()
        assert doc["version"] == 1
        assert "exported_at" in doc
        # jenkins_url has a DB override; ai_call_timeout does not (env/default)
        assert doc["settings"] == {"jenkins_url": "https://db.jenkins.com"}

    def test_export_excludes_sensitive_by_default(self, test_client) -> None:
        """Sensitive settings are omitted unless include_sensitive is set."""
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {"github_token": "gh-secret"}
            },  # pragma: allowlist secret
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert "github_token" not in doc["settings"]
        assert "warning" not in doc

        doc = test_client.get(
            "/api/admin/settings/export", params={"include_sensitive": "true"}
        ).json()
        assert doc["settings"]["github_token"] == "gh-secret"
        assert "secrets" in doc["warning"]

    def test_export_skips_internal_rows(self, test_client) -> None:
        """Internal bookkeeping rows (jira_verified_method) never export."""
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"jira_auth_mode": "basic"}},
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert "jira_verified_method" not in doc["settings"]
        assert doc["settings"]["jira_auth_mode"] == "basic"

    def test_export_non_admin_forbidden(self, test_client) -> None:
        response = test_client.get(
            "/api/admin/settings/export", headers=self._NO_ADMIN_HEADERS
        )
        assert response.status_code in (401, 403)

    def test_import_applies_and_reports_diff(self, test_client) -> None:
        """A valid document applies and returns per-setting diff."""
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "10", "log_level": "INFO"}},
        )
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {
                    "ai_call_timeout": "25",
                    "log_level": "INFO",
                    "public_base_url": "https://rootcoz.example.com",
                },
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["applied"] == {
            "ai_call_timeout": {"from": "10", "to": "25"},
            "public_base_url": {"from": None, "to": "https://rootcoz.example.com"},
        }
        assert data["unchanged"] == ["log_level"]
        # log_level changed nothing; public_base_url needs no restart
        assert data["restart_required"] == []
        # Values actually applied
        assert _settings_item(test_client, "ai_call_timeout")["value"] == "25"

    def test_import_reports_restart_required(self, test_client) -> None:
        """Changed restart-required settings are listed in the response."""
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {"log_level": "DEBUG", "vapid_claim_email": "a@b.c"},
            },
        )
        assert response.status_code == 200
        assert response.json()["restart_required"] == ["log_level", "vapid_claim_email"]

    def test_import_masks_sensitive_values_in_diff(self, test_client) -> None:
        """Sensitive values never appear in the diff — masked as ***."""
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {"github_token": "old-secret"}
            },  # pragma: allowlist secret
        )
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {"github_token": "new-secret"},
            },  # pragma: allowlist secret
        )
        assert response.status_code == 200
        assert response.json()["applied"]["github_token"] == {
            "from": "***",
            "to": "***",
        }
        # The value itself round-trips: re-export with sensitive included
        doc = test_client.get(
            "/api/admin/settings/export", params={"include_sensitive": "true"}
        ).json()
        assert doc["settings"]["github_token"] == "new-secret"

    def test_import_invalid_value_is_atomic(self, test_client) -> None:
        """One invalid entry rejects the whole import — nothing is applied."""
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "10"}},
        )
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {"ai_call_timeout": "30", "jenkins_timeout": "abc"},
            },
        )
        assert response.status_code == 422
        assert "jenkins_timeout" in response.json()["detail"]
        # The valid entry was NOT applied
        assert _settings_item(test_client, "ai_call_timeout")["value"] == "10"

    def test_import_unknown_key_rejected(self, test_client) -> None:
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {"nonexistent_key": "x"}},
        )
        assert response.status_code == 422
        assert "Unknown settings" in response.json()["detail"]

    def test_import_server_only_key_rejected(self, test_client) -> None:
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {"admin_key": "new-key"},
            },  # pragma: allowlist secret
        )
        assert response.status_code == 422
        assert "Server-only" in response.json()["detail"]

    def test_export_omits_derived_settings(self, test_client) -> None:
        """Derived settings are instance-computed, not user data — a DB row
        for one never ships in either export mode, so a transferred document
        cannot pin a stale sidecar_url on another instance.
        """
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"sidecar_url": "http://127.0.0.1:9100"}},
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert "sidecar_url" not in doc["settings"]
        doc = test_client.get(
            "/api/admin/settings/export", params={"include_sensitive": "true"}
        ).json()
        assert "sidecar_url" not in doc["settings"]

    def test_import_derived_key_rejected(self, test_client) -> None:
        """A stale export carrying sidecar_url must not pin the derived URL
        on this instance — 422 naming the key, nothing applied.
        """
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"ai_call_timeout": "10"}},
        )
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {
                    "sidecar_url": "http://127.0.0.1:9100",
                    "ai_call_timeout": "30",
                },
            },
        )
        assert response.status_code == 422
        assert "sidecar_url" in response.json()["detail"]
        # Atomic rejection — the valid entry was NOT applied either
        assert _settings_item(test_client, "ai_call_timeout")["value"] == "10"
        assert _settings_item(test_client, "sidecar_url")["source"] != "db"

    def test_import_rejects_wrong_version_and_empty(self, test_client) -> None:
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 2, "settings": {"ai_call_timeout": "30"}},
        )
        assert response.status_code == 422
        assert "version" in response.json()["detail"]
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {}},
        )
        assert response.status_code == 422

    def test_import_dry_run_validates_without_applying(self, test_client) -> None:
        """dry_run reports the diff but writes nothing."""
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "dry_run": True,
                "settings": {"ai_call_timeout": "40"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["applied"] == {"ai_call_timeout": {"from": None, "to": "40"}}
        # Nothing was written — no DB override exists
        assert _settings_item(test_client, "ai_call_timeout")["source"] != "db"

    def test_import_non_admin_forbidden(self, test_client) -> None:
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {"ai_call_timeout": "30"}},
            headers=self._NO_ADMIN_HEADERS,
        )
        assert response.status_code in (401, 403)

    def test_import_invalid_boolean_value_rejected(self, test_client) -> None:
        """A value its Settings field rejects never reaches the DB — once
        stored, get_settings() cannot validate the merged overrides and
        silently falls back to the base env settings (all-or-nothing loss).
        """
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {"secure_cookies": "not-a-bool"}},
        )
        assert response.status_code == 422
        assert "secure_cookies" in response.json()["detail"]
        assert _settings_item(test_client, "secure_cookies")["source"] != "db"

    def test_put_invalid_boolean_value_rejected(self, test_client) -> None:
        """The shared validator covers the PUT path too."""
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"secure_cookies": "not-a-bool"}},
        )
        assert response.status_code == 400
        assert "secure_cookies" in response.json()["detail"]

    def test_import_sidecar_host_stored_stripped(self, test_client) -> None:
        """The validated (stripped) host is what gets stored — padding would
        pass through the env bridge to pi-config's listen host on restart.
        """
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {"sidecar_host": " 127.0.0.1 "}},
        )
        assert response.status_code == 200
        assert _settings_item(test_client, "sidecar_host")["value"] == "127.0.0.1"

    @pytest.mark.parametrize("empty_value", [None, ""])
    def test_import_unknown_key_with_empty_value(
        self, test_client, empty_value
    ) -> None:
        """Unknown keys with empty values return the collected 422, not a
        KeyError-turned-500 from _empty_override_error.
        """
        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": {"bogus": empty_value}},
        )
        assert response.status_code == 422
        assert "Unknown settings" in response.json()["detail"]

    def test_export_sanitizes_embedded_credentials_by_default(
        self, test_client
    ) -> None:
        """Default exports strip repo tokens and URL userinfo — credentials
        leave only with the include_sensitive opt-in.
        """
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "additional_repos": (
                        "main:https://user:pass@github.com/org/repo@secret-token"  # pragma: allowlist secret
                    ),
                    "tests_repo_url": "https://user:pass@github.com/org/tests",  # pragma: allowlist secret
                }
            },
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert doc["settings"]["additional_repos"] == "main:https://github.com/org/repo"
        assert doc["settings"]["tests_repo_url"] == "https://github.com/org/tests"

        doc = test_client.get(
            "/api/admin/settings/export", params={"include_sensitive": "true"}
        ).json()
        assert (
            doc["settings"]["additional_repos"]
            == (
                "main:https://user:pass@github.com/org/repo@secret-token"  # pragma: allowlist secret
            )
        )
        assert doc["settings"]["tests_repo_url"] == (
            "https://user:pass@github.com/org/tests"  # pragma: allowlist secret
        )

    def test_import_diff_masks_embedded_credentials(self, test_client) -> None:
        """Credential substrings in non-sensitive values are masked in the
        import diff display.
        """
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {
                    "additional_repos": "main:https://github.com/org/repo@secret-token",  # pragma: allowlist secret
                    "tests_repo_url": "https://user:pass@github.com/org/tests",  # pragma: allowlist secret
                },
            },
        )
        assert response.status_code == 200
        applied = response.json()["applied"]
        assert (
            applied["additional_repos"]["to"] == "main:https://github.com/org/repo@***"
        )
        assert applied["tests_repo_url"]["to"] == "https://***@github.com/org/tests"
        assert "secret-token" not in response.text
        assert "user:pass" not in response.text

    def test_export_includes_empty_sensitive_override(self, test_client) -> None:
        """An explicit empty sensitive override (which deliberately beats the
        env value) round-trips through a sensitive export as "".
        """
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"github_token": ""}},
        )
        doc = test_client.get(
            "/api/admin/settings/export", params={"include_sensitive": "true"}
        ).json()
        assert doc["settings"]["github_token"] == ""

    def test_export_skips_malformed_additional_repos(self, test_client) -> None:
        """An unparseable additional_repos value never ships in a default
        export — its credentials could sit anywhere a parser cannot find
        (#361 Qodo r2: fail closed, skip).
        """
        raw = (
            "main:https://github.com/org/repo@secret-token,"  # pragma: allowlist secret
            "other:https://user:pass@github.com/org/other,"  # trailing comma: unparseable  # pragma: allowlist secret
        )
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"additional_repos": raw}},
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert "additional_repos" not in doc["settings"]
        assert "secret-token" not in json.dumps(doc)
        assert "user:pass" not in json.dumps(doc)
        # The secrets opt-in still exports the raw value verbatim
        doc = test_client.get(
            "/api/admin/settings/export", params={"include_sensitive": "true"}
        ).json()
        assert doc["settings"]["additional_repos"] == raw

    def test_import_diff_masks_malformed_additional_repos(self, test_client) -> None:
        """Malformed additional_repos values are masked whole in the import
        diff — on both the from and to side (#361 Qodo r2).
        """
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "additional_repos": (
                        "main:https://user:pass@github.com/org/repo,"  # trailing comma  # pragma: allowlist secret
                    )
                }
            },
        )
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {
                    "additional_repos": (
                        "other:https://github.com/org/other@secret-token"  # trailing comma makes it malformed too  # pragma: allowlist secret
                        ","
                    )
                },
            },
        )
        assert response.status_code == 200
        applied = response.json()["applied"]
        assert applied["additional_repos"] == {"from": "***", "to": "***"}
        assert "secret-token" not in response.text
        assert "user:pass" not in response.text

    def test_export_strips_git_url_userinfo(self, test_client) -> None:
        """git:// is a supported clone scheme — its URL userinfo is a
        credential and must not leave a default export (#361 Qodo r2).
        """
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "tests_repo_url": "git://user:pass@host/repo"  # pragma: allowlist secret
                }
            },
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert doc["settings"]["tests_repo_url"] == "git://host/repo"

        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {
                    "tests_repo_url": "git://user:pass@other/repo"  # pragma: allowlist secret
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["applied"]["tests_repo_url"]["to"] == (
            "git://***@other/repo"
        )
        assert "user:pass" not in response.text

    def test_export_survives_malformed_stored_url(self, test_client) -> None:
        """A stored URL urlparse rejects must not raise from export — the
        sanitizer fails safe and still drops the userinfo (#361 Qodo r2).
        """
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "tests_repo_url": "https://user:pass@[broken"  # pragma: allowlist secret
                }
            },
        )
        response = test_client.get("/api/admin/settings/export")
        assert response.status_code == 200
        doc = response.json()
        assert doc["settings"]["tests_repo_url"] == "https://[broken"
        assert "user:pass" not in response.text

        diff = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {"tests_repo_url": "https://other/repo"},
            },
        ).json()
        assert diff["applied"]["tests_repo_url"]["from"] == "https://***@[broken"
        assert "user:pass" not in json.dumps(diff)

    def test_import_default_export_preserves_tokenized_repos(self, test_client) -> None:
        """Importing a default (sanitized) export must not wipe target repo
        tokens — a key whose imported value matches the sanitized form of
        the current value is skipped and reported as unchanged (#361 r2).
        """
        tokenized = (
            "main:https://github.com/org/repo@secret-token"  # pragma: allowlist secret
        )
        test_client.put(
            "/api/admin/settings",
            json={"settings": {"additional_repos": tokenized}},
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert doc["settings"]["additional_repos"] == "main:https://github.com/org/repo"

        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": doc["settings"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "additional_repos" in data["unchanged"]
        assert "additional_repos" not in data["applied"]
        # The token survived — the target's credential-bearing value wins
        assert _settings_item(test_client, "additional_repos")["value"] == (tokenized)

    def test_export_strips_url_query_tokens_by_default(self, test_client) -> None:
        """A query or fragment can carry a token (``?token=SECRET``) —
        default exports strip it from URL settings and additional_repos
        entries (fail closed); the base URL stays importable and the
        secrets opt-in keeps the value verbatim.
        """
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "tests_repo_url": "https://git.example/repo?token=SECRET#frag",  # pragma: allowlist secret
                    "additional_repos": "infra:https://git.example/infra?token=SECRET",  # pragma: allowlist secret
                }
            },
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert doc["settings"]["tests_repo_url"] == "https://git.example/repo"
        assert doc["settings"]["additional_repos"] == "infra:https://git.example/infra"
        assert "SECRET" not in json.dumps(doc)

        doc = test_client.get(
            "/api/admin/settings/export", params={"include_sensitive": "true"}
        ).json()
        assert doc["settings"]["tests_repo_url"] == (
            "https://git.example/repo?token=SECRET#frag"  # pragma: allowlist secret
        )
        assert doc["settings"]["additional_repos"] == (
            "infra:https://git.example/infra?token=SECRET"  # pragma: allowlist secret
        )

    def test_import_diff_masks_url_query_tokens(self, test_client) -> None:
        """Query/fragment tokens are masked in the import diff display."""
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {
                    "tests_repo_url": "https://git.example/repo?token=SECRET",  # pragma: allowlist secret
                    "additional_repos": "infra:https://git.example/infra?token=SECRET",  # pragma: allowlist secret
                },
            },
        )
        assert response.status_code == 200
        applied = response.json()["applied"]
        assert applied["tests_repo_url"]["to"] == "https://git.example/repo?***"
        assert (
            applied["additional_repos"]["to"] == "infra:https://git.example/infra?***"
        )
        assert "SECRET" not in response.text

    def test_import_default_export_preserves_query_tokenized_urls(
        self, test_client
    ) -> None:
        """Importing a default (sanitized) export must not wipe target query
        tokens — the skip compares against _sanitize_export_value, which now
        strips queries, so the credential-bearing target value wins.
        """
        tokenized_url = (
            "https://git.example/repo?token=SECRET"  # pragma: allowlist secret
        )
        tokenized_repos = (
            "infra:https://git.example/infra?token=SECRET"  # pragma: allowlist secret
        )
        test_client.put(
            "/api/admin/settings",
            json={
                "settings": {
                    "tests_repo_url": tokenized_url,
                    "additional_repos": tokenized_repos,
                }
            },
        )
        doc = test_client.get("/api/admin/settings/export").json()
        assert doc["settings"]["tests_repo_url"] == "https://git.example/repo"
        assert doc["settings"]["additional_repos"] == "infra:https://git.example/infra"

        response = test_client.post(
            "/api/admin/settings/import",
            json={"version": 1, "settings": doc["settings"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "tests_repo_url" in data["unchanged"]
        assert "additional_repos" in data["unchanged"]
        assert "tests_repo_url" not in data["applied"]
        assert "additional_repos" not in data["applied"]
        # The query tokens survived — the target's credential-bearing values win
        assert _settings_item(test_client, "tests_repo_url")["value"] == tokenized_url
        assert (
            _settings_item(test_client, "additional_repos")["value"] == tokenized_repos
        )

    def test_put_bad_integer_reports_single_error(self, test_client) -> None:
        """Each integer violation produces exactly one message — the manual
        pre-checks duplicated pydantic's (#361 Qodo r2).
        """
        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"peer_analysis_max_rounds": "0"}},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail.count("peer_analysis_max_rounds") == 1
        assert "must be >= 1" in detail

        response = test_client.put(
            "/api/admin/settings",
            json={"settings": {"jenkins_timeout": "abc"}},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail.count("jenkins_timeout") == 1
        assert "must be an integer" in detail

    def test_import_bad_integer_reports_single_error(self, test_client) -> None:
        """Same dedupe guarantee on the import (422) path."""
        response = test_client.post(
            "/api/admin/settings/import",
            json={
                "version": 1,
                "settings": {"peer_analysis_max_rounds": "0"},
            },
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail.count("peer_analysis_max_rounds") == 1
        assert "must be >= 1" in detail


class TestIntegrationConnectionEndpoints:
    """Tests for POST /api/admin/settings/test/{integration}."""

    _NO_ADMIN_HEADERS: ClassVar[dict[str, str]] = {"Authorization": ""}

    @staticmethod
    def _mock_async_client(get_responses):
        """Patch rootcoz.main.httpx.AsyncClient with canned GET responses.

        Returns ``(patcher, mock_client_cls)`` — the instance is
        ``mock_client_cls.return_value.__aenter__.return_value``.
        """
        mock_instance = AsyncMock()
        if isinstance(get_responses, list):
            mock_instance.get.side_effect = get_responses
        else:
            mock_instance.get.return_value = get_responses
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        return (
            patch("rootcoz.main.httpx.AsyncClient", mock_client_cls),
            mock_client_cls,
        )

    @staticmethod
    def _http_response(status: int, payload: Any, url: str) -> httpx.Response:
        """Build a real httpx.Response (raise_for_status/json need a request)."""
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    def test_non_admin_forbidden(self, test_client) -> None:
        """Non-admin users cannot run connection tests."""
        response = test_client.post(
            "/api/admin/settings/test/jenkins",
            headers=self._NO_ADMIN_HEADERS,
        )
        assert response.status_code in (401, 403)

    def test_unknown_integration_400(self, test_client) -> None:
        """Unknown integration returns 400 with the exact contract detail."""
        response = test_client.post("/api/admin/settings/test/nonsense")
        assert response.status_code == 400
        assert response.json() == {"detail": "Unknown integration: nonsense"}

    def test_jenkins_anonymous_access_fails(self, test_client) -> None:
        """Anonymous-enabled Jenkins must not validate missing credentials."""
        # Real /whoAmI/api/json shape on RedHat SSO Jenkins: authenticated is
        # true even for anonymous requests, so it must not be trusted.
        with patch("rootcoz.main.JenkinsClient") as mock_client_cls:
            mock_client_cls.return_value.get_whoami_security.return_value = {
                "_class": "hudson.security.WhoAmI",
                "anonymous": True,
                "authenticated": True,
                "name": "anonymous",
                "authorities": ["anonymous"],
            }
            response = test_client.post("/api/admin/settings/test/jenkins")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "anonymous" in data["message"]
        assert data["details"]["authenticated_user"] == "anonymous"

    def test_jenkins_anonymous_flag_absent_fails(self, test_client) -> None:
        """A whoAmI response without an anonymous flag fails conservatively."""
        with patch("rootcoz.main.JenkinsClient") as mock_client_cls:
            mock_client_cls.return_value.get_whoami_security.return_value = {
                "name": "myakove",
            }
            response = test_client.post("/api/admin/settings/test/jenkins")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False

    def test_jenkins_success(self, test_client) -> None:
        """Jenkins test pings whoAmI with the effective settings."""
        # Real /whoAmI/api/json shape for a valid authenticated request.
        with patch("rootcoz.main.JenkinsClient") as mock_client_cls:
            mock_client_cls.return_value.get_whoami_security.return_value = {
                "_class": "hudson.security.WhoAmI",
                "anonymous": False,
                "authenticated": True,
                "name": "testuser",
                "authorities": ["authenticated"],
            }
            response = test_client.post("/api/admin/settings/test/jenkins")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"]["authenticated_user"] == "testuser"
        # Effective credentials come from the fixture env — compare against
        # the live Settings rather than a hardcoded password literal.
        mock_client_cls.assert_called_once_with(
            url="https://jenkins.example.com",
            username="testuser",
            password=Settings().jenkins_password,
            ssl_verify=True,
            timeout=10,
        )

    def test_jenkins_invalid_credentials_http_401(self, test_client) -> None:
        """A 401 from whoAmI yields an invalid-credentials failure, not a 500."""
        with patch("rootcoz.main.JenkinsClient") as mock_client_cls:
            mock_client_cls.return_value.get_whoami_security.side_effect = (
                jenkins.JenkinsException("authentication failed (HTTP 401)")
            )
            response = test_client.post("/api/admin/settings/test/jenkins")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "credentials are invalid" in data["message"]
        assert "HTTP 401" in data["message"]

    def test_jenkins_failure_returns_result_not_500(self, test_client) -> None:
        """A failing Jenkins connection is a test result, never a 500."""
        with patch("rootcoz.main.JenkinsClient") as mock_client_cls:
            mock_client_cls.return_value.get_whoami_security.side_effect = (
                jenkins.JenkinsException("connection refused")
            )
            response = test_client.post("/api/admin/settings/test/jenkins")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "Jenkins connection failed" in data["message"]

    def test_jenkins_not_configured(self, test_client) -> None:
        """Missing Jenkins URL reports not configured instead of erroring."""
        with patch("rootcoz.main.get_settings", return_value=Settings(jenkins_url="")):
            response = test_client.post("/api/admin/settings/test/jenkins")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not configured" in data["message"]

    def test_jira_success_reports_auth_mode(self, test_client) -> None:
        """Jira test fetches /myself and reports the exercised auth mode."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
        )
        response_payload = self._http_response(
            200,
            {"displayName": "Test User"},
            "https://jira.example.com/rest/api/3/myself",
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(response_payload)[0] as _,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"] == {"auth_mode": "basic", "user": "Test User"}

    def _jira_myself_response(self) -> httpx.Response:
        """A successful /myself payload against a generic Jira URL."""
        return self._http_response(
            200,
            {"displayName": "Test User"},
            "https://jira.example.com/rest/api/3/myself",
        )

    def test_jira_sa_probes_project_and_records_oauth(self, test_client) -> None:
        """oauth mode probes project visibility (not /myself), records oauth."""
        import rootcoz.jira as jira_module

        jira_module._sa_token_cache.clear()
        settings = Settings(
            atlassian_oauth_cloud_id="cloud-id-123",
            jira_site_url="https://myorg.atlassian.net",
            jira_auth_mode="oauth",
            jira_oauth_client_secret=SecretStr("ATOA-not-a-real-secret"),
            atlassian_oauth_client_id="test-client-id",
        )
        mint_resp = httpx.Response(
            200,
            json={"access_token": "minted-token", "expires_in": 3600},
            request=httpx.Request("POST", "https://auth.atlassian.com/oauth/token"),
        )
        project_resp = self._http_response(
            200,
            [{"key": "PROJ"}],
            "https://api.atlassian.com/ex/jira/cloud-id-123/rest/api/3/project",
        )
        patcher, mock_client_cls = self._mock_async_client(project_resp)
        # Minting (rootcoz.jira) and the probe (rootcoz.main) share the
        # global httpx.AsyncClient — one mock serves both calls.
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=mint_resp)
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            patcher,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"] == {"auth_mode": "oauth", "user": "service account"}
        # The probe is project visibility with maxResults=1 — never /myself
        # (SA credentials only carry read:jira-work).
        requested_url = instance.get.call_args.args[0]
        assert requested_url.endswith("/rest/api/3/project")
        assert instance.get.call_args.kwargs["params"] == {"maxResults": 1}
        # Authorization carries the MINTED token, never the client secret
        # (passed on the AsyncClient constructor, not per-request)
        client_headers = mock_client_cls.call_args.kwargs["headers"]
        assert client_headers["Authorization"] == "Bearer minted-token"
        assert "ATOA-not-a-real-secret" not in json.dumps(data)
        # Success records the verified-method marker for oauth
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == "oauth"
        jira_module._sa_token_cache.clear()

    def test_jira_sa_mint_401_reports_clear_error(self, test_client) -> None:
        """A 401 from the token endpoint fails the test with a clear message."""
        import rootcoz.jira as jira_module

        jira_module._sa_token_cache.clear()
        settings = Settings(
            atlassian_oauth_cloud_id="cloud-id-123",
            jira_site_url="https://myorg.atlassian.net",
            jira_auth_mode="oauth",
            jira_oauth_client_secret=SecretStr("ATOA-not-a-real-secret"),
            atlassian_oauth_client_id="test-client-id",
        )
        mint_resp = httpx.Response(
            401,
            json={"error": "invalid_client"},
            request=httpx.Request("POST", "https://auth.atlassian.com/oauth/token"),
        )
        patcher, mock_client_cls = self._mock_async_client(self._jira_myself_response())
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=mint_resp)
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            patcher,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "could not mint" in data["message"]
        assert "HTTP 401" in data["message"]
        assert "check client ID and secret" in data["message"]
        assert "ATOA-not-a-real-secret" not in json.dumps(data)
        # A failed mint records no verified method
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == ""
        jira_module._sa_token_cache.clear()

    def test_jira_oauth_missing_client_id_reports_missing_field(
        self, test_client
    ) -> None:
        """oauth without a client ID names the exact missing field."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="oauth",
            jira_oauth_client_secret=SecretStr("ATOA-not-a-real-secret"),
        )
        with patch("rootcoz.main.get_settings", return_value=settings):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["message"] == (
            "Jira auth method 'oauth' is selected but missing: "
            "atlassian_oauth_client_id, atlassian_oauth_cloud_id"
        )

    def test_jira_success_persists_verified_method_selected(self, test_client) -> None:
        """A successful test persists the selected method as verified."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
        )
        # Never tested successfully → empty in metadata
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == ""
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(self._jira_myself_response())[0] as _,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == "basic"

    def test_jira_success_persists_verified_method_legacy_resolved(
        self, test_client
    ) -> None:
        """Unset mode never reaches Jira and never records a verified method."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_pat="not-a-real-pat",
        )
        with patch("rootcoz.main.get_settings", return_value=settings):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        assert response.json()["ok"] is False
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == ""

    def test_jira_failure_keeps_previous_verified_method(self, test_client) -> None:
        """A failed re-test does not clear the last successful record."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(self._jira_myself_response())[0] as _,
        ):
            assert test_client.post("/api/admin/settings/test/jira").json()["ok"]
        unauthorized = self._http_response(
            401,
            {"message": "Unauthorized"},
            "https://jira.example.com/rest/api/3/myself",
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(unauthorized)[0] as _,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        assert response.json()["ok"] is False
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == "basic"

    def test_jira_failure_no_secrets_in_response(self, test_client) -> None:
        """A 401 from Jira yields ok:false without leaking the token."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("super-secret-token"),
        )
        response_payload = self._http_response(
            401,
            {"message": "Unauthorized"},
            "https://jira.example.com/rest/api/3/myself",
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(response_payload)[0] as _,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "authentication failed" in data["message"]
        assert "super-secret-token" not in json.dumps(data)

    def test_jira_malformed_200_fails_and_records_nothing(self, test_client) -> None:
        """HTTP 200 with a non-object body fails the test, no verified write."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
        )
        malformed = self._http_response(
            200,
            ["not", "an", "object"],
            "https://jira.example.com/rest/api/3/myself",
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(malformed)[0] as _,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "unexpected" in data["message"]
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == ""

    def test_jira_marker_write_failure_fails_test(self, test_client) -> None:
        """A failed verified-marker write reports ok:false, not a success."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(self._jira_myself_response())[0] as _,
            patch.object(
                storage,
                "set_server_setting",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "could not be recorded" in data["message"]

    async def test_jira_repeated_success_writes_marker_once(self, test_client) -> None:
        """Repeated successful tests don't rewrite the unchanged marker."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(self._jira_myself_response())[0] as _,
        ):
            assert test_client.post("/api/admin/settings/test/jira").json()["ok"]
            assert test_client.post("/api/admin/settings/test/jira").json()["ok"]
        rows = await storage.get_server_settings_history(key="jira_verified_method")
        assert len(rows) == 1

    def test_jira_settings_changed_midflight_skips_verified_write(
        self, test_client
    ) -> None:
        """A stale in-flight test must not re-write verification after a creds change.

        The test snapshots the settings before the network call; if an
        admin updates the Jira credentials while it is in flight (which
        deletes the verification record), the completing old test must
        skip the write instead of verifying the untested configuration.
        """
        snapshot = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("old-token"),
        )
        changed = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="other@example.com",
            jira_api_token=SecretStr("new-token"),
        )
        # The admin update lands exactly when the network response
        # arrives: every get_settings() call before that returns the
        # tested snapshot, every call after returns the updated settings.
        state = {"updated": False}

        def _get_response(*_args: Any, **_kwargs: Any) -> httpx.Response:
            state["updated"] = True
            return self._jira_myself_response()

        def _settings() -> Settings:
            return changed if state["updated"] else snapshot

        # The canned-response helper only accepts lists; wire the callable
        # side_effect (which flips the settings on delivery) manually.
        patcher, mock_client_cls = self._mock_async_client(self._jira_myself_response())
        mock_client_cls.return_value.__aenter__.return_value.get.side_effect = (
            _get_response
        )
        with (
            patcher,
            patch("rootcoz.main.get_settings", side_effect=_settings),
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"]["auth_mode"] == "basic"
        assert "changed during the test" in data["details"]["note"]
        # No verification record for the untested new credentials.
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == ""

        # The same test with UNCHANGED settings still records the marker
        # (the fingerprint re-check must not break the normal path).
        with (
            patch("rootcoz.main.get_settings", return_value=snapshot),
            self._mock_async_client(self._jira_myself_response())[0] as _,
        ):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        item = _settings_item(test_client, "jira_auth_mode")
        assert item["jira_verified_method"] == "basic"

    def test_jira_credential_update_clears_verified_method(self, test_client) -> None:
        """Updating Jira credentials/mode invalidates the verified record."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(self._jira_myself_response())[0] as _,
        ):
            assert test_client.post("/api/admin/settings/test/jira").json()["ok"]
        assert _settings_item(test_client, "jira_auth_mode")[
            "jira_verified_method"
        ] == ("basic")

        # Unrelated updates keep the record
        test_client.put(
            "/api/admin/settings", json={"settings": {"ai_call_timeout": "20"}}
        )
        assert _settings_item(test_client, "jira_auth_mode")[
            "jira_verified_method"
        ] == ("basic")

        # A Jira credential update invalidates it
        test_client.put(
            "/api/admin/settings", json={"settings": {"jira_pat": "new-pat"}}
        )
        assert (
            _settings_item(test_client, "jira_auth_mode")["jira_verified_method"] == ""
        )

        # Re-verify, then an auth-mode update invalidates it too
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(self._jira_myself_response())[0] as _,
        ):
            assert test_client.post("/api/admin/settings/test/jira").json()["ok"]
        test_client.put(
            "/api/admin/settings", json={"settings": {"jira_auth_mode": "pat"}}
        )
        assert (
            _settings_item(test_client, "jira_auth_mode")["jira_verified_method"] == ""
        )

    def test_jira_selected_method_missing_fields(self, test_client) -> None:
        """A selected method with missing fields names the method and fields."""
        # jira_cloud_url="" pins the field empty — the deprecated JIRA_URL
        # env alias must not silently satisfy it.
        settings = Settings(
            jira_cloud_url="",
            jira_auth_mode="basic",
        )
        with patch("rootcoz.main.get_settings", return_value=settings):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["message"] == (
            "Jira auth method 'basic' is selected but missing: "
            "jira_email, jira_api_token, jira_cloud_url"
        )

    def test_jira_selected_method_partial_fields(self, test_client) -> None:
        """Only the actually-missing fields are named, not every field."""
        settings = Settings(
            jira_cloud_url="",
            jira_auth_mode="basic",
            jira_email="user@example.com",
        )
        with patch("rootcoz.main.get_settings", return_value=settings):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["message"] == (
            "Jira auth method 'basic' is selected but missing: "
            "jira_api_token, jira_cloud_url"
        )

    def test_jira_unset_mode_reports_method_required(self, test_client) -> None:
        """Unset mode (even with full credentials) reports the method-required error."""
        settings = Settings(
            jira_cloud_url="https://jira.example.com",
            jira_email="user@example.com",
            jira_api_token=SecretStr("not-a-real-token"),
            jira_pat="not-a-real-pat",
            jira_oauth_client_secret=SecretStr("ATOA-not-a-real-secret"),
        )
        assert not settings.jira_enabled
        with patch("rootcoz.main.get_settings", return_value=settings):
            response = test_client.post("/api/admin/settings/test/jira")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert (
            data["message"]
            == "No Jira auth method selected — choose one in Server Settings"
        )

    def test_github_success(self, test_client) -> None:
        """GitHub test hits api.github.com/user with the effective token."""
        settings = Settings(github_token=SecretStr("not-a-real-token"))
        response_payload = self._http_response(
            200, {"login": "octocat"}, "https://api.github.com/user"
        )
        mock_patch, mock_client_cls = self._mock_async_client(response_payload)
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            mock_patch,
        ):
            response = test_client.post("/api/admin/settings/test/github")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"] == {"login": "octocat"}
        assert (
            mock_client_cls.call_args.kwargs["headers"]["Authorization"]
            == "Bearer not-a-real-token"
        )

    def test_github_not_configured(self, test_client) -> None:
        """Missing GitHub token reports not configured."""
        response = test_client.post("/api/admin/settings/test/github")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not configured" in data["message"]

    def test_reportportal_success(self, test_client) -> None:
        """Report Portal test pings the project endpoint with the token."""
        settings = Settings(
            reportportal_url="https://rp.example.com",
            reportportal_api_token=SecretStr("not-a-real-token"),
            reportportal_project="my-project",
        )
        response_payload = self._http_response(
            200, {"id": 1}, "https://rp.example.com/api/v1/project/my-project"
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(response_payload)[0],
        ):
            response = test_client.post("/api/admin/settings/test/reportportal")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"] == {"project": "my-project"}

    def test_reportportal_failure(self, test_client) -> None:
        """A 403 from Report Portal yields ok:false, not a 500."""
        settings = Settings(
            reportportal_url="https://rp.example.com",
            reportportal_api_token=SecretStr("not-a-real-token"),
            reportportal_project="my-project",
        )
        response_payload = self._http_response(
            403,
            {"message": "Forbidden"},
            "https://rp.example.com/api/v1/project/my-project",
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(response_payload)[0],
        ):
            response = test_client.post("/api/admin/settings/test/reportportal")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "Report Portal connection failed" in data["message"]

    #: Application key shape the Datadog v2 intake requires alongside the API key.
    _DD_APP_KEY = "b" * 40  # pragma: allowlist secret

    def _dd_settings(self, **overrides) -> Settings:
        """Datadog settings with the full credential trio configured."""
        params = {
            "datadog_api_key": SecretStr("not-a-real-key"),
            "datadog_app_key": SecretStr(self._DD_APP_KEY),
            "datadog_site": "datadoghq.eu",
        }
        params.update(overrides)
        return Settings(**params)

    def test_datadog_success(self, test_client) -> None:
        """Datadog test validates the API key and reports the app key as present."""
        settings = self._dd_settings()
        validate_payload = self._http_response(
            200, {"valid": True}, "https://api.datadoghq.eu/api/v1/validate"
        )
        mock_patch, mock_client_cls = self._mock_async_client(validate_payload)
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            mock_patch,
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"] == {"site": "datadoghq.eu"}
        assert data["message"] == (
            "Datadog API key is valid (site datadoghq.eu); application key present"
            " — Datadog validates its event-write permission when the first event"
            " is published"
        )
        assert "not-a-real-key" not in json.dumps(data)
        assert self._DD_APP_KEY not in json.dumps(data)
        instance = mock_client_cls.return_value.__aenter__.return_value
        # Only the documented API-key probe: no side-effect-free probe exists
        # for the application key's event-write permission.
        assert [call.args[0] for call in instance.get.call_args_list] == [
            "https://api.datadoghq.eu/api/v1/validate",
        ]
        headers = mock_client_cls.call_args.kwargs["headers"]
        assert headers["DD-API-KEY"] == "not-a-real-key"
        assert headers["DD-APPLICATION-KEY"] == self._DD_APP_KEY

    def test_datadog_normalizes_site_before_validating(self, test_client) -> None:
        """A site configured as a full URL is normalized to a bare hostname."""
        settings = self._dd_settings(datadog_site="https://api.datadoghq.com/")
        mock_patch, mock_client_cls = self._mock_async_client(
            self._http_response(
                200, {"valid": True}, "https://api.datadoghq.com/api/v1/validate"
            )
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            mock_patch,
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["details"] == {"site": "datadoghq.com"}
        instance = mock_client_cls.return_value.__aenter__.return_value
        assert [call.args[0] for call in instance.get.call_args_list] == [
            "https://api.datadoghq.com/api/v1/validate",
        ]

    def test_datadog_invalid_key_http_401(self, test_client) -> None:
        """A 401 from the validate endpoint yields a rejected-key failure."""
        settings = self._dd_settings()
        response_payload = self._http_response(
            401,
            {"errors": ["Invalid API key"]},
            "https://api.datadoghq.eu/api/v1/validate",
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(response_payload)[0],
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "API key rejected" in data["message"]

    def test_datadog_not_configured(self, test_client) -> None:
        """Missing API key reports not configured instead of erroring."""
        with patch(
            "rootcoz.main.get_settings",
            return_value=Settings(datadog_api_key=None),
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not configured" in data["message"]
        assert "datadog_api_key" in data["message"]

    def test_datadog_missing_app_key_is_not_configured(self, test_client) -> None:
        """The v2 intake needs the application key too — name it when missing."""
        with patch(
            "rootcoz.main.get_settings",
            return_value=self._dd_settings(datadog_app_key=None),
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not configured" in data["message"]
        assert "datadog_app_key" in data["message"]
        assert "datadog_api_key" not in data["message"]

    def test_datadog_empty_key_is_not_configured(self, test_client) -> None:
        """An empty secret value counts as missing, not as a rejected key."""
        with patch(
            "rootcoz.main.get_settings",
            return_value=Settings(
                datadog_api_key=SecretStr(""),
                datadog_app_key=SecretStr(self._DD_APP_KEY),
                datadog_site="datadoghq.eu",
            ),
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not configured" in data["message"]
        assert "datadog_api_key" in data["message"]

    def test_datadog_invalid_site_rejected_before_request(self, test_client) -> None:
        """A nonempty but invalid site fails fast — no https://api./ request."""
        settings = self._dd_settings(datadog_site="not a site!!")
        mock_instance = AsyncMock()
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            patch("rootcoz.main.httpx.AsyncClient", mock_client_cls),
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "Invalid Datadog site" in data["message"]
        mock_instance.get.assert_not_called()

    def test_datadog_non_dict_json_body_tolerated(self, test_client) -> None:
        """A 200 with a non-object JSON body is a rejected key, not a crash."""
        settings = self._dd_settings()
        response_payload = self._http_response(
            200,
            ["unexpected"],
            "https://api.datadoghq.eu/api/v1/validate",
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(response_payload)[0],
        ):
            response = test_client.post("/api/admin/settings/test/datadog")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "API key rejected" in data["message"]

    def test_crash_log_and_message_have_no_credentials(
        self, test_client, caplog
    ) -> None:
        """The catch-all log keeps the redacted message, never a raw traceback."""

        secret_url = (
            "https://user:secretpass@jenkins.example.com/"  # pragma: allowlist secret
        )

        async def _crash(settings: Settings) -> dict[str, Any]:
            raise RuntimeError(f"boom at {secret_url}")

        with (
            patch.dict("rootcoz.main._INTEGRATION_TESTERS", {"jenkins": _crash}),
            # python-simple-logger disables propagation; re-enable so caplog sees records
            caplog.at_level(logging.WARNING, logger="rootcoz.main"),
        ):
            logging.getLogger("rootcoz.main").propagate = True
            try:
                response = test_client.post("/api/admin/settings/test/jenkins")
            finally:
                logging.getLogger("rootcoz.main").propagate = False
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "secretpass" not in json.dumps(data)
        crash_logs = [r for r in caplog.records if "crashed" in r.getMessage()]
        assert crash_logs, "expected the catch-all crash warning in logs"
        for record in crash_logs:
            assert "secretpass" not in record.getMessage()
            assert record.exc_info is None

    def test_overall_deadline_bounds_hanging_tester(self, test_client) -> None:
        """A hanging tester returns a failed result, bounded by the route deadline."""

        async def _hang(settings: Settings) -> dict[str, Any]:
            await asyncio.sleep(60)
            return {"ok": True, "message": "never reached"}

        with (
            patch.dict("rootcoz.main._INTEGRATION_TESTERS", {"jenkins": _hang}),
            patch("rootcoz.main._INTEGRATION_TEST_TIMEOUT", 0.05),
        ):
            response = test_client.post("/api/admin/settings/test/jenkins")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "timed out" in data["message"]

    def test_http_error_message_has_no_url_credentials(self, test_client) -> None:
        """A configured URL with userinfo/query never leaks into failure messages."""
        secret_url = "https://user:pass@rp.example.com/?token=topsecret"  # pragma: allowlist secret
        settings = Settings(
            reportportal_url=secret_url,
            reportportal_api_token=SecretStr("not-a-real-token"),
            reportportal_project="my-project",
        )
        response_payload = self._http_response(
            500, {"message": "boom"}, f"{secret_url}/api/v1/project/my-project"
        )
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(response_payload)[0],
        ):
            response = test_client.post("/api/admin/settings/test/reportportal")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        body = json.dumps(data)
        assert "user:pass" not in body
        assert "topsecret" not in body
        assert "token=" not in body
        assert "rp.example.com" in data["message"]

    def test_transport_error_message_has_no_url_credentials(self, test_client) -> None:
        """Transport-failure messages never echo userinfo or query strings."""
        secret_url = "https://user:pass@rp.example.com/?token=topsecret"  # pragma: allowlist secret
        settings = Settings(
            reportportal_url=secret_url,
            reportportal_api_token=SecretStr("not-a-real-token"),
            reportportal_project="my-project",
        )
        transport_error = httpx.ConnectError(
            f"connection refused to {secret_url}/api/v1/project/my-project"
        )
        mock_patch, _ = self._mock_async_client([transport_error])
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            mock_patch,
        ):
            response = test_client.post("/api/admin/settings/test/reportportal")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        body = json.dumps(data)
        assert "user:pass" not in body
        assert "topsecret" not in body
        assert "token=" not in body

    def test_prow_success_checks_deck_and_gcs(self, test_client) -> None:
        """Prow test verifies deck reachability and GCS bucket listability."""
        settings = Settings(
            prow_url="https://prow.example.com",
            gcs_bucket="test-bucket",
        )
        responses = [
            self._http_response(200, {}, "https://prow.example.com/api/jobs"),
            self._http_response(
                200,
                {"items": []},
                "https://storage.googleapis.com/storage/v1/b/test-bucket/o",
            ),
        ]
        mock_patch, mock_client_cls = self._mock_async_client(responses)
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            mock_patch,
        ):
            response = test_client.post("/api/admin/settings/test/prow")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "listable" in data["message"]
        assert data["details"]["prow_http_status"] == 200
        mock_instance = mock_client_cls.return_value.__aenter__.return_value
        gcs_call = mock_instance.get.call_args_list[1]
        assert gcs_call.kwargs["params"] == {"maxResults": 1}

    def test_prow_gcs_failure(self, test_client) -> None:
        """An unlistable GCS bucket fails the Prow test."""
        settings = Settings(
            prow_url="https://prow.example.com",
            gcs_bucket="private-bucket",
        )
        responses = [
            self._http_response(200, {}, "https://prow.example.com/api/jobs"),
            self._http_response(
                403,
                {"error": "Forbidden"},
                "https://storage.googleapis.com/storage/v1/b/private-bucket/o",
            ),
        ]
        with (
            patch("rootcoz.main.get_settings", return_value=settings),
            self._mock_async_client(responses)[0],
        ):
            response = test_client.post("/api/admin/settings/test/prow")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not listable" in data["message"]


class TestSubmitterAutoTag:
    """Tests for auto-tagging analyses with the submitter username."""

    def test_ensure_submitter_tag_adds_username(self) -> None:
        """_ensure_submitter_tag appends the lowercased username."""
        from rootcoz.main import _ensure_submitter_tag

        assert _ensure_submitter_tag(None, "Alice") == ["alice"]
        assert _ensure_submitter_tag([], "Bob") == ["bob"]
        assert _ensure_submitter_tag(["nightly"], "Carol") == ["nightly", "carol"]

    def test_ensure_submitter_tag_no_duplicate(self) -> None:
        """_ensure_submitter_tag does not duplicate an existing username tag."""
        from rootcoz.main import _ensure_submitter_tag

        assert _ensure_submitter_tag(["alice"], "Alice") == ["alice"]
        assert _ensure_submitter_tag(["nightly", "bob"], "bob") == ["nightly", "bob"]

    def test_ensure_submitter_tag_case_insensitive_dedup(self) -> None:
        """_ensure_submitter_tag leaves list unchanged when match exists."""
        from rootcoz.main import _ensure_submitter_tag

        assert _ensure_submitter_tag(["Admin"], "admin") == ["Admin"]
        assert _ensure_submitter_tag(["ALICE", "nightly"], "alice") == [
            "ALICE",
            "nightly",
        ]

    def test_ensure_submitter_tag_empty_username(self) -> None:
        """_ensure_submitter_tag skips blank usernames."""
        from rootcoz.main import _ensure_submitter_tag

        assert _ensure_submitter_tag(["nightly"], "") == ["nightly"]
        assert _ensure_submitter_tag(None, "  ") == []

    def test_ensure_submitter_tag_skips_system_tag_username(self) -> None:
        """_ensure_submitter_tag does not add usernames that match system tags."""
        from rootcoz.main import _ensure_submitter_tag

        assert _ensure_submitter_tag([], "re-analyze") == []
        assert _ensure_submitter_tag(["nightly"], "Re-Analyze") == ["nightly"]

    def test_strip_old_submitter_tag_preserves_system_tags(self) -> None:
        """_strip_old_submitter_tag skips stripping when old submitter matches a system tag."""
        from rootcoz.main import _strip_old_submitter_tag

        tags = ["re-analyze", "nightly"]
        result_data = {"request_params": {"submitted_by": "re-analyze"}}
        assert _strip_old_submitter_tag(tags, result_data) == ["re-analyze", "nightly"]

    def test_raw_analysis_auto_tags_submitter(self, test_client) -> None:
        """POST /analyze type=raw auto-adds the submitter username to tags."""
        data, _ = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_x",
                        "error_message": "fail",
                        "stack_trace": "trace",
                    }
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        job_id = data["job_id"]
        result = test_client.get(f"/results/{job_id}").json()["result"]
        assert "admin" in result["tags"]

    def test_raw_analysis_preserves_user_tags(self, test_client) -> None:
        """POST /analyze type=raw keeps user-supplied tags alongside the submitter."""
        data, _ = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_x",
                        "error_message": "fail",
                        "stack_trace": "trace",
                    }
                ],
                "tags": ["nightly", "regression"],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        job_id = data["job_id"]
        result = test_client.get(f"/results/{job_id}").json()["result"]
        assert "nightly" in result["tags"]
        assert "regression" in result["tags"]
        assert "admin" in result["tags"]

    def test_jenkins_analysis_auto_tags_submitter(self, test_client) -> None:
        """POST /analyze type=jenkins auto-adds the submitter username to tags."""
        with patch("rootcoz.main._process_ci_source_analysis"):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test-job",
                    "build_number": 1,
                    "ai_provider": "claude",
                    "ai_model": "test-model",
                },
            )
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            result = test_client.get(f"/results/{job_id}").json()["result"]
            assert "admin" in result["tags"]

    def test_jenkins_analysis_no_duplicate_submitter_tag(self, test_client) -> None:
        """POST /analyze type=jenkins doesn't duplicate when tags already has the username."""
        with patch("rootcoz.main._process_ci_source_analysis"):
            response = test_client.post(
                "/analyze",
                json={
                    "type": "jenkins",
                    "job_name": "test-job",
                    "build_number": 1,
                    "tags": ["Admin"],
                    "ai_provider": "claude",
                    "ai_model": "test-model",
                },
            )
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            result = test_client.get(f"/results/{job_id}").json()["result"]
            # "admin" tag already present (as "Admin" normalized to "admin")
            # — no duplicate should be added
            assert result["tags"].count("admin") == 1

    def test_submitter_tag_preserved_on_tag_update(self, test_client) -> None:
        """PUT /results/{job_id}/tags cannot remove the submitter username tag."""
        data, _ = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_x",
                        "error_message": "fail",
                        "stack_trace": "trace",
                    }
                ],
                "tags": ["nightly"],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        job_id = data["job_id"]
        # Try to remove all tags — submitter tag must be preserved
        response = test_client.put(
            f"/results/{job_id}/tags",
            json={"tags": ["custom-only"]},
        )
        assert response.status_code == 200
        updated_tags = response.json()["tags"]
        assert "admin" in updated_tags
        assert "custom-only" in updated_tags

    @pytest.mark.anyio
    async def test_all_system_tags_preserved_on_update(self, test_client) -> None:
        """PUT /results/{job_id}/tags preserves both re-analyze and submitter tags."""
        # Create a job first
        data, _ = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_x",
                        "error_message": "fail",
                        "stack_trace": "trace",
                    }
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        job_id = data["job_id"]

        # Inject re-analyze into stored tags (normally added by re-analyze flow)
        from rootcoz.storage import patch_result_json

        await patch_result_json(
            job_id, lambda d: d.update({"tags": ["admin", "re-analyze", "nightly"]})
        )

        # Try to replace all tags with only "new-tag"
        response = test_client.put(
            f"/results/{job_id}/tags",
            json={"tags": ["new-tag"]},
        )
        assert response.status_code == 200
        updated_tags = response.json()["tags"]
        assert "new-tag" in updated_tags
        assert "re-analyze" in updated_tags
        assert "admin" in updated_tags

    def test_user_cannot_add_system_tags_via_update(self, test_client) -> None:
        """PUT /results/{job_id}/tags filters out system tags from user input."""
        data, _ = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {
                        "test_name": "test_x",
                        "error_message": "fail",
                        "stack_trace": "trace",
                    }
                ],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        job_id = data["job_id"]
        # Try to add re-analyze via tag update
        response = test_client.put(
            f"/results/{job_id}/tags",
            json={"tags": ["re-analyze", "custom"]},
        )
        assert response.status_code == 200
        updated_tags = response.json()["tags"]
        assert "custom" in updated_tags
        assert "admin" in updated_tags  # submitter preserved
        assert "re-analyze" not in updated_tags  # system tag filtered out

    @pytest.mark.asyncio
    async def test_reanalyze_replaces_old_submitter_tag(self, test_client) -> None:
        """Re-analyze by user B replaces user A's submitter tag with B's."""
        from rootcoz import storage

        # Create a completed job originally submitted by "alice"
        result_data = {
            "summary": "1 failure",
            "job_name": "test-job",
            "build_number": 1,
            "failures": [],
            "tags": ["alice", "nightly"],
            "request_params": encrypt_sensitive_fields(
                {
                    "job_name": "test-job",
                    "build_number": 1,
                    "ai_provider": "claude",
                    "ai_model": "opus",
                    "jenkins_url": "https://jenkins.example.com",
                    "jenkins_user": "testuser",
                    "jenkins_password": "testpw",  # pragma: allowlist secret
                    "submitted_by": "alice",
                }
            ),
        }
        await storage.save_result(
            "job-alice", "http://jenkins/job/test-job/1/", "completed", result_data
        )
        # Re-analyze as "admin" (the test_client user)
        with patch("rootcoz.main._process_ci_source_analysis"):
            response = test_client.post("/re-analyze/job-alice", json={})
        assert response.status_code == 202
        new_job_id = response.json()["job_id"]
        new_result = test_client.get(f"/results/{new_job_id}").json()["result"]
        assert "admin" in new_result["tags"]  # new submitter
        assert "alice" not in new_result["tags"]  # old submitter removed
        assert "nightly" in new_result["tags"]  # non-submitter tag preserved
        assert "re-analyze" in new_result["tags"]  # system tag preserved


class TestSanitizeControlChars:
    """Tests for _sanitize_control_chars."""

    @pytest.mark.parametrize(
        "input_val, expected",
        [
            ("alice", "alice"),
            ("", ""),
            ("alice\nfake", "alicefake"),
            ("bob\r\nINFO", "bobINFO"),
            ("tab\there", "tabhere"),
            ("null\x00here", "nullhere"),
            ("del\x7fhere", "delhere"),
            ("\x01\x02test\x1f", "test"),
        ],
        ids=[
            "normal",
            "empty",
            "newline",
            "crlf",
            "tab",
            "null_byte",
            "del_char",
            "mixed_control",
        ],
    )
    def test_sanitize_control_chars(self, input_val: str, expected: str) -> None:
        from rootcoz.main import _sanitize_control_chars

        assert _sanitize_control_chars(input_val) == expected


class TestRefreshAiModelsEndpoint:
    """Tests for POST /api/admin/ai-models/refresh."""

    def test_refresh_ai_models_returns_updated_list(
        self, test_client, monkeypatch
    ) -> None:
        """Admin can refresh models and get updated list."""
        mock_models = [
            {"id": "claude-sonnet-4", "name": "Claude Sonnet 4"},
            {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        ]
        monkeypatch.setattr(
            "rootcoz.main.registered_provider_ids", AsyncMock(return_value=set())
        )
        with patch(
            "pi_sidecar_client.get_sidecar_client",
        ) as mock_get_client:
            mock_client = AsyncMock()
            mock_client.refresh_models.return_value = mock_models
            mock_get_client.return_value = mock_client
            response = test_client.post("/api/admin/ai-models/refresh")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert data["models"] == mock_models
        mock_client.refresh_models.assert_awaited_once()

    def test_refresh_ai_models_sidecar_failure_returns_502(self, test_client) -> None:
        """Returns 502 when sidecar refresh fails."""
        with patch(
            "pi_sidecar_client.get_sidecar_client",
            side_effect=RuntimeError("sidecar down"),
        ):
            response = test_client.post("/api/admin/ai-models/refresh")

        assert response.status_code == 502
        assert "Failed to refresh" in response.json()["detail"]

    def test_refresh_ai_models_sidecar_failure_returns_502_without_route_cache(
        self, test_client
    ) -> None:
        """Catalog refresh failure does not rely on a local provider route cache."""
        with patch(
            "pi_sidecar_client.get_sidecar_client",
        ) as mock_get_client:
            mock_client = AsyncMock()
            mock_client.refresh_models.side_effect = RuntimeError("sidecar down")
            mock_get_client.return_value = mock_client
            response = test_client.post("/api/admin/ai-models/refresh")

        assert response.status_code == 502


class TestCursorProviderStatusGating:
    """#374: Cursor status only when the catalog contains a Cursor provider."""

    @staticmethod
    def _request(*, is_admin: bool) -> SimpleNamespace:
        return SimpleNamespace(
            state=SimpleNamespace(
                username="alice",
                role="admin" if is_admin else "reviewer",
                is_admin=is_admin,
            )
        )

    @staticmethod
    def _patch_listing(monkeypatch, listing: dict[str, Any]) -> None:
        """Point ``model_listing_status`` at a context var holding *listing*."""
        from contextvars import ContextVar

        from rootcoz import main

        var: ContextVar[dict[str, Any]] = ContextVar("test_listing_status")
        var.set(listing)
        monkeypatch.setattr(main, "model_listing_status", var)

    @pytest.mark.parametrize("is_admin", [True, False])
    @pytest.mark.asyncio
    async def test_list_all_omits_cursor_without_cursor_provider(
        self, monkeypatch, is_admin: bool
    ) -> None:
        """A catalog without Cursor reports no cursor status and skips the probe."""
        from rootcoz import main

        probe = AsyncMock(return_value={"ok": False, "reason": "auth_expired"})
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        catalog = {"EnMaaS": [{"provider": "EnMaaS", "id": "m1"}]}
        monkeypatch.setattr(
            main, "scoped_models_for_request", AsyncMock(return_value=catalog)
        )
        listing = {"EnMaaS": {"modelListingSupported": True}}
        self._patch_listing(monkeypatch, listing)

        data = await main.list_ai_models(self._request(is_admin=is_admin), provider="")

        assert data["providers"] == catalog
        assert "cursor" not in data["provider_status"]
        assert data["provider_status"]["EnMaaS"]["modelListingSupported"] is True
        probe.assert_not_awaited()

    @pytest.mark.parametrize("cursor_provider", ["cursor", "acpx-cursor"])
    @pytest.mark.parametrize("is_admin", [True, False])
    @pytest.mark.asyncio
    async def test_list_all_reports_cursor_provider_with_zero_models(
        self, monkeypatch, is_admin: bool, cursor_provider: str
    ) -> None:
        """A present Cursor provider with no models still reports its status."""
        from rootcoz import main

        raw = {
            "ok": False,
            "reason": "auth_expired",
            "hint": "login expired",
            "has_api_key": False,
            "model_count": 0,
        }
        probe = AsyncMock(return_value=raw)
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        catalog = {
            cursor_provider: [],
            "EnMaaS": [{"provider": "EnMaaS", "id": "m1"}],
        }
        monkeypatch.setattr(
            main, "scoped_models_for_request", AsyncMock(return_value=catalog)
        )
        self._patch_listing(monkeypatch, {})

        data = await main.list_ai_models(self._request(is_admin=is_admin), provider="")

        status = data["provider_status"]["cursor"]
        if is_admin:
            probe.assert_awaited_once_with(model_count=0)
            assert status == raw
        else:
            probe.assert_not_awaited()
            assert status == {
                "ok": False,
                "reason": "unavailable",
                "hint": "Cursor is unavailable. Contact an administrator.",
                "model_count": 0,
            }

    @pytest.mark.parametrize("is_admin", [True, False])
    @pytest.mark.asyncio
    async def test_list_all_drops_listed_cursor_without_cursor_provider(
        self, monkeypatch, is_admin: bool
    ) -> None:
        """A user-credential cursor listing cannot invent a Cursor banner."""
        from rootcoz import main

        probe = AsyncMock(return_value={"ok": False, "reason": "auth_expired"})
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        catalog = {"EnMaaS": [{"provider": "EnMaaS", "id": "m1"}]}
        monkeypatch.setattr(
            main, "scoped_models_for_request", AsyncMock(return_value=catalog)
        )
        self._patch_listing(
            monkeypatch,
            {
                "cursor": {
                    "has_api_key": True,
                    "modelListingSupported": True,
                    "model_count": 0,
                },
                "EnMaaS": {"modelListingSupported": True},
            },
        )

        data = await main.list_ai_models(self._request(is_admin=is_admin), provider="")

        assert "cursor" not in data["provider_status"]
        assert data["provider_status"]["EnMaaS"]["modelListingSupported"] is True
        probe.assert_not_awaited()

    @pytest.mark.parametrize("cursor_provider", ["cursor", "acpx-cursor"])
    @pytest.mark.parametrize("is_admin", [True, False])
    @pytest.mark.asyncio
    async def test_list_all_reports_cursor_with_cursor_provider(
        self, monkeypatch, is_admin: bool, cursor_provider: str
    ) -> None:
        """Cursor-present shape stays: admin probe shape, non-admin coarse shape."""
        from rootcoz import main

        raw = {
            "ok": False,
            "reason": "auth_expired",
            "hint": "login expired",
            "has_api_key": True,
            "model_count": 2,
        }
        probe = AsyncMock(return_value=raw)
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        cursor_models = [
            {"provider": cursor_provider, "id": "c1"},
            {"provider": cursor_provider, "id": "c2"},
        ]
        monkeypatch.setattr(
            main,
            "scoped_models_for_request",
            AsyncMock(
                return_value={
                    cursor_provider: cursor_models,
                    "EnMaaS": [{"provider": "EnMaaS", "id": "m1"}],
                }
            ),
        )
        self._patch_listing(monkeypatch, {})

        data = await main.list_ai_models(self._request(is_admin=is_admin), provider="")

        status = data["provider_status"]["cursor"]
        if is_admin:
            probe.assert_awaited_once_with(model_count=2)
            assert status["has_api_key"] is True
            assert status["reason"] == "auth_expired"
        else:
            probe.assert_not_awaited()
            assert status == {
                "ok": True,
                "reason": None,
                "hint": None,
                "model_count": 2,
            }

    @pytest.mark.asyncio
    async def test_list_cursor_provider_keeps_status(self, monkeypatch) -> None:
        """Single-provider Cursor listing is unchanged."""
        from rootcoz import main

        monkeypatch.setattr(
            main, "probe_cursor_auth", AsyncMock(return_value={"ok": True})
        )
        monkeypatch.setattr(
            main,
            "scoped_models_for_request",
            AsyncMock(return_value={"cursor": [{"provider": "cursor", "id": "c1"}]}),
        )
        self._patch_listing(monkeypatch, {})

        data = await main.list_ai_models(
            self._request(is_admin=True), provider="cursor"
        )

        assert data["provider"] == "cursor"
        assert data["provider_status"]["cursor"]["ok"] is True

    @pytest.mark.asyncio
    async def test_list_non_cursor_provider_has_no_status(self, monkeypatch) -> None:
        """Single-provider non-Cursor listing never carries provider_status."""
        from rootcoz import main

        probe = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        monkeypatch.setattr(
            main,
            "scoped_models_for_request",
            AsyncMock(return_value={"EnMaaS": [{"provider": "EnMaaS", "id": "m1"}]}),
        )
        self._patch_listing(monkeypatch, {})

        data = await main.list_ai_models(
            self._request(is_admin=True), provider="EnMaaS"
        )

        assert "provider_status" not in data
        probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_omits_cursor_without_cursor_provider(
        self, monkeypatch
    ) -> None:
        """Admin refresh skips the probe and the cursor key without Cursor."""
        from rootcoz import main

        mock_models = [{"provider": "EnMaaS", "id": "m1", "name": "M1"}]
        probe = AsyncMock(return_value={"ok": False, "reason": "auth_expired"})
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        monkeypatch.setattr(main, "update_model_catalog", lambda models: None)
        monkeypatch.setattr(main, "clear_cursor_auth_cache", lambda: None)
        monkeypatch.setattr(
            main, "registered_provider_ids", AsyncMock(return_value=set())
        )
        monkeypatch.setattr(
            main,
            "capture_deployment_provider_listings",
            AsyncMock(return_value={}),
        )
        with patch("pi_sidecar_client.get_sidecar_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.refresh_models.return_value = mock_models
            mock_get_client.return_value = mock_client
            data = await main.refresh_ai_models(self._request(is_admin=True))

        assert data["models"] == mock_models
        assert data["count"] == 1
        assert "provider_status" not in data
        probe.assert_not_awaited()

    @pytest.mark.parametrize(
        "registry_payload",
        [
            None,
            "providers",
            [None, "junk", {"provider": ""}, {"provider": 3}],
            [None, "junk", {"provider": "EnMaaS"}],
        ],
    )
    def test_refresh_survives_unusable_provider_registry(
        self, test_client, monkeypatch, registry_payload: object
    ) -> None:
        """A malformed /providers payload cannot fail the whole refresh (200)."""
        from rootcoz import ai_client

        mock_models = [{"provider": "EnMaaS", "id": "m1", "name": "M1"}]
        monkeypatch.setattr(
            ai_client,
            "get_sidecar_client",
            lambda: SimpleNamespace(
                get_providers=AsyncMock(return_value=registry_payload)
            ),
        )
        with patch("pi_sidecar_client.get_sidecar_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.refresh_models.return_value = mock_models
            mock_get_client.return_value = mock_client
            response = test_client.post("/api/admin/ai-models/refresh")

        assert response.status_code == 200
        assert response.json()["models"] == mock_models

    @pytest.mark.parametrize("cursor_provider", ["cursor", "acpx-cursor"])
    @pytest.mark.asyncio
    async def test_refresh_reports_registered_cursor_with_zero_models(
        self, monkeypatch, cursor_provider: str
    ) -> None:
        """A registered Cursor provider with zero refreshed models is probed."""
        from rootcoz import main

        raw = {"ok": False, "reason": "auth_expired", "hint": "login expired"}
        probe = AsyncMock(return_value=raw)
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        monkeypatch.setattr(main, "update_model_catalog", lambda models: None)
        monkeypatch.setattr(main, "clear_cursor_auth_cache", lambda: None)
        monkeypatch.setattr(
            main, "registered_provider_ids", AsyncMock(return_value={cursor_provider})
        )
        monkeypatch.setattr(
            main,
            "capture_deployment_provider_listings",
            AsyncMock(return_value={}),
        )
        with patch("pi_sidecar_client.get_sidecar_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.refresh_models.return_value = []
            mock_get_client.return_value = mock_client
            data = await main.refresh_ai_models(self._request(is_admin=True))

        probe.assert_awaited_once_with(force=True)
        assert data["provider_status"]["cursor"] == raw

    @pytest.mark.parametrize("cursor_provider", ["cursor", "acpx-cursor"])
    @pytest.mark.asyncio
    async def test_refresh_reports_cursor_with_cursor_provider(
        self, monkeypatch, cursor_provider: str
    ) -> None:
        """Admin refresh keeps the forced Cursor probe when Cursor is present."""
        from rootcoz import main

        mock_models = [{"provider": cursor_provider, "id": "c1", "name": "C1"}]
        raw = {"ok": True, "reason": None, "hint": None, "has_api_key": False}
        probe = AsyncMock(return_value=raw)
        monkeypatch.setattr(main, "probe_cursor_auth", probe)
        monkeypatch.setattr(main, "update_model_catalog", lambda models: None)
        monkeypatch.setattr(main, "clear_cursor_auth_cache", lambda: None)
        monkeypatch.setattr(
            main, "registered_provider_ids", AsyncMock(return_value=set())
        )
        monkeypatch.setattr(
            main,
            "capture_deployment_provider_listings",
            AsyncMock(return_value={}),
        )
        with patch("pi_sidecar_client.get_sidecar_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.refresh_models.return_value = mock_models
            mock_get_client.return_value = mock_client
            data = await main.refresh_ai_models(self._request(is_admin=True))

        probe.assert_awaited_once_with(force=True)
        assert data["provider_status"]["cursor"] == raw


class TestCursorStatusForClient:
    """Tests for _cursor_status_for_client credential redaction."""

    def test_admin_keeps_has_api_key(self) -> None:
        from rootcoz.main import _cursor_status_for_client

        raw = {
            "ok": False,
            "reason": "auth_expired",
            "hint": "login expired",
            "has_api_key": False,
            "model_count": 0,
        }
        out = _cursor_status_for_client(raw, is_admin=True)
        assert out["has_api_key"] is False
        assert out["reason"] == "auth_expired"
        assert out["hint"] == "login expired"

    def test_non_admin_strips_credential_state(self) -> None:
        from rootcoz.main import _cursor_status_for_client

        raw = {
            "ok": False,
            "reason": "api_key_not_applied",
            "hint": "CURSOR_API_KEY is set but unavailable",
            "has_api_key": True,
            "model_count": 0,
        }
        out = _cursor_status_for_client(raw, is_admin=False)
        assert "has_api_key" not in out
        assert out["reason"] == "unavailable"
        assert "administrator" in out["hint"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin, credential_source",
    [(False, "user"), (False, "server"), (True, "user"), (True, "server")],
)
async def test_chat_send_validates_with_sender_credentials(
    monkeypatch, tmp_path, admin, credential_source
):
    from rootcoz import ai_client, main
    from rootcoz.models import ChatMessageRequest

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "send.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("sid", "alice", "openai", credential_source)
    await storage.add_chat_message(
        main.ADMIN_CHAT_JOB_ID if admin else "job",
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="test-model",
        session_id="sid",
    )

    request = SimpleNamespace(
        state=SimpleNamespace(
            username="alice", role="admin" if admin else "reviewer", is_admin=admin
        )
    )
    monkeypatch.setattr(
        main,
        "get_result",
        AsyncMock(return_value={"result": {"request_params": {}}}),
    )
    monkeypatch.setattr(
        main.storage, "add_chat_message_pair", AsyncMock(return_value=(1, 2))
    )
    monkeypatch.setattr(main, "notify_chat_changed", lambda *args, **kwargs: None)
    seen = []

    async def validate(provider, model):
        seen.append((ai_client.ai_username.get(), "validated"))
        ai_client._selected_credential_source.set(credential_source)
        return provider, model

    monkeypatch.setattr(main, "resolve_catalog_pair", validate)
    ai_client._selected_credential_source.set("")
    send = main.send_admin_chat_message if admin else main.send_chat_message
    args = (
        ChatMessageRequest(
            message="hello", ai_provider="openai", ai_model="test-model"
        ),
        request,
        BackgroundTasks(),
    )
    original_user = ai_client.ai_username.set("previous")
    try:
        result = await (send(*args) if admin else send("job", *args))
        assert result["assistant_message_id"] == 2
        # The sender's identity is the scope for validation, then restored.
        assert seen == [("alice", "validated")]
        assert ai_client.ai_username.get() == "previous"

        monkeypatch.setattr(
            main,
            "resolve_catalog_pair",
            AsyncMock(side_effect=ValueError("invalid model")),
        )
        with pytest.raises(HTTPException) as exc:
            await (send(*args) if admin else send("job", *args))
        assert exc.value.status_code == 422
        assert ai_client.ai_username.get() == "previous"
    finally:
        ai_client.ai_username.reset(original_user)


@pytest.mark.asyncio
async def test_admin_chat_init_uses_selected_source(monkeypatch, tmp_path):
    from rootcoz import ai_client, main
    from rootcoz.engine import chat
    from rootcoz.models import ChatInitRequest

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "admin-init.db")
    await storage.init_db()
    await storage.create_admin_user("alice")

    async def validate(provider, model):
        ai_client._selected_credential_source.set("server")
        return provider, model

    monkeypatch.setattr(main, "_validate_catalog_pair", validate)
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main.storage, "get_chat_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        main.storage, "get_user_ai_credential_generation", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(main.storage, "add_chat_message", AsyncMock(return_value=1))
    seen = []

    async def init(**kwargs):
        seen.append((ai_client.ai_username.get(), ai_client.chat_session_source.get()))
        return "session"

    monkeypatch.setattr(chat, "init_admin_chat_session", init)
    request = SimpleNamespace(state=SimpleNamespace(username="alice", is_admin=True))
    original_source = ai_client.chat_session_source.set("user")
    try:
        body = ChatInitRequest(ai_provider="openai", ai_model="test-model")
        assert (await main.init_admin_chat(request, body))["session_id"] == "session"
        assert seen == [("alice", "server")]
        # The per-session source is scoped to the init call only.
        assert ai_client.chat_session_source.get() == "user"
        monkeypatch.setattr(
            chat,
            "init_admin_chat_session",
            AsyncMock(side_effect=RuntimeError("unavailable")),
        )
        await storage.delete_chat_messages(main.ADMIN_CHAT_JOB_ID, username="alice")
        with pytest.raises(RuntimeError, match="unavailable"):
            await main.init_admin_chat(request, body)
        assert ai_client.chat_session_source.get() == "user"
    finally:
        ai_client.chat_session_source.reset(original_source)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_admin_chat_processing_uses_session_source_and_resets_context(
    monkeypatch, tmp_path, fails
):
    from rootcoz import ai_client, main
    from rootcoz.engine import chat

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "admin-process.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    await storage.save_ai_session_source("sid", "alice", "openai", "server")
    await storage.add_chat_message(
        main.ADMIN_CHAT_JOB_ID,
        "assistant",
        "",
        username="alice",
        ai_provider="openai",
        ai_model="test-model",
        session_id="sid",
    )

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            ai_provider="openai",
            ai_model="test-model",
            ai_call_timeout=30,
            enable_github_issues=False,
            enable_jira_issues=False,
        ),
    )
    monkeypatch.setattr(
        main.storage, "get_user_ai_credential_generation", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(main.storage, "count_chat_messages", AsyncMock(return_value=0))
    monkeypatch.setattr(chat, "ensure_chat_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(main, "_build_internal_server_url", lambda: "http://localhost")
    monkeypatch.setattr(chat, "build_admin_custom_tools", lambda **kwargs: [])
    monkeypatch.setattr(main.storage, "update_chat_message_content", AsyncMock())
    monkeypatch.setattr(main.storage, "update_chat_message_status", AsyncMock())
    monkeypatch.setattr(
        main.storage, "get_chat_message_status", AsyncMock(return_value="pending")
    )
    monkeypatch.setattr(
        main.storage,
        "complete_chat_message_if_generation",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(main, "notify_chat_changed", lambda *args, **kwargs: None)
    seen = []

    async def run(**kwargs):
        seen.append((ai_client.ai_username.get(), ai_client.chat_session_source.get()))
        if fails:
            raise RuntimeError("unavailable")
        return True, "reply", "session"

    monkeypatch.setattr(chat, "admin_chat_with_ai", run)
    original_source = ai_client.chat_session_source.set("user")
    original_user = ai_client.ai_username.set("previous")
    try:
        await main._process_admin_chat_message(
            user_msg_id=1,
            assistant_msg_id=2,
            message="hello",
            ai_provider_override="openai",
            ai_model_override="test-model",
            username="alice",
            credential_source="server",
        )
        assert seen == [("alice", "server")]
        assert ai_client.ai_username.get() == "previous"
        assert ai_client.chat_session_source.get() == "user"
    finally:
        ai_client.ai_username.reset(original_user)
        ai_client.chat_session_source.reset(original_source)


class TestResolveChatAiConfig:
    """Tests for shared chat provider/model resolution helper."""

    def test_override_pair_wins(self) -> None:
        from rootcoz.main import _resolve_chat_ai_config

        provider, model = _resolve_chat_ai_config(
            override_provider="cursor",
            override_model="composer-1",
            settings_provider="claude",
            settings_model="sonnet",
            result_data={"ai_provider": "gemini", "ai_model": "pro"},
        )
        assert (provider, model) == ("cursor", "composer-1")

    def test_partial_override_raises(self) -> None:
        from rootcoz.main import _resolve_chat_ai_config

        with pytest.raises(RuntimeError, match="Both AI provider and model"):
            _resolve_chat_ai_config(
                override_provider="cursor",
                override_model=None,
                settings_provider="claude",
                settings_model="sonnet",
            )

    def test_falls_back_to_result_then_settings(self) -> None:
        from rootcoz.main import _resolve_chat_ai_config

        provider, model = _resolve_chat_ai_config(
            override_provider=None,
            override_model=None,
            settings_provider="claude",
            settings_model="sonnet",
            result_data={"ai_provider": "gemini", "ai_model": ""},
            request_params={"ai_model": "flash"},
        )
        assert (provider, model) == ("gemini", "flash")

    def test_missing_config_admin_mentions_settings(self) -> None:
        from rootcoz.main import _resolve_chat_ai_config

        with pytest.raises(RuntimeError, match="Server Settings"):
            _resolve_chat_ai_config(
                override_provider=None,
                override_model=None,
                settings_provider="",
                settings_model="",
                is_admin=True,
            )

    def test_missing_config_non_admin_contacts_admin(self) -> None:
        from rootcoz.main import _resolve_chat_ai_config

        with pytest.raises(RuntimeError, match="contact a server administrator"):
            _resolve_chat_ai_config(
                override_provider=None,
                override_model=None,
                settings_provider="",
                settings_model="",
                is_admin=False,
            )


class TestSanitizeSidecarPlaceholder:
    """Unsubstituted sidecar placeholders must not become filter values."""

    def test_clears_brace_placeholders(self) -> None:
        from rootcoz.main import _sanitize_sidecar_placeholder

        assert _sanitize_sidecar_placeholder("{job_name}") == ""
        assert _sanitize_sidecar_placeholder("{job_id}") == ""
        assert _sanitize_sidecar_placeholder("{references}") == ""
        assert _sanitize_sidecar_placeholder("real-job") == "real-job"
        assert _sanitize_sidecar_placeholder("") == ""


class TestGetJobTestsEndpoint:
    """Tests for GET /api/results/{job_id}/tests."""

    @pytest.mark.asyncio
    async def test_placeholder_status_treated_as_omitted(self, test_client) -> None:
        """Sidecar `{status}` placeholder must not 400 — treat as no filter."""
        await storage.save_result(
            job_id="job-tests-placeholder",
            jenkins_url="https://jenkins.example.com/job/t/1/",
            status="completed",
            result={"summary": "ok"},
        )
        await storage.save_test_entries(
            "job-tests-placeholder",
            [
                {
                    "test_name": "a.pass",
                    "duration": 0.1,
                    "status": "passed",
                },
                {
                    "test_name": "a.fail",
                    "duration": 0.2,
                    "status": "failed",
                },
            ],
        )

        response = test_client.get(
            "/api/results/job-tests-placeholder/tests",
            params={"status": "{status}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_child_scope_filter(self, test_client) -> None:
        """Child filters return only that child's test entries."""
        await storage.save_result(
            job_id="job-tests-child",
            jenkins_url="https://jenkins.example.com/job/pipe/1/",
            status="completed",
            result={"summary": "ok"},
        )
        await storage.save_test_entries(
            "job-tests-child",
            [
                {
                    "test_name": "child.pass",
                    "duration": 1.0,
                    "status": "passed",
                }
            ],
            child_job_name="runner",
            child_build_number=7,
        )

        response = test_client.get(
            "/api/results/job-tests-child/tests",
            params={
                "status": "passed",
                "child_job_name": "runner",
                "child_build_number": 7,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["entries"][0]["test_name"] == "child.pass"


class TestGetJobResultFullEndpoint:
    """Tests for GET /api/results/{job_id}/full."""

    @pytest.mark.asyncio
    async def test_full_response_joins_everything(self, test_client) -> None:
        """Failures across child jobs are flattened with reviews joined."""
        result_data = {
            "status": "completed",
            "summary": "2 failures",
            "job_name": "pipeline-main",
            "build_number": 42,
            "passed_count": 10,
            "skipped_count": 2,
            "failed_count": 3,
            "failures": [
                {
                    "test_name": "test_top",
                    "error": "boom",
                    "error_signature": "sig-top",
                    "analysis": {
                        "classification": "CODE ISSUE",
                        "pattern": "NEW",
                    },
                }
            ],
            "child_job_analyses": [
                {
                    "job_name": "child-job",
                    "build_number": 7,
                    "failures": [
                        {
                            "test_name": "test_child",
                            "error": "crash",
                            "error_signature": "sig-child",
                            "analysis": {
                                "classification": "PRODUCT BUG",
                                "pattern": "REGRESSION",
                            },
                        }
                    ],
                    "failed_children": [],
                }
            ],
        }
        await storage.save_result(
            "job-full-1", "http://jenkins", "completed", result_data
        )
        await storage.set_reviewed(
            "job-full-1", "test_top", reviewed=True, username="alice"
        )
        # Wildcard child review (build 0) covers the child-scoped failure
        await storage.set_reviewed(
            "job-full-1",
            "test_child",
            reviewed=True,
            username="bob",
            child_job_name="child-job",
            child_build_number=0,
        )
        await storage.add_comment(
            "job-full-1", "test_top", "investigating", username="carol"
        )
        await storage.add_tracked_in_link(
            "job-full-1",
            "test_top",
            "https://github.com/org/repo/issues/1",
            "github",
            tracked_by="alice",
        )

        response = test_client.get("/api/results/job-full-1/full")
        assert response.status_code == 200
        data = response.json()

        assert data["job"]["job_id"] == "job-full-1"
        assert data["job"]["job_name"] == "pipeline-main"
        assert data["job"]["build_number"] == 42
        assert data["job"]["status"] == "completed"
        assert data["counts"] == {"passed": 10, "skipped": 2, "failed": 3}

        failures = data["failures"]
        assert len(failures) == 2
        top = failures[0]
        assert top["test_name"] == "test_top"
        assert top["classification"] == "CODE ISSUE"
        assert top["reviewed"] is True
        assert top["reviewed_by"] == "alice"
        assert top["child_job_name"] == ""
        child = failures[1]
        assert child["test_name"] == "test_child"
        assert child["child_job_name"] == "child-job"
        assert child["child_build_number"] == 7
        assert child["reviewed"] is True
        assert child["reviewed_by"] == "bob"

        assert data["summary"] == {
            "total_failures": 2,
            "reviewed_count": 2,
            "classifications": {"CODE ISSUE": 1, "PRODUCT BUG": 1},
        }
        assert data["tests"]["total"] == 0
        assert len(data["comments"]) == 1
        assert data["comments"][0]["comment"] == "investigating"
        assert "test_top" in data["tracked_in"]

    @pytest.mark.asyncio
    async def test_exact_child_review_takes_precedence(self, test_client) -> None:
        """Exact-build review wins over the wildcard (build 0) one."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "child_job_analyses": [
                {
                    "job_name": "runner",
                    "build_number": 9,
                    "failures": [
                        {
                            "test_name": "test_x",
                            "error": "e",
                            "analysis": {"classification": "INFRASTRUCTURE"},
                        }
                    ],
                }
            ],
        }
        await storage.save_result(
            "job-full-2", "http://jenkins", "completed", result_data
        )
        await storage.set_reviewed(
            "job-full-2",
            "test_x",
            reviewed=True,
            username="wildcard-user",
            child_job_name="runner",
            child_build_number=0,
        )
        await storage.set_reviewed(
            "job-full-2",
            "test_x",
            reviewed=True,
            username="exact-user",
            child_job_name="runner",
            child_build_number=9,
        )

        data = test_client.get("/api/results/job-full-2/full").json()
        assert data["failures"][0]["reviewed_by"] == "exact-user"

    @pytest.mark.asyncio
    async def test_bare_review_does_not_apply_to_child_scope(self, test_client) -> None:
        """Top-level reviews never mark child-scoped failures as reviewed."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [],
            "child_job_analyses": [
                {
                    "job_name": "runner",
                    "build_number": 3,
                    "failures": [
                        {
                            "test_name": "test_y",
                            "error": "e",
                            "analysis": {"classification": "CODE ISSUE"},
                        }
                    ],
                }
            ],
        }
        await storage.save_result(
            "job-full-3", "http://jenkins", "completed", result_data
        )
        await storage.set_reviewed(
            "job-full-3", "test_y", reviewed=True, username="alice"
        )

        data = test_client.get("/api/results/job-full-3/full").json()
        assert data["failures"][0]["reviewed"] is False
        assert data["failures"][0]["reviewed_by"] == ""
        assert data["summary"]["reviewed_count"] == 0

    @pytest.mark.asyncio
    async def test_effective_classification_override_applied(self, test_client) -> None:
        """User overrides replace the stored classification in the response."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_override",
                    "error": "e",
                    "analysis": {"classification": "CODE ISSUE"},
                }
            ],
        }
        await storage.save_result(
            "job-full-4", "http://jenkins", "completed", result_data
        )
        await storage.override_classification(
            "job-full-4", "test_override", "PRODUCT BUG", username="alice"
        )

        data = test_client.get("/api/results/job-full-4/full").json()
        assert data["failures"][0]["classification"] == "PRODUCT BUG"
        assert data["summary"]["classifications"] == {"PRODUCT BUG": 1}

    @pytest.mark.asyncio
    async def test_tests_section_pagination_and_filters(self, test_client) -> None:
        """Tests section honors the /tests pagination contract."""
        await storage.save_result(
            "job-full-5",
            "http://jenkins",
            "completed",
            {"status": "completed", "summary": "ok", "passed_count": 3},
        )
        await storage.save_test_entries(
            "job-full-5",
            [
                {"test_name": f"t{i}", "duration": 0.1, "status": "passed"}
                for i in range(3)
            ],
        )

        base = "/api/results/job-full-5/full"
        data = test_client.get(f"{base}?limit=2").json()
        assert data["tests"]["total"] == 3
        assert len(data["tests"]["entries"]) == 2
        assert data["tests"]["has_more"] is True

        data = test_client.get(f"{base}?limit=2&offset=2").json()
        assert len(data["tests"]["entries"]) == 1
        assert data["tests"]["has_more"] is False

        data = test_client.get(f"{base}?status=failed").json()
        assert data["tests"]["total"] == 0
        assert data["tests"]["entries"] == []

    @pytest.mark.asyncio
    async def test_404_unknown_job(self, test_client) -> None:
        response = test_client.get("/api/results/job-full-missing/full")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_202_in_progress(self, test_client) -> None:
        await storage.save_result(
            "job-full-6", "http://jenkins", "running", {"summary": ""}
        )
        response = test_client.get("/api/results/job-full-6/full")
        assert response.status_code == 202
        data = response.json()
        assert data["job"]["status"] == "running"
        assert data["failures"] == []

    @pytest.mark.asyncio
    async def test_legacy_string_analysis_handled(self, test_client) -> None:
        """Legacy results with a plain-text analysis string must not crash."""
        result_data = {
            "status": "completed",
            "summary": "",
            "failures": [
                {
                    "test_name": "test_legacy",
                    "error": "e",
                    "analysis": "plain text analysis from an old run",
                }
            ],
        }
        await storage.save_result(
            "job-full-legacy", "http://jenkins", "completed", result_data
        )

        response = test_client.get("/api/results/job-full-legacy/full")
        assert response.status_code == 200
        data = response.json()
        failure = data["failures"][0]
        assert failure["test_name"] == "test_legacy"
        assert failure["classification"] == ""
        assert failure["pattern"] == ""
        assert data["summary"]["classifications"] == {}

    @pytest.mark.asyncio
    async def test_compressed_row_serves_the_complete_payload(
        self, test_client
    ) -> None:
        """A BLOB result_json reads exactly like an inline one over the API (#409)."""
        analysis = {"classification": "CODE ISSUE", "pattern": "NEW", "analysis": "x"}
        result_data = {
            "status": "completed",
            "summary": "2 failures",
            "job_name": "zstd-job",
            "build_number": 11,
            "passed_count": 1,
            "skipped_count": 0,
            "failed_count": 2,
            "request_params": {"submitted_by": "yuval", "build_id": "11"},
            "token_usage": {"total": {"input": 3, "output": 4}},
            "failures": [
                {
                    "id": f"zstd-failure-{index}",
                    "test_name": f"test_zstd_{index}",
                    "error": "boom " + "detail " * 200,
                    "analysis": analysis,
                }
                for index in range(2)
            ],
            "child_job_analyses": [
                {
                    "job_name": "child-job",
                    "build_number": 3,
                    "failures": [
                        {
                            "id": "zstd-child-failure",
                            "test_name": "test_child",
                            "error": "child boom " + "detail " * 200,
                            "analysis": analysis,
                        }
                    ],
                    "failed_children": [],
                }
            ],
        }
        await storage.save_result(
            "job-zstd-full", "http://jenkins", "completed", result_data
        )
        async with storage._connect_db() as db:
            row = await (
                await db.execute(
                    "SELECT typeof(result_json) FROM results WHERE job_id = ?",
                    ("job-zstd-full",),
                )
            ).fetchone()
        assert row[0] == "blob", "the payload must be stored compressed for this test"

        response = test_client.get("/api/results/job-zstd-full/full")
        assert response.status_code == 200
        data = response.json()
        assert data["job"]["job_name"] == "zstd-job"
        assert data["job"]["build_number"] == 11
        assert data["summary"]["total_failures"] == 3
        assert [
            (failure["test_name"], failure["classification"])
            for failure in data["failures"]
        ] == [
            ("test_zstd_0", "CODE ISSUE"),
            ("test_zstd_1", "CODE ISSUE"),
            ("test_child", "CODE ISSUE"),
        ]

    @pytest.mark.asyncio
    async def test_compressed_row_sparse_fields_projection_is_complete(
        self, test_client
    ) -> None:
        """A ``fields=`` projection returns full-length values from a compressed row."""
        long_analysis = "root cause: " + "detail " * 400
        result_data = {
            "status": "completed",
            "summary": "one failure",
            "job_name": "zstd-sparse",
            "build_number": 5,
            "failures": [
                {
                    "test_name": "test_sparse",
                    "error": "boom",
                    "analysis": {
                        "classification": "PRODUCT BUG",
                        "pattern": "NEW",
                        "analysis": long_analysis,
                    },
                }
            ],
        }
        await storage.save_result(
            "job-zstd-sparse", "http://jenkins", "completed", result_data
        )
        async with storage._connect_db() as db:
            row = await (
                await db.execute(
                    "SELECT typeof(result_json) FROM results WHERE job_id = ?",
                    ("job-zstd-sparse",),
                )
            ).fetchone()
        assert row[0] == "blob"

        response = test_client.get(
            "/results/job-zstd-sparse",
            params={"fields": "result.failures.analysis,result.summary,status"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["result"]["summary"] == "one failure"
        assert data["result"]["failures"][0]["analysis"]["analysis"] == long_analysis

    @pytest.mark.asyncio
    async def test_cli_parity_with_endpoint_response(self, test_client) -> None:
        """Client and CLI JSON output preserve the exact endpoint payload.

        The client method and the ``results full --json`` command consume the
        same payload the endpoint returns — verified against the real
        endpoint response rather than an independently authored mock.
        """
        result_data = {
            "status": "completed",
            "summary": "1 failure",
            "job_name": "pipeline-parity",
            "build_number": 7,
            "passed_count": 2,
            "skipped_count": 1,
            "failed_count": 1,
            "failures": [
                {
                    "test_name": "test_parity",
                    "error": "boom",
                    "error_signature": "sig-parity",
                    "analysis": {
                        "classification": "CODE ISSUE",
                        "pattern": "NEW",
                    },
                }
            ],
        }
        await storage.save_result(
            "job-full-parity", "http://jenkins", "completed", result_data
        )
        await storage.set_reviewed(
            "job-full-parity", "test_parity", reviewed=True, username="alice"
        )
        await storage.save_test_entries(
            "job-full-parity",
            [
                {"test_name": "t.pass", "duration": 0.1, "status": "passed"},
                {"test_name": "t.skip", "duration": 0.0, "status": "skipped"},
            ],
        )
        await storage.add_comment(
            "job-full-parity", "test_parity", "looks real", username="bob"
        )
        await storage.add_tracked_in_link(
            "job-full-parity",
            "test_parity",
            "https://github.com/org/repo/issues/3",
            "github",
            tracked_by="alice",
        )

        response = test_client.get("/api/results/job-full-parity/full")
        assert response.status_code == 200
        endpoint_payload = response.json()

        # The API client returns the endpoint payload unchanged
        api_client = make_test_client(
            lambda request: httpx.Response(200, json=endpoint_payload)
        )
        assert api_client.get_full_result("job-full-parity") == endpoint_payload

        # The CLI --json output is the endpoint payload
        from typer.testing import CliRunner

        from rootcoz.cli import main as cli_main
        from rootcoz.cli.config import ServerConfig

        mock_cli_client = MagicMock()
        mock_cli_client.get_full_result.return_value = endpoint_payload
        with (
            patch.dict(os.environ, {"ROOTCOZ_SERVER": "parity-test"}),
            patch(
                "rootcoz.cli.main.get_server_config",
                return_value=ServerConfig(url="http://localhost:8700"),
            ),
            patch("rootcoz.cli.main._get_client", return_value=mock_cli_client),
        ):
            cli_result = CliRunner().invoke(
                cli_main.app, ["results", "full", "job-full-parity", "--json"]
            )
        assert cli_result.exit_code == 0
        assert json.loads(cli_result.output) == endpoint_payload

    @pytest.mark.asyncio
    async def test_invalid_status_400(self, test_client) -> None:
        await storage.save_result(
            "job-full-7", "http://jenkins", "completed", {"summary": "ok"}
        )
        response = test_client.get(
            "/api/results/job-full-7/full", params={"status": "bogus"}
        )
        assert response.status_code == 400
        assert "Invalid status values" in response.json()["detail"]


class TestPersistTestEntriesAndCacheCounts:
    """Tests for the shared early-exit counts helper."""

    @pytest.mark.asyncio
    async def test_persists_entries_and_caches_counts(self, test_client) -> None:
        from rootcoz.main import _persist_test_entries_and_cache_counts
        from rootcoz.models import BaseTestEntry
        from rootcoz.sources.base import CISourceResult

        source_result = CISourceResult(
            failures=[],
            passed_tests=[
                BaseTestEntry(test_name="a.pass", duration=0.1, status="passed")
            ],
            skipped_tests=[
                BaseTestEntry(test_name="b.skip", duration=0.0, status="skipped")
            ],
        )
        child_scopes = [
            (
                "child-job",
                3,
                [{"test_name": "c.fail", "duration": 0.2, "status": "failed"}],
            )
        ]
        result_data: dict[str, Any] = {}

        await _persist_test_entries_and_cache_counts(
            "job-counts-1", source_result, child_scopes, result_data
        )

        assert result_data == {
            "passed_count": 1,
            "skipped_count": 1,
            "failed_count": 1,
        }
        entries = await storage.get_test_entries("job-counts-1")
        assert entries["total"] == 3
        # Child entries are stored with their scope
        child_entries = await storage.get_test_entries(
            "job-counts-1", child_job_name="child-job", child_build_number=3
        )
        assert child_entries["total"] == 1
        assert child_entries["entries"][0]["test_name"] == "c.fail"

    @pytest.mark.asyncio
    async def test_no_counts_cached_when_persist_fails(self, test_client) -> None:
        """Persist failure must not leave counts the DB cannot back."""
        from rootcoz.main import _persist_test_entries_and_cache_counts
        from rootcoz.models import BaseTestEntry
        from rootcoz.sources.base import CISourceResult

        source_result = CISourceResult(
            failures=[],
            passed_tests=[
                BaseTestEntry(test_name="a.pass", duration=0.1, status="passed")
            ],
        )
        result_data: dict[str, Any] = {}

        with patch(
            "rootcoz.main.replace_job_test_entries",
            side_effect=RuntimeError("db locked"),
        ):
            await _persist_test_entries_and_cache_counts(
                "job-counts-2", source_result, None, result_data
            )

        assert "passed_count" not in result_data


class TestResolveFailureReview:
    """Unit tests for the shared review-resolution helper."""

    def _reviews(self) -> dict[str, dict[str, Any]]:
        return {
            "test_bare": {"reviewed": True, "username": "alice"},
            "runner#9::test_x": {
                "reviewed": True,
                "username": "exact-user",
            },
            "runner#0::test_x": {
                "reviewed": True,
                "username": "wildcard-user",
            },
            "test_unreviewed": {"reviewed": False, "username": ""},
            "test_anonymous": {"reviewed": True, "username": ""},
        }

    def test_exact_beats_wildcard(self) -> None:
        from rootcoz.main import resolve_failure_review

        entry = resolve_failure_review(self._reviews(), "test_x", "runner", 9)
        assert entry is not None
        assert entry["username"] == "exact-user"

    def test_wildcard_fallback(self) -> None:
        from rootcoz.main import resolve_failure_review

        entry = resolve_failure_review(self._reviews(), "test_x", "runner", 12)
        assert entry is not None
        assert entry["username"] == "wildcard-user"

    def test_bare_lookup(self) -> None:
        from rootcoz.main import resolve_failure_review

        entry = resolve_failure_review(self._reviews(), "test_bare")
        assert entry is not None
        assert entry["username"] == "alice"

    def test_bare_never_matches_child_scope(self) -> None:
        from rootcoz.main import resolve_failure_review

        assert resolve_failure_review(self._reviews(), "test_bare", "runner", 9) is None

    def test_child_never_matches_bare_scope(self) -> None:
        from rootcoz.main import resolve_failure_review

        assert resolve_failure_review(self._reviews(), "test_x") is None

    def test_unreviewed_and_anonymous_return_none(self) -> None:
        from rootcoz.main import resolve_failure_review

        assert resolve_failure_review(self._reviews(), "test_unreviewed") is None
        assert resolve_failure_review(self._reviews(), "test_anonymous") is None

    def test_missing_test_returns_none(self) -> None:
        from rootcoz.main import resolve_failure_review

        assert resolve_failure_review(self._reviews(), "test_nope") is None


class TestReadLogTail:
    """Bounded backwards log tail reader for /api/admin/logs/stream."""

    def test_returns_last_n_lines(self, tmp_path) -> None:
        from rootcoz.main import _read_log_tail

        path = tmp_path / "app.log"
        path.write_text("\n".join(f"line-{i}" for i in range(20)) + "\n")
        assert _read_log_tail(path, 5) == [f"line-{i}" for i in range(15, 20)]

    def test_respects_max_bytes_budget(self, tmp_path) -> None:
        from rootcoz.main import _read_log_tail

        path = tmp_path / "big.log"
        # 50 lines of 100 bytes each; max_bytes=250 should not return all 50
        path.write_bytes((b"y" * 99 + b"\n") * 50)
        result = _read_log_tail(path, 50, max_bytes=250, chunk_size=100)
        assert 0 < len(result) < 50
        assert all(line.startswith("y") for line in result)


class TestLogStream:
    """SSE log stream: the tail is read only after the queue is subscribed."""

    async def _chunks(
        self,
        monkeypatch,
        tmp_path,
        tail_lines: list[str],
        published: list[str],
        expected: int,
    ) -> list[str]:
        """Stream chunks while ``published`` lines land during the tail read.

        Publishing from inside the patched tail reader models a line logged in
        the window between subscribing to the queue and reading the file.
        """
        from rootcoz import main as main_module

        log_path = tmp_path / "app.log"
        log_path.write_text("INFO old line\n")
        monkeypatch.setattr(main_module, "_LOG_FILE", str(log_path))
        monkeypatch.setattr(main_module, "record_audit", AsyncMock())

        def fake_tail(path, n):
            for line in published:
                main_module._log_broadcast.publish(line)
            return tail_lines

        monkeypatch.setattr(main_module, "_read_log_tail", fake_tail)

        request = AsyncMock()
        request.state.is_admin = True
        request.is_disconnected = AsyncMock(return_value=False)

        response = await main_module.stream_logs(request, lines=100, level="")
        body = response.body_iterator
        try:
            return [
                await asyncio.wait_for(anext(body), timeout=1) for _ in range(expected)
            ]
        finally:
            await body.aclose()

    async def test_line_logged_during_the_tail_read_is_not_lost(
        self, tmp_path, monkeypatch
    ) -> None:
        """A line logged while the file is being read is delivered exactly once."""
        racy = "INFO line logged mid-tail"

        chunks = await self._chunks(
            monkeypatch, tmp_path, ["INFO old line"], [racy], expected=3
        )

        # Tail line, the connect comment, then the live line.
        assert sum(chunk.count(racy) for chunk in chunks) == 1

    async def test_line_present_in_both_sources_is_delivered_once(
        self, tmp_path, monkeypatch
    ) -> None:
        """The live copy of a line the tail already sent is de-duplicated."""
        racy = "INFO line logged mid-tail"

        chunks = await self._chunks(
            monkeypatch, tmp_path, ["INFO old line", racy], [racy], expected=3
        )

        # Both tail lines plus the connect comment; the queued copy matched the
        # tail line already sent, so it was skipped.
        assert sum(chunk.count(racy) for chunk in chunks) == 1


class TestSubmitIntake:
    def test_submit_raw_does_not_require_ai(self, test_client) -> None:
        data, mock_process = _post_submit_queued(
            test_client,
            {
                "type": "raw",
                "failures": [
                    {"test_name": "test_foo", "error_message": "assert False"},
                ],
            },
        )
        assert "job_id" in data
        assert mock_process.await_args.kwargs["ingest_only"] is True

    def test_submit_sets_submitted_state(self, test_client) -> None:
        data, _ = _post_submit_queued(
            test_client,
            {
                "type": "raw",
                "failures": [{"test_name": "t", "error_message": "e"}],
            },
        )
        stored = test_client.get(f"/results/{data['job_id']}").json()
        assert stored["result"]["analysis_state"] == "submitted"

    def test_submit_does_not_persist_artifact_skip(self, test_client) -> None:
        data, mock_process = _post_submit_queued(
            test_client,
            {
                "type": "jenkins",
                "job_name": "test-job",
                "build_number": 1,
            },
        )
        merged = mock_process.await_args.kwargs["merged"]
        assert merged.get_job_artifacts is False
        stored = test_client.get(f"/results/{data['job_id']}").json()
        params = stored["result"]["request_params"]
        assert params.get("get_job_artifacts") is True

    def test_in_place_analyze_keeps_submitter_and_records_analyzer(
        self, test_client
    ) -> None:
        data, _ = _post_submit_queued(
            test_client,
            {
                "type": "raw",
                "failures": [{"test_name": "t", "error_message": "e"}],
            },
        )
        job_id = data["job_id"]

        async def _set_submitter() -> None:
            stored = await storage.get_result(job_id, strip_sensitive=False)
            result = stored["result"]
            result["request_params"]["submitted_by"] = "alice"
            result["tags"] = ["alice"]
            await storage.update_status(job_id, "completed", result)

        asyncio.run(_set_submitter())
        with patch("rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock):
            response = test_client.post(
                f"/results/{job_id}/analyze",
                json={"ai_provider": "claude", "ai_model": "test-model"},
            )
        assert response.status_code == 202, response.text
        stored = test_client.get(f"/results/{job_id}").json()["result"]
        assert stored["request_params"]["submitted_by"] == "alice"
        assert stored["analyzed_by"] == "admin"
        assert stored.get("analyzed_at")
        assert "alice" in stored["tags"]
        assert "admin" not in stored["tags"]

    def test_in_place_analyze_keeps_stored_artifact_opt_out(self, test_client) -> None:
        data, _ = _post_submit_queued(
            test_client,
            {
                "type": "jenkins",
                "job_name": "test-job",
                "build_number": 1,
            },
        )
        job_id = data["job_id"]

        async def _complete_with_artifacts_off() -> None:
            stored = await storage.get_result(job_id, strip_sensitive=False)
            result = stored["result"]
            result["request_params"]["get_job_artifacts"] = False
            await storage.update_status(job_id, "completed", result)

        asyncio.run(_complete_with_artifacts_off())
        with patch(
            "rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock
        ) as mock_process:
            response = test_client.post(
                f"/results/{job_id}/analyze",
                json={"ai_provider": "claude", "ai_model": "test-model"},
            )
            assert response.status_code == 202, response.text
            merged = mock_process.await_args.kwargs["merged"]
            assert merged.get_job_artifacts is False

    def test_in_place_analyze_requires_submitted(self, test_client) -> None:
        data, _ = _post_analyze_queued(
            test_client,
            {
                "type": "raw",
                "failures": [{"test_name": "t", "error_message": "e"}],
                "ai_provider": "claude",
                "ai_model": "test-model",
            },
        )
        response = test_client.post(f"/results/{data['job_id']}/analyze")
        assert response.status_code == 409

    def test_in_place_analyze_rejects_second_claim(self, test_client) -> None:
        data, _ = _post_submit_queued(
            test_client,
            {
                "type": "jenkins",
                "job_name": "test-job",
                "build_number": 1,
            },
        )
        job_id = data["job_id"]
        asyncio.run(storage.update_status(job_id, "completed"))
        analyze_body = {"ai_provider": "claude", "ai_model": "test-model"}
        with patch("rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock):
            first = test_client.post(f"/results/{job_id}/analyze", json=analyze_body)
            second = test_client.post(f"/results/{job_id}/analyze", json=analyze_body)
        assert first.status_code == 202, first.text
        assert second.status_code == 409

    @pytest.mark.asyncio
    async def test_re_analyze_verdicts_come_from_the_final_attempt(
        self, test_client
    ) -> None:
        """Both flags are per attempt: ``patch_result_json`` re-runs the callback.

        Attempt one finds the failure still running from an earlier run, attempt two
        finds that run finished and records it running again. Answering 409 from the
        first attempt's flag would leave the failure running with no worker behind
        it.
        """
        from rootcoz import storage

        fa = FailureAnalysis(
            test_name="tests.TestRetry.test_one",
            error="ValueError",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
        )
        await storage.save_result(
            "job-reanalyze-retry",
            "",
            "completed",
            {
                "summary": "1 failure",
                "failures": [fa.model_dump(mode="json")],
                "request_params": encrypt_sensitive_fields(
                    {
                        "ai_provider": "claude",
                        "ai_model": "opus",
                        "submitted_by": "admin",
                    }
                ),
            },
        )

        real_patch = storage.patch_result_json
        attempts: list[str] = []

        async def retried(job_id: str, patch_fn: Any, **kwargs: Any) -> None:
            if attempts:
                # The background re-analysis that the 202 started patches on its own.
                await real_patch(job_id, patch_fn, **kwargs)
                return

            def still_running(data: dict[str, Any]) -> None:
                attempts.append("running")
                data["failures"][0]["reanalysis_status"] = "running"
                patch_fn(data)

            await real_patch(job_id, still_running, **kwargs)

            def finished(data: dict[str, Any]) -> None:
                attempts.append("finished")
                data["failures"][0].pop("reanalysis_status", None)
                patch_fn(data)

            await real_patch(job_id, finished, **kwargs)

        with (
            patch("rootcoz.main.patch_result_json", retried),
            patch(
                "rootcoz.main.analyze_failure_group",
                new_callable=AsyncMock,
                return_value=[fa],
            ),
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("rootcoz.main.RepositoryManager"),
        ):
            response = test_client.post(f"/api/failures/{fa.id}/re-analyze")

        assert attempts == ["running", "finished"]
        assert response.status_code == 202, response.text

    @pytest.mark.parametrize("previous_status", ["completed", "failed"])
    def test_in_place_analyze_releases_claim_when_enqueue_fails(
        self, test_client, previous_status: str
    ) -> None:
        from fastapi import HTTPException

        data, _ = _post_submit_queued(
            test_client,
            {
                "type": "jenkins",
                "job_name": "test-job",
                "build_number": 1,
            },
        )
        job_id = data["job_id"]
        asyncio.run(storage.update_status(job_id, previous_status))
        analyze_body = {"ai_provider": "claude", "ai_model": "test-model"}
        with patch(
            "rootcoz.main._enqueue_ci_source_analysis",
            side_effect=HTTPException(status_code=422, detail="enqueue failed"),
        ):
            failed = test_client.post(f"/results/{job_id}/analyze", json=analyze_body)
        assert failed.status_code == 422
        stored = test_client.get(f"/results/{job_id}").json()
        assert stored["status"] == previous_status
        with patch("rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock):
            retry = test_client.post(f"/results/{job_id}/analyze", json=analyze_body)
        assert retry.status_code == 202, retry.text

    def test_dashboard_analysis_state_filter(self, test_client) -> None:
        ok = test_client.get(
            "/api/dashboard/filtered", params={"analysis_state": "submitted"}
        )
        assert ok.status_code == 200
        bad = test_client.get(
            "/api/dashboard/filtered", params={"analysis_state": "nope"}
        )
        assert bad.status_code == 400


def _post_submit_queued(test_client, payload: dict) -> tuple[dict, AsyncMock]:
    with patch(
        "rootcoz.main._process_ci_source_analysis", new_callable=AsyncMock
    ) as mock_process:
        response = test_client.post("/submit", json=payload)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        return data, mock_process


class TestAdminUsersPaging:
    """GET /api/admin/users paging and username search (plan E2E-J11)."""

    @staticmethod
    def _seed(test_client, *usernames: str) -> None:
        for username in usernames:
            response = test_client.post(
                "/api/admin/users/create",
                json={"username": username, "role": "viewer"},
            )
            assert response.status_code == 200

    def test_unpaged_call_keeps_every_user_and_reports_the_totals(
        self, test_client
    ) -> None:
        self._seed(test_client, "alice", "bob")

        body = test_client.get("/api/admin/users").json()

        assert {user["username"] for user in body["users"]} == {"alice", "bob"}
        assert body["total"] == 2
        # No limit asked for means nothing was cut, so the page is the whole set.
        assert body["limit"] == 2
        assert body["offset"] == 0

    def test_limit_and_offset_walk_the_unpaged_order_without_overlap(
        self, test_client
    ) -> None:
        self._seed(test_client, "alice", "bob", "carol", "dana")

        everyone = test_client.get("/api/admin/users").json()
        first = test_client.get(
            "/api/admin/users", params={"limit": 2, "offset": 0}
        ).json()
        second = test_client.get(
            "/api/admin/users", params={"limit": 2, "offset": 2}
        ).json()

        assert len(first["users"]) == len(second["users"]) == 2
        # The total ignores limit/offset; each page reports its own window.
        assert first["total"] == second["total"] == 4
        assert (first["limit"], first["offset"]) == (2, 0)
        assert (second["limit"], second["offset"]) == (2, 2)
        # Paged order is the unpaged order, not a reshuffle.
        assert [user["id"] for user in first["users"] + second["users"]] == [
            user["id"] for user in everyone["users"]
        ]
        assert [user["id"] for user in first["users"]] == [
            user["id"] for user in everyone["users"][:2]
        ]

    def test_query_matches_the_username_case_insensitively(self, test_client) -> None:
        self._seed(test_client, "alice", "bob", "malika")

        body = test_client.get("/api/admin/users", params={"q": "AL"}).json()

        assert sorted(user["username"] for user in body["users"]) == [
            "alice",
            "malika",
        ]
        assert body["total"] == 2
        # q and limit combine: total still ignores the page bound.
        paged = test_client.get(
            "/api/admin/users", params={"q": "AL", "limit": 1, "offset": 0}
        ).json()
        assert len(paged["users"]) == 1
        assert paged["total"] == 2

    @pytest.mark.parametrize(
        "params",
        [{"limit": 0}, {"limit": 501}, {"offset": -1}],
        ids=["limit-below-range", "limit-above-range", "negative-offset"],
    )
    def test_out_of_range_paging_is_rejected(self, test_client, params) -> None:
        assert test_client.get("/api/admin/users", params=params).status_code == 422


class TestAnalysisReaperIsOneCopy:
    """Both analysis ``finally`` blocks reap through one helper (#424).

    The sequence — revoke task first, then the graph/artifact steps, then the
    shielded revoke — is only correct when it is edited in one place. Two copies
    had already drifted (``if repo_manager:`` vs ``if repo_manager is not None:``),
    so the contract asserted here is the shared call, not a shape of the source.
    """

    #: The whole surface of the helper: no call site may grow a private variant.
    _KWARGS: ClassVar[set[str]] = {
        "job_id",
        "auth_header",
        "source",
        "repo_manager",
        "repo_path",
    }

    async def test_ci_source_analysis_reaps_through_the_shared_helper(
        self, temp_db_path: Path, monkeypatch
    ) -> None:
        from rootcoz.main import _process_ci_source_analysis
        from rootcoz.models import UnifiedAnalyzeRequest
        from rootcoz.sources.base import CISourceResult

        monkeypatch.setattr(storage, "DB_PATH", temp_db_path)
        body = UnifiedAnalyzeRequest(
            type="jenkins",
            job_name="my-job",
            build_number=1,
            ai_provider="claude",
            ai_model="test-model",
        )
        merged = _build_wait_settings(
            jenkins_url="https://jenkins.example.com", wait_for_completion=False
        )
        with (
            patch.object(storage, "DB_PATH", temp_db_path),
            patch(
                "rootcoz.sources.jenkins_source.JenkinsSource.fetch",
                new_callable=AsyncMock,
                return_value=CISourceResult(
                    failures=[], identity={"job_name": "my-job"}
                ),
            ),
            patch("rootcoz.main.safe_update_progress", new_callable=AsyncMock),
            _patch_preflight(),
            patch(
                "rootcoz.main._reap_analysis_workspace", new_callable=AsyncMock
            ) as reap,
        ):
            await storage.init_db()
            await storage.save_result("job-one-copy", "", "pending", {})
            await _process_ci_source_analysis(
                job_id="job-one-copy",
                body=body,
                merged=merged,
                display_name="my-job",
                ai_provider="claude",
                ai_model="test-model",
                peer_ai_configs=None,
                tests_repo_url="",
                tests_repo_ref="",
                resolved_tests_repo_token="",
                additional_repos_list=[],
                base_url="",
            )

        reap.assert_awaited_once()
        kwargs = reap.await_args.kwargs
        assert set(kwargs) == self._KWARGS
        assert kwargs["job_id"] == "job-one-copy"
        # The clone workspace the run created is handed to the reaper, not lost.
        assert kwargs["repo_manager"] is not None
        assert kwargs["repo_path"] is not None

    async def test_reanalysis_reaps_through_the_shared_helper(
        self, tmp_path, monkeypatch
    ) -> None:
        from rootcoz.main import _reanalyze_failure_background

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "one-copy.db")
        with (
            patch("rootcoz.main.RepositoryManager") as manager,
            patch(
                "rootcoz.main._create_ai_auth_header",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "rootcoz.main._resolve_settings_json_before_analysis",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "rootcoz.main._reap_analysis_workspace", new_callable=AsyncMock
            ) as reap,
        ):
            manager.return_value.create_workspace.return_value = tmp_path / "workspace"
            manager.return_value.clone_into.return_value = None
            await storage.init_db()
            await storage.save_result(
                "job-one-copy-2",
                "",
                "completed",
                {"failures": [{"id": "failure-id", "reanalysis_status": "running"}]},
            )
            await _reanalyze_failure_background(
                "job-one-copy-2",
                "failure-id",
                {},
                "claude",
                "opus",
                None,
                "",
                None,
                0,
                "https://example.com/tests",
                "main",
                "",
                None,
                "admin",
                1,
            )

        reap.assert_awaited_once()
        kwargs = reap.await_args.kwargs
        assert set(kwargs) == self._KWARGS
        assert kwargs["job_id"] == "job-one-copy-2"
        # Same manager and workspace the re-analyze run used: an inline `if
        # repo_manager:` copy would at best drift again, at worst skip the reaper.
        assert kwargs["repo_manager"] is manager.return_value
        assert kwargs["repo_path"] == tmp_path / "workspace"


# -- A refused analysis keeps its sentence (#423) --------------------------
#
# The analysis path never raises to its caller (the AIResult contract), so a
# refusal has to survive as text: through the retry decision, into the
# placeholder row and out as the job's stored error. These tests drive the same
# functions ``/analyze`` ends in, with the sidecar client refusing.

_LIMITS_LITERAL = (
    "Model metadata missing reliable input/output token limits for unknown model. "
    "Declare the model in the agent-dir models.json with contextWindow and maxTokens "
    "to use it with a session API key."
)
_LIMITS_SENTENCE = f"AI session refused: {_LIMITS_LITERAL}"
_LIBRARY_LIMITS = f"Sidecar session creation failed (HTTP 400): {_LIMITS_LITERAL}"
_HTTPX_KEYLESS = (
    "Client error '400 Bad Request' for url 'http://127.0.0.1:9100/sessions'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400"
)


def _ai_result(*, success: bool, text: str) -> Any:
    """An AIResult whose usage recording is a no-op (no tracker in a unit test)."""
    from pi_sidecar_client import AIResult

    result = AIResult(success=success, text=text, error=None if success else text)
    result.record_usage = AsyncMock()
    return result


def _refusal(monkeypatch, text: str, *, redact_key=None):
    """Make the engine's AI call reach the real refusal mapping.

    conftest denies the engine's AI call outright, so this installs the one
    production layer that turns a sidecar body into a sentence —
    ``_call_with_safe_error`` — over a fake library call that answers ``text``.
    """
    from rootcoz import ai_client

    refused = AsyncMock(
        side_effect=lambda *a, **k: _ai_result(success=False, text=text)
    )
    monkeypatch.setattr(ai_client, "_call_ai", refused)

    async def mapped(prompt, **kwargs):
        return await ai_client._call_with_safe_error(
            ai_client._call_ai, prompt, redact_key=redact_key, **kwargs
        )

    monkeypatch.setattr("rootcoz.engine.core.call_ai_once", mapped)
    return refused


def _one_group():
    from rootcoz.models import FailedTest

    failures = [
        FailedTest(test_name="test_a", error_message="boom", stack_trace="trace")
    ]
    return {"sig-1": failures}, failures


async def _orchestrated(tmp_path):
    from rootcoz.engine.core import run_orchestrated_analysis

    groups, _ = _one_group()
    return await run_orchestrated_analysis(
        groups=groups,
        console_context="console output",
        repo_path=tmp_path,
        ai_provider="claude",
        ai_model="opus",
    )


async def _stored_error(monkeypatch, tmp_path, job_id: str = "refusal-job"):
    """Run the finalisation ``/analyze`` ends in and return the stored row."""
    from rootcoz import main as rootcoz_main

    groups, failures = _one_group()
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / f"{job_id}.db")
    await storage.init_db()
    await storage.save_result(
        job_id, "", "queued", {"status": "queued", "result": {}, "request_params": {}}
    )
    await rootcoz_main._analyze_failures_or_exit(
        job_id=job_id,
        test_failures=failures,
        console_context="console output",
        artifacts_context="",
        metadata_job_name="job",
        display_name="job",
        repo_path=tmp_path,
        ai_provider="claude",
        ai_model="opus",
        merged=Settings(),
        custom_prompt="",
        server_url="",
        peer_ai_configs=None,
        cloned_repos={},
        auth_header="",
        groups=groups,
        source_result=None,
    )
    return await storage.get_result(job_id)


class TestAnalyzeRefusalReachesTheStoredError:
    """The refusal is a sentence the job records, on both credential sources."""

    async def test_keyless_analysis_maps_the_library_4xx(self, tmp_path, monkeypatch):
        """A keyless refusal maps to the generic sentence and is not retried.

        The text is the genuine two-line httpx message, so the old
        ``$``-anchored ``/sessions'?$`` matcher returned ``False`` here.
        """
        from rootcoz import ai_client

        refused = _refusal(monkeypatch, _HTTPX_KEYLESS)
        analyses, _ = await _orchestrated(tmp_path)
        assert refused.await_count == 1
        expected = ai_client._session_refusal(400, _HTTPX_KEYLESS, "claude", "opus")
        assert analyses[0].analysis.details == expected
        assert "(sidecar HTTP 400)" in analyses[0].analysis.details
        assert "models.json" not in analyses[0].analysis.details
        assert analyses[0].analysis.placeholder is True

    async def test_server_credential_analysis_keeps_the_refusal_sentence(
        self, tmp_path, monkeypatch
    ):
        """A stored server key keeps the sidecar's own limits sentence."""
        refused = _refusal(monkeypatch, _LIBRARY_LIMITS)
        stored = await _stored_error(monkeypatch, tmp_path, "refusal-server")
        assert refused.await_count == 1
        assert stored["result"]["error"] == _LIMITS_SENTENCE
        assert stored["result"]["status"] == "failed"
        assert (
            stored["result"]["failures"][0]["analysis"]["details"] == _LIMITS_SENTENCE
        )
        assert stored["result"]["failures"][0]["analysis"]["placeholder"] is True

    async def test_user_keyed_analysis_keeps_the_refusal_sentence(
        self, tmp_path, monkeypatch
    ):
        """The user-credential path carries the same sentence."""
        from rootcoz import ai_client

        async def refuse_client(*_args, **_kwargs):
            # What _call_user_session returns for a caught SessionCreationRefused.
            return ai_client.AIResult(
                success=False, text=_LIMITS_SENTENCE, error=_LIMITS_SENTENCE
            )

        async def mapped(prompt, **kwargs):
            return await ai_client._call_with_safe_error(
                refuse_client, prompt, redact_key="user-key", **kwargs
            )

        monkeypatch.setattr("rootcoz.engine.core.call_ai_once", mapped)
        stored = await _stored_error(monkeypatch, tmp_path, "refusal-user")
        assert stored["result"]["error"] == _LIMITS_SENTENCE
        assert (
            stored["result"]["failures"][0]["analysis"]["details"] == _LIMITS_SENTENCE
        )

    async def test_refusal_reaches_the_group_placeholder(self, tmp_path, monkeypatch):
        """One refused group carries the sentence; its sibling keeps the generic."""
        from rootcoz import ai_client
        from rootcoz.engine.core import run_orchestrated_analysis
        from rootcoz.models import FailedTest

        _refusal(monkeypatch, _LIBRARY_LIMITS)
        calls = {"n": 0}

        async def mapped(prompt, **kwargs):
            # One concurrency slot below, so the first call is the first group.
            calls["n"] += 1
            if calls["n"] == 1:
                return await ai_client._call_with_safe_error(
                    ai_client._call_ai, prompt, redact_key=None, **kwargs
                )
            return _ai_result(
                success=True,
                text=json.dumps(
                    {
                        "classification": "CODE ISSUE",
                        "pattern": "NEW",
                        "affected_tests": ["test_b"],
                        "details": "real answer",
                    }
                ),
            )

        monkeypatch.setattr("rootcoz.engine.core.call_ai_once", mapped)
        groups = {
            "sig-a": [FailedTest(test_name="test_a", error_message="boom")],
            "sig-b": [FailedTest(test_name="test_b", error_message="bang")],
        }
        analyses, _ = await run_orchestrated_analysis(
            groups=groups,
            console_context="console",
            repo_path=tmp_path,
            ai_provider="claude",
            ai_model="opus",
            max_concurrent_ai_calls=1,
        )
        refused = next(a for a in analyses if a.test_name == "test_a")
        answered = next(a for a in analyses if a.test_name == "test_b")
        assert refused.analysis.details == _LIMITS_SENTENCE
        assert refused.analysis.placeholder is True
        assert answered.analysis.details == "real answer"
        assert answered.analysis.placeholder is False

    async def test_fully_refused_job_is_still_failed(self, tmp_path, monkeypatch):
        """Every group refused is a failed job, not a completed one.

        ``is_failed_analysis`` reads the typed flag, so a refusal placeholder is
        never mistaken for an analysis; the classified-marker case pins the early
        return that keeps a real analysis out of that set.
        """
        from rootcoz.models import (
            AnalysisDetail,
            FailureAnalysis,
            is_failed_analysis,
        )

        _refusal(monkeypatch, _LIBRARY_LIMITS)
        stored = await _stored_error(monkeypatch, tmp_path, "refusal-all")
        assert stored["result"]["status"] == "failed"
        assert stored["result"]["error"] == _LIMITS_SENTENCE
        assert stored["result"]["failed_analysis_groups"] >= 1
        assert stored["result"]["failures"][0]["analysis"]["placeholder"] is True

        classified = FailureAnalysis(
            test_name="t",
            error="boom",
            analysis=AnalysisDetail(
                classification="CODE ISSUE", details=_LIMITS_SENTENCE
            ),
        )
        assert not is_failed_analysis(classified)
        assert classified.analysis.placeholder is False

    async def test_child_build_refusal_is_not_generic(self, tmp_path, monkeypatch):
        """All three child placeholders carry the sentence, not "check server logs"."""
        from rootcoz.models import FailedTest
        from rootcoz.sources import jenkins_source
        from rootcoz.sources.base import run_console_only_analysis

        failure = FailedTest(test_name="test_a", error_message="boom")

        async def refuse(*_args, **_kwargs):
            raise RuntimeError(_LIMITS_SENTENCE)

        # The jenkins module holds its own reference to the analyzer.
        monkeypatch.setattr(jenkins_source, "analyze_failure_group", refuse)
        monkeypatch.setattr("rootcoz.engine.core.analyze_failure_group", refuse)
        analyses, _unique, _failed = await jenkins_source._analyze_grouped_failures(
            [failure],
            console_context="console",
            repo_path=tmp_path,
            artifacts_context="",
        )
        assert analyses[0].analysis.details == _LIMITS_SENTENCE
        assert analyses[0].analysis.placeholder is True

        bundles = jenkins_source._normalize_child_results(
            [("child", 1)], [RuntimeError(_LIMITS_SENTENCE)]
        )
        assert bundles[0].analysis.note == _LIMITS_SENTENCE

        success, rows, error_text = await run_console_only_analysis(
            test_name="child#1",
            console_context="console",
            artifacts_context="",
            repo_path=tmp_path,
            ai_provider="claude",
            ai_model="opus",
            ai_call_timeout=None,
            custom_prompt="",
            server_url="",
            job_id="job",
            additional_repos=None,
            auth_header="",
        )
        assert success is False
        assert error_text == _LIMITS_SENTENCE
        assert rows[0].analysis.details == _LIMITS_SENTENCE

    async def test_refusal_is_identical_when_stored_and_returned(
        self, tmp_path, monkeypatch
    ):
        """One 401 cause: the stored error, the group detail and the chat 422 agree."""
        from rootcoz import ai_client

        body = "upstream rejected the key"
        _refusal(monkeypatch, f"Sidecar session creation failed (HTTP 401): {body}")
        stored = await _stored_error(monkeypatch, tmp_path, "refusal-401")
        sentence = stored["result"]["error"]
        assert sentence == stored["result"]["failures"][0]["analysis"]["details"]
        assert sentence == ai_client._session_refusal(401, body, "claude", "opus")
        assert (
            ai_client.format_chat_ai_user_error(
                f"Sidecar session creation failed (HTTP 401): {body}",
                ai_provider="claude",
                ai_model="opus",
            )
            == sentence
        )
        for word in ("your", "re-enter", "rotate", "the server's"):
            assert word not in sentence
