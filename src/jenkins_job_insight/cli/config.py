"""JJI CLI configuration file support.

Loads named server configurations from $XDG_CONFIG_HOME/jji/config.toml
(defaults to ~/.config/jji/config.toml when XDG_CONFIG_HOME is unset).
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_xdg = os.environ.get("XDG_CONFIG_HOME", "")
_XDG_CONFIG_HOME = Path(_xdg) if _xdg else Path.home() / ".config"
CONFIG_DIR = _XDG_CONFIG_HOME / "jji"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class ServerConfig:
    """A named server entry from the config file."""

    url: str
    username: str = ""
    no_verify_ssl: bool = False
    # Jenkins
    jenkins_url: str = ""
    jenkins_user: str = ""
    jenkins_password: str = ""
    jenkins_ssl_verify: bool | None = None
    jenkins_timeout: int = 0  # 0 means use server default
    # Tests
    tests_repo_url: str = ""
    tests_repo_token: str = ""
    # AI
    ai_provider: str = ""
    ai_model: str = ""
    ai_cli_timeout: int = 0  # 0 means use server default
    max_concurrent_ai_calls: int = 0  # 0 means use server default
    # Jira
    jira_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_pat: str = ""
    jira_token: str = ""
    jira_project_key: str = ""
    jira_security_level: str = ""
    jira_ssl_verify: bool | None = None
    jira_max_results: int = 0  # 0 means use server default
    enable_jira: bool | None = None
    # GitHub
    github_token: str = ""
    github_repo_url: str = ""
    # Peer analysis
    peers: str = ""  # "provider:model,provider:model" format
    peer_analysis_max_rounds: int = 0  # 0 = not set, use server default
    # Additional repositories for AI analysis context
    additional_repos: str = ""  # "name:url,name:url" format
    # Jenkins job monitoring
    wait_for_completion: bool | None = None
    poll_interval_minutes: int = 0  # 0 means use server default
    max_wait_minutes: int = 0  # 0 means use server default
    # Force analysis on successful builds
    force: bool | None = None
    # Admin authentication
    api_key: str = ""  # Admin API key for authentication


def _validate_section_server_field(
    section: dict, section_name: str, config_path: Path
) -> None:
    """Validate the ``server`` field inside a config section.

    Raises ``ValueError`` when the field is present but not a non-empty string.
    """
    server = section.get("server")
    if server is not None and (
        not isinstance(server, str) or not server.strip() or server != server.strip()
    ):
        raise ValueError(
            f"Invalid config in {config_path}: '{section_name}.server' must be a non-empty, trimmed string, "
            f"got {type(server).__name__}"
        )


def load_config(path: Path | None = None) -> dict:
    """Load config from TOML file.

    Args:
        path: Override config file path (used by tests). Defaults to
            ~/.config/jji/config.toml.

    Returns:
        Parsed TOML as a dict, or empty dict if file does not exist.
    """
    config_path = path or CONFIG_FILE
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Invalid TOML in {config_path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid config in {config_path}: top-level value must be a mapping, "
            f"got {type(data).__name__}"
        )

    default = data.get("default")
    if default is not None and not isinstance(default, dict):
        raise ValueError(
            f"Invalid config in {config_path}: 'default' must be a mapping, "
            f"got {type(default).__name__}"
        )
    if default is not None:
        _validate_section_server_field(default, "default", config_path)

    defaults = data.get("defaults")
    if defaults is not None and not isinstance(defaults, dict):
        raise ValueError(
            f"Invalid config in {config_path}: 'defaults' must be a mapping, "
            f"got {type(defaults).__name__}"
        )
    if defaults is not None and "server" in defaults:
        raise ValueError(
            f"Invalid config in {config_path}: 'defaults.server' is not supported; use [default].server"
        )

    servers = data.get("servers")
    if servers is not None:
        if not isinstance(servers, dict):
            raise ValueError(
                f"Invalid config in {config_path}: 'servers' must be a mapping, "
                f"got {type(servers).__name__}"
            )
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Invalid config in {config_path}: servers.{name} must be a mapping, "
                    f"got {type(entry).__name__}"
                )
            merged = _merge_server_dict(defaults or {}, entry)
            url = merged.get("url")
            if not isinstance(url, str) or not url.strip() or url != url.strip():
                raise ValueError(
                    f"Invalid config in {config_path}: servers.{name}.url must be a non-empty, trimmed string"
                )

    return data


def get_default_server_name(config: dict | None = None) -> str:
    """Return the default server name from config.

    Args:
        config: Pre-loaded config dict. If None, loads from disk.

    Returns:
        Default server name, or empty string if not set.
    """
    if config is None:
        config = load_config()
    return config.get("default", {}).get("server", "")


def _validated_str(data: dict, key: str) -> str:
    """Extract a string value from *data*, raising on type mismatch.

    ``None`` is treated as the empty-string default so that explicit
    ``key = None`` in TOML doesn't crash downstream code.
    """
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Invalid config: '{key}' must be a string")
    return value


def _validated_int(data: dict, key: str) -> int:
    """Extract an integer value from *data*, raising on type mismatch.

    ``None`` is treated as ``0`` (server default).  Booleans are rejected
    even though ``bool`` is a subclass of ``int`` in Python.
    """
    value = data.get(key, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid config: '{key}' must be an integer")
    return value


def _validated_non_negative_int(data: dict, key: str) -> int:
    value = _validated_int(data, key)
    if value < 0:
        raise ValueError(f"Invalid config: '{key}' must be a non-negative integer")
    return value


def _server_config_from_dict(data: dict) -> ServerConfig:
    """Build a ServerConfig from a TOML server dict.

    Args:
        data: The TOML dict for a single ``[servers.<name>]`` section.

    Returns:
        A fully populated ServerConfig.
    """
    return ServerConfig(
        url=data.get("url", ""),
        username=data.get("username", ""),
        no_verify_ssl=data.get("no_verify_ssl", False),
        # Jenkins
        jenkins_url=data.get("jenkins_url", ""),
        jenkins_user=data.get("jenkins_user", ""),
        jenkins_password=data.get("jenkins_password", ""),
        jenkins_ssl_verify=data.get("jenkins_ssl_verify"),
        jenkins_timeout=_validated_non_negative_int(data, "jenkins_timeout"),
        # Tests
        tests_repo_url=data.get("tests_repo_url", ""),
        tests_repo_token=data.get("tests_repo_token", ""),
        # AI
        ai_provider=data.get("ai_provider", ""),
        ai_model=data.get("ai_model", ""),
        ai_cli_timeout=data.get("ai_cli_timeout", 0),
        max_concurrent_ai_calls=_validated_non_negative_int(
            data, "max_concurrent_ai_calls"
        ),
        # Peer analysis
        peers=_validated_str(data, "peers"),
        peer_analysis_max_rounds=_validated_int(data, "peer_analysis_max_rounds"),
        # Additional repos
        additional_repos=_validated_str(data, "additional_repos"),
        # Jira
        jira_url=data.get("jira_url", ""),
        jira_email=data.get("jira_email", ""),
        jira_api_token=data.get("jira_api_token", ""),
        jira_pat=data.get("jira_pat", ""),
        jira_token=_validated_str(data, "jira_token"),
        jira_project_key=data.get("jira_project_key", ""),
        jira_security_level=_validated_str(data, "jira_security_level"),
        jira_ssl_verify=data.get("jira_ssl_verify"),
        jira_max_results=data.get("jira_max_results", 0),
        enable_jira=data.get("enable_jira"),
        # GitHub
        github_token=data.get("github_token", ""),
        github_repo_url=_validated_str(data, "github_repo_url"),
        # Jenkins job monitoring
        wait_for_completion=data.get("wait_for_completion"),
        poll_interval_minutes=data.get("poll_interval_minutes", 0),
        max_wait_minutes=data.get("max_wait_minutes", 0),
        # Force analysis on successful builds
        force=data.get("force"),
        # Admin authentication
        api_key=data.get("api_key", ""),
    )


def get_server_config(
    name: str | None = None,
    config: dict | None = None,
) -> ServerConfig | None:
    """Look up a named server, falling back to the default.

    Global ``[defaults]`` values are merged first, then server-specific
    values override them.

    Args:
        name: Server name to look up. If None, uses the default.
        config: Pre-loaded config dict. If None, loads from disk.

    Returns:
        ServerConfig if found, None otherwise.
    """
    if config is None:
        config = load_config()
    if not config:
        return None

    server_name = name or get_default_server_name(config)
    if not server_name:
        return None

    server_data = config.get("servers", {}).get(server_name)
    if server_data is None:
        return None

    return _build_server_config(config.get("defaults", {}), server_data)


def _merge_server_dict(defaults: dict, overrides: dict) -> dict:
    """Merge default settings with per-server overrides."""
    return {**defaults, **overrides}


def _build_server_config(defaults: dict, overrides: dict) -> ServerConfig:
    """Merge defaults with per-server overrides and build a ServerConfig."""
    return _server_config_from_dict(_merge_server_dict(defaults, overrides))


def list_servers(config: dict | None = None) -> dict[str, ServerConfig]:
    """Return all configured servers.

    Global ``[defaults]`` values are merged into each server entry.

    Args:
        config: Pre-loaded config dict. If None, loads from disk.

    Returns:
        Mapping of server name to ServerConfig.
    """
    if config is None:
        config = load_config()
    defaults = config.get("defaults", {})
    result: dict[str, ServerConfig] = {}
    for name, data in config.get("servers", {}).items():
        result[name] = _build_server_config(defaults, data)
    return result
