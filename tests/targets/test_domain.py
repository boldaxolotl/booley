"""Tests for dependency-neutral Target domain policy."""

from pathlib import Path

import pytest

from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetRef, flow_can_drive


def _ref(flow: str | None, eda_tool: str | None, name: str = "t", **kwargs) -> TargetRef:
    return TargetRef(name, "a:b:c:1.0", Path("x.core"), eda_tool, flow, **kwargs)


@pytest.mark.parametrize(
    "requested,flow,eda_tool,name,expected",
    [
        ("sim", "sim", "verilator", "t", True),
        ("lint", "sim", "verilator", "t", False),
        ("synth", "sim", "verilator", "t", False),
        ("fpga", "sim", "verilator", "t", False),
        ("sim", "sim", "icarus", "t", True),
        ("sim", "lint", "verible", "t", False),
        # A generic Flow splits on the EDA tool.
        ("synth", "generic", "yosys", "t", True),
        ("fpga", "generic", "yosys", "t", False),
        ("fpga", "generic", "vivado", "t", True),
        ("synth", "generic", "vivado", "t", False),
        # The axis in the name outranks the resolution tool.
        ("fpga", "generic", "verilator", "fpga_core_fast", True),
        ("fpga", "generic", "vivado", "synth_core_fast", False),
        # A `tools:`-style Target (flow=None) must not vanish from --for.
        ("sim", None, "iverilog", "t", True),
        ("lint", None, "iverilog", "t", False),
        # Vendor .cores may enumerate them, but Booley must not advertise support.
        ("sim", "sim", "xcelium", "t", False),
        ("sim", None, "xcelium", "t", False),
        ("sim", "sim", "vcs", "t", False),
        ("sim", None, "vcs", "t", False),
        # No accepted tool declaration.
        ("sim", None, None, "t", False),
        ("sim", "sim", None, "t", False),
        # Lint validates the authored EDA tool.
        ("lint", "lint", "verilator", "lint_style", True),
        ("lint", "lint", "verible", "lint_style", True),
        ("lint", "lint", "veriblelint", "lint_style", True),
        ("lint", None, "verilator", "lint_style", True),
        ("lint", None, "verible", "lint_style", True),
        ("lint", None, "veriblelint", "lint_style", False),
        ("lint", "lint", None, "lint_style", False),
        ("lint", "lint", "", "lint_style", False),
        ("lint", "lint", "slang", "lint_style", False),
        ("lint", "lint", "Verible", "lint_style", False),
        ("lint", "sim", "veriblelint", "lint_style", False),
        ("lint", "generic", "verible", "lint_style", False),
        # fpga intent precedes an explicit lint declaration.
        ("lint", "lint", "verilator", "fpga_top", False),
        ("lint", "lint", "verible", "fpga_top", False),
        ("lint", "lint", "veriblelint", "fpga_top", False),
        ("fpga", "lint", "verilator", "fpga_top", True),
        ("fpga", "lint", "verible", "fpga_top", True),
        ("fpga", "lint", "veriblelint", "fpga_top", True),
    ],
)
def test_flow_can_drive(requested, flow, eda_tool, name, expected):
    assert flow_can_drive(requested, _ref(flow, eda_tool, name)) is expected


@pytest.mark.parametrize("requested", ["reviewer", "elab"])
def test_non_target_aware_flow_name_is_rejected(requested: str):
    with pytest.raises(ValueError, match=rf"{requested}.*not a target-aware"):
        flow_can_drive(requested, _ref("sim", "verilator"))


@pytest.mark.parametrize("missing", [False, True])
def test_lint_missing_provenance_on_ref(missing):
    ref = _ref("lint", "verible", "lint_style", lint_flow_eda_tool_missing=missing)
    assert flow_can_drive("lint", ref) is not missing


@pytest.mark.parametrize("missing", [False, True])
def test_lint_missing_provenance_on_canonical_handle(tmp_path, missing):
    selection = "default_tool: verible" if missing else "flow_options: {tool: verible}"
    (tmp_path / "style.core").write_text(
        "CAPI=2:\nname: acme:ip:style:1.0\ntargets:\n  lint_style:\n    flow: lint\n"
        + f"    {selection}\n"
    )
    handle = TargetCatalog.build(tmp_path).select("lint_style")
    assert handle.lint_flow_eda_tool_missing is missing
    assert flow_can_drive("lint", handle) is not missing


@pytest.mark.parametrize(
    "flow,name,eda_tool,requested",
    [
        ("lint", "synth_vehicle", "yosys", "asic_synthesize"),
        ("lint", "implementation", "vivado", "fpga_impl"),
        ("lint", "fpga_top", "verible", "fpga_impl"),
        ("sim", "sim_control", "verilator", "simulate"),
    ],
)
def test_lint_provenance_does_not_change_other_flow_aliases(flow, name, eda_tool, requested):
    ref = _ref(flow, eda_tool, name, lint_flow_eda_tool_missing=True)
    assert flow_can_drive(requested, ref)
