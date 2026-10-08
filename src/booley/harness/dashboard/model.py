"""Coherent observational Dashboard data. All filesystem access stays outside UI code."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from booley.config.goals import quiet_after
from booley.criteria.evidence_ledger import validated_evidence_records
from booley.criteria.presentation import CriterionPresentation, criterion_presentation
from booley.goals.binding import GoalRunBinding
from booley.goals.model import OCCUPYING_STATES, GoalRecord
from booley.goals.paths import REVIEW_PACKAGE_FILE, record_paths
from booley.goals.proposals import digest
from booley.goals.recorder import GOAL_SCOPE
from booley.goals.review_package import original_observations
from booley.goals.state_store import load_goal_state
from booley.goals.status import GoalStatusView, build_status
from booley.goals.store import GoalStore
from booley.harness import auto_doctor, upgrade_review
from booley.harness.dashboard.resources import ProcessSampler, Resources, ResourceSampler
from booley.mcp.session_registry import SessionRegistry, SessionRow, namespace
from booley.review.goal_package import GoalCompletionPackage
from booley.runtime import job_slots
from booley.runtime.artifact_paths import available_paths
from booley.runtime.job_snapshot import JobSnapshot, snapshot_jobs, target_arg
from booley.runtime.pid import ProcessIdentity


@dataclass(frozen=True)
class GoalDetail:
    """Requirement and exact observations; checking never changes evidence status."""

    key: str
    state: str
    presentation: CriterionPresentation
    evidence: dict[str, Any] | None = None
    provenance: tuple[dict[str, Any], ...] = ()
    checking: bool = False
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalView:
    """One isolated record, including corrupt/busy/interrupted observations."""

    id: str
    record: GoalRecord | None = None
    status: GoalStatusView | None = None
    goals: tuple[GoalDetail, ...] = ()
    diagnostic: str = ""
    terminal_summary: str = ""


@dataclass(frozen=True)
class DashboardSnapshot:
    """Immutable presentation snapshot, detached from every acquired lock."""

    sessions: tuple[SessionRow, ...] = ()
    goals: tuple[GoalView, ...] = ()
    jobs: JobSnapshot = field(default_factory=lambda: JobSnapshot(()))
    resources: Resources = field(default_factory=Resources)
    health: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    observed_at: float = 0


def _terminal(store: GoalStore, record: GoalRecord) -> GoalView:
    root = record_paths(store.project_dir, record.id).root
    packages = (
        []
        if record.finish_operation is None
        else [root / "operations" / record.finish_operation / REVIEW_PACKAGE_FILE]
    )
    if not packages or not packages[0].is_file():
        return GoalView(record.id, record, terminal_summary=record.state.value)
    data = GoalCompletionPackage.from_json(json.loads(packages[0].read_bytes())).to_json()
    if data["record"]["id"] != record.id or (
        record.package_digest and record.package_digest != "sha256:" + digest(data)
    ):
        raise ValueError("completion package differs from frozen Goal facts")
    details = tuple(
        GoalDetail(
            row["key"],
            "passing",
            criterion_presentation(
                row["key"],
                {
                    "params": row["spec"].get("params", {}),
                    "detail": row["selected_observation"]["detail"],
                },
            ),
            row["selected_observation"],
            tuple(row["original_observations"]),
            artifacts=available_paths(
                row["selected_observation"], (Path(record.worktree_path), store.project_dir)
            ),
        )
        for row in data["goals"]
    )
    return GoalView(record.id, record, goals=details, terminal_summary=data["session_summary"])


def project_goal(store: GoalStore, record: GoalRecord) -> GoalView:
    """Read under an existing lock only; no creation, repair or mixed apply projection."""
    with store.record_lock(record.id, existing_only=True):
        current = store.load(record.id)
        if current.state not in OCCUPYING_STATES:
            return _terminal(store, current)
        status = build_status(store, current, observational=True)
        if not Path(current.worktree_path).is_dir():
            return GoalView(current.id, current, status, diagnostic=status.warning)
        if status.interrupted_applies:
            return GoalView(
                current.id, current, status, diagnostic="Interrupted apply: recovery required"
            )
        state = load_goal_state(store, current)
        logs = record_paths(store.project_dir, current.id).logs_dir
        evidence = validated_evidence_records(GOAL_SCOPE, logs, state, {})
        selected = {row["criterion"]: row for row in evidence if row["role"] == "candidate"}
        by_key = {goal.spec.key: goal for goal in current.goals}
        details = []
        for goal in status.goals:
            entry = state.criteria.get(goal.key)
            facts = {"params": by_key[goal.key].spec.params} if entry is None else entry.to_dict()
            label = (
                "passing"
                if goal.status == "met"
                else (
                    "needs recheck"
                    if goal.status == "stale"
                    else ("failing" if entry is not None and entry.ever_failed else "not yet run")
                )
            )
            details.append(
                GoalDetail(
                    goal.key,
                    label,
                    criterion_presentation(goal.key, facts),
                    selected.get(goal.key),
                    tuple(
                        original_observations(
                            selected[goal.key], {row["sequence"]: row for row in evidence}
                        )
                    )
                    if goal.key in selected
                    else (),
                )
            )
        return GoalView(current.id, current, status, _goal_artifacts(details, current, store))


def _goal_artifacts(
    details: list[GoalDetail], record: GoalRecord, store: GoalStore
) -> tuple[GoalDetail, ...]:
    return tuple(
        replace(
            goal,
            artifacts=available_paths(
                goal.evidence or {}, (Path(record.worktree_path), store.project_dir)
            ),
        )
        for goal in details
    )


def checking_goals(view: GoalView, jobs: JobSnapshot) -> GoalView:
    """Explicit matching running producer marks checking without changing its evidence verdict."""
    if view.record is None:
        return view
    checking = set()
    for job in jobs.jobs:
        if job.state != "running" or job.record.binding is None:
            continue
        try:
            binding = GoalRunBinding.from_json(job.record.binding)
        except ValueError:
            continue
        if binding.record_id != view.id or binding.record_revision != view.record.revision:
            continue
        target = target_arg(job.record.argv)
        for goal in view.record.goals:
            if (
                goal.spec.family.value == job.record.endpoint
                and goal.spec.target == target
                and dict(binding.spec_revisions).get(goal.spec.key) == goal.spec_revision
            ):
                checking.add(goal.spec.key)
    return replace(
        view, goals=tuple(replace(goal, checking=goal.key in checking) for goal in view.goals)
    )


def health_snapshot(root: Path, project_dir: Path) -> tuple[str, ...]:
    """Non-consuming Doctor freshness and typed read-only upgrade-review projection."""
    lines = []
    report = auto_doctor.load_report(root)
    if report is not None and any(auto_doctor.issue_counts(report)):
        due = auto_doctor.due_reason(root)
        lines.append(
            auto_doctor.current_summary(root) + (f" · stale: {due}" if due else " · current")
        )
    elif report is None:
        lines.append("Doctor health unavailable: no readable automatic result")
    elif due := auto_doctor.due_reason(root):
        lines.append("Doctor health stale: " + due)
    review = upgrade_review.read_status(project_dir)
    if review.condition not in {
        upgrade_review.ReviewCondition.CURRENT,
        upgrade_review.ReviewCondition.UNAVAILABLE,
    }:
        lines.append(
            f"Upgrade review {review.condition}: {review.pending_target or review.diagnostic}"
        )
    return tuple(lines)


class DashboardReader:
    """Bounded record-lock acquisitions and independent failures across record rows."""

    def __init__(self, root: Path, project_dir: Path) -> None:
        self.root = root
        self.project_dir = project_dir
        self.resources = ResourceSampler()
        self.processes = ProcessSampler()

    def read(self) -> DashboardSnapshot:
        """Read one snapshot without retaining locks or touching persistent state."""
        now = time.time()
        sessions = SessionRegistry(
            self.project_dir, quiet_after=quiet_after(self.project_dir)
        ).visible_snapshot(now=now, scope=namespace())
        store = GoalStore(self.project_dir, lock_timeout_s=0)
        scan = store.list_records()
        goals = []
        for record in scan.records:
            try:
                goals.append(project_goal(store, record))
            except (OSError, ValueError, RuntimeError) as exc:
                goals.append(GoalView(record.id, record, diagnostic=f"unavailable: {exc}"))
        goals.extend(
            GoalView(str(item), diagnostic="corrupt Goal Record") for item in scan.corrupt
        )
        resources = self.resources.sample(self.root, now=time.monotonic())
        jobs = snapshot_jobs(self.project_dir, slots_root=job_slots.slots_dir(self.project_dir))
        measured = []
        for job in jobs.jobs:
            identity = ProcessIdentity.from_payload(job.record.process_identity)
            projected = job
            if identity is not None and job.state == "running":
                sample = self.processes.sample(identity, now=time.monotonic())
                projected = replace(
                    job,
                    cpu_percent=sample.cpu_percent,
                    memory=sample.memory,
                    peak_memory=sample.peak_memory,
                )
            measured.append(projected)
        return DashboardSnapshot(
            sessions.rows,
            tuple(checking_goals(goal, jobs) for goal in goals),
            replace(jobs, jobs=tuple(measured)),
            resources,
            health_snapshot(self.root, self.project_dir),
            sessions.diagnostics,
            now,
        )
