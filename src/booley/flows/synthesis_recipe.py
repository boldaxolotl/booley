"""Canonical identity for the ASIC synthesis recipe a Target selects."""

from __future__ import annotations

import argparse
import hashlib
import json
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

from .synth_ppa_config import append_ppa_args

RECIPE_FINGERPRINT_PARAM = "_recipe_fingerprint"
RECIPE_FINGERPRINT_DETAIL = "_recipe_fingerprint"
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


def synthesis_recipe_fingerprint(
    resolved: Any,
    args: argparse.Namespace,
    *,
    target: str,
) -> str:
    """Hash the normalized complete recipe and design constraints for a Target."""
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

    payload = {
        "schema": 1,
        "target": target,
        "toplevel": resolved.toplevel,
        "parameters": _jsonable(resolved.parameters),
        "recipe_args": synthesis_recipe_args(resolved.flow_options, args, target=target),
        "constraints": constraints,
        "default_clock_ps": getattr(args, "default_clock", None),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """Convert EDAM/YAML values into a deterministic JSON-safe structure."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
