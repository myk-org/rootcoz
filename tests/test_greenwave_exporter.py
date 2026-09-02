import json
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from rootcoz.exporters.base import ExportContext, ExporterPrerequisiteError
from rootcoz.exporters.greenwave_exporter import (
    GreenwaveExporter,
    _build_waiver_comment,
    _negotiate_challenge_token,
    _sanitize_error,
)
from rootcoz.storage import AI_SYSTEM_USERNAME

GROUP_UUID = "123e4567-e89b-42d3-a456-426614174000"


def _mock_async_client(handler):
    OriginalAsyncClient = httpx.AsyncClient

    def factory(*args, **kwargs):
        # Pass kwargs to AsyncClient along with transport
        kwargs.pop("transport", None)
        return OriginalAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _render_log_call(log_call) -> str:
    """Render a mocked lazy logger call as logging would."""
    if not log_call.args:
        return ""
    message, *args = log_call.args
    return str(message) % tuple(args) if args else str(message)


def _failure(test_name="test_a", classification="INFRASTRUCTURE", details="flaky"):
    return {
        "test_name": test_name,
        "error": "boom",
        "analysis": {"classification": classification, "details": details},
    }


def _exporter(**overrides):
    kw = {
        "url": "https://resultsdb.example.com/api/v2.0",
        "outcome_map": {
            "PRODUCT BUG": "FAILED",
            "CODE ISSUE": "FAILED",
            "INFRASTRUCTURE": "INFO",
        },
        "subject_type": "koji_build",
        "testcase_template": "rootcoz.{job_name}.{test_name}",
        "resultsdb_auth_method": "token",
        "api_token": "tok-rdb",
        "waiver_url": "https://waiverdb.example.com/api/v1.0",
        "waiver_auth_method": "oidc",
        "waiver_token": "tok-wvr",
        "push_waivers": False,
        "waivable_classifications": frozenset({"infrastructure"}),
        "allow_ai_waivers": False,
        "product_version": "prod-1.0",
        "verify": True,
    }
    kw.update(overrides)
    return GreenwaveExporter(**kw)


def _context(**overrides):
    kw = {
        "job_id": "j-123",
        "job_name": "job-abc",
        "build_number": "123",
        "jenkins_url": "http://jenkins",
        "failures": [],
        "report_url": "http://report",
        "pushed_by": "tester",
    }
    kw.update(overrides)
    return ExportContext(**kw)


async def test_push_no_failures():
    exporter = _exporter()
    ctx = _context(failures=[])
    res = await exporter.push(ctx)
    assert not res.success
    assert "no failures" in res.message.lower()
    assert res.details["pushed"] == 0
    assert res.details["details"]["resultsdb_ids"] == []


async def test_push_results_success():
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        if request.url.path.endswith("/results"):
            return httpx.Response(201, json={"id": 101})
        return httpx.Response(404)

    exporter = _exporter()
    ctx = _context(failures=[_failure("test_a"), _failure("test_b")])

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["pushed"] == 2
    assert res.details["details"]["resultsdb_ids"] == [101, 101]
    assert res.details["details"]["group_uuid"] == GROUP_UUID

    results_requests = [r for r in requests_seen if r.url.path.endswith("/results")]
    assert len(results_requests) == 2

    req_data = json.loads(results_requests[0].content)
    assert req_data["testcase"]["name"] == "rootcoz.job-abc.test_a"
    assert req_data["outcome"] == "INFO"
    assert req_data["data"]["item"] == ["job-abc"]
    assert req_data["groups"] == [GROUP_UUID]


async def test_unmapped_classification_skipped():
    exporter = _exporter()
    ctx = _context(
        failures=[
            _failure("test_a", classification="MYSTERY"),
            _failure("test_b", classification=""),
        ]
    )

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(lambda r: httpx.Response(201, json={"id": 1})),
    ):
        res = await exporter.push(ctx)

    assert not res.success
    assert "skipped" in res.message
    assert res.details["skipped"] == 2
    assert res.details["pushed"] == 0


async def test_outcome_mapping_case_insensitive():
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        return httpx.Response(201, json={"id": 1, "uuid": GROUP_UUID})

    exporter = _exporter()
    ctx = _context(failures=[_failure("test_a", classification="infrastructure")])
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)
    assert res.success
    assert res.details["pushed"] == 1
    req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    data = json.loads(req.content)
    assert data["outcome"] == "INFO"


async def test_testcase_template_with_tier():
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        return httpx.Response(201, json={"id": 1, "uuid": GROUP_UUID})

    exporter = _exporter(
        tier="tier-1", testcase_template="myproduct.product-build.{tier}.{test_name}"
    )
    ctx = _context(failures=[_failure("test_a")])
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        await exporter.push(ctx)

    req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    data = json.loads(req.content)
    assert data["testcase"]["name"] == "myproduct.product-build.tier-1.test_a"


async def test_subject_identifier_override():
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if request.url.path.endswith("/waivers/"):
            return httpx.Response(201, json={"id": 42})
        return httpx.Response(201, json={"id": 1, "uuid": GROUP_UUID})

    exporter = _exporter(push_waivers=True)

    # 1. Subject Identifier provided — writes go through normally.
    ctx = _context(
        failures=[_failure("test_a")],
        subject_identifier="build-nvr-1",
        reviewed_by={"test_a": "alice"},
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        await exporter.push(ctx)

    res_req = [r for r in requests_seen if r.url.path.endswith("/results")][-1]
    waiver_req = [r for r in requests_seen if r.url.path.endswith("/waivers/")][-1]

    assert json.loads(res_req.content)["data"]["item"] == ["build-nvr-1"]
    assert json.loads(waiver_req.content)["subject_identifier"] == "build-nvr-1"

    # 2. push_waivers=True without subject_identifier must be rejected.
    ctx2 = _context(failures=[_failure("test_a")], reviewed_by={"test_a": "alice"})
    with (
        pytest.raises(ExporterPrerequisiteError, match="subject_identifier"),
        patch(
            "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
            _mock_async_client(handler),
        ),
    ):
        await exporter.push(ctx2)


async def test_subject_identifier_in_testcase_template():
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        if request.url.path.endswith("/results"):
            return httpx.Response(201, json={"id": 101})
        return httpx.Response(404)

    exporter = _exporter(testcase_template="rootcoz.{subject_identifier}.{test_name}")

    # Case 1: subject_identifier="build-nvr-1"
    ctx1 = _context(
        failures=[_failure("test_a", classification="INFRASTRUCTURE")],
        subject_identifier="build-nvr-1",
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        await exporter.push(ctx1)

    req1 = [r for r in requests_seen if r.url.path.endswith("/results")][-1]
    data1 = json.loads(req1.content)
    assert data1["testcase"]["name"] == "rootcoz.build-nvr-1.test_a"

    # Case 2: WITHOUT subject_identifier (falls back to job_name)
    ctx2 = _context(failures=[_failure("test_a", classification="INFRASTRUCTURE")])
    job_name = ctx2.job_name  # confirm job_name

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        await exporter.push(ctx2)

    req2 = [r for r in requests_seen if r.url.path.endswith("/results")][-1]
    data2 = json.loads(req2.content)
    assert data2["testcase"]["name"] == f"rootcoz.{job_name}.test_a"

    # Case 3: subject_identifier="{something}" exactly
    ctx3 = _context(
        failures=[_failure("test_a", classification="INFRASTRUCTURE")],
        subject_identifier="{something}",
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        await exporter.push(ctx3)

    req3 = [r for r in requests_seen if r.url.path.endswith("/results")][-1]
    data3 = json.loads(req3.content)
    assert data3["testcase"]["name"] == "rootcoz.{something}.test_a"


@pytest.mark.parametrize(
    "group_body",
    [
        pytest.param({}, id="missing-uuid"),
        pytest.param({"uuid": "private-malformed-group-id"}, id="malformed-uuid"),
    ],
)
async def test_group_response_without_valid_uuid_is_a_partial_error(group_body):
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json=group_body)
        return httpx.Response(201, json={"id": 1})

    exporter = _exporter()
    context = _context(failures=[_failure("test_a")])
    with (
        patch(
            "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
            _mock_async_client(handler),
        ),
        patch("rootcoz.exporters.greenwave_exporter.logger") as mock_logger,
    ):
        result = await exporter.push(context)

    assert result.success
    assert result.details["pushed"] == 1
    assert result.details["details"]["group_uuid"] is None
    assert result.details["errors"] == ["group creation failed: invalid group response"]
    log_call = mock_logger.warning.call_args
    assert log_call.kwargs.get("exc_info") is None
    logged = _render_log_call(log_call)
    assert "ResultsDB group creation failed" in logged
    assert "job_id='j-123'" in logged
    assert "private-malformed-group-id" not in logged
    result_request = next(
        request for request in requests_seen if request.url.path.endswith("/results")
    )
    assert "groups" not in json.loads(result_request.content)


async def test_group_creation_failure_continues():
    requests_seen = []

    def handler(r):
        requests_seen.append(r)
        if r.url.path.endswith("/groups"):
            return httpx.Response(500)
        return httpx.Response(201, json={"id": 1})

    exporter = _exporter()
    ctx = _context(failures=[_failure("test_a")])
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["pushed"] == 1
    assert res.details["details"]["group_uuid"] is None
    assert any("group creation failed" in e for e in res.details["errors"])

    res_req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    assert "groups" not in json.loads(res_req.content)


async def test_per_failure_isolation():
    req_count = {"results": 0}

    def handler(r):
        if r.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        if r.url.path.endswith("/results"):
            req_count["results"] += 1
            if req_count["results"] == 1:
                return httpx.Response(500)
            return httpx.Response(201, json={"id": 2})
        return httpx.Response(404)

    exporter = _exporter()
    ctx = _context(failures=[_failure("test_a"), _failure("test_b")])
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["pushed"] == 1
    assert res.details["details"]["resultsdb_ids"] == [2]
    assert len(res.details["errors"]) == 1
    assert "(ResultsDB)" in res.details["errors"][0]


@pytest.mark.parametrize("target", ["resultsdb", "waiverdb"])
@pytest.mark.parametrize(
    "response_kwargs",
    [
        pytest.param({"json": {}}, id="missing-id"),
        pytest.param({"json": {"id": "101"}}, id="invalid-id"),
        pytest.param({"content": b"not json"}, id="non-json"),
    ],
)
async def test_accepted_write_without_valid_id_is_reported_without_retry(
    target, response_kwargs
):
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        if request.url.path.endswith("/results"):
            if target == "resultsdb":
                return httpx.Response(201, **response_kwargs)
            return httpx.Response(201, json={"id": 101})
        if request.url.path.endswith("/waivers/"):
            return httpx.Response(201, **response_kwargs)
        return httpx.Response(404)

    exporter = _exporter(push_waivers=target == "waiverdb")
    context = _context(
        failures=[_failure("test_a")],
        reviewed_by={"test_a": "alice"},
        subject_identifier="build-nvr-1",
    )
    with (
        patch(
            "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
            _mock_async_client(handler),
        ),
        patch("rootcoz.exporters.greenwave_exporter.logger") as mock_logger,
    ):
        result = await exporter.push(context)

    service = "ResultsDB" if target == "resultsdb" else "WaiverDB"
    target_path = "/results" if target == "resultsdb" else "/waivers/"
    ids_field = "resultsdb_ids" if target == "resultsdb" else "waiver_ids"
    assert result.success
    assert result.details["pushed"] == 1
    assert result.details["waived"] == (1 if target == "waiverdb" else 0)
    assert result.details["details"][ids_field] == []
    expected_error = (
        f"rootcoz.job-abc.test_a ({service}): accepted write returned no valid "
        "positive integer 'id'; external write may have succeeded but cannot be "
        "reconciled"
    )
    assert result.details["errors"] == [expected_error]
    assert (
        len(
            [
                request
                for request in requests_seen
                if request.url.path.endswith(target_path)
            ]
        )
        == 1
    )
    logged = _render_log_call(mock_logger.warning.call_args)
    assert f"{service} accepted write" in logged
    assert "job_id='j-123'" in logged
    assert "failure_index=0" in logged
    assert "rootcoz.job-abc.test_a" not in logged
    assert mock_logger.warning.call_args.kwargs.get("exc_info") is None


async def test_sanitized_logs_do_not_attach_raw_exception_details():
    def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        raise RuntimeError("secret path /etc/krb5.keytab and https://private.example")

    exporter = _exporter(testcase_template="rootcoz.{subject_identifier}.{test_name}")
    context = _context(
        failures=[_failure("test_a")],
        subject_identifier="private-subject-nvr",
        waiver_comment="private waiver justification",
    )
    with (
        patch(
            "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
            _mock_async_client(handler),
        ),
        patch("rootcoz.exporters.greenwave_exporter.logger") as mock_logger,
    ):
        result = await exporter.push(context)

    assert result.details["errors"] == [
        "rootcoz.private-subject-nvr.test_a (ResultsDB): RuntimeError"
    ]
    log_call = mock_logger.warning.call_args
    assert log_call.kwargs.get("exc_info") is None
    logged = _render_log_call(log_call)
    assert "failure_index=0" in logged
    assert "private-subject-nvr" not in logged
    assert "private waiver justification" not in logged
    assert "/etc/krb5.keytab" not in logged
    assert "private.example" not in logged


async def test_error_sanitization_no_token_leak():
    def handler(r):
        if r.url.path.endswith("/results"):
            return httpx.Response(401)
        return httpx.Response(201, json={"uuid": GROUP_UUID})

    exporter = _exporter(api_token="tok-rdb")
    ctx = _context(failures=[_failure("test_a")])
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert not res.success
    err = res.details["errors"][0]
    assert "HTTP 401" in err
    assert "tok-rdb" not in err


async def test_waiver_submitted_for_human_reviewed():
    requests_seen = []

    def handler(r):
        requests_seen.append(r)
        if r.url.path.endswith("/waivers/"):
            return httpx.Response(201, json={"id": 42})
        return httpx.Response(201, json={"uuid": GROUP_UUID, "id": 101})

    exporter = _exporter(push_waivers=True)
    ctx = _context(
        failures=[_failure("test_a")],
        reviewed_by={"test_a": "alice"},
        subject_identifier="build-nvr-1",
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["waived"] == 1
    assert res.details["details"]["waiver_ids"] == [42]

    w_req = next(r for r in requests_seen if r.url.path.endswith("/waivers/"))
    data = json.loads(w_req.content)
    assert data["subject_type"] == "koji_build"
    assert data["testcase"] == "rootcoz.job-abc.test_a"
    assert data["waived"] is True
    assert data["product_version"] == "prod-1.0"
    assert "alice" in data["comment"]


async def test_waiver_failure_is_logged_and_reported_as_partial_success():
    def handler(request):
        if request.url.path.endswith("/waivers/"):
            return httpx.Response(503)
        return httpx.Response(201, json={"uuid": GROUP_UUID, "id": 101})

    exporter = _exporter(
        push_waivers=True,
        testcase_template="rootcoz.{subject_identifier}.{test_name}",
    )
    context = _context(
        failures=[_failure("test_a")],
        reviewed_by={"test_a": "alice"},
        subject_identifier="private-subject-nvr",
        waiver_comment="private waiver justification",
    )
    with (
        patch(
            "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
            _mock_async_client(handler),
        ),
        patch("rootcoz.exporters.greenwave_exporter.logger") as mock_logger,
    ):
        result = await exporter.push(context)

    assert result.success
    assert result.details["pushed"] == 1
    assert result.details["waived"] == 0
    assert result.details["errors"] == [
        "rootcoz.private-subject-nvr.test_a (WaiverDB): HTTP 503"
    ]
    logged = " ".join(
        _render_log_call(call) for call in mock_logger.warning.call_args_list
    )
    assert "WaiverDB write failed" in logged
    assert "failure_index=0" in logged
    assert "private-subject-nvr" not in logged
    assert "private waiver justification" not in logged


async def test_waiver_guard_blocks_ai_review():
    requests_seen = []

    def handler(r):
        requests_seen.append(r)
        return httpx.Response(201, json={"uuid": GROUP_UUID, "id": 101})

    exporter = _exporter(push_waivers=True, allow_ai_waivers=False)
    ctx = _context(
        failures=[_failure("test_a")],
        reviewed_by={"test_a": AI_SYSTEM_USERNAME},
        subject_identifier="build-nvr-1",
    )

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["waived"] == 0
    assert not any(r.url.path.endswith("/waivers/") for r in requests_seen)

    # allow_ai_waivers = True
    requests_seen.clear()
    exporter2 = _exporter(push_waivers=True, allow_ai_waivers=True)
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res2 = await exporter2.push(ctx)

    assert res2.success
    assert res2.details["waived"] == 1
    assert any(r.url.path.endswith("/waivers/") for r in requests_seen)


async def test_waiver_skipped_when_unreviewed():
    requests_seen = []

    def handler(r):
        requests_seen.append(r)
        return httpx.Response(201, json={"uuid": GROUP_UUID, "id": 101})

    exporter = _exporter(push_waivers=True)
    ctx = _context(
        failures=[_failure("test_a")],
        reviewed_by={},
        subject_identifier="build-nvr-1",
    )

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["waived"] == 0
    assert not any(r.url.path.endswith("/waivers/") for r in requests_seen)


async def test_waiver_skipped_non_waivable():
    requests_seen = []

    def handler(r):
        requests_seen.append(r)
        return httpx.Response(201, json={"uuid": GROUP_UUID, "id": 101})

    exporter = _exporter(push_waivers=True)
    ctx = _context(
        failures=[_failure("test_a", classification="CODE ISSUE")],
        reviewed_by={"test_a": "alice"},
        subject_identifier="build-nvr-1",
    )

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["pushed"] == 1
    assert res.details["waived"] == 0
    assert not any(r.url.path.endswith("/waivers/") for r in requests_seen)


def test_waiver_missing_product_version_rejected_before_writes():
    with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_PRODUCT_VERSION"):
        _exporter(push_waivers=True, product_version=None)


def test_waiver_missing_url_rejected_before_writes():
    with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_WAIVER_URL"):
        _exporter(push_waivers=True, waiver_url=None)


def test_waiver_missing_oidc_token_rejected_before_writes():
    with pytest.raises(ExporterPrerequisiteError, match="GREENWAVE_WAIVER_TOKEN"):
        _exporter(push_waivers=True, waiver_token=None)


def test_client_reuse_identity_includes_origin_transport_and_credentials():
    exporter = _exporter(
        resultsdb_auth_method="kerberos",
        waiver_auth_method="kerberos",
        kerberos_keytab="/keytab",
    )
    results_identity = exporter._client_identity(
        service="resultsdb", url="https://resultsdb.example.com/api"
    )
    assert results_identity == exporter._client_identity(
        service="waiver", url="https://resultsdb.example.com/waivers"
    )
    assert results_identity != exporter._client_identity(
        service="waiver", url="https://waiverdb.example.com/api"
    )


def test_authenticated_http_requires_explicit_development_escape_hatch():
    options = {
        "url": "http://resultsdb.example.com/api/v2.0",
        "resultsdb_auth_method": "token",
        "api_token": "tok-rdb",
    }
    with pytest.raises(ExporterPrerequisiteError, match="require HTTPS"):
        _exporter(**options)

    # token auth with verify=False is also rejected: bearer tokens must not be
    # sent over plaintext HTTP (credential-leak risk, see finding #183).
    with pytest.raises(ExporterPrerequisiteError, match="require HTTPS"):
        _exporter(**options, verify=False)


def test_unauthenticated_http_with_verification_enabled_is_rejected():
    with pytest.raises(ExporterPrerequisiteError, match="require HTTPS"):
        _exporter(
            url="http://resultsdb.example.com/api/v2.0",
            resultsdb_auth_method="none",
            api_token=None,
            verify=True,
        )


def test_unauthenticated_http_with_verification_disabled_warns():
    with patch("rootcoz.exporters.greenwave_exporter.logger") as mock_logger:
        exporter = _exporter(
            url="http://resultsdb.example.com/api/v2.0",
            resultsdb_auth_method="none",
            api_token=None,
            verify=False,
        )

    assert exporter.is_enabled
    mock_logger.warning.assert_called_once()
    assert "isolated local development" in _render_log_call(
        mock_logger.warning.call_args
    )


def test_client_certificate_http_is_rejected_with_verification_disabled():
    with pytest.raises(ExporterPrerequisiteError, match="require HTTPS"):
        _exporter(
            url="http://resultsdb.example.com/api/v2.0",
            resultsdb_auth_method="ssl",
            api_token=None,
            ssl_cert="/etc/rootcoz/client.crt",
            ssl_key="/etc/rootcoz/client.key",
            verify=False,
        )


def test_exporter_rejects_casefold_colliding_outcome_keys():
    with pytest.raises(
        ValueError, match="Duplicate Greenwave outcome-map classification keys"
    ):
        _exporter(outcome_map={"Code Issue": "INFO", "CODE ISSUE": "FAILED"})


def test_exporter_normalizes_write_base_urls():
    exporter = _exporter(
        url="HTTPS://ResultsDB.EXAMPLE.COM/api/v2.0///",
        push_waivers=True,
        waiver_url="https://WaiverDB.EXAMPLE.COM/api/v1.0///",
    )
    assert exporter._url == "https://resultsdb.example.com/api/v2.0"
    assert exporter._waiver_url == "https://waiverdb.example.com/api/v1.0"


async def test_waiver_success_code_201():
    def handler(r):
        if r.url.path.endswith("/waivers/"):
            return httpx.Response(201, json={"id": 42})
        return httpx.Response(201, json={"id": 1, "uuid": GROUP_UUID})

    exporter = _exporter(push_waivers=True)
    ctx = _context(
        failures=[_failure("test_a")],
        reviewed_by={"test_a": "alice"},
        subject_identifier="build-nvr-1",
    )

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    assert res.details["waived"] == 1
    assert res.details["details"]["waiver_ids"] == [42]


async def test_every_corrupt_failure_entry_is_logged_and_reported():
    exporter = _exporter()
    context = _context(failures=["notadict", {"test_name": "missing fields"}, None])

    with (
        patch(
            "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
            _mock_async_client(
                lambda request: httpx.Response(201, json={"id": 1, "uuid": GROUP_UUID})
            ),
        ),
        patch("rootcoz.exporters.greenwave_exporter.logger") as mock_logger,
    ):
        result = await exporter.push(context)

    assert not result.success
    assert result.details["pushed"] == 0
    assert result.details["errors"] == [
        "corrupt failure entry at index 0",
        "corrupt failure entry at index 1",
        "corrupt failure entry at index 2",
    ]
    assert mock_logger.warning.call_count == 3
    logged = " ".join(
        _render_log_call(call) for call in mock_logger.warning.call_args_list
    )
    assert "j-123" in logged
    assert "missing fields" not in logged
    assert "Field required" not in logged
    assert all(
        call.kwargs.get("exc_info") is None
        for call in mock_logger.warning.call_args_list
    )


async def test_details_shape():
    exporter = _exporter()
    ctx = _context(failures=[])
    res = await exporter.push(ctx)
    assert "pushed" in res.details
    assert "skipped" in res.details
    assert "waived" in res.details
    assert "errors" in res.details
    assert "details" in res.details
    assert "resultsdb_ids" in res.details["details"]
    assert "waiver_ids" in res.details["details"]
    assert "group_uuid" in res.details["details"]


def test_build_waiver_comment_helper():
    comment = _build_waiver_comment("INFRA", "broken network", "jenkins", "alice")
    assert comment.startswith("jenkins: ")
    assert "Waived by rootcoz: INFRA" in comment
    assert "broken network" in comment
    assert "reviewed by alice" in comment


def test_build_waiver_comment_with_user_comment():
    comment = _build_waiver_comment(
        "INFRASTRUCTURE", "flaky net", "alice", "alice", user_comment="known CI flake"
    )
    assert comment == (
        "alice: known CI flake — rootcoz: INFRASTRUCTURE, flaky net, reviewed by alice"
    )
    assert "Waived by rootcoz" not in comment


def test_build_waiver_comment_user_comment_no_pushed_by():
    comment = _build_waiver_comment(
        "INFRASTRUCTURE", "flaky net", "", "alice", user_comment="known CI flake"
    )
    assert comment == (
        "known CI flake — rootcoz: INFRASTRUCTURE, flaky net, reviewed by alice"
    )
    assert "Waived by rootcoz" not in comment


def test_build_waiver_comment_empty_user_comment_falls_back():
    for empty in ("", "   ", None):
        comment = _build_waiver_comment(
            "INFRASTRUCTURE", "flaky net", "alice", "alice", user_comment=empty
        )
        assert "Waived by rootcoz: INFRASTRUCTURE" in comment
        assert "reviewed by alice" in comment


async def test_waiver_uses_user_comment():
    requests_seen = []

    def handler(r):
        requests_seen.append(r)
        if r.url.path.endswith("/waivers/"):
            return httpx.Response(201, json={"id": 42})
        return httpx.Response(201, json={"uuid": GROUP_UUID, "id": 101})

    exporter = _exporter(push_waivers=True)
    ctx = _context(
        failures=[_failure("test_a")],
        reviewed_by={"test_a": "alice"},
        waiver_comment="known CI flake",
        subject_identifier="build-nvr-1",
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    w_req = next(r for r in requests_seen if r.url.path.endswith("/waivers/"))
    data = json.loads(w_req.content)
    assert data["comment"] == (
        "tester: known CI flake — rootcoz: INFRASTRUCTURE, flaky, reviewed by alice"
    )
    assert "Waived by rootcoz" not in data["comment"]


def test_negotiate_token_parses_combined_authentication_schemes():
    response = httpx.Response(
        401,
        headers={"WWW-Authenticate": 'Basic realm="internal", Negotiate c2VydmVy'},
    )
    assert _negotiate_challenge_token(response) == b"server"


def test_sanitize_error_helper():
    resp = httpx.Response(403)
    exc = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "http://foo"), response=resp
    )
    assert _sanitize_error(exc) == "HTTP 403"

    gen_exc = RuntimeError("secret-token-leaked")
    assert _sanitize_error(gen_exc) == "RuntimeError"
    assert "secret-token-leaked" not in _sanitize_error(gen_exc)


async def test_multiround_kerberos_reuses_context_client_and_cookies():
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if len(requests_seen) == 1:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": "Negotiate c2VydmVyLTE=",
                    "Set-Cookie": "gw_session=round1; Path=/",
                },
            )
        if len(requests_seen) == 2:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": "Negotiate c2VydmVyLTI="},
            )
        return httpx.Response(
            201,
            json={"id": 1},
            headers={"WWW-Authenticate": "Negotiate c2VydmVyLTM="},
        )

    exporter = _exporter(resultsdb_auth_method="kerberos", kerberos_keytab="/k")
    fake_spnego = MagicMock()
    fake_spnego.KerberosKeytab = MagicMock()
    fake_context = MagicMock()
    fake_context.step.side_effect = [b"client-1", b"client-2", None]
    fake_context.complete = True
    fake_spnego.client.return_value = fake_context

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with patch.dict(sys.modules, {"spnego": fake_spnego}):
            response = await exporter._post_with_auth(
                client,
                "https://resultsdb.example.com/api/v2.0/results",
                json={"outcome": "INFO"},
                service="resultsdb",
            )

    assert response.status_code == 201
    fake_spnego.client.assert_called_once()
    assert fake_context.step.call_count == 3
    assert fake_context.step.call_args_list[0].args == (b"server-1",)
    assert fake_context.step.call_args_list[1].args == (b"server-2",)
    assert fake_context.step.call_args_list[2].args == (b"server-3",)
    assert "Authorization" not in requests_seen[0].headers
    assert requests_seen[1].headers["Authorization"] == "Negotiate Y2xpZW50LTE="
    assert requests_seen[2].headers["Authorization"] == "Negotiate Y2xpZW50LTI="
    assert requests_seen[1].headers["Cookie"] == "gw_session=round1"
    assert requests_seen[2].headers["Cookie"] == "gw_session=round1"


async def test_kerberos_session_cookie_avoids_fresh_incomplete_context():
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if request.headers.get("Cookie") == "gw_session=authenticated":
            return httpx.Response(201, json={"id": 2})
        if "Authorization" not in request.headers:
            return httpx.Response(401, headers={"WWW-Authenticate": "Negotiate"})
        return httpx.Response(
            201,
            json={"id": 1},
            headers={
                "WWW-Authenticate": "Negotiate ZmluYWw=",
                "Set-Cookie": "gw_session=authenticated; Path=/",
            },
        )

    exporter = _exporter(resultsdb_auth_method="kerberos", kerberos_keytab="/k")
    fake_spnego = MagicMock()
    fake_spnego.KerberosKeytab = MagicMock()
    fake_context = MagicMock()
    fake_context.step.side_effect = [b"initial", None]
    fake_context.complete = True
    fake_spnego.client.return_value = fake_context

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with patch.dict(sys.modules, {"spnego": fake_spnego}):
            first = await exporter._post_with_auth(
                client,
                "https://resultsdb.example.com/results",
                json={"n": 1},
                service="resultsdb",
            )
            second = await exporter._post_with_auth(
                client,
                "https://resultsdb.example.com/results",
                json={"n": 2},
                service="resultsdb",
            )

    assert first.status_code == 201
    assert second.status_code == 201
    fake_spnego.client.assert_called_once()
    assert len(requests_seen) == 3
    assert "Authorization" not in requests_seen[-1].headers
    assert requests_seen[-1].headers["Cookie"] == "gw_session=authenticated"


async def test_kerberos_headers_challenge():
    requests_seen = []

    def handler(r):
        requests_seen.append(r)
        if "Authorization" not in r.headers:
            return httpx.Response(401, headers={"WWW-Authenticate": "Negotiate"})
        return httpx.Response(201, json={"id": 1, "uuid": GROUP_UUID})

    exporter = _exporter(resultsdb_auth_method="kerberos", kerberos_keytab="/k")
    ctx = _context(failures=[_failure("test_a")])

    fake_spnego = MagicMock()
    fake_spnego.KerberosKeytab = MagicMock()
    fake_ctx = MagicMock()
    fake_ctx.step.return_value = b"faketoken"
    fake_spnego.client.return_value = fake_ctx

    with (
        patch(
            "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
            _mock_async_client(handler),
        ),
        patch.dict(sys.modules, {"spnego": fake_spnego}),
    ):
        res = await exporter.push(ctx)

    assert res.success
    # ResultsDB uses challenge-first SPNEGO, then retries with the token.
    result_requests = [
        request for request in requests_seen if request.url.path.endswith("/results")
    ]
    assert len(result_requests) == 2
    assert "Authorization" not in result_requests[0].headers
    assert result_requests[1].headers["Authorization"] == (
        "Negotiate ZmFrZXRva2Vu"  # base64("faketoken")
    )


# ---------------------------------------------------------------------------
# Finding #183 tests — subject-identifier / transport safety coverage
# ---------------------------------------------------------------------------


def test_oidc_http_verify_false_is_rejected():
    """(a) oidc waiver auth + HTTP + verify=False must be rejected."""
    with pytest.raises(ExporterPrerequisiteError, match="require HTTPS"):
        _exporter(
            push_waivers=True,
            waiver_url="http://waiverdb.example.com/api/v1.0",
            waiver_auth_method="oidc",
            waiver_token="tok-wvr",
            verify=False,
        )


async def test_auto_push_without_subject_identifier_rejected():
    """(c) auto-push (pushed_by=AI_SYSTEM_USERNAME) without subject_identifier raises."""
    exporter = _exporter()
    ctx = _context(
        failures=[_failure("test_a")],
        pushed_by=AI_SYSTEM_USERNAME,
        # subject_identifier intentionally omitted
    )
    with pytest.raises(ExporterPrerequisiteError, match="subject_identifier"):
        await exporter.push(ctx)


async def test_push_waivers_without_subject_identifier_rejected():
    """(d) push_waivers=True without subject_identifier raises before any HTTP write."""
    exporter = _exporter(push_waivers=True)
    ctx = _context(
        failures=[_failure("test_a")],
        reviewed_by={"test_a": "alice"},
        # subject_identifier intentionally omitted
    )
    with pytest.raises(
        ExporterPrerequisiteError,
        match="subject_identifier",
    ):
        await exporter.push(ctx)


async def test_manual_resultsdb_only_without_subject_identifier_falls_back_to_job_name():
    """(e) Manual push with push_waivers=False and non-AI pushed_by falls back to job_name."""
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        return httpx.Response(201, json={"id": 1})

    exporter = _exporter(push_waivers=False)
    ctx = _context(
        failures=[_failure("test_a")],
        pushed_by="human-operator",
        # subject_identifier intentionally omitted
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    data = json.loads(req.content)
    # Falls back to job_name for ResultsDB-only (non-gating) pushes.
    assert data["data"]["item"] == [ctx.job_name]


# ---------------------------------------------------------------------------
# Feature: GREENWAVE_SUBJECT_TEMPLATE – auto-push subject construction
# ---------------------------------------------------------------------------


def _exporter_with_template(template: str, **overrides):
    """Create a GreenwaveExporter with a subject_template set."""
    return _exporter(subject_template=template, **overrides)


async def test_subject_template_renders_correct_subject():
    """(c) push() with no explicit subject but configured template renders subject correctly."""
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        return httpx.Response(201, json={"id": 42})

    template = "hco-bundle-registry-container-{product_version}.rhel9-{build_number}"
    exporter = _exporter_with_template(template, product_version="v4.20.0")
    ctx = _context(
        failures=[_failure("test_a")],
        build_number="240",
        # no subject_identifier — must be derived from template
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    result_req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    body = json.loads(result_req.content)
    expected = "hco-bundle-registry-container-v4.20.0.rhel9-240"
    assert body["data"]["item"] == [expected], body["data"]


async def test_subject_template_explicit_subject_takes_priority():
    """Explicit subject_identifier overrides the configured template."""
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        return httpx.Response(201, json={"id": 1})

    exporter = _exporter_with_template(
        "auto-{product_version}-{build_number}", product_version="v1"
    )
    explicit = "explicit-nvr-build-999"
    ctx = _context(
        failures=[_failure("test_a")],
        build_number="100",
        subject_identifier=explicit,
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    result_req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    body = json.loads(result_req.content)
    assert body["data"]["item"] == [explicit]


async def test_subject_template_with_tier_placeholder():
    """Template {tier} resolves to the exporter's tier."""
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        return httpx.Response(201, json={"id": 1})

    exporter = _exporter_with_template(
        "build-{tier}-{build_number}", tier="tier-2", product_version="v1"
    )
    ctx = _context(failures=[_failure("test_a")], build_number="77")
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    result_req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    body = json.loads(result_req.content)
    assert body["data"]["item"] == ["build-tier-2-77"]


async def test_subject_template_rendered_subject_gets_control_char_sanitization():
    """(d) Control chars in a rendered subject template are stripped."""
    from rootcoz.exporters.greenwave_exporter import _render_subject

    # Template that would produce a control char in the rendered output
    # (e.g., if build_number were tampered via env with embedded control chars)
    result = _render_subject(
        template="build-{build_number}",
        job_name="job",
        build_number="123\x00abc",
        tier=None,
        product_version=None,
    )
    assert result == "build-123abc"
    assert "\x00" not in result


async def test_auto_push_with_template_does_not_raise_prerequisite_error():
    """With a subject_template, auto-push (pushed_by=AI_SYSTEM_USERNAME) must not raise."""
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        return httpx.Response(201, json={"id": 1})

    from rootcoz.storage import AI_SYSTEM_USERNAME

    exporter = _exporter_with_template(
        "build-{product_version}-{build_number}", product_version="v2.0"
    )
    ctx = _context(
        failures=[_failure("test_a")],
        build_number="50",
        pushed_by=AI_SYSTEM_USERNAME,
        # no explicit subject_identifier
    )
    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success
    result_req = next(r for r in requests_seen if r.url.path.endswith("/results"))
    body = json.loads(result_req.content)
    assert body["data"]["item"] == ["build-v2.0-50"]


# ---------------------------------------------------------------------------
# FIX 5: rendered subject 500-char cap
# ---------------------------------------------------------------------------


async def test_subject_template_exceeds_500_chars_raises_prerequisite_error():
    """Rendered subject > 500 chars raises ExporterPrerequisiteError (FIX 5)."""
    # Build a template whose render will exceed 500 chars via a long build_number.
    long_build_number = "9" * 498  # "prefix-" + 498 + "-suffix" = 512 chars
    template = "prefix-{build_number}-suffix"

    exporter = _exporter_with_template(template, product_version="v1")
    ctx = _context(
        failures=[_failure("test_a")],
        build_number=long_build_number,
    )
    with pytest.raises(ExporterPrerequisiteError, match="500"):
        await exporter.push(ctx)


async def test_subject_template_exactly_500_chars_is_allowed():
    """Rendered subject == 500 chars must not raise (boundary condition)."""
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(201, json={"uuid": GROUP_UUID})
        return httpx.Response(201, json={"id": 1})

    # Render to exactly 500 chars: template literal + build_number padding
    prefix = "build-"
    subject_500 = prefix + "x" * (500 - len(prefix))
    assert len(subject_500) == 500
    pad = "x" * (500 - len(prefix))
    exporter = _exporter_with_template("build-{build_number}", product_version="v1")
    ctx = _context(failures=[_failure("test_a")], build_number=pad)

    with patch(
        "rootcoz.exporters.greenwave_exporter.httpx.AsyncClient",
        _mock_async_client(handler),
    ):
        res = await exporter.push(ctx)

    assert res.success


# ---------------------------------------------------------------------------
# FIX 2/3: _render_subject fail-closed — unresolved placeholders, empty result
# ---------------------------------------------------------------------------


async def test_manual_push_unset_product_version_raises_prerequisite_error():
    """(a) Manual push (non-AI, push_waivers=False) with {product_version} unset raises.

    A template referencing a placeholder whose value is None must raise
    ExporterPrerequisiteError instead of writing a malformed NVR to
    ResultsDB/WaiverDB.
    """
    exporter = _exporter_with_template(
        "build-{product_version}-{build_number}",
        product_version=None,
    )
    ctx = _context(
        failures=[_failure("test_a")],
        build_number="240",
        pushed_by="human-operator",  # not AI, not push_waivers
    )
    with pytest.raises(ExporterPrerequisiteError, match="subject_identifier"):
        await exporter.push(ctx)


def test_render_subject_raises_for_control_char_only_template():
    """(b) A template that is entirely control characters raises ExporterPrerequisiteError.

    The literal-control-char guard (FIX 1 symmetric) catches this at the top of
    _render_subject before any rendering occurs, producing a 'control characters'
    error rather than the empty-result 'subject_identifier' message.  Both are
    fail-closed; the earlier guard now wins because the template literal itself
    is malformed.
    """
    from rootcoz.exporters.greenwave_exporter import _render_subject

    with pytest.raises(ExporterPrerequisiteError, match="control characters"):
        _render_subject(
            template="\x01\x02\x03",
            job_name="job",
            build_number="123",
            tier=None,
            product_version=None,
        )


# ---------------------------------------------------------------------------
# FIX 1 regression: control-char-only placeholder values must fail-closed
#
# Each test reproduces the exact hole described in the review: a value that is
# control-chars-only (truthy after Python's `bool()` but empty after
# sanitize_control_chars+strip) must raise ExporterPrerequisiteError instead
# of slipping through the per-placeholder guard and being stripped from the
# rendered string, which would produce a malformed subject like 'build--240'.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,description",
    [
        (
            {
                "template": "build-{tier}-{build_number}",
                "job_name": "job",
                "build_number": "240",
                "tier": "\x00",
                "product_version": None,
            },
            "{tier}='\\x00' must not produce 'build--240'",
        ),
        (
            {
                "template": "product-{product_version}-{build_number}",
                "job_name": "job",
                "build_number": "240",
                "tier": None,
                "product_version": "\x00",
            },
            "{product_version}='\\x00' must not produce 'product--240'",
        ),
        (
            {
                "template": "{job_name}-{build_number}",
                "job_name": "\x00",
                "build_number": "240",
                "tier": None,
                "product_version": None,
            },
            "{job_name}='\\x00' must not produce '-240'",
        ),
        (
            {
                "template": "build-{build_number}-suffix",
                "job_name": "job",
                "build_number": "\x00",
                "tier": None,
                "product_version": None,
            },
            "{build_number}='\\x00' must not produce 'build--suffix'",
        ),
    ],
)
def test_render_subject_control_char_only_placeholder_raises(
    kwargs: dict, description: str
) -> None:
    """Control-char-only placeholder values must raise ExporterPrerequisiteError.

    Regression test for the gating-safety hole: per-placeholder sanitization
    must happen BEFORE the emptiness guard so that e.g. tier='\\x00' is caught
    here rather than slipping through and being stripped from the final rendered
    string, which would produce a malformed gating subject.
    """
    from rootcoz.exporters.greenwave_exporter import _render_subject

    with pytest.raises(
        ExporterPrerequisiteError,
        match="subject_identifier",
        # description is only for human readability in pytest output
    ):
        _render_subject(**kwargs)


# ---------------------------------------------------------------------------
# Symmetric FIX: control chars in the template LITERAL (not a placeholder value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "build_number", "description"),
    [
        (
            "build-\x01-{build_number}",
            "240",
            "SOH (\\x01) in template literal must not produce 'build--240'",
        ),
        (
            "\x01{build_number}\x01",
            "240",
            "Leading and trailing SOH in template literal must fail closed",
        ),
    ],
)
def test_render_subject_control_char_in_template_literal_raises(
    template: str, build_number: str, description: str
) -> None:
    """Control chars in the template literal must raise ExporterPrerequisiteError.

    Symmetric regression to the placeholder-value control-char hole: a template
    like 'build-\\x01-{build_number}' with build_number='240' previously
    rendered to 'build--240' because the final sanitize_control_chars call
    silently stripped the literal \\x01.  The render-time guard now rejects
    any template whose literal text contains control characters BEFORE any
    rendering takes place.
    """
    from rootcoz.exporters.greenwave_exporter import _render_subject

    with pytest.raises(
        ExporterPrerequisiteError,
        match="control characters",
    ):
        _render_subject(
            template=template,
            job_name="job",
            build_number=build_number,
            tier=None,
            product_version=None,
        )
