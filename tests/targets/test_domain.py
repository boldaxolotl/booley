"""Tests for dependency-neutral Target domain policy."""

from pathlib import Path

import pytest

from booley.targets.domain import TargetRef, flow_can_drive

# ---------------------------------------------------------------------------
# flow_can_drive
# ---------------------------------------------------------------------------


class TestFlowCanDrive:
    def _ref(
        self,
        flow: str | None,
        eda_tool: str | None,
        *,
        name: str = "t",
    ) -> TargetRef:
        return TargetRef(
            name=name,
            vlnv="a:b:c:1.0",
            core_file=Path("x.core"),
            eda_tool=eda_tool,
            flow=flow,
        )

    def test_sim_flow_drives_sim_targets(self):
        ref = self._ref("sim", "verilator")
        assert flow_can_drive("sim", ref)
        assert not flow_can_drive("lint", ref)
        assert not flow_can_drive("synth", ref)
        assert not flow_can_drive("fpga", ref)

    def test_sim_flow_drives_canonical_icarus_target(self):
        ref = self._ref("sim", "icarus")
        assert flow_can_drive("sim", ref)

    def test_lint_flow_drives_lint_only(self):
        ref = self._ref("lint", "verible")
        assert flow_can_drive("lint", ref)
        assert not flow_can_drive("sim", ref)

    def test_generic_flow_splits_on_eda_tool(self):
        yosys = self._ref("generic", "yosys")
        vivado = self._ref("generic", "vivado")
        assert flow_can_drive("synth", yosys)
        assert not flow_can_drive("fpga", yosys)
        assert flow_can_drive("fpga", vivado)
        assert not flow_can_drive("synth", vivado)

    def test_fpga_axis_ignores_resolution_tool(self):
        ref = self._ref("generic", "verilator", name="fpga_core_fast")
        assert flow_can_drive("fpga", ref)

    def test_non_fpga_axis_overrides_vivado_fallback(self):
        ref = self._ref("generic", "vivado", name="synth_core_fast")
        assert not flow_can_drive("fpga", ref)

    def test_legacy_flowless_target_falls_back_to_eda_tool_family(self):
        """A `tools:`-style Target (flow=None) must not vanish from --for."""
        legacy_sim = self._ref(None, "iverilog")
        assert flow_can_drive("sim", legacy_sim)
        assert not flow_can_drive("lint", legacy_sim)

    @pytest.mark.parametrize("eda_tool", ["xcelium", "vcs"])
    def test_unsupported_commercial_simulators_are_not_drivable(self, eda_tool: str):
        """Vendor .cores may enumerate them, but Booley must not advertise support."""
        for declared_flow in ("sim", None):
            ref = self._ref(declared_flow, eda_tool)
            assert not flow_can_drive("sim", ref)

    @pytest.mark.parametrize("flow", (None, "sim"))
    def test_sim_target_without_an_accepted_tool_declaration_is_not_drivable(
        self, flow: str | None
    ) -> None:
        assert not flow_can_drive("sim", self._ref(flow, None))

    def test_specialist_name_is_rejected(self):
        with pytest.raises(ValueError, match=r"mutation_tester.*not a target-aware"):
            flow_can_drive("mutation_tester", self._ref("sim", "verilator"))

    def test_retired_elab_name_is_rejected(self):
        with pytest.raises(ValueError, match=r"elab.*not a target-aware"):
            flow_can_drive("elab", self._ref("sim", "verilator"))


@pytest.mark.parametrize(
    "flow,eda_tool,expected",
    [
        ("lint", "verilator", True),
        ("lint", "verible", True),
        ("lint", "veriblelint", True),
        (None, "verilator", True),
        (None, "verible", True),
        (None, "veriblelint", False),
        ("lint", None, False),
        ("lint", "", False),
        ("lint", "slang", False),
        ("lint", "Verible", False),
        ("sim", "veriblelint", False),
        ("generic", "verible", False),
    ],
)
def test_lint_drivability_validates_authored_eda_tool(flow, eda_tool, expected):
    target = TargetRef("lint_style", "acme:ip:top:1.0", Path("top.core"), eda_tool, flow)
    assert flow_can_drive("lint", target) is expected


@pytest.mark.parametrize("eda_tool", ["verilator", "verible", "veriblelint"])
def test_fpga_intent_precedes_explicit_lint(eda_tool):
    target = TargetRef("fpga_top", "acme:ip:top:1.0", Path("top.core"), eda_tool, "lint")
    assert not flow_can_drive("lint", target)
    assert flow_can_drive("fpga", target)


@pytest.mark.parametrize("missing", [False, True])
def test_lint_missing_provenance_on_ref(missing):
    ref = TargetRef(
        "lint_style",
        "acme:ip:top:1.0",
        Path("top.core"),
        "verible",
        "lint",
        lint_flow_eda_tool_missing=missing,
    )
    assert flow_can_drive("lint", ref) is not missing


def test_lint_manual_provenance_defaults_are_compatible():
    ref = TargetRef("lint_style", "acme:ip:top:1.0", Path("top.core"), "verible", "lint")
    assert flow_can_drive("lint", ref)


@pytest.mark.parametrize("missing", [False, True])
def test_lint_missing_provenance_on_canonical_handle(tmp_path, missing):
    from booley.targets.catalog import TargetCatalog

    selection = "default_tool: verible" if missing else "flow_options: {tool: verible}"
    (tmp_path / "style.core").write_text(
        "CAPI=2:\nname: acme:ip:style:1.0\ntargets:\n  lint_style:\n    flow: lint\n"
        + f"    {selection}\n"
    )
    handle = TargetCatalog.build(tmp_path).select("lint_style")
    assert handle.lint_flow_eda_tool_missing is missing
    assert flow_can_drive("lint", handle) is not missing


def test_lint_manual_handle_provenance_default(tmp_path):
    from tests.target_test_support import make_target_handle

    handle = make_target_handle(tmp_path, "lint_style", flow="lint", eda_tool="verible")
    assert handle.lint_flow_eda_tool_missing is False
    assert flow_can_drive("lint", handle)


@pytest.mark.parametrize(
    "flow,name,eda_tool,requested",
    [
        ("lint", "synth_vehicle", "yosys", "asic_synthesize"),
        ("lint", "fpga_top", "verible", "fpga_impl"),
        ("sim", "sim_control", "verilator", "simulate"),
    ],
)
def test_lint_provenance_does_not_change_other_flow_aliases(flow, name, eda_tool, requested):
    ref = TargetRef(
        name, "acme:ip:top:1.0", Path("top.core"), eda_tool, flow, lint_flow_eda_tool_missing=True
    )
    assert flow_can_drive(requested, ref)
