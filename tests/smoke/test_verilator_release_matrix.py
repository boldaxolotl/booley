"""Real-tool acceptance matrix for Booley's pinned Verilator release."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

VERILATOR_VERSION = "5.052"
VERILATOR_REF = "ea338be98e1e838d3518809ce8899f85a009963c"
_COVERAGE_FLAGS = (
    "--coverage-line",
    "--coverage-toggle",
    "--coverage-expr",
    "--coverage-user",
    "--coverage-per-instance",
)
_REQUIRED_TOOLS = ("verilator", "verilator_coverage", "cocotb-config", "bwave")

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in _REQUIRED_TOOLS),
    reason="the complete matrix runs inside the built Booley Session Image",
)


def _run(
    argv: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"command failed ({result.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _compile_binary(source: Path, build: Path, *options: str) -> Path:
    _run(
        [
            "verilator",
            "--binary",
            "--build",
            "-j",
            "2",
            "--timing",
            "--Mdir",
            str(build),
            *options,
            str(source),
        ],
        source.parent,
    )
    binary = build / f"V{source.stem}"
    assert binary.is_file()
    return binary


def _write_generated_coverage_source(path: Path) -> None:
    path.write_text(
        """
module counted(input logic clk, input logic enable, output logic [2:0] value = 0);
  always_ff @(posedge clk) if (enable) value <= value + 1'b1;
endmodule
module coverage_top;
  logic clk = 0;
  logic [2:0] first, second;
  counted one(.clk(clk), .enable(1'b1), .value(first));
  counted two(.clk(clk), .enable(first[0]), .value(second));
  always #1 clk = ~clk;
  initial begin
    repeat (8) @(posedge clk);
    cover (first != second);
    $display("PASS generated coverage");
    $finish;
  end
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _write_custom_coverage_sources(source: Path, main: Path) -> None:
    source.write_text(
        """
module custom_dut(input logic clk, output logic [2:0] value = 0);
  always_ff @(posedge clk) value <= value + 1'b1;
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )
    main.write_text(
        """
#include <cstdlib>
#include "Vcustom_dut.h"
#include "verilated.h"
#include "verilated_cov.h"
int main(int argc, char** argv) {
    VerilatedContext context;
    context.commandArgs(argc, argv);
    Vcustom_dut dut{&context};
    for (int cycle = 0; cycle < 8; ++cycle) {
        dut.clk = 0; dut.eval();
        dut.clk = 1; dut.eval();
    }
    const char* path = std::getenv("BOOLEY_COVERAGE_FILE");
    if (!path) return 2;
    VerilatedCov::write(path);
    dut.final();
    return 0;
}
""".lstrip(),
        encoding="utf-8",
    )


def test_pinned_release_identity_is_immutable() -> None:
    version = subprocess.run(
        ["verilator", "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    provenance = Path("/usr/local/share/verilator/BOOLEY-SOURCE.txt").read_text(encoding="utf-8")

    assert f"Verilator {VERILATOR_VERSION}" in version
    assert f"Verilator release: v{VERILATOR_VERSION}" in provenance
    assert f"Verilator source revision: {VERILATOR_REF}" in provenance


def test_nested_shift_compiler_correctness_regression(tmp_path: Path) -> None:
    source = tmp_path / "shiftl_shiftl.sv"
    source.write_text(
        """
module shiftl_shiftl;
  logic [31:0] a;
  logic [5:0] b, c;
  logic [31:0] middle, got, expected;
  assign middle = a << b;
  assign got = middle << c;
  assign expected = a << ({26'b0, b} + {26'b0, c});
  initial begin
    a = 32'h1; b = 6'd32; c = 6'd32; #1;
    if (got !== expected) $fatal(1, "nested shift miscompiled");
    a = 32'h1; b = 6'd40; c = 6'd40; #1;
    if (got !== expected) $fatal(1, "nested shift miscompiled");
    $display("PASS nested shifts");
    $finish;
  end
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = _run([str(_compile_binary(source, tmp_path / "build"))], tmp_path)

    assert "PASS nested shifts" in result.stdout


def test_forceable_unpacked_array_elaborates_and_runs(tmp_path: Path) -> None:
    source = tmp_path / "force_unpacked.sv"
    source.write_text(
        """
module force_unpacked;
  logic var_en [0:1] /*verilator forceable*/;
  logic sig;
  initial begin var_en[0] = 1'b0; var_en[1] = 1'b0; end
  // verilator lint_off IEEEMAYDEPRECATE
  initial assign sig = 1'b1;
  // verilator lint_on IEEEMAYDEPRECATE
  initial begin
    #1; force var_en[0] = 1'b1; #1;
    if (var_en[0] !== 1'b1 || var_en[1] !== 1'b0) $fatal(1, "bad force result");
    $display("PASS unpacked force");
    $finish;
  end
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = _run([str(_compile_binary(source, tmp_path / "build"))], tmp_path)

    assert "PASS unpacked force" in result.stdout


def test_generated_main_native_coverage_is_per_instance_and_mergeable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "coverage_top.sv"
    _write_generated_coverage_source(source)
    binary = _compile_binary(source, tmp_path / "build", *_COVERAGE_FLAGS)
    raw_one = tmp_path / "one.dat"
    raw_two = tmp_path / "two.dat"
    merged = tmp_path / "merged.dat"

    for seed, raw in (("11", raw_one), ("29", raw_two)):
        result = _run(
            [
                str(binary),
                f"+verilator+seed+{seed}",
                f"+verilator+coverage+file+{raw}",
            ],
            tmp_path,
        )
        assert "PASS generated coverage" in result.stdout
        assert raw.is_file()

    _run(
        ["verilator_coverage", "--write", str(merged), str(raw_one), str(raw_two)],
        tmp_path,
    )
    text = merged.read_text(encoding="utf-8")
    assert text.startswith("# SystemC::Coverage-3\n")
    assert "\x01h\x02coverage_top.one" in text
    assert "\x01h\x02coverage_top.two" in text
    assert len(re.findall(r"^C '", text, re.MULTILINE)) > 1


def test_custom_main_writes_selected_native_database(tmp_path: Path) -> None:
    source = tmp_path / "custom_dut.sv"
    main = tmp_path / "custom_main.cpp"
    _write_custom_coverage_sources(source, main)
    build = tmp_path / "build"
    _run(
        [
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "-j",
            "2",
            "--Mdir",
            str(build),
            "--top-module",
            "custom_dut",
            *_COVERAGE_FLAGS,
            str(source),
            str(main),
        ],
        tmp_path,
    )
    raw = tmp_path / "custom.dat"
    environment = os.environ.copy()
    environment["BOOLEY_COVERAGE_FILE"] = str(raw)

    _run([str(build / "Vcustom_dut")], tmp_path, env=environment)

    assert raw.read_text(encoding="utf-8").startswith("# SystemC::Coverage-3\n")


def test_cocotb_verilator_writes_native_coverage(tmp_path: Path) -> None:
    source = tmp_path / "cocotb_dut.sv"
    source.write_text(
        """
module cocotb_dut(input logic clk, input logic enable, output logic [3:0] value = 0);
  always_ff @(posedge clk) if (enable) value <= value + 1'b1;
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "test_cocotb_dut.py").write_text(
        """
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

@cocotb.test()
async def increments(dut):
    dut.enable.value = 1
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    for _ in range(4):
        await RisingEdge(dut.clk)
    assert int(dut.value.value) > 0
""".lstrip(),
        encoding="utf-8",
    )
    raw = tmp_path / "cocotb.dat"
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "\n".join(
            (
                "SIM := verilator",
                "TOPLEVEL_LANG := verilog",
                f"VERILOG_SOURCES := {source}",
                "TOPLEVEL := cocotb_dut",
                "COCOTB_TEST_MODULES := test_cocotb_dut",
                f"COMPILE_ARGS += {' '.join(_COVERAGE_FLAGS)}",
                f"SIM_ARGS += +verilator+coverage+file+{raw}",
                "include $(shell cocotb-config --makefiles)/Makefile.sim",
                "",
            )
        ),
        encoding="utf-8",
    )

    _run(["make", "-f", str(makefile)], tmp_path)

    assert (tmp_path / "results.xml").is_file()
    assert raw.read_text(encoding="utf-8").startswith("# SystemC::Coverage-3\n")


def test_native_fst_is_queryable_by_bwave(tmp_path: Path) -> None:
    source = tmp_path / "fst_top.sv"
    source.write_text(
        """
module fst_top;
  logic clk = 0;
  logic [3:0] count = 0;
  always #1 clk = ~clk;
  always_ff @(posedge clk) count <= count + 1'b1;
  initial begin
    $dumpfile("trace.fst");
    $dumpvars(0, fst_top);
    repeat (6) @(posedge clk);
    $finish;
  end
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )
    binary = _compile_binary(source, tmp_path / "build", "--trace-fst")

    _run([str(binary)], tmp_path)
    listed = _run(["bwave", "list", "trace.fst", "--format", "json"], tmp_path)
    payload = json.loads(listed.stdout)

    assert (tmp_path / "trace.fst").stat().st_size > 0
    assert payload["data"]["signals"]


def test_explicit_seed_is_repeatable_and_diagnostic_is_actionable(tmp_path: Path) -> None:
    source = tmp_path / "seed_top.sv"
    source.write_text(
        """
module seed_top;
  initial begin
    $display("RANDOM=%08x", $urandom);
    $finish;
  end
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )
    binary = _compile_binary(source, tmp_path / "build")
    first = _run([str(binary), "+verilator+seed+123"], tmp_path).stdout
    second = _run([str(binary), "+verilator+seed+123"], tmp_path).stdout
    different = _run([str(binary), "+verilator+seed+124"], tmp_path).stdout

    # Verilator also prints wall-clock telemetry, which is not seed-controlled.
    values = [
        re.findall(r"^RANDOM=([0-9a-f]{8})$", output, flags=re.MULTILINE)
        for output in (first, second, different)
    ]
    assert all(len(value) == 1 for value in values), (first, second, different)
    assert values[0] == values[1]
    assert values[0] != values[2]

    invalid = tmp_path / "invalid.sv"
    invalid.write_text("module invalid; this is not SystemVerilog; endmodule\n", encoding="utf-8")
    diagnostic = _run(["verilator", "--lint-only", str(invalid)], tmp_path, check=False)

    assert diagnostic.returncode != 0
    assert str(invalid) in diagnostic.stderr
    assert "%Error" in diagnostic.stderr
