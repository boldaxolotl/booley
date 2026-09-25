"""Observational progress for one sequential Coverage Campaign invocation."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from booley.flows.progress_lifecycle import progress_document, write_progress_json

from .coverage_campaign import coverage_mapping_document
from .coverage_transaction import CoverageTargetOutcome


@dataclass
class CoverageProgress:
    invocation_dir: Path
    targets: tuple[str, ...]
    outcomes: list[CoverageTargetOutcome] = field(default_factory=list)

    def completed(self, outcome: CoverageTargetOutcome) -> None:
        if outcome.abort_remaining:
            self.checkpoint()
            return
        self.outcomes.append(outcome)
        try:
            self.checkpoint()
        except Exception:
            self.outcomes.pop()
            raise

    def checkpoint(self, *, complete: bool = False, phase: str | None = None) -> None:
        done = [outcome.target for outcome in self.outcomes]
        resolved_phase = phase or (
            "aborted"
            if complete and len(done) != len(self.targets)
            else ("complete" if complete else "running")
        )
        if complete != (resolved_phase in {"complete", "aborted", "superseded"}):
            raise ValueError("coverage progress complete and phase disagree")
        write_progress_json(
            self.invocation_dir / "progress.json",
            progress_document(
                flow="sim",
                run_id=os.environ.get("BOOLEY_RUN_ID", ""),
                phase=resolved_phase,
                targets=self.targets,
                completed_targets=done,
                detail={
                    outcome.target: {
                        **coverage_mapping_document(outcome.detail),
                        "exit_code": outcome.exit_code,
                    }
                    for outcome in self.outcomes
                },
                extra={"coverage": True},
            ),
        )
