"""Translate resolved Target parameters into EDA-facing argument values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ParameterValue:
    """Declared datatype and unchanged receiver value of a Target parameter."""

    datatype: str
    value: str

    def __str__(self) -> str:
        return self.value

    @property
    def is_string(self) -> bool:
        return self.datatype in ("str", "file")


def vlogparam_specs(parameters: Any) -> dict[str, ParameterValue]:
    """Retain declared types, using the resolved CAPI2 value contract."""
    values = {}
    for name, spec in (parameters or {}).items():
        if not isinstance(spec, Mapping) or spec.get("paramtype") != "vlogparam":
            continue
        default = spec.get("default")
        if default is None:
            continue
        datatype = str(spec.get("datatype", ""))
        value = (
            str(default)
            if datatype in ("str", "file")
            else ("1" if default is True else "0" if default is False else str(default))
        )
        values[str(name)] = ParameterValue(datatype, value)
    return values


def packed_string_constant(value: str) -> str:
    """Encode exact UTF-8 bytes and the canonical quoted empty constant."""
    raw = value.encode()
    return f"{8 * len(raw)}'h{raw.hex()}" if raw else '""'


def sv_byte_string_literal(value: str) -> str:
    """Encode UTF-8 bytes with octal escapes for whitespace-sensitive receivers."""
    return '"' + "".join(f"\\{byte:03o}" for byte in value.encode()) + '"'
