"""Tests for the compressed ``result_json`` column (#409).

The column carries the whole payload, so these tests are end-to-end at the
storage layer: what the writer stores, what the backfill rewrites, and every SQL
site that used to read the payload with JSON functions — which cannot read a frame.
"""

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from compression import zstd
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from rootcoz import storage

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # little-endian frame magic (RFC 8878)

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def _db(temp_db_path: Path):
    """A migrated database the storage functions use."""
    with patch.object(storage, "DB_PATH", temp_db_path):
        asyncio.run(storage.init_db())
        yield temp_db_path


@pytest.fixture
def _storage(_db: Path):
    """Patch ``DB_PATH`` for every call in the test."""
    with patch.object(storage, "DB_PATH", _db):
        yield _db


def _payload(
    failure_count: int = 414,
    *,
    job_name: str = "big-job",
    summary: str = "many failures",
    distinct_analyses: int = 2,
    analysis_size: int = 300,
) -> dict:
    """A report-shaped payload: many failures, few distinct analysis texts."""
    analyses = [
        {
            "classification": "CODE ISSUE",
            "pattern": "NEW",
            "analysis": f"analysis {index} " + "detail " * analysis_size,
        }
        for index in range(distinct_analyses)
    ]
    return {
        "job_name": job_name,
        "build_number": 42,
        "build_id": "4242",
        "display_name": "Human Name",
        "summary": summary,
        "request_params": {"submitted_by": "yuval", "build_id": "4242"},
        "passed_count": 10,
        "failed_count": failure_count,
        "failures": [
            {
                "id": f"failure-{index}",
                "test_name": f"test_{index}",
                "error": f"AssertionError: boom {index} " + "x" * 120,
                "error_signature": f"sig-{index}",
                "analysis": analyses[index % distinct_analyses],
            }
            for index in range(failure_count)
        ],
        "child_job_analyses": [
            {
                "id": "child-1",
                "job_name": "child-job",
                "build_number": 7,
                "failures": [
                    {
                        "id": "child-failure-1",
                        "test_name": "test_child",
                        "error": "child crash",
                        "analysis": analyses[0],
                    }
                ],
                "failed_children": [],
            }
        ],
        "token_usage": {"total": {"input": 1, "output": 2}},
    }


async def _stored(db_path: Path, job_id: str):
    """Return the raw stored row for *job_id*."""
    async with storage._connect_db() as db:
        return await (
            await db.execute(
                "SELECT typeof(result_json) AS kind, length(result_json) AS size,"
                " result_json, job_name, build_number, build_id, submitted_by,"
                " summary, display_name, failure_ids, denorm_version"
                " FROM results WHERE job_id = ?",
                (job_id,),
            )
        ).fetchone()


async def _insert_legacy(job_id: str, payload: dict) -> str:
    """Insert a row the way a pre-compression build wrote it (inline JSON, no columns)."""
    text = json.dumps(payload)
    async with storage._connect_db() as db:
        await db.execute(
            "INSERT INTO results (job_id, status, result_json) VALUES (?, ?, ?)",
            (job_id, "completed", text),
        )
        await db.commit()
    return text


async def _insert_damaged(job_id: str) -> None:
    """Insert a row whose stored frame is truncated: unreadable, not absent."""
    frame = zstd.compress(json.dumps(_payload(failure_count=20)).encode())
    async with storage._connect_db() as db:
        await db.execute(
            "INSERT INTO results (job_id, status, result_json) VALUES (?, ?, ?)",
            (job_id, "running", frame[: len(frame) // 2]),
        )
        await db.commit()


async def _insert_newer_encoding(job_id: str, blob: bytes) -> None:
    """Insert a row as a build with a result_json codec this one lacks wrote it."""
    async with storage._connect_db() as db:
        await db.execute(
            "INSERT INTO results (job_id, status, result_json, job_name,"
            " build_number, submitted_by, summary, display_name, failure_ids,"
            " denorm_version) VALUES (?, 'running', ?, 'newer-job', 7, 'dana',"
            " 'newer summary', 'Newer Name', 'failure-x', ?)",
            (job_id, blob, storage._RESULTS_DENORM_VERSION),
        )
        await db.commit()


@pytest.fixture
def storage_logs(caplog: pytest.LogCaptureFixture) -> Any:
    """Attach caplog to the storage logger (simple_logger sets propagate=False)."""
    storage.logger.setLevel(logging.DEBUG)
    storage.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        storage.logger.removeHandler(caplog.handler)


# --- the writer -------------------------------------------------------------


async def test_full_size_roundtrip_is_content_identical(_storage) -> None:
    """414 failures / 2 analyses: identical payload after a compressed write."""
    payload = _payload()
    await storage.save_result("job-roundtrip", "http://ci", "completed", payload)

    row = await _stored(_storage, "job-roundtrip")
    assert row["kind"] == "blob", "a payload this size must be stored compressed"

    # Exact round-trip through the frame -> decompress -> json.loads path. The
    # read-side enrichment get_result adds (token usage) is asserted below.
    assert (
        storage.parse_result_json(row["result_json"], job_id="job-roundtrip") == payload
    )

    stored = await storage.get_result("job-roundtrip")
    assert stored["result"]["summary"] == payload["summary"]
    assert stored["result"]["request_params"] == payload["request_params"]
    assert stored["result"]["child_job_analyses"] == payload["child_job_analyses"]
    failures = stored["result"]["failures"]
    assert len(failures) == 414
    # No truncation and no deduplication of the per-signature analysis text.
    assert len({failure["analysis"]["analysis"] for failure in failures}) == 2
    assert (
        failures[0]["analysis"]["analysis"]
        == payload["failures"][0]["analysis"]["analysis"]
    )


async def test_save_does_not_block_the_event_loop(_storage, monkeypatch) -> None:
    """Both result writers encode in a thread: the loop serves other work meanwhile."""
    real_compress = zstd.compress
    ticks = 0
    ticks_per_encode: list[int] = []

    def slow_compress(data: bytes, level: int = 0) -> bytes:
        before = ticks
        time.sleep(0.1)  # stands in for a report-sized encode
        ticks_per_encode.append(ticks - before)
        return real_compress(data, level)

    monkeypatch.setattr(storage.zstd, "compress", slow_compress)
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    payload = _payload()
    await storage.save_result("job-save-loop", "http://ci", "completed", payload)
    await storage.update_status("job-save-loop", "completed", payload)
    stop = True
    await task

    # An encode on the loop would let the ticker advance zero times while it ran.
    assert len(ticks_per_encode) == 2, "both writers must encode exactly once"
    assert min(ticks_per_encode) > 10, "a save blocked the event loop while encoding"
    assert (await _stored(_storage, "job-save-loop"))["kind"] == "blob"


async def test_save_encodes_the_payload_once(_storage, monkeypatch) -> None:
    """One save compresses once and stores one frame, not a frame of a frame."""
    calls = 0
    real_compress = zstd.compress

    def counting_compress(data: bytes, level: int = 0) -> bytes:
        nonlocal calls
        calls += 1
        return real_compress(data, level)

    monkeypatch.setattr(storage.zstd, "compress", counting_compress)
    payload = _payload(failure_count=40)
    await storage.save_result("job-once", "http://ci", "completed", payload)
    assert calls == 1, "the save encoded a payload it then discarded"

    async def stored_frame() -> bytes:
        row = await _stored(_storage, "job-once")
        assert row["kind"] == "blob"
        return row["result_json"]

    frame = await stored_frame()
    # One hop to the JSON, and exactly one frame magic in the column.
    assert frame.count(_ZSTD_MAGIC) == 1
    assert json.loads(zstd.decompress(frame)) == payload
    assert storage.parse_result_json(frame, job_id="job-once") == payload

    # A re-save of the same row (read-modify-write) stays single-framed too.
    await storage.save_result("job-once", "http://ci", "completed", payload)
    again = await stored_frame()
    assert again.count(_ZSTD_MAGIC) == 1
    assert storage.parse_result_json(again, job_id="job-once") == payload


async def test_size_gate_never_grows_a_row(_storage, monkeypatch) -> None:
    """Small payloads stay TEXT, and a frame that would be bigger is never used."""
    await storage.save_result(
        "job-small", "", "completed", {"job_name": "tiny", "summary": "ok"}
    )
    assert (await _stored(_storage, "job-small"))["kind"] == "text"

    await storage.save_result("job-compressible", "", "completed", _payload())
    assert (await _stored(_storage, "job-compressible"))["kind"] == "blob"

    raw = json.dumps(_payload(failure_count=1))
    assert len(storage._encode_result_text(raw)) <= len(raw)

    # Even when the codec returns a larger frame, the row keeps the text.
    monkeypatch.setattr(
        storage.zstd, "compress", lambda data, level=0: b"\x00" * (len(data) + 64)
    )
    assert storage._encode_result_text(raw) == raw
    await storage.save_result("job-no-gain", "", "completed", {"raw": raw})
    row = await _stored(_storage, "job-no-gain")
    assert row["kind"] == "text"
    assert row["size"] == len(json.dumps({"raw": raw}))


async def test_legacy_text_row_reads_and_patch_upgrades_it(_storage) -> None:
    """A row written by today's writer keeps working, and a patch upgrades it lazily."""
    payload = _payload(failure_count=3)
    await _insert_legacy("job-legacy", payload)

    row = await _stored(_storage, "job-legacy")
    assert row["kind"] == "text"
    assert storage.parse_result_json(row["result_json"]) == payload

    await storage.patch_result_json(
        "job-legacy", lambda data: data.update({"summary": "patched"})
    )
    row = await _stored(_storage, "job-legacy")
    patched = storage.parse_result_json(row["result_json"])
    assert patched["summary"] == "patched"
    assert patched["failures"] == payload["failures"]
    assert (await storage.get_result("job-legacy"))["result"]["summary"] == "patched"


# --- reads degrade instead of raising (corrupt frames) ----------------------


async def test_unreadable_frames_degrade_to_none(_storage) -> None:
    """A truncated frame and a frame of non-UTF-8 bytes are a read miss, not an error.

    ``parse_result_json`` names the cause with a typed error; every read path
    goes through the lenient wrapper, so nothing raises at the caller.
    """
    frame = zstd.compress(json.dumps(_payload(failure_count=20)).encode())
    bad_values = (
        frame[: len(frame) // 2],  # truncated frame
        zstd.compress(b"\xff\xfe\x00\x00not utf-8"),  # decodes to non-UTF-8 bytes
        b"not a frame",
    )
    for bad in bad_values:
        assert storage.parse_result_json_lenient(bad, job_id="job-bad") is None
    # Our own magic that will not inflate is damage...
    with pytest.raises(storage.DamagedResultEncoding, match="truncated or corrupt"):
        storage.parse_result_json(bad_values[0], job_id="job-bad")
    # ...not the missing codec a newer encoding gets, so the two never collapse.
    with pytest.raises(storage.UnsupportedResultEncoding, match="not UTF-8 text"):
        storage.parse_result_json(b"\xff\xfe\x00not text", job_id="job-bad")
    # Bytes that are not a frame keep the codec's degrade-to-None: inflated to
    # non-UTF-8, or text that is not JSON.
    for not_a_frame in (bad_values[1], bad_values[2]):
        assert storage.parse_result_json(not_a_frame, job_id="job-bad") is None

    async with storage._connect_db() as db:
        await db.execute(
            "INSERT INTO results (job_id, status, result_json) VALUES (?, ?, ?)",
            ("job-bad", "completed", b"not a frame"),
        )
        await db.commit()

    # No exception reaches the caller: the payload reads as empty, the row stays.
    assert (await storage.get_result("job-bad"))["result"] is None
    assert (await _stored(_storage, "job-bad"))["result_json"] == b"not a frame"

    # And a payload that decodes to nothing is still not overwritten by a patch
    # (that is the malformed-JSON path, which may be a bug rather than damage).
    await storage.patch_result_json("job-bad", lambda data: data.update({"x": 1}))
    assert (await _stored(_storage, "job-bad"))["result_json"] == b"not a frame"


async def test_writes_replace_a_damaged_row(_storage, storage_logs) -> None:
    """A damaged frame never blocks its own job's writes.

    Each writer reads the stored payload to merge into it. Unreadable reads as
    "no previous payload": the new payload lands, and one warning per write names
    the job so the damage stays reportable after the bytes are gone.
    """
    for job_id in ("damaged-save", "damaged-status", "damaged-patch"):
        await _insert_damaged(job_id)

    payload = _payload(failure_count=20)
    await storage.save_result("damaged-save", "http://ci", "completed", payload)
    saved = storage.parse_result_json(
        (await _stored(_storage, "damaged-save"))["result_json"]
    )
    assert saved == payload, "save_result must store the new payload, not nothing"

    await storage.update_status("damaged-status", "completed", payload)
    assert (await storage.get_result("damaged-status"))["result"]["summary"] == (
        payload["summary"]
    )

    await storage.patch_result_json(
        "damaged-patch", lambda data: data.update({"summary": "patched"})
    )
    patched = await storage.get_result("damaged-patch")
    assert patched["result"]["summary"] == "patched", "the patch was discarded"

    for job_id in ("damaged-save", "damaged-status", "damaged-patch"):
        assert [
            record.getMessage()
            for record in storage_logs.records
            if record.levelname == "WARNING" and job_id in record.getMessage()
        ], f"no warning named {job_id}"
    assert "previous result_json is damaged" in storage_logs.text
    assert "is replaced by this write" in storage_logs.text
    assert "is replaced by this patch" in storage_logs.text


async def test_older_build_never_overwrites_a_newer_builds_payload(
    _storage, storage_logs
) -> None:
    """A rolled-back build must not write over a payload a newer build stored.

    ``UnsupportedResultEncoding`` is an *intact* report: a build with a result_json
    codec this one lacks wrote it. Every writer reads before it writes, so every
    writer has to leave that row byte-identical -- a save, a status update and a
    patch alike -- and every writer has to tell its caller the write did not
    happen, otherwise a refused update is reported as an applied one.
    """
    blob = b"\xff\xfe\x00a payload from a newer build"
    job_ids = ("newer-save", "newer-status", "newer-patch")
    for job_id in job_ids:
        await _insert_newer_encoding(job_id, blob)

    payload = _payload(failure_count=20)
    # Each writer raises the refusal, naming the reason, instead of returning.
    with pytest.raises(
        storage.UnsupportedResultEncoding, match="newer result_json encoding"
    ):
        await storage.save_result("newer-save", "http://ci", "completed", payload)
    with pytest.raises(
        storage.UnsupportedResultEncoding, match="newer result_json encoding"
    ):
        await storage.update_status("newer-status", "completed", payload)
    with pytest.raises(
        storage.UnsupportedResultEncoding, match="newer result_json encoding"
    ):
        await storage.patch_result_json(
            "newer-patch", lambda data: data.update({"summary": "patched"})
        )

    for job_id in job_ids:
        row = await _stored(_storage, job_id)
        assert row["result_json"] == blob, f"{job_id} wrote over a newer payload"
        # Not even the columns derived from that payload: it stays preview-only.
        assert (
            row["job_name"],
            row["summary"],
            row["submitted_by"],
            row["failure_ids"],
            row["denorm_version"],
        ) == (
            "newer-job",
            "newer summary",
            "dana",
            "failure-x",
            storage._RESULTS_DENORM_VERSION,
        )
        assert any(
            record.levelname == "ERROR" and job_id in record.getMessage()
            for record in storage_logs.records
        ), f"no error named {job_id}"
    # A status update is a write too, so it rolled back instead of half-applying.
    assert (await storage.get_result("newer-status"))["status"] == "running"
    assert "the write was refused and nothing was written over it" in (
        storage_logs.text
    )
    # And the refusal reaches a caller that translates it, without touching it.
    await storage.fail_job_without_result_write(
        "newer-status", "write refused: newer result_json encoding"
    )
    failed = await _stored(_storage, "newer-status")
    assert failed["result_json"] == blob
    status = (await storage.get_result("newer-status"))["status"]
    assert status == "failed"

    # The same bytes are what a build with the codec reads back, untouched.
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE results SET result_json = ? WHERE job_id = 'newer-save'",
            (json.dumps(payload),),
        )
        await db.commit()
    await storage.save_result("newer-save", "http://ci", "completed", payload)
    assert (
        storage.parse_result_json(
            (await _stored(_storage, "newer-save"))["result_json"]
        )
        == payload
    )


async def test_a_refused_save_result_still_logs_error(_storage, storage_logs) -> None:
    """The refusal level was not weakened for writes that did fail.

    The abort path logs the refusal at WARNING because the abort completes
    without the payload rewrite; a save has nothing to fall back on, so its
    refusal stays an error.
    """
    await _insert_newer_encoding("refused-save", b"\xff\xfe\x00newer payload")

    with pytest.raises(storage.UnsupportedResultEncoding):
        await storage.save_result(
            "refused-save", "http://ci", "completed", _payload(failure_count=2)
        )

    levels = [
        record.levelname
        for record in storage_logs.records
        if "refused-save" in record.getMessage() and record.levelno >= logging.WARNING
    ]
    assert levels == ["ERROR"], [
        record.getMessage()
        for record in storage_logs.records
        if "refused-save" in record.getMessage()
    ]


# --- the backfill -----------------------------------------------------------


async def test_backfill_is_byte_identical(_storage, storage_logs) -> None:
    """The frame decompresses to exactly the bytes the old build stored."""
    text = await _insert_legacy("job-bytes", _payload(failure_count=30))
    await storage.backfill_results_denorm()

    stats = await storage.backfill_result_json_compression()
    assert stats["rows"] == 1
    assert "is no longer the database it started on" not in storage_logs.text

    row = await _stored(_storage, "job-bytes")
    assert row["kind"] == "blob"
    assert zstd.decompress(row["result_json"]) == text.encode()


async def test_backfill_shrinks_the_column(_storage, storage_logs) -> None:
    """The acceptance factor measured locally: the column is a fraction of itself."""
    for index in range(20):
        await _insert_legacy(f"job-compress-{index}", _payload(failure_count=40))
    await storage.backfill_results_denorm()
    async with storage._connect_db() as db:
        before = (
            await (
                await db.execute(
                    "SELECT SUM(length(CAST(result_json AS BLOB))) FROM results"
                )
            ).fetchone()
        )[0]

    stats = await storage.backfill_result_json_compression()
    assert stats["rows"] == 20
    async with storage._connect_db() as db:
        after = (
            await (
                await db.execute(
                    "SELECT SUM(length(CAST(result_json AS BLOB))) FROM results"
                )
            ).fetchone()
        )[0]
    assert after == stats["bytes_after"] < stats["bytes_before"] == before
    assert after <= before / 5  # measured ~9x on the dev DB
    assert "is no longer the database it started on" not in storage_logs.text

    # Idempotent and resumable by construction: a second pass has nothing to do.
    assert (await storage.backfill_result_json_compression())["rows"] == 0


async def test_compression_does_not_overwrite_a_concurrent_patch(
    _storage, storage_logs
) -> None:
    """The UPDATE is a compare-and-swap on the text it read, not just its type."""
    await _insert_legacy("job-race", _payload(failure_count=40))
    await storage.backfill_results_denorm()
    newer = {"job_name": "newer", "request_params": {}, "failures": []}
    compress = storage._compress_result_rows

    def race(rows: list, **kwargs: Any) -> list:
        # Another writer replaces the payload after this batch was read.
        with sqlite3.connect(_storage) as conn:
            conn.execute(
                "UPDATE results SET result_json = ? WHERE job_id = 'job-race'",
                (json.dumps(newer),),
            )
        return compress(rows, **kwargs)

    with patch.object(storage, "_compress_result_rows", race):
        stats = await storage.backfill_result_json_compression()

    assert stats["rows"] == 0
    assert "is no longer the database it started on" not in storage_logs.text
    row = await _stored(_storage, "job-race")
    assert row["kind"] == "text"
    assert json.loads(row["result_json"]) == newer


async def test_backfill_does_not_block_the_event_loop(_storage, storage_logs) -> None:
    """Encode runs in a thread, so a ticker keeps firing between batches (#402)."""
    for index in range(120):
        await _insert_legacy(f"job-loop-{index}", _payload(failure_count=60))
    await storage.backfill_results_denorm()

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    started = time.monotonic()
    stats = await storage.backfill_result_json_compression(batch_size=10)
    elapsed = time.monotonic() - started
    stop = True
    await task

    assert stats["rows"] == 120
    assert "is no longer the database it started on" not in storage_logs.text
    assert elapsed > 0.01, "the run must be long enough for the ticker to matter"
    # One tick per batch at minimum; a blocking encode would allow at most one.
    assert ticks > stats["rows"] // 10


# --- a database that stops being ours mid-sweep (#435) ---------------------

_STOPPED = "is no longer the database it started on"
_EMPTY_STATS = {"rows": 0, "bytes_before": 0, "bytes_after": 0}


def _foreign_database(path: Path) -> bytes:
    """Bytes of a valid SQLite database that has no ``results`` table."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
    finally:
        conn.close()  # a clean close: no -journal/-wal sibling left behind
    return path.read_bytes()


@pytest.mark.parametrize("damage", ["unlink", "replace"])
async def test_a_sweep_stops_when_its_database_is_damaged_between_batches(
    _storage, storage_logs, tmp_path: Path, damage: str
) -> None:
    """The reported frame: a database that goes away mid-pass stops the pass.

    ``no such table: results`` came from a batch connection that re-created the
    file a teardown had just unlinked. Damaged between the read of batch two and
    its write, the pass must stop, keep the batch it already committed, and say so
    once -- and it must not write into a file that is not ours.
    """
    db_path = _storage
    await _insert_legacy("job-stop-1", _payload(failure_count=20))
    await _insert_legacy("job-stop-2", _payload(failure_count=20))
    await storage.backfill_results_denorm()
    foreign_bytes = _foreign_database(tmp_path / "foreign.db")

    compress = storage._compress_result_rows
    calls = 0

    def damage_on_the_second_batch(rows: list, **kwargs: Any) -> list:
        # Page one is already committed, page two already read: the slot a teardown
        # lands in.
        nonlocal calls
        calls += 1
        if calls == 2:
            if damage == "unlink":
                db_path.unlink()
            else:
                db_path.write_bytes(foreign_bytes)
        return compress(rows, **kwargs)

    with patch.object(storage, "_compress_result_rows", damage_on_the_second_batch):
        stats = await storage.backfill_result_json_compression(batch_size=1)

    assert calls == 2, "the pass must have reached the second batch"
    assert stats["rows"] == 1, "the committed batch must be kept"
    stopped = [r for r in storage_logs.records if _STOPPED in r.getMessage()]
    assert len(stopped) == 1, [r.getMessage() for r in storage_logs.records]
    assert str(db_path) in stopped[0].getMessage()
    if damage == "unlink":
        assert not db_path.exists(), "the sweep re-created the database that was gone"
    else:
        assert db_path.read_bytes() == foreign_bytes, (
            "the sweep wrote to a foreign file"
        )
    # The done line is the INFO form: no file to report on, nothing raised out.
    assert "no file report" in storage_logs.text
    assert [r.getMessage() for r in storage_logs.records if r.exc_info] == []
    assert [
        r.getMessage() for r in storage_logs.records if r.levelno >= logging.ERROR
    ] == []


async def test_a_sweep_stops_when_its_path_becomes_another_rootcoz_database(
    _storage, storage_logs, tmp_path: Path
) -> None:
    """A schema match is not ownership: a swapped-in rootcoz database is not ours.

    The bytes baseline of the pass describes the file it started on, so a
    replacement -- even one that looks exactly like ours -- must stop the pass
    instead of being swept as old.
    """
    db_path = _storage
    await _insert_legacy("job-swapped-1", _payload(failure_count=20))
    await _insert_legacy("job-swapped-2", _payload(failure_count=20))
    await storage.backfill_results_denorm()
    async with storage._connect_db() as db:  # a self-contained copy of a rootcoz db
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement = tmp_path / "replacement.db"
    replacement_bytes = db_path.read_bytes()
    replacement.write_bytes(replacement_bytes)

    compress = storage._compress_result_rows
    calls = 0

    def swap_on_the_second_batch(rows: list, **kwargs: Any) -> list:
        nonlocal calls
        calls += 1
        if calls == 2:  # a different inode, same schema: stat is the only tell
            replacement.replace(db_path)
        return compress(rows, **kwargs)

    with patch.object(storage, "_compress_result_rows", swap_on_the_second_batch):
        stats = await storage.backfill_result_json_compression(batch_size=1)

    assert calls == 2, "the pass must have reached the second batch"
    assert stats["rows"] == 1, "the committed batch must be kept"
    stopped = [r for r in storage_logs.records if _STOPPED in r.getMessage()]
    assert len(stopped) == 1, [r.getMessage() for r in storage_logs.records]
    assert db_path.read_bytes() == replacement_bytes, (
        "the sweep wrote into the database that replaced it"
    )


async def test_a_sweep_refuses_a_database_swapped_while_it_opens(
    _storage, storage_logs, tmp_path: Path
) -> None:
    """A swap that lands while a connection opens is refused, not committed to.

    A ``stat`` taken only before the await cannot see a swap that lands during it,
    and the pass would then read and commit to the replacement. What the
    connection actually opened is the file that has to be verified.
    """
    db_path = _storage
    await _insert_legacy("job-raced-1", _payload(failure_count=20))
    await storage.backfill_results_denorm()
    async with storage._connect_db() as db:  # a self-contained copy of a rootcoz db
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement = tmp_path / "replacement.db"
    replacement_bytes = db_path.read_bytes()
    replacement.write_bytes(replacement_bytes)

    real_connect = storage.aiosqlite.connect
    calls = 0

    async def swap_before_the_write_open(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 3:  # the batch's write open: lands inside the await
            replacement.replace(db_path)
        return await real_connect(*args, **kwargs)

    with patch.object(storage.aiosqlite, "connect", new=swap_before_the_write_open):
        stats = await storage.backfill_result_json_compression()

    assert calls == 3, "the swap must have landed before the write open"
    assert stats["rows"] == 0, "the pass committed a batch into the replacement"
    stopped = [r for r in storage_logs.records if _STOPPED in r.getMessage()]
    assert len(stopped) == 1, [r.getMessage() for r in storage_logs.records]
    assert db_path.read_bytes() == replacement_bytes, (
        "the sweep wrote into the database that replaced it"
    )


async def test_a_sweep_does_not_pin_a_database_it_did_not_open(
    _storage, storage_logs, tmp_path: Path
) -> None:
    """The first connection pins the file it opened, not whatever sits at the path.

    The identity is read on both sides of the open, so a swap landing inside the
    await -- after the descriptor was taken but before that stat -- is a mismatch
    instead of becoming the pinned identity. Pinned to the replacement, every
    later batch would accept it and commit into it.
    """
    db_path = _storage
    await _insert_legacy("job-raced-0", _payload(failure_count=20))
    await storage.backfill_results_denorm()
    async with storage._connect_db() as db:  # a self-contained copy of a rootcoz db
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement = tmp_path / "replacement.db"
    replacement_bytes = db_path.read_bytes()
    replacement.write_bytes(replacement_bytes)

    real_connect = storage.aiosqlite.connect
    calls = 0

    async def swap_after_the_open(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        db = await real_connect(*args, **kwargs)
        if calls == 1:  # the check open: the swap lands once its descriptor exists
            replacement.replace(db_path)
        return db

    with patch.object(storage.aiosqlite, "connect", new=swap_after_the_open):
        stats = await storage.backfill_result_json_compression()

    assert calls == 1, "the pass must have stopped at the first connection"
    assert stats["rows"] == 0, "the pass committed a batch into the replacement"
    stopped = [r for r in storage_logs.records if _STOPPED in r.getMessage()]
    assert len(stopped) == 1, [r.getMessage() for r in storage_logs.records]
    assert db_path.read_bytes() == replacement_bytes, (
        "the sweep wrote into the database that replaced it"
    )


async def test_a_lock_error_on_the_check_is_not_a_vanished_database(_storage) -> None:
    """A transient lock or I/O fault fails the pass loudly, it does not stop it."""
    await _insert_legacy("job-locked", _payload(failure_count=20))
    await storage.backfill_results_denorm()

    with (
        patch.object(
            storage,
            "_has_results_table",
            side_effect=sqlite3.OperationalError("database is locked"),
        ),
        pytest.raises(sqlite3.OperationalError, match="database is locked"),
    ):
        await storage.backfill_result_json_compression()


async def test_a_foreign_results_table_is_not_our_database(tmp_path: Path) -> None:
    """Our column names are not our schema: a short ``results`` configures nothing.

    The two columns the compression query names are not enough either -- the
    denormalized ones are what makes this our table.
    """
    foreign = tmp_path / "foreign-results.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE results (job_id TEXT, result_json TEXT)")
    conn.execute("INSERT INTO results VALUES ('job-foreign', '{}')")
    conn.commit()
    conn.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    with patch.object(storage, "DB_PATH", foreign):
        assert await storage.backfill_results_denorm() == 0
        assert await storage.backfill_result_json_compression() == _EMPTY_STATS

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


async def test_a_sweep_never_creates_a_database_that_is_gone(tmp_path: Path) -> None:
    """Neither pass may manufacture the database it was told to sweep."""
    gone = tmp_path / "gone.db"
    corrupt = tmp_path / "corrupt.db"
    garbage = b"this is not a sqlite database" * 8
    corrupt.write_bytes(garbage)

    for path in (gone, corrupt):
        with patch.object(storage, "DB_PATH", path):
            assert await storage.backfill_results_denorm() == 0
            assert await storage.backfill_result_json_compression() == _EMPTY_STATS

    assert not gone.exists(), "a sweep created the database it was missing"
    assert corrupt.read_bytes() == garbage, "a sweep wrote into a corrupt file"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["corrupt.db"], (
        "a sweep left -wal/-shm siblings of a file it does not own"
    )


async def test_a_sweep_leaves_a_file_that_is_not_a_rootcoz_database_alone(
    tmp_path: Path,
) -> None:
    """A database-shaped file without ``results`` is neither configured nor written."""
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    rollback = tmp_path / "rollback.db"
    conn = sqlite3.connect(rollback)
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.execute("INSERT INTO other VALUES (1)")
    conn.commit()
    conn.close()
    wal = tmp_path / "wal.db"
    conn = sqlite3.connect(wal)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.execute("INSERT INTO other VALUES (1)")
    conn.commit()
    conn.close()
    # Bytes *and* sidecar set: the WAL pragma used to turn an empty file into 4096
    # bytes, and the migration table appeared in the foreign ones.
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    for path in (empty, rollback, wal):
        with patch.object(storage, "DB_PATH", path):
            assert await storage.backfill_results_denorm() == 0
            assert await storage.backfill_result_json_compression() == _EMPTY_STATS

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


# --- the denormalized columns ----------------------------------------------


async def test_denorm_backfill_repairs_a_late_old_build_row(
    _storage, storage_logs
) -> None:
    """A row an old build writes after the sweep is repaired, then compressed.

    The sweep is per-row version gated, not claimed once: otherwise every row a
    pre-compression binary writes during a mixed-version window keeps empty
    columns forever and the compression predicate skips it.
    """
    payload = _payload(failure_count=20)
    await _insert_legacy("job-late", payload)
    row = await _stored(_storage, "job-late")
    assert row["submitted_by"] == row["summary"] == row["failure_ids"] == ""

    # This boot's sweep was already stamped applied by init_db.
    async with storage._connect_db() as db:
        assert await storage._migration_applied(db, storage._RESULTS_DENORM_KEY)
    await storage.backfill_results_denorm()

    row = await _stored(_storage, "job-late")
    assert (row["submitted_by"], row["summary"], row["display_name"]) == (
        "yuval",
        "many failures",
        "Human Name",
    )
    assert "failure-0" in row["failure_ids"]
    assert "child-failure-1" in row["failure_ids"]
    assert row["denorm_version"] == storage._RESULTS_DENORM_VERSION

    # Only now is it eligible for compression.
    assert (await storage.backfill_result_json_compression())["rows"] == 1
    row = await _stored(_storage, "job-late")
    assert row["kind"] == "blob"
    assert storage.parse_result_json(row["result_json"]) == payload
    assert "is no longer the database it started on" not in storage_logs.text


async def test_denorm_sweep_does_not_overwrite_a_concurrent_patch(_storage) -> None:
    """The sweep's UPDATE is a compare-and-swap on the payload it derived from.

    The derive runs off the loop, so a patch can land inside that window. An
    unconditional rowid-only update would then write the stale ``failure_ids``
    back and stamp the row current, and the new failure would never be found by
    identifier again.
    """
    # Small enough that the patch writes TEXT, so the sweep can re-read it.
    await _insert_legacy(
        "job-denorm-race",
        {
            "job_name": "race",
            "summary": "one failure",
            "failures": [{"id": "failure-0", "test_name": "test_0"}],
        },
    )
    derive = storage._denorm_row_params
    deriving = threading.Event()
    release = threading.Event()

    def delayed(rows: list) -> list:
        deriving.set()
        assert release.wait(10), "the test never released the derive"
        return derive(rows)

    with patch.object(storage, "_denorm_row_params", delayed):
        sweep = asyncio.create_task(storage.backfill_results_denorm())
        assert await asyncio.to_thread(deriving.wait, 10), "derive never started"
        await storage.patch_result_json(
            "job-denorm-race",
            lambda data: data["failures"].append(
                {"id": "failure-raced", "test_name": "test_raced", "error": "boom"}
            ),
        )
        release.set()
        assert await sweep == 0, "the stale derivation must not be written"

    row = await _stored(_storage, "job-denorm-race")
    assert row["denorm_version"] == 0, "the row must stay pending for the next pass"
    assert "failure-raced" in row["failure_ids"], "the patch kept its own columns"

    # The next pass derives it from the newer payload, not from the stale one.
    assert await storage.backfill_results_denorm() == 1
    row = await _stored(_storage, "job-denorm-race")
    assert row["denorm_version"] == storage._RESULTS_DENORM_VERSION
    assert "failure-raced" in row["failure_ids"]
    found = await storage.find_failure_by_uuid("failure-raced")
    assert found is not None
    assert found["job_id"] == "job-denorm-race"


async def test_denorm_sweep_repairs_a_row_an_old_build_rewrote(_storage) -> None:
    """A pre-compression build updates the payload and leaves the version alone.

    That build cannot decode a frame, so the only row it can still rewrite is a
    TEXT one. Trusting ``denorm_version`` alone there would leave the row at the
    current version with stale filters and unfindable failures forever.
    """
    await _insert_legacy("job-old-writer", _payload(failure_count=20))
    assert await storage.backfill_results_denorm() == 1

    newer = _payload(failure_count=20, summary="newer summary", job_name="renamed")
    newer["request_params"]["submitted_by"] = "dana"
    newer["failures"].append(
        {"id": "failure-old-writer", "test_name": "t", "error": "e"}
    )
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE results SET result_json = ?, job_name = ?, build_number = ?,"
            " build_id = ? WHERE job_id = 'job-old-writer'",
            (json.dumps(newer), "renamed", 42, "4242"),
        )
        await db.commit()

    stale = await _stored(_storage, "job-old-writer")
    assert stale["denorm_version"] == storage._RESULTS_DENORM_VERSION
    assert (stale["summary"], stale["submitted_by"]) == ("many failures", "yuval")

    assert await storage.backfill_results_denorm() == 1

    row = await _stored(_storage, "job-old-writer")
    assert (row["summary"], row["submitted_by"]) == ("newer summary", "dana")
    assert "failure-old-writer" in row["failure_ids"]
    assert await storage.list_dashboard_submitters() == ["dana"]

    # Converged: a further pass parses the row but no longer writes it.
    assert await storage.backfill_results_denorm() == 0


async def test_compression_rederives_a_row_an_old_build_rewrote(_storage) -> None:
    """A frame only ever lands with the columns derived from that same payload.

    The denorm sweep runs first, then a pre-compression build rewrites a row it
    will not revisit: framing that text with the columns of the older payload would
    leave the filters stale in a row no later sweep visits, because a frame at the
    current version is final.
    """
    await _insert_legacy("job-old-writer-compress", _payload(failure_count=20))
    assert await storage.backfill_results_denorm() == 1

    newer = _payload(failure_count=20, summary="newer summary", job_name="renamed")
    newer["request_params"]["submitted_by"] = "dana"
    newer["failures"].append(
        {"id": "failure-old-writer", "test_name": "t", "error": "e"}
    )
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE results SET result_json = ? WHERE job_id = ?",
            (json.dumps(newer), "job-old-writer-compress"),
        )
        await db.commit()
    stale = await _stored(_storage, "job-old-writer-compress")
    assert (stale["summary"], stale["submitted_by"]) == ("many failures", "yuval")

    assert (await storage.backfill_result_json_compression())["rows"] == 1

    row = await _stored(_storage, "job-old-writer-compress")
    assert row["kind"] == "blob"
    assert (row["summary"], row["submitted_by"]) == ("newer summary", "dana")
    assert "failure-old-writer" in row["failure_ids"]
    assert storage.parse_result_json(row["result_json"]) == newer


async def test_denorm_sweep_survives_an_undecodable_row(_storage, storage_logs) -> None:
    """One row this build cannot decode must not abort the derive pass.

    The pending sets only admit inline TEXT, so a framed row is not selected today;
    the guard is what keeps a damaged payload from taking the sweep (and startup)
    down if a pending set ever admits one. The row itself is never rewritten, so
    rolling forward still derives its columns.
    """
    await _insert_damaged("job-damaged-denorm")
    # Not selected by the TEXT-only pending sets, and the pass still completes.
    assert await storage.backfill_results_denorm() == 0

    frame = zstd.compress(json.dumps(_payload(failure_count=2)).encode())
    params = storage._denorm_row_params(
        [
            {"rid": 1, "job_id": "job-damaged", "result_json": frame[:10]},
            {
                "rid": 2,
                "job_id": "job-good",
                "result_json": json.dumps(_payload(failure_count=2)),
            },
        ]
    )
    assert [p["rid"] for p in params] == [2], "an undecodable row was derived"
    assert "Startup skipped job_id=job-damaged" in storage_logs.text
    assert storage.undecodable_result_count() == 1


async def test_denorm_sweep_never_blanks_identity_a_partial_payload_omits(
    _storage,
) -> None:
    """A derived empty value means the payload does not know, not "clear it".

    ``update_status`` keeps ``build_id`` in the column when its payload has none
    and stamps the row current, so the sweep re-derives that row from the partial
    payload — where the missing key must leave the preserved identity alone.
    """
    await storage.save_result(
        job_id="job-partial",
        status="running",
        result={
            "job_name": "partial-job",
            "build_number": 42,
            "build_id": "999",
            "display_name": "Human Name",
            "request_params": {"submitted_by": "yuval"},
        },
    )
    await storage.update_status(
        "job-partial",
        "failed",
        result={"job_name": "partial-job", "error": "boom"},
    )
    assert await storage.backfill_results_denorm() == 1

    row = await _stored(_storage, "job-partial")
    assert (row["build_id"], row["build_number"]) == ("999", 42)
    assert (row["submitted_by"], row["display_name"]) == ("yuval", "Human Name")
    assert row["denorm_version"] == storage._RESULTS_DENORM_VERSION
    assert row["kind"] == "text"
    payload = storage.parse_result_json(row["result_json"])
    assert (payload["job_name"], payload["error"]) == ("partial-job", "boom")
    assert "build_id" not in payload, "the payload never carried it"

    # Fill-in still works: a payload that does carry a build_id replaces it.
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE results SET result_json = ? WHERE job_id = 'job-partial'",
            (json.dumps({"job_name": "partial-job", "build_id": "1000"}),),
        )
        await db.commit()
    assert await storage.backfill_results_denorm() == 1
    row = await _stored(_storage, "job-partial")
    assert row["build_id"] == "1000"
    assert row["build_number"] == 42, "the payload still says nothing about it"


async def test_denorm_sweep_never_blanks_failures_or_summary_a_payload_omits(
    _storage,
) -> None:
    """Same class as the ``build_id`` regression, different columns.

    ``update_status`` replaces the payload, so a partial one carries neither
    ``failures`` nor ``summary`` — the columns survive the write, and the sweep
    must not blank them either, or the failures become unfindable by identifier.
    """
    await storage.save_result(
        job_id="job-partial-failures",
        status="running",
        result={
            "job_name": "partial-job",
            "summary": "all failures analysed",
            "failures": [
                {"id": "fail-kept-1", "test_name": "t1", "error": "e1"},
                {"id": "fail-kept-2", "test_name": "t2", "error": "e2"},
            ],
        },
    )
    await storage.update_status(
        "job-partial-failures",
        "failed",
        result={"job_name": "partial-job", "error": "boom"},
    )
    assert await storage.backfill_results_denorm() == 1

    row = await _stored(_storage, "job-partial-failures")
    assert row["failure_ids"] == "fail-kept-1 fail-kept-2"
    assert row["summary"] == "all failures analysed"
    assert "failures" not in storage.parse_result_json(row["result_json"])

    # Fill-in still works: the payload that does describe them replaces them.
    await storage.save_result(
        job_id="job-partial-failures",
        status="completed",
        result={
            "job_name": "partial-job",
            "summary": "fresh",
            "failures": [{"id": "fail-new", "test_name": "t1", "error": "e1"}],
        },
    )
    row = await _stored(_storage, "job-partial-failures")
    assert (row["failure_ids"], row["summary"]) == ("fail-new", "fresh")
    assert await storage.backfill_results_denorm() == 0


async def test_denorm_sweep_honours_a_value_the_payload_explicitly_clears(
    _storage,
) -> None:
    """An empty value under a present key is a clear, not an omission.

    The writer keys on presence, so ``request_params.submitted_by = ""`` clears
    the column. A build that replaces the payload without its columns (an older
    one) leaves the stale value behind, and once the row is compressed a
    version-gated pass can never revisit it — only the TEXT sweep can.
    """
    await _insert_legacy("job-cleared", _payload(failure_count=3))
    assert await storage.backfill_results_denorm() == 1
    row = await _stored(_storage, "job-cleared")
    assert (row["submitted_by"], row["display_name"]) == ("yuval", "Human Name")
    assert await storage.list_dashboard_submitters() == ["yuval"]

    cleared = _payload(failure_count=3)
    cleared["request_params"]["submitted_by"] = ""
    cleared["display_name"] = ""
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE results SET result_json = ? WHERE job_id = 'job-cleared'",
            (json.dumps(cleared),),
        )
        await db.commit()

    assert await storage.backfill_results_denorm() == 1
    row = await _stored(_storage, "job-cleared")
    assert (row["submitted_by"], row["display_name"]) == ("", "")
    assert await storage.list_dashboard_submitters() == []
    assert await storage.backfill_results_denorm() == 0, "the clear converged"


async def test_init_db_leaves_legacy_row_payloads_alone(_storage) -> None:
    """Startup is schema-only; the derive pass is a background task (#409).

    Parsing every legacy payload inside ``init_db`` outlives the startup probe on
    a large database and the rolled-back transaction repeats on the next boot.
    """
    text = await _insert_legacy("job-startup", _payload(failure_count=5))

    await storage.init_db()

    after = await _stored(_storage, "job-startup")
    assert (after["kind"], after["result_json"]) == ("text", text)
    assert (after["submitted_by"], after["summary"], after["failure_ids"]) == (
        "",
        "",
        "",
    )
    assert after["denorm_version"] == 0

    # The background passes are what derive it, then compress it.
    assert await storage.backfill_results_denorm() == 1
    assert (await storage.backfill_result_json_compression())["rows"] == 1


async def test_denorm_backfill_resumes_after_an_interrupted_pass(_storage) -> None:
    """One page per transaction: a killed pass keeps its progress (#409)."""
    for index in range(3):
        await _insert_legacy(f"job-resume-{index}", _payload(failure_count=40))

    derive = storage._denorm_row_params
    pages = 0

    def kill_after_first_page(rows: list) -> list:
        nonlocal pages
        pages += 1
        if pages > 1:
            raise RuntimeError("pod killed mid-sweep")
        return derive(rows)

    with (
        patch.object(storage, "_denorm_row_params", kill_after_first_page),
        pytest.raises(RuntimeError, match="pod killed"),
    ):
        await storage.backfill_results_denorm(batch_size=1)

    # The first page was committed before the kill, so its progress survived.
    first = await _stored(_storage, "job-resume-0")
    assert first["denorm_version"] == storage._RESULTS_DENORM_VERSION
    assert (await _stored(_storage, "job-resume-1"))["denorm_version"] == 0

    # Compressing it is this build's own next step, and it makes the derived
    # page final: only a row left as TEXT is re-derived by the next pass.
    assert (await storage.backfill_result_json_compression())["rows"] == 1

    # The next boot finishes the rest and does not redo the committed page.
    async with storage._connect_db() as db:
        await db.execute(
            "UPDATE results SET summary = 'sentinel' WHERE job_id = 'job-resume-0'"
        )
        await db.commit()
    assert await storage.backfill_results_denorm(batch_size=1) == 2
    assert (await _stored(_storage, "job-resume-0"))["summary"] == "sentinel"
    for index in (1, 2):
        row = await _stored(_storage, f"job-resume-{index}")
        assert row["denorm_version"] == storage._RESULTS_DENORM_VERSION
        assert row["submitted_by"] == "yuval"


async def test_compression_skips_a_row_whose_columns_are_not_derived(_storage) -> None:
    """Ordering guard: a row below the current denorm version is never compressed."""
    payload = _payload(failure_count=20)
    await _insert_legacy("job-not-derived", payload)

    assert (await storage.backfill_result_json_compression())["rows"] == 0
    assert (await _stored(_storage, "job-not-derived"))["kind"] == "text"


async def test_submitted_by_index_is_a_plain_column_index(_storage) -> None:
    """The old expression index must be replaced, not kept by IF NOT EXISTS."""
    async with storage._connect_db() as db:
        await db.execute("DROP INDEX IF EXISTS idx_results_submitted_by")
        await db.execute(
            "CREATE INDEX idx_results_submitted_by ON results "
            "(json_extract(result_json, '$.request_params.submitted_by'))"
        )
        # The replacement is claimed once per database: replay this build's first boot.
        await db.execute(
            "DELETE FROM _migrations_applied WHERE key = ?",
            (storage._SUBMITTED_BY_INDEX_KEY,),
        )
        await db.commit()

    await storage.init_db()

    async with storage._connect_db() as db:
        columns = [
            row["name"]
            for row in await (
                await db.execute("PRAGMA index_info(idx_results_submitted_by)")
            ).fetchall()
        ]
        plan = " ".join(
            row["detail"]
            for row in await (
                await db.execute(f"EXPLAIN QUERY PLAN {storage._SUBMITTER_LIST_SQL}")
            ).fetchall()
        )
    assert columns == ["submitted_by"]
    assert "idx_results_submitted_by" in plan

    # A later boot must not drop and rebuild it: the swap is claimed once.
    async with storage._connect_db() as db:
        cursor = await db.execute("PRAGMA schema_version")
        schema_version = (await cursor.fetchone())[0]
    await storage.init_db()
    async with storage._connect_db() as db:
        cursor = await db.execute("PRAGMA schema_version")
        after = (await cursor.fetchone())[0]
    assert after == schema_version, "startup must not run DDL on every boot"


# --- the six SQL sites that used to read JSON inside SQLite -----------------


async def test_submitter_picker_and_filter_read_a_compressed_row(_storage) -> None:
    payload = _payload(failure_count=5)
    await storage.save_result("job-submitter", "", "completed", payload)
    assert (await _stored(_storage, "job-submitter"))["kind"] == "blob"

    assert await storage.list_dashboard_submitters() == ["yuval"]
    listed = await storage.list_results_for_dashboard_filtered(submitters={"yuval"})
    assert [job["job_id"] for job in listed["jobs"]] == ["job-submitter"]
    assert (await storage.get_job_submitters(["job-submitter"])) == {
        "job-submitter": "yuval"
    }
    # A different name matches nothing — the column is not a LIKE attack surface.
    other = await storage.list_results_for_dashboard_filtered(submitters={"someone"})
    assert other["jobs"] == []


async def test_timeout_filter_matches_a_compressed_row(_storage) -> None:
    await storage.save_result(
        "job-timeout", "", "failed", _payload(summary="analysis timed out after 600s")
    )
    await storage.save_result(
        "job-timeout-plain", "", "failed", _payload(summary="all good")
    )
    assert (await _stored(_storage, "job-timeout"))["kind"] == "blob"

    listed = await storage.list_results_for_dashboard_filtered(status=["timeout"])
    assert [job["job_id"] for job in listed["jobs"]] == ["job-timeout"]


async def test_audit_labels_a_compressed_row(_storage) -> None:
    await storage.save_result("job-audit", "", "completed", _payload(failure_count=5))
    async with storage._connect_db() as db:
        resolved = await storage._audit_target_jobs(
            db, ["job-audit", "Human Name", "big-job", "origin-only"]
        )
    assert resolved["job-audit"] == ("job-audit", "Human Name")
    assert resolved["Human Name"] == ("job-audit", "Human Name")
    assert resolved["big-job"] == ("job-audit", "Human Name")
    assert "origin-only" not in resolved


async def test_audit_labels_a_row_the_sweep_has_not_reached(_storage) -> None:
    """A legacy row's names are only in its payload until the sweep fills them.

    Startup does not await that sweep, so reading the columns alone would lose the
    audit link for a reference to the name (and label a job-id reference with no
    name at all).
    """
    await _insert_legacy("job-legacy-audit", _payload(failure_count=1))
    assert (await _stored(_storage, "job-legacy-audit"))["kind"] == "text"

    async with storage._connect_db() as db:
        resolved = await storage._audit_target_jobs(
            db, ["job-legacy-audit", "Human Name", "big-job"]
        )
    assert resolved["job-legacy-audit"] == ("job-legacy-audit", "Human Name")
    assert resolved["Human Name"] == ("job-legacy-audit", "Human Name")
    assert resolved["big-job"] == ("job-legacy-audit", "Human Name")


async def test_find_failure_by_uuid_reads_a_compressed_row(_storage) -> None:
    await storage.save_result("job-uuid", "", "completed", _payload(failure_count=5))
    assert (await _stored(_storage, "job-uuid"))["kind"] == "blob"

    top = await storage.find_failure_by_uuid("failure-3")
    assert top is not None
    assert top["job_id"] == "job-uuid"
    assert top["failure"]["test_name"] == "test_3"

    child = await storage.find_failure_by_uuid("child-failure-1")
    assert child is not None
    assert child["child_job_name"] == "child-job"
    assert child["child_build_number"] == 7

    assert await storage.find_failure_by_uuid("not-a-uuid-at-all") is None


async def test_find_failure_by_uuid_finds_a_row_the_sweep_has_not_reached(
    _storage,
) -> None:
    """A legacy row holds ``''`` in ``failure_ids`` until the sweep reaches it.

    Startup does not await that sweep, so the derived column cannot be the only way
    in: failure lookup is how both re-analysis endpoints resolve their target.
    """
    await _insert_legacy("job-legacy-lookup", _payload(failure_count=3))
    row = await _stored(_storage, "job-legacy-lookup")
    assert (row["failure_ids"], row["denorm_version"]) == ("", 0)

    top = await storage.find_failure_by_uuid("failure-1")
    assert top is not None
    assert top["job_id"] == "job-legacy-lookup"

    child = await storage.find_failure_by_uuid("child-failure-1")
    assert child is not None
    assert child["child_job_name"] == "child-job"

    # Derived now, so the prefilter finds it without looking at the payload.
    assert await storage.backfill_results_denorm() == 1
    found = await storage.find_failure_by_uuid("failure-1")
    assert found is not None
    assert found["job_id"] == "job-legacy-lookup"


async def test_get_job_submitters_reads_a_row_the_sweep_has_not_reached(
    _storage,
) -> None:
    """Ownership checks read the payload of an underived legacy row.

    The column is empty until the sweep fills it, which would refuse an operator
    the delete of their own job for as long as the sweep takes.
    """
    await _insert_legacy("job-legacy-submitter", _payload(failure_count=1))
    assert (await _stored(_storage, "job-legacy-submitter"))["submitted_by"] == ""

    expected = {"job-legacy-submitter": "yuval"}
    assert await storage.get_job_submitters(["job-legacy-submitter"]) == expected

    assert await storage.backfill_results_denorm() == 1
    assert await storage.get_job_submitters(["job-legacy-submitter"]) == expected
    # A job that does not exist is still absent, not an empty submitter.
    assert await storage.get_job_submitters(["job-missing"]) == {}


async def test_no_sql_site_still_parses_result_json(_storage) -> None:
    """Every site that reads the payload must go through parse_result_json.

    Three blocks are allowlisted, all guarded by ``typeof(...) = 'text'`` so none
    can ever read a frame: the ``failure_history`` build-id candidate CTE, which
    runs inside ``init_db``; the failure-UUID prefilter's fallback for a row the
    denorm sweep has not reached yet; and the audit list's legacy name-match
    fragment for the same upgrade window.
    """
    source = Path(storage.__file__).read_text()
    for allowed in (
        storage._FH_BUILD_ID_CANDIDATES_CTE,
        storage._FIND_FAILURE_BY_UUID_SQL,
        storage._AUDIT_LEGACY_NAME_MATCH_SQL,
    ):
        assert source.count(allowed) == 1
        source = source.replace(allowed, "")

    patterns = (
        r"json_extract\(\s*\w*\.?result_json",
        r"json_valid\(result_json",
        r"json_type\(result_json",
        r"INSTR\(result_json",
    )
    for pattern in patterns:
        assert not re.search(pattern, source), pattern

    for module in Path(storage.__file__).parent.glob("*.py"):
        if module.name == "storage.py":
            continue  # its allowlisted blocks were removed from ``source`` above
        text = module.read_text()
        for pattern in patterns:
            assert not re.search(pattern, text), f"{module.name}: {pattern}"


# --- the write lock is never held across an encode (#433) -------------------


@pytest.mark.parametrize("parked_writer", ["save_result", "update_status"])
async def test_a_slow_encode_does_not_hold_the_write_lock(
    _storage, monkeypatch, parked_writer: str
) -> None:
    """Other jobs' writes land while one job's payload is being encoded.

    The encode runs off the loop with no transaction open, so the write lock is
    taken only around the (fast) statement. Holding it across the encode makes
    the competing writes wait out ``busy_timeout`` and fail with ``database is
    locked`` -- the CI failure this shape exists to prevent.

    The parked writer is held inside its encode by events this test owns, so "the
    writes went through while the encode was in progress" holds by construction,
    not by the clock: that encode cannot finish before the test releases it, and
    the release comes after every competing write has returned.
    """
    payload = _payload(failure_count=20)
    # Seed the rows the status writers update while the encoder is still fast.
    for job_id in ("job-lock-status-a", "job-lock-status-b"):
        await storage.save_result(job_id, "", "running", payload)

    def write(job_id: str):
        if job_id.startswith("job-lock-save"):
            return storage.save_result(job_id, "", "completed", payload)
        return storage.update_status(job_id, "completed", payload)

    job_ids = (
        "job-lock-save-a",
        "job-lock-save-b",
        "job-lock-status-a",
        "job-lock-status-b",
    )
    parked = (
        "job-lock-save-a" if parked_writer == "save_result" else "job-lock-status-a"
    )
    rivals = [job_id for job_id in job_ids if job_id != parked]

    encoding = threading.Event()  # the parked writer has begun its encode
    release = threading.Event()  # the test lets that encode finish
    real_compress = zstd.compress

    def parked_first_compress(data: bytes, level: int = 0) -> bytes:
        if not encoding.is_set():  # only the parked writer waits; rivals must not
            encoding.set()
            # Only a deadlock guard: the test releases this, it never times it.
            assert release.wait(60), "the parked encode was never released"
        return real_compress(data, level)

    monkeypatch.setattr(storage.zstd, "compress", parked_first_compress)
    encoder = asyncio.create_task(write(parked))
    rival_tasks: list[asyncio.Task[None]] = []
    try:
        assert await asyncio.to_thread(encoding.wait, 60), "no encode to park"
        # Written while that encode is still parked. A write lock held across the
        # encode would raise here -- after the connection's ``busy_timeout``, so
        # this fails on the bug instead of hanging on it.
        rival_tasks = [asyncio.create_task(write(job_id)) for job_id in rivals]
        await asyncio.gather(*rival_tasks)
        assert not encoder.done(), "the encode has to still be in progress"
    finally:
        # Gather re-raises on the first failure and leaves its siblings running;
        # drain them before the encode is released so no write outlives the test.
        await asyncio.gather(*rival_tasks, return_exceptions=True)
        release.set()
        await encoder

    for job_id in ("job-lock-save-a", "job-lock-save-b"):
        assert (await _stored(_storage, job_id))["kind"] == "blob"
    for job_id in ("job-lock-status-a", "job-lock-status-b"):
        stored = await storage.get_result(job_id)
        assert stored["status"] == "completed"
        assert stored["result"]["summary"] == payload["summary"]


async def test_a_patch_waits_for_the_write_it_would_have_raced(
    _storage, monkeypatch
) -> None:
    """A patch cannot land inside another write, so neither can lose the other.

    The save holds the job's write lock while its payload is encoded, so a patch
    for the same job waits for it: the link is applied to the payload the save
    wrote, and the save's payload is complete rather than stale.
    """
    await storage.save_result("job-lock", "", "running", _payload(failure_count=20))

    real_compress = zstd.compress
    encoding = threading.Event()
    encodes: list[int] = []

    def slow_first_compress(data: bytes, level: int = 0) -> bytes:
        encodes.append(len(encodes))
        if len(encodes) == 1:
            encoding.set()
            time.sleep(0.4)  # the patch has to wait out this window
        return real_compress(data, level)

    monkeypatch.setattr(storage.zstd, "compress", slow_first_compress)
    finishing = _payload(failure_count=20, summary="finishing")
    save = asyncio.create_task(
        storage.save_result("job-lock", "", "completed", finishing)
    )
    await asyncio.to_thread(encoding.wait)

    def _link(data: dict) -> None:
        data["request_params"]["reanalyzed_to_job_ids"] = ["job-child"]

    patch = asyncio.create_task(storage.patch_result_json("job-lock", _link))
    assert not patch.done(), "the patch ran inside the save's write"
    await save
    await patch

    stored = storage.parse_result_json(
        (await _stored(_storage, "job-lock"))["result_json"], job_id="job-lock"
    )
    assert stored["summary"] == "finishing"
    assert len(stored["failures"]) == 20
    assert stored["request_params"]["reanalyzed_to_job_ids"] == ["job-child"], (
        "the patch must apply to the payload the save wrote"
    )
    # One encode by the save and one by the patch: neither re-encoded.
    assert len(encodes) == 2


async def test_a_status_write_waits_for_the_payload_write_it_raced(
    _storage, monkeypatch
) -> None:
    """Writes to one job are ordered, never interleaved, so nothing is reverted.

    A status-only update for a job whose payload is being encoded waits for that
    write. Both land, in that order: the completion is the newer state and the
    payload written before it stays.
    """
    await storage.save_result(
        "job-status-race", "", "running", _payload(failure_count=20)
    )
    real_dump = storage._dump_result_json
    encoding = threading.Event()
    encodes: list[int] = []

    def slow_first_dump(result: dict[str, Any]) -> Any:
        encodes.append(len(encodes))
        if len(encodes) == 1:
            encoding.set()
            time.sleep(0.4)  # the status-only writer has to wait this out
        return real_dump(result)

    monkeypatch.setattr(storage, "_dump_result_json", slow_first_dump)
    failing = asyncio.create_task(
        storage.update_status(
            "job-status-race",
            "failed",
            _payload(failure_count=20, summary="boom"),
        )
    )
    await asyncio.to_thread(encoding.wait)
    completing = asyncio.create_task(
        storage.update_status("job-status-race", "completed")
    )
    assert not completing.done(), "the status write ran inside the payload write"

    assert await failing is True
    assert await completing is True
    stored = await storage.get_result("job-status-race")
    assert stored["status"] == "completed", "the newer status must be the last one"
    assert stored["result"]["summary"] == "boom"
    assert len(encodes) == 1, "an ordered write must not re-encode"


async def test_an_expectation_that_moved_refuses_the_transition(_storage) -> None:
    """A transition decided from one status is refused over a newer one.

    The abort endpoint decides from the status it re-read; the write takes that
    status as its guard rather than whatever the row holds by then (see
    ``_write_status``), so a job that finished meanwhile is never ended over.
    """
    await storage.save_result("job-expect", "", "running", _payload(failure_count=20))
    assert await storage.update_status("job-expect", "completed") is True

    assert (
        await storage.update_status("job-expect", "aborted", expect_status="running")
        is False
    )
    stored = await storage.get_result("job-expect")
    assert stored["status"] == "completed"
    assert stored["result"]["summary"] == "many failures"


async def test_a_status_only_update_never_touches_the_payload(
    _storage, monkeypatch
) -> None:
    """No payload, no read, no thread hop and no encode -- just the status write."""
    await storage.save_result(
        "job-status-only", "", "running", _payload(failure_count=20)
    )
    before = (await _stored(_storage, "job-status-only"))["result_json"]

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a status-only update read or encoded result_json")

    monkeypatch.setattr(storage, "_dump_result_json", boom)
    monkeypatch.setattr(storage, "parse_result_json", boom)
    real_to_thread = asyncio.to_thread
    hops: list[Any] = []

    async def counting_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        hops.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)
    await storage.update_status("job-status-only", "failed")

    assert hops == [], "a status-only update paid a thread hop"
    async with storage._connect_db() as db:
        status = (
            await (
                await db.execute(
                    "SELECT status FROM results WHERE job_id = 'job-status-only'"
                )
            ).fetchone()
        )[0]
    assert status == "failed"
    assert (await _stored(_storage, "job-status-only"))["result_json"] == before


async def test_usage_recorded_after_the_snapshot_is_merged_by_the_next_write(
    _storage, monkeypatch
) -> None:
    """The payload summarizes the usage recorded before its write, no more.

    ``record_token_usage`` inserts on its own connection and does not take the
    job's write lock, so a call that finishes while a payload is encoded is not
    in that payload. It is not lost: it is in the usage rows, which the next write
    merges -- the cost of serialising writes instead of compare-and-swapping them.
    """
    await storage.save_result(
        "job-usage-race", "", "running", _payload(failure_count=20)
    )
    real_dump = storage._dump_result_json
    encoding = threading.Event()
    encodes: list[int] = []

    def slow_first_dump(result: dict[str, Any]) -> Any:
        encodes.append(len(encodes))
        if len(encodes) == 1:
            encoding.set()
            time.sleep(0.4)  # the usage row lands inside this window
        return real_dump(result)

    monkeypatch.setattr(storage, "_dump_result_json", slow_first_dump)
    finishing = asyncio.create_task(
        storage.update_status(
            "job-usage-race",
            "completed",
            _payload(failure_count=20, summary="finishing"),
        )
    )
    await asyncio.to_thread(encoding.wait)
    await storage.record_token_usage(
        "job-usage-race",
        "claude",
        "opus",
        "primary",
        input_tokens=7,
        output_tokens=3,
    )
    assert await finishing is True

    committed = storage.parse_result_json(
        (await _stored(_storage, "job-usage-race"))["result_json"],
        job_id="job-usage-race",
    )
    assert committed["summary"] == "finishing"
    assert "total_input_tokens" not in committed["token_usage"], (
        "the row landed after the snapshot"
    )

    await storage.update_status(
        "job-usage-race", "completed", _payload(failure_count=20, summary="finishing")
    )
    merged = storage.parse_result_json(
        (await _stored(_storage, "job-usage-race"))["result_json"],
        job_id="job-usage-race",
    )
    assert merged["token_usage"]["total_input_tokens"] == 7, (
        "the next write must merge the usage recorded since the snapshot"
    )


# --- one row, two writers ---------------------------------------------------
#
# Every ``result_json`` write holds its job's asyncio lock from the read to the
# commit, but that lock is process-local: a second uvicorn worker has its own, and
# its read/merge/encode happens before SQLite's write lock. So the write also
# proves inside its transaction that the row still is the one it encoded, and
# redoes itself on the payload it finds when it is not.


async def test_a_legacy_read_backfill_does_not_clobber_a_concurrent_patch(
    _storage, monkeypatch
) -> None:
    """A read that backfills ids must never commit the payload it parsed.

    ``get_result`` is the one read that writes: it stores the ids a legacy payload
    lacks. Committing the payload it parsed would erase a patch a writer landed in
    between, so the backfill is a write of its own -- it re-reads the row under the
    job's write lock and the read returns what that write stored.
    """
    legacy = _payload(failure_count=3)
    for failure in legacy["failures"]:
        failure.pop("id")
    await _insert_legacy("job-legacy-read", legacy)

    real_connect = storage._connect_db
    opened = 0
    patch_task: asyncio.Task[None] | None = None

    async def apply_patch() -> None:
        await storage.patch_result_json(
            "job-legacy-read", lambda data: data.update({"summary": "patched"})
        )

    @contextlib.asynccontextmanager
    async def racing_connect():
        # Connection 2 is the backfill, which has read its payload and not written
        # it yet: the window a patch must not be erased from.
        nonlocal opened, patch_task
        opened += 1
        if opened == 2:
            patch_task = asyncio.create_task(apply_patch())
            with contextlib.suppress(TimeoutError):
                # The backfill holds the job's write lock here, so the patch waits
                # for it instead of landing in the middle of the read.
                await asyncio.wait_for(asyncio.shield(patch_task), 0.2)
        async with real_connect() as db:
            yield db

    monkeypatch.setattr(storage, "_connect_db", racing_connect)
    read = await storage.get_result("job-legacy-read")
    assert patch_task is not None
    await patch_task

    stored = storage.parse_result_json(
        (await _stored(_storage, "job-legacy-read"))["result_json"],
        job_id="job-legacy-read",
    )
    assert stored["summary"] == "patched", (
        "the read committed the payload it parsed over a patch"
    )
    ids = [failure["id"] for failure in stored["failures"]]
    assert len(set(ids)) == 3, "the legacy ids are persisted"
    # The ids the read reports are the ones it stored, so the next read is stable.
    assert [failure["id"] for failure in read["result"]["failures"]] == ids


async def test_a_refused_backfill_returns_the_ids_the_row_stored(_storage) -> None:
    """A backfill that loses the row to a rival must report the stored ids.

    Two reads of one legacy job each assign ids locally; the first lands them, so
    the second finds ids already persisted and refuses its write. What it returns
    still has to be what the row holds: otherwise ``get_result`` serves ids no
    payload contains, and the failure lookup that verifies against the stored
    payload cannot find them.
    """
    legacy = _payload(failure_count=3)
    for failure in legacy["failures"]:
        failure.pop("id")
    await _insert_legacy("job-refused-backfill", legacy)
    # The second request's read: parsed before the rival persisted any id.
    second_read = storage.parse_result_json_lenient(
        json.dumps(legacy), job_id="job-refused-backfill"
    )

    first = await storage.get_result("job-refused-backfill")
    refused = await storage._backfill_failure_uuids("job-refused-backfill", second_read)

    stored = storage.parse_result_json(
        (await _stored(_storage, "job-refused-backfill"))["result_json"],
        job_id="job-refused-backfill",
    )
    stored_ids = [failure["id"] for failure in stored["failures"]]
    assert [failure["id"] for failure in first["result"]["failures"]] == stored_ids
    assert [failure["id"] for failure in refused["failures"]] == stored_ids, (
        "the refused backfill returned ids it never stored"
    )
    for failure_id in (failure["id"] for failure in refused["failures"]):
        assert await storage.find_failure_by_uuid(failure_id) is not None, (
            f"reported id {failure_id} is not in the stored payload"
        )


async def test_two_processes_editing_one_job_keep_both_edits(
    _storage, monkeypatch
) -> None:
    """A second worker's write must not be lost to the first worker's stale read.

    A fresh lock per write is what two processes see: same row, no shared lock, so
    nothing serialises their read/encode. The guard inside the write transaction
    does -- the writer whose row moved re-reads and lands on top of the other edit.
    """
    await storage.save_result(
        "job-two-workers", "", "running", _payload(failure_count=2)
    )
    monkeypatch.setattr(storage, "_job_write_lock", lambda _job_id: asyncio.Lock())

    real_connect = storage._connect_db
    opened = 0
    first_read = asyncio.Event()
    second_written = asyncio.Event()

    @contextlib.asynccontextmanager
    async def pausing_connect():
        # Connection 2 is the first worker's write: it has encoded a payload from
        # the read it did before, and has not taken SQLite's write lock yet.
        nonlocal opened
        opened += 1
        if opened == 2:
            first_read.set()
            await second_written.wait()
        async with real_connect() as db:
            yield db

    monkeypatch.setattr(storage, "_connect_db", pausing_connect)
    first = asyncio.create_task(
        storage.patch_result_json(
            "job-two-workers", lambda data: data.update({"first": True})
        )
    )
    await asyncio.wait_for(first_read.wait(), 5)
    await storage.patch_result_json(
        "job-two-workers", lambda data: data.update({"second": True})
    )
    second_written.set()
    await first

    stored = storage.parse_result_json(
        (await _stored(_storage, "job-two-workers"))["result_json"],
        job_id="job-two-workers",
    )
    assert stored["first"] is True, "the first worker's edit was overwritten"
    assert stored["second"] is True, (
        "the second worker's edit was silently overwritten by the first"
    )
