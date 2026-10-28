"""Retention rules for supervised Sandbox Attachment execution records."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from tests.sharing_error_support import hold_opens, no_retry_delay, sharing_error

from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    ExecutionId,
    atomic_write_json,
    campaign_child_entry_manifests,
    child_context_matches,
    execution_paths,
    gc_terminal_executions,
    read_attachment_heartbeat,
    read_json,
    verify_inherited_execution_id,
    write_attachment_heartbeat,
)
from booley.runtime.pid import (
    DEAD,
    REUSED,
    RUNNING,
    UNKNOWN,
    ZOMBIE,
    ProcessIdentity,
    ProcessObservation,
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

    entry["parent_execution_id"] = "a" * 32
    (entries / f"{child_id}.json").write_text(
        json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    assert campaign_child_entry_manifests(tmp_path) == (Path(entry["manifest_path"]),)


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


def _active_execution_record(supervisor: ProcessIdentity | None) -> dict:
    return {
        "schema_version": PROTOCOL_VERSION,
        "state": "running",
        "runtime_identity": supervisor.identity_scope if supervisor is not None else None,
        "supervisor": supervisor.to_payload() if supervisor is not None else None,
        "leader": None,
        "exit_code": None,
        "tree_terminal": False,
        "terminal_cause": None,
        "updated_at": "1970-01-01T00:00:00Z",
    }


@pytest.mark.parametrize(("owner_pid", "ancestor_result"), [(20, True), (10, False)])
def test_verified_inherited_execution_accepts_descendant_and_supervisor_itself(
    tmp_path: Path, owner_pid: int, ancestor_result: bool
) -> None:
    execution_id = "d" * 32
    supervisor = ProcessIdentity(10, "fake", 1)
    atomic_write_json(
        execution_paths(execution_id, booley_dir=tmp_path).record,
        _active_execution_record(supervisor),
    )

    assert verify_inherited_execution_id(
        execution_id,
        booley_dir=tmp_path,
        owner_pid=owner_pid,
        observe_identity=lambda _identity: ProcessObservation(RUNNING),
        ancestor_check=lambda pid, ancestor: ancestor_result and (pid, ancestor) == (20, 10),
    ) == ExecutionId(execution_id)


def test_verified_inherited_execution_rejects_record_from_another_project(
    tmp_path: Path,
) -> None:
    execution_id = "e" * 32
    foreign = tmp_path / "foreign"
    current = tmp_path / "current"
    supervisor = ProcessIdentity(10, "fake", 1)
    atomic_write_json(
        execution_paths(execution_id, booley_dir=foreign).record,
        _active_execution_record(supervisor),
    )

    assert (
        verify_inherited_execution_id(
            execution_id,
            booley_dir=current,
            owner_pid=20,
            observe_identity=lambda _identity: ProcessObservation(RUNNING),
            ancestor_check=lambda pid, ancestor: (pid, ancestor) == (20, 10),
        )
        is None
    )


def test_verified_inherited_execution_rejects_non_ancestor(tmp_path: Path) -> None:
    execution_id = "f" * 32
    supervisor = ProcessIdentity(10, "fake", 1)
    atomic_write_json(
        execution_paths(execution_id, booley_dir=tmp_path).record,
        _active_execution_record(supervisor),
    )

    assert (
        verify_inherited_execution_id(
            execution_id,
            booley_dir=tmp_path,
            owner_pid=20,
            observe_identity=lambda _identity: ProcessObservation(RUNNING),
            ancestor_check=lambda _pid, _ancestor: False,
        )
        is None
    )


def test_verified_inherited_execution_rejects_invalid_id(tmp_path: Path) -> None:
    assert (
        verify_inherited_execution_id(
            "not-an-execution",
            booley_dir=tmp_path,
            owner_pid=20,
        )
        is None
    )


@pytest.mark.parametrize("record_kind", ["missing", "malformed", "terminal", "future"])
def test_verified_inherited_execution_rejects_unusable_records(
    tmp_path: Path, record_kind: str
) -> None:
    execution_id = "1" * 32
    paths = execution_paths(execution_id, booley_dir=tmp_path)
    supervisor = ProcessIdentity(10, "fake", 1)
    if record_kind == "malformed":
        paths.root.mkdir(parents=True)
        paths.record.write_text("not-json", encoding="utf-8")
    elif record_kind != "missing":
        record = _active_execution_record(supervisor)
        if record_kind == "terminal":
            record.update(state="terminal", tree_terminal=True, exit_code=0)
        else:
            record["schema_version"] = PROTOCOL_VERSION + 1
        atomic_write_json(paths.record, record)

    assert (
        verify_inherited_execution_id(
            execution_id,
            booley_dir=tmp_path,
            owner_pid=20,
            observe_identity=lambda _identity: ProcessObservation(RUNNING),
            ancestor_check=lambda _pid, _ancestor: True,
        )
        is None
    )


@pytest.mark.parametrize("state", [DEAD, REUSED, ZOMBIE, UNKNOWN])
def test_verified_inherited_execution_requires_live_durable_supervisor(
    tmp_path: Path, state
) -> None:
    execution_id = "2" * 32
    supervisor = ProcessIdentity(10, "fake", 1)
    atomic_write_json(
        execution_paths(execution_id, booley_dir=tmp_path).record,
        _active_execution_record(supervisor),
    )

    assert (
        verify_inherited_execution_id(
            execution_id,
            booley_dir=tmp_path,
            owner_pid=20,
            observe_identity=lambda _identity: ProcessObservation(state),
            ancestor_check=lambda _pid, _ancestor: True,
        )
        is None
    )


def test_verified_inherited_execution_rejects_live_leader_without_supervisor(
    tmp_path: Path,
) -> None:
    execution_id = "3" * 32
    leader = ProcessIdentity(10, "fake", 1)
    record = _active_execution_record(None)
    record["leader"] = leader.to_payload()
    atomic_write_json(execution_paths(execution_id, booley_dir=tmp_path).record, record)

    assert (
        verify_inherited_execution_id(
            execution_id,
            booley_dir=tmp_path,
            owner_pid=20,
            observe_identity=lambda _identity: ProcessObservation(RUNNING),
            ancestor_check=lambda _pid, _ancestor: True,
        )
        is None
    )


def _old_terminal(booley_dir: Path, execution_id: str) -> None:
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    paths.root.mkdir(parents=True)
    paths.record.write_text(
        json.dumps({"schema_version": 1, "state": "terminal", "tree_terminal": True}),
        encoding="utf-8",
    )
    os.utime(paths.record, (1, 1))


def test_heartbeat_rename_permission_error_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    """Windows [WinError 5] on replace() must not crash the auth flow."""
    paths = execution_paths("a" * 32, booley_dir=tmp_path)
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
    running_paths = execution_paths(running, booley_dir=tmp_path)
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
    assert not execution_paths(unreferenced, booley_dir=tmp_path).root.exists()
    assert execution_paths(referenced, booley_dir=tmp_path).root.exists()
    assert running_paths.root.exists()


def _unreadable_slot(tmp_path: Path, monkeypatch, execution_id: str) -> Path:
    """A slot entry that exists but cannot be opened (Windows scanner, sharing error)."""
    slot = tmp_path / "runtime" / "jobs" / "slots" / "heavy" / "holder.json"
    slot.parent.mkdir(parents=True)
    slot.write_text(json.dumps({"execution_id": execution_id}), encoding="utf-8")
    original_open = Path.open

    def locked_open(path, *args, **kwargs):
        if path == slot:
            raise PermissionError(13, "held by a scanner")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked_open)
    return slot


def test_gc_keeps_records_pinned_by_unreadable_slot_entry(tmp_path: Path, monkeypatch) -> None:
    # An unreadable entry may pin any record, so the pass removes nothing (#1905).
    pinned = "a" * 32
    _old_terminal(tmp_path, pinned)
    _unreadable_slot(tmp_path, monkeypatch, pinned)

    assert gc_terminal_executions(tmp_path, now=1_000_000, retention_seconds=10) == []
    assert execution_paths(pinned, booley_dir=tmp_path).root.exists()


def test_child_release_refuses_unreadable_slot_entry(tmp_path: Path, monkeypatch) -> None:
    from booley.runtime.execution_records import _require_child_token_absent

    child = ExecutionId("b" * 32)
    slot = _unreadable_slot(tmp_path, monkeypatch, child)
    with pytest.raises(ValueError, match=r"cannot prove .* unreadable"):
        _require_child_token_absent(tmp_path, child)
    monkeypatch.undo()
    slot.unlink()  # missing is absent
    _require_child_token_absent(tmp_path, child)


# --- Windows sharing errors on protocol reads (#2187) -------------------------------

_EXEC_ID = "d" * 32


def _protocol_paths(tmp_path: Path):
    paths = execution_paths(_EXEC_ID, booley_dir=tmp_path)
    paths.root.mkdir(parents=True)
    return paths


def _child_context(tmp_path: Path):
    """A valid child context plus the Project entry that binds its digest."""
    paths = _protocol_paths(tmp_path)
    context = {
        "$schema": "booley.supervised-child-context/v1",
        "child_execution_id": _EXEC_ID,
        "parent_execution_id": "sim-20260925T153819Z-1",
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_sha256": "sha256:" + "1" * 64,
        "work_item_id": "item:0000:0123456789abcdef",
        "attempt_id": "550e8400-e29b-41d4-a716-446655440001",
        "attempt_ordinal": 1,
    }
    raw = json.dumps(context).encode()
    paths.context.write_bytes(raw)
    entry = {
        "$schema": "booley.simulation-campaign-child-entry/v1",
        "child_execution_id": _EXEC_ID,
        "runtime_context_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        **{k: v for k, v in context.items() if k not in {"$schema", "child_execution_id"}},
    }
    entries = paths.root.parents[2] / ".runtime" / "campaign-child-executions" / "entries"
    entries.mkdir(parents=True)
    (entries / f"{_EXEC_ID}.json").write_text(json.dumps(entry), encoding="utf-8")
    return paths


def _heartbeat(tmp_path: Path):
    paths = _protocol_paths(tmp_path)
    write_attachment_heartbeat(paths, generation=7)
    return paths


def _record(tmp_path: Path):
    paths = _protocol_paths(tmp_path)
    paths.record.write_text(json.dumps({"state": "running"}), encoding="utf-8")
    return paths


def test_record_read_survives_transient_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _record(tmp_path)
    hold_opens(monkeypatch, {paths.record: 1})
    assert read_json(paths.record) == {"state": "running"}


def test_context_read_survives_transient_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _child_context(tmp_path)
    hold_opens(monkeypatch, {paths.context: 1})
    assert child_context_matches(paths, _EXEC_ID) is True


def test_context_bytes_read_survives_transient_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _child_context(tmp_path)
    hold_opens(monkeypatch, {paths.context: 1}, skip=1)  # hold the digest read, not the parse
    assert child_context_matches(paths, _EXEC_ID) is True


def test_heartbeat_read_survives_transient_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _heartbeat(tmp_path)
    hold_opens(monkeypatch, {paths.heartbeat: 1})
    assert read_attachment_heartbeat(paths) == 7


def test_record_read_raises_lasting_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _record(tmp_path)
    attempts = hold_opens(monkeypatch, {paths.record: 5})
    with pytest.raises(PermissionError):
        read_json(paths.record)
    assert attempts[paths.record] > 1


def test_context_read_raises_lasting_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _child_context(tmp_path)
    hold_opens(monkeypatch, {paths.context: 5})
    with pytest.raises(PermissionError):
        child_context_matches(paths, _EXEC_ID)


def test_context_bytes_read_raises_lasting_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _child_context(tmp_path)
    hold_opens(monkeypatch, {paths.context: 5}, skip=1)
    with pytest.raises(PermissionError):
        child_context_matches(paths, _EXEC_ID)


def test_heartbeat_read_raises_lasting_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _heartbeat(tmp_path)
    hold_opens(monkeypatch, {paths.heartbeat: 5})
    with pytest.raises(PermissionError):
        read_attachment_heartbeat(paths)


def _hold_heartbeat_replace(monkeypatch, failures: int) -> None:
    no_retry_delay(monkeypatch)
    real_replace = Path.replace
    remaining = [failures]

    def replace_with_holds(self, target):
        if "attachment-heartbeat" in str(target) and remaining[0] > 0:
            remaining[0] -= 1
            raise sharing_error()
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace_with_holds)


def test_heartbeat_write_retries_transient_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _heartbeat(tmp_path)
    _hold_heartbeat_replace(monkeypatch, 1)
    write_attachment_heartbeat(paths, generation=8)
    assert read_attachment_heartbeat(paths) == 8


def test_heartbeat_write_survives_lasting_sharing_error(tmp_path: Path, monkeypatch) -> None:
    paths = _heartbeat(tmp_path)
    _hold_heartbeat_replace(monkeypatch, 99)
    write_attachment_heartbeat(paths, generation=8)  # exhausted retries do not fail the run
    assert read_attachment_heartbeat(paths) == 7
    assert [p.name for p in paths.root.iterdir()] == ["attachment-heartbeat"]


def test_missing_protocol_files_read_as_absent(tmp_path: Path) -> None:
    paths = _protocol_paths(tmp_path)
    assert read_json(paths.record) is None
    assert read_attachment_heartbeat(paths) is None


def test_corrupt_protocol_files_read_as_absent(tmp_path: Path) -> None:
    paths = _protocol_paths(tmp_path)
    paths.record.write_bytes(b"\xff{not json")
    paths.heartbeat.write_bytes(b"abc")
    assert read_json(paths.record) is None
    assert read_attachment_heartbeat(paths) is None


def test_posix_unreadable_protocol_file_reads_as_absent(tmp_path: Path, monkeypatch) -> None:
    paths = _record(tmp_path)
    no_retry_delay(monkeypatch)
    real_open = Path.open

    def denied(path, *args, **kwargs):
        if path == paths.record:
            raise PermissionError(13, "Permission denied")  # POSIX EACCES: no winerror
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert read_json(paths.record) is None


def test_gc_keeps_record_whose_read_outlasts_sharing_budget(tmp_path: Path, monkeypatch) -> None:
    _old_terminal(tmp_path, _EXEC_ID)
    paths = execution_paths(_EXEC_ID, booley_dir=tmp_path)
    hold_opens(monkeypatch, {paths.record: 99})
    gc_terminal_executions(tmp_path)
    assert paths.record.exists()
