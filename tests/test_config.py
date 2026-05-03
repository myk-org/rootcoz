"""Tests for configuration settings."""

import os
from unittest.mock import patch

import pytest

from jenkins_job_insight.config import (
    Settings,
    _resolve_jira_auth,
    get_settings,
    parse_additional_repos,
    parse_peer_configs,
    parse_repo_ref,
)
from tests.conftest import build_test_env as _build_env


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
        # Clear the lru_cache before testing
        get_settings.cache_clear()
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_get_settings_cached(self, mock_env_vars: dict[str, str]) -> None:
        """Test that get_settings returns cached instance."""
        get_settings.cache_clear()
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2


class TestJiraSettings:
    """Tests for Jira configuration fields."""

    def test_jira_disabled_by_default(self) -> None:
        """Jira is disabled when no Jira env vars are set."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert not settings.jira_enabled
            assert settings.jira_url is None

    def test_jira_enabled_with_cloud_auth(self) -> None:
        """Jira is enabled with URL + email + API token + project key."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_EMAIL="user@example.com",
                JIRA_API_TOKEN="token-123",  # noqa: S106  # pragma: allowlist secret
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert settings.jira_enabled

    def test_jira_enabled_with_server_pat(self) -> None:
        """Jira is enabled with URL + PAT + project key."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira-server.example.com",
                JIRA_PAT="pat-token-456",
                JIRA_PROJECT_KEY="TEST",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert settings.jira_enabled

    def test_jira_disabled_without_url(self) -> None:
        """Jira is disabled when URL is missing even with credentials."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_EMAIL="user@example.com",
                JIRA_API_TOKEN="token-123",  # noqa: S106  # pragma: allowlist secret
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
            assert settings.jira_ssl_verify is True
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
        """Test custom values for Jira fields."""
        with patch.dict(
            os.environ,
            _build_env(
                JIRA_URL="https://jira.example.com",
                JIRA_EMAIL="user@example.com",
                JIRA_API_TOKEN="token",  # noqa: S106  # pragma: allowlist secret
                JIRA_PROJECT_KEY="MYPROJ",
                JIRA_SSL_VERIFY="false",
                JIRA_MAX_RESULTS="10",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
            assert settings.jira_project_key == "MYPROJ"
            assert settings.jira_ssl_verify is False
            assert settings.jira_max_results == 10


class TestResolveJiraAuth:
    """Tests for _resolve_jira_auth helper."""

    def test_email_plus_pat_is_cloud(self) -> None:
        """JIRA_EMAIL + JIRA_PAT resolves to Cloud mode.

        Email present = Cloud.  PAT is used as the token via Basic auth.
        """
        env = _build_env(
            JIRA_URL="https://myorg.atlassian.net",
            JIRA_EMAIL="user@example.com",
            JIRA_PAT="pat-token-456",
            JIRA_PROJECT_KEY="TEST",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            is_cloud, token = _resolve_jira_auth(settings)
            assert is_cloud, "email present must activate Cloud mode"
            assert token == env["JIRA_PAT"]


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
            GITHUB_TOKEN="ghp_test123",  # noqa: S106  # pragma: allowlist secret
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
            GITHUB_TOKEN="ghp_test123",  # noqa: S106  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.github_issues_enabled

    def test_github_issues_explicit_false_disables(self) -> None:
        """GitHub issues disabled when ENABLE_GITHUB_ISSUES is explicitly False."""
        env = _build_env(
            GITHUB_TOKEN="ghp_test123",  # noqa: S106  # pragma: allowlist secret
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


class TestParsePeerConfigs:
    """Tests for the parse_peer_configs helper."""

    def test_parse_peer_configs_valid(self) -> None:
        """Valid 'provider:model' pairs produce correct dicts."""
        result = parse_peer_configs("claude:opus,cursor:gpt")
        assert result == [
            {"ai_provider": "claude", "ai_model": "opus"},
            {"ai_provider": "cursor", "ai_model": "gpt"},
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

    def test_parse_peer_configs_invalid_provider(self) -> None:
        """Unsupported provider raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported provider 'openai'"):
            parse_peer_configs("openai:gpt")

    def test_parse_peer_configs_trailing_comma(self) -> None:
        """Trailing comma results in empty entry ValueError."""
        with pytest.raises(ValueError, match="Empty entry"):
            parse_peer_configs("claude:opus,")


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
                "token": "ghp_secret123",  # noqa: S105  # pragma: allowlist secret
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
                "token": "ghp_secret123",  # noqa: S105  # pragma: allowlist secret
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
        assert result[0]["token"] == "tok1"  # noqa: S105  # pragma: allowlist secret
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


class TestForceAnalysisSettings:
    """Tests for force_analysis setting."""

    def test_force_analysis_default_false(self) -> None:
        """force_analysis defaults to False."""
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert settings.force_analysis is False

    def test_force_analysis_from_env(self) -> None:
        """force_analysis is loaded from FORCE_ANALYSIS env var."""
        env = _build_env(FORCE_ANALYSIS="true")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.force_analysis is True

    def test_force_analysis_env_false(self) -> None:
        """FORCE_ANALYSIS=false sets force_analysis to False."""
        env = _build_env(FORCE_ANALYSIS="false")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.force_analysis is False


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
