"""Tests for Report Portal API endpoint and auto-push hook."""

import os
from types import MethodType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from rootcoz.exporters.reportportal import ReportPortalClient


def _install_rp_client_mock(mock_rp_class, mock_rp=None):
    """Wire a mocked ReportPortalClient so real push() orchestration runs.

    Sets constructor side_effect to copy push-* toggle kwargs onto the mock
    and binds :meth:`ReportPortalClient.push` for end-to-end endpoint tests.
    """
    if mock_rp is None:
        mock_rp = MagicMock()
    mock_rp.__enter__ = MagicMock(return_value=mock_rp)
    mock_rp.__exit__ = MagicMock(return_value=False)
    mock_rp.push = MethodType(ReportPortalClient.push, mock_rp)
    mock_rp._failure_result = ReportPortalClient._failure_result

    def _factory(*_args, **kwargs):
        mock_rp._push_classifications = kwargs.get("push_classifications", True)
        mock_rp._push_rootcoz_url = kwargs.get("push_rootcoz_url", True)
        mock_rp._push_tracker_links = kwargs.get("push_tracker_links", True)
        return mock_rp

    mock_rp_class.side_effect = _factory
    return mock_rp


@pytest.fixture(autouse=True)
def _mock_storage_reviews():
    """Auto-mock storage helpers used during RP push context building."""
    with (
        patch(
            "rootcoz.main.storage.get_reviews_for_job",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "rootcoz.main.storage.get_tracked_in_for_scope",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "rootcoz.main.get_history_classification",
            new_callable=AsyncMock,
            return_value="",
        ),
    ):
        yield


@pytest.fixture
def _rp_disabled_env():
    """Environment with RP disabled."""
    env = {
        "JENKINS_URL": "https://jenkins.example.com",
        "JENKINS_USER": "testuser",
        "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
        "AI_PROVIDER": "claude",
        "AI_MODEL": "test-model",
        "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
        "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
        "REQUIRE_APPROVAL": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


_BASE_RP_ENABLED_ENV = {
    "JENKINS_URL": "https://jenkins.example.com",
    "JENKINS_USER": "testuser",
    "JENKINS_PASSWORD": "testpassword",  # pragma: allowlist secret
    "AI_PROVIDER": "claude",
    "AI_MODEL": "test-model",
    "REPORTPORTAL_URL": "http://rp.example.com",
    "REPORTPORTAL_API_TOKEN": "rp-token",  # pragma: allowlist secret
    "REPORTPORTAL_PROJECT": "my-project",
    "PUBLIC_BASE_URL": "https://rootcoz.example.com",
    "ADMIN_KEY": "test-admin-key-16chars",  # pragma: allowlist secret
    "ROOTCOZ_ENCRYPTION_KEY": "test-encryption-key-for-hmac",  # pragma: allowlist secret
    "REQUIRE_APPROVAL": "false",
}


@pytest.fixture
def _rp_enabled_env():
    """Environment with RP enabled."""
    with patch.dict(os.environ, _BASE_RP_ENABLED_ENV, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


@pytest.fixture
def _rp_enabled_all_push_disabled_env():
    """Environment with RP enabled but all push content toggles disabled."""
    env = {
        **_BASE_RP_ENABLED_ENV,
        "RP_PUSH_CLASSIFICATIONS": "false",
        "RP_PUSH_ROOTCOZ_URL": "false",
        "RP_PUSH_TRACKER_LINKS": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


@pytest.fixture
def _rp_enabled_partial_push_env():
    """Environment with RP enabled and partial push toggles."""
    env = {
        **_BASE_RP_ENABLED_ENV,
        "RP_PUSH_CLASSIFICATIONS": "true",
        "RP_PUSH_ROOTCOZ_URL": "false",
        "RP_PUSH_TRACKER_LINKS": "true",
    }
    with patch.dict(os.environ, env, clear=True):
        from rootcoz.config import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


class TestPushReportPortalEndpoint:
    """Test POST /results/{job_id}/push-reportportal."""

    def test_returns_400_when_rp_disabled(self, _rp_disabled_env):
        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 400
        detail = response.json()["detail"].lower()
        assert "disabled" in detail or "not configured" in detail

    @patch("rootcoz.main.get_result")
    def test_returns_404_when_job_not_found(self, mock_get_result, _rp_enabled_env):
        mock_get_result.return_value = None
        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/nonexistent-id/push-reportportal")
        assert response.status_code == 404

    @patch(
        "rootcoz.main.ReportPortalClient",
    )
    @patch("rootcoz.main.get_result")
    def test_returns_422_on_invalid_stored_failures(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        mock_get_result.return_value = {
            "result": {
                "failures": [{"bad_field": "not_a_valid_failure"}],
                "jenkins_url": "http://jenkins.example.com/job/test/1/",
                "job_name": "test-job",
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = [{"id": 1, "name": "test_a"}]

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/corrupt-job/push-reportportal")
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "validation error" in detail.lower()

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_returns_result_on_success(
        self, mock_rp_class, mock_get_result, mock_get_cls, _rp_enabled_env
    ):
        mock_get_cls.return_value = ""
        # Mock stored result
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "my-job",
                "build_number": 42,
                "jenkins_url": "https://jenkins.example.com/job/my-job/42/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "Bug found",
                        },
                    }
                ],
            },
        }
        # Mock RP client (supports context manager protocol)
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 100
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_a", "status": "FAILED"}
        ]
        mock_rp.match_failures.return_value = [
            ({"id": 1, "name": "test_a"}, MagicMock(test_name="test_a"))
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 100,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()
        assert data["pushed"] == 1
        mock_rp.__exit__.assert_called_once()

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_infrastructure_classification_passed_to_rp(
        self, mock_rp_class, mock_get_result, mock_get_cls, _rp_enabled_env
    ):
        """INFRASTRUCTURE history classification maps to RP System Issue."""
        mock_get_cls.return_value = "INFRASTRUCTURE"
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "my-job",
                "build_number": 42,
                "jenkins_url": "https://jenkins.example.com/job/my-job/42/",
                "failures": [
                    {
                        "test_name": "test_infra",
                        "error": "timeout",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "Network timeout",
                        },
                    }
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 200
        mock_rp.get_failed_items.return_value = [
            {"id": 10, "name": "test_infra", "status": "FAILED"}
        ]
        # match_failures returns a pair where the FailureAnalysis has test_name
        mock_failure = MagicMock()
        mock_failure.test_name = "test_infra"
        mock_rp.match_failures.return_value = [
            ({"id": 10, "name": "test_infra"}, mock_failure)
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 200,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 200, f"Response: {response.text}"
        # Verify push_classifications was called with INFRASTRUCTURE in history_classifications
        push_call = mock_rp.push_classifications.call_args
        history_arg = (
            push_call[0][2]
            if len(push_call[0]) > 2
            else push_call[1].get("history_classifications", {})
        )
        assert history_arg.get("test_infra") == "INFRASTRUCTURE"

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_no_overlap_returns_error(
        self, mock_rp_class, mock_get_result, mock_get_cls, _rp_enabled_env
    ):
        """When RP items and rootcoz failures have no name overlap, return an error."""
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_alpha",
                        "error": "err",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "d",
                        },
                    }
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 99
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_beta", "status": "FAILED"}
        ]
        mock_rp.match_failures.return_value = []  # no overlap

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 200
        data = response.json()
        assert data["pushed"] == 0
        assert len(data["errors"]) == 1
        assert "No overlap" in data["errors"][0]
        # Test names are in server logs only, not user-facing
        assert "test_beta" not in data["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_verify_ssl_passed_to_rp_client(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """REPORTPORTAL_VERIFY_SSL is forwarded to the ReportPortalClient."""
        with patch.dict(os.environ, {"REPORTPORTAL_VERIFY_SSL": "false"}):
            from rootcoz.config import get_settings

            get_settings.cache_clear()
            mock_get_result.return_value = {
                "result": {
                    "job_name": "test-job",
                    "jenkins_url": "https://jenkins.example.com/job/test/1/",
                    "failures": [
                        {
                            "test_name": "test_a",
                            "error": "err",
                            "analysis": {
                                "classification": "PRODUCT BUG",
                                "details": "d",
                            },
                        }
                    ],
                }
            }
            mock_rp = _install_rp_client_mock(mock_rp_class)
            mock_rp.find_launch.return_value = 1
            mock_rp.get_failed_items.return_value = []
            mock_rp.push_classifications.return_value = {
                "pushed": 0,
                "unmatched": [],
                "errors": [],
                "launch_id": 1,
            }

            from rootcoz.main import app

            client = TestClient(
                app,
                raise_server_exceptions=False,
                headers={"Authorization": "Bearer test-admin-key-16chars"},
            )
            client.post("/results/some-job/push-reportportal")

            # Verify verify_ssl=False was passed
            mock_rp_class.assert_called_once()
            call_kwargs = mock_rp_class.call_args[1]
            assert call_kwargs["verify_ssl"] is False
            get_settings.cache_clear()

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_child_job_push_uses_child_data(
        self, mock_rp_class, mock_get_result, mock_get_cls, _rp_enabled_env
    ):
        """Child job params scope push to child's failures and job name."""
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent-pipeline",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 42,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/42/",
                        "failures": [
                            {
                                "test_name": "test_child_a",
                                "error": "err",
                                "analysis": {
                                    "classification": "PRODUCT BUG",
                                    "details": "child bug",
                                },
                            }
                        ],
                        "failed_children": [],
                    }
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 300
        mock_rp.get_failed_items.return_value = [
            {"id": 5, "name": "test_child_a", "status": "FAILED"}
        ]
        mock_failure = MagicMock()
        mock_failure.test_name = "test_child_a"
        mock_rp.match_failures.return_value = [
            ({"id": 5, "name": "test_child_a"}, mock_failure)
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 300,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 42},
        )
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()
        assert data["pushed"] == 1
        # find_launch should be called with child job name, not parent
        mock_rp.find_launch.assert_called_once_with(
            "child-job", "https://jenkins.example.com/job/child-job/42/"
        )

    @patch("rootcoz.main.get_result")
    def test_child_job_not_found_returns_400(self, mock_get_result, _rp_enabled_env):
        """Returns 400 when the specified child job doesn't exist."""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent-pipeline",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [],
                "child_job_analyses": [],
            },
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "nonexistent", "child_build_number": 99},
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"].lower()

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_child_job_report_url_contains_anchor(
        self, mock_rp_class, mock_get_result, mock_get_cls, _rp_enabled_env
    ):
        """Report URL includes child anchor fragment."""
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent-pipeline",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 10,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/10/",
                        "failures": [
                            {
                                "test_name": "test_x",
                                "error": "err",
                                "analysis": {
                                    "classification": "CODE ISSUE",
                                    "details": "d",
                                },
                            }
                        ],
                        "failed_children": [],
                    }
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 400
        mock_rp.get_failed_items.return_value = [
            {"id": 7, "name": "test_x", "status": "FAILED"}
        ]
        mock_failure = MagicMock()
        mock_failure.test_name = "test_x"
        mock_rp.match_failures.return_value = [
            ({"id": 7, "name": "test_x"}, mock_failure)
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 400,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 10},
        )
        assert response.status_code == 200
        # Verify the report_url passed to push_classifications contains the anchor
        push_call = mock_rp.push_classifications.call_args
        report_url = push_call[0][1]  # second positional arg
        assert "#child-child-job-10" in report_url

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_nested_child_job_push(
        self, mock_rp_class, mock_get_result, mock_get_cls, _rp_enabled_env
    ):
        """Recursively finds nested child job in failed_children."""
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent-pipeline",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-1",
                        "build_number": 10,
                        "jenkins_url": "https://jenkins.example.com/job/child-1/10/",
                        "failures": [],
                        "failed_children": [
                            {
                                "job_name": "nested-child",
                                "build_number": 5,
                                "jenkins_url": "https://jenkins.example.com/job/nested-child/5/",
                                "failures": [
                                    {
                                        "test_name": "test_nested",
                                        "error": "err",
                                        "analysis": {
                                            "classification": "INFRASTRUCTURE",
                                            "details": "d",
                                        },
                                    }
                                ],
                                "failed_children": [],
                            }
                        ],
                    }
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 500
        mock_rp.get_failed_items.return_value = [
            {"id": 9, "name": "test_nested", "status": "FAILED"}
        ]
        mock_failure = MagicMock()
        mock_failure.test_name = "test_nested"
        mock_rp.match_failures.return_value = [
            ({"id": 9, "name": "test_nested"}, mock_failure)
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 500,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "nested-child", "child_build_number": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["pushed"] == 1
        # find_launch called with nested child's job name
        mock_rp.find_launch.assert_called_once_with(
            "nested-child", "https://jenkins.example.com/job/nested-child/5/"
        )


class TestRPPushHTTPErrors:
    """Verify HTTP errors from RP API return proper error responses, not 500."""

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_find_launch_401_returns_200_with_error(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """A 401 from RP find_launch returns a push result with errors, not 500."""
        import requests as _requests

        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = '{"message": "Full authentication is required"}'
        mock_response.json.return_value = {"message": "Full authentication is required"}
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.side_effect = _requests.exceptions.HTTPError(
            response=mock_response
        )

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert len(body["errors"]) == 1
        assert "401" in body["errors"][0]
        assert "Full authentication is required" in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_find_launch_connection_error_returns_200_with_error(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """A ConnectionError from find_launch returns a push result with errors."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.side_effect = ConnectionError("connection refused")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job2/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert len(body["errors"]) == 1
        assert "Error" in body["errors"][0]
        assert "searching RP launches" in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_get_failed_items_error_returns_200_with_error(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """An HTTPError from get_failed_items returns errors, not 500."""
        import requests as _requests

        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = '{"message": "Access denied"}'
        mock_response.json.return_value = {"message": "Access denied"}
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.side_effect = _requests.exceptions.HTTPError(
            response=mock_response
        )

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert body["launch_id"] == 42
        assert len(body["errors"]) == 1
        assert "403" in body["errors"][0]
        assert "Access denied" in body["errors"][0]
        assert "fetching failed items" in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_match_failures_error_returns_200_with_error(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """An exception from match_failures returns errors, not 500."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = [{"id": 1, "name": "test_a"}]
        mock_rp.match_failures.side_effect = TypeError("unexpected None")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job2/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert body["launch_id"] == 42
        assert len(body["errors"]) == 1
        assert "Error" in body["errors"][0]
        assert "matching RP items" in body["errors"][0]

    @patch("rootcoz.exporters.reportportal.logger")
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_get_failed_items_error_log_includes_build_number(
        self, mock_get_result, mock_rp_class, mock_logger, _rp_enabled_env
    ):
        """get_failed_items error log must include build_number for debugging."""
        import requests as _requests

        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "build_number": 77,
                "jenkins_url": "https://jenkins.example.com/job/my-job/77/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = '{"message": "Access denied"}'
        mock_response.json.return_value = {"message": "Access denied"}
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.side_effect = _requests.exceptions.HTTPError(
            response=mock_response
        )

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200

        # Verify error log includes build_number
        error_calls = [
            c for c in mock_logger.error.call_args_list if "RP push failed" in str(c)
        ]
        assert error_calls, "Expected ERROR log for get_failed_items failure"
        log_fmt = error_calls[0][0][0]  # format string
        log_args = error_calls[0][0][1:]  # positional args
        rendered = log_fmt % log_args
        assert "77" in rendered, f"build_number (77) missing from error log: {rendered}"

    @patch("rootcoz.exporters.reportportal.logger")
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_match_failures_error_log_includes_build_number(
        self, mock_get_result, mock_rp_class, mock_logger, _rp_enabled_env
    ):
        """match_failures error log must include build_number for debugging."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "build_number": 88,
                "jenkins_url": "https://jenkins.example.com/job/my-job/88/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = [{"id": 1, "name": "test_a"}]
        mock_rp.match_failures.side_effect = TypeError("unexpected None")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job2/push-reportportal")
        assert response.status_code == 200

        # Verify error log includes build_number
        error_calls = [
            c for c in mock_logger.error.call_args_list if "RP push failed" in str(c)
        ]
        assert error_calls, "Expected ERROR log for match_failures failure"
        log_fmt = error_calls[0][0][0]
        log_args = error_calls[0][0][1:]
        rendered = log_fmt % log_args
        assert "88" in rendered, f"build_number (88) missing from error log: {rendered}"

    @patch("rootcoz.exporters.reportportal.logger")
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_push_classifications_error_log_includes_build_number(
        self, mock_get_result, mock_rp_class, mock_get_cls, mock_logger, _rp_enabled_env
    ):
        """push_classifications error log must include build_number for debugging."""
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "build_number": 99,
                "jenkins_url": "https://jenkins.example.com/job/my-job/99/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_a", "status": "FAILED"}
        ]
        mock_failure = MagicMock()
        mock_failure.test_name = "test_a"
        mock_rp.match_failures.return_value = [
            ({"id": 1, "name": "test_a"}, mock_failure)
        ]
        mock_rp.push_classifications.side_effect = RuntimeError("network timeout")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job3/push-reportportal")
        assert response.status_code == 200

        # Verify error log includes build_number
        error_calls = [
            c for c in mock_logger.error.call_args_list if "RP push failed" in str(c)
        ]
        assert error_calls, "Expected ERROR log for push_classifications failure"
        log_fmt = error_calls[0][0][0]
        log_args = error_calls[0][0][1:]
        rendered = log_fmt % log_args
        assert "99" in rendered, f"build_number (99) missing from error log: {rendered}"

    @patch("rootcoz.exporters.reportportal.logger")
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_ambiguous_launch_returns_200_with_error(
        self, mock_get_result, mock_rp_class, mock_logger, _rp_enabled_env
    ):
        """AmbiguousLaunchError from find_launch returns errors and logs WARNING."""
        from rootcoz.exporters.reportportal import AmbiguousLaunchError

        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.side_effect = AmbiguousLaunchError(
            count=3,
            job_name="my-job",
            jenkins_url="https://jenkins.example.com/job/my-job/1/",
        )

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert len(body["errors"]) == 1
        assert "Ambiguous" in body["errors"][0]
        assert "Remove duplicate" in body["errors"][0]

        # Ambiguous launch is logged at WARNING, not ERROR
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "ambiguous" in str(c).lower()
        ]
        assert warning_calls, "Expected WARNING log for ambiguous launch"
        error_calls = [
            c for c in mock_logger.error.call_args_list if "ambiguous" in str(c).lower()
        ]
        assert not error_calls, "Should NOT log ambiguous launch at ERROR"

    @patch("rootcoz.main.logger")
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_rp_client_constructor_failure_returns_200_with_error(
        self, mock_get_result, mock_rp_class, mock_logger, _rp_enabled_env
    ):
        """RPClient constructor failure returns errors and logs ERROR."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp_class.side_effect = ConnectionError("Name resolution failed")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job3/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert len(body["errors"]) == 1
        assert "Error" in body["errors"][0]
        assert "connecting to Report Portal" in body["errors"][0]

        # Constructor failure is logged at ERROR
        error_calls = [
            c
            for c in mock_logger.error.call_args_list
            if "name resolution" in str(c).lower()
        ]
        assert error_calls, "Expected ERROR log for constructor failure"
        # RP URL must appear in log (not in user-facing error)
        assert "reportportal_host=" in str(error_calls[0]), (
            "Log should include reportportal_host for operator debugging"
        )


class TestRPPushEarlyGuard:
    """Verify early exit when there are no failures to push."""

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_empty_failures_skips_rp_calls(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """When failures list is empty, push() returns early without RP API calls."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert len(body["errors"]) == 1
        assert "No failures to push" in body["errors"][0]
        mock_rp.find_launch.assert_not_called()

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_missing_failures_key_skips_rp_calls(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """When failures key is absent, push() returns early without RP API calls."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert len(body["errors"]) == 1
        assert "No failures to push" in body["errors"][0]
        mock_rp.find_launch.assert_not_called()

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_child_job_empty_failures_skips_rp_calls(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """When scoped child job has empty failures, push() returns early."""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent-pipeline",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [
                    {
                        "test_name": "test_parent",
                        "error": "err",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "d",
                        },
                    }
                ],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 42,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/42/",
                        "failures": [],
                        "failed_children": [],
                    }
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 42},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert len(body["errors"]) == 1
        assert "No failures to push" in body["errors"][0]
        mock_rp.find_launch.assert_not_called()


class TestRPPushDebugLogging:
    """Verify normal-state RP paths log at DEBUG, not ERROR."""

    @patch("rootcoz.exporters.reportportal.logger")
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_no_failed_items_logs_debug(
        self, mock_get_result, mock_rp_class, mock_logger, _rp_enabled_env
    ):
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = []

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200
        assert response.json()["pushed"] == 0

        # Normal state: logged at DEBUG, not ERROR
        debug_calls = [
            c
            for c in mock_logger.debug.call_args_list
            if "no failed items" in str(c).lower()
        ]
        assert debug_calls, "Expected DEBUG log for 'no failed items'"
        error_calls = [
            c
            for c in mock_logger.error.call_args_list
            if "no failed items" in str(c).lower()
        ]
        assert not error_calls, "Should NOT log 'no failed items' at ERROR"

    @patch("rootcoz.main.logger")
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_empty_failures_early_guard_does_not_log_error(
        self, mock_get_result, mock_rp_class, mock_logger, _rp_enabled_env
    ):
        """Empty failures guard in push() does not produce ERROR logs."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [],
            }
        }
        _install_rp_client_mock(mock_rp_class)

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job2/push-reportportal")
        assert response.status_code == 200
        assert response.json()["pushed"] == 0

        error_calls = mock_logger.error.call_args_list
        assert not error_calls, "Empty-failures guard should not produce ERROR logs"


class TestRPPushContentToggles:
    """Verify push content toggles are validated and passed through."""

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_all_toggles_disabled_returns_error(
        self, mock_get_result, mock_rp_class, _rp_enabled_all_push_disabled_env
    ):
        """When all 3 push toggles are disabled, push() returns error without RP API calls."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "failures": [
                    {
                        "test_name": "test_example",
                        "error_message": "AssertionError",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "text",
                        },
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)

        from rootcoz.main import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/results/test-job-id/push-reportportal",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["pushed"] == 0
        assert any(
            "All Report Portal push content toggles are disabled" in e
            for e in data["errors"]
        )
        mock_rp.find_launch.assert_not_called()

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_toggles_passed_to_push_classifications(
        self, mock_get_result, mock_rp_class, mock_get_cls, _rp_enabled_partial_push_env
    ):
        """Verify toggle values are forwarded to push_classifications()."""
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "build_number": 1,
                "failures": [
                    {
                        "test_name": "test_example",
                        "error": "AssertionError",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "text",
                        },
                    }
                ],
            }
        }

        # Setup RP client mock
        mock_rp_instance = _install_rp_client_mock(mock_rp_class)
        mock_rp_instance.find_launch.return_value = 42
        mock_rp_instance.get_failed_items.return_value = [
            {"id": 100, "name": "test_example", "launchId": 42}
        ]
        mock_rp_instance.match_failures.return_value = [
            (
                {"id": 100, "name": "test_example", "launchId": 42},
                MagicMock(test_name="test_example"),
            )
        ]
        mock_rp_instance.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }

        from rootcoz.main import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/results/test-job-id/push-reportportal",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )

        assert response.status_code == 200
        # Verify push_classifications was called with the correct toggle values
        mock_rp_instance.push_classifications.assert_called_once()
        call_kwargs = mock_rp_instance.push_classifications.call_args
        assert call_kwargs.kwargs["push_classifications"] is True
        assert call_kwargs.kwargs["push_rootcoz_url"] is False
        assert call_kwargs.kwargs["push_tracker_links"] is True

    @patch("rootcoz.main.storage.get_tracked_in_for_scope", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_tracked_in_links_passed_to_push_classifications(
        self,
        mock_get_result,
        mock_rp_class,
        mock_get_cls,
        mock_get_scope,
        _rp_enabled_partial_push_env,
    ):
        """Tracked-in links fetched from DB are forwarded to push_classifications."""
        mock_get_cls.return_value = ""
        mock_get_scope.return_value = {
            "test_example": [
                {
                    "tracked_in_url": "https://github.com/org/repo/pull/42",
                    "tracked_in_type": "github",
                    "tracked_in_by": "user1",
                }
            ]
        }
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "build_number": 1,
                "failures": [
                    {
                        "test_name": "test_example",
                        "error": "AssertionError",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "text",
                        },
                    }
                ],
            }
        }

        mock_rp_instance = _install_rp_client_mock(mock_rp_class)
        mock_rp_instance.find_launch.return_value = 42
        mock_rp_instance.get_failed_items.return_value = [
            {"id": 100, "name": "test_example", "launchId": 42}
        ]
        mock_rp_instance.match_failures.return_value = [
            (
                {"id": 100, "name": "test_example", "launchId": 42},
                MagicMock(test_name="test_example"),
            )
        ]
        mock_rp_instance.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }

        from rootcoz.main import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/results/test-job-id/push-reportportal",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )

        assert response.status_code == 200
        # Verify scope query used parent scope (no child)
        mock_get_scope.assert_called_once_with(
            "test-job-id", child_job_name="", child_build_number=0
        )
        # Verify tracked_in_links forwarded to push_classifications
        call_kwargs = mock_rp_instance.push_classifications.call_args
        assert call_kwargs.kwargs["tracked_in_links"] == mock_get_scope.return_value

    @patch("rootcoz.main.storage.get_tracked_in_for_scope", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_tracked_in_scope_uses_child_params(
        self,
        mock_get_result,
        mock_rp_class,
        mock_get_cls,
        mock_get_scope,
        _rp_enabled_partial_push_env,
    ):
        """Child push passes child_job_name and child_build_number to scope query."""
        mock_get_cls.return_value = ""
        mock_get_scope.return_value = {}
        mock_get_result.return_value = {
            "result": {
                "job_name": "parent-pipeline",
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "build_number": 1,
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 42,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/42/",
                        "failures": [
                            {
                                "test_name": "test_child",
                                "error": "err",
                                "analysis": {
                                    "classification": "PRODUCT BUG",
                                    "details": "bug",
                                },
                            }
                        ],
                    }
                ],
            }
        }

        mock_rp_instance = _install_rp_client_mock(mock_rp_class)
        mock_rp_instance.find_launch.return_value = 42
        mock_rp_instance.get_failed_items.return_value = [
            {"id": 200, "name": "test_child", "launchId": 42}
        ]
        mock_rp_instance.match_failures.return_value = [
            (
                {"id": 200, "name": "test_child", "launchId": 42},
                MagicMock(test_name="test_child"),
            )
        ]
        mock_rp_instance.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }

        from rootcoz.main import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/results/test-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 42},
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )

        assert response.status_code == 200
        # Verify scope query used child scope
        mock_get_scope.assert_called_once_with(
            "test-job-id", child_job_name="child-job", child_build_number=42
        )

    @patch("rootcoz.main.storage.get_tracked_in_for_scope", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_tracked_in_fetch_failure_is_graceful(
        self,
        mock_get_result,
        mock_rp_class,
        mock_get_cls,
        mock_get_scope,
        _rp_enabled_partial_push_env,
    ):
        """Storage failure when fetching tracked links doesn't crash the push."""
        mock_get_cls.return_value = ""
        mock_get_scope.side_effect = Exception("DB error")
        mock_get_result.return_value = {
            "result": {
                "job_name": "my-job",
                "jenkins_url": "https://jenkins.example.com/job/my-job/1/",
                "build_number": 1,
                "failures": [
                    {
                        "test_name": "test_example",
                        "error": "AssertionError",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "text",
                        },
                    }
                ],
            }
        }

        mock_rp_instance = _install_rp_client_mock(mock_rp_class)
        mock_rp_instance.find_launch.return_value = 42
        mock_rp_instance.get_failed_items.return_value = [
            {"id": 100, "name": "test_example", "launchId": 42}
        ]
        mock_rp_instance.match_failures.return_value = [
            (
                {"id": 100, "name": "test_example", "launchId": 42},
                MagicMock(test_name="test_example"),
            )
        ]
        mock_rp_instance.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 42,
        }

        from rootcoz.main import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/results/test-job-id/push-reportportal",
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )

        assert response.status_code == 200
        assert response.json()["pushed"] == 1
        # push_classifications still called with empty tracked data
        call_kwargs = mock_rp_instance.push_classifications.call_args
        assert call_kwargs.kwargs["tracked_in_links"] == {}


class TestCapabilitiesEndpoint:
    """Test that capabilities includes reportportal."""

    def test_capabilities_includes_rp_disabled(self, _rp_disabled_env):
        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.get("/api/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert "reportportal" in data
        assert data["reportportal"] is False
        assert data["reportportal_project"] == ""

    def test_capabilities_includes_rp_enabled(self, _rp_enabled_env):
        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.get("/api/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert "reportportal" in data
        assert data["reportportal"] is True
        assert data["reportportal_project"] == "my-project"


class TestRPErrorMessage:
    """Unit tests for _rp_error_message helper."""

    def _make_exc_with_response(self, *, json_return, text="fallback text"):
        """Build an exception whose .response mimics an httpx/requests Response."""
        resp = MagicMock()
        resp.status_code = 500
        resp.text = text
        resp.json.return_value = json_return
        exc = Exception("boom")
        exc.response = resp
        return exc

    def test_dict_body_extracts_message_field(self, _rp_enabled_env):
        """When RP returns a JSON dict with 'message', extract it for user."""
        from rootcoz.main import _rp_error_message

        exc = self._make_exc_with_response(json_return={"message": "Token expired"})
        user_msg, _log_msg = _rp_error_message(exc, "finding launch")
        assert "Token expired" in user_msg
        assert "500" in user_msg
        assert "finding launch" in user_msg

    def test_dict_body_without_message_shows_status_only(self, _rp_enabled_env):
        """When RP returns a JSON dict without 'message', user sees status only."""
        from rootcoz.main import _rp_error_message

        exc = self._make_exc_with_response(
            json_return={"error": "something else"},
            text="raw response text",
        )
        user_msg, log_msg = _rp_error_message(exc, "finding launch")
        # User sees status + operation, no raw text
        assert "500" in user_msg
        assert "finding launch" in user_msg
        # Raw text only in log
        assert "raw response text" in log_msg

    def test_list_body_shows_status_only(self, _rp_enabled_env):
        """When RP returns a JSON array, user sees status only (no crash)."""
        from rootcoz.main import _rp_error_message

        exc = self._make_exc_with_response(
            json_return=["error1", "error2"],
            text="the raw text",
        )
        user_msg, log_msg = _rp_error_message(exc, "finding launch")
        assert "500" in user_msg
        assert "finding launch" in user_msg
        # Raw text only in log
        assert "the raw text" in log_msg

    def test_string_body_shows_status_only(self, _rp_enabled_env):
        """When RP returns a plain JSON string, user sees status only."""
        from rootcoz.main import _rp_error_message

        exc = self._make_exc_with_response(
            json_return="just a string",
            text="the raw text",
        )
        user_msg, log_msg = _rp_error_message(exc, "finding launch")
        assert "500" in user_msg
        # Raw text only in log
        assert "the raw text" in log_msg

    def test_json_parse_failure_shows_status_only(self, _rp_enabled_env):
        """When resp.json() raises, user sees status only, log has raw text."""
        from rootcoz.main import _rp_error_message

        resp = MagicMock()
        resp.status_code = 502
        resp.text = "Bad Gateway"
        resp.json.side_effect = ValueError("No JSON")
        exc = Exception("boom")
        exc.response = resp
        user_msg, log_msg = _rp_error_message(exc, "finding launch")
        assert "502" in user_msg
        assert "finding launch" in user_msg
        # Full text only in log
        assert "Bad Gateway" in log_msg

    def test_no_response_shows_operation_only(self, _rp_enabled_env):
        """When exc has no .response, user sees operation only; log has detail."""
        from rootcoz.main import _rp_error_message

        exc = ConnectionError("connection refused")
        user_msg, log_msg = _rp_error_message(exc, "connecting")
        # User sees short message
        assert user_msg == "Error connecting"
        # Log has full detail
        assert "ConnectionError" in log_msg
        assert "connection refused" in log_msg

    def test_rp_message_shown_to_user_but_raw_body_only_in_log(self, _rp_enabled_env):
        """RP JSON message goes to user; full response body only in log."""
        from rootcoz.main import _rp_error_message

        exc = self._make_exc_with_response(
            json_return={"message": "Access denied"},
            text='{"message": "Access denied", "debug": "lots of internal detail"}',
        )
        user_msg, log_msg = _rp_error_message(exc, "pushing classifications")
        assert "Access denied" in user_msg
        assert "internal detail" not in user_msg
        assert "internal detail" in log_msg


class TestRpPushErrorResult:
    """Unit tests for _rp_push_error_result."""

    def test_message_preserved_as_is(self, _rp_enabled_env):
        """Error message is returned verbatim — no context suffix."""
        from rootcoz.main import _rp_push_error_result

        result = _rp_push_error_result("Some error")
        assert result["errors"] == ["Some error"]

    def test_launch_id_preserved(self, _rp_enabled_env):
        """launch_id is set correctly."""
        from rootcoz.main import _rp_push_error_result

        result = _rp_push_error_result("Some error", launch_id=99)
        assert result["launch_id"] == 99
        assert result["errors"] == ["Some error"]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_early_guard_error_is_clean(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """Empty-failures guard returns a short error without context suffix."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "context-job",
                "build_number": 77,
                "jenkins_url": "https://jenkins.example.com/job/context-job/77/",
                "failures": [],
            }
        }
        _install_rp_client_mock(mock_rp_class)

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        assert response.status_code == 200
        body = response.json()
        assert body["pushed"] == 0
        assert "No failures to push" in body["errors"][0]
        assert "(job=" not in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_find_launch_error_is_clean(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """find_launch exception error has no context suffix."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "ctx-job",
                "build_number": 55,
                "jenkins_url": "https://jenkins.example.com/job/ctx-job/55/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.side_effect = ConnectionError("refused")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        body = response.json()
        assert "searching RP launches" in body["errors"][0]
        assert "(job=" not in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_no_launch_found_is_clean(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """No launch found error is short and has no context suffix."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "ctx-job",
                "build_number": 33,
                "jenkins_url": "https://jenkins.example.com/job/ctx-job/33/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = None

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        body = response.json()
        assert "No Report Portal launch found." in body["errors"][0]
        assert (
            "Ensure the Jenkins build URL is in the RP launch description"
            in body["errors"][0]
        )
        assert "(job=" not in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_get_failed_items_error_is_clean(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """get_failed_items error has no context suffix."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "ctx-job",
                "build_number": 44,
                "jenkins_url": "https://jenkins.example.com/job/ctx-job/44/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.side_effect = RuntimeError("network err")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        body = response.json()
        assert "fetching failed items" in body["errors"][0]
        assert "(job=" not in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_match_failures_error_is_clean(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """match_failures error has no context suffix."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "ctx-job",
                "build_number": 66,
                "jenkins_url": "https://jenkins.example.com/job/ctx-job/66/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = [{"id": 1, "name": "test_a"}]
        mock_rp.match_failures.side_effect = TypeError("boom")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        body = response.json()
        assert "matching RP items" in body["errors"][0]
        assert "(job=" not in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_no_overlap_error_is_clean(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """No overlap error has no context suffix."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "ctx-job",
                "build_number": 22,
                "jenkins_url": "https://jenkins.example.com/job/ctx-job/22/",
                "failures": [
                    {
                        "test_name": "test_alpha",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_beta", "status": "FAILED"}
        ]
        mock_rp.match_failures.return_value = []

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        body = response.json()
        assert "No overlap" in body["errors"][0]
        assert "(job=" not in body["errors"][0]

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_push_classifications_error_is_clean(
        self, mock_get_result, mock_rp_class, mock_get_cls, _rp_enabled_env
    ):
        """push_classifications error has no context suffix."""
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "result": {
                "job_name": "ctx-job",
                "build_number": 11,
                "jenkins_url": "https://jenkins.example.com/job/ctx-job/11/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 42
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_a", "status": "FAILED"}
        ]
        mock_failure = MagicMock()
        mock_failure.test_name = "test_a"
        mock_rp.match_failures.return_value = [
            ({"id": 1, "name": "test_a"}, mock_failure)
        ]
        mock_rp.push_classifications.side_effect = RuntimeError("timeout")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        body = response.json()
        assert "pushing classifications" in body["errors"][0]
        assert "(job=" not in body["errors"][0]

    @patch("rootcoz.main.ReportPortalClient")
    @patch("rootcoz.main.get_result")
    def test_constructor_failure_is_clean(
        self, mock_get_result, mock_rp_class, _rp_enabled_env
    ):
        """Constructor failure has no context suffix."""
        mock_get_result.return_value = {
            "result": {
                "job_name": "ctx-job",
                "build_number": 88,
                "jenkins_url": "https://jenkins.example.com/job/ctx-job/88/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {"classification": "PRODUCT BUG", "details": "d"},
                    }
                ],
            }
        }
        mock_rp_class.side_effect = ConnectionError("DNS failed")

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post("/results/job1/push-reportportal")
        body = response.json()
        assert "connecting to Report Portal" in body["errors"][0]
        assert "(job=" not in body["errors"][0]


class TestRPPushChildValidation:
    """Validate child_job_name / child_build_number parameter combinations."""

    @staticmethod
    def _make_parent_result() -> dict:
        """Return a fresh parent result dict for each test."""
        return {
            "status": "completed",
            "result": {
                "job_name": "parent-pipeline",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [
                    {
                        "test_name": "test_parent",
                        "error": "err",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "d",
                        },
                    }
                ],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 42,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/42/",
                        "failures": [
                            {
                                "test_name": "test_child",
                                "error": "err",
                                "analysis": {
                                    "classification": "CODE ISSUE",
                                    "details": "d",
                                },
                            }
                        ],
                        "failed_children": [],
                    }
                ],
            },
        }

    @patch("rootcoz.main.get_result")
    def test_child_job_name_without_build_number_returns_400(
        self, mock_get_result, _rp_enabled_env
    ):
        """child_job_name without child_build_number should fail."""
        mock_get_result.return_value = self._make_parent_result()

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job"},
        )
        assert response.status_code == 400
        assert "child_build_number" in response.json()["detail"].lower()

    @patch("rootcoz.main.get_result")
    def test_child_build_number_zero_returns_400(
        self, mock_get_result, _rp_enabled_env
    ):
        """child_build_number=0 with child_job_name should fail."""
        mock_get_result.return_value = self._make_parent_result()

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 0},
        )
        assert response.status_code == 400
        assert "child_build_number" in response.json()["detail"].lower()

    @patch("rootcoz.main.get_result")
    def test_child_build_number_without_job_name_returns_400(
        self, mock_get_result, _rp_enabled_env
    ):
        """child_build_number without child_job_name should fail."""
        mock_get_result.return_value = self._make_parent_result()

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_build_number": 42},
        )
        assert response.status_code == 400
        assert "child_job_name" in response.json()["detail"].lower()


class TestPushedByForwarding:
    """Verify pushed_by is threaded from endpoint to push_classifications."""

    @staticmethod
    def _setup_mocks(mock_rp_class, mock_get_result, mock_get_cls):
        """Configure shared mocks for RP push tests. Returns (mock_rp, client)."""
        from rootcoz.main import app

        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "my-job",
                "build_number": 42,
                "jenkins_url": "https://jenkins.example.com/job/my-job/42/",
                "failures": [
                    {
                        "test_name": "test_a",
                        "error": "err",
                        "analysis": {
                            "classification": "PRODUCT BUG",
                            "details": "Bug found",
                        },
                    }
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 100
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_a", "status": "FAILED"}
        ]
        mock_rp.match_failures.return_value = [
            ({"id": 1, "name": "test_a"}, MagicMock(test_name="test_a"))
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 100,
        }

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        return mock_rp, client

    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_pushed_by_forwarded_from_endpoint(
        self, mock_rp_class, mock_get_result, mock_get_cls, _rp_enabled_env
    ):
        """Username from request is forwarded as pushed_by to push_classifications."""
        mock_rp, client = self._setup_mocks(
            mock_rp_class, mock_get_result, mock_get_cls
        )
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 200
        # Verify push_classifications received the pushed_by parameter
        push_call = mock_rp.push_classifications.call_args
        assert push_call.kwargs["pushed_by"] == "admin"

    @patch("rootcoz.main.logger")
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_info_log_includes_username(
        self, mock_rp_class, mock_get_result, mock_get_cls, mock_logger, _rp_enabled_env
    ):
        """INFO log for manual RP push includes the authenticated username."""
        _, client = self._setup_mocks(mock_rp_class, mock_get_result, mock_get_cls)
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 200, f"Response: {response.text}"
        # Verify INFO log was called with the expected format string and args
        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and "RP push requested" in c.args[0]
        ]
        assert info_calls, "Expected INFO log for RP push request"
        _, logged_username, logged_job_id = info_calls[0].args
        assert logged_username == "admin", f"Expected 'admin', got '{logged_username}'"
        assert logged_job_id == "some-job-id", (
            f"Expected 'some-job-id', got '{logged_job_id}'"
        )

    @patch("rootcoz.main.storage.get_reviews_for_job", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_reviewed_by_forwarded_from_endpoint(
        self,
        mock_rp_class,
        mock_get_result,
        mock_get_cls,
        mock_get_reviews,
        _rp_enabled_env,
    ):
        """Reviewer username from storage is forwarded as reviewed_by to push_classifications."""
        mock_get_reviews.return_value = {
            "test_a": {
                "reviewed": True,
                "username": "bob",
                "updated_at": "2024-01-01T00:00:00Z",
            },
        }
        mock_rp, client = self._setup_mocks(
            mock_rp_class,
            mock_get_result,
            mock_get_cls,
        )
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 200
        push_call = mock_rp.push_classifications.call_args
        assert push_call.kwargs["reviewed_by"] == {"test_a": "bob"}

    @patch("rootcoz.main.storage.get_reviews_for_job", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_reviewed_by_preserves_test_names_with_colons(
        self,
        mock_rp_class,
        mock_get_result,
        mock_get_cls,
        mock_get_reviews,
        _rp_enabled_env,
    ):
        """Test names containing '::' are not mistaken for child-scoped keys."""
        mock_get_reviews.return_value = {
            "TestClass::test_method": {
                "reviewed": True,
                "username": "eve",
                "updated_at": "2024-01-01T00:00:00Z",
            },
        }
        mock_rp, client = self._setup_mocks(
            mock_rp_class,
            mock_get_result,
            mock_get_cls,
        )
        # Override test_name in the result to match
        mock_get_result.return_value["result"]["failures"][0]["test_name"] = (
            "TestClass::test_method"
        )
        response = client.post("/results/some-job-id/push-reportportal")
        assert response.status_code == 200
        push_call = mock_rp.push_classifications.call_args
        assert push_call.kwargs["reviewed_by"] == {"TestClass::test_method": "eve"}

    @patch("rootcoz.main.storage.get_reviews_for_job", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_reviewed_by_scopes_to_child_job(
        self,
        mock_rp_class,
        mock_get_result,
        mock_get_cls,
        mock_get_reviews,
        _rp_enabled_env,
    ):
        """Child-scoped push picks only reviews from that child, not others."""
        mock_get_reviews.return_value = {
            "child-job#42::test_a": {
                "reviewed": True,
                "username": "carol",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            "other-child#99::test_a": {
                "reviewed": True,
                "username": "dave",
                "updated_at": "2024-01-01T00:00:00Z",
            },
        }
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 42,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/42/",
                        "failures": [
                            {
                                "test_name": "test_a",
                                "error": "err",
                                "analysis": {
                                    "classification": "PRODUCT BUG",
                                    "details": "Bug found",
                                },
                            }
                        ],
                        "failed_children": [],
                    },
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 100
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_a", "status": "FAILED"}
        ]
        mock_rp.match_failures.return_value = [
            ({"id": 1, "name": "test_a"}, MagicMock(test_name="test_a"))
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 100,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 42},
        )
        assert response.status_code == 200
        push_call = mock_rp.push_classifications.call_args
        # Only carol (child-job#42) should match, not dave (other-child#99)
        assert push_call.kwargs["reviewed_by"] == {"test_a": "carol"}

    @patch("rootcoz.main.storage.get_reviews_for_job", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_reviewed_by_accepts_wildcard_child_review(
        self,
        mock_rp_class,
        mock_get_result,
        mock_get_cls,
        mock_get_reviews,
        _rp_enabled_env,
    ):
        """Wildcard child reviews (build_number=0) are accepted for child pushes."""
        mock_get_reviews.return_value = {
            # Wildcard review (build_number=0 means name-only scope)
            "child-job#0::test_a": {
                "reviewed": True,
                "username": "wildcard_reviewer",
                "updated_at": "2024-01-01T00:00:00Z",
            },
        }
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 42,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/42/",
                        "failures": [
                            {
                                "test_name": "test_a",
                                "error": "err",
                                "analysis": {
                                    "classification": "PRODUCT BUG",
                                    "details": "Bug found",
                                },
                            }
                        ],
                        "failed_children": [],
                    },
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 100
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_a", "status": "FAILED"}
        ]
        mock_rp.match_failures.return_value = [
            ({"id": 1, "name": "test_a"}, MagicMock(test_name="test_a"))
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 100,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 42},
        )
        assert response.status_code == 200
        push_call = mock_rp.push_classifications.call_args
        assert push_call.kwargs["reviewed_by"] == {"test_a": "wildcard_reviewer"}

    @patch("rootcoz.main.storage.get_reviews_for_job", new_callable=AsyncMock)
    @patch("rootcoz.main.get_history_classification", new_callable=AsyncMock)
    @patch("rootcoz.main.get_result")
    @patch("rootcoz.main.ReportPortalClient")
    def test_reviewed_by_exact_overrides_wildcard(
        self,
        mock_rp_class,
        mock_get_result,
        mock_get_cls,
        mock_get_reviews,
        _rp_enabled_env,
    ):
        """Exact build review takes precedence over wildcard regardless of dict order."""
        # Wildcard listed FIRST to verify order-independence
        mock_get_reviews.return_value = {
            "child-job#0::test_a": {
                "reviewed": True,
                "username": "wildcard_reviewer",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            "child-job#42::test_a": {
                "reviewed": True,
                "username": "exact_reviewer",
                "updated_at": "2024-01-01T00:00:00Z",
            },
        }
        mock_get_cls.return_value = ""
        mock_get_result.return_value = {
            "status": "completed",
            "result": {
                "job_name": "parent",
                "build_number": 1,
                "jenkins_url": "https://jenkins.example.com/job/parent/1/",
                "failures": [],
                "child_job_analyses": [
                    {
                        "job_name": "child-job",
                        "build_number": 42,
                        "jenkins_url": "https://jenkins.example.com/job/child-job/42/",
                        "failures": [
                            {
                                "test_name": "test_a",
                                "error": "err",
                                "analysis": {
                                    "classification": "PRODUCT BUG",
                                    "details": "Bug found",
                                },
                            }
                        ],
                        "failed_children": [],
                    },
                ],
            },
        }
        mock_rp = _install_rp_client_mock(mock_rp_class)
        mock_rp.find_launch.return_value = 100
        mock_rp.get_failed_items.return_value = [
            {"id": 1, "name": "test_a", "status": "FAILED"}
        ]
        mock_rp.match_failures.return_value = [
            ({"id": 1, "name": "test_a"}, MagicMock(test_name="test_a"))
        ]
        mock_rp.push_classifications.return_value = {
            "pushed": 1,
            "unmatched": [],
            "errors": [],
            "launch_id": 100,
        }

        from rootcoz.main import app

        client = TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer test-admin-key-16chars"},
        )
        response = client.post(
            "/results/some-job-id/push-reportportal",
            params={"child_job_name": "child-job", "child_build_number": 42},
        )
        assert response.status_code == 200
        push_call = mock_rp.push_classifications.call_args
        # Exact (build 42) wins over wildcard (build 0)
        assert push_call.kwargs["reviewed_by"] == {"test_a": "exact_reviewer"}
