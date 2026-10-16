"""Internal SDK-independent endpoint process supervision.

Owns output draining, queue-aware timeout accounting, process-group termination,
and synthetic end events when a child cannot close its own presentation state.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.flows.endpoint_events import _write_display_event
from booley.mcp.call_context import CallContext
from booley.mcp.report_encoding import _max_stderr_bytes, _max_stdout_bytes
from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.display_identity import DisplayIdentity
from booley.runtime.process_group import (
    ProcessGroup,
    capture_process_group,
    force_async_process_group,
    force_async_process_group_now,
    new_group_kwargs,
    terminate_adopted_process_group,
    terminate_async_process_group,
)
from booley.runtime.python_artifacts import relocate_python_artifacts
from booley.runtime.session_paths import logs_runtime_dir
from booley.runtime.timefmt import utc_now_rfc3339

_STREAM_CAP_MULTIPLIER = 4
_WATCHDOG_TICK_SECONDS = 5.0
_CANCEL_GRACE_SECONDS = 5.0


def _stdout_cap_bytes() -> int:
    """Peak-memory cap for the buffered stdout of a endpoint subprocess."""
    return _max_stdout_bytes() * _STREAM_CAP_MULTIPLIER


def _stderr_cap_bytes() -> int:
    """Peak-memory cap for the buffered stderr of a endpoint subprocess."""
    return _max_stderr_bytes() * _STREAM_CAP_MULTIPLIER


async def _read_tail(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Drain *stream* to EOF, keeping only the last *cap* bytes.

    Trims periodically so peak memory stays under ~2x cap regardless of
    total output volume.
    """
    buf = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap * 2:
            del buf[:-cap]
    if len(buf) > cap:
        del buf[:-cap]
    return bytes(buf)


async def _wait_with_queue_credit(
    proc: asyncio.subprocess.Process,
    timeout: float,
    is_queued: Callable[[], bool],
    on_first_active: Callable[[], None] | None,
    *,
    tick_seconds: float = _WATCHDOG_TICK_SECONDS,
) -> bool:
    """Wait for *proc*, charging only non-queued time against *timeout*.

    The child claims its concurrency slot INSIDE its own process (ADR 0028),
    so wall-clock since spawn includes any queue wait — which can exceed the
    endpoint's whole budget when a long run holds the class slot. Killing a job
    for time it spent waiting in line defeats the queue, so the watchdog
    samples the child's admission state each tick and only ticks the budget
    down while the child is not QUEUED. ``on_first_active`` fires once, on
    the first non-queued observation — the run-start stamp for the durable
    record. Returns True when the process exited, False on (active-time)
    timeout. State is sampled at tick granularity; a queued→active flip
    mid-tick miscounts at most one tick.
    """
    active_used = 0.0
    seen_active = False
    while True:
        # This probe performs only a handful of bounded local file reads.  A
        # thread hop is more dangerous than those reads: repeatedly submitting
        # through the default asyncio executor has hung on some runtimes during
        # executor wake-up, wedging the watchdog before it can enforce its own
        # timeout.  Keep the small admission-state sample on the event loop.
        queued = is_queued()
        if not queued and not seen_active:
            seen_active = True
            if on_first_active is not None:
                with contextlib.suppress(Exception):
                    on_first_active()
        remaining = timeout - active_used
        if not queued and remaining <= 0:
            return False
        tick = tick_seconds if queued else min(tick_seconds, remaining)
        started = time.monotonic()
        try:
            await asyncio.wait_for(proc.wait(), timeout=tick)
            return True
        except TimeoutError:
            if not queued:
                active_used += time.monotonic() - started


def _endpoint_subprocess_env(
    context: CallContext, *, pytest_scope: str = "endpoint", **overrides: str
) -> dict[str, str]:
    """Return the child environment after MCP composes runtime persistence.

    The call context's overrides apply over the server environment, and the
    per-run *overrides* apply over both.
    """
    from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV

    env = {**os.environ, **context.subprocess_env_overrides, **overrides}
    env[INVOCATION_ORIGIN_ENV] = "transport"
    logs_dir = env.get("BOOLEY_LOGS_DIR", "")
    if logs_dir and not env.get("BOOLEY_RUNTIME_DIR"):
        env["BOOLEY_RUNTIME_DIR"] = str(logs_runtime_dir(logs_dir))
    runtime = env.get("BOOLEY_RUNTIME_DIR")
    if runtime:
        runtime_root = Path(runtime)
    else:
        try:
            runtime_root = resolve_booley_dir() / ".runtime"
        except (OSError, RuntimeError, ValueError):
            runtime_root = Path(tempfile.gettempdir()) / "booley-runtime"
        env["BOOLEY_RUNTIME_DIR"] = str(runtime_root)
    return relocate_python_artifacts(
        env,
        runtime_root / "python-artifacts",
        pytest_scope=pytest_scope,
    )


@dataclass(frozen=True)
class _RunningProcess:
    process: asyncio.subprocess.Process
    group: ProcessGroup
    stdout: asyncio.Task[bytes]
    stderr: asyncio.Task[bytes]


async def _run_subprocess(
    cmd: list[str],
    *,
    timeout: int = 600,
    on_spawn: Callable[[int], None] | None = None,
    env: dict[str, str] | None = None,
    is_queued: Callable[[], bool] | None = None,
    on_first_active: Callable[[], None] | None = None,
    watchdog_tick: float = _WATCHDOG_TICK_SECONDS,
    cancel_grace: float = _CANCEL_GRACE_SECONDS,
    spawn_context: Callable[[], AbstractContextManager[object]] | None = None,
) -> tuple[int, str, str, bool]:
    """Run a grouped child with bounded tails and wall-clock or active-time budget."""
    try:
        proc, group = await _spawn_endpoint(cmd, env, on_spawn, spawn_context)
    except OSError as exc:
        return 2, "", f"Subprocess launch failed for {cmd[0]!r}: {exc}", False
    assert proc.stdout is not None and proc.stderr is not None
    child = _RunningProcess(
        proc,
        group,
        asyncio.create_task(_read_tail(proc.stdout, _stdout_cap_bytes())),
        asyncio.create_task(_read_tail(proc.stderr, _stderr_cap_bytes())),
    )
    return await _supervise_output(
        child,
        timeout,
        is_queued=is_queued,
        on_first_active=on_first_active,
        watchdog_tick=watchdog_tick,
        cancel_grace=cancel_grace,
    )


async def _spawn_endpoint(
    cmd: list[str],
    env: dict[str, str] | None,
    on_spawn: Callable[[int], None] | None,
    spawn_context: Callable[[], AbstractContextManager[object]] | None,
) -> tuple[asyncio.subprocess.Process, ProcessGroup]:
    """Bounded creation/publication transaction, with cleanup after releasing ownership."""
    proc = None
    group = None
    creation = None
    try:
        with spawn_context() if spawn_context else nullcontext():
            creation = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    **_async_process_group_kwargs(),
                )
            )
            proc = await asyncio.wait_for(
                asyncio.shield(creation),
                timeout=5 if spawn_context else None,
            )
            group = capture_process_group(proc)
            if on_spawn is not None:
                if spawn_context:
                    on_spawn(proc.pid)
                else:
                    with contextlib.suppress(Exception):
                        on_spawn(proc.pid)
    except BaseException:
        # Release ownership before completing a cancelled spawn or reaping.
        # Fresh admission remains prune-visible throughout bounded cleanup.
        await _reap_failed_spawn(creation, proc, group)
        raise
    assert group is not None
    return proc, group


async def _reap_failed_spawn(
    creation: asyncio.Task[asyncio.subprocess.Process] | None,
    proc: asyncio.subprocess.Process | None,
    group: ProcessGroup | None,
) -> None:
    if proc is None and creation is not None:
        try:
            proc = await asyncio.wait_for(asyncio.shield(creation), timeout=5)
            group = capture_process_group(proc)
        except (OSError, ValueError, RuntimeError, TimeoutError, asyncio.CancelledError):
            creation.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(creation, timeout=5)
    if proc is not None and group is not None:
        await asyncio.shield(_kill_async_process_tree(proc, group))


async def _supervise_output(
    child: _RunningProcess,
    timeout: int,
    *,
    is_queued: Callable[[], bool] | None,
    on_first_active: Callable[[], None] | None,
    watchdog_tick: float,
    cancel_grace: float,
) -> tuple[int, str, str, bool]:
    try:
        try:
            await _await_supervised_process(
                child.process,
                timeout,
                is_queued=is_queued,
                on_first_active=on_first_active,
                watchdog_tick=watchdog_tick,
            )
        except TimeoutError:
            return await _timeout_subprocess_result(
                child.process, child.group, timeout, child.stdout, child.stderr
            )
        except asyncio.CancelledError:
            # Give cooperative cleanup a bounded chance, then force surviving descendants.
            await _cancel_async_process_tree(
                child.process, child.group, grace_seconds=cancel_grace
            )
            await _cancel_stream_tasks(child.stdout, child.stderr)
            raise
        stdout, stderr = await child.stdout, await child.stderr
        return (
            child.process.returncode if child.process.returncode is not None else 2,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            False,
        )
    finally:
        _force_unfinished_process(child)


def _force_unfinished_process(child: _RunningProcess) -> None:
    """Signal-only fallback when awaiting cleanup is unsafe during unwinding."""
    if child.process.returncode is None:
        _signal_kill_process_group(child.process, child.group)
        for task in (child.stdout, child.stderr):
            if not task.done():
                task.cancel()


async def _await_supervised_process(
    proc: asyncio.subprocess.Process,
    timeout: int,
    *,
    is_queued: Callable[[], bool] | None,
    on_first_active: Callable[[], None] | None,
    watchdog_tick: float = _WATCHDOG_TICK_SECONDS,
) -> None:
    """Wait for a child, charging either wall-clock or active-only time."""
    if is_queued is None:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return
    if not await _wait_with_queue_credit(
        proc, timeout, is_queued, on_first_active, tick_seconds=watchdog_tick
    ):
        raise TimeoutError


async def _cancel_stream_tasks(*tasks: asyncio.Task[bytes]) -> None:
    """Cancel and drain subprocess output readers."""
    for task in tasks:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _timeout_subprocess_result(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
    timeout: int,
    stdout_task: asyncio.Task[bytes],
    stderr_task: asyncio.Task[bytes],
) -> tuple[int, str, str, bool]:
    """Kill a timed-out subprocess and construct its bounded diagnostic."""
    snapshot = await _async_process_snapshot(proc.pid)
    await _kill_async_process_tree(proc, group)
    await _cancel_stream_tasks(stdout_task, stderr_task)
    stderr = f"Subprocess timed out after {timeout}s"
    if snapshot:
        stderr += "\n\nProcess snapshot before kill:\n" + snapshot
    return 2, "", stderr, True


def _async_process_group_kwargs() -> dict[str, Any]:
    """Popen kwargs for an asyncio child process group/session."""
    return new_group_kwargs()


async def _async_process_snapshot(pid: int) -> str:
    """Best-effort process tree snapshot for timeout diagnostics."""
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            "tasklist",
            "/FI",
            f"PID eq {pid}",
            "/FO",
            "LIST",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
        except TimeoutError:
            return ""
        return stdout.decode("utf-8", errors="replace").strip()

    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-eo",
        "pid,ppid,stat,etime,cmd",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
    except TimeoutError:
        return ""
    rows = stdout.decode("utf-8", errors="replace").splitlines()
    if not rows:
        return ""
    wanted = {str(pid)}
    selected = [rows[0]]
    changed = True
    while changed:
        changed = False
        for row in rows[1:]:
            parts = row.split(None, 4)
            if len(parts) < 2:
                continue
            row_pid, row_ppid = parts[0], parts[1]
            if row_pid in wanted or row_ppid in wanted:
                if row not in selected:
                    selected.append(row)
                if row_pid not in wanted:
                    wanted.add(row_pid)
                    changed = True
    return "\n".join(selected[:80])


def _signal_kill_process_group(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Best-effort SIGKILL of the child's process group, no awaits.

    Safe to call while this coroutine is being cancelled (unlike
    :func:`_kill_async_process_tree`, which awaits and would immediately
    re-raise the pending cancellation). The child is started with
    ``start_new_session=True`` so it leads its own process group; killing the
    group reaps the whole endpoint subprocess tree — e.g. the Python runner *and* the
    simulator binary it spawned.
    """
    force_async_process_group_now(proc, group)


async def _kill_async_process_tree(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Terminate an asyncio subprocess and its process group."""
    await force_async_process_group(proc, group)


async def _cancel_async_process_tree(
    proc: asyncio.subprocess.Process,
    group: ProcessGroup,
    *,
    grace_seconds: float = _CANCEL_GRACE_SECONDS,
) -> None:
    """SIGTERM a endpoint process group, then SIGKILL it after a bounded grace."""
    await terminate_async_process_group(proc, group, grace_seconds=grace_seconds)


def _write_synthetic_endpoint_end(
    mcp_tool_name: str,
    duration_s: int,
    *,
    identity: DisplayIdentity,
    outcome: str = "timed_out",
    runtime_dir: Path | None = None,
) -> None:
    """Close presentation state when a killed child cannot publish its own end."""
    exit_code = 130 if outcome == "cancelled" else 2
    report_text = {
        "cancelled": "Cancelled by request",
        "aborted": "Endpoint supervisor stopped before completion",
    }.get(outcome, f"Killed by MCP server after {duration_s}s timeout")
    event: dict[str, Any] = {
        "type": "endpoint_end",
        "endpoint": mcp_tool_name,
        "exit_code": exit_code,
        "duration_s": duration_s,
        "outcome": outcome,
        "report_text": report_text,
        "timestamp": utc_now_rfc3339(),
    }
    if runtime_dir is None:
        _write_display_event(identity.apply(event))
    else:
        _write_display_event(identity.apply(event), runtime_dir=runtime_dir)


async def _cancel_adopted_process_group(
    pid: int, *, grace_seconds: float = _CANCEL_GRACE_SECONDS
) -> None:
    """Cancel a disk-only job by its session-leader PID."""
    await terminate_adopted_process_group(
        ProcessGroup(pid),
        grace_seconds=grace_seconds,
    )
