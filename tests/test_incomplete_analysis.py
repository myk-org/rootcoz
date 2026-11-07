"""Tests for surfacing failures with no usable classification (issue #397)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
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


class TestWildcardOverrideScope:
    """A child-name override stored with build 0 means "any build" of that
    child -- the SQL scoping already reads it that way
    (``child_build_number = 0 OR child_build_number = <build>``). The predicate
    matched the exact ``(test, child, build)`` tuple only, so a wildcard row
    (a legacy one, or one whose job had no ``failure_history`` row to fan out
    to) never cleared the failure it was made for (#399)."""

    def test_a_build_zero_override_clears_a_concrete_build_failure(self) -> None:
        payload = _result_payload(
            [],
            child_job_analyses=[
                {"job_name": "child", "build_number": 5, "failures": [_failure("t1")]}
            ],
        )
        overrides = {("t1", "child", 0): "CODE ISSUE"}
        assert (
            storage._effective_classification(_failure("t1"), "child", 5, overrides)
            == "CODE ISSUE"
        )
        assert storage.incomplete_failures(payload, overrides) == []

    def test_an_exact_build_override_wins_over_the_wildcard(self) -> None:
        overrides = {
            ("t1", "child", 5): "PRODUCT BUG",
            ("t1", "child", 0): "CODE ISSUE",
        }
        assert (
            storage._effective_classification(_failure("t1"), "child", 5, overrides)
            == "PRODUCT BUG"
        )


class TestReportWildcardOverride:
    """The report path resolved overrides on the exact ``(test, child, build)``
    tuple only, so a build-0 child override cleared the listing count while the
    report row kept ``classification: ""`` -- two answers to one question
    (#399). Both paths now share :func:`storage.effective_override`."""

    def test_the_helper_holds_the_exact_then_wildcard_rule(self) -> None:
        assert (
            storage.effective_override(
                {("t1", "child", 0): "CODE ISSUE"}, "t1", "child", 5
            )
            == "CODE ISSUE"
        )
        assert storage.effective_override(None, "t1", "child", 5) == ""
        assert storage.effective_override({}, "t1", "child", 0) == ""
        # No wildcard lookup when the failure itself is build 0.
        assert (
            storage.effective_override(
                {("t1", "child", 5): "PRODUCT BUG"}, "t1", "child", 0
            )
            == ""
        )

    async def _seed_report(
        self, temp_db_path: Path, rows: list[tuple[str, int]]
    ) -> None:
        """Save a report whose child build 5 failure is unclassified, plus
        the given ``(classification, child_build_number)`` override rows.

        Inserted directly: ``override_classification`` fans a wildcard out to
        the builds it finds in ``failure_history``, so it cannot reproduce the
        legacy build-0 row this guards.
        """
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload(
                    [],
                    child_job_analyses=[
                        {
                            "job_name": "child",
                            "build_number": 5,
                            "failures": [_failure("t1")],
                        }
                    ],
                ),
            )
        async with aiosqlite.connect(temp_db_path) as db:
            await db.executemany(
                "INSERT INTO test_classifications "
                "(job_id, test_name, job_name, classification, visible, "
                "child_build_number) VALUES ('job-1', 't1', 'child', ?, 1, ?)",
                rows,
            )
            await db.commit()

    async def test_a_build_zero_child_override_reaches_the_report_row(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        await self._seed_report(temp_db_path, [("CODE ISSUE", 0)])

        result = client.get("/results/job-1", headers=_ADMIN_HEADERS).json()["result"]
        assert (
            result["child_job_analyses"][0]["failures"][0]["analysis"]["classification"]
            == "CODE ISSUE"
        )
        assert result["incomplete_count"] == 0

    async def test_an_exact_build_override_still_wins_in_the_report(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        await self._seed_report(temp_db_path, [("CODE ISSUE", 0), ("PRODUCT BUG", 5)])

        row = client.get("/results/job-1", headers=_ADMIN_HEADERS).json()["result"][
            "child_job_analyses"
        ][0]["failures"][0]
        assert row["analysis"]["classification"] == "PRODUCT BUG"


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


class TestDashboardOverridesAreFetchedOnce:
    async def test_page_opens_one_extra_query_for_all_jobs(
        self, _init_db, temp_db_path: Path
    ) -> None:
        """A page of unclassified jobs must not add a query per job."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            for i in range(3):
                await storage.save_result(
                    job_id=f"job-{i}",
                    build_url=f"https://ci.example.com/job/example-job/{i}/",
                    status="completed",
                    result=_result_payload([_failure(f"t{i}")]),
                )

        real_connect = storage._connect_db
        connections = 0

        def counting_connect():
            nonlocal connections
            connections += 1
            return real_connect()

        with patch.object(storage, "_connect_db", counting_connect):
            rows = await storage.list_results_for_dashboard(limit=10)

        assert [row["incomplete_count"] for row in rows] == [1, 1, 1]
        # One page query plus one overrides query, not one per job.
        assert connections == 2


class TestSubmittedJobsAreNotIncomplete:
    """Incompleteness means the analysis ran and produced nothing — never that
    it has not run yet. An ingest-only (``submitted``) job stores every failure
    with an empty classification, which is not a failed analysis."""

    async def _seed(
        self, temp_db_path: Path, *failures: dict[str, Any], state: str
    ) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="job-1",
                build_url="https://ci.example.com/job/example-job/7/",
                status="completed",
                result=_result_payload(list(failures), analysis_state=state),
            )

    def test_predicate_skips_an_ingest_only_job(self) -> None:
        payload = _result_payload(
            [_failure("t1"), _failure("t2")], analysis_state="submitted"
        )
        assert storage.incomplete_failures(payload) == []

    async def test_results_dashboard_and_listing_agree_on_submitted(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        await self._seed(temp_db_path, _failure("t1"), state="submitted")

        resp = client.get("/results/job-1", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["result"]["incomplete_count"] == 0

        resp = client.get("/api/dashboard", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        rows = {row["job_id"]: row for row in resp.json()}
        assert rows["job-1"]["analysis_state"] == "submitted"
        assert "incomplete_count" not in rows["job-1"]

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []

    async def test_the_same_job_is_incomplete_after_a_fruitless_analysis(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        """Same failures, but the analysis ran and classified none of them."""
        await self._seed(
            temp_db_path,
            _failure("t1", analysis_error="model returned no usable analysis"),
            state="analyzed",
        )

        resp = client.get("/results/job-1", headers=_ADMIN_HEADERS)
        assert resp.json()["result"]["incomplete_count"] == 1

        resp = client.get("/api/dashboard", headers=_ADMIN_HEADERS)
        rows = {row["job_id"]: row for row in resp.json()}
        assert rows["job-1"]["incomplete_count"] == 1

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert [j["job_id"] for j in resp.json()["jobs"]] == ["job-1"]

    def test_an_analysed_job_with_classifications_is_not_incomplete(self) -> None:
        payload = _result_payload(
            [_failure("t1", classification="CODE ISSUE")], analysis_state="analyzed"
        )
        assert storage.incomplete_failures(payload) == []


class TestBulkOverridesChunking:
    """A dashboard page may carry every job (``limit=0``): the overrides query
    must not put one bound variable per job in a single statement."""

    async def _seed_overrides(self, temp_db_path: Path, job_ids: list[str]) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
        async with aiosqlite.connect(temp_db_path) as db:
            await db.executemany(
                "INSERT INTO test_classifications "
                "(job_id, test_name, classification, visible) VALUES (?, ?, ?, 1)",
                [(job_id, "t1", "CODE ISSUE") for job_id in job_ids],
            )
            await db.commit()

    async def test_ids_are_looked_up_across_chunks(
        self, temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_ids = [f"job-{i}" for i in range(5)]
        await self._seed_overrides(temp_db_path, job_ids)
        monkeypatch.setattr(storage, "_JOB_ID_QUERY_CHUNK", 2)

        with patch.object(storage, "DB_PATH", temp_db_path):
            overrides = await storage.get_effective_classifications_bulk(job_ids)

        assert set(overrides) == set(job_ids)
        assert overrides["job-4"] == {("t1", "", 0): "CODE ISSUE"}

    async def test_a_large_job_set_does_not_exceed_the_variable_limit(
        self, temp_db_path: Path
    ) -> None:
        """An unlimited dashboard page binds every job id. Unchunked, this set
        exceeds SQLite's compiled bound-variable limit (32766 here) and the
        request dies with ``too many SQL variables``."""
        job_ids = [f"job-{i}" for i in range(40000)]

        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            assert await storage.get_effective_classifications_bulk(job_ids) == {}


class TestDashboardOverrideLookup:
    """Overrides are read only for jobs that still have an unclassified failure.

    An override can clear an unclassified failure but never create one, so every
    other job on the page has nothing for its override rows to change — and an
    unlimited dashboard page carries every job.
    """

    async def test_only_unclassified_jobs_are_queried(self, temp_db_path: Path) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.save_result(
                job_id="job-bad",
                build_url="",
                status="completed",
                result=_result_payload([_failure("t1")]),
            )
            await storage.save_result(
                job_id="job-good",
                build_url="",
                status="completed",
                result=_result_payload([_failure("t2", classification="CODE ISSUE")]),
            )
            seen: list[list[str]] = []
            real_lookup = storage.get_effective_classifications_bulk

            async def _recording_lookup(job_ids: list[str]):
                seen.append(list(job_ids))
                return await real_lookup(job_ids)

            with patch.object(
                storage, "get_effective_classifications_bulk", _recording_lookup
            ):
                rows = await storage.list_results_for_dashboard(limit=10)
                filtered = await storage.list_results_for_dashboard_filtered(limit=10)

        assert {row["job_id"] for row in rows} == {"job-bad", "job-good"}
        assert {row["job_id"] for row in filtered["jobs"]} == {"job-bad", "job-good"}
        assert [sorted(ids) for ids in seen] == [["job-bad"], ["job-bad"]]


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
        jobs = resp.json()["jobs"]
        assert resp.json()["truncated"] is False
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

        # Above the cap the server clamps, it does not fail the request.
        resp = client.get(
            "/api/results/incomplete",
            params={"limit": storage.MAX_INCOMPLETE_LIST_LIMIT + 1000},
            headers=_ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert {j["job_id"] for j in resp.json()["jobs"]} == {"job-a", "job-b"}
        assert resp.json()["truncated"] is False

        resp = client.get(
            "/api/results/incomplete", params={"limit": 1}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert len(resp.json()["jobs"]) == 1
        assert resp.json()["truncated"] is True

    async def test_the_cap_is_applied_not_just_accepted(
        self,
        client: TestClient,
        temp_db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pin the clamp: without it an oversized limit returns every match."""
        for job_id in ("job-a", "job-b", "job-c"):
            await self._seed(temp_db_path, job_id, _failure(f"t-{job_id}"))
        monkeypatch.setattr("rootcoz.main.MAX_INCOMPLETE_LIST_LIMIT", 2)

        resp = client.get(
            "/api/results/incomplete", params={"limit": 100}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert len(resp.json()["jobs"]) == 2
        assert resp.json()["truncated"] is True

    async def test_limit_marks_the_listing_truncated(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        """A cut listing must say so, not look complete."""
        for job_id in ("job-a", "job-b", "job-c"):
            await self._seed(temp_db_path, job_id, _failure(f"t-{job_id}"))

        resp = client.get(
            "/api/results/incomplete", params={"limit": 2}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["jobs"]) == 2
        assert body["truncated"] is True
        assert body["scanned"] >= 2


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
        assert resp.json()["jobs"] == []

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
        assert resp.json()["jobs"] == []

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
        assert resp.json()["jobs"] == []


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

    async def test_one_undecodable_row_does_not_break_the_listing(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        """A result written by a newer build must not abort the whole scan."""
        await self._seed(temp_db_path, 1)
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute(
                "INSERT INTO results (job_id, build_url, status, result_json) "
                "VALUES (?, ?, 'completed', ?)",
                (
                    "job-newer-build",
                    "https://ci.example.com/job/example-job/9/",
                    b"\x28\xb5\x2f\xfd\x00\x01\x02\x03rest-of-frame",
                ),
            )
            await db.commit()

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert [j["job_id"] for j in resp.json()["jobs"]] == ["job-0"]

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
        assert len(resp.json()["jobs"]) == 1
        assert resp.json()["truncated"] is True
        assert resp.json()["truncated_by"] == "limit"
        assert len(visited) <= 1 + batch

    async def test_no_truncation_when_the_listing_ends_at_the_bound(
        self,
        _init_db,
        temp_db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A listing whose last match is the bound has no next page.

        Both causes claimed truncation without probing for a leftover row: the
        limit branch stopped on ``len(jobs) >= limit`` and the ceiling branch
        assumed every ceiling stop had rows behind it (#399).
        """
        await self._seed(temp_db_path, 3)

        exact_limit = await storage.list_incomplete_analyses(limit=3)
        assert len(exact_limit["jobs"]) == 3
        assert exact_limit["truncated"] is False
        assert exact_limit["truncated_by"] is None

        monkeypatch.setattr(storage, "_INCOMPLETE_SCAN_MAX_ROWS", 3)
        exact_ceiling = await storage.list_incomplete_analyses(limit=500)
        assert len(exact_ceiling["jobs"]) == 3
        assert exact_ceiling["truncated"] is False
        assert exact_ceiling["truncated_by"] is None

    async def test_override_lookups_are_batched(
        self,
        client: TestClient,
        temp_db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One chunked lookup for the batch, never one query per job."""
        await self._seed(temp_db_path, 3)

        async def _per_job_query(_job_id: str):
            raise AssertionError("listing must not query overrides per job")

        monkeypatch.setattr(
            storage, "get_all_effective_classifications", _per_job_query
        )

        resp = client.get("/api/results/incomplete", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert {j["job_id"] for j in resp.json()["jobs"]} == {
            "job-0",
            "job-1",
            "job-2",
        }

    async def test_storage_zero_limit_is_unreachable_from_the_api(
        self, client: TestClient, temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._seed(temp_db_path, 5)
        monkeypatch.setattr(storage, "_INCOMPLETE_SCAN_BATCH", 2)

        resp = client.get(
            "/api/results/incomplete", params={"limit": 0}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 422

    async def test_scan_has_a_row_ceiling(
        self,
        client: TestClient,
        temp_db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`limit` bounds the response, not the work: a viewer cannot force a
        full-table result_json parse on every call."""
        await self._seed(temp_db_path, 20)
        monkeypatch.setattr(storage, "_INCOMPLETE_SCAN_BATCH", 2)
        monkeypatch.setattr(storage, "_INCOMPLETE_SCAN_MAX_ROWS", 3)

        visited: list[str] = []
        real_parse = storage.parse_result_json

        def _counting(raw: str, **kwargs: Any) -> dict[str, Any] | None:
            visited.append(str(kwargs.get("job_id", "")))
            return real_parse(raw, **kwargs)

        monkeypatch.setattr(storage, "parse_result_json", _counting)

        resp = client.get(
            "/api/results/incomplete", params={"limit": 500}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200
        # Every seeded row matches, so only the ceiling can stop the scan.
        assert len(resp.json()["jobs"]) < 20
        assert resp.json()["truncated"] is True
        assert resp.json()["truncated_by"] == "scan_ceiling"
        assert len(visited) <= 3 + 2


class TestOperationId:
    def test_incomplete_listing_operation_id_matches_handler_name(
        self, client: TestClient
    ) -> None:
        schema = client.get("/openapi.json").json()
        assert (
            schema["paths"]["/api/results/incomplete"]["get"]["operationId"]
            == "apiIncompleteAnalyses"
        )


class TestPerGroupFallbackKeepsTheReason:
    async def test_unclassified_group_still_explains_itself(
        self, _init_db, temp_db_path: Path
    ) -> None:
        """The per-group path must not drop the reason the outer one keeps."""
        from rootcoz.engine.core import AnalysisIncompleteError
        from rootcoz.main import _run_per_group_analysis
        from rootcoz.models import FailedTest, is_failed_analysis

        reason = "AI analysis incomplete: response has no usable classification"

        with patch(
            "rootcoz.main.analyze_failure_group",
            new_callable=AsyncMock,
            side_effect=AnalysisIncompleteError(reason, error_signature="sig"),
        ):
            analyses = await _run_per_group_analysis(
                groups={"sig": [FailedTest(test_name="t1", error_message="boom")]},
                console_context="",
                repo_path=None,
                ai_provider="claude",
                ai_model="opus",
                ai_call_timeout=None,
                custom_prompt="",
                artifacts_context="",
                server_url="",
                job_id="job-1",
                additional_repos=None,
                max_concurrent_ai_calls=1,
                auth_header="",
            )

        assert len(analyses) == 1
        assert analyses[0].analysis.classification == ""
        assert analyses[0].analysis_error == reason
        # The job must not report this group as a completed analysis.
        assert is_failed_analysis(analyses[0])


class TestFailedAnalysisPredicate:
    def test_a_row_with_only_an_analysis_error_counts_as_failed(self) -> None:
        """Peer path: no classification, reason stored — not a completed analysis."""
        from rootcoz.models import AnalysisDetail, FailureAnalysis, is_failed_analysis

        failure = FailureAnalysis(
            test_name="t1",
            error="boom",
            analysis=AnalysisDetail(),
            analysis_error="AI analysis incomplete: no usable classification",
        )
        assert is_failed_analysis(failure)

    def test_a_classified_row_is_not_failed(self) -> None:
        from rootcoz.models import AnalysisDetail, FailureAnalysis, is_failed_analysis

        failure = FailureAnalysis(
            test_name="t1",
            error="boom",
            analysis=AnalysisDetail(classification="CODE ISSUE"),
            analysis_error="stale reason that must not win",
        )
        assert not is_failed_analysis(failure)
