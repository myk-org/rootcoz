"""Single-source Server Settings registry and write/read/export/import lifecycle.

Every setting exposed in Server Settings is declared once as a
:class:`SettingSpec` (category & tab order, human label, sensitivity,
server-only/derived/restart-required flags, choices, conditional visibility,
UI grouping/hints/traps, and post-save hooks). SSO provider settings are
derived directly from :data:`rootcoz.auth.sso.PROVIDER_CLASSES` so adding a
new SSO provider requires zero edits here or in ``main.py``.

``main.py`` consumes this module through thin HTTP route adapters and passes
process-local callbacks via :class:`SettingsRuntimeHooks`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, get_args

import httpx
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from pydantic_core import InitErrorDetails

from rootcoz import ai_client, get_child_logger, storage
from rootcoz import transcripts as sidecar_transcripts
from rootcoz.ai_client import (
    normalize_provider,
    parse_ambient_auth_provider_ids,
)
from rootcoz.auth import sso as auth_sso
from rootcoz.auth.sso.base import SSOProvider
from rootcoz.auth.sso.oidc import OIDCProvider
from rootcoz.config import (
    DEFAULT_USER_ROLES,
    GREENWAVE_AUTH_MODES,
    GREENWAVE_KNOWN_CLASSIFICATIONS,
    JIRA_AUTH_MODES,
    Settings,
    atlassian_cloud_id_error,
    browser_provider_without_base_url,
    csv_entries,
    db_setting_is_set,
    duplicate_additional_repo_names,
    get_db_setting,
    get_settings,
    invalid_bearer_token_error,
    issuer_problem,
    jira_site_url_error,
    normalize_domain_list,
    normalize_feedback_repo_url,
    normalize_greenwave_auth_mode,
    parse_additional_repos,
    parse_peer_configs,
    parse_repo_ref,
    remove_from_db_settings_cache,
    safe_parse_repo_ref,
    update_db_settings_cache,
    validate_greenwave_url,
)
from rootcoz.encryption import (
    SENSITIVE_KEYS,
    decrypt_value,
    decrypt_value_or_none,
    encrypt_value,
    strip_sensitive_from_response,
)
from rootcoz.exporters.datadog import DATADOG_SITES, normalize_datadog_site
from rootcoz.logging_context import enable_log_file
from rootcoz.prow_validation import normalize_prow_url
from rootcoz.tag_facets import (
    collect_option_tag_sets,
    diff_tag_facets,
    find_newly_introduced_unknown_tags,
    parse_tag_facets,
    serialize_tag_facets,
)
from rootcoz.url_utils import (
    mask_url_credentials_in_text,
    redact_urls_in_text,
    strip_url_userinfo,
)
from rootcoz.url_utils import (
    strip_url_credentials as _strip_url_credentials,
)

logger = get_child_logger(__name__)

# Valid log levels shared by PUT, import, and env-persist write paths (#347):
# Settings.log_level itself is unconstrained, so all write paths enforce this set.
VALID_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Schema version of the settings export document — bumped on breaking changes
# to the export shape so imports can reject mismatched documents.
SETTINGS_EXPORT_VERSION = 1

# Warning shown when an export includes decrypted sensitive values.
SETTINGS_EXPORT_SECRETS_WARNING = (
    "This export contains decrypted secrets in plaintext — store and "
    "transfer it securely and delete it once imported."
)

# Process/deployment bootstrap keys that have no Settings model field at all
# but must never become settable or persistable through the settings API.
_NON_MODEL_SERVER_ONLY_SETTINGS: frozenset[str] = frozenset({"db_path", "debug"})

#: Deployment-wide SSO policy settings that no single provider owns.
SSO_GENERIC_SETTINGS: tuple[str, ...] = (
    "sso_allowed_domains",
    "sso_state_ttl_seconds",
    "sso_device_code_ttl_seconds",
    "sso_device_poll_interval_seconds",
)

# Optional string fields coerced from None -> "" in /api/default-server-settings
# while preserving None for booleans and non-string fields.
_DEFAULT_SETTINGS_OPTIONAL_STRINGS: frozenset[str] = frozenset(
    {
        "tests_repo_url",
        "jira_cloud_url",
        "jira_server_url",
        "jira_project_key",
        "jenkins_url",
        "prow_url",
        "gcs_bucket",
        "reportportal_url",
        "reportportal_project",
    }
)

# Serializes validate+write+diff operations on ``tag_facets`` so concurrent
# admin saves cannot observe a stale vanished-tag baseline and reintroduce
# removed dead-tag options.
TAG_FACETS_WRITE_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Human label formatting
# ---------------------------------------------------------------------------

# Tokens the humaniser keeps uppercase — "Ai model" is not an English label.
_LABEL_ACRONYMS: frozenset[str] = frozenset(
    {
        "AI",
        "API",
        "URL",
        "URI",
        "ID",
        "SSO",
        "TLS",
        "SSL",
        "SMTP",
        "DNS",
        "JSON",
        "CSV",
        "XML",
        "YAML",
        "HTTP",
        "JIRA",
        "GITHUB",
        "GIT",
        "CI",
        "DB",
        "GCS",
        "AWS",
        "GCP",
        "LLM",
        "MCP",
        "PR",
        "RP",
        "DD",
        "RBAC",
        "DSN",
        "VAPID",
        "OIDC",
        "SAML",
        "TTL",
        "CPU",
        "GPU",
        "HTML",
        "PDF",
    }
)

# Brand casing for acronym words that are not written all-caps.
_LABEL_PROPER_CASE: dict[str, str] = {
    "GITHUB": "GitHub",
    "REPORTPORTAL": "ReportPortal",
    "ROOTCOZ": "RootCoz",
    "DATADOG": "Datadog",
}

# A trailing token that is a unit renders as a parenthetical suffix instead of
# a word: audit_retention_days -> "Audit retention (days)".
_LABEL_UNITS: dict[str, str] = {
    "DAYS": "days",
    "HOURS": "hours",
    "MINUTES": "minutes",
    "SECONDS": "seconds",
    "BYTES": "bytes",
    "PERCENT": "percent",
    "MB": "MB",
}


def _label_word(word: str) -> str:
    """Render one key token: an acronym keeps its casing, the rest is lowercased."""
    upper = word.upper()
    if upper in _LABEL_PROPER_CASE:
        return _LABEL_PROPER_CASE[upper]
    return upper if upper in _LABEL_ACRONYMS else word.lower()


def humanize_setting_key(key: str) -> str:
    """Mechanical label for a setting key: ``audit_max_rows`` -> ``Audit max rows``."""
    words = [word for word in key.split("_") if word]
    unit = ""
    if len(words) > 1 and words[-1].upper() in _LABEL_UNITS:
        unit = f" ({_LABEL_UNITS[words.pop().upper()]})"
    labels = [_label_word(word) for word in words]
    if labels[0] == words[0].lower():  # not an acronym/brand -> sentence case
        labels[0] = labels[0].capitalize()
    return " ".join(labels) + unit


# ---------------------------------------------------------------------------
# SSO provider field derivation (single source from auth.sso.PROVIDER_CLASSES)
# ---------------------------------------------------------------------------


def sso_provider_settings(
    providers: Sequence[type[SSOProvider]] | None = None,
) -> tuple[str, ...]:
    """Return every registered SSO provider's Settings fields in offer order."""
    classes = auth_sso.PROVIDER_CLASSES if providers is None else providers
    return tuple(
        name
        for provider in classes
        for name in getattr(provider, "settings_fields", ())
    )


def sso_sensitive_settings(
    providers: Sequence[type[SSOProvider]] | None = None,
) -> frozenset[str]:
    """Return declared secret fields across all registered SSO providers."""
    classes = auth_sso.PROVIDER_CLASSES if providers is None else providers
    return frozenset(
        name
        for provider in classes
        for name in getattr(provider, "sensitive_settings", ())
    )


def sso_restart_required_settings(
    providers: Sequence[type[SSOProvider]] | None = None,
) -> frozenset[str]:
    """Return declared restart-required fields across all registered SSO providers."""
    classes = auth_sso.PROVIDER_CLASSES if providers is None else providers
    return frozenset(
        name
        for provider in classes
        for name in getattr(provider, "restart_required_settings", ())
    )


def sso_url_field_rules(
    providers: Sequence[type[SSOProvider]] | None = None,
) -> dict[str, tuple[str, bool]]:
    """Map every SSO provider's URL setting to ``(noun, allow_path)``.

    OIDC providers with a configurable ``issuer_setting`` (e.g. Red Hat SSO
    Keycloak realms) allow a path in the issuer URL because the realm path is
    part of ``iss``. Non-OIDC providers (e.g. OpenShift) append ``/oauth/...``
    and ``/api/...`` to bare origins, so paths are refused on both
    ``issuer_setting`` and ``api_url_setting``.
    """
    classes = auth_sso.PROVIDER_CLASSES if providers is None else providers
    rules: dict[str, tuple[str, bool]] = {}
    for provider in classes:
        issuer_field = getattr(provider, "issuer_setting", "")
        if issuer_field:
            rules[issuer_field] = ("issuer", issubclass(provider, OIDCProvider))
        api_url_field = getattr(provider, "api_url_setting", "")
        if api_url_field:
            rules[api_url_field] = ("cluster API URL", False)
    return rules


def validate_and_strip_sso_fields(
    settings_obj: Settings,
    providers: Sequence[type[SSOProvider]] | None = None,
) -> None:
    """Strip whitespace and validate URL fields for every registered SSO provider.

    Called by :class:`rootcoz.config.Settings` so adding a provider in
    ``auth/sso/`` requires no edits to hardcoded field/secret/URL tuples in
    ``config.py``.
    """
    sensitive = sso_sensitive_settings(providers)
    url_rules = sso_url_field_rules(providers)

    for field_name in sso_provider_settings(providers):
        if not hasattr(settings_obj, field_name):
            continue
        value = getattr(settings_obj, field_name)
        if field_name in sensitive or isinstance(value, SecretStr):
            if isinstance(value, SecretStr):
                stripped = value.get_secret_value().strip()
                object.__setattr__(
                    settings_obj,
                    field_name,
                    SecretStr(stripped) if stripped else None,
                )
        elif isinstance(value, str):
            object.__setattr__(settings_obj, field_name, value.strip())

    error_details: list[InitErrorDetails] = []
    for field_name, (noun, allow_path) in url_rules.items():
        if not hasattr(settings_obj, field_name):
            continue
        raw = getattr(settings_obj, field_name)
        issuer = (raw or "").strip() if isinstance(raw, str) else ""
        if not issuer:
            object.__setattr__(settings_obj, field_name, "")
            continue
        if not issuer.startswith(("https://", "http://")):
            msg = f"{issuer!r} is not an http(s) URL"
            error_details.append(
                InitErrorDetails(
                    type="value_error",
                    loc=(field_name,),
                    input=raw,
                    ctx={"error": ValueError(msg)},
                )
            )
            continue
        problem = issuer_problem(issuer, noun=noun, allow_path=allow_path)
        if problem is not None:
            msg = f"{issuer!r}: {problem}"
            error_details.append(
                InitErrorDetails(
                    type="value_error",
                    loc=(field_name,),
                    input=raw,
                    ctx={"error": ValueError(msg)},
                )
            )
            continue
        object.__setattr__(settings_obj, field_name, issuer.rstrip("/"))

    if error_details:
        raise ValidationError.from_exception_data(
            type(settings_obj).__name__, error_details
        )


# ---------------------------------------------------------------------------
# Single declarative SettingSpec registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingSpec:
    """Single-source declaration for one Server Setting."""

    key: str
    category: str = ""
    category_order: int = 0
    label_override: str | None = None
    sensitive: bool = False
    server_only: bool = False
    derived: bool = False
    defaults_hidden: bool = False
    restart_required: bool = False
    empty_override_forbidden: bool = False
    db_only: bool = False
    options: tuple[str, ...] | None = None
    options_source: str | None = None
    multiple: bool = False
    allow_custom: bool = False
    visible_when: dict[str, Any] | None = None
    section: str | None = None
    section_label: str | None = None
    section_collapsible: bool = False
    group: str | None = None
    hint: str | None = None
    value_traps: dict[str, str] | None = None
    pre_validate_model: bool = False
    sidecar_bridge: bool = False
    log_file_hook: bool = False
    jira_verified_invalidating: bool = False
    sso_browser_login: bool = False

    @property
    def env_var(self) -> str:
        """Environment variable name, or ``""`` for DB-only settings."""
        return "" if self.db_only else self.key.upper()

    @property
    def persistable(self) -> bool:
        """Whether an env-sourced value can be persisted into the DB store.

        DB-only settings have no env value to promote, so they are never
        persistable either.
        """
        return not self.server_only and not self.derived and not self.db_only

    @property
    def label(self) -> str:
        """Human-readable display label (override or mechanical humanisation)."""
        return self.label_override or humanize_setting_key(self.key)

    def choices_dict(self) -> dict[str, Any]:
        """Enumerated-setting choice metadata, or empty dict when unenumerated."""
        result: dict[str, Any] = {}
        if self.options is not None:
            result["options"] = list(self.options)
        if self.options_source is not None:
            result["options_source"] = self.options_source
        if self.multiple:
            result["multiple"] = True
        if self.allow_custom:
            result["allow_custom"] = True
        return result


def _build_sso_provider_specs(
    providers: Sequence[type[SSOProvider]] | None = None,
) -> tuple[SettingSpec, ...]:
    """Generate :class:`SettingSpec` entries for every registered SSO provider."""
    classes = auth_sso.PROVIDER_CLASSES if providers is None else providers
    url_rules = sso_url_field_rules(classes)
    specs: list[SettingSpec] = []
    for provider in classes:
        display = getattr(provider, "display_name", "") or provider.name
        sensitive_set = set(getattr(provider, "sensitive_settings", ()))
        restart_set = set(getattr(provider, "restart_required_settings", ()))
        for field_name in getattr(provider, "settings_fields", ()):
            specs.append(
                SettingSpec(
                    key=field_name,
                    sensitive=field_name in sensitive_set,
                    restart_required=field_name in restart_set,
                    section=provider.name,
                    section_label=display,
                    section_collapsible=True,
                    pre_validate_model=field_name in url_rules,
                    sso_browser_login=True,
                )
            )
    return tuple(specs)


def _mode_visible(key: str, modes: list[str]) -> dict[str, Any]:
    """A ``visible_when`` rule: show the setting while *key* holds one of *modes*."""
    return {"key": key, "in": modes}


# Purpose-based category layout (#585): deployment-level first, then the two CI
# sources adjacent and before what consumes their artifacts, then analysis/output,
# then the operator-facing categories. Every Settings field lives here ONCE.
_RAW_CATEGORY_LAYOUT: tuple[tuple[str, tuple[SettingSpec, ...]], ...] = (
    (
        "Server",
        (
            # Exactly two: where the server is reachable, and where it pushes.
            SettingSpec(
                "public_base_url",
                defaults_hidden=True,
                sso_browser_login=True,
            ),
            SettingSpec(
                "auto_push_exporters",
                defaults_hidden=True,
                options_source="exporters",
                multiple=True,
            ),
        ),
    ),
    (
        "Jenkins",
        (
            SettingSpec("jenkins_url"),
            SettingSpec("jenkins_user", sensitive=True),
            SettingSpec("jenkins_password", sensitive=True),
            SettingSpec("jenkins_ssl_verify"),
            SettingSpec("jenkins_timeout"),
            SettingSpec("jenkins_artifacts_max_size_mb"),
            # Jenkins-only polling, beside the settings it polls with.
            SettingSpec("wait_for_completion"),
            SettingSpec("poll_interval_minutes"),
            SettingSpec(
                "max_wait_minutes",
                value_traps={
                    "0": (
                        "0 means wait forever — the build is polled until it finishes."
                    )
                },
            ),
        ),
    ),
    (
        "Prow",
        (
            SettingSpec(
                "prow_url",
                label_override="Prow Deck URL",
                pre_validate_model=True,
            ),
            SettingSpec(
                "gcs_bucket",
                label_override="GCS bucket",
                pre_validate_model=True,
            ),
            SettingSpec("prow_artifacts_max_size_mb"),
            SettingSpec("prow_artifact_max_file_size_mb"),
            SettingSpec("prow_artifacts_max_objects"),
        ),
    ),
    (
        "Analysis",
        (
            SettingSpec("ai_provider", label_override="AI provider"),
            SettingSpec("ai_model", label_override="AI model"),
            SettingSpec("ai_call_timeout", label_override="AI call timeout"),
            SettingSpec(
                "max_concurrent_ai_calls",
                label_override="Max concurrent AI calls",
            ),
            SettingSpec("peer_ai_configs"),
            SettingSpec("peer_analysis_max_rounds"),
            SettingSpec(
                "enable_free_models",
                label_override="Enable free AI models",
            ),
            SettingSpec(
                "ambient_auth_providers",
                defaults_hidden=True,
                options_source="providers",
                multiple=True,
            ),
            SettingSpec("active_server_providers"),
            SettingSpec("record_transcripts"),
            SettingSpec("enable_auto_review", defaults_hidden=True),
            SettingSpec(
                "max_concurrent_analyses",
                label_override="Max concurrent analyses",
            ),
            SettingSpec(
                "metadata_rules_file",
                defaults_hidden=True,
                restart_required=True,
            ),
            SettingSpec("tag_facets", label_override="Tag facets"),
            # The only setting BOTH sources read (jenkins_source, prow_source).
            SettingSpec("get_job_artifacts"),
        ),
    ),
    (
        "Jira",
        (
            SettingSpec(
                "jira_auth_mode",
                db_only=True,
                options=tuple(JIRA_AUTH_MODES),
                pre_validate_model=True,
                jira_verified_invalidating=True,
            ),
            SettingSpec("enable_jira"),
            SettingSpec("enable_jira_issues"),
            SettingSpec("jira_project_key"),
            SettingSpec("jira_site_url"),
            SettingSpec("jira_max_results"),
            # basic (Jira Cloud email + API token)
            SettingSpec(
                "jira_email",
                sensitive=True,
                visible_when=_mode_visible("jira_auth_mode", ["basic"]),
                jira_verified_invalidating=True,
            ),
            SettingSpec(
                "jira_api_token",
                sensitive=True,
                visible_when=_mode_visible("jira_auth_mode", ["basic"]),
                jira_verified_invalidating=True,
            ),
            SettingSpec(
                "jira_cloud_url",
                visible_when=_mode_visible("jira_auth_mode", ["basic"]),
                jira_verified_invalidating=True,
            ),
            SettingSpec(
                "jira_cloud_ssl_verify",
                visible_when=_mode_visible("jira_auth_mode", ["basic"]),
                jira_verified_invalidating=True,
            ),
            # pat (Jira Server/DC bearer PAT)
            SettingSpec(
                "jira_pat",
                label_override="JIRA personal access token",
                sensitive=True,
                visible_when=_mode_visible("jira_auth_mode", ["pat"]),
                jira_verified_invalidating=True,
            ),
            SettingSpec(
                "jira_server_url",
                visible_when=_mode_visible("jira_auth_mode", ["pat"]),
                jira_verified_invalidating=True,
            ),
            SettingSpec(
                "jira_server_ssl_verify",
                visible_when=_mode_visible("jira_auth_mode", ["pat"]),
                jira_verified_invalidating=True,
            ),
            # oauth (Atlassian OAuth 2.0 service-account credential)
            SettingSpec(
                "jira_oauth_client_secret",
                sensitive=True,
                visible_when=_mode_visible("jira_auth_mode", ["oauth"]),
                jira_verified_invalidating=True,
            ),
            SettingSpec(
                "atlassian_oauth_client_id",
                visible_when=_mode_visible("jira_auth_mode", ["oauth"]),
                jira_verified_invalidating=True,
            ),
            # oauth + atst (shared api.atlassian.com gateway)
            SettingSpec(
                "atlassian_oauth_cloud_id",
                label_override="Atlassian OAuth cloud ID",
                visible_when=_mode_visible("jira_auth_mode", ["oauth", "atst"]),
                jira_verified_invalidating=True,
            ),
            SettingSpec(
                "jira_oauth_ssl_verify",
                visible_when=_mode_visible("jira_auth_mode", ["oauth", "atst"]),
                jira_verified_invalidating=True,
            ),
            # atst (already-minted service-account scoped token)
            SettingSpec(
                "jira_atst_token",
                label_override="Jira service account scoped token",
                sensitive=True,
                visible_when=_mode_visible("jira_auth_mode", ["atst"]),
                jira_verified_invalidating=True,
            ),
        ),
    ),
    (
        "GitHub issues",
        (
            SettingSpec("github_token", sensitive=True),
            SettingSpec(
                "feedback_repo_url",
                label_override="Feedback repository URL",
            ),
            SettingSpec("enable_github_issues", defaults_hidden=True),
        ),
    ),
    (
        "Report Portal",
        (
            SettingSpec("reportportal_url"),
            SettingSpec("reportportal_api_token", sensitive=True),
            SettingSpec("reportportal_project"),
            SettingSpec("reportportal_verify_ssl"),
            SettingSpec("enable_reportportal", defaults_hidden=True),
            SettingSpec("rp_push_classifications", defaults_hidden=True),
            SettingSpec("rp_push_rootcoz_url", defaults_hidden=True),
            SettingSpec("rp_push_tracker_links", defaults_hidden=True),
            SettingSpec("rp_allow_unreviewed_push", defaults_hidden=True),
        ),
    ),
    (
        "Datadog",
        (
            SettingSpec("datadog_api_key", sensitive=True),
            SettingSpec("datadog_app_key", sensitive=True),
            SettingSpec(
                "datadog_site",
                options=tuple(DATADOG_SITES),
                allow_custom=True,
            ),
            SettingSpec("datadog_service"),
            SettingSpec("datadog_env"),
            SettingSpec("enable_datadog", defaults_hidden=True),
            SettingSpec("dd_push_classifications", defaults_hidden=True),
            SettingSpec("dd_push_rootcoz_url", defaults_hidden=True),
            SettingSpec("dd_push_tracker_links", defaults_hidden=True),
            SettingSpec("dd_allow_unreviewed_push", defaults_hidden=True),
        ),
    ),
    (
        "Greenwave",
        (
            SettingSpec(
                "greenwave_auth_mode",
                db_only=True,
                options=tuple(GREENWAVE_AUTH_MODES),
                pre_validate_model=True,
            ),
            SettingSpec("greenwave_url"),
            SettingSpec(
                "greenwave_api_token",
                sensitive=True,
                visible_when=_mode_visible("greenwave_auth_mode", ["bearer"]),
            ),
            # oidc (client-credentials grant against the token URL)
            SettingSpec(
                "greenwave_oidc_token_url",
                visible_when=_mode_visible("greenwave_auth_mode", ["oidc"]),
            ),
            SettingSpec(
                "greenwave_oidc_client_id",
                visible_when=_mode_visible("greenwave_auth_mode", ["oidc"]),
            ),
            SettingSpec(
                "greenwave_oidc_client_secret",
                sensitive=True,
                visible_when=_mode_visible("greenwave_auth_mode", ["oidc"]),
            ),
            # kerberos (SPNEGO/Negotiate with a client keytab)
            SettingSpec(
                "greenwave_keytab_path",
                visible_when=_mode_visible("greenwave_auth_mode", ["kerberos"]),
            ),
            SettingSpec(
                "greenwave_keytab_principal",
                visible_when=_mode_visible("greenwave_auth_mode", ["kerberos"]),
            ),
            SettingSpec(
                "greenwave_krb5_realm",
                visible_when=_mode_visible("greenwave_auth_mode", ["kerberos"]),
            ),
            SettingSpec(
                "greenwave_krb5_service",
                visible_when=_mode_visible("greenwave_auth_mode", ["kerberos"]),
            ),
            # mtls (client certificate authentication)
            SettingSpec(
                "greenwave_client_cert",
                visible_when=_mode_visible("greenwave_auth_mode", ["mtls"]),
            ),
            SettingSpec(
                "greenwave_client_key",
                visible_when=_mode_visible("greenwave_auth_mode", ["mtls"]),
            ),
            SettingSpec(
                "greenwave_ca_bundle",
                visible_when=_mode_visible("greenwave_auth_mode", ["mtls"]),
            ),
            # basic (HTTP Basic authentication)
            SettingSpec(
                "greenwave_basic_user",
                visible_when=_mode_visible("greenwave_auth_mode", ["basic"]),
            ),
            SettingSpec(
                "greenwave_basic_password",
                sensitive=True,
                visible_when=_mode_visible("greenwave_auth_mode", ["basic"]),
            ),
            # sso (the requesting user's browser Red Hat SSO session, with
            # optional RFC 8693 audience exchanges)
            SettingSpec(
                "greenwave_sso_audience",
                visible_when=_mode_visible("greenwave_auth_mode", ["sso"]),
            ),
            SettingSpec(
                "greenwave_sso_waiver_audience",
                visible_when=_mode_visible("greenwave_auth_mode", ["sso"]),
            ),
            # WaiverDB surface (mode-independent)
            SettingSpec("greenwave_waiver_url", pre_validate_model=True),
            SettingSpec(
                "greenwave_waiver_token",
                sensitive=True,
                visible_when=_mode_visible("greenwave_auth_mode", ["bearer"]),
            ),
            SettingSpec("greenwave_push_waivers"),
            SettingSpec(
                "greenwave_waivable_classifications",
                options=tuple(sorted(GREENWAVE_KNOWN_CLASSIFICATIONS)),
                multiple=True,
                allow_custom=True,
            ),
            SettingSpec("greenwave_outcome_map", pre_validate_model=True),
            SettingSpec("greenwave_subject_type"),
            SettingSpec("greenwave_product_version"),
            SettingSpec("greenwave_verify_ssl"),
            SettingSpec("enable_greenwave"),
        ),
    ),
    (
        "Auth & Security",
        (
            SettingSpec(
                "admin_key",
                label_override="Admin API key",
                sensitive=True,
                server_only=True,
            ),
            SettingSpec(
                "secure_cookies",
                label_override="Secure cookies",
                defaults_hidden=True,
                restart_required=True,
            ),
            SettingSpec(
                "trust_proxy_headers",
                label_override="Trust proxy identity headers",
                defaults_hidden=True,
                restart_required=True,
            ),
            SettingSpec(
                "require_approval",
                label_override="Require admin approval",
                defaults_hidden=True,
            ),
            SettingSpec(
                "admin_wait_approve_msg",
                label_override="Approval wait message",
                defaults_hidden=True,
            ),
            SettingSpec(
                "allowed_users",
                label_override="Allowed users (write allow-list)",
                defaults_hidden=True,
                options_source="users",
                multiple=True,
                allow_custom=True,
            ),
            SettingSpec(
                "default_user_role",
                label_override="Default role for new users",
                defaults_hidden=True,
                restart_required=True,
                options=tuple(DEFAULT_USER_ROLES),
                pre_validate_model=True,
            ),
            SettingSpec(
                "session_ttl_hours",
                label_override="Session lifetime (hours)",
                defaults_hidden=True,
            ),
            SettingSpec(
                "login_lockout_enabled",
                label_override="Enable login lockout",
            ),
            SettingSpec(
                "login_lockout_threshold",
                label_override="Failed logins before lockout",
                hint="Failed logins before the account locks. 0 disables lockout.",
            ),
            # SSO policy settings (non-collapsible "SSO" sub-section)
            SettingSpec(
                "sso_allowed_domains",
                defaults_hidden=True,
                section="sso",
                section_label="SSO",
                pre_validate_model=True,
            ),
            SettingSpec(
                "sso_state_ttl_seconds",
                section="sso",
                section_label="SSO",
            ),
            SettingSpec(
                "sso_device_code_ttl_seconds",
                section="sso",
                section_label="SSO",
            ),
            SettingSpec(
                "sso_device_poll_interval_seconds",
                section="sso",
                section_label="SSO",
            ),
            # Per-provider SSO settings derived dynamically from PROVIDER_CLASSES
            *_build_sso_provider_specs(),
        ),
    ),
    (
        "Notifications",
        (
            SettingSpec("smtp_host", defaults_hidden=True),
            SettingSpec("smtp_port", defaults_hidden=True),
            SettingSpec("smtp_user", defaults_hidden=True),
            SettingSpec("smtp_password", sensitive=True, defaults_hidden=True),
            SettingSpec("smtp_from", defaults_hidden=True),
            SettingSpec("alert_email_to", defaults_hidden=True),
            SettingSpec("slack_webhook_url", sensitive=True, defaults_hidden=True),
            # Built-in health monitor (#464): runtime applied.
            SettingSpec("enable_health_monitor", defaults_hidden=True),
            SettingSpec("health_monitor_interval_seconds", defaults_hidden=True),
            SettingSpec("health_alert_unhealthy_seconds", defaults_hidden=True),
            SettingSpec("health_alert_error_rate_percent", defaults_hidden=True),
            SettingSpec("health_alert_error_rate_seconds", defaults_hidden=True),
            SettingSpec("health_alert_stuck_analysis_seconds", defaults_hidden=True),
            # Browser push is a notification channel.
            SettingSpec(
                "vapid_public_key",
                defaults_hidden=True,
                restart_required=True,
            ),
            SettingSpec(
                "vapid_private_key",
                sensitive=True,
                restart_required=True,
            ),
            SettingSpec(
                "vapid_claim_email",
                defaults_hidden=True,
                restart_required=True,
            ),
        ),
    ),
    (
        "Data & retention",
        (
            SettingSpec("audit_retention_days"),
            SettingSpec("audit_max_rows"),
            SettingSpec(
                "audit_auth_failure_max_per_minute",
                label_override="Audit rows kept per minute",
            ),
            SettingSpec("results_retention_days"),
            SettingSpec("results_max_rows"),
            SettingSpec(
                "pi_sidecar_transcripts_max_age_days",
                defaults_hidden=True,
                restart_required=True,
                sidecar_bridge=True,
                group="transcripts",
            ),
            SettingSpec(
                "pi_sidecar_transcripts_max_size_mb",
                defaults_hidden=True,
                restart_required=True,
                sidecar_bridge=True,
                group="transcripts",
            ),
        ),
    ),
    (
        "Repos & workspace",
        (
            SettingSpec(
                "tests_repo_url",
                label_override="Tests repository URL",
            ),
            SettingSpec("tests_repo_token", sensitive=True),
            SettingSpec("additional_repos"),
            SettingSpec("max_concurrent_repo_clones"),
            SettingSpec("chat_clone_depth"),
            SettingSpec("repo_clone_timeout_seconds"),
            SettingSpec(
                "max_workspace_graph_mb",
                label_override="Workspace graph budget (MB)",
            ),
        ),
    ),
    (
        "Logging",
        (
            SettingSpec(
                "log_level",
                label_override="Log level",
                defaults_hidden=True,
                restart_required=True,
                options=VALID_LOG_LEVELS,
            ),
            SettingSpec(
                "log_max_bytes",
                label_override="Log file size (bytes)",
                log_file_hook=True,
            ),
            SettingSpec(
                "log_backup_count",
                label_override="Rotated log files kept",
                log_file_hook=True,
            ),
            SettingSpec(
                "log_min_free_bytes",
                label_override="Log free-space floor (bytes)",
                log_file_hook=True,
            ),
            SettingSpec(
                "pi_sidecar_log_level",
                defaults_hidden=True,
                restart_required=True,
                sidecar_bridge=True,
            ),
        ),
    ),
    (
        "Advanced",
        (
            SettingSpec(
                "sidecar_host",
                defaults_hidden=True,
                restart_required=True,
                empty_override_forbidden=True,
                sidecar_bridge=True,
            ),
            SettingSpec(
                "sidecar_port",
                defaults_hidden=True,
                restart_required=True,
                empty_override_forbidden=True,
                sidecar_bridge=True,
            ),
            SettingSpec(
                "sidecar_url",
                derived=True,
                defaults_hidden=True,
                restart_required=True,
                sidecar_bridge=True,
            ),
            SettingSpec(
                "node_binary",
                defaults_hidden=True,
                restart_required=True,
                sidecar_bridge=True,
            ),
            SettingSpec(
                "pi_sidecar_transcripts_dir",
                defaults_hidden=True,
                restart_required=True,
                sidecar_bridge=True,
                group="transcripts",
            ),
        ),
    ),
)


def _materialize_specs(
    layout: Sequence[tuple[str, Sequence[SettingSpec]]],
) -> tuple[SettingSpec, ...]:
    """Attach canonical ``category`` and ``category_order`` to every spec."""
    materialized: list[SettingSpec] = []
    for order, (category, specs) in enumerate(layout):
        for spec in specs:
            materialized.append(
                SettingSpec(
                    **{
                        **spec.__dict__,
                        "category": category,
                        "category_order": order,
                    }
                )
            )
    return tuple(materialized)


SETTINGS_SPECS: tuple[SettingSpec, ...] = _materialize_specs(_RAW_CATEGORY_LAYOUT)
REGISTRY: Mapping[str, SettingSpec] = MappingProxyType(
    {spec.key: spec for spec in SETTINGS_SPECS}
)

# ---------------------------------------------------------------------------
# Single-source derived views (all computed from SETTINGS_SPECS)
# ---------------------------------------------------------------------------

CATEGORY_ORDER: tuple[str, ...] = tuple(
    category for category, _ in _RAW_CATEGORY_LAYOUT
)
CATEGORY_ORDER_INDEX: Mapping[str, int] = MappingProxyType(
    {category: idx for idx, category in enumerate(CATEGORY_ORDER)}
)

SETTINGS_CATEGORIES: dict[str, list[str]] = {
    category: [spec.key for spec in specs] for category, specs in _RAW_CATEGORY_LAYOUT
}
FIELD_TO_CATEGORY: dict[str, str] = {spec.key: spec.category for spec in SETTINGS_SPECS}
FIELD_TO_SSO_PROVIDER: dict[str, tuple[str, str]] = {
    spec.key: (spec.section, spec.section_label or spec.section)
    for spec in SETTINGS_SPECS
    if spec.section_collapsible and spec.section
}

SENSITIVE_SETTINGS: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.sensitive
)
SERVER_ONLY_SETTINGS: frozenset[str] = _NON_MODEL_SERVER_ONLY_SETTINGS | frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.server_only
)
DERIVED_SETTINGS: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.derived
)
DEFAULTS_HIDDEN_SETTINGS: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.defaults_hidden
)
RESTART_REQUIRED_SETTINGS: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.restart_required
)
LOG_FILE_SETTINGS: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.log_file_hook
)
JIRA_VERIFIED_INVALIDATING_KEYS: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.jira_verified_invalidating
)
SIDECAR_BRIDGE_SETTINGS: tuple[str, ...] = tuple(
    key
    for key in (
        "sidecar_host",
        "sidecar_port",
        "sidecar_url",
        "pi_sidecar_log_level",
        "node_binary",
        "pi_sidecar_transcripts_dir",
        "pi_sidecar_transcripts_max_age_days",
        "pi_sidecar_transcripts_max_size_mb",
    )
    if key in REGISTRY and REGISTRY[key].sidecar_bridge
) + tuple(
    spec.key
    for spec in SETTINGS_SPECS
    if spec.sidecar_bridge
    and spec.key
    not in {
        "sidecar_host",
        "sidecar_port",
        "sidecar_url",
        "pi_sidecar_log_level",
        "node_binary",
        "pi_sidecar_transcripts_dir",
        "pi_sidecar_transcripts_max_age_days",
        "pi_sidecar_transcripts_max_size_mb",
    }
)
EMPTY_OVERRIDE_FORBIDDEN: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.empty_override_forbidden
)
SSO_BROWSER_LOGIN_SETTINGS: frozenset[str] = frozenset(
    spec.key for spec in SETTINGS_SPECS if spec.sso_browser_login
)
PRE_VALIDATE_MODEL_KEYS: tuple[str, ...] = tuple(
    spec.key for spec in SETTINGS_SPECS if spec.pre_validate_model
)
SETTING_CHOICES: dict[str, dict[str, Any]] = {
    spec.key: choices for spec in SETTINGS_SPECS if (choices := spec.choices_dict())
}
SETTING_VISIBLE_WHEN: dict[str, dict[str, Any]] = {
    spec.key: spec.visible_when
    for spec in SETTINGS_SPECS
    if spec.visible_when is not None
}
SETTING_LABEL_OVERRIDES: dict[str, str] = {
    spec.key: spec.label_override
    for spec in SETTINGS_SPECS
    if spec.label_override is not None
}


def setting_label(field_name: str) -> str:
    """Human label for a Settings field — an override, else the humanised key."""
    spec = REGISTRY.get(field_name)
    if spec is not None:
        return spec.label
    return humanize_setting_key(field_name)


def audit_setting_details(key: str) -> dict[str, Any]:
    """Audit details for a single-key settings event: the key name, never its value."""
    return {
        "key": key,
        "changed": True,
        **({"redacted": True} if key in SENSITIVE_SETTINGS else {}),
    }


# ---------------------------------------------------------------------------
# Runtime hooks & sidecar/logging side-effect helpers
# ---------------------------------------------------------------------------


def is_loopback_host(host: str) -> bool:
    """True for loopback bind addresses (127.0.0.0/8, ::1, localhost)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def sidecar_host_supports_loopback_probe(host: str) -> bool:
    """True when a sidecar bind host keeps the IPv4 127.0.0.1 health probe working."""
    return host.strip() in ("", "0.0.0.0", "127.0.0.1")


def derive_sidecar_url_from_port() -> None:
    """Point the sidecar client at SIDECAR_PORT when no URL is configured."""
    if os.environ.get("SIDECAR_URL"):
        return
    port = (os.environ.get("SIDECAR_PORT") or "").strip()
    if not port or port == "9100":
        return
    os.environ["SIDECAR_URL"] = f"http://127.0.0.1:{port}"
    logger.info("Derived SIDECAR_URL=http://127.0.0.1:%s from SIDECAR_PORT", port)


def write_sidecar_env_bridge_from_db() -> None:
    """Write the full sidecar env bridge from the current DB overrides."""
    values: dict[str, str | None] = {"RECORD_TRANSCRIPTS": None}
    if db_setting_is_set("record_transcripts"):
        values["RECORD_TRANSCRIPTS"] = (
            "true" if get_settings().record_transcripts else "false"
        )
    for key in SIDECAR_BRIDGE_SETTINGS:
        values[key.upper()] = get_db_setting(key) if db_setting_is_set(key) else None
    sidecar_transcripts.write_sidecar_env_bridge(values)


def apply_log_level_setting() -> None:
    """Apply a DB-configured log_level to the rootcoz logger tree (restart-required)."""
    if not db_setting_is_set("log_level"):
        return
    name = get_settings().log_level.strip().upper()
    if not name:
        return
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        logger.warning("Ignoring invalid log_level setting: %r", name)
        return
    logging.getLogger("rootcoz").setLevel(level)
    logger.info("Applied DB log_level=%s to the rootcoz logger tree", name)


async def live_provider_spellings() -> dict[str, str]:
    """Lowercased provider ID -> the spelling the deployment registers."""
    spellings = {
        provider.lower(): provider
        for provider in ai_client._cached_catalog_provider_ids()
    }
    for provider in await ai_client.supported_key_providers():
        spellings.setdefault(provider.lower(), provider)
    return spellings


async def deployment_authenticated_provider_ids(
    ids: list[str],
) -> frozenset[str] | None:
    """Normalized provider IDs the deployment itself can authenticate."""
    from pi_sidecar_client import get_sidecar_client

    credentials = ai_client.deployment_provider_credentials()
    if credentials is None:
        return None
    authenticated = {normalize_provider(name) for name in credentials}
    for provider in ids:
        if normalize_provider(provider) in authenticated:
            continue
        try:
            status = await get_sidecar_client().get_model_provider_status(provider)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue
            logger.warning(
                "Provider status unavailable for provider=%s (HTTP %d) — "
                "accepting AMBIENT_AUTH_PROVIDERS without validating it",
                provider,
                exc.response.status_code,
            )
            return None
        except httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError:
            logger.warning(
                "Provider status unavailable for provider=%s — accepting "
                "AMBIENT_AUTH_PROVIDERS without validating it",
                provider,
            )
            return None
        auth_status = status.get("authStatus") if isinstance(status, dict) else None
        if not isinstance(auth_status, dict) or "configured" not in auth_status:
            logger.warning(
                "Sidecar reported no auth status for provider=%s — accepting "
                "AMBIENT_AUTH_PROVIDERS without validating it",
                provider,
            )
            return None
        if auth_status["configured"] is True:
            authenticated.add(normalize_provider(provider))
    return frozenset(authenticated)


@dataclass(frozen=True)
class SettingsRuntimeHooks:
    """Process-local callbacks invoked by settings validation and post-write hooks."""

    apply_log_file: Callable[[], None]
    sync_transcript_recording: Callable[[bool | None], Awaitable[bool]]
    broadcast_change: Callable[[], None]
    exporter_names: Callable[[], Sequence[str]]
    live_provider_spellings: Callable[[], Awaitable[dict[str, str]]]
    deployment_authenticated_providers: Callable[
        [list[str]], Awaitable[frozenset[str] | None]
    ]


def _default_exporter_names() -> list[str]:
    return ["reportportal", "datadog"]


def _noop_broadcast() -> None:
    return None


def _default_apply_log_file() -> None:
    enable_log_file(reconfigure=True)


def default_runtime_hooks() -> SettingsRuntimeHooks:
    """Standalone runtime hooks used when callers do not pass custom hooks."""
    return SettingsRuntimeHooks(
        apply_log_file=_default_apply_log_file,
        sync_transcript_recording=sidecar_transcripts.sync_transcript_recording,
        broadcast_change=_noop_broadcast,
        exporter_names=_default_exporter_names,
        live_provider_spellings=live_provider_spellings,
        deployment_authenticated_providers=deployment_authenticated_provider_ids,
    )


async def apply_transcript_recording_setting(
    hooks: SettingsRuntimeHooks | None = None,
) -> bool:
    """Persist + live-apply the record_transcripts DB state and sidecar env bridge."""
    active_hooks = hooks or default_runtime_hooks()
    enabled = (
        get_settings().record_transcripts
        if db_setting_is_set("record_transcripts")
        else None
    )
    write_sidecar_env_bridge_from_db()
    return await active_hooks.sync_transcript_recording(enabled)


# ---------------------------------------------------------------------------
# Metadata & read lifecycle
# ---------------------------------------------------------------------------


def settings_dotenv_values() -> dict[str, str]:
    """The dotenv values Settings merges (DotEnvSettingsSource), by field."""
    from pydantic_settings import DotEnvSettingsSource

    try:
        return DotEnvSettingsSource(
            Settings,
            env_file=Settings.model_config.get("env_file"),
            env_file_encoding=Settings.model_config.get("env_file_encoding"),
        )()
    except OSError:
        logger.debug("Unable to read dotenv sources", exc_info=True)
        return {}


def setting_is_int(key: str) -> bool:
    """True when the Settings field behind ``key`` is integer-typed."""
    annotation = Settings.model_fields[key].annotation
    return annotation is int or (
        hasattr(annotation, "__args__") and int in getattr(annotation, "__args__", ())
    )


def settings_env_value(key: str, dotenv: dict[str, str] | None = None) -> str | None:
    """Resolve a setting's environment value the same way Settings does."""
    spec = REGISTRY.get(key)
    if spec is not None and spec.db_only:
        return None
    env_var = spec.env_var if spec is not None else key.upper()
    if not env_var:
        return None
    env_value: str | None
    if os.environ.get(env_var) is not None:
        env_value = os.environ[env_var]
    else:
        env_value = (dotenv if dotenv is not None else settings_dotenv_values()).get(
            key
        )
    if env_value == "" and setting_is_int(key):
        return None
    return env_value


def get_settings_metadata() -> list[dict[str, Any]]:
    """Build metadata for all Settings fields with current values and sources."""
    settings = get_settings()

    result: list[dict[str, Any]] = []
    for field_name, field_info in Settings.model_fields.items():
        value = getattr(settings, field_name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value() if value else ""

        if value is None:
            display_value = ""
        elif isinstance(value, bool):
            display_value = str(value).lower()
        else:
            display_value = str(value)

        annotation = field_info.annotation
        field_type = "string"
        if annotation is bool or (
            hasattr(annotation, "__args__")
            and bool in getattr(annotation, "__args__", ())
        ):
            field_type = "boolean"
        elif annotation is int or (
            hasattr(annotation, "__args__")
            and int in getattr(annotation, "__args__", ())
        ):
            field_type = "integer"

        description = field_info.description or ""

        default = field_info.default
        if default is None:
            default_str = ""
        elif isinstance(default, bool):
            default_str = str(default).lower()
        else:
            default_str = str(default) if default != "" else ""

        spec = REGISTRY.get(field_name)
        is_sensitive = spec.sensitive if spec is not None else False
        choices = spec.choices_dict() if spec is not None else {}
        is_set = value is not None and str(value).strip() != ""
        category = spec.category if spec is not None else "Other"
        category_order = (
            spec.category_order if spec is not None else len(CATEGORY_ORDER)
        )

        entry: dict[str, Any] = {
            "key": field_name,
            "label": spec.label
            if spec is not None
            else humanize_setting_key(field_name),
            "env_var": spec.env_var if spec is not None else field_name.upper(),
            "value": display_value,
            "is_set": is_set,
            "default": default_str,
            "description": description,
            "type": field_type,
            "category": category,
            "category_order": category_order,
            "sensitive": is_sensitive,
            "restart_required": spec.restart_required if spec is not None else False,
            "source": "default",
            "options": choices.get("options"),
            "options_source": choices.get("options_source"),
            "multiple": bool(choices.get("multiple", False)),
            "allow_custom": bool(choices.get("allow_custom", False)),
            "server_only": spec.server_only if spec is not None else False,
            "derived": spec.derived if spec is not None else False,
            "persistable": spec.persistable if spec is not None else True,
        }
        if spec is not None:
            if spec.section:
                entry["section"] = spec.section
                entry["section_label"] = spec.section_label or spec.section
                if spec.section_collapsible:
                    entry["section_collapsible"] = True
            if spec.visible_when is not None:
                entry["visible_when"] = spec.visible_when
            if spec.group is not None:
                entry["group"] = spec.group
            if spec.hint is not None:
                entry["hint"] = spec.hint
            if spec.value_traps is not None:
                entry["value_traps"] = spec.value_traps
        result.append(entry)

    return result


async def list_admin_settings(reveal_key: str = "") -> list[dict[str, Any]]:
    """Return full admin settings metadata with DB/env sources and secret masking."""
    metadata = get_settings_metadata()

    for item in metadata:
        if item["key"] == "active_server_providers":
            item["options"] = await ai_client.server_provider_options()

    db_overrides = await storage.get_server_settings()
    verified_method = db_overrides.get("jira_verified_method", {}).get("value", "")
    for item in metadata:
        if item["key"] == "jira_auth_mode":
            item["jira_verified_method"] = verified_method

    dotenv = settings_dotenv_values()

    for item in metadata:
        key = item["key"]
        if key in db_overrides:
            override = db_overrides[key]
            item["source"] = "db"
            db_value = override["value"]
            if item["sensitive"] and db_value:
                try:
                    db_value = decrypt_value(db_value)
                except (RuntimeError, TypeError, ValueError, UnicodeError) as exc:
                    logger.debug(
                        "Failed to decrypt settings value for %s: %s", key, exc
                    )
            item["value"] = normalize_setting_override(key, db_value)
            item["updated_by"] = override.get("updated_by", "")
            item["updated_at"] = override.get("updated_at", "")
            env_val = os.environ.get(item["env_var"]) if item["env_var"] else None
            if env_val is not None:
                item["has_env_var"] = True
                if not item["sensitive"]:
                    item["env_var_value"] = (
                        strip_url_userinfo(env_val) if "://" in env_val else env_val
                    )
        elif settings_env_value(key, dotenv) is not None:
            item["source"] = "env"
        else:
            item["source"] = "default"

    reveal_all = reveal_key == "__all__"
    for item in metadata:
        if (
            item["sensitive"]
            and item["value"]
            and not reveal_all
            and item["key"] != reveal_key
        ):
            item["value"] = "••••••••"

    return metadata


async def get_masked_settings_history(
    key: str = "", limit: int = 100
) -> list[dict[str, Any]]:
    """Fetch settings change history and mask sensitive values."""
    history = await storage.get_server_settings_history(key=key or None, limit=limit)
    for entry in history:
        if entry.get("key") in SENSITIVE_SETTINGS:
            if entry.get("value"):
                entry["value"] = "••••••••"
            if entry.get("previous_value"):
                entry["previous_value"] = "••••••••"
    return history


def get_default_server_settings_payload(
    settings: Settings,
    resolve_ai_fn: Callable[..., tuple[str, str]],
) -> dict[str, Any]:
    """Build the non-sensitive ``/api/default-server-settings`` response payload."""
    result: dict[str, Any] = {}
    for field_name in type(settings).model_fields:
        if field_name in SENSITIVE_SETTINGS or field_name in DEFAULTS_HIDDEN_SETTINGS:
            continue
        value = getattr(settings, field_name)
        if isinstance(value, SecretStr):
            continue
        result[field_name] = value

    ai_provider, ai_model = resolve_ai_fn(settings=settings)
    result["ai_provider"] = ai_provider
    result["ai_model"] = ai_model

    if settings.peer_ai_configs:
        try:
            result["peer_ai_configs"] = parse_peer_configs(settings.peer_ai_configs)
        except ValueError:
            logger.warning(
                "Failed to parse PEER_AI_CONFIGS for default-server-settings"
            )
            result["peer_ai_configs"] = []
    else:
        result["peer_ai_configs"] = []

    if settings.additional_repos:
        try:
            parsed = parse_additional_repos(settings.additional_repos)
            result["additional_repos"] = [
                {
                    **{k: v for k, v in repo.items() if k != "token"},
                    "url": strip_url_userinfo(repo["url"]),
                }
                for repo in parsed
            ]
        except ValueError:
            logger.warning(
                "Failed to parse ADDITIONAL_REPOS for default-server-settings"
            )
            result["additional_repos"] = []
    else:
        result["additional_repos"] = []

    tests_url, tests_ref = safe_parse_repo_ref(settings.tests_repo_url or "")
    result["tests_repo_url"] = strip_url_userinfo(tests_url)
    result["tests_repo_ref"] = tests_ref
    feedback_url, _ = parse_repo_ref(settings.feedback_repo_url or "")
    result["feedback_repo_url"] = strip_url_userinfo(feedback_url)

    for key, value in result.items():
        if value is None and key in _DEFAULT_SETTINGS_OPTIONAL_STRINGS:
            result[key] = ""

    result["jira_enabled"] = settings.jira_enabled

    for key, value in result.items():
        if isinstance(value, str) and "://" in value:
            result[key] = strip_url_userinfo(value)

    for key in SENSITIVE_KEYS:
        result.pop(key, None)
    return strip_sensitive_from_response(result)


# ---------------------------------------------------------------------------
# Validation & normalization lifecycle
# ---------------------------------------------------------------------------


def log_level_error(value: object) -> str | None:
    """Validate a log level; return the error message, or None when valid."""
    if str(value).strip().upper() not in VALID_LOG_LEVELS:
        return (
            "log_level: must be one of "
            f"{', '.join(VALID_LOG_LEVELS)} (case-insensitive)"
        )
    return None


def _known_providers_hint(known: set[str]) -> str:
    return ", ".join(sorted(known))


async def normalize_ambient_auth_providers(
    value: str,
    hooks: SettingsRuntimeHooks | None = None,
) -> tuple[list[str], str | None]:
    """Canonical AMBIENT_AUTH_PROVIDERS IDs, plus a rejection when unbacked."""
    active_hooks = hooks or default_runtime_hooks()
    ids = parse_ambient_auth_provider_ids(value)
    if not ids:
        return ids, None
    spellings = await active_hooks.live_provider_spellings()
    if not spellings:
        logger.warning(
            "No provider catalog — accepting AMBIENT_AUTH_PROVIDERS=%r "
            "without validating the provider IDs",
            value,
        )
        return ids, None
    known = set(spellings) | {
        provider.lower()
        for provider in parse_ambient_auth_provider_ids(
            get_settings().ambient_auth_providers or ""
        )
    }
    if unknown := [provider for provider in ids if provider.lower() not in known]:
        return ids, (
            "Unknown provider id(s) in AMBIENT_AUTH_PROVIDERS: "
            f"{', '.join(unknown)} (known: {_known_providers_hint(known)})"
        )
    authenticated = await active_hooks.deployment_authenticated_providers(ids)
    if authenticated is None:
        logger.warning(
            "Deployment credentials unknown — accepting "
            "AMBIENT_AUTH_PROVIDERS=%r without checking that the deployment "
            "authenticates its providers",
            value,
        )
    elif unbacked := [
        provider
        for provider in ids
        if normalize_provider(provider) not in authenticated
    ]:
        return ids, (
            "AMBIENT_AUTH_PROVIDERS: no deployment credential for "
            f"{', '.join(unbacked)} — a provider declared ambient must be "
            "one the deployment authenticates itself (an ambient "
            '<PROVIDER>_API_KEY variable, a manifest "$VAR" apiKey, or a '
            "credential the sidecar reports as configured)"
        )
    return [spellings.get(provider.lower(), provider) for provider in ids], None


def normalize_auto_push_exporters(
    value: str,
    hooks: SettingsRuntimeHooks | None = None,
) -> tuple[list[str], str | None]:
    """Canonical exporter plugin names, plus a rejection when unknown."""
    active_hooks = hooks or default_runtime_hooks()
    names = [part.strip() for part in value.split(",") if part.strip()]
    spellings = {name.lower(): name for name in active_hooks.exporter_names()}
    if not spellings:
        logger.warning(
            "Exporter registry is empty — accepting auto_push_exporters=%r "
            "without validating the plugin names",
            value,
        )
        return names, None
    if unknown := [name for name in names if name.lower() not in spellings]:
        return names, (
            "auto_push_exporters: unknown exporter plugin(s): "
            f"{', '.join(unknown)} "
            f"(known: {', '.join(sorted(spellings.values()))})"
        )
    return [spellings[name.lower()] for name in names], None


def normalize_allowed_users(value: str) -> tuple[list[str], str | None]:
    """Comma-separated usernames, stripped and in order, plus a shape error."""
    names = csv_entries(value, lower=False)
    if any(any(ch.isspace() for ch in name) for name in names):
        return names, (
            "allowed_users: must be comma-separated usernames — "
            "one username per entry, no spaces inside a name"
        )
    return names, None


async def normalize_enumerated_value(
    key: str,
    value: str,
    hooks: SettingsRuntimeHooks | None = None,
) -> tuple[str, str | None]:
    """Canonical value for an enumerated setting, plus a rejection when invalid."""
    if key == "log_level":
        return str(value).strip().upper(), log_level_error(value)
    if key == "auto_push_exporters":
        names, error = normalize_auto_push_exporters(value, hooks=hooks)
        return ", ".join(names), error
    if key == "allowed_users":
        names, error = normalize_allowed_users(value)
        return ", ".join(names), error
    if key == "sso_allowed_domains":
        try:
            domains = sorted(normalize_domain_list(value))
        except ValueError as exc:
            return value, f"sso_allowed_domains: {exc}"
        return ", ".join(domains), None
    if key == "ambient_auth_providers":
        ids, error = await normalize_ambient_auth_providers(value, hooks=hooks)
        return ", ".join(ids), error
    if key == "datadog_site":
        site = normalize_datadog_site(str(value))
        if not site:
            return value, (
                f"datadog_site: {value!r} is not a Datadog site hostname — "
                f"pick one of {', '.join(DATADOG_SITES)}, or type the bare "
                "hostname of your regional site"
            )
        return site, None
    return value, None


def persisted_tag_facet_option_sets() -> list[frozenset[str]]:
    """Return option tag sets from the persisted DB ``tag_facets`` override only."""
    stored = get_db_setting("tag_facets")
    if not stored:
        return []
    return collect_option_tag_sets(parse_tag_facets(stored, strict=False))


async def validate_and_normalize_tag_facets(
    raw_facets: Any,
    *,
    allow_missing_tags: bool = False,
    existing_option_sets: Sequence[frozenset[str]] | None = None,
) -> str:
    """Validate ``tag_facets`` schema and (optionally) live tag existence."""
    if raw_facets is None or (isinstance(raw_facets, str) and not raw_facets.strip()):
        return "[]"
    try:
        parsed_facets = parse_tag_facets(raw_facets, strict=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"tag_facets: {exc}") from exc
    if not allow_missing_tags and parsed_facets:
        live_labels = await storage.get_live_job_labels()
        unknown_tags = find_newly_introduced_unknown_tags(
            parsed_facets, live_labels, existing_option_sets
        )
        if unknown_tags:
            raise HTTPException(
                status_code=422,
                detail=(
                    "tag_facets: unknown tag(s) not present in job "
                    f"metadata: {', '.join(unknown_tags)}"
                ),
            )
    return serialize_tag_facets(parsed_facets)


def validate_effective_setting(updates: dict[str, Any]) -> Settings:
    """Validate candidate setting values merged on top of currently effective values."""
    return Settings.model_validate(get_settings().model_dump(mode="python") | updates)


def empty_override_error(key: str) -> str | None:
    """User-facing error when an explicit empty override is invalid for ``key``."""
    if key not in Settings.model_fields:
        return None
    if key in EMPTY_OVERRIDE_FORBIDDEN:
        return (
            f"Setting '{key}' cannot be empty — the sidecar bridge would "
            "export a blank value. Use Reset to fall back to the "
            "env/default value."
        )
    annotation = Settings.model_fields[key].annotation
    types = get_args(annotation) or (annotation,)
    if not all(t is str or t is SecretStr or t is type(None) for t in types):
        return (
            f"Setting '{key}' cannot be empty — its type does not accept "
            "an empty override. Use Reset to fall back to the env/default "
            "value."
        )
    try:
        Settings.model_validate({key: ""})
    except ValidationError:
        return (
            f"Setting '{key}' cannot be empty — its validator rejects an "
            "empty value. Use Reset to fall back to the env/default value."
        )
    return None


def normalize_setting_override(key: str, value: str) -> str:
    """Normalize persisted setting overrides whose blank values coerce to a fixed default."""
    if key == "feedback_repo_url":
        return normalize_feedback_repo_url(value)
    return value


# The Greenwave settings whose write re-runs the URL-scheme cross-field
# checks below: the mode, the ResultsDB URL, the per-mode credential fields
# and the waiver surface (URL, waivers toggle, waiver token). A write
# re-checks the effective ResultsDB URL against the effective mode, the OIDC
# token endpoint on an explicit write, and the waiver endpoint when it is
# written or waivers are on. The set gates no mode completeness — an
# incomplete mode is stored, because the UI renders a mode's fields only once
# it is persisted — so the credential fields are listed only to re-check the
# schemes their mode puts in play; `greenwave_waiver_token` is listed with
# them so a waiver-token save re-runs the same checks (its bearer shape is
# checked separately in _GREENWAVE_TOKEN_SETTING_KEYS). Fields that gate no
# scheme (krb5 service class, CA bundle) are excluded.
_GREENWAVE_AUTH_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "greenwave_auth_mode",
        "greenwave_url",
        "greenwave_waiver_url",
        "greenwave_push_waivers",
        "greenwave_api_token",
        "greenwave_oidc_token_url",
        "greenwave_oidc_client_id",
        "greenwave_oidc_client_secret",
        "greenwave_keytab_path",
        "greenwave_keytab_principal",
        "greenwave_client_cert",
        "greenwave_client_key",
        "greenwave_basic_user",
        "greenwave_basic_password",
        "greenwave_waiver_token",
    }
)

# The bearer tokens a save WRITES, as opposed to the ones it inherits: the shape
# rule applies only to these (see the cross-field block below).
_GREENWAVE_TOKEN_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "greenwave_api_token",
        "greenwave_waiver_token",
    }
)


def _effective_proposed_settings(
    settings_updates: dict[str, Any],
) -> Settings | None:
    """Merge an admin update over the effective settings; None when invalid.

    Shared by the cross-field save rules (the Greenwave URL-scheme
    checks, auto-push requires auto-review) so an update touching only
    one side of a rule is still evaluated against the resulting
    configuration. None/empty values reset to the model default, matching
    the PUT semantics — an explicit empty override beats the env value.
    """
    effective = get_settings().model_dump()
    valid_keys = set(Settings.model_fields.keys())
    for key, value in settings_updates.items():
        if key not in valid_keys:
            continue
        if value is None or value == "":
            field = Settings.model_fields[key]
            effective[key] = (
                field.default_factory() if field.default_factory else field.default
            )
        else:
            effective[key] = value
    try:
        return Settings.model_validate(effective)
    except ValidationError:
        # Per-field problems were already reported by the caller; skip the
        # cross-field checks when the merged proposal cannot validate.
        return None


async def validate_settings_updates(
    settings_updates: dict[str, Any],
    *,
    allow_missing_facet_tags: bool = False,
    hooks: SettingsRuntimeHooks | None = None,
) -> list[str]:
    """Validate admin settings updates; return per-entry error messages.

    Shared by PUT and import write paths so both enforce identical rules.
    Mutates *settings_updates* in place to canonicalize accepted values.
    """
    errors: list[str] = []

    valid_keys = set(Settings.model_fields.keys())
    invalid_keys = set(settings_updates.keys()) - valid_keys
    if invalid_keys:
        errors.append(f"Unknown settings: {', '.join(sorted(invalid_keys))}")

    server_only_keys = set(settings_updates.keys()) & SERVER_ONLY_SETTINGS
    if server_only_keys:
        errors.append(
            f"Server-only settings (env var only): "
            f"{', '.join(sorted(server_only_keys))}"
        )

    pre_validated_failed: set[str] = set()
    for key in PRE_VALIDATE_MODEL_KEYS:
        if key not in settings_updates:
            continue
        value = settings_updates[key]
        if value is None or value == "":
            continue
        try:
            if key == "prow_url":
                settings_updates[key] = normalize_prow_url(value)
            else:
                validated = validate_effective_setting({key: value})
                settings_updates[key] = getattr(validated, key)
        except ValidationError as exc:
            pre_validated_failed.add(key)
            for err in exc.errors():
                ctx_err = err.get("ctx", {}).get("error")
                errors.append(str(ctx_err) if ctx_err else err.get("msg", str(err)))
        except (TypeError, ValueError) as exc:
            pre_validated_failed.add(key)
            errors.append(str(exc))

    if settings_updates.get("additional_repos") and (
        dupes := duplicate_additional_repo_names(
            str(settings_updates["additional_repos"])
        )
    ):
        errors.append(f"Duplicate additional repo names: {', '.join(dupes)}")

    cloud_id = settings_updates.get("atlassian_oauth_cloud_id")
    if cloud_id and (cloud_id_error := atlassian_cloud_id_error(str(cloud_id))):
        raise HTTPException(status_code=422, detail=cloud_id_error)

    site_url = settings_updates.get("jira_site_url")
    if site_url and (site_error := jira_site_url_error(str(site_url))):
        raise HTTPException(status_code=422, detail=site_error)

    if "tag_facets" in settings_updates:
        existing_option_sets = (
            None if allow_missing_facet_tags else persisted_tag_facet_option_sets()
        )
        settings_updates["tag_facets"] = await validate_and_normalize_tag_facets(
            settings_updates["tag_facets"],
            allow_missing_tags=allow_missing_facet_tags,
            existing_option_sets=existing_option_sets,
        )

    accepted: dict[str, Any] = {}

    for key, value in settings_updates.items():
        if key not in valid_keys or key in pre_validated_failed:
            continue
        field_info = Settings.model_fields[key]
        if value is None or value == "":
            continue
        if key in SETTING_CHOICES:
            canonical, enum_error = await normalize_enumerated_value(
                key, str(value), hooks=hooks
            )
            if enum_error:
                errors.append(enum_error)
            else:
                settings_updates[key] = canonical
        if key in (
            "prow_url",
            "gcs_bucket",
            "default_user_role",
            # greenwave_url is NOT skipped: its STRUCTURAL problems
            # (userinfo, query, fragment, bad port) are reported by the
            # per-key model_validate below, while its mode-aware SCHEME rule
            # needs the merged proposal and runs in the Greenwave cross-field
            # block. greenwave_waiver_url skips here because the pre-validate
            # block above already model-validates it (one message per
            # problem, #361 Qodo r2).
            "greenwave_waiver_url",
            "greenwave_outcome_map",
        ):
            continue
        if key == "sidecar_port":
            try:
                port = int(str(value).strip())
            except ValueError, TypeError:
                port = 0
            if not 1 <= port <= 65535:
                errors.append("sidecar_port: must be an integer between 1 and 65535")
        if key == "sidecar_host":
            host = str(value).strip()
            if host and not sidecar_host_supports_loopback_probe(host):
                errors.append(
                    f"sidecar_host: '{host}' would break the container loopback "
                    "health probe (startup would fail on every restart) — use "
                    "127.0.0.1, 0.0.0.0, or leave it empty"
                )
            settings_updates[key] = host
        is_int = setting_is_int(key)
        int_error = False
        if is_int:
            try:
                if isinstance(value, bool):
                    raise TypeError("a boolean is not an integer")
                if isinstance(value, float) and value.is_integer():
                    value = settings_updates[key] = int(value)
                int_val = int(value)
                for meta in field_info.metadata:
                    if (
                        hasattr(meta, "gt")
                        and meta.gt is not None
                        and int_val <= meta.gt
                    ):
                        errors.append(f"{key}: must be > {meta.gt}")
                        int_error = True
                    if (
                        hasattr(meta, "ge")
                        and meta.ge is not None
                        and int_val < meta.ge
                    ):
                        errors.append(f"{key}: must be >= {meta.ge}")
                        int_error = True
                    if (
                        hasattr(meta, "le")
                        and meta.le is not None
                        and int_val > meta.le
                    ):
                        errors.append(f"{key}: must be <= {meta.le}")
                        int_error = True
            except ValueError, TypeError:
                errors.append(f"{key}: must be an integer")
                int_error = True
        if int_error:
            continue
        try:
            validated = validate_effective_setting({**accepted, key: value})
        except ValidationError as exc:
            field_errs = [e for e in exc.errors() if e.get("loc")]
            if field_errs:
                for err in field_errs:
                    ctx_err = err.get("ctx", {}).get("error")
                    errors.append(
                        f"{key}: {ctx_err if ctx_err else err.get('msg', str(err))}"
                    )
                continue
            if is_int:
                settings_updates[key] = str(int(value))
            accepted[key] = int(value) if is_int else value
            continue
        if is_int:
            validated_int = getattr(validated, key)
            if validated_int == 0 and int_val != 0:
                errors.append(f"{key}: must be an integer")
                continue
            settings_updates[key] = str(validated_int)
        accepted[key] = getattr(validated, key)

    if not errors and accepted:
        try:
            validate_effective_setting(accepted)
        except ValidationError as exc:
            for err in exc.errors():
                ctx_err = err.get("ctx", {}).get("error")
                errors.append(str(ctx_err) if ctx_err else err.get("msg", str(err)))

    if not errors and settings_updates.keys() & SSO_BROWSER_LOGIN_SETTINGS:
        try:
            effective = validate_effective_setting(settings_updates)
        except ValidationError:
            effective = None
        if effective is not None and (
            hidden_provider := browser_provider_without_base_url(effective)
        ):
            errors.append(
                f"{hidden_provider}: PUBLIC_BASE_URL must be set for browser"
                " sign-in — without it this provider is hidden from the login page"
            )

    # Greenwave auth mode (#183): the URL schemes must match the mode (plain
    # HTTP is valid only for the credential-less 'none' mode, or while no mode
    # is set — the exporter is inert then).  Like the auto-push rule
    # below, it runs only when the update touches one of the mode's keys,
    # and is evaluated on the EFFECTIVE proposed values via
    # _effective_proposed_settings so absent-vs-explicitly-cleared is the
    # helper's job: an explicit CLEAR of the mode is judged on the cleared
    # value (a batch clearing the mode while setting an inert http URL is
    # accepted), and a batch replacing url+mode together is judged on the
    # new values — never on ``or``-fallbacks to the pre-batch state.
    if not _GREENWAVE_AUTH_SETTING_KEYS.isdisjoint(settings_updates):
        proposed = _effective_proposed_settings(settings_updates)
        if proposed is not None:
            try:
                # Unset mode = exporter disabled, so its URL is inert. An
                # explicit write of a URL is ALWAYS held to its scheme rule
                # (so saving http:// can never succeed); an INHERITED value
                # is only judged where it is actually in play, because a
                # save that reveals a hidden field must stay possible (the
                # exporter and the auth build still fail closed meanwhile).
                validate_greenwave_url(
                    proposed.greenwave_url,
                    "GREENWAVE_URL",
                    allow_http=normalize_greenwave_auth_mode(
                        proposed.greenwave_auth_mode
                    )
                    in ("none", ""),
                )
                if "greenwave_waiver_url" in settings_updates or (
                    proposed.greenwave_push_waivers
                ):
                    # A waiver write gates releases, so the waiver endpoint
                    # stays HTTPS-only in every mode.
                    validate_greenwave_url(
                        proposed.greenwave_waiver_url, "GREENWAVE_WAIVER_URL"
                    )
                if "greenwave_oidc_token_url" in settings_updates:
                    # The OIDC token endpoint carries the client secret on
                    # every push. Only an EXPLICIT save is refused here: an
                    # inherited http:// value must not block switching the
                    # auth mode to oidc, or the admin could never reveal the
                    # field that fixes it — greenwave_enabled and
                    # build_greenwave_auth fail closed until it is fixed.
                    validate_greenwave_url(
                        proposed.greenwave_oidc_token_url,
                        "GREENWAVE_OIDC_TOKEN_URL",
                    )
            except ValueError as exc:
                errors.append(str(exc))
        # A save is NOT rejected for an incomplete mode. The mode has to be
        # storable first: the Server Settings UI renders a mode's fields only
        # once that mode is persisted, so rejecting the switch would leave the
        # admin unable to reach the inputs that complete it (Jira behaves the
        # same way). An incomplete mode simply leaves the exporter disabled —
        # `greenwave_enabled`, the exporters capability list and the Test
        # connection button all name the missing fields.

    # A bearer token THIS update writes must be a header value the transport
    # can carry — in EVERY mode and whatever ``greenwave_push_waivers`` says.
    # The value outlives the save, so an unusable token saved while its mode is
    # inert only surfaces later, as a refused push (or a 422 from the auth
    # build) once an admin activates that mode. Only an EXPLICIT write is judged: an
    # inherited token stays inert-until-replaced, so a mode switch remains
    # storable — the UI renders a mode's fields only once that mode is saved,
    # and blocking the switch would lock the admin out of the field that fixes
    # the token. An empty value is a clearing write, always allowed.
    for token_key in sorted(_GREENWAVE_TOKEN_SETTING_KEYS & set(settings_updates)):
        raw_val = settings_updates.get(token_key)
        if isinstance(raw_val, str):
            stripped = raw_val.strip()
            token_err = invalid_bearer_token_error(token_key, stripped)
            if token_err:
                errors.append(token_err)
            else:
                settings_updates[token_key] = stripped

    # Auto-push requires auto-review.  The Settings model only warns
    # (raising there would hard-fail env-var startups and, via
    # get_settings()'s fallback, wipe every DB override), so the
    # combination is rejected here where the admin can act on it.
    # The rule only runs when the update actually changes one of the two
    # keys: a server already carrying the inert combination (set via env
    # or saved before this rule existed) must still be able to save
    # unrelated settings — a Jenkins URL, a token — so unrelated batches
    # are not rejected for a combination the admin is not touching.
    # Evaluated on the EFFECTIVE proposed values — current get_settings()
    # semantics (env + DB overrides, not raw env) overlaid with this
    # update — so an update touching only one of the two keys is caught
    # too.  Covers both PUT and settings import, which share this
    # function.  None/empty values reset to the model default, matching
    # the PUT semantics above.
    if {"auto_push_exporters", "enable_auto_review"}.isdisjoint(settings_updates):
        return errors
    proposed = _effective_proposed_settings(settings_updates)
    if (
        proposed is not None
        and proposed.auto_push_exporters.strip()
        and proposed.enable_auto_review is False
    ):
        errors.append(
            "auto_push_exporters requires enable_auto_review — auto-push "
            "only runs for failures auto-review has marked reviewed; "
            "enable ENABLE_AUTO_REVIEW or clear AUTO_PUSH_EXPORTERS"
        )

    return errors


# ---------------------------------------------------------------------------
# Write, persist, reset, export & import lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateSettingsResult:
    """Outcome of a validated PUT /api/admin/settings batch."""

    changed_keys: list[str]
    redacted_keys: list[str]
    warnings: list[str]
    tag_facet_changes: list[dict[str, Any]]
    audit_details: dict[str, Any]


@dataclass(frozen=True)
class PersistSettingResult:
    """Outcome of persisting one env-sourced setting into the DB store."""

    key: str
    tag_facet_changes: list[dict[str, Any]]


@dataclass(frozen=True)
class ResetSettingResult:
    """Outcome of resetting one DB setting override."""

    content: dict[str, Any]
    tag_facet_changes: list[dict[str, Any]]
    audit_details: dict[str, Any]


@dataclass(frozen=True)
class ExportSettingsResult:
    """Outcome of exporting DB-stored settings."""

    content: dict[str, Any]
    key_count: int


@dataclass(frozen=True)
class ImportSettingsResult:
    """Outcome of validating/applying a settings export document."""

    content: dict[str, Any]
    dry_run: bool
    changed_keys: list[str]
    redacted_keys: list[str]
    unchanged_count: int
    tag_facet_changes: list[dict[str, Any]]
    audit_details: dict[str, Any]


async def save_settings_to_db(
    settings_updates: dict[str, Any],
    username: str,
    *,
    hooks: SettingsRuntimeHooks | None = None,
) -> list[str]:
    """Persist validated settings updates atomically and run post-write hooks."""
    active_hooks = hooks or default_runtime_hooks()

    for key, value in settings_updates.items():
        str_value = str(value) if value is not None else ""
        if not str_value:
            error = empty_override_error(key)
            if error:
                raise HTTPException(status_code=400, detail=error)

    normalized_updates = {
        key: normalize_setting_override(key, str(value) if value is not None else "")
        for key, value in settings_updates.items()
    }
    db_values: dict[str, str] = {}
    for key, str_value in normalized_updates.items():
        if str_value and key in SENSITIVE_SETTINGS:
            db_values[key] = encrypt_value(str_value)
        else:
            db_values[key] = str_value

    await storage.set_server_settings_batch(db_values, updated_by=username)

    if settings_updates.keys() & JIRA_VERIFIED_INVALIDATING_KEYS:
        try:
            await storage.delete_server_setting(
                "jira_verified_method", deleted_by=username
            )
            remove_from_db_settings_cache(["jira_verified_method"])
        except Exception:
            logger.warning("Failed to clear jira_verified_method", exc_info=True)

    update_db_settings_cache(normalized_updates)

    if settings_updates.keys() & LOG_FILE_SETTINGS:
        active_hooks.apply_log_file()

    warnings: list[str] = []
    if "record_transcripts" in settings_updates or any(
        key in settings_updates for key in SIDECAR_BRIDGE_SETTINGS
    ):
        synced = await apply_transcript_recording_setting(hooks=active_hooks)
        if not synced and "record_transcripts" in settings_updates:
            warnings.append(
                "record_transcripts was saved, but the running sidecar could "
                "not be updated — it applies after a restart or a later "
                "successful sync"
            )

    if "sidecar_host" in settings_updates:
        host = str(settings_updates.get("sidecar_host") or "").strip()
        if host and not is_loopback_host(host):
            warnings.append(
                f"sidecar_host '{host}' is not a loopback address — the sidecar "
                "will listen on an external interface. Only do this on a "
                "trusted network (same trust class as trust_proxy_headers)."
            )

    active_hooks.broadcast_change()
    return warnings


TagFacetAuditCallback = Callable[[Sequence[dict[str, Any]]], Awaitable[None]]


async def _run_with_tag_facets_lock[T](
    should_lock: bool,
    fn: Callable[[], Awaitable[T]],
    *,
    on_tag_facet_changes: TagFacetAuditCallback | None = None,
) -> tuple[T, list[dict[str, Any]]]:
    """Serialize tag_facets validate+write+audit under ``TAG_FACETS_WRITE_LOCK``.

    Holding the lock through ``on_tag_facet_changes`` guarantees that concurrent
    tag-facet writes emit their per-facet audit rows in exact DB write order.
    """
    if not should_lock:
        return await fn(), []
    async with TAG_FACETS_WRITE_LOCK:
        before = get_settings().parsed_tag_facets
        result = await fn()
        facet_changes = diff_tag_facets(before, get_settings().parsed_tag_facets)
        if on_tag_facet_changes is not None and facet_changes:
            await on_tag_facet_changes(facet_changes)
        return result, facet_changes


async def update_settings(
    settings_updates: dict[str, Any],
    username: str,
    *,
    hooks: SettingsRuntimeHooks | None = None,
    on_tag_facet_changes: TagFacetAuditCallback | None = None,
) -> UpdateSettingsResult:
    """Validate, persist, and apply a settings update batch."""
    if not settings_updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    async def _run() -> tuple[list[str], list[str], dict[str, Any]]:
        errors = await validate_settings_updates(settings_updates, hooks=hooks)
        if errors:
            raise HTTPException(
                status_code=400, detail=f"Invalid settings: {'; '.join(errors)}"
            )
        # Read the old Greenwave mode before the save — the audit row names
        # old/new mode only, never credential values. After validation, so the
        # new mode is the canonical normalized one.
        audit_details = _greenwave_auth_mode_audit_detail(settings_updates)
        warnings = await save_settings_to_db(settings_updates, username, hooks=hooks)
        return list(settings_updates.keys()), warnings, audit_details

    (
        (changed_keys, warnings, audit_details),
        facet_changes,
    ) = await _run_with_tag_facets_lock(
        "tag_facets" in settings_updates,
        _run,
        on_tag_facet_changes=on_tag_facet_changes,
    )

    return UpdateSettingsResult(
        changed_keys=changed_keys,
        redacted_keys=[key for key in changed_keys if key in SENSITIVE_SETTINGS],
        warnings=warnings,
        tag_facet_changes=facet_changes,
        audit_details=audit_details,
    )


async def persist_setting(
    key: str,
    username: str,
    *,
    hooks: SettingsRuntimeHooks | None = None,
    on_tag_facet_changes: TagFacetAuditCallback | None = None,
) -> PersistSettingResult:
    """Persist an env-sourced setting's current value into the DB store."""
    if key not in Settings.model_fields:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
    if key in SERVER_ONLY_SETTINGS:
        raise HTTPException(
            status_code=400, detail=f"Server-only setting (env var only): {key}"
        )
    if key in DERIVED_SETTINGS:
        raise HTTPException(
            status_code=400,
            detail=f"{key} is derived from sidecar_port — persist sidecar_port instead",
        )
    env_value = settings_env_value(key)
    if not env_value:
        raise HTTPException(
            status_code=400,
            detail=f"Setting '{key}' has no env value set",
        )
    # ONE validator for every write path: enumerated canonicalisation, per-field
    # Pydantic validation, integer bounds and the cross-field combination rules
    # that PUT and import apply, so an env value cannot be promoted into a
    # state those paths would refuse (a URL scheme the selected auth mode does
    # not allow, an unusable waiver endpoint, …). It normalizes in place, so the
    # value saved is the canonical one it approved.
    updates = {key: env_value}
    try:
        validation_errors = await validate_settings_updates(updates, hooks=hooks)
    except HTTPException as exc:
        if key == "tag_facets":
            # Schema/live-tag problems keep their own 422 contract on this path
            # (the tag_facets validation), not the promotion 400 below.
            raise
        # The shared validator RAISES (rather than returning) for one shape — an
        # account-ID-shaped cloud id, which PUT answers with a 422. A promotion
        # has no such legacy contract, so report it in the same 400 shape as
        # every other invalid env value, which also keeps one reason code
        # (`invalid_request`) for this class of refusal in the audit trail.
        raise HTTPException(
            status_code=400, detail=f"Invalid env value for '{key}': {exc.detail}"
        ) from exc
    if validation_errors:
        detail = "; ".join(validation_errors)
        if key not in detail and key.upper() not in detail:
            # A cross-field rule can refuse this save over a setting the admin
            # did not touch — the effective URL/mode pair, say. Attribute that
            # to the configuration instead of to the value being promoted, so
            # a one-key env→DB migration is not blamed for the wrong field.
            detail += (
                " (rejected by the current effective configuration, not by this value)"
            )
        raise HTTPException(
            status_code=400, detail=f"Invalid env value for '{key}': {detail}"
        )
    env_value = str(updates[key] or "")

    async def _run() -> None:
        value_to_save = env_value
        if key == "tag_facets":
            value_to_save = await validate_and_normalize_tag_facets(
                value_to_save,
                allow_missing_tags=False,
                existing_option_sets=persisted_tag_facet_option_sets(),
            )
        await save_settings_to_db({key: value_to_save}, username, hooks=hooks)

    _, facet_changes = await _run_with_tag_facets_lock(
        key == "tag_facets",
        _run,
        on_tag_facet_changes=on_tag_facet_changes,
    )

    return PersistSettingResult(key=key, tag_facet_changes=facet_changes)


async def reset_setting(
    key: str,
    username: str,
    *,
    hooks: SettingsRuntimeHooks | None = None,
    on_tag_facet_changes: TagFacetAuditCallback | None = None,
) -> ResetSettingResult:
    """Remove a DB setting override so the env/default value governs again."""
    active_hooks = hooks or default_runtime_hooks()
    if key not in Settings.model_fields:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    if key in SERVER_ONLY_SETTINGS:
        raise HTTPException(
            status_code=400, detail=f"Server-only setting (env var only): {key}"
        )

    async def _run() -> dict[str, Any]:
        deleted = await storage.delete_server_setting(key, deleted_by=username)
        if not deleted:
            raise HTTPException(
                status_code=404, detail=f"Setting '{key}' has no DB override"
            )
        remove_from_db_settings_cache([key])
        if key in LOG_FILE_SETTINGS:
            active_hooks.apply_log_file()
        content: dict[str, Any] = {"reset": key}
        if key == "record_transcripts" or key in SIDECAR_BRIDGE_SETTINGS:
            synced = await apply_transcript_recording_setting(hooks=active_hooks)
            if not synced and key == "record_transcripts":
                content["warnings"] = [
                    (
                        "record_transcripts was reset, but the running sidecar could "
                        "not be updated — it keeps its current recording state until "
                        "a restart or a later successful sync"
                    )
                ]
        active_hooks.broadcast_change()
        return content

    # A reset removes the override, which for greenwave_auth_mode IS the new
    # effective value (unset). Captured BEFORE the delete, so the audit row
    # names the transition instead of recording the already-reset state — the
    # same old/new detail PUT and import record for a mode change.
    audit_details = _greenwave_auth_mode_audit_detail({key: None})
    content, facet_changes = await _run_with_tag_facets_lock(
        key == "tag_facets",
        _run,
        on_tag_facet_changes=on_tag_facet_changes,
    )

    return ResetSettingResult(
        content=content,
        tag_facet_changes=facet_changes,
        audit_details=audit_details,
    )


def _greenwave_auth_mode_audit_detail(
    settings_updates: dict[str, Any],
) -> dict[str, Any]:
    """Audit detail for a greenwave_auth_mode change: old/new mode only.

    The mode value is a non-secret enum (like default_user_role); every
    other changed setting stays key-names-only in the audit trail. Must be
    read BEFORE the update is saved. Returns {} when the mode is unchanged
    by this update.
    """
    if "greenwave_auth_mode" not in settings_updates:
        return {}
    return {
        "greenwave_auth_mode_change": {
            "old": get_settings().greenwave_auth_mode or "",
            "new": str(settings_updates.get("greenwave_auth_mode") or ""),
        }
    }


def _transform_additional_repos(
    raw: str, url_fn: Callable[[str], str], token_suffix: str
) -> str | None:
    """Rebuild an ``additional_repos`` value, transforming each repo URL."""
    try:
        repos = parse_additional_repos(raw)
    except ValueError:
        return None
    parts = []
    for repo in repos:
        part = f"{repo['name']}:{url_fn(repo['url'])}"
        if repo.get("ref"):
            part += f":{repo['ref']}"
        if repo.get("token") and token_suffix:
            part += f"@{token_suffix}"
        parts.append(part)
    return ",".join(parts)


def sanitize_export_value(key: str, value: str) -> str | None:
    """Strip credentials from a non-sensitive value on default exports."""
    if key == "additional_repos":
        return _transform_additional_repos(value, _strip_url_credentials, "")
    return redact_urls_in_text(value)


def mask_diff_value(key: str, value: str | None) -> str | None:
    """Mask credential substrings (URL userinfo, repo tokens) in diff display."""
    if value is None:
        return None
    if key == "additional_repos":
        masked = _transform_additional_repos(value, mask_url_credentials_in_text, "***")
        return "***" if masked is None else masked
    return mask_url_credentials_in_text(value)


def diff_import_updates(
    updates: dict[str, str], db_settings: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Diff import values against current DB overrides with sensitive masking."""
    applied: dict[str, dict[str, Any]] = {}
    unchanged: list[str] = []
    restart_required: list[str] = []
    for key, value in updates.items():
        old = db_settings[key]["value"] if key in db_settings else None
        if old and key in SENSITIVE_SETTINGS:
            old = decrypt_value_or_none(old)
        if old == value or (
            old is not None
            and key not in SENSITIVE_SETTINGS
            and value == sanitize_export_value(key, old)
        ):
            unchanged.append(key)
            continue
        masked = key in SENSITIVE_SETTINGS
        applied[key] = {
            "from": "***" if masked else mask_diff_value(key, old),
            "to": "***" if masked else mask_diff_value(key, value),
        }
        if key in RESTART_REQUIRED_SETTINGS:
            restart_required.append(key)
    return applied, unchanged, restart_required


async def export_settings(*, include_sensitive: bool = False) -> ExportSettingsResult:
    """Export DB-stored server settings as a versioned JSON document."""
    settings: dict[str, str] = {}
    sensitive_included = False
    for key, row in sorted((await storage.get_server_settings()).items()):
        if (
            key not in FIELD_TO_CATEGORY
            or key in SERVER_ONLY_SETTINGS
            or key in DERIVED_SETTINGS
        ):
            continue
        value = row["value"]
        if key in SENSITIVE_SETTINGS:
            if not include_sensitive:
                continue
            if value == "":
                settings[key] = ""
                continue
            sensitive_included = True
            decrypted = decrypt_value_or_none(value)
            if decrypted is None:
                logger.debug("Cannot decrypt setting %s for export; skipping", key)
                continue
            value = decrypted
        elif not include_sensitive:
            sanitized = sanitize_export_value(key, value)
            if sanitized is None:
                logger.warning(
                    "Skipping setting %s from default export: unparseable stored value",
                    key,
                )
                continue
            value = sanitized
        settings[key] = value

    content: dict[str, Any] = {
        "version": SETTINGS_EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "settings": settings,
    }
    if sensitive_included:
        content["warning"] = SETTINGS_EXPORT_SECRETS_WARNING
    return ExportSettingsResult(content=content, key_count=len(settings))


async def import_settings(
    body: Any,
    username: str,
    *,
    hooks: SettingsRuntimeHooks | None = None,
    on_tag_facet_changes: TagFacetAuditCallback | None = None,
) -> ImportSettingsResult:
    """Validate and optionally apply a settings export document atomically."""
    if not isinstance(body, dict) or body.get("version") != SETTINGS_EXPORT_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unsupported export document: expected version "
                f"{SETTINGS_EXPORT_VERSION}"
            ),
        )
    raw_settings = body.get("settings")
    if not isinstance(raw_settings, dict) or not raw_settings:
        raise HTTPException(status_code=422, detail="No settings in export document")
    updates = {
        str(k): normalize_setting_override(str(k), "" if v is None else str(v))
        for k, v in raw_settings.items()
    }

    errors = await validate_settings_updates(
        updates, allow_missing_facet_tags=True, hooks=hooks
    )
    for key, value in updates.items():
        if key in DERIVED_SETTINGS:
            errors.append(
                f"{key} is derived from sidecar_port — set sidecar_port instead"
            )
        if not value and (error := empty_override_error(key)):
            errors.append(error)
    if errors:
        raise HTTPException(
            status_code=422, detail=f"Invalid settings: {'; '.join(errors)}"
        )

    applied, unchanged, restart_required = diff_import_updates(
        updates, await storage.get_server_settings()
    )
    content: dict[str, Any] = {
        "applied": applied,
        "unchanged": unchanged,
        "restart_required": restart_required,
    }
    if body.get("dry_run"):
        content["dry_run"] = True
        return ImportSettingsResult(
            content=content,
            dry_run=True,
            changed_keys=[],
            redacted_keys=[],
            unchanged_count=len(unchanged),
            tag_facet_changes=[],
            audit_details={},
        )

    applied_updates = {key: value for key, value in updates.items() if key in applied}
    # Read the previous Greenwave auth mode BEFORE the save:
    # save_settings_to_db refreshes the settings cache synchronously, so
    # afterwards get_settings() already returns the NEW mode and the audit row
    # would report a mode change that never happened (new -> new).
    audit_details = _greenwave_auth_mode_audit_detail(applied_updates)
    warnings, facet_changes = await _run_with_tag_facets_lock(
        "tag_facets" in applied,
        lambda: save_settings_to_db(applied_updates, username, hooks=hooks),
        on_tag_facet_changes=on_tag_facet_changes,
    )

    if warnings:
        content["warnings"] = warnings
    changed_keys = list(applied)
    return ImportSettingsResult(
        content=content,
        dry_run=False,
        changed_keys=changed_keys,
        redacted_keys=[key for key in changed_keys if key in SENSITIVE_SETTINGS],
        unchanged_count=len(unchanged),
        tag_facet_changes=facet_changes,
        audit_details=audit_details,
    )
