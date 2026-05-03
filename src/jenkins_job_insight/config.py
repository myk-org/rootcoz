"""Configuration settings from environment variables."""

import os
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from ai_cli_runner import VALID_AI_PROVIDERS
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


def parse_peer_configs(raw: str) -> list[dict]:
    """Parse 'provider:model,provider:model' into list of dicts.

    Raises ValueError on malformed input. Empty string returns [].
    """
    if not raw or not raw.strip():
        return []
    result = []
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            raise ValueError(f"Empty entry at position {i + 1} in peer config: '{raw}'")
        if ":" not in entry:
            raise ValueError(
                f"Invalid peer config at position {i + 1}: '{entry}' (expected 'provider:model')"
            )
        provider, model = entry.split(":", 1)
        provider, model = provider.strip(), model.strip()
        if not provider:
            raise ValueError(f"Empty provider at position {i + 1}: '{entry}'")
        if not model:
            raise ValueError(f"Empty model at position {i + 1}: '{entry}'")
        if provider not in VALID_AI_PROVIDERS:
            raise ValueError(
                f"Unsupported provider '{provider}' at position {i + 1}. Valid: {', '.join(sorted(VALID_AI_PROVIDERS))}"
            )
        result.append({"ai_provider": provider, "ai_model": model})
    return result


def parse_additional_repos(raw: str) -> list[dict]:
    """Parse 'name:url,name:url' or 'name:url:ref@token' into list of dicts.

    Token is separated from the URL (or URL:ref) by ``@token`` at the end.
    To specify a token without a ref, use ``name:https://host/repo@token``.
    To specify both ref and token, use ``name:https://host/repo:ref@token``.

    Raises ValueError on malformed input. Empty string returns [].
    """
    if not raw or not raw.strip():
        return []
    result = []
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            raise ValueError(f"Empty entry at position {i + 1} in additional repos")
        if ":" not in entry:
            raise ValueError(
                f"Invalid additional repo at position {i + 1} (expected 'name:url')"
            )
        name, url_raw = entry.split(":", 1)
        name = name.strip()
        url_raw = url_raw.strip()
        if not name:
            raise ValueError(f"Empty name at position {i + 1}")
        if not url_raw:
            raise ValueError(f"Empty URL at position {i + 1}")
        # Extract token: look for @token after the path (not in the netloc)
        token = _extract_token_from_url_spec(url_raw)
        if token:
            # Remove the @token suffix from url_raw
            url_raw = url_raw[: url_raw.rfind("@" + token)]
        url, ref = parse_repo_ref(url_raw)
        entry_dict: dict = {"name": name, "url": url, "ref": ref}
        if token:
            entry_dict["token"] = token
        result.append(entry_dict)

    names = [r["name"] for r in result]
    dupes = [n for n in names if names.count(n) > 1]
    if dupes:
        raise ValueError(
            f"Duplicate additional repo names: {', '.join(sorted(set(dupes)))}"
        )

    return result


def _extract_token_from_url_spec(url_spec: str) -> str:
    """Extract a token from a URL spec like 'https://host/repo@token'.

    The token is the part after the last '@' that appears after the
    URL's netloc (i.e., in the path portion). Returns empty string
    if no token is found.
    """
    parts = urlsplit(url_spec)
    # Check for @token in the path portion (after netloc)
    # The token is the last @-separated segment of the full path+ref portion
    full_after_netloc = url_spec
    if parts.scheme and parts.netloc:
        scheme_netloc = f"{parts.scheme}://{parts.netloc}"
        full_after_netloc = url_spec[len(scheme_netloc) :]

    if "@" not in full_after_netloc:
        return ""

    # The token is everything after the last '@' in the path portion
    candidate = full_after_netloc.rsplit("@", 1)[1]
    # Token should not contain '/' or ':' (those indicate it's part of the URL)
    if "/" in candidate or ":" in candidate or not candidate:
        return ""
    return candidate


def parse_repo_ref(raw: str) -> tuple[str, str]:
    """Extract git ref from a URL string.

    Format: 'url:ref' where ref is appended after the repo path with a colon.
    Examples:
        'https://github.com/org/repo:develop' -> ('https://github.com/org/repo', 'develop')
        'https://github.com/org/repo:feature/foo' -> ('https://github.com/org/repo', 'feature/foo')
        'https://github.com/org/repo' -> ('https://github.com/org/repo', '')
        'https://gitlab.internal:8443/org/repo:v1.0.0' -> ('https://gitlab.internal:8443/org/repo', 'v1.0.0')
        '' -> ('', '')
    """
    if not raw or not raw.strip():
        return ("", "")
    raw = raw.strip()

    parts = urlsplit(raw)
    path = parts.path or ""
    if ":" in path:
        repo_path, ref = path.split(":", 1)
        clean_url = urlunsplit(
            (parts.scheme, parts.netloc, repo_path, parts.query, parts.fragment)
        )
        return (clean_url, ref)
    return (raw, "")


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Claude Code CLI configuration (set by container environment)
    # These env vars are read by the claude CLI, not by this application:
    # - CLAUDE_CODE_USE_VERTEX=1
    # - CLOUD_ML_REGION=<region>
    # - ANTHROPIC_VERTEX_PROJECT_ID=<project>

    # Jenkins configuration (optional; can be provided per-request via API body).
    # Empty string means "not configured"; checked with `if not self.jenkins_url`.
    jenkins_url: str = ""
    jenkins_user: str = ""
    jenkins_password: str = Field(default="", repr=False)
    jenkins_ssl_verify: bool = True
    jenkins_timeout: int = Field(
        default=30, gt=0, description="Jenkins API request timeout in seconds"
    )

    # Optional defaults (can be overridden per-request in webhook)
    tests_repo_url: str | None = None
    tests_repo_token: SecretStr | None = None  # NEW
    # Jira integration (optional)
    jira_url: str | None = None
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    jira_pat: SecretStr | None = None
    jira_project_key: str | None = None
    jira_ssl_verify: bool = True
    jira_max_results: int = Field(default=5, gt=0)

    # Explicit Jira toggle (optional)
    enable_jira: bool | None = None

    # Explicit GitHub issue creation toggle (optional)
    enable_github_issues: bool | None = Field(
        default=None,
        description="Enable GitHub issue creation. When None, enabled if TESTS_REPO_URL and GITHUB_TOKEN are configured.",
    )

    # Explicit Jira issue creation toggle (optional)
    enable_jira_issues: bool | None = Field(
        default=None,
        description="Enable Jira bug creation. When None, defaults to enabled. Independent of enable_jira.",
    )

    # AI CLI timeout in minutes
    ai_cli_timeout: int = Field(default=10, gt=0)

    # Max concurrent AI CLI calls
    max_concurrent_ai_calls: int = Field(default=3, gt=0)

    # Peer analysis configuration
    peer_ai_configs: str = ""  # "provider:model,provider:model" format
    peer_analysis_max_rounds: int = Field(default=3, ge=1, le=10)

    # Additional repositories for AI analysis context
    additional_repos: str = ""  # "name:url,name:url" format

    # Jenkins artifacts configuration
    jenkins_artifacts_max_size_mb: int = Field(default=500, gt=0)

    # Artifact download toggle
    get_job_artifacts: bool = True

    # Force analysis on successful builds
    force_analysis: bool = False

    # Jenkins job monitoring (wait for completion before analysis)
    wait_for_completion: bool = True
    poll_interval_minutes: int = Field(default=2, gt=0)
    max_wait_minutes: int = Field(default=0, ge=0)

    # Allow list — comma-separated usernames allowed to submit/modify data.
    # Empty means open access (all users allowed). Admin users always bypass.
    allowed_users: str = Field(
        default="",
        description=(
            "Comma-separated list of usernames allowed to create/modify data. "
            "Empty = open access (no restriction). Admin users always bypass."
        ),
    )

    # Admin authentication
    admin_key: str = Field(
        default="", repr=False
    )  # JJI_ADMIN_KEY — bootstraps admin superuser
    secure_cookies: bool = True  # Set to False for local HTTP dev

    # Trust reverse-proxy headers (e.g., X-Forwarded-User from OAuth proxy).
    # When enabled, auto-identifies users from the X-Forwarded-User header.
    # Only enable behind a trusted reverse proxy (e.g., OpenShift oauth-proxy).
    trust_proxy_headers: bool = False

    # Trusted public base URL — used for result_url and tracker links.
    # When set, _extract_base_url() returns this value verbatim.
    # When unset, _extract_base_url() returns an empty string (relative
    # URLs only) — request Host / X-Forwarded-* headers are never trusted.
    public_base_url: str | None = None

    # GitHub (optional) -- for comment enrichment (PR status)
    github_token: SecretStr | None = None

    # Report Portal integration (optional)
    reportportal_url: str | None = None
    reportportal_api_token: SecretStr | None = None
    reportportal_project: str | None = None
    reportportal_verify_ssl: bool = Field(
        default=True,
        description="Verify SSL certificates for Report Portal connections. Set to False for self-signed certs.",
    )
    enable_reportportal: bool | None = Field(
        default=None,
        description="Enable Report Portal integration. When None, enabled if REPORTPORTAL_URL, REPORTPORTAL_API_TOKEN, and REPORTPORTAL_PROJECT are configured.",
    )

    # Web Push (VAPID) configuration (optional, server-only)
    vapid_public_key: str = ""
    vapid_private_key: str = Field(default="", repr=False)
    vapid_claim_email: str = ""

    # Metadata rules file path (optional, server-only)
    metadata_rules_file: str = Field(
        default="",
        description="Path to a YAML/JSON file defining name-based metadata rules for auto-assignment",
    )

    @model_validator(mode="after")
    def _normalize_optional_strings(self) -> "Settings":
        """Strip whitespace from optional string fields; blank becomes None."""
        for field_name in (
            "tests_repo_url",
            "jira_url",
            "jira_email",
            "jira_project_key",
            "public_base_url",
            "reportportal_url",
            "reportportal_project",
        ):
            value = getattr(self, field_name)
            if isinstance(value, str):
                stripped = value.strip()
                object.__setattr__(self, field_name, stripped or None)
        # Strip whitespace from Jenkins credentials (empty-string defaults)
        for field_name in ("jenkins_url", "jenkins_user", "jenkins_password"):
            value = getattr(self, field_name)
            if isinstance(value, str):
                object.__setattr__(self, field_name, value.strip())
        # Strip whitespace from secret fields; blank becomes None
        for field_name in (
            "github_token",
            "tests_repo_token",
            "jira_api_token",
            "jira_pat",
            "reportportal_api_token",
        ):
            secret = getattr(self, field_name)
            if secret is not None:
                stripped = secret.get_secret_value().strip()
                object.__setattr__(
                    self,
                    field_name,
                    SecretStr(stripped) if stripped else None,
                )
        return self

    @property
    def allowed_users_set(self) -> frozenset[str]:
        """Parse ALLOWED_USERS into a frozen set of lowercase usernames.

        Returns an empty frozenset when unset (open access).
        """
        if not self.allowed_users or not self.allowed_users.strip():
            return frozenset()
        return frozenset(
            u.strip().lower() for u in self.allowed_users.split(",") if u.strip()
        )

    @property
    def jira_enabled(self) -> bool:
        """Check if Jira integration is enabled and configured with valid credentials."""
        if self.enable_jira is False:
            return False
        if not self.jira_url:
            if self.enable_jira is True:
                logger.warning("enable_jira is True but JIRA_URL is not configured")
            return False
        _, token_value = _resolve_jira_auth(self)
        if not token_value:
            if self.enable_jira is True:
                logger.warning(
                    "enable_jira is True but no Jira credentials are configured"
                )
            return False
        if not self.jira_project_key:
            if self.enable_jira is True:
                logger.warning(
                    "enable_jira is True but JIRA_PROJECT_KEY is not configured"
                )
            return False
        return True

    @property
    def github_issues_enabled(self) -> bool:
        """Check if GitHub issue creation is enabled and configured."""
        if self.enable_github_issues is False:
            return False
        tests_repo_url = str(self.tests_repo_url) if self.tests_repo_url else ""
        github_token = self.github_token.get_secret_value() if self.github_token else ""
        if self.enable_github_issues is True:
            if not tests_repo_url:
                logger.warning(
                    "enable_github_issues is True but TESTS_REPO_URL is not configured"
                )
            if not github_token:
                logger.warning(
                    "enable_github_issues is True but GITHUB_TOKEN is not configured"
                )
        return bool(tests_repo_url and github_token)

    @property
    def feedback_enabled(self) -> bool:
        """Check if feedback submission is enabled.

        Requires ENABLE_GITHUB_ISSUES to not be explicitly False and
        GITHUB_TOKEN to be configured. Unlike github_issues_enabled,
        does not require TESTS_REPO_URL since feedback issues go to
        the hardcoded project repo.
        """
        if self.enable_github_issues is False:
            return False
        return bool(self.github_token and self.github_token.get_secret_value())

    @property
    def web_push_enabled(self) -> bool:
        """Check if Web Push is enabled (env vars or auto-generated keys)."""
        if hasattr(self, "_vapid_config_cache"):
            return bool(self._vapid_config_cache)

        pub = self.vapid_public_key.strip()
        priv = self.vapid_private_key.strip()

        # Detect partial env config
        if bool(pub) != bool(priv):
            logger.warning(
                "Partial VAPID configuration: only one of VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY is set. "
                "Both must be provided, or neither (auto-generation will be used)."
            )

        if pub and priv:
            object.__setattr__(self, "_vapid_config_cache", True)
            return True

        from jenkins_job_insight.vapid import get_vapid_config

        result = bool(get_vapid_config())
        object.__setattr__(self, "_vapid_config_cache", result)
        return result

    @property
    def metadata_rules(self) -> list[dict]:
        """Load and cache metadata rules from the configured file.

        Rules are cached for the process lifetime.  Changes to the rules
        file require a server restart to take effect.

        Returns an empty list when no file is configured or on load errors.
        """
        if hasattr(self, "_metadata_rules_cache"):
            return self._metadata_rules_cache

        path = self.metadata_rules_file.strip()
        if not path:
            object.__setattr__(self, "_metadata_rules_cache", [])
            return []

        try:
            from jenkins_job_insight.metadata_rules import load_metadata_rules

            rules = load_metadata_rules(path)
        except Exception:  # noqa: BLE001 — never crash the app on bad rule config
            logger.warning("Failed to load metadata rules from %s", path, exc_info=True)
            rules = []

        object.__setattr__(self, "_metadata_rules_cache", rules)
        return rules

    @property
    def reportportal_enabled(self) -> bool:
        """Check if Report Portal integration is enabled and configured."""
        if self.enable_reportportal is False:
            return False
        if not self.reportportal_url:
            if self.enable_reportportal is True:
                logger.warning(
                    "enable_reportportal is True but REPORTPORTAL_URL is not configured"
                )
            return False
        if (
            not self.reportportal_api_token
            or not self.reportportal_api_token.get_secret_value()
        ):
            if self.enable_reportportal is True:
                logger.warning(
                    "enable_reportportal is True but REPORTPORTAL_API_TOKEN is not configured"
                )
            return False
        if not self.reportportal_project:
            if self.enable_reportportal is True:
                logger.warning(
                    "enable_reportportal is True but REPORTPORTAL_PROJECT is not configured"
                )
            return False
        return True


def _resolve_jira_auth(settings: Settings) -> tuple[bool, str]:
    """Resolve Jira authentication mode and token value.

    Determines Cloud vs Server/DC deployment first, then selects the
    appropriate credential.

    Cloud mode (``is_cloud=True``) is detected when ``jira_email`` is
    set.  The token is selected by preferring ``jira_api_token`` and
    falling back to ``jira_pat``.

    Server/DC mode (no ``jira_email``) prefers ``jira_pat`` and falls
    back to ``jira_api_token`` only when PAT is absent.

    Returns:
        Tuple of (is_cloud, token_value).  ``token_value`` is empty when
        no credentials are configured.
    """
    has_api_token = bool(
        settings.jira_api_token and settings.jira_api_token.get_secret_value()
    )
    has_pat = bool(settings.jira_pat and settings.jira_pat.get_secret_value())
    has_email = bool(settings.jira_email)

    # email present = Cloud; use api_token first, fall back to pat
    if has_email:
        if has_api_token:
            return True, settings.jira_api_token.get_secret_value()  # type: ignore[union-attr]
        if has_pat:
            return True, settings.jira_pat.get_secret_value()  # type: ignore[union-attr]
        return True, ""

    # No email = Server/DC; prefer PAT, fall back to API token
    if has_pat and settings.jira_pat:
        return False, settings.jira_pat.get_secret_value()
    if has_api_token and settings.jira_api_token:
        return False, settings.jira_api_token.get_secret_value()

    return False, ""


@lru_cache
def get_settings() -> Settings:
    """Get application settings instance."""
    return Settings()
