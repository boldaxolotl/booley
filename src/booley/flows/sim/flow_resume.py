"""Simulation Record resume (`--resume-from`) for the Simulation Flow.

The validated manifest, never ``progress.json``, decides what resume recovers.
Missing, malformed, or concurrently changed origin progress only skips marking
the origin ``superseded``.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.progress_lifecycle import (
    supersede_progress,
)
from booley.flows.sim.coverage_invocation import CoverageTargetPlan
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.flows.sim.live_progress import (
    attempt_scope,
)
from booley.flows.test_runs import TestRun
from booley.fusesoc import fusesoc_registry
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    EndpointOutcome,
)
from booley.targets.catalog import TargetCatalog

from ..baseline_worktree import (
    git_full_sha,
)
from .execution import (
    SelectedTestRuns,
)
from .record import (
    ResumeSimulationRecordPreview,
    ResumeSimulationRecordPreviewRequest,
    ResumeSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordCancellationError,
    SimulationRecordIntegrityError,
    SimulationRecordManifest,
    SimulationRecordManifestView,
    SimulationRecordOutcome,
    SimulationRecordPlan,
    SimulationRecordRecoveryStatus,
    ValidatedResumeManifest,
)
from .record.coordinator import SimulationRecordPublicationError
from .record.coverage_execution import (
    CoverageAggregateExecutor,
    _CoverageAggregateError,
)
from .record.flow_planning import (
    plan_coarse_simulation_record,
    plan_ordinary_hdl_simulation_record,
)
from .record.planning import (
    SimulationRecordWorkloadMismatchError,
    verify_workload,
)
from .record.serial_execution import OrdinaryHdlSerialExecutor
from .record_presentation import (
    _coverage_failed_target,
    _coverage_preflight_findings_outcome,
    _coverage_target_outcome,
    _CoverageTargetError,
    _simulation_record_recovery_detail,
)
from .record_scaffold import _group_disclosures, _SimulationRecordRun, _SimulationRecordSteps

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import ExitStack

    from booley.flows.base import SubprocessResult
    from booley.flows.job_estimates import JobRequest
    from booley.flows.sim.request import SimRequest
    from booley.flows.sim.verilator_coverage import AuthenticatedSimulationExecutionPort
    from booley.flows.target_test_plan import TargetTestPlan

    from .execution import SimulationExecution, SimulationOptions
    from .record import SimulationRecordPolicy


def _manifest_run_groups(
    manifest: SimulationRecordManifest,
) -> tuple[tuple[TestRun, ...], ...]:
    """The Test Runs of each work item, exactly as the manifest froze them."""
    return SimulationRecordManifestView(manifest).test_run_groups


def _fresh_simulation_record_recovery_detail(
    validated: ValidatedResumeManifest,
    observed: SimulationRecordRecoveryStatus,
) -> dict[str, object]:
    """Refresh failure presentation without treating the pre-run view as authority."""
    try:
        status = SimulationRecord().inspect_resume(validated)
    except (OSError, ValueError) as exc:
        detail = _simulation_record_recovery_detail(observed)
        detail["recovery_refresh_error"] = str(exc)
        return detail
    return _simulation_record_recovery_detail(status)


def _checkpoint_coverage_record(
    progress: CoverageProgress,
    outcome: SimulationRecordOutcome,
    evaluation: str,
    publish: Callable[[SimulationRecordOutcome], bool],
) -> bool:
    """Retain one completed Target once its projection is published; ``False`` keeps it pending."""
    try:
        target = _coverage_target_outcome(outcome)
    except (OSError, ValueError) as error:
        detail = _coverage_failed_target(
            str(outcome.target["selector"]), error, evaluation, outcome
        )
        raise _CoverageTargetError(str(error), detail) from error
    if not publish(outcome):
        return False
    progress.outcomes.append(target)
    progress.checkpoint()
    return True


class SimulationRecordResumeMixin:
    """Simulation Record resume paths for `SimulateFlow`."""

    if TYPE_CHECKING:
        # Host members SimulateFlow provides; Pyright checks them against the host.
        publication_resources: ExitStack[bool | None]
        _held_resume_origins: set[Path]
        _simulation_record_publication_checkpoint: Callable[[str], None] | None

        @property
        def args(self) -> SimRequest: ...
        def _target_test_plan(self, target: str) -> TargetTestPlan: ...
        def _record_simulation_record_tool(self, manifest: SimulationRecordManifest) -> None: ...
        def _simulation_record_policy(self) -> SimulationRecordPolicy: ...
        @staticmethod
        def _simulation_record_cancelled_outcome(
            error: SimulationRecordCancellationError,
            detail: Mapping[str, object] | None = None,
        ) -> EndpointOutcome: ...
        def _capture_cycle_report(self, outcomes: Sequence[SimulationRecordOutcome]) -> None: ...
        def _write_simulation_record_progress(
            self,
            targets: Sequence[str],
            outcomes: Sequence[SimulationRecordOutcome],
            *,
            phase: str,
            complete: bool,
        ) -> None: ...
        def _simulation_record_endpoint_outcome(
            self, outcomes: list[SimulationRecordOutcome]
        ) -> EndpointOutcome: ...
        def _publish_projection(self, outcome: SimulationRecordOutcome) -> bool: ...
        def work_item_job_request(
            self, selector: str, runs: Sequence[TestRun], flags: tuple[str, ...]
        ) -> JobRequest | None: ...
        def _record_pre_sim_firing(self, key: tuple[str, str, str, int]) -> None: ...
        def _execute_boundary(
            self, cmd: list[str], *, timeout: int | None = None, cwd: Path | None = None
        ) -> SubprocessResult: ...
        def _coverage_simulation_record_execution(
            self,
            plan: CoverageTargetPlan,
            options: SimulationOptions,
            pre_sim_commands: tuple[str, ...],
            pre_sim_build_access: str,
        ) -> AuthenticatedSimulationExecutionPort: ...
        def _simulation_execution(self) -> SimulationExecution: ...

    def _refuse_unfinished_coverage_resume(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        """Refuse resuming a coverage Simulation Record whose collection has no result.

        A resumed attempt builds privately and re-runs every test, so it costs
        the same as a new run.  Publication-only resumes (collection complete)
        are untouched, as are all non-coverage Simulation Records.
        """
        manifest = validated.manifest
        view = SimulationRecordManifestView(manifest)
        if not view.is_coverage:
            return None
        try:
            status = SimulationRecord().inspect_resume(validated)
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Record integrity failure: {exc}",
            )
        unfinished = view.unfinished_work_item_ids(status)
        if not unfinished:
            return None
        item_id = unfinished[0]
        state = "interrupted" if item_id in status.interrupted else "pending"
        selector = view.target.selector
        trace = " --trace" if view.trace else ""
        no_waivers = " --no-waivers" if view.no_waivers else ""
        tests = self._frozen_test_run_options(manifest, selector)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                f"sim: cannot resume coverage Simulation Record {status.manifest_path}: "
                f"its coverage collection has no recorded result (work item {item_id} is "
                f"{state}; the run was interrupted or is still running). Resuming would "
                "rebuild and re-run every test, so start a new run instead:\n"
                f"  booley flow sim --target {shlex.quote(selector)}{tests} "
                f"--coverage{trace}{no_waivers}\n"
                + (
                    "The command names the Test Runs this Simulation Record froze, so the new "
                    "run repeats their Test Variants and seeds."
                    if tests
                    else "Add the original --test/--tests-file selection if you used one."
                )
            ),
            detail=_simulation_record_recovery_detail(status),
        )

    def _frozen_test_run_options(self, manifest: SimulationRecordManifest, selector: str) -> str:
        """``--test`` options that repeat the record's Test Runs in a new run.

        Empty when every frozen Test Run is still its Test's default at the
        Default Seed: the plain selection then resolves to the same runs.
        Any other record (a Test Variant, a pinned or drawn seed, a default
        that has since changed) is named run by run, so a new run draws and
        resolves nothing again.
        """
        try:
            runs = [run for group in _manifest_run_groups(manifest) for run in group]
        except ValueError:
            return ""
        try:
            plan = self._target_test_plan(selector)
            unchanged = all(plan.is_default_run(run) for run in runs)
        except (fusesoc_registry.FuseSocError, ValueError):
            # Nothing shows the frozen runs are still defaults, so the command
            # names each of them (test_test_run_resolution_errors).
            unchanged = False
        if unchanged or any(run.test is None for run in runs):
            return ""
        return "".join(f" {shlex.quote(f'--test={run.name}')}" for run in runs)

    def _resume_simulation_record(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | _SimulationRecordRun:
        """Validate one exact durable record resume: a refusal, a preview or the run."""
        try:
            observed = SimulationRecord().inspect_resume(validated)
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Record integrity failure: {exc}",
            )
        if self.args.dry_run:
            return self._preview_simulation_record_resume(validated, observed)
        self._record_simulation_record_tool(validated.manifest)
        return self._resume_simulation_record_run(validated, observed)

    def _preview_simulation_record_resume(
        self, validated: ValidatedResumeManifest, observed: SimulationRecordRecoveryStatus
    ) -> EndpointOutcome:
        try:
            current_plan = self._resume_simulation_record_plan(
                validated, SimulationRecordManifestView(validated.manifest).origin_invocation_id
            )
            request = ResumeSimulationRecordPreviewRequest(
                validated,
                current_plan,
                Path(self.args.work_dir),
                self.args.report_dir,
                self._simulation_record_policy(),
            )
            preview = SimulationRecord().preview(request)
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Record resume preview failed: {exc}",
                detail=_simulation_record_recovery_detail(observed),
            )
        assert isinstance(preview, ResumeSimulationRecordPreview)
        status = _simulation_record_recovery_detail(preview.recovery)
        status.update(preview.diagnostic.detail)
        status["required_bundle_variants"] = list(preview.required_bundle_variants)
        if not preview.mismatches:
            # The real run reports a mismatch before its run_cwd check.
            refusal = _resume_run_cwd_refusal(request, status)
            if refusal is not None:
                return refusal
        lines = [
            f"manifest: {validated.path}",
            f"completed: {len(preview.recovery.completed)}",
            f"interrupted: {len(preview.recovery.interrupted)}",
            f"pending: {len(preview.recovery.pending)}",
            f"mismatches: {len(preview.mismatches)}",
        ]
        if preview.mismatches:
            lines.append(preview.diagnostic.report(verbose=self.args.verbose))
        return EndpointOutcome(
            exit_code=EXIT_ERROR if preview.mismatches else EXIT_SUCCESS,
            detail=status,
            report_text="\n".join(lines),
        )

    def _resume_simulation_record_run(
        self, validated: ValidatedResumeManifest, observed: SimulationRecordRecoveryStatus
    ) -> _SimulationRecordRun:
        """Describe the exact resume to the shared execution scaffold."""
        assert self.args.report_dir is not None, "prepared Flow requires a report root"

        def start(
            admission: AdmissionContext,
            invocation: Path,
            manifest_published: Callable[[Path], None],
        ) -> _SimulationRecordSteps | EndpointOutcome:
            lock_error = self._lock_simulation_record_resume_origin(validated, observed)
            if lock_error is not None:
                return lock_error
            coverage_plan = (
                self._resume_coverage_target_plan(validated)
                if SimulationRecordManifestView(validated.manifest).is_coverage
                else None
            )
            if coverage_plan is None:
                return self._resume_ordinary_steps(
                    validated, invocation, admission, manifest_published
                )
            return self._resume_coverage_steps(
                validated, invocation, admission, coverage_plan, manifest_published
            )

        return _SimulationRecordRun(
            refusal="sim: no admission context",
            refusal_detail=_simulation_record_recovery_detail(observed),
            progress_operation="publish Simulation progress",
            start=start,
            failure=lambda error, invocation: self._simulation_record_resume_failure(
                validated, observed, invocation, error
            ),
        )

    def _lock_simulation_record_resume_origin(
        self,
        validated: ValidatedResumeManifest,
        observed: SimulationRecordRecoveryStatus,
    ) -> EndpointOutcome | None:
        from .record_reports import simulation_record_invocation_lock

        # The destination lock is already held; also retain the resumed origin. A Job's
        # requeued attempt resumes the same origin, whose lock this process already holds
        # until the Job ends: locking it again would refuse our own lock.
        origin = validated.path.parents[3]
        if origin not in self._held_resume_origins:
            try:
                self.publication_resources.enter_context(
                    simulation_record_invocation_lock(origin, booley_dir=validated.booley_dir)
                )
            except (OSError, ValueError) as exc:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=f"Simulation Record resume origin is busy: {exc}",
                    detail=_simulation_record_recovery_detail(observed),
                )
            self._held_resume_origins.add(origin)
        return None

    def _simulation_record_resume_failure(
        self,
        validated: ValidatedResumeManifest,
        observed: SimulationRecordRecoveryStatus,
        invocation: Path,
        error: Exception,
    ) -> EndpointOutcome:
        detail = _fresh_simulation_record_recovery_detail(validated, observed)
        if isinstance(error, SimulationRecordCancellationError):
            return self._simulation_record_cancelled_outcome(error, detail)
        message = str(error)
        if isinstance(error, SimulationRecordWorkloadMismatchError):
            detail.update(error.diagnostic.detail)
            message = error.diagnostic.report(verbose=self.args.verbose)
        if isinstance(error, (_CoverageAggregateError, _CoverageTargetError)):
            selector = str(error.detail["target"])
            detail["targets"] = {
                selector: _coverage_failed_target(
                    selector, error, str(error.detail["evaluation"]), report_invocation=invocation
                )
            }
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Simulation Record resume failed: {message}",
            detail=detail,
        )

    def _resume_coverage_steps(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        coverage_plan: CoverageTargetPlan,
        manifest_published: Callable[[Path], None],
    ) -> _SimulationRecordSteps:
        progress = CoverageProgress(invocation, (coverage_plan.handle.selector,))

        def run() -> EndpointOutcome:
            progress.checkpoint()
            outcome = self._run_validated_resume_simulation_record(
                validated, invocation, admission, coverage_plan, manifest_published
            )
            self._capture_cycle_report((outcome,))
            published = _checkpoint_coverage_record(
                progress,
                outcome,
                "not_requested" if coverage_plan.criterion is None else "blocked",
                self._publish_projection,
            )
            supersede_progress(
                validated.path.parents[3] / "progress.json",
                new_invocation=int(invocation.name),
                new_run_id=os.environ.get("BOOLEY_RUN_ID", ""),
                booley_dir=validated.booley_dir,
            )
            return self._resumed_endpoint_outcome(outcome, published=published)

        return _SimulationRecordSteps(
            lambda phase: progress.checkpoint(complete=True, phase=phase), run
        )

    def _resume_ordinary_steps(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        manifest_published: Callable[[Path], None],
    ) -> _SimulationRecordSteps:
        target = SimulationRecordManifestView(validated.manifest).target
        selector = target.selector
        outcomes: list[SimulationRecordOutcome] = []

        def run() -> EndpointOutcome:
            self._write_simulation_record_progress(
                (selector,), outcomes, phase="starting", complete=False
            )
            with attempt_scope(
                selector,
                identity=target.identity,
                operation="publication",
            ):
                outcome = self._run_validated_resume_simulation_record(
                    validated, invocation, admission, None, manifest_published
                )
            published = self._publish_projection(outcome)
            if published:
                outcomes.append(outcome)
                self._write_simulation_record_progress(
                    (selector,), outcomes, phase="running", complete=False
                )
            return self._resumed_endpoint_outcome(outcome, published=published)

        return _SimulationRecordSteps(
            lambda phase: self._write_simulation_record_progress(
                (selector,), outcomes, phase=phase, complete=True
            ),
            run,
        )

    def _resumed_endpoint_outcome(
        self, outcome: SimulationRecordOutcome, *, published: bool
    ) -> EndpointOutcome:
        """Report the resumed Target; one whose projection failed has no card entry."""
        result = self._simulation_record_endpoint_outcome([outcome] if published else [])
        result.detail.update(_simulation_record_recovery_detail(outcome.recovery))
        return result

    def _run_validated_resume_simulation_record(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        coverage_plan: CoverageTargetPlan | None,
        manifest_published: Callable[[Path], None],
    ) -> SimulationRecordOutcome:
        plan = self._resume_simulation_record_plan(
            validated, int(invocation.name), coverage_plan=coverage_plan
        )
        verify_workload(validated.manifest, plan.manifest)
        manifest_published(validated.path)
        executor = self._resume_simulation_record_executor(coverage_plan)
        try:
            return SimulationRecord(
                executor,
                publication_checkpoint=self._simulation_record_publication_checkpoint,
                work_item_request=self.work_item_job_request,
            ).run(
                ResumeSimulationRecordRunRequest(
                    validated,
                    plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._simulation_record_policy(),
                    invocation,
                    admission,
                    pre_sim_firing_published=self._record_pre_sim_firing,
                )
            )
        except SimulationRecordPublicationError as error:
            if coverage_plan is None:
                raise
            detail = _coverage_failed_target(
                coverage_plan.handle.selector,
                error,
                "not_requested" if coverage_plan.criterion is None else "blocked",
                report_invocation=invocation,
            )
            raise _CoverageTargetError(str(error), detail) from error

    def _resume_simulation_record_executor(self, coverage_plan: CoverageTargetPlan | None):
        if coverage_plan is None:
            return OrdinaryHdlSerialExecutor(invoke=self._execute_boundary)
        return CoverageAggregateExecutor(
            plans={coverage_plan.handle.identity: coverage_plan},
            execution_factory=self._coverage_simulation_record_execution,
            publication_checkpoint=self._simulation_record_publication_checkpoint,
        )

    def _resume_simulation_record_plan(
        self,
        validated: ValidatedResumeManifest,
        invocation_id: int,
        *,
        coverage_plan: CoverageTargetPlan | None = None,
    ):
        """Recompute current workload identity for exact resume comparison."""
        manifest = validated.manifest
        binding = validated.binding_for(manifest)
        handle = binding.handle if binding is not None else validated.target_handles[0]
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        view = SimulationRecordManifestView(manifest)
        groups = view.selected_name_groups
        names = tuple(name for group in groups for name in group)
        # Resume runs the manifest's own Test Runs: nothing is resolved from
        # tests.toml again and no seed is drawn again.
        run_groups = view.test_run_groups
        runs = tuple(run for group in run_groups for run in group)
        execution = self._simulation_execution()
        preview = execution.preview(handle, SelectedTestRuns(runs))
        is_coverage = view.is_coverage
        if is_coverage:
            preview = replace(preview, groups=(names,), run_groups=(runs,))
        disclosures = (
            () if is_coverage else _group_disclosures(execution, handle, groups, run_groups)
        )
        revision = git_full_sha("HEAD", handle.project_root)
        if revision is None:
            raise SimulationRecordIntegrityError(
                f"cannot verify resume Target revision at {handle.project_root}"
            )
        kind = view.work_item_kind
        if kind is not None and (view.is_coverage or kind == "cocotb_batch"):
            if view.is_coverage and coverage_plan is None:
                self._resume_coverage_target_plan(validated)
            return plan_coarse_simulation_record(
                handle=handle,
                inspection=inspection,
                preview=preview,
                selected_tests=names,
                required_suite=view.required_suite_names,
                revision=revision,
                invocation_id=invocation_id,
                execution_id="",
                trace=view.trace,
                kind=kind,
                planning_disclosures=disclosures if kind == "cocotb_batch" else (),
                required_suite_catalog_backed=not view.required_suite_default_invocation,
                no_waivers=view.no_waivers,
            )
        return self._ordinary_resume_plan(
            handle, inspection, preview, view, revision, invocation_id, disclosures
        )

    def _ordinary_resume_plan(
        self,
        handle,
        inspection,
        preview,
        view: SimulationRecordManifestView,
        revision,
        invocation_id,
        disclosures,
    ) -> SimulationRecordPlan:
        return plan_ordinary_hdl_simulation_record(
            handle=handle,
            inspection=inspection,
            preview=preview,
            groups=view.selected_name_groups,
            required_suite=view.required_suite_names,
            revision=revision,
            invocation_id=invocation_id,
            execution_id="",
            trace=view.trace,
            prerequisite_documents=view.prerequisites,
            planning_disclosures=disclosures,
            role=view.target.role,
            required_suite_catalog_backed=not view.required_suite_default_invocation,
        )

    def _resume_coverage_prepared(self, validated: ValidatedResumeManifest) -> Any:
        """Run the shared coverage preflight for the manifest's frozen coverage choice."""
        from booley.flows.sim.coverage_flow_context import coverage_project_context
        from booley.flows.sim.coverage_invocation import (
            CoverageInvocationRequest,
            prepare_coverage_invocation,
        )

        view = SimulationRecordManifestView(validated.manifest)
        selector = view.target.selector
        names = view.selected_name_groups[0]
        context = coverage_project_context(self.args.work_dir)
        runs = view.test_run_groups[0]
        prepared = prepare_coverage_invocation(
            CoverageInvocationRequest(
                (selector,),
                tests=names,
                # The manifest froze its Test Runs: resume never resolves again.
                test_runs={selector: runs},
                trace=view.trace,
                no_waivers=view.no_waivers,
            ),
            context,
        )
        return prepared

    def _resume_coverage_preflight_if_coverage(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        if SimulationRecordManifestView(validated.manifest).is_coverage:
            return self._resume_coverage_preflight(validated)
        return None

    def _resume_coverage_preflight(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        """Refuse an unresumable raw-coverage aggregate before admission or reservation.

        Manifests without ``no_waivers`` (all legacy ones) keep their original
        lifecycle: the plan is rebuilt during resume, not at preflight.
        """
        if not SimulationRecordManifestView(validated.manifest).no_waivers:
            return None
        prepared = self._resume_coverage_prepared(validated)
        if prepared.plan is None:
            return _coverage_preflight_findings_outcome(prepared.findings)
        if len(prepared.plan.targets) == 1:
            self._resume_coverage_plan = prepared.plan.targets[0]
        return None

    def _resume_coverage_target_plan(
        self, validated: ValidatedResumeManifest
    ) -> CoverageTargetPlan:
        """Reconstruct the exact native coverage plan bound by the manifest."""
        if self._resume_coverage_plan is not None:
            return self._resume_coverage_plan
        prepared = self._resume_coverage_prepared(validated)
        if prepared.plan is None or len(prepared.plan.targets) != 1:
            detail = "; ".join(item.message for item in prepared.findings)
            raise SimulationRecordIntegrityError(
                f"cannot reconstruct coverage aggregate: {detail}"
            )
        return prepared.plan.targets[0]


def _resume_run_cwd_refusal(
    request: ResumeSimulationRecordPreviewRequest, status: dict[str, object]
) -> EndpointOutcome | None:
    """Return the real run's failure when a record with work left has an unusable run_cwd."""
    try:
        SimulationRecord().preflight(request)
    except (OSError, ValueError) as exc:
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Simulation Record resume failed: {exc}",
            detail=status,
        )
    return None
