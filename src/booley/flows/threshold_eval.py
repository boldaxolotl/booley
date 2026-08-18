"""Numeric threshold evaluator — criteria metrics vs. numeric caps/floors.

Free functions that evaluate a single threshold param (``*_max`` absolute cap,
``*_min`` absolute floor) from a Flow/LLM-supplied metric dict, plus the metric
coercion helper and the default synthesis metric map. Split out of
``development_state`` (principle 8); imports nothing from it so the dependency
flows one way. Delta (growth/reduction) threshold logic remains as methods on
``DevelopmentState`` because it is bound to that class.
"""

from __future__ import annotations

import logging
from typing import Any

from booley.core.boundary import as_float
from booley.flows.clock_timing import (
    CLOCK_TIMING_FIELDS,
    per_clock_from_json,
    worst_clock,
)

logger = logging.getLogger(__name__)

# The per-clock timing sub-metrics — a *flat* threshold on one of these (no clock
# named) gates on the timing-worst clock, since Fmax/critical-path have no single
# whole-design scalar. Everything else is read straight from the detail dict.
_PER_CLOCK_METRIC_KEYS = frozenset(CLOCK_TIMING_FIELDS)


# Maps synthesis_ok param prefix to the metric key in the detail dict.
# Includes full-name variants (area_um2, area_kge) for absolute caps.
_SYNTH_METRIC_MAP: dict[str, str] = {
    "area": "area_um2",
    "area_um2": "area_um2",
    "area_kge": "area_kge",
    "cell_count": "cells",
    "wire_count": "wire_count",
    "critical_path_ps": "critical_path_ps",
    "fmax_mhz": "fmax_mhz",
}


def _coerce_metric(value: Any) -> float | None:
    """Coerce a Flow/LLM-supplied metric value to float, or None if not numeric.

    Metric dicts (``detail``/``baseline``) cross a Flow/LLM boundary, so a
    value may be a string, bool, NaN/inf, or other non-numeric type. Callers
    treat None as 'metric unavailable' and skip the gate rather than crashing
    the disposition decision path. Delegates to ``core.boundary.as_float``,
    which rejects both the bool trap (``float(True)`` would silently become
    1.0) and NaN/inf (valid floats that would otherwise poison a `<=`/`>=`
    comparison).
    """
    return as_float(value)


def resolve_metric(
    detail: dict[str, Any],
    metric_prefix: str,
    metric_map: dict[str, str],
) -> tuple[float | None, str | None]:
    """Resolve a (possibly per-clock) metric prefix to ``(value, label)``.

    Two addressing forms share one resolver so caps, floors, and deltas all
    speak per-clock:

    * flat — ``"<metric>"`` reads ``detail[metric_map[metric]]``; a flat
      *timing* metric with no stored scalar falls back to the timing-worst clock
      of ``detail["per_clock"]`` (so ``fmax_mhz_min`` means "every clock ≥ N");
    * per-clock — ``"<clock>.<sub>"`` reads
      ``detail["per_clock"][clock][metric_map[sub]]`` (Fmax/critical-path are
      per-clock, so a threshold names the clock: ``clk_i.fmax_mhz_min``).

    ``label`` is the resolved metric key used in the check/skip message, or
    ``None`` when the prefix maps to no known metric (the caller then skips the
    param entirely). A known metric whose value is merely absent returns
    ``(None, label)`` so the caller emits a clean "not available" skip.
    """
    if "." in metric_prefix:
        clock, _, sub = metric_prefix.partition(".")
        metric_key = metric_map.get(sub)
        if not metric_key:
            return None, None
        label = f"per_clock[{clock}].{metric_key}"
        per_clock = detail.get("per_clock")
        entry = per_clock.get(clock) if isinstance(per_clock, dict) else None
        if not isinstance(entry, dict):
            return None, label
        return _coerce_metric(entry.get(metric_key)), label
    metric_key = metric_map.get(metric_prefix)
    if not metric_key:
        return None, None
    value = _coerce_metric(detail.get(metric_key))
    if value is None and metric_key in _PER_CLOCK_METRIC_KEYS:
        # A flat timing threshold gates on the worst clock (single-clock designs
        # have exactly one). No scalar is stored — read it out of per_clock.
        worst = worst_clock(per_clock_from_json(detail.get("per_clock")))
        if worst is not None:
            value = _coerce_metric(getattr(worst, metric_key, None))
    return value, metric_key


def min_gate_key(metric_prefix: str) -> str:
    """``min_allowed`` gate key for a prefix (per-clock gates on the sub-metric).

    ``clk_i.fmax_mhz`` gates on ``fmax_mhz``; a flat ``fmax_mhz`` gates on
    itself — so one ``_min_allowed`` list covers both flat and per-clock floors.
    """
    return metric_prefix.rpartition(".")[2] if "." in metric_prefix else metric_prefix


def _check_absolute_cap(
    param_key: str,
    suffix: str,
    threshold: float,
    detail: dict[str, Any],
    metric_map: dict[str, str],
) -> dict[str, Any] | None:
    """Evaluate an absolute cap (*_max) threshold. Returns check dict or None."""
    metric_prefix = param_key.removesuffix(suffix)
    value, label = resolve_metric(detail, metric_prefix, metric_map)
    if label is None:
        return None
    if value is None:
        return {
            "param": param_key,
            "pass": True,
            "skipped": True,
            "reason": f"metric {label} not available",
        }
    return {"param": param_key, "pass": value <= threshold, "value": value, "threshold": threshold}


def _check_absolute_min(
    param_key: str,
    threshold: float,
    detail: dict[str, Any],
    metric_map: dict[str, str],
    min_allowed: set[str],
) -> dict[str, Any] | None:
    """Evaluate an absolute minimum (*_min) threshold. Returns check dict or None."""
    metric_prefix = param_key.removesuffix("_min")
    gate_key = min_gate_key(metric_prefix)
    if gate_key not in min_allowed:
        logger.warning(
            "Rejecting *_min param %r: metric %r not in min_allowed", param_key, gate_key
        )
        return None
    value, label = resolve_metric(detail, metric_prefix, metric_map)
    if label is None:
        return None
    if value is None:
        return {
            "param": param_key,
            "pass": True,
            "skipped": True,
            "reason": f"{label} not available",
        }
    return {"param": param_key, "pass": value >= threshold, "value": value, "threshold": threshold}
