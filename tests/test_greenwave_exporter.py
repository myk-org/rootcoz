"""Tests for the Greenwave/ResultsDB exporter plugin (issue #183)."""

import asyncio
import os
import uuid as uuid_module
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


def _make_settings(**env_overrides: str) -> Settings:
    """Build Settings under a patched environment."""
    with patch.dict(os.environ, _greenwave_env(**env_overrides), clear=True):
        return Settings(_env_file=None)


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


def _make_context(
    failures: list[dict],
    *,
    subject_identifier: str | None = None,
    waiver_comment: str | None = None,
    reviewed_by: dict[str, str] | None = None,
    pushed_by: str = "operator",
    skip_waivers: bool = False,
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
        skip_waivers=skip_waivers,
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


class TestGreenwaveSettings:
    """Settings-level configuration for the Greenwave exporter."""

    def test_disabled_by_default(self):
        with patch.dict(os.environ, _build_env(), clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_enabled_when_url_and_token_configured(self):
        settings = _make_settings()
        assert settings.greenwave_enabled

    def test_disabled_when_url_missing(self):
        env = _build_env(GREENWAVE_API_TOKEN="rdb-token")  # pragma: allowlist secret
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_disabled_when_token_missing(self):
        env = _build_env(GREENWAVE_URL=RESULTSDB_URL)
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled

    def test_explicitly_disabled_overrides_config(self):
        settings = _make_settings(ENABLE_GREENWAVE="false")
        assert not settings.greenwave_enabled

    def test_explicitly_enabled_with_missing_url_warns(self):
        env = _build_env(ENABLE_GREENWAVE="true")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("rootcoz.config.logger") as mock_logger,
        ):
            settings = Settings(_env_file=None)
            assert not settings.greenwave_enabled
            assert mock_logger.warning.called

    def test_blank_url_becomes_none(self):
        settings = _make_settings(GREENWAVE_URL="   ")
        assert settings.greenwave_url is None
        assert not settings.greenwave_enabled

    @pytest.mark.parametrize(
        ("env_name", "bad_url"),
        [
            ("GREENWAVE_URL", "http://resultsdb.example.com"),
            (
                "GREENWAVE_URL",
                "https://user:pass@resultsdb.example.com",  # pragma: allowlist secret
            ),
            ("GREENWAVE_URL", "https://resultsdb.example.com?token=secret"),
            ("GREENWAVE_URL", "https://resultsdb.example.com#section"),
            ("GREENWAVE_URL", "not-a-url"),
            ("GREENWAVE_WAIVER_URL", "http://waiverdb.example.com"),
        ],
    )
    def test_invalid_greenwave_url_rejected(self, env_name: str, bad_url: str):
        env = _greenwave_env(**{env_name: bad_url})
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match=env_name),
        ):
            Settings(_env_file=None)

    def test_invalid_greenwave_port_rejected(self):
        """Syntactically invalid ports fail at Settings construction."""
        for env_name in ("GREENWAVE_URL", "GREENWAVE_WAIVER_URL"):
            env = _greenwave_env(**{env_name: "https://results.example.com:bad"})
            with (
                patch.dict(os.environ, env, clear=True),
                pytest.raises(ValueError, match="invalid port"),
            ):
                Settings(_env_file=None)

    def test_auto_push_without_auto_review_rejected(self):
        """AUTO_PUSH_EXPORTERS with auto-review disabled is a config error."""
        env = _greenwave_env(
            AUTO_PUSH_EXPORTERS="greenwave", ENABLE_AUTO_REVIEW="false"
        )
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="AUTO_PUSH_EXPORTERS"),
        ):
            Settings(_env_file=None)

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
            GreenwaveExporter(Settings(_env_file=None))

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
            GreenwaveExporter(Settings(_env_file=None))

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
            GreenwaveExporter(Settings(_env_file=None))

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
        assert group_call.kwargs["token"] == "rdb-token"
        assert group_call.kwargs["json"]["description"] == "rootcoz analysis job-123"

        # result requests: testcase names, outcome mapping, subject fallback
        first_result = post.call_args_list[1].kwargs["json"]
        assert first_result["testcase"]["name"] == "rootcoz.my-job.test_a"
        assert first_result["outcome"] == "FAILED"
        assert first_result["data"]["item"] == ["my-job"]  # job_name fallback
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
        assert result.details["errors"] == ["failure[0]: validation failed"]

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
            _run_push(exporter, _make_context([_failure("test_a", "INFRASTRUCTURE")]))

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

        async def tracked_post(client, url, *, token, json, label=""):
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

    def test_push_skip_waivers_allows_missing_subject_identifier(self):
        """skip_waivers (auto-push path) pushes results instead of refusing."""
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
                reviewed_by={"test_a": "reviewer"},
                skip_waivers=True,
            ),
        )
        assert result.success is True
        assert result.details["pushed"] == 1
        assert result.details["waived"] == 0
        # exactly two posts: group + one result; no waiver call
        assert post.call_count == 2
        assert not post.call_args_list[1].args[1].startswith(WAIVERDB_URL)

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
        assert waiver_call.kwargs["token"] == "wdb-token"
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

    def test_push_waiver_allowed_for_ai_reviewer_when_enabled(self):
        settings = _make_settings(
            GREENWAVE_PUSH_WAIVERS="true",
            GREENWAVE_WAIVER_URL=WAIVERDB_URL,
            GREENWAVE_WAIVER_TOKEN="wdb-token",  # pragma: allowlist secret
            GREENWAVE_PRODUCT_VERSION="el10",
            GREENWAVE_ALLOW_AI_WAIVERS="true",
        )
        exporter = GreenwaveExporter(settings)
        self._mock_post(
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
                reviewed_by={"test_a": AI_SYSTEM_USERNAME},
            ),
        )
        assert result.details["waived"] == 1

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
        settings.greenwave_api_token.get_secret_value.return_value = "rdb-token"
        settings.greenwave_waiver_url = None
        settings.greenwave_waiver_token = None
        settings.greenwave_push_waivers = False
        settings.greenwave_waivable_set = frozenset()
        settings.greenwave_allow_ai_waivers = False
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
        settings.greenwave_api_token = None
        with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_API_TOKEN"):
            _create_exporter("greenwave", settings)

    def test_exporter_classes_registry_contains_greenwave(self):
        from rootcoz.main import _EXPORTER_CLASSES

        assert "greenwave" in _EXPORTER_CLASSES

    def test_sensitive_settings_include_greenwave_tokens(self):
        from rootcoz.main import _SENSITIVE_SETTINGS

        assert "greenwave_api_token" in _SENSITIVE_SETTINGS
        assert "greenwave_waiver_token" in _SENSITIVE_SETTINGS


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

    def test_push_greenwave_prerequisite_returns_422(self, _init_db, temp_db_path):
        """POST /push/greenwave maps ExporterPrerequisiteError to 422."""
        from rootcoz.exporters import ExporterPrerequisiteError
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
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
