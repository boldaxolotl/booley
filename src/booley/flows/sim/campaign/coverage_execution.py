"""One-attempt adapter for serial native coverage collection and merge."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from booley.flows.sim.campaign.bundle import authenticate_executable_snapshot
from booley.flows.sim.campaign_durability import durable_directory
from booley.flows.sim.coverage_invocation import CoverageTargetPlan
from booley.flows.sim.coverage_transaction import CoverageTargetOutcome, run_coverage_target
from booley.flows.sim.execution.contract import SimulationOptions, SimulationTestOutcome
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
from .coordinator import SerialWorkExecutor, WorkExecutionRequest
from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutableSnapshot,
    SimulationResult,
    SimulatorBundle,
)
from .serial_execution import (
    _attempt,
    _build_attempt,
    _capture_private_image,
    _create_snapshot,
    _now,
    _observation,
    _publish_ready_build_result,
    _record_ref,
    _result,
)


class _NoProgress:
    def completed(self, outcome: CoverageTargetOutcome) -> None:
        del outcome


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

    def __init__(self, delegate: SimulationExecutionPort, ready: Callable[[], _CapturedBuild]):
        self._delegate = delegate
        self._ready = ready
        self.captured: _CapturedBuild | None = None

    def build(self, request: SimulationBuildRequest) -> SimulationBuildResult:
        result = self._delegate.build(request)
        if result.success:
            self.captured = self._ready()
        return result

    def run(self, request: SimulationRunRequest) -> SimulationRunResult:
        return self._delegate.run(request)

    def command(self, request: SimulationCommandRequest) -> SimulationCommandResult:
        return self._delegate.command(request)


class CoverageAggregateExecutor(SerialWorkExecutor):
    """Execute one complete Coverage Campaign as one Simulation Work Item."""

    def __init__(
        self,
        *,
        plans: Mapping[str, CoverageTargetPlan],
        execution_factory: Callable[
            [CoverageTargetPlan, SimulationOptions], SimulationExecutionPort
        ],
        publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._plans = dict(plans)
        self._execution_factory = execution_factory
        self._publication_checkpoint = publication_checkpoint or (lambda _boundary: None)
        self._prepared: set[Path] = set()

    def prepare_attempt(self, request: WorkExecutionRequest) -> None:
        """Publish the exact coverage attempt before child admission."""
        attempt, _run_directory = _attempt(request)
        self._publication_checkpoint("before:simulation_attempt")
        request.store.publish_attempt(request.attempt_directory, attempt)
        self._publication_checkpoint("after:simulation_attempt")
        self._prepared.add(request.attempt_directory)

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        """Collect, merge and commit one nested Coverage Campaign atomically."""
        if request.work_item["kind"] != "coverage_aggregate":
            raise SimulationCampaignIntegrityError(
                "coverage aggregate executor received another work-item kind"
            )
        if request.attempt_directory not in self._prepared:
            self.prepare_attempt(request)
        self._prepared.discard(request.attempt_directory)
        started = time.monotonic()
        build_directory = request.attempt_directory / "private-build"
        durable_directory(build_directory)
        build_directory.chmod(0o700)
        build_attempt = _build_attempt(request)
        request.store.publish_build_attempt(build_directory, build_attempt)
        execution = self._execution(request)
        capturing = _CapturingExecution(
            execution,
            lambda: self._capture_build(request, build_directory, build_attempt, execution),
        )
        outcome = self._collect(request, capturing)
        if outcome.abort_remaining:
            raise SimulationCampaignIntegrityError(
                str(outcome.detail.get("error", "native coverage collection failed"))
            )
        captured = _required_capture(capturing.captured)
        return _completed_result(request, captured, outcome, started)

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
        options = SimulationOptions(
            trace=cast(bool, request.manifest.document["workload"]["trace"]),
            timeout_ms=round(request.policy.timeout_seconds * 1000)
            if request.policy.timeout_seconds is not None
            else None,
            result_verbosity=request.policy.result_verbosity,
        )
        return self._execution_factory(plan, options)

    def _capture_build(
        self,
        request: WorkExecutionRequest,
        directory: Path,
        attempt: BundleBuildAttempt,
        execution: SimulationExecutionPort,
    ) -> _CapturedBuild:
        source_root, paths = execution.authenticated_image()
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        declarations = cast(tuple[Mapping[str, str], ...], workload["runtime_inputs"])
        artifacts = _capture_private_image(
            paths, source_root, directory, declarations
        )
        result, bundle = _publish_ready_build_result(
            request, directory, attempt, artifacts, 0.0
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


def _required_capture(captured: _CapturedBuild | None) -> _CapturedBuild:
    if captured is None:
        raise SimulationCampaignIntegrityError(
            "coverage collection has no authenticated simulator build"
        )
    return captured


def _completed_result(
    request: WorkExecutionRequest,
    captured: _CapturedBuild,
    outcome: CoverageTargetOutcome,
    started: float,
) -> SimulationResult:
    post_exit = authenticate_executable_snapshot(captured.snapshot, captured.snapshot_root)
    bundle = cast(Mapping[str, object], captured.result.document["bundle"])
    return _result(
        request,
        captured.directory,
        captured.result,
        state="completed",
        bundle_id=captured.bundle.document["bundle_id"],
        snapshot={
            "manifest": captured.snapshot_reference,
            "bundle_manifest_sha256": bundle["manifest_sha256"],
            "pre_launch_sha256": captured.pre_launch_sha256,
            "post_exit_sha256": post_exit,
            "verified_after_exit": True,
        },
        observations=[_coverage_observation(item) for item in _coverage_tests(outcome)],
        elapsed=time.monotonic() - started,
        evidence=_coverage_evidence(request, outcome),
    )


def _coverage_tests(outcome: CoverageTargetOutcome) -> tuple[Mapping[str, object], ...]:
    tests = outcome.detail.get("tests", ())
    if not isinstance(tests, tuple):
        raise SimulationCampaignIntegrityError("Coverage Campaign returned invalid test evidence")
    if not tests:
        raise SimulationCampaignIntegrityError("Coverage Campaign returned no test evidence")
    return cast(tuple[Mapping[str, object], ...], tests)


def _coverage_observation(item: Mapping[str, object]) -> dict[str, object]:
    name = item.get("name", item.get("test"))
    verdict = item.get("verdict", item.get("simulation_verdict"))
    if not isinstance(name, str) or not name or verdict not in {
        "pass", "fail", "timeout", "inconclusive", "elab_error"
    }:
        raise SimulationCampaignIntegrityError("Coverage Campaign test evidence is invalid")
    test = SimulationTestOutcome(
        name=name,
        verdict=cast(str, verdict),  # type: ignore[arg-type]
        passed=verdict == "pass",
        timed_out=verdict == "timeout",
        elab_failed=verdict == "elab_error",
        inconclusive=verdict == "inconclusive",
        error_tail=(
            "" if verdict == "pass" else f"coverage simulation {verdict}"
        ),
    )
    return _observation(test)


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


__all__ = ["CoverageAggregateExecutor"]
