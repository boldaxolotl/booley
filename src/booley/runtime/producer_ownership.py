"""Durable neutral ownership of an admitted process lineage.

A terminal endpoint is separate from proof that every descendant stopped.
Only a matching supervisor completion permits the latter observation.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from booley.core.boundary import require_int, require_list, require_str
from booley.runtime.atomic_files import atomic_write_once
from booley.runtime.pid import RUNNING, ProcessIdentity, capture_process_identity, observe_process
from booley.runtime.process_group import ProcessGroup
from booley.runtime.safe_storage import refuse_symlinks


@dataclass(frozen=True)
class ProducerObservation:
    """Physical writer observation, independent of any owner's deletion policy."""

    state: Literal["stopped", "live", "unavailable"]
    reason: str


def root_identity(root: Path) -> list[int]:
    """Capture the existing storage inode without following links."""
    refuse_symlinks(root)
    for path in (root, *root.parents):
        if getattr(path, "is_junction", lambda: False)():
            raise OSError(f"junction producer storage is unavailable: {path}")
    stat = root.stat()
    if not root.is_dir():
        raise ValueError("producer storage is not a directory")
    return [stat.st_dev, stat.st_ino]


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def new_ticket(run_id: str, runtime: Path, *, timeout_s: int = 60) -> dict[str, Any]:
    """Persist pending ownership before a caller schedules asynchronous creation."""
    server = capture_process_identity(os.getpid())
    directory = runtime / "producer-ownership" / run_id
    refuse_symlinks(directory)
    directory.mkdir(parents=True, exist_ok=False)
    ticket = {
        "schema": "booley.producer-ownership/v1",
        "run_id": run_id,
        "token": uuid.uuid4().hex,
        "runtime": str(runtime.resolve()),
        "root_identity": root_identity(runtime),
        "record_identity": root_identity(runtime.parent),
        "directory_identity": root_identity(directory),
        "boot_id": _boot_id(),
        "owner": None if server is None else server.to_payload(),
        "supervisor": None,
        "endpoint": None,
        "group": None,
        "capable": None,
        "timeout_s": timeout_s,
    }
    payload = json.dumps(ticket, sort_keys=True).encode()
    try:
        atomic_write_once(directory / "pending.json", payload)
    except (OSError, ValueError):
        _discard_unpublished_pending(directory, payload)
        raise
    return ticket


def ownership_directory(ticket: dict[str, Any]) -> Path:
    """Return only a validated, still-identical ownership destination."""
    _validate_ticket_fields(ticket)
    runtime = Path(ticket["runtime"])
    directory = runtime / "producer-ownership" / ticket["run_id"]
    if (
        ticket["schema"] != "booley.producer-ownership/v1"
        or Path(ticket["run_id"]).name != ticket["run_id"]
        or len(ticket["token"]) != 32
        or ticket["record_identity"] != root_identity(runtime.parent)
        or ticket["root_identity"] != root_identity(runtime)
        or ticket["directory_identity"] != root_identity(directory)
        or not ticket["boot_id"]
        or ticket["boot_id"] != _boot_id()
    ):
        raise ValueError("producer ownership storage differs from admission")
    pending = {**ticket, "supervisor": None, "endpoint": None, "group": None, "capable": None}
    source = directory / "pending.json"
    refuse_symlinks(source)
    if json.loads(source.read_bytes()) != pending:
        raise ValueError("producer handoff differs from immutable pending admission")
    entries = {entry.name for entry in directory.iterdir()}
    if not entries <= {"pending.json", "handoff.json", "complete.json", "complete.tmp"}:
        raise ValueError("unrecognized producer ownership entries")
    if "complete.tmp" in entries and "complete.json" in entries:
        raise ValueError("conflicting producer completion publication")
    local = capture_process_identity(os.getpid())
    owner = ProcessIdentity.from_payload(ticket["owner"])
    if local is None or owner is None or local.identity_scope != owner.identity_scope:
        raise ValueError("producer ownership namespace unavailable")
    return directory


def completion_payload(ticket: dict[str, Any]) -> dict[str, Any]:
    """Bind the immutable completion to this exact durable handoff."""
    return {"schema": "booley.producer-completion/v1", "ticket": ticket, "all_reaped": True}


def observe_producer(ticket: object, runtime: Path, run_id: str) -> ProducerObservation:
    """Observe the full lineage; PID disappearance never substitutes for completion."""
    try:
        if not isinstance(ticket, dict) or ticket["run_id"] != run_id:
            raise ValueError("producer ownership association unavailable")
        if Path(ticket["runtime"]) != runtime.resolve():
            raise ValueError("producer ownership runtime differs from Job storage")
        directory = ownership_directory(ticket)
        supervisor = ProcessIdentity.from_payload(ticket["supervisor"])
        if supervisor is not None:
            owner = ProcessIdentity.from_payload(ticket["owner"])
            endpoint = ProcessIdentity.from_payload(ticket["endpoint"])
            if (
                owner is None
                or endpoint is None
                or supervisor.identity_scope != owner.identity_scope
                or endpoint.identity_scope != owner.identity_scope
                or ticket.get("group") != supervisor.pid
                or ticket.get("capable") is not True
            ):
                raise ValueError("producer handoff identity/capability unavailable")
            require_handoff(ticket)
            proof = directory / "complete.json"
            refuse_symlinks(proof)
            if proof.exists():
                if json.loads(proof.read_bytes()) != completion_payload(ticket):
                    raise ValueError("producer completion differs from exact handoff")
                return ProducerObservation("stopped", "all adopted descendants reaped")
        identity = supervisor or ProcessIdentity.from_payload(ticket["owner"])
        if identity is not None and observe_process(identity).state == RUNNING:
            return ProducerObservation("live", "admitted producer ownership remains live")
        raise ValueError("producer completion unavailable")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ProducerObservation("unavailable", str(exc))


def live_producer_group(ticket: object, runtime: Path, run_id: str) -> ProcessGroup | None:
    """Return a group only while its exact captured supervisor still owns it."""
    if observe_producer(ticket, runtime, run_id).state != "live" or not isinstance(ticket, dict):
        return None
    identity = ProcessIdentity.from_payload(ticket.get("supervisor"))
    if identity is None or capture_process_identity(identity.pid) != identity:
        return None
    try:
        if os.getpgid(identity.pid) != ticket.get("group"):
            return None
    except OSError:
        return None
    return ProcessGroup(identity.pid) if ticket.get("group") == identity.pid else None


def discard_pending(ticket: dict[str, Any]) -> None:
    """Roll back this caller's never-scheduled admission, under its storage guard."""
    if ticket["supervisor"] is not None or ticket["endpoint"] is not None:
        raise ValueError("handed-off producer ownership cannot be discarded")
    runtime = Path(ticket["runtime"])
    directory = runtime / "producer-ownership" / ticket["run_id"]
    if (
        ticket["root_identity"] != root_identity(runtime)
        or ticket["record_identity"] != root_identity(runtime.parent)
        or ticket["directory_identity"] != root_identity(directory)
    ):
        raise ValueError("never-scheduled producer storage changed")
    source = directory / "pending.json"
    refuse_symlinks(source)
    if json.loads(source.read_bytes()) != ticket or (directory / "handoff.json").exists():
        raise ValueError("pending admission no longer exclusively owned")
    source.unlink()
    directory.rmdir()


def publish_handoff(ticket: dict[str, Any]) -> None:
    """Persist the exact parked endpoint/supervisor association before release."""
    directory = ownership_directory(ticket)
    destination = directory / "handoff.json"
    refuse_symlinks(destination)
    atomic_write_once(destination, json.dumps(ticket, sort_keys=True).encode())


def require_handoff(ticket: dict[str, Any]) -> None:
    """Reject missing, replaced or mismatched durable handoff authority."""
    source = ownership_directory(ticket) / "handoff.json"
    refuse_symlinks(source)
    if json.loads(source.read_bytes()) != ticket:
        raise ValueError("producer handoff differs from immutable association")


def _validate_ticket_fields(ticket: dict[str, Any]) -> None:
    fields = {
        "schema",
        "run_id",
        "token",
        "runtime",
        "root_identity",
        "record_identity",
        "directory_identity",
        "boot_id",
        "owner",
        "supervisor",
        "endpoint",
        "group",
        "capable",
        "timeout_s",
    }
    if set(ticket) != fields:
        raise ValueError("producer ownership fields unavailable")
    for key in ("schema", "run_id", "token", "runtime", "boot_id"):
        require_str(ticket, key)
    if uuid.UUID(hex=ticket["token"]).hex != ticket["token"]:
        raise ValueError("producer launch token is not canonical")
    for key in ("root_identity", "record_identity", "directory_identity"):
        values = require_list(ticket[key], field=key)
        if len(values) != 2 or any(require_int(value, field=key) < 0 for value in values):
            raise ValueError("producer storage identity is invalid")
    if require_int(ticket["timeout_s"], field="producer timeout") <= 0:
        raise ValueError("producer timeout must be positive")
    owner = ProcessIdentity.from_payload(ticket["owner"])
    if owner is None or owner.pid <= 0 or owner.start_token < 0:
        raise ValueError("producer admission owner identity unavailable")
    if ticket["supervisor"] is None:
        if any(ticket[key] is not None for key in ("endpoint", "group", "capable")):
            raise ValueError("partial producer handoff")
    elif type(ticket["capable"]) is not bool:
        raise ValueError("producer handoff capability unavailable")


def observe_producer_inventory(runtime: Path) -> list[ProducerObservation]:
    """Observe every private ticket, including admissions missing their Job JSON."""
    root = runtime / "producer-ownership"
    try:
        refuse_symlinks(root)
        try:
            root.stat()
        except FileNotFoundError:
            return []
        root_identity(root)
        observations = []
        for directory in root.iterdir():
            root_identity(directory)
            source = directory / "pending.json"
            refuse_symlinks(source)
            ticket = json.loads(source.read_bytes())
            if not isinstance(ticket, dict) or ticket.get("run_id") != directory.name:
                raise ValueError("producer inventory association unavailable")
            handoff = directory / "handoff.json"
            refuse_symlinks(handoff)
            with contextlib.suppress(FileNotFoundError):
                ticket = json.loads(handoff.read_bytes())
            observations.append(observe_producer(ticket, runtime, directory.name))
        return observations
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [ProducerObservation("unavailable", f"producer inventory unavailable: {exc}")]


def _discard_unpublished_pending(directory: Path, payload: bytes) -> None:
    source = directory / "pending.json"
    with contextlib.suppress(OSError, ValueError):
        refuse_symlinks(source)
        if source.read_bytes() == payload:
            source.unlink()
    with contextlib.suppress(OSError):
        directory.rmdir()
