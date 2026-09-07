"""Canonical identity for the FPGA implementation recipe a Target selects."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..recipe_evidence import jsonable, recipe_snapshot_fingerprint
from .profiles import VivadoProfile, resolve_fpga_profile


def fpga_recipe_snapshot(
    resolved: Any,
    *,
    target: str,
    profile: VivadoProfile | None = None,
) -> dict[str, Any]:
    """Return normalized FPGA implementation intent and design constraints."""
    constraints = []
    for xdc_file in resolved.xdc_files:
        path = xdc_file.absolute(resolved.build_root)
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            digest = None
        constraints.append({"name": xdc_file.name, "sha256": digest})

    selected_profile = profile or resolve_fpga_profile(resolved.flow_options, target=target)
    flow_options = dict(resolved.flow_options)
    # Store the effective profile exactly once so omitted-default and explicit
    # ``balanced`` Targets have the same semantic recipe identity.
    flow_options.pop("ppa_profile", None)
    return {
        "schema": 2,
        "flow": "fpga",
        "target": target,
        "vlnv": resolved.vlnv,
        "toplevel": resolved.toplevel,
        "eda_tool": resolved.eda_tool,
        "flow_options": jsonable(flow_options),
        "ppa_profile": selected_profile.as_dict(),
        "parameters": jsonable(resolved.parameters),
        "constraints": constraints,
    }


def fpga_recipe_snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Hash one normalized FPGA-recipe snapshot."""
    return recipe_snapshot_fingerprint(snapshot)
