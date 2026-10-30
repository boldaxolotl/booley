"""Terminal view of ``booley feedback list``.

The listing lives in the harness, not in :mod:`booley.feedback`, because the
shared table renderer and the colour palette are harness modules and the
source dependency contract (rule D33) forbids ``booley.feedback`` from
importing the harness. The harness injects :func:`render_listing` into
``booley.feedback.cli.run`` as its ``list_view``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from booley.feedback.findings import Finding, FindingsLog

from . import colors
from .terminal_table import render_table

_HEADERS = ("ID", "Kind", "Bucket", "Title", "Status")
_TITLE_COLUMN = 3
#: Fixed visible-character cap for titles, so output does not depend on the host terminal.
_TITLE_CAP = 48
#: Bucket cell for a win, which has no owner to triage.
_NO_BUCKET = "—"

#: Colour per bug severity. Words carry the severity; colour only reinforces it.
_SEVERITY_STYLE: dict[str, Callable[[str], str]] = {
    "blocker": colors.red,
    "workaround": colors.amber,
    "note": colors.dim,
}


def _kind_cell(entry: Finding) -> str:
    """Spell the kind in words; a bug also spells its severity, coloured by it."""
    if entry.kind != "finding":
        return entry.kind
    return _SEVERITY_STYLE[entry.severity](f"bug ({entry.severity})")


def _status_cell(entry: Finding) -> str:
    """Say whether the entry is filed, still lacks evidence, or needs nothing.

    Wins, filable entries and entries in the ``project``/``unknown`` buckets
    need nothing from the reporter.
    """
    if entry.kind == "win":
        return ""
    if entry.filed.strip():
        return f"filed: {_single_line(entry.filed)}"
    if entry.is_filable() or entry.bucket in ("project", "unknown"):
        return ""
    return colors.amber("needs evidence")


def _single_line(text: str) -> str:
    """Make user-written *text* safe for one table cell.

    ``render_table`` leaves sanitising to its caller: drop escape sequences and
    fold newlines and tabs into single spaces so a title cannot break the row
    or bleed colour into the rows below.
    """
    return " ".join(colors.strip_ansi(text).split())


def _row(entry: Finding) -> list[str]:
    bucket = _NO_BUCKET if entry.kind == "win" else entry.bucket
    return [entry.id, _kind_cell(entry), bucket, _single_line(entry.title), _status_cell(entry)]


def _totals(log: FindingsLog) -> str:
    """One line of tallies over the whole log (not just the listed rows).

    Bugs, friction, impressions and wins are counted apart: severities describe
    bugs only, so folding the others in makes the numbers not add up.
    """
    counts = log.counts()
    return (
        f"  bugs {len(log.bugs)} "
        f"(blocker {counts['blocker']} · workaround {counts['workaround']} · note {counts['note']})"
        f"   friction {len(log.friction)}"
        f"   impressions {len(log.impressions)}"
        f"   wins {len(log.wins)}"
    )


def render_listing(log: FindingsLog, entries: Sequence[Finding]) -> str:
    """Render *entries* as a table followed by a blank line and the log's totals."""
    lines = render_table(
        _HEADERS,
        [_row(entry) for entry in entries],
        max_widths={_TITLE_COLUMN: _TITLE_CAP},
    )
    return "\n".join([*lines, "", _totals(log)])
