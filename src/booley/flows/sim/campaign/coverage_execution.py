"""One-attempt adapter for serial native coverage collection and merge."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from booley.flows.sim.campaign.bundle import authenticate_executable_snapshot
from booley.flows.sim.campaign_durability import durable_directory
from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
from booley.flows.sim.coverage_invocation import CoverageTargetPlan
from booley.flows.sim.coverage_reference import (
    authenticate_coverage_campaign_owner,
    resolve_coverage_campaign_reference,
)
from booley.flows.sim.coverage_transaction import CoverageTargetOutcome, run_coverage_target
from booley.flows.sim.execution.contract import (
    SimulationInfrastructureFailure,
    SimulationOptions,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.runtime_inputs import (
    RuntimeInputBinding,
    materialize_campaign_runtime_inputs,
)
from booley.flows.sim.verilator_coverage import (
    SimulationBuildRequest,
    SimulationBuildResult,
    SimulationCommandRequest,
    SimulationCommandResult,
    SimulationExecutionPort,
    SimulationRunRequest,
    SimulationRunResult,
)

from .codec import SimulationCampaignIntegrityError, encode_executable_snapshot
from .coordinator import (
    CampaignFailureContext,
    SerialWorkExecutor,
    WorkExecutionRequest,
    simulation_options_from_policy,
)
from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutableSnapshot,
    SimulationResult,
    SimulatorBundle,
    simulation_status_from_observations,
)
from .run_directory import RunDirectory, claimed_run_directory
from .serial_execution import (
    _attempt,
    _blocked_result,
    _build_attempt,
    _capture_private_image,
    _create_snapshot,
    _now,
    _observation,
    _publish_failed_build_result,
    _publish_ready_build_result,
    _record_ref,
    _result,
    _runtime_documents,
)


class _NoProgress:
    def completed(self, outcome: CoverageTargetOutcome) -> None:
        del outcome


class _CoverageAggregateError(RuntimeError):
    """A nested Coverage Campaign failed with validated Target facts."""

    def __init__(self, message: str, detail: Mapping[str, object]) -> None:
        super().__init__(message)
        self.detail = _validated_failure_detail(detail)

    @property
    def evaluation_status(self) -> str:
        return cast(str, self.detail["evaluation"])


def _validated_failure_detail(detail: Mapping[str, object]) -> Mapping[str, object]:
    simulation = detail.get("simulation")
    collection = detail.get("collection")
    evaluation = detail.get("evaluation")
    if simulation not in {
        "pass",
        "fail",
        "timeout",
        "crash",
        "aborted",
        "inconclusive",
        "not_run",
    }:
        if simulation == "elab_error":
            simulation = "fail"
        else:
            raise SimulationCampaignIntegrityError(
                "native coverage collection returned an invalid simulation status"
            )
    if collection == "collector_error":
        collection = "infrastructure_error"
    if collection not in {"complete", "partial", "incompatible", "infrastructure_error"}:
        raise SimulationCampaignIntegrityError(
            "native coverage collection returned an invalid collection status"
        )
    if evaluation not in {"pass", "fail", "blocked", "not_requested"}:
        raise SimulationCampaignIntegrityError(
            "native coverage collection returned an invalid evaluation status"
        )
    validated = {
        **detail,
        "simulation": simulation,
        "collection": collection,
        "evaluation": evaluation,
        "abort_remaining": True,
    }
    validated.pop("coverage_campaign", None)
    return validated


@dataclass(slots=True)
class _CapturedBuild:
    directory: Path
    result: BundleBuildResult
    bundle: SimulatorBundle
    snapshot_root: Path
    snapshot: ExecutableSnapshot
    snapshot_reference: dict[str, object]
    pre_launch_sha256: str


class _CapturingExecution:
    """Capture the exact collector-built simulator before its first run."""

    def __init__(
        self,
        delegate: SimulationExecutionPort,
        ready: Callable[[float], _CapturedBuild],
        staged: Callable[[_CapturedBuild], tuple[RuntimeInputBinding, ...]],
    ):
        self._delegate = delegate
        self._ready = ready
        self._staged = staged
        self.captured: _CapturedBuild | None = None
        self.bindings: tuple[RuntimeInputBinding, ...] = ()
        self.build_result: SimulationBuildResult | None = None
        self.build_elapsed = 0.0

    def build(self, request: SimulationBuildRequest) -> SimulationBuildResult:
        started = time.monotonic()
        try:
            result = self._delegate.build(request)
        except OSError as exc:
            result = SimulationBuildResult(False, str(exc), infrastructure_error=True)
        self.build_elapsed = time.monotonic() - started
        self.build_result = result
        if result.success:
            self.captured = self._ready(self.build_elapsed)
            self.bindings = self._staged(self.captured)
        return result

    def run(self, request: SimulationRunRequest) -> SimulationRunResult:
        return self._delegate.run(request)

    def command(self, request: SimulationCommandRequest) -> SimulationCommandResult:
        return self._delegate.command(request)


def _raise_if_coverage_aborted(
    outcome: CoverageTargetOutcome, build_result: SimulationBuildResult | None
) -> None:
    if not outcome.abort_remaining:
        return
    # A collector failure without its own error reports the simulator build output.
    message = outcome.detail.get("error")
    if message is None and build_result is not None:
        message = build_result.output
    raise _CoverageAggregateError(
        str(message or "native coverage collection failed"),
        outcome.detail,
    )


class CoverageAggregateExecutor(SerialWorkExecutor):
    """Execute one complete Coverage Campaign as one Simulation Work Item."""

    def __init__(
        self,
        *,
        plans: Mapping[str, CoverageTargetPlan],
        execution_factory: Callable[
            [CoverageTargetPlan, SimulationOptions, tuple[str, ...], str],
            SimulationExecutionPort,
        ],
        publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._plans = dict(plans)
        self._execution_factory = execution_factory
        self._publication_checkpoint = publication_checkpoint or (lambda _boundary: None)
        self._prepared: dict[Path, RunDirectory] = {}

    def prepare_attempt(self, request: WorkExecutionRequest) -> None:
        """Publish the exact coverage attempt before child admission."""
        attempt, _run_directory = _attempt(request)
        self._publication_checkpoint("before:simulation_attempt")
        request.store.publish_attempt(request.attempt_directory, attempt)
        self._publication_checkpoint("after:simulation_attempt")
        self._prepared[request.attempt_directory] = _run_directory

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        """Collect, merge and commit one nested Coverage Campaign atomically."""
        if request.work_item["kind"] != "coverage_aggregate":
            raise SimulationCampaignIntegrityError(
                "coverage aggregate executor received another work-item kind"
            )
        if request.attempt_directory not in self._prepared:
            self.prepare_attempt(request)
        run_directory = self._prepared.pop(request.attempt_directory)
        started = time.monotonic()
        build_directory = request.attempt_directory / "private-build"
        durable_directory(build_directory)
        build_directory.chmod(0o700)
        build_attempt = _build_attempt(request)
        request.store.publish_build_attempt(build_directory, build_attempt)
        execution = self._execution(request)
        identity = _attempt_identity(request)
        with (
            claimed_run_directory(run_directory, identity=identity) as run_cwd,
            ExitStack() as stack,
        ):
            capturing = _CapturingExecution(
                execution,
                lambda elapsed: self._capture_build(
                    request, build_directory, build_attempt, execution, elapsed
                ),
                lambda captured: self._stage_attempt(
                    request, captured, execution, run_directory, run_cwd, stack
                ),
            )
            outcome = self._collect(request, capturing)
        failure = self._publish_missing_build(
            request, build_directory, build_attempt, outcome, capturing
        )
        if failure is not None:
            return failure
        _raise_if_coverage_aborted(outcome, capturing.build_result)
        if capturing.captured is None:
            detail = outcome.detail.get("error") or outcome.detail.get("collection")
            raise SimulationCampaignIntegrityError(
                "coverage collection has no authenticated simulator build"
                f" (build={capturing.build_result!r}, detail={detail!r})"
            )
        return _completed_result(request, capturing.captured, outcome, capturing.bindings, started)

    def _publish_missing_build(
        self,
        request: WorkExecutionRequest,
        build_directory: Path,
        build_attempt: BundleBuildAttempt,
        outcome: CoverageTargetOutcome,
        capturing: _CapturingExecution,
    ) -> SimulationResult | None:
        if capturing.captured is not None:
            return None
        return _publish_coverage_build_failure(
            request,
            build_directory,
            build_attempt,
            outcome,
            capturing.build_result,
            capturing.build_elapsed,
            self._publication_checkpoint,
        )

    def _collect(
        self, request: WorkExecutionRequest, execution: SimulationExecutionPort
    ) -> CoverageTargetOutcome:
        target = cast(Mapping[str, str], request.manifest.document["target"])
        identity = f"{target['vlnv']}#{target['name']}"
        try:
            plan = self._plans[identity]
        except KeyError as exc:
            raise SimulationCampaignIntegrityError(
                "coverage plan is missing for campaign Target"
            ) from exc
        origin_invocation = request.store.root.parents[2]
        nested_plan = replace(
            plan,
            invocation_dir=origin_invocation,
            acceptance=None,
            started_at=_now(),
        )
        return run_coverage_target(
            nested_plan,
            execution,
            _NoProgress(),
            embedded_root=request.attempt_directory / "coverage-campaign",
            publication_checkpoint=self._publication_checkpoint,
        )

    def _execution(self, request: WorkExecutionRequest) -> SimulationExecutionPort:
        target = cast(Mapping[str, str], request.manifest.document["target"])
        identity = f"{target['vlnv']}#{target['name']}"
        try:
            plan = self._plans[identity]
        except KeyError as exc:
            raise SimulationCampaignIntegrityError(
                "coverage plan is missing for campaign Target"
            ) from exc
        options = simulation_options_from_policy(
            request.policy,
            trace=cast(bool, request.manifest.document["workload"]["trace"]),
        )
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        source_recipe = cast(Mapping[str, object], workload["source_recipe"])
        commands = tuple(cast(tuple[str, ...], source_recipe["pre_sim_commands"]))
        access = cast(str, workload["pre_sim_build_access"])
        return self._execution_factory(plan, options, commands, access)

    def _capture_build(
        self,
        request: WorkExecutionRequest,
        directory: Path,
        attempt: BundleBuildAttempt,
        execution: SimulationExecutionPort,
        elapsed: float,
    ) -> _CapturedBuild:
        source_root, paths = execution.authenticated_image()
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        declarations = cast(tuple[Mapping[str, str], ...], workload["runtime_inputs"])
        artifacts = _capture_private_image(paths, source_root, directory, declarations)
        result, bundle = _publish_ready_build_result(
            request, directory, attempt, artifacts, elapsed
        )
        snapshot_root = request.attempt_directory / "snapshot"
        snapshot = _create_snapshot(request, directory, result, bundle, snapshot_root)
        raw = encode_executable_snapshot(snapshot)
        reference = _record_ref(
            snapshot_root / "snapshot.json",
            request.attempt_directory,
            raw,
            "executable_snapshot_manifest",
            request.attempt_id,
        )
        pre_launch = authenticate_executable_snapshot(snapshot, snapshot_root)
        return _CapturedBuild(
            directory, result, bundle, snapshot_root, snapshot, reference, pre_launch
        )

    def _stage_attempt(
        self,
        request: WorkExecutionRequest,
        captured: _CapturedBuild,
        execution: SimulationExecutionPort,
        run_directory: RunDirectory,
        run_cwd: Path,
        stack: ExitStack,
    ) -> tuple[RuntimeInputBinding, ...]:
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        declarations = cast(tuple[dict[str, str], ...], workload["runtime_inputs"])
        self._publication_checkpoint("before:runtime_inputs")
        bindings = stack.enter_context(
            materialize_campaign_runtime_inputs(
                bundle_root=captured.directory,
                attempt_root=request.attempt_directory,
                run_cwd=run_cwd,
                declarations=declarations,
                owned_run_directory=run_directory.owned,
            )
        )
        execution.bind_authenticated_attempt(captured.snapshot_root, run_cwd)
        self._publication_checkpoint("after:runtime_inputs")
        return bindings


def _attempt_identity(request: WorkExecutionRequest) -> dict[str, object]:
    return {
        "campaign_id": request.manifest.document["campaign_id"],
        "work_item_id": request.work_item["work_item_id"],
        "attempt_id": request.attempt_id,
    }


def _completed_result(
    request: WorkExecutionRequest,
    captured: _CapturedBuild,
    outcome: CoverageTargetOutcome,
    bindings: tuple[RuntimeInputBinding, ...],
    started: float,
) -> SimulationResult:
    post_exit = authenticate_executable_snapshot(captured.snapshot, captured.snapshot_root)
    bundle = cast(Mapping[str, object], captured.result.document["bundle"])
    observations = [_coverage_observation(item) for item in _coverage_tests(outcome)]
    return _result(
        request,
        captured.directory,
        captured.result,
        state=_coverage_result_state(observations),
        bundle_id=captured.bundle.document["bundle_id"],
        snapshot={
            "manifest": captured.snapshot_reference,
            "bundle_manifest_sha256": bundle["manifest_sha256"],
            "pre_launch_sha256": captured.pre_launch_sha256,
            "post_exit_sha256": post_exit,
            "verified_after_exit": True,
        },
        observations=observations,
        elapsed=time.monotonic() - started,
        evidence=_coverage_evidence(request, outcome),
        runtime_inputs=_runtime_documents(request, bindings),
    )


def _publish_coverage_build_failure(
    request: WorkExecutionRequest,
    directory: Path,
    attempt: BundleBuildAttempt,
    outcome: CoverageTargetOutcome,
    build: SimulationBuildResult | None,
    elapsed: float,
    checkpoint: Callable[[str], None],
) -> SimulationResult | None:
    if build is None or build.success:
        detail = outcome.detail.get("error") or outcome.detail.get("collection")
        raise SimulationCampaignIntegrityError(
            "coverage collection has no authenticated simulator build"
            f" (build={build!r}, detail={detail!r})"
        )
    target = cast(Mapping[str, str], request.manifest.document["target"])
    workload = cast(Mapping[str, object], request.manifest.document["workload"])
    recipe = cast(Mapping[str, object], workload["build_recipe"])
    infrastructure = build.infrastructure_error
    tests = (
        ()
        if infrastructure
        else tuple(_coverage_test_outcome(item) for item in _coverage_tests(outcome))
    )
    failed = SimulationTargetOutcome(
        target=target["selector"],
        target_identity=f"{target['vlnv']}#{target['name']}",
        toplevel=cast(str, recipe["toplevel"]),
        eda_tool="verilator",
        passed=False,
        verdict="error" if infrastructure else "fail",
        elapsed_s=elapsed,
        tests=tests,
        infrastructure_failure=(
            SimulationInfrastructureFailure(
                "build_transport",
                build.reason or build.output or "coverage build failed",
                detail=build.output,
            )
            if infrastructure
            else None
        ),
    )
    result = _publish_failed_build_result(
        request,
        directory,
        attempt,
        failed,
        elapsed,
        infrastructure=infrastructure,
        checkpoint=checkpoint,
    )
    if infrastructure:
        return None
    return _blocked_result(request, directory, result, failed, elapsed)


def _coverage_result_state(observations: list[dict[str, object]]) -> str:
    executions = {str(item["execution"]) for item in observations}
    if "aborted" in executions:
        return "aborted"
    if "crash" in executions:
        return "crash"
    if "timeout" in executions:
        return "timeout"
    return "completed"


def _coverage_tests(outcome: CoverageTargetOutcome) -> tuple[Mapping[str, object], ...]:
    tests = outcome.detail.get("tests", ())
    if not isinstance(tests, tuple):
        raise SimulationCampaignIntegrityError("Coverage Campaign returned invalid test evidence")
    if not tests:
        raise SimulationCampaignIntegrityError("Coverage Campaign returned no test evidence")
    return cast(tuple[Mapping[str, object], ...], tests)


def _coverage_observation(item: Mapping[str, object]) -> dict[str, object]:
    execution = item.get("execution")
    return _observation(
        _coverage_test_outcome(item),
        execution="not_run" if execution == "not_run" else None,
    )


def _coverage_test_outcome(item: Mapping[str, object]) -> SimulationTestOutcome:
    name = item.get("name", item.get("test"))
    verdict = item.get("verdict", item.get("simulation_verdict"))
    if (
        not isinstance(name, str)
        or not name
        or verdict not in {"pass", "fail", "timeout", "crash", "inconclusive", "elab_error"}
    ):
        raise SimulationCampaignIntegrityError("Coverage Campaign test evidence is invalid")
    error_tail = item.get("error_tail")
    if not isinstance(error_tail, str):
        error_tail = "" if verdict == "pass" else f"coverage simulation {verdict}"
    test = SimulationTestOutcome(
        name=name,
        verdict=cast(str, verdict),  # type: ignore[arg-type]
        passed=verdict == "pass",
        timed_out=verdict == "timeout",
        crashed=verdict == "crash",
        elab_failed=verdict == "elab_error",
        inconclusive=verdict == "inconclusive",
        error_tail=error_tail,
        termination=cast(str, item.get("termination", "completed")),  # type: ignore[arg-type]
        failure_kind=cast(str, item.get("failure_kind", "")),
    )
    return test


def _coverage_evidence(
    request: WorkExecutionRequest, outcome: CoverageTargetOutcome
) -> list[dict[str, object]]:
    root = request.attempt_directory / "coverage-campaign"
    paths = [outcome.campaign_path]
    points = root / "coverage-points.jsonl.gz"
    if points.is_file():
        paths.append(points)
    references = []
    for path in paths:
        raw = path.read_bytes()
        references.append(
            _record_ref(
                path,
                request.attempt_directory,
                raw,
                "coverage_campaign_manifest"
                if path.name == "coverage.json"
                else "coverage_point_store",
                request.attempt_id,
            )
        )
    return references


def project_coverage_failure(
    context: CampaignFailureContext,
) -> tuple[dict[str, object], bool] | None:
    """Recover compact Target facts from one exact authenticated failed attempt."""
    try:
        detail = _failure_projection(context)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return detail, _safe_reference_matches_context(context)


def _failure_projection(context: CampaignFailureContext) -> dict[str, object]:
    document = context.result.document
    if not _result_matches_context(document, context):
        raise ValueError("Simulation result does not match failure context")
    campaign_path = _failure_campaign_path(context)
    raw = campaign_path.read_bytes()
    if not _nested_evidence_matches(document, context, raw):
        raise ValueError("nested Coverage Campaign evidence is invalid")
    campaign = load_coverage_campaign(
        campaign_path, DurableTargetIdentity(context.target_identity)
    ).campaign
    observations = cast(tuple[Mapping[str, object], ...], document["observations"])
    simulation = simulation_status_from_observations(observations)
    return {
        "target": context.target_selector,
        "passed": None if simulation == "not_run" else simulation == "pass",
        "simulation": simulation,
        "collection": campaign.collection["status"],
        "evaluation": campaign.evaluation["status"],
        "tests": [_failure_test(item) for item in observations],
        "abort_remaining": True,
    }


def _result_matches_context(
    document: Mapping[str, object], context: CampaignFailureContext
) -> bool:
    return (
        document["campaign_id"] == context.campaign_id
        and document["work_item_id"] == context.work_item_id
        and document["attempt_id"] == context.attempt_id
        and document["attempt_ordinal"] == context.attempt_ordinal
        and document["producer_invocation_id"] == context.producer_invocation_id
    )


def _failure_campaign_path(context: CampaignFailureContext) -> Path:
    _, work_ordinal, work_digest = context.work_item_id.split(":")
    return (
        context.target_root
        / "campaign"
        / "work-items"
        / f"{int(work_ordinal) + 1:04d}-{work_digest}"
        / "attempts"
        / f"{context.attempt_ordinal:04d}-{context.attempt_id}"
        / "coverage-campaign"
        / "coverage.json"
    )


def _nested_evidence_matches(
    document: Mapping[str, object], context: CampaignFailureContext, raw: bytes
) -> bool:
    evidence = cast(tuple[Mapping[str, object], ...], document["evidence"])
    nested = [item for item in evidence if item["kind"] == "coverage_campaign_manifest"]
    return (
        len(nested) == 1
        and nested[0]["path"] == "coverage-campaign/coverage.json"
        and nested[0]["bytes"] == len(raw)
        and nested[0]["sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        and nested[0]["owner"] == context.attempt_id
    )


def _failure_test(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "name": item["test"],
        "verdict": item["functional"],
        "passed": item["functional"] == "pass",
        "failure_kind": item["failure_class"] or "",
        "error_tail": cast(Mapping[str, object], item["detail"]).get("reason", ""),
    }


def _safe_reference_matches_context(context: CampaignFailureContext) -> bool:
    try:
        return _reference_matches_context(context)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _reference_matches_context(context: CampaignFailureContext) -> bool:
    if context.coverage_reference is None:
        return False
    resolved = resolve_coverage_campaign_reference(context.target_root / "coverage.json")
    authenticate_coverage_campaign_owner(resolved)
    document = resolved.reference.document
    return (
        document == context.coverage_reference.document
        and document["simulation_campaign_id"] == context.campaign_id
        and document["simulation_work_item_id"] == context.work_item_id
        and document["simulation_attempt_id"] == context.attempt_id
        and document["producer_invocation_id"] == context.producer_invocation_id
    )


__all__ = ["CoverageAggregateExecutor", "project_coverage_failure"]
