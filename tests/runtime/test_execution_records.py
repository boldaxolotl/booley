"""Retention rules for supervised Runtime Attachment execution records."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.runtime.execution_records import ExecutionId, execution_paths, gc_terminal_executions


def test_execution_id_owns_validation() -> None:
    assert str(ExecutionId("a" * 32)) == "a" * 32
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        ExecutionId("not-an-execution")


def _old_terminal(project_dir: Path, execution_id: str) -> None:
    paths = execution_paths(execution_id, project_dir=project_dir)
    paths.root.mkdir(parents=True)
    paths.record.write_text(
        json.dumps({"schema_version": 1, "state": "terminal", "tree_terminal": True}),
        encoding="utf-8",
    )
    os.utime(paths.record, (1, 1))


def test_gc_keeps_slot_referenced_and_nonterminal_execution_records(tmp_path: Path) -> None:
    unreferenced = "a" * 32
    referenced = "b" * 32
    running = "c" * 32
    _old_terminal(tmp_path, unreferenced)
    _old_terminal(tmp_path, referenced)
    running_paths = execution_paths(running, project_dir=tmp_path)
    running_paths.root.mkdir(parents=True)
    running_paths.record.write_text(
        json.dumps({"schema_version": 1, "state": "running", "tree_terminal": False}),
        encoding="utf-8",
    )
    os.utime(running_paths.record, (1, 1))
    slot = tmp_path / "runtime" / "jobs" / "slots" / "heavy" / "holder.json"
    slot.parent.mkdir(parents=True)
    slot.write_text(json.dumps({"execution_id": referenced}), encoding="utf-8")

    removed = gc_terminal_executions(tmp_path, now=1_000_000, retention_seconds=10)

    assert removed == [unreferenced]
    assert not execution_paths(unreferenced, project_dir=tmp_path).root.exists()
    assert execution_paths(referenced, project_dir=tmp_path).root.exists()
    assert running_paths.root.exists()
