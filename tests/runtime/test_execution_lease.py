"""Generic execution-lease validation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.runtime.execution_lease import ExecutionLeaseError, validate_recording


def _lease_environment(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state.json"
    lease = tmp_path / "operation.json"
    worktree.mkdir()
    state.write_text("{}\n", encoding="utf-8")
    lease.write_text(
        json.dumps({"token": "lease-1", "phase": "interactive"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOOLEY_EXECUTION_LEASE_ID", "lease-1")
    monkeypatch.setenv("BOOLEY_EXECUTION_LEASE_FILE", str(lease))
    monkeypatch.setenv("BOOLEY_EXECUTION_LEASE_PHASE", "interactive")
    monkeypatch.setenv("BOOLEY_EXECUTION_LEASE_STATE_FILE", str(state))
    monkeypatch.setenv("BOOLEY_EXECUTION_LEASE_WORK_DIR", str(worktree))
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))
    return worktree, lease


def test_unleased_publication_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BOOLEY_EXECUTION_LEASE_ID", raising=False)

    validate_recording(tmp_path)


def test_current_lease_accepts_matching_paths(tmp_path: Path, monkeypatch) -> None:
    worktree, _lease = _lease_environment(tmp_path, monkeypatch)

    validate_recording(worktree)


def test_stale_lease_and_foreign_paths_are_rejected(tmp_path: Path, monkeypatch) -> None:
    worktree, lease = _lease_environment(tmp_path, monkeypatch)
    lease.write_text(
        json.dumps({"token": "replacement", "phase": "interactive"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ExecutionLeaseError, match="no longer current"):
        validate_recording(worktree)

    lease.write_text(
        json.dumps({"token": "lease-1", "phase": "interactive"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutionLeaseError, match="work directory"):
        validate_recording(tmp_path)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(tmp_path / "foreign.json"))
    with pytest.raises(ExecutionLeaseError, match="state path"):
        validate_recording(worktree)
