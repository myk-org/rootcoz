"""Tests for the exporter plugin architecture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rootcoz.exporters.base import (
    ExportContext,
    Exporter,
    ExporterPrerequisiteError,
    ExporterResult,
)


class TestExportContext:
    """Tests for ExportContext dataclass."""

    def test_minimal_construction(self):
        """ExportContext can be created with required fields only."""
        ctx = ExportContext(
            job_id="job-1",
            job_name="my-job",
            build_number="42",
            jenkins_url="https://jenkins.example.com/job/my-job/42/",
            failures=[{"test_name": "test_a", "error": "boom"}],
            report_url="https://rootcoz.example.com/results/job-1",
        )
        assert ctx.job_id == "job-1"
        assert ctx.pushed_by == ""
        assert ctx.history_classifications == {}
        assert ctx.tracked_in_links == {}
        assert ctx.reviewed_by == {}
        assert ctx.child_job_name is None
        assert ctx.child_build_number is None

    def test_full_construction(self):
        """ExportContext can be created with all fields."""
        ctx = ExportContext(
            job_id="job-1",
            job_name="my-job",
            build_number="42",
            jenkins_url="https://jenkins.example.com/job/my-job/42/",
            failures=[{"test_name": "test_a"}],
            report_url="https://rootcoz.example.com/results/job-1",
            child_job_name="child-job",
            child_build_number=5,
            pushed_by="admin",
            history_classifications={"test_a": "INFRASTRUCTURE"},
            tracked_in_links={
                "test_a": [{"tracked_in_url": "https://jira.example.com/PROJ-123"}]
            },
            reviewed_by={"test_a": "reviewer1"},
        )
        assert ctx.child_job_name == "child-job"
        assert ctx.child_build_number == 5
        assert ctx.pushed_by == "admin"


class TestExporterResult:
    """Tests for ExporterResult dataclass."""

    def test_success_result(self):
        result = ExporterResult(success=True, message="Pushed 5 items")
        assert result.success is True
        assert result.details == {}

    def test_failure_result_with_details(self):
        result = ExporterResult(
            success=False,
            message="Push failed",
            details={"pushed": 0, "errors": ["connection timeout"]},
        )
        assert result.success is False
        assert result.details["errors"] == ["connection timeout"]


class TestExporterABC:
    """Tests for the Exporter abstract base class."""

    def test_cannot_instantiate_directly(self):
        """Exporter ABC cannot be instantiated."""
        with pytest.raises(TypeError):
            Exporter()

    def test_subclass_must_implement_all_methods(self):
        """Incomplete subclass raises TypeError."""

        class IncompleteExporter(Exporter):
            @property
            def name(self):
                return "test"

        with pytest.raises(TypeError):
            IncompleteExporter()

    def test_complete_subclass(self):
        """Complete subclass can be instantiated."""

        class TestExporter(Exporter):
            @property
            def name(self):
                return "test"

            @property
            def display_name(self):
                return "Test Exporter"

            @property
            def is_enabled(self):
                return True

            async def push(self, context):
                return ExporterResult(success=True, message="ok")

        exporter = TestExporter()
        assert exporter.name == "test"
        assert exporter.display_name == "Test Exporter"
        assert exporter.is_enabled is True


class TestCreateExporter:
    """Tests for _create_exporter() factory in main.py."""

    def test_create_reportportal_exporter(self):
        """Factory creates ReportPortalClient for 'reportportal' plugin."""
        from rootcoz.exporters.reportportal import ReportPortalClient

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.url = "https://rp.example.com"
        settings.rp.api_token.get_secret_value.return_value = "test-token"
        settings.rp.project = "test-project"
        settings.rp.verify_ssl = True
        settings.rp.push_classifications = True
        settings.rp.push_rootcoz_url = True
        settings.rp.push_tracker_links = True

        from rootcoz.main import _create_exporter

        with (
            patch("rootcoz.exporters.reportportal.RPClient"),
            patch(
                "rootcoz.main._extract_base_url",
                return_value="https://rootcoz.example.com",
            ),
        ):
            exporter = _create_exporter("reportportal", settings)
            assert isinstance(exporter, ReportPortalClient)
            exporter.close()

    def test_create_reportportal_disabled_raises(self):
        """Factory raises ValueError when Report Portal is disabled."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.reportportal_enabled = False
        with pytest.raises(ValueError, match="disabled"):
            _create_exporter("reportportal", settings)

    def test_create_reportportal_missing_public_base_url(self):
        """Factory raises ValueError when push_rootcoz_url but no PUBLIC_BASE_URL."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.push_rootcoz_url = True
        with (
            patch("rootcoz.main._extract_base_url", return_value=""),
            pytest.raises(ValueError, match="PUBLIC_BASE_URL"),
        ):
            _create_exporter("reportportal", settings)

    def test_create_unknown_plugin_raises(self):
        """Factory raises ValueError for unknown plugin name."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        with pytest.raises(ValueError, match="Unknown exporter plugin"):
            _create_exporter("nonexistent", settings)

    def test_create_reportportal_missing_url(self):
        """Factory raises ValueError when RP URL is missing."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.url = None
        settings.rp.push_rootcoz_url = False
        with pytest.raises(ValueError, match="reportportal_url is required"):
            _create_exporter("reportportal", settings)

    def test_create_reportportal_missing_token(self):
        """Factory raises ValueError when RP token is missing."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.url = "https://rp.example.com"
        settings.rp.api_token = None
        settings.rp.push_rootcoz_url = False
        with pytest.raises(ValueError, match="reportportal_api_token is required"):
            _create_exporter("reportportal", settings)

    def test_create_reportportal_empty_token(self):
        """Factory raises ValueError when RP token is empty."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.url = "https://rp.example.com"
        settings.rp.api_token.get_secret_value.return_value = ""
        settings.rp.push_rootcoz_url = False
        with pytest.raises(ValueError, match="reportportal_api_token is required"):
            _create_exporter("reportportal", settings)

    def test_create_reportportal_missing_project(self):
        """Factory raises ValueError when RP project is missing."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.url = "https://rp.example.com"
        settings.rp.api_token.get_secret_value.return_value = "test-token"
        settings.rp.project = None
        settings.rp.push_rootcoz_url = False
        with pytest.raises(ValueError, match="reportportal_project is required"):
            _create_exporter("reportportal", settings)

    def test_create_reportportal_all_toggles_disabled(self):
        """Factory raises when RP is configured but all push content toggles are off."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.push_classifications = False
        settings.rp.push_rootcoz_url = False
        settings.rp.push_tracker_links = False
        with pytest.raises(ValueError, match="push content toggles are disabled"):
            _create_exporter("reportportal", settings)

    def test_available_exporters_disabled_when_all_toggles_off(self):
        """List exporters reports RP disabled when all content toggles are off."""
        from rootcoz.main import _available_exporters

        settings = MagicMock()
        settings.reportportal_enabled = True
        settings.rp.push_classifications = False
        settings.rp.push_rootcoz_url = False
        settings.rp.push_tracker_links = False
        exporters = _available_exporters(settings)
        rp = next(e for e in exporters if e.name == "reportportal")
        assert rp.enabled is False

    def test_create_greenwave_disabled_raises(self):
        """Factory raises ValueError when Greenwave is disabled."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = False
        with pytest.raises(ValueError, match="disabled"):
            _create_exporter("greenwave", settings)

    def test_create_greenwave_enabled_token(self):
        """Factory creates GreenwaveExporter with token auth."""
        from rootcoz.exporters.greenwave_exporter import GreenwaveExporter
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = True
        settings.greenwave_url = "https://gw.example.com"
        settings.greenwave_resultsdb_auth_method = "token"
        settings.greenwave_api_token.get_secret_value.return_value = "token"
        settings.greenwave_waiver_auth_method = "oidc"
        settings.greenwave_push_waivers = False

        exporter = _create_exporter("greenwave", settings)
        assert isinstance(exporter, GreenwaveExporter)
        assert exporter.name == "greenwave"

    def test_create_greenwave_push_waivers_requires_product_version(self):
        """Factory raises when push_waivers is enabled but product_version is missing."""
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = True
        settings.greenwave_url = "https://gw.example.com"
        settings.greenwave_push_waivers = True
        settings.greenwave_product_version = None

        with pytest.raises(
            ExporterPrerequisiteError, match="GREENWAVE_PRODUCT_VERSION"
        ):
            _create_exporter("greenwave", settings)

    def test_create_greenwave_kerberos(self):
        """Factory creates GreenwaveExporter with kerberos auth."""
        from rootcoz.exporters.greenwave_exporter import GreenwaveExporter
        from rootcoz.main import _create_exporter

        settings = MagicMock()
        settings.greenwave_enabled = True
        settings.greenwave_url = "https://gw.example.com"
        settings.greenwave_resultsdb_auth_method = "kerberos"
        settings.greenwave_kerberos_keytab = "/etc/keytab"
        settings.greenwave_kerberos_principal = "user@REALM"
        settings.greenwave_waiver_auth_method = "kerberos"
        settings.greenwave_push_waivers = False

        # Token not required for kerberos
        settings.greenwave_api_token = None

        exporter = _create_exporter("greenwave", settings)
        assert isinstance(exporter, GreenwaveExporter)
        assert exporter.name == "greenwave"


class TestExporterEndpoints:
    """Tests for the generic exporter API endpoints."""

    @pytest.fixture
    def temp_db_path(self, tmp_path):
        return tmp_path / "test.db"

    @pytest.fixture
    def _init_db(self, temp_db_path):
        import asyncio

        from rootcoz import storage

        with patch.object(storage, "DB_PATH", temp_db_path):
            asyncio.run(storage.init_db())
            yield

    def test_list_exporters(self, _init_db, temp_db_path):
        """GET /api/exporters returns exporter list."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            response = client.get("/api/exporters")
            assert response.status_code == 200
            data = response.json()
            assert isinstance(data, list)
            assert len(data) >= 1
            rp = next(e for e in data if e["name"] == "reportportal")
            assert rp["display_name"] == "Report Portal"
            assert "enabled" in rp

    def test_push_to_exporter_openapi_documents_json_options(
        self, _init_db, temp_db_path
    ):
        from tests.conftest import make_app_client

        for client in make_app_client(temp_db_path):
            operation = client.get("/openapi.json").json()["paths"][
                "/results/{job_id}/push/{plugin_name}"
            ]["post"]
            assert operation["requestBody"]["content"]["application/json"]
            parameters = {item["name"]: item for item in operation["parameters"]}
            assert "subject_identifier" not in parameters
            assert "waiver_comment" not in parameters

    def test_push_to_exporter_unknown_plugin(self, _init_db, temp_db_path):
        """POST /push/{plugin_name} returns 400 for unknown plugin."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            # First create a job so the 404 check passes
            with patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = {"result": {"failures": []}}
                response = client.post("/results/job-1/push/nonexistent")
            assert response.status_code == 400
            assert "Unknown exporter plugin" in response.json()["detail"]

    def test_push_to_exporter_invalid_plugin_name(self, _init_db, temp_db_path):
        """POST /push/{plugin_name} returns 422 for invalid plugin name pattern."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            response = client.post("/results/job-1/push/INVALID!")
            assert response.status_code == 422

    def test_push_to_exporter_job_not_found(self, _init_db, temp_db_path):
        """POST /push/{plugin_name} returns 404 when job doesn't exist."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = None
                response = client.post("/results/job-1/push/reportportal")
            assert response.status_code == 404

    def test_push_to_exporter_success(self, _init_db, temp_db_path):
        """POST /push/{plugin_name} dispatches to exporter and returns result."""
        from tests.conftest import admin_login, make_app_client

        mock_exporter = MagicMock()
        mock_exporter.push = AsyncMock(
            return_value=ExporterResult(
                success=True,
                message="Pushed 3 items",
                details={"pushed": 3, "errors": [], "launch_id": 42},
            )
        )
        mock_exporter.__enter__ = MagicMock(return_value=mock_exporter)
        mock_exporter.__exit__ = MagicMock(return_value=False)

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch("rootcoz.main._create_exporter", return_value=mock_exporter),
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_ctx,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_ctx.return_value = ExportContext(
                    job_id="job-1",
                    job_name="test-job",
                    build_number="1",
                    jenkins_url="https://jenkins.example.com/job/test-job/1/",
                    failures=[{"test_name": "t"}],
                    report_url="https://rootcoz.example.com/results/job-1",
                )
                response = client.post("/results/job-1/push/reportportal")

            assert response.status_code == 200
            data = response.json()
            assert data["pushed"] == 3
            assert data["launch_id"] == 42

    def test_push_to_exporter_viewer_forbidden(self, _init_db, temp_db_path):
        """POST /push/{plugin_name} returns 403 for viewer role."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            # Register as viewer
            reg = client.post("/api/auth/register", json={"username": "viewer1"})
            api_key = reg.json().get("api_key", "")
            # Login
            client.post(
                "/api/auth/login",
                json={"username": "viewer1", "api_key": api_key},
            )
            # Set role to viewer via admin
            admin_login(client)
            client.put(
                "/api/admin/users/viewer1/role",
                json={"role": "viewer"},
            )
            # Login as viewer
            client.post(
                "/api/auth/login",
                json={"username": "viewer1", "api_key": api_key},
            )
            response = client.post("/results/job-1/push/reportportal")
            assert response.status_code == 403

    def test_push_to_exporter_details_cannot_overwrite_envelope(
        self, _init_db, temp_db_path
    ):
        """Details keys must not overwrite standardized success/message fields."""
        from tests.conftest import admin_login, make_app_client

        mock_exporter = MagicMock()
        mock_exporter.push = AsyncMock(
            return_value=ExporterResult(
                success=True,
                message="Pushed 1 item",
                details={
                    "pushed": 1,
                    "success": False,
                    "message": "overwritten",
                },
            )
        )
        mock_exporter.__enter__ = MagicMock(return_value=mock_exporter)
        mock_exporter.__exit__ = MagicMock(return_value=False)

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch("rootcoz.main._create_exporter", return_value=mock_exporter),
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_ctx,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_ctx.return_value = ExportContext(
                    job_id="job-1",
                    job_name="test-job",
                    build_number="1",
                    jenkins_url="https://jenkins.example.com/job/test-job/1/",
                    failures=[{"test_name": "t"}],
                    report_url="https://rootcoz.example.com/results/job-1",
                )
                response = client.post("/results/job-1/push/reportportal")

            assert response.status_code == 200
            data = response.json()
            # Standardized fields win over details
            assert data["success"] is True
            assert data["message"] == "Pushed 1 item"
            # Details field is still present
            assert data["pushed"] == 1

    def test_push_to_exporter_unexpected_error_returns_502(
        self, _init_db, temp_db_path
    ):
        """An unexpected exporter.push() failure surfaces as a controlled 502."""
        from tests.conftest import admin_login, make_app_client

        mock_exporter = MagicMock()
        mock_exporter.push = AsyncMock(side_effect=RuntimeError("boom"))
        mock_exporter.__enter__ = MagicMock(return_value=mock_exporter)
        mock_exporter.__exit__ = MagicMock(return_value=False)

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch("rootcoz.main._create_exporter", return_value=mock_exporter),
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_ctx,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_ctx.return_value = ExportContext(
                    job_id="job-1",
                    job_name="test-job",
                    build_number="1",
                    jenkins_url="https://jenkins.example.com/job/test-job/1/",
                    failures=[{"test_name": "t"}],
                    report_url="https://rootcoz.example.com/results/job-1",
                )
                response = client.post("/results/job-1/push/reportportal")

            assert response.status_code == 502
            assert "failed unexpectedly" in response.json()["detail"]

    def test_push_to_exporter_prerequisite_error_returns_422(
        self, _init_db, temp_db_path
    ):
        """ExporterPrerequisiteError raised inside push() maps to HTTP 422, not 502.

        Greenwave raises ExporterPrerequisiteError when push_waivers or
        auto_push is enabled and subject_identifier is missing.  The generic
        push endpoint must catch this BEFORE the broad Exception handler so
        callers receive a 422 (client error) not a misleading 502.
        """
        from tests.conftest import admin_login, make_app_client

        mock_exporter = MagicMock()
        mock_exporter.push = AsyncMock(
            side_effect=ExporterPrerequisiteError(
                "subject_identifier is required when push_waivers is enabled"
            )
        )
        mock_exporter.__enter__ = MagicMock(return_value=mock_exporter)
        mock_exporter.__exit__ = MagicMock(return_value=False)

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch("rootcoz.main._create_exporter", return_value=mock_exporter),
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_ctx,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_ctx.return_value = ExportContext(
                    job_id="job-1",
                    job_name="test-job",
                    build_number="1",
                    jenkins_url="https://jenkins.example.com/job/test-job/1/",
                    failures=[{"test_name": "t"}],
                    report_url="https://rootcoz.example.com/results/job-1",
                )
                response = client.post("/results/job-1/push/greenwave")

            assert response.status_code == 422
            assert "subject_identifier" in response.json()["detail"]

    def test_push_greenwave_dispatches_subject_identifier(self, _init_db, temp_db_path):
        """Greenwave endpoint dispatches and preserves the requested subject."""
        from tests.conftest import admin_login, make_app_client

        mock_exporter = MagicMock()
        mock_exporter.push = AsyncMock(
            return_value=ExporterResult(
                success=True,
                message="Pushed 1 result",
                details={"pushed": 1, "errors": []},
            )
        )
        mock_exporter.__enter__ = MagicMock(return_value=mock_exporter)
        mock_exporter.__exit__ = MagicMock(return_value=False)

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._create_exporter", return_value=mock_exporter
                ) as mock_create,
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_context,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_context.return_value = ExportContext(
                    job_id="job-1",
                    job_name="test-job",
                    build_number="1",
                    jenkins_url="",
                    failures=[{"test_name": "t"}],
                    report_url="",
                    subject_identifier="build-nvr-1",
                )
                response = client.post(
                    "/results/job-1/push/greenwave",
                    json={"subject_identifier": "build-nvr-1"},
                )

            assert response.status_code == 200
            mock_create.assert_called_once()
            assert mock_create.call_args.args[0] == "greenwave"
            assert mock_context.await_args.kwargs["subject_identifier"] == "build-nvr-1"

    def test_push_greenwave_validates_json_option_lengths(self, _init_db, temp_db_path):
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            response = client.post(
                "/results/job-1/push/greenwave",
                json={"waiver_comment": "x" * 501},
            )
            assert response.status_code == 422

    def test_push_greenwave_rejects_options_in_query_string(
        self, _init_db, temp_db_path
    ):
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            response = client.post(
                "/results/job-1/push/greenwave?waiver_comment=private"
            )
            assert response.status_code == 400
            assert "JSON request body" in response.json()["detail"]

    def test_push_greenwave_disabled_returns_400(self, _init_db, temp_db_path):
        """Disabled Greenwave capability is explicit at the endpoint."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_context,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_context.return_value = MagicMock()
                response = client.post("/results/job-1/push/greenwave")

            assert response.status_code == 400
            assert "disabled" in response.json()["detail"]

    def test_push_greenwave_missing_product_version_returns_400(
        self, _init_db, temp_db_path
    ):
        """Missing product version with push_waivers=True: greenwave_enabled returns False -> 400.

        Finding #5: greenwave_enabled now validates WaiverDB prereqs (including
        GREENWAVE_PRODUCT_VERSION) when GREENWAVE_PUSH_WAIVERS=true, so the
        endpoint returns 400 'disabled' rather than deferring to the exporter
        constructor which would return 422.
        """
        from tests.conftest import admin_login, make_app_client

        env = {
            "ENABLE_GREENWAVE": "true",
            "GREENWAVE_URL": "https://resultsdb.example.com/api/v2.0",
            "GREENWAVE_API_TOKEN": "results-token",  # pragma: allowlist secret
            "GREENWAVE_PUSH_WAIVERS": "true",
            "GREENWAVE_WAIVER_URL": "https://waiverdb.example.com/api/v1.0",
            "GREENWAVE_WAIVER_TOKEN": "waiver-token",  # pragma: allowlist secret
            # GREENWAVE_PRODUCT_VERSION intentionally omitted
        }
        for client in make_app_client(temp_db_path, env):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_context,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_context.return_value = MagicMock()
                response = client.post("/results/job-1/push/greenwave")

            # greenwave_enabled now returns False when product_version is missing
            # with push_waivers=True (Finding #5), so the endpoint returns 400.
            assert response.status_code == 400
            assert "disabled" in response.json()["detail"]

    def test_push_greenwave_job_not_found(self, _init_db, temp_db_path):
        """Greenwave endpoint returns 404 before attempting external writes."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = None
                response = client.post("/results/missing/push/greenwave")
            assert response.status_code == 404

    def test_push_to_exporter_constructor_oserror_returns_502(
        self, _init_db, temp_db_path
    ):
        """Transport/constructor failures from _create_exporter map to 502."""
        from tests.conftest import admin_login, make_app_client

        for client in make_app_client(temp_db_path):
            admin_login(client)
            with (
                patch("rootcoz.main.get_result", new_callable=AsyncMock) as mock_get,
                patch(
                    "rootcoz.main._create_exporter",
                    side_effect=OSError("connection refused"),
                ),
                patch(
                    "rootcoz.main._build_export_context", new_callable=AsyncMock
                ) as mock_ctx,
                patch("rootcoz.main.logger") as mock_logger,
            ):
                mock_get.return_value = {"result": {"failures": [{"test_name": "t"}]}}
                mock_ctx.return_value = ExportContext(
                    job_id="job-1",
                    job_name="test-job",
                    build_number="1",
                    jenkins_url="https://jenkins.example.com/job/test-job/1/",
                    failures=[{"test_name": "t"}],
                    report_url="https://rootcoz.example.com/results/job-1",
                )
                response = client.post("/results/job-1/push/reportportal")

            assert response.status_code == 502
            assert response.json()["detail"] == (
                "Exporter 'reportportal' could not be initialized"
            )
            mock_logger.error.assert_called_once_with(
                "Exporter '%s' initialization failed for job %s: %s",
                "reportportal",
                "job-1",
                "OSError",
            )


class TestExporterNeedsHistory:
    """Tests for the needs_history_classifications capability + gating."""

    def test_base_default_is_false(self):
        assert Exporter.needs_history_classifications is False

    def test_reportportal_needs_history(self):
        from rootcoz.exporters.reportportal import ReportPortalClient

        assert ReportPortalClient.needs_history_classifications is True

    def test_helper_true_for_reportportal(self):
        from rootcoz.main import _exporter_needs_history_classifications

        assert _exporter_needs_history_classifications(["reportportal"]) is True

    def test_helper_false_for_unknown(self):
        from rootcoz.main import _exporter_needs_history_classifications

        assert _exporter_needs_history_classifications(["nonexistent"]) is False

    def test_helper_false_for_empty(self):
        from rootcoz.main import _exporter_needs_history_classifications

        assert _exporter_needs_history_classifications([]) is False

    def test_helper_true_if_any_matches(self):
        from rootcoz.main import _exporter_needs_history_classifications

        assert (
            _exporter_needs_history_classifications(["nonexistent", "reportportal"])
            is True
        )


class TestBuildExportContextFetchHistory:
    """_build_export_context honours the fetch_history flag."""

    def _run_build(self, fetch_history):
        import asyncio

        from rootcoz.main import _build_export_context

        result_data = {
            "job_name": "j",
            "jenkins_url": "https://jenkins.example.com/job/j/1/",
            "failures": [{"test_name": "t1"}, {"test_name": "t2"}],
        }
        with (
            patch(
                "rootcoz.main._extract_base_url",
                return_value="https://rc.example.com",
            ),
            patch(
                "rootcoz.main.storage.get_all_effective_classifications",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "rootcoz.main.get_history_classification", new_callable=AsyncMock
            ) as mock_hist,
            patch(
                "rootcoz.storage.get_tracked_in_for_scope",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "rootcoz.storage.get_reviews_for_job",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            mock_hist.return_value = "PRODUCT BUG"
            ctx = asyncio.run(
                _build_export_context(
                    job_id="job-1",
                    result_data=result_data,
                    fetch_history=fetch_history,
                )
            )
        return ctx, mock_hist

    def test_fetch_history_true_populates_classifications(self):
        ctx, mock_hist = self._run_build(True)
        assert mock_hist.await_count == 2
        assert ctx.history_classifications == {
            "t1": "PRODUCT BUG",
            "t2": "PRODUCT BUG",
        }

    def test_fetch_history_false_skips_lookups(self):
        ctx, mock_hist = self._run_build(False)
        assert mock_hist.await_count == 0
        assert ctx.history_classifications == {}


class TestBuildExportContextEffectiveClassifications:
    """Export context uses authoritative classification overrides."""

    def test_child_scope_applies_current_effective_classification(self):
        import asyncio

        from rootcoz.main import _build_export_context

        result_data = {
            "job_name": "parent",
            "child_job_analyses": [
                {
                    "job_name": "child",
                    "build_number": 7,
                    "failures": [
                        {
                            "test_name": "test_a",
                            "analysis": {
                                "classification": "INFRASTRUCTURE",
                                "details": "stale snapshot",
                            },
                        }
                    ],
                }
            ],
        }
        with (
            patch(
                "rootcoz.main.storage.get_all_effective_classifications",
                new_callable=AsyncMock,
                return_value={("test_a", "child", 7): "PRODUCT BUG"},
            ),
            patch(
                "rootcoz.main.storage.get_reviews_for_job",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            context = asyncio.run(
                _build_export_context(
                    job_id="job-1",
                    result_data=result_data,
                    child_job_name="child",
                    child_build_number=7,
                    fetch_history=False,
                    fetch_tracked_in_links=False,
                )
            )

        assert context.failures[0]["analysis"]["classification"] == "PRODUCT BUG"
        assert context.child_job_name == "child"
        assert context.child_build_number == 7


class TestExporterNeedsTrackedInLinks:
    """Tests for the needs_tracked_in_links capability + gating."""

    def test_base_default_is_false(self):
        assert Exporter.needs_tracked_in_links is False

    def test_reportportal_needs_tracked_in(self):
        from rootcoz.exporters.reportportal import ReportPortalClient

        assert ReportPortalClient.needs_tracked_in_links is True

    def test_helper_true_when_rp_toggle_on(self):
        from rootcoz.main import _exporter_needs_tracked_in_links

        settings = MagicMock()
        settings.rp.push_tracker_links = True
        assert _exporter_needs_tracked_in_links(["reportportal"], settings) is True

    def test_helper_false_when_rp_toggle_off(self):
        from rootcoz.main import _exporter_needs_tracked_in_links

        settings = MagicMock()
        settings.rp.push_tracker_links = False
        assert _exporter_needs_tracked_in_links(["reportportal"], settings) is False

    def test_helper_false_for_unknown(self):
        from rootcoz.main import _exporter_needs_tracked_in_links

        settings = MagicMock()
        settings.rp.push_tracker_links = True
        assert _exporter_needs_tracked_in_links(["nonexistent"], settings) is False


class TestBuildExportContextFetchTrackedIn:
    """_build_export_context honours the fetch_tracked_in_links flag."""

    def _run_build(self, fetch_tracked_in_links):
        import asyncio

        from rootcoz.main import _build_export_context

        result_data = {
            "job_name": "j",
            "jenkins_url": "https://jenkins.example.com/job/j/1/",
            "failures": [{"test_name": "t1"}],
        }
        with (
            patch(
                "rootcoz.main._extract_base_url",
                return_value="https://rc.example.com",
            ),
            patch(
                "rootcoz.main.storage.get_all_effective_classifications",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "rootcoz.main.get_history_classification",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "rootcoz.main.storage.get_tracked_in_for_scope",
                new_callable=AsyncMock,
            ) as mock_tracked,
            patch(
                "rootcoz.main.storage.get_reviews_for_job",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            mock_tracked.return_value = {"t1": [{"url": "https://example.com/1"}]}
            ctx = asyncio.run(
                _build_export_context(
                    job_id="job-1",
                    result_data=result_data,
                    fetch_history=False,
                    fetch_tracked_in_links=fetch_tracked_in_links,
                )
            )
        return ctx, mock_tracked

    def test_fetch_tracked_in_true_calls_storage(self):
        ctx, mock_tracked = self._run_build(True)
        assert mock_tracked.await_count == 1
        assert "t1" in ctx.tracked_in_links

    def test_fetch_tracked_in_false_skips_storage(self):
        ctx, mock_tracked = self._run_build(False)
        assert mock_tracked.await_count == 0
        assert ctx.tracked_in_links == {}


class TestExporterModel:
    """Tests for ExporterInfo Pydantic model."""

    def test_exporter_info_serialization(self):
        from rootcoz.models import ExporterInfo

        info = ExporterInfo(
            name="reportportal", display_name="Report Portal", enabled=True
        )
        d = info.model_dump()
        assert d == {
            "name": "reportportal",
            "display_name": "Report Portal",
            "enabled": True,
        }


class TestExporterPushOptionsControlCharNormalization:
    """Finding #4: ExporterPushOptions.normalize_optional_text_validator must strip control chars.

    A control-char-only subject_identifier like '\x00' must normalize to None
    so it does not pass the ``if not context.subject_identifier`` guard and
    reach ResultsDB/WaiverDB gating writes as garbage.
    """

    def test_null_byte_subject_identifier_normalizes_to_none(self):
        """subject_identifier='\\x00' must normalize to None."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(subject_identifier="\x00")
        assert opts.subject_identifier is None

    def test_control_chars_only_subject_identifier_normalizes_to_none(self):
        """subject_identifier='\\x01\\x02' must normalize to None."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(subject_identifier="\x01\x02")
        assert opts.subject_identifier is None

    def test_whitespace_only_subject_identifier_normalizes_to_none(self):
        """whitespace-only subject_identifier must normalize to None (existing behaviour)."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(subject_identifier="   ")
        assert opts.subject_identifier is None

    def test_mixed_control_and_text_subject_identifier_strips_control_chars(self):
        """Embedded control chars are stripped; the remaining text is kept."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(subject_identifier="build\x00nvr")
        assert opts.subject_identifier == "buildnvr"

    def test_valid_subject_identifier_preserved(self):
        """Normal ASCII subject_identifier passes through unchanged."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(subject_identifier="hco-bundle-v4.20.0-240")
        assert opts.subject_identifier == "hco-bundle-v4.20.0-240"

    def test_null_byte_waiver_comment_normalizes_to_none(self):
        """waiver_comment='\\x00' must normalize to None."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(waiver_comment="\x00")
        assert opts.waiver_comment is None

    def test_control_chars_stripped_from_waiver_comment(self):
        """Control chars embedded in waiver_comment are stripped."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(waiver_comment="ok\x01comment")
        assert opts.waiver_comment == "okcomment"

    def test_none_subject_identifier_stays_none(self):
        """None input must remain None (no crash)."""
        from rootcoz.models import ExporterPushOptions

        opts = ExporterPushOptions(subject_identifier=None)
        assert opts.subject_identifier is None
