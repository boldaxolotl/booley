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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTraceResult,
    work_transport_identity,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import SimulationBuildSession
from booley.flows.sim.execution import SimulationExecution
from booley.flows.sim.flow import _TRACE_OK_RE, SimulateFlow, _trace_metadata
from booley.flows.sim.request import SimRequest
from booley.flows.sim.result import parse_sim_verdict, parse_summary_line
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

ProcessFactory = Callable[[str, tuple[str, ...]], SubprocessResult]


@dataclass
class CampaignSimRun:
    """Outcome of one campaign-path simulation."""

    result: ExecutionResult
    flow: SimulateFlow
    # (target, selected tests) for every simulator launch, in order.
    launched: list[tuple[str, tuple[str, ...]]]


def run_campaign_sim(
    tmp_path: Path,
    monkeypatch: Any,
    targets: dict[str, list[str]],
    process: SubprocessResult | ProcessFactory,
    *,
    skip: dict[str, list[str]] | None = None,
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
    timed-out launch writes none (the wrapper killed the adapter). ``report_dir=None`` keeps the endpoint default
    report root; *project_data* places ``.booley`` outside *tmp_path*.
    """
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    data = write_sim_preview_project(tmp_path, targets)
    if project_data is not None:
        project_data.mkdir(parents=True, exist_ok=True)
        (data / "tests.toml").replace(project_data / "tests.toml")
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
    launched: list[tuple[str, tuple[str, ...]]] = []
    flow = SimulateFlow()
    if report_dir is _DEFAULT_REPORT_DIR:
        report_dir = tmp_path / "reports"
    # Undo the fakes before returning: an autouse fixture may have patched the same
    # class attributes, and a test-long undo would restore its patch after it exits.
    with monkeypatch.context() as scoped:
        _install_fakes(scoped, process, build, launched)
        result = flow.execute(
            SimRequest(work_dir=tmp_path, report_dir=report_dir, **request),
            adapter=adapter,
        )
    return CampaignSimRun(result, flow, launched)


def _install_fakes(
    monkeypatch: Any,
    process: SubprocessResult | ProcessFactory,
    build: SubprocessResult | None,
    launched: list[tuple[str, tuple[str, ...]]],
) -> None:
    def prepare_build(self: SimulationExecution, handle: Any) -> Any:
        assert self._build_session is not None
        work_root = self._build_session.new_generation()
        build_root = work_root / "build"
        build_root.mkdir()
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        (build_root / "Vtop").write_bytes(b"image")
        resolved = ResolvedTarget(
            name=handle.selector,
            vlnv=handle.vlnv,
            toplevel=inspection.toplevel,
            eda_tool="verilator",
            files=(),
            parameters={},
            build_root=build_root,
            edam_path=build_root / "test.eda.yml",
            flow_options={},
            cocotb_module=None,
        )
        prepared = PreparedSimulationBuild(
            handle.selector,
            handle.identity,
            resolved,
            work_root,
            build_root,
            "verilator",
            inspection.toplevel,
            ("make", "-C", str(build_root)),
        )
        return prepared, TraceMode.VCD_FIFO

    def boundary(self: SimulateFlow, command: list[str], *, timeout: float, **_kw: Any) -> Any:
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
        identity = work_transport_identity(launched_work(script.splitlines()[-1]))
        assert identity is not None
        target = identity.target_identity.split("#")[-1]
        selected = identity.selected_tests
        launched.append((target, selected))
        canned = process(target, selected) if callable(process) else process
        if not canned.timed_out:
            write_adapter_result(identity, adapter_result_for_test_process(selected, canned))
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


def adapter_result_for_test_process(
    names: tuple[str, ...],
    process: SubprocessResult,
) -> AdapterResult:
    output = process.stdout + ("\n" + process.stderr if process.stderr else "")
    try:
        summary = parse_summary_line(output)
    except ValueError:
        summary = None
    parsed = cocotb_results.parse_results_line(output)
    if parsed is not None:
        return _cocotb_adapter_result(names, process, output, summary, parsed)
    return _native_adapter_result(names, process, output, summary)


def _cocotb_adapter_result(names, process, output, summary, parsed) -> AdapterResult:
    timed_out = "cocotb simulation timed out" in output.lower() or process.timed_out
    if timed_out:
        parsed = cocotb_results.recover_timeout_progress(output, list(names), parsed)
    elapsed = {test.name: test.elapsed_s for test in parsed.tests}
    tests = tuple(
        AdapterTestResult(
            name,
            "timeout" if detail == cocotb_results.TIMEOUT_ACTIVE_DETAIL else verdict,
            elapsed.get(name, process.duration_s),
            detail,
        )
        for name, verdict, detail in cocotb_results.reconcile(list(names), parsed)
    )
    extras = tuple(test.name for test in parsed.tests if test.name not in names)
    diagnostics = (
        (
            "results.xml reports extra non-selected test(s): "
            f"{', '.join(extras)} — logged, not verdict-bearing",
        )
        if extras
        else ()
    )
    sva_errors = int(summary.get("sva_errors", 0)) if summary else 0
    tests = _normalize_canned_cocotb_timeout(tests, timed_out)
    inconclusive = not timed_out and any(test.verdict == "inconclusive" for test in tests)
    passed = bool(
        process.returncode == 0
        and summary is not None
        and summary["passed"]
        and sva_errors == 0
        and tests
        and all(test.verdict == "pass" for test in tests)
    )
    return _test_adapter_result(
        names,
        tests,
        passed,
        inconclusive,
        sva_errors,
        diagnostics,
        _test_trace_result(output),
        simulator_returncode=process.returncode,
        termination="timeout" if timed_out else "completed",
        termination_detail="cocotb simulation timed out" if timed_out else "",
    )


def _normalize_canned_cocotb_timeout(tests, timed_out):
    if not timed_out:
        return tests
    detail = "cocotb simulation timed out"
    return tuple(
        test
        if test.verdict in {"pass", "fail"}
        else AdapterTestResult(
            test.name,
            "timeout",
            test.elapsed_s,
            detail,
            "timeout",
            "timeout",
        )
        for test in tests
    )


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
