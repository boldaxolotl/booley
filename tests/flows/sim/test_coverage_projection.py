"""Atomic Coverage Criterion projection from aggregate campaign evidence."""

from __future__ import annotations

import pytest

from booley.criteria.state import CriterionEntry
from booley.flows.execution_persistence import AcceptanceRecordingError
from booley.flows.sim.coverage_projection import project_coverage_criterion


def _evaluation(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "fail",
        "suite": {"status": "match"},
        "diagnostics": [],
        "metrics": [
            {"metric": "line", "verdict": "pass"},
            {"metric": "branch", "verdict": "fail"},
        ],
    }
    value.update(changes)
    return value


def _entry(*metrics: str) -> CriterionEntry:
    return CriterionEntry(params={"metrics": {metric: {"min_pct": 50} for metric in metrics}})


def test_atomic_projection_uses_its_metric_and_preserves_aggregate_detail() -> None:
    evaluation = _evaluation()
    met, detail = project_coverage_criterion(
        _entry("line"), evaluation, {"evaluation": evaluation}, atomic=True
    )

    assert met is True
    assert detail == {"evaluation": evaluation, "criterion_metric": "line"}


@pytest.mark.parametrize(
    "changes",
    [
        {"suite": {"status": "mismatch"}},
        {"diagnostics": [{"code": "blocked"}]},
    ],
)
def test_atomic_projection_fails_closed_for_suite_or_diagnostics(changes) -> None:
    met, _detail = project_coverage_criterion(
        _entry("line"), _evaluation(**changes), {}, atomic=True
    )

    assert met is False


@pytest.mark.parametrize("metrics", [(), ("line", "branch")])
def test_atomic_projection_rejects_malformed_authored_metrics(metrics: tuple[str, ...]) -> None:
    with pytest.raises(AcceptanceRecordingError, match="exactly one"):
        project_coverage_criterion(_entry(*metrics), _evaluation(), {}, atomic=True)


def test_atomic_projection_rejects_missing_scored_metric() -> None:
    with pytest.raises(AcceptanceRecordingError, match="omits authored metric"):
        project_coverage_criterion(_entry("expression"), _evaluation(), {}, atomic=True)


def test_atomic_projection_rejects_duplicate_metric_rows() -> None:
    evaluation = _evaluation(
        metrics=[
            {"metric": "line", "verdict": "pass"},
            {"metric": "line", "verdict": "fail"},
        ]
    )

    with pytest.raises(AcceptanceRecordingError, match="repeats metric"):
        project_coverage_criterion(_entry("line"), evaluation, {}, atomic=True)


def test_blocked_projection_leaves_missing_atomic_metric_unmet() -> None:
    evaluation = _evaluation(status="blocked", metrics=[])
    met, detail = project_coverage_criterion(_entry("line"), evaluation, {}, atomic=True)

    assert met is False
    assert detail["criterion_metric"] == "line"


@pytest.mark.parametrize(("status", "expected"), [("pass", True), ("fail", False)])
def test_direct_non_atomic_projection_keeps_aggregate_fallback(
    status: str, expected: bool
) -> None:
    met, detail = project_coverage_criterion(
        CriterionEntry(), _evaluation(status=status), {"campaign": "coverage.json"}, atomic=False
    )

    assert met is expected
    assert detail == {"campaign": "coverage.json"}
