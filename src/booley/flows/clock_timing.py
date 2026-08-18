"""Per-clock timing representation shared by the FPGA and ASIC QoR Flows.

Fmax and critical-path delay are inherently *per-clock*: a single scalar is
meaningless for any multi-clock design ("Fmax for *what* clock?"). Both
:mod:`booley.flows.fpga.flow` and :mod:`booley.flows.synth.flow` therefore
report timing as a ``dict[str, ClockTiming]`` keyed by clock name rather than a
pair of top-level scalars (the legacy ``critical_path_ps``/``fmax_mhz`` fields
were removed — per-clock is the sole timing representation).

Aggregate ``wns_ns``/``whs_ns`` stay as honest whole-design worst-case scalars
on the metrics records; they are not part of this per-clock view (though each
clock also carries its own ``wns_ns``/``whs_ns`` here).

This module owns the record, the derivation of critical-path/Fmax from a clock
period and its worst setup slack, and the JSON round-trip used by reports and
criteria detail. It imports nothing from either Flow so the dependency flows
one way (principle 8 / DIP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from booley.core.boundary import as_float

# The five sub-metrics carried per clock, in report/detail order. Kept as a
# module constant so the JSON round-trip and the threshold engine agree on the
# spelling of every per-clock field.
CLOCK_TIMING_FIELDS: tuple[str, ...] = (
    "period_ns",
    "wns_ns",
    "whs_ns",
    "critical_path_ps",
    "fmax_mhz",
)


@dataclass
class ClockTiming:
    """Timing metrics for a single clock domain.

    ``wns_ns``/``whs_ns`` are this clock's worst setup/hold slack. ``period_ns``
    is the clock's constrained period. ``critical_path_ps`` and ``fmax_mhz`` are
    derived from the two via :func:`derive_critical_path_and_fmax` — a clock that
    reports slack but whose period is unknown leaves both ``None`` (the delay is
    ``period - slack`` and there is no honest value without the period).
    """

    clock: str
    period_ns: float | None = None
    wns_ns: float | None = None
    whs_ns: float | None = None
    critical_path_ps: float | None = None
    fmax_mhz: float | None = None


def derive_critical_path_and_fmax(
    period_ns: float | None, wns_ns: float | None
) -> tuple[float | None, float | None]:
    """Derive ``(critical_path_ps, fmax_mhz)`` from a period and worst slack.

    The critical path is ``period - worst_setup_slack``; Fmax is its inverse.
    A path with slack *s* against a clock of period *P* completes in ``P - s``
    (positive slack shortens the achieved delay below the period, negative slack
    means the design overran it). Returns ``(None, None)`` when the period is
    unknown, and leaves ``fmax_mhz`` ``None`` when the derived delay is
    non-positive (a degenerate over-constrained result we refuse to invert).

    Both Flows funnel through this one helper so the ns→ps→MHz arithmetic — and
    its edge cases — are identical between the Vivado and Yosys/STA flows.
    """
    if period_ns is None or wns_ns is None:
        return None, None
    critical_path_ps = (period_ns - wns_ns) * 1000.0
    if critical_path_ps <= 0:
        return critical_path_ps, None
    return critical_path_ps, 1_000_000.0 / critical_path_ps


def make_clock_timing(
    clock: str, period_ns: float | None, wns_ns: float | None, whs_ns: float | None
) -> ClockTiming:
    """Build a :class:`ClockTiming`, deriving critical-path/Fmax from period+WNS."""
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
    """Serialize a ``{clk: ClockTiming}`` map to nested JSON dicts.

    Each value is the sub-metric dict (``period_ns``/``wns_ns``/``whs_ns``/
    ``critical_path_ps``/``fmax_mhz``); the clock name lives in the outer key, so
    it is not repeated inside. Clocks are emitted in insertion order.
    """
    return {
        name: {field: getattr(ct, field) for field in CLOCK_TIMING_FIELDS}
        for name, ct in per_clock.items()
    }


def per_clock_from_json(raw: Any) -> dict[str, ClockTiming]:
    """Rebuild a ``{clk: ClockTiming}`` map from JSON (report / adapter payload).

    Tolerant of the Flow/adapter boundary: a non-dict ``raw`` yields ``{}``, a
    non-dict value for a clock is skipped, and missing sub-fields default to
    ``None`` rather than raising. Non-numeric sub-values are coerced to ``None``.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ClockTiming] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        out[str(name)] = ClockTiming(
            clock=str(name),
            **{field: as_float(value.get(field)) for field in CLOCK_TIMING_FIELDS},
        )
    return out


def worst_clock(per_clock: dict[str, ClockTiming]) -> ClockTiming | None:
    """Return the timing-worst clock (lowest Fmax / longest critical path).

    Used only for a single-line human display where one representative number is
    wanted — never as a stored metric (the scalars were removed precisely because
    a single number is ill-defined). Ranks by ``fmax_mhz`` (lower is worse),
    ignoring clocks with no Fmax; falls back to the largest ``critical_path_ps``,
    then to the most-negative ``wns_ns``. ``None`` when the map is empty.
    """
    if not per_clock:
        return None
    with_fmax = [ct for ct in per_clock.values() if ct.fmax_mhz is not None]
    if with_fmax:
        return min(with_fmax, key=lambda ct: ct.fmax_mhz)
    with_cp = [ct for ct in per_clock.values() if ct.critical_path_ps is not None]
    if with_cp:
        return max(with_cp, key=lambda ct: ct.critical_path_ps)
    with_wns = [ct for ct in per_clock.values() if ct.wns_ns is not None]
    if with_wns:
        return min(with_wns, key=lambda ct: ct.wns_ns)
    return next(iter(per_clock.values()))


def worst_fmax_from_json(raw: Any) -> float | None:
    """Timing-worst clock's Fmax (MHz) from a per_clock JSON map, or None.

    Convenience for the one-line criterion/console displays that want a single
    representative Fmax — ``worst_clock(per_clock_from_json(raw)).fmax_mhz`` in one
    call, tolerant of a missing/malformed map.
    """
    worst = worst_clock(per_clock_from_json(raw))
    return worst.fmax_mhz if worst else None
