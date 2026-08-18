#!/usr/bin/env python3
"""Run an edalize-built sim of a **Cocotb Target** — the cocotb run-half.

ADR 0034: FuseSoC+Edalize own the *build* (``fusesoc run --setup`` → ``make``
bakes the VPI linkage from the ``.core`` flow options); Booley owns the *run*.
This module is the cocotb sibling of :mod:`booley.sim.iverilog_run` /
:mod:`booley.sim.verilator_run` — a self-contained subprocess entry-point
(``python -m booley.sim.cocotb_run …``) shipped whole across the host/sandbox
boundary — with the run-stage glue Edalize's bypassed ``Sim.run()`` would have
supplied (decision 9):

  * env: ``COCOTB_TEST_MODULES`` (+ legacy ``MODULE``), the test-selection
    variable in the dialect the installed cocotb generation speaks —
    ``COCOTB_TEST_FILTER`` (a regex, cocotb ≥2) or ``TESTCASE`` (a
    comma-separated name list, cocotb 1.x; project images may pin a 1.x
    stack for legacy TBs even though the base sandbox ships 2.x), probed via
    ``cocotb-config --version`` — ``LIBPYTHON_LOC`` / ``PYGPI_PYTHON_BIN``
    (via ``cocotb-config``, called in-sandbox at run
    time), ``COCOTB_RESULTS_FILE`` (explicit — the path is never guessed), and
    ``PYTHONPATH=<build dir>`` (spike S1: cocotb 2.x resolves the test module
    against the process cwd, so a project ``run_cwd`` would otherwise break
    the import at rc=0 with no XML);
  * Icarus: ``vvp -n -M<build> -M `cocotb-config --lib-dir` -m
    `cocotb-config --lib-name vpi icarus` <image> [+plusargs]`` (matches the
    Makefile run target edalize generates, minus its ``-fst``);
  * Verilator: runs the built binary — named ``Vtop``, not ``V<toplevel>``
    (edalize forces ``--prefix Vtop`` and compiles cocotb's own
    ``verilator.cpp`` main when ``cocotb_module`` is set).

**Verdict** (decision 6): sentinels do not apply — per-test pass/fail parses
from cocotb's ``results.xml`` (:mod:`booley.sim.cocotb_results`), re-emitted
as a ``[COCOTB_RESULTS]`` line for simulate's per-test fan-out plus the usual
``[SIM_SUMMARY]`` batch verdict. Output scanning is retained only for what the
XML cannot see: ``count_sva_errors`` still runs (the RTL under a cocotb TB can
``$fatal``/``$error``), and a missing/truncated XML is **inconclusive, never a
pass** — the exit code alone decides nothing (a failing cocotb test exits 0).

**Trace** (decision 12, spikes S2/S3): file → postprocess, no FIFO. Icarus
reuses the trace-overlay ``booley_vcd_dump`` second top (``+trace`` plusarg →
``dump.vcd`` in the run cwd); Verilator uses cocotb's built-in main flags
(``--trace --trace-file dump.vcd`` — the overlay's ``--trace`` build enables
``VM_TRACE``). Both feed the same ``TraceSession`` VCD→bwave postprocess the
Icarus run-half already uses.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from booley.sim.run_guard import DiskBudgetGuard, SimTimeStallGuard

from booley.sim.cocotb_results import (
    STATE_OK,
    VERDICT_PASS,
    find_import_failure,
    format_results_line,
    parse_results_xml,
    reconcile,
    recover_timeout_progress,
)
from booley.sim.run_guard import DEFAULT_SIM_TIME_GRACE_S, find_sim_time_stall
from booley.sim.sim_result import (
    count_sva_errors,
    format_infra_error,
    format_summary,
    write_result_json,
    write_run_log,
)
from booley.sim.trace_session import TraceSession

# The dump/trace file name in the run cwd — identical for both EDA tools: Icarus's
# booley_vcd_dump module hardcodes $dumpfile("dump.vcd"); Verilator's cocotb
# main receives it via --trace-file.
_DEFAULT_VCD_NAME = "dump.vcd"

# The results file name, pinned inside the work dir (never guessed —
# COCOTB_RESULTS_FILE is always set explicitly, ADR 0034 / C1).
RESULTS_XML_NAME = "results.xml"

# D3: the actionable stale-image error when the sandbox predates cocotb
# support. Named as a constant so simulate's tests can assert the wording.
COCOTB_CONFIG_MISSING_MSG = (
    "cocotb-config not found — the sandbox image predates cocotb support; "
    "rebuild the sandbox image (src/booley/data/docker/build.sh)"
)


def build_cocotb_test_filter(module: str, names: list[str]) -> str:
    """Build the anchored ``COCOTB_TEST_FILTER`` regex for the selected set (A3).

    cocotb 2.x matches the filter against the fully-qualified
    ``<module>.<test>`` name (spike S4 — an anchored bare-name alternation
    matches *zero* tests), so the regex is
    ``^<module>\\.(name1|name2)$`` with every component ``re.escape``d: a test
    named with regex metacharacters must not widen the match.
    """
    escaped = "|".join(re.escape(n) for n in names)
    return rf"^{re.escape(module)}\.({escaped})$"


def _find_verilator_binary(build_dir: Path) -> Path | None:
    """The cocotb Verilator binary — always ``Vtop`` (edalize pins the prefix)."""
    exe = build_dir / "Vtop"
    return exe if exe.exists() else None


def _find_icarus_image(build_dir: Path) -> str | None:
    """Locate the vvp image via its ``.scr`` sibling (iverilog_run's discovery)."""
    from booley.sim.iverilog_run import _find_image

    return _find_image(build_dir)


def _cocotb_config(arg_sets: list[list[str]]) -> list[str]:
    """Run ``cocotb-config`` once per arg set; return the stripped outputs.

    Raises ``FileNotFoundError`` when the binary is absent — callers surface
    the D3 stale-image message instead of a raw traceback.
    """
    outputs: list[str] = []
    for args in arg_sets:
        proc = subprocess.run(
            ["cocotb-config", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(proc.stdout.strip())
    return outputs


def _cocotb_major_version() -> int:
    """Best-effort cocotb major version via ``cocotb-config --version``.

    Defaults to 2 (the base-image-pinned generation) when the probe fails for
    any reason: only a positive 1.x identification switches the selection
    env-var dialect. A truly absent ``cocotb-config`` still surfaces the D3
    stale-image message — the mandatory ``--libpython`` call raises for it.
    """
    try:
        (version,) = _cocotb_config([["--version"]])
        return int(version.strip().split(".")[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        return 2


def _build_cocotb_env(
    build_dir: Path,
    module: str,
    tests: list[str],
    results_file: Path,
) -> dict[str, str]:
    """The cocotb run environment (decision 9's run-stage glue)."""
    libpython, python_bin = _cocotb_config([["--libpython"], ["--python-bin"]])
    env = os.environ.copy()
    env["COCOTB_TEST_MODULES"] = module
    env["MODULE"] = module  # cocotb < 2.0 compat (harmless on 2.x)
    if tests:
        if _cocotb_major_version() < 2:
            # cocotb 1.x ignores COCOTB_TEST_FILTER entirely (it would
            # silently run the whole module); its selection dialect is
            # TESTCASE — comma-separated exact test-function names. Gated by
            # version because cocotb 2.x removed TESTCASE support.
            env["TESTCASE"] = ",".join(tests)
        else:
            env["COCOTB_TEST_FILTER"] = build_cocotb_test_filter(module, tests)
    env["LIBPYTHON_LOC"] = libpython
    env["PYGPI_PYTHON_BIN"] = python_bin
    env["COCOTB_RESULTS_FILE"] = str(results_file)
    # Spike S1: PyGPI imports resolve against the process cwd; pin the build
    # dir (where copyto staged the module) so a project run_cwd can't break it.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{build_dir}{os.pathsep}{existing}" if existing else str(build_dir)
    return env


def _build_run_cmd(
    *,
    eda_tool: str,
    build_dir: Path,
    plusargs: list[str],
    vcd: bool,
) -> list[str] | None:
    """Assemble the sim invocation for one cocotb run; None when unbuilt.

    Plusargs follow SETUP-7: a ``+…`` token is a plusarg, a ``-…`` token is
    forwarded verbatim. Trace flags are owned here (mirroring the other
    run-halves): Icarus gets ``+trace`` (fires the overlay's dump module),
    Verilator gets cocotb's ``--trace --trace-file`` main flags.
    """
    plus = [pa if pa.startswith(("+", "-")) else f"+{pa}" for pa in plusargs]
    if eda_tool == "icarus":
        image = _find_icarus_image(build_dir)
        if image is None:
            return None
        libdir, libname = _cocotb_config(
            [["--lib-dir"], ["--lib-name", "vpi", "icarus"]],
        )
        vvp_bin = shutil.which("vvp") or "vvp"
        cmd = [
            vvp_bin,
            "-n",
            f"-M{build_dir}",
            "-M",
            libdir,
            "-m",
            libname,
            str(build_dir / image),
            *plus,
        ]
        if vcd and not any(p.lstrip("+") == "trace" for p in plus):
            cmd.append("+trace")
        return cmd
    exe = _find_verilator_binary(build_dir)
    if exe is None:
        return None
    cmd = [str(exe), *plus]
    if vcd:
        cmd += ["--trace", "--trace-file", _DEFAULT_VCD_NAME]
    return cmd


def _stream_output(
    cmd: list[str],
    run_cwd: Path,
    env: dict[str, str],
    timeout: int,
    *,
    max_rundir_bytes: int = 0,
    sim_time_grace_s: float = 0.0,
) -> tuple[deque[str], subprocess.Popen, bool]:
    """Run the sim, stream stdout live, enforce *timeout* (seconds).

    The iverilog_run watchdog pattern (timer + disk guard + $readmemh trap),
    parameterized by *env* — the cocotb glue rides the environment — plus the
    cocotb-specific frozen-clock guard (F-25). Returns
    ``(lines, proc, timed_out)``.
    """
    import threading

    from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs
    from booley.sim.run_guard import (
        DiskBudgetGuard,
        SimTimeStallGuard,
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
    )
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
    stall_guard = SimTimeStallGuard(proc, sim_time_grace_s)
    stall_guard.start()
    readmemh_error = ""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            lines.append(line)
            # F-25: track the simulator's own clock, so a cocotb/simulator
            # run-loop mismatch (time frozen at 0.00 ns) aborts with a
            # diagnosis instead of burning the whole wall-clock budget.
            stall_guard.observe(line)
            # SETUP-23: a missing $readmemh init file warns once then the sim
            # spins forever on uninitialised RAM — treat the warning as fatal.
            fatal = readmemh_fatal_line(line)
            if fatal:
                readmemh_error = f"missing $readmemh memory-init file — {fatal}"
                kill_process_tree(proc)
                break
        proc.wait()
    finally:
        timer.cancel()
        guard.stop()
        stall_guard.stop()

    _append_abort_reason(
        lines,
        readmemh_error=readmemh_error,
        guard=guard,
        stall_guard=stall_guard,
        timed_out=timed_out["hit"],
        timeout=timeout,
    )
    return lines, proc, timed_out["hit"]


def _append_abort_reason(
    lines: deque[str],
    *,
    readmemh_error: str,
    guard: DiskBudgetGuard,
    stall_guard: SimTimeStallGuard,
    timed_out: bool,
    timeout: int,
) -> None:
    """Echo + record why a streamed sim was cut short (if it was).

    Ordered by specificity: a guard that fired names a concrete cause, so it
    outranks the bare timeout (which the stall kill also trips, the timer
    having no way to know the run was already doomed).
    """
    if readmemh_error:
        msg = f"ERROR: {readmemh_error}"
        print(msg)
        lines.append(msg + "\n")
    elif guard.tripped:
        print(guard.message)
        lines.append(guard.message + "\n")
    elif stall_guard.tripped:
        msg = f"ERROR: {stall_guard.message}"
        print(msg)
        lines.append(msg + "\n")
    elif timed_out:
        msg = f"ERROR: cocotb simulation timed out ({timeout}s)"
        print(msg)
        lines.append(msg + "\n")


def _recover_partial_timeout_results(results, output: str, timed_out: bool, tests: list[str]):
    """Use durable cocotb progress when timeout prevented results.xml."""
    if timed_out:
        return recover_timeout_progress(output, tests, results)
    return results


def _evaluate_verdict(
    output: str,
    returncode: int,
    timed_out: bool,
    work_dir: Path,
    results_file: Path,
    tests: list[str],
) -> tuple[str, bool]:
    """Parse results.xml, print the verdict sentinels, persist result files.

    Returns ``(output_with_sentinels, passed)``. The batch passes iff every
    selected test reconciles to a real XML pass AND the raw output carries no
    SVA/$fatal errors AND nothing timed out (decision 6 / C2). Sentinel
    scanning (``parse_sim_verdict``) is deliberately absent — sentinels do not
    apply to Cocotb Targets.
    """
    results = _recover_partial_timeout_results(
        parse_results_xml(results_file), output, timed_out, tests
    )
    # An import failure kills cocotb before it writes any XML — and still exits
    # 0 — so the parser can only report the symptom ("results.xml not found"),
    # which it then stamps onto every selected test. The cause is a CRITICAL
    # line on the console and nowhere but run.log. Promote it to the detail the
    # user actually sees, or they go debugging the results file (F-6).
    if results.state != STATE_OK:
        import_failure = find_import_failure(output)
        stall = find_sim_time_stall(output)
        if import_failure:
            results = replace(results, detail=import_failure)
        elif stall:
            # F-25: same promotion for a frozen run loop — "results.xml not
            # found" is true and useless; the guard already wrote the cause.
            results = replace(results, detail=stall)

    # No selected list (a Target without tests.toml): every XML test is the
    # selected set — cocotb ran the module's full suite.
    selected = tests or [t.name for t in results.tests]
    verdicts = reconcile(selected, results)

    sva_errors = count_sva_errors(output)
    all_pass = bool(verdicts) and all(v == VERDICT_PASS for _, v, _ in verdicts)
    inconclusive = (
        results.state != STATE_OK or any(v == "inconclusive" for _, v, _ in verdicts)
    ) and not timed_out
    passed = all_pass and sva_errors == 0 and not timed_out and returncode == 0

    results_line = format_results_line(results)
    summary = format_summary(
        passed,
        sva_errors,
        inconclusive=inconclusive and not passed,
    )
    print(results_line)
    print(summary)
    if passed:
        print(f"\ncocotb sim PASSED ({len(selected)} tests)")
    elif timed_out:
        print("\ncocotb sim FAILED (timed out)")
    elif inconclusive:
        print(
            f"\ncocotb sim INCONCLUSIVE ({results.state}: {results.detail})"
            if results.state != STATE_OK
            else "\ncocotb sim INCONCLUSIVE (selected tests unresolved)"
        )
    else:
        failed = [n for n, v, _ in verdicts if v != VERDICT_PASS]
        reason = f"{len(failed)}/{len(selected)} tests failed"
        if sva_errors:
            reason += f", {sva_errors} SVA assertion errors"
        print(f"\ncocotb sim FAILED ({reason})")

    first_err = ""
    if not passed:
        for name, verdict, detail in verdicts:
            if verdict != VERDICT_PASS and detail:
                first_err = f"{name}: {detail}"
                break
    write_result_json(
        work_dir,
        passed,
        sva_errors,
        first_err,
        returncode,
        inconclusive=inconclusive and not passed,
    )
    output = f"{output}\n{results_line}\n{summary}"
    write_run_log(work_dir, output)
    return output, passed


def _prepare_invocation(
    *,
    build_dir: Path,
    eda_tool: str,
    cocotb_module: str,
    tests: list[str],
    results_file: Path,
    work_dir: Path,
    plusargs: list[str],
    vcd: bool,
) -> tuple[dict[str, str], list[str]] | None:
    """Resolve the sim env + command; ``None`` means the failure was reported.

    D3: an image built before the cocotb pip layer has no cocotb-config —
    name the cause and the fix instead of a raw FileNotFoundError. A
    cocotb-config that exists but errors is surfaced with its own detail.

    Every failure here is *infrastructure*, not the design: the sim never
    started, so the run observed nothing. Each path emits the shared
    ``[SIM_INFRA_ERROR]`` marker alongside its message so a grading caller
    (mutation_tester) records "no observation" rather than a verdict.
    """
    try:
        env = _build_cocotb_env(build_dir, cocotb_module, tests, results_file)
        cmd = _build_run_cmd(
            eda_tool=eda_tool,
            build_dir=build_dir,
            plusargs=plusargs,
            vcd=vcd,
        )
    except FileNotFoundError:
        _report_infra_failure(work_dir, COCOTB_CONFIG_MISSING_MSG)
        return None
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
        detail = (
            f"cocotb-config failed (rc={exc.returncode})"
            + (f": {stderr}" if stderr else "")
            + f" — {COCOTB_CONFIG_MISSING_MSG}"
        )
        _report_infra_failure(work_dir, detail)
        return None
    if cmd is None:
        _report_infra_failure(
            work_dir,
            f"no built cocotb sim found in {build_dir} "
            f"({'no vvp image (*.scr)' if eda_tool == 'icarus' else 'no Vtop binary'})",
        )
        return None
    return env, cmd


def _report_infra_failure(work_dir: Path, detail: str) -> None:
    """Print, persist and mark a pre-run harness failure (never a verdict)."""
    msg = f"ERROR: {detail}"
    print(msg)
    print(format_infra_error(detail))
    write_result_json(work_dir, False, 0, detail, 1, inconclusive=True)
    write_run_log(work_dir, f"{msg}\n{format_infra_error(detail)}")


def _print_run_banner(
    env: dict[str, str],
    cmd: list[str],
    run_cwd: Path,
    cocotb_module: str,
    eda_tool: str,
    tests: list[str],
) -> None:
    """Echo the invocation header, including whichever selection dialect
    :func:`_build_cocotb_env` chose (regex filter on cocotb ≥2, TESTCASE name
    list on 1.x)."""
    print(f"\n{'=' * 60}")
    print(f"[cocotb simulation: {cocotb_module} on {eda_tool}]")
    print(f"{'=' * 60}")
    print(f"CWD: {run_cwd}")
    if tests:
        if "COCOTB_TEST_FILTER" in env:
            print(f"FILTER: {env['COCOTB_TEST_FILTER']}")
        else:
            print(f"TESTCASE: {env['TESTCASE']}")
    print(f"CMD: {' '.join(cmd)}\n")


def run_cocotb_sim(
    *,
    build_dir: Path,
    eda_tool: str,
    cocotb_module: str,
    tests: list[str] | None = None,
    run_cwd: Path | None = None,
    work_dir: Path | None = None,
    vcd: bool = False,
    plusargs: list[str] | None = None,
    timeout: int = 600,
    max_rundir_bytes: int = 0,
    sim_time_grace_s: float = DEFAULT_SIM_TIME_GRACE_S,
) -> int:
    """Run the edalize-built cocotb sim once; return the process exit code.

    *build_dir* holds the built sim (vvp image / ``Vtop``) plus the
    ``copyto``-staged Python testbench; *run_cwd* is where the sim runs from
    (TB vector/firmware base), defaulting to *build_dir*; *work_dir* is the
    result/trace output dir (``results.xml``, ``result.json``, ``run.log``,
    the ``.fst`` store), defaulting to *build_dir*. *sim_time_grace_s* is the
    frozen-clock watchdog's grace period (F-25; 0 disables it).
    """
    from booley.runtime.heartbeat import Heartbeat

    build_dir = Path(build_dir).resolve()
    run_cwd = Path(run_cwd).resolve() if run_cwd is not None else build_dir
    work_dir = Path(work_dir).resolve() if work_dir is not None else build_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    results_file = work_dir / RESULTS_XML_NAME
    results_file.unlink(missing_ok=True)  # never parse a stale run's verdicts
    tests = list(tests or [])

    prepared = _prepare_invocation(
        build_dir=build_dir,
        eda_tool=eda_tool,
        cocotb_module=cocotb_module,
        tests=tests,
        results_file=results_file,
        work_dir=work_dir,
        plusargs=list(plusargs or []),
        vcd=vcd,
    )
    if prepared is None:
        return 1
    env, cmd = prepared

    trace = TraceSession(work_dir, None, backend="iverilog") if vcd else None
    if trace:
        trace.reset_for_run((run_cwd / _DEFAULT_VCD_NAME,))

    _print_run_banner(env, cmd, run_cwd, cocotb_module, eda_tool, tests)

    hb = Heartbeat("cocotb sim", interval=60)
    hb.start()
    try:
        lines, proc, timed_out = _stream_output(
            cmd,
            run_cwd,
            env,
            timeout,
            max_rundir_bytes=max_rundir_bytes,
            sim_time_grace_s=sim_time_grace_s,
        )
    finally:
        hb.stop()

    output = "".join(lines)
    output, passed = _evaluate_verdict(
        output,
        proc.returncode,
        timed_out,
        work_dir,
        results_file,
        tests,
    )

    if vcd and trace:
        # Both EDA tools drop dump.vcd in the run cwd (Icarus via the overlay's
        # dump module, Verilator via cocotb's --trace-file main flag); feed it
        # through the VCD→bwave postprocess and assert an artifact landed.
        trace.postprocess(run_cwd / _DEFAULT_VCD_NAME)
        found = trace.find()
        if found is None:
            reason = "trace requested but no queryable .fst store or .vcd was produced"
            incident = trace.write_incident(reason, sim_proc=proc)
            print(f"ERROR: {reason}")
            print(f"TRACE_INCIDENT: {incident}")
        else:
            print(f"TRACE_OK: {found}")

    return 0 if passed else 1


def _positive_int(value: str) -> int:
    """argparse type: reject non-positive timeouts at the CLI boundary."""
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {ivalue}")
    return ivalue


def _non_negative_float(value: str) -> float:
    """argparse type: a wall-clock grace in seconds; 0 disables the guard."""
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {fvalue}")
    return fvalue


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run an edalize-built cocotb sim (Icarus or Verilator)",
    )
    p.add_argument(
        "--build-dir",
        required=True,
        help="edalize build dir (vvp image / Vtop + staged Python TB)",
    )
    p.add_argument(
        "--eda-tool",
        required=True,
        choices=["icarus", "verilator"],
        help="EDA-tool family of the built sim",
    )
    p.add_argument(
        "--cocotb-module",
        required=True,
        dest="cocotb_module",
        help="cocotb test module (from the resolved flow options)",
    )
    p.add_argument(
        "--test",
        action="append",
        default=[],
        dest="tests",
        help="selected test-function name (repeatable; drives "
        "COCOTB_TEST_FILTER — omit to run the whole module)",
    )
    p.add_argument("--run-cwd", default=None, help="cwd to run the sim from")
    p.add_argument("--work-dir", default=None, help="result/trace output dir")
    p.add_argument("--timeout", type=_positive_int, default=600, help="run timeout (seconds)")
    p.add_argument(
        "--max-rundir-bytes",
        type=int,
        default=0,
        dest="max_rundir_bytes",
        help="kill the run if the run dir exceeds this many bytes (0=off; SETUP-25)",
    )
    p.add_argument(
        "--sim-time-grace",
        type=_non_negative_float,
        default=DEFAULT_SIM_TIME_GRACE_S,
        dest="sim_time_grace_s",
        help="abort when simulation time is still 0.00 ns after this many "
        "wall-clock seconds (0=off; F-25 cocotb/simulator run-loop mismatch)",
    )
    p.add_argument("--trace", action="store_true", help="postprocess a B-Wave trace from dump.vcd")
    p.add_argument(
        "--plusarg",
        action="append",
        default=[],
        dest="plusargs",
        help="plusarg passed to the sim (repeatable; +-prefix optional)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return run_cocotb_sim(
        build_dir=Path(args.build_dir),
        eda_tool=args.eda_tool,
        cocotb_module=args.cocotb_module,
        tests=args.tests,
        run_cwd=Path(args.run_cwd) if args.run_cwd else None,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        vcd=args.trace,
        plusargs=args.plusargs,
        timeout=args.timeout,
        max_rundir_bytes=args.max_rundir_bytes,
        sim_time_grace_s=args.sim_time_grace_s,
    )


if __name__ == "__main__":  # pragma: no cover - subprocess entry-point
    raise SystemExit(main())
