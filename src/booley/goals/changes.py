"""The Change Log: ``changes.jsonl``, append-only, two lines per Goal change (D15).

A Goal change is written as an ``intent`` line before the record and state
are rewritten, and an ``applied`` line after::

    {"id": "<uuid>", "phase": "intent", "entry": {...ChangeEntry...}}
    {"id": "<uuid>", "phase": "applied", "at": "2026-10-06T10:15:00Z"}

An ``intent`` without its ``applied`` line is an interrupted apply; the
reader reports it so a mutating call can complete it. Only the last line may
be torn by a crash (no trailing newline, or not JSON): readers ignore it and
the next writer truncates it. Damage anywhere else, a line of the wrong
shape, or an out-of-order ``applied`` raises :class:`ChangeLogCorruptError`.

Writers take a :class:`~booley.goals.store.RecordLock`, the proof that the
caller holds the record lock that guards every Goal Record file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_uuid4
from booley.goals.model import GoalRecordFormatError, GoalSpec, record_timestamp
from booley.goals.paths import CHANGES_FILE
from booley.goals.store import GoalStoreError, RecordLock
from booley.runtime.atomic_files import fsync_directory
from booley.runtime.timefmt import utc_now_rfc3339


class ChangeKind(StrEnum):
    """What a Goal change does to one Goal."""

    ADD = "add"
    RELAX = "relax"
    RETARGET = "retarget"
    WAIVER = "waiver"


class Approval(StrEnum):
    """How the human's decision reached Booley (ADR 0067 Amendments)."""

    ELICITED = "elicited"
    AGENT_RECORDED = "agent-recorded"


class ChangeLogCorruptError(GoalStoreError):
    """``changes.jsonl`` is damaged somewhere other than a torn last line."""

    def __init__(self, path: Path, line: int, reason: str) -> None:
        super().__init__(f"corrupt Change Log {path} line {line}: {reason}")
        self.path = path
        self.line = line


class ChangeLogOrderError(GoalStoreError):
    """An append would break the intent-then-applied protocol."""


@dataclass(frozen=True)
class ChangeEntry:
    """One approved Goal change.

    ``before`` is absent for ``add``; every other kind has both sides.
    ``goal_key`` names the Goal before the change (after it, for ``add``).
    An agent-recorded approval carries the human's quoted words in
    ``quote``. ``session_key`` (the acting session) and ``peer_process`` (the
    process that sent an elicited answer) are audit fields only.
    """

    id: str
    at: str
    kind: ChangeKind
    goal_key: str
    before: GoalSpec | None
    after: GoalSpec
    reason: str
    approval: Approval
    quote: str | None = None
    session_key: str | None = None
    peer_process: str | None = None
    transaction_ref: str | None = None

    def __post_init__(self) -> None:
        require_uuid4(self.id, field="change id")
        record_timestamp(self.at, "change at")
        if (self.kind is ChangeKind.ADD) != (self.before is None):
            needed = "absent" if self.kind is ChangeKind.ADD else "present"
            raise ValueError(f"a {self.kind.value!r} change needs 'before' {needed}")
        if not self.reason.strip():
            raise ValueError("a Goal change needs the human's reason")
        if self.approval is Approval.AGENT_RECORDED and not (self.quote or "").strip():
            raise ValueError("an agent-recorded approval needs the human's quoted words")

    def to_json(self) -> dict[str, Any]:
        """The JSON object stored in an ``intent`` line."""
        result: dict[str, Any] = {
            "id": self.id,
            "at": self.at,
            "kind": self.kind.value,
            "goal_key": self.goal_key,
            "before": None if self.before is None else self.before.to_json(),
            "after": self.after.to_json(),
            "reason": self.reason,
            "approval": self.approval.value,
            "quote": self.quote,
            "session_key": self.session_key,
            "peer_process": self.peer_process,
        }
        if self.transaction_ref is not None:
            result["transaction_ref"] = self.transaction_ref
        return result

    @classmethod
    def from_json(cls, raw: Any) -> ChangeEntry:
        """Parse an ``intent`` line's entry; raises ``ValueError`` on any defect."""
        raw = require_dict(raw, field="entry")
        if set(raw) - {"transaction_ref"} != set(_ENTRY_KEYS):
            raise ValueError(f"entry must have exactly the fields {list(_ENTRY_KEYS)}")
        before = raw["before"]
        return cls(
            id=_text(raw["id"], "id"),
            at=_text(raw["at"], "at"),
            kind=ChangeKind(raw["kind"]),
            goal_key=_text(raw["goal_key"], "goal_key"),
            before=None if before is None else GoalSpec.from_json(before, where="before"),
            after=GoalSpec.from_json(raw["after"], where="after"),
            reason=_text(raw["reason"], "reason"),
            approval=Approval(raw["approval"]),
            quote=_optional_text(raw["quote"], "quote"),
            session_key=_optional_text(raw["session_key"], "session_key"),
            peer_process=_optional_text(raw["peer_process"], "peer_process"),
            transaction_ref=None
            if raw.get("transaction_ref") is None
            else require_uuid4(raw["transaction_ref"], field="transaction_ref"),
        )


_ENTRY_KEYS = (
    "id",
    "at",
    "kind",
    "goal_key",
    "before",
    "after",
    "reason",
    "approval",
    "quote",
    "session_key",
    "peer_process",
)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


@dataclass(frozen=True)
class ChangeLog:
    """What ``changes.jsonl`` holds: applied changes, interrupted applies, a torn tail."""

    applied: tuple[ChangeEntry, ...]
    interrupted: tuple[ChangeEntry, ...]
    torn_tail: bool


@dataclass(frozen=True)
class _Scan:
    log: ChangeLog
    good_length: int  # bytes up to the end of the last intact line
    file_length: int


def read_change_log(path: Path) -> ChangeLog:
    """Read a Change Log; a missing file is an empty log."""
    return _scan(path).log


def _scan(path: Path) -> _Scan:
    data = path.read_bytes() if path.exists() else b""
    lines = data.split(b"\n")
    tail = lines.pop()  # bytes after the last newline: b"" unless the last line is torn
    intents: dict[str, ChangeEntry] = {}
    applied: list[str] = []
    good_length = 0
    torn = bool(tail)
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except ValueError as exc:
            if number == len(lines) and not tail:
                torn = True  # the last line is complete but not JSON
                break
            raise ChangeLogCorruptError(path, number, f"not JSON: {exc}") from None
        _apply_line(path, number, record, intents, applied)
        good_length += len(line) + 1
    done = set(applied)
    return _Scan(
        ChangeLog(
            applied=tuple(intents[change_id] for change_id in applied),
            interrupted=tuple(entry for cid, entry in intents.items() if cid not in done),
            torn_tail=torn,
        ),
        good_length,
        len(data),
    )


def _apply_line(
    path: Path, number: int, record: Any, intents: dict[str, ChangeEntry], applied: list[str]
) -> None:
    """Validate one intact line against the log so far and record it."""
    try:
        record = require_dict(record, field="line")
        change_id = require_uuid4(record.get("id"), field="id")
        phase = record.get("phase")
        if phase == "intent" and set(record) == {"id", "phase", "entry"}:
            entry = ChangeEntry.from_json(record["entry"])
            if entry.id != change_id or change_id in intents:
                raise ValueError(f"intent {change_id} is duplicated or mislabelled")
            intents[change_id] = entry
        elif phase == "applied" and set(record) == {"id", "phase", "at"}:
            record_timestamp(record["at"], "applied at")
            if change_id not in intents or change_id in applied:
                raise ValueError(f"applied {change_id} has no pending intent")
            applied.append(change_id)
        else:
            raise ValueError("a line must be an 'intent' or 'applied' record")
    except (ValueError, BoundaryError, GoalRecordFormatError) as exc:
        raise ChangeLogCorruptError(path, number, str(exc)) from None


def append_intent(lock: RecordLock, entry: ChangeEntry) -> None:
    """Append the ``intent`` line of *entry* before the change is applied."""
    path = _locked_path(lock)
    log = _prepare_append(path)
    if any(e.id == entry.id for e in (*log.applied, *log.interrupted)):
        raise ChangeLogOrderError(f"Goal change {entry.id} is already in {path}")
    _append_line(path, {"id": entry.id, "phase": "intent", "entry": entry.to_json()})


def append_applied(lock: RecordLock, change_id: str) -> None:
    """Append the ``applied`` line that completes the pending change *change_id*."""
    path = _locked_path(lock)
    log = _prepare_append(path)
    if not any(entry.id == change_id for entry in log.interrupted):
        raise ChangeLogOrderError(f"Goal change {change_id} has no pending intent in {path}")
    _append_line(path, {"id": change_id, "phase": "applied", "at": utc_now_rfc3339()})


def _locked_path(lock: RecordLock) -> Path:
    lock.require_owned()
    return lock.record_dir / CHANGES_FILE


def _prepare_append(path: Path) -> ChangeLog:
    """Validate the log and truncate a torn last line before appending."""
    scan = _scan(path)
    if scan.good_length < scan.file_length:
        with path.open("r+b") as stream:
            stream.truncate(scan.good_length)
            stream.flush()
            os.fsync(stream.fileno())
    return scan.log


def _append_line(path: Path, record: dict[str, Any]) -> None:
    line = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    created = not path.exists()
    with path.open("ab") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    if created:
        fsync_directory(path.parent)
