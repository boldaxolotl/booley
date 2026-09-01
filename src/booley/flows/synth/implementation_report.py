"""Synthesis adapter for the shared implementation-report module."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from booley.flows.implementation_report import (
    ImplementationContext,
    ImplementationReport,
    ImplementationRun,
    MetricPolicy,
    build_implementation_report,
)

if TYPE_CHECKING:
    from booley.criteria.templates import TargetPair

    from .flow import SynthMetrics

_SYNTH_DELTA_METRICS = (
    "area_um2",
    "area_kge",
    "cells",
    "wire_count",
    "wns_ns",
    "whs_ns",
    "reg2reg_slack_ns",
    "reg2reg_fmax_mhz",
)


def _timing_reasons(
    metrics: SynthMetrics, *, fatal: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reasons: list[str] = []
    detail = metrics.qor_detail()
    for label, key in (("setup", "wns_ns"), ("hold", "whs_ns")):
        value = detail.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            reasons.append(f"{label} slack {value:.3f} ns")
    resolved = tuple(reasons)
    return ((), resolved) if fatal else (resolved, ())


def _io_bound(metrics: SynthMetrics) -> bool:
    worst = metrics.wns_ns
    register = metrics.reg2reg_slack_ns
    return worst is not None and register is not None and worst < register - 1e-3


def _run(metrics: SynthMetrics, *, fatal_timing: bool) -> ImplementationRun:
    warnings, failures = _timing_reasons(metrics, fatal=fatal_timing)
    conditions = {
        **metrics.structural_detail(),
        "process_count": metrics.process_count,
        "io_bound_critical": _io_bound(metrics),
    }
    return ImplementationRun(
        passed=metrics.passed,
        tool_returncode=metrics.returncode,
        timed_out=metrics.timed_out,
        infra_error=metrics.infra_error or None,
        elapsed_s=metrics.elapsed_s,
        termination=metrics.termination,
        warning_reasons=warnings if metrics.passed else (),
        failure_reasons=failures if metrics.passed else (),
        metrics=metrics.qor_detail(),
        conditions=conditions,
        completion={
            "yosys": metrics.yosys_complete,
            "timing": metrics.timing_complete,
            "structural_checks": metrics.structural_checks_complete,
            "ppa": metrics.ppa_complete,
        },
        resources={"peak_rss_mb": metrics.peak_rss_mb},
        diagnostic_excerpt=metrics.failure_output or None,
        recipe_fingerprint=metrics.recipe_fingerprint or None,
        recipe_snapshot=metrics.recipe_snapshot,
        provenance=metrics.run_evidence,
        artifacts={
            **({"log": metrics.log_path} if metrics.log_path else {}),
            **({"dirs": dict(metrics.dirs)} if metrics.dirs else {}),
        },
    )


def build_synth_implementation_report(
    *,
    target: str,
    pair: TargetPair,
    current: SynthMetrics,
    baseline: SynthMetrics | None,
    baseline_ref: str | None,
    resolved_baseline_ref: str | None,
    eda_tool: str | None,
    fatal_timing: bool,
) -> ImplementationReport:
    """Normalize synthesis evidence and build its canonical v1 report."""
    context = ImplementationContext(
        flow="synth",
        target=target,
        eda_tool=eda_tool,
        invocation_run_id=os.environ.get("BOOLEY_RUN_ID", ""),
        baseline_target=pair.baseline,
        requested_baseline_ref=baseline_ref,
        resolved_baseline_ref=resolved_baseline_ref,
    )
    policy = MetricPolicy(
        delta_metrics=_SYNTH_DELTA_METRICS,
        required_comparison_metrics=("area_kge",),
    )
    return build_implementation_report(
        context,
        _run(current, fatal_timing=fatal_timing),
        _run(baseline, fatal_timing=fatal_timing) if baseline is not None else None,
        policy,
    )
