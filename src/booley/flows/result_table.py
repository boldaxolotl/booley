"""Shared pieces of the per-Target result table printed by synthesis-style Flows.

``booley flow synth`` and ``booley flow fpga`` end with one aligned table (one
row per Target) followed by notes (warnings, critical conditions, log paths).
Each Flow builds its own columns; this module owns what they share: the cell
conventions, the note line format, and the *record* versus *console* split.

One rendering serves two readers. The **console** text carries colour and goes
to a terminal. The **record** text is the same lines with every ANSI code
stripped; it is what the report, the MCP result and any piped stdout carry, so
agent-facing text never contains escape codes. Every cell is styled through
:mod:`booley.harness.colors`, so ``strip_ansi(console) == record`` by
construction.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import NamedTuple

from booley.harness import colors

#: Placeholder for a metric the run did not produce.
MISSING = "—"

#: Visible-character cap of the Target column (names are cut with ``…``). A
#: fixed cap, not the terminal width, so the output never depends on the host.
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
    """Render ``  <subject>: <LABEL> -- <text>`` with the label coloured by severity.

    An empty *subject* gives a run-level note (``  <LABEL> -- <text>``).
    """
    paint = colors.red if label in _ERROR_LABELS else colors.amber
    owner = f"{subject}: " if subject else ""
    return f"  {owner}{paint(label)} -- {text}"


def result_text(lines: Sequence[str]) -> ResultText:
    """Join *lines* into the console text and its ANSI-free record."""
    console = "\n".join(lines)
    return ResultText(console=console, record=colors.strip_ansi(console))


def tty_text(result: ResultText) -> str:
    """The console form on a terminal, the record everywhere else."""
    return result.console if sys.stdout.isatty() else result.record
