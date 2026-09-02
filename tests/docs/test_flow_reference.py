"""Keep the public Flow reference aligned with executable interfaces."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, TypeVar

import pytest

from booley.flows.base import BooleyFlow
from booley.flows.fpga.backends.vivado.metrics import FpgaMetrics
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintConfigResult, LintFlow, LintWarning
from booley.flows.sim.build import BuildOutcome
from booley.flows.sim.flow import (
    ElabOnlyTargetResult,
    SimulateFlow,
    TargetResult,
    _test_report_entry,
)
from booley.flows.sim.flow import (
    TestResult as SimTestResult,
)
from booley.flows.synth.flow import AsicSynthesizeFlow, SynthMetrics
from booley.targets.target import _HANDLE_FACTORY_KEY, TargetHandle

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "docs" / "user" / "FLOW_REFERENCE.md"
FLOW_TYPES = (
    SimulateFlow,
    LintFlow,
    AsicSynthesizeFlow,
    FpgaImplFlow,
)
FlowT = TypeVar("FlowT", bound=BooleyFlow)


def _reference_text() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _flow_section(flow_name: str) -> str:
    text = _reference_text()
    start = text.index(f"## `{flow_name}`")
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _shared_section() -> str:
    return _reference_text().split("\n## `sim`", maxsplit=1)[0]


def _documented_fields(flow_name: str) -> set[str]:
    code_spans = re.findall(
        r"(?<!`)`([^`\n]+)`(?!`)",
        _shared_section() + _flow_section(flow_name),
    )
    return {
        identifier
        for code_span in code_spans
        for identifier in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code_span)
    }


def _assert_documented(flow_name: str, payload: dict[str, Any]) -> None:
    missing = set(payload) - _documented_fields(flow_name)
    assert not missing, f"{flow_name} structured fields missing from reference: {sorted(missing)}"


def _configured_flow(
    flow_type: type[FlowT],
    tmp_path: Path,
    target: str,
) -> tuple[FlowT, Path]:
    report_dir = tmp_path / "reports"
    flow = flow_type()
    flow.parse_args(
        ["--target", target, "--work-dir", str(tmp_path), "--report-dir", str(report_dir)]
    )
    handle = TargetHandle(
        identity=f"::docs:0#{target}",
        selector=target,
        name=target,
        vlnv="::docs:0",
        core_file=tmp_path / "docs.core",
        flow=None,
        eda_tool=None,
        drivable_by=(flow.name,),
        project_root=tmp_path.resolve(),
        doctor_private=False,
        _factory_key=_HANDLE_FACTORY_KEY,
    )
    flow._target_handles = {target: handle}  # type: ignore[attr-defined]
    return flow, report_dir


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("flow_type", FLOW_TYPES, ids=lambda flow_type: flow_type.name)
def test_flow_reference_lists_every_long_cli_option(flow_type: type[Any]) -> None:
    flow = flow_type()
    parser_options = {
        option
        for action in flow._parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    documented_options = set(
        re.findall(r"--[a-z][a-z0-9-]*", _shared_section() + _flow_section(flow.name))
    )

    assert parser_options <= documented_options


def test_flow_reference_distinguishes_target_owned_synth_mode() -> None:
    options = {
        option
        for action in AsicSynthesizeFlow()._parser._actions
        for option in action.option_strings
    }

    assert "--synth-mode" not in options
    assert "no per-call `--synth-mode` option" in _flow_section("synth")


def test_flow_reference_uses_the_executable_target_filter() -> None:
    assert "`booley targets --for-flow <flow>`" in _shared_section()
    assert "booley targets --for <flow>" not in _reference_text()


def test_sim_test_fields_stay_documented() -> None:
    entry = _test_report_entry(
        SimTestResult(
            name="reset",
            passed=False,
            test_validated=False,
            trace_path="trace.vcd",
            run_log_path="run.log",
            workload_snapshot={"fingerprint": "sha256"},
        )
    )

    _assert_documented("sim", entry)
    _assert_documented("sim", entry["artifacts"])


def test_sim_report_fields_stay_documented(tmp_path: Path) -> None:
    sim, report_dir = _configured_flow(SimulateFlow, tmp_path, "sim_demo")
    sim._compile_command_str = lambda _target: "make sim"  # type: ignore[method-assign]
    sim._fileset_for_report = lambda _target: {"rtl": [], "tb": []}  # type: ignore[method-assign]
    sim._artifacts_for = lambda _target, _result: {}  # type: ignore[method-assign]
    sim._write_target_report(
        TargetResult(
            target="sim_demo",
            tb_top="tb",
            eda_tool="verilator",
            passed=True,
            tests=[SimTestResult(name="reset", passed=True)],
        )
    )
    _assert_documented("sim", _read_json(report_dir / "sim_sim_demo.json"))


def test_sim_elab_only_report_fields_stay_documented(tmp_path: Path) -> None:
    sim, report_dir = _configured_flow(SimulateFlow, tmp_path, "sim_demo")
    sim._write_elab_only_target_report(
        ElabOnlyTargetResult(
            target="sim_demo",
            target_identity="vendor:library:demo:1.0#sim_demo",
            eda_tool="verilator",
            toplevel="demo",
            compile_command="make",
            fileset={"rtl": ["demo.sv"], "tb": ["tb_demo.sv"]},
            outcome=BuildOutcome(True, "pass", None, elapsed_s=0.1),
            log_path="run.log",
        )
    )
    _assert_documented("sim", _read_json(report_dir / "sim_sim_demo.json"))


def test_lint_report_fields_stay_documented(tmp_path: Path) -> None:
    lint, _report_dir = _configured_flow(LintFlow, tmp_path, "lint_demo")
    warning = LintWarning("RULE", "rtl.sv", 1, 2, "message", "lint_demo")
    lint_result = LintConfigResult(
        target="lint_demo",
        warnings=[warning],
        eda_tool="verilator",
        files_linted=1,
        toplevel="top",
        log_path="run.log",
    )
    lint_error = LintConfigResult(target="lint_broken", error="could not run")
    lint_path = lint._write_lint_report(
        ["lint_demo", "lint_broken"],
        [warning],
        0.1,
        errored=[lint_error],
        target_results=[lint_result, lint_error],
    )
    assert lint_path is not None
    lint_report = _read_json(lint_path)
    _assert_documented("lint", lint_report)
    _assert_documented("lint", lint_report["warnings"][0])
    _assert_documented("lint", lint_report["errors"][0])
    _assert_documented("lint", lint_report["target_results"][0])


def test_synth_report_fields_stay_documented(tmp_path: Path) -> None:
    synth, report_dir = _configured_flow(AsicSynthesizeFlow, tmp_path, "synth_demo")
    current_synth = SynthMetrics(
        area_um2=10.0,
        area_source="openroad",
        area_kge=1.0,
        cells=10,
        wns_ns=-1.0,
        reg2reg_slack_ns=0.0,
        reg2reg_fmax_mhz=100.0,
        failure_output="failure",
        run_evidence={"run_id": "current"},
        synth_mode="physical",
    )
    baseline_synth = SynthMetrics(
        area_um2=8.0,
        area_source="openroad",
        area_kge=0.8,
        cells=8,
        run_evidence={"run_id": "baseline"},
        synth_mode="physical",
    )
    synth._write_target_report("synth_demo", current_synth, baseline_synth, "main")
    synth_path = next(report_dir.glob("synth_*.json"))
    synth_report = _read_json(synth_path)
    _assert_documented("synth", synth_report)
    _assert_documented("synth", synth_report["baseline"])
    _assert_documented("synth", synth_report["conditions"])


def test_fpga_report_fields_stay_documented(tmp_path: Path) -> None:
    fpga, report_dir = _configured_flow(FpgaImplFlow, tmp_path, "fpga_demo")
    current_fpga = FpgaMetrics(
        lut_count=10,
        ff_count=20,
        wns_ns=0.1,
        whs_ns=0.1,
        cached=True,
        cache_fingerprint="cache",
        failure_output="failure",
        log_path="run.log",
        dirs={"build": "build"},
        cache_consumer_run_id="consumer",
        recipe_fingerprint="recipe",
        recipe_snapshot={"target": "fpga_demo"},
        run_evidence={"run_id": "current"},
    )
    baseline_fpga = FpgaMetrics(
        lut_count=9,
        ff_count=19,
        wns_ns=0.2,
        whs_ns=0.2,
        recipe_fingerprint="baseline-recipe",
        recipe_snapshot={"target": "fpga_demo"},
        run_evidence={"run_id": "baseline"},
    )
    fpga_report = fpga._target_report_payload(
        "fpga_demo",
        current_fpga,
        baseline_fpga,
        "main",
        report_dir / "fpga_fpga_demo.json",
    )
    _assert_documented("fpga", fpga_report)
    _assert_documented("fpga", fpga_report["metrics"])
    _assert_documented("fpga", fpga_report["baseline_metrics"])
