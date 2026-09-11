"""Generic durable execution-lease identity and path guards."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ExecutionLeaseError(ValueError):
    """A command tried to publish after its execution lease became invalid."""


@dataclass(frozen=True)
class ExecutionLease:
    """The immutable identity read from one durable operation record."""

    lease_id: str
    phase: str
    record: dict[str, Any]


def current_lease_id() -> str | None:
    """Return the inherited lease identity, if this is a leased command."""
    return os.environ.get("BOOLEY_EXECUTION_LEASE_ID") or None


def load_execution_lease(
    path: Path, *, expected_id: str, expected_phase: str
) -> ExecutionLease:
    """Load and validate a generic durable lease record."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionLeaseError("execution lease is no longer available") from exc
    if not isinstance(value, dict):
        raise ExecutionLeaseError("execution lease record is invalid")
    if value.get("token") != expected_id or value.get("phase") != expected_phase:
        raise ExecutionLeaseError("execution lease is no longer current")
    return ExecutionLease(expected_id, expected_phase, value)


def require_same_path(actual: Path | str, expected: Path | str, *, label: str) -> None:
    """Reject evidence routed to a path outside the leased execution."""
    if Path(actual).resolve() != Path(expected).resolve():
        raise ExecutionLeaseError(f"{label} differs from the execution lease")


def validate_recording(work_dir: Path | str | None = None) -> None:
    """Validate endpoint publication against its inherited generic lease."""
    lease_id = current_lease_id()
    if not lease_id:
        return
    lease_file = os.environ.get("BOOLEY_EXECUTION_LEASE_FILE")
    phase = os.environ.get("BOOLEY_EXECUTION_LEASE_PHASE")
    if not lease_file or not phase:
        raise ExecutionLeaseError("execution lease environment is incomplete")
    load_execution_lease(Path(lease_file), expected_id=lease_id, expected_phase=phase)
    state_file = os.environ.get("BOOLEY_EXECUTION_LEASE_STATE_FILE")
    current_state_file = os.environ.get("BOOLEY_STATE_FILE")
    if state_file and current_state_file:
        require_same_path(current_state_file, state_file, label="interactive state path")
    expected_work_dir = os.environ.get("BOOLEY_EXECUTION_LEASE_WORK_DIR")
    if work_dir is not None and expected_work_dir:
        require_same_path(
            work_dir,
            expected_work_dir,
            label="interactive endpoint work directory",
        )
