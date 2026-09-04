"""Production-interface tests for the deep Simulation execution boundary."""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTraceResult,
    AdapterTransportIdentity,
    partial_result_identity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild, SimulationBuildPreparationError
from booley.flows.sim.execution import (
    DefaultSelection,
    NamedTests,
    PreRunEvidence,
    SimulationExecution,
    SimulationOptions,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.targets.target import TargetHandle


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


def _inspection(*, cocotb: bool) -> SimpleNamespace:
    return SimpleNamespace(
        toplevel="tb_demo",
        eda_tool="icarus",
        parameters={},
        flow_options={"cocotb_module": "test_demo"} if cocotb else {},
    )


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
            patch(
                "booley.flows.sim.execution.engine.inspect_target",
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
        self.pre_run_timeouts: list[int] = []
        self.invocation_timeouts: list[int] = []

    def pre_run(self, _handle, attempt) -> PreRunEvidence:
        self.pre_run_timeouts.append(attempt.wrapper_timeout_s)
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
        return PreRunEvidence((), ("smoke",), "passed", 0.0, "")

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


def test_trace_reset_failure_is_not_recorded_as_success(tmp_path: Path) -> None:
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions(trace=True))
    build_root = tmp_path / "build"
    build_root.mkdir()

    with (
        patch("booley.flows.sim.execution.engine.shutil.rmtree", side_effect=OSError("busy")),
        pytest.raises(
            SimulationBuildPreparationError,
            match="could not reset traced Simulation build root",
        ),
    ):
        execution._reset_trace_root(build_root)

    assert execution._reset_trace_roots == set()


@pytest.mark.parametrize(
    "evidence, expected_verdict, expected_error",
    [
        (PreRunEvidence(("slow",), ("smoke",), "timed_out", 1.0, "expired"), "fail", None),
        (
            PreRunEvidence(
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
def test_pre_run_stage_preserves_elaboration_and_infrastructure_classes(
    tmp_path: Path,
    evidence: PreRunEvidence,
    expected_verdict: str,
    expected_error: str | None,
) -> None:
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    with (
        patch(
            "booley.flows.sim.execution.engine.inspect_target",
            return_value=_inspection(cocotb=False),
        ),
        patch("booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared),
        patch.object(execution, "_run_pre_run", return_value=evidence),
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
            "booley.flows.sim.execution.engine.inspect_target",
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
        "booley.flows.sim.execution.engine.inspect_target",
        return_value=_inspection(cocotb=cocotb),
    ):
        execution.run(handle, NamedTests(("a", "b")))
        execution.preview(handle, NamedTests(("a", "b")))

    assert [call.args[1] for call in execution._run_group.call_args_list] == expected
    assert [call.args[2] for call in execution._preview_group.call_args_list] == expected


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
            "booley.flows.sim.execution.engine.inspect_target",
            return_value=_inspection(cocotb=False),
        ),
        patch(
            "booley.flows.sim.execution.engine.fusesoc_registry.setup_command",
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
    assert run.pre_run_timeouts == run.invocation_timeouts == [91, 92]
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
        patch(
            "booley.flows.sim.execution.engine.inspect_target",
            return_value=_inspection(cocotb=False),
        ),
        patch.object(execution, "_prepare_build", return_value=(prepared, TraceMode.NATIVE_FST)),
        patch.object(execution, "_run_pre_run", side_effect=run.pre_run),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123"),
    ):
        first = execution.run(handle, NamedTests(("smoke",)))
        second = execution.run(handle, NamedTests(("smoke",)))

    _assert_attempt_config_freezing(first, second, run)
