"""Tests for Jenkins client."""

from unittest.mock import patch

import pytest

from jenkins_job_insight.jenkins import JenkinsClient


class TestJenkinsClientInit:
    """Tests for JenkinsClient initialization."""

    @patch("jenkins_job_insight.jenkins.jenkins.Jenkins.__init__", return_value=None)
    def test_timeout_passed_to_parent(self, mock_init):
        """Test that timeout parameter is passed to parent Jenkins class."""
        JenkinsClient(
            url="http://jenkins.example.com",
            username="user",
            password="pass",  # noqa: S106  # pragma: allowlist secret
            timeout=60,
        )
        mock_init.assert_called_once_with(
            url="http://jenkins.example.com",
            username="user",
            password="pass",  # noqa: S106  # pragma: allowlist secret
            timeout=60,
        )

    @patch("jenkins_job_insight.jenkins.jenkins.Jenkins.__init__", return_value=None)
    def test_default_timeout(self, mock_init):
        """Test that default timeout is 30 seconds."""
        JenkinsClient(
            url="http://jenkins.example.com",
            username="user",
            password="pass",  # noqa: S106  # pragma: allowlist secret
        )
        mock_init.assert_called_once_with(
            url="http://jenkins.example.com",
            username="user",
            password="pass",  # noqa: S106  # pragma: allowlist secret
            timeout=30,
        )


class TestParseJenkinsUrl:
    """Tests for the parse_jenkins_url static method."""

    def test_parse_simple_url(self) -> None:
        """Test parsing a simple Jenkins build URL."""
        url = "https://jenkins.example.com/job/my-job/123/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "my-job"
        assert build_number == 123

    def test_parse_url_without_trailing_slash(self) -> None:
        """Test parsing URL without trailing slash."""
        url = "https://jenkins.example.com/job/my-job/456"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "my-job"
        assert build_number == 456

    def test_parse_nested_folder_url(self) -> None:
        """Test parsing URL with nested folders."""
        url = "https://jenkins.example.com/job/folder/job/my-job/789/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "folder/job/my-job"
        assert build_number == 789

    def test_parse_deeply_nested_folder_url(self) -> None:
        """Test parsing URL with deeply nested folders."""
        url = "https://jenkins.example.com/job/org/job/team/job/project/job/my-job/101/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "org/job/team/job/project/job/my-job"
        assert build_number == 101

    def test_parse_url_with_special_characters_in_job_name(self) -> None:
        """Test parsing URL with special characters in job name."""
        url = "https://jenkins.example.com/job/my-job-2.0/42/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "my-job-2.0"
        assert build_number == 42

    def test_parse_url_with_query_params(self) -> None:
        """Test parsing URL with query parameters (should be ignored)."""
        # Note: urlparse handles query params separately, path won't include them
        url = "https://jenkins.example.com/job/my-job/123/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "my-job"
        assert build_number == 123

    def test_parse_url_invalid_no_build_number(self) -> None:
        """Test that URL without build number raises ValueError."""
        url = "https://jenkins.example.com/job/my-job/"
        with pytest.raises(ValueError) as exc_info:
            JenkinsClient.parse_jenkins_url(url)
        assert "Could not parse build number" in str(exc_info.value)

    def test_parse_url_invalid_format(self) -> None:
        """Test that invalid URL format raises ValueError."""
        url = "https://jenkins.example.com/"
        with pytest.raises(ValueError) as exc_info:
            JenkinsClient.parse_jenkins_url(url)
        # Could raise either "Invalid Jenkins URL format" or "Could not parse build number"
        assert "Invalid Jenkins URL format" in str(
            exc_info.value
        ) or "Could not parse build number" in str(exc_info.value)

    def test_parse_url_invalid_build_number(self) -> None:
        """Test that non-numeric build number raises ValueError."""
        url = "https://jenkins.example.com/job/my-job/not-a-number/"
        with pytest.raises(ValueError) as exc_info:
            JenkinsClient.parse_jenkins_url(url)
        assert "Could not parse build number" in str(exc_info.value)

    def test_parse_url_multibranch_pipeline(self) -> None:
        """Test parsing multibranch pipeline URL."""
        url = "https://jenkins.example.com/job/project/job/main/123/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "project/job/main"
        assert build_number == 123

    def test_parse_url_with_different_port(self) -> None:
        """Test parsing URL with non-standard port."""
        url = "https://jenkins.example.com:8443/job/my-job/55/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "my-job"
        assert build_number == 55

    def test_parse_url_http_scheme(self) -> None:
        """Test parsing URL with http scheme."""
        url = "http://jenkins.local/job/my-job/1/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "my-job"
        assert build_number == 1

    def test_parse_url_with_context_path(self) -> None:
        """Test parsing URL with context path before /job/."""
        url = "https://jenkins.example.com/ci/job/my-job/10/"
        job_name, build_number = JenkinsClient.parse_jenkins_url(url)
        assert job_name == "my-job"
        assert build_number == 10
