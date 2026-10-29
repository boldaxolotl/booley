"""Durable protocol records for one supervised Sandbox Attachment execution."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.filesystem_utils import (
    is_transient_read_error,
    read_file_bytes,
    replace_file,
)
from booley.runtime.pid import RUNNING, ProcessIdentity, ProcessObservation, observe_process
from booley.runtime.process_tree import has_ancestor
from booley.runtime.timefmt import utc_now_rfc3339

PROTOCOL_VERSION = 1
RUNTIME_EXECUTION_ENV = "BOOLEY_RUNTIME_EXECUTION_ID"
EXECUTION_RETENTION_SECONDS = 7 * 24 * 60 * 60
_EXECUTION_ID_RE = re.compile(r"[0-9a-f]{32}")
_PROTOCOL_FILENAMES = {
    "record.json",
    "cancel.json",
    "force-cancel",
    "attachment-heartbeat",
    "context.json",
}
_TERMINAL_FIELDS = {
    "schema_version",
    "state",
    "runtime_identity",
    "supervisor",
    "leader",
    "exit_code",
    "tree_terminal",
    "terminal_cause",
    "updated_at",
}


class ExecutionId(str):
    """Validated opaque identity shared by one Sandbox Attachment execution."""

    def __new__(cls, value: object) -> ExecutionId:
        if not isinstance(value, str) or _EXECUTION_ID_RE.fullmatch(value) is None:
            raise ValueError("execution_id must be 32 lowercase hexadecimal characters")
        return str.__new__(cls, value)


@dataclass(frozen=True)
class ExecutionPaths:
    """Files shared by the host attachment and in-Sandbox supervisor."""

    root: Path
    record: Path
    cancel: Path
    force: Path
    heartbeat: Path
    context: Path


def execution_paths(
    execution_id: str | ExecutionId, *, booley_dir: Path | None = None
) -> ExecutionPaths:
    """Resolve protocol paths for one validated opaque execution ID."""
    validated_id = ExecutionId(execution_id)
    resolved = booley_dir if booley_dir is not None else resolve_booley_dir()
    root = resolved / ".runtime" / "executions" / validated_id
    return ExecutionPaths(
        root=root,
        record=root / "record.json",
        cancel=root / "cancel.json",
        force=root / "force-cancel",
        heartbeat=root / "attachment-heartbeat",
        context=root / "context.json",
    )


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Durably replace one protocol JSON file with canonical content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_protocol_bytes(path: Path) -> bytes | None:
    """Read a protocol file through the sharing-retrying reader.

    Missing or otherwise unreadable input is ``None`` (indeterminate). A Windows
    sharing error that outlasts the retry budget raises ``PermissionError``
    instead, so it is never mistaken for an absent file.
    """
    try:
        return read_file_bytes(path)
    except OSError as exc:
        if is_transient_read_error(exc):
            raise
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    """Read one protocol object; corrupt or missing input is indeterminate.

    A sharing error that outlasts the retry budget raises ``PermissionError``.
    """
    raw = _read_protocol_bytes(path)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def read_slot_entry(
    path: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """Read one Job Slot entry, keeping "missing" apart from "unreadable".

    Returns the payload, or ``None`` when the file is missing or its content is
    corrupt (not UTF-8, not JSON, not an object). Any other ``OSError``,
    including a Windows sharing error that outlasted the shared retry budget,
    propagates so the caller can fail closed instead of reading "no token".
    """
    try:
        payload = json.loads(read_file_bytes(path, sleep=sleep).decode("utf-8"))
    except (FileNotFoundError, ValueError):  # ValueError: bad UTF-8 or bad JSON
        return None
    return payload if isinstance(payload, dict) else None


def verify_inherited_execution_id(
    raw_execution_id: object,
    *,
    booley_dir: Path,
    owner_pid: int,
    observe_identity: Callable[[ProcessIdentity], ProcessObservation] = observe_process,
    ancestor_check: Callable[[int, int], bool] = has_ancestor,
) -> ExecutionId | None:
    """Validate a Project-local inherited execution owner, failing closed."""
    try:
        execution_id = ExecutionId(raw_execution_id)
    except ValueError:
        return None
    record = read_json(execution_paths(execution_id, booley_dir=booley_dir).record)
    if record is None or set(record) != _TERMINAL_FIELDS:
        return None
    if (
        record.get("schema_version") != PROTOCOL_VERSION
        or record.get("state") not in {"starting", "running", "cancelling"}
        or record.get("tree_terminal") is not False
        or record.get("exit_code") is not None
        or record.get("terminal_cause") is not None
        or not isinstance(record.get("updated_at"), str)
        or not record["updated_at"]
    ):
        return None
    supervisor = ProcessIdentity.from_payload(record.get("supervisor"))
    leader = record.get("leader")
    if (
        supervisor is None
        or record.get("runtime_identity") != supervisor.identity_scope
        or (leader is not None and ProcessIdentity.from_payload(leader) is None)
        or observe_identity(supervisor).state is not RUNNING
    ):
        return None
    if owner_pid != supervisor.pid and not ancestor_check(owner_pid, supervisor.pid):
        return None
    return execution_id


def child_context_matches(paths: ExecutionPaths, execution_id: ExecutionId) -> bool:
    """Authenticate an optional immutable campaign-child context sidecar.

    A missing sidecar is an ordinary execution; a present one must carry exactly the
    campaign-child fields and name this execution. A leftover Project-local child
    registry from Booley 0.3.0 is ignored.
    """
    if not paths.context.exists():
        return True
    payload = read_json(paths.context)
    expected = {
        "$schema",
        "child_execution_id",
        "parent_execution_id",
        "campaign_id",
        "manifest_sha256",
        "work_item_id",
        "attempt_id",
        "attempt_ordinal",
    }
    return bool(
        payload is not None
        and set(payload) == expected
        and payload.get("$schema") == "booley.supervised-child-context/v1"
        and payload.get("child_execution_id") == execution_id
    )


def write_attachment_heartbeat(paths: ExecutionPaths, *, generation: int) -> None:
    """Publish a monotonically changing attachment generation."""
    paths.root.mkdir(parents=True, exist_ok=True)
    tmp = paths.heartbeat.with_name(f".{paths.heartbeat.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{generation}\n", encoding="ascii")
    try:
        replace_file(tmp, paths.heartbeat)
    except PermissionError:
        with suppress(OSError):
            tmp.unlink()


def read_attachment_heartbeat(paths: ExecutionPaths) -> int | None:
    """Read the current attachment generation without comparing host clocks.

    A sharing error that outlasts the retry budget raises ``PermissionError``.
    """
    raw = _read_protocol_bytes(paths.heartbeat)
    if raw is None:
        return None
    try:
        generation = int(raw.decode("ascii").strip())
    except ValueError:
        return None
    return generation if generation >= 0 else None


def request_cancellation(
    paths: ExecutionPaths,
    *,
    force: bool = False,
    signum: int = 2,
    reason: str = "cancelled",
) -> None:
    """Durably and idempotently request cancellation of one execution."""
    if force:
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.force.touch(exist_ok=True)
    try:
        current = read_json(paths.cancel)
    except OSError:
        # A sharing error that outlasted the read retries: the request is
        # idempotent, so write it again rather than abort a cancellation loop.
        current = None
    force = force or paths.force.exists()
    if current is not None and (not force or current.get("force") is True):
        return
    atomic_write_json(
        paths.cancel,
        {
            "force": force,
            "reason": reason,
            "signum": signum,
            "requested_at": utc_now_rfc3339(),
        },
    )


def force_cancellation_requested(paths: ExecutionPaths) -> bool:
    """Return whether any writer durably escalated this cancellation."""
    return paths.force.exists()


def _referenced_execution_ids(booley_dir: Path) -> set[str] | None:
    """Execution IDs pinned by live claims, or ``None`` when a slot entry is unreadable."""
    references: set[str] = set()
    slots = booley_dir / "runtime" / "jobs" / "slots"
    for path in slots.glob("*/*.json"):
        try:
            payload = read_slot_entry(path)
        except OSError:
            return None
        execution_id = payload.get("execution_id") if payload is not None else None
        try:
            references.add(ExecutionId(execution_id))
        except ValueError:
            continue
    return references


def _is_complete_terminal_record(record: Path) -> bool:
    """True for a readable, tree-terminal record; one we cannot read is never collectable."""
    try:
        payload = read_json(record)
    except OSError:
        return False
    return (
        payload is not None
        and payload.get("state") == "terminal"
        and payload.get("tree_terminal") is True
    )


def _remove_terminal_record(root: Path, *, cutoff: float) -> bool:
    record = root / "record.json"
    if not _is_complete_terminal_record(record):
        return False
    try:
        entries = list(root.iterdir())
        if record.stat().st_mtime >= cutoff:
            return False
    except OSError:
        return False
    if any(entry.name not in _PROTOCOL_FILENAMES or not entry.is_file() for entry in entries):
        return False
    try:
        for entry in entries:
            entry.unlink()
        root.rmdir()
    except OSError:
        return False
    return True


def gc_terminal_executions(
    booley_dir: Path,
    *,
    now: float | None = None,
    retention_seconds: float = EXECUTION_RETENTION_SECONDS,
) -> list[str]:
    """Remove old complete records not pinned by any current Job lease."""
    executions = booley_dir / ".runtime" / "executions"
    if not executions.is_dir():
        return []
    referenced = _referenced_execution_ids(booley_dir)
    if referenced is None:
        return []  # a pin may hide in the unreadable entry: remove nothing this pass
    cutoff = (time.time() if now is None else now) - retention_seconds
    removed: list[str] = []
    for root in executions.iterdir():
        try:
            execution_id = ExecutionId(root.name)
        except ValueError:
            continue
        if execution_id in referenced:
            continue
        if root.is_dir() and _remove_terminal_record(root, cutoff=cutoff):
            removed.append(execution_id)
    return sorted(removed)
