"""Normalized FPGA Target recipe identity."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from booley.flows.fpga.recipe import (
    fpga_recipe_snapshot,
    fpga_recipe_snapshot_fingerprint,
)
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget


def _resolved(root: Path) -> ResolvedTarget:
    (root / "timing.xdc").write_text(
        "create_clock -period 10 [get_ports clk]\n",
        encoding="utf-8",
    )
    return ResolvedTarget(
        name="fpga_core",
        vlnv="::core:0",
        toplevel="top",
        eda_tool="vivado",
        files=(ResolvedFile(name="timing.xdc", file_type="xdc"),),
        parameters={"WIDTH": {"paramtype": "vlogparam", "default": 8}},
        build_root=root,
        edam_path=root / "core.eda.yml",
        flow_options={"tool": "vivado", "part": "xc7a35tcpg236-1"},
    )


def test_snapshot_fingerprint_tracks_target_recipe_and_xdc(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path)
    baseline = fpga_recipe_snapshot(resolved, target="fpga_core")
    baseline_fingerprint = fpga_recipe_snapshot_fingerprint(baseline)

    changed_part = fpga_recipe_snapshot(
        replace(resolved, flow_options={**resolved.flow_options, "part": "xc7a200t"}),
        target="fpga_core",
    )
    assert fpga_recipe_snapshot_fingerprint(changed_part) != baseline_fingerprint

    (tmp_path / "timing.xdc").write_text(
        "create_clock -period 8 [get_ports clk]\n",
        encoding="utf-8",
    )
    changed_xdc = fpga_recipe_snapshot(resolved, target="fpga_core")
    assert fpga_recipe_snapshot_fingerprint(changed_xdc) != baseline_fingerprint
    assert baseline["flow"] == "fpga"
