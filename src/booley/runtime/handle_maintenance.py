"""Bounded legacy handle allocation owned by MCP maintenance, never Dashboard reads."""

from __future__ import annotations

import logging
from itertools import chain, islice
from pathlib import Path

from booley.core.boundary import require_str_value
from booley.runtime import job_records
from booley.runtime.handles import (
    HandleStore,
    fallback_predecessors,
    job_prefix,
    job_writer,
    session_writer,
)
from booley.runtime.safe_storage import refuse_symlinks

logger = logging.getLogger(__name__)
MAX_LEGACY_JOBS = 16384
MAX_RECORD_BYTES = 2 * 1024 * 1024


def _legacy_records(booley_dir: Path) -> list[job_records.JobRecord]:
    patterns = ("goals/*/.runtime/jobs/*.json", ".interactive_logs/*/.runtime/jobs/*.json")
    paths = chain.from_iterable(booley_dir.glob(pattern) for pattern in patterns)
    records = []
    for index, path in enumerate(islice(paths, MAX_LEGACY_JOBS + 1)):
        if index == MAX_LEGACY_JOBS:
            logger.warning("Legacy handle backfill limited to %s Job files", MAX_LEGACY_JOBS)
            break
        try:
            refuse_symlinks(path)
            if path.stat().st_size > MAX_RECORD_BYTES:
                continue
            rec = job_records.read_record(path.stem, path.parent)
            if rec is None or rec.run_id != path.stem:
                continue
            require_str_value(rec.endpoint, field="Job endpoint")
            if rec.session_key is not None:
                require_str_value(rec.session_key, field="Job owner")
            records.append(rec)
        except (OSError, ValueError, job_records.JobRecordError):
            continue
    return records


def backfill_job_handles(booley_dir: Path) -> None:
    """Reserve handles for retained legacy Jobs and disconnected owners in batches."""
    records = _legacy_records(booley_dir)
    if not records:
        return
    with job_writer(booley_dir):
        store = HandleStore(booley_dir, jobs=True)
        handles = store.load()
        before = handles.next_number
        for rec in records:
            handles.reserve(rec.run_id, job_prefix(rec.endpoint))
        if handles.next_number != before:
            store.save(handles)
    with session_writer(booley_dir):
        store = HandleStore(booley_dir)
        handles = store.load()
        before = handles.labels()
        for rec in records:
            if rec.session_key:
                handles.reserve(
                    rec.session_key,
                    "S",
                    # Historical relative paths have no recorded cwd; do not guess an alias.
                    predecessors=fallback_predecessors(rec.session_key, rec.work_dir)
                    if rec.work_dir and Path(rec.work_dir).is_absolute()
                    else (),
                )
        if handles.labels() != before:
            store.save(handles)
