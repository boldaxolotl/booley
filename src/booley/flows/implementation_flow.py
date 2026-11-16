"""Per-Target publication shared by the ASIC Synthesis and FPGA Implementation Flows.

Both Flows run each Target, publish one canonical implementation report, checkpoint
progress after every finished Target and turn baseline and comparison errors into
notes. This base owns those steps; a Flow supplies only its two report hooks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, cast

from booley.runtime import job_slots
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.domain import TargetHandle

from .base import BuiltinFlow, RequestT, SubprocessResult
from .implementation_comparison import (
    TargetPairPlan,
    selected_target_handle,
    target_plan_for_handle,
)
from .implementation_publication import (
    ImplementationProgress,
    ImplementationPublisher,
    target_report_path,
)
from .implementation_report import (
    ImplementationContext,
    ImplementationReport,
    implementation_context,
)
from .result_table import note_line


class _TargetMetrics(Protocol):
    """The part of a Flow's per-Target metrics the base reads."""

    infra_error: str


class _BaselineRequest(Protocol):
    """The ``--baseline`` field both implementation Flow requests carry."""

    baseline: str | None


MetricsT = TypeVar("MetricsT", bound=_TargetMetrics)


class ImplementationFlow(BuiltinFlow[RequestT], Generic[RequestT, MetricsT]):
    """Target lookup, report publication and checkpoints for an implementation Flow."""

    _implementation_reports: dict[str, ImplementationReport]

    # -- BooleyFlow abstract methods (unused: _run handles Targets directly) ----

    def _resolve_job_class(self) -> str:
        """Implementation runs are memory-admitted Sandbox workloads."""
        return job_slots.CLASS_MEMORY

    def _build_command(self) -> list[str]:
        """Not used: _run drives the Targets."""
        return []

    def _interpret_result(self, result: SubprocessResult) -> EndpointOutcome:
        """Not used: _run interprets each Target."""
        return EndpointOutcome()

    # -- Flow hooks -----------------------------------------------------------

    def _build_implementation_report(
        self,
        context: ImplementationContext,
        current: MetricsT,
        baseline: MetricsT | None,
    ) -> ImplementationReport:
        """Adapt the Flow's native evidence to the canonical report."""
        raise NotImplementedError

    def _target_report_payload(
        self,
        target: str,
        current: MetricsT,
        baseline: MetricsT | None,
        baseline_ref: str | None,
        report_path: Path,
    ) -> dict[str, Any]:
        """Return the Flow's per-Target JSON report before the verdict is set."""
        raise NotImplementedError

    # -- Target lookup --------------------------------------------------------

    def _target_handle(self, target: str) -> TargetHandle:
        """Return the selected handle, reselecting only in a baseline checkout."""
        return selected_target_handle(
            getattr(self, "_target_handles", {}),
            self.args.work_dir,
            target,
            flow=self.name,
        )

    def _target_plan(self, target: str) -> TargetPairPlan:
        """Look up the total pair plan by canonical candidate identity."""
        return target_plan_for_handle(
            getattr(self, "_target_pairs", ()),
            self._target_handle(target),
            flow=self.name,
        )

    # -- Publication ----------------------------------------------------------

    def _implementation_report(
        self,
        target: str,
        current: MetricsT,
        baseline: MetricsT | None,
        baseline_ref: str | None,
    ) -> ImplementationReport:
        """Build the canonical report for one Target from its run context."""
        context = implementation_context(
            self._target_plan(target),
            target=target,
            eda_tool=self._eda_tool,
            baseline_ran=baseline is not None,
            requested_baseline_ref=cast(_BaselineRequest, self.args).baseline,
            resolved_baseline_ref=getattr(self, "_baseline_full_sha", None) or baseline_ref,
        )
        return self._build_implementation_report(context, current, baseline)

    def _publisher(self) -> ImplementationPublisher:
        return ImplementationPublisher(
            work_dir=Path(self.args.work_dir),
            report_dir=self.args.report_dir,
            invocation_dir=self.reserve_invocation_dir(),
        )

    def _write_target_report(
        self,
        target: str,
        current: MetricsT,
        baseline: MetricsT | None,
        baseline_ref: str | None,
        implementation: ImplementationReport,
    ) -> ImplementationReport:
        """Publish the per-Target JSON report; the canonical verdict wins over the payload."""
        report_dir = self.args.report_dir
        if report_dir is None:
            return implementation
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = target_report_path(self.name, target, report_dir)
        report = self._target_report_payload(target, current, baseline, baseline_ref, report_path)
        report["passed"] = implementation.passed
        published = self._publisher().publish_report(implementation, report)
        return ImplementationReport(published.payload["implementation"])

    def _write_progress_report(
        self,
        targets: list[str],
        current_results: dict[str, MetricsT],
        baseline_results: dict[str, MetricsT],
        *,
        phase: str,
        baseline_ref: str | None = None,
        complete: bool = False,
    ) -> None:
        """Checkpoint a long matrix after every Target and phase transition."""
        progress = ImplementationProgress(
            flow=self.name,
            run_id=os.environ.get("BOOLEY_RUN_ID", ""),
            targets=tuple(targets),
            completed_targets=tuple(current_results),
            baseline_completed_targets=tuple(baseline_results),
            phase=phase,
            complete=complete,
            baseline_ref=baseline_ref,
            reports=getattr(self, "_implementation_reports", {}),
        )
        self._publisher().publish_progress(progress)

    def _persist_target_outcome(
        self,
        target: str,
        current: MetricsT,
        baseline: MetricsT | None,
        baseline_ref: str | None,
    ) -> None:
        """Durably record one terminal Target before the next one starts."""
        implementation = self._implementation_report(target, current, baseline, baseline_ref)
        implementation = self._write_target_report(
            target, current, baseline, baseline_ref, implementation
        )
        self._implementation_reports[target] = implementation

    # -- Run-level notes ------------------------------------------------------

    @staticmethod
    def _baseline_error_notes(
        baseline_results: dict[str, MetricsT],
        reports: dict[str, ImplementationReport],
    ) -> tuple[list[str], list[str]]:
        """Return note lines and failure reasons for baseline and comparison errors."""
        lines: list[str] = []
        failures: list[str] = []
        for target, metrics in baseline_results.items():
            if metrics.infra_error:
                lines.append(note_line(f"baseline {target}", "ERROR", metrics.infra_error))
                failures.append(f"baseline {target}: {metrics.infra_error}")
        for target, report in reports.items():
            comparison = report.canonical.get("comparison")
            errors = comparison.get("basis_errors", []) if isinstance(comparison, dict) else []
            for message in errors:
                if str(message).startswith("baseline infrastructure error:"):
                    continue
                lines.append(note_line(f"comparison {target}", "ERROR", str(message)))
                failures.append(f"comparison {target}: {message}")
        return lines, failures
