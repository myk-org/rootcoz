"""Jenkins artifacts extraction and context building for test failure analysis."""

import io
import os
import shutil
import tarfile
import urllib.parse
import uuid
import zipfile
from pathlib import Path

import requests
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

EXTRACT_BASE = Path("/tmp/jenkins-insight")

# Maximum ratio of extracted size to compressed size. Typical compression ratios
# for Jenkins artifacts (logs, YAML, JSON) range from 3-8x; 10x provides safe
# headroom while still catching decompression bombs.
EXTRACT_SIZE_MULTIPLIER = 10


class _SizeLimitExceeded(Exception):
    """Raised when archive extraction exceeds size limit."""


def download_artifact(
    session: requests.Session,
    build_url: str,
    relative_path: str,
    max_size_mb: int = 500,
) -> bytes | None:
    """Download a single artifact from Jenkins.

    Args:
        session: Authenticated requests session.
        build_url: Full Jenkins build URL (from API response).
        relative_path: Relative path of the artifact.
        max_size_mb: Maximum allowed artifact size in megabytes.

    Returns:
        Raw bytes of the artifact, or None if download fails.
    """
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
            max_bytes = max_size_mb * 1024 * 1024

            for chunk in response.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    logger.warning(
                        f"Artifact '{relative_path}' exceeded max size "
                        f"({max_size_mb} MB), skipping"
                    )
                    return None
                buffer.write(chunk)

            return buffer.getvalue()
        finally:
            response.close()
    except Exception as exc:
        logger.warning(f"Failed to download artifact '{relative_path}': {exc}")
        return None


def store_artifact(
    relative_path: str,
    data: bytes,
    artifacts_dir: Path,
    max_size_mb: int = 500,
) -> None:
    """Store a single artifact to disk, extracting archives if possible.

    Args:
        relative_path: Relative path of the artifact.
        data: Raw bytes of the artifact.
        artifacts_dir: Root directory to store artifacts in.
        max_size_mb: Maximum allowed extracted archive size in megabytes.
    """
    # Validate path doesn't escape artifacts_dir
    root = artifacts_dir.resolve()
    resolved = (artifacts_dir / relative_path).resolve()
    if not str(resolved).startswith(str(root) + os.sep):
        logger.warning(f"Skipping artifact with unsafe path: {relative_path}")
        return

    if _is_archive(relative_path):
        extract_subdir = artifacts_dir / _strip_archive_extension(relative_path)
        extract_resolved = extract_subdir.resolve()
        if not str(extract_resolved).startswith(str(root) + os.sep):
            logger.warning(
                f"Skipping artifact with unsafe extraction path: {relative_path}"
            )
            return
        if (
            validate_and_extract_archive(data, max_size_mb, extract_dir=extract_subdir)
            is not None
        ):
            logger.info(f"Extracted archive artifact '{relative_path}'")
            return
        logger.warning(f"Could not extract '{relative_path}', storing as raw file")

    dest = artifacts_dir / relative_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _extract_tar(
    fileobj: io.BytesIO, extract_dir: Path, max_extracted_bytes: int = 0
) -> None:
    """Extract a tar archive with security checks.

    Logs warnings for unsafe entries and skips them. On Python 3.12+,
    falls back to manual filtering if data_filter rejects entries.

    Args:
        fileobj: BytesIO containing the tar data.
        extract_dir: Directory to extract into.
        max_extracted_bytes: Maximum allowed total extracted size in bytes.
            If 0, no pre-extraction size check is performed.

    Raises:
        tarfile.TarError: If the data is not a valid tar archive.
        _SizeLimitExceeded: If the estimated extracted size exceeds the limit.
    """
    tar = tarfile.open(fileobj=fileobj, mode="r:*")
    with tar:
        members = tar.getmembers()

        if hasattr(tarfile, "data_filter"):
            if max_extracted_bytes > 0:
                total_size = sum(m.size for m in members if m.isreg())
                if total_size > max_extracted_bytes:
                    raise _SizeLimitExceeded(
                        f"Estimated extracted size ({total_size / (1024 * 1024):.1f} MB) "
                        f"exceeds limit ({max_extracted_bytes / (1024 * 1024):.0f} MB)"
                    )
            try:
                tar.extractall(path=extract_dir, filter="data")
                return
            except Exception as exc:
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

        if max_extracted_bytes > 0:
            total_size = sum(m.size for m in safe_members if m.isreg())
            if total_size > max_extracted_bytes:
                raise _SizeLimitExceeded(
                    f"Estimated extracted size ({total_size / (1024 * 1024):.1f} MB) "
                    f"exceeds limit ({max_extracted_bytes / (1024 * 1024):.0f} MB)"
                )

        tar.extractall(path=extract_dir, members=safe_members)


def _extract_zip(
    fileobj: io.BytesIO, extract_dir: Path, max_extracted_bytes: int = 0
) -> None:
    """Extract a zip archive with security checks.

    Logs warnings for unsafe entries and skips them instead of raising.

    Args:
        fileobj: BytesIO containing the zip data.
        extract_dir: Directory to extract into.
        max_extracted_bytes: Maximum allowed total extracted size in bytes.
            If 0, no pre-extraction size check is performed.

    Raises:
        zipfile.BadZipFile: If the data is not a valid zip file.
        _SizeLimitExceeded: If the estimated extracted size exceeds the limit.
    """
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

        if max_extracted_bytes > 0:
            total_size = sum(info.file_size for info in safe_names)
            if total_size > max_extracted_bytes:
                raise _SizeLimitExceeded(
                    f"Estimated extracted size ({total_size / (1024 * 1024):.1f} MB) "
                    f"exceeds limit ({max_extracted_bytes / (1024 * 1024):.0f} MB)"
                )

        zf.extractall(path=extract_dir, members=safe_names)


def validate_and_extract_archive(
    archive_data: bytes, max_size_mb: int = 500, extract_dir: Path | None = None
) -> Path | None:
    """Validate and extract a tar/tar.gz/tgz or zip archive to a temporary directory.

    Performs size validation, format validation, and path traversal security checks
    before extracting. Returns None on failure instead of raising.

    Args:
        archive_data: Raw bytes of the archive.
        max_size_mb: Maximum allowed size of ``archive_data`` in megabytes.
        extract_dir: Optional directory to extract into. If not provided, a random
            directory under EXTRACT_BASE is created.

    Returns:
        Path to the directory where the archive was extracted, or None on failure.
    """
    size_mb = len(archive_data) / (1024 * 1024)
    if size_mb > max_size_mb:
        logger.warning(
            f"Archive size ({size_mb:.1f} MB) exceeds maximum allowed size ({max_size_mb} MB)"
        )
        return None

    if extract_dir is None:
        extract_dir = EXTRACT_BASE / f"jenkins-artifacts-{uuid.uuid4().hex[:8]}"
    extract_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Extracting archive ({size_mb:.1f} MB) to {extract_dir}")

    fileobj = io.BytesIO(archive_data)

    # Try tar first, then zip
    max_extracted_bytes = max_size_mb * EXTRACT_SIZE_MULTIPLIER * 1024 * 1024
    try:
        _extract_tar(fileobj, extract_dir, max_extracted_bytes=max_extracted_bytes)
    except _SizeLimitExceeded as exc:
        shutil.rmtree(extract_dir, ignore_errors=True)
        logger.warning(f"Archive rejected: {exc}")
        return None
    except Exception:
        fileobj.seek(0)
        try:
            _extract_zip(fileobj, extract_dir, max_extracted_bytes=max_extracted_bytes)
        except _SizeLimitExceeded as exc:
            shutil.rmtree(extract_dir, ignore_errors=True)
            logger.warning(f"Archive rejected: {exc}")
            return None
        except Exception as exc:
            shutil.rmtree(extract_dir, ignore_errors=True)
            logger.warning(f"Invalid archive (not a valid tar or zip file): {exc}")
            return None

    # Post-extraction size check to prevent decompression bombs
    extracted_size = sum(
        f.stat().st_size for f in extract_dir.rglob("*") if f.is_file()
    )
    if extracted_size > max_extracted_bytes:
        shutil.rmtree(extract_dir)
        logger.warning(
            f"Extracted size ({extracted_size / (1024 * 1024):.1f} MB) exceeds maximum "
            f"allowed ({max_size_mb * EXTRACT_SIZE_MULTIPLIER} MB). Possible decompression bomb."
        )
        return None

    logger.info(f"Extraction complete: {extract_dir}")
    return extract_dir


def _discover_files(extract_path: Path) -> list[tuple[str, int]]:
    """Discover files in extracted archive.

    Returns:
        List of (relative_path, size_in_bytes) tuples.
    """
    all_files: list[tuple[str, int]] = []

    for file_path in extract_path.rglob("*"):
        if not file_path.is_file():
            continue
        relative = str(file_path.relative_to(extract_path))
        try:
            size = file_path.stat().st_size
        except OSError:
            size = 0
        all_files.append((relative, size))

    return all_files


def build_artifacts_context(extract_path: Path) -> str:
    """Walk an extracted archive directory and build a structured artifacts summary."""
    logger.info(f"Building artifacts context from {extract_path}")

    all_files = _discover_files(extract_path)
    total_size = sum(size for _, size in all_files)
    total_size_mb = total_size / (1024 * 1024)

    logger.info(
        f"Archive file discovery: {len(all_files)} total files ({total_size_mb:.1f} MB)"
    )

    # Build the structured context
    sections: list[str] = [
        "=== BUILD ARTIFACTS CONTEXT ===",
        f"Artifacts directory: {extract_path}",
        "Also accessible at: build-artifacts/ (or current working directory if no test repo)",
        f"Contains {len(all_files)} files ({total_size_mb:.1f} MB total).",
        "",
        "IMPORTANT: You MUST explore the build-artifacts/ directory (or the absolute path above).",
        "The index below lists paths and sizes. Read files under the artifacts directory for full content.",
        "",
    ]

    # Aggregate per-directory stats in a single pass
    dir_stats: dict[str, tuple[int, int]] = {}  # dir -> (count, total_size)
    root_files: list[tuple[str, int]] = []
    for path, size in all_files:
        parent = str(Path(path).parent)
        if parent == ".":
            root_files.append((path, size))
        else:
            count, total = dir_stats.get(parent, (0, 0))
            dir_stats[parent] = (count + 1, total + size)

    if dir_stats or root_files:
        sections.append("--- Directory Structure ---")
        for d in sorted(dir_stats):
            file_count, dir_size = dir_stats[d]
            if dir_size >= 1024 * 1024:
                sections.append(
                    f"  {d}/ ({file_count} files, {dir_size / (1024 * 1024):.1f} MB)"
                )
            elif dir_size >= 1024:
                sections.append(
                    f"  {d}/ ({file_count} files, {dir_size / 1024:.1f} KB)"
                )
            else:
                sections.append(f"  {d}/ ({file_count} files, {dir_size} B)")
        if root_files:
            for path, size in root_files:
                if size >= 1024:
                    sections.append(f"  {path} ({size / 1024:.1f} KB)")
                else:
                    sections.append(f"  {path} ({size} B)")
        sections.append("")

    sections.append(f"--- Full artifacts available at: {extract_path} ---")
    sections.append(
        "Explore the build-artifacts/ directory (or current working directory) for complete evidence before classifying."
    )

    context = "\n".join(sections)
    logger.info(f"Artifacts context built: {len(context)} chars")
    return context


def process_build_artifacts(
    session: requests.Session,
    build_url: str,
    artifact_list: list[dict],
    max_size_mb: int = 500,
) -> tuple[str, Path | None]:
    """Download, store, and analyze all build artifacts in a single pass.

    Downloads each artifact, immediately stores it to disk (extracting
    archives), then builds an artifacts context summary.

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

    artifacts_dir = EXTRACT_BASE / f"artifacts-{uuid.uuid4().hex[:8]}"
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

            data = download_artifact(session, build_url, relative_path, max_size_mb)
            if data is None:
                continue

            store_artifact(relative_path, data, artifacts_dir, max_size_mb)
            downloaded += 1

        if downloaded == 0:
            cleanup_extract_dir(artifacts_dir)
            return "NOTE: No artifacts could be downloaded.", None

        logger.info(
            f"Downloaded and stored {downloaded}/{len(artifact_list)} artifacts"
        )
        context = build_artifacts_context(artifacts_dir)
        return context, artifacts_dir

    except Exception:
        cleanup_extract_dir(artifacts_dir)
        raise


def _is_archive(filename: str) -> bool:
    """Check if a filename looks like a tar or zip archive."""
    lower = filename.lower()
    return lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip"))


def _strip_archive_extension(filename: str) -> str:
    """Strip archive extension from filename for use as directory name."""
    lower = filename.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tar", ".zip"):
        if lower.endswith(ext):
            return filename[: -len(ext)]
    return filename


def cleanup_extract_dir(extract_path: Path) -> None:
    """Remove an extracted archive directory tree.

    Logs the cleanup operation. Errors during removal are logged as warnings
    and swallowed so that cleanup failures never crash the pipeline.

    Args:
        extract_path: Path to the extracted directory to remove.
    """
    logger.info(f"Cleaning up extracted archive directory: {extract_path}")
    try:
        shutil.rmtree(extract_path)
        logger.info(f"Cleanup complete: {extract_path}")
    except OSError as exc:
        logger.warning(f"Failed to clean up {extract_path}: {exc}")
