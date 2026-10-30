"""Session activity reported by agent-client lifecycle hooks (ADR 0068).

A hook writes the last lifecycle event of one session; the Dashboard reads it.
Activity is presentational only: no lifecycle rule reads it, and a missing or
stale signal is "unknown", never a guess.

The writer is the standalone hook script the Sandbox Image installs
(``booley/data/docker/session_activity_hook.py``); it cannot import this
package, so it repeats the event table, key formats and file layout below.

``SessionEnd`` replaces the session's signal with an end marker: the same file
and layout with state ``ended``. A marker is not activity (``read`` ignores
it); the Session Registry uses it to hide and prune the session's row.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Collection
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
from pathlib import Path

from booley.runtime.filesystem_utils import read_file_bytes
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
    # A turn the user interrupts ends without Stop. Codex reports the interrupt;
    # Claude Code reports that it is idle or asking through a notification.
    "Interrupt": WAITING_USER,
    "Notification": WAITING_USER,
}
END_EVENT = "SessionEnd"
# SessionEnd turns the session's signal into an end marker with this state.
ENDED = "ended"

# A client that died mid-turn sends no closing event. A busy state older than
# the longest Booley tool call is therefore no longer evidence of activity.
BUSY_STALE_AFTER = 7200.0
# Tolerated clock difference between the hook and the reader.
FUTURE_TOLERANCE = 5.0
MAX_SIGNALS = 4096
MAX_SIGNAL_BYTES = 4096
MAX_TOOL_NAME = 128
MAX_THREAD_ID = 1024


def process_session_key(identity: ProcessIdentity) -> str:
    """Registry key of a session identified by its agent-client process."""
    return f"pid:{identity.identity_scope}:{identity.pid}:{identity.start_token}"


def process_identity_from_session_key(key: str) -> ProcessIdentity | None:
    """Inverse of :func:`process_session_key`; ``None`` when *key* is not a process key."""
    if not key.startswith("pid:"):
        return None
    try:
        scope, pid, start_token = key[len("pid:") :].rsplit(":", 2)
        return ProcessIdentity.from_payload(
            {"pid": int(pid), "identity_scope": scope, "start_token": int(start_token)}
        )
    except ValueError:
        return None


def thread_session_key(thread: str) -> str:
    """Registry key of a session identified by its Codex thread."""
    return "codex:" + thread


def valid_thread_id(value: object) -> bool:
    """Bound client metadata and reject controls or unencodable Unicode."""
    return isinstance(value, str) and 0 < len(value) <= MAX_THREAD_ID and value.isprintable()


@dataclass(frozen=True)
class ActivitySignal:
    """The last lifecycle event one session reported."""

    key: str
    state: str
    at: str
    tool: str = ""


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
    """Reader and pruner of the per-session signal files the hook script writes."""

    def __init__(self, booley_dir: Path) -> None:
        self.booley_dir = booley_dir
        self.root = booley_dir / "runtime" / "session_activity"

    def path(self, key: str) -> Path:
        """Opaque filename; logical keys never become filesystem components."""
        return self.root / (sha256(key.encode()).hexdigest() + ".json")

    def _entries(self) -> list[os.DirEntry[str]]:
        """A bounded listing; the directory handle is closed before returning."""
        with os.scandir(self.root) as scan:
            return list(islice(scan, MAX_SIGNALS))

    def _read_bounded(self, key: str) -> bytes:
        """The session file's bytes; refuses a symlinked or oversized file."""
        path = self.path(key)
        refuse_symlinks(path, booley_dir=self.booley_dir)
        if path.stat().st_size > MAX_SIGNAL_BYTES:
            raise ValueError("session activity signal is oversized")
        return read_file_bytes(path)

    def read(self, key: str) -> ActivitySignal | None:
        """The session's signal, or ``None`` when absent, unsafe or malformed."""
        try:
            return _parse(self._read_bounded(key), key)
        except (OSError, ValueError, UnicodeError):
            return None

    def ended_at(self, key: str, *, now: float) -> str | None:
        """When the session ended (its end marker's ``at``), or ``None`` if unusable.

        A missing, unsafe or malformed marker, another state, and a time more
        than FUTURE_TOLERANCE ahead of *now* all read as "not ended".
        """
        try:
            data = json.loads(self._read_bounded(key))
            at = data.get("at") if isinstance(data, dict) else None
            if (
                not isinstance(at, str)
                or data.get("schema") != SCHEMA
                or data.get("key") != key
                or data.get("state") != ENDED
                or rfc3339_from_epoch(parse_timestamp(at).timestamp()) != at
                or parse_timestamp(at).timestamp() - now > FUTURE_TOLERANCE
            ):
                return None
        except (OSError, ValueError, UnicodeError):
            return None
        return at

    def prune(self, retained: Collection[str], *, now: float, quiet_after: float) -> int:
        """Drop signals of sessions the registry no longer holds, once they are quiet.

        A signal may precede its registry row (a prompt before the first Booley
        call), so an unmatched signal is kept until it is older than *quiet_after*.
        An unmatched end marker can hide nothing, so it is removed at once.
        """
        keep = {self.path(key).name for key in retained}
        removed = 0
        try:
            refuse_symlinks(self.root, booley_dir=self.booley_dir)
            entries = self._entries()
        except OSError:
            return 0
        for entry in entries:
            if entry.name not in keep and self._unlink_if_unretained(entry, now - quiet_after):
                removed += 1
        return removed

    def _is_end_marker(self, path: Path) -> bool:
        """Whether *path* is a session's end marker, whoever the session was."""
        try:
            data = json.loads(read_file_bytes(path))
        except (OSError, ValueError, UnicodeError):
            return False
        return (
            isinstance(data, dict)
            and data.get("state") == ENDED
            and isinstance(data.get("key"), str)
            and self.path(data["key"]).name == path.name
        )

    def _unlink_if_unretained(self, entry: os.DirEntry[str], cutoff: float) -> bool:
        """Remove an end marker, or a signal last written before *cutoff*.

        The bytes are read only for a file that is not yet expired. The hook
        publishes by atomic replacement and shares no lock with this
        pruner, so the file is examined again immediately before removal: a
        replacement is a different inode. The remaining window is the instant
        between that check and the unlink; losing it shows unknown until the
        session's next event.
        """
        try:
            path = Path(entry.path)
            # Windows DirEntry.stat() has no inode; use the same API for both reads.
            listed = path.lstat()
            if listed.st_mtime > cutoff and not (
                stat.S_ISREG(listed.st_mode)
                and listed.st_size <= MAX_SIGNAL_BYTES
                and self._is_end_marker(path)
            ):
                return False
            current = path.lstat()
            if (current.st_ino, current.st_mtime_ns) != (listed.st_ino, listed.st_mtime_ns):
                return False
            path.unlink()
        except OSError:
            return False
        return True


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
