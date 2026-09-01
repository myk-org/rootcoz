"""Tests for Greenwave settings in config.py."""

import os
from unittest.mock import patch

import pytest

from rootcoz.config import Settings, validate_db_settings_candidate
from rootcoz.greenwave import evaluate_greenwave_transport, referenced_placeholders
from tests.conftest import build_test_env as _build_env


class TestGreenwaveSettings:
    """Test Greenwave settings fields and the greenwave_enabled property."""

    def test_gw_disabled_by_default(self):
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_gw_stays_disabled_without_explicit_enable(self):
        env = _build_env(
            GREENWAVE_URL="https://gw.example.com",
            GREENWAVE_API_TOKEN="gw-token",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_gw_enabled_when_explicitly_enabled_and_configured(self):
        env = _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_URL="https://gw.example.com",
            GREENWAVE_API_TOKEN="gw-token",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert settings.greenwave_enabled

    def test_gw_disabled_when_url_missing(self):
        env = _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_API_TOKEN="gw-token",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_gw_disabled_when_token_missing(self):
        env = _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_URL="https://gw.example.com",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_gw_explicitly_disabled_overrides_config(self):
        env = _build_env(
            GREENWAVE_URL="https://gw.example.com",
            GREENWAVE_API_TOKEN="gw-token",  # pragma: allowlist secret
            ENABLE_GREENWAVE="false",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    @patch("rootcoz.config.logger")
    def test_gw_warns_when_enabled_but_url_missing(self, mock_logger):
        env = _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_API_TOKEN="gw-token",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled
        mock_logger.warning.assert_called()
        warn_msg = mock_logger.warning.call_args[0][0]
        assert "GREENWAVE_URL" in warn_msg

    @patch("rootcoz.config.logger")
    def test_gw_warns_when_enabled_but_token_missing(self, mock_logger):
        env = _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_URL="https://gw.example.com",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled
        mock_logger.warning.assert_called()
        warn_msg = mock_logger.warning.call_args[0][0]
        assert "GREENWAVE_API_TOKEN" in warn_msg

    def test_gw_rejects_url_credentials(self):
        env = _build_env(
            GREENWAVE_URL="https://user:secret@gw.example.com",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "embedded credentials" in str(exc.value)

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://gw.example.com/api?tenant=private", "query string or fragment"),
            ("https://gw.example.com/api#results", "query string or fragment"),
            ("https://gw.example.com:not-a-port/api", "malformed"),
            ("https://gw.example.com:70000/api", "malformed"),
            ("ftp://gw.example.com/api", "HTTP(S)"),
            ("https:///api", "hostname"),
        ],
    )
    def test_gw_rejects_invalid_base_urls(self, url, expected):
        with patch.dict(
            os.environ,
            _build_env(GREENWAVE_URL=url),
            clear=True,
        ):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert expected in str(exc.value)

    def test_gw_normalizes_base_url_before_endpoint_concatenation(self):
        policy = evaluate_greenwave_transport(
            "HTTPS://ResultsDB.EXAMPLE.COM/api/v2.0///",
            service="ResultsDB",
            auth_method="none",
            verify=True,
        )
        assert policy.error is None
        assert policy.base_url == "https://resultsdb.example.com/api/v2.0"

    def test_gw_outcome_map_malformed_raises(self):
        env = _build_env(
            GREENWAVE_OUTCOME_MAP="PRODUCTBUG",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "GREENWAVE_OUTCOME_MAP" in str(exc.value)

    def test_gw_outcome_map_invalid_outcome_raises(self):
        env = _build_env(
            GREENWAVE_OUTCOME_MAP="PRODUCT BUG:BOGUS",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "BOGUS" in str(exc.value)

    @pytest.mark.parametrize(
        "outcome_map",
        [
            "Code Issue:INFO,CODE ISSUE:FAILED",
            "CODE ISSUE:INFO,CODE ISSUE:FAILED",
        ],
    )
    def test_gw_outcome_map_rejects_casefold_duplicate_keys(self, outcome_map):
        env = _build_env(GREENWAVE_OUTCOME_MAP=outcome_map)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "Duplicate Greenwave outcome-map classification keys" in str(
                exc.value
            )

    @patch("rootcoz.config.logger")
    def test_gw_no_false_warning_on_case_mismatch(self, mock_logger):
        env = _build_env(
            GREENWAVE_OUTCOME_MAP="CODE ISSUE:FAILED",
            GREENWAVE_WAIVABLE_CLASSIFICATIONS="code issue",
        )
        with patch.dict(os.environ, env, clear=True):
            Settings(_env_file=None)

        for call in mock_logger.warning.call_args_list:
            assert "GREENWAVE_WAIVABLE_CLASSIFICATIONS" not in call[0][0]

    @patch("rootcoz.config.logger")
    def test_gw_warns_when_waivable_absent_from_map(self, mock_logger):
        env = _build_env(
            GREENWAVE_OUTCOME_MAP="PRODUCT BUG:FAILED",
            GREENWAVE_WAIVABLE_CLASSIFICATIONS="INFRASTRUCTURE",
        )
        with patch.dict(os.environ, env, clear=True):
            Settings(_env_file=None)

        warning_found = False
        for call in mock_logger.warning.call_args_list:
            if "GREENWAVE_WAIVABLE_CLASSIFICATIONS" in call[0][0]:
                warning_found = True
                break
        assert warning_found

    def test_gw_kerberos_without_keytab_raises(self):
        env = _build_env(
            GREENWAVE_RESULTSDB_AUTH_METHOD="kerberos",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "KERBEROS_KEYTAB" in str(exc.value)

    def test_gw_ssl_without_cert_raises(self):
        env = _build_env(
            GREENWAVE_RESULTSDB_AUTH_METHOD="ssl",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "SSL_CERT" in str(exc.value)

    def test_gw_invalid_resultsdb_auth_method_raises(self):
        env = _build_env(
            GREENWAVE_RESULTSDB_AUTH_METHOD="bogus",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "greenwave_resultsdb_auth_method" in str(exc.value)

    def test_gw_invalid_waiver_auth_method_raises(self):
        env = _build_env(
            GREENWAVE_WAIVER_AUTH_METHOD="bogus",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "greenwave_waiver_auth_method" in str(exc.value)

    @pytest.mark.parametrize(
        "template",
        [
            "rootcoz.{test_name!r}",
            "rootcoz.{test_name:>1000000000}",
        ],
    )
    def test_gw_template_rejects_conversions_and_format_specs(self, template):
        env = _build_env(GREENWAVE_TESTCASE_TEMPLATE=template)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "conversions or format specifications" in str(exc.value)

    def test_gw_invalid_template_placeholder_raises(self):
        env = _build_env(
            GREENWAVE_TESTCASE_TEMPLATE="{bad_key}.{test_name}",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "bad_key" in str(exc.value) or "placeholder" in str(exc.value)

    def test_gw_authenticated_http_transport_matches_exporter_policy(self):
        rejected = _build_env(
            GREENWAVE_URL="http://resultsdb.example.com/api",
            GREENWAVE_RESULTSDB_AUTH_METHOD="token",
            GREENWAVE_API_TOKEN="token",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, rejected, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "writes require HTTPS" in str(exc.value)

        # token auth + verify=False is also rejected (credential-leak risk,
        # finding #183): only unauthenticated (none) auth gets the HTTP escape
        # hatch.  The none+http+verify=False case is covered by
        # test_gw_unauthenticated_http_also_requires_escape_hatch.
        rejected_verify_false = _build_env(
            GREENWAVE_URL="http://resultsdb.example.com/api",
            GREENWAVE_RESULTSDB_AUTH_METHOD="token",
            GREENWAVE_API_TOKEN="token",  # pragma: allowlist secret
            GREENWAVE_VERIFY_SSL="false",
        )
        with patch.dict(os.environ, rejected_verify_false, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "writes require HTTPS" in str(exc.value)

    def test_gw_unauthenticated_http_also_requires_escape_hatch(self):
        rejected = _build_env(
            GREENWAVE_URL="http://resultsdb.example.com/api",
            GREENWAVE_RESULTSDB_AUTH_METHOD="none",
        )
        with patch.dict(os.environ, rejected, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "writes require HTTPS" in str(exc.value)

        allowed = _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_URL="http://resultsdb.example.com/api",
            GREENWAVE_RESULTSDB_AUTH_METHOD="none",
            GREENWAVE_VERIFY_SSL="false",
        )
        with patch.dict(os.environ, allowed, clear=True):
            assert Settings(_env_file=None).greenwave_enabled

    def test_gw_ca_bundle_prevents_http_escape_hatch(self):
        env = _build_env(
            GREENWAVE_URL="http://resultsdb.example.com/api",
            GREENWAVE_RESULTSDB_AUTH_METHOD="token",
            GREENWAVE_API_TOKEN="token",  # pragma: allowlist secret
            GREENWAVE_VERIFY_SSL="false",
            GREENWAVE_CA_BUNDLE="/etc/pki/private-ca.pem",
        )
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc:
                Settings(_env_file=None)
            assert "no CA bundle" in str(exc.value)

    def test_db_override_is_applied_before_cross_field_settings_validation(self):
        env = _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_URL="https://resultsdb.example.com/api",
            GREENWAVE_RESULTSDB_AUTH_METHOD="kerberos",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = validate_db_settings_candidate(
                {"greenwave_kerberos_keytab": "/etc/rootcoz/greenwave.keytab"}
            )
        assert settings.greenwave_kerberos_keytab == "/etc/rootcoz/greenwave.keytab"
        assert settings.greenwave_enabled

    def test_gw_server_settings_metadata_is_complete(self):
        from rootcoz.main import (
            _RESTART_REQUIRED_SETTINGS,
            _SENSITIVE_SETTINGS,
            _SERVER_ONLY_SETTINGS,
            _SETTINGS_CATEGORIES,
        )

        greenwave_fields = {
            name
            for name in Settings.model_fields
            if name == "enable_greenwave" or name.startswith("greenwave_")
        }
        # Finding #6: env-only gating-safety toggles must NOT appear in
        # _SETTINGS_CATEGORIES (they would mislead the UI into showing them as
        # editable).  The UI-visible Greenwave fields are the full set minus the
        # three server-only toggles.
        _SERVER_ONLY_GREENWAVE = {
            "enable_greenwave",
            "greenwave_push_waivers",
            "greenwave_allow_ai_waivers",
        }
        assert _SERVER_ONLY_GREENWAVE.issubset(_SERVER_ONLY_SETTINGS)
        expected_ui_fields = greenwave_fields - _SERVER_ONLY_GREENWAVE
        assert set(_SETTINGS_CATEGORIES["Greenwave"]) == expected_ui_fields
        assert greenwave_fields & _SENSITIVE_SETTINGS == {
            "greenwave_api_token",
            "greenwave_waiver_token",
        }
        assert greenwave_fields & _RESTART_REQUIRED_SETTINGS == {
            "enable_greenwave",
            "greenwave_push_waivers",
            "greenwave_allow_ai_waivers",
        }

    @patch("rootcoz.config.logger")
    def test_gw_tier_placeholder_without_tier_warns(self, mock_logger):
        env = _build_env(
            GREENWAVE_TESTCASE_TEMPLATE="rootcoz.{tier}.{test_name}",
        )
        with patch.dict(os.environ, env, clear=True):
            Settings(_env_file=None)

        mock_logger.warning.assert_called()
        warning_found = False
        for call in mock_logger.warning.call_args_list:
            if "GREENWAVE_TIER" in call[0][0] or "tier" in call[0][0]:
                warning_found = True
                break
        assert warning_found


class TestAutoPushExportersGreenwaveRejection:
    """Greenwave cannot be used in AUTO_PUSH_EXPORTERS (issue #183)."""

    def _env(self, **overrides: str) -> dict[str, str]:
        return _build_env(**overrides)

    def test_greenwave_alone_raises(self):
        """auto_push_exporters='greenwave' without GREENWAVE_SUBJECT_TEMPLATE is rejected."""
        env = self._env(AUTO_PUSH_EXPORTERS="greenwave")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="AUTO_PUSH_EXPORTERS") as exc_info,
        ):
            Settings(_env_file=None)
        # Error must mention both the setting name and the required template env var.
        assert "greenwave" in str(exc_info.value)
        assert "GREENWAVE_SUBJECT_TEMPLATE" in str(exc_info.value)

    def test_greenwave_control_char_bypass_is_rejected(self):
        """'greenwave\x01' (control char) must also be rejected (FIX 1: shared sanitization)."""
        env = self._env(AUTO_PUSH_EXPORTERS="greenwave\x01")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="AUTO_PUSH_EXPORTERS"),
        ):
            Settings(_env_file=None)

    def test_mixed_list_with_greenwave_raises(self):
        """greenwave mixed with another exporter must still be rejected."""
        env = self._env(AUTO_PUSH_EXPORTERS="reportportal,greenwave")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="AUTO_PUSH_EXPORTERS"),
        ):
            Settings(_env_file=None)

    def test_greenwave_only_other_exporter_does_not_raise(self):
        """reportportal without greenwave must not trigger the greenwave-specific error."""
        env = self._env(AUTO_PUSH_EXPORTERS="reportportal")
        with patch.dict(os.environ, env, clear=True):
            # Should not raise for the greenwave-incompatibility reason.
            # Other validators may raise for unrelated reasons; only check the
            # greenwave auto-push message is absent.
            try:
                Settings(_env_file=None)
            except ValueError as exc:
                msg = str(exc)
                assert not ("AUTO_PUSH_EXPORTERS" in msg and "greenwave" in msg), (
                    f"Raised AUTO_PUSH_EXPORTERS/greenwave error unexpectedly: {exc}"
                )

    def test_greenwave_case_insensitive_upper(self):
        """'GREENWAVE' (all-caps) must also be rejected."""
        env = self._env(AUTO_PUSH_EXPORTERS="GREENWAVE")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="greenwave"),
        ):
            Settings(_env_file=None)

    def test_greenwave_case_insensitive_mixed_with_whitespace(self):
        """' Greenwave ' (whitespace + mixed-case) must be rejected."""
        env = self._env(AUTO_PUSH_EXPORTERS=" Greenwave ")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="greenwave"),
        ):
            Settings(_env_file=None)

    def test_empty_auto_push_exporters_does_not_raise_greenwave_error(self):
        """Empty AUTO_PUSH_EXPORTERS must not raise the greenwave error."""
        env = self._env(AUTO_PUSH_EXPORTERS="")
        with patch.dict(os.environ, env, clear=True):
            # May raise for other reasons; only check greenwave message is absent.
            try:
                Settings(_env_file=None)
            except ValueError as exc:
                msg = str(exc)
                assert not ("AUTO_PUSH_EXPORTERS" in msg and "greenwave" in msg), (
                    f"Unexpected greenwave auto-push error: {exc}"
                )


class TestGreenwaveEnabledWaiverDBPrereqs:
    """Finding #5: greenwave_enabled must also check WaiverDB prereqs when push_waivers=True."""

    def _base_env(self, **extra: str) -> dict[str, str]:
        return _build_env(
            ENABLE_GREENWAVE="true",
            GREENWAVE_URL="https://resultsdb.example.com/api/v2.0",
            GREENWAVE_API_TOKEN="rdb-tok",  # pragma: allowlist secret
            GREENWAVE_PUSH_WAIVERS="true",
            **extra,
        )

    def test_push_waivers_missing_waiver_url_returns_false(self):
        env = self._base_env()
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert not s.greenwave_enabled

    @patch("rootcoz.config.logger")
    def test_push_waivers_missing_waiver_url_warns(self, mock_logger):
        env = self._base_env()
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert not s.greenwave_enabled
        calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("GREENWAVE_WAIVER_URL" in c for c in calls)

    def test_push_waivers_missing_product_version_returns_false(self):
        env = self._base_env(
            GREENWAVE_WAIVER_URL="https://waiverdb.example.com/api/v1.0",
            GREENWAVE_WAIVER_TOKEN="wvr-tok",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert not s.greenwave_enabled

    @patch("rootcoz.config.logger")
    def test_push_waivers_missing_product_version_warns(self, mock_logger):
        env = self._base_env(
            GREENWAVE_WAIVER_URL="https://waiverdb.example.com/api/v1.0",
            GREENWAVE_WAIVER_TOKEN="wvr-tok",  # pragma: allowlist secret
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert not s.greenwave_enabled
        calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("GREENWAVE_PRODUCT_VERSION" in c for c in calls)

    def test_push_waivers_missing_oidc_token_returns_false(self):
        env = self._base_env(
            GREENWAVE_WAIVER_URL="https://waiverdb.example.com/api/v1.0",
            GREENWAVE_PRODUCT_VERSION="prod-1.0",
            GREENWAVE_WAIVER_AUTH_METHOD="oidc",
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert not s.greenwave_enabled

    @patch("rootcoz.config.logger")
    def test_push_waivers_missing_oidc_token_warns(self, mock_logger):
        env = self._base_env(
            GREENWAVE_WAIVER_URL="https://waiverdb.example.com/api/v1.0",
            GREENWAVE_PRODUCT_VERSION="prod-1.0",
            GREENWAVE_WAIVER_AUTH_METHOD="oidc",
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert not s.greenwave_enabled
        calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("GREENWAVE_WAIVER_TOKEN" in c for c in calls)

    def test_fully_configured_push_waivers_returns_true(self):
        env = self._base_env(
            GREENWAVE_WAIVER_URL="https://waiverdb.example.com/api/v1.0",
            GREENWAVE_WAIVER_TOKEN="wvr-tok",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="prod-1.0",
            GREENWAVE_WAIVER_AUTH_METHOD="oidc",
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert s.greenwave_enabled


class TestGreenwaveSubjectTemplate:
    """Feature: GREENWAVE_SUBJECT_TEMPLATE enables auto-push with constructed NVR."""

    def _env(self, **extra: str) -> dict[str, str]:
        return _build_env(**extra)

    def test_greenwave_with_template_allowed_at_load(self):
        """AUTO_PUSH_EXPORTERS=greenwave WITH fully-resolved template succeeds at config load.

        Uses only runtime placeholders ({build_number}) so no server-config
        values are needed at load time. Asserts Settings constructs successfully
        with greenwave accepted in AUTO_PUSH_EXPORTERS.
        """
        env = self._env(
            AUTO_PUSH_EXPORTERS="greenwave",
            GREENWAVE_SUBJECT_TEMPLATE="build-{build_number}",
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
        assert "greenwave" in s.auto_push_exporters

    def test_subject_template_field_defaults_to_none(self):
        env = self._env()
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert s.greenwave_subject_template is None

    def test_subject_template_is_stored(self):
        tmpl = "hco-bundle-{product_version}.rhel9-{build_number}"
        env = self._env(GREENWAVE_SUBJECT_TEMPLATE=tmpl)
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert s.greenwave_subject_template == tmpl

    def test_subject_template_whitespace_only_normalizes_to_none(self):
        env = self._env(GREENWAVE_SUBJECT_TEMPLATE="   ")
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
            assert s.greenwave_subject_template is None

    def test_subject_template_invalid_placeholder_raises(self):
        env = self._env(GREENWAVE_SUBJECT_TEMPLATE="{bad_key}-{build_number}")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="bad_key"),
        ):
            Settings(_env_file=None)

    def test_subject_template_rejects_format_spec(self):
        env = self._env(GREENWAVE_SUBJECT_TEMPLATE="{build_number:04d}")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="conversions or format specifications"),
        ):
            Settings(_env_file=None)

    def test_subject_template_in_settings_categories(self):
        from rootcoz.main import _SETTINGS_CATEGORIES

        assert "greenwave_subject_template" in _SETTINGS_CATEGORIES["Greenwave"]


class TestGreenwaveSubjectTemplatePlaceholderValidation:
    """FIX 4: server-config placeholders in template must resolve when auto-push is enabled."""

    def _env(self, **extra: str) -> dict[str, str]:
        return _build_env(**extra)

    def test_tier_placeholder_without_greenwave_tier_rejected(self):
        """AUTO_PUSH=greenwave + template with {tier} + no GREENWAVE_TIER => rejected."""
        env = self._env(
            AUTO_PUSH_EXPORTERS="greenwave",
            GREENWAVE_SUBJECT_TEMPLATE="build-{tier}-{build_number}",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="GREENWAVE_TIER"),
        ):
            Settings(_env_file=None)

    def test_product_version_placeholder_without_value_rejected(self):
        """AUTO_PUSH=greenwave + {product_version} in template + no GREENWAVE_PRODUCT_VERSION => rejected."""
        env = self._env(
            AUTO_PUSH_EXPORTERS="greenwave",
            GREENWAVE_SUBJECT_TEMPLATE="build-{product_version}-{build_number}",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="GREENWAVE_PRODUCT_VERSION"),
        ):
            Settings(_env_file=None)

    def test_fully_resolved_template_allowed(self):
        """AUTO_PUSH=greenwave + fully-resolved template (all server vars set) => allowed."""
        env = self._env(
            AUTO_PUSH_EXPORTERS="greenwave",
            GREENWAVE_SUBJECT_TEMPLATE="build-{product_version}-{tier}-{build_number}",
            GREENWAVE_PRODUCT_VERSION="v4.20",
            GREENWAVE_TIER="tier1",
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
        assert "greenwave" in s.auto_push_exporters

    def test_runtime_placeholders_not_checked_at_load(self):
        """Runtime placeholders {job_name} and {build_number} are not validated at config load."""
        env = self._env(
            AUTO_PUSH_EXPORTERS="greenwave",
            GREENWAVE_SUBJECT_TEMPLATE="build-{build_number}",
        )
        with patch.dict(os.environ, env, clear=True):
            s = Settings(_env_file=None)
        assert "greenwave" in s.auto_push_exporters

    def test_tier_placeholder_validation_skipped_when_not_auto_push(self):
        """Without AUTO_PUSH=greenwave, a missing tier value is only a warning, not an error."""
        env = self._env(
            GREENWAVE_SUBJECT_TEMPLATE="build-{tier}-{build_number}",
            # No GREENWAVE_TIER, no AUTO_PUSH_EXPORTERS=greenwave
        )
        with patch.dict(os.environ, env, clear=True):
            # Must NOT raise ValueError about GREENWAVE_TIER.
            s = Settings(_env_file=None)
        assert s.greenwave_subject_template is not None

    def test_control_char_only_tier_with_auto_push_is_rejected(self):
        """FIX 2: AUTO_PUSH_EXPORTERS=greenwave + {tier} template + GREENWAVE_TIER='\\x01' => rejected.

        A control-char-only tier value (SOH, \\x01) passes Python str.strip()
        and _normalize_optional_strings unchanged — it is not whitespace —
        but sanitize_control_chars removes it, leaving an empty string.  The
        auto-push fail-fast check must therefore reject it.

        Note: null bytes (\\x00) cannot be stored in Linux environment
        variables; \\x01 (SOH) is used as a representative non-whitespace
        control character that survives the OS env layer.
        """
        env = self._env(
            AUTO_PUSH_EXPORTERS="greenwave",
            GREENWAVE_SUBJECT_TEMPLATE="{tier}-x",
            GREENWAVE_TIER="\x01",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="GREENWAVE_TIER"),
        ):
            Settings(_env_file=None)


class TestGreenwaveTemplateControlCharRejection:
    """FIX 2 (config-load): templates with control chars in their literal text are rejected.

    _validate_greenwave_template_placeholders now checks the template literal
    with sanitize_control_chars before parsing placeholders.  This covers BOTH
    greenwave_subject_template and greenwave_testcase_template because both
    call the shared validator.
    """

    def _env(self, **extra: str) -> dict[str, str]:
        return _build_env(**extra)

    def test_subject_template_with_control_char_raises(self):
        """GREENWAVE_SUBJECT_TEMPLATE with a control char in the literal raises ValueError."""
        env = self._env(GREENWAVE_SUBJECT_TEMPLATE="build-\x01-{build_number}")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="control characters"),
        ):
            Settings(_env_file=None)

    def test_testcase_template_with_control_char_raises(self):
        """GREENWAVE_TESTCASE_TEMPLATE with a control char in the literal raises ValueError."""
        env = self._env(GREENWAVE_TESTCASE_TEMPLATE="rootcoz.\x01.{test_name}")
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="control characters"),
        ):
            Settings(_env_file=None)


class TestReferencedPlaceholders:
    """FIX 3 (quality): direct unit tests for referenced_placeholders() in rootcoz.greenwave."""

    def test_no_placeholders_returns_empty_set(self):
        assert referenced_placeholders("no placeholders here") == set()

    def test_single_placeholder(self):
        assert referenced_placeholders("{build_number}") == {"build_number"}

    def test_repeated_placeholder_returned_once(self):
        assert referenced_placeholders("{x}-{x}-{x}") == {"x"}

    def test_multiple_distinct_placeholders(self):
        assert referenced_placeholders("{job_name}-{build_number}-{tier}") == {
            "job_name",
            "build_number",
            "tier",
        }

    def test_escaped_braces_yield_no_placeholder(self):
        assert referenced_placeholders("{{literal}}") == set()

    def test_escaped_braces_mixed_with_placeholder(self):
        assert referenced_placeholders("{{literal}}-{real}") == {"real"}

    def test_format_spec_yields_field_name(self):
        assert referenced_placeholders("{x:>10}") == {"x"}

    def test_conversion_yields_field_name(self):
        assert referenced_placeholders("{y!r}") == {"y"}

    def test_format_spec_and_conversion_together(self):
        assert referenced_placeholders("{z!s:>20}") == {"z"}

    def test_empty_string_returns_empty_set(self):
        assert referenced_placeholders("") == set()
