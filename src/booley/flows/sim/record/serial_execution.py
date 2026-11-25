"""Production bridge from durable record work to one legacy HDL execution.

This Phase-2 adapter deliberately preserves the existing leaf execution path.
It adds the manifest-first attempt protocol around one ordinary-HDL work item
and captures the engine-authorized private image as attempt-owned evidence.
"""

from __future__ import annotations

import functools
import json
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.core.boundary import BoundaryError
from booley.flows.artifact_durability import (
    durable_copy,
    durable_create,
    durable_directory,
    durable_in_place,
    fsync_directory,
)
from booley.flows.base import DEFAULT_TIMEOUT_S
from booley.flows.job_resources import judged_commands_failing_on_error
from booley.flows.sim.backends.cocotb_results import decode_results_payload, reconcile
from booley.flows.sim.build import BuildOutcome
from booley.flows.sim.build_session import project_compile_surface
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.execution.contract import (
    PreSimEvidence,
    PreSimScopeStoppedError,
    SimulationArtifactEvidence,
    SimulationInfrastructureFailure,
    SimulationOptions,
    SimulationTargetOutcome,
    SimulationTestOutcome,
    pre_sim_failure_message,
)
from booley.flows.sim.execution.engine import (
    ProcessInvoker,
    SimulationExecution,
    simulation_target_environment,
)
from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.flows.sim.live_progress import attempt_scope, end_phase
from booley.flows.sim.record.bundle import verify_bundle_in_place
from booley.flows.sim.record_files import document_digest
from booley.flows.sim.runtime_inputs import (
    RuntimeInputBinding,
    RuntimeInputError,
    materialize_simulation_record_runtime_inputs,
    safe_runtime_relative,
)
from booley.flows.test_runs import TestRun
from booley.runtime.endpoint_execution import work_item_attempt_requeues
from booley.runtime.filesystem_utils import make_directories
from booley.runtime.supervised_execution import current_supervised_execution
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle

from ..build import build_output_tail
from .codec import (
    MAX_COMMAND_BYTES,
    MAX_DETAIL_BYTES,
    RECORD_MAX_BYTES,
    RESULT_SCHEMA,
    SimulationRecordIntegrityError,
    canonical_json_bytes,
    encode_bundle_build_attempt,
    encode_bundle_build_result,
    encode_simulator_bundle,
    reads_simulation_record_documents,
)
from .coordinator import SerialWorkExecutor, WorkExecutionRequest, simulation_options_from_policy
from .model import (
    AssertionObservation,
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutionObservation,
    FailureClass,
    FrozenJson,
    FunctionalObservation,
    SimulationAttempt,
    SimulationResult,
    SimulatorBundle,
    frozen_pre_sim_commands,
    frozen_resource_flags,
    grade_observations,
)
from .planning import manifest_digest
from .pre_sim_evidence import publish_pre_sim_firing
from .run_directory import (
    RunDirectory,
    claimed_run_directory,
    expand_run_directory,
    require_run_directory_outside,
)
from .test_run_binding import observation_test_runs, work_item_test_runs
from .test_run_files import TRACE_WARNING_CODE

#: Where a build directory keeps its Simulator Bundle manifest.
_BUNDLE_MANIFEST_PATH = "evidence/bundle.json"
# Bundle artifacts are read-only; only the simulator executable stays executable.
_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_ARTIFACT_MODES = {
    "simulator_executable": _READ_ONLY | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    "shared_library": _READ_ONLY,
    "runtime_data": _READ_ONLY,
    "runtime_input": _READ_ONLY,
}


@dataclass(frozen=True, slots=True)
class _ReadyLaunch:
    build: tuple[BundleBuildResult, SimulatorBundle, object]
    simulation: tuple[object, tuple[str, ...], SimulationOptions]
    policy: tuple[Mapping[str, object], str, RunDirectory, float]


@dataclass(frozen=True, slots=True)
class _SharedReady:
    directory: Path
    result: BundleBuildResult
    bundle: SimulatorBundle
    build_execution: Mapping[str, object]
    # The retained launch preparation every other lane of this build derives from.
    preparation: _OrdinaryGroup | None = None


@dataclass(frozen=True, slots=True)
class _SharedFailure:
    directory: Path
    result: BundleBuildResult
    failure_class: FailureClass
    detail: str


@dataclass(frozen=True, slots=True)
class _GroupInputs:
    handle: TargetHandle
    names: tuple[str, ...]
    #: The work item's concrete Test Runs; ``None`` for a manifest older than v4.
    test_runs: tuple[TestRun, ...] | None
    options: SimulationOptions
    execution: SimulationExecution
    workload: Mapping[str, object]
    access: str


@dataclass(frozen=True, slots=True)
class _CapturedBuild:
    """A compiled build's image inventory and compiler evidence."""

    artifacts: list[dict[str, object]]
    execution_ref: dict[str, object]
    build_execution: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BuildScope:
    """Who a build belongs to: its sharing scope and the identity on its records.

    ``sharing`` is ``"shared_variant"`` (one build serves every work item of a
    build variant within one invocation, so it has no owner) or
    ``"private_work_item"`` (one build per Simulation Attempt, owned by that
    work item and attempt).
    """

    sharing: str
    build_attempt_id: str
    ordinal: int
    owner: Mapping[str, object]

    @classmethod
    def shared(cls, build_attempt_id: str, ordinal: int) -> BuildScope:
        """Scope of a variant build shared by many work items."""
        owner = {"work_item_id": None, "simulation_attempt_id": None}
        return cls("shared_variant", build_attempt_id, ordinal, owner)

    @classmethod
    def private(cls, request: WorkExecutionRequest) -> BuildScope:
        """Scope of the build private to *request*'s work item and attempt."""
        owner = {
            "work_item_id": request.work_item["work_item_id"],
            "simulation_attempt_id": request.attempt_id,
        }
        return cls("private_work_item", request.attempt_id, 1, owner)


class _OrdinaryGroup(Protocol):
    @property
    def build_root(self) -> Path: ...
    @property
    def artifact_paths(self) -> tuple[Path, ...]: ...

    def planning_disclosure(self) -> dict[str, object]: ...
    def for_work_item(
        self, test_names: tuple[str, ...], test_runs: tuple[TestRun, ...] | None
    ) -> _OrdinaryGroup: ...
    def compile(self) -> BuildOutcome: ...
    def finish_build_failure(self) -> SimulationTargetOutcome: ...
    def build_recovery_document(self) -> dict[str, object]: ...
    def bind_authenticated_bundle(self, evidence: Mapping[str, object]) -> None: ...
    def launch_bundle(
        self, bundle_root: Path, evidence_root: Path, run_cwd: Path
    ) -> SimulationTargetOutcome: ...


class OrdinaryHdlSerialExecutor(SerialWorkExecutor):
    """Execute one ordinary-HDL item through :class:`SimulationExecution`."""

    def __init__(
        self,
        *,
        invoke: ProcessInvoker,
        execution_factory: Callable[[SimulationOptions], SimulationExecution] | None = None,
        publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._invoke = invoke
        self._execution_factory = execution_factory
        self._publication_checkpoint = publication_checkpoint or (lambda _boundary: None)
        self._shared_ready: dict[tuple[Path, str, int], _SharedReady] = {}
        self._shared_failure: dict[tuple[Path, str, int], _SharedFailure] = {}
        self._shared_locks: dict[tuple[Path, str, int], threading.Lock] = {}
        self._shared_locks_gate = threading.Lock()
        self._prepared_attempts: dict[Path, RunDirectory] = {}
        self._prepared_attempts_gate = threading.Lock()

    def prepare_attempt(self, request: WorkExecutionRequest) -> None:
        """Publish the child-bound attempt before its slot waiter becomes visible."""
        attempt, run_directory = simulation_attempt(request)
        self._publication_checkpoint("before:simulation_attempt")
        request.store.publish_attempt(request.attempt_directory, attempt)
        self._publication_checkpoint("after:simulation_attempt")
        with self._prepared_attempts_gate:
            self._prepared_attempts[request.attempt_directory] = run_directory

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        """Bind lifecycle observations to this durable work item's attempt."""
        target = cast(Mapping[str, str], request.manifest.document["target"])
        with attempt_scope(
            target["selector"],
            identity=f"{target['vlnv']}#{target['name']}",
            attempt_id=request.attempt_id,
        ):
            return self._execute_observed(request)

    def _execute_observed(self, request: WorkExecutionRequest) -> SimulationResult:
        scope = current_supervised_execution()
        if frozen_pre_sim_commands(request.manifest) and scope is not None and scope.cancelled():
            raise PreSimScopeStoppedError("execution scope stopped before Pre-Sim Commands")
        item = request.work_item
        if item["kind"] not in {"ordinary_hdl", "cocotb_batch"}:
            raise SimulationRecordIntegrityError(
                "HDL record executor supports only ordinary or Cocotb work items"
            )
        with self._prepared_attempts_gate:
            run_directory = self._prepared_attempts.pop(request.attempt_directory, None)
        if run_directory is None:
            self.prepare_attempt(request)
            with self._prepared_attempts_gate:
                run_directory = self._prepared_attempts.pop(request.attempt_directory)
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        if workload["pre_sim_build_access"] == "immutable":
            return self._execute_shared(request, run_directory)
        build_directory = request.attempt_directory / "private-build"
        durable_directory(build_directory)
        build_directory.chmod(0o700)
        build_attempt = self._publish_build_attempt(
            request, build_directory, BuildScope.private(request)
        )
        return self._execute_group(request, build_directory, build_attempt, run_directory)

    def _execute_shared(
        self, request: WorkExecutionRequest, run_directory: RunDirectory
    ) -> SimulationResult:
        """Compile one record/invocation variant and launch one isolated item."""
        key = (
            request.store.root,
            cast(str, request.work_item["build_variant_id"]),
            request.producer_invocation_id,
        )
        started = time.monotonic()
        inputs = self._group_inputs(request)
        handle = inputs.handle
        try:
            with self._shared_lock(key):
                settled = self._settle_shared(request, key, inputs, started)
                if isinstance(settled, SimulationResult):
                    return settled
                ready, launch_group = settled
        finally:
            # The shared build is settled (built, reused, recovered or failed). Pre-Sim
            # Commands are a phase of their own, so the build's estimate ends here.
            end_phase(handle.selector, "sim_build")
        # A ready build lets the item take a claim of its own before it runs Pre-Sim
        # Commands and simulates; waiting for it holds no lock of the variant.
        if request.build_settled is not None:
            request.build_settled()
        return self._launch_shared(request, run_directory, ready, launch_group, inputs, started)

    def _settle_shared(
        self, request, key, inputs, started
    ) -> SimulationResult | tuple[_SharedReady, _OrdinaryGroup | None]:
        """Settle the build under the shared lock; the one lane that configures gets its group.

        A lane that finds the retained preparation already made gets ``None``
        and derives its launch from it without the Target slot lease.
        """
        failed = self._shared_failure.get(key)
        if failed is not None:
            return _blocked_shared_result(request, failed, time.monotonic() - started)
        ready = self._recover_shared(request, key)
        recovered_failure = self._shared_failure.get(key)
        if recovered_failure is not None:
            return _blocked_shared_result(request, recovered_failure, time.monotonic() - started)
        if ready is None:
            return self._build_shared(
                request,
                key,
                inputs.handle,
                inputs.names,
                inputs.execution,
                inputs.workload,
                started,
            )
        if ready.preparation is not None:
            return ready, None
        # A recovered build is configured once here, by the first lane that needs it.
        group = self._prepare_shared_launch(
            request, ready, inputs.execution, inputs.handle, inputs.names
        )
        self._shared_ready[key] = replace(ready, preparation=group)
        return self._shared_ready[key], group

    def _launch_shared(self, request, run_directory, ready, launch_group, inputs, started):
        if launch_group is None:
            assert ready.preparation is not None
            launch_group = ready.preparation.for_work_item(
                inputs.names, work_item_test_runs(request.work_item)
            )
            _authenticate_planning_disclosure(request, launch_group)
        return self._claim_and_launch(
            request,
            run_directory,
            ready.directory,
            (ready.result, ready.bundle, launch_group),
            inputs,
            started,
        )

    def _claim_and_launch(
        self, request, run_directory, build_directory, build, inputs, started
    ) -> SimulationResult:
        """Claim the run directory and launch *build* (a ``(result, bundle, group)``) in it."""
        identity = {
            "simulation_record_id": cast(str, request.manifest.document["simulation_record_id"]),
            "work_item_id": cast(str, request.work_item["work_item_id"]),
            "attempt_id": request.attempt_id,
        }
        require_run_directory_outside(
            run_directory, request.attempt_directory / "execution-evidence"
        )
        with claimed_run_directory(run_directory, identity=identity) as run_cwd:
            return self._run_ready_group(
                request,
                build_directory,
                run_cwd,
                _ReadyLaunch(
                    build,
                    (inputs.handle, inputs.names, inputs.options),
                    (inputs.workload, inputs.access, run_directory, started),
                ),
            )

    def _shared_lock(self, key: tuple[Path, str, int]) -> threading.Lock:
        with self._shared_locks_gate:
            return self._shared_locks.setdefault(key, threading.Lock())

    def _recover_shared(self, request, key) -> _SharedReady | None:
        ready = self._shared_ready.get(key)
        if ready is not None:
            return ready
        recovered = request.store.recover_shared_build(key[1], key[2])
        if recovered is None:
            return None
        if recovered.result.document["state"] == "design_failure":
            self._shared_failure[key] = _recovered_shared_failure(
                recovered.directory, recovered.result
            )
            return None
        assert recovered.bundle is not None and recovered.build_execution is not None
        ready = _SharedReady(
            recovered.directory, recovered.result, recovered.bundle, recovered.build_execution
        )
        self._shared_ready[key] = ready
        return ready

    def _build_shared(
        self, request, key, handle, names, execution, workload, started
    ) -> tuple[_SharedReady, _OrdinaryGroup] | SimulationResult:
        build_id = str(uuid.uuid4())
        ordinal, directory = request.store.allocate_build_attempt_directory(key[1], build_id)
        attempt = self._publish_build_attempt(
            request, directory, BuildScope.shared(build_id, ordinal)
        )
        with execution.ordinary_group(
            handle, names, work_item_test_runs(request.work_item)
        ) as group:
            _authenticate_planning_disclosure(request, group)
            captured = self._compile_and_capture(request, directory, attempt, group, workload)
            if captured is None:
                return self._shared_build_failure(request, key, directory, attempt, group, started)
        result, bundle = self._publish_ready(
            request, directory, attempt, captured.artifacts, captured.execution_ref, started
        )
        ready = _SharedReady(directory, result, bundle, captured.build_execution, group)
        self._shared_ready[key] = ready
        return ready, group

    def _publish_build_attempt(
        self, request: WorkExecutionRequest, directory: Path, scope: BuildScope
    ) -> BundleBuildAttempt:
        """Publish the build attempt of *scope* between its publication checkpoints."""
        attempt = bundle_build_attempt(request, scope)
        self._publication_checkpoint("before:build_attempt")
        request.store.publish_build_attempt(directory, attempt)
        self._publication_checkpoint("after:build_attempt")
        return attempt

    def _compile_and_capture(
        self,
        request: WorkExecutionRequest,
        directory: Path,
        attempt: BundleBuildAttempt,
        group: _OrdinaryGroup,
        workload: Mapping[str, object],
    ) -> _CapturedBuild | None:
        """Compile *group* and capture its evidence; ``None`` when the compile failed."""
        if not group.compile().passed:
            return None
        build_execution = group.build_recovery_document()
        execution_ref = _capture_build_execution(
            directory, cast(str, attempt.document["build_attempt_id"]), build_execution
        )
        self._publication_checkpoint("before:bundle_evidence")
        artifacts = capture_private_image(
            group.artifact_paths,
            group.build_root,
            directory,
            cast(tuple[Mapping[str, str], ...], workload["runtime_inputs"]),
        )
        self._publication_checkpoint("after:bundle_evidence")
        return _CapturedBuild(artifacts, execution_ref, build_execution)

    def _publish_ready(
        self,
        request: WorkExecutionRequest,
        directory: Path,
        attempt: BundleBuildAttempt,
        artifacts: list[dict[str, object]],
        execution_ref: dict[str, object],
        started: float,
    ) -> tuple[BundleBuildResult, SimulatorBundle]:
        """Publish the ready build result between its publication checkpoints."""
        elapsed = time.monotonic() - started
        self._publication_checkpoint("before:build_result")
        published = publish_ready_build_result(
            request, directory, attempt, artifacts, execution_ref, elapsed
        )
        self._publication_checkpoint("after:build_result")
        return published

    def _shared_build_failure(
        self, request, key, directory, attempt, group, started
    ) -> SimulationResult:
        outcome = group.finish_build_failure()
        infrastructure = outcome.infrastructure_failure is not None
        result = publish_failed_build_result(
            request,
            directory,
            attempt,
            outcome,
            time.monotonic() - started,
            infrastructure=infrastructure,
            checkpoint=self._publication_checkpoint,
        )
        failure = _fresh_shared_failure(directory, result, outcome)
        if not work_item_attempt_requeues(passed=False):
            # An OOM-killed work item attempt is run again: its killed build must not block it.
            self._shared_failure[key] = failure
        return _blocked_shared_result(request, failure, time.monotonic() - started)

    @staticmethod
    def _prepare_shared_launch(request, ready, execution, handle, names):
        with execution.ordinary_group(
            handle, names, work_item_test_runs(request.work_item)
        ) as group:
            _authenticate_planning_disclosure(request, group)
            group.bind_authenticated_bundle(ready.build_execution)
        return group

    def _execute_group(
        self,
        request: WorkExecutionRequest,
        build_directory: Path,
        build_attempt: BundleBuildAttempt,
        run_directory: RunDirectory,
    ) -> SimulationResult:
        started = time.monotonic()
        inputs = self._group_inputs(request)
        runs = work_item_test_runs(request.work_item)
        with inputs.execution.ordinary_group(inputs.handle, inputs.names, runs) as group:
            prepared = self._prepare_bundle_artifacts(
                request, build_directory, build_attempt, group, inputs, started
            )
            if isinstance(prepared, SimulationResult):
                return prepared
        build_result, bundle = self._publish_ready(
            request,
            build_directory,
            build_attempt,
            prepared.artifacts,
            prepared.execution_ref,
            started,
        )
        return self._claim_and_launch(
            request, run_directory, build_directory, (build_result, bundle, group), inputs, started
        )

    def _prepare_bundle_artifacts(
        self,
        request: WorkExecutionRequest,
        build_directory: Path,
        build_attempt: BundleBuildAttempt,
        group: _OrdinaryGroup,
        inputs: _GroupInputs,
        started: float,
    ) -> _CapturedBuild | SimulationResult:
        """Compile the private build; return its captured evidence or the terminal failure."""
        _authenticate_planning_disclosure(request, group)
        if inputs.access == "legacy-per-test":
            failure = self._legacy_hook_failure(
                request,
                build_directory,
                build_attempt,
                inputs.handle,
                group,
                inputs.names,
                inputs.options,
                started,
            )
            if failure is not None:
                return failure
        captured = self._compile_and_capture(
            request, build_directory, build_attempt, group, inputs.workload
        )
        if captured is None:
            return self._compile_failure(request, build_directory, build_attempt, group, started)
        return captured

    def _group_inputs(self, request: WorkExecutionRequest):
        target = cast(Mapping[str, str], request.manifest.document["target"])
        handle = request.target_handle or TargetCatalog.build(request.project_root).select(
            target["selector"], for_flow="sim"
        )
        if handle.identity != f"{target['vlnv']}#{target['name']}":
            raise SimulationRecordIntegrityError(
                "current Target identity disagrees with record manifest"
            )
        selection = cast(Mapping[str, object], request.work_item["selection"])
        names = cast(tuple[str, ...], selection["names"])
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        options = simulation_options_from_policy(
            request.policy, trace=cast(bool, workload["trace"])
        )
        # Build and launch commands name paths relative to the record's own
        # checkout (a Cycle Count baseline's is not the invocation's), so they run there.
        execution = (
            self._execution_factory(options)
            if self._execution_factory
            else SimulationExecution(
                invoke=functools.partial(self._invoke, cwd=request.project_root), options=options
            )
        )
        return _GroupInputs(
            handle,
            names,
            work_item_test_runs(request.work_item),
            options,
            execution,
            workload,
            cast(str, workload["pre_sim_build_access"]),
        )

    def _legacy_hook_failure(
        self, request, build_directory, build_attempt, handle, group, names, options, started
    ) -> SimulationResult | None:
        pre_sim = _run_hook(
            handle,
            group.build_root,
            names,
            options,
            None,
            True,
            commands=frozen_pre_sim_commands(request.manifest),
            test_runs=work_item_test_runs(request.work_item),
            resource_flags=frozen_resource_flags(request.manifest),
        )
        if pre_sim is not None:
            publish_pre_sim_firing(request, pre_sim, checkpoint=self._publication_checkpoint)
        if pre_sim is None or pre_sim.status == "passed":
            return None
        outcome = _hook_failure_outcome(handle.selector, names, pre_sim)
        elapsed = time.monotonic() - started
        infrastructure = pre_sim.status == "spawn_error"
        if infrastructure:
            outcome = _hook_infrastructure_outcome(outcome, pre_sim.detail)
        result = publish_failed_build_result(
            request,
            build_directory,
            build_attempt,
            outcome,
            elapsed,
            infrastructure=infrastructure,
            checkpoint=self._publication_checkpoint,
        )
        return blocked_result(
            request, build_directory, result, outcome, elapsed, infrastructure=infrastructure
        )

    def _compile_failure(
        self, request, build_directory, build_attempt, group, started
    ) -> SimulationResult:
        outcome = group.finish_build_failure()
        elapsed = time.monotonic() - started
        infrastructure = outcome.infrastructure_failure is not None
        result = publish_failed_build_result(
            request,
            build_directory,
            build_attempt,
            outcome,
            elapsed,
            infrastructure=infrastructure,
            checkpoint=self._publication_checkpoint,
        )
        return blocked_result(
            request, build_directory, result, outcome, elapsed, infrastructure=infrastructure
        )

    def _run_ready_group(
        self, request, build_directory, run_cwd, ready: _ReadyLaunch
    ) -> SimulationResult:
        token = request.attempt_id.replace("-", "")
        # Only a group that owns a runtime view has this method (the engine's PreparedOrdinaryGroup).
        runtime_view: Callable[[Path, str], AbstractContextManager[Path]] | None = getattr(
            ready.build[2], "runtime_view", None
        )
        view = runtime_view(run_cwd, token) if runtime_view is not None else nullcontext(run_cwd)
        with view as effective_cwd:
            return self._run_ready_group_in_view(request, build_directory, effective_cwd, ready)

    def _run_ready_group_in_view(
        self, request, build_directory, run_cwd, ready: _ReadyLaunch
    ) -> SimulationResult:
        build_result, bundle, group = ready.build
        handle, names, options = ready.simulation
        workload, access, run_directory, started = ready.policy
        declarations = cast(tuple[dict[str, str], ...], workload["runtime_inputs"])
        self._publication_checkpoint("before:runtime_inputs")
        with materialize_simulation_record_runtime_inputs(
            bundle_root=build_directory,
            attempt_root=request.attempt_directory,
            run_cwd=run_cwd,
            declarations=declarations,
            owned_run_directory=run_directory.owned,
            booley_dir=resolve_checkout_booley_dir(request.project_root),
        ) as bindings:
            self._publication_checkpoint("after:runtime_inputs")
            failure = _immutable_hook_failure(
                request,
                build_directory,
                build_result,
                bundle,
                handle,
                group,
                names,
                options,
                run_cwd,
                bindings,
                access,
                started,
                self._publication_checkpoint,
            )
            if failure is not None:
                return failure
            return _launch_bundle(
                request,
                build_directory,
                build_result,
                bundle,
                group,
                run_cwd,
                bindings,
                started,
                self._publication_checkpoint,
            )


def _authenticate_planning_disclosure(request: WorkExecutionRequest, group: object) -> None:
    disclosures = cast(
        tuple[Mapping[str, object], ...], request.manifest.document["planning_disclosures"]
    )
    ordinal = cast(int, request.work_item["ordinal"])
    if disclosures and (
        ordinal >= len(disclosures)
        or canonical_json_bytes(group.planning_disclosure())  # type: ignore[attr-defined]
        != canonical_json_bytes(disclosures[ordinal])
    ):
        raise SimulationRecordIntegrityError(
            "prepared generator source closure disagrees with record plan"
        )


def _immutable_hook_failure(
    request,
    build_directory,
    build_result,
    bundle,
    handle,
    group,
    names,
    options,
    run_cwd,
    bindings,
    access,
    started,
    checkpoint,
) -> SimulationResult | None:
    if access != "immutable":
        return None
    surface = project_compile_surface(group.compile_surface)
    pre_sim = _run_hook(
        handle,
        group.build_root,
        names,
        options,
        run_cwd,
        False,
        commands=frozen_pre_sim_commands(request.manifest),
        test_runs=work_item_test_runs(request.work_item),
        resource_flags=frozen_resource_flags(request.manifest),
    )
    if pre_sim is not None:
        publish_pre_sim_firing(request, pre_sim, checkpoint=checkpoint)
    if project_compile_surface(group.compile_surface) != surface:
        raise SimulationRecordIntegrityError(
            "Project compile inputs changed during immutable Pre-Sim Commands"
        )
    if pre_sim is None or pre_sim.status == "passed":
        return None
    if pre_sim.status == "spawn_error":
        raise SimulationRecordIntegrityError(pre_sim.detail or "Pre-Sim Commands could not start")
    return _setup_result(
        request,
        build_directory,
        build_result,
        bundle,
        names,
        bindings,
        pre_sim.detail,
        time.monotonic() - started,
    )


def simulation_attempt(request: WorkExecutionRequest) -> tuple[SimulationAttempt, RunDirectory]:
    """Return the Simulation Attempt record for *request* and its run directory."""
    workload = cast(Mapping[str, object], request.manifest.document["workload"])
    run_cwd = cast(Mapping[str, object], workload["run_cwd"])
    configured = cast(str, run_cwd["configured"])
    target = cast(Mapping[str, str], request.manifest.document["target"])
    run = expand_run_directory(
        configured,
        project_root=request.project_root,
        simulation_record_id=cast(str, request.manifest.document["simulation_record_id"]),
        target_key=target["selector"].replace("/", "%2F"),
        work_item_key=request.store.work_item_directory(
            cast(str, request.work_item["work_item_id"])
        ).name,
        attempt_key=f"{request.attempt_ordinal:04d}-{request.attempt_id}",
    )
    document = {
        "$schema": "booley.simulation-attempt/v2",
        **_common(request),
        "attempt_id": request.attempt_id,
        "attempt_ordinal": request.attempt_ordinal,
        "producer_invocation_id": request.producer_invocation_id,
        "build_variant_id": request.work_item["build_variant_id"],
        "run_directory": {
            "kind": run_cwd["kind"],
            "configured": configured,
            "resolved": str(run.path),
            "collision_key": run.collision_key,
            "owned": run.owned,
        },
        "child_execution_id": request.child_execution_id,
        "child_entry_sha256": None,
        "pre_sim_build_access": workload["pre_sim_build_access"],
        "policy": {
            "timeout_seconds": (
                round(request.policy.timeout_seconds)
                if request.policy.timeout_seconds is not None
                else None
            ),
            "no_kill": request.policy.no_kill,
        },
        "started_at": _now(),
    }
    return SimulationAttempt(document), run


def _run_hook(
    handle: object,
    build_root: Path,
    names: tuple[str, ...],
    _options: SimulationOptions,
    run_cwd: Path | None,
    expose_build_root: bool,
    *,
    resource_flags: tuple[str, ...],
    commands: tuple[str, ...] | None = None,
    test_runs: tuple[TestRun, ...] = (),
):
    return run_pre_sim_commands(
        handle,  # type: ignore[arg-type]
        test_names=names,
        build_root=build_root,
        eda_tool=cast(str, getattr(handle, "eda_tool", "")),
        timeout_s=DEFAULT_TIMEOUT_S,
        simulator_environment=simulation_target_environment(handle),  # type: ignore[arg-type]
        run_cwd=str(run_cwd) if run_cwd is not None else None,
        expose_build_root=expose_build_root,
        commands=commands,
        test_runs=test_runs,
        resource_flags=resource_flags,
    )


def _hook_failure_outcome(
    target: str, names: tuple[str, ...], evidence: PreSimEvidence
) -> SimulationTargetOutcome:
    tests = tuple(
        SimulationTestOutcome(
            name=name or target,
            verdict="elab_error",
            passed=False,
            elab_failed=True,
            error_tail=pre_sim_failure_message(evidence.status, evidence.detail),
        )
        for name in (names or (target,))
    )
    return SimulationTargetOutcome(
        target=target,
        target_identity="",
        toplevel="",
        eda_tool="",
        passed=False,
        verdict="fail",
        elapsed_s=cast(float, getattr(evidence, "elapsed_s", 0.0)),
        tests=tests,
    )


def _hook_infrastructure_outcome(
    outcome: SimulationTargetOutcome, detail: str
) -> SimulationTargetOutcome:
    return SimulationTargetOutcome(
        target=outcome.target,
        target_identity=outcome.target_identity,
        toplevel=outcome.toplevel,
        eda_tool=outcome.eda_tool,
        passed=False,
        verdict="error",
        elapsed_s=outcome.elapsed_s,
        tests=outcome.tests,
        infrastructure_failure=SimulationInfrastructureFailure(
            "pre_sim_spawn", "Pre-Sim Commands could not start", detail=detail
        ),
    )


@reads_simulation_record_documents
def _recovered_shared_failure(directory: Path, result: BundleBuildResult) -> _SharedFailure:
    observation = cast(Mapping[str, object], result.document["observation"])
    detail = cast(Mapping[str, object], observation["detail"])
    text = detail.get("text", observation["message"])
    if not isinstance(text, str):
        raise SimulationRecordIntegrityError("shared build failure text must be a string")
    return _SharedFailure(directory, result, FailureClass(cast(str, observation["class"])), text)


def _fresh_shared_failure(
    directory: Path, result: BundleBuildResult, outcome: SimulationTargetOutcome
) -> _SharedFailure:
    detail = next(
        (
            test.reason or test.error_tail
            for test in outcome.tests
            if test.reason or test.error_tail
        ),
        "build failed",
    )
    return _SharedFailure(
        directory,
        result,
        FailureClass.INFRASTRUCTURE if outcome.infrastructure_failure else FailureClass.DESIGN,
        _bounded_reason(detail) if outcome.infrastructure_failure else detail[-1536:],
    )


def _setup_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_result: BundleBuildResult,
    bundle: SimulatorBundle,
    names: tuple[str, ...],
    bindings: tuple[RuntimeInputBinding, ...],
    detail: str,
    elapsed: float,
) -> SimulationResult:
    selection = cast(Mapping[str, object], request.work_item["selection"])
    # The codec's _read_strings proves target.selector is a string.
    target = cast(Mapping[str, str], request.manifest.document["target"])
    fallback = "" if selection["kind"] == "unfiltered" else target["selector"]
    tests = tuple(
        SimulationTestOutcome(
            name=name, verdict="elab_error", passed=False, elab_failed=True, error_tail=detail
        )
        for name in (names or (fallback,))
    )
    return simulation_result(
        request,
        build_directory,
        build_result,
        state="setup_error",
        bundle_id=bundle.document["bundle_id"],
        verification=None,
        observations=[observation_document(test, execution="setup_error") for test in tests],
        elapsed=elapsed,
        evidence=[],
        runtime_inputs=runtime_input_documents(request, bindings),
    )


def runtime_input_documents(
    request: WorkExecutionRequest, bindings: tuple[RuntimeInputBinding, ...]
) -> list[dict[str, object]]:
    """Describe the runtime inputs bound to *request*'s work item as record documents."""
    return [
        binding.result_document(
            reference_path=binding.authoritative_copy.relative_to(
                request.attempt_directory
            ).as_posix(),
            owner=request.attempt_id,
        )
        for binding in bindings
    ]


def bundle_build_attempt(request: WorkExecutionRequest, scope: BuildScope) -> BundleBuildAttempt:
    """Return the build attempt that *scope* starts for *request*."""
    workload = cast(Mapping[str, object], request.manifest.document["workload"])
    eda = cast(Mapping[str, str], workload["eda"])
    document = {
        "$schema": "booley.bundle-build-attempt/v2",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt_id": scope.build_attempt_id,
        "build_attempt_ordinal": scope.ordinal,
        "producer_invocation_id": request.producer_invocation_id,
        "sharing": scope.sharing,
        "owner": scope.owner,
        "tool_provenance": {
            "eda_kind": eda["kind"],
            "eda_version": eda["version"],
            "adapter_contract_version": workload["adapter_contract_version"],
        },
        "started_at": _now(),
    }
    return BundleBuildAttempt(document)


def _common(request: WorkExecutionRequest, *, include_work_item: bool = True) -> dict[str, object]:
    fingerprints = cast(Mapping[str, str], request.manifest.document["fingerprints"])
    common: dict[str, object] = {
        "simulation_record_id": request.manifest.document["simulation_record_id"],
        "manifest_sha256": manifest_digest(request.manifest),
        "workload_sha256": fingerprints["workload_sha256"],
    }
    if include_work_item:
        common["work_item_id"] = request.work_item["work_item_id"]
    return common


def publish_ready_build_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_attempt: BundleBuildAttempt,
    artifacts: list[dict[str, object]],
    execution_ref: dict[str, object] | None,
    elapsed: float,
) -> tuple[BundleBuildResult, SimulatorBundle]:
    """Publish the Simulator Bundle and ready build result of *build_attempt*.

    The sharing scope, owner and build-attempt id come from *build_attempt*.
    *execution_ref* is the build-execution record; native coverage captures
    its image without a recorded compiler process, so it passes ``None``.
    """
    bundle = _simulator_bundle(request, build_attempt, artifacts)
    bundle_raw = encode_simulator_bundle(bundle)
    _create_immutable(build_directory / _BUNDLE_MANIFEST_PATH, bundle_raw)
    document = _ready_result_document(
        request, build_directory, build_attempt, bundle, bundle_raw, execution_ref, elapsed
    )
    # _freeze_json checks the values when the document is constructed.
    result = BundleBuildResult(cast(Mapping[str, FrozenJson], document))
    request.store.publish_build_result(build_directory, result)
    return result, bundle


def _simulator_bundle(
    request: WorkExecutionRequest,
    build_attempt: BundleBuildAttempt,
    artifacts: list[dict[str, object]],
) -> SimulatorBundle:
    attempt = build_attempt.document
    document = {
        "$schema": "booley.simulator-bundle/v3",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt_id": attempt["build_attempt_id"],
        "bundle_id": str(uuid.uuid4()),
        "sharing": attempt["sharing"],
        "owner": attempt["owner"],
        "tool_provenance": attempt["tool_provenance"],
        "created_at": _now(),
        "artifacts": artifacts,
        "inventory_sha256": document_digest(artifacts),
    }
    return SimulatorBundle(document)


def _ready_result_document(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_attempt: BundleBuildAttempt,
    bundle: SimulatorBundle,
    bundle_raw: bytes,
    execution_ref: dict[str, object] | None,
    elapsed: float,
) -> dict[str, object]:
    build_id = cast(str, build_attempt.document["build_attempt_id"])
    # _simulator_bundle built the artifacts list in this process.
    bundle_artifacts = cast(tuple[Mapping[str, FrozenJson], ...], bundle.document["artifacts"])
    return {
        "$schema": "booley.bundle-build-result/v2",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt": record_reference(
            build_directory / "build-attempt.json",
            build_directory,
            encode_bundle_build_attempt(build_attempt),
            "bundle_build_attempt",
            build_id,
        )
        | {"build_attempt_id": build_id},
        "state": "ready",
        "phase": "ready",
        "finished_at": _now(),
        "elapsed_seconds": elapsed,
        "bundle": {
            "bundle_id": bundle.document["bundle_id"],
            "manifest_path": _BUNDLE_MANIFEST_PATH,
            "manifest_bytes": len(bundle_raw),
            "manifest_sha256": content_digest(bundle_raw),
            "sharing": bundle.document["sharing"],
            "artifacts": [dict(item) for item in bundle_artifacts],
        },
        "observation": None,
        "evidence": [execution_ref] if execution_ref is not None else [],
    }


# Simulation Record readers load build-execution evidence under ``RECORD_MAX_BYTES``,
# but a compiler can print far more (a traced build of a large design easily
# exceeds 1 MiB). Each captured text field therefore keeps a fixed head and
# tail; the full output stays in every test's ``run.log``.
BUILD_EXECUTION_TEXT_HEAD_CHARS = 16 * 1024
BUILD_EXECUTION_TEXT_TAIL_CHARS = 16 * 1024
_BOUNDED_BUILD_TEXT_FIELDS = (("process", "stdout"), ("process", "stderr"), ("build", "output"))
# Canonical JSON escapes a control character as ``\u00XX``: six bytes for one
# character, the worst expansion. Even then the kept text stays well inside
# the reader's ceiling, leaving room for the markers and the scalar fields.
_WORST_CASE_TEXT_BYTES = (
    6
    * len(_BOUNDED_BUILD_TEXT_FIELDS)
    * (BUILD_EXECUTION_TEXT_HEAD_CHARS + BUILD_EXECUTION_TEXT_TAIL_CHARS)
)
assert _WORST_CASE_TEXT_BYTES <= RECORD_MAX_BYTES * 3 // 4


def bounded_build_execution_document(document: Mapping[str, object]) -> dict[str, object]:
    """Return *document* with each compiler text field cut to its head and tail.

    Text within the budget is kept verbatim. Longer text keeps the first
    ``BUILD_EXECUTION_TEXT_HEAD_CHARS`` and last
    ``BUILD_EXECUTION_TEXT_TAIL_CHARS`` characters around a marker that names
    how many UTF-8 bytes were omitted. *document* itself is not modified.
    """
    bounded: dict[str, object] = {
        key: dict(value) if isinstance(value, Mapping) else value
        for key, value in document.items()
    }
    for section, field in _BOUNDED_BUILD_TEXT_FIELDS:
        values = cast(dict[str, object], bounded[section])
        values[field] = _head_and_tail(cast(str, values[field]))
    return bounded


def _head_and_tail(text: str) -> str:
    head_chars = BUILD_EXECUTION_TEXT_HEAD_CHARS
    tail_chars = BUILD_EXECUTION_TEXT_TAIL_CHARS
    if len(text) <= head_chars + tail_chars:
        return text
    omitted = len(text[head_chars:-tail_chars].encode("utf-8", "surrogatepass"))
    marker = (
        f"\n[... {omitted} bytes omitted from build-execution evidence;"
        " the test run.log keeps the full compiler output ...]\n"
    )
    return text[:head_chars] + marker + text[-tail_chars:]


def _capture_build_execution(
    directory: Path, build_attempt_id: str, document: Mapping[str, object]
) -> dict[str, object]:
    raw = canonical_json_bytes(bounded_build_execution_document(document))
    if len(raw) > RECORD_MAX_BYTES:
        # Only the scalar fields remain unbounded; refuse to publish a record
        # that every reader would reject.
        raise SimulationRecordIntegrityError(
            f"build execution evidence is {len(raw)} bytes, over the {RECORD_MAX_BYTES}-byte"
            " record ceiling"
        )
    path = directory / "evidence" / "build-execution.json"
    _create_immutable(path, raw)
    return record_reference(path, directory, raw, "simulation_build_execution", build_attempt_id)


def publish_failed_build_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_attempt: BundleBuildAttempt,
    outcome: SimulationTargetOutcome,
    elapsed: float,
    *,
    infrastructure: bool,
    checkpoint: Callable[[str], None],
) -> BundleBuildResult:
    checkpoint("before:build_result")
    result = _failed_build_result(
        request, build_directory, build_attempt, outcome, elapsed, infrastructure=infrastructure
    )
    request.store.publish_build_result(build_directory, result)
    checkpoint("after:build_result")
    return result


def _failed_build_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_attempt: BundleBuildAttempt,
    outcome: SimulationTargetOutcome,
    elapsed: float,
    *,
    infrastructure: bool,
) -> BundleBuildResult:
    build_attempt_id = cast(str, build_attempt.document["build_attempt_id"])
    failure = outcome.infrastructure_failure
    detail = (
        failure.detail or failure.message
        if failure is not None
        else next((test.error_tail for test in outcome.tests if test.error_tail), "build failed")
    )
    state = "infrastructure_error" if infrastructure else "design_failure"
    document = {
        "$schema": "booley.bundle-build-result/v2",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt": record_reference(
            build_directory / "build-attempt.json",
            build_directory,
            encode_bundle_build_attempt(build_attempt),
            "bundle_build_attempt",
            build_attempt_id,
        )
        | {"build_attempt_id": build_attempt_id},
        "state": state,
        "phase": "pre_sim"
        if failure is not None and failure.kind == "pre_sim_spawn"
        else "elaboration",
        "finished_at": _now(),
        "elapsed_seconds": elapsed,
        "bundle": None,
        "observation": {
            "class": "infrastructure" if infrastructure else "design",
            "code": failure.kind if failure is not None else "elaboration",
            "message": (
                _bounded_message(failure.message)
                if failure is not None
                else "Simulation build failed"
            ),
            "detail": _build_failure_detail(outcome, detail)
            if infrastructure
            else {"text": detail[:1536]},
        },
        "evidence": [],
    }
    return BundleBuildResult(document)


def _bounded_message(message: str) -> str:
    """Keep the first ``MAX_COMMAND_BYTES`` UTF-8 bytes of a build failure message.

    Messages can carry a whole tool output; readers refuse longer strings.
    """
    return message.encode("utf-8")[:MAX_COMMAND_BYTES].decode("utf-8", errors="ignore")


def blocked_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_result: BundleBuildResult,
    outcome: SimulationTargetOutcome,
    elapsed: float,
    *,
    infrastructure: bool = False,
    evidence: list[dict[str, object]] | None = None,
) -> SimulationResult:
    """Build the terminal result for a work item blocked before it ran."""
    observations = (
        _infrastructure_observations(request, outcome, build_failure=True)
        if infrastructure
        else [observation_document(test, execution="blocked_by_build") for test in outcome.tests]
    )
    selection = cast(Mapping[str, object], request.work_item["selection"])
    if selection["kind"] == "unfiltered":
        observations = observations[:1]
        observations[0]["test"] = None
    pre_sim_failure = build_result.document["phase"] == "pre_sim"
    if infrastructure and pre_sim_failure:
        for observation in observations:
            observation["execution"] = "blocked_by_build"
    return simulation_result(
        request,
        build_directory,
        build_result,
        state="aborted" if infrastructure and not pre_sim_failure else "blocked_by_build",
        bundle_id=None,
        verification=None,
        observations=observations,
        elapsed=elapsed,
        evidence=evidence or [],
    )


def _blocked_shared_result(
    request: WorkExecutionRequest, failure: _SharedFailure, elapsed: float
) -> SimulationResult:
    selection = cast(Mapping[str, object], request.work_item["selection"])
    names = cast(tuple[str, ...], selection["names"])
    if selection["kind"] == "unfiltered":
        identities: tuple[str | None, ...] = (None,)
    elif names:
        identities = names
    else:
        # The default selection's name is the Target selector; Test Run binding
        # (discovered_test_run) maps it back to the unnamed invocation; the codec proves it a str.
        identities = (cast(Mapping[str, str], request.manifest.document["target"])["selector"],)
    observations = []
    for index, identity in enumerate(identities):
        test = SimulationTestOutcome(
            name=identity or "",
            verdict="elab_error",
            passed=False,
            elab_failed=True,
            error_tail=failure.detail,
        )
        observation = observation_document(
            test,
            execution="aborted"
            if failure.failure_class == FailureClass.INFRASTRUCTURE
            else "blocked_by_build",
        )
        if failure.failure_class == FailureClass.INFRASTRUCTURE:
            observation["functional"] = observation["assertions"] = "not_observed"
            # The persisted build detail object has open-ended keys, so `Any` is the honest type.
            observation["detail"] = _compact_build_detail(
                cast(Mapping[str, Any], failure.result.document["observation"])["detail"],
                1 if index == 0 else len(identities),
            )
        observation["failure_class"] = failure.failure_class.value
        observations.append(observation)
    return simulation_result(
        request,
        failure.directory,
        failure.result,
        state="aborted"
        if failure.failure_class == FailureClass.INFRASTRUCTURE
        else "blocked_by_build",
        bundle_id=None,
        verification=None,
        observations=observations,
        elapsed=elapsed,
        evidence=[],
    )


def _launch_bundle(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_result: BundleBuildResult,
    bundle: SimulatorBundle,
    group: object,
    run_cwd: Path,
    bindings: tuple[RuntimeInputBinding, ...],
    started: float,
    publication_checkpoint: Callable[[str], None],
) -> SimulationResult:
    """Launch the record's bundle in place, verifying it before launch and after exit."""
    pre_launch = verify_bundle_in_place(bundle, build_result, build_directory)
    evidence_root = request.attempt_directory / "execution-evidence"
    # The run's recorded outcome waits for the post-exit check: a bundle changed
    # by the run is refused there, after the run's own verdict.
    with judged_commands_failing_on_error(joinable=True):
        outcome = group.launch_bundle(build_directory, evidence_root, run_cwd)  # type: ignore[attr-defined]
        post_exit = verify_bundle_in_place(bundle, build_result, build_directory)
    return _bundle_result(
        request,
        build_directory,
        build_result,
        bundle,
        bundle_verification(build_result, pre_launch, post_exit),
        outcome,
        bindings,
        started,
        publication_checkpoint,
    )


def bundle_verification(
    build_result: BundleBuildResult, pre_launch: str, post_exit: str
) -> dict[str, object]:
    """The Result's record of the bundle inventory that passed each check."""
    return {
        "bundle_manifest_sha256": cast(Mapping[str, object], build_result.document["bundle"])[
            "manifest_sha256"
        ],
        "pre_launch_sha256": pre_launch,
        "post_exit_sha256": post_exit,
        "verified_after_exit": True,
    }


def _bundle_result(
    request,
    build_directory,
    build_result,
    bundle,
    verification,
    outcome,
    bindings,
    started,
    publication_checkpoint,
) -> SimulationResult:
    observations = (
        _infrastructure_observations(request, outcome)
        if outcome.infrastructure_failure is not None
        else [observation_document(test) for test in outcome.tests]
    )
    state = settled_state(observations)
    publication_checkpoint("before:execution_evidence")
    evidence = _capture_outcome_evidence(request, outcome)
    publication_checkpoint("after:execution_evidence")
    return simulation_result(
        request,
        build_directory,
        build_result,
        state=state,
        bundle_id=bundle.document["bundle_id"],
        verification=verification,
        observations=observations,
        elapsed=time.monotonic() - started,
        evidence=evidence,
        runtime_inputs=runtime_input_documents(request, bindings),
        diagnostics=tuple(
            item for item in outcome.diagnostics if item.startswith("artifact_persistence:")
        ),
        trace_warnings=tuple(
            warning
            for artifact in outcome.artifacts
            if artifact.kind == "trace"
            for warning in artifact.warnings
        ),
    )


def _named_observations(
    work_item: Mapping[str, object], observations: list[dict[str, object]]
) -> list[dict[str, object]]:
    """The observations, each naming the Test Run it observed."""
    bound = observation_test_runs(work_item, observations)
    return [
        {**observation, "test_run": name}
        for observation, (name, _fingerprint) in zip(observations, bound, strict=True)
    ]


def simulation_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_result: BundleBuildResult,
    *,
    state: str,
    bundle_id: object,
    verification: object,
    observations: list[dict[str, object]],
    elapsed: float,
    evidence: list[dict[str, object]],
    runtime_inputs: list[dict[str, object]] | None = None,
    diagnostics: tuple[str, ...] = (),
    trace_warnings: tuple[str, ...] = (),
) -> SimulationResult:
    """Build the terminal simulation result for a completed run."""
    observations = _named_observations(request.work_item, observations)
    document = {
        "$schema": RESULT_SCHEMA,
        **_common(request),
        "attempt_id": request.attempt_id,
        "attempt_ordinal": request.attempt_ordinal,
        "producer_invocation_id": request.producer_invocation_id,
        "state": state,
        "build_result": _build_result_ref(request, build_directory, build_result),
        "bundle_id": bundle_id,
        "finished_at": _now(),
        "elapsed_seconds": elapsed,
        "bundle_verification": verification,
        "runtime_inputs": runtime_inputs or [],
        "observations": observations,
        "grade": _result_grade(observations),
        "diagnostics": _result_diagnostics(diagnostics, trace_warnings),
        "evidence": [*evidence, *request.store.pre_sim_references(request.attempt_directory)],
    }
    return SimulationResult(document)


def _result_diagnostics(
    diagnostics: tuple[str, ...], trace_warnings: tuple[str, ...]
) -> list[dict[str, object]]:
    """The Result's warning diagnostics: persistence notes, then the waveform's conversion warnings."""
    entries = [
        ("artifact_persistence" if item.startswith("artifact_persistence:") else "execution", item)
        for item in diagnostics
    ]
    entries.extend((TRACE_WARNING_CODE, warning) for warning in trace_warnings)
    return [
        {"severity": "warning", "code": code, "pointer": "", "message": _bounded_reason(message)}
        for code, message in entries
    ]


def _result_grade(observations: list[dict[str, object]]) -> str:
    grades = [
        grade_observations(
            ExecutionObservation(cast(str, item["execution"])),
            FailureClass(cast(str, item["failure_class"]))
            if item["failure_class"] is not None
            else None,
            FunctionalObservation(cast(str, item["functional"])),
            AssertionObservation(cast(str, item["assertions"])),
        )
        for item in observations
    ]
    precedence = {"pass": 0, "inconclusive": 1, "fail": 2, "error": 3}
    return max(grades, key=lambda item: precedence[item.value]).value


def observation_document(
    test: SimulationTestOutcome, *, execution: str | None = None, reason_limit: int = 768
) -> dict[str, object]:
    """Render one test outcome as an observation record document."""
    observed_execution = execution or (
        "aborted"
        if test.failure_kind == "infrastructure"
        else "timeout"
        if test.timed_out
        else "crash"
        if test.crashed
        else "aborted"
        if test.termination != "completed"
        else "completed"
    )
    blocked = observed_execution in {"blocked_by_build", "setup_error", "not_run"}
    functional = (
        "not_observed"
        if blocked
        or observed_execution in {"timeout", "crash"}
        or (observed_execution == "aborted" and test.failure_kind == "infrastructure")
        else "pass"
        if test.verdict == "pass"
        else "inconclusive"
        if test.inconclusive
        else "fail"
    )
    assertions = (
        "not_observed"
        if blocked or observed_execution in {"timeout", "crash", "aborted"}
        else "dirty"
        if test.sva_errors
        else "clean"
    )
    return {
        "test": test.name or None,
        "execution": observed_execution,
        "failure_class": (
            "infrastructure"
            if test.failure_kind == "infrastructure"
            else "design"
            if blocked or observed_execution in {"timeout", "aborted"} or not test.passed
            else None
        ),
        "functional": functional,
        "assertions": assertions,
        "assertion_count": test.sva_errors,
        "detail": (
            {
                "reason": _bounded_reason(test.reason or test.error_tail, reason_limit),
                "termination": test.termination,
                "code": test.failure_kind,
                "simulator_returncode": test.simulator_returncode,
            }
            if test.failure_kind == "infrastructure"
            else {"reason": (test.reason or test.error_tail)[-1536:]}
        ),
        "cycle_count": test.cycles if observed_execution == "completed" else None,
    }


def _bounded_reason(text: str, limit: int = 768) -> str:
    return text.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _build_failure_detail(outcome: SimulationTargetOutcome, reason: str) -> dict[str, object]:
    failure = outcome.infrastructure_failure
    header = outcome.builds[-1].reason if outcome.builds else reason.splitlines()[0]
    text = header.encode("utf-8")[:256].decode("utf-8", errors="ignore")
    tail = _bounded_reason(build_output_tail(reason), 384)
    if tail and tail != text:
        text += "\n--- output tail ---\n" + tail
    detail: dict[str, object] = {"reason": text, "text": text}
    if failure is not None:
        detail.update(
            code=_bounded_reason(failure.kind, 128),
            phase="pre_sim" if failure.kind == "pre_sim_spawn" else "elaboration",
            missing_executable=_bounded_reason(failure.missing_executable, 128),
        )
    if outcome.builds and not outcome.builds[-1].passed:
        build = outcome.builds[-1]
        detail["build_stage"] = {
            "ran": build.ran,
            "verdict": build.verdict,
            "failure_class": build.failure_kind,
            "elapsed_s": round(build.elapsed_s, 3),
            "terminal_record": build.terminal_record,
            "cache_decision": build.cache_decision,
            "returncode": build.returncode,
            "timed_out": build.timed_out,
            "peak_rss_mb": build.peak_rss_mb,
            "oom_kill_delta": build.oom_kill_delta,
            "reason": _bounded_reason(build.reason, 128),
        }
    return _bounded_build_detail(detail)


def _bounded_build_detail(detail: dict[str, object]) -> dict[str, object]:
    for limit in (512, 256, 128, 64, 32, 16):
        if len(canonical_json_bytes(detail)) <= MAX_DETAIL_BYTES:
            return detail
        for key, value in detail.items():
            if isinstance(value, str):
                detail[key] = value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
            elif isinstance(value, dict):
                for nested, item in value.items():
                    if isinstance(item, str):
                        value[nested] = item.encode("utf-8")[:limit].decode(
                            "utf-8", errors="ignore"
                        )
    assert len(canonical_json_bytes(detail)) <= MAX_DETAIL_BYTES, (
        "build metadata exceeds detail budget"
    )
    return detail


def _compact_build_detail(detail: Mapping[str, object], count: int) -> dict[str, object]:
    if count <= 128:
        return dict(detail)
    return {
        "reason": _bounded_reason(str(detail["reason"]), 16),
        "code": detail.get("code", "build"),
        "phase": detail.get("phase", "elaboration"),
    }


def _observed_infrastructure_tests(
    outcome: SimulationTargetOutcome, *, build_failure: bool
) -> list[dict[str, object]]:
    observations = [
        observation_document(test, reason_limit=16 if len(outcome.tests) > 128 else 768)
        for test in outcome.tests
    ]
    if build_failure or (
        outcome.builds
        and not outcome.builds[-1].passed
        and outcome.infrastructure_failure is not None
    ):
        for index, observation in enumerate(observations):
            observation.update(
                execution="aborted",
                failure_class="infrastructure",
                functional="not_observed",
                assertions="not_observed",
                cycle_count=None,
            )
            observation["detail"] = _compact_build_detail(
                _build_failure_detail(
                    outcome, outcome.tests[index].reason or outcome.tests[index].error_tail
                ),
                1 if index == 0 else len(observations),
            )
    return observations


def _infrastructure_observations(
    request, outcome: SimulationTargetOutcome, *, build_failure: bool = False
) -> list[dict[str, object]]:
    if outcome.tests:
        return _observed_infrastructure_tests(outcome, build_failure=build_failure)
    selection = cast(Mapping[str, object], request.work_item["selection"])
    # The unnamed invocation (unfiltered batch or default selection) has no names.
    identities = tuple(cast(Sequence[str], selection["names"])) or (None,)
    failure = outcome.infrastructure_failure
    assert failure is not None
    if build_failure:
        detail = _build_failure_detail(outcome, failure.detail or failure.message)
    else:
        detail = {
            "reason": _bounded_reason(failure.detail or failure.message),
            "termination": failure.kind,
            "code": failure.kind,
        }
    if len(identities) > 128 and not build_failure:
        detail = {
            "reason": _bounded_reason(failure.message, 16),
            "code": failure.kind,
            "termination": failure.kind,
        }
    return [
        {
            "test": name,
            "execution": "aborted",
            "failure_class": "infrastructure",
            "functional": "not_observed",
            "assertions": "not_observed",
            "assertion_count": 0,
            "detail": _compact_build_detail(detail, len(identities))
            if build_failure and index > 0
            else detail,
            "cycle_count": None,
        }
        for index, name in enumerate(identities)
    ]


def settled_state(observations: list[dict[str, object]]) -> str:
    """Return the result state implied by ``observations``, settling them to match.

    Booley writes only one pairing of result state and observations:
    an ``aborted`` result carries no ``timeout`` or ``crash`` observation. So an
    abort dominates: when any observation is ``aborted``, every ``timeout`` or
    ``crash`` observation is rewritten in place to the aborted design matrix
    (functional ``fail``, assertions ``not_observed``, no cycle count, detail
    kept) and the state is ``aborted``. Otherwise the state is ``crash``, then
    ``timeout``, then ``completed`` (``not_run`` counts as completed).
    """
    executions = {str(item["execution"]) for item in observations}
    if "aborted" in executions:
        for item in observations:
            if item["execution"] in {"timeout", "crash"}:
                item.update(
                    execution="aborted",
                    functional="fail",
                    assertions="not_observed",
                    cycle_count=None,
                )
        return "aborted"
    if "crash" in executions:
        return "crash"
    if "timeout" in executions:
        return "timeout"
    return "completed"


def capture_private_image(
    paths: tuple[Path, ...],
    source_root: Path,
    destination: Path,
    runtime_declarations: tuple[Mapping[str, str], ...],
) -> list[dict[str, object]]:
    """Copy the build artifacts into *destination* and return their evidence records."""
    if not paths:
        raise SimulationRecordIntegrityError("successful build has no authenticated image")
    make_directories(destination.parent)
    destination.mkdir(mode=0o700, exist_ok=True)
    sources: list[tuple[Path, str]] = []
    executable_assigned = False
    for source in paths:
        is_executable = source.suffix not in {".scr", ".vpi", ".so"}
        kind = (
            "simulator_executable"
            if is_executable and not executable_assigned
            else "shared_library"
            if source.suffix in {".vpi", ".so"}
            else "runtime_data"
        )
        executable_assigned |= kind == "simulator_executable"
        sources.append((source, kind))
    for declaration in runtime_declarations:
        # A resumed prerequisite manifest is not re-planned, so refuse a path
        # that could copy outside the private image before touching the disk.
        try:
            relative = safe_runtime_relative(declaration["source_artifact_path"])
        except RuntimeInputError as exc:
            raise SimulationRecordIntegrityError(str(exc)) from exc
        sources.append((source_root / relative, "runtime_input"))
    if not executable_assigned:
        raise SimulationRecordIntegrityError("private image has no simulator executable")
    records = []
    for source, kind in sources:
        authenticated = _regular_child(source_root, source.relative_to(source_root).as_posix())
        target = destination / authenticated.relative_to(source_root)
        make_directories(target.parent)
        durable_copy(authenticated, target, mode=_ARTIFACT_MODES[kind])
        raw = target.read_bytes()
        records.append(
            {
                "path": target.relative_to(destination).as_posix(),
                "bytes": len(raw),
                "sha256": content_digest(raw),
                "kind": kind,
            }
        )
    return records


def _capture_outcome_evidence(
    request: WorkExecutionRequest, outcome: SimulationTargetOutcome
) -> list[dict[str, object]]:
    evidence_root = request.attempt_directory / "evidence"
    durable_directory(evidence_root)
    evidence_root.chmod(0o700)
    live_root = request.attempt_directory / "execution-evidence"
    references = []
    seen: set[Path] = set()
    for artifact in outcome.artifacts:
        source = Path(artifact.path)
        if source in seen or not source.is_file() or source.is_symlink():
            continue
        seen.add(source)
        references.append(
            _outcome_artifact_reference(
                request,
                outcome,
                artifact,
                evidence_root / f"{len(references) + 1:04d}-{source.name}",
                live_root,
            )
        )
    if outcome.infrastructure_failure is not None and not outcome.tests:
        failure = outcome.infrastructure_failure
        selection = cast(Mapping[str, object], request.work_item["selection"])
        names = list(cast(Sequence[str], selection["names"])) or [None]
        raw = canonical_json_bytes(
            {
                "$schema": "booley.simulation-infrastructure-failure/v1",
                "attempt_id": request.attempt_id,
                "work_item_id": request.work_item["work_item_id"],
                "selected_tests": names,
                "code": failure.kind,
                "termination": failure.kind,
                "source": "process",
            }
        )
        destination = evidence_root / "infrastructure-failure.json"
        _create_immutable(destination, raw)
        references.append(
            record_reference(
                destination,
                request.attempt_directory,
                raw,
                "simulation_infrastructure_failure",
                request.attempt_id,
            )
        )
    return references


def _outcome_artifact_reference(
    request: WorkExecutionRequest,
    outcome: SimulationTargetOutcome,
    artifact: SimulationArtifactEvidence,
    destination: Path,
    live_root: Path,
) -> dict[str, object]:
    """Make one outcome artifact durable and reference it.

    A file already inside the attempt's ``execution-evidence/`` is referenced
    where it is, so the attempt keeps one copy; a file outside the attempt is
    copied to *destination*; a Cocotb transport is rewritten to its derived
    document at *destination*.
    """
    source = Path(artifact.path)
    source_raw = source.read_bytes()
    kind = "cocotb_results" if artifact.kind == "cocotb_results_json" else artifact.kind
    if artifact.kind == "cocotb_results_json":
        source_observations = _decode_cocotb_source(source_raw, artifact.test_names)
        raw = canonical_json_bytes(
            {
                "$schema": "booley.cocotb-record-transport/v1",
                "source_bytes": len(source_raw),
                "source_sha256": content_digest(source_raw),
                "source_observations": source_observations,
                "observations": (
                    _infrastructure_observations(request, outcome)
                    if outcome.infrastructure_failure is not None
                    else [observation_document(test) for test in outcome.tests]
                ),
            }
        )
        _create_immutable(destination, raw)
    elif source.resolve().is_relative_to(live_root.resolve()):
        durable_in_place(source)
        raw = source_raw
        destination = live_root / source.resolve().relative_to(live_root.resolve())
        for directory in destination.parents:  # a nested file's new folders, up to the attempt
            if not directory.is_relative_to(live_root.parent):
                break
            fsync_directory(directory)
    else:
        durable_copy(source, destination)
        raw = destination.read_bytes()
    return record_reference(destination, request.attempt_directory, raw, kind, request.attempt_id)


def _decode_cocotb_source(raw: bytes, selected: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        encoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SimulationRecordIntegrityError("Cocotb transport is not UTF-8") from exc
    try:
        decoded = decode_results_payload(json.loads(encoded))
    except (json.JSONDecodeError, BoundaryError) as exc:
        raise SimulationRecordIntegrityError("Cocotb transport is invalid") from exc
    names = selected or tuple(test.name for test in decoded.tests if test.name)
    return [
        {"test": name, "verdict": verdict, "detail": detail}
        for name, verdict, detail in reconcile(list(names), decoded)
    ]


def _build_result_ref(
    request: WorkExecutionRequest, build_directory: Path, result: BundleBuildResult
) -> dict[str, object]:
    build_document = result.document
    build_attempt = cast(Mapping[str, object], build_document["build_attempt"])
    build_attempt_id = cast(str, build_attempt["build_attempt_id"])
    bundle = build_document["bundle"]
    sharing = (
        cast(str, bundle["sharing"])
        if isinstance(bundle, Mapping)
        else (
            "shared_variant"
            if build_directory.is_relative_to(request.store.root / "build-variants")
            else "private_work_item"
        )
    )
    root = request.store.root if sharing == "shared_variant" else request.attempt_directory
    return record_reference(
        build_directory / "build-result.json",
        root,
        encode_bundle_build_result(result),
        "bundle_build_result",
        build_attempt_id,
    ) | {
        "build_attempt_id": build_attempt_id,
        "state": result.document["state"],
        "sharing": sharing,
    }


def record_reference(
    path: Path, root: Path, raw: bytes, kind: str, owner: str
) -> dict[str, object]:
    """Reference the record at *path* by digest, size and owner."""
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(raw),
        "sha256": content_digest(raw),
        "kind": kind,
        "owner": owner,
    }


def _regular_child(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise SimulationRecordIntegrityError("private image artifact escapes its root") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise SimulationRecordIntegrityError("private image artifact is not a regular file")
    return path


def _create_immutable(path: Path, raw: bytes) -> None:
    durable_create(path, raw)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "BuildScope",
    "OrdinaryHdlSerialExecutor",
    "blocked_result",
    "bundle_build_attempt",
    "bundle_verification",
    "capture_private_image",
    "observation_document",
    "publish_failed_build_result",
    "publish_ready_build_result",
    "record_reference",
    "runtime_input_documents",
    "settled_state",
    "simulation_attempt",
    "simulation_result",
]
