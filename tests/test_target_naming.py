"""Tests for target_naming: the ``<axis>_<subject>`` Target naming convention."""

from __future__ import annotations

import pytest

from booley.targets import target_naming
from booley.targets.target_surface import TARGET_AWARE_FLOWS


def test_every_target_aware_flow_has_exactly_one_axis():
    """A Booley Flow with no axis would make its Targets unnameable under the rule."""
    assert set(target_naming.AXIS_FOR_FLOW) == set(TARGET_AWARE_FLOWS)


@pytest.mark.parametrize(
    "name,axis",
    [
        ("sim_soc", "sim"),
        ("synth_matmul_par_b8", "synth"),
        ("fpga_soc", "fpga"),
        ("lint_style", "lint"),
        # The axis is a whole segment, not a string prefix; elab has no axis.
        ("soc_sim", None),
        ("simulate_soc", None),
        ("simsoc", None),
        ("asic", None),
        ("elab_soc", None),
    ],
)
def test_axis_of(name: str, axis: str | None):
    assert target_naming.axis_of(name) == axis


@pytest.mark.parametrize("axis", ["sim", "lint", "synth", "fpga"])
def test_bare_axis_keeps_its_axis(axis: str):
    """Flow intent and Criteria selection still read the axis off a bare name."""
    assert target_naming.axis_of(axis) == axis
    assert target_naming.fpga_intent(axis, "verilator") is (axis == "fpga")


@pytest.mark.parametrize(
    "name,eda_tool,expected",
    [
        # The fpga axis is authoritative over the resolution tool.
        ("fpga_core", "verilator", True),
        ("synth_core", "vivado", False),
        # An unprefixed vendored Target falls back to vivado.
        ("upstream_board", "vivado", True),
        ("upstream_board", "verilator", False),
    ],
)
def test_fpga_intent(name: str, eda_tool: str, expected: bool):
    assert target_naming.fpga_intent(name, eda_tool) is expected
