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


class _RealVerilatorExecution:
    def __init__(self, source: Path, build_dir: Path) -> None:
        self.source = source
        self.build_dir = build_dir

    def build(self, request) -> SimulationBuildResult:
        version = subprocess.run(
            ["verilator", "--version"],
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


def test_real_target_collects_through_simulation_execution_path(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "tb").mkdir()
    (tmp_path / "rtl" / "counter.sv").write_text(
        "module counter(input logic clk, output logic [1:0] value);\n"
        "  always_ff @(posedge clk) value <= value + 1'b1;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "tb" / "counter_tb.sv").write_text(
        "module counter_tb;\n"
        "  logic clk = 0; logic [1:0] value;\n"
        "  counter dut(.clk(clk), .value(value));\n"
        "  always #1 clk = ~clk;\n"
        "  initial begin repeat (4) @(posedge clk);\n"
        '    $display("[SIM_RESULT] PASSED"); $finish; end\n'
        "endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "counter.core").write_text(
        textwrap.dedent(
            """\
            CAPI=2:
            name: booley:smoke:coverage:1
            filesets:
              rtl:
                files:
                  - rtl/counter.sv: {file_type: systemVerilogSource}
              tb:
                files:
                  - tb/counter_tb.sv: {file_type: systemVerilogSource}
                tags: [tb]
            targets:
              sim:
                flow: sim
                default_tool: verilator
                flow_options:
                  tool: verilator
                  verilator_options: [--timing, --main, --exe]
                filesets: [rtl, tb]
                toplevel: counter_tb
            """
        ),
        encoding="utf-8",
    )
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

    result = collect_target_coverage(
        handle,
        selected_tests=("smoke",),
        artifact_root=tmp_path / "campaign",
        invoke=_invoke,
        options=SimulationOptions(timeout_ms=30_000),
    )

    assert result.status == "complete", [
        (finding.code, finding.message) for finding in result.findings
    ]
    assert result.runs[0].simulation_verdict == "pass"
    assert result.merge.status == "equivalent"
    assert any(point.identity.metric == "line" for point in result.points)


def test_real_custom_main_collects_through_packaged_window_hooks(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "tb").mkdir()
    (tmp_path / "rtl" / "counter.sv").write_text(
        "module counter(input logic clk, output logic [1:0] value = 0);\n"
        "  always_ff @(posedge clk) value <= value + 1'b1;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "tb" / "main.cpp").write_text(
        textwrap.dedent(
            """\
            #include "Vcounter.h"
            #include "booley_coverage.h"
            #include "verilated.h"
            #include <iostream>

            int main(int argc, char** argv) {
                VerilatedContext context;
                context.commandArgs(argc, argv);
                Vcounter dut{&context};
                booley_coverage_start();
                for (int cycle = 0; cycle < 8; ++cycle) {
                    dut.clk = 0; dut.eval();
                    dut.clk = 1; dut.eval();
                }
                dut.final();
                booley_coverage_write();
                std::cout << "[SIM_RESULT] PASSED\\n";
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "counter.core").write_text(
        textwrap.dedent(
            """\
            CAPI=2:
            name: booley:smoke:custom-coverage:1
            filesets:
              rtl:
                files:
                  - rtl/counter.sv: {file_type: systemVerilogSource}
              tb:
                files:
                  - tb/main.cpp: {file_type: cppSource}
                tags: [tb]
            targets:
              sim:
                flow: sim
                default_tool: verilator
                flow_options:
                  tool: verilator
                  verilator_options: [--timing]
                  booley:
                    coverage:
                      reset_included: false
                      custom_main_hooks: [start_hook, write_hook]
                filesets: [rtl, tb]
                toplevel: counter
            """
        ),
        encoding="utf-8",
    )
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")

    result = collect_target_coverage(
        handle,
        selected_tests=("custom",),
        artifact_root=tmp_path / "campaign",
        invoke=_invoke,
        options=SimulationOptions(timeout_ms=30_000),
    )

    assert result.status == "complete", [
        (finding.code, finding.message) for finding in result.findings
    ]
    assert result.runs[0].simulation_verdict == "pass"
    assert result.coverage_window.mode == "post_reset"
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)
    assert result.merge.status == "equivalent"
