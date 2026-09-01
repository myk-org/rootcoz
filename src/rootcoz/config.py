"""Configuration settings from environment variables."""

import os
import string
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, NamedTuple
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from simple_logger.logger import get_logger

from rootcoz.ai_client import VALID_AI_PROVIDERS, normalize_provider
from rootcoz.greenwave import (
    evaluate_greenwave_transport,
    normalize_greenwave_outcome_map,
    referenced_placeholders,
)
from rootcoz.metadata_rules import load_metadata_rules
from rootcoz.utils import parse_exporter_names, sanitize_control_chars
from rootcoz.vapid import get_vapid_config

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


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
        if provider not in VALID_AI_PROVIDERS:
            raise ValueError(
                f"Unsupported provider '{provider}' at position {i + 1}. Valid: {', '.join(sorted(VALID_AI_PROVIDERS))}"
            )
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


_GREENWAVE_VALID_OUTCOMES = frozenset({"PASSED", "FAILED", "INFO", "NEEDS_INSPECTION"})


def parse_greenwave_outcome_map(raw: str, *, strict: bool = False) -> dict[str, str]:
    """Parse ``CLASS:OUTCOME,...`` into a normalized Greenwave outcome map.

    Args:
        raw: Comma-separated classification-to-outcome mapping.
        strict: Raise :class:`ValueError` for malformed entries or unsupported
            outcomes. Non-strict parsing skips malformed entries for backwards
            compatibility; :class:`Settings` always uses strict parsing.

    Duplicate classification keys are rejected after case-insensitive
    normalization in both modes because accepting them would be ambiguous.
    """
    entries: list[tuple[str, str]] = []
    if not raw:
        return {}
    for segment in raw.split(","):
        entry = segment.strip()
        if not entry:
            continue
        classification, separator, raw_outcome = entry.partition(":")
        classification = classification.strip()
        outcome = raw_outcome.strip().upper()
        if not separator or not classification or not outcome:
            if strict:
                raise ValueError(
                    f"Invalid GREENWAVE_OUTCOME_MAP entry '{entry}'. "
                    "Both class and outcome must be non-empty "
                    "(format 'CLASS:OUTCOME')."
                )
            continue
        if outcome not in _GREENWAVE_VALID_OUTCOMES:
            if strict:
                valid = ", ".join(sorted(_GREENWAVE_VALID_OUTCOMES))
                raise ValueError(
                    f"Invalid GREENWAVE_OUTCOME_MAP outcome '{raw_outcome.strip()}' "
                    f"for classification '{classification}'. Valid outcomes: {valid}"
                )
            continue
        entries.append((classification, outcome))
    # validates: raises ValueError on duplicate casefolded classification keys
    normalize_greenwave_outcome_map(entries)
    return dict(entries)


def parse_greenwave_classifications(raw: str) -> frozenset[str]:
    """Parse a comma-separated classification list for case-insensitive matching."""
    return frozenset(
        classification.strip().casefold()
        for classification in raw.split(",")
        if classification.strip()
    )


def _validate_greenwave_template_placeholders(
    template: str,
    *,
    allowed: set[str],
    field_name: str,
) -> None:
    """Validate that all placeholders in *template* are in *allowed* and use no
    conversions or format specifications, and that the template literal itself
    contains no control characters.

    Raises ValueError with a message that includes *field_name* so callers get
    a field-specific error message without duplicating the parsing logic.
    """
    if template != sanitize_control_chars(template):
        raise ValueError(f"{field_name} must not contain control characters")
    for _, placeholder_name, format_spec, conversion in string.Formatter().parse(
        template
    ):
        if placeholder_name is None:
            continue
        if not placeholder_name or placeholder_name not in allowed:
            raise ValueError(
                f"Invalid placeholder '{placeholder_name}' in {field_name}. "
                f"Allowed: {', '.join(sorted(allowed))}"
            )
        if conversion or format_spec:
            raise ValueError(
                f"{field_name} placeholders must not use "
                "conversions or format specifications"
            )


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
    ai_provider: str = ""
    # Default AI model (server-level default, can be overridden per-request)
    ai_model: str = ""

    # Peer analysis configuration
    peer_ai_configs: str = ""  # "provider:model,provider:model" format
    peer_analysis_max_rounds: int = Field(default=3, ge=1, le=10)

    # Additional repositories for AI analysis context
    additional_repos: str = ""  # "name:url,name:url" format

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

    # Greenwave / ResultsDB / WaiverDB integration (optional, server-only)
    enable_greenwave: bool = Field(
        default=False,
        description=(
            "Explicit deployment safety gate for the Greenwave "
            "(ResultsDB/WaiverDB) exporter. Must be true; configuring URLs or "
            "credentials alone never enables gating writes."
        ),
    )
    greenwave_url: str | None = Field(
        default=None,
        description="ResultsDB API base URL (e.g. https://resultsdb.example.com/api/v2.0).",
    )
    greenwave_api_token: SecretStr | None = Field(
        default=None,
        description="ResultsDB API authentication token.",
    )
    greenwave_waiver_url: str | None = Field(
        default=None,
        description="WaiverDB API base URL (required when waiver submission is enabled).",
    )
    greenwave_waiver_token: SecretStr | None = Field(
        default=None,
        description=(
            "WaiverDB OIDC bearer or service-account token (required when "
            "waiver submission uses OIDC authentication)."
        ),
    )
    greenwave_push_waivers: bool = Field(
        default=False,
        description="Enable waiver submission to WaiverDB.",
    )
    greenwave_waivable_classifications: str = Field(
        default="INFRASTRUCTURE",
        description=(
            "Comma-separated rootcoz classifications that generate WaiverDB waivers. "
            "Whitespace-trimmed, matched case-insensitively (e.g. 'INFRASTRUCTURE,CODE ISSUE')."
        ),
    )
    greenwave_allow_ai_waivers: bool = Field(
        default=False,
        description=(
            "Allow auto-reviewed failures (rootcoz-ai) to generate WaiverDB waivers. "
            "Default: only human-reviewed failures generate waivers."
        ),
    )
    greenwave_outcome_map: str = Field(
        default="PRODUCT BUG:FAILED,CODE ISSUE:FAILED,INFRASTRUCTURE:INFO",
        description=(
            "Classification -> ResultsDB outcome mapping. Format: 'CLASS:OUTCOME,...'. "
            "Valid outcomes: PASSED, FAILED, INFO, NEEDS_INSPECTION. "
            "Classification keys must be unique when matched case-insensitively. "
            "Classifications not in the map are skipped (not exported)."
        ),
    )
    greenwave_subject_type: str = Field(
        default="koji_build",
        description="ResultsDB subject type (e.g. koji_build).",
    )
    greenwave_product_version: str | None = Field(
        default=None,
        description=(
            "Product version for WaiverDB waivers (required when "
            "GREENWAVE_PUSH_WAIVERS=true)."
        ),
    )
    greenwave_verify_ssl: bool = Field(
        default=True,
        description=(
            "Verify TLS certificates for ResultsDB and WaiverDB connections. "
            "False permits HTTP only for unauthenticated (none) auth in isolated local "
            "development when no CA bundle is set; all authenticated methods "
            "(token, oidc, kerberos, ssl) always require HTTPS. Never disable verification in production."
        ),
    )

    # Greenwave auth method selection (admin-configurable)
    greenwave_resultsdb_auth_method: str = Field(
        default="token",
        description="Auth for ResultsDB writes: 'none', 'token' (Bearer), 'kerberos' (SPNEGO via keytab), or 'ssl' (client cert).",
    )
    greenwave_waiver_auth_method: str = Field(
        default="oidc",
        description="Auth for WaiverDB: 'oidc' (Bearer), 'kerberos' (SPNEGO via keytab), or 'ssl' (client cert).",
    )
    greenwave_kerberos_keytab: str | None = Field(
        default=None,
        description="Path to Kerberos keytab for Greenwave auth (e.g. /etc/rootcoz/gw.keytab).",
    )
    greenwave_kerberos_principal: str | None = Field(
        default=None,
        description="Kerberos service principal for the keytab (e.g. svc-rootcoz@EXAMPLE.COM).",
    )
    greenwave_ssl_cert: str | None = Field(
        default=None,
        description="Path to client SSL cert (PEM) for Greenwave 'ssl' auth.",
    )
    greenwave_ssl_key: str | None = Field(
        default=None,
        description="Path to client SSL private key (PEM) for Greenwave 'ssl' auth.",
    )
    greenwave_ca_bundle: str | None = Field(
        default=None,
        description="Path to a CA bundle PEM for verifying ResultsDB/WaiverDB TLS. When set, overrides greenwave_verify_ssl.",
    )
    greenwave_testcase_template: str = Field(
        default="rootcoz.{job_name}.{test_name}",
        description="Template for testcase names. Placeholders: {job_name},{test_name},{tier},{subject_identifier}. Example: myproduct.product-build.{tier}.{test_name}",
    )
    greenwave_subject_template: str | None = Field(
        default=None,
        description=(
            "Template for constructing the ResultsDB/WaiverDB subject identifier "
            "when no explicit subject is provided at push time. "
            "Required to use AUTO_PUSH_EXPORTERS=greenwave. "
            "Placeholders: {job_name}, {build_number}, {tier}, {product_version}. "
            "Example: 'hco-bundle-registry-container-{product_version}.rhel9-{build_number}'"
        ),
    )
    greenwave_tier: str | None = Field(
        default=None,
        description="Value for the {tier} placeholder in the testcase template (e.g. tier-1).",
    )

    # Web Push (VAPID) configuration (optional, server-only)
    vapid_public_key: str = ""
    vapid_private_key: str = Field(default="", repr=False)
    vapid_claim_email: str = ""

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
            "failures are reviewed. Example: 'reportportal,greenwave'. Empty string "
            "disables auto-push. Only takes effect when enable_auto_review is also "
            "enabled. 'greenwave' requires GREENWAVE_SUBJECT_TEMPLATE to be set so "
            "the subject (build NVR) can be constructed from runtime context; rejected "
            "at config load when the template is absent or references an unconfigured "
            "server-side placeholder."
        ),
    )

    # Metadata rules file path (optional, server-only)
    metadata_rules_file: str = Field(
        default="",
        description="Path to a YAML/JSON file defining name-based metadata rules for auto-assignment",
    )

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
            "greenwave_url",
            "greenwave_waiver_url",
            "greenwave_product_version",
            "greenwave_kerberos_keytab",
            "greenwave_kerberos_principal",
            "greenwave_ssl_cert",
            "greenwave_ssl_key",
            "greenwave_ca_bundle",
            "greenwave_subject_template",
            "greenwave_tier",
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
            "greenwave_subject_type",
            "greenwave_resultsdb_auth_method",
            "greenwave_waiver_auth_method",
            "greenwave_testcase_template",
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
            "reportportal_api_token",
            "greenwave_api_token",
            "greenwave_waiver_token",
        ):
            secret = getattr(self, field_name)
            if secret is not None:
                stripped = secret.get_secret_value().strip()
                object.__setattr__(
                    self,
                    field_name,
                    SecretStr(stripped) if stripped else None,
                )
        for _amf in ("greenwave_resultsdb_auth_method", "greenwave_waiver_auth_method"):
            v = getattr(self, _amf)
            object.__setattr__(self, _amf, v.lower() if isinstance(v, str) else v)
        return self

    @model_validator(mode="after")
    def _validate_greenwave_config(self) -> "Settings":
        """Validate Greenwave mapping, URLs, authentication, and templates."""
        # Reject 'greenwave' in AUTO_PUSH_EXPORTERS when no subject template is set.
        # GreenwaveExporter.push() requires a subject_identifier (build NVR); without
        # GREENWAVE_SUBJECT_TEMPLATE auto-push cannot construct one.  When the template
        # IS set, auto-push is allowed and the NVR is rendered from runtime context.
        # Server-config placeholders referenced in the template are further validated
        # below to prevent malformed NVRs at render time.
        if (
            "greenwave" in parse_exporter_names(self.auto_push_exporters)
            and not self.greenwave_subject_template
        ):
            raise ValueError(
                "AUTO_PUSH_EXPORTERS cannot include 'greenwave' without "
                "GREENWAVE_SUBJECT_TEMPLATE: Greenwave is a gating exporter "
                "that requires a subject_identifier (build NVR). Set "
                "GREENWAVE_SUBJECT_TEMPLATE (e.g. "
                "'my-build-{product_version}-{build_number}') to enable "
                "auto-push, or remove 'greenwave' from AUTO_PUSH_EXPORTERS "
                "and push to Greenwave manually via the "
                "API/CLI/report-page with a subject_identifier."
            )

        outcome_map = parse_greenwave_outcome_map(
            self.greenwave_outcome_map, strict=True
        )

        transports = [
            (
                "greenwave_url",
                "ResultsDB",
                self.greenwave_resultsdb_auth_method,
            ),
            (
                "greenwave_waiver_url",
                "WaiverDB",
                self.greenwave_waiver_auth_method
                if self.greenwave_push_waivers
                else "none",
            ),
        ]
        for field_name, service, auth_method in transports:
            url = getattr(self, field_name)
            if not url:
                continue
            policy = evaluate_greenwave_transport(
                url,
                service=service,
                auth_method=auth_method,
                verify=self.greenwave_effective_verify,
            )
            if policy.error:
                raise ValueError(policy.error)
            object.__setattr__(self, field_name, policy.base_url)

        # Warn (do not fail) for waivable classifications missing from the outcome map
        if self.greenwave_waivable_classifications:
            outcome_map_keys_cf = {key.casefold() for key in outcome_map}
            for classification in self.greenwave_waivable_classifications_parsed:
                if classification not in outcome_map_keys_cf:
                    logger.warning(
                        "GREENWAVE_WAIVABLE_CLASSIFICATIONS entry '%s' is not a key "
                        "in GREENWAVE_OUTCOME_MAP; it will not be exported",
                        classification,
                    )

        if self.greenwave_resultsdb_auth_method not in {
            "none",
            "token",
            "kerberos",
            "ssl",
        }:
            raise ValueError(
                f"Invalid greenwave_resultsdb_auth_method: '{self.greenwave_resultsdb_auth_method}'. Valid values: 'none', 'token', 'kerberos', 'ssl'"
            )
        if self.greenwave_waiver_auth_method not in {"oidc", "kerberos", "ssl"}:
            raise ValueError(
                f"Invalid greenwave_waiver_auth_method: '{self.greenwave_waiver_auth_method}'. Valid values: 'oidc', 'kerberos', 'ssl'"
            )

        if self.greenwave_testcase_template:
            _validate_greenwave_template_placeholders(
                self.greenwave_testcase_template,
                allowed={"job_name", "test_name", "tier", "subject_identifier"},
                field_name="greenwave_testcase_template",
            )

        if self.greenwave_subject_template:
            _validate_greenwave_template_placeholders(
                self.greenwave_subject_template,
                allowed={"job_name", "build_number", "tier", "product_version"},
                field_name="greenwave_subject_template",
            )

        # When greenwave is in AUTO_PUSH_EXPORTERS, all server-config placeholders
        # referenced in greenwave_subject_template must resolve to non-empty
        # configured values.  A missing value would render a malformed NVR such
        # as 'hco-...-.rhel9-240'.  {job_name} and {build_number} come from
        # runtime context and cannot be validated at config-load time.
        if (
            "greenwave" in parse_exporter_names(self.auto_push_exporters)
            and self.greenwave_subject_template
        ):
            used_placeholders = referenced_placeholders(self.greenwave_subject_template)
            _server_cfg_placeholders: dict[str, tuple[str, str | None]] = {
                "tier": ("GREENWAVE_TIER", self.greenwave_tier),
                "product_version": (
                    "GREENWAVE_PRODUCT_VERSION",
                    self.greenwave_product_version,
                ),
            }
            for placeholder, (env_var, value) in _server_cfg_placeholders.items():
                if (
                    placeholder in used_placeholders
                    and not sanitize_control_chars(value or "").strip()
                ):
                    raise ValueError(
                        f"GREENWAVE_SUBJECT_TEMPLATE references '{{{placeholder}}}' "
                        f"but {env_var} is not configured; the rendered subject would "
                        "be malformed. Set the missing env var or remove the "
                        "placeholder from GREENWAVE_SUBJECT_TEMPLATE."
                    )

        active_auth_methods = {self.greenwave_resultsdb_auth_method}
        if self.greenwave_push_waivers:
            active_auth_methods.add(self.greenwave_waiver_auth_method)

        if "kerberos" in active_auth_methods and not self.greenwave_kerberos_keytab:
            raise ValueError(
                "GREENWAVE_KERBEROS_KEYTAB is required when an active Greenwave auth method is 'kerberos'"
            )

        if "ssl" in active_auth_methods and (
            not self.greenwave_ssl_cert or not self.greenwave_ssl_key
        ):
            raise ValueError(
                "GREENWAVE_SSL_CERT and GREENWAVE_SSL_KEY are required when an active Greenwave auth method is 'ssl'"
            )

        if (
            "{tier}" in (self.greenwave_testcase_template or "")
            and not self.greenwave_tier
        ):
            logger.warning(
                "greenwave_testcase_template contains '{tier}' but GREENWAVE_TIER is not set; it will render empty."
            )

        return self

    @property
    def greenwave_effective_verify(self) -> bool | str:
        """Return the effective httpx verification value for all write URLs."""
        return self.greenwave_ca_bundle or self.greenwave_verify_ssl

    @property
    def greenwave_outcome_map_parsed(self) -> dict[str, str]:
        """Parsed GREENWAVE_OUTCOME_MAP as {classification: OUTCOME}."""
        return parse_greenwave_outcome_map(self.greenwave_outcome_map)

    @property
    def greenwave_waivable_classifications_parsed(self) -> frozenset[str]:
        """Normalized classifications eligible for WaiverDB submission."""
        return parse_greenwave_classifications(self.greenwave_waivable_classifications)

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
        _, token_value = resolve_jira_auth(self)
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
    def greenwave_enabled(self) -> bool:
        """Check the safety gate, ResultsDB prerequisites, transport policy, and WaiverDB prerequisites when push_waivers is enabled."""
        if not self.enable_greenwave:
            return False
        if not self.greenwave_url:
            logger.warning(
                "enable_greenwave is True but GREENWAVE_URL is not configured"
            )
            return False
        policy = evaluate_greenwave_transport(
            self.greenwave_url,
            service="ResultsDB",
            auth_method=self.greenwave_resultsdb_auth_method,
            verify=self.greenwave_effective_verify,
        )
        if policy.error:
            logger.warning(
                "Greenwave ResultsDB transport is not ready: %s", policy.error
            )
            return False
        if self.greenwave_resultsdb_auth_method == "token" and (
            not self.greenwave_api_token
            or not self.greenwave_api_token.get_secret_value()
        ):
            logger.warning(
                "enable_greenwave is True but GREENWAVE_API_TOKEN is not configured "
                "(required for greenwave_resultsdb_auth_method='token')"
            )
            return False
        if self.greenwave_push_waivers:
            if not self.greenwave_waiver_url:
                logger.warning(
                    "enable_greenwave is True and GREENWAVE_PUSH_WAIVERS is True "
                    "but GREENWAVE_WAIVER_URL is not configured"
                )
                return False
            waiver_policy = evaluate_greenwave_transport(
                self.greenwave_waiver_url,
                service="WaiverDB",
                auth_method=self.greenwave_waiver_auth_method,
                verify=self.greenwave_effective_verify,
            )
            if waiver_policy.error:
                logger.warning(
                    "Greenwave WaiverDB transport is not ready: %s", waiver_policy.error
                )
                return False
            if not self.greenwave_product_version:
                logger.warning(
                    "enable_greenwave is True and GREENWAVE_PUSH_WAIVERS is True "
                    "but GREENWAVE_PRODUCT_VERSION is not configured"
                )
                return False
            waiver_method = self.greenwave_waiver_auth_method
            if waiver_method == "oidc" and (
                not self.greenwave_waiver_token
                or not self.greenwave_waiver_token.get_secret_value()
            ):
                logger.warning(
                    "enable_greenwave is True and GREENWAVE_PUSH_WAIVERS is True "
                    "but GREENWAVE_WAIVER_TOKEN is not configured "
                    "(required for greenwave_waiver_auth_method='oidc')"
                )
                return False
            if waiver_method == "kerberos" and not self.greenwave_kerberos_keytab:
                logger.warning(
                    "enable_greenwave is True and GREENWAVE_PUSH_WAIVERS is True "
                    "but GREENWAVE_KERBEROS_KEYTAB is not configured "
                    "(required for greenwave_waiver_auth_method='kerberos')"
                )
                return False
            if waiver_method == "ssl" and (
                not self.greenwave_ssl_cert or not self.greenwave_ssl_key
            ):
                logger.warning(
                    "enable_greenwave is True and GREENWAVE_PUSH_WAIVERS is True "
                    "but GREENWAVE_SSL_CERT and GREENWAVE_SSL_KEY are required "
                    "for greenwave_waiver_auth_method='ssl'"
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


def resolve_jira_auth(settings: Settings) -> tuple[bool, str]:
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
            assert settings.jira_api_token is not None  # guarded by has_api_token
            return True, settings.jira_api_token.get_secret_value()
        if has_pat:
            assert settings.jira_pat is not None  # guarded by has_pat
            return True, settings.jira_pat.get_secret_value()
        return True, ""

    # No email = Server/DC; prefer PAT, fall back to API token
    if has_pat and settings.jira_pat:
        return False, settings.jira_pat.get_secret_value()
    if has_api_token and settings.jira_api_token:
        return False, settings.jira_api_token.get_secret_value()

    return False, ""


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


def validate_db_settings_candidate(updates: Mapping[str, Any]) -> Settings:
    """Validate merged settings after applying updates and reset values.

    ``None`` and empty-string values remove a DB override, allowing the
    environment/default value to participate in cross-field validation. This
    helper is the single candidate-construction path for both PUT resets and
    DELETE resets.
    """
    reset_keys = {key for key, value in updates.items() if value in (None, "")}
    candidate_overrides = {
        key: value for key, value in _db_settings_cache.items() if key not in reset_keys
    }
    candidate_overrides.update(
        {key: value for key, value in updates.items() if value not in (None, "")}
    )
    # Init kwargs have higher priority than environment values in BaseSettings.
    # Passing only DB overrides lets reset fields fall through to the environment
    # before one complete validation pass; constructing Settings() first would
    # incorrectly validate a lower-priority, incomplete environment configuration.
    return Settings(**candidate_overrides)


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

    DB cache values are strings (same format as env vars). BaseSettings init
    values override environment values, so passing the overrides directly
    performs type coercion and one complete cross-field validation pass.
    """
    if not _db_settings_cache:
        return Settings()
    try:
        return Settings(**_db_settings_cache)
    except Exception:
        logger.warning(
            "Failed to validate merged settings with DB overrides",
            exc_info=True,
        )
        return Settings()
