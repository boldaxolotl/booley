#!/usr/bin/env python3
"""Run an already-built Icarus (vvp) sim image — the edalize-era Icarus run-half.

ADR 0019/0022: FuseSoC+Edalize own the *build* (``fusesoc run --setup`` →
``make`` runs ``iverilog`` and produces the vvp image named ``<edam-name>`` with
a sibling ``<name>.scr``); Booley owns the *run* — it executes that image with
``vvp``, optionally postprocesses a VCD into a queryable B-Wave, and re-emits the
``[SIM_SUMMARY]`` summary line for log and agent consumption.

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
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.flows.sim.adapter_contract import PreparedSimulationWork, run_adapter_entry
from booley.flows.sim.adapter_transport import (
    AdapterTransportIdentity,
    NativeRun,
    evaluate_native_verdict,
    publish_native_adapter_result,
    work_transport_identity,
)
from booley.flows.sim.backends.shared import (
    BOOLEY_DUMP_VCD_NAME,
    CurrentRunTraces,
    find_icarus_image,
)
from booley.flows.sim.backends.supervision import SimulatorLaunch, run_supervised
from booley.flows.sim.backends.trace_finalization import (
    finalize_trace,
    native_pending_publisher,
)
from booley.flows.sim.config import DEFAULT_SIM_TRACE_CONVERT_TIMEOUT_S
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
    trace = TraceSession(work, trace_scope, backend="iverilog") if vcd else None
    current = (
        CurrentRunTraces.begin(
            trace,
            raw_trace_paths=(run / _DEFAULT_VCD_NAME,),
            patterns=tuple(trace_files),
            roots=tuple(_trace_roots(run, work, build)),
            stream_from=run,
        )
        if trace is not None
        else None
    )
    return _IcarusRun(build, run, work, image, command, current)


def _execute_icarus_run(
    run: _IcarusRun,
    timeout: int,
    max_rundir_bytes: int,
    runtime_inputs: tuple[str, ...],
):
    print(f"\n{'=' * 60}")
    print(f"[iverilog simulation: {run.image}]")
    print(f"{'=' * 60}")
    print(f"CWD: {run.run_cwd}")
    print(f"CMD: {' '.join(run.command)}\n")
    try:
        return run_supervised(
            SimulatorLaunch(
                label="iverilog",
                command=run.command,
                run_cwd=run.run_cwd,
                env=os.environ.copy(),
                timeout_s=timeout,
                work_dir=run.work_dir,
                max_rundir_bytes=max_rundir_bytes,
                runtime_inputs=runtime_inputs,
                streams=run.current.streams if run.current is not None else None,
            )
        )
    finally:
        if run.current is not None:
            run.current.close_streams()


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
    trace_convert_timeout: int = DEFAULT_SIM_TRACE_CONVERT_TIMEOUT_S,
) -> NativeRun:
    """Run an edalize-built vvp image once and return its output and verdict."""
    run = _prepare_icarus_run(
        build_dir,
        run_cwd,
        work_dir,
        list(plusargs or []),
        vcd,
        trace_scope,
        list(trace_files or []),
    )
    if isinstance(run, str):
        return _icarus_setup_failure(run, transport, pass_sentinels, fail_sentinels)
    lines, proc, termination = _execute_icarus_run(run, timeout, max_rundir_bytes, runtime_inputs)
    output = "".join(lines)
    evaluate_native_verdict(
        "iverilog",
        output,
        proc.returncode,
        run.work_dir,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        termination=termination,
    )
    trace_result = None
    if run.current is not None:
        trace_output, trace_result = finalize_trace(
            run.current,
            proc,
            convert_timeout_s=trace_convert_timeout,
            publish_pending=native_pending_publisher(
                transport,
                output,
                proc.returncode,
                pass_sentinels=pass_sentinels,
                fail_sentinels=fail_sentinels,
                termination=termination,
            ),
            raw_vcd=run.run_cwd / _DEFAULT_VCD_NAME,
        )
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
        trace_convert_timeout=work.trace_convert_timeout_s,
    )
    return run.exit_code


def main(argv: list[str] | None = None) -> int:
    return run_adapter_entry(sys.argv[1:] if argv is None else argv, _run_work)


if __name__ == "__main__":  # pragma: no cover - subprocess entry-point
    raise SystemExit(main())
