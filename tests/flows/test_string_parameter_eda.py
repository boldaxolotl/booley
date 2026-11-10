"""Real-tool acceptance for declared string vlogparams (#1439) in the pinned Sandbox.

BOOLEY_REQUIRED_STRING_EDA=1 fails, rather than skips, on missing prerequisites."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.backends import pipeline
from booley.flows.synth.backends.yosys import core
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.flows.synth.timing import StaTimingConfig
from booley.flows.target_parameters import ParameterValue
from booley.targets.catalog import TargetCatalog

pytestmark = pytest.mark.string_parameter_eda


@pytest.fixture(autouse=True)
def prerequisites():
    if missing := [t for t in ("yosys", "sv2v", "verilator", "make") if not shutil.which(t)]:
        if os.environ.get("BOOLEY_REQUIRED_STRING_EDA") == "1":
            pytest.fail(f"Required string EDA prerequisites missing: {missing}")
        pytest.skip(f"String EDA prerequisites missing: {missing}")


def _run(argv, cwd):
    args = [str(arg) for arg in argv]
    result = subprocess.run(
        args, cwd=cwd, text=True, capture_output=True, timeout=120, check=False
    )
    assert result.returncode == 0, f"{argv!r}\n{result.stdout}\n{result.stderr}"
    return result


def _sv_literal(value):
    # Independent fixture spelling: every UTF-8 byte is an octal SV escape.
    return '"' + "".join(f"\\{byte:03o}" for byte in value.encode()) + '"'


def _ok_bit(path, module):
    return json.loads(path.read_text())["modules"][module]["ports"]["ok"]["bits"]


@pytest.mark.parametrize("value", ["MAC", "", '  a" b\\c; d  ', '"MAC"', "é漢\t"])
@pytest.mark.parametrize("frontend", ["slang", "sv2v"])
def test_real_typed_receiver(tmp_path, value, frontend):
    header = 'module top #(parameter string NAME="X") (output ok);'
    (tmp_path / "top.sv").write_text(f"{header} assign ok=(NAME=={_sv_literal(value)}); endmodule")
    if frontend == "sv2v":
        (tmp_path / core.SV2V_OUTPUT_NAME).write_text(_run(["sv2v", "top.sv"], tmp_path).stdout)
    spec = pipeline.SynthSpec(
        design_name="top",
        sources=(tmp_path / "top.sv",),
        inc_dirs=(),
        defines=(),
        params={"NAME": ParameterValue("str", value)},
        liberty=Path("unused.lib"),
        liberty_found=True,
        flatten=True,
        abc_recipe=None,
        frontend=frontend,
        timing=StaTimingConfig(mode="logical"),
    )
    pipeline._write_yosys_script(spec, tmp_path)
    script = tmp_path / "synth.ys"
    lines = script.read_text().splitlines()  # run only the frontend, before tech mapping
    frontend_lines = lines[: next(i for i, line in enumerate(lines) if line.startswith("synth"))]
    script.write_text("\n".join([*frontend_lines, "flatten", "opt", "write_json r.json"]))
    _run(["yosys", "-Q", "-T", "-s", "synth.ys"], tmp_path)
    assert _ok_bit(tmp_path / "r.json", "top") == ["1"]


def test_same_unchanged_core_simulation_and_synthesis_flows(tmp_path, monkeypatch):
    """One `.core` with a plain `default: MAC` builds in Simulation, Lint and Synthesis."""
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley/booley.toml").write_text(
        "[flows.sim]\nenabled=true\n[flows.lint]\nenabled=true\n"
    )
    check = 'module dut #(parameter string NAME="X") (output ok); assign ok=(NAME=="MAC");'
    (tmp_path / "dut.sv").write_text(check + " endmodule\n")
    (tmp_path / "tb.sv").write_text(
        'module tb #(parameter string NAME="X")(); wire ok; dut #(.NAME(NAME)) d(ok);\n'
        'initial begin #1; if (!ok) $fatal(1, "mismatch"); $display("SIM_OK"); end\nendmodule\n'
    )
    (tmp_path / "shared.core").write_text("""CAPI=2:
name: proof:string:shared:1
filesets:
  rtl: {files: [dut.sv], file_type: systemVerilogSource}
  tb: {files: [tb.sv], file_type: systemVerilogSource}
parameters:
  NAME: {datatype: str, paramtype: vlogparam, default: MAC}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator, mode: binary, verilator_options: [--timing]}
    filesets: [rtl, tb]
    parameters: [NAME]
    toplevel: tb
  lint:
    flow: lint
    flow_options: {tool: verilator}
    filesets: [rtl]
    parameters: [NAME]
    toplevel: dut
  synth:
    flow: generic
    flow_options: {tool: yosys, frontend: slang, synth_mode: logical, arch: xilinx}
    filesets: [rtl]
    parameters: [NAME]
    toplevel: dut
""")
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    library = tmp_path / "cell" / "lib"
    library.mkdir(parents=True)
    (library / "NangateOpenCellLibrary_typical_ccs.lib").write_text(
        "library(proof) { cell(INV) { area: 1; pin(A) { direction: input; } "
        'pin(Y) { direction: output; function: "!A"; } } }\n'
    )
    monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path))
    sim = SimulateFlow()
    sim.parse_args(["--project", str(tmp_path), "--target", "sim", "--mode", "elab-only"])
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    build_root = tmp_path / "sim-build"
    prepared = sim._prepare_elab_only_target(
        "sim", time.monotonic(), handle=handle, build_root=build_root
    )
    _run(prepared.make_argv, tmp_path)
    assert "SIM_OK" in _run([prepared.build_root / "Vtb"], prepared.build_root).stdout
    lint = LintFlow()
    lint.parse_args(["--project", str(tmp_path), "--target", "lint"])
    lint_handle = TargetCatalog.build(tmp_path).select("lint", for_flow="lint")
    _run(lint._prepare_lint_command(lint_handle).command, tmp_path)
    synth = AsicSynthesizeFlow()
    synth.parse_args(["--project", str(tmp_path), "--target", "synth"])
    plan = synth._configure_synth("synth", synth._build_synth_cmd("synth"))
    _run(synth._synth_boundary_cmd(plan), tmp_path)
    _run(["yosys", "-Q", "-p", "read_verilog synth_dut.v; write_json s.json"], plan.build_dir)
    assert _ok_bit(plan.build_dir / "s.json", "dut") == ["1"]
