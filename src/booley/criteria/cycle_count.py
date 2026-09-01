"""Neutral Cycle Count evidence used by state and simulation adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from booley.flows.recipe_evidence import BASELINE_REF_PARAM

PROVENANCE_LIMITATION = (
    "Booley fingerprints declared Target inputs and captured controls. It cannot prove "
    "arbitrary transitive files opened by hooks, generated or gitignored inputs, ambient "
    "environment values, or external toolchain state unless they are declared on a captured surface."
)


def workload_changes(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return deterministic path-oriented changes between declared inputs."""
    before = _inputs_by_path(baseline)
    after = _inputs_by_path(current)
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old is not None and new is not None and old.get("sha256") == new.get("sha256"):
            continue
        changes.append(_workload_change(path, old, new))
    return changes


def _inputs_by_path(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get("path")): row
        for row in snapshot.get("inputs", [])
        if isinstance(row, Mapping) and row.get("path")
    }


def _workload_change(
    path: str,
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any] | None,
) -> dict[str, Any]:
    status = "added" if old is None else "deleted" if new is None else "modified"
    return {
        "path": path,
        "role": (new or old or {}).get("role", "workload"),
        "status": status,
        "baseline_sha256": old.get("sha256") if old is not None else None,
        "current_sha256": new.get("sha256") if new is not None else None,
    }


def build_cycle_comparison(
    params: Mapping[str, Any],
    detail: Mapping[str, Any],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the typed review block for one per-test Cycle Count Criterion."""
    baseline = detail.get("baseline_cycles")
    current = detail.get("cycles")
    delta = _cycle_delta(baseline, current)
    baseline_snapshot = detail.get("baseline_workload_snapshot")
    current_snapshot = detail.get("workload_snapshot")
    changes = _snapshot_changes(baseline_snapshot, current_snapshot)
    return {
        "target": params.get("target"),
        "test": params.get("test"),
        "baseline_ref": params.get(BASELINE_REF_PARAM),
        "baseline_cycles": baseline,
        "cycles": current,
        "delta_cycles": delta,
        "delta_pct": delta / baseline * 100 if delta is not None and baseline else None,
        "checks": list(checks),
        "baseline_workload_fingerprint": _snapshot_fingerprint(baseline_snapshot),
        "workload_fingerprint": _snapshot_fingerprint(current_snapshot),
        "workload_changed": bool(changes),
        "known_input_changes": changes,
        "provenance_limitation": PROVENANCE_LIMITATION,
    }


def _cycle_delta(baseline: Any, current: Any) -> int | None:
    current_valid = isinstance(current, int) and not isinstance(current, bool)
    baseline_valid = isinstance(baseline, int) and not isinstance(baseline, bool)
    return current - baseline if current_valid and baseline_valid else None


def _snapshot_changes(baseline: Any, current: Any) -> list[dict[str, Any]]:
    if not isinstance(baseline, Mapping) or not isinstance(current, Mapping):
        return []
    return workload_changes(baseline, current)


def _snapshot_fingerprint(snapshot: Any) -> Any:
    return snapshot.get("fingerprint") if isinstance(snapshot, Mapping) else None
