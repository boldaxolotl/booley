"""Persistent Project display handles; never routing or execution authority."""

import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from booley.core.boundary import require_dict, require_int, require_str_value
from booley.core.file_lock import acquire_file_lock, release_file_lock, wait_for_file_lock
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.filesystem_utils import open_with_sharing_retry
from booley.runtime.job_records import STATUS_RUNNING, JobRecord, parse_stamp
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.worktree_usage import session_number_taken

logger = logging.getLogger(__name__)
MAX_MAP_BYTES = 16 * 1024 * 1024
# Doctor warns once a map passes this size; MCP server start prunes expired entries.
MAP_WARN_BYTES = MAX_MAP_BYTES * 3 // 4
# A finished Job keeps its handle this long after it ended.
JOB_HANDLE_GRACE_SECONDS = 24 * 3600
_RUN_ID_STAMP = re.compile(r"-(\d{8}T\d{6}Z)-\d+-[0-9a-f]{8}$")

JOB_TYPES = {
    "sim": "SIM",
    "simulate": "SIM",
    "sim_baseline": "SIM",
    "synth": "SYN",
    "synth_baseline": "SYN",
    "asic_synthesize": "SYN",
    "lint": "LINT",
    "lint_rtl": "LINT",
    "fpga": "FPGA",
    "fpga_impl": "FPGA",
}


def session_title(handle: str, name: str | None) -> str:
    """How a session shows on its tab and in the Dashboard: ``S3``, or ``S3 · uart-fix``."""
    return f"{handle} · {name}" if name else handle


def job_prefix(endpoint: str) -> str:
    return "J-" + JOB_TYPES.get(endpoint, "JOB")


@dataclass
class HandleMap:
    """Display allocations and direct aliases, loaded under the owner's writer lock."""

    entries: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    next_number: int = 1
    # Session maps only: the name `booley chat --name` gave an entry, keyed like `entries`.
    names: dict[str, str] = field(default_factory=dict)
    # Session maps only: numbers a worktree or branch occupies are never allocated.
    number_taken: Callable[[int], bool] | None = field(default=None, compare=False, repr=False)

    def stored(self) -> dict[str, object]:
        """The persisted fields; the allocation predicate never reaches storage."""
        return {
            "entries": self.entries,
            "aliases": self.aliases,
            "next_number": self.next_number,
            "names": self.names,
        }

    def labels(self) -> dict[str, str]:
        entries = self.entries
        return {k: entries[v] for k, v in self.aliases.items() if v in entries} | entries

    def titles(self) -> dict[str, str]:
        """Each key's display title: its handle, followed by its name when it has one."""
        return {
            key: session_title(label, self.names.get(self.aliases.get(key, key)))
            for key, label in self.labels().items()
        }

    def reserve(
        self,
        key: str,
        prefix: str,
        *,
        predecessors: tuple[str, ...] = (),
        name: str | None = None,
    ) -> str:
        """Reserve *key*'s handle; a *name* replaces the entry's name, ``None`` keeps it."""
        require_str_value(key, field="handle key")
        target = key if key in self.entries else self.aliases.get(key, key)
        if key in self.aliases and key not in self.entries and target not in self.entries:
            raise ValueError("requested handle alias has no allocation")
        if target not in self.entries:
            for old in predecessors:
                prior = old if old in self.entries else self.aliases.get(old)
                if prior in self.entries:
                    target = self.aliases[key] = prior
                    break
            else:
                while self.number_taken is not None and self.number_taken(self.next_number):
                    self.next_number += 1
                self.entries[key] = f"{prefix}{self.next_number}"
                self.next_number += 1
        for old in predecessors:
            if old not in self.entries:
                self.aliases.setdefault(old, target)
        if name is not None:
            self.names[target] = name
        return self.entries[target]

    def drop(self, keys: set[str]) -> None:
        """Remove *keys* and every alias pointing at them; an empty map restarts numbering.

        An alias of a removed alias goes too: the Codex ownership marker of a resumed tab
        points at the tab's own key, which is itself an alias of the first tab's entry.
        """
        for key in keys:
            self.entries.pop(key, None)
            self.names.pop(key, None)
        removed = {name for name, target in self.aliases.items() if target in keys}
        self.aliases = {
            name: target
            for name, target in self.aliases.items()
            if target not in keys and target not in removed
        }
        if not self.entries:
            self.aliases.clear()
            self.next_number = 1


def job_handle_expired(record: JobRecord, now: float) -> bool:
    """Whether a Job record is terminal for longer than the handle grace period.

    The grace starts at ``ended_at``; startup reconciliation writes terminal
    records without it, so those age from ``started_at``. An unparseable stamp
    never expires the handle.
    """
    if record.status == STATUS_RUNNING:
        return False
    anchor = parse_stamp(record.ended_at)
    if anchor is None:
        anchor = parse_stamp(record.started_at)
    return anchor is not None and now - anchor > JOB_HANDLE_GRACE_SECONDS


def run_id_submitted_at(run_id: str) -> float | None:
    """Epoch seconds from the submit stamp of a ``make_run_id`` ID, else ``None``."""
    match = _RUN_ID_STAMP.search(run_id)
    if match is None:
        return None
    try:
        return datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def fallback_predecessors(key: str, work_dir: str | None) -> tuple[str, ...]:
    if not key.startswith("worktree:") or not work_dir:
        return ()
    root = Path(work_dir).resolve()
    for parent in (root, *root.parents[:127]):
        if (parent / ".git").is_dir() or (parent / ".git").is_file():
            return tuple(dict.fromkeys((f"worktree:path:{root}", f"worktree:path:{parent}")))
    return (f"worktree:path:{root}",)


class HandleStore:
    """Atomic map I/O. Writers supply serialization; readers never create files."""

    def __init__(self, booley_dir: Path, *, jobs: bool = False) -> None:
        self.jobs = jobs
        self.booley_dir = booley_dir
        self.root = booley_dir / "runtime" / ("job-handles" if jobs else "sessions/handles")
        self.path = self.root / "map.json"

    def load(self) -> HandleMap:
        refuse_symlinks(self.path, booley_dir=self.booley_dir)
        taken = None if self.jobs else session_number_taken(self.booley_dir)
        if not self.path.exists():
            return HandleMap(number_taken=taken)
        with open_with_sharing_retry(lambda: self.path.open("rb"), self.path) as stream:
            raw = stream.read(MAX_MAP_BYTES + 1)
        if len(raw) > MAX_MAP_BYTES:
            raise ValueError("handle map exceeds 16 MiB")
        data = require_dict(json.loads(raw), field="handle map")
        if type(data.get("schema")) is not int or data["schema"] != 1:
            raise ValueError("unsupported handle map schema")
        entries, aliases = (require_dict(data.get(n), field=n) for n in ("entries", "aliases"))
        if any(not isinstance(v, str) for m in (entries, aliases) for v in m.values()):
            raise ValueError("handle mapping must contain strings")
        highest = max(
            (int(m[0]) for v in entries.values() if (m := re.search(r"[0-9]+$", v))), default=0
        )
        counter = max(1, require_int(data.get("next_number"), field="handle counter"), highest + 1)
        # Maps written before session names existed have no `names` field.
        names = require_dict(data.get("names", {}), field="names")
        if any(not isinstance(v, str) for v in names.values()):
            raise ValueError("handle names must be strings")
        return HandleMap(entries, aliases, counter, names, taken)

    def save(self, handles: HandleMap) -> None:
        refuse_symlinks(self.path, booley_dir=self.booley_dir)
        payload = (json.dumps({"schema": 1, **handles.stored()}, sort_keys=True) + "\n").encode()
        if len(payload) > MAX_MAP_BYTES:
            raise ValueError("handle map exceeds 16 MiB")
        atomic_replace_bytes(self.path, payload)

    def reserve(
        self,
        key: str,
        prefix: str,
        *,
        predecessors: tuple[str, ...] = (),
        wait_s: float | None = None,
        name: str | None = None,
    ) -> str:
        root = self.root if self.jobs else self.root.parent
        wait = 1 if self.jobs else wait_s
        with _writer(root, wait_s=wait, booley_dir=self.booley_dir):
            handles = self.load()
            before = json.dumps(handles.stored(), sort_keys=True)
            label = handles.reserve(key, prefix, predecessors=predecessors, name=name)
            if json.dumps(handles.stored(), sort_keys=True) != before:
                self.save(handles)
            return label

    def prune(self, select: Callable[[HandleMap], set[str]], *, wait_s: float) -> HandleMap:
        """Drop the entries *select* names from the freshly loaded map, under the writer lock.

        A map that cannot be loaded raises and stays untouched: corrupt maps are
        refused, never reset.
        """
        root = self.root if self.jobs else self.root.parent
        with _writer(root, wait_s=wait_s, booley_dir=self.booley_dir):
            handles = self.load()
            expired = select(handles) & handles.entries.keys()
            if expired:
                handles.drop(expired)
                self.save(handles)
            return handles

    def snapshot(self) -> tuple[dict[str, str], tuple[str, ...]]:
        labels, _, diagnostics = self.titled_snapshot()
        return labels, diagnostics

    def titled_snapshot(self) -> tuple[dict[str, str], dict[str, str], tuple[str, ...]]:
        """Handles and display titles by key, from one read; an unreadable map is diagnosed."""
        try:
            handles = self.load()
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            return {}, {}, (f"{'Job' if self.jobs else 'Session'} handles unavailable: {exc}",)
        return handles.labels(), handles.titles(), ()


@contextmanager
def _writer(root: Path, *, wait_s: float | None, booley_dir: Path) -> Iterator[None]:
    lock = root / ".lock"
    refuse_symlinks(lock, booley_dir=booley_dir)
    root.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8", newline="") as handle:
        if wait_s is None:
            acquire_file_lock(handle)
        else:
            wait_for_file_lock(handle, timeout_s=wait_s)
        try:
            yield
        finally:
            release_file_lock(handle)


def session_writer(
    booley_dir: Path, *, wait_s: float | None = None
) -> AbstractContextManager[None]:
    """The Session writer lock; *wait_s* bounds the wait, ``None`` fails on first contention."""
    return _writer(booley_dir / "runtime/sessions", wait_s=wait_s, booley_dir=booley_dir)


def reserve_session_handle(
    booley_dir: Path,
    key: str,
    *,
    wait_s: float | None = None,
    predecessors: tuple[str, ...] = (),
    name: str | None = None,
) -> str:
    """Reserve the Session handle for *key*, waiting up to *wait_s* for the writer lock.

    *key* keeps the handle of the first of *predecessors* that has one (a resumed tab).
    A *name* (`booley chat --name`) replaces the session's name; ``None`` keeps it.
    """
    return HandleStore(booley_dir).reserve(
        key, "S", wait_s=wait_s, predecessors=predecessors, name=name
    )


def stored_session_title(booley_dir: Path, key: str, handle: str) -> str:
    """The title of the session *key* just reserved as *handle*, with its stored name if any."""
    return HandleStore(booley_dir).titled_snapshot()[1].get(key, handle)


def register_job(
    booley_dir: Path,
    run_id: str,
    endpoint: str,
    session_key: str | None,
    *,
    work_dir: str | None = None,
) -> None:
    if run_id:
        try:
            HandleStore(booley_dir, jobs=True).reserve(run_id, job_prefix(endpoint))
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Job handle unavailable for Job %s: %s", run_id, exc)
    if session_key:
        try:
            HandleStore(booley_dir).reserve(
                session_key, "S", predecessors=fallback_predecessors(session_key, work_dir)
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Session handle unavailable for Job %s: %s", run_id, exc)
