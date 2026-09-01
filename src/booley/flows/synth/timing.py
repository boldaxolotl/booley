"""Flow-level timing intent and normalized result parsing.

This module is the synthesis Flow's timing interface. EDA-tool command and
report implementations belong to the adapters under :mod:`backends`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from booley.flows.synth.mode import SynthMode


class StaTimingConfig(NamedTuple):
    """Resolved timing intent passed to the built-in synthesis adapters."""

    mode: SynthMode
    clock: str | None
    period_ps: float
    input_delay_pct: float
    output_delay_pct: float
    sdc: tuple[Path, ...] = ()
    utilization_pct: float = 40.0
    repair_timing: bool = True
    placement_density: float | None = None
    repair_hold: bool = False
    gate_cloning: bool = False
    setup_margin_ns: float = 0.0
    repair_tns_percent: float | None = None


class SdcOwnership(NamedTuple):
    """Constraint categories supplied by the Target's authored SDC."""

    clock: bool
    input_delay: bool
    output_delay: bool


DEFAULT_STA_PERIOD_PS = 4000.0
DEFAULT_STA_INPUT_DELAY_PCT = 30.0
DEFAULT_STA_OUTPUT_DELAY_PCT = 70.0
DEFAULT_STA_UTILIZATION_PCT = 40.0

_CLOCK_CANDIDATES = ("clk_i", "clk", "clock", "i_clk", "aclk")
_PERCLOCK_RE = re.compile(
    r"STA_PERCLOCK:\s*name=(?P<name>\S+)\s+period_ns=(?P<period>[-+]?\d+(?:\.\d+)?)"
    r"\s+wns_ns=(?P<wns>NA|[-+]?\d+(?:\.\d+)?)"
    r"\s+whs_ns=(?P<whs>NA|[-+]?\d+(?:\.\d+)?)"
)
_CREATE_CLOCK_NAME_RE = re.compile(r"(?m)^[^\n#]*?\bcreate_clock\b[^\n]*?-name\s+([^\s\]\}]+)")
_SDC_CREATE_CLOCK_RE = re.compile(r"(?m)^[^\n#]*?\bcreate_clock\b")
_SDC_INPUT_DELAY_RE = re.compile(r"(?m)^[^\n#]*?\bset_input_delay\b")
_SDC_OUTPUT_DELAY_RE = re.compile(r"(?m)^[^\n#]*?\bset_output_delay\b")
_CREATE_CLOCK_PERIOD_RE = re.compile(
    r"(?m)^[^\n#]*?\bcreate_clock\b[^\n]*?-period\s+"
    r"([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)"
)


def parse_perclock(text: str) -> dict[str, dict[str, float | None]]:
    """Parse normalized per-clock timing markers from adapter output."""
    rows: dict[str, dict[str, float | None]] = {}
    for match in _PERCLOCK_RE.finditer(text):
        name = match.group("name")
        period = _optional_float(match.group("period"))
        wns = _optional_float(match.group("wns"))
        whs = _optional_float(match.group("whs"))
        row = rows.setdefault(name, {"period_ns": period, "wns_ns": wns, "whs_ns": whs})
        if period is not None:
            row["period_ns"] = period
        row["wns_ns"] = _minimum_optional(row["wns_ns"], wns)
        row["whs_ns"] = _minimum_optional(row["whs_ns"], whs)
    return rows


def read_user_sdc_text(config: StaTimingConfig) -> str:
    """Concatenate the Target's authored SDC files in fileset order."""
    return "\n".join(path.read_text(encoding="utf-8") for path in config.sdc)


def sdc_ownership(config: StaTimingConfig) -> SdcOwnership:
    """Return which default constraint categories the authored SDC replaces."""
    text = read_user_sdc_text(config)
    return SdcOwnership(
        clock=bool(_SDC_CREATE_CLOCK_RE.search(text)),
        input_delay=bool(_SDC_INPUT_DELAY_RE.search(text)),
        output_delay=bool(_SDC_OUTPUT_DELAY_RE.search(text)),
    )


def parse_sdc_clock_names(text: str) -> list[str]:
    """Return authored ``create_clock -name`` values in source order."""
    return _CREATE_CLOCK_NAME_RE.findall(text)


def first_authored_clock(config: StaTimingConfig) -> str | None:
    """Return the first authored clock name, if the Target declares one."""
    names = parse_sdc_clock_names(read_user_sdc_text(config))
    return names[0] if names else None


def parse_sdc_clock_periods_ps(text: str) -> list[float]:
    """Return authored ``create_clock -period`` values converted from ns to ps."""
    periods: list[float] = []
    for match in _CREATE_CLOCK_PERIOD_RE.finditer(text):
        try:
            periods.append(float(match.group(1)) * 1000.0)
        except ValueError:
            continue
    return periods


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
