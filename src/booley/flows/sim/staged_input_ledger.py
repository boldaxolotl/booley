"""Durable ownership record for runtime inputs a run stages into a run directory.

A run that is killed or interrupted cannot unwind its ``finally`` blocks, so the
inputs it staged would stay behind and break the next run.  Each staging
context therefore records, before it creates a destination, what it is about to
create and who owns the run; it confirms the entry with the created object's
``(st_dev, st_ino)`` right after.  A later start sweeps the records whose owner
is observably gone and removes only the very object a confirmed entry names.
Anything else is left in place and stays recorded, so the next run can say a
cancelled Booley run left it there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.process_identity import ProcessIdentity
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.filesystem_utils import make_directories
from booley.runtime.pid import DEAD_PROCESS_STATES, capture_process_identity, observe_process

logger = logging.getLogger(__name__)

RECORD_KIND = "booley-staged-runtime-inputs"
RECORD_VERSION = 1
_MAX_RECORD_BYTES = 1024 * 1024
_ENTRY_KINDS = frozenset({"file", "link", "directory"})


def _record_root(booley_dir: Path) -> Path:
    return booley_dir / ".runtime" / "staged-runtime-inputs"


@dataclass
class _Entry:
    path: str
    kind: str
    identity: str | None
    created: tuple[int, int] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind,
            "created": list(self.created) if self.created else None,
        }
        if self.identity is not None:
            payload["identity"] = self.identity
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> _Entry | None:
        if not isinstance(payload, dict):
            return None
        path, kind = payload.get("path"), payload.get("kind")
        identity, created = payload.get("identity"), payload.get("created")
        if not isinstance(path, str) or kind not in _ENTRY_KINDS:
            return None
        if identity is not None and not isinstance(identity, str):
            return None
        if created is not None and not (
            isinstance(created, list)
            and len(created) == 2
            and all(isinstance(value, int) for value in created)
        ):
            return None
        return cls(path, kind, identity, (created[0], created[1]) if created else None)


def _publish(record: Path, owner: ProcessIdentity, run_dir: Path, entries: list[_Entry]) -> None:
    document = {
        "kind": RECORD_KIND,
        "version": RECORD_VERSION,
        "owner": owner.to_payload(),
        "run_directory": str(run_dir),
        "entries": [entry.to_payload() for entry in entries],
    }
    atomic_replace_bytes(record, json.dumps(document).encode("utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def leftover_message(relative: Path | str) -> str:
    return (
        f"run input conflicts with an existing path: {Path(relative).as_posix()}; "
        "a cancelled Booley run left it there and it is safe to delete"
    )


class StagedInputLedger:
    """Record what one staging context creates in one run directory."""

    def __init__(self, booley_dir: Path, run_dir: Path) -> None:
        self._run_dir = run_dir.resolve()
        self._owner = capture_process_identity(os.getpid())
        self._record = _record_root(booley_dir) / f"{uuid.uuid4().hex}.json"
        self._entries: list[_Entry] = []
        self._retained = sweep_cancelled_run_inputs(booley_dir)

    def will_create(self, relative: Path, *, kind: str, identity: str | None) -> None:
        """Publish a pending entry before the destination is created."""
        if self._owner is None:
            return
        make_directories(self._record.parent)
        self._entries.append(_Entry(relative.as_posix(), kind, identity))
        _publish(self._record, self._owner, self._run_dir, self._entries)

    def created(self, relative: Path) -> None:
        """Confirm the pending entry with the identity of the object just created."""
        if self._owner is None:
            return
        status = os.lstat(self._run_dir / relative)
        for entry in reversed(self._entries):
            if entry.path == relative.as_posix() and entry.created is None:
                entry.created = (status.st_dev, status.st_ino)
                break
        _publish(self._record, self._owner, self._run_dir, self._entries)

    def is_leftover(self, relative: Path) -> bool:
        """Whether *relative* holds a retained leftover of a cancelled run."""
        return self._run_dir / relative in self._retained

    def discard(self) -> None:
        """Delete this ledger's record after a normal unwind removed everything."""
        self._record.unlink(missing_ok=True)


def sweep_cancelled_run_inputs(booley_dir: Path) -> frozenset[Path]:
    """Remove leftovers of records whose owner is gone; return those left behind."""
    current = capture_process_identity(os.getpid())
    if current is None:
        return frozenset()
    retained: set[Path] = set()
    try:
        records = sorted(_record_root(booley_dir).glob("*.json"))
    except OSError:
        return frozenset()
    for record in records:
        try:
            retained |= _sweep_record(record, current.identity_scope)
        except OSError as exc:
            logger.warning("Could not sweep staged runtime inputs %s: %s", record, exc)
    return frozenset(retained)


def _read_record(record: Path) -> tuple[ProcessIdentity, Path, list[_Entry]] | None:
    status = record.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_RECORD_BYTES:
        return None
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("kind") != RECORD_KIND
        or data.get("version") != RECORD_VERSION
        or not isinstance(data.get("run_directory"), str)
        or not isinstance(data.get("entries"), list)
    ):
        return None
    owner = ProcessIdentity.from_payload(data.get("owner"))
    parsed = [_Entry.from_payload(item) for item in data["entries"]]
    if owner is None or None in parsed:
        return None
    run_dir = Path(data["run_directory"])
    if not run_dir.is_absolute():
        return None
    return owner, run_dir, [entry for entry in parsed if entry is not None]


def _sweep_record(record: Path, scope: str) -> set[Path]:
    try:
        parsed = _read_record(record)
    except FileNotFoundError:
        return set()
    if parsed is None:
        return set()
    owner, run_dir, entries = parsed
    # A PID from another PID namespace reads as dead whether or not it runs.
    if owner.identity_scope != scope or observe_process(owner).state not in DEAD_PROCESS_STATES:
        return set()
    kept: list[_Entry] = []
    retained: set[Path] = set()
    for entry in reversed(entries):
        if entry.created is None:
            continue
        destination = run_dir / entry.path
        if _remove_entry(run_dir, entry, destination, retained):
            kept.append(entry)
            retained.add(destination)
    kept.reverse()
    if kept:
        _publish(record, owner, run_dir, kept)
    else:
        record.unlink(missing_ok=True)
    return retained


def _ancestors_are_real(run_dir: Path, relative: Path) -> bool:
    """Whether run_dir and every directory down to the entry's parent is real."""
    current = run_dir
    for part in (None, *relative.parts[:-1]):
        current = current if part is None else current / part
        if not stat.S_ISDIR(os.lstat(current).st_mode):
            return False
    return True


def _remove_entry(run_dir: Path, entry: _Entry, destination: Path, retained: set[Path]) -> bool:
    """Remove the object *entry* created; return whether the entry must stay recorded."""
    try:
        return _remove_created(run_dir, entry, destination, retained)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not remove staged runtime input %s: %s", destination, exc)
        return True


def _still_created(entry: _Entry, destination: Path, status: os.stat_result) -> bool:
    """Whether *destination* is still the object (and content) *entry* created."""
    if (status.st_dev, status.st_ino) != entry.created:
        return False
    if entry.kind == "file":
        return stat.S_ISREG(status.st_mode) and _sha256(destination) == entry.identity
    if entry.kind == "link":
        return stat.S_ISLNK(status.st_mode) and str(destination.readlink()) == entry.identity
    return stat.S_ISDIR(status.st_mode)


def _remove_created(run_dir: Path, entry: _Entry, destination: Path, retained: set[Path]) -> bool:
    if not _ancestors_are_real(run_dir, Path(entry.path)):
        return True
    if not _still_created(entry, destination, os.lstat(destination)):
        return True
    if entry.kind != "directory":
        destination.unlink()
        return False
    try:
        destination.rmdir()
    except OSError:
        # Content the user added is not ours; a retained leftover below stays recorded.
        return any(path.is_relative_to(destination) for path in retained)
    return False


__all__ = [
    "StagedInputLedger",
    "leftover_message",
    "sweep_cancelled_run_inputs",
]
