"""Grouped, column-aligned rows of the Dashboard Sessions overview.

Pure presentation, no Textual: per-session facts and the list width in, ordered
group headers and fitted rows out. Widths count terminal cells, not characters.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from rich.cells import cell_len, set_cell_size
from rich.text import Text

from booley.runtime.session_activity import (
    UNKNOWN,
    WAITING_TOOL,
    WAITING_USER,
    WORKING,
    Activity,
)
from booley.runtime.timefmt import format_duration, parse_timestamp

NEEDS_YOU = "NEEDS YOU"
WORKING_GROUP = "WORKING"
UNKNOWN_GROUP = "UNKNOWN"
STANDALONE_GROUP = "STANDALONE JOBS"
GROUP_ORDER = (NEEDS_YOU, WORKING_GROUP, UNKNOWN_GROUP, STANDALONE_GROUP)
#: Row keys of group header rows start with this; no session or Job key does.
HEADER_PREFIX = "group:"
ABSENT = "—"
ELLIPSIS = "…"

SESSION, MODE, BRANCH, WORK_DIR, STARTED, ACTIVITY = range(6)
LABELS = ("Session", "Mode", "Branch", "Work dir", "Started", "Activity")
GAP = 2
#: Narrowest a column is squeezed before columns drop; Activity is cut last, below 40 cells.
_FLOORS = {BRANCH: 8, WORK_DIR: 10, ACTIVITY: 40}
#: Columns that disappear when the list is too narrow, first to go first.
DROP_ORDER = (STARTED, WORK_DIR, BRANCH)
#: Spare width is handed out in this order, each up to the column's natural width.
GROW_ORDER = (ACTIVITY, BRANCH, WORK_DIR)


@dataclass(frozen=True)
class SessionFacts:
    """What one overview row needs to know about a connected session."""

    key: str
    handle: str
    #: ``Interactive``, ``Goal m/n``, or ``Goal`` when Goal status is unavailable.
    mode: str
    branch: str
    work_dir: str
    started: str
    activity: Activity
    proposals: int
    #: Running-Job summaries such as ``▶ J-SIM3 sim fifo 1m 12s``.
    jobs: tuple[str, ...] = ()
    #: Shared-worktree warning, shared-attribution notice and Goal diagnostic.
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class StandaloneJob:
    """A running Job that belongs to no session."""

    key: str
    label: str
    detail: str


@dataclass(frozen=True)
class Overview:
    """Header line, row keys and rows, in display order."""

    header: Text
    keys: list[str]
    rows: list[Text]


def group_of(facts: SessionFacts) -> str:
    """A pending proposal needs the human whatever the session is doing."""
    if facts.activity.state == WAITING_USER or facts.proposals > 0:
        return NEEDS_YOU
    if facts.activity.state in {WORKING, WAITING_TOOL}:
        return WORKING_GROUP
    return UNKNOWN_GROUP


def order_key(facts: SessionFacts) -> tuple[bool, float, float, str]:
    """Longest in state first; unparseable times, then ties, fall back to the handle number."""
    try:
        since, parsed = parse_timestamp(facts.activity.since).timestamp(), True
    except ValueError:
        since, parsed = 0.0, False
    number = re.fullmatch(r"S(\d+)", facts.handle)
    return (not parsed, since, int(number[1]) if number else math.inf, facts.key)


def _short_activity(activity: Activity, now: float) -> str:
    """The activity with how long it has lasted; unknown is said plainly."""
    if activity.state == UNKNOWN:
        return "unknown"
    try:
        lasted = " " + format_duration(now - parse_timestamp(activity.since).timestamp())
    except ValueError:
        lasted = ""
    if activity.state == WAITING_TOOL:
        return f"tool call{f' ({activity.tool})' if activity.tool else ''}{lasted}"
    return ("waiting" if activity.state == WAITING_USER else activity.state) + lasted


def last_column(facts: SessionFacts, now: float) -> str:
    """Activity, or the running Jobs of a working session, then proposals and notices."""
    activity = _short_activity(facts.activity, now)
    proposals = (
        f"{facts.proposals} proposal{'s' * (facts.proposals != 1)}" if facts.proposals else ""
    )
    # Waiting leads with the wait; a session needing the human only by proposal leads with it.
    parts = (
        [activity, proposals] if facts.activity.state == WAITING_USER else [proposals, activity]
    )
    jobs_replace_activity = group_of(facts) == WORKING_GROUP and facts.jobs
    parts = list(facts.jobs) if jobs_replace_activity else [*parts, *facts.jobs]
    return " · ".join(part for part in [*parts, *facts.notices] if part)


def cut(text: str, width: int, *, keep_tail: bool = False) -> str:
    """Fit ``text`` into ``width`` cells, marking the cut with an ellipsis."""
    if cell_len(text) <= width:
        return text
    if width <= 1:
        return ELLIPSIS[:width]
    if keep_tail:  # Cut the reversed text, so the end survives.
        return ELLIPSIS + set_cell_size(text[::-1], width - 1)[::-1].lstrip()
    return set_cell_size(text, width - 1).rstrip() + ELLIPSIS


def fit(width: int, rows: Sequence[Sequence[str]]) -> dict[int, int]:
    """Cell width of each visible column, in order, for a list ``width`` cells wide."""
    natural = [
        max([cell_len(LABELS[c]), *(cell_len(row[c]) for row in rows)]) for c in range(len(LABELS))
    ]

    def minimum(column: int) -> int:
        return min(natural[column], _FLOORS.get(column, natural[column]))

    def need(columns: Sequence[int]) -> int:
        return sum(minimum(c) for c in columns) + GAP * (len(columns) - 1)

    shown = [c for c in range(len(LABELS)) if natural[c]]
    for dropped in DROP_ORDER:
        if need(shown) <= width:
            break
        shown.remove(dropped)
    widths = {c: minimum(c) for c in shown}
    spare = width - need(shown)
    for column in GROW_ORDER:
        if column in widths and spare > 0:
            grown = min(spare, natural[column] - widths[column])
            widths[column] += grown
            spare -= grown
    if spare < 0:  # Even the kept columns overflow: the Activity takes the cut.
        widths[ACTIVITY] = max(1, widths[ACTIVITY] + spare)
    return widths


def _line(widths: dict[int, int], cells: Sequence[str], style: str) -> Text:
    """One row as a single padded ``Text`` so every column starts at the same offset."""
    line = Text()
    for position, (column, width) in enumerate(widths.items()):
        cell = cut(cells[column], width, keep_tail=column == WORK_DIR)  # Paths differ at the end.
        if position:
            line.append(" " * GAP)
        line.append(cell, style="dim" if cell == ABSENT else style)
        if position < len(widths) - 1:
            line.append(" " * (width - cell_len(cell)))
    return line


def build(
    sessions: Sequence[SessionFacts],
    standalone: Sequence[StandaloneJob],
    now: float,
    width: int,
) -> Overview:
    """Group, order and fit every row for a list ``width`` cells wide."""
    grouped: dict[str, list[tuple[str, tuple[str, ...]]]] = {name: [] for name in GROUP_ORDER}
    for facts in sorted(sessions, key=order_key):
        grouped[group_of(facts)].append(
            (
                facts.key,
                (
                    facts.handle,
                    facts.mode,
                    facts.branch or ABSENT,
                    facts.work_dir or ABSENT,
                    facts.started,
                    last_column(facts, now),
                ),
            )
        )
    grouped[STANDALONE_GROUP] = [
        (job.key, (job.label, "standalone", "", "", "", job.detail)) for job in standalone
    ]
    widths = fit(width, [cells for rows in grouped.values() for _, cells in rows])
    keys, lines = [], []
    for name in GROUP_ORDER:
        if not grouped[name]:
            continue
        dim = "dim" if name == UNKNOWN_GROUP else ""
        keys.append(HEADER_PREFIX + name)
        lines.append(Text(f"{name} {len(grouped[name])}", style=f"bold {dim}".strip()))
        for key, cells in grouped[name]:
            keys.append(key)
            lines.append(_line(widths, cells, dim))
    return Overview(_line(widths, LABELS, "bold"), keys, lines)
