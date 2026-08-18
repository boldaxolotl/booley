"""Cross-flow guards for macro-versus-top-parameter configuration mistakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.targets.parameter_integrity import (
    ParameterIntegrityError,
    enabled_vlogdefine_names,
    validate_top_parameter_intent,
    vlogparam_values,
)


def _resolved(
    tmp_path: Path,
    source: str,
    parameters: dict,
    *,
    include: str | None = None,
) -> ResolvedTarget:
    rtl = tmp_path / "dut.sv"
    rtl.write_text(source, encoding="utf-8")
    files = [
        ResolvedFile(
            name="dut.sv",
            file_type="systemVerilogSource",
            tags=("tb",),
        )
    ]
    if include is not None:
        (tmp_path / "defs.svh").write_text(include, encoding="utf-8")
        files.append(
            ResolvedFile(
                name="defs.svh",
                file_type="systemVerilogSource",
                is_include=True,
            )
        )
    return ResolvedTarget(
        name="configured",
        vlnv="::demo:0",
        toplevel="dut",
        eda_tool="verilator",
        # Simulation tops normally live in a TB-tagged fileset. The guard must
        # inspect compiled TB HDL too, not only ResolvedTarget.rtl_files.
        files=tuple(files),
        parameters=parameters,
        build_root=tmp_path,
        edam_path=tmp_path / "demo.eda.yml",
    )


def _define(value=True) -> dict:
    return {"ENABLE_ZBB": {"paramtype": "vlogdefine", "default": value}}


def test_enabled_define_cannot_silently_leave_top_parameter_zero(tmp_path: Path):
    resolved = _resolved(
        tmp_path,
        "module dut #(parameter bit ENABLE_ZBB = 1'b0) (); endmodule\n",
        _define(),
    )

    with pytest.raises(ParameterIntegrityError, match=r"vlogdefine.*literal default 0"):
        validate_top_parameter_intent(resolved, flow="sim")


@pytest.mark.parametrize(
    "source",
    [
        "module dut #(parameter ENABLE_ZBB = `ENABLE_ZBB) (); endmodule\n",
        """\
module dut #(
`ifdef ENABLE_ZBB
  parameter ENABLE_ZBB = 1
`else
  parameter ENABLE_ZBB = 0
`endif
) (); endmodule
""",
    ],
)
def test_macro_driven_top_parameter_is_allowed(tmp_path: Path, source: str):
    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_explicit_nested_parameter_override_is_allowed(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut (); child #(.ENABLE_ZBB(1)) u_child(); endmodule
"""
    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_macro_override_inside_unrelated_ifndef_is_allowed(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module wrapper ();
  child #(
`ifndef SYNTH_TEST
`ifdef ENABLE_ZBB
    .ENABLE_ZBB(1)
`endif
`endif
  ) u_child();
endmodule
module dut (); wrapper u_wrapper(); endmodule
"""

    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_override_in_known_inactive_target_branch_does_not_hide_mismatch(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut ();
  child #(
`ifndef ENABLE_ZBB
    .ENABLE_ZBB(1)
`endif
  ) u_child();
endmodule
"""

    with pytest.raises(ParameterIntegrityError, match=r"ENABLE_ZBB \(child\)"):
        validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_selected_tool_builtin_define_is_active(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut ();
  child #(
`ifdef VERILATOR
    .ENABLE_ZBB(1)
`endif
  ) u_child();
endmodule
"""

    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_string_valued_define_is_present_in_preprocessor_environment(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut ();
  child #(
`ifdef MODE
    .ENABLE_ZBB(1)
`endif
  ) u_child();
endmodule
"""
    params = {
        **_define(),
        "MODE": {"paramtype": "vlogdefine", "default": "fast"},
    }

    validate_top_parameter_intent(_resolved(tmp_path, source, params), flow="sim")


@pytest.mark.parametrize("include", [None, "`define MODE\n"])
def test_source_defined_macro_is_present_in_preprocessor_environment(
    tmp_path: Path,
    include: str | None,
):
    prefix = "`define MODE\n" if include is None else '`include "defs.svh"\n'
    source = (
        prefix
        + """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut ();
  child #(
`ifdef MODE
    .ENABLE_ZBB(1)
`endif
  ) u_child();
endmodule
"""
    )

    validate_top_parameter_intent(
        _resolved(tmp_path, source, _define(), include=include),
        flow="sim",
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "`define MODE\n`undef MODE\n",
        "`ifdef NEVER\n`define MODE\n`endif\n",
    ],
)
def test_ambiguous_source_macro_state_cannot_block_ifndef_override(
    tmp_path: Path,
    prefix: str,
):
    source = (
        prefix
        + """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut ();
  child #(
`ifndef MODE
    .ENABLE_ZBB(1)
`endif
  ) u_child();
endmodule
"""
    )

    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_source_undef_of_target_macro_cannot_force_false_branch(tmp_path: Path):
    source = """\
`undef ENABLE_ZBB
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut ();
  child #(
`ifndef ENABLE_ZBB
    .ENABLE_ZBB(1)
`endif
  ) u_child();
endmodule
"""

    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_outer_conditional_selects_effective_module_definition(tmp_path: Path):
    source = """\
`ifndef ENABLE_ZBB
module child #(parameter ENABLE_ZBB = 0) (); endmodule
`else
module child #(parameter ENABLE_ZBB = 1) (); endmodule
`endif
module dut (); child u_child(); endmodule
"""

    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_unknown_elsif_override_is_marked_unproven(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut ();
  child #(
`ifndef ENABLE_ZBB
    .ENABLE_ZBB(0)
`elsif MODE
    .ENABLE_ZBB(1)
`endif
  ) u_child();
endmodule
"""

    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_unoverridden_parameter_below_simulation_top_is_rejected(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut (); child u_child(); endmodule
"""

    with pytest.raises(ParameterIntegrityError, match=r"ENABLE_ZBB \(child\)"):
        validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_explicit_zero_override_below_simulation_top_is_rejected(tmp_path: Path):
    source = """\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut (); child #(.ENABLE_ZBB(1'b0)) u_child(); endmodule
"""

    with pytest.raises(ParameterIntegrityError, match=r"ENABLE_ZBB \(child\)"):
        validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


@pytest.mark.parametrize(
    "top_body",
    [
        '$display("child fake()");',
        '$display("parameter ENABLE_ZBB = 0)");',
        "generate if (0) begin : g child u_child(); end endgenerate",
        "if (0) child u_child();",
    ],
)
def test_unproven_nested_reachability_does_not_block(tmp_path: Path, top_body: str):
    source = f"""\
module child #(parameter ENABLE_ZBB = 0) (); endmodule
module dut (); {top_body} endmodule
"""

    validate_top_parameter_intent(_resolved(tmp_path, source, _define()), flow="sim")


def test_disabled_define_and_vlogparam_are_not_guard_candidates(tmp_path: Path):
    source = "module dut #(parameter ENABLE_ZBB = 0) (); endmodule\n"
    validate_top_parameter_intent(_resolved(tmp_path, source, _define(False)), flow="sim")
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
    assert vlogparam_values(params) == {"WIDTH": 8, "FLAG": False}
