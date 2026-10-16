"""pi-sidecar session transcript access (#329).

The sidecar (>= 4.12.0) records one ``{session_id}.jsonl`` per session when
``RECORD_TRANSCRIPTS`` is enabled at container start. Files live under
``PI_SIDECAR_TRANSCRIPTS_DIR`` (rootcoz default: ``/data/pi-sidecar/transcripts``
so they survive container/pod replacement).

Correlation contract (#328): the transcript filename is the sidecar
``session_id``. ``ai_token_usage.session_id`` + ``job_id``/``call_type`` give
the business context; ``chat_messages.session_id`` covers chat sessions.

This module is read-only — transcript files are never modified or deleted
through rootcoz.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_logger.logger import get_logger

logger = get_logger(name=__name__)

# Bridge file: the sidecar reads RECORD_TRANSCRIPTS from its process env at
# container start. rootcoz persists the DB-configured value to
# ``<transcripts parent>/env``; the container entrypoint sources it before
# starting the sidecar, so a Server Settings change applies to the sidecar
# after a restart.

# Same env var the sidecar reads; the default must live under /data so
# transcripts survive container replacement.
TRANSCRIPTS_DIR = Path(
    os.getenv("PI_SIDECAR_TRANSCRIPTS_DIR", "/data/pi-sidecar/transcripts")
)

# Sidecar session ids are UUIDs (node:crypto randomUUID). Anchored with
# \Z (not $) — Python's `$` matches before a trailing newline, which would
# let `"<uuid>\n"` through — and character-restricted, so a match can never
# contain a path separator: transcript paths built from validated ids are
# traversal-safe.
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


def valid_session_id(session_id: str) -> bool:
    """True when the value is a well-formed sidecar session id (UUID)."""
    return bool(SESSION_ID_RE.match(session_id))


def write_transcript_env_bridge(enabled: bool) -> None:
    """Persist the record_transcripts value for the container entrypoint.

    Best-effort: environments without a writable /data (local dev without a
    container) simply skip the bridge — the deployment env still controls the
    sidecar there.
    """
    try:
        bridge = TRANSCRIPTS_DIR.parent / "env"
        TRANSCRIPTS_DIR.parent.mkdir(parents=True, exist_ok=True)
        bridge.write_text(
            f"RECORD_TRANSCRIPTS={'true' if enabled else 'false'}\n"
            f"PI_SIDECAR_TRANSCRIPTS_DIR={TRANSCRIPTS_DIR}\n"
        )
    except OSError:
        logger.warning(
            "Unable to write transcript env bridge at %s",
            TRANSCRIPTS_DIR.parent / "env",
        )


@dataclass(frozen=True)
class TranscriptInfo:
    """Metadata for one transcript file (content is never loaded here)."""

    session_id: str
    size_bytes: int
    modified_at: float
    turns: int


def list_transcripts() -> list[TranscriptInfo]:
    """List transcript files in the recordings directory, newest first.

    Unreadable or non-matching entries are skipped — a partially written or
    foreign file must not break the listing.
    """
    if not TRANSCRIPTS_DIR.is_dir():
        return []
    infos: list[TranscriptInfo] = []
    try:
        entries = list(TRANSCRIPTS_DIR.iterdir())
    except OSError:
        logger.warning("Unable to list transcripts dir %s", TRANSCRIPTS_DIR)
        return []
    for entry in entries:
        if not entry.name.endswith(".jsonl"):
            continue
        session_id = entry.name[: -len(".jsonl")]
        if not valid_session_id(session_id):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        turns = _count_lines(entry)
        infos.append(
            TranscriptInfo(
                session_id=session_id,
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
                turns=turns,
            )
        )
    infos.sort(key=lambda info: info.modified_at, reverse=True)
    return infos


def _count_lines(path: Path) -> int:
    """Count JSONL lines (turns). Unreadable files report 0."""
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def transcript_path(session_id: str) -> Path | None:
    """Validated transcript file path, or None for malformed/traversal ids."""
    if not valid_session_id(session_id):
        return None
    return TRANSCRIPTS_DIR / f"{session_id}.jsonl"


def transcript_summary(info: TranscriptInfo, context: dict[str, Any]) -> dict[str, Any]:
    """Serialize a transcript listing entry with its joined business context."""
    return {
        "session_id": info.session_id,
        "size_bytes": info.size_bytes,
        "modified_at": info.modified_at,
        "turns": info.turns,
        "context": context,
    }
