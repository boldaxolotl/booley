"""Render a subset of Markdown to ANSI terminal output.

Handles: headers (##/###/####), tables, bold, backticks, bullet lists.
No external dependencies for ANSI mode.
"""

from __future__ import annotations

import re

from .colors import accent, bold, bold_accent, dim, len_visible

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*([^*]+)\*")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    """Apply inline formatting: **bold**, *italic*, `code`."""
    text = _BOLD_RE.sub(lambda m: bold(m.group(1)), text)
    text = _ITALIC_RE.sub(lambda m: dim(m.group(1)), text)
    text = _BACKTICK_RE.sub(lambda m: accent(m.group(1)), text)
    return text


def _is_separator(row: str) -> bool:
    """Check if a table row is a separator (|---|---|)."""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    return all(re.fullmatch(r":?-+:?", c) for c in cells)


def _parse_table_row(row: str) -> list[str]:
    """Split a pipe-delimited table row into cells."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _render_table(rows: list[str]) -> list[str]:
    """Render markdown table rows as column-aligned text."""
    header = _parse_table_row(rows[0])
    data_rows = [_parse_table_row(r) for r in rows[2:]]  # skip separator

    # Compute column widths from raw text (no ANSI)
    ncols = len(header)
    widths = [len(h) for h in header]
    for row in data_rows:
        for i, cell in enumerate(row[:ncols]):
            clean = _BOLD_RE.sub(r"\1", cell)
            clean = _ITALIC_RE.sub(r"\1", clean)
            clean = _BACKTICK_RE.sub(r"\1", clean)
            widths[i] = max(widths[i], len(clean))

    out: list[str] = []

    # Header
    styled_header = [bold_accent(h.ljust(widths[i])) for i, h in enumerate(header)]
    out.append("  " + "   ".join(styled_header))

    # Separator
    out.append("  " + "   ".join("─" * w for w in widths))

    # Data rows
    for row in data_rows:
        cells: list[str] = []
        for i in range(ncols):
            raw = row[i] if i < len(row) else ""
            styled = _inline(raw)
            pad = widths[i] - len_visible(styled)
            cells.append(styled + " " * max(0, pad))
        out.append("  " + "   ".join(cells))

    return out


def render(markdown: str) -> str:
    """Render markdown string to ANSI-formatted terminal text."""
    lines = markdown.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Table: collect consecutive pipe-rows
        if line.strip().startswith("|"):
            table_rows: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_rows.append(lines[i])
                i += 1
            if len(table_rows) >= 2 and _is_separator(table_rows[1]):
                out.extend(_render_table(table_rows))
            else:
                out.extend(_inline(r) for r in table_rows)
            continue

        # Headers
        if line.startswith("#### "):
            out.append(bold(line[5:]))
        elif line.startswith("### "):
            out.append("")
            out.append(bold_accent(line[4:]))
            out.append("")
        elif line.startswith("## "):
            out.append(bold_accent(line[3:]))
            out.append("─" * len(line[3:]))
        # Bullet points
        elif line.strip().startswith("- "):
            indent = len(line) - len(line.lstrip())
            out.append(" " * indent + "  • " + _inline(line.strip()[2:]))
        # Regular text
        else:
            out.append(_inline(line))

        i += 1

    return "\n".join(out) + "\n"
