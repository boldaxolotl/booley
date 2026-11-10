from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import CriteriaTemplate
from booley.flows.plan import FlowPlan, WorkUnitPlan, WorkUnitRole
from booley.flows.sim.execution import SelectedTestRuns
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.mode import SimulationMode
from booley.flows.target_test_plan import build_target_test_plan
from booley.flows.test_runs import build_test_run
from booley.targets.catalog import TargetCatalog

_TARGET_IDENTITY = "vendor:library:core#sim_core"
_TARGET_SELECTOR = "sim_core"


_COREMARK_RUN = build_test_run("coremark", seed=1)


def _plan_unit(
    role: WorkUnitRole,
    selector: str = _TARGET_SELECTOR,
    identity: str = _TARGET_IDENTITY,
    revision: str | None = None,
) -> WorkUnitPlan:
    return WorkUnitPlan(
        unit_id="sim-baseline-coremark",
        role=role,
        revision=revision,
        selector=selector,
        target_identity=identity,
        test_or_module_scope=("coremark",),
        eda_tool="verilator",
        timeout_ms=1_000,
    )


def test_simulation_plan_includes_baseline_before_candidate() -> None:
    flow, _key = _criterion_flow(relative=True)
    baseline = _plan_unit("baseline", revision="b" * 40)
    candidate = _plan_unit("candidate")
    flow._plan_cycle_count_baseline_units = MagicMock(return_value=([baseline], []))
    flow._plan_simulation_targets = MagicMock(return_value=([candidate], []))

    plan = flow._plan_simulation([_TARGET_SELECTOR], {_TARGET_SELECTOR: ["coremark"]})

    assert plan.work_units == (baseline, candidate)


def test_baseline_planning_uses_ephemeral_tree_and_reports_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = tmp_path
    baseline_ref = "b" * 40
    baseline = _plan_unit("baseline", identity="drifted", revision=baseline_ref)
    flow._cycle_baseline_selection = MagicMock(
        return_value=(baseline_ref, [_TARGET_SELECTOR], None)
    )
    flow._plan_simulation_targets = MagicMock(return_value=([baseline], ["preview failed"]))
    baseline_root = tmp_path / "baseline"

    @contextmanager
    def fake_baseline_worktree(project_root: Path, revision: str, *, paired_project):
        assert project_root == tmp_path
        assert revision == baseline_ref
        assert paired_project is flow._paired_project_baseline
        yield baseline_root

    monkeypatch.setattr("booley.flows.sim.flow.baseline_worktree", fake_baseline_worktree)

    with flow.publication_resources:
        units, errors = flow._plan_cycle_count_baseline_units(
            [_TARGET_SELECTOR],
            {_TARGET_SELECTOR: ["coremark"]},
        )

    assert units == [baseline]
    assert errors == [
        "preview failed",
        "sim_core: baseline resolves to 'drifted', expected 'sim_core'",
    ]
    assert flow.args.work_dir == tmp_path
    flow._plan_simulation_targets.assert_called_once_with(
        [_TARGET_SELECTOR],
        {_TARGET_SELECTOR: ["coremark"]},
        role="baseline",
        revision=baseline_ref,
        # The baseline runs the candidate's Test Run of the compared Test.
        selections={_TARGET_SELECTOR: SelectedTestRuns((_COREMARK_RUN,))},
    )


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
    assert flow._cycle_baseline_test_names({"sim_core": ["coremark"]}) == {
        "sim_core": ["coremark"],
        "sim_old": ["coremark"],
    }


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

    def preview(targets, tests, *, role, revision, selections):
        assert selections == {"sim_core": SelectedTestRuns((_COREMARK_RUN,))}
        baseline = Path(flow.args.work_dir)
        _assert_topology_baseline(baseline, root, "sim_core")
        roots.append(baseline)
        assert targets == ["sim_core"]
        assert tests == {"sim_core": ["coremark"]}
        return [_plan_unit(role, identity=handle.identity, revision=revision)], []

    monkeypatch.setattr(flow, "_plan_simulation_targets", preview)
    with flow.publication_resources:
        units, errors = flow._plan_cycle_count_baseline_units(
            ["sim_core"], {"sim_core": ["coremark"]}
        )
        assert errors == []
        assert units[0].target_identity == handle.identity
        assert units[0].revision == revision
        assert flow.args.work_dir == root
    assert roots and all(not path.exists() for path in roots)


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

    def plan(targets, _tests, *, role, revision, selections):
        roots.append(Path(flow.args.work_dir))
        return [_plan_unit(role, identity=handle.identity, revision=revision)], []

    def request(_target, _identity, _ref, _runs, _tests, worktree, _invocation, _admission):
        roots.append(worktree)
        return object(), {"coremark@1": {"revision": revision}}

    monkeypatch.setattr(flow, "_plan_simulation_targets", plan)
    monkeypatch.setattr(flow, "_planned_baseline_request", request)
    with flow.publication_resources:
        authorized = flow._campaign_authorization_baselines(revision, ["sim_core"])
        units, errors = flow._plan_cycle_count_baseline_units(["sim_core"], tests)
        assert errors == []
        flow._flow_plan = FlowPlan("sim", "simulate", tuple(units))
        requests, _ = flow._plan_campaign_baselines(tmp_path / "inv", None, ["sim_core"], tests)
        assert len(requests) == 1
        assert [item.identity for item in authorized] == [handle.identity]
        assert [item.project_root for item in authorized] == roots[:1]
        assert len(roots) == 2
        assert len(set(roots)) == 1
        assert roots[0].is_dir()
        assert flow.args.work_dir == root
    assert not roots[0].exists()
    assert not list((root / ".booley").glob(".baseline-wt-*"))
