"""Tests for configuration settings."""

import logging
import os
from dataclasses import replace
from typing import ClassVar
from unittest.mock import patch

import pytest
from pydantic import SecretStr, ValidationError

from rootcoz.auth import identity as auth_identity
from rootcoz.auth.sso.base import AuthError, ClaimSet
from rootcoz.config import (
    Settings,
    browser_provider_without_base_url,
    clear_settings_cache,
    duplicate_additional_repo_names,
    get_settings,
    jira_availability,
    jira_method_missing_fields,
    jira_unavailable_reason,
    normalize_domain_list,
    parse_additional_repos,
    parse_peer_configs,
    parse_repo_ref,
    safe_parse_repo_ref,
    update_db_settings_cache,
    validate_tests_repo_url,
)
from rootcoz.jira import resolve_jira_connection
from tests.conftest import build_test_env as _build_env
from tests.conftest import capture_main_logs


class TestSettings:
    """Tests for the Settings class."""

    def test_settings_loads_from_env_vars(self, mock_env_vars: dict[str, str]) -> None:
        """Test that Settings loads required fields from environment variables."""
        settings = Settings()
        assert settings.jenkins_url == mock_env_vars["JENKINS_URL"]
        assert settings.jenkins_user == mock_env_vars["JENKINS_USER"]
        assert settings.jenkins_password == mock_env_vars["JENKINS_PASSWORD"]

    def test_settings_optional_fields_default_to_none(self) -> None:
        """Test that optional fields default to None when not set."""
        # Use clear=True to ensure no existing env vars pollute the test
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert settings.tests_repo_url is None

    @pytest.mark.parametrize(
        "field",
        ["jenkins_url", "jenkins_user", "jenkins_password"],
    )
    def test_jenkins_fields_default_to_empty_string(self, field: str) -> None:
        """Test that Jenkins fields default to empty string when env vars are missing."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
            assert getattr(settings, field) == ""

    def test_settings_extra_fields_ignored(self, mock_env_vars: dict[str, str]) -> None:
        """Test that extra environment variables are ignored."""
        with patch.dict(os.environ, {"UNKNOWN_FIELD": "value"}, clear=False):
            settings = Settings()
            assert not hasattr(settings, "unknown_field")

    def test_settings_loads_tests_repo_url(self) -> None:
        """Test that tests_repo_url is loaded from environment variable."""
        with patch.dict(
            os.environ,
            _build_env(TESTS_REPO_URL="https://github.com/org/test-repo"),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert settings.tests_repo_url == "https://github.com/org/test-repo"


class TestGetSettings:
    """Tests for the get_settings function."""

    def test_get_settings_returns_settings_instance(
        self, mock_env_vars: dict[str, str]
    ) -> None:
        """Test that get_settings returns a Settings instance."""
        clear_settings_cache()
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_get_settings_cached(self, mock_env_vars: dict[str, str]) -> None:
        """Test that get_settings returns cached instance."""
        clear_settings_cache()
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_build_started_before_a_clear_is_not_published(
        self, mock_env_vars: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build in flight across a clear never lands in the cache (#529)."""
        import threading

        from rootcoz import config

        started = threading.Event()
        release = threading.Event()
        real_build = config._build_settings

        def paused_build() -> Settings:
            # Build from the current env, then hold the result before publish.
            completed = real_build()
            started.set()
            assert release.wait(10)
            return completed

        monkeypatch.setattr(config, "_build_settings", paused_build)
        monkeypatch.setenv("JENKINS_USER", "before")
        clear_settings_cache()

        built: list[Settings] = []
        builder = threading.Thread(target=lambda: built.append(get_settings()))
        builder.start()
        assert started.wait(10)

        # The admin save #529 describes: change the value, clear, mid-build.
        monkeypatch.setenv("JENKINS_USER", "after")
        clear_settings_cache()
        release.set()
        builder.join(10)

        assert not builder.is_alive()
        assert built[0].jenkins_user == "before"
        # Its pre-clear result must not be what the process serves next.
        assert get_settings().jenkins_user == "after"

    def test_build_hold_seam_is_inert_by_default(
        self, mock_env_vars: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset or 0 ⇒ no sleep at all and the cache still memoises (#529 seam)."""
        from rootcoz import config

        slept: list[float] = []
        monkeypatch.setattr(config.time, "sleep", slept.append)
        for value in (None, "0"):
            if value is None:
                monkeypatch.delenv("ROOTCOZ_SETTINGS_BUILD_HOLD_MS", raising=False)
            else:
                monkeypatch.setenv("ROOTCOZ_SETTINGS_BUILD_HOLD_MS", value)
            clear_settings_cache()
            settings = get_settings()
            assert get_settings() is settings
            assert slept == []

    def test_build_hold_seam_delays_the_publish(
        self, mock_env_vars: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A set hold is read per call and slept once, before the publish."""
        from rootcoz import config

        slept: list[float] = []

        def hold(seconds: float) -> None:
            # The hold must sit between the build and the publish: publishing
            # first would close the window the seam exists to open while a
            # sleep-only assertion above stayed green.
            assert config.__dict__["_cached_settings"] is None
            slept.append(seconds)

        monkeypatch.setattr(config.time, "sleep", hold)
        monkeypatch.setenv("ROOTCOZ_SETTINGS_BUILD_HOLD_MS", "1500")
        clear_settings_cache()
        settings = get_settings()
        # The documented delay, not a wall-clock measurement: the cached read
        # must not hold again, and a fresh build after the env changes must.
        assert slept == [1.5]
        assert get_settings() is settings
        monkeypatch.setenv("ROOTCOZ_SETTINGS_BUILD_HOLD_MS", "250")
        clear_settings_cache()
        get_settings()
        assert slept == [1.5, 0.25]


class TestJiraSettings:
    """Tests for Jira configuration fields."""

    def test_jira_disabled_by_default(self) -> None:
        """Jira is disabled when no Jira env vars are set."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert not settings.jira_enabled
            assert settings.jira_cloud_url == ""
            assert settings.jira_server_url == ""

    def test_jira_enabled_with_cloud_auth(self) -> None:
        """Jira is enabled with URL + selected basic method + project key."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_EMAIL="user@example.com",
                JIRA_API_TOKEN="token-123",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            update_db_settings_cache({"jira_auth_mode": "basic"})
            assert get_settings().jira_enabled

    def test_jira_enabled_with_server_pat(self) -> None:
        """Jira is enabled with URL + selected pat method + project key."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira-server.example.com",
                JIRA_PAT="pat-token-456",
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            update_db_settings_cache({"jira_auth_mode": "pat"})
            assert get_settings().jira_enabled

    def test_jira_disabled_without_url(self) -> None:
        """Jira is disabled when URL is missing even with credentials."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_EMAIL="user@example.com",
                JIRA_API_TOKEN="token-123",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert not settings.jira_enabled

    def test_jira_disabled_without_project_key(self) -> None:
        """Jira is disabled when credentials are present but project key is missing."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_PAT="pat-token-456",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert not settings.jira_enabled

    def test_jira_disabled_without_credentials(self) -> None:
        """Jira is disabled when URL is set but no credentials."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert not settings.jira_enabled

    def test_jira_default_values(self) -> None:
        """Test default values for optional Jira fields."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert settings.jira_oauth_ssl_verify is True
            assert settings.jira_cloud_ssl_verify is True
            assert settings.jira_server_ssl_verify is True
            assert settings.jira_max_results == 5
            assert settings.jira_project_key is None

    def test_enable_jira_explicit_false_disables(self) -> None:
        """Jira is disabled when ENABLE_JIRA is explicitly False."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_PAT="pat-token-456",
                JIRA_PROJECT_KEY="TEST",
                ENABLE_JIRA="false",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert not settings.jira_enabled

    def test_jira_custom_values(self) -> None:
        """Deprecated JIRA_URL/JIRA_SSL_VERIFY aliases feed every per-method field."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_EMAIL="user@example.com",
                JIRA_API_TOKEN="token",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="MYPROJ",
                JIRA_SSL_VERIFY="false",
                JIRA_MAX_RESULTS="10",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert settings.jira_project_key == "MYPROJ"
            assert settings.jira_cloud_url == "https://jira.example.com"
            assert settings.jira_server_url == "https://jira.example.com"
            assert settings.jira_oauth_ssl_verify is False
            assert settings.jira_cloud_ssl_verify is False
            assert settings.jira_server_ssl_verify is False
            assert settings.jira_max_results == 10

    def test_jira_url_alias_feeds_both_per_method_urls(self) -> None:
        """JIRA_URL feeds both URL fields; canonical names win once set."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://alias.example.com",
                JIRA_CLOUD_URL="https://cloud.example.com",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            # Canonical JIRA_CLOUD_URL wins; the alias fills the unset server URL
            assert settings.jira_cloud_url == "https://cloud.example.com"
            assert settings.jira_server_url == "https://alias.example.com"

    def test_jira_ssl_verify_alias_feeds_all_three_flags(self) -> None:
        """JIRA_SSL_VERIFY feeds all three verify flags; canonicals win."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_SSL_VERIFY="false",
                JIRA_SERVER_SSL_VERIFY="true",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert settings.jira_oauth_ssl_verify is False
            assert settings.jira_cloud_ssl_verify is False
            # Canonical JIRA_SERVER_SSL_VERIFY wins over the alias
            assert settings.jira_server_ssl_verify is True

    def test_jira_url_alias_only_in_dotenv_feeds_both_url_fields(
        self, tmp_path, monkeypatch
    ) -> None:
        """JIRA_URL present ONLY in .env (no canonical fields) feeds both URLs.

        The dotenv source surfaces unknown keys that ``extra='ignore'``
        later drops — the alias mapping must read them from there too,
        not only from os.environ.
        """
        (tmp_path / ".env").write_text("JIRA_URL=https://jira-dotenv.example.com\n")
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings()
            assert settings.jira_cloud_url == "https://jira-dotenv.example.com"
            assert settings.jira_server_url == "https://jira-dotenv.example.com"

    def test_jira_ssl_verify_alias_only_in_dotenv_feeds_all_flags(
        self, tmp_path, monkeypatch
    ) -> None:
        """JIRA_SSL_VERIFY present ONLY in .env feeds all three verify flags."""
        (tmp_path / ".env").write_text("JIRA_SSL_VERIFY=false\n")
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings()
            assert settings.jira_oauth_ssl_verify is False
            assert settings.jira_cloud_ssl_verify is False
            assert settings.jira_server_ssl_verify is False

    def test_dotenv_canonical_url_wins_over_dotenv_alias(
        self, tmp_path, monkeypatch
    ) -> None:
        """Canonical names win over aliases within the same .env file."""
        (tmp_path / ".env").write_text(
            "JIRA_URL=https://alias-dotenv.example.com\n"
            "JIRA_CLOUD_URL=https://canonical-dotenv.example.com\n"
        )
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings()
            assert settings.jira_cloud_url == "https://canonical-dotenv.example.com"
            assert settings.jira_server_url == "https://alias-dotenv.example.com"


class TestJiraUnavailableReason:
    """Tests for jira_unavailable_reason — one reason per refusal.

    The connection reason makes NO claim about the deployment: it also runs on
    per-request (merged) settings, where the caller's own override can be what
    removed the connection, and the string is persisted and replayed on
    re-analysis (#536).
    """

    def _settings(self, **env: str) -> Settings:
        with patch.dict(os.environ, _build_env(**env), clear=True):
            settings = Settings(_env_file=None)
        return settings

    def test_empty_when_configured(self) -> None:
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_PAT="pat-token",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None, jira_auth_mode="pat")
        assert jira_unavailable_reason(settings) == ""
        assert settings.jira_enabled is True

    def test_names_explicitly_disabled(self) -> None:
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_PAT="pat-token",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
                ENABLE_JIRA="false",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None, jira_auth_mode="pat")
        assert "ENABLE_JIRA" in jira_unavailable_reason(settings)
        assert settings.jira_enabled is False

    def test_names_missing_auth_mode(self) -> None:
        settings = self._settings()
        reason = jira_unavailable_reason(settings)
        # Cause-free and deployment-free: the field detail is availability's
        # DEBUG line, never this persisted string.
        assert reason
        assert "auth method" not in reason
        assert "selected" not in reason
        assert "Server Settings" not in reason
        assert settings.jira_enabled is False

    def test_names_missing_method_field(self) -> None:
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_PAT="pat-token",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None, jira_auth_mode="pat")
        reason = jira_unavailable_reason(settings)
        # The missing locator is not named here (it would claim the deployment
        # is unconfigured even on a configured one) — availability's DEBUG line
        # names it.
        assert "jira_server_url" not in reason
        assert "jira_server_url is unset" in jira_availability(settings)[1]
        assert settings.jira_enabled is False

    def test_names_missing_project_key(self) -> None:
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_PAT="pat-token",  # pragma: allowlist secret
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None, jira_auth_mode="pat")
        assert "JIRA_PROJECT_KEY" in jira_unavailable_reason(settings)
        assert settings.jira_enabled is False

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"ENABLE_JIRA": "false"},
            {"JIRA_URL": "https://jira.example.com", "JIRA_PROJECT_KEY": "T"},
        ],
    )
    def test_jira_enabled_matches_reason(self, env: dict[str, str]) -> None:
        """The property and the reason are one source of truth."""
        settings = self._settings(**env)
        assert settings.jira_enabled is (jira_unavailable_reason(settings) == "")


class TestJiraAuthMode:
    """Tests for the explicit jira_auth_mode setting."""

    def test_env_var_is_ignored(self) -> None:
        """JIRA_AUTH_MODE env has no effect — selection is Server Settings only."""
        with patch.dict(os.environ, _build_env(JIRA_AUTH_MODE=" Basic "), clear=True):
            settings = Settings(_env_file=None)
            assert settings.jira_auth_mode is None
            assert not settings.jira_enabled

    def test_deprecated_env_alias_feeds_renamed_setting(self) -> None:
        """JIRA_OAUTH_ACCESS_TOKEN (deprecated) feeds jira_oauth_client_secret."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_OAUTH_ACCESS_TOKEN="ATOA-old-secret"
            ),  # pragma: allowlist secret
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert (
                settings.jira_oauth_client_secret.get_secret_value()
                == "ATOA-old-secret"  # pragma: allowlist secret
            )

    def test_deprecated_oauth_alias_only_in_dotenv_feeds_secret(
        self, tmp_path, monkeypatch
    ) -> None:
        """JIRA_OAUTH_ACCESS_TOKEN present ONLY in .env feeds the renamed secret."""
        (tmp_path / ".env").write_text(
            "JIRA_OAUTH_ACCESS_TOKEN=ATOA-dotenv-secret\n"  # pragma: allowlist secret
        )
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings()
            assert (
                settings.jira_oauth_client_secret.get_secret_value()
                == "ATOA-dotenv-secret"  # pragma: allowlist secret
            )

    def test_canonical_env_wins_over_deprecated_alias(self) -> None:
        """JIRA_OAUTH_CLIENT_SECRET beats the old JIRA_OAUTH_ACCESS_TOKEN."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_OAUTH_CLIENT_SECRET="ATOA-new-secret",  # pragma: allowlist secret
                JIRA_OAUTH_ACCESS_TOKEN="ATOA-old-secret",  # pragma: allowlist secret
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert (
                settings.jira_oauth_client_secret.get_secret_value()
                == "ATOA-new-secret"  # pragma: allowlist secret
            )

    def test_db_setting_selects_mode(self) -> None:
        """The resolver reads the selected method from the DB settings cache."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira-server.example.com",
                JIRA_PAT="pat-token",
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            update_db_settings_cache({"jira_auth_mode": "pat"})
            settings = get_settings()
            assert settings.jira_auth_mode == "pat"
            assert settings.jira_enabled
            assert resolve_jira_connection(settings).mode == "server"

    def test_invalid_value_rejected(self) -> None:
        """Invalid methods fail validation with a clear error."""
        with pytest.raises(ValueError, match="jira_auth_mode"):
            Settings(_env_file=None, jira_auth_mode="kerberos")

    def test_unset_defaults_to_none(self) -> None:
        """No env var means no method selected (Jira disabled)."""
        with patch.dict(os.environ, _build_env(), clear=True):
            assert Settings(_env_file=None).jira_auth_mode is None

    def test_unset_mode_disables_jira_even_with_credentials(self) -> None:
        """Unset mode: no auto-detection — full credentials are ignored."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://myorg.atlassian.net",
                JIRA_EMAIL="user@example.com",
                JIRA_API_TOKEN="api-token",  # pragma: allowlist secret
                JIRA_PAT="pat-token",  # pragma: allowlist secret
                JIRA_OAUTH_CLIENT_SECRET="ATOA-token",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert not settings.jira_enabled
            assert resolve_jira_connection(settings).mode == "none"

    def test_missing_fields_unset_mode(self) -> None:
        """Unset mode never reports missing fields (Jira is disabled instead)."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert jira_method_missing_fields(settings) == []

    def test_missing_fields_per_method(self) -> None:
        """Each method names exactly its own missing fields."""
        cases = [
            (
                "oauth",
                {},
                [
                    "jira_oauth_client_secret",
                    "atlassian_oauth_client_id",
                    "atlassian_oauth_cloud_id",
                ],
            ),
            (
                "oauth",
                {
                    "jira_oauth_client_secret": SecretStr("tok")
                },  # pragma: allowlist secret
                ["atlassian_oauth_client_id", "atlassian_oauth_cloud_id"],
            ),
            (
                "oauth",
                {
                    "atlassian_oauth_client_id": "cid",
                    "atlassian_oauth_cloud_id": "cloud-id",
                },
                ["jira_oauth_client_secret"],
            ),
            ("basic", {}, ["jira_email", "jira_api_token", "jira_cloud_url"]),
            (
                "basic",
                {"jira_email": "user@example.com"},
                ["jira_api_token", "jira_cloud_url"],
            ),
            ("pat", {}, ["jira_pat", "jira_server_url"]),
            ("atst", {}, ["jira_atst_token", "atlassian_oauth_cloud_id"]),
            (
                "atst",
                {"jira_atst_token": SecretStr("ATST-tok")},  # pragma: allowlist secret
                ["atlassian_oauth_cloud_id"],
            ),
        ]
        for mode, extra, expected in cases:
            with patch.dict(os.environ, _build_env(), clear=True):
                settings = Settings(jira_auth_mode=mode, **extra)
            assert jira_method_missing_fields(settings) == expected, mode

    def test_missing_fields_complete(self) -> None:
        """A fully configured method reports no missing fields."""
        settings = Settings(
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_api_token=SecretStr("tok"),  # pragma: allowlist secret
            jira_cloud_url="https://jira.example.com",
            jira_pat=SecretStr("other"),  # pragma: allowlist secret
        )
        assert jira_method_missing_fields(settings) == []

    def test_explicit_oauth_requires_both_credential_halves(self) -> None:
        """Explicit oauth needs the client secret AND the client ID."""
        settings = Settings(
            atlassian_oauth_cloud_id="cloud-id",
            jira_auth_mode="oauth",
            jira_oauth_client_secret=SecretStr(
                "ATOA-test-secret"
            ),  # pragma: allowlist secret
            atlassian_oauth_client_id="test-client-id",
        )
        connection = resolve_jira_connection(settings)
        assert connection.mode == "sa"
        # The secret is carried for minting, never sent as the Bearer
        assert "Authorization" not in connection.headers
        sa_secret = connection.sa_client_secret
        assert sa_secret == "ATOA-test-secret"  # pragma: allowlist secret

        half = settings.model_copy(update={"atlassian_oauth_client_id": None})
        assert resolve_jira_connection(half).mode == "none"

    def test_explicit_basic_no_pat_fallback(self) -> None:
        """Explicit basic uses only email + api token — no PAT fallback."""
        settings = Settings(
            jira_cloud_url="https://myorg.atlassian.net",
            jira_auth_mode="basic",
            jira_email="user@example.com",
            jira_pat=SecretStr("pat-token"),  # pragma: allowlist secret
        )
        connection = resolve_jira_connection(settings)
        assert connection.mode == "none"

    def test_explicit_pat_ignores_sa_and_api_token(self) -> None:
        """Explicit pat uses only jira_pat — SA and api tokens are ignored."""
        settings = Settings(
            jira_server_url="https://jira.example.com",
            jira_auth_mode="pat",
            jira_oauth_client_secret=SecretStr(
                "ATOA-token"
            ),  # pragma: allowlist secret
            jira_api_token=SecretStr("api-token"),  # pragma: allowlist secret
            jira_pat=SecretStr("my-pat"),  # pragma: allowlist secret
        )
        connection = resolve_jira_connection(settings)
        assert connection.mode == "server"
        assert connection.headers["Authorization"] == "Bearer my-pat"

    def test_jira_enabled_with_explicit_mode_missing_fields(self) -> None:
        """jira_enabled is False when the selected method lacks fields."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_API_TOKEN="token-123",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(
                _env_file=None, jira_auth_mode="basic"
            )  # missing jira_email
            assert not settings.jira_enabled

    def test_jira_enabled_with_explicit_mode_complete(self) -> None:
        """jira_enabled is True when the selected method is complete."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_PAT="pat-token",  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None, jira_auth_mode="pat")
            assert settings.jira_enabled

    def test_atst_mode_accepted_and_enabled(self) -> None:
        """'atst' is a valid jira_auth_mode and enables Jira when complete."""
        with patch.dict(
            os.environ,
            _build_env(JIRA_PROJECT_KEY="TEST"),
            clear=True,
        ):
            settings = Settings(
                _env_file=None,
                jira_auth_mode="atst",
                jira_atst_token=SecretStr("ATST-tok"),  # pragma: allowlist secret
                atlassian_oauth_cloud_id="cloud-id",
            )
            assert settings.jira_auth_mode == "atst"
            assert settings.jira_enabled
            assert resolve_jira_connection(settings).mode == "atst"


class TestGitHubIssuesSettings:
    """Tests for GitHub issue creation configuration."""

    def test_github_issues_disabled_by_default(self) -> None:
        """GitHub issues disabled when no GitHub env vars are set."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert not settings.github_issues_enabled

    def test_github_issues_enabled_with_token_and_repo(self) -> None:
        """GitHub issues enabled when both GITHUB_TOKEN and TESTS_REPO_URL are set."""
        env = _build_env(
            GITHUB_TOKEN="ghp_test123",  # pragma: allowlist secret
            TESTS_REPO_URL="https://github.com/org/repo",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.github_issues_enabled

    def test_github_issues_disabled_without_token(self) -> None:
        """GitHub issues disabled when GITHUB_TOKEN is missing."""
        env = _build_env(TESTS_REPO_URL="https://github.com/org/repo")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.github_issues_enabled

    def test_github_issues_disabled_without_repo_url(self) -> None:
        """GitHub issues disabled when TESTS_REPO_URL is missing."""
        env = _build_env(
            GITHUB_TOKEN="ghp_test123",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.github_issues_enabled

    def test_github_issues_explicit_false_disables(self) -> None:
        """GitHub issues disabled when ENABLE_GITHUB_ISSUES is explicitly False."""
        env = _build_env(
            GITHUB_TOKEN="ghp_test123",  # pragma: allowlist secret
            TESTS_REPO_URL="https://github.com/org/repo",
            ENABLE_GITHUB_ISSUES="false",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.github_issues_enabled

    def test_github_issues_explicit_true_without_config(self) -> None:
        """GitHub issues disabled when ENABLE_GITHUB_ISSUES is True but config missing."""
        env = _build_env(ENABLE_GITHUB_ISSUES="true")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.github_issues_enabled


# Shared bracketed-model literals used across multiple peer-config tests
_OPENAI_GPT54_BRACKETED = "gpt-5.4[context=272k,reasoning=medium,fast=false]"
_CLAUDE_SONNET_BRACKETED = "claude-sonnet-4-6[thinking=true,context=200k,effort=medium]"
_GEMINI_PRO_BRACKETED = "gemini-2.5-pro[fast=true]"


class TestParsePeerConfigs:
    """Tests for the parse_peer_configs helper."""

    def test_parse_peer_configs_valid(self) -> None:
        """Valid 'provider:model' pairs produce correct dicts."""
        result = parse_peer_configs("claude:opus,gemini:pro")
        assert result == [
            {"ai_provider": "claude", "ai_model": "opus"},
            {"ai_provider": "gemini", "ai_model": "pro"},
        ]

    def test_parse_peer_configs_empty(self) -> None:
        """Empty string returns empty list."""
        assert parse_peer_configs("") == []

    def test_parse_peer_configs_whitespace(self) -> None:
        """Whitespace-only string returns empty list."""
        assert parse_peer_configs("  ") == []

    def test_parse_peer_configs_invalid_no_colon(self) -> None:
        """Entry without colon raises ValueError."""
        with pytest.raises(ValueError, match="expected 'provider:model'"):
            parse_peer_configs("claude-opus")

    def test_parse_peer_configs_empty_provider(self) -> None:
        """':model' raises ValueError for empty provider."""
        with pytest.raises(ValueError, match="Empty provider"):
            parse_peer_configs(":model")

    def test_parse_peer_configs_empty_model(self) -> None:
        """'claude:' raises ValueError for empty model."""
        with pytest.raises(ValueError, match="Empty model"):
            parse_peer_configs("claude:")

    def test_parse_peer_configs_accepts_catalog_provider_ids(self) -> None:
        """Catalog validation is deferred to the async sidecar resolution path."""
        assert parse_peer_configs(
            "openai:gpt-5.4,openrouter:anthropic/claude-sonnet-4-6:thinking"
        ) == [
            {"ai_provider": "openai", "ai_model": "gpt-5.4"},
            {
                "ai_provider": "openrouter",
                "ai_model": "anthropic/claude-sonnet-4-6:thinking",
            },
        ]

    def test_parse_peer_configs_trailing_comma(self) -> None:
        """Trailing comma results in empty entry ValueError."""
        with pytest.raises(ValueError, match="Empty entry"):
            parse_peer_configs("claude:opus,")

    def test_parse_peer_configs_model_with_brackets_no_commas(self) -> None:
        """Model name with brackets but no internal commas parses correctly."""
        result = parse_peer_configs(f"gemini:{_GEMINI_PRO_BRACKETED}")
        assert result == [{"ai_provider": "gemini", "ai_model": _GEMINI_PRO_BRACKETED}]

    def test_parse_peer_configs_model_with_brackets_and_commas(self) -> None:
        """Model name with commas inside brackets is not split."""
        result = parse_peer_configs(
            f"gemini:{_GEMINI_PRO_BRACKETED},openai:{_OPENAI_GPT54_BRACKETED}"
        )
        assert result == [
            {"ai_provider": "gemini", "ai_model": _GEMINI_PRO_BRACKETED},
            {
                "ai_provider": "openai",
                "ai_model": _OPENAI_GPT54_BRACKETED,
            },
        ]

    def test_parse_peer_configs_mixed_bracket_and_plain(self) -> None:
        """Mix of plain model and bracketed model with commas."""
        result = parse_peer_configs(
            f"claude:claude-opus-4-6-1m,openai:{_OPENAI_GPT54_BRACKETED}"
        )
        assert result == [
            {"ai_provider": "claude", "ai_model": "claude-opus-4-6-1m"},
            {
                "ai_provider": "openai",
                "ai_model": _OPENAI_GPT54_BRACKETED,
            },
        ]

    def test_parse_peer_configs_multiple_bracketed_models(self) -> None:
        """Multiple models with commas inside brackets."""
        result = parse_peer_configs(
            f"openai:{_OPENAI_GPT54_BRACKETED},claude:{_CLAUDE_SONNET_BRACKETED}"
        )
        assert result == [
            {
                "ai_provider": "openai",
                "ai_model": _OPENAI_GPT54_BRACKETED,
            },
            {
                "ai_provider": "claude",
                "ai_model": _CLAUDE_SONNET_BRACKETED,
            },
        ]


class TestParseAdditionalRepos:
    """Tests for parse_additional_repos function."""

    def test_empty_string_returns_empty(self) -> None:
        assert parse_additional_repos("") == []

    def test_whitespace_returns_empty(self) -> None:
        assert parse_additional_repos("   ") == []

    def test_single_repo(self) -> None:
        result = parse_additional_repos("infra:https://github.com/org/infra")
        assert result == [
            {"name": "infra", "url": "https://github.com/org/infra", "ref": ""}
        ]

    def test_multiple_repos(self) -> None:
        result = parse_additional_repos(
            "infra:https://github.com/org/infra,product:https://github.com/org/product"
        )
        assert len(result) == 2
        assert result[0] == {
            "name": "infra",
            "url": "https://github.com/org/infra",
            "ref": "",
        }
        assert result[1] == {
            "name": "product",
            "url": "https://github.com/org/product",
            "ref": "",
        }

    def test_thirteen_repos(self) -> None:
        raw = ",".join(f"repo{i}:https://example.com/repo{i}" for i in range(13))
        result = parse_additional_repos(raw)
        assert [repo["name"] for repo in result] == [f"repo{i}" for i in range(13)]
        assert result[-1]["url"] == "https://example.com/repo12"

    def test_whitespace_trimmed(self) -> None:
        result = parse_additional_repos("  infra : https://github.com/org/infra  ")
        assert result == [
            {"name": "infra", "url": "https://github.com/org/infra", "ref": ""}
        ]

    def test_repo_with_branch_ref(self) -> None:
        """Repo URL with ':branch' extracts ref into result dict."""
        result = parse_additional_repos("infra:https://github.com/org/infra:develop")
        assert result == [
            {"name": "infra", "url": "https://github.com/org/infra", "ref": "develop"}
        ]

    def test_repo_with_port_and_ref(self) -> None:
        """Repo URL with port AND ref correctly extracts both."""
        result = parse_additional_repos("infra:https://gitlab:8443/org/infra:main")
        assert result == [
            {"name": "infra", "url": "https://gitlab:8443/org/infra", "ref": "main"}
        ]

    def test_empty_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty entry"):
            parse_additional_repos(
                "infra:https://github.com/org/infra,,product:https://github.com/org/product"
            )

    def test_missing_colon_raises(self) -> None:
        with pytest.raises(ValueError, match="expected 'name:url'"):
            parse_additional_repos("just-a-name-no-url")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty name"):
            parse_additional_repos(":https://github.com/org/infra")

    def test_empty_url_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty URL"):
            parse_additional_repos("infra:")

    def test_duplicate_names_rejected(self) -> None:
        """Duplicate names in additional repos env var raise ValueError."""
        with pytest.raises(ValueError, match="Duplicate"):
            parse_additional_repos(
                "infra:https://github.com/org/a,infra:https://github.com/org/b"
            )

    def test_duplicate_names_found_without_a_full_parse(self) -> None:
        """The save-time check still sees a collision next to a malformed entry."""
        assert duplicate_additional_repo_names(
            "infra:https://github.com/org/a,infra:https://github.com/org/b,"
        ) == ["infra"]
        assert duplicate_additional_repo_names("infra:a,b:b") == []

    def test_repo_with_token(self) -> None:
        """Repo URL with @token extracts token into result dict."""
        result = parse_additional_repos(
            "infra:https://github.com/org/infra@ghp_secret123"
        )
        assert result == [
            {
                "name": "infra",
                "url": "https://github.com/org/infra",
                "ref": "",
                "token": "ghp_secret123",  # pragma: allowlist secret
            }
        ]

    def test_repo_with_ref_and_token(self) -> None:
        """Repo URL with :ref and @token extracts both."""
        result = parse_additional_repos(
            "infra:https://github.com/org/infra:develop@ghp_secret123"
        )
        assert result == [
            {
                "name": "infra",
                "url": "https://github.com/org/infra",
                "ref": "develop",
                "token": "ghp_secret123",  # pragma: allowlist secret
            }
        ]

    def test_repo_without_token_has_no_token_key(self) -> None:
        """Repo without @token does not include token key in result."""
        result = parse_additional_repos("infra:https://github.com/org/infra")
        assert "token" not in result[0]

    def test_multiple_repos_mixed_tokens(self) -> None:
        """Some repos with token, some without."""
        result = parse_additional_repos(
            "infra:https://github.com/org/infra@tok1,"
            "product:https://github.com/org/product"
        )
        assert len(result) == 2
        assert result[0]["token"] == "tok1"  # pragma: allowlist secret
        assert "token" not in result[1]

    def test_settings_loads_additional_repos(self) -> None:
        with patch.dict(
            os.environ,
            _build_env(ADDITIONAL_REPOS="infra:https://github.com/org/infra"),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert settings.additional_repos == "infra:https://github.com/org/infra"


class TestParseRepoRef:
    """Tests for parse_repo_ref function."""

    def test_url_without_ref(self) -> None:
        """URL without ref returns empty ref string."""
        url, ref = parse_repo_ref("https://github.com/org/repo")
        assert url == "https://github.com/org/repo"
        assert ref == ""

    def test_url_with_branch_ref(self) -> None:
        """URL with ':develop' returns URL and branch ref."""
        url, ref = parse_repo_ref("https://github.com/org/repo:develop")
        assert url == "https://github.com/org/repo"
        assert ref == "develop"

    def test_url_with_tag_ref(self) -> None:
        """URL with ':v1.0.0' returns URL and tag ref."""
        url, ref = parse_repo_ref("https://github.com/org/repo:v1.0.0")
        assert url == "https://github.com/org/repo"
        assert ref == "v1.0.0"

    def test_url_with_port_and_ref(self) -> None:
        """URL with port AND ref correctly splits on last segment's colon."""
        url, ref = parse_repo_ref("https://gitlab.internal:8443/org/repo:main")
        assert url == "https://gitlab.internal:8443/org/repo"
        assert ref == "main"

    def test_url_ending_with_git_and_ref(self) -> None:
        """URL ending with '.git:ref' extracts ref from .git segment."""
        url, ref = parse_repo_ref("https://github.com/org/repo.git:feature-x")
        assert url == "https://github.com/org/repo.git"
        assert ref == "feature-x"

    def test_empty_string(self) -> None:
        """Empty string returns empty tuple."""
        url, ref = parse_repo_ref("")
        assert url == ""
        assert ref == ""

    def test_whitespace_only(self) -> None:
        """Whitespace-only string returns empty tuple."""
        url, ref = parse_repo_ref("   ")
        assert url == ""
        assert ref == ""

    def test_url_with_trailing_slash(self) -> None:
        """URL with trailing slash has no ref (last segment is empty)."""
        url, ref = parse_repo_ref("https://github.com/org/repo/")
        assert url == "https://github.com/org/repo/"
        assert ref == ""

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace is stripped before parsing."""
        url, ref = parse_repo_ref("  https://github.com/org/repo:develop  ")
        assert url == "https://github.com/org/repo"
        assert ref == "develop"

    def test_trailing_colon_returns_empty_ref(self) -> None:
        url, ref = parse_repo_ref("https://github.com/org/repo:")
        assert url == "https://github.com/org/repo"
        assert ref == ""

    def test_ref_with_slash(self) -> None:
        """Ref containing slashes (branch names like feature/foo)."""
        url, ref = parse_repo_ref("https://github.com/org/repo:feature/foo")
        assert url == "https://github.com/org/repo"
        assert ref == "feature/foo"


class TestSafeParseRepoRef:
    """``safe_parse_repo_ref`` degrades a stored value that no clone could use."""

    def test_blank_is_absent_without_a_warning(self, caplog) -> None:
        from tests.conftest import capture_main_logs

        with capture_main_logs(
            caplog, logging.WARNING, "rootcoz.config", bypass_duplicate_filter=True
        ):
            assert safe_parse_repo_ref("   ") == ("", "")
        assert "Ignoring malformed repository URL" not in caplog.text

    def test_valid_url_with_ref_is_kept(self) -> None:
        assert safe_parse_repo_ref("https://github.com/org/repo:develop") == (
            "https://github.com/org/repo",
            "develop",
        )

    def test_stored_http_url_still_replays(self) -> None:
        """Only *new* input must be https/git; a stored http value is not dropped."""
        assert safe_parse_repo_ref("http://host/org/repo:main") == (
            "http://host/org/repo",
            "main",
        )

    @pytest.mark.parametrize(
        "stored",
        ["not-a-url", "github.com/org/repo", "/local/path", "https://[bad/x"],
    )
    def test_unusable_stored_value_is_absent(self, stored: str) -> None:
        """A value no clone can use is absent, not a repository (#581 Qodo r3).

        ``parse_repo_ref`` returns a scheme-less value unchanged, so ``not-a-url``
        used to count as an available tests repository and reached the clone path.
        """
        assert safe_parse_repo_ref(stored) == ("", "")

    def test_absent_value_warns_without_its_credentials(self, caplog) -> None:
        from tests.conftest import capture_main_logs

        with capture_main_logs(
            caplog, logging.WARNING, "rootcoz.config", bypass_duplicate_filter=True
        ):
            assert safe_parse_repo_ref("github.com/org/repo?token=tok") == ("", "")
        assert "Ignoring malformed repository URL" in caplog.text
        assert "?***" in caplog.text
        assert "tok" not in caplog.text


class TestValidateTestsRepoUrl:
    """The request-boundary ``tests_repo_url`` check (#562 Rule 1)."""

    def test_none_and_empty_pass_through(self) -> None:
        assert validate_tests_repo_url(None) is None
        assert validate_tests_repo_url("  ") == ""

    @pytest.mark.parametrize(
        "value",
        [
            "https://github.com/org/repo",
            "https://github.com/org/repo:feature/foo",
            "git://host/repo",
        ],
    )
    def test_valid_values_are_returned_stripped(self, value: str) -> None:
        assert validate_tests_repo_url(f" {value} ") == value

    @pytest.mark.parametrize(
        "value",
        [
            "https://github.com/org/repo:bad ref",
            "https://github.com/org/repo:bad\x00ref",
            "https://github.com/org/repo:bad\nref",
        ],
    )
    def test_ref_with_whitespace_or_control_bytes_is_refused(self, value: str) -> None:
        """The ref reaches ``git clone --branch``: it cannot hold either (#581)."""
        with pytest.raises(ValueError) as excinfo:
            validate_tests_repo_url(value)
        assert "tests_repo_url" in str(excinfo.value)
        assert "bad" not in str(excinfo.value)

    def test_plain_http_is_refused_like_the_clone_refuses_it(self) -> None:
        """http is refused at the boundary because main's clone refuses it too.

        ``RepositoryManager._validate_repo_url`` (unchanged by #562) raises
        ``Invalid repository URL scheme. Only https:// and git://`` for an
        ``http://`` URL, so on main an http ``tests_repo_url`` was accepted by
        the untyped field and then failed the job at clone time. Refusing it at
        the request boundary is the same product rule, reported earlier.
        """
        with pytest.raises(ValueError) as excinfo:
            validate_tests_repo_url("http://host/org/repo")
        assert "tests_repo_url" in str(excinfo.value)
        assert "https:// or git://" in str(excinfo.value)
        assert "host" not in str(excinfo.value)


class TestPeerSettingsFields:
    """Tests for peer_ai_configs and peer_analysis_max_rounds Settings fields."""

    def test_peer_ai_configs_defaults_to_empty(self) -> None:
        """peer_ai_configs defaults to empty string."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert settings.peer_ai_configs == ""

    def test_peer_ai_configs_from_env(self) -> None:
        """peer_ai_configs is loaded from PEER_AI_CONFIGS env var."""
        env = _build_env(PEER_AI_CONFIGS="claude:opus,gemini:pro")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.peer_ai_configs == "claude:opus,gemini:pro"

    def test_peer_analysis_max_rounds_default(self) -> None:
        """peer_analysis_max_rounds defaults to 3."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert settings.peer_analysis_max_rounds == 3

    def test_peer_analysis_max_rounds_from_env(self) -> None:
        """peer_analysis_max_rounds is loaded from PEER_ANALYSIS_MAX_ROUNDS env var."""
        env = _build_env(PEER_ANALYSIS_MAX_ROUNDS="5")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.peer_analysis_max_rounds == 5


class TestMaxConcurrentAiCalls:
    """Tests for max_concurrent_ai_calls setting."""

    def test_default_value_is_3(self) -> None:
        """Default max_concurrent_ai_calls is 3."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert settings.max_concurrent_ai_calls == 3

    def test_env_override(self) -> None:
        """MAX_CONCURRENT_AI_CALLS env var overrides the default."""
        env = _build_env(MAX_CONCURRENT_AI_CALLS="5")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.max_concurrent_ai_calls == 5

    def test_env_value_of_1(self) -> None:
        """MAX_CONCURRENT_AI_CALLS=1 is valid."""
        env = _build_env(MAX_CONCURRENT_AI_CALLS="1")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.max_concurrent_ai_calls == 1


class TestDefaultUserRole:
    """Tests for DEFAULT_USER_ROLE validation."""

    def test_default_is_reviewer(self) -> None:
        env = _build_env()
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.default_user_role == "reviewer"

    def test_operator_allowed(self) -> None:
        env = _build_env(DEFAULT_USER_ROLE="operator")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.default_user_role == "operator"

    def test_viewer_allowed(self) -> None:
        env = _build_env(DEFAULT_USER_ROLE="viewer")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.default_user_role == "viewer"

    def test_admin_rejected(self) -> None:
        """DEFAULT_USER_ROLE=admin is rejected to prevent privilege escalation."""
        env = _build_env(DEFAULT_USER_ROLE="admin")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(Exception, match="DEFAULT_USER_ROLE"),
        ):
            Settings(_env_file=None)

    def test_invalid_value_rejected(self) -> None:
        env = _build_env(DEFAULT_USER_ROLE="superuser")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(Exception, match="DEFAULT_USER_ROLE"),
        ):
            Settings(_env_file=None)


class TestSSOIssuerPaths:
    """Only the settings an origin is appended to refuse a path."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("sso_openshift_issuer", "https://oauth-openshift.example.com/some/path"),
            ("sso_openshift_api_url", "https://api.cluster:6443/extra"),
        ],
    )
    def test_a_path_on_an_openshift_origin_is_refused(
        self, field: str, value: str
    ) -> None:
        """The provider appends /oauth/... and /api/... to these origins."""
        with (
            patch.dict(os.environ, _build_env(), clear=True),
            pytest.raises(ValidationError, match="bare origin"),
        ):
            Settings(_env_file=None, **{field: value})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("sso_openshift_issuer", "https://oauth-openshift.example.com"),
            ("sso_openshift_api_url", "https://api.cluster:6443"),
            # A Keycloak realm issuer is a path by design, so it still passes.
            ("sso_redhat_issuer", "https://sso.example.com/auth/realms/myrealm"),
        ],
    )
    def test_a_bare_origin_or_realm_path_is_accepted(
        self, field: str, value: str
    ) -> None:
        """A bare origin, and the Red Hat realm path, are the valid shapes."""
        with patch.dict(os.environ, _build_env(), clear=True):
            assert getattr(Settings(_env_file=None, **{field: value}), field) == value


class TestDeviceCodePollInterval:
    """The poll interval must leave the CLI time to poll a live code."""

    @pytest.mark.parametrize("interval", [600, 601])
    def test_interval_at_or_above_the_ttl_is_refused(self, interval: int) -> None:
        """An interval that long means no poll happens before the code expires."""
        with (
            patch.dict(os.environ, _build_env(), clear=True),
            pytest.raises(ValidationError, match="must be shorter than"),
        ):
            Settings(
                _env_file=None,
                sso_device_code_ttl_seconds=600,
                sso_device_poll_interval_seconds=interval,
            )

    def test_interval_below_the_ttl_is_accepted(self) -> None:
        """A shorter interval leaves room for at least one poll."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(
                _env_file=None,
                sso_device_code_ttl_seconds=600,
                sso_device_poll_interval_seconds=599,
            )
            assert settings.sso_device_poll_interval_seconds == 599

    def test_defaults_are_accepted(self) -> None:
        """The shipped defaults (600s TTL, 5s interval) stay valid."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert settings.sso_device_code_ttl_seconds == 600
            assert settings.sso_device_poll_interval_seconds == 5


class TestBrowserProviderWithoutBaseUrl:
    """A configured provider with no public origin cannot complete a login.

    One predicate, asked by the settings save path (which refuses to introduce
    it) and by ``get_settings`` (which says so once). The device flow is not
    covered: the CLI joins a relative verification URL itself.
    """

    _GOOGLE: ClassVar[dict[str, str]] = {
        "SSO_GOOGLE_CLIENT_ID": "client-id",
        "SSO_GOOGLE_CLIENT_SECRET": "client-secret",  # pragma: allowlist secret
    }

    def _settings(self, **env: str) -> Settings:
        with patch.dict(os.environ, _build_env(**env), clear=True):
            return Settings(_env_file=None)

    def test_a_device_only_deployment_is_correctly_configured(self) -> None:
        """No provider and no base URL is the shipped CLI-only setup."""
        assert browser_provider_without_base_url(self._settings()) is None

    def test_a_configured_provider_without_a_base_url_is_named(self) -> None:
        """The name is what makes the refusal and the warning actionable."""
        settings = self._settings(**self._GOOGLE)
        assert browser_provider_without_base_url(settings) == "google"

    def test_a_whitespace_base_url_is_no_base_url(self) -> None:
        """It cannot build a URL, so it must not read as configured."""
        settings = self._settings(**self._GOOGLE, PUBLIC_BASE_URL="   ")
        assert browser_provider_without_base_url(settings) == "google"

    def test_a_base_url_settles_it(self) -> None:
        """The pair a deployment needs: credentials and an origin."""
        settings = self._settings(**self._GOOGLE, PUBLIC_BASE_URL="https://x.example")
        assert browser_provider_without_base_url(settings) is None

    def test_half_a_credential_is_not_a_provider(self) -> None:
        """No secret means the button was never offered, so nothing is hidden."""
        settings = self._settings(SSO_GOOGLE_CLIENT_ID="client-id")
        assert browser_provider_without_base_url(settings) is None

    def test_the_misconfiguration_is_warned_about_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rebuild repeats neither the line nor its absence; startup stays up."""
        from rootcoz import config

        monkeypatch.setattr(config, "_warned_missing_base_url", None)
        with patch.dict(os.environ, _build_env(**self._GOOGLE), clear=True):
            clear_settings_cache()
            with capture_main_logs(
                caplog, level=logging.WARNING, logger="rootcoz.config"
            ):
                settings = get_settings()
                clear_settings_cache()
                get_settings()

            lines = [
                r.getMessage()
                for r in caplog.records
                if "PUBLIC_BASE_URL" in r.getMessage()
            ]
            assert len(lines) == 1
            assert "'google'" in lines[0]
        # The load itself never fails: Server Settings is where this is fixed.
        assert settings.public_base_url is None


class TestSSODomainAllowList:
    """An allow-list entry no email domain can match is a lockout, not a typo."""

    @pytest.mark.parametrize(
        "raw",
        [
            # An address pasted whole: no email domain ever equals it.
            "alice@redhat.com",
            # An empty label can never appear in a host name.
            "redhat..com",
            # A label may not start or end with '-', nor carry a path.
            "-x.com",
            "x-.com",
            "redhat.com/foo",
            # A single label admits nothing a mail domain can satisfy.
            "com",
            # 64 characters is one past the DNS limit.
            f"{'a' * 64}.com",
            # Not a legal IDNA name at all (a label is empty after encoding).
            "münchen..de",
        ],
    )
    def test_an_entry_that_cannot_match_is_refused(self, raw: str) -> None:
        """Saved as-is, each of these would refuse every SSO sign-in."""
        with pytest.raises(ValueError, match="Invalid SSO domain entry"):
            normalize_domain_list(raw)

    def test_an_overlong_name_is_refused_despite_valid_labels(self) -> None:
        """Five 63-character labels pass the label rule but exceed 253 characters."""
        name = ".".join(["a" * 63] * 5)
        assert all(len(label) == 63 for label in name.split("."))
        with pytest.raises(ValueError, match="Invalid SSO domain entry"):
            normalize_domain_list(name)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("@RedHat.com", frozenset({"redhat.com"})),
            ("redhat.com.", frozenset({"redhat.com"})),
            # A Unicode entry is kept in its punycode form — the one a claim is
            # compared in.
            ("münchen.de", frozenset({"xn--mnchen-3ya.de"})),
            ("MÜNCHEN.DE", frozenset({"xn--mnchen-3ya.de"})),
            ("eng.redhat.com", frozenset({"eng.redhat.com"})),
            ("@RedHat.com,example.org", frozenset({"redhat.com", "example.org"})),
            # Punycode is plain ASCII, so it is a usable entry.
            ("xn--mnchen-3ya.de", frozenset({"xn--mnchen-3ya.de"})),
        ],
    )
    def test_a_real_domain_is_normalised(
        self, raw: str, expected: frozenset[str]
    ) -> None:
        """The '@', the trailing dot and the case never change the meaning."""
        assert normalize_domain_list(raw) == expected

    def test_a_unicode_entry_admits_both_spellings_of_the_claim(self) -> None:
        """The IdP may report either form, so both sides are compared as IDNA."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None, sso_allowed_domains="münchen.de")
        assert settings.sso_allowed_domains_set == frozenset({"xn--mnchen-3ya.de"})

        claim = ClaimSet(
            subject="1", username_hint="alice", claims={"email_verified": True}
        )
        for email in ("alice@münchen.de", "alice@xn--mnchen-3ya.de"):
            auth_identity.check_domain(replace(claim, email=email), settings)

        # A look-alike suffix is still refused, and a domain that cannot be
        # encoded matches nothing rather than raising out of the callback.
        for refused in ("alice@notmünchen.de", "alice@münchen..de"):
            with pytest.raises(AuthError) as excinfo:
                auth_identity.check_domain(replace(claim, email=refused), settings)
            assert excinfo.value.reason == "domain_not_allowed"

    def test_a_blank_value_disables_the_check(self) -> None:
        """Unset means no restriction, and so does whitespace alone."""
        assert normalize_domain_list("") == frozenset()
        assert normalize_domain_list(None) == frozenset()
        assert normalize_domain_list("   ") == frozenset()

    def test_a_list_of_separators_is_refused(self) -> None:
        """Reading it as 'no restriction' would silently disable the policy."""
        with pytest.raises(ValueError, match="Invalid SSO domain entry"):
            normalize_domain_list(",")
