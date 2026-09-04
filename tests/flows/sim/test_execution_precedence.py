"""Build, wrapper, and adapter-result precedence through the public boundary."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.targets.target import TargetHandle

_TOKEN = "abc123"


def _handle(root: Path) -> TargetHandle:
    return cast(
        TargetHandle,
        SimpleNamespace(
            project_root=root.resolve(),
            selector="sim",
            identity="acme:lib:core:1#sim",
            vlnv="acme:lib:core:1",
        ),
    )


def _prepared(handle: TargetHandle) -> PreparedSimulationBuild:
    build_root = handle.project_root / "build" / "sim"
    build_root.mkdir(parents=True)
    resolved = ResolvedTarget(
        name="sim",
        vlnv=handle.vlnv,
        toplevel="tb_core",
        eda_tool="icarus",
        files=(),
        parameters={},
        build_root=build_root,
        edam_path=build_root / "core.eda.yml",
        flow_options={},
    )
    return PreparedSimulationBuild(
        "sim",
        handle.identity,
        resolved,
        build_root,
        build_root,
        "icarus",
        "tb_core",
        ("make",),
    )


def _run(
    tmp_path: Path,
    process: SubprocessResult,
    result: AdapterResult | None = None,
):
    handle = _handle(tmp_path)
    prepared = _prepared(handle)

    def invoke(_command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        if result is not None:
            identity = AdapterTransportIdentity(
                "icarus",
                _TOKEN,
                handle.identity,
                ("smoke",),
                prepared.build_root / f".booley-adapter-{_TOKEN}.json",
            )
            write_adapter_result(identity, result)
        return process

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions())
    inspection = SimpleNamespace(toplevel="tb_core", eda_tool="icarus", flow_options={})
    with (
        patch("booley.flows.sim.execution.engine.inspect_target", return_value=inspection),
        patch("booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value=_TOKEN),
    ):
        return execution.run(handle, NamedTests(("smoke",)))


def _passing_result() -> AdapterResult:
    return AdapterResult(
        True,
        False,
        0,
        ("smoke",),
        test_results=(AdapterTestResult("smoke", "pass"),),
    )


def test_normal_completion_requires_terminal_transport(tmp_path: Path) -> None:
    process = SubprocessResult(
        returncode=0,
        stdout=f"BOOLEY_BUILD_STAGE token={_TOKEN} rc=0\n",
    )

    outcome = _run(tmp_path, process)

    assert outcome.verdict == "error"
    assert outcome.infrastructure_failure is not None
    assert outcome.infrastructure_failure.kind == "adapter_protocol"


def test_timeout_precedes_missing_terminal_transport(tmp_path: Path) -> None:
    process = SubprocessResult(
        returncode=-9,
        stdout=f"BOOLEY_BUILD_STAGE token={_TOKEN} rc=0\n",
        timed_out=True,
    )

    outcome = _run(tmp_path, process)

    assert outcome.verdict == "fail"
    assert outcome.tests[0].verdict == "timeout"
    assert outcome.infrastructure_failure is None


def test_design_rejection_does_not_require_adapter_transport(tmp_path: Path) -> None:
    process = SubprocessResult(
        returncode=1,
        stdout=f"error: syntax error\nBOOLEY_BUILD_STAGE token={_TOKEN} rc=1\n",
    )

    outcome = _run(tmp_path, process)

    assert outcome.verdict == "fail"
    assert outcome.tests[0].verdict == "elab_error"
    assert outcome.infrastructure_failure is None


def test_adapter_pass_cannot_override_nonzero_process_exit(tmp_path: Path) -> None:
    process = SubprocessResult(
        returncode=1,
        stdout=f"BOOLEY_BUILD_STAGE token={_TOKEN} rc=0\n",
    )

    outcome = _run(tmp_path, process, _passing_result())

    assert outcome.verdict == "error"
    assert outcome.infrastructure_failure is not None
    assert "contradicts" in outcome.infrastructure_failure.detail
