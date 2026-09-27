"""Project aggregate Coverage Campaign evidence onto one Criterion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from booley.criteria.state import CriterionEntry
from booley.flows.execution_persistence import AcceptanceRecordingError


def project_coverage_criterion(
    entry: CriterionEntry,
    evaluation: Mapping[str, object],
    detail: Mapping[str, Any],
    *,
    atomic: bool,
) -> tuple[bool, dict[str, Any]]:
    """Return one fail-closed verdict and its immutable campaign detail."""
    projected = dict(detail)
    if not atomic:
        return evaluation.get("status") == "pass", projected
    metric = _authored_metric(entry)
    verdicts = _metric_verdicts(evaluation)
    projected["criterion_metric"] = metric
    if metric not in verdicts:
        if evaluation.get("status") in {"pass", "fail"}:
            raise AcceptanceRecordingError(
                f"scored Coverage Campaign omits authored metric {metric!r}"
            )
        return False, projected
    suite = evaluation.get("suite")
    suite_matches = isinstance(suite, Mapping) and suite.get("status") == "match"
    diagnostics = evaluation.get("diagnostics")
    return bool(suite_matches and not diagnostics and verdicts[metric] == "pass"), projected


def _authored_metric(entry: CriterionEntry) -> str:
    metrics = entry.params.get("metrics")
    if not isinstance(metrics, Mapping) or len(metrics) != 1:
        raise AcceptanceRecordingError(
            "atomic Coverage Criterion requires exactly one authored metric"
        )
    metric = next(iter(metrics))
    if not isinstance(metric, str) or not metric:
        raise AcceptanceRecordingError("atomic Coverage Criterion metric must be named")
    return metric


def _metric_verdicts(evaluation: Mapping[str, object]) -> dict[str, object]:
    rows = evaluation.get("metrics", ())
    if not isinstance(rows, tuple | list):
        raise AcceptanceRecordingError("Coverage Campaign metric evidence is malformed")
    verdicts: dict[str, object] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise AcceptanceRecordingError("Coverage Campaign metric evidence is malformed")
        metric = row.get("metric")
        if not isinstance(metric, str) or not metric:
            raise AcceptanceRecordingError("Coverage Campaign metric evidence is malformed")
        if metric in verdicts:
            raise AcceptanceRecordingError(
                f"Coverage Campaign repeats metric evidence for {metric!r}"
            )
        verdicts[metric] = row.get("verdict")
    return verdicts


__all__ = ["project_coverage_criterion"]
