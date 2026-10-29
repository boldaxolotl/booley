"""Project-scoped Pre-Sim Commands execution for Simulation work units."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from booley.core.file_lock import active_child_lease_fd
from booley.flows.eda_failures import find_missing_executable
from booley.flows.sim.config import resolve_pre_sim_commands, resolve_run_cwd
from booley.flows.sim.live_progress import observe_stage
from booley.flows.test_runs import TestRun
from booley.runtime.booley_dir import resolve_booley_dir, resolve_checkout_booley_dir
from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV
from booley.runtime.platform_paths import (
    bash_bin,
    kill_process_tree,
    popen_new_group_kwargs,
)
from booley.runtime.python_artifacts import relocate_python_artifacts
from booley.runtime.supervised_execution import current_supervised_execution
from booley.targets.domain import TargetHandle

from .contract import PreSimEvidence, PreSimScopeStoppedError

TAIL_MAX_BYTES = 8192


def bounded_pre_sim_text(value: str | bytes | None) -> str:
    """Retain a valid UTF-8 tail within the evidence stream budget."""
    raw = value if isinstance(value, bytes) else (value or "").encode("utf-8")
    return raw[-TAIL_MAX_BYTES:].decode("utf-8", errors="ignore")


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
    test_runs: tuple[TestRun, ...] = (),
) -> PreSimEvidence | None:
    """Run the hook once for a native test or once for a Cocotb batch.

    *test_runs* are the concrete Test Runs the firing prepares; the hook sees
    them through the ``BOOLEY_TEST_RUN_*`` variables.
    """
    root = handle.project_root
    resolved_commands = tuple(resolve_pre_sim_commands(root)) if commands is None else commands
    if not resolved_commands:
        return None
    from booley.flows.terminal_progress import announce_unit

    observe_stage(handle.selector, "pre_sim", evidence_root=build_root, tests=test_names)
    announce_unit("Pre-Sim Commands: " + ",".join(test_names), target=handle.selector)
    environment = _pre_sim_environment(
        handle,
        test_names=test_names,
        build_root=build_root,
        eda_tool=eda_tool,
        simulator_environment=simulator_environment,
        run_cwd=run_cwd,
        expose_build_root=expose_build_root,
        test_runs=test_runs,
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
    test_runs: tuple[TestRun, ...] = (),
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
        environment["BOOLEY_DIR"] = str(resolve_booley_dir(root))
    if len(test_names) == 1:
        environment["BOOLEY_TEST_NAME"] = test_names[0]
    for name in TEST_RUN_VARIABLES:
        # An inherited value would describe some other firing.
        environment.pop(name, None)
    environment.update(test_run_environment(test_runs))
    try:
        cache_root = resolve_checkout_booley_dir(root) / ".runtime" / "python-artifacts"
    except (OSError, RuntimeError, ValueError):
        cache_root = build_root / "python-artifacts"
    scope = f"pre-sim:{handle.selector}:{resolved_run_cwd}:{' '.join(test_names)}"
    return relocate_python_artifacts(environment, cache_root, pytest_scope=scope)


#: Every variable that describes the Test Runs of one hook firing.
TEST_RUN_VARIABLES = (
    "BOOLEY_TEST_RUN_NAMES",
    "BOOLEY_TEST_RUN_NAME",
    "BOOLEY_TEST_VARIANT",
    "BOOLEY_TEST_SEED",
)


def test_run_environment(test_runs: tuple[TestRun, ...]) -> dict[str, str]:
    """The ``BOOLEY_TEST_RUN_*`` variables of a firing for *test_runs*.

    ``BOOLEY_TEST_NAME(S)`` keep bare Test names for existing hooks. These add
    the resolved names: ``BOOLEY_TEST_RUN_NAMES`` (space-joined), and for a
    firing of exactly one Test Run its name, its Test Variant (when the Test
    declares Variants) and its seed (when the Target is seeded).
    """
    if not test_runs:
        return {}
    variables = {"BOOLEY_TEST_RUN_NAMES": " ".join(run.name for run in test_runs)}
    if len(test_runs) == 1:
        run = test_runs[0]
        variables["BOOLEY_TEST_RUN_NAME"] = run.name
        if run.variant is not None:
            variables["BOOLEY_TEST_VARIANT"] = run.variant
        if run.seed is not None:
            variables["BOOLEY_TEST_SEED"] = str(run.seed)
    return variables


# Not a pytest test, although its name starts with ``test_``.
test_run_environment.__test__ = False  # type: ignore[attr-defined]


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
            bounded_pre_sim_text(str(exc)),
            stdout_tail=bounded_pre_sim_text(exc.stdout),
            stderr_tail=bounded_pre_sim_text(exc.stderr),
        )
    except OSError as exc:
        return PreSimEvidence(
            commands,
            test_names,
            "failed",
            time.monotonic() - started,
            bounded_pre_sim_text(str(exc)),
        )
    return _completed_pre_sim_evidence(commands, test_names, result, time.monotonic() - started)


def _completed_pre_sim_evidence(
    commands: tuple[str, ...],
    test_names: tuple[str, ...],
    result: subprocess.CompletedProcess[str],
    elapsed: float,
) -> PreSimEvidence:
    detail = result.stderr.strip() or result.stdout.strip()
    status = (
        "passed"
        if result.returncode == 0
        else "spawn_error"
        if find_missing_executable(detail)
        else "failed"
    )
    return PreSimEvidence(
        commands,
        test_names,
        status,
        elapsed,
        bounded_pre_sim_text(detail),
        result.returncode,
        bounded_pre_sim_text(result.stdout),
        bounded_pre_sim_text(result.stderr),
    )


def _run_pre_sim_process(command, *, cwd, env, timeout, **child_kwargs):
    from booley.flows.terminal_progress import current_progress

    observer = current_progress()
    scope = current_supervised_execution()
    if scope is None and observer is None:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **child_kwargs,
        )
    return _run_owned_pre_sim(command, cwd, env, timeout, child_kwargs, scope, observer)


def _run_owned_pre_sim(command, cwd, env, timeout, child_kwargs, scope, observer):
    if scope is not None and scope.cancelled():
        raise PreSimScopeStoppedError("execution scope stopped before Pre-Sim Commands")
    supervised_env = dict(env)
    if scope is not None and scope.execution_id is not None:
        supervised_env[RUNTIME_EXECUTION_ENV] = str(scope.execution_id)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=supervised_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_new_group_kwargs(),
        **child_kwargs,
    )
    if scope is not None:
        scope.processes.register(process)
    if observer is not None:
        # Only a spawned hook counts as started in the progress record.
        observer.begin_command()
    try:
        stdout, stderr = _communicate_pre_sim(process, command, timeout, observer)
    finally:
        if scope is not None:
            scope.processes.unregister(process)
        if observer is not None:
            process.stdout.close()
            process.stderr.close()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _communicate_pre_sim(process, command, timeout, observer):
    if observer is not None:
        from booley.flows.observed_process import communicate_observed

        stdout, stderr, timed_out = communicate_observed(process, observer, timeout=timeout)
        if timed_out:
            raise subprocess.TimeoutExpired(command, timeout, stdout, stderr)
        return stdout, stderr
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(process)
        stdout, stderr = process.communicate()
        exc.output = stdout or exc.output
        exc.stderr = stderr or exc.stderr
        raise
    except BaseException:
        kill_process_tree(process)
        process.communicate()
        raise


__all__ = ["run_pre_sim_commands"]
