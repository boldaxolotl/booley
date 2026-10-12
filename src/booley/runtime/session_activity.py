"""Session activity reported by agent-client lifecycle hooks (ADR 0068).

A hook writes the last lifecycle event of one session; the Dashboard reads it.
Activity is presentational only: no lifecycle rule reads it, and a missing or
stale signal is "unknown", never a guess.

The writer is the standalone hook script the Sandbox Image installs
(``booley/data/docker/session_activity_hook.py``); it cannot import this
package, so it repeats the event table, key formats and file layout below.
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
from pathlib import Path

from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.pid import ProcessIdentity
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_epoch

SCHEMA = 1
WORKING = "working"
WAITING_TOOL = "waiting for tool call"
WAITING_USER = "waiting for user input"
UNKNOWN = "unknown"

# Lifecycle events the hook script translates; the image must install a hook for each.
EVENT_STATES = {
    "UserPromptSubmit": WORKING,
    "PreToolUse": WAITING_TOOL,
    "PostToolUse": WORKING,
    "PostToolUseFailure": WORKING,
    "PermissionRequest": WAITING_USER,
    "Stop": WAITING_USER,
}
END_EVENT = "SessionEnd"

# A client that died mid-turn sends no closing event. A busy state older than
# the longest Booley tool call is therefore no longer evidence of activity.
BUSY_STALE_AFTER = 7200.0
# Tolerated clock difference between the hook and the reader.
FUTURE_TOLERANCE = 5.0
MAX_SIGNALS = 4096
MAX_SIGNAL_BYTES = 4096
MAX_TOOL_NAME = 128


def process_session_key(identity: ProcessIdentity) -> str:
    """Registry key of a session identified by its agent-client process."""
    return f"pid:{identity.identity_scope}:{identity.pid}:{identity.start_token}"


def thread_session_key(thread: str) -> str:
    """Registry key of a session identified by its Codex thread."""
    return "codex:" + thread


def valid_thread_id(value: object) -> bool:
    """Bound client metadata and reject controls or unencodable Unicode."""
    return isinstance(value, str) and 0 < len(value) <= 1024 and value.isprintable()


@dataclass(frozen=True)
class ActivitySignal:
    """The last lifecycle event one session reported."""

    key: str
    state: str
    at: str
    tool: str = ""

    def payload(self) -> bytes:
        """Canonical persisted form."""
        return json.dumps(
            {"schema": SCHEMA, "key": self.key, "state": self.state, "at": self.at}
            | ({"tool": self.tool} if self.tool else {}),
            sort_keys=True,
        ).encode()


@dataclass(frozen=True)
class Activity:
    """What the Dashboard shows: a reported state and when it began, or unknown."""

    state: str = UNKNOWN
    since: str = ""
    tool: str = ""


def _parse(raw: bytes, key: str) -> ActivitySignal:
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("key") != key:
        raise ValueError("not this session's activity signal")
    state, at, tool = data.get("state"), data.get("at"), data.get("tool", "")
    if state not in EVENT_STATES.values() or not isinstance(at, str) or not isinstance(tool, str):
        raise ValueError("malformed activity signal")
    if rfc3339_from_epoch(parse_timestamp(at).timestamp()) != at:
        raise ValueError("activity timestamps must be canonical UTC seconds")
    return ActivitySignal(key, state, at, tool[:MAX_TOOL_NAME])


class ActivityStore:
    """One small file per session key; writes are atomic, reads never write."""

    def __init__(self, project_dir: Path) -> None:
        self.root = project_dir / "runtime" / "session_activity"

    def path(self, key: str) -> Path:
        """Opaque filename; logical keys never become filesystem components."""
        return self.root / (sha256(key.encode()).hexdigest() + ".json")

    def _entries(self) -> list[os.DirEntry[str]]:
        """A bounded listing; the directory handle is closed before returning."""
        with os.scandir(self.root) as scan:
            return list(islice(scan, MAX_SIGNALS))

    def write(self, signal: ActivitySignal) -> None:
        """Replace the session's signal. Raises ``OSError`` when the store is full or unsafe."""
        refuse_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(signal.key)
        refuse_symlinks(path)
        if not path.exists() and len(self._entries()) >= MAX_SIGNALS:
            raise OSError("session activity store is full")
        atomic_replace_bytes(path, signal.payload())

    def clear(self, key: str) -> None:
        """Forget a session that reported its own end."""
        refuse_symlinks(self.root)
        self.path(key).unlink(missing_ok=True)

    def read(self, key: str) -> ActivitySignal | None:
        """The session's signal, or ``None`` when absent, unsafe or malformed."""
        path = self.path(key)
        try:
            refuse_symlinks(path)
            if path.stat().st_size > MAX_SIGNAL_BYTES:
                return None
            return _parse(path.read_bytes(), key)
        except (OSError, ValueError, UnicodeError):
            return None

    def prune(self, retained: Collection[str], *, now: float, quiet_after: float) -> int:
        """Drop signals of sessions the registry no longer holds, once they are quiet.

        A signal may precede its registry row (a prompt before the first Booley
        call), so an unmatched signal is kept until it is older than *quiet_after*.
        """
        keep = {self.path(key).name for key in retained}
        removed = 0
        try:
            refuse_symlinks(self.root)
            entries = self._entries()
        except OSError:
            return 0
        for entry in entries:
            try:
                expired = now - entry.stat(follow_symlinks=False).st_mtime >= quiet_after
            except OSError:
                continue
            if entry.name not in keep and expired:
                Path(entry.path).unlink(missing_ok=True)
                removed += 1
        return removed


def observed_activity(signal: ActivitySignal | None, *, now: float) -> Activity:
    """Report a fresh signal; a missing, future or stale busy signal is unknown."""
    if signal is None:
        return Activity()
    age = now - parse_timestamp(signal.at).timestamp()
    if age < -FUTURE_TOLERANCE:
        return Activity()
    if signal.state != WAITING_USER and age >= BUSY_STALE_AFTER:
        return Activity()
    return Activity(signal.state, signal.at, signal.tool)
