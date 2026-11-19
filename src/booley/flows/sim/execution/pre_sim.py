"""Project-scoped Pre-Sim Commands execution for Simulation work units."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from booley.core.file_lock import active_child_lease_fd
from booley.flows.eda_failures import find_missing_executable
from booley.flows.job_resources import (
    JobResourceMonitor,
    MeasuredPopen,
    ResourceLabel,
    record_command,
)
from booley.flows.sim.config import resolve_pre_sim_commands, resolve_run_cwd
from booley.flows.sim.live_progress import observe_stage
from booley.flows.test_runs import TestRun
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.endpoint_execution import OOM_STOPPED_MESSAGE, job_stopped_after_oom
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
_KILL_DRAIN_TIMEOUT_S = 5  # how long a killed hook's pipes may stay open


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
    resource_flags: tuple[str, ...],
    simulator_environment: Mapping[str, str] | None = None,
    commands: tuple[str, ...] | None = None,
    run_cwd: str | None = None,
    working_directory: Path | None = None,
    expose_build_root: bool = True,
    test_runs: tuple[TestRun, ...] = (),
) -> PreSimEvidence | None:
    """Run the hook once for a native test or once for a Cocotb batch.

    *test_runs* are the concrete Test Runs the firing prepares; the hook sees
    them through the ``BOOLEY_TEST_RUN_*`` variables. Each firing that spawns is
    measured and appended to the Resource History as the Target's ``sim_pre_run``
    phase under *resource_flags*, the flags that key every sim record of the Job.
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
        (ResourceLabel(handle.selector, "sim_pre_run"), resource_flags),
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
    if len(test_names) == 1:
        environment["BOOLEY_TEST_NAME"] = test_names[0]
    for name in TEST_RUN_VARIABLES:
        # An inherited value would describe some other firing.
        environment.pop(name, None)
    environment.update(test_run_environment(test_runs))
    try:
        # The checkout's own directory, not the selected one: a linked worktree
        # inside the Sandbox must not be handed the Main Booley Directory.
        own_booley_dir = resolve_checkout_booley_dir(root)
    except (OSError, RuntimeError, ValueError):
        cache_root = build_root / "python-artifacts"
    else:
        environment["BOOLEY_DIR"] = str(own_booley_dir)
        cache_root = own_booley_dir / ".runtime" / "python-artifacts"
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
    resource: tuple[ResourceLabel, tuple[str, ...]],
) -> PreSimEvidence:
    """Execute one prepared hook and normalize its result."""
    if job_stopped_after_oom():
        # A failure, not a stopped scope: the Simulation Record must not end cancelled.
        return PreSimEvidence(commands, test_names, "failed", 0.0, OOM_STOPPED_MESSAGE)
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
            resource=resource,
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


def _run_pre_sim_process(command, *, cwd, env, timeout, resource, **child_kwargs):
    from booley.flows.terminal_progress import current_progress

    scope = current_supervised_execution()
    if scope is not None and scope.cancelled():
        raise PreSimScopeStoppedError("execution scope stopped before Pre-Sim Commands")
    # Each firing gets the private TMPDIR of every measured command, so temporary
    # files on a tmpfs count toward its peak.
    monitor = JobResourceMonitor()
    supervised_env = {**env, **monitor.temp_env()}
    if scope is not None and scope.execution_id is not None:
        supervised_env[RUNTIME_EXECUTION_ENV] = str(scope.execution_id)
    try:
        process = MeasuredPopen(
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
        return _await_pre_sim(
            process, command, timeout, monitor, (scope, current_progress(), resource)
        )
    finally:
        monitor.finish()  # stops the sampling thread, also when the spawn failed
        if monitor.tmp_dir is not None:
            shutil.rmtree(monitor.tmp_dir, ignore_errors=True)


def _await_pre_sim(process, command, timeout, monitor, owners):
    """Wait for a spawned hook; a firing that finished or timed out is recorded."""
    scope, observer, (label, flags) = owners
    started = time.monotonic()
    if scope is not None:
        scope.processes.register(process)
    if observer is not None:
        # Only a spawned hook counts as started in the progress record.
        observer.begin_command()
    timed_out = False
    settled = False
    try:
        try:
            monitor.start(process.pid)
        except BaseException:
            # A hook that spawned must not outlive a monitor that cannot start.
            _reap_pre_sim(process)
            raise
        try:
            stdout, stderr = _communicate_pre_sim(process, command, timeout, observer)
        except subprocess.TimeoutExpired:
            timed_out = settled = True  # a lower bound, recorded as a timeout
            raise
        settled = True
    finally:
        if scope is not None:
            scope.processes.unregister(process)
        if observer is not None:
            assert process.stdout is not None and process.stderr is not None  # spawned with PIPE
            process.stdout.close()
            process.stderr.close()
        measurement = monitor.finish(process.reaped_peak_bytes)
        if settled:
            from booley.flows.base import SubprocessResult

            result = SubprocessResult(
                returncode=process.returncode,
                timed_out=timed_out,
                duration_s=time.monotonic() - started,
            )
            record_command("sim", label, flags, measurement, result)
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
        stdout, stderr = _reap_pre_sim(process)
        exc.output = stdout or exc.output
        exc.stderr = stderr or exc.stderr
        raise
    except BaseException:
        _reap_pre_sim(process)
        raise


def _reap_pre_sim(process):
    """Kill the hook's tree and collect its output, giving up on escaped holders."""
    kill_process_tree(process)
    try:
        return process.communicate(timeout=_KILL_DRAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # A descendant that left the group still holds a pipe: drop its output.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        return None, None


__all__ = ["run_pre_sim_commands"]
