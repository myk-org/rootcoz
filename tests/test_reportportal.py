"""Tests for Report Portal integration module."""

from unittest.mock import MagicMock, patch

import pytest
import requests as _requests

import jenkins_job_insight.reportportal as rp_module
from jenkins_job_insight.reportportal import ReportPortalClient


# -- Classification mapping tests -------------------------------------------


class TestClassificationMapping:
    """Test JJI-to-RP classification mapping."""

    def test_product_bug_maps_to_product_bug(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        )
        locator = client._map_classification("PRODUCT BUG")
        assert locator == "pb001"

    def test_code_issue_maps_to_automation_bug(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        )
        locator = client._map_classification("CODE ISSUE")
        assert locator == "ab001"

    def test_infrastructure_maps_to_system_issue(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        )
        locator = client._map_classification("INFRASTRUCTURE")
        assert locator == "si001"

    def test_unknown_classification_returns_none(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        )
        locator = client._map_classification("SOMETHING ELSE")
        assert locator is None

    def test_empty_classification_returns_none(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        )
        locator = client._map_classification("")
        assert locator is None

    def test_custom_locators_override_defaults(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        )
        custom = {"PRODUCT_BUG": "pb_custom_123"}
        locator = client._map_classification("PRODUCT BUG", locators=custom)
        assert locator == "pb_custom_123"

    def test_custom_locators_falls_back_to_defaults_for_missing_key(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        )
        # Custom locators without PRODUCT_BUG — falls back to default
        custom = {"SYSTEM_ISSUE": "si_custom"}
        locator = client._map_classification("PRODUCT BUG", locators=custom)
        assert locator == "pb001"  # default locator


# -- Constructor tests -------------------------------------------------------


class TestReportPortalClientInit:
    """Test client construction."""

    def test_constructor_creates_rp_client(self):
        client = ReportPortalClient(
            url="http://rp.example.com",
            token="my-token",
            project="my-project",
        )
        assert client._rp_client is not None

    def test_constructor_strips_trailing_slash(self):
        client = ReportPortalClient(
            url="http://rp.example.com/",
            token="tok",
            project="proj",
        )
        assert client._rp_client.endpoint == "http://rp.example.com"

    def test_context_manager_calls_close(self):
        with ReportPortalClient(
            url="http://rp.example.com",
            token="tok",
            project="proj",
        ) as client:
            assert client._rp_client is not None
        # After exiting, close was called (no error raised)


# -- get_defect_type_locators tests ------------------------------------------


class TestGetDefectTypeLocators:
    """Test fetching defect type locators from RP project settings."""

    def test_parses_project_settings(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "subTypes": {
                "PRODUCT_BUG": [{"locator": "pb_abc", "shortName": "PB"}],
                "AUTOMATION_BUG": [{"locator": "ab_def", "shortName": "AB"}],
                "SYSTEM_ISSUE": [{"locator": "si_ghi", "shortName": "SI"}],
                "TO_INVESTIGATE": [{"locator": "ti_jkl", "shortName": "TI"}],
            }
        }
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session
        result = client.get_defect_type_locators()
        assert result == {
            "PRODUCT_BUG": "pb_abc",
            "AUTOMATION_BUG": "ab_def",
            "SYSTEM_ISSUE": "si_ghi",
            "TO_INVESTIGATE": "ti_jkl",
        }

    def test_handles_empty_subtypes(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        mock_response = MagicMock()
        mock_response.json.return_value = {"subTypes": {}}
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session
        result = client.get_defect_type_locators()
        assert result == {}


# -- find_launch tests -------------------------------------------------------


class TestFindLaunch:
    """Test finding a launch by job name."""

    def test_finds_single_launch(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [
                {
                    "id": 42,
                    "name": "my-job",
                    "description": "http://jenkins.example.com/job/my-job/1/",
                }
            ],
            "page": {"totalPages": 1},
        }
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        result = client.find_launch(
            "my-job", "http://jenkins.example.com/job/my-job/1/"
        )
        assert result == 42

    def test_returns_none_when_no_launches(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [],
            "page": {"totalPages": 1},
        }
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        result = client.find_launch(
            "my-job", "http://jenkins.example.com/job/my-job/1/"
        )
        assert result is None

    def test_disambiguates_by_jenkins_url(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        mock_session = MagicMock()
        # URL filter returns exactly the one launch whose description contains the URL
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [
                {
                    "id": 20,
                    "name": "my-job",
                    "description": "http://jenkins.example.com/job/my-job/5/",
                },
            ],
            "page": {"totalPages": 1},
        }
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        result = client.find_launch(
            "my-job", "http://jenkins.example.com/job/my-job/5/"
        )
        assert result == 20

    def test_raises_ambiguous_launch_error(self):
        from jenkins_job_insight.reportportal import AmbiguousLaunchError

        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        jenkins_url = "http://jenkins/job/my-job/99/"
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [
                {"id": 10, "name": "my-job", "description": jenkins_url},
                {"id": 20, "name": "my-job", "description": jenkins_url},
            ],
            "page": {"totalPages": 1},
        }
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        with pytest.raises(AmbiguousLaunchError) as exc_info:
            client.find_launch("my-job", jenkins_url)
        assert exc_info.value.count == 2
        assert exc_info.value.job_name == "my-job"
        assert exc_info.value.jenkins_url == jenkins_url

    def test_paginates_across_multiple_pages(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        # URL filter query paginates across 2 pages; match is on page 2
        page1_response = MagicMock()
        page1_response.json.return_value = {
            "content": [],
            "page": {"totalPages": 2},
        }
        page1_response.raise_for_status = MagicMock()

        page2_response = MagicMock()
        page2_response.json.return_value = {
            "content": [
                {
                    "id": 20,
                    "name": "my-job",
                    "description": "http://jenkins.example.com/job/my-job/5/",
                }
            ],
            "page": {"totalPages": 2},
        }
        page2_response.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.side_effect = [page1_response, page2_response]
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        result = client.find_launch(
            "my-job", "http://jenkins.example.com/job/my-job/5/"
        )
        assert result == 20
        assert mock_session.get.call_count == 2


# -- get_failed_items tests --------------------------------------------------


class TestGetFailedItems:
    """Test fetching failed items from a launch."""

    def test_returns_failed_items(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        items = [
            {"id": 1, "name": "test_a", "status": "FAILED", "type": "STEP"},
            {"id": 2, "name": "test_b", "status": "FAILED", "type": "STEP"},
        ]
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": items,
            "page": {"totalPages": 1, "number": 1},
        }
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        result = client.get_failed_items(42)
        assert len(result) == 2
        assert result[0]["name"] == "test_a"

    def test_handles_pagination(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        page1_response = MagicMock()
        page1_response.json.return_value = {
            "content": [{"id": 1, "name": "test_a"}],
            "page": {"totalPages": 2, "number": 1},
        }
        page1_response.raise_for_status = MagicMock()

        page2_response = MagicMock()
        page2_response.json.return_value = {
            "content": [{"id": 2, "name": "test_b"}],
            "page": {"totalPages": 2, "number": 2},
        }
        page2_response.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.side_effect = [page1_response, page2_response]
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        result = client.get_failed_items(42)
        assert len(result) == 2


# -- match_failures tests ----------------------------------------------------


class TestMatchFailures:
    """Test matching RP items to JJI failures."""

    def test_exact_match_by_name(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        rp_items = [{"id": 1, "name": "test_login_success"}]
        jji_failures = [
            MagicMock(test_name="test_login_success"),
        ]
        result = client.match_failures(rp_items, jji_failures)
        assert len(result) == 1
        assert result[0][0]["id"] == 1
        assert result[0][1].test_name == "test_login_success"

    def test_suffix_match(self):
        """RP items often have short names; JJI has FQN."""
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        rp_items = [{"id": 1, "name": "test_login_success"}]
        jji_failures = [
            MagicMock(test_name="tests.auth.TestAuth.test_login_success"),
        ]
        result = client.match_failures(rp_items, jji_failures)
        assert len(result) == 1

    def test_match_by_code_ref(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        rp_items = [
            {
                "id": 1,
                "name": "Some Test",
                "codeRef": "tests.auth.TestAuth.test_login_success",
            }
        ]
        jji_failures = [
            MagicMock(test_name="tests.auth.TestAuth.test_login_success"),
        ]
        result = client.match_failures(rp_items, jji_failures)
        assert len(result) == 1

    def test_no_match(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        rp_items = [{"id": 1, "name": "test_something_else"}]
        jji_failures = [
            MagicMock(test_name="test_login_success"),
        ]
        result = client.match_failures(rp_items, jji_failures)
        assert len(result) == 0


# -- push_classifications tests -----------------------------------------------


def _extract_put_json(mock_session):
    """Extract the first issue entry from the bulk PUT payload.

    The RP bulk update endpoint uses ``{"issues": [{"testItemId": ..., "issue": {...}}]}``.
    Returns the outer wrapper so callers can access ``result["issue"]``.
    """
    put_call = mock_session.put.call_args
    body = put_call[1].get("json") or (put_call[0][1] if len(put_call[0]) > 1 else None)
    if body and "issues" in body:
        return body["issues"][0]
    return body


class TestPushClassifications:
    """Test pushing classifications to RP."""

    _DEFAULT_LOCATORS = {
        "PRODUCT_BUG": "pb001",
        "AUTOMATION_BUG": "ab001",
        "SYSTEM_ISSUE": "si001",
    }

    def _make_failure(self, classification="PRODUCT BUG", details="Analysis text"):
        failure = MagicMock()
        failure.analysis = MagicMock()
        failure.analysis.classification = classification
        failure.analysis.details = details
        failure.analysis.product_bug_report = None
        failure.analysis.code_fix = None
        return failure

    def _setup_push_client(self, *, locators=None, put_side_effect=None):
        """Create a ReportPortalClient with mocked session for push tests."""
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(
            return_value=self._DEFAULT_LOCATORS if locators is None else locators
        )
        mock_session = MagicMock()
        if put_side_effect is not None:
            mock_session.put.side_effect = put_side_effect
        else:
            mock_put_response = MagicMock()
            mock_put_response.raise_for_status = MagicMock()
            mock_session.put.return_value = mock_put_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session
        return client, mock_session

    def test_push_product_bug(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        # Mock get_defect_type_locators
        client.get_defect_type_locators = MagicMock(
            return_value={
                "PRODUCT_BUG": "pb001",
                "AUTOMATION_BUG": "ab001",
                "SYSTEM_ISSUE": "si001",
            }
        )
        # Mock the PUT call
        mock_session = MagicMock()
        mock_put_response = MagicMock()
        mock_put_response.raise_for_status = MagicMock()
        mock_session.put.return_value = mock_put_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        failure = self._make_failure("PRODUCT BUG", "The auth service is broken")
        matched = [({"id": 100, "name": "test_login"}, failure)]

        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc-123"
        )
        assert result["pushed"] == 1
        assert result["errors"] == []
        # Verify the PUT was called
        mock_session.put.assert_called_once()

    def test_push_code_issue(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(
            return_value={
                "PRODUCT_BUG": "pb001",
                "AUTOMATION_BUG": "ab001",
                "SYSTEM_ISSUE": "si001",
            }
        )
        mock_session = MagicMock()
        mock_put_response = MagicMock()
        mock_put_response.raise_for_status = MagicMock()
        mock_session.put.return_value = mock_put_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        failure = self._make_failure("CODE ISSUE", "Missing import")
        matched = [({"id": 101, "name": "test_import"}, failure)]

        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc-123"
        )
        assert result["pushed"] == 1

    def test_push_infrastructure_from_history(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(
            return_value={
                "PRODUCT_BUG": "pb001",
                "AUTOMATION_BUG": "ab001",
                "SYSTEM_ISSUE": "si001",
            }
        )
        mock_session = MagicMock()
        mock_put_response = MagicMock()
        mock_put_response.raise_for_status = MagicMock()
        mock_session.put.return_value = mock_put_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        failure = self._make_failure("PRODUCT BUG", "Network timeout")
        failure.test_name = "test_network"
        matched = [({"id": 102, "name": "test_network"}, failure)]

        result = client.push_classifications(
            matched,
            "http://jji.example.com/results/abc-123",
            history_classifications={"test_network": "INFRASTRUCTURE"},
        )
        assert result["pushed"] == 1
        # Verify the PUT used the System Issue locator due to history classification
        payload = _extract_put_json(mock_session)
        assert payload is not None
        assert payload["issue"]["issueType"] == "si001"
        assert payload["issue"]["autoAnalyzed"] is False
        assert payload["issue"]["ignoreAnalyzer"] is True

    def test_push_unmapped_classification_skipped(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(
            return_value={
                "PRODUCT_BUG": "pb001",
                "AUTOMATION_BUG": "ab001",
                "SYSTEM_ISSUE": "si001",
            }
        )
        mock_session = MagicMock()
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        failure = self._make_failure("SOMETHING UNKNOWN", "text")
        matched = [({"id": 103, "name": "test_x"}, failure)]

        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc-123"
        )
        assert result["pushed"] == 0
        assert len(result["unmatched"]) == 1

    def test_push_with_jira_matches_as_external_issues(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(
            return_value={"PRODUCT_BUG": "pb001", "AUTOMATION_BUG": "ab001"}
        )
        mock_session = MagicMock()
        mock_put_response = MagicMock()
        mock_put_response.raise_for_status = MagicMock()
        mock_session.put.return_value = mock_put_response
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        failure = self._make_failure("PRODUCT BUG", "Bug text")
        jira_match = MagicMock()
        jira_match.url = "https://jira.example.com/browse/PROJ-123"
        jira_match.key = "PROJ-123"
        jira_match.summary = "Known bug"
        failure.analysis.product_bug_report = MagicMock()
        failure.analysis.product_bug_report.jira_matches = [jira_match]
        matched = [({"id": 104, "name": "test_jira"}, failure)]

        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc-123"
        )
        assert result["pushed"] == 1
        # Verify PUT payload includes external issues
        payload = _extract_put_json(mock_session)
        assert payload is not None
        ext_issues = payload["issue"]["externalSystemIssues"]
        assert len(ext_issues) == 1
        assert ext_issues[0]["ticketId"] == "PROJ-123"
        assert ext_issues[0]["btsProject"] == "PROJ"
        assert ext_issues[0]["url"] == "https://jira.example.com/browse/PROJ-123"

    def test_push_skips_item_without_id(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(
            return_value={"PRODUCT_BUG": "pb001"}
        )
        mock_session = MagicMock()
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        failure = self._make_failure("PRODUCT BUG", "Details")
        # RP item without 'id' field
        matched = [({"name": "test_no_id"}, failure)]

        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc-123"
        )
        assert result["pushed"] == 0
        assert len(result["errors"]) == 1
        assert "missing 'id'" in result["errors"][0]
        mock_session.put.assert_not_called()

    def test_push_error_handling(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(
            return_value={"PRODUCT_BUG": "pb001"}
        )
        mock_session = MagicMock()
        mock_session.put.side_effect = Exception("RP API error")
        mock_rp = MagicMock()
        mock_rp.base_url_v1 = "http://rp.example.com/api/v1/proj"
        client._rp_client = mock_rp
        client._session = mock_session

        failure = self._make_failure("PRODUCT BUG", "text")
        matched = [({"id": 105, "name": "test_err"}, failure)]

        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc-123"
        )
        assert result["pushed"] == 0
        assert len(result["errors"]) == 1

    def test_empty_matched_list(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        client.get_defect_type_locators = MagicMock(return_value={})
        result = client.push_classifications(
            [], "http://jji.example.com/results/abc-123"
        )
        assert result["pushed"] == 0
        assert result["errors"] == []
        assert result["unmatched"] == []

    @patch("jenkins_job_insight.reportportal.logger")
    def test_http_error_extracts_rp_message(self, mock_logger):
        """HTTPError responses extract the RP JSON message field into errors."""
        mock_error_response = MagicMock()
        mock_error_response.status_code = 403
        mock_error_response.text = '{"message": "Not a launch owner"}'
        mock_error_response.json.return_value = {"message": "Not a launch owner"}
        mock_put_response = MagicMock()
        mock_put_response.raise_for_status.side_effect = _requests.exceptions.HTTPError(
            response=mock_error_response
        )

        client, _mock_session = self._setup_push_client(
            locators={"PRODUCT_BUG": "pb001"}
        )
        _mock_session.put.return_value = mock_put_response

        failure = self._make_failure("PRODUCT BUG", "Bug")
        matched = [({"id": 200, "name": "test_x"}, failure)]
        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc"
        )
        assert result["pushed"] == 0
        assert len(result["errors"]) == 1
        assert "403" in result["errors"][0]
        assert "Not a launch owner" in result["errors"][0]

        # Verify error logged with status, detail, and response body
        error_calls = [
            c
            for c in mock_logger.error.call_args_list
            if "batch update failed" in str(c).lower()
        ]
        assert error_calls, "Expected error log for HTTP error"
        log_msg = str(error_calls[0])
        assert "403" in log_msg
        assert "Not a launch owner" in log_msg
        # Response body included in ERROR log (not separate DEBUG)
        assert '{"message": "Not a launch owner"}' in log_msg

    @patch("jenkins_job_insight.reportportal.logger")
    def test_generic_exception_uses_type_name(self, mock_logger):
        """Non-HTTP exceptions use type(exc).__name__ in the error."""
        client, _ = self._setup_push_client(
            locators={"PRODUCT_BUG": "pb001"},
            put_side_effect=ConnectionError("connection refused"),
        )

        failure = self._make_failure("PRODUCT BUG", "Bug")
        matched = [({"id": 201, "name": "test_y"}, failure)]
        result = client.push_classifications(
            matched, "http://jji.example.com/results/abc"
        )
        assert result["pushed"] == 0
        assert len(result["errors"]) == 1
        assert "Error updating" in result["errors"][0]

        # Verify error logged with error details
        error_calls = [
            c
            for c in mock_logger.error.call_args_list
            if "batch update failed" in str(c).lower()
        ]
        assert error_calls, "Expected error log for generic exception"
        log_msg = str(error_calls[0])
        assert "connection refused" in log_msg


# -- close tests --------------------------------------------------------------


class TestClose:
    def test_close_calls_rp_client_close(self):
        client = ReportPortalClient(
            url="http://rp.example.com", token="tok", project="proj"
        )
        mock_rp = MagicMock()
        mock_sess = MagicMock()
        client._rp_client = mock_rp
        client._session = mock_sess
        client.close()
        mock_sess.close.assert_called_once()
        mock_rp.close.assert_called_once()


# -- thread-safety tests ----------------------------------------------------


class TestRPClientInitLock:
    """Verify RPClient init is serialised via a module-level threading lock."""

    def test_module_level_lock_exists_and_is_threading_lock(self):
        """_RPCLIENT_INIT_LOCK must be a threading.Lock at module scope."""
        assert hasattr(rp_module, "_RPCLIENT_INIT_LOCK")
        # threading.Lock() returns _thread.lock; check it has acquire/release
        lock = rp_module._RPCLIENT_INIT_LOCK
        assert callable(getattr(lock, "acquire", None))
        assert callable(getattr(lock, "release", None))

    def test_init_acquires_lock_during_rpclient_creation(self):
        """Constructor must hold _RPCLIENT_INIT_LOCK while creating RPClient."""
        acquired_during_init = []

        original_rpclient = rp_module.RPClient

        def spy_rpclient(*args, **kwargs):
            """Record whether the lock is held when RPClient.__init__ runs."""
            lock = rp_module._RPCLIENT_INIT_LOCK
            # locked() returns True when the lock is held by any thread
            acquired_during_init.append(lock.locked())
            return original_rpclient(*args, **kwargs)

        with patch.object(rp_module, "RPClient", side_effect=spy_rpclient):
            ReportPortalClient(url="http://rp.example.com", token="tok", project="proj")

        assert acquired_during_init, "RPClient was never called"
        assert acquired_during_init[0] is True, (
            "_RPCLIENT_INIT_LOCK was NOT held when RPClient was initialised"
        )

    def test_init_timeout_when_lock_held(self):
        """TimeoutError raised when lock cannot be acquired within timeout."""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False  # Simulate timeout
        with patch.object(rp_module, "_RPCLIENT_INIT_LOCK", mock_lock):
            with pytest.raises(TimeoutError, match="Timed out waiting"):
                ReportPortalClient(
                    url="http://rp.example.com",
                    token="tok",
                    project="proj",
                )
        mock_lock.acquire.assert_called_once_with(timeout=30)
        # Lock release should NOT be called since acquire failed
        mock_lock.release.assert_not_called()
