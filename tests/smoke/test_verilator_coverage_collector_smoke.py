from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.execution.contract import SimulationOptions
from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    CoverageCollectionRequest,
    CoverageSource,
    CoverageTarget,
    SelectedCoverageTest,
    SimulationBuildResult,
    SimulationCommandResult,
    SimulationRunResult,
    collect,
)
from booley.flows.sim.verilator_coverage_execution import collect_target_coverage
from booley.targets.catalog import TargetCatalog

pytestmark = pytest.mark.skipif(
    shutil.which("verilator") is None or shutil.which("verilator_coverage") is None,
    reason="real smoke runs inside the pinned Booley Session Image",
)
_TOOL_TIMEOUT_S = 120


class _RealVerilatorExecution:
    def __init__(self, source: Path, build_dir: Path) -> None:
        self.source = source
        self.build_dir = build_dir

    def build(self, request) -> SimulationBuildResult:
        version = subprocess.run(
            ["verilator", "--version"],
            timeout=30,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        provenance = Path("/usr/local/share/verilator/BOOLEY-SOURCE.txt")
        if (
            "Verilator 5.052" not in version
            or not provenance.is_file()
            or PINNED_VERILATOR.commit not in provenance.read_text(encoding="utf-8")
        ):
            return SimulationBuildResult(success=False, output=version)
        result = subprocess.run(
            [
                "verilator",
                "--binary",
                "--timing",
                "--top-module",
                "counter_tb",
                "--Mdir",
                str(self.build_dir),
                *request.instrumentation,
                str(self.source),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TOOL_TIMEOUT_S,
        )
        return SimulationBuildResult(
            success=result.returncode == 0,
            output=result.stdout + result.stderr,
            collector=PINNED_VERILATOR,
        )

    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(request.environment)
        result = subprocess.run(
            [str(self.build_dir / "Vcounter_tb"), *request.argv_suffix],
            cwd=request.raw_path.parent,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TOOL_TIMEOUT_S,
        )
        return SimulationRunResult(
            verdict="pass" if result.returncode == 0 else "fail",
            output=result.stdout + result.stderr,
        )

    def command(self, request) -> SimulationCommandResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            request.argv,
            cwd=request.cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TOOL_TIMEOUT_S,
        )
        return SimulationCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def test_real_generated_main_native_database_is_queryable_and_merge_equivalent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "counter.sv"
    source.write_text(
        """
module counter(input logic clk, output logic [1:0] value);
  always_ff @(posedge clk) value <= value + 1'b1;
endmodule

module counter_tb;
  logic clk = 0;
  logic [1:0] value;
  counter dut(.clk(clk), .value(value));
  always #1 clk = ~clk;
  initial begin
    repeat (4) @(posedge clk);
    $display("PASS value=%0d", value);
    $finish;
  end
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="smoke:booley:counter:1#sim_counter",
            selector="sim_counter",
            toplevel="counter_tb",
            harness="generated_main",
            sources=(CoverageSource(str(source), "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("generated_main"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _RealVerilatorExecution(source, tmp_path / "build"))

    assert result.status == "complete", [
        (finding.code, finding.message) for finding in result.findings
    ]
    assert result.runs[0].simulation_verdict == "pass"
    assert result.merge.status == "equivalent"
    assert any(point.identity.metric == "line" for point in result.points)
    assert [artifact.kind for artifact in result.artifacts] == [
        "raw_native",
        "merged_native",
    ]


def _invoke(command: list[str], *, timeout: int) -> SubprocessResult:
    try:
        completed = subprocess.run(
            command,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return SubprocessResult(
            returncode=-1,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            timed_out=True,
        )
    return SubprocessResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _write_generated_target(root: Path, *, post_reset: bool = False) -> None:
    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    (root / "rtl" / "counter.sv").write_text(
        "module counter(input logic clk, output logic [1:0] value);\n"
        "  always_ff @(posedge clk) value <= value + 1'b1;\nendmodule\n",
        encoding="utf-8",
    )
    declaration = '  import "DPI-C" function void booley_coverage_start();\n' if post_reset else ""
    start = "repeat (2) @(posedge clk); booley_coverage_start(); " if post_reset else ""
    (root / "tb" / "counter_tb.sv").write_text(
        "module counter_tb;\n  logic clk = 0; logic [1:0] value;\n"
        "  counter dut(.clk(clk), .value(value));\n  always #1 clk = ~clk;\n"
        f"{declaration}  initial begin {start}repeat (4) @(posedge clk);\n"
        '    $display("[SIM_RESULT] PASSED"); $finish; end\nendmodule\n',
        encoding="utf-8",
    )
    coverage = "      booley: {coverage: {reset_included: false}}\n" if post_reset else ""
    _write_core(root, "tb/counter_tb.sv", "counter_tb", "[--timing, --main, --exe]", coverage)


def _write_custom_target(root: Path) -> None:
    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    (root / "rtl" / "counter.sv").write_text(
        "module counter(input logic clk, output logic [1:0] value = 0);\n"
        "  always_ff @(posedge clk) value <= value + 1'b1;\nendmodule\n",
        encoding="utf-8",
    )
    _write_custom_main(root / "tb" / "main.cpp")
    coverage = (
        "      booley:\n        coverage:\n          reset_included: false\n"
        "          custom_main_hooks: [start_hook, write_hook]\n"
    )
    _write_core(root, "tb/main.cpp", "counter", "[--timing]", coverage)


def _write_custom_main(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #include "Vcounter.h"
            #include "booley_coverage.h"
            #include "verilated.h"
            #include <iostream>
            int main(int argc, char** argv) {
                VerilatedContext context; context.commandArgs(argc, argv);
                Vcounter dut{&context}; booley_coverage_start();
                for (int cycle = 0; cycle < 8; ++cycle) {
                    dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval();
                }
                dut.final(); booley_coverage_write();
                std::cout << "[SIM_RESULT] PASSED\\n"; return 0;
            }
            """
        ),
        encoding="utf-8",
    )


def _write_core(root: Path, testbench: str, top: str, options: str, coverage: str) -> None:
    file_type = "cppSource" if testbench.endswith(".cpp") else "systemVerilogSource"
    content = f"""\
CAPI=2:
name: booley:smoke:coverage:1
filesets:
  rtl:
    files:
      - rtl/counter.sv: {{file_type: systemVerilogSource}}
  tb:
    files:
      - {testbench}: {{file_type: {file_type}}}
    tags: [tb]
targets:
  sim:
    flow: sim
    default_tool: verilator
    flow_options:
      tool: verilator
      verilator_options: {options}
{coverage}    filesets: [rtl, tb]
    toplevel: {top}
"""
    (root / "counter.core").write_text(content, encoding="utf-8")


def _collect_target(root: Path, test: str):
    handle = TargetCatalog.build(root).select("sim", for_flow="sim")
    return collect_target_coverage(
        handle,
        selected_tests=(test,),
        artifact_root=root / "campaign",
        invoke=_invoke,
        options=SimulationOptions(timeout_ms=30_000),
    )


def test_real_target_collects_through_simulation_execution_path(tmp_path: Path) -> None:
    _write_generated_target(tmp_path)
    result = _collect_target(tmp_path, "smoke")
    assert result.status == "complete", [
        (finding.code, finding.message) for finding in result.findings
    ]
    assert result.runs[0].simulation_verdict == "pass"
    assert result.merge.status == "equivalent"
    assert any(point.identity.metric == "line" for point in result.points)


def test_real_hdl_post_reset_collection_uses_packaged_hook_bridge(tmp_path: Path) -> None:
    _write_generated_target(tmp_path, post_reset=True)
    result = _collect_target(tmp_path, "hdl_post_reset")
    assert result.status == "complete", [
        (finding.code, finding.message) for finding in result.findings
    ]
    assert result.coverage_window.mode == "post_reset"
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)


def test_real_custom_main_collects_through_packaged_window_hooks(tmp_path: Path) -> None:
    _write_custom_target(tmp_path)
    result = _collect_target(tmp_path, "custom")
    assert result.status == "complete", [
        (finding.code, finding.message) for finding in result.findings
    ]
    assert result.runs[0].simulation_verdict == "pass"
    assert result.coverage_window.mode == "post_reset"
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)
    assert result.merge.status == "equivalent"


@pytest.mark.parametrize(
    "harness,trace",
    [("generated", False), ("generated", True), ("post_reset", False), ("custom", False)],
)
def test_real_coverage_flow_publishes_canonical_campaign(
    tmp_path: Path, harness: str, trace: bool
) -> None:
    import json

    from booley.flows.sim.coverage_campaign import DurableTargetIdentity, decode_coverage_campaign
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest

    if harness == "custom":
        _write_custom_target(tmp_path)
    else:
        _write_generated_target(tmp_path, post_reset=harness == "post_reset")
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim]\ntests = ["smoke"]\n')
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=tmp_path,
            coverage=True,
            trace=trace,
            report_dir=tmp_path / "reports",
        )
    )
    assert result.exit_code == 0, result.outcome
    path = tmp_path / result.outcome.detail["targets"]["sim"]["coverage_campaign"]
    campaign = decode_coverage_campaign(
        json.loads(path.read_text()), DurableTargetIdentity("booley:smoke:coverage:1#sim")
    )
    assert campaign.collection["status"] == "complete"
    assert campaign.evaluation["status"] == "not_requested"
    assert campaign.build["trace"] is trace
    assert campaign.points


def test_real_cocotb_flow_uses_one_process_per_selected_test(tmp_path: Path) -> None:
    import json

    from tests.fixtures.verilator_acceptance.flow_fixture import write_project

    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest

    root = tmp_path / "project"
    write_project(root, "cocotb", "pass")
    path = root / "test_case.py"
    original = path.read_text()
    path.write_text(
        original
        + "\n"
        + original[original.index("@cocotb.test()") :].replace(
            "async def check(", "async def another("
        )
    )
    (root / ".booley_project/tests.toml").write_text('[sim]\ntests = ["check", "another"]\n')
    result = SimulateFlow().execute(
        SimRequest(target="sim", work_dir=root, coverage=True, report_dir=root / "reports")
    )
    assert result.exit_code == 0, result.outcome
    campaign = json.loads(
        (root / result.outcome.detail["targets"]["sim"]["coverage_campaign"]).read_text()
    )
    assert len(campaign["tests"]["runs"]) == 2
    raw = [artifact for artifact in campaign["artifacts"] if artifact["kind"] == "raw_native"]
    assert len({artifact["path"] for artifact in raw}) == 2
    assert {run["test"] for run in campaign["tests"]["runs"]} == {"another", "check"}


def test_real_flow_preserves_all_four_build_variants(tmp_path: Path) -> None:
    import hashlib

    from tests.fixtures.verilator_acceptance.flow_fixture import write_project

    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest

    root = tmp_path / "project"
    write_project(root, "generated", "pass")
    top = root / "top.sv"
    top.write_text(
        top.read_text().replace(
            "module top;",
            "module top; string tracefile; initial begin "
            'if ($value$plusargs("tracefile=%s", tracefile)) begin '
            "$dumpfile(tracefile); $dumpvars(0, top); end end",
        )
    )
    (root / ".booley_project" / "tests.toml").write_text('[sim]\ntests = ["smoke"]\n')
    previous = {}
    for coverage, trace in [(False, False), (False, True), (True, False), (True, True)]:
        result = SimulateFlow().execute(
            SimRequest(
                target="sim",
                work_dir=root,
                coverage=coverage,
                trace=trace,
                report_dir=root / "reports",
            )
        )
        assert result.exit_code == 0, result.outcome
        binaries = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("Vtop")
            if path.is_file()
        }
        assert len(binaries) == len(previous) + 1
        assert all(binaries.get(path) == digest for path, digest in previous.items())
        previous = binaries
