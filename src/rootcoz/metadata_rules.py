"""Name-based metadata rule matching for automatic job metadata assignment.

Rules are loaded from a YAML or JSON file and evaluated in order against
job names.  The first rule whose pattern matches wins for scalar fields
(team, tier, version); labels accumulate from all matching rules.
"""

import fnmatch
import json
import os
import re
from pathlib import Path

from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))


def load_metadata_rules(path: str) -> list[dict]:
    """Parse a YAML or JSON metadata rules file.

    The file must contain a top-level ``metadata_rules`` key whose value
    is a list of rule objects.  Alternatively, a bare list is accepted.

    Each rule object must have a ``pattern`` key.  Optional keys:
    ``team``, ``tier``, ``version``, ``labels`` (list[str]).

    Args:
        path: Filesystem path to the rules file.

    Returns:
        List of validated rule dicts.

    Raises:
        FileNotFoundError: When *path* does not exist.
        ValueError: When the file content is malformed.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Metadata rules file not found: {path}")

    content = p.read_text(encoding="utf-8")
    raw: object

    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise ImportError(
                "PyYAML is required to load YAML rules files. "
                "Install with: pip install pyyaml"
            ) from exc
        raw = yaml.safe_load(content)
    else:
        raw = json.loads(content)

    # Accept both ``{"metadata_rules": [...]}`` and a bare list.
    if isinstance(raw, dict):
        rules_list = raw.get("metadata_rules")
        if rules_list is None:
            raise ValueError(
                "Rules file must contain a 'metadata_rules' key or be a JSON array"
            )
    elif isinstance(raw, list):
        rules_list = raw
    else:
        raise ValueError("Rules file must be a dict with 'metadata_rules' or a list")

    if not isinstance(rules_list, list):
        raise ValueError("'metadata_rules' must be a list")

    validated: list[dict] = []
    for idx, rule in enumerate(rules_list):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule at index {idx} must be a dict")
        if "pattern" not in rule:
            raise ValueError(f"Rule at index {idx} is missing 'pattern'")
        validated.append(_normalize_rule(rule))

    logger.info(f"Loaded {len(validated)} metadata rules from {path}")
    return validated


def _normalize_rule(rule: dict) -> dict:
    """Normalize a single rule dict to consistent types."""
    normalized: dict = {"pattern": str(rule["pattern"])}
    # Pre-compile regex patterns so malformed rules are caught at load time.
    if _is_regex_pattern(normalized["pattern"]):
        try:
            re.compile(normalized["pattern"])
        except re.error as exc:
            raise ValueError(
                f"Invalid regex pattern {normalized['pattern']!r}: {exc}"
            ) from exc
    for key in ("team", "tier", "version"):
        if key in rule and rule[key] is not None:
            normalized[key] = str(rule[key])
    if "labels" in rule and rule["labels"] is not None:
        labels = rule["labels"]
        if isinstance(labels, str):
            normalized["labels"] = [labels]
        elif isinstance(labels, list):
            normalized["labels"] = [str(lbl) for lbl in labels]
        else:
            raise ValueError(
                f"Rule '{normalized['pattern']}': 'labels' must be a list or string, "
                f"got {type(labels).__name__}"
            )
    return normalized


def _is_regex_pattern(pattern: str) -> bool:
    """Detect whether a pattern uses regex features (named groups).

    Heuristic: a pattern is treated as regex if and only if it contains
    a named capture group ``(?P<...>)``.  Patterns without named groups
    — even ones that look like regex (e.g. ``^job-.*$``) — are routed
    through ``fnmatch`` and matched as globs.  To use full regex
    matching, include at least one named capture group in the pattern.
    """
    return "(?P<" in pattern


def _match_single_rule(job_name: str, rule: dict) -> dict | None:
    """Try matching a single rule against a job name.

    Returns a dict of matched metadata fields (may be empty aside from
    pattern-derived values), or None if no match.
    """
    pattern = rule["pattern"]
    matched_fields: dict = {}

    if _is_regex_pattern(pattern):
        m = re.fullmatch(pattern, job_name)
        if not m:
            return None
        # Extract named groups (e.g. version)
        for key, value in m.groupdict().items():
            if value is not None:
                matched_fields[key] = value
    else:
        if not fnmatch.fnmatchcase(job_name, pattern):
            return None

    # Copy explicit fields from rule.
    # Precedence: explicit rule values override regex-captured values.
    # E.g. a rule with both pattern "(?P<version>\d+)" and version: "fixed"
    # will always set version="fixed", regardless of the capture.
    for key in ("team", "tier", "version"):
        if key in rule:
            matched_fields[key] = rule[key]
    if "labels" in rule:
        matched_fields["labels"] = list(rule["labels"])

    return matched_fields


def match_job_metadata(job_name: str, rules: list[dict]) -> dict | None:
    """Match a job name against an ordered list of rules.

    Scalar fields (team, tier, version) use first-match-wins.
    Labels accumulate from all matching rules.

    Args:
        job_name: The Jenkins job name to match.
        rules: Ordered list of rule dicts (from :func:`load_metadata_rules`).

    Returns:
        A metadata dict with ``team``, ``tier``, ``version``, ``labels``
        keys, or ``None`` if no rule matched.
    """
    result: dict = {}
    accumulated_labels: list[str] = []
    any_match = False

    for rule in rules:
        matched = _match_single_rule(job_name, rule)
        if matched is None:
            continue

        any_match = True

        # Scalar fields: first match wins
        for key in ("team", "tier", "version"):
            if key in matched and key not in result:
                result[key] = matched[key]

        # Labels accumulate
        if "labels" in matched:
            for lbl in matched["labels"]:
                if lbl not in accumulated_labels:
                    accumulated_labels.append(lbl)

    if not any_match:
        return None

    return {
        "team": result.get("team"),
        "tier": result.get("tier"),
        "version": result.get("version"),
        "labels": accumulated_labels,
    }
