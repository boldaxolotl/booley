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
Target-owned native-FST lifecycle — as one Sandbox subprocess.

Unlike the legacy runner it does **not** build anything and reads no
``configs.toml``/``build_file_list`` design-description: the binary location is
passed in (the prepared work document's build dir, the edalize build dir holding
``V<top>``), so once every legacy consumer is on this path the legacy file-list
registry can be deleted (Unit 6).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from booley.bwave.waveform_store import StreamingConversion
from booley.flows.run_log import write_run_log
from booley.flows.sim.adapter_contract import PreparedSimulationWork, run_adapter_entry
from booley.flows.sim.adapter_transport import (
    AdapterTraceResult,
    AdapterTransportIdentity,
    NativeRun,
    evaluate_native_verdict,
    publish_native_adapter_result,
    work_transport_identity,
)
from booley.flows.sim.backends.shared import (
    CurrentRunTraces,
    RunTermination,
)
from booley.flows.sim.backends.supervision import SimulatorLaunch, run_supervised
from booley.flows.sim.backends.trace_finalization import finalize_trace
from booley.flows.sim.result import format_infra_error
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.sim.trace_session import TraceSession

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
    the wrong convention is silent — the binary ignores the unknown plusarg
    and the dump contains nothing but a header — yet the traced run still ends
    inconclusive rather than passing. So the contract is configurable
    (``[flows.sim].trace_args``), with Booley's own convention as the default.

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


def _setup_bwave(
    trace: TraceSession,
    cmd: list[str],
    trace_args: list[str] | None = None,
) -> StreamingConversion | None:
    """Start the FIFO streamer and append the project's trace arguments."""
    conversion = trace.start_fifo()
    cmd.extend(_render_trace_args(trace_args, trace.fifo_path if conversion else None))
    return conversion


def _setup_trace(
    trace: TraceSession,
    cmd: list[str],
    trace_args: list[str] | None,
    trace_mode: TraceMode,
) -> StreamingConversion | None:
    """Configure the selected trace adapter and append its run arguments."""
    if trace_mode is TraceMode.NATIVE_FST:
        cmd.extend(_render_trace_args(trace_args, trace.work_bwave_path))
        return None
    return _setup_bwave(trace, cmd, trace_args)


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
    current: CurrentRunTraces | None
    mode: TraceMode
    conversion: StreamingConversion | None


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
    session = TraceSession(paths.work_dir, trace_scope, backend="verilator") if enabled else None
    current = (
        CurrentRunTraces.begin(
            session,
            patterns=tuple(trace_files or ()),
            roots=(paths.run_cwd, paths.work_dir, paths.bin_dir),
        )
        if session is not None
        else None
    )
    conversion = (
        _setup_trace(session, cmd, trace_args, trace_mode) if session is not None else None
    )
    return _TraceRuntime(
        session=session,
        current=current,
        mode=trace_mode,
        conversion=conversion,
    )


def _execute_supervised(
    cmd: list[str],
    paths: _RunPaths,
    env: dict[str, str],
    timeout: int,
    trace: _TraceRuntime,
    max_rundir_bytes: int,
    runtime_inputs: tuple[str, ...],
) -> tuple[deque[str], subprocess.Popen, RunTermination]:
    """Execute one supervised simulator process and clean up its trace FIFO."""
    try:
        return run_supervised(
            SimulatorLaunch(
                label="Verilator",
                command=cmd,
                run_cwd=paths.run_cwd,
                env=env,
                timeout_s=timeout,
                work_dir=paths.work_dir,
                max_rundir_bytes=max_rundir_bytes,
                runtime_inputs=runtime_inputs,
                trace=trace.session,
                conversion=trace.conversion,
            )
        )
    finally:
        if trace.session is not None and trace.mode is TraceMode.VCD_FIFO:
            trace.session.cleanup_fifo(trace.conversion)


def _finalize_verilated_run(
    lines: deque[str],
    proc: subprocess.Popen,
    paths: _RunPaths,
    trace: _TraceRuntime,
    pass_sentinels: list[str] | None,
    fail_sentinels: list[str] | None,
    termination: RunTermination,
) -> tuple[str, AdapterTraceResult | None]:
    """Persist the verdict and validate any trace produced by the current run."""
    output = "".join(lines)
    evaluate_native_verdict(
        "Verilator",
        output,
        proc.returncode,
        paths.work_dir,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        termination=termination,
    )
    if trace.current is None:
        return output, None
    raw_vcd = paths.work_dir / "trace.vcd"
    trace_output, trace_result = finalize_trace(
        trace.current,
        proc,
        conversion=trace.conversion,
        raw_vcd=raw_vcd if trace.mode is TraceMode.VCD_FIFO and trace.conversion is None else None,
    )
    return output + trace_output, trace_result


def _missing_binary_result(
    paths: _RunPaths,
    top_module: str,
    transport: AdapterTransportIdentity | None,
    pass_sentinels: list[str] | None,
    fail_sentinels: list[str] | None,
) -> NativeRun:
    output = _report_missing_binary(paths.bin_dir, top_module, paths.work_dir)
    return _publish_adapter_result(
        transport,
        output,
        1,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        failure_kind="infrastructure",
        detail=output.strip(),
    )


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
    runtime_inputs: tuple[str, ...] = (),
    transport: AdapterTransportIdentity | None = None,
) -> NativeRun:
    """Run one edalize-built ``V<top>`` under the resolved trace recipe."""
    paths = _resolve_run_paths(bin_dir, run_cwd, work_dir)
    paths.work_dir.mkdir(parents=True, exist_ok=True)
    exe = _find_binary(paths.bin_dir, top_module)
    if exe is None:
        return _missing_binary_result(paths, top_module, transport, pass_sentinels, fail_sentinels)
    scope = _resolve_single_scope(trace_scope)
    cmd, env = _build_run_cmd(exe, paths.bin_dir, plusargs)
    trace = _prepare_trace_runtime(paths, vcd, scope, trace_files, cmd, trace_args, trace_mode)
    _print_verilator_banner(top_module, paths.run_cwd, cmd)
    return _execute_verilated_and_publish(
        cmd,
        paths,
        env,
        timeout,
        trace,
        max_rundir_bytes,
        runtime_inputs,
        pass_sentinels,
        fail_sentinels,
        vcd,
        transport,
    )


def _print_verilator_banner(top_module: str, run_cwd: Path, cmd: list[str]) -> None:
    print(f"\n{'=' * 60}")
    print(f"[Verilator simulation: {top_module}]")
    print(f"{'=' * 60}")
    print(f"CWD: {run_cwd}")
    print(f"CMD: {' '.join(cmd)}\n")


def _execute_verilated_and_publish(
    cmd,
    paths,
    env,
    timeout,
    trace,
    max_rundir_bytes,
    runtime_inputs,
    pass_sentinels,
    fail_sentinels,
    vcd,
    transport,
) -> NativeRun:
    lines, proc, termination = _execute_supervised(
        cmd, paths, env, timeout, trace, max_rundir_bytes, runtime_inputs
    )
    output, trace_result = _finalize_verilated_run(
        lines,
        proc,
        paths,
        trace,
        pass_sentinels,
        fail_sentinels,
        termination,
    )
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
    """Run the prepared *work* as this process's one Verilator launch."""
    # Entry-point only (never the in-process API): tie this supervisor's life to
    # its parent's so simulate's wrapper timeout cannot orphan it (F-13).
    from booley.flows.sim.run_guard import install_parent_death_guard

    install_parent_death_guard()
    run = run_verilated_binary(
        top_module=work.top,
        bin_dir=Path(work.build_dir),
        run_cwd=Path(work.run_cwd) if work.run_cwd else None,
        work_dir=Path(work.work_dir) if work.work_dir else None,
        vcd=work.trace,
        plusargs=list(work.plusargs),
        timeout=work.timeout_s,
        trace_args=list(work.trace_args) if work.trace and work.trace_args else None,
        trace_files=list(work.trace_files) if work.trace and work.trace_files else None,
        trace_mode=TraceMode(work.trace_mode) if work.trace else TraceMode.VCD_FIFO,
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
