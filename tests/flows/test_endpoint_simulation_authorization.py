"""Preflight ordering for prepared Simulation Record Targets."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.flow import SimulateFlow, SimulationMode
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.domain import TargetHandle
from tests.flows.invocation_doubles import prepare_as


def _handle(selector: str, identity: str, root: Path) -> TargetHandle:
    return cast(
        TargetHandle,
        SimpleNamespace(
            selector=selector,
            name=selector,
            identity=identity,
            project_root=root,
        ),
    )


# Private seam: each hook is the check that refuses before the build timeout resolves; Target
# preparation needs a resolved FuseSoC core, so the stubs keep the test on the ordering.
@pytest.mark.parametrize(
    ("hook", "resume"),
    (
        pytest.param("_validate_prepared_selection", None, id="selection-rejection"),
        pytest.param("_refuse_unfinished_coverage_resume", SimpleNamespace(), id="unfinished"),
    ),
)
def test_refusal_precedes_build_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook: str, resume: object
) -> None:
    handle = _handle("sim", "acme:lib:dut:1#sim", tmp_path)
    flow = SimulateFlow()
    # Private seam: Target preparation needs a resolved FuseSoC core (see above).
    flow._args = SimpleNamespace(mode=SimulationMode.SIMULATE)
    monkeypatch.setattr(
        flow,
        "_prepare_simulation_record_targets",
        lambda: ((handle,), resume, ("sim",), {"sim": ["smoke"]}),
    )
    refusal = EndpointOutcome(exit_code=2, report_text="refused before the build timeout")
    monkeypatch.setattr(flow, hook, lambda *_args: refusal)
    build_timeout_resolutions: list[object] = []
    # Private seam: the build timeout resolves only inside the preflight under test.
    monkeypatch.setattr(
        flow,
        "_effective_build_timeout_ms",
        lambda: build_timeout_resolutions.append(object()),
    )

    assert flow.prepare_simulation_endpoint() is refusal
    assert build_timeout_resolutions == []


@pytest.mark.parametrize(
    "value",
    ['"make -C tests build"', '["make", 3]'],
    ids=["bare-string", "non-string-entry"],
)
def test_invalid_pre_run_commands_refuse_in_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A malformed Pre-Sim Commands knob fails the preflight with Doctor's message."""
    from booley.audit.flow_schema import audit_flow_tables

    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text(
        f"[flows.sim]\npre_run_commands = {value}\n", encoding="utf-8"
    )
    handle = _handle("sim", "acme:lib:dut:1#sim", tmp_path)
    flow = SimulateFlow()
    # Private seam: Target preparation needs a resolved FuseSoC core; the stubs keep the test
    # on the preflight knob.
    flow._args = SimpleNamespace(
        mode=SimulationMode.SIMULATE, work_dir=tmp_path, coverage=False, resume_from=None
    )
    monkeypatch.setattr(
        flow,
        "_prepare_simulation_record_targets",
        lambda: ((handle,), None, ("sim",), {"sim": ["smoke"]}),
    )
    monkeypatch.setattr(flow, "_validate_prepared_selection", lambda *_args: None)
    monkeypatch.setattr(flow, "_effective_build_timeout_ms", lambda: 1000)
    audit = audit_flow_tables({"flows": {"sim": {"pre_run_commands": "x"}}}, ())
    (doctor_finding,) = audit.findings

    outcome = flow.prepare_simulation_endpoint()

    assert isinstance(outcome, EndpointOutcome)
    assert outcome.exit_code == 2
    assert doctor_finding.message in outcome.report_text
    assert doctor_finding.fix in outcome.report_text


def test_state_backed_simulation_preflight_rejection_reaches_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The state gate refuses a Flow run outside the container before preparation.
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    flow = SimulateFlow()
    prepare_as(
        monkeypatch,
        flow,
        EndpointOutcome(exit_code=2, report_text="Simulation preflight rejected"),
    )

    execution = flow.execute_cli(
        [
            "--target",
            "demo",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )

    captured = capsys.readouterr()
    assert execution.exit_code == 2
    assert captured.err.count("Simulation preflight rejected") == 1
    assert captured.out == ""


def test_prepare_resets_report_only_metadata_before_early_gate(tmp_path, monkeypatch):
    from booley.flows.endpoint_session import prepare_execution
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_endpoint_record_lifecycle import _public_cycle_simulation_record

    flow = SimulateFlow()
    simulation_record = _public_cycle_simulation_record(tmp_path, monkeypatch)
    # Private seam: an early-gate rejection writes no report, so the reset of report-only
    # metadata is observable only inside the gate, on state set through these members.
    flow._args = SimRequest(target="sim", work_dir=tmp_path, report_dir=tmp_path / "rejected")
    flow._simulation_record_endpoint_outcome([simulation_record])
    previous = flow._simulation_record_outcomes
    flow.record_eda_tool("previous", "verilator")
    rejected = EndpointOutcome(exit_code=2)

    def gate():
        assert flow.cycle_report_json is None
        # Private seam: observable only inside the gate (see above).
        assert flow._eda_tool is None
        assert flow._eda_tools == {"sim": None}
        with flow.publication_resources:
            path = flow.write_report(rejected)
        assert '"cycle_counts"' not in path.read_text()
        # Private seam: observable only inside the gate (see above).
        assert flow._simulation_record_outcomes is previous
        return rejected

    # Private seam: observable only inside the gate (see above).
    monkeypatch.setattr(flow, "_apply_pre_state_gate", gate)
    assert prepare_execution(flow) is rejected


@pytest.mark.parametrize("target", ["sim_core", "sim_core, sim_other", ["sim_core", "sim_other"]])
def test_prepare_early_gate_keeps_unresolved_report_target_cardinality(monkeypatch, target):
    from booley.flows.endpoint_session import prepare_execution

    context = SimulateFlow()
    # Private seam: an early-gate rejection writes no report, so the report Target
    # cardinality is observable only inside the gate, on state set through these members.
    context._args = SimpleNamespace(target=target)
    context.record_eda_tool("previous", "verilator")
    rejected = EndpointOutcome(exit_code=2)
    selected = ["sim_core"] if target == "sim_core" else ["sim_core", "sim_other"]

    def gate():
        # Private seam: observable only inside the gate (see above).
        assert context._eda_tool is None
        assert context._eda_tools == dict.fromkeys(selected)
        return rejected

    # Private seam: observable only inside the gate (see above).
    monkeypatch.setattr(context, "_apply_pre_state_gate", gate)
    assert prepare_execution(context) is rejected
