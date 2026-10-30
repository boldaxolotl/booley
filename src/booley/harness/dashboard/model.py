"""Coherent observational Dashboard data. All filesystem access stays outside UI code."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from booley.config.goals import quiet_after
from booley.core.boundary import as_dict, require_dict
from booley.criteria.evidence_ledger import validated_evidence_records
from booley.criteria.presentation import (
    CriterionPresentation,
    implementation_baseline_comparison,
)
from booley.goals.binding import GoalBindingError, GoalRunBinding
from booley.goals.diagnostics import is_unnamable_error, unavailable_diagnostic
from booley.goals.format import test_run_lines
from booley.goals.lifecycle import read_sealed_artifact
from booley.goals.model import OCCUPYING_STATES, GoalRecord, GoalSpec
from booley.goals.naming import apply_names, optional_names, read_names
from booley.goals.paths import REVIEW_PACKAGE_FILE, record_paths
from booley.goals.presentation import UNAVAILABLE_GOAL, goal_presentation
from booley.goals.proposals import digest
from booley.goals.recorder import GOAL_SCOPE
from booley.goals.review_package import original_observations
from booley.goals.state_store import load_goal_state
from booley.goals.status import GoalStatus, GoalStatusView, build_status
from booley.goals.store import GoalStore
from booley.harness import upgrade_review
from booley.harness.dashboard.jobs import JobReader, JobSnapshot, target_arg
from booley.harness.dashboard.resources import ProcessSampler, Resources, ResourceSampler
from booley.mcp.session_registry import SessionRegistry, SessionRow, namespace
from booley.review.goal_package import GoalCompletionPackage
from booley.runtime import health, health_presentation, job_slots
from booley.runtime.artifact_paths import available_paths
from booley.runtime.handles import HandleStore
from booley.runtime.pid import ProcessIdentity
from booley.runtime.session_activity import Activity, ActivityStore, observed_activity


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
    #: The Goal's frozen Test Runs with their outcomes and rerun commands (ADR 0072).
    test_runs: tuple[str, ...] = ()


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
class HealthLine:
    """Literal health text with optional Doctor semantic severity."""

    text: str
    severity: health_presentation.HealthSeverity | None
    # Trailing part of ``text`` rendered in the attention style instead of the severity style.
    due_suffix: str = ""


@dataclass(frozen=True)
class DashboardSnapshot:
    """Immutable presentation snapshot, detached from every acquired lock."""

    sessions: tuple[SessionRow, ...] = ()
    goals: tuple[GoalView, ...] = ()
    jobs: JobSnapshot = field(default_factory=lambda: JobSnapshot(()))
    resources: Resources = field(default_factory=Resources)
    health: tuple[HealthLine, ...] = ()
    diagnostics: tuple[str, ...] = ()
    observed_at: float = 0
    # Reported activity by session key; a session without an entry is unknown.
    activity: Mapping[str, Activity] = field(default_factory=dict)
    session_handles: Mapping[str, str] = field(default_factory=dict)


def _live_baseline_comparison(goal: GoalStatus, entry: Any) -> tuple[str, ...]:
    """SYNTH/FPGA baseline line for a live Goal, silent where Status reports stale evidence."""
    # entry.stale also covers a category reset, which Status reports as "unmet" with "?".
    if entry is None or entry.stale or goal.status == "stale":
        return ()
    comparison = implementation_baseline_comparison(goal.key, entry.params, entry.detail)
    return (comparison,) if comparison else ()


def _terminal_test_runs(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Baseline lines recorded in a frozen package row: SIM summary and SYNTH/FPGA comparison."""
    lines = []
    if "simulation_baseline" in row:
        lines.append("Baseline -> final: " + row["simulation_baseline"]["summary"])
    comparison = implementation_baseline_comparison(
        row["key"],
        as_dict(row["spec"], default={}).get("params") or {},
        as_dict(row["selected_observation"].get("detail"), default={}),
    )
    if comparison:
        lines.append(comparison)
    return tuple(lines)


def _terminal(store: GoalStore, record: GoalRecord) -> GoalView:
    root = record_paths(store.booley_dir, record.id).root
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
    names = _terminal_names(packages[0].parent, root, record)
    details = tuple(
        GoalDetail(
            row["key"],
            "passing",
            _terminal_presentation(row, names),
            row["selected_observation"],
            tuple(row["original_observations"]),
            test_runs=_terminal_test_runs(row),
            artifacts=available_paths(
                row["selected_observation"], (Path(record.worktree_path), store.booley_dir)
            ),
        )
        for row in data["goals"]
    )
    return GoalView(
        record.id,
        record,
        goals=details,
        diagnostic=UNAVAILABLE_GOAL if names is None else "",
        terminal_summary=data["session_summary"],
    )


def _terminal_names(directory: Path, root: Path, record: GoalRecord) -> dict[str, str] | None:
    try:
        if any(
            (directory / name).exists()
            for name in ("goal-names.json", "goal-names.authority.json")
        ):
            return _completion_names(directory, record)
        return optional_names(root)
    except (OSError, ValueError):
        return None


def _terminal_presentation(
    row: dict[str, Any], names: dict[str, str] | None
) -> CriterionPresentation:
    if names is None:
        return CriterionPresentation(UNAVAILABLE_GOAL, "", "", "", "")
    try:
        spec = apply_names(GoalSpec.from_json(row["spec"], where="completion Goal"), names)
    except ValueError:
        return CriterionPresentation(UNAVAILABLE_GOAL, "", "", "", "")
    return goal_presentation(
        spec, {"params": spec.params, "detail": row["selected_observation"]["detail"]}
    ).criterion()


def _completion_names(directory: Path, record: GoalRecord) -> dict[str, str]:
    """Validate the independently sealed display snapshot before using its subjects."""
    names = read_names(directory)
    if not (directory / "goal-names.json").is_file():
        raise ValueError(UNAVAILABLE_GOAL)
    request = require_dict(
        json.loads((directory / "request.json").read_bytes()), field="Goal naming request"
    )
    request_digest = request.pop("payload_digest", None)
    if (
        request_digest != digest(request)
        or request.get("record_id") != record.id
        or request.get("operation_id") != record.finish_operation
        or request.get("worktree") != record.worktree.to_json()
    ):
        raise ValueError(UNAVAILABLE_GOAL)
    if read_sealed_artifact(directory, "goal-names", request, record) != names:
        raise ValueError(UNAVAILABLE_GOAL)
    return names


def project_goal(store: GoalStore, record: GoalRecord) -> GoalView:
    """Read under an existing lock only; no creation, repair or mixed apply projection."""
    with store.record_lock(record.id, existing_only=True):
        current = store.load(record.id)
        if current.state not in OCCUPYING_STATES:
            return _terminal(store, current)
        from booley.flows.sim.unpublished_results import read_unpublished_results

        status = build_status(
            store, current, observational=True, campaign_reader=read_unpublished_results
        )
        if not Path(current.worktree_path).is_dir():
            return GoalView(current.id, current, status, diagnostic=status.warning)
        if status.interrupted_applies:
            return GoalView(
                current.id, current, status, diagnostic="Interrupted apply: recovery required"
            )
        state = load_goal_state(store, current)
        logs = record_paths(store.booley_dir, current.id).logs_dir
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
                    goal_presentation(by_key[goal.key].spec, facts).criterion(),
                    selected.get(goal.key),
                    tuple(
                        original_observations(
                            selected[goal.key], {row["sequence"]: row for row in evidence}, current
                        )
                    )
                    if goal.key in selected
                    else (),
                    test_runs=(
                        *test_run_lines(goal),
                        *_live_baseline_comparison(goal, entry),
                    ),
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
                goal.evidence or {}, (Path(record.worktree_path), store.booley_dir)
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
        except (ValueError, GoalBindingError):
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


def health_lines(root: Path, booley_dir: Path) -> tuple[HealthLine, ...]:
    """Non-consuming Doctor freshness and typed read-only upgrade-review projection."""
    compact = health_presentation.count_line(health.read_health(root))
    lines = [HealthLine(compact.text, compact.severity, compact.due_suffix)]
    review = upgrade_review.read_status(booley_dir)
    if review.condition not in {
        upgrade_review.ReviewCondition.CURRENT,
        upgrade_review.ReviewCondition.UNAVAILABLE,
    }:
        lines.append(
            HealthLine(
                f"Upgrade review {review.condition}: {review.pending_target or review.diagnostic}",
                None,
            )
        )
    return tuple(lines)


def _unavailable_view(record: GoalRecord, error: BaseException) -> GoalView:
    diagnostic = (
        unavailable_diagnostic(error) if is_unnamable_error(error) else f"unavailable: {error}"
    )
    return GoalView(record.id, record, diagnostic=diagnostic)


class DashboardReader:
    """Bounded record-lock acquisitions and independent failures across record rows."""

    def __init__(self, root: Path, booley_dir: Path) -> None:
        self.root = root
        self.booley_dir = booley_dir
        self.resources = ResourceSampler()
        self.processes = ProcessSampler()
        self.jobs = JobReader(booley_dir, slots_root=job_slots.slots_dir(booley_dir))

    def read(self) -> DashboardSnapshot:
        """Read one snapshot without retaining locks or touching persistent state."""
        now = time.time()
        sessions = SessionRegistry(
            self.booley_dir, quiet_after=quiet_after(self.booley_dir)
        ).visible_snapshot(now=now, scope=namespace())
        handles, handle_diagnostics = HandleStore(self.booley_dir).snapshot()
        store = GoalStore(self.booley_dir, lock_timeout_s=0)
        scan = store.list_records()
        goals = []
        for record in scan.records:
            try:
                goals.append(project_goal(store, record))
            except (OSError, ValueError, RuntimeError) as exc:
                goals.append(_unavailable_view(record, exc))
        goals.extend(
            GoalView(str(item), diagnostic="corrupt Goal Record") for item in scan.corrupt
        )
        resources = self.resources.sample(self.root, now=time.monotonic())
        jobs = self.jobs.read(scan.records, session_handles=handles, now=now)
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
            health_lines(self.root, self.booley_dir),
            (*sessions.diagnostics, *handle_diagnostics),
            now,
            self._activity(sessions.rows, now),
            handles,
        )

    def _activity(self, rows: tuple[SessionRow, ...], now: float) -> dict[str, Activity]:
        """Join each visible session with the last lifecycle event its client reported."""
        store = ActivityStore(self.booley_dir)
        return {
            row.attribution.key: observed_activity(store.read(row.attribution.key), now=now)
            for row in rows
        }
