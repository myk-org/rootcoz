"""Configuration settings from environment variables."""

import os
from functools import lru_cache
from typing import Any, NamedTuple
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from rootcoz import get_child_logger
from rootcoz.ai_client import normalize_provider
from rootcoz.metadata_rules import load_metadata_rules
from rootcoz.vapid import get_vapid_config

logger = get_child_logger(__name__)

# Defaults for the Prow artifact download limits (see ``Settings``).
# Single source of truth — ``rootcoz.sources.prow_source`` reuses them as
# fallback limits for callers that do not pass settings.
PROW_ARTIFACTS_MAX_SIZE_MB = 50
PROW_ARTIFACT_MAX_FILE_SIZE_MB = 10
PROW_ARTIFACTS_MAX_OBJECTS = 5_000

# Valid values for the explicit ``jira_auth_mode`` setting.
JIRA_AUTH_MODES = ("oauth", "basic", "pat")


def _split_outside_brackets(raw: str) -> list[str]:
    """Split string on commas that are not inside square brackets."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in raw:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            if depth < 0:
                raise ValueError("Unmatched closing bracket in peer config")
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if depth != 0:
        raise ValueError("Unmatched opening bracket in peer config")
    parts.append("".join(current))
    return parts


def parse_peer_configs(raw: str) -> list[dict[str, str]]:
    """Parse 'provider:model,provider:model' into list of dicts.

    Model names may contain commas inside square brackets
    (e.g. ``cursor:gpt-5.4[context=272k,reasoning=medium]``).

    Raises ValueError on malformed input. Empty string returns [].
    """
    if not raw or not raw.strip():
        return []
    result = []
    for i, entry in enumerate(_split_outside_brackets(raw)):
        entry = entry.strip()
        if not entry:
            raise ValueError(f"Empty entry at position {i + 1} in peer config: '{raw}'")
        if ":" not in entry:
            raise ValueError(
                f"Invalid peer config at position {i + 1}: '{entry}' (expected 'provider:model')"
            )
        provider, model = entry.split(":", 1)
        provider, model = normalize_provider(provider.strip()), model.strip()
        if not provider:
            raise ValueError(f"Empty provider at position {i + 1}: '{entry}'")
        if not model:
            raise ValueError(f"Empty model at position {i + 1}: '{entry}'")
        result.append({"ai_provider": provider, "ai_model": model})
    return result


def parse_additional_repos(raw: str) -> list[dict[str, Any]]:
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
        entry_dict: dict[str, Any] = {"name": name, "url": url, "ref": ref}
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


class ReportPortalConfig(NamedTuple):
    """Typed access to Report Portal settings.

    ``api_token`` is kept as :class:`SecretStr` to preserve redaction
    guarantees — callers must call ``.get_secret_value()`` at the point
    of use (e.g. when constructing the RP client).
    """

    url: str | None
    api_token: SecretStr | None
    project: str | None
    verify_ssl: bool
    enabled: bool
    push_classifications: bool
    push_rootcoz_url: bool
    push_tracker_links: bool


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
    # Jira integration (optional). Each auth method is a fully
    # self-contained definition — its own URL and TLS verification. The
    # ONLY settings shared across methods: jira_project_key,
    # jira_max_results, jira_site_url, enable_jira, enable_jira_issues.
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    jira_pat: SecretStr | None = None
    jira_project_key: str | None = None
    jira_max_results: int = Field(default=5, gt=0)
    # basic method (Jira Cloud email + API token)
    jira_cloud_url: str = Field(
        default="",
        description=(
            "Jira Cloud site URL for the basic auth method (email + API token)."
        ),
    )
    jira_cloud_ssl_verify: bool = Field(
        default=True,
        description=(
            "Verify TLS certificates for Jira Cloud basic-auth connections "
            "(basic method). Set to False for self-signed certs."
        ),
    )
    # pat method (Jira Server/DC bearer PAT)
    jira_server_url: str = Field(
        default="",
        description=(
            "Jira Server/Data Center base URL for the pat auth method "
            "(bearer personal access token)."
        ),
    )
    jira_server_ssl_verify: bool = Field(
        default=True,
        description=(
            "Verify TLS certificates for Jira Server/DC PAT connections "
            "(pat method). Set to False for self-signed certs."
        ),
    )
    # oauth method (Atlassian OAuth 2.0 credential)
    jira_oauth_ssl_verify: bool = Field(
        default=True,
        description=(
            "Verify TLS certificates for Atlassian OAuth 2.0 connections "
            "(oauth method: token minting and the api.atlassian.com "
            "gateway)."
        ),
    )
    # Atlassian "OAuth 2.0 credential for service accounts" — a third,
    # additional connection method alongside Cloud basic auth and Server/DC
    # PAT. The stored value is the credential's permanent CLIENT SECRET
    # (ATOA-prefixed); short-lived access tokens are minted from it via
    # client_credentials at auth.atlassian.com (see rootcoz.jira).
    jira_oauth_client_secret: SecretStr | None = None
    atlassian_oauth_cloud_id: str | None = None
    # Client ID of the OAuth 2.0 credential — required together with the
    # client secret to mint access tokens for the oauth auth method.
    atlassian_oauth_client_id: str | None = None
    # Site URL for browse links (shared across methods; the scoped
    # api.atlassian.com gateway URL of the oauth method cannot yield one).
    jira_site_url: str | None = None
    # DB-only Jira auth method selection (Server Settings UI) — REQUIRED
    # for the Jira integration. When set, ONLY that method's fields are
    # used. Unset disables Jira entirely: no credentials are read, no
    # inference. Never read from the environment (see
    # settings_customise_sources); set via PUT /api/admin/settings.
    jira_auth_mode: str | None = Field(
        default=None,
        description=(
            "Explicit Jira auth method: 'oauth', 'basic', or 'pat' "
            "(Server Settings only). Unset disables the Jira integration "
            "(no auto-detection)."
        ),
    )

    # Explicit Jira toggle (optional)
    enable_jira: bool | None = None

    # Explicit GitHub issue creation toggle (optional)
    enable_github_issues: bool | None = Field(
        default=None,
        description=(
            "Enable GitHub issue creation."
            " When None, enabled if TESTS_REPO_URL and GITHUB_TOKEN are configured."
        ),
    )

    # Explicit Jira issue creation toggle (optional)
    enable_jira_issues: bool | None = Field(
        default=None,
        description="Enable Jira bug creation. When None, defaults to enabled. Independent of enable_jira.",
    )

    # AI timeout in minutes
    ai_call_timeout: int = Field(default=10, gt=0)

    # Max concurrent AI calls
    max_concurrent_ai_calls: int = Field(default=3, gt=0)

    # Default AI provider (server-level default, can be overridden per-request)
    force_server_credentials: bool = False
    ai_provider: str = ""
    # Default AI model (server-level default, can be overridden per-request)
    ai_model: str = ""

    # pi-sidecar session transcript recording (sidecar >= 4.12.0).
    # Runtime-toggleable through the sidecar's /transcripts/recording
    # endpoint (pi-sidecar >= 4.14.0): a Server Settings change applies to
    # the running sidecar immediately. The env bridge still persists the
    # DB value for the next boot.
    record_transcripts: bool = Field(
        default=False,
        description=(
            "Record pi-sidecar session transcripts (JSONL, redacted) under "
            "/data for admin-only retrieval. Applies immediately via the "
            "sidecar runtime toggle (pi-sidecar >= 4.14.0) — no restart "
            "needed."
        ),
    )

    # Peer analysis configuration
    peer_ai_configs: str = ""  # "provider:model,provider:model" format
    peer_analysis_max_rounds: int = Field(default=3, ge=1, le=10)

    # Additional repositories for AI analysis context
    additional_repos: str = ""  # "name:url,name:url" format
    max_concurrent_repo_clones: int = Field(
        default=10,
        gt=0,
        description="Maximum simultaneous additional-repository clones",
    )

    # Jenkins artifacts configuration
    jenkins_artifacts_max_size_mb: int = Field(default=500, gt=0)

    # Artifact download toggle
    get_job_artifacts: bool = True

    # Prow configuration (optional; can be provided per-request via API body)
    prow_url: str = Field(
        default="",
        description="Default Prow Deck URL (e.g. https://prow.ci.openshift.org)",
    )
    gcs_bucket: str = Field(
        default="",
        description="Default GCS bucket for Prow artifacts (e.g. test-platform-results)",
    )

    # Prow artifact download limits (server-level deployment settings).
    prow_artifacts_max_size_mb: int = Field(
        default=PROW_ARTIFACTS_MAX_SIZE_MB,
        gt=0,
        description="Total budget in MB for downloaded Prow artifacts",
    )
    prow_artifact_max_file_size_mb: int = Field(
        default=PROW_ARTIFACT_MAX_FILE_SIZE_MB,
        gt=0,
        description="Max size in MB of a single downloaded Prow artifact",
    )
    prow_artifacts_max_objects: int = Field(
        default=PROW_ARTIFACTS_MAX_OBJECTS,
        gt=0,
        description="Max number of GCS objects listed per Prow build",
    )

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

    # Default role for new user registrations
    default_user_role: str = Field(
        default="reviewer",
        description=(
            "Default role assigned to new user registrations. "
            "Must be 'viewer', 'reviewer', or 'operator'. Defaults to 'reviewer'."
        ),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Drop ``jira_auth_mode`` from the env/dotenv sources.

        The auth method is selected ONLY in Server Settings (DB); the
        ``JIRA_AUTH_MODE`` environment variable is deliberately ignored.
        Init kwargs (DB-override merge in ``get_settings()``, admin PUT
        validation) and ``model_copy`` pinning are unaffected.
        """

        def _without_auth_mode(
            source: PydanticBaseSettingsSource,
        ) -> PydanticBaseSettingsSource:
            def _load() -> dict[str, Any]:
                data = source()
                data.pop("jira_auth_mode", None)

                def _alias_value(alias: str) -> str | None:
                    # Aliases resolve from os.environ (the env source) AND
                    # the source's own data: the dotenv source surfaces
                    # unknown keys from the .env file in ``data`` (later
                    # dropped by ``extra='ignore'``), so aliases present
                    # ONLY in .env are still readable here instead of being
                    # silently lost.
                    env_value = os.environ.get(alias)
                    if env_value is not None:
                        return env_value
                    return data.get(alias.lower()) or None

                # Deprecated alias: JIRA_OAUTH_ACCESS_TOKEN feeds the renamed
                # jira_oauth_client_secret when the canonical env var is
                # unset. The canonical name always wins once set.
                alias_secret = _alias_value("JIRA_OAUTH_ACCESS_TOKEN")
                if "jira_oauth_client_secret" not in data and alias_secret:
                    data["jira_oauth_client_secret"] = alias_secret
                # Deprecated aliases for the removed shared settings: JIRA_URL
                # feeds both per-method URL fields and JIRA_SSL_VERIFY all
                # three per-method verify flags, each only when its canonical
                # env var is unset. Canonical names always win once set.
                alias_url = _alias_value("JIRA_URL")
                if alias_url:
                    for target in ("jira_cloud_url", "jira_server_url"):
                        if target not in data:
                            data[target] = alias_url
                alias_ssl = _alias_value("JIRA_SSL_VERIFY")
                if alias_ssl:
                    for target in (
                        "jira_oauth_ssl_verify",
                        "jira_cloud_ssl_verify",
                        "jira_server_ssl_verify",
                    ):
                        if target not in data:
                            data[target] = alias_ssl
                return data

            return _load

        return (
            init_settings,
            _without_auth_mode(env_settings),
            _without_auth_mode(dotenv_settings),
            file_secret_settings,
        )

    @field_validator("jira_auth_mode")
    @classmethod
    def _validate_jira_auth_mode(cls, value: str | None) -> str | None:
        """Accept only the three known methods (or empty — Jira disabled)."""
        mode = (value or "").strip().lower()
        if mode and mode not in JIRA_AUTH_MODES:
            raise ValueError(
                f"jira_auth_mode must be one of 'oauth', 'basic', 'pat' (got {value!r})"
            )
        return mode or None

    @field_validator("default_user_role")
    @classmethod
    def _validate_default_role(cls, v: str) -> str:
        allowed = ("viewer", "reviewer", "operator")
        if v not in allowed:
            raise ValueError(f"DEFAULT_USER_ROLE must be one of: {', '.join(allowed)}")
        return v

    @field_validator("prow_url", mode="before")
    @classmethod
    def _validate_prow_url(cls, v: object) -> str:
        from rootcoz.prow_validation import normalize_prow_url

        return normalize_prow_url(v)

    @field_validator("gcs_bucket", mode="before")
    @classmethod
    def _validate_gcs_bucket(cls, v: object) -> str:
        from rootcoz.prow_validation import normalize_gcs_bucket

        return normalize_gcs_bucket(v)

    # Admin authentication
    admin_key: str = Field(
        default="", repr=False
    )  # ROOTCOZ_ADMIN_KEY — bootstraps admin superuser
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
    # Flat fields for env var + settings UI compatibility; use `rp` property for typed access.
    reportportal_url: str | None = None
    reportportal_api_token: SecretStr | None = None
    reportportal_project: str | None = None
    reportportal_verify_ssl: bool = Field(
        default=True,
        description="Verify SSL certificates for Report Portal connections. Set to False for self-signed certs.",
    )
    enable_reportportal: bool | None = Field(
        default=None,
        description=(
            "Enable Report Portal integration."
            " When None, enabled if REPORTPORTAL_URL, REPORTPORTAL_API_TOKEN,"
            " and REPORTPORTAL_PROJECT are configured."
        ),
    )
    rp_push_classifications: bool = Field(
        default=True,
        description=(
            "Include classification (defect type mapping) when pushing to Report Portal. "
            "Maps rootcoz classifications to RP defect types (PRODUCT_BUG, AUTOMATION_BUG, SYSTEM_ISSUE)."
        ),
    )
    rp_push_rootcoz_url: bool = Field(
        default=True,
        description="Include rootcoz analysis URL as a comment on Report Portal test items.",
    )
    rp_push_tracker_links: bool = Field(
        default=True,
        description="Include Jira/GitHub issue links as external system issues on Report Portal test items.",
    )

    # Web Push (VAPID) configuration (optional, server-only)
    vapid_public_key: str = ""
    vapid_private_key: str = Field(default="", repr=False)
    vapid_claim_email: str = ""

    # Free AI models: when enabled, every user may run admin-declared free
    # provider+model pairs on server credentials (the free-pair list lives in
    # the free_ai_models table; users with the server-providers grant already
    # have the superset)
    enable_free_models: bool = Field(
        default=False,
        description=(
            "When enabled, all users may use the admin-declared free "
            "provider+model pairs with server credentials. Free pairs are "
            "managed on the Users page (Free AI models) or via "
            "rootcoz admin models free-list."
        ),
    )

    ambient_auth_providers: str = Field(
        default="google-vertex-claude",
        description=(
            "Comma-separated provider IDs that authenticate via ambient "
            "credentials (ADC, OAuth, CLI login) and never consume per-session "
            "user API keys. Excluded from the user credential flow regardless "
            "of sidecar capability reporting; their models remain available "
            "with server credentials."
        ),
    )

    active_server_providers: str = Field(
        default="",
        description=(
            "Comma-separated server provider IDs exposed to users on server "
            "credentials (model catalog, admin-declared free pairs, and "
            "ambient-auth models). Empty = all providers. A deactivated "
            "provider's models lose their server credential source — free "
            "pairs on it stop working with server credentials too — but stay "
            "usable with a user's own API key."
        ),
    )

    # Auto-review: automatically mark failures as reviewed when they match
    # a previous human-reviewed failure with the same error signature
    enable_auto_review: bool = Field(
        default=True,
        description=(
            "When enabled, failures with the same job_name, test_name, and "
            "error_signature as a previous human-reviewed failure are "
            "automatically marked as reviewed."
        ),
    )

    auto_push_exporters: str = Field(
        default="",
        description=(
            "Comma-separated list of exporter plugin names to auto-push to when all "
            "failures are reviewed. Example: 'reportportal'. Empty string disables "
            "auto-push. Only takes effect when enable_auto_review is also enabled."
        ),
    )

    # Metadata rules file path (optional, server-only)
    metadata_rules_file: str = Field(
        default="",
        description="Path to a YAML/JSON file defining name-based metadata rules for auto-assignment",
    )

    # Log level for rootcoz loggers (restart-required: applied at startup).
    # Empty = LOG_LEVEL env / library default.
    log_level: str = Field(
        default="",
        description=(
            "Logging level for rootcoz loggers (DEBUG, INFO, WARNING, ERROR, "
            "CRITICAL). Applied at server startup — restart required."
        ),
    )

    # --- Sidecar settings (#347) ---
    # The sidecar starts once at container boot; these DB overrides reach it
    # through the /data/pi-sidecar/env bridge the entrypoint sources. Empty
    # string = no override: the deployment environment (compose/Helm) governs.
    # Restart required — the running processes keep their boot values.
    sidecar_host: str = Field(
        default="",
        description=(
            "Bind host for the pi-sidecar server (SIDECAR_HOST). Non-loopback "
            "values are warned about on save — same trust class as "
            "trust_proxy_headers. Restart required."
        ),
    )
    sidecar_port: str = Field(
        default="",
        description="Port for the pi-sidecar server (SIDECAR_PORT). Restart required.",
    )
    sidecar_url: str = Field(
        default="",
        description="Base URL rootcoz uses to reach the sidecar (SIDECAR_URL). Restart required.",
    )
    acpx_agents: str = Field(
        default="",
        description="ACPX agent spec for the sidecar (ACPX_AGENTS). Restart required.",
    )
    cli_agents: str = Field(
        default="",
        description="CLI agent spec for the sidecar (CLI_AGENTS). Restart required.",
    )
    pi_sidecar_log_level: str = Field(
        default="",
        description="Log level for the pi-sidecar server (PI_SIDECAR_LOG_LEVEL). Restart required.",
    )
    node_binary: str = Field(
        default="",
        description=(
            "Node binary used to launch the pi-sidecar process (NODE_BINARY). "
            "Restart required. Controls the sidecar process only — nested "
            "processes spawned inside pi-config follow the sidecar's own "
            "binary (upstream behavior) and cannot be retargeted separately."
        ),
    )
    pi_sidecar_transcripts_dir: str = Field(
        default="",
        description=(
            "Directory for pi-sidecar session transcripts "
            "(PI_SIDECAR_TRANSCRIPTS_DIR). Restart required."
        ),
    )

    # --- Notifications (#347) ---
    smtp_host: str = Field(default="", description="SMTP server host for email alerts.")
    smtp_port: int = Field(default=587, gt=0, description="SMTP server port.")
    smtp_user: str = Field(default="", description="SMTP username.")
    smtp_password: SecretStr | None = Field(
        default=None, description="SMTP password.", repr=False
    )
    smtp_from: str = Field(
        default="", description="From address for email alerts. Defaults to smtp_user."
    )

    @field_validator("smtp_port", mode="before")
    @classmethod
    def _coerce_smtp_port(cls, v: object) -> int:
        """Tolerate a malformed SMTP_PORT env instead of failing Settings().

        Mirrors ``_coerce_session_ttl``: a typo in the deployment env must
        not brick app-wide settings; fall back to 587 with a warning and
        let the (strict) field constraint reject bad admin-typed values.
        """
        if isinstance(v, bool):
            port: int | None = None
        elif isinstance(v, int):
            port = v
        elif isinstance(v, str):
            try:
                port = int(v)
            except ValueError:
                port = None
        else:
            port = None
        if port is None or port <= 0:
            logger.warning("Ignoring invalid SMTP_PORT value %r; using default 587", v)
            return 587
        return port

    slack_webhook_url: SecretStr | None = Field(
        default=None, description="Slack webhook URL for alerts.", repr=False
    )
    alert_email_to: str = Field(
        default="", description="Recipient address for email alerts."
    )

    # --- Auth (#347) ---
    # Upper bound keeps datetime arithmetic in create_session/renew_session
    # well inside datetime.max even for the longest-lived deployment
    # (10 years); without it an out-of-range value could brick every login.
    session_ttl_hours: int = Field(
        default=720,
        ge=1,
        le=87600,
        description="Login session lifetime in hours (SESSION_TTL_HOURS).",
    )

    @field_validator("session_ttl_hours", mode="before")
    @classmethod
    def _coerce_session_ttl(cls, v: object) -> int:
        """Tolerate bad values like the env parser always did (boot default).

        Env/DB values are clamped into the 1–87600 range so ``Settings()``
        never fails app-wide; the PUT path enforces the same bound with a
        400 via the field constraints.
        """
        if isinstance(v, bool):
            return 720
        if isinstance(v, int):
            return min(max(v, 1), 87600)
        if isinstance(v, str):
            try:
                return min(max(int(v), 1), 87600)
            except ValueError:
                return 720
        return 720

    # Admin approval for new user registrations
    require_approval: bool = Field(
        default=True,
        description=(
            "When True, new user registrations require admin approval. "
            "Users are created with 'pending' status and cannot access "
            "protected endpoints until approved."
        ),
    )
    admin_wait_approve_msg: str = Field(
        default="",
        description=(
            "Custom message appended to admin approval notices. "
            "Used to tell users how to get approved (e.g., 'Contact @admin in Slack')."
        ),
    )

    @model_validator(mode="after")
    def _normalize_optional_strings(self) -> Settings:
        """Strip whitespace from optional string fields; blank becomes None."""
        for field_name in (
            "tests_repo_url",
            "jira_email",
            "jira_project_key",
            "atlassian_oauth_cloud_id",
            "atlassian_oauth_client_id",
            "jira_site_url",
            "public_base_url",
            "reportportal_url",
            "reportportal_project",
        ):
            value = getattr(self, field_name)
            if isinstance(value, str):
                stripped = value.strip()
                object.__setattr__(self, field_name, stripped or None)
        # Strip whitespace from string fields with empty-string defaults
        for field_name in (
            "jenkins_url",
            "jenkins_user",
            "jenkins_password",
            "admin_wait_approve_msg",
            "ai_provider",
            "ai_model",
            "prow_url",
            "gcs_bucket",
            "jira_cloud_url",
            "jira_server_url",
        ):
            value = getattr(self, field_name)
            if isinstance(value, str):
                object.__setattr__(self, field_name, value.strip())
        # Normalize ai_provider (lowercase + legacy *-cli → canonical)
        if self.ai_provider:
            object.__setattr__(
                self, "ai_provider", normalize_provider(self.ai_provider)
            )
        # Strip whitespace from secret fields; blank becomes None
        for field_name in (
            "github_token",
            "tests_repo_token",
            "jira_api_token",
            "jira_pat",
            "jira_oauth_client_secret",
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
        if not normalize_jira_auth_mode(self.jira_auth_mode):
            # No method selected: Jira is disabled — no credential inference.
            if self.enable_jira is True:
                logger.warning(
                    "enable_jira is True but no Jira auth method is selected "
                    "(choose one in Server Settings)"
                )
            return False
        missing = jira_method_missing_fields(self)
        if missing:
            if self.enable_jira is True:
                logger.warning(
                    "enable_jira is True but Jira auth method '%s' is missing: %s",
                    self.jira_auth_mode,
                    ", ".join(missing),
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

        Requires ENABLE_GITHUB_ISSUES to not be explicitly False.
        Unlike github_issues_enabled, does not require TESTS_REPO_URL
        or a server-level GITHUB_TOKEN since feedback uses user-scoped
        tokens and issues go to the hardcoded project repo.
        """
        return self.enable_github_issues is not False

    @property
    def web_push_enabled(self) -> bool:
        """Check if Web Push is enabled (configured keys or auto-generated keys)."""
        if hasattr(self, "_vapid_config_cache"):
            return bool(self._vapid_config_cache)

        if bool(self.vapid_public_key.strip()) != bool(self.vapid_private_key.strip()):
            logger.info(
                "Partial VAPID configuration: the public key is derived from the private key."
            )

        result = bool(get_vapid_config())
        object.__setattr__(self, "_vapid_config_cache", result)
        return result

    @property
    def metadata_rules(self) -> list[dict[str, Any]]:
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
            rules = load_metadata_rules(path)
        except Exception:  # never crash the app on bad rule config
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

    @property
    def rp(self) -> ReportPortalConfig:
        """Typed access to all Report Portal settings."""
        return ReportPortalConfig(
            url=self.reportportal_url,
            api_token=self.reportportal_api_token,
            project=self.reportportal_project,
            verify_ssl=self.reportportal_verify_ssl,
            enabled=self.reportportal_enabled,
            push_classifications=self.rp_push_classifications,
            push_rootcoz_url=self.rp_push_rootcoz_url,
            push_tracker_links=self.rp_push_tracker_links,
        )


def normalize_jira_auth_mode(value: str | None) -> str:
    """Canonical ``jira_auth_mode`` value; ``""`` when unset or invalid.

    Lenient on purpose: ``Settings.model_copy`` (per-request overrides)
    bypasses field validators, so resolve paths normalize instead of
    crashing. Invalid values log a warning and disable Jira (empty mode).
    """
    mode = (value or "").strip().lower()
    if mode and mode not in JIRA_AUTH_MODES:
        logger.warning(
            "Ignoring invalid jira_auth_mode %r (expected one of: %s)",
            value,
            ", ".join(JIRA_AUTH_MODES),
        )
        return ""
    return mode


def jira_method_missing_fields(settings: Settings) -> list[str]:
    """Missing settings field names for the explicitly selected Jira auth method.

    Returns [] when ``jira_auth_mode`` is unset (Jira is disabled then —
    ``jira_enabled`` short-circuits before field checks) or when the
    selected method's fields are all configured.
    """
    mode = normalize_jira_auth_mode(settings.jira_auth_mode)
    if not mode:
        return []
    required: dict[str, list[tuple[str, Any]]] = {
        "oauth": [
            ("jira_oauth_client_secret", settings.jira_oauth_client_secret),
            ("atlassian_oauth_client_id", settings.atlassian_oauth_client_id),
            ("atlassian_oauth_cloud_id", settings.atlassian_oauth_cloud_id),
        ],
        "basic": [
            ("jira_email", settings.jira_email),
            ("jira_api_token", settings.jira_api_token),
            ("jira_cloud_url", settings.jira_cloud_url),
        ],
        "pat": [
            ("jira_pat", settings.jira_pat),
            ("jira_server_url", settings.jira_server_url),
        ],
    }
    return [name for name, value in required[mode] if not value]


# Legacy auto-detection (ATST prefix sniffing, email-presence Cloud
# detection, pat/api-token fallback chain) was removed in #346:
# jira_auth_mode is required — unset disables Jira entirely. Each
# selected method's token is read directly by the resolver in jira.py.


# In-memory cache for DB settings overrides.
# Populated at startup by load_db_settings() and updated by admin settings endpoints.
_db_settings_cache: dict[str, str] = {}


async def load_db_settings() -> None:
    """Load server_settings DB overrides into in-memory cache.

    Called once at startup before get_settings() is first invoked.
    DB values are NOT written to os.environ — the cache is merged
    by get_settings() via model_copy().
    """
    try:
        # Late import to avoid circular dependency
        from rootcoz import storage
        from rootcoz.encryption import decrypt_value

        db_settings = await storage.get_server_settings()
        if not db_settings:
            return

        _db_settings_cache.clear()
        for key, entry in db_settings.items():
            value = entry["value"]
            # Decrypt if encrypted (sensitive values are stored encrypted)
            try:
                value = decrypt_value(value)
            except (RuntimeError, OSError, UnicodeDecodeError, ValueError) as exc:
                # Not encrypted or decryption failed — use as-is
                logger.debug(
                    "Failed to decrypt setting %s; using raw value: %s",
                    key,
                    exc,
                )
            _db_settings_cache[key] = value

        if _db_settings_cache:
            get_settings.cache_clear()
            logger.info(
                "[startup] Loaded %d server setting(s) from DB",
                len(_db_settings_cache),
            )
    except Exception:
        logger.warning("Failed to load server settings from DB", exc_info=True)


def db_setting_is_set(key: str) -> bool:
    """True when the server_settings DB table holds an explicit value for key."""
    return key in _db_settings_cache


def get_db_setting(key: str) -> str:
    """Return a Server Settings DB override (decrypted), or "" when unset.

    Only DB overrides are returned — environment variables are not merged in.
    Callers that want "env > DB" precedence read the env var first.
    """
    return _db_settings_cache.get(key, "")


def update_db_settings_cache(updates: dict[str, str]) -> None:
    """Update in-memory cache when admin changes settings via the API."""
    _db_settings_cache.update(updates)
    get_settings.cache_clear()


def remove_from_db_settings_cache(keys: list[str]) -> None:
    """Remove keys from cache when admin deletes settings via the API."""
    for key in keys:
        _db_settings_cache.pop(key, None)
    get_settings.cache_clear()


def clear_db_settings_cache() -> None:
    """Clear the entire DB settings cache. Used by tests for isolation."""
    _db_settings_cache.clear()
    get_settings.cache_clear()


@lru_cache
def get_settings() -> Settings:
    """Get merged settings: env vars (base) + DB overrides.

    DB cache values are strings (same format as env vars).  Using
    ``Settings(**merged_data)`` triggers full pydantic validation and
    type coercion (str→int, str→bool, str→SecretStr, etc.).
    """
    base = Settings()
    if not _db_settings_cache:
        return base
    merged_data = base.model_dump()
    merged_data.update(_db_settings_cache)
    try:
        return Settings(**merged_data)
    except Exception:
        logger.warning(
            "Failed to validate merged settings with DB overrides",
            exc_info=True,
        )
        return base
