"""Advisory session presence. No reader here selects or changes Goal work."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from booley.core.boundary import require_dict, require_str, require_str_value
from booley.core.file_lock import nonblocking_file_lock
from booley.goals.checkout import CheckoutError, GoalCheckout
from booley.goals.store import resolve_worktree_identity
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.pid import ProcessIdentity, ProcessState, observe_process
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.timefmt import parse_timestamp

MAX_ROWS = 4096
MAX_ROW_BYTES = 65536
CALL_HISTORY = 20


@dataclass(frozen=True)
class Attribution:
    """One request's advisory identity, independent of routing and approval."""

    key: str
    kind: Literal["thread", "process", "worktree"]
    work_dir: str
    worktree_key: str
    branch: str = ""
    process: ProcessIdentity | None = None
    namespace: str = ""


@dataclass(frozen=True)
class RecentCall:
    """Bounded metadata only; arguments and result payloads are never saved."""

    at: str
    tool: str
    outcome: str


@dataclass(frozen=True)
class SessionRow:
    """Versioned durable presence, not a client activity signal."""

    attribution: Attribution
    started_at: str
    last_call_at: str
    calls: tuple[RecentCall, ...] = ()
    schema: int = 1

    def payload(self) -> bytes:
        """Serialize complete presence atomically."""
        return (json.dumps(asdict(self), sort_keys=True) + "\n").encode()


@dataclass(frozen=True)
class RegistrySnapshot:
    """Readable rows and isolated storage diagnostics."""

    rows: tuple[SessionRow, ...] = ()
    diagnostics: tuple[str, ...] = ()


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_row(raw: bytes) -> SessionRow:
    data = require_dict(json.loads(raw), field="session row")
    if type(data.get("schema")) is not int or data["schema"] != 1:
        raise ValueError("unsupported session schema")
    facts = require_dict(data.get("attribution"), field="attribution")
    kind = require_str(facts, "kind")
    if kind not in {"thread", "process", "worktree"}:
        raise ValueError("invalid attribution kind")
    process = ProcessIdentity.from_payload(facts.get("process"))
    if facts.get("process") is not None and process is None:
        raise ValueError("invalid process identity")
    if process is not None and (process.pid <= 0 or process.start_token < 0):
        raise ValueError("invalid process identity values")
    if kind == "process" and process is None:
        raise ValueError("process attribution requires identity")
    attribution = Attribution(
        require_str(facts, "key"),
        kind,
        require_str(facts, "work_dir"),
        require_str(facts, "worktree_key"),
        require_str_value(facts.get("branch", ""), field="branch", allow_empty=True),
        process,
        require_str_value(facts.get("namespace", ""), field="namespace", allow_empty=True),
    )
    started, latest = require_str(data, "started_at"), require_str(data, "last_call_at")
    if (
        _stamp(parse_timestamp(started).timestamp()) != started
        or _stamp(parse_timestamp(latest).timestamp()) != latest
    ):
        raise ValueError("session timestamps must be canonical UTC seconds")
    if parse_timestamp(started) > parse_timestamp(latest):
        raise ValueError("session timestamps regress")
    calls = data.get("calls", [])
    if not isinstance(calls, list) or len(calls) > CALL_HISTORY:
        raise ValueError("invalid call history")
    history = tuple(_parse_call(call) for call in calls)
    stamps = [call.at for call in history]
    if stamps != sorted(stamps) or any(at < started or at > latest for at in stamps):
        raise ValueError("call history timestamps regress")
    return SessionRow(attribution, started, latest, history)


def _parse_call(value: object) -> RecentCall:
    data = require_dict(value, field="recent call")
    at = require_str(data, "at")
    if _stamp(parse_timestamp(at).timestamp()) != at:
        raise ValueError("call timestamps must be canonical UTC seconds")
    return RecentCall(at, require_str(data, "tool"), require_str(data, "outcome"))


class SessionRegistry:
    """Nonblocking serialized upsert/prune; observational snapshots never lock or write.

    Thread and fallback presence expires after quiet_after. Unknown process
    liveness is retained. Each scan is bounded; excess/corrupt rows are diagnosed,
    never deleted as if dead. The maintenance owner alone prunes presence.
    """

    def __init__(
        self, project_dir: Path, *, quiet_after: float = 7200, proc_root: Path = Path("/proc")
    ) -> None:
        self.root = project_dir / "runtime" / "sessions"
        self.quiet_after = quiet_after
        self.proc_root = proc_root

    def path(self, key: str) -> Path:
        """Opaque filename; logical keys never become filesystem components."""
        return self.root / (sha256(key.encode()).hexdigest() + ".json")

    @contextmanager
    def _writer(self) -> Iterator[None]:
        refuse_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".lock"
        refuse_symlinks(lock)
        with lock.open("a+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
            yield

    def _read(self, path: Path) -> SessionRow:
        refuse_symlinks(path)
        if path.stat().st_size > MAX_ROW_BYTES:
            raise ValueError("oversized session row")
        row = _parse_row(path.read_bytes())
        if path != self.path(row.attribution.key):
            raise ValueError("session filename does not match its key")
        return row

    def snapshot(self) -> RegistrySnapshot:
        """Read retained presence with no pruning or persistent side effects."""
        rows, diagnostics = [], []
        try:
            refuse_symlinks(self.root)
            if not self.root.exists():
                return RegistrySnapshot(diagnostics=("session registry unavailable: absent",))
            paths = list(islice(self.root.glob("*.json"), MAX_ROWS + 1))
            if len(paths) > MAX_ROWS:
                diagnostics.append("session registry exceeds scan bound")
            for path in paths[:MAX_ROWS]:
                try:
                    rows.append(self._read(path))
                except (OSError, ValueError, UnicodeError) as exc:
                    diagnostics.append(f"{path.name}: {exc}")
        except OSError as exc:
            diagnostics.append(f"session registry unavailable: {exc}")
        return RegistrySnapshot(tuple(rows), tuple(diagnostics))

    def visible_snapshot(self, *, now: float, scope: str) -> RegistrySnapshot:
        """Observational quiet filtering; maintenance owns every durable deletion."""
        snapshot = self.snapshot()
        if not scope:
            return RegistrySnapshot(
                diagnostics=(*snapshot.diagnostics, "session namespace unavailable")
            )
        rows = tuple(
            row
            for row in snapshot.rows
            if row.attribution.namespace == scope
            and not (
                row.attribution.kind != "process"
                and now - parse_timestamp(row.last_call_at).timestamp() >= self.quiet_after
            )
        )
        return RegistrySnapshot(rows, snapshot.diagnostics)

    def upsert(self, attribution: Attribution, tool: str, outcome: str, *, now: float) -> None:
        """Preserve first presence and monotonic last-call ordering under one lock."""
        with self._writer():
            path = self.path(attribution.key)
            refuse_symlinks(path)
            old = self._read(path) if path.exists() else None
            stamp = _stamp(now)
            if old is not None and parse_timestamp(old.last_call_at).timestamp() > now:
                stamp = old.last_call_at
            calls = (
                *(() if old is None else old.calls),
                RecentCall(stamp, tool[:128], outcome[:64]),
            )
            row = SessionRow(
                attribution, old.started_at if old else stamp, stamp, tuple(calls[-CALL_HISTORY:])
            )
            if old is None and len(list(islice(self.root.glob("*.json"), MAX_ROWS))) >= MAX_ROWS:
                raise OSError("session registry is full")
            payload = row.payload()
            if len(payload) > MAX_ROW_BYTES:
                raise ValueError("oversized session row")
            atomic_replace_bytes(path, payload)

    def prune(
        self,
        *,
        now: float,
        namespace: str,
        worktree_exists: Callable[[Attribution], bool | None] | None = None,
    ) -> int:
        """Remove only proven expired registry rows; refresh cannot race deletion."""
        removed = 0
        with self._writer():
            for row in self.snapshot().rows:
                facts = row.attribution
                if not namespace or facts.namespace != namespace:
                    continue
                missing = worktree_exists(facts) is False if worktree_exists else False
                quiet = now - parse_timestamp(row.last_call_at).timestamp() >= self.quiet_after
                dead = False
                if facts.kind == "process" and facts.process is not None:
                    dead = observe_process(facts.process, proc_root=self.proc_root).state in {
                        ProcessState.DEAD,
                        ProcessState.REUSED,
                        ProcessState.ZOMBIE,
                    }
                if missing or dead or (facts.kind != "process" and quiet):
                    self.path(facts.key).unlink(missing_ok=True)
                    removed += 1
        return removed

    def other_sessions(self, worktree_key: str) -> tuple[str, ...]:
        """Advisory keys sharing a Worktree Identity."""
        return tuple(
            row.attribution.key
            for row in self.snapshot().rows
            if row.attribution.worktree_key == worktree_key
        )


def namespace(proc_root: Path = Path("/proc")) -> str:
    """Current PID namespace; unavailable cannot authorize process observation."""
    try:
        return str((proc_root / "self/ns/pid").readlink())
    except OSError:
        return ""


def _worktree_facts(work_dir: Path) -> tuple[Path, str, str]:
    root = work_dir.resolve()
    try:
        identity = resolve_worktree_identity(root)
    except (OSError, ValueError, RuntimeError):
        identity = None
    worktree_key = identity.key if identity else "path:" + str(root)
    try:
        branch = (GoalCheckout(root).head_ref() or "").removeprefix("refs/heads/")
    except (CheckoutError, OSError):
        branch = ""
    return root, worktree_key, branch


def valid_thread_id(value: object) -> bool:
    """Bound client metadata and reject controls or unencodable Unicode."""
    return isinstance(value, str) and 0 < len(value) <= 1024 and value.isprintable()


def resolve_attribution(
    work_dir: Path,
    *,
    metadata: Mapping[str, Any],
    client_name: str,
    peer: ProcessIdentity | None = None,
    proc_root: Path = Path("/proc"),
) -> Attribution:
    """Resolve once: Codex thread, distinct peer, then observational worktree identity."""
    root, worktree_key, branch = _worktree_facts(work_dir)
    thread = metadata.get("threadId")
    codex = "codex" in client_name.lower() or thread is not None
    scope = namespace(proc_root)
    if codex and valid_thread_id(thread):
        return Attribution(
            "codex:" + str(thread),
            "thread",
            str(root),
            worktree_key,
            branch=branch,
            process=peer,
            namespace=scope,
        )
    if (
        not codex
        and client_name
        and len(client_name) <= 128
        and client_name.isprintable()
        and peer is not None
        and peer.pid != os.getpid()
    ):
        return Attribution(
            f"pid:{peer.identity_scope}:{peer.pid}:{peer.start_token}",
            "process",
            str(root),
            worktree_key,
            branch=branch,
            process=peer,
            namespace=scope,
        )
    return Attribution(
        "worktree:" + worktree_key,
        "worktree",
        str(root),
        worktree_key,
        branch=branch,
        namespace=scope,
    )


def registered_worktree_presence(root: Path) -> Callable[[Attribution], bool | None]:
    """Resolve observed repository/admin facts once; Git failure is unavailable, never missing."""
    import subprocess

    from booley.runtime.worktrees import list_worktrees

    try:
        entries = list_worktrees(root)
        identities = {
            identity.key
            for entry in entries
            if entry.path.exists()
            and (identity := resolve_worktree_identity(entry.path)) is not None
        }
        paths = {str(entry.path.resolve()) for entry in entries if entry.path.exists()}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return lambda _facts: None

    def present(facts: Attribution) -> bool | None:
        if facts.worktree_key.startswith("path:"):
            return str(Path(facts.work_dir).resolve()) in paths
        return facts.worktree_key in identities

    return present
