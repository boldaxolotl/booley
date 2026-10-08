"""Advisory session presence. No reader here selects or changes Goal work."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from booley.core.boundary import require_dict, require_str, require_str_value, require_uuid4
from booley.core.file_lock import nonblocking_file_lock
from booley.goals.model import WorktreeIdentity
from booley.goals.store import REPOSITORY_ID_FILE, resolve_worktree_identity
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.pid import ProcessIdentity, ProcessState, observe_process
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_epoch

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
    process_state: ProcessState | None = None

    def payload(self) -> bytes:
        """Serialize complete presence atomically."""
        data = asdict(self)
        data.pop(
            "process_state"
        )  # A live observation is detached presentation, not durable presence.
        return (json.dumps(data, sort_keys=True) + "\n").encode()


@dataclass(frozen=True)
class RegistrySnapshot:
    """Readable rows and isolated storage diagnostics."""

    rows: tuple[SessionRow, ...] = ()
    diagnostics: tuple[str, ...] = ()
    unavailable: bool = False


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
        require_str_value(facts.get("work_dir"), field="work_dir", allow_empty=True),
        require_str_value(facts.get("worktree_key"), field="worktree_key", allow_empty=True),
        require_str_value(facts.get("branch", ""), field="branch", allow_empty=True),
        process,
        require_str_value(facts.get("namespace", ""), field="namespace", allow_empty=True),
    )
    started, latest = require_str(data, "started_at"), require_str(data, "last_call_at")
    if (
        rfc3339_from_epoch(parse_timestamp(started).timestamp()) != started
        or rfc3339_from_epoch(parse_timestamp(latest).timestamp()) != latest
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
    if rfc3339_from_epoch(parse_timestamp(at).timestamp()) != at:
        raise ValueError("call timestamps must be canonical UTC seconds")
    return RecentCall(at, require_str(data, "tool"), require_str(data, "outcome"))


def _updated_row(
    facts: Attribution, prior: list[SessionRow], tool: str, outcome: str, now: float
) -> SessionRow:
    stamp = max([rfc3339_from_epoch(now), *(row.last_call_at for row in prior)])
    started = min([stamp, *(row.started_at for row in prior)])
    calls = sorted((call for row in prior for call in row.calls), key=lambda call: call.at)
    calls.append(RecentCall(stamp, tool[:128], outcome[:64]))
    return SessionRow(facts, started, stamp, tuple(calls[-CALL_HISTORY:]))


DEAD_PROCESS_STATES = frozenset({ProcessState.DEAD, ProcessState.REUSED, ProcessState.ZOMBIE})


@dataclass(frozen=True)
class SessionPresence:
    """One quiet/liveness policy for presentation, pruning and Doctor."""

    kind: str
    quiet: bool
    process_state: ProcessState | None

    @property
    def retained(self) -> bool:
        return (
            self.process_state not in DEAD_PROCESS_STATES
            if self.kind == "process"
            else not self.quiet
        )

    @property
    def recent(self) -> bool:
        return not self.quiet and self.retained


def session_presence(
    row: SessionRow, *, now: float, quiet_after: float, proc_root: Path = Path("/proc")
) -> SessionPresence:
    """Unknown process observations never prove expiry or a connection."""
    state = (
        observe_process(row.attribution.process, proc_root=proc_root).state
        if row.attribution.process is not None
        else None
    )
    return SessionPresence(
        row.attribution.kind,
        now - parse_timestamp(row.last_call_at).timestamp() >= quiet_after,
        state,
    )


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
        with (
            lock.open("a+", encoding="utf-8", newline="") as handle,
            nonblocking_file_lock(handle),
        ):
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
                return RegistrySnapshot(
                    diagnostics=("session registry unavailable: absent",), unavailable=True
                )
            paths = list(islice(self.root.glob("*.json"), MAX_ROWS + 1))
            if len(paths) > MAX_ROWS:
                diagnostics.append("session registry exceeds scan bound")
            for path in paths[:MAX_ROWS]:
                try:
                    row = self._read(path)
                    rows.append(replace(row, attribution=canonical_attribution(row.attribution)))
                except (OSError, ValueError, UnicodeError) as exc:
                    diagnostics.append(f"{path.name}: {exc}")
        except OSError as exc:
            diagnostics.append(f"session registry unavailable: {exc}")
        unique: dict[str, SessionRow] = {}
        for row in sorted(rows, key=lambda row: row.last_call_at):
            unique[row.attribution.key] = row
        return RegistrySnapshot(
            tuple(unique.values()), tuple(diagnostics), bool(diagnostics) and not rows
        )

    def visible_snapshot(self, *, now: float, scope: str) -> RegistrySnapshot:
        """Observational quiet filtering; maintenance owns every durable deletion."""
        snapshot = self.snapshot()
        if not scope:
            return RegistrySnapshot(
                diagnostics=(*snapshot.diagnostics, "session namespace unavailable"),
                unavailable=True,
            )
        rows = []
        for row in snapshot.rows:
            if row.attribution.namespace != scope:
                continue
            presence = session_presence(
                row, now=now, quiet_after=self.quiet_after, proc_root=self.proc_root
            )
            if presence.retained:
                rows.append(replace(row, process_state=presence.process_state))
        return RegistrySnapshot(tuple(rows), snapshot.diagnostics, snapshot.unavailable)

    def preserve_worktree(self, attribution: Attribution) -> Attribution:
        """Calls without a work directory refresh the caller's previous association."""
        if attribution.worktree_key:
            return attribution
        path = self.path(attribution.key)
        old = self._read(path) if path.exists() else None
        prior = canonical_attribution(old.attribution) if old else None
        return (
            replace(
                attribution,
                work_dir=prior.work_dir,
                worktree_key=prior.worktree_key,
                branch=prior.branch,
            )
            if prior
            else attribution
        )

    def _predecessors(self, facts: Attribution) -> list[tuple[Path, SessionRow]]:
        candidates = [self.path(facts.key)]
        if (
            facts.kind == "worktree"
            and facts.work_dir
            and not facts.worktree_key.startswith("path:")
        ):
            candidates.append(self.path("worktree:path:" + facts.work_dir))
        return [(path, self._read(path)) for path in candidates if path.exists()]

    def upsert(self, attribution: Attribution, tool: str, outcome: str, *, now: float) -> None:
        """Preserve first presence and migrate path fallbacks under the same writer lock."""
        with self._writer():
            path = self.path(attribution.key)
            refuse_symlinks(path)
            predecessors = self._predecessors(attribution)
            attribution = self.preserve_worktree(attribution)
            row = _updated_row(attribution, [row for _, row in predecessors], tool, outcome, now)
            if (
                not predecessors
                and len(list(islice(self.root.glob("*.json"), MAX_ROWS))) >= MAX_ROWS
            ):
                raise OSError("session registry is full")
            payload = row.payload()
            if len(payload) > MAX_ROW_BYTES:
                raise ValueError("oversized session row")
            atomic_replace_bytes(path, payload)
            for prior_path, _ in predecessors:
                if prior_path != path:
                    prior_path.unlink(missing_ok=True)

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
            for path in islice(self.root.glob("*.json"), MAX_ROWS):
                try:
                    row = self._read(path)
                except (OSError, ValueError, UnicodeError):
                    continue
                facts = canonical_attribution(row.attribution)
                if not namespace or facts.namespace != namespace:
                    continue
                missing = bool(
                    facts.worktree_key and worktree_exists and worktree_exists(facts) is False
                )
                presence = session_presence(
                    row, now=now, quiet_after=self.quiet_after, proc_root=self.proc_root
                )
                if missing or not presence.retained:
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed


def shares_worktree(facts: Attribution, other: Attribution) -> bool:
    """Only distinct attributed callers count; a fallback cannot prove another session."""
    return bool(
        facts.worktree_key
        and facts.worktree_key == other.worktree_key
        and facts.key != other.key
        and other.kind != "worktree"
    )


def namespace(proc_root: Path = Path("/proc")) -> str:
    """Current PID namespace; unavailable cannot authorize process observation."""
    try:
        return str((proc_root / "self/ns/pid").readlink())
    except OSError:
        return ""


def _git_fact(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        value = handle.read(16385)
    if len(value) > 16384:
        raise ValueError("Git metadata exceeds observation bound")
    return value.strip()


def _git_metadata(root: Path) -> tuple[Path, Path, Path] | None:
    # Observational filesystem facts avoid invoking Git on the MCP response path.
    for candidate in islice((root, *root.parents), 128):
        marker = candidate / ".git"
        if marker.is_dir():
            return candidate, marker.resolve(), marker.resolve()
        if marker.is_file():
            raw = _git_fact(marker)
            if not raw.startswith("gitdir: "):
                raise ValueError("invalid Git worktree marker")
            git = (candidate / raw.removeprefix("gitdir: ")).resolve()
            common_file = git / "commondir"
            common = (git / _git_fact(common_file)).resolve() if common_file.exists() else git
            return candidate, git, common
    return None


def _worktree_facts(work_dir: Path) -> tuple[Path, str, str]:
    """Canonical observational key/branch from bounded Git metadata reads, without creation."""
    root = work_dir.resolve()
    key, branch = "path:" + str(root), ""
    try:
        metadata = _git_metadata(root)
        if metadata is None:
            return root, key, branch
        root, git, common = metadata
        key = "path:" + str(root)
        identity_file = common / REPOSITORY_ID_FILE
        if identity_file.exists():
            repository = require_uuid4(_git_fact(identity_file), field="repository id")
            checkout = "main" if git == common else git.relative_to(common).as_posix()
            key = WorktreeIdentity.from_json(
                {"repository": repository, "checkout": checkout}, where="observed worktree"
            ).key
        head = _git_fact(git / "HEAD")
        if head.startswith("ref: refs/heads/"):
            branch = head.removeprefix("ref: refs/heads/")
    except (OSError, ValueError, UnicodeError):
        pass  # Missing/unreadable metadata cannot confer a repository identity.
    return root, key, branch


def canonical_attribution(facts: Attribution) -> Attribution:
    """Match pre-Goal path rows to the same repository/admin identity used by Goal Records."""
    if not facts.work_dir:
        return facts
    root, key, branch = _worktree_facts(Path(facts.work_dir))
    if facts.worktree_key.startswith("path:"):
        return replace(
            facts,
            work_dir=str(root),
            worktree_key=key,
            branch=branch,
            key="worktree:" + key if facts.kind == "worktree" else facts.key,
        )
    return replace(facts, branch=branch or facts.branch)


def valid_thread_id(value: object) -> bool:
    """Bound client metadata and reject controls or unencodable Unicode."""
    return isinstance(value, str) and 0 < len(value) <= 1024 and value.isprintable()


def resolve_attribution(
    work_dir: Path | None,
    *,
    metadata: Mapping[str, Any],
    client_name: str,
    peer: ProcessIdentity | None = None,
    proc_root: Path = Path("/proc"),
) -> Attribution:
    """Resolve once: Codex thread, distinct peer, then observational worktree identity."""
    root, worktree_key, branch = (
        _worktree_facts(work_dir) if work_dir is not None else (None, "", "")
    )
    thread = metadata.get("threadId")
    codex = "codex" in client_name.lower() or thread is not None
    scope = namespace(proc_root)
    if codex and valid_thread_id(thread):
        return Attribution(
            "codex:" + str(thread),
            "thread",
            str(root) if root is not None else "",
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
            str(root) if root is not None else "",
            worktree_key,
            branch=branch,
            process=peer,
            namespace=scope,
        )
    return Attribution(
        "worktree:" + worktree_key,
        "worktree",
        str(root) if root is not None else "",
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
        if not facts.worktree_key:
            return None
        if facts.worktree_key.startswith("path:"):
            return str(Path(facts.work_dir).resolve()) in paths
        return facts.worktree_key in identities

    return present
