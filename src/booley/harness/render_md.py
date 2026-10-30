"""Render a subset of Markdown to ANSI terminal output.

Handles: headers (##/###/####), tables, bold, backticks, bullet lists.
No external dependencies for ANSI mode. Given a width, tables shrink their
widest columns and prose and bullets wrap at word boundaries.
"""

from __future__ import annotations

import re

from .colors import (
    accent,
    active_style,
    bold,
    bold_accent,
    char_width,
    close_style,
    dim,
    len_visible,
)
from .terminal_table import render_table

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*([^*]+)\*")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def inline(text: str) -> str:
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


def _render_markdown_table(rows: list[str], width: int | None) -> list[str]:
    """Render markdown table rows (header, separator, data) through the shared renderer."""
    header = _parse_table_row(rows[0])
    data_rows = [[inline(cell) for cell in _parse_table_row(r)] for r in rows[2:]]
    return render_table(header, data_rows, width=width)


def _split_long(word: str, room: int) -> list[str]:
    """Split a word wider than *room* cells into pieces of at most *room* cells."""
    pieces: list[str] = []
    piece, cells = "", 0
    for token in re.split(r"(\033\[[0-9;]*m)", word):
        if token.startswith("\033"):
            piece += token  # a style code takes no cell
            continue
        for char in token:
            if cells + char_width(char) > room:
                pieces.append(piece)
                piece, cells = "", 0
            piece += char
            cells += char_width(char)
    return [*pieces, piece]


def wrap(text: str, width: int, first: str = "", rest: str = "") -> list[str]:
    """Wrap *text* at spaces into lines of at most *width* cells.

    *first* leads the first line and *rest* every later one; give them the same
    cell width (indent, hanging bullet indent) so every line has the same room.
    A word wider than the room splits at a cell boundary. A style (SGR codes
    only) open at a break is closed at the end of that line and reopened after
    the next line's prefix, so it never bleeds into indentation.
    """
    room = max(1, width - len_visible(rest))
    lines: list[str] = []
    prefix, body, cells, opened, active = first, "", 0, "", ""
    for word in text.split():
        for piece in _split_long(word, room):
            piece_cells = len_visible(piece)
            if body and cells + 1 + piece_cells > room:
                lines.append(prefix + opened + body + close_style(active))
                prefix, body, cells, opened = rest, "", 0, active
            if body:
                body, cells = body + " ", cells + 1
            body, cells = body + piece, cells + piece_cells
            active = active_style(piece, active)
    lines.append(prefix + opened + body + close_style(active))
    return lines if body else [first.rstrip()]


def _emit_text(out: list[str], text: str, first: str, rest: str, width: int | None) -> None:
    """Append *text* led by *first*; wrap it when it is wider than *width*."""
    if width is None or len_visible(first + text) <= width:
        out.append(first + text)
    else:
        out.extend(wrap(text, width, first, rest))


def _render_table_block(lines: list[str], start: int, out: list[str], width: int | None) -> int:
    """Render the run of pipe-rows at *start* into *out*; returns the next line index."""
    end = start
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    rows = lines[start:end]
    if len(rows) >= 2 and _is_separator(rows[1]):
        out.extend(_render_markdown_table(rows, width))
    else:
        for row in rows:
            _emit_text(out, inline(row), "", "", width)
    return end


def _render_section_heading(lines: list[str], start: int, out: list[str]) -> int:
    """Render the ``### `` heading at *start* with one blank line on each side.

    The source's own blank lines around it are absorbed; returns the next line index.
    """
    if not out or out[-1] != "":
        out.append("")
    out.extend([bold_accent(lines[start][4:]), ""])
    end = start + 1
    while end < len(lines) and not lines[end].strip():
        end += 1
    return end


def render(markdown: str, *, width: int | None = None) -> str:
    """Render markdown string to ANSI-formatted terminal text.

    With a *width* (in terminal cells), tables shrink to fit it and prose and
    bullets wrap; ``None`` leaves every line as long as its source.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        indent = line[: len(line) - len(line.lstrip())]

        if line.strip().startswith("|"):
            i = _render_table_block(lines, i, out, width)
            continue
        if line.startswith("### "):
            i = _render_section_heading(lines, i, out)
            continue

        if line.startswith("#### "):
            out.append(bold(line[5:]))
        elif line.startswith("## "):
            out.append(bold_accent(line[3:]))
            out.append("─" * len(line[3:]))
        elif line.strip().startswith("- "):
            _emit_text(out, inline(line.strip()[2:]), indent + "  • ", indent + "    ", width)
        elif not line.strip():
            out.append("")
        else:
            _emit_text(out, inline(line.lstrip()), indent, indent, width)

        i += 1

    return "\n".join(out) + "\n"
