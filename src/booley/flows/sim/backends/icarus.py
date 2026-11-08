#!/usr/bin/env python3
"""Run an already-built Icarus (vvp) sim image — the edalize-era Icarus run-half.

ADR 0019/0022: FuseSoC+Edalize own the *build* (``fusesoc run --setup`` →
``make`` runs ``iverilog`` and produces the vvp image named ``<edam-name>`` with
a sibling ``<name>.scr``); Booley owns the *run* — it executes that image with
``vvp``, optionally postprocesses a VCD into a queryable B-Wave, and re-emits the
``[SIM_SUMMARY]`` verdict sentinel the criteria layer scrapes.

This is the Icarus counterpart of :mod:`booley.flows.sim.backends.verilator` and the
edalize successor to the legacy ``run_iverilog_sim`` / ``run_sim_batch`` Icarus
runners. Like the Verilator run-half it is a **self-contained subprocess
entry-point** (``python -m booley.flows.sim.backends.icarus …``) so a Booley Flow can
supervise the whole run through one Sandbox subprocess.

Empirically-nailed Icarus run-half facts (notes-unitA-edalize-trace-runmany.md):

  * Edalize's ``icarus`` flow names the vvp image ``<edam-name>`` (no ``.vvp``
    extension) and writes a sibling ``<name>.scr``; the image is discovered via
    ``glob('*.scr')`` → strip ``.scr``. The build dir holds the VPI modules, so
    ``vvp`` runs with ``-M<abs build_dir>`` to resolve them from any cwd.
  * **No live FIFO.** The committed ``sim/booley_vcd_dump.sv`` fires
    ``$dumpfile("dump.vcd")``/``$dumpvars(0)`` on ``+trace`` (the explicit
    name matches iverilog's ``-fst``-omitted default and exists for xmsim,
    which silently dumps nothing without a ``$dumpfile``), so under vvp the
    dump lands in ``dump.vcd`` in the run cwd.
    v1 is therefore **file → postprocess**, never a streaming FIFO: run
    ``vvp`` directly (not ``make run``), omit ``-fst`` to get ``dump.vcd``, then
    feed it through ``TraceSession(..., backend="iverilog").postprocess`` — the
    same VCD→bwave machinery the legacy runner used.
  * ``run_cwd`` is load-bearing: a TB that opens vectors/firmware relative to
    cwd must run from the project's sim cwd (the pilot opens ``../../tb/`` from
    ``util/sim``), so the binary's dump and any ``$fopen`` resolve there — *not*
    from the edalize build dir.

It builds nothing and reads no ``configs.toml`` / ``build_file_list``
design-description: the build dir is passed in (the prepared work document), so once every
Icarus consumer is on this path the legacy file-list registry can be deleted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from booley.flows.run_log import write_run_log
from booley.flows.sim.adapter_contract import PreparedSimulationWork, run_adapter_entry
from booley.flows.sim.adapter_transport import (
    AdapterTraceResult,
    AdapterTransportIdentity,
    NativeRun,
    assess_native_verdict,
    first_native_failure,
    native_verdict_message,
    publish_native_adapter_result,
    work_transport_identity,
)
from booley.flows.sim.backends.shared import (
    BOOLEY_DUMP_VCD_NAME,
    CurrentRunTraces,
    RunLogProgress,
    RunTermination,
    ambiguous_trace_reason,
    append_child_cpu_marker,
    child_cpu_snapshot,
    find_icarus_image,
    format_idle_note,
)
from booley.flows.sim.result import (
    extract_vrfc_warnings,
    format_summary,
    write_result_json,
)
from booley.flows.sim.trace_session import TraceSession

# The dump module's explicit ``$dumpfile`` name — identical to iverilog's
# default with ``-fst`` omitted. vvp writes it into the run cwd.
_DEFAULT_VCD_NAME = BOOLEY_DUMP_VCD_NAME


@dataclass(frozen=True)
class _IcarusRun:
    build_dir: Path
    run_cwd: Path
    work_dir: Path
    image: Path
    command: list[str]
    trace: TraceSession | None
    # Freshness filter for this run's waveform; None when tracing is off.
    current: CurrentRunTraces | None


def _trace_roots(run_cwd: Path, work_dir: Path, build_dir: Path) -> list[Path]:
    """Directories searched for a testbench-declared trace file."""
    return [run_cwd, work_dir, build_dir]


_publish_adapter_result = publish_native_adapter_result


def _find_vvp() -> str:
    """Return the ``vvp`` binary to invoke (PATH lookup, ``vvp`` fallback).

    The image is data, not an executable, so — unlike the Verilator run-half
    which execs ``V<top>`` directly — the Icarus run-half invokes the ``vvp``
    interpreter on it. ``vvp`` ships alongside ``iverilog`` and is on PATH in the
    Sandbox image (Phase 0).
    """
    return shutil.which("vvp") or "vvp"


def _build_vvp_cmd(
    vvp_bin: str,
    build_dir: Path,
    image: str,
    plusargs: list[str] | None,
) -> list[str]:
    """Assemble the ``vvp`` command line (mirrors edalize's run target, minus -fst).

    ``-n`` non-interactive; ``-M<build_dir>`` so the VPI modules resolve when the
    cwd is the project sim dir rather than the build dir; the image is given as
    an absolute path for the same reason. ``-fst`` is **omitted** so the dump is
    a VCD (``dump.vcd``) the existing VCD→bwave postprocess understands.
    """
    cmd = [vvp_bin, "-n", f"-M{build_dir}", str(build_dir / image)]
    for pa in plusargs or []:
        # A '+…' token is a plusarg ($value$plusargs); a '-…'/'--…' token is a
        # getopt argument forwarded verbatim (SETUP-7).
        cmd.append(pa if pa.startswith(("+", "-")) else f"+{pa}")
    return cmd


def _new_trace_session(work_dir: Path, trace_scope: str | None) -> TraceSession:
    """Create an iverilog-backed trace session (tolerates older signatures)."""
    try:
        return TraceSession(work_dir, trace_scope, backend="iverilog")
    except TypeError:  # pragma: no cover - defensive against fakes in tests
        return TraceSession(work_dir, trace_scope)


def _stream_output(  # noqa: PLR0915 — linear spawn+watchdog+guard+drain pipeline; splitting it would strand shared proc/timer/guard state
    cmd: list[str],
    run_cwd: Path,
    timeout: int,
    max_rundir_bytes: int = 0,
    work_dir: Path | None = None,
    runtime_inputs: tuple[str, ...] = (),
) -> tuple[deque[str], subprocess.Popen, RunTermination]:
    """Run vvp from *run_cwd*, stream stdout live, enforce *timeout* (seconds).

    Uses a watchdog timer rather than stdout-activity polling so a silent hung
    vvp is still killed at the wall-clock budget (matches the legacy runner).

    Two safety guards ride alongside the timeout (see :mod:`booley.flows.sim.run_guard`):
    a missing-``$readmemh``-file warning is fatal (SETUP-23 — vvp would otherwise
    spin forever on uninitialised RAM, silent), and *max_rundir_bytes* (>0) caps
    how large *run_cwd* may grow before a runaway ``$dumpfile``/``$fwrite`` sink
    is killed (SETUP-25).
    """
    import threading

    from booley.flows.sim.run_guard import (
        DiskBudgetGuard,
        child_death_kwargs,
        classify_readmemh_termination,
        parse_readmemh_fatal,
        snapshot_dir_baseline,
        supervise_child,
    )
    from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs

    lines: deque[str] = deque(maxlen=5_000)
    cpu_started = child_cpu_snapshot()
    # BEFORE the spawn: the baseline walk takes seconds on the multi-GB trees
    # this budget exists for, and anything vvp dumps during that walk would
    # otherwise land in the baseline free of charge (fpu F-23).
    disk_baseline = snapshot_dir_baseline(run_cwd, max_rundir_bytes)
    proc = subprocess.Popen(
        cmd,
        cwd=str(run_cwd),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_new_group_kwargs(),
        **child_death_kwargs(),
    )
    supervise_child(proc)  # F-13: reapable if our parent dies under us
    timed_out = {"hit": False}

    def _kill_on_timeout() -> None:
        if proc.poll() is None:
            timed_out["hit"] = True
            kill_process_tree(proc)

    timer = threading.Timer(timeout, _kill_on_timeout)
    timer.daemon = True
    timer.start()
    guard = DiskBudgetGuard(run_cwd, max_rundir_bytes, proc, baseline=disk_baseline)
    guard.start()
    progress = RunLogProgress(work_dir, time.monotonic())
    readmemh_error = ""
    stdout = proc.stdout
    try:
        assert stdout is not None
        for line in stdout:
            print(line, end="", flush=True)
            lines.append(line)
            progress.observe(lines)  # F-18: run.log shows a live tail mid-run
            # SETUP-23: a missing $readmemh init file warns once then vvp spins
            # forever on uninitialised RAM — treat that warning as fatal.
            missing = parse_readmemh_fatal(line)
            if missing and proc.poll() is None:
                fatal = classify_readmemh_termination(missing, run_cwd, runtime_inputs)
                readmemh_error = fatal.detail
                kill_process_tree(proc)
                break
        proc.wait()
    finally:
        if stdout is not None:
            stdout.close()
        timer.cancel()
        guard.stop()
        progress.final_flush(lines)
        append_child_cpu_marker(lines, cpu_started)

    if not readmemh_error and not timed_out["hit"] and not guard.tripped:
        guard.finish()

    # Precedence: an explicit fatal (readmemh) over the disk runaway over the
    # wall-clock timeout — the first is the most specific cause of death.
    termination = RunTermination()
    if readmemh_error:
        msg = f"ERROR: {readmemh_error}"
        print(msg)
        lines.append(msg + "\n")
        termination = fatal
    elif guard.tripped:
        print(guard.message)
        lines.append(guard.message + "\n")
        termination = RunTermination("disk_budget", guard.message, "infrastructure")
    elif timed_out["hit"]:
        # F-21: name the silence, so "slow" and "wedged with no sentinel" read
        # differently in the verdict tail.
        msg = f"ERROR: iverilog simulation timed out ({timeout}s)" + format_idle_note(
            progress.idle_s, len(lines)
        )
        print(msg)
        lines.append(msg + "\n")
        termination = RunTermination("timeout", msg, "timeout")
    return lines, proc, termination


def _evaluate_verdict(
    output: str,
    returncode: int,
    work_dir: Path,
    *,
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
    termination: RunTermination | None = None,
) -> None:
    """Print the ``[SIM_SUMMARY]`` verdict + write result JSON (legacy parity)."""
    termination = termination or RunTermination()
    assessment = assess_native_verdict(
        output,
        returncode,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        termination=termination,
    )
    vrfc = extract_vrfc_warnings(output)
    print(
        format_summary(
            assessment.passed,
            assessment.sva_errors,
            vrfc,
            inconclusive=assessment.inconclusive,
        )
    )
    print(native_verdict_message("iverilog", assessment, returncode, termination))
    first_err = first_native_failure(output, termination.detail) if not assessment.passed else ""
    write_result_json(
        work_dir,
        assessment.passed,
        assessment.sva_errors,
        first_err,
        returncode,
        inconclusive=assessment.inconclusive,
    )
    # Persist the raw output next to result.json on pass AND fail:
    # result.json only carries a 500-char first_error, so run.log is what
    # survives MCP-level stdout truncation.
    write_run_log(work_dir, output)


def _prepare_icarus_run(
    build_dir: Path,
    run_cwd: Path | None,
    work_dir: Path | None,
    plusargs: list[str],
    vcd: bool,
    trace_scope: str | None,
    trace_files: list[str],
) -> _IcarusRun | str:
    build = Path(build_dir).resolve()
    run = Path(run_cwd).resolve() if run_cwd is not None else build
    work = Path(work_dir).resolve() if work_dir is not None else build
    work.mkdir(parents=True, exist_ok=True)
    image = find_icarus_image(build)
    if image is None:
        return f"ERROR: no vvp image (*.scr) found in {build}"
    if vcd and not any(value.lstrip("+") == "trace" for value in plusargs):
        plusargs.append("+trace")
    command = _build_vvp_cmd(_find_vvp(), build, image, plusargs)
    trace = _new_trace_session(work, trace_scope) if vcd else None
    current = (
        CurrentRunTraces.begin(
            trace,
            raw_trace_paths=(run / _DEFAULT_VCD_NAME,),
            patterns=tuple(trace_files),
            roots=tuple(_trace_roots(run, work, build)),
        )
        if trace is not None
        else None
    )
    return _IcarusRun(build, run, work, image, command, trace, current)


def _execute_icarus_run(
    run: _IcarusRun,
    timeout: int,
    max_rundir_bytes: int,
    runtime_inputs: tuple[str, ...],
):
    from booley.harness.colors import dim
    from booley.presentation.heartbeat import render_heartbeat
    from booley.runtime.heartbeat import Heartbeat

    print(f"\n{'=' * 60}")
    print(f"[iverilog simulation: {run.image}]")
    print(f"{'=' * 60}")
    print(f"CWD: {run.run_cwd}")
    print(f"CMD: {' '.join(run.command)}\n")
    heartbeat = Heartbeat("iverilog sim", render=partial(render_heartbeat, style=dim), interval=60)
    heartbeat.start()
    try:
        return _stream_output(
            run.command,
            run.run_cwd,
            timeout,
            max_rundir_bytes=max_rundir_bytes,
            work_dir=run.work_dir,
            runtime_inputs=runtime_inputs,
        )
    finally:
        heartbeat.stop()


def _finalize_icarus_trace(
    run: _IcarusRun,
    proc,
    trace_files: list[str],
) -> tuple[str, AdapterTraceResult | None]:
    if run.trace is None or run.current is None:
        return "", None
    run.trace.postprocess(run.run_cwd / _DEFAULT_VCD_NAME)
    selection = run.current.select()
    if selection.ambiguous:
        return _icarus_trace_incident(run, proc, ambiguous_trace_reason(selection.ambiguous))
    found = selection.path
    if found is not None:
        inspection = run.trace.inspect(found)
        if not inspection.usable:
            if found.suffix.lower() == ".vcd":
                reason = f"native FST conversion unavailable: {inspection.failure_reason}"
                print(f"TRACE_FALLBACK: {found}")
                return (
                    f"\nTRACE_FALLBACK: {found}",
                    AdapterTraceResult("ok", path=str(found), detail=reason),
                )
            reason = (
                "trace requested but retained waveform is not queryable: "
                f"{inspection.failure_reason}"
            )
            return _icarus_trace_incident(run, proc, reason)
        print(f"TRACE_OK: {found}")
        return f"\nTRACE_OK: {found}", AdapterTraceResult("ok", path=str(found))
    reason = "trace requested but no queryable .fst store or .vcd was produced"
    if trace_files:
        reason += (
            " (no fresh, nonempty file matching [flows.sim].trace_files "
            f"{trace_files} was produced by the current run)"
        )
    else:
        reason += (
            " (a testbench writing its dump under its own name needs "
            "[flows.sim].trace_files to declare it)"
        )
    return _icarus_trace_incident(run, proc, reason)


def _icarus_trace_incident(run: _IcarusRun, proc, reason: str) -> tuple[str, AdapterTraceResult]:
    """Record a trace incident and return its output line and adapter result."""
    assert run.trace is not None
    incident = run.trace.write_incident(reason, sim_proc=proc)
    print(f"ERROR: {reason}")
    print(f"TRACE_INCIDENT: {incident}")
    return (
        f"\nERROR: {reason}\nTRACE_INCIDENT: {incident}",
        AdapterTraceResult("incident", path=str(incident), detail=reason),
    )


def _icarus_setup_failure(
    detail: str,
    transport: AdapterTransportIdentity | None,
    pass_sentinels: list[str] | None,
    fail_sentinels: list[str] | None,
) -> NativeRun:
    print(detail)
    return _publish_adapter_result(
        transport,
        detail,
        1,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        failure_kind="infrastructure",
        detail=detail,
    )


def run_icarus_image(
    *,
    build_dir: Path,
    run_cwd: Path | None = None,
    work_dir: Path | None = None,
    vcd: bool = False,
    plusargs: list[str] | None = None,
    timeout: int = 600,
    trace_scope: str | None = None,
    trace_files: list[str] | None = None,
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
    max_rundir_bytes: int = 0,
    runtime_inputs: tuple[str, ...] = (),
    transport: AdapterTransportIdentity | None = None,
) -> NativeRun:
    """Run an edalize-built vvp image once and return its output and verdict."""
    declared = list(trace_files or [])
    run = _prepare_icarus_run(
        build_dir,
        run_cwd,
        work_dir,
        list(plusargs or []),
        vcd,
        trace_scope,
        declared,
    )
    if isinstance(run, str):
        return _icarus_setup_failure(run, transport, pass_sentinels, fail_sentinels)
    lines, proc, termination = _execute_icarus_run(run, timeout, max_rundir_bytes, runtime_inputs)
    output = "".join(lines)
    _evaluate_verdict(
        output,
        proc.returncode,
        run.work_dir,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        termination=termination,
    )
    trace_output, trace_result = _finalize_icarus_trace(run, proc, declared)
    output += trace_output
    return _publish_adapter_result(
        transport,
        output,
        proc.returncode,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        trace_required=vcd,
        trace=trace_result,
        termination=termination,
    )


def _run_work(work: PreparedSimulationWork) -> int:
    """Run the prepared *work* as this process's one Icarus launch."""
    # Entry-point only (never the in-process API): tie this supervisor's life to
    # its parent's so simulate's wrapper timeout cannot orphan it (F-13).
    from booley.flows.sim.run_guard import install_parent_death_guard

    install_parent_death_guard()
    run = run_icarus_image(
        build_dir=Path(work.build_dir),
        run_cwd=Path(work.run_cwd) if work.run_cwd else None,
        work_dir=Path(work.work_dir) if work.work_dir else None,
        vcd=work.trace,
        plusargs=list(work.plusargs),
        timeout=work.timeout_s,
        trace_files=list(work.trace_files) if work.trace and work.trace_files else None,
        pass_sentinels=list(work.pass_sentinels) or None,
        fail_sentinels=list(work.fail_sentinels) or None,
        max_rundir_bytes=work.max_rundir_bytes,
        runtime_inputs=work.runtime_inputs,
        transport=work_transport_identity(work),
    )
    return run.exit_code


def main(argv: list[str] | None = None) -> int:
    return run_adapter_entry(sys.argv[1:] if argv is None else argv, _run_work)


if __name__ == "__main__":  # pragma: no cover - subprocess entry-point
    raise SystemExit(main())
