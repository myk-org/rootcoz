"""Tests for the Datadog v2 event exporter (issue #359, VMEDO-1256/1257).

Hermetic: httpx.AsyncClient is mocked everywhere — no real network calls.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from rootcoz import storage
from rootcoz.config import Settings
from rootcoz.exporters.base import ExportContext
from rootcoz.exporters.datadog import (
    API_KEY_IN_APP_SLOT_ERROR,
    APP_KEY_SERVICE_TOKEN_ERROR,
    APPLICATION_KEY_ERROR,
    SERVICE_ACCESS_TOKEN_ERROR,
    DatadogExporter,
    _auth_headers,
    classify_datadog_credential,
    sanitize_tag_value,
    validate_datadog_credentials,
)
from rootcoz.storage import AI_SYSTEM_USERNAME

_API_KEY = "dd-test-key"  # pragma: allowlist secret
_APP_KEY = "b" * 40  # pragma: allowlist secret
_SAT = "ddsat_service-token"  # pragma: allowlist secret
_EVENTS_URL = "https://event-management-intake.datadoghq.eu/api/v2/events"
_EXPECTED_HEADERS = {
    "DD-API-KEY": _API_KEY,
    "DD-APPLICATION-KEY": _APP_KEY,
    "Accept": "application/json",
}

#: v2 ``data.attributes`` keys — the whole documented event payload.
_ATTRIBUTE_KEYS = {
    "title",
    "message",
    "category",
    "timestamp",
    "aggregation_key",
    "tags",
    "attributes",
}

#: Fields that only the deprecated v1 events endpoint accepted.
_V1_ONLY_FIELDS = ("priority", "source_type_name", "date_happened", "text")


def _attributes(event: dict[str, Any]) -> dict[str, Any]:
    """The ``data.attributes`` object of a built v2 event."""
    return event["data"]["attributes"]


def _custom(event: dict[str, Any]) -> dict[str, Any]:
    """The ``data.attributes.attributes.custom`` free-form object."""
    return _attributes(event)["attributes"]["custom"]


def _message(event: dict[str, Any]) -> str:
    """The ``message`` field of a built v2 event."""
    return _attributes(event)["message"]


def _iso(epoch: int) -> str:
    """ISO 8601 UTC rendering the v2 intake expects."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure(
    test_name: str = "test_login_flow",
    classification: str = "PRODUCT BUG",
    details: str = "Session expired before token refresh.",
) -> dict[str, Any]:
    return {
        "test_name": test_name,
        "error": "boom",
        "analysis": {"classification": classification, "details": details},
    }


def _console_failure(test_name: str = "e2e-suite") -> dict[str, Any]:
    """Stored console-only failure: placeholder name + synthetic message.

    Identical to what ``CISource.run_console_only_analysis`` stores.
    """
    return {
        **_failure(test_name=test_name),
        "error": (
            "Console-only analysis — read the mandatory console-output section "
            "in the analysis prompt before analyzing."
        ),
    }


def _context(**overrides: Any) -> ExportContext:
    params: dict[str, Any] = {
        "job_id": "job-123",
        "job_name": "e2e-suite",
        "build_number": "42",
        "jenkins_url": "https://jenkins.example.com/job/e2e-suite/42/",
        "failures": [],
        "report_url": "https://rootcoz.example.com/results/job-123",
        "pushed_by": "alice",
        "analysis_timestamp": 1735689600,
    }
    params.update(overrides)
    return ExportContext(**params)


def _exporter(**overrides: Any) -> DatadogExporter:
    params: dict[str, Any] = {
        "api_key": _API_KEY,
        "app_key": _APP_KEY,
        "site": "datadoghq.eu",
    }
    params.update(overrides)
    return DatadogExporter(**params)


def _mock_http_client(
    responses: list[Any] | None = None,
) -> tuple[MagicMock, list[dict[str, Any]]]:
    """Build a mocked httpx.AsyncClient capturing POSTs.

    Args:
        responses: Per-call return values for ``post`` (or side effects).
            Defaults to a queue of successful responses.

    Returns:
        ``(MockClient, posts)`` — the patched class mock and the list the
        captured ``post`` calls are recorded into.
    """
    mock_client = AsyncMock()
    posts: list[dict[str, Any]] = []

    async def _post(url: str, json: dict[str, Any] | None = None, **kw: Any) -> Any:
        posts.append({"url": url, "json": json, **kw})
        if responses:
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        response = MagicMock()
        response.raise_for_status = MagicMock()
        return response

    mock_client.post = _post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_class = MagicMock(return_value=mock_client)
    return mock_class, posts


class TestSanitizeTagValue:
    def test_spaces_and_punctuation_replaced(self):
        assert sanitize_tag_value("PRODUCT BUG") == "PRODUCT_BUG"
        assert sanitize_tag_value("e2e: suite [main]") == "e2e:_suite__main"

    def test_allowed_characters_preserved(self):
        assert sanitize_tag_value("a-b_c.d:e9") == "a-b_c.d:e9"

    def test_empty_collapses_to_unknown(self):
        assert sanitize_tag_value("") == "unknown"
        assert sanitize_tag_value("///") == "unknown"


class TestV2Envelope:
    """The payload is the documented v2 event envelope, not a v1 event."""

    def test_envelope_shape(self):
        event = _exporter()._build_event(_context(), _failure())
        assert set(event) == {"data"}
        assert event["data"]["type"] == "event"
        assert set(_attributes(event)) == _ATTRIBUTE_KEYS

    def test_category_is_alert(self):
        """v2 has no generic "event" category — a failure is an alert."""
        for failure in (_failure(), _console_failure()):
            event = _exporter()._build_event(_context(), failure)
            assert _attributes(event)["category"] == "alert"

    def test_no_v1_only_fields(self):
        event = _exporter()._build_event(_context(), _failure())
        attributes = _attributes(event)
        for field in _V1_ONLY_FIELDS:
            assert field not in event, field
            assert field not in attributes, field
        # No severity either — classification and severity are unrelated.
        assert "alert_type" not in attributes

    def test_no_percent_wrapper(self):
        """The v1 ``%%%`` markdown wrapper is gone — v2 takes free-form text."""
        for failure in (_failure(), _console_failure()):
            message = _message(_exporter()._build_event(_context(), failure))
            assert "%%%" not in message, message
            assert message.startswith("### ")

    def test_timestamp_is_iso_8601(self):
        with patch("rootcoz.exporters.datadog.time.time", return_value=2_000_000.0):
            event = _exporter()._build_event(
                _context(analysis_timestamp=1_999_000), _failure()
            )
        assert _attributes(event)["timestamp"] == _iso(1_999_000)


class TestBuildEvent:
    """Payload shape for a single event."""

    def _event(self, **ctx_overrides: Any) -> dict[str, Any]:
        exporter = _exporter()
        return _attributes(exporter._build_event(_context(**ctx_overrides), _failure()))

    def test_title_format(self):
        assert self._event()["title"] == "[PRODUCT BUG] test_login_flow (Build #42)"

    def test_aggregation_key(self):
        assert self._event()["aggregation_key"] == "rootcoz-job-123-test_login_flow"

    def test_tags_shape(self):
        event = self._event()
        assert event["tags"] == [
            "tfa.stage:automated",
            "job_name:e2e-suite",
            "build_number:42",
            "rootcoz.analysis_id:job-123",
            "tfa.classification:PRODUCT_BUG",
            "pushed_by:alice",
        ]

    def test_service_and_env_tags_omitted_when_unset(self):
        event = self._event()
        assert not any(t.startswith("service:") for t in event["tags"])
        assert not any(t.startswith("env:") for t in event["tags"])

    def test_child_job_tag_when_scoped(self):
        event = self._event(child_job_name="child job", child_build_number=7)
        tags = event["tags"]
        assert "child_job_name:child_job" in tags
        # child_build_number is scoping metadata, not a tag
        assert not any(t.startswith("child_build_number") for t in tags)

    def test_pushed_by_falls_back_to_ai_system(self):
        event = self._event(pushed_by="")
        assert f"pushed_by:{AI_SYSTEM_USERNAME}" in event["tags"]

    def test_custom_service_and_env(self):
        exporter = _exporter(service="my svc", env="staging eu")
        event = _attributes(exporter._build_event(_context(), _failure()))
        assert "service:my_svc" in event["tags"]
        assert "env:staging_eu" in event["tags"]

    def test_classification_verbatim_in_title_and_message(self):
        exporter = _exporter()
        event = exporter._build_event(_context(), _failure())
        assert "[PRODUCT BUG]" in _attributes(event)["title"]
        assert "**Classification:** PRODUCT BUG" in _message(event)

    def test_message_body_contents(self):
        message = self._event()["message"]
        assert "### test_login_flow" in message
        assert "**Job:** e2e-suite (#42)" in message
        assert "https://rootcoz.example.com/results/job-123" in message
        assert "https://jenkins.example.com/job/e2e-suite/42/" in message
        assert "Session expired before token refresh." in message

    def test_message_body_without_links(self):
        context = _context()
        context.report_url = ""
        context.jenkins_url = ""
        exporter = _exporter()
        message = _message(exporter._build_event(context, _failure()))
        assert "RootCoz report" not in message
        assert "CI build" not in message

    def test_message_body_without_details(self):
        exporter = _exporter()
        failure = _failure(details="")
        assert "AI diagnosis" not in _message(
            exporter._build_event(_context(), failure)
        )

    def test_missing_analysis_dict_tolerated(self):
        exporter = _exporter()
        event = exporter._build_event(
            _context(), {"test_name": "test_x", "error": "boom"}
        )
        assert _attributes(event)["title"] == "[UNKNOWN] test_x (Build #42)"

    def test_legacy_string_analysis_used_as_details(self):
        """Legacy plain-string analyses carry the diagnosis text (models.py)."""
        exporter = _exporter()
        failure = {
            "test_name": "test_x",
            "error": "boom",
            "analysis": "Plain legacy diagnosis text.",
        }
        event = exporter._build_event(_context(), failure)
        assert _attributes(event)["title"] == "[UNKNOWN] test_x (Build #42)"
        assert "Plain legacy diagnosis text." in _message(event)
        assert "**AI diagnosis:**" in _message(event)

    def test_legacy_empty_string_analysis_tolerated(self):
        exporter = _exporter()
        failure = {
            "test_name": "test_x",
            "error": "boom",
            "analysis": "   ",
        }
        event = exporter._build_event(_context(), failure)
        assert _attributes(event)["title"] == "[UNKNOWN] test_x (Build #42)"
        assert "AI diagnosis" not in _message(event)


class TestCustomAttributes:
    """``attributes.custom`` carries the test identity, never a tag."""

    def test_parametrized_node_id_verbatim(self):
        node_id = "tests/test_x.py::test_concurrent_uploads[a-b:param]"
        event = _exporter()._build_event(_context(), _failure(test_name=node_id))
        assert _custom(event)["test"]["name"] == node_id
        assert _custom(event)["rootcoz"]["scope"] == "test"

    def test_no_test_name_tag(self):
        for failure in (_failure(), _console_failure()):
            event = _exporter()._build_event(_context(), failure)
            tags = _attributes(event)["tags"]
            assert not any(t.startswith("test_name:") for t in tags)

    def test_test_attribute_carries_only_name(self):
        event = _exporter()._build_event(_context(), _failure())
        assert _custom(event)["test"] == {"name": "test_login_flow"}

    def test_console_only_message_has_no_test_attribute(self):
        event = _exporter()._build_event(_context(), _console_failure())
        assert "test" not in _custom(event)
        assert _custom(event) == {"rootcoz": {"scope": "console"}}

    def test_console_only_title_and_heading(self):
        event = _exporter()._build_event(_context(), _console_failure())
        assert _attributes(event)["title"] == "[PRODUCT BUG] build failure (Build #42)"
        message = _message(event)
        assert "### Build failure" in message
        # The job name must never be presented as the failing test.
        assert "### e2e-suite" not in message
        assert "e2e-suite (Build #42)" not in _attributes(event)["title"]

    def test_console_only_title_without_classification(self):
        exporter = _exporter(push_classifications=False)
        event = exporter._build_event(_context(), _console_failure())
        assert _attributes(event)["title"] == "build failure (Build #42)"

    def test_real_test_named_like_job_is_not_console_only(self):
        # A structured test may legitimately share the job's name — the name
        # alone (no synthetic marker) must not demote it to a build failure.
        failure = _failure(test_name="e2e-suite")
        event = _exporter()._build_event(_context(), failure)
        assert _custom(event)["test"] == {"name": "e2e-suite"}
        assert _custom(event)["rootcoz"] == {"scope": "test"}
        assert _attributes(event)["title"] == "[PRODUCT BUG] e2e-suite (Build #42)"
        assert "### e2e-suite" in _message(event)

    def test_real_test_named_like_child_placeholder_is_not_console_only(self):
        context = _context(child_job_name="child job", child_build_number=7)
        failure = _failure(test_name="child job#7")
        event = _exporter()._build_event(context, failure)
        assert _custom(event)["test"] == {"name": "child job#7"}
        assert _custom(event)["rootcoz"] == {"scope": "test"}


class TestContentToggles:
    """dd_push_* content toggles — parity with the rp_push_* trio."""

    def test_classifications_toggle_off(self):
        exporter = _exporter(push_classifications=False)
        event = exporter._build_event(_context(), _failure())
        attributes = _attributes(event)
        assert attributes["title"] == "test_login_flow (Build #42)"
        assert "Classification" not in attributes["message"]
        assert not any(t.startswith("tfa.classification:") for t in attributes["tags"])
        # Unrelated content survives.
        assert "### test_login_flow" in attributes["message"]
        assert "**Job:** e2e-suite (#42)" in attributes["message"]

    def test_classifications_toggle_on_keeps_defaults(self):
        event = _exporter()._build_event(_context(), _failure())
        assert _attributes(event)["title"].startswith("[PRODUCT BUG]")
        assert "**Classification:** PRODUCT BUG" in _message(event)
        assert "tfa.classification:PRODUCT_BUG" in _attributes(event)["tags"]

    def test_rootcoz_url_toggle_off(self):
        exporter = _exporter(push_rootcoz_url=False)
        message = _message(exporter._build_event(_context(), _failure()))
        assert "RootCoz report" not in message
        assert "https://rootcoz.example.com/results/job-123" not in message
        # The CI build link is not part of this toggle.
        assert "CI build" in message

    def test_rootcoz_url_omitted_without_report_url(self):
        """No PUBLIC_BASE_URL → no report URL in the context → no line."""
        context = _context()
        context.report_url = ""
        message = _message(_exporter()._build_event(context, _failure()))
        assert "RootCoz report" not in message

    def test_tracker_links_included_when_present(self):
        context = _context(
            tracked_in_links={
                "test_login_flow": [
                    {
                        "tracked_in_url": "https://jira.example.com/browse/PROJ-1",
                        "tracked_in_type": "jira",
                    },
                    {
                        "tracked_in_url": "https://github.com/org/repo/issues/2",
                        "tracked_in_type": "github",
                    },
                ]
            }
        )
        message = _message(_exporter()._build_event(context, _failure()))
        assert (
            "- **Tracked in:** [Jira](https://jira.example.com/browse/PROJ-1)"
            in message
        )
        assert (
            "- **Tracked in:** [GitHub](https://github.com/org/repo/issues/2)"
            in message
        )

    def test_tracker_links_toggle_off(self):
        context = _context(
            tracked_in_links={
                "test_login_flow": [
                    {
                        "tracked_in_url": "https://jira.example.com/browse/PROJ-1",
                        "tracked_in_type": "jira",
                    }
                ]
            }
        )
        exporter = _exporter(push_tracker_links=False)
        message = _message(exporter._build_event(context, _failure()))
        assert "Tracked in" not in message
        assert "https://jira.example.com/browse/PROJ-1" not in message

    def test_tracker_links_for_other_tests_ignored(self):
        context = _context(
            tracked_in_links={
                "test_other": [
                    {
                        "tracked_in_url": "https://jira.example.com/browse/PROJ-1",
                        "tracked_in_type": "jira",
                    }
                ]
            }
        )
        message = _message(_exporter()._build_event(context, _failure()))
        assert "Tracked in" not in message

    def test_tracker_link_without_url_skipped(self):
        context = _context(
            tracked_in_links={
                "test_login_flow": [
                    {"tracked_in_url": "", "tracked_in_type": "jira"},
                    {"tracked_in_type": "github"},
                ]
            }
        )
        message = _message(_exporter()._build_event(context, _failure()))
        assert "Tracked in" not in message

    def test_unknown_tracker_type_gets_generic_label(self):
        context = _context(
            tracked_in_links={
                "test_login_flow": [
                    {
                        "tracked_in_url": "https://bugs.example.com/XYZ-1",
                        "tracked_in_type": "bugzilla",
                    }
                ]
            }
        )
        message = _message(_exporter()._build_event(context, _failure()))
        assert "- **Tracked in:** [issue](https://bugs.example.com/XYZ-1)" in message


class TestFieldLimits:
    """Datadog's documented v2 caps, enforced with a visible marker."""

    def test_short_title_and_message_untouched(self):
        event = _exporter()._build_event(_context(), _failure())
        assert "truncated" not in _attributes(event)["title"]
        assert "truncated" not in _message(event)

    def test_long_title_capped_at_500(self):
        event = _exporter()._build_event(_context(), _failure("t" * 600))
        title = _attributes(event)["title"]
        assert len(title) == 500
        assert title.endswith("… [truncated]")

    def test_long_message_capped_at_4000(self):
        event = _exporter()._build_event(_context(), _failure(details="d" * 5000))
        message = _message(event)
        assert len(message) == 4000
        assert message.endswith("… [truncated]")
        # The head of the diagnosis survives.
        assert "### test_login_flow" in message

    def test_long_console_message_capped_too(self):
        event = _exporter()._build_event(
            _context(), _console_failure() | {"analysis": {"details": "d" * 5000}}
        )
        assert len(_message(event)) == 4000


class TestHistoryClassificationsAndReviewers:
    """Report Portal parity: ExportContext history + reviewer fields.

    Neither is toggle-gated — Report Portal is not either.
    """

    def test_needs_history_classifications_flag(self):
        assert DatadogExporter.needs_history_classifications is True

    def test_history_classification_in_message_when_present(self):
        event = self._event(
            history_classifications={"test_login_flow": "INFRASTRUCTURE"}
        )
        assert "**Failure pattern:** INFRASTRUCTURE" in event["message"]
        # After the Classification line, before the links.
        message = event["message"]
        assert message.index("**Classification:**") < message.index(
            "**Failure pattern:**"
        )
        assert message.index("**Failure pattern:**") < message.index("RootCoz report")

    def test_history_classification_uses_the_test_own_entry(self):
        event = self._event(history_classifications={"test_other": "CODE ISSUE"})
        assert "Failure pattern" not in event["message"]

    def test_history_classification_absent_when_dict_empty(self):
        event = self._event()
        assert "Failure pattern" not in event["message"]

    def test_history_classification_not_toggle_gated(self):
        """The dd_push_classifications toggle covers the current classification
        only — history parity with Report Portal is unconditional."""
        exporter = _exporter(push_classifications=False)
        context = _context(
            history_classifications={"test_login_flow": "INFRASTRUCTURE"}
        )
        message = _message(exporter._build_event(context, _failure()))
        assert "**Failure pattern:** INFRASTRUCTURE" in message
        assert "Classification:**" not in message  # current one still toggled off

    def test_reviewer_line_and_tag_when_present(self):
        event = self._event(reviewed_by={"test_login_flow": "bob"})
        assert "**Reviewed by:** bob" in event["message"]
        assert "reviewed_by:bob" in event["tags"]
        # Tag comes right after the pushed_by attribution tag.
        tags = event["tags"]
        assert tags.index("pushed_by:alice") < tags.index("reviewed_by:bob")

    def test_reviewer_absent_when_no_entry(self):
        event = self._event(reviewed_by={"test_other": "bob"})
        assert "Reviewed by" not in event["message"]
        assert not any(t.startswith("reviewed_by:") for t in event["tags"])

    def test_reviewer_username_sanitized_in_tag_only(self):
        """Tag values must be tag-safe; the message line keeps the raw username."""
        event = self._event(reviewed_by={"test_login_flow": "Bob Smith!"})
        assert "reviewed_by:Bob_Smith" in event["tags"]
        assert "**Reviewed by:** Bob Smith!" in event["message"]

    def _event(self, **ctx_overrides: Any) -> dict[str, Any]:
        exporter = _exporter()
        return _attributes(exporter._build_event(_context(**ctx_overrides), _failure()))


class TestCredentialClassification:
    """Multi-type Datadog credentials: API key, SAT, application key."""

    def test_api_key_32_hex(self):
        assert classify_datadog_credential("a" * 32) == "api_key"

    def test_unrecognized_value_falls_back_to_api_key(self):
        # Only shapes Datadog documents are special-cased.
        assert classify_datadog_credential("dd-test-key") == "api_key"
        assert classify_datadog_credential("") == "api_key"

    def test_service_access_token_prefix(self):
        assert classify_datadog_credential("ddsat_anything") == "service_token"

    def test_application_key_ddapp_prefix(self):
        assert classify_datadog_credential("ddapp_anything") == "application_key"

    def test_application_key_legacy_40_hex(self):
        assert classify_datadog_credential("b" * 40) == "application_key"

    def test_whitespace_stripped_before_classifying(self):
        assert classify_datadog_credential("  ddsat_tok \n") == "service_token"
        assert classify_datadog_credential("\t" + "c" * 40 + " ") == "application_key"

    def test_auth_headers_carry_both_keys(self):
        """The v2 intake needs both keys on every request."""
        headers = _auth_headers(f" {_API_KEY} ", f" {_APP_KEY}\n")
        assert headers == _EXPECTED_HEADERS


class TestCredentialValidation:
    """Both keys are required and each slot rejects the other's shape."""

    @staticmethod
    def _validate(**overrides: Any) -> str:
        params: dict[str, Any] = {
            "api_key": _API_KEY,
            "app_key": _APP_KEY,
            "site": "datadoghq.eu",
        }
        params.update(overrides)
        return validate_datadog_credentials(**params)

    def test_valid_pair_returns_normalized_site(self):
        assert self._validate(site="https://api.datadoghq.com/") == "datadoghq.com"

    @pytest.mark.parametrize("app_key", ["ddapp_" + "k" * 30, "d" * 40])
    def test_application_key_shapes_accepted_in_app_slot(self, app_key):
        assert self._validate(app_key=app_key) == "datadoghq.eu"

    def test_missing_api_key(self):
        with pytest.raises(ValueError, match="datadog_api_key is required"):
            self._validate(api_key="  ")

    def test_missing_app_key(self):
        with pytest.raises(ValueError, match="datadog_app_key is required"):
            self._validate(app_key="   ")

    def test_application_key_in_api_slot_points_at_the_right_setting(self):
        with pytest.raises(ValueError) as exc_info:
            self._validate(api_key="ddapp_" + "k" * 30)  # pragma: allowlist secret
        assert str(exc_info.value) == APPLICATION_KEY_ERROR
        assert "put it in the application key setting" in APPLICATION_KEY_ERROR
        assert "Datadog application keys cannot post events" in APPLICATION_KEY_ERROR

    def test_api_key_in_app_slot_points_at_the_right_setting(self):
        with pytest.raises(ValueError) as exc_info:
            self._validate(app_key="a" * 32)
        assert str(exc_info.value) == API_KEY_IN_APP_SLOT_ERROR
        assert "put it in the API key setting" in API_KEY_IN_APP_SLOT_ERROR

    def test_service_token_rejected_in_api_slot(self):
        with pytest.raises(ValueError) as exc_info:
            self._validate(api_key=_SAT)
        assert str(exc_info.value) == SERVICE_ACCESS_TOKEN_ERROR

    def test_service_token_rejected_in_app_slot(self):
        with pytest.raises(ValueError) as exc_info:
            self._validate(app_key=_SAT)
        assert str(exc_info.value) == APP_KEY_SERVICE_TOKEN_ERROR
        assert APP_KEY_SERVICE_TOKEN_ERROR != SERVICE_ACCESS_TOKEN_ERROR

    def test_invalid_site_after_keys(self):
        with pytest.raises(ValueError, match="Invalid Datadog site"):
            self._validate(site="localhost")

    def test_message_names_one_problem_at_a_time(self):
        """A missing key is reported before the site is even looked at."""
        with pytest.raises(ValueError, match="datadog_app_key is required"):
            self._validate(app_key="", site="not a site!!")


class TestServiceTokenFailFast:
    """Service Access Tokens cannot post events, so both the constructor and
    the connection test fail fast with a message distinct from the
    application-key one."""

    @pytest.mark.parametrize(
        "credential",
        [
            "ddsat_" + "t" * 30,
            "  ddsat_tok \n",  # whitespace-stripped before classifying
        ],
        ids=["ddsat-prefix", "whitespace-stripped"],
    )
    def test_constructor_rejects_service_tokens(self, credential):
        with pytest.raises(ValueError) as exc_info:
            _exporter(api_key=credential)
        assert str(exc_info.value) == SERVICE_ACCESS_TOKEN_ERROR

    def test_site_check_not_reached_for_service_token(self):
        """The credential check fires before the site validation."""
        with pytest.raises(ValueError, match="service access tokens"):
            _exporter(
                api_key="ddsat_tok",  # pragma: allowlist secret
                site="not a site!!",
            )

    async def test_service_token_never_reaches_the_events_api(self):
        """No POST can be attempted with a rejected credential."""
        mock_class, posts = _mock_http_client()
        with (
            patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class),
            # The constructor itself raises — no push, no HTTP client.
            pytest.raises(ValueError, match="service access tokens"),
        ):
            _exporter(api_key=_SAT)
        assert posts == []


class TestConstructorFailFast:
    """The exporter cannot be built without a usable credential trio."""

    def test_missing_app_key(self):
        with pytest.raises(ValueError, match="datadog_app_key is required"):
            _exporter(app_key="")

    def test_missing_api_key(self):
        with pytest.raises(ValueError, match="datadog_api_key is required"):
            _exporter(api_key="")

    def test_site_check_not_reached_for_missing_app_key(self):
        with pytest.raises(ValueError, match="datadog_app_key is required"):
            _exporter(app_key="", site="not a site!!")

    def test_application_key_in_api_slot(self):
        with pytest.raises(ValueError, match="application keys cannot post events"):
            _exporter(api_key="f" * 40)


class TestConnectionTester:
    """_test_datadog_connection validates both keys before reporting success."""

    @staticmethod
    def _settings(**overrides: Any) -> Settings:
        params: dict[str, Any] = {
            "datadog_api_key": _API_KEY,  # pragma: allowlist secret
            "datadog_app_key": _APP_KEY,  # pragma: allowlist secret
            "datadog_site": "datadoghq.eu",
        }
        params.update(overrides)
        return Settings(**params)

    async def test_application_key_fails_fast_with_explanatory_message(self):
        from rootcoz.main import _test_datadog_connection

        with patch(
            "rootcoz.main._integration_test_get_json", new_callable=AsyncMock
        ) as mock_get:
            result = await _test_datadog_connection(
                self._settings(datadog_api_key="f" * 40)
            )

        assert result["ok"] is False
        assert result["message"] == APPLICATION_KEY_ERROR
        mock_get.assert_not_awaited()  # no request with a misplaced key

    async def test_ddapp_prefix_in_api_slot_fails_fast_too(self):
        from rootcoz.main import _test_datadog_connection

        result = await _test_datadog_connection(
            self._settings(datadog_api_key="ddapp_tok")  # pragma: allowlist secret
        )
        assert result["ok"] is False
        assert result["message"] == APPLICATION_KEY_ERROR

    async def test_service_token_fails_fast_with_explanatory_message(self):
        from rootcoz.main import _test_datadog_connection

        with patch(
            "rootcoz.main._integration_test_get_json", new_callable=AsyncMock
        ) as mock_get:
            result = await _test_datadog_connection(
                self._settings(datadog_api_key=_SAT)
            )

        assert result["ok"] is False
        assert result["message"] == SERVICE_ACCESS_TOKEN_ERROR
        mock_get.assert_not_awaited()

    async def test_missing_app_key_is_not_configured(self):
        from rootcoz.main import _test_datadog_connection

        with patch(
            "rootcoz.main._integration_test_get_json", new_callable=AsyncMock
        ) as mock_get:
            result = await _test_datadog_connection(
                self._settings(datadog_app_key=None)
            )

        assert result["ok"] is False
        assert "not configured" in result["message"]
        assert "datadog_app_key" in result["message"]
        mock_get.assert_not_awaited()

    async def test_both_keys_probed_with_both_headers(self):
        from rootcoz.main import _test_datadog_connection

        with patch(
            "rootcoz.main._integration_test_get_json", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = {"valid": True}
            result = await _test_datadog_connection(self._settings())

        assert result["ok"] is True
        assert result["details"] == {"site": "datadoghq.eu"}
        assert [call.args[0] for call in mock_get.await_args_list] == [
            "https://api.datadoghq.eu/api/v1/validate",
            "https://api.datadoghq.eu/api/v1/dashboard",
        ]
        for call in mock_get.await_args_list:
            assert call.kwargs["headers"] == _EXPECTED_HEADERS

    async def test_application_key_rejected(self):
        from rootcoz.main import _IntegrationTestError, _test_datadog_connection

        async def _get(url: str, **_kwargs: Any) -> dict[str, Any]:
            if url.endswith("/dashboard"):
                raise _IntegrationTestError("authentication failed (HTTP 403)")
            return {"valid": True}

        with patch("rootcoz.main._integration_test_get_json", new=_get):
            result = await _test_datadog_connection(self._settings())

        assert result["ok"] is False
        assert "application key rejected" in result["message"]

    async def test_api_key_rejected(self):
        from rootcoz.main import _IntegrationTestError, _test_datadog_connection

        async def _get(url: str, **_kwargs: Any) -> dict[str, Any]:
            raise _IntegrationTestError("authentication failed (HTTP 401)")

        with patch("rootcoz.main._integration_test_get_json", new=_get):
            result = await _test_datadog_connection(self._settings())

        assert result["ok"] is False
        assert "API key rejected" in result["message"]

    async def test_keys_never_echoed_in_result(self):
        from rootcoz.main import _test_datadog_connection

        with patch(
            "rootcoz.main._integration_test_get_json", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = {"valid": True}
            result = await _test_datadog_connection(self._settings())

        assert _API_KEY not in str(result)
        assert _APP_KEY not in str(result)


class TestPush:
    async def test_empty_failures(self):
        result = await _exporter().push(_context())
        assert result.success is False
        assert result.details == {"pushed": 0, "total": 0, "errors": [], "skipped": 0}

    async def test_one_event_per_failure(self):
        context = _context(
            failures=[
                _failure("test_a"),
                _failure("test_b", classification="INFRASTRUCTURE"),
                _failure("test_c", classification="CODE ISSUE"),
            ]
        )
        mock_class, posts = _mock_http_client()
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(context)

        assert result.success is True
        assert result.details["pushed"] == 3
        assert result.details["total"] == 3
        assert result.details["errors"] == []
        assert len(posts) == 3
        assert [_attributes(p["json"])["title"] for p in posts] == [
            "[PRODUCT BUG] test_a (Build #42)",
            "[INFRASTRUCTURE] test_b (Build #42)",
            "[CODE ISSUE] test_c (Build #42)",
        ]
        assert [_attributes(p["json"])["aggregation_key"] for p in posts] == [
            "rootcoz-job-123-test_a",
            "rootcoz-job-123-test_b",
            "rootcoz-job-123-test_c",
        ]

    async def test_posts_v2_intake_url_with_both_key_headers(self):
        mock_class, posts = _mock_http_client()
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            await _exporter().push(_context(failures=[_failure()]))

        assert posts[0]["url"] == _EVENTS_URL
        assert posts[0]["headers"] == _EXPECTED_HEADERS
        assert posts[0]["json"]["data"]["type"] == "event"

    async def test_custom_site(self):
        exporter = _exporter(site="ddog.us")
        mock_class, posts = _mock_http_client()
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            await exporter.push(_context(failures=[_failure()]))
        assert posts[0]["url"] == (
            "https://event-management-intake.ddog.us/api/v2/events"
        )

    async def test_http_error_recorded_not_raised(self):
        response = MagicMock()
        response.status_code = 500
        error = httpx.HTTPStatusError(
            "Server Error", request=MagicMock(), response=response
        )
        mock_class, _posts = _mock_http_client(responses=[error])
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(_context(failures=[_failure()]))

        assert result.success is False
        assert result.details["pushed"] == 0
        assert result.details["skipped"] == 0
        assert "test_login_flow" in result.details["errors"][0]
        assert "HTTP 500" in result.details["errors"][0]

    async def test_network_error_recorded_not_raised(self):
        mock_class, _ = _mock_http_client(
            responses=[httpx.RequestError("Connection refused")]
        )
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(_context(failures=[_failure()]))

        assert result.success is False
        assert result.details["pushed"] == 0
        assert len(result.details["errors"]) == 1

    async def test_one_failed_event_does_not_abort_the_rest(self):
        error = httpx.RequestError("boom")
        mock_class, posts = _mock_http_client(responses=[error])
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(
                _context(failures=[_failure("test_a"), _failure("test_b")])
            )

        assert len(posts) == 2  # second event still attempted
        assert result.details["pushed"] == 1
        assert result.details["errors"] == ["Failed to emit Datadog event for 'test_a'"]

    async def test_non_dict_failure_entry_recorded(self):
        mock_class, _posts = _mock_http_client()
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(
                _context(failures=["not-a-dict", _failure()])
            )
        assert result.details["pushed"] == 1
        assert "non-dict" in result.details["errors"][0]

    async def test_falls_back_to_push_time_when_no_analysis_timestamp(self):
        context = _context(failures=[_failure()])
        context.analysis_timestamp = None
        mock_class, posts = _mock_http_client()
        with (
            patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class),
            patch("rootcoz.exporters.datadog.time.time", return_value=12345.0),
        ):
            await _exporter().push(context)
        assert _attributes(posts[0]["json"])["timestamp"] == _iso(12345)


class TestExporterAbc:
    def test_name_and_display_name(self):
        exporter = _exporter()
        assert exporter.name == "datadog"
        assert exporter.display_name == "Datadog"
        assert exporter.is_enabled is True

    def test_registered_in_exporter_classes(self):
        from rootcoz.main import _EXPORTER_CLASSES

        assert _EXPORTER_CLASSES["datadog"] is DatadogExporter


class TestFactory:
    """_create_exporter / _available_exporters / Settings wiring."""

    @staticmethod
    def _settings(**overrides: Any) -> Settings:
        params: dict[str, Any] = {
            "datadog_api_key": _API_KEY,  # pragma: allowlist secret
            "datadog_app_key": _APP_KEY,  # pragma: allowlist secret
            "datadog_site": "datadoghq.eu",
            "dd_push_rootcoz_url": False,
        }
        params.update(overrides)
        return Settings(**params)

    def test_create_datadog_exporter(self):
        from rootcoz.main import _create_exporter

        exporter = _create_exporter("datadog", self._settings())
        assert isinstance(exporter, DatadogExporter)
        assert exporter._push_classifications is True
        assert exporter._push_rootcoz_url is False
        assert exporter._push_tracker_links is True
        assert exporter._app_key == _APP_KEY

    def test_create_datadog_requires_both_keys(self):
        from rootcoz.main import _create_exporter

        settings = self._settings()
        settings.datadog_app_key = None
        with pytest.raises(ValueError, match="disabled or not configured"):
            _create_exporter("datadog", settings)

    def test_create_datadog_disabled_raises(self):
        from rootcoz.main import _create_exporter

        with pytest.raises(ValueError, match="disabled"):
            _create_exporter("datadog", self._settings(enable_datadog=False))

    def test_available_exporters_lists_datadog(self):
        from rootcoz.main import _available_exporters

        settings = MagicMock()
        settings.reportportal_enabled = False
        settings.datadog_enabled = True
        exporters = _available_exporters(settings)
        dd = next(e for e in exporters if e.name == "datadog")
        assert dd.display_name == "Datadog"
        assert dd.enabled is True

    def test_available_exporters_datadog_disabled(self):
        from rootcoz.main import _available_exporters

        settings = MagicMock()
        settings.reportportal_enabled = False
        settings.datadog_enabled = False
        exporters = _available_exporters(settings)
        dd = next(e for e in exporters if e.name == "datadog")
        assert dd.enabled is False


class TestSettings:
    def test_defaults(self):
        from rootcoz.config import Settings

        settings = Settings()
        assert settings.datadog_site is None
        assert settings.datadog_service is None
        assert settings.datadog_env is None
        assert settings.enable_datadog is None
        assert settings.datadog_enabled is False  # no keys
        # Content toggles default on.
        assert settings.dd_push_classifications is True
        assert settings.dd_push_rootcoz_url is True
        assert settings.dd_push_tracker_links is True
        # Manual pushes of unreviewed jobs are gated by default.
        assert settings.rp_allow_unreviewed_push is False
        assert settings.dd_allow_unreviewed_push is False

    def test_enabled_requires_both_keys_and_site(self, monkeypatch):
        from rootcoz.config import Settings

        monkeypatch.setenv("ENABLE_DATADOG", "true")
        monkeypatch.setenv("DATADOG_SITE", "datadoghq.eu")
        assert Settings().datadog_enabled is False  # no keys

        monkeypatch.setenv("DATADOG_API_KEY", "dd-key")  # pragma: allowlist secret
        assert Settings().datadog_enabled is False  # still no app key

        monkeypatch.setenv("DATADOG_APP_KEY", _APP_KEY)  # pragma: allowlist secret
        assert Settings().datadog_enabled is True

    def test_enabled_requires_site(self, monkeypatch):
        from rootcoz.config import Settings

        monkeypatch.setenv("ENABLE_DATADOG", "true")
        monkeypatch.setenv("DATADOG_API_KEY", "dd-key")  # pragma: allowlist secret
        monkeypatch.setenv("DATADOG_APP_KEY", _APP_KEY)  # pragma: allowlist secret
        assert Settings().datadog_enabled is False  # no site

        monkeypatch.setenv("DATADOG_SITE", "datadoghq.eu")
        assert Settings().datadog_enabled is True

    def test_disabled_ignores_credentials(self, monkeypatch):
        from rootcoz.config import Settings

        monkeypatch.setenv("ENABLE_DATADOG", "false")
        monkeypatch.setenv("DATADOG_API_KEY", "dd-key")  # pragma: allowlist secret
        monkeypatch.setenv("DATADOG_APP_KEY", _APP_KEY)  # pragma: allowlist secret
        monkeypatch.setenv("DATADOG_SITE", "datadoghq.eu")
        assert Settings().datadog_enabled is False

    def test_auto_enabled_by_credentials(self, monkeypatch):
        from rootcoz.config import Settings

        # None (unset) + keys + site -> auto-enabled, same as Report Portal
        monkeypatch.setenv("DATADOG_API_KEY", "dd-key")  # pragma: allowlist secret
        monkeypatch.setenv("DATADOG_APP_KEY", _APP_KEY)  # pragma: allowlist secret
        monkeypatch.setenv("DATADOG_SITE", "datadoghq.eu")
        assert Settings().enable_datadog is None
        assert Settings().datadog_enabled is True

    def test_blank_app_key_is_none(self, monkeypatch):
        from rootcoz.config import Settings

        monkeypatch.setenv("ENABLE_DATADOG", "true")
        monkeypatch.setenv("DATADOG_SITE", "datadoghq.eu")
        monkeypatch.setenv("DATADOG_API_KEY", "dd-key")  # pragma: allowlist secret
        monkeypatch.setenv("DATADOG_APP_KEY", "  ")
        settings = Settings()
        assert settings.datadog_app_key is None
        assert settings.datadog_enabled is False

    def test_settings_category_and_sensitive_registration(self):
        from rootcoz.main import (
            _DEFAULTS_HIDDEN_SETTINGS,
            _FIELD_TO_CATEGORY,
            _RESTART_REQUIRED_SETTINGS,
            _SENSITIVE_SETTINGS,
        )

        assert "Datadog" == _FIELD_TO_CATEGORY["datadog_api_key"]
        assert "Datadog" == _FIELD_TO_CATEGORY["datadog_app_key"]
        assert _FIELD_TO_CATEGORY["enable_datadog"] == "Datadog"
        assert "datadog_api_key" in _SENSITIVE_SETTINGS
        assert "datadog_app_key" in _SENSITIVE_SETTINGS
        for field in (
            "dd_push_classifications",
            "dd_push_rootcoz_url",
            "dd_push_tracker_links",
            "dd_allow_unreviewed_push",
        ):
            assert _FIELD_TO_CATEGORY[field] == "Datadog", field
            # Same registries as the rp_push_* trio: hidden from the
            # any-role defaults payload, not sensitive.
            assert field in _DEFAULTS_HIDDEN_SETTINGS, field
            assert field not in _SENSITIVE_SETTINGS, field
            # Runtime-tunable: no restart needed.
            assert field not in _RESTART_REQUIRED_SETTINGS, field
        assert _FIELD_TO_CATEGORY["rp_allow_unreviewed_push"] == "Report Portal"
        assert "rp_allow_unreviewed_push" in _DEFAULTS_HIDDEN_SETTINGS
        assert "rp_allow_unreviewed_push" not in _SENSITIVE_SETTINGS
        assert "rp_allow_unreviewed_push" not in _RESTART_REQUIRED_SETTINGS

    def test_app_key_encrypted_at_rest(self):
        """The new setting is encrypted like every other credential."""
        from rootcoz.encryption import SENSITIVE_KEYS, encrypt_sensitive_fields

        encrypted = encrypt_sensitive_fields({"datadog_app_key": _APP_KEY})
        assert encrypted["datadog_app_key"] != _APP_KEY
        assert SENSITIVE_KEYS.issuperset({"datadog_api_key", "datadog_app_key"})

    async def test_app_key_stripped_from_api_responses(self):
        """The non-sensitive settings payload never carries either key."""
        from rootcoz.main import get_default_server_settings

        payload = await get_default_server_settings(
            Settings(
                datadog_api_key=_API_KEY,  # pragma: allowlist secret
                datadog_app_key=_APP_KEY,  # pragma: allowlist secret
                datadog_site="datadoghq.eu",
            )
        )
        assert "datadog_app_key" not in payload
        assert "datadog_api_key" not in payload
        # Non-secret settings survive untouched.
        assert payload["datadog_site"] == "datadoghq.eu"


class TestTimestampClamp:
    """Datadog rejects v2 events older than its ~18h intake window."""

    def test_fresh_analysis_timestamp_preserved(self):
        with patch("rootcoz.exporters.datadog.time.time", return_value=1_000_000.0):
            event = _exporter()._build_event(
                _context(analysis_timestamp=1_000_000 - 60), _failure()
            )
        assert _attributes(event)["timestamp"] == _iso(1_000_000 - 60)

    def test_stale_timestamp_clamped_to_push_time(self):
        """A manual push a day later must not emit a rejected timestamp."""
        stale = 1_000_000 - 18 * 3600
        with patch("rootcoz.exporters.datadog.time.time", return_value=1_000_000.0):
            event = _exporter()._build_event(
                _context(analysis_timestamp=stale), _failure()
            )
        assert _attributes(event)["timestamp"] == _iso(1_000_000)

    def test_boundary_exactly_17h_still_used(self):
        boundary = 1_000_000 - 17 * 3600
        with patch("rootcoz.exporters.datadog.time.time", return_value=1_000_000.0):
            event = _exporter()._build_event(
                _context(analysis_timestamp=boundary), _failure()
            )
        assert _attributes(event)["timestamp"] == _iso(boundary)


class TestAggregationKeyClamp:
    """Datadog caps aggregation_key at 100 characters."""

    def test_short_name_unchanged(self):
        event = _exporter()._build_event(_context(), _failure("test_a"))
        assert _attributes(event)["aggregation_key"] == "rootcoz-job-123-test_a"

    def test_long_name_truncated_with_hash_suffix(self):
        test_name = "tests.test_very_long_module.TestClass::test_" + "x" * 120
        event = _exporter()._build_event(_context(), _failure(test_name))
        key = _attributes(event)["aggregation_key"]
        assert len(key) <= 100
        assert key.startswith("rootcoz-job-123-")
        digest = hashlib.sha256(test_name.encode("utf-8")).hexdigest()[:8]
        assert key.endswith(f"-{digest}")
        # The truncated test name is still visible before the digest.
        assert key != f"rootcoz-job-123-{test_name}"

    def test_long_names_never_collide(self):
        """Distinct long names sharing a prefix must produce distinct keys."""
        base = "tests.test_module.TestClass::test_scenario_" + "y" * 100
        event_a = _exporter()._build_event(_context(), _failure(base + "_one"))
        event_b = _exporter()._build_event(_context(), _failure(base + "_two"))
        key_a = _attributes(event_a)["aggregation_key"]
        key_b = _attributes(event_b)["aggregation_key"]
        assert key_a != key_b
        assert len(key_a) <= 100
        assert len(key_b) <= 100

    def test_boundary_exactly_100_chars_unchanged(self):
        """A key of exactly 100 characters fits and is not hashed."""
        test_name = "t" * (100 - len("rootcoz-job-123-"))
        event = _exporter()._build_event(_context(), _failure(test_name))
        assert _attributes(event)["aggregation_key"] == f"rootcoz-job-123-{test_name}"


class TestAuthFailFast:
    """HTTP 401/403 means the keys are bad — abort the whole push."""

    @staticmethod
    def _status_error(status: int) -> httpx.HTTPStatusError:
        response = MagicMock()
        response.status_code = status
        return httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=response
        )

    async def test_401_aborts_remaining_events(self):
        mock_class, posts = _mock_http_client(responses=[self._status_error(401)])
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(
                _context(
                    failures=[
                        _failure("test_a"),
                        _failure("test_b"),
                        _failure("test_c"),
                    ]
                )
            )

        assert len(posts) == 1  # remaining events never attempted
        assert result.success is False
        assert result.details["pushed"] == 0
        assert result.details["skipped"] == 2
        assert result.details["errors"] == [
            "Failed to emit Datadog event for 'test_a' (HTTP 401)"
        ]
        assert "credentials" in result.message
        assert "HTTP 401" in result.message
        assert "2 event(s) skipped" in result.message

    async def test_403_aborts_remaining_events(self):
        mock_class, posts = _mock_http_client(responses=[self._status_error(403)])
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(
                _context(failures=[_failure("test_a"), _failure("test_b")])
            )

        assert len(posts) == 1
        assert result.details["skipped"] == 1
        assert "HTTP 403" in result.message

    async def test_auth_abort_after_earlier_success(self):
        """The abort message wins over earlier per-event errors/results."""
        ok = MagicMock()
        ok.raise_for_status = MagicMock()
        mock_class, posts = _mock_http_client(responses=[ok, self._status_error(401)])
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(
                _context(failures=[_failure("test_a"), _failure("test_b")])
            )

        assert len(posts) == 2
        assert result.details["pushed"] == 1
        assert result.details["skipped"] == 0
        assert "credentials" in result.message


class TestCircuitBreaker:
    """After 5 consecutive failed events, skip the rest."""

    async def test_breaks_after_five_consecutive_failures(self):
        failures = [_failure(f"test_{i}") for i in range(7)]
        responses: list[Any] = [httpx.RequestError("boom")] * 7
        mock_class, posts = _mock_http_client(responses=responses)
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(_context(failures=failures))

        assert len(posts) == 5  # circuit broke, last 2 never attempted
        assert result.success is False
        assert result.details["pushed"] == 0
        assert result.details["total"] == 7
        assert result.details["skipped"] == 2
        assert len(result.details["errors"]) == 5
        assert "consecutive failed events" in result.message
        assert "2 event(s) skipped" in result.message

    async def test_success_resets_consecutive_counter(self):
        """Interleaved successes keep the push running past 5 total errors."""
        ok = MagicMock()
        ok.raise_for_status = MagicMock()
        responses: list[Any] = [
            httpx.RequestError("boom"),
            httpx.RequestError("boom"),
            httpx.RequestError("boom"),
            httpx.RequestError("boom"),
            ok,
            httpx.RequestError("boom"),
            httpx.RequestError("boom"),
            ok,
        ]
        failures = [_failure(f"test_{i}") for i in range(8)]
        mock_class, posts = _mock_http_client(responses=responses)
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(_context(failures=failures))

        assert len(posts) == 8  # never broke — no run of 5 consecutive failures
        assert result.details["pushed"] == 2
        assert result.details["skipped"] == 0
        assert len(result.details["errors"]) == 6

    async def test_non_dict_entries_count_toward_breaker(self):
        failures: list[Any] = ["not-a-dict"] * 5 + [_failure("test_ok")]
        mock_class, posts = _mock_http_client()
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(_context(failures=failures))

        assert posts == []  # broke before any HTTP attempt
        assert result.details["skipped"] == 1
        assert result.details["pushed"] == 0


class TestSiteNormalization:
    """Site values with schemes/paths/bad hosts must not leak the API key."""

    def test_plain_site_unchanged(self):
        assert _exporter(site="datadoghq.eu")._site == "datadoghq.eu"

    def test_scheme_stripped_to_hostname(self):
        exporter = _exporter(site="https://datadoghq.eu")
        assert exporter._site == "datadoghq.eu"
        assert exporter._events_url == _EVENTS_URL

    def test_api_prefix_stripped(self):
        assert _exporter(site="api.datadoghq.eu")._site == "datadoghq.eu"
        assert _exporter(site="https://api.datadoghq.eu")._site == "datadoghq.eu"

    def test_trailing_slash_stripped(self):
        assert _exporter(site="datadoghq.eu/")._site == "datadoghq.eu"

    def test_uppercase_lowered(self):
        assert _exporter(site="DDOG.US")._site == "ddog.us"

    def test_path_only_value_parsed_as_url(self):
        exporter = _exporter(site="datadoghq.eu/some/path")
        assert exporter._site == "datadoghq.eu"
        assert exporter._events_url == _EVENTS_URL

    def test_garbage_site_rejected(self):
        with (
            patch("rootcoz.exporters.datadog.logger") as mock_logger,
            pytest.raises(ValueError, match="Invalid Datadog site"),
        ):
            _exporter(site="not a site!!")
        assert mock_logger.warning.called

    def test_no_dot_rejected(self):
        with pytest.raises(ValueError, match="Invalid Datadog site"):
            _exporter(site="localhost")

    def test_empty_site_rejected(self):
        with pytest.raises(ValueError, match="Invalid Datadog site"):
            _exporter(site="  ")

    def test_unset_site_rejected(self):
        with pytest.raises(ValueError, match="Invalid Datadog site"):
            _exporter(site="")


class TestAutoPushAnalysisTimestamp:
    """The auto-push path must emit the stored analysis timestamp.

    Same ``timestamp`` as the manual push endpoint for the same analysis —
    verified end-to-end through ``_auto_review_matching_failures`` with the
    DB-touching collaborators mocked (hermetic).
    """

    @staticmethod
    def _settings() -> Settings:
        return Settings(
            enable_auto_review=True,
            auto_push_exporters="datadog",
            datadog_api_key=_API_KEY,
            datadog_app_key=_APP_KEY,
            datadog_site="datadoghq.eu",
        )

    @staticmethod
    def _result_data() -> dict[str, Any]:
        return {
            "job_name": "my-job",
            "build_number": 101,
            "failures": [
                {
                    "test_name": "test_a",
                    "error": "err",
                    "error_signature": "sig-a",
                    "analysis": {"classification": "PRODUCT BUG"},
                }
            ],
        }

    async def test_auto_push_threads_stored_analysis_timestamp(self):
        from rootcoz.main import _auto_review_matching_failures

        stored = {
            "job_id": "current-job",
            "completed_at": "2026-01-01T00:00:00",
            "analysis_started_at": None,
            "created_at": None,
        }
        expected = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())

        with (
            patch(
                "rootcoz.main._match_and_auto_review_failures",
                new=AsyncMock(return_value=(1, 1)),
            ),
            patch("rootcoz.main.notify_job_status_changed"),
            patch.object(
                storage,
                "get_reviews_for_job",
                new=AsyncMock(
                    return_value={
                        "test_a": {
                            "reviewed": True,
                            "username": AI_SYSTEM_USERNAME,
                        }
                    }
                ),
            ),
            patch(
                "rootcoz.main.get_result", new=AsyncMock(return_value=stored)
            ) as mock_get_result,
            patch(
                "rootcoz.main._build_export_context", new_callable=AsyncMock
            ) as mock_build_context,
            patch("rootcoz.main._create_exporter"),
        ):
            await _auto_review_matching_failures(
                "current-job", "my-job", 101, self._result_data(), self._settings()
            )

        mock_get_result.assert_awaited_once_with("current-job")
        assert mock_build_context.call_args.kwargs["analysis_timestamp"] == expected

    async def test_auto_push_timestamp_none_when_job_not_stored(self):
        from rootcoz.main import _auto_review_matching_failures

        with (
            patch(
                "rootcoz.main._match_and_auto_review_failures",
                new=AsyncMock(return_value=(1, 1)),
            ),
            patch("rootcoz.main.notify_job_status_changed"),
            patch.object(
                storage,
                "get_reviews_for_job",
                new=AsyncMock(
                    return_value={
                        "test_a": {
                            "reviewed": True,
                            "username": AI_SYSTEM_USERNAME,
                        }
                    }
                ),
            ),
            patch("rootcoz.main.get_result", new=AsyncMock(return_value=None)),
            patch(
                "rootcoz.main._build_export_context", new_callable=AsyncMock
            ) as mock_build_context,
            patch("rootcoz.main._create_exporter"),
        ):
            await _auto_review_matching_failures(
                "current-job", "my-job", 101, self._result_data(), self._settings()
            )

        assert mock_build_context.call_args.kwargs["analysis_timestamp"] is None
