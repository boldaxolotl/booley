"""Deep execution boundary for one selected Simulation Target."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from booley.bwave.contract import decode_trace_metadata
from booley.config.project_config import load_test_configuration_field, lookup_target_section
from booley.core.boundary import BoundaryError
from booley.flows import edam as edam_layer
from booley.flows.base import SubprocessResult
from booley.flows.run_log import begin_run_log, write_run_log
from booley.flows.sim import edam as sim_edam
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTransportError,
    AdapterTransportIdentity,
    read_adapter_result,
)
from booley.flows.sim.build import (
    BuildOutcome,
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    build_stage_script,
    classify_build_outcome,
    new_attempt_token,
    prepare_simulation_build,
)
from booley.flows.sim.config import (
    resolve_cycle_sentinels,
    resolve_max_rundir_bytes,
    resolve_pre_run_commands,
    resolve_run_cwd,
    resolve_sim_time_grace_s,
    resolve_sim_timeout_ms,
    resolve_trace_args,
    resolve_trace_files,
)
from booley.flows.sim.runner import resolve_sim_sentinels
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.workload import build_workload_snapshot
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.targets.target import TargetHandle, inspect_target

from .artifacts import artifact_path_component, configured_trace_path
from .composition import prepare_adapter_invocation
from .contract import (
    DefaultSelection,
    NamedTests,
    PreRunEvidence,
    SimulationArtifactEvidence,
    SimulationInfrastructureFailure,
    SimulationOptions,
    SimulationPreview,
    SimulationSelection,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from .failures import find_missing_executable
from .freshness import ArtifactValidationError, validate_fresh_artifact
from .pre_run import run_pre_run_commands
from .telemetry import parse_build_seconds, parse_run_seconds, process_resources

ProcessInvoker = Callable[..., SubprocessResult]
_TRACE_OK_RE = re.compile(r"^TRACE_OK:\s*(\S.*?)\s*$", re.MULTILINE)
_TRACE_METADATA_RE = re.compile(r"^TRACE_METADATA:\s*(\{.*\})\s*$", re.MULTILINE)
_DEFAULT_CYCLE_SENTINEL = "[SIM_CYCLES]"
_TRACE_CLEANUP_MARGIN_S = 90
_NO_SENTINEL = "no pass/fail sentinel detected, simulation exited cleanly"
_NO_WAVEFORM = "the simulation passed, but --trace produced no queryable waveform"


@dataclass(frozen=True)
class _Attempt:
    prepared: PreparedSimulationBuild
    identity: AdapterTransportIdentity
    command: tuple[str, ...]
    test_names: tuple[str, ...]
    adapter: str
    trace_requested: bool
    setup_s: float


class AdapterEvidenceError(RuntimeError):
    """A completed adapter supplied no trustworthy terminal result."""


class SimulationArtifactPersistenceError(RuntimeError):
    """A completed attempt could not preserve its required run evidence."""


def read_completed_adapter_result(
    identity: AdapterTransportIdentity,
    process: SubprocessResult,
    build: BuildOutcome,
) -> AdapterResult | None:
    """Compatibility wrapper for callers testing phase precedence directly."""
    if build.design_failed:
        return None
    if not identity.result_path.exists():
        if process.dispatched_unix <= 0 or process.timed_out:
            return None
        raise AdapterEvidenceError(
            "adapter completed without authenticated terminal result evidence"
        )
    try:
        result = read_adapter_result(identity)
    except AdapterTransportError as exc:
        raise AdapterEvidenceError(f"invalid authenticated adapter result: {exc}") from exc
    if result.passed and (process.returncode != 0 or not build.passed):
        raise AdapterEvidenceError(
            "authenticated adapter pass contradicts process or build evidence"
        )
    return result


class SimulationExecution:
    """Resolve, preview, and execute one Target behind a two-method interface."""

    def __init__(
        self,
        *,
        invoke: ProcessInvoker,
        options: SimulationOptions,
        artifact_root: Path | None = None,
    ) -> None:
        self._invoke = invoke
        self._options = options
        self._artifact_root = artifact_root
        self._reset_trace_roots: set[Path] = set()

    def run(
        self,
        handle: TargetHandle,
        selection: SimulationSelection,
    ) -> SimulationTargetOutcome:
        """Execute the selected Target and return immutable normalized evidence."""
        started = time.monotonic()
        try:
            inspection = inspect_target(handle.project_root, handle)
        except fusesoc_registry.FuseSocError as exc:
            return _setup_failure(handle, str(exc), started)
        groups = _work_groups(selection, _is_cocotb(inspection.flow_options))
        try:
            results = [self._run_group(handle, names) for names in groups]
        except SimulationBuildPreparationError as exc:
            return _setup_failure(handle, str(exc), started)
        failure = next(
            (result.infrastructure_failure for result in results if result.infrastructure_failure),
            None,
        )
        tests = tuple(test for result in results for test in result.tests)
        builds = tuple(build for result in results for build in result.builds)
        pre_runs = tuple(item for result in results for item in result.pre_runs)
        artifacts = tuple(item for result in results for item in result.artifacts)
        return _aggregate(
            handle,
            inspection.toplevel,
            results,
            tests,
            builds,
            pre_runs,
            artifacts,
            failure,
            started,
        )

    def preview(
        self,
        handle: TargetHandle,
        selection: SimulationSelection,
    ) -> SimulationPreview:
        """Describe the same work grouping and adapter rendering without side effects."""
        inspection = inspect_target(handle.project_root, handle)
        cocotb = _is_cocotb(inspection.flow_options)
        groups = _work_groups(selection, cocotb)
        commands = tuple(
            self._preview_group(handle, inspection, names, cocotb) for names in groups
        )
        return SimulationPreview(commands)

    def _run_group(
        self,
        handle: TargetHandle,
        test_names: tuple[str, ...],
    ) -> SimulationTargetOutcome:
        started = time.monotonic()
        attempt = self._prepare_attempt(handle, test_names)
        pre_run = self._run_pre_run(handle, attempt)
        if pre_run is not None and pre_run.status != "passed":
            if pre_run.status == "spawn_error":
                return _pre_run_infrastructure_failure(handle, attempt, pre_run, started)
            return _pre_run_failure(handle, attempt, pre_run, started)
        dispatched_ns = time.time_ns()
        process = self._invoke(list(attempt.command), timeout=self._wrapper_timeout_s(handle))
        processing_started = time.monotonic()
        build = classify_build_outcome(process, attempt.identity.attempt_token)
        if build.failure_kind == "infrastructure":
            return _infrastructure_failure(handle, attempt, build, pre_run, started)
        try:
            adapter = self._read_adapter_result(attempt, process, build, dispatched_ns)
        except (AdapterTransportError, ArtifactValidationError) as exc:
            return _transport_failure(handle, attempt, build, pre_run, str(exc), started)
        try:
            return self._completed_group(
                handle,
                attempt,
                process,
                build,
                adapter,
                pre_run,
                dispatched_ns,
                processing_started,
                started,
            )
        except SimulationArtifactPersistenceError as exc:
            return _artifact_failure(handle, attempt, build, pre_run, str(exc), started)

    def _prepare_attempt(self, handle: TargetHandle, test_names: tuple[str, ...]) -> _Attempt:
        started = time.monotonic()
        prepared, trace_mode = self._prepare_build(handle)
        adapter = "cocotb" if prepared.resolved.cocotb_module else prepared.eda_tool
        token = new_attempt_token()
        identity = AdapterTransportIdentity(
            adapter=adapter,
            attempt_token=token,
            target_identity=handle.identity,
            selected_tests=test_names,
            result_path=prepared.build_root / f".booley-adapter-{token}.json",
        )
        begin_run_log(prepared.build_root, flow="sim", target=handle.selector)
        work = self._prepared_work(handle, prepared, identity, trace_mode)
        try:
            invocation = prepare_adapter_invocation(work)
        except ValueError as exc:
            raise SimulationBuildPreparationError(str(exc)) from exc
        script = build_stage_script(
            prepared.make_argv,
            token,
            run_line=shlex.join(invocation),
            environment=_target_environment(handle),
        )
        return _Attempt(
            prepared=prepared,
            identity=identity,
            command=("sh", "-c", script),
            test_names=test_names,
            adapter=adapter,
            trace_requested=self._options.trace,
            setup_s=time.monotonic() - started,
        )

    def _prepare_build(self, handle: TargetHandle) -> tuple[PreparedSimulationBuild, TraceMode]:
        variant = "trace" if self._options.trace else ""
        build_root = edam_layer.work_root_for(
            handle.project_root,
            "sim",
            handle.selector,
            variant=variant,
        )
        self._reset_trace_root(build_root)
        overlay = _trace_overlay(handle) if self._options.trace else None
        try:
            prepared = prepare_simulation_build(
                handle,
                variant=variant,
                resolution_vlnv=overlay.vlnv if overlay is not None else None,
                environment=_target_environment(handle),
            )
            if overlay is not None and prepared.resolved.cocotb_module:
                fusesoc_registry.validate_cocotb_trace_mode(handle.selector, overlay.mode)
            mode = overlay.mode if overlay is not None else TraceMode.VCD_FIFO
            return prepared, mode
        finally:
            if overlay is not None:
                overlay.cleanup()

    def _prepared_work(
        self,
        handle: TargetHandle,
        prepared: PreparedSimulationBuild,
        identity: AdapterTransportIdentity,
        trace_mode: TraceMode,
    ) -> PreparedSimulationWork:
        root = handle.project_root
        rel = edam_layer.relpath_for_make(prepared.build_root, root)
        cocotb = bool(prepared.resolved.cocotb_module)
        plusargs = _simulation_plusargs(
            handle, identity.selected_tests, prepared.resolved.parameters
        )
        passes, fails = resolve_sim_sentinels(root)
        return PreparedSimulationWork(
            adapter="cocotb" if cocotb else prepared.eda_tool,
            build_dir=rel,
            run_cwd=_simulation_run_cwd(root, rel),
            timeout_s=max(1, self._effective_timeout_ms(handle) // 1000),
            eda_tool=prepared.eda_tool,
            max_rundir_bytes=resolve_max_rundir_bytes(root),
            plusargs=tuple(plusargs),
            trace=self._options.trace,
            trace_mode=trace_mode.value,
            trace_scope=prepared.toplevel,
            trace_args=tuple(resolve_trace_args(root)),
            trace_files=tuple(resolve_trace_files(root)),
            pass_sentinels=tuple(passes),
            fail_sentinels=tuple(fails),
            top=prepared.toplevel,
            cocotb_module=prepared.resolved.cocotb_module or "",
            tests=identity.selected_tests,
            result_verbosity=self._options.result_verbosity,
            sim_time_grace_s=resolve_sim_time_grace_s(root),
            adapter_result_path=str(identity.result_path),
            attempt_token=identity.attempt_token,
            target_identity=identity.target_identity,
        )

    def _run_pre_run(self, handle: TargetHandle, attempt: _Attempt) -> PreRunEvidence | None:
        return run_pre_run_commands(
            handle,
            test_names=attempt.test_names,
            build_root=attempt.prepared.build_root,
            eda_tool=attempt.prepared.eda_tool,
            timeout_s=self._wrapper_timeout_s(handle),
            simulator_environment=_target_environment(handle),
        )

    @staticmethod
    def _read_adapter_result(
        attempt: _Attempt,
        process: SubprocessResult,
        build: BuildOutcome,
        dispatched_ns: int,
    ) -> AdapterResult | None:
        if build.design_failed:
            return None
        if not attempt.identity.result_path.exists():
            if process.timed_out:
                return None
            raise AdapterTransportError("adapter completed without authenticated terminal result")
        validate_fresh_artifact(
            attempt.identity.result_path,
            roots=(attempt.prepared.build_root,),
            before=None,
            not_before_ns=dispatched_ns,
        )
        result = read_adapter_result(attempt.identity)
        if result.passed and (process.returncode != 0 or not build.passed):
            raise AdapterTransportError("adapter pass contradicts process or build evidence")
        if result.failure_kind == "infrastructure":
            raise AdapterTransportError(result.detail or "adapter infrastructure failure")
        return result

    def _completed_group(
        self,
        handle: TargetHandle,
        attempt: _Attempt,
        process: SubprocessResult,
        build: BuildOutcome,
        adapter: AdapterResult | None,
        pre_run: PreRunEvidence | None,
        dispatched_ns: int,
        processing_started: float,
        started: float,
    ) -> SimulationTargetOutcome:
        output = process.stdout + ("\n" + process.stderr if process.stderr else "")
        logs = _persist_run_logs(handle, attempt, output, self._artifact_root)
        trace = _trace_artifact(handle, attempt, output, dispatched_ns)
        if attempt.trace_requested and trace is None and adapter is not None and adapter.passed:
            adapter = _missing_trace_result(adapter)
        tests = _test_outcomes(
            handle,
            attempt,
            process,
            build,
            adapter,
            output,
            logs,
            trace,
        )
        tests = _attach_group_telemetry(
            tests,
            attempt,
            process,
            build,
            pre_run,
            output,
            time.monotonic() - processing_started,
        )
        artifacts = (*logs, *((trace,) if trace is not None else ()))
        diagnostics = adapter.diagnostics if adapter is not None else ()
        return _group_outcome(
            handle,
            attempt,
            tests,
            build,
            pre_run,
            artifacts,
            started,
            diagnostics,
            adapter.passed if adapter is not None else None,
        )

    def _preview_group(
        self,
        handle: TargetHandle,
        inspection: Any,
        test_names: tuple[str, ...],
        cocotb: bool,
    ) -> tuple[str, ...]:
        root = handle.project_root
        variant = "trace" if self._options.trace else ""
        build_root = edam_layer.work_root_for(root, "sim", handle.selector, variant=variant)
        setup = fusesoc_registry.setup_command(
            handle.selector,
            project_root=root,
            build_root=build_root,
            vlnv=handle.vlnv,
        )
        rel = edam_layer.relpath_for_make(build_root, root)
        work = _preview_work(self, handle, inspection, test_names, cocotb, rel)
        steps = [
            *_preview_exports(handle, test_names, build_root),
            shlex.join(setup),
            *resolve_pre_run_commands(root),
            shlex.join(edam_layer.make_command(rel)),
            shlex.join(prepare_adapter_invocation(work)),
        ]
        return ("sh", "-c", " && ".join(steps))

    def _effective_timeout_ms(self, handle: TargetHandle) -> int:
        return self._options.timeout_ms or resolve_sim_timeout_ms(handle.project_root)

    def _wrapper_timeout_s(self, handle: TargetHandle) -> int:
        timeout = max(1, self._effective_timeout_ms(handle) // 1000)
        return timeout + (_TRACE_CLEANUP_MARGIN_S if self._options.trace else 0)

    def _reset_trace_root(self, build_root: Path) -> None:
        if not self._options.trace:
            return
        key = build_root.resolve()
        if key in self._reset_trace_roots:
            return
        shutil.rmtree(build_root, ignore_errors=True)
        self._reset_trace_roots.add(key)


def _trace_overlay(handle: TargetHandle) -> Any:
    return fusesoc_registry.write_trace_overlay(
        handle.selector,
        project_root=handle.project_root,
    )


def _is_cocotb(flow_options: Mapping[str, Any]) -> bool:
    module = flow_options.get("cocotb_module")
    return isinstance(module, str) and bool(module)


def _work_groups(selection: SimulationSelection, cocotb: bool) -> tuple[tuple[str, ...], ...]:
    if isinstance(selection, DefaultSelection):
        return ((),)
    if not isinstance(selection, NamedTests):
        raise TypeError(f"unsupported Simulation selection: {type(selection).__name__}")
    return (selection.names,) if cocotb else tuple((name,) for name in selection.names)


def _target_environment(handle: TargetHandle) -> dict[str, str]:
    sections = load_test_configuration_field(handle.project_root, "env")
    environment = dict(lookup_target_section(sections, handle.selector) or {})
    return {str(name): str(value) for name, value in environment.items()}


def _simulation_plusargs(
    handle: TargetHandle,
    test_names: tuple[str, ...],
    parameters: Mapping[str, Any],
) -> list[str]:
    rendered = _parameter_plusargs(parameters)
    if len(test_names) != 1:
        return rendered
    registry = load_test_configuration_field(handle.project_root, "tests")
    available = lookup_target_section(registry, handle.selector) or []
    if test_names[0] not in available:
        return rendered
    from booley.config.project_config import render_test_selector

    selector = render_test_selector(
        handle.selector,
        available.index(test_names[0]),
        test_names[0],
        work_dir=handle.project_root,
    ).removeprefix("+")
    key = _plusarg_key(selector)
    return [value for value in rendered if _plusarg_key(value) != key] + [selector]


def _parameter_plusargs(parameters: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for raw_name, raw_spec in parameters.items():
        if not isinstance(raw_spec, Mapping) or raw_spec.get("paramtype") != "plusarg":
            continue
        if "default" not in raw_spec:
            continue
        name = str(raw_name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            raise ValueError(f"invalid resolved plusarg parameter name: {name!r}")
        value = raw_spec["default"]
        text = "1" if value is True else "0" if value is False else str(value)
        if "\x00" in text:
            raise ValueError(f"resolved plusarg parameter {name!r} contains a NUL byte")
        result.append(f"{name}={text}")
    return result


def _plusarg_key(value: str) -> str | None:
    stripped = value.removeprefix("+")
    return None if stripped.startswith("-") else stripped.partition("=")[0] or None


def _simulation_run_cwd(root: Path, build_dir: str) -> str:
    if os.environ.get(selftest_overlay.INTERNAL_KIND_ENV) == selftest_overlay.BAD_KIND:
        return (Path(build_dir) / selftest_overlay.BAD_RUN_CWD_DIR).as_posix()
    return resolve_run_cwd(root)


def _preview_work(
    execution: SimulationExecution,
    handle: TargetHandle,
    inspection: Any,
    test_names: tuple[str, ...],
    cocotb: bool,
    rel: str,
) -> PreparedSimulationWork:
    root = handle.project_root
    eda_tool = sim_edam.normalize_eda_tool(inspection.eda_tool)
    passes, fails = resolve_sim_sentinels(root)
    return PreparedSimulationWork(
        adapter="cocotb" if cocotb else eda_tool,
        build_dir=rel,
        run_cwd=_simulation_run_cwd(root, rel),
        timeout_s=max(1, execution._effective_timeout_ms(handle) // 1000),
        eda_tool=eda_tool,
        max_rundir_bytes=resolve_max_rundir_bytes(root),
        plusargs=tuple(_simulation_plusargs(handle, test_names, inspection.parameters)),
        trace=execution._options.trace,
        trace_scope=inspection.toplevel,
        trace_args=tuple(resolve_trace_args(root)),
        trace_files=tuple(resolve_trace_files(root)),
        pass_sentinels=tuple(passes),
        fail_sentinels=tuple(fails),
        top=inspection.toplevel,
        cocotb_module=str(inspection.flow_options.get("cocotb_module") or ""),
        tests=test_names,
        result_verbosity=execution._options.result_verbosity,
        sim_time_grace_s=resolve_sim_time_grace_s(root),
    )


def _preview_exports(handle: TargetHandle, names: tuple[str, ...], build_root: Path) -> list[str]:
    root = handle.project_root
    values = {
        **_target_environment(handle),
        "BOOLEY_TARGET": handle.selector,
        "BOOLEY_TEST_NAMES": " ".join(names),
        "BOOLEY_PROJECT_ROOT": str(root),
        "BOOLEY_RUN_CWD": str((root / resolve_run_cwd(root)).resolve()),
        "BOOLEY_BUILD_ROOT": str(build_root),
    }
    if len(names) == 1:
        values["BOOLEY_TEST_NAME"] = names[0]
    return [f"export {name}={shlex.quote(value)}" for name, value in values.items()]


def _pre_run_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    evidence: PreRunEvidence,
    started: float,
) -> SimulationTargetOutcome:
    names = attempt.test_names or (handle.selector,)
    detail = evidence.detail or f"pre-run commands {evidence.status}"
    tests = tuple(
        SimulationTestOutcome(
            name=name,
            verdict="elab_error",
            passed=False,
            elapsed_s=evidence.elapsed_s,
            error_tail=f"pre-run commands failed ({evidence.status}): {detail}",
            elab_failed=True,
        )
        for name in names
    )
    tests = (
        replace(
            tests[0],
            phase_timings_s={
                "setup": round(attempt.setup_s, 3),
                "pre_run": round(evidence.elapsed_s, 3),
                "build": 0.0,
                "run": 0.0,
                "result_processing": 0.0,
            },
        ),
        *tests[1:],
    )
    return _group_outcome(handle, attempt, tests, None, evidence, (), started)


def _pre_run_infrastructure_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    evidence: PreRunEvidence,
    started: float,
) -> SimulationTargetOutcome:
    detail = evidence.detail or "could not start Pre-Run Commands"
    failure = SimulationInfrastructureFailure(
        "pre_run",
        "Pre-Run Commands could not start",
        missing_executable=find_missing_executable(detail) or "",
        detail=detail,
    )
    return _error_outcome(handle, attempt, None, evidence, failure, started)


def _infrastructure_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome,
    pre_run: PreRunEvidence | None,
    started: float,
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure("build", build.reason, detail=build.output)
    return _error_outcome(handle, attempt, build, pre_run, failure, started)


def _transport_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome,
    pre_run: PreRunEvidence | None,
    detail: str,
    started: float,
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure("adapter_protocol", detail, detail=detail)
    return _error_outcome(handle, attempt, build, pre_run, failure, started)


def _artifact_failure(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome,
    pre_run: PreRunEvidence | None,
    detail: str,
    started: float,
) -> SimulationTargetOutcome:
    failure = SimulationInfrastructureFailure("artifact_persistence", detail, detail=detail)
    return _error_outcome(handle, attempt, build, pre_run, failure, started)


def _error_outcome(
    handle: TargetHandle,
    attempt: _Attempt,
    build: BuildOutcome | None,
    pre_run: PreRunEvidence | None,
    failure: SimulationInfrastructureFailure,
    started: float,
) -> SimulationTargetOutcome:
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel=attempt.prepared.toplevel,
        eda_tool=attempt.prepared.eda_tool,
        passed=False,
        verdict="error",
        elapsed_s=time.monotonic() - started,
        tests=(),
        builds=(build,) if build is not None else (),
        pre_runs=(pre_run,) if pre_run is not None else (),
        infrastructure_failure=failure,
    )


def _setup_failure(handle: TargetHandle, detail: str, started: float) -> SimulationTargetOutcome:
    test = SimulationTestOutcome(
        name=handle.selector,
        verdict="elab_error",
        passed=False,
        error_tail=f"sim setup failed: {detail}",
        elab_failed=True,
    )
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel="",
        eda_tool="",
        passed=False,
        verdict="fail",
        elapsed_s=time.monotonic() - started,
        tests=(test,),
    )


def _group_outcome(
    handle: TargetHandle,
    attempt: _Attempt,
    tests: tuple[SimulationTestOutcome, ...],
    build: BuildOutcome | None,
    pre_run: PreRunEvidence | None,
    artifacts: tuple[SimulationArtifactEvidence, ...],
    started: float,
    diagnostics: tuple[str, ...] = (),
    adapter_passed: bool | None = None,
) -> SimulationTargetOutcome:
    inconclusive = any(test.inconclusive for test in tests)
    passed = (
        bool(tests)
        and all(test.passed for test in tests)
        and not inconclusive
        and adapter_passed is not False
    )
    elapsed_s = time.monotonic() - started
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel=attempt.prepared.toplevel,
        eda_tool=attempt.prepared.eda_tool,
        passed=passed,
        verdict="pass" if passed else "inconclusive" if inconclusive else "fail",
        elapsed_s=elapsed_s,
        tests=tests,
        builds=(build,) if build is not None else (),
        pre_runs=(pre_run,) if pre_run is not None else (),
        artifacts=artifacts,
        diagnostics=diagnostics,
        phase_timings_s=_target_phase_timings(tests, elapsed_s),
    )


def _aggregate(
    handle: TargetHandle,
    toplevel: str,
    groups: list[SimulationTargetOutcome],
    tests: tuple[SimulationTestOutcome, ...],
    builds: tuple[BuildOutcome, ...],
    pre_runs: tuple[PreRunEvidence, ...],
    artifacts: tuple[SimulationArtifactEvidence, ...],
    failure: SimulationInfrastructureFailure | None,
    started: float,
) -> SimulationTargetOutcome:
    inconclusive = any(test.inconclusive for test in tests)
    passed = (
        bool(tests)
        and all(test.passed for test in tests)
        and all(group.passed for group in groups)
        and failure is None
    )
    eda_tool = next((group.eda_tool for group in groups if group.eda_tool), "")
    verdict = (
        "error" if failure else "pass" if passed else "inconclusive" if inconclusive else "fail"
    )
    elapsed_s = time.monotonic() - started
    return SimulationTargetOutcome(
        target=handle.selector,
        target_identity=handle.identity,
        toplevel=toplevel,
        eda_tool=eda_tool,
        passed=passed,
        verdict=verdict,
        elapsed_s=elapsed_s,
        tests=tests,
        builds=builds,
        pre_runs=pre_runs,
        artifacts=artifacts,
        diagnostics=tuple(note for group in groups for note in group.diagnostics),
        infrastructure_failure=failure,
        phase_timings_s=_target_phase_timings(tests, elapsed_s),
    )


def _target_phase_timings(
    tests: tuple[SimulationTestOutcome, ...],
    elapsed_s: float,
) -> dict[str, float]:
    phases: dict[str, float] = {}
    for test in tests:
        for name, duration in test.phase_timings_s.items():
            phases[name] = phases.get(name, 0.0) + duration
    phases["unattributed"] = max(0.0, elapsed_s - sum(phases.values()))
    phases["execution_total"] = elapsed_s
    return {name: round(duration, 3) for name, duration in phases.items()}


def _test_outcomes(
    handle: TargetHandle,
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    adapter: AdapterResult | None,
    output: str,
    logs: tuple[SimulationArtifactEvidence, ...],
    trace: SimulationArtifactEvidence | None,
) -> tuple[SimulationTestOutcome, ...]:
    if build.design_failed:
        name = attempt.test_names[0] if attempt.test_names else handle.selector
        return (_build_failure_test(handle, attempt, process, build, _run_log_for(logs, name)),)
    names = _outcome_names(handle, attempt, adapter)
    by_name = {test.name: test for test in adapter.test_results} if adapter else {}
    results = []
    for index, name in enumerate(names):
        item = by_name.get(name)
        verdict = (
            item.verdict if item else "timeout" if process.timed_out else _adapter_verdict(adapter)
        )
        cycle_status, cycles = _cycle_observation(output, name, handle.project_root)
        results.append(
            _test_outcome(
                handle,
                attempt,
                process,
                build,
                adapter,
                item,
                name,
                verdict,
                cycle_status,
                cycles,
                _run_log_for(logs, name),
                trace if index == 0 else None,
                adapter.sva_errors if adapter is not None and index == 0 else 0,
            )
        )
    return tuple(results)


def _outcome_names(
    handle: TargetHandle,
    attempt: _Attempt,
    adapter: AdapterResult | None,
) -> tuple[str, ...]:
    if adapter and adapter.test_results:
        return tuple(test.name for test in adapter.test_results)
    return attempt.test_names or (handle.selector,)


def _adapter_verdict(adapter: AdapterResult | None) -> str:
    if adapter is None:
        return "fail"
    if adapter.passed:
        return "pass"
    return "inconclusive" if adapter.inconclusive else "fail"


def _test_outcome(
    handle: TargetHandle,
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    adapter: AdapterResult | None,
    item: Any,
    name: str,
    verdict: str,
    cycle_status: str,
    cycles: int | None,
    log: SimulationArtifactEvidence | None,
    trace: SimulationArtifactEvidence | None,
    sva_errors: int,
) -> SimulationTestOutcome:
    inconclusive = verdict == "inconclusive"
    passed = verdict == "pass" and adapter is not None and sva_errors == 0
    detail = item.detail if item else adapter.detail if adapter else ""
    reason = detail
    if verdict == "timeout" and not reason:
        reason = f"TIMEOUT: simulation exceeded {_timeout_ms(process)} ms"
    if inconclusive and not reason:
        reason = _NO_WAVEFORM if attempt.trace_requested and trace is None else _NO_SENTINEL
    return SimulationTestOutcome(
        name=name,
        verdict=verdict,
        passed=passed,
        elapsed_s=item.elapsed_s if item and item.elapsed_s else process.duration_s,
        cycles=cycles,
        cycle_status=cycle_status,
        inconclusive=inconclusive,
        reason=reason,
        sva_errors=sva_errors,
        error_tail="" if passed else reason or _output_tail(process, build.design_failed),
        timed_out=verdict == "timeout",
        build=build,
        artifacts=tuple(item for item in (log, trace) if item is not None),
        run_log_path=log.path if log is not None else "",
        workload_snapshot=_workload_snapshot(handle, attempt, name),
    )


def _timeout_ms(process: SubprocessResult) -> int:
    return max(1, round(process.duration_s * 1000))


def _build_failure_test(
    handle: TargetHandle,
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    log: SimulationArtifactEvidence | None,
) -> SimulationTestOutcome:
    name = attempt.test_names[0] if len(attempt.test_names) == 1 else handle.selector
    return SimulationTestOutcome(
        name=name,
        verdict="elab_error",
        passed=False,
        elapsed_s=process.duration_s,
        error_tail=_output_tail(process, True),
        elab_failed=True,
        build=build,
        artifacts=(log,) if log is not None else (),
        run_log_path=log.path if log is not None else "",
        workload_snapshot=_workload_snapshot(handle, attempt, name),
    )


def _workload_snapshot(handle: TargetHandle, attempt: _Attempt, name: str) -> Mapping[str, Any]:
    root = handle.project_root
    controls = {
        "cycle_sentinels": resolve_cycle_sentinels(root),
        "pre_run_commands": resolve_pre_run_commands(root),
        "run_cwd": resolve_run_cwd(root),
        "environment": _target_environment(handle),
    }
    return build_workload_snapshot(
        root,
        handle.selector,
        name,
        attempt.prepared.resolved,
        controls=controls,
    )


def _run_log_for(
    artifacts: tuple[SimulationArtifactEvidence, ...],
    name: str,
) -> SimulationArtifactEvidence | None:
    return next(
        (
            item
            for item in artifacts
            if item.kind == "run_log" and item.test_names == (name,)
        ),
        None,
    )


def _persist_run_logs(
    handle: TargetHandle,
    attempt: _Attempt,
    output: str,
    artifact_root: Path | None,
) -> tuple[SimulationArtifactEvidence, ...]:
    build_root = attempt.prepared.build_root
    try:
        live_path = write_run_log(build_root, output)
        live = validate_fresh_artifact(live_path, roots=(build_root,), before=None)
        archives = _archive_run_logs(handle, attempt, output, artifact_root)
    except (OSError, ArtifactValidationError) as exc:
        raise SimulationArtifactPersistenceError(f"could not preserve run log: {exc}") from exc
    live_evidence = SimulationArtifactEvidence(
        "live_run_log", str(live.path), live.size, attempt.test_names
    )
    return (live_evidence, *archives)


def _archive_run_logs(
    handle: TargetHandle,
    attempt: _Attempt,
    output: str,
    artifact_root: Path | None,
) -> tuple[SimulationArtifactEvidence, ...]:
    if artifact_root is None or not output:
        return ()
    names = attempt.test_names or (handle.selector,)
    target = artifact_path_component(f"sim_{handle.selector}")
    evidence: list[SimulationArtifactEvidence] = []
    for name in names:
        test = artifact_path_component(name)
        directory = artifact_root / "artifacts" / target / "tests" / test
        directory.mkdir(parents=True, exist_ok=True)
        path = write_run_log(directory, output, max_bytes=None)
        validated = validate_fresh_artifact(path, roots=(artifact_root,), before=None)
        evidence.append(
            SimulationArtifactEvidence("run_log", str(validated.path), validated.size, (name,))
        )
    return tuple(evidence)


def _trace_artifact(
    handle: TargetHandle,
    attempt: _Attempt,
    output: str,
    dispatched_ns: int,
) -> SimulationArtifactEvidence | None:
    matches = _TRACE_OK_RE.findall(output)
    if not matches:
        return None
    path = Path(matches[-1])
    run_cwd = Path(resolve_run_cwd(handle.project_root))
    if not run_cwd.is_absolute():
        run_cwd = handle.project_root / run_cwd
    if not path.is_absolute():
        path = run_cwd / path
    search_roots = (run_cwd.resolve(), attempt.prepared.build_root.resolve())
    patterns = tuple(resolve_trace_files(handle.project_root))
    allowed = (path,) if configured_trace_path(path, patterns, search_roots) else ()
    try:
        evidence = validate_fresh_artifact(
            path,
            roots=search_roots,
            before=None,
            explicitly_allowed=allowed,
            not_before_ns=dispatched_ns,
        )
    except ArtifactValidationError:
        return None
    scope, signals, ticks = _trace_metadata(output)
    return SimulationArtifactEvidence(
        "trace",
        str(evidence.path),
        evidence.size,
        attempt.test_names,
        scope,
        signals,
        ticks,
    )


def _missing_trace_result(adapter: AdapterResult) -> AdapterResult:
    tests = tuple(
        replace(test, verdict="inconclusive", detail=_NO_WAVEFORM)
        if test.verdict == "pass"
        else test
        for test in adapter.test_results
    )
    return replace(
        adapter,
        passed=False,
        inconclusive=True,
        failure_kind="artifact",
        detail=_NO_WAVEFORM,
        test_results=tests,
    )


def _trace_metadata(output: str) -> tuple[str, int, int]:
    matches = _TRACE_METADATA_RE.findall(output)
    if not matches:
        return "", 0, 0
    try:
        metadata = decode_trace_metadata(matches[-1])
    except (BoundaryError, json.JSONDecodeError):
        return "", 0, 0
    return metadata.display_scope, metadata.signal_count, metadata.total_ticks


def _cycle_observation(output: str, name: str, root: Path) -> tuple[str, int | None]:
    sentinels = resolve_cycle_sentinels(root) or [_DEFAULT_CYCLE_SENTINEL]
    records = _cycle_records(output, sentinels)
    named = [parts for parts in records if len(parts) >= 2 and " ".join(parts[:-1]) == name]
    legacy = [parts[0] for parts in records if len(parts) == 1]
    if len(named) == 1 and named[0][-1].isdigit():
        return "observed", int(named[0][-1])
    if len(named) > 1 or len(legacy) > 1:
        return "duplicate", None
    if len(legacy) == 1 and legacy[0].isdigit():
        return "legacy", int(legacy[0])
    return ("wrong_test" if records else "missing"), None


def _cycle_records(output: str, sentinels: list[str]) -> list[list[str]]:
    records = []
    for line in output.splitlines():
        sentinel = next((value for value in sentinels if value in line), None)
        if sentinel is not None:
            records.append(line.split(sentinel, 1)[1].strip().split())
    return records


def _attach_group_telemetry(
    tests: tuple[SimulationTestOutcome, ...],
    attempt: _Attempt,
    process: SubprocessResult,
    build: BuildOutcome,
    pre_run: PreRunEvidence | None,
    output: str,
    result_processing_s: float,
) -> tuple[SimulationTestOutcome, ...]:
    if not tests:
        return tests
    build_s = parse_build_seconds(output)
    run_s = parse_run_seconds(output)
    if run_s is None:
        run_s = max(0.0, process.duration_s - build_s) if build.passed else 0.0
    phases = {
        "setup": round(attempt.setup_s, 3),
        "pre_run": round(pre_run.elapsed_s if pre_run else 0.0, 3),
        "build": round(build_s, 3),
        "run": round(run_s, 3),
        "result_processing": round(result_processing_s, 3),
    }
    first = replace(
        tests[0],
        build_s=build_s,
        phase_timings_s=phases,
        resources=process_resources(output, process),
    )
    return (first, *tests[1:])


def _output_tail(process: SubprocessResult, build_failed: bool) -> str:
    combined = process.stdout + ("\n" + process.stderr if process.stderr else "")
    source = combined if build_failed or not process.stdout.strip() else process.stdout
    return "\n".join(source.strip().splitlines()[-50:])


__all__ = ["AdapterEvidenceError", "SimulationExecution", "read_completed_adapter_result"]
