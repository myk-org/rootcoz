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

    async def test_limit_zero_lists_all_and_limit_caps(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        await self._seed(temp_db_path, "job-a", _failure("t-a"))
        await self._seed(temp_db_path, "job-b", _failure("t-b"))

        resp = client.get(
            "/api/results/incomplete", params={"limit": 0}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert {j["job_id"] for j in resp.json()} == {"job-a", "job-b"}

        resp = client.get(
            "/api/results/incomplete", params={"limit": 1}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
