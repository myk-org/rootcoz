"""Tests for job metadata storage, API endpoints, and CLI commands."""

import json as json_mod
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from jenkins_job_insight import storage
from jenkins_job_insight.cli.config import ServerConfig
from jenkins_job_insight.cli.main import app as cli_app
from tests.conftest import CLI_TEST_BASE_URL, make_test_client


@pytest.fixture
async def setup_test_db(temp_db_path: Path):
    """Set up a test database with the path patched."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        await storage.init_db()
        yield temp_db_path


# --- Storage tests ---


class TestJobMetadataStorage:
    """Tests for job_metadata CRUD in storage.py."""

    async def test_set_and_get_metadata(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.set_job_metadata(
                "my-job",
                team="platform",
                tier="critical",
                version="v1.0",
                labels=["nightly", "smoke"],
            )
            assert result["job_name"] == "my-job"
            assert result["team"] == "platform"
            assert result["labels"] == ["nightly", "smoke"]

            fetched = await storage.get_job_metadata("my-job")
            assert fetched is not None
            assert fetched["team"] == "platform"
            assert fetched["tier"] == "critical"
            assert fetched["labels"] == ["nightly", "smoke"]

    async def test_get_metadata_not_found(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            result = await storage.get_job_metadata("nonexistent")
            assert result is None

    async def test_update_metadata(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("my-job", team="alpha")
            await storage.set_job_metadata("my-job", team="beta", tier="low")
            fetched = await storage.get_job_metadata("my-job")
            assert fetched is not None
            assert fetched["team"] == "beta"
            assert fetched["tier"] == "low"

    async def test_delete_metadata(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("my-job", team="platform")
            deleted = await storage.delete_job_metadata("my-job")
            assert deleted is True
            assert await storage.get_job_metadata("my-job") is None

    async def test_delete_metadata_not_found(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            deleted = await storage.delete_job_metadata("nonexistent")
            assert deleted is False

    async def test_list_all_metadata(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("job-a", team="alpha")
            await storage.set_job_metadata("job-b", team="beta")
            await storage.set_job_metadata("job-c", team="alpha")

            all_items = await storage.list_jobs_with_metadata()
            assert len(all_items) == 3

    async def test_list_filter_by_team(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("job-a", team="alpha")
            await storage.set_job_metadata("job-b", team="beta")
            await storage.set_job_metadata("job-c", team="alpha")

            filtered = await storage.list_jobs_with_metadata(team="alpha")
            assert len(filtered) == 2
            assert all(j["team"] == "alpha" for j in filtered)

    async def test_list_filter_by_tier(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("job-a", tier="critical")
            await storage.set_job_metadata("job-b", tier="low")

            filtered = await storage.list_jobs_with_metadata(tier="critical")
            assert len(filtered) == 1
            assert filtered[0]["job_name"] == "job-a"

    async def test_list_filter_by_version(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("job-a", version="v1.0")
            await storage.set_job_metadata("job-b", version="v2.0")

            filtered = await storage.list_jobs_with_metadata(version="v1.0")
            assert len(filtered) == 1
            assert filtered[0]["job_name"] == "job-a"

    async def test_list_filter_by_labels(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("job-a", labels=["nightly", "smoke"])
            await storage.set_job_metadata("job-b", labels=["nightly"])
            await storage.set_job_metadata("job-c", labels=["regression"])

            # Filter by single label
            filtered = await storage.list_jobs_with_metadata(labels=["nightly"])
            assert len(filtered) == 2

            # Filter by multiple labels (AND logic)
            filtered = await storage.list_jobs_with_metadata(
                labels=["nightly", "smoke"]
            )
            assert len(filtered) == 1
            assert filtered[0]["job_name"] == "job-a"

    async def test_list_filter_combined(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata(
                "job-a", team="alpha", tier="critical", labels=["nightly"]
            )
            await storage.set_job_metadata(
                "job-b", team="alpha", tier="low", labels=["nightly"]
            )
            await storage.set_job_metadata(
                "job-c", team="beta", tier="critical", labels=["nightly"]
            )

            filtered = await storage.list_jobs_with_metadata(
                team="alpha", tier="critical"
            )
            assert len(filtered) == 1
            assert filtered[0]["job_name"] == "job-a"

    async def test_bulk_set_metadata(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            items = [
                {"job_name": "job-a", "team": "alpha", "labels": ["smoke"]},
                {"job_name": "job-b", "team": "beta", "tier": "critical"},
            ]
            result = await storage.bulk_set_metadata(items)
            assert result["updated"] == 2

            a = await storage.get_job_metadata("job-a")
            assert a is not None
            assert a["team"] == "alpha"
            assert a["labels"] == ["smoke"]

            b = await storage.get_job_metadata("job-b")
            assert b is not None
            assert b["team"] == "beta"
            assert b["tier"] == "critical"

    async def test_metadata_with_none_fields(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("my-job", team="alpha")
            fetched = await storage.get_job_metadata("my-job")
            assert fetched is not None
            assert fetched["team"] == "alpha"
            assert fetched["tier"] is None
            assert fetched["version"] is None
            assert fetched["labels"] == []

    async def test_metadata_empty_labels(self, setup_test_db: Path) -> None:
        with patch.object(storage, "DB_PATH", setup_test_db):
            await storage.set_job_metadata("my-job", labels=[])
            fetched = await storage.get_job_metadata("my-job")
            assert fetched is not None
            assert fetched["labels"] == []


# --- API endpoint tests ---


_ADMIN_KEY = "test-admin-key-16chars"  # pragma: allowlist secret


@pytest.fixture
def mock_settings(temp_db_path):
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "GEMINI_API_KEY": "test-key",  # pragma: allowlist secret
        "ADMIN_KEY": _ADMIN_KEY,
        "DB_PATH": str(temp_db_path),
    }
    with patch.dict(os.environ, env, clear=True):
        from jenkins_job_insight.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def api_client(mock_settings, temp_db_path: Path):
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient
        from jenkins_job_insight.main import app

        with TestClient(app) as client:
            # Inject admin auth header for mutation endpoints
            _original_put = client.put
            _original_delete = client.delete

            def _put_with_auth(*args, **kwargs):
                kwargs.setdefault("headers", {})["Authorization"] = (
                    f"Bearer {_ADMIN_KEY}"
                )
                return _original_put(*args, **kwargs)

            def _delete_with_auth(*args, **kwargs):
                kwargs.setdefault("headers", {})["Authorization"] = (
                    f"Bearer {_ADMIN_KEY}"
                )
                return _original_delete(*args, **kwargs)

            client.put = _put_with_auth
            client.delete = _delete_with_auth
            yield client


class TestJobMetadataAPI:
    """Tests for job metadata API endpoints."""

    def test_set_and_get_metadata(self, api_client) -> None:
        resp = api_client.put(
            "/api/jobs/my-job/metadata",
            json={"team": "platform", "tier": "critical", "labels": ["smoke"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_name"] == "my-job"
        assert data["team"] == "platform"

        resp = api_client.get("/api/jobs/my-job/metadata")
        assert resp.status_code == 200
        assert resp.json()["team"] == "platform"

    def test_get_metadata_not_found(self, api_client) -> None:
        resp = api_client.get("/api/jobs/nonexistent/metadata")
        assert resp.status_code == 404

    def test_delete_metadata(self, api_client) -> None:
        api_client.put("/api/jobs/my-job/metadata", json={"team": "alpha"})
        resp = api_client.delete("/api/jobs/my-job/metadata")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        resp = api_client.get("/api/jobs/my-job/metadata")
        assert resp.status_code == 404

    def test_delete_metadata_not_found(self, api_client) -> None:
        resp = api_client.delete("/api/jobs/nonexistent/metadata")
        assert resp.status_code == 404

    def test_list_metadata(self, api_client) -> None:
        api_client.put("/api/jobs/job-a/metadata", json={"team": "alpha"})
        api_client.put("/api/jobs/job-b/metadata", json={"team": "beta"})

        resp = api_client.get("/api/jobs/metadata")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_list_metadata_filter_by_team(self, api_client) -> None:
        api_client.put("/api/jobs/job-a/metadata", json={"team": "alpha"})
        api_client.put("/api/jobs/job-b/metadata", json={"team": "beta"})

        resp = api_client.get("/api/jobs/metadata", params={"team": "alpha"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["team"] == "alpha"

    def test_list_metadata_filter_by_label(self, api_client) -> None:
        api_client.put(
            "/api/jobs/job-a/metadata",
            json={"labels": ["nightly", "smoke"]},
        )
        api_client.put(
            "/api/jobs/job-b/metadata",
            json={"labels": ["nightly"]},
        )

        resp = api_client.get(
            "/api/jobs/metadata", params={"label": ["nightly", "smoke"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["job_name"] == "job-a"

    def test_bulk_import(self, api_client) -> None:
        resp = api_client.put(
            "/api/jobs/metadata/bulk",
            json={
                "items": [
                    {"job_name": "job-a", "team": "alpha"},
                    {"job_name": "job-b", "team": "beta", "labels": ["ci"]},
                ]
            },
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2

        resp = api_client.get("/api/jobs/job-b/metadata")
        assert resp.status_code == 200
        assert resp.json()["labels"] == ["ci"]

    def test_dashboard_filtered_no_filters(self, api_client) -> None:
        resp = api_client.get("/api/dashboard/filtered")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_dashboard_filtered_by_metadata(self, api_client, temp_db_path) -> None:
        """Verify dashboard filter returns only jobs matching metadata."""
        import asyncio

        async def _seed():
            with patch.object(storage, "DB_PATH", temp_db_path):
                await storage.save_result(
                    "id-alpha",
                    "https://jenkins.example.com/job/alpha-job/1",
                    "completed",
                    {"job_name": "alpha-job", "build_number": 1, "failures": []},
                )
                await storage.save_result(
                    "id-beta",
                    "https://jenkins.example.com/job/beta-job/2",
                    "completed",
                    {"job_name": "beta-job", "build_number": 2, "failures": []},
                )

        asyncio.run(_seed())

        # Attach metadata only to alpha-job
        api_client.put("/api/jobs/alpha-job/metadata", json={"team": "alpha"})
        api_client.put("/api/jobs/beta-job/metadata", json={"team": "beta"})

        # Filter by team=alpha → only alpha-job returned
        resp = api_client.get("/api/dashboard/filtered", params={"team": "alpha"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["job_name"] == "alpha-job"
        assert data[0]["metadata"]["team"] == "alpha"

    def test_partial_update_preserves_omitted_fields(self, api_client) -> None:
        """PUT with a subset of fields should not clear the others."""
        api_client.put(
            "/api/jobs/my-job/metadata",
            json={"team": "alpha", "tier": "critical", "labels": ["nightly"]},
        )
        # Update only team — tier and labels should be preserved
        api_client.put("/api/jobs/my-job/metadata", json={"team": "beta"})
        resp = api_client.get("/api/jobs/my-job/metadata")
        assert resp.status_code == 200
        data = resp.json()
        assert data["team"] == "beta"
        assert data["tier"] == "critical"
        assert data["labels"] == ["nightly"]

    def test_folder_style_job_name(self, api_client) -> None:
        """Test that folder-style job names (with /) work correctly."""
        resp = api_client.put(
            "/api/jobs/folder/subfolder/my-job/metadata",
            json={"team": "platform"},
        )
        assert resp.status_code == 200
        assert resp.json()["job_name"] == "folder/subfolder/my-job"

        resp = api_client.get("/api/jobs/folder/subfolder/my-job/metadata")
        assert resp.status_code == 200
        assert resp.json()["team"] == "platform"


# --- Non-admin access tests ---


@pytest.fixture
def noauth_client(mock_settings, temp_db_path: Path):
    """Test client with NO admin credentials."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        from starlette.testclient import TestClient
        from jenkins_job_insight.main import app

        with TestClient(app) as client:
            yield client


class TestJobMetadataNoAdmin:
    """Mutation endpoints must return 403 without admin auth."""

    def test_put_metadata_forbidden(self, noauth_client) -> None:
        resp = noauth_client.put("/api/jobs/my-job/metadata", json={"team": "alpha"})
        assert resp.status_code == 403

    def test_delete_metadata_forbidden(self, noauth_client) -> None:
        resp = noauth_client.delete("/api/jobs/my-job/metadata")
        assert resp.status_code == 403

    def test_bulk_put_metadata_forbidden(self, noauth_client) -> None:
        resp = noauth_client.put(
            "/api/jobs/metadata/bulk",
            json={"items": [{"job_name": "j", "team": "t"}]},
        )
        assert resp.status_code == 403

    def test_get_metadata_allowed(self, noauth_client) -> None:
        """Read endpoints should work without admin auth."""
        resp = noauth_client.get("/api/jobs/metadata")
        assert resp.status_code == 200


# --- CLI client tests ---


def _metadata_handler(request: httpx.Request) -> httpx.Response:
    """Mock handler for metadata CLI client tests."""
    path = request.url.path
    method = request.method

    if method == "GET" and path == "/api/jobs/metadata":
        return httpx.Response(
            200,
            json=[
                {
                    "job_name": "job-a",
                    "team": "alpha",
                    "tier": None,
                    "version": None,
                    "labels": [],
                }
            ],
        )
    if method == "GET" and path == "/api/jobs/my-job/metadata":
        return httpx.Response(
            200,
            json={
                "job_name": "my-job",
                "team": "platform",
                "tier": "critical",
                "version": None,
                "labels": ["smoke"],
            },
        )
    if method == "PUT" and path == "/api/jobs/my-job/metadata":
        import json

        body = json.loads(request.content)
        body["job_name"] = "my-job"
        return httpx.Response(200, json=body)
    if method == "DELETE" and path == "/api/jobs/my-job/metadata":
        return httpx.Response(200, json={"status": "deleted", "job_name": "my-job"})
    if method == "PUT" and path == "/api/jobs/metadata/bulk":
        import json

        body = json.loads(request.content)
        return httpx.Response(200, json={"updated": len(body.get("items", []))})
    return httpx.Response(404, json={"detail": "Not found"})


class TestJobMetadataCLIClient:
    """Tests for job metadata CLI client methods."""

    def test_list_jobs_metadata(self) -> None:
        client = make_test_client(_metadata_handler)
        data = client.list_jobs_metadata()
        assert len(data) == 1
        assert data[0]["team"] == "alpha"

    def test_get_job_metadata(self) -> None:
        client = make_test_client(_metadata_handler)
        data = client.get_job_metadata("my-job")
        assert data["team"] == "platform"
        assert data["labels"] == ["smoke"]

    def test_set_job_metadata(self) -> None:
        client = make_test_client(_metadata_handler)
        data = client.set_job_metadata("my-job", team="platform", labels=["ci"])
        assert data["job_name"] == "my-job"

    def test_delete_job_metadata(self) -> None:
        client = make_test_client(_metadata_handler)
        data = client.delete_job_metadata("my-job")
        assert data["status"] == "deleted"

    def test_bulk_set_metadata(self) -> None:
        client = make_test_client(_metadata_handler)
        data = client.bulk_set_metadata(
            [
                {"job_name": "job-a", "team": "alpha"},
                {"job_name": "job-b", "team": "beta"},
            ]
        )
        assert data["updated"] == 2


# --- CLI command tests ---

runner = CliRunner()


class TestMetadataCLICommands:
    """Tests for metadata CLI commands."""

    @pytest.fixture(autouse=True)
    def mock_client(self):
        with (
            patch.dict(
                os.environ,
                {"JJI_SERVER": CLI_TEST_BASE_URL},
                clear=True,
            ),
            patch(
                "jenkins_job_insight.cli.main.get_server_config",
                return_value=ServerConfig(url=CLI_TEST_BASE_URL),
            ),
            patch("jenkins_job_insight.cli.main._get_client") as mock_get,
        ):
            client = MagicMock()
            mock_get.return_value = client
            self._mock_client = client
            yield client

    def test_metadata_list(self) -> None:
        self._mock_client.list_jobs_metadata.return_value = [
            {
                "job_name": "job-a",
                "team": "alpha",
                "tier": None,
                "version": None,
                "labels": [],
            }
        ]
        result = runner.invoke(cli_app, ["metadata", "list"])
        assert result.exit_code == 0
        assert "job-a" in result.output

    def test_metadata_get(self) -> None:
        self._mock_client.get_job_metadata.return_value = {
            "job_name": "my-job",
            "team": "platform",
            "tier": "critical",
            "version": None,
            "labels": [],
        }
        result = runner.invoke(cli_app, ["metadata", "get", "my-job"])
        assert result.exit_code == 0
        assert "platform" in result.output

    def test_metadata_set(self) -> None:
        self._mock_client.set_job_metadata.return_value = {
            "job_name": "my-job",
            "team": "alpha",
        }
        result = runner.invoke(
            cli_app, ["metadata", "set", "my-job", "--team", "alpha"]
        )
        assert result.exit_code == 0
        assert "Metadata set" in result.output

    def test_metadata_delete(self) -> None:
        self._mock_client.delete_job_metadata.return_value = {
            "status": "deleted",
            "job_name": "my-job",
        }
        result = runner.invoke(cli_app, ["metadata", "delete", "my-job"])
        assert result.exit_code == 0
        assert "Metadata deleted" in result.output

    def test_metadata_import_json(self, tmp_path) -> None:
        self._mock_client.bulk_set_metadata.return_value = {"updated": 2}

        f = tmp_path / "metadata.json"
        f.write_text(
            json_mod.dumps(
                [
                    {"job_name": "job-a", "team": "alpha"},
                    {"job_name": "job-b", "team": "beta"},
                ]
            )
        )

        result = runner.invoke(cli_app, ["metadata", "import", str(f)])
        assert result.exit_code == 0
        assert "Imported 2" in result.output

    def test_metadata_list_with_filters(self) -> None:
        self._mock_client.list_jobs_metadata.return_value = []
        result = runner.invoke(
            cli_app,
            ["metadata", "list", "--team", "alpha", "--tier", "critical"],
        )
        assert result.exit_code == 0
        self._mock_client.list_jobs_metadata.assert_called_once_with(
            team="alpha", tier="critical", version="", labels=None
        )

    def test_metadata_list_json(self) -> None:
        self._mock_client.list_jobs_metadata.return_value = [{"job_name": "j"}]
        result = runner.invoke(cli_app, ["metadata", "list", "--json"])
        assert result.exit_code == 0
        # JSON output should be parseable
        parsed = json_mod.loads(result.output)
        assert isinstance(parsed, list)
