# pyright: reportIncompatibleMethodOverride=true, reportIncompatibleVariableOverride=true
# The directive makes a mismatch between SimulateFlow and the host declarations in
# elab_only.py, flow_resume.py, standalone.py and test_selection.py a type error; keep it.
"""SimulateFlow — BooleyFlow for running RTL simulation.

Runs RTL simulation for one or more configs via the Edalize sim flow + a
EDA-tool-specific run-half (sim.verilator_run / sim.iverilog_run).
Supports multi-config sequential execution, per-test filtering, dry-run mode,
cycle count extraction, and structured JSON reporting.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, cast

from booley.config.project_config import (
    lookup_target_section,
)
from booley.flows.cli_help import CliHelp
from booley.flows.display import format_flow_display_label
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.job_estimates import JobRequest, sim_job_request, sim_resource_flags
from booley.flows.prepared_invocation import PreparedInvocation
from booley.flows.progress_lifecycle import (
    ProgressLifecycle,
    ProgressPublicationError,
    progress_document,
)
from booley.flows.request import FlowRequest
from booley.flows.sim.acceptance import publish_simulation_record_projections
from booley.flows.sim.bwave_registration import register_trace_sessions
from booley.flows.sim.cli import SimArguments
from booley.flows.sim.config import (
    resolve_pre_sim_commands,
    resolve_sim_build_timeout_ms,
    resolve_sim_trace_convert_timeout_ms,
)
from booley.flows.sim.coverage_invocation import CoverageTargetPlan
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.flows.sim.live_progress import (
    LiveProgressSink,
    current_progress,
    install_progress,
    publish_checkpoint,
)
from booley.flows.sim.record_invocation import SimulationRecordInvocation
from booley.flows.sim.record_layout import RECORD_DIRECTORY
from booley.flows.sim.record_reports import invocation_booley_dir
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import AuthenticatedSimulationExecutionPort
from booley.flows.target_test_plan import TargetTestPlan
from booley.flows.target_test_suite import (
    resolve_target_test_suite,
)
from booley.flows.test_runs import (
    TestRun,
)
from booley.fusesoc import fusesoc_registry
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_SUCCESS,
    EndpointOutcome,
    ExecutionResult,
    normalize_completion_error,
)
from booley.runtime.file_lock import LockContentionError
from booley.runtime.filesystem_utils import make_directories
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    TargetHandle,
)

from ..base import BuiltinFlow, SubprocessResult
from ..baseline_worktree import (
    baseline_worktree,
    git_full_sha,
)
from ..invocation import default_timeout_ms
from .build import (
    SimulationBuildPreparationError,
)
from .elab_only import ElabOnlyMixin
from .execution import (
    SelectedTestRuns,
    SimulationExecution,
    SimulationOptions,
    SimulationPreview,
)
from .execution.engine import TRACE_CLEANUP_MARGIN_S
from .flow_resume import SimulationRecordResumeMixin
from .mode import SimulationMode
from .record import (
    NewSimulationRecordPreviewRequest,
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordCancellationError,
    SimulationRecordIntegrityError,
    SimulationRecordManifest,
    SimulationRecordManifestView,
    SimulationRecordOutcome,
    SimulationRecordPlan,
    SimulationRecordPolicy,
    ValidatedResumeManifest,
    validate_resume_manifest,
)
from .record.coverage_execution import (
    CoverageAggregateExecutor,
    _CoverageAggregateError,
)
from .record.flow_planning import (
    plan_coarse_simulation_record,
    plan_ordinary_hdl_simulation_record,
)
from .record.serial_execution import OrdinaryHdlSerialExecutor
from .record_presentation import (
    _append_missing_report_lines,
    _coverage_compatibility_targets,
    _coverage_failed_target,
    _coverage_failure_outcome,
    _coverage_preflight_findings_outcome,
    _coverage_record_exit_code,
    _coverage_record_lines,
    _coverage_request_selector,
    _coverage_request_target,
    _coverage_target_outcome,
    _grade_exit_code,
    _is_pre_sim_card_line,
    _pre_sim_integrity_failure,
    _pre_sim_lines,
    _pre_sim_projection,
    _resolved_coverage_records,
    _simulation_record_build_infrastructure_detail,
    _simulation_record_hook_inputs,
    _simulation_record_preview_entry,
    _simulation_record_preview_outcome,
    _simulation_record_report_lines,
    _simulation_record_structured_details,
    _unmatched_coverage_failure,
)
from .record_scaffold import _group_disclosures, _SimulationRecordRun, _SimulationRecordSteps
from .standalone import StandaloneMixin
from .test_run_invocation import (
    TestRunInvocation,
)
from .test_selection import SimulationTestSelectionMixin, _get_test_names, _get_test_skips

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedSimulationEndpoint:
    """Read-only all-Target preflight retained through endpoint execution."""

    targets: tuple[TargetHandle, ...]
    resume: ValidatedResumeManifest | None
    selected_targets: tuple[str, ...]
    test_names_map: dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class PlannedTarget:
    """One Target previewed once for this invocation, in the checkout it runs in."""

    selector: str
    handle: TargetHandle
    preview: SimulationPreview


@dataclass(frozen=True, slots=True)
class OrdinarySimulationRecordPlanning:
    """What an ordinary run resolved before reserving anything; its run and dry run reuse it."""

    candidates: tuple[PlannedTarget, ...]
    test_names_map: dict[str, list[str]]


_DEFAULT_TIMEOUT_MS = default_timeout_ms("sim")


def _prepared_coverage_display_label(plans: Sequence[CoverageTargetPlan]) -> str | None:
    """Describe the exact Target/test scope selected during coverage preflight."""
    targets = [plan.handle.selector for plan in plans]
    tests = [test for plan in plans for test in plan.selected_tests]
    return format_flow_display_label(targets, tests=tests if tests else None)


class SimulateFlow(
    ElabOnlyMixin,
    SimulationRecordResumeMixin,
    SimulationTestSelectionMixin,
    StandaloneMixin,
    BuiltinFlow[SimRequest],
):
    """Run RTL simulation for one or more Targets."""

    def __init__(
        self,
        *,
        coverage_execution: Callable[
            [TargetHandle, SimulationOptions, tuple[str, ...], str],
            AuthenticatedSimulationExecutionPort,
        ]
        | None = None,
        simulation_record_publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._coverage_execution_factory = coverage_execution
        self._simulation_record_publication_checkpoint = simulation_record_publication_checkpoint

    def _start_invocation(self, request: FlowRequest | None = None) -> None:
        """Start an invocation with no Simulation Record outcomes: only this Flow keeps them."""
        super()._start_invocation(request)
        self._simulation_record_outcomes: tuple[Any, ...] = ()
        # Targets whose projection could not be published, with the error: they stay pending.
        self._projection_failures: list[tuple[SimulationRecordOutcome, Exception]] = []
        # Resumed origins this invocation's `publication_resources` already lock (see flow_resume).
        self._held_resume_origins: set[Path] = set()

    def begin_invocation(self) -> None:
        """Start this invocation's Test Run decisions afresh.

        Resolved Test Runs (drawn seeds included) belong to one request. A reused Flow
        object must resolve the next request itself, never reuse these. The
        Flow's other per-request state restarts here too, so no invocation reads
        what an earlier one prepared.
        """
        self._test_runs = TestRunInvocation()
        self._current_published_pre_sim_keys: set[tuple[str, str, str, int]] = set()
        self._coverage_prepared: Any = None
        self._resolved_build_timeout_ms: int | None = None
        self._resume_coverage_plan: CoverageTargetPlan | None = None
        self._target_handles: dict[str, TargetHandle] = {}
        self._work_item_plans: dict[str, TargetTestPlan] = {}
        self._standalone_plan_recipe = None

    def begin_requeued_attempt(self) -> None:
        """Drop the killed attempt's outcomes; keep its Test Runs, seeds and build settings."""
        super().begin_requeued_attempt()
        self._simulation_record_outcomes = ()
        self._projection_failures = []
        self._current_published_pre_sim_keys = set()

    request_type = SimRequest
    argument_adapter = SimArguments
    report_schema = "booley.simulation-report/v3"
    target_required = False

    name: str = "sim"
    description: str = (
        "Run RTL simulation for one or more Targets. Set mode=elab_only to "
        "compile, elaborate, and link the ordinary untraced simulator image "
        "without running tests, or mode=elab_only_standalone to add the "
        "reusable-module sweep. "
        "Do NOT use --trace for initial pass/fail checks — tracing adds "
        "overhead and is only useful after a failure, when you need waveforms "
        "for debugging via the B-Wave (`bwave`) MCP tool."
    )
    cli_help: ClassVar[CliHelp | None] = CliHelp(
        summary=(
            "Compile and run the RTL simulation tests of one or more Targets and report "
            "pass or fail for each test. Use it to check that a change still passes its tests. "
            "Add --mode elab-only to check that the design compiles and elaborates "
            "without running tests, or --mode elab-only-standalone to also sweep the "
            "reusable modules on their own. Leave --trace off for pass/fail runs: "
            "tracing slows simulation, so enable it only to capture waveforms of a "
            "failing test, then inspect them with `bwave`."
        ),
        example="booley flow sim --target sim_soc --test reset",
    )
    code_modifying: bool = False

    # Simulation is memory-admitted as a Sandbox job.
    def _resolve_job_class(self) -> str:
        """Simulation is a memory-admitted Sandbox workload."""
        return job_slots.CLASS_MEMORY

    def _next_invocation_dir(self, report_dir: Path) -> Path:
        """Acquire the Simulation producer lock before publishing its directory."""
        from .record_reports import simulation_record_invocation_lock

        endpoint_dir = report_dir / self.name
        make_directories(endpoint_dir)
        existing = (
            int(path.name.removeprefix(".pruned-"))
            for path in endpoint_dir.iterdir()
            if path.name.removeprefix(".pruned-").isdigit()
        )
        number = max(existing, default=0) + 1
        while number <= 1_000_000:
            invocation = endpoint_dir / str(number)
            # The attempt stack releases the lock on every exit that does not
            # publish the directory: a raced number or any mkdir failure.
            with ExitStack() as attempt:
                try:
                    attempt.enter_context(
                        # The same derivation the artifact references use, so a custom
                        # report root under a linked entry is refused here, up front.
                        simulation_record_invocation_lock(
                            invocation, booley_dir=invocation_booley_dir(invocation)
                        )
                    )
                    invocation.mkdir()
                except (LockContentionError, FileExistsError):
                    number += 1
                    continue
                self.publication_resources.push(attempt.pop_all())
                return invocation
        raise RuntimeError("Simulation invocation reservation ceiling exceeded")

    def _publish_projection(self, outcome: SimulationRecordOutcome) -> bool:
        """Publish one Target's ``simulation.json`` before anything reports the Target complete.

        A failure keeps the Target pending: it is recorded for the run's completion errors and
        the caller leaves the Target out of progress and the card.
        """
        try:
            publish_simulation_record_projections(Path(self.args.work_dir), (outcome,))
        except (OSError, ValueError, RuntimeError) as error:
            self._projection_failures.append((outcome, error))
            return False
        return True

    def _with_projection_failures(self, result: EndpointOutcome) -> EndpointOutcome:
        """Report each Target whose projection failed: a completion error and a pending Target."""
        if not self._projection_failures:
            return result
        for outcome, error in self._projection_failures:
            normalize_completion_error(
                result,
                error,
                "publish Simulation Record projections",
                path=outcome.manifest_path.parents[1] / "simulation.json",
            )
        pending = result.detail.get("pending_targets")
        result.detail["pending_targets"] = [
            *(cast(list[str], pending) if isinstance(pending, list) else []),
            *(str(outcome.target["selector"]) for outcome, _ in self._projection_failures),
        ]
        return result

    def finish_execution(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
        *,
        started: float | None,
    ) -> ExecutionResult:
        """Register the published Targets' bwave traces, then report: the attempt's final step.

        Only the attempt whose result stands reaches this, so a requeued first attempt never
        moves a bwave alias. ``work_dir`` is the candidate checkout, never the cwd or
        BOOLEY_DIR.
        """
        if self._simulation_record_outcomes:
            register_trace_sessions(self._simulation_record_outcomes, Path(self.args.work_dir))
        return super().finish_execution(prepared, outcome, started=started)

    def _capture_cycle_report(self, outcomes: Sequence[SimulationRecordOutcome]) -> None:
        if self.cycle_report_json is not None:
            return
        try:
            rows = {
                str(outcome.target["selector"]): [
                    {"test": item["test"], "cycle_count": item["cycle_count"]}
                    for item in outcome.observations
                ]
                # A Target whose projection failed keeps its measured counts.
                for outcome in (*outcomes, *(failed for failed, _ in self._projection_failures))
            }
            self.cycle_report_json = json.dumps({"cycle_counts": rows}) if rows else "{}"
        except Exception:  # optional metadata cannot change verdict or acceptance
            logger.warning("Simulation cycle count metadata unavailable", exc_info=True)
            self.cycle_report_json = '{"cycle_counts_error":"unavailable"}'

    def refresh_report_detail(self, result: EndpointOutcome) -> None:
        """Bind the records' artifact references to this invocation's directory."""
        outcomes = self._simulation_record_outcomes
        invocation = self._reserved_invocation_dir
        if outcomes and invocation is not None:
            result.detail["records"] = _simulation_record_structured_details(outcomes, invocation)

    def prepare_endpoint_invocation(self) -> PreparedInvocation | EndpointOutcome:
        """Prepare a Simulation Record; elaboration-only modes keep the Target-selection invocation."""
        if self._mode.elaborates_only:
            return super().prepare_endpoint_invocation()
        prepared = self.prepare_simulation_endpoint()
        if isinstance(prepared, EndpointOutcome):
            return prepared
        return SimulationRecordInvocation(self, prepared)

    def memory_job_requests(self, handles: tuple[TargetHandle, ...]) -> tuple[JobRequest, ...]:
        """An elaboration-only Job runs no simulator process: its preparation and build are sized."""
        flags = self._resource_flags()
        return tuple(
            JobRequest("sim", handle.selector, test_runs=(), flags=flags) for handle in handles
        )

    def simulation_record_job_requests(
        self, prepared: PreparedSimulationEndpoint
    ) -> tuple[JobRequest, ...] | None:
        """What a prepared Simulation Record runs; ``None`` when that is not known yet.

        A resume has no Test Runs resolved before admission.
        """
        if prepared.resume is not None:
            return None
        flags = self._resource_flags()
        try:
            pre_sim_commands = bool(resolve_pre_sim_commands(self.args.work_dir))
            return tuple(
                sim_job_request(
                    self._registered_test_plan(target, prepared.test_names_map),
                    self._resolve_test_runs(target, prepared.test_names_map),
                    flags=flags,
                    pre_sim_commands=pre_sim_commands,
                )
                for target in prepared.selected_targets
            )
        except (fusesoc_registry.FuseSocError, OSError, ValueError):
            return None

    def work_item_job_request(
        self, selector: str, runs: Sequence[TestRun], flags: tuple[str, ...]
    ) -> JobRequest | None:
        """What one Simulation Record work item runs, to size its own Memory Reservation.

        *flags* come from the frozen manifest, never from ``args`` (a resume
        reads them there). ``None`` when the Target does not resolve.
        """
        try:
            if selector not in self._work_item_plans:
                self._work_item_plans[selector] = self._target_test_plan(selector)
            return sim_job_request(self._work_item_plans[selector], runs, flags=flags)
        except (fusesoc_registry.FuseSocError, OSError, ValueError):
            return None

    def manifest_names_targets(self) -> bool:
        """An explicit resume reads its Targets from the manifest."""
        return self.args.resume_from is not None

    def prepare_simulation_endpoint(  # noqa: PLR0911 -- ordered pre-admission rejections
        self,
    ) -> PreparedSimulationEndpoint | EndpointOutcome:
        """Resolve every Target before reservation or admission."""
        try:
            prepared = self._prepare_simulation_record_targets()
            if isinstance(prepared, EndpointOutcome):
                return prepared
            targets, resume, selected_targets, test_names_map = prepared
            if resume is None:
                self.set_report_targets(list(selected_targets))
            if (
                resume is not None
                and (refusal := self._refuse_unfinished_coverage_resume(resume)) is not None
            ):
                return refusal
            if resume is None:
                selection_error = self._validate_prepared_selection(
                    list(selected_targets), test_names_map
                )
                if selection_error is not None:
                    return selection_error
            else:
                resume_error = self._resume_coverage_preflight_if_coverage(resume)
                if resume_error is not None:
                    return resume_error
            self._effective_build_timeout_ms()
            # A malformed conversion budget must refuse before any spawn too.
            resolve_sim_trace_convert_timeout_ms(self.args.work_dir)
            # A malformed Pre-Sim Commands knob must refuse here, before any
            # admission, build or Pre-Sim spawn.
            resolve_pre_sim_commands(self.args.work_dir)
            if resume is None and getattr(self.args, "coverage", False):
                coverage_error = self._prepare_coverage()
                if coverage_error is not None:
                    return coverage_error
        except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"sim Simulation Record preflight failed: {exc}",
            )
        return PreparedSimulationEndpoint(targets, resume, selected_targets, test_names_map)

    def run_prepared_simulation(
        self,
        prepared: PreparedSimulationEndpoint,
        admission: object | None,
    ) -> EndpointOutcome:
        """Run one immutable preflight with its explicitly borrowed admission."""
        if prepared.resume is not None:
            simulation_record = self._resume_simulation_record(prepared.resume)
        elif getattr(self.args, "coverage", False):
            simulation_record = (
                self._preview_coverage_simulation_records()
                if self.args.dry_run
                else self._coverage_simulation_record_run()
            )
        else:
            simulation_record = self._ordinary_simulation_record(prepared)
        # Refusals and dry runs are answered first: they reserve no invocation.
        result = (
            simulation_record
            if isinstance(simulation_record, EndpointOutcome)
            else self._with_live_progress(
                lambda: self._execute_simulation_record(admission, simulation_record)
            )
        )
        if prepared.resume is not None:
            return self._attach_published_pre_sim(result, prepared.resume.path)
        result.detail["mode"] = self._mode.value
        return self._attach_published_pre_sim(result)

    def _record_pre_sim_firing(self, key: tuple[str, str, str, int]) -> None:
        """Record only authenticated firings durably published by this execution."""
        self._current_published_pre_sim_keys.add(key)

    def _attach_published_pre_sim(
        self, result: EndpointOutcome, origin_manifest: Path | None = None
    ) -> EndpointOutcome:
        """Attach authentic hook evidence even when no terminal Simulation Record outcome exists."""
        from .record import collect_pre_sim_firings

        invocation = self._reserved_invocation_dir
        if invocation is None:
            return result
        try:
            paths = sorted((invocation / "targets").glob(f"*/{RECORD_DIRECTORY}/manifest.json"))
            if len(paths) > 4000:
                raise SimulationRecordIntegrityError(
                    "invocation Simulation Record count exceeds bound"
                )
            if origin_manifest is not None:
                paths.append(origin_manifest)
            firings = collect_pre_sim_firings(*paths, booley_dir=invocation_booley_dir(invocation))
        except (OSError, ValueError) as exc:
            return self._retain_pre_sim_after_scan_error(result, exc)
        return self._project_published_pre_sim(result, firings, invocation)

    def _project_published_pre_sim(self, result, firings, invocation) -> EndpointOutcome:
        if not firings:
            return result
        firings = {firing.key: firing for firing in firings}
        keys = frozenset(self._current_published_pre_sim_keys)
        detail, card = _pre_sim_projection(firings, keys, invocation)
        result.detail.update(detail)
        result.report_text = "\n".join(
            line for line in result.report_text.splitlines() if not _is_pre_sim_card_line(line)
        )
        return _append_missing_report_lines(result, card)

    def _retain_pre_sim_after_scan_error(
        self, result: EndpointOutcome, error: Exception
    ) -> EndpointOutcome:
        from .record import collect_pre_sim_firings

        firings = tuple(
            firing
            for outcome in self._simulation_record_outcomes
            for firing in outcome.pre_sim_firings
        )
        retained = False
        invocation = self._reserved_invocation_dir
        if firings:
            try:
                result = self._project_published_pre_sim(
                    result,
                    collect_pre_sim_firings(
                        prior=firings,
                        booley_dir=None
                        if invocation is None
                        else invocation_booley_dir(invocation),
                    ),
                    invocation,
                )
                retained = True
            except (OSError, ValueError):
                pass
        self._simulation_record_outcomes = ()
        return _pre_sim_integrity_failure(result, error, retain_authenticated=retained)

    def _prepare_simulation_record_targets(
        self,
    ) -> (
        tuple[
            tuple[TargetHandle, ...],
            ValidatedResumeManifest | None,
            tuple[str, ...],
            dict[str, list[str]],
        ]
        | EndpointOutcome
    ):
        if self.args.resume_from is not None:

            def revision_root(target: Mapping[str, str]) -> Path:
                if target["role"] == "candidate":
                    return Path(self.args.work_dir)
                return self._shared_baseline_worktree(target["revision"])

            resume = validate_resume_manifest(
                self.args.resume_from,
                project_root=Path(self.args.work_dir),
                revision_root=revision_root,
            )
            return resume.target_handles, resume, (), {}
        requested = self._resolve_requested_targets()
        if isinstance(requested, EndpointOutcome):
            return requested
        if (missing := self._validate_call_args(requested)) is not None:
            return missing
        selected = [self._target_handle(target) for target in requested]
        return tuple(selected), None, tuple(requested), _get_test_names(self.args.work_dir)

    def _shared_baseline_worktree(self, revision: str) -> Path:
        """This invocation's one checkout of *revision*, open until publication ends.

        Resuming a record that kept baseline prerequisites opens their revision. The
        scratch path depends only on the process and revision, so a second opening
        while the first is held fails in ``git worktree add`` (#1959); callers share
        this one. Call from the candidate checkout: ``self.args.work_dir`` is the
        Project Root.
        """
        worktrees = self.baseline_worktrees
        if revision not in worktrees:
            resources = self.publication_resources
            worktrees[revision] = resources.enter_context(
                baseline_worktree(
                    Path(self.args.work_dir),
                    revision,
                    paired_project=self._paired_project_baseline,
                )
            )
            # Unwinds before the worktree is removed: no entry outlives its checkout.
            resources.callback(worktrees.pop, revision)
        return worktrees[revision]

    # The MCP server computes mode-aware build, hook, execution, and finalization
    # headroom. This class default is only its defensive floor.
    default_timeout: ClassVar[int] = (_DEFAULT_TIMEOUT_MS // 1000) * 2 + TRACE_CLEANUP_MARGIN_S

    @property
    def _mode(self) -> SimulationMode:
        """Effective mode without erasing whether the caller supplied it."""
        return self.args.mode or SimulationMode.SIMULATE

    def _resolve_display_label(self) -> str | None:
        """Describe the requested Target/test scope without validating it."""
        targets = self._requested_targets()
        if self._mode.elaborates_only:
            return format_flow_display_label(targets, mode="elaboration")
        if getattr(self.args, "coverage", False) and self._coverage_prepared is not None:
            return _prepared_coverage_display_label(self._coverage_prepared.targets)
        try:
            test_names = _get_test_names(self.args.work_dir)
            selected = (
                [name for _target in targets for name in self.args.test]
                if self.args.test
                else [
                    test
                    for target in targets
                    for test in self._resolve_tests_to_run(target, test_names)
                ]
            )
        except Exception:  # display metadata must not block a Flow run
            logger.debug("could not resolve simulation display scope", exc_info=True)
            return format_flow_display_label(targets, tests=None)
        if any(test is None for test in selected):
            return format_flow_display_label(targets, tests=None)
        return format_flow_display_label(targets, tests=(str(test) for test in selected))

    def _build_command(self) -> list[str]:
        # Not used — _run() is overridden for multi-config logic
        return []

    def _interpret_result(self, result: SubprocessResult) -> EndpointOutcome:
        # Not used — _run() handles interpretation directly
        return EndpointOutcome()

    def _effective_timeout_ms(self) -> int:
        """Resolve the per-test timeout in ms.

        Precedence: explicit ``--timeout-ms`` CLI/MCP arg > ``[flows.sim]``
        ``timeout_ms`` in booley.toml (F4) > :data:`_DEFAULT_TIMEOUT_MS`. Mirrors
        ``AsicSynthesizeFlow._timeout_ms`` so a large core can persist a higher
        sim budget instead of passing ``--timeout-ms`` on every call.
        """
        return self._timeout_ms()

    def _effective_build_timeout_ms(self) -> int:
        """Resolve and freeze the invocation-wide simulator-image build budget."""
        if self._resolved_build_timeout_ms is None:
            self._resolved_build_timeout_ms = resolve_sim_build_timeout_ms(self.args.work_dir)
        return self._resolved_build_timeout_ms

    def _resource_flags(self) -> tuple[str, ...]:
        """Coverage and tracing change a simulation's footprint; key history by them."""
        return sim_resource_flags(coverage=self.args.coverage, trace=self.args.trace)

    def _get_timeout(self) -> int:
        """Wrapper timeout in seconds, derived from the effective timeout (ms).

        The child runner owns the actual simulator wall-clock budget. Every
        caller that spawns a traced simulation passes its own explicit timeout.
        """
        return self._effective_timeout_ms() // 1000

    def _cocotb_module_for_target(self, target: str) -> str | None:
        """The Target's declared ``cocotb_module``, from a cheap ``.core`` read.

        ADR 0034 decision 2: cocotb-ness is detected from the Target's flow
        options, never marked in tests.toml.
        This cheap read backs validation, dry-run previews and the batch-vs-loop
        dispatch *before* resolution; the run itself re-reads the *resolved*
        flow options (``ResolvedTarget.cocotb_module``) as the authority
        (ADR 0022 decision 6's enumerate-vs-resolve line). ``None`` for a
        non-cocotb Target or when the read fails (degrades to the SV path).
        """
        try:
            handle = self._target_handle(target)
            options = TargetCatalog.build(self.args.work_dir).inspect(handle).flow_options
        except Exception:  # noqa: BLE001 — best-effort cheap read; degrades to non-cocotb
            return None
        module = options.get("cocotb_module")
        return module if isinstance(module, str) and module else None

    def is_cocotb_target(self, target: str) -> bool:
        """True when *target* declares a ``cocotb_module`` (a Cocotb Target)."""
        return self._cocotb_module_for_target(target) is not None

    def _validate_call_args(
        self,
        targets: list[str],
    ) -> EndpointOutcome | None:
        """Developer Agent session guard: a Target must be selected.

        ``tb_top`` left the surface (ADR 0021) — a sim Target's ``toplevel`` is
        its TB top, so it comes from the resolved Target (via
        ``tb_top_for_target``), not a per-call ``--tb-top``. Nothing to validate
        here beyond config selection; the Target resolution downstream raises a
        clear error if a selected Target is malformed.
        """
        if not targets:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    "sim: --target is required when multiple or zero "
                    "Targets are available. Pass --target <name>; available "
                    "Targets are the .core sim Targets in .booley/."
                ),
            )
        return None

    def _plan_ordinary_run(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> OrdinarySimulationRecordPlanning | EndpointOutcome:
        """Validate the selection, then plan the run once; an outcome refuses it.

        Validates ``--test`` the way ``--target`` is already validated
        (``resolve_target_selection`` raises ``UnknownTargetError``) — an
        unknown name here otherwise falls through to a plusarg-less run of
        the TB's default test and reports a false PASS (a CI green on a typo).
        """
        test_error = self._validate_test_selector(targets, test_names_map)
        if test_error is not None:
            return test_error

        cocotb_error = self._validate_cocotb_targets(targets)
        if cocotb_error is not None:
            return cocotb_error

        plan_error = self._validate_test_plans(targets, test_names_map)
        if plan_error is not None:
            return plan_error
        runnable_error = self._validate_runnable_tests(targets, test_names_map)
        if runnable_error is not None:
            return runnable_error

        return self._plan_ordinary_simulation_records(targets, test_names_map)

    def _plan_ordinary_simulation_records(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> OrdinarySimulationRecordPlanning | EndpointOutcome:
        """Preview every Target before anything is reserved."""
        candidates, errors = self._preview_targets(targets, test_names_map)
        if errors:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim planning failed: " + "; ".join(errors),
                detail={"mode": self._mode.value},
            )
        return OrdinarySimulationRecordPlanning(tuple(candidates), test_names_map)

    def _preview_targets(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> tuple[list[PlannedTarget], list[str]]:
        """Preview one checkout's selected Simulation Targets, once each."""
        planned: list[PlannedTarget] = []
        errors: list[str] = []
        execution = self._simulation_execution()
        for target in targets:
            try:
                handle = self._target_handle(target)
                preview = execution.preview(
                    handle, self._test_run_selection(target, test_names_map)
                )
            except (
                fusesoc_registry.FuseSocError,
                SimulationBuildPreparationError,
                OSError,
                ValueError,
            ) as exc:
                errors.append(f"{target}: {exc}")
                continue
            planned.append(PlannedTarget(target, handle, preview))
        return planned, errors

    def _run(self) -> EndpointOutcome:
        """Run an elaboration-only mode; simulate mode always runs a prepared Simulation Record."""
        # prepare_endpoint_invocation sends every simulate-mode coverage run to
        # run_prepared_simulation; only elaboration-only modes reach this path.
        result = (
            EndpointOutcome(exit_code=2, report_text="Coverage requires simulation mode")
            if getattr(self.args, "coverage", False)
            else self._validate_mode_args() or self._run_elab_only()
        )
        result.detail["mode"] = self._mode.value
        return result

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return super()._pre_state_gate()

    def _prepare_coverage(self) -> EndpointOutcome | None:
        from booley.flows.sim.coverage_flow_context import coverage_project_context
        from booley.flows.sim.coverage_invocation import (
            CoverageInvocationRequest,
            prepare_coverage_invocation,
        )

        if not self._flow_enabled():
            return EndpointOutcome(exit_code=2, report_text="sim is disabled")
        try:
            context = coverage_project_context(self.args.work_dir)
            selection = self._selected_target_selection()
            prepared = prepare_coverage_invocation(
                CoverageInvocationRequest(
                    tuple(self._requested_targets()),
                    tests=self.args.test or None,
                    test_runs=self._coverage_test_runs(selection.handles),
                    trace=self.args.trace,
                    no_waivers=self.args.no_waivers,
                ),
                context,
                selection=selection,
            )
        except (ValueError, OSError, fusesoc_registry.FuseSocError) as exc:
            return EndpointOutcome(exit_code=2, report_text=f"Coverage Preflight: {exc}")
        if prepared.plan is None:
            return _coverage_preflight_findings_outcome(prepared.findings)
        self._coverage_prepared = prepared.plan
        for plan in prepared.plan.targets:
            self._record_eda_tool(plan.handle.selector, plan.handle.eda_tool)
        return None

    def _coverage_test_runs(
        self, handles: Sequence[TargetHandle]
    ) -> dict[str, tuple[TestRun, ...]]:
        """The Test Runs this invocation resolved for each coverage Target.

        Every selector form is collected: several Test Variants of one Test,
        pinned seeds and ``test@xN``. A request the Target cannot run raises
        (``ValueError`` or ``FuseSocError``), and coverage preflight reports
        it: collecting the Tests' defaults instead would ignore the request.
        """
        test_names_map = _get_test_names(self.args.work_dir)
        resolved: dict[str, tuple[TestRun, ...]] = {}
        for handle in handles:
            runs = self._resolve_test_runs(handle.selector, test_names_map)
            if all(run.test is not None for run in runs):
                resolved[handle.selector] = runs
        return resolved

    def _preview_coverage_simulation_records(self) -> EndpointOutcome:
        """Preview one coverage Simulation Record per prepared Target.

        Reserves no invocation directory, publishes no manifest, registers no
        record with the execution adapter, and builds or simulates nothing.
        """
        simulation_record = SimulationRecord()
        try:
            requests = [
                NewSimulationRecordPreviewRequest(
                    # Placeholder ids: a preview reserves no invocation and
                    # starts no execution, so neither is ever read.
                    self._coverage_simulation_record_plan(plan, invocation_id=1, execution_id=""),
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._simulation_record_policy(),
                )
                for plan in self._coverage_prepared.targets
            ]
            previews = [
                _simulation_record_preview_entry(simulation_record.preview(request))
                for request in requests
            ]
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Record coverage preview failed: {exc}",
            )
        try:
            for request in requests:
                simulation_record.preflight(request)
        except (OSError, ValueError) as exc:
            # The real run's failure, with every Target still pending.
            return _coverage_failure_outcome(
                (), {}, [_coverage_request_selector(item) for item in requests], exc, None
            )
        outcome = _simulation_record_preview_outcome(previews)
        outcome.detail["coverage"] = True
        return outcome

    def _coverage_simulation_record_run(self) -> _SimulationRecordRun:
        """Describe running each native coverage selection as one durable aggregate item."""
        prepared = self._coverage_prepared
        assert self.args.report_dir is not None, "prepared Flow requires a report root"

        def start(
            admission: AdmissionContext,
            invocation: Path,
            manifest_published: Callable[[Path], None],
        ) -> _SimulationRecordSteps:
            progress = CoverageProgress(
                invocation, tuple(plan.handle.selector for plan in prepared.targets)
            )

            def run() -> EndpointOutcome:
                progress.checkpoint()
                simulation_record, requests = self._coverage_simulation_record_session(
                    admission, invocation, manifest_published
                )
                return self._execute_coverage_simulation_record_requests(
                    simulation_record, requests, progress
                )

            return _SimulationRecordSteps(
                lambda phase: progress.checkpoint(complete=True, phase=phase),
                run,
                finished=lambda result: (
                    result.exit_code != EXIT_ERROR
                    and len(progress.outcomes) == len(progress.targets)
                ),
            )

        return _SimulationRecordRun(
            refusal="sim: coverage Simulation Record has no borrowed admission context",
            refusal_detail={},
            progress_operation="publish coverage progress",
            start=start,
            failure=lambda error, _invocation: EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Record coverage planning failed: {error}",
            ),
            paths_on_success=True,
        )

    def _coverage_simulation_record_session(
        self,
        admission: AdmissionContext,
        invocation: Path,
        manifest_published: Callable[[Path], None] | None = None,
    ) -> tuple[
        SimulationRecord,
        list[NewSimulationRecordRunRequest],
    ]:
        """Publish every immutable coverage manifest before admitting work."""
        prepared = self._coverage_prepared
        plans = {plan.handle.identity: plan for plan in prepared.targets}
        executor = CoverageAggregateExecutor(
            plans=plans,
            execution_factory=self._coverage_simulation_record_execution,
            publication_checkpoint=self._simulation_record_publication_checkpoint,
        )
        simulation_record = SimulationRecord(
            executor,
            publication_checkpoint=self._simulation_record_publication_checkpoint,
            manifest_published=manifest_published,
            work_item_request=self.work_item_job_request,
        )
        execution_id = str(admission.execution_id or "")
        requests = [
            NewSimulationRecordRunRequest(
                self._coverage_simulation_record_plan(plan, int(invocation.name), execution_id),
                Path(self.args.work_dir),
                self.args.report_dir,
                self._simulation_record_policy(),
                invocation,
                admission,
                pre_sim_firing_published=self._record_pre_sim_firing,
            )
            for plan in prepared.targets
        ]
        for request in requests:
            simulation_record.publish_new(request)
        return simulation_record, requests

    def _execute_coverage_simulation_record_requests(
        self,
        simulation_record: SimulationRecord,
        requests: list[NewSimulationRecordRunRequest],
        progress: CoverageProgress,
    ) -> EndpointOutcome:
        """Execute published coverage aggregates in deterministic Target order."""
        outcomes: list[SimulationRecordOutcome] = []
        for index, request in enumerate(requests):
            try:
                outcome = simulation_record.run(request)
            except SimulationRecordCancellationError as exc:
                self._capture_cycle_report(outcomes)
                return self._with_coverage_record_lines(
                    self._simulation_record_cancelled_outcome(exc), outcomes
                )
            except (OSError, ValueError, RuntimeError) as exc:
                return self._coverage_simulation_record_failure(
                    outcomes, requests, index, request, exc
                )
            try:
                target = _coverage_target_outcome(outcome)
            except (OSError, ValueError) as exc:
                return self._coverage_simulation_record_failure(
                    outcomes, requests, index, request, exc, failed_outcome=outcome
                )
            if not self._publish_projection(outcome):
                continue
            outcomes.append(outcome)
            progress.outcomes.append(target)
            try:
                progress.checkpoint()
            except (OSError, ValueError) as exc:
                result = self._simulation_record_endpoint_outcome(outcomes)
                result.detail = dict(result.detail)
                result.detail["pending_targets"] = [
                    _coverage_request_selector(item) for item in requests[index + 1 :]
                ]
                return normalize_completion_error(
                    result,
                    exc,
                    "publish coverage progress",
                    path=progress.invocation_dir / "progress.json",
                )
        return self._simulation_record_endpoint_outcome(outcomes)

    @staticmethod
    def _with_coverage_record_lines(
        result: EndpointOutcome, outcomes: Sequence[SimulationRecordOutcome]
    ) -> EndpointOutcome:
        """Append the finished Targets' ``Coverage Record:`` lines to a stopped run's card."""
        lines = _coverage_record_lines(outcomes)
        if lines:
            result.report_text = "\n".join([result.report_text, *lines])
        return result

    def _coverage_simulation_record_failure(
        self,
        outcomes: list[SimulationRecordOutcome],
        requests: list[NewSimulationRecordRunRequest],
        index: int,
        request: NewSimulationRecordRunRequest,
        error: Exception,
        *,
        failed_outcome: SimulationRecordOutcome | None = None,
    ) -> EndpointOutcome:
        """Report a fatal aggregate without inventing a terminal Simulation result."""
        self._simulation_record_outcomes = tuple(outcomes)
        self._capture_cycle_report(
            (*outcomes, failed_outcome) if failed_outcome is not None else outcomes
        )
        targets = _coverage_compatibility_targets(
            outcomes,
            self._reserved_invocation_dir,
        )
        failed = _coverage_request_selector(request)
        evaluation = self._coverage_failure_evaluation(request, error)
        if evaluation is None:
            return _unmatched_coverage_failure(outcomes, targets, error)
        targets[failed] = _coverage_failed_target(
            failed, error, evaluation, failed_outcome, self._reserved_invocation_dir
        )
        pending = [failed, *[_coverage_request_selector(item) for item in requests[index + 1 :]]]
        return _coverage_failure_outcome(
            outcomes,
            targets,
            pending,
            error,
            self._reserved_invocation_dir,
        )

    def _coverage_failure_evaluation(
        self, request: NewSimulationRecordRunRequest, error: Exception
    ) -> str | None:
        """Preserve nested evaluation or derive policy before an outcome exists."""
        if isinstance(error, _CoverageAggregateError):
            return cast(str, error.detail["evaluation"])
        identity = _coverage_request_target(request).identity
        matches = [
            plan for plan in self._coverage_prepared.targets if plan.handle.identity == identity
        ]
        if len(matches) != 1:
            return None
        return "not_requested" if matches[0].criterion is None else "blocked"

    def _coverage_simulation_record_execution(
        self,
        plan: CoverageTargetPlan,
        options: SimulationOptions,
        pre_sim_commands: tuple[str, ...],
        pre_sim_build_access: str,
    ) -> AuthenticatedSimulationExecutionPort:
        from booley.flows.sim.verilator_coverage_execution import VerilatorCoverageExecution

        return (
            self._coverage_execution_factory(
                plan.handle, options, pre_sim_commands, pre_sim_build_access
            )
            if self._coverage_execution_factory is not None
            else VerilatorCoverageExecution(
                plan.handle,
                invoke=self._execute_boundary,
                options=options,
                pre_sim_commands=pre_sim_commands,
                pre_sim_build_access=pre_sim_build_access,
            )
        )

    def _coverage_simulation_record_plan(
        self, plan: CoverageTargetPlan, invocation_id: int, execution_id: str
    ) -> SimulationRecordPlan:
        inspection = TargetCatalog.build(plan.handle.project_root).inspect(plan.handle)
        execution = self._simulation_execution()
        # One aggregate work item records every Test Run the plan collects,
        # in collection order, so each observation binds to its own run.
        preview = execution.preview(plan.handle, SelectedTestRuns(plan.test_runs))
        preview = replace(preview, groups=(plan.selected_tests,), run_groups=(plan.test_runs,))
        return plan_coarse_simulation_record(
            handle=plan.handle,
            inspection=inspection,
            preview=preview,
            selected_tests=plan.selected_tests,
            required_suite=plan.declared_tests,
            revision=git_full_sha("HEAD", plan.handle.project_root) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            kind="coverage_aggregate",
            required_suite_catalog_backed=True,
            no_waivers=self.args.no_waivers,
        )

    def _ordinary_simulation_record(
        self, prepared: PreparedSimulationEndpoint
    ) -> EndpointOutcome | _SimulationRecordRun:
        """Plan the prepared simulate-mode Simulation Record: a refusal, a preview or the run."""
        mode_error = self._validate_mode_args()
        if mode_error is not None:
            return mode_error
        planning = self._plan_ordinary_run(
            list(prepared.selected_targets), prepared.test_names_map
        )
        if isinstance(planning, EndpointOutcome):
            return planning
        if self.args.dry_run:
            return self._preview_ordinary_simulation_records(planning)
        assert self.args.report_dir is not None, "prepared Flow requires a report root"
        return self._ordinary_simulation_record_run(planning)

    def _execute_simulation_record(
        self, admission: object | None, simulation_record: _SimulationRecordRun
    ) -> EndpointOutcome:
        """Run one Simulation Record kind through the steps every kind shares.

        Admission check, invocation reservation, published-manifest tracking,
        the progress lifecycle and failure normalization live here; a kind
        supplies only what differs. Each Target's projection is published as its
        run returns, so progress lists a Target complete only once its
        ``simulation.json`` exists; a Target whose projection failed stays pending
        and the run ends ``aborted`` with a completion error.
        """
        if not isinstance(admission, AdmissionContext):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=simulation_record.refusal,
                detail=dict(simulation_record.refusal_detail),
            )
        invocation = self.reserve_invocation_dir()
        assert invocation is not None
        published: list[Path] = []
        result: EndpointOutcome | None = None
        try:
            started = simulation_record.start(
                admission, invocation, self._manifest_publisher(published)
            )
            if isinstance(started, EndpointOutcome):
                return started
            with ProgressLifecycle(started.terminal) as lifecycle:
                result = self._with_projection_failures(started.run())
                if started.finished(result) and not self._projection_failures:
                    lifecycle.complete()
                return (
                    self._with_simulation_record_paths(result, published)
                    if simulation_record.paths_on_success or self._projection_failures
                    else result
                )
        except (OSError, ValueError, RuntimeError) as exc:
            if result is not None and isinstance(exc, ProgressPublicationError):
                # The result exists; only publishing its terminal progress failed.
                result = normalize_completion_error(
                    result,
                    exc.__cause__ if isinstance(exc.__cause__, Exception) else exc,
                    simulation_record.progress_operation,
                    path=invocation / "progress.json",
                )
            else:
                result = self._with_projection_failures(simulation_record.failure(exc, invocation))
            return self._with_simulation_record_paths(result, published)

    def _with_live_progress(self, run: Callable[[], EndpointOutcome]) -> EndpointOutcome:
        """Bind execution observations to the original destination invocation."""
        args = self._args
        if args is None or getattr(args, "dry_run", False) or current_progress() is not None:
            return run()
        if not hasattr(args, "work_dir"):
            return run()
        invocation = self.reserve_invocation_dir()
        if invocation is None:
            return run()
        sink = LiveProgressSink(
            invocation / "progress.json",
            Path(self.args.work_dir),
            os.environ.get("BOOLEY_RUN_ID", ""),
        )
        with install_progress(sink):
            result = run()
        if sink.error is not None:
            result.detail["observation_error"] = sink.error
        return result

    def _manifest_publisher(self, published: list[Path]) -> Callable[[Path], None]:
        """Retain committed paths even when console publication is unavailable."""

        def publish(path: Path) -> None:
            path = path.resolve()
            if path in published:
                return
            published.append(path)
            if self._console_publication_requested:
                with suppress(OSError, ValueError):
                    print(
                        f"Simulation Record manifest: {path.resolve()}",
                        file=sys.stderr,
                        flush=True,
                    )

        return publish

    def _report_destination(self) -> Path | None:
        """Reserve and name this run's ``report.json``; call it only before ``write_report``."""
        args = self._args
        if args is None or getattr(args, "dry_run", False):
            return None
        if getattr(args, "report_dir", None) is None:
            return None
        try:
            # Reserves the directory write_report later fills, so the path is this run's report.
            invocation = self.reserve_invocation_dir()
        except (OSError, ValueError, RuntimeError):
            return None
        return None if invocation is None else (invocation / "report.json").resolve()

    def _with_simulation_record_paths(
        self, result: EndpointOutcome, published: Sequence[Path] = ()
    ) -> EndpointOutcome:
        lines = [result.report_text] if result.report_text else []
        existing = set(result.report_text.splitlines())
        for path in published:
            line = f"  manifest: {path.resolve()}"
            if line not in existing:
                lines.append(line)
                existing.add(line)
        report = self._report_destination()
        if report is not None and f"  report: {report}" not in existing:
            lines.append(f"  report: {report}")
        result.report_text = "\n".join(lines)
        return result

    def _ordinary_simulation_record_run(
        self, planning: OrdinarySimulationRecordPlanning
    ) -> _SimulationRecordRun:
        """Describe running every planned Target as one ordinary Simulation Record."""
        targets = [planned.selector for planned in planning.candidates]
        outcomes: list[SimulationRecordOutcome] = []

        def start(
            admission: AdmissionContext,
            invocation: Path,
            manifest_published: Callable[[Path], None],
        ) -> _SimulationRecordSteps:
            simulation_record = SimulationRecord(
                OrdinaryHdlSerialExecutor(invoke=self._execute_boundary),
                publication_checkpoint=self._simulation_record_publication_checkpoint,
                manifest_published=manifest_published,
                work_item_request=self.work_item_job_request,
            )

            def run() -> EndpointOutcome:
                self._write_simulation_record_progress(
                    targets, outcomes, phase="starting", complete=False
                )
                candidate_requests = self._candidate_simulation_record_requests(
                    planning, invocation, admission
                )
                self._publish_and_run_simulation_record_requests(
                    simulation_record,
                    candidate_requests,
                    outcomes=outcomes,
                    checkpoint=lambda: self._write_simulation_record_progress(
                        targets, outcomes, phase="running", complete=False
                    ),
                )
                return self._simulation_record_endpoint_outcome(outcomes)

            return _SimulationRecordSteps(
                lambda phase: self._write_simulation_record_progress(
                    targets, outcomes, phase=phase, complete=True
                ),
                run,
            )

        return _SimulationRecordRun(
            refusal="sim: Simulation Record has no borrowed admission context",
            refusal_detail={},
            progress_operation="publish simulation progress",
            start=start,
            failure=lambda error, _invocation: self._ordinary_simulation_record_failure(
                error, outcomes
            ),
        )

    def _ordinary_simulation_record_failure(
        self, error: Exception, outcomes: list[SimulationRecordOutcome]
    ) -> EndpointOutcome:
        """Report an ordinary Simulation Record that stopped before any result existed."""
        self._capture_cycle_report(outcomes)
        if isinstance(error, SimulationRecordCancellationError):
            return self._simulation_record_cancelled_outcome(error)
        detail: dict[str, object] = (
            {"records": _simulation_record_structured_details(outcomes)} if outcomes else {}
        )
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            detail=detail,
            report_text=f"Simulation Record execution failed: {error}",
        )

    @staticmethod
    def _simulation_record_cancelled_outcome(
        error: SimulationRecordCancellationError,
        detail: Mapping[str, object] | None = None,
    ) -> EndpointOutcome:
        return EndpointOutcome(
            exit_code=EXIT_CANCELLED,
            detail=dict(detail or {}),
            report_text=str(error),
        )

    def _candidate_simulation_record_requests(
        self,
        planning: OrdinarySimulationRecordPlanning,
        invocation: Path,
        admission: AdmissionContext,
    ) -> list[NewSimulationRecordRunRequest]:
        execution_id = str(admission.execution_id or "")
        requests = []
        for planned in planning.candidates:
            plan = self._ordinary_simulation_record_plan(
                planned,
                planning.test_names_map,
                invocation_id=int(invocation.name),
                execution_id=execution_id,
            )
            self._record_simulation_record_tool(plan.manifest)
            requests.append(
                NewSimulationRecordRunRequest(
                    plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._simulation_record_policy(),
                    invocation,
                    admission,
                    pre_sim_firing_published=self._record_pre_sim_firing,
                )
            )
        return requests

    def _publish_and_run_simulation_record_requests(
        self,
        simulation_record: SimulationRecord,
        candidates: Sequence[NewSimulationRecordRunRequest],
        *,
        outcomes: list[SimulationRecordOutcome] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> list[SimulationRecordOutcome]:
        completed = outcomes if outcomes is not None else []
        # Reject an unusable literal run_cwd before anything is published or built.
        for request in candidates:
            simulation_record.preflight(request)
        for request in candidates:
            simulation_record.publish_new(request)
        for request in candidates:
            outcome = simulation_record.run(request)
            if self._publish_projection(outcome):
                completed.append(outcome)
                if checkpoint is not None:
                    checkpoint()
        return completed

    def _write_simulation_record_progress(
        self,
        targets: Sequence[str],
        outcomes: Sequence[SimulationRecordOutcome],
        *,
        phase: str,
        complete: bool,
    ) -> None:
        invocation = self.reserve_invocation_dir()
        if invocation is None:
            return
        completed = [str(outcome.target["selector"]) for outcome in outcomes]
        payload = progress_document(
            flow=self.name,
            run_id=os.environ.get("BOOLEY_RUN_ID", ""),
            phase=phase,
            targets=targets,
            completed_targets=completed,
            detail=_simulation_record_structured_details(outcomes),
            extra={"mode": self._mode.value},
        )
        if complete != bool(payload["complete"]):
            raise ValueError("Simulation Record progress complete and phase disagree")
        publish_checkpoint(invocation / "progress.json", payload)

    def _preview_ordinary_simulation_records(
        self, planning: OrdinarySimulationRecordPlanning
    ) -> EndpointOutcome:
        """Preview ordinary records without reserving or mutating report state."""
        previews = []
        requests = []
        simulation_record = SimulationRecord()
        try:
            for planned in planning.candidates:
                plan = self._ordinary_simulation_record_plan(
                    planned,
                    planning.test_names_map,
                    invocation_id=1,
                    execution_id="",
                )
                request = NewSimulationRecordPreviewRequest(
                    plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._simulation_record_policy(),
                )
                requests.append(request)
                previews.append(
                    _simulation_record_preview_entry(simulation_record.preview(request))
                )
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Record preview failed: {exc}",
            )
        try:
            for request in requests:
                simulation_record.preflight(request)
        except (OSError, ValueError) as exc:
            return self._ordinary_simulation_record_failure(exc, [])
        return _simulation_record_preview_outcome(previews)

    def _ordinary_simulation_record_plan(
        self,
        planned: PlannedTarget,
        test_names_map: dict[str, list[str]],
        *,
        invocation_id: int,
        execution_id: str,
    ) -> SimulationRecordPlan:
        """Turn one planned Target into the same exact plan used by preview and run."""
        handle = planned.handle
        inputs = self._ordinary_simulation_record_inputs(planned, test_names_map)
        if self.is_cocotb_target(planned.selector):
            return self._cocotb_simulation_record_plan(
                handle,
                inputs[0],
                inputs[1],
                inputs[3],
                invocation_id,
                execution_id,
                inputs[2],
                inputs[4],
            )
        return plan_ordinary_hdl_simulation_record(
            handle=handle,
            inspection=inputs[0],
            preview=inputs[1],
            groups=inputs[1].groups,
            required_suite=inputs[3],
            revision=git_full_sha("HEAD", Path(self.args.work_dir)) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            planning_disclosures=inputs[2],
            required_suite_catalog_backed=inputs[4],
        )

    def _ordinary_simulation_record_inputs(self, planned: PlannedTarget, test_names_map):
        target, handle, preview = planned.selector, planned.handle, planned.preview
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        execution = self._simulation_execution()
        disclosures = _group_disclosures(execution, handle, preview.groups, preview.run_groups)
        suite = resolve_target_test_suite(
            target,
            identity=handle.identity,
            test_names=test_names_map,
            test_skips=_get_test_skips(self.args.work_dir),
        )
        required = tuple(name for name in suite.tests if name is not None)
        catalog_backed = bool(lookup_target_section(test_names_map, handle.identity) or [])
        return inspection, preview, disclosures, required, catalog_backed

    def _cocotb_simulation_record_plan(
        self,
        handle,
        inspection,
        preview,
        required,
        invocation_id,
        execution_id,
        planning_disclosures,
        catalog_backed,
    ) -> SimulationRecordPlan:
        selected = tuple(name for group in preview.groups for name in group)
        return plan_coarse_simulation_record(
            handle=handle,
            inspection=inspection,
            preview=preview,
            selected_tests=selected,
            required_suite=required,
            revision=git_full_sha("HEAD", Path(self.args.work_dir)) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            kind="cocotb_batch",
            planning_disclosures=planning_disclosures,
            required_suite_catalog_backed=catalog_backed,
        )

    def _record_simulation_record_tool(self, manifest: SimulationRecordManifest) -> None:
        """Use the frozen execution recipe, including when execution later fails."""
        view = SimulationRecordManifestView(manifest)
        self._record_eda_tool(view.target.selector, view.eda_tool_kind)

    def _simulation_record_policy(self) -> SimulationRecordPolicy:
        return SimulationRecordPolicy(
            timeout_seconds=self._effective_timeout_ms() / 1000,
            build_timeout_seconds=self._effective_build_timeout_ms() / 1000,
            no_kill=self.args.no_kill,
        )

    def _simulation_record_endpoint_outcome(
        self, outcomes: list[SimulationRecordOutcome]
    ) -> EndpointOutcome:
        self._simulation_record_outcomes = tuple(outcomes)
        self._capture_cycle_report(outcomes)
        coverage_records = _resolved_coverage_records(outcomes)
        grades = [outcome.aggregate_grade for outcome in outcomes]
        exit_code = _coverage_record_exit_code(outcomes, coverage_records) or max(
            (_grade_exit_code(grade) for grade in grades), default=EXIT_SUCCESS
        )
        report: dict[str, Any] = {
            "report_path": self._report_destination(),
            "default_seeds": self._test_runs.default_seeds,
        }
        invocation = self._reserved_invocation_dir
        simulation_records = _simulation_record_structured_details(outcomes, invocation)
        coverage_targets = _coverage_compatibility_targets(
            outcomes,
            invocation,
            coverage_records,
        )
        detail: dict[str, object] = {"records": simulation_records}
        firings, current_keys = _simulation_record_hook_inputs(outcomes, invocation)
        pre_sim_detail, hook_lines = _pre_sim_projection(firings, current_keys, invocation)
        detail.update(pre_sim_detail)
        detail.update(_simulation_record_build_infrastructure_detail(outcomes))
        keys = frozenset(self._current_published_pre_sim_keys)
        budget_lines: list[str] | None = None
        if keys.issubset(firings):
            _, budget_lines = _pre_sim_lines(firings, keys)
        lines = _simulation_record_report_lines(outcomes, coverage_records, budget_lines, **report)
        lines.extend(hook_lines)
        if coverage_targets:
            detail.update(coverage=True, targets=coverage_targets, pending_targets=[])
        return EndpointOutcome(
            exit_code=exit_code,
            cycle_report_json=self.cycle_report_json or "{}",
            detail=detail,
            report_text="\n".join(lines),
            display_label=self._simulation_record_completed_display_label(outcomes),
        )

    def _simulation_record_completed_display_label(
        self, outcomes: Sequence[SimulationRecordOutcome]
    ) -> str | None:
        """Describe the requested Targets and trustworthy observed test scope."""
        observations = [item for outcome in outcomes for item in outcome.observations]
        tests = [item.get("test") for item in observations]
        if not tests or any(not isinstance(test, str) or not test.strip() for test in tests):
            return format_flow_display_label(self._requested_targets(), tests=None)
        return format_flow_display_label(self._requested_targets(), tests=cast(list[str], tests))

    def _validate_mode_args(self) -> EndpointOutcome | None:
        """Refuse bare ``--standalone`` and run-stage arguments meaningless in elab-only mode."""
        if self.args._legacy_standalone_without_elab:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    "sim: --standalone alone is invalid; use --mode elab-only-standalone"
                ),
            )
        if not self._mode.elaborates_only:
            return None
        conflicts = (
            ("--test", bool(self.args.test)),
            ("--trace", self.args.trace),
            ("--no-kill", self.args.no_kill),
        )
        for argument, active in conflicts:
            if active:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: {argument} conflicts with --mode {self._mode.value}; "
                        f"remove {argument} or use --mode simulate."
                    ),
                )
        return None

    def _resolve_requested_targets(self) -> list[str] | EndpointOutcome:
        handles = self._selected_target_handles()
        self._target_handles = {handle.selector: handle for handle in handles}
        targets = [handle.selector for handle in handles]
        if targets:
            return targets
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                "sim: --target is required. Pass --target <name> (or a "
                "comma-separated list); list available Targets with `booley targets`."
            ),
        )

    def _target_identity(self, target: str) -> str:
        """The identity (``vlnv#name``) of the selected Target written *target*."""
        return self._target_handle(target).identity

    def _target_handle(self, target: str) -> TargetHandle:
        """Return the selected handle for the active checkout.

        Cycle-count baselines intentionally materialize another checkout, so
        they reselect and verify the planned selector in that checkout.
        """
        root = Path(self.args.work_dir).resolve()
        selected = self._target_handles.get(target)
        if selected is not None and selected.project_root == root:
            return selected
        return TargetCatalog.build(root).select(target, for_flow="sim")

    def _record_eda_tool(self, target: str, eda_tool: str | None) -> None:
        """Remember *target*'s raw resolved EDA tool (e.g. ``"verilator"``).

        Raw — not the ``normalize_eda_tool`` run-half family — because this
        feeds report observability (the per-target tool and the run-level
        ``eda_tool`` key), not parser dispatch. Recorded at prepare time.
        """
        self.record_eda_tool(target, eda_tool)

    def _simulation_execution(self) -> SimulationExecution:
        """Compose the execution boundary with the Flow's process transport."""
        return SimulationExecution(
            invoke=self._execute_boundary,
            options=SimulationOptions(
                trace=self.args.trace,
                timeout_ms=self.args.timeout_ms,
                build_timeout_ms=self._effective_build_timeout_ms(),
            ),
        )


if __name__ == "__main__":
    SimulateFlow().cli()
