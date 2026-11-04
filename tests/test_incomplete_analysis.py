"""Tests for surfacing failures with no usable classification (issue #397)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rootcoz import storage
from rootcoz.result_fields import RESULT_FIELD_PATHS
from tests.conftest import make_app_client

_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}


def _failure(
    test_name: str,
    *,
    classification: str = "",
    analysis_error: str = "",
    analysis: Any = None,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "id": f"id-{test_name}",
        "test_name": test_name,
        "error": "boom",
        "analysis": (
            {"classification": classification, "details": "d"}
            if analysis is None
            else analysis
        ),
    }
    if analysis_error:
        failure["analysis_error"] = analysis_error
    return failure


def _result_payload(failures: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "job_id": "job-1",
        "job_name": "example-job",
        "build_number": 7,
        "status": "completed",
        "summary": "done",
        "failures": failures,
        **extra,
    }


@pytest.fixture
def _init_db(temp_db_path: Path):
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def client(_init_db, temp_db_path: Path):
    yield from make_app_client(temp_db_path)


class TestIncompletePredicate:
    def test_empty_classification_is_incomplete(self) -> None:
        names = storage.incomplete_failures(_result_payload([_failure("t1")]))
        assert names == ["t1"]

    def test_analysis_error_alone_marks_incomplete(self) -> None:
        payload = _result_payload(
            [_failure("t1", analysis_error="model returned no usable analysis")]
        )
        assert storage.incomplete_failures(payload) == ["t1"]

    def test_valid_classification_is_complete(self) -> None:
        payload = _result_payload([_failure("t1", classification="PRODUCT BUG")])
        assert storage.incomplete_failures(payload) == []

    def test_legacy_string_analysis_does_not_crash(self) -> None:
        """Legacy rows stored the analysis as plain text: no classification."""
        payload = _result_payload([_failure("t1", analysis="free text analysis")])
        assert storage.incomplete_failures(payload) == ["t1"]

    def test_child_and_nested_failures_are_counted(self) -> None:
        payload = _result_payload(
            [_failure("t1", classification="CODE ISSUE")],
            child_job_analyses=[
                {
                    "job_name": "child",
                    "build_number": 1,
                    "failures": [_failure("t2")],
                    "failed_children": [
                        {
                            "job_name": "leaf",
                            "build_number": 2,
                            "failures": [_failure("t3")],
                        }
                    ],
                }
            ],
        )
        assert storage.incomplete_failures(payload) == ["t2", "t3"]


class TestResultPayloadExposesCount:
    async def test_result_response_counts_incomplete_failures(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload(
                    [
                        _failure("t1"),
                        _failure("t2", classification="CODE ISSUE"),
                    ]
                ),
            )

        resp = client.get("/results/job-1", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["result"]["incomplete_count"] == 1

    async def test_fields_path_returns_the_count(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        assert "result.incomplete_count" in RESULT_FIELD_PATHS
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload([_failure("t1"), _failure("t2")]),
            )

        resp = client.get(
            "/results/job-1",
            params={"fields": "result.incomplete_count"},
            headers=_ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json() == {"result": {"incomplete_count": 2}}


class TestDashboardRowExposesCount:
    async def test_dashboard_row_counts_incomplete_failures(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload(
                    [_failure("t1"), _failure("t2", classification="CODE ISSUE")]
                ),
            )
            # A fully classified job carries no incomplete_count key at all
            await storage.save_result(
                job_id="job-2",
                build_url="https://ci.example.com/job/example-job/8/",
                status="completed",
                result=_result_payload([_failure("t3", classification="CODE ISSUE")]),
            )

        resp = client.get("/api/dashboard", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        rows = {row["job_id"]: row for row in resp.json()}
        assert rows["job-1"]["incomplete_count"] == 1
        assert "incomplete_count" not in rows["job-2"]


class TestIncompleteListingEndpoint:
    async def _seed(self, temp_db_path: Path, job_id: str, *failures: dict[str, Any]):
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id=job_id,
                build_url=f"https://ci.example.com/job/example-job/{job_id}/",
                status="completed",
                result=_result_payload(list(failures)),
            )

    async def test_lists_only_jobs_with_incomplete_failures(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        await self._seed(temp_db_path, "job-bad", _failure("t1"))
        await self._seed(
            temp_db_path, "job-good", _failure("t2", classification="CODE ISSUE")
        )

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        jobs = resp.json()
        assert [j["job_id"] for j in jobs] == ["job-bad"]
        assert jobs[0]["job_name"] == "example-job"
        assert jobs[0]["build_number"] == 7
        assert jobs[0]["incomplete_count"] == 1
        assert jobs[0]["test_names"] == ["t1"]

    async def test_zero_limit_is_rejected_and_limit_caps(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        """No "no limit": an unbounded request would be an unbounded scan."""
        await self._seed(temp_db_path, "job-a", _failure("t-a"))
        await self._seed(temp_db_path, "job-b", _failure("t-b"))

        resp = client.get(
            "/api/results/incomplete", params={"limit": 0}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 422

        assert (
            client.get(
                "/api/results/incomplete",
                params={"limit": storage.MAX_INCOMPLETE_LIST_LIMIT + 1},
                headers=_ADMIN_HEADERS,
            ).status_code
            == 422
        )

        resp = client.get(
            "/api/results/incomplete", params={"limit": 1}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestOverridesClearIncompleteness:
    """A reviewer override classifies the failure: every path must follow it.

    The override is seeded in ``test_classifications`` only (not patched into
    ``result_json``), which is what a carried-forward override looks like.
    """

    async def _seed_and_override(self, temp_db_path: Path, job_id: str = "job-1"):
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id=job_id,
                build_url=f"https://ci.example.com/job/example-job/{job_id}/",
                status="completed",
                result=_result_payload([_failure("t1")]),
            )
            await storage.override_classification(
                job_id=job_id,
                test_name="t1",
                classification="CODE ISSUE",
                username="alice",
            )

    async def test_result_payload_counts_it_complete(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload([_failure("t1")]),
            )
        resp = client.get("/results/job-1", headers=_ADMIN_HEADERS)
        assert resp.json()["result"]["incomplete_count"] == 1

        await self._seed_and_override(temp_db_path)
        resp = client.get("/results/job-1", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["result"]["incomplete_count"] == 0

    async def test_dashboard_row_counts_it_complete(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        await self._seed_and_override(temp_db_path)

        resp = client.get("/api/dashboard", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        rows = {row["job_id"]: row for row in resp.json()}
        assert "incomplete_count" not in rows["job-1"]

    async def test_listing_drops_the_job(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        await self._seed_and_override(temp_db_path)

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_analysis_error_text_does_not_keep_it_incomplete(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        """analysis_error explains the miss; the override still completes the row."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload(
                    [_failure("t1", analysis_error="no usable classification")]
                ),
            )
            await storage.override_classification(
                job_id="job-1",
                test_name="t1",
                classification="PRODUCT BUG",
                username="alice",
            )

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert resp.json() == []

    async def test_legacy_plain_text_analysis_gets_the_override(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        """A legacy string analysis must not stay unclassified after an override.

        The listing reads overrides directly, so the result payload has to show
        the same effective classification -- or the report disagrees with it.
        """
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload(
                    [_failure("t1", analysis="free text legacy analysis")]
                ),
            )
            await storage.override_classification(
                job_id="job-1",
                test_name="t1",
                classification="CODE ISSUE",
                username="alice",
            )

        resp = client.get("/results/job-1", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["result"]["incomplete_count"] == 0
        analysis = resp.json()["result"]["failures"][0]["analysis"]
        assert analysis["classification"] == "CODE ISSUE"
        # The legacy text is kept, just where a details field lives.
        assert analysis["details"] == "free text legacy analysis"

    async def test_listing_drops_the_legacy_job(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload(
                    [_failure("t1", analysis="free text legacy analysis")]
                ),
            )
            await storage.override_classification(
                job_id="job-1",
                test_name="t1",
                classification="CODE ISSUE",
                username="alice",
            )

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert resp.json() == []


class TestListingScanIsBounded:
    async def _seed(self, temp_db_path: Path, count: int) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            for i in range(count):
                await storage.save_result(
                    job_id=f"job-{i}",
                    build_url=f"https://ci.example.com/job/example-job/{i}/",
                    status="completed",
                    result=_result_payload([_failure(f"t{i}")]),
                )

    async def test_limit_stops_the_scan_early(
        self, client: TestClient, temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._seed(temp_db_path, 8)
        batch = 3
        monkeypatch.setattr(storage, "_INCOMPLETE_SCAN_BATCH", batch)

        visited: list[str] = []
        real_parse = storage.parse_result_json

        def _counting(raw: str, **kwargs: Any) -> dict[str, Any] | None:
            visited.append(str(kwargs.get("job_id", "")))
            return real_parse(raw, **kwargs)

        monkeypatch.setattr(storage, "parse_result_json", _counting)

        resp = client.get(
            "/api/results/incomplete", params={"limit": 1}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert len(visited) <= 1 + batch

    async def test_storage_zero_limit_is_unreachable_from_the_api(
        self, client: TestClient, temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._seed(temp_db_path, 5)
        monkeypatch.setattr(storage, "_INCOMPLETE_SCAN_BATCH", 2)

        resp = client.get(
            "/api/results/incomplete", params={"limit": 0}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 422


class TestOperationId:
    def test_incomplete_listing_operation_id_matches_handler_name(
        self, client: TestClient
    ) -> None:
        schema = client.get("/openapi.json").json()
        assert (
            schema["paths"]["/api/results/incomplete"]["get"]["operationId"]
            == "apiIncompleteAnalyses"
        )
