"""Observational progress for one sequential Coverage Campaign invocation."""

from dataclasses import dataclass, field
from pathlib import Path

from .campaign_reports import write_campaign_json
from .coverage_campaign import coverage_mapping_document
from .coverage_transaction import CoverageTargetOutcome


@dataclass
class CoverageProgress:
    invocation_dir: Path
    targets: tuple[str, ...]
    outcomes: list[CoverageTargetOutcome] = field(default_factory=list)

    def completed(self, outcome: CoverageTargetOutcome) -> None:
        self.outcomes.append(outcome)
        self.checkpoint()

    def checkpoint(self, *, complete: bool = False) -> None:
        done = [outcome.target for outcome in self.outcomes]
        pending = [target for target in self.targets if target not in done]
        phase = "aborted" if complete and pending else ("complete" if complete else "running")
        write_campaign_json(
            self.invocation_dir / "progress.json",
            {
                "flow": "sim",
                "coverage": True,
                "complete": complete,
                "phase": phase,
                "targets": list(self.targets),
                "completed_targets": done,
                "pending_targets": pending,
                "detail": {
                    outcome.target: {
                        **coverage_mapping_document(outcome.detail),
                        "exit_code": outcome.exit_code,
                    }
                    for outcome in self.outcomes
                },
            },
        )
