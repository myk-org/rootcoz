"""Tests for filter presets (#179): storage, API endpoints, role rules."""

import pytest
from fastapi.testclient import TestClient

from rootcoz import storage

_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}


@pytest.fixture
def _init_db(temp_db_path):
    import asyncio

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(storage, "DB_PATH", temp_db_path)
        asyncio.run(storage.init_db())
        yield


@pytest.fixture
def client(_init_db, temp_db_path):

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ADMIN_KEY", _ADMIN_KEY)
        mp.setenv("ROOTCOZ_ENCRYPTION_KEY", "test-encryption-key-for-hmac")
        mp.setenv("SECURE_COOKIES", "false")
        mp.setenv("DB_PATH", str(temp_db_path))
        mp.setenv("REQUIRE_APPROVAL", "false")
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        from rootcoz.main import app

        with TestClient(app) as c:
            yield c
        get_settings.cache_clear()


def _register(client: TestClient, username: str) -> dict[str, str]:
    """Register a user and return session cookies."""
    resp = client.post("/api/auth/register", json={"username": username})
    assert resp.status_code == 200, resp.text
    session = resp.cookies.get("rootcoz_session", "")
    assert session, "Registration should set rootcoz_session cookie"
    return {"rootcoz_session": session}


def _set_role(client: TestClient, username: str, role: str) -> None:
    """Set a user's role via the admin API (no-op 400 when unchanged)."""
    resp = client.put(
        f"/api/admin/users/{username}/role", json={"role": role}, headers=_ADMIN_HEADERS
    )
    if resp.status_code == 400 and "already has role" in resp.text:
        return
    assert resp.status_code == 200, resp.text


def _register_with_role(client: TestClient, username: str, role: str) -> dict[str, str]:
    """Register, set the role, then re-login — role changes invalidate the
    registration session (the fresh login carries the new role)."""
    resp = client.post("/api/auth/register", json={"username": username})
    assert resp.status_code == 200, resp.text
    api_key = resp.json()["api_key"]
    _set_role(client, username, role)
    login = client.post(
        "/api/auth/login", json={"username": username, "api_key": api_key}
    )
    assert login.status_code == 200, login.text
    session = login.cookies.get("rootcoz_session", "")
    assert session, "Login should set rootcoz_session cookie"
    return {"rootcoz_session": session}


def _create(client: TestClient, cookies: dict[str, str], **overrides) -> object:
    body = {
        "name": "My preset",
        "page": "dashboard",
        "scope": "user",
        "filters": {"team": ["storage"], "review_status": "unreviewed"},
    }
    body.update(overrides)
    return client.post("/api/user/filter-presets", json=body, cookies=cookies)


class TestFilterPresetsCRUD:
    def test_create_and_list_personal(self, client):
        cookies = _register(client, "alice")
        resp = _create(client, cookies, name="Storage unreviewed")
        assert resp.status_code == 201, resp.text
        preset = resp.json()
        assert preset["scope"] == "user"
        assert preset["page"] == "dashboard"
        assert preset["filters"] == {"team": ["storage"], "review_status": "unreviewed"}

        resp = client.get("/api/user/filter-presets?page=dashboard", cookies=cookies)
        assert resp.status_code == 200
        listed = resp.json()
        assert [p["name"] for p in listed] == ["Storage unreviewed"]

    def test_list_page_scoping(self, client):
        cookies = _register(client, "alice")
        _create(client, cookies, name="Dash preset", page="dashboard")
        _create(client, cookies, name="Reports preset", page="reports")

        dash = client.get(
            "/api/user/filter-presets?page=dashboard", cookies=cookies
        ).json()
        reports = client.get(
            "/api/user/filter-presets?page=reports", cookies=cookies
        ).json()
        assert [p["name"] for p in dash] == ["Dash preset"]
        assert [p["name"] for p in reports] == ["Reports preset"]

    def test_list_invalid_page_422(self, client):
        cookies = _register(client, "alice")
        resp = client.get("/api/user/filter-presets?page=other", cookies=cookies)
        assert resp.status_code == 422

    def test_create_conflict_409(self, client):
        cookies = _register(client, "alice")
        assert _create(client, cookies, name="Same").status_code == 201
        resp = _create(client, cookies, name="Same")
        assert resp.status_code == 409

    def test_same_name_ok_across_users_and_scopes(self, client):
        """Personal namespaces are per user; server is a separate namespace."""
        alice = _register_with_role(client, "alice", "operator")
        bob = _register(client, "bob")
        assert (
            _create(client, alice, name="Shared name", scope="server").status_code
            == 201
        )
        # Bob's personal preset may share the server preset's name
        assert _create(client, bob, name="Shared name").status_code == 201
        # But a second server preset with the same name conflicts
        resp = _create(client, alice, name="Shared name", scope="server")
        assert resp.status_code == 409

    def test_create_limit_422(self, client):
        cookies = _register(client, "alice")
        for i in range(storage.MAX_FILTER_PRESETS):
            resp = _create(client, cookies, name=f"Preset {i}")
            assert resp.status_code == 201
        resp = _create(client, cookies, name="One too many")
        assert resp.status_code == 422
        assert "Maximum" in resp.json()["detail"]

    def test_unknown_filter_key_422(self, client):
        cookies = _register(client, "alice")
        resp = _create(client, cookies, filters={"bogus": "x"})
        assert resp.status_code == 422
        assert "bogus" in resp.json()["detail"]

    def test_reports_date_keys_ok_dashboard_date_keys_rejected(self, client):
        cookies = _register(client, "alice")
        assert (
            _create(
                client,
                cookies,
                page="reports",
                filters={"from": "2026-01-01", "to": "2026-02-01"},
            ).status_code
            == 201
        )
        # date_from/date_to are dashboard keys — reports rejects them
        resp = _create(
            client, cookies, page="reports", filters={"date_from": "2026-01-01"}
        )
        assert resp.status_code == 422

    def test_empty_and_whitespace_name_422(self, client):
        cookies = _register(client, "alice")
        resp = _create(client, cookies, name="   ")
        assert resp.status_code == 422

    def test_name_stripped_on_save(self, client):
        cookies = _register(client, "alice")
        resp = _create(client, cookies, name="  Padded  ")
        assert resp.status_code == 201
        assert resp.json()["name"] == "Padded"

    def test_update_rename_and_filters(self, client):
        cookies = _register(client, "alice")
        preset_id = _create(client, cookies, name="Old name").json()["id"]

        resp = client.put(
            f"/api/user/filter-presets/{preset_id}",
            json={"name": "New name"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New name"
        assert resp.json()["filters"] == {
            "team": ["storage"],
            "review_status": "unreviewed",
        }

        resp = client.put(
            f"/api/user/filter-presets/{preset_id}",
            json={"filters": {"tier": ["2"]}},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New name"
        assert resp.json()["filters"] == {"tier": ["2"]}

    def test_update_name_conflict_409(self, client):
        cookies = _register(client, "alice")
        first = _create(client, cookies, name="First").json()["id"]
        _create(client, cookies, name="Second")
        resp = client.put(
            f"/api/user/filter-presets/{first}",
            json={"name": "Second"},
            cookies=cookies,
        )
        assert resp.status_code == 409

    def test_delete_own_preset(self, client):
        cookies = _register(client, "alice")
        preset_id = _create(client, cookies, name="Doomed").json()["id"]
        resp = client.delete(f"/api/user/filter-presets/{preset_id}", cookies=cookies)
        assert resp.status_code == 204
        listed = client.get(
            "/api/user/filter-presets?page=dashboard", cookies=cookies
        ).json()
        assert listed == []

    def test_delete_missing_404(self, client):
        cookies = _register(client, "alice")
        resp = client.delete("/api/user/filter-presets/999", cookies=cookies)
        assert resp.status_code == 404


class TestFilterPresetRoles:
    def test_auth_required(self, client):
        assert client.get("/api/user/filter-presets?page=dashboard").status_code == 401

    def test_viewer_can_manage_own_presets(self, client):
        cookies = _register_with_role(client, "viewy", "viewer")
        resp = _create(client, cookies, name="Mine")
        assert resp.status_code == 201
        preset_id = resp.json()["id"]
        assert (
            client.put(
                f"/api/user/filter-presets/{preset_id}",
                json={"name": "Renamed"},
                cookies=cookies,
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"/api/user/filter-presets/{preset_id}", cookies=cookies
            ).status_code
            == 204
        )

    def test_server_scope_forbidden_below_operator(self, client):
        for role in ("viewer", "reviewer"):
            cookies = _register_with_role(client, f"{role}user", role)
            resp = _create(client, cookies, name="Server try", scope="server")
            assert resp.status_code == 403, f"{role} should not create server presets"

    def test_operator_and_admin_create_server_preset(self, client):
        op = _register_with_role(client, "opuser", "operator")
        resp = _create(client, op, name="Team view", scope="server")
        assert resp.status_code == 201

        # Server preset appears in every user's list
        bob = _register(client, "bob")
        listed = client.get(
            "/api/user/filter-presets?page=dashboard", cookies=bob
        ).json()
        assert [p["name"] for p in listed] == ["Team view"]

        # Admin can also create one
        resp = client.post(
            "/api/user/filter-presets",
            json={
                "name": "Admin view",
                "page": "dashboard",
                "scope": "server",
                "filters": {},
            },
            headers=_ADMIN_HEADERS,
        )
        assert resp.status_code == 201

    def test_server_preset_mutation_requires_operator(self, client):
        op = _register_with_role(client, "opuser", "operator")
        server_id = _create(client, op, name="Server preset", scope="server").json()[
            "id"
        ]

        reviewer = _register_with_role(client, "revuser", "reviewer")
        assert (
            client.put(
                f"/api/user/filter-presets/{server_id}",
                json={"name": "Nope"},
                cookies=reviewer,
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/api/user/filter-presets/{server_id}", cookies=reviewer
            ).status_code
            == 403
        )

        # Operator (not the creator — a second operator) can update and delete
        op2 = _register_with_role(client, "opuser2", "operator")
        assert (
            client.put(
                f"/api/user/filter-presets/{server_id}",
                json={"name": "Renamed"},
                cookies=op2,
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"/api/user/filter-presets/{server_id}", cookies=op2
            ).status_code
            == 204
        )

    def test_other_users_personal_preset_404(self, client):
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        preset_id = _create(client, alice, name="Alice only").json()["id"]
        assert (
            client.put(
                f"/api/user/filter-presets/{preset_id}",
                json={"name": "Hax"},
                cookies=bob,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/user/filter-presets/{preset_id}", cookies=bob
            ).status_code
            == 404
        )
        # Alice still sees her preset
        listed = client.get(
            "/api/user/filter-presets?page=dashboard", cookies=alice
        ).json()
        assert [p["name"] for p in listed] == ["Alice only"]


class TestFilterPresetStorage:
    def test_limit_constant(self):
        assert storage.MAX_FILTER_PRESETS == 20

    def test_list_orders_newest_first(self, client):
        cookies = _register(client, "alice")
        for name in ("Old", "Middle", "New"):
            _create(client, cookies, name=name)
        listed = client.get(
            "/api/user/filter-presets?page=dashboard", cookies=cookies
        ).json()
        assert [p["name"] for p in listed] == ["New", "Middle", "Old"]


class TestFilterPresetHardening:
    """Fixes from the PR #355 review cycle."""

    def test_oversized_filter_value_422(self, client):
        cookies = _register(client, "alice")
        resp = _create(client, cookies, name="Big value", filters={"team": ["x" * 201]})
        assert resp.status_code == 422, resp.text
        resp = _create(client, cookies, name="Big value", filters={"team": "x" * 201})
        assert resp.status_code == 422, resp.text

    def test_oversized_filter_list_422(self, client):
        cookies = _register(client, "alice")
        resp = _create(
            client,
            cookies,
            name="Big list",
            filters={"team": [f"t{i}" for i in range(101)]},
        )
        assert resp.status_code == 422, resp.text

    def test_oversized_total_json_422(self, client):
        cookies = _register(client, "alice")
        # 50 keys of 200-char values stays under the per-value cap but
        # exceeds the 10k serialized budget.
        filters = {f"k{i:02d}": "x" * 200 for i in range(50)}
        resp = _create(client, cookies, name="Big blob", filters=filters)
        assert resp.status_code == 422, resp.text

    def test_update_oversized_filters_422(self, client):
        cookies = _register(client, "alice")
        preset_id = _create(client, cookies, name="Base").json()["id"]
        resp = client.put(
            f"/api/user/filter-presets/{preset_id}",
            json={"filters": {"team": "x" * 201}},
            cookies=cookies,
        )
        assert resp.status_code == 422, resp.text

    def test_out_of_range_preset_id_404_not_500(self, client):
        cookies = _register(client, "alice")
        huge = 2**63  # above SQLite's signed 64-bit range
        assert (
            client.put(
                f"/api/user/filter-presets/{huge}",
                json={"name": "X"},
                cookies=cookies,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/user/filter-presets/{huge}", cookies=cookies
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/api/user/filter-presets/0", json={"name": "X"}, cookies=cookies
            ).status_code
            == 404
        )

    def test_probing_other_users_preset_with_invalid_body_404(self, client):
        # Invalid update body must NOT reveal whether another user's
        # personal preset id exists (404 for both, never 422).
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        preset_id = _create(client, alice, name="Alice only").json()["id"]
        assert (
            client.put(
                f"/api/user/filter-presets/{preset_id}",
                json={"filters": {"nonsense_key": "x"}},
                cookies=bob,
            ).status_code
            == 404
        )

    def test_dashboard_analysis_state_key_accepted(self, client):
        cookies = _register(client, "alice")
        resp = _create(
            client,
            cookies,
            name="Analyzed only",
            filters={"analysis_state": "analyzed"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["filters"] == {"analysis_state": "analyzed"}

    def test_response_schema_and_headers(self, client):
        cookies = _register(client, "alice")
        resp = _create(client, cookies, name="Schema")
        assert resp.status_code == 201
        body = resp.json()
        assert set(body) == {
            "id",
            "name",
            "page",
            "scope",
            "filters",
            "created_at",
            "updated_at",
        }
        listed = client.get("/api/user/filter-presets?page=dashboard", cookies=cookies)
        assert listed.headers.get("cache-control") == "no-store"
        assert listed.json()[0]["name"] == "Schema"

    def test_openapi_operation_ids_match_handlers(self, client):
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert "updateFilterPresetEndpoint" == str(
            paths["/api/user/filter-presets/{preset_id}"]["put"]["operationId"]
        )
        assert "deleteFilterPresetEndpoint" == str(
            paths["/api/user/filter-presets/{preset_id}"]["delete"]["operationId"]
        )
