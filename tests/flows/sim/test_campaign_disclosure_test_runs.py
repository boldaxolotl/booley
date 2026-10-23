"""Campaign disclosure planning prepares the Test Runs that launch (ADR 0072).

A Cocotb batch may hold non-default Test Variants that share an environment
while their Tests' default Variants do not. Planning the disclosure from bare
Test names would prepare the defaults and refuse the batch ("cannot share one
simulator process"), so the concrete Test Runs are passed through.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.sim.execution import SimulationExecution, SimulationOptions
from booley.flows.target_test_plan import batch_test_runs, build_target_test_plan
from tests.flows.sim.test_flow_test_run_resolution import _flow

_TESTS = {
    "sim": {
        "tests": ["a", "b"],
        "variants": {
            "a": {
                "default": "one",
                "one": {"env": {"MODE": "A"}},
                "shared": {"env": {"MODE": "C"}},
            },
            "b": {
                "default": "two",
                "two": {"env": {"MODE": "B"}},
                "shared": {"env": {"MODE": "C"}},
            },
        },
    }
}
_MAP = {"sim": ["a", "b"]}


def _plan():
    return build_target_test_plan("sim", normalize_tests_toml(_TESTS)["sim"], adapter="cocotb")


def test_selected_variants_share_a_batch_their_defaults_could_not():
    plan = _plan()
    runs = plan.resolve(["a+shared", "b+shared"])

    assert batch_test_runs(runs, one_process_per_run=False) == (runs,)
    assert plan.launch(runs).environment == (("MODE", "C"),)
    with pytest.raises(ValueError, match="cannot share one simulator process"):
        plan.launch(plan.default_runs_for(("a", "b")))


def test_campaign_inputs_disclose_the_concrete_test_runs(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "a+shared", "--test", "b+shared")
    plan = _plan()
    monkeypatch.setattr(flow, "_target_test_plan", lambda _target: plan)
    monkeypatch.setattr(flow, "_effective_skips", lambda _target: set())
    runs = flow._resolve_test_runs("sim", _MAP)
    disclosed: list[tuple[tuple[str, ...], object]] = []
    execution = SimpleNamespace(
        preview=lambda _handle, _selection: SimpleNamespace(
            groups=(("a", "b"),), run_groups=(runs,)
        ),
        plan_campaign_group=lambda _handle, group, test_runs=None: (
            disclosed.append((group, test_runs)) or {}
        ),
    )
    monkeypatch.setattr(flow, "_simulation_execution", lambda: execution)
    monkeypatch.setattr(
        "booley.flows.sim.flow.TargetCatalog.build",
        lambda _root: SimpleNamespace(inspect=lambda _handle: object()),
    )
    handle = SimpleNamespace(project_root=tmp_path, selector="sim", identity="sim")

    flow._ordinary_campaign_inputs("sim", _MAP, handle, None)

    assert disclosed == [(("a", "b"), runs)]
    assert [run.name for run in runs] == ["a+shared@1", "b+shared@1"]


def test_group_disclosure_prepares_the_test_runs_it_is_given(monkeypatch):
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    runs = _plan().resolve(["a+shared", "b+shared"])
    prepared: list[tuple[tuple[str, ...], object]] = []

    @contextmanager
    def ordinary_group(_handle, test_names, test_runs=None):
        prepared.append((test_names, test_runs))
        yield SimpleNamespace(
            planning_disclosure=lambda: {"disclosed": True},
            discard_prepared_generation=lambda: None,
        )

    monkeypatch.setattr(execution, "ordinary_group", ordinary_group)

    assert execution.plan_campaign_group(object(), ("a", "b"), runs) == {"disclosed": True}
    assert execution.plan_campaign_group(object(), ("a",)) == {"disclosed": True}
    assert prepared == [(("a", "b"), runs), (("a",), None)]


def test_resume_discloses_the_test_runs_the_manifest_froze(tmp_path, monkeypatch):
    """The resume call site: disclosure planning gets each batch's frozen Test Runs.

    The manifest froze two batches of non-default Test Variants. Re-planning
    for the resume comparison must prepare exactly those runs, not the
    default Variants of the same Tests (which could not even share a batch).
    """
    from booley.flows.sim import flow as flow_module
    from booley.flows.test_runs import build_test_run
    from tests.flows.sim.test_campaign_test_runs import _cocotb_plan

    groups = (
        (
            build_test_run("reset", variant="shared", seed=1, env={"MODE": "C"}),
            build_test_run("count", variant="shared", seed=1, env={"MODE": "C"}),
        ),
        (build_test_run("count", variant="other", seed=9, env={"MODE": "D"}),),
    )
    manifest = _cocotb_plan(tmp_path, groups).manifest
    handle = SimpleNamespace(project_root=tmp_path, selector="sim", identity="sim")
    validated = SimpleNamespace(
        manifest=manifest, binding_for=lambda _manifest: None, target_handles=(handle,)
    )
    disclosed: list[tuple[tuple[str, ...], object]] = []
    previewed: list[object] = []
    execution = SimpleNamespace(
        preview=lambda _handle, selection: (
            previewed.append(selection) or SimpleNamespace(groups=(), run_groups=())
        ),
        plan_campaign_group=lambda _handle, group, test_runs=None: (
            disclosed.append((group, test_runs)) or {"group": group}
        ),
    )
    flow = _flow(tmp_path, monkeypatch)
    monkeypatch.setattr(flow, "_simulation_execution", lambda: execution)
    monkeypatch.setattr(
        flow_module.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(inspect=lambda _handle: object()),
    )
    monkeypatch.setattr(flow_module, "git_full_sha", lambda *_args: "c" * 40)
    planned: dict[str, object] = {}
    monkeypatch.setattr(
        flow_module,
        "plan_coarse_simulation_campaign",
        lambda **kwargs: planned.update(kwargs) or "plan",
    )

    assert flow._resume_campaign_plan(validated, 1) == "plan"

    assert disclosed == [(("reset", "count"), groups[0]), (("count",), groups[1])]
    assert planned["planning_disclosures"] == (
        {"group": ("reset", "count")},
        {"group": ("count",)},
    )
    # Resume previews the frozen runs too: nothing is resolved or drawn again.
    (selection,) = previewed
    assert selection.runs == (*groups[0], *groups[1])
