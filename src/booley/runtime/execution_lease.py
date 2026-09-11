"""Generic durable execution-lease identity and evidence guards."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_list,
    require_str,
)
from booley.runtime.job_records import JobRecordError
from booley.runtime.job_wait import active_jobs
from booley.runtime.pid import (
    RUNNING,
    ProcessIdentity,
    capture_process_identity,
    observe_process,
)


class ExecutionLeaseError(ValueError):
    """A recording attempt escaped the execution lease that admitted it."""


@dataclass(frozen=True)
class ExecutionLease:
    """Validated durable lease record."""

    lease_id: str
    phase: str
    owner_pid: int
    owner_identity: ProcessIdentity
    record: dict[str, Any]


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class ExecutionLeaseFile:
    """One immutable file whose bytes define an admitted execution."""

    label: str
    path: Path
    sha256: str

    @classmethod
    def capture(cls, label: str, path: Path) -> ExecutionLeaseFile:
        """Capture a required file at lease admission."""
        if not path.is_file():
            raise ExecutionLeaseError(f"execution lease requires {label} at {path}")
        return cls(label=label, path=path, sha256=_file_sha256(path))

    @classmethod
    def from_mapping(cls, value: Any, *, field: str) -> ExecutionLeaseFile:
        """Parse one file constraint from untrusted lease JSON."""
        row = require_dict(value, field=field)
        return cls(
            label=require_str(row, "label"),
            path=Path(require_str(row, "path")),
            sha256=require_str(row, "sha256"),
        )

    def as_dict(self) -> dict[str, str]:
        """Return the JSON representation persisted with the lease."""
        return {"label": self.label, "path": str(self.path), "sha256": self.sha256}

    def validate(self) -> None:
        """Require this file to remain present and byte-for-byte unchanged."""
        try:
            current = _file_sha256(self.path)
        except OSError as exc:
            raise ExecutionLeaseError(f"execution {self.label} is no longer available") from exc
        if current != self.sha256:
            raise ExecutionLeaseError(f"execution {self.label} is no longer current")


@dataclass(frozen=True)
class ExecutionLeaseAbsentPath:
    """One path that must remain absent while an execution publishes evidence."""

    label: str
    path: Path

    @classmethod
    def from_mapping(cls, value: Any, *, field: str) -> ExecutionLeaseAbsentPath:
        """Parse one absence constraint from untrusted lease JSON."""
        row = require_dict(value, field=field)
        return cls(label=require_str(row, "label"), path=Path(require_str(row, "path")))

    def as_dict(self) -> dict[str, str]:
        """Return the JSON representation persisted with the lease."""
        return {"label": self.label, "path": str(self.path)}

    def validate(self) -> None:
        """Require this path to remain absent."""
        if self.path.exists():
            raise ExecutionLeaseError(f"execution {self.label} now exists")


def _parse_files(value: Any) -> tuple[ExecutionLeaseFile, ...]:
    rows = require_list(value, field="required_files")
    return tuple(
        ExecutionLeaseFile.from_mapping(row, field=f"required_files[{index}]")
        for index, row in enumerate(rows)
    )


def _parse_absent_paths(value: Any) -> tuple[ExecutionLeaseAbsentPath, ...]:
    rows = require_list(value, field="absent_paths")
    return tuple(
        ExecutionLeaseAbsentPath.from_mapping(row, field=f"absent_paths[{index}]")
        for index, row in enumerate(rows)
    )


@dataclass(frozen=True)
class ExecutionLeaseEnvironment:
    """Typed process-boundary representation of one execution lease.

    Ticket Board resolves Ticket-specific policy into generic path and identity
    constraints. Runtime validates them without rediscovering Ticket state.
    """

    schema: int
    lease_id: str
    lease_file: Path
    phase: str
    issued_at: str
    expires_at: str
    basis_id: str
    state_file: Path
    work_dir: Path
    log_dir: Path
    runtime_dir: Path
    ticket_file: Path
    jobs_root: Path
    required_files: tuple[ExecutionLeaseFile, ...]
    absent_paths: tuple[ExecutionLeaseAbsentPath, ...]

    _ENVIRONMENT_KEY = "BOOLEY_EXECUTION_LEASE"

    def _constraints(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "lease_file": str(self.lease_file),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "basis_id": self.basis_id,
            "state_file": str(self.state_file),
            "work_dir": str(self.work_dir),
            "log_dir": str(self.log_dir),
            "runtime_dir": str(self.runtime_dir),
            "ticket_file": str(self.ticket_file),
            "jobs_root": str(self.jobs_root),
            "required_files": [item.as_dict() for item in self.required_files],
            "absent_paths": [item.as_dict() for item in self.absent_paths],
        }

    def to_environment(self) -> dict[str, str]:
        """Serialize this lease for a child process."""
        payload = {
            "lease_id": self.lease_id,
            "phase": self.phase,
            **self._constraints(),
        }
        return {self._ENVIRONMENT_KEY: json.dumps(payload, sort_keys=True)}

    def operation_record(self, *, owner_pid: int) -> dict[str, Any]:
        """Build the durable record checked by every evidence publication."""
        identity = capture_process_identity(owner_pid)
        if identity is None:
            raise ExecutionLeaseError("execution lease owner process identity is unavailable")
        return {
            "pid": owner_pid,
            "owner_identity": identity.to_payload(),
            "phase": self.phase,
            "token": self.lease_id,
            **self._constraints(),
        }

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ExecutionLeaseEnvironment | None:
        """Parse the current lease, or ``None`` outside leased execution."""
        source = os.environ if environment is None else environment
        raw = source.get(cls._ENVIRONMENT_KEY)
        if not raw:
            return None
        try:
            value = require_dict(json.loads(raw), field=cls._ENVIRONMENT_KEY)
            schema = require_int(value.get("schema"), field="execution lease schema")
            if schema != 1:
                raise BoundaryError(f"unsupported execution lease schema {schema}")
            return cls(
                schema=schema,
                lease_id=require_str(value, "lease_id"),
                lease_file=Path(require_str(value, "lease_file")),
                phase=require_str(value, "phase"),
                issued_at=require_str(value, "issued_at"),
                expires_at=require_str(value, "expires_at"),
                basis_id=require_str(value, "basis_id"),
                state_file=Path(require_str(value, "state_file")),
                work_dir=Path(require_str(value, "work_dir")),
                log_dir=Path(require_str(value, "log_dir")),
                runtime_dir=Path(require_str(value, "runtime_dir")),
                ticket_file=Path(require_str(value, "ticket_file")),
                jobs_root=Path(require_str(value, "jobs_root")),
                required_files=_parse_files(value.get("required_files")),
                absent_paths=_parse_absent_paths(value.get("absent_paths")),
            )
        except (BoundaryError, json.JSONDecodeError, TypeError) as exc:
            raise ExecutionLeaseError(f"execution lease environment is malformed: {exc}") from exc

    def validate_record(self, record: Mapping[str, Any]) -> None:
        """Require the durable record to preserve all issued constraints."""
        expected = {"phase": self.phase, "token": self.lease_id, **self._constraints()}
        for key, value in expected.items():
            if record.get(key) != value:
                raise ExecutionLeaseError(f"execution lease {key} is no longer current")

    def is_unexpired(self, *, now: datetime | None = None) -> bool:
        """Validate issue/expiry metadata and report base-lease freshness."""
        from booley.runtime.timefmt import parse_timestamp

        current = now or datetime.now(UTC)
        try:
            issued = parse_timestamp(self.issued_at)
            expires = parse_timestamp(self.expires_at)
        except ValueError as exc:
            raise ExecutionLeaseError("execution lease lifetime is malformed") from exc
        if expires <= issued:
            raise ExecutionLeaseError("execution lease expiry does not follow issuance")
        if issued > current:
            raise ExecutionLeaseError("execution lease was issued in the future")
        return expires > current


def current_lease_id() -> str | None:
    """Return the active typed lease identity, if this process has one."""
    lease = ExecutionLeaseEnvironment.from_environment()
    return lease.lease_id if lease is not None else None


def load_execution_lease(path: Path, *, expected_id: str, expected_phase: str) -> ExecutionLease:
    """Load and validate the generic identity fields of one lease record."""
    try:
        value = require_dict(json.loads(path.read_text(encoding="utf-8")), field="execution lease")
        token = require_str(value, "token")
        phase = require_str(value, "phase")
        owner_pid = require_int(value.get("pid"), field="pid")
        raw_identity = value.get("owner_identity")
        owner_identity = ProcessIdentity.from_payload(raw_identity)
        if owner_identity is None or owner_identity.pid != owner_pid:
            raise BoundaryError("execution lease owner identity is invalid")
    except (OSError, json.JSONDecodeError, BoundaryError, TypeError) as exc:
        raise ExecutionLeaseError(f"execution lease is unavailable: {exc}") from exc
    if token != expected_id or phase != expected_phase:
        raise ExecutionLeaseError("execution lease is no longer current")
    return ExecutionLease(token, phase, owner_pid, owner_identity, value)


def _require_same_path(actual: Path | str | None, expected: Path, label: str) -> None:
    if actual is None or Path(actual).resolve() != expected.resolve():
        raise ExecutionLeaseError(f"execution {label} escaped its review lease")


def _has_matching_job(environment: ExecutionLeaseEnvironment) -> bool:
    try:
        jobs = active_jobs(environment.jobs_root)
    except JobRecordError as exc:
        raise ExecutionLeaseError(f"execution lease Job fence is invalid: {exc}") from exc
    return any(record.lease_id == environment.lease_id for record in jobs)


def validate_recording(work_dir: Path | str | None = None) -> None:
    """Reject evidence publication outside the current resolved lease."""
    environment = ExecutionLeaseEnvironment.from_environment()
    if environment is None:
        return
    lease = load_execution_lease(
        environment.lease_file,
        expected_id=environment.lease_id,
        expected_phase=environment.phase,
    )
    try:
        environment.validate_record(lease.record)
    except BoundaryError as exc:
        raise ExecutionLeaseError(f"execution lease is malformed: {exc}") from exc
    unexpired = environment.is_unexpired()
    if work_dir is not None:
        _require_same_path(work_dir, environment.work_dir, "work directory")
    _require_same_path(os.environ.get("BOOLEY_WORKTREE"), environment.work_dir, "worktree path")
    _require_same_path(os.environ.get("BOOLEY_STATE_FILE"), environment.state_file, "state path")
    _require_same_path(os.environ.get("BOOLEY_LOGS_DIR"), environment.log_dir, "log path")
    _require_same_path(
        os.environ.get("BOOLEY_RUNTIME_DIR"), environment.runtime_dir, "runtime path"
    )
    _require_same_path(
        os.environ.get("BOOLEY_TICKET_FILE"), environment.ticket_file, "Ticket path"
    )
    if os.environ.get("BOOLEY_EXECUTION_ID") != environment.lease_id:
        raise ExecutionLeaseError("execution identity escaped its review lease")
    if os.environ.get("BOOLEY_REVIEW_OPERATION") != environment.lease_id:
        raise ExecutionLeaseError("review operation identity escaped its execution lease")
    for constraint in environment.required_files:
        constraint.validate()
    for constraint in environment.absent_paths:
        constraint.validate()
    if _has_matching_job(environment):
        return
    if not unexpired:
        raise ExecutionLeaseError("execution lease has expired")
    if observe_process(lease.owner_identity).state is not RUNNING:
        raise ExecutionLeaseError("execution lease has no live owner or matching detached job")
