"""Canonical reporting for implementation-oriented FPGA and ASIC Flows.

Flow adapters normalize their native metrics into :class:`ImplementationRun`.
This module then owns the policy-resolved verdict, comparison, durable envelope,
aggregate MCP projection, target paths, and live progress shape.  Legacy report
fields remain outside the versioned ``implementation`` envelope while readers
migrate to the shared schema.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339

Grade = Literal["pass", "warn", "fail", "error"]

SCHEMA_VERSION = 1
ENVELOPE_KEY = "implementation"
_DIAGNOSTIC_LIMIT = 4_000
_GRADE_SEVERITY: dict[Grade, int] = {"pass": 0, "warn": 1, "fail": 2, "error": 3}


def _json_copy(value: Any) -> Any:
    """Defensively copy JSON-compatible adapter input at the public boundary."""
    return copy.deepcopy(value)


def _bounded_diagnostic(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= _DIAGNOSTIC_LIMIT:
        return value
    omitted = len(value) - _DIAGNOSTIC_LIMIT
    return f"[... {omitted} character(s) omitted ...]\n{value[-_DIAGNOSTIC_LIMIT:]}"


@dataclass(frozen=True)
class ImplementationContext:
    """Identity and comparison coordinates for one implementation target."""

    flow: str
    target: str
    eda_tool: str | None = None
    invocation_run_id: str = ""
    baseline_target: str | None = None
    requested_baseline_ref: str | None = None
    resolved_baseline_ref: str | None = None
    timestamp: str = field(default_factory=utc_now_rfc3339)

    def __post_init__(self) -> None:
        if not self.flow or not self.target:
            raise ValueError("implementation flow and target must be non-empty")


@dataclass(frozen=True)
class ImplementationRun:
    """Flow-neutral evidence for one current or baseline tool execution."""

    passed: bool
    tool_returncode: int
    metrics: Mapping[str, Any]
    timed_out: bool = False
    infra_error: str | None = None
    elapsed_s: float = 0.0
    termination: str | None = None
    warning_reasons: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()
    conditions: Mapping[str, Any] = field(default_factory=dict)
    completion: Mapping[str, Any] = field(default_factory=dict)
    resources: Mapping[str, Any] = field(default_factory=dict)
    diagnostic_excerpt: str | None = None
    recipe_fingerprint: str | None = None
    recipe_snapshot: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    cache: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    cache_consumer_run_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "metrics",
            "conditions",
            "completion",
            "resources",
            "recipe_snapshot",
            "provenance",
            "cache",
            "artifacts",
        ):
            object.__setattr__(self, name, _json_copy(getattr(self, name)))


@dataclass(frozen=True)
class MetricPolicy:
    """Adapter-owned comparison policy for scalar implementation metrics."""

    delta_metrics: tuple[str, ...]
    required_comparison_metrics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImplementationReport:
    """One canonical target report with compatibility projections."""

    _canonical: dict[str, Any] = field(repr=False)

    @property
    def canonical(self) -> dict[str, Any]:
        """Return a defensive copy of the canonical envelope."""
        return _json_copy(self._canonical)

    @property
    def grade(self) -> Grade:
        """Return the single policy-resolved target grade."""
        return self._canonical["status"]["grade"]

    @property
    def passed(self) -> bool:
        """Whether the target outcome maps to process exit zero."""
        return bool(self._canonical["status"]["passed"])

    @property
    def target(self) -> str:
        return str(self._canonical["identity"]["target"])

    def envelope(self, legacy: Mapping[str, Any]) -> dict[str, Any]:
        """Nest canonical v1 beside an unchanged legacy projection."""
        payload = _json_copy(dict(legacy))
        payload[ENVELOPE_KEY] = self.canonical
        return payload

    def mcp_entry(self) -> dict[str, Any]:
        """Return the bounded-by-construction target projection for MCP."""
        entry = self.canonical
        entry["recipe"].pop("snapshot", None)
        entry["status"].pop("diagnostic_excerpt", None)
        comparison = entry.get("comparison")
        if isinstance(comparison, dict):
            comparison.get("baseline", {}).get("recipe", {}).pop("snapshot", None)
        return entry


@dataclass(frozen=True)
class ImplementationAggregate:
    """Canonical run-level projection and its process exit code."""

    detail: dict[str, Any]
    exit_code: int


@dataclass(frozen=True)
class PublicationLocations:
    """Filesystem locations used to publish a target or progress checkpoint."""

    work_dir: Path
    report_dir: Path | None
    invocation_dir: Path | None


@dataclass(frozen=True)
class PublishedReport:
    """Paths and payload produced by durable target publication."""

    stable_path: Path | None
    invocation_path: Path | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class ImplementationProgress:
    """Common live checkpoint for long implementation matrices."""

    flow: str
    run_id: str
    targets: tuple[str, ...]
    completed_targets: tuple[str, ...] = ()
    baseline_completed_targets: tuple[str, ...] = ()
    phase: str = "running"
    complete: bool = False
    baseline_ref: str | None = None
    reports: Mapping[str, ImplementationReport] = field(default_factory=dict)


def _run_grade(run: ImplementationRun) -> Grade:
    if run.infra_error:
        return "error"
    if not run.passed or run.failure_reasons:
        return "fail"
    if run.warning_reasons:
        return "warn"
    return "pass"


def _status(run: ImplementationRun, grade: Grade | None = None) -> dict[str, Any]:
    resolved = grade or _run_grade(run)
    return {
        "grade": resolved,
        "passed": resolved in ("pass", "warn"),
        "tool_returncode": run.tool_returncode,
        "timed_out": run.timed_out,
        "infra_error": run.infra_error,
        "elapsed_s": round(run.elapsed_s, 3),
        "termination": run.termination,
        "warning_reasons": list(run.warning_reasons),
        "failure_reasons": list(run.failure_reasons),
        "completion": _json_copy(run.completion),
        "resources": _json_copy(run.resources),
        "diagnostic_excerpt": _bounded_diagnostic(run.diagnostic_excerpt),
    }


def _recipe(run: ImplementationRun) -> dict[str, Any]:
    return {
        "fingerprint": run.recipe_fingerprint,
        "snapshot": _json_copy(run.recipe_snapshot) or None,
    }


def _provenance(run: ImplementationRun, invocation_run_id: str) -> dict[str, Any]:
    return {
        "producer": _json_copy(run.provenance) or None,
        "invocation_run_id": invocation_run_id or None,
        "consumer_run_id": run.cache_consumer_run_id or None,
    }


def _metric_delta(current: Any, baseline: Any) -> dict[str, Any]:
    result = {"current": current, "baseline": baseline, "delta_pct": None}
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        result["unavailable_reason"] = "current metric is unavailable"
    elif isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
        result["unavailable_reason"] = "baseline metric is unavailable"
    elif baseline == 0:
        result["unavailable_reason"] = "baseline metric is zero"
    else:
        result["delta_pct"] = ((current - baseline) / baseline) * 100.0
        result["unavailable_reason"] = None
    return result


def _comparison_errors(
    current: ImplementationRun,
    baseline: ImplementationRun,
    policy: MetricPolicy,
) -> list[str]:
    errors = (
        [f"baseline infrastructure error: {baseline.infra_error}"] if baseline.infra_error else []
    )
    for metric in policy.required_comparison_metrics:
        if current.passed and current.metrics.get(metric) is None:
            errors.append(f"current required metric '{metric}' is unavailable")
        if baseline.metrics.get(metric) is None:
            errors.append(f"baseline required metric '{metric}' is unavailable")
    return errors


def _baseline_payload(run: ImplementationRun, invocation_run_id: str) -> dict[str, Any]:
    artifacts = run.artifacts if isinstance(run.artifacts, dict) else {}
    return {
        "status": _status(run),
        "metrics": _json_copy(run.metrics),
        "conditions": _json_copy(run.conditions),
        "recipe": _recipe(run),
        "provenance": _provenance(run, invocation_run_id),
        "cache": _json_copy(run.cache) or None,
        "artifacts": {"log": artifacts["log"]} if artifacts.get("log") else {},
    }


def _comparison(
    context: ImplementationContext,
    current: ImplementationRun,
    baseline: ImplementationRun,
    policy: MetricPolicy,
) -> tuple[dict[str, Any], list[str]]:
    errors = _comparison_errors(current, baseline, policy)
    deltas = {
        metric: _metric_delta(current.metrics.get(metric), baseline.metrics.get(metric))
        for metric in policy.delta_metrics
    }
    comparison = {
        "requested_ref": context.requested_baseline_ref,
        "resolved_ref": context.resolved_baseline_ref,
        "baseline_target": context.baseline_target or context.target,
        "candidate_target": context.target,
        "basis_valid": not errors,
        "basis_errors": errors,
        "baseline": _baseline_payload(baseline, context.invocation_run_id),
        "deltas": deltas,
    }
    return comparison, errors


def build_implementation_report(
    context: ImplementationContext,
    current_run: ImplementationRun,
    baseline_run: ImplementationRun | None,
    metric_policy: MetricPolicy,
) -> ImplementationReport:
    """Build one canonical report after applying all target policy exactly once."""
    comparison = None
    comparison_errors: list[str] = []
    if baseline_run is not None:
        comparison, comparison_errors = _comparison(
            context, current_run, baseline_run, metric_policy
        )
    grade = "error" if comparison_errors else _run_grade(current_run)
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "flow": context.flow,
            "target": context.target,
            "eda_tool": context.eda_tool,
            "invocation_run_id": context.invocation_run_id or None,
            "timestamp": context.timestamp,
        },
        "status": _status(current_run, grade),
        "metrics": _json_copy(current_run.metrics),
        "conditions": _json_copy(current_run.conditions),
        "recipe": _recipe(current_run),
        "provenance": _provenance(current_run, context.invocation_run_id),
        "comparison": comparison,
        "cache": _json_copy(current_run.cache) or None,
        "artifacts": _json_copy(current_run.artifacts),
    }
    return ImplementationReport(canonical)


def build_implementation_aggregate(
    reports: Mapping[str, ImplementationReport],
    *,
    baseline_ref: str | None = None,
) -> ImplementationAggregate:
    """Build a cross-Flow run summary from policy-resolved target reports."""
    ordered = {target: reports[target].mcp_entry() for target in reports}
    grades = [report.grade for report in reports.values()]
    grade = max(grades, key=_GRADE_SEVERITY.__getitem__) if grades else "error"
    exit_code = 2 if grade == "error" else 1 if grade == "fail" else 0
    detail = {
        "schema_version": SCHEMA_VERSION,
        "targets": list(reports),
        "grade": grade,
        "passed": exit_code == 0,
        "baseline_ref": baseline_ref,
        "results": ordered,
    }
    return ImplementationAggregate(detail=detail, exit_code=exit_code)


def target_report_slug(target: str) -> str:
    """Return a filesystem-safe, collision-resistant Target selector."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", target).strip(".") or "target"
    if safe == target:
        return safe
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def target_report_path(flow: str, target: str, report_dir: Path) -> Path:
    """Return the stable compatibility path for one implementation target."""
    if flow not in {"synth", "fpga"}:
        raise ValueError(f"unsupported implementation flow: {flow}")
    return report_dir / f"{flow}_{target_report_slug(target)}.json"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _published_canonical(
    report: ImplementationReport,
    locations: PublicationLocations,
    invocation_path: Path | None,
    stable_path: Path,
) -> dict[str, Any]:
    canonical = report.canonical
    source_artifacts = canonical.get("artifacts", {})
    published: dict[str, Any] = {
        "report": posix_relpath(invocation_path or stable_path, locations.work_dir)
    }
    if isinstance(source_artifacts, dict):
        dirs = source_artifacts.get("dirs")
        if isinstance(dirs, dict) and dirs:
            published["live_dirs"] = _json_copy(dirs)
        log = source_artifacts.get("log")
        if isinstance(log, str) and log:
            published["log"] = log
    canonical["artifacts"] = published
    return canonical


def _copy_log_snapshot(
    artifacts: dict[str, Any],
    locations: PublicationLocations,
    destination: Path,
) -> None:
    log = artifacts.get("log")
    if not isinstance(log, str) or not log:
        return
    source = locations.work_dir / log
    if not source.is_file():
        artifacts.pop("log", None)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    artifacts["log"] = posix_relpath(destination, locations.work_dir)


def _snapshot_logs(
    payload: dict[str, Any],
    locations: PublicationLocations,
    invocation_path: Path | None,
) -> None:
    if invocation_path is None:
        return
    implementation = payload[ENVELOPE_KEY]
    artifacts = implementation.get("artifacts", {})
    if isinstance(artifacts, dict):
        _copy_log_snapshot(artifacts, locations, invocation_path.with_suffix("") / "run.log")
    comparison = implementation.get("comparison")
    baseline = comparison.get("baseline") if isinstance(comparison, dict) else None
    baseline_artifacts = baseline.get("artifacts") if isinstance(baseline, dict) else None
    if isinstance(baseline_artifacts, dict):
        destination = invocation_path.with_suffix("") / "baseline" / "run.log"
        _copy_log_snapshot(baseline_artifacts, locations, destination)


def publish_implementation_report(
    report: ImplementationReport,
    locations: PublicationLocations,
    legacy_payload: Mapping[str, Any],
) -> PublishedReport:
    """Atomically publish immutable numbered evidence, then its stable alias."""
    if locations.report_dir is None:
        return PublishedReport(None, None, report.envelope(legacy_payload))
    stable = target_report_path(
        report.canonical["identity"]["flow"], report.target, locations.report_dir
    )
    invocation = None
    if locations.invocation_dir is not None:
        invocation = (
            locations.invocation_dir / "targets" / f"{target_report_slug(report.target)}.json"
        )
    canonical = _published_canonical(report, locations, invocation, stable)
    payload = _json_copy(dict(legacy_payload))
    payload[ENVELOPE_KEY] = canonical
    _snapshot_logs(payload, locations, invocation)
    if invocation is not None:
        _atomic_write_json(invocation, payload)
    _atomic_write_json(stable, payload)
    return PublishedReport(stable, invocation, payload)


def publish_implementation_progress(
    progress: ImplementationProgress,
    locations: PublicationLocations,
) -> Path | None:
    """Atomically checkpoint the common live implementation-matrix shape."""
    if locations.invocation_dir is None:
        return None
    completed = set(progress.completed_targets)
    payload: dict[str, Any] = {
        "flow": progress.flow,
        "run_id": progress.run_id,
        "timestamp": utc_now_rfc3339(),
        "phase": progress.phase,
        "complete": progress.complete,
        "targets": list(progress.targets),
        "completed_targets": list(progress.completed_targets),
        "pending_targets": [target for target in progress.targets if target not in completed],
        "baseline_completed_targets": list(progress.baseline_completed_targets),
        ENVELOPE_KEY: {
            "schema_version": SCHEMA_VERSION,
            "results": {target: report.mcp_entry() for target, report in progress.reports.items()},
        },
    }
    if progress.baseline_ref:
        payload["baseline_ref"] = progress.baseline_ref
    path = locations.invocation_dir / "progress.json"
    _atomic_write_json(path, payload)
    return path
