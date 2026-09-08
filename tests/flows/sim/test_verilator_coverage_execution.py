from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.coverage_overlay import CoverageOverlay
from booley.flows.sim.execution.contract import SimulationOptions
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    VERILATOR_COVERAGE_INSTRUMENTATION,
    CoverageTarget,
    SelectedCoverageTest,
    SimulationBuildRequest,
    SimulationBuildVariant,
    SimulationRunRequest,
)
from booley.flows.sim.verilator_coverage_execution import (
    VerilatorCoverageExecution,
    prepare_coverage_collection,
)
from booley.targets.catalog import TargetCatalog


def _write_target(root: Path, *, custom_main: bool = False) -> None:
    hooks = "          custom_main_hooks: [start_hook, write_hook]\n" if custom_main else ""
    main = "      - tb/main.cpp: {file_type: cppSource, tags: [tb]}\n" if custom_main else ""
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir()
    (root / "rtl" / "counter.sv").write_text("module counter; endmodule\n")
    (root / "tb" / "counter_tb.sv").write_text("module counter_tb; endmodule\n")
    if custom_main:
        (root / "tb" / "main.cpp").write_text("int main() { return 0; }\n")
    (root / "counter.core").write_text(
        "CAPI=2:\n"
        "name: acme:demo:counter:1\n"
        "filesets:\n"
        "  rtl:\n"
        "    files:\n"
        "      - rtl/counter.sv: {file_type: systemVerilogSource}\n"
        "  tb:\n"
        "    files:\n"
        "      - tb/counter_tb.sv: {file_type: systemVerilogSource}\n"
        f"{main}"
        "    tags: [tb]\n"
        "targets:\n"
        "  sim:\n"
        "    flow: sim\n"
        "    default_tool: verilator\n"
        "    flow_options:\n"
        "      tool: verilator\n"
        "      booley:\n"
        "        coverage:\n"
        f"          reset_included: {str(not custom_main).lower()}\n"
        f"{hooks}"
        "    filesets: [rtl, tb]\n"
        "    toplevel: counter_tb\n",
        encoding="utf-8",
    )


def test_prepare_collection_projects_resolved_sources_and_custom_main_recipe(
    tmp_path: Path,
) -> None:
    _write_target(tmp_path, custom_main=True)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

    request = prepare_coverage_collection(
        handle,
        selected_tests=("wrap",),
        artifact_root=tmp_path / "coverage",
        trace=True,
    )

    assert request.target.identity == handle.identity
    assert request.target.harness == "custom_main"
    assert request.target.custom_main_hooks == ("start_hook", "write_hook")
    assert request.reset_included is False
    assert request.trace is True
    assert [(source.path, source.kind) for source in request.target.sources] == [
        ("rtl/counter.sv", "rtl"),
        ("tb/counter_tb.sv", "testbench"),
    ]


def test_execution_uses_simulation_build_and_authenticated_run_adapters(
    tmp_path: Path, monkeypatch
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    build = execution.build(
        SimulationBuildRequest(
            target,
            SimulationBuildVariant(trace=False, coverage=True),
            VERILATOR_COVERAGE_INSTRUMENTATION,
        )
    )
    run = execution.run(_run_request(target, raw_path))

    assert build.success is True
    assert build.collector == PINNED_VERILATOR
    assert captured["prepare"]["variant"] == "coverage"
    assert captured["prepare"]["resolution_vlnv"] == "::coverage:0"
    assert captured["work"].plusargs[-1] == f"+verilator+coverage+file+{raw_path}"
    assert "BOOLEY_COVERAGE_RUN_ID=run:001:wrap" in captured["run_script"]
    assert run.verdict == "pass"


def _execution_fixture(tmp_path: Path, monkeypatch):
    _write_target(tmp_path)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    build_root = tmp_path / "build" / "coverage"
    build_root.mkdir(parents=True)
    provenance = tmp_path / "BOOLEY-SOURCE.txt"
    provenance.write_text(
        f"release={PINNED_VERILATOR.tag}\nsource_revision={PINNED_VERILATOR.commit}\n",
        encoding="utf-8",
    )
    prepared = PreparedSimulationBuild(
        target=handle.selector,
        target_identity=handle.identity,
        resolved=SimpleNamespace(cocotb_module="", parameters={}),
        work_root=build_root,
        build_root=build_root,
        eda_tool="verilator",
        toplevel="counter_tb",
        make_argv=("make",),
    )
    captured = {}
    raw_path = tmp_path / "artifacts" / "raw.dat"
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.write_coverage_overlay",
        _fake_overlay(tmp_path),
    )
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.prepare_simulation_build",
        _fake_prepare(prepared, captured),
    )
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.prepare_adapter_invocation",
        _fake_adapter(captured),
    )
    execution = VerilatorCoverageExecution(
        handle,
        invoke=_fake_invoke(captured, raw_path),
        options=SimulationOptions(),
        provenance_path=provenance,
    )
    target = CoverageTarget(handle.identity, handle.selector, "counter_tb", "generated_main", ())
    return execution, target, raw_path, captured


def _fake_overlay(tmp_path: Path):
    def fake_overlay(*args, **kwargs):
        return CoverageOverlay(
            tmp_path / "missing-overlay.core", "::coverage:0", TraceMode.VCD_FIFO
        )

    return fake_overlay


def _fake_prepare(prepared, captured):
    def fake_prepare(*args, **kwargs):
        captured["prepare"] = kwargs
        prepared.build_root.mkdir(parents=True, exist_ok=True)
        return prepared

    return fake_prepare


def _fake_adapter(captured):
    def fake_adapter(work):
        captured["work"] = work
        return ["true"]

    return fake_adapter


def _fake_invoke(captured, raw_path: Path):
    def fake_invoke(command, *, timeout):
        if command == ["verilator", "--version"]:
            return SubprocessResult(returncode=0, stdout="Verilator 5.052 2026-09-05\n")
        script = command[2]
        if "BOOLEY_BUILD_STAGE" in script:
            token = re.search(r"token=([0-9a-f]+)", script).group(1)
            return SubprocessResult(
                returncode=0,
                stdout=f"BOOLEY_BUILD_STAGE token={token} rc=0 duration_ms=1\n",
            )
        work = captured["work"]
        identity = AdapterTransportIdentity(
            adapter=work.adapter,
            attempt_token=work.attempt_token,
            target_identity=work.target_identity,
            selected_tests=work.tests,
            result_path=Path(work.adapter_result_path),
        )
        write_adapter_result(
            identity,
            captured.get("result")
            or AdapterResult(
                passed=True,
                inconclusive=False,
                sva_errors=0,
                tests=work.tests,
                test_results=(AdapterTestResult("wrap", "pass"),),
            ),
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text("# SystemC::Coverage-3\n", encoding="utf-8")
        captured["run_script"] = script
        return captured.get("process") or SubprocessResult(
            returncode=0, stdout="[SIM_RESULT] PASSED\n"
        )

    return fake_invoke


def _run_request(target: CoverageTarget, raw_path: Path) -> SimulationRunRequest:
    return SimulationRunRequest(
        target=target,
        test=SelectedCoverageTest("wrap"),
        run_id="run:001:wrap",
        raw_path=raw_path,
        hook_evidence_path=None,
        trace=False,
        argv_suffix=(f"+verilator+coverage+file+{raw_path}",),
        environment={"BOOLEY_COVERAGE_RUN_ID": "run:001:wrap"},
    )


@pytest.mark.parametrize("verdict", ["pass", "fail", "timeout", "inconclusive"])
def test_batch_timeout_preserves_authoritative_per_test_verdict(
    tmp_path: Path, monkeypatch, verdict: str
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert execution.build(
        SimulationBuildRequest(
            target,
            SimulationBuildVariant(trace=False, coverage=True),
            VERILATOR_COVERAGE_INSTRUMENTATION,
        )
    ).success
    captured["result"] = AdapterResult(
        passed=False,
        inconclusive=True,
        sva_errors=0,
        tests=("wrap",),
        failure_kind="timeout",
        test_results=(AdapterTestResult("wrap", verdict),),
    )

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == verdict


@pytest.mark.parametrize("process_timeout", [False, True])
def test_missing_per_test_evidence_is_rejected_without_losing_process_timeout(
    tmp_path: Path, monkeypatch, process_timeout: bool
) -> None:
    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert execution.build(
        SimulationBuildRequest(
            target,
            SimulationBuildVariant(trace=False, coverage=True),
            VERILATOR_COVERAGE_INSTRUMENTATION,
        )
    ).success
    captured["result"] = AdapterResult(
        passed=False,
        inconclusive=True,
        sva_errors=0,
        tests=("wrap",),
        failure_kind="" if process_timeout else "timeout",
    )
    captured["process"] = SubprocessResult(returncode=1, timed_out=process_timeout)

    result = execution.run(_run_request(target, raw_path))

    assert result.verdict == ("timeout" if process_timeout else "inconclusive")
    assert "omits required per-test verdicts" in result.output
