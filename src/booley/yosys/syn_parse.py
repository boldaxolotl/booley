"""Result parsing for the Yosys synthesis flow.

Pure text/number extraction: CLI parameter lists, chip area from Yosys
``stat`` output, and area-to-gate-equivalent conversion.  No subprocess or
external-EDA-tool side effects beyond reading the stat file.  A leaf module — it
does not import from ``syn_core``.
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

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


def parse_area_from_stat(stat_file: Path) -> float | None:
    """Extract chip area from stat file. Returns float or None.
    Looks for 'Chip area for top module' (hierarchical total) first,
    falls back to last 'Chip area' match."""
    if not stat_file.exists():
        return None
    text = stat_file.read_text(encoding="utf-8")
    m = re.search(r"Chip area for top module .*?:\s*([\d.]+)", text)
    if m:
        # ([\d.]+) can capture malformed tokens like "." or "1.2.3"; treat
        # a non-float as absent rather than crashing the flow.
        with contextlib.suppress(ValueError):
            return float(m.group(1))
    for candidate in reversed(re.findall(r"Chip area for .*?:\s*([\d.]+)", text)):
        with contextlib.suppress(ValueError):
            return float(candidate)
    return None


def area_to_kge(area_um2: float | None) -> float | None:
    """Convert area in µm² to kilogate equivalents (kGE), using NAND2_X1 as 1 GE."""
    if area_um2 is None:
        return None
    return area_um2 / (NAND2_AREA_UM2 * 1000)
