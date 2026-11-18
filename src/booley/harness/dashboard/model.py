"""Coherent observational Dashboard data. All filesystem access stays outside UI code."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import time as wall_clock

from booley.harness import upgrade_review
from booley.harness.dashboard.jobs import JobReader, JobSnapshot
from booley.harness.dashboard.measurement import (
    MeasurementLine,
    checkout_of,
    measurement_lines,
)
from booley.harness.dashboard.resources import ProcessSampler, Resources, ResourceSampler
from booley.mcp.session_registry import SessionRegistry, SessionRow, namespace
from booley.runtime import health, health_presentation, job_slots
from booley.runtime.handles import HandleStore
from booley.runtime.pid import ProcessIdentity
from booley.runtime.session_activity import Activity, ActivityStore, observed_activity


@dataclass(frozen=True)
class HealthLine:
    """Literal health text with optional Doctor semantic severity."""

    text: str
    severity: health_presentation.HealthSeverity | None
    # Trailing part of ``text`` rendered in the attention style instead of the severity style.
    due_suffix: str = ""


@dataclass(frozen=True)
class DashboardSnapshot:
    """Immutable presentation snapshot, detached from every acquired lock."""

    sessions: tuple[SessionRow, ...] = ()
    jobs: JobSnapshot = field(default_factory=lambda: JobSnapshot(()))
    resources: Resources = field(default_factory=Resources)
    health: tuple[HealthLine, ...] = ()
    diagnostics: tuple[str, ...] = ()
    observed_at: float = 0
    # Reported activity by session key; a session without an entry is unknown.
    activity: Mapping[str, Activity] = field(default_factory=dict)
    session_handles: Mapping[str, str] = field(default_factory=dict)
    #: Display title per session key (``S3 · uart-fix``); read with the handles in one map read.
    session_titles: Mapping[str, str] = field(default_factory=dict)
    #: The measurement line per checkout a session targets; a checkout without one shows none.
    measurements: Mapping[str, MeasurementLine] = field(default_factory=dict)


def health_lines(root: Path, booley_dir: Path) -> tuple[HealthLine, ...]:
    """Non-consuming Doctor freshness and typed read-only upgrade-review projection."""
    compact = health_presentation.count_line(health.read_health(root))
    lines = [HealthLine(compact.text, compact.severity, compact.due_suffix)]
    review = upgrade_review.read_status(booley_dir)
    if review.condition not in {
        upgrade_review.ReviewCondition.CURRENT,
        upgrade_review.ReviewCondition.UNAVAILABLE,
    }:
        lines.append(
            HealthLine(
                f"Upgrade review {review.condition}: {review.pending_target or review.diagnostic}",
                None,
            )
        )
    return tuple(lines)


class DashboardReader:
    """Reads one Dashboard snapshot from the Booley Directory without retaining locks."""

    def __init__(self, root: Path, booley_dir: Path) -> None:
        self.root = root
        self.booley_dir = booley_dir
        self.resources = ResourceSampler()
        self.processes = ProcessSampler()
        self.jobs = JobReader(booley_dir, slots_root=job_slots.slots_dir(booley_dir))

    def read(self) -> DashboardSnapshot:
        """Read one snapshot without retaining locks or touching persistent state."""
        now = wall_clock()
        sessions = SessionRegistry(self.booley_dir).visible_snapshot(now=now, scope=namespace())
        handles, titles, handle_diagnostics = HandleStore(self.booley_dir).titled_snapshot()
        resources = self.resources.sample(self.root, now=time.monotonic())
        jobs = self.jobs.read(session_handles=handles, now=now)
        measured = []
        for job in jobs.jobs:
            identity = ProcessIdentity.from_payload(job.record.process_identity)
            projected = job
            if identity is not None and job.state == "running":
                sample = self.processes.sample(identity, now=time.monotonic())
                projected = replace(
                    job,
                    cpu_percent=sample.cpu_percent,
                    memory=sample.memory,
                    peak_memory=sample.peak_memory,
                )
            measured.append(projected)
        return DashboardSnapshot(
            sessions.rows,
            replace(jobs, jobs=tuple(measured)),
            resources,
            health_lines(self.root, self.booley_dir),
            (*sessions.diagnostics, *handle_diagnostics),
            now,
            self._activity(sessions.rows, now),
            handles,
            titles,
            measurement_lines({checkout_of(row.attribution) for row in sessions.rows}, measured),
        )

    def _activity(self, rows: tuple[SessionRow, ...], now: float) -> dict[str, Activity]:
        """Join each visible session with the last lifecycle event its client reported."""
        store = ActivityStore(self.booley_dir)
        return {
            row.attribution.key: observed_activity(store.read(row.attribution.key), now=now)
            for row in rows
        }
