"""Canonical backend contract for job filtering, filter presets, and SQL compilation.

Owns the single source of truth for:
- The allowlisted job filter keys (shared by Dashboard, Reports, and Presets).
- Query and preset parsing with backwards-compatible legacy alias normalization
  (``from``/``to`` -> ``date_from``/``date_to``, ``tags``/``exclude_tags`` ->
  ``label``/``exclude_label``, repeatable and CSV list shapes, ``review_status="all"``).
- Shared SQL predicate compilation (including #658 facet-grouped OR-within /
  AND-across label predicates) consumed by Dashboard listing, Reports queries,
  and metadata listing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from rootcoz.tag_facets import TagFacet, partition_labels_by_facets

# ─── Canonical constants & allowlists ───────────────────────────────────────

FILTER_PRESET_PAGES: tuple[str, ...] = ("dashboard", "reports")

# Canonical 14 dimensions matching frontend/src/lib/jobFilters.ts
# (JOB_FILTER_KEYS + TEST_NAME_FILTER_KEYS).
CANONICAL_JOB_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "search",
        "team",
        "tier",
        "version",
        "label",
        "exclude_label",
        "status",
        "review_status",
        "analysis_state",
        "submitted_by",
        "date_from",
        "date_to",
        "test_name",
        "test_name_mode",
    }
)

# Read-only legacy aliases mapped to canonical names.
LEGACY_FILTER_KEY_ALIASES: dict[str, str] = {
    "from": "date_from",
    "to": "date_to",
    "tags": "label",
    "exclude_tags": "exclude_label",
}

# URL query param names each page's presets may carry (validated on save).
# Legacy date keys stay accepted on reports so presets saved before the unified
# contract still replay; the UI writes the canonical pair.
FILTER_PRESET_KEYS: dict[str, frozenset[str]] = {
    "dashboard": CANONICAL_JOB_FILTER_KEYS,
    "reports": CANONICAL_JOB_FILTER_KEYS | frozenset({"from", "to"}),
}

# The four test-name match modes (#546). Literal text in every one: ``*``/``?``
# are not metacharacters here and are never interpreted.
TEST_NAME_MODES: tuple[str, ...] = ("contains", "exact", "starts_with", "ends_with")
DEFAULT_TEST_NAME_MODE: str = "contains"

# Longest stored test name measured on the reporter's dataset is 405 characters;
# 546 distinct failing names exceed 200, none exceed 1000. This equals
# models.MAX_FILTER_PRESET_VALUE_LENGTH (pinned by a test) so a name the filter
# accepts can also be bookmarked.
MAX_TEST_NAME_LENGTH: int = 1000

MAX_SUBMITTED_BY_COUNT: int = 50
# The filtered endpoint caps one submitted_by value at this many characters;
# storage also drops over-long historical values from the picker.
MAX_SUBMITTED_BY_LENGTH: int = 64
MAX_CSV_FILTER_LENGTH: int = 2000

ANALYSIS_STATE_SUBMITTED: Literal["submitted"] = "submitted"
ANALYSIS_STATE_ANALYZED: Literal["analyzed"] = "analyzed"
VALID_ANALYSIS_STATES: frozenset[str] = frozenset(
    {ANALYSIS_STATE_SUBMITTED, ANALYSIS_STATE_ANALYZED}
)
VALID_REVIEW_STATUSES: frozenset[str] = frozenset(
    {"", "all", "reviewed", "not_reviewed"}
)


# ─── Canonical model & parsing / validation ─────────────────────────────────


def parse_filter_list(
    values: str | Sequence[str] | None,
    *,
    split_csv: bool = True,
    max_length: int = MAX_CSV_FILTER_LENGTH,
) -> tuple[str, ...]:
    """Normalize a repeatable and/or comma-separated filter value into unique strings.

    Accepts ``None``, a bare string (optionally CSV), or a sequence of strings
    (each element optionally CSV when *split_csv* is True). Trims whitespace,
    drops empty entries, and deduplicates while preserving order.
    """
    if not values:
        return ()
    raw_items: Sequence[str] = (values,) if isinstance(values, str) else values
    collected: list[str] = []
    for raw in raw_items:
        if not isinstance(raw, str):
            raw = str(raw)
        if len(raw) > max_length:
            raise ValueError(
                f"Filter value too long ({len(raw)} chars, max {max_length})"
            )
        parts = raw.split(",") if split_csv else (raw,)
        for part in parts:
            cleaned = part.strip()
            if cleaned:
                collected.append(cleaned)
    return tuple(dict.fromkeys(collected))


def normalize_submitters(values: str | Sequence[str] | None) -> frozenset[str]:
    """Normalize ``submitted_by`` filter values; empty means no filter.

    Values are trimmed and deduplicated but never lowercased and never split on
    commas: the case fold happens in SQL (ASCII ``lower()``), and usernames
    legitimately contain commas, ``+``, spaces, and non-ASCII characters.
    """
    if not values:
        return frozenset()
    raw_items: Sequence[str] = (values,) if isinstance(values, str) else values
    cleaned = [str(value).strip() for value in raw_items if str(value).strip()]
    if len(cleaned) > MAX_SUBMITTED_BY_COUNT:
        raise ValueError(
            f"Too many submitted_by values ({len(cleaned)}, max {MAX_SUBMITTED_BY_COUNT})"
        )
    too_long = [value for value in cleaned if len(value) > MAX_SUBMITTED_BY_LENGTH]
    if too_long:
        raise ValueError(
            f"submitted_by value too long ({len(too_long[0])} chars, "
            f"max {MAX_SUBMITTED_BY_LENGTH})"
        )
    return frozenset(cleaned)


def normalize_review_status(value: str | None) -> str:
    """Validate and normalize ``review_status`` (``""`` or ``"all"`` -> ``""``)."""
    cleaned = (value or "").strip()
    if cleaned not in VALID_REVIEW_STATUSES:
        raise ValueError(
            f"Invalid review_status '{cleaned}'. "
            "Must be one of: all, not_reviewed, reviewed"
        )
    return "" if cleaned == "all" else cleaned


def normalize_analysis_state(value: str | None) -> str:
    """Validate and normalize ``analysis_state`` (``""`` or ``"all"`` -> ``""``)."""
    cleaned = (value or "").strip()
    if cleaned in ("", "all"):
        return ""
    if cleaned not in VALID_ANALYSIS_STATES:
        raise ValueError(
            f"Invalid analysis_state '{cleaned}'. "
            f"Must be {', '.join(sorted(VALID_ANALYSIS_STATES))}."
        )
    return cleaned


def validate_test_name_mode(mode: str | None) -> str:
    """Validate ``test_name_mode`` against ``TEST_NAME_MODES``."""
    resolved = mode if mode is not None and mode != "" else DEFAULT_TEST_NAME_MODE
    if resolved not in TEST_NAME_MODES:
        raise ValueError(
            f"Invalid test_name_mode '{resolved}'. "
            f"Must be one of: {', '.join(TEST_NAME_MODES)}."
        )
    return resolved


@dataclass(frozen=True, slots=True)
class JobFilters:
    """Canonical, validated job filter contract shared across dashboard & reports."""

    team: tuple[str, ...] = ()
    tier: tuple[str, ...] = ()
    version: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    search: str = ""
    status: tuple[str, ...] = ()
    date_from: str = ""
    date_to: str = ""
    review_status: str = ""
    analysis_state: str = ""
    submitters: frozenset[str] = frozenset()
    test_name: str = ""
    test_name_mode: str = DEFAULT_TEST_NAME_MODE

    @classmethod
    def from_values(
        cls,
        *,
        team: str | Sequence[str] | None = None,
        tier: str | Sequence[str] | None = None,
        version: str | Sequence[str] | None = None,
        labels: str | Sequence[str] | None = None,
        exclude_labels: str | Sequence[str] | None = None,
        search: str = "",
        status: str | Sequence[str] | None = None,
        date_from: str = "",
        date_to: str = "",
        review_status: str = "",
        analysis_state: str = "",
        submitters: str | Sequence[str] | set[str] | frozenset[str] | None = None,
        test_name: str = "",
        test_name_mode: str = DEFAULT_TEST_NAME_MODE,
        split_csv: bool = True,
    ) -> JobFilters:
        """Build a validated ``JobFilters`` from Python-level arguments."""
        normalized_submitters = (
            normalize_submitters(sorted(submitters))
            if isinstance(submitters, (set, frozenset))
            else normalize_submitters(submitters)
        )
        return cls(
            team=parse_filter_list(team, split_csv=split_csv),
            tier=parse_filter_list(tier, split_csv=split_csv),
            version=parse_filter_list(version, split_csv=split_csv),
            labels=parse_filter_list(labels, split_csv=split_csv),
            exclude_labels=parse_filter_list(exclude_labels, split_csv=split_csv),
            search=search or "",
            status=parse_filter_list(status, split_csv=split_csv),
            date_from=(date_from or "").strip(),
            date_to=(date_to or "").strip(),
            review_status=normalize_review_status(review_status),
            analysis_state=normalize_analysis_state(analysis_state),
            submitters=normalized_submitters,
            test_name=test_name or "",
            test_name_mode=validate_test_name_mode(test_name_mode),
        )


def parse_job_filters_query(
    *,
    team: str | Sequence[str] | None = None,
    tier: str | Sequence[str] | None = None,
    version: str | Sequence[str] | None = None,
    label: str | Sequence[str] | None = None,
    exclude_label: str | Sequence[str] | None = None,
    tags: str | Sequence[str] | None = None,
    exclude_tags: str | Sequence[str] | None = None,
    search: str = "",
    status: str | Sequence[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    legacy_from: str = "",
    legacy_to: str = "",
    review_status: str = "",
    analysis_state: str = "",
    submitted_by: str | Sequence[str] | None = None,
    test_name: str = "",
    test_name_mode: str = DEFAULT_TEST_NAME_MODE,
) -> JobFilters:
    """Parse HTTP query parameters into canonical ``JobFilters``.

    Accepts both repeatable lists and CSV strings for list-valued fields
    (except ``submitted_by``, which is never split on commas), and resolves
    legacy aliases (``from``/``to``, ``tags``/``exclude_tags``) with canonical
    names taking precedence whenever both are supplied.
    """
    canonical_labels = parse_filter_list(label)
    legacy_labels = parse_filter_list(tags)
    canonical_exclude = parse_filter_list(exclude_label)
    legacy_exclude = parse_filter_list(exclude_tags)

    return JobFilters.from_values(
        team=team,
        tier=tier,
        version=version,
        labels=canonical_labels or legacy_labels,
        exclude_labels=canonical_exclude or legacy_exclude,
        search=search,
        status=status,
        date_from=date_from or legacy_from,
        date_to=date_to or legacy_to,
        review_status=review_status,
        analysis_state=analysis_state,
        submitters=submitted_by,
        test_name=test_name,
        test_name_mode=test_name_mode,
    )


def validate_filter_preset_filters(page: str, filters: Mapping[str, Any]) -> None:
    """Validate saved preset filters for *page*, raising ``ValueError`` on error.

    Enforces the canonical per-page key allowlist (never silently dropping
    unknown keys), scalar ``search`` type, valid ``test_name_mode``, and the
    same ``submitted_by`` count/length caps enforced by query endpoints.
    """
    allowed = FILTER_PRESET_KEYS.get(page)
    if allowed is None:
        raise ValueError(f"page must be one of: {', '.join(FILTER_PRESET_PAGES)}")
    unknown = set(filters) - allowed
    if unknown:
        raise ValueError(
            f"Unknown filter key(s) for {page}: " + ", ".join(sorted(unknown))
        )
    search = filters.get("search")
    if search is not None and not isinstance(search, str):
        raise ValueError("Filter 'search' must be a string")
    test_name_mode = filters.get("test_name_mode")
    if test_name_mode is not None and test_name_mode not in TEST_NAME_MODES:
        raise ValueError(
            f"Invalid test_name_mode {test_name_mode!r}. "
            f"Must be one of: {', '.join(TEST_NAME_MODES)}"
        )
    submitters = filters.get("submitted_by")
    if submitters is not None:
        values = [submitters] if isinstance(submitters, str) else list(submitters)
        normalize_submitters([str(value) for value in values])


def parse_filter_preset(page: str, filters: Mapping[str, Any]) -> JobFilters:
    """Validate a preset filter map and normalize it into ``JobFilters``."""
    validate_filter_preset_filters(page, filters)
    raw_review = filters.get("review_status", "")
    if isinstance(raw_review, list):
        raw_review = raw_review[0] if raw_review else ""
    # Legacy presets created before #546 occasionally stored "unreviewed".
    if raw_review == "unreviewed":
        raw_review = "not_reviewed"
    raw_analysis = filters.get("analysis_state", "")
    if isinstance(raw_analysis, list):
        raw_analysis = raw_analysis[0] if raw_analysis else ""
    raw_test_name = filters.get("test_name", "")
    if isinstance(raw_test_name, list):
        raw_test_name = raw_test_name[0] if raw_test_name else ""
    raw_mode = filters.get("test_name_mode", DEFAULT_TEST_NAME_MODE)
    if isinstance(raw_mode, list):
        raw_mode = raw_mode[0] if raw_mode else DEFAULT_TEST_NAME_MODE

    def _scalar(key: str) -> str:
        val = filters.get(key, "")
        if isinstance(val, list):
            return str(val[0]) if val else ""
        return str(val) if val is not None else ""

    return parse_job_filters_query(
        team=filters.get("team"),
        tier=filters.get("tier"),
        version=filters.get("version"),
        label=filters.get("label"),
        exclude_label=filters.get("exclude_label"),
        search=_scalar("search"),
        status=filters.get("status"),
        date_from=_scalar("date_from"),
        date_to=_scalar("date_to"),
        legacy_from=_scalar("from"),
        legacy_to=_scalar("to"),
        review_status=str(raw_review or ""),
        analysis_state=str(raw_analysis or ""),
        submitted_by=filters.get("submitted_by"),
        test_name=str(raw_test_name or ""),
        test_name_mode=str(raw_mode or DEFAULT_TEST_NAME_MODE),
    )


# ─── Shared SQL predicate compilation ───────────────────────────────────────


def like_escape(fragment: str) -> str:
    """Escape SQLite LIKE metacharacters so *fragment* matches literally."""
    for char in ("\\", "%", "_"):
        fragment = fragment.replace(char, f"\\{char}")
    return fragment


def normalize_filter_datetime(value: str) -> str:
    """Normalize an ISO datetime string for SQLite text comparison.

    Parses offset-aware datetimes and converts to UTC so absolute-time
    semantics are preserved. Strips fractional seconds and formats the
    result as ``YYYY-MM-DD HH:MM:SS`` to match SQLite's ``CURRENT_TIMESTAMP``.
    """
    stripped = value.strip()
    try:
        dt = datetime.fromisoformat(stripped)
    except ValueError:
        normalized = stripped.replace("T", " ")
        normalized = re.sub(r"\.\d+", "", normalized)
        return normalized.strip()

    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)

    return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def build_date_filter(
    column: str,
    date_from: str,
    date_to: str,
    conditions: list[str],
    params: list[Any],
) -> None:
    """Append date-range conditions and params in-place.

    Date-only values (``YYYY-MM-DD``) are used as-is for ``date_from``;
    for ``date_to``, ``" 23:59:59"`` is appended to make the range inclusive
    of the entire day. Values containing a time component (``T`` or space)
    are normalized to ``YYYY-MM-DD HH:MM:SS`` (UTC for offset-aware inputs).
    """
    if date_from:
        conditions.append(f"{column} >= ?")
        if "T" in date_from or " " in date_from.strip():
            params.append(normalize_filter_datetime(date_from))
        else:
            params.append(date_from)
    if date_to:
        conditions.append(f"{column} <= ?")
        if "T" in date_to or " " in date_to.strip():
            params.append(normalize_filter_datetime(date_to))
        else:
            params.append(f"{date_to} 23:59:59")


def build_metadata_join(
    team: str | Sequence[str] | None,
    tier: str | Sequence[str] | None,
    version: str | Sequence[str] | None,
    job_name_col: str,
    conditions: list[str],
    params: list[Any],
) -> str:
    """Return a JOIN clause for ``job_metadata`` filtering and append predicates.

    Returns an empty string when no metadata filters are active.
    """
    team_list = parse_filter_list(team, split_csv=False)
    tier_list = parse_filter_list(tier, split_csv=False)
    version_list = parse_filter_list(version, split_csv=False)
    if not (team_list or tier_list or version_list):
        return ""
    join_sql = f" JOIN job_metadata jm ON jm.job_name = {job_name_col}"
    if team_list:
        placeholders = ", ".join("?" for _ in team_list)
        conditions.append(f"jm.team IN ({placeholders})")
        params.extend(team_list)
    if tier_list:
        placeholders = ", ".join("?" for _ in tier_list)
        conditions.append(f"jm.tier IN ({placeholders})")
        params.extend(tier_list)
    if version_list:
        placeholders = ", ".join("?" for _ in version_list)
        conditions.append(f"jm.version IN ({placeholders})")
        params.extend(version_list)
    return join_sql


def build_labels_subquery(labels: Sequence[str]) -> str:
    """Return the ``job_metadata_labels`` subquery for a sequence of labels."""
    placeholders = ", ".join("?" for _ in labels)
    return (
        "SELECT jml.job_name FROM job_metadata_labels jml "
        f"WHERE jml.label IN ({placeholders})"
    )


def build_labels_filter(
    labels: Sequence[str] | None,
    job_name_col: str,
    conditions: list[str],
    params: list[Any],
    *,
    tag_facets: Sequence[TagFacet] = (),
    ungrouped_mode: Literal["and", "or"] = "and",
) -> None:
    """Append facet-grouped label-filtering conditions in-place.

    Tags in the same configured facet combine with OR; distinct facets combine
    with AND; tags outside any facet combine according to *ungrouped_mode*
    (``"and"`` for Dashboard, ``"or"`` for Reports).
    """
    if not labels:
        return
    for group in partition_labels_by_facets(
        labels, tag_facets, ungrouped_mode=ungrouped_mode
    ):
        conditions.append(f"{job_name_col} IN ({build_labels_subquery(group)})")
        params.extend(group)


def build_exclude_labels_filter(
    exclude_labels: Sequence[str] | None,
    job_name_col: str,
    conditions: list[str],
    params: list[Any],
) -> None:
    """Append label-exclusion conditions in-place (OR semantics across excluded labels).

    Rows with NULL or empty *job_name_col* pass through — they have no label
    associations and must never be dropped by an exclusion filter.
    """
    if not exclude_labels:
        return
    conditions.append(
        f"({job_name_col} IS NULL OR {job_name_col} NOT IN ({build_labels_subquery(exclude_labels)}))"
    )
    params.extend(exclude_labels)


def build_status_predicate(
    statuses: Sequence[str] | None, *, results_alias: str = "r"
) -> tuple[str, list[Any]]:
    """Compile status filter, translating virtual ``timeout`` to its SQL predicate."""
    if not statuses:
        return "", []
    real_statuses = [s for s in statuses if s != "timeout"]
    has_timeout = "timeout" in statuses
    status_parts: list[str] = []
    params: list[Any] = []
    if real_statuses:
        placeholders = ", ".join("?" for _ in real_statuses)
        status_parts.append(f"{results_alias}.status IN ({placeholders})")
        params.extend(real_statuses)
    if has_timeout:
        status_parts.append(
            f"({results_alias}.status = 'failed' AND ("
            f"lower({results_alias}.error) LIKE '%timed out%' OR "
            f"lower({results_alias}.error) LIKE '%timeout%' OR "
            f"lower({results_alias}.summary) LIKE '%timed out%' OR "
            f"lower({results_alias}.summary) LIKE '%timeout%'"
            "))"
        )
    if len(status_parts) == 1:
        return status_parts[0], params
    return f"({' OR '.join(status_parts)})", params


def build_review_status_predicate(
    review_status: str, *, job_id_col: str = "r.job_id"
) -> str:
    """Compile review_status into an EXISTS / NOT EXISTS clause (or empty string)."""
    normalized = normalize_review_status(review_status)
    if normalized == "reviewed":
        return (
            "EXISTS (SELECT 1 FROM failure_reviews fr_rs "
            f"WHERE fr_rs.job_id = {job_id_col} AND fr_rs.reviewed = 1)"
        )
    if normalized == "not_reviewed":
        return (
            "NOT EXISTS (SELECT 1 FROM failure_reviews fr_rs "
            f"WHERE fr_rs.job_id = {job_id_col} AND fr_rs.reviewed = 1)"
        )
    return ""


def build_analysis_state_predicate(
    analysis_state: str, *, column: str = "r.analysis_state"
) -> tuple[str, list[Any]]:
    """SQL fragment and params for an optional ``analysis_state`` filter."""
    normalized = normalize_analysis_state(analysis_state)
    if not normalized:
        return "", []
    return f"COALESCE({column}, 'analyzed') = ?", [normalized]


def build_submitted_by_predicate(
    values: set[str] | frozenset[str], *, column: str = "submitted_by"
) -> tuple[str, list[str]]:
    """Build a LIKE-any predicate for ``submitted_by`` values, folded in SQL."""
    patterns = [f"%{like_escape(value.strip())}%" for value in sorted(values)]
    folded = f"lower(NULLIF({column}, ''))"
    clause = " OR ".join(f"{folded} LIKE lower(?) ESCAPE '\\'" for _ in patterns)
    return f"({clause})", patterns


def build_test_name_pattern(mode: str, text: str) -> str:
    """Build the literal LIKE pattern for one of ``TEST_NAME_MODES``."""
    if mode == "contains":
        return f"%{like_escape(text)}%"
    if mode == "starts_with":
        return f"{like_escape(text)}%"
    if mode == "ends_with":
        return f"%{like_escape(text)}"
    if mode == "exact":
        return like_escape(text)
    raise ValueError(
        f"Invalid test_name_mode '{mode}'. Must be {', '.join(TEST_NAME_MODES)}."
    )


def build_failed_test_name_predicate(
    mode: str, text: str, *, job_column: str = "r.job_id"
) -> tuple[str, list[str]]:
    """EXISTS clause selecting jobs with a **failing** test matching *text*."""
    if not text:
        return "", []
    pattern = build_test_name_pattern(mode, text)
    clause = (
        f"EXISTS (SELECT 1 FROM test_entries te WHERE te.job_id = {job_column}"
        " AND te.status = 'failed'"
        " AND lower(te.test_name) LIKE lower(?) ESCAPE '\\')"
    )
    return clause, [pattern]


@dataclass(frozen=True, slots=True)
class CompiledJobFilterSQL:
    """SQL joins, WHERE conditions, and bound parameters compiled from ``JobFilters``."""

    results_join: str
    meta_join: str
    conditions: list[str]
    params: list[Any]


def compile_job_filter_sql(
    filters: JobFilters,
    *,
    job_name_col: str = "r.job_name",
    job_id_col: str = "r.job_id",
    date_col: str = "r.created_at",
    results_alias: str = "r",
    results_join_col: str | None = None,
    results_join_mode: Literal[
        "none", "inner", "optional_inner", "left_or_inner"
    ] = "none",
    default_statuses: Sequence[str] = (),
    ungrouped_label_mode: Literal["and", "or"] = "and",
    tag_facets: Sequence[TagFacet] = (),
    include_test_name: bool = True,
) -> CompiledJobFilterSQL:
    """Compile a canonical ``JobFilters`` instance into SQL joins and WHERE clauses.

    Shared by Dashboard listing (``list_results_for_dashboard_filtered``) and
    all Reports queries (``get_report_totals``, ``_count_reviewed_tests``,
    ``get_report_classification_overrides``, ``get_report_issues_created``).
    """
    conditions: list[str] = []
    params: list[Any] = []

    effective_statuses: Sequence[str] = filters.status or default_statuses

    # Determine whether any predicate requires reading columns from `results`.
    job_name_uses_results = job_name_col.startswith(f"{results_alias}.")
    has_job_name_filters = bool(
        filters.team
        or filters.tier
        or filters.version
        or filters.labels
        or filters.exclude_labels
    )
    has_results_only_filters = bool(
        effective_statuses
        or filters.submitters
        or filters.analysis_state
        or filters.search
    )
    needs_results_join = has_results_only_filters or (
        job_name_uses_results and has_job_name_filters
    )

    results_join = ""
    if results_join_mode == "inner":
        if not results_join_col:
            raise ValueError(
                "results_join_col is required when results_join_mode != 'none'"
            )
        results_join = (
            f" JOIN results {results_alias} "
            f"ON {results_alias}.job_id = {results_join_col}"
        )
    elif results_join_mode == "optional_inner":
        if needs_results_join:
            if not results_join_col:
                raise ValueError(
                    "results_join_col is required when results_join_mode != 'none'"
                )
            results_join = (
                f" JOIN results {results_alias} "
                f"ON {results_alias}.job_id = {results_join_col}"
            )
    elif results_join_mode == "left_or_inner":
        if not results_join_col:
            raise ValueError(
                "results_join_col is required when results_join_mode != 'none'"
            )
        join_kw = "JOIN" if needs_results_join else "LEFT JOIN"
        results_join = (
            f" {join_kw} results {results_alias} "
            f"ON {results_alias}.job_id = {results_join_col}"
        )

    if filters.search:
        conditions.append(
            f"({results_alias}.job_id LIKE ? OR {results_alias}.job_name LIKE ?)"
        )
        like_param = f"%{filters.search}%"
        params.extend([like_param, like_param])

    status_clause, status_params = build_status_predicate(
        effective_statuses, results_alias=results_alias
    )
    if status_clause:
        conditions.append(status_clause)
        params.extend(status_params)

    build_date_filter(date_col, filters.date_from, filters.date_to, conditions, params)

    meta_join = build_metadata_join(
        filters.team,
        filters.tier,
        filters.version,
        job_name_col,
        conditions,
        params,
    )
    build_labels_filter(
        filters.labels,
        job_name_col,
        conditions,
        params,
        tag_facets=tag_facets,
        ungrouped_mode=ungrouped_label_mode,
    )
    build_exclude_labels_filter(
        filters.exclude_labels,
        job_name_col,
        conditions,
        params,
    )

    review_clause = build_review_status_predicate(
        filters.review_status, job_id_col=job_id_col
    )
    if review_clause:
        conditions.append(review_clause)

    analysis_clause, analysis_params = build_analysis_state_predicate(
        filters.analysis_state, column=f"{results_alias}.analysis_state"
    )
    if analysis_clause:
        conditions.append(analysis_clause)
        params.extend(analysis_params)

    if filters.submitters:
        sub_clause, sub_params = build_submitted_by_predicate(
            filters.submitters, column=f"{results_alias}.submitted_by"
        )
        conditions.append(sub_clause)
        params.extend(sub_params)

    if include_test_name and filters.test_name:
        test_clause, test_params = build_failed_test_name_predicate(
            filters.test_name_mode, filters.test_name, job_column=job_id_col
        )
        if test_clause:
            conditions.append(test_clause)
            params.extend(test_params)

    return CompiledJobFilterSQL(
        results_join=results_join,
        meta_join=meta_join,
        conditions=conditions,
        params=params,
    )
