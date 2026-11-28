"""Normalized FPGA Target recipe identity."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from booley.evidence.recipe import recipe_changes
from booley.flows.fpga.recipe import (
    fpga_recipe_snapshot,
    fpga_recipe_snapshot_fingerprint,
)
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget


def _resolved(root: Path) -> ResolvedTarget:
    (root / "timing.xdc").write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
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


def _with_options(resolved: ResolvedTarget, **options: object) -> ResolvedTarget:
    return replace(resolved, flow_options={**resolved.flow_options, **options})


@pytest.mark.parametrize(
    ("change", "same"),
    [
        ("part", False),
        ("xdc", False),
        ("compact", False),
        # An explicit `balanced` is the omitted default: one recipe identity.
        ("balanced", True),
    ],
)
def test_snapshot_fingerprint_tracks_the_target_recipe(
    tmp_path: Path, change: str, same: bool
) -> None:
    resolved = _resolved(tmp_path)
    baseline = fpga_recipe_snapshot(resolved, target="fpga_core")
    if change == "part":
        resolved = _with_options(resolved, part="xc7a200t")
    elif change == "xdc":
        (tmp_path / "timing.xdc").write_text("create_clock -period 8 [get_ports clk]\n")
    else:
        resolved = _with_options(resolved, ppa_profile=change)

    changed = fpga_recipe_snapshot(resolved, target="fpga_core")

    assert (changed == baseline) is same
    assert (
        fpga_recipe_snapshot_fingerprint(changed) == fpga_recipe_snapshot_fingerprint(baseline)
    ) is same


def test_compact_profile_records_its_vivado_mapping(tmp_path: Path) -> None:
    resolved = _with_options(_resolved(tmp_path), ppa_profile="compact")

    assert fpga_recipe_snapshot(resolved, target="fpga_core")["ppa_profile"]["mapping"] == {
        "synthesis_strategy": "Flow_AreaOptimized_high",
        "implementation_strategy": "Area_Explore",
        "applied": True,
        "final_step": "route_design",
    }


def test_recipe_identity_ignores_isolated_checkout_path(tmp_path: Path) -> None:
    roots = (tmp_path / "baseline", tmp_path / "candidate")
    snapshots = []
    for root in roots:
        root.mkdir()
        constraint = root / "timing.xdc"
        constraint.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
        files = (ResolvedFile(name=str(constraint), file_type="xdc", core="::core:0"),)
        resolved = replace(_resolved(root), files=files)
        snapshots.append(fpga_recipe_snapshot(resolved, target="fpga_core"))

    assert snapshots[0] == snapshots[1]
    assert snapshots[0]["constraints"] == [
        {
            "core": "::core:0",
            "sha256": hashlib.sha256((roots[0] / "timing.xdc").read_bytes()).hexdigest(),
        }
    ]


def test_schema_one_snapshot_is_not_silently_equivalent_to_profile_evidence(
    tmp_path: Path,
) -> None:
    current = fpga_recipe_snapshot(_resolved(tmp_path), target="fpga_core")
    legacy = {key: value for key, value in current.items() if key != "ppa_profile"}
    legacy["schema"] = 1

    changes = recipe_changes(legacy, current)

    assert changes == [
        {
            "path": "ppa_profile",
            "before": None,
            "after": current["ppa_profile"],
        },
        {"path": "schema", "before": 1, "after": current["schema"]},
    ]
