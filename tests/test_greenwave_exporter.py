"""Tests for the Greenwave/ResultsDB exporter plugin (issue #183)."""

import asyncio
import os
import uuid as uuid_module
from dataclasses import replace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from rootcoz.config import (
    Settings,
    parse_greenwave_outcome_map,
    parse_greenwave_waivable_classifications,
)
from rootcoz.exporters.base import (
    ExportContext,
    ExporterPrerequisiteError,
    ExporterResult,
)
from rootcoz.exporters.greenwave_exporter import (
    GreenwaveExporter,
    _build_waiver_comment,
    _positive_integer_response_id,
    _sanitize_error,
    _testcase_name,
    _uuid_response_id,
)
from rootcoz.storage import AI_SYSTEM_USERNAME
from tests.conftest import build_test_env as _build_env

RESULTSDB_URL = "https://resultsdb.example.com/api/v2.0"
WAIVERDB_URL = "https://waiverdb.example.com/api/v1.0"


def _greenwave_env(**overrides: str) -> dict[str, str]:
    """Baseline env with Greenwave ResultsDB configured (no waivers)."""
    env = _build_env(
        GREENWAVE_URL=RESULTSDB_URL,
        GREENWAVE_API_TOKEN="rdb-token",  # pragma: allowlist secret
    )
    env.update(overrides)
    return env


def _make_settings(mode: str = "bearer", **env_overrides: str) -> Settings:
    """Build Settings under a patched environment.

    The auth mode is DB-only (the env var is ignored), so it is passed as
    an init kwarg — the same way ``get_settings()`` merges DB overrides.
    """
    with patch.dict(os.environ, _greenwave_env(**env_overrides), clear=True):
        return Settings(_env_file=None, greenwave_auth_mode=mode)


def _failure(
    test_name: str,
    classification: str = "",
    details: str = "",
) -> dict:
    """Build a FailureAnalysis-compatible dict for ExportContext.failures."""
    return {
        "test_name": test_name,
        "error": "boom",
        "analysis": {"classification": classification, "details": details},
    }


DEFAULT_SUBJECT = "pkg-1.0-1.el10"


def _make_context(
    failures: list[dict],
    *,
    subject_identifier: str | None = DEFAULT_SUBJECT,
    waiver_comment: str | None = None,
    reviewed_by: dict[str, str] | None = None,
    pushed_by: str = "operator",
    effective_classifications: dict[tuple[str, str, int], str] | None = None,
    child_job_name: str | None = None,
    child_build_number: int | None = None,
) -> ExportContext:
    return ExportContext(
        job_id="job-123",
        job_name="my-job",
        build_number="42",
        jenkins_url="https://jenkins.example.com",
        failures=failures,
        report_url="https://rootcoz.example.com/results/job-123",
        subject_identifier=subject_identifier,
        waiver_comment=waiver_comment,
        reviewed_by=reviewed_by or {},
        pushed_by=pushed_by,
        effective_classifications=effective_classifications or {},
        child_job_name=child_job_name,
        child_build_number=child_build_number,
    )


def _response(json_body: dict | None = None, *, error: Exception | None = None):
    """Mock httpx.Response with a JSON body and optional raise_for_status."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_body if json_body is not None else {}
    if error is not None:
        resp.raise_for_status.side_effect = error
    return resp


def _run_push(exporter: GreenwaveExporter, context: ExportContext):
    """Run the async push() method from sync test code."""
    return asyncio.run(exporter.push(context))


def _rendered_authorization(auth: httpx.Auth | None) -> str | None:
    """The Authorization header an ``httpx.Auth`` attaches to a request."""
    if auth is None:
        return None
    request = httpx.Request("POST", f"{WAIVERDB_URL}/waivers/")
    flow = auth.sync_auth_flow(request) if hasattr(auth, "sync_auth_flow") else None
    if flow is None:
        return None
    sent = next(flow)
    return sent.headers.get("Authorization")


class TestGreenwaveSettings:
    """Settings-level configuration for the Greenwave exporter."""

    def test_disabled_by_default(self):
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_enabled_when_mode_fields_configured(self):
        settings = _make_settings()
        assert settings.greenwave_enabled

    def test_unset_mode_disables_even_with_url_and_token(self):
        """No mode selected → disabled, regardless of stored credentials."""
        env = _greenwave_env()
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
        assert not settings.greenwave_enabled

    def test_env_var_auth_mode_is_ignored(self):
        """GREENWAVE_AUTH_MODE is DB-only — the env var never selects a mode."""
        env = _greenwave_env(GREENWAVE_AUTH_MODE="bearer")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
        assert settings.greenwave_auth_mode is None
        assert not settings.greenwave_enabled

    def test_disabled_when_url_missing(self):
        env = _build_env(GREENWAVE_API_TOKEN="rdb-token")  # pragma: allowlist secret
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None, greenwave_auth_mode="bearer")
            assert not settings.greenwave_enabled

    def test_disabled_when_bearer_token_missing(self):
        env = _build_env(GREENWAVE_URL=RESULTSDB_URL)
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None, greenwave_auth_mode="bearer")
            assert not settings.greenwave_enabled

    def test_none_mode_needs_only_url(self):
        env = _build_env(GREENWAVE_URL="http://resultsdb.example.com")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None, greenwave_auth_mode="none")
        assert settings.greenwave_enabled

    def test_explicitly_disabled_overrides_config(self):
        settings = _make_settings(ENABLE_GREENWAVE="false")
        assert not settings.greenwave_enabled

    def test_explicitly_enabled_with_missing_url_warns(self):
        env = _build_env(ENABLE_GREENWAVE="true")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("rootcoz.config.logger") as mock_logger,
        ):
            settings = Settings(_env_file=None, greenwave_auth_mode="bearer")
            assert not settings.greenwave_enabled
            assert mock_logger.warning.called

    def test_explicitly_enabled_without_mode_warns(self):
        env = _greenwave_env(ENABLE_GREENWAVE="true")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("rootcoz.config.logger") as mock_logger,
        ):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled
            assert any(
                "no Greenwave auth method" in str(c.args[0])
                for c in mock_logger.warning.call_args_list
            )

    def test_blank_url_becomes_none(self):
        settings = _make_settings(GREENWAVE_URL="   ")
        assert settings.greenwave_url is None
        assert not settings.greenwave_enabled

    @pytest.mark.parametrize(
        ("env_name", "bad_url"),
        [
            # Structural problems (userinfo, query, fragment, no hostname,
            # bad port) stay construction errors. Scheme problems do not —
            # the credentials-over-plain-http rule lives at the write path
            # and the auth build (see TestHttpSchemeRefusedAtBuild in
            # test_greenwave_auth.py), because an env URL + DB-only mode
            # must never make the merged config unvalidatable.
            (
                "GREENWAVE_URL",
                "https://user:pass@resultsdb.example.com",  # pragma: allowlist secret
            ),
            ("GREENWAVE_URL", "https://resultsdb.example.com?token=secret"),
            ("GREENWAVE_URL", "https://resultsdb.example.com#section"),
            ("GREENWAVE_URL", "not-a-url"),
        ],
    )
    def test_invalid_greenwave_url_rejected(self, env_name: str, bad_url: str):
        env = _greenwave_env(**{env_name: bad_url})
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match=env_name),
        ):
            Settings(_env_file=None)

    def test_http_url_scheme_mismatch_is_not_a_construction_error(self):
        """Plain http + a credential mode must not crash Settings.

        The mode is DB-only, so at startup the env-only Settings sees it
        unset: an env-configured http:// ResultsDB URL must not crash the
        process for a mode only the database can supply — and once the DB
        supplies that mode, get_settings() must keep merging instead of
        falling back to env-only (which would wipe every DB override).
        The credential is never sent over http: build_greenwave_auth
        refuses the combination (TestHttpSchemeRefusedAtBuild).
        """
        env = _greenwave_env(GREENWAVE_URL="http://resultsdb.example.com")
        with patch.dict(os.environ, env, clear=True):
            # Unset mode: exporter disabled, URL inert — construction succeeds.
            assert Settings(_env_file=None).greenwave_enabled is False
            # none mode: credential-less, so http stays legal.
            assert Settings(_env_file=None, greenwave_auth_mode="none")
            # bearer mode sends a token: still constructible — the refusal
            # happens at the auth build (TestHttpSchemeRefusedAtBuild).
            assert Settings(_env_file=None, greenwave_auth_mode="bearer")

    def test_get_settings_keeps_db_overrides_on_scheme_mismatch(self):
        """An env http URL + DB mode must not wipe the DB overrides.

        Regression: raising on the scheme/mode mismatch made the merged
        config unvalidatable, so get_settings() fell back to env-only and
        silently dropped EVERY database override (credentials, roles,
        allow lists) — a URL reset could not be the trigger for that.
        """
        from rootcoz.config import (
            clear_db_settings_cache,
            get_settings,
            update_db_settings_cache,
        )

        env = _greenwave_env(GREENWAVE_URL="http://resultsdb.example.com")
        with patch.dict(os.environ, env, clear=True):
            update_db_settings_cache(
                {"greenwave_auth_mode": "bearer", "greenwave_basic_user": "svc"}
            )
            try:
                settings = get_settings()
            finally:
                clear_db_settings_cache()
        assert settings.greenwave_auth_mode == "bearer"
        assert settings.greenwave_basic_user == "svc"
        assert settings.greenwave_url == "http://resultsdb.example.com"

    def test_waiver_url_scheme_is_not_a_construction_error(self):
        """An http waiver URL must not crash Settings either.

        Same reliability rule as the ResultsDB URL: the scheme check lives
        at the write path and at build_greenwave_waiver_auth (which refuses
        it in EVERY mode — a waiver lifts a gate, so it is never downgraded
        to plain HTTP). Construction only rejects structural problems.
        """
        env = _greenwave_env(
            GREENWAVE_URL="http://resultsdb.example.com",
            GREENWAVE_WAIVER_URL="http://waiverdb.example.com",
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None, greenwave_auth_mode="none")
        assert settings.greenwave_waiver_url == "http://waiverdb.example.com"

    def test_oidc_token_url_userinfo_is_a_construction_error(self):
        """Structural problems in the token URL still fail construction.

        The http-scheme case is NOT a construction error (same reliability
        rule as GREENWAVE_URL): it is refused at the auth build, before the
        client secret is POSTed anywhere.
        """
        env = _greenwave_env(
            GREENWAVE_AUTH_MODE="oidc",
            GREENWAVE_OIDC_TOKEN_URL="https://u:p@sso.example.com",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="GREENWAVE_OIDC_TOKEN_URL"),
        ):
            Settings(_env_file=None, greenwave_auth_mode="oidc")

    def test_invalid_greenwave_port_rejected(self):
        """Syntactically invalid ports fail at Settings construction."""
        for env_name in ("GREENWAVE_URL", "GREENWAVE_WAIVER_URL"):
            env = _greenwave_env(**{env_name: "https://results.example.com:bad"})
            with (
                patch.dict(os.environ, env, clear=True),
                pytest.raises(ValueError, match="invalid port"),
            ):
                Settings(_env_file=None)

    def test_auto_push_without_auto_review_warns(self):
        """AUTO_PUSH_EXPORTERS with auto-review disabled is inert, not fatal."""
        env = _greenwave_env(
            AUTO_PUSH_EXPORTERS="greenwave", ENABLE_AUTO_REVIEW="false"
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch("rootcoz.config.logger") as mock_logger,
        ):
            settings = Settings(_env_file=None)
        assert settings.auto_push_exporters == "greenwave"
        assert settings.enable_auto_review is False
        assert any(
            "auto-push will not run" in str(c.args[0])
            for c in mock_logger.warning.call_args_list
        )

    def test_auto_push_with_auto_review_accepted(self):
        """AUTO_PUSH_EXPORTERS with auto-review enabled (the default) is fine."""
        env = _greenwave_env(AUTO_PUSH_EXPORTERS="greenwave")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
        assert settings.auto_push_exporters == "greenwave"

    def test_valid_urls_pass_validation(self):
        settings = _make_settings(
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
        )
        assert settings.greenwave_url == RESULTSDB_URL
        assert settings.greenwave_waiver_url == WAIVERDB_URL

    def test_default_outcome_map_parsed(self):
        settings = _make_settings()
        assert settings.greenwave_outcome_map_parsed == {
            "PRODUCT BUG": "FAILED",
            "CODE ISSUE": "FAILED",
            "INFRASTRUCTURE": "INFO",
        }

    def test_default_waivable_set(self):
        settings = _make_settings()
        assert settings.greenwave_waivable_set == frozenset({"INFRASTRUCTURE"})


class TestGreenwaveOutcomeMapParsing:
    """parse_greenwave_outcome_map behaviour."""

    def test_empty_and_none(self):
        assert parse_greenwave_outcome_map(None) == {}
        assert parse_greenwave_outcome_map("") == {}
        assert parse_greenwave_outcome_map("  ") == {}

    def test_case_insensitive_entries(self):
        assert parse_greenwave_outcome_map(" product bug : failed ") == {
            "PRODUCT BUG": "FAILED"
        }

    def test_multiple_entries(self):
        parsed = parse_greenwave_outcome_map(
            "PRODUCT BUG:FAILED, CODE ISSUE:INFO, INFRASTRUCTURE:PASSED"
        )
        assert parsed == {
            "PRODUCT BUG": "FAILED",
            "CODE ISSUE": "INFO",
            "INFRASTRUCTURE": "PASSED",
        }

    def test_malformed_entry_raises(self):
        with pytest.raises(ValueError, match="expected CLASS:OUTCOME"):
            parse_greenwave_outcome_map("PRODUCT BUG")

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValueError, match="Invalid ResultsDB outcome"):
            parse_greenwave_outcome_map("PRODUCT BUG:WOBBLE")

    def test_invalid_outcome_map_fails_settings_startup(self):
        with pytest.raises(ValidationError):
            _make_settings(GREENWAVE_OUTCOME_MAP="PRODUCT BUG:WOBBLE")


class TestGreenwaveWaivableParsing:
    """parse_greenwave_waivable_classifications behaviour."""

    def test_empty_and_none(self):
        assert parse_greenwave_waivable_classifications(None) == frozenset()
        assert parse_greenwave_waivable_classifications("") == frozenset()

    def test_uppercased_and_trimmed(self):
        parsed = parse_greenwave_waivable_classifications(
            "infrastructure, product bug ,"
        )
        assert parsed == frozenset({"INFRASTRUCTURE", "PRODUCT BUG"})

    def test_unrecognized_warns_only_when_enabled(self):
        with patch("rootcoz.config.logger") as mock_logger:
            parsed = parse_greenwave_waivable_classifications(
                "MYSTERY", warn_unrecognized=True
            )
            assert parsed == frozenset({"MYSTERY"})
            assert mock_logger.warning.called

    def test_unrecognized_silent_by_default(self):
        with patch("rootcoz.config.logger") as mock_logger:
            parse_greenwave_waivable_classifications("MYSTERY")
            assert not mock_logger.warning.called


class TestGreenwaveHelpers:
    """Pure helper functions in greenwave_exporter."""

    def test_testcase_name_format(self):
        assert _testcase_name("my-job", "test_a") == "rootcoz.my-job.test_a"

    def test_testcase_name_strips_control_chars(self):
        assert _testcase_name("my\x00-job", "te\x1fst\x7f") == "rootcoz.my-job.test"

    def test_testcase_name_strips_unicode_invisible_chars(self):
        assert _testcase_name("my\u200b-job", "te\ufeffst") == "rootcoz.my-job.test"

    def test_normalize_identifier_strips_invisible_and_whitespace(self):
        from rootcoz.utils import normalize_identifier

        assert normalize_identifier("  pkg-1.0  ") == "pkg-1.0"
        assert normalize_identifier("\u200bpkg-1.0\u00ad") == "pkg-1.0"
        assert normalize_identifier("\u200b\u200b") is None
        assert normalize_identifier("   ") is None
        assert normalize_identifier("") is None
        assert normalize_identifier(None) is None

    def test_sanitize_error_preserves_long_messages(self):
        assert _sanitize_error(RuntimeError("x" * 500)) == "x" * 500

    def test_sanitize_error_redacts_url_userinfo(self):
        message = "https://user:pass@rdb.example.com/api"  # pragma: allowlist secret
        result = _sanitize_error(RuntimeError(message))
        assert "user:pass" not in result
        assert "https://rdb.example.com/api" in result

    def test_sanitize_error_strips_control_chars(self):
        assert _sanitize_error(RuntimeError("bo\x00om")) == "boom"

    def test_sanitize_error_falls_back_to_class_name(self):
        assert _sanitize_error(RuntimeError()) == "RuntimeError"

    def test_positive_integer_response_id_valid(self):
        assert _positive_integer_response_id(_response({"id": 42})) == 42

    def test_positive_integer_response_id_rejects_bool(self):
        assert _positive_integer_response_id(_response({"id": True})) is None

    def test_positive_integer_response_id_rejects_non_positive(self):
        assert _positive_integer_response_id(_response({"id": 0})) is None
        assert _positive_integer_response_id(_response({"id": -3})) is None

    def test_positive_integer_response_id_rejects_non_int(self):
        assert _positive_integer_response_id(_response({"id": "42"})) is None
        assert _positive_integer_response_id(_response({})) is None

    def test_positive_integer_response_id_handles_bad_json(self):
        resp = MagicMock(spec=httpx.Response)
        resp.json.side_effect = ValueError("not json")
        assert _positive_integer_response_id(resp) is None

    def test_uuid_response_id_normalizes(self):
        raw = "550E8400-E29B-41D4-A716-446655440000"
        assert _uuid_response_id(_response({"uuid": raw})) == str(uuid_module.UUID(raw))

    def test_uuid_response_id_rejects_invalid(self):
        assert _uuid_response_id(_response({"uuid": "not-a-uuid"})) is None
        assert _uuid_response_id(_response({})) is None
        assert _uuid_response_id(_response({"uuid": 123})) is None

    def test_build_waiver_comment_with_user_comment(self):
        comment = _build_waiver_comment(
            "INFRASTRUCTURE",
            "pod OOM",
            "operator",
            "reviewer",
            user_comment="flake tolerance",
        )
        assert comment.startswith("operator: flake tolerance")
        assert "rootcoz: INFRASTRUCTURE" in comment
        assert "pod OOM" in comment
        assert comment.endswith(", reviewed by reviewer")

    def test_build_waiver_comment_without_user_comment(self):
        comment = _build_waiver_comment("INFRASTRUCTURE", "", "operator", "reviewer")
        assert (
            comment
            == "operator: Waived by rootcoz: INFRASTRUCTURE, reviewed by reviewer"
        )

    def test_build_waiver_comment_without_pushed_by_or_reviewer(self):
        comment = _build_waiver_comment("INFRASTRUCTURE", "det", "", "")
        assert comment == "Waived by rootcoz: INFRASTRUCTURE — det"


class TestGreenwaveExporterConstructor:
    """GreenwaveExporter prerequisite validation."""

    def test_constructs_without_waivers(self):
        exporter = GreenwaveExporter(_make_settings())
        assert exporter.name == "greenwave"
        assert exporter.display_name == "Greenwave"
        assert exporter.is_enabled
        assert exporter._url == RESULTSDB_URL

    def test_name_and_display_name_satisfy_abc(self):
        exporter = GreenwaveExporter(_make_settings())
        assert isinstance(exporter.name, str)
        assert isinstance(exporter.display_name, str)

    def test_missing_waiver_settings_lists_all_missing(self):
        """Single-source-of-truth helper reports every missing setting."""
        settings = _make_settings(GREENWAVE_PUSH_WAIVERS="true")
        assert GreenwaveExporter.missing_waiver_settings(settings) == [
            "GREENWAVE_WAIVER_URL",
            "GREENWAVE_WAIVER_TOKEN",
            "GREENWAVE_PRODUCT_VERSION",
        ]

    def test_missing_waiver_settings_empty_when_complete(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        assert GreenwaveExporter.missing_waiver_settings(settings) == []

    def test_non_bearer_mode_does_not_require_a_waiver_token(self):
        """The selected mode authenticates the waiver write, so no token.

        Only bearer mode splits WaiverDB onto its own token; requiring one in
        kerberos/mtls/oidc/basic/none mode would force a credential the
        deployment deliberately does not use.
        """
        settings = _make_settings(
            "basic",
            GREENWAVE_BASIC_USER="greenwave-user",
            GREENWAVE_BASIC_PASSWORD="greenwave-pass",  # pragma: allowlist secret
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        assert GreenwaveExporter.missing_waiver_settings(settings) == []
        GreenwaveExporter(settings)  # constructs without a waiver token

    def test_waivers_require_waiver_url(self):
        env = _greenwave_env(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_WAIVER_URL"),
        ):
            GreenwaveExporter(Settings(_env_file=None, greenwave_auth_mode="bearer"))

    def test_waivers_require_waiver_token(self):
        env = _greenwave_env(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_WAIVER_TOKEN"),
        ):
            GreenwaveExporter(Settings(_env_file=None, greenwave_auth_mode="bearer"))

    def test_waivers_require_product_version(self):
        env = _greenwave_env(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_PRODUCT_VERSION"),
        ):
            GreenwaveExporter(Settings(_env_file=None, greenwave_auth_mode="bearer"))

    def test_waivers_with_full_config(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        assert exporter._push_waivers


class TestGreenwaveExporterPush:
    """push() flow with mocked HTTP posts."""

    def _make_exporter(self, **env_overrides: str) -> GreenwaveExporter:
        return GreenwaveExporter(_make_settings(**env_overrides))

    def _mock_post(self, exporter: GreenwaveExporter, responses: list) -> AsyncMock:
        post = AsyncMock(side_effect=responses)
        exporter._post = post
        return post

    def test_push_no_failures(self):
        exporter = self._make_exporter()
        result = _run_push(exporter, _make_context([]))
        assert result.success is False
        assert result.details["errors"] == ["no failures"]
        assert result.details["pushed"] == 0

    def test_push_results_flow(self):
        exporter = self._make_exporter()
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
                _response({"id": 102}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [
                    _failure("test_a", classification="PRODUCT BUG", details="det-a"),
                    _failure("test_b", classification="INFRASTRUCTURE"),
                ]
            ),
        )

        assert result.success is True
        assert result.details["pushed"] == 2
        assert result.details["skipped"] == 0
        assert result.details["errors"] == []
        assert result.details["details"]["resultsdb_ids"] == [101, 102]
        assert (
            result.details["details"]["group_uuid"]
            == "550e8400-e29b-41d4-a716-446655440000"
        )

        # group request
        group_call = post.call_args_list[0]
        assert group_call.args[1] == f"{RESULTSDB_URL}/groups"
        assert group_call.kwargs["auth"] is exporter._auth.auth
        assert group_call.kwargs["json"]["description"] == "rootcoz analysis job-123"

        # result requests: testcase names, outcome mapping, subject fallback
        first_result = post.call_args_list[1].kwargs["json"]
        assert first_result["testcase"]["name"] == "rootcoz.my-job.test_a"
        assert first_result["outcome"] == "FAILED"
        assert first_result["data"]["item"] == ["pkg-1.0-1.el10"]  # job_name fallback
        assert first_result["groups"] == ["550e8400-e29b-41d4-a716-446655440000"]
        assert "rootcoz classification: PRODUCT BUG" in first_result["note"]
        assert "det-a" in first_result["note"]

        second_result = post.call_args_list[2].kwargs["json"]
        assert second_result["outcome"] == "INFO"
        assert second_result["data"]["type"] == ["koji_build"]
        assert second_result["data"]["rootcoz_job_id"] == ["job-123"]

    def test_push_skips_unclassified_and_unmapped(self):
        """Nothing exportable: no HTTP writes at all — no empty group."""
        exporter = self._make_exporter()
        post = self._mock_post(
            exporter, [_response({"uuid": "550e8400-e29b-41d4-a716-446655440000"})]
        )
        result = _run_push(
            exporter,
            _make_context(
                [
                    _failure("test_a", classification=""),
                    _failure("test_b", classification="MYSTERY"),
                ]
            ),
        )
        assert result.success is False
        assert result.details["pushed"] == 0
        assert result.details["skipped"] == 2
        assert result.details["errors"] == []
        # no group request — an all-unmapped analysis must not leave an
        # empty ResultsDB group behind
        assert post.call_count == 0
        assert result.details["details"]["group_uuid"] is None
        assert "no matching classifications" in result.message

    def test_push_mixed_classifications_still_creates_group(self):
        """At least one exportable failure: the group IS created."""
        exporter = self._make_exporter()
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [
                    _failure("test_a", classification=""),
                    _failure("test_b", classification="PRODUCT BUG"),
                ]
            ),
        )
        assert result.success is True
        assert result.details["pushed"] == 1
        assert result.details["skipped"] == 1
        assert post.call_count == 2  # group + one result

    def test_push_unexpected_task_error_isolated(self):
        """An unexpected exception in one task never aborts the batch."""
        exporter = self._make_exporter()
        group_uuid = "550e8400-e29b-41d4-a716-446655440000"
        real_inner = exporter._push_failure_inner

        async def inner_for_a(
            client, semaphore, failure, classification, outcome_for_class, outcome, **kw
        ):
            # Raise OUTSIDE the HTTP handlers so only the per-task
            # catch-all in _push_failure() can contain it.
            if failure.test_name == "test_a":
                raise RuntimeError("unexpected boom")
            await real_inner(
                client,
                semaphore,
                failure,
                classification,
                outcome_for_class,
                outcome,
                **kw,
            )

        exporter._push_failure_inner = inner_for_a
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": group_uuid}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [
                    _failure("test_a", "PRODUCT BUG"),
                    _failure("test_b", "PRODUCT BUG"),
                ]
            ),
        )
        # test_b still pushed despite test_a's unexpected failure
        assert result.success is True
        assert result.details["pushed"] == 1
        assert result.details["details"]["resultsdb_ids"] == [101]
        assert any(
            "failure[0]" in e and "unexpected boom" in e
            for e in result.details["errors"]
        )
        assert post.call_count == 2  # group + test_b's result

    def test_push_non_dict_failure_entry(self):
        exporter = self._make_exporter()
        self._mock_post(
            exporter,
            [_response({"uuid": "550e8400-e29b-41d4-a716-446655440000"})],
        )
        result = _run_push(exporter, _make_context(["not-a-dict"]))
        assert result.success is False
        assert result.details["errors"] == ["failure[0]: entry is not an object"]

    def test_push_invalid_failure_entry(self):
        exporter = self._make_exporter()
        self._mock_post(
            exporter,
            [_response({"uuid": "550e8400-e29b-41d4-a716-446655440000"})],
        )
        result = _run_push(exporter, _make_context([{"unexpected": "shape"}]))
        assert result.success is False
        # The reason is now the pydantic field summary (input values are never
        # echoed), which is what an operator can act on.
        expected_reason = (
            "failure[0]: test_name: Field required; error: Field required; "
            "analysis: Field required"
        )
        assert result.details["errors"] == [expected_reason]

    def test_push_waiver_requires_subject_identifier(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        exporter._post = AsyncMock()
        with pytest.raises(ExporterPrerequisiteError, match="subject_identifier"):
            _run_push(
                exporter,
                _make_context(
                    [_failure("test_a", "INFRASTRUCTURE")], subject_identifier=None
                ),
            )

    def test_push_waiver_rejects_invisible_only_subject_identifier(self):
        """Unicode invisible-only input cannot bypass the subject guard."""
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        exporter._post = AsyncMock()
        with pytest.raises(ExporterPrerequisiteError, match="subject_identifier"):
            _run_push(
                exporter,
                _make_context(
                    [_failure("test_a", "INFRASTRUCTURE")],
                    subject_identifier="\u200b\u200b\ufeff",
                ),
            )

    def test_push_subject_identifier_invisible_chars_stripped(self):
        """Invisible characters are stripped from a usable identifier."""
        exporter = self._make_exporter()
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "PRODUCT BUG")],
                subject_identifier="\u200bpkg-1.0-1.el10\u00ad",
            ),
        )
        assert result.success is True
        assert post.call_args_list[1].kwargs["json"]["data"]["item"] == [
            "pkg-1.0-1.el10"
        ]

    def test_push_many_failures_bounded_concurrency(self):
        """Concurrent pushes are semaphore-bounded; identifiers keep input order."""
        from rootcoz.exporters.greenwave_exporter import _MAX_CONCURRENT_WRITES

        exporter = self._make_exporter()
        group_uuid = "550e8400-e29b-41d4-a716-446655440000"
        failures = [_failure(f"test_{i}", "PRODUCT BUG") for i in range(12)]

        state = {"active": 0, "max_active": 0}

        async def tracked_post(
            client, url, *, auth=None, headers=None, json=None, label=""
        ):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                if url.endswith("/groups"):
                    await asyncio.sleep(0)
                    return _response({"uuid": group_uuid})
                index = int(json["testcase"]["name"].rsplit("_", 1)[1])
                # Reverse completion order: test_11 resolves first, test_0 last
                await asyncio.sleep((12 - index) * 0.01)
                return _response({"id": 100 + index})
            finally:
                state["active"] -= 1

        exporter._post = tracked_post
        result = _run_push(exporter, _make_context(failures))
        assert result.success is True
        assert result.details["pushed"] == 12
        # Identifiers aggregate in input order despite reversed completion
        assert result.details["details"]["resultsdb_ids"] == [
            100 + i for i in range(12)
        ]
        # 12 pushes over a semaphore of _MAX_CONCURRENT_WRITES: all 8 slots
        # fill before the fastest (0.01s) request can release
        assert state["max_active"] == _MAX_CONCURRENT_WRITES

    def test_results_only_push_also_requires_subject_identifier(self):
        """A job name is not a subject: results are gating writes too."""
        from rootcoz.exporters import ExporterPrerequisiteError

        exporter = GreenwaveExporter(_make_settings(GREENWAVE_PUSH_WAIVERS="false"))
        exporter._post = AsyncMock()
        with pytest.raises(ExporterPrerequisiteError):
            _run_push(
                exporter,
                _make_context(
                    [_failure("test_a", "PRODUCT BUG")], subject_identifier=None
                ),
            )
        exporter._post.assert_not_called()

    def test_waiver_push_uses_the_selected_mode_not_a_bearer_token(self):
        """A non-bearer mode authenticates ResultsDB *and* WaiverDB.

        Regression: the waiver write used to send a static bearer token
        regardless of the mode (and nothing at all when that token was
        unset), so selecting kerberos/mtls/oidc had no effect on waivers —
        the one write that actually lifts a gate.
        """
        settings = _make_settings(
            "basic",
            GREENWAVE_BASIC_USER="greenwave-user",
            GREENWAVE_BASIC_PASSWORD="greenwave-pass",  # pragma: allowlist secret
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
                _response({"id": 201}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                waiver_comment="known flake",
                reviewed_by={"test_a": "reviewer"},
            ),
        )
        assert result.success is True
        assert result.details["waived"] == 1

        waiver_call = post.call_args_list[2]
        assert waiver_call.args[1] == f"{WAIVERDB_URL}/waivers/"
        rendered = _rendered_authorization(waiver_call.kwargs["auth"])
        assert rendered is not None
        assert rendered.startswith("Basic ")
        assert "wdb-token" not in rendered

    def test_auto_reviewed_failure_is_never_waived(self):
        """rootcoz auto-review must never produce a waiver — no escape hatch.

        The results row is still written (it records the failure); only the
        gating write is withheld, because lifting a gate needs a human
        decision, not an AI reviewing its own analysis.
        """
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                waiver_comment="known flake",
                reviewed_by={"test_a": AI_SYSTEM_USERNAME},
            ),
        )
        assert result.success is True
        assert result.details["pushed"] == 1
        assert result.details["waived"] == 0
        urls = [call.args[1] for call in post.call_args_list]
        assert not any(url.endswith("/waivers/") for url in urls), urls

    def test_unavailable_reviewer_lookup_refuses_a_waiver_push(self):
        """An unreadable review table must not silently drop waivers.

        Empty ``reviewed_by`` looks identical whether nobody reviewed the
        failure or the lookup failed. With waivers enabled the difference
        matters: pushing results and omitting the requested waivers would
        report success for a gate that was never lifted.
        """
        from rootcoz.exporters import ReviewersUnavailable

        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(exporter, [])
        # 503-mapped, not ExporterPrerequisiteError: the server could not read
        # its own data, the caller did nothing wrong.
        with pytest.raises(ReviewersUnavailable, match="reviewer lookup failed"):
            _run_push(
                exporter,
                replace(
                    _make_context(
                        [_failure("test_a", "INFRASTRUCTURE")],
                        subject_identifier="pkg-1.0-1.el10",
                    ),
                    reviews_unavailable=True,
                ),
            )
        post.assert_not_called()

    def test_outage_does_not_block_a_push_with_nothing_waivable(self):
        """No waivable failure ⇒ nothing to lose from the outage ⇒ push it.

        Refusing here would only withhold the informational ResultsDB rows.
        """
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
            # PRODUCT BUG is classified FAILED and is not waivable here.
            GREENWAVE_WAIVABLE_CLASSIFICATIONS="INFRASTRUCTURE",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        context = replace(
            _make_context(
                [_failure("test_a", "PRODUCT BUG")],
                subject_identifier="pkg-1.0-1.el10",
            ),
            reviews_unavailable=True,
        )
        result = _run_push(exporter, context)
        assert result.success is True
        assert result.details["pushed"] == 1
        assert not any(
            call.args[1].endswith("/waivers/") for call in post.call_args_list
        )

    def test_outage_does_not_block_mapped_failures_behind_unmapped_waivable(
        self,
    ):
        """A waivable-but-UNMAPPED classification can never be waived.

        The push loop skips it (no outcome mapping) before any waiver is
        considered, so it must not make the outage fatal for the mapped,
        non-waivable failures in the same push.
        """
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
            GREENWAVE_WAIVABLE_CLASSIFICATIONS="INFRASTRUCTURE",
            # Only PRODUCT BUG is mapped; INFRASTRUCTURE is deliberately absent.
            GREENWAVE_OUTCOME_MAP="PRODUCT BUG:FAILED",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        context = replace(
            _make_context(
                [
                    _failure("test_infra", "INFRASTRUCTURE"),
                    _failure("test_bug", "PRODUCT BUG"),
                ],
                subject_identifier="pkg-1.0-1.el10",
            ),
            reviews_unavailable=True,
        )
        result = _run_push(exporter, context)
        assert result.success is True
        assert result.details["pushed"] == 1  # the mapped PRODUCT BUG failure
        assert not any(
            call.args[1].endswith("/waivers/") for call in post.call_args_list
        )

    def test_unavailable_reviewer_lookup_still_pushes_results_only(self):
        """Results are informational: they do not need reviewer identity."""
        exporter = GreenwaveExporter(_make_settings())
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        context = replace(
            _make_context(
                [_failure("test_a", "PRODUCT BUG")],
                subject_identifier="pkg-1.0-1.el10",
            ),
            reviews_unavailable=True,
        )
        result = _run_push(exporter, context)
        assert result.success is True
        assert result.details["pushed"] == 1
        # Two POSTs: the group and the result row. No waiver write.
        assert len(post.call_args_list) == 2
        assert not any(
            call.args[1].endswith("/waivers/") for call in post.call_args_list
        )

    def test_push_waiver_submitted_for_human_review(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
                _response({"id": 201}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                waiver_comment="known flake",
                reviewed_by={"test_a": "reviewer"},
            ),
        )
        assert result.success is True
        assert result.details["waived"] == 1
        assert result.details["details"]["waiver_ids"] == [201]

        waiver_call = post.call_args_list[2]
        assert waiver_call.args[1] == f"{WAIVERDB_URL}/waivers/"
        # The token is applied by the auth strategy, not a raw header, so
        # assert the header it actually renders.
        assert _rendered_authorization(waiver_call.kwargs["auth"]) == "Bearer wdb-token"
        payload = waiver_call.kwargs["json"]
        assert payload["subject_identifier"] == "pkg-1.0-1.el10"
        assert payload["testcase"] == "rootcoz.my-job.test_a"
        assert payload["waived"] is True
        assert payload["product_version"] == "el10"
        assert "known flake" in payload["comment"]
        assert "reviewed by reviewer" in payload["comment"]

    def test_push_waiver_skipped_for_non_waivable_classification(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "PRODUCT BUG")],
                subject_identifier="pkg-1.0-1.el10",
                reviewed_by={"test_a": "reviewer"},
            ),
        )
        assert result.details["waived"] == 0
        assert post.call_count == 2  # group + result only, no waiver

    def test_push_waiver_skipped_without_human_reviewer(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
            ),
        )
        assert result.details["waived"] == 0
        assert post.call_count == 2

    def test_push_waiver_skipped_for_ai_reviewer_by_default(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                reviewed_by={"test_a": AI_SYSTEM_USERNAME},
            ),
        )
        assert result.details["waived"] == 0
        assert post.call_count == 2

    def test_ai_waiver_switch_is_gone_not_merely_defaulted(self):
        """The old GREENWAVE_ALLOW_AI_WAIVERS escape hatch no longer exists.

        A deployment that still sets it must not be able to waive
        auto-reviews: the setting is not a field any more (``extra="ignore"``
        drops the env var), so the behaviour cannot be re-enabled by config.
        """
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
            GREENWAVE_ALLOW_AI_WAIVERS="true",
        )
        assert not hasattr(settings, "greenwave_allow_ai_waivers")
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                reviewed_by={"test_a": AI_SYSTEM_USERNAME},
            ),
        )
        assert result.details["waived"] == 0
        assert not any(
            call.args[1].endswith("/waivers/") for call in post.call_args_list
        )

    def test_push_result_error_isolated(self):
        exporter = self._make_exporter()
        self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response(
                    error=httpx.HTTPStatusError(
                        "500", request=MagicMock(), response=MagicMock()
                    )
                ),
                _response({"id": 102}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [
                    _failure("test_a", "PRODUCT BUG"),
                    _failure("test_b", "PRODUCT BUG"),
                ]
            ),
        )
        assert result.success is True
        assert result.details["pushed"] == 1
        assert len(result.details["errors"]) == 1
        assert "rootcoz.my-job.test_a" in result.details["errors"][0]
        assert result.details["details"]["resultsdb_ids"] == [102]

    def test_push_waiver_skipped_when_result_failed(self):
        """No waiver is submitted when the failure's ResultsDB push failed."""
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        exporter = GreenwaveExporter(settings)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response(
                    error=httpx.HTTPStatusError(
                        "500", request=MagicMock(), response=MagicMock()
                    )
                ),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                reviewed_by={"test_a": "reviewer"},
            ),
        )
        assert result.details["pushed"] == 0
        assert result.details["waived"] == 0
        assert post.call_count == 2  # group + failed result, no waiver

    def test_push_group_failure_aborts_push(self):
        """Group-creation failure is fatal: no ungrouped result writes."""
        exporter = self._make_exporter()
        post = self._mock_post(
            exporter,
            [
                _response(
                    error=httpx.HTTPStatusError(
                        "503", request=MagicMock(), response=MagicMock()
                    )
                ),
            ],
        )
        result = _run_push(exporter, _make_context([_failure("test_a", "PRODUCT BUG")]))
        assert result.success is False
        assert result.details["pushed"] == 0
        assert result.details["details"]["group_uuid"] is None
        assert len(result.details["errors"]) == 1
        assert "group creation failed" in result.details["errors"][0]
        assert "no results were pushed" in result.message
        # The mocked result response was never consumed — no result POST
        assert post.call_count == 1

    def test_push_group_failure_carries_prepass_accounting(self):
        """Group failure keeps the pre-pass skipped count and errors."""
        exporter = self._make_exporter()
        post = self._mock_post(
            exporter,
            [
                _response(
                    error=httpx.HTTPStatusError(
                        "503", request=MagicMock(), response=MagicMock()
                    )
                ),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [
                    _failure("test_a", "PRODUCT BUG"),
                    _failure("test_b", classification="MYSTERY"),
                    "not-a-dict",
                ]
            ),
        )
        assert result.success is False
        # the unmapped failure is still counted as skipped
        assert result.details["skipped"] == 1
        # both the pre-pass validation error and the group error are kept
        assert result.details["errors"] == [
            "failure[2]: entry is not an object",
            "group creation failed: 503",
        ]
        assert post.call_count == 1

    def test_push_result_without_usable_id_records_error(self):
        exporter = self._make_exporter()
        self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": "not-an-int"}),
            ],
        )
        result = _run_push(exporter, _make_context([_failure("test_a", "PRODUCT BUG")]))
        assert result.success is True
        assert result.details["pushed"] == 1
        assert result.details["details"]["resultsdb_ids"] == []
        assert any("no usable 'id'" in e for e in result.details["errors"])

    def test_push_group_without_uuid_records_error(self):
        """Missing 'uuid' in the group response is a fatal group failure."""
        exporter = self._make_exporter()
        post = self._mock_post(exporter, [_response({"unexpected": "shape"})])
        result = _run_push(exporter, _make_context([_failure("test_a", "PRODUCT BUG")]))
        assert result.success is False
        assert result.details["pushed"] == 0
        assert any("group creation failed" in e for e in result.details["errors"])
        # No result writes were attempted after the unusable group response
        assert post.call_count == 1


class TestGreenwaveEffectiveClassificationOverrides:
    """Human overrides in ExportContext.effective_classifications win.

    The override table (test_classifications) is authoritative; the
    result-JSON classification copy is patched best-effort.  The resolved
    classification must drive the outcome mapping, the waivable gate,
    and the waiver comment together.
    """

    _FULL_WAIVER_ENV: ClassVar[dict[str, str]] = {
        "GREENWAVE_PUSH_WAIVERS": "true",
        "GREENWAVE_WAIVER_URL": WAIVERDB_URL,
        "GREENWAVE_WAIVER_TOKEN": "wdb-token",  # pragma: allowlist secret
        "GREENWAVE_PRODUCT_VERSION": "el10",
    }

    def _make_exporter(self, **env_overrides: str) -> GreenwaveExporter:
        return GreenwaveExporter(_make_settings(**env_overrides))

    def _mock_post(self, exporter: GreenwaveExporter, responses: list) -> AsyncMock:
        post = AsyncMock(side_effect=responses)
        exporter._post = post
        return post

    def test_override_drives_outcome_and_blocks_waiver(self):
        """INFRASTRUCTURE overridden to PRODUCT BUG: FAILED outcome, no waiver.

        The stale JSON says INFRASTRUCTURE (INFO outcome, waivable) — the
        override must win for both the outcome and the waiver gate.
        """
        exporter = self._make_exporter(**self._FULL_WAIVER_ENV)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                reviewed_by={"test_a": "reviewer"},
                effective_classifications={("test_a", "", 0): "PRODUCT BUG"},
            ),
        )
        assert result.success is True
        assert result.details["pushed"] == 1
        assert result.details["waived"] == 0
        assert post.call_count == 2  # group + result only, no waiver

        result_payload = post.call_args_list[1].kwargs["json"]
        assert result_payload["outcome"] == "FAILED"
        assert result_payload["data"]["rootcoz_classification"] == ["PRODUCT BUG"]
        assert "rootcoz classification: PRODUCT BUG" in result_payload["note"]

    def test_override_makes_failure_waivable(self):
        """PRODUCT BUG overridden to INFRASTRUCTURE: waiver IS filed with it."""
        exporter = self._make_exporter(**self._FULL_WAIVER_ENV)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
                _response({"id": 201}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "PRODUCT BUG")],
                subject_identifier="pkg-1.0-1.el10",
                reviewed_by={"test_a": "reviewer"},
                effective_classifications={("test_a", "", 0): "INFRASTRUCTURE"},
            ),
        )
        assert result.success is True
        assert result.details["waived"] == 1
        assert result.details["details"]["waiver_ids"] == [201]

        waiver_payload = post.call_args_list[2].kwargs["json"]
        assert "rootcoz: INFRASTRUCTURE" in waiver_payload["comment"]

    def test_no_override_uses_stored_json(self):
        """Empty overrides: behaviour identical to the pre-override flow."""
        exporter = self._make_exporter(**self._FULL_WAIVER_ENV)
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
                _response({"id": 201}),
            ],
        )
        result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                subject_identifier="pkg-1.0-1.el10",
                reviewed_by={"test_a": "reviewer"},
            ),
        )
        assert result.details["pushed"] == 1
        assert result.details["waived"] == 1
        result_payload = post.call_args_list[1].kwargs["json"]
        assert result_payload["outcome"] == "INFO"
        waiver_payload = post.call_args_list[2].kwargs["json"]
        assert "rootcoz: INFRASTRUCTURE" in waiver_payload["comment"]

    def test_child_scope_override_wins_over_root(self):
        """A child-scoped failure uses the child's override, not the root's."""
        overrides = {
            ("test_a", "", 0): "INFRASTRUCTURE",
            ("test_a", "child-a", 7): "PRODUCT BUG",
        }
        exporter = self._make_exporter()
        post = self._mock_post(
            exporter,
            [
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 101}),
                _response({"uuid": "550e8400-e29b-41d4-a716-446655440000"}),
                _response({"id": 102}),
            ],
        )
        # Child-scoped push: the child override applies.
        child_result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                effective_classifications=overrides,
                child_job_name="child-a",
                child_build_number=7,
            ),
        )
        assert child_result.details["pushed"] == 1
        child_payload = post.call_args_list[1].kwargs["json"]
        assert child_payload["outcome"] == "FAILED"  # PRODUCT BUG override
        assert child_payload["data"]["rootcoz_classification"] == ["PRODUCT BUG"]

        # Root-scoped push with the same override table: the root entry
        # applies — the scope keys must not collide.
        root_result = _run_push(
            exporter,
            _make_context(
                [_failure("test_a", "INFRASTRUCTURE")],
                effective_classifications=overrides,
            ),
        )
        assert root_result.details["pushed"] == 1
        root_payload = post.call_args_list[3].kwargs["json"]
        assert root_payload["outcome"] == "INFO"  # INFRASTRUCTURE override


class TestBuildExportContextEffectiveClassifications:
    """_build_export_context populates effective_classifications."""

    _RESULT_DATA: ClassVar[dict] = {
        "job_name": "j",
        "jenkins_url": "https://jenkins.example.com/job/j/1/",
        "failures": [{"test_name": "t1"}],
    }

    def _build(
        self,
        *,
        overrides_mock: AsyncMock,
        child_job_name: str | None = None,
        child_build_number: int | None = None,
        result_data: dict | None = None,
    ):
        from rootcoz.main import _build_export_context

        with (
            patch(
                "rootcoz.main._extract_base_url",
                return_value="https://rc.example.com",
            ),
            patch(
                "rootcoz.main.get_history_classification",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "rootcoz.storage.get_tracked_in_for_scope",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "rootcoz.storage.get_reviews_for_job",
                new_callable=AsyncMock,
                return_value={},
            ),
            overrides_mock,
        ):
            return asyncio.run(
                _build_export_context(
                    job_id="job-1",
                    result_data=result_data or self._RESULT_DATA,
                    settings=MagicMock(),
                    child_job_name=child_job_name,
                    child_build_number=child_build_number,
                    fetch_history=False,
                    fetch_tracked_in_links=False,
                )
            )

    def test_populates_overrides_from_storage(self):
        overrides = {("t1", "", 0): "PRODUCT BUG"}
        mock = patch(
            "rootcoz.storage.get_all_effective_classifications",
            new_callable=AsyncMock,
            return_value=overrides,
        )
        ctx = self._build(overrides_mock=mock)
        assert ctx.effective_classifications == overrides

    def test_child_scope_passes_job_scoped_table_through(self):
        """The whole job-scoped table is carried; exporters resolve per scope."""
        overrides = {("t1", "child-a", 7): "PRODUCT BUG"}
        mock = patch(
            "rootcoz.storage.get_all_effective_classifications",
            new_callable=AsyncMock,
            return_value=overrides,
        )
        result_data = {
            **self._RESULT_DATA,
            "failures": [],
            "child_job_analyses": [
                {
                    "job_name": "child-a",
                    "build_number": 7,
                    "failures": [{"test_name": "t1"}],
                }
            ],
        }
        ctx = self._build(
            overrides_mock=mock,
            child_job_name="child-a",
            child_build_number=7,
            result_data=result_data,
        )
        assert ctx.effective_classifications == overrides

    def test_storage_failure_refuses_instead_of_using_stale_json(self):
        """DB down: the gating push is refused, never pushed from stale JSON.

        The result JSON is only a best-effort mirror of the override table, so
        falling back to it could publish an outcome a reviewer overruled or
        file a waiver for the classification they rejected.
        """
        from rootcoz.main import EffectiveClassificationsUnavailable

        mock = patch(
            "rootcoz.storage.get_all_effective_classifications",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        )
        with pytest.raises(EffectiveClassificationsUnavailable):
            self._build(overrides_mock=mock)


class TestGreenwaveExporterFactory:
    """_create_exporter dispatch for the greenwave plugin."""

    def test_available_exporters_false_when_waiver_prereqs_missing(self):
        """Fix: /api/exporters must not advertise greenwave as enabled when
        waiver prereqs are incomplete — mirrors GreenwaveExporter readiness."""
        from rootcoz.main import _available_exporters

        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            # GREENWAVE_WAIVER_URL / _TOKEN / _PRODUCT_VERSION all missing
        )
        entries = _available_exporters(settings)
        gw = next(e for e in entries if e.name == "greenwave")
        assert gw.enabled is False

    def test_available_exporters_true_with_full_config(self):
        from rootcoz.main import _available_exporters

        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
        )
        entries = _available_exporters(settings)
        gw = next(e for e in entries if e.name == "greenwave")
        assert gw.enabled is True

    def test_create_greenwave_exporter(self):
        from rootcoz.exporters.greenwave_exporter import GreenwaveExporter as GW
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = True
        settings.greenwave_url = RESULTSDB_URL
        settings.greenwave_auth_mode = "bearer"
        settings.greenwave_api_token.get_secret_value.return_value = "rdb-token"
        settings.greenwave_oidc_token_url = None
        settings.greenwave_oidc_client_id = None
        settings.greenwave_oidc_client_secret = None
        settings.greenwave_keytab_path = None
        settings.greenwave_keytab_principal = None
        settings.greenwave_client_cert = None
        settings.greenwave_client_key = None
        settings.greenwave_basic_user = None
        settings.greenwave_basic_password = None
        settings.greenwave_waiver_url = None
        settings.greenwave_waiver_token = None
        settings.greenwave_push_waivers = False
        settings.greenwave_waivable_set = frozenset()
        settings.greenwave_outcome_map_parsed = {}
        settings.greenwave_subject_type = "koji_build"
        settings.greenwave_product_version = None
        settings.greenwave_verify_ssl = True

        exporter = _create_exporter("greenwave", settings)
        assert isinstance(exporter, GW)

    def test_create_greenwave_disabled_raises(self):
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = False
        with pytest.raises(ValueError, match="disabled"):
            _create_exporter("greenwave", settings)

    def test_create_greenwave_missing_url_raises(self):
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = True
        settings.greenwave_url = None
        with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_URL"):
            _create_exporter("greenwave", settings)

    def test_create_greenwave_missing_token_raises(self):
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = True
        settings.greenwave_url = RESULTSDB_URL
        settings.greenwave_auth_mode = "bearer"
        settings.greenwave_api_token = None
        with pytest.raises(ExporterPrerequisiteError, match="greenwave_api_token"):
            _create_exporter("greenwave", settings)

    def test_create_greenwave_http_url_with_credentials_fails_closed(self):
        """A push never sends a credential over plain http.

        The scheme rule cannot live in Settings construction (an env URL
        + DB-only mode must stay mergeable), so the exporter refuses to
        build instead — the push fails closed before any request.
        """
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = True
        settings.greenwave_url = "http://resultsdb.example.com"
        settings.greenwave_auth_mode = "bearer"
        settings.greenwave_api_token = "rdb-token"  # pragma: allowlist secret
        with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_URL"):
            _create_exporter("greenwave", settings)

    def test_exporter_classes_registry_contains_greenwave(self):
        from rootcoz.main import _EXPORTER_CLASSES

        assert "greenwave" in _EXPORTER_CLASSES

    def test_sensitive_settings_include_greenwave_tokens(self):
        from rootcoz.main import _SENSITIVE_SETTINGS

        assert "greenwave_api_token" in _SENSITIVE_SETTINGS
        assert "greenwave_waiver_token" in _SENSITIVE_SETTINGS
        assert "greenwave_oidc_client_secret" in _SENSITIVE_SETTINGS
        assert "greenwave_basic_password" in _SENSITIVE_SETTINGS


class TestGreenwaveExporterEndpoints:
    """API endpoint tests for the Greenwave exporter (generic push flow)."""

    @pytest.fixture
    def temp_db_path(self, tmp_path):
        return tmp_path / "test.db"

    @pytest.fixture
    def _init_db(self, temp_db_path):
        import asyncio

        from rootcoz import storage

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(storage.init_db())
            yield

    @staticmethod
    def _context():
        return ExportContext(
            job_id="job-1",
            job_name="test-job",
            build_number="1",
            jenkins_url="https://jenkins.example.com/job/test-job/1/",
            failures=[{"test_name": "t"}],
            report_url="https://rootcoz.example.com/results/job-1",
        )

    def test_list_exporters_includes_greenwave(self, _init_db, temp_db_path):
        """GET /api/exporters includes the greenwave entry (disabled by default)."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            response = client.get("/api/exporters")
            assert response.status_code == 200
            data = response.json()
            gw = next(e for e in data if e["name"] == "greenwave")
            assert gw["display_name"] == "Greenwave"
            assert gw["enabled"] is False

    def test_push_greenwave_disabled_returns_400(self, _init_db, temp_db_path):
        """POST /push/greenwave returns 400 when the exporter is not enabled."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                # The unreviewed-push gate is main's; these cases are about the
                # push plumbing (disabled/prerequisite/query params), so keep it
                # open rather than seeding reviews they do not need.
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
                patch(
                    "rootcoz.main._build_export_context",
                    new_callable=AsyncMock,
                    return_value=self._context(),
                ),
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                response = client.post("/results/job-1/push/greenwave")
            assert response.status_code == 400
            assert "disabled" in response.json()["detail"]

    def test_push_greenwave_reviewer_outage_returns_503(self, _init_db, temp_db_path):
        """A reviewer-table outage is 503, never 422.

        422 tells the caller their request is wrong; a failed internal read is
        the server's problem and retryable. Mirrors the
        EffectiveClassificationsUnavailable -> 503 split for the override table.
        """
        from rootcoz.exporters import ReviewersUnavailable
        from tests.conftest import admin_login, make_app_client

        exporter = MagicMock()
        exporter.__enter__.return_value = exporter
        exporter.push = AsyncMock(
            side_effect=ReviewersUnavailable("reviewer lookup failed")
        )

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
                patch(
                    "rootcoz.main._build_export_context",
                    new_callable=AsyncMock,
                    return_value=self._context(),
                ),
                patch("rootcoz.main._create_exporter", return_value=exporter),
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                response = client.post("/results/job-1/push/greenwave")
            assert response.status_code == 503
            assert "reviewer lookup failed" in response.json()["detail"]

    def test_push_greenwave_prerequisite_returns_422(self, _init_db, temp_db_path):
        """POST /push/greenwave maps ExporterPrerequisiteError to 422."""
        from rootcoz.exporters import ExporterPrerequisiteError
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                # The unreviewed-push gate is main's; these cases are about the
                # push plumbing (disabled/prerequisite/query params), so keep it
                # open rather than seeding reviews they do not need.
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
                patch(
                    "rootcoz.main._build_export_context",
                    new_callable=AsyncMock,
                    return_value=self._context(),
                ),
                patch(
                    "rootcoz.main._create_exporter",
                    side_effect=ExporterPrerequisiteError(
                        "missing GREENWAVE_PRODUCT_VERSION"
                    ),
                ),
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                response = client.post("/results/job-1/push/greenwave")
            assert response.status_code == 422
            assert "GREENWAVE_PRODUCT_VERSION" in response.json()["detail"]

    def test_push_greenwave_push_time_prerequisite_returns_422(
        self, _init_db, temp_db_path
    ):
        """ExporterPrerequisiteError raised by push() itself maps to 422 too."""
        from rootcoz.exporters import ExporterPrerequisiteError
        from tests.conftest import admin_login, make_app_client

        mock_exporter = MagicMock()
        mock_exporter.push = AsyncMock(
            side_effect=ExporterPrerequisiteError(
                "Greenwave waiver submission requires an explicit subject_identifier"
            )
        )
        mock_exporter.__enter__ = MagicMock(return_value=mock_exporter)
        mock_exporter.__exit__ = MagicMock(return_value=False)

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                # The unreviewed-push gate is main's; these cases are about the
                # push plumbing (disabled/prerequisite/query params), so keep it
                # open rather than seeding reviews they do not need.
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
                patch(
                    "rootcoz.main._build_export_context",
                    new_callable=AsyncMock,
                    return_value=self._context(),
                ),
                patch("rootcoz.main._create_exporter", return_value=mock_exporter),
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                response = client.post("/results/job-1/push/greenwave")
            assert response.status_code == 422
            assert "subject_identifier" in response.json()["detail"]

    def test_push_greenwave_plumbs_query_params(self, _init_db, temp_db_path):
        """POST /push/greenwave?subject_identifier=... reaches the export context."""
        from tests.conftest import admin_login, make_app_client

        mock_exporter = MagicMock()
        mock_exporter.push = AsyncMock(
            return_value=ExporterResult(
                success=True,
                message="Pushed 1 result(s) to ResultsDB",
                details={"pushed": 1, "errors": [], "resultsdb_ids": [101]},
            )
        )
        mock_exporter.__enter__ = MagicMock(return_value=mock_exporter)
        mock_exporter.__exit__ = MagicMock(return_value=False)

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                # The unreviewed-push gate is main's; these cases are about the
                # push plumbing (disabled/prerequisite/query params), so keep it
                # open rather than seeding reviews they do not need.
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_ctx,
                patch("rootcoz.main._create_exporter", return_value=mock_exporter),
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_ctx.return_value = self._context()
                response = client.post(
                    "/results/job-1/push/greenwave",
                    params={
                        "subject_identifier": "pkg-1.0-1.el10",
                        "waiver_comment": "known flake",
                    },
                )
            assert response.status_code == 200
            data = response.json()
            assert data["pushed"] == 1
            assert data["resultsdb_ids"] == [101]
            assert mock_ctx.call_args.kwargs["subject_identifier"] == "pkg-1.0-1.el10"
            assert mock_ctx.call_args.kwargs["waiver_comment"] == "known flake"


class TestEffectiveClassificationsFailClosed:
    """A gating push must not proceed on possibly-stale classifications."""

    @pytest.fixture(autouse=True)
    def _init_db(self, temp_db_path):
        import asyncio

        from rootcoz import storage

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(storage.init_db())
            yield

    def test_push_returns_503_when_overrides_unreadable(self, temp_db_path):
        """Storage failure -> 503 and no exporter call, never a JSON fallback."""
        from unittest.mock import patch

        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch(
                    "rootcoz.main.storage.get_all_effective_classifications",
                    new=AsyncMock(side_effect=RuntimeError("db down")),
                ),
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
                patch("rootcoz.main._create_exporter") as mock_create,
            ):
                mock_get.return_value = {
                    "result": {
                        "job_name": "j",
                        "build_number": 1,
                        "failures": [{"test_name": "t"}],
                    }
                }
                response = client.post("/results/job-1/push/greenwave")

            assert response.status_code == 503, response.text
            assert "overrides" in response.json()["detail"].lower()
            mock_create.assert_not_called()

    def test_exporter_declares_it_needs_effective_classifications(self):
        from rootcoz.exporters import GreenwaveExporter

        assert GreenwaveExporter.needs_effective_classifications is True

    def test_only_gating_exporters_request_the_lookup(self):
        """Non-gating exporters never trigger the override read at all."""
        from rootcoz.main import _exporter_needs_effective_classifications

        assert _exporter_needs_effective_classifications(["greenwave"]) is True
        assert _exporter_needs_effective_classifications(["reportportal"]) is False
        assert _exporter_needs_effective_classifications(["datadog"]) is False
        assert _exporter_needs_effective_classifications([]) is False


class TestGreenwaveUnreviewedPushGate:
    """Greenwave obeys main's unreviewed-push gate and has no escape hatch."""

    @pytest.fixture(autouse=True)
    def _init_db(self, tmp_path):
        import asyncio

        from rootcoz import storage

        with patch.object(storage, "DB_PATH", tmp_path / "gate.db"):
            asyncio.run(storage.init_db())
            yield

    def test_push_is_refused_with_409_and_names_the_scope(self, tmp_path):
        from unittest.mock import patch

        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(tmp_path / "gate.db"):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=2,
                ),
                patch("rootcoz.main._create_exporter") as mock_create,
            ):
                mock_get.return_value = {
                    "result": {
                        "job_name": "j",
                        "build_number": 1,
                        "failures": [{"test_name": "t"}],
                    }
                }
                response = client.post("/results/job-1/push/greenwave")

            assert response.status_code == 409, response.text
            detail = response.json()["detail"]
            assert "2 unreviewed failure(s)" in detail
            # No escape hatch exists for Greenwave: the message must not
            # advertise a toggle that would ungate it.
            assert "ALLOW_UNREVIEWED_PUSH" not in detail
            mock_create.assert_not_called()

    def test_greenwave_exposes_no_unreviewed_push_toggle(self):
        """Greenwave is absent from the toggle map, so it always stays gated."""
        from rootcoz.main import _UNREVIEWED_PUSH_TOGGLE_FIELDS

        assert "greenwave" not in _UNREVIEWED_PUSH_TOGGLE_FIELDS
        assert set(_UNREVIEWED_PUSH_TOGGLE_FIELDS) == {"reportportal", "datadog"}


class TestGatingRefusalAudit:
    """A refused gating push leaves an audit row with a meaningful reason."""

    @pytest.fixture(autouse=True)
    def _init_db(self, tmp_path):
        import asyncio

        from rootcoz import storage

        self.db_path = tmp_path / "audit.db"
        with patch.object(storage, "DB_PATH", self.db_path):
            asyncio.run(storage.init_db())
            yield

    def test_refusal_is_audited_with_service_unavailable(self):
        import asyncio

        from rootcoz import storage
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(self.db_path):
            admin_login(client)
            with (
                patch(
                    "rootcoz.main.storage.get_all_effective_classifications",
                    new=AsyncMock(side_effect=RuntimeError("db down")),
                ),
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._unreviewed_failure_count",
                    new_callable=AsyncMock,
                    return_value=0,
                ),
            ):
                mock_get.return_value = {
                    "result": {
                        "job_name": "j",
                        "build_number": 1,
                        "failures": [{"test_name": "t"}],
                    }
                }
                response = client.post("/results/job-1/push/greenwave")
            assert response.status_code == 503

        async def _events():
            with patch.object(storage, "DB_PATH", self.db_path):
                return await storage.list_audit_events(limit=50)

        page = asyncio.run(_events())
        events = page["events"] if isinstance(page, dict) else page
        push_rows = [e for e in events if e.get("action") == "export.push"]
        assert push_rows, "a refused push must leave an audit row"
        row = push_rows[0]
        # One row per attempt: the reason code names why, and the details name
        # the subject (plugin + job), with no request text copied in.
        assert row["outcome"] == "failure"
        assert row["details"]["reason"] == "service_unavailable"
        assert row["target_id"] == "job-1"
        assert row["details"]["plugin"] == "greenwave"


class TestAutoPushOverridesUnavailableAudit:
    """The per-scope overrides-unavailable skip leaves an audit row too."""

    @pytest.fixture
    async def setup_test_db(self, tmp_path):
        from rootcoz import storage

        self.db_path = tmp_path / "auto.db"
        with patch.object(storage, "DB_PATH", self.db_path):
            await storage.init_db()
            yield self.db_path

    async def test_scope_skip_is_audited(self, setup_test_db):
        from rootcoz import storage as st
        from rootcoz.main import _auto_review_matching_failures

        result_data = {
            "job_name": "my-job",
            "build_number": 101,
            "failures": [
                {
                    "test_name": "test_a",
                    "error": "err",
                    "error_signature": "sig-a",
                    "analysis": {"classification": "INFRASTRUCTURE"},
                }
            ],
        }
        from tests.conftest import build_test_env

        settings = Settings(
            **{
                k.lower(): v
                for k, v in build_test_env(AUTO_PUSH_EXPORTERS="reportportal").items()
            }
        )
        with (
            patch.object(st, "DB_PATH", setup_test_db),
            patch(
                "rootcoz.main._match_and_auto_review_failures",
                new_callable=AsyncMock,
                return_value=(1, 1),
            ),
            patch(
                "rootcoz.main._auto_review_child_failures",
                new_callable=AsyncMock,
                return_value=(0, 0),
            ),
            patch(
                "rootcoz.main._unreviewed_failure_count",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch("rootcoz.main._all_failures_ai_reviewed", return_value=True),
            # Force the per-scope context build to refuse.
            patch(
                "rootcoz.main._build_export_context",
                new_callable=AsyncMock,
                side_effect=__import__(
                    "rootcoz.main", fromlist=["EffectiveClassificationsUnavailable"]
                ).EffectiveClassificationsUnavailable("nope"),
            ),
            patch("rootcoz.main._create_exporter") as mock_create,
        ):
            await _auto_review_matching_failures(
                "current-job", "my-job", 101, result_data, settings
            )
            mock_create.assert_not_called()
            page = await st.list_audit_events(action="export.push", limit=20)

        rows = page["events"] if isinstance(page, dict) else page
        assert rows, "the per-scope refusal must leave an audit row"
        assert rows[0]["details"]["reason"] == "overrides_unavailable"
        assert rows[0]["details"]["scope"] == "root#0"
        assert rows[0]["actor"] == "rootcoz-ai"


class TestReviewerLookupFlag:
    """The reviewer-lookup FAILURE must be distinguishable from 'no reviews'.

    These pin the wiring, not just the exporter's use of it: if
    ``_build_export_context`` ever goes back to swallowing the error into an
    empty ``reviewed_by``, the waiver refusal above silently disappears.
    """

    @pytest.mark.asyncio
    async def test_failed_review_lookup_sets_the_flag(self, initialized_db):
        from rootcoz import main as main_module

        with patch.object(
            main_module.storage,
            "get_reviews_for_job",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            context = await main_module._build_export_context(
                job_id="job-1",
                result_data={
                    "job_name": "my-job",
                    "build_number": 42,
                    "failures": [{"test_name": "test_a"}],
                },
                settings=_make_settings(),
                fetch_history=False,
                fetch_tracked_in_links=False,
            )
        assert context.reviews_unavailable is True
        assert context.reviewed_by == {}

    @pytest.mark.asyncio
    async def test_successful_lookup_with_no_reviews_does_not_set_the_flag(
        self, initialized_db
    ):
        from rootcoz import main as main_module

        with patch.object(
            main_module.storage, "get_reviews_for_job", AsyncMock(return_value={})
        ):
            context = await main_module._build_export_context(
                job_id="job-1",
                result_data={
                    "job_name": "my-job",
                    "build_number": 42,
                    "failures": [{"test_name": "test_a"}],
                },
                settings=_make_settings(),
                fetch_history=False,
                fetch_tracked_in_links=False,
            )
        assert context.reviews_unavailable is False
