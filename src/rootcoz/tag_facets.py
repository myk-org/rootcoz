"""Admin-defined tag facets: schema, validation, label partitioning, and audit diffing.

Facets are a presentation and grouping layer over existing ``job_metadata_labels``
tags (#658). Selections keep the existing flat ``label=`` URL contract:

- Tags belonging to the **same** facet combine with **OR**.
- Selections across **different** facets combine with **AND**.
- Tags outside any facet combine with **AND** (preserving existing behaviour).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from rootcoz import get_child_logger

logger = get_child_logger(__name__)

MAX_TAG_FACETS = 12
MAX_FACET_OPTIONS = 30
MAX_OPTION_TAGS = 20
MAX_FACET_NAME_LENGTH = 48
MAX_OPTION_LABEL_LENGTH = 64
FACET_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")


class TagFacetOption(BaseModel):
    """One selectable option inside a tag facet, mapping to one or more tags."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., description="Human-readable option label")
    tags: list[str] = Field(..., description="One or more job metadata tags (OR)")

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("option label must not be empty")
        if len(cleaned) > MAX_OPTION_LABEL_LENGTH:
            raise ValueError(
                f"option label must be at most {MAX_OPTION_LABEL_LENGTH} characters"
            )
        return cleaned

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("option must include at least one tag")
        deduped: list[str] = []
        seen: set[str] = set()
        for raw in value:
            tag = raw.strip()
            if not tag:
                raise ValueError("option tags must not contain empty strings")
            if tag not in seen:
                seen.add(tag)
                deduped.append(tag)
        if len(deduped) > MAX_OPTION_TAGS:
            raise ValueError(f"option may contain at most {MAX_OPTION_TAGS} tags")
        return deduped


class TagFacet(BaseModel):
    """An admin-defined named filter dimension over job tags."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique slug identifier (^[a-z0-9-]{1,32}$)")
    name: str = Field(..., description="Display name (max 48 characters)")
    options: list[TagFacetOption] = Field(
        ..., description="Ordered list of facet options (1..30)"
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not FACET_ID_RE.match(value):
            raise ValueError(f"facet id '{value}' must match {FACET_ID_RE.pattern}")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("facet name must not be empty")
        if len(cleaned) > MAX_FACET_NAME_LENGTH:
            raise ValueError(
                f"facet name must be at most {MAX_FACET_NAME_LENGTH} characters"
            )
        return cleaned

    @field_validator("options")
    @classmethod
    def _validate_options(cls, value: list[TagFacetOption]) -> list[TagFacetOption]:
        if not value:
            raise ValueError("facet must define at least one option")
        if len(value) > MAX_FACET_OPTIONS:
            raise ValueError(f"facet may define at most {MAX_FACET_OPTIONS} options")
        seen_labels: set[str] = set()
        for opt in value:
            key = opt.label.lower()
            if key in seen_labels:
                raise ValueError(f"duplicate option label '{opt.label}'")
            seen_labels.add(key)
        return value


class FilterFacetOptionResponse(BaseModel):
    """Per-option payload returned by ``GET /api/filters/facets``."""

    label: str
    tags: list[str]
    count: int
    missing_tags: list[str] = Field(default_factory=list)


class FilterFacetResponse(BaseModel):
    """One facet returned by ``GET /api/filters/facets``."""

    id: str
    name: str
    options: list[FilterFacetOptionResponse]


class FilterFacetsResponse(BaseModel):
    """Top-level response for ``GET /api/filters/facets``."""

    facets: list[FilterFacetResponse]


def _format_validation_error(exc: ValidationError) -> str:
    """Turn a Pydantic ValidationError into a concise, single-line message."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(segment) for segment in err.get("loc", ()))
        ctx_err = err.get("ctx", {}).get("error")
        msg = str(ctx_err) if ctx_err else err.get("msg", str(err))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def parse_tag_facets(
    raw: str | list[Any] | None, *, strict: bool = True
) -> list[TagFacet]:
    """Parse and validate ``tag_facets`` from a JSON string or list.

    When *strict* is False (runtime read path from env/DB), malformed data
    logs a warning and degrades to ``[]`` rather than crashing settings
    resolution. When *strict* is True (save / import validation), raises
    ``ValueError`` with a clear message.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(f"tag_facets must be valid JSON: {exc.msg}") from exc
            logger.warning("Ignoring invalid TAG_FACETS JSON: %s", exc.msg)
            return []
    elif isinstance(raw, list):
        data = raw
    else:
        if strict:
            raise ValueError("tag_facets must be a JSON array")
        logger.warning(
            "Ignoring non-list TAG_FACETS value of type %s", type(raw).__name__
        )
        return []

    if not isinstance(data, list):
        if strict:
            raise ValueError("tag_facets must be a JSON array")
        logger.warning("Ignoring non-list TAG_FACETS JSON value")
        return []

    if len(data) > MAX_TAG_FACETS:
        msg = f"at most {MAX_TAG_FACETS} facets are allowed (got {len(data)})"
        if strict:
            raise ValueError(msg)
        logger.warning("Ignoring TAG_FACETS: %s", msg)
        return []

    facets: list[TagFacet] = []
    seen_ids: set[str] = set()
    tag_owner: dict[str, str] = {}
    for index, item in enumerate(data):
        try:
            facet = TagFacet.model_validate(item)
        except ValidationError as exc:
            msg = f"facet[{index}]: {_format_validation_error(exc)}"
            if strict:
                raise ValueError(msg) from exc
            logger.warning("Ignoring TAG_FACETS: %s", msg)
            return []
        if facet.id in seen_ids:
            msg = f"duplicate facet id '{facet.id}'"
            if strict:
                raise ValueError(msg)
            logger.warning("Ignoring TAG_FACETS: %s", msg)
            return []
        facet_tags = {tag for opt in facet.options for tag in opt.tags}
        for tag in sorted(facet_tags):
            if tag in tag_owner:
                msg = (
                    f"tag '{tag}' cannot belong to multiple facets "
                    f"('{tag_owner[tag]}' and '{facet.id}')"
                )
                if strict:
                    raise ValueError(msg)
                logger.warning("Ignoring TAG_FACETS: %s", msg)
                return []
            tag_owner[tag] = facet.id
        seen_ids.add(facet.id)
        facets.append(facet)

    return facets


def serialize_tag_facets(facets: Sequence[TagFacet]) -> str:
    """Serialize validated facets to a canonical compact JSON string."""
    return json.dumps(
        [facet.model_dump() for facet in facets],
        separators=(",", ":"),
    )


def collect_facet_tags(facets: Sequence[TagFacet]) -> set[str]:
    """Return every distinct tag referenced across *facets*."""
    return {tag for facet in facets for opt in facet.options for tag in opt.tags}


def collect_option_tag_sets(
    facets: Sequence[TagFacet],
) -> list[frozenset[str]]:
    """Return each option's tag set across *facets* (preserving multiplicity)."""
    return [frozenset(opt.tags) for facet in facets for opt in facet.options]


def find_newly_introduced_unknown_tags(
    submitted: Sequence[TagFacet],
    live_labels: set[str],
    existing_option_sets: Sequence[frozenset[str]] | None = None,
) -> list[str]:
    """Return sorted unknown tags that are NOT carried over 1-to-1 from an existing option.

    Each existing option may cover at most one submitted option whose missing
    tags are a subset of that existing option's tags. This lets admins rename
    or reorder options/facets holding a vanished tag, or persist an unchanged
    env config, while still rejecting copying a vanished tag into additional
    options or introducing brand-new unknown tags.
    """
    available = list(existing_option_sets) if existing_option_sets is not None else []
    unknown: set[str] = set()
    for facet in submitted:
        for opt in facet.options:
            opt_missing = frozenset(t for t in opt.tags if t not in live_labels)
            if not opt_missing:
                continue
            matched_idx = next(
                (i for i, existing in enumerate(available) if opt_missing <= existing),
                None,
            )
            if matched_idx is not None:
                available.pop(matched_idx)
            else:
                unknown.update(opt_missing)
    return sorted(unknown)


def partition_labels_by_facets(
    labels: Sequence[str],
    facets: Sequence[TagFacet],
    *,
    ungrouped_mode: Literal["and", "or"] = "and",
) -> list[list[str]]:
    """Partition requested labels into AND-of-OR groups using *facets*.

    - Requested labels that belong to the same facet form one OR group.
    - Distinct facets produce separate groups combined with AND.
    - Requested labels outside any facet combine according to
      *ungrouped_mode*: ``"and"`` (Dashboard: one singleton group per
      tag) or ``"or"`` (Reports: one shared OR group, preserving legacy
      multi-tag report bookmarks when no facets cover those tags).
    """
    unique_labels = list(dict.fromkeys(lbl for lbl in labels if lbl))
    if not unique_labels:
        return []

    groups: list[list[str]] = []
    covered: set[str] = set()

    for facet in facets:
        facet_tags = {tag for opt in facet.options for tag in opt.tags}
        matched = [lbl for lbl in unique_labels if lbl in facet_tags]
        if matched:
            groups.append(matched)
            covered.update(matched)

    ungrouped = [lbl for lbl in unique_labels if lbl not in covered]
    if ungrouped:
        if ungrouped_mode == "or":
            groups.append(ungrouped)
        else:
            groups.extend([lbl] for lbl in ungrouped)

    return groups


def diff_tag_facets(
    before: Sequence[TagFacet], after: Sequence[TagFacet]
) -> list[dict[str, Any]]:
    """Compute per-facet create/update/delete audit records.

    Returns a list of ``{"op": "create"|"update"|"delete", "facet_id": str,
    "changed_fields": list[str]}`` entries — never the full facet blob.
    """
    before_by_id = {f.id: f for f in before}
    after_by_id = {f.id: f for f in after}

    surviving_before = [f.id for f in before if f.id in after_by_id]
    surviving_after = [f.id for f in after if f.id in before_by_id]
    reordered_ids = {
        fid
        for fid, before_fid in zip(surviving_after, surviving_before, strict=True)
        if fid != before_fid
    }

    changes: list[dict[str, Any]] = []

    for facet in after:
        prev = before_by_id.get(facet.id)
        if prev is None:
            changes.append(
                {
                    "op": "create",
                    "facet_id": facet.id,
                    "changed_fields": ["name", "options"],
                }
            )
            continue

        changed_fields: list[str] = []
        if prev.name != facet.name:
            changed_fields.append("name")
        if [o.model_dump() for o in prev.options] != [
            o.model_dump() for o in facet.options
        ]:
            changed_fields.append("options")
        if facet.id in reordered_ids:
            changed_fields.append("order")

        if changed_fields:
            changes.append(
                {
                    "op": "update",
                    "facet_id": facet.id,
                    "changed_fields": changed_fields,
                }
            )

    for facet in before:
        if facet.id not in after_by_id:
            changes.append(
                {
                    "op": "delete",
                    "facet_id": facet.id,
                    "changed_fields": [],
                }
            )

    return changes
