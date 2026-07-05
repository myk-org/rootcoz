"""Tests for the Prow CI source plugin."""

from __future__ import annotations

import json

import httpx
import pytest

from rootcoz.main import _fetch_pr_changes, _format_prow_context
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
    _gcs_url,
    _list_gcs_junit_files,
    _parse_junit_failures,
    _parse_prowjob_json,
    _raise_if_oversize,
)

pytestmark = pytest.mark.anyio


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

PROWJOB_JSON_PRESUBMIT = json.dumps(
    {
        "spec": {
            "type": "presubmit",
            "job": "pull-kubevirt-e2e",
            "refs": {
                "org": "kubevirt",
                "repo": "kubevirt",
                "base_ref": "main",
                "pulls": [{"number": 17509, "author": "dev-user", "sha": "abc123"}],
            },
        },
        "status": {"state": "failure"},
    }
)

PROWJOB_JSON_PERIODIC = json.dumps(
    {
        "spec": {
            "type": "periodic",
            "job": "periodic-ci-e2e",
            "refs": {
                "org": "kubevirt",
                "repo": "kubevirt",
                "base_ref": "main",
            },
        },
        "status": {"state": "failure"},
    }
)

PROWJOB_JSON_SUCCESS = json.dumps(
    {
        "spec": {
            "type": "periodic",
            "job": "periodic-ci-e2e",
            "refs": {
                "org": "kubevirt",
                "repo": "kubevirt",
                "base_ref": "main",
            },
        },
        "status": {"state": "success"},
    }
)


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
        url = _build_url(
            "https://prow.example.com/", _TEST_GCS_BUCKET, "logs/my-job/456"
        )
        assert (
            url
            == f"https://prow.example.com/view/gs/{_TEST_GCS_BUCKET}/logs/my-job/456"
        )

    def test_custom_bucket(self):
        url = _build_url(_TEST_PROW_URL, "custom-bucket", "logs/my-job/123")
        assert url == f"{_TEST_PROW_URL}/view/gs/custom-bucket/logs/my-job/123"

    def test_pr_logs_prefix(self):
        prefix = "pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/2072319655766134784"  # pragma: allowlist secret
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


class TestParseProwjobJson:
    def test_presubmit_metadata(self):
        meta = _parse_prowjob_json(PROWJOB_JSON_PRESUBMIT)
        assert meta is not None
        assert meta.job_type == "presubmit"
        assert meta.org == "kubevirt"
        assert meta.repo == "kubevirt"
        assert meta.base_ref == "main"
        assert meta.pr_number == 17509
        assert meta.pr_author == "dev-user"
        assert meta.state == "failure"

    def test_periodic_metadata(self):
        meta = _parse_prowjob_json(PROWJOB_JSON_PERIODIC)
        assert meta is not None
        assert meta.job_type == "periodic"
        assert meta.pr_number is None
        assert meta.pr_author == ""
        assert meta.state == "failure"

    def test_invalid_json_returns_none(self):
        assert _parse_prowjob_json("not json") is None

    def test_empty_json_returns_defaults(self):
        meta = _parse_prowjob_json("{}")
        assert meta is not None
        assert meta.job_type == ""
        assert meta.state == ""

    def test_missing_refs_handled(self):
        meta = _parse_prowjob_json(
            json.dumps({"spec": {"type": "periodic"}, "status": {"state": "failure"}})
        )
        assert meta is not None
        assert meta.org == ""
        assert meta.repo == ""
        assert meta.pr_number is None


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
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text="hello"))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_gcs_text(client, "http://example.com/file.txt")
        assert result == "hello"

    async def test_404_returns_none(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(404))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_gcs_text(client, "http://example.com/missing.txt")
        assert result is None

    async def test_500_raises_gcs_access_error(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSAccessError) as exc_info:
                await _fetch_gcs_text(
                    client, "http://example.com/error.txt", label="test"
                )
        assert exc_info.value.status_code == 500

    async def test_403_raises_gcs_access_error(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(403))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSAccessError) as exc_info:
                await _fetch_gcs_text(
                    client, "http://example.com/forbidden.txt", label="test"
                )
        assert exc_info.value.status_code == 403

    async def test_oversized_content_length_raises(self):
        """Content-length exceeding max_size raises GCSOversizeError."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                text="x",
                headers={"content-length": str(_MAX_SIZE_FINISHED + 1)},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError) as exc_info:
                await _fetch_gcs_text(
                    client,
                    "http://example.com/big.txt",
                    label="test",
                    max_size=_MAX_SIZE_FINISHED,
                )
        assert exc_info.value.size == _MAX_SIZE_FINISHED + 1
        assert exc_info.value.max_size == _MAX_SIZE_FINISHED

    async def test_oversized_body_raises(self):
        """Body exceeding max_size raises GCSOversizeError."""
        big_text = "x" * (_MAX_SIZE_FINISHED + 100)
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=big_text))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError):
                await _fetch_gcs_text(
                    client,
                    "http://example.com/big.txt",
                    label="test",
                    max_size=_MAX_SIZE_FINISHED,
                )

    async def test_oversized_build_log_raises(self):
        """Build-log.txt exceeding _MAX_SIZE_BUILD_LOG raises GCSOversizeError."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                text="x",
                headers={"content-length": str(_MAX_SIZE_BUILD_LOG + 1)},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError) as exc_info:
                await _fetch_gcs_text(
                    client,
                    "http://example.com/build-log.txt",
                    label="build-log.txt",
                    max_size=_MAX_SIZE_BUILD_LOG,
                )
        assert exc_info.value.max_size == _MAX_SIZE_BUILD_LOG

    async def test_oversized_junit_xml_raises(self):
        """JUnit XML exceeding _MAX_SIZE_JUNIT_XML raises GCSOversizeError."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                text="x",
                headers={"content-length": str(_MAX_SIZE_JUNIT_XML + 1)},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSOversizeError) as exc_info:
                await _fetch_gcs_text(
                    client,
                    "http://example.com/junit.xml",
                    label="junit.xml",
                    max_size=_MAX_SIZE_JUNIT_XML,
                )
        assert exc_info.value.max_size == _MAX_SIZE_JUNIT_XML

    async def test_content_length_non_numeric_ignored(self):
        """Non-numeric content-length header doesn't crash."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                text="hello",
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
        transport = httpx.MockTransport(lambda req: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(GCSAccessError) as exc_info:
                await _list_gcs_junit_files(
                    client, "test-bucket", "logs/job/123/artifacts/"
                )
        assert exc_info.value.status_code == 500

    async def test_pagination_truncation_appends_warning(self):
        """When pagination exceeds max_pages, partial results returned with warning."""
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            # Always return a nextPageToken to force infinite pagination
            return httpx.Response(
                200,
                json={
                    "items": [{"name": f"prefix/junit_p{call_count}.xml"}],
                    "nextPageToken": f"token-{call_count}",
                },
            )

        transport = httpx.MockTransport(handler)
        warnings: list[str] = []
        # max_pages=100 is hardcoded in _list_gcs_junit_files
        original_max = 100
        async with httpx.AsyncClient(transport=transport) as client:
            files = await _list_gcs_junit_files(
                client, "bucket", "prefix/", warnings=warnings
            )
        assert call_count == original_max
        assert len(files) == original_max  # one file per page
        assert len(warnings) == 1
        assert "exceeded" in warnings[0]
        assert "truncated" in warnings[0]


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

        # prowjob.json — return 404 to fall through to finished.json
        if url.endswith("prowjob.json"):
            return httpx.Response(404)

        # directory pointer — return 404 for default handler
        if "pr-logs/directory/" in url:
            return httpx.Response(404)

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


def _make_pointer_handler(
    *,
    pointer_response: httpx.Response | None = None,
    prowjob_response: httpx.Response | None = None,
    finished_json: str = FINISHED_JSON_FAILURE,
    build_log: str | None = None,
    capture: list[str] | None = None,
):
    """Build a mock handler that supports prowjob.json + directory pointer resolution.

    Args:
        pointer_response: Response for pointer file requests (None = 404).
        prowjob_response: Response for prowjob.json requests (None = 404).
        finished_json: Content for finished.json.
        build_log: Content for build-log.txt (None = 404).
        capture: Optional list to capture all request URLs.
    """

    def handler(request: httpx.Request):
        url = str(request.url)
        if capture is not None:
            capture.append(url)
        if url.endswith("prowjob.json"):
            return prowjob_response or httpx.Response(404)
        if "pr-logs/directory/" in url:
            return pointer_response or httpx.Response(404)
        if "/storage/v1/b/" in url:
            return httpx.Response(200, json={"items": []})
        if url.endswith("finished.json"):
            return httpx.Response(200, text=finished_json)
        if url.endswith("build-log.txt"):
            if build_log is None:
                return httpx.Response(404)
            return httpx.Response(200, text=build_log)
        return httpx.Response(404)

    return handler


class TestGcsPrefixResolution:
    """Tests for automatic GCS prefix resolution via prowjob.json and directory pointer files."""

    async def test_periodic_job_resolved_via_prowjob_json(self):
        """prowjob.json at default path confirms periodic job path and provides metadata."""
        handler = _make_pointer_handler(
            prowjob_response=httpx.Response(200, text=PROWJOB_JSON_PERIODIC),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="periodic-ci-e2e",
            build_id="456",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == "logs/periodic-ci-e2e/456"
        assert source._prowjob_metadata is not None
        assert source._prowjob_metadata.job_type == "periodic"

    async def test_pr_job_resolved_via_pointer_file(self):
        """When prowjob.json 404s at default path, pointer file resolves PR job prefix."""
        pr_prefix = "pr-logs/pull/kubevirt_kubevirt/17509/pull-kubevirt-e2e/123"
        pointer_content = f"gs://kubevirt-prow/{pr_prefix}"
        handler = _make_pointer_handler(
            pointer_response=httpx.Response(200, text=pointer_content),
            # prowjob.json 404 at default path but available at resolved path
            prowjob_response=httpx.Response(404),  # 404 at default logs/ path
        )
        # Need custom handler for prowjob.json at resolved path
        base_handler = handler

        def smart_handler(request: httpx.Request):
            url = str(request.url)
            # prowjob.json at resolved path should succeed
            if f"kubevirt-prow/{pr_prefix}/prowjob.json" in url:
                return httpx.Response(200, text=PROWJOB_JSON_PRESUBMIT)
            return base_handler(request)

        transport = httpx.MockTransport(smart_handler)
        source = ProwSource(
            job_name="pull-kubevirt-e2e",
            build_id="123",
            gcs_bucket="kubevirt-prow",
            prow_url="https://prow.ci.kubevirt.io",
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == pr_prefix
        assert "pr-logs/pull/" in source.build_url
        assert source._prowjob_metadata is not None
        assert source._prowjob_metadata.job_type == "presubmit"
        assert source._prowjob_metadata.pr_number == 17509

    async def test_missing_prowjob_and_pointer_falls_back_to_default(self):
        """When both prowjob.json and pointer file 404, falls back to logs/ default."""
        handler = _make_pointer_handler()  # both default to 404
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="456",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == "logs/my-job/456"
        assert source._prowjob_metadata is None

    async def test_explicit_prefix_skips_resolution(self):
        """Explicit gcs_prefix skips both prowjob.json and pointer file checks."""
        requests_seen: list[str] = []
        handler = _make_pointer_handler(capture=requests_seen)
        transport = httpx.MockTransport(handler)
        custom_prefix = "pr-logs/pull/org_repo/99/my-job/789"
        source = ProwSource(
            job_name="my-job",
            build_id="789",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
            gcs_prefix=custom_prefix,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert not any("pr-logs/directory/" in url for url in requests_seen)
        assert source._gcs_prefix == custom_prefix

    async def test_explicit_prefix_fetches_metadata(self):
        """Explicit gcs_prefix still fetches prowjob.json for metadata."""
        requests_seen: list[str] = []
        custom_prefix = "pr-logs/pull/org_repo/99/my-job/789"

        def handler(request: httpx.Request):
            url = str(request.url)
            requests_seen.append(url)
            if url.endswith("prowjob.json"):
                return httpx.Response(200, text=PROWJOB_JSON_PRESUBMIT)
            if "/storage/v1/b/" in url:
                return httpx.Response(200, json={"items": []})
            if url.endswith("finished.json"):
                return httpx.Response(200, text=FINISHED_JSON_FAILURE)
            if url.endswith("build-log.txt"):
                return httpx.Response(404)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="789",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
            gcs_prefix=custom_prefix,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert any("prowjob.json" in url for url in requests_seen)
        assert source._prowjob_metadata is not None
        assert source._prowjob_metadata.job_type == "presubmit"

    async def test_malformed_pointer_falls_back_gracefully(self):
        """Malformed pointer file content (wrong bucket) falls back to default."""
        handler = _make_pointer_handler(
            pointer_response=httpx.Response(
                200, text="gs://wrong-bucket/pr-logs/pull/org/1/job/123"
            ),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="123",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == "logs/my-job/123"

    async def test_pointer_with_trailing_whitespace(self):
        """Pointer file content with trailing whitespace/newline is handled."""
        handler = _make_pointer_handler(
            pointer_response=httpx.Response(
                200, text="gs://test-bucket/pr-logs/pull/org_repo/42/my-job/123\n"
            ),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="123",
            gcs_bucket="test-bucket",
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == "pr-logs/pull/org_repo/42/my-job/123"

    async def test_pointer_with_trailing_slash_stripped(self):
        """Trailing slash in pointer file content is stripped."""
        handler = _make_pointer_handler(
            pointer_response=httpx.Response(
                200, text="gs://test-bucket/pr-logs/pull/org_repo/42/my-job/123/"
            ),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="123",
            gcs_bucket="test-bucket",
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == "pr-logs/pull/org_repo/42/my-job/123"

    async def test_suspicious_pointer_content_falls_back(self):
        """Pointer with newlines or excessive length falls back to default."""
        handler = _make_pointer_handler(
            pointer_response=httpx.Response(
                200,
                text=f"gs://{_TEST_GCS_BUCKET}/pr-logs/pull/org/1/job/123\nextra-line",
            ),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="123",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == "logs/my-job/123"

    async def test_prowjob_metadata_skips_finished_json(self):
        """When prowjob.json provides status, finished.json is not fetched."""
        requests_seen: list[str] = []
        handler = _make_pointer_handler(
            prowjob_response=httpx.Response(200, text=PROWJOB_JSON_PERIODIC),
            capture=requests_seen,
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="periodic-ci-e2e",
            build_id="456",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        # finished.json should NOT have been fetched since prowjob.json provided the status
        assert not any("finished.json" in url for url in requests_seen)

    async def test_prowjob_success_skips_analysis(self):
        """When prowjob.json says SUCCESS, analysis is skipped."""
        handler = _make_pointer_handler(
            prowjob_response=httpx.Response(200, text=PROWJOB_JSON_SUCCESS),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="periodic-ci-e2e",
            build_id="456",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.build_passed is True
        assert result.failures == []
        assert result.source_metadata.get("job_type") == "periodic"

    async def test_source_metadata_included_in_result(self):
        """Source metadata is included in CISourceResult (periodic job)."""
        handler = _make_pointer_handler(
            prowjob_response=httpx.Response(200, text=PROWJOB_JSON_PERIODIC),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="periodic-ci-e2e",
            build_id="456",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.source_metadata["job_type"] == "periodic"
        assert result.source_metadata["org"] == "kubevirt"
        assert result.source_metadata["repo"] == "kubevirt"

    async def test_presubmit_at_default_path_continues_to_pointer(self):
        """Presubmit prowjob.json at logs/ path doesn't short-circuit — continues to pointer."""
        pr_prefix = "pr-logs/pull/kubevirt_kubevirt/17509/pull-kubevirt-e2e/123"
        pointer_content = f"gs://{_TEST_GCS_BUCKET}/{pr_prefix}"
        base_handler = _make_pointer_handler(
            # prowjob.json at default path says presubmit — should be ignored
            prowjob_response=httpx.Response(200, text=PROWJOB_JSON_PRESUBMIT),
            pointer_response=httpx.Response(200, text=pointer_content),
        )

        def handler(request: httpx.Request):
            url = str(request.url)
            # prowjob.json at resolved path should succeed with presubmit metadata
            if f"{_TEST_GCS_BUCKET}/{pr_prefix}/prowjob.json" in url:
                return httpx.Response(200, text=PROWJOB_JSON_PRESUBMIT)
            return base_handler(request)

        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="pull-kubevirt-e2e",
            build_id="123",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert source._gcs_prefix == pr_prefix
        assert result.source_metadata["job_type"] == "presubmit"
        assert result.source_metadata["pr_number"] == 17509


class TestProwSourceFetch:
    async def test_fetch_extracts_failures(self):
        handler = _make_gcs_handler()
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert len(result.failures) == 2
        assert not result.build_passed
        assert result.build_url == _build_url(
            _TEST_PROW_URL, _TEST_GCS_BUCKET, "logs/my-job/42"
        )

    async def test_fetch_build_passed(self):
        handler = _make_gcs_handler(finished_json=FINISHED_JSON_SUCCESS)
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.build_passed is True
        assert result.failures == []

    async def test_fetch_build_passed_force(self):
        handler = _make_gcs_handler(finished_json=FINISHED_JSON_SUCCESS)
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
            force=True,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.build_passed is False
        assert len(result.failures) == 2

    async def test_fetch_no_finished_json(self):
        """When finished.json is missing, analysis continues (job may be running)."""
        handler = _make_gcs_handler(finished_json=None)
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert not result.build_passed
        assert len(result.failures) == 2

    async def test_fetch_no_build_log(self):
        handler = _make_gcs_handler(build_log=None)
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.console_context == ""
        assert len(result.failures) == 2

    async def test_fetch_no_junit_files(self):
        handler = _make_gcs_handler(junit_files=[])
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
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
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
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
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        # 2 failures per file × 2 files = 4 failures
        assert len(result.failures) == 4

    async def test_fetch_aborted_build(self):
        handler = _make_gcs_handler(finished_json=FINISHED_JSON_ABORTED)
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
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
            job_name="my-job",
            build_id="42",
            gcs_bucket="custom-bucket",
            prow_url=_TEST_PROW_URL,
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
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="../etc/passwd", build_id="1"
            )

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
            UnifiedAnalyzeRequest(
                type="prow", prow_job_name="my-job", build_id="../123"
            )

    def test_gcs_bucket_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            UnifiedAnalyzeRequest(
                type="prow",
                prow_job_name="my-job",
                build_id="1",
                gcs_bucket="../evil-bucket",
            )

    def test_gcs_bucket_rejects_uppercase(self):
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            UnifiedAnalyzeRequest(
                type="prow",
                prow_job_name="my-job",
                build_id="1",
                gcs_bucket="MyBucket",
            )

    def test_prow_url_rejects_http(self):
        with pytest.raises(ValueError, match="https://"):
            UnifiedAnalyzeRequest(
                type="prow",
                prow_job_name="my-job",
                build_id="1",
                prow_url="http://prow.example.com",
            )

    def test_prow_url_rejects_non_url(self):
        with pytest.raises(ValueError, match="https://"):
            UnifiedAnalyzeRequest(
                type="prow",
                prow_job_name="my-job",
                build_id="1",
                prow_url="not-a-url",
            )

    def test_gcs_prefix_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="must not contain"):
            UnifiedAnalyzeRequest(
                type="prow",
                prow_job_name="my-job",
                build_id="1",
                gcs_prefix="../../other-bucket/sensitive-data",
            )

    def test_gcs_prefix_rejects_dotdot_in_middle(self):
        with pytest.raises(ValueError, match="must not contain"):
            UnifiedAnalyzeRequest(
                type="prow",
                prow_job_name="my-job",
                build_id="1",
                gcs_prefix="logs/my-job/../../../etc/passwd",
            )

    def test_gcs_prefix_rejects_invalid_chars(self):
        with pytest.raises(ValueError, match="invalid characters"):
            UnifiedAnalyzeRequest(
                type="prow",
                prow_job_name="my-job",
                build_id="1",
                gcs_prefix="logs/my job/with spaces",
            )

    def test_gcs_prefix_accepts_valid_pr_logs(self):
        req = UnifiedAnalyzeRequest(
            type="prow",
            prow_job_name="my-job",
            build_id="1",
            gcs_prefix="pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/123",
        )
        assert (
            req.gcs_prefix
            == "pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/123"
        )

    def test_gcs_prefix_accepts_valid_logs(self):
        req = UnifiedAnalyzeRequest(
            type="prow",
            prow_job_name="my-job",
            build_id="1",
            gcs_prefix="logs/periodic-ci-e2e/999",
        )
        assert req.gcs_prefix == "logs/periodic-ci-e2e/999"


class TestFormatProwContext:
    def test_presubmit_context(self):
        metadata = {
            "job_type": "presubmit",
            "org": "kubevirt",
            "repo": "kubevirt",
            "base_ref": "main",
            "pr_number": 17509,
            "pr_author": "dev-user",
            "state": "failure",
        }
        result = _format_prow_context(
            metadata, "https://prow.example.com/view/gs/bucket/path"
        )
        assert "presubmit (PR check)" in result
        assert "kubevirt/kubevirt#17509" in result
        assert "dev-user" in result
        assert "main" in result
        assert "FAILURE" in result
        assert "https://prow.example.com" in result

    def test_periodic_context(self):
        metadata = {
            "job_type": "periodic",
            "org": "kubevirt",
            "repo": "kubevirt",
            "base_ref": "main",
            "state": "failure",
        }
        result = _format_prow_context(metadata)
        assert "periodic (scheduled)" in result
        assert "kubevirt/kubevirt" in result
        assert "#" not in result  # no PR number

    def test_empty_metadata_returns_empty(self):
        assert _format_prow_context({}) == ""
        assert _format_prow_context(None) == ""


class TestProwSourceProperties:
    def test_build_url(self):
        source = ProwSource(
            job_name="test-job",
            build_id="999",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        expected = f"{_TEST_PROW_URL}/view/gs/{_TEST_GCS_BUCKET}/logs/test-job/999"
        assert source.build_url == expected

    def test_gcs_prefix(self):
        source = ProwSource(
            job_name="test-job",
            build_id="999",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        assert source._gcs_prefix == "logs/test-job/999"

    def test_custom_gcs_prefix(self):
        """Custom gcs_prefix overrides the default logs/ pattern."""
        prefix = "pr-logs/pull/kubevirt_kubevirt/17598/pull-kubevirt-fuzz/2072319655766134784"  # pragma: allowlist secret
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
            job_name="my-job",
            build_id="42",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
            gcs_prefix="",
        )
        assert source._gcs_prefix == "logs/my-job/42"

    def test_no_child_source(self):
        """ProwSource doesn't support child jobs."""
        source = ProwSource(
            job_name="test-job",
            build_id="999",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        assert source.create_child_source("child", 1) is None

    def test_cleanup_noop(self):
        """ProwSource cleanup is a no-op (no temp files)."""
        source = ProwSource(
            job_name="test-job",
            build_id="999",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        source.cleanup()  # should not raise


class TestParseProwjobJsonDefensive:
    """Tests for defensive parsing of malformed prowjob.json."""

    def test_array_json_returns_none(self):
        assert _parse_prowjob_json("[]") is None

    def test_string_json_returns_none(self):
        assert _parse_prowjob_json('"hello"') is None

    def test_number_json_returns_none(self):
        assert _parse_prowjob_json("42") is None

    def test_spec_is_array_returns_none(self):
        assert _parse_prowjob_json(json.dumps({"spec": []})) is None

    def test_refs_is_array_handled(self):
        meta = _parse_prowjob_json(
            json.dumps({"spec": {"type": "periodic", "refs": []}})
        )
        assert meta is not None
        assert meta.org == ""

    def test_pulls_contains_non_dict(self):
        data = {"spec": {"refs": {"org": "o", "repo": "r", "pulls": ["not-a-dict"]}}}
        meta = _parse_prowjob_json(json.dumps(data))
        assert meta is not None
        assert meta.pr_number is None

    def test_batch_multiple_prs_preserved(self):
        data = {
            "spec": {
                "type": "batch",
                "refs": {
                    "org": "kubevirt",
                    "repo": "kubevirt",
                    "pulls": [
                        {"number": 101, "author": "alice"},
                        {"number": 102, "author": "bob"},
                        {"number": 103, "author": "charlie"},
                    ],
                },
            },
            "status": {"state": "failure"},
        }
        meta = _parse_prowjob_json(json.dumps(data))
        assert meta is not None
        assert meta.pr_number == 101
        assert meta.pr_author == "alice"
        assert meta.additional_prs is not None
        assert len(meta.additional_prs) == 2
        assert meta.additional_prs[0]["number"] == 102
        assert meta.additional_prs[1]["author"] == "charlie"


class TestPointerValidation:
    """Tests for pointer path validation (job_name/build_id suffix check)."""

    async def test_pointer_wrong_job_name_rejected(self):
        """Pointer pointing to a different job is rejected."""
        handler = _make_pointer_handler(
            pointer_response=httpx.Response(
                200, text=f"gs://{_TEST_GCS_BUCKET}/pr-logs/pull/org/1/other-job/123"
            ),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="123",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        # Should fall back to default since job_name doesn't match
        assert source._gcs_prefix == "logs/my-job/123"

    async def test_pointer_with_path_traversal_rejected(self):
        """Pointer containing .. path traversal is rejected."""
        handler = _make_pointer_handler(
            pointer_response=httpx.Response(
                200, text=f"gs://{_TEST_GCS_BUCKET}/../other-bucket/my-job/123"
            ),
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="123",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == "logs/my-job/123"


class TestFormatProwContextBatch:
    """Tests for batch job context formatting with multiple PRs."""

    def test_batch_multiple_prs_rendered(self):
        metadata = {
            "job_type": "batch",
            "org": "kubevirt",
            "repo": "kubevirt",
            "pr_number": 101,
            "pr_author": "alice",
            "additional_prs": [
                {"number": 102, "author": "bob"},
                {"number": 103, "author": "charlie"},
            ],
            "state": "failure",
        }
        result = _format_prow_context(metadata)
        assert "batch (merge queue)" in result
        assert (
            "PRs: kubevirt/kubevirt#101, kubevirt/kubevirt#102, kubevirt/kubevirt#103"
            in result
        )
        assert "PR Authors: alice, bob, charlie" in result

    def test_single_pr_no_batch_label(self):
        metadata = {
            "job_type": "presubmit",
            "org": "kubevirt",
            "repo": "kubevirt",
            "pr_number": 101,
            "pr_author": "alice",
            "state": "failure",
        }
        result = _format_prow_context(metadata)
        assert "PR: kubevirt/kubevirt#101" in result
        assert "PR Author: alice" in result
        assert "PRs:" not in result


class TestRegressionEdgeCases:
    """Regression tests for edge cases found during peer review."""

    async def test_empty_prowjob_at_default_path_falls_through_to_pointer(self):
        """Empty prowjob.json ({}) at logs/ path doesn't confirm path — continues to pointer."""
        pr_prefix = "pr-logs/pull/kubevirt_kubevirt/42/my-job/123"
        pointer_content = f"gs://{_TEST_GCS_BUCKET}/{pr_prefix}"
        base_handler = _make_pointer_handler(
            prowjob_response=httpx.Response(200, text="{}"),
            pointer_response=httpx.Response(200, text=pointer_content),
        )

        def handler(request: httpx.Request):
            url = str(request.url)
            if f"{_TEST_GCS_BUCKET}/{pr_prefix}/prowjob.json" in url:
                return httpx.Response(200, text=PROWJOB_JSON_PRESUBMIT)
            return base_handler(request)

        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="my-job",
            build_id="123",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await source._fetch_with_client(client)

        assert source._gcs_prefix == pr_prefix

    async def test_pointer_500_with_passed_build_preserves_warning(self):
        """Non-404 pointer error + SUCCESS build preserves warning in result."""
        # prowjob.json 404 at default → pointer 500 → falls back to default
        # then finished.json returns SUCCESS
        handler = _make_pointer_handler(
            prowjob_response=httpx.Response(404),
            pointer_response=httpx.Response(500),
            finished_json=FINISHED_JSON_SUCCESS,
        )
        transport = httpx.MockTransport(handler)
        source = ProwSource(
            job_name="periodic-ci-e2e",
            build_id="456",
            gcs_bucket=_TEST_GCS_BUCKET,
            prow_url=_TEST_PROW_URL,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await source._fetch_with_client(client)

        assert result.build_passed is True
        assert len(result.warnings) >= 1
        assert any("500" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Tests for _fetch_pr_changes
# ---------------------------------------------------------------------------


def _pr_json(*, title="Fix bug", body="", changed_files=3, additions=10, deletions=2):
    return {
        "title": title,
        "body": body,
        "changed_files": changed_files,
        "additions": additions,
        "deletions": deletions,
        "html_url": "https://github.com/org/repo/pull/42",
    }


class TestFetchPrChanges:
    """Tests for GitHub PR diff fetching."""

    async def test_fetches_pr_info_and_diff(self):
        diff_text = "diff --git a/foo.go b/foo.go\n+new line\n"

        def handler(request: httpx.Request):
            if "application/vnd.github.v3.diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=diff_text)
            return httpx.Response(
                200, json=_pr_json(title="Fix migration bug", body="Fixes a timeout")
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes("org", "repo", 42, _client=client)

        assert result is not None
        assert "Fix migration bug" in result
        assert "Fixes a timeout" in result
        assert "diff --git" in result
        assert "+10 -2" in result

    async def test_404_returns_none(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(404))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes("org", "repo", 42, _client=client)
        assert result is None

    async def test_large_diff_preserved_fully(self):
        big_diff = "x" * 600_000  # ~600 KB

        def handler(request: httpx.Request):
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=big_diff)
            return httpx.Response(200, json=_pr_json(title="Big PR"))

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes("org", "repo", 1, _client=client)
        assert result is not None
        assert big_diff in result
        assert "TRUNCATED" not in result

    async def test_diff_fetch_failure_still_returns_metadata(self):
        def handler(request: httpx.Request):
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(500)
            return httpx.Response(200, json=_pr_json(title="Good PR"))

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes("org", "repo", 1, _client=client)
        assert result is not None
        assert "Good PR" in result
        assert (
            "diff" not in result.lower().split("---")[-1] if "---" in result else True
        )

    async def test_github_token_passed_as_header(self):
        captured_headers = {}

        def handler(request: httpx.Request):
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_pr_json())

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await _fetch_pr_changes(
                "org", "repo", 1, github_token="ghp_secret", _client=client
            )
        assert captured_headers.get("authorization") == "token ghp_secret"

    async def test_invalid_org_rejected(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=_pr_json()))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes("../../evil", "repo", 1, _client=client)
        assert result is None

    async def test_invalid_repo_rejected(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=_pr_json()))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes("org", "../evil", 1, _client=client)
        assert result is None

    async def test_negative_pr_number_rejected(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=_pr_json()))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes("org", "repo", -1, _client=client)
        assert result is None

    async def test_string_pr_number_rejected(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=_pr_json()))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _fetch_pr_changes(
                "org", "repo", "../../evil", _client=client
            )
        assert result is None
