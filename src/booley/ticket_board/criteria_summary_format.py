"""Terminal criteria-summary formatting.

Extracted from ``criteria_acceptance.py`` (principle 8 -- Single
Responsibility): that module decides ticket disposition (met/unmet/blocked
criteria -> review/failed/blocked), this one renders the per-criterion and
totals lines shown in the terminal at end-of-run. Mirrors the earlier split
of ``console/criteria_format.py`` out of the Textual widgets module for the
same reason -- disposition logic and display logic are separate reasons to
change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from booley.criteria.actions import planned_invocation

if TYPE_CHECKING:
    from pathlib import Path

    from booley.criteria.endpoint_catalog import CriterionEndpointCatalog

    from .criteria_acceptance import CriteriaVerdict


def format_criteria_verdict(verdict: CriteriaVerdict) -> str:
    """Format verdict as a human-readable summary string."""
    lines = [
        f"Criteria: {verdict.met}/{verdict.total} met "
        f"({verdict.mandatory_met}/{verdict.mandatory} mandatory)",
    ]
    if verdict.passed:
        lines.append("Disposition: REVIEW (all mandatory criteria met)")
    elif verdict.blocked_reason:
        lines.append(f"Disposition: BLOCKED ({verdict.blocked_reason})")
    else:
        lines.append("Disposition: FAILED (unmet mandatory criteria)")
        for key in verdict.unmet_mandatory:
            lines.append(f"  - {key}")
    note = verdict.unverified_transitions_note()
    if note:
        lines.append(note)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal criteria summary (per-criterion detail lines)
# ---------------------------------------------------------------------------


from booley.goals.format import format_criterion_metric

_COLLAPSIBLE_GROUPS = [
    ("review_", "reviews"),
    ("mutation_", "mutation"),
]


def _group_of(key: str) -> str | None:
    for prefix, name in _COLLAPSIBLE_GROUPS:
        if key.startswith(prefix):
            return name
    return None


def _is_never_evaluated(entry) -> bool:
    return (
        not entry.met
        and not entry.detail
        and not getattr(entry, "stale", False)
        and not getattr(entry, "ever_met", False)
    )


def _collapsed_groups(real: dict) -> set[str]:
    """Return group names whose every member criterion was never evaluated."""
    groups: dict[str, list[tuple[str, object]]] = {}
    for key, entry in real.items():
        gname = _group_of(key)
        if gname is not None:
            groups.setdefault(gname, []).append((key, entry))
    return {
        gname
        for gname, members in groups.items()
        if all(_is_never_evaluated(e) for _, e in members)
    }


def _partition_criteria_lines(
    real: dict,
    collapsed: set[str],
    fmt,
) -> tuple[list[str], list[str]]:
    """Split criteria into (not-met, met) display lines, collapsing dead groups."""
    from booley.harness.colors import dim, gray

    not_met_lines: list[str] = []
    met_lines: list[str] = []
    emitted: set[str] = set()

    for key, entry in real.items():
        gname = _group_of(key)

        if gname in collapsed:
            if gname not in emitted:
                emitted.add(gname)
                not_met_lines.append(f"{gray('○')} {dim(f'{gname} (not yet run)')}")
            continue

        line = fmt(key, entry)
        if entry.met:
            met_lines.append(line)
        else:
            not_met_lines.append(line)

    return not_met_lines, met_lines


def build_criteria_summary_lines(
    state_path: Path,
    endpoint_catalog: CriterionEndpointCatalog,
) -> tuple[list[str], str]:
    """Build per-criterion lines and a totals line for terminal display.

    Returns (criterion_lines, totals_line). Empty lists if state is unreadable.
    """
    from booley.criteria.state import DevelopmentState
    from booley.harness.colors import amber, dim, gray, green, red

    # Local import (not module-level) to avoid a circular import with
    # criteria_acceptance, which re-exports this function for compatibility.
    from .criteria_acceptance import _compute_criteria_stats

    if not state_path.exists():
        return [], ""

    state = DevelopmentState.load(state_path)
    if not state.criteria:
        return [], ""

    real = {k: e for k, e in state.criteria.items() if not k.startswith("_")}

    def _icon(entry) -> str:
        if entry.met:
            return green("✓")
        if _is_never_evaluated(entry):
            return gray("○")
        if getattr(entry, "stale", False):
            return amber("↻")
        return red("✗")

    def _fmt(key: str, entry) -> str:
        icon = _icon(entry)
        metric = format_criterion_metric(key, entry)
        opt = "" if entry.mandatory else " (opt)"
        metric_str = f"  {metric}" if metric and metric not in key else ""
        name_part = f"{key}{opt}{metric_str}"
        if _is_never_evaluated(entry):
            name_part = dim(name_part)
        line = f"{icon} {name_part}"
        if not entry.met:
            invocation = planned_invocation(key, entry, endpoint_catalog)
            if invocation:
                line += f"\n  next: {invocation}"
        return line

    collapsed = _collapsed_groups(real)
    not_met_lines, met_lines = _partition_criteria_lines(real, collapsed, _fmt)

    lines = not_met_lines
    if not_met_lines and met_lines:
        lines.append("")
    lines.extend(met_lines)

    stats = _compute_criteria_stats(state.criteria)
    n_unmet = len(stats["unmet"])
    totals = f"{stats['met']}/{stats['total']} met"
    if n_unmet:
        totals += f" ({n_unmet} mandatory unmet)"
    else:
        totals += f" ({stats['mandatory_met']}/{stats['mandatory']} mandatory)"
    # run.log is append-only, so a total printed at step N stays verbatim even
    # after later runs move the tally. Stamp the point-in-time so a triager
    # reading the tail doesn't reconcile a stale count against the live board.
    step_n = len(getattr(state, "timeline", []) or [])
    totals += f" (as of step {step_n})"
    return lines, totals
