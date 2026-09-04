"""Pre-Run Commands tests for the Simulation execution boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

from booley.flows.sim.execution.pre_run import run_pre_run_commands
from booley.targets.target import TargetHandle


def _handle(root: Path) -> TargetHandle:
    project = root / ".booley_project"
    project.mkdir(parents=True)
    (project / "booley.toml").write_text(
        "[flows.sim]\n"
        'pre_run_commands = ["make -C tests prep CASE=$BOOLEY_TEST_NAME"]\n'
        'run_cwd = "util/sim"\n',
        encoding="utf-8",
    )
    return cast(
        TargetHandle,
        SimpleNamespace(project_root=root.resolve(), selector="lite"),
    )


def _completed(returncode: int = 0, *, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def test_single_test_environment_is_project_scoped_and_reserved_values_win(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    build_root = tmp_path / "build" / "lite"
    run = MagicMock(return_value=_completed())

    with patch("booley.flows.sim.execution.pre_run.subprocess.run", run):
        evidence = run_pre_run_commands(
            handle,
            test_names=("smoke",),
            build_root=build_root,
            eda_tool="verilator",
            timeout_s=5,
            simulator_environment={"FLAVOR": "vanilla", "BOOLEY_TARGET": "hijack"},
        )

    assert evidence is not None and evidence.status == "passed"
    environment = run.call_args.kwargs["env"]
    assert environment["FLAVOR"] == "vanilla"
    assert environment["BOOLEY_TARGET"] == "lite"
    assert environment["BOOLEY_TEST_NAME"] == "smoke"
    assert environment["BOOLEY_TEST_NAMES"] == "smoke"
    assert environment["BOOLEY_BUILD_ROOT"] == str(build_root)
    assert environment["BOOLEY_PROJECT_ROOT"] == str(tmp_path)
    assert environment["BOOLEY_RUN_CWD"] == str(tmp_path / "util" / "sim")
    assert environment["BOOLEY_SIM_EDA_TOOL"] == "verilator"
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert run.call_args.kwargs["timeout"] == 5


def test_batch_environment_has_selected_set_without_single_test_name(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    run = MagicMock(return_value=_completed())

    with patch("booley.flows.sim.execution.pre_run.subprocess.run", run):
        evidence = run_pre_run_commands(
            handle,
            test_names=("reset", "count"),
            build_root=tmp_path / "build" / "lite",
            eda_tool="icarus",
            timeout_s=7,
        )

    assert evidence is not None and evidence.test_names == ("reset", "count")
    environment = run.call_args.kwargs["env"]
    assert "BOOLEY_TEST_NAME" not in environment
    assert environment["BOOLEY_TEST_NAMES"] == "reset count"


def test_explicit_run_cwd_overrides_live_project_configuration(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    run = MagicMock(return_value=_completed())

    with patch("booley.flows.sim.execution.pre_run.subprocess.run", run):
        evidence = run_pre_run_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build" / "lite",
            eda_tool="icarus",
            timeout_s=5,
            run_cwd="frozen/sim",
        )

    assert evidence is not None and evidence.status == "passed"
    assert run.call_args.kwargs["env"]["BOOLEY_RUN_CWD"] == str(tmp_path / "frozen" / "sim")


def test_nonzero_pre_run_is_a_design_stage_failure(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    with patch(
        "booley.flows.sim.execution.pre_run.subprocess.run",
        return_value=_completed(3, stderr="firmware build failed"),
    ):
        evidence = run_pre_run_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build" / "lite",
            eda_tool="icarus",
            timeout_s=5,
        )

    assert evidence is not None
    assert evidence.status == "failed"
    assert evidence.detail == "firmware build failed"


def test_missing_pre_run_executable_is_a_spawn_error(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    missing = _completed(
        127,
        stderr="/bin/bash: line 2: riscv64-unknown-elf-gcc: command not found",
    )
    with patch("booley.flows.sim.execution.pre_run.subprocess.run", return_value=missing):
        evidence = run_pre_run_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build" / "lite",
            eda_tool="icarus",
            timeout_s=5,
        )

    assert evidence is not None
    assert evidence.status == "spawn_error"
    assert "riscv64-unknown-elf-gcc" in evidence.detail


def test_pre_run_process_spawn_error_is_an_ordinary_failure(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    with patch(
        "booley.flows.sim.execution.pre_run.subprocess.run",
        side_effect=OSError("resource temporarily unavailable"),
    ):
        evidence = run_pre_run_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build" / "lite",
            eda_tool="icarus",
            timeout_s=5,
        )

    assert evidence is not None
    assert evidence.status == "failed"
    assert "resource temporarily unavailable" in evidence.detail


def test_timeout_is_preserved_as_pre_run_evidence(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    with patch(
        "booley.flows.sim.execution.pre_run.subprocess.run",
        side_effect=subprocess.TimeoutExpired("bash", 5),
    ):
        evidence = run_pre_run_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build" / "lite",
            eda_tool="icarus",
            timeout_s=5,
        )

    assert evidence is not None
    assert evidence.status == "timed_out"
