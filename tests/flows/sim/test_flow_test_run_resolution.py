"""The Simulation Flow resolves a request to concrete Test Runs once (ADR 0072)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.criteria.presentation import CriterionPresentation
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

    def present_criterion(self, key: str, entry: object) -> CriterionPresentation:
        """The public Goal name the execution owner pins for *key*."""
        return CriterionPresentation(
            f"SIM:{key.removeprefix('sim_pass_')}", "tests pass", "", "", ""
        )


def _goal_flow(tmp_path, monkeypatch, frozen: FrozenTestRuns, *argv: str) -> SimulateFlow:
    flow = _flow(tmp_path, monkeypatch, *argv)
    flow.configure_flow_execution(_GoalAdapter(frozen))
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
    assert flow.execution_adapter.reads == 1  # read once per invocation


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


def test_direct_recording_gives_a_test_goal_only_its_matching_runs(tmp_path, monkeypatch):
    """Outside a campaign too, a Goal that names a selector takes just its frozen runs."""
    from unittest.mock import MagicMock

    from booley.flows.sim.flow import TargetResult, TestResult

    pinned = GoalTestRuns(
        "sim_pass_sim_run_x_s9", "simulation", "sim", (_run("stress", "soak", 9),)
    )
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SUITE, pinned, _SEEDS))
    flow.set_criterion = MagicMock()

    def result(run, passed: bool) -> TestResult:
        return TestResult(name=run.test, passed=passed, test_run=run)

    target = TargetResult(
        target="sim",
        passed=False,
        tests=[result(pinned.runs[0], True), result(_run("stress", "short", 1), False)],
    )
    flow._record_test_goal_criteria(target)

    # The suite Goal is recorded under ``sim_pass_sim`` elsewhere, and no frozen
    # seed of the multi-seed Goal ran: only the pinned Goal gets a change.
    flow.set_criterion.assert_called_once()
    assert flow.set_criterion.call_args.args == ("sim_pass_sim_run_x_s9", True)
    detail = flow.set_criterion.call_args.kwargs["detail"]
    assert [row["name"] for row in detail["test_runs"]] == ["stress+soak@9"]
    assert (detail["tests_passed"], detail["tests_total"]) == (1, 1)

    standalone = _flow(tmp_path, monkeypatch)
    standalone.set_criterion = MagicMock()
    standalone._record_test_goal_criteria(target)
    standalone.set_criterion.assert_not_called()


def _cycle_goal_flow(tmp_path, monkeypatch, goal: GoalTestRuns) -> SimulateFlow:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    flow = _goal_flow(tmp_path, monkeypatch, _frozen(goal))
    flow.args.state_file = "state.json"
    entry = SimpleNamespace(params={"target": "sim", "test": "stress", "cycle_count_max": 900})
    flow._state = SimpleNamespace(criteria={goal.key: entry})
    flow.set_criterion = MagicMock()
    return flow


def test_direct_cycle_count_goal_is_graded_from_its_frozen_run_not_the_bare_test(
    tmp_path, monkeypatch
):
    """A fast default-seed run of the Test never lends its Cycle Count to the pinned Goal."""
    from booley.flows.sim.flow import TargetResult, TestResult

    pinned = _run("stress", "short", 7)
    goal = GoalTestRuns("cycle_count_sim_x", "cycle_count", "sim", (pinned,))

    def result(run, cycles: int) -> TestResult:
        return TestResult(
            name=run.test, passed=True, cycles=cycles, cycle_status="observed", test_run=run
        )

    # Only the default-seed run ran: no evidence for the Goal at all.
    flow = _cycle_goal_flow(tmp_path, monkeypatch, goal)
    default = TargetResult(
        target="sim", passed=True, tests=[result(_run("stress", "short", 1), 9)]
    )
    flow._record_cycle_count_criteria(default)
    flow.set_criterion.assert_not_called()

    # Several Test Runs of the Test ran: the Goal is graded from its frozen one.
    both = TargetResult(
        target="sim",
        passed=True,
        tests=[result(pinned, 1500), result(_run("stress", "short", 1), 9)],
    )
    flow._record_cycle_count_criteria(both)
    flow.set_criterion.assert_called_once()
    detail = flow.set_criterion.call_args.kwargs["detail"]
    assert detail["cycles"] == 1500
    assert [row["name"] for row in detail["test_runs"]] == ["stress+short@7"]


def test_direct_recording_enters_a_coverage_goals_matching_failure_as_outcomes_only(
    tmp_path, monkeypatch
):
    from unittest.mock import MagicMock

    from booley.flows.frozen_test_runs import OUTCOMES_ONLY_DETAIL_KEY
    from booley.flows.sim.flow import TargetResult, TestResult

    run = _run("stress", "soak", 5)
    goal = GoalTestRuns("coverage_sim", "coverage", "sim", (run,))
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(goal))
    flow.set_criterion = MagicMock()
    failed = TestResult(name="stress", passed=False, test_run=run)
    flow._record_test_goal_criteria(TargetResult(target="sim", passed=False, tests=[failed]))

    flow.set_criterion.assert_called_once()
    assert flow.set_criterion.call_args.args == ("coverage_sim", False)
    detail = flow.set_criterion.call_args.kwargs["detail"]
    assert detail[OUTCOMES_ONLY_DETAIL_KEY] is True
    assert [(row["name"], row["grade"]) for row in detail["test_runs"]] == [
        ("stress+soak@5", "fail")
    ]


def test_a_goal_written_with_the_target_identity_is_not_listed_as_diagnostic(
    tmp_path, monkeypatch
):
    """A campaign keyed by selector matches a Goal that names the Target by its identity."""
    by_identity = replace(_SUITE, target="lib:ip:core#sim")
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(by_identity))
    monkeypatch.setattr(flow, "_target_names", lambda target: {target, f"lib:ip:core#{target}"})
    frozen_row = {**test_run_document(_SUITE.runs[0]), "grade": "fail"}
    result = EndpointOutcome(
        report_text="sim: ran", detail={"campaigns": {"sim": {"test_runs": [frozen_row]}}}
    )
    flow._with_diagnostic_test_run_note(result)
    assert "diagnostic_test_runs" not in result.detail
    assert result.report_text == "sim: ran"


def test_every_published_manifest_is_registered_with_the_goal_run_before_it_runs(
    tmp_path, monkeypatch
):
    """A campaign that never returns an outcome must already be known to its Goals."""
    from booley.flows.campaign_results import StartedCampaign
    from booley.flows.sim import flow as flow_module

    class Registry(_GoalAdapter):
        def __init__(self) -> None:
            super().__init__(_frozen(_SUITE))
            self.campaigns: list[StartedCampaign] = []

        def register_campaign(self, campaign: StartedCampaign) -> None:
            self.campaigns.append(campaign)

    def started(path: Path) -> StartedCampaign:
        return StartedCampaign(path, "id", "sha256:" + "0" * 64, ("sim",), True)

    monkeypatch.setattr(flow_module, "started_campaign", started)
    manifest = tmp_path / "campaign" / "manifest.json"

    flow = _flow(tmp_path, monkeypatch)
    registry = Registry()
    flow.configure_flow_execution(registry)
    published: list[Path] = []
    publish = flow._manifest_publisher(published)
    publish(manifest)
    publish(manifest)  # a resume names the path again: registered once per invocation
    assert [item.manifest_path for item in registry.campaigns] == [manifest.resolve()]

    diagnostic = _flow(tmp_path, monkeypatch, "--diagnostic")
    silent = Registry()
    diagnostic.configure_flow_execution(silent)
    diagnostic._manifest_publisher([])(manifest)
    assert silent.campaigns == []
    # Outside Goal Mode nothing wants to know.
    _flow(tmp_path, monkeypatch)._manifest_publisher([])(manifest)


def test_direct_cycle_count_goal_compares_with_the_baseline_of_its_frozen_run(
    tmp_path, monkeypatch
):
    """Baseline-relative: the baseline observation is of the Goal's frozen Test Run too."""
    from booley.flows.sim.flow import TargetResult, TestResult

    pinned, default = _run("stress", "short", 7), _run("stress", "short", 1)
    goal = GoalTestRuns("cycle_count_sim_x", "cycle_count", "sim", (pinned,))
    flow = _cycle_goal_flow(tmp_path, monkeypatch, goal)
    flow._state.criteria[goal.key].params = {
        "target": "sim",
        "test": "stress",
        "cycle_count_reduce_at_least": "5%",
    }

    def result(run, cycles: int) -> TestResult:
        return TestResult(
            name=run.test, passed=True, cycles=cycles, cycle_status="observed", test_run=run
        )

    # On the baseline the default-seed run is slow and the pinned one is not: only the
    # pinned run may be compared with the candidate's pinned run.
    flow._baseline_results = {
        "sim": TargetResult(
            target="sim", passed=True, tests=[result(default, 9000), result(pinned, 100)]
        )
    }
    candidate = TargetResult(
        target="sim", passed=True, tests=[result(default, 10), result(pinned, 99)]
    )
    flow._record_cycle_count_criteria(candidate)

    flow.set_criterion.assert_called_once()
    assert flow.set_criterion.call_args.args[0] == goal.key
    detail = flow.set_criterion.call_args.kwargs["detail"]
    assert (detail["cycles"], detail["baseline_cycles"]) == (99, 100)


def test_a_coverage_campaign_collected_on_its_own_is_registered_before_it_runs(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from booley.flows.campaign_results import StartedCampaign

    class Registry(_GoalAdapter):
        def __init__(self) -> None:
            super().__init__(_frozen(_SUITE))
            self.campaigns: list[StartedCampaign] = []

        def register_campaign(self, campaign: StartedCampaign) -> None:
            self.campaigns.append(campaign)

    target = SimpleNamespace(handle=SimpleNamespace(selector="sim", identity="lib:ip:core#sim"))
    flow = _flow(tmp_path, monkeypatch)
    registry = Registry()
    flow.configure_flow_execution(registry)
    flow._register_coverage_campaign(target, tmp_path / "reports" / "sim" / "1")
    (started,) = registry.campaigns
    assert started.manifest_path == tmp_path / "reports/sim/1/targets/sim/coverage.json"
    assert started.target_names == ("sim", "lib:ip:core#sim") and started.candidate

    diagnostic = _flow(tmp_path, monkeypatch, "--diagnostic")
    silent = Registry()
    diagnostic.configure_flow_execution(silent)
    diagnostic._register_coverage_campaign(target, tmp_path / "reports" / "sim" / "2")
    assert silent.campaigns == []


def test_a_diagnostic_run_names_the_goals_whose_frozen_runs_it_ran(tmp_path, monkeypatch):
    """``--diagnostic`` records nothing: the report says whose frozen runs ran and failed."""
    flow = _goal_flow(tmp_path, monkeypatch, _frozen(_SUITE), "--diagnostic")
    frozen_row = {**test_run_document(_SUITE.runs[0]), "grade": "fail"}
    other = {**test_run_document(_run("reset")), "grade": "fail"}
    result = EndpointOutcome(
        report_text="sim: ran", detail={"campaigns": {"sim": {"test_runs": [frozen_row, other]}}}
    )
    flow._with_diagnostic_test_run_note(result)
    assert result.detail["diagnostic_test_runs"] == {
        "goal_frozen_runs": {"sim_pass_sim": ["stress+soak@5"]},
        "goal_failures": {"sim_pass_sim": ["stress+soak@5"]},
    }
    assert "these frozen Test Runs of Goals ran and nothing was recorded" in result.report_text
    # The report names the Goal by its public name; only ``detail`` keeps the key.
    assert result.report_text == (
        "sim: ran\n"
        "Diagnostic run: these frozen Test Runs of Goals ran and nothing was recorded:\n"
        "  SIM:sim (tests pass): stress+soak@5\n"
        "Diagnostic failures of Goals' frozen Test Runs (not recorded):\n"
        "  SIM:sim (tests pass): stress+soak@5"
    )
    assert "sim_pass_sim" not in result.report_text
    # A diagnostic run of other Test Runs only says nothing more.
    plain = EndpointOutcome(
        report_text="sim: ran", detail={"campaigns": {"sim": {"test_runs": [other]}}}
    )
    assert flow._with_diagnostic_test_run_note(plain).report_text == "sim: ran"
