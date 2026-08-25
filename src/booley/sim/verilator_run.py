#!/usr/bin/env python3
"""Run an already-built Verilator sim binary (edalize-era run-half).

ADR 0019/0022: FuseSoC+Edalize own the *build* (``fusesoc run --setup`` →
``make`` produces ``V<top>``); Booley owns the *run* — it executes that binary,
optionally under a live B-Wave trace, and re-emits the ``[SIM_SUMMARY]``
verdict sentinel the criteria layer scrapes.

This module is the edalize successor to the retired legacy Verilator
run-half. It is deliberately a **self-contained subprocess entry-point**
(``python -m booley.sim.verilator_run …``) so a Booley Flow can supervise the
whole run — including the FIFO/bwave streaming lifecycle — as one Session
Runtime subprocess.

Unlike the legacy runner it does **not** build anything and reads no
``configs.toml``/``build_file_list`` design-description: the binary location is
passed in (``--bin-dir``, the edalize build dir holding ``V<top>``), so once
every legacy consumer is on this path the legacy file-list registry can be
deleted (Unit 6).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from booley.sim.sim_result import (
    count_sva_errors,
    extract_vrfc_warnings,
    format_infra_error,
    format_summary,
    parse_sim_verdict,
    write_result_json,
    write_run_log,
    write_run_log_progress,
)
from booley.sim.trace_session import TraceSession

#: How often the streaming loop refreshes run.log with a live tail (seconds).
#: Frequent enough that a poll a few seconds apart shows movement, rare enough
#: that a chatty sim does not turn the log into a write loop (fpu F-18).
RUN_LOG_PROGRESS_INTERVAL_S = 5.0


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
            "Cocotb Target. Run Cocotb Targets through booley.sim.cocotb_run "
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

    ``{file}`` interpolates the trace destination. Arguments that reference it
    are dropped when there is no destination (the non-FIFO Windows path),
    since a template with nowhere to write is not a meaningful argument.
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

    Trace arguments are appended separately by :func:`_setup_bwave`, which is
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
        from booley.sim.run_guard import supervise_child

        supervise_child(proc)
    except ImportError:  # pragma: no cover - defensive against partial installs
        pass


class RunLogProgress:
    """Periodic live-tail flush of the streamed output into ``run.log`` (F-18).

    run.log used to hold nothing but the "run in progress" placeholder for the
    whole duration of a run, so a healthy six-minute sim and a wedged one looked
    identical to anyone polling. This writes the current tail — plus elapsed and
    idle time, the hang-versus-slow signal — every few seconds. Disabled (all
    methods no-op) when no work dir is known.
    """

    def __init__(self, work_dir: Path | None, started: float) -> None:
        self._work_dir = work_dir
        self._started = started
        self._last_flush = started
        self._last_line_at = started

    @property
    def idle_s(self) -> float:
        """Seconds since the sim last printed a line."""
        return time.monotonic() - self._last_line_at

    def observe(self, lines: deque[str]) -> None:
        """Record a freshly streamed line; flush the tail when due."""
        self._last_line_at = time.monotonic()
        if self._work_dir is None:
            return
        if self._last_line_at - self._last_flush < RUN_LOG_PROGRESS_INTERVAL_S:
            return
        self._last_flush = self._last_line_at
        self._write(lines)

    def final_flush(self, lines: deque[str]) -> None:
        """Land one last tail on the way out of the streaming loop.

        The timeout/kill paths return before ``_evaluate_verdict`` writes the
        real log, so without this a killed run's log keeps a tail from up to one
        interval before the kill.
        """
        if self._work_dir is not None:
            self._write(lines)

    def _write(self, lines: deque[str]) -> None:
        # Observability only: a run.log we cannot refresh must never be the
        # reason a simulation fails.
        with contextlib.suppress(OSError):
            write_run_log_progress(
                self._work_dir,
                "".join(lines),
                elapsed_s=time.monotonic() - self._started,
                line_count=len(lines),
                idle_s=self.idle_s,
            )


def format_idle_note(idle_s: float, line_count: int) -> str:
    """Attribution suffix for a timeout: how long the sim has been silent.

    fpu F-21: a testbench whose ``$fopen`` "succeeded" on a *directory* never
    printed a sentinel and simply spun out the whole budget. "Timed out" alone
    does not distinguish that from a legitimately slow run; "timed out, last
    output 900 s ago" does — the sim stopped talking almost immediately.
    """
    if line_count == 0:
        return " — the simulation printed NO output at all"
    return f" — last output {idle_s:.0f}s ago, {line_count} line(s) total"


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

    Two safety guards ride alongside the timeout (see :mod:`booley.sim.run_guard`):
    a missing-``$readmemh``-file warning is fatal (SETUP-23 — the sim would
    otherwise spin forever on uninitialised RAM), and *max_rundir_bytes* (>0)
    caps how much *run_cwd* may grow before a runaway tracer/``$dumpfile`` is
    killed (SETUP-25).

    When *work_dir* is given, the streamed tail is flushed into its ``run.log``
    every :data:`RUN_LOG_PROGRESS_INTERVAL_S` seconds so the run is observable
    while it is still going (fpu F-18).
    """
    from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs
    from booley.sim.run_guard import (
        DiskBudgetGuard,
        child_death_kwargs,
        readmemh_fatal_line,
        snapshot_dir_baseline,
    )

    lines: deque[str] = deque(maxlen=5_000)
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

    try:
        for line in proc.stdout:
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
        watchdog.cancel()
        guard.stop()
        progress.final_flush(lines)


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


#: Largest declared ``.vcd`` this adopts by *converting* it to a queryable
#: store. ``TraceSession.postprocess`` shells out to an unbounded ``bwave
#: build``, and it runs at finalize time — after the sim's own ``--timeout`` has
#: been honoured, but still inside the caller's Flow-level ``timeout_ms``. So
#: the 4.66 GB dump this feature was written for (fpu F-22) would turn a passing
#: simulation into a timeout kill. Past the cap the dump is adopted as-is: the
#: run is still reported as having produced a waveform (F-22's actual
#: complaint), the user just converts it by hand or scopes the dump down.
_MAX_ADOPTED_VCD_BYTES = 2 * 1024**3


def adopt_declared_trace_files(
    trace: TraceSession,
    trace_files: list[str] | None,
    search_dirs: list[Path],
) -> Path | None:
    """Adopt the first declared custom trace artifact that exists; None if none do.

    A testbench that owns its C++ ``main()`` writes whatever dump file it likes,
    wherever its cwd happens to be — ``fpu.vcd`` in ``run_cwd``, say. Booley's
    checker probes only the bin dir for ``trace.{fst,fifo,vcd}`` plus the bwave
    cache, so such a run was reported as "no waveform produced" while a 4.66 GB
    VCD sat right there, with no knob to say otherwise (fpu F-22). Declaring the
    artifact is what ``[flows.sim].trace_files`` is for.

    Entries are paths relative to each of *search_dirs* in order (absolute paths
    are used as given), and may be globs. A ``.vcd`` under
    :data:`_MAX_ADOPTED_VCD_BYTES` is fed through the ``TraceSession`` VCD→bwave
    postprocess so the result is genuinely queryable; a bigger one is adopted
    raw rather than risking the caller's budget on the conversion. An artifact
    that is already a store is published as found.
    """
    for pattern in trace_files or []:
        candidate = Path(pattern)
        matches: list[Path] = []
        if candidate.is_absolute():
            # An absolute entry names one place; keep glob support for the
            # basename (a dump numbered by run, say) without re-rooting it.
            matches = sorted(p for p in candidate.parent.glob(candidate.name) if p.is_file())
        else:
            for base in search_dirs:
                matches = sorted(p for p in base.glob(pattern) if p.is_file())
                if matches:
                    break
        for match in matches:
            if not match.is_file() or (size := match.stat().st_size) == 0:
                continue
            if match.suffix.lower() == ".vcd":
                if size > _MAX_ADOPTED_VCD_BYTES:
                    # Bounded on purpose — see _MAX_ADOPTED_VCD_BYTES.
                    print(
                        f"WARNING: declared trace {match} is {size:,} bytes, over the "
                        f"{_MAX_ADOPTED_VCD_BYTES:,}-byte finalize-time conversion cap; "
                        "adopting the raw VCD instead (convert it separately with "
                        "`bwave build`, or scope the dump down)"
                    )
                    return match
                trace.postprocess(match)
                found = trace.find()
                if found is not None:
                    return found
                continue
            return match
    return None


def _finalize_trace(trace, proc, bwave_proc, trace_files=None, search_dirs=None) -> str:
    """Locate the produced waveform and emit the TRACE_OK / TRACE_INCIDENT line.

    Returns the text to append to the run's captured output.
    """
    found = trace.find()
    if found is None and trace_files:
        # F-22: the project declared where its custom main() drops the dump.
        found = adopt_declared_trace_files(trace, trace_files, search_dirs or [])
    if found is None:
        reason = "trace requested but no queryable .fst store or .vcd was produced"
        if not trace_files:
            reason += (
                " (a testbench with its own C++ main() writes its dump under a name "
                "Booley cannot guess — declare it in [flows.sim].trace_files)"
            )
        incident = trace.write_incident(reason, sim_proc=proc, bwave_proc=bwave_proc)
        print(f"ERROR: {reason}")
        print(f"TRACE_INCIDENT: {incident}")
        return f"\nERROR: {reason}\nTRACE_INCIDENT: {incident}"
    # Positive assertion: a queryable waveform really landed. Callers that
    # delegated tracing (simulate's --trace overlay path) scrape this so a
    # silently-traceless run can no longer pass as success.
    print(f"TRACE_OK: {found}")
    return f"\nTRACE_OK: {found}"


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
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
    max_rundir_bytes: int = 0,
) -> str:
    """Run the edalize-built ``V<top>`` once; return the captured output.

    *bin_dir* holds the binary; *run_cwd* is the directory to run it from (a TB
    that opens vectors/firmware relative to cwd needs the project's sim cwd),
    defaulting to *bin_dir*; *work_dir* is the trace/result output dir (where the
    ``.fst`` store and ``result.json`` land, and what ``TraceSession.find()``
    discovers), defaulting to *bin_dir*. Tracing wraps the run in a
    ``TraceSession`` FIFO/bwave stream exactly as the legacy runner did.
    """
    from booley.runtime.heartbeat import Heartbeat

    # Resolve every path to absolute up front, while cwd is still the project
    # root: --bin-dir/--work-dir/--run-cwd arrive relative so the generated
    # command is workspace-location independent. The binary runs from --run-cwd,
    # where a relative binary path, FIFO path, or trace dir would miss. The TraceSession
    # FIFO and the V<top> path must both be absolute for the run cwd switch.
    bin_dir = Path(bin_dir).resolve()
    run_cwd = Path(run_cwd).resolve() if run_cwd is not None else bin_dir
    work_dir = Path(work_dir).resolve() if work_dir is not None else bin_dir
    # The run-half owns its output dir: the trace FIFO, postprocessed store,
    # and result.json all land here. A caller-derived trace dir (e.g.
    # coverage's derive_work_dir path) may not exist yet, and os.mkfifo /
    # write_result_json would fail on a missing parent — so create it.
    work_dir.mkdir(parents=True, exist_ok=True)

    exe = _find_binary(bin_dir, top_module)
    if exe is None:
        return _report_missing_binary(bin_dir, top_module, work_dir)

    scope = _resolve_single_scope(trace_scope)

    cmd, env = _build_run_cmd(exe, bin_dir, plusargs)
    trace = _new_trace_session(work_dir, scope) if vcd else None
    if trace:
        trace.reset_for_run()
    bwave_proc, use_fifo, keepalive_fd = (
        _setup_bwave(trace, cmd, trace_args) if trace else (None, False, None)
    )

    print(f"\n{'=' * 60}")
    print(f"[Verilator simulation: {top_module}]")
    print(f"{'=' * 60}")
    print(f"CWD: {run_cwd}")
    print(f"CMD: {' '.join(cmd)}\n")

    hb = Heartbeat("Verilator sim", interval=60)
    hb.start()
    try:
        lines, proc = _stream_output(
            cmd,
            run_cwd,
            env,
            timeout,
            trace,
            bwave_proc,
            max_rundir_bytes=max_rundir_bytes,
            work_dir=work_dir,
        )
    except subprocess.TimeoutExpired as exc:
        # Raised from _stream_output's final wait, *before* lines/proc were
        # assigned here — referencing them would NameError and mask the
        # timeout. _stream_output already killed the child and stashed the
        # streamed output on the exception; report from that instead.
        print(f"ERROR: Verilator simulation timed out ({timeout}s)")
        output = (exc.output or "") + f"\nTimed out after {timeout}s"
        # _evaluate_verdict never runs on this path, so persist the streamed
        # output here — a timeout is exactly when the raw log matters most.
        write_run_log(work_dir, output)
        return output
    finally:
        hb.stop()
        if trace:
            trace.cleanup_fifo(bwave_proc, keepalive_fd)

    output = "".join(lines)
    if trace and trace.stall_killed:
        output += f"\nERROR: {trace.stall_message}"
    _evaluate_verdict(
        output,
        proc.returncode,
        work_dir,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
    )

    if vcd and not use_fifo and trace:
        trace.postprocess(work_dir / "trace.vcd")
    if trace:
        # run_cwd first: a custom main() writes its dump relative to the cwd it
        # was started in, which is exactly run_cwd (F-22).
        output += _finalize_trace(
            trace,
            proc,
            bwave_proc,
            trace_files=trace_files,
            search_dirs=[run_cwd, work_dir, bin_dir],
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run an edalize-built Verilator sim binary")
    p.add_argument("--bin-dir", required=True, help="dir holding V<top> (edalize build dir)")
    p.add_argument("--top", required=True, help="toplevel module (binary is V<top>)")
    p.add_argument("--run-cwd", default=None, help="cwd to run the binary from")
    p.add_argument("--work-dir", default=None, help="trace/result output dir")
    p.add_argument("--timeout", type=_positive_int, default=600, help="run timeout (seconds)")
    p.add_argument(
        "--max-rundir-bytes",
        type=int,
        default=0,
        dest="max_rundir_bytes",
        help="kill the run if the run dir exceeds this many bytes (0=off; SETUP-25)",
    )
    p.add_argument("--trace", action="store_true", help="stream a live B-Wave trace")
    p.add_argument("--trace-scope", default=None, help="scope the trace to a hierarchy")
    p.add_argument(
        "--trace-arg",
        action="append",
        default=[],
        dest="trace_args",
        help="argument enabling the binary's trace capture (repeatable; "
        "'{file}' interpolates the destination). Defaults to Booley's own "
        "'+trace +tracefile={file}' convention; a project owning its C++ "
        "main() sets its own, e.g. --trace=<file>",
    )
    p.add_argument(
        "--trace-file",
        action="append",
        default=[],
        dest="trace_files",
        help="path (relative to --run-cwd/--work-dir/--bin-dir, or absolute; "
        "globs allowed) where the testbench's own main() writes its dump. "
        "Repeatable; checked when Booley's own trace artifacts are absent",
    )
    p.add_argument(
        "--plusarg",
        action="append",
        default=[],
        dest="plusargs",
        help="plusarg passed to the binary (repeatable; +-prefix optional)",
    )
    # Project-configured verdict sentinels (booley.toml [flows.sim]); the
    # The Simulation Flow forwards these so a project keeps its own TB wording.
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
