"""Translate resolved Target parameters into EDA-facing argument values."""

from __future__ import annotations

from typing import Any


def vlogdefine_args(parameters: Any) -> list[str]:
    """Map ``vlogdefine`` parameters to Verilog define strings."""
    defines: list[str] = []
    for name, spec in (parameters or {}).items():
        if not isinstance(spec, dict) or spec.get("paramtype") != "vlogdefine":
            continue
        default = spec.get("default")
        if default is True:
            defines.append(str(name))
        elif default not in (False, None, ""):
            defines.append(f"{name}={default}")
    return defines


def vlogparam_args(parameters: Any) -> list[str]:
    """Map ``vlogparam`` parameters to ``NAME=VALUE`` strings."""
    assignments: list[str] = []
    for name, spec in (parameters or {}).items():
        if not isinstance(spec, dict) or spec.get("paramtype") != "vlogparam":
            continue
        default = spec.get("default")
        if default is not None:
            # EDAM preserves CAPI2 bool defaults as Python values, whose
            # spelling is not a SystemVerilog literal.
            value = "1" if default is True else "0" if default is False else str(default)
            assignments.append(f"{name}={value}")
    return assignments
