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
import shutil
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

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

# Maximum size for prowjob.json (bytes)
_MAX_SIZE_PROWJOB = 2_000_000  # 2 MB

# Maximum total download size for non-JUnit artifacts (bytes)
_MAX_SIZE_ARTIFACTS_TOTAL = 50_000_000  # 50 MB

# Maximum size for a single non-JUnit artifact file (bytes)
_MAX_SIZE_SINGLE_ARTIFACT = 10_000_000  # 10 MB

# Base directory for artifact extraction
_ARTIFACTS_BASE = Path("/tmp/prow-artifacts")


@dataclass
class ProwJobMetadata:
    """Metadata extracted from ``prowjob.json``.

    Prow uploads ``prowjob.json`` alongside build artifacts.  It contains
    the full ProwJob spec including job type, repository refs, PR info,
    and job status.
    """

    job_type: str = ""
    """Job type: ``presubmit``, ``periodic``, ``postsubmit``, or ``batch``."""

    org: str = ""
    """Source repository organisation (e.g. ``kubevirt``)."""

    repo: str = ""
    """Source repository name (e.g. ``kubevirt``)."""

    base_ref: str = ""
    """Base branch (e.g. ``main``)."""

    pr_number: int | None = None
    """Pull request number (presubmit jobs only; first PR for batch)."""

    pr_author: str = ""
    """PR author login (presubmit jobs only; first PR for batch)."""

    additional_prs: list[dict] | None = None
    """Additional PRs for batch jobs: ``[{"number": N, "author": "..."}]``."""

    state: str = ""
    """Job result: ``success``, ``failure``, ``aborted``, ``error``."""


def _parse_prowjob_json(raw: str) -> ProwJobMetadata | None:
    """Parse ``prowjob.json`` content into ``ProwJobMetadata``.

    Args:
        raw: Raw JSON content of the prowjob.json file.

    Returns:
        Parsed metadata, or ``None`` if the JSON is unparseable.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    meta = ProwJobMetadata()

    # spec.type
    spec = data.get("spec", {})
    if not isinstance(spec, dict):
        return None
    meta.job_type = spec.get("type", "")

    # spec.refs — repository info and PR details
    refs = spec.get("refs") or {}
    if isinstance(refs, dict):
        meta.org = refs.get("org", "")
        meta.repo = refs.get("repo", "")
        meta.base_ref = refs.get("base_ref", "")

        pulls = refs.get("pulls") or []
        if isinstance(pulls, list) and pulls and isinstance(pulls[0], dict):
            pr_num = pulls[0].get("number")
            meta.pr_number = pr_num if isinstance(pr_num, int) else None
            meta.pr_author = pulls[0].get("author", "")
            # Preserve additional PRs for batch jobs
            if len(pulls) > 1:
                meta.additional_prs = [
                    {
                        "number": p.get("number"),
                        "author": p.get("author", ""),
                    }
                    for p in pulls[1:]
                    if isinstance(p, dict)
                ]

    # status.state
    status = data.get("status", {})
    if isinstance(status, dict):
        meta.state = status.get("state", "")

    return meta


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
        super().__init__(f"GCS {label} returned {status_code}: {url}")


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


async def _fetch_gcs_response(
    client: httpx.AsyncClient,
    url: str,
    *,
    label: str = "",
    max_size: int = _MAX_SIZE_JUNIT_XML,
) -> httpx.Response | None:
    """Fetch a GCS object and validate size limits.

    Shared implementation for both text and binary GCS fetches.

    Args:
        client: httpx async client.
        url: Full URL to fetch.
        label: Human-readable label for log messages.
        max_size: Maximum response size in bytes.

    Returns:
        The validated ``httpx.Response`` on success, ``None`` on 404.

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
            "GCS %s access error (%d): %s",
            effective_label,
            exc.response.status_code,
            url,
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
    return resp


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
    resp = await _fetch_gcs_response(client, url, label=label, max_size=max_size)
    if resp is None:
        return None
    return resp.text


async def _fetch_gcs_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    label: str = "",
    max_size: int = _MAX_SIZE_SINGLE_ARTIFACT,
) -> bytes | None:
    """Fetch raw bytes from GCS.

    Args:
        client: httpx async client.
        url: Full URL to fetch.
        label: Human-readable label for log messages.
        max_size: Maximum response size in bytes.

    Returns:
        Response bytes on success, ``None`` on 404.

    Raises:
        GCSAccessError: On non-404 HTTP errors (403, 500, etc.).
        GCSOversizeError: When the response exceeds *max_size*.
    """
    resp = await _fetch_gcs_response(client, url, label=label, max_size=max_size)
    if resp is None:
        return None
    return resp.content


async def _list_gcs_objects(
    client: httpx.AsyncClient,
    bucket: str,
    prefix: str,
    *,
    filter_fn: Callable[[dict], bool] | None = None,
    warnings: list[str] | None = None,
) -> list[dict]:
    """List GCS objects under a prefix, optionally filtered.

    Args:
        client: httpx async client.
        bucket: GCS bucket name.
        prefix: Object prefix to search under.
        filter_fn: Optional predicate applied to each item dict from the
            GCS JSON API.  When provided, only items where ``filter_fn(item)``
            returns ``True`` are included.
        warnings: Optional list to append truncation warnings to.

    Returns:
        List of GCS object dicts (keys: ``name``, ``size``, etc.).
    """
    matched: list[dict] = []
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
                "gcs-listing", exc.response.status_code, api_url
            ) from exc
        except httpx.HTTPError as exc:
            raise GCSAccessError("gcs-listing", 0, api_url) from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise GCSAccessError("gcs-listing-parse", 0, api_url) from exc

        for item in data.get("items", []):
            if filter_fn is None or filter_fn(item):
                matched.append(item)

        page_token = data.get("nextPageToken")
        if not page_token:
            break
    else:
        msg = f"GCS listing exceeded {max_pages} pages for {prefix}, results truncated"
        logger.warning(msg)
        if warnings is not None:
            warnings.append(msg)

    return matched


def _is_junit(item: dict) -> bool:
    """Return ``True`` if the GCS object looks like a JUnit XML file."""
    name = item.get("name", "")
    return name.endswith(".xml") and bool(re.search(r"junit", name, re.IGNORECASE))


async def _list_gcs_junit_files(
    client: httpx.AsyncClient,
    bucket: str,
    prefix: str,
    warnings: list[str] | None = None,
) -> list[str]:
    """List JUnit XML files under a GCS prefix using the JSON API.

    Uses the GCS JSON API ``list`` endpoint to find all ``*.xml`` files
    under the artifacts directory that look like JUnit results.

    Args:
        client: httpx async client.
        bucket: GCS bucket name.
        prefix: Object prefix to search under (e.g. ``logs/job/123/artifacts/``).
        warnings: Optional list to append truncation warnings to when
            pagination exceeds the safety limit.

    Returns:
        List of full object names (keys) for JUnit XML files.
    """
    items = await _list_gcs_objects(
        client, bucket, prefix, filter_fn=_is_junit, warnings=warnings
    )
    return [item["name"] for item in items]


async def _download_gcs_artifacts(
    client: httpx.AsyncClient,
    bucket: str,
    artifact_objects: list[dict],
    artifacts_prefix: str,
    *,
    max_total_bytes: int = _MAX_SIZE_ARTIFACTS_TOTAL,
    max_single_bytes: int = _MAX_SIZE_SINGLE_ARTIFACT,
    warnings: list[str] | None = None,
) -> Path | None:
    """Download non-JUnit artifacts from GCS to a local directory.

    Creates a temporary directory under ``_ARTIFACTS_BASE`` and downloads
    each artifact file, preserving the directory structure relative to
    the artifacts prefix.

    Artifacts exceeding ``max_single_bytes`` are skipped with a warning.
    Downloading stops when the total downloaded size exceeds ``max_total_bytes``.

    Args:
        client: httpx async client.
        bucket: GCS bucket name.
        artifact_objects: GCS object dicts (from ``_list_gcs_objects``).
        artifacts_prefix: The GCS prefix used for listing (e.g.
            ``logs/job/123/artifacts/``).  Used to compute relative paths
            for local storage.
        max_total_bytes: Maximum total download size.
        max_single_bytes: Maximum size for a single file.
        warnings: Optional list to append warnings to.

    Returns:
        Path to the artifacts directory, or ``None`` if nothing was downloaded.
    """
    if not artifact_objects:
        return None

    dest_dir = _ARTIFACTS_BASE / f"prow-{uuid.uuid4().hex}"
    dest_dir.mkdir(parents=True, exist_ok=False)

    total_downloaded = 0
    files_downloaded = 0
    dest_dir_resolved = dest_dir.resolve()

    try:
        for obj in artifact_objects:
            obj_name = obj.get("name", "")
            if not obj_name:
                continue

            # Compute relative path by stripping the artifacts prefix
            if obj_name.startswith(artifacts_prefix):
                rel_path = obj_name[len(artifacts_prefix) :]
            else:
                rel_path = obj_name

            # Skip empty relative paths (the prefix directory itself)
            if not rel_path or rel_path == "/":
                continue

            # Skip GCS directory marker objects
            if rel_path.endswith("/"):
                continue

            # Path traversal / absolute path validation
            if rel_path.startswith("/") or ".." in rel_path.split("/"):
                logger.warning("Skipping artifact with unsafe path: %s", rel_path)
                continue

            target = (dest_dir / rel_path).resolve()
            if not str(target).startswith(str(dest_dir_resolved) + os.sep):
                logger.warning("Skipping artifact escaping artifact dir: %s", rel_path)
                continue

            # Check single-file size from GCS metadata
            try:
                obj_size = int(obj.get("size", 0))
            except (ValueError, TypeError):
                obj_size = 0

            if obj_size > max_single_bytes:
                msg = (
                    f"Skipping artifact {rel_path}: size {obj_size} bytes "
                    f"exceeds single-file limit ({max_single_bytes} bytes)"
                )
                logger.warning(msg)
                if warnings is not None:
                    warnings.append(msg)
                continue

            # Check total budget before downloading
            if total_downloaded + obj_size > max_total_bytes:
                msg = (
                    f"Artifact download budget exhausted ({total_downloaded} bytes downloaded, "
                    f"limit {max_total_bytes} bytes) \u2014 skipping remaining artifacts"
                )
                logger.warning(msg)
                if warnings is not None:
                    warnings.append(msg)
                break

            # Download
            url = _gcs_url(bucket, obj_name)
            try:
                data = await _fetch_gcs_bytes(
                    client, url, label=rel_path, max_size=max_single_bytes
                )
            except (GCSAccessError, GCSOversizeError) as exc:
                logger.warning("Failed to download artifact %s: %s", rel_path, exc)
                if warnings is not None:
                    warnings.append(f"Failed to download artifact {rel_path}: {exc}")
                continue

            if data is None:
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            total_downloaded += len(data)
            files_downloaded += 1

            # Re-check budget after download (defense-in-depth for
            # objects with missing/inaccurate GCS size metadata)
            if total_downloaded >= max_total_bytes:
                msg = (
                    f"Artifact download budget reached after download "
                    f"({total_downloaded} bytes, limit {max_total_bytes} bytes) "
                    "\u2014 skipping remaining artifacts"
                )
                logger.warning(msg)
                if warnings is not None:
                    warnings.append(msg)
                break
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise

    if files_downloaded == 0:
        shutil.rmtree(dest_dir, ignore_errors=True)
        return None

    logger.info(
        "Downloaded %d artifact(s) (%d bytes) to %s",
        files_downloaded,
        total_downloaded,
        dest_dir,
    )
    return dest_dir


class ProwSource(CISource):
    """CI source plugin for Prow jobs.

    Fetches build data from GCS where Prow stores artifacts:
    - ``build-log.txt`` — full build log
    - ``finished.json`` — build result and timestamps
    - ``artifacts/**/junit*.xml`` — JUnit test results
    - Non-JUnit artifacts (pod logs, gather-extra dumps, etc.)

    The GCS path convention is:
    - Periodic/postsubmit: ``gs://{bucket}/logs/{job_name}/{build_id}/``
    - Presubmit (PR): ``gs://{bucket}/pr-logs/pull/{org}_{repo}/{pr}/{job_name}/{build_id}/``

    For presubmit jobs, the path is auto-resolved via the Prow directory
    pointer file at ``pr-logs/directory/{job_name}/{build_id}.txt``.
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
        get_job_artifacts: bool = True,
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
            get_job_artifacts: When True, download non-JUnit build artifacts
                for AI exploration.
        """
        self.job_name = job_name
        self.build_id = build_id
        self.gcs_bucket = gcs_bucket
        self.prow_url = prow_url
        self._custom_gcs_prefix = gcs_prefix
        self._resolved_gcs_prefix: str | None = None
        self._prowjob_metadata: ProwJobMetadata | None = None
        self._resolution_warnings: list[str] = []
        self.force = force
        self.get_job_artifacts = get_job_artifacts
        self._extract_path: Path | None = None

    @property
    def build_url(self) -> str:
        """Construct the Prow Deck build URL."""
        return _build_url(self.prow_url, self.gcs_bucket, self._gcs_prefix)

    @property
    def _gcs_prefix(self) -> str:
        """GCS object prefix for this build's artifacts.

        After ``_resolve_gcs_prefix()`` runs, this returns the
        resolved prefix (which may differ from the default for PR jobs).
        """
        if self._resolved_gcs_prefix is not None:
            return self._resolved_gcs_prefix
        if self._custom_gcs_prefix:
            return self._custom_gcs_prefix
        return f"logs/{self.job_name}/{self.build_id}"

    async def _resolve_gcs_prefix(self, client: httpx.AsyncClient) -> str:
        """Resolve the GCS prefix and fetch job metadata.

        Resolution order:

        1. If an explicit ``gcs_prefix`` was provided, use it and attempt
           to fetch ``prowjob.json`` there for metadata.
        2. Otherwise, try ``prowjob.json`` at the default ``logs/`` path.
           If found, the path is confirmed (periodic/postsubmit) **and**
           we get metadata in a single request.
        3. If not found, check the Prow directory pointer file at
           ``pr-logs/directory/{job}/{build_id}.txt``.  If it exists,
           resolve the real path and fetch ``prowjob.json`` there.
        4. Fall back to ``logs/{job}/{build_id}`` with no metadata.
        """
        if self._custom_gcs_prefix:
            # Explicit prefix — just try to fetch metadata there
            await self._fetch_prowjob_metadata(client, self._custom_gcs_prefix)
            return self._custom_gcs_prefix

        default = f"logs/{self.job_name}/{self.build_id}"

        # Try prowjob.json at the default logs/ path — if found AND the
        # job type is consistent with the logs/ path (periodic/postsubmit),
        # confirm the path and use the metadata.  Presubmit/batch types at
        # logs/ are contradictory — continue to pointer resolution.
        if await self._fetch_prowjob_metadata(client, default):
            jt = self._prowjob_metadata.job_type if self._prowjob_metadata else ""
            if jt in ("periodic", "postsubmit"):
                return default
            # Metadata is empty, unknown, presubmit, or batch at logs/ —
            # not authoritative for this path, continue to pointer resolution
            logger.info(
                "prowjob.json at default path has type=%s, "
                "continuing to pointer resolution",
                jt,
            )
            self._prowjob_metadata = None

        # Default path didn't have prowjob.json — check the directory
        # pointer file (standard for presubmit/batch jobs)
        pointer_path = f"pr-logs/directory/{self.job_name}/{self.build_id}.txt"
        pointer_url = _gcs_url(self.gcs_bucket, pointer_path)
        try:
            pointer_content = await _fetch_gcs_text(
                client,
                pointer_url,
                label="directory-pointer",
                max_size=_MAX_SIZE_FINISHED,
            )
        except GCSAccessError as exc:
            # Non-404 errors (403, 500, network errors, oversize) are tracked
            # as warnings so callers know resolution was degraded.
            # 404 is expected (periodic/postsubmit jobs have no pointer file).
            if exc.status_code != 404:
                self._resolution_warnings.append(str(exc))
            pointer_content = None

        if not pointer_content:
            return default

        pointer_content = pointer_content.strip()
        expected_prefix = f"gs://{self.gcs_bucket}/"
        if not pointer_content.startswith(expected_prefix):
            logger.warning(
                "Directory pointer content does not match expected bucket "
                "(expected gs://%s/..., got %s) — using default prefix",
                self.gcs_bucket,
                pointer_content,
            )
            return default

        resolved = pointer_content[len(expected_prefix) :].rstrip("/")
        # Validate: pointer content is from external GCS — reject
        # suspicious values (newlines, control chars, excessive length,
        # path traversal, or mismatched job/build)
        if any(c in resolved for c in "\n\r\x00") or len(resolved) > 500:
            logger.warning(
                "Suspicious directory pointer content (len=%d), using default prefix",
                len(resolved),
            )
            return default

        if ".." in resolved:
            logger.warning("Directory pointer contains path traversal: %s", resolved)
            return default

        if not resolved.startswith("pr-logs/"):
            logger.warning(
                "Directory pointer path is not under pr-logs/: %s "
                "\u2014 using default prefix",
                resolved,
            )
            return default

        expected_suffix = f"/{self.job_name}/{self.build_id}"
        if not resolved.endswith(expected_suffix):
            logger.warning(
                "Directory pointer path does not end with /%s/%s: %s "
                "\u2014 using default prefix",
                self.job_name,
                self.build_id,
                resolved,
            )
            return default

        logger.info("Resolved GCS prefix via directory pointer: %s", resolved)

        # Fetch metadata at the resolved path
        await self._fetch_prowjob_metadata(client, resolved)

        return resolved

    async def _fetch_prowjob_metadata(
        self, client: httpx.AsyncClient, prefix: str
    ) -> bool:
        """Fetch and parse ``prowjob.json`` at the given prefix.

        Stores the result in ``self._prowjob_metadata``.  On failure,
        ``self._prowjob_metadata`` remains ``None`` (non-fatal).

        Returns:
            ``True`` if prowjob.json was found and parsed, ``False`` otherwise.
        """
        prowjob_url = _gcs_url(self.gcs_bucket, prefix, "prowjob.json")
        try:
            prowjob_text = await _fetch_gcs_text(
                client,
                prowjob_url,
                label="prowjob.json",
                max_size=_MAX_SIZE_PROWJOB,
            )
        except GCSAccessError:
            return False

        if not prowjob_text:
            return False

        self._prowjob_metadata = _parse_prowjob_json(prowjob_text)
        if self._prowjob_metadata:
            logger.info(
                "Parsed prowjob.json metadata: type=%s org=%s repo=%s pr=%s",
                self._prowjob_metadata.job_type,
                self._prowjob_metadata.org,
                self._prowjob_metadata.repo,
                self._prowjob_metadata.pr_number,
            )
            return True
        return False

    def _metadata_dict(self) -> dict:
        """Return prowjob metadata as a dict for ``CISourceResult``."""
        if not self._prowjob_metadata:
            return {}
        return {
            k: v
            for k, v in asdict(self._prowjob_metadata).items()
            if v is not None and v != ""
        }

    async def fetch(self) -> CISourceResult:
        """Fetch build data from GCS and return normalized result.

        Steps:
          1. Fetch ``finished.json`` to check build result.
          2. Fetch ``build-log.txt`` for console context.
          3. List and fetch JUnit XML files from artifacts.
          4. Extract failures from JUnit XMLs.
          5. Download non-JUnit artifacts for AI exploration.
          6. Build and return ``CISourceResult``.
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

        # Resolve GCS prefix (auto-detects PR jobs via directory pointer)
        self._resolution_warnings = []
        gcs_prefix = await self._resolve_gcs_prefix(client)
        self._resolved_gcs_prefix = gcs_prefix
        access_warnings.extend(self._resolution_warnings)

        # ------------------------------------------------------------------
        # 1. Check build result — prefer prowjob.json metadata (already
        #    fetched during prefix resolution), fall back to finished.json
        # ------------------------------------------------------------------
        build_state = ""
        if self._prowjob_metadata and self._prowjob_metadata.state:
            build_state = self._prowjob_metadata.state.upper()
        else:
            # No prowjob.json metadata — fall back to finished.json
            finished_url = _gcs_url(self.gcs_bucket, gcs_prefix, "finished.json")
            try:
                finished_text = await _fetch_gcs_text(
                    client,
                    finished_url,
                    label="finished.json",
                    max_size=_MAX_SIZE_FINISHED,
                )
            except GCSAccessError as exc:
                access_warnings.append(str(exc))
                finished_text = None

            if finished_text:
                try:
                    finished = json.loads(finished_text)
                    if not isinstance(finished, dict):
                        logger.warning(
                            "finished.json is not a JSON object (got %s)",
                            type(finished).__name__,
                        )
                    else:
                        build_state = finished.get("result", "").upper()
                except (ValueError, KeyError) as exc:
                    logger.warning("Failed to parse finished.json: %s", exc)
            else:
                logger.info(
                    "No finished.json found for %s/%s \u2014 job may still be running",
                    self.job_name,
                    self.build_id,
                )

        if build_state == "SUCCESS" and not self.force:
            logger.info(
                "Prow job %s build %s passed, skipping analysis",
                self.job_name,
                self.build_id,
            )
            return CISourceResult(
                failures=[],
                build_passed=True,
                build_url=self.build_url,
                source_metadata=self._metadata_dict(),
                warnings=access_warnings,
            )
        if build_state == "SUCCESS" and self.force:
            logger.info(
                "Prow job %s build %s passed but force=True, continuing",
                self.job_name,
                self.build_id,
            )

        # ------------------------------------------------------------------
        # 2. Fetch build-log.txt for console context
        # ------------------------------------------------------------------
        build_log_url = _gcs_url(self.gcs_bucket, gcs_prefix, "build-log.txt")
        try:
            build_log = await _fetch_gcs_text(
                client,
                build_log_url,
                label="build-log.txt",
                max_size=_MAX_SIZE_BUILD_LOG,
            )
        except GCSAccessError as exc:
            access_warnings.append(str(exc))
            build_log = None
        console_context = extract_relevant_console_lines(build_log or "")

        # ------------------------------------------------------------------
        # 3. List all artifact files from GCS
        # ------------------------------------------------------------------
        artifacts_prefix = f"{gcs_prefix}/artifacts/"
        try:
            all_artifact_objects = await _list_gcs_objects(
                client, self.gcs_bucket, artifacts_prefix, warnings=access_warnings
            )
        except GCSAccessError as exc:
            access_warnings.append(str(exc))
            all_artifact_objects = []

        # Partition into JUnit XMLs and non-JUnit artifacts
        junit_objects = [obj for obj in all_artifact_objects if _is_junit(obj)]
        junit_files = [obj["name"] for obj in junit_objects]
        non_junit_objects = [obj for obj in all_artifact_objects if not _is_junit(obj)]

        logger.info(
            "Found %d JUnit XML file(s) and %d other artifact(s) for %s/%s",
            len(junit_files),
            len(non_junit_objects),
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
        # 5. Download non-JUnit artifacts for AI exploration
        # ------------------------------------------------------------------
        extract_path: Path | None = None
        artifacts_context = ""
        if self.get_job_artifacts and non_junit_objects:
            try:
                extract_path = await _download_gcs_artifacts(
                    client,
                    self.gcs_bucket,
                    non_junit_objects,
                    artifacts_prefix,
                    warnings=access_warnings,
                )
                if extract_path:
                    artifacts_context = str(extract_path)
                    self._extract_path = extract_path
                    logger.info("Build artifacts available at %s", extract_path)
            except Exception as exc:
                logger.warning("Failed to download Prow artifacts: %s", exc)
                access_warnings.append(f"Failed to download artifacts: {exc}")

        # ------------------------------------------------------------------
        # 6. Build and return CISourceResult
        # ------------------------------------------------------------------
        return CISourceResult(
            failures=all_failures,
            console_context=console_context,
            artifacts_context=artifacts_context,
            build_url=self.build_url,
            warnings=access_warnings,
            source_metadata=self._metadata_dict(),
            extract_path=extract_path,
        )

    def cleanup(self) -> None:
        """Remove temporary artifact directory."""
        if self._extract_path and self._extract_path.exists():
            shutil.rmtree(self._extract_path, ignore_errors=True)
            logger.info("Cleaned up Prow artifacts: %s", self._extract_path)
            self._extract_path = None
