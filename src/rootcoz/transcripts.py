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

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_logger.logger import get_logger

logger = get_logger(name=__name__)

# Bridge file: the sidecar reads RECORD_TRANSCRIPTS from its process env at
# container start. rootcoz persists the DB-configured value to a FIXED path
# (never derived from TRANSCRIPTS_DIR — a custom directory must not move the
# bridge away from the path the entrypoint sources). The bridge carries ONLY
# the DB override: when the admin has not set the value in Server Settings,
# no assignment is written, so the deployment environment (compose/Helm)
# controls the sidecar.

# Same env var the sidecar reads; the default must live under /data so
# transcripts survive container replacement.
TRANSCRIPTS_DIR = Path(
    os.getenv("PI_SIDECAR_TRANSCRIPTS_DIR", "/data/pi-sidecar/transcripts")
)

# Fixed bridge location (env-overridable for tests).
ENV_BRIDGE_PATH = Path(os.getenv("PI_SIDECAR_ENV_BRIDGE", "/data/pi-sidecar/env"))

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


def write_transcript_env_bridge(enabled: bool | None) -> None:
    """Persist the DB-configured record_transcripts value for the entrypoint.

    ``enabled=None`` means no DB override exists — nothing is assigned, so the
    deployment environment (compose/Helm) stays in control. Values are
    shell-quoted: a directory or value containing spaces must survive
    ``source`` under ``set -e``.

    Best-effort: environments without a writable /data (local dev without a
    container) simply skip the bridge — the deployment env still controls the
    sidecar there.
    """
    try:
        ENV_BRIDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if enabled is None:
            # Comment-only file: sourcing it assigns nothing.
            content = "# no DB override for RECORD_TRANSCRIPTS\n"
        else:
            content = (
                f"RECORD_TRANSCRIPTS={shlex.quote('true' if enabled else 'false')}\n"
            )
        ENV_BRIDGE_PATH.write_text(content)
    except OSError:
        logger.warning("Unable to write transcript env bridge at %s", ENV_BRIDGE_PATH)


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
        turns = _count_turns(entry)
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


def _count_turns(path: Path) -> int:
    """Count ``turn`` records. Unreadable files report 0.

    pi-sidecar writes a ``session_header`` record before the ``turn`` records;
    headers are not turns. Unparseable lines still count — they are content
    written for this session, and a placeholder replacing an oversized record
    keeps its ``type`` so it parses normally.
    """
    turns = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    turns += 1
                    continue
                if not isinstance(record, dict) or record.get("type") == "turn":
                    turns += 1
    except OSError:
        return 0
    return turns


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
