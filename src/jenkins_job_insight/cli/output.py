"""Output formatting helpers for the jji CLI."""

import json


def format_table(
    data: list[dict],
    columns: list[str],
    labels: dict[str, str] | None = None,
) -> str:
    """Format a list of dicts as an aligned text table.

    Args:
        data: List of row dicts.
        columns: Column keys to display, in order.
        labels: Optional mapping of column key to display label.
            If not provided, the column key is uppercased.

    Returns:
        Formatted table string.
    """
    if not data:
        return "No results."

    if labels is None:
        labels = {}

    headers = [labels.get(col, col.upper().replace("_", " ")) for col in columns]

    rows: list[list[str]] = []
    for item in data:
        row = []
        for col in columns:
            val = str(item.get(col, ""))
            row.append(val)
        rows.append(row)

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build output
    lines: list[str] = []
    header_line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    lines.append(header_line)
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True))
        )

    return "\n".join(lines)


def format_json(data: dict | list) -> str:
    """Format data as pretty-printed JSON.

    Args:
        data: Dict or list to serialize.

    Returns:
        JSON string with 2-space indentation.
    """
    return json.dumps(data, indent=2, default=str)


def print_output(
    data: dict | list,
    columns: list[str],
    as_json: bool = False,
    labels: dict[str, str] | None = None,
) -> None:
    """Print data to stdout in table or JSON format.

    Args:
        data: Data to print. For table mode, must be a list of dicts.
        columns: Column keys for table mode.
        as_json: If True, print as JSON. If False, print as table.
        labels: Optional column label overrides for table mode.
    """
    if as_json:
        print(format_json(data))
        return

    if isinstance(data, dict):
        data = [data]
    print(format_table(data, columns=columns, labels=labels))
