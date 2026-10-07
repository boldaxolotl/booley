"""Shared evidence metrics and Rich rendering for Goal status."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rich.console import Console
from rich.table import Table

from booley.evidence.timing import worst_fmax_from_json

if TYPE_CHECKING:
    from booley.goals.status import GoalStatusView


def _format_coverage_metric(
    key: str, d: dict[str, Any], p: dict[str, Any], stale: bool
) -> str | None:
    """Format a Coverage Campaign status, or None for another Criterion family."""
    if key != "coverage" and not key.startswith("coverage_"):
        return None
    status = d.get("status")
    return "?" if stale else (str(status) if status else None)


def _format_fpga_impl_metric(d: dict[str, Any], stale: bool) -> str | None:
    """Format FPGA implementation LUT/FF usage + optional timing, or None."""
    if stale:
        return "?"
    luts = d.get("lut_count")
    ffs = d.get("ff_count")
    wns = d.get("wns_ns")
    parts: list[str] = []
    if luts is not None:
        parts.append(f"{luts / 1000:.1f}k LUTs" if luts >= 1000 else f"{luts} LUTs")
    if ffs is not None:
        parts.append(f"{ffs / 1000:.1f}k FFs" if ffs >= 1000 else f"{ffs} FFs")
    if wns is not None:
        parts.append(f"WNS {wns:.2f}ns")
    if parts:
        return " | ".join(parts)
    return None


def _format_synthesis_metric(d: dict[str, Any], stale: bool) -> str | None:
    """Format synthesis cell count + fmax, or None."""
    if stale:
        return "?"
    cells = d.get("cells")
    # Fmax is per-clock now; the timing-worst clock is the representative number.
    fmax = worst_fmax_from_json(d.get("per_clock"))
    parts: list[str] = []
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


def _format_mutation_metric(d: dict[str, Any]) -> str | None:
    """Format mutation detection as ``12/20 (60%) / need 16``, or None."""
    detected = d.get("detected")
    total = d.get("total_valid")
    if detected is None or not total:
        return None
    min_det = d.get("min_detected")
    thr_str = f" / need {min_det}" if min_det else ""
    return f"{detected}/{total} ({detected / total * 100:.0f}%){thr_str}"


def _format_sim_metric(d: dict[str, Any], stale: bool) -> str | None:
    """Format simulation as ``9/9 tests``, or None when the counts are absent."""
    passed = d.get("tests_passed")
    total = d.get("tests_total")
    if passed is None or not total:
        return None
    return "?" if stale else f"{passed}/{total} tests"


def _format_cycle_metric(d: dict[str, Any], stale: bool) -> str | None:
    """Format current and optional baseline Cycle Counts."""
    if stale:
        return "?"
    current = d.get("cycles")
    baseline = d.get("baseline_cycles")
    if current is None:
        return None
    if baseline is None:
        return f"{current:,} cycles"
    return f"{baseline:,} → {current:,} cycles ({current - baseline:+,})"


def _format_finding_count_metric(  # noqa: PLR0911
    key: str, d: dict[str, Any], stale: bool
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
            and cast("dict[str, Any]", finding).get("status") in {"waived", "impasse_deferred"}
        )
        if key.endswith("_done"):
            return f"reviewed, {issues} findings"
        if issues:
            return f"{issues} open"
        return f"clean ({waived} waived)" if waived else "clean"

    return None


def format_criterion_metric(key: str, entry: Any) -> str:  # noqa: PLR0911 — metric-type dispatch; each criterion kind is its own formatting branch/return
    """Extract a short metric string from a criterion's detail dict.

    Public so human-facing evidence renderers can show the same per-criterion
    metric as the terminal without duplicating metric interpretation.
    """
    d: dict[str, Any] = entry.detail or {}
    p: dict[str, Any] = entry.params or {}
    stale = getattr(entry, "stale", False)

    # Coverage Campaign status.
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
    if key.startswith("cycle_count_"):
        cycle = _format_cycle_metric(d, stale)
        if cycle is not None:
            return cycle

    if key.startswith("sim_pass"):
        sim = _format_sim_metric(d, stale)
        if sim is not None:
            return sim

    # Lint warnings / reviewer issues
    finding_count = _format_finding_count_metric(key, d, stale)
    if finding_count is not None:
        return finding_count

    return ""


def render_status(views: tuple[GoalStatusView, ...], *, short: bool | None = None) -> str:
    """Render long detail for one Goal Mode, short rows for several by default."""
    import io

    stream = io.StringIO()
    console = Console(file=stream, color_system=None, width=120, markup=False)
    compact = len(views) != 1 if short is None else short
    for view in views:
        console.print(
            f"{view.record.id} ({view.record.state.value}) · {view.met}/{len(view.goals)} met"
        )
        if view.warning:
            console.print(view.warning)
        if not compact:
            console.print(f"Worktree: {view.record.worktree_path}")
            console.print(f"Branch: {view.record.branch}")
            table = Table("Goal", "Status", "Evidence", box=None, padding=(0, 1))
            for goal in view.goals:
                table.add_row(goal.key, goal.status, goal.evidence_summary)
            console.print(table)
            console.print(f"Pending proposals: {view.pending_proposals}")
    return stream.getvalue().rstrip()
