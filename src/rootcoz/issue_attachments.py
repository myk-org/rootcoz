"""Validated evidence and fd-relative storage. Authorization belongs to callers."""

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
import stat
import struct
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote
from uuid import UUID

from rootcoz import storage
from rootcoz.models import IssueAttachmentUpload
from rootcoz.temp_paths import (
    CleanupOutcome,
    DeleteResult,
    cleanup_off_loop,
    sanitize_path_part,
)

logger = logging.getLogger(__name__)
MAX_FILES = 3
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_ENVELOPE_BYTES = 15 * 1024 * 1024
MAX_RUN_BYTES = 200 * 1024 * 1024
MAX_OWNER_BYTES = 500 * 1024 * 1024
TEXT_SUFFIXES = {".log", ".txt", ".json", ".xml", ".csv", ".yaml", ".yml", ".md"}
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class IssueAttachmentRejected(ValueError):
    """A complete request was refused with safe named reasons."""


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    """Decoded evidence; never log or put these bytes in a model prompt."""

    display_name: str
    media_type: str
    data: bytes

    @property
    def sha256(self) -> str:
        """Digest for ordered consent manifests, never audit details."""
        return hashlib.sha256(self.data).hexdigest()


def _jpeg_structure(data: bytes) -> bool:
    offset = 2
    frame = False
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return False
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if length < 2 or offset + 2 + length > len(data):
            return False
        if marker in {0xC0, 0xC1, 0xC2}:
            frame = (
                length >= 8
                and int.from_bytes(data[offset + 5 : offset + 7], "big") > 0
                and int.from_bytes(data[offset + 7 : offset + 9], "big") > 0
            )
        if marker == 0xDA:
            return frame and length >= 6 and offset + 2 + length < len(data) - 2
        offset += length + 2
    return False


def _media(data: bytes) -> str:
    if (
        data.startswith(
            (
                b"%PDF",
                b"MZ",
                b"PK\x03\x04",
                b"PK\x05\x06",
                b"\x1f\x8b",
                b"7z\xbc\xaf\x27\x1c",
                b"\xd0\xcf\x11\xe0",
                b"\x7fELF",
                b"\xfe\xed\xfa",
                b"\xcf\xfa\xed\xfe",
                b"\xce\xfa\xed\xfe",
                b"\xca\xfe\xba\xbe",
            )
        )
        or data[257:262] == b"ustar"
    ):
        raise ValueError("container or executable content")
    if (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        and len(data) >= 45
        and data[12:16] == b"IHDR"
        and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    ):
        return "image/png"
    if (
        data.startswith(b"\xff\xd8\xff")
        and data.endswith(b"\xff\xd9")
        and _jpeg_structure(data)
    ):
        return "image/jpeg"
    if (
        data.startswith((b"GIF87a", b"GIF89a"))
        and len(data) >= 24
        and struct.unpack("<H", data[6:8])[0] > 0
        and struct.unpack("<H", data[8:10])[0] > 0
        and b"," in data[13:-1]
        and data.endswith(b";")
    ):
        return "image/gif"
    if (
        len(data) >= 20
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP"
        and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
        and struct.unpack("<I", data[4:8])[0] == len(data) - 8
    ):
        return "image/webp"
    text = data.decode("utf-8", errors="strict")
    if "\x00" in text or "\x7f" in text:
        raise ValueError("NUL or DEL in text")
    return "text/plain"


def validate_attachments(
    files: list[IssueAttachmentUpload],
) -> list[ValidatedAttachment]:
    """Refuse the whole request and collect every offending filename."""
    errors = []
    result = []
    total = 0
    if len(files) > MAX_FILES:
        errors.append("At most 3 attachments are allowed")
    for item in files:
        safe = item.filename.encode("utf-8", "backslashreplace").decode("utf-8")
        try:
            name = item.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if (
                not name
                or name in {".", ".."}
                or name.startswith(".")
                or len(name) > 120
                or any(unicodedata.category(c) in {"Cc", "Cs"} for c in item.filename)
            ):
                raise ValueError("invalid display name; maximum 120 characters")
            name = sanitize_path_part(name)
            if len(item.data_b64) > 4 * ((MAX_FILE_BYTES + 2) // 3):
                raise ValueError("exceeds 5 MiB per-file limit")
            try:
                data = base64.b64decode(item.data_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise binascii.Error("invalid base64 encoding") from exc
            total += len(data)
            if not data:
                raise ValueError("attachment is empty")
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("exceeds 5 MiB per-file limit")
            media = _media(data)
            suffix = Path(name).suffix.lower()
            if suffix not in TEXT_SUFFIXES and suffix not in IMAGE_TYPES:
                raise ValueError("unsupported file extension")
            if not (
                media == "text/plain"
                and suffix in TEXT_SUFFIXES
                or IMAGE_TYPES.get(suffix) == media
            ):
                raise ValueError("file content does not match extension")
            result.append(ValidatedAttachment(name, media, data))
        except binascii.Error:
            errors.append(f"{safe}: invalid base64 encoding")
        except UnicodeError:
            errors.append(f"{safe}: text attachment is not valid UTF-8")
        except ValueError as exc:
            errors.append(f"{safe}: {exc}")
    if total > MAX_TOTAL_BYTES:
        errors.append("Attachments exceed 10 MiB total")
    if errors:
        raise IssueAttachmentRejected("; ".join(errors))
    return result


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Noncanonical attachment scope")
    return value


@contextmanager
def _directory(row: dict[str, Any], *, create: bool = False) -> Iterator[int]:
    # Open from /, including every data-directory ancestor, without following links.
    base = storage.current_db_path().parent.absolute()
    owner = row["owner_id"]
    if type(owner) is not int or owner <= 0:
        raise ValueError("Invalid attachment owner")
    parts = [
        *base.parts[1:],
        "issue-attachments",
        "jobs" if row["job_id"] else "users",
        _uuid(row["job_id"]) if row["job_id"] else str(owner),
    ]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            if create and index >= len(base.parts) - 1:
                try:
                    os.mkdir(part, 0o770, dir_fd=fd)
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    os.fchmod(child, 0o770)
                except FileExistsError:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
            else:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def open_attachment(row: dict[str, Any]) -> BinaryIO:
    """Open once and return the same checked regular-file fd for streaming."""
    with _directory(row) as directory:
        fd = os.open(
            _uuid(row["id"]),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != row["size_bytes"]:
            raise OSError("Attachment missing or invalid")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _write(row: dict[str, Any], item: ValidatedAttachment) -> None:
    with _directory(row, create=True) as directory:
        fd = os.open(
            _uuid(row["id"]),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o660,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o660)
            stream.write(item.data)
            stream.flush()
            os.fsync(stream.fileno())


async def store(rows: list[dict[str, Any]], items: list[ValidatedAttachment]) -> None:
    """Write only persisted reservations; retain retry metadata on failure."""
    if len(rows) != len(items):
        raise ValueError("Reservation count mismatch")
    for row, item in zip(rows, items, strict=True):
        saved = await storage.get_issue_attachment(row["id"])
        if (
            saved is None
            or saved["state"] != "reserved"
            or any(saved[k] != row[k] for k in row)
            or row["size_bytes"] != len(item.data)
        ):
            raise ValueError("Missing or mismatched reservation")
    written = []
    try:
        for row, item in zip(rows, items, strict=True):
            await asyncio.to_thread(_write, row, item)
            written.append(row)
        await storage.mark_issue_attachments_stored([row["id"] for row in rows])
        logger.info("Attachment blobs stored: count=%d", len(rows))
    except Exception:
        await storage.mark_issue_attachments_stored(
            [row["id"] for row in rows], cleanup_pending=True
        )
        await delete_stored(written)
        raise


def _unlink(row: dict[str, Any]) -> None:
    try:
        with _directory(row) as directory:
            os.unlink(_uuid(row["id"]), dir_fd=directory)
    except FileNotFoundError:
        pass


async def delete_stored(rows: list[dict[str, Any]]) -> None:
    """Rollback exact ids; failed removals remain explicitly retryable."""
    await storage.mark_issue_attachments_stored(
        [r["id"] for r in rows], cleanup_pending=True
    )
    for row in rows:
        try:
            outcome = await cleanup_off_loop(row["id"], partial(_unlink, row))
            if outcome == CleanupOutcome.CLEAN:
                await storage.delete_issue_attachment_rows([row["id"]])
        except OSError as exc:
            logger.warning("Attachment cleanup pending: errno=%s", exc.errno)
    logger.info("Attachment cleanup attempted: count=%d", len(rows))


def content_disposition(filename: str, attachment_id: str) -> str:
    """Use an ASCII UUID fallback and RFC5987 display name."""
    return f"attachment; filename=\"{_uuid(attachment_id)}\"; filename*=UTF-8''{quote(filename, safe='')}"


def attachments_section(
    rows: list[dict[str, Any]], base_url: str, *, tracker: str = "github"
) -> str:
    """Format hosted links; caller validates the configured public base URL."""
    lines = [
        "\n\nAttachments",
        "Rootcoz login required. Links expire when their run or uploader account is deleted.",
    ]
    for row in rows:
        url = f"{base_url.rstrip('/')}/api/issue-attachments/{_uuid(row['id'])}"
        if tracker == "github":
            name = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", row["display_name"])
            lines.append(f"- [{name}]({url})")
        else:
            name = re.sub(r"[\[\]|{}\r\n]", "_", row["display_name"])
            lines.append(f"* [{name}|{url}]")
    return "\n".join(lines)


@contextmanager
def _scope_parent_fd(kind: str, *, create: bool = False) -> Iterator[int]:
    """Open ``<data_dir>/issue-attachments/<kind>`` from ``/`` without following symlinks."""
    if kind not in {"jobs", "users"}:
        raise ValueError("Invalid attachment scope kind")
    base = storage.current_db_path().parent.absolute()
    parts = [*base.parts[1:], "issue-attachments", kind]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            if create and index >= len(base.parts) - 1:
                try:
                    os.mkdir(part, 0o770, dir_fd=fd)
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    os.fchmod(child, 0o770)
                except FileExistsError:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
            else:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _delete_scope_child_dir(kind: str, child_name: str) -> DeleteResult:
    """Remove ``<kind>/<child_name>`` via fd-relative ``O_NOFOLLOW`` operations."""
    try:
        with _scope_parent_fd(kind) as parent_fd:
            try:
                st = os.stat(child_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return DeleteResult(0, 0)
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(child_name, dir_fd=parent_fd)
                return DeleteResult(1, st.st_size)
            dir_fd = os.open(
                child_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            entries_removed = 0
            bytes_removed = 0
            failed = 0
            try:
                with os.scandir(os.dup(dir_fd)) as it:
                    names = [entry.name for entry in it]
                for name in names:
                    try:
                        cst = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                        if stat.S_ISDIR(cst.st_mode):
                            failed += 1
                            continue
                        os.unlink(name, dir_fd=dir_fd)
                        entries_removed += 1
                        bytes_removed += cst.st_size
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        logger.warning(
                            "Attachment child unlink failed: errno=%s", exc.errno
                        )
                        failed += 1
            finally:
                os.close(dir_fd)
            if failed:
                return DeleteResult(entries_removed, bytes_removed, failed=failed)
            try:
                os.rmdir(child_name, dir_fd=parent_fd)
            except FileNotFoundError:
                return DeleteResult(entries_removed, bytes_removed)
            except OSError as exc:
                logger.warning("Attachment scope rmdir failed: errno=%s", exc.errno)
                return DeleteResult(entries_removed, bytes_removed, failed=1)
            return DeleteResult(entries_removed + 1, bytes_removed)
    except FileNotFoundError:
        return DeleteResult(0, 0)
    except OSError as exc:
        logger.warning("Attachment scope open failed: errno=%s", exc.errno)
        return DeleteResult(0, 0, failed=1)


def delete_job_attachments_dir(job_id: str) -> DeleteResult:
    """Remove ``issue-attachments/jobs/<job_id>`` safely without following symlinks."""
    return _delete_scope_child_dir("jobs", _uuid(job_id))


def delete_owner_attachments_dir(owner_id: int) -> DeleteResult:
    """Remove ``issue-attachments/users/<owner_id>`` safely without following symlinks."""
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError("Invalid attachment owner")
    return _delete_scope_child_dir("users", str(owner_id))


def _write_owner_marker_sync(owner_id: int) -> bool:
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError("Invalid attachment owner")
    marker_name = f".{owner_id}.rootcoz-deleting"
    with _scope_parent_fd("users", create=True) as users_fd:
        try:
            fd = os.open(
                marker_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o660,
                dir_fd=users_fd,
            )
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o660)
            stream.flush()
            os.fsync(stream.fileno())
        return True


async def write_owner_delete_marker(owner_id: int) -> bool:
    """Create ``.<owner_id>.rootcoz-deleting``; return True if newly created here."""
    return await asyncio.to_thread(_write_owner_marker_sync, owner_id)


def _remove_owner_marker_sync(owner_id: int) -> None:
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError("Invalid attachment owner")
    marker_name = f".{owner_id}.rootcoz-deleting"
    try:
        with _scope_parent_fd("users") as users_fd:
            os.unlink(marker_name, dir_fd=users_fd)
    except FileNotFoundError:
        return


async def remove_owner_delete_marker(owner_id: int) -> None:
    """Remove ``.<owner_id>.rootcoz-deleting`` once blobs and run-less rows are gone."""
    await asyncio.to_thread(_remove_owner_marker_sync, owner_id)


def _write_job_marker_sync(job_id: str, *, only_if_dir_exists: bool = False) -> bool:
    canonical = _uuid(job_id)
    marker_name = f".job-{canonical}.rootcoz-deleting"
    try:
        with _scope_parent_fd("jobs", create=not only_if_dir_exists) as jobs_fd:
            if only_if_dir_exists:
                try:
                    st = os.stat(canonical, dir_fd=jobs_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                if not stat.S_ISDIR(st.st_mode):
                    return False
            try:
                fd = os.open(
                    marker_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o660,
                    dir_fd=jobs_fd,
                )
            except FileExistsError:
                return False
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o660)
                stream.flush()
                os.fsync(stream.fileno())
            return True
    except FileNotFoundError:
        return False


async def write_job_delete_marker(
    job_id: str, *, only_if_dir_exists: bool = False
) -> bool:
    """Create ``.job-<job_id>.rootcoz-deleting``; return True if newly created here."""
    return await asyncio.to_thread(
        partial(_write_job_marker_sync, job_id, only_if_dir_exists=only_if_dir_exists)
    )


def _remove_job_marker_sync(job_id: str) -> None:
    marker_name = f".job-{_uuid(job_id)}.rootcoz-deleting"
    try:
        with _scope_parent_fd("jobs") as jobs_fd:
            os.unlink(marker_name, dir_fd=jobs_fd)
    except FileNotFoundError:
        return


async def remove_job_delete_marker(job_id: str) -> None:
    """Remove ``.job-<job_id>.rootcoz-deleting`` once run blobs and rows are gone."""
    await asyncio.to_thread(_remove_job_marker_sync, job_id)


def list_scope_entries(kind: str) -> list[tuple[str, os.stat_result]] | None:
    """Return ``[(name, lstat)]`` under ``issue-attachments/<kind>``, or ``None`` on OSError."""
    try:
        with (
            _scope_parent_fd(kind) as parent_fd,
            os.scandir(os.dup(parent_fd)) as it,
        ):
            result: list[tuple[str, os.stat_result]] = []
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                result.append((entry.name, st))
            return sorted(result, key=lambda item: item[0])
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Cannot list attachment scope %s: errno=%s", kind, exc.errno)
        return None


async def unlink_attachment_row(row: dict[str, Any]) -> bool:
    """Unlink a single attachment blob off-loop and delete its row only if clean."""
    try:
        outcome = await cleanup_off_loop(row["id"], partial(_unlink, row))
    except OSError as exc:
        logger.warning("Pending attachment unlink failed: errno=%s", exc.errno)
        return False
    if outcome is not CleanupOutcome.CLEAN:
        return False
    await storage.delete_issue_attachment_rows([row["id"]])
    return True
