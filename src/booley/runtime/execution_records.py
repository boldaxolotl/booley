"""Durable protocol records for one supervised Sandbox Attachment execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.runtime.project_dir import resolve_project_dir
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
    execution_id: str | ExecutionId, *, project_dir: Path | None = None
) -> ExecutionPaths:
    """Resolve protocol paths for one validated opaque execution ID."""
    validated_id = ExecutionId(execution_id)
    resolved = project_dir if project_dir is not None else resolve_project_dir()
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
    """Atomically replace one protocol JSON file with canonical content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    """Read one protocol object; corrupt or missing input is indeterminate."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def child_context_matches(paths: ExecutionPaths, execution_id: ExecutionId) -> bool:
    """Authenticate an optional immutable campaign-child context sidecar."""
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
    context_valid = bool(
        payload is not None
        and set(payload) == expected
        and payload.get("$schema") == "booley.supervised-child-context/v1"
        and payload.get("child_execution_id") == execution_id
    )
    if not context_valid:
        return False
    return _child_entry_matches(paths, execution_id, payload)


def _child_entry_matches(
    paths: ExecutionPaths, execution_id: ExecutionId, context: dict[str, Any]
) -> bool:
    project_data = paths.root.parents[2]
    entry_path = (
        project_data
        / ".runtime"
        / "campaign-child-executions"
        / "entries"
        / f"{execution_id}.json"
    )
    entry = read_json(entry_path)
    if entry is None:
        return False
    try:
        raw = paths.context.read_bytes()
    except OSError:
        return False
    digest = "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
    linked = (
        "parent_execution_id",
        "campaign_id",
        "manifest_sha256",
        "work_item_id",
        "attempt_id",
        "attempt_ordinal",
    )
    return bool(
        entry.get("$schema") == "booley.simulation-campaign-child-entry/v1"
        and entry.get("child_execution_id") == execution_id
        and entry.get("runtime_context_sha256") == digest
        and all(entry.get(field) == context.get(field) for field in linked)
    )


def write_attachment_heartbeat(paths: ExecutionPaths, *, generation: int) -> None:
    """Publish a monotonically changing attachment generation."""
    paths.root.mkdir(parents=True, exist_ok=True)
    tmp = paths.heartbeat.with_name(f".{paths.heartbeat.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{generation}\n", encoding="ascii")
    try:
        tmp.replace(paths.heartbeat)
    except PermissionError:
        with suppress(OSError):
            tmp.unlink()


def read_attachment_heartbeat(paths: ExecutionPaths) -> int | None:
    """Read the current attachment generation without comparing host clocks."""
    try:
        generation = int(paths.heartbeat.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
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
    current = read_json(paths.cancel)
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


def _referenced_execution_ids(project_dir: Path) -> set[str]:
    references: set[str] = set()
    slots = project_dir / "runtime" / "jobs" / "slots"
    for path in slots.glob("*/*.json"):
        payload = read_json(path)
        execution_id = payload.get("execution_id") if payload is not None else None
        try:
            references.add(ExecutionId(execution_id))
        except ValueError:
            continue
    child_entries = project_dir / ".runtime" / "campaign-child-executions" / "entries"
    child_retired = project_dir / ".runtime" / "campaign-child-executions" / "retired"
    for path in child_entries.glob("*.json"):
        if (child_retired / path.name).is_file():
            continue
        try:
            references.add(ExecutionId(path.stem))
        except ValueError:
            continue
    return references


def _remove_terminal_record(root: Path, *, cutoff: float) -> bool:
    record = root / "record.json"
    payload = read_json(record)
    if (
        payload is None
        or payload.get("state") != "terminal"
        or payload.get("tree_terminal") is not True
    ):
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
    project_dir: Path,
    *,
    now: float | None = None,
    retention_seconds: float = EXECUTION_RETENTION_SECONDS,
) -> list[str]:
    """Remove old complete records not pinned by any current Job lease."""
    executions = project_dir / ".runtime" / "executions"
    if not executions.is_dir():
        return []
    referenced = _referenced_execution_ids(project_dir)
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
