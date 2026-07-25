"""Shared validation helpers for Prow configuration.

Cross-cutting URL helpers live in ``rootcoz.url_utils``.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

PROW_JOB_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
PROW_BUILD_ID_PATTERN = re.compile(r"^[0-9]+$")


def normalize_prow_url(value: object) -> str:
    """Validate and normalize a Prow Deck URL (empty allowed for server default)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("prow_url must be a string")
    url = value.strip()
    if not url:
        return ""
    if not url.startswith("https://"):
        raise ValueError("prow_url must start with https://")
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("prow_url must not contain credentials")
    if not parsed.hostname:
        raise ValueError("prow_url must include a hostname")
    return url


def normalize_gcs_bucket(value: object) -> str:
    """Validate and normalize a GCS bucket name (empty allowed for server default)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("gcs_bucket must be a string")
    bucket = value.strip()
    if not bucket:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", bucket):
        raise ValueError(
            "gcs_bucket must be lowercase alphanumeric with hyphens, dots, or underscores"
        )
    return bucket


def normalize_gcs_prefix(value: object) -> str:
    """Validate and normalize a GCS object prefix (empty allowed for auto-resolution)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("gcs_prefix must be a string")
    prefix = value.strip().rstrip("/")
    if not prefix:
        return ""
    if ".." in prefix:
        raise ValueError("gcs_prefix must not contain '..'")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_.-]*", prefix):
        raise ValueError("gcs_prefix contains invalid characters")
    return prefix


def validate_gcs_prefix_suffix(prefix: str, job_name: str, build_id: str) -> None:
    """Ensure *prefix* ends with /{job_name}/{build_id}."""
    expected_suffix = f"/{job_name}/{build_id}"
    if not prefix.endswith(expected_suffix):
        raise ValueError(
            f"gcs_prefix must end with {expected_suffix} "
            f"for prow_job_name={job_name!r} and build_id={build_id!r}"
        )


def validate_prow_job_name(value: object) -> str:
    """Validate a Prow job name."""
    if value is None:
        raise ValueError("prow_job_name is required")
    if not isinstance(value, str):
        raise ValueError("prow_job_name must be a string")
    name = value.strip()
    if not name:
        raise ValueError("prow_job_name cannot be blank")
    if not PROW_JOB_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "prow_job_name must be alphanumeric with hyphens, dots, or underscores"
        )
    return name


def validate_prow_build_id(value: object) -> str:
    """Validate a Prow build ID (numeric string)."""
    if value is None:
        raise ValueError("build_id is required")
    if not isinstance(value, str):
        raise ValueError("build_id must be a string")
    build_id = value.strip()
    if not build_id:
        raise ValueError("build_id cannot be blank")
    if not PROW_BUILD_ID_PATTERN.fullmatch(build_id):
        raise ValueError("build_id must be numeric")
    return build_id


# Re-export URL helpers for backward-compatible imports
from rootcoz.url_utils import sanitize_http_href, strip_url_userinfo  # noqa: E402

__all__ = [
    "PROW_BUILD_ID_PATTERN",
    "PROW_JOB_NAME_PATTERN",
    "normalize_gcs_bucket",
    "normalize_gcs_prefix",
    "normalize_prow_url",
    "sanitize_http_href",
    "strip_url_userinfo",
    "validate_gcs_prefix_suffix",
    "validate_prow_build_id",
    "validate_prow_job_name",
]
