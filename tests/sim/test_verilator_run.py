"""Unit tests for the edalize-era Verilator run-half (booley.sim.verilator_run)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from booley.sim import verilator_run as vr
from booley.sim.sim_result import SIM_INFRA_ERROR_PREFIX


def test_find_binary_locates_flat_vtop(tmp_path: Path):
    """Edalize links V<top> flat in the build dir (no obj_dir/)."""
    exe = tmp_path / "Vtb_top"
    exe.write_text("#!/bin/sh\n")
    assert vr._find_binary(tmp_path, "tb_top") == exe
    assert vr._find_binary(tmp_path, "missing") is None


def test_build_run_cmd_appends_plusargs(tmp_path: Path):
    exe = tmp_path / "Vtop"
    cmd, env = vr._build_run_cmd(exe, tmp_path, ["test_id=1", "+already"])
    assert cmd[0] == str(exe)
    # bare plusargs gain a leading +, already-prefixed ones are left alone
    assert "+test_id=1" in cmd
    assert "+already" in cmd
    # Trace arguments are appended by _setup_bwave, which owns the destination.
    assert "+trace" not in cmd
    # LD_LIBRARY_PATH widened to the binary dir (harmless for a static binary)
    if "LD_LIBRARY_PATH" in env:
        assert str(tmp_path) in env["LD_LIBRARY_PATH"]


def test_build_run_cmd_forwards_getopt_arg_verbatim(tmp_path: Path):
    # SETUP-7: a '-'/'--' selector is a getopt argument, forwarded to the
    # binary's main WITHOUT a '+' prefix (unlike bare plusargs).
    cmd, _ = vr._build_run_cmd(tmp_path / "Vtop", tmp_path, ["--meminit=ram,boot.elf"])
    assert "--meminit=ram,boot.elf" in cmd
    assert "+--meminit=ram,boot.elf" not in cmd


class TestTraceArgContract:
    """F-15: the trace CLI is the project's to declare, not Booley's to assume.

    Ibex's VerilatorSimCtrl enables capture only for getopt `-t`/`--trace=FILE`.
    Booley's generic plusarg pair was ignored, the run passed, and a 443-byte
    header-only FST was accepted as a traced simulation.
    """

    def test_default_is_booleys_own_plusarg_convention(self, tmp_path: Path):
        rendered = vr._render_trace_args(None, tmp_path / "trace.fifo")
        assert rendered == ["+trace", f"+tracefile={tmp_path / 'trace.fifo'}"]

    def test_project_contract_replaces_the_default(self, tmp_path: Path):
        fifo = tmp_path / "trace.fifo"
        rendered = vr._render_trace_args(["--trace={file}"], fifo)
        assert rendered == [f"--trace={fifo}"]
        assert "+trace" not in rendered

    def test_file_templates_dropped_without_a_destination(self):
        # The non-FIFO path has nowhere to write; an unrendered template would
        # reach the binary as the literal string "{file}".
        assert vr._render_trace_args(["-t", "--trace={file}"], None) == ["-t"]

    def test_setup_bwave_appends_the_configured_contract(self, tmp_path: Path):
        class FakeTrace:
            fifo_path = tmp_path / "trace.fifo"

            def start_fifo(self):
                return None, True, None

        cmd = ["/bin/Vtop"]
        vr._setup_bwave(FakeTrace(), cmd, ["--trace={file}"])
        assert cmd == ["/bin/Vtop", f"--trace={tmp_path / 'trace.fifo'}"]

    def test_trace_arg_round_trips_through_the_cli(self):
        # The `=` form is mandatory for an option-like value: passing it as a
        # separate argv item makes argparse read it as a runner option (F-12).
        args = vr._parse_args(
            ["--bin-dir", "build/sim", "--top", "tb", "--trace", "--trace-arg=--trace={file}"]
        )
        assert args.trace_args == ["--trace={file}"]


def test_parse_args_round_trips_run_options():
    args = vr._parse_args(
        [
            "--bin-dir",
            "build/sim",
            "--top",
            "tb_top",
            "--run-cwd",
            "util/sim",
            "--timeout",
            "120",
            "--trace",
            "--trace-scope",
            "tb.dut",
            "--plusarg",
            "test_id=2",
            "--plusarg",
            "verbose",
        ]
    )
    assert args.bin_dir == "build/sim"
    assert args.top == "tb_top"
    assert args.run_cwd == "util/sim"
    assert args.timeout == 120
    assert args.trace is True
    assert args.trace_scope == "tb.dut"
    assert args.plusargs == ["test_id=2", "verbose"]


def test_parse_args_round_trips_sentinels():
    args = vr._parse_args(
        [
            "--bin-dir",
            "b",
            "--top",
            "tb",
            "--pass-sentinel",
            "ALL TESTS PASSED.",
            "--fail-sentinel",
            "ERROR!",
            "--fail-sentinel",
            "TIMEOUT",
        ]
    )
    assert args.pass_sentinels == ["ALL TESTS PASSED."]
    assert args.fail_sentinels == ["ERROR!", "TIMEOUT"]


def test_evaluate_verdict_honors_custom_sentinels(tmp_path: Path, capsys):
    vr._evaluate_verdict(
        "ALL TESTS PASSED.",
        0,
        tmp_path,
        pass_sentinels=["ALL TESTS PASSED."],
        fail_sentinels=["ERROR!"],
    )
    assert "PASSED" in capsys.readouterr().out


def test_evaluate_verdict_fail_sentinel_rc0_avoids_confusing_rc0(tmp_path: Path, capsys):
    # A FAIL sentinel with a clean exit must NOT print the confusing "(rc=0)".
    vr._evaluate_verdict(
        "ERROR!",
        0,
        tmp_path,
        pass_sentinels=["ALL TESTS PASSED."],
        fail_sentinels=["ERROR!"],
    )
    out = capsys.readouterr().out
    assert "FAILED (fail sentinel matched)" in out
    assert "rc=0" not in out


def test_run_verilated_binary_missing_exe_returns_error(tmp_path: Path):
    """A missing binary yields a clear error string (no crash)."""
    out = vr.run_verilated_binary(top_module="tb_top", bin_dir=tmp_path)
    assert "not found" in out


def test_missing_exe_is_marked_as_infra_not_a_verdict(tmp_path: Path):
    """SETUP-F-41b: no binary means no observation — the marker lets a grading
    caller refuse to score the run instead of reading rc!=0 as a FAIL."""
    out = vr.run_verilated_binary(top_module="tb_top", bin_dir=tmp_path)
    assert SIM_INFRA_ERROR_PREFIX in out
    assert (tmp_path / "run.log").exists()


def test_cocotb_vtop_binary_is_named_not_silently_run(tmp_path: Path):
    """SETUP-F-40: the cocotb flow builds Vtop, not V<top>. Picking it up here
    would run a VPI-driven binary with no cocotb env (an empty pass), so the
    run-half must name the mismatch instead."""
    (tmp_path / "Vtop").write_text("#!/bin/sh\n", encoding="utf-8")
    assert vr._find_binary(tmp_path, "ravenoc_wrapper") is None

    out = vr.run_verilated_binary(top_module="ravenoc_wrapper", bin_dir=tmp_path)
    assert "Cocotb Target" in out
    assert "booley.sim.cocotb_run" in out


def test_evaluate_verdict_pass_writes_run_log(tmp_path: Path, capsys):
    """The full raw output lands in run.log next to result.json on a PASS."""
    output = "V banner\nsome TB chatter\n[SIM_RESULT] PASSED\n"
    vr._evaluate_verdict(output, 0, tmp_path)
    assert (tmp_path / "run.log").read_text(encoding="utf-8") == output
    assert (tmp_path / "result.json").exists()


def test_evaluate_verdict_fail_writes_run_log(tmp_path: Path, capsys):
    """run.log is written on a FAIL too — that's when it matters most."""
    output = "ERROR: scoreboard mismatch\n[SIM_RESULT] FAILED\n"
    vr._evaluate_verdict(output, 1, tmp_path)
    assert (tmp_path / "run.log").read_text(encoding="utf-8") == output


def test_evaluate_verdict_captures_uppercase_error_bang(tmp_path: Path, capsys):
    """Project fail sentinels commonly use uppercase ``ERROR!`` wording."""
    output = "[0]ERROR! list crc 0x1234 - should be 0x5678\n"
    vr._evaluate_verdict(output, 0, tmp_path, fail_sentinels=["ERROR!"])
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["first_error"] == output.strip()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-half execs a POSIX sim binary (#!/bin/sh stub); real sims run in-container",
)
def test_run_verilated_binary_writes_run_log_end_to_end(tmp_path: Path, capsys):
    """The full run path persists the raw output to <work_dir>/run.log —
    including on a FAIL (nonzero rc + FAIL sentinel)."""
    bin_dir = tmp_path / "build"
    bin_dir.mkdir()
    exe = bin_dir / "Vtb_top"
    exe.write_text("#!/bin/sh\necho 'TB chatter'\necho '[SIM_RESULT] FAILED'\nexit 1\n")
    exe.chmod(0o755)

    work_dir = tmp_path / "work"
    vr.run_verilated_binary(top_module="tb_top", bin_dir=bin_dir, work_dir=work_dir, timeout=30)

    log = (work_dir / "run.log").read_text(encoding="utf-8")
    assert "TB chatter" in log
    assert "[SIM_RESULT] FAILED" in log
    assert (work_dir / "result.json").exists()  # run.log sits beside result.json


def test_run_verilated_binary_creates_missing_work_dir(tmp_path: Path):
    """The run-half owns its output dir: a caller-derived trace/result dir that
    doesn't exist yet is created (else os.mkfifo / write_result_json would fail
    on a missing parent — the bug that broke the first coverage A.2 run)."""
    bin_dir = tmp_path / "build"
    bin_dir.mkdir()
    work_dir = tmp_path / "sim" / "config_a"  # nested, does not exist
    # No binary present → returns early after the mkdir, before any run.
    vr.run_verilated_binary(top_module="tb_top", bin_dir=bin_dir, work_dir=work_dir)
    assert work_dir.is_dir()


# ---------------------------------------------------------------------------
# _check_dut_info_diagnostics (Verilator)
# ---------------------------------------------------------------------------


class TestVerilatorDutInfoDiagnostics:
    """Phase 5.3: empirical patterns from observed Verilator failures.

    Verilator's wording may shift across versions — these patterns are
    best-effort and tagged EMPIRICAL in the source.
    """

    def test_no_such_scope_flags_hier_paths(self):
        out = "%Error: no such scope tb.dut.foo\n"
        msg = vr._check_dut_info_diagnostics(out)
        assert msg is not None
        assert "dut_hier_path" in msg

    def test_cannot_find_module_flags_tb_top(self):
        out = "%Error: Cannot find module tb_typo\n"
        msg = vr._check_dut_info_diagnostics(out)
        assert msg is not None
        assert "tb_top_module" in msg

    def test_cannot_find_file_flags_tb_top(self):
        out = "%Error: Cannot find file: verif/tb.sv\n"
        msg = vr._check_dut_info_diagnostics(out)
        assert msg is not None
        assert "tb_top_module" in msg

    def test_hierarchical_reference_not_found(self):
        out = "%Error: Hierarchical reference not found: tb.dut.x\n"
        msg = vr._check_dut_info_diagnostics(out)
        assert msg is not None
        assert "dut_hier_path" in msg

    def test_clean_output_returns_none(self):
        assert vr._check_dut_info_diagnostics("all clean\n") is None

    def test_empty_output_returns_none(self):
        assert vr._check_dut_info_diagnostics("") is None


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_parse_args_rejects_non_positive_timeout(bad: str):
    # A non-positive timeout would make the deadline math time out instantly.
    with pytest.raises(SystemExit):
        vr._parse_args(["--bin-dir", "b", "--top", "t", "--timeout", bad])


# ---------------------------------------------------------------------------
# Per-run safety guards: missing $readmemh (SETUP-23) + disk budget (SETUP-25)
# ---------------------------------------------------------------------------


def test_parse_args_round_trips_max_rundir_bytes():
    args = vr._parse_args(["--bin-dir", "b", "--top", "t", "--max-rundir-bytes", "5000"])
    assert args.max_rundir_bytes == 5000
    # Unset -> guard disabled (0).
    assert vr._parse_args(["--bin-dir", "b", "--top", "t"]).max_rundir_bytes == 0


def test_stream_output_kills_on_missing_readmemh(tmp_path: Path):
    """SETUP-23: the missing-$readmemh warning kills the run fast, not at the
    wall-clock timeout (no trace session — plain pass/fail run)."""
    import os
    import sys
    import time

    run = tmp_path / "run"
    run.mkdir()
    script = (
        "import sys, time; "
        'print("%Warning: tb.sv:9: $readmemh: cannot open file \\"boot.hex\\""); '
        "sys.stdout.flush(); time.sleep(60)"
    )
    start = time.monotonic()
    lines, proc = vr._stream_output(
        [sys.executable, "-c", script],
        run,
        os.environ.copy(),
        30,
        None,
        None,
    )
    elapsed = time.monotonic() - start
    out = "".join(lines)
    assert "missing $readmemh" in out
    assert elapsed < 15
    assert proc.poll() is not None


def test_disk_baseline_is_taken_before_the_spawn(tmp_path: Path, monkeypatch):
    """A3: the pre-existing-bytes snapshot must predate the simulator.

    ``dir_size_bytes`` is a recursive scandir walk — seconds on the 3.3 GB tree
    that motivated the growth budget — so a baseline taken after the Popen
    absorbs everything the sim dumped during the walk (free bytes, a silently
    raised budget) and puts a full tree walk on the sim-start critical path.
    """
    import os
    import subprocess
    import sys

    import booley.sim.run_guard as rg

    order: list[str] = []
    real_snapshot = rg.snapshot_dir_baseline
    real_popen = subprocess.Popen

    def _snapshot(rundir, budget):
        order.append("baseline")
        return real_snapshot(rundir, budget)

    def _popen(*args, **kwargs):
        order.append("spawn")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(rg, "snapshot_dir_baseline", _snapshot)
    monkeypatch.setattr(subprocess, "Popen", _popen)

    run = tmp_path / "run"
    run.mkdir()
    vr._stream_output(
        [sys.executable, "-c", "pass"],
        run,
        os.environ.copy(),
        30,
        None,
        None,
        max_rundir_bytes=1 << 20,
    )
    assert order == ["baseline", "spawn"]


def test_stream_output_kills_on_disk_runaway(tmp_path: Path, monkeypatch):
    """SETUP-25: a run dir over budget is killed even with no stdout (silent
    runaway) — the watchdog polls while the stdout loop is blocked."""
    import functools
    import os
    import sys
    import time

    import booley.sim.run_guard as rg

    monkeypatch.setattr(
        rg,
        "DiskBudgetGuard",
        functools.partial(rg.DiskBudgetGuard, interval=0.02),
    )
    run = tmp_path / "run"
    run.mkdir()
    script = (
        f'open({str(run / "big.bin")!r}, "wb").write(b"0" * 200_000); import time; time.sleep(60)'
    )
    start = time.monotonic()
    lines, proc = vr._stream_output(
        [sys.executable, "-c", script],
        run,
        os.environ.copy(),
        30,
        None,
        None,
        max_rundir_bytes=1024,
    )
    elapsed = time.monotonic() - start
    out = "".join(lines)
    assert "run directory" in out and "budget" in out
    assert "max_rundir_bytes" in out
    assert elapsed < 15
    assert proc.poll() is not None


def test_stream_output_kills_a_silent_sim_at_the_deadline(tmp_path: Path):
    """F-13/F-21: a sim that prints NOTHING is still killed by the run-half.

    The in-loop deadline check only fires when a line arrives, so a silent
    testbench used to block here past its whole budget and be reaped by
    simulate's wrapper timeout instead — which kills only the `sh -c` and
    orphans this supervisor plus its simulator.
    """
    import os
    import sys
    import time

    run = tmp_path / "run"
    run.mkdir()
    start = time.monotonic()
    lines, proc = vr._stream_output(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        run,
        os.environ.copy(),
        1,  # 1-second budget
        None,
        None,
    )
    elapsed = time.monotonic() - start
    out = "".join(lines)
    assert "timed out" in out
    # F-21: the message attributes the silence rather than just saying "slow".
    assert "printed NO output at all" in out
    assert elapsed < 20
    assert proc.poll() is not None


def test_stream_output_refreshes_run_log_while_running(tmp_path: Path, monkeypatch):
    """F-18: run.log carries a live tail mid-run, not just a placeholder."""
    import os
    import sys

    from booley.sim.sim_result import begin_run_log, run_log_is_current

    monkeypatch.setattr(vr, "RUN_LOG_PROGRESS_INTERVAL_S", 0.0)
    run = tmp_path / "run"
    run.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    begin_run_log(work, flow="sim", target="sim_x", run="run-live")
    script = 'print("cycle 1"); print("cycle 2")'
    vr._stream_output(
        [sys.executable, "-c", script],
        run,
        os.environ.copy(),
        30,
        None,
        None,
        work_dir=work,
    )
    content = (work / "run.log").read_text(encoding="utf-8")
    assert "cycle 2" in content
    assert "output line(s)" in content
    # Still "in progress": _evaluate_verdict has not landed the real log yet.
    assert run_log_is_current(work, "run-live") is False


class TestDeclaredTraceFiles:
    """F-22: a custom main()'s dump can be declared instead of guessed."""

    class _FakeTrace:
        def __init__(self, store: Path | None = None):
            self.store = store
            self.postprocessed: list[Path] = []

        def postprocess(self, path: Path) -> None:
            self.postprocessed.append(path)

        def find(self):
            return self.store

    def test_declared_vcd_is_postprocessed_and_adopted(self, tmp_path: Path):
        run_cwd = tmp_path / "tests"
        run_cwd.mkdir()
        (run_cwd / "fpu.vcd").write_text("$var wire 1 ! clk $end\n")
        store = tmp_path / "trace.fst"
        store.write_bytes(b"fst")
        trace = self._FakeTrace(store)
        found = vr.adopt_declared_trace_files(trace, ["fpu.vcd"], [run_cwd, tmp_path])
        assert found == store
        assert trace.postprocessed == [run_cwd / "fpu.vcd"]

    def test_globs_and_non_vcd_artifacts(self, tmp_path: Path):
        (tmp_path / "dump_003.fst").write_bytes(b"fst")
        trace = self._FakeTrace(None)
        found = vr.adopt_declared_trace_files(trace, ["dump_*.fst"], [tmp_path])
        assert found == tmp_path / "dump_003.fst"
        assert trace.postprocessed == []  # already a store; no conversion

    def test_huge_vcd_is_adopted_raw_instead_of_converted(self, tmp_path: Path, monkeypatch):
        """The conversion is bounded — it runs inside the CALLER's budget.

        ``TraceSession.postprocess`` shells out to an unbounded ``bwave build``
        at finalize time, after the sim's own ``--timeout`` was honoured but
        still inside the Flow-level ``timeout_ms``. On the 4.66 GB dump this
        feature was written for that turns a passing simulation into a timeout
        kill, so past the cap the raw VCD is adopted as-is — the run is still
        reported as having produced a waveform, which was F-22's real complaint.
        """
        monkeypatch.setattr(vr, "_MAX_ADOPTED_VCD_BYTES", 16)
        big = tmp_path / "fpu.vcd"
        big.write_bytes(b"0" * 64)
        trace = self._FakeTrace(tmp_path / "trace.fst")

        found = vr.adopt_declared_trace_files(trace, ["fpu.vcd"], [tmp_path])

        assert found == big  # the dump itself, not a store
        assert trace.postprocessed == []  # no unbounded conversion was attempted

    def test_vcd_under_the_cap_is_still_converted(self, tmp_path: Path):
        store = tmp_path / "trace.fst"
        store.write_bytes(b"fst")
        (tmp_path / "fpu.vcd").write_bytes(b"0" * 64)
        trace = self._FakeTrace(store)

        assert vr.adopt_declared_trace_files(trace, ["fpu.vcd"], [tmp_path]) == store
        assert trace.postprocessed == [tmp_path / "fpu.vcd"]

    def test_empty_and_missing_candidates_are_ignored(self, tmp_path: Path):
        (tmp_path / "empty.vcd").write_text("")
        trace = self._FakeTrace(None)
        assert vr.adopt_declared_trace_files(trace, ["empty.vcd"], [tmp_path]) is None
        assert vr.adopt_declared_trace_files(trace, ["nope.vcd"], [tmp_path]) is None
        assert vr.adopt_declared_trace_files(trace, [], [tmp_path]) is None

    def test_finalize_trace_adopts_the_declared_file(self, tmp_path: Path, capsys):
        (tmp_path / "fpu.vcd").write_text("$var wire 1 ! clk $end\n")
        store = tmp_path / "trace.fst"
        store.write_bytes(b"fst")

        class _Trace(TestDeclaredTraceFiles._FakeTrace):
            def __init__(self):
                super().__init__(None)
                self.calls = 0

            def find(self):
                # First probe (Booley's own artifacts) misses; the adopted VCD
                # is what makes the second one hit.
                self.calls += 1
                return store if self.postprocessed else None

        trace = _Trace()
        suffix = vr._finalize_trace(
            trace, None, None, trace_files=["fpu.vcd"], search_dirs=[tmp_path]
        )
        assert f"TRACE_OK: {store}" in suffix

    def test_incident_names_the_knob_when_nothing_is_declared(self, tmp_path: Path, capsys):
        class _Trace(TestDeclaredTraceFiles._FakeTrace):
            def write_incident(self, reason, **_kw):
                path = tmp_path / "trace_incident.txt"
                path.write_text(reason)
                return path

        suffix = vr._finalize_trace(_Trace(None), None, None)
        assert "trace_files" in suffix


def test_trace_file_round_trips_through_the_cli():
    args = vr._parse_args(["--bin-dir", "b", "--top", "t", "--trace", "--trace-file=fpu.vcd"])
    assert args.trace_files == ["fpu.vcd"]
    assert vr._parse_args(["--bin-dir", "b", "--top", "t"]).trace_files == []
