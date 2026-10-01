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
    analysis_in_flight,
    claim_signature_migration,
    complete_signature_migration,
    failure_error_text,
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

#: Unrecoverable records kept in memory and returned. The count is always
#: exact; the list is a sample, because the first run after traces started
#: being persisted reports every row stored before that -- one entry per
#: failure in the database, which is neither bounded nor usable as a report.
UNRECOVERABLE_SAMPLE_SIZE = 100

#: Same treatment for the comment groups whose rows cannot be attributed to
#: one of the failures sharing their old hash (see
#: :func:`rootcoz.storage._apply_denormalized_signatures`): counted exactly,
#: sampled in the report, never reassigned.
AMBIGUOUS_COMMENT_SAMPLE_SIZE = 100


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
        # Failures whose signature inputs cannot be recovered (see
        # signature_inputs): counted exactly, sampled in the report, and left
        # with the hash they already carry rather than rehashed from a guess.
        self.unrecoverable_count = 0
        self.unrecoverable_failures: list[dict[str, Any]] = []
        # Comment rows that could not be attributed to the failure they were
        # written about, because every failure sharing their old hash did not
        # end on the same hash -- some moved to a new one, another was already
        # current or unrecoverable and stayed put -- and a comment row records
        # nothing that would tell them apart. They keep their old hash, which
        # costs them signature-based lookups but never files them under a
        # failure nobody commented on.
        self.ambiguous_comment_rows = 0
        self.ambiguous_comments: list[dict[str, Any]] = []
        self.migration_preempted = False
        # Jobs the scan had to leave alone because their analysis was still
        # running. Their signatures stay stale until that analysis saves -- or,
        # if it fails and restores the job, until a later run reaches them -- so
        # a run that leaves any behind has not finished the version.
        self.deferred_jobs: list[str] = []

    def record_unrecoverable(self, record: dict[str, Any]) -> None:
        """Count one unrecoverable failure, keeping a bounded sample of them."""
        self.unrecoverable_count += 1
        if len(self.unrecoverable_failures) < UNRECOVERABLE_SAMPLE_SIZE:
            self.unrecoverable_failures.append(record)
        else:
            logger.debug("Unrecoverable signature inputs (not sampled): %s", record)

    def record_ambiguous_comments(self, job_id: str, rows: int) -> None:
        """Count comment rows left on a stale hash, keeping a bounded sample."""
        self.ambiguous_comment_rows += rows
        record = {"job_id": job_id, "comment_rows": rows}
        if len(self.ambiguous_comments) < AMBIGUOUS_COMMENT_SAMPLE_SIZE:
            self.ambiguous_comments.append(record)
        else:
            logger.debug("Ambiguous comment rows (not sampled): %s", record)

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
            "unrecoverable_failures_count": self.unrecoverable_count,
            "unrecoverable_failures": self.unrecoverable_failures,
            "ambiguous_comment_rows": self.ambiguous_comment_rows,
            "ambiguous_comments": self.ambiguous_comments,
            "migration_preempted": self.migration_preempted,
            "jobs_deferred": len(self.deferred_jobs),
            "deferred_jobs": self.deferred_jobs,
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


def _signature_update(
    failure: dict[str, Any],
    child_name: str,
    child_build: int,
    previous_signature: str,
    new_signature: str,
) -> SignatureUpdate:
    """Build the denormalized update describing one stored failure."""
    return SignatureUpdate(
        test_name=failure.get("test_name", ""),
        child_job_name=child_name,
        child_build_number=child_build,
        error_message=failure_error_text(failure),
        previous_signature=previous_signature,
        new_signature=new_signature,
    )


def _rehash_stored_failures(
    result_data: dict[str, Any],
    *,
    collect: Callable[[SignatureUpdate], None] | None = None,
    retain: Callable[[SignatureUpdate], None] | None = None,
    stats: BackfillStats | None = None,
    job_id: str = "",
) -> int:
    """Recompute every stored failure signature in *result_data*, in place.

    Args:
        result_data: Parsed ``result_json`` for one job.
        collect: Called once per failure whose signature changes, with the
            matching ``failure_history`` / ``comments`` update. Only needed when
            writing; a dry run only needs the counts.
        retain: Called once per failure that *keeps* its signature -- already
            current under the new rules, or unrecoverable (see
            :func:`signature_inputs`) -- as an update whose ``new_signature`` is
            the hash it already holds. Such a failure rewrites no row, but it
            still owns the comment rows on that hash, and the storage layer
            needs it to tell a comment it may move from one it may not.
        stats: Collects the scan counts and the unrecoverable-record report.
        job_id: Reported alongside unrecoverable records.

    Returns:
        Number of failures whose signature changed. The caller decides whether
        that counts as a rewrite: in apply mode the pre-check result can still
        be thrown away by the write (a job that went in flight, or that another
        writer already fixed), so only the counts taken inside the committed
        transaction are reported as changed.
    """
    changed = 0
    for failure, child_name, child_build in iter_stored_failures(result_data):
        if stats is not None:
            stats.failures_scanned += 1
        previous_signature = failure.get("error_signature", "")
        inputs = signature_inputs(failure)
        if inputs is None:
            if stats is not None:
                stats.record_unrecoverable(
                    {
                        "job_id": job_id,
                        "test_name": failure.get("test_name", ""),
                        "child_job_name": child_name,
                        "child_build_number": child_build,
                        "error_signature": previous_signature,
                    }
                )
            # An unrecoverable row keeps the hash it has, so it still owns the
            # comments written against it.
            new_signature = previous_signature
        else:
            new_signature = compute_signature(*inputs)
        if new_signature == previous_signature:
            if retain is not None:
                retain(
                    _signature_update(
                        failure,
                        child_name,
                        child_build,
                        previous_signature,
                        new_signature,
                    )
                )
            continue
        failure["error_signature"] = new_signature
        changed += 1
        if collect is not None:
            collect(
                _signature_update(
                    failure, child_name, child_build, previous_signature, new_signature
                )
            )
    return changed


def _make_updater(
    collect: list[SignatureUpdate],
    retain: list[SignatureUpdate],
) -> Callable[[dict[str, Any]], None]:
    """Return a ``patch_result_json`` callback that re-hashes *only* signatures.

    Runs against the row's current contents inside ``patch_result_json``'s write
    transaction, never against the copy the scan saw: a job that analysis or
    reanalysis updated in the meantime keeps its newer fields, and only the
    signature changes are written. A factory rather than a lambda so the values
    are bound, not the loop variables (ruff B023).

    Args:
        collect: Collects the matching denormalized updates for this job.
        retain: Collects the failures that keep their signature, so the storage
            layer can see which comment rows they still own.
    """

    def _update(data: dict[str, Any]) -> None:
        _rehash_stored_failures(data, collect=collect.append, retain=retain.append)

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

    Jobs whose analysis was still running are deferred rather than counted as
    done, and the version is left unapplied when any are -- otherwise a version
    could be marked applied while stale rows it never touched sit behind a
    running job, and no later startup would revisit them.
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
    if stats["jobs_deferred"]:
        # Leave the version unapplied so the next startup runs the backfill
        # again and picks up the jobs that were in flight here.
        logger.warning(
            "Signature backfill for %s left %s job(s) to a running analysis; "
            "not recording the version as applied, they are retried next start",
            version,
            stats["jobs_deferred"],
        )
        return stats
    await mark_migration_applied(MIGRATION_KEY_PREFIX + version)
    if not await complete_signature_migration(version):
        logger.warning(
            "Signature version %s was taken over before it could be recorded",
            version,
        )
    logger.info(
        "Signature backfill complete: %s/%s failures re-hashed across %s jobs "
        "(%s failures kept their hash -- unrecoverable signature inputs; "
        "sample of %s reported)",
        stats["failures_changed"],
        stats["failures_scanned"],
        stats["jobs_scanned"],
        stats["unrecoverable_failures_count"],
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
    interrupt and safe to retry. Jobs whose analysis is still running are left
    alone: that analysis writes current-rule signatures itself when it saves,
    and a backfill landing between its history and result writes would only
    race it.

    Args:
        dry_run: When True (the default) nothing is written and the returned
            counts describe what an apply would change -- including the jobs an
            apply defers, which are reported as deferred and not as changed.
            The ``*_rows`` counters are not previewed: how many
            ``failure_history`` / ``comments`` rows a write touches (and which
            comment rows stay ambiguous) is only known inside its transaction.
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
        for job_id, raw_json, status in batch:
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

            # Probe the scanned copy first: patch_result_json opens
            # BEGIN IMMEDIATE before it can tell that nothing changed, and most
            # rows are already current or unrecoverable. Only a job the probe
            # finds stale is worth the write lock -- or, in a dry run, worth
            # counting.
            changed = _rehash_stored_failures(result_data, stats=stats, job_id=job_id)
            if not changed:
                continue

            if dry_run:
                # Same skip the apply below makes, and for the same reason (see
                # patch_result_json's ``skip_in_flight``): a running analysis
                # writes its own current-rule signatures when it saves. Counted
                # as deferred, not as a rewrite, so the preview matches what the
                # apply it stands in for would do.
                if analysis_in_flight(status):
                    stats.deferred_jobs.append(job_id)
                else:
                    stats.jobs_changed += 1
                    stats.failures_changed += changed
                continue

            updates: list[SignatureUpdate] = []
            retained: list[SignatureUpdate] = []
            outcome = await patch_result_json(
                job_id,
                _make_updater(updates, retained),
                denormalized=updates,
                retained=retained,
                write_if_changed=True,
                skip_in_flight=True,
            )
            if outcome.written:
                stats.jobs_changed += 1
                # Counted from the committed transaction, not from the probe
                # above: this is what the result blob actually had rewritten.
                stats.failures_changed += len(updates)
            elif outcome.in_flight:
                stats.deferred_jobs.append(job_id)
            stats.history_rows_changed += outcome.history_rows
            stats.comment_rows_changed += outcome.comment_rows
            if outcome.comment_rows_ambiguous:
                stats.record_ambiguous_comments(job_id, outcome.comment_rows_ambiguous)
        # Yield between batches so live requests interleave with the scan
        # instead of queueing behind one long uninterrupted run.
        await asyncio.sleep(0)

    result = stats.as_dict()
    result["dry_run"] = dry_run
    logger.info(
        "Signature backfill (dry_run=%s): %s jobs scanned, %s failures changed, "
        "%s history rows, %s comment rows, %s comment rows left ambiguous, "
        "%s jobs deferred (analysis running)",
        dry_run,
        stats.jobs_scanned,
        stats.failures_changed,
        stats.history_rows_changed,
        stats.comment_rows_changed,
        stats.ambiguous_comment_rows,
        len(stats.deferred_jobs),
    )
    return result
