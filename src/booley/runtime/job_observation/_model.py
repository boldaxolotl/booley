"""Immutable observations independent of Goal history and Target presentation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.runtime import job_records


@dataclass(frozen=True)
class JobObservation:
    """A run identity includes its root; duplicate legacy run IDs remain separate."""

    root: Path
    record: job_records.JobRecord
    state: str
    report: dict[str, Any] | None = None
    report_path: Path | None = None
    stage: str = ""
    diagnostic: str = ""
    artifacts: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        """Stable selection identity through completion and session expiry."""
        return str(self.root), self.record.run_id


@dataclass(frozen=True)
class ObservationSnapshot:
    """Bounded rows and isolated diagnostics; history is retained by its producer."""

    jobs: tuple[JobObservation, ...]
    diagnostics: tuple[str, ...] = ()
