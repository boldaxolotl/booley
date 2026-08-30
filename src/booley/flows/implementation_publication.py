"""Durable publication for canonical implementation reports and progress."""

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
from typing import Any

from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339

from .implementation_report import (
    ENVELOPE_KEY,
    SCHEMA_VERSION,
    ImplementationReport,
)


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
    publisher: ImplementationPublisher,
    invocation_path: Path | None,
    stable_path: Path,
) -> dict[str, Any]:
    canonical = report.canonical
    source_artifacts = canonical.get("artifacts", {})
    published: dict[str, Any] = {
        "report": posix_relpath(invocation_path or stable_path, publisher.work_dir)
    }
    if isinstance(source_artifacts, dict):
        dirs = source_artifacts.get("dirs")
        if isinstance(dirs, dict) and dirs:
            published["live_dirs"] = copy.deepcopy(dirs)
        log = source_artifacts.get("log")
        if isinstance(log, str) and log:
            published["log"] = log
    canonical["artifacts"] = published
    return canonical


def _copy_log_snapshot(
    artifacts: dict[str, Any],
    publisher: ImplementationPublisher,
    destination: Path,
) -> None:
    log = artifacts.get("log")
    if not isinstance(log, str) or not log:
        return
    source = publisher.work_dir / log
    if not source.is_file():
        artifacts.pop("log", None)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    artifacts["log"] = posix_relpath(destination, publisher.work_dir)


def _snapshot_logs(
    payload: dict[str, Any],
    publisher: ImplementationPublisher,
    invocation_path: Path | None,
) -> None:
    if invocation_path is None:
        return
    implementation = payload[ENVELOPE_KEY]
    artifacts = implementation.get("artifacts", {})
    if isinstance(artifacts, dict):
        _copy_log_snapshot(artifacts, publisher, invocation_path.with_suffix("") / "run.log")
    comparison = implementation.get("comparison")
    baseline = comparison.get("baseline") if isinstance(comparison, dict) else None
    baseline_artifacts = baseline.get("artifacts") if isinstance(baseline, dict) else None
    if isinstance(baseline_artifacts, dict):
        destination = invocation_path.with_suffix("") / "baseline" / "run.log"
        _copy_log_snapshot(baseline_artifacts, publisher, destination)


@dataclass(frozen=True)
class ImplementationPublisher:
    """Publish target evidence and progress behind one filesystem interface."""

    work_dir: Path
    report_dir: Path | None
    invocation_dir: Path | None

    def publish_report(
        self,
        report: ImplementationReport,
        legacy_payload: Mapping[str, Any],
    ) -> PublishedReport:
        """Atomically publish immutable evidence, then its stable alias."""
        if self.report_dir is None:
            return PublishedReport(None, None, report.envelope(legacy_payload))
        flow = str(report.canonical["identity"]["flow"])
        stable = target_report_path(flow, report.target, self.report_dir)
        invocation = self._invocation_path(report.target)
        canonical = _published_canonical(report, self, invocation, stable)
        payload = copy.deepcopy(dict(legacy_payload))
        payload[ENVELOPE_KEY] = canonical
        _snapshot_logs(payload, self, invocation)
        if invocation is not None:
            _atomic_write_json(invocation, payload)
        _atomic_write_json(stable, payload)
        return PublishedReport(stable, invocation, payload)

    def publish_progress(self, progress: ImplementationProgress) -> Path | None:
        """Atomically checkpoint the common live implementation-matrix shape."""
        if self.invocation_dir is None:
            return None
        completed = set(progress.completed_targets)
        payload = self._progress_payload(progress, completed)
        path = self.invocation_dir / "progress.json"
        _atomic_write_json(path, payload)
        return path

    def _invocation_path(self, target: str) -> Path | None:
        if self.invocation_dir is None:
            return None
        return self.invocation_dir / "targets" / f"{target_report_slug(target)}.json"

    @staticmethod
    def _progress_payload(
        progress: ImplementationProgress,
        completed: set[str],
    ) -> dict[str, Any]:
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
                "results": {
                    target: report.mcp_entry() for target, report in progress.reports.items()
                },
            },
        }
        if progress.baseline_ref:
            payload["baseline_ref"] = progress.baseline_ref
        return payload
