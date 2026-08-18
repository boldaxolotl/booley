#!/usr/bin/env python3
"""Run an already-built Icarus (vvp) sim image — the edalize-era Icarus run-half.

ADR 0019/0022: FuseSoC+Edalize own the *build* (``fusesoc run --setup`` →
``make`` runs ``iverilog`` and produces the vvp image named ``<edam-name>`` with
a sibling ``<name>.scr``); Booley owns the *run* — it executes that image with
``vvp``, optionally postprocesses a VCD into a queryable B-Wave, and re-emits the
``[SIM_SUMMARY]`` verdict sentinel the criteria layer scrapes.

This is the Icarus counterpart of :mod:`booley.sim.verilator_run` and the
edalize successor to the legacy ``run_iverilog_sim`` / ``run_sim_batch`` Icarus
runners. Like the Verilator run-half it is a **self-contained subprocess
entry-point** (``python -m booley.sim.iverilog_run …``) so a Booley Flow can ship the
whole run across the host/sandbox boundary via ``BooleyFlow._execute``.

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
design-description: the build dir is passed in (``--build-dir``), so once every
Icarus consumer is on this path the legacy file-list registry can be deleted.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from booley.sim.sim_result import (
    count_sva_errors,
    extract_vrfc_warnings,
    format_summary,
    parse_sim_verdict,
    write_result_json,
    write_run_log,
)
from booley.sim.trace_session import TraceSession

# The dump module's explicit ``$dumpfile`` name — identical to iverilog's
# default with ``-fst`` omitted. vvp writes it into the run cwd.
_DEFAULT_VCD_NAME = "dump.vcd"


def _find_vvp() -> str:
    """Return the ``vvp`` binary to invoke (PATH lookup, ``vvp`` fallback).

    The image is data, not an executable, so — unlike the Verilator run-half
    which execs ``V<top>`` directly — the Icarus run-half invokes the ``vvp``
    interpreter on it. ``vvp`` ships alongside ``iverilog`` and is on PATH in the
    Sandbox image (Phase 0).
    """
    return shutil.which("vvp") or "vvp"


def _find_image(build_dir: Path) -> str | None:
    """Locate the vvp image name in *build_dir* via its ``<name>.scr`` sibling.

    Edalize emits one ``.scr`` next to the image; the image is that stem with no
    extension. Returns the bare image name (not a path), or ``None`` when no
    ``.scr`` is present (an unbuilt / non-Icarus build dir).
    """
    scripts = sorted(build_dir.glob("*.scr"))
    if not scripts:
        return None
    if len(scripts) > 1:
        print(
            f"WARNING: multiple .scr files in {build_dir}; using {scripts[0].name}",
            file=sys.stderr,
        )
    return scripts[0].stem


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
) -> tuple[deque[str], subprocess.Popen]:
    """Run vvp from *run_cwd*, stream stdout live, enforce *timeout* (seconds).

    Uses a watchdog timer rather than stdout-activity polling so a silent hung
    vvp is still killed at the wall-clock budget (matches the legacy runner).

    Two safety guards ride alongside the timeout (see :mod:`booley.sim.run_guard`):
    a missing-``$readmemh``-file warning is fatal (SETUP-23 — vvp would otherwise
    spin forever on uninitialised RAM, silent), and *max_rundir_bytes* (>0) caps
    how large *run_cwd* may grow before a runaway ``$dumpfile``/``$fwrite`` sink
    is killed (SETUP-25).
    """
    import threading

    from booley.platform_paths import kill_process_tree, popen_new_group_kwargs
    from booley.sim.run_guard import (
        DiskBudgetGuard,
        child_death_kwargs,
        readmemh_fatal_line,
        snapshot_dir_baseline,
        supervise_child,
    )
    from booley.sim.verilator_run import RunLogProgress, format_idle_note

    lines: deque[str] = deque(maxlen=5_000)
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
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            lines.append(line)
            progress.observe(lines)  # F-18: run.log shows a live tail mid-run
            # SETUP-23: a missing $readmemh init file warns once then vvp spins
            # forever on uninitialised RAM — treat that warning as fatal.
            fatal = readmemh_fatal_line(line)
            if fatal:
                readmemh_error = f"missing $readmemh memory-init file — {fatal}"
                kill_process_tree(proc)
                break
        proc.wait()
    finally:
        timer.cancel()
        guard.stop()
        progress.final_flush(lines)

    # Precedence: an explicit fatal (readmemh) over the disk runaway over the
    # wall-clock timeout — the first is the most specific cause of death.
    if readmemh_error:
        msg = f"ERROR: {readmemh_error}"
        print(msg)
        lines.append(msg + "\n")
    elif guard.tripped:
        print(guard.message)
        lines.append(guard.message + "\n")
    elif timed_out["hit"]:
        # F-21: name the silence, so "slow" and "wedged with no sentinel" read
        # differently in the verdict tail.
        msg = f"ERROR: iverilog simulation timed out ({timeout}s)" + format_idle_note(
            progress.idle_s, len(lines)
        )
        print(msg)
        lines.append(msg + "\n")
    return lines, proc


def _check_dut_info_diagnostics(
    combined_output: str,
    dut_info: Any = None,
) -> str | None:
    """Scan elab stdout+stderr for known dut_info-mismatch patterns.

    Returns a human-readable message naming the suspected stale field, or None
    when no pattern matches. Patterns are empirical — iverilog's wording is
    fairly stable but may shift across versions. (Relocated from the retired
    ``run_iverilog_sim`` so simulate's Icarus diagnostic survives that deletion.)

    Pattern catalogue:
      * "Unable to bind variable" / "Unable to find" — the $dumpvars hier path
        bound at elab time did not resolve, indicating dut_hier_path is stale.
      * "Unknown module" / "Cannot find module" — the -s top_module flag could
        not resolve, indicating tb_top_module is stale.
    """
    if not combined_output:
        return None
    # dut_hier_path mismatches surface from $dumpvars binding failures.
    hier_markers = ("Unable to bind variable", "Unable to find")
    # tb_top_module mismatches surface from the top-level -s flag.
    top_markers = ("Unknown module", "Cannot find module")

    matched_lines: list[str] = []
    matched_field: str | None = None
    for line in combined_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if matched_field is None and any(m in stripped for m in hier_markers):
            matched_field = "dut_hier_path"
        if matched_field is None and any(m in stripped for m in top_markers):
            matched_field = "tb_top_module"
        if any(m in stripped for m in hier_markers + top_markers):
            matched_lines.append(stripped)

    if matched_field is None:
        return None

    expected = ""
    if dut_info is not None:
        if matched_field == "dut_hier_path":
            expected = getattr(dut_info, "dut_hier_path", "") or ""
        elif matched_field == "tb_top_module":
            expected = getattr(dut_info, "tb_top_module", "") or ""

    snippet = "\n".join(matched_lines[:5])
    parts = [f"dut_info stale: {matched_field} in state does not match elaborated design."]
    if expected:
        parts.append(f"Expected: {expected}")
    parts.append(f"Diagnostic: {snippet}")
    parts.append("Correct dut_info.")
    return "\n".join(parts)


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
        print("\niverilog sim INCONCLUSIVE (rc=0, no sentinel)")
    elif passed:
        print(f"\niverilog sim PASSED (rc={returncode})")
    elif verdict is False:
        # A FAIL sentinel matched. vvp can still exit 0 (e.g. an SVA/$error
        # that reports but doesn't $fatal), so cite rc only when it is
        # actually nonzero — never print the maximally-confusing "(rc=0)".
        reason = f"rc={returncode}" if returncode else "fail sentinel matched"
        print(f"\niverilog sim FAILED ({reason})")
    elif sva_errors > 0:
        print(f"\niverilog sim FAILED ({sva_errors} SVA assertion errors)")
    else:
        print(f"\niverilog sim FAILED (rc={returncode})")

    first_err = ""
    if not passed:
        for ln in output.splitlines():
            if re.search(r"(?:failed|fatal|error|mismatch)", ln, re.IGNORECASE):
                first_err = ln.strip()
                break
    write_result_json(
        work_dir, passed, sva_errors, first_err, returncode, inconclusive=inconclusive
    )
    # Persist the raw output next to result.json on pass AND fail:
    # result.json only carries a 500-char first_error, so run.log is what
    # survives MCP-level stdout truncation.
    write_run_log(work_dir, output)


def run_icarus_image(  # noqa: PLR0915 — linear vvp run+capture pipeline: build args, spawn, poll, parse, write result
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
) -> str:
    """Run the edalize-built vvp image once; return the captured output.

    *build_dir* holds the image + ``.scr`` + VPI modules; *run_cwd* is the
    directory to run ``vvp`` from (a TB that opens vectors relative to cwd needs
    the project's sim cwd), defaulting to *build_dir*; *work_dir* is the
    trace/result output dir (where the ``.fst`` store and ``result.json`` land,
    and what ``TraceSession.find()`` discovers), defaulting to *build_dir*.

    Tracing is **file → postprocess** (no FIFO, see module docstring): vvp writes
    ``dump.vcd`` into *run_cwd*, which is then fed through the iverilog
    ``TraceSession`` VCD→bwave postprocess.
    """
    from booley.heartbeat import Heartbeat

    # Resolve every path to absolute up front, while cwd is still the project
    # root: --build-dir/--work-dir/--run-cwd arrive relative (so they cross the
    # host/sandbox boundary), but vvp runs from --run-cwd — where a relative
    # build-dir, image path, or trace dir would miss.
    build_dir = Path(build_dir).resolve()
    run_cwd = Path(run_cwd).resolve() if run_cwd is not None else build_dir
    work_dir = Path(work_dir).resolve() if work_dir is not None else build_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    image = _find_image(build_dir)
    if image is None:
        msg = f"ERROR: no vvp image (*.scr) found in {build_dir}"
        print(msg)
        return msg

    # booley_vcd_dump.sv triggers $dumpvars(0) only when +trace is set, so the
    # run-half owns that coupling: a trace run always passes +trace to vvp (the
    # caller just requests --trace). Mirrors how the Verilator run-half owns its
    # own trace flags.
    plusargs = list(plusargs or [])
    if vcd and not any(pa.lstrip("+") == "trace" for pa in plusargs):
        plusargs.append("+trace")

    cmd = _build_vvp_cmd(_find_vvp(), build_dir, image, plusargs)
    trace = _new_trace_session(work_dir, trace_scope) if vcd else None
    if trace:
        trace.reset_for_run((run_cwd / _DEFAULT_VCD_NAME,))

    print(f"\n{'=' * 60}")
    print(f"[iverilog simulation: {image}]")
    print(f"{'=' * 60}")
    print(f"CWD: {run_cwd}")
    print(f"CMD: {' '.join(cmd)}\n")

    hb = Heartbeat("iverilog sim", interval=60)
    hb.start()
    try:
        lines, proc = _stream_output(
            cmd,
            run_cwd,
            timeout,
            max_rundir_bytes=max_rundir_bytes,
            work_dir=work_dir,
        )
    finally:
        hb.stop()

    output = "".join(lines)
    _evaluate_verdict(
        output,
        proc.returncode,
        work_dir,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
    )

    if vcd and trace:
        # $dumpfile/$dumpvars drop dump.vcd in the run cwd; hand it to the
        # VCD→bwave postprocess, then verify a queryable artifact landed.
        trace.postprocess(run_cwd / _DEFAULT_VCD_NAME)
        found = trace.find()
        if found is None and trace_files:
            # F-22: the TB writes its dump under a name only the project knows
            # ([flows.sim].trace_files).
            from booley.sim.verilator_run import adopt_declared_trace_files

            found = adopt_declared_trace_files(trace, trace_files, [run_cwd, work_dir, build_dir])
        if found is None:
            reason = "trace requested but no queryable .fst store or .vcd was produced"
            if not trace_files:
                reason += (
                    " (a testbench writing its dump under its own name needs "
                    "[flows.sim].trace_files to declare it)"
                )
            incident = trace.write_incident(reason, sim_proc=proc)
            print(f"ERROR: {reason}")
            print(f"TRACE_INCIDENT: {incident}")
            output += f"\nERROR: {reason}\nTRACE_INCIDENT: {incident}"
        else:
            # Positive assertion mirroring verilator_run: a queryable waveform
            # really landed. simulate's --trace path scrapes TRACE_OK so a
            # silently-traceless run can no longer pass as success.
            print(f"TRACE_OK: {found}")
            output += f"\nTRACE_OK: {found}"

    return output


def _positive_int(value: str) -> int:
    """argparse type: reject non-positive timeouts at the CLI boundary.

    A negative/zero ``--timeout`` reaches threading.Timer and would kill the
    simulation instantly, so validate range here rather than misbehave later.
    """
    ivalue = int(value)  # raises ValueError -> argparse reports the bad token
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {ivalue}")
    return ivalue


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run an edalize-built Icarus vvp image")
    p.add_argument(
        "--build-dir", required=True, help="dir holding the vvp image + .scr (edalize build dir)"
    )
    p.add_argument("--run-cwd", default=None, help="cwd to run vvp from (TB vector/firmware base)")
    p.add_argument("--work-dir", default=None, help="trace/result output dir")
    p.add_argument("--timeout", type=_positive_int, default=600, help="run timeout (seconds)")
    p.add_argument(
        "--max-rundir-bytes",
        type=int,
        default=0,
        dest="max_rundir_bytes",
        help="kill the run if the run dir exceeds this many bytes (0=off; SETUP-25)",
    )
    p.add_argument("--trace", action="store_true", help="postprocess a B-Wave trace from dump.vcd")
    p.add_argument("--trace-scope", default=None, help="scope the trace to a hierarchy")
    p.add_argument(
        "--trace-file",
        action="append",
        default=[],
        dest="trace_files",
        help="path (relative to --run-cwd/--work-dir/--build-dir, or absolute; "
        "globs allowed) where the testbench writes its own dump. Repeatable; "
        "checked when Booley's own trace artifacts are absent",
    )
    p.add_argument(
        "--plusarg",
        action="append",
        default=[],
        dest="plusargs",
        help="plusarg passed to vvp (repeatable; +-prefix optional)",
    )
    # Project-configured verdict sentinels (booley.toml [flows.sim]); the
    # host side of simulate forwards these so a project keeps its own TB wording.
    p.add_argument(
        "--pass-sentinel",
        action="append",
        default=[],
        dest="pass_sentinels",
        help="substring marking a PASS (repeatable; overrides the default)",
    )
    p.add_argument(
        "--fail-sentinel",
        action="append",
        default=[],
        dest="fail_sentinels",
        help="substring marking a FAIL (repeatable; overrides the default)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Entry-point only (never the in-process API): tie this supervisor's life to
    # its parent's so simulate's wrapper timeout cannot orphan it (F-13).
    from booley.sim.run_guard import install_parent_death_guard

    install_parent_death_guard()
    output = run_icarus_image(
        build_dir=Path(args.build_dir),
        run_cwd=Path(args.run_cwd) if args.run_cwd else None,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        vcd=args.trace,
        plusargs=args.plusargs,
        timeout=args.timeout,
        trace_scope=args.trace_scope,
        trace_files=args.trace_files or None,
        pass_sentinels=args.pass_sentinels or None,
        fail_sentinels=args.fail_sentinels or None,
        max_rundir_bytes=args.max_rundir_bytes,
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
