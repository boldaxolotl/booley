"""Retention rules for supervised Sandbox Attachment execution records."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from booley.runtime.execution_records import (
    ExecutionId,
    atomic_write_json,
    campaign_child_entry_manifests,
    execution_paths,
    gc_terminal_executions,
    read_attachment_heartbeat,
    write_attachment_heartbeat,
)


def test_campaign_child_entry_manifest_rejects_display_form_parent_identity(
    tmp_path: Path,
) -> None:
    child_id = "e" * 32
    entries = tmp_path / "entries"
    entries.mkdir()
    entry = {
        "$schema": "booley.simulation-campaign-child-entry/v1",
        "child_execution_id": child_id,
        "parent_execution_id": "sim-20260925T153819Z-1",
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_path": str((tmp_path / "manifest.json").resolve()),
        "manifest_sha256": "sha256:" + "1" * 64,
        "work_item_id": "item:0000:0123456789abcdef",
        "attempt_id": "550e8400-e29b-41d4-a716-446655440001",
        "attempt_ordinal": 1,
        "attempt_relative_path": "items/item/attempts/0001-attempt",
        "runtime_context_sha256": "sha256:" + "2" * 64,
    }
    (entries / f"{child_id}.json").write_text(
        json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid schema"):
        campaign_child_entry_manifests(tmp_path)


def test_atomic_write_json_fsyncs_file_then_rename_then_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def observe_fsync(descriptor: int) -> None:
        events.append(
            "parent_fsync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file_fsync"
        )
        real_fsync(descriptor)

    def observe_replace(self: Path, target: Path) -> Path:
        events.append("replace")
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(Path, "replace", observe_replace)

    path = tmp_path / "record.json"
    atomic_write_json(path, {"state": "terminal"})

    expected = ["file_fsync", "replace"]
    if os.name != "nt":
        expected.append("parent_fsync")
    assert events == expected
    assert json.loads(path.read_text()) == {"state": "terminal"}


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


def test_heartbeat_rename_permission_error_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    """Windows [WinError 5] on replace() must not crash the auth flow."""
    paths = execution_paths("a" * 32, project_dir=tmp_path)
    write_attachment_heartbeat(paths, generation=1)
    assert read_attachment_heartbeat(paths) == 1

    original_replace = Path.replace

    def fail_replace(self, target):
        if "heartbeat" in str(target):
            raise PermissionError("[WinError 5] Access is denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    write_attachment_heartbeat(paths, generation=2)
    assert read_attachment_heartbeat(paths) == 1


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
