"""Generic execution-lease validation tests."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime import execution_lease as leases
from booley.runtime import job_records as jobrec
from booley.runtime.execution_lease import (
    ExecutionLeaseAbsentPath,
    ExecutionLeaseEnvironment,
    ExecutionLeaseError,
    ExecutionLeaseFile,
    validate_recording,
)
from booley.runtime.pid import DEAD, REUSED, UNKNOWN, ProcessIdentity, ProcessObservation
from booley.runtime.timefmt import rfc3339_from_epoch


def _lease_environment(
    tmp_path: Path, monkeypatch, *, owner_pid: int | None = None
) -> tuple[Path, Path, ExecutionLeaseEnvironment]:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state.json"
    lease_path = tmp_path / "operation.json"
    review_ticket = tmp_path / "tickets" / "board" / "review" / "demo.md"
    worktree.mkdir()
    state.write_text("{}\n", encoding="utf-8")
    review_ticket.parent.mkdir(parents=True)
    review_ticket.write_text("demo\n", encoding="utf-8")
    runtime_ticket = tmp_path / "logs" / "ticket.md"
    runtime_ticket.parent.mkdir()
    runtime_ticket.write_text("demo\n", encoding="utf-8")
    log_dir = tmp_path / "logs"
    runtime_dir = log_dir / ".runtime"
    issued_epoch = time.time() - 1
    environment = ExecutionLeaseEnvironment(
        schema=1,
        lease_id="lease-1",
        lease_file=lease_path,
        phase="interactive",
        issued_at=rfc3339_from_epoch(issued_epoch),
        expires_at=rfc3339_from_epoch(issued_epoch + 7200),
        basis_id="basis-1",
        state_file=state,
        work_dir=worktree,
        log_dir=log_dir,
        runtime_dir=runtime_dir,
        ticket_file=runtime_ticket,
        jobs_root=runtime_dir / "jobs",
        required_files=(
            ExecutionLeaseFile.capture("Ticket Board review Ticket", review_ticket),
            ExecutionLeaseFile.capture("Acceptance Basis runtime Ticket", runtime_ticket),
        ),
        absent_paths=(
            ExecutionLeaseAbsentPath(
                "accepted Criteria Satisfaction Record",
                log_dir / "acceptance" / "accepted.json",
            ),
        ),
    )
    record = environment.operation_record(owner_pid=os.getpid())
    if owner_pid is not None:
        record["pid"] = owner_pid
        record["owner_identity"] = ProcessIdentity(
            pid=owner_pid, identity_scope="synthetic", start_token=1
        ).to_payload()
    lease_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    for key, value in environment.to_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))
    monkeypatch.setenv("BOOLEY_WORKTREE", str(worktree))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(log_dir))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(runtime_ticket))
    monkeypatch.setenv("BOOLEY_EXECUTION_ID", environment.lease_id)
    monkeypatch.setenv("BOOLEY_REVIEW_OPERATION", environment.lease_id)
    return worktree, lease_path, environment


def test_unleased_publication_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BOOLEY_EXECUTION_LEASE", raising=False)

    validate_recording(tmp_path)


def test_current_lease_accepts_matching_constraints(tmp_path: Path, monkeypatch) -> None:
    worktree, _lease, _environment = _lease_environment(tmp_path, monkeypatch)

    validate_recording(worktree)


def test_stale_identity_and_foreign_paths_are_rejected(tmp_path: Path, monkeypatch) -> None:
    worktree, lease_path, environment = _lease_environment(tmp_path, monkeypatch)
    record = environment.operation_record(owner_pid=os.getpid())
    record["token"] = "replacement"
    lease_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLeaseError, match="no longer current"):
        validate_recording(worktree)

    lease_path.write_text(
        json.dumps(environment.operation_record(owner_pid=os.getpid())) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutionLeaseError, match="work directory"):
        validate_recording(tmp_path)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(tmp_path / "foreign.json"))
    with pytest.raises(ExecutionLeaseError, match="state path"):
        validate_recording(worktree)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("BOOLEY_TICKET_FILE", "foreign-ticket.md", "Ticket path"),
        ("BOOLEY_EXECUTION_ID", "foreign-execution", "execution identity"),
        ("BOOLEY_REVIEW_OPERATION", "foreign-operation", "review operation identity"),
    ],
)
def test_publication_environment_identity_drift_is_rejected(
    tmp_path: Path, monkeypatch, key: str, value: str, message: str
) -> None:
    worktree, _lease, _environment = _lease_environment(tmp_path, monkeypatch)
    monkeypatch.setenv(key, value)

    with pytest.raises(ExecutionLeaseError, match=message):
        validate_recording(worktree)


def test_lease_record_path_drift_is_rejected(tmp_path: Path, monkeypatch) -> None:
    worktree, lease_path, environment = _lease_environment(tmp_path, monkeypatch)
    redirected = tmp_path / "redirected-operation.json"
    redirected.write_bytes(lease_path.read_bytes())
    payload = json.loads(environment.to_environment()["BOOLEY_EXECUTION_LEASE"])
    payload["lease_file"] = str(redirected)
    monkeypatch.setenv("BOOLEY_EXECUTION_LEASE", json.dumps(payload))

    with pytest.raises(ExecutionLeaseError, match="lease_file"):
        validate_recording(worktree)


@pytest.mark.parametrize(
    "field", ["basis_id", "state_file", "log_dir", "runtime_dir", "jobs_root"]
)
def test_resolved_lease_constraint_drift_is_rejected(
    tmp_path: Path, monkeypatch, field: str
) -> None:
    worktree, lease_path, environment = _lease_environment(tmp_path, monkeypatch)
    record = environment.operation_record(owner_pid=os.getpid())
    record[field] = "changed"
    lease_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLeaseError, match=field):
        validate_recording(worktree)


def test_review_status_and_acceptance_are_fenced(tmp_path: Path, monkeypatch) -> None:
    worktree, _lease, environment = _lease_environment(tmp_path, monkeypatch)
    review_ticket = environment.required_files[0].path
    review_ticket.unlink()
    with pytest.raises(ExecutionLeaseError, match="Ticket Board review Ticket"):
        validate_recording(worktree)

    review_ticket.write_text("demo\n", encoding="utf-8")
    acceptance_record = environment.absent_paths[0].path
    acceptance_record.parent.mkdir(parents=True)
    acceptance_record.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ExecutionLeaseError, match="accepted Criteria Satisfaction Record"):
        validate_recording(worktree)


def test_ticket_basis_drift_is_rejected(tmp_path: Path, monkeypatch) -> None:
    worktree, _lease, environment = _lease_environment(tmp_path, monkeypatch)

    for constraint in environment.required_files:
        constraint.path.write_text("changed basis\n", encoding="utf-8")
        with pytest.raises(ExecutionLeaseError, match=constraint.label):
            validate_recording(worktree)
        constraint.path.write_text("demo\n", encoding="utf-8")


def test_typed_environment_round_trips(tmp_path: Path, monkeypatch) -> None:
    _worktree, _lease, environment = _lease_environment(tmp_path, monkeypatch)

    assert ExecutionLeaseEnvironment.from_environment(environment.to_environment()) == environment


def test_owner_identity_capture_failure_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _worktree, _lease, environment = _lease_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(leases, "capture_process_identity", lambda _pid: None)

    with pytest.raises(ExecutionLeaseError, match="owner process identity"):
        environment.operation_record(owner_pid=os.getpid())


def test_owner_identity_must_match_recorded_pid(tmp_path: Path, monkeypatch) -> None:
    worktree, lease_path, environment = _lease_environment(tmp_path, monkeypatch)
    record = environment.operation_record(owner_pid=os.getpid())
    record["pid"] = os.getpid() + 1
    lease_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLeaseError, match="owner identity"):
        validate_recording(worktree)


def test_dead_owner_requires_a_matching_active_job(tmp_path: Path, monkeypatch) -> None:
    worktree, _lease, _environment = _lease_environment(tmp_path, monkeypatch, owner_pid=999999)
    monkeypatch.setattr(leases, "observe_process", lambda _identity: ProcessObservation(DEAD))
    monkeypatch.setattr(
        leases,
        "active_jobs",
        lambda _root: [SimpleNamespace(lease_id="another-lease")],
    )
    with pytest.raises(ExecutionLeaseError, match="matching detached job"):
        validate_recording(worktree)

    monkeypatch.setattr(
        leases,
        "active_jobs",
        lambda _root: [SimpleNamespace(lease_id="lease-1")],
    )
    validate_recording(worktree)


def test_reused_owner_pid_requires_a_matching_active_job(tmp_path: Path, monkeypatch) -> None:
    worktree, lease_path, environment = _lease_environment(tmp_path, monkeypatch)
    record = environment.operation_record(owner_pid=os.getpid())
    record["owner_identity"] = ProcessIdentity(
        pid=os.getpid(), identity_scope="original", start_token=1
    ).to_payload()
    lease_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(leases, "observe_process", lambda _identity: ProcessObservation(REUSED))
    monkeypatch.setattr(leases, "active_jobs", lambda _root: [])

    with pytest.raises(ExecutionLeaseError, match="matching detached job"):
        validate_recording(worktree)


def test_unknown_owner_identity_requires_a_matching_active_job(
    tmp_path: Path, monkeypatch
) -> None:
    worktree, _lease_path, _environment = _lease_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(leases, "observe_process", lambda _identity: ProcessObservation(UNKNOWN))
    monkeypatch.setattr(leases, "active_jobs", lambda _root: [])

    with pytest.raises(ExecutionLeaseError, match="matching detached job"):
        validate_recording(worktree)


def test_expired_lease_is_rejected(tmp_path: Path, monkeypatch) -> None:
    worktree, lease_path, environment = _lease_environment(tmp_path, monkeypatch)
    expired = replace(
        environment,
        issued_at="2000-01-01T00:00:00Z",
        expires_at="2000-01-01T02:00:00Z",
    )
    lease_path.write_text(
        json.dumps(expired.operation_record(owner_pid=os.getpid())) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "BOOLEY_EXECUTION_LEASE", expired.to_environment()["BOOLEY_EXECUTION_LEASE"]
    )

    with pytest.raises(ExecutionLeaseError, match="expired"):
        validate_recording(worktree)

    monkeypatch.setattr(
        leases,
        "active_jobs",
        lambda _root: [SimpleNamespace(lease_id=environment.lease_id)],
    )
    validate_recording(worktree)


def test_future_dated_job_cannot_extend_expired_lease(tmp_path: Path, monkeypatch) -> None:
    worktree, lease_path, environment = _lease_environment(tmp_path, monkeypatch)
    expired = replace(
        environment,
        issued_at="2000-01-01T00:00:00Z",
        expires_at="2000-01-01T02:00:00Z",
    )
    lease_path.write_text(
        json.dumps(expired.operation_record(owner_pid=os.getpid())) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "BOOLEY_EXECUTION_LEASE", expired.to_environment()["BOOLEY_EXECUTION_LEASE"]
    )
    jobrec.write_record(
        jobrec.JobRecord(
            run_id="future-job",
            endpoint="simulate",
            started_at="2999-01-01T00:00:00Z",
            timeout_s=60,
            lease_id=environment.lease_id,
        ),
        environment.jobs_root,
    )

    with pytest.raises(ExecutionLeaseError, match="Job started_at is in the future"):
        validate_recording(worktree)
