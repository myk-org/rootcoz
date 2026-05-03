"""Tests for Report Portal settings in config.py."""

import os
from unittest.mock import patch

from jenkins_job_insight.config import Settings
from tests.conftest import build_test_env as _build_env


class TestReportPortalSettings:
    """Test Report Portal settings fields and the reportportal_enabled property."""

    def test_rp_disabled_by_default(self):
        """RP is disabled when no RP env vars are set."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled

    def test_rp_enabled_when_all_configured(self):
        """RP is enabled when url, token, and project are set."""
        env = _build_env(
            REPORTPORTAL_URL="http://rp.example.com",
            REPORTPORTAL_API_TOKEN="rp-token",  # pragma: allowlist secret
            REPORTPORTAL_PROJECT="my-project",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.reportportal_enabled

    def test_rp_disabled_when_url_missing(self):
        env = _build_env(
            REPORTPORTAL_API_TOKEN="rp-token",  # pragma: allowlist secret
            REPORTPORTAL_PROJECT="my-project",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled

    def test_rp_disabled_when_token_missing(self):
        env = _build_env(
            REPORTPORTAL_URL="http://rp.example.com",
            REPORTPORTAL_PROJECT="my-project",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled

    def test_rp_disabled_when_project_missing(self):
        env = _build_env(
            REPORTPORTAL_URL="http://rp.example.com",
            REPORTPORTAL_API_TOKEN="rp-token",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled

    def test_rp_explicitly_disabled_overrides_config(self):
        """When enable_reportportal=False, RP is disabled even with full config."""
        env = _build_env(
            REPORTPORTAL_URL="http://rp.example.com",
            REPORTPORTAL_API_TOKEN="rp-token",  # pragma: allowlist secret
            REPORTPORTAL_PROJECT="my-project",
            ENABLE_REPORTPORTAL="false",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled

    @patch("jenkins_job_insight.config.logger")
    def test_rp_warns_when_explicitly_enabled_but_url_missing(self, mock_logger):
        """When enable_reportportal=True but url is missing, warn."""
        env = _build_env(
            ENABLE_REPORTPORTAL="true",
            REPORTPORTAL_API_TOKEN="rp-token",  # pragma: allowlist secret
            REPORTPORTAL_PROJECT="my-project",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled
        mock_logger.warning.assert_called()
        warn_msg = mock_logger.warning.call_args[0][0]
        assert "REPORTPORTAL_URL" in warn_msg

    @patch("jenkins_job_insight.config.logger")
    def test_rp_warns_when_explicitly_enabled_but_token_missing(self, mock_logger):
        env = _build_env(
            ENABLE_REPORTPORTAL="true",
            REPORTPORTAL_URL="http://rp.example.com",
            REPORTPORTAL_PROJECT="my-project",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled
        mock_logger.warning.assert_called()
        warn_msg = mock_logger.warning.call_args[0][0]
        assert "REPORTPORTAL_API_TOKEN" in warn_msg

    @patch("jenkins_job_insight.config.logger")
    def test_rp_warns_when_explicitly_enabled_but_project_missing(self, mock_logger):
        env = _build_env(
            ENABLE_REPORTPORTAL="true",
            REPORTPORTAL_URL="http://rp.example.com",
            REPORTPORTAL_API_TOKEN="rp-token",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.reportportal_enabled
        mock_logger.warning.assert_called()
        warn_msg = mock_logger.warning.call_args[0][0]
        assert "REPORTPORTAL_PROJECT" in warn_msg
