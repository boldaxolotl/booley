"""Production bridge from durable campaign work to one legacy HDL execution.

This Phase-2 adapter deliberately preserves the existing leaf execution path.
It adds the manifest-first attempt protocol around one ordinary-HDL work item
and captures the engine-authorized private image as attempt-owned evidence.
"""

from __future__ import annotations

import hashlib
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from booley.flows.sim.build_session import project_compile_surface
from booley.flows.sim.campaign.bundle import (
    authenticate_executable_snapshot,
    create_executable_snapshot,
)
from booley.flows.sim.campaign_durability import (
    durable_copy,
    durable_create,
    durable_directory,
)
from booley.flows.sim.execution.contract import (
    SimulationInfrastructureFailure,
    SimulationOptions,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.execution.engine import (
    ProcessInvoker,
    SimulationExecution,
    simulation_target_environment,
)
from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.flows.sim.runtime_inputs import (
    RuntimeInputBinding,
    materialize_campaign_runtime_inputs,
)
from booley.targets.catalog import TargetCatalog

from .codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
    decode_simulation_attempt,
    decode_simulation_result,
    decode_simulator_bundle,
    encode_bundle_build_attempt,
    encode_bundle_build_result,
    encode_executable_snapshot,
    encode_simulator_bundle,
)
from .coordinator import SerialWorkExecutor, WorkExecutionRequest
from .model import (
    AssertionObservation,
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutionObservation,
    FailureClass,
    FunctionalObservation,
    SimulationAttempt,
    SimulationResult,
    SimulatorBundle,
    grade_observations,
)
from .planning import manifest_digest
from .run_directory import RunDirectory, claimed_run_directory, expand_run_directory


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
    compiled_group: _OrdinaryGroup | None


@dataclass(frozen=True, slots=True)
class _GroupInputs:
    handle: object
    names: tuple[str, ...]
    options: SimulationOptions
    execution: SimulationExecution
    workload: Mapping[str, object]
    access: str


class _OrdinaryGroup(Protocol):
    build_root: Path
    artifact_paths: tuple[Path, ...]

    def planning_disclosure(self) -> dict[str, object]: ...
    def compile(self) -> object: ...
    def finish_build_failure(self) -> SimulationTargetOutcome: ...
    def build_recovery_document(self) -> dict[str, object]: ...
    def reuse_compilation_from(self, source: object) -> None: ...
    def bind_authenticated_bundle(self, evidence: Mapping[str, object]) -> None: ...
    def launch_snapshot(self, snapshot_root: Path, run_cwd: Path) -> SimulationTargetOutcome: ...


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
        self._shared_failure: dict[
            tuple[Path, str, int], tuple[Path, BundleBuildResult, SimulationTargetOutcome]
        ] = {}
        self._shared_locks: dict[tuple[Path, str, int], threading.Lock] = {}
        self._shared_locks_gate = threading.Lock()
        self._prepared_attempts: dict[Path, RunDirectory] = {}
        self._prepared_attempts_gate = threading.Lock()

    def prepare_attempt(self, request: WorkExecutionRequest) -> None:
        """Publish the child-bound attempt before its slot waiter becomes visible."""
        attempt, run_directory = _attempt(request)
        self._publication_checkpoint("before:simulation_attempt")
        request.store.publish_attempt(request.attempt_directory, attempt)
        self._publication_checkpoint("after:simulation_attempt")
        with self._prepared_attempts_gate:
            self._prepared_attempts[request.attempt_directory] = run_directory

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        item = request.work_item
        if item["kind"] not in {"ordinary_hdl", "cocotb_batch"}:
            raise SimulationCampaignIntegrityError(
                "HDL campaign executor supports only ordinary or Cocotb work items"
            )
        with self._prepared_attempts_gate:
            run_directory = self._prepared_attempts.pop(request.attempt_directory, None)
        if run_directory is None:
            self.prepare_attempt(request)
            with self._prepared_attempts_gate:
                run_directory = self._prepared_attempts.pop(request.attempt_directory)
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        if workload["pre_sim_build_access"] == "immutable" and _sharing_eligible(request):
            return self._execute_shared(request, run_directory)
        build_directory = request.attempt_directory / "private-build"
        durable_directory(build_directory)
        build_directory.chmod(0o700)
        build_attempt = _build_attempt(request)
        self._publication_checkpoint("before:build_attempt")
        request.store.publish_build_attempt(build_directory, build_attempt)
        self._publication_checkpoint("after:build_attempt")
        return self._execute_group(request, build_directory, build_attempt, run_directory)

    def _execute_shared(
        self, request: WorkExecutionRequest, run_directory: RunDirectory
    ) -> SimulationResult:
        """Compile one campaign/invocation variant and launch one isolated item."""
        key = (
            request.store.root,
            cast(str, request.work_item["build_variant_id"]),
            request.producer_invocation_id,
        )
        started = time.monotonic()
        inputs = self._group_inputs(request)
        handle, names = inputs.handle, inputs.names
        execution, workload = inputs.execution, inputs.workload
        with self._shared_lock(key):
            failed = self._shared_failure.get(key)
            if failed is not None:
                directory, result, outcome = failed
                return _blocked_result(
                    request, directory, result, outcome, time.monotonic() - started
                )
            ready = self._recover_shared(request, key, handle, names)
            recovered_failure = self._shared_failure.get(key)
            if recovered_failure is not None:
                directory, result, outcome = recovered_failure
                return _blocked_result(
                    request, directory, result, outcome, time.monotonic() - started
                )
            built_now = False
            if ready is None:
                built = self._build_shared(
                    request, key, handle, names, execution, workload, started
                )
                if isinstance(built, SimulationResult):
                    return built
                ready = built
                built_now = True
        return self._launch_shared(
            request,
            run_directory,
            ready,
            built_now,
            inputs,
            started,
        )

    def _launch_shared(self, request, run_directory, ready, built_now, inputs, started):
        handle, names = inputs.handle, inputs.names
        execution = inputs.execution
        launch_group = (
            ready.compiled_group
            if built_now
            else self._prepare_shared_launch(request, ready, execution, handle, names)
        )
        assert launch_group is not None
        identity = {
            "campaign_id": cast(str, request.manifest.document["campaign_id"]),
            "work_item_id": cast(str, request.work_item["work_item_id"]),
            "attempt_id": request.attempt_id,
        }
        with claimed_run_directory(run_directory, identity=identity) as run_cwd:
            return self._run_ready_group(
                request,
                ready.directory,
                run_cwd,
                _ReadyLaunch(
                    (ready.result, ready.bundle, launch_group),
                    (handle, names, inputs.options),
                    (inputs.workload, inputs.access, run_directory, started),
                ),
            )

    def _shared_lock(self, key: tuple[Path, str, int]) -> threading.Lock:
        with self._shared_locks_gate:
            return self._shared_locks.setdefault(key, threading.Lock())

    def _recover_shared(self, request, key, handle, names) -> _SharedReady | None:
        ready = self._shared_ready.get(key)
        if ready is not None:
            return ready
        recovered = request.store.recover_shared_build(key[1], key[2])
        if recovered is None:
            return None
        if recovered.result.document["state"] == "design_failure":
            outcome = _recovered_build_failure(handle, names, recovered.result)
            self._shared_failure[key] = (recovered.directory, recovered.result, outcome)
            return None
        assert recovered.bundle is not None and recovered.build_execution is not None
        ready = _SharedReady(
            recovered.directory,
            recovered.result,
            recovered.bundle,
            recovered.build_execution,
            None,
        )
        self._shared_ready[key] = ready
        return ready

    def _build_shared(
        self, request, key, handle, names, execution, workload, started
    ) -> _SharedReady | SimulationResult:
        build_id = str(uuid.uuid4())
        ordinal, directory = request.store.allocate_build_attempt_directory(key[1], build_id)
        attempt = _shared_build_attempt(request, build_id, ordinal)
        self._publication_checkpoint("before:build_attempt")
        request.store.publish_build_attempt(directory, attempt)
        self._publication_checkpoint("after:build_attempt")
        with execution.ordinary_group(handle, names) as group:
            _authenticate_planning_disclosure(request, group)
            build = group.compile()
            if not build.passed:
                return self._shared_build_failure(request, key, directory, attempt, group, started)
            build_execution = group.build_recovery_document()
            execution_ref = _capture_build_execution(directory, build_id, build_execution)
            self._publication_checkpoint("before:bundle_evidence")
            artifacts = _capture_private_image(
                group.artifact_paths,
                group.build_root,
                directory,
                cast(tuple[Mapping[str, str], ...], workload["runtime_inputs"]),
            )
            self._publication_checkpoint("after:bundle_evidence")
        self._publication_checkpoint("before:build_result")
        result, bundle = _publish_ready_shared_build_result(
            request,
            directory,
            attempt,
            artifacts,
            execution_ref,
            time.monotonic() - started,
        )
        self._publication_checkpoint("after:build_result")
        ready = _SharedReady(directory, result, bundle, build_execution, group)
        self._shared_ready[key] = ready
        return ready

    def _shared_build_failure(
        self, request, key, directory, attempt, group, started
    ) -> SimulationResult:
        outcome = group.finish_build_failure()
        infrastructure = outcome.infrastructure_failure is not None
        result = _publish_failed_build_result(
            request,
            directory,
            attempt,
            outcome,
            time.monotonic() - started,
            infrastructure=infrastructure,
            checkpoint=self._publication_checkpoint,
        )
        if infrastructure:
            raise SimulationCampaignIntegrityError(
                outcome.infrastructure_failure.detail or outcome.infrastructure_failure.message
            )
        self._shared_failure[key] = (directory, result, outcome)
        return _blocked_result(request, directory, result, outcome, time.monotonic() - started)

    @staticmethod
    def _prepare_shared_launch(request, ready, execution, handle, names):
        with execution.ordinary_group(handle, names) as group:
            _authenticate_planning_disclosure(request, group)
            if ready.compiled_group is None:
                group.bind_authenticated_bundle(ready.build_execution)
            else:
                group.reuse_compilation_from(ready.compiled_group)
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
        handle, names, options = inputs.handle, inputs.names, inputs.options
        execution, workload, access = inputs.execution, inputs.workload, inputs.access
        with execution.ordinary_group(handle, names) as group:
            prepared = self._prepare_bundle_artifacts(
                request,
                build_directory,
                build_attempt,
                handle,
                group,
                names,
                options,
                workload,
                access,
                started,
            )
            if isinstance(prepared, SimulationResult):
                return prepared
            bundle_artifacts = prepared
        elapsed = time.monotonic() - started
        self._publication_checkpoint("before:build_result")
        build_result, bundle = _publish_ready_build_result(
            request, build_directory, build_attempt, bundle_artifacts, elapsed
        )
        self._publication_checkpoint("after:build_result")
        identity = {
            "campaign_id": cast(str, request.manifest.document["campaign_id"]),
            "work_item_id": cast(str, request.work_item["work_item_id"]),
            "attempt_id": request.attempt_id,
        }
        with claimed_run_directory(run_directory, identity=identity) as run_cwd:
            return self._run_ready_group(
                request,
                build_directory,
                run_cwd,
                _ReadyLaunch(
                    (build_result, bundle, group),
                    (handle, names, options),
                    (workload, access, run_directory, started),
                ),
            )

    def _prepare_bundle_artifacts(
        self,
        request,
        build_directory,
        build_attempt,
        handle,
        group,
        names,
        options,
        workload,
        access,
        started,
    ) -> list[dict[str, object]] | SimulationResult:
        _authenticate_planning_disclosure(request, group)
        if access == "legacy-per-test":
            failure = self._legacy_hook_failure(
                request,
                build_directory,
                build_attempt,
                handle,
                group,
                names,
                options,
                started,
            )
            if failure is not None:
                return failure
        build = group.compile()
        if not build.passed:
            return self._compile_failure(request, build_directory, build_attempt, group, started)
        self._publication_checkpoint("before:bundle_evidence")
        artifacts = _capture_private_image(
            group.artifact_paths,
            group.build_root,
            build_directory,
            cast(tuple[Mapping[str, str], ...], workload["runtime_inputs"]),
        )
        self._publication_checkpoint("after:bundle_evidence")
        return artifacts

    def _group_inputs(self, request: WorkExecutionRequest):
        target = cast(Mapping[str, str], request.manifest.document["target"])
        handle = request.target_handle or TargetCatalog.build(request.project_root).select(
            target["selector"], for_flow="sim"
        )
        if handle.identity != f"{target['vlnv']}#{target['name']}":
            raise SimulationCampaignIntegrityError(
                "current Target identity disagrees with campaign manifest"
            )
        selection = cast(Mapping[str, object], request.work_item["selection"])
        names = cast(tuple[str, ...], selection["names"])
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        options = SimulationOptions(
            trace=cast(bool, workload["trace"]),
            timeout_ms=round(request.policy.timeout_seconds * 1000)
            if request.policy.timeout_seconds is not None
            else None,
            result_verbosity=request.policy.result_verbosity,
        )
        execution = (
            self._execution_factory(options)
            if self._execution_factory
            else (SimulationExecution(invoke=self._invoke, options=options))
        )
        return _GroupInputs(
            handle,
            names,
            options,
            execution,
            workload,
            cast(str, workload["pre_sim_build_access"]),
        )

    def _legacy_hook_failure(
        self,
        request,
        build_directory,
        build_attempt,
        handle,
        group,
        names,
        options,
        started,
    ) -> SimulationResult | None:
        pre_sim = _run_hook(handle, group.build_root, names, options, None, True)
        if pre_sim is None or pre_sim.status == "passed":
            return None
        outcome = _hook_failure_outcome(handle.selector, names, pre_sim)
        elapsed = time.monotonic() - started
        if pre_sim.status == "spawn_error":
            outcome = _hook_infrastructure_outcome(outcome, pre_sim.detail)
            result = _publish_failed_build_result(
                request,
                build_directory,
                build_attempt,
                outcome,
                elapsed,
                infrastructure=True,
                checkpoint=self._publication_checkpoint,
            )
            return _blocked_result(
                request,
                build_directory,
                result,
                outcome,
                elapsed,
                infrastructure=True,
            )
        result = _publish_failed_build_result(
            request,
            build_directory,
            build_attempt,
            outcome,
            elapsed,
            infrastructure=False,
            checkpoint=self._publication_checkpoint,
        )
        return _blocked_result(request, build_directory, result, outcome, elapsed)

    def _compile_failure(
        self,
        request,
        build_directory,
        build_attempt,
        group,
        started,
    ) -> SimulationResult:
        outcome = group.finish_build_failure()
        elapsed = time.monotonic() - started
        infrastructure = outcome.infrastructure_failure is not None
        result = _publish_failed_build_result(
            request,
            build_directory,
            build_attempt,
            outcome,
            elapsed,
            infrastructure=infrastructure,
            checkpoint=self._publication_checkpoint,
        )
        return _blocked_result(
            request,
            build_directory,
            result,
            outcome,
            elapsed,
            infrastructure=infrastructure,
        )

    def _run_ready_group(
        self,
        request,
        build_directory,
        run_cwd,
        ready: _ReadyLaunch,
    ) -> SimulationResult:
        build_result, bundle, group = ready.build
        handle, names, options = ready.simulation
        workload, access, run_directory, started = ready.policy
        declarations = cast(tuple[dict[str, str], ...], workload["runtime_inputs"])
        self._publication_checkpoint("before:runtime_inputs")
        with materialize_campaign_runtime_inputs(
            bundle_root=build_directory,
            attempt_root=request.attempt_directory,
            run_cwd=run_cwd,
            declarations=declarations,
            owned_run_directory=run_directory.owned,
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
            )
            if failure is not None:
                return failure
            return _launch_snapshot(
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
        tuple[Mapping[str, object], ...],
        request.manifest.document["planning_disclosures"],
    )
    ordinal = cast(int, request.work_item["ordinal"])
    if disclosures and (
        ordinal >= len(disclosures)
        or canonical_json_bytes(group.planning_disclosure())  # type: ignore[attr-defined]
        != canonical_json_bytes(disclosures[ordinal])
    ):
        raise SimulationCampaignIntegrityError(
            "prepared generator source closure disagrees with campaign plan"
        )


def _sharing_eligible(request: WorkExecutionRequest) -> bool:
    variant_id = request.work_item["build_variant_id"]
    variants = cast(tuple[Mapping[str, object], ...], request.manifest.document["build_variants"])
    variant = next(item for item in variants if item["build_variant_id"] == variant_id)
    return cast(bool, variant["sharing_eligible"])


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
) -> SimulationResult | None:
    if access != "immutable":
        return None
    surface = project_compile_surface(request.project_root)
    pre_sim = _run_hook(handle, group.build_root, names, options, run_cwd, False)
    if project_compile_surface(request.project_root) != surface:
        raise SimulationCampaignIntegrityError(
            "Project compile inputs changed during immutable Pre-Sim Commands"
        )
    if pre_sim is None or pre_sim.status == "passed":
        return None
    if pre_sim.status == "spawn_error":
        raise SimulationCampaignIntegrityError(
            pre_sim.detail or "Pre-Sim Commands could not start"
        )
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


def _attempt(request: WorkExecutionRequest) -> tuple[SimulationAttempt, RunDirectory]:
    workload = cast(Mapping[str, object], request.manifest.document["workload"])
    run_cwd = cast(Mapping[str, object], workload["run_cwd"])
    configured = cast(str, run_cwd["configured"])
    target = cast(Mapping[str, str], request.manifest.document["target"])
    run = expand_run_directory(
        configured,
        project_root=request.project_root,
        campaign_id=cast(str, request.manifest.document["campaign_id"]),
        target_key=target["selector"].replace("/", "%2F"),
        work_item_key=request.store.work_item_directory(
            cast(str, request.work_item["work_item_id"])
        ).name,
        attempt_key=f"{request.attempt_ordinal:04d}-{request.attempt_id}",
    )
    document = {
        "$schema": "booley.simulation-attempt/v1",
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
        "child_entry_sha256": request.child_entry_sha256,
        "pre_sim_build_access": workload["pre_sim_build_access"],
        "policy": {
            "timeout_seconds": (
                round(request.policy.timeout_seconds)
                if request.policy.timeout_seconds is not None
                else None
            ),
            "no_kill": request.policy.no_kill,
            "diagnostic": request.policy.diagnostic,
        },
        "started_at": _now(),
    }
    return decode_simulation_attempt(canonical_json_bytes(document)), run


def _run_hook(
    handle: object,
    build_root: Path,
    names: tuple[str, ...],
    options: SimulationOptions,
    run_cwd: Path | None,
    expose_build_root: bool,
):
    return run_pre_sim_commands(
        handle,  # type: ignore[arg-type]
        test_names=names,
        build_root=build_root,
        eda_tool=cast(str, getattr(handle, "eda_tool", "")),
        timeout_s=max(1, (options.timeout_ms or 600_000) // 1000),
        simulator_environment=simulation_target_environment(handle),  # type: ignore[arg-type]
        run_cwd=str(run_cwd) if run_cwd is not None else None,
        working_directory=run_cwd,
        expose_build_root=expose_build_root,
    )


def _hook_failure_outcome(
    target: str, names: tuple[str, ...], evidence: object
) -> SimulationTargetOutcome:
    detail = cast(str, getattr(evidence, "detail", ""))
    tests = tuple(
        SimulationTestOutcome(
            name=name or target,
            verdict="elab_error",
            passed=False,
            elab_failed=True,
            error_tail=detail or "Pre-Sim Commands failed",
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
            "pre_sim_spawn",
            "Pre-Sim Commands could not start",
            detail=detail,
        ),
    )


def _recovered_build_failure(
    handle: object,
    names: tuple[str, ...],
    result: BundleBuildResult,
) -> SimulationTargetOutcome:
    observation = cast(Mapping[str, object], result.document["observation"])
    detail = cast(Mapping[str, object], observation["detail"])
    text = cast(str, detail.get("text", observation["message"]))
    tests = tuple(
        SimulationTestOutcome(
            name=name,
            verdict="elab_error",
            passed=False,
            elab_failed=True,
            error_tail=text,
        )
        for name in (names or (cast(str, getattr(handle, "selector", "simulation")),))
    )
    return SimulationTargetOutcome(
        target=cast(str, getattr(handle, "selector", "simulation")),
        target_identity=cast(str, getattr(handle, "identity", "")),
        toplevel="",
        eda_tool=cast(str, getattr(handle, "eda_tool", "")),
        passed=False,
        verdict="fail",
        elapsed_s=cast(float, result.document["elapsed_seconds"]),
        tests=tests,
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
    fallback = (
        ""
        if selection["kind"] == "unfiltered"
        else cast(str, request.manifest.document["target"]["selector"])
    )
    tests = tuple(
        SimulationTestOutcome(
            name=name,
            verdict="elab_error",
            passed=False,
            elab_failed=True,
            error_tail=detail,
        )
        for name in (names or (fallback,))
    )
    return _result(
        request,
        build_directory,
        build_result,
        state="setup_error",
        bundle_id=bundle.document["bundle_id"],
        snapshot=None,
        observations=[_observation(test, execution="setup_error") for test in tests],
        elapsed=elapsed,
        evidence=[],
        runtime_inputs=_runtime_documents(request, bindings),
    )


def _runtime_documents(
    request: WorkExecutionRequest, bindings: tuple[RuntimeInputBinding, ...]
) -> list[dict[str, object]]:
    return [
        binding.result_document(
            reference_path=binding.authoritative_copy.relative_to(
                request.attempt_directory
            ).as_posix(),
            owner=request.attempt_id,
        )
        for binding in bindings
    ]


def _build_attempt(request: WorkExecutionRequest) -> BundleBuildAttempt:
    workload = cast(Mapping[str, object], request.manifest.document["workload"])
    eda = cast(Mapping[str, str], workload["eda"])
    document = {
        "$schema": "booley.bundle-build-attempt/v1",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt_id": request.attempt_id,
        "build_attempt_ordinal": 1,
        "producer_invocation_id": request.producer_invocation_id,
        "sharing": "private_work_item",
        "owner": {
            "work_item_id": request.work_item["work_item_id"],
            "simulation_attempt_id": request.attempt_id,
        },
        "tool_provenance": {
            "eda_kind": eda["kind"],
            "eda_version": eda["version"],
            "adapter_contract_version": workload["adapter_contract_version"],
        },
        "started_at": _now(),
    }
    return decode_bundle_build_attempt(canonical_json_bytes(document))


def _shared_build_attempt(
    request: WorkExecutionRequest, build_attempt_id: str, ordinal: int
) -> BundleBuildAttempt:
    workload = cast(Mapping[str, object], request.manifest.document["workload"])
    eda = cast(Mapping[str, str], workload["eda"])
    document = {
        "$schema": "booley.bundle-build-attempt/v1",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt_id": build_attempt_id,
        "build_attempt_ordinal": ordinal,
        "producer_invocation_id": request.producer_invocation_id,
        "sharing": "shared_variant",
        "owner": {"work_item_id": None, "simulation_attempt_id": None},
        "tool_provenance": {
            "eda_kind": eda["kind"],
            "eda_version": eda["version"],
            "adapter_contract_version": workload["adapter_contract_version"],
        },
        "started_at": _now(),
    }
    return decode_bundle_build_attempt(canonical_json_bytes(document))


def _common(request: WorkExecutionRequest, *, include_work_item: bool = True) -> dict[str, object]:
    fingerprints = cast(Mapping[str, str], request.manifest.document["fingerprints"])
    common: dict[str, object] = {
        "campaign_id": request.manifest.document["campaign_id"],
        "manifest_sha256": manifest_digest(request.manifest),
        "workload_sha256": fingerprints["workload_sha256"],
    }
    if include_work_item:
        common["work_item_id"] = request.work_item["work_item_id"]
    return common


def _publish_ready_build_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_attempt: BundleBuildAttempt,
    artifacts: list[dict[str, object]],
    elapsed: float,
) -> tuple[BundleBuildResult, SimulatorBundle]:
    bundle_id = str(uuid.uuid4())
    owner = {
        "work_item_id": request.work_item["work_item_id"],
        "simulation_attempt_id": request.attempt_id,
    }
    build_document = build_attempt.document
    inventory = _sha_value(artifacts)
    snapshot_inventory = _sha_value(
        [item for item in artifacts if item["kind"] != "runtime_input"]
    )
    bundle = decode_simulator_bundle(
        canonical_json_bytes(
            {
                "$schema": "booley.simulator-bundle/v1",
                **_common(request, include_work_item=False),
                "build_variant_id": request.work_item["build_variant_id"],
                "build_attempt_id": request.attempt_id,
                "bundle_id": bundle_id,
                "sharing": "private_work_item",
                "owner": owner,
                "tool_provenance": build_document["tool_provenance"],
                "created_at": _now(),
                "artifacts": artifacts,
                "inventory_sha256": inventory,
                "snapshot_inventory_sha256": snapshot_inventory,
            }
        )
    )
    bundle_path = build_directory / "evidence" / "bundle.json"
    _create_immutable(bundle_path, encode_simulator_bundle(bundle))
    build_result = _ready_build_result(
        request, build_attempt, bundle, bundle_path, build_directory, elapsed
    )
    request.store.publish_build_result(build_directory, build_result)
    return build_result, bundle


def _publish_ready_shared_build_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_attempt: BundleBuildAttempt,
    artifacts: list[dict[str, object]],
    execution_ref: dict[str, object],
    elapsed: float,
) -> tuple[BundleBuildResult, SimulatorBundle]:
    build_id = cast(str, build_attempt.document["build_attempt_id"])
    bundle_id = str(uuid.uuid4())
    bundle = _shared_bundle(request, build_attempt, build_id, bundle_id, artifacts)
    bundle_path = build_directory / "evidence" / "bundle.json"
    _create_immutable(bundle_path, encode_simulator_bundle(bundle))
    bundle_raw = encode_simulator_bundle(bundle)
    document = _shared_ready_result_document(
        request,
        build_directory,
        build_attempt,
        bundle_path,
        bundle_raw,
        bundle_id,
        artifacts,
        execution_ref,
        elapsed,
    )
    result = decode_bundle_build_result(canonical_json_bytes(document))
    request.store.publish_build_result(build_directory, result)
    return result, bundle


def _shared_bundle(request, attempt, build_id, bundle_id, artifacts) -> SimulatorBundle:
    document = {
        "$schema": "booley.simulator-bundle/v1",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt_id": build_id,
        "bundle_id": bundle_id,
        "sharing": "shared_variant",
        "owner": {"work_item_id": None, "simulation_attempt_id": None},
        "tool_provenance": attempt.document["tool_provenance"],
        "created_at": _now(),
        "artifacts": artifacts,
        "inventory_sha256": _sha_value(artifacts),
        "snapshot_inventory_sha256": _sha_value(
            [item for item in artifacts if item["kind"] != "runtime_input"]
        ),
    }
    return decode_simulator_bundle(canonical_json_bytes(document))


def _shared_ready_result_document(
    request,
    directory,
    attempt,
    bundle_path,
    bundle_raw,
    bundle_id,
    artifacts,
    execution_ref,
    elapsed,
):
    build_id = attempt.document["build_attempt_id"]
    return {
        "$schema": "booley.bundle-build-result/v1",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt": _record_ref(
            directory / "build-attempt.json",
            directory,
            encode_bundle_build_attempt(attempt),
            "bundle_build_attempt",
            build_id,
        )
        | {"build_attempt_id": build_id},
        "state": "ready",
        "phase": "ready",
        "finished_at": _now(),
        "elapsed_seconds": elapsed,
        "bundle": {
            "bundle_id": bundle_id,
            "manifest_path": bundle_path.relative_to(directory).as_posix(),
            "manifest_bytes": len(bundle_raw),
            "manifest_sha256": _sha_evidence_bytes(bundle_raw),
            "sharing": "shared_variant",
            "artifacts": artifacts,
        },
        "observation": None,
        "evidence": [execution_ref],
    }


def _capture_build_execution(
    directory: Path, build_attempt_id: str, document: Mapping[str, object]
) -> dict[str, object]:
    raw = canonical_json_bytes(document)
    path = directory / "evidence" / "build-execution.json"
    _create_immutable(path, raw)
    return _record_ref(
        path,
        directory,
        raw,
        "simulation_build_execution",
        build_attempt_id,
    )


def _ready_build_result(
    request: WorkExecutionRequest,
    build_attempt: BundleBuildAttempt,
    bundle: SimulatorBundle,
    bundle_path: Path,
    build_directory: Path,
    elapsed: float,
) -> BundleBuildResult:
    bundle_raw = encode_simulator_bundle(bundle)
    document = {
        "$schema": "booley.bundle-build-result/v1",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt": _record_ref(
            build_directory / "build-attempt.json",
            build_directory,
            encode_bundle_build_attempt(build_attempt),
            "bundle_build_attempt",
            request.attempt_id,
        )
        | {"build_attempt_id": request.attempt_id},
        "state": "ready",
        "phase": "ready",
        "finished_at": _now(),
        "elapsed_seconds": elapsed,
        "bundle": {
            "bundle_id": bundle.document["bundle_id"],
            "manifest_path": bundle_path.relative_to(build_directory).as_posix(),
            "manifest_bytes": len(bundle_raw),
            "manifest_sha256": _sha_evidence_bytes(bundle_raw),
            "sharing": "private_work_item",
            "artifacts": [dict(item) for item in bundle.document["artifacts"]],
        },
        "observation": None,
        "evidence": [],
    }
    return decode_bundle_build_result(canonical_json_bytes(document))


def _publish_failed_build_result(
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
        request,
        build_directory,
        build_attempt,
        outcome,
        elapsed,
        infrastructure=infrastructure,
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
        "$schema": "booley.bundle-build-result/v1",
        **_common(request, include_work_item=False),
        "build_variant_id": request.work_item["build_variant_id"],
        "build_attempt": _record_ref(
            build_directory / "build-attempt.json",
            build_directory,
            encode_bundle_build_attempt(build_attempt),
            "bundle_build_attempt",
            build_attempt_id,
        )
        | {"build_attempt_id": build_attempt_id},
        "state": state,
        "phase": "storage" if infrastructure else "elaboration",
        "finished_at": _now(),
        "elapsed_seconds": elapsed,
        "bundle": None,
        "observation": {
            "class": "infrastructure" if infrastructure else "design",
            "code": failure.kind if failure is not None else "elaboration",
            "message": (failure.message if failure is not None else "Simulation build failed"),
            "detail": {"text": detail[:1536]},
        },
        "evidence": [],
    }
    return decode_bundle_build_result(canonical_json_bytes(document))


def _blocked_result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_result: BundleBuildResult,
    outcome: SimulationTargetOutcome,
    elapsed: float,
    *,
    infrastructure: bool = False,
) -> SimulationResult:
    observations = [_observation(test, execution="blocked_by_build") for test in outcome.tests]
    selection = cast(Mapping[str, object], request.work_item["selection"])
    if selection["kind"] == "unfiltered":
        observations = observations[:1]
        observations[0]["test"] = None
    if infrastructure:
        for observation in observations:
            observation["failure_class"] = "infrastructure"
    return _result(
        request,
        build_directory,
        build_result,
        state="blocked_by_build",
        bundle_id=None,
        snapshot=None,
        observations=observations,
        elapsed=elapsed,
        evidence=[],
    )


def _launch_snapshot(
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
    snapshot_root = request.attempt_directory / "snapshot"
    publication_checkpoint("before:snapshot_manifest")
    snapshot = _create_snapshot(request, build_directory, build_result, bundle, snapshot_root)
    publication_checkpoint("after:snapshot_manifest")
    pre_launch = authenticate_executable_snapshot(snapshot, snapshot_root)
    publication_checkpoint("integrity:prelaunch_authentication")
    outcome = group.launch_snapshot(snapshot_root, run_cwd)  # type: ignore[attr-defined]
    post_exit = authenticate_executable_snapshot(snapshot, snapshot_root)
    publication_checkpoint("integrity:post_exit_authentication")
    if outcome.infrastructure_failure is not None:
        raise SimulationCampaignIntegrityError(
            outcome.infrastructure_failure.detail or outcome.infrastructure_failure.message
        )
    return _snapshot_result(
        request,
        build_directory,
        build_result,
        bundle,
        snapshot,
        snapshot_root,
        pre_launch,
        post_exit,
        outcome,
        bindings,
        started,
        publication_checkpoint,
    )


def _snapshot_result(
    request,
    build_directory,
    build_result,
    bundle,
    snapshot,
    snapshot_root,
    pre_launch,
    post_exit,
    outcome,
    bindings,
    started,
    publication_checkpoint,
) -> SimulationResult:
    snapshot_raw = encode_executable_snapshot(snapshot)
    snapshot_ref = _record_ref(
        snapshot_root / "snapshot.json",
        request.attempt_directory,
        snapshot_raw,
        "executable_snapshot_manifest",
        request.attempt_id,
    )
    observations = [_observation(test) for test in outcome.tests]
    state = _result_state(outcome.tests)
    publication_checkpoint("before:execution_evidence")
    evidence = _capture_outcome_evidence(request, outcome)
    publication_checkpoint("after:execution_evidence")
    return _result(
        request,
        build_directory,
        build_result,
        state=state,
        bundle_id=bundle.document["bundle_id"],
        snapshot={
            "manifest": snapshot_ref,
            "bundle_manifest_sha256": cast(Mapping[str, object], build_result.document["bundle"])[
                "manifest_sha256"
            ],
            "pre_launch_sha256": pre_launch,
            "post_exit_sha256": post_exit,
            "verified_after_exit": True,
        },
        observations=observations,
        elapsed=time.monotonic() - started,
        evidence=evidence,
        runtime_inputs=_runtime_documents(request, bindings),
    )


def _create_snapshot(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_result: BundleBuildResult,
    bundle: SimulatorBundle,
    snapshot_root: Path,
):
    fingerprints = cast(Mapping[str, str], request.manifest.document["fingerprints"])
    return create_executable_snapshot(
        bundle=bundle,
        bundle_root=build_directory,
        snapshot_root=snapshot_root,
        campaign_id=cast(str, request.manifest.document["campaign_id"]),
        manifest_sha256=manifest_digest(request.manifest),
        workload_sha256=fingerprints["workload_sha256"],
        work_item_id=cast(str, request.work_item["work_item_id"]),
        attempt_id=request.attempt_id,
        build_result=_build_result_ref(request, build_directory, build_result),
        created_at=_now(),
    )


def _result(
    request: WorkExecutionRequest,
    build_directory: Path,
    build_result: BundleBuildResult,
    *,
    state: str,
    bundle_id: object,
    snapshot: object,
    observations: list[dict[str, object]],
    elapsed: float,
    evidence: list[dict[str, object]],
    runtime_inputs: list[dict[str, object]] | None = None,
) -> SimulationResult:
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
    grade = max(grades, key=lambda item: precedence[item.value]).value
    document = {
        "$schema": "booley.simulation-result/v1",
        **_common(request),
        "attempt_id": request.attempt_id,
        "attempt_ordinal": request.attempt_ordinal,
        "producer_invocation_id": request.producer_invocation_id,
        "state": state,
        "build_result": _build_result_ref(request, build_directory, build_result),
        "bundle_id": bundle_id,
        "finished_at": _now(),
        "elapsed_seconds": elapsed,
        "executable_snapshot": snapshot,
        "runtime_inputs": runtime_inputs or [],
        "observations": observations,
        "grade": grade,
        "diagnostics": [
            {"severity": "warning", "code": "execution", "pointer": "", "message": item}
            for item in outcome_diagnostics_safe(evidence, request)
        ],
        "evidence": evidence,
    }
    return decode_simulation_result(canonical_json_bytes(document))


def outcome_diagnostics_safe(
    evidence: list[dict[str, object]], request: WorkExecutionRequest
) -> tuple[str, ...]:
    del evidence, request
    return ()


def _observation(
    test: SimulationTestOutcome, *, execution: str | None = None
) -> dict[str, object]:
    observed_execution = execution or (
        "timeout" if test.timed_out else "crash" if test.crashed else "completed"
    )
    blocked = observed_execution in {"blocked_by_build", "setup_error"}
    functional = (
        "not_observed"
        if blocked or observed_execution in {"timeout", "crash"}
        else "pass"
        if test.verdict == "pass"
        else "inconclusive"
        if test.inconclusive
        else "fail"
    )
    assertions = (
        "not_observed"
        if blocked or observed_execution in {"timeout", "crash"}
        else "dirty"
        if test.sva_errors
        else "clean"
    )
    return {
        "test": test.name or None,
        "execution": observed_execution,
        "failure_class": (
            "design" if blocked or observed_execution == "timeout" or not test.passed else None
        ),
        "functional": functional,
        "assertions": assertions,
        "assertion_count": test.sva_errors,
        "detail": {"reason": (test.reason or test.error_tail)[:1536]},
        "cycle_count": test.cycles if observed_execution == "completed" else None,
    }


def _result_state(tests: tuple[SimulationTestOutcome, ...]) -> str:
    if any(test.timed_out for test in tests):
        return "timeout"
    if any(test.crashed for test in tests):
        return "crash"
    return "completed"


def _capture_private_image(
    paths: tuple[Path, ...],
    source_root: Path,
    destination: Path,
    runtime_declarations: tuple[Mapping[str, str], ...],
) -> list[dict[str, object]]:
    if not paths:
        raise SimulationCampaignIntegrityError("successful build has no authenticated image")
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
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
        sources.append((source_root / declaration["source_artifact_path"], "runtime_input"))
    if not executable_assigned:
        raise SimulationCampaignIntegrityError("private image has no simulator executable")
    records = []
    for source, kind in sources:
        authenticated = _regular_child(source_root, source.relative_to(source_root).as_posix())
        target = destination / authenticated.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        durable_copy(authenticated, target)
        raw = target.read_bytes()
        records.append(
            {
                "path": target.relative_to(destination).as_posix(),
                "bytes": len(raw),
                "sha256": _sha_evidence_bytes(raw),
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
    references = []
    seen: set[Path] = set()
    for artifact in outcome.artifacts:
        source = Path(artifact.path)
        if source in seen or not source.is_file() or source.is_symlink():
            continue
        seen.add(source)
        destination = evidence_root / f"{len(references) + 1:04d}-{source.name}"
        durable_copy(source, destination)
        raw = destination.read_bytes()
        references.append(
            _record_ref(
                destination,
                request.attempt_directory,
                raw,
                "cocotb_results"
                if artifact.kind == "cocotb_results_json"
                else artifact.kind,
                request.attempt_id,
            )
        )
    return references


def _build_result_ref(
    request: WorkExecutionRequest,
    build_directory: Path,
    result: BundleBuildResult,
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
    return _record_ref(
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


def _record_ref(path: Path, root: Path, raw: bytes, kind: str, owner: str) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(raw),
        "sha256": _sha_evidence_bytes(raw),
        "kind": kind,
        "owner": owner,
    }


def _regular_child(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise SimulationCampaignIntegrityError("private image artifact escapes its root") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise SimulationCampaignIntegrityError("private image artifact is not a regular file")
    return path


def _create_immutable(path: Path, raw: bytes) -> None:
    durable_create(path, raw)


def _sha_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()


def _sha_evidence_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _sha_value(value: object) -> str:
    return _sha_bytes(canonical_json_bytes(value))


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["OrdinaryHdlSerialExecutor"]
