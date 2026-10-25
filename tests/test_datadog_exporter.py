"""Tests for the Datadog Events exporter (issue #359, VMEDO-1256/1257).

Hermetic: httpx.AsyncClient is mocked everywhere — no real network calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from rootcoz.exporters.base import ExportContext
from rootcoz.exporters.datadog import DatadogExporter, sanitize_tag_value
from rootcoz.storage import AI_SYSTEM_USERNAME

_API_KEY = "dd-test-key"  # pragma: allowlist secret
_EVENTS_URL = "https://api.datadoghq.eu/api/v1/events"


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
    params: dict[str, Any] = {"api_key": _API_KEY}
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


class TestBuildEvent:
    """Payload shape for a single event."""

    def _event(self, **ctx_overrides: Any) -> dict[str, Any]:
        exporter = _exporter()
        return exporter._build_event(_context(**ctx_overrides), _failure())

    def test_title_format(self):
        assert self._event()["title"] == "[PRODUCT BUG] test_login_flow (Build #42)"

    def test_fixed_fields(self):
        event = self._event()
        assert event["priority"] == "normal"
        assert event["source_type_name"] == "rootcoz"
        assert event["date_happened"] == 1735689600

    def test_aggregation_key(self):
        assert self._event()["aggregation_key"] == "rootcoz-job-123-test_login_flow"

    def test_tags_shape(self):
        event = self._event()
        assert event["tags"] == [
            "service:cnv-qe-tests",
            "env:production",
            "tfa.stage:automated",
            "tfa.classification:PRODUCT_BUG",
            "job_name:e2e-suite",
            "build_number:42",
            "test_name:test_login_flow",
            "rootcoz.analysis_id:job-123",
            "pushed_by:alice",
        ]

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
        event = exporter._build_event(_context(), _failure())
        assert "service:my_svc" in event["tags"]
        assert "env:staging_eu" in event["tags"]

    def test_alert_type_mapping(self):
        cases = {
            "PRODUCT BUG": "error",
            "INFRASTRUCTURE": "warning",
            "CODE ISSUE": "warning",
            "AUTOMATION": "warning",
            "UNKNOWN": "info",
            "SOMETHING ELSE": "info",
            "": "info",
        }
        for classification, expected in cases.items():
            exporter = _exporter()
            failure = _failure(classification=classification)
            event = exporter._build_event(_context(), failure)
            assert event["alert_type"] == expected, classification

    def test_classification_verbatim_in_title_and_text(self):
        exporter = _exporter()
        event = exporter._build_event(_context(), _failure())
        assert "[PRODUCT BUG]" in event["title"]
        assert "**Classification:** PRODUCT BUG" in event["text"]

    def test_text_body_contents(self):
        event = self._event()
        text = event["text"]
        assert "### test_login_flow" in text
        assert "**Job:** e2e-suite (#42)" in text
        assert "https://rootcoz.example.com/results/job-123" in text
        assert "https://jenkins.example.com/job/e2e-suite/42/" in text
        assert "Session expired before token refresh." in text

    def test_text_body_without_links(self):
        context = _context()
        context.report_url = ""
        context.jenkins_url = ""
        exporter = _exporter()
        text = exporter._build_event(context, _failure())["text"]
        assert "RootCoz report" not in text
        assert "CI build" not in text

    def test_text_body_without_details(self):
        exporter = _exporter()
        failure = _failure(details="")
        assert "AI diagnosis" not in exporter._build_event(_context(), failure)["text"]

    def test_missing_analysis_dict_tolerated(self):
        exporter = _exporter()
        event = exporter._build_event(
            _context(), {"test_name": "test_x", "error": "boom"}
        )
        assert event["title"] == "[UNKNOWN] test_x (Build #42)"
        assert event["alert_type"] == "info"


class TestPush:
    async def test_empty_failures(self):
        result = await _exporter().push(_context())
        assert result.success is False
        assert result.details == {"pushed": 0, "total": 0, "errors": []}

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
        assert [p["json"]["title"] for p in posts] == [
            "[PRODUCT BUG] test_a (Build #42)",
            "[INFRASTRUCTURE] test_b (Build #42)",
            "[CODE ISSUE] test_c (Build #42)",
        ]
        assert [p["json"]["aggregation_key"] for p in posts] == [
            "rootcoz-job-123-test_a",
            "rootcoz-job-123-test_b",
            "rootcoz-job-123-test_c",
        ]

    async def test_posts_to_site_events_url_with_api_key_header(self):
        mock_class, posts = _mock_http_client()
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            await _exporter().push(_context(failures=[_failure()]))

        assert posts[0]["url"] == _EVENTS_URL
        assert posts[0]["headers"] == {"DD-API-KEY": _API_KEY}
        assert posts[0]["json"]["source_type_name"] == "rootcoz"

    async def test_custom_site(self):
        exporter = _exporter(site="ddog.us")
        mock_class, posts = _mock_http_client()
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            await exporter.push(_context(failures=[_failure()]))
        assert posts[0]["url"] == "https://api.ddog.us/api/v1/events"

    async def test_http_error_recorded_not_raised(self):
        response = MagicMock()
        response.status_code = 403
        error = httpx.HTTPStatusError(
            "Forbidden", request=MagicMock(), response=response
        )
        mock_class, _posts = _mock_http_client(responses=[error])
        with patch("rootcoz.exporters.datadog.httpx.AsyncClient", mock_class):
            result = await _exporter().push(_context(failures=[_failure()]))

        assert result.success is False
        assert result.details["pushed"] == 0
        assert "test_login_flow" in result.details["errors"][0]
        assert "HTTP 403" in result.details["errors"][0]

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
        assert posts[0]["json"]["date_happened"] == 12345


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

    def test_create_datadog_exporter(self):
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.datadog_enabled = True
        settings.datadog_api_key.get_secret_value.return_value = _API_KEY
        settings.datadog_site = "datadoghq.eu"
        settings.datadog_service = "cnv-qe-tests"
        settings.datadog_env = "production"

        exporter = _create_exporter("datadog", settings)
        assert isinstance(exporter, DatadogExporter)

    def test_create_datadog_disabled_raises(self):
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.datadog_enabled = False
        with pytest.raises(ValueError, match="disabled"):
            _create_exporter("datadog", settings)

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
        assert settings.datadog_site == "datadoghq.eu"
        assert settings.datadog_service == "cnv-qe-tests"
        assert settings.datadog_env == "production"
        assert settings.enable_datadog is False
        assert settings.datadog_enabled is False

    def test_enabled_requires_api_key(self, monkeypatch):
        from rootcoz.config import Settings

        monkeypatch.setenv("ENABLE_DATADOG", "true")
        assert Settings().datadog_enabled is False  # no API key

        monkeypatch.setenv("DATADOG_API_KEY", "dd-key")  # pragma: allowlist secret
        assert Settings().datadog_enabled is True

    def test_disabled_ignores_api_key(self, monkeypatch):
        from rootcoz.config import Settings

        monkeypatch.setenv("DATADOG_API_KEY", "dd-key")  # pragma: allowlist secret
        assert Settings().datadog_enabled is False

    def test_blank_api_key_is_none(self, monkeypatch):
        from rootcoz.config import Settings

        monkeypatch.setenv("ENABLE_DATADOG", "true")
        monkeypatch.setenv("DATADOG_API_KEY", "  ")
        settings = Settings()
        assert settings.datadog_api_key is None
        assert settings.datadog_enabled is False

    def test_settings_category_and_sensitive_registration(self):
        from rootcoz.main import _FIELD_TO_CATEGORY, _SENSITIVE_SETTINGS

        assert "Datadog" == _FIELD_TO_CATEGORY["datadog_api_key"]
        assert _FIELD_TO_CATEGORY["enable_datadog"] == "Datadog"
        assert "datadog_api_key" in _SENSITIVE_SETTINGS
