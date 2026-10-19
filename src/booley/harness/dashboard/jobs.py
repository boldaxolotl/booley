"""Dashboard Job history, Goal metadata validation and Target display enrichment."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from booley.core.boundary import as_dict, as_float
from booley.goals.binding import GoalBindingError, GoalRunBinding
from booley.goals.model import GoalRecord
from booley.goals.paths import record_paths
from booley.runtime import job_records
from booley.runtime.handles import HandleStore
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
    handle: str = ""
    owner_handle: str = ""

    @property
    def recorded_resources(self) -> tuple[float, float] | None:
        """Validated recorded CPU seconds and largest-process RSS in MiB."""
        values = as_dict((self.report or {}).get("resources")) or {}
        try:
            numbers = [
                as_float(values.get(key)) for key in ("cpu_user_s", "cpu_sys_s", "peak_rss_mb")
            ]
        except OverflowError:
            return None
        user, system, peak = numbers
        if user is None or system is None or peak is None:
            return None
        return user + system, peak

    @property
    def recorded_tools(self) -> str:
        """Retain Target association and reject malformed optional tool records."""
        report = self.report or {}
        tools = report.get("eda_tools")
        if isinstance(tools, dict) and tools:
            return ", ".join(
                f"{key}: {value or 'not recorded by this Flow'}"
                for key, value in sorted(tools.items())
            )
        tool = report.get("eda_tool") or report.get("tool")
        return tool if isinstance(tool, str) else ""


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
    booley_dir: Path, records: Sequence[GoalRecord], *, interactive_root: Path | None = None
) -> tuple[Path, ...]:
    """Interactive roots and every retained Goal, including finished/abandoned records.

    Ordered by what a bounded scan must not lose: the caller's interactive
    root, then every Goal Record, then older interactive sessions newest first.
    """
    roots = [] if interactive_root is None else [interactive_root]
    from booley.goals.sim_baseline_store import baseline_jobs_dir

    roots.extend(record_paths(booley_dir, rec.id).jobs_dir for rec in records)
    baseline = baseline_jobs_dir(booley_dir)
    if baseline.is_dir():
        roots.append(baseline)
    history = []
    for path in islice(
        booley_dir.glob(".interactive_logs/*/.runtime/jobs"), MAX_SCANNED_INTERACTIVE_ROOTS
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
        booley_dir: Path,
        *,
        slots_root: Path | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self._booley_dir = booley_dir
        self._observer = JobObserver(
            booley_dir,
            slots_root=slots_root,
            proc_root=proc_root,
            validate_record=_validate_goal_binding,
        )

    def read(
        self,
        records: Sequence[GoalRecord],
        *,
        interactive_root: Path | None = None,
        session_handles: dict[str, str] | None = None,
    ) -> JobSnapshot:
        """Reuse the Goal enumeration that supplied this Dashboard refresh."""
        roots = retained_job_roots(self._booley_dir, records, interactive_root=interactive_root)
        observed = self._observer.read(roots)
        catalogs: dict[str, TargetCatalog | None] = {}
        handles, diagnostics = HandleStore(self._booley_dir, jobs=True).snapshot()
        owners, owner_diagnostics = (
            HandleStore(self._booley_dir).snapshot()
            if session_handles is None
            else (session_handles, ())
        )
        rows = tuple(_job_view(job, catalogs, handles, owners) for job in observed.jobs)
        return JobSnapshot(rows, (*observed.diagnostics, *diagnostics, *owner_diagnostics))


def _job_view(
    job: JobObservation,
    catalogs: dict[str, TargetCatalog | None],
    handles: dict[str, str],
    owners: dict[str, str],
) -> JobView:
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
        handle=handles.get(job.record.run_id, ""),
        owner_handle=owners.get(job.record.session_key or "", ""),
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
