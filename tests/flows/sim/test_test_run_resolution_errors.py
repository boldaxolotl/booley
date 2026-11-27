"""A Target whose Test Runs do not resolve is never run as its Tests' defaults.

Each place in the Simulation Flow that resolves Test Runs either reports the
error with the Target's name or takes the conservative branch; none silently
continues with the default Test Variants at the Default Seed (ADR 0072).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.flows.test_runs import UnseededTargetError, build_test_run
from booley.fusesoc import fusesoc_registry
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow


def _unresolvable(flow, monkeypatch) -> None:
    def raise_error(_target):
        raise fusesoc_registry.FuseSocError("no core declares Target 'sim'")

    # Private seam: no public request reaches the Target-plan error once the endpoint resolved the Target (ADR 0072).
    monkeypatch.setattr(flow, "_target_test_plan", raise_error)


def test_execution_selection_raises_instead_of_selecting_defaults(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _unresolvable(flow, monkeypatch)
    # Private seam: no public request reaches the Target-plan error once the endpoint resolved the Target (ADR 0072).
    with pytest.raises(fusesoc_registry.FuseSocError, match="no core declares Target 'sim'"):
        flow._test_run_selection("sim", _MAP)


def test_planning_reports_the_unresolvable_target_by_name(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _unresolvable(flow, monkeypatch)
    # Private seam: no public request reaches the Target-plan error once the endpoint resolved the Target (ADR 0072).
    execution = SimpleNamespace(preview=lambda *_args: pytest.fail("previewed defaults"))
    monkeypatch.setattr(flow, "_simulation_execution", lambda: execution)
    monkeypatch.setattr(flow, "_target_handle", lambda _target: SimpleNamespace())

    planned, errors = flow._preview_targets(["sim"], dict(_MAP))

    assert (planned, errors) == ([], ["sim: no core declares Target 'sim'"])


def test_labels_fall_back_to_registered_test_names(tmp_path, monkeypatch):
    """Display metadata only: nothing is launched from these names."""
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _unresolvable(flow, monkeypatch)
    # Private seam: no public request reaches the Target-plan error once the endpoint resolved the Target (ADR 0072).
    assert flow._resolve_tests_to_run("sim", _MAP) == ["stress"]


def test_coverage_reports_a_request_the_target_cannot_run(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--coverage", "--seed", "3", adapter="icarus")
    # Private seam: no public request reaches the Target-plan error once the endpoint resolved the Target (ADR 0072).
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


def test_a_resume_hint_names_every_frozen_run_when_the_plan_cannot_be_built(tmp_path, monkeypatch):
    from tests.flows.sim.test_simulation_record_test_runs import _hdl_plan

    default = build_test_run("stress", variant="short", seed=1)
    manifest = _hdl_plan(tmp_path, ((default,),), ("reset", "stress")).manifest
    flow = _flow(tmp_path, monkeypatch)
    # Private seam: no public request reaches the Target-plan error once the endpoint resolved the Target (ADR 0072).
    assert flow._frozen_test_run_options(manifest, "sim") == ""
    _unresolvable(flow, monkeypatch)
    assert flow._frozen_test_run_options(manifest, "sim") == " --test=stress+short@1"
    (tmp_path / "unnamed").mkdir()
    unnamed = _hdl_plan(tmp_path / "unnamed", ((build_test_run(None, seed=7),),), ()).manifest
    assert flow._frozen_test_run_options(unnamed, "sim") == " --seed 7"
