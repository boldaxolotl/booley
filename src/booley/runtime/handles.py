"""Persistent Project display handles; never routing or execution authority."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

from booley.core.boundary import require_dict, require_int, require_str_value
from booley.core.file_lock import nonblocking_file_lock, release_file_lock, wait_for_file_lock
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.safe_storage import refuse_symlinks

logger = logging.getLogger(__name__)
MAX_MAP_BYTES = 16 * 1024 * 1024
JOB_TYPES = {
    "sim": "SIM",
    "simulate": "SIM",
    "sim_baseline": "SIM",
    "synth": "SYN",
    "asic_synthesize": "SYN",
    "lint": "LINT",
    "lint_rtl": "LINT",
    "fpga": "FPGA",
    "fpga_impl": "FPGA",
}


def job_prefix(endpoint: str) -> str:
    """Use a fixed vocabulary, including a safe fallback for custom endpoints."""
    return "J-" + JOB_TYPES.get(endpoint, "JOB")


@dataclass
class HandleMap:
    """Unique allocations and direct aliases, loaded under the owner's writer lock."""

    entries: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    next_number: int = 1

    def labels(self) -> dict[str, str]:
        """Detached lookup including old keys; suitable for observational readers."""
        return {
            **self.entries,
            **{key: self.entries[target] for key, target in self.aliases.items()},
        }

    def reserve(self, key: str, prefix: str, *, predecessors: tuple[str, ...] = ()) -> str:
        """Allocate once, carrying a fallback identity forward without renumbering."""
        require_str_value(key, field="handle key")
        if key not in self.entries and key not in self.aliases:
            prior = next(
                (
                    self.aliases.get(old, old)
                    for old in predecessors
                    if old in self.entries or old in self.aliases
                ),
                None,
            )
            if prior is not None:
                self.entries[key] = self.entries.pop(prior)
                self.aliases = {
                    alias: key if target == prior else target
                    for alias, target in self.aliases.items()
                }
                self.aliases[prior] = key
            else:
                self.entries[key] = f"{prefix}{self.next_number}"
                self.next_number += 1
        target = self.aliases.get(key, key)
        for old in predecessors:
            if old != target and old not in self.entries and old not in self.aliases:
                self.aliases[old] = target
        return self.entries[target]


def fallback_predecessors(key: str, work_dir: str | None) -> tuple[str, ...]:
    """A canonical worktree owner may have first registered by its physical path."""
    if not key.startswith("worktree:") or not work_dir:
        return ()
    root = Path(work_dir).resolve()
    candidates = [root]
    # Registry worktree facts use the checkout root, even when a call names a subdirectory.
    for parent in islice((root, *root.parents), 128):
        marker = parent / ".git"
        if marker.is_dir() or marker.is_file():
            candidates.append(parent)
            break
    return tuple(dict.fromkeys("worktree:path:" + str(path) for path in candidates))


class HandleStore:
    """Atomic map I/O. Writers supply serialization; readers never create files."""

    def __init__(self, booley_dir: Path, *, jobs: bool = False) -> None:
        self.jobs = jobs
        self.root = booley_dir / "runtime" / ("job-handles" if jobs else "sessions/handles")
        self.path = self.root / "map.json"

    def load(self) -> HandleMap:
        """Validate persistent state without resetting corrupt or unsupported maps."""
        refuse_symlinks(self.path)
        if not self.path.exists():
            return HandleMap()
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_MAP_BYTES + 1)
        if len(raw) > MAX_MAP_BYTES:
            raise ValueError("handle map exceeds 16 MiB")
        data = require_dict(json.loads(raw), field="handle map")
        if type(data.get("schema")) is not int or data["schema"] != 1:
            raise ValueError("unsupported handle map schema")
        entries = _entries(data.get("entries"), jobs=self.jobs)
        aliases = _aliases(data.get("aliases"), entries, jobs=self.jobs)
        next_number = require_int(data.get("next_number"), field="handle counter")
        maximum = max(
            (int(re.search(r"[0-9]+$", value)[0]) for value in entries.values()), default=0
        )
        if next_number <= maximum or next_number <= 0:
            raise ValueError("handle counter would reuse an allocated number")
        return HandleMap(entries, aliases, next_number)

    def save(self, handles: HandleMap) -> None:
        """Reserve durable bytes before the owner publishes a row or Job record."""
        refuse_symlinks(self.path)
        payload = (
            json.dumps(
                {
                    "schema": 1,
                    "entries": handles.entries,
                    "aliases": handles.aliases,
                    "next_number": handles.next_number,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if len(payload) > MAX_MAP_BYTES:
            raise ValueError("handle map exceeds 16 MiB")
        atomic_replace_bytes(self.path, payload)

    def snapshot(self) -> tuple[dict[str, str], tuple[str, ...]]:
        """Isolate presentation storage failures without hiding their diagnostics."""
        try:
            return self.load().labels(), ()
        except (OSError, ValueError, UnicodeError) as exc:
            return {}, (f"{'Job' if self.jobs else 'Session'} handles unavailable: {exc}",)


def _entries(value: object, *, jobs: bool) -> dict[str, str]:
    entries = require_dict(value, field="handle entries")
    pattern = r"J-(?:SIM|SYN|LINT|FPGA|JOB)([1-9][0-9]*)" if jobs else r"S([1-9][0-9]*)"
    numbers: set[int] = set()
    result = {}
    for key, label in entries.items():
        require_str_value(key, field="handle key")
        text = require_str_value(label, field="handle label")
        match = re.fullmatch(pattern, text)
        if match is None or int(match[1]) in numbers:
            raise ValueError("invalid or duplicate handle allocation")
        numbers.add(int(match[1]))
        result[key] = text
    return result


def _aliases(value: object, entries: Mapping[str, str], *, jobs: bool) -> dict[str, str]:
    aliases = require_dict(value, field="handle aliases")
    if jobs and aliases:
        raise ValueError("Job handles cannot have aliases")
    result = {}
    for key, raw_target in aliases.items():
        require_str_value(key, field="handle alias")
        target = require_str_value(raw_target, field="handle alias target")
        if key in entries or target not in entries:
            raise ValueError("handle alias must point directly to an allocation")
        result[key] = target
    return result


@contextmanager
def session_writer(booley_dir: Path) -> Iterator[None]:
    """Share exactly the registry's nonblocking writer lock with handle allocation."""
    root = booley_dir / "runtime" / "sessions"
    refuse_symlinks(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".lock"
    refuse_symlinks(lock)
    with lock.open("a+", encoding="utf-8", newline="") as handle, nonblocking_file_lock(handle):
        yield


@contextmanager
def job_writer(booley_dir: Path) -> Iterator[None]:
    """Serialize cross-process Job allocation with a bounded wait."""
    root = HandleStore(booley_dir, jobs=True).root
    refuse_symlinks(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".lock"
    refuse_symlinks(lock)
    with lock.open("a+", encoding="utf-8", newline="") as handle:
        wait_for_file_lock(handle, timeout_s=1)
        try:
            yield
        finally:
            release_file_lock(handle)


def register_job(
    booley_dir: Path,
    run_id: str,
    endpoint: str,
    session_key: str | None,
    *,
    work_dir: str | None = None,
) -> None:
    """Best-effort presentation allocation; broken maps never gate Job execution."""
    try:
        with job_writer(booley_dir):
            store = HandleStore(booley_dir, jobs=True)
            handles = store.load()
            if run_id not in handles.entries:
                handles.reserve(run_id, job_prefix(endpoint))
                store.save(handles)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Job handle unavailable for %s: %s", run_id, exc)
    if session_key:
        try:
            with session_writer(booley_dir):
                store = HandleStore(booley_dir)
                handles = store.load()
                before = handles.labels()
                handles.reserve(
                    session_key, "S", predecessors=fallback_predecessors(session_key, work_dir)
                )
                if handles.labels() != before:
                    store.save(handles)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Session handle unavailable for Job %s: %s", run_id, exc)
