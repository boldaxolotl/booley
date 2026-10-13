"""Normalize generated simulator records before build identity is captured."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from booley.flows.target_parameters import (
    ParameterValue,
    packed_string_constant,
    sv_byte_string_literal,
    vlogparam_specs,
)


def _record_pairs(
    tool: str, name: str, parameter: ParameterValue, top: str
) -> list[tuple[str, str]]:
    raw = parameter.value
    if tool == "verilator":
        prefix = f"-G{name}="
        original = prefix + '\\"' + raw + '\\"'
        # Whole-token quotes protect the apostrophe for the existing reuse parser.
        normalized = f'"{prefix}{packed_string_constant(raw)}"' if raw else original
        return [(original, normalized)]
    return [
        (
            f'+parameter+{module}.{name}="{raw}"',
            f"+parameter+{module}.{name}=" + sv_byte_string_literal(raw),
        )
        for module in top.split()
    ]


def _read_records(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def _record_matches(text: str, record: str) -> list[re.Match[str]]:
    return list(re.finditer(r"(?m)^" + re.escape(record) + r"(?=\r?$)", text))


def _normalize_record(
    contents: dict[Path, str], original: str, normalized: str, description: str
) -> None:
    matches = []
    for path, text in contents.items():
        for match in _record_matches(text, original):
            matches.append((path, match, False))
        if original != normalized:
            for match in _record_matches(text, normalized):
                matches.append((path, match, True))
    if len(matches) != 1:
        raise ValueError(f"{description}: missing or ambiguous generated string parameter record")
    path, match, ready = matches[0]
    if not ready:
        text = contents[path]
        contents[path] = text[: match.start()] + normalized + text[match.end() :]


def normalize_parameter_records(resolved: Any) -> None:
    """Rewrite only declared str/file assignments in confined generated files."""
    tool = "icarus" if resolved.configured_eda_tool == "iverilog" else resolved.configured_eda_tool
    if tool not in ("verilator", "icarus"):
        return
    parameters = {
        name: value
        for name, value in vlogparam_specs(resolved.parameters).items()
        if value.is_string
    }
    if not parameters:
        return
    root = Path(resolved.build_root).resolve()
    paths = sorted(root.rglob("*.vc" if tool == "verilator" else "*.scr"))
    if any(not path.resolve().is_relative_to(root) for path in paths):
        raise ValueError(f"{tool}: parameter record escapes build root")
    contents = {path: _read_records(path) for path in paths}
    for name, parameter in parameters.items():
        for original, normalized in _record_pairs(tool, name, parameter, str(resolved.toplevel)):
            _normalize_record(
                contents,
                original,
                normalized,
                f"{tool} Target {resolved.toplevel!r} parameter {name!r}",
            )
    for path, text in contents.items():
        if _read_records(path) != text:
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
