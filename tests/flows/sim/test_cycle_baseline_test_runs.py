"""A Cycle Count baseline runs the candidate's concrete Test Run (ADR 0072)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.execution import NamedTests, SelectedTestRuns
from booley.flows.sim.execution.engine import target_test_plan
from booley.flows.test_runs import UnseededTargetError
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow
from tests.flows.sim.test_test_run_launch import (
    _handle,
    _inspection,
    _plusargs,
    _preview,
    _project,
    _work,
)

_CANDIDATE = """
[sim]
tests = ["stress"]
select = "+test={name}"
default_seed = 7

[sim.variants.stress]
default = "soak"
short = ["+iters=10"]
soak = { args = ["+iters=900"], env = { MODE = "soak" } }
"""

# The baseline revision declares another default Test Variant and Default Seed.
_BASELINE = """
[sim]
tests = ["stress"]
select = "+test={name}"

[sim.variants.stress]
default = "short"
short = ["+iters=10"]
soak = ["+iters=5"]
"""


def test_baseline_checkout_launches_the_candidates_test_run(tmp_path: Path):
    candidate = _project(tmp_path / "candidate", _CANDIDATE)
    baseline = _project(tmp_path / "baseline", _BASELINE)
    inspection = _inspection("verilator")
    run = target_test_plan(_handle(candidate), "verilator", {}).run("stress")
    assert run.name == "stress+soak@7"

    on_candidate = _preview(candidate, inspection, SelectedTestRuns((run,)))
    on_baseline = _preview(baseline, inspection, SelectedTestRuns((run,)))
    own_default = _preview(baseline, inspection, NamedTests(("stress",)))

    expected = ["test=stress", "iters=900", "verilator+seed+7"]
    assert _plusargs(on_candidate.commands[0]) == expected
    assert _plusargs(on_baseline.commands[0]) == expected
    assert on_baseline.run_groups == ((run,),)
    launched = _work(baseline, inspection, ("stress",), (run,))
    assert launched.environment == (("MODE", "soak"),)
    # What the baseline would have run from its own tests.toml, and must not.
    assert _plusargs(own_default.commands[0]) == ["test=stress", "iters=10", "verilator+seed+1"]


def test_baseline_that_cannot_apply_the_seed_refuses_instead_of_dropping_it(tmp_path: Path):
    candidate = _project(tmp_path / "candidate", _CANDIDATE)
    baseline = _project(tmp_path / "baseline", _BASELINE)
    run = target_test_plan(_handle(candidate), "verilator", {}).run("stress")
    with pytest.raises(UnseededTargetError) as excinfo:
        _preview(baseline, _inspection("icarus"), SelectedTestRuns((run,)))
    assert str(excinfo.value).startswith(
        "Test Run 'stress+soak@7' has a seed, but Target 'sim' is unseeded"
    )


def test_flow_freezes_the_candidates_run_for_each_baseline_test(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")

    flow._freeze_baseline_test_runs((("sim", "sim_ref", "stress"), ("sim", "sim", "reset")), _MAP)

    selection = flow._baseline_selection("sim_ref", ("stress",))
    assert isinstance(selection, SelectedTestRuns)
    assert [run.name for run in selection.runs] == ["stress+soak@9"]
    # The candidate does not run "reset" in this invocation: the baseline
    # checkout resolves that Test itself.
    assert flow._baseline_selection("sim", ("reset",)) == NamedTests(("reset",))
    assert flow._baseline_selection("other", ("stress",)) == NamedTests(("stress",))


def test_default_request_freezes_the_default_test_run(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    flow._freeze_baseline_test_runs((("sim", "sim", "stress"),), _MAP)
    selection = flow._baseline_selection("sim", ("stress",))
    assert isinstance(selection, SelectedTestRuns)
    assert [run.name for run in selection.runs] == ["stress+short@1"]


def test_campaign_planning_runs_no_baseline_for_a_refused_test_run(tmp_path, monkeypatch):
    """A baseline that cannot run the Test Run gets no campaign and no prerequisite."""
    from booley.flows.target_test_plan import build_target_test_plan
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_cycle_observation import _criterion_flow, _pin_baseline
    from tests.flows.test_baseline_worktree import _project_topology_checkout

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, "standalone")
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = root
    flow._target_handles = {"sim_core": TargetCatalog.build(root).select("sim_core")}
    _pin_baseline(flow, revision)

    def plan_of(target):
        # The candidate registers the compared Test; the baseline revision does not.
        tests = ["coremark"] if Path(flow.args.work_dir) == root else ["other"]
        return build_target_test_plan(target, {"tests": tests}, adapter="verilator")

    monkeypatch.setattr(flow, "_target_test_plan", plan_of)
    monkeypatch.setattr(
        flow, "_planned_baseline_request", lambda *_args: pytest.fail("baseline was planned")
    )
    try:
        requests, prerequisites = flow._plan_campaign_baselines(
            tmp_path / "invocation", None, ["sim_core"], {"sim_core": ["coremark"]}
        )
    finally:
        flow.context.publication_resources.close()

    assert (requests, prerequisites) == ([], {"sim_core": []})
    reason = flow._baseline_refusals[("sim_core", "coremark")]
    assert reason == (
        f"baseline revision {revision[:12]} does not register Test 'coremark' on Target 'sim_core'"
    )
    from booley.runtime.endpoint_execution import EndpointOutcome

    noted = flow._with_baseline_refusal_note(EndpointOutcome(report_text="sim: PASS"))
    assert noted.report_text.splitlines() == [
        "sim: PASS",
        f"Cycle Count comparison inconclusive for sim_core Test coremark: {reason}",
    ]


def test_campaign_criterion_names_why_the_comparison_is_inconclusive(tmp_path: Path):
    from booley.criteria.state import DevelopmentState
    from booley.criteria.templates import cycle_count_criterion_key
    from booley.flows.sim.acceptance import SimulationAcceptanceCoordinator
    from booley.flows.test_runs import build_test_run
    from tests.flows.sim.test_campaign_test_runs import _hdl_plan, _run_campaign

    run = build_test_run("count", seed=1)
    outcome = _run_campaign(tmp_path, _hdl_plan(tmp_path, ((run,),)))
    name, selector = str(outcome.target["name"]), str(outcome.target["selector"])
    key = cycle_count_criterion_key(name, "count")
    params = {
        "target": f"{outcome.target['vlnv']}#{name}",
        "_target_selector": selector,
        "test": "count",
        "cycle_count_increase_at_most": 5,
    }
    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({key: True}, criterion_params={key: params}, strict=True)

    def detail(refusals):
        (change,) = SimulationAcceptanceCoordinator._derive_changes(outcome, state, refusals)
        return change.met, change.detail

    assert detail({(selector, "count"): "baseline revision abc cannot apply the seed"}) == (
        False,
        {
            **detail({})[1],
            "baseline_observation": "inapplicable",
            "reason": "baseline revision abc cannot apply the seed",
        },
    )
    # Without a refusal a missing baseline keeps its own state and no reason.
    assert detail({})[1]["baseline_observation"] == "mismatched"
    assert "reason" not in detail({})[1]


def test_a_cocotb_baseline_without_a_registry_discovers_the_test():
    from booley.flows.sim.flow import baseline_test_run_refusal
    from booley.flows.target_test_plan import build_target_test_plan
    from booley.flows.test_runs import build_test_run

    run = build_test_run("stress", seed=7)
    discovering = build_target_test_plan("sim", {}, adapter="cocotb")
    registered = build_target_test_plan("sim", {"tests": ["reset"]}, adapter="cocotb")
    hdl = build_target_test_plan("sim", {}, adapter="verilator")

    assert baseline_test_run_refusal(discovering, "stress", run, "a" * 40) is None
    for plan in (registered, hdl):
        assert baseline_test_run_refusal(plan, "stress", run, "a" * 40) == (
            "baseline revision aaaaaaaaaaaa does not register Test 'stress' on Target 'sim'"
        )
