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
from typing import Any, NamedTuple

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
)
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


# Names kept per list in the JSON detail; the run log holds every name.
UNCONSTRAINED_SAMPLE_SIZE = 10
_CHECK_PREFIX = "STA_CONSTRAINT_CHECK:"
_CHECK_HEADER_RE = re.compile(
    r"There (?:is|are) \d+ (?P<kind>input port|output port|unconstrained endpoint)"
)
_CHECK_KINDS = {
    "input port": "inputs_without_delay",
    "output port": "outputs_without_delay",
    "unconstrained endpoint": "endpoints",
}


@dataclass(frozen=True)
class UnconstrainedTiming:
    """What the authored SDC leaves untimed, from OpenSTA's constraint check."""

    endpoints: tuple[str, ...] = ()
    inputs_without_delay: tuple[str, ...] = ()
    outputs_without_delay: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        """True when any endpoint or I/O port is left untimed."""
        return bool(self.endpoints or self.inputs_without_delay or self.outputs_without_delay)

    def to_detail(self) -> dict[str, dict[str, Any]]:
        """Counts with a bounded sample of names per list, for the JSON reports."""
        return {
            key: {"count": len(names), "sample": list(names[:UNCONSTRAINED_SAMPLE_SIZE])}
            for key, names in (
                ("endpoints", self.endpoints),
                ("inputs_without_delay", self.inputs_without_delay),
                ("outputs_without_delay", self.outputs_without_delay),
            )
        }


def parse_unconstrained_timing(text: str) -> UnconstrainedTiming | None:
    """Parse the ``STA_CONSTRAINT_CHECK`` block, or None without a complete one.

    A header such as ``There are 2 input ports missing set_input_delay.`` opens a
    list and each following line adds one name, verbatim (bus bits such as
    ``data[3]`` survive). A block without ``end`` (OpenROAD died mid-check)
    counts as no check.
    """
    lists: dict[str, list[str]] = {key: [] for key in _CHECK_KINDS.values()}
    in_block = False
    complete = False
    current: list[str] | None = None
    for raw in text.splitlines():
        if not raw.startswith(_CHECK_PREFIX):
            continue
        body = raw[len(_CHECK_PREFIX) :].rstrip()
        # Delimiters are matched unindented: a port named ``end`` is a name line.
        if body == " begin":
            in_block = True
        elif body == " end":
            complete = in_block
            break
        elif in_block and (header := _CHECK_HEADER_RE.search(body)):
            current = lists[_CHECK_KINDS[header.group("kind")]]
        elif in_block and body.startswith("  ") and current is not None:
            current.append(body.strip())
    if not complete:
        return None
    return UnconstrainedTiming(
        endpoints=tuple(lists["endpoints"]),
        inputs_without_delay=tuple(lists["inputs_without_delay"]),
        outputs_without_delay=tuple(lists["outputs_without_delay"]),
    )


def read_user_sdc_text(config: StaTimingConfig) -> str:
    """Concatenate the Target's authored SDC files in fileset order."""
    return "\n".join(path.read_text(encoding="utf-8") for path in config.sdc)


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
