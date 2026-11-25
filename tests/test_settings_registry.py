"""Dedicated tests for the single-source Server Settings registry (#663)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from rootcoz import settings_registry, storage
from rootcoz import transcripts as sidecar_transcripts
from rootcoz.auth.sso import PROVIDER_CLASSES
from rootcoz.auth.sso.base import SSOProvider
from rootcoz.auth.sso.oidc import OIDCProvider
from rootcoz.config import (
    Settings,
    clear_db_settings_cache,
    clear_settings_cache,
    get_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def registry_db(tmp_path, monkeypatch):
    """Isolated SQLite DB + sidecar env bridge for direct registry lifecycle tests."""
    db_path = tmp_path / "settings_registry.db"
    bridge_path = tmp_path / "sidecar.env"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setattr(sidecar_transcripts, "ENV_BRIDGE_PATH", bridge_path)
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SECURE_COOKIES", "false")
    clear_db_settings_cache()
    clear_settings_cache()
    await storage.init_db()
    yield bridge_path
    clear_db_settings_cache()
    clear_settings_cache()


def _recording_hooks(
    *,
    sync_return: bool = True,
    exporters: list[str] | None = None,
    providers: dict[str, str] | None = None,
    authenticated: frozenset[str] | None = None,
) -> tuple[settings_registry.SettingsRuntimeHooks, dict[str, list]]:
    calls: dict[str, list] = {
        "apply_log_file": [],
        "sync_transcript_recording": [],
        "broadcast_change": [],
    }

    async def _sync(enabled: bool | None) -> bool:
        calls["sync_transcript_recording"].append(enabled)
        return sync_return

    async def _spellings() -> dict[str, str]:
        return dict(providers or {})

    async def _auth(ids: list[str]) -> frozenset[str] | None:
        return authenticated

    hooks = settings_registry.SettingsRuntimeHooks(
        apply_log_file=lambda: calls["apply_log_file"].append(True),
        sync_transcript_recording=_sync,
        broadcast_change=lambda: calls["broadcast_change"].append(True),
        exporter_names=lambda: list(exporters or ["reportportal", "datadog"]),
        live_provider_spellings=_spellings,
        deployment_authenticated_providers=_auth,
    )
    return hooks, calls


# ---------------------------------------------------------------------------
# Registry structural & single-source invariants
# ---------------------------------------------------------------------------


def test_every_settings_field_is_registered_exactly_once() -> None:
    keys = [spec.key for spec in settings_registry.SETTINGS_SPECS]
    assert len(keys) == len(set(keys)), "Duplicate SettingSpec key detected"
    assert set(keys) == set(Settings.model_fields)


def test_category_order_is_contiguous_and_emitted_in_metadata() -> None:
    meta = settings_registry.get_settings_metadata()
    by_key = {item["key"]: item for item in meta}
    assert set(by_key) == set(Settings.model_fields)

    expected_order = {
        cat: idx for idx, cat in enumerate(settings_registry.CATEGORY_ORDER)
    }
    for spec in settings_registry.SETTINGS_SPECS:
        item = by_key[spec.key]
        assert item["category"] == spec.category
        assert item["category_order"] == expected_order[spec.category]


def test_ui_metadata_fields_emitted_only_where_declared() -> None:
    by_key = {item["key"]: item for item in settings_registry.get_settings_metadata()}

    # Transcript group drives the live usage card in ServerSettingsPage.tsx
    transcript_keys = {
        "pi_sidecar_transcripts_dir",
        "pi_sidecar_transcripts_max_age_days",
        "pi_sidecar_transcripts_max_size_mb",
    }
    for key, item in by_key.items():
        if key in transcript_keys:
            assert item["group"] == "transcripts"
        else:
            assert "group" not in item

    assert (
        by_key["login_lockout_threshold"]["hint"]
        == "Failed logins before the account locks. 0 disables lockout."
    )
    assert "hint" not in by_key["ai_model"]

    assert by_key["max_wait_minutes"]["value_traps"] == {
        "0": "0 means wait forever — the build is polled until it finishes."
    }
    assert "value_traps" not in by_key["jenkins_timeout"]


# ---------------------------------------------------------------------------
# Single-source SSO provider derivation (no hardcoded tuples)
# ---------------------------------------------------------------------------


def test_sso_url_rules_derived_from_provider_classes() -> None:
    rules = settings_registry.sso_url_field_rules(PROVIDER_CLASSES)
    assert rules == {
        "sso_openshift_issuer": ("issuer", False),
        "sso_openshift_api_url": ("cluster API URL", False),
        "sso_redhat_issuer": ("issuer", True),
    }


def test_validate_and_strip_sso_fields_handles_synthetic_fifth_provider() -> None:
    """A new provider needs zero edits to config.py or settings_registry.py."""

    class FakeOIDCProvider(OIDCProvider):
        name = "fake_oidc"
        display_name = "Fake OIDC"
        issuer_setting = "sso_fake_issuer"
        client_id_setting = "sso_fake_client_id"
        client_secret_setting = "sso_fake_client_secret"  # pragma: allowlist secret

    class FakeClusterProvider(SSOProvider):
        name = "fake_cluster"
        display_name = "Fake Cluster"
        issuer_setting = "sso_cluster_issuer"
        api_url_setting = "sso_cluster_api_url"
        client_id_setting = "sso_cluster_client_id"
        client_secret_setting = "sso_cluster_client_secret"  # pragma: allowlist secret

        @classmethod
        def from_settings(cls, settings: Settings):
            return None

        async def authorization_url(self, **kwargs):
            return ""

        async def exchange(self, **kwargs):
            raise NotImplementedError

    providers = (FakeOIDCProvider, FakeClusterProvider)
    specs = settings_registry._build_sso_provider_specs(providers)
    by_key = {spec.key: spec for spec in specs}
    assert by_key["sso_fake_client_secret"].sensitive is True
    assert by_key["sso_fake_issuer"].pre_validate_model is True
    assert by_key["sso_cluster_api_url"].pre_validate_model is True
    assert by_key["sso_cluster_client_id"].section == "fake_cluster"
    assert by_key["sso_cluster_client_id"].section_collapsible is True

    # Whitespace stripping + trailing slash normalization + realm path allowed on OIDC
    candidate = SimpleNamespace(
        sso_fake_client_id="  fake-id  ",
        sso_fake_client_secret=SecretStr("  "),
        sso_fake_issuer="  https://idp.example.com/realms/demo/  ",
        sso_cluster_client_id="  cluster-id ",
        sso_cluster_client_secret=SecretStr("  top-secret  "),
        sso_cluster_issuer="https://oauth.cluster.example.com/",
        sso_cluster_api_url="https://api.cluster.example.com:6443/",
    )
    settings_registry.validate_and_strip_sso_fields(candidate, providers=providers)
    assert candidate.sso_fake_client_id == "fake-id"
    assert candidate.sso_fake_client_secret is None
    assert candidate.sso_fake_issuer == "https://idp.example.com/realms/demo"
    assert candidate.sso_cluster_client_id == "cluster-id"
    assert candidate.sso_cluster_client_secret.get_secret_value() == "top-secret"
    assert candidate.sso_cluster_issuer == "https://oauth.cluster.example.com"
    assert candidate.sso_cluster_api_url == "https://api.cluster.example.com:6443"

    # Non-OIDC cluster API URL rejects a path with field-level ValidationError loc
    bad = SimpleNamespace(
        sso_fake_client_id="",
        sso_fake_client_secret=None,
        sso_fake_issuer="",
        sso_cluster_client_id="",
        sso_cluster_client_secret=None,
        sso_cluster_issuer="https://oauth.cluster.example.com",
        sso_cluster_api_url="https://api.cluster.example.com:6443/unexpected/path",
    )
    with pytest.raises(ValidationError) as exc_info:
        settings_registry.validate_and_strip_sso_fields(bad, providers=providers)
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("sso_cluster_api_url",)
    assert "bare origin" in str(errors[0]["ctx"]["error"])


# ---------------------------------------------------------------------------
# Unified write, persist, reset, export & import lifecycle + hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_and_reset_invoke_runtime_hooks(registry_db) -> None:
    hooks, calls = _recording_hooks(sync_return=True)

    # 1. Log-file setting invokes apply_log_file + broadcast
    res = await settings_registry.update_settings(
        {"log_max_bytes": "1048576"}, "alice", hooks=hooks
    )
    assert res.changed_keys == ["log_max_bytes"]
    assert len(calls["apply_log_file"]) == 1
    assert len(calls["broadcast_change"]) == 1
    assert get_settings().log_max_bytes == 1048576

    # 2. Sidecar + transcript setting writes bridge, syncs sidecar, warns on 0.0.0.0
    res2 = await settings_registry.update_settings(
        {"sidecar_host": "0.0.0.0", "record_transcripts": "true"},
        "alice",
        hooks=hooks,
    )
    assert any("loopback" in w for w in res2.warnings)
    assert calls["sync_transcript_recording"] == [True]
    bridge_text = registry_db.read_text()
    assert "SIDECAR_HOST=0.0.0.0" in bridge_text
    assert "RECORD_TRANSCRIPTS=true" in bridge_text

    # 3. Reset removes DB override, re-runs log_file hook + broadcast
    reset_res = await settings_registry.reset_setting(
        "log_max_bytes", "alice", hooks=hooks
    )
    assert reset_res.content == {"reset": "log_max_bytes"}
    assert len(calls["apply_log_file"]) == 2
    assert len(calls["broadcast_change"]) == 3


@pytest.mark.asyncio
async def test_update_invalidates_jira_verified_method_and_encrypts_secrets(
    registry_db,
) -> None:
    hooks, _ = _recording_hooks()
    await storage.set_server_settings_batch(
        {"jira_verified_method": "basic"}, updated_by="alice"
    )

    secret = "my-secret-jira-token"  # pragma: allowlist secret
    result = await settings_registry.update_settings(
        {"jira_api_token": secret}, "alice", hooks=hooks
    )
    assert result.redacted_keys == ["jira_api_token"]

    stored = await storage.get_server_settings()
    assert "jira_verified_method" not in stored
    assert stored["jira_api_token"]["value"] != secret

    # Masked by default, revealed on explicit request
    masked = {
        item["key"]: item for item in await settings_registry.list_admin_settings()
    }
    assert masked["jira_api_token"]["value"] == "••••••••"
    assert masked["jira_api_token"]["is_set"] is True

    revealed = {
        item["key"]: item
        for item in await settings_registry.list_admin_settings(
            reveal_key="jira_api_token"
        )
    }
    assert revealed["jira_api_token"]["value"] == secret


@pytest.mark.asyncio
async def test_export_and_import_round_trip_and_secret_redaction(registry_db) -> None:
    hooks, _ = _recording_hooks()
    await settings_registry.update_settings(
        {
            "ai_call_timeout": "25",
            "jenkins_password": "secret-pw",  # pragma: allowlist secret
            "jenkins_url": "https://user:pass@jenkins.example.com",  # pragma: allowlist secret
        },
        "alice",
        hooks=hooks,
    )

    # Default export omits sensitive keys and strips URL credentials
    default_export = await settings_registry.export_settings(include_sensitive=False)
    assert "jenkins_password" not in default_export.content["settings"]
    assert "warning" not in default_export.content
    assert default_export.content["settings"]["ai_call_timeout"] == "25"
    assert "user:pass" not in default_export.content["settings"]["jenkins_url"]

    # Opt-in sensitive export decrypts secrets and attaches warning
    full_export = await settings_registry.export_settings(include_sensitive=True)
    assert (
        full_export.content["settings"]["jenkins_password"]
        == "secret-pw"  # pragma: allowlist secret
    )
    assert "warning" in full_export.content

    # Re-importing the sanitized default export marks credentials-stripped URL unchanged
    dry_run = await settings_registry.import_settings(
        {**default_export.content, "dry_run": True}, "bob", hooks=hooks
    )
    assert dry_run.dry_run is True
    assert "jenkins_url" in dry_run.content["unchanged"]

    # Import with a real change applies atomically and rejects derived keys
    with pytest.raises(HTTPException) as bad_exc:
        await settings_registry.import_settings(
            {"version": 1, "settings": {"sidecar_url": "http://127.0.0.1:9999"}},
            "bob",
            hooks=hooks,
        )
    assert bad_exc.value.status_code == 422

    applied = await settings_registry.import_settings(
        {"version": 1, "settings": {"ai_call_timeout": "40", "log_level": "debug"}},
        "bob",
        hooks=hooks,
    )
    assert applied.dry_run is False
    assert set(applied.changed_keys) == {"ai_call_timeout", "log_level"}
    assert applied.content["restart_required"] == ["log_level"]
    assert get_settings().ai_call_timeout == 40
    assert get_settings().log_level == "DEBUG"


@pytest.mark.asyncio
async def test_persist_setting_validates_and_rejects_server_only_and_derived(
    registry_db, monkeypatch
) -> None:
    hooks, _ = _recording_hooks()
    monkeypatch.setenv("ADMIN_KEY", "boot-secret")
    monkeypatch.setenv("SIDECAR_URL", "http://127.0.0.1:9100")
    monkeypatch.setenv("AI_CALL_TIMEOUT", "33")

    with pytest.raises(HTTPException) as srv_exc:
        await settings_registry.persist_setting("admin_key", "alice", hooks=hooks)
    assert srv_exc.value.status_code == 400

    with pytest.raises(HTTPException) as der_exc:
        await settings_registry.persist_setting("sidecar_url", "alice", hooks=hooks)
    assert der_exc.value.status_code == 400

    persisted = await settings_registry.persist_setting(
        "ai_call_timeout", "alice", hooks=hooks
    )
    assert persisted.key == "ai_call_timeout"
    assert get_settings().ai_call_timeout == 33


@pytest.mark.asyncio
async def test_default_server_settings_excludes_sensitive_and_hidden() -> None:
    payload = settings_registry.get_default_server_settings_payload(
        Settings(
            _env_file=None,
            jenkins_password="pw",  # pragma: allowlist secret
            admin_key="k",
        ),
        lambda settings: ("claude", "claude-sonnet"),
    )
    for key in settings_registry.SENSITIVE_SETTINGS:
        assert key not in payload
    for key in settings_registry.DEFAULTS_HIDDEN_SETTINGS:
        assert key not in payload
    assert payload["ai_provider"] == "claude"
    assert payload["ai_model"] == "claude-sonnet"


@pytest.mark.asyncio
async def test_transcript_sync_failure_surfaces_warning_on_update_and_reset(
    registry_db,
) -> None:
    hooks, _ = _recording_hooks(sync_return=False)
    updated = await settings_registry.update_settings(
        {"record_transcripts": "true"}, "alice", hooks=hooks
    )
    assert any("running sidecar could not be updated" in w for w in updated.warnings)

    reset = await settings_registry.reset_setting(
        "record_transcripts", "alice", hooks=hooks
    )
    assert any(
        "running sidecar could not be updated" in w
        for w in reset.content.get("warnings", [])
    )


@pytest.mark.asyncio
async def test_validate_settings_updates_uses_dynamic_hooks_for_exporters_and_providers(
    registry_db,
) -> None:
    hooks, _ = _recording_hooks(
        exporters=["reportportal"],
        providers={"claude": "claude"},
        authenticated=frozenset({"claude"}),
    )
    updates = {
        "auto_push_exporters": "ReportPortal",
        "ambient_auth_providers": "CLAUDE",
    }
    errors = await settings_registry.validate_settings_updates(updates, hooks=hooks)
    assert errors == []
    assert updates["auto_push_exporters"] == "reportportal"
    assert updates["ambient_auth_providers"] == "claude"

    bad_errors = await settings_registry.validate_settings_updates(
        {"auto_push_exporters": "unknown_exp"}, hooks=hooks
    )
    assert any("unknown exporter plugin" in e for e in bad_errors)


@pytest.mark.asyncio
async def test_list_admin_settings_uses_provider_options(
    registry_db, monkeypatch
) -> None:
    monkeypatch.setattr(
        "rootcoz.ai_client.server_provider_options",
        AsyncMock(return_value=["claude", "gemini"]),
    )
    items = {
        item["key"]: item for item in await settings_registry.list_admin_settings()
    }
    assert items["active_server_providers"]["options"] == ["claude", "gemini"]


# ---------------------------------------------------------------------------
# Architectural deepening invariants (#663)
# ---------------------------------------------------------------------------


def test_main_py_owns_no_duplicated_settings_tables_or_lifecycle_helpers() -> None:
    """main.py must delegate settings registry tables and lifecycle to settings_registry."""
    main_tree = ast.parse((REPO_ROOT / "src/rootcoz/main.py").read_text())
    top_level_funcs = {
        node.name
        for node in main_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for removed in (
        "_get_settings_metadata",
        "_save_settings_to_db",
        "_normalize_enumerated_value",
        "_sanitize_export_value",
        "_mask_diff_value",
        "_diff_import_updates",
        "_empty_override_error",
        "_sso_provider_settings",
        "_sso_sensitive_settings",
        "_sso_restart_required_settings",
        "_sso_section",
    ):
        assert removed not in top_level_funcs, f"{removed} still defined in main.py"


def test_config_py_has_no_hardcoded_sso_setting_tuples() -> None:
    """config.py delegates SSO provider stripping/URL validation to settings_registry."""
    config_text = (REPO_ROOT / "src/rootcoz/config.py").read_text()
    assert "def _validate_issuer(" not in config_text
    assert "_SERVER_ONLY_SETTINGS in main.py" not in config_text


def test_frontend_server_settings_page_has_no_mirrored_backend_constants() -> None:
    """ServerSettingsPage.tsx reads category order & grouping from backend metadata."""
    page_text = (REPO_ROOT / "frontend/src/pages/ServerSettingsPage.tsx").read_text()
    for mirrored in (
        "const CATEGORY_ORDER",
        "const TRANSCRIPT_SETTING_KEYS",
        "const JIRA_SHARED_FIELDS",
        "const SERVER_ONLY_SETTING_KEYS",
    ):
        assert mirrored not in page_text, f"{mirrored} still mirrored in frontend"
