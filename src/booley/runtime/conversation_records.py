"""Reader of the conversation records the lifecycle hook writes for worktree conversations.

The hook (`booley/data/docker/session_activity_hook.py`) records, at `SessionStart` inside an
interactive worktree, which worktree a conversation ran in (ADR 0068 Amendments, 2026-11-10).
`booley chat` reads them to resume a conversation where it ran (ADR 0082, Resume). A record
that is missing, oversized, malformed or unreadable reads as absent: resume then behaves as it
did before records existed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from booley.core.boundary import require_dict, require_int, require_opt_str, require_str
from booley.runtime.pid import DEAD_PROCESS_STATES, observe_process
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.session_activity import process_identity_from_session_key

# The hook's bounds (`MAX_CONVERSATIONS`, `MAX_RECORD_BYTES`): it never writes past them.
MAX_RECORDS = 4096
MAX_RECORD_BYTES = 6 * (4096 + 1024) + 1024
SCHEMA = 1


@dataclass(frozen=True)
class ConversationRecord:
    """Where one conversation started, as the hook recorded it."""

    client: str
    conversation_id: str
    cwd: str
    worktree: str
    handle: str | None
    process_key: str | None
    git_stamp: int


def _store(common: Path) -> Path:
    return common / "worktrees" / ".conversations"


def _parse(raw: bytes) -> ConversationRecord:
    """Validate one record's bytes; raises `ValueError` for anything the hook would not write."""
    data = require_dict(json.loads(raw), field="conversation record")
    if require_int(data.get("schema"), field="schema") != SCHEMA:
        raise ValueError("unsupported conversation record schema")
    return ConversationRecord(
        client=require_str(data, "client"),
        conversation_id=require_str(data, "conversation_id"),
        cwd=require_str(data, "cwd"),
        worktree=require_str(data, "worktree"),
        handle=require_opt_str(data, "handle"),
        process_key=require_opt_str(data, "process_key"),
        git_stamp=require_int(data.get("git_stamp"), field="git_stamp"),
    )


def _read(path: Path) -> ConversationRecord | None:
    """The record in *path*, or ``None`` when it is missing, redirected or invalid."""
    if path.is_symlink():
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            return None
        return _parse(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def find(common: Path, client: str, conversation_id: str) -> ConversationRecord | None:
    """The record of *client*'s conversation *conversation_id* in *common*, else ``None``."""
    digest = hashlib.sha256(f"{client}:{conversation_id}".encode()).hexdigest()
    path = _store(common) / f"{digest}.json"
    try:
        refuse_symlinks(path, booley_dir=common)
    except OSError:
        return None
    record = _read(path)
    mismatch = record is None or (record.client, record.conversation_id) != (
        client,
        conversation_id,
    )
    return None if mismatch else record


def is_live(common: Path, record: ConversationRecord) -> bool:
    """Whether the worktree the record names is still the one the conversation ran in.

    A linked worktree's `.git` is a file; the hook stored its modification time, so a worktree
    removed and made again under the same name does not count.
    """
    marker = common / "worktrees" / record.worktree / ".git"
    try:
        return marker.is_file() and marker.stat().st_mtime_ns == record.git_stamp
    except OSError:
        return False


def live(common: Path) -> list[ConversationRecord]:
    """The records whose worktree still exists, at most `MAX_RECORDS` files scanned."""
    try:
        refuse_symlinks(_store(common), booley_dir=common)
        with os.scandir(_store(common)) as scan:
            paths = [
                Path(entry.path)
                for entry in islice(scan, MAX_RECORDS)
                if entry.name.endswith(".json") and not entry.name.startswith(".")
            ]
    except OSError:
        return []
    records = (_read(path) for path in sorted(paths))
    return [record for record in records if record is not None and is_live(common, record)]


def occupants(records: list[ConversationRecord]) -> dict[str, str]:
    """Map each worktree where a recorded session still runs to that session's handle.

    The handle falls back to the process key when the record names none.
    """
    running: dict[str, bool] = {}
    found: dict[str, str] = {}
    for record in records:
        key = record.process_key
        if key is None or record.worktree in found:
            continue
        if key not in running:
            identity = process_identity_from_session_key(key)
            running[key] = (
                identity is not None and observe_process(identity).state not in DEAD_PROCESS_STATES
            )
        if running[key]:
            found[record.worktree] = record.handle or key
    return found
