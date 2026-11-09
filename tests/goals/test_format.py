"""Canonical evidence metrics stay compatible in Goal rendering."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from booley.goals.format import format_criterion_metric, render_status
from booley.goals.status import GoalStatus, GoalStatusView

REVISION = "0123456789abcdef0123456789abcdef01234567"


def _delta_check(param, baseline, current):
    """A relative check exactly as ``DevelopmentState._check_delta`` records it."""
    return {
        "param": param,
        "pass": True,
        "pct": (current - baseline) / baseline * 100,
        "threshold": 5,
        "current": current,
        "baseline": baseline,
    }


RELATIVE_CASES = {
    "synth": (
        "synthesis_ok_top",
        {"area_reduce_at_least": 8},
        {
            "cells": 2500,
            "per_clock": {"clk": {"fmax_mhz": 100}},
            "_metric_map": {"area": "area_um2"},
            "checks": [_delta_check("area_reduce_at_least", 1200, 1050)],
        },
        "2.5k cells · 100MHz; baseline 0123456789ab → current: "
        "area 1,200 → 1,050 μm² (-150, -12.5%)",
    ),
    "fpga": (
        "fpga_impl_ok_top",
        {"lut_count_increase_at_most": 5, "clk_i.critical_path_ps_increase_at_most": 10},
        {
            "lut_count": 1500,
            "checks": [
                _delta_check("lut_count_increase_at_most", 1400, 1500),
                _delta_check("clk_i.critical_path_ps_increase_at_most", 4000, 4200),
            ],
        },
        "1.5k LUTs; baseline 0123456789ab → current: LUTs 1,400 → 1,500 (+100, +7.1%) · "
        "clk_i critical path 4,000 → 4,200 ps (+200, +5.0%)",
    ),
}


@pytest.mark.parametrize("family", RELATIVE_CASES)
def test_relative_implementation_metric_shows_baseline_current_and_delta(family):
    key, params, detail, expected = RELATIVE_CASES[family]
    entry = SimpleNamespace(
        detail={**detail, "_baseline_ref": REVISION}, params=params, stale=False
    )
    assert format_criterion_metric(key, entry) == expected


@pytest.mark.parametrize(
    ("params", "checks"),
    [
        ({"lut_count_max": 2000}, [{"param": "lut_count_max", "pass": True, "value": 1500}]),
        (
            {"area_reduce_at_least": 8},
            [{"param": "area_reduce_at_least", "skipped": True, "reason": "no baseline for area"}],
        ),
    ],
    ids=["absolute", "skipped"],
)
def test_implementation_metric_without_relative_baseline_is_unchanged(params, checks):
    entry = SimpleNamespace(
        detail={"lut_count": 1500, "_baseline_ref": REVISION, "checks": checks},
        params=params,
        stale=False,
    )
    assert format_criterion_metric("fpga_impl_ok_top", entry) == "1.5k LUTs"


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
    assert "Goal unavailable" in detailed
    assert "lint_clean_top" not in detailed
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
    assert format_criterion_metric(key, entry) == expected


@pytest.mark.parametrize("key", ["elab_passenger", "custom_gate", "lint_clean_top"])
def test_met_metric_fallback_preserves_raw_empty_output(key):
    from booley.goals.format import format_met_goal_metric

    entry = SimpleNamespace(detail={}, params={}, met=True, stale=False)
    assert format_criterion_metric(key, entry) == ""
    assert format_met_goal_metric(key, entry) == "evidence recorded"


@pytest.mark.parametrize("short", [None, True, False])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_project_wide_status_only_adds_the_prefix(goal_mode, short, count):
    views = tuple(
        GoalStatusView(replace(goal_mode.record, id=f"mode-{i}"), (), "", 0) for i in range(count)
    )
    plain = render_status(views, short=short)
    for index in range(count):
        assert f"mode-{index} (active) · 0/0 met" in plain
    prefix = "No Goal Mode is active here. Active Goal Modes in this Project:\n" if count else ""
    assert render_status(views, short=short, project_wide=True) == prefix + plain


def test_long_status_names_the_worktree_and_branch(goal_mode):
    """Agents read the long form's Worktree and Branch lines to locate the checkout."""
    record = replace(goal_mode.record, worktree_path="/work/goal-tree", branch="goal/lint-fix")
    lines = render_status((GoalStatusView(record, (), "", 0),), short=False).splitlines()
    assert "Worktree: /work/goal-tree" in lines
    assert "Branch: goal/lint-fix" in lines


def test_long_status_proposal_lines_are_frozen(goal_mode):
    """The agent-facing (MCP) text keeps its proposal, conflict, decision and apply lines."""
    from booley.goals.changes import Approval, ChangeKind
    from booley.goals.proposals import Decision, ProposalView, new_proposal

    spec = goal_mode.record.goals[0].spec
    proposal = replace(
        new_proposal(
            goal_mode.record,
            kind=ChangeKind.ADD,
            goal_key=spec.key,
            before=None,
            after=spec,
            after_arg=None,
            runtime_params={},
            rationale="add this Goal",
            session_key=None,
        ),
        id="p-1",
    )
    decision = Decision(
        "approve",
        "reviewed",
        Approval.AGENT_RECORDED,
        "approve",
        "2026-10-19T00:00:00Z",
        proposal.payload_digest,
        None,
    )
    view = GoalStatusView(
        goal_mode.record,
        (),
        "",
        1,
        (ProposalView(proposal, decision=decision),),
        (("p-1", "stale revision"),),
        ("op-1",),
    )
    lines = render_status((view,), short=False).splitlines()
    assert lines[-5:] == [
        "Pending proposals: 1",
        "Proposal p-1: pending · add LINT:top (no unwaived findings) · add this Goal",
        "  Conflict: stale revision",
        "  agent-recorded: reviewed",
        "Interrupted applies: op-1",
    ]
