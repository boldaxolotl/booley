"""Unit tests for booley.sim.vcs_run — the broker-era VCS parse-half.

Build-half fixtures (``Error-[TAG]`` block headers, ``Warning-[TAG]`` no-count,
license failure) are frozen excerpts of real vcs X-2025.06-1 logs captured on
the EDA host in the Phase D calibration loop (ADR 0025, 2026-07), names
genericized (lite/tb_lite). Run-half fixtures (``Error:``/``Fatal:`` runtime
severity-task lines from simv) remain PROVISIONAL — the EDA host had no
reachable Synopsys license daemon, so no simv ever ran; freeze them when the
license infra is fixed, exactly as the xcelium fixtures were in Phase B.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.sim.vcs_run import (
    _check_dut_info_diagnostics,
    evaluate_vcs_log,
    main,
    reemit_vcs_summary,
)

# --------------------------------------------------------------------------
# PROVISIONAL VCS-shaped log excerpts (pending Phase D calibration freeze)
# --------------------------------------------------------------------------

_PASS_LOG = """\
Chronologic VCS simulator copyright 1991-2025
=================================================================
  PASSED 1/1 tests
=================================================================
[SIM_RESULT] PASSED
$finish called from file "./src/lite_0/tb/tb_lite.sv", line 600.
$finish at simulation time 272335 ns
           V C S   S i m u l a t i o n   R e p o r t
"""

# TB data mismatch: the TB ends with a plain $finish, so simv exits 0 —
# the [SIM_RESULT] FAILED sentinel is the only failure signal.
_FAIL_LOG = """\
>>> Test 1/3: data/cfg1/lite_smoke_mem.txt
Error!                    Addr = 0000  e572f6d4 - Expected
Error!       FAILED       Addr = 0000  d572f6d4 - Received
Error!                    Addr = 0000  30000000 - Delta
<<< Test 1/3: FAIL (25597 cycles)  data/cfg1/lite_smoke_mem.txt
  FAILED 1/1 tests
=================================================================
[SIM_RESULT] FAILED
$finish at simulation time 274335 ns
"""

# SVA failures do not stop the sim: the TB ran to its PASSED sentinel, yet
# simv printed one Error:-prefixed line per failing assertion (the default
# assertion action is $error).
_ASSERT_LOG = """\
Error: "src/lite_0/tb/tb_lite.sv", 608: tb_lite.unnamed$$_0: at time 100000 ns
CAL immediate assertion failed
Error: "src/lite_0/tb/tb_lite.sv", 614: tb_lite.chk_sva: started at 100005ns failed at 100005ns
CAL concurrent assertion failed
  PASSED 1/1 tests
[SIM_RESULT] PASSED
$finish at simulation time 272335 ns
"""

# $fatal aborts the run before the TB summary (no sentinel).
_FATAL_LOG = """\
Fatal: "src/lite_0/tb/tb_lite.sv", 607: tb_lite: at time 120000 ns
TB forced fatal
$finish called from file "src/lite_0/tb/tb_lite.sv", line 607.
"""

# vlogan syntax error (3-stage front-end): Error-[TAG] block header.
# PROVISIONAL (no license on the EDA host reached the vlogan-error class).
_COMPILE_ERROR_LOG = """\
Error-[SE] Syntax error
  Following verilog source has syntax error :
  "src/lite_0/rtl/lite_add.sv", 383: token is ';'
1 error
CPU time: .057 seconds to compile
"""

# Elaboration with a stale -top unit name.
# PROVISIONAL (same reason as _COMPILE_ERROR_LOG).
_ELAB_ERROR_LOG = """\
Error-[URMI] Unresolved modules
  Unresolved module tb_liteX in the design.
"""

# REAL (frozen, vcs X-2025.06-1): common-elaboration error block — VCS's
# strict driver check rejects a declaration initializer on an always_ff-driven
# array (a VCS-only strictness; xcelium/verilator accept the same RTL). Block
# body lines must not double-count; the trailing Booley wrapper marker line
# ("ERROR: vcs elaboration failed") is what simulate._ELAB_FAIL_RE scrapes.
_ELAB_ICPD_LOG = """\
Top Level Modules:
       tb_lite
TimeScale is 1 ns / 1 ns

Error-[ICPD_INIT] Illegal combination of drivers
src/lite_0/rtl/lite_ram.sv, 38
  Illegal combination of procedural drivers
  Variable "mem" is driven by an invalid combination of procedural drivers.
  Variables written on left-hand of "always_ff" cannot be written to by any
  other processes, including other "always_ff" processes.
  Use '-ignore initializer_driver_checks' to suppress this error


Error-[ICPD_INIT] Illegal combination of drivers
src/lite_0/rtl/lite_ram.sv, 38
  Illegal combination of procedural drivers
  Variable "mem" is driven by an invalid combination of procedural drivers.
  Use '-ignore initializer_driver_checks' to suppress this error

2 errors
CPU time: .384 seconds to compile

 common elaboration failed
make[1]: *** [Makefile:13: lite_0] Error 255
ERROR: vcs elaboration failed (rc=2)
"""

# REAL (frozen, vcs X-2025.06-1): license checkout failure during common
# elaboration — no Error-/Fatal-[TAG] line at all; the Booley wrapper marker
# plus the non-zero exit are the only machine signals.
_LICENSE_FAIL_LOG = """\
Top Level Modules:
       tb_lite
TimeScale is 1 ns / 1 ns
Cannot connect to the license server.
The connect() system call failed.
Make sure that your SNPSLMD_LICENSE_FILE is pointing to the right
location and that the license server is up.
Retrying request.....


 Failed to obtain license ...
Note: Use +vcs+lic+wait ( or -licwait <minute> or -licqueue  in Unified Use Model ) to queue for license
CPU time: .365 seconds to compile

 common elaboration failed
make[1]: *** [Makefile:13: lite_0] Error 255
ERROR: vcs elaboration failed (rc=2)
"""

# Stale hierarchical reference (a TB naming an instance that no longer
# exists) — the dut_hier_path diagnostic wording.
_HIER_ERROR_LOG = """\
Error-[XMRE] Cross-module reference resolution error
  Error found while trying to resolve cross-module reference.
  The hierarchical reference 'tb_lite.u_dut_stale_name.val' could not be resolved.
  Source info: "src/lite_0/tb/tb_lite.sv", 607: tb_lite.u_dut_stale_name.val
"""

_CLEAN_NO_SENTINEL_LOG = """\
$finish at simulation time 100 ns
           V C S   S i m u l a t i o n   R e p o r t
"""


class TestEvaluateVcsLog:
    def test_pass_sentinel_passes(self):
        v = evaluate_vcs_log(_PASS_LOG, 0)
        assert v.passed and not v.inconclusive
        assert v.sva_errors == 0

    def test_fail_sentinel_fails(self):
        # A TB data mismatch exits 0 (plain $finish) — the sentinel alone
        # must drive the verdict (matches the xcelium empirical table).
        v = evaluate_vcs_log(_FAIL_LOG, 0)
        assert not v.passed and not v.inconclusive
        assert "Error!" in v.first_error

    def test_assertion_counts_and_fails(self):
        # Assertions fail but the sim runs on to a PASSED sentinel; the
        # counted Error:-prefixed lines must still force a FAIL. Each line
        # is counted once — the trailing "failed at" wording is not
        # double-counted by the generic TB markers.
        v = evaluate_vcs_log(_ASSERT_LOG, 1)
        assert not v.passed
        assert v.sva_errors == 2

    def test_fatal_fails_without_sentinel(self):
        v = evaluate_vcs_log(_FATAL_LOG, 1)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 1  # the Fatal: line
        assert "Fatal:" in v.first_error

    def test_compile_error_fails(self):
        v = evaluate_vcs_log(_COMPILE_ERROR_LOG, 1)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 1  # the Error-[SE] block header
        assert "Error-[SE]" in v.first_error

    def test_elab_error_fails(self):
        v = evaluate_vcs_log(_ELAB_ERROR_LOG, 1)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 1  # the Error-[URMI] block header
        assert "Error-[URMI]" in v.first_error

    def test_real_elab_icpd_block_fails(self):
        # Real X-2025.06-1 wording: two Error-[ICPD_INIT] block headers; the
        # indented block bodies must not inflate the count, and neither must
        # Booley's own "ERROR: vcs elaboration failed" wrapper marker — that is
        # EDA-tool infrastructure, not a DUT assertion (F-30). Observed exit: make
        # wraps vcs's 255 into 2.
        v = evaluate_vcs_log(_ELAB_ICPD_LOG, 2)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 2  # the 2 ICPD_INIT headers, nothing else
        assert "Error-[ICPD_INIT]" in v.first_error

    def test_real_license_failure_fails_on_exit_code(self):
        # Real X-2025.06-1 wording: a license checkout failure emits NO
        # tagged error lines — the exit code carries the verdict (never
        # inconclusive), and the SVA count stays 0: no simulator ran, so there
        # is no statement to make about the design's assertions (F-30).
        v = evaluate_vcs_log(_LICENSE_FAIL_LOG, 2)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 0
        assert "vcs elaboration failed" in v.first_error

    def test_nonzero_exit_without_sentinel_fails(self):
        v = evaluate_vcs_log("some simv noise\n", 3)
        assert not v.passed and not v.inconclusive

    def test_clean_exit_without_sentinel_is_inconclusive(self):
        v = evaluate_vcs_log(_CLEAN_NO_SENTINEL_LOG, 0)
        assert not v.passed and v.inconclusive
        assert v.first_error == ""

    def test_pass_sentinel_with_errors_fails(self):
        log = _PASS_LOG + 'Error: "t.sv", 1: tb.a: at time 5 ns\n'
        v = evaluate_vcs_log(log, 0)
        assert not v.passed

    def test_sentinel_found_past_default_tail_window(self):
        # A long build preamble must not bury the sentinel (full-text scan).
        log = ("vlogan compile noise line\n" * 500) + _PASS_LOG
        assert evaluate_vcs_log(log, 0).passed


class TestReemitVcsSummary:
    def test_appends_pass_summary(self):
        out = reemit_vcs_summary(_PASS_LOG, 0)
        assert '[SIM_SUMMARY] {"passed":true' in out
        assert out.startswith("Chronologic VCS")

    def test_appends_fail_summary(self):
        out = reemit_vcs_summary(_FAIL_LOG, 0)
        assert '[SIM_SUMMARY] {"passed":false' in out

    def test_clean_no_sentinel_marks_inconclusive(self):
        out = reemit_vcs_summary(_CLEAN_NO_SENTINEL_LOG, 0)
        assert '"inconclusive":true' in out

    def test_idempotent_when_summary_present(self):
        raw = 'log\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        assert reemit_vcs_summary(raw, 0) == raw


class TestDutInfoDiagnostics:
    def test_unbound_hierarchy_names_dut_hier_path(self):
        msg = _check_dut_info_diagnostics(_HIER_ERROR_LOG)
        assert msg and "dut_hier_path" in msg
        assert "u_dut_stale_name" in msg

    def test_missing_top_names_tb_top_module(self):
        msg = _check_dut_info_diagnostics(_ELAB_ERROR_LOG)
        assert msg and "tb_top_module" in msg

    def test_clean_log_yields_none(self):
        assert _check_dut_info_diagnostics(_PASS_LOG) is None
        assert _check_dut_info_diagnostics("") is None


class TestOfflineCli:
    """`python -m booley.sim.vcs_run --parse-log` — the copy-back loop helper."""

    def test_parse_pass_log(self, tmp_path: Path, capsys):
        log = tmp_path / "vcs.log"
        log.write_text(_PASS_LOG, encoding="utf-8")
        rc = main(["--parse-log", str(log)])
        assert rc == 0
        out = capsys.readouterr().out
        assert '[SIM_SUMMARY] {"passed":true' in out
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["passed"] is True

    def test_parse_fail_log_with_exit_code_and_work_dir(self, tmp_path: Path, capsys):
        log = tmp_path / "vcs.log"
        log.write_text(_ASSERT_LOG, encoding="utf-8")
        work = tmp_path / "out"
        work.mkdir()
        rc = main(["--parse-log", str(log), "--exit-code", "1", "--work-dir", str(work)])
        assert rc == 1
        result = json.loads((work / "result.json").read_text())
        assert result["passed"] is False
        assert result["sva_errors"] == 2
        assert result["returncode"] == 1
        assert "Error:" in result["first_error"]

    def test_parse_pass_log_writes_run_log(self, tmp_path: Path, capsys):
        """The full raw log is persisted verbatim as <work_dir>/run.log on a
        PASS — result.json alone can't survive stdout truncation."""
        log = tmp_path / "vcs.log"
        log.write_text(_PASS_LOG, encoding="utf-8")
        work = tmp_path / "out"
        work.mkdir()
        main(["--parse-log", str(log), "--work-dir", str(work)])
        assert (work / "run.log").read_text(encoding="utf-8") == _PASS_LOG
        assert (work / "result.json").exists()  # run.log sits beside result.json

    def test_parse_fail_log_writes_run_log(self, tmp_path: Path, capsys):
        """run.log is written on a FAIL too (default work dir = log dir)."""
        log = tmp_path / "vcs.log"
        log.write_text(_ASSERT_LOG, encoding="utf-8")
        main(["--parse-log", str(log), "--exit-code", "1"])
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == _ASSERT_LOG

    def test_missing_log_file_is_infra_error(self, tmp_path: Path, capsys):
        rc = main(["--parse-log", str(tmp_path / "nope.log")])
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_fail_sentinel_rc0_avoids_confusing_rc0(self, tmp_path: Path, capsys):
        # A bare FAIL sentinel with a clean exit (no counted assertions) must
        # NOT print the maximally-confusing "(rc=0)".
        log = tmp_path / "simv.log"
        log.write_text("[SIM_RESULT] FAILED\n", encoding="utf-8")
        main(["--parse-log", str(log), "--exit-code", "0"])
        out = capsys.readouterr().out
        assert "FAILED (fail sentinel matched)" in out
        assert "rc=0" not in out
