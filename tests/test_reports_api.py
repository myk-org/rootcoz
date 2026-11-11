"""HTTP-level tests for the three reports endpoints (#546 A1/A2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rootcoz import storage

_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}

_ENDPOINTS = (
    "/api/reports/totals",
    "/api/reports/classification-overrides",
    "/api/reports/issues-created",
)


@pytest.fixture
def _init_db(temp_db_path: Path):
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def client(_init_db, temp_db_path: Path):
    """Admin-authenticated client; the guard cases ask for a session cookie."""
    import os

    from starlette.testclient import TestClient as _TestClient

    from rootcoz.config import clear_db_settings_cache

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "ADMIN_KEY": _ADMIN_KEY,
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "SECURE_COOKIES": "false",
        "DB_PATH": str(temp_db_path),
        "REQUIRE_APPROVAL": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        clear_db_settings_cache()
        from rootcoz.main import app

        with _TestClient(app, headers=_ADMIN_HEADERS) as c:
            yield c
    clear_db_settings_cache()


@pytest.fixture
def seeded(_init_db, temp_db_path: Path, client: TestClient):
    """Two jobs: one inside the 2025 window by alice, one in 2026 by bob."""

    async def _run():
        with patch.object(storage, "DB_PATH", temp_db_path):
            for job_id, name, created, submitter, state in (
                ("in-window", "job-alpha", "2025-06-01T10:00:00", "alice", "analyzed"),
                ("out-window", "job-beta", "2026-06-01T10:00:00", "bob", "submitted"),
            ):
                await storage.save_result(
                    job_id,
                    f"https://jenkins.example.com/job/{name}/1",
                    "completed",
                    {
                        "job_name": name,
                        "build_number": 1,
                        "failures": [],
                        "request_params": {"submitted_by": submitter},
                        "analysis_state": state,
                    },
                )
                await storage.add_comment(
                    job_id,
                    "test_login",
                    "GitHub Issue: [bug](https://github.com/example/repo/issues/1)",
                    username=submitter,
                )
                async with storage._connect_db() as db:
                    await db.execute(
                        "UPDATE results SET created_at = ? WHERE job_id = ?",
                        (created, job_id),
                    )
                    await db.commit()

    asyncio.run(_run())
    return client


def _register_and_login(client: TestClient, username: str) -> dict:
    resp = client.post("/api/auth/register", json={"username": username})
    assert resp.status_code == 200
    api_key = resp.json()["api_key"]
    login = client.post(
        "/api/auth/login", json={"username": username, "api_key": api_key}
    )
    assert login.status_code == 200
    return {"rootcoz_session": login.cookies["rootcoz_session"]}


class TestReportsFilters:
    def test_submitted_by_narrows_each_endpoint(self, seeded) -> None:
        """Each endpoint narrows to the alice job and drops the bob job.

        classification-overrides has no seeded override row here, so it is
        covered at the storage level instead (TestClassificationOverrideFilters).
        """
        for path in _ENDPOINTS:
            if path == "/api/reports/classification-overrides":
                continue
            filtered = seeded.get(path, params={"submitted_by": "alice"})
            assert filtered.status_code == 200, filtered.text
            body = filtered.text
            assert "job-alpha" in body, (path, body)
            assert "job-beta" not in body, (path, body)

    def test_analysis_state_narrows_each_endpoint(self, seeded) -> None:
        for path in _ENDPOINTS:
            if path == "/api/reports/classification-overrides":
                continue
            filtered = seeded.get(path, params={"analysis_state": "submitted"})
            assert filtered.status_code == 200, filtered.text
            body = filtered.text
            assert "job-beta" in body, (path, body)
            assert "job-alpha" not in body, (path, body)

    def test_invalid_analysis_state_is_400(self, seeded) -> None:
        for path in _ENDPOINTS:
            resp = seeded.get(path, params={"analysis_state": "nonsense"})
            assert resp.status_code == 400, (path, resp.text)

    def test_over_long_submitter_is_400(self, seeded) -> None:
        for path in _ENDPOINTS:
            resp = seeded.get(path, params={"submitted_by": "x" * 65})
            assert resp.status_code == 400, (path, resp.text)

    def test_canonical_and_legacy_dates_agree(self, seeded) -> None:
        """The alias pair is genuinely equivalent, seeded so the body can differ.

        ``date_from`` used to be an ignored name, so an all-inside seed would make
        this assertion true on the old code. The seed has a job **outside** the
        window, so an ignored bound changes the body.
        """
        canonical = {"date_from": "2025-01-01", "date_to": "2025-12-31"}
        legacy = {"from": "2025-01-01", "to": "2025-12-31"}
        for path in _ENDPOINTS:
            a = seeded.get(path, params=canonical)
            b = seeded.get(path, params=legacy)
            assert a.status_code == 200 and b.status_code == 200
            assert a.json() == b.json(), path

    def test_canonical_wins_when_both_are_sent(self, seeded) -> None:
        # Differing bounds: only a real 'date_from or legacy_from' resolves to 2025.
        params = {
            "date_from": "2025-01-01",
            "date_to": "2025-12-31",
            "from": "2026-01-01",
            "to": "2026-12-31",
        }
        for path in _ENDPOINTS:
            both = seeded.get(path, params=params)
            canonical = seeded.get(
                path, params={"date_from": "2025-01-01", "date_to": "2025-12-31"}
            )
            assert both.status_code == 200
            assert both.json() == canonical.json(), path


class TestReportsAccessGuard:
    def test_each_endpoint_403_without_can_view_reports(self, seeded) -> None:
        """A plain session (no can_view_reports) is refused by every endpoint."""
        cookies = _register_and_login(seeded, "plainuser")
        with TestClient(seeded.app) as anonymous:
            for path in _ENDPOINTS:
                resp = anonymous.get(path, cookies=cookies)
                assert resp.status_code == 403, (path, resp.text)

    def test_admin_key_is_allowed(self, seeded) -> None:
        for path in _ENDPOINTS:
            resp = seeded.get(path, headers=_ADMIN_HEADERS)
            assert resp.status_code == 200, (path, resp.text)


class TestClassificationOverrideFilters:
    """The third endpoint's new filters, at the level that can seed an override."""

    @pytest.fixture
    def overrides(self, _init_db, temp_db_path: Path):
        async def _run():
            with patch.object(storage, "DB_PATH", temp_db_path):
                async with storage._connect_db() as db:
                    for job_id, submitter, state in (
                        ("ov-alpha", "alice", "analyzed"),
                        ("ov-beta", "bob", "submitted"),
                    ):
                        await db.execute(
                            "INSERT INTO results (job_id, job_name, status,"
                            " created_at, submitted_by, analysis_state)"
                            " VALUES (?, ?, 'completed', '2025-06-01T10:00:00', ?, ?)",
                            (job_id, f"job-{submitter}", submitter, state),
                        )
                        await db.execute(
                            "INSERT INTO failure_history (job_id, job_name,"
                            " build_number, test_name, child_job_name,"
                            " child_build_number)"
                            " VALUES (?, ?, 1, 'test_login', '', 1)",
                            (job_id, f"job-{submitter}"),
                        )
                        await db.execute(
                            "INSERT INTO failure_reviews (job_id, test_name,"
                            " reviewed) VALUES (?, 'test_login', 1)",
                            (job_id,),
                        )
                        await db.execute(
                            "INSERT INTO test_entries (job_id, test_name, status)"
                            " VALUES (?, 'test_login', 'failed')",
                            (job_id,),
                        )
                        await db.execute(
                            "INSERT INTO test_classifications (job_id, job_name,"
                            " test_name, child_build_number, classification,"
                            " pattern, original_classification, original_pattern,"
                            " created_by, created_at, visible)"
                            " VALUES (?, '', 'test_login', 1, 'PRODUCT BUG', '',"
                            " 'FLAKY', '', 'reviewer', '2025-06-02T10:00:00', 1)",
                            (job_id,),
                        )
                    await db.commit()

        asyncio.run(_run())
        return storage

    async def test_submitter_filter_narrows(self, overrides) -> None:
        rows = await overrides.get_report_classification_overrides(submitters={"alice"})
        assert [row["job_id"] for row in rows["details"]] == ["ov-alpha"]

    async def test_analysis_state_filter_narrows(self, overrides) -> None:
        rows = await overrides.get_report_classification_overrides(
            analysis_state="submitted"
        )
        assert [row["job_id"] for row in rows["details"]] == ["ov-beta"]

    async def test_unfiltered_returns_both(self, overrides) -> None:
        rows = await overrides.get_report_classification_overrides()
        assert {row["job_id"] for row in rows["details"]} == {"ov-alpha", "ov-beta"}

    async def test_test_name_filter_narrows_on_the_classification_row(
        self, overrides
    ) -> None:
        """The overrides query has no ``results`` alias: its EXISTS is on its own row."""
        rows = await overrides.get_report_classification_overrides(test_name="test_log")
        assert {row["job_id"] for row in rows["details"]} == {"ov-alpha", "ov-beta"}

        rows = await overrides.get_report_classification_overrides(
            test_name="other_test"
        )
        assert rows["details"] == []
