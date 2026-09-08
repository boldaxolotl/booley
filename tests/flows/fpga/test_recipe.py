"""Normalized FPGA Target recipe identity."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from booley.flows.fpga.recipe import (
    fpga_recipe_snapshot,
    fpga_recipe_snapshot_fingerprint,
)
from booley.flows.recipe_evidence import implementation_comparison_basis, recipe_changes
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
    assert baseline["schema"] == 2
    assert baseline["ppa_profile"] == {
        "name": "balanced",
        "adapter": "vivado",
        "mapping": {
            "synthesis_strategy": "Vivado Synthesis Defaults",
            "implementation_strategy": "Vivado Implementation Defaults",
            "applied": False,
            "final_step": "route_design",
        },
    }


def test_snapshot_fingerprint_tracks_resolved_profile_mapping(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path)
    balanced = fpga_recipe_snapshot(resolved, target="fpga_core")
    compact = fpga_recipe_snapshot(
        replace(resolved, flow_options={**resolved.flow_options, "ppa_profile": "compact"}),
        target="fpga_core",
    )

    assert compact["ppa_profile"]["mapping"] == {
        "synthesis_strategy": "Flow_AreaOptimized_high",
        "implementation_strategy": "Area_Explore",
        "applied": True,
        "final_step": "route_design",
    }
    assert fpga_recipe_snapshot_fingerprint(compact) != fpga_recipe_snapshot_fingerprint(balanced)


def test_explicit_balanced_and_omitted_default_have_one_recipe_identity(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path)
    implicit = fpga_recipe_snapshot(resolved, target="fpga_core")
    explicit = fpga_recipe_snapshot(
        replace(resolved, flow_options={**resolved.flow_options, "ppa_profile": "balanced"}),
        target="fpga_core",
    )

    assert explicit == implicit
    assert fpga_recipe_snapshot_fingerprint(explicit) == fpga_recipe_snapshot_fingerprint(implicit)


def test_schema_one_snapshot_is_not_silently_equivalent_to_profile_evidence(
    tmp_path: Path,
) -> None:
    current = fpga_recipe_snapshot(_resolved(tmp_path), target="fpga_core")
    legacy = {key: value for key, value in current.items() if key != "ppa_profile"}
    legacy["schema"] = 1

    changes = recipe_changes(
        implementation_comparison_basis(legacy),
        implementation_comparison_basis(current),
    )

    assert changes == [
        {
            "path": "ppa_profile",
            "before": None,
            "after": current["ppa_profile"],
        }
    ]
