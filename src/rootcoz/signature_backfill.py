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

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

from rootcoz.engine.core import compute_signature
from rootcoz.storage import (
    list_all_result_json,
    patch_result_json,
    update_denormalized_signatures,
)

logger = logging.getLogger(__name__)


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
        self.failures_missing_input = 0

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
            "failures_missing_input": self.failures_missing_input,
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


def _make_updater(source: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    """Return a ``patch_result_json`` callback that copies *source* into *data*.

    A factory rather than an inline lambda so the value is bound, not the loop
    variable (ruff B023), and so the callback type is inferable by mypy.
    """

    def _update(data: dict[str, Any]) -> None:
        data.update(source)

    return _update


async def backfill_signatures(*, dry_run: bool = True) -> dict[str, Any]:
    """Recompute and rewrite stored failure signatures.

    Args:
        dry_run: When True (the default) nothing is written and the returned
            counts describe what an apply would change.

    Returns:
        Stats dict (see :meth:`BackfillStats.as_dict`).
    """
    stats = BackfillStats()
    denormalized: dict[tuple[str, str, str, int], str] = {}

    for job_id, raw_json in await list_all_result_json():
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

        job_changed = False
        for failure, child_name, child_build in iter_stored_failures(result_data):
            stats.failures_scanned += 1
            error_message = failure.get("error_message")
            stack_trace = failure.get("stack_trace")
            if not isinstance(error_message, str) or not isinstance(stack_trace, str):
                # Legacy rows may lack the fields; leave the stored hash alone
                # rather than inventing one that cannot be reproduced.
                stats.failures_missing_input += 1
                continue
            new_signature = compute_signature(error_message, stack_trace)
            if new_signature == failure.get("error_signature"):
                continue
            failure["error_signature"] = new_signature
            job_changed = True
            stats.failures_changed += 1
            denormalized[
                (job_id, failure.get("test_name", ""), child_name, child_build)
            ] = new_signature

        if job_changed:
            stats.jobs_changed += 1
            if not dry_run:
                await patch_result_json(job_id, _make_updater(result_data))

    if not dry_run and denormalized:
        history, comments = await update_denormalized_signatures(denormalized)
        stats.history_rows_changed = history
        stats.comment_rows_changed = comments

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
