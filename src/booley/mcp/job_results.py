"""Internal durable Job and report lookup over the existing runtime owners.

Records and slots remain authoritative. This module locates retained roots,
validates Goal bindings, and decides which reports can vouch for a Job outcome.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.flows.progress_lifecycle import read_progress_for_run
from booley.goals.binding import GoalBindingError, GoalRunBinding
from booley.goals.paths import record_paths
from booley.mcp.call_context import CallContext, container_jobs_root, project_goal_store
from booley.runtime import job_records as jobrec
from booley.runtime import job_slots
from booley.runtime.pid import is_pid_alive
from booley.runtime.session_paths import logs_runtime_dir

logger = logging.getLogger(__name__)
_KNOWN_JOB_ROOTS: dict[str, Path | None] = {}


@dataclass(frozen=True)
class LocatedJob:
    """A durable Job and the exact root it was read from."""

    record: jobrec.JobRecord
    root: Path | None


def job_roots(*, container_root: Callable[[], Path | None] | None = None) -> tuple[Path, ...]:
    """Container jobs and every retained Goal Record, including terminal ones."""
    roots = [(container_root or container_jobs_root)()]
    store = project_goal_store()
    if store is None:
        return tuple(root for root in roots if root is not None)
    from booley.goals.sim_baseline_store import BASELINE_FAMILIES, baseline_jobs_dir

    for family in BASELINE_FAMILIES:
        baseline = baseline_jobs_dir(store.booley_dir, family)
        if baseline.is_dir():
            roots.append(baseline)
    roots.extend(
        record_paths(store.booley_dir, rec.id).jobs_dir for rec in store.list_records().records
    )
    return tuple(dict.fromkeys(root for root in roots if root is not None))


def read_record_or_none(run_id: str, root: Path | None) -> jobrec.JobRecord | None:
    """Read a record for a by-id lookup; a record unreadable after retries counts as not found.

    Terminal, cancel and reconcile writes resolve their root through this
    lookup, so a held record must not make it raise. Best effort: a Job whose
    root cannot be resolved because its record is held still skips that write.
    """
    try:
        return jobrec.read_record(run_id, root=root)
    except OSError as exc:
        logger.warning("Job record %s unreadable under %s: %s", run_id, root, exc)
        return None


def _locate_job(run_id: str, *, roots: tuple[Path, ...] | None = None) -> LocatedJob | None:
    """Find a unique run across roots; refuse even legacy collisions."""
    search_roots: list[Path | None] = list(job_roots() if roots is None else roots)
    if run_id in _KNOWN_JOB_ROOTS and _KNOWN_JOB_ROOTS[run_id] not in search_roots:
        search_roots.insert(0, _KNOWN_JOB_ROOTS[run_id])
    found = [
        LocatedJob(rec, root)
        for root in search_roots
        if (rec := read_record_or_none(run_id, root)) is not None
    ]
    if len(found) > 1:
        raise jobrec.JobRecordError(f"Ambiguous run_id {run_id!r}: present in multiple job roots")
    if found:
        job_binding(found[0].record)
    return found[0] if found else None


def job_binding(rec: jobrec.JobRecord) -> GoalRunBinding | None:
    """Interpret a persisted Goal binding at the MCP composition boundary."""
    if rec.binding is None:
        return None
    try:
        return GoalRunBinding.from_json(rec.binding)
    except GoalBindingError as exc:
        raise jobrec.JobRecordError(f"Malformed Goal binding for Job {rec.run_id}: {exc}") from exc


def _job_record(run_id: str) -> jobrec.JobRecord | None:
    located = _locate_job(run_id)
    return None if located is None else located.record


def _job_phase(run_id: str) -> str:
    return _phase_for_record(_job_record(run_id))


def _phase_for_record(rec: jobrec.JobRecord | None) -> str:
    """Admission phase of a detached job: "RUNNING" or "QUEUED (position N)".

    Derived from the child's own slot-store claim (ADR 0028): a waiter entry
    means the child process is alive but idling for its class slot. Position
    is 1-based for humans. Falls back to RUNNING when the store or claim is
    unreadable — over-claiming "queued" would stall agents that should poll.
    """
    root = job_slots.slots_dir()
    if rec is None or rec.pid is None or root is None:
        return "RUNNING"
    try:
        state = job_slots.SlotStore(root).state_for_pid(rec.pid)
    except OSError:
        return "RUNNING"
    if state is not None and state.state == job_slots.QUEUED:
        return f"QUEUED (position {(state.position or 0) + 1})"
    return "RUNNING"


def _running_progress(
    run_id: str, *, repository: JobRepository | None = None
) -> dict[str, Any] | None:
    """Return only this live job's run-scoped nonterminal checkpoint."""
    repository = repository or JobRepository()
    rec = repository.record(run_id)
    if rec is None:
        return None
    progress = repository.progress_for_run(rec.endpoint, rec.run_id)
    if progress is None or progress.get("complete") is True:
        return None
    return progress


def _report_for_run_id(
    endpoint: str, run_id: str, *, report_dirs: tuple[Path, ...] | None = None
) -> dict[str, Any] | None:
    """Scan an endpoint's numbered report dirs for *run_id*'s report.

    Fallback when the flat ``<endpoint>.json`` belongs to a different run.
    """
    reports = _endpoint_report_dirs(_locate_job(run_id)) if report_dirs is None else report_dirs
    if not reports:
        return None
    numbered = sorted(
        (path for root in reports for path in root.glob(f"{endpoint}/*/report.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in numbered:
        report = _read_report_json(path)
        if report is not None and report.get("run_id") == run_id:
            return report
    return None


def _progress_for_run_id(
    endpoint: str,
    run_id: str,
    *,
    report_dirs: tuple[Path, ...] | None = None,
    booley_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Newest run-scoped checkpoint when a killed matrix has no final report."""
    reports = _endpoint_report_dirs(_locate_job(run_id)) if report_dirs is None else report_dirs
    if not reports:
        return None
    found = read_progress_for_run(reports, endpoint, run_id, booley_dir=booley_dir)
    if found is None:
        return None
    _path, report = found
    pending = report.get("pending_targets")
    report["partial"] = report.get("phase") != "complete" or bool(pending)
    return report


def _job_report(
    rec: jobrec.JobRecord | None, *, repository: JobRepository | None = None
) -> tuple[dict[str, Any] | None, bool]:
    """The report belonging to *rec*'s run, plus whether it provably is.

    Returns ``(report, fresh)``. Identity first: a report stamped with a
    ``run_id`` (written by the child from ``BOOLEY_RUN_ID``) either matches
    the job — proof, ``fresh=True`` — or names a different concurrent run of
    the same endpoint, in which case the numbered invocation dirs are scanned for
    ours and the mismatched flat copy is never shown under this job's
    EXIT_CODE. Time-window gating survives only for legacy reports with no
    ``run_id``: the report's ``timestamp`` must fall inside
    ``[started_at, run_start + timeout_s + slack]``, where ``run_start`` is
    the observed ``run_started_at`` when the record has one — a job that
    queued past its own budget (ADR 0028) still writes its report inside the
    window. When a stamp is missing/unparseable we cannot judge: the report
    is kept (matching the pre-ADR-0027 attach behaviour) but never counts as
    fresh, so it can be shown yet never *vouches* for the run's outcome.
    """
    repository = repository or JobRepository()
    if rec is None:
        report = repository.latest_report()
        if report is None:
            return None, False
        return report, False

    # Prefer identity-stamped invocation artifacts before consulting the
    # last-writer-wins flat copy.  In particular, an old legacy flat report
    # must not hide this run's durable progress checkpoint after a worker dies.
    report = repository.report_for_run(rec.endpoint, rec.run_id)
    if report is None:
        report = repository.progress_for_run(rec.endpoint, rec.run_id)
    if report is not None:
        return report, True

    report = repository.latest_report(rec.endpoint, located=repository.locate(rec.run_id))
    if report is None:
        return None, False

    report_run_id = report.get("run_id")
    if isinstance(report_run_id, str) and report_run_id:
        return (report, True) if report_run_id == rec.run_id else (None, False)
    return _gate_report_by_window(rec, report)


def _gate_report_by_window(
    rec: jobrec.JobRecord,
    report: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """Legacy freshness gate for reports with no ``run_id`` (see _job_report)."""
    report_ts = jobrec.parse_stamp(report.get("timestamp"))
    started_ts = jobrec.parse_stamp(rec.started_at)
    if report_ts is None or started_ts is None:
        return report, False
    if report_ts < started_ts:
        return None, False
    run_start_ts = jobrec.parse_stamp(rec.run_started_at) or started_ts
    if isinstance(rec.timeout_s, (int, float)) and (
        report_ts > run_start_ts + rec.timeout_s + jobrec.DEADLINE_SLACK_SECONDS
    ):
        return None, False
    return report, True


def _endpoint_report_dirs(
    located: LocatedJob | None = None, *, context: CallContext | None = None
) -> tuple[Path, ...]:
    """Return existing Flow and MCP endpoint report directories."""
    if located is not None and located.record.binding is not None:
        binding = job_binding(located.record)
        assert binding is not None
        runtime_dir = record_paths(binding.booley_dir, binding.record_id).runtime_dir
    elif context is not None and context.runtime_dir is not None:
        runtime_dir = context.runtime_dir
    elif located is not None and located.root is not None:
        runtime_dir = located.root.parent
    else:
        logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
        if not logs_dir:
            return ()
        runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
        runtime_dir = Path(runtime_env) if runtime_env else logs_runtime_dir(logs_dir)
    candidates = (runtime_dir / "flow-reports", runtime_dir / "mcp-tool-reports")
    return tuple(path for path in candidates if path.is_dir())


def _read_report_json(path: Path) -> dict[str, Any] | None:
    """Parse a report.json, logging (not raising) on malformed/unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("Malformed report.json at %s: %s", path, e)
        return None
    except OSError as e:
        logger.warning("Failed to read report.json at %s: %s", path, e)
        return None


def _latest_report(
    endpoint: str | None = None,
    *,
    located: LocatedJob | None = None,
    context: CallContext | None = None,
    report_dirs: tuple[Path, ...] | None = None,
) -> dict[str, Any] | None:
    """Read the most recent run report from disk.

    With *endpoint* set, prefer that endpoint's flat ``<endpoint>.json`` (the latest copy
    ``base.write_report`` refreshes each run), falling back to its newest
    numbered ``<endpoint>/<N>/report.json``. Without *endpoint*, return the most recent
    ``report.json`` across every endpoint (the inline dispatch behaviour).
    """
    report_dirs = (
        _endpoint_report_dirs(located, context=context) if report_dirs is None else report_dirs
    )
    if not report_dirs:
        return None

    if endpoint:
        flats = [reports / f"{endpoint}.json" for reports in report_dirs]
        existing_flats = [path for path in flats if path.is_file()]
        if existing_flats:
            return _read_report_json(max(existing_flats, key=lambda path: path.stat().st_mtime))
        numbered = sorted(
            (
                path
                for reports in report_dirs
                for path in reports.glob(f"{endpoint}/*/report.json")
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return _read_report_json(numbered[0]) if numbered else None

    latest: Path | None = None
    latest_mtime = 0.0
    for report_file in (path for reports in report_dirs for path in reports.rglob("report.json")):
        mtime = report_file.stat().st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest = report_file
    return _read_report_json(latest) if latest else None


def _try_read_report() -> dict[str, Any] | None:
    """Read the most recent report.json across all endpoints (inline dispatch)."""
    return _latest_report(None)


def _available_report_endpoints(*, context: CallContext | None = None) -> list[str]:
    """Names of endpoints that have a flat report on disk."""
    return sorted(
        {
            p.stem
            for reports in _endpoint_report_dirs(context=context)
            for p in reports.glob("*.json")
        }
    )


def _strip_transcript_dir(argv: list[str]) -> list[str]:
    """*argv* minus any ``--transcript-dir <path>`` pair.

    The dispatch layer appends a per-call numbered transcript dir to every
    specialist invocation, so two submits of the SAME logical call never have
    equal raw argv — comparing without it is what makes idempotent attach
    reachable for specialists at all (the endpoints whose duplicate submits
    each burn a full LLM sub-agent run). Comparison only: records keep the
    full argv, which the /proc identity guards need verbatim.
    """
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--transcript-dir":
            skip_next = True
            continue
        out.append(arg)
    return out


def _find_attachable_job(
    name: str,
    cmd: list[str],
    *,
    context: CallContext | None = None,
    roots: tuple[Path, ...] | None = None,
) -> str | None:
    """run_id of a live (running or queued) job matching endpoint + argv, or None.

    Scans the durable records rather than any in-process slot: the identical
    job may have been submitted by a previous server generation. Liveness is
    judged by ``derive_status`` (PID + deadline + argv identity), so a stale
    record cannot capture the new submit. Argv is compared modulo the
    per-call ``--transcript-dir`` (see ``_strip_transcript_dir``).
    """
    wanted = _strip_transcript_dir(cmd)
    newest: jobrec.JobRecord | None = None
    for rec in (
        rec
        for root in (job_roots() if roots is None else roots)
        for rec in jobrec.list_records(root=root)
    ):
        wanted_binding = (
            None if context is None or context.binding is None else context.binding.to_json()
        )
        if _attach_binding_key(rec.binding) != _attach_binding_key(wanted_binding):
            continue
        if rec.endpoint != name or _strip_transcript_dir(rec.argv) != wanted:
            continue
        if rec.status != jobrec.STATUS_RUNNING:
            continue
        if not jobrec.is_active(rec, is_pid_alive):
            continue
        # started_at is a fixed-width ISO stamp: string order == time order.
        if newest is None or rec.started_at > newest.started_at:
            newest = rec
    return newest.run_id if newest is not None else None


def _attach_binding_key(binding: dict[str, Any] | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    try:
        validated = GoalRunBinding.from_json(binding).to_json()
    except GoalBindingError as exc:
        raise jobrec.JobRecordError(f"Malformed Goal Job binding: {exc}") from exc
    return {
        key: value
        for key, value in validated.items()
        if key not in {"invocation_id", "record_revision"}
    }


class JobRepository:
    """Private lookup seam shared by supervision and standalone report fetching."""

    def roots(self) -> tuple[Path, ...]:
        return job_roots()

    def locate(self, run_id: str) -> LocatedJob | None:
        return _locate_job(run_id, roots=self.roots())

    def record(self, run_id: str) -> jobrec.JobRecord | None:
        located = self.locate(run_id)
        return None if located is None else located.record

    def report_dirs(
        self, located: LocatedJob | None = None, *, context: CallContext | None = None
    ) -> tuple[Path, ...]:
        return _endpoint_report_dirs(located, context=context)

    def latest_report(
        self,
        endpoint: str | None = None,
        *,
        located: LocatedJob | None = None,
        context: CallContext | None = None,
    ) -> dict[str, Any] | None:
        return _latest_report(endpoint, report_dirs=self.report_dirs(located, context=context))

    def report_for_run(self, endpoint: str, run_id: str) -> dict[str, Any] | None:
        return _report_for_run_id(
            endpoint, run_id, report_dirs=self.report_dirs(self.locate(run_id))
        )

    def report_booley_dir(self, located: LocatedJob | None) -> Path | None:
        """The Booley Directory entry the Job's report roots live under, if any.

        Mirrors the root choice in ``_endpoint_report_dirs``: a Goal-bound Job's
        own directory, otherwise the session's.
        """
        if located is not None and located.record.binding is not None:
            binding = job_binding(located.record)
            assert binding is not None
            return binding.booley_dir
        store = project_goal_store()
        return None if store is None else store.booley_dir

    def progress_for_run(self, endpoint: str, run_id: str) -> dict[str, Any] | None:
        located = self.locate(run_id)
        return _progress_for_run_id(
            endpoint,
            run_id,
            report_dirs=self.report_dirs(located),
            booley_dir=self.report_booley_dir(located),
        )

    def report(self, rec: jobrec.JobRecord | None) -> tuple[dict[str, Any] | None, bool]:
        return _job_report(rec, repository=self)

    def phase(self, run_id: str) -> str:
        return _phase_for_record(self.record(run_id))

    def progress(self, run_id: str) -> dict[str, Any] | None:
        return _running_progress(run_id, repository=self)

    def find_attachable(
        self, name: str, cmd: list[str], *, context: CallContext | None = None
    ) -> str | None:
        return _find_attachable_job(name, cmd, context=context, roots=self.roots())
