"""Neutral, strictly observational multi-root Job projection for the Dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from booley.goals.binding import GoalBindingError, GoalRunBinding
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from booley.runtime import job_records, job_slots
from booley.runtime.artifact_paths import available_paths
from booley.runtime.job_artifacts import JobArtifactCache
from booley.runtime.pid import ProcessIdentity, ProcessState, observe_process
from booley.runtime.timefmt import parse_timestamp
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

MAX_JOBS = 4096
MAX_INTERACTIVE_ROOTS = 64
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class JobView:
    """A run identity includes its root; duplicate legacy run IDs remain separate."""

    root: Path
    record: job_records.JobRecord
    state: str
    report: dict[str, Any] | None = None
    report_path: Path | None = None
    stage: str = ""
    diagnostic: str = ""
    cpu_percent: float | None = None
    memory: int | None = None
    peak_memory: int | None = None
    artifacts: tuple[str, ...] = ()
    eda_tool: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Stable selection identity through completion and session expiry."""
        return str(self.root), self.record.run_id


@dataclass(frozen=True)
class JobSnapshot:
    """Bounded rows and isolated diagnostics; history is retained by its producer."""

    jobs: tuple[JobView, ...]
    diagnostics: tuple[str, ...] = ()


def target_arg(argv: list[str]) -> str | None:
    """Read the producer's Target once, without inventing one for legacy records."""
    try:
        index = argv.index("--target")
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def retained_job_roots(
    project_dir: Path, *, interactive_root: Path | None = None
) -> tuple[Path, ...]:
    """Interactive roots and every retained Goal, including finished/abandoned records."""
    roots = [] if interactive_root is None else [interactive_root]
    history = []
    for path in islice(project_dir.glob(".interactive_logs/*/.runtime/jobs"), MAX_JOBS):
        try:
            history.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    roots.extend(
        path
        for _, path in sorted(history, key=lambda item: item[0], reverse=True)[
            :MAX_INTERACTIVE_ROOTS
        ]
    )
    roots.extend(
        record_paths(project_dir, rec.id).jobs_dir
        for rec in GoalStore(project_dir).list_records().records
    )
    return tuple(dict.fromkeys(roots))


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


def snapshot_jobs(
    project_dir: Path,
    *,
    interactive_root: Path | None = None,
    slots_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    artifact_cache: JobArtifactCache | None = None,
) -> JobSnapshot:
    """Read files only. Never poll, derive/reconcile status, adopt or invoke a reaper."""
    cache = artifact_cache if artifact_cache is not None else JobArtifactCache()
    cache.configure_project(project_dir, max_endpoints=MAX_JOBS)
    cache.begin()
    tokens, rows, diagnostics = [], [], []
    catalogs: dict[str, TargetCatalog | None] = {}
    if slots_root is not None:
        store = job_slots.SlotStore(slots_root)
        for kind in ("heavy", "light", "ticket"):
            holders, waiters = store.observe(kind)
            tokens.extend((*holders, *waiters))
    records, record_diagnostics = _snapshot_records(project_dir, interactive_root)
    diagnostics.extend(record_diagnostics)
    cache.prepare_endpoints((root, rec.endpoint) for root, rec in records)
    for root, rec in records:
        rows.append(_project_job(root, rec, tokens, proc_root, catalogs, cache))
    diagnostics.extend(cache.diagnostics)
    diagnostics.extend(_ambiguous_ids(rows))
    cache.complete()
    return JobSnapshot(tuple(rows), tuple(diagnostics))


def _snapshot_records(
    project_dir: Path, interactive_root: Path | None
) -> tuple[list[tuple[Path, job_records.JobRecord]], list[str]]:
    """Collect the bounded projection before selecting eviction victims."""
    records, diagnostics = [], []
    roots = retained_job_roots(project_dir, interactive_root=interactive_root)
    for root in roots:
        if root.is_symlink():
            diagnostics.append(f"Job root unavailable: {root}")
            continue
        for path in islice(root.glob("*.json"), max(0, MAX_JOBS - len(records))):
            rec = _read_job(path)
            if rec is None:
                diagnostics.append(f"Job record unavailable: {path}")
                continue
            records.append((root, rec))
    return records, diagnostics


def _read_job(path: Path) -> job_records.JobRecord | None:
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
        if rec.binding is not None:
            GoalRunBinding.from_json(rec.binding)
        return rec
    except (OSError, ValueError, job_records.JobRecordError, GoalBindingError):
        return None


def _project_job(
    root: Path,
    rec: job_records.JobRecord,
    tokens: list[job_slots.SlotToken],
    proc_root: Path,
    catalogs: dict[str, TargetCatalog | None],
    cache: JobArtifactCache,
) -> JobView:
    artifact = cache.find(root, rec.endpoint, rec.run_id, "report")
    report, report_path = (None, None) if artifact is None else artifact
    progress = cache.find(root, rec.endpoint, rec.run_id, "progress")
    stage = _reported_stage(None if progress is None else progress[0])
    artifacts = available_paths(
        report or {}, (root.parent, Path(rec.work_dir) if rec.work_dir else root)
    )
    return JobView(
        root,
        rec,
        _state(rec, tokens, proc_root),
        report,
        report_path,
        stage,
        artifacts=artifacts,
        eda_tool=_configured_tool(rec, catalogs),
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


def _configured_tool(rec: job_records.JobRecord, catalogs: dict[str, TargetCatalog | None]) -> str:
    target = target_arg(rec.argv)
    if rec.status != job_records.STATUS_RUNNING or not rec.work_dir or target is None:
        return ""
    try:
        if rec.work_dir not in catalogs:
            catalogs[rec.work_dir] = None
            catalogs[rec.work_dir] = TargetCatalog.build(rec.work_dir)
        catalog = catalogs[rec.work_dir]
        return "" if catalog is None else (catalog.select(target).eda_tool or "")
    except (OSError, ValueError, FuseSocError):
        return ""


def _ambiguous_ids(rows: list[JobView]) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.record.run_id] = counts.get(row.record.run_id, 0) + 1
    return [f"ambiguous legacy run ID: {run}" for run, count in counts.items() if count > 1]
