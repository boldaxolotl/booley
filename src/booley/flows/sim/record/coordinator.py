"""Serial durable coordinator behind the small Simulation Record seam."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.build_session import SimulationBuildLeaseStoppedError
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.coverage_record import CoverageRecordValidationError
from booley.flows.sim.coverage_record_store import CoverageRecordStoreError
from booley.flows.sim.coverage_reference import (
    CoverageRecordReference,
    build_coverage_record_reference,
    encode_coverage_record_reference,
    publish_coverage_record_reference,
)
from booley.flows.sim.execution.contract import PreSimScopeStoppedError, SimulationOptions
from booley.flows.sim.record_layout import COVERAGE_RECORD_DIRECTORY, RECORD_DIRECTORY
from booley.flows.sim.record_reports import simulation_booley_dir, target_report_directory
from booley.flows.sim.staged_input_ledger import sweep_cancelled_run_inputs
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.endpoint_execution import (
    note_final_work_item_oom_kill,
    work_item_attempt_requeues,
)
from booley.runtime.supervised_execution import current_supervised_execution
from booley.targets.domain import TargetHandle

from .capacity import SimulationRecordCapacity, SimulationRecordCapacityError
from .child_protocol import recover_interrupted_attempts
from .codec import (
    SimulationRecordIntegrityError,
    reads_simulation_record_documents,
)
from .facts import ACCEPTANCE_FACTS_SCHEMA, AcceptanceFacts
from .flow_claim import FlowClaim, WorkItemRequest
from .model import (
    AssertionObservation,
    ExecutionObservation,
    FailureClass,
    FunctionalObservation,
    SimulationRecordManifest,
    SimulationRecordPlan,
    SimulationResult,
)
from .planning import manifest_digest, verify_workload
from .pre_sim_evidence import PreSimFiring, PreSimFiringDecoder
from .resume import ValidatedResumeManifest
from .run_directory import (
    expand_run_directory,
    literal_run_path,
    require_literal_run_directory,
)
from .scheduler import BoundedSimulationRecordScheduler, ScheduledAttempt, work_item_label
from .store import SimulationRecordRecovery, SimulationRecordStore, collect_pre_sim_firings
from .test_run_binding import observation_test_runs


@dataclass(frozen=True, slots=True)
class SimulationRecordPolicy:
    timeout_seconds: float | None = None
    build_timeout_seconds: float | None = None
    no_kill: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("record timeout must be positive")
        if self.build_timeout_seconds is not None and self.build_timeout_seconds <= 0:
            raise ValueError("record build timeout must be positive")


def simulation_options_from_policy(
    policy: SimulationRecordPolicy,
    *,
    trace: bool,
) -> SimulationOptions:
    """Translate one invocation policy into leaf Simulation options."""
    return SimulationOptions(
        trace=trace,
        timeout_ms=round(policy.timeout_seconds * 1000)
        if policy.timeout_seconds is not None
        else None,
        build_timeout_ms=round(policy.build_timeout_seconds * 1000)
        if policy.build_timeout_seconds is not None
        else None,
    )


class SimulationRecordCancellationError(RuntimeError):
    """The borrowed endpoint admission requested orderly record shutdown."""


@dataclass(frozen=True, slots=True)
class SimulationRecordPublicationFailure:
    """Facts the coordinator already holds when publication fails after a Simulation result."""

    target_root: Path  # holds the public coverage.json reference
    target_selector: str
    observations: tuple[Mapping[str, object], ...]
    nested_coverage_record: Path | None = None  # coverage workloads only
    coverage_reference: CoverageRecordReference | None = None  # only once published


class SimulationRecordPublicationError(RuntimeError):
    """A publication failed after a Simulation result became available."""

    def __init__(self, message: str, failure: SimulationRecordPublicationFailure) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class NewSimulationRecordPreviewRequest:
    plan: SimulationRecordPlan
    project_root: Path
    report_root: Path | None
    policy: SimulationRecordPolicy
    # An existing Simulation Record this run continues.
    record_directory: Path | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class NewSimulationRecordRunRequest(NewSimulationRecordPreviewRequest):
    invocation_directory: Path
    admission: AdmissionContext
    pre_sim_firing_published: Callable[[tuple[str, str, str, int]], None] | None = None


@dataclass(frozen=True, slots=True)
class NewSimulationRecordPreview:
    target: Mapping[str, object]
    normalized_selection: tuple[Mapping[str, object], ...]
    workload: Mapping[str, object]
    required_suite: Mapping[str, object]
    build_variants: tuple[object, ...]
    planning_disclosures: tuple[object, ...]
    prerequisites: tuple[object, ...]
    work_items: tuple[object, ...]
    fingerprints: Mapping[str, object]
    effective_policy: SimulationRecordPolicy
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SimulationRecordRecoveryStatus:
    """Caller-facing observation of authenticated durable record state."""

    manifest_path: Path
    manifest_sha256: str
    completed: tuple[str, ...]
    interrupted: tuple[str, ...]
    pending: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkExecutionRequest:
    store: SimulationRecordStore
    manifest: SimulationRecordManifest
    work_item: Mapping[str, object]
    attempt_id: str
    attempt_ordinal: int
    attempt_directory: Path
    producer_invocation_id: int
    policy: SimulationRecordPolicy
    admission: AdmissionContext
    project_root: Path
    target_handle: TargetHandle | None = None
    child_execution_id: str | None = None
    pre_sim_firing_published: Callable[[tuple[str, str, str, int]], None] | None = None
    #: Called by the executor once the item's shared build is ready (built, reused or recovered).
    build_settled: Callable[[], None] | None = None
    pre_sim_decoder: PreSimFiringDecoder = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pre_sim_decoder", PreSimFiringDecoder(self.manifest))


class SerialWorkExecutor(Protocol):
    """Private execution port; implementations publish attempt/build evidence."""

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        """Return a validated result after publishing ``attempt.json`` and evidence."""
        ...

    def prepare_attempt(self, request: WorkExecutionRequest) -> None:
        """Publish one exact child-bound attempt before queue submission."""
        ...


@dataclass(frozen=True, slots=True)
class SimulationRecordOutcome:
    manifest_path: Path
    summary_path: Path
    target: Mapping[str, object]
    observations: tuple[Mapping[str, object], ...]
    aggregate_grade: str
    complete: bool
    coverage_reference: Mapping[str, object] | None
    acceptance_facts: AcceptanceFacts
    acceptance_ready: bool
    recovery: SimulationRecordRecoveryStatus
    diagnostics: tuple[str, ...] = ()
    pre_sim_firings: tuple[PreSimFiring, ...] = ()
    current_pre_sim_keys: frozenset[tuple[str, str, str, int]] = frozenset()
    producer_invocation_directory: Path | None = None


def _coverage_attempt_directory(
    store: SimulationRecordStore, work_item_id: str, result: SimulationResult
) -> Path:
    attempt = store.latest_attempt(work_item_id)
    result_document = result.document
    if attempt is None or attempt.document["attempt_id"] != result_document["attempt_id"]:
        raise SimulationRecordIntegrityError(
            "coverage aggregate result has no matching Simulation Attempt"
        )
    directory = store.work_item_directory(work_item_id) / "attempts"
    return directory / f"{result_document['attempt_ordinal']:04d}-{result_document['attempt_id']}"


class SimulationRecord:
    """Plan/preview/run one Target's durable serial record."""

    def __init__(
        self,
        executor: SerialWorkExecutor | None = None,
        *,
        publication_checkpoint: Callable[[str], None] | None = None,
        manifest_published: Callable[[Path], None] | None = None,
        work_item_request: WorkItemRequest | None = None,
    ) -> None:
        self._manifest_published = manifest_published
        # Sizes a work item's own Memory Reservation; without it items stay under the Flow's.
        self._work_item_request = work_item_request
        self._flow_claim: FlowClaim | None = None
        self._executor = executor
        self._publication_checkpoint = publication_checkpoint or (lambda _boundary: None)
        self._publication_gate = threading.Lock()

    def preview(self, request: NewSimulationRecordPreviewRequest) -> NewSimulationRecordPreview:
        return _new_preview(request)

    def inspect_resume(self, validated: ValidatedResumeManifest) -> SimulationRecordRecoveryStatus:
        """Return an observational recovery status for one validated manifest."""
        _recovery, status = self._inspect_resume(validated)
        return status

    @staticmethod
    def _inspect_resume(
        validated: ValidatedResumeManifest,
    ) -> tuple[SimulationRecordRecovery, SimulationRecordRecoveryStatus]:
        store = SimulationRecordStore(validated.path.parent, booley_dir=validated.booley_dir)
        recovery = store.scan(validated.manifest, validated.sha256)
        return recovery, _recovery_status(validated.path, validated.sha256, recovery)

    def preflight(self, request: NewSimulationRecordPreviewRequest) -> None:
        """Fail before any publication or build when a literal run_cwd is unusable."""
        continued = _continued_store(request)
        if continued is None:
            _preflight_run_directory(request.plan.manifest, request.project_root)
            return
        # Identity first: a changed workload is reported before a run_cwd problem.
        stored = _stored_manifest(continued, request.plan.manifest)
        self._preflight_incomplete(stored, continued.scan(stored), request)

    def run(self, request: NewSimulationRecordRunRequest) -> SimulationRecordOutcome:
        if self._executor is None:
            raise RuntimeError("SimulationRecord requires a serial work executor")
        current_keys: set[tuple[str, str, str, int]] = set()
        observer = request.pre_sim_firing_published

        def record_firing(key: tuple[str, str, str, int]) -> None:
            current_keys.add(key)
            if observer is not None:
                observer(key)

        request = replace(request, pre_sim_firing_published=record_firing)
        store, manifest = self._open_simulation_record(request)
        with store.mutation_lock():
            outcome = self._run_locked(store, manifest, request)
        return replace(
            outcome,
            producer_invocation_directory=request.invocation_directory.absolute(),
            current_pre_sim_keys=frozenset(current_keys),
        )

    def _open_simulation_record(
        self, request: NewSimulationRecordRunRequest
    ) -> tuple[SimulationRecordStore, SimulationRecordManifest]:
        store = _new_store(request)
        manifest = _continued_manifest(store, request)
        if manifest is None:
            manifest = request.plan.manifest
            self._publish_manifest(store, manifest)
        return store, manifest

    def _run_locked(
        self,
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        request: NewSimulationRecordRunRequest,
    ) -> SimulationRecordOutcome:
        self._raise_if_cancelled(request)
        recovery = store.scan(manifest)
        self._run_pending(store, manifest, recovery, request)
        self._raise_if_cancelled(request)
        target = cast(Mapping[str, str], manifest.document["target"])
        coverage = cast(Mapping[str, object], manifest.document["workload"])["coverage"] is True
        failure: SimulationRecordPublicationFailure | None = None
        reference: CoverageRecordReference | None = None
        try:
            if coverage:
                # The one scan that feeds both the reference and any publication failure.
                work_item_id, result = _committed_coverage_result(store, manifest)
                failure = SimulationRecordPublicationFailure(
                    store.root.parent,
                    target["selector"],
                    cast(tuple[Mapping[str, object], ...], result.document["observations"]),
                )
                nested = (
                    _coverage_attempt_directory(store, work_item_id, result)
                    / f"{COVERAGE_RECORD_DIRECTORY}/coverage.json"
                )
                failure = replace(failure, nested_coverage_record=nested)
            self._publication_checkpoint("before:summary_replace")
            summary = store.regenerate_summary(manifest)
            self._publication_checkpoint("after:summary_replace")
            if failure is not None:
                reference = self._publish_coverage_reference(
                    store, manifest, work_item_id, result, nested
                )
                self._publication_checkpoint("after:coverage_reference")
        except (OSError, ValueError, RuntimeError) as exc:
            if failure is None:
                raise
            raise SimulationRecordPublicationError(
                str(exc), replace(failure, coverage_reference=reference)
            ) from exc
        final = store.scan(manifest)
        return _outcome(store, manifest, summary, final, reference)

    @staticmethod
    def _raise_if_cancelled(request: NewSimulationRecordRunRequest) -> None:
        if request.admission.cancellation():
            cause = request.admission.cancellation_cause() or "cancellation requested"
            raise SimulationRecordCancellationError(f"Simulation Record cancelled: {cause}")

    def _publish_coverage_reference(
        self,
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        work_item_id: str,
        result: SimulationResult,
        nested_path: Path,
    ) -> CoverageRecordReference:
        result_document = result.document
        target = cast(Mapping[str, str], manifest.document["target"])
        origin = cast(Mapping[str, object], manifest.document["origin"])
        try:
            reference = build_coverage_record_reference(
                simulation_record_id=cast(str, manifest.document["simulation_record_id"]),
                simulation_manifest_sha256=manifest_digest(manifest),
                target_identity=f"{target['vlnv']}#{target['name']}",
                target_selector=target["selector"],
                origin_invocation_id=cast(int, origin["invocation_id"]),
                producer_invocation_id=cast(int, result_document["producer_invocation_id"]),
                simulation_work_item_id=work_item_id,
                simulation_attempt_id=cast(str, result_document["attempt_id"]),
                origin_target_directory=store.root.parent,
                coverage_record_path=nested_path,
            )
        except (CoverageRecordStoreError, CoverageRecordValidationError) as exc:
            raise SimulationRecordIntegrityError(
                "nested Coverage Record cannot be authenticated"
            ) from exc
        self._publication_checkpoint("before:coverage_reference")
        return publish_coverage_record_reference(store.root.parent / "coverage.json", reference)

    def _expect_builds(
        self,
        admission: AdmissionContext,
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        items: Iterable[Mapping[str, object]],
    ) -> None:
        """Tell the Flow's claim which shared builds *items* still need (memory admission only)."""
        if self._flow_claim is None and (
            admission.memory_sizing is not None and self._work_item_request is not None
        ):
            self._flow_claim = FlowClaim(
                admission, admission.memory_sizing, self._work_item_request
            )
        if self._flow_claim is not None:
            self._flow_claim.expect(store.root, manifest, items)

    def publish_new(self, request: NewSimulationRecordRunRequest) -> Path:
        """Atomically publish one planned manifest without starting work."""
        store = _new_store(request)
        manifest = request.plan.manifest
        self._expect_builds(
            request.admission,
            store,
            manifest,
            cast(tuple[Mapping[str, object], ...], manifest.document["work_items"]),
        )
        if _continued_manifest(store, request) is not None:
            if self._manifest_published is not None:
                self._manifest_published(store.manifest_path)
            return store.manifest_path
        self._publish_manifest(store, request.plan.manifest)
        return store.manifest_path

    def _publish_manifest(
        self, store: SimulationRecordStore, manifest: SimulationRecordManifest
    ) -> None:
        self._publication_checkpoint("before:manifest_commit")
        store.publish_manifest(manifest)
        if self._manifest_published is not None:
            self._manifest_published(store.manifest_path)
        self._publication_checkpoint("after:manifest_commit")

    @staticmethod
    def _preflight_incomplete(
        manifest: SimulationRecordManifest,
        recovery: SimulationRecordRecovery,
        request: NewSimulationRecordPreviewRequest,
    ) -> None:
        """Preflight one record's run_cwd when it still has work to run."""
        if all(item.state == "complete" for item in recovery.items):
            return
        _preflight_run_directory(manifest, request.project_root)

    def _run_pending(
        self,
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        recovery: SimulationRecordRecovery,
        request: NewSimulationRecordRunRequest,
    ) -> None:
        project_root = request.project_root
        project_data = resolve_checkout_booley_dir(project_root)
        interrupted = {
            work_item_id: attempt.document
            for work_item_id in recovery.interrupted
            if (attempt := store.latest_attempt(work_item_id)) is not None
        }
        recover_interrupted_attempts(manifest, interrupted, project_data, booley_dir=project_data)
        sweep_cancelled_run_inputs(project_data)
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        by_id = {cast(str, item["work_item_id"]): item for item in items}
        invocation = _invocation_number(request.invocation_directory)
        pending = [
            by_id[recovered.work_item_id]
            for recovered in recovery.items
            if recovered.state != "complete"
        ]
        if pending:
            _preflight_run_directory(manifest, project_root)
        self._raise_if_cancelled(request)
        self._expect_builds(request.admission, store, manifest, pending)
        flow_claim = self._flow_claim
        capacity = SimulationRecordCapacity(
            request.admission,
            manifest,
            project_data,
            None
            if flow_claim is None
            else lambda item: flow_claim.item_claim(store.root, manifest, item),
        )
        self._scheduler(capacity, store, manifest, project_root, invocation, request).run(pending)
        self._raise_if_cancelled(request)

    def _scheduler(
        self, capacity, store, manifest, project_root, invocation, request
    ) -> BoundedSimulationRecordScheduler:
        return BoundedSimulationRecordScheduler(
            capacity,
            allocate=lambda item: self._allocate_scheduled(store, manifest, project_root, item),
            execute=lambda attempt, child_id: self._execute_scheduled(
                store, manifest, attempt, invocation, request, child_id
            ),
            prepare_child=lambda attempt, child_id: self._prepare_child(
                store, manifest, attempt, invocation, request, child_id
            ),
        )

    def _prepare_child(
        self,
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        attempt: ScheduledAttempt,
        invocation: int,
        request: NewSimulationRecordRunRequest,
        child_execution_id: str,
    ) -> None:
        executor = self._executor
        if executor is None or not hasattr(executor, "prepare_attempt"):
            raise SimulationRecordIntegrityError(
                "record executor cannot publish a child attempt before admission"
            )
        executor.prepare_attempt(
            self._work_execution_request(
                store,
                manifest,
                attempt,
                invocation,
                request,
                child_execution_id,
            )
        )

    @staticmethod
    def _allocate_scheduled(
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        project_root: Path,
        work_item: Mapping[str, object],
    ) -> ScheduledAttempt:
        work_item_id = cast(str, work_item["work_item_id"])
        attempt_id = _new_uuid()
        ordinal, directory = store.allocate_attempt_directory(work_item_id, attempt_id)
        collision_key = _scheduled_collision_key(
            store, manifest, project_root, work_item_id, attempt_id, ordinal
        )
        return ScheduledAttempt(work_item, attempt_id, ordinal, directory, collision_key)

    def _execute_item(
        self,
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        attempt: ScheduledAttempt,
        invocation: int,
        request: NewSimulationRecordRunRequest,
        child_execution_id: str | None,
    ):
        """Execute one attempt; a killed final attempt is named even when its execution fails."""
        assert self._executor is not None
        try:
            return self._executor.execute(
                self._work_execution_request(
                    store, manifest, attempt, invocation, request, child_execution_id
                )
            )
        except (PreSimScopeStoppedError, SimulationBuildLeaseStoppedError) as signal:
            raise SimulationRecordCapacityError(str(signal)) from signal
        except BaseException:
            note_final_work_item_oom_kill(work_item_label(attempt.item), passed=False, raised=True)
            raise

    def _execute_scheduled(
        self,
        store: SimulationRecordStore,
        manifest: SimulationRecordManifest,
        attempt: ScheduledAttempt,
        invocation: int,
        request: NewSimulationRecordRunRequest,
        child_execution_id: str | None,
    ) -> bool:
        """Run one attempt and publish its result; ``True`` when it was OOM-killed instead.

        A work item attempt that was OOM-killed and whose result does not
        survive the kill publishes no result and does not settle its build: the
        scheduler runs the item again, alone, as a new attempt, and only the
        final attempt's result counts.
        """
        work_item = attempt.item
        work_item_id = cast(str, work_item["work_item_id"])
        result = self._execute_item(
            store, manifest, attempt, invocation, request, child_execution_id
        )
        passed = result.document["grade"] == "pass"
        if work_item_attempt_requeues(passed=passed):
            raise_if_cancelled_before_publication()  # a cancelled attempt is never requeued
            return True
        if self._flow_claim is not None:
            # The item's shared build is settled; the last one frees the Flow's reservation.
            self._flow_claim.settle(store.root, work_item)
            self._flow_claim.release(store.root)
        raise_if_cancelled_before_publication()
        note_final_work_item_oom_kill(work_item_label(work_item), passed=passed)
        try:
            self._publish_scheduled_result(store, work_item_id, result)
        except (OSError, ValueError, RuntimeError) as exc:
            target = cast(Mapping[str, str], manifest.document["target"])
            coverage = cast(Mapping[str, object], manifest.document["workload"])["coverage"]
            failure = SimulationRecordPublicationFailure(
                store.root.parent,
                target["selector"],
                cast(tuple[Mapping[str, object], ...], result.document["observations"]),
                attempt.directory / f"{COVERAGE_RECORD_DIRECTORY}/coverage.json"
                if coverage is True
                else None,
            )
            raise SimulationRecordPublicationError(str(exc), failure) from exc
        return False

    def _build_settled(
        self, store: SimulationRecordStore, attempt: ScheduledAttempt
    ) -> Callable[[], None]:
        """What the executor does when the item's shared build is ready.

        The Flow's claim learns the build is settled, then the scheduler may let the
        item take a claim of its own.
        """

        def settled() -> None:
            if self._flow_claim is not None:
                self._flow_claim.settle(store.root, attempt.item)
            if attempt.build_settled is not None:
                attempt.build_settled()

        return settled

    def _work_execution_request(
        self,
        store,
        manifest,
        attempt,
        invocation,
        request,
        child_execution_id,
    ) -> WorkExecutionRequest:
        return WorkExecutionRequest(
            store,
            manifest,
            attempt.item,
            attempt.attempt_id,
            attempt.ordinal,
            attempt.directory,
            invocation,
            request.policy,
            request.admission,
            request.project_root,
            child_execution_id=child_execution_id,
            pre_sim_firing_published=request.pre_sim_firing_published,
            build_settled=self._build_settled(store, attempt),
        )

    def _publish_scheduled_result(
        self,
        store: SimulationRecordStore,
        work_item_id: str,
        result: SimulationResult,
    ) -> None:
        with self._publication_gate:
            self._publication_checkpoint("before:simulation_result")
            store.publish_result(work_item_id, result)
            self._publication_checkpoint("after:simulation_result")


def raise_if_cancelled_before_publication() -> None:
    """Refuse to publish a result produced under a cancelled supervised scope."""
    scope = current_supervised_execution()
    if scope is not None and scope.cancelled():
        raise SimulationRecordCapacityError(
            f"execution cancelled before result publication: {scope.describe_cancellation()}"
        )


def _invocation_number(directory: Path) -> int:
    try:
        return int(directory.name)
    except ValueError as exc:
        raise SimulationRecordIntegrityError(
            "record invocation directory must end in a numeric id"
        ) from exc


def _committed_coverage_result(
    store: SimulationRecordStore,
    manifest: SimulationRecordManifest,
) -> tuple[str, SimulationResult]:
    recovery = store.scan(manifest)
    completed = [item for item in recovery.items if item.result is not None]
    if len(completed) != 1:
        raise SimulationRecordIntegrityError(
            "coverage aggregate must have one committed Simulation result"
        )
    result = completed[0].result
    assert result is not None
    return completed[0].work_item_id, result


def _scheduled_collision_key(
    store: SimulationRecordStore,
    manifest: SimulationRecordManifest,
    project_root: Path,
    work_item_id: str,
    attempt_id: str,
    ordinal: int,
) -> str:
    workload = cast(Mapping[str, object], manifest.document["workload"])
    run_cwd = cast(Mapping[str, object], workload["run_cwd"])
    target = cast(Mapping[str, str], manifest.document["target"])
    run = expand_run_directory(
        cast(str, run_cwd["configured"]),
        project_root=project_root,
        simulation_record_id=cast(str, manifest.document["simulation_record_id"]),
        target_key=target["selector"].replace("/", "%2F"),
        work_item_key=store.work_item_directory(work_item_id).name,
        attempt_key=f"{ordinal:04d}-{attempt_id}",
    )
    return run.collision_key


def _new_preview(request: NewSimulationRecordPreviewRequest) -> NewSimulationRecordPreview:
    document = request.plan.manifest.document
    return NewSimulationRecordPreview(
        cast(Mapping[str, object], document["target"]),
        tuple(
            cast(Mapping[str, object], item["selection"])
            for item in cast(tuple[Mapping[str, object], ...], document["work_items"])
        ),
        cast(Mapping[str, object], document["workload"]),
        cast(Mapping[str, object], document["required_suite"]),
        cast(tuple[object, ...], document["build_variants"]),
        cast(tuple[object, ...], document["planning_disclosures"]),
        cast(tuple[object, ...], document["prerequisites"]),
        cast(tuple[object, ...], document["work_items"]),
        cast(Mapping[str, object], document["fingerprints"]),
        request.policy,
    )


def _continued_store(request: NewSimulationRecordPreviewRequest) -> SimulationRecordStore | None:
    if request.record_directory is None:
        return None
    return SimulationRecordStore(
        request.record_directory, booley_dir=simulation_booley_dir(request.project_root)
    )


def _stored_manifest(
    store: SimulationRecordStore, planned: SimulationRecordManifest
) -> SimulationRecordManifest:
    """The record's own manifest, once *planned* has the same workload identity."""
    stored = store.load_manifest()
    verify_workload(stored, planned)
    return stored


def _continued_manifest(
    store: SimulationRecordStore, request: NewSimulationRecordRunRequest
) -> SimulationRecordManifest | None:
    """The stored manifest this run continues; None when it publishes its own.

    A named ``record_directory`` never gets a new manifest: a missing one is refused.
    """
    if request.record_directory is None and not store.manifest_path.exists():
        return None
    return _stored_manifest(store, request.plan.manifest)


def _new_store(request: NewSimulationRecordRunRequest) -> SimulationRecordStore:
    if (continued := _continued_store(request)) is not None:
        return continued
    target = cast(Mapping[str, str], request.plan.manifest.document["target"])
    selector = target["selector"]
    if target["role"] == "cycle_count_baseline":
        selector = f"{selector}@baseline-{target['revision'][:12]}"
    directory = target_report_directory(request.invocation_directory, selector) / RECORD_DIRECTORY
    return SimulationRecordStore(directory, booley_dir=simulation_booley_dir(request.project_root))


def _outcome(
    store: SimulationRecordStore,
    manifest: SimulationRecordManifest,
    summary: Mapping[str, object],
    recovery: SimulationRecordRecovery,
    coverage_reference: CoverageRecordReference | None = None,
) -> SimulationRecordOutcome:
    facts, observations = _acceptance_facts(store, manifest, recovery, coverage_reference)
    complete = cast(bool, summary["complete"])
    grade = cast(str, summary["aggregate_grade"])
    return SimulationRecordOutcome(
        store.manifest_path,
        store.summary_path,
        cast(Mapping[str, object], manifest.document["target"]),
        observations,
        grade,
        complete,
        coverage_reference.document if coverage_reference is not None else None,
        facts,
        _acceptance_ready(manifest, observations, complete, grade),
        _recovery_status(store.manifest_path, manifest_digest(manifest), recovery),
        pre_sim_firings=collect_pre_sim_firings(store.manifest_path, booley_dir=store.booley_dir),
    )


def _recovery_status(
    manifest_path: Path,
    manifest_sha256: str,
    recovery: SimulationRecordRecovery,
) -> SimulationRecordRecoveryStatus:
    return SimulationRecordRecoveryStatus(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        completed=recovery.complete,
        interrupted=recovery.interrupted,
        pending=recovery.pending,
    )


def _preflight_run_directory(manifest: SimulationRecordManifest, project_root: Path) -> None:
    """Require a literal run_cwd to exist before any attempt, build, or launch."""
    workload = cast(Mapping[str, object], manifest.document["workload"])
    run_cwd = cast(Mapping[str, object], workload["run_cwd"])
    if run_cwd["kind"] != "literal":
        return
    require_literal_run_directory(
        literal_run_path(cast(str, run_cwd["configured"]), project_root=project_root)
    )


def _acceptance_ready(
    manifest: SimulationRecordManifest,
    observations: tuple[Mapping[str, object], ...],
    complete: bool,
    grade: str,
) -> bool:
    if not complete or grade != "pass":
        return False
    suite = cast(Mapping[str, object], manifest.document["required_suite"])
    passing = {
        item["test"]
        for item in observations
        if item["execution"] == "completed"
        and item["functional"] == "pass"
        and item["assertions"] != "dirty"
    }
    if suite["default_invocation"] is True:
        return len(observations) == 1 and bool(passing)
    return set(cast(tuple[str, ...], suite["names"])).issubset(passing)


def _acceptance_facts(
    store: SimulationRecordStore,
    manifest: SimulationRecordManifest,
    recovery: SimulationRecordRecovery,
    coverage_reference: CoverageRecordReference | None = None,
) -> tuple[AcceptanceFacts, tuple[Mapping[str, object], ...]]:
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    by_id = {item["work_item_id"]: item for item in items}
    consumed, observations = collected_result_facts(store, recovery, by_id)
    suite = cast(Mapping[str, object], manifest.document["required_suite"])
    facts = AcceptanceFacts(
        {
            "$schema": ACCEPTANCE_FACTS_SCHEMA,
            "campaign_id": manifest.document["simulation_record_id"],
            "manifest_sha256": manifest_digest(manifest),
            "origin": manifest.document["origin"],
            "target": manifest.document["target"],
            "required_suite": {
                "names": suite["names"],
                "default_invocation": suite["default_invocation"],
                "source_sha256": suite["source_sha256"],
            },
            "prerequisites": [],
            "consumed_results": consumed,
            "observations": observations,
            "coverage_reference": _coverage_acceptance_reference(
                store, manifest, coverage_reference
            ),
        }
    )
    return facts, tuple(observations)


def _coverage_acceptance_reference(
    store: SimulationRecordStore,
    manifest: SimulationRecordManifest,
    reference: CoverageRecordReference | None,
) -> Mapping[str, object] | None:
    if reference is None:
        return None
    raw = encode_coverage_record_reference(reference)
    path = store.root.parent / "coverage.json"
    invocation = store.root.parents[2]
    return {
        "reference": {
            "path_base": "origin_invocation",
            "path": path.relative_to(invocation).as_posix(),
            "bytes": len(raw),
            "sha256": content_digest(raw),
            "kind": "coverage_campaign_reference",
            "owner": manifest.document["simulation_record_id"],
        },
        "document": json.loads(raw),
    }


def collected_result_facts(
    store: SimulationRecordStore,
    recovery: SimulationRecordRecovery,
    by_id: Mapping[object, Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """The consumed-result and observation facts of every terminal result in *recovery*.

    A work item without a result contributes nothing, so the facts of an
    interrupted record are those of the work that finished.
    """
    consumed: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    for recovered in recovery.items:
        if recovered.result is None:
            continue
        item = by_id[recovered.work_item_id]
        result_path = store.work_item_directory(recovered.work_item_id) / "result.json"
        raw = result_path.read_bytes()
        digest = content_digest(raw)
        invocation = store.root.parents[2]
        relative = result_path.relative_to(invocation).as_posix()
        document = recovered.result.document
        reference = {
            "path_base": "origin_invocation",
            "path": relative,
            "bytes": len(raw),
            "sha256": digest,
            "kind": "simulation_result",
            "owner": document["attempt_id"],
        }
        consumed.append(
            {
                "work_item_id": recovered.work_item_id,
                "role": item["role"],
                "revision": item["revision"],
                "target": item["target"],
                "attempt_id": document["attempt_id"],
                "result": reference,
                "finished_at": document["finished_at"],
            }
        )
        results = result_observations(recovered.result)
        observations.extend(_observation_facts(recovered.work_item_id, item, digest, results))
    return consumed, observations


_OBSERVATION_MEMBERS = frozenset(
    {
        "test_run",
        "test",
        "execution",
        "failure_class",
        "functional",
        "assertions",
        "assertion_count",
        "detail",
        "cycle_count",
    }
)


@reads_simulation_record_documents
def result_observations(result: SimulationResult) -> tuple[Mapping[str, object], ...]:
    """The observations of a stored *result*, each checked for the members facts read.

    The result decoder checks only the document boundary (#1744), so the
    enum values, test name and cycle count that feed acceptance are checked
    here. A bad value raises the record integrity error.
    """
    observations = cast(tuple[Mapping[str, object], ...], result.document["observations"])
    if not isinstance(observations, tuple):
        raise TypeError("result observations must be a list")
    for item in observations:
        # Facts spread each observation after the identity members, so an extra key would
        # override them.
        if set(item) != _OBSERVATION_MEMBERS:
            raise ValueError("observation must have exactly the recorded members")
        ExecutionObservation(item["execution"])
        FunctionalObservation(item["functional"])
        AssertionObservation(item["assertions"])
        if item["failure_class"] is not None:
            FailureClass(item["failure_class"])
        if not isinstance(item["test"], str | None):
            raise TypeError("observation test must be a string or null")
        cycle_count = item["cycle_count"]
        if cycle_count is not None and (type(cycle_count) is not int or cycle_count < 0):
            raise ValueError("observation cycle count must be a nonnegative integer or null")
    return observations


def _observation_facts(
    work_item_id: str,
    item: Mapping[str, object],
    result_sha256: str,
    results: tuple[Mapping[str, object], ...],
) -> list[dict[str, object]]:
    """The facts of one result's observations, each naming the Test Run it observed."""
    bound = observation_test_runs(item, results)
    facts: list[dict[str, object]] = []
    for index, observation in enumerate(results):
        fact = {
            "work_item_id": work_item_id,
            "role": item["role"],
            "revision": item["revision"],
            "target": item["target"],
            "result_sha256": result_sha256,
            **dict(observation),
        }
        fact |= {"test_run": bound[index][0], "test_run_fingerprint": bound[index][1]}
        facts.append(fact)
    return facts


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


__all__ = [
    "NewSimulationRecordPreview",
    "NewSimulationRecordPreviewRequest",
    "NewSimulationRecordRunRequest",
    "SerialWorkExecutor",
    "SimulationRecord",
    "SimulationRecordOutcome",
    "SimulationRecordPolicy",
    "WorkExecutionRequest",
]
