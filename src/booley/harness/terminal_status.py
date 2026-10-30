"""Shared status-line presentation: one glyph per row, section headers, hints.

Doctor prints its rows with these helpers. A status row is::

      ✓  message text                                         dimmed.check.id

The glyph sits in a fixed column, the message always starts at the same column,
the optional check ID is dimmed and right-aligned, and a fix hint hangs under
the message. All styling goes through :mod:`booley.harness.colors`, which
returns plain text when color is off (pipes, ``NO_COLOR``, ``TERM=dumb``), so
the plain layout equals the ANSI-stripped colored layout.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from enum import Enum

from .colors import accent, amber, bold_accent, dim, green, len_visible, lilac, red

#: Widest line a status row is padded to; a wider terminal does not widen rows.
MAX_WIDTH = 100
_INDENT = "  "
_GLYPH_GAP = "  "
#: Fewest spaces between a message and its right-aligned check ID.
_MIN_ID_GAP = 2


class LineStatus(Enum):
    """What a status row reports."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    NOTE = "note"
    WAIVED = "waived"
    RUNNING = "running"


#: status -> (single-character glyph, style applied to it)
_GLYPHS: dict[LineStatus, tuple[str, Callable[[str], str]]] = {
    LineStatus.PASS: ("✓", green),
    LineStatus.WARN: ("!", amber),
    LineStatus.FAIL: ("✗", red),
    LineStatus.SKIP: ("·", dim),
    LineStatus.NOTE: ("·", dim),
    LineStatus.WAIVED: ("~", lilac),
    LineStatus.RUNNING: ("▸", accent),
}

# The message column: indent + one glyph + the gap after it.
_MESSAGE_COLUMN = len(_INDENT) + 1 + len(_GLYPH_GAP)


def glyph(status: LineStatus) -> str:
    """The styled single-character glyph for *status*."""
    char, style = _GLYPHS[status]
    return style(char)


def _line_width() -> int:
    """Width right-aligned IDs end at: the terminal's, capped; fixed off a TTY.

    Pipes, captured output, and transcripts get the fixed cap so the same run
    always prints the same bytes.
    """
    if not sys.stdout.isatty():
        return MAX_WIDTH
    return min(shutil.get_terminal_size((MAX_WIDTH, 24)).columns, MAX_WIDTH)


def status_line(status: LineStatus, message: str, *, check_id: str = "") -> str:
    """One status row; a *check_id* is dimmed and ends at the line width.

    A message too long for the width keeps a gap of two spaces before its ID
    rather than being cut.
    """
    line = f"{_INDENT}{glyph(status)}{_GLYPH_GAP}{message}"
    if not check_id:
        return line
    gap = max(_MIN_ID_GAP, _line_width() - len_visible(line) - len(check_id))
    return f"{line}{' ' * gap}{dim(check_id)}"


def hint_line(text: str) -> str:
    """A hint hanging under a row's message, led by an arrow."""
    return f"{' ' * _MESSAGE_COLUMN}→ {text}"


def section_header(title: str) -> str:
    """A section title at column 0."""
    return bold_accent(title)
