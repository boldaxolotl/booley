"""Prune expired Job and Session display handles when a top-level MCP server starts (ADR 0068).

A Job handle lives while its Job runs and for 24 h after it ends. A Session handle
lives while a registry row names it (or one of its aliases), while a live Job
names it as owner, or, for a ``booley chat`` tab, while its process is not
proven dead. Everything else goes; a map left empty restarts numbering at 1.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from hashlib import sha256
from pathlib import Path

from booley.core.booley_dir import resolve_booley_dir
from booley.mcp.session_registry import DEAD_PROCESS_STATES
from booley.runtime import job_records
from booley.runtime.bootstrap_mode import should_run_outer_bookkeeping
from booley.runtime.handles import (
    JOB_HANDLE_GRACE_SECONDS,
    HandleMap,
    HandleStore,
    job_handle_expired,
    run_id_submitted_at,
)
from booley.runtime.pid import observe_process
from booley.runtime.session_activity import process_identity_from_session_key
from booley.runtime.session_paths import project_job_roots, session_jobs_dir

logger = logging.getLogger(__name__)

# Both maps are written by short-lived reservations; a prune that cannot get the
# lock this fast is skipped and retried at the next server start.
LOCK_WAIT_SECONDS = 1.0


def _read_job_records(
    booley_dir: Path, run_ids: Iterable[str]
) -> tuple[dict[str, list[job_records.JobRecord]], set[str]]:
    """Records of *run_ids* in every Job root, plus the IDs whose record cannot be read."""
    wanted = set(run_ids)
    roots = list(project_job_roots(booley_dir))
    if (own := session_jobs_dir()) is not None:
        roots.append(own)
    found: dict[str, list[job_records.JobRecord]] = {}
    unreadable: set[str] = set()
    for root in roots:
        for path in root.glob("*.json"):
            if path.stem not in wanted:
                continue
            try:
                record = job_records.read_record(path.stem, root)
            except (OSError, job_records.JobRecordError):
                record = None
            if record is None:
                unreadable.add(path.stem)
            else:
                found.setdefault(path.stem, []).append(record)
    return found, unreadable


def _job_expired(
    run_id: str,
    found: dict[str, list[job_records.JobRecord]],
    unreadable: set[str],
    now: float,
) -> bool:
    if run_id in unreadable:
        return False
    if run_id in found:
        return all(job_handle_expired(record, now) for record in found[run_id])
    # No record: a synchronous run deleted it, or it is not written yet.
    submitted = run_id_submitted_at(run_id)
    return submitted is None or now - submitted > JOB_HANDLE_GRACE_SECONDS


def _prune_jobs(booley_dir: Path, now: float) -> dict[str, list[job_records.JobRecord]]:
    """Drop expired Job entries; return the records of the entries that remain."""
    store = HandleStore(booley_dir, jobs=True)
    if not store.path.exists():
        return {}
    found, unreadable = _read_job_records(booley_dir, store.load().entries)
    kept = store.prune(
        lambda handles: {
            run_id for run_id in handles.entries if _job_expired(run_id, found, unreadable, now)
        },
        wait_s=LOCK_WAIT_SECONDS,
    )
    return {run_id: records for run_id, records in found.items() if run_id in kept.entries}


def _registry_row_names(root: Path) -> set[str]:
    """File names of the Session registry rows, from one directory scan."""
    try:
        with os.scandir(root) as scan:
            return {entry.name for entry in scan if entry.name.endswith(".json")}
    except FileNotFoundError:
        return set()


def _session_select(
    booley_dir: Path, owners: set[str], process_retained: Callable[[str], bool]
) -> Callable[[HandleMap], set[str]]:
    """Choose the Session entries nothing keeps alive; runs under the Session writer lock."""

    def select(handles: HandleMap) -> set[str]:
        rows = _registry_row_names(booley_dir / "runtime" / "sessions")
        names: dict[str, set[str]] = {entry: {entry} for entry in handles.entries}
        for alias, target in handles.aliases.items():
            names.get(target, set()).add(alias)
        return {
            entry
            for entry, entry_names in names.items()
            if not any(
                sha256(name.encode()).hexdigest() + ".json" in rows
                or name in owners
                or process_retained(name)
                for name in entry_names
            )
        }

    return select


def _prune_sessions(booley_dir: Path, owners: set[str]) -> None:
    store = HandleStore(booley_dir)
    if not store.path.exists():
        return
    observed: dict[str, bool] = {}

    def process_retained(name: str) -> bool:
        if name not in observed:
            identity = process_identity_from_session_key(name)
            observed[name] = (
                identity is not None and observe_process(identity).state not in DEAD_PROCESS_STATES
            )
        return observed[name]

    # Observe processes before taking the lock; only keys reserved meanwhile are observed under it.
    initial = store.load()
    for name in (*initial.entries, *initial.aliases):
        process_retained(name)
    store.prune(_session_select(booley_dir, owners, process_retained), wait_s=LOCK_WAIT_SECONDS)


def prune_project_handles(booley_dir: Path, *, now: float) -> None:
    """Prune both handle maps of one Project; errors propagate and leave a map untouched."""
    remaining = _prune_jobs(booley_dir, now)
    owners = {
        record.session_key
        for records in remaining.values()
        for record in records
        if record.session_key
    }
    _prune_sessions(booley_dir, owners)


def _prune_quietly() -> None:
    try:
        booley_dir = resolve_booley_dir()
    except FileNotFoundError:
        return
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Handle pruning skipped: %s", exc)
        return
    try:
        prune_project_handles(booley_dir, now=time.time())
    except (OSError, ValueError, RuntimeError) as exc:  # includes UnicodeError, RecursionError
        logger.warning("Handle pruning skipped: %s", exc)


def prune_at_start() -> threading.Thread | None:
    """Prune in the background of a top-level MCP server; nested servers never prune.

    A first prune over a large map must not delay the client's startup handshake.
    Both maps are replaced atomically, so a daemon thread cut off at exit leaves
    each of them whole.
    """
    if not should_run_outer_bookkeeping():
        return None
    thread = threading.Thread(target=_prune_quietly, name="booley-handle-prune", daemon=True)
    thread.start()
    return thread
