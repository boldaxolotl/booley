#!/usr/bin/env python3
"""Run an already-built Verilator sim binary (edalize-era run-half).

ADR 0019/0022: FuseSoC+Edalize own the *build* (``fusesoc run --setup`` →
``make`` produces ``V<top>``); Booley owns the *run* — it executes that binary,
optionally under a live B-Wave trace, and re-emits the ``[SIM_SUMMARY]``
verdict sentinel the criteria layer scrapes.

This module is the edalize successor to the retired legacy Verilator
run-half. It is deliberately a **self-contained subprocess entry-point**
(``python -m booley.flows.sim.backends.verilator …``) so a Booley Flow can supervise the
whole run — including either the FIFO/B-Wave conversion lifecycle or a
Target-owned native-FST lifecycle — as one Session Runtime subprocess.

Unlike the legacy runner it does **not** build anything and reads no
``configs.toml``/``build_file_list`` design-description: the binary location is
passed in (``--bin-dir``, the edalize build dir holding ``V<top>``), so once
every legacy consumer is on this path the legacy file-list registry can be
deleted (Unit 6).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from booley.flows.run_log import write_run_log
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import (
    AdapterTransportIdentity,
    add_transport_arguments,
    publish_native_adapter_result,
    transport_identity_from_args,
    work_transport_arguments,
)
from booley.flows.sim.backends.shared import (
    RunLogProgress,
    TraceFileSnapshot,
    adopt_declared_trace_files,
    append_child_cpu_marker,
    child_cpu_snapshot,
    format_idle_note,
    snapshot_declared_trace_files,
    trace_file_stamp,
)
from booley.flows.sim.result import (
    count_sva_errors,
    extract_vrfc_warnings,
    format_infra_error,
    format_summary,
    parse_sim_verdict,
    write_result_json,
)
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.trace_session import TraceSession


def prepare_invocation(work: PreparedSimulationWork) -> list[str]:
    """Render one parent-side Verilator adapter invocation."""
    if work.adapter != "verilator":
        raise ValueError(f"Verilator adapter cannot prepare {work.adapter!r} work")
    cmd = [
        "python3",
        "-m",
        "booley.flows.sim.backends.verilator",
        "--bin-dir",
        work.build_dir,
        "--top",
        work.top,
        "--timeout",
        str(work.timeout_s),
    ]
    if work.max_rundir_bytes > 0:
        cmd += ["--max-rundir-bytes", str(work.max_rundir_bytes)]
    if work.run_cwd:
        cmd += ["--run-cwd", work.run_cwd]
    if work.trace:
        cmd += ["--trace", "--trace-mode", work.trace_mode]
    cmd += [f"--plusarg={value}" for value in work.plusargs]
    cmd += [f"--pass-sentinel={value}" for value in work.pass_sentinels]
    cmd += [f"--fail-sentinel={value}" for value in work.fail_sentinels]
    if work.trace:
        cmd += [f"--trace-arg={value}" for value in work.trace_args]
        cmd += [f"--trace-file={value}" for value in work.trace_files]
    cmd += work_transport_arguments(work)
    return cmd


_publish_adapter_result = publish_native_adapter_result


def _find_binary(bin_dir: Path, top_module: str) -> Path | None:
    """Locate the built ``V<top>`` executable in *bin_dir*.

    Edalize's Verilator flow links a static binary flat in the build dir (no
    ``obj_dir/`` and no ``.so`` — verified against a resolved sim build), so
    this looks directly in *bin_dir* rather than the legacy ``obj_dir`` layout.

    Deliberately does **not** fall back to the bare ``Vtop`` a Cocotb Target
    builds: edalize forces ``--prefix Vtop`` and links cocotb's own ``main``
    only when ``cocotb_module`` is set, and that binary is driven from Python
    over VPI — running it without cocotb's ``COCOTB_TEST_MODULES`` environment
    executes no test at all. Picking it up here would turn "wrong run-half"
    into a silent empty pass; :func:`_missing_binary_reason` names the mismatch
    instead (SETUP-F-40).
    """
    for name in (f"V{top_module}", f"V{top_module}.exe"):
        exe = bin_dir / name
        if exe.exists():
            return exe
    return None


def _report_missing_binary(bin_dir: Path, top_module: str, work_dir: Path) -> str:
    """Report an unfindable ``V<top>``; return the text to hand back to the caller.

    Names the cocotb mismatch when it fits, and stamps the shared
    ``[SIM_INFRA_ERROR]`` marker: no binary means the run never happened, so a
    grading caller must record "no observation" rather than reading the nonzero
    exit as a real FAIL verdict (SETUP-F-40/F-41b).
    """
    reason = f"Verilator executable V{top_module} not found in {bin_dir}"
    if (bin_dir / "Vtop").exists():
        reason += (
            " — but a 'Vtop' binary is present, which is what edalize builds for a "
            "Cocotb Target. Run Cocotb Targets through booley.flows.sim.backends.cocotb "
            "(they need cocotb's COCOTB_TEST_MODULES environment and are driven "
            "over VPI), not this run-half"
        )
    output = f"ERROR: {reason}\n{format_infra_error(reason)}"
    print(output)
    write_run_log(work_dir, output)
    return output


#: How Booley asks a verilated binary to start tracing, when the project has
#: not said otherwise. ``booley_vcd_dump.sv`` — the convention module Booley
#: ships — consumes exactly this pair via ``$value$plusargs``.
DEFAULT_TRACE_ARGS = ("+trace", "+tracefile={file}")


def _render_trace_args(trace_args: list[str] | None, trace_file: Path | None) -> list[str]:
    """Render the project's trace-enable arguments for this run.

    Booley cannot assume its own plusarg convention: a project that owns its
    C++ ``main()`` defines whatever trace CLI it likes, and Ibex's
    ``VerilatorSimCtrl`` accepts only getopt ``-t``/``--trace[=FILE]``. Passing
    the wrong convention is silent — the binary ignores the unknown plusarg,
    the run passes, and the dump contains nothing but a header. So the
    contract is configurable (``[flows.sim].trace_args``), with Booley's
    own convention as the default.

    ``{file}`` interpolates the resolved trace destination: a VCD FIFO for the
    conversion recipe or a regular FST file for the native recipe. Arguments
    that reference it are dropped when no destination is available.
    """
    rendered = []
    for arg in trace_args or DEFAULT_TRACE_ARGS:
        if "{file}" not in arg:
            rendered.append(arg)
        elif trace_file is not None:
            rendered.append(arg.format(file=trace_file))
    return rendered


def _build_run_cmd(
    exe: Path,
    lib_dir: Path,
    plusargs: list[str] | None,
) -> tuple[list[str], dict[str, str]]:
    """Assemble the binary invocation and environment (mirrors the legacy cmd).

    Trace arguments are appended separately by :func:`_setup_trace`, which is
    the only place that knows the trace destination. ``LD_LIBRARY_PATH`` is
    widened to *lib_dir* harmlessly (the edalize binary is static, but a DPI
    ``.so`` placed alongside still resolves, matching the legacy runner).
    """
    cmd = [str(exe)]
    env = os.environ.copy()
    if sys.platform != "win32":
        ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{ld}" if ld else str(lib_dir)
    for pa in plusargs or []:
        # A '+…' token is a plusarg ($value$plusargs); a '-…'/'--…' token is a
        # getopt argument forwarded verbatim to the binary's main (SETUP-7).
        cmd.append(pa if pa.startswith(("+", "-")) else f"+{pa}")
    return cmd, env


def _new_trace_session(work_dir: Path, trace_scope: str | None) -> TraceSession:
    """Create a verilator-backed trace session (tolerates older signatures)."""
    try:
        return TraceSession(work_dir, trace_scope, backend="verilator")
    except TypeError:  # pragma: no cover - defensive against fakes in tests
        return TraceSession(work_dir, trace_scope)


def _setup_bwave(
    trace: TraceSession,
    cmd: list[str],
    trace_args: list[str] | None = None,
) -> tuple[subprocess.Popen | None, bool, int | None]:
    """Start the FIFO streamer and append the project's trace arguments."""
    bwave_proc, use_fifo, keepalive_fd = trace.start_fifo()
    cmd.extend(_render_trace_args(trace_args, trace.fifo_path if use_fifo else None))
    return bwave_proc, use_fifo, keepalive_fd


def _setup_trace(
    trace: TraceSession,
    cmd: list[str],
    trace_args: list[str] | None,
    trace_mode: TraceMode,
) -> tuple[subprocess.Popen | None, bool, int | None]:
    """Configure the selected trace adapter and append its run arguments."""
    if trace_mode is TraceMode.NATIVE_FST:
        cmd.extend(_render_trace_args(trace_args, trace.work_bwave_path))
        return None, False, None
    return _setup_bwave(trace, cmd, trace_args)


def _kill_with_reason(
    proc: subprocess.Popen,
    trace: TraceSession | None,
    bwave_proc: subprocess.Popen | None,
    lines: deque[str],
    reason: str,
) -> None:
    """Kill the run, record *reason* (+ a trace incident when tracing), append it.

    Shared by the mid-run abort paths (timeout, missing-$readmemh, disk budget)
    so each names its own cause but they all tear the process tree down and
    surface the reason on stdout/in the captured output the same way.
    """
    from booley.runtime.platform_paths import kill_process_tree

    incident: Path | None = None
    if trace:
        incident = trace.write_incident(
            reason,
            sim_proc=proc,
            bwave_proc=bwave_proc,
        )
    kill_process_tree(proc)
    proc.wait()
    msg = f"ERROR: {reason}"
    print(msg)
    lines.append(msg + "\n")
    if incident is not None:
        lines.append(f"TRACE_INCIDENT: {incident}\n")


def _supervise(proc: subprocess.Popen) -> None:
    """Register *proc* with the parent-death guard (best-effort, F-13)."""
    try:
        from booley.flows.sim.run_guard import supervise_child

        supervise_child(proc)
    except ImportError:  # pragma: no cover - defensive against partial installs
        pass


def _stream_output(  # noqa: PLR0915 — one linear spawn+watchdogs+drain pipeline; splitting it would strand the shared proc/timer/guard/progress state
    cmd: list[str],
    run_cwd: Path,
    env: dict[str, str],
    timeout: int,
    trace: TraceSession | None,
    bwave_proc: subprocess.Popen | None,
    max_rundir_bytes: int = 0,
    work_dir: Path | None = None,
) -> tuple[deque[str], subprocess.Popen]:
    """Run the binary, stream stdout live, enforce *timeout* (seconds).

    Two safety guards ride alongside the timeout (see :mod:`booley.flows.sim.run_guard`):
    a missing-``$readmemh``-file warning is fatal (SETUP-23 — the sim would
    otherwise spin forever on uninitialised RAM), and *max_rundir_bytes* (>0)
    caps how much *run_cwd* may grow before a runaway tracer/``$dumpfile`` is
    killed (SETUP-25).

    When *work_dir* is given, the streamed tail is flushed into its ``run.log``
    every :data:`RUN_LOG_PROGRESS_INTERVAL_S` seconds so the run is observable
    while it is still going (fpu F-18).
    """
    from booley.flows.sim.run_guard import (
        DiskBudgetGuard,
        child_death_kwargs,
        readmemh_fatal_line,
        snapshot_dir_baseline,
    )
    from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs

    lines: deque[str] = deque(maxlen=5_000)
    cpu_started = child_cpu_snapshot()
    # BEFORE the spawn: the baseline walk takes seconds on the multi-GB trees
    # this budget exists for, and anything the sim dumps during that walk would
    # otherwise land in the baseline free of charge (fpu F-23).
    disk_baseline = snapshot_dir_baseline(run_cwd, max_rundir_bytes)
    proc = subprocess.Popen(
        cmd,
        cwd=str(run_cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_new_group_kwargs(),
        **child_death_kwargs(),
    )
    _supervise(proc)
    if trace and bwave_proc:
        trace.start_monitor(bwave_proc, proc)
    guard = DiskBudgetGuard(run_cwd, max_rundir_bytes, proc, baseline=disk_baseline)
    guard.start()
    started = time.monotonic()
    deadline = started + timeout
    progress = RunLogProgress(work_dir, started)
    # The in-loop deadline check below only runs when a line arrives, so a sim
    # that goes SILENT (fpu F-21: $fopen on a directory, no sentinel, no output)
    # would block in `for line in proc.stdout` past its budget forever and leave
    # the kill to simulate's wrapper timeout — which reaps only the `sh -c` and
    # orphans this supervisor plus its V<top> (fpu F-13). A timer thread kills
    # the tree from here instead, where the group kill actually reaches it.
    timed_out = {"hit": False}

    def _kill_at_deadline() -> None:
        if proc.poll() is None:
            timed_out["hit"] = True
            kill_process_tree(proc)

    watchdog = threading.Timer(timeout, _kill_at_deadline)
    watchdog.daemon = True
    watchdog.start()

    def _timeout_reason() -> str:
        return f"Verilator simulation timed out ({timeout}s)" + format_idle_note(
            progress.idle_s, len(lines)
        )

    stdout = proc.stdout
    try:
        assert stdout is not None
        for line in stdout:
            print(line, end="")
            lines.append(line)
            progress.observe(lines)
            # SETUP-23: a missing $readmemh init file warns once then spins
            # forever on uninitialised RAM — treat that warning as fatal.
            fatal = readmemh_fatal_line(line)
            if fatal:
                _kill_with_reason(
                    proc,
                    trace,
                    bwave_proc,
                    lines,
                    f"missing $readmemh memory-init file — {fatal}",
                )
                return lines, proc
            # SETUP-25: the disk-budget watchdog kills the proc on a runaway;
            # report it here (the for-loop sees EOF once the proc is gone).
            if guard.tripped:
                print(guard.message)
                lines.append(guard.message + "\n")
                return lines, proc
            if timed_out["hit"] or time.monotonic() > deadline:
                _kill_with_reason(proc, trace, bwave_proc, lines, _timeout_reason())
                return lines, proc
        # stdout closed. A silent disk runaway or a watchdog kill (no line ticked
        # the in-loop checks) still surfaces here once the proc is gone.
        if guard.tripped:
            print(guard.message)
            lines.append(guard.message + "\n")
            return lines, proc
        if timed_out["hit"]:
            _kill_with_reason(proc, trace, bwave_proc, lines, _timeout_reason())
            return lines, proc
        try:
            proc.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            # The binary closed stdout but didn't exit in budget. Kill it here —
            # this scope owns the Popen (the caller's `proc` is never assigned
            # when we raise) — and hand the streamed output to the caller via the
            # exception so its timeout message can still include it.
            kill_process_tree(proc)
            proc.wait()
            exc.output = "".join(lines)
            raise
        return lines, proc
    finally:
        if stdout is not None:
            stdout.close()
        watchdog.cancel()
        guard.stop()
        progress.final_flush(lines)
        append_child_cpu_marker(lines, cpu_started)


def _evaluate_verdict(
    output: str,
    returncode: int,
    work_dir: Path,
    *,
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
) -> None:
    """Print the ``[SIM_SUMMARY]`` verdict + write result JSON (legacy parity)."""
    verdict = parse_sim_verdict(
        output,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
    )
    sva_errors = count_sva_errors(output)
    vrfc = extract_vrfc_warnings(output)

    inconclusive = False
    if verdict is True:
        passed = sva_errors == 0
    elif verdict is False or returncode != 0 or sva_errors > 0:
        passed = False
    else:
        passed, inconclusive = False, True

    print(format_summary(passed, sva_errors, vrfc, inconclusive=inconclusive))
    if inconclusive:
        print("\nVerilator sim INCONCLUSIVE (rc=0, no sentinel)")
    elif passed:
        print(f"\nVerilator sim PASSED (rc={returncode})")
    elif verdict is False:
        # A FAIL sentinel matched. The sim can still exit 0 (e.g. an SVA/$error
        # that reports but doesn't $fatal), so cite rc only when it is
        # actually nonzero — never print the maximally-confusing "(rc=0)".
        reason = f"rc={returncode}" if returncode else "fail sentinel matched"
        print(f"\nVerilator sim FAILED ({reason})")
    elif sva_errors > 0:
        print(f"\nVerilator sim FAILED ({sva_errors} SVA assertion errors)")
    else:
        print(f"\nVerilator sim FAILED (rc={returncode})")

    first_err = ""
    if not passed:
        for ln in output.splitlines():
            if any(s in ln for s in ("FAILED", "Fatal:", "Error!", "ERROR!", "Mismatch")):
                first_err = ln.strip()
                break
    write_result_json(
        work_dir, passed, sva_errors, first_err, returncode, inconclusive=inconclusive
    )
    # Persist the raw output next to result.json on pass AND fail:
    # result.json only carries a 500-char first_error, so run.log is what
    # survives MCP-level stdout truncation.
    write_run_log(work_dir, output)


def _resolve_single_scope(trace_scope: str | None) -> str | None:
    """Collapse a comma-list trace scope to Verilator's single supported scope.

    Verilator can only trace one hierarchy; if the caller passed several,
    warn and keep the first. Returns the effective scope (or None).
    """
    scope = trace_scope
    if trace_scope and "," in trace_scope:
        parts = [s.strip() for s in trace_scope.split(",") if s.strip()]
        if len(parts) > 1:
            print(
                f"WARNING: Verilator supports only a single trace scope; using "
                f"{parts[0]!r}, ignoring {parts[1:]}",
                file=sys.stderr,
            )
        scope = parts[0] if parts else None
    return scope


@dataclass(frozen=True)
class _RunPaths:
    """Resolved filesystem locations for one verilated execution."""

    bin_dir: Path
    run_cwd: Path
    work_dir: Path


@dataclass(frozen=True)
class _TraceRuntime:
    """Prepared trace processes and freshness evidence for one execution."""

    session: TraceSession | None
    search_dirs: list[Path]
    files_before: TraceFileSnapshot
    mode: TraceMode
    bwave_proc: subprocess.Popen | None
    use_fifo: bool
    keepalive_fd: int | None


def _prepare_trace_artifacts(
    trace: TraceSession | None,
    trace_files: list[str] | None,
    run_cwd: Path,
    work_dir: Path,
    bin_dir: Path,
) -> tuple[list[Path], TraceFileSnapshot]:
    """Reset Booley-owned output and snapshot project-owned trace locations."""
    if trace is not None:
        trace.reset_for_run()
    search_dirs = [run_cwd, work_dir, bin_dir]
    return search_dirs, snapshot_declared_trace_files(trace_files, search_dirs)


def _find_current_trace(
    trace: TraceSession,
    trace_files: list[str] | None,
    search_dirs: list[Path],
    before: TraceFileSnapshot | None,
) -> Path | None:
    """Find one artifact proven new or changed by the current simulation."""
    found = trace.find()
    if (
        found is not None
        and before is not None
        and before.get(found.resolve()) == trace_file_stamp(found)
    ):
        found = None
    if found is None and trace_files:
        found = adopt_declared_trace_files(trace, trace_files, search_dirs, before=before)
    return found


def _missing_trace_reason(trace_files: list[str] | None) -> str:
    """Explain why no current trace artifact could be retained."""
    reason = "trace requested but no fresh .fst store or convertible .vcd was produced"
    if trace_files:
        return reason + " (no fresh declared artifact was produced by the current run)"
    return reason + (
        " (a testbench with its own C++ main() writes its dump under a name "
        "Booley cannot guess — declare it in [flows.sim].trace_files)"
    )


def _finalize_trace(
    trace: TraceSession,
    proc: subprocess.Popen | None,
    bwave_proc: subprocess.Popen | None,
    trace_files: list[str] | None = None,
    search_dirs: list[Path] | None = None,
    trace_files_before: TraceFileSnapshot | None = None,
) -> str:
    """Locate the produced waveform and emit the TRACE_OK / TRACE_INCIDENT line.

    Returns the text to append to the run's captured output.
    """
    found = _find_current_trace(
        trace,
        trace_files,
        search_dirs or [],
        trace_files_before,
    )
    if found is None:
        failure_reason = _missing_trace_reason(trace_files)
    else:
        inspection = trace.inspect(found)
        failure_reason = (
            ""
            if inspection.usable
            else (
                "trace requested but the retained waveform is not queryable: "
                f"{inspection.failure_reason}"
            )
        )
    if failure_reason:
        incident = trace.write_incident(
            failure_reason,
            sim_proc=proc,
            bwave_proc=bwave_proc,
        )
        print(f"ERROR: {failure_reason}")
        print(f"TRACE_INCIDENT: {incident}")
        return f"\nERROR: {failure_reason}\nTRACE_INCIDENT: {incident}"
    artifact = inspection.artifact
    assert artifact is not None
    output = f"TRACE_OK: {artifact.path}\n{artifact.metadata_line()}"
    print(output)
    return f"\n{output}"


def _resolve_run_paths(
    bin_dir: Path,
    run_cwd: Path | None,
    work_dir: Path | None,
) -> _RunPaths:
    """Resolve runner paths before the simulator changes its cwd."""
    resolved_bin = Path(bin_dir).resolve()
    return _RunPaths(
        bin_dir=resolved_bin,
        run_cwd=Path(run_cwd).resolve() if run_cwd is not None else resolved_bin,
        work_dir=Path(work_dir).resolve() if work_dir is not None else resolved_bin,
    )


def _prepare_trace_runtime(
    paths: _RunPaths,
    enabled: bool,
    trace_scope: str | None,
    trace_files: list[str] | None,
    cmd: list[str],
    trace_args: list[str] | None,
    trace_mode: TraceMode,
) -> _TraceRuntime:
    """Prepare trace output, freshness evidence, and any FIFO converter."""
    session = _new_trace_session(paths.work_dir, trace_scope) if enabled else None
    search_dirs, files_before = _prepare_trace_artifacts(
        session,
        trace_files,
        paths.run_cwd,
        paths.work_dir,
        paths.bin_dir,
    )
    bwave_proc, use_fifo, keepalive_fd = (
        _setup_trace(session, cmd, trace_args, trace_mode)
        if session is not None
        else (None, False, None)
    )
    return _TraceRuntime(
        session=session,
        search_dirs=search_dirs,
        files_before=files_before,
        mode=trace_mode,
        bwave_proc=bwave_proc,
        use_fifo=use_fifo,
        keepalive_fd=keepalive_fd,
    )


def _execute_with_heartbeat(
    cmd: list[str],
    paths: _RunPaths,
    env: dict[str, str],
    timeout: int,
    trace: _TraceRuntime,
    max_rundir_bytes: int,
) -> tuple[deque[str], subprocess.Popen]:
    """Execute one simulator process with heartbeat and trace cleanup."""
    from booley.runtime.heartbeat import Heartbeat

    heartbeat = Heartbeat("Verilator sim", interval=60)
    heartbeat.start()
    try:
        return _stream_output(
            cmd,
            paths.run_cwd,
            env,
            timeout,
            trace.session,
            trace.bwave_proc,
            max_rundir_bytes=max_rundir_bytes,
            work_dir=paths.work_dir,
        )
    finally:
        heartbeat.stop()
        if trace.session is not None and trace.mode is TraceMode.VCD_FIFO:
            trace.session.cleanup_fifo(trace.bwave_proc, trace.keepalive_fd)


def _timeout_result(
    exc: subprocess.TimeoutExpired,
    timeout: int,
    work_dir: Path,
) -> str:
    """Persist and return the output of a simulator that exhausted its budget."""
    print(f"ERROR: Verilator simulation timed out ({timeout}s)")
    output = (exc.output or "") + f"\nTimed out after {timeout}s"
    write_run_log(work_dir, output)
    return output


def _finalize_verilated_run(
    lines: deque[str],
    proc: subprocess.Popen,
    paths: _RunPaths,
    trace: _TraceRuntime,
    trace_files: list[str] | None,
    pass_sentinels: list[str] | None,
    fail_sentinels: list[str] | None,
) -> str:
    """Persist the verdict and validate any trace produced by the current run."""
    output = "".join(lines)
    if trace.session is not None and trace.session.stall_killed:
        output += f"\nERROR: {trace.session.stall_message}"
    _evaluate_verdict(
        output,
        proc.returncode,
        paths.work_dir,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
    )
    if trace.session is None:
        return output
    if trace.mode is TraceMode.VCD_FIFO and not trace.use_fifo:
        trace.session.postprocess(paths.work_dir / "trace.vcd")
    output += _finalize_trace(
        trace.session,
        proc,
        trace.bwave_proc,
        trace_files=trace_files,
        search_dirs=trace.search_dirs,
        trace_files_before=trace.files_before,
    )
    return output


def run_verilated_binary(
    *,
    top_module: str,
    bin_dir: Path,
    run_cwd: Path | None = None,
    work_dir: Path | None = None,
    vcd: bool = False,
    plusargs: list[str] | None = None,
    timeout: int = 600,
    trace_scope: str | None = None,
    trace_args: list[str] | None = None,
    trace_files: list[str] | None = None,
    trace_mode: TraceMode = TraceMode.VCD_FIFO,
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
    max_rundir_bytes: int = 0,
    transport: AdapterTransportIdentity | None = None,
) -> str:
    """Run one edalize-built ``V<top>`` under the resolved trace recipe."""
    paths = _resolve_run_paths(bin_dir, run_cwd, work_dir)
    paths.work_dir.mkdir(parents=True, exist_ok=True)
    exe = _find_binary(paths.bin_dir, top_module)
    if exe is None:
        output = _report_missing_binary(paths.bin_dir, top_module, paths.work_dir)
        _publish_adapter_result(
            transport,
            output,
            1,
            failure_kind="infrastructure",
            detail=output.strip(),
        )
        return output
    scope = _resolve_single_scope(trace_scope)
    cmd, env = _build_run_cmd(exe, paths.bin_dir, plusargs)
    trace = _prepare_trace_runtime(paths, vcd, scope, trace_files, cmd, trace_args, trace_mode)
    print(f"\n{'=' * 60}")
    print(f"[Verilator simulation: {top_module}]")
    print(f"{'=' * 60}")
    print(f"CWD: {paths.run_cwd}")
    print(f"CMD: {' '.join(cmd)}\n")
    try:
        lines, proc = _execute_with_heartbeat(cmd, paths, env, timeout, trace, max_rundir_bytes)
    except subprocess.TimeoutExpired as exc:
        output = _timeout_result(exc, timeout, paths.work_dir)
        _publish_adapter_result(
            transport,
            output,
            1,
            failure_kind="timeout",
            detail=f"Verilator simulation timed out after {timeout}s",
        )
        return output
    output = _finalize_verilated_run(
        lines, proc, paths, trace, trace_files, pass_sentinels, fail_sentinels
    )
    _publish_adapter_result(
        transport,
        output,
        proc.returncode,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        trace_required=vcd,
    )
    return output


def _positive_int(value: str) -> int:
    """argparse type: reject non-positive timeouts at the CLI boundary.

    A negative/zero ``--timeout`` reaches the deadline math and would time the
    simulation out instantly, so validate range here rather than misbehave later.
    """
    ivalue = int(value)  # raises ValueError -> argparse reports the bad token
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {ivalue}")
    return ivalue


def _add_trace_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the closed trace-recipe command-line surface."""
    parser.add_argument("--trace", action="store_true", help="stream a live B-Wave trace")
    parser.add_argument(
        "--trace-mode",
        type=TraceMode,
        choices=list(TraceMode),
        default=TraceMode.VCD_FIFO,
        help="resolved trace recipe supplied by the parent Simulation Flow",
    )
    parser.add_argument("--trace-scope", default=None, help="scope the trace to a hierarchy")
    parser.add_argument(
        "--trace-arg",
        action="append",
        default=[],
        dest="trace_args",
        help="argument enabling trace capture; '{file}' interpolates the destination",
    )
    parser.add_argument(
        "--trace-file",
        action="append",
        default=[],
        dest="trace_files",
        help="declared trace path or glob, relative to a runner search directory",
    )


def _add_verdict_arguments(parser: argparse.ArgumentParser) -> None:
    """Register simulator arguments and project-owned verdict sentinels."""
    parser.add_argument(
        "--plusarg",
        action="append",
        default=[],
        dest="plusargs",
        help="plusarg passed to the binary (repeatable; +-prefix optional)",
    )
    parser.add_argument(
        "--pass-sentinel",
        action="append",
        default=[],
        dest="pass_sentinels",
        help="substring marking a PASS (repeatable; overrides the default)",
    )
    parser.add_argument(
        "--fail-sentinel",
        action="append",
        default=[],
        dest="fail_sentinels",
        help="substring marking a FAIL (repeatable; overrides the default)",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse one Verilator run-half invocation."""
    parser = argparse.ArgumentParser(description="Run an edalize-built Verilator sim binary")
    parser.add_argument("--bin-dir", required=True, help="dir holding V<top>")
    parser.add_argument("--top", required=True, help="toplevel module")
    parser.add_argument("--run-cwd", default=None, help="cwd to run the binary from")
    parser.add_argument("--work-dir", default=None, help="trace/result output dir")
    parser.add_argument(
        "--timeout",
        type=_positive_int,
        default=600,
        help="run timeout (seconds)",
    )
    parser.add_argument(
        "--max-rundir-bytes",
        type=int,
        default=0,
        dest="max_rundir_bytes",
        help="kill the run if the run dir exceeds this many bytes (0=off; SETUP-25)",
    )
    _add_trace_arguments(parser)
    _add_verdict_arguments(parser)
    add_transport_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    transport = transport_identity_from_args(args, "verilator")
    # Entry-point only (never the in-process API): tie this supervisor's life to
    # its parent's so simulate's wrapper timeout cannot orphan it (F-13).
    from booley.flows.sim.run_guard import install_parent_death_guard

    install_parent_death_guard()
    output = run_verilated_binary(
        top_module=args.top,
        bin_dir=Path(args.bin_dir),
        run_cwd=Path(args.run_cwd) if args.run_cwd else None,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        vcd=args.trace,
        plusargs=args.plusargs,
        timeout=args.timeout,
        trace_scope=args.trace_scope,
        trace_args=args.trace_args or None,
        trace_files=args.trace_files or None,
        trace_mode=args.trace_mode,
        pass_sentinels=args.pass_sentinels or None,
        fail_sentinels=args.fail_sentinels or None,
        max_rundir_bytes=args.max_rundir_bytes,
        transport=transport,
    )
    # Non-zero exit on a parsed FAIL so a shipped `&&` chain reflects the verdict.
    verdict = parse_sim_verdict(
        output,
        pass_sentinels=args.pass_sentinels or None,
        fail_sentinels=args.fail_sentinels or None,
    )
    return 0 if verdict is True else 1


if __name__ == "__main__":  # pragma: no cover - subprocess entry-point
    raise SystemExit(main())
