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

    def test_specialist_name_is_rejected(self):
        with pytest.raises(ValueError, match=r"mutation_tester.*not a target-aware"):
            flow_can_drive("mutation_tester", self._ref("sim", "verilator"))

    def test_retired_elab_name_is_rejected(self):
        with pytest.raises(ValueError, match=r"elab.*not a target-aware"):
            flow_can_drive("elab", self._ref("sim", "verilator"))
