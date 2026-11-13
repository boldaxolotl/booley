"""Simulation Campaign resume (`--resume-from`) for the Simulation Flow."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.progress_lifecycle import (
    supersede_progress,
)
from booley.flows.sim.coverage_invocation import CoverageTargetPlan
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.flows.sim.live_progress import (
    attempt_scope,
)
from booley.flows.test_runs import (
    TestRun,
    test_run_from_document,
)
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
from .build import (
    SimulationBuildInfrastructureError,
)
from .execution import (
    SelectedTestRuns,
)
from .record import (
    CampaignOutcome,
    CampaignRecoveryStatus,
    ResumeCampaignPreview,
    ResumeCampaignPreviewRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
    SimulationCampaignCancellationError,
    SimulationCampaignIntegrityError,
    SimulationCampaignManifest,
    SimulationCampaignPlan,
    ValidatedResumeManifest,
)
from .record.coordinator import CampaignPublicationError
from .record.coverage_execution import (
    CoverageAggregateExecutor,
    _CoverageAggregateError,
)
from .record.flow_planning import (
    plan_coarse_simulation_campaign,
    plan_ordinary_hdl_campaign,
)
from .record.planning import (
    SimulationCampaignWorkloadMismatchError,
    verify_workload,
)
from .record.serial_execution import OrdinaryHdlSerialExecutor
from .record_presentation import (
    _build_infrastructure_detail,
    _campaign_recovery_detail,
    _coverage_failed_target,
    _coverage_preflight_findings_outcome,
    _CoverageTargetError,
    _retain_coverage_campaign,
)
from .record_scaffold import _CampaignRun, _CampaignSteps, _group_disclosures


def _manifest_no_waivers(manifest: SimulationCampaignManifest) -> bool:
    """Whether the frozen workload asked for raw coverage; absent (legacy) means waivers apply."""
    workload = cast(Mapping[str, object], manifest.document["workload"])
    return workload.get("no_waivers") is True


def _manifest_trace(manifest: SimulationCampaignManifest) -> bool:
    """Whether the campaign *manifest* froze a traced workload."""
    return cast(bool, cast(Mapping[str, object], manifest.document["workload"])["trace"])


def _manifest_run_groups(
    manifest: SimulationCampaignManifest,
) -> tuple[tuple[TestRun, ...], ...]:
    """The Test Runs of each work item, exactly as the manifest froze them."""
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    return tuple(
        tuple(
            test_run_from_document(entry) for entry in cast(tuple[object, ...], item["test_runs"])
        )
        for item in items
    )


def _is_coverage_manifest(manifest: SimulationCampaignManifest) -> bool:
    """True when resume dispatches the coverage aggregate executor."""
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    return bool(items) and items[0]["kind"] == "coverage_aggregate"


def _unfinished_coverage_work(
    manifest: SimulationCampaignManifest, status: CampaignRecoveryStatus
) -> tuple[str, ...]:
    """Coverage work-item ids without a recorded result; empty for other Campaigns."""
    if not _is_coverage_manifest(manifest):
        return ()
    unfinished = set(status.pending) | set(status.interrupted)
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    return tuple(
        cast(str, item["work_item_id"]) for item in items if item["work_item_id"] in unfinished
    )


def _fresh_campaign_recovery_detail(
    validated: ValidatedResumeManifest,
    observed: CampaignRecoveryStatus,
) -> dict[str, object]:
    """Refresh failure presentation without treating the pre-run view as authority."""
    try:
        status = SimulationCampaign().inspect_resume(validated)
    except (OSError, ValueError) as exc:
        detail = _campaign_recovery_detail(observed)
        detail["recovery_refresh_error"] = str(exc)
        return detail
    return _campaign_recovery_detail(status)


def _checkpoint_coverage_campaign(
    progress: CoverageProgress, outcome: CampaignOutcome, evaluation: str
) -> None:
    try:
        _retain_coverage_campaign(progress, outcome)
    except (OSError, ValueError) as error:
        detail = _coverage_failed_target(
            str(outcome.target["selector"]), error, evaluation, outcome
        )
        raise _CoverageTargetError(str(error), detail) from error
    progress.checkpoint()


class CampaignResumeMixin:
    """Simulation Campaign resume paths for `SimulateFlow`."""

    def _refuse_unfinished_coverage_resume(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        """Refuse resuming a coverage Campaign whose collection has no result.

        A resumed attempt builds privately and re-runs every test, so it costs
        the same as a new run.  Publication-only resumes (collection complete)
        are untouched, as are all non-coverage Campaigns.
        """
        manifest = validated.manifest
        if not _is_coverage_manifest(manifest):
            return None
        try:
            status = SimulationCampaign().inspect_resume(validated)
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign integrity failure: {exc}",
            )
        unfinished = _unfinished_coverage_work(manifest, status)
        if not unfinished:
            return None
        item_id = unfinished[0]
        state = "interrupted" if item_id in status.interrupted else "pending"
        selector = cast(Mapping[str, str], manifest.document["target"])["selector"]
        workload = cast(Mapping[str, object], manifest.document["workload"])
        trace = " --trace" if workload["trace"] is True else ""
        # A raw-coverage run of a gated Target is refused without --diagnostic.
        no_waivers = " --no-waivers --diagnostic" if _manifest_no_waivers(manifest) else ""
        tests = self._frozen_test_run_options(manifest, selector)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                f"sim: cannot resume coverage Simulation Campaign {status.manifest_path}: "
                f"its coverage collection has no recorded result (work item {item_id} is "
                f"{state}; the run was interrupted or is still running). Resuming would "
                "rebuild and re-run every test, so start a new run instead:\n"
                f"  booley flow sim --target {shlex.quote(selector)}{tests} "
                f"--coverage{trace}{no_waivers}\n"
                + (
                    "The command names the Test Runs this campaign froze, so the new "
                    "run repeats their Test Variants and seeds."
                    if tests
                    else "Add the original --test/--tests-file selection if you used one."
                )
            ),
            detail=_campaign_recovery_detail(status),
        )

    def _frozen_test_run_options(self, manifest: SimulationCampaignManifest, selector: str) -> str:
        """``--test`` options that repeat the campaign's Test Runs in a new run.

        Empty when every frozen Test Run is still its Test's default at the
        Default Seed: the plain selection then resolves to the same runs.
        Any other campaign (a Test Variant, a pinned or drawn seed, a default
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

    def _resume_campaign(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | _CampaignRun:
        """Validate one exact durable campaign resume: a refusal, a preview or the run."""
        try:
            observed = SimulationCampaign().inspect_resume(validated)
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign integrity failure: {exc}",
            )
        if self.args.dry_run:
            return self._preview_campaign_resume(validated, observed)
        self._record_campaign_tool(validated.manifest)
        return self._resume_campaign_run(validated, observed)

    def _preview_campaign_resume(
        self, validated: ValidatedResumeManifest, observed: CampaignRecoveryStatus
    ) -> EndpointOutcome:
        origin = cast(Mapping[str, object], validated.manifest.document["origin"])
        try:
            current_plan = self._resume_campaign_plan(
                validated, cast(int, origin["invocation_id"])
            )
            preview = SimulationCampaign().preview(
                ResumeCampaignPreviewRequest(
                    validated,
                    current_plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._campaign_policy(),
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign resume preview failed: {exc}",
                detail=_campaign_recovery_detail(observed),
            )
        assert isinstance(preview, ResumeCampaignPreview)
        status = _campaign_recovery_detail(preview.recovery)
        status.update(preview.diagnostic.detail)
        status["required_bundle_variants"] = list(preview.required_bundle_variants)
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

    def _resume_campaign_run(
        self, validated: ValidatedResumeManifest, observed: CampaignRecoveryStatus
    ) -> _CampaignRun:
        """Describe the exact resume to the shared execution scaffold."""
        assert self.args.report_dir is not None, "prepared Flow requires a report root"

        def start(
            admission: AdmissionContext,
            invocation: Path,
            manifest_published: Callable[[Path], None],
        ) -> _CampaignSteps | EndpointOutcome:
            dependency_error = self._prepare_campaign_resume_dependency(
                validated, invocation, observed
            )
            if dependency_error is not None:
                return dependency_error
            coverage_plan = (
                self._resume_coverage_target_plan(validated)
                if _is_coverage_manifest(validated.manifest)
                else None
            )
            if coverage_plan is None:
                return self._resume_ordinary_steps(
                    validated, invocation, admission, manifest_published
                )
            return self._resume_coverage_steps(
                validated, invocation, admission, coverage_plan, manifest_published
            )

        return _CampaignRun(
            refusal="sim: no admission context",
            refusal_detail=_campaign_recovery_detail(observed),
            progress_operation="publish Simulation progress",
            start=start,
            failure=lambda error, invocation: self._campaign_resume_failure(
                validated, observed, invocation, error
            ),
        )

    def _prepare_campaign_resume_dependency(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        observed: CampaignRecoveryStatus,
    ) -> EndpointOutcome | None:
        from .record_reports import campaign_invocation_lock

        # The destination lock is already held; also retain the resumed origin. A Job's
        # requeued attempt resumes the same origin, whose lock this process already holds
        # until the Job ends: locking it again would refuse our own lock.
        origin = validated.path.parents[3]
        if origin not in self._held_resume_origins:
            try:
                self.publication_resources.enter_context(
                    campaign_invocation_lock(origin, booley_dir=validated.booley_dir)
                )
            except (OSError, ValueError) as exc:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=f"Simulation Campaign resume origin is busy: {exc}",
                    detail=_campaign_recovery_detail(observed),
                )
            self._held_resume_origins.add(origin)
        try:
            from .record.dependency import register_campaign_dependency

            register_campaign_dependency(
                validated.path,
                campaign_id=cast(str, validated.manifest.document["simulation_record_id"]),
                manifest_sha256=validated.sha256,
                dependent_invocation=invocation,
            )
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign resume dependency failed: {exc}",
                detail=_campaign_recovery_detail(observed),
            )
        return None

    def _campaign_resume_failure(
        self,
        validated: ValidatedResumeManifest,
        observed: CampaignRecoveryStatus,
        invocation: Path,
        error: Exception,
    ) -> EndpointOutcome:
        detail = _fresh_campaign_recovery_detail(validated, observed)
        if isinstance(error, SimulationCampaignCancellationError):
            return self._campaign_cancelled_outcome(error, detail)
        message = str(error)
        if isinstance(error, SimulationCampaignWorkloadMismatchError):
            detail.update(error.diagnostic.detail)
            message = error.diagnostic.report(verbose=self.args.verbose)
        if isinstance(error, SimulationBuildInfrastructureError):
            detail.update(_build_infrastructure_detail(error))
        if isinstance(error, (_CoverageAggregateError, _CoverageTargetError)):
            selector = str(error.detail["target"])
            detail["targets"] = {
                selector: _coverage_failed_target(
                    selector, error, str(error.detail["evaluation"]), report_invocation=invocation
                )
            }
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Simulation Campaign resume failed: {message}",
            detail=detail,
        )

    def _resume_coverage_steps(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        coverage_plan: CoverageTargetPlan,
        manifest_published: Callable[[Path], None],
    ) -> _CampaignSteps:
        progress = CoverageProgress(invocation, (coverage_plan.handle.selector,))

        def run() -> EndpointOutcome:
            progress.checkpoint()
            outcome = self._run_validated_resume_campaign(
                validated, invocation, admission, coverage_plan, manifest_published
            )
            self._capture_cycle_report((outcome,))
            _checkpoint_coverage_campaign(
                progress,
                outcome,
                "not_requested" if coverage_plan.criterion is None else "blocked",
            )
            supersede_progress(
                validated.path.parents[3] / "progress.json",
                new_invocation=int(invocation.name),
                new_run_id=os.environ.get("BOOLEY_RUN_ID", ""),
                booley_dir=validated.booley_dir,
            )
            return self._resumed_endpoint_outcome(outcome)

        return _CampaignSteps(lambda phase: progress.checkpoint(complete=True, phase=phase), run)

    def _resume_ordinary_steps(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        manifest_published: Callable[[Path], None],
    ) -> _CampaignSteps:
        target = cast(Mapping[str, object], validated.manifest.document["target"])
        selector = str(target["selector"])
        outcomes: list[CampaignOutcome] = []

        def run() -> EndpointOutcome:
            self._write_campaign_progress((selector,), outcomes, phase="starting", complete=False)
            with attempt_scope(
                selector,
                identity=f"{target['vlnv']}#{target['name']}",
                operation="publication",
            ):
                outcome = self._run_validated_resume_campaign(
                    validated, invocation, admission, None, manifest_published
                )
            outcomes.append(outcome)
            self._write_campaign_progress((selector,), outcomes, phase="running", complete=False)
            return self._resumed_endpoint_outcome(outcome)

        return _CampaignSteps(
            lambda phase: self._write_campaign_progress(
                (selector,), outcomes, phase=phase, complete=True
            ),
            run,
        )

    def _resumed_endpoint_outcome(self, outcome: CampaignOutcome) -> EndpointOutcome:
        result = self._campaign_endpoint_outcome([outcome])
        result.detail.update(_campaign_recovery_detail(outcome.recovery))
        return result

    def _run_validated_resume_campaign(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        coverage_plan: CoverageTargetPlan | None,
        manifest_published: Callable[[Path], None],
    ) -> CampaignOutcome:
        plan = self._resume_campaign_plan(
            validated, int(invocation.name), coverage_plan=coverage_plan
        )
        verify_workload(validated.manifest, plan.manifest)
        manifest_published(validated.path)
        executor = self._resume_campaign_executor(coverage_plan)
        try:
            return SimulationCampaign(
                executor,
                publication_checkpoint=self._campaign_publication_checkpoint,
                work_item_request=self.work_item_job_request,
            ).run(
                ResumeCampaignRunRequest(
                    validated,
                    plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._campaign_policy(),
                    invocation,
                    admission,
                    pre_sim_firing_published=self._record_pre_sim_firing,
                )
            )
        except CampaignPublicationError as error:
            if coverage_plan is None:
                raise
            detail = _coverage_failed_target(
                coverage_plan.handle.selector,
                error,
                "not_requested" if coverage_plan.criterion is None else "blocked",
                report_invocation=invocation,
            )
            raise _CoverageTargetError(str(error), detail) from error

    def _resume_campaign_executor(self, coverage_plan: CoverageTargetPlan | None):
        if coverage_plan is None:
            return OrdinaryHdlSerialExecutor(invoke=self._execute_boundary)
        return CoverageAggregateExecutor(
            plans={coverage_plan.handle.identity: coverage_plan},
            execution_factory=self._coverage_campaign_execution,
            publication_checkpoint=self._campaign_publication_checkpoint,
        )

    def _resume_campaign_plan(
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
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        groups = tuple(
            cast(tuple[str, ...], cast(Mapping[str, object], item["selection"])["names"])
            for item in items
        )
        names = tuple(name for group in groups for name in group)
        # Resume runs the manifest's own Test Runs: nothing is resolved from
        # tests.toml again and no seed is drawn again.
        run_groups = _manifest_run_groups(manifest)
        runs = tuple(run for group in run_groups for run in group)
        execution = self._simulation_execution()
        preview = execution.preview(handle, SelectedTestRuns(runs))
        is_coverage = _is_coverage_manifest(manifest)
        if is_coverage:
            preview = replace(preview, groups=(names,), run_groups=(runs,))
        disclosures = (
            () if is_coverage else _group_disclosures(execution, handle, groups, run_groups)
        )
        suite = cast(Mapping[str, object], manifest.document["required_suite"])
        target = cast(Mapping[str, str], manifest.document["target"])
        revision = git_full_sha("HEAD", handle.project_root)
        if revision is None:
            raise SimulationCampaignIntegrityError(
                f"cannot verify resume Target revision at {handle.project_root}"
            )
        if items and items[0]["kind"] in {"coverage_aggregate", "cocotb_batch"}:
            kind = cast(str, items[0]["kind"])
            if kind == "coverage_aggregate" and coverage_plan is None:
                self._resume_coverage_target_plan(validated)
            return plan_coarse_simulation_campaign(
                handle=handle,
                inspection=inspection,
                preview=preview,
                selected_tests=names,
                required_suite=cast(tuple[str, ...], suite["names"]),
                revision=revision,
                invocation_id=invocation_id,
                execution_id="",
                trace=_manifest_trace(manifest),
                kind=kind,
                planning_disclosures=disclosures if kind == "cocotb_batch" else (),
                required_suite_catalog_backed=not cast(bool, suite["default_invocation"]),
                no_waivers=_manifest_no_waivers(manifest),
            )
        return self._ordinary_resume_plan(
            handle,
            inspection,
            preview,
            groups,
            suite,
            target,
            revision,
            invocation_id,
            manifest,
            disclosures,
        )

    def _ordinary_resume_plan(
        self,
        handle,
        inspection,
        preview,
        groups,
        suite,
        target,
        revision,
        invocation_id,
        manifest,
        disclosures,
    ) -> SimulationCampaignPlan:
        return plan_ordinary_hdl_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            groups=groups,
            required_suite=cast(tuple[str, ...], suite["names"]),
            revision=revision,
            invocation_id=invocation_id,
            execution_id="",
            trace=cast(bool, manifest.document["workload"]["trace"]),
            prerequisite_documents=cast(
                tuple[Mapping[str, object], ...], manifest.document["prerequisites"]
            ),
            planning_disclosures=disclosures,
            role=target["role"],
            required_suite_catalog_backed=not cast(bool, suite["default_invocation"]),
        )

    def _resume_coverage_prepared(self, validated: ValidatedResumeManifest) -> Any:
        """Run the shared coverage preflight for the manifest's frozen coverage choice."""
        from booley.criteria.state import DevelopmentState
        from booley.flows.sim.coverage_flow_context import coverage_context_for_execution
        from booley.flows.sim.coverage_invocation import (
            CoverageInvocationRequest,
            prepare_coverage_invocation,
        )

        manifest = validated.manifest
        target = cast(Mapping[str, str], manifest.document["target"])
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        names = cast(tuple[str, ...], cast(Mapping[str, object], items[0]["selection"])["names"])
        workload = cast(Mapping[str, object], manifest.document["workload"])
        context = coverage_context_for_execution(
            self.args.work_dir,
            DevelopmentState.load(self.args.state_file)
            if self.args.state_file
            else DevelopmentState(),
            recorder=self._acceptance_recorder,
        )
        runs = tuple(
            test_run_from_document(entry)
            for entry in cast(tuple[object, ...], items[0]["test_runs"])
        )
        prepared = prepare_coverage_invocation(
            CoverageInvocationRequest(
                (target["selector"],),
                tests=names,
                # The manifest froze its Test Runs: resume never resolves again.
                test_runs={target["selector"]: runs},
                trace=cast(bool, workload["trace"]),
                no_waivers=_manifest_no_waivers(manifest),
                diagnostic=self.args.diagnostic,
            ),
            context,
        )
        return prepared

    def _resume_coverage_preflight_if_coverage(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        items = cast(tuple[Mapping[str, object], ...], validated.manifest.document["work_items"])
        if items and items[0]["kind"] == "coverage_aggregate":
            return self._resume_coverage_preflight(validated)
        return None

    def _resume_coverage_preflight(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        """Refuse an unresumable raw-coverage aggregate before admission or reservation.

        Manifests without ``no_waivers`` (all legacy ones) keep their original
        lifecycle: the plan is rebuilt during resume, not at preflight.
        """
        if not _manifest_no_waivers(validated.manifest):
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
            raise SimulationCampaignIntegrityError(
                f"cannot reconstruct coverage aggregate: {detail}"
            )
        return prepared.plan.targets[0]
