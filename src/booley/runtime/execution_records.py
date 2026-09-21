"""Durable protocol records for one supervised Sandbox Attachment execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
        project_data = paths.root.parents[2]
        entry = (
            project_data
            / ".runtime"
            / "campaign-child-executions"
            / "entries"
            / f"{execution_id}.json"
        )
        return not entry.exists()
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
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
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


def release_retired_campaign_children(
    project_dir: Path, campaign_children: Path
) -> tuple[str, ...]:
    """Release exact retired child index pairs after campaign retention.

    The campaign mirror remains authoritative until its enclosing invocation is
    removed.  Every Project-local byte that still exists must match that mirror
    before any index entry is unlinked, making retries after partial cleanup
    safe without accepting a substituted Project-local record.
    """
    campaign_entries = campaign_children / "entries"
    campaign_retired = campaign_children / "retired"
    if not campaign_entries.exists():
        return ()
    if campaign_entries.is_symlink() or not campaign_entries.is_dir():
        raise ValueError("campaign child entries are not a regular directory")
    project_root = project_dir / ".runtime" / "campaign-child-executions"
    releases: list[tuple[ExecutionId, Path, Path]] = []
    for mirror_entry in sorted(campaign_entries.glob("*.json")):
        execution_id = ExecutionId(mirror_entry.stem)
        mirror_retirement = campaign_retired / mirror_entry.name
        entry_raw = _regular_bytes(mirror_entry)
        retirement_raw = _regular_bytes(mirror_retirement)
        _validate_retired_pair(execution_id, entry_raw, retirement_raw)
        project_entry = project_root / "entries" / mirror_entry.name
        project_retirement = project_root / "retired" / mirror_entry.name
        marker = campaign_children / "released" / mirror_entry.name
        marker_raw = _release_marker(execution_id, entry_raw, retirement_raw)
        _require_matching_if_present(project_entry, entry_raw)
        _require_matching_if_present(project_retirement, retirement_raw)
        _require_child_token_absent(project_dir, execution_id)
        if marker.exists() or marker.is_symlink():
            if _regular_bytes(marker) != marker_raw:
                raise ValueError("campaign child release marker is contradictory")
        else:
            if not project_entry.is_file() or not project_retirement.is_file():
                raise ValueError("Project child retirement pair disappeared before release")
            _require_retirement_terminal(project_dir, execution_id, retirement_raw)
            _durable_create(marker, marker_raw)
        releases.append((execution_id, project_entry, project_retirement))
    for _execution_id, entry, retirement in releases:
        retirement.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)
    return tuple(str(item[0]) for item in releases)


def _regular_bytes(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"required child record is unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"child record is not a regular file: {path}")
    return path.read_bytes()


def _require_matching_if_present(path: Path, expected: bytes) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if _regular_bytes(path) != expected:
        raise ValueError(f"Project child record disagrees with campaign mirror: {path}")


def _validate_retired_pair(
    execution_id: ExecutionId, entry_raw: bytes, retirement_raw: bytes
) -> None:
    try:
        entry = json.loads(entry_raw)
        retirement = json.loads(retirement_raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("campaign child retirement pair is invalid JSON") from exc
    entry_digest = "sha256:" + hashlib.sha256(entry_raw).hexdigest()
    if (
        not isinstance(entry, dict)
        or entry.get("$schema") != "booley.simulation-campaign-child-entry/v1"
        or entry.get("child_execution_id") != execution_id
        or not isinstance(retirement, dict)
        or retirement.get("$schema")
        != "booley.simulation-campaign-child-retirement/v1"
        or retirement.get("child_execution_id") != execution_id
        or retirement.get("entry_sha256") != entry_digest
        or retirement.get("token_absent") is not True
    ):
        raise ValueError("campaign child retirement pair has contradictory identity")


def _release_marker(
    execution_id: ExecutionId, entry_raw: bytes, retirement_raw: bytes
) -> bytes:
    document = {
        "$schema": "booley.simulation-campaign-child-release/v1",
        "child_execution_id": execution_id,
        "entry_sha256": "sha256:" + hashlib.sha256(entry_raw).hexdigest(),
        "retirement_sha256": "sha256:" + hashlib.sha256(retirement_raw).hexdigest(),
    }
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _durable_create(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _require_child_token_absent(project_dir: Path, execution_id: ExecutionId) -> None:
    slots = project_dir / "runtime" / "jobs" / "slots"
    for path in slots.glob("*/*.json"):
        payload = read_json(path)
        if payload is not None and payload.get("execution_id") == execution_id:
            raise ValueError("retired campaign child still owns a Job Slot token")


def _require_retirement_terminal(
    project_dir: Path, execution_id: ExecutionId, retirement_raw: bytes
) -> None:
    retirement = json.loads(retirement_raw)
    paths = execution_paths(execution_id, project_dir=project_dir)
    terminal = read_json(paths.record)
    if (
        terminal is None
        or terminal.get("state") != "terminal"
        or terminal.get("tree_terminal") is not True
    ):
        raise ValueError("retired campaign child lacks tree-terminal proof")
    canonical = (
        json.dumps(
            terminal,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
    if retirement.get("execution_terminal_sha256") != digest:
        raise ValueError("campaign child retirement terminal digest is invalid")


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
