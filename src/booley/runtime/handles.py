"""Persistent Project display handles; never routing or execution authority."""

import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from booley.core.boundary import require_dict, require_int, require_str_value
from booley.core.file_lock import acquire_file_lock, release_file_lock, wait_for_file_lock
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.filesystem_utils import open_with_sharing_retry
from booley.runtime.job_records import STATUS_RUNNING, JobRecord, parse_stamp
from booley.runtime.safe_storage import refuse_symlinks

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


def job_prefix(endpoint: str) -> str:
    return "J-" + JOB_TYPES.get(endpoint, "JOB")


@dataclass
class HandleMap:
    """Display allocations and direct aliases, loaded under the owner's writer lock."""

    entries: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    next_number: int = 1

    def labels(self) -> dict[str, str]:
        entries = self.entries
        return {k: entries[v] for k, v in self.aliases.items() if v in entries} | entries

    def reserve(self, key: str, prefix: str, *, predecessors: tuple[str, ...] = ()) -> str:
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
                self.entries[key] = f"{prefix}{self.next_number}"
                self.next_number += 1
        for old in predecessors:
            if old not in self.entries:
                self.aliases.setdefault(old, target)
        return self.entries[target]

    def drop(self, keys: set[str]) -> None:
        """Remove *keys* and every alias pointing at them; an empty map restarts numbering."""
        for key in keys:
            self.entries.pop(key, None)
        self.aliases = {
            name: target for name, target in self.aliases.items() if target not in keys
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
        if not self.path.exists():
            return HandleMap()
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
        return HandleMap(entries, aliases, counter)

    def save(self, handles: HandleMap) -> None:
        refuse_symlinks(self.path, booley_dir=self.booley_dir)
        payload = (json.dumps({"schema": 1, **asdict(handles)}, sort_keys=True) + "\n").encode()
        if len(payload) > MAX_MAP_BYTES:
            raise ValueError("handle map exceeds 16 MiB")
        atomic_replace_bytes(self.path, payload)

    def reserve(self, key: str, prefix: str, *, predecessors: tuple[str, ...] = ()) -> str:
        root = self.root if self.jobs else self.root.parent
        with _writer(root, wait_s=1 if self.jobs else None, booley_dir=self.booley_dir):
            handles = self.load()
            before = asdict(handles)
            label = handles.reserve(key, prefix, predecessors=predecessors)
            if asdict(handles) != before:
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
        try:
            return self.load().labels(), ()
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            return {}, (f"{'Job' if self.jobs else 'Session'} handles unavailable: {exc}",)


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


def session_writer(booley_dir: Path) -> AbstractContextManager[None]:
    return _writer(booley_dir / "runtime/sessions", wait_s=None, booley_dir=booley_dir)


def reserve_session_handle(booley_dir: Path, key: str) -> str:
    return HandleStore(booley_dir).reserve(key, "S")


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
