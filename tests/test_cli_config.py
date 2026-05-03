"""Tests for jji CLI config module and config-driven server resolution."""

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from jenkins_job_insight.cli.config import (
    ServerConfig,
    _server_config_from_dict,
    get_default_server_name,
    get_server_config,
    list_servers,
    load_config,
)
from jenkins_job_insight.cli.main import app

runner = CliRunner()

_SAMPLE_TOML = """\
[default]
server = "dev"

[servers.dev]
url = "http://localhost:8000"
username = "dev-user"
no_verify_ssl = true
jenkins_url = "https://jenkins.dev.local"
jenkins_user = "jenkins-dev"
jenkins_password = "dev-token"  # pragma: allowlist secret
jenkins_ssl_verify = false
tests_repo_url = "https://github.com/org/tests"
ai_provider = "claude"
ai_model = "opus-4"
ai_cli_timeout = 15
max_concurrent_ai_calls = 7
jira_url = "https://jira.dev.local"
jira_email = "dev@example.com"
jira_api_token = "jira-tok-dev"
jira_pat = "jira-pat-dev"
jira_token = "jira-token-dev"
jira_project_key = "DEV"
jira_ssl_verify = false
jira_max_results = 30
enable_jira = true
github_token = "ghp_dev123"
wait_for_completion = true
poll_interval_minutes = 2
max_wait_minutes = 45
force = true

[servers.prod]
url = "https://jji.example.com"
username = "prod-user"
no_verify_ssl = false

[servers.staging]
url = "https://staging-jji.example.com"
username = "admin"
"""

_DEFAULTS_TOML = """\
[default]
server = "dev"

[defaults]
jenkins_url = "https://jenkins.shared.local"
jenkins_user = "shared-user"
jenkins_password = "shared-token"  # pragma: allowlist secret
ai_provider = "claude"
ai_model = "opus-4"
tests_repo_url = "https://github.com/org/tests"

[servers.dev]
url = "http://localhost:8000"
username = "dev-user"
jenkins_ssl_verify = false

[servers.prod]
url = "https://jji.example.com"
username = "prod-user"
ai_provider = "cursor"
ai_model = "gpt-5"
"""


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """Write sample TOML config and return its path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(_SAMPLE_TOML)
    return cfg


@pytest.fixture()
def defaults_config_file(tmp_path: Path) -> Path:
    """Write TOML config with a [defaults] section and return its path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(_DEFAULTS_TOML)
    return cfg


@pytest.fixture()
def empty_config(tmp_path: Path) -> Path:
    """Return a path to a non-existent config file."""
    return tmp_path / "missing.toml"


# -- load_config --------------------------------------------------------------


def _write_config(tmp_path: Path, content: str) -> Path:
    """Write a temporary config file and return its path."""
    cfg = tmp_path / "bad.toml"
    cfg.write_text(content)
    return cfg


class TestLoadConfig:
    def test_loads_valid_toml(self, config_file: Path):
        config = load_config(config_file)
        assert config["default"]["server"] == "dev"
        assert "dev" in config["servers"]
        assert "prod" in config["servers"]

    def test_returns_empty_when_missing(self, empty_config: Path):
        assert load_config(empty_config) == {}

    def test_raises_value_error_on_malformed_toml(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, "[invalid\n")
        with pytest.raises(ValueError, match=r"Invalid TOML"):
            load_config(bad_file)

    def test_raises_on_non_dict_default(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, 'default = "not-a-dict"\n')
        with pytest.raises(ValueError, match=r"'default' must be a mapping"):
            load_config(bad_file)

    def test_raises_on_non_dict_defaults(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, 'defaults = "not-a-dict"\n')
        with pytest.raises(ValueError, match=r"'defaults' must be a mapping"):
            load_config(bad_file)

    def test_raises_on_missing_server_url(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, '[servers.myserver]\nusername = "user"\n')
        with pytest.raises(
            ValueError, match=r"servers\.myserver\.url must be a non-empty"
        ):
            load_config(bad_file)

    def test_raises_on_empty_server_url(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, '[servers.myserver]\nurl = ""\n')
        with pytest.raises(
            ValueError, match=r"servers\.myserver\.url must be a non-empty"
        ):
            load_config(bad_file)

    def test_raises_on_whitespace_only_server_url(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, '[servers.myserver]\nurl = "   "\n')
        with pytest.raises(
            ValueError, match=r"servers\.myserver\.url must be a non-empty"
        ):
            load_config(bad_file)

    def test_raises_on_leading_trailing_whitespace_url(self, tmp_path: Path):
        bad_file = _write_config(
            tmp_path, '[servers.myserver]\nurl = " https://api.example.com "\n'
        )
        with pytest.raises(
            ValueError, match=r"servers\.myserver\.url must be a non-empty"
        ):
            load_config(bad_file)

    def test_raises_on_non_dict_server_entry(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, '[servers]\nmyserver = "not-a-dict"\n')
        with pytest.raises(ValueError, match=r"servers\.myserver must be a mapping"):
            load_config(bad_file)

    def test_rejects_defaults_server_key(self, tmp_path: Path):
        bad_file = _write_config(tmp_path, '[defaults]\nserver = "prod"\n')
        with pytest.raises(ValueError, match=r"'defaults\.server' is not supported"):
            load_config(bad_file)


# -- get_default_server_name --------------------------------------------------


class TestGetDefaultServerName:
    def test_returns_default(self, config_file: Path):
        config = load_config(config_file)
        assert get_default_server_name(config) == "dev"

    def test_returns_empty_when_no_default(self):
        assert get_default_server_name({}) == ""

    def test_returns_empty_when_default_section_missing(self):
        assert get_default_server_name({"servers": {"x": {}}}) == ""


# -- get_server_config --------------------------------------------------------


class TestGetServerConfig:
    def test_lookup_by_name(self, config_file: Path):
        config = load_config(config_file)
        cfg = get_server_config("prod", config)
        assert cfg is not None
        assert cfg.url == "https://jji.example.com"
        assert cfg.username == "prod-user"
        assert cfg.no_verify_ssl is False

    def test_falls_back_to_default(self, config_file: Path):
        config = load_config(config_file)
        cfg = get_server_config(None, config)
        assert cfg is not None
        assert cfg.url == "http://localhost:8000"
        assert cfg.username == "dev-user"
        assert cfg.no_verify_ssl is True

    def test_returns_none_for_unknown_name(self, config_file: Path):
        config = load_config(config_file)
        assert get_server_config("nonexistent", config) is None

    def test_returns_none_when_config_empty(self):
        assert get_server_config("anything", {}) is None

    def test_returns_none_when_no_default_and_no_name(self):
        config = {"servers": {"a": {"url": "http://a"}}}
        assert get_server_config(None, config) is None

    def test_reads_all_analyze_fields(self, config_file: Path):
        """All analyze-related fields are populated from TOML."""
        config = load_config(config_file)
        cfg = get_server_config("dev", config)
        assert cfg is not None
        assert cfg.jenkins_url == "https://jenkins.dev.local"
        assert cfg.jenkins_user == "jenkins-dev"
        assert cfg.jenkins_password == "dev-token"  # noqa: S105  # pragma: allowlist secret
        assert cfg.jenkins_ssl_verify is False
        assert cfg.tests_repo_url == "https://github.com/org/tests"
        assert cfg.ai_provider == "claude"
        assert cfg.ai_model == "opus-4"
        assert cfg.ai_cli_timeout == 15
        assert cfg.max_concurrent_ai_calls == 7
        assert cfg.jira_url == "https://jira.dev.local"
        assert cfg.jira_email == "dev@example.com"
        assert cfg.jira_api_token == "jira-tok-dev"  # noqa: S105
        assert cfg.jira_pat == "jira-pat-dev"  # noqa: S105
        assert cfg.jira_token == "jira-token-dev"  # noqa: S105
        assert cfg.jira_project_key == "DEV"
        assert cfg.jira_ssl_verify is False
        assert cfg.jira_max_results == 30
        assert cfg.enable_jira is True
        assert cfg.github_token == "ghp_dev123"  # noqa: S105
        assert cfg.wait_for_completion is True
        assert cfg.poll_interval_minutes == 2
        assert cfg.max_wait_minutes == 45
        assert cfg.force is True

    def test_defaults_for_missing_analyze_fields(self, config_file: Path):
        """Servers without analyze fields get dataclass defaults."""
        config = load_config(config_file)
        cfg = get_server_config("prod", config)
        assert cfg is not None
        assert cfg.jenkins_url == ""
        assert cfg.jenkins_ssl_verify is None
        assert cfg.ai_provider == ""
        assert cfg.ai_cli_timeout == 0
        assert cfg.max_concurrent_ai_calls == 0
        assert cfg.jira_ssl_verify is None
        assert cfg.jira_max_results == 0
        assert cfg.enable_jira is None
        assert cfg.github_token == ""
        assert cfg.jira_token == ""
        assert cfg.wait_for_completion is None
        assert cfg.poll_interval_minutes == 0
        assert cfg.max_wait_minutes == 0
        assert cfg.force is None


# -- list_servers --------------------------------------------------------------


class TestListServers:
    def test_lists_all(self, config_file: Path):
        config = load_config(config_file)
        servers = list_servers(config)
        assert set(servers.keys()) == {"dev", "prod", "staging"}
        assert isinstance(servers["dev"], ServerConfig)

    def test_empty_when_no_servers(self):
        assert list_servers({}) == {}

    def test_server_fields(self, config_file: Path):
        config = load_config(config_file)
        staging = list_servers(config)["staging"]
        assert staging.url == "https://staging-jji.example.com"
        assert staging.username == "admin"
        assert staging.no_verify_ssl is False

    def test_list_servers_includes_analyze_fields(self, config_file: Path):
        config = load_config(config_file)
        dev = list_servers(config)["dev"]
        assert dev.jenkins_url == "https://jenkins.dev.local"
        assert dev.ai_provider == "claude"
        assert dev.enable_jira is True


def _mock_healthy_client(mock_client_fn: MagicMock) -> MagicMock:
    """Wire up a mock client that returns a healthy status."""
    client = MagicMock()
    client.health.return_value = {"status": "healthy"}
    mock_client_fn.return_value = client
    return client


# -- CLI integration: --server with config name ----------------------------------


class TestServerNameResolution:
    """--server accepts a config server name and resolves it."""

    def test_server_name_resolves_from_config(self, config_file: Path):
        with (
            patch("jenkins_job_insight.cli.main.get_server_config") as mock_get,
            patch("jenkins_job_insight.cli.main.list_servers") as mock_list,
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            mock_get.return_value = ServerConfig(
                url="https://jji.example.com",
                username="prod-user",
                no_verify_ssl=False,
            )
            mock_list.return_value = {}
            _mock_healthy_client(mock_client_fn)

            result = runner.invoke(app, ["--server", "prod", "health"])
            assert result.exit_code == 0
            assert "healthy" in result.output
            mock_get.assert_called_once_with("prod")

    def test_unknown_server_name_errors(self):
        with (
            patch("jenkins_job_insight.cli.main.get_server_config", return_value=None),
            patch(
                "jenkins_job_insight.cli.main.list_servers",
                return_value={"dev": ServerConfig(url="http://x")},
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = runner.invoke(app, ["--server", "nonexistent", "health"])
            assert result.exit_code == 1
            assert "nonexistent" in result.output
            assert "dev" in result.output  # lists available servers


class TestDefaultServerFromConfig:
    """Default server from config is used when no --server given."""

    def test_default_server_used(self, config_file: Path):
        config = load_config(config_file)
        # Verify default profile name resolves to "dev"
        assert get_default_server_name(config) == "dev"
        resolved = get_server_config("dev", config)
        assert resolved is not None

        with (
            patch("jenkins_job_insight.cli.config.load_config", return_value=config),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                wraps=get_server_config,
            ) as mock_get,
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch.dict(os.environ, {}, clear=True),
        ):
            _mock_healthy_client(mock_client_fn)

            result = runner.invoke(app, ["health"])
            assert result.exit_code == 0
            assert "healthy" in result.output
            # Default server resolution: called without explicit name,
            # internally resolves default profile "dev" from config.
            mock_get.assert_called_once()
            # Verify no explicit server name was passed (relies on default)
            args, _kwargs = mock_get.call_args
            assert not args or args[0] is None

    def test_no_server_no_config_errors(self):
        with (
            patch("jenkins_job_insight.cli.main.get_server_config", return_value=None),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = runner.invoke(app, ["health"])
            assert result.exit_code == 1
            assert "No server specified" in result.output
            assert "config.toml" in result.output


class TestCLIOverridesConfig:
    """CLI flags take precedence over config values."""

    def test_cli_user_overrides_config(self):
        with (
            patch("jenkins_job_insight.cli.main.get_server_config") as mock_get,
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch("jenkins_job_insight.cli.main._state", {}) as mock_state,
            patch.dict(os.environ, {}, clear=True),
        ):
            mock_get.return_value = ServerConfig(
                url="http://localhost:8000",
                username="config-user",
                no_verify_ssl=False,
            )
            _mock_healthy_client(mock_client_fn)

            result = runner.invoke(
                app,
                ["--server", "dev", "--user", "cli-user", "health"],
            )
            assert result.exit_code == 0
            assert mock_state["username"] == "cli-user"
            # Ensure config profile was loaded (named server exercises the override path)
            mock_get.assert_called_once_with("dev")

    def test_cli_no_verify_ssl_overrides_config(self):
        with (
            patch("jenkins_job_insight.cli.main.get_server_config") as mock_get,
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch("jenkins_job_insight.cli.main._state", {}) as mock_state,
            patch.dict(os.environ, {}, clear=True),
        ):
            mock_get.return_value = ServerConfig(
                url="http://localhost:8000",
                username="",
                no_verify_ssl=False,
            )
            _mock_healthy_client(mock_client_fn)

            result = runner.invoke(
                app,
                ["--server", "dev", "--no-verify-ssl", "health"],
            )
            assert result.exit_code == 0
            assert mock_state["no_verify_ssl"] is True
            # Ensure config profile was loaded (named server exercises the override path)
            mock_get.assert_called_once_with("dev")

    def test_url_does_not_inherit_config_profile(self):
        """When --server is a concrete URL, config profile is NOT loaded."""
        with (
            patch("jenkins_job_insight.cli.main.get_server_config") as mock_get,
            patch("jenkins_job_insight.cli.main._get_client") as mock_client_fn,
            patch("jenkins_job_insight.cli.main._state", {}) as mock_state,
            patch.dict(os.environ, {}, clear=True),
        ):
            mock_get.return_value = ServerConfig(
                url="http://default:8000",
                username="default-user",
                no_verify_ssl=False,
            )
            _mock_healthy_client(mock_client_fn)

            result = runner.invoke(app, ["--server", "http://custom:9000", "health"])
            assert result.exit_code == 0
            assert mock_state["server_url"] == "http://custom:9000"
            # A concrete URL is self-contained; config username is NOT inherited.
            assert mock_state["username"] == ""
            # server_config should be None (no profile loaded).
            assert mock_state["server_config"] is None
            # get_server_config should NOT have been called.
            mock_get.assert_not_called()


# -- Config subcommand tests ---------------------------------------------------


class TestConfigShow:
    def test_show_no_config(self):
        with patch("jenkins_job_insight.cli.main.load_config", return_value={}):
            result = runner.invoke(app, ["config", "show"])
            assert result.exit_code == 0
            assert "No config file" in result.output

    def test_show_with_config(self, config_file: Path):
        config = load_config(config_file)
        with (
            patch("jenkins_job_insight.cli.main.load_config", return_value=config),
            patch(
                "jenkins_job_insight.cli.main.get_default_server_name",
                return_value="dev",
            ),
            patch(
                "jenkins_job_insight.cli.main.list_servers",
                return_value={
                    "dev": ServerConfig(
                        url="http://localhost:8000",
                        username="dev-user",
                        no_verify_ssl=True,
                    ),
                    "prod": ServerConfig(
                        url="https://jji.example.com",
                        username="prod-user",
                        no_verify_ssl=False,
                    ),
                },
            ),
        ):
            result = runner.invoke(app, ["config", "show"])
            assert result.exit_code == 0
            assert "dev" in result.output
            assert "prod" in result.output
            assert "Default server: dev" in result.output


class TestConfigServers:
    def test_servers_table(self, config_file: Path):
        config = load_config(config_file)
        with (
            patch("jenkins_job_insight.cli.main.load_config", return_value=config),
            patch(
                "jenkins_job_insight.cli.main.get_default_server_name",
                return_value="dev",
            ),
            patch(
                "jenkins_job_insight.cli.main.list_servers",
                return_value={
                    "dev": ServerConfig(
                        url="http://localhost:8000",
                        username="dev-user",
                        no_verify_ssl=True,
                    ),
                },
            ),
        ):
            result = runner.invoke(app, ["config", "servers"])
            assert result.exit_code == 0
            assert "dev" in result.output
            assert "localhost" in result.output

    def test_servers_empty(self):
        with (
            patch("jenkins_job_insight.cli.main.load_config", return_value={}),
            patch(
                "jenkins_job_insight.cli.main.get_default_server_name",
                return_value="",
            ),
            patch("jenkins_job_insight.cli.main.list_servers", return_value={}),
        ):
            result = runner.invoke(app, ["config", "servers"])
            assert result.exit_code == 0
            assert "No servers configured" in result.output

    def test_servers_json(self, config_file: Path):
        import json

        config = load_config(config_file)
        with (
            patch("jenkins_job_insight.cli.main.load_config", return_value=config),
            patch(
                "jenkins_job_insight.cli.main.get_default_server_name",
                return_value="dev",
            ),
            patch(
                "jenkins_job_insight.cli.main.list_servers",
                return_value={
                    "dev": ServerConfig(
                        url="http://localhost:8000",
                        username="dev-user",
                        no_verify_ssl=True,
                    ),
                },
            ),
        ):
            result = runner.invoke(app, ["config", "servers", "--json"])
            assert result.exit_code == 0
            parsed = json.loads(result.output)
            assert "dev" in parsed
            assert parsed["dev"]["url"] == "http://localhost:8000"
            assert parsed["dev"]["default"] is True


# -- XDG_CONFIG_HOME ----------------------------------------------------------


@contextmanager
def _reload_config_under_env(env_patch: dict[str, str], *, clear: bool = False):
    """Reload ``jenkins_job_insight.cli.config`` under *env_patch*, restoring afterwards."""
    import importlib

    import jenkins_job_insight.cli.config as cfg_mod

    try:
        with patch.dict(os.environ, env_patch, clear=clear):
            importlib.reload(cfg_mod)
            yield cfg_mod
    finally:
        importlib.reload(cfg_mod)


class TestXDGConfigHome:
    """CONFIG_DIR and CONFIG_FILE respect XDG_CONFIG_HOME."""

    def test_default_config_dir_uses_home_dot_config(self):
        """When XDG_CONFIG_HOME is unset, falls back to ~/.config."""
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with _reload_config_under_env(env, clear=True) as cfg_mod:
            assert cfg_mod.CONFIG_DIR == Path.home() / ".config" / "jji"
            assert (
                cfg_mod.CONFIG_FILE == Path.home() / ".config" / "jji" / "config.toml"
            )

    def test_xdg_config_home_override(self, tmp_path: Path):
        """When XDG_CONFIG_HOME is set, CONFIG_DIR uses it."""
        with _reload_config_under_env({"XDG_CONFIG_HOME": str(tmp_path)}) as cfg_mod:
            assert cfg_mod.CONFIG_DIR == tmp_path / "jji"
            assert cfg_mod.CONFIG_FILE == tmp_path / "jji" / "config.toml"


# -- Global defaults merging --------------------------------------------------


class TestGlobalDefaults:
    """[defaults] section values are inherited by all servers."""

    def test_server_inherits_defaults(self, defaults_config_file: Path):
        """dev server inherits jenkins_url etc. from [defaults]."""
        config = load_config(defaults_config_file)
        cfg = get_server_config("dev", config)
        assert cfg is not None
        # Inherited from [defaults]
        assert cfg.jenkins_url == "https://jenkins.shared.local"
        assert cfg.jenkins_user == "shared-user"
        assert cfg.jenkins_password == "shared-token"  # noqa: S105  # pragma: allowlist secret
        assert cfg.ai_provider == "claude"
        assert cfg.ai_model == "opus-4"
        assert cfg.tests_repo_url == "https://github.com/org/tests"
        # From [servers.dev]
        assert cfg.url == "http://localhost:8000"
        assert cfg.username == "dev-user"
        assert cfg.jenkins_ssl_verify is False

    def test_server_overrides_defaults(self, defaults_config_file: Path):
        """prod server overrides ai_provider and ai_model from [defaults]."""
        config = load_config(defaults_config_file)
        cfg = get_server_config("prod", config)
        assert cfg is not None
        # Overridden by [servers.prod]
        assert cfg.ai_provider == "cursor"
        assert cfg.ai_model == "gpt-5"
        # Still inherited from [defaults]
        assert cfg.jenkins_url == "https://jenkins.shared.local"
        assert cfg.jenkins_user == "shared-user"
        assert cfg.tests_repo_url == "https://github.com/org/tests"
        # From [servers.prod]
        assert cfg.url == "https://jji.example.com"
        assert cfg.username == "prod-user"

    def test_list_servers_merges_defaults(self, defaults_config_file: Path):
        """list_servers also applies [defaults] to every server."""
        config = load_config(defaults_config_file)
        servers = list_servers(config)
        assert servers["dev"].jenkins_url == "https://jenkins.shared.local"
        assert servers["dev"].ai_provider == "claude"
        assert servers["prod"].jenkins_url == "https://jenkins.shared.local"
        assert servers["prod"].ai_provider == "cursor"

    def test_no_defaults_section_still_works(self, config_file: Path):
        """Config without [defaults] works exactly as before."""
        config = load_config(config_file)
        cfg = get_server_config("dev", config)
        assert cfg is not None
        assert cfg.jenkins_url == "https://jenkins.dev.local"
        assert cfg.ai_provider == "claude"

    def test_empty_defaults_section(self, tmp_path: Path):
        """An empty [defaults] section has no effect."""
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[default]\nserver = "a"\n\n[defaults]\n\n[servers.a]\nurl = "http://a"\n'
        )
        config = load_config(cfg_path)
        cfg = get_server_config("a", config)
        assert cfg is not None
        assert cfg.url == "http://a"
        assert cfg.jenkins_url == ""
        assert cfg.ai_provider == ""


# -- _server_config_from_dict type validation ---------------------------------


class TestServerConfigAdditionalRepos:
    """Tests for additional_repos field on ServerConfig."""

    def test_additional_repos_default_empty(self) -> None:
        """Default additional_repos is empty string."""
        cfg = ServerConfig(url="http://test")
        assert cfg.additional_repos == ""

    def test_additional_repos_from_toml(self, tmp_path: Path) -> None:
        """additional_repos is loaded from TOML config."""
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[default]\nserver = "a"\n\n'
            "[servers.a]\n"
            'url = "http://a"\n'
            'additional_repos = "infra:https://github.com/org/infra,product:https://github.com/org/product"\n'
        )
        config = load_config(cfg_path)
        cfg = get_server_config("a", config)
        assert cfg is not None
        assert (
            cfg.additional_repos
            == "infra:https://github.com/org/infra,product:https://github.com/org/product"
        )

    def test_additional_repos_from_dict(self) -> None:
        """_server_config_from_dict loads additional_repos."""
        data = {
            "url": "http://test",
            "additional_repos": "infra:https://github.com/org/infra",
        }
        cfg = _server_config_from_dict(data)
        assert cfg.additional_repos == "infra:https://github.com/org/infra"

    def test_additional_repos_non_string_raises(self) -> None:
        """Non-string additional_repos value raises ValueError."""
        data = {"url": "http://test", "additional_repos": 42}
        with pytest.raises(ValueError, match=r"additional_repos.*must be a string"):
            _server_config_from_dict(data)


class TestServerConfigFromDictTypeValidation:
    """Type validation for peer-analysis fields in _server_config_from_dict."""

    def test_peers_non_string_raises(self) -> None:
        """Non-string 'peers' value raises ValueError."""
        data = {"url": "http://test", "peers": 42}
        with pytest.raises(ValueError, match=r"peers.*must be a string"):
            _server_config_from_dict(data)

    def test_peers_none_defaults_to_empty(self) -> None:
        """None 'peers' value defaults to empty string."""
        data = {"url": "http://test", "peers": None}
        cfg = _server_config_from_dict(data)
        assert cfg.peers == ""

    def test_peers_valid_string_accepted(self) -> None:
        """Valid string 'peers' passes through."""
        data = {"url": "http://test", "peers": "claude:opus,gemini:pro"}
        cfg = _server_config_from_dict(data)
        assert cfg.peers == "claude:opus,gemini:pro"

    def test_peer_analysis_max_rounds_non_int_raises(self) -> None:
        """Non-int 'peer_analysis_max_rounds' raises ValueError."""
        data = {"url": "http://test", "peer_analysis_max_rounds": "five"}
        with pytest.raises(
            ValueError, match=r"peer_analysis_max_rounds.*must be an integer"
        ):
            _server_config_from_dict(data)

    def test_peer_analysis_max_rounds_bool_raises(self) -> None:
        """Boolean 'peer_analysis_max_rounds' raises ValueError (bool is subclass of int)."""
        data = {"url": "http://test", "peer_analysis_max_rounds": True}
        with pytest.raises(
            ValueError, match=r"peer_analysis_max_rounds.*must be an integer"
        ):
            _server_config_from_dict(data)

    def test_peer_analysis_max_rounds_none_defaults_to_zero(self) -> None:
        """None 'peer_analysis_max_rounds' defaults to 0."""
        data = {"url": "http://test", "peer_analysis_max_rounds": None}
        cfg = _server_config_from_dict(data)
        assert cfg.peer_analysis_max_rounds == 0

    def test_peer_analysis_max_rounds_valid_int_accepted(self) -> None:
        """Valid int 'peer_analysis_max_rounds' passes through."""
        data = {"url": "http://test", "peer_analysis_max_rounds": 5}
        cfg = _server_config_from_dict(data)
        assert cfg.peer_analysis_max_rounds == 5

    def test_max_concurrent_ai_calls_negative_raises(self) -> None:
        """Negative 'max_concurrent_ai_calls' raises ValueError."""
        data = {"url": "http://test", "max_concurrent_ai_calls": -1}
        with pytest.raises(
            ValueError, match=r"max_concurrent_ai_calls.*non-negative integer"
        ):
            _server_config_from_dict(data)

    def test_max_concurrent_ai_calls_bool_raises(self) -> None:
        """Boolean 'max_concurrent_ai_calls' raises ValueError (bool is subclass of int)."""
        data = {"url": "http://test", "max_concurrent_ai_calls": True}
        with pytest.raises(
            ValueError, match=r"max_concurrent_ai_calls.*must be an integer"
        ):
            _server_config_from_dict(data)

    def test_max_concurrent_ai_calls_string_raises(self) -> None:
        """String 'max_concurrent_ai_calls' raises ValueError."""
        data = {"url": "http://test", "max_concurrent_ai_calls": "five"}
        with pytest.raises(
            ValueError, match=r"max_concurrent_ai_calls.*must be an integer"
        ):
            _server_config_from_dict(data)
