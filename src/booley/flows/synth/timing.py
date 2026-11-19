"""Flow-level timing intent and normalized result parsing.

This module is the synthesis Flow's timing interface. EDA-tool command and
report implementations belong to the adapters under :mod:`backends`. It also
holds the OpenROAD PPA settings the timing resolver takes, so the Yosys adapter
can accept them without importing the OpenROAD adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from booley.flows.synth.mode import SynthMode

# Worst setup paths listed by each OpenROAD diagnostic timing report. Override
# project-wide with ``[flows.synth].timing_report_paths`` in ``booley.toml``.
DEFAULT_TIMING_REPORT_PATHS = 10


class StaTimingConfig(NamedTuple):
    """Resolved timing intent passed to the built-in synthesis adapters."""

    mode: SynthMode
    sdc: tuple[Path, ...] = ()
    utilization_pct: float = 40.0
    repair_timing: bool = True
    placement_density: float | None = None
    repair_hold: bool = False
    gate_cloning: bool = False
    setup_margin_ns: float = 0.0
    repair_tns_percent: float | None = None
    report_paths: int = DEFAULT_TIMING_REPORT_PATHS


@dataclass(frozen=True)
class OpenRoadPpaSettings:
    """OpenROAD-specific placement and timing-repair controls."""

    utilization_pct: float
    placement_density: float
    repair_setup: bool = True
    repair_hold: bool = False
    gate_cloning: bool = False
    setup_margin_ns: float = 0.0
    repair_tns_percent: float | None = None


DEFAULT_STA_UTILIZATION_PCT = 40.0

_CLOCK_CANDIDATES = ("clk_i", "clk", "clock", "i_clk", "aclk")
_PERCLOCK_RE = re.compile(
    r"STA_PERCLOCK:\s*name=(?P<name>\S+)\s+period_ns=(?P<period>[-+]?\d+(?:\.\d+)?)"
    r"\s+wns_ns=(?P<wns>NA|[-+]?\d+(?:\.\d+)?)"
    r"\s+whs_ns=(?P<whs>NA|[-+]?\d+(?:\.\d+)?)"
    r"\s+setup_same_clock=(?P<same>[01])"
)


class PerClockRow(NamedTuple):
    """One clock's normalized ``STA_PERCLOCK`` marker.

    ``setup_same_clock`` is True only when the clock's worst setup path is
    launched and captured by that same clock; a path launched by another
    clock (or by none) is timed against no period of this clock.
    """

    period_ns: float | None
    wns_ns: float | None
    whs_ns: float | None
    setup_same_clock: bool


def parse_perclock(text: str) -> dict[str, PerClockRow]:
    """Parse normalized per-clock timing markers from adapter output.

    Repeated markers for one clock keep the worst (minimum) slacks; the
    same-clock flag is the one of the marker whose setup slack wins.
    """
    rows: dict[str, PerClockRow] = {}
    for match in _PERCLOCK_RE.finditer(text):
        name = match.group("name")
        period = _optional_float(match.group("period"))
        wns = _optional_float(match.group("wns"))
        whs = _optional_float(match.group("whs"))
        same = match.group("same") == "1"
        previous = rows.get(name)
        if previous is None:
            rows[name] = PerClockRow(period, wns, whs, same)
            continue
        wins = wns is not None and (previous.wns_ns is None or wns < previous.wns_ns)
        rows[name] = PerClockRow(
            period if period is not None else previous.period_ns,
            _minimum_optional(previous.wns_ns, wns),
            _minimum_optional(previous.whs_ns, whs),
            same if wins else previous.setup_same_clock,
        )
    return rows


def detect_clock_port(netlist: Path) -> str | None:
    """Best-effort clock-port detection for simple single-clock projects."""
    try:
        text = netlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    inputs = set()
    for match in re.finditer(
        r"\binput\b\s*(?:wire\s+|reg\s+|logic\s+)?(?:\[[^\]]+\]\s*)?([^;]+);",
        text,
    ):
        for raw_name in match.group(1).split(","):
            name = raw_name.strip().split()[-1].lstrip("\\")
            if name:
                inputs.add(name)
    for candidate in _CLOCK_CANDIDATES:
        if candidate in inputs:
            return candidate
    return None


def _optional_float(token: str) -> float | None:
    if token == "NA":
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _minimum_optional(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)
