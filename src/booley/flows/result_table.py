"""Shared pieces of the per-Target result table printed by synthesis-style Flows.

Each Flow builds its own columns; this module owns the cell conventions, the
note line format, and the *record* versus *console* split. The console text
carries colour; the record is the same lines with every ANSI code stripped
(report, MCP result, piped stdout), so ``strip_ansi(console) == record``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import NamedTuple

from booley.harness import colors

#: Placeholder for a metric the run did not produce.
MISSING = "—"

#: Visible-character cap of the Target column (cut with ``…``); fixed, so the
#: output never depends on the terminal width.
TARGET_WIDTH = 40

#: Note labels that mark a problem; every other label is advisory.
_ERROR_LABELS = frozenset({"CRITICAL", "ERROR"})


class ResultText(NamedTuple):
    """One rendering of a result block, in both of its forms."""

    console: str
    record: str


def status_cell(text: str) -> str:
    """Colour a Status cell: green for ``PASS``, red for anything else."""
    return colors.green(text) if text.startswith("PASS") else colors.red(text)


def note_line(subject: str, label: str, text: str) -> str:
    """Render ``  <subject>: <LABEL> -- <text>``; an empty *subject* is a run-level note."""
    paint = colors.red if label in _ERROR_LABELS else colors.amber
    owner = f"{subject}: " if subject else ""
    return f"  {owner}{paint(label)} -- {text}"


def compose_result(
    baseline_ref: str | None,
    table: Sequence[str],
    notes: Sequence[str],
    result_line: str,
) -> ResultText:
    """Assemble the ``baseline:`` line, table, notes and ``RESULT:`` line."""
    lines = [
        *([f"baseline: {baseline_ref}"] if baseline_ref else []),
        *table,
        *([""] if notes else []),
        *notes,
        "",
        result_line,
    ]
    console = "\n".join(lines)
    return ResultText(console=console, record=colors.strip_ansi(console))


def tty_text(result: ResultText) -> str:
    """The console form on a terminal, the record everywhere else."""
    return result.console if sys.stdout.isatty() else result.record
