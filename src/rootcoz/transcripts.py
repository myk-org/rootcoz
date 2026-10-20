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

from rootcoz import get_child_logger

logger = get_child_logger(__name__)

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


def write_sidecar_env_bridge(values: dict[str, str | None]) -> None:
    """Persist DB-configured sidecar env overrides for the entrypoint (#347).

    Generalizes the RECORD_TRANSCRIPTS bridge: the entrypoint sources this
    fixed path before starting the sidecar. ``None`` means no DB override —
    nothing is assigned for that var, so the deployment environment
    (compose/Helm) controls the sidecar. An empty string is an explicit
    empty override (#352): the var IS assigned empty, which beats the
    deployment env (e.g. clearing ``CLI_AGENTS`` while the container env
    still sets it). Values are shell-quoted: a value containing spaces
    must survive ``source`` under ``set -e``.

    Best-effort: environments without a writable /data (local dev without a
    container) simply skip the bridge — the deployment env still controls
    the sidecar there.
    """
    try:
        ENV_BRIDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"{var}={shlex.quote(value)}"
            for var, value in sorted(values.items())
            if value is not None
        ]
        ENV_BRIDGE_PATH.write_text(
            "\n".join(lines) + "\n" if lines else "# no DB overrides for the sidecar\n"
        )
    except OSError:
        logger.warning("Unable to write sidecar env bridge at %s", ENV_BRIDGE_PATH)


def write_transcript_env_bridge(enabled: bool | None) -> None:
    """Backward-compatible single-var writer (RECORD_TRANSCRIPTS only).

    Prefer :func:`write_sidecar_env_bridge` — this wrapper must not be used
    alongside other bridged vars, it would drop their assignments.
    """
    write_sidecar_env_bridge(
        {
            "RECORD_TRANSCRIPTS": None
            if enabled is None
            else ("true" if enabled else "false")
        }
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


_DEPLOY_RECORD_TRANSCRIPTS_VAR = "ROOTCOZ_DEPLOY_RECORD_TRANSCRIPTS"


def deployment_record_transcripts() -> bool | None:
    """Pristine deployment RECORD_TRANSCRIPTS, captured by the entrypoint

    The entrypoint exports ``ROOTCOZ_DEPLOY_RECORD_TRANSCRIPTS`` before
    sourcing the DB bridge, so it holds the deployment value even after a
    DB override pollutes ``RECORD_TRANSCRIPTS`` for this boot. Empty means
    the deployment left it unset (sidecar default: off). None means an
    older entrypoint never captured it.
    """
    raw = os.environ.get(_DEPLOY_RECORD_TRANSCRIPTS_VAR)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def sync_transcript_recording(enabled: bool | None) -> bool:
    """Live-apply the recording state through the sidecar runtime toggle.

    Requires pi-sidecar >= 4.14.0 (``POST /transcripts/recording``).
    ``enabled=None`` drops the DB override: when the entrypoint captured the
    pristine deployment value (``ROOTCOZ_DEPLOY_RECORD_TRANSCRIPTS``), it is
    pushed explicitly — the sidecar's boot env may still carry the bridged
    old DB value, and a bare override reset would fall back to that until a
    restart. Without the capture (older entrypoint) the override is simply
    dropped so the boot env governs again.
    Best-effort: an older sidecar (no endpoint) or an unreachable sidecar
    leaves the env-governed state in place — the env bridge still applies
    the DB value after a restart.
    """
    from rootcoz.ai_client import get_sidecar_client

    try:
        client = get_sidecar_client()
        if enabled is None:
            deploy = deployment_record_transcripts()
            if deploy is not None:
                await client.set_recording(deploy)
            else:
                await client.reset_recording()
        else:
            await client.set_recording(enabled)
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort sync; older sidecars stay env-governed
        logger.warning(
            "Sidecar recording sync failed (recording stays env-governed; "
            "pi-sidecar >= 4.14.0 required): %s",
            exc,
        )
        return False


async def live_recording_enabled() -> bool | None:
    """Effective recording state from the sidecar, or None if unreachable."""
    from rootcoz.ai_client import get_sidecar_client

    try:
        state = await get_sidecar_client().get_recording()
        return bool(state.get("enabled"))
    except Exception as exc:  # noqa: BLE001 - best-effort read; None means unreachable
        logger.debug("Sidecar recording state unavailable: %s", exc)
        return None
