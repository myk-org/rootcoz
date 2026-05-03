"""Tests for Report Portal CLI commands."""

import httpx
import pytest

from jenkins_job_insight.cli.client import JJIError
from tests.conftest import make_test_client as _make_client


class TestPushReportPortal:
    """Tests for push_reportportal client method."""

    def test_push_reportportal_success(self):
        result = {
            "pushed": 3,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert "/results/job-123/push-reportportal" in str(request.url)
            return httpx.Response(200, json=result)

        client = _make_client(handler)
        data = client.push_reportportal("job-123")
        assert data == result

    def test_push_reportportal_rp_disabled(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"detail": "Report Portal is disabled"})

        client = _make_client(handler)
        with pytest.raises(JJIError) as exc_info:
            client.push_reportportal("job-123")
        assert exc_info.value.status_code == 400

    def test_push_reportportal_job_not_found(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Job not found"})

        client = _make_client(handler)
        with pytest.raises(JJIError) as exc_info:
            client.push_reportportal("job-123")
        assert exc_info.value.status_code == 404
