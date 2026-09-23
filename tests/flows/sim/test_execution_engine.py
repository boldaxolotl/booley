"""Production-interface tests for the deep Simulation execution boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from booley.core.build_paths import work_root_for
from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTraceResult,
    AdapterTransportIdentity,
    partial_result_identity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import (
    SimulationBuildSession,
    SimulationBuildSlotError,
    simulation_build_slot,
)
from booley.flows.sim.execution import (
    DefaultSelection,
    NamedTests,
    PreSimEvidence,
    SimulationExecution,
    SimulationOptions,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.execution.engine import PreparedOrdinaryGroup, _preview_work
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.trace_session import TraceSession
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.runtime.paths import native_bwave_binary
from booley.runtime.project_dir import reset_cache
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle


def _handle(root: Path, *, selector: str = "sim") -> TargetHandle:
    return cast(
        TargetHandle,
        SimpleNamespace(
            project_root=root.resolve(),
            selector=selector,
            identity=f"acme:lib:demo:1#{selector}",
            vlnv="acme:lib:demo:1",
        ),
    )


def _prepared(handle: TargetHandle, *, cocotb: bool) -> PreparedSimulationBuild:
    build_root = handle.project_root / "build" / handle.selector
    build_root.mkdir(parents=True)
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


def _prepared_group_with_source(handle: TargetHandle, source: Path) -> PreparedOrdinaryGroup:
    prepared = _prepared(handle, cocotb=False)
    prepared = replace(
        prepared,
        resolved=replace(
            prepared.resolved,
            files=(ResolvedFile(str(source), "systemVerilogSource"),),
        ),
    )
    return PreparedOrdinaryGroup(
        MagicMock(),
        handle,
        cast(Any, SimpleNamespace(prepared=prepared)),
        MagicMock(),
        0.0,
        {},
        {},
        {},
    )


def _inspection(*, cocotb: bool) -> SimpleNamespace:
    inspection = SimpleNamespace(
        toplevel="tb_demo",
        eda_tool="icarus",
        parameters={},
        flow_options={"cocotb_module": "test_demo"} if cocotb else {},
    )
    inspection.inspect = lambda _handle: inspection
    return inspection


def _runtime_input_vvp(root: Path, build_root: Path) -> Path:
    (build_root / "demo.scr").write_text("", encoding="utf-8")
    (build_root / "demo").write_text("", encoding="utf-8")
    (build_root / "dhry.hex").write_text("00000013\n", encoding="utf-8")
    fake_bin = root / "bin"
    fake_bin.mkdir()
    vvp = fake_bin / ("vvp.bat" if os.name == "nt" else "vvp")
    if os.name == "nt":
        script = (
            "@echo off\n"
            "if not exist dhry.hex (\n"
            "  echo $readmemh: Unable to open dhry.hex for reading.\n"
            "  exit /b 1\n"
            ")\n"
            "echo [SIM_RESULT] PASSED\n"
        )
    else:
        script = (
            "#!/bin/sh\n"
            "if [ ! -f dhry.hex ]; then\n"
            "  echo '$readmemh: Unable to open dhry.hex for reading.'\n"
            "  exit 1\n"
            "fi\n"
            "echo '[SIM_RESULT] PASSED'\n"
        )
    vvp.write_text(script, encoding="utf-8")
    vvp.chmod(0o755)
    return fake_bin


def _write_runtime_input_project(root: Path) -> Path:
    state = root / ".booley_project"
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
        "    flow: sim\n"
        "    flow_options: {tool: icarus}\n"
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
    vvp = fake_bin / f"vvp{suffix}"
    if os.name == "nt":
        vvp_script = (
            "@echo off\n"
            "if not exist firmware.hex (\n"
            "  echo $readmemh: Unable to open firmware.hex for reading.\n"
            "  exit /b 1\n"
            ")\n"
            "echo [SIM_RESULT] PASSED\n"
        )
    else:
        vvp_script = (
            "#!/bin/sh\n"
            "if [ ! -f firmware.hex ]; then\n"
            "  echo '$readmemh: Unable to open firmware.hex for reading.'\n"
            "  exit 1\n"
            "fi\n"
            "echo '[SIM_RESULT] PASSED'\n"
        )
    vvp.write_text(vvp_script, encoding="utf-8")
    vvp.chmod(0o755)
    return fake_bin


def _write_stale_compiler_fixture(root: Path) -> tuple[Path, Path]:
    """A compiler that deliberately leaves an existing image stale."""
    project = root / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
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
    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=root,
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


def _run_execution(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    invoke,
    names: tuple[str, ...] | None,
    *,
    cocotb: bool,
    options: SimulationOptions | None = None,
    artifact_root: Path | None = None,
    trace_mode: TraceMode | None = None,
) -> SimulationTargetOutcome:
    execution = SimulationExecution(
        invoke=invoke,
        options=options or SimulationOptions(),
        artifact_root=artifact_root,
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                execution,
                "_run_groups_with_session",
                side_effect=lambda current_handle, groups: [
                    execution._run_group(current_handle, group) for group in groups
                ],
            )
        )
        stack.enter_context(
            patch(
                "booley.flows.sim.execution.engine.TargetCatalog.build",
                return_value=_inspection(cocotb=cocotb),
            )
        )
        if trace_mode is None:
            stack.enter_context(
                patch(
                    "booley.flows.sim.execution.engine.prepare_simulation_build",
                    return_value=prepared,
                )
            )
        else:
            stack.enter_context(
                patch.object(execution, "_prepare_build", return_value=(prepared, trace_mode))
            )
        stack.enter_context(
            patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123")
        )
        selection = DefaultSelection() if names is None else NamedTests(names)
        return execution.run(handle, selection)


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
        prepared.build_root / ".booley-adapter-abc123.json",
    )
    write_adapter_result(identity, result)


def _passing_attempt_invoker(
    handle: TargetHandle,
    prepared: PreparedSimulationBuild,
    attempts: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    adapter: str,
) -> Callable[..., SubprocessResult]:
    remaining = iter(attempts)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        token, names = next(remaining)
        identity = AdapterTransportIdentity(
            adapter,
            token,
            handle.identity,
            names,
            prepared.build_root / f".booley-adapter-{token}.json",
        )
        result = AdapterResult(
            True,
            False,
            0,
            names,
            test_results=tuple(AdapterTestResult(name, "pass") for name in names),
        )
        write_adapter_result(identity, result)
        return SubprocessResult(
            returncode=0,
            stdout=f"BOOLEY_BUILD_STAGE token={token} rc=0\n",
        )

    return invoke


def _write_partial_timeout_transport(
    handle,
    prepared,
    *,
    modified_ns: int | None = None,
) -> None:
    identity = AdapterTransportIdentity(
        "cocotb",
        "abc123",
        handle.identity,
        ("done", "active", "later"),
        prepared.build_root / ".booley-adapter-abc123.json",
    )
    result = AdapterResult(
        False,
        True,
        0,
        identity.selected_tests,
        failure_kind="timeout",
        test_results=(
            AdapterTestResult("done", "pass"),
            AdapterTestResult("active", "timeout"),
            AdapterTestResult("later", "inconclusive"),
        ),
    )
    partial_identity = partial_result_identity(identity)
    write_adapter_result(partial_identity, result)
    if modified_ns is not None:
        os.utime(partial_identity.result_path, ns=(modified_ns, modified_ns))


def _passing_trace_invoker(
    handle,
    prepared,
    trace: Path,
    *,
    fresh: bool,
    modified_ns: int | None = None,
    transport_modified_ns: int | None = None,
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
                trace=AdapterTraceResult("ok", path=str(trace)),
            ),
        )
        if transport_modified_ns is not None:
            result_path = prepared.build_root / ".booley-adapter-abc123.json"
            os.utime(result_path, ns=(transport_modified_ns, transport_modified_ns))
        return SubprocessResult(
            returncode=0,
            stdout=f"BOOLEY_BUILD_STAGE token=abc123 rc=0\nTRACE_OK: {trace}\n",
        )

    return invoke


class _ChangingTraceRun:
    def __init__(
        self,
        handle: TargetHandle,
        prepared: PreparedSimulationBuild,
        config: Path,
        traces: tuple[Path, Path],
    ) -> None:
        self.handle = handle
        self.prepared = prepared
        self.config = config
        self.traces = traces
        self.commands: list[str] = []
        self.pre_sim_timeouts: list[int] = []
        self.invocation_timeouts: list[int] = []

    def pre_sim(self, _handle, attempt) -> PreSimEvidence:
        self.pre_sim_timeouts.append(attempt.wrapper_timeout_s)
        (self.prepared.build_root / "firmware.hex").write_text("generated\n", encoding="utf-8")
        if not self.commands:
            self.config.write_text(
                "[flows.sim]\n"
                'run_cwd = "other-run"\n'
                'trace_files = ["../second/*.fst"]\n'
                "timeout_ms = 2000\n"
                'cycle_sentinels = ["[NEW]"]\n'
                'pre_run_commands = ["new"]\n',
                encoding="utf-8",
            )
        return PreSimEvidence((), ("smoke",), "passed", 0.0, "")

    def invoke(self, command: list[str], *, timeout: int) -> SubprocessResult:
        self.invocation_timeouts.append(timeout)
        self.commands.append(command[-1])
        trace = self.traces[len(self.commands) - 1]
        trace.parent.mkdir(exist_ok=True)
        trace.write_bytes(b"fresh waveform")
        _write_transport(
            self.handle,
            self.prepared,
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
            stdout=(
                f"BOOLEY_BUILD_STAGE token=abc123 rc=0\n"
                f"{'[OLD]' if len(self.commands) == 1 else '[NEW]'} smoke "
                f"{17 if len(self.commands) == 1 else 23}\n"
                f"TRACE_OK: {trace}\n"
            ),
        )


def _assert_authoritative_cocotb_outcome(outcome: SimulationTargetOutcome) -> None:
    assert outcome.verdict == "fail"
    assert [(test.name, test.verdict) for test in outcome.tests] == [
        ("reset", "pass"),
        ("count", "fail"),
    ]
    assert outcome.tests[1].cycles == 17
    assert outcome.tests[1].workload_snapshot is not None
    assert outcome.builds[0].passed is True
    assert sum(a.kind == "live_run_log" for a in outcome.artifacts) == 1
    assert outcome.tests[0].run_log_path == outcome.tests[1].run_log_path
    assert Path(outcome.tests[0].run_log_path).parts[-3:] == ("build", "sim", "run.log")


def test_authenticated_cocotb_result_is_the_per_test_authority(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        assert command[:2] == ["sh", "-c"]
        assert timeout == 600
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
        artifact_root=tmp_path / "reports",
    )

    _assert_authoritative_cocotb_outcome(outcome)


def test_default_cocotb_selection_snapshots_discovered_test_names(tmp_path: Path) -> None:
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
    snapshot = outcome.tests[0].workload_snapshot
    assert snapshot is not None and snapshot["test"] == "discovered"


def test_timeout_transport_preserves_completed_active_and_not_run_tests(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)
    token = "abc123"

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        identity = AdapterTransportIdentity(
            "cocotb",
            token,
            handle.identity,
            ("done", "active", "later"),
            prepared.build_root / f".booley-adapter-{token}.json",
        )
        write_adapter_result(
            identity,
            AdapterResult(
                passed=False,
                inconclusive=True,
                sva_errors=0,
                tests=identity.selected_tests,
                failure_kind="timeout",
                test_results=(
                    AdapterTestResult("done", "pass", elapsed_s=0.1),
                    AdapterTestResult("active", "timeout", detail="timed out while running"),
                    AdapterTestResult("later", "inconclusive", detail="did not run"),
                ),
            ),
        )
        return SubprocessResult(
            returncode=-9,
            stdout=f"BOOLEY_BUILD_STAGE token={token} rc=0\n",
            timed_out=True,
            duration_s=10.0,
        )

    outcome = _run_execution(handle, prepared, invoke, ("done", "active", "later"), cocotb=True)

    assert [(test.name, test.verdict, test.timed_out) for test in outcome.tests] == [
        ("done", "pass", False),
        ("active", "timeout", True),
        ("later", "inconclusive", False),
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
        ("later", "inconclusive"),
    ]


def test_unchanged_timeout_partial_result_is_stale(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=True)
    _write_partial_timeout_transport(handle, prepared)

    outcome = _run_execution(
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

    assert outcome.verdict == "error"
    failure = outcome.infrastructure_failure
    assert failure is not None and "stale" in failure.detail


def test_run_log_open_failure_is_typed_infrastructure(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    invoke = MagicMock()

    with patch(
        "booley.flows.sim.execution.engine.begin_run_log",
        side_effect=OSError("read-only filesystem"),
    ):
        outcome = _run_execution(handle, prepared, invoke, ("smoke",), cocotb=False)

    assert outcome.verdict == "error"
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "artifact_persistence"
    invoke.assert_not_called()


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
    source.write_text("module top; endmodule\n", encoding="utf-8")
    group = _prepared_group_with_source(handle, source)

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
    source.write_text("module outside; endmodule\n", encoding="utf-8")
    group = _prepared_group_with_source(handle, source)

    with pytest.raises(SimulationBuildSlotError, match="unsafe prepared Simulation source"):
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
        options=SimulationOptions(timeout_ms=5_000),
    )

    assert outcome.passed is True
    assert not (handle.project_root / "dhry.hex").exists()


def test_declared_runtime_input_is_cleaned_after_adapter_timeout(tmp_path: Path) -> None:
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
    _runtime_input_vvp(tmp_path, prepared.build_root)

    def timeout_invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        staged = handle.project_root / "dhry.hex"
        assert staged.is_symlink()
        assert staged.read_text(encoding="utf-8") == "00000013\n"
        return SubprocessResult(
            returncode=-9,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
            timed_out=True,
            duration_s=0.01,
        )

    outcome = _run_execution(
        handle,
        prepared,
        timeout_invoke,
        ("dhry",),
        cocotb=False,
    )

    assert outcome.tests[0].timed_out is True
    assert not (handle.project_root / "dhry.hex").exists()


def test_conflicting_runtime_input_is_typed_infrastructure_failure(tmp_path: Path) -> None:
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
    _runtime_input_vvp(tmp_path, prepared.build_root)
    existing = handle.project_root / "dhry.hex"
    existing.write_text("project-owned\n", encoding="utf-8")
    invoke = MagicMock()

    outcome = _run_execution(
        handle,
        prepared,
        invoke,
        ("dhry",),
        cocotb=False,
    )

    assert outcome.verdict == "error"
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "runtime_input"
    assert "conflicts with an existing path" in outcome.infrastructure_failure.detail
    assert existing.read_text(encoding="utf-8") == "project-owned\n"
    invoke.assert_not_called()


def _run_real_icarus(project: Path, handle: TargetHandle) -> SimulationTargetOutcome:
    real_resolve = fusesoc_registry.resolve_target_handle
    fusesoc_cmd = (
        list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        if shutil.which("fusesoc")
        else [sys.executable, "-c", "from fusesoc.main import main; main()"]
    )
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project),
        # Real FuseSoC setup and Icarus startup are substantially slower on
        # Windows runners than the lightweight adapter tests around this one.
        options=SimulationOptions(timeout_ms=30_000),
    )
    with patch.object(
        fusesoc_registry,
        "resolve_target_handle",
        side_effect=lambda *args, **kwargs: real_resolve(
            *args,
            **{**kwargs, "fusesoc_cmd": fusesoc_cmd},
        ),
    ):
        return execution.run(handle, NamedTests(("dhry",)))


def _run_real_icarus_twice(
    project: Path, handle: TargetHandle
) -> tuple[SimulationTargetOutcome, ...]:
    real_resolve = fusesoc_registry.resolve_target_handle
    fusesoc_cmd = (
        list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        if shutil.which("fusesoc")
        else [sys.executable, "-c", "from fusesoc.main import main; main()"]
    )
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project), options=SimulationOptions(timeout_ms=30_000)
    )
    with patch.object(
        fusesoc_registry,
        "resolve_target_handle",
        side_effect=lambda *args, **kwargs: real_resolve(
            *args, **{**kwargs, "fusesoc_cmd": fusesoc_cmd}
        ),
    ):
        return tuple(execution.run(handle, NamedTests(("dhry",))) for _ in range(2))


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
    return subprocess.run(
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
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
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
    outcome = _run_real_icarus(project, handle)

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
    assert not (project / "firmware.hex").is_symlink()


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
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    reset_cache()

    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    previous_project_dir = os.environ.get("BOOLEY_PROJECT_DIR")
    try:
        for outcome in _run_real_icarus_twice(project, handle):
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
        if previous_project_dir is None:
            monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        else:
            monkeypatch.setenv("BOOLEY_PROJECT_DIR", previous_project_dir)
        reset_cache()


def test_two_targets_never_launch_stale_image_after_equal_mtime_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, source = _write_stale_compiler_fixture(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    catalog = TargetCatalog.build(project)
    handles = tuple(catalog.select(name, for_flow="sim") for name in ("sim_a", "sim_b"))
    observed: list[str] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        result = base_invoke(command, timeout=timeout)
        if "booley.flows.sim.backends.icarus" in command[-1]:
            observed.append(result.stdout)
        return result

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    for handle in handles:
        assert execution.run(handle, NamedTests(("smoke",))).passed
    assert len(observed) == 2 and all("OLD" in item for item in observed)

    old_stat = source.stat()
    source.write_text("module tb; // NEW\nendmodule\n", encoding="utf-8")
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    observed.clear()
    for handle in handles:
        assert execution.run(handle, NamedTests(("smoke",))).passed
    assert len(observed) == 2 and all("NEW" in item and "OLD" not in item for item in observed)


@pytest.mark.skipif(os.name == "nt", reason="Icarus reuse closure runs in the Linux Sandbox")
def test_matching_closed_icarus_inputs_reuse_verified_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    commands: list[list[str]] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        commands.append(command)
        return base_invoke(command, timeout=timeout)

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    first = execution.run(handle, NamedTests(("first",)))
    second = execution.run(handle, NamedTests(("second",)))
    assert first.passed, first.infrastructure_failure
    assert second.passed, (second.builds, second.tests, second.infrastructure_failure)
    assert sum("BOOLEY_BUILD_STAGE" in command[-1] for command in commands) == 1
    assert second.builds[0].ran is False
    assert second.builds[0].cache_decision.startswith("hit;")


def test_changed_input_with_failed_rebuild_never_launches_prior_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, source = _write_stale_compiler_fixture(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    observed: list[str] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        result = base_invoke(command, timeout=timeout)
        if "booley.flows.sim.backends.icarus" in command[-1]:
            observed.append(result.stdout)
        return result

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    assert execution.run(handle, NamedTests(("smoke",))).passed
    old_stat = source.stat()
    source.write_text("module tb; // BAD\nendmodule\n", encoding="utf-8")
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    outcome = execution.run(handle, NamedTests(("smoke",)))
    assert not outcome.passed
    assert len(observed) == 1


@pytest.mark.skipif(os.name == "nt", reason="Icarus reuse closure runs in the Linux Sandbox")
@pytest.mark.parametrize("tamper", ["image", "pointer"])
def test_corrupt_cache_forces_fresh_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    commands: list[list[str]] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        commands.append(command)
        return base_invoke(command, timeout=timeout)

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    assert execution.run(handle, NamedTests(("smoke",))).passed
    slot = simulation_build_slot(handle)
    pointer = json.loads((slot / "current.json").read_text(encoding="utf-8"))
    if tamper == "image":
        build_root = slot / "g" / pointer["generation"] / pointer["build_root"]
        image = next(build_root.glob("*.scr")).with_suffix("")
        image.write_text("[SIM_RESULT] PASSED\nSTALE\n", encoding="utf-8")
    else:
        pointer["build_root"] = "../outside"
        (slot / "current.json").write_text(json.dumps(pointer), encoding="utf-8")
    assert execution.run(handle, NamedTests(("smoke",))).passed
    assert sum("BOOLEY_BUILD_STAGE" in command[-1] for command in commands) == 2


def test_missing_new_image_is_build_infrastructure_and_never_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    launched = False
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        nonlocal launched
        result = base_invoke(command, timeout=timeout)
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            for script in (project / ".booley_project" / ".runtime").rglob("*.scr"):
                script.with_suffix("").unlink(missing_ok=True)
        if "booley.flows.sim.backends.icarus" in command[-1]:
            launched = True
        return result

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    outcome = execution.run(handle, NamedTests(("smoke",)))
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "build"
    assert launched is False


def test_each_pre_sim_hook_runs_in_its_launched_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    (project / ".booley_project" / "booley.toml").write_text(
        "[flows.sim]\n"
        'pre_run_commands = ["printf %s \\"$BOOLEY_TEST_NAME\\" > '
        '\\"$BOOLEY_BUILD_ROOT/hook.txt\\""]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    observed: list[str] = []
    builds = 0
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        nonlocal builds
        result = base_invoke(command, timeout=timeout)
        builds += "BOOLEY_BUILD_STAGE" in command[-1]
        if "booley.flows.sim.backends.icarus" in command[-1]:
            observed.append(result.stdout)
        return result

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    outcome = execution.run(handle, NamedTests(("a", "b")))
    assert outcome.passed
    assert builds == 2
    assert "HOOK=a" in observed[0]
    assert "HOOK=b" in observed[1]


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
    with patch("booley.flows.sim.trace_session._bwave_cache_root", return_value=cache_root):
        session = TraceSession(trace_path.parent)
        if trace_path.suffix == ".vcd":
            session.postprocess(trace_path)
            trace_path = session.work_bwave_path
        inspection = session.inspect(trace_path)
    assert inspection.usable
    assert inspection.artifact is not None
    assert inspection.artifact.signal_count > 0
    assert inspection.artifact.total_ticks > 0


@pytest.mark.parametrize(
    "queryable",
    [False, pytest.param(True, marks=pytest.mark.native_bwave)],
    ids=["execution", "queryable"],
)
def test_icarus_trace_reaches_execution_on_first_and_repeat_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queryable: bool
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    native_bwave = native_bwave_binary()
    if queryable and native_bwave is None:
        pytest.skip("native B-Wave binary is required to verify a queryable Trace Artifact")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    state = project / ".booley_project"
    cores = state / "cores"
    cores.mkdir()
    (project / "multi.core").rename(cores / "multi.core")
    (state / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n",
        encoding="utf-8",
    )
    (state / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    _write_fake_trace_tools(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    commands: list[list[str]] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        commands.append(command)
        return base_invoke(command, timeout=timeout)

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(trace=True))
    for _ in range(2):
        outcome = execution.run(handle, NamedTests(("smoke",)))
        assert outcome.passed
        if queryable:
            _assert_queryable_trace(outcome, tmp_path / "bwave-cache")
        else:
            assert any(Path(artifact.path).suffix == ".vcd" for artifact in outcome.artifacts)
        assert any("BOOLEY_BUILD_STAGE" in command[-1] for command in commands)
        assert any("booley.flows.sim.backends.icarus" in command[-1] for command in commands)
        commands.clear()


def test_hook_editing_project_source_after_setup_cannot_launch_staged_old_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    (project / ".booley_project" / "booley.toml").write_text(
        '[flows.sim]\npre_run_commands = ["sed -i s/OLD/NEW/ tb.sv"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    commands: list[list[str]] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        commands.append(command)
        return base_invoke(command, timeout=timeout)

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    outcome = execution.run(handle, NamedTests(("smoke",)))
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "build"
    assert "changed during setup or Pre-Sim Commands" in outcome.infrastructure_failure.detail
    assert not commands


def test_hook_editing_staged_source_cannot_launch_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    (project / ".booley_project" / "booley.toml").write_text(
        '[flows.sim]\npre_run_commands = ["printf MUTATED >> '
        '\\"$BOOLEY_BUILD_ROOT/src/acme_lib_multi_1/tb.sv\\""]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    invoke = _subprocess_invoker(project)
    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    outcome = execution.run(handle, NamedTests(("smoke",)))

    assert outcome.infrastructure_failure is not None
    assert "build input changed during Pre-Sim Commands" in outcome.infrastructure_failure.detail


def test_hook_editing_authored_core_cannot_launch_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    (project / ".booley_project" / "booley.toml").write_text(
        "[flows.sim]\npre_run_commands = [\"printf '\\n# mutation\\n' >> multi.core\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    calls: list[list[str]] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        calls.append(command)
        return base_invoke(command, timeout=timeout)

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    outcome = execution.run(handle, NamedTests(("smoke",)))

    assert outcome.infrastructure_failure is not None
    assert "compile inputs changed during setup or Pre-Sim Commands" in (
        outcome.infrastructure_failure.detail
    )
    assert not calls


def test_hook_editing_generated_isolated_core_cannot_launch_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    state = project / ".booley_project"
    cores = state / "cores"
    cores.mkdir()
    (project / "multi.core").rename(cores / "multi.core")
    (state / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n"
        "[flows.sim]\npre_run_commands = ["
        "\"printf '\\n# mutation\\n' >> "
        '.booley_project/tmp/fusesoc-isolated-cores/booley-isolated-multi.core"]\n',
        encoding="utf-8",
    )
    (state / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    calls: list[list[str]] = []
    base_invoke = _subprocess_invoker(project)

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        calls.append(command)
        return base_invoke(command, timeout=timeout)

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    outcome = execution.run(handle, NamedTests(("smoke",)))

    assert outcome.infrastructure_failure is not None
    assert (
        "compile inputs changed during Pre-Sim Commands" in outcome.infrastructure_failure.detail
    )
    assert not calls


def test_generation_allocation_failure_is_typed_infrastructure(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions(trace=True))

    with (
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch(
            "booley.flows.sim.execution.engine.SimulationBuildSession.new_generation",
            side_effect=SimulationBuildSlotError("busy"),
        ),
    ):
        outcome = execution.run(handle, NamedTests(("smoke",)))

    assert outcome.verdict == "error"
    assert outcome.tests == ()
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "build"
    assert "busy" in outcome.infrastructure_failure.detail


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
    with (
        patch(
            "booley.flows.sim.execution.engine.work_root_for",
            return_value=work_root,
        ) as work_root_for,
        patch(
            "booley.flows.sim.execution.engine.preview_generation_root",
            return_value=work_root,
        ) as preview_root,
        patch.object(execution, "_reset_build_root") as reset,
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
    ):
        execution._prepare_build(handle)
        execution._preview_group(handle, _inspection(cocotb=False), ("smoke",), False)

    assert [call.kwargs["variant"] for call in work_root_for.call_args_list] == [expected]
    preview_root.assert_called_once_with(handle, expected)
    assert reset.call_args.args[0] == work_root
    assert reset.call_args.args[1].variant == expected
    assert prepare.call_args.kwargs["variant"] == expected
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


def test_legacy_selector_root_is_preserved_but_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project, _ = _write_stale_compiler_fixture(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    handle = TargetCatalog.build(project).select("sim_a", for_flow="sim")
    legacy = work_root_for(project, "sim", handle.selector)
    legacy.mkdir(parents=True)
    sentinel = legacy / "old-image"
    sentinel.write_text("stale\n", encoding="utf-8")

    execution = SimulationExecution(
        invoke=_subprocess_invoker(project), options=SimulationOptions(timeout_ms=5000)
    )
    assert execution.run(handle, NamedTests(("smoke",))).passed
    assert sentinel.read_text(encoding="utf-8") == "stale\n"
    assert tuple((simulation_build_slot(handle) / "g").iterdir())


@pytest.mark.parametrize(
    "evidence, expected_verdict, expected_error",
    [
        (PreSimEvidence(("slow",), ("smoke",), "timed_out", 1.0, "expired"), "fail", None),
        (
            PreSimEvidence(
                ("missing-tool",),
                ("smoke",),
                "spawn_error",
                0.1,
                "bash: line 1: missing-tool: command not found",
            ),
            "error",
            "missing-tool",
        ),
    ],
)
def test_pre_sim_stage_preserves_elaboration_and_infrastructure_classes(
    tmp_path: Path,
    evidence: PreSimEvidence,
    expected_verdict: str,
    expected_error: str | None,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    with (
        patch.object(
            execution,
            "_run_groups_with_session",
            side_effect=lambda current_handle, groups: [
                execution._run_group(current_handle, group) for group in groups
            ],
        ),
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch("booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared),
        patch.object(execution, "_run_pre_sim", return_value=evidence),
    ):
        outcome = execution.run(handle, NamedTests(("smoke",)))

    assert outcome.verdict == expected_verdict
    if expected_error is None:
        assert outcome.tests[0].verdict == "elab_error"
        assert outcome.tests[0].timed_out is False
    else:
        assert outcome.infrastructure_failure is not None
        assert outcome.infrastructure_failure.missing_executable == expected_error


def test_unexpected_execution_defect_propagates(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    execution._run_group = MagicMock(side_effect=RuntimeError("programmer defect"))

    with (
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        pytest.raises(RuntimeError, match="programmer defect"),
    ):
        execution.run(handle, NamedTests(("smoke",)))


@pytest.mark.parametrize("cocotb, expected", [(False, [("a",), ("b",)]), (True, [("a", "b")])])
def test_run_and_preview_share_the_same_work_grouping(
    tmp_path: Path,
    cocotb: bool,
    expected: list[tuple[str, ...]],
) -> None:
    handle = _handle(tmp_path)
    outcome = SimulationTargetOutcome(
        target="sim",
        target_identity=handle.identity,
        toplevel="tb_demo",
        eda_tool="icarus",
        passed=True,
        verdict="pass",
        elapsed_s=0.0,
        tests=(SimulationTestOutcome("a", "pass", True),),
    )
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    execution._run_group = MagicMock(return_value=outcome)
    execution._preview_group = MagicMock(return_value=("sh", "-c", "preview"))

    with patch(
        "booley.flows.sim.execution.engine.TargetCatalog.build",
        return_value=_inspection(cocotb=cocotb),
    ):
        execution.run(handle, NamedTests(("a", "b")))
        execution.preview(handle, NamedTests(("a", "b")))

    assert [call.args[1] for call in execution._run_group.call_args_list] == expected
    assert [call.args[2] for call in execution._preview_group.call_args_list] == expected


def test_doctor_bad_preview_uses_placeholder_without_filesystem_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    handle = _handle(tmp_path)
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    work = _preview_work(
        execution,
        handle,
        _inspection(cocotb=False),
        (),
        False,
        ".booley_project/.runtime/edalize/sim/slot/g/<generation>/build",
    )

    assert work.run_cwd == "<attempt>"
    assert not tuple(tmp_path.rglob(".booley-doctor-run-cwd-*"))


def test_preview_resolves_configuration_from_each_handle_root(tmp_path: Path) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for root, name, selector in (
        (current, "now", "+test={name}"),
        (baseline, "then", "+case={index}"),
    ):
        project = root / ".booley_project"
        project.mkdir(parents=True)
        (project / "tests.toml").write_text(
            f'[sim]\ntests = ["{name}"]\nselect = "{selector}"\n',
            encoding="utf-8",
        )

    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    with (
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch(
            "booley.flows.sim.execution.engine.fusesoc_registry.setup_command_for_handle",
            return_value=["setup"],
        ),
    ):
        current_preview = execution.preview(_handle(current), NamedTests(("now",)))
        baseline_preview = execution.preview(_handle(baseline), NamedTests(("then",)))

    assert "--plusarg=test=now" in current_preview.commands[0][-1]
    assert "--plusarg=case=0" in baseline_preview.commands[0][-1]


@pytest.mark.parametrize("fresh, expected", [(True, "pass"), (False, "inconclusive")])
def test_trace_evidence_must_be_fresh_for_this_attempt(
    tmp_path: Path,
    fresh: bool,
    expected: str,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text('[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8")
    run_cwd = tmp_path / "run"
    run_cwd.mkdir()
    trace = run_cwd / "wave.fst"
    trace.write_bytes(b"old")
    outcome = _run_execution(
        handle,
        prepared,
        _passing_trace_invoker(handle, prepared, trace, fresh=fresh),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert outcome.verdict == expected
    traces = [artifact for artifact in outcome.artifacts if artifact.kind == "trace"]
    assert bool(traces) is fresh


@pytest.mark.parametrize(
    "trace_config, relative_path, expected",
    [
        ("", "undeclared/wave.fst", "inconclusive"),
        ('trace_files = ["../declared/*.fst"]\n', "declared/wave.fst", "pass"),
    ],
)
def test_trace_authority_is_limited_to_runtime_roots_and_declared_globs(
    tmp_path: Path,
    trace_config: str,
    relative_path: str,
    expected: str,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text(
        f'[flows.sim]\nrun_cwd = "run"\n{trace_config}', encoding="utf-8"
    )
    (tmp_path / "run").mkdir()
    trace = tmp_path / relative_path
    trace.parent.mkdir()
    outcome = _run_execution(
        handle,
        prepared,
        _passing_trace_invoker(handle, prepared, trace, fresh=True),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert outcome.verdict == expected


def test_declared_trace_freshness_uses_pre_dispatch_identity(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "run"\ntrace_files = ["../declared/*.fst"]\n',
        encoding="utf-8",
    )
    (tmp_path / "run").mkdir()
    trace = tmp_path / "declared" / "wave.fst"
    trace.parent.mkdir()

    outcome = _run_execution(
        handle,
        prepared,
        _passing_trace_invoker(handle, prepared, trace, fresh=True, modified_ns=1),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert outcome.verdict == "pass"
    assert any(artifact.kind == "trace" for artifact in outcome.artifacts)


def test_new_runtime_trace_accepts_filesystem_clock_skew(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text('[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8")
    run_cwd = tmp_path / "run"
    run_cwd.mkdir()
    trace = run_cwd / "wave.fst"

    outcome = _run_execution(
        handle,
        prepared,
        _passing_trace_invoker(handle, prepared, trace, fresh=True, modified_ns=1),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert outcome.verdict == "pass"


def test_unchanged_declared_trace_is_stale(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "run"\ntrace_files = ["../declared/*.fst"]\n',
        encoding="utf-8",
    )
    (tmp_path / "run").mkdir()
    trace = tmp_path / "declared" / "wave.fst"
    trace.parent.mkdir()
    trace.write_bytes(b"stale waveform")

    outcome = _run_execution(
        handle,
        prepared,
        _passing_trace_invoker(
            handle,
            prepared,
            trace,
            fresh=False,
            transport_modified_ns=1,
        ),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert outcome.verdict == "inconclusive"
    assert all(artifact.kind != "trace" for artifact in outcome.artifacts)


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
    result = prepared.build_root / "result.json"
    result.write_text("stale", encoding="utf-8")

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

    outcome = _run_execution(
        handle,
        prepared,
        lambda _command, timeout: SubprocessResult(
            returncode=0,
            stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n",
        ),
        ("smoke",),
        cocotb=False,
    )

    assert outcome.verdict == "error"
    failure = outcome.infrastructure_failure
    assert failure is not None and "stale" in failure.detail


def test_stdout_cannot_forge_trace_without_authenticated_evidence(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text('[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8")
    run_cwd = tmp_path / "run"
    run_cwd.mkdir()
    trace = run_cwd / "forged.fst"

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        trace.write_bytes(b"fresh waveform")
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
            stdout=f"BOOLEY_BUILD_STAGE token=abc123 rc=0\nTRACE_OK: {trace}\n",
        )

    outcome = _run_execution(
        handle,
        prepared,
        invoke,
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(trace=True),
        trace_mode=TraceMode.NATIVE_FST,
    )

    assert outcome.verdict == "inconclusive"
    assert all(artifact.kind != "trace" for artifact in outcome.artifacts)


def _assert_attempt_config_freezing(
    first: SimulationTargetOutcome,
    second: SimulationTargetOutcome,
    run: _ChangingTraceRun,
) -> None:
    first_snapshot = first.tests[0].workload_snapshot
    second_snapshot = second.tests[0].workload_snapshot
    assert first_snapshot is not None and second_snapshot is not None
    assert first.verdict == second.verdict == "pass"
    assert "../first/*.fst" in run.commands[0]
    assert "../second/*.fst" in run.commands[1]
    assert "--run-cwd run" in run.commands[0]
    assert "--run-cwd other-run" in run.commands[1]
    assert "--timeout 1" in run.commands[0]
    assert "--timeout 2" in run.commands[1]
    assert run.pre_sim_timeouts == run.invocation_timeouts == [91, 92]
    assert first_snapshot["controls"]["run_cwd"] == "run"
    assert second_snapshot["controls"]["run_cwd"] == "other-run"
    assert first_snapshot["controls"]["pre_run_commands"] == ["old"]
    assert second_snapshot["controls"]["pre_run_commands"] == ["new"]
    assert first_snapshot["controls"]["cycle_sentinels"] == ["[OLD]"]
    assert second_snapshot["controls"]["cycle_sentinels"] == ["[NEW]"]
    assert first_snapshot["inputs"][0]["present"] is True
    assert first_snapshot["inputs"][0]["bytes"] == len(f"generated{os.linesep}".encode())
    assert first.tests[0].cycles == 17
    assert second.tests[0].cycles == 23


def test_trace_declarations_are_frozen_for_each_attempt(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    prepared = replace(
        prepared,
        resolved=replace(
            prepared.resolved,
            files=(ResolvedFile("firmware.hex", "user"),),
        ),
    )
    project = tmp_path / ".booley_project"
    project.mkdir()
    config = project / "booley.toml"
    config.write_text(
        "[flows.sim]\n"
        'run_cwd = "run"\n'
        'trace_files = ["../first/*.fst"]\n'
        "timeout_ms = 1000\n"
        'cycle_sentinels = ["[OLD]"]\n'
        'pre_run_commands = ["old"]\n',
        encoding="utf-8",
    )
    (tmp_path / "run").mkdir()
    (tmp_path / "other-run").mkdir()
    traces = (tmp_path / "first" / "wave.fst", tmp_path / "second" / "wave.fst")
    run = _ChangingTraceRun(handle, prepared, config, traces)
    execution = SimulationExecution(invoke=run.invoke, options=SimulationOptions(trace=True))
    with (
        patch.object(
            execution,
            "_run_groups_with_session",
            side_effect=lambda current_handle, groups: [
                execution._run_group(current_handle, group) for group in groups
            ],
        ),
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch.object(execution, "_prepare_build", return_value=(prepared, TraceMode.NATIVE_FST)),
        patch.object(execution, "_run_pre_sim", side_effect=run.pre_sim),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123"),
    ):
        first = execution.run(handle, NamedTests(("smoke",)))
        second = execution.run(handle, NamedTests(("smoke",)))

    _assert_attempt_config_freezing(first, second, run)


@pytest.mark.parametrize(
    ("eda_tool", "artifact_names", "adapter_flag"),
    [
        ("icarus", ("demo.scr", "demo"), "--build-dir"),
        ("verilator", ("Vtb_demo",), "--bin-dir"),
    ],
)
def test_ordinary_group_builds_before_launching_supplied_snapshot(
    tmp_path: Path,
    eda_tool: str,
    artifact_names: tuple[str, ...],
    adapter_flag: str,
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
    snapshot = tmp_path / "attempt" / "snapshot"
    snapshot.mkdir(parents=True)
    for artifact in artifacts:
        (snapshot / artifact.name).write_bytes(artifact.read_bytes())
    run_cwd = tmp_path / "run"
    run_cwd.mkdir()
    commands: list[list[str]] = []
    invoke = _snapshot_invoke(commands, eda_tool, handle, snapshot)
    execution = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=5000))
    with (
        patch.object(execution, "_prepare_build", return_value=(prepared, TraceMode.VCD_FIFO)),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123"),
        patch.object(SimulationBuildSession, "capture_inputs", return_value={}),
        patch.object(SimulationBuildSession, "authorize_fresh_image", return_value=artifacts),
    ):
        with execution.ordinary_group(handle, ("smoke",)) as group:
            build = group.compile()
            assert build.passed
            assert group.artifact_paths == artifacts
            assert len(commands) == 1
        outcome = group.launch_snapshot(snapshot, run_cwd)

    assert outcome.passed
    assert len(commands) == 2
    launch = commands[1][-1]
    assert adapter_flag in launch
    assert str(snapshot) in launch
    assert str(run_cwd) in launch
    assert "--work-dir" in launch
    assert str(snapshot.parent / "execution-evidence") in launch
    assert str(prepared.build_root) not in launch


def _snapshot_invoke(commands, eda_tool, handle, snapshot):
    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        commands.append(command)
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")
        identity = AdapterTransportIdentity(
            eda_tool,
            "abc123",
            handle.identity,
            ("smoke",),
            snapshot.parent / "execution-evidence" / "adapter-abc123.json",
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


def test_ordinary_group_refuses_snapshot_launch_until_lease_released(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    with (
        patch.object(execution, "_prepare_build", return_value=(prepared, TraceMode.VCD_FIFO)),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123"),
        execution.ordinary_group(handle, ("smoke",)) as group,
        pytest.raises(SimulationBuildSlotError, match="leave ordinary_group"),
    ):
        group.launch_snapshot(tmp_path / "snapshot", tmp_path / "run")
