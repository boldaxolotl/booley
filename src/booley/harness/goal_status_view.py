"""Terminal view of ``booley goal status``: a shared-table layout with colored status.

The MCP ``goal_status`` tool keeps :func:`booley.goals.format.render_status`:
agent-facing text that release tooling parses. The terminal view lives in the
harness because ``booley.goals`` must not import ``booley.harness`` (rule D35).
Both views share the same lines of content (Test Runs, baselines, proposals);
only the layout differs. Styling goes through :mod:`booley.harness.colors`, so
piped output is plain text equal to the ANSI-stripped colored layout.
"""

from __future__ import annotations

from collections.abc import Callable

from booley.goals.format import (
    PROJECT_WIDE_PREFIX,
    goal_name,
    proposal_lines,
    synth_baseline_lines,
    test_run_section,
)
from booley.goals.sim_transitions import transition_summary
from booley.goals.status import GoalStatus, GoalStatusView
from booley.harness import colors
from booley.harness.terminal_table import render_table

#: How each Goal status is painted.
_STATUS_STYLE: dict[str, Callable[[str], str]] = {
    "met": colors.green,
    "unmet": colors.red,
    "stale": colors.amber,
}
_HEADERS = ("Goal", "Status", "Evidence")
#: Gap between a fact's key and value (the table's column gap).
_FACT_GAP = "   "
_INDENT = "  "


def _evidence(goal: GoalStatus) -> str:
    """The evidence summary, with the reason an unmet Goal is unmet."""
    return goal.evidence_summary + (
        f" — {goal.reason}" if goal.status == "unmet" and goal.reason else ""
    )


def _facts_block(view: GoalStatusView) -> list[str]:
    """Dimmed key/value lines for the worktree, branch and pending proposals."""
    facts = (
        ("Worktree", view.record.worktree_path),
        ("Branch", view.record.branch),
        ("Pending proposals", str(view.pending_proposals)),
    )
    width = max(len(key) for key, _ in facts)
    return [_INDENT + colors.dim(f"{key:<{width}}{_FACT_GAP}{value}") for key, value in facts]


def _goal_extras(goal: GoalStatus) -> list[str]:
    """Short mode: a Goal's baseline lines, shown under its row."""
    lines = synth_baseline_lines(goal)
    if goal.sim_transitions is not None:
        lines.append(
            f"{goal_name(goal)}: baseline -> current: {transition_summary(goal.sim_transitions)}"
        )
    return [_INDENT + line for line in lines]


def _view_lines(view: GoalStatusView, compact: bool) -> list[str]:
    """One Goal Mode: header, optional facts, the Goal table, then detail lines."""
    lines = [f"{view.record.id} ({view.record.state.value}) · {view.met}/{len(view.goals)} met"]
    if view.warning:
        lines.append(view.warning)
    if not compact:
        lines += _facts_block(view)
    table = render_table(
        _HEADERS,
        [
            (goal_name(goal), _STATUS_STYLE[goal.status](goal.status), _evidence(goal))
            for goal in view.goals
        ],
    )
    if compact:
        # `render_table` guarantees `table[2 + i]` is Goal `i`'s row.
        lines += table[:2]
        for row, goal in zip(table[2:], view.goals, strict=True):
            lines += [row, *_goal_extras(goal)]
        return lines
    lines += table
    lines += test_run_section(view)
    lines += [line for goal in view.goals for line in synth_baseline_lines(goal)]
    lines += proposal_lines(view)
    return lines


def render_goal_status(
    views: tuple[GoalStatusView, ...],
    *,
    short: bool | None = None,
    project_wide: bool = False,
) -> str:
    """Render long detail for one Goal Mode, short rows for several by default."""
    compact = len(views) != 1 if short is None else short
    lines = [PROJECT_WIDE_PREFIX] if project_wide and views else []
    for view in views:
        lines += _view_lines(view, compact)
    return "\n".join(lines).rstrip()
