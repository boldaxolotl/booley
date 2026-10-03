"""Build, wrapper, and adapter-result precedence through the public boundary."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.flows.sim.execution.freshness import ArtifactValidationError
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.targets.domain import TargetHandle

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
    inspection.inspect = lambda _handle: inspection
    with (
        patch.object(
            execution,
            "_run_groups_with_session",
            side_effect=lambda current_handle, groups: [
                execution._run_group(current_handle, group) for group in groups
            ],
        ),
        patch("booley.flows.sim.execution.engine.TargetCatalog.build", return_value=inspection),
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

    with pytest.raises(ArtifactValidationError, match="contradicts"):
        _run(tmp_path, process, _passing_result())


@pytest.mark.parametrize("build_rc", [1, 137])
def test_build_failure_cannot_hide_adapter_authentication_error(tmp_path, build_rc) -> None:
    from dataclasses import replace

    writer = write_adapter_result

    def foreign_writer(identity, result):
        writer(replace(identity, target_identity="foreign#sim"), result)

    process = SubprocessResult(
        returncode=build_rc,
        stdout=f"BOOLEY_BUILD_STAGE token={_TOKEN} rc={build_rc}\n",
    )
    with (
        patch(__name__ + ".write_adapter_result", side_effect=foreign_writer),
        pytest.raises(ArtifactValidationError, match="Target"),
    ):
        _run(tmp_path, process, _passing_result())


@pytest.mark.parametrize("passed", [True, False])
def test_cleanup_failure_keeps_authenticated_design_verdict(tmp_path, passed) -> None:
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

    process = SubprocessResult(returncode=0, stdout=f"BOOLEY_BUILD_STAGE token={_TOKEN} rc=0\n")
    with patch.object(Path, "unlink", denied_partial):
        outcome = _run(tmp_path, process, result)
    assert outcome.tests[0].verdict == ("pass" if passed else "fail")
    assert outcome.infrastructure_failure is None
    assert any("artifact_persistence:" in item for item in outcome.diagnostics)
