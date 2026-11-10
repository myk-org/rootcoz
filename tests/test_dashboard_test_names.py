"""The failing-test-name typeahead endpoint (#546 B4)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz import storage
from tests.test_dashboard_test_name_filter import (  # noqa: F401 (fixtures)
    _ADMIN_KEY,
    _seed,
    api_client,
    mock_settings,
)


class TestDashboardTestNames:
    @pytest.fixture
    def seeded(self, api_client, temp_db_path):  # noqa: F811 (re-exported fixture)
        _seed(
            temp_db_path,
            [
                (
                    "job-a",
                    [
                        {"test_name": "tests.login.test_one", "status": "failed"},
                        {"test_name": "tests.login.test_one", "status": "failed"},
                        {"test_name": "tests.checkout.test_two", "status": "failed"},
                        {"test_name": "tests.login.green", "status": "passed"},
                    ],
                )
            ],
        )
        return api_client

    def test_returns_names_with_counts(self, seeded) -> None:
        resp = seeded.get("/api/dashboard/test-names", params={"q": "login"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["test_names"]
        # The passed entry never contributes, and the failing duplicate counts twice.
        assert rows == [{"test_name": "tests.login.test_one", "failure_count": 2}]

    def test_orders_by_count_desc(self, seeded) -> None:
        rows = seeded.get("/api/dashboard/test-names").json()["test_names"]
        assert [row["failure_count"] for row in rows] == sorted(
            (row["failure_count"] for row in rows), reverse=True
        )

    def test_limit_caps_the_rows(self, seeded) -> None:
        rows = seeded.get("/api/dashboard/test-names", params={"limit": 1}).json()[
            "test_names"
        ]
        assert len(rows) == 1

    def test_case_insensitive(self, seeded) -> None:
        rows = seeded.get("/api/dashboard/test-names", params={"q": "LOGIN"}).json()[
            "test_names"
        ]
        assert [row["test_name"] for row in rows] == ["tests.login.test_one"]

    def test_metacharacters_are_literal(self, api_client, temp_db_path) -> None:  # noqa: F811
        _seed(
            temp_db_path,
            [
                (
                    "job-a",
                    [
                        {"test_name": "100% done", "status": "failed"},
                        {"test_name": "100x done", "status": "failed"},
                        {"test_name": "a_b", "status": "failed"},
                        {"test_name": "axb", "status": "failed"},
                    ],
                )
            ],
        )
        rows = api_client.get("/api/dashboard/test-names", params={"q": "100%"}).json()[
            "test_names"
        ]
        assert [row["test_name"] for row in rows] == ["100% done"]
        rows = api_client.get("/api/dashboard/test-names", params={"q": "a_b"}).json()[
            "test_names"
        ]
        assert [row["test_name"] for row in rows] == ["a_b"]

    def test_empty_q_returns_the_top_names(self, seeded) -> None:
        rows = seeded.get("/api/dashboard/test-names", params={"q": ""}).json()[
            "test_names"
        ]
        assert len(rows) == 2

    def test_q_over_cap_is_422(self, seeded) -> None:
        resp = seeded.get("/api/dashboard/test-names", params={"q": "x" * 201})
        assert resp.status_code == 422

    def test_limit_bounds(self, seeded) -> None:
        assert (
            seeded.get("/api/dashboard/test-names", params={"limit": 0}).status_code
            == 422
        )
        assert (
            seeded.get("/api/dashboard/test-names", params={"limit": 101}).status_code
            == 422
        )

    def test_unauthenticated_is_401(self, seeded) -> None:
        from starlette.testclient import TestClient

        from rootcoz.main import app

        with TestClient(app) as anonymous:
            resp = anonymous.get("/api/dashboard/test-names")
        assert resp.status_code == 401


class TestFailedTestNameStorage:
    """Storage-level checks: the partial index exists and the plan can use it."""

    async def test_partial_index_exists(self, temp_db_path: Path) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with storage._connect_db() as db:
                cursor = await db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index'"
                    " AND name='idx_test_entries_failed_name'"
                )
                row = await cursor.fetchone()
        assert row is not None
        assert "WHERE status = 'failed'" in row["sql"]

    async def test_existing_db_gains_the_partial_index(
        self, temp_db_path: Path
    ) -> None:
        """An upgrade must create it too, not only a fresh DB (#546)."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            async with storage._connect_db() as db:
                await db.execute("DROP INDEX idx_test_entries_failed_name")
                await db.commit()
            await storage.init_db()
            async with storage._connect_db() as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                    " AND name='idx_test_entries_failed_name'"
                )
                row = await cursor.fetchone()
        assert row is not None

    async def test_failing_scope_is_a_sql_constant(self) -> None:
        """A constant, not a parameter, so the planner can use the index."""
        clause, _ = storage._failed_test_name_predicate("contains", "login")
        assert "te.status = 'failed'" in clause
        assert "te.status = ?" not in clause

    async def test_list_failed_test_names_rejects_zero_limit(
        self, temp_db_path: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            with pytest.raises(ValueError, match="limit must be >= 1"):
                await storage.list_failed_test_names("", 0)


def test_max_test_name_length_matches_the_preset_cap() -> None:
    """One 1000, stated in two modules that must not drift (#546 decision 13)."""
    from rootcoz import models

    assert storage.MAX_TEST_NAME_LENGTH == models.MAX_FILTER_PRESET_VALUE_LENGTH


def test_submitted_by_escapes_metacharacters() -> None:
    """The one LIKE escaper, used by _submitted_by_predicate (#546 blocker 3)."""
    clause, params = storage._submitted_by_predicate({"a%b_c\\d"})
    assert params == ["%a\\%b\\_c\\\\d%"]
    assert "ESCAPE" in clause
