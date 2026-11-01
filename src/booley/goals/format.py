"""Shared evidence metrics and the Rich rendering of Goal status.

The Rich rendering is the agent-facing (MCP) text; ``booley goal status`` prints its
own terminal view (``booley.harness.goal_status_view``) from the same helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rich.console import Console
from rich.table import Table

from booley.criteria.presentation import implementation_baseline_comparison
from booley.evidence.timing import worst_fmax_from_json
from booley.goals.presentation import UNAVAILABLE_GOAL, goal_presentation, proposal_presentation
from booley.goals.test_run_resolution import SUITE_SLOT

if TYPE_CHECKING:
    from booley.goals.status import GoalStatus, GoalStatusView


def _format_coverage_metric(
    key: str, d: dict[str, Any], p: dict[str, Any], stale: bool
) -> str | None:
    """Format a Coverage Campaign status, or None for another Criterion family."""
    if key != "coverage" and not key.startswith("coverage_"):
        return None
    status = d.get("status")
    return "?" if stale else (str(status) if status else None)


def _format_status_metric(
    key: str, entry: Any, d: dict[str, Any], p: dict[str, Any], stale: bool
) -> str | None:
    """Format the status-only elaboration and Coverage Criterion families."""
    if key == "elab_pass" or key.startswith("elab_pass_"):
        if not getattr(entry, "met", False):
            return ""
        return "?" if stale else "elaborated"
    return _format_coverage_metric(key, d, p, stale)


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


def _format_implementation_metric(
    key: str, d: dict[str, Any], p: dict[str, Any], stale: bool
) -> str | None:
    """Format FPGA/synthesis usage and any relative baseline comparison, or None."""
    if key.startswith("fpga_impl_ok"):
        compact = _format_fpga_impl_metric(d, stale)
    elif key.startswith("synthesis_ok"):
        compact = _format_synthesis_metric(d, stale)
    else:
        return None
    if stale:
        return compact
    comparison = implementation_baseline_comparison(key, p, d)
    return "; ".join(part for part in (compact, comparison) if part) or None


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

    # Elaboration and Coverage Campaign status.
    coverage = _format_status_metric(key, entry, d, p, stale)
    if coverage is not None:
        return coverage

    # FPGA implementation / synthesis: compact usage summary plus, for relative
    # thresholds with recorded baseline evidence, the baseline -> current change.
    implementation = _format_implementation_metric(key, d, p, stale)
    if implementation is not None:
        return implementation

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


def format_met_goal_metric(key: str, entry: Any) -> str:
    """Present recorded met evidence even when the producer supplied no metric."""
    return format_criterion_metric(key, entry) or "evidence recorded"


# How many Test Runs, and how many rerun commands, one Goal lists before it
# only counts the rest: a multi-seed Goal may hold up to a thousand seeds.
_TEST_RUN_LIMIT = 16
_RERUN_LIMIT = 8
#: Printed above the views of the Project-wide fallback, by every status surface.
PROJECT_WIDE_PREFIX = "No Goal Mode is active here. Active Goal Modes in this Project:"


def goal_name(goal: GoalStatus) -> str:
    """The Goal's human name, or the unavailable-record placeholder."""
    return goal.presentation.text if goal.presentation else UNAVAILABLE_GOAL


def goal_evidence(goal: GoalStatus) -> str:
    """The evidence summary, with the reason an unmet Goal is unmet."""
    if goal.status == "unmet" and goal.reason:
        return f"{goal.evidence_summary} — {goal.reason}"
    return goal.evidence_summary


def test_run_lines(goal: GoalStatus) -> list[str]:
    """What status shows of a Goal's frozen Test Runs (ADR 0072); empty for other Goals.

    Every frozen run with its outcome (``passed``, ``failed``, ``missing``),
    the slots still pending, the runs frozen after entry, and for each
    failing run the command that runs exactly it again.
    """
    judgement = goal.test_runs
    if judgement is None:
        return []
    runs = judgement.runs
    counts = ", ".join(
        f"{sum(run.outcome == outcome for run in runs)} {outcome}"
        for outcome in ("passed", "failed", "missing")
        if any(run.outcome == outcome for run in runs)
    )
    shown = ", ".join(f"{run.name} {run.outcome}" for run in runs[:_TEST_RUN_LIMIT])
    more = f", and {len(runs) - _TEST_RUN_LIMIT} more" if len(runs) > _TEST_RUN_LIMIT else ""
    name = goal_name(goal)
    lines = [f"{name}: {len(runs)} Test Runs ({counts or 'none'}): {shown or '—'}{more}"]
    if judgement.pending:
        waiting = ", ".join(
            "the Target" if slot == SUITE_SLOT else slot for slot in judgement.pending
        )
        lines.append(f"  pending (does not exist yet): {waiting}")
    late = [run.name for run in runs if run.deferred]
    if late:
        lines.append(f"  resolved after entry: {', '.join(late)}")
    failed = judgement.failed
    lines += [f"  rerun {run.name}: {run.rerun}" for run in failed[:_RERUN_LIMIT]]
    if len(failed) > _RERUN_LIMIT:
        lines.append(f"  ... and {len(failed) - _RERUN_LIMIT} more failing Test Runs")
    if goal.sim_transitions is not None:
        from booley.goals.sim_transitions import transition_summary

        lines.append(f"{name}: baseline -> current: {transition_summary(goal.sim_transitions)}")
    return lines


def synth_baseline_lines(goal: GoalStatus) -> list[str]:
    """One line for a relative SYNTH Goal's baseline Job: pending, complete or unavailable."""
    if goal.synth_baseline is None:
        return []
    name = goal_name(goal)
    reason = goal.synth_baseline["reason"]
    detail = f" ({reason})" if reason and goal.synth_baseline["status"] == "unavailable" else ""
    return [f"{name}: baseline: {goal.synth_baseline['status']}{detail}"]


# Not a pytest test, although its name starts with ``test_``.
test_run_lines.__test__ = False  # type: ignore[attr-defined]


def test_run_section(view: GoalStatusView) -> list[str]:
    """The ``Test Runs`` heading and indented lines of *view*'s Goals; empty without any."""
    lines = [line for goal in view.goals for line in test_run_lines(goal)]
    return ["Test Runs", *(f"  {line}" for line in lines)] if lines else []


# Not a pytest test, although its name starts with ``test_``.
test_run_section.__test__ = False  # type: ignore[attr-defined]


def _print_test_runs(console: Console, view: GoalStatusView) -> None:
    """The frozen Test Runs of every Goal of *view* that is judged by them."""
    for line in test_run_section(view):
        console.print(line, soft_wrap=True)


def render_status(
    views: tuple[GoalStatusView, ...],
    *,
    short: bool | None = None,
    project_wide: bool = False,
) -> str:
    """Render long detail for one Goal Mode, short rows for several by default."""
    import io

    stream = io.StringIO()
    console = Console(file=stream, color_system=None, width=120, markup=False, emoji=False)
    if project_wide and views:
        console.print(PROJECT_WIDE_PREFIX)
    compact = len(views) != 1 if short is None else short
    for view in views:
        console.print(
            f"{view.record.id} ({view.record.state.value}) · {view.met}/{len(view.goals)} met"
        )
        if view.warning:
            console.print(view.warning)
        if compact:
            for goal in view.goals:
                name = goal_name(goal)
                console.print(f"  {name}: {goal.status}")
                for line in synth_baseline_lines(goal):
                    console.print(f"  {line}", soft_wrap=True)
                if goal.sim_transitions is not None:
                    from booley.goals.sim_transitions import transition_summary

                    console.print(
                        f"  {name}: baseline -> current: {transition_summary(goal.sim_transitions)}",
                        soft_wrap=True,
                    )
        else:
            _render_long_status(console, view)
    return stream.getvalue().rstrip()


def _render_long_status(console: Console, view: GoalStatusView) -> None:
    """Detailed Goal rows and recoverable proposal state."""
    console.print(f"Worktree: {view.record.worktree_path}")
    console.print(f"Branch: {view.record.branch}")
    # The Goal name and its status stay on one line however long the evidence is:
    # clients read a row as "<name> <status>".
    table = Table(box=None, padding=(0, 1))
    table.add_column("Goal", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Evidence")
    for goal in view.goals:
        table.add_row(goal_name(goal), goal.status, goal_evidence(goal))
    console.print(table)
    _print_test_runs(console, view)
    for line in (line for goal in view.goals for line in synth_baseline_lines(goal)):
        console.print(line, soft_wrap=True)
    console.print(f"Pending proposals: {view.pending_proposals}")
    for line in proposal_lines(view):
        console.print(line)


def proposal_lines(view: GoalStatusView) -> list[str]:
    """Proposal, conflict, decision and interrupted-apply lines of *view*."""
    lines: list[str] = []
    conflicts = dict(view.proposal_conflicts)
    for proposal_view in view.proposals:
        proposal = proposal_view.proposal
        after = goal_presentation(proposal.after).text
        change = (
            f"{goal_presentation(proposal.before).text} → {after}"
            if proposal.before is not None
            else after
        )
        lines.append(
            f"Proposal {proposal.id}: {proposal_view.state} · {proposal.kind.value} {change} · {proposal.rationale}"
        )
        if effect := proposal_presentation(proposal_view).get("review_retarget_effect"):
            lines.append(f"  {effect}")
        if proposal.id in conflicts:
            lines.append(f"  Conflict: {conflicts[proposal.id]}")
        if proposal_view.decision is not None:
            lines.append(
                f"  {proposal_view.decision.source.value}: {proposal_view.decision.reason}"
            )
    if view.interrupted_applies:
        lines.append("Interrupted applies: " + ", ".join(view.interrupted_applies))
    return lines
