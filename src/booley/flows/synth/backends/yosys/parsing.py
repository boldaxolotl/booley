"""Result parsing for the Yosys synthesis flow.

Pure text/number extraction: CLI parameter lists, chip area from Yosys
``stat`` output, and area-to-gate-equivalent conversion.  No subprocess or
external-EDA-tool side effects beyond reading the stat file.  A leaf module — it
does not import from ``syn_core``.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from booley.core.boundary import as_float

# Nangate 45nm NAND2_X1 area in µm² — used as 1 gate equivalent (GE)
NAND2_AREA_UM2 = 0.798


def parse_params(param_list: list[str]) -> dict[str, str]:
    """
    Parse parameter list from CLI (e.g. ['OP_W=32', 'DEPTH=4']).
    Returns dict of {name: value}.
    """
    params = {}
    for p in param_list:
        if "=" not in p:
            sys.exit(f"ERROR: Invalid parameter format '{p}'. Use NAME=VALUE (e.g. OP_W=32)")
        name, value = p.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            sys.exit(f"ERROR: Empty parameter name in '{p}'")
        params[name] = value
    return params


# Numeric tokens are complete fields: malformed prefixes and nonfinite values
# cannot become usable area evidence.
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_AREA_COLUMN = rf"(?:-|{_NUMBER})"
_STAT_BOUNDARY_RE = re.compile(
    r"^(?:(?P<stat>(?:\d+(?:\.\d+)*\.\s+)?Printing statistics\.)|"
    r"(?P<artifact>--- .+ ---)|"
    r"(?P<end>(?:\d+(?:\.\d+)*\.\s+Executing\b|Warnings:|End of script\.|ERROR:)[^\n]*))[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)
_MODULE_RE = re.compile(r"^\s*=== (?P<name>.+?) ===\s*$", re.MULTILINE)
_SUMMARY_RE = re.compile(
    rf"^\s*(?:(?P<old>Number of (?:cells|wires|processes)):\s*(?P<old_count>\d+)|"
    rf"(?P<count>\d+|-)(?:\s+(?P<area>{_AREA_COLUMN}))?\s+(?P<kind>cells|wires|processes))\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CELL_RE = re.compile(
    rf"^\s+(?:(?P<cell_first>\$\S+)\s+(?P<old_count>\d+)(?:\s+(?P<old_area>{_AREA_COLUMN}))?|"
    rf"(?P<count>\d+)(?:\s+(?P<area>{_AREA_COLUMN}))?\s+(?P<cell>\$\S+))\s*$",
    re.MULTILINE,
)
_LATCH_RE = re.compile(r"\$(?:a?dlatch|_DLATCH(?:SR)?_[A-Z]+_)", re.IGNORECASE)
_CHIP_AREA_RE = re.compile(r"Chip area for (?P<module>[^\n:]+):\s*(?P<value>\S+)")


@dataclass(frozen=True)
class StatSummary:
    """Authoritative final Yosys inventory, with absent area kept unknown."""

    section: str | None = None
    area_um2: float | None = None
    cells: int | None = None
    wires: int = 0
    processes: int = 0
    latch_types: dict[str, int] = field(default_factory=dict)


def _finite_number(token: str) -> float | None:
    if re.fullmatch(_NUMBER, token) is None:
        return None
    return as_float(token)


def _valid_column(token: str | None) -> bool:
    return token is None or token == "-" or _finite_number(token) is not None


def _summary_counts(section: str) -> dict[str, int]:
    counts = {}
    for match in _SUMMARY_RE.finditer(section):
        if not _valid_column(match["area"]):
            continue
        kind = match["kind"] or match["old"].split()[-1]
        token = match["count"] or match["old_count"]
        counts[kind.lower()] = 0 if token == "-" else int(token)
    return counts


def _cell_inventory(section: str) -> dict[str, int]:
    inventory = {}
    for match in _CELL_RE.finditer(section):
        if not _valid_column(match["area"] or match["old_area"]):
            continue
        cell = match["cell"] or match["cell_first"]
        inventory[cell] = int(match["count"] or match["old_count"])
    return inventory


def _selected_module(section: str) -> str | None:
    """Use hierarchy aggregates once; otherwise the last usable module."""
    modules = list(_MODULE_RE.finditer(section))
    if not modules:
        return section if "cells" in _summary_counts(section) or _cell_inventory(section) else None
    candidates = []
    for index, match in enumerate(modules):
        end = modules[index + 1].start() if index + 1 < len(modules) else len(section)
        module = section[match.end() : end]
        if "cells" in _summary_counts(module) or _cell_inventory(module):
            if match["name"].lower() == "design hierarchy":
                return module
            candidates.append(module)
    return candidates[-1] if candidates else None


def authoritative_stat_section(output: str) -> str | None:
    """Select final usable stat; terminated diagnostic tails are not inventories."""
    candidates = []
    start, active = 0, True
    for boundary in _STAT_BOUNDARY_RE.finditer(output):
        if active:
            candidates.append(output[start : boundary.start()])
        active = bool(boundary["stat"]) or bool(
            boundary["artifact"]
            and re.fullmatch(r"--- (?:stat_[^/]+\.txt|yosys\.log) ---", boundary["artifact"])
        )
        start = boundary.end()
    if active:
        candidates.append(output[start:])
    for section in reversed(candidates):
        selected = _selected_module(section)
        if selected is not None:
            return section
    return None


def _chip_area(section: str) -> float | None:
    matches = list(_CHIP_AREA_RE.finditer(section))
    top = [match for match in matches if match["module"].startswith("top module")]
    preferred = top or matches
    if not preferred:
        return None
    # The final requested area remains unknown if its token is malformed.
    return _finite_number(preferred[-1]["value"])


def parse_stat(output: str) -> StatSummary:
    """Parse old/logical/liberty stat grammars through one selection policy."""
    section = authoritative_stat_section(output)
    if section is None:
        # Private Flow helpers also accept isolated legacy summary snippets.
        counts = _summary_counts(output) if not _STAT_BOUNDARY_RE.search(output) else {}
        return StatSummary(
            area_um2=_chip_area(output),
            wires=counts.get("wires", 0),
            processes=counts.get("processes", 0),
        )
    module = _selected_module(section)
    assert module is not None
    counts = _summary_counts(module)
    inventory = _cell_inventory(module)
    latches = {cell: count for cell, count in inventory.items() if _LATCH_RE.fullmatch(cell)}
    return StatSummary(
        section=section,
        area_um2=_chip_area(section),
        cells=counts.get("cells"),
        wires=counts.get("wires", 0),
        processes=counts.get("processes", 0),
        latch_types=latches,
    )


def parse_area_from_stat(stat_file: Path) -> float | None:
    """Extract finite area from the authoritative final stat, or None."""
    try:
        text = stat_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return parse_stat(text).area_um2


def area_to_kge(area_um2: float | None) -> float | None:
    """Convert area in µm² to kilogate equivalents (kGE), using NAND2_X1 as 1 GE."""
    if area_um2 is None:
        return None
    return area_um2 / (NAND2_AREA_UM2 * 1000)
