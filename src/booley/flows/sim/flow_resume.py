"""Simulation Record resume (`--resume-from`) for the Simulation Flow.

The validated manifest, never ``progress.json``, decides what resume recovers.
A coverage Simulation Record never resumes.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.endpoint_session import default_report_root
from booley.flows.test_runs import TestRun
from booley.fusesoc import fusesoc_registry
from booley.runtime.endpoint_execution import EXIT_ERROR, EXIT_SUCCESS, EndpointOutcome

from .record import (
    NewSimulationRecordPreviewRequest,
    SimulationRecord,
    SimulationRecordCancellationError,
    SimulationRecordManifest,
    SimulationRecordManifestView,
    SimulationRecordPlan,
    SimulationRecordRecoveryStatus,
    ValidatedResumeManifest,
)
from .record.planning import (
    SimulationRecordWorkloadMismatchError,
    WorkloadDiagnostic,
    workload_diagnostic,
)
from .record_presentation import _simulation_record_recovery_detail
from .record_scaffold import _SimulationRecordRun, _SimulationRecordSteps
from .test_selection import _get_test_names

if TYPE_CHECKING:
    from contextlib import ExitStack

    from booley.flows.sim.request import SimRequest
    from booley.flows.target_test_plan import TargetTestPlan
    from booley.targets.domain import TargetHandle

    from .flow import OrdinarySimulationRecordPlanning, PlannedTarget, PreparedSimulationEndpoint
    from .record import SimulationRecordPolicy
    from .test_run_invocation import TestRunInvocation


def _manifest_run_groups(manifest: SimulationRecordManifest) -> tuple[tuple[TestRun, ...], ...]:
    """The Test Runs of each work item, exactly as the manifest froze them."""
    return SimulationRecordManifestView(manifest).test_run_groups


def _fresh_simulation_record_recovery_detail(
    validated: ValidatedResumeManifest, observed: SimulationRecordRecoveryStatus
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
        endpoint_kind: str
        _args: SimRequest
        _target_handles: dict[str, TargetHandle]
        _test_runs: TestRunInvocation

        @property
        def args(self) -> SimRequest: ...
        def _target_test_plan(self, target: str) -> TargetTestPlan: ...
        def _plan_ordinary_simulation_records(
            self, targets: list[str], test_names_map: dict[str, list[str]]
        ) -> OrdinarySimulationRecordPlanning | EndpointOutcome: ...
        def _ordinary_simulation_record_plan(
            self,
            planned: PlannedTarget,
            test_names_map: dict[str, list[str]],
            *,
            invocation_id: int,
            execution_id: str,
        ) -> SimulationRecordPlan: ...
        def _ordinary_simulation_record_run(
            self, planning: OrdinarySimulationRecordPlanning, record_directory: Path | None = None
        ) -> _SimulationRecordRun: ...
        def _simulation_record_policy(self) -> SimulationRecordPolicy: ...
        @staticmethod
        def _simulation_record_cancelled_outcome(
            error: SimulationRecordCancellationError, detail: Mapping[str, object] | None = None
        ) -> EndpointOutcome: ...

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
                exit_code=EXIT_ERROR, report_text=f"Simulation Record integrity failure: {exc}"
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
        command, note = self._new_run_command(validated, view)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                f"sim: cannot resume coverage Simulation Record {status.manifest_path}: "
                f"{reason}\n  {command}\n{note}"
            ),
            detail=_simulation_record_recovery_detail(status),
        )

    def _workload_mismatch_report(
        self, validated: ValidatedResumeManifest, diagnostic: WorkloadDiagnostic
    ) -> str:
        """What changed since the original run, and the command for a new run."""
        view = SimulationRecordManifestView(validated.manifest)
        command, note = self._new_run_command(validated, view)
        return f"{diagnostic.report()}\nStart a new run instead:\n  {command}\n{note}"

    def _new_run_command(
        self, validated: ValidatedResumeManifest, view: SimulationRecordManifestView
    ) -> tuple[str, str]:
        """The command for a new run that repeats the record, and the line after it."""
        selector = view.target.selector
        coverage = " --coverage" if view.is_coverage else ""
        trace = " --trace" if view.trace else ""
        no_waivers = " --no-waivers" if view.no_waivers else ""
        tests = self._frozen_test_run_options(validated.manifest, selector)
        command = (
            f"booley flow sim --target {shlex.quote(selector)}{tests or ''}{coverage}"
            f"{trace}{no_waivers}{self._recorded_report_dir_option(validated)}"
        )
        if self._unnamed_run_is_replaced(validated.manifest, selector):
            note = (
                "This Simulation Record froze the unnamed run of a Target without registered "
                "Tests. The Target registers Tests now: remove them from tests.toml first, or "
                "the command runs them instead."
            )
        elif tests is None:
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

    def _unnamed_run_is_replaced(self, manifest: SimulationRecordManifest, selector: str) -> bool:
        """Whether the record froze the unnamed run and the Target now registers Tests.

        ``--test`` cannot name that run, so no command repeats it while they exist.
        """
        try:
            runs = [run for group in _manifest_run_groups(manifest) for run in group]
            return [run.test for run in runs] == [None] and bool(
                self._target_test_plan(selector).tests
            )
        except (fusesoc_registry.FuseSocError, ValueError):
            return False

    def _frozen_test_run_options(
        self, manifest: SimulationRecordManifest, selector: str
    ) -> str | None:
        """``--test``/``--seed`` options that repeat the record's Test Runs in a new run.

        None when the manifest's Test Runs cannot be read.  Empty when the
        frozen Test Runs are the Target's whole default suite, in its order
        (a coverage record, planned sorted by Test, ignores order): the plain
        selection then resolves to the same runs.  Any other record (a subset,
        a reordering, a Test Variant, a pinned or drawn seed, a default that
        has since changed) is named run by run, so a new run draws no seed
        again.  The unnamed invocation is named by ``--seed``, or by nothing
        without a seed.
        """
        try:
            runs = [run for group in _manifest_run_groups(manifest) for run in group]
        except ValueError:
            return None
        try:
            defaults = self._target_test_plan(selector).default_runs()
            if SimulationRecordManifestView(manifest).is_coverage:
                whole_suite = sorted(runs, key=lambda run: run.name) == sorted(
                    defaults, key=lambda run: run.name
                )
            else:
                whole_suite = tuple(runs) == defaults
        except (fusesoc_registry.FuseSocError, ValueError):
            # Nothing shows the frozen runs are the default suite, so the command
            # names each of them (test_test_run_resolution_errors).
            whole_suite = False
        if whole_suite:
            return ""
        options: list[str] = []
        for run in runs:
            if run.test is not None:
                options.append(f" {shlex.quote(f'--test={run.name}')}")
            elif run.seed is not None:
                options.append(f" --seed {run.seed}")
        return "".join(options)

    def _replay_simulation_record(
        self, validated: ValidatedResumeManifest
    ) -> tuple[tuple[str, ...], dict[str, list[str]]]:
        """Replace this resume's request with the new-run request the record froze."""
        view = SimulationRecordManifestView(validated.manifest)
        selector = view.target.selector
        runs = tuple(run for group in view.test_run_groups for run in group)
        request = replace(
            self.args,
            resume_from=None,
            target=selector,
            test=tuple(run.name for run in runs if run.test is not None) or None,
            seed=next((run.seed for run in runs if run.test is None), None),
            trace=view.trace,
        )
        request.slug = self.args.slug  # init=False, so replace() dropped it
        self._args = request
        # A resume never resolves tests.toml again nor draws a seed: a removed
        # frozen Test is a changed workload, not a selection error.
        self._test_runs.resolved[(str(Path(request.work_dir)), selector)] = runs
        self._target_handles = {selector: validated.target_handles[0]}
        return (selector,), _get_test_names(request.work_dir)

    def _resume_simulation_record(
        self, prepared: PreparedSimulationEndpoint
    ) -> EndpointOutcome | _SimulationRecordRun:
        """Plan the replayed record as a new run: a refusal, a preview or the run."""
        validated = prepared.resume
        assert validated is not None, "only a resume reaches this path"
        try:
            observed = SimulationRecord().inspect_resume(validated)
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR, report_text=f"Simulation Record integrity failure: {exc}"
            )
        planning = self._plan_ordinary_simulation_records(
            list(prepared.selected_targets), prepared.test_names_map
        )
        if isinstance(planning, EndpointOutcome):
            return planning
        if self.args.dry_run:
            return self._preview_simulation_record_resume(validated, observed, planning)
        return self._resume_simulation_record_run(validated, observed, planning)

    def _preview_simulation_record_resume(
        self,
        validated: ValidatedResumeManifest,
        observed: SimulationRecordRecoveryStatus,
        planning: OrdinarySimulationRecordPlanning,
    ) -> EndpointOutcome:
        status = _simulation_record_recovery_detail(observed)
        try:
            plan = self._ordinary_simulation_record_plan(
                planning.candidates[0], planning.test_names_map, invocation_id=1, execution_id=""
            )
            diagnostic = workload_diagnostic(validated.manifest, plan.manifest)
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Record resume preview failed: {exc}",
                detail=status,
            )
        status.update(diagnostic.detail)
        # The real run reports a mismatch before its run_cwd check.
        if not diagnostic.areas:
            request = NewSimulationRecordPreviewRequest(
                plan,
                Path(self.args.work_dir),
                self.args.report_dir,
                self._simulation_record_policy(),
                record_directory=validated.path.parent,
            )
            if (refusal := _resume_run_cwd_refusal(request, status)) is not None:
                return refusal
        areas = diagnostic.areas
        lines = [
            f"manifest: {validated.path}",
            f"completed: {len(observed.completed)}",
            f"interrupted: {len(observed.interrupted)}",
            *_unfinished_test_run_lines(validated.manifest, observed.interrupted),
            f"pending: {len(observed.pending)}",
            *_unfinished_test_run_lines(validated.manifest, observed.pending),
            f"mismatches: {len(areas)}",
        ]
        if areas:
            lines.append(self._workload_mismatch_report(validated, diagnostic))
        return EndpointOutcome(
            exit_code=EXIT_ERROR if areas else EXIT_SUCCESS,
            detail=status,
            report_text="\n".join(lines),
        )

    def _resume_simulation_record_run(
        self,
        validated: ValidatedResumeManifest,
        observed: SimulationRecordRecoveryStatus,
        planning: OrdinarySimulationRecordPlanning,
    ) -> _SimulationRecordRun:
        """The ordinary run into the record's own directory, behind its origin lock."""
        ordinary = self._ordinary_simulation_record_run(
            planning, record_directory=validated.path.parent
        )

        def start(
            admission: AdmissionContext,
            invocation: Path,
            manifest_published: Callable[[Path], None],
        ) -> _SimulationRecordSteps | EndpointOutcome:
            lock_error = self._lock_simulation_record_resume_origin(validated, observed)
            if lock_error is not None:
                return lock_error
            steps = ordinary.start(admission, invocation, manifest_published)
            assert isinstance(steps, _SimulationRecordSteps), "an ordinary start never refuses"

            def run() -> EndpointOutcome:
                result = steps.run()
                result.detail.update(_fresh_simulation_record_recovery_detail(validated, observed))
                return result

            return replace(steps, run=run)

        return replace(
            ordinary,
            refusal="sim: no admission context",
            refusal_detail=_simulation_record_recovery_detail(observed),
            progress_operation="publish Simulation progress",
            start=start,
            failure=lambda error, _invocation: self._simulation_record_resume_failure(
                validated, observed, error
            ),
        )

    def _lock_simulation_record_resume_origin(
        self, validated: ValidatedResumeManifest, observed: SimulationRecordRecoveryStatus
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
            message = self._workload_mismatch_report(validated, error.diagnostic)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Simulation Record resume failed: {message}",
            detail=detail,
        )


def _unfinished_test_run_lines(
    manifest: SimulationRecordManifest, work_item_ids: tuple[str, ...]
) -> list[str]:
    """One line per Test Run of the named work items, in manifest order."""
    view = SimulationRecordManifestView(manifest)
    return [
        f"  {run.name}"
        for item_id, group in zip(view.work_item_ids, view.test_run_groups, strict=True)
        if item_id in work_item_ids
        for run in group
    ]


def _resume_run_cwd_refusal(
    request: NewSimulationRecordPreviewRequest, status: dict[str, object]
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
