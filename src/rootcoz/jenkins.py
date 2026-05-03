"""Jenkins API client wrapper."""

import os
from urllib.parse import urlparse

import jenkins
import requests
import urllib3
from pydantic import HttpUrl
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


class JenkinsClient(jenkins.Jenkins):
    """Extended Jenkins client with helper methods."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        ssl_verify: bool = True,
        timeout: int = 30,
    ) -> None:
        """Initialize Jenkins client.

        Args:
            url: Jenkins server URL.
            username: Jenkins username.
            password: Jenkins password or API token.
            ssl_verify: Whether to verify SSL certificates. Set to False for self-signed certs.
            timeout: HTTP request timeout in seconds.
        """
        super().__init__(url=url, username=username, password=password, timeout=timeout)
        logger.info(f"Connecting to Jenkins: {url}")
        if not ssl_verify:
            self._session.verify = False
            # Suppress InsecureRequestWarning
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def session(self) -> "requests.Session":
        """Authenticated requests session for direct API calls."""
        return self._session

    def get_build_console(self, job_name: str, build_number: int) -> str:
        """Get console output for a build.

        Args:
            job_name: Name of the Jenkins job.
            build_number: Build number to retrieve.

        Returns:
            Console output as a string.
        """
        logger.debug(f"Fetching console output: {job_name} #{build_number}")
        return self.get_build_console_output(job_name, build_number)

    def get_build_info_safe(self, job_name: str, build_number: int) -> dict:
        """Get build information safely.

        Args:
            job_name: Name of the Jenkins job.
            build_number: Build number to retrieve.

        Returns:
            Build information dictionary.
        """
        logger.debug(f"Fetching build info: {job_name} #{build_number}")
        return super().get_build_info(job_name, build_number)

    def get_test_report(self, job_name: str, build_number: int) -> dict | None:
        """Get test report for a build.

        Uses the Jenkins /testReport/api/json endpoint which provides structured
        test results with all failures in a parseable format.

        Args:
            job_name: Name of the Jenkins job.
            build_number: Build number to retrieve.

        Returns:
            Test report dictionary if available, None if no test report exists.

        Raises:
            jenkins.JenkinsException: If there's an error other than 404 (not found).
        """
        logger.debug(f"Fetching test report: {job_name} #{build_number}")
        try:
            return self.get_build_test_report(job_name, build_number)
        except jenkins.NotFoundException:
            # No test report available (404)
            return None
        except jenkins.JenkinsException as err:
            logger.warning(
                "Failed to fetch test report: %s #%s - %s",
                job_name,
                build_number,
                err,
            )
            raise

    @staticmethod
    def parse_jenkins_url(url: str | HttpUrl) -> tuple[str, int]:
        """Parse Jenkins URL to extract job name and build number.

        Handles various Jenkins URL formats including nested folders.

        Args:
            url: Full Jenkins build URL (string or HttpUrl).

        Returns:
            Tuple of (job_name, build_number).

        Raises:
            ValueError: If URL format is invalid.

        Examples:
            >>> JenkinsClient.parse_jenkins_url("https://jenkins.example.com/job/my-job/123/")
            ('my-job', 123)
            >>> JenkinsClient.parse_jenkins_url("https://jenkins.example.com/job/folder/job/my-job/456")
            ('folder/job/my-job', 456)
        """
        path = urlparse(str(url)).path.rstrip("/")
        parts = path.split("/")

        if len(parts) < 2:
            raise ValueError(f"Invalid Jenkins URL format: {url}")

        try:
            build_number = int(parts[-1])
        except ValueError as err:
            raise ValueError(f"Could not parse build number from URL: {url}") from err

        # Find the job path by looking for /job/ segments
        job_parts = []
        i = 0
        while i < len(parts) - 1:
            if parts[i] == "job" and i + 1 < len(parts):
                job_parts.append(parts[i + 1])
                i += 2
            else:
                i += 1

        if not job_parts:
            # Fallback: assume the part before build number is the job name
            job_name = parts[-2]
        else:
            job_name = "/job/".join(job_parts)

        return job_name, build_number
