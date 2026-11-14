# pyright: reportIncompatibleMethodOverride=true, reportIncompatibleVariableOverride=true
# The directive makes a mismatch between SimulateFlow and the host declarations in
# elab_only.py, standalone.py and test_selection.py a type error; keep it.
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, cast

from booley.config.project_config import (
    lookup_target_section,
)
from booley.criteria.thresholds import has_relative_threshold
from booley.evidence.fields import BASELINE_TARGET_PARAM
from booley.flows.cli_help import CliHelp
from booley.flows.display import format_flow_display_label
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.execution_persistence import (
    EvidenceDiscarded,
    FlowExecutionAdapter,
    register_campaign_for,
    wants_campaigns,
)
from booley.flows.job_estimates import JobRequest, sim_job_request
from booley.flows.prepared_invocation import PreparedInvocation
from booley.flows.progress_lifecycle import (
    ProgressLifecycle,
    ProgressPublicationError,
    progress_document,
)
from booley.flows.request import FlowRequest
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
    attempt_scope,
    current_progress,
    install_progress,
    publish_checkpoint,
)
from booley.flows.sim.record_invocation import SimulationCampaignInvocation
from booley.flows.sim.record_layout import RECORD_DIRECTORY
from booley.flows.sim.record_reports import target_report_directory
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import SimulationExecutionPort
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
    normalize_completion_error,
)
from booley.runtime.file_lock import LockContentionError
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    TargetHandle,
    criterion_matches_target,
)

from ..base import BuiltinFlow, SubprocessResult
from ..baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
)
from ..invocation import default_timeout_ms
from .build import (
    SimulationBuildInfrastructureError,
    SimulationBuildPreparationError,
)
from .elab_only import ElabOnlyMixin
from .execution import (
    SelectedTestRuns,
    SimulationExecution,
    SimulationOptions,
    SimulationPreview,
    SimulationSelection,
)
from .execution.engine import TRACE_CLEANUP_MARGIN_S
from .flow_resume import CampaignResumeMixin
from .mode import SimulationMode
from .record import (
    NewSimulationRecordPreviewRequest,
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordCancellationError,
    SimulationRecordIntegrityError,
    SimulationRecordManifest,
    SimulationRecordOutcome,
    SimulationRecordPlan,
    SimulationRecordPolicy,
    ValidatedResumeManifest,
    encode_simulation_record_manifest,
    started_simulation_record,
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
from .record.planning import (
    manifest_digest,
)
from .record.serial_execution import OrdinaryHdlSerialExecutor
from .record_presentation import (
    _append_missing_report_lines,
    _baseline_refusal_line,
    _build_infrastructure_detail,
    _campaign_build_infrastructure_detail,
    _campaign_hook_inputs,
    _campaign_preview_entry,
    _campaign_preview_outcome,
    _campaign_report_lines,
    _campaign_structured_details,
    _coverage_campaign_exit_code,
    _coverage_campaign_lines,
    _coverage_compatibility_targets,
    _coverage_failed_target,
    _coverage_failure_outcome,
    _coverage_preflight_findings_outcome,
    _coverage_request_selector,
    _coverage_request_target,
    _grade_exit_code,
    _is_pre_sim_card_line,
    _pre_sim_integrity_failure,
    _pre_sim_lines,
    _pre_sim_projection,
    _resolved_coverage_campaigns,
    _retain_coverage_campaign,
    _unmatched_coverage_failure,
)
from .record_scaffold import _CampaignRun, _CampaignSteps, _group_disclosures
from .standalone import StandaloneMixin
from .test_run_invocation import (
    BaselineLink,
    BaselineRuns,
    TestRunInvocation,
    decide_baseline_runs,
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
class PlannedBaselines:
    """The Cycle Count baseline Targets that run, decided once in their shared checkout."""

    revision: str
    worktree: Path
    targets: tuple[PlannedTarget, ...]


@dataclass(frozen=True, slots=True)
class OrdinaryCampaignPlanning:
    """What an ordinary run resolved before reserving anything; its run and dry run reuse it."""

    candidates: tuple[PlannedTarget, ...]
    baselines: PlannedBaselines | None
    test_names_map: dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class ReportTarget:
    """Candidate identity of a resume that has no catalog binding."""

    identity: str
    selector: str
    name: str

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ReportTarget:
        """The Target a campaign manifest records."""
        target = document["target"]
        return cls(
            f"{target['vlnv']}#{target['name']}", str(target["selector"]), str(target["name"])
        )


@dataclass(frozen=True, slots=True)
class CandidateWorkload:
    """One prepared candidate's Tests and the sim_pass Criteria they can judge."""

    handle: TargetHandle | ReportTarget
    selected: frozenset[str]
    eligible: frozenset[str]


_DEFAULT_TIMEOUT_MS = default_timeout_ms("sim")


def _link_baseline_prerequisites(
    prerequisites: dict[str, list[Mapping[str, object]]],
    links: Sequence[BaselineLink],
    by_run: Mapping[str, Mapping[str, object]],
) -> None:
    """Give each candidate the baseline observation of its own Test Run.

    Two Test Runs of one candidate cannot read the same baseline work item (one
    Cocotb batch holds several): a campaign links each work item once.
    """
    for link in links:
        assert link.run is not None, "an accepted baseline link has a Test Run"
        document = by_run[link.run.name]
        if document in prerequisites[link.candidate]:
            raise ValueError(
                f"Cycle Count comparison of {link.candidate} Test Run {link.run.name} would "
                f"read a baseline work item of {link.baseline} that another of its Test Runs "
                "already reads; compare these Tests in separate runs"
            )
        prerequisites[link.candidate].append(document)


def _prepared_coverage_display_label(plans: Sequence[CoverageTargetPlan]) -> str | None:
    """Describe the exact Target/test scope selected during coverage preflight."""
    targets = [plan.handle.selector for plan in plans]
    tests = [test for plan in plans for test in plan.selected_tests]
    return format_flow_display_label(targets, tests=tests if tests else None)


class SimulateFlow(
    ElabOnlyMixin,
    CampaignResumeMixin,
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
            SimulationExecutionPort,
        ]
        | None = None,
        campaign_publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._coverage_execution_factory = coverage_execution
        self._campaign_publication_checkpoint = campaign_publication_checkpoint

    def _start_invocation(
        self, adapter: FlowExecutionAdapter, request: FlowRequest | None = None
    ) -> None:
        """Start an invocation with no Campaign outcomes: only this Flow keeps them."""
        super()._start_invocation(adapter, request)
        self._simulation_campaign_outcomes: tuple[Any, ...] = ()
        self._simulation_acceptance_outcomes: tuple[Any, ...] = ()
        # Resumed origins this invocation's `publication_resources` already lock (see flow_resume).
        self._held_resume_origins: set[Path] = set()

    def begin_invocation(self) -> None:
        """Start this invocation's Test Run decisions afresh.

        Resolved Test Runs (drawn seeds included), baseline
        runs and the Goals' frozen Test Runs belong to one request. A reused Flow
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
        self._simulation_campaign_outcomes = ()
        self._simulation_acceptance_outcomes = ()
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
        from .record_reports import campaign_invocation_lock
        from .record_retention import invocation_booley_dir

        endpoint_dir = report_dir / self.name
        endpoint_dir.mkdir(parents=True, exist_ok=True)
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
                        campaign_invocation_lock(
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

    def record_flow_owned_acceptance(self, outcome: EndpointOutcome) -> bool:
        """Record Simulation Campaign acceptance; no Campaign outcomes means not handled."""
        from .acceptance import retain_partial_campaign_acceptance

        outcomes = self._simulation_campaign_outcomes
        if not outcomes:
            return False
        try:
            self.record_campaign_acceptance(outcomes)
        except EvidenceDiscarded:
            raise
        except Exception:
            retain_partial_campaign_acceptance(
                outcome, outcomes, self._simulation_acceptance_outcomes
            )
            raise
        # After acceptance, in this process. ``work_dir`` is the candidate checkout again
        # (``_in_checkout`` restored it): never a baseline worktree, the cwd or BOOLEY_DIR.
        register_trace_sessions(outcomes, Path(self.args.work_dir))
        return True

    def generic_binding_gate_applies(self) -> bool:
        """A coverage run is authorized by its own preparation, not the generic gate.

        Explicit native collection also supports ungated Targets (#213); in
        simulate mode coverage preparation authorizes the complete selection.
        """
        return not self.args.coverage

    def record_campaign_acceptance(self, outcomes: tuple[object, ...]) -> None:
        """Own campaign-specific reconciliation behind the generic endpoint hook."""
        from .acceptance import record_campaign_acceptance

        record_campaign_acceptance(
            self, outcomes, baseline_refusals=self._test_runs.baseline.refusals
        )

    def _capture_cycle_report(self, outcomes: Sequence[SimulationRecordOutcome]) -> None:
        if self.cycle_report_json is not None:
            return
        try:
            rows = {
                str(outcome.target["selector"]): [
                    {"test": item["test"], "cycle_count": item["cycle_count"]}
                    for item in outcome.observations
                ]
                for outcome in outcomes
            }
            self.cycle_report_json = json.dumps({"cycle_counts": rows}) if rows else "{}"
        except Exception:  # optional metadata cannot change verdict or acceptance
            logger.warning("Simulation cycle count metadata unavailable", exc_info=True)
            self.cycle_report_json = '{"cycle_counts_error":"unavailable"}'

    def refresh_report_detail(self, result: EndpointOutcome) -> None:
        """Bind final projection references after acceptance publication."""
        outcomes = self._simulation_campaign_outcomes
        invocation = self._reserved_invocation_dir
        if outcomes and invocation is not None:
            result.detail["records"] = _campaign_structured_details(outcomes, invocation)

    def report_candidate_workloads(
        self, prepared: PreparedSimulationEndpoint
    ) -> tuple[tuple[CandidateWorkload, ...], bool]:
        """Prepared candidates and their workloads; baseline prerequisites are not candidates."""
        if prepared.resume is None:
            handles = prepared.targets[: len(prepared.selected_targets)]
            coverage = bool(getattr(self.args, "coverage", False))
            # A coverage manifest records the declared suite; any other keeps skips removed.
            skips = {} if coverage else _get_test_skips(self.args.work_dir)
            workloads = tuple(
                self._candidate_workload(
                    handle,
                    {
                        test
                        for test in resolve_target_test_suite(
                            handle.selector,
                            identity=handle.identity,
                            test_names=prepared.test_names_map,
                            test_skips=skips,
                        ).tests
                        if test is not None
                    },
                    {
                        "default" if test is None else test
                        for test in self._resolve_tests_to_run(
                            handle.selector, prepared.test_names_map
                        )
                    },
                )
                for handle in handles
            )
            return workloads, coverage
        manifest = prepared.resume.candidate.manifest
        document: Any = manifest.document
        binding = prepared.resume.binding_for(manifest)
        handle = binding.handle if binding is not None else ReportTarget.from_document(document)
        suite, items = document["required_suite"], document["work_items"]
        registered = set(suite["names"])
        selected = set().union(
            *(set(item["selection"]["names"]) for item in items if item["role"] == "candidate")
        ) or ({"default"} if suite["default_invocation"] else registered)
        coverage = bool(items) and items[0].get("kind") == "coverage_aggregate"
        return (self._candidate_workload(handle, registered, selected),), coverage

    def _candidate_workload(
        self, handle: TargetHandle | ReportTarget, registered: set[str], selected: set[str]
    ) -> CandidateWorkload:
        """Fill an empty selection like the manifest does and attach acceptance eligibility."""
        from .acceptance import eligible_simulation_keys

        selected = selected or ({"default"} if not registered else set())
        eligible = eligible_simulation_keys(
            self.state, handle.identity, handle.selector, handle.name, registered, selected
        )
        return CandidateWorkload(handle, frozenset(selected), frozenset(eligible))

    def prepare_endpoint_invocation(self) -> PreparedInvocation | EndpointOutcome:
        """Prepare a Campaign; elaboration-only modes keep the Target-selection invocation."""
        if self._mode.elaborates_only:
            return super().prepare_endpoint_invocation()
        prepared = self.prepare_simulation_endpoint()
        if isinstance(prepared, EndpointOutcome):
            return prepared
        return SimulationCampaignInvocation(self, prepared)

    def memory_job_requests(self, handles: tuple[TargetHandle, ...]) -> tuple[JobRequest, ...]:
        """An elaboration-only Job runs no simulator process: its preparation and build are sized."""
        flags = self._resource_flags()
        return tuple(
            JobRequest("sim", handle.selector, test_runs=(), flags=flags) for handle in handles
        )

    def campaign_job_requests(
        self, prepared: PreparedSimulationEndpoint
    ) -> tuple[JobRequest, ...] | None:
        """What a prepared Simulation Campaign runs; ``None`` when that is not known yet.

        A resume, and a Cycle Count baseline on another Target, have no Test
        Runs resolved before admission. A baseline on a selected Target re-runs
        that Target's keys at another revision, so it adds no request.
        """
        selected = set(prepared.selected_targets)
        if prepared.resume is not None or any(
            handle.selector not in selected
            for handle in prepared.targets[len(prepared.selected_targets) :]
        ):
            return None
        flags = self._resource_flags()
        try:
            return tuple(
                sim_job_request(
                    self._registered_test_plan(target, prepared.test_names_map),
                    self._resolve_test_runs(target, prepared.test_names_map),
                    flags=flags,
                )
                for target in prepared.selected_targets
            )
        except (fusesoc_registry.FuseSocError, OSError, ValueError):
            return None

    def work_item_job_request(
        self, selector: str, runs: Sequence[TestRun], flags: tuple[str, ...]
    ) -> JobRequest | None:
        """What one Campaign work item runs, to size its own Memory Reservation.

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

    def report_families(self) -> set[str]:
        """Criterion families the selected mode can judge."""
        mode = self._mode
        if mode is SimulationMode.ELAB_ONLY_STANDALONE:
            return {"elab_pass", "elaborate_standalone"}
        if mode is SimulationMode.ELAB_ONLY:
            return {"elab_pass"}
        families = {"sim_pass", "cycle_count"}
        if getattr(self.args, "coverage", False):
            families.add("coverage")
        if self.state._file_path is not None:
            families.add("elab_pass")
        return families

    def conventional_report_families(self) -> set[str]:
        """Families a non-strict Project may judge under their conventional names."""
        if self._mode.elaborates_only:
            return {"elab_pass"}
        if self.state._file_path is None:
            return {"sim_pass"}
        return {"sim_pass", "elab_pass"}

    def prepare_simulation_endpoint(  # noqa: PLR0911 -- ordered pre-admission rejections
        self,
    ) -> PreparedSimulationEndpoint | EndpointOutcome:
        """Resolve and authorize every Target before reservation or admission."""
        from booley.flows.endpoint_admission import authorize_targets

        try:
            prepared = self._prepare_campaign_targets()
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
            # authorization, admission, build or Pre-Sim spawn.
            resolve_pre_sim_commands(self.args.work_dir)
            if resume is None and getattr(self.args, "coverage", False):
                coverage_error = self._prepare_coverage()
                if coverage_error is not None:
                    return coverage_error
            rejection = authorize_targets(self, targets)
        except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"sim campaign preflight failed: {exc}",
            )
        if rejection is not None:
            return rejection
        return PreparedSimulationEndpoint(targets, resume, selected_targets, test_names_map)

    def run_prepared_simulation(
        self,
        prepared: PreparedSimulationEndpoint,
        admission: object | None,
    ) -> EndpointOutcome:
        """Run one immutable preflight with its explicitly borrowed admission."""
        if prepared.resume is not None:
            campaign = self._resume_campaign(prepared.resume)
        elif getattr(self.args, "coverage", False):
            campaign = (
                self._preview_coverage_campaigns()
                if self.args.dry_run
                else self._coverage_campaign_run()
            )
        else:
            campaign = self._ordinary_campaign(prepared)
        # Refusals and dry runs are answered first: they reserve no invocation.
        result = (
            campaign
            if isinstance(campaign, EndpointOutcome)
            else self._with_live_progress(lambda: self._execute_campaign(admission, campaign))
        )
        if prepared.resume is not None:
            result = self._with_diagnostic_test_run_note(result)
            return self._attach_published_pre_sim(result, prepared.resume.path)
        result.detail["mode"] = self._mode.value
        return self._attach_published_pre_sim(
            self._with_baseline_refusal_note(self._with_diagnostic_test_run_note(result))
        )

    def _record_pre_sim_firing(self, key: tuple[str, str, str, int]) -> None:
        """Record only authenticated firings durably published by this execution."""
        self._current_published_pre_sim_keys.add(key)

    def _attach_published_pre_sim(
        self, result: EndpointOutcome, origin_manifest: Path | None = None
    ) -> EndpointOutcome:
        """Attach authentic hook evidence even when no terminal Campaign outcome exists."""
        from .record import collect_pre_sim_firings
        from .record_retention import invocation_booley_dir

        invocation = self._reserved_invocation_dir
        if invocation is None:
            return result
        try:
            paths = sorted((invocation / "targets").glob(f"*/{RECORD_DIRECTORY}/manifest.json"))
            if len(paths) > 4000:
                raise SimulationRecordIntegrityError("invocation Campaign count exceeds bound")
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
        from .record_retention import invocation_booley_dir

        firings = tuple(
            firing
            for outcome in self._simulation_campaign_outcomes
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
        self._simulation_campaign_outcomes = ()
        self._pending_criteria_set = ()
        self._report_criteria.evaluated.clear()
        self._report_criteria.known = False
        return _pre_sim_integrity_failure(result, error, retain_authenticated=retained)

    def _prepare_campaign_targets(
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
        if (missing := self._validate_interactive_args(requested)) is not None:
            return missing
        selected = [self._target_handle(target) for target in requested]
        baseline_ref, baseline_targets, baseline_error = self._cycle_baseline_selection(requested)
        if baseline_error is not None:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=baseline_error)
        baselines = self._campaign_authorization_baselines(baseline_ref, baseline_targets)
        return (*selected, *baselines), None, tuple(requested), _get_test_names(self.args.work_dir)

    def _campaign_authorization_baselines(
        self, revision: str | None, selectors: Sequence[str]
    ) -> tuple[TargetHandle, ...]:
        """Resolve Criterion baselines from their historical checkout."""
        if revision is None or not selectors:
            return ()
        catalog = TargetCatalog.build(self._shared_baseline_worktree(revision))
        return tuple(catalog.select(selector, for_flow="sim") for selector in selectors)

    def _shared_baseline_worktree(self, revision: str) -> Path:
        """This invocation's one checkout of *revision*, open until publication ends.

        Campaign authorization and Cycle Count planning open the baseline
        revision; campaign preview and execution reuse the planned path
        (``PlannedBaselines.worktree``). The scratch path depends
        only on the process and revision, so a second opening while the first
        is held fails in ``git worktree add`` (#1959); they share this one.
        Consumers run in that order and only execution writes (build output),
        so sharing never shows a consumer another's changes. Call from the
        candidate checkout: ``self.args.work_dir`` is the Project Root.
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

    satisfies: ClassVar[list[str]] = [
        "elab_pass",
        "elaborate_standalone",
        "sim_pass",
        "cycle_count",
        "coverage",
    ]
    satisfies_args: ClassVar[dict[str, str]] = {
        "coverage": "--coverage",
        "elab_pass": "--mode elab-only",
        "elaborate_standalone": "--mode elab-only-standalone",
    }
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
        return tuple(
            flag
            for flag, enabled in (("coverage", self.args.coverage), ("trace", self.args.trace))
            if enabled
        )

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

    def _validate_interactive_args(
        self,
        targets: list[str],
    ) -> EndpointOutcome | None:
        """Interactive-Mode guard: a Target must be selected.

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
    ) -> OrdinaryCampaignPlanning | EndpointOutcome:
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

        return self._plan_ordinary_campaigns(targets, test_names_map)

    def _plan_ordinary_campaigns(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> OrdinaryCampaignPlanning | EndpointOutcome:
        """Preview every Target and decide the baseline before anything is reserved."""
        baselines, baseline_errors = self._plan_baseline_targets(targets, test_names_map)
        candidates, candidate_errors = self._preview_targets(targets, test_names_map)
        errors = [*baseline_errors, *candidate_errors]
        if errors:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim planning failed: " + "; ".join(errors),
                detail={"mode": self._mode.value},
            )
        return OrdinaryCampaignPlanning(tuple(candidates), baselines, test_names_map)

    def _preview_targets(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
        selections: Mapping[str, SimulationSelection] | None = None,
    ) -> tuple[list[PlannedTarget], list[str]]:
        """Preview one checkout's selected Simulation Targets, once each.

        *selections* gives a Target the exact selection to preview instead of
        the request's own (a Cycle Count baseline runs the candidate's Test
        Runs, not what its checkout would resolve).
        """
        planned: list[PlannedTarget] = []
        errors: list[str] = []
        execution = self._simulation_execution()
        for target in targets:
            try:
                handle = self._target_handle(target)
                preview = execution.preview(
                    handle,
                    selections[target]
                    if selections is not None
                    else self._test_run_selection(target, test_names_map),
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

    def _plan_baseline_targets(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> tuple[PlannedBaselines | None, list[str]]:
        """Decide the baseline Test Runs once and preview them in the baseline checkout."""
        baseline_ref, baseline_targets, error = self._cycle_baseline_selection(targets)
        if error is not None:
            return None, [error]
        if baseline_ref is None:
            return None, []
        expected = {target: self._target_handle(target).identity for target in baseline_targets}
        links = self._baseline_links(targets, test_names_map)
        try:
            worktree = self._shared_baseline_worktree(baseline_ref)
            with self._baseline_checkout(worktree, links, baseline_ref) as baseline:
                runnable = [target for target in baseline_targets if baseline.runs_of(target)]
                planned, errors = self._preview_targets(
                    runnable,
                    test_names_map,
                    {target: SelectedTestRuns(baseline.runs_of(target)) for target in runnable},
                )
                mismatches = [
                    f"{item.selector}: baseline resolves to {item.preview.target_identity!r}, "
                    f"expected {expected[item.selector]!r}"
                    for item in planned
                    if item.preview.target_identity != expected[item.selector]
                ]
                return (
                    PlannedBaselines(baseline_ref, worktree, tuple(planned)),
                    [*errors, *mismatches],
                )
        except (BaselineWorktreeError, fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
            return None, [f"sim: Cycle Count baseline planning failed: {exc}"]

    def _baseline_links(
        self, targets: Sequence[str], test_names_map: Mapping[str, list[str]]
    ) -> tuple[BaselineLink, ...]:
        """Every relative Cycle Count comparison of *targets*, with its Test Run.

        A relative Cycle Count compares one Test Run on two revisions, so each
        link carries one concrete candidate Test Run (the same Test Variant
        arguments, environment and seed) for the baseline to run. A Test the
        candidate runs several times (``test@x3``, two Test Variants) gets a
        link per Test Run, so each has its own baseline run or its own named
        refusal. Call in the candidate checkout.
        """
        links: list[BaselineLink] = []
        for candidate, baseline, test in self._campaign_baseline_requirements(targets):
            runs = self._candidate_test_runs(candidate, test, test_names_map)
            # One link per candidate Test Run; without any, one that is refused by name.
            links.extend(BaselineLink(candidate, baseline, test, run) for run in runs or (None,))
        return tuple(links)

    def _candidate_test_runs(
        self, candidate: str, test: str, test_names_map: Mapping[str, list[str]]
    ) -> tuple[TestRun, ...]:
        """Every Test Run of *test* this invocation runs on *candidate*."""
        try:
            runs = self._resolve_test_runs(candidate, test_names_map)
        except (fusesoc_registry.FuseSocError, ValueError):
            # Nothing to compare: the link is refused by name and no baseline
            # runs for it (test_test_run_resolution_errors).
            return ()
        return tuple(run for run in runs if run.test == test)

    @contextmanager
    def _in_checkout(self, worktree: Path) -> Iterator[None]:
        """Work in *worktree*, and restore the candidate checkout on exit."""
        project_root = Path(self.args.work_dir)
        self.args.work_dir = worktree
        try:
            yield
        finally:
            self.args.work_dir = project_root

    @contextmanager
    def _baseline_checkout(
        self, worktree: Path, links: Sequence[BaselineLink], revision: str
    ) -> Iterator[BaselineRuns]:
        """Work in the baseline checkout with the candidate Test Runs it can run.

        Decides there which comparisons the baseline revision can run, keeps
        the decision for the rest of the invocation, and restores the
        candidate checkout on exit.
        """
        with self._in_checkout(worktree):
            self._test_runs.baseline = decide_baseline_runs(
                links, self._target_test_plan, revision
            )
            yield self._test_runs.baseline

    def _with_baseline_refusal_note(self, result: EndpointOutcome) -> EndpointOutcome:
        """Say which Cycle Count comparisons are inconclusive, and why."""
        refused = self._test_runs.baseline.refused
        if not refused:
            return result
        result.detail["cycle_count_baseline_inconclusive"] = [
            {
                "target": item.link.candidate,
                "test": item.link.test,
                **({"test_run": item.link.run.name} if item.link.run is not None else {}),
                "reason": item.reason,
            }
            for item in refused
        ]
        note = "\n".join(_baseline_refusal_line(item) for item in refused)
        result.report_text = f"{result.report_text}\n{note}" if result.report_text else note
        return result

    def _run(self) -> EndpointOutcome:
        """Run an elaboration-only mode; simulate mode always runs a prepared Campaign."""
        # prepare_endpoint_invocation sends every simulate-mode coverage run to
        # run_prepared_simulation; only elaboration-only modes reach this path.
        result = (
            EndpointOutcome(exit_code=2, report_text="Coverage requires simulation mode")
            if getattr(self.args, "coverage", False)
            else self._validate_mode_args() or self._run_elab_only()
        )
        result.detail["mode"] = self._mode.value
        return self._with_baseline_refusal_note(self._with_diagnostic_test_run_note(result))

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return super()._pre_state_gate()

    def _prepare_coverage(self) -> EndpointOutcome | None:
        from booley.criteria.state import DevelopmentState
        from booley.flows.sim.coverage_flow_context import coverage_context_for_execution
        from booley.flows.sim.coverage_invocation import (
            CoverageInvocationRequest,
            prepare_coverage_invocation,
        )

        if not self._flow_enabled():
            return EndpointOutcome(exit_code=2, report_text="sim is disabled")
        try:
            context = coverage_context_for_execution(
                self.args.work_dir,
                (
                    DevelopmentState.load(self.args.state_file)
                    if self.args.state_file
                    else DevelopmentState()
                ),
                recorder=self._acceptance_recorder,
            )
            selection = self._selected_target_selection()
            prepared = prepare_coverage_invocation(
                CoverageInvocationRequest(
                    tuple(self._requested_targets()),
                    tests=self.args.test or None,
                    test_runs=self._coverage_test_runs(selection.handles),
                    trace=self.args.trace,
                    no_waivers=self.args.no_waivers,
                    diagnostic=self.args.diagnostic,
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

    def _preview_coverage_campaigns(self) -> EndpointOutcome:
        """Preview one coverage Campaign per prepared Target.

        Reserves no invocation directory, publishes no manifest, registers no
        campaign with the execution adapter, and builds or simulates nothing.
        """
        campaign = SimulationRecord()
        try:
            previews = [
                _campaign_preview_entry(
                    campaign.preview(
                        NewSimulationRecordPreviewRequest(
                            # Placeholder ids: a preview reserves no invocation and
                            # starts no execution, so neither is ever read.
                            self._coverage_campaign_plan(plan, invocation_id=1, execution_id=""),
                            Path(self.args.work_dir),
                            self.args.report_dir,
                            self._campaign_policy(),
                        )
                    )
                )
                for plan in self._coverage_prepared.targets
            ]
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign coverage preview failed: {exc}",
            )
        outcome = _campaign_preview_outcome(previews)
        outcome.detail["coverage"] = True
        return outcome

    def _coverage_campaign_run(self) -> _CampaignRun:
        """Describe running each native coverage selection as one durable aggregate item."""
        prepared = self._coverage_prepared
        assert self.args.report_dir is not None, "prepared Flow requires a report root"

        def start(
            admission: AdmissionContext,
            invocation: Path,
            manifest_published: Callable[[Path], None],
        ) -> _CampaignSteps:
            progress = CoverageProgress(
                invocation, tuple(plan.handle.selector for plan in prepared.targets)
            )

            def run() -> EndpointOutcome:
                progress.checkpoint()
                campaign, requests = self._coverage_campaign_session(
                    admission, invocation, manifest_published
                )
                return self._execute_coverage_campaign_requests(campaign, requests, progress)

            return _CampaignSteps(
                lambda phase: progress.checkpoint(complete=True, phase=phase),
                run,
                finished=lambda result: (
                    result.exit_code != EXIT_ERROR
                    and len(progress.outcomes) == len(progress.targets)
                ),
            )

        return _CampaignRun(
            refusal="sim: Coverage Campaign has no borrowed admission context",
            refusal_detail={},
            progress_operation="publish coverage progress",
            start=start,
            failure=lambda error, _invocation: EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign coverage planning failed: {error}",
            ),
            paths_on_success=True,
        )

    def _coverage_campaign_session(
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
            execution_factory=self._coverage_campaign_execution,
            publication_checkpoint=self._campaign_publication_checkpoint,
        )
        campaign = SimulationRecord(
            executor,
            publication_checkpoint=self._campaign_publication_checkpoint,
            manifest_published=manifest_published,
            work_item_request=self.work_item_job_request,
        )
        execution_id = str(admission.execution_id or "")
        requests = [
            NewSimulationRecordRunRequest(
                self._coverage_campaign_plan(plan, int(invocation.name), execution_id),
                Path(self.args.work_dir),
                self.args.report_dir,
                self._campaign_policy(),
                invocation,
                admission,
                pre_sim_firing_published=self._record_pre_sim_firing,
            )
            for plan in prepared.targets
        ]
        for request in requests:
            campaign.publish_new(request)
        return campaign, requests

    def _execute_coverage_campaign_requests(
        self,
        campaign: SimulationRecord,
        requests: list[NewSimulationRecordRunRequest],
        progress: CoverageProgress,
    ) -> EndpointOutcome:
        """Execute published coverage aggregates in deterministic Target order."""
        outcomes: list[SimulationRecordOutcome] = []
        for index, request in enumerate(requests):
            try:
                outcome = campaign.run(request)
            except SimulationRecordCancellationError as exc:
                self._capture_cycle_report(outcomes)
                return self._with_coverage_campaign_lines(
                    self._campaign_cancelled_outcome(exc), outcomes
                )
            except (OSError, ValueError, RuntimeError) as exc:
                return self._coverage_campaign_failure(outcomes, requests, index, request, exc)
            outcomes.append(outcome)
            try:
                _retain_coverage_campaign(progress, outcome)
            except (OSError, ValueError) as exc:
                return self._coverage_campaign_failure(
                    outcomes[:-1], requests, index, request, exc, failed_outcome=outcome
                )
            try:
                progress.checkpoint()
            except (OSError, ValueError) as exc:
                result = self._campaign_endpoint_outcome(outcomes)
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
        return self._campaign_endpoint_outcome(outcomes)

    @staticmethod
    def _with_coverage_campaign_lines(
        result: EndpointOutcome, outcomes: Sequence[SimulationRecordOutcome]
    ) -> EndpointOutcome:
        """Append the finished Targets' ``coverage campaign:`` lines to a stopped run's card."""
        lines = _coverage_campaign_lines(outcomes)
        if lines:
            result.report_text = "\n".join([result.report_text, *lines])
        return result

    def _coverage_campaign_failure(
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
        self._simulation_campaign_outcomes = tuple(outcomes)
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
        target = _coverage_request_target(request)
        identity = f"{target['vlnv']}#{target['name']}"
        matches = [
            plan for plan in self._coverage_prepared.targets if plan.handle.identity == identity
        ]
        if len(matches) != 1:
            return None
        return "not_requested" if matches[0].criterion is None else "blocked"

    def _coverage_campaign_execution(
        self,
        plan: CoverageTargetPlan,
        options: SimulationOptions,
        pre_sim_commands: tuple[str, ...],
        pre_sim_build_access: str,
    ) -> SimulationExecutionPort:
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

    def _coverage_campaign_plan(
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

    def _ordinary_campaign(
        self, prepared: PreparedSimulationEndpoint
    ) -> EndpointOutcome | _CampaignRun:
        """Plan the prepared simulate-mode Campaign: a refusal, a preview or the run."""
        mode_error = self._validate_mode_args()
        if mode_error is not None:
            return mode_error
        planning = self._plan_ordinary_run(
            list(prepared.selected_targets), prepared.test_names_map
        )
        if isinstance(planning, EndpointOutcome):
            return planning
        if self.args.dry_run:
            return self._preview_ordinary_campaigns(planning)
        assert self.args.report_dir is not None, "prepared Flow requires a report root"
        return self._ordinary_campaign_run(planning)

    def _execute_campaign(
        self, admission: object | None, campaign: _CampaignRun
    ) -> EndpointOutcome:
        """Run one Campaign kind through the steps every kind shares.

        Admission check, invocation reservation, published-manifest tracking,
        the progress lifecycle and failure normalization live here; a kind
        supplies only what differs.
        """
        if not isinstance(admission, AdmissionContext):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=campaign.refusal,
                detail=dict(campaign.refusal_detail),
            )
        invocation = self.reserve_invocation_dir()
        assert invocation is not None
        published: list[Path] = []
        result: EndpointOutcome | None = None
        try:
            started = campaign.start(admission, invocation, self._manifest_publisher(published))
            if isinstance(started, EndpointOutcome):
                return started
            with ProgressLifecycle(started.terminal) as lifecycle:
                result = started.run()
                if started.finished(result):
                    lifecycle.complete()
                return (
                    self._with_campaign_paths(result, published)
                    if campaign.paths_on_success
                    else result
                )
        except (OSError, ValueError, RuntimeError) as exc:
            if result is not None and isinstance(exc, ProgressPublicationError):
                # The result exists; only publishing its terminal progress failed.
                result = normalize_completion_error(
                    result,
                    exc.__cause__ if isinstance(exc.__cause__, Exception) else exc,
                    campaign.progress_operation,
                    path=invocation / "progress.json",
                )
            else:
                result = campaign.failure(exc, invocation)
            return self._with_campaign_paths(result, published)

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
            adapter = self.execution_adapter
            diagnostic = getattr(self._args, "diagnostic", False)
            if wants_campaigns(adapter) and diagnostic is not True:
                # Before any of its work runs: a campaign that never returns an
                # outcome still leaves results its Goals must account for.
                register_campaign_for(adapter, started_simulation_record(path))
            if self._console_publication_requested:
                with suppress(OSError, ValueError):
                    print(f"campaign manifest: {path.resolve()}", file=sys.stderr, flush=True)

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

    def _with_campaign_paths(
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

    def _ordinary_campaign_run(self, planning: OrdinaryCampaignPlanning) -> _CampaignRun:
        """Describe running every planned Target as one ordinary Campaign."""
        targets = [planned.selector for planned in planning.candidates]
        outcomes: list[SimulationRecordOutcome] = []

        def start(
            admission: AdmissionContext,
            invocation: Path,
            manifest_published: Callable[[Path], None],
        ) -> _CampaignSteps:
            campaign = SimulationRecord(
                OrdinaryHdlSerialExecutor(invoke=self._execute_boundary),
                publication_checkpoint=self._campaign_publication_checkpoint,
                manifest_published=manifest_published,
                work_item_request=self.work_item_job_request,
            )

            def run() -> EndpointOutcome:
                self._write_campaign_progress(targets, outcomes, phase="starting", complete=False)
                baseline_requests, prerequisites = self._plan_campaign_baselines(
                    invocation, admission, planning
                )
                candidate_requests = self._candidate_campaign_requests(
                    planning, prerequisites, invocation, admission
                )
                self._publish_and_run_campaign_requests(
                    campaign,
                    baseline_requests,
                    candidate_requests,
                    outcomes=outcomes,
                    checkpoint=lambda: self._write_campaign_progress(
                        targets, outcomes, phase="running", complete=False
                    ),
                )
                return self._campaign_endpoint_outcome(outcomes)

            return _CampaignSteps(
                lambda phase: self._write_campaign_progress(
                    targets, outcomes, phase=phase, complete=True
                ),
                run,
            )

        return _CampaignRun(
            refusal="sim: Simulation Campaign has no borrowed admission context",
            refusal_detail={},
            progress_operation="publish simulation progress",
            start=start,
            failure=lambda error, _invocation: self._ordinary_campaign_failure(error, outcomes),
        )

    def _ordinary_campaign_failure(
        self, error: Exception, outcomes: list[SimulationRecordOutcome]
    ) -> EndpointOutcome:
        """Report an ordinary Campaign that stopped before any result existed."""
        self._capture_cycle_report(outcomes)
        if isinstance(error, SimulationRecordCancellationError):
            return self._campaign_cancelled_outcome(error)
        detail: dict[str, object] = (
            {"records": _campaign_structured_details(outcomes)} if outcomes else {}
        )
        if isinstance(error, SimulationBuildInfrastructureError):
            detail.update(_build_infrastructure_detail(error))
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            detail=detail,
            report_text=f"Simulation Campaign execution failed: {error}",
        )

    @staticmethod
    def _campaign_cancelled_outcome(
        error: SimulationRecordCancellationError,
        detail: Mapping[str, object] | None = None,
    ) -> EndpointOutcome:
        return EndpointOutcome(
            exit_code=EXIT_CANCELLED,
            detail=dict(detail or {}),
            report_text=str(error),
        )

    def _candidate_campaign_requests(
        self,
        planning: OrdinaryCampaignPlanning,
        prerequisites: Mapping[str, Sequence[Mapping[str, object]]],
        invocation: Path,
        admission: AdmissionContext,
    ) -> list[NewSimulationRecordRunRequest]:
        execution_id = str(admission.execution_id or "")
        requests = []
        for planned in planning.candidates:
            plan = self._ordinary_campaign_plan(
                planned,
                planning.test_names_map,
                invocation_id=int(invocation.name),
                execution_id=execution_id,
                prerequisite_documents=prerequisites.get(planned.selector, ()),
            )
            self._record_campaign_tool(plan.manifest)
            requests.append(
                NewSimulationRecordRunRequest(
                    plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._campaign_policy(),
                    invocation,
                    admission,
                    pre_sim_firing_published=self._record_pre_sim_firing,
                )
            )
        return requests

    @staticmethod
    def _publish_and_run_campaign_requests(
        campaign: SimulationRecord,
        baselines: Sequence[NewSimulationRecordRunRequest],
        candidates: Sequence[NewSimulationRecordRunRequest],
        *,
        outcomes: list[SimulationRecordOutcome] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> list[SimulationRecordOutcome]:
        completed = outcomes if outcomes is not None else []
        # Reject an unusable literal run_cwd before anything is published or built,
        # including Cycle Count baselines that would otherwise build first.
        for request in (*baselines, *candidates):
            campaign.preflight(request)
        for request in (*baselines, *candidates):
            campaign.publish_new(request)
        for request in baselines:
            target = cast(Mapping[str, object], request.plan.manifest.document["target"])
            with attempt_scope(
                str(target["selector"]),
                identity=f"{target['vlnv']}#{target['name']}",
                role="baseline",
                revision=str(target["revision"]),
                ephemeral_root=request.project_root,
            ):
                campaign.run(request)
        for request in candidates:
            completed.append(campaign.run(request))
            if checkpoint is not None:
                checkpoint()
        return completed

    def _write_campaign_progress(
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
            detail=_campaign_structured_details(outcomes),
            extra={"mode": self._mode.value},
        )
        if complete != bool(payload["complete"]):
            raise ValueError("Simulation Campaign progress complete and phase disagree")
        publish_checkpoint(invocation / "progress.json", payload)

    def _plan_campaign_baselines(
        self,
        invocation: Path,
        admission: Any,
        planning: OrdinaryCampaignPlanning,
    ) -> tuple[list[NewSimulationRecordRunRequest], dict[str, list[Mapping[str, object]]]]:
        """Build the planned baselines' requests without starting any campaign work."""
        baselines = planning.baselines
        if baselines is None:
            return [], {}
        prerequisites: dict[str, list[Mapping[str, object]]] = {
            planned.selector: [] for planned in planning.candidates
        }
        requests: list[NewSimulationRecordRunRequest] = []
        with self._in_checkout(baselines.worktree):
            for planned in baselines.targets:
                request, by_run = self._planned_baseline_request(
                    planned, baselines, planning.test_names_map, invocation, admission
                )
                requests.append(request)
                _link_baseline_prerequisites(
                    prerequisites, self._test_runs.baseline.links_of(planned.selector), by_run
                )
        return requests, prerequisites

    def _planned_baseline_request(
        self,
        planned: PlannedTarget,
        baselines: PlannedBaselines,
        test_names_map: dict[str, list[str]],
        invocation: Path,
        admission: Any,
    ) -> tuple[NewSimulationRecordRunRequest, dict[str, Mapping[str, object]]]:
        plan = self._ordinary_campaign_plan(
            planned,
            test_names_map,
            invocation_id=int(invocation.name),
            execution_id=str(admission.execution_id or ""),
            role="cycle_count_baseline",
            revision=baselines.revision,
        )
        request = NewSimulationRecordRunRequest(
            plan,
            baselines.worktree,
            self.args.report_dir,
            self._campaign_policy(),
            invocation,
            admission,
            pre_sim_firing_published=self._record_pre_sim_firing,
        )
        return request, self._baseline_prerequisites_by_run(planned.selector, plan)

    def _baseline_prerequisites_by_run(
        self, target: str, plan: SimulationRecordPlan
    ) -> dict[str, Mapping[str, object]]:
        """The prerequisite document of each baseline work item, by Test Run name."""
        target_document = cast(Mapping[str, str], plan.manifest.document["target"])
        relative = (
            target_report_directory(
                Path(), f"{target}@baseline-{target_document['revision'][:12]}"
            )
            / RECORD_DIRECTORY
            / "manifest.json"
        )
        documents = self._campaign_prerequisite_documents(plan, relative)
        items = cast(tuple[Mapping[str, object], ...], plan.manifest.document["work_items"])
        return {
            str(run["name"]): document
            for item, document in zip(items, documents, strict=True)
            for run in cast(Sequence[Mapping[str, object]], item["test_runs"])
        }

    def _campaign_baseline_requirements(
        self, targets: Sequence[str]
    ) -> tuple[tuple[str, str, str], ...]:
        """Return candidate/baseline/test links required by relative criteria."""
        handles = {target: self._target_handle(target) for target in targets}
        links: list[tuple[str, str, str]] = []
        for key, entry in self.state.criteria.items():
            params = entry.params or {}
            candidate = self._matching_cycle_target(handles, params)
            test = params.get("test")
            if (
                not key.startswith("cycle_count_")
                or candidate is None
                or not has_relative_threshold(params)
                or not isinstance(test, str)
                or not test
            ):
                continue
            baseline = params.get(BASELINE_TARGET_PARAM, candidate)
            if isinstance(baseline, str) and baseline:
                links.append((candidate, baseline, test))
        return tuple(dict.fromkeys(links))

    @staticmethod
    def _campaign_prerequisite_documents(
        plan: SimulationRecordPlan, manifest_path: Path
    ) -> list[Mapping[str, object]]:
        raw = encode_simulation_record_manifest(plan.manifest)
        manifest_ref = {
            "path_base": "origin_invocation",
            "path": manifest_path.as_posix(),
            "bytes": len(raw),
            "sha256": manifest_digest(plan.manifest),
            "kind": "simulation_record_manifest",
            "owner": plan.manifest.document["simulation_record_id"],
        }
        baseline_target = dict(cast(Mapping[str, object], plan.manifest.document["target"]))
        return [
            {
                "role": "cycle_count_baseline",
                "manifest": manifest_ref,
                "simulation_record_id": plan.manifest.document["simulation_record_id"],
                "target": baseline_target,
                "required_observation": "cycle_count",
                "work_item_id": item["work_item_id"],
            }
            for item in cast(
                tuple[Mapping[str, object], ...], plan.manifest.document["work_items"]
            )
        ]

    def _preview_ordinary_campaigns(self, planning: OrdinaryCampaignPlanning) -> EndpointOutcome:
        """Preview ordinary campaigns without reserving or mutating report state."""
        previews = []
        campaign = SimulationRecord()
        try:
            prerequisites, baseline_previews = self._preview_campaign_baselines(campaign, planning)
            previews.extend(baseline_previews)
            for planned in planning.candidates:
                plan = self._ordinary_campaign_plan(
                    planned,
                    planning.test_names_map,
                    invocation_id=1,
                    execution_id="",
                    prerequisite_documents=prerequisites.get(planned.selector, ()),
                )
                preview = campaign.preview(
                    NewSimulationRecordPreviewRequest(
                        plan,
                        Path(self.args.work_dir),
                        self.args.report_dir,
                        self._campaign_policy(),
                    )
                )
                previews.append(_campaign_preview_entry(preview))
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign preview failed: {exc}",
            )
        return _campaign_preview_outcome(previews)

    def _preview_campaign_baselines(
        self,
        campaign: SimulationRecord,
        planning: OrdinaryCampaignPlanning,
    ) -> tuple[dict[str, list[Mapping[str, object]]], list[dict[str, object]]]:
        """Preview linked baselines through the campaign seam without publication."""
        baselines = planning.baselines
        if baselines is None:
            return {}, []
        prerequisites: dict[str, list[Mapping[str, object]]] = {
            planned.selector: [] for planned in planning.candidates
        }
        previews: list[dict[str, object]] = []
        with self._in_checkout(baselines.worktree):
            for planned in baselines.targets:
                preview, by_run = self._preview_one_campaign_baseline(
                    campaign, planned, baselines, planning.test_names_map
                )
                previews.append(preview)
                _link_baseline_prerequisites(
                    prerequisites, self._test_runs.baseline.links_of(planned.selector), by_run
                )
        return prerequisites, previews

    def _preview_one_campaign_baseline(
        self,
        campaign: SimulationRecord,
        planned: PlannedTarget,
        baselines: PlannedBaselines,
        test_names_map: dict[str, list[str]],
    ) -> tuple[dict[str, object], dict[str, Mapping[str, object]]]:
        plan = self._ordinary_campaign_plan(
            planned,
            test_names_map,
            invocation_id=1,
            execution_id="",
            role="cycle_count_baseline",
            revision=baselines.revision,
        )
        preview = campaign.preview(
            NewSimulationRecordPreviewRequest(
                plan,
                baselines.worktree,
                self.args.report_dir,
                self._campaign_policy(),
            )
        )
        summary = {
            "target": dict(preview.target),
            "work_items": len(preview.work_items),
            "build_variants": len(preview.build_variants),
            "prerequisites": 0,
        }
        return summary, self._baseline_prerequisites_by_run(planned.selector, plan)

    def _ordinary_campaign_plan(
        self,
        planned: PlannedTarget,
        test_names_map: dict[str, list[str]],
        *,
        invocation_id: int,
        execution_id: str,
        prerequisite_documents: Sequence[Mapping[str, object]] = (),
        role: str = "candidate",
        revision: str | None = None,
    ) -> SimulationRecordPlan:
        """Turn one planned Target into the same exact plan used by preview and run."""
        handle = planned.handle
        inputs = self._ordinary_campaign_inputs(planned, test_names_map)
        if self.is_cocotb_target(planned.selector):
            return self._cocotb_campaign_plan(
                handle,
                inputs[0],
                inputs[1],
                inputs[3],
                revision,
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
            revision=revision or git_full_sha("HEAD", Path(self.args.work_dir)) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            prerequisite_documents=prerequisite_documents,
            planning_disclosures=inputs[2],
            role=role,
            required_suite_catalog_backed=inputs[4],
        )

    def _ordinary_campaign_inputs(self, planned: PlannedTarget, test_names_map):
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

    def _cocotb_campaign_plan(
        self,
        handle,
        inspection,
        preview,
        required,
        revision,
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
            revision=revision or git_full_sha("HEAD", Path(self.args.work_dir)) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            kind="cocotb_batch",
            planning_disclosures=planning_disclosures,
            required_suite_catalog_backed=catalog_backed,
        )

    def _record_campaign_tool(self, manifest: SimulationRecordManifest) -> None:
        """Use the frozen execution recipe, including when execution later fails."""
        document = manifest.document
        target = cast(Mapping[str, str], document["target"])
        workload = cast(Mapping[str, object], document["workload"])
        eda = cast(Mapping[str, str], workload["eda"])
        self._record_eda_tool(str(target["selector"]), str(eda["kind"]))

    def _campaign_policy(self) -> SimulationRecordPolicy:
        return SimulationRecordPolicy(
            timeout_seconds=self._effective_timeout_ms() / 1000,
            build_timeout_seconds=self._effective_build_timeout_ms() / 1000,
            no_kill=self.args.no_kill,
            diagnostic=self.args.diagnostic,
            result_verbosity=self.args.result_verbosity,
        )

    def _campaign_endpoint_outcome(
        self, outcomes: list[SimulationRecordOutcome]
    ) -> EndpointOutcome:
        self._simulation_campaign_outcomes = tuple(outcomes)
        self._capture_cycle_report(outcomes)
        coverage_campaigns = _resolved_coverage_campaigns(outcomes)
        grades = [outcome.aggregate_grade for outcome in outcomes]
        exit_code = _coverage_campaign_exit_code(outcomes, coverage_campaigns) or max(
            (_grade_exit_code(grade) for grade in grades), default=EXIT_SUCCESS
        )
        threshold_missed = any(
            campaign.evaluation["status"] == "fail" for campaign in coverage_campaigns.values()
        )
        report: dict[str, Any] = {
            "report_path": self._report_destination(),
            "default_seeds": self._test_runs.default_seeds,
        }
        invocation = self._reserved_invocation_dir
        campaigns = _campaign_structured_details(outcomes, invocation)
        coverage_targets = _coverage_compatibility_targets(
            outcomes,
            invocation,
            coverage_campaigns,
        )
        detail: dict[str, object] = {"records": campaigns}
        firings, current_keys = _campaign_hook_inputs(outcomes, invocation)
        pre_sim_detail, hook_lines = _pre_sim_projection(firings, current_keys, invocation)
        detail.update(pre_sim_detail)
        detail.update(_campaign_build_infrastructure_detail(outcomes))
        keys = frozenset(self._current_published_pre_sim_keys)
        budget_lines: list[str] | None = None
        if keys.issubset(firings):
            _, budget_lines = _pre_sim_lines(firings, keys)
        lines = _campaign_report_lines(outcomes, coverage_campaigns, budget_lines, **report)
        lines.extend(hook_lines)
        if coverage_targets:
            detail.update(coverage=True, targets=coverage_targets, pending_targets=[])
        return EndpointOutcome(
            exit_code=exit_code,
            cycle_report_json=self.cycle_report_json or "{}",
            criterion_met=exit_code == EXIT_SUCCESS and not threshold_missed,
            detail=detail,
            report_text="\n".join(lines),
            display_label=self._campaign_completed_display_label(outcomes),
        )

    def _campaign_completed_display_label(
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
            ("--result-verbosity full", self.args.result_verbosity == "full"),
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

    def _cycle_baseline_selection(
        self, targets: list[str]
    ) -> tuple[str | None, list[str], str | None]:
        """Return the pinned ref and selected Targets needing relative evidence."""
        from booley.evidence.fields import BASELINE_REF_PARAM

        refs: set[str] = set()
        selected: list[str] = []
        handles = {target: self._target_handle(target) for target in targets}
        for key, entry in self.state.criteria.items():
            params = entry.params or {}
            target = self._matching_cycle_target(handles, params)
            if not key.startswith("cycle_count_") or target is None:
                continue
            ref = params.get(BASELINE_REF_PARAM)
            if not has_relative_threshold(params) or not isinstance(ref, str) or not ref:
                continue
            refs.add(ref)
            baseline = params.get(BASELINE_TARGET_PARAM, target)
            if not isinstance(baseline, str) or not baseline:
                return None, selected, "sim: Cycle Count baseline Target is invalid"
            if baseline not in selected:
                selected.append(baseline)
        if not refs:
            return None, [], None
        if len(refs) != 1:
            return (
                None,
                selected,
                "sim: selected Cycle Count criteria carry conflicting baseline refs",
            )
        ref = next(iter(refs))
        resolved = git_full_sha(ref, Path(self.args.work_dir))
        if resolved is None:
            return (
                ref,
                selected,
                "sim: Cycle Count pinned baseline ref cannot be resolved to a commit",
            )
        return resolved, selected, None

    @staticmethod
    def _matching_cycle_target(handles: dict[str, Any], params: dict[str, Any]) -> str | None:
        return next(
            (
                selector
                for selector, handle in handles.items()
                if criterion_matches_target(
                    params, identity=handle.identity, selector=handle.selector
                )
            ),
            None,
        )

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
                result_verbosity=self.args.result_verbosity,
            ),
        )


if __name__ == "__main__":
    SimulateFlow().cli()
