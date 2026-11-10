"""Drive ordinary Simulation through the real campaign path with a faked process edge.

Stdlib + booley only (no pytest import), like ``prepared_group``, so every test
module can share it without importing another test module.

Production reaches ordinary simulation through ``SimulateFlow.execute``: the
endpoint prepares the campaign, ``OrdinaryHdlSerialExecutor`` plans and runs one
work item per test and the acceptance coordinator records Criteria. Only the
edges that need a real toolchain are faked here: the build preparation, the
leased build session's input capture and image authorization, and
``SimulateFlow._execute_boundary`` (the campaign's process boundary), which
answers the build-stage command with a ``BOOLEY_BUILD_STAGE`` record and the
launch command with a canned ``SubprocessResult`` plus the authenticated
adapter result the real adapter would have written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTraceResult,
    AdapterTransportIdentity,
    work_transport_identity,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb as cocotb_backend
from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.backends.shared import RunTermination
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import SimulationBuildSession
from booley.flows.sim.execution import SimulationExecution
from booley.flows.sim.flow import _TRACE_OK_RE, SimulateFlow, _trace_metadata
from booley.flows.sim.request import SimRequest
from booley.flows.sim.result import count_sva_errors, parse_sim_verdict, parse_summary_line
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.runtime.endpoint_execution import ExecutionResult
from booley.targets.catalog import TargetCatalog
from tests.flows.sim.launched_work import launched_work
from tests.target_test_support import write_sim_preview_project

# Canned simulator processes shared by the campaign-path tests.
PASS_PROCESS = SubprocessResult(
    returncode=0,
    stdout='[SIM_RESULT] PASSED\n[SIM_CYCLES] 2561\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n',
    duration_s=1.0,
)
FAIL_PROCESS = SubprocessResult(
    returncode=1,
    stdout='[SIM_RESULT] FAILED\n[SIM_CYCLES] 8012\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n',
    duration_s=1.0,
)
# rc=0 with neither a sentinel nor a summary: the grade is inconclusive.
INCONCLUSIVE_PROCESS = SubprocessResult(
    returncode=0, stdout="simulation finished\nno sentinel here\n", duration_s=1.0
)

# Distinguishes "use tmp_path/reports" from an explicit ``None`` (endpoint default root).
_DEFAULT_REPORT_DIR: Any = object()

_BUILD_TOKEN = re.compile(r"BOOLEY_BUILD_STAGE token=(\S+?)[\s\"']")


@dataclass(frozen=True)
class Launch:
    """One simulator launch the campaign handed to the process edge."""

    target: str
    tests: tuple[str, ...]
    # The checkout the command runs in (a Cycle Count baseline's is not the invocation's).
    cwd: Path
    # The launch shell script, for the environment it exports.
    script: str = field(repr=False)
    # The decoded work document: adapter, plusargs, cocotb module, trace scope, ...
    work: PreparedSimulationWork = field(repr=False)


ProcessFactory = Callable[[Launch], SubprocessResult]


@dataclass
class CampaignSimRun:
    """Outcome of one campaign-path simulation."""

    result: ExecutionResult
    flow: SimulateFlow
    # Every simulator launch, in order.
    launches: list[Launch]

    @property
    def launched(self) -> list[tuple[str, tuple[str, ...]]]:
        """``(target, selected tests)`` of every simulator launch, in order."""
        return [(launch.target, launch.tests) for launch in self.launches]


def run_campaign_sim(
    tmp_path: Path,
    monkeypatch: Any,
    targets: dict[str, list[str]],
    process: SubprocessResult | ProcessFactory,
    *,
    skip: dict[str, list[str]] | None = None,
    files: Mapping[str, str] | None = None,
    build: SubprocessResult | None = None,
    report_dir: Path | None = _DEFAULT_REPORT_DIR,
    project_data: Path | None = None,
    adapter: Any = None,
    **request: Any,
) -> CampaignSimRun:
    """Run ``SimulateFlow.execute`` for *targets* with only the process edge faked.

    *targets* maps each Target to its tests. *process* is the canned simulator
    result (or a callable of ``(target, tests)``); *build* answers the build
    stage (default: success). The adapter result is derived from *process*; a
    timed-out launch writes none (the wrapper killed the adapter). *files* maps
    paths under *tmp_path* to text written after the default project (it adds
    or overrides ``.core`` files, ``.booley/tests.toml``, ``.booley/booley.toml``).
    ``report_dir=None`` keeps the endpoint default report root; *project_data*
    places ``.booley`` outside *tmp_path*.
    """
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    data = write_sim_preview_project(tmp_path, targets)
    for relative, text in (files or {}).items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    if project_data is not None:
        project_data.mkdir(parents=True, exist_ok=True)
        for entry in data.iterdir():
            entry.replace(project_data / entry.name)
        data.rmdir()
        data = project_data
    if skip:
        registry = data / "tests.toml"
        text = registry.read_text(encoding="utf-8")
        for name, names in skip.items():
            text = text.replace(f"[{name}]\n", f"[{name}]\nskip = {json.dumps(names)}\n")
        registry.write_text(text, encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    reset_cache()
    launches: list[Launch] = []
    flow = SimulateFlow()
    if report_dir is _DEFAULT_REPORT_DIR:
        report_dir = tmp_path / "reports"
    # Undo the fakes before returning: an autouse fixture may have patched the same
    # class attributes, and a test-long undo would restore its patch after it exits.
    with monkeypatch.context() as scoped:
        _install_fakes(scoped, process, build, launches)
        result = flow.execute(
            SimRequest(work_dir=tmp_path, report_dir=report_dir, **request),
            adapter=adapter,
        )
    return CampaignSimRun(result, flow, launches)


def _install_fakes(
    monkeypatch: Any,
    process: SubprocessResult | ProcessFactory,
    build: SubprocessResult | None,
    launches: list[Launch],
) -> None:
    def prepare_build(self: SimulationExecution, handle: Any) -> Any:
        assert self._build_session is not None
        work_root = self._build_session.new_generation()
        build_root = work_root / "build"
        build_root.mkdir()
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        (build_root / "Vtop").write_bytes(b"image")
        module = inspection.flow_options.get("cocotb_module")
        cocotb_module = module if isinstance(module, str) else None
        resolved = ResolvedTarget(
            name=handle.selector,
            vlnv=handle.vlnv,
            toplevel=inspection.toplevel,
            eda_tool=inspection.eda_tool or "verilator",
            files=(),
            parameters={},
            build_root=build_root,
            edam_path=build_root / "test.eda.yml",
            flow_options={},
            cocotb_module=cocotb_module,
        )
        prepared = PreparedSimulationBuild(
            handle.selector,
            handle.identity,
            resolved,
            work_root,
            build_root,
            resolved.eda_tool,
            inspection.toplevel,
            ("make", "-C", str(build_root)),
        )
        return prepared, TraceMode.VCD_FIFO

    def boundary(
        self: SimulateFlow, command: list[str], *, timeout: float, cwd: Path, **_kw: Any
    ) -> Any:
        del self, timeout
        script = command[-1]
        if "BOOLEY_BUILD_STAGE" in script:
            token = _BUILD_TOKEN.search(script)
            assert token is not None
            built = build if build is not None else SubprocessResult(returncode=0)
            record = f"BOOLEY_BUILD_STAGE token={token.group(1)} rc={built.returncode}\n"
            return SubprocessResult(
                returncode=built.returncode,
                stdout=built.stdout + record,
                stderr=built.stderr,
            )
        work = launched_work(script.splitlines()[-1])
        identity = work_transport_identity(work)
        assert identity is not None
        launch = Launch(
            identity.target_identity.split("#")[-1], identity.selected_tests, cwd, script, work
        )
        launches.append(launch)
        canned = process(launch) if callable(process) else process
        if not canned.timed_out:
            if work.adapter == "cocotb":
                result = _cocotb_adapter_result(identity, canned)
            else:
                result = adapter_result_for_test_process(launch.tests, canned)
            write_adapter_result(identity, result)
        return canned

    monkeypatch.setattr(SimulationExecution, "_prepare_build", prepare_build)
    monkeypatch.setattr(SimulateFlow, "_execute_boundary", boundary)
    monkeypatch.setattr(SimulationBuildSession, "capture_inputs", lambda *_a, **_k: {})
    monkeypatch.setattr(SimulationBuildSession, "reusable_key", lambda *_a, **_k: None)
    monkeypatch.setattr(
        SimulationBuildSession,
        "authorize_fresh_image",
        lambda _self, prepared, *_a, **_k: (prepared.build_root / "Vtop",),
    )


def _cocotb_adapter_result(
    identity: AdapterTransportIdentity, process: SubprocessResult
) -> AdapterResult:
    """The result the production Cocotb adapter publishes for *process*'s output.

    Composes the adapter's own verdict assessment and result builder: only the
    simulator run (which would have produced ``results.xml``) is replaced by the
    transported ``[COCOTB_RESULTS]`` line, or by a missing ``results.xml`` when
    the run printed none.
    """
    output = _process_output(process)
    names = list(identity.selected_tests)
    timed_out = "cocotb simulation timed out" in output.lower()
    termination = (
        RunTermination("timeout", "cocotb simulation timed out", "timeout")
        if timed_out
        else RunTermination()
    )
    parsed = cocotb_results.parse_results_line(output) or cocotb_results.parse_results_xml(
        Path("absent") / "results.xml"
    )
    results = cocotb_backend._recover_partial_timeout_results(  # type: ignore[reportPrivateUsage]
        parsed, output, timed_out, names
    )
    _focused, assessment = cocotb_backend._assess_verdict(  # type: ignore[reportPrivateUsage]
        results, output, process.returncode, termination, names, ""
    )
    failure_kind = (
        termination.failure_kind
        if termination.aborted
        else "design"
        if process.returncode != 0 or count_sva_errors(output)
        else ""
    )
    return cocotb_backend._cocotb_adapter_result(  # type: ignore[reportPrivateUsage]
        identity,
        output,
        results,
        assessment.passed,
        failure_kind,
        "",
        _test_trace_result(output),
        termination,
        process.returncode,
    )


def _process_output(process: SubprocessResult) -> str:
    return process.stdout + ("\n" + process.stderr if process.stderr else "")


def adapter_result_for_test_process(
    names: tuple[str, ...],
    process: SubprocessResult,
) -> AdapterResult:
    """The result the native adapter publishes for *process* (sentinels and summary)."""
    output = _process_output(process)
    try:
        summary = parse_summary_line(output)
    except ValueError:
        summary = None
    return _native_adapter_result(names, process, output, summary)


def _native_adapter_result(names, process, output, summary) -> AdapterResult:
    sentinel = parse_sim_verdict(output)
    passed = bool(
        process.returncode == 0
        and (summary["passed"] if summary is not None else sentinel is True)
    )
    inconclusive = summary is None and sentinel is None and not process.timed_out
    verdict = (
        "timeout"
        if process.timed_out
        else "pass"
        if passed
        else "inconclusive"
        if inconclusive
        else "fail"
    )
    tests = tuple(AdapterTestResult(name, verdict, process.duration_s) for name in names)
    sva_errors = int(summary.get("sva_errors", 0)) if summary else 0
    return _test_adapter_result(
        names,
        tests,
        passed,
        inconclusive,
        sva_errors,
        (),
        _test_trace_result(output),
        simulator_returncode=process.returncode,
    )


def _test_trace_result(output: str) -> AdapterTraceResult | None:
    paths = _TRACE_OK_RE.findall(output)
    if not paths:
        return None
    top_scope, signal_count, total_ticks = _trace_metadata(output)
    return AdapterTraceResult(
        "ok",
        path=paths[-1],
        top_scope=top_scope,
        signal_count=signal_count,
        total_ticks=total_ticks,
    )


def _test_adapter_result(
    names,
    tests,
    passed,
    inconclusive,
    sva_errors,
    diagnostics,
    trace=None,
    *,
    simulator_returncode=0,
    termination="completed",
    termination_detail="",
):
    timed_out = any(test.verdict == "timeout" for test in tests)
    return AdapterResult(
        passed=passed,
        inconclusive=inconclusive,
        sva_errors=sva_errors,
        tests=names,
        simulator_returncode=simulator_returncode,
        termination=termination,
        failure_kind=(
            "timeout"
            if timed_out
            else "inconclusive"
            if inconclusive
            else "design"
            if not passed
            else ""
        ),
        detail=termination_detail,
        test_results=tests,
        diagnostics=diagnostics,
        trace=trace,
    )
