"""Run sequence and publication shared by the ASIC Synthesis and FPGA Implementation Flows.

Both Flows plan their Targets, run the optional ``--baseline`` pass once per unique
baseline Target, run each current Target, publish one canonical implementation report,
checkpoint progress after every finished Target and aggregate the verdict. This base
owns that sequence; a Flow supplies the hooks that differ: its plan, how one Target
runs, its result text and its report adapters.
"""

from __future__ import annotations

import contextlib
import copy
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, cast

from booley.core.build_paths import work_root_for
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome
from booley.runtime.filesystem_utils import make_directories
from booley.targets.domain import TargetHandle

from .base import BuiltinFlow, RequestT, SubprocessResult
from .baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
    git_short_sha,
)
from .implementation_comparison import (
    ImplementationComparisonError,
    TargetPairPlan,
    baseline_execution_context,
    candidate_execution_refs,
    selected_target_handle,
    target_pair_plans_for_handles,
    target_plan_for_handle,
)
from .implementation_publication import (
    ImplementationProgress,
    ImplementationProgressRun,
    ImplementationPublisher,
    target_report_path,
)
from .implementation_report import (
    ImplementationAggregate,
    ImplementationContext,
    ImplementationReport,
    build_implementation_aggregate,
    implementation_context,
)
from .plan import FlowPlan
from .progress_lifecycle import ProgressPublicationError
from .result_table import compose_result, note_line, tty_text


class _TargetMetrics(Protocol):
    """The part of a Flow's per-Target metrics the base reads."""

    infra_error: str


class _BaselineRequest(Protocol):
    """The ``--baseline`` field both implementation Flow requests carry."""

    baseline: str | None


MetricsT = TypeVar("MetricsT", bound=_TargetMetrics)


def _ignore_publish(metrics: object) -> None:
    """Publish nothing: a baseline result is fanned out after its Target ran."""


class ImplementationFlow(BuiltinFlow[RequestT], Generic[RequestT, MetricsT]):
    """Target lookup, report publication and checkpoints for an implementation Flow."""

    _implementation_reports: dict[str, ImplementationReport]
    _baseline_full_sha: str | None
    _execution_role: str
    # The initially selected checkout: it tells primary-run artifacts from the
    # temporary baseline ones while ``args.work_dir`` points at a baseline worktree.
    _project_root: Path

    # -- BooleyFlow abstract methods (unused: _run handles Targets directly) ----

    def _resolve_job_class(self) -> str:
        """Implementation runs are memory-admitted Sandbox workloads."""
        return job_slots.CLASS_MEMORY

    def _build_command(self) -> list[str]:
        """Not used: _run drives the Targets."""
        return []

    def _interpret_result(self, result: SubprocessResult) -> EndpointOutcome:  # noqa: ARG002 — overrides Flow._interpret_result
        """Not used: _run interprets each Target."""
        return EndpointOutcome()

    @contextlib.contextmanager
    def _dry_run_build_root(self, target: str) -> Iterator[Path]:
        """A disposable build root for planning *target*, beside its real work root.

        A killed dry run cannot remove it, so it lives in the Booley Directory's
        ignored build tree and never in the Project Root itself.
        """
        parent = work_root_for(self.args.work_dir, self.name, target).parent
        make_directories(parent)
        with tempfile.TemporaryDirectory(prefix=".plan-", dir=parent) as scratch:
            yield Path(scratch) / target

    # -- Flow hooks -----------------------------------------------------------

    def _plan_implementation(self, targets: list[str], /) -> FlowPlan:
        """Resolve candidate and baseline recipes before either revision runs."""
        raise NotImplementedError

    def _run_target(self, target: str, publish: Callable[[MetricsT], None], /) -> MetricsT:
        """Run one Target; call *publish* while its workspace is still owned."""
        raise NotImplementedError

    def _format_config_line(
        self, target: str, current: MetricsT, baseline: MetricsT | None, /, *, human: bool
    ) -> str:
        """Format one Target's completion line."""
        raise NotImplementedError

    def _result_text(
        self,
        targets: list[str],
        current: dict[str, MetricsT],
        baseline: dict[str, MetricsT],
        reports: dict[str, ImplementationReport],
        aggregate: ImplementationAggregate,
        /,
    ) -> tuple[list[str], list[str], str]:
        """Return the result table lines, note lines and ``RESULT:`` headline."""
        raise NotImplementedError

    def _result_detail(
        self,
        targets: list[str],
        current: dict[str, MetricsT],
        baseline: dict[str, MetricsT],
        short_sha: str | None,
        reports: dict[str, ImplementationReport],
        aggregate: ImplementationAggregate,
        /,
    ) -> dict[str, Any]:
        """Return the Flow's structured result detail."""
        raise NotImplementedError

    def _display_lines(self, targets: list[str], current: dict[str, MetricsT], /) -> list[str]:
        """Return the lines a quiet run shows for the first valid Target."""
        raise NotImplementedError

    def _run_policy_error(self) -> str | None:
        """Return the message that blocks the run, or ``None``."""
        return None

    def _begin_run(self, targets: list[str], /) -> None:
        """Reset per-run Flow state once planning has succeeded."""

    def _enter_baseline_worktree(
        self, pairs: Sequence[TargetPairPlan], project_root: Path, worktree: Path, /
    ) -> None:
        """Inspect the baseline checkout before its Targets run."""

    def _build_implementation_report(
        self, context: ImplementationContext, current: MetricsT, baseline: MetricsT | None
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

    # -- Run sequence ---------------------------------------------------------

    def _run(self) -> EndpointOutcome:
        """Plan, run the optional baseline pass, run each Target and aggregate."""
        self._baseline_full_sha = None
        self._implementation_reports = {}
        self._execution_role = "candidate"
        self._project_root = Path(self.args.work_dir)
        handles = self._selected_target_handles()
        self._target_handles = {handle.selector: handle for handle in handles}
        targets = [handle.selector for handle in handles]
        if not targets:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"{self.name}: no Target selected. Pass --target <name> "
                    "(bare name if unambiguous, else vlnv#name)."
                ),
            )
        refusal = self._prepare_target_pairs(handles)
        if refusal is None:
            refusal = self._run_refusal()
        if refusal is not None:
            return refusal
        plan = self._plan_implementation(targets)
        self._flow_plan = plan
        if self.args.dry_run:
            return self._dry_run_result(plan)
        if plan.aggregate_errors:
            return self._planning_error(targets, plan)
        self._begin_run(targets)
        return self._run_planned(targets)

    def _prepare_target_pairs(self, handles: Sequence[TargetHandle]) -> EndpointOutcome | None:
        """Plan each candidate's baseline pair, or refuse the run."""
        try:
            self._target_pairs = target_pair_plans_for_handles(handles, flow=self.name)
            self._target_execution_refs = candidate_execution_refs(handles, self._target_pairs)
        except ImplementationComparisonError as exc:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"{self.name}: {exc}")
        return None

    def _run_refusal(self) -> EndpointOutcome | None:
        """Refuse a disabled Flow, then whatever the Flow's run policy blocks."""
        if not self._flow_enabled():
            message: str | None = f"{self.name} is disabled ([flows.{self.name}].enabled = false)."
        else:
            message = self._run_policy_error()
        if message is None:
            return None
        return EndpointOutcome(exit_code=EXIT_ERROR, report_text=message, refused=True)

    def _planning_error(self, targets: list[str], plan: FlowPlan) -> EndpointOutcome:
        """Refuse the run, listing each failing candidate Target's error."""
        detail: dict[str, Any] = {"plan": plan.as_dict()}
        for target in targets:
            prefix = f"candidate {target}: "
            message = next(
                (
                    error.removeprefix(prefix)
                    for error in plan.aggregate_errors
                    if error.startswith(prefix)
                ),
                None,
            )
            if message is not None:
                detail[target] = {"infra_error": message}
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                f"{self.name}: infrastructure error during planning: "
                + "; ".join(plan.aggregate_errors)
            ),
            detail=detail,
        )

    def _run_planned(self, targets: list[str]) -> EndpointOutcome:
        """Run the baseline and current passes of a planned run under progress."""
        self.reserve_invocation_dir()
        progress = ImplementationProgressRun(
            self.name,
            lambda current, baseline, phase, baseline_ref, complete: self._write_progress_report(
                targets,
                current,
                baseline,
                phase=phase,
                baseline_ref=baseline_ref,
                complete=complete,
            ),
        )
        try:
            with progress:
                baseline_outcome, short_sha = self._run_baseline_configs(
                    self._target_pairs, progress.baseline_results
                )
                if isinstance(baseline_outcome, EndpointOutcome):
                    return baseline_outcome
                progress.baseline_ref = short_sha
                current_results = self._run_current_targets(
                    targets, progress.baseline_results, short_sha, progress.current_results
                )
                result = self._aggregate_results(
                    targets, current_results, progress.baseline_results, short_sha
                )
                return progress.complete(result)
        except ProgressPublicationError as exc:
            return progress.publication_failure(exc)

    def _run_baseline_configs(
        self, pairs: Sequence[TargetPairPlan], baseline_results: dict[str, MetricsT] | None = None
    ) -> tuple[dict[str, MetricsT] | EndpointOutcome, str | None]:
        """Run each paired baseline Target in an ephemeral ``git worktree``.

        ``args.work_dir`` points at the worktree while it runs and the selected
        checkout's handles, execution refs and role are restored afterwards, even
        when a baseline Target raises.
        """
        baseline_ref = cast(_BaselineRequest, self.args).baseline
        if not baseline_ref:
            return baseline_results or {}, None
        project_root = Path(self.args.work_dir)
        short_sha = git_short_sha(baseline_ref, project_root)
        full_sha = git_full_sha(str(baseline_ref), project_root)
        if full_sha is not None:
            self._baseline_full_sha = full_sha
        self._project_root = project_root
        try:
            with baseline_worktree(
                project_root, baseline_ref, paired_project=self._paired_project_baseline
            ) as worktree:
                handles = getattr(self, "_target_handles", {})
                refs = getattr(self, "_target_execution_refs", {})
                role = getattr(self, "_execution_role", "candidate")
                try:
                    self.args.work_dir = worktree
                    self._target_handles, self._target_execution_refs = baseline_execution_context(
                        pairs, worktree
                    )
                    self._execution_role = "baseline"
                    self._enter_baseline_worktree(pairs, project_root, worktree)
                    results = self._execute_baseline_pairs(pairs, short_sha, baseline_results)
                finally:
                    self._target_handles = handles
                    self._target_execution_refs = refs
                    self._execution_role = role
                    self.args.work_dir = project_root
        except (BaselineWorktreeError, ImplementationComparisonError) as exc:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"{self.name}: {exc}"), None
        return results, short_sha

    def _execute_baseline_pairs(
        self,
        pairs: Sequence[TargetPairPlan],
        short_sha: str,
        results: dict[str, MetricsT] | None = None,
    ) -> dict[str, MetricsT]:
        """Run each unique baseline Target once and copy it to every paired candidate."""
        results = results if results is not None else {}
        executed: dict[str, MetricsT] = {}
        targets = [plan.candidate.selector for plan in pairs]
        for plan in pairs:
            baseline = plan.baseline.selector
            if baseline not in executed:
                executed[baseline] = self._run_target(baseline, _ignore_publish)
            results[plan.candidate.selector] = copy.deepcopy(executed[baseline])
            self._write_progress_report(
                targets, {}, results, phase="baseline", baseline_ref=short_sha
            )
        return results

    def _run_current_targets(
        self,
        targets: list[str],
        baseline_results: dict[str, MetricsT],
        short_sha: str | None,
        current_results: dict[str, MetricsT] | None = None,
    ) -> dict[str, MetricsT]:
        """Run each Target, persisting and checkpointing it while its workspace is owned."""
        current_results = current_results if current_results is not None else {}
        for target in targets:

            def publish(metrics: MetricsT, target: str = target) -> None:
                self._persist_target_outcome(
                    target, metrics, baseline_results.get(target), short_sha
                )
                current_results[target] = metrics
                self._write_progress_report(
                    targets,
                    current_results,
                    baseline_results,
                    phase="current",
                    baseline_ref=short_sha,
                )

            metrics = self._run_target(target, publish)
            if len(targets) > 1:
                self.emit_completion(
                    self._format_config_line(
                        target, metrics, baseline_results.get(target), human=True
                    )
                )
        return current_results

    def _aggregate_results(
        self,
        targets: list[str],
        current_results: dict[str, MetricsT],
        baseline_results: dict[str, MetricsT],
        short_sha: str | None,
    ) -> EndpointOutcome:
        """Build and print the run verdict from every Target's published report."""
        reports = {
            target: getattr(self, "_implementation_reports", {}).get(target)
            or self._implementation_report(
                target, current_results[target], baseline_results.get(target), short_sha
            )
            for target in targets
        }
        aggregate = build_implementation_aggregate(
            reports, baseline_ref=getattr(self, "_baseline_full_sha", None) or short_sha
        )
        table, notes, headline = self._result_text(
            targets, current_results, baseline_results, reports, aggregate
        )
        result = compose_result(
            short_sha if cast(_BaselineRequest, self.args).baseline else None,
            table,
            notes,
            headline,
        )
        print(tty_text(result), flush=True)
        return EndpointOutcome(
            exit_code=aggregate.exit_code,
            report_text=result.record,
            display_lines=self._display_lines(targets, current_results),
            detail=self._result_detail(
                targets, current_results, baseline_results, short_sha, reports, aggregate
            ),
        )

    # -- Target lookup --------------------------------------------------------

    def _target_handle(self, target: str) -> TargetHandle:
        """Return the selected handle, reselecting only in a baseline checkout."""
        return selected_target_handle(
            getattr(self, "_target_handles", {}), self.args.work_dir, target, flow=self.name
        )

    def _target_plan(self, target: str) -> TargetPairPlan:
        """Look up the total pair plan by canonical candidate identity."""
        return target_plan_for_handle(
            getattr(self, "_target_pairs", ()), self._target_handle(target), flow=self.name
        )

    # -- Publication ----------------------------------------------------------

    def _implementation_report(
        self, target: str, current: MetricsT, baseline: MetricsT | None, baseline_ref: str | None
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
        make_directories(report_dir)
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
        self, target: str, current: MetricsT, baseline: MetricsT | None, baseline_ref: str | None
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
        baseline_results: dict[str, MetricsT], reports: dict[str, ImplementationReport]
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
