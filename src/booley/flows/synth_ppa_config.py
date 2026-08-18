"""Boundary handling for generic synthesis profiles and backend overrides."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    require_bool,
    require_dict,
    require_finite_number,
    require_opt_str,
)
from booley.targets.synthesis_profiles import (
    DEFAULT_PPA_PROFILE,
    PPA_PROFILE_CHOICES,
    validate_ppa_profile,
)
from booley.yosys.ppa import validate_abc_recipe, validate_abc_script

_YOSYS_KEYS = {
    "abc_recipe",
    "abc_script",
    "generic_abc_before_mapping",
    "abc_delay_ps",
}
_OPENROAD_KEYS = {
    "utilization_pct",
    "placement_density",
    "repair_setup",
    "repair_hold",
    "gate_cloning",
    "setup_margin_ns",
    "repair_tns_percent",
}


def add_ppa_arguments(parser: argparse.ArgumentParser) -> None:
    """Add generic profile and backend-expert per-call overrides."""
    parser.add_argument(
        "--ppa-profile",
        choices=PPA_PROFILE_CHOICES,
        default=None,
        help="Override the project PPA profile for this call",
    )
    parser.add_argument(
        "--abc-recipe",
        choices=("default", "balanced", "fast"),
        default=None,
        help="Expert Yosys override for the profile's ABC recipe",
    )
    parser.add_argument("--abc-script", default=None, help="Expert raw Yosys ABC +script")
    _add_bool_pair(parser, "generic-abc-before-mapping")
    parser.add_argument("--abc-delay-ps", type=int, default=None, help="Expert ABC delay target")
    parser.add_argument(
        "--utilization-pct", type=float, default=None, help="Expert OpenROAD floorplan override"
    )
    parser.add_argument(
        "--placement-density",
        type=float,
        default=None,
        help="Expert OpenROAD global-placement density override",
    )
    _add_bool_pair(parser, "repair-setup")
    _add_bool_pair(parser, "repair-hold")
    _add_bool_pair(parser, "gate-cloning")
    parser.add_argument(
        "--setup-margin-ns", type=float, default=None, help="Expert OpenROAD setup margin"
    )
    parser.add_argument(
        "--repair-tns-percent",
        type=float,
        default=None,
        help="Expert OpenROAD violating-endpoint repair percentage",
    )


def _add_bool_pair(parser: argparse.ArgumentParser, option: str) -> None:
    """Add tri-state ``--foo``/``--no-foo`` arguments."""
    dest = option.replace("-", "_")
    parser.add_argument(f"--{option}", dest=dest, action="store_true", default=None)
    parser.add_argument(f"--no-{option}", dest=dest, action="store_false")


def append_ppa_args(
    cmd: list[str],
    recipe: Mapping[str, Any],
    args: argparse.Namespace,
    *,
    field_prefix: str = "Target flow_options",
) -> None:
    """Append resolved profile and config/call overrides to backend argv.

    A per-call profile selects a clean built-in profile, deliberately skipping
    project backend overrides. Per-call expert flags still apply afterward.
    """
    cli_profile = getattr(args, "ppa_profile", None)
    configured = recipe.get("ppa_profile", DEFAULT_PPA_PROFILE)
    profile = validate_ppa_profile(
        cli_profile or configured,
        field=f"{field_prefix}.ppa_profile",
    )
    cmd.extend(["--ppa-profile", profile])
    if cli_profile is None:
        _append_yosys_config(
            cmd,
            _subtable(recipe, "yosys", field_prefix=field_prefix),
            args,
            section=f"{field_prefix}.yosys",
        )
        _append_openroad_config(
            cmd,
            _subtable(recipe, "openroad", field_prefix=field_prefix),
            section=f"{field_prefix}.openroad",
        )
    _append_cli_overrides(cmd, args)


def _subtable(
    cfg: Mapping[str, Any], key: str, *, field_prefix: str = "Target flow_options"
) -> dict[str, Any]:
    """Read an optional backend table strictly."""
    if key not in cfg:
        return {}
    return require_dict(cfg[key], field=f"{field_prefix}.{key}")


def _append_yosys_config(
    cmd: list[str],
    cfg: Mapping[str, Any],
    args: argparse.Namespace,
    *,
    section: str = "Target flow_options.yosys",
) -> None:
    """Validate and append Yosys expert overrides."""
    _reject_unknown_keys(cfg, _YOSYS_KEYS, section)
    recipe = require_opt_str(cfg, "abc_recipe", field=f"{section}.abc_recipe")
    script = require_opt_str(cfg, "abc_script", field=f"{section}.abc_script")
    if recipe is not None and script is not None:
        raise BoundaryError(f"{section} cannot set both abc_recipe and abc_script")
    # Either per-call mapping control replaces the configured mapping control
    # as one semantic setting. Emitting both recipe and script would make the
    # downstream resolver reject the invocation instead of honoring CLI-last
    # precedence.
    if (
        getattr(args, "abc_recipe", None) is not None
        or getattr(args, "abc_script", None) is not None
    ):
        recipe = None
        script = None
    if recipe is not None:
        cmd.extend(["--abc-recipe", validate_abc_recipe(recipe)])
    if script is not None:
        cmd.extend(["--abc-script", validate_abc_script(script)])
    _append_bool_config(cmd, cfg, "generic_abc_before_mapping")
    _append_positive_int_config(cmd, cfg, "abc_delay_ps")


def _append_openroad_config(
    cmd: list[str],
    cfg: Mapping[str, Any],
    *,
    section: str = "Target flow_options.openroad",
) -> None:
    """Validate and append OpenROAD expert overrides."""
    _reject_unknown_keys(cfg, _OPENROAD_KEYS, section)
    _append_number_config(cmd, cfg, "utilization_pct", positive=True)
    _append_number_config(cmd, cfg, "placement_density", positive=True)
    _append_bool_config(cmd, cfg, "repair_setup")
    _append_bool_config(cmd, cfg, "repair_hold")
    _append_bool_config(cmd, cfg, "gate_cloning")
    _append_number_config(cmd, cfg, "setup_margin_ns")
    _append_number_config(cmd, cfg, "repair_tns_percent")


def _reject_unknown_keys(cfg: Mapping[str, Any], allowed: set[str], section: str) -> None:
    """Reject misspelled expert settings instead of silently ignoring them."""
    unknown = sorted(set(cfg) - allowed)
    if unknown:
        choices = ", ".join(sorted(allowed))
        raise BoundaryError(
            f"{section} unknown setting(s): {', '.join(unknown)}; valid settings: {choices}"
        )


def _append_bool_config(cmd: list[str], cfg: Mapping[str, Any], key: str) -> None:
    """Append a strict boolean backend config as a positive/negative flag."""
    if key not in cfg:
        return
    value = require_bool(cfg, key, field=key)
    option = key.replace("_", "-")
    cmd.append(f"--{option}" if value else f"--no-{option}")


def _append_number_config(
    cmd: list[str], cfg: Mapping[str, Any], key: str, *, positive: bool = False
) -> None:
    """Append one strict finite backend numeric config."""
    if key not in cfg:
        return
    value = require_finite_number(cfg[key], field=key)
    if positive and value <= 0:
        raise BoundaryError(f"{key} must be greater than zero")
    cmd.extend([f"--{key.replace('_', '-')}", f"{value:g}"])


def _append_positive_int_config(cmd: list[str], cfg: Mapping[str, Any], key: str) -> None:
    """Append one strictly positive integer backend config."""
    if key not in cfg:
        return
    value = cfg[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BoundaryError(f"{key} must be a positive integer, got {value!r}")
    cmd.extend([f"--{key.replace('_', '-')}", str(value)])


def _append_cli_overrides(cmd: list[str], args: argparse.Namespace) -> None:
    """Append explicit per-call expert overrides after the selected profile."""
    for name in ("abc_recipe", "abc_script", "abc_delay_ps"):
        _append_cli_value(cmd, args, name)
    for name in (
        "generic_abc_before_mapping",
        "repair_setup",
        "repair_hold",
        "gate_cloning",
    ):
        _append_cli_bool(cmd, args, name)
    for name in (
        "utilization_pct",
        "placement_density",
        "setup_margin_ns",
        "repair_tns_percent",
    ):
        _append_cli_value(cmd, args, name)


def _append_cli_value(cmd: list[str], args: argparse.Namespace, name: str) -> None:
    value = getattr(args, name, None)
    if value is not None:
        cmd.extend([f"--{name.replace('_', '-')}", str(value)])


def _append_cli_bool(cmd: list[str], args: argparse.Namespace, name: str) -> None:
    value = getattr(args, name, None)
    if value is not None:
        option = name.replace("_", "-")
        cmd.append(f"--{option}" if value else f"--no-{option}")
