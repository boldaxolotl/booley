"""Prune expired Job handles when a top-level MCP server starts (ADR 0093).

A Job handle lives while its Job runs and for 24 h after it ends. Everything else
goes; a map left empty restarts numbering at 1.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from booley.core.booley_dir import resolve_common_booley_dir
from booley.runtime import job_records
from booley.runtime.bootstrap_mode import should_run_outer_bookkeeping
from booley.runtime.handles import (
    JOB_HANDLE_GRACE_SECONDS,
    HandleStore,
    job_handle_expired,
    run_id_submitted_at,
)
from booley.runtime.session_paths import project_job_roots, session_jobs_dir

logger = logging.getLogger(__name__)

# The map is written by short-lived reservations; a prune that cannot get the
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
    run_id: str, found: dict[str, list[job_records.JobRecord]], unreadable: set[str], now: float
) -> bool:
    if run_id in unreadable:
        return False
    if run_id in found:
        return all(job_handle_expired(record, now) for record in found[run_id])
    # No record: a synchronous run deleted it, or it is not written yet.
    submitted = run_id_submitted_at(run_id)
    return submitted is None or now - submitted > JOB_HANDLE_GRACE_SECONDS


def prune_project_handles(booley_dir: Path, *, now: float) -> None:
    """Drop one Project's expired Job entries; errors propagate and leave the map untouched."""
    store = HandleStore(booley_dir)
    if not store.path.exists():
        return
    found, unreadable = _read_job_records(booley_dir, store.load().entries)
    store.prune(
        lambda handles: {
            run_id for run_id in handles.entries if _job_expired(run_id, found, unreadable, now)
        },
        wait_s=LOCK_WAIT_SECONDS,
    )


def _prune_quietly() -> None:
    try:
        booley_dir = resolve_common_booley_dir()
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
    The map is replaced atomically, so a daemon thread cut off at exit leaves it
    whole.
    """
    if not should_run_outer_bookkeeping():
        return None
    thread = threading.Thread(target=_prune_quietly, name="booley-handle-prune", daemon=True)
    thread.start()
    return thread
