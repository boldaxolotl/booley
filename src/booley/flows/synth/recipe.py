"""Canonical identity for the ASIC synthesis recipe a Target selects."""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_bool, require_opt_str
from booley.yosys.syn_core import (
    DEFAULT_FRONTEND,
    TIMING_ENGINE_CHOICES,
    resolve_frontend,
    resolve_slang_options,
)

from ..recipe_evidence import (
    BASELINE_RECIPE_FINGERPRINT_DETAIL,
    BASELINE_RECIPE_SNAPSHOT_DETAIL,
    BASELINE_REF_DETAIL,
    BASELINE_REF_PARAM,
    RECIPE_FINGERPRINT_DETAIL,
    RECIPE_FINGERPRINT_PARAM,
    RECIPE_SNAPSHOT_DETAIL,
    RECIPE_SNAPSHOT_PARAM,
    jsonable,
    recipe_changes,
    recipe_snapshot_fingerprint,
)
from .ppa_config import append_ppa_args

__all__ = [
    "BASELINE_RECIPE_FINGERPRINT_DETAIL",
    "BASELINE_RECIPE_SNAPSHOT_DETAIL",
    "BASELINE_REF_DETAIL",
    "BASELINE_REF_PARAM",
    "RECIPE_FINGERPRINT_DETAIL",
    "RECIPE_FINGERPRINT_PARAM",
    "RECIPE_SNAPSHOT_DETAIL",
    "RECIPE_SNAPSHOT_PARAM",
    "default_recipe_args",
    "synthesis_recipe_args",
    "synthesis_recipe_changes",
    "synthesis_recipe_fingerprint",
    "synthesis_recipe_snapshot",
    "synthesis_recipe_snapshot_fingerprint",
]

_DEFAULT_TIMING_ENGINE = "openroad"


def default_recipe_args() -> argparse.Namespace:
    """Return the no-CLI-override namespace used when ticket intake freezes intent."""
    names = (
        "flatten",
        "ppa_profile",
        "abc_recipe",
        "abc_script",
        "generic_abc_before_mapping",
        "abc_delay_ps",
        "utilization_pct",
        "placement_density",
        "repair_setup",
        "repair_hold",
        "gate_cloning",
        "setup_margin_ns",
        "repair_tns_percent",
        "frontend",
        "default_clock",
    )
    return argparse.Namespace(**dict.fromkeys(names))


def synthesis_recipe_args(
    flow_options: Mapping[str, Any],
    args: argparse.Namespace,
    *,
    target: str,
) -> list[str]:
    """Return normalized effective recipe argv for one resolved Target."""
    recipe = dict(flow_options)
    cli_flatten = getattr(args, "flatten", None)
    flatten = (
        bool(cli_flatten)
        if cli_flatten is not None
        else require_bool(
            recipe,
            "flatten",
            default=True,
            field=f"Target {target!r} flow_options.flatten",
        )
    )
    out = ["--flatten" if flatten else "--no-flatten"]
    append_ppa_args(
        out,
        recipe,
        args,
        field_prefix=f"Target {target!r} flow_options",
    )

    timing_engine = (
        require_opt_str(
            recipe,
            "timing_engine",
            field=f"Target {target!r} flow_options.timing_engine",
        )
        or _DEFAULT_TIMING_ENGINE
    )
    if timing_engine not in TIMING_ENGINE_CHOICES:
        raise BoundaryError(
            f"Target {target!r} flow_options.timing_engine must be one of "
            f"{', '.join(TIMING_ENGINE_CHOICES)}; got {timing_engine!r}"
        )
    out.extend(["--timing-engine", timing_engine])

    frontend = (
        resolve_frontend(
            recipe,
            override=getattr(args, "frontend", None),
            field=f"Target {target!r} flow_options.frontend",
        )
        or DEFAULT_FRONTEND
    )
    out.extend(["--frontend", frontend])
    for option in resolve_slang_options(
        recipe,
        field=f"Target {target!r} flow_options.slang_options",
    ):
        out.append(f"--slang-option={option}")
    return out


def synthesis_recipe_snapshot(
    resolved: Any,
    args: argparse.Namespace,
    *,
    target: str,
) -> dict[str, Any]:
    """Return the normalized recipe and design constraints for a Target."""
    constraints = []
    for sdc_file in resolved.sdc_files:
        path = sdc_file.absolute(resolved.build_root)
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            # Target resolution normally stages every file. Keeping the stable
            # identity when a synthetic/test resolver omits the staged bytes
            # still produces a deterministic fingerprint; the real synth path
            # will report the missing constraint as an infrastructure error.
            digest = None
        constraints.append({"name": sdc_file.name, "sha256": digest})

    return {
        "schema": 1,
        "target": target,
        "vlnv": resolved.vlnv,
        "toplevel": resolved.toplevel,
        "parameters": jsonable(resolved.parameters),
        "recipe_args": synthesis_recipe_args(resolved.flow_options, args, target=target),
        "constraints": constraints,
        "default_clock_ps": getattr(args, "default_clock", None),
    }


def synthesis_recipe_snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Hash one normalized synthesis-recipe snapshot."""
    return recipe_snapshot_fingerprint(snapshot)


def synthesis_recipe_fingerprint(
    resolved: Any,
    args: argparse.Namespace,
    *,
    target: str,
) -> str:
    """Hash the normalized complete recipe and design constraints for a Target."""
    snapshot = synthesis_recipe_snapshot(resolved, args, target=target)
    return synthesis_recipe_snapshot_fingerprint(snapshot)


def synthesis_recipe_changes(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return deterministic leaf-level changes between two recipe snapshots."""
    return recipe_changes(baseline, current)
