"""The Simulation Flow resolves a request to concrete Test Runs once (ADR 0072)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.sim.execution import DefaultSelection, SelectedTestRuns
from booley.flows.sim.flow import SimulateFlow
from booley.flows.target_test_plan import build_target_test_plan
from booley.flows.test_runs import UnseededTargetError

_TESTS = {
    "sim": {
        "tests": ["reset", "stress"],
        "skip": ["reset"],
        "variants": {"stress": {"default": "short", "short": [], "soak": ["+iters=9"]}},
    }
}
_MAP = {"sim": ["reset", "stress"]}


def _flow(tmp_path: Path, monkeypatch, *argv: str, adapter: str = "verilator") -> SimulateFlow:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim", *argv])
    plan = build_target_test_plan("sim", normalize_tests_toml(_TESTS)["sim"], adapter=adapter)
    monkeypatch.setattr(flow, "_target_test_plan", lambda _target: plan)
    monkeypatch.setattr(flow, "_effective_skips", lambda _target: {"reset"})
    return flow


def _names(flow: SimulateFlow) -> list[str]:
    return [run.name for run in flow._resolve_test_runs("sim", _MAP)]


def test_default_request_runs_default_variants_at_the_default_seed(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    assert _names(flow) == ["stress+short@1"]
    assert flow._resolve_tests_to_run("sim", _MAP) == ["stress"]


def test_selectors_keep_order_and_override_skip(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9", "--test", "reset")
    assert _names(flow) == ["stress+soak@9", "reset@1"]
    selection = flow._test_run_selection("sim", _MAP)
    assert isinstance(selection, SelectedTestRuns)
    assert [run.args for run in selection.runs] == [("+iters=9",), ()]


def test_random_seeds_are_drawn_once_per_invocation(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--seed", "random", "--test", "stress", "--test", "reset")
    first = _names(flow)
    assert _names(flow) == first
    assert flow._test_run_selection("sim", _MAP).runs == flow._resolve_test_runs("sim", _MAP)
    seeds = [run.seed for run in flow._resolve_test_runs("sim", _MAP)]
    assert all(seed is not None and seed >= 1 for seed in seeds)


def test_count_selector_resolves_to_distinct_seeds(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress@x4")
    runs = flow._resolve_test_runs("sim", _MAP)
    assert len({run.seed for run in runs}) == 4
    assert {run.variant for run in runs} == {"short"}
    # The repeated Test is named once where only Test names are wanted.
    assert flow._resolve_tests_to_run("sim", _MAP) == ["stress"]


def test_unseeded_target_refuses_a_seed_before_anything_runs(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--seed", "3", adapter="icarus")
    with pytest.raises(UnseededTargetError):
        flow._resolve_test_runs("sim", _MAP)
    outcome = flow._validate_test_plans(["sim"])
    assert outcome is not None and "is unseeded" in outcome.report_text


def test_all_skipped_suite_resolves_to_no_runs(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    monkeypatch.setattr(flow, "_effective_skips", lambda _target: {"reset", "stress"})
    assert flow._resolve_test_runs("sim", _MAP) == ()
    assert isinstance(flow._test_run_selection("sim", _MAP), DefaultSelection)


@pytest.mark.parametrize(
    "argv",
    [("--test", "stress+soak"), ("--test", "stress@7"), ("--seed", "7"), ("--test", "stress@x2")],
)
def test_coverage_of_other_test_runs_is_diagnostic_until_goals_name_them(
    tmp_path, monkeypatch, argv
):
    flow = _flow(tmp_path, monkeypatch, "--coverage", *argv)
    assert flow._validate_test_plans(["sim"], _MAP) is None
    # Interim Goal guard: such a campaign runs, and records no Goal evidence.
    assert flow.args.diagnostic is True
    assert all(name.startswith("sim: stress") for name in flow._test_run_guard_names)


@pytest.mark.parametrize(
    "argv", [(), ("--test", "stress"), ("--test", "stress+short@1"), ("--seed", "1")]
)
def test_coverage_of_default_test_runs_keeps_its_goal_evidence(tmp_path, monkeypatch, argv):
    flow = _flow(tmp_path, monkeypatch, "--coverage", *argv)
    assert flow._validate_test_plans(["sim"], _MAP) is None
    assert flow.args.diagnostic is False
    assert not getattr(flow, "_test_run_guard_names", ())
    assert tuple(flow.args.test or ()) in {(), ("stress",)}
    assert flow.args.seed is None or not flow.args.test
