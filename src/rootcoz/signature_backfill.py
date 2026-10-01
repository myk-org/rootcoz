"""Recompute stored failure signatures after normalization rules change.

Failure signatures are stored (``results.result_json``, ``failure_history``,
``comments``) rather than derived at read time, so changing the normalization
rules in :mod:`rootcoz.engine.core` changes the hash every new analysis
produces.  Stored rows keep their old hashes, and auto-review / history
matching silently stop matching across the deploy boundary.

This module rewrites stored signatures with the current rules so old and new
data share one hash space.

Run it **after** deploying the code whose rules you are adopting -- running it
first would write new-format hashes into a server still producing old-format
ones.  It is not a permanent fix: the next rule change recreates the same
cliff.  See ``compute_signature`` for the single hash definition shared with
the analyzer.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from rootcoz.engine.core import compute_signature, normalization_rules_version
from rootcoz.storage import (
    RESULT_JSON_BATCH_SIZE,
    SignatureUpdate,
    claim_signature_migration,
    complete_signature_migration,
    get_signature_versions,
    iter_result_json_batches,
    mark_migration_applied,
    owns_signature_migration,
    patch_result_json,
)

logger = logging.getLogger(__name__)

# Migration key prefix. The fingerprint of the normalization rules is appended,
# so any rule edit records a new key and the history of completed migrations
# grows by one. What the database *currently* holds is tracked separately, by
# storage.get_signature_versions() -- the history alone cannot answer that.
MIGRATION_KEY_PREFIX = "failure-signatures-"


class BackfillStats:
    """Counts for one backfill run."""

    def __init__(self) -> None:
        self.jobs_scanned = 0
        self.jobs_changed = 0
        self.failures_scanned = 0
        self.failures_changed = 0
        self.history_rows_changed = 0
        self.comment_rows_changed = 0
        self.unparsable_jobs: list[str] = []
        # Records whose signature inputs cannot be recovered (see
        # signature_inputs). Reported rather than rehashed with a guessed trace.
        self.unrecoverable_failures: list[dict[str, Any]] = []
        self.migration_preempted = False

    def as_dict(self) -> dict[str, Any]:
        """Return the stats as a JSON-serializable dict."""
        return {
            "dry_run": True,
            "jobs_scanned": self.jobs_scanned,
            "jobs_changed": self.jobs_changed,
            "failures_scanned": self.failures_scanned,
            "failures_changed": self.failures_changed,
            "history_rows_changed": self.history_rows_changed,
            "comment_rows_changed": self.comment_rows_changed,
            "unparsable_jobs": self.unparsable_jobs,
            "unrecoverable_failures": self.unrecoverable_failures,
            "migration_preempted": self.migration_preempted,
        }


def iter_stored_failures(
    result_data: dict[str, Any],
) -> Iterator[tuple[dict[str, Any], str, int]]:
    """Yield every stored failure with its child-job identity.

    Walks ``failures`` plus the nested ``child_job_analyses`` tree (including
    ``failed_children``), yielding ``(failure, child_job_name,
    child_build_number)``. Identity is carried separately rather than written
    into the failure dict so the stored data is never polluted with scratch
    keys, and so failures sharing a ``test_name`` across child jobs stay
    distinguishable.

    Args:
        result_data: Parsed ``result_json`` for one job.

    Yields:
        ``(failure_dict, child_job_name, child_build_number)``.
    """

    def walk(
        failures: list[Any], child_job_name: str, child_build_number: int
    ) -> Iterator[tuple[dict[str, Any], str, int]]:
        for failure in failures:
            if isinstance(failure, dict):
                yield failure, child_job_name, child_build_number

    def walk_children(
        children: list[Any],
    ) -> Iterator[tuple[dict[str, Any], str, int]]:
        for child in children:
            if not isinstance(child, dict):
                continue
            yield from walk(
                child.get("failures", []),
                child.get("job_name", ""),
                child.get("build_number", 0),
            )
            yield from walk_children(child.get("failed_children", []))

    yield from walk(result_data.get("failures", []), "", 0)
    yield from walk_children(result_data.get("child_job_analyses", []))


def signature_inputs(failure: dict[str, Any]) -> tuple[str, str] | None:
    """Return the persisted ``(error, stack_trace)`` pair a signature is built from.

    ``FailureAnalysis`` stores the message as ``error`` and the trace as
    ``stack_trace``; those are the two inputs ``compute_signature`` hashes.

    Returns:
        The inputs, or None when they cannot be recovered. Rows written before
        the trace was persisted carry no trace at all, and the original
        signature was computed from the real one -- substituting an empty trace
        would invent a hash that never existed, so those rows are reported by
        the caller instead of rewritten.
    """
    error = failure.get("error")
    stack_trace = failure.get("stack_trace")
    if not isinstance(error, str) or not isinstance(stack_trace, str):
        return None
    return error, stack_trace


def _rehash_stored_failures(
    result_data: dict[str, Any],
    *,
    collect: Callable[[SignatureUpdate], None] | None = None,
    stats: BackfillStats | None = None,
    job_id: str = "",
) -> int:
    """Recompute every stored failure signature in *result_data*, in place.

    Args:
        result_data: Parsed ``result_json`` for one job.
        collect: Called once per failure whose signature changes, with the
            matching ``failure_history`` / ``comments`` update. Only needed when
            writing; a dry run only needs the counts.
        stats: Collects the scan counts and the unrecoverable-record report.
        job_id: Reported alongside unrecoverable records.

    Returns:
        Number of failures whose signature changed.
    """
    changed = 0
    for failure, child_name, child_build in iter_stored_failures(result_data):
        if stats is not None:
            stats.failures_scanned += 1
        inputs = signature_inputs(failure)
        if inputs is None:
            if stats is not None:
                stats.unrecoverable_failures.append(
                    {
                        "job_id": job_id,
                        "test_name": failure.get("test_name", ""),
                        "child_job_name": child_name,
                        "child_build_number": child_build,
                        "error_signature": failure.get("error_signature", ""),
                    }
                )
            continue
        new_signature = compute_signature(*inputs)
        previous_signature = failure.get("error_signature", "")
        if new_signature == previous_signature:
            continue
        failure["error_signature"] = new_signature
        changed += 1
        if collect is not None:
            collect(
                SignatureUpdate(
                    test_name=failure.get("test_name", ""),
                    child_job_name=child_name,
                    child_build_number=child_build,
                    error_message=inputs[0],
                    previous_signature=previous_signature,
                    new_signature=new_signature,
                )
            )
    if stats is not None:
        stats.failures_changed += changed
    return changed


def _make_updater(
    collect: list[SignatureUpdate], stats: BackfillStats, job_id: str
) -> Callable[[dict[str, Any]], None]:
    """Return a ``patch_result_json`` callback that re-hashes *only* signatures.

    Runs against the row's current contents inside ``patch_result_json``'s write
    transaction, never against the copy the scan saw: a job that analysis or
    reanalysis updated in the meantime keeps its newer fields, and only the
    signature changes are written. A factory rather than a lambda so the values
    are bound, not the loop variables (ruff B023).

    Args:
        collect: Collects the matching denormalized updates for this job.
        stats: Counts the changes applied to the current row.
        job_id: Identifies the job, for the unrecoverable-record report.
    """

    def _update(data: dict[str, Any]) -> None:
        _rehash_stored_failures(
            data, collect=collect.append, stats=stats, job_id=job_id
        )

    return _update


async def ensure_signatures_current() -> dict[str, Any] | None:
    """Re-hash stored signatures if the signature algorithm has changed.

    Gated on a fingerprint of everything that affects a signature (see
    :func:`normalization_rules_version`), so this runs itself on startup after
    any change and never needs an admin to remember an operational step -- and,
    running post-deploy, it always uses the rules the running server is
    actually applying.

    Returns the backfill stats, or None when already current or when another
    process version owns the migration.

    The gate is the *currently applied* version, not the set of migration keys
    ever completed: after versions A then B, the A key is still on disk and
    would make a rollback to A skip the rehash B's rows need. The claim is
    re-checked before every batch so an older process stops writing once a newer
    deployment takes the migration over.
    """
    version = normalization_rules_version()
    if (await get_signature_versions())[0] == version:
        return None
    await claim_signature_migration(version)
    logger.info(
        "Failure signature normalization changed (%s) - recomputing stored signatures",
        version,
    )
    stats = await backfill_signatures(
        dry_run=False,
        guard=lambda: owns_signature_migration(version),
    )
    if stats["migration_preempted"]:
        logger.warning(
            "Signature backfill for %s stopped: another version owns the migration",
            version,
        )
        return None
    await mark_migration_applied(MIGRATION_KEY_PREFIX + version)
    if not await complete_signature_migration(version):
        logger.warning(
            "Signature version %s was taken over before it could be recorded",
            version,
        )
    logger.info(
        "Signature backfill complete: %s/%s failures re-hashed across %s jobs "
        "(%s records had unrecoverable signature inputs)",
        stats["failures_changed"],
        stats["failures_scanned"],
        stats["jobs_scanned"],
        len(stats["unrecoverable_failures"]),
    )
    return stats


async def backfill_signatures(
    *,
    dry_run: bool = True,
    guard: Callable[[], Awaitable[bool]] | None = None,
    batch_size: int = RESULT_JSON_BATCH_SIZE,
) -> dict[str, Any]:
    """Recompute and rewrite stored failure signatures.

    Each job's result and its ``failure_history`` / ``comments`` rows are
    rewritten in one transaction, in bounded batches, so the run is safe to
    interrupt and safe to retry.

    Args:
        dry_run: When True (the default) nothing is written and the returned
            counts describe what an apply would change.
        guard: Awaited before each batch; when it returns False the run stops
            and ``migration_preempted`` is set. Used to hand the migration to a
            newer process version.
        batch_size: Result blobs read per batch, bounding peak memory.

    Returns:
        Stats dict (see :meth:`BackfillStats.as_dict`).
    """
    stats = BackfillStats()

    async for batch in iter_result_json_batches(batch_size):
        if guard is not None and not await guard():
            stats.migration_preempted = True
            break
        for job_id, raw_json in batch:
            stats.jobs_scanned += 1
            try:
                result_data = json.loads(raw_json)
            except json.JSONDecodeError:
                # Never destroy data we cannot parse -- report and skip.
                logger.warning("Skipping unparsable result_json for job %s", job_id)
                stats.unparsable_jobs.append(job_id)
                continue
            if not isinstance(result_data, dict):
                stats.unparsable_jobs.append(job_id)
                continue

            if dry_run:
                if _rehash_stored_failures(result_data, stats=stats, job_id=job_id):
                    stats.jobs_changed += 1
                continue

            updates: list[SignatureUpdate] = []
            outcome = await patch_result_json(
                job_id,
                _make_updater(updates, stats, job_id),
                denormalized=updates,
                write_if_changed=True,
            )
            if outcome.written:
                stats.jobs_changed += 1
            stats.history_rows_changed += outcome.history_rows
            stats.comment_rows_changed += outcome.comment_rows
        # Yield between batches so live requests interleave with the scan
        # instead of queueing behind one long uninterrupted run.
        await asyncio.sleep(0)

    result = stats.as_dict()
    result["dry_run"] = dry_run
    logger.info(
        "Signature backfill (dry_run=%s): %s jobs scanned, %s failures changed, "
        "%s history rows, %s comment rows",
        dry_run,
        stats.jobs_scanned,
        stats.failures_changed,
        stats.history_rows_changed,
        stats.comment_rows_changed,
    )
    return result
