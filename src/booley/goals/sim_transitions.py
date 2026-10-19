"""Report baseline-to-current Test Run transitions without deciding any Goal."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from booley.flows.test_runs import TestRun
from booley.goals.freshness import GoalFreshnessResolvers
from booley.goals.model import GoalRecord, RecordedGoal
from booley.goals.sim_baseline_store import (
    entry_inputs,
    read_document,
    request_identity,
    request_path,
    validate_results,
)
from booley.goals.test_run_evidence import (
    HistoryScope,
    freshness_stamp,
    matching_rows,
    run_outcomes,
)
from booley.goals.test_run_freezing import TargetPlanError, TargetPlans
from booley.runtime import job_records
from booley.runtime.pid import is_pid_alive


def transition_for(
    run: TestRun, baseline: str, current: str, *, seen_failing: bool = False, reason: str = ""
) -> dict[str, Any]:
    """One exact Test Run's report; absence and unavailable evidence stay distinct."""
    final = {"passed": "pass", "failed": "fail"}.get(current)
    if final is None:
        transition = f"{baseline} -> current unavailable"
    elif baseline == "new":
        start = (
            f"new ({'seen failing' if seen_failing else 'never seen failing'})"
            if final == "pass"
            else "new"
        )
        transition = f"{start} -> {final}"
    else:
        transition = f"{baseline} -> {final}"
    return {
        "name": run.name,
        "fingerprint": run.fingerprint,
        "baseline": baseline,
        "current": current,
        "transition": transition,
        "reason": reason,
    }


def transition_summary(rows: Sequence[Mapping[str, Any]]) -> str:
    """One shared summary, naming every outcome other than pass -> pass."""
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        reason = f": {row['reason']}" if row.get("reason") else ""
        groups[str(row["transition"])].append(str(row["name"]) + reason)
    return (
        ", ".join(
            f"{len(names)} {label}" + (f" ({'; '.join(names)})" if label != "pass -> pass" else "")
            for label, names in groups.items()
        )
        or "baseline pending (Test Runs unresolved)"
    )


def seen_failing(
    record: GoalRecord,
    goal: RecordedGoal,
    run: TestRun,
    current: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> bool:
    """Earlier graded failures of this session, across revisions, at different contents."""
    stamp = freshness_stamp(goal.spec.key, current["detail"])
    if stamp is None:
        return False
    for row in history:
        identity = row.get("goal_identity", {})
        old = freshness_stamp(goal.spec.key, row["detail"])
        if old is None or old == stamp:
            continue
        target = row["detail"].get("_source_fingerprint", {}).get("target")
        if (
            row["role"] == "candidate"
            and row["sequence"] < current["sequence"]
            and identity.get("record_id") == record.id
            and target == goal.spec.target
            and any(
                item.get("grade") == "fail" and item.get("functional_failure") is True
                for item in matching_rows(row["detail"], (run,))
            )
        ):
            return True
    return False


def baseline_observations(project: Path, record: GoalRecord, goal: RecordedGoal) -> dict[str, Any]:
    """Read an independent request; dead Jobs never remain pending indefinitely."""
    try:
        inputs = entry_inputs(project, record)
        identity = request_identity(record, goal, inputs)
        path = request_path(project, identity)
        if not path.exists():
            return {"status": "pending", "reason": "baseline has not been scheduled yet"}
        raw = read_document(path)
        if raw.get("identity") != identity:
            raise ValueError("baseline request identity differs")
        validate_results(project, raw)
        if raw.get("status") == "pending":
            job = job_records.read_record(raw["job_id"], Path(raw["jobs_root"]))
            if job is None or not job_records.is_active(job, is_pid_alive):
                return {
                    "status": "unavailable",
                    "reason": "baseline Job interrupted or unavailable",
                }
        return raw
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "unavailable", "reason": f"baseline evidence cannot be read: {exc}"}


def goal_transitions(
    project: Path,
    record: GoalRecord,
    goal: RecordedGoal,
    current: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]],
    root: Path,
    resolvers: GoalFreshnessResolvers,
) -> list[dict[str, Any]]:
    """Compare only exact Test Runs observed at today's dependent contents."""
    resolution = record.test_run_resolution(goal.spec.key)
    if resolution is None:
        return []
    baseline = baseline_observations(project, record, goal)
    finals, reason = _current_outcomes(record, goal, current, history, root, resolvers)
    indexed = {(row["name"], row["fingerprint"]): row for row in baseline.get("results", [])}
    target_identity = _target_identity(root, goal)
    rows: list[dict[str, Any]] = []
    for run in resolution.test_runs:
        before = indexed.get((run.name, run.fingerprint), {})
        start = before.get("grade", baseline.get("status", "unavailable"))
        note = (
            before.get("reason", baseline.get("reason", ""))
            if start not in {"pass", "fail", "new"}
            else ""
        )
        proof = before.get("proof", {})
        if (
            proof.get("target_identity") is not None
            and proof["target_identity"] != target_identity
        ):
            start, note = "not comparable", "Target identity differs from baseline"
        final = finals.get((run.name, run.fingerprint), "missing")
        seen = current is not None and seen_failing(record, goal, run, current, history)
        row = transition_for(
            run,
            start,
            final,
            seen_failing=seen,
            reason=note if start in {"unavailable", "not comparable"} and note else reason or note,
        )
        row["baseline_proof"] = before.get("proof")
        rows.append(row)
    return rows


def _target_identity(root: Path, goal: RecordedGoal) -> str | None:
    assert goal.spec.target is not None
    try:
        return TargetPlans(root).identity(goal.spec.target)
    except (TargetPlanError, OSError, ValueError, RuntimeError):
        return None


def _current_outcomes(
    record: GoalRecord,
    goal: RecordedGoal,
    current: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]],
    root: Path,
    resolvers: GoalFreshnessResolvers,
) -> tuple[dict[tuple[str, str], str], str]:
    if current is None:
        return {}, "no current Test Run evidence"
    identity = current["goal_identity"]
    if (
        identity["record_id"] != record.id
        or identity["spec_revisions"].get(goal.spec.key) != goal.spec_revision
    ):
        return {}, "current evidence is from another Goal revision"
    try:
        detail = {
            "_source_fingerprint": {
                "fingerprint": resolvers.fingerprint(root, target=goal.spec.target)
            }
        }
        stamp = freshness_stamp(goal.spec.key, detail)
        if stamp is None or freshness_stamp(goal.spec.key, current["detail"]) != stamp:
            return {}, "current evidence is stale or has no dependent-contents fingerprint"
        resolution = record.test_run_resolution(goal.spec.key)
        runs = run_outcomes(
            HistoryScope.of(record, goal),
            () if resolution is None else resolution.test_runs,
            current["detail"],
            history,
            before=current["sequence"],
        )
        return {(run["name"], run["fingerprint"]): run["outcome"] for run in runs}, ""
    except (OSError, ValueError, RuntimeError) as exc:
        return {}, f"current evidence is unavailable: {exc}"
