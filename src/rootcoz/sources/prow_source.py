"""Prow CI source plugin.

Implements the ``CISource`` interface to fetch build data from Prow jobs
stored in Google Cloud Storage (GCS).  Prow uploads build artifacts
(build-log.txt, finished.json, JUnit XMLs) to a GCS bucket
which is publicly accessible via HTTPS.
"""

from __future__ import annotations

import json
import os
import re

import httpx
from simple_logger.logger import get_logger

from rootcoz.engine.core import extract_relevant_console_lines
from rootcoz.models import FailedTest
from rootcoz.sources.base import CISource, CISourceResult
from rootcoz.xml_enrichment import extract_test_failures

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

# Base URL for accessing GCS objects via HTTPS
GCS_BASE_URL = "https://storage.googleapis.com"

# HTTP timeout for GCS requests (seconds)
_HTTP_TIMEOUT = 60

# Maximum download sizes per artifact type (bytes)
_MAX_SIZE_FINISHED = 1_000_000  # 1 MB
_MAX_SIZE_BUILD_LOG = 10_000_000  # 10 MB
_MAX_SIZE_JUNIT_XML = 5_000_000  # 5 MB


def _gcs_url(bucket: str, *path_parts: str) -> str:
    """Build an HTTPS URL for a GCS object.

    Args:
        bucket: GCS bucket name.
        *path_parts: Path segments within the bucket.

    Returns:
        Full HTTPS URL to the GCS object.
    """
    path = "/".join(path_parts)
    return f"{GCS_BASE_URL}/{bucket}/{path}"


def _build_url(prow_url: str, bucket: str, gcs_prefix: str) -> str:
    """Build the Prow Deck URL for a specific build.

    Args:
        prow_url: Base Prow Deck URL.
        bucket: GCS bucket name.
        gcs_prefix: GCS object prefix (e.g. ``logs/job/build`` or
            ``pr-logs/pull/org_repo/pr/job/build``).

    Returns:
        Full URL to the build on Prow Deck.
    """
    return f"{prow_url.rstrip('/')}/view/gs/{bucket}/{gcs_prefix}"


def _parse_junit_failures(raw_xml: str) -> list[FailedTest]:
    """Parse a JUnit XML string and extract failures.

    Uses the shared ``extract_test_failures`` helper from xml_enrichment
    to avoid duplicating XML parsing logic.

    Args:
        raw_xml: Raw JUnit XML content.

    Returns:
        List of FailedTest objects extracted from the XML.
    """
    try:
        return extract_test_failures(raw_xml)
    except Exception as exc:
        logger.warning("Failed to parse JUnit XML: %s", exc)
        return []


class GCSAccessError(Exception):
    """Non-404 HTTP error when accessing GCS (403, 500, etc.)."""

    def __init__(self, label: str, status_code: int | None, url: str) -> None:
        self.label = label
        self.status_code = status_code
        self.url = url
        super().__init__(
            f"GCS {label} returned {status_code}: {url}"
        )


class GCSOversizeError(GCSAccessError):
    """GCS artifact exceeds the maximum allowed download size."""

    def __init__(self, label: str, size: int, max_size: int, url: str) -> None:
        self.label = label
        self.size = size
        self.max_size = max_size
        self.url = url
        self.status_code = None  # Not an HTTP status error
        Exception.__init__(
            self,
            f"GCS {label} too large ({size} bytes, max {max_size}): {url}",
        )


def _raise_if_oversize(label: str, size: int, max_size: int, url: str) -> None:
    """Raise ``GCSOversizeError`` if *size* exceeds *max_size*."""
    if size > max_size:
        logger.warning(
            "GCS %s too large (%d bytes, max %d): %s", label, size, max_size, url
        )
        raise GCSOversizeError(label, size, max_size, url)


async def _fetch_gcs_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    label: str = "",
    max_size: int = _MAX_SIZE_JUNIT_XML,
) -> str | None:
    """Fetch a text file from GCS.

    Args:
        client: httpx async client.
        url: Full URL to fetch.
        label: Human-readable label for log messages.
        max_size: Maximum response size in bytes.  Responses exceeding
            this are rejected (``GCSOversizeError`` is raised).

    Returns:
        Response text on success, ``None`` on 404.

    Raises:
        GCSAccessError: On non-404 HTTP errors (403, 500, etc.).
        GCSOversizeError: When the response exceeds *max_size*.
    """
    effective_label = label or "file"
    # GCS always returns Content-Length, so the header check below is the
    # primary defense against oversized responses.  The body-size check is
    # a defense-in-depth fallback for the rare case where the header is
    # missing or inaccurate.
    try:
        resp = await client.get(url)
        if resp.status_code == 404:
            logger.debug("GCS %s not found: %s", effective_label, url)
            return None
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "GCS %s access error (%d): %s", effective_label, exc.response.status_code, url
        )
        raise GCSAccessError(effective_label, exc.response.status_code, url) from exc
    except httpx.HTTPError as exc:
        logger.warning("GCS network error for %s: %s", effective_label, exc)
        raise GCSAccessError(effective_label, 0, url) from exc

    try:
        content_length = int(resp.headers.get("content-length", 0))
    except (ValueError, TypeError):
        content_length = 0
    _raise_if_oversize(effective_label, content_length, max_size, url)
    # Defense-in-depth: check actual body bytes when content-length is
    # missing or lies.
    _raise_if_oversize(effective_label, len(resp.content), max_size, url)
    return resp.text


async def _list_gcs_junit_files(
    client: httpx.AsyncClient,
    bucket: str,
    prefix: str,
) -> list[str]:
    """List JUnit XML files under a GCS prefix using the JSON API.

    Uses the GCS JSON API ``list`` endpoint to find all ``*.xml`` files
    under the artifacts directory that look like JUnit results.

    Args:
        client: httpx async client.
        bucket: GCS bucket name.
        prefix: Object prefix to search under (e.g. ``logs/job/123/artifacts/``).

    Returns:
        List of full object names (keys) for JUnit XML files.
    """
    junit_files: list[str] = []
    page_token: str | None = None
    api_url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o"
    max_pages = 100

    for _page in range(max_pages):
        params: dict[str, str] = {"prefix": prefix}
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = await client.get(api_url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GCSAccessError(
                "junit-listing", exc.response.status_code, api_url
            ) from exc
        except httpx.HTTPError as exc:
            raise GCSAccessError("junit-listing", 0, api_url) from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise GCSAccessError("junit-listing-parse", 0, api_url) from exc

        for item in data.get("items", []):
            name = item.get("name", "")
            # Match files that look like JUnit XML results
            if name.endswith(".xml") and re.search(r"junit", name, re.IGNORECASE):
                junit_files.append(name)

        page_token = data.get("nextPageToken")
        if not page_token:
            break
    else:
        logger.warning(
            "GCS JUnit listing exceeded %d pages for %s, truncating",
            max_pages,
            prefix,
        )

    return junit_files


class ProwSource(CISource):
    """CI source plugin for Prow jobs.

    Fetches build data from GCS where Prow stores artifacts:
    - ``build-log.txt`` — full build log
    - ``finished.json`` — build result and timestamps
    - ``artifacts/**/junit*.xml`` — JUnit test results

    The GCS path convention is:
    ``gs://{bucket}/logs/{job_name}/{build_id}/``
    """

    def __init__(
        self,
        *,
        job_name: str,
        build_id: str,
        gcs_bucket: str,
        prow_url: str,
        gcs_prefix: str = "",
        force: bool = False,
    ) -> None:
        """Store config needed to fetch from Prow/GCS.

        Args:
            job_name: Prow job name (e.g. ``periodic-ci-openshift-release-master-nightly-4.17-e2e-aws``).
            build_id: Build ID (numeric string, e.g. ``1234567890``).
            gcs_bucket: GCS bucket name.
            prow_url: Prow Deck URL.
            gcs_prefix: GCS object prefix. When empty, defaults to ``logs/{job_name}/{build_id}``.
                For PR jobs this is ``pr-logs/pull/{org}_{repo}/{pr}/{job_name}/{build_id}``.
            force: When True, analyze even if the build passed.
        """
        self.job_name = job_name
        self.build_id = build_id
        self.gcs_bucket = gcs_bucket
        self.prow_url = prow_url
        self._custom_gcs_prefix = gcs_prefix
        self.force = force

    @property
    def build_url(self) -> str:
        """Construct the Prow Deck build URL."""
        return _build_url(self.prow_url, self.gcs_bucket, self._gcs_prefix)

    @property
    def _gcs_prefix(self) -> str:
        """GCS object prefix for this build's artifacts."""
        if self._custom_gcs_prefix:
            return self._custom_gcs_prefix
        return f"logs/{self.job_name}/{self.build_id}"

    async def fetch(self) -> CISourceResult:
        """Fetch build data from GCS and return normalized result.

        Steps:
          1. Fetch ``finished.json`` to check build result.
          2. Fetch ``build-log.txt`` for console context.
          3. List and fetch JUnit XML files from artifacts.
          4. Extract failures from JUnit XMLs.
          5. Build and return ``CISourceResult``.
        """
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            return await self._fetch_with_client(client)

    async def _fetch_with_client(self, client: httpx.AsyncClient) -> CISourceResult:
        """Inner fetch logic with an injected client (for testability).

        Args:
            client: httpx async client to use for HTTP requests.

        Returns:
            Normalized CISourceResult.
        """
        access_warnings: list[str] = []

        # ------------------------------------------------------------------
        # 1. Fetch finished.json to check build result
        # ------------------------------------------------------------------
        finished_url = _gcs_url(self.gcs_bucket, self._gcs_prefix, "finished.json")
        try:
            finished_text = await _fetch_gcs_text(
                client, finished_url, label="finished.json", max_size=_MAX_SIZE_FINISHED
            )
        except GCSAccessError as exc:
            access_warnings.append(str(exc))
            finished_text = None

        if finished_text:
            try:
                finished = json.loads(finished_text)
                result = finished.get("result", "").upper()
                if result == "SUCCESS" and not self.force:
                    logger.info(
                        "Prow job %s build %s passed, skipping analysis",
                        self.job_name,
                        self.build_id,
                    )
                    return CISourceResult(
                        failures=[],
                        build_passed=True,
                        build_url=self.build_url,
                    )
                if result == "SUCCESS" and self.force:
                    logger.info(
                        "Prow job %s build %s passed but force=True, continuing",
                        self.job_name,
                        self.build_id,
                    )
            except (ValueError, KeyError) as exc:
                logger.warning("Failed to parse finished.json: %s", exc)
        else:
            logger.info(
                "No finished.json found for %s/%s \u2014 job may still be running",
                self.job_name,
                self.build_id,
            )

        # ------------------------------------------------------------------
        # 2. Fetch build-log.txt for console context
        # ------------------------------------------------------------------
        build_log_url = _gcs_url(self.gcs_bucket, self._gcs_prefix, "build-log.txt")
        try:
            build_log = await _fetch_gcs_text(
                client, build_log_url, label="build-log.txt", max_size=_MAX_SIZE_BUILD_LOG
            )
        except GCSAccessError as exc:
            access_warnings.append(str(exc))
            build_log = None
        console_context = extract_relevant_console_lines(build_log or "")

        # ------------------------------------------------------------------
        # 3. List and fetch JUnit XML files from artifacts
        # ------------------------------------------------------------------
        artifacts_prefix = f"{self._gcs_prefix}/artifacts/"
        try:
            junit_files = await _list_gcs_junit_files(client, self.gcs_bucket, artifacts_prefix)
        except GCSAccessError as exc:
            access_warnings.append(str(exc))
            junit_files = []
        logger.info(
            "Found %d JUnit XML file(s) for %s/%s",
            len(junit_files),
            self.job_name,
            self.build_id,
        )

        # ------------------------------------------------------------------
        # 4. Fetch and parse JUnit XMLs to extract failures
        # ------------------------------------------------------------------
        all_failures: list[FailedTest] = []
        for junit_path in junit_files:
            junit_url = _gcs_url(self.gcs_bucket, junit_path)
            try:
                xml_content = await _fetch_gcs_text(
                    client, junit_url, label=junit_path, max_size=_MAX_SIZE_JUNIT_XML
                )
            except GCSAccessError as exc:
                access_warnings.append(str(exc))
                continue
            if xml_content:
                failures = _parse_junit_failures(xml_content)
                all_failures.extend(failures)

        logger.info(
            "Extracted %d test failure(s) from %d JUnit file(s)",
            len(all_failures),
            len(junit_files),
        )

        # ------------------------------------------------------------------
        # 5. Build and return CISourceResult
        # ------------------------------------------------------------------
        return CISourceResult(
            failures=all_failures,
            console_context=console_context,
            build_url=self.build_url,
            warnings=access_warnings,
        )
