"""Dashboard Job history, Goal metadata validation and Target display enrichment."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from booley.goals.binding import GoalBindingError, GoalRunBinding
from booley.goals.model import GoalRecord
from booley.goals.paths import record_paths
from booley.runtime import job_records
from booley.runtime.job_observation import JobObservation, JobObserver
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

MAX_INTERACTIVE_ROOTS = 64
# History selection has its own enumeration bound, independent of record projection.
MAX_SCANNED_INTERACTIVE_ROOTS = 4096
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class JobView(JobObservation):
    """A neutral observation enriched for Dashboard display."""

    cpu_percent: float | None = None
    memory: int | None = None
    peak_memory: int | None = None
    eda_tool: str = ""


@dataclass(frozen=True)
class JobSnapshot:
    """Prepared Dashboard rows with isolated observation diagnostics."""

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
    project_dir: Path, records: Sequence[GoalRecord], *, interactive_root: Path | None = None
) -> tuple[Path, ...]:
    """Interactive roots and every retained Goal, including finished/abandoned records.

    Ordered by what a bounded scan must not lose: the caller's interactive
    root, then every Goal Record, then older interactive sessions newest first.
    """
    roots = [] if interactive_root is None else [interactive_root]
    roots.extend(record_paths(project_dir, rec.id).jobs_dir for rec in records)
    history = []
    for path in islice(
        project_dir.glob(".interactive_logs/*/.runtime/jobs"), MAX_SCANNED_INTERACTIVE_ROOTS
    ):
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
    return tuple(dict.fromkeys(roots))


def _validate_goal_binding(record: job_records.JobRecord) -> None:
    if record.binding is not None:
        try:
            GoalRunBinding.from_json(record.binding)
        except GoalBindingError as exc:
            raise ValueError(str(exc)) from exc


class JobReader:
    """Compose retained history and display policy over one persistent observer."""

    def __init__(
        self,
        project_dir: Path,
        *,
        slots_root: Path | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self._project_dir = project_dir
        self._observer = JobObserver(
            project_dir,
            slots_root=slots_root,
            proc_root=proc_root,
            validate_record=_validate_goal_binding,
        )

    def read(
        self, records: Sequence[GoalRecord], *, interactive_root: Path | None = None
    ) -> JobSnapshot:
        """Reuse the Goal enumeration that supplied this Dashboard refresh."""
        roots = retained_job_roots(self._project_dir, records, interactive_root=interactive_root)
        observed = self._observer.read(roots)
        catalogs: dict[str, TargetCatalog | None] = {}
        rows = tuple(_job_view(job, catalogs) for job in observed.jobs)
        return JobSnapshot(rows, observed.diagnostics)


def _job_view(job: JobObservation, catalogs: dict[str, TargetCatalog | None]) -> JobView:
    return JobView(
        root=job.root,
        record=job.record,
        state=job.state,
        report=job.report,
        report_path=job.report_path,
        stage=job.stage,
        diagnostic=job.diagnostic,
        artifacts=job.artifacts,
        eda_tool=_configured_tool(job.record, catalogs),
    )


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
