"""A Target whose Test Runs do not resolve is never run as its Tests' defaults.

Each place in the Simulation Flow that resolves Test Runs either reports the
error with the Target's name or takes the conservative branch; none silently
continues with the default Test Variants at the Default Seed (ADR 0072).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.flows.sim.test_run_invocation import BaselineLink
from booley.flows.test_runs import UnseededTargetError, build_test_run
from booley.fusesoc import fusesoc_registry
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow


def _unresolvable(flow, monkeypatch) -> None:
    def raise_error(_target):
        raise fusesoc_registry.FuseSocError("no core declares Target 'sim'")

    monkeypatch.setattr(flow, "_target_test_plan", raise_error)


def test_execution_selection_raises_instead_of_selecting_defaults(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _unresolvable(flow, monkeypatch)
    with pytest.raises(fusesoc_registry.FuseSocError, match="no core declares Target 'sim'"):
        flow._test_run_selection("sim", _MAP)


def test_planning_reports_the_unresolvable_target_by_name(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _unresolvable(flow, monkeypatch)
    execution = SimpleNamespace(preview=lambda *_args: pytest.fail("previewed defaults"))
    monkeypatch.setattr(flow, "_simulation_execution", lambda: execution)
    monkeypatch.setattr(flow, "_target_handle", lambda _target: SimpleNamespace())

    units, errors = flow._plan_simulation_targets(
        ["sim"], dict(_MAP), role="ordinary", revision=None
    )

    assert (units, errors) == ([], ["sim: no core declares Target 'sim'"])
    assert flow._test_runs.planned_groups == {}


def test_a_work_unit_whose_target_plan_fails_is_a_planning_error(tmp_path, monkeypatch):
    """The unit is not quietly named by bare Tests, which would merge its Test Runs."""
    flow = _flow(tmp_path, monkeypatch, "--test", "stress@x2")
    runs = flow._resolve_test_runs("sim", _MAP)
    preview = SimpleNamespace(
        groups=(("stress",), ("stress",)),
        run_groups=((runs[0],), (runs[1],)),
        commands=(("sim",), ("sim",)),
    )
    monkeypatch.setattr(
        flow, "_simulation_execution", lambda: SimpleNamespace(preview=lambda *_args: preview)
    )
    monkeypatch.setattr(flow, "_target_handle", lambda _target: SimpleNamespace())

    def work_unit(target, _preview, group, _command, _role, _revision, unit_runs):
        return flow._unit_scope(target, group, unit_runs)

    monkeypatch.setattr(flow, "_simulation_work_unit", work_unit)
    units, errors = flow._plan_simulation_targets(
        ["sim"], dict(_MAP), role="ordinary", revision=None
    )
    assert errors == []
    assert units == [(runs[0].name,), (runs[1].name,)]

    _unresolvable(flow, monkeypatch)
    units, errors = flow._plan_simulation_targets(
        ["sim"], dict(_MAP), role="ordinary", revision=None
    )
    assert (units, errors) == ([], ["sim: no core declares Target 'sim'"])


def test_labels_fall_back_to_registered_test_names(tmp_path, monkeypatch):
    """Display metadata only: nothing is launched from these names."""
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _unresolvable(flow, monkeypatch)
    assert flow._resolve_tests_to_run("sim", _MAP) == ["stress"]


def test_coverage_reports_a_request_the_target_cannot_run(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--coverage", "--seed", "3", adapter="icarus")
    monkeypatch.setattr("booley.flows.sim.flow._get_test_names", lambda _work_dir: dict(_MAP))
    handle = SimpleNamespace(selector="sim")

    with pytest.raises(UnseededTargetError, match="is unseeded"):
        flow._coverage_test_runs([handle])

    monkeypatch.setattr(flow, "_flow_enabled", lambda: True)
    monkeypatch.setattr(
        flow, "_selected_target_selection", lambda: SimpleNamespace(handles=(handle,))
    )
    monkeypatch.setattr(
        "booley.flows.sim.coverage_flow_context.coverage_project_context", lambda *_args: None
    )
    outcome = flow._prepare_coverage()
    assert outcome is not None and outcome.exit_code == 2
    assert outcome.report_text.startswith("Coverage Preflight: Target 'sim' is unseeded")


def test_a_candidate_that_does_not_resolve_has_no_test_run_to_compare(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    _unresolvable(flow, monkeypatch)
    monkeypatch.setattr(
        flow, "_campaign_baseline_requirements", lambda _targets: (("sim", "ref", "stress"),)
    )

    links = flow._baseline_links(["sim"], _MAP)

    assert links == (BaselineLink("sim", "ref", "stress", None),)
    with flow._baseline_checkout(tmp_path / "baseline", links, "a" * 40) as baseline:
        assert baseline.runs_of("ref") == ()
    assert ("sim", "stress") in baseline.refusals


def test_a_resume_hint_names_every_frozen_run_when_the_plan_cannot_be_built(tmp_path, monkeypatch):
    from tests.flows.sim.test_campaign_test_runs import _hdl_plan

    default = build_test_run("stress", variant="short", seed=1)
    manifest = _hdl_plan(tmp_path, ((default,),), ("reset", "stress")).manifest
    flow = _flow(tmp_path, monkeypatch)
    assert flow._frozen_test_run_options(manifest, "sim") == ""
    _unresolvable(flow, monkeypatch)
    assert flow._frozen_test_run_options(manifest, "sim") == " --test=stress+short@1"
