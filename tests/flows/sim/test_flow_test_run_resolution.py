"""The Simulation Flow resolves a request to concrete Test Runs once (ADR 0072)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.execution_persistence import StandaloneFlowExecution
from booley.flows.frozen_test_runs import FrozenTestRuns, GoalTestRuns, SeedGroup
from booley.flows.sim.execution import DefaultSelection, SelectedTestRuns
from booley.flows.sim.flow import SimulateFlow
from booley.flows.target_test_plan import build_target_test_plan
from booley.flows.test_runs import UnseededTargetError, build_test_run, test_run_document
from booley.runtime.endpoint_execution import EndpointOutcome

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


# --- Inside Goal Mode: the Goals' frozen Test Runs (ADR 0072) -------------------------


class _GoalAdapter(StandaloneFlowExecution):
    """A Goal execution adapter as far as the Flow reads it: frozen Test Runs."""

    def __init__(self, frozen: FrozenTestRuns) -> None:
        self._frozen = frozen
        self.reads = 0

    def frozen_test_runs(self) -> FrozenTestRuns:
        self.reads += 1
        return self._frozen


def _goal_flow(tmp_path, monkeypatch, frozen: FrozenTestRuns, *argv: str) -> SimulateFlow:
    flow = _flow(tmp_path, monkeypatch, *argv)
    flow.context.configure_flow_execution(_GoalAdapter(frozen))
    return flow


def _frozen(*goals: GoalTestRuns) -> FrozenTestRuns:
    return FrozenTestRuns(tuple(goals))


def _run(test: str, variant: str | None = None, seed: int | None = 1, args=()):
    return build_test_run(test, variant=variant, seed=seed, args=args)


_SUITE = GoalTestRuns(
    "sim_pass_sim", "simulation", "sim", (_run("stress", "soak", 5, ("+iters=9",)),), suite=True
)
_SEEDS = GoalTestRuns(
    "sim_pass_sim_run_x",
    "simulation",
    "sim",
    tuple(_run("stress", "short", seed) for seed in (11, 12, 13)),
    seed_groups=(SeedGroup("stress", "short", (11, 12, 13)),),
)


def test_target_run_under_a_target_goal_runs_its_frozen_test_runs(tmp_path, monkeypatch):
    """The frozen runs, then the current default of every Test not frozen yet."""
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SUITE))
    monkeypatch.setattr(flow, "_effective_skips", lambda _target: set())
    # ``stress`` is frozen at +soak@5 although its default is +short@1 now;
    # ``reset`` was not frozen and runs as its current default.
    assert _names(flow) == ["stress+soak@5", "reset@1"]
    assert flow._test_runs.fresh_seed_selectors == {}
    assert flow.context.execution_adapter.reads == 1  # read once per invocation


def test_frozen_run_whose_variant_is_gone_or_skipped_does_not_run(tmp_path, monkeypatch):
    gone = replace(_SUITE, runs=(_run("stress", "removed", 5), _run("reset")))
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(gone))
    # ``reset`` is skipped now and ``stress+removed`` no longer exists: neither runs.
    # Nothing frozen can run, so the Target's current default suite runs; it matches no
    # frozen Test Run and is diagnostic.
    assert flow._resolve_test_runs("sim", _MAP) == flow._target_test_plan("sim").resolve(None)


def test_multi_seed_selector_runs_the_frozen_seeds_of_the_matching_goal(tmp_path, monkeypatch):
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SEEDS), "--test", "stress@x3")
    assert _names(flow) == ["stress+short@11", "stress+short@12", "stress+short@13"]
    assert flow._test_runs.fresh_seed_selectors == {}
    # The written default Variant names the same Goal.
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SEEDS), "--test", "stress+short@x3")
    assert _names(flow) == ["stress+short@11", "stress+short@12", "stress+short@13"]


@pytest.mark.parametrize("selector", ["stress@x2", "stress+soak@x3"])
def test_multi_seed_selector_no_goal_names_draws_new_seeds_and_is_diagnostic(
    tmp_path, monkeypatch, selector
):
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SEEDS), "--test", selector)
    runs = flow._resolve_test_runs("sim", _MAP)
    assert len({run.seed for run in runs}) == len(runs)
    assert flow._test_runs.fresh_seed_selectors == {"sim": (selector,)}
    result = flow._with_diagnostic_test_run_note(EndpointOutcome(report_text="sim: ran"))
    assert "no Goal names these multi-seed Test Runs" in result.report_text
    assert result.detail["diagnostic_test_runs"]["fresh_seed_selectors"] == [f"sim: {selector}"]


def test_other_selectors_and_seed_requests_resolve_from_the_current_tests_toml(
    tmp_path, monkeypatch
):
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SUITE, _SEEDS), "--test", "stress")
    assert _names(flow) == ["stress+short@1"]
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SUITE, _SEEDS), "--seed", "7")
    assert _names(flow) == ["stress+short@7"]
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SEEDS))  # no suite Goal on the Target
    assert _names(flow) == ["stress+short@1"]


def test_failing_diagnostic_test_run_is_reported_prominently(tmp_path, monkeypatch):
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SUITE), "--test", "stress")
    frozen_row = {**test_run_document(_SUITE.runs[0]), "grade": "fail"}
    other_row = {**test_run_document(_run("stress", "short")), "grade": "fail"}
    passing = {**test_run_document(_run("reset")), "grade": "pass"}
    result = EndpointOutcome(
        report_text="sim: ran",
        detail={"campaigns": {"sim": {"test_runs": [frozen_row, other_row, passing]}}},
    )
    flow._with_diagnostic_test_run_note(result)
    # The frozen run is Goal evidence, not diagnostic, whatever its grade.
    assert result.detail["diagnostic_test_runs"] == {
        "test_runs": ["sim: stress+short@1", "sim: reset@1"],
        "failures": ["sim: stress+short@1"],
        "fresh_seed_selectors": [],
    }
    assert "Diagnostic failures (they block no Goal):\n  sim: stress+short@1" in (
        result.report_text
    )


def test_outside_goal_mode_nothing_is_called_diagnostic(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress@x2")
    result = EndpointOutcome(report_text="sim: ran")
    assert flow._with_diagnostic_test_run_note(result).report_text == "sim: ran"
    assert "diagnostic_test_runs" not in result.detail
