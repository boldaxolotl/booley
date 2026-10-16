"""Explicit, exact-record orphan cleanup with durable fencing and resumable quarantine."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_list
from booley.goals.finish_attempt import validate_attempt, validate_local_artifacts
from booley.goals.inventory import load_record, logical_bytes, presence, unlinked
from booley.goals.lifecycle import LifecycleOperation, LifecycleRequest, read_bound_request
from booley.goals.model import GoalRecord, GoalState
from booley.goals.paths import goals_root, pruning_intent, pruning_root, record_paths
from booley.goals.proposals import load_proposal, proposal_path
from booley.goals.store import (
    GoalStore,
    GoalStoreError,
    RecordLock,
    current_lock_owner,
    shared_prune_lock,
)
from booley.runtime.atomic_files import atomic_write_once, fsync_directory
from booley.runtime.job_records import JobRecordError, strict_records
from booley.runtime.job_wait import active_jobs
from booley.runtime.pid import (
    DEAD,
    REUSED,
    RUNNING,
    UNKNOWN,
    ProcessIdentity,
    capture_process_identity,
    is_pid_alive,
    observe_process,
)
from booley.runtime.process_group import ProcessGroup, is_process_group_alive

_EVIDENCE_LOSS = "Permanent evidence loss: logs, transcripts, retained outcomes, operation authority and retry evidence will be deleted. History and shared locks are preserved."
_INCOMPLETE = "missing sealed result; incomplete operation authority and retry evidence will be permanently lost."
_FIELDS = {
    "schema",
    "record_id",
    "original",
    "quarantine",
    "booley_identity",
    "root_identity",
    "record_revision",
    "record_digest",
    "eligibility",
    "authority_mode",
}


def _identity(path: Path) -> list[int]:
    value = path.stat()
    return [value.st_dev, value.st_ino]


def _abandonment(operation: LifecycleOperation) -> None:
    value = operation.read_sealed("abandonment")
    if set(value) != {"notes", "closures"} or not all(
        isinstance(row, str) for row in require_list(value["notes"], field="notes")
    ):
        raise GoalStoreError("invalid abandonment closure authority")
    for raw in require_list(value["closures"], field="closures"):
        row = require_dict(raw, field="closure")
        if set(row) != {"proposal_id", "metadata", "payload_digest"}:
            raise GoalStoreError("invalid abandonment proposal association")
        unlinked(
            proposal_path(operation.lock.record_dir, row["proposal_id"]), operation.lock.record_dir
        )
        view = load_proposal(operation.lock.record_dir, row["proposal_id"])
        if {**view.metadata(), "closed_by_abandonment": None} != row[
            "metadata"
        ] or view.proposal.payload_digest != row["payload_digest"]:
            raise GoalStoreError("abandonment proposal differs from immutable association")


def _authority(store: GoalStore, record: GoalRecord, lock: RecordLock) -> str:
    if record.state is GoalState.FAILED:
        if record.finish_operation is not None or record.abandon_operation is not None:
            raise GoalStoreError("failed entry has unexpected lifecycle operation authority")
        return "failed-entry"
    operation_id = (
        record.finish_operation if record.state is GoalState.FINISHED else record.abandon_operation
    )
    if operation_id is None:
        raise GoalStoreError("legacy terminal operation authority unavailable")
    operation = LifecycleOperation(
        store,
        lock,
        LifecycleRequest(Path(record.worktree_path), record.id, operation_id),
        Path(record.worktree_path),
    )
    unlinked(operation.directory / "request.json", lock.record_dir)
    operation = replace(operation, request=read_bound_request(operation, operation_id))
    if operation.request.abandon != (record.state is GoalState.ABANDONED):
        raise GoalStoreError("lifecycle request does not match terminal state")
    if operation.request.abandon and (
        not operation.request.instruction_quote
        or operation.request.instruction_quote != record.end_instruction_quote
    ):
        raise GoalStoreError("abandonment instruction does not match retained request")
    result = operation.saved_result()
    if result is not None:
        if result["status"] != record.state.value:
            raise GoalStoreError("saved lifecycle result does not match terminal state")
        return "sealed-result"
    if record.state is GoalState.FINISHED:
        frozen = operation.read_sealed("attempt")
        validate_attempt(frozen, operation)
        validate_local_artifacts(frozen, operation, required=True)
    else:
        _abandonment(operation)
    return "incomplete-operation"


def _eligibility(
    store: GoalStore, root: Path, record_id: str, lock: RecordLock
) -> tuple[GoalRecord, int, str]:
    record = load_record(store, record_id)
    if record.state not in {GoalState.FINISHED, GoalState.ABANDONED, GoalState.FAILED}:
        raise GoalStoreError(f"{record.state.value} Goal Record cannot be pruned")
    if presence(record, root) != ("missing", "missing"):
        raise GoalStoreError(
            "prune requires positively missing worktree AND local Goal Branch; presence is present or unavailable"
        )
    paths = record_paths(store.booley_dir, record_id)
    size = logical_bytes(paths.root, entries=200_000, seconds=20)
    if size is None:
        raise GoalStoreError("record size scan unavailable/incomplete; cleanup refused")
    _metadata_unlinked(paths.root)
    try:
        if active_jobs(paths.jobs_dir):
            raise GoalStoreError("active record-owned Jobs prevent cleanup")
    except JobRecordError as exc:
        raise GoalStoreError(f"unreadable record-owned Jobs: {exc}") from exc
    _require_stopped_writers(paths.jobs_dir)
    return record, size, _authority(store, record, lock)


def _require_stopped_writers(jobs_root: Path) -> None:
    """A display deadline never proves that a producer stopped writing."""
    for job in strict_records(jobs_root):
        if job.pid is None:
            continue  # fresh PID-less admissions were checked by active_jobs
        identity = ProcessIdentity.from_payload(job.process_identity)
        if job.process_identity is not None:
            local = capture_process_identity(os.getpid())
            if (
                identity is None
                or identity.pid != job.pid
                or (
                    identity.identity_kind == "linux-procfs-start"
                    and (local is None or identity.identity_scope != local.identity_scope)
                )
            ):
                raise GoalStoreError("record-owned writer identity unavailable")
            state = observe_process(identity).state
            if state in {RUNNING, UNKNOWN}:
                raise GoalStoreError("live or unavailable record-owned writer prevents cleanup")
        elif is_pid_alive(job.pid):
            # Legacy argv mismatches can prove reuse; unreadable argv cannot.
            from booley.runtime.job_records import _proc_cmdline

            current = _proc_cmdline(job.pid)
            if not job.argv or current is None or current == job.argv:
                raise GoalStoreError(
                    "live or unavailable legacy record-owned writer prevents cleanup"
                )
            state = REUSED
        else:
            state = DEAD
        if state != REUSED and _owned_group_may_write(job.pid):
            raise GoalStoreError("live or unavailable record-owned writer group prevents cleanup")


def _owned_group_may_write(pid: int) -> bool:
    if not is_process_group_alive(ProcessGroup(pid)):
        return False
    # killpg includes zombies. Procfs can prove a group contains only nonwriters;
    # every unreadable member or unsupported host remains unavailable/refused.
    import sys

    if not sys.platform.startswith("linux"):
        return True
    try:
        entries = list(Path("/proc").iterdir())
        if len(entries) > 200_000:
            return True
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                continue
            if int(fields[2]) == pid and fields[0] != "Z":
                return True
        return False
    except (OSError, ValueError, IndexError):
        return True


def _metadata_unlinked(root: Path) -> None:
    # The bounded sizing pass has already limited the tree. Never traverse a child
    # link while checking persisted authority, including a linked metadata ancestor.
    remaining = 200_000
    import time

    deadline = time.monotonic() + 20
    for directory, dirs, files in os.walk(root, followlinks=False):
        remaining -= 1 + len(dirs) + len(files)
        if remaining < 0 or time.monotonic() >= deadline:
            raise GoalStoreError("metadata scan incomplete; cleanup refused")
        for name in [*dirs, *files]:
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                if name.endswith((".json", ".jsonl")) or name in {
                    "operations",
                    ".runtime",
                    "jobs",
                    "logs",
                    "proposals",
                }:
                    raise GoalStoreError(f"linked record metadata: {path}")
                if name in dirs:
                    dirs.remove(name)


def _read_intent(store: GoalStore, record_id: str) -> dict[str, Any]:
    marker = pruning_intent(store.booley_dir, record_id)
    unlinked(marker, store.booley_dir)
    saved = require_dict(json.loads(marker.read_bytes()), field="pruning intent")
    original = record_paths(store.booley_dir, record_id).root
    quarantine = pruning_root(store.booley_dir, record_id)
    if (
        set(saved) != _FIELDS
        or saved["schema"] != "booley.goal-pruning/v1"
        or saved["record_id"] != record_id
        or saved["original"] != str(original)
        or saved["quarantine"] != str(quarantine)
        or saved["booley_identity"] != _identity(store.booley_dir)
        or saved["eligibility"] != ["missing", "missing"]
        or saved["authority_mode"] not in {"failed-entry", "sealed-result", "incomplete-operation"}
    ):
        raise GoalStoreError("conflicting/malformed pruning intent; refusing adoption")
    if (
        not isinstance(saved["record_revision"], int)
        or isinstance(saved["record_revision"], bool)
        or saved["record_revision"] < 1
        or not isinstance(saved["record_digest"], str)
        or len(saved["record_digest"]) != 64
    ):
        raise GoalStoreError("malformed pruning record capture")
    identity = saved["root_identity"]
    if (
        not isinstance(identity, list)
        or len(identity) != 2
        or not all(type(value) is int and value >= 0 for value in identity)
    ):
        raise GoalStoreError("malformed pruning root identity")
    unlinked(original, store.booley_dir)
    unlinked(quarantine, store.booley_dir)
    if original.exists() and quarantine.exists():
        raise GoalStoreError("both original and quarantine exist; refusing cleanup")
    for path in (original, quarantine):
        if path.exists() and (not path.is_dir() or _identity(path) != identity):
            raise GoalStoreError("pruning root inode/device differs from intent")
    return saved


def _capture(store: GoalStore, record: GoalRecord, mode: str) -> dict[str, Any]:
    paths = record_paths(store.booley_dir, record.id)
    return {
        "schema": "booley.goal-pruning/v1",
        "record_id": record.id,
        "original": str(paths.root),
        "quarantine": str(pruning_root(store.booley_dir, record.id)),
        "booley_identity": _identity(store.booley_dir),
        "root_identity": _identity(paths.root),
        "record_revision": record.revision,
        "record_digest": hashlib.sha256(paths.record_file.read_bytes()).hexdigest(),
        "eligibility": ["missing", "missing"],
        "authority_mode": mode,
    }


def _validate_capture(
    saved: dict[str, Any], store: GoalStore, record: GoalRecord, mode: str
) -> None:
    if saved != _capture(store, record, mode):
        raise GoalStoreError("original record differs from validated pruning intent")


def _clear_intent(store: GoalStore, record_id: str) -> None:
    pruning_intent(store.booley_dir, record_id).unlink()
    fsync_directory(goals_root(store.booley_dir))


def _remove_tree(path: Path) -> None:
    """stdlib rmtree does not follow directory symlinks or Windows junctions."""
    shutil.rmtree(path)


def _preview(store: GoalStore, root: Path, record_id: str) -> dict[str, Any]:
    marker = pruning_intent(store.booley_dir, record_id)
    if marker.exists() or marker.is_symlink():
        saved = _read_intent(store, record_id)
        retained = (
            Path(saved["quarantine"])
            if Path(saved["quarantine"]).exists()
            else Path(saved["original"])
        )
        return {
            "record_id": record_id,
            "eligible": True,
            "size_bytes": logical_bytes(retained) if retained.exists() else 0,
            "path": str(retained),
            "authority_mode": saved["authority_mode"],
            "pending": True,
            "message": "Pending cleanup: confirmed retry resumes removal, or restores original record access if eligibility is lost. "
            + _EVIDENCE_LOSS,
        }
    if pruning_root(store.booley_dir, record_id).exists():
        raise GoalStoreError("unmarked quarantine; refusing adoption")
    # Preview never creates/acquires a file lock. The ephemeral proof carries only
    # the exact existing path for the read-only lifecycle validators.
    load_record(store, record_id)
    lock = RecordLock(store.booley_dir, record_id, (), current_lock_owner())
    _record, size, mode = _eligibility(store, root, record_id, lock)
    return {
        "record_id": record_id,
        "eligible": True,
        "size_bytes": size,
        "path": str(lock.record_dir),
        "authority_mode": mode,
        "pending": False,
        "message": _EVIDENCE_LOSS,
    }


def _confirm(store: GoalStore, root: Path, record_id: str) -> dict[str, Any]:
    with shared_prune_lock(store) as guard:
        marker = pruning_intent(store.booley_dir, record_id)
        saved = _read_intent(store, record_id) if marker.exists() else None
        original = record_paths(store.booley_dir, record_id).root
        quarantine = pruning_root(store.booley_dir, record_id)
        if original.exists():
            try:
                with store.record_lock(record_id, existing_only=True, _prune=guard) as lock:
                    record, _size, mode = _eligibility(store, root, record_id, lock)
                    if saved is not None:
                        _validate_capture(saved, store, record, mode)
                    else:
                        if quarantine.exists() or quarantine.is_symlink():
                            raise GoalStoreError("unmarked quarantine; refusing adoption")
                        saved = _capture(store, record, mode)
                        atomic_write_once(marker, json.dumps(saved, sort_keys=True).encode())
                original.rename(quarantine)
                fsync_directory(original.parent)
            except Exception:
                if (
                    saved is not None
                    and original.exists()
                    and not quarantine.exists()
                    and _identity(original) == saved["root_identity"]
                ):
                    _clear_intent(store, record_id)
                raise
        elif saved is None:
            raise GoalStoreError("no validated record or pruning intent")
        if quarantine.exists():
            _read_intent(store, record_id)
            _remove_tree(quarantine)
            fsync_directory(quarantine.parent)
        _clear_intent(store, record_id)
        return {
            "record_id": record_id,
            "removed": True,
            "authority_mode": saved["authority_mode"],
            "message": _EVIDENCE_LOSS,
        }


def prune_record(
    store: GoalStore, root: Path, record_id: str, *, confirm: bool = False
) -> dict[str, Any]:
    """Preview exactly one record; confirmation revalidates under locks before removal."""
    from booley.goals.paths import validate_goal_id

    validate_goal_id(record_id)
    unlinked(goals_root(store.booley_dir), store.booley_dir)
    try:
        result = _confirm(store, root, record_id) if confirm else _preview(store, root, record_id)
        if result["authority_mode"] == "incomplete-operation":
            result["message"] += " " + _INCOMPLETE
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GoalStoreError(f"prune {record_id} refused: {exc}") from exc
