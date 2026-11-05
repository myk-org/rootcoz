"""Tests for the compressed ``result_json`` column (#409).

The column carries the whole payload, so these tests are end-to-end at the
storage layer: what the writer stores, what the backfill rewrites, and every SQL
site that used to read the payload with JSON functions — which cannot read a frame.
"""

import asyncio
import json
import re
import sqlite3
import threading
import time
from compression import zstd
from pathlib import Path
from unittest.mock import patch

import pytest

from rootcoz import storage

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
    """A truncated frame and a frame of non-UTF-8 bytes are a read miss, not an error."""
    frame = zstd.compress(json.dumps(_payload(failure_count=20)).encode())
    bad_values = (
        frame[: len(frame) // 2],  # truncated frame
        zstd.compress(b"\xff\xfe\x00\x00not utf-8"),  # decodes to non-UTF-8 bytes
        b"not a frame",
    )
    for bad in bad_values:
        assert storage.parse_result_json(bad, job_id="job-bad") is None

    async with storage._connect_db() as db:
        await db.execute(
            "INSERT INTO results (job_id, status, result_json) VALUES (?, ?, ?)",
            ("job-bad", "completed", b"not a frame"),
        )
        await db.commit()

    # No exception reaches the caller: the payload reads as empty, the row stays.
    assert (await storage.get_result("job-bad"))["result"] is None
    assert (await _stored(_storage, "job-bad"))["result_json"] == b"not a frame"

    # And no writer may overwrite a row it cannot read.
    await storage.patch_result_json("job-bad", lambda data: data.update({"x": 1}))
    assert (await _stored(_storage, "job-bad"))["result_json"] == b"not a frame"


# --- the backfill -----------------------------------------------------------


async def test_backfill_is_byte_identical(_storage) -> None:
    """The frame decompresses to exactly the bytes the old build stored."""
    text = await _insert_legacy("job-bytes", _payload(failure_count=30))
    await storage.backfill_results_denorm()

    stats = await storage.backfill_result_json_compression()
    assert stats["rows"] == 1

    row = await _stored(_storage, "job-bytes")
    assert row["kind"] == "blob"
    assert zstd.decompress(row["result_json"]) == text.encode()


async def test_backfill_shrinks_the_column(_storage) -> None:
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

    # Idempotent and resumable by construction: a second pass has nothing to do.
    assert (await storage.backfill_result_json_compression())["rows"] == 0


async def test_compression_does_not_overwrite_a_concurrent_patch(_storage) -> None:
    """The UPDATE is a compare-and-swap on the text it read, not just its type."""
    await _insert_legacy("job-race", _payload(failure_count=40))
    await storage.backfill_results_denorm()
    newer = {"job_name": "newer", "request_params": {}, "failures": []}
    compress = storage._compress_result_rows

    def race(rows: list) -> list:
        # Another writer replaces the payload after this batch was read.
        with sqlite3.connect(_storage) as conn:
            conn.execute(
                "UPDATE results SET result_json = ? WHERE job_id = 'job-race'",
                (json.dumps(newer),),
            )
        return compress(rows)

    with patch.object(storage, "_compress_result_rows", race):
        stats = await storage.backfill_result_json_compression()

    assert stats["rows"] == 0
    row = await _stored(_storage, "job-race")
    assert row["kind"] == "text"
    assert json.loads(row["result_json"]) == newer


async def test_backfill_does_not_block_the_event_loop(_storage) -> None:
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
    assert elapsed > 0.01, "the run must be long enough for the ticker to matter"
    # One tick per batch at minimum; a blocking encode would allow at most one.
    assert ticks > stats["rows"] // 10


# --- the denormalized columns ----------------------------------------------


async def test_denorm_backfill_repairs_a_late_old_build_row(_storage) -> None:
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


async def test_no_sql_site_still_parses_result_json(_storage) -> None:
    """Every site that reads the payload must go through parse_result_json.

    One maintenance-only block is allowlisted: the ``failure_history`` build-id
    candidate CTE, which runs inside ``init_db`` and carries a
    ``typeof(...) = 'text'`` guard for the boot after the compression sweep.
    """
    source = Path(storage.__file__).read_text()
    for allowed in (storage._FH_BUILD_ID_CANDIDATES_CTE,):
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
