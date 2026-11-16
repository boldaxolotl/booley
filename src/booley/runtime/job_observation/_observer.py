"""Bounded read-only Job refreshes with private artifact generations."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from itertools import islice
from pathlib import Path
from time import time_ns
from typing import Any

from booley.runtime import job_records, job_slots
from booley.runtime.artifact_paths import available_paths
from booley.runtime.job_observation._artifacts import JobArtifactCache
from booley.runtime.job_observation._model import JobObservation, ObservationSnapshot
from booley.runtime.job_observation._unfinished import UnfinishedIndex
from booley.runtime.pid import ProcessIdentity, ProcessState, observe_process
from booley.runtime.timefmt import parse_timestamp

MAX_JOBS = 4096
# Examine at most four times the projected record capacity before selecting newest records.
MAX_SCANNED_JOBS = 4 * MAX_JOBS
RecordValidator = Callable[[job_records.JobRecord], None]


class JobObserver:
    """Own one Project's cache across read-only refreshes of explicitly selected roots.

    The optional validator interprets caller-owned record metadata before artifact
    or process observation. Raise ValueError to isolate an invalid record. Other
    failures abort the refresh without committing its cache generation.
    """

    def __init__(
        self,
        booley_dir: Path,
        *,
        slots_root: Path | None = None,
        proc_root: Path = Path("/proc"),
        validate_record: RecordValidator | None = None,
    ) -> None:
        self._slots_root = slots_root
        self._proc_root = proc_root
        self._validate_record = validate_record
        self._artifacts = JobArtifactCache(booley_dir)
        self._unfinished = UnfinishedIndex()

    def read(
        self,
        roots: Sequence[Path],
        *,
        unfinished_roots: Sequence[Path] = (),
        artifact_roots: Sequence[Path] = (),
    ) -> ObservationSnapshot:
        """Never poll, reconcile status, adopt, repair or invoke a reaper.

        Records of *roots* are history: bounded to the newest ``MAX_JOBS``. From
        *unfinished_roots* only unfinished Jobs are listed, and an unfinished Job of *roots*
        that the bound cuts is listed too. Reports are read under the Booley Directory and
        under *artifact_roots*.
        """
        cache = self._artifacts
        cache.begin(artifact_roots)
        self._unfinished.begin(time_ns())
        tokens, memory = self._observe_slots()
        records, diagnostics = _snapshot_records(
            roots, unfinished_roots, self._unfinished, self._validate_record
        )
        rows = []
        for root, rec, exempt in records:
            row = _project_job(
                root, rec, tokens, memory, self._proc_root, cache, self._validate_record
            )
            # Only the newest MAX_JOBS records may yield a finished row.
            if not exempt or (row.state != "orphaned" and row.record.status == "running"):
                rows.append(row)
        diagnostics.extend(cache.diagnostics)
        diagnostics.extend(_ambiguous_ids(rows))
        snapshot = ObservationSnapshot(tuple(rows), tuple(diagnostics))
        cache.complete()
        self._unfinished.complete()
        return snapshot

    def _observe_slots(
        self,
    ) -> tuple[
        list[job_slots.SlotToken], tuple[list[job_slots.SlotToken], list[job_slots.SlotToken]]
    ]:
        """Every observed token, plus the memory class's own (holders, waiters)."""
        tokens: list[job_slots.SlotToken] = []
        memory: tuple[list[job_slots.SlotToken], list[job_slots.SlotToken]] = ([], [])
        if self._slots_root is not None:
            store = job_slots.SlotStore(self._slots_root)
            holders, waiters = store.observe(job_slots.CLASS_MEMORY)
            tokens.extend((*holders, *waiters))
            memory = (holders, waiters)
        return tokens, memory


def _state(rec: job_records.JobRecord, slots: list[job_slots.SlotToken], proc_root: Path) -> str:
    if rec.status != job_records.STATUS_RUNNING:
        return "completed" if rec.status == job_records.STATUS_DONE else rec.status
    if rec.pid is None:
        return "spawning"
    identity = ProcessIdentity.from_payload(rec.process_identity)
    observed = (
        ProcessState.UNKNOWN
        if identity is None
        else observe_process(identity, proc_root=proc_root).state
    )
    if observed in {ProcessState.DEAD, ProcessState.REUSED, ProcessState.ZOMBIE}:
        return "orphaned"
    if observed == ProcessState.UNKNOWN:
        return "unavailable"
    # Child lanes (one execution each) run under the Job's PID but are not the Job's own entry.
    matched = [
        token
        for token in slots
        if token.owner_identity == identity and token.owner_kind != "execution"
    ]
    if len(matched) > 1:
        matched = _with_item_claims(matched)
    if matched:
        return (
            "unavailable"
            if len(matched) > 1
            else ("running" if matched[0].is_holder else "queued")
        )
    return "running" if rec.run_started_at else "spawning"


def _with_item_claims(matched: list[job_slots.SlotToken]) -> list[job_slots.SlotToken]:
    """The Job's own entry, standing for a waiting work-item claim when one waits.

    A Job holding a claim may also hold Memory Reservations for its work items, in
    its own PID. One non-memory entry or companion memory entry (a mutation
    coordinator) with item claims beside it is the Job: it is ``queued`` while an
    item claim waits.
    """

    def is_own(token: job_slots.SlotToken) -> bool:
        return token.job_class != job_slots.CLASS_MEMORY or (
            token.memory is not None and token.memory.companion
        )

    own = [token for token in matched if is_own(token)]
    items = [token for token in matched if not is_own(token)]
    if len(own) != 1 or not items:
        return matched
    waiting = [token for token in items if not token.is_holder]
    return [waiting[0]] if waiting and own[0].is_holder else own


def _snapshot_records(
    roots: Sequence[Path],
    unfinished_roots: Sequence[Path],
    index: UnfinishedIndex,
    validate_record: RecordValidator | None,
) -> tuple[list[tuple[Path, job_records.JobRecord, bool]], list[str]]:
    """Parse the newest bounded set of records plus every unfinished one, saying what is cut.

    The third item is true for a record outside the bound (to be dropped once finished).
    """
    records, diagnostics = [], []
    candidates = _record_candidates(roots, diagnostics)
    if len(candidates) > MAX_JOBS:
        diagnostics.append(_truncation_notice(len(candidates)))
        # Newest by modification time; a record is rewritten whenever its Job changes state.
        newest = sorted(candidates, key=lambda item: item[0], reverse=True)[:MAX_JOBS]
        kept = {(root, path) for _, root, path in newest}
        candidates = [item for item in candidates if (item[1], item[2]) in kept]
    chosen = [(root, path, False) for _, root, path in candidates]
    taken = {(root, path) for root, path, _ in chosen}
    unfinished = _unfinished_paths(roots, unfinished_roots, index, validate_record, diagnostics)
    chosen.extend((root, path, True) for root, path in unfinished if (root, path) not in taken)
    for root, path, exempt in chosen:
        rec = _read_job(path, validate_record)
        if rec is None:
            diagnostics.append(f"Job record unavailable: {path}")
            continue
        records.append((root, rec, exempt))
    return records, diagnostics


def _unfinished_paths(
    roots: Sequence[Path],
    unfinished_roots: Sequence[Path],
    index: UnfinishedIndex,
    validate_record: RecordValidator | None,
    diagnostics: list[str],
) -> list[tuple[Path, Path]]:
    """(root, path) of every record whose on-disk status is running, in every root."""

    def is_unfinished(path: Path) -> bool:
        rec = _validated(_parse_job(path), validate_record)
        return rec is not None and rec.status == job_records.STATUS_RUNNING

    found: list[tuple[Path, Path]] = []
    for root in dict.fromkeys((*roots, *unfinished_roots)):
        if root.is_symlink():
            if root not in roots:
                diagnostics.append(f"Job root unavailable: {root}")
            continue
        found.extend((root, path) for path in index.paths(root, is_unfinished))
    return found


def _truncation_notice(examined: int) -> str:
    """Say what was cut; past the scan bound finished Jobs of older sessions go unlisted."""
    notice = f"Job history truncated: showing the newest {MAX_JOBS} of {examined} records"
    if examined < MAX_SCANNED_JOBS:
        return notice
    return (
        f"Job history truncated: showing the newest {MAX_JOBS} of the first "
        f"{examined} records examined; finished Jobs of older interactive sessions are not shown"
    )


def _record_candidates(
    roots: Sequence[Path], diagnostics: list[str]
) -> list[tuple[int, Path, Path]]:
    """List (mtime, root, record path) in root order without reading any record."""
    candidates: list[tuple[int, Path, Path]] = []
    for root in dict.fromkeys(roots):
        if root.is_symlink():
            diagnostics.append(f"Job root unavailable: {root}")
            continue
        for path in islice(root.glob("*.json"), max(0, MAX_SCANNED_JOBS - len(candidates))):
            try:
                candidates.append((path.lstat().st_mtime_ns, root, path))
            except OSError:
                diagnostics.append(f"Job record unavailable: {path}")
    return candidates


def _read_job(path: Path, validate_record: RecordValidator | None) -> job_records.JobRecord | None:
    try:
        rec = _parse_job(path)
    except OSError:
        return None
    return _validated(rec, validate_record)


def _validated(
    rec: job_records.JobRecord | None, validate_record: RecordValidator | None
) -> job_records.JobRecord | None:
    """*rec* unless the caller's validator rejects it; other validator errors propagate."""
    if rec is not None and validate_record is not None:
        try:
            validate_record(rec)
        except ValueError:
            return None
    return rec


def _parse_job(path: Path) -> job_records.JobRecord | None:
    """The well-formed record at *path*, or ``None``; ``OSError`` propagates."""
    try:
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        rec = job_records.read_record(path.stem, path.parent)
        if (
            rec is None
            or rec.run_id != path.stem
            or not isinstance(rec.endpoint, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", rec.endpoint)
            or not isinstance(rec.argv, list)
            or not all(isinstance(arg, str) for arg in rec.argv)
            or rec.status not in {"running", "done", "failed", "cancelled"}
        ):
            return None
        if not all(
            isinstance(stamp, str)
            for stamp in (rec.started_at, rec.ended_at or "", rec.run_started_at or "")
        ):
            return None
        parse_timestamp(rec.started_at)
        if rec.ended_at is not None:
            parse_timestamp(rec.ended_at)
        if rec.run_started_at is not None:
            parse_timestamp(rec.run_started_at)
        if rec.pid is not None and (type(rec.pid) is not int or rec.pid <= 0):
            return None
    except (ValueError, job_records.JobRecordError):
        return None
    return rec


def _project_job(
    root: Path,
    rec: job_records.JobRecord,
    tokens: list[job_slots.SlotToken],
    memory: tuple[list[job_slots.SlotToken], list[job_slots.SlotToken]],
    proc_root: Path,
    cache: JobArtifactCache,
    validate_record: RecordValidator | None,
) -> JobObservation:
    state = _state(rec, tokens, proc_root)
    if state == "orphaned":
        # The record was read before its owner finished: it may have written its
        # terminal record and exited since, so show that outcome, not a false orphan.
        current = _read_job(root / f"{rec.run_id}.json", validate_record)
        if current is not None and current.status != job_records.STATUS_RUNNING:
            rec, state = current, _state(current, tokens, proc_root)
    reports_parent = None if rec.report_root is None else Path(rec.report_root)
    artifact = cache.find(root, rec.endpoint, rec.run_id, "report", reports_parent=reports_parent)
    report, report_path = (None, None) if artifact is None else artifact
    progress = cache.find(
        root, rec.endpoint, rec.run_id, "progress", reports_parent=reports_parent
    )
    stage = _reported_stage(None if progress is None else progress[0])
    artifacts = available_paths(
        report or {},
        (reports_parent or root.parent, Path(rec.work_dir) if rec.work_dir else root),
    )
    wait = _queue_wait(rec, state, memory)
    return JobObservation(
        root,
        rec,
        state,
        report,
        report_path,
        stage,
        artifacts=artifacts,
        queue="" if wait is None else f"position {wait.position}: {wait.describe()}",
        queue_position=None if wait is None else wait.position,
    )


def _queue_wait(
    rec: job_records.JobRecord,
    state: str,
    memory: tuple[list[job_slots.SlotToken], list[job_slots.SlotToken]],
) -> job_slots.MemoryWait | None:
    """What the Job's queued memory claim waits for; ``None`` unless it is such a waiter."""
    if state != "queued":
        return None
    identity = ProcessIdentity.from_payload(rec.process_identity)
    holders, waiters = memory
    for waiter in waiters:
        if waiter.owner_identity == identity:
            return job_slots.memory_wait(waiter, holders, waiters, time.time())
    return None


def _reported_stage(progress: dict[str, Any] | None) -> str:
    if progress is None:
        return ""
    stage = progress.get("stage")
    if isinstance(stage, str) and stage:
        return stage
    phase = progress.get("phase")
    # Starting is an initial checkpoint, not proof of the current EDA stage.
    return phase if isinstance(phase, str) and phase != "starting" else ""


def _ambiguous_ids(rows: list[JobObservation]) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.record.run_id] = counts.get(row.record.run_id, 0) + 1
    return [f"ambiguous legacy run ID: {run}" for run, count in counts.items() if count > 1]
