"""Pre-Sim Commands tests for the Simulation execution boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.targets.domain import TargetHandle
from tests.time_budget import hang_guard_s


def _handle(root: Path) -> TargetHandle:
    project = root / ".booley"
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


class _HookProcess:
    """A spawned hook: the pid is above the kernel limit, so sampling it finds nothing."""

    pid = 4_194_305

    def __init__(
        self,
        returncode: int = 0,
        *,
        stdout: str = "",
        stderr: str = "",
        timeout: subprocess.TimeoutExpired | None = None,
    ) -> None:
        self.returncode = returncode
        self._answers: list[Any] = [timeout] if timeout is not None else []
        self._output = (stdout, stderr)
        self.communicate = MagicMock(side_effect=self._communicate)

    def _communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self._answers:
            raise self._answers.pop()
        return self._output


def _fire(
    handle: TargetHandle,
    process: _HookProcess | None = None,
    *,
    spawn_error: OSError | None = None,
    **overrides: Any,
) -> tuple[Any, MagicMock]:
    """Run the Pre-Sim Commands of *handle* with a mocked ``subprocess.Popen``."""
    spawn = MagicMock(return_value=process or _HookProcess(), side_effect=spawn_error)
    options: dict[str, Any] = {
        "test_names": ("smoke",),
        "build_root": handle.project_root / "build" / "lite",
        "eda_tool": "icarus",
        "timeout_s": 5,
        "resource_flags": (),
        **overrides,
    }
    with (
        patch("booley.flows.sim.execution.pre_sim.subprocess.Popen", spawn),
        patch("booley.flows.sim.execution.pre_sim.kill_process_tree"),
    ):
        return run_pre_sim_commands(handle, **options), spawn


def test_single_test_environment_is_project_scoped_and_reserved_values_win(
    tmp_path: Path,
) -> None:
    handle = _handle(tmp_path)
    build_root = tmp_path / "build" / "lite"

    evidence, run = _fire(
        handle,
        eda_tool="verilator",
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
    assert run.return_value.communicate.call_args.kwargs["timeout"] == 5


def test_batch_environment_has_selected_set_without_single_test_name(tmp_path: Path) -> None:
    evidence, run = _fire(_handle(tmp_path), test_names=("reset", "count"), timeout_s=7)

    assert evidence is not None and evidence.test_names == ("reset", "count")
    environment = run.call_args.kwargs["env"]
    assert "BOOLEY_TEST_NAME" not in environment
    assert environment["BOOLEY_TEST_NAMES"] == "reset count"


def _write_pre_run_commands(root: Path, value: str) -> None:
    """Replace the fixture's ``pre_run_commands`` with the raw TOML *value*."""
    (root / ".booley" / "booley.toml").write_text(
        f"[flows.sim]\npre_run_commands = {value}\n", encoding="utf-8"
    )


@pytest.mark.parametrize(
    "value",
    ['"make -C tests build"', '["make", 3]'],
    ids=["bare-string", "non-string-entry"],
)
def test_invalid_pre_run_commands_spawn_nothing(tmp_path: Path, value: str) -> None:
    """Only a list of strings is a command list; anything else runs nothing."""
    handle = _handle(tmp_path)
    _write_pre_run_commands(tmp_path, value)
    with (
        patch("booley.flows.sim.execution.pre_sim.subprocess.Popen") as spawn,
        pytest.raises(
            ValueError, match=r"\[flows\.sim\]\.pre_run_commands must be a list of strings"
        ),
    ):
        run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build",
            eda_tool="icarus",
            timeout_s=5,
            resource_flags=(),
        )

    spawn.assert_not_called()


@pytest.mark.parametrize("table", ["[flows.sim]\n", "[flows.sim]\npre_run_commands = []\n"])
def test_unset_or_empty_pre_run_commands_run_nothing(tmp_path: Path, table: str) -> None:
    handle = _handle(tmp_path)
    (tmp_path / ".booley" / "booley.toml").write_text(table, encoding="utf-8")

    evidence, run = _fire(handle)

    assert evidence is None
    run.assert_not_called()


def test_uninitialized_checkout_uses_build_cache_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    handle = cast(
        TargetHandle,
        SimpleNamespace(project_root=tmp_path.resolve(), selector="lite"),
    )
    build_root = tmp_path / "build" / "lite"

    evidence, run = _fire(handle, commands=("true",))

    assert evidence is not None and evidence.status == "passed"
    environment = run.call_args.kwargs["env"]
    assert environment["PYTHONPYCACHEPREFIX"] == str(build_root / "python-artifacts" / "bytecode")


def test_explicit_run_cwd_overrides_live_project_configuration(tmp_path: Path) -> None:
    evidence, run = _fire(_handle(tmp_path), run_cwd="frozen/sim")

    assert evidence is not None and evidence.status == "passed"
    assert run.call_args.kwargs["env"]["BOOLEY_RUN_CWD"] == str(tmp_path / "frozen" / "sim")


def test_immutable_simulation_record_hook_runs_from_project_root_without_build_access(
    tmp_path: Path,
) -> None:
    actual_cwd = tmp_path / "attempt-run"
    actual_cwd.mkdir()

    evidence, run = _fire(
        _handle(tmp_path),
        build_root=tmp_path / "private-build",
        run_cwd=str(actual_cwd),
        expose_build_root=False,
    )

    assert evidence is not None and evidence.status == "passed"
    assert run.call_args.kwargs["cwd"] == tmp_path
    environment = run.call_args.kwargs["env"]
    assert environment["BOOLEY_RUN_CWD"] == str(actual_cwd)
    assert "BOOLEY_BUILD_ROOT" not in environment


@pytest.mark.parametrize(
    ("answer", "status", "detail", "exact"),
    [
        pytest.param(
            {"process": _HookProcess(3, stderr="firmware build failed")},
            "failed",
            "firmware build failed",
            True,
            id="nonzero-is-a-design-stage-failure",
        ),
        pytest.param(
            {
                "process": _HookProcess(
                    127, stderr="/bin/bash: line 2: riscv64-unknown-elf-gcc: command not found"
                )
            },
            "spawn_error",
            "riscv64-unknown-elf-gcc",
            False,
            id="missing-executable",
        ),
        pytest.param(
            {"spawn_error": OSError("resource temporarily unavailable")},
            "failed",
            "resource temporarily unavailable",
            False,
            id="process-spawn-error",
        ),
        pytest.param(
            {"process": _HookProcess(-9, timeout=subprocess.TimeoutExpired("bash", 5))},
            "timed_out",
            "",
            False,
            id="timeout",
        ),
    ],
)
def test_pre_sim_process_outcomes_become_evidence(
    tmp_path: Path, answer: dict[str, Any], status: str, detail: str, exact: bool
) -> None:
    evidence, _run = _fire(_handle(tmp_path), **answer)

    assert evidence is not None
    assert evidence.status == status
    assert (evidence.detail == detail) if exact else (detail in evidence.detail)


def _booley_dir_seen_by_pre_sim(root: Path, tmp_path: Path) -> dict[str, str]:
    """Run one Pre-Sim Command for *root* with a mocked subprocess; return its environment."""
    handle = cast(TargetHandle, SimpleNamespace(project_root=root.resolve(), selector="lite"))
    evidence, run = _fire(
        handle, build_root=tmp_path / "build", eda_tool="verilator", commands=("true",)
    )
    assert evidence is not None and evidence.status == "passed"
    return run.call_args.kwargs["env"]


def test_linked_worktree_pre_sim_commands_see_its_own_booley_directory(
    tmp_path: Path, monkeypatch
) -> None:
    from tests.booley_checkouts_support import linked_worktree

    _main, worktree = linked_worktree(tmp_path, monkeypatch, layout="snapshot", runtime="sandbox")

    environment = _booley_dir_seen_by_pre_sim(worktree, tmp_path)

    own = worktree / ".booley"
    assert environment["BOOLEY_DIR"] == str(own)
    assert environment["PYTHONPYCACHEPREFIX"].startswith(
        str(own / ".runtime" / "python-artifacts")
    )


def test_main_checkout_pre_sim_commands_see_the_main_booley_dir(
    tmp_path: Path, monkeypatch
) -> None:
    from tests.booley_checkouts_support import linked_worktree

    main, _worktree = linked_worktree(tmp_path, monkeypatch, layout="snapshot", runtime="sandbox")

    environment = _booley_dir_seen_by_pre_sim(main, tmp_path)

    assert environment["BOOLEY_DIR"] == str((main / ".booley").resolve())


def test_python_and_pytest_caches_stay_under_project_runtime(tmp_path: Path, monkeypatch) -> None:
    from booley.runtime.booley_dir import reset_cache

    handle = _handle(tmp_path)
    helper = tmp_path / "project_helper.py"
    helper.write_text("VALUE = 42\n", encoding="utf-8")
    nested = tmp_path / "test_nested.py"
    nested.write_text("def test_cache_feature():\n    assert True\n", encoding="utf-8")
    for name in (
        "PYTHONPYCACHEPREFIX",
        "PYTHONDONTWRITEBYTECODE",
        "PYTEST_ADDOPTS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    reset_cache()

    evidence = run_pre_sim_commands(
        handle,
        test_names=("smoke",),
        build_root=tmp_path / "build",
        eda_tool="icarus",
        timeout_s=hang_guard_s(),
        resource_flags=(),
        commands=(
            f'"{sys.executable}" -c "import project_helper; assert project_helper.VALUE == 42"',
            f'"{sys.executable}" -m pytest --lf -q "{nested}"',
        ),
    )

    assert evidence is not None and evidence.status == "passed", evidence
    runtime = tmp_path / ".booley" / ".runtime" / "python-artifacts"
    assert any((runtime / "bytecode").rglob("*.pyc"))
    assert any((runtime / "pytest").rglob("*"))
    assert not (tmp_path / "__pycache__").exists()
    assert not list(tmp_path.glob("*.pyc"))
    assert not (tmp_path / ".pytest_cache").exists()


@pytest.mark.parametrize("returncode", [0, 7, -9, 127])
def test_hook_preserves_actual_rc_and_independent_utf8_tails(
    tmp_path: Path, returncode: int
) -> None:
    process = _HookProcess(returncode, stdout="\u03b1" * 10000, stderr="β" * 10000)
    evidence, _run = _fire(_handle(tmp_path), process)
    assert evidence is not None
    assert evidence.returncode == returncode
    assert evidence.stdout_tail == "\u03b1" * 4096
    assert evidence.stderr_tail == "β" * 4096
    assert len(evidence.detail.encode("utf-8")) <= 8192


def test_timeout_partial_byte_streams_preserve_unknown_exit_code(tmp_path: Path) -> None:
    timeout = subprocess.TimeoutExpired("bash", 5, output=b"hook-out\n", stderr=b"hook-err\n")
    evidence, _run = _fire(_handle(tmp_path), _HookProcess(-9, timeout=timeout))
    assert evidence is not None and evidence.status == "timed_out"
    assert evidence.returncode is None
    assert evidence.stdout_tail == "hook-out\n"
    assert evidence.stderr_tail == "hook-err\n"


def test_pre_cancelled_scope_does_not_spawn_or_fabricate_a_firing(tmp_path: Path) -> None:
    from booley.flows.sim.execution.contract import PreSimScopeStoppedError
    from booley.runtime.supervised_execution import (
        SupervisedExecutionScope,
        supervised_execution_scope,
    )

    handle = _handle(tmp_path)
    scope = SupervisedExecutionScope(None, tmp_path / ".booley", lambda: True)
    with (
        supervised_execution_scope(scope),
        patch("booley.flows.sim.execution.pre_sim.subprocess.Popen") as spawn,
        pytest.raises(PreSimScopeStoppedError),
    ):
        run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build",
            eda_tool="icarus",
            timeout_s=5,
            resource_flags=(),
        )
    spawn.assert_not_called()


def test_pre_sim_spawn_failure_records_no_command_start(tmp_path: Path) -> None:
    from tests.flows.test_terminal_progress import observer

    handle = _handle(tmp_path)
    sink = observer(tmp_path, flow="sim")
    with (
        sink.installed(),
        patch(
            "booley.flows.sim.execution.pre_sim.subprocess.Popen",
            side_effect=OSError("resource temporarily unavailable"),
        ),
    ):
        evidence = run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build",
            eda_tool="icarus",
            timeout_s=5,
            resource_flags=(),
        )
    assert evidence is not None and evidence.status == "failed"
    assert "command started" not in sink.stream.getvalue(), (
        "unstarted command must not record a start"
    )
    assert sink.worker is None


def test_actual_hook_normalizes_invalid_utf8_output(tmp_path: Path) -> None:
    handle = _handle(tmp_path)
    evidence = run_pre_sim_commands(
        handle,
        test_names=("smoke",),
        build_root=tmp_path / "build",
        eda_tool="icarus",
        timeout_s=hang_guard_s(),
        resource_flags=(),
        commands=("printf '\\377hook-out\\n'; printf '\\376hook-err\\n' >&2",),
    )
    assert evidence is not None and evidence.returncode == 0
    assert evidence.stdout_tail == "�hook-out\n"
    assert evidence.stderr_tail == "�hook-err\n"


def test_pre_sim_commands_do_not_run_after_the_jobs_oom_kill(tmp_path: Path) -> None:
    """An armed attempt that already had an OOM kill spawns no hook; an unarmed one does."""
    from booley.runtime.endpoint_execution import job_attempt, note_job_oom_kill

    handle = _handle(tmp_path)
    marker = tmp_path / "hook-ran"
    # Portable "touch": the interpreter running the tests creates the marker file.
    touch = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').close()\""

    def run_hook():
        return run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build",
            eda_tool="icarus",
            timeout_s=hang_guard_s(),
            resource_flags=(),
            commands=(touch,),
        )

    with job_attempt(stop_on_oom=True):
        note_job_oom_kill()
        stopped = run_hook()
    assert stopped is not None
    assert stopped.status == "failed"
    assert "OOM-killed" in stopped.detail
    assert not marker.exists()

    # Contrast: the attempt that will not be requeued still runs its hook after a kill.
    with job_attempt(stop_on_oom=False):
        note_job_oom_kill()
        ran = run_hook()
    assert ran is not None and ran.status == "passed", ran
    assert marker.exists()
