"""Required receiver and unchanged-core acceptance in the pinned Sandbox.

Set BOOLEY_REQUIRED_STRING_EDA=1 to fail missing prerequisites instead of skip.
The ordinary Python profile does not stand in for this suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from booley.flows import edam as edam_layer
from booley.flows.lint.flow import LintFlow
from booley.flows.parameter_transport import normalize_parameter_records
from booley.flows.sim.build_session import SimulationBuildSession
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.backends import pipeline
from booley.flows.synth.backends.yosys import core
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.flows.synth.timing import StaTimingConfig
from booley.flows.target_parameters import ParameterValue
from booley.targets.catalog import TargetCatalog

pytestmark = pytest.mark.string_parameter_eda
VALUES = [
    "MAC",
    "123",
    "",
    '  a" b\\c; d  ',
    '"MAC"',
    "# $ [ { // \\",
    "\n\t\r",
    "é漢",
    "ERROR: Unsupported Syntax error",
]


@pytest.fixture(autouse=True)
def prerequisites():
    missing = [
        name
        for name in ("yosys", "sv2v", "verilator", "iverilog", "vvp", "make", "cocotb-config")
        if shutil.which(name) is None
    ]
    if missing:
        if os.environ.get("BOOLEY_REQUIRED_STRING_EDA") == "1":
            pytest.fail(f"Required string EDA prerequisites missing: {missing}")
        pytest.skip(f"String EDA prerequisites missing: {missing}")


def _run(argv, cwd, environment=None):
    result = subprocess.run(
        [str(arg) for arg in argv],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, f"{argv!r}\n{result.stdout}\n{result.stderr}"
    return result


def _expected(value):
    # Independent fixture spelling: every UTF-8 byte is an octal SV escape.
    return '"' + "".join(f"\\{byte:03o}" for byte in value.encode()) + '"'


def _spec(root, value, frontend="sv2v", params=None):
    return pipeline.SynthSpec(
        design_name="top",
        sources=(root / "top.sv",),
        inc_dirs=(),
        defines=(),
        params=params or {"NAME": ParameterValue("str", value)},
        liberty=Path("/opt/pdk/nangate45/lib/NangateOpenCellLibrary_typical.lib"),
        liberty_found=True,
        flatten=True,
        abc_recipe=None,
        frontend=frontend,
        timing=StaTimingConfig(mode="logical"),
    )


def _read_constant(root, name, port="ok"):
    module = json.loads((root / name).read_text())["modules"]["top"]
    return module["ports"][port]["bits"], module


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("frontend", ["slang", "sv2v"])
def test_real_typed_receiver(tmp_path, value, frontend):
    source = f"""module child #(parameter string NAME="X") (output ok);
assign ok=(NAME=={_expected(value)});
endmodule
module top #(parameter string NAME="X", parameter integer N=12) (output ok, output [32767:0] bytes);
child #(.NAME(NAME)) child_instance(ok);
assign bytes=NAME;
endmodule
"""
    (tmp_path / "top.sv").write_text(source)
    if frontend == "slang":
        # String-to-packed conversions are not supported by slang's sized cast.
        # Byte equality is checked independently in the child constant instead.
        source = source.replace("assign bytes=NAME;", "assign bytes='0;")
        (tmp_path / "top.sv").write_text(source)
    else:
        converted = _run(["sv2v", "top.sv"], tmp_path).stdout
        (tmp_path / core.SV2V_OUTPUT_NAME).write_text(converted)
    build = _spec(tmp_path, value, frontend)
    pipeline._write_yosys_script(build, tmp_path)
    script = tmp_path / "synth.ys"
    # Execute the actual writer's frontend commands, before tech mapping.
    lines = script.read_text().splitlines()
    boundary = next(i for i, line in enumerate(lines) if line.startswith("synth -top"))
    script.write_text(
        "\n".join(lines[:boundary])
        + "\nflatten\nopt\nwrite_json result.json\nwrite_rtlil result.il\n"
    )
    result = _run(["yosys", "-Q", "-T", "-s", "synth.ys"], tmp_path)
    (tmp_path / "yosys.log").write_text(result.stdout + result.stderr)
    assert core.scan_synth_logs(tmp_path) is None
    bits, module = _read_constant(tmp_path, "result.json")
    assert bits == ["1"]
    if frontend == "sv2v":
        actual = int("".join(reversed(module["ports"]["bytes"]["bits"])), 2)
        assert actual == int.from_bytes(value.encode(), "big")
        if value:
            assert len(module["parameter_default_values"]["NAME"]) == 8 * len(value.encode())
        else:
            rtlil = (tmp_path / "result.il").read_text()
            assert 'parameter \\NAME ""' in rtlil
            _run(
                ["yosys", "-Q", "-T", "-p", "read_rtlil result.il; write_rtlil roundtrip.il"],
                tmp_path,
            )
            assert 'parameter \\NAME ""' in (tmp_path / "roundtrip.il").read_text()
            numeric_script = script.read_text().replace('-set NAME ""', "-set NAME 8'h00")
            assert numeric_script != script.read_text()
            (tmp_path / "numeric.ys").write_text(numeric_script)
            _run(["yosys", "-Q", "-T", "-s", "numeric.ys"], tmp_path)
            assert _read_constant(tmp_path, "result.json")[0] == ["1"]
            numeric_rtlil = (tmp_path / "result.il").read_text()
            assert "parameter \\NAME 8'00000000" in numeric_rtlil
            assert 'parameter \\NAME ""' not in numeric_rtlil


@pytest.mark.parametrize("tool", ["verilator", "icarus"])
@pytest.mark.parametrize("value", VALUES)
def test_real_simulator_response_receiver(tmp_path, tool, value):
    declaration = 'parameter string NAME="X"' if tool == "verilator" else 'parameter NAME="X"'
    checks = ""
    if tool == "verilator":
        checks = f'if (NAME.len()!={len(value.encode())}) $fatal(1,"length mismatch");\n'
        checks += "\n".join(
            f'if (NAME[{index}]!=8\'h{byte:02x}) $fatal(1,"byte mismatch");'
            for index, byte in enumerate(value.encode())
        )
    (tmp_path / "top.sv").write_text(f"""module top #({declaration})();
initial begin
 {checks}
 if(NAME!={_expected(value)}) $fatal(1,"value mismatch");
 $display("STRING_VALUE_OK"); $finish;
end
endmodule
""")
    options = {"mode": "binary"} if tool == "verilator" else {"iverilog_options": ["-g2012"]}
    edam = _receiver_edam(tool, value, options)
    edam_layer.configure("sim", edam, tmp_path)
    resolved = SimpleNamespace(
        build_root=tmp_path,
        configured_eda_tool=tool,
        toplevel="top",
        parameters=edam["parameters"],
    )
    normalize_parameter_records(resolved)
    _run(["make", "-C", tmp_path], tmp_path)
    result = (
        _run([tmp_path / "Vtop"], tmp_path)
        if tool == "verilator"
        else _run(["vvp", "top"], tmp_path)
    )
    assert "STRING_VALUE_OK" in result.stdout


def _project(root, value, *, explicit_empty=False):
    (root / ".booley_project").mkdir()
    (root / ".booley_project/booley.toml").write_text(
        "[flows.sim]\nenabled=true\n[flows.synth]\nenabled=true\n"
    )
    (root / "dut.sv").write_text(f"""module dut #(parameter string NAME="X") (output ok);
assign ok=(NAME=={_expected(value)});
endmodule
""")
    (root / "tb.sv").write_text(f"""module tb #(parameter string NAME="X")();
wire ok; dut #(.NAME(NAME)) dut_instance(ok);
initial begin
 #1;
 if (!ok || NAME!={_expected(value)}) $fatal(1,"core value mismatch");
 $display("DUAL_FLOW_VALUE_OK"); $finish;
end
endmodule
""")
    (root / "main.cpp").write_text("""#include "Vtb.h"
#include "verilated.h"
int main(int argc, char **argv) {
 VerilatedContext context; context.commandArgs(argc, argv);
 Vtb top{&context};
 while (!context.gotFinish()) {
  top.eval();
  if (!top.eventsPending()) break;
  context.time(top.nextTimeSlot());
 }
 top.final(); return context.gotFinish() ? 0 : 1;
}
""")
    _write_shared_core(root, value, explicit_empty)


def _write_shared_core(root, value, explicit_empty):
    data = {
        "name": "proof:string:shared:1",
        "filesets": {
            "rtl": {"files": ["dut.sv"], "file_type": "systemVerilogSource"},
            "tb": {"files": ["tb.sv"], "file_type": "systemVerilogSource"},
            "driver": {"files": ["main.cpp"], "file_type": "cppSource"},
        },
        "parameters": {
            "NAME": {
                "datatype": "str",
                "paramtype": "vlogparam",
                "default": "fallback" if explicit_empty else value,
            }
        },
        "targets": {
            "sim": {
                "flow": "sim",
                "flow_options": {
                    "tool": "verilator",
                    "mode": "cc",
                    "verilator_options": ["--timing"],
                },
                "filesets": ["rtl", "tb", "driver"],
                "parameters": ["NAME=" if explicit_empty else "NAME"],
                "toplevel": "tb",
            },
            "lint": {
                "flow": "lint",
                "flow_options": {"tool": "verilator"},
                "filesets": ["rtl"],
                "parameters": ["NAME=" if explicit_empty else "NAME"],
                "toplevel": "dut",
            },
            "synth": {
                "flow": "generic",
                "flow_options": {
                    "tool": "yosys",
                    "frontend": "slang",
                    "synth_mode": "logical",
                    "arch": "xilinx",
                },
                "filesets": ["rtl"],
                "parameters": ["NAME=" if explicit_empty else "NAME"],
                "toplevel": "dut",
            },
        },
    }
    (root / "shared.core").write_text("CAPI=2:\n" + yaml.safe_dump(data, sort_keys=False))


@pytest.mark.parametrize(
    "value,explicit_empty",
    [("MAC", False), ('a" b\\c; d', False), ('"MAC"', False), ("", False), ("", True)],
)
def test_same_unchanged_core_simulation_and_synthesis_flows(
    tmp_path, monkeypatch, value, explicit_empty
):
    _project(tmp_path, value, explicit_empty=explicit_empty)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / ".booley_project"))
    _install_liberty(tmp_path, monkeypatch)
    original = hashlib.sha256((tmp_path / "shared.core").read_bytes()).hexdigest()
    sim = SimulateFlow()
    sim.parse_args(["--project", str(tmp_path), "--target", "sim", "--mode", "elab-only"])
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    _build_and_run_simulation(sim, handle, tmp_path)
    lint = LintFlow()
    lint.parse_args(["--project", str(tmp_path), "--target", "lint"])
    lint_handle = TargetCatalog.build(tmp_path).select("lint", for_flow="lint")
    prepared_lint = lint._prepare_lint_command(lint_handle)
    _run(prepared_lint.command, tmp_path)
    synth = AsicSynthesizeFlow()
    synth.parse_args(["--project", str(tmp_path), "--target", "synth"])
    command = synth._build_synth_cmd("synth")
    plan = synth._configure_synth("synth", command)
    _run(synth._synth_boundary_cmd(plan), tmp_path)
    # Inspect final synthesized constant through an independent Yosys load.
    _run(
        [
            "yosys",
            "-Q",
            "-T",
            "-p",
            "read_verilog synth_dut.v; hierarchy -top dut; write_json semantic.json",
        ],
        plan.build_dir,
    )
    module = json.loads((plan.build_dir / "semantic.json").read_text())["modules"]["dut"]
    assert module["ports"]["ok"]["bits"] == ["1"]
    assert hashlib.sha256((tmp_path / "shared.core").read_bytes()).hexdigest() == original


def _install_liberty(root, monkeypatch):
    library = root / "cell" / "lib"
    library.mkdir(parents=True)
    (library / "NangateOpenCellLibrary_typical_ccs.lib").write_text(
        'library(proof) { cell(INV) { area: 1; pin(A) { direction: input; } pin(Y) { direction: output; function: "!A"; } } }\n'
    )
    monkeypatch.setenv("PRJ_LIB_DIR", str(root))
    return library / "NangateOpenCellLibrary_typical_ccs.lib"


@pytest.mark.parametrize("empty_last", [False, True])
def test_empty_through_production_makefile(tmp_path, monkeypatch, empty_last):
    liberty = _install_liberty(tmp_path, monkeypatch)
    (tmp_path / "top.sv").write_text(
        'module top #(parameter string NAME="X", parameter integer N=12) (output ok); assign ok=(NAME=="" && N==12); endmodule\n'
    )
    parameters = (
        {"N": "12", "NAME": ParameterValue("str", "")}
        if empty_last
        else {"NAME": ParameterValue("str", ""), "N": "12"}
    )
    from dataclasses import replace

    build = replace(_spec(tmp_path, ""), params=parameters, liberty=liberty)
    plan = pipeline.configure_synthesis(build, tmp_path / "build")
    _run(["make", "-C", plan.build_dir], tmp_path)
    artifact = plan.build_dir / core.effective_params_filename("top")
    assert 'parameter \\NAME ""' in artifact.read_text()
    _run(
        [
            "yosys",
            "-Q",
            "-T",
            "-p",
            "read_verilog synth_top.v; hierarchy -top top; write_json semantic.json",
        ],
        plan.build_dir,
    )
    assert _read_constant(plan.build_dir, "semantic.json")[0] == ["1"]


def test_awkward_file_parameter_selects_declared_resource(tmp_path, monkeypatch):
    _install_liberty(tmp_path, monkeypatch)
    selected = tmp_path / 'data a"b\\c.mem'
    resource = tmp_path / "declared.mem"
    resource.write_text("a5\n")
    selected.symlink_to(resource.name)
    assert selected.samefile(resource)
    default = tmp_path / "default.mem"
    default.write_text("11\n")
    (tmp_path / "top.sv").write_text(
        f"module top #(parameter string FILE={_expected(str(default))}) (output [7:0] value); reg [7:0] mem[0:0]; initial $readmemh(FILE,mem); assign value=mem[0]; endmodule\n"
    )
    _declare_file_consumer(tmp_path, selected)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / ".booley_project"))
    flow = AsicSynthesizeFlow()
    flow.parse_args(["--project", str(tmp_path), "--target", "synth"])
    command = flow._build_synth_cmd("synth")
    plan = flow._configure_synth("synth", command)
    _run(flow._synth_boundary_cmd(plan), tmp_path)
    _run(
        [
            "yosys",
            "-Q",
            "-T",
            "-p",
            "read_verilog synth_top.v; hierarchy -top top; write_json semantic.json",
        ],
        plan.build_dir,
    )
    bits, _module = _read_constant(plan.build_dir, "semantic.json", "value")
    assert int("".join(reversed(bits)), 2) == 0xA5


def test_long_parameter_exact_width_and_bytes(tmp_path):
    value = "a" * 4096
    (tmp_path / "top.sv").write_text(
        'module top #(parameter string NAME="X") (output [32767:0] bytes); assign bytes=NAME; endmodule\n'
    )
    (tmp_path / core.SV2V_OUTPUT_NAME).write_text(_run(["sv2v", "top.sv"], tmp_path).stdout)
    commands = core._rtl_frontend_commands(
        [Path(core.SV2V_OUTPUT_NAME)],
        "top",
        frontend="sv2v",
        inc_dirs=[],
        defines=[],
        params={"NAME": ParameterValue("str", value)},
        slang_options=[],
    )
    (tmp_path / "long.ys").write_text("\n".join([*commands, "opt", "write_json result.json"]))
    _run(["yosys", "-Q", "-T", "-s", "long.ys"], tmp_path)
    bits, module = _read_constant(tmp_path, "result.json", "bytes")
    assert int("".join(reversed(bits)), 2) == int.from_bytes(value.encode(), "big")
    assert len(module["parameter_default_values"]["NAME"]) == 32768


@pytest.mark.parametrize("value", ["MAC", "", "MACX", 'a" b\\c'])
def test_forwarding_formatting_unequal_lengths_and_marker_concat(tmp_path, value):
    width = max(8, len(value.encode()) * 8)
    (
        tmp_path / "top.sv"
    ).write_text(f"""module child #(parameter string NAME="X") (output [255:0] formatted);
assign formatted=$sformatf("%s",NAME);
endmodule
module top #(parameter string NAME="X") (output [255:0] formatted,output equal_short,equal_long,output [{width + 15}:0] marker);
child #(.NAME(NAME)) forwarded(formatted);
assign equal_short=(NAME=="MAC"); assign equal_long=(NAME=="MACX");
assign marker={{8'ha5,NAME,8'h5a}};
endmodule
""")
    (tmp_path / core.SV2V_OUTPUT_NAME).write_text(_run(["sv2v", "top.sv"], tmp_path).stdout)
    commands = core._rtl_frontend_commands(
        [Path(core.SV2V_OUTPUT_NAME)],
        "top",
        frontend="sv2v",
        inc_dirs=[],
        defines=[],
        params={"NAME": ParameterValue("str", value)},
        slang_options=[],
    )
    (tmp_path / "format.ys").write_text(
        "\n".join([*commands, "flatten", "opt", "write_json result.json"])
    )
    _run(["yosys", "-Q", "-T", "-s", "format.ys"], tmp_path)
    formatted, module = _read_constant(tmp_path, "result.json", "formatted")
    assert int("".join(reversed(formatted)), 2) == int.from_bytes(value.encode(), "big")
    assert module["ports"]["equal_short"]["bits"] == ["1" if value == "MAC" else "0"]
    assert module["ports"]["equal_long"]["bits"] == ["1" if value == "MACX" else "0"]
    marker = int("".join(reversed(module["ports"]["marker"]["bits"])), 2)
    assert marker == int.from_bytes(b"\xa5" + (value.encode() or b"\x00") + b"\x5a", "big")


def test_real_capi2_declared_string_coercion(tmp_path):
    from booley.flows.target_parameters import vlogparam_specs
    from booley.fusesoc.fusesoc_registry import resolve_target_handle

    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    declarations = {
        "TEXT_TRUE": {"datatype": "str", "paramtype": "vlogparam", "default": True},
        "TEXT_FALSE": {"datatype": "str", "paramtype": "vlogparam", "default": False},
        "TEXT_NUMBER": {"datatype": "str", "paramtype": "vlogparam", "default": 123},
        "EMPTY": {"datatype": "str", "paramtype": "vlogparam", "default": ""},
        "EMPTY_FILE": {"datatype": "file", "paramtype": "vlogparam", "default": ""},
        "FILE": {"datatype": "file", "paramtype": "vlogparam", "default": '../raw a"b\\c.mem'},
        "BOOL": {"datatype": "bool", "paramtype": "vlogparam", "default": True},
    }
    data = {
        "name": "proof:string:coercion:1",
        "filesets": {"rtl": {"files": ["top.sv"], "file_type": "systemVerilogSource"}},
        "parameters": declarations,
        "targets": {
            "synth": {
                "flow": "generic",
                "flow_options": {"tool": "yosys", "arch": "xilinx"},
                "filesets": ["rtl"],
                "parameters": list(declarations),
                "toplevel": "top",
            }
        },
    }
    (tmp_path / "proof.core").write_text("CAPI=2:\n" + yaml.safe_dump(data))
    handle = TargetCatalog.build(tmp_path).select("synth", for_flow="synth")
    resolved = resolve_target_handle(handle, build_root=tmp_path / "build")
    values = vlogparam_specs(resolved.parameters)
    assert values["TEXT_TRUE"].value == "True"
    assert values["TEXT_FALSE"].value == "False"
    assert values["TEXT_NUMBER"].value == "123"
    assert "EMPTY" in values, resolved.parameters
    assert values["EMPTY"].value == ""
    assert "EMPTY_FILE" not in values
    assert "default" not in resolved.parameters["EMPTY_FILE"]
    assert values["BOOL"].value == "1"
    assert values["FILE"] == ParameterValue("file", '../raw a"b\\c.mem')
    assert all(
        resolved.parameters[name]["datatype"] == spec["datatype"]
        for name, spec in declarations.items()
    )


def _build_and_run_simulation(flow, handle, root):
    with SimulationBuildSession(handle) as session:
        prepared = flow._prepare_elab_only_target(
            "sim", time.monotonic(), handle=handle, build_root=session.new_generation()
        )
        assert hasattr(prepared, "make_argv"), prepared
        inputs = session.capture_inputs(prepared)
        key = session.reusable_key(prepared, inputs, hooks=False)
        assert key is not None
        _run(prepared.make_argv, root)
        session.authorize_fresh_image(prepared, inputs, key)
        executable = prepared.build_root / "Vtb"
        assert "DUAL_FLOW_VALUE_OK" in _run([executable], prepared.build_root).stdout
        candidate = flow._prepare_elab_only_target(
            "sim", time.monotonic(), handle=handle, build_root=session.new_generation()
        )
        candidate_key = session.reusable_key(
            candidate, session.capture_inputs(candidate), hooks=False
        )
        assert candidate_key == key
        reused = session.try_reuse(candidate, candidate_key)
        assert reused is not None, session.cache_decision
        assert reused.build_root == prepared.build_root
        assert "DUAL_FLOW_VALUE_OK" in _run([reused.build_root / "Vtb"], reused.build_root).stdout


def _declare_file_consumer(root, selected):
    """Declare resources while preserving an explicitly absolute file default."""
    (root / ".booley_project").mkdir()
    (root / ".booley_project" / "booley.toml").write_text("[flows.synth]\nenabled=true\n")
    data = {
        "name": "proof:string:resource:1",
        "filesets": {
            "rtl": {"files": ["top.sv"], "file_type": "systemVerilogSource"},
            "data": {"files": ["declared.mem", "default.mem"], "file_type": "user"},
        },
        "parameters": {
            "FILE": {"datatype": "file", "paramtype": "vlogparam", "default": str(selected)}
        },
        "targets": {
            "synth": {
                "flow": "generic",
                "flow_options": {
                    "tool": "yosys",
                    "arch": "xilinx",
                    "frontend": "sv2v",
                    "synth_mode": "logical",
                },
                "filesets": ["rtl", "data"],
                "parameters": ["FILE"],
                "toplevel": "top",
            }
        },
    }
    (root / "resource.core").write_text("CAPI=2:\n" + yaml.safe_dump(data))


def _configure_verilator_case(root, value, *, mode="binary", normalize=True, options=()):
    edam = _receiver_edam("verilator", value, {"mode": mode, "verilator_options": list(options)})
    edam_layer.configure("lint" if mode == "lint-only" else "sim", edam, root)
    if normalize:
        normalize_parameter_records(
            SimpleNamespace(
                build_root=root,
                configured_eda_tool="verilator",
                toplevel="top",
                parameters=edam["parameters"],
            )
        )


def _compatibility_source(typed):
    declaration = 'parameter string NAME="X"' if typed else 'parameter NAME="X"'
    return f"""function automatic string as_string(input string value);
return value;
endfunction
module child #(parameter string VALUE="X") (output ok);
assign ok=(VALUE=="MAC" && VALUE.len()==3);
endmodule
module top #({declaration})();
wire ok; wire unused_signal;
child #(.VALUE(NAME)) forwarded(ok);
initial begin
 #1;
 if(!ok) $fatal(1,"forwarding mismatch");
 $display(NAME);
 $display("FORMAT %s FUNCTION %s",NAME,as_string(NAME));
 $display("CONTEXT_OK"); $finish;
end
endmodule
"""


@pytest.mark.parametrize("typed", [True, False])
def test_quoted_clean_and_packed_semantics_and_lint_diagnostics_match(tmp_path, typed):
    import re

    from booley.flows.lint.flow import parse_warnings

    outputs = []
    diagnostics = []
    for normalize in [False, True]:
        root = tmp_path / ("packed" if normalize else "quoted")
        root.mkdir()
        (root / "top.sv").write_text(_compatibility_source(typed))
        _configure_verilator_case(root, "MAC", normalize=normalize)
        _run(["make", "-C", root], root)
        output = _run([root / "Vtop"], root).stdout
        outputs.append(
            [
                line
                for line in output.splitlines()
                if line.strip() in ("MAC", "5062979", "CONTEXT_OK") or line.startswith("FORMAT")
            ]
        )
        lint = root / "lint"
        lint.mkdir()
        (lint / "top.sv").write_text(_compatibility_source(typed))
        _configure_verilator_case(
            lint,
            "MAC",
            mode="lint-only",
            normalize=normalize,
            options=("--timing", "--Wall", "-Wno-fatal"),
        )
        result = _run(["make", "-C", lint], lint)
        messages = result.stdout + result.stderr
        diagnostics.append(set(re.findall(r"%((?:Warning|Error)(?:-[A-Z0-9_]+)?):", messages)))
        assert parse_warnings(messages, "lint"), (
            "Deliberate unused signal must remain a real lint finding"
        )
    assert outputs[0] == outputs[1]
    assert ("MAC" if typed else "5062979") in [line.strip() for line in outputs[1]]
    assert "FORMAT MAC FUNCTION MAC" in outputs[1]
    assert diagnostics[0] == diagnostics[1]


@pytest.mark.parametrize(
    "size,options,passes",
    [(8192, (), True), (8193, (), False), (8193, ("--max-num-width", "65544"), True)],
)
def test_native_verilator_width_limit_and_explicit_target_option(
    tmp_path, monkeypatch, size, options, passes
):
    value = "a" * size
    (tmp_path / "top.sv").write_text(
        f'module top #(parameter string NAME="X") (output ok); assign ok=(NAME.len()=={size}); endmodule\n'
    )
    (tmp_path / ".booley_project").mkdir()
    (tmp_path / ".booley_project/booley.toml").write_text("[flows.lint]\nenabled=true\n")
    (tmp_path / "proof.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump(
            {
                "name": "proof:string:width:1",
                "filesets": {"rtl": {"files": ["top.sv"], "file_type": "systemVerilogSource"}},
                "parameters": {
                    "NAME": {"datatype": "str", "paramtype": "vlogparam", "default": value}
                },
                "targets": {
                    "lint": {
                        "flow": "lint",
                        "flow_options": {"tool": "verilator", "verilator_options": list(options)},
                        "filesets": ["rtl"],
                        "parameters": ["NAME"],
                        "toplevel": "top",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / ".booley_project"))
    flow = LintFlow()
    flow.parse_args(["--project", str(tmp_path), "--target", "lint"])
    handle = TargetCatalog.build(tmp_path).select("lint", for_flow="lint")
    prepared = flow._prepare_lint_command(handle)
    result = subprocess.run(
        prepared.command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    if not passes:
        assert "Width of number exceeds implementation limit" in result.stdout + result.stderr


def _receiver_edam(tool, value, options):
    """Resolved typed EDAM fixture for real upstream configuration."""
    return {
        "name": "top",
        "toplevel": "top",
        "files": [{"name": "top.sv", "file_type": "systemVerilogSource"}],
        "parameters": {"NAME": {"datatype": "str", "paramtype": "vlogparam", "default": value}},
        "flow_options": {"tool": tool, **options},
    }


@pytest.mark.parametrize("value", ["MAC", 'a" b\\c', ""])
def test_actual_cocotb_target_receives_bytes_and_native_length(tmp_path, monkeypatch, value):
    from xml.etree import ElementTree

    from booley.flows.sim.backends.cocotb import _build_cocotb_env, _build_run_cmd

    (tmp_path / ".booley_project").mkdir()
    (tmp_path / ".booley_project/booley.toml").write_text("[flows.sim]\nenabled=true\n")
    byte_expression = (
        "{" + ",".join(f"NAME[{index}]" for index in range(len(value.encode()))) + "}"
        if value
        else "'0"
    )
    (tmp_path / "dut.sv").write_text(f"""`timescale 1ns/1ps
module dut #(parameter string NAME="X") (output [127:0] bytes,output [31:0] length,output ok);
assign bytes=128'({byte_expression}); assign length=NAME.len(); assign ok=(NAME=={_expected(value)});
endmodule
""")
    (tmp_path / "test_strings.py").write_text(f"""import cocotb
from cocotb.triggers import Timer
@cocotb.test()
async def exact_parameter(dut):
    await Timer(1,unit="ns")
    assert int(dut.bytes.value)=={int.from_bytes(value.encode(), "big")}
    assert int(dut.length.value)=={len(value.encode())}
    assert int(dut.ok.value)==1
""")
    _write_cocotb_core(tmp_path, value)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / ".booley_project"))
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    flow = SimulateFlow()
    flow.parse_args(["--project", str(tmp_path), "--target", "sim", "--mode", "elab-only"])
    with SimulationBuildSession(handle) as session:
        prepared = flow._prepare_elab_only_target(
            "sim", time.monotonic(), handle=handle, build_root=session.new_generation()
        )
        assert hasattr(prepared, "make_argv"), prepared
        _run(prepared.make_argv, tmp_path)
        results = prepared.build_root / "parameter-results.xml"
        environment = _build_cocotb_env(prepared.build_root, "test_strings", [], results)
        command = _build_run_cmd(
            eda_tool="verilator", build_dir=prepared.build_root, plusargs=[], vcd=False
        )
        assert command
        _run(command, prepared.build_root, environment)
        document = ElementTree.parse(results)
        cases = document.findall(".//testcase")
        assert len(cases) == 1
        assert not document.findall(".//failure") and not document.findall(".//error")


def _write_cocotb_core(tmp_path, value):
    data = {
        "name": "proof:string:cocotb:1",
        "filesets": {
            "rtl": {"files": ["dut.sv"], "file_type": "systemVerilogSource"},
            "tb": {
                "files": [{"test_strings.py": {"file_type": "user", "copyto": "test_strings.py"}}]
            },
        },
        "parameters": {"NAME": {"datatype": "str", "paramtype": "vlogparam", "default": value}},
        "targets": {
            "sim": {
                "flow": "sim",
                "flow_options": {"tool": "verilator", "cocotb_module": "test_strings"},
                "filesets": ["rtl", "tb"],
                "parameters": ["NAME"],
                "toplevel": "dut",
            }
        },
    }
    (tmp_path / "proof.core").write_text("CAPI=2:\n" + yaml.safe_dump(data))


@pytest.mark.parametrize("typed", [True, False])
def test_verilator_file_consumer_and_clean_baseline(tmp_path, typed):
    resource = tmp_path / "declared.mem"
    resource.write_text("a5\n")
    selected = tmp_path / 'data a"b\\c.mem'
    selected.symlink_to(resource.name)
    default = tmp_path / "default.mem"
    default.write_text("11\n")
    declaration = 'parameter string FILE="X"' if typed else 'parameter FILE="X"'
    (tmp_path / "top.sv").write_text(f"""module top #({declaration})();
reg [7:0] mem[0:0]; integer opened;
initial begin
 $readmemh(FILE,mem); opened=$fopen(FILE,"r");
 if(opened==0 || mem[0]!=8'ha5) $fatal(1,"file receiver mismatch");
 $fclose(opened); $display("FILE_VALUE_OK"); $finish;
end
endmodule
""")
    for variant, path in (("baseline", resource), ("packed", selected)):
        build = tmp_path / variant
        build.mkdir()
        shutil.copy(tmp_path / "top.sv", build / "top.sv")
        _verify_verilator_file(build, path, normalize=variant == "packed")


def _verify_verilator_file(tmp_path, selected, *, normalize):
    edam = {
        "name": "top",
        "toplevel": "top",
        "files": [
            {"name": "top.sv", "file_type": "systemVerilogSource"},
            {"name": str(selected), "file_type": "user"},
        ],
        "parameters": {
            "FILE": {"datatype": "file", "paramtype": "vlogparam", "default": str(selected)}
        },
        "flow_options": {"tool": "verilator", "mode": "binary"},
    }
    edam_layer.configure("sim", edam, tmp_path)
    if normalize:
        normalize_parameter_records(
            SimpleNamespace(
                build_root=tmp_path,
                configured_eda_tool="verilator",
                toplevel="top",
                parameters=edam["parameters"],
            )
        )
    _run(["make", "-C", tmp_path], tmp_path)
    assert "FILE_VALUE_OK" in _run([tmp_path / "Vtop"], tmp_path).stdout


@pytest.mark.parametrize("value,explicit_empty", [("", False), ("", True), ('a" b\\c', False)])
def test_actual_trace_resolution_preserves_owned_parameters_and_authored_flags(
    tmp_path, value, explicit_empty
):
    from booley.flows.sim.build import prepare_simulation_build
    from booley.flows.sim.trace_overlay import write_trace_overlay

    _project(tmp_path, value, explicit_empty=explicit_empty)
    path = tmp_path / "shared.core"
    data = yaml.safe_load(path.read_text().split("\n", 1)[1])
    data["targets"]["sim"]["flags"] = {"owned": True}
    reference = "NAME=" if explicit_empty else "NAME"
    data["targets"]["sim"]["parameters"] = [f"owned ? ({reference})"]
    path.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    overlay = write_trace_overlay(handle)
    try:
        resolved = prepare_simulation_build(
            handle, build_root=tmp_path / "trace-build", resolution_vlnv=overlay.vlnv
        ).resolved
        assert resolved.parameters["NAME"] == {
            "datatype": "str",
            "paramtype": "vlogparam",
            "default": value,
        }
        _run(["make", "-C", resolved.build_root], tmp_path)
        assert (
            "DUAL_FLOW_VALUE_OK" in _run([resolved.build_root / "Vtb"], resolved.build_root).stdout
        )
    finally:
        overlay.cleanup()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def _legacy_receiver_project(tmp_path, tool, value):
    declaration = 'parameter string NAME="X"' if tool == "verilator" else 'parameter NAME="X"'
    source = f"""module top #({declaration})();
initial begin
 if (NAME!={_expected(value)}) $fatal(1,"legacy value mismatch");
 $display("LEGACY_STRING_OK"); $finish;
end
endmodule
"""
    tops = "top"
    if tool == "icarus":
        source += source.replace("module top #", "module other #")
        tops = ["top", "other"]
    (tmp_path / "top.sv").write_text(source)
    options = {"exe": False} if tool == "verilator" else {"iverilog_options": ["-g2012"]}
    (tmp_path / "legacy.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump(
            {
                "name": "proof:string:legacy:1",
                "filesets": {"rtl": {"files": ["top.sv"], "file_type": "systemVerilogSource"}},
                "parameters": {
                    "NAME": {"datatype": "str", "paramtype": "vlogparam", "default": value}
                },
                "targets": {
                    "sim": {
                        "default_tool": tool,
                        "tools": {tool: options},
                        "filesets": ["rtl"],
                        "parameters": ["NAME"],
                        "toplevel": tops,
                    }
                },
            }
        )
    )


@pytest.mark.parametrize("tool", ["verilator", "icarus"])
@pytest.mark.parametrize("value", ['a" b\\c', ""])
def test_actual_legacy_default_tool_receiver(tmp_path, tool, value):
    from booley.flows.sim.build import prepare_simulation_build

    _legacy_receiver_project(tmp_path, tool, value)
    core_hash = hashlib.sha256((tmp_path / "legacy.core").read_bytes()).hexdigest()
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    resolved = prepare_simulation_build(handle, build_root=tmp_path / "build").resolved
    assert resolved.parameters["NAME"]["datatype"] == "str"
    assert resolved.parameters["NAME"]["default"] == value
    root = resolved.build_root
    suffix = ".vc" if tool == "verilator" else ".scr"
    response = root / (resolved.configuration_name + suffix)
    normalize_parameter_records(resolved)
    before = response.read_bytes()
    normalize_parameter_records(resolved)
    assert response.read_bytes() == before
    if tool == "verilator":
        _run(["verilator", "-f", response.name, "--binary", "--build", "--timing"], root)
        output = _run([root / "Vtop"], root).stdout
    else:
        assert before.count(b"+parameter+") == 2
        _run(["make", "-C", root], root)
        output = _run(["vvp", root / resolved.configuration_name], root).stdout
        assert output.count("LEGACY_STRING_OK") == 2
    assert "LEGACY_STRING_OK" in output
    assert hashlib.sha256((tmp_path / "legacy.core").read_bytes()).hexdigest() == core_hash
