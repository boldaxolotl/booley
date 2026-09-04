#!/usr/bin/env python3
"""Run an edalize-built sim of a **Cocotb Target** — the cocotb run-half.

ADR 0034: FuseSoC+Edalize own the *build* (``fusesoc run --setup`` → ``make``
bakes the VPI linkage from the ``.core`` flow options); Booley owns the *run*.
This module is the cocotb sibling of :mod:`booley.flows.sim.backends.icarus` /
:mod:`booley.flows.sim.backends.verilator` — a self-contained subprocess entry-point
(``python -m booley.flows.sim.backends.cocotb …``) supervised wholly inside the Session
Runtime — with the run-stage glue Edalize's bypassed ``Sim.run()`` would have
supplied (decision 9):

  * env: ``COCOTB_TEST_MODULES`` (+ legacy ``MODULE``), the test-selection
    variable in the dialect the installed cocotb generation speaks —
    ``COCOTB_TEST_FILTER`` (a regex, cocotb ≥2) or ``TESTCASE`` (a
    comma-separated name list, cocotb 1.x; project images may pin a 1.x
    stack for legacy TBs even though the base sandbox ships 2.x), probed via
    ``cocotb-config --version`` — ``LIBPYTHON_LOC`` / ``PYGPI_PYTHON_BIN``
    plus cocotb 2.1+'s ``GPI_USERS=<libpython>;<PyGPI entry point>`` (all via
    ``cocotb-config``, called in-sandbox at run time),
    ``COCOTB_RESULTS_FILE`` (explicit — the path is never guessed), and
    ``PYTHONPATH=<build dir>`` (spike S1: cocotb 2.x resolves the test module
    against the process cwd, so a project ``run_cwd`` would otherwise break
    the import at rc=0 with no XML);
  * Icarus: ``vvp -n -M<build> -m
    `cocotb-config --lib-name-path vpi icarus` <image> [+plusargs]``. The
    absolute library path works with cocotb 1.x/2.0's extensionless ``.vpl``
    lookup and cocotb 2.1+'s platform-suffixed shared library;
  * Verilator: runs the built binary — named ``Vtop``, not ``V<toplevel>``
    (edalize forces ``--prefix Vtop`` and compiles cocotb's own
    ``verilator.cpp`` main when ``cocotb_module`` is set).

**Verdict** (decision 6): sentinels do not apply — per-test pass/fail parses
from cocotb's ``results.xml`` (:mod:`booley.flows.sim.backends.cocotb_results`), re-emitted
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
import json
import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from booley.flows.sim.run_guard import DiskBudgetGuard, SimTimeStallGuard

from booley.flows.run_log import write_run_log
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTransportIdentity,
    add_transport_arguments,
    transport_identity_from_args,
    write_adapter_result,
)
from booley.flows.sim.backends.cocotb_results import (
    STATE_OK,
    VERDICT_PASS,
    CocotbResults,
    find_import_failure,
    format_results_line,
    parse_results_line,
    parse_results_xml,
    reconcile,
    recover_timeout_progress,
    results_payload,
)
from booley.flows.sim.backends.shared import find_icarus_image


def prepare_invocation(work: PreparedSimulationWork) -> list[str]:
    """Render one parent-side Cocotb adapter invocation."""
    if work.adapter != "cocotb":
        raise ValueError(f"Cocotb adapter cannot prepare {work.adapter!r} work")
    cmd = [
        "python3",
        "-m",
        "booley.flows.sim.backends.cocotb",
        "--build-dir",
        work.build_dir,
        "--eda-tool",
        work.eda_tool,
        "--cocotb-module",
        work.cocotb_module,
        "--timeout",
        str(work.timeout_s),
    ]
    cmd += [f"--test={name}" for name in work.tests]
    cmd += ["--result-verbosity", work.result_verbosity]
    cmd += ["--sim-time-grace", str(work.sim_time_grace_s)]
    if work.max_rundir_bytes > 0:
        cmd += ["--max-rundir-bytes", str(work.max_rundir_bytes)]
    if work.run_cwd:
        cmd += ["--run-cwd", work.run_cwd]
    if work.trace:
        cmd += ["--trace", "--expected-trace-scope", work.trace_scope]
    cmd += [f"--plusarg={value}" for value in work.plusargs]
    if work.adapter_result_path:
        cmd += [
            "--adapter-result",
            work.adapter_result_path,
            "--attempt-token",
            work.attempt_token,
            "--target-identity",
            work.target_identity,
        ]
        cmd += [f"--selected-test={name}" for name in work.tests]
    return cmd


def _publish_adapter_result(
    identity: AdapterTransportIdentity | None,
    output: str,
    passed: bool,
    *,
    failure_kind: str = "",
    detail: str = "",
) -> None:
    if identity is None:
        return
    results = parse_results_line(output)
    discovered = tuple(test.name for test in results.tests if test.name) if results else ()
    names = identity.selected_tests or discovered
    sva_errors = count_sva_errors(output)
    inconclusive = not passed and (results is None or results.state != STATE_OK)
    write_adapter_result(
        identity,
        AdapterResult(
            passed=passed and sva_errors == 0,
            inconclusive=inconclusive,
            sva_errors=sva_errors,
            tests=tuple(names),
            failure_kind=failure_kind or ("inconclusive" if inconclusive else ""),
            detail=detail,
        ),
    )
from booley.flows.sim.result import (
    count_sva_errors,
    format_infra_error,
    format_summary,
    write_result_json,
)
from booley.flows.sim.run_guard import DEFAULT_SIM_TIME_GRACE_S, find_sim_time_stall
from booley.flows.sim.trace_session import TraceSession

# The dump/trace file name in the run cwd — identical for both EDA tools: Icarus's
# booley_vcd_dump module hardcodes $dumpfile("dump.vcd"); Verilator's cocotb
# main receives it via --trace-file.
_DEFAULT_VCD_NAME = "dump.vcd"

# The results file name, pinned inside the work dir (never guessed —
# COCOTB_RESULTS_FILE is always set explicitly, ADR 0034 / C1).
RESULTS_XML_NAME = "results.xml"
FULL_RESULTS_JSON_NAME = "cocotb_results.json"

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


def _cocotb_version() -> tuple[int, int]:
    """Best-effort ``(major, minor)`` cocotb version.

    The fallback retains the pre-2.1 environment contract. A successful 2.1+
    probe is required before emitting ``GPI_USERS`` because older
    ``cocotb-config`` versions do not provide ``--pygpi-entry-point``.
    """
    try:
        (version,) = _cocotb_config([["--version"]])
        match = re.match(r"^(\d+)\.(\d+)", version.strip())
        if match is None:
            return (2, 0)
        return (int(match.group(1)), int(match.group(2)))
    except (OSError, subprocess.SubprocessError, ValueError):
        return (2, 0)


def _build_cocotb_env(
    build_dir: Path,
    module: str,
    tests: list[str],
    results_file: Path,
) -> dict[str, str]:
    """The cocotb run environment (decision 9's run-stage glue)."""
    libpython, python_bin = _cocotb_config([["--libpython"], ["--python-bin"]])
    cocotb_version = _cocotb_version()
    env = os.environ.copy()
    env["COCOTB_TEST_MODULES"] = module
    env["MODULE"] = module  # cocotb < 2.0 compat (harmless on 2.x)
    if tests:
        if cocotb_version[0] < 2:
            # cocotb 1.x ignores COCOTB_TEST_FILTER entirely (it would
            # silently run the whole module); its selection dialect is
            # TESTCASE — comma-separated exact test-function names. Gated by
            # version because cocotb 2.x removed TESTCASE support.
            env["TESTCASE"] = ",".join(tests)
        else:
            env["COCOTB_TEST_FILTER"] = build_cocotb_test_filter(module, tests)
    env["LIBPYTHON_LOC"] = libpython
    env["PYGPI_PYTHON_BIN"] = python_bin
    if cocotb_version >= (2, 1):
        (pygpi_entry_point,) = _cocotb_config([["--pygpi-entry-point"]])
        required_users = [libpython, pygpi_entry_point]
        existing_users = env.get("GPI_USERS", "").split(";")
        env["GPI_USERS"] = ";".join(
            [
                *required_users,
                *(user for user in existing_users if user and user not in required_users),
            ]
        )
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
        image = find_icarus_image(build_dir)
        if image is None:
            return None
        (libpath,) = _cocotb_config([["--lib-name-path", "vpi", "icarus"]])
        vvp_bin = shutil.which("vvp") or "vvp"
        cmd = [
            vvp_bin,
            "-n",
            f"-M{build_dir}",
            "-m",
            libpath,
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

    from booley.flows.sim.run_guard import (
        DiskBudgetGuard,
        SimTimeStallGuard,
        readmemh_fatal_line,
        snapshot_dir_baseline,
    )
    from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs

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
    stdout = proc.stdout
    try:
        assert stdout is not None
        for line in stdout:
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
        if stdout is not None:
            stdout.close()
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


@dataclass(frozen=True)
class _VerdictAssessment:
    """All inputs needed to emit and persist one Cocotb batch verdict."""

    selected: tuple[str, ...]
    verdicts: tuple[tuple[str, str, str], ...]
    sva_errors: int
    passed: bool
    inconclusive: bool
    infrastructure_error: str


@dataclass(frozen=True)
class _TraceFinalization:
    """Trace markers plus any infrastructure failure they establish."""

    output: str = ""
    failure_reason: str = ""


def _persist_result_transport(
    results,
    work_dir: Path,
    selected: list[str],
    focused: bool,
    result_verbosity: str,
) -> tuple[str, int]:
    """Write lossless JSON and return the requested stdout line plus skip count."""
    (work_dir / FULL_RESULTS_JSON_NAME).write_text(
        json.dumps(results_payload(results), indent=2) + "\n",
        encoding="utf-8",
    )
    compact = results_payload(results, selected=selected, verbosity="compact")
    line = format_results_line(
        results,
        selected=selected if focused else None,
        verbosity=result_verbosity,
    )
    return line, int(compact["skipped_unselected"]) if focused else 0


def _selected_verdicts(tests: list[str], results) -> tuple[bool, list[str], list[tuple]]:
    """Resolve the selected set and its reconciled verdicts."""
    focused = bool(tests)
    selected = tests or [test.name for test in results.tests]
    return focused, selected, reconcile(selected, results)


def _load_cocotb_results(
    results_file: Path,
    output: str,
    timed_out: bool,
    tests: list[str],
) -> CocotbResults:
    """Load current results and replace generic missing-file diagnostics."""
    results = _recover_partial_timeout_results(
        parse_results_xml(results_file), output, timed_out, tests
    )
    if results.state == STATE_OK:
        return results
    detail = find_import_failure(output) or find_sim_time_stall(output)
    return replace(results, detail=detail) if detail else results


def _assess_verdict(
    results: CocotbResults,
    output: str,
    returncode: int,
    timed_out: bool,
    tests: list[str],
    infrastructure_error: str,
) -> tuple[bool, _VerdictAssessment]:
    """Reconcile selected tests and classify the batch outcome."""
    focused, selected, verdicts = _selected_verdicts(tests, results)
    sva_errors = count_sva_errors(output)
    all_pass = bool(verdicts) and all(v == VERDICT_PASS for _, v, _ in verdicts)
    result_inconclusive = results.state != STATE_OK or any(
        verdict == "inconclusive" for _, verdict, _ in verdicts
    )
    trace_inconclusive = bool(infrastructure_error) and all_pass and sva_errors == 0
    inconclusive = (result_inconclusive or trace_inconclusive) and not timed_out
    passed = (
        all_pass
        and sva_errors == 0
        and not timed_out
        and returncode == 0
        and not infrastructure_error
    )
    return focused, _VerdictAssessment(
        selected=tuple(selected),
        verdicts=tuple(verdicts),
        sva_errors=sva_errors,
        passed=passed,
        inconclusive=inconclusive,
        infrastructure_error=infrastructure_error,
    )


def _print_verdict(
    assessment: _VerdictAssessment,
    results: CocotbResults,
    timed_out: bool,
    skipped_unselected: int,
) -> None:
    """Print one unambiguous human Cocotb verdict."""
    selected = assessment.selected
    if assessment.passed:
        skipped_note = f"; {skipped_unselected} skipped" if skipped_unselected else ""
        print(f"\ncocotb sim PASSED ({len(selected)} tests{skipped_note})")
    elif timed_out:
        print("\ncocotb sim FAILED (timed out)")
    elif assessment.inconclusive:
        detail = assessment.infrastructure_error or results.detail
        print(f"\ncocotb sim INCONCLUSIVE ({detail or 'selected tests unresolved'})")
    else:
        failed = [name for name, verdict, _ in assessment.verdicts if verdict != VERDICT_PASS]
        reason = f"{len(failed)}/{len(selected)} tests failed"
        if assessment.sva_errors:
            reason += f", {assessment.sva_errors} SVA assertion errors"
        print(f"\ncocotb sim FAILED ({reason})")


def _first_failure(assessment: _VerdictAssessment) -> str:
    """Return the most actionable durable failure detail."""
    if assessment.infrastructure_error and assessment.inconclusive:
        return assessment.infrastructure_error
    for name, verdict, detail in assessment.verdicts:
        if verdict != VERDICT_PASS and detail:
            return f"{name}: {detail}"
    return assessment.infrastructure_error


def _evaluate_verdict(
    output: str,
    returncode: int,
    timed_out: bool,
    work_dir: Path,
    results_file: Path,
    tests: list[str],
    result_verbosity: str = "compact",
    infrastructure_error: str = "",
) -> tuple[str, bool]:
    """Parse results.xml, print the verdict sentinels, persist result files.

    Returns ``(output_with_sentinels, passed)``. The batch passes iff every
    selected test reconciles to a real XML pass AND the raw output carries no
    SVA/$fatal errors AND nothing timed out (decision 6 / C2). Sentinel
    scanning (``parse_sim_verdict``) is deliberately absent — sentinels do not
    apply to Cocotb Targets.
    """
    results = _load_cocotb_results(results_file, output, timed_out, tests)
    focused, assessment = _assess_verdict(
        results, output, returncode, timed_out, tests, infrastructure_error
    )
    results_line, skipped_unselected = _persist_result_transport(
        results,
        work_dir,
        list(assessment.selected),
        focused,
        result_verbosity,
    )
    summary = format_summary(
        assessment.passed,
        assessment.sva_errors,
        inconclusive=assessment.inconclusive and not assessment.passed,
    )
    print(results_line)
    print(summary)
    _print_verdict(assessment, results, timed_out, skipped_unselected)
    effective_returncode = 1 if infrastructure_error and returncode == 0 else returncode
    write_result_json(
        work_dir,
        assessment.passed,
        assessment.sva_errors,
        _first_failure(assessment),
        effective_returncode,
        inconclusive=assessment.inconclusive and not assessment.passed,
    )
    output = f"{output}\n{results_line}\n{summary}"
    write_run_log(work_dir, output)
    return output, assessment.passed


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
) -> tuple[dict[str, str], list[str]] | str:
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
        return COCOTB_CONFIG_MISSING_MSG
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
        detail = (
            f"cocotb-config failed (rc={exc.returncode})"
            + (f": {stderr}" if stderr else "")
            + f" — {COCOTB_CONFIG_MISSING_MSG}"
        )
        _report_infra_failure(work_dir, detail)
        return detail
    if cmd is None:
        detail = (
            f"no built cocotb sim found in {build_dir} "
            f"({'no vvp image (*.scr)' if eda_tool == 'icarus' else 'no Vtop binary'})"
        )
        _report_infra_failure(work_dir, detail)
        return detail
    return env, cmd


def _report_infra_failure(work_dir: Path, detail: str) -> None:
    """Print, persist and mark a pre-run harness failure (never a verdict)."""
    msg = f"ERROR: {detail}"
    print(msg)
    print(format_infra_error(detail))
    write_result_json(work_dir, False, 0, detail, 1, inconclusive=True)
    write_run_log(work_dir, f"{msg}\n{format_infra_error(detail)}")


def _reset_result_transports(work_dir: Path) -> Path:
    """Remove verdict transports that must never survive into a new run."""
    results_file = work_dir / RESULTS_XML_NAME
    results_file.unlink(missing_ok=True)
    (work_dir / FULL_RESULTS_JSON_NAME).unlink(missing_ok=True)
    return results_file


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
    result_verbosity: str = "compact",
    expected_trace_scope: str = "",
    transport: AdapterTransportIdentity | None = None,
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
    results_file = _reset_result_transports(work_dir)
    tests = list(tests or [])
    if vcd and not expected_trace_scope.strip():
        detail = (
            "trace requested without an expected DUT scope; the waveform identity "
            "cannot be validated"
        )
        _report_infra_failure(
            work_dir,
            detail,
        )
        _publish_adapter_result(
            transport,
            "",
            False,
            failure_kind="infrastructure",
            detail=detail,
        )
        return 1

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
    if isinstance(prepared, str):
        _publish_adapter_result(
            transport,
            "",
            False,
            failure_kind="infrastructure",
            detail=prepared,
        )
        return 1
    env, cmd = prepared

    trace = TraceSession(work_dir, expected_trace_scope, backend="iverilog") if vcd else None
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
    trace_result = _finalize_cocotb_trace(trace, run_cwd, proc) if trace else _TraceFinalization()
    if trace_result.output:
        print(trace_result.output)
        output = f"{output}\n{trace_result.output}"
    output, passed = _evaluate_verdict(
        output,
        proc.returncode,
        timed_out,
        work_dir,
        results_file,
        tests,
        result_verbosity,
        trace_result.failure_reason,
    )

    _publish_adapter_result(transport, output, passed)

    return 0 if passed else 1


def _finalize_cocotb_trace(
    trace: TraceSession,
    run_cwd: Path,
    proc,
) -> _TraceFinalization:
    """Postprocess and validate one Cocotb waveform before any verdict."""
    trace.postprocess(run_cwd / _DEFAULT_VCD_NAME)
    inspection = trace.inspect(trace.find())
    if not inspection.usable:
        reason = (
            f"trace requested but no queryable waveform was produced: {inspection.failure_reason}"
        )
        incident = trace.write_incident(reason, sim_proc=proc)
        return _TraceFinalization(
            output=f"ERROR: {reason}\nTRACE_INCIDENT: {incident}",
            failure_reason=reason,
        )
    artifact = inspection.artifact
    assert artifact is not None
    return _TraceFinalization(output=f"TRACE_OK: {artifact.path}\n{artifact.metadata_line()}")


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
    p.add_argument(
        "--result-verbosity",
        choices=["compact", "full"],
        default="compact",
        help="Cocotb result transport detail (full results always remain in artifacts)",
    )
    p.add_argument(
        "--expected-trace-scope",
        default="",
        help="resolved DUT toplevel required to validate a requested trace",
    )
    add_transport_arguments(p)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    transport = transport_identity_from_args(args, "cocotb")
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
        result_verbosity=args.result_verbosity,
        expected_trace_scope=args.expected_trace_scope,
        transport=transport,
    )


if __name__ == "__main__":  # pragma: no cover - subprocess entry-point
    raise SystemExit(main())
