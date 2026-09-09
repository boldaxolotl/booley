"""Per-clock timing values shared by implementation Flows and Criteria."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from booley.core.boundary import as_float

CLOCK_TIMING_FIELDS: tuple[str, ...] = (
    "period_ns",
    "wns_ns",
    "whs_ns",
    "critical_path_ps",
    "fmax_mhz",
)


@dataclass
class ClockTiming:
    """Timing metrics for a single clock domain."""

    clock: str
    period_ns: float | None = None
    wns_ns: float | None = None
    whs_ns: float | None = None
    critical_path_ps: float | None = None
    fmax_mhz: float | None = None


def derive_critical_path_and_fmax(
    period_ns: float | None, wns_ns: float | None
) -> tuple[float | None, float | None]:
    """Derive critical-path delay and Fmax from period and worst setup slack."""
    if period_ns is None or wns_ns is None:
        return None, None
    critical_path_ps = (period_ns - wns_ns) * 1000.0
    if critical_path_ps <= 0:
        return critical_path_ps, None
    return critical_path_ps, 1_000_000.0 / critical_path_ps


def make_clock_timing(
    clock: str, period_ns: float | None, wns_ns: float | None, whs_ns: float | None
) -> ClockTiming:
    """Build a clock record and derive its critical-path delay and Fmax."""
    critical_path_ps, fmax_mhz = derive_critical_path_and_fmax(period_ns, wns_ns)
    return ClockTiming(
        clock=clock,
        period_ns=period_ns,
        wns_ns=wns_ns,
        whs_ns=whs_ns,
        critical_path_ps=critical_path_ps,
        fmax_mhz=fmax_mhz,
    )


def per_clock_to_json(per_clock: dict[str, ClockTiming]) -> dict[str, dict[str, Any]]:
    """Serialize clock records to their persisted nested mapping."""
    return {
        name: {field: getattr(timing, field) for field in CLOCK_TIMING_FIELDS}
        for name, timing in per_clock.items()
    }


def per_clock_from_json(raw: Any) -> dict[str, ClockTiming]:
    """Deserialize a tolerant nested clock mapping."""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, ClockTiming] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        result[str(name)] = ClockTiming(
            clock=str(name),
            **{field: as_float(value.get(field)) for field in CLOCK_TIMING_FIELDS},
        )
    return result


def worst_clock(per_clock: dict[str, ClockTiming]) -> ClockTiming | None:
    """Return the timing-worst clock using stable metric fallbacks."""
    if not per_clock:
        return None
    with_fmax = [timing for timing in per_clock.values() if timing.fmax_mhz is not None]
    if with_fmax:
        return min(with_fmax, key=lambda timing: timing.fmax_mhz)
    with_path = [
        timing for timing in per_clock.values() if timing.critical_path_ps is not None
    ]
    if with_path:
        return max(with_path, key=lambda timing: timing.critical_path_ps)
    with_wns = [timing for timing in per_clock.values() if timing.wns_ns is not None]
    if with_wns:
        return min(with_wns, key=lambda timing: timing.wns_ns)
    return next(iter(per_clock.values()))


def worst_fmax_from_json(raw: Any) -> float | None:
    """Return the timing-worst clock's Fmax from a persisted clock mapping."""
    timing = worst_clock(per_clock_from_json(raw))
    return timing.fmax_mhz if timing is not None else None
