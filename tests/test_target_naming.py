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
def test_bare_axis_keeps_its_axis_but_breaks_the_convention(axis: str):
    """Flow intent and Criteria selection still read the axis off a bare name."""
    assert target_naming.axis_of(axis) == axis
    assert target_naming.fpga_intent(axis, "verilator") is (axis == "fpga")
    assert not target_naming.is_conventional(axis)
    assert target_naming.violation(axis) == "bare axis; add a subject"


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


@pytest.mark.parametrize(
    "name,violation",
    [
        ("sim_soc", None),
        # `default` is FuseSoC plumbing; it carries its own doctor finding.
        ("default", None),
        ("soc_sim", "no axis prefix (sim/lint/synth/fpga)"),
        # A name that is not snake_case fails on shape, whatever prefix it wears.
        ("Sim_SOC", "not lowercase_snake_case"),
        ("sim-soc", "not lowercase_snake_case"),
        ("sim__soc", "not lowercase_snake_case"),
        ("sim_", "not lowercase_snake_case"),
    ],
)
def test_violation(name: str, violation: str | None):
    assert target_naming.violation(name) == violation


@pytest.mark.parametrize(
    "args,subject,expected",
    [
        (("soc_sim",), None, "sim_soc"),
        (("matmul_par_b8_synth",), None, "synth_matmul_par_b8"),
        (("style_lint",), None, "lint_style"),
        (("soc_fpga",), None, "fpga_soc"),
        (("soc_lite_lint",), None, "lint_soc_lite"),
        (("soc_xcelium_sim",), None, "sim_soc_xcelium"),
        # A bare axis needs a subject (Doctor passes the core name).
        (("synth",), None, None),
        (("sim",), "demo", "sim_demo"),
        (("lint",), "demo", "lint_demo"),
        (("synth",), "demo", "synth_demo"),
        (("fpga",), "demo", "fpga_demo"),
        (("asic", "synth"), "demo", "synth_demo"),
        # The subject is only sanitized: the suggestion is literally <axis>_<subject>.
        (("fpga",), "impl_top", "fpga_impl_top"),
        (("lint",), "Lint-Top", "lint_lint_top"),
        (("sim",), "-", None),
        # Doctor metadata is stronger evidence than a trailing word: a `flow: lint`
        # resolution vehicle named `*_synth` with explicit fpga intent means fpga.
        (("soc_synth", "fpga"), None, "fpga_soc"),
        # Better silent than a confidently wrong rename.
        (("soc",), None, None),
        # A leading legacy word is replaced, not prepended.
        (("asic_core", "synth"), None, "synth_core"),
        (("impl_top", "fpga"), None, "fpga_top"),
        # A leading word for a different axis is a real subject token there.
        (("sim_core", "synth"), None, "synth_sim_core"),
    ],
)
def test_suggest_name(args: tuple[str, ...], subject: str | None, expected: str | None):
    kwargs = {} if subject is None else {"subject": subject}
    assert target_naming.suggest_name(*args, **kwargs) == expected
