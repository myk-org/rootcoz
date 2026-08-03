"""Tests for issues #198–#201: sparse fields, can_view_reports, public OpenAPI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from rootcoz import storage
from rootcoz.result_fields import (
    RESULT_FIELD_PATHS,
    filter_result_fields,
    parse_fields_param,
)
from tests.conftest import make_app_client

_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}


@pytest.fixture
def _init_db(temp_db_path: Path):
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def client(_init_db, temp_db_path: Path):
    yield from make_app_client(temp_db_path)


def _register_and_login(client: TestClient, username: str) -> tuple[str, dict]:
    resp = client.post("/api/auth/register", json={"username": username})
    assert resp.status_code == 200
    api_key = resp.json()["api_key"]
    login = client.post(
        "/api/auth/login", json={"username": username, "api_key": api_key}
    )
    assert login.status_code == 200
    return api_key, {"rootcoz_session": login.cookies["rootcoz_session"]}


class TestPublicOpenAPIPaths:
    def test_issue_specific_operation_ids_present(self, client: TestClient) -> None:
        """Issue-specific operationIds (uniqueness covered in test_main.TestOpenAPISchema)."""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        ops = [
            op["operationId"]
            for path_item in resp.json()["paths"].values()
            for op in path_item.values()
            if isinstance(op, dict) and "operationId" in op
        ]
        assert "getJobResult" in ops
        assert "listResultFields" in ops
        assert "setUserCanViewReports" in ops


class TestResultFieldsHelper:
    def test_parse_unknown_fields(self) -> None:
        with pytest.raises(ValueError, match="Unknown field"):
            parse_fields_param("status,not_a_real_field")

    def test_filter_top_level_and_nested(self) -> None:
        data = {
            "job_id": "j1",
            "status": "completed",
            "error": "",
            "result": {
                "summary": "ok",
                "ai_provider": "claude",
                "failures": [
                    {
                        "id": "f1",
                        "test_name": "t1",
                        "error": "boom",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "long details",
                        },
                    }
                ],
            },
            "tracked_in": {},
        }
        out = filter_result_fields(
            data, ["status", "result.summary", "result.failures.test_name"]
        )
        assert out == {
            "status": "completed",
            "result": {
                "summary": "ok",
                "failures": [{"test_name": "t1"}],
            },
        }

    def test_full_failures_wins_over_projection(self) -> None:
        data = {
            "result": {
                "failures": [{"id": "f1", "test_name": "t1", "error": "e"}],
            }
        }
        out = filter_result_fields(data, ["result.failures", "result.failures.id"])
        assert out["result"]["failures"][0]["error"] == "e"

    def test_nested_null_included_when_present(self) -> None:
        data = {"result": {"summary": None, "ai_provider": "claude"}}
        out = filter_result_fields(data, ["result.summary", "result.ai_provider"])
        assert out == {
            "result": {"summary": None, "ai_provider": "claude"},
        }

    def test_test_count_fields_allowlisted(self) -> None:
        assert "result.passed_count" in RESULT_FIELD_PATHS
        assert "result.skipped_count" in RESULT_FIELD_PATHS
        assert "result.failed_count" in RESULT_FIELD_PATHS
        data = {
            "result": {
                "passed_count": 10,
                "skipped_count": 2,
                "failed_count": 1,
            }
        }
        out = filter_result_fields(
            data,
            ["result.passed_count", "result.skipped_count", "result.failed_count"],
        )
        assert out == {
            "result": {
                "passed_count": 10,
                "skipped_count": 2,
                "failed_count": 1,
            }
        }

    def test_classification_alias_from_analysis(self) -> None:
        data = {
            "result": {
                "failures": [
                    {
                        "id": "f1",
                        "analysis": {"classification": "PRODUCT BUG"},
                    }
                ]
            }
        }
        out = filter_result_fields(data, ["result.failures.classification"])
        assert out["result"]["failures"] == [{"classification": "PRODUCT BUG"}]


class TestSparseResultFieldsEndpoint:
    async def test_fields_param_and_list_endpoint(
        self, client: TestClient, temp_db_path: Path
    ) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.save_result(
                job_id="sparse-1",
                jenkins_url="https://jenkins.example.com/job/t/1/",
                status="completed",
                result={
                    "summary": "Done",
                    "ai_provider": "claude",
                    "failures": [
                        {
                            "id": "fail-1",
                            "test_name": "test_a",
                            "error": "AssertionError",
                            "analysis": {
                                "classification": "CODE ISSUE",
                                "details": "fix it",
                            },
                        }
                    ],
                },
            )

        list_resp = client.get("/api/results/fields", headers=_ADMIN_HEADERS)
        assert list_resp.status_code == 200
        assert set(list_resp.json()["fields"]) == set(RESULT_FIELD_PATHS)

        bad = client.get(
            "/results/sparse-1",
            params={"fields": "nope"},
            headers=_ADMIN_HEADERS,
        )
        assert bad.status_code == 400
        assert "Unknown field" in bad.json()["detail"]

        sparse = client.get(
            "/results/sparse-1",
            params={"fields": "status,result.summary,result.failures.test_name"},
            headers=_ADMIN_HEADERS,
        )
        assert sparse.status_code == 200
        body = sparse.json()
        assert set(body.keys()) == {"status", "result"}
        assert body["status"] == "completed"
        assert body["result"]["summary"] == "Done"
        assert body["result"]["failures"] == [{"test_name": "test_a"}]

        alias = client.get(
            "/results/sparse-1",
            params={"fields": "result.failures.classification"},
            headers=_ADMIN_HEADERS,
        )
        assert alias.status_code == 200
        assert alias.json()["result"]["failures"] == [{"classification": "CODE ISSUE"}]


class TestCanViewReports:
    async def test_storage_flag_default_and_set(self, temp_db_path: Path) -> None:
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.create_user("reporter", status="active", role="reviewer")
            user = await storage.get_user_by_username("reporter")
            assert user is not None
            assert user["can_view_reports"] is False

            ok = await storage.set_user_can_view_reports("reporter", True)
            assert ok is True
            user = await storage.get_user_by_username("reporter")
            assert user["can_view_reports"] is True

            users = await storage.list_users()
            match = next(u for u in users if u["username"] == "reporter")
            assert match["can_view_reports"] is True

    async def test_create_user_key_gen_preserves_can_view_reports(
        self, temp_db_path: Path
    ) -> None:
        """Existing-user key generation must not reset can_view_reports."""
        with patch.object(storage, "DB_PATH", temp_db_path):
            await storage.init_db()
            await storage.track_user("tracked_flag")
            await storage.set_user_can_view_reports("tracked_flag", True)
            user = await storage.get_user_by_username("tracked_flag")
            assert user["can_view_reports"] is True
            assert not await storage.user_has_key("tracked_flag")

            _, api_key = await storage.create_user("tracked_flag")
            assert api_key.startswith("rootcoz_")
            user = await storage.get_user_by_username("tracked_flag")
            assert user["can_view_reports"] is True

    def test_auth_me_and_reports_access(self, client: TestClient) -> None:
        _api_key, cookies = _register_and_login(client, "reportuser")

        me = client.get("/api/auth/me", cookies=cookies)
        assert me.status_code == 200
        assert me.json()["can_view_reports"] is False

        denied = client.get("/api/reports/totals", cookies=cookies)
        assert denied.status_code == 403

        set_resp = client.put(
            "/api/admin/users/reportuser/can-view-reports",
            headers=_ADMIN_HEADERS,
            json={"can_view_reports": True},
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["can_view_reports"] is True

        # Same session cookies — middleware reloads flag from DB (no re-login).
        me2 = client.get("/api/auth/me", cookies=cookies)
        assert me2.status_code == 200
        assert me2.json()["can_view_reports"] is True

        allowed = client.get("/api/reports/totals", cookies=cookies)
        assert allowed.status_code == 200

        revoke = client.put(
            "/api/admin/users/reportuser/can-view-reports",
            headers=_ADMIN_HEADERS,
            json={"can_view_reports": False},
        )
        assert revoke.status_code == 200
        assert revoke.json()["can_view_reports"] is False

        denied_again = client.get("/api/reports/totals", cookies=cookies)
        assert denied_again.status_code == 403
        me3 = client.get("/api/auth/me", cookies=cookies)
        assert me3.status_code == 200
        assert me3.json()["can_view_reports"] is False

        admin_me = client.get("/api/auth/me", headers=_ADMIN_HEADERS)
        assert admin_me.status_code == 200
        assert admin_me.json()["is_admin"] is True
        assert admin_me.json()["can_view_reports"] is True

        admin_ok = client.get("/api/reports/totals", headers=_ADMIN_HEADERS)
        assert admin_ok.status_code == 200

    def test_login_returns_can_view_reports(self, client: TestClient) -> None:
        resp = client.post("/api/auth/register", json={"username": "loginflag"})
        assert resp.status_code == 200
        api_key = resp.json()["api_key"]

        login = client.post(
            "/api/auth/login",
            json={"username": "loginflag", "api_key": api_key},
        )
        assert login.status_code == 200
        assert login.json()["can_view_reports"] is False

        client.put(
            "/api/admin/users/loginflag/can-view-reports",
            headers=_ADMIN_HEADERS,
            json={"can_view_reports": True},
        )
        login2 = client.post(
            "/api/auth/login",
            json={"username": "loginflag", "api_key": api_key},
        )
        assert login2.status_code == 200
        assert login2.json()["can_view_reports"] is True

        admin_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "api_key": _ADMIN_KEY},
        )
        assert admin_login.status_code == 200
        assert admin_login.json()["can_view_reports"] is True

    def test_admin_create_with_can_view_reports(self, client: TestClient) -> None:
        resp = client.post(
            "/api/admin/users/create",
            headers=_ADMIN_HEADERS,
            json={
                "username": "withreports",
                "role": "reviewer",
                "can_view_reports": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_view_reports"] is True

        listed = client.get("/api/admin/users", headers=_ADMIN_HEADERS)
        assert listed.status_code == 200
        user = next(u for u in listed.json()["users"] if u["username"] == "withreports")
        assert user["can_view_reports"] is True

        login = client.post(
            "/api/auth/login",
            json={"username": "withreports", "api_key": body["api_key"]},
        )
        assert login.status_code == 200
        assert login.json()["can_view_reports"] is True
        cookies = {"rootcoz_session": login.cookies["rootcoz_session"]}
        totals = client.get("/api/reports/totals", cookies=cookies)
        assert totals.status_code == 200

    def test_admin_create_response_effective_for_admin_role(
        self, client: TestClient
    ) -> None:
        """Create response returns effective can_view_reports; DB keeps stored flag."""
        resp = client.post(
            "/api/admin/users/create",
            headers=_ADMIN_HEADERS,
            json={
                "username": "adminflag",
                "role": "admin",
                "can_view_reports": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "admin"
        assert body["can_view_reports"] is True  # effective, like /me

        listed = client.get("/api/admin/users", headers=_ADMIN_HEADERS)
        user = next(u for u in listed.json()["users"] if u["username"] == "adminflag")
        assert user["can_view_reports"] is False  # stored flag stays False

        me = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {body['api_key']}"}
        )
        assert me.status_code == 200
        assert me.json()["can_view_reports"] is True

    def test_admin_create_rejects_string_false(self, client: TestClient) -> None:
        resp = client.post(
            "/api/admin/users/create",
            headers=_ADMIN_HEADERS,
            json={
                "username": "strfalse",
                "role": "reviewer",
                "can_view_reports": "false",
            },
        )
        assert resp.status_code == 422
