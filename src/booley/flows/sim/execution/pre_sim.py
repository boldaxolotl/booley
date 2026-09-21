"""Project-scoped Pre-Sim Commands execution for Simulation work units."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from booley.core.file_lock import active_child_lease_fd
from booley.flows.sim.config import resolve_pre_sim_commands, resolve_run_cwd
from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV
from booley.runtime.platform_paths import (
    bash_bin,
    kill_process_tree,
    popen_new_group_kwargs,
)
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.supervised_execution import current_supervised_execution
from booley.targets.domain import TargetHandle

from .contract import PreSimEvidence
from .failures import find_missing_executable


def run_pre_sim_commands(
    handle: TargetHandle,
    *,
    test_names: tuple[str, ...],
    build_root: Path,
    eda_tool: str,
    timeout_s: int,
    simulator_environment: Mapping[str, str] | None = None,
    commands: tuple[str, ...] | None = None,
    run_cwd: str | None = None,
    working_directory: Path | None = None,
    expose_build_root: bool = True,
) -> PreSimEvidence | None:
    """Run the hook once for a native test or once for a Cocotb batch."""
    root = handle.project_root
    resolved_commands = tuple(resolve_pre_sim_commands(root)) if commands is None else commands
    if not resolved_commands:
        return None
    environment = _pre_sim_environment(
        handle,
        test_names=test_names,
        build_root=build_root,
        eda_tool=eda_tool,
        simulator_environment=simulator_environment,
        run_cwd=run_cwd,
        expose_build_root=expose_build_root,
    )
    return _invoke_pre_sim(
        resolved_commands,
        test_names,
        working_directory or root,
        environment,
        timeout_s,
    )


def _pre_sim_environment(
    handle: TargetHandle,
    *,
    test_names: tuple[str, ...],
    build_root: Path,
    eda_tool: str,
    simulator_environment: Mapping[str, str] | None,
    run_cwd: str | None,
    expose_build_root: bool,
) -> dict[str, str]:
    """Build the Project-scoped environment for one hook firing."""
    root = handle.project_root
    resolved_run_cwd = (root / (run_cwd or resolve_run_cwd(root))).resolve()
    environment = os.environ.copy()
    environment.update(simulator_environment or {})
    environment.update(
        {
            "BOOLEY_TARGET": handle.selector,
            "BOOLEY_TEST_NAMES": " ".join(test_names),
            "BOOLEY_PROJECT_ROOT": str(root),
            "BOOLEY_RUN_CWD": str(resolved_run_cwd),
            "BOOLEY_SIM_EDA_TOOL": eda_tool,
        }
    )
    if expose_build_root:
        environment["BOOLEY_BUILD_ROOT"] = str(build_root)
    else:
        environment.pop("BOOLEY_BUILD_ROOT", None)
    with suppress(FileNotFoundError):
        environment["BOOLEY_PROJECT_DIR"] = str(resolve_project_dir(root))
    if len(test_names) == 1:
        environment["BOOLEY_TEST_NAME"] = test_names[0]
    return environment


def _invoke_pre_sim(
    commands: tuple[str, ...],
    test_names: tuple[str, ...],
    root: Path,
    environment: Mapping[str, str],
    timeout_s: int,
) -> PreSimEvidence:
    """Execute one prepared hook and normalize its result."""
    started = time.monotonic()
    try:
        lease_fd = active_child_lease_fd()
        child_kwargs = (
            {"pass_fds": (lease_fd,)} if os.name != "nt" and lease_fd is not None else {}
        )
        command = [bash_bin(), "-c", "\n".join(("set -e", *commands))]
        result = _run_pre_sim_process(
            command,
            cwd=root,
            env=environment,
            timeout=timeout_s,
            **child_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        return PreSimEvidence(
            commands,
            test_names,
            "timed_out",
            time.monotonic() - started,
            str(exc),
        )
    except OSError as exc:
        return PreSimEvidence(
            commands,
            test_names,
            "failed",
            time.monotonic() - started,
            str(exc),
        )
    detail = result.stderr.strip() or result.stdout.strip()
    status = (
        "passed"
        if result.returncode == 0
        else "spawn_error"
        if find_missing_executable(detail)
        else "failed"
    )
    return PreSimEvidence(commands, test_names, status, time.monotonic() - started, detail)


def _run_pre_sim_process(command, *, cwd, env, timeout, **child_kwargs):
    scope = current_supervised_execution()
    if scope is None:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **child_kwargs,
        )
    if scope.cancelled():
        return subprocess.CompletedProcess(command, 125, "", "campaign cancelled")
    supervised_env = dict(env)
    if scope.execution_id is not None:
        supervised_env[RUNTIME_EXECUTION_ENV] = str(scope.execution_id)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=supervised_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_new_group_kwargs(),
        **child_kwargs,
    )
    scope.processes.register(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        kill_process_tree(process)
        process.communicate()
        raise
    finally:
        scope.processes.unregister(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


__all__ = ["run_pre_sim_commands"]
