"""Production-interface tests for the deep Simulation execution boundary."""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from booley.core.build_paths import work_root_for
from booley.core.file_lock import acquire_file_lock
from booley.flows import run_log
from booley.flows.base import SubprocessResult
from booley.flows.memory_admission import JOB_THREADS_ENV
from booley.flows.sim import build_parallelism
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTraceResult,
    AdapterTransportIdentity,
    partial_result_identity,
    publish_native_adapter_result,
    write_adapter_result,
)
from booley.flows.sim.backends.shared import RunTermination
from booley.flows.sim.build import (
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    prepare_simulation_build,
)
from booley.flows.sim.build_session import (
    SimulationBuildLeaseTimeoutError,
    SimulationBuildSession,
    SimulationBuildSlotError,
    TargetCompileSurface,
    simulation_build_slot,
)
from booley.flows.sim.execution import (
    NamedTests,
    SimulationExecution,
    SimulationOptions,
    SimulationTargetOutcome,
)
from booley.flows.sim.execution.engine import (
    PreparedOrdinaryGroup,
)
from booley.flows.sim.execution.freshness import ArtifactValidationError
from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.flows.sim.live_progress import LiveProgressSink, install_progress
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.trace_session import TraceSession
from booley.flows.terminal_progress import TerminalProgress
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.runtime import paths as runtime_paths
from booley.runtime import resource_history
from booley.runtime.booley_dir import reset_cache
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from tests.canned_process_support import canned_command, measured_flow
from tests.flows.sim.launched_work import launched_work
from tests.flows.sim.leased_session import canned_build_session, split_invoker
from tests.flows.sim.prepared_group import run_prepared_group
from tests.flows.sim.test_verilator_build_reuse_engine import (
    _edit_keeping_mtime,
    _isolate_native_cores,
)
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import hang_guard_s


def _handle(root: Path, *, selector: str = "sim", eda_tool: str | None = "icarus") -> TargetHandle:
    return cast(
        TargetHandle,
        SimpleNamespace(
            project_root=root.resolve(),
            selector=selector,
            identity=f"acme:lib:demo:1#{selector}",
            vlnv="acme:lib:demo:1",
            eda_tool=eda_tool,
            snapshot_id="",
        ),
    )


def _prepared(handle: TargetHandle, *, cocotb: bool) -> PreparedSimulationBuild:
    build_root = handle.project_root / "build" / handle.selector
    build_root.mkdir(parents=True, exist_ok=True)
    resolved = ResolvedTarget(
        name=handle.selector,
        vlnv=handle.vlnv,
        toplevel="tb_demo",
        eda_tool="icarus",
        files=(),
        parameters={},
        build_root=build_root,
        edam_path=build_root / "demo.eda.yml",
        flow_options={"tool": "icarus", "cocotb_module": "test_demo"} if cocotb else {},
        cocotb_module="test_demo" if cocotb else None,
    )
    return PreparedSimulationBuild(
        target=handle.selector,
        target_identity=handle.identity,
        resolved=resolved,
        work_root=build_root,
        build_root=build_root,
        eda_tool="icarus",
        toplevel="tb_demo",
        make_argv=("make", "-C", str(build_root)),
    )


def _prepared_group_with_sources(
    handle: TargetHandle,
    *sources: Path,
    trace_requested: bool = False,
) -> PreparedOrdinaryGroup:
    prepared = _prepared(handle, cocotb=False)
    prepared = replace(
        prepared,
        resolved=replace(
            prepared.resolved,
            files=tuple(ResolvedFile(str(source), "systemVerilogSource") for source in sources),
        ),
    )
    return PreparedOrdinaryGroup(
        MagicMock(),
        handle,
        cast(Any, SimpleNamespace(prepared=prepared, trace_requested=trace_requested)),
        MagicMock(),
        0.0,
        TargetCompileSurface(handle.project_root, (), ()),
        {},
        {},
        {},
        {},
        (),
    )


def _inspection(*, cocotb: bool) -> SimpleNamespace:
    inspection = SimpleNamespace(
        toplevel="tb_demo",
        eda_tool="icarus",
        parameters={},
        flow_options={"cocotb_module": "test_demo"} if cocotb else {},
        tool_options={},
        inputs=(),
        core_closure=lambda _handles: (),
        compile_inputs=lambda handle: TargetCompileSurface(handle.project_root, (), ()),
        rtl_files=(),
        tb_files=(),
    )
    inspection.inspect = lambda _handle: inspection
    return inspection


def _write_vvp(fake_bin: Path, needs: str) -> None:
    """A ``vvp`` that passes only when its run directory holds the file *needs*."""
    message = f"$readmemh: Unable to open {needs} for reading."
    if os.name == "nt":
        script = (
            f"@echo off\nif not exist {needs} (\n  echo {message}\n  exit /b 1\n)\n"
            "echo [SIM_RESULT] PASSED\n"
        )
    else:
        script = (
            f"#!/bin/sh\nif [ ! -f {needs} ]; then\n  echo '{message}'\n  exit 1\nfi\n"
            "echo '[SIM_RESULT] PASSED'\n"
        )
    vvp = fake_bin / ("vvp.bat" if os.name == "nt" else "vvp")
    vvp.write_text(script, encoding="utf-8")
    vvp.chmod(0o755)


def _runtime_input_vvp(root: Path, build_root: Path) -> Path:
    (build_root / "demo.scr").write_text("", encoding="utf-8")
    (build_root / "demo").write_text("", encoding="utf-8")
    (build_root / "dhry.hex").write_text("00000013\n", encoding="utf-8")
    fake_bin = root / "bin"
    fake_bin.mkdir()
    _write_vvp(fake_bin, "dhry.hex")
    return fake_bin


def _write_runtime_input_project(
    root: Path,
    *,
    tool_declaration: str = "    flow: sim\n    flow_options: {tool: icarus}\n",
) -> Path:
    state = root / ".booley"
    state.mkdir(parents=True)
    (state / "booley.toml").write_text("", encoding="utf-8")
    (root / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")
    firmware = root / "data" / "firmware.hex"
    firmware.parent.mkdir()
    firmware.write_text("00000013\n", encoding="utf-8")
    (root / "runtime_input.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:runtime_input:1\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - tb.sv: {file_type: systemVerilogSource, tags: [tb]}\n"
        "      - data/firmware.hex: {file_type: user, copyto: firmware.hex}\n"
        "targets:\n"
        "  sim:\n"
        f"{tool_declaration}"
        "    filesets: [tb]\n"
        "    toplevel: tb\n",
        encoding="utf-8",
    )
    return state


def _write_fake_icarus_tools(root: Path) -> Path:
    fake_bin = root / "bin"
    fake_bin.mkdir()
    suffix = ".bat" if os.name == "nt" else ""
    iverilog = fake_bin / f"iverilog{suffix}"
    if os.name == "nt":
        iverilog_script = (
            "@echo off\n"
            ":loop\n"
            'if "%~1"=="" exit /b 2\n'
            'if "%~1"=="-o" goto output\n'
            "shift\n"
            "goto loop\n"
            ":output\n"
            "shift\n"
            'type nul > "%~1"\n'
        )
    else:
        iverilog_script = (
            "#!/bin/sh\n"
            "output=''\n"
            'while [ "$#" -gt 0 ]; do\n'
            "  if [ \"$1\" = '-o' ]; then output=$2; shift 2; else shift; fi\n"
            "done\n"
            '[ -n "$output" ] || exit 2\n'
            'touch "$output"\n'
        )
    iverilog.write_text(iverilog_script, encoding="utf-8")
    iverilog.chmod(0o755)
    _write_vvp(fake_bin, "firmware.hex")
    return fake_bin


def _write_stale_compiler_fixture(root: Path) -> tuple[Path, Path]:
    """A compiler that deliberately leaves an existing image stale."""
    project = root / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    source = project / "tb.sv"
    source.write_text("module tb; // OLD\nendmodule\n", encoding="utf-8")
    (project / "multi.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:multi:1\n"
        "filesets:\n"
        "  tb:\n"
        "    files: [tb.sv]\n"
        "    file_type: systemVerilogSource\n"
        "targets:\n"
        "  sim_a:\n"
        "    flow: sim\n"
        "    flow_options: {tool: icarus}\n"
        "    filesets: [tb]\n"
        "    toplevel: tb\n"
        "  sim_b:\n"
        "    flow: sim\n"
        "    flow_options: {tool: icarus}\n"
        "    filesets: [tb]\n"
        "    toplevel: tb\n",
        encoding="utf-8",
    )
    fake_bin = root / "bin"
    fake_bin.mkdir()
    compiler = fake_bin / "iverilog"
    compiler.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "if not output.exists():\n"
        "    source = next(pathlib.Path.cwd().rglob('tb.sv'))\n"
        "    if 'BAD' in source.read_text(): sys.exit(1)\n"
        "    word = 'NEW' if 'NEW' in source.read_text() else 'OLD'\n"
        "    output.write_text('[SIM_RESULT] PASSED\\n' + word + '\\n')\n",
        encoding="utf-8",
    )
    compiler.chmod(0o755)
    runner = fake_bin / ("vvp.bat" if os.name == "nt" else "vvp")
    if os.name == "nt":
        runner.write_text(
            "@echo off\n"
            'type "%~3"\n'
            'if exist "%~dp3hook.txt" for /f "usebackq delims=" %%A in '
            '("%~dp3hook.txt") do echo HOOK=%%A\n',
            encoding="utf-8",
        )
    else:
        runner.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "image = next(pathlib.Path(arg) for arg in sys.argv[1:] "
            "if pathlib.Path(arg).is_file())\n"
            "print(image.read_text(), end='')\n"
            "hook = image.parent / 'hook.txt'\n"
            "if hook.exists(): print('HOOK=' + hook.read_text())\n",
            encoding="utf-8",
        )
    runner.chmod(0o755)
    return project, source


def _subprocess_invoker(root: Path) -> Callable[..., SubprocessResult]:
    source_root = Path(__file__).resolve().parents[3] / "src"
    python_path = os.pathsep.join(
        part for part in (str(source_root), os.environ.get("PYTHONPATH", "")) if part
    )

    def invoke(command: list[str], *, timeout: int, cwd: Path | None = None) -> SubprocessResult:
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=cwd or root,
            env={**os.environ, "PYTHONPATH": python_path},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return SubprocessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_s=time.monotonic() - started,
        )

    return invoke


def _compile_surface_patch(handle: TargetHandle) -> AbstractContextManager[object]:
    return patch(
        "booley.flows.sim.execution.engine.resolve_target_compile_surface",
        return_value=TargetCompileSurface(handle.project_root, (), ()),
    )


def _run_execution(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    invoke,
    names: tuple[str, ...] | None,
    *,
    cocotb: bool,
    options: SimulationOptions | None = None,
    trace_mode: TraceMode | None = None,
    canned_at: str = "launch",
) -> SimulationTargetOutcome:
    """Run one group on the prepared path, as a record does."""
    execution = SimulationExecution(
        invoke=split_invoker(invoke, canned_at=canned_at),
        options=options or SimulationOptions(),
    )
    with ExitStack() as stack:
        stack.enter_context(canned_build_session())
        stack.enter_context(
            patch(
                "booley.flows.sim.execution.engine.TargetCatalog.build",
                return_value=_inspection(cocotb=cocotb),
            )
        )
        stack.enter_context(_compile_surface_patch(handle))
        stack.enter_context(
            patch(
                "booley.flows.sim.execution.engine.prepare_simulation_build",
                return_value=prepared,
            )
        )
        if trace_mode is not None:
            stack.enter_context(
                patch(
                    "booley.flows.sim.execution.engine.trace_overlay.write_trace_overlay",
                    return_value=SimpleNamespace(vlnv=None, mode=trace_mode, cleanup=lambda: None),
                )
            )
        stack.enter_context(
            patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123")
        )
        return run_prepared_group(
            execution,
            handle,
            names or (),
            attempt_root=handle.project_root / "attempt",
            copy_build_root=True,
        )


def _evidence_path(handle: TargetHandle) -> Path:
    """Adapter transport path of a prepared-path launch under the test attempt root."""
    return handle.project_root / "attempt" / "execution-evidence" / "adapter-abc123.json"


def _write_transport(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    names: tuple[str, ...],
    result: AdapterResult,
    *,
    adapter: str = "icarus",
) -> None:
    identity = AdapterTransportIdentity(
        adapter,
        "abc123",
        handle.identity,
        names,
        _evidence_path(handle),
    )
    write_adapter_result(identity, result)


def _write_partial_timeout_transport(
    handle,
    prepared,
    *,
    modified_ns: int | None = None,
    partial: bool = True,
) -> None:
    identity = AdapterTransportIdentity(
        "cocotb",
        "abc123",
        handle.identity,
        ("done", "active", "later"),
        _evidence_path(handle),
    )
    result = AdapterResult(
        False,
        False,
        0,
        identity.selected_tests,
        simulator_returncode=-9,
        termination="timeout",
        failure_kind="timeout",
        detail="cocotb batch ended before terminal adapter transport",
        test_results=(
            AdapterTestResult("done", "pass"),
            AdapterTestResult(
                "active",
                "timeout",
                detail="timed out while running",
                termination="timeout",
                failure_kind="timeout",
            ),
            AdapterTestResult(
                "later",
                "timeout",
                detail="not run before timeout",
                termination="timeout",
                failure_kind="timeout",
            ),
        ),
    )
    written = partial_result_identity(identity) if partial else identity
    write_adapter_result(written, result)
    if modified_ns is not None:
        os.utime(written.result_path, ns=(modified_ns, modified_ns))


def _passing_result() -> AdapterResult:
    return AdapterResult(
        True, False, 0, ("smoke",), test_results=(AdapterTestResult("smoke", "pass"),)
    )


def _passing_trace_invoker(
    handle,
    prepared,
    trace: Path,
    *,
    fresh: bool,
    modified_ns: int | None = None,
    transport_modified_ns: int | None = None,
    report_trace: bool = True,
):
    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        if fresh:
            trace.write_bytes(b"fresh waveform")
            if modified_ns is not None:
                os.utime(trace, ns=(modified_ns, modified_ns))
        _write_transport(
            handle,
            prepared,
            ("smoke",),
            AdapterResult(
                True,
                False,
                0,
                ("smoke",),
                test_results=(AdapterTestResult("smoke", "pass"),),
                trace=AdapterTraceResult("ok", path=str(trace)) if report_trace else None,
            ),
        )
        if transport_modified_ns is not None:
            result_path = _evidence_path(handle)
            os.utime(result_path, ns=(transport_modified_ns, transport_modified_ns))
        return SubprocessResult(
            returncode=0,
            stdout=f"BOOLEY_BUILD_STAGE token=abc123 rc=0\nTRACE_OK: {trace}\n",
        )

    return invoke


def _assert_authoritative_cocotb_outcome(outcome: SimulationTargetOutcome) -> None:
    assert outcome.verdict == "fail"
    assert [(test.name, test.verdict) for test in outcome.tests] == [
        ("reset", "pass"),
        ("count", "fail"),
    ]
    assert outcome.tests[1].cycles == 17
    assert outcome.builds[0].passed is True
    assert sum(a.kind == "live_run_log" for a in outcome.artifacts) == 1
    assert outcome.tests[0].run_log_path == outcome.tests[1].run_log_path


def test_authenticated_cocotb_result_is_the_per_test_authority(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)

    def invoke(command: list[str], *, timeout: int, **kwargs: Any) -> SubprocessResult:
        assert command[:2] == ["sh", "-c"]
        assert timeout == 5
        _write_transport(
            handle,
            prepared,
            ("reset", "count"),
            AdapterResult(
                passed=False,
                inconclusive=False,
                sva_errors=0,
                tests=("reset", "count"),
                failure_kind="design",
                test_results=(
                    AdapterTestResult("reset", "pass", elapsed_s=0.1),
                    AdapterTestResult("count", "fail", elapsed_s=0.2, detail="assertion"),
                ),
            ),
            adapter="cocotb",
        )
        return SubprocessResult(
            returncode=0,
            stdout=(
                "BOOLEY_BUILD_STAGE token=abc123 rc=0\n"
                "[SIM_RESULT] PASSED\n"
                "[SIM_CYCLES] count 17\n"
            ),
            duration_s=0.3,
        )

    outcome = _run_execution(
        handle,
        prepared,
        invoke,
        ("reset", "count"),
        cocotb=True,
        options=SimulationOptions(timeout_ms=5000, build_timeout_ms=7000),
    )

    _assert_authoritative_cocotb_outcome(outcome)


def test_default_cocotb_selection_runs_discovered_test_names(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_transport(
            handle,
            prepared,
            (),
            AdapterResult(
                True,
                False,
                0,
                ("discovered",),
                test_results=(AdapterTestResult("discovered", "pass"),),
            ),
            adapter="cocotb",
        )
        return SubprocessResult(
            returncode=0,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
        )

    with patch(
        "booley.flows.sim.execution.engine.TraceArtifactPolicy.capture"
    ) as capture_trace_policy:
        outcome = _run_execution(handle, prepared, invoke, None, cocotb=True)

    capture_trace_policy.assert_not_called()
    assert [test.name for test in outcome.tests] == ["discovered"]


def test_timeout_transport_preserves_completed_active_and_not_run_tests(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_partial_timeout_transport(handle, prepared, partial=False)
        return SubprocessResult(
            returncode=-9,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
            timed_out=True,
            duration_s=10.0,
        )

    outcome = _run_execution(handle, prepared, invoke, ("done", "active", "later"), cocotb=True)

    assert [(test.name, test.verdict, test.timed_out) for test in outcome.tests] == [
        ("done", "pass", False),
        ("active", "timeout", True),
        ("later", "timeout", True),
    ]


def test_timeout_without_transport_recovers_cocotb_progress(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)
    output = "\n".join(
        (
            "BOOLEY_BUILD_STAGE token=abc123 rc=0",
            "cocotb.regression running done (1/3)",
            "cocotb.regression done passed",
            "cocotb.regression running active (2/3)",
        )
    )

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_partial_timeout_transport(handle, prepared, modified_ns=1)
        return SubprocessResult(returncode=-9, stdout=output, timed_out=True)

    outcome = _run_execution(handle, prepared, invoke, ("done", "active", "later"), cocotb=True)

    assert [(test.name, test.verdict) for test in outcome.tests] == [
        ("done", "pass"),
        ("active", "timeout"),
        ("later", "timeout"),
    ]


def _check_post_simulation_partial(
    tmp_path: Path,
    timed_out: bool,
    returncode: int,
    sim_passed: bool,
    names: tuple[str, ...] = ("smoke",),
) -> None:
    """An adapter dying mid-conversion leaves the simulation verdict, never a timeout.

    ``names=()`` is the unnamed invocation (a Target with no registered Tests).
    """
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    identity = AdapterTransportIdentity(
        "icarus", "abc123", handle.identity, names, _evidence_path(handle)
    )

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        partial = partial_result_identity(identity)
        publish_native_adapter_result(
            partial,
            "[SIM_RESULT] PASSED\n" if sim_passed else "[SIM_RESULT] FAILED\n",
            0 if sim_passed else 1,
            trace_required=True,
            trace=AdapterTraceResult("incident", detail="trace conversion had not finished"),
        )
        os.utime(partial.result_path, ns=(1, 1))
        return SubprocessResult(
            returncode=returncode,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
            timed_out=timed_out,
        )

    outcome = _run_execution(handle, prepared, invoke, names or None, cocotb=False)

    assert outcome.tests[0].timed_out is False
    # Unnamed invocations have no per-test rows: the adapter-level verdict (a
    # missing trace makes it inconclusive) applies, as on a run that was not killed.
    assert outcome.tests[0].verdict == ("inconclusive" if sim_passed or not names else "fail")
    assert outcome.verdict != "timeout"


def test_post_simulation_partial_survives_adapter_kill(tmp_path: Path) -> None:
    """Sigkill, shell 137 and an adapter exception, for a passing and a failing simulation."""
    for returncode in (-9, 137, 1):
        for sim_passed in (True, False):
            case = tmp_path / f"rc{returncode}-{sim_passed}"
            case.mkdir()
            _check_post_simulation_partial(case, False, returncode, sim_passed)


def test_post_simulation_partial_survives_kill_of_unnamed_invocation(tmp_path: Path) -> None:
    """A Target without registered Tests keeps its verdict on a deadline kill or a signal."""
    for timed_out, returncode in ((True, -9), (False, -9)):
        for sim_passed in (True, False):
            case = tmp_path / f"to{timed_out}-{sim_passed}"
            case.mkdir()
            _check_post_simulation_partial(case, timed_out, returncode, sim_passed, ())


@pytest.mark.parametrize("simulator_returncode", [-15, 0, 7])
def test_guard_abort_reason_uses_simulator_not_adapter_returncode(
    tmp_path: Path, simulator_returncode: int
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_transport(
            handle,
            prepared,
            ("smoke",),
            AdapterResult(
                passed=False,
                inconclusive=False,
                sva_errors=0,
                tests=("smoke",),
                simulator_returncode=simulator_returncode,
                termination="disk_budget",
                failure_kind="infrastructure",
                detail="run directory exceeded its disk budget",
                test_results=(
                    AdapterTestResult(
                        "smoke",
                        "fail",
                        detail="run directory exceeded its disk budget",
                        termination="disk_budget",
                        failure_kind="infrastructure",
                    ),
                ),
            ),
        )
        return SubprocessResult(
            returncode=1,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\nadapter wrapper failed\n",
        )

    outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)

    assert outcome.verdict == "error"
    assert outcome.tests[0].simulator_returncode == simulator_returncode
    if simulator_returncode == 0:
        assert outcome.tests[0].reason == "run directory exceeded its disk budget"
    else:
        assert outcome.tests[0].reason.endswith(f"(rc={simulator_returncode})")


def test_run_log_write_failure_is_typed_infrastructure(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_transport(
            handle,
            prepared,
            ("smoke",),
            AdapterResult(
                True,
                False,
                0,
                ("smoke",),
                test_results=(AdapterTestResult("smoke", "pass"),),
            ),
        )
        return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")

    with patch(
        "booley.flows.sim.execution.engine.write_run_log",
        side_effect=OSError("evidence disk full"),
    ):
        outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)

    assert outcome.verdict == "error"
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "artifact_persistence"
    assert "evidence disk full" in outcome.infrastructure_failure.detail
    assert len(outcome.builds) == 1


def test_adapter_exit_one_with_design_result_is_failure_not_infrastructure(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_transport(
            handle,
            prepared,
            ("smoke",),
            AdapterResult(
                False,
                False,
                0,
                ("smoke",),
                simulator_returncode=1,
                failure_kind="design",
                test_results=(AdapterTestResult("smoke", "fail", detail="DUT mismatch"),),
            ),
        )
        return SubprocessResult(returncode=1, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")

    outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)

    assert outcome.verdict == "fail"
    assert outcome.infrastructure_failure is None
    assert outcome.tests[0].failure_kind == "design"


@pytest.mark.parametrize(
    ("process", "canned_at", "expected_verdict", "expected_kind", "expected_test"),
    [
        pytest.param(
            SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n"),
            "launch",
            "error",
            "adapter_protocol",
            None,
            id="test_normal_completion_requires_terminal_transport",
        ),
        pytest.param(
            SubprocessResult(returncode=2, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n"),
            "launch",
            "error",
            "adapter_protocol",
            None,
            id="adapter-error",
        ),
        pytest.param(
            SubprocessResult(
                returncode=-9,
                stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
                timed_out=True,
            ),
            "launch",
            "fail",
            None,
            "timeout",
            id="test_timeout_precedes_missing_terminal_transport",
        ),
        pytest.param(
            SubprocessResult(
                returncode=1,
                stdout="error: syntax error\nBOOLEY_BUILD_STAGE token=abc123 rc=1\n",
            ),
            "build",
            "fail",
            None,
            "elab_error",
            id="test_design_rejection_does_not_require_adapter_transport",
        ),
    ],
)
def test_missing_transport_distinguishes_adapter_error_from_outer_timeout(
    tmp_path: Path,
    process: SubprocessResult,
    canned_at: str,
    expected_verdict: str,
    expected_kind: str | None,
    expected_test: str | None,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    outcome = _run_execution(
        handle,
        prepared,
        lambda _command, timeout: process,
        ("smoke",),
        cocotb=False,
        canned_at=canned_at,
    )

    assert outcome.verdict == expected_verdict
    if expected_kind is not None:
        assert outcome.infrastructure_failure is not None
        assert outcome.infrastructure_failure.kind == expected_kind
    else:
        assert outcome.infrastructure_failure is None
    if expected_test is not None:
        assert outcome.tests[0].verdict == expected_test
    if expected_verdict == "fail" and expected_test == "timeout":
        assert outcome.tests[0].timed_out is True
        assert outcome.tests[0].termination == "completed"


def test_adapter_pass_cannot_override_nonzero_process_exit(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_transport(handle, prepared, ("smoke",), _passing_result())
        return SubprocessResult(returncode=1, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")

    with pytest.raises(ArtifactValidationError, match="contradicts"):
        _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)


@pytest.mark.parametrize("launch_rc", [1, 137])
def test_nonzero_launch_cannot_hide_adapter_authentication_error(tmp_path, launch_rc) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        foreign = AdapterTransportIdentity(
            "icarus", "abc123", "foreign#sim", ("smoke",), _evidence_path(handle)
        )
        write_adapter_result(foreign, _passing_result())
        return SubprocessResult(
            returncode=launch_rc, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n"
        )

    with pytest.raises(ArtifactValidationError, match="Target"):
        _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)


@pytest.mark.parametrize("passed", [True, False])
def test_cleanup_failure_keeps_authenticated_design_verdict(tmp_path, passed) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    result = AdapterResult(
        passed,
        False,
        0,
        ("smoke",),
        test_results=(AdapterTestResult("smoke", "pass" if passed else "fail"),),
    )
    unlink = Path.unlink

    def denied_partial(path, *args, **kwargs):
        if path.name.endswith(".partial"):
            raise OSError("partial cleanup denied")
        return unlink(path, *args, **kwargs)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_transport(handle, prepared, ("smoke",), result)
        return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")

    with patch.object(Path, "unlink", denied_partial):
        outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)
    assert outcome.tests[0].verdict == ("pass" if passed else "fail")
    assert outcome.infrastructure_failure is None
    assert any("artifact_persistence:" in item for item in outcome.diagnostics)


def test_unchanged_timeout_partial_result_is_stale(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)
    _write_partial_timeout_transport(handle, prepared)

    with pytest.raises(ArtifactValidationError, match="stale"):
        _run_execution(
            handle,
            prepared,
            lambda _command, timeout: SubprocessResult(
                returncode=-9,
                stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
                timed_out=True,
            ),
            ("done", "active", "later"),
            cocotb=True,
        )


def test_adapter_programmer_value_error_propagates(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)

    with (
        patch(
            "booley.flows.sim.execution.engine.prepare_adapter_invocation",
            side_effect=ValueError("adapter defect"),
        ),
        pytest.raises(ValueError, match="adapter defect"),
    ):
        _run_execution(handle, prepared, MagicMock(), ("smoke",), cocotb=False)


def test_prepared_source_entries_accept_absolute_project_source(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    source = tmp_path / "top.sv"
    source.write_bytes(b"module top; endmodule\n")
    group = _prepared_group_with_sources(handle, source)

    assert group.prepared_source_entries() == (
        {
            "path": "top.sv",
            "bytes": len(source.read_bytes()),
            "sha256": "sha256:bbfca2afc8562f8675a4e3f474a685b4f47d7728ae08df9a2f1a2a8bb77826e7",
            "kind": "generated_input",
        },
    )


def test_prepared_source_entries_reject_absolute_source_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    handle = _handle(project)
    source = tmp_path / "outside.sv"
    source.write_bytes(b"module outside; endmodule\n")
    group = _prepared_group_with_sources(handle, source)

    with pytest.raises(SimulationBuildSlotError, match="unsafe prepared Simulation source"):
        group.prepared_source_entries()


@pytest.fixture
def packaged_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A project and a packaged trace dump source the engine accepts as its own."""
    project = tmp_path / "project"
    project.mkdir()
    packaged = tmp_path / "package" / "refs" / "booley_vcd_dump.sv"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"trusted")
    monkeypatch.setattr(
        "booley.flows.sim.execution.engine.trace_overlay.packaged_vcd_dump_source",
        MagicMock(return_value=packaged.resolve()),
    )
    return project, packaged


def test_prepared_source_entries_accept_exact_packaged_trace_source(
    packaged_dump: tuple[Path, Path],
) -> None:
    project, packaged = packaged_dump
    packaged.write_bytes(b"module booley_vcd_dump; endmodule\n")
    group = _prepared_group_with_sources(_handle(project), packaged, trace_requested=True)

    assert group.prepared_source_entries() == (
        {
            "path": "booley-package/refs/booley_vcd_dump.sv",
            "bytes": len(packaged.read_bytes()),
            "sha256": "sha256:ff3f002ecdd71580a204971585b494799950bb71d14dda0439f37e41972c1119",
            "kind": "generated_input",
        },
    )


def test_prepared_source_entries_do_not_require_package_for_project_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "sim" / "booley_vcd_dump.sv"
    source.parent.mkdir()
    source.write_bytes(b"module booley_vcd_dump; endmodule\n")
    monkeypatch.setattr(
        "booley.flows.sim.execution.engine.trace_overlay.packaged_vcd_dump_source",
        MagicMock(
            side_effect=fusesoc_registry.FuseSocError(
                "packaged trace dump module is not a regular file"
            )
        ),
    )
    group = _prepared_group_with_sources(_handle(project), source, trace_requested=True)

    assert group.prepared_source_entries()[0]["path"] == "sim/booley_vcd_dump.sv"


def test_prepared_source_entries_fail_closed_when_packaged_authority_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    missing = tmp_path / "package" / "refs" / "booley_vcd_dump.sv"
    monkeypatch.setattr(
        "booley.flows.sim.execution.engine.trace_overlay.packaged_vcd_dump_source",
        MagicMock(
            side_effect=fusesoc_registry.FuseSocError(
                f"packaged trace dump module is not a regular file: {missing}"
            )
        ),
    )
    group = _prepared_group_with_sources(_handle(project), missing, trace_requested=True)

    with pytest.raises(
        SimulationBuildSlotError,
        match="packaged trace dump module is not a regular file",
    ):
        group.prepared_source_entries()


def test_prepared_source_entries_reject_packaged_sibling_and_external_symlink(
    tmp_path: Path, packaged_dump: tuple[Path, Path]
) -> None:
    project, packaged = packaged_dump
    sibling = packaged.parent / "other.sv"
    sibling.write_bytes(b"untrusted")
    external = tmp_path / "external.sv"
    external.write_bytes(b"external")
    alias = tmp_path / "external-alias.sv"
    alias.symlink_to(external)

    for source in (sibling, alias):
        group = _prepared_group_with_sources(_handle(project), source, trace_requested=True)
        with pytest.raises(SimulationBuildSlotError, match="unsafe prepared Simulation source"):
            group.prepared_source_entries()


def test_prepared_source_entries_treat_packaged_alias_as_same_identity(
    tmp_path: Path, packaged_dump: tuple[Path, Path]
) -> None:
    project, packaged = packaged_dump
    alias = tmp_path / "dump-alias.sv"
    alias.symlink_to(packaged)

    group = _prepared_group_with_sources(_handle(project), alias, trace_requested=True)
    assert group.prepared_source_entries()[0]["path"] == ("booley-package/refs/booley_vcd_dump.sv")


def test_prepared_source_entries_reject_duplicate_mapped_identity(
    tmp_path: Path, packaged_dump: tuple[Path, Path]
) -> None:
    project, packaged = packaged_dump
    alias = tmp_path / "dump-alias.sv"
    alias.symlink_to(packaged)
    group = _prepared_group_with_sources(_handle(project), packaged, alias, trace_requested=True)

    with pytest.raises(SimulationBuildSlotError, match="duplicate prepared Simulation source"):
        group.prepared_source_entries()


def test_declared_staged_runtime_input_is_available_at_run_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    prepared = replace(
        prepared,
        make_argv=("true",),
        resolved=replace(
            prepared.resolved,
            files=(ResolvedFile("dhry.hex", "user"),),
        ),
    )
    fake_bin = _runtime_input_vvp(tmp_path, prepared.build_root)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    outcome = _run_execution(
        handle,
        prepared,
        _subprocess_invoker(handle.project_root),
        ("dhry",),
        cocotb=False,
        # Hosted Windows runners can spend more than five seconds starting the
        # real shell and Python adapter under load. This test exercises staged
        # input visibility, not timeout classification.
        options=SimulationOptions(timeout_ms=hang_guard_s() * 1000),
    )

    assert outcome.passed is True
    assert not (handle.project_root / "dhry.hex").exists()


@contextmanager
def _real_fusesoc() -> Iterator[None]:
    """Resolve targets through the real FuseSoC command, not the PATH shim."""
    real_resolve = fusesoc_registry.resolve_target_handle
    fusesoc_cmd = (
        list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        if shutil.which("fusesoc")
        else [sys.executable, "-c", "from fusesoc.main import main; main()"]
    )
    with patch.object(
        fusesoc_registry,
        "resolve_target_handle",
        side_effect=lambda *args, **kwargs: real_resolve(
            *args, **{**kwargs, "fusesoc_cmd": fusesoc_cmd}
        ),
    ):
        yield


def _run_real_icarus(
    project: Path, handle: TargetHandle, runs: int = 1
) -> tuple[SimulationTargetOutcome, ...]:
    # Real FuseSoC setup and Icarus startup are substantially slower on
    # Windows runners than the lightweight adapter tests around this one.
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project),
        options=SimulationOptions(timeout_ms=hang_guard_s() * 1000),
    )
    with _real_fusesoc():
        return tuple(run_prepared_group(execution, handle, ("dhry",)) for _ in range(runs))


def _doctor_view_paths(state: Path) -> tuple[Path, ...]:
    return tuple(
        path for path in state.rglob("*") if path.name.startswith(selftest_overlay.BAD_RUN_CWD_DIR)
    )


def _configure_non_root_doctor_runtime(project: Path, state: Path) -> tuple[Path, Path]:
    (state / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "runtime-assets"\n', encoding="utf-8"
    )
    runtime = project / "runtime-assets"
    runtime.mkdir()
    runtime_file = runtime / "firmware.hex"
    runtime_file.write_text("good\n", encoding="utf-8")
    sibling = runtime / "vectors" / "input.hex"
    sibling.parent.mkdir()
    sibling.write_text("unchanged\n", encoding="utf-8")
    return runtime_file, sibling


def _run_sim_cli(project: Path) -> subprocess.CompletedProcess[str]:
    source = Path(__file__).resolve().parents[3] / "src"
    return run_bounded_child(
        [
            sys.executable,
            "-c",
            "from booley.harness.booley import main; raise SystemExit(main())",
            "flow",
            "sim",
            "--target",
            "sim",
            "--test",
            "dhry",
        ],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(source), "BOOLEY_CONTAINER": "1"},
    )


def test_copyto_runtime_input_resolves_through_real_fusesoc_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project = tmp_path / "project"
    state = _write_runtime_input_project(project)
    (state / "tests.toml").write_text('[sim]\ntests = ["dhry"]\n', encoding="utf-8")
    fake_bin = _write_fake_icarus_tools(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    (outcome,) = _run_real_icarus(project, handle)

    edam = next((state / ".runtime").rglob("*.eda.yml"))
    resolved = fusesoc_registry.parse_edam(
        edam,
        target="sim",
        vlnv=handle.vlnv,
    )
    assert [
        (item.name, item.file_type) for item in resolved.files if item.file_type == "user"
    ] == [("firmware.hex", "user")]
    assert outcome.passed is True
    assert not (project / "firmware.hex").exists()


@pytest.mark.parametrize(
    "tool_declaration",
    (
        "    flow: sim\n    flow_options: {tool: icarus}\n",
        "    default_tool: icarus\n    tools:\n      icarus: {}\n",
    ),
    ids=("modern-flow-api", "legacy-tool-api"),
)
def test_real_icarus_declarations_build_authorize_and_launch_as_icarus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_declaration: str,
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project = tmp_path / "project"
    state = _write_runtime_input_project(project, tool_declaration=tool_declaration)
    (state / "tests.toml").write_text('[sim]\ntests = ["dhry"]\n', encoding="utf-8")
    fake_bin = _write_fake_icarus_tools(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    (outcome,) = _run_real_icarus(project, handle)

    build_root = next((state / ".runtime").rglob("*.eda.yml")).parent
    makefile = (build_root / "Makefile").read_text(encoding="utf-8")
    scripts = tuple(build_root.glob("*.scr"))
    assert "iverilog" in makefile
    assert "vvp" in makefile
    assert len(scripts) == 1
    assert scripts[0].with_suffix("").is_file()
    assert not (build_root / "Vtb").exists()
    assert outcome.eda_tool == "icarus"
    assert outcome.passed is True


def test_preparation_rejects_declared_and_configured_simulator_disagreement(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path, selector="sim_disagree", eda_tool="icarus")
    resolved = replace(
        _prepared(handle, cocotb=False).resolved,
        configured_eda_tool="verilator",
    )

    with (
        patch.object(fusesoc_registry, "resolve_target_handle", return_value=resolved),
        patch(
            "booley.flows.sim.build.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        pytest.raises(SimulationBuildPreparationError) as exc,
    ):
        prepare_simulation_build(handle)

    message = str(exc.value)
    assert "sim_disagree" in message
    assert "icarus" in message
    assert "verilator" in message
    assert "flow_options.tool" in message
    assert "default_tool" in message


def test_real_legacy_verilator_target_prepares_as_verilator(tmp_path: Path) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project = tmp_path / "project"
    _write_runtime_input_project(
        project,
        tool_declaration=("    default_tool: verilator\n    tools:\n      verilator: {}\n"),
    )
    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    with _real_fusesoc():
        prepared = prepare_simulation_build(handle)

    assert prepared.resolved.eda_tool is None
    assert prepared.resolved.configured_eda_tool == "verilator"
    assert prepared.eda_tool == "verilator"
    assert "verilator" in (prepared.build_root / "Makefile").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "tool_declaration",
    (
        "    flow: sim\n    flow_options: {tool: verilator}\n",
        "    default_tool: verilator\n    tools:\n      verilator: {}\n",
    ),
    ids=("modern-flow-api", "legacy-tool-api"),
)
def test_default_verilator_make_recipe_is_resource_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_declaration: str,
) -> None:
    """The nested Verilator C++ compilation receives Booley's job budget."""
    importlib.import_module("fusesoc")
    importlib.import_module("edalize")
    project = tmp_path / "project"
    _write_runtime_input_project(project, tool_declaration=tool_declaration)
    # The Job's share of the cores, fixed at its admission.
    monkeypatch.setenv(JOB_THREADS_ENV, "4")
    handle = TargetCatalog.build(project).select("sim", for_flow="sim")

    with _real_fusesoc():
        prepared = prepare_simulation_build(handle)

    makefile = (prepared.build_root / "Makefile").read_text(encoding="utf-8")
    with _real_fusesoc():
        repeated = prepare_simulation_build(handle)
    assert (repeated.build_root / "Makefile").read_text(encoding="utf-8") == makefile
    if "$(MAKE_OPTIONS)" in makefile:
        config = (prepared.build_root / "config.mk").read_text(encoding="utf-8")
        assert "$(MAKE) $(MAKE_OPTIONS) -f" in makefile
        assert "MAKE_OPTIONS      := -j4 VM_PARALLEL_BUILDS=1" in config
    else:
        assert re.search(r"make -f Vtb\.mk -j4 VM_PARALLEL_BUILDS=1", makefile)


def test_cgroup_probe_failure_is_a_typed_setup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    _write_runtime_input_project(
        project,
        tool_declaration="    flow: sim\n    flow_options: {tool: verilator}\n",
    )
    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    # Private seam: the cgroup quota is host state read through `build_parallelism._read_text`
    # (imported from `cpu_budget`); no public input makes that read fail.
    monkeypatch.setattr(
        build_parallelism,
        "_read_text",
        lambda _path: (_ for _ in ()).throw(PermissionError("cgroup denied")),
    )

    with pytest.raises(SimulationBuildPreparationError, match="cgroup denied"):
        prepare_simulation_build(handle)


@pytest.mark.parametrize("api", ("modern", "legacy"))
@pytest.mark.parametrize("authored_jobs", (False, True), ids=("non-jobs", "jobs"))
def test_real_verilator_setup_merges_authored_make_options_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: str,
    authored_jobs: bool,
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    options = "[-j2, OPT_FAST=-O3]" if authored_jobs else "[OPT_FAST=-O3]"
    if api == "modern":
        declaration = (
            "    flow: sim\n"
            "    flow_options:\n"
            "      tool: verilator\n"
            f"      make_options: {options}\n"
        )
    else:
        declaration = (
            "    default_tool: verilator\n"
            "    tools:\n"
            "      verilator:\n"
            f"        make_options: {options}\n"
        )
    project = tmp_path / "project"
    _write_runtime_input_project(project, tool_declaration=declaration)
    # The Job's share of the cores, fixed at its admission.
    monkeypatch.setenv(JOB_THREADS_ENV, "4")
    handle = TargetCatalog.build(project).select("sim", for_flow="sim")

    with _real_fusesoc():
        prepared = prepare_simulation_build(handle)

    makefile = (prepared.build_root / "Makefile").read_text(encoding="utf-8")
    recipe = (
        (prepared.build_root / "config.mk").read_text(encoding="utf-8")
        if "$(MAKE_OPTIONS)" in makefile
        else makefile
    )
    assert recipe.count("OPT_FAST=-O3") == 1
    if authored_jobs:
        assert recipe.count("-j2") == 1
        assert "VM_PARALLEL_BUILDS=1" not in recipe
        assert "-j4" not in recipe
    else:
        assert re.search(r"OPT_FAST=-O3.*-j4.*VM_PARALLEL_BUILDS=1", recipe)


@pytest.mark.parametrize("configured_tool", (None, "mystery-sim"))
def test_preparation_rejects_missing_or_unknown_configured_simulator(
    tmp_path: Path,
    configured_tool: str | None,
) -> None:
    handle = _handle(tmp_path, selector="sim_unresolved", eda_tool="icarus")
    resolved = replace(
        _prepared(handle, cocotb=False).resolved,
        configured_eda_tool=configured_tool,
    )

    with (
        patch.object(fusesoc_registry, "resolve_target_handle", return_value=resolved),
        patch(
            "booley.flows.sim.build.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        pytest.raises(SimulationBuildPreparationError, match="sim_unresolved") as exc,
    ):
        prepare_simulation_build(handle)

    assert "flow_options.tool" in str(exc.value)
    assert "default_tool" in str(exc.value)


@pytest.mark.skipif(sys.platform == "win32", reason="Doctor runtime shadow requires symlinks")
def test_projected_core_bad_overlay_reaches_design_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project = tmp_path / "project"
    state = _write_runtime_input_project(project)
    runtime_file, sibling = _configure_non_root_doctor_runtime(project, state)
    (state / "tests.toml").write_text('[sim]\ntests = ["dhry"]\n', encoding="utf-8")
    (project / ".booley-projected-demo.core").write_text(
        "CAPI=2:\nname: acme:lib:projected:1\n", encoding="utf-8"
    )
    overlay = selftest_overlay.bad_overlay_dir(state, "sim") / "firmware.hex"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("broken\n", encoding="utf-8")
    fake_bin = _write_fake_icarus_tools(tmp_path)
    (fake_bin / "vvp").write_text(
        "#!/bin/sh\n"
        "if grep -qx broken firmware.hex; then\n"
        "  echo '[SIM_RESULT] FAILED'\n"
        "  exit 1\n"
        "fi\n"
        "echo '[SIM_RESULT] PASSED'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    monkeypatch.setenv("BOOLEY_DIR", str(state))
    reset_cache()

    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    previous_booley_dir = os.environ.get("BOOLEY_DIR")
    try:
        for outcome in _run_real_icarus(project, handle, runs=2):
            assert outcome.verdict == "fail"
            assert outcome.infrastructure_failure is None
            assert any(test.verdict == "fail" for test in outcome.tests), outcome.tests
            assert runtime_file.read_text(encoding="utf-8") == "good\n"
            assert sibling.read_text(encoding="utf-8") == "unchanged\n"
            assert not _doctor_view_paths(state)
        result = _run_sim_cli(project)
        assert result.returncode == 1, result.stdout + result.stderr
        assert not _doctor_view_paths(state)
    finally:
        if previous_booley_dir is None:
            monkeypatch.delenv("BOOLEY_DIR", raising=False)
        else:
            monkeypatch.setenv("BOOLEY_DIR", previous_booley_dir)
        reset_cache()


@dataclass
class _StaleIcarus:
    """The stale-compiler Project with every command and Icarus launch recorded."""

    project: Path
    source: Path
    commands: list[list[str]] = field(default_factory=lambda: [])
    launched: list[str] = field(default_factory=lambda: [])

    def select(self, name: str = "sim_a") -> TargetHandle:
        return TargetCatalog.build(self.project).select(name, for_flow="sim")

    def execution(self, **options: Any) -> SimulationExecution:
        options.setdefault("timeout_ms", hang_guard_s() * 1000)
        base_invoke = _subprocess_invoker(self.project)

        def invoke(command: list[str], *, timeout: int, **kwargs: Any) -> SubprocessResult:
            self.commands.append(command)
            result = base_invoke(command, timeout=timeout, **kwargs)
            if "booley.flows.sim.backends.icarus" in command[-1]:
                self.launched.append(result.stdout)
            return result

        return SimulationExecution(invoke=invoke, options=SimulationOptions(**options))

    def builds(self) -> int:
        return sum("BOOLEY_BUILD_STAGE" in command[-1] for command in self.commands)


@pytest.fixture
def stale_icarus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _StaleIcarus:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, source = _write_stale_compiler_fixture(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    return _StaleIcarus(project, source)


@pytest.fixture
def reusable_icarus(stale_icarus: _StaleIcarus, monkeypatch: pytest.MonkeyPatch) -> _StaleIcarus:
    # Private seam: the reuse closure hashes the installed Icarus tree (`<bin>/../lib/ivl`
    # and ELF dependencies); the fake tools have none and no public input supplies one.
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    return stale_icarus


def test_two_targets_never_launch_stale_image_after_equal_mtime_edit(
    stale_icarus: _StaleIcarus,
) -> None:
    handles = tuple(stale_icarus.select(name) for name in ("sim_a", "sim_b"))
    execution = stale_icarus.execution()
    for handle in handles:
        assert run_prepared_group(execution, handle, ("smoke",)).passed
    observed = stale_icarus.launched
    assert len(observed) == 2 and all("OLD" in item for item in observed)

    _edit_keeping_mtime(stale_icarus.source, "module tb; // NEW\nendmodule\n")
    observed.clear()
    for handle in handles:
        assert run_prepared_group(execution, handle, ("smoke",)).passed
    assert len(observed) == 2 and all("NEW" in item and "OLD" not in item for item in observed)


@pytest.mark.skipif(os.name == "nt", reason="Icarus reuse closure runs in the Linux Sandbox")
def test_matching_closed_icarus_inputs_reuse_verified_image(
    reusable_icarus: _StaleIcarus,
) -> None:
    handle = reusable_icarus.select()
    execution = reusable_icarus.execution()
    first = run_prepared_group(execution, handle, ("first",))
    second = run_prepared_group(execution, handle, ("second",))
    assert first.passed, first.infrastructure_failure
    assert second.passed, (second.builds, second.tests, second.infrastructure_failure)
    assert reusable_icarus.builds() == 1
    assert second.builds[0].ran is False
    assert second.builds[0].cache_decision.startswith("hit;")


@pytest.mark.skipif(os.name == "nt", reason="Icarus reuse closure runs in the Linux Sandbox")
def test_isolated_icarus_identical_build_reuses_and_source_edit_rebuilds(
    reusable_icarus: _StaleIcarus,
) -> None:
    """In an ``ignore_native_cores`` Project an identical build reuses; an edited source rebuilds."""
    _isolate_native_cores(reusable_icarus.project, core="multi.core")
    handle = reusable_icarus.select()
    execution = reusable_icarus.execution()
    first = run_prepared_group(execution, handle, ("first",))
    second = run_prepared_group(execution, handle, ("second",))
    assert first.passed, first.infrastructure_failure
    assert second.passed, (second.builds, second.tests, second.infrastructure_failure)
    assert first.builds[0].ran is True
    assert second.builds[0].ran is False
    assert second.builds[0].cache_decision.startswith("hit;")
    assert reusable_icarus.builds() == 1

    # The fake compiler writes the source's marker into the image, so NEW in the
    # launched output proves the stale image never ran.
    _edit_keeping_mtime(reusable_icarus.source, "module tb; // NEW\nendmodule\n")
    third = run_prepared_group(execution, handle, ("third",))
    assert third.passed, (third.builds, third.tests, third.infrastructure_failure)
    assert third.builds[0].ran is True
    assert third.builds[0].cache_decision.startswith("changed source, recipe, or tool;")
    assert reusable_icarus.builds() == 2
    assert "NEW" in reusable_icarus.launched[-1] and "OLD" not in reusable_icarus.launched[-1]


def test_changed_input_with_failed_rebuild_never_launches_prior_image(
    reusable_icarus: _StaleIcarus,
) -> None:
    handle = reusable_icarus.select()
    execution = reusable_icarus.execution()
    assert run_prepared_group(execution, handle, ("smoke",)).passed
    _edit_keeping_mtime(reusable_icarus.source, "module tb; // BAD\nendmodule\n")
    outcome = run_prepared_group(execution, handle, ("smoke",))
    assert not outcome.passed
    assert len(reusable_icarus.launched) == 1


@pytest.mark.skipif(os.name == "nt", reason="Icarus reuse closure runs in the Linux Sandbox")
@pytest.mark.parametrize("tamper", ["image", "pointer"])
def test_corrupt_cache_forces_fresh_build(reusable_icarus: _StaleIcarus, tamper: str) -> None:
    handle = reusable_icarus.select()
    execution = reusable_icarus.execution()
    assert run_prepared_group(execution, handle, ("smoke",)).passed
    slot = simulation_build_slot(handle)
    pointer = json.loads((slot / "current.json").read_text(encoding="utf-8"))
    if tamper == "image":
        build_root = slot / "g" / pointer["generation"] / pointer["build_root"]
        image = next(build_root.glob("*.scr")).with_suffix("")
        image.write_text("[SIM_RESULT] PASSED\nSTALE\n", encoding="utf-8")
    else:
        pointer["build_root"] = "../outside"
        (slot / "current.json").write_text(json.dumps(pointer), encoding="utf-8")
    assert run_prepared_group(execution, handle, ("smoke",)).passed
    assert reusable_icarus.builds() == 2


@pytest.mark.skipif(os.name == "nt", reason="Icarus reuse closure runs in the Linux Sandbox")
@pytest.mark.parametrize("record", ["pointer", "manifest"])
def test_corrupt_retained_record_is_missing_provenance_and_rebuilds(
    reusable_icarus: _StaleIcarus, record: str
) -> None:
    handle = reusable_icarus.select()
    execution = reusable_icarus.execution()
    assert run_prepared_group(execution, handle, ("smoke",)).passed
    slot = simulation_build_slot(handle)
    pointer_path = slot / "current.json"
    if record == "pointer":
        corrupt = pointer_path
    else:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        corrupt = (
            slot
            / "g"
            / pointer["generation"]
            / pointer["build_root"]
            / (".booley-build-manifest.json")
        )
    corrupt.write_text("{not json", encoding="utf-8")
    second = run_prepared_group(execution, handle, ("smoke",))
    assert second.passed
    assert second.builds[0].ran is True
    assert second.builds[0].cache_decision.startswith("missing provenance;")
    assert reusable_icarus.builds() == 2


def test_missing_new_image_refuses_the_build_and_never_launches(
    stale_icarus: _StaleIcarus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_DIR", str(stale_icarus.project / ".booley"))
    handle = stale_icarus.select()
    launched = False
    flow = measured_flow(stale_icarus.project)
    base_invoke = _subprocess_invoker(stale_icarus.project)

    def invoke(command: list[str], *, timeout: int, **kwargs: Any) -> SubprocessResult:
        nonlocal launched
        result = base_invoke(command, timeout=timeout, **kwargs)
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            for script in (stale_icarus.project / ".booley" / ".runtime").rglob("*.scr"):
                script.with_suffix("").unlink(missing_ok=True)
        if "booley.flows.sim.backends.icarus" in command[-1]:
            launched = True
        return flow._execute_boundary(canned_command(result), timeout=timeout)

    execution = SimulationExecution(
        invoke=invoke, options=SimulationOptions(timeout_ms=hang_guard_s() * 1000)
    )
    with pytest.raises(SimulationBuildSlotError, match="cannot record Simulation image"):
        run_prepared_group(execution, handle, ("smoke",))
    assert launched is False
    assert _run_records(stale_icarus.project, "sim_build") == [("exit_nonzero", 0)]


def _run_records(project: Path, phase: str) -> list[tuple[str, int]]:
    """``(outcome, returncode)`` of every Resource History record of *phase*."""
    records = resource_history.load_records(project / ".booley")
    return [(r["outcome"], r["returncode"]) for r in records if r["key"]["phase"] == phase]


def _native_launch(
    handle: TargetHandle, output: str, simulator_rc: int, termination: RunTermination | None = None
) -> Callable[..., SubprocessResult]:
    """A native adapter launch that exits as the production adapter does."""

    def launch(_command: list[str], *, timeout: float) -> SubprocessResult:
        del timeout
        identity = AdapterTransportIdentity(
            "icarus", "abc123", handle.identity, ("smoke",), _evidence_path(handle)
        )
        run = publish_native_adapter_result(
            identity, output, simulator_rc, termination=termination
        )
        return SubprocessResult(returncode=run.exit_code, stdout="")

    return launch


def _cocotb_batch_abort(handle: TargetHandle, prepared: PreparedSimulationBuild):
    """A cocotb batch whose tests all passed but whose final disk accounting aborted it."""

    def launch(_command: list[str], *, timeout: float) -> SubprocessResult:
        del timeout
        result = AdapterResult(
            False,
            False,
            0,
            ("smoke",),
            termination="disk_budget",
            failure_kind="infrastructure",
            detail="run directory exceeded its disk budget",
            test_results=(AdapterTestResult("smoke", "pass"),),
        )
        _write_transport(handle, prepared, ("smoke",), result, adapter="cocotb")
        return SubprocessResult(returncode=1, stdout="")

    return launch


_DISK_ABORT = RunTermination(
    "disk_budget", "run directory exceeded its disk budget", "infrastructure"
)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        pytest.param("pass", ("exit_zero", 0), id="pass"),
        pytest.param("simulator-exit", ("exit_nonzero", 0), id="simulator-exit"),
        pytest.param("sva-error", ("exit_nonzero", 0), id="sva-error"),
        pytest.param("aborted", ("exit_nonzero", 0), id="aborted"),
        pytest.param("missing-waveform", ("exit_nonzero", 0), id="missing-waveform"),
        pytest.param("cocotb-batch-abort", ("exit_zero", 1), id="cocotb-batch-abort"),
    ],
)
def test_sim_run_record_outcome_follows_the_test_run_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: tuple[str, int]
) -> None:
    """The recorded outcome is the Flow's verdict on the Test Run; the raw exit code stays (#4964)."""
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=case == "cocotb-batch-abort")
    passed = "[SIM_RESULT] PASSED\n"
    launches: dict[str, Callable[..., SubprocessResult]] = {
        "pass": _native_launch(handle, passed, 0),
        "simulator-exit": _native_launch(handle, passed, 3),
        "sva-error": _native_launch(handle, passed + "[5] Error: assertion failed\n", 0),
        "aborted": _native_launch(handle, passed, 0, _DISK_ABORT),
        "cocotb-batch-abort": _cocotb_batch_abort(handle, prepared),
    }
    options = SimulationOptions()
    trace_mode = None
    if case == "missing-waveform":
        (tmp_path / "run").mkdir()
        (tmp_path / ".booley").mkdir()
        (tmp_path / ".booley" / "booley.toml").write_text(
            '[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8"
        )
        trace = tmp_path / "run" / "wave.fst"
        trace.write_bytes(b"old")
        launches[case] = _passing_trace_invoker(handle, prepared, trace, fresh=False)
        options = SimulationOptions(trace=True)
        trace_mode = TraceMode.NATIVE_FST
    flow = measured_flow(tmp_path)

    def invoke(command: list[str], *, timeout: float) -> SubprocessResult:
        result = launches[case](command, timeout=timeout)
        return flow._execute_boundary(canned_command(result), timeout=timeout)

    outcome = _run_execution(
        handle,
        prepared,
        invoke,
        ("smoke",),
        cocotb=case == "cocotb-batch-abort",
        options=options,
        trace_mode=trace_mode,
    )

    assert all(test.passed for test in outcome.tests) == (case in ("pass", "cocotb-batch-abort"))
    assert _run_records(tmp_path, "sim_run") == [expected]


def _write_fake_trace_tools(root: Path) -> None:
    compiler = root / "bin" / "iverilog"
    compiler.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "output.write_text('[SIM_RESULT] PASSED\\n')\n",
        encoding="utf-8",
    )
    runner_source = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "image = next(pathlib.Path(arg) for arg in sys.argv[1:] "
        "if pathlib.Path(arg).is_file())\n"
        "print(image.read_text(), end='')\n"
        "pathlib.Path('dump.vcd').write_text(\n"
        "    '$date\\nnow\\n$end\\n$timescale 1ns $end\\n'\n"
        "    '$scope module tb $end\\n$var wire 1 ! signal $end\\n'\n"
        "    '$upscope $end\\n$enddefinitions $end\\n#0\\n0!\\n#1\\n1!\\n'\n"
        ")\n"
    )
    if os.name == "nt":
        (root / "bin" / "trace_vvp.py").write_text(runner_source, encoding="utf-8")
        (root / "bin" / "vvp.bat").write_text(
            '@echo off\npython "%~dp0trace_vvp.py" %*\n', encoding="utf-8"
        )
    else:
        runner = root / "bin" / "vvp"
        runner.write_text(runner_source, encoding="utf-8")
        runner.chmod(0o755)


def _assert_queryable_trace(outcome: SimulationTargetOutcome, cache_root: Path) -> None:
    traces = [artifact for artifact in outcome.artifacts if artifact.kind == "trace"]
    assert len(traces) == 1 and Path(traces[0].path).is_file()
    trace_path = Path(traces[0].path)
    assert trace_path.suffix in {".fst", ".vcd"}
    with patch(
        "booley.flows.sim.trace_session.waveform_cache_dir",
        side_effect=lambda work_dir: cache_root / work_dir.name,
    ):
        session = TraceSession(trace_path.parent)
        if trace_path.suffix == ".vcd":
            session.postprocess(trace_path)
            trace_path = session.work_bwave_path
        inspection = session.inspect(trace_path)
    assert inspection.usable
    assert inspection.artifact is not None
    assert inspection.artifact.signal_count == 1
    assert inspection.artifact.total_ticks == 1


def _write_failing_trace_converter(root: Path) -> Path:
    converter = root / ("bwave-failed.bat" if os.name == "nt" else "bwave-failed")
    converter.write_text(
        "@echo off\necho fixture conversion unavailable 1>&2\nexit /b 1\n"
        if os.name == "nt"
        else "#!/bin/sh\necho fixture conversion unavailable >&2\nexit 1\n",
        encoding="utf-8",
    )
    converter.chmod(0o755)
    return converter


def _assert_execution_trace(
    outcome: SimulationTargetOutcome, cache_root: Path, *, queryable: bool
) -> None:
    if queryable:
        assert all(
            Path(artifact.path).suffix == ".fst"
            for artifact in outcome.artifacts
            if artifact.kind == "trace"
        )
        _assert_queryable_trace(outcome, cache_root)
    else:
        # A VCD that failed to convert is a trace incident, never a waveform.
        assert not any(artifact.kind == "trace" for artifact in outcome.artifacts)
        (run_log,) = (Path(a.path) for a in outcome.artifacts if a.kind == "live_run_log")
        work = run_log.parent  # the simulation work directory
        assert (work / "trace.vcd").read_text(encoding="utf-8") == (
            "$date\nnow\n$end\n$timescale 1ns $end\n"
            "$scope module tb $end\n$var wire 1 ! signal $end\n"
            "$upscope $end\n$enddefinitions $end\n#0\n0!\n#1\n1!\n"
        )
        assert "fixture conversion unavailable" in (work / "trace.fst.stderr").read_text(
            encoding="utf-8"
        )
        manifest = json.loads((work / "trace_status.json").read_text(encoding="utf-8"))
        assert any(
            attempt["kind"] == "vcd_postprocess" and attempt["status"] == "vcd_only"
            for attempt in manifest["attempts"]
        )


@pytest.mark.parametrize(
    "queryable",
    [False, pytest.param(True, marks=pytest.mark.native_bwave)],
    ids=["execution", "queryable"],
)
def test_icarus_trace_reaches_execution_on_first_and_repeat_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale_icarus: _StaleIcarus,
    queryable: bool,
) -> None:
    native_bwave = runtime_paths.native_bwave_binary()
    if queryable and native_bwave is None:
        pytest.skip("native B-Wave binary is required to verify a queryable Trace Artifact")
    _isolate_native_cores(stale_icarus.project, core="multi.core")
    _write_fake_trace_tools(tmp_path)
    if not queryable:
        monkeypatch.setenv("BOOLEY_BWAVE_BIN", str(_write_failing_trace_converter(tmp_path)))
    handle = stale_icarus.select()
    execution = stale_icarus.execution(trace=True, timeout_ms=None)
    for _ in range(2):
        outcome = run_prepared_group(execution, handle, ("smoke",))
        # An unconvertible VCD leaves the Test Run inconclusive, not passed.
        assert outcome.passed is queryable
        assert outcome.verdict == ("pass" if queryable else "inconclusive")
        _assert_execution_trace(outcome, tmp_path / "bwave-cache", queryable=queryable)
        assert stale_icarus.builds() >= 1
        assert stale_icarus.launched
        stale_icarus.commands.clear()
        stale_icarus.launched.clear()


def _non_setup_commands(stale_icarus: _StaleIcarus) -> list[list[str]]:
    # Only FuseSoC setup ran: no build or launch.
    return [command for command in stale_icarus.commands if command[0] != "fusesoc"]


def test_hook_editing_project_source_after_setup_cannot_launch_staged_old_copy(
    stale_icarus: _StaleIcarus,
) -> None:
    handle = stale_icarus.select()
    with stale_icarus.execution().ordinary_group(handle, ("smoke",)) as group:
        # What a Pre-Sim Command editing the Project source would do after setup.
        source = stale_icarus.source
        source.write_text(
            source.read_text(encoding="utf-8").replace("OLD", "NEW"), encoding="utf-8"
        )
        with pytest.raises(
            SimulationBuildSlotError, match="changed during setup or Pre-Sim Commands"
        ):
            group.compile()
    assert _non_setup_commands(stale_icarus) == []


def test_hook_editing_staged_source_cannot_launch_image(stale_icarus: _StaleIcarus) -> None:
    handle = stale_icarus.select()
    with stale_icarus.execution().ordinary_group(handle, ("smoke",)) as group:
        # What a Pre-Sim Command editing the staged copy would do.
        staged = group.build_root / "src" / "acme_lib_multi_1" / "tb.sv"
        with staged.open("a", encoding="utf-8") as stream:
            stream.write("MUTATED")
        with pytest.raises(
            SimulationBuildSlotError, match="build input changed during Pre-Sim Commands"
        ):
            group.compile()


def test_hook_editing_authored_core_cannot_launch_image(stale_icarus: _StaleIcarus) -> None:
    handle = stale_icarus.select()
    with stale_icarus.execution().ordinary_group(handle, ("smoke",)) as group:
        # What a Pre-Sim Command editing the authored core would do.
        with (stale_icarus.project / "multi.core").open("a", encoding="utf-8") as stream:
            stream.write("\n# mutation\n")
        with pytest.raises(
            SimulationBuildSlotError,
            match="compile inputs changed during setup or Pre-Sim Commands",
        ):
            group.compile()
    assert _non_setup_commands(stale_icarus) == []


def test_hook_editing_generated_isolated_core_cannot_launch_image(
    stale_icarus: _StaleIcarus,
) -> None:
    _isolate_native_cores(stale_icarus.project, core="multi.core")
    handle = stale_icarus.select()
    with stale_icarus.execution().ordinary_group(handle, ("smoke",)) as group:
        # A real Pre-Sim Command, run as a record's legacy hook runs, edits the generated core.
        evidence = run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=group.build_root,
            eda_tool="icarus",
            timeout_s=hang_guard_s(),
            resource_flags=(),
            commands=(
                "printf '\\n# mutation\\n' >> "
                ".booley/tmp/fusesoc-isolated-cores/booley-isolated-multi.core",
            ),
            expose_build_root=True,
        )
        assert evidence is not None and evidence.status == "passed"
        with pytest.raises(
            SimulationBuildSlotError, match="compile inputs changed during Pre-Sim Commands"
        ):
            group.compile()
    assert _non_setup_commands(stale_icarus) == []


class _StopBeforeBuildError(Exception):
    """Raised by the patched build session to end a group right after its variant is chosen."""


def _assert_live_and_preview_build_variant(
    tmp_path: Path,
    *,
    trace: bool,
    expected: str,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=trace)
    work_root = tmp_path / "work-root"
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions(trace=trace))
    trace_overlay = SimpleNamespace(
        vlnv="acme:lib:demo_trace:1",
        mode=TraceMode.NATIVE_FST,
        cleanup=MagicMock(),
    )
    session_class = MagicMock()
    session_class.return_value.__exit__.return_value = False
    session_class.return_value.__enter__.return_value.new_generation.return_value = work_root
    with (
        patch(
            "booley.flows.sim.execution.engine.preview_generation_root",
            return_value=work_root,
        ) as preview_root,
        patch(
            "booley.flows.sim.execution.engine.prepare_simulation_build",
            return_value=prepared,
        ) as prepare,
        patch(
            "booley.flows.sim.execution.engine.fusesoc_registry.setup_command_for_handle",
            return_value=["setup"],
        ) as setup,
        patch(
            "booley.flows.sim.execution.engine.trace_overlay.write_trace_overlay",
            return_value=trace_overlay,
        ) as write_overlay,
        patch(
            "booley.flows.sim.execution.engine.trace_overlay.validate_cocotb_trace_mode"
        ) as validate_trace,
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch("booley.flows.sim.execution.engine.SimulationBuildSession", session_class),
        patch(
            "booley.flows.sim.execution.engine.prepare_simulation_work",
            side_effect=_StopBeforeBuildError,
        ),
        _compile_surface_patch(handle),
    ):
        execution.preview(handle, NamedTests(("smoke",)))
        with pytest.raises(_StopBeforeBuildError), execution.ordinary_group(handle, ("smoke",)):
            pass

    preview_root.assert_called_once_with(handle, expected)
    session_class.assert_called_once_with(handle, expected, wait_s=3600)
    assert prepare.call_args.kwargs["variant"] == expected
    assert prepare.call_args.kwargs["build_root"] == work_root
    setup.assert_called_once_with(handle, build_root=work_root)
    assert write_overlay.call_count == int(trace)
    if trace:
        validate_trace.assert_called_once_with(handle.selector, trace_overlay.mode)
    else:
        validate_trace.assert_not_called()
    assert trace_overlay.cleanup.call_count == int(trace)


@pytest.mark.parametrize(
    "trace, doctor_kind, expected",
    [
        (False, None, ""),
        (True, None, "trace"),
        (False, selftest_overlay.BAD_KIND, "doctor-selftest-bad"),
        (True, selftest_overlay.BAD_KIND, "trace-doctor-selftest-bad"),
    ],
)
def test_live_and_preview_use_the_same_build_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trace: bool,
    doctor_kind: str | None,
    expected: str,
) -> None:
    if doctor_kind is None:
        monkeypatch.delenv(selftest_overlay.INTERNAL_KIND_ENV, raising=False)
    else:
        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, doctor_kind)
    _assert_live_and_preview_build_variant(tmp_path, trace=trace, expected=expected)


def test_legacy_selector_root_is_preserved_but_not_used(stale_icarus: _StaleIcarus) -> None:
    project = stale_icarus.project
    handle = stale_icarus.select()
    legacy = work_root_for(project, "sim", handle.selector)
    legacy.mkdir(parents=True)
    sentinel = legacy / "old-image"
    sentinel.write_text("stale\n", encoding="utf-8")

    assert run_prepared_group(stale_icarus.execution(), handle, ("smoke",)).passed
    assert sentinel.read_text(encoding="utf-8") == "stale\n"
    assert tuple((simulation_build_slot(handle) / "g").iterdir())


def _preview(handle: TargetHandle, selection: Any, inspection: Any = None) -> Any:
    """Preview *selection* of *handle* through the public boundary."""
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    with (
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=inspection or _inspection(cocotb=False),
        ),
        patch(
            "booley.flows.sim.execution.engine.fusesoc_registry.setup_command_for_handle",
            return_value=["setup"],
        ),
    ):
        return execution.preview(handle, selection)


def test_doctor_bad_preview_uses_placeholder_without_filesystem_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    preview = _preview(_handle(tmp_path), NamedTests(("smoke",)))

    assert launched_work(preview.commands[0]).run_cwd == "<attempt>"
    assert not tuple(tmp_path.rglob(".booley-doctor-run-cwd-*"))


def test_preview_resolves_configuration_from_each_handle_root(tmp_path: Path) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for root, name, selector in (
        (current, "now", "+test={name}"),
        (baseline, "then", "+case={index}"),
    ):
        project = root / ".booley"
        project.mkdir(parents=True)
        (project / "tests.toml").write_text(
            f'[sim]\ntests = ["{name}"]\nselect = "{selector}"\n',
            encoding="utf-8",
        )

    current_preview = _preview(_handle(current), NamedTests(("now",)))
    baseline_preview = _preview(_handle(baseline), NamedTests(("then",)))

    assert "test=now" in launched_work(current_preview.commands[0]).plusargs
    assert "case=0" in launched_work(baseline_preview.commands[0]).plusargs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh -n syntax check")
def test_preview_is_a_runnable_chain(tmp_path: Path) -> None:
    for adapter, run_name in (
        ("verilator", "smoke@1"),
        ("icarus", "smoke"),
    ):  # only Verilator seeds
        root = tmp_path / adapter
        (root / ".booley").mkdir(parents=True)
        (root / ".booley/tests.toml").write_text('[sim]\ntests = ["smoke"]\n', encoding="utf-8")
        inspection = _inspection(cocotb=False)
        inspection.eda_tool = adapter
        preview = _preview(_handle(root), NamedTests(("smoke",)), inspection)

        shell, flag, script = preview.commands[0]
        assert (shell, flag) == ("sh", "-c")
        subprocess.run(["sh", "-n", "-c", script], check=True)
        work = launched_work(script)
        assert (work.adapter, work.eda_tool, work.top) == (adapter, adapter, "tb_demo")
        assert work.tests == ("smoke",)
        assert [run.name for run in work.test_runs] == [run_name]


@pytest.mark.parametrize(
    ("config", "trace_rel", "preexisting", "invoker", "verdict", "captured", "reason"),
    [
        pytest.param("", "run/wave.fst", b"old", {}, "pass", True, None, id="fresh-replaces-old"),
        pytest.param(
            "", "run/wave.fst", b"old", {"fresh": False}, "inconclusive", False, None, id="stale"
        ),
        pytest.param(
            "", "undeclared/wave.fst", None, {}, "inconclusive", None, None, id="undeclared-path"
        ),
        pytest.param(
            'trace_files = ["../declared/*.fst"]\n',
            "declared/wave.fst",
            None,
            {},
            "pass",
            None,
            None,
            id="declared-glob",
        ),
        pytest.param(
            'trace_files = ["../declared/*.fst"]\n',
            "declared/wave.fst",
            None,
            {"modified_ns": 1},
            "pass",
            True,
            None,
            id="declared-freshness-uses-pre-dispatch-identity",
        ),
        pytest.param(
            "", "run/wave.fst", None, {"modified_ns": 1}, "pass", None, None, id="clock-skew"
        ),
        pytest.param(
            'trace_files = ["../declared/*.fst"]\n',
            "declared/wave.fst",
            b"stale waveform",
            {"fresh": False, "transport_modified_ns": 1},
            "inconclusive",
            False,
            None,
            id="unchanged-declared-is-stale",
        ),
        pytest.param(
            "",
            "run/forged.fst",
            None,
            {"report_trace": False},
            "inconclusive",
            False,
            None,
            id="stdout-cannot-forge-trace",
        ),
        pytest.param(
            None,
            "run/wave.fst",
            None,
            {"fresh": False},
            "inconclusive",
            None,
            "[flows.sim].trace_files",
            id="refused-route-names-trace-files",  # #884
        ),
    ],
)
def test_trace_evidence_is_authenticated_fresh_and_in_authority(
    tmp_path: Path,
    config: str | None,
    trace_rel: str,
    preexisting: bytes | None,
    invoker: dict[str, Any],
    verdict: str,
    captured: bool | None,
    reason: str | None,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    if config is not None:
        (tmp_path / ".booley").mkdir()
        (tmp_path / ".booley" / "booley.toml").write_text(
            f'[flows.sim]\nrun_cwd = "run"\n{config}', encoding="utf-8"
        )
    (tmp_path / "run").mkdir()
    trace = tmp_path / trace_rel
    trace.parent.mkdir(exist_ok=True)
    if preexisting is not None:
        trace.write_bytes(preexisting)
    outcome = _run_execution(
        handle,
        prepared,
        _passing_trace_invoker(handle, prepared, trace, **{"fresh": True, **invoker}),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert outcome.verdict == verdict
    if captured is not None:
        assert any(artifact.kind == "trace" for artifact in outcome.artifacts) is captured
    if reason is not None:
        assert reason in outcome.tests[0].reason


_BACKEND_TRACE_HINT = (
    "trace requested but no fresh .fst store or convertible .vcd was produced "
    "(a testbench with its own C++ main() writes its dump under a name "
    "Booley cannot guess - declare it in [flows.sim].trace_files)"
)


def _owned_main_invoker(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    writes: tuple[Path, ...],
    *,
    detail: str = _BACKEND_TRACE_HINT,
    adapter: str = "icarus",
):
    """Simulate a native adapter whose testbench wrote *writes* but no declared trace."""

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        for path in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"dump")
        identity = AdapterTransportIdentity(
            adapter,
            "abc123",
            handle.identity,
            ("smoke",),
            _evidence_path(handle),
        )
        publish_native_adapter_result(
            identity,
            "[SIM_RESULT] PASSED\n",
            0,
            trace_required=True,
            trace=AdapterTraceResult("incident", path="incident.txt", detail=detail),
        )
        return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")

    return invoke


def _run_owned_main(
    tmp_path: Path,
    writes: tuple[str, ...],
    *,
    config: str = 'run_cwd = "run"\n',
    detail: str = _BACKEND_TRACE_HINT,
    preexisting: tuple[str, ...] = (),
    booley_dir: str = ".booley",
) -> SimulationTargetOutcome:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / booley_dir
    project.mkdir(exist_ok=True)
    if booley_dir != ".booley":
        (tmp_path / "booley.toml").write_text(
            f'[project]\ndir = "{booley_dir}"\n', encoding="utf-8"
        )
    (project / "booley.toml").write_text(f"[flows.sim]\n{config}", encoding="utf-8")
    (tmp_path / "run").mkdir()
    for name in preexisting:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"old")
    return _run_execution(
        handle,
        prepared,
        _owned_main_invoker(
            handle, prepared, tuple(tmp_path / name for name in writes), detail=detail
        ),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )


_UNQUERYABLE = "trace requested but the retained waveform is not queryable: bad header"


@pytest.mark.parametrize(
    ("writes", "options", "verdict", "prefix", "suffix", "present", "absent"),
    [
        pytest.param(
            ("run/sim.vcd",),
            {},
            "inconclusive",
            None,
            None,
            ("[flows.sim].trace_files", "listed, not adopted): sim.vcd"),
            (),
            id="undeclared-dump-is-inconclusive-with-candidates",  # #884
        ),
        pytest.param(
            ("run/extra.fst",),
            {},
            "inconclusive",
            None,
            None,
            ("listed, not adopted): extra.fst",),
            (),
            id="undeclared-fst-is-named",  # #2543
        ),
        pytest.param(
            ("run/sim.vcd",),
            {"detail": _UNQUERYABLE},
            None,
            _UNQUERYABLE,
            None,
            (),
            (),
            id="unqueryable-detail-survives-verbatim",
        ),
        pytest.param(
            ("run/new.vcd", "run/dump.vcd", ".booley/.runtime/other/attempt.vcd"),
            {"config": 'run_cwd = "."\n', "preexisting": ("run/old.vcd",)},
            None,
            None,
            "listed, not adopted): run/dump.vcd, run/new.vcd",
            (),
            ("old.vcd", "attempt.vcd"),
            id="stale-owned-and-other-attempt-files-excluded",
        ),
        pytest.param(
            ("run/new.vcd", "state/.runtime/other/attempt.vcd"),
            {"booley_dir": "state"},
            None,
            None,
            # run_cwd = "run" is read from the relocated state/booley.toml, so the
            # candidate is named relative to it.
            "listed, not adopted): new.vcd",
            (),
            ("attempt.vcd",),
            id="relocated-runtime-ignored",
        ),
        pytest.param(
            ("run/dump.vcd",), {}, None, None, None, (), ("listed, not adopted",), id="icarus-dump"
        ),
        pytest.param(
            tuple(f"run/w{i}.vcd" for i in range(7)),
            {},
            None,
            None,
            None,
            ("w0.vcd, w1.vcd, w2.vcd, w3.vcd, w4.vcd, +2 more",),
            ("w5.vcd",),
            id="candidates-capped",
        ),
        pytest.param(
            (),
            {},
            "inconclusive",
            None,
            None,
            (),
            ("listed, not adopted",),
            id="no-fresh-waveform",
        ),
    ],
)
def test_trace_candidates_of_an_owned_main_dump(
    tmp_path: Path,
    writes: tuple[str, ...],
    options: dict[str, Any],
    verdict: str | None,
    prefix: str | None,
    suffix: str | None,
    present: tuple[str, ...],
    absent: tuple[str, ...],
) -> None:
    outcome = _run_owned_main(tmp_path, writes, **options)

    reason = outcome.tests[0].reason
    if verdict is not None:
        assert outcome.verdict == verdict
    if prefix is not None:
        assert reason.startswith(prefix)
    if suffix is not None:
        assert reason.endswith(suffix)
    assert all(fragment in reason for fragment in present)
    assert not any(fragment in reason for fragment in absent)


def test_stale_compatibility_siblings_do_not_cross_execution_boundary(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    stale = {
        "results_xml": prepared.build_root / "results.xml",
        "cocotb_results_json": prepared.build_root / "cocotb_results.json",
        "trace_status": prepared.build_root / "trace_status.json",
        "trace_incident": prepared.build_root / "trace_incident.txt",
    }
    for path in stale.values():
        path.write_text("stale", encoding="utf-8")
    (prepared.build_root / "result.json").write_text("stale", encoding="utf-8")
    # The launched bundle carries the stale copies; only a rewrite is current.
    result = handle.project_root / "attempt" / "bundle" / "result.json"

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        result.write_text("current result", encoding="utf-8")
        _write_transport(
            handle,
            prepared,
            ("smoke",),
            AdapterResult(
                True,
                False,
                0,
                ("smoke",),
                test_results=(AdapterTestResult("smoke", "pass"),),
            ),
        )
        return SubprocessResult(
            returncode=0,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
        )

    outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)

    kinds = {item.kind for item in outcome.artifacts}
    assert "result" in kinds
    assert kinds.isdisjoint(stale)


@pytest.mark.parametrize(
    ("cocotb", "name", "kind", "captured"),
    [
        pytest.param(True, "results.xml", "results_xml", True, id="cocotb-evidence-root"),
        pytest.param(False, "result.json", "result", False, id="native-captures-none"),
    ],
)
def test_bundle_launch_keeps_each_adapters_compatibility_evidence(
    tmp_path: Path, cocotb: bool, name: str, kind: str, captured: bool
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=cocotb)
    # A bundle launch never writes into the bundle: Cocotb writes its
    # compatibility files to the attempt's evidence root, so that is where they
    # are captured; a native adapter's files there are not compatibility evidence.
    written = handle.project_root / "attempt" / "execution-evidence" / name

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        written.write_text("current", encoding="utf-8")
        _write_transport(
            handle,
            prepared,
            ("smoke",),
            AdapterResult(
                True,
                False,
                0,
                ("smoke",),
                test_results=(AdapterTestResult("smoke", "pass"),),
            ),
            adapter="cocotb" if cocotb else "icarus",
        )
        return SubprocessResult(
            returncode=0,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
        )

    outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=cocotb)

    assert (kind in {item.kind for item in outcome.artifacts}) is captured
    assert not (handle.project_root / "attempt" / "bundle" / name).exists()


def test_unchanged_adapter_result_is_stale(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    _write_transport(
        handle,
        prepared,
        ("smoke",),
        AdapterResult(
            True,
            False,
            0,
            ("smoke",),
            test_results=(AdapterTestResult("smoke", "pass"),),
        ),
    )

    with pytest.raises(ArtifactValidationError, match="stale"):
        _run_execution(
            handle,
            prepared,
            lambda _command, timeout: SubprocessResult(
                returncode=0,
                stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
            ),
            ("smoke",),
            cocotb=False,
        )


def _traced_smoke_launch(handle: TargetHandle, trace: Path, stdout: str = "") -> SubprocessResult:
    """Write a passing traced ``smoke`` transport at the attempt's evidence path."""
    trace.parent.mkdir(exist_ok=True)
    trace.write_bytes(b"fresh waveform")
    _write_transport(
        handle,
        _prepared(handle, cocotb=False),
        ("smoke",),
        AdapterResult(
            True,
            False,
            0,
            ("smoke",),
            test_results=(AdapterTestResult("smoke", "pass"),),
            trace=AdapterTraceResult("ok", path=str(trace)),
        ),
    )
    return SubprocessResult(
        returncode=0,
        stdout=f"BOOLEY_BUILD_STAGE token=abc123 rc=0\n{stdout}TRACE_OK: {trace}\n",
    )


def test_traced_attempt_deadline_adds_conversion_budget(tmp_path: Path) -> None:
    """A simulation passing near its timeout still has the whole conversion budget."""
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text(
        "[flows.sim]\ntimeout_ms = 5000\ntrace_convert_timeout_ms = 7000\n", encoding="utf-8"
    )
    commands: list[str] = []
    timeouts: list[int] = []

    def invoke(command: list[str], *, timeout: int, **kwargs: Any) -> SubprocessResult:
        timeouts.append(timeout)
        commands.append(command[-1])
        return _traced_smoke_launch(handle, tmp_path / "first" / "wave.fst")

    _run_execution(
        handle,
        prepared,
        invoke,
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert timeouts == [5 + 7 + 90]
    work = launched_work(commands[0])
    assert (work.timeout_s, work.trace_convert_timeout_s) == (5, 7)


def test_trace_declarations_are_frozen_at_planning(tmp_path: Path) -> None:
    """Config edited after planning, while the build runs, never reaches the launch."""
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    prepared = replace(
        prepared,
        resolved=replace(
            prepared.resolved,
            files=(ResolvedFile("firmware.hex", "user"),),
        ),
    )
    project = tmp_path / ".booley"
    project.mkdir()
    config = project / "booley.toml"
    config.write_text(
        "[flows.sim]\n"
        'run_cwd = "run"\n'
        'trace_files = ["../first/*.fst"]\n'
        "timeout_ms = 1000\n"
        "trace_convert_timeout_ms = 7000\n"
        'cycle_sentinels = ["[OLD]"]\n'
        'pre_run_commands = ["old"]\n',
        encoding="utf-8",
    )
    (tmp_path / "run").mkdir()
    (tmp_path / "other-run").mkdir()
    (prepared.build_root / "firmware.hex").write_text("generated\n", encoding="utf-8")
    launches: list[str] = []
    launch_timeouts: list[int] = []

    def invoke(command: list[str], *, timeout: int, **kwargs: Any) -> SubprocessResult:
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            config.write_text(
                "[flows.sim]\n"
                'run_cwd = "other-run"\n'
                'trace_files = ["../second/*.fst"]\n'
                "timeout_ms = 2000\n"
                "trace_convert_timeout_ms = 9000\n"
                'cycle_sentinels = ["[NEW]"]\n'
                'pre_run_commands = ["new"]\n',
                encoding="utf-8",
            )
            return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")
        launches.append(command[-1])
        launch_timeouts.append(timeout)
        return _traced_smoke_launch(handle, tmp_path / "first" / "wave.fst", "[OLD] smoke 17\n")

    outcome = _run_execution(
        handle,
        prepared,
        invoke,
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
        canned_at="raw",
    )

    assert outcome.verdict == "pass"
    (work,) = (launched_work(command) for command in launches)
    assert work.trace_files == ("../first/*.fst",)
    assert (work.timeout_s, work.trace_convert_timeout_s) == (1, 7)
    # simulation timeout + conversion budget + the fixed 90 s margin
    assert launch_timeouts == [1 + 7 + 90]
    assert outcome.tests[0].cycles == 17


@pytest.mark.parametrize(
    ("eda_tool", "artifact_names"),
    [
        ("icarus", ("demo.scr", "demo")),
        ("verilator", ("Vtb_demo",)),
    ],
)
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("log_failure", [False, True])
def test_ordinary_group_builds_before_launching_supplied_bundle(
    tmp_path: Path,
    eda_tool: str,
    artifact_names: tuple[str, ...],
    observed: bool,
    log_failure: bool,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    prepared = replace(
        prepared,
        eda_tool=eda_tool,
        resolved=replace(prepared.resolved, eda_tool=eda_tool),
    )
    artifacts = tuple(prepared.build_root / name for name in artifact_names)
    for artifact in artifacts:
        artifact.write_bytes(b"simulator image")
    bundle = tmp_path / "attempt" / "bundle"
    bundle.mkdir(parents=True)
    evidence = tmp_path / "attempt" / "execution-evidence"
    for artifact in artifacts:
        (bundle / artifact.name).write_bytes(artifact.read_bytes())
    run_cwd = tmp_path / "run"
    run_cwd.mkdir()
    commands: list[list[str]] = []
    timeouts: list[int] = []
    bundle_invoke = _bundle_invoke(commands, eda_tool, handle, evidence)

    def invoke(command: list[str], *, timeout: int, **kwargs: Any) -> SubprocessResult:
        timeouts.append(timeout)
        return bundle_invoke(command, timeout=timeout)

    execution = SimulationExecution(
        invoke=invoke,
        options=SimulationOptions(timeout_ms=5000, build_timeout_ms=7000),
    )
    with (
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch("booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123"),
        patch.object(SimulationBuildSession, "capture_inputs", return_value={}),
        patch.object(SimulationBuildSession, "authorize_fresh_image", return_value=artifacts),
    ):
        with execution.ordinary_group(handle, ("smoke",)) as group:
            build = group.compile()
            assert build.passed
            assert group.artifact_paths == artifacts
            assert len(commands) == 1
        sink = LiveProgressSink(tmp_path / "progress.json", tmp_path, "bundle")
        with (
            install_progress(sink) if observed else nullcontext(),
            patch("booley.flows.run_log.begin_run_log", side_effect=OSError("full"))
            if log_failure
            else nullcontext(),
        ):
            outcome = group.launch_bundle(bundle, evidence, run_cwd)

    assert outcome.passed
    if observed and log_failure:
        assert sink.error == "OSError: full"
    assert timeouts == [7, 5]
    assert len(commands) == 2
    launch = commands[1][-1]
    work = launched_work(launch)
    assert work.adapter == eda_tool
    assert Path(work.build_dir) == bundle
    assert Path(work.run_cwd) == run_cwd
    assert Path(work.work_dir) == evidence
    assert str(prepared.build_root) not in repr(work)


def _bundle_invoke(commands, eda_tool, handle, evidence):
    def invoke(command: list[str], *, timeout: int, **kwargs: Any) -> SubprocessResult:
        del timeout
        commands.append(command)
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")
        identity = AdapterTransportIdentity(
            eda_tool,
            "abc123",
            handle.identity,
            ("smoke",),
            evidence / "adapter-abc123.json",
        )
        write_adapter_result(
            identity,
            AdapterResult(
                True,
                False,
                0,
                ("smoke",),
                test_results=(AdapterTestResult("smoke", "pass"),),
            ),
        )
        return SubprocessResult(returncode=0, stdout="[SIM_RESULT] PASSED\n")

    return invoke


def test_ordinary_group_refuses_bundle_launch_until_lease_released(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    with (
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch("booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123"),
        execution.ordinary_group(handle, ("smoke",)) as group,
        pytest.raises(SimulationBuildSlotError, match="leave ordinary_group"),
    ):
        group.launch_bundle(tmp_path / "bundle", tmp_path / "evidence", tmp_path / "run")


@pytest.mark.parametrize(
    ("output", "sentinels", "expected"),
    [
        pytest.param("[SIM_CYCLES]", (), ("malformed", None), id="empty-payload"),
        pytest.param("[SIM_CYCLES] smoke nope", (), ("malformed", None), id="non-digit-count"),
        pytest.param("[SIM_CYCLES] nope", (), ("malformed", None), id="non-digit-unnamed"),
        pytest.param(
            "[SIM_CYCLES] smoke \u0661\u0662", (), ("malformed", None), id="non-ascii-digits"
        ),
        pytest.param("[SIM_CYCLES] smoke \u00b2", (), ("malformed", None), id="superscript-digit"),
        pytest.param(
            "CYCLES TOTAL smoke 20",
            ("CYCLES", "CYCLES TOTAL"),
            ("observed", 20),
            id="longest-sentinel-first",
        ),
        pytest.param(
            "log\n[SIM_CYCLES] stress 200\n[SIM_CYCLES]   smoke   42\nmore",
            (),
            ("observed", 42),
            id="observed",
        ),
        pytest.param(
            "[SIM_CYCLES]\n[SIM_CYCLES] smoke 4",
            (),
            ("observed", 4),
            id="empty-record-does-not-cancel",
        ),
        pytest.param("[SIM_CYCLES] stress 42", (), ("wrong_test", None), id="wrong-test"),
        pytest.param("ordinary output", (), ("missing", None), id="missing"),
        pytest.param("[SIM_CYCLES] 42", (), ("legacy", 42), id="legacy"),
        pytest.param(
            "[SIM_CYCLES] smoke 41\n[SIM_CYCLES] smoke 42",
            (),
            ("duplicate", None),
            id="duplicate-named",
        ),
        pytest.param(
            "[SIM_CYCLES] 10\n[SIM_CYCLES] 20", (), ("duplicate", None), id="duplicate-unnamed"
        ),
        pytest.param(
            "[SIM_CYCLES] smoke 7\n[SIM_CYCLES] 3", (), ("observed", 7), id="named-beats-unnamed"
        ),
        pytest.param(
            "[SIM_CYCLES] stress 5\n[SIM_CYCLES] 3",
            (),
            ("legacy", 3),
            id="other-test-and-unnamed",
        ),
        pytest.param(
            "[SIM_CYCLES] smoke 10\nCYCLES[1] smoke 9",
            ("CYCLES[1]",),
            ("observed", 9),
            id="configured-literal-replaces-default",
        ),
        pytest.param(
            "CoreMark completed in: smoke 12",
            ("CoreMark completed in:",),
            ("observed", 12),
            id="configured-multi-word-sentinel",
        ),
        pytest.param(
            "[SIM_CYCLES] smoke nope\n[SIM_CYCLES] 5",
            (),
            ("malformed", None),
            id="bad-named-not-rescued",
        ),
        pytest.param(
            "[SIM_CYCLES] stress 5\n[SIM_CYCLES] nope",
            (),
            ("malformed", None),
            id="bad-unnamed-beats-other",
        ),
        pytest.param(
            "[SIM_CYCLES]\n[SIM_CYCLES] stress 5",
            (),
            ("malformed", None),
            id="empty-without-own-record",
        ),
    ],
)
def test_cycle_count_records_are_classified_per_test(
    tmp_path: Path,
    output: str,
    sentinels: tuple[str, ...],
    expected: tuple[str, int | None],
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    if sentinels:
        (tmp_path / ".booley").mkdir()
        (tmp_path / ".booley" / "booley.toml").write_text(
            f"[flows.sim]\ncycle_sentinels = {json.dumps(list(sentinels))}\n",
            encoding="utf-8",
        )

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        _write_transport(handle, prepared, ("smoke",), _passing_result())
        return SubprocessResult(
            returncode=0, stdout=f"BOOLEY_BUILD_STAGE token=abc123 rc=0\n{output}"
        )

    outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)

    assert (outcome.tests[0].cycle_status, outcome.tests[0].cycles) == expected


@contextmanager
def _real_group(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """An ordinary group of the ``sim`` target of *project* built with the fake Icarus tools."""
    fake_bin = _write_fake_icarus_tools(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project),
        options=SimulationOptions(timeout_ms=hang_guard_s() * 1000),
    )
    with execution.ordinary_group(handle, ()) as group:
        yield group


@pytest.mark.parametrize("copyto", ["firmware.hex", "data/firmware.hex", None])
@pytest.mark.parametrize(
    ("source_bytes", "source_sha256"),
    [
        (b"00000013\n", "acc1a4b3ae11b9c321268fac9cc42bee0be463484ec33a3cec6b84ee22094a3e"),
        (b"00000013\r\n", "9aa09655c12bb69c0ceb5ba04048dea837df879713beaef16c03531bfb9f02b1"),
    ],
)
def test_real_copyto_prepared_source_entries(
    tmp_path, monkeypatch, copyto, source_bytes, source_sha256
):
    importlib.import_module("fusesoc")
    importlib.import_module("edalize")
    project = tmp_path / "project"
    _write_runtime_input_project(project)
    (project / "data/firmware.hex").write_bytes(source_bytes)
    core = project / "runtime_input.core"
    core.write_text(
        core.read_text().replace(
            ", copyto: firmware.hex", "" if copyto is None else ", copyto: " + copyto
        )
    )
    with _real_group(project, tmp_path, monkeypatch) as group:
        entries = group.prepared_source_entries()
        data = next(entry for entry in entries if str(entry["path"]).endswith("firmware.hex"))
        assert data["source_path"] == "data/firmware.hex"
        assert data["path"] == (copyto or "src/acme_lib_runtime_input_1/data/firmware.hex")
        assert data["sha256"] == f"sha256:{source_sha256}"
        (group.build_root / data["path"]).write_bytes(b"prepared bytes changed")
        changed = next(
            entry
            for entry in group.prepared_source_entries()
            if str(entry["path"]).endswith("firmware.hex")
        )
        assert changed["sha256"] == (
            "sha256:245e1c58419263e34092a956d221f87ed55a077261df60c02b17645711cbd929"
        )
        assert (project / "data/firmware.hex").read_bytes() == source_bytes


@pytest.mark.parametrize(
    "core, declarations, expected",
    [
        (None, [("nested/a.hex", "a:b:c:1")], "nested/a.hex"),
        (None, [("nested/a.hex", "a:b:c:1"), ("other/a.hex", "a:b:d:1")], None),
        ("a:b:c:1", [("nested/a.hex", "a:b:c:1"), ("other/a.hex", "a:b:d:1")], "nested/a.hex"),
        ("a:b:foreign:1", [("nested/a.hex", "a:b:c:1")], None),
    ],
)
def test_copyto_source_association_requires_unique_full_destination_and_core(
    tmp_path, core, declarations, expected
):
    # Private seam: the declaration map comes from FuseSoC's resolved core graph; only a
    # stubbed map reaches the ambiguous and foreign-core cases without four real cores.
    from booley.flows.sim.execution.engine import _SourceDeclaration

    group = _prepared_group_with_sources(_handle(tmp_path))
    staged = group.build_root / "a.hex"
    staged.write_bytes(b"staged bytes")
    group._attempt.prepared = replace(
        group._attempt.prepared,
        resolved=replace(
            group._attempt.prepared.resolved, files=(ResolvedFile("a.hex", "user", core=core),)
        ),
    )
    group._source_declarations = tuple(
        _SourceDeclaration(path, owner, "user", ("a.hex",)) for path, owner in declarations
    )
    entry = group.prepared_source_entries()[0]
    assert entry.get("source_path") == expected
    assert entry["sha256"] == (
        "sha256:a9228f1bda65e64e61f866520f7439e2e7a549dd27b47abf29e1b76dbb93b404"
    )


def test_copyto_declaration_drift_refuses_publication(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _write_runtime_input_project(project)
    with _real_group(project, tmp_path, monkeypatch) as group:
        core = project / "runtime_input.core"
        core.write_text(core.read_text().replace("copyto: firmware.hex", "copyto: changed.hex"))
        with pytest.raises(SimulationBuildSlotError, match="compile inputs changed"):
            group.prepared_source_entries()


@pytest.mark.parametrize(
    "source, expected", [("nested/../data.hex", "data.hex"), ("../outside.hex", None)]
)
def test_copyto_declared_path_lexical_confinement(tmp_path, source, expected):
    # Private seam: FuseSoC refuses a `..` source path before staging, so no public
    # request reaches this confinement check.
    from booley.flows.sim.execution.engine import _safe_declared_path

    if expected is None:
        with pytest.raises(SimulationBuildSlotError, match="escapes Project"):
            _safe_declared_path(tmp_path, source)
    else:
        (tmp_path / "nested").symlink_to(tmp_path.parent, target_is_directory=True)
        assert _safe_declared_path(tmp_path, source) == expected


def test_prepared_source_entries_real_dependency_core_export_root(tmp_path, monkeypatch):
    importlib.import_module("fusesoc")
    importlib.import_module("edalize")
    project = tmp_path / "project"
    _write_runtime_input_project(project)
    core = project / "runtime_input.core"
    core.write_text(
        core.read_text().replace(
            "      - data/firmware.hex: {file_type: user, copyto: firmware.hex}",
            "    depend: [acme:lib:data:1]",
        )
    )
    dependency = project / "dependency"
    (dependency / "inputs").mkdir(parents=True)
    (dependency / "inputs/firmware.hex").write_bytes(b"dependency bytes")
    (dependency / "data.core").write_text(
        "CAPI=2:\nname: acme:lib:data:1\nfilesets:\n  data:\n    files: [inputs/firmware.hex]\n    file_type: user\ntargets:\n  default:\n    filesets: [data]\n"
    )
    with _real_group(project, tmp_path, monkeypatch) as group:
        data = next(
            entry
            for entry in group.prepared_source_entries()
            if str(entry["path"]).endswith("firmware.hex")
        )
        assert data["path"] == "src/acme_lib_data_1/inputs/firmware.hex"
        assert data["source_path"] == "dependency/inputs/firmware.hex"
        assert data["sha256"] == (
            "sha256:ae41cf82309521b9e587f2a73f60d94b4afa3125c4c690dc22546befc52b1059"
        )


def test_real_copyto_same_basename_uses_full_declared_paths(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _write_runtime_input_project(project)
    (project / "other").mkdir()
    (project / "other/firmware.hex").write_bytes(b"second bytes")
    core = project / "runtime_input.core"
    core.write_text(
        core.read_text()
        .replace("copyto: firmware.hex", "copyto: first.hex")
        .replace(
            "targets:\n",
            "      - other/firmware.hex: {file_type: user, copyto: second.hex}\ntargets:\n",
        )
    )
    with _real_group(project, tmp_path, monkeypatch) as group:
        entries = {entry["path"]: entry for entry in group.prepared_source_entries()}
        assert entries["first.hex"]["source_path"] == "data/firmware.hex"
        assert entries["second.hex"]["source_path"] == "other/firmware.hex"
        assert entries["first.hex"]["sha256"] != entries["second.hex"]["sha256"]


@pytest.mark.parametrize("observed", [False, True])
def test_retained_log_failure_keeps_build_slot_failure_boundary(tmp_path, observed):
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    retained_root = tmp_path / "retained"
    retained_root.mkdir()
    retained = replace(prepared, build_root=retained_root, work_root=retained_root)
    invoke = MagicMock()
    execution = SimulationExecution(invoke=invoke, options=SimulationOptions())
    sink = LiveProgressSink(tmp_path / "progress.json", tmp_path, "reuse")
    real_begin = run_log.begin_run_log

    def begin(root: Path, *args: Any, **kwargs: Any) -> Any:
        if root == retained_root:
            raise OSError("full")
        return real_begin(root, *args, **kwargs)

    with (
        install_progress(sink) if observed else nullcontext(),
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch("booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared),
        _compile_surface_patch(handle),
        patch.object(SimulationBuildSession, "capture_inputs", return_value={}),
        patch.object(SimulationBuildSession, "reusable_key", return_value="key"),
        patch.object(SimulationBuildSession, "try_reuse", return_value=retained),
        patch.object(SimulationBuildSession, "discard_candidate") as discard,
        patch("booley.flows.run_log.begin_run_log", side_effect=begin),
        pytest.raises(SimulationBuildSlotError, match="cannot open retained generation log"),
        execution.ordinary_group(handle, ()) as group,
    ):
        group.compile()
    discard.assert_called_once_with(prepared.work_root)
    invoke.assert_not_called()


def test_lease_wait_reports_and_is_bounded_by_the_build_timeout(tmp_path: Path) -> None:
    """A busy Target slot shows that the run waits, for the build timeout, not 60 s (#4843)."""
    handle = _handle(tmp_path)
    execution = SimulationExecution(
        invoke=MagicMock(), options=SimulationOptions(build_timeout_ms=1000)
    )
    stream = io.StringIO()
    slot = simulation_build_slot(handle, "")
    slot.mkdir(parents=True)
    with (
        (slot / "lease.lock").open("a+", encoding="utf-8") as holder,
        TerminalProgress("sim", "sim", tmp_path, stream=stream).installed() as progress,
        _compile_surface_patch(handle),
        patch("booley.flows.sim.execution.engine._source_declarations", return_value=()),
    ):
        acquire_file_lock(holder)
        started = time.monotonic()
        with (
            pytest.raises(SimulationBuildLeaseTimeoutError, match="busy for 1s"),
            execution.ordinary_group(handle, ()),
        ):
            pass
        waited = time.monotonic() - started
        stage = progress.stage

    assert "waiting for the Simulation build slot" in stage
    # A loose upper bound on a real 1 s wait; the old fixed wait was 60 s.
    assert waited < 30
