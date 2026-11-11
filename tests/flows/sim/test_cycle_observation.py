from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import CriteriaTemplate
from booley.flows.sim.execution import SelectedTestRuns
from booley.flows.sim.flow import PlannedBaselines, PlannedTarget, SimulateFlow
from booley.flows.sim.mode import SimulationMode
from booley.flows.target_test_plan import build_target_test_plan
from booley.flows.test_runs import build_test_run
from booley.targets.catalog import TargetCatalog

_TARGET_IDENTITY = "vendor:library:core#sim_core"
_TARGET_SELECTOR = "sim_core"


_COREMARK_RUN = build_test_run("coremark", seed=1)


def _criterion_flow(*, relative: bool = False) -> tuple[SimulateFlow, str]:
    threshold = {"cycle_count_reduce_at_least": "5%"} if relative else {"cycle_count_max": 100}
    template = CriteriaTemplate.from_yaml(
        {"mandatory": {"cycle_count": [{"target": "sim_core", "test": "coremark", **threshold}]}}
    )
    state = DevelopmentState()
    state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))
    flow = SimulateFlow()
    flow._state = state
    flow._args = MagicMock(
        state_file="state.json",
        test=None,
        seed=None,
        work_dir=".",
        mode=SimulationMode.SIMULATE,
    )
    flow._target_handles = {
        "sim_core": MagicMock(
            identity="sim_core",
            selector="sim_core",
            project_root=Path().resolve(),
        )
    }
    flow.set_criterion = MagicMock()
    # Both revisions register the compared Test on a seeded Target.
    flow._target_test_plan = lambda target: build_target_test_plan(
        target, {"tests": ["coremark"]}, adapter="verilator"
    )
    return flow, next(iter(state.criteria))


def test_relative_cycle_campaign_defaults_omitted_baseline_to_candidate_target() -> None:
    flow, _key = _criterion_flow(relative=True)

    assert flow._campaign_baseline_requirements(["sim_core"]) == (
        ("sim_core", "sim_core", "coremark"),
    )


def _sealed_criterion_flow(*, relative: bool = False) -> tuple[SimulateFlow, str]:
    threshold = {"cycle_count_reduce_at_least": "5%"} if relative else {"cycle_count_max": 100}
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [{"target": _TARGET_SELECTOR, "test": "coremark", **threshold}]
            }
        }
    )
    expanded = template.expand([])
    params = template.expand_params([])
    from booley.targets.domain import TARGET_IDENTITY_PARAM, TARGET_SELECTOR_PARAM

    for values in params.values():
        values[TARGET_IDENTITY_PARAM] = _TARGET_IDENTITY
        values[TARGET_SELECTOR_PARAM] = _TARGET_SELECTOR
    state = DevelopmentState()
    state.init_criteria(expanded, criterion_params=params)
    flow = SimulateFlow()
    flow._state = state
    flow._args = MagicMock(state_file="state.json", test=None, work_dir=".")
    flow._target_handles = {
        _TARGET_SELECTOR: MagicMock(
            identity=_TARGET_IDENTITY,
            selector=_TARGET_SELECTOR,
            project_root=Path().resolve(),
        )
    }
    flow.set_criterion = MagicMock()
    # Both revisions register the compared Test on a seeded Target.
    flow._target_test_plan = lambda target: build_target_test_plan(
        target, {"tests": ["coremark"]}, adapter="verilator"
    )
    return flow, next(iter(state.criteria))


def _pin_baseline(flow: SimulateFlow, ref: str = "a" * 40) -> None:
    for entry in flow.state.criteria.values():
        entry.params["_baseline_ref"] = ref


def test_absolute_cycle_criterion_does_not_select_a_baseline(monkeypatch) -> None:
    flow, _key = _criterion_flow()
    resolve = MagicMock()
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", resolve)

    assert flow._cycle_baseline_selection(["sim_core"]) == (None, [], None)
    resolve.assert_not_called()


def test_relative_cycle_criteria_share_one_resolved_baseline(monkeypatch) -> None:
    flow, _key = _criterion_flow(relative=True)
    _pin_baseline(flow)
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    assert flow._cycle_baseline_selection(["sim_core"]) == (
        "b" * 40,
        ["sim_core"],
        None,
    )


def test_relative_cycle_criterion_uses_declared_baseline_target(monkeypatch) -> None:
    flow, key = _criterion_flow(relative=True)
    flow.state.criteria[key].params["_baseline_target"] = "sim_old"
    flow._target_handles["sim_old"] = MagicMock(
        identity="sim_old", selector="sim_old", project_root=Path().resolve()
    )
    _pin_baseline(flow)
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    assert flow._cycle_baseline_selection(["sim_core"]) == ("b" * 40, ["sim_old"], None)


def test_schema_four_relative_cycle_criterion_selects_callable_target(monkeypatch) -> None:
    flow, _key = _sealed_criterion_flow(relative=True)
    _pin_baseline(flow)
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    assert flow._cycle_baseline_selection(["sim_core"]) == (
        "b" * 40,
        ["sim_core"],
        None,
    )


def test_invalid_cycle_baseline_ref_is_actionable(monkeypatch) -> None:
    flow, _key = _criterion_flow(relative=True)
    _pin_baseline(flow, "missing")
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: None)

    ref, targets, error = flow._cycle_baseline_selection(["sim_core"])

    assert ref == "missing"
    assert targets == ["sim_core"]
    assert "cannot be resolved" in error


@pytest.mark.parametrize("topology", ["standalone", "linked"])
def test_cycle_baseline_planning_materializes_project_topologies(tmp_path, monkeypatch, topology):
    from tests.flows.test_baseline_worktree import (
        _assert_topology_baseline,
        _project_topology_checkout,
    )

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, topology)
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = root
    handle = TargetCatalog.build(root).select("sim_core", for_flow="sim")
    flow._target_handles = {"sim_core": handle}
    _pin_baseline(flow, revision)
    roots = []

    def preview(targets, tests, selections):
        assert selections == {"sim_core": SelectedTestRuns((_COREMARK_RUN,))}
        baseline = Path(flow.args.work_dir)
        _assert_topology_baseline(baseline, root, "sim_core")
        roots.append(baseline)
        assert targets == ["sim_core"]
        assert tests == {"sim_core": ["coremark"]}
        return [PlannedTarget("sim_core", handle, MagicMock(target_identity=handle.identity))], []

    monkeypatch.setattr(flow, "_preview_targets", preview)
    with flow.publication_resources:
        baselines, errors = flow._plan_baseline_targets(["sim_core"], {"sim_core": ["coremark"]})
        assert errors == []
        assert baselines is not None
        assert [planned.selector for planned in baselines.targets] == ["sim_core"]
        assert baselines.revision == revision
        assert baselines.worktree == roots[0]
        assert flow.args.work_dir == root
    assert roots and all(not path.exists() for path in roots)


def test_baseline_planning_reports_a_refused_selection_and_a_refused_checkout(monkeypatch):
    from booley.flows.baseline_worktree import BaselineWorktreeError

    flow, _key = _criterion_flow(relative=True)
    _pin_baseline(flow, "missing")
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: None)

    baselines, errors = flow._plan_baseline_targets(["sim_core"], {})

    assert baselines is None
    assert errors == ["sim: Cycle Count pinned baseline ref cannot be resolved to a commit"]

    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    def refuse(_revision):
        raise BaselineWorktreeError("checkout refused")

    monkeypatch.setattr(flow, "_shared_baseline_worktree", refuse)
    assert flow._plan_baseline_targets(["sim_core"], {}) == (
        None,
        ["sim: Cycle Count baseline planning failed: checkout refused"],
    )


def test_one_run_shares_one_baseline_checkout_across_consumers(tmp_path, monkeypatch):
    """Every baseline consumer of one ``sim`` run reuses one checkout (#1959).

    Campaign authorization keeps its checkout open until publication ends, so
    planning and execution used to open a second worktree for the same
    revision at the same path, and ``git worktree add`` refused it.
    """
    from tests.flows.test_baseline_worktree import _project_topology_checkout

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, "standalone")
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = root
    handle = TargetCatalog.build(root).select("sim_core", for_flow="sim")
    flow._target_handles = {"sim_core": handle}
    _pin_baseline(flow, revision)
    tests = {"sim_core": ["coremark"]}
    roots: list[Path] = []

    def preview(targets, _tests, selections=None):
        if selections is not None:  # the baseline's Test Runs, previewed in its checkout
            roots.append(Path(flow.args.work_dir))
        return [PlannedTarget("sim_core", handle, MagicMock(target_identity=handle.identity))], []

    def request(_planned, baselines, _tests, _invocation, _admission):
        roots.append(baselines.worktree)
        return object(), {"coremark@1": {"revision": revision}}

    monkeypatch.setattr(flow, "_preview_targets", preview)
    monkeypatch.setattr(flow, "_planned_baseline_request", request)
    with flow.publication_resources:
        authorized = flow._campaign_authorization_baselines(revision, ["sim_core"])
        planning = flow._plan_ordinary_campaigns(["sim_core"], tests)
        assert isinstance(planning.baselines, PlannedBaselines)
        requests, _ = flow._plan_campaign_baselines(tmp_path / "inv", None, planning)
        assert len(requests) == 1
        assert [item.identity for item in authorized] == [handle.identity]
        assert [item.project_root for item in authorized] == roots[:1]
        assert len(roots) == 2
        assert len(set(roots)) == 1
        assert roots[0].is_dir()
        assert flow.args.work_dir == root
    assert not roots[0].exists()
    assert not list((root / ".booley").glob(".baseline-wt-*"))
