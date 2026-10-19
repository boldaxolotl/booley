"""Bounded read-only Job refreshes with private artifact generations."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from itertools import islice
from pathlib import Path
from typing import Any

from booley.runtime import job_records, job_slots
from booley.runtime.artifact_paths import available_paths
from booley.runtime.job_observation._artifacts import JobArtifactCache
from booley.runtime.job_observation._model import JobObservation, ObservationSnapshot
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

    def read(self, roots: Sequence[Path]) -> ObservationSnapshot:
        """Never poll, reconcile status, adopt, repair or invoke a reaper."""
        cache = self._artifacts
        cache.begin()
        tokens = self._observe_slots()
        records, diagnostics = _snapshot_records(roots, self._validate_record)
        rows = [_project_job(root, rec, tokens, self._proc_root, cache) for root, rec in records]
        diagnostics.extend(cache.diagnostics)
        diagnostics.extend(_ambiguous_ids(rows))
        snapshot = ObservationSnapshot(tuple(rows), tuple(diagnostics))
        cache.complete()
        return snapshot

    def _observe_slots(self) -> list[job_slots.SlotToken]:
        tokens = []
        if self._slots_root is not None:
            store = job_slots.SlotStore(self._slots_root)
            for kind in ("heavy", "light"):
                holders, waiters = store.observe(kind)
                tokens.extend((*holders, *waiters))
        return tokens


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
    matched = [token for token in slots if token.owner_identity == identity]
    if matched:
        return (
            "unavailable"
            if len(matched) > 1
            else ("running" if matched[0].is_holder else "queued")
        )
    return "running" if rec.run_started_at else "spawning"


def _snapshot_records(
    roots: Sequence[Path], validate_record: RecordValidator | None
) -> tuple[list[tuple[Path, job_records.JobRecord]], list[str]]:
    """Parse only the newest bounded set of records, and say so when history is cut."""
    records, diagnostics = [], []
    candidates = _record_candidates(roots, diagnostics)
    if len(candidates) > MAX_JOBS:
        diagnostics.append(_truncation_notice(len(candidates)))
        # Newest by modification time; a record is rewritten whenever its Job changes state.
        newest = sorted(candidates, key=lambda item: item[0], reverse=True)[:MAX_JOBS]
        kept = {(root, path) for _, root, path in newest}
        candidates = [item for item in candidates if (item[1], item[2]) in kept]
    for _, root, path in candidates:
        rec = _read_job(path, validate_record)
        if rec is None:
            diagnostics.append(f"Job record unavailable: {path}")
            continue
        records.append((root, rec))
    return records, diagnostics


def _truncation_notice(examined: int) -> str:
    """Say what was cut; past the scan bound the oldest interactive sessions go unexamined."""
    notice = f"Job history truncated: showing the newest {MAX_JOBS} of {examined} records"
    if examined < MAX_SCANNED_JOBS:
        return notice
    return (
        f"Job history truncated: showing the newest {MAX_JOBS} of the first "
        f"{examined} records examined; older interactive sessions were not examined"
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
    except (OSError, ValueError, job_records.JobRecordError):
        return None
    if validate_record is not None:
        try:
            validate_record(rec)
        except ValueError:
            rec = None
    return rec


def _project_job(
    root: Path,
    rec: job_records.JobRecord,
    tokens: list[job_slots.SlotToken],
    proc_root: Path,
    cache: JobArtifactCache,
) -> JobObservation:
    artifact = cache.find(root, rec.endpoint, rec.run_id, "report")
    report, report_path = (None, None) if artifact is None else artifact
    progress = cache.find(root, rec.endpoint, rec.run_id, "progress")
    stage = _reported_stage(None if progress is None else progress[0])
    artifacts = available_paths(
        report or {}, (root.parent, Path(rec.work_dir) if rec.work_dir else root)
    )
    return JobObservation(
        root,
        rec,
        _state(rec, tokens, proc_root),
        report,
        report_path,
        stage,
        artifacts=artifacts,
    )


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
