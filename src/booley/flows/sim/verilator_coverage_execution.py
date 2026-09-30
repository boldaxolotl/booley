"""Production Simulation execution adapter for Verilator-native coverage."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

from booley.core.build_paths import work_root_for
from booley.flows.base import DEFAULT_TIMEOUT_S
from booley.flows.eda_failures import new_attempt_token
from booley.flows.sim import trace_overlay
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import AdapterResult, AdapterTransportIdentity
from booley.flows.sim.build import (
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    build_failure_report,
    classify_build_outcome,
    prepare_simulation_build,
    simulation_build_script,
)
from booley.flows.sim.build_session import (
    SimulationBuildSession,
    SimulationBuildSlotError,
    project_compile_surface,
    resolve_target_compile_surface,
    snapshot_build_inputs,
    verify_existing_build_inputs,
)
from booley.flows.sim.config import (
    DEFAULT_SIM_BUILD_TIMEOUT_MS,
    resolve_pre_sim_build_access,
    resolve_pre_sim_commands,
)
from booley.flows.sim.coverage_overlay import CoverageOverlay, write_coverage_overlay
from booley.flows.sim.execution.attempt import (
    AdapterAttemptOutcome,
    AdapterAttemptRequest,
    ProcessInvoker,
    execute_adapter_attempt,
)
from booley.flows.sim.execution.composition import prepare_adapter_invocation
from booley.flows.sim.execution.contract import (
    PreSimEvidence,
    SimulationOptions,
    pre_sim_failure_message,
)
from booley.flows.sim.execution.engine import (
    prepare_simulation_work,
    simulation_target_environment,
)
from booley.flows.sim.execution.freshness import ArtifactStamp, snapshot_artifact
from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.fusesoc.fusesoc_registry import (
    FuseSocError,
    core_target_coverage_errors,
    coverage_target_metadata_errors,
)
from booley.targets.catalog import TargetCatalog, TargetCompileSurface
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection

from .coverage_campaign import SimulationVerdict
from .verilator_coverage import (
    PINNED_VERILATOR,
    CoverageCollectionRequest,
    CoverageCollectionResult,
    CoverageHarness,
    CoverageSource,
    CoverageTarget,
    SelectedCoverageTest,
    SimulationBuildRequest,
    SimulationBuildResult,
    SimulationCommandRequest,
    SimulationCommandResult,
    SimulationRunRequest,
    SimulationRunResult,
    VerilatorCollectorIdentity,
    collect,
)

_PROVENANCE_PATH = Path("/usr/local/share/verilator/BOOLEY-SOURCE.txt")
_VERSION_RE = re.compile(r"\bVerilator (?P<version>[0-9]+\.[0-9]+)\b")
_ADAPTER_CLEANUP_MARGIN_S = 90


@dataclass(frozen=True)
class _PreSimSnapshot:
    """Protected coverage state captured immediately before one Project hook."""

    compile_surface: TargetCompileSurface
    compile_hashes: Mapping[str, str]
    build_inputs: Mapping[str, str]
    image: tuple[tuple[str, int, int, str], ...]
    raw: ArtifactStamp | None
    hook: ArtifactStamp | None


class VerilatorCoverageExecution:
    """Bind the collector port to Booley's ordinary build and run adapters."""

    def __init__(
        self,
        handle: TargetHandle,
        *,
        invoke: ProcessInvoker,
        options: SimulationOptions,
        provenance_path: Path = _PROVENANCE_PATH,
        pre_sim_commands: tuple[str, ...] | None = None,
        pre_sim_build_access: str | None = None,
    ) -> None:
        self._handle = handle
        self._invoke = invoke
        self._options = options
        self._provenance_path = provenance_path
        self._pre_sim_commands = pre_sim_commands
        self._pre_sim_build_access = pre_sim_build_access
        self._prepared: PreparedSimulationBuild | None = None
        self._artifact_paths: tuple[Path, ...] = ()
        self._build_variant: str | None = None
        self._trace_mode = "vcd_fifo"
        self._attempt_run_cwd: Path | None = None
        self._snapshot_bound = False

    def build(self, request: SimulationBuildRequest) -> SimulationBuildResult:
        """Prepare and compile the collector-selected isolated build variant."""
        self._prepared = None
        self._artifact_paths = ()
        self._build_variant = None
        self._attempt_run_cwd = None
        self._snapshot_bound = False
        commands = self._commands()
        if commands and self._build_access() == "legacy-per-test":
            return SimulationBuildResult(
                False,
                "native coverage does not support legacy-per-test Pre-Sim build access; "
                "use immutable access or remove Pre-Sim Commands",
            )
        if request.target.identity != self._handle.identity:
            return SimulationBuildResult(False, "coverage Target identity does not match handle")
        identity, version_output = self._collector_identity()
        if identity is None:
            return SimulationBuildResult(False, version_output, infrastructure_error=True)
        try:
            compile_surface = resolve_target_compile_surface(self._handle)
            sources_before = project_compile_surface(compile_surface)
            with SimulationBuildSession(self._handle, request.variant.name) as session:
                candidate = session.new_generation()
                prepared = self._prepare_build(request, build_root=candidate)
                if isinstance(prepared, str):
                    return SimulationBuildResult(False, prepared)
                if project_compile_surface(compile_surface) != sources_before:
                    raise SimulationBuildSlotError(
                        "Project compile inputs changed during coverage preparation"
                    )
                if prepared.work_root != candidate:
                    raise SimulationBuildSlotError(
                        "coverage preparation escaped its leased generation"
                    )
                inputs = session.capture_inputs(prepared)
                result = self._execute_build(prepared, identity)
                if result.success:
                    self._artifact_paths = session.authorize_fresh_image(prepared, inputs)
                    self._build_variant = request.variant.name
                return result
        except SimulationBuildSlotError as exc:
            self._prepared = None
            return SimulationBuildResult(False, str(exc), infrastructure_error=True)

    def _prepare_build(
        self, request: SimulationBuildRequest, *, build_root: Path | None = None
    ) -> PreparedSimulationBuild | str:
        variant = request.variant.name
        if build_root is None:
            build_root = work_root_for(
                self._handle.project_root, "sim", self._handle.selector, variant=variant
            )
            try:
                shutil.rmtree(build_root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                return f"could not reset coverage build root: {exc}"

        overlay: CoverageOverlay | None = None
        try:
            overlay = write_coverage_overlay(
                self._handle,
                instrumentation=request.instrumentation,
                trace=request.variant.trace,
            )
            prepared = prepare_simulation_build(
                self._handle,
                variant=variant,
                build_root=build_root,
                resolution_vlnv=overlay.vlnv,
                environment=simulation_target_environment(self._handle),
            )
            if prepared.resolved.cocotb_module and request.variant.trace:
                trace_overlay.validate_cocotb_trace_mode(
                    self._handle.selector,
                    overlay.trace_mode,
                )
            self._trace_mode = overlay.trace_mode.value
        except (SimulationBuildPreparationError, FuseSocError) as exc:
            return str(exc)
        finally:
            if overlay is not None:
                overlay.cleanup()
        return prepared

    def _execute_build(
        self,
        prepared: PreparedSimulationBuild,
        identity: VerilatorCollectorIdentity,
    ) -> SimulationBuildResult:
        token = new_attempt_token()
        script = simulation_build_script(prepared, token)
        script = _in_directory_script(self._handle.project_root, script)
        timeout_ms = self._options.build_timeout_ms or DEFAULT_SIM_BUILD_TIMEOUT_MS
        timeout_s = max(1, timeout_ms // 1000)
        process = self._invoke(["sh", "-c", script], timeout=timeout_s)
        outcome = classify_build_outcome(process, token, timeout_s=timeout_s)
        if outcome.failure_kind == "infrastructure":
            return SimulationBuildResult(
                False,
                build_failure_report(outcome),
                identity,
                infrastructure_error=True,
                reason=outcome.reason,
            )
        if not outcome.passed:
            return SimulationBuildResult(False, outcome.output or outcome.reason, identity)
        self._prepared = prepared
        return SimulationBuildResult(True, outcome.output, identity)

    def run(self, request: SimulationRunRequest) -> SimulationRunResult:
        """Run one selected test in one process through the authenticated adapter."""
        prepared = self._prepared
        variant = self._build_variant
        if prepared is None or variant is None:
            return SimulationRunResult("inconclusive", "coverage build was not completed")
        if request.target.identity != self._handle.identity:
            return SimulationRunResult("inconclusive", "coverage Target identity mismatch")
        try:
            if self._snapshot_bound:
                return self._run_prepared(request, prepared)
            with SimulationBuildSession(self._handle, variant) as session:
                session.verify_fresh_image(prepared)
                return self._run_prepared(
                    request,
                    prepared,
                    verify_image=lambda: session.verify_fresh_image(prepared),
                )
        except SimulationBuildSlotError as exc:
            return SimulationRunResult(
                "inconclusive",
                f"coverage image verification failed: {exc}",
                infrastructure_error=True,
            )

    def _run_prepared(
        self,
        request: SimulationRunRequest,
        prepared: PreparedSimulationBuild,
        *,
        verify_image: Callable[[], None] | None = None,
    ) -> SimulationRunResult:
        _prepare_artifact_directories(request)
        token = new_attempt_token()
        adapter = "cocotb" if prepared.resolved.cocotb_module else prepared.eda_tool
        test_names = (request.test.name,)
        transport = AdapterTransportIdentity(
            adapter=adapter,
            attempt_token=token,
            target_identity=self._handle.identity,
            selected_tests=test_names,
            result_path=prepared.build_root / f".booley-coverage-adapter-{token}.json",
        )
        work = self._prepare_work(request, prepared, transport)
        before = _snapshot_pre_sim_state(self._handle, prepared, request, self._artifact_paths)
        pre_sim = self._run_pre_sim(prepared, test_names, work.run_cwd)
        try:
            _verify_pre_sim_state(
                prepared,
                request,
                self._artifact_paths,
                before,
                verify_image=verify_image,
            )
        except SimulationBuildSlotError as exc:
            return SimulationRunResult(
                "inconclusive",
                f"coverage image verification failed: {exc}",
                pre_sim,
                infrastructure_error=True,
            )
        if pre_sim is not None and pre_sim.status != "passed":
            return SimulationRunResult(
                "elab_error",
                pre_sim_failure_message(pre_sim.status, pre_sim.detail),
                pre_sim,
                infrastructure_error=pre_sim.status == "spawn_error",
            )
        return self._execute_run(request, prepared, transport, work, pre_sim)

    def _prepare_work(
        self,
        request: SimulationRunRequest,
        prepared: PreparedSimulationBuild,
        transport: AdapterTransportIdentity,
    ) -> PreparedSimulationWork:
        work = prepare_simulation_work(
            self._handle,
            prepared,
            transport,
            self._options,
            trace=request.trace,
            trace_mode=self._trace_mode,
            plusargs_suffix=request.argv_suffix,
        )
        return replace(work, run_cwd=str(self._attempt_run_cwd)) if self._attempt_run_cwd else work

    def _run_pre_sim(
        self, prepared: PreparedSimulationBuild, test_names: tuple[str, ...], run_cwd: str
    ) -> PreSimEvidence | None:
        return run_pre_sim_commands(
            self._handle,
            test_names=test_names,
            build_root=prepared.build_root,
            eda_tool=prepared.eda_tool,
            timeout_s=DEFAULT_TIMEOUT_S,
            simulator_environment=simulation_target_environment(self._handle),
            commands=self._commands(),
            run_cwd=run_cwd,
            working_directory=self._handle.project_root,
            expose_build_root=False,
        )

    def _execute_run(
        self,
        request: SimulationRunRequest,
        prepared: PreparedSimulationBuild,
        transport: AdapterTransportIdentity,
        work: PreparedSimulationWork,
        pre_sim: PreSimEvidence | None,
    ) -> SimulationRunResult:
        invocation = prepare_adapter_invocation(work)
        environment = {**simulation_target_environment(self._handle), **request.environment}
        script = _environment_script(environment, invocation)
        script = _in_directory_script(self._handle.project_root, script)
        executed = execute_adapter_attempt(
            self._invoke,
            AdapterAttemptRequest(
                ("sh", "-c", script),
                work.timeout_s + _ADAPTER_CLEANUP_MARGIN_S,
                transport,
                prepared.build_root,
            ),
        )
        return replace(_simulation_run_result(executed, request.test.name), pre_sim=pre_sim)

    def _commands(self) -> tuple[str, ...]:
        if self._pre_sim_commands is not None:
            return self._pre_sim_commands
        return tuple(resolve_pre_sim_commands(self._handle.project_root))

    def _build_access(self) -> str:
        if self._pre_sim_build_access is not None:
            return self._pre_sim_build_access
        return resolve_pre_sim_build_access(self._handle.project_root)

    def command(self, request: SimulationCommandRequest) -> SimulationCommandResult:
        """Run one collector utility in its requested artifact directory."""
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        script = f"cd {shlex.quote(str(request.cwd))}\nexec {shlex.join(request.argv)}"
        result = self._invoke(["sh", "-c", script], timeout=DEFAULT_TIMEOUT_S)
        return SimulationCommandResult(result.returncode, result.stdout, result.stderr)

    def authenticated_image(self) -> tuple[Path, tuple[Path, ...]]:
        """Expose only the exact leased image authorized by the successful build."""
        prepared = self._prepared
        if prepared is None:
            raise SimulationBuildSlotError("coverage simulator image is not authorized")
        if not self._artifact_paths:
            raise SimulationBuildSlotError("coverage simulator image has no artifacts")
        return prepared.build_root, self._artifact_paths

    def bind_authenticated_attempt(self, snapshot_root: Path, run_cwd: Path) -> None:
        """Execute only from the campaign-owned snapshot and claimed run directory."""
        prepared = self._prepared
        if prepared is None:
            raise SimulationBuildSlotError("coverage simulator image is not authorized")
        try:
            rebound_paths = tuple(
                snapshot_root / path.relative_to(prepared.build_root)
                for path in self._artifact_paths
            )
        except ValueError as exc:
            raise SimulationBuildSlotError("coverage image escapes its authorized root") from exc
        self._prepared = replace(
            prepared,
            work_root=snapshot_root,
            build_root=snapshot_root,
        )
        self._artifact_paths = rebound_paths
        self._attempt_run_cwd = run_cwd
        self._snapshot_bound = True

    def _collector_identity(self) -> tuple[VerilatorCollectorIdentity | None, str]:
        version = self._invoke(["verilator", "--version"], timeout=30)
        output = version.stdout + ("\n" + version.stderr if version.stderr else "")
        match = _VERSION_RE.search(output)
        expected = f"{PINNED_VERILATOR.tag} @ {PINNED_VERILATOR.commit}"
        guidance = "rebuild the Sandbox image (booley session refresh)"
        if version.returncode != 0 or version.timed_out or match is None:
            message = (
                "Verilator coverage collector version could not be determined "
                f"(expected {expected}); {guidance}"
            )
            return None, f"{message}\n\n{output}".strip()
        expected_version = PINNED_VERILATOR.tag.removeprefix("v")
        if match["version"] != expected_version:
            message = (
                f"Verilator {match['version']} is not the pinned coverage collector "
                f"(expected {expected}); {guidance}"
            )
            return None, f"{message}\n\n{output}".strip()
        try:
            provenance = self._provenance_path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, (
                "Verilator coverage collector provenance is unavailable "
                f"(expected {expected}); {guidance}: {exc}"
            )
        if PINNED_VERILATOR.commit not in provenance:
            message = (
                "Verilator coverage collector provenance does not match the pinned collector "
                f"(expected {expected}); {guidance}"
            )
            return None, f"{message}\n\n{provenance}".strip()
        return PINNED_VERILATOR, output


def prepare_coverage_collection(
    handle: TargetHandle,
    *,
    selected_tests: tuple[str, ...],
    artifact_root: Path,
    trace: bool = False,
) -> CoverageCollectionRequest:
    """Project one resolved Target into the collector's policy-free request."""
    coverage_errors = core_target_coverage_errors(handle.core_file, handle.name)
    if coverage_errors:
        raise ValueError("; ".join(coverage_errors))
    inspection = TargetCatalog.build(handle.project_root).inspect(handle)
    if inspection.eda_tool != "verilator":
        raise ValueError("Verilator-native coverage requires a Verilator sim Target")
    resolved_errors = _resolved_coverage_errors(inspection, handle.name)
    if resolved_errors:
        raise ValueError("; ".join(resolved_errors))
    metadata = _coverage_metadata(inspection)
    harness = _coverage_harness(inspection)
    hooks = cast(tuple[str, ...], metadata.get("custom_main_hooks", ()))
    reset_included = cast(bool, metadata.get("reset_included", True))
    return CoverageCollectionRequest(
        target=CoverageTarget(
            identity=handle.identity,
            selector=handle.selector,
            toplevel=inspection.toplevel,
            harness=harness,
            sources=tuple(_coverage_source(item) for item in inspection.inputs if _is_hdl(item)),
            custom_main_hooks=hooks,
        ),
        selected_tests=tuple(SelectedCoverageTest(name) for name in selected_tests),
        artifact_root=artifact_root,
        trace=trace,
        reset_included=reset_included,
    )


def collect_target_coverage(
    handle: TargetHandle,
    *,
    selected_tests: tuple[str, ...],
    artifact_root: Path,
    invoke: ProcessInvoker,
    options: SimulationOptions,
) -> CoverageCollectionResult:
    """Collect a Target through the same build and adapter stack as Simulation."""
    request = prepare_coverage_collection(
        handle,
        selected_tests=selected_tests,
        artifact_root=artifact_root,
        trace=options.trace,
    )
    execution = VerilatorCoverageExecution(handle, invoke=invoke, options=options)
    return collect(request, execution)


def _coverage_metadata(inspection: TargetInspection) -> Mapping[str, object]:
    booley = inspection.flow_options.get("booley")
    if not isinstance(booley, Mapping):
        return MappingProxyType({})
    coverage = booley.get("coverage")
    return coverage if isinstance(coverage, Mapping) else MappingProxyType({})


def _resolved_coverage_errors(inspection: TargetInspection, target_name: str) -> list[str]:
    """Validate FuseSoC's effective recipe after inheritance and flag resolution."""
    booley = inspection.flow_options.get("booley")
    if not isinstance(booley, Mapping) or "coverage" not in booley:
        return []
    coverage = booley["coverage"]
    if isinstance(coverage, Mapping):
        coverage = dict(coverage)
        hooks = coverage.get("custom_main_hooks")
        if isinstance(hooks, tuple):
            coverage["custom_main_hooks"] = list(hooks)
    return coverage_target_metadata_errors(coverage, f"targets.{target_name}.flow_options.booley")


def _coverage_harness(inspection: TargetInspection) -> CoverageHarness:
    if inspection.flow_options.get("cocotb_module"):
        return "cocotb"
    if any(item.file_type in {"cppSource", "cSource"} for item in inspection.inputs):
        return "custom_main"
    if any("tb" in item.tags and _is_hdl(item) for item in inspection.inputs):
        return "hdl_testbench"
    return "generated_main"


def _is_hdl(item: TargetInput) -> bool:
    return (
        item.file_type
        in {
            "verilogSource",
            "systemVerilogSource",
            "vhdlSource",
            "vhdlSource-2008",
        }
        and not item.is_include
    )


def _coverage_source(item: TargetInput) -> CoverageSource:
    return CoverageSource(
        native_path=item.path,
        path=item.path,
        kind="testbench" if "tb" in item.tags else "rtl",
    )


def _environment_script(environment: Mapping[str, str], invocation: list[str]) -> str:
    exports = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in environment.items()
    )
    return f"{exports}exec {shlex.join(invocation)}"


def _in_directory_script(directory: Path, script: str) -> str:
    return f"cd {shlex.quote(str(directory))}\n{script}"


def _prepare_artifact_directories(request: SimulationRunRequest) -> None:
    request.raw_path.parent.mkdir(parents=True, exist_ok=True)
    if request.hook_evidence_path is not None:
        request.hook_evidence_path.parent.mkdir(parents=True, exist_ok=True)


def _snapshot_pre_sim_state(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    request: SimulationRunRequest,
    artifact_paths: tuple[Path, ...],
) -> _PreSimSnapshot:
    surface = resolve_target_compile_surface(handle)
    return _PreSimSnapshot(
        compile_surface=surface,
        compile_hashes=project_compile_surface(surface, include_operational_cores=True),
        build_inputs=snapshot_build_inputs(prepared),
        image=_image_identity(prepared.build_root, artifact_paths),
        raw=snapshot_artifact(request.raw_path),
        hook=(
            snapshot_artifact(request.hook_evidence_path)
            if request.hook_evidence_path is not None
            else None
        ),
    )


def _verify_pre_sim_state(
    prepared: PreparedSimulationBuild,
    request: SimulationRunRequest,
    artifact_paths: tuple[Path, ...],
    before: _PreSimSnapshot,
    *,
    verify_image: Callable[[], None] | None,
) -> None:
    verify_existing_build_inputs(prepared, before.build_inputs)
    if (
        project_compile_surface(before.compile_surface, include_operational_cores=True)
        != before.compile_hashes
    ):
        raise SimulationBuildSlotError("Project compile inputs changed during Pre-Sim Commands")
    if verify_image is not None:
        verify_image()
    if _image_identity(prepared.build_root, artifact_paths) != before.image:
        raise SimulationBuildSlotError("coverage image changed during Pre-Sim Commands")
    if snapshot_artifact(request.raw_path) != before.raw:
        raise SimulationBuildSlotError("native coverage database changed during Pre-Sim Commands")
    if request.hook_evidence_path is not None and (
        snapshot_artifact(request.hook_evidence_path) != before.hook
    ):
        raise SimulationBuildSlotError("Coverage Window evidence changed during Pre-Sim Commands")


def _image_identity(root: Path, paths: tuple[Path, ...]) -> tuple[tuple[str, int, int, str], ...]:
    try:
        resolved_root = root.resolve(strict=True)
        if root.is_symlink() or not resolved_root.is_dir():
            raise SimulationBuildSlotError("coverage image root is not a regular directory")
        identities = []
        for path in paths:
            info = path.lstat()
            resolved = path.resolve(strict=True)
            if not stat.S_ISREG(info.st_mode) or not resolved.is_relative_to(resolved_root):
                raise SimulationBuildSlotError(f"unsafe coverage image: {path}")
            mode = stat.S_IMODE(info.st_mode)
            if os.name != "nt" and not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                raise SimulationBuildSlotError(f"coverage image is not executable: {path}")
            content = path.read_bytes()
            relative = path.relative_to(root).as_posix()
            identities.append((relative, mode, len(content), hashlib.sha256(content).hexdigest()))
        return tuple(identities)
    except OSError as exc:
        raise SimulationBuildSlotError(f"cannot verify coverage image: {exc}") from exc


def _simulation_run_result(attempt: AdapterAttemptOutcome, test_name: str) -> SimulationRunResult:
    process = attempt.process
    output = process.stdout + ("\n" + process.stderr if process.stderr else "")
    if attempt.error is not None or attempt.result is None:
        verdict: SimulationVerdict = "timeout" if process.timed_out else "inconclusive"
        return SimulationRunResult(verdict, f"{output}\n{attempt.error or ''}".strip())
    test = next(item for item in attempt.result.test_results if item.name == test_name)
    return SimulationRunResult(
        _adapter_verdict(attempt.result, test_name),
        attempt.result.detail or output,
        infrastructure_error=test.failure_kind == "infrastructure",
        termination=test.termination,
        failure_kind=test.failure_kind,
        simulator_returncode=attempt.result.simulator_returncode,
    )


def _adapter_verdict(
    result: AdapterResult,
    test_name: str,
) -> SimulationVerdict:
    test = next((item for item in result.test_results if item.name == test_name), None)
    assert test is not None, "authenticated coverage result must include the selected test"
    return cast(SimulationVerdict, test.verdict)


__all__ = [
    "VerilatorCoverageExecution",
    "collect_target_coverage",
    "prepare_coverage_collection",
]
