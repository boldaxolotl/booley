"""FPGA adapter for the shared implementation-report module."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from booley.flows.clock_timing import per_clock_to_json
from booley.flows.implementation_report import (
    ImplementationContext,
    ImplementationReport,
    ImplementationRun,
    MetricPolicy,
    build_implementation_report,
)

if TYPE_CHECKING:
    from booley.criteria.templates import TargetPair

    from .backends.vivado.metrics import FpgaMetrics

_FPGA_DELTA_METRICS = (
    "lut_count",
    "ff_count",
    "bram_count",
    "dsp_count",
    "wns_ns",
    "whs_ns",
)


def _failure_reasons(metrics: FpgaMetrics) -> tuple[str, ...]:
    if metrics.infra_error:
        return ()
    reasons: list[str] = []
    if not metrics.has_primary_metrics:
        reasons.append("primary LUT/FF metrics are unavailable")
    if not metrics.timing_met:
        reasons.append("timing constraints are not met")
    if metrics.has_critical:
        reasons.append("critical structural conditions were detected")
    return tuple(reasons)


def _run(metrics: FpgaMetrics) -> ImplementationRun:
    metric_values = {
        "lut_count": metrics.lut_count,
        "ff_count": metrics.ff_count,
        "bram_count": metrics.bram_count,
        "dsp_count": metrics.dsp_count,
        "wns_ns": metrics.wns_ns,
        "whs_ns": metrics.whs_ns,
        "per_clock": per_clock_to_json(metrics.per_clock),
    }
    return ImplementationRun(
        passed=metrics.passed,
        tool_returncode=metrics.returncode,
        timed_out=metrics.timed_out,
        infra_error=metrics.infra_error or None,
        elapsed_s=metrics.elapsed_s,
        failure_reasons=_failure_reasons(metrics),
        metrics=metric_values,
        conditions={
            "has_critical": metrics.has_critical,
            "latches": metrics.latches,
            "comb_loops": metrics.comb_loops,
            "multi_driven": metrics.multi_driven,
        },
        completion={
            "primary_metrics": metrics.has_primary_metrics,
            "timing": metrics.wns_ns is not None and metrics.whs_ns is not None,
            "route": metrics.returncode == 0 and not metrics.infra_error,
        },
        diagnostic_excerpt=metrics.failure_output or None,
        recipe_fingerprint=metrics.recipe_fingerprint or None,
        recipe_snapshot=metrics.recipe_snapshot,
        provenance=metrics.run_evidence,
        cache={
            "cached": metrics.cached,
            "fingerprint": metrics.cache_fingerprint or None,
        },
        cache_consumer_run_id=metrics.cache_consumer_run_id or None,
        artifacts={
            **({"log": metrics.log_path} if metrics.log_path else {}),
            **({"dirs": dict(metrics.dirs)} if metrics.dirs else {}),
        },
    )


def build_fpga_implementation_report(
    *,
    target: str,
    pair: TargetPair,
    current: FpgaMetrics,
    baseline: FpgaMetrics | None,
    baseline_ref: str | None,
    resolved_baseline_ref: str | None,
    eda_tool: str | None,
) -> ImplementationReport:
    """Normalize FPGA evidence and build its canonical v1 report."""
    context = ImplementationContext(
        flow="fpga",
        target=target,
        eda_tool=eda_tool,
        invocation_run_id=os.environ.get("BOOLEY_RUN_ID", ""),
        baseline_target=pair.baseline,
        requested_baseline_ref=baseline_ref,
        resolved_baseline_ref=resolved_baseline_ref,
    )
    policy = MetricPolicy(
        delta_metrics=_FPGA_DELTA_METRICS,
        required_comparison_metrics=("lut_count", "ff_count"),
    )
    return build_implementation_report(
        context,
        _run(current),
        _run(baseline) if baseline is not None else None,
        policy,
    )
