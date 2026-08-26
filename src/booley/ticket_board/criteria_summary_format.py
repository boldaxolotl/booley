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

from booley.dev_support.criteria_actions import planned_invocation
from booley.flows.clock_timing import worst_fmax_from_json

if TYPE_CHECKING:
    from pathlib import Path

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

_COVERAGE_DETAIL_KEYS = {
    "coverage_toggle": "toggle",
    "coverage_fsm": "fsm",
    "coverage_value": "value",
    "coverage_branch": "branch",
    "coverage_expression": "expression",
    "coverage_mean": "mean",
}


def _format_coverage_metric(key: str, d: dict, p: dict, stale: bool) -> str | None:
    """Format a coverage criterion's ``pct`` (need threshold%), or None if N/A."""
    for prefix, sub_key in _COVERAGE_DETAIL_KEYS.items():
        if key == prefix or key.startswith(f"{prefix}_"):
            sub = d.get(sub_key, {})
            pct = sub.get("pct") if isinstance(sub, dict) else None
            if pct is not None:
                thr = p.get("min_pct", "")
                thr_str = f" / need {thr}%" if thr else ""
                val = "?%" if stale else f"{pct:.0f}%"
                return f"{val}{thr_str}"
            break
    return None


def _format_fpga_impl_metric(d: dict, stale: bool) -> str | None:
    """Format FPGA implementation LUT/FF usage + optional timing, or None."""
    if stale:
        return "?"
    luts = d.get("lut_count")
    ffs = d.get("ff_count")
    wns = d.get("wns_ns")
    parts = []
    if luts is not None:
        parts.append(f"{luts / 1000:.1f}k LUTs" if luts >= 1000 else f"{luts} LUTs")
    if ffs is not None:
        parts.append(f"{ffs / 1000:.1f}k FFs" if ffs >= 1000 else f"{ffs} FFs")
    if wns is not None:
        parts.append(f"WNS {wns:.2f}ns")
    if parts:
        return " | ".join(parts)
    return None


def _format_synthesis_metric(d: dict, stale: bool) -> str | None:
    """Format synthesis cell count + fmax, or None."""
    if stale:
        return "?"
    cells = d.get("cells")
    # Fmax is per-clock now; the timing-worst clock is the representative number.
    fmax = worst_fmax_from_json(d.get("per_clock"))
    parts = []
    if cells is not None:
        if cells >= 1000:
            parts.append(f"{cells / 1000:.1f}k cells")
        else:
            parts.append(f"{cells} cells")
    if fmax is not None:
        parts.append(f"{fmax:.0f}MHz")
    if parts:
        return " · ".join(parts)
    return None


def _format_mutation_metric(d: dict) -> str | None:
    """Format mutation detection as ``12/20 (60%) / need 16``, or None."""
    detected = d.get("detected")
    total = d.get("total_valid")
    if detected is None or not total:
        return None
    min_det = d.get("min_detected")
    thr_str = f" / need {min_det}" if min_det else ""
    return f"{detected}/{total} ({detected / total * 100:.0f}%){thr_str}"


def _format_sim_metric(d: dict, stale: bool) -> str | None:
    """Format simulation as ``9/9 tests``, or None when the counts are absent."""
    passed = d.get("tests_passed")
    total = d.get("tests_total")
    if passed is None or not total:
        return None
    return "?" if stale else f"{passed}/{total} tests"


def _format_finding_count_metric(  # noqa: PLR0911
    key: str, d: dict, stale: bool
) -> str | None:
    """Format the two count-of-findings criteria families (lint, reviewer).

    Both read as ``clean`` at zero, so they share a branch; returns None when
    *key* is neither family or the count was never recorded.
    """
    if key.startswith("lint_clean"):
        if stale:
            return "?"
        warnings = d.get("warnings")
        return None if warnings is None else (f"{warnings} warnings" if warnings else "clean")

    if key.startswith("review_"):
        issues = d.get("issues")
        if issues is None:
            return None
        waived = sum(
            1
            for finding in d.get("resolved", [])
            if isinstance(finding, dict)
            and finding.get("status") in {"waived", "impasse_deferred"}
        )
        if key.endswith("_done"):
            return f"reviewed, {issues} findings"
        if issues:
            return f"{issues} open"
        return f"clean ({waived} waived)" if waived else "clean"

    return None


def format_criterion_metric(key: str, entry) -> str:  # noqa: PLR0911 — metric-type dispatch; each criterion kind is its own formatting branch/return
    """Extract a short metric string from a criterion's detail dict.

    Public so human-facing evidence renderers can show the same per-criterion
    metric as the terminal without duplicating metric interpretation.
    """
    d = entry.detail or {}
    p = entry.params or {}
    stale = getattr(entry, "stale", False)

    # Coverage criteria: pct (need threshold%)
    coverage = _format_coverage_metric(key, d, p, stale)
    if coverage is not None:
        return coverage

    # FPGA implementation: LUT/FF usage + optional timing.
    if key.startswith("fpga_impl_ok"):
        fpga = _format_fpga_impl_metric(d, stale)
        if fpga is not None:
            return fpga

    # Synthesis: cells + fmax
    if key.startswith("synthesis_ok"):
        synth = _format_synthesis_metric(d, stale)
        if synth is not None:
            return synth

    # Mutation score
    if key.startswith("mutation_score"):
        mutation = _format_mutation_metric(d)
        if mutation is not None:
            return mutation

    # Simulation: tests passed / total
    if key.startswith("sim_pass"):
        sim = _format_sim_metric(d, stale)
        if sim is not None:
            return sim

    # Lint warnings / reviewer issues
    finding_count = _format_finding_count_metric(key, d, stale)
    if finding_count is not None:
        return finding_count

    return ""


_COLLAPSIBLE_GROUPS = [
    ("coverage_", "coverage"),
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


def build_criteria_summary_lines(state_path: Path) -> tuple[list[str], str]:
    """Build per-criterion lines and a totals line for terminal display.

    Returns (criterion_lines, totals_line). Empty lists if state is unreadable.
    """
    from booley.dev_support.development_state import DevelopmentState
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
            invocation = planned_invocation(key, entry)
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
