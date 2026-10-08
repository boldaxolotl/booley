"""Namespace-aware Doctor warnings and existing-lock read-only Goal projections."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from booley.config.goals import quiet_after
from booley.goals.model import GoalRecord, GoalState, WorktreeIdentity
from booley.goals.paths import record_paths
from booley.goals.status import GoalStatusView
from booley.goals.store import GoalStore
from booley.harness.dashboard import doctor, model
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime.worktrees import WorktreeEntry

SCOPE = "pid:[17]"
WT = WorktreeIdentity("00000000-0000-4000-8000-000000000001", "main")


def record(path):
    return GoalRecord(
        "fixture-20261008T100000Z",
        GoalState.ACTIVE,
        WT,
        str(path),
        "goal/fixture",
        "refs/heads/main",
        "a" * 40,
        "2026-10-08T10:00:00Z",
        revision=1,
    )


@pytest.mark.parametrize("value", [True, "2h", 0, 59, 604801, float("inf"), float("nan")])
def test_invalid_quiet_after(tmp_path, value):
    (tmp_path / "booley.toml").write_text(
        "[goals]\nquiet_after = "
        + json.dumps(value)
        .replace("true", "true")
        .replace("Infinity", "inf")
        .replace("NaN", "nan")
    )
    with pytest.raises(ValueError):
        quiet_after(tmp_path)


def test_default_and_seconds_quiet_after(tmp_path):
    assert quiet_after(tmp_path) == 7200
    (tmp_path / "booley.toml").write_text("[goals]\nquiet_after = 60")
    assert quiet_after(tmp_path) == 60


def test_host_never_observes_colliding_sandbox_pid_or_starts_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "namespace", lambda *_: pytest.fail("host proc inspection"))
    monkeypatch.setattr(
        doctor, "list_worktrees", lambda *_: pytest.fail("host Sandbox worktree inspection")
    )
    result = doctor.inspect_presence(tmp_path, tmp_path, inside_sandbox=False)
    assert result.warnings == ()
    assert "host" in result.unavailable


@pytest.mark.parametrize(
    "kind,fresh,expected",
    [("thread", True, 0), ("thread", False, 1), ("worktree", True, 0), ("worktree", False, 1)],
)
def test_recent_calls_not_activity_and_stable_warning_subjects(
    tmp_path, monkeypatch, kind, fresh, expected
):
    rec = record(tmp_path)
    fake_store = SimpleNamespace(
        list_active=lambda: SimpleNamespace(records=(rec,)), identify_worktree=lambda _: WT
    )
    monkeypatch.setattr(doctor, "GoalStore", lambda _: fake_store)
    monkeypatch.setattr(doctor, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(doctor, "list_worktrees", lambda _: (WorktreeEntry(tmp_path),))
    registry = SessionRegistry(tmp_path)
    registry.upsert(
        Attribution("key", kind, str(tmp_path), WT.key, namespace=SCOPE),
        "sim",
        "completed",
        now=100,
    )
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = doctor.inspect_presence(
        tmp_path, tmp_path, inside_sandbox=True, now=101 if fresh else 10000
    )
    assert len(result.warnings) == expected
    if result.warnings:
        assert result.warnings[0].check_id == "goals.quiet-session"
        assert result.warnings[0].subject == rec.id
        assert "abandonment" in str(result.warnings[0])
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_worktree_identity_moved_missing_and_unreadable(tmp_path):
    rec = record(tmp_path / "removed")
    store = SimpleNamespace(identify_worktree=lambda _: WT)
    assert doctor.worktree_presence(store, rec, (WorktreeEntry(tmp_path),)) == "present"
    assert doctor.worktree_presence(store, rec, ()) == "missing"

    def unreadable(_):
        raise OSError("git unreadable")

    store.identify_worktree = unreadable
    assert doctor.worktree_presence(store, rec, (WorktreeEntry(tmp_path),)) == "unreadable"


def test_git_failure_is_unavailable_not_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(
        doctor, "list_worktrees", lambda *_: (_ for _ in ()).throw(OSError("git failed"))
    )
    result = doctor.inspect_presence(tmp_path, tmp_path, inside_sandbox=True)
    assert "unavailable" in result.unavailable
    assert result.warnings == ()


def _persist_record(tmp_path, rec, lock=True):
    paths = record_paths(tmp_path, rec.id)
    paths.root.mkdir(parents=True)
    paths.record_file.write_text(json.dumps(rec.to_json()))
    if lock:
        paths.lock_file.write_bytes(
            b"\0"
        )  # Existing nonempty Windows lock needs no initialization write.
    return paths


def test_observational_missing_lock_never_created(tmp_path):
    rec = record(tmp_path)
    paths = _persist_record(tmp_path, rec, lock=False)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError, match="disappeared"):
        model.project_goal(GoalStore(tmp_path, lock_timeout_s=0), rec)
    assert not paths.lock_file.exists()
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_interrupted_apply_projection_never_recovers_and_is_byte_identical(tmp_path, monkeypatch):
    rec = record(tmp_path)
    _persist_record(tmp_path, rec)
    monkeypatch.setattr(
        model,
        "build_status",
        lambda *_a, **_k: GoalStatusView(rec, (), interrupted_applies=("pending",)),
    )
    monkeypatch.setattr(
        model, "load_goal_state", lambda *_a: pytest.fail("mixed/interrupted state read")
    )
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    view = model.project_goal(GoalStore(tmp_path, lock_timeout_s=0), rec)
    assert "recovery required" in view.diagnostic
    assert view.goals == ()
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_terminal_record_read_only_and_busy_lock_isolated(tmp_path):
    from booley.core.file_lock import nonblocking_file_lock

    rec = replace(record(tmp_path), state=GoalState.ABANDONED, ended_at="2026-10-08T11:00:00Z")
    paths = _persist_record(tmp_path, rec)
    store = GoalStore(tmp_path, lock_timeout_s=0)
    before = paths.record_file.read_bytes()
    assert model.project_goal(store, rec).terminal_summary == "abandoned"
    with (
        paths.lock_file.open("r+") as handle,
        nonblocking_file_lock(handle),
        pytest.raises(RuntimeError, match="busy"),
    ):
        model.project_goal(store, rec)
    assert paths.record_file.read_bytes() == before


def test_goal_evidence_states_checking_and_artifact_projection(tmp_path, monkeypatch):
    from booley.goals.binding import GoalRunBinding
    from booley.goals.model import GoalFamily, GoalSpec, RecordedGoal
    from booley.goals.status import GoalStatus
    from booley.harness.dashboard.app import _evidence_detail
    from booley.runtime.job_records import JobRecord
    from booley.runtime.job_snapshot import JobSnapshot, JobView

    keys = ("lint_clean_a", "lint_clean_b", "lint_clean_c", "lint_clean_d")
    rec = replace(
        record(tmp_path),
        goals=tuple(
            RecordedGoal(GoalSpec(key, GoalFamily.LINT, "top", {}, ("ad-hoc",)), 1) for key in keys
        ),
    )
    _persist_record(tmp_path, rec)
    status = GoalStatusView(
        rec,
        tuple(
            GoalStatus(key, verdict, "fixture")
            for key, verdict in zip(keys, ("met", "stale", "unmet", "unmet"), strict=True)
        ),
    )
    entries = {
        key: SimpleNamespace(ever_failed=index == 2, to_dict=lambda: {"params": {}})
        for index, key in enumerate(keys)
    }
    artifact = tmp_path / "lint.log"
    artifact.write_bytes(b"never display log content")
    evidence = [
        {
            "criterion": key,
            "role": "candidate",
            "sequence": index + 1,
            "recorded_at": "2026-10-08T10:00:00Z",
            "detail": {"log_path": str(artifact)},
        }
        for index, key in enumerate(keys)
    ]
    monkeypatch.setattr(model, "build_status", lambda *_a, **_k: status)
    monkeypatch.setattr(model, "load_goal_state", lambda *_: SimpleNamespace(criteria=entries))
    monkeypatch.setattr(model, "validated_evidence_records", lambda *_: evidence)
    monkeypatch.setattr(model, "original_observations", lambda *_: [{"sequence": 0}])
    view = model.project_goal(GoalStore(tmp_path), rec)
    assert [goal.state for goal in view.goals] == [
        "passing",
        "needs recheck",
        "failing",
        "not yet run",
    ]
    assert view.goals[0].artifacts == (str(artifact),)
    text = _evidence_detail(view.goals[0])
    assert "2026-10-08T10:00:00Z" in text and "Immutable derivation provenance" in text
    assert "never display log content" not in text
    binding = GoalRunBinding(
        tmp_path,
        rec.id,
        1,
        WT,
        tmp_path,
        rec.branch,
        "run",
        tuple((key, 1) for key in keys),
        (),
        "sha256:" + "a" * 64,
        "sha256:" + "a" * 64,
        True,
    )
    job = JobView(
        tmp_path,
        JobRecord(
            "run", "lint", rec.entered_at, 60, argv=["--target", "top"], binding=binding.to_json()
        ),
        "running",
    )
    projected = model.checking_goals(view, JobSnapshot((job,)))
    assert all(goal.checking for goal in projected.goals)
    assert [goal.state for goal in projected.goals] == [goal.state for goal in view.goals]
    stale_binding = replace(binding, record_revision=2)
    assert not any(
        goal.checking
        for goal in model.checking_goals(
            view,
            JobSnapshot(
                (replace(job, record=replace(job.record, binding=stale_binding.to_json())),)
            ),
        ).goals
    )


def test_terminal_public_package_projection_is_frozen_and_read_only(tmp_path):
    from booley.goals.proposals import digest

    rec = replace(
        record(tmp_path),
        state=GoalState.FINISHED,
        finish_operation="00000000-0000-4000-8000-000000000002",
    )
    facts = {
        "purpose": "booley.goal-completion/v1",
        "record": {"id": rec.id},
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "branch": rec.branch,
        "input_proof": {},
        "session_summary": "Finished and validated.",
        **{
            name: []
            for name in (
                "goals",
                "change_log",
                "proposal_decisions",
                "applied_proposal_decisions",
                "rejected_proposal_decisions",
                "agent_recorded_decisions",
                "evidence_transactions",
                "target_changes",
                "diff_summary",
                "constraint_edits",
            )
        },
    }
    rec = replace(rec, package_digest="sha256:" + digest(facts))
    paths = _persist_record(tmp_path, rec)
    package = paths.root / "operations" / rec.finish_operation / "review-package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps(facts))
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert (
        model.project_goal(GoalStore(tmp_path), rec).terminal_summary == facts["session_summary"]
    )
    assert before == {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
    facts["session_summary"] = "Tampered"
    package.write_text(json.dumps(facts))
    with pytest.raises(ValueError, match="frozen"):
        model.project_goal(GoalStore(tmp_path), rec)


@pytest.mark.parametrize("corrupt", [False, True])
def test_unavailable_registry_never_reports_quiet_goal_worktrees(tmp_path, monkeypatch, corrupt):
    rec = record(tmp_path)
    fake_store = SimpleNamespace(
        list_active=lambda: SimpleNamespace(records=(rec,)), identify_worktree=lambda _: WT
    )
    monkeypatch.setattr(doctor, "GoalStore", lambda _: fake_store)
    monkeypatch.setattr(doctor, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(doctor, "list_worktrees", lambda _: (WorktreeEntry(tmp_path),))
    if corrupt:
        registry = SessionRegistry(tmp_path)
        registry.root.mkdir(parents=True)
        (registry.root / "bad.json").write_text("{broken")
    result = doctor.inspect_presence(tmp_path, tmp_path, inside_sandbox=True, now=100)
    assert result.unavailable
    assert not any(w.check_id == "goals.quiet-session" for w in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["healthy", "unoccupied"])
async def test_deleted_goal_worktree_does_not_break_status_or_dashboard(
    tmp_path, monkeypatch, caller
):
    from booley.goals.input_identity import root_bindings
    from booley.goals.status import build_status
    from booley.mcp.goal_tools import dispatch_goal_tool

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    work = tmp_path / "removed"
    healthy = tmp_path / "healthy"
    unoccupied = tmp_path / "unoccupied"
    control = tmp_path / "data"
    for path in (work, healthy, unoccupied, control):
        path.mkdir()
    missing_record = replace(
        record(work), input_paths=root_bindings({"rtl": work, "project": control})
    )
    healthy_record = replace(
        record(healthy),
        id="healthy-20261008T100000Z",
        worktree=replace(WT, checkout="worktrees/other"),
    )
    _persist_record(control, missing_record)
    _persist_record(control, healthy_record)
    before_healthy = build_status(GoalStore(control), healthy_record)
    work.rmdir()
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = await dispatch_goal_tool(
        "goal_status", {"work_dir": str(tmp_path / caller)}, project_dir=control
    )
    assert not result.is_error
    assert "Goal worktree missing or unavailable" in result.value[0].text
    assert build_status(GoalStore(control), healthy_record) == before_healthy
    snapshot = model.DashboardReader(healthy, control).read()
    missing_view = next(goal for goal in snapshot.goals if goal.id == missing_record.id)
    assert missing_view.status is not None
    assert "Goal worktree missing or unavailable" in missing_view.diagnostic
    assert "Goal worktree missing or unavailable" in missing_view.status.warning
    assert (
        next(goal for goal in snapshot.goals if goal.id == healthy_record.id).status
        == before_healthy
    )
    assert before == {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
