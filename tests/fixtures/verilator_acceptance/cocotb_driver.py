"""Invoked in a bounded subprocess by the image acceptance suite."""

import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

here = Path(__file__).resolve().parent
runner = get_runner("verilator")
runner.build(
    sources=[here / "coverage source.sv"],
    hdl_toplevel="top",
    build_dir=Path.cwd() / "obj",
    build_args=[
        "--timing",
        "-Wno-fatal",
        "--coverage-line",
        "--coverage-toggle",
        "--coverage-expr",
        "--coverage-user",
        "--coverage-per-instance",
    ],
    timescale=("1ns", "1ps"),
)
runner.test(
    test_module="test_coverage_cocotb",
    hdl_toplevel="top",
    test_dir=Path.cwd(),
    seed=123,
    test_args=["--coverage-per-instance"],
    plusargs=["+verilator+seed+123", f"+verilator+coverage+file+{sys.argv[1]}"],
    extra_env={"PYTHONPATH": str(here)},
    results_xml=sys.argv[2],
)
