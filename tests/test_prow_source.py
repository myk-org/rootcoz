"""Tests for the Prow CI source plugin."""

from __future__ import annotations

import json

import httpx
import pytest

from rootcoz.models import UnifiedAnalyzeRequest
from rootcoz.sources.prow_source import (

    GCS_BASE_URL,
    GCSAccessError,
    GCSOversizeError,
    ProwSource,
    _MAX_SIZE_BUILD_LOG,
    _MAX_SIZE_FINISHED,
    _MAX_SIZE_JUNIT_XML,
    _build_url,
    _fetch_gcs_text,
    _raise_if_oversize,
    _gcs_url,
    _list_gcs_junit_files,
    _parse_junit_failures,
)

# ---------------------------------------------------------------------------
# Test constants (mirrors Settings defaults; tests must not import them)
# ---------------------------------------------------------------------------
_TEST_GCS_BUCKET = "test-platform-results"
_TEST_PROW_URL = "https://prow.ci.openshift.org"

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

FINISHED_JSON_SUCCESS = json.dumps({"result": "SUCCESS", "timestamp": 1700000000})
FINISHED_JSON_FAILURE = json.dumps({"result": "FAILURE", "timestamp": 1700000000})
FINISHED_JSON_ABORTED = json.dumps({"result": "ABORTED", "timestamp": 1700000000})

JUNIT_XML_WITH_FAILURES = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="e2e" tests="3" failures="2" errors="0">
  <testcase classname="e2e.aws" name="test_create_cluster" time="120.5">
    <failure message="cluster creation timed out">
      TimeoutError: cluster creation timed out after 30m
      at test_create_cluster:42
    </failure>
  </testcase>
  <testcase classname="e2e.aws" name="test_destroy_cluster" time="60.0">
    <failure message="cluster not found">
      NotFoundError: cluster not found
    </failure>
  </testcase>
  <testcase classname="e2e.aws" name="test_list_clusters" time="5.0"/>
</testsuite>
"""

JUNIT_XML_NO_FAILURES = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="e2e" tests="1" failures="0" errors="0">
  <testcase classname="e2e.aws" name="test_list_clusters" time="5.0"/>
</testsuite>
"""

BUILD_LOG = "level=info msg='Build started'\nlevel=error msg='Pod crashed'\nlevel=info msg='Build finished'"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestGcsUrl:
    def test_basic(self):
        url = _gcs_url("my-bucket", "logs", "job-1", "123")
        assert url == f"{GCS_BASE_URL}/my-bucket/logs/job-1/123"

    def test_single_part(self):
        url = _gcs_url("bucket", "file.txt")
        assert url == f"{GCS_BASE_URL}/bucket/file.txt"


class TestBuildUrl:
    def test_default_prefix(self):
        url = _build_url(_TEST_PROW_URL, _TEST_GCS_BUCKET, "logs/my-job/123")
        assert url == f"{_TEST_PROW_URL}/view/gs/{_TEST_GCS_BUCKET}/logs/my-job/123"

    def test_custom_prow_url_trailing_slash(self):
        url = _build_url("https://prow.example.com/", _TEST_GCS_BUCKET, "logs/my-job/456")
        assert url == f"https://prow.example.com/view/gs/{_TEST_GCS_BUCKET}/logs/my-job/456"

    def test_custom_bucket(self):
        url = _build_url(_TEST_PROW_URL, "custom-bucket", "logs/my-job/123")
        assert url == f"{_TEST_PROW_URL}/view/gs/custom-bucket/logs/my-job/123"

    def test_pr_logs_prefix(self):
        prefix = "pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/2072319655766134784"
        url = _build_url(_TEST_PROW_URL, _TEST_GCS_BUCKET, prefix)
        assert url == f"{_TEST_PROW_URL}/view/gs/{_TEST_GCS_BUCKET}/{prefix}"


class TestParseJunitFailures:
    def test_extracts_failures(self):
        failures = _parse_junit_failures(JUNIT_XML_WITH_FAILURES)
        assert len(failures) == 2
        names = {f.test_name for f in failures}
        assert "e2e.aws.test_create_cluster" in names
        assert "e2e.aws.test_destroy_cluster" in names

    def test_no_failures(self):
        failures = _parse_junit_failures(JUNIT_XML_NO_FAILURES)
        assert failures == []

    def test_malformed_xml(self):
        failures = _parse_junit_failures("not xml at all")
        assert failures == []


# ---------------------------------------------------------------------------
# _fetch_gcs_text
# ---------------------------------------------------------------------------


class TestRaiseIfOversize:
    def test_below_threshold_no_raise(self):
        _raise_if_oversize("test", 100, 200, "http://example.com")  # should not raise

    def test_equal_threshold_no_raise(self):
        _raise_if_oversize("test", 200, 200, "http://example.com")  # should not raise

    def test_above_threshold_raises(self):
        with pytest.raises(GCSOversizeError) as exc_info:
            _raise_if_oversize("test", 201, 200, "http://example.com")
        assert exc_info.value.size == 201
        assert exc_info.value.max_size == 200


class TestFetchGcsText:
    async def test_success(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text="hello")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_gcs_text(client, "http://example.com/file.txt")
        assert result == "hello"

    async def test_404_returns_none(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(404)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_gcs_text(client, "http://example.com/missing.txt")
        assert result is None

    async def test_500_raises_gcs_access_error(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(500)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSAccessError) as exc_info:
                await _fetch_gcs_text(client, "http://example.com/error.txt", label="test")
        assert exc_info.value.status_code == 500

    async def test_403_raises_gcs_access_error(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(403)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSAccessError) as exc_info:
                await _fetch_gcs_text(client, "http://example.com/forbidden.txt", label="test")
        assert exc_info.value.status_code == 403

    async def test_oversized_content_length_raises(self):
        """Content-length exceeding max_size raises GCSOversizeError."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, text="x",
                headers={"content-length": str(_MAX_SIZE_FINISHED + 1)},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError) as exc_info:
                await _fetch_gcs_text(
                    client, "http://example.com/big.txt",
                    label="test", max_size=_MAX_SIZE_FINISHED,
                )
        assert exc_info.value.size == _MAX_SIZE_FINISHED + 1
        assert exc_info.value.max_size == _MAX_SIZE_FINISHED

    async def test_oversized_body_raises(self):
        """Body exceeding max_size raises GCSOversizeError."""
        big_text = "x" * (_MAX_SIZE_FINISHED + 100)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=big_text)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError):
                await _fetch_gcs_text(
                    client, "http://example.com/big.txt",
                    label="test", max_size=_MAX_SIZE_FINISHED,
                )

    async def test_oversized_build_log_raises(self):
        """Build-log.txt exceeding _MAX_SIZE_BUILD_LOG raises GCSOversizeError."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, text="x",
                headers={"content-length": str(_MAX_SIZE_BUILD_LOG + 1)},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError) as exc_info:
                await _fetch_gcs_text(
                    client, "http://example.com/build-log.txt",
                    label="build-log.txt", max_size=_MAX_SIZE_BUILD_LOG,
                )
        assert exc_info.value.max_size == _MAX_SIZE_BUILD_LOG

    async def test_oversized_junit_xml_raises(self):
        """JUnit XML exceeding _MAX_SIZE_JUNIT_XML raises GCSOversizeError."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, text="x",
                headers={"content-length": str(_MAX_SIZE_JUNIT_XML + 1)},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError) as exc_info:
                await _fetch_gcs_text(
                    client, "http://example.com/junit.xml",
                    label="junit.xml", max_size=_MAX_SIZE_JUNIT_XML,
                )
        assert exc_info.value.max_size == _MAX_SIZE_JUNIT_XML

    async def test_content_length_non_numeric_ignored(self):
        """Non-numeric content-length header doesn't crash."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, text="hello",
                headers={"content-length": "unknown"},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_gcs_text(client, "http://example.com/file.txt")
        assert result == "hello"


# ---------------------------------------------------------------------------
# _list_gcs_junit_files
# ---------------------------------------------------------------------------


class TestListGcsJunitFiles:
    async def test_lists_junit_files(self):
        response_data = {
            "items": [
                {"name": "logs/job/123/artifacts/junit/junit_results.xml"},
                {"name": "logs/job/123/artifacts/other/data.json"},
                {"name": "logs/job/123/artifacts/e2e/junit_operator_e2e.xml"},
            ]
        }
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json=response_data)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            files = await _list_gcs_junit_files(
                client, "test-bucket", "logs/job/123/artifacts/"
            )
        assert len(files) == 2
        assert "logs/job/123/artifacts/junit/junit_results.xml" in files
        assert "logs/job/123/artifacts/e2e/junit_operator_e2e.xml" in files

    async def test_empty_bucket(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"items": []})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            files = await _list_gcs_junit_files(
                client, "test-bucket", "logs/job/123/artifacts/"
            )
        assert files == []

    async def test_pagination(self):
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"name": "prefix/junit_page1.xml"},
                        ],
                        "nextPageToken": "token2",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"name": "prefix/junit_page2.xml"},
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            files = await _list_gcs_junit_files(client, "bucket", "prefix/")
        assert len(files) == 2
        assert call_count == 2

    async def test_api_error_raises_gcs_access_error(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(500)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSAccessError) as exc_info:
                await _list_gcs_junit_files(
                    client, "test-bucket", "logs/job/123/artifacts/"
                )
        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# ProwSource.fetch()
# ---------------------------------------------------------------------------


def _make_gcs_handler(
    *,
    finished_json: str | None = FINISHED_JSON_FAILURE,
    build_log: str | None = BUILD_LOG,
    junit_files: list[str] | None = None,
    junit_xml: str = JUNIT_XML_WITH_FAILURES,
):
    """Build an httpx mock transport handler simulating GCS responses.

    Args:
        finished_json: Content for finished.json (None = 404).
        build_log: Content for build-log.txt (None = 404).
        junit_files: List of JUnit file names for the GCS list API.
        junit_xml: XML content returned for each JUnit file fetch.
    """
    if junit_files is None:
        junit_files = ["logs/my-job/42/artifacts/junit/junit_results.xml"]

    def handler(request: httpx.Request):
        url = str(request.url)

        # GCS list API
        if "/storage/v1/b/" in url:
            items = [{"name": f} for f in junit_files]
            return httpx.Response(200, json={"items": items})

        # finished.json
        if url.endswith("finished.json"):
            if finished_json is None:
                return httpx.Response(404)
            return httpx.Response(200, text=finished_json)

        # build-log.txt
        if url.endswith("build-log.txt"):
            if build_log is None:
                return httpx.Response(404)
            return httpx.Response(200, text=build_log)

        # JUnit XML files
        if url.endswith(".xml"):
            return httpx.Response(200, text=junit_xml)

        return httpx.Response(404)

    return handler


class TestProwSourceFetch:
    async def test_fetch_extracts_failures(self):
        handler = _make_gcs_handler()
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert len(result.failures) == 2
        assert not result.build_passed
        assert result.build_url == _build_url(_TEST_PROW_URL, _TEST_GCS_BUCKET, "logs/my-job/42")

    async def test_fetch_build_passed(self):
        handler = _make_gcs_handler(finished_json=FINISHED_JSON_SUCCESS)
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.build_passed is True
        assert result.failures == []

    async def test_fetch_build_passed_force(self):
        handler = _make_gcs_handler(finished_json=FINISHED_JSON_SUCCESS)
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL, force=True)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.build_passed is False
        assert len(result.failures) == 2

    async def test_fetch_no_finished_json(self):
        """When finished.json is missing, analysis continues (job may be running)."""
        handler = _make_gcs_handler(finished_json=None)
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert not result.build_passed
        assert len(result.failures) == 2

    async def test_fetch_no_build_log(self):
        handler = _make_gcs_handler(build_log=None)
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.console_context == ""
        assert len(result.failures) == 2

    async def test_fetch_no_junit_files(self):
        handler = _make_gcs_handler(junit_files=[])
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.failures == []
        assert not result.build_passed

    async def test_fetch_gcs_errors_produce_warnings(self):
        """Non-404 GCS errors are tracked as warnings."""

        def error_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            # JUnit listing returns empty
            if "/storage/v1/b/" in url:
                return httpx.Response(200, json={"items": []})
            # finished.json returns 403
            if "finished.json" in url:
                return httpx.Response(403)
            # build-log.txt returns 500
            if "build-log.txt" in url:
                return httpx.Response(500)
            return httpx.Response(404)

        transport = httpx.MockTransport(error_handler)
        source = ProwSource(
            job_name="my-job", build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert len(result.warnings) == 2
        assert any("403" in w for w in result.warnings)
        assert any("500" in w for w in result.warnings)
        assert result.failures == []

    async def test_fetch_multiple_junit_files(self):
        handler = _make_gcs_handler(
            junit_files=[
                "logs/my-job/42/artifacts/junit/junit_01.xml",
                "logs/my-job/42/artifacts/junit/junit_02.xml",
            ],
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        # 2 failures per file × 2 files = 4 failures
        assert len(result.failures) == 4

    async def test_fetch_aborted_build(self):
        handler = _make_gcs_handler(finished_json=FINISHED_JSON_ABORTED)
        transport = httpx.MockTransport(handler)
        source = ProwSource(job_name="my-job", build_id="42", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert not result.build_passed

    async def test_custom_gcs_bucket(self):
        requests_seen: list[str] = []

        def handler(request: httpx.Request):
            requests_seen.append(str(request.url))
            if "/storage/v1/b/" in str(request.url):
                return httpx.Response(200, json={"items": []})
            if str(request.url).endswith("finished.json"):
                return httpx.Response(200, text=FINISHED_JSON_FAILURE)
            if str(request.url).endswith("build-log.txt"):
                return httpx.Response(404)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job", build_id="42", gcs_bucket="custom-bucket", prow_url=_TEST_PROW_URL
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert any("custom-bucket" in url for url in requests_seen)

    async def test_custom_prow_url(self):
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url="https://prow.custom.org",
        )
        assert "prow.custom.org" in source.build_url


# ---------------------------------------------------------------------------
# UnifiedAnalyzeRequest prow validation
# ---------------------------------------------------------------------------


class TestUnifiedAnalyzeRequestProw:
    def test_valid_prow_request(self):
        req = UnifiedAnalyzeRequest(
            type="prow",
            prow_job_name="periodic-ci-e2e-aws",
            build_id="1234567890",
        )
        assert req.type == "prow"
        assert req.prow_job_name == "periodic-ci-e2e-aws"
        assert req.build_id == "1234567890"
        assert req.prow_url == ""  # empty = use server default
        assert req.gcs_bucket == ""  # empty = use server default

    def test_prow_missing_job_name(self):
        with pytest.raises(ValueError, match="prow_job_name is required"):
            UnifiedAnalyzeRequest(type="prow", build_id="123")

    def test_prow_missing_build_id(self):
        with pytest.raises(ValueError, match="build_id is required"):
            UnifiedAnalyzeRequest(type="prow", prow_job_name="my-job")

    def test_prow_custom_url_and_bucket(self):
        req = UnifiedAnalyzeRequest(
            type="prow",
            prow_job_name="my-job",
            build_id="42",
            prow_url="https://prow.custom.org",
            gcs_bucket="custom-bucket",
        )
        assert req.prow_url == "https://prow.custom.org"
        assert req.gcs_bucket == "custom-bucket"

    def test_prow_job_name_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            UnifiedAnalyzeRequest(type="prow", prow_job_name="../etc/passwd", build_id="1")

    def test_prow_job_name_rejects_slashes(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            UnifiedAnalyzeRequest(type="prow", prow_job_name="job/name", build_id="1")

    def test_prow_job_name_allows_hyphens_dots_underscores(self):
        req = UnifiedAnalyzeRequest(
            type="prow",
            prow_job_name="periodic-ci-openshift_release-4.17-e2e.aws",
            build_id="123",
        )
        assert req.prow_job_name == "periodic-ci-openshift_release-4.17-e2e.aws"

    def test_build_id_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="numeric"):
            UnifiedAnalyzeRequest(type="prow", prow_job_name="my-job", build_id="abc")

    def test_build_id_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="numeric"):
            UnifiedAnalyzeRequest(type="prow", prow_job_name="my-job", build_id="../123")

    def test_gcs_bucket_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="1",
                gcs_bucket="../evil-bucket",
            )

    def test_gcs_bucket_rejects_uppercase(self):
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="1",
                gcs_bucket="MyBucket",
            )

    def test_prow_url_rejects_http(self):
        with pytest.raises(ValueError, match="https://"):
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="1",
                prow_url="http://prow.example.com",
            )

    def test_prow_url_rejects_non_url(self):
        with pytest.raises(ValueError, match="https://"):
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="1",
                prow_url="not-a-url",
            )

    def test_gcs_prefix_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="must not contain"):
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="1",
                gcs_prefix="../../other-bucket/sensitive-data",
            )

    def test_gcs_prefix_rejects_dotdot_in_middle(self):
        with pytest.raises(ValueError, match="must not contain"):
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="1",
                gcs_prefix="logs/my-job/../../../etc/passwd",
            )

    def test_gcs_prefix_rejects_invalid_chars(self):
        with pytest.raises(ValueError, match="invalid characters"):
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="1",
                gcs_prefix="logs/my job/with spaces",
            )

    def test_gcs_prefix_accepts_valid_pr_logs(self):
        req = UnifiedAnalyzeRequest(
            type="prow", prow_job_name="my-job", build_id="1",
            gcs_prefix="pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/123",
        )
        assert req.gcs_prefix == "pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/123"

    def test_gcs_prefix_accepts_valid_logs(self):
        req = UnifiedAnalyzeRequest(
            type="prow", prow_job_name="my-job", build_id="1",
            gcs_prefix="logs/periodic-ci-e2e/999",
        )
        assert req.gcs_prefix == "logs/periodic-ci-e2e/999"


class TestProwSourceProperties:
    def test_build_url(self):
        source = ProwSource(job_name="test-job", build_id="999", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        expected = f"{_TEST_PROW_URL}/view/gs/{_TEST_GCS_BUCKET}/logs/test-job/999"
        assert source.build_url == expected

    def test_gcs_prefix(self):
        source = ProwSource(job_name="test-job", build_id="999", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        assert source._gcs_prefix == "logs/test-job/999"

    def test_custom_gcs_prefix(self):
        """Custom gcs_prefix overrides the default logs/ pattern."""
        prefix = "pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/2072319655766134784"
        source = ProwSource(
            job_name="pull-kubevirt-fuzz",
            build_id="2072319655766134784",
            gcs_bucket="kubevirt-prow",
            prow_url="https://prow.ci.kubevirt.io",
            gcs_prefix=prefix,
        )
        assert source._gcs_prefix == prefix
        assert "pr-logs/pull/" in source.build_url

    def test_empty_gcs_prefix_uses_default(self):
        source = ProwSource(
            job_name="my-job", build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL,
            gcs_prefix="",
        )
        assert source._gcs_prefix == "logs/my-job/42"

    def test_no_child_source(self):
        """ProwSource doesn't support child jobs."""
        source = ProwSource(job_name="test-job", build_id="999", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        assert source.create_child_source("child", 1) is None

    def test_cleanup_noop(self):
        """ProwSource cleanup is a no-op (no temp files)."""
        source = ProwSource(job_name="test-job", build_id="999", gcs_bucket=_TEST_GCS_BUCKET, prow_url=_TEST_PROW_URL)
        source.cleanup()  # should not raise
