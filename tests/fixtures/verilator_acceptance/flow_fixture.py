"""Small projects exercising real Booley execution and coverage finalization."""

from pathlib import Path

import yaml


def write_project(root: Path, harness: str, outcome: str) -> None:
    root.mkdir(parents=True)
    (root / ".booley_project").mkdir()
    (root / ".booley_project" / "booley.toml").write_text('[project]\nname = "acceptance"\n')
    rtl = "module counter(input logic clk, output logic [1:0] value = 0);\n"
    rtl += "always_ff @(posedge clk) value <= value + 1'b1;\nendmodule\n"
    if outcome == "compile":
        rtl += "module broken; final begin #1; end endmodule\n"
    (root / "counter.sv").write_text(rtl)
    if harness == "generated":
        files, options, top = _generated(root, outcome)
    elif harness == "custom":
        files, options, top = _custom(root, outcome)
    else:
        files, options, top = _cocotb(root, outcome)
    if outcome == "compile":
        (root / "counter.sv").write_text("module counter; invalid syntax ! endmodule\n")
    core = {
        "name": "booley:acceptance:verilator:1",
        "filesets": {
            "rtl": {"files": [{"counter.sv": {"file_type": "systemVerilogSource"}}]},
            "tb": {"files": files, "tags": ["tb"]},
        },
        "targets": {
            "sim": {
                "flow": "sim",
                "flow_options": options,
                "filesets": ["rtl", "tb"],
                "toplevel": top,
            }
        },
    }
    (root / "acceptance.core").write_text("CAPI=2:\n" + yaml.safe_dump(core))


def _generated(root: Path, outcome: str):
    statements = {
        "pass": '$display("[SIM_RESULT] PASSED"); $finish;',
        "fail": '$fatal(1, "intentional failure");',
        "timeout": "forever #1;",
        "signal": '$system("kill -TERM $PPID");',
        "compile": "$finish;",
    }
    source = (
        """module top;
logic clk = 0; wire [1:0] value; counter dut(clk, value);
always #1 clk = ~clk;
initial begin repeat (4) @(posedge clk);
"""
        + statements[outcome]
        + "\nend\nendmodule\n"
    )
    (root / "top.sv").write_text(source)
    files = [{"top.sv": {"file_type": "systemVerilogSource"}}]
    return (
        files,
        {"tool": "verilator", "verilator_options": ["--timing", "--main", "--exe", "-Wno-fatal"]},
        "top",
    )


def _custom(root: Path, outcome: str):
    statements = {
        "pass": 'std::cout << "[SIM_RESULT] PASSED\\n"; return 0;',
        "fail": 'std::cerr << "[SIM_RESULT] FAILED\\n"; return 1;',
        "timeout": "std::this_thread::sleep_for(std::chrono::seconds(30)); return 2;",
        "signal": "std::raise(SIGTERM); return 3;",
        "compile": "return 0;",
    }
    source = (
        """#include "Vcounter.h"
#include "verilated.h"
#include <iostream>
#include <csignal>
#include <thread>
#include <chrono>
#if VM_COVERAGE
#include "booley_coverage.h"
#endif
int main(int argc, char** argv) {
VerilatedContext context; context.commandArgs(argc, argv); Vcounter dut{&context};
#if VM_COVERAGE
booley_coverage_start();
#endif
for (int i=0; i<8; ++i) {dut.clk=0; dut.eval(); dut.clk=1; dut.eval();}
#if VM_COVERAGE
booley_coverage_write();
#endif
"""
        + statements[outcome]
        + "\n}\n"
    )
    (root / "main.cpp").write_text(source)
    options = {
        "tool": "verilator",
        "verilator_options": ["--timing", "-Wno-fatal"],
        "booley": {
            "coverage": {
                "reset_included": False,
                "custom_main_hooks": ["start_hook", "write_hook"],
            }
        },
    }
    return [{"main.cpp": {"file_type": "cppSource"}}], options, "counter"


def _cocotb(root: Path, outcome: str):
    statements = {
        "pass": "assert int(dut.value.value) == 0",
        "fail": 'assert False, "intentional failure"',
        "timeout": "await Timer(30, unit='sec')",
        "signal": "os.kill(os.getpid(), signal.SIGTERM)",
        "compile": "assert True",
    }
    source = (
        """import os
import signal
import cocotb
from cocotb.triggers import Timer
@cocotb.test()
async def check(dut):
    dut.clk.value = 0
    for i in range(4):
        await Timer(1, unit="ns")
        dut.clk.value = 1
        await Timer(1, unit="ns")
        dut.clk.value = 0
    """
        + statements[outcome]
        + "\n"
    )
    # A simulator time jump is instant; use endless timed edges to test a wall timeout.
    if outcome == "timeout":
        source = source.replace(
            "await Timer(30, unit='sec')", 'while True:\n        await Timer(1, unit="ns")'
        )
    (root / "test_case.py").write_text(source)
    files = [{"test_case.py": {"file_type": "user", "copyto": "test_case.py"}}]
    return (
        files,
        {
            "tool": "verilator",
            "cocotb_module": "test_case",
            "verilator_options": ["--timing", "-Wno-fatal"],
            "timescale": "1ns/1ps",
        },
        "counter",
    )
