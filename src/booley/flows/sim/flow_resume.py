"""Simulation Record resume (`--resume-from`) for the Simulation Flow.

The validated manifest, never ``progress.json``, decides what resume recovers.
A coverage Simulation Record never resumes.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.endpoint_session import default_report_root
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
from .record.flow_planning import (
    plan_coarse_simulation_record,
    plan_ordinary_hdl_simulation_record,
)
from .record.planning import (
    SimulationRecordWorkloadMismatchError,
    verify_workload,
)
from .record.serial_execution import OrdinaryHdlSerialExecutor
from .record_presentation import _simulation_record_recovery_detail
from .record_scaffold import _group_disclosures, _SimulationRecordRun, _SimulationRecordSteps

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import ExitStack

    from booley.flows.base import SubprocessResult
    from booley.flows.job_estimates import JobRequest
    from booley.flows.sim.request import SimRequest
    from booley.flows.target_test_plan import TargetTestPlan

    from .execution import SimulationExecution
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


class SimulationRecordResumeMixin:
    """Simulation Record resume paths for `SimulateFlow`."""

    if TYPE_CHECKING:
        # Host members SimulateFlow provides; Pyright checks them against the host.
        publication_resources: ExitStack[bool | None]
        _held_resume_origins: set[Path]
        _simulation_record_publication_checkpoint: Callable[[str], None] | None
        endpoint_kind: str

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
        def _simulation_execution(self) -> SimulationExecution: ...

    def _refuse_coverage_resume(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        """Refuse every coverage Simulation Record, naming the new run that repeats it."""
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
        if unfinished:
            item_id = unfinished[0]
            state = "interrupted" if item_id in status.interrupted else "pending"
            reason = (
                f"its coverage collection has no recorded result (work item {item_id} is "
                f"{state}; the run was interrupted or is still running). Resuming would "
                "rebuild and re-run every test, so start a new run instead:"
            )
        else:
            reason = (
                "its coverage collection finished, but a coverage run never resumes, "
                "so start a new run instead:"
            )
        command, note = self._new_coverage_run_command(validated, view)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                f"sim: cannot resume coverage Simulation Record {status.manifest_path}: "
                f"{reason}\n  {command}\n{note}"
            ),
            detail=_simulation_record_recovery_detail(status),
        )

    def _new_coverage_run_command(
        self, validated: ValidatedResumeManifest, view: SimulationRecordManifestView
    ) -> tuple[str, str]:
        """The command for a new run that repeats the record, and the line after it."""
        selector = view.target.selector
        trace = " --trace" if view.trace else ""
        no_waivers = " --no-waivers" if view.no_waivers else ""
        tests = self._frozen_test_run_options(validated.manifest, selector)
        command = (
            f"booley flow sim --target {shlex.quote(selector)}{tests or ''} --coverage"
            f"{trace}{no_waivers}{self._recorded_report_dir_option(validated)}"
        )
        if tests is None:
            note = "Add the original --test/--tests-file selection if you used one."
        elif tests:
            note = (
                "The command names the Test Runs this Simulation Record froze, so the new "
                "run repeats their Test Variants and seeds."
            )
        else:
            note = (
                "This Simulation Record froze the Target's whole default suite, so the "
                "command names no Test Run."
            )
        return command, note

    def _recorded_report_dir_option(self, validated: ValidatedResumeManifest) -> str:
        """`` --report-dir <root>`` unless a run without it would use the record's root."""
        # <root>/sim/<N>/targets/<target>/record/manifest.json
        root = validated.path.parents[3].parent.parent
        try:
            if (
                root.resolve()
                == default_report_root(self.endpoint_kind, Path(self.args.work_dir)).resolve()
            ):
                return ""
        except (OSError, RuntimeError, ValueError):
            pass  # the default is unknown, so the command names the root
        return f" --report-dir {shlex.quote(str(root))}"

    def _frozen_test_run_options(
        self, manifest: SimulationRecordManifest, selector: str
    ) -> str | None:
        """``--test`` options that repeat the record's Test Runs in a new run.

        None when the manifest's Test Runs cannot be read.  Empty when the
        frozen Test Runs are the Target's whole default suite: the plain
        selection then resolves to the same runs.  Any other record (a subset,
        a Test Variant, a pinned or drawn seed, a default that has since
        changed) is named run by run, so a new run draws and resolves nothing
        again.
        """
        try:
            runs = [run for group in _manifest_run_groups(manifest) for run in group]
        except ValueError:
            return None
        try:
            defaults = self._target_test_plan(selector).default_runs()
            whole_suite = sorted(runs, key=lambda run: run.name) == sorted(
                defaults, key=lambda run: run.name
            )
        except (fusesoc_registry.FuseSocError, ValueError):
            # Nothing shows the frozen runs are the default suite, so the command
            # names each of them (test_test_run_resolution_errors).
            whole_suite = False
        if whole_suite:
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
            return self._resume_ordinary_steps(
                validated, invocation, admission, manifest_published
            )

        return _SimulationRecordRun(
            refusal="sim: no admission context",
            refusal_detail=_simulation_record_recovery_detail(observed),
            progress_operation="publish Simulation progress",
            start=start,
            failure=lambda error, _invocation: self._simulation_record_resume_failure(
                validated, observed, error
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
                    refused=True,
                )
            self._held_resume_origins.add(origin)
        return None

    def _simulation_record_resume_failure(
        self,
        validated: ValidatedResumeManifest,
        observed: SimulationRecordRecoveryStatus,
        error: Exception,
    ) -> EndpointOutcome:
        detail = _fresh_simulation_record_recovery_detail(validated, observed)
        if isinstance(error, SimulationRecordCancellationError):
            return self._simulation_record_cancelled_outcome(error, detail)
        message = str(error)
        if isinstance(error, SimulationRecordWorkloadMismatchError):
            detail.update(error.diagnostic.detail)
            message = error.diagnostic.report(verbose=self.args.verbose)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Simulation Record resume failed: {message}",
            detail=detail,
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
                    validated, invocation, admission, manifest_published
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
        manifest_published: Callable[[Path], None],
    ) -> SimulationRecordOutcome:
        plan = self._resume_simulation_record_plan(validated, int(invocation.name))
        verify_workload(validated.manifest, plan.manifest)
        manifest_published(validated.path)
        return SimulationRecord(
            OrdinaryHdlSerialExecutor(invoke=self._execute_boundary),
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

    def _resume_simulation_record_plan(
        self, validated: ValidatedResumeManifest, invocation_id: int
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
        disclosures = _group_disclosures(execution, handle, groups, run_groups)
        revision = git_full_sha("HEAD", handle.project_root)
        if revision is None:
            raise SimulationRecordIntegrityError(
                f"cannot verify resume Target revision at {handle.project_root}"
            )
        kind = view.work_item_kind
        if kind == "cocotb_batch":
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
                planning_disclosures=disclosures,
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
