"""Sparse field selection for GET /results/{job_id}.

Field paths use dots for nesting. ``result.failures.<field>`` projects each
failure object to the requested subfields. Classification/pattern/details/
affected_tests/artifacts_evidence may live under ``analysis``; those paths
resolve from either the failure root or ``analysis`` when present.
"""

from __future__ import annotations

from typing import Any

# Allowlist of selectable field paths for sparse GET /results/{job_id}.
RESULT_FIELD_PATHS: frozenset[str] = frozenset(
    {
        # Top-level response keys
        "job_id",
        "jenkins_url",
        "status",
        "error",
        "created_at",
        "completed_at",
        "analysis_started_at",
        "capabilities",
        "tracked_in",
        "base_url",
        "result_url",
        "reanalyzed_from_job_id",
        "origin_job_name",
        "result",
        # Nested under result
        "result.job_id",
        "result.job_name",
        "result.build_number",
        "result.jenkins_url",
        "result.status",
        "result.summary",
        "result.ai_provider",
        "result.ai_model",
        "result.failures",
        "result.child_job_analyses",
        "result.child_job_analyses.passed_count",
        "result.child_job_analyses.skipped_count",
        "result.child_job_analyses.failed_count",
        "result.token_usage",
        "result.passed_count",
        "result.skipped_count",
        "result.failed_count",
        "result.request_params",
        "result.display_name",
        # Per-failure projections (applied to each item in result.failures)
        "result.failures.id",
        "result.failures.test_name",
        "result.failures.error",
        "result.failures.error_signature",
        "result.failures.analysis",
        "result.failures.classification",
        "result.failures.pattern",
        "result.failures.details",
        "result.failures.affected_tests",
        "result.failures.artifacts_evidence",
        "result.failures.peer_debate",
    }
)

# Failure keys that may be nested under analysis.*
_FAILURE_ANALYSIS_ALIASES = frozenset(
    {
        "classification",
        "pattern",
        "details",
        "affected_tests",
        "artifacts_evidence",
    }
)


def parse_fields_param(fields: str | None) -> list[str] | None:
    """Parse a comma-separated fields query value.

    Returns None when fields is omitted/empty (full response).
    Raises ValueError with a clear message when unknown paths are present.
    """
    if fields is None:
        return None
    raw = fields.strip()
    if not raw:
        return None
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    if not requested:
        return None
    unknown = sorted({p for p in requested if p not in RESULT_FIELD_PATHS})
    if unknown:
        raise ValueError(
            "Unknown field(s): "
            + ", ".join(unknown)
            + ". Use GET /api/results/fields for the allowlist."
        )
    # Preserve order, drop duplicates
    seen: set[str] = set()
    ordered: list[str] = []
    for path in requested:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


_MISSING = object()


def _set_nested(out: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur: dict[str, Any] = out
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _get_nested(data: dict[str, Any], path: str) -> Any:
    """Return nested value, or ``_MISSING`` when the path is absent.

    Distinguishes missing keys from present ``None`` so sparse field
    selection can include explicit nulls.
    """
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _project_failure(failure: dict[str, Any], subfields: list[str]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    analysis = failure.get("analysis")
    analysis_dict = analysis if isinstance(analysis, dict) else {}
    for key in subfields:
        if key in failure:
            projected[key] = failure[key]
        elif key in _FAILURE_ANALYSIS_ALIASES and key in analysis_dict:
            projected[key] = analysis_dict[key]
    return projected


def filter_result_fields(data: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Return a new dict containing only the requested allowlisted field paths.

    When ``result.failures`` is requested, the full failures list is included.
    When only ``result.failures.<subfield>`` paths are requested, each failure
    is projected to those subfields (full values, never truncated).
    Present-but-null nested values are included; missing paths are omitted.
    """
    out: dict[str, Any] = {}
    field_set = set(fields)

    # Whole result object short-circuits nested result.* selection
    if "result" in field_set:
        if "result" in data:
            out["result"] = data["result"]
        # Still copy other top-level selections
        for path in fields:
            if path == "result" or path.startswith("result."):
                continue
            if "." in path:
                val = _get_nested(data, path)
                if val is not _MISSING:
                    _set_nested(out, path, val)
            elif path in data:
                out[path] = data[path]
        return out

    failure_subfields = [
        p.removeprefix("result.failures.")
        for p in fields
        if p.startswith("result.failures.") and p != "result.failures"
    ]
    want_full_failures = "result.failures" in field_set

    for path in fields:
        if path.startswith("result.failures."):
            continue  # handled via projection below
        if path == "result.failures":
            continue  # handled below
        if "." in path:
            val = _get_nested(data, path)
            if val is not _MISSING:
                _set_nested(out, path, val)
        elif path in data:
            out[path] = data[path]

    result_obj = data.get("result")
    if isinstance(result_obj, dict) and (want_full_failures or failure_subfields):
        result_out = out.setdefault("result", {})
        if not isinstance(result_out, dict):
            result_out = {}
            out["result"] = result_out
        failures = result_obj.get("failures")
        if isinstance(failures, list):
            if want_full_failures:
                result_out["failures"] = failures
            else:
                result_out["failures"] = [
                    _project_failure(f, failure_subfields) if isinstance(f, dict) else f
                    for f in failures
                ]

    return out
