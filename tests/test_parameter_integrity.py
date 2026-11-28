"""Cross-flow guards for macro-versus-top-parameter configuration mistakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.targets.parameter_integrity import (
    ParameterIntegrityError,
    enabled_vlogdefine_names,
    validate_top_parameter_intent,
)

_DEFINE = {"ENABLE_ZBB": {"paramtype": "vlogdefine", "default": True}}
_CHILD = "module child #(parameter ENABLE_ZBB = 0) (); endmodule\n"


def _resolved(
    tmp_path: Path,
    source: str,
    parameters: dict = _DEFINE,
    *,
    include: str | None = None,
    eda_tool: str | None = "verilator",
    configured_eda_tool: str | None = "verilator",
) -> ResolvedTarget:
    rtl = tmp_path / "dut.sv"
    rtl.write_text(source, encoding="utf-8")
    files = [ResolvedFile(name="dut.sv", file_type="systemVerilogSource", tags=("tb",))]
    if include is not None:
        (tmp_path / "defs.svh").write_text(include, encoding="utf-8")
        files.append(
            ResolvedFile(name="defs.svh", file_type="systemVerilogSource", is_include=True)
        )
    return ResolvedTarget(
        name="configured",
        vlnv="::demo:0",
        toplevel="dut",
        eda_tool=eda_tool,
        configured_eda_tool=configured_eda_tool,
        # Simulation tops normally live in a TB-tagged fileset. The guard must
        # inspect compiled TB HDL too, not only ResolvedTarget.rtl_files.
        files=tuple(files),
        parameters=parameters,
        build_root=tmp_path,
        edam_path=tmp_path / "demo.eda.yml",
    )


def _guarded_override(directive: str, prefix: str = "", value: str = "1") -> str:
    """``dut`` instantiates ``child`` with an ENABLE_ZBB override inside *directive*."""
    return (
        f"{prefix}{_CHILD}module dut ();\n  child #(\n`{directive}\n"
        f"    .ENABLE_ZBB({value})\n`endif\n  ) u_child();\nendmodule\n"
    )


_TOP = r"vlogdefine.*literal default 0"
_CHILD_ZERO = r"ENABLE_ZBB \(child\)"


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ("module dut #(parameter bit ENABLE_ZBB = 1'b0) (); endmodule\n", _TOP),
        # The override sits in a branch the Target's own define makes inactive.
        (_guarded_override("ifndef ENABLE_ZBB"), _CHILD_ZERO),
        (_CHILD + "module dut (); child u_child(); endmodule\n", _CHILD_ZERO),
        (_CHILD + "module dut (); child #(.ENABLE_ZBB(1'b0)) u_child(); endmodule\n", _CHILD_ZERO),
    ],
    ids=["top-literal-zero", "inactive-branch", "unoverridden-child", "zero-override"],
)
def test_enabled_define_cannot_silently_leave_a_parameter_zero(
    tmp_path: Path, source: str, match: str
):
    with pytest.raises(ParameterIntegrityError, match=match):
        validate_top_parameter_intent(_resolved(tmp_path, source), flow="sim")


@pytest.mark.parametrize(
    "source",
    [
        "module dut #(parameter ENABLE_ZBB = `ENABLE_ZBB) (); endmodule\n",
        "module dut #(\n`ifdef ENABLE_ZBB\n  parameter ENABLE_ZBB = 1\n`else\n"
        "  parameter ENABLE_ZBB = 0\n`endif\n) (); endmodule\n",
        _CHILD + "module dut (); child #(.ENABLE_ZBB(1)) u_child(); endmodule\n",
        _CHILD + "module wrapper ();\n  child #(\n`ifndef SYNTH_TEST\n`ifdef ENABLE_ZBB\n"
        "    .ENABLE_ZBB(1)\n`endif\n`endif\n  ) u_child();\nendmodule\n"
        "module dut (); wrapper u_wrapper(); endmodule\n",
        _guarded_override("ifdef VERILATOR"),
        # Ambiguous source macro state cannot block an `ifndef override.
        _guarded_override("ifndef MODE", "`define MODE\n`undef MODE\n"),
        _guarded_override("ifndef MODE", "`ifdef NEVER\n`define MODE\n`endif\n"),
        _guarded_override("ifdef MODE", "`define MODE\n"),
        _guarded_override("ifndef ENABLE_ZBB", "`undef ENABLE_ZBB\n"),
        "`ifndef ENABLE_ZBB\n" + _CHILD + "`else\n"
        "module child #(parameter ENABLE_ZBB = 1) (); endmodule\n`endif\n"
        "module dut (); child u_child(); endmodule\n",
        _CHILD + "module dut ();\n  child #(\n`ifndef ENABLE_ZBB\n    .ENABLE_ZBB(0)\n"
        "`elsif MODE\n    .ENABLE_ZBB(1)\n`endif\n  ) u_child();\nendmodule\n",
        # Unproven nested reachability does not block.
        _CHILD + 'module dut (); $display("child fake()"); endmodule\n',
        _CHILD + 'module dut (); $display("parameter ENABLE_ZBB = 0)"); endmodule\n',
        _CHILD + "module dut (); generate if (0) begin : g child u_child(); end endgenerate"
        " endmodule\n",
        _CHILD + "module dut (); if (0) child u_child(); endmodule\n",
    ],
    ids=[
        "macro-default",
        "conditional-default",
        "nested-override",
        "override-inside-unrelated-ifndef",
        "selected-tool-builtin-define",
        "define-then-undef",
        "define-in-inactive-branch",
        "source-defined-macro",
        "source-undef-of-target-macro",
        "outer-conditional-module",
        "unknown-elsif",
        "display-text-instance",
        "display-text-parameter",
        "generate-if-0",
        "if-0",
    ],
)
def test_proven_or_unproven_override_is_allowed(tmp_path: Path, source: str):
    validate_top_parameter_intent(_resolved(tmp_path, source), flow="sim")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"eda_tool": None, "configured_eda_tool": "icarus"},
        {"eda_tool": None, "configured_eda_tool": "iverilog"},
        {"parameters": {**_DEFINE, "MODE": {"paramtype": "vlogdefine", "default": "fast"}}},
        {"include": "`define MODE\n"},
    ],
    ids=["icarus", "iverilog", "string-define", "included-define"],
)
def test_preprocessor_environment_activates_the_override(tmp_path: Path, kwargs: dict):
    directive = "ifdef __ICARUS__" if "configured_eda_tool" in kwargs else "ifdef MODE"
    prefix = '`include "defs.svh"\n' if "include" in kwargs else ""
    source = _guarded_override(directive, prefix)
    validate_top_parameter_intent(_resolved(tmp_path, source, **kwargs), flow="sim")


def test_disabled_define_and_vlogparam_are_not_guard_candidates(tmp_path: Path):
    source = "module dut #(parameter ENABLE_ZBB = 0) (); endmodule\n"
    disabled = {"ENABLE_ZBB": {"paramtype": "vlogdefine", "default": False}}
    validate_top_parameter_intent(_resolved(tmp_path, source, disabled), flow="sim")
    params = {"ENABLE_ZBB": {"paramtype": "vlogparam", "default": 1}}
    validate_top_parameter_intent(_resolved(tmp_path, source, params), flow="fpga")


def test_resolved_parameter_helpers_preserve_types():
    params = {
        "FEATURE": {"paramtype": "vlogdefine", "default": True},
        "ZERO": {"paramtype": "vlogdefine", "default": 0},
        "WIDTH": {"paramtype": "vlogparam", "default": 8},
        "FLAG": {"paramtype": "vlogparam", "default": False},
    }
    assert enabled_vlogdefine_names(params) == ("FEATURE",)
