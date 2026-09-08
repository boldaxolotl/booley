"""Production Simulation execution adapter for Verilator-native coverage."""

from __future__ import annotations

import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import cast

from booley.flows import edam as edam_layer
from booley.flows.base import DEFAULT_TIMEOUT_S, SubprocessResult
from booley.flows.sim import trace_overlay
from booley.flows.sim.adapter_transport import AdapterResult, AdapterTransportIdentity
from booley.flows.sim.build import (
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    build_stage_script,
    classify_build_outcome,
    new_attempt_token,
    prepare_simulation_build,
)
from booley.flows.sim.coverage_overlay import CoverageOverlay, write_coverage_overlay
from booley.flows.sim.execution.attempt import (
    AdapterAttemptOutcome,
    AdapterAttemptRequest,
    ProcessInvoker,
    execute_adapter_attempt,
)
from booley.flows.sim.execution.composition import prepare_adapter_invocation
from booley.flows.sim.execution.contract import SimulationOptions
from booley.flows.sim.execution.engine import (
    prepare_simulation_work,
    simulation_target_environment,
)
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.targets.catalog import TargetCatalog
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


class VerilatorCoverageExecution:
    """Bind the collector port to Booley's ordinary build and run adapters."""

    def __init__(
        self,
        handle: TargetHandle,
        *,
        invoke: ProcessInvoker,
        options: SimulationOptions,
        provenance_path: Path = _PROVENANCE_PATH,
    ) -> None:
        self._handle = handle
        self._invoke = invoke
        self._options = options
        self._provenance_path = provenance_path
        self._prepared: PreparedSimulationBuild | None = None
        self._trace_mode = "vcd_fifo"

    def build(self, request: SimulationBuildRequest) -> SimulationBuildResult:
        """Prepare and compile the collector-selected isolated build variant."""
        if request.target.identity != self._handle.identity:
            return SimulationBuildResult(False, "coverage Target identity does not match handle")
        identity, version_output = self._collector_identity()
        if identity is None:
            return SimulationBuildResult(False, version_output)
        prepared = self._prepare_build(request)
        if isinstance(prepared, str):
            return SimulationBuildResult(False, prepared)
        return self._execute_build(prepared, identity)

    def _prepare_build(self, request: SimulationBuildRequest) -> PreparedSimulationBuild | str:
        variant = request.variant.name
        build_root = edam_layer.work_root_for(
            self._handle.project_root,
            "sim",
            self._handle.selector,
            variant=variant,
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
        script = build_stage_script(
            prepared.make_argv,
            token,
            environment=simulation_target_environment(self._handle),
        )
        script = _in_directory_script(self._handle.project_root, script)
        process = self._invoke(["sh", "-c", script], timeout=DEFAULT_TIMEOUT_S)
        outcome = classify_build_outcome(process, token)
        if not outcome.passed:
            return SimulationBuildResult(False, outcome.output or outcome.reason, identity)
        self._prepared = prepared
        return SimulationBuildResult(True, outcome.output, identity)

    def run(self, request: SimulationRunRequest) -> SimulationRunResult:
        """Run one selected test in one process through the authenticated adapter."""
        prepared = self._prepared
        if prepared is None:
            return SimulationRunResult("inconclusive", "coverage build was not completed")
        if request.target.identity != self._handle.identity:
            return SimulationRunResult("inconclusive", "coverage Target identity mismatch")

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
        work = prepare_simulation_work(
            self._handle,
            prepared,
            transport,
            self._options,
            trace=request.trace,
            trace_mode=self._trace_mode,
            plusargs_suffix=request.argv_suffix,
        )
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
        return _simulation_run_result(executed, request.test.name)

    def command(self, request: SimulationCommandRequest) -> SimulationCommandResult:
        """Run one collector utility in its requested artifact directory."""
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        script = f"cd {shlex.quote(str(request.cwd))}\nexec {shlex.join(request.argv)}"
        result = self._invoke(["sh", "-c", script], timeout=DEFAULT_TIMEOUT_S)
        return SimulationCommandResult(result.returncode, result.stdout, result.stderr)

    def _collector_identity(self) -> tuple[VerilatorCollectorIdentity | None, str]:
        version = self._invoke(["verilator", "--version"], timeout=30)
        output = version.stdout + ("\n" + version.stderr if version.stderr else "")
        match = _VERSION_RE.search(output)
        try:
            provenance = self._provenance_path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"{output}\nVerilator provenance unavailable: {exc}".strip()
        expected_version = PINNED_VERILATOR.tag.removeprefix("v")
        if (
            version.returncode != 0
            or version.timed_out
            or match is None
            or match["version"] != expected_version
            or PINNED_VERILATOR.commit not in provenance
        ):
            return None, f"{output}\n{provenance}".strip()
        return PINNED_VERILATOR, output


def prepare_coverage_collection(
    handle: TargetHandle,
    *,
    selected_tests: tuple[str, ...],
    artifact_root: Path,
    trace: bool = False,
) -> CoverageCollectionRequest:
    """Project one resolved Target into the collector's policy-free request."""
    inspection = TargetCatalog.build(handle.project_root).inspect(handle)
    if inspection.eda_tool != "verilator":
        raise ValueError("Verilator-native coverage requires a Verilator sim Target")
    metadata = _coverage_metadata(inspection)
    harness = _coverage_harness(inspection)
    declared_hooks = metadata.get("custom_main_hooks", ())
    hooks = (
        tuple(str(item) for item in declared_hooks)
        if isinstance(declared_hooks, Sequence) and not isinstance(declared_hooks, str | bytes)
        else ()
    )
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
        reset_included=bool(metadata.get("reset_included", True)),
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


def _simulation_run_result(attempt: AdapterAttemptOutcome, test_name: str) -> SimulationRunResult:
    process = attempt.process
    output = process.stdout + ("\n" + process.stderr if process.stderr else "")
    if attempt.error is not None or attempt.result is None:
        verdict: SimulationVerdict = "timeout" if process.timed_out else "inconclusive"
        return SimulationRunResult(verdict, f"{output}\n{attempt.error or ''}".strip())
    return SimulationRunResult(_adapter_verdict(attempt.result, test_name, process), output)


def _adapter_verdict(
    result: AdapterResult,
    test_name: str,
    process: SubprocessResult,
) -> SimulationVerdict:
    test = next((item for item in result.test_results if item.name == test_name), None)
    if test is not None:
        return cast(SimulationVerdict, test.verdict)
    if process.timed_out or result.failure_kind == "timeout":
        return "timeout"
    if result.passed:
        return "pass"
    return "inconclusive" if result.inconclusive else "fail"


__all__ = [
    "VerilatorCoverageExecution",
    "collect_target_coverage",
    "prepare_coverage_collection",
]
