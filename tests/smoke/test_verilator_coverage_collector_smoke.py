from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
        if "Verilator 5.046" not in version or "24b2ac2" not in version:
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
