"""Jenkins artifacts extraction and context building for test failure analysis."""

import io
import os
import tarfile
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any

import requests

from rootcoz import get_child_logger
from rootcoz.temp_paths import delete_tree, rootcoz_temp_base

logger = get_child_logger(__name__)

# Under process temp root so chat symlink cleanup (honors TMPDIR) can delete it.
EXTRACT_BASE = rootcoz_temp_base()

#: The two directory names this module creates under :data:`EXTRACT_BASE`, shared
#: with the startup reclaim so a rename cannot silently stop matching.
JENKINS_ARTIFACTS_PREFIX = "jenkins-artifacts-"
ARTIFACTS_PREFIX = "artifacts-"

# Maximum ratio of extracted size to compressed size. Typical compression ratios
# for Jenkins artifacts (logs, YAML, JSON) range from 3-8x; 10x provides safe
# headroom while still catching decompression bombs.
EXTRACT_SIZE_MULTIPLIER = 10


class _SizeLimitExceeded(Exception):
    """Raised when archive extraction exceeds size limit."""


def _dir_size_bytes(root_dir: Path) -> int:
    """Sum sizes of regular non-symlink files currently under ``root_dir``."""
    if not root_dir.exists():
        return 0
    total = 0
    for f in root_dir.rglob("*"):
        if not f.is_symlink() and f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _resolve_extract_subdir(parent: Path, filename: str, root_dir: Path) -> Path | None:
    """Return a validated extraction subdirectory strictly inside ``parent`` and ``root_dir``."""
    stripped = _strip_archive_extension(filename)
    if not stripped:
        return None
    extract_subdir = parent / stripped
    if extract_subdir.is_symlink():
        return None
    root_resolved = root_dir.resolve()
    parent_resolved = parent.resolve()
    subdir_resolved = extract_subdir.resolve()
    if (
        not subdir_resolved.is_relative_to(root_resolved)
        or subdir_resolved == root_resolved
        or not subdir_resolved.is_relative_to(parent_resolved)
        or subdir_resolved == parent_resolved
    ):
        return None
    return extract_subdir


def _cleanup_failed_extract_dir(
    extract_dir: Path,
    *,
    existed_before: bool,
    boundary_root: Path | None,
) -> None:
    """Remove a newly created failed extraction directory if strictly inside its boundary."""
    if existed_before or extract_dir.is_symlink():
        return
    resolved_dir = extract_dir.resolve()
    base_resolved = EXTRACT_BASE.resolve()
    # Never delete EXTRACT_BASE or any ancestor of EXTRACT_BASE.
    if base_resolved.is_relative_to(resolved_dir):
        logger.warning(f"Refusing to delete protected directory: {extract_dir}")
        return
    effective_root = boundary_root if boundary_root is not None else base_resolved
    if (
        not resolved_dir.is_relative_to(effective_root)
        or resolved_dir == effective_root
    ):
        logger.warning(
            f"Refusing to delete directory outside boundary {effective_root}: {extract_dir}"
        )
        return
    delete_tree(extract_dir)


def download_artifact(
    session: requests.Session,
    build_url: str,
    relative_path: str,
    max_size_mb: int = 500,
    *,
    max_bytes: int | None = None,
) -> bytes | None:
    """Download a single artifact from Jenkins.

    Args:
        session: Authenticated requests session.
        build_url: Full Jenkins build URL (from API response).
        relative_path: Relative path of the artifact.
        max_size_mb: Maximum allowed artifact size in megabytes.
        max_bytes: Optional stricter byte ceiling (e.g. remaining cumulative budget).

    Returns:
        Raw bytes of the artifact, or None if download fails.
    """
    per_artifact_bytes = max_size_mb * 1024 * 1024
    effective_max_bytes = (
        min(per_artifact_bytes, max_bytes)
        if max_bytes is not None
        else per_artifact_bytes
    )
    if effective_max_bytes <= 0:
        logger.warning(
            f"Skipping artifact '{relative_path}': remaining download size budget exhausted"
        )
        return None

    url = f"{build_url.rstrip('/')}/artifact/{urllib.parse.quote(relative_path, safe='/')}"
    logger.debug(f"Downloading artifact: {url}")

    try:
        response = session.get(url, stream=True, timeout=60)
        try:
            if response.status_code != 200:
                logger.warning(
                    f"Failed to download artifact '{relative_path}': "
                    f"HTTP {response.status_code}"
                )
                return None

            buffer = io.BytesIO()
            downloaded = 0

            for chunk in response.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded > effective_max_bytes:
                    logger.warning(
                        f"Artifact '{relative_path}' exceeded max size "
                        f"({effective_max_bytes / (1024 * 1024):.1f} MB), skipping"
                    )
                    return None
                buffer.write(chunk)

            return buffer.getvalue()
        finally:
            response.close()
    except (requests.RequestException, ConnectionError, OSError) as exc:
        logger.warning(f"Failed to download artifact '{relative_path}': {exc}")
        return None


def store_artifact(
    relative_path: str,
    data: bytes,
    artifacts_dir: Path,
    max_size_mb: int = 500,
    *,
    remaining_bytes: int | None = None,
) -> bool:
    """Store a single artifact to disk, extracting archives if possible.

    Args:
        relative_path: Relative path of the artifact.
        data: Raw bytes of the artifact.
        artifacts_dir: Root directory to store artifacts in.
        max_size_mb: Maximum allowed extracted archive size in megabytes.
        remaining_bytes: Optional remaining cumulative workspace byte budget.

    Returns:
        True if the artifact was stored or extracted, False if skipped.
    """
    # Validate path doesn't escape artifacts_dir
    root = artifacts_dir.resolve()
    dest = artifacts_dir / relative_path
    resolved = dest.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        logger.warning(f"Skipping artifact with unsafe path: {relative_path}")
        return False

    if remaining_bytes is not None and remaining_bytes <= 0:
        logger.warning(
            f"Skipping artifact '{relative_path}': cumulative artifact size budget exhausted"
        )
        return False

    if _is_archive(relative_path):
        extract_subdir = _resolve_extract_subdir(dest.parent, dest.name, artifacts_dir)
        if extract_subdir is None:
            logger.warning(
                f"Skipping artifact with unsafe extraction path: {relative_path}"
            )
            return False
        if (
            validate_and_extract_archive(
                data,
                max_size_mb,
                extract_dir=extract_subdir,
                remaining_bytes=remaining_bytes,
                root_dir=artifacts_dir,
            )
            is not None
        ):
            logger.info(f"Extracted archive artifact '{relative_path}'")
            return True
        logger.warning(f"Could not extract '{relative_path}', storing as raw file")

    if remaining_bytes is not None and len(data) > remaining_bytes:
        logger.warning(
            f"Skipping artifact '{relative_path}': size ({len(data)} bytes) "
            f"exceeds remaining budget ({remaining_bytes} bytes)"
        )
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def _extract_tar(
    fileobj: io.BytesIO, extract_dir: Path, max_extracted_bytes: int
) -> None:
    """Extract a tar archive with security checks.

    Logs warnings for unsafe entries and skips them. On Python 3.12+,
    falls back to manual filtering if data_filter rejects entries.

    Args:
        fileobj: BytesIO containing the tar data.
        extract_dir: Directory to extract into.
        max_extracted_bytes: Maximum allowed total extracted size in bytes.
            Fails closed when ``<= 0``.

    Raises:
        tarfile.TarError: If the data is not a valid tar archive.
        _SizeLimitExceeded: If the estimated extracted size exceeds the limit
            or ``max_extracted_bytes <= 0``.
    """
    if max_extracted_bytes <= 0:
        raise _SizeLimitExceeded("Remaining extraction size budget exhausted")

    with tarfile.open(fileobj=fileobj, mode="r:*") as tar:
        members = tar.getmembers()

        if hasattr(tarfile, "data_filter"):
            total_size = sum(m.size for m in members if m.isreg())
            if total_size > max_extracted_bytes:
                raise _SizeLimitExceeded(
                    f"Estimated extracted size ({total_size / (1024 * 1024):.1f} MB) "
                    f"exceeds limit ({max_extracted_bytes / (1024 * 1024):.1f} MB)"
                )
            try:
                tar.extractall(path=extract_dir, filter="data")
                return
            except (tarfile.FilterError, OSError) as exc:
                logger.warning(
                    f"Tar data_filter rejected entries, falling back to manual filtering: {exc}"
                )

        # Manual member-by-member extraction with filtering
        boundary = str(extract_dir.resolve()) + os.sep
        safe_members = []
        for member in members:
            if member.issym() or member.islnk():
                if member.linkname.startswith("/") or ".." in member.linkname.split(
                    "/"
                ):
                    logger.warning(
                        f"Skipping tar entry with unsafe linkname: "
                        f"{member.name} -> {member.linkname}"
                    )
                    continue
                member_parent = Path(extract_dir / member.name).parent
                link_target = (member_parent / member.linkname).resolve()
                if not str(link_target).startswith(boundary):
                    logger.warning(
                        f"Skipping tar entry with symlink outside extraction dir: "
                        f"{member.name} -> {member.linkname}"
                    )
                    continue
            member_path = Path(extract_dir / member.name).resolve()
            if not str(member_path).startswith(boundary):
                logger.warning(f"Skipping tar entry with unsafe path: {member.name}")
                continue
            if member.name.startswith("/") or ".." in member.name.split("/"):
                logger.warning(
                    f"Skipping tar entry with unsafe path component: {member.name}"
                )
                continue
            safe_members.append(member)

        total_size = sum(m.size for m in safe_members if m.isreg())
        if total_size > max_extracted_bytes:
            raise _SizeLimitExceeded(
                f"Estimated extracted size ({total_size / (1024 * 1024):.1f} MB) "
                f"exceeds limit ({max_extracted_bytes / (1024 * 1024):.1f} MB)"
            )

        tar.extractall(path=extract_dir, members=safe_members, filter="data")


def _extract_zip(
    fileobj: io.BytesIO, extract_dir: Path, max_extracted_bytes: int
) -> None:
    """Extract a zip archive with security checks.

    Logs warnings for unsafe entries and skips them instead of raising.

    Args:
        fileobj: BytesIO containing the zip data.
        extract_dir: Directory to extract into.
        max_extracted_bytes: Maximum allowed total extracted size in bytes.
            Fails closed when ``<= 0``.

    Raises:
        zipfile.BadZipFile: If the data is not a valid zip file.
        _SizeLimitExceeded: If the estimated extracted size exceeds the limit
            or ``max_extracted_bytes <= 0``.
    """
    if max_extracted_bytes <= 0:
        raise _SizeLimitExceeded("Remaining extraction size budget exhausted")

    with zipfile.ZipFile(fileobj) as zf:
        boundary = str(extract_dir.resolve()) + os.sep
        safe_names = []
        for info in zf.infolist():
            if info.filename.startswith("/") or ".." in info.filename.split("/"):
                logger.warning(f"Skipping zip entry with unsafe path: {info.filename}")
                continue
            member_path = Path(extract_dir / info.filename).resolve()
            if not str(member_path).startswith(boundary):
                logger.warning(f"Skipping zip entry with unsafe path: {info.filename}")
                continue
            safe_names.append(info)

        total_size = sum(info.file_size for info in safe_names)
        if total_size > max_extracted_bytes:
            raise _SizeLimitExceeded(
                f"Estimated extracted size ({total_size / (1024 * 1024):.1f} MB) "
                f"exceeds limit ({max_extracted_bytes / (1024 * 1024):.1f} MB)"
            )

        zf.extractall(path=extract_dir, members=safe_names)


def validate_and_extract_archive(
    archive_data: bytes,
    max_size_mb: int = 500,
    extract_dir: Path | None = None,
    *,
    remaining_bytes: int | None = None,
    root_dir: Path | None = None,
) -> Path | None:
    """Validate and extract a tar/tar.gz/tgz or zip archive to a temporary directory.

    Performs size validation, format validation, and path traversal security checks
    before extracting. Returns None on failure instead of raising.

    Args:
        archive_data: Raw bytes of the archive.
        max_size_mb: Maximum allowed size of ``archive_data`` in megabytes.
        extract_dir: Optional directory to extract into. If not provided, a random
            directory under EXTRACT_BASE is created.
        remaining_bytes: Optional remaining cumulative extracted-byte budget.
            When provided and ``<= 0``, fails closed and returns None.
        root_dir: Optional artifacts root directory that ``extract_dir`` must be
            strictly contained within.

    Returns:
        Path to the directory where the archive was extracted, or None on failure.
    """
    if max_size_mb <= 0:
        logger.warning(f"Archive rejected: invalid max_size_mb ({max_size_mb})")
        return None

    size_mb = len(archive_data) / (1024 * 1024)
    if size_mb > max_size_mb:
        logger.warning(
            f"Archive size ({size_mb:.1f} MB) exceeds maximum allowed size ({max_size_mb} MB)"
        )
        return None

    default_max_extracted = max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024
    if remaining_bytes is not None:
        if remaining_bytes <= 0:
            logger.warning(
                "Archive rejected: cumulative extraction size budget exhausted"
            )
            return None
        max_extracted_bytes = min(default_max_extracted, remaining_bytes)
    else:
        max_extracted_bytes = default_max_extracted

    if extract_dir is None:
        extract_dir = EXTRACT_BASE / f"{JENKINS_ARTIFACTS_PREFIX}{uuid.uuid4().hex[:8]}"
        boundary_root: Path | None = EXTRACT_BASE.resolve()
    else:
        if extract_dir.is_symlink():
            logger.warning(f"Refusing to extract archive into symlink: {extract_dir}")
            return None
        boundary_root = root_dir.resolve() if root_dir is not None else None
        if boundary_root is not None:
            resolved_dir = extract_dir.resolve()
            if (
                not resolved_dir.is_relative_to(boundary_root)
                or resolved_dir == boundary_root
            ):
                logger.warning(
                    f"Refusing to extract archive outside root {boundary_root}: {extract_dir}"
                )
                return None

    existed_before = extract_dir.exists()
    size_before = _dir_size_bytes(extract_dir) if existed_before else 0
    extract_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Extracting archive ({size_mb:.1f} MB) to {extract_dir}")

    fileobj = io.BytesIO(archive_data)

    # Try tar first, then zip
    try:
        _extract_tar(fileobj, extract_dir, max_extracted_bytes=max_extracted_bytes)
    except _SizeLimitExceeded as exc:
        _cleanup_failed_extract_dir(
            extract_dir, existed_before=existed_before, boundary_root=boundary_root
        )
        logger.warning(f"Archive rejected: {exc}")
        return None
    except tarfile.TarError, OSError:
        fileobj.seek(0)
        try:
            _extract_zip(fileobj, extract_dir, max_extracted_bytes=max_extracted_bytes)
        except _SizeLimitExceeded as exc:
            _cleanup_failed_extract_dir(
                extract_dir, existed_before=existed_before, boundary_root=boundary_root
            )
            logger.warning(f"Archive rejected: {exc}")
            return None
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
            _cleanup_failed_extract_dir(
                extract_dir, existed_before=existed_before, boundary_root=boundary_root
            )
            logger.warning(f"Invalid archive (not a valid tar or zip file): {exc}")
            return None

    # Post-extraction size check to prevent decompression bombs
    extracted_size = max(0, _dir_size_bytes(extract_dir) - size_before)
    if extracted_size > max_extracted_bytes:
        _cleanup_failed_extract_dir(
            extract_dir, existed_before=existed_before, boundary_root=boundary_root
        )
        logger.warning(
            f"Extracted size ({extracted_size / (1024 * 1024):.1f} MB) exceeds maximum "
            f"allowed ({max_extracted_bytes / (1024 * 1024):.1f} MB). Possible decompression bomb."
        )
        return None

    logger.info(f"Extraction complete: {extract_dir}")
    return extract_dir


def _extract_nested_archives(
    root_dir: Path,
    max_size_mb: int = 500,
    max_depth: int = 5,
    *,
    max_total_bytes: int | None = None,
) -> int:
    """Recursively extract archives found inside extracted directories.

    Walks the directory, extracts any archive files found, deletes the
    archive after successful extraction, and repeats until no more
    archives are found, the cumulative byte budget is exhausted, or
    max_depth is reached.

    Returns:
        Total number of nested archives extracted.
    """
    budget_bytes = (
        max_total_bytes
        if max_total_bytes is not None
        else max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024
    )
    if max_size_mb <= 0 or budget_bytes <= 0:
        logger.warning("Skipping nested archive extraction: non-positive size budget")
        return 0

    total_extracted = 0
    skipped: set[Path] = set()
    max_archive_bytes = max_size_mb * 1024 * 1024

    for depth in range(1, max_depth + 1):
        archives = sorted(
            f
            for f in root_dir.rglob("*")
            if not f.is_symlink()
            and f.is_file()
            and _is_archive(f.name)
            and f not in skipped
        )
        if not archives:
            break
        logger.debug(
            "Nested archive scan depth %d: found %d archive(s)", depth, len(archives)
        )
        extracted_this_depth = 0
        for archive_path in archives:
            relative = archive_path.relative_to(root_dir)
            remaining_bytes = budget_bytes - _dir_size_bytes(root_dir)
            if remaining_bytes <= 0:
                logger.warning(
                    "Cumulative extracted size limit reached in %s (%d MB)",
                    root_dir,
                    budget_bytes // (1024 * 1024),
                )
                return total_extracted

            extract_subdir = _resolve_extract_subdir(
                archive_path.parent, archive_path.name, root_dir
            )
            if extract_subdir is None:
                logger.warning(
                    "Skipping nested archive with unsafe extraction path: %s", relative
                )
                skipped.add(archive_path)
                continue

            try:
                archive_size = archive_path.stat().st_size
                if archive_size > max_archive_bytes:
                    logger.debug(
                        "Skipping nested archive %s: size %.1f MB exceeds limit %d MB",
                        relative,
                        archive_size / (1024 * 1024),
                        max_size_mb,
                    )
                    skipped.add(archive_path)
                    continue
                data = archive_path.read_bytes()
                result = validate_and_extract_archive(
                    data,
                    max_size_mb,
                    extract_dir=extract_subdir,
                    remaining_bytes=remaining_bytes,
                    root_dir=root_dir,
                )
                if result is not None:
                    logger.debug(
                        "Extracted nested archive: %s -> %s", relative, extract_subdir
                    )
                    archive_path.unlink()
                    total_extracted += 1
                    extracted_this_depth += 1
                else:
                    logger.debug("Could not extract nested archive: %s", relative)
                    skipped.add(archive_path)
            except OSError as exc:
                logger.debug("Failed to extract nested archive %s: %s", relative, exc)
                skipped.add(archive_path)

        if extracted_this_depth == 0:
            break

    if total_extracted:
        logger.info("Extracted %d nested archive(s)", total_extracted)
    return total_extracted


def get_artifacts_path(extract_path: Path) -> str:
    """Return the artifacts directory path for the AI to explore with tools."""
    logger.info(f"Build artifacts available at {extract_path}")
    return str(extract_path)


def process_build_artifacts(
    session: requests.Session,
    build_url: str,
    artifact_list: list[dict[str, Any]],
    max_size_mb: int = 500,
) -> tuple[str, Path | None]:
    """Download, store, and analyze all build artifacts in a single pass.

    Downloads each artifact, immediately stores it to disk (extracting
    archives), then builds an artifacts context summary. Enforces a shared
    cumulative disk budget of ``max_size_mb * EXTRACT_SIZE_MULTIPLIER`` MB
    across all artifacts and nested archive extractions.

    Args:
        session: Authenticated requests session.
        build_url: Full Jenkins build URL (from API response).
        artifact_list: List of artifact dicts from Jenkins API.
        max_size_mb: Maximum allowed size per artifact in megabytes.

    Returns:
        Tuple of (context_string, artifacts_dir_or_None).
        The caller must call cleanup_extract_dir(artifacts_dir) when done.
    """
    if not artifact_list:
        logger.debug("No artifacts to process")
        return "", None

    if max_size_mb <= 0:
        logger.warning(f"Invalid max_size_mb ({max_size_mb}), skipping artifacts")
        return "NOTE: No artifacts could be downloaded.", None

    max_total_bytes = max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024
    artifacts_dir = EXTRACT_BASE / f"{ARTIFACTS_PREFIX}{uuid.uuid4().hex[:8]}"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Processing {len(artifact_list)} artifacts to {artifacts_dir}")

    try:
        downloaded = 0
        for artifact in artifact_list:
            relative_path = artifact.get("relativePath", "")
            if not relative_path:
                continue

            # Reject absolute paths and traversal attempts
            if relative_path.startswith("/") or ".." in relative_path.split("/"):
                logger.warning(f"Skipping artifact with unsafe path: {relative_path}")
                continue

            remaining_bytes = max_total_bytes - _dir_size_bytes(artifacts_dir)
            if remaining_bytes <= 0:
                logger.warning(
                    "Cumulative artifact size limit (%d MB) reached in %s, "
                    "skipping remaining artifacts",
                    max_size_mb * EXTRACT_SIZE_MULTIPLIER,
                    artifacts_dir,
                )
                break

            data = download_artifact(
                session,
                build_url,
                relative_path,
                max_size_mb,
                max_bytes=remaining_bytes,
            )
            if data is None:
                continue

            if store_artifact(
                relative_path,
                data,
                artifacts_dir,
                max_size_mb,
                remaining_bytes=remaining_bytes,
            ):
                downloaded += 1

        if downloaded == 0:
            cleanup_extract_dir(artifacts_dir)
            return "NOTE: No artifacts could be downloaded.", None

        logger.info(
            f"Downloaded and stored {downloaded}/{len(artifact_list)} artifacts"
        )

        # Recursively extract any nested archives and delete the archive files
        _extract_nested_archives(
            artifacts_dir, max_size_mb, max_total_bytes=max_total_bytes
        )

        context = get_artifacts_path(artifacts_dir)
        return context, artifacts_dir

    except Exception:
        cleanup_extract_dir(artifacts_dir)
        raise


def _is_archive(filename: str) -> bool:
    """Check if a filename looks like a tar or zip archive."""
    lower = filename.lower()
    return lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip"))


def _strip_archive_extension(filename: str) -> str:
    """Strip archive extension from a single filename for use as directory name.

    Returns an empty string when the derived name is empty, ``.``, ``..``, or
    contains a path separator so callers never construct traversal targets.
    """
    lower = filename.lower()
    stripped = filename
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tar", ".zip"):
        if lower.endswith(ext):
            stripped = filename[: -len(ext)]
            break
    if (
        not stripped
        or stripped in {".", ".."}
        or "/" in stripped
        or "\\" in stripped
        or os.sep in stripped
    ):
        return ""
    return stripped


def cleanup_extract_dir(extract_path: Path) -> None:
    """Remove an extracted archive directory tree.

    Thin wrapper around ``sources.base.cleanup_extract_dir`` for callers that
    historically imported cleanup from this module.
    """
    from rootcoz.sources.base import cleanup_extract_dir as _cleanup

    _cleanup(extract_path, label="extracted archive directory")
