"""Canonical evidence metrics stay compatible across Goal and Ticket renderers."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.table import Table

from booley.goals.format import format_criterion_metric, render_status
from booley.goals.status import GoalStatus, GoalStatusView
from booley.ticket_board.criteria_summary_format import format_criterion_metric as ticket_metric


@pytest.mark.parametrize(
    ("key", "detail", "stale", "expected"),
    [
        ("coverage_top", {"status": "fail"}, False, "fail"),
        ("coverage", {"status": "pass"}, True, "?"),
        ("coverage", {}, False, ""),
        (
            "fpga_impl_ok_top",
            {"lut_count": 1500, "ff_count": 900, "wns_ns": -0.125},
            False,
            "1.5k LUTs | 900 FFs | WNS -0.12ns",
        ),
        ("fpga_impl_ok", {"lut_count": 999, "ff_count": 2200}, False, "999 LUTs | 2.2k FFs"),
        ("fpga_impl_ok", {}, False, ""),
        ("fpga_impl_ok", {"lut_count": 1}, True, "?"),
        (
            "synthesis_ok_top",
            {"cells": 2500, "per_clock": {"fast": {"fmax_mhz": 250}, "slow": {"fmax_mhz": 99.6}}},
            False,
            "2.5k cells · 100MHz",
        ),
        ("synthesis_ok", {"cells": 12}, False, "12 cells"),
        ("synthesis_ok", {}, False, ""),
        ("synthesis_ok", {"cells": 12}, True, "?"),
        (
            "mutation_score_top",
            {"detected": 12, "total_valid": 20, "min_detected": 16},
            False,
            "12/20 (60%) / need 16",
        ),
        ("mutation_score", {"detected": 0, "total_valid": 10}, False, "0/10 (0%)"),
        ("mutation_score", {"total_valid": 0}, False, ""),
        ("sim_pass_top", {"tests_passed": 2, "tests_total": 3}, False, "2/3 tests"),
        ("sim_pass_top", {"tests_passed": 2, "tests_total": 3}, True, "?"),
        ("sim_pass_top", {"tests_total": 0}, False, ""),
        ("cycle_count_top", {"cycles": 1234}, False, "1,234 cycles"),
        (
            "cycle_count_top",
            {"cycles": 1234, "baseline_cycles": 1500},
            False,
            "1,500 → 1,234 cycles (-266)",
        ),
        ("cycle_count_top", {}, False, ""),
        ("cycle_count_top", {"cycles": 1234}, True, "?"),
        ("lint_clean_top", {"warnings": 0}, False, "clean"),
        ("lint_clean_top", {"warnings": 3}, False, "3 warnings"),
        ("lint_clean_top", {}, False, ""),
        ("lint_clean_top", {"warnings": 0}, True, "?"),
        ("review_rtl_bugs_done", {"issues": 2}, False, "reviewed, 2 findings"),
        ("review_rtl_bugs_clean", {"issues": 2}, False, "2 open"),
        ("review_rtl_bugs_clean", {"issues": 0}, False, "clean"),
        (
            "review_rtl_bugs_clean",
            {
                "issues": 0,
                "resolved": [
                    {"status": "waived"},
                    {"status": "impasse_deferred"},
                    {"status": "fixed"},
                    None,
                ],
            },
            False,
            "clean (2 waived)",
        ),
        ("review_rtl_bugs_clean", {}, False, ""),
        ("custom_gate", {"cells": 12}, False, ""),
    ],
)
def test_shared_metric_has_canonical_output(key, detail, stale, expected):
    entry = SimpleNamespace(detail=detail, params={}, stale=stale)
    assert format_criterion_metric(key, entry) == expected
    assert ticket_metric(key, entry) == expected


def test_status_defaults_to_summary_for_multiple_modes_and_preserves_warnings(goal_mode):
    first = GoalStatusView(
        goal_mode.record,
        (GoalStatus("lint_clean_top", "met", "clean"),),
        "WARNING: shared worktree",
        2,
    )
    second = replace(first, record=replace(goal_mode.record, id="other-mode"), warning="")
    assert render_status(()) == ""
    compact = render_status((first, second))
    assert f"{first.record.id} (active) · 1/1 met" in compact
    assert "other-mode (active) · 1/1 met" in compact
    assert "WARNING: shared worktree" in compact
    assert "Worktree:" not in compact
    assert "lint_clean_top" not in compact
    detailed = render_status((first, second), short=False)
    assert "lint_clean_top" in detailed
    assert "Pending proposals: 2" in detailed
    assert "Worktree:" in detailed


@pytest.mark.parametrize("status", ["unmet", "stale"])
@pytest.mark.parametrize("reason", ["", "contract requires the complete suite"])
def test_detailed_unmet_reason_and_compact_output(goal_mode, status, reason):
    view = GoalStatusView(
        goal_mode.record, (GoalStatus("sim_pass_top", status, "1/1 tests", reason),), "", 0
    )
    detailed = " ".join(render_status((view,), short=False).split())
    assert "1/1 tests" in detailed
    if status == "unmet" and reason:
        assert "1/1 tests — " + reason in detailed
    elif reason:
        assert reason not in detailed
    else:
        assert "—" not in detailed
    compact = render_status((view,), short=True)
    assert "1/1 tests" not in compact
    if reason:
        assert reason not in compact


@pytest.mark.parametrize("key", ["elab_pass", "elab_pass_top"])
@pytest.mark.parametrize(
    ("met", "stale", "expected"),
    [(True, False, "elaborated"), (True, True, "?"), (False, False, ""), (False, True, "")],
)
def test_elaboration_metric_requires_met_evidence(key, met, stale, expected):
    entry = SimpleNamespace(detail={}, params={}, met=met, stale=stale)
    assert format_criterion_metric(key, entry) == ticket_metric(key, entry) == expected


@pytest.mark.parametrize("key", ["elab_passenger", "custom_gate", "lint_clean_top"])
def test_met_metric_fallback_preserves_raw_empty_output(key):
    from booley.goals.format import format_met_goal_metric

    entry = SimpleNamespace(detail={}, params={}, met=True, stale=False)
    assert format_criterion_metric(key, entry) == ""
    assert format_met_goal_metric(key, entry) == "evidence recorded"


# Frozen verbatim from 9da903961; independent of the production renderer.
def baseline_render_status(views: tuple[GoalStatusView, ...], *, short: bool | None = None) -> str:
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
                evidence = goal.evidence_summary
                if goal.status == "unmet" and goal.reason:
                    evidence += f" — {goal.reason}"
                table.add_row(goal.key, goal.status, evidence)
            console.print(table)
            console.print(f"Pending proposals: {view.pending_proposals}")
            conflicts = dict(view.proposal_conflicts)
            for proposal_view in view.proposals:
                proposal = proposal_view.proposal
                console.print(
                    f"Proposal {proposal.id}: {proposal_view.state} · {proposal.kind.value} {proposal.goal_key} · {proposal.rationale}"
                )
                if proposal.id in conflicts:
                    console.print(f"  Conflict: {conflicts[proposal.id]}")
                if proposal_view.decision is not None:
                    console.print(
                        f"  {proposal_view.decision.source.value}: {proposal_view.decision.reason}"
                    )
            if view.interrupted_applies:
                console.print("Interrupted applies: " + ", ".join(view.interrupted_applies))
    return stream.getvalue().rstrip()


@pytest.mark.parametrize("short", [None, True, False])
@pytest.mark.parametrize("count", [0, 1, 2])
@pytest.mark.parametrize("long_path", [False, True])
def test_project_wide_prefix_preserves_baseline(goal_mode, short, count, long_path):
    record = replace(
        goal_mode.record,
        worktree_path=goal_mode.record.worktree_path + "/directory" * (20 if long_path else 0),
    )
    views = tuple(GoalStatusView(replace(record, id=f"mode-{i}"), (), "", 0) for i in range(count))
    expected = baseline_render_status(views, short=short)
    assert render_status(views, short=short) == expected
    prefix = "No Goal Mode is active here. Active Goal Modes in this Project:\n" if count else ""
    assert render_status(views, short=short, project_wide=True) == prefix + expected
