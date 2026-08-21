"""Unit tests for booley.sim.xcelium_run — the broker-era Xcelium parse-half.

Log snippets are frozen excerpts of real xrun(64) 21.03-s001 logs captured in
the Phase B calibration loop (ADR 0025, 2026-07), with design names
genericized (lite/tb_lite). Observed exit codes: TB data-mismatch FAIL exits 0
(plain $finish — the [SIM_RESULT] sentinel is the only signal), SVA failure
exits 1 with the sim running to completion, $fatal exits 2, an external
SIGTERM (timeout) exits 124 with a *W,NCTERM line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.sim.xcelium_run import (
    evaluate_xcelium_log,
    main,
    reemit_xcelium_summary,
)

# --------------------------------------------------------------------------
# Frozen excerpts of real xrun 21.03-s001 logs (Phase B; names genericized)
# --------------------------------------------------------------------------

_PASS_LOG = """\
xcelium> run
=================================================================
  PASSED 1/1 tests
=================================================================
[SIM_RESULT] PASSED
Simulation complete via $finish(1) at time 272335 NS + 0
./src/lite_0/tb/tb_lite.sv:600     $finish;
xcelium> exit
TOOL:\txrun(64)\t21.03-s001: Exiting on Jul 02, 2026 at 13:00:17 IDT  (total: 00:00:00)
"""

# TB data mismatch: the TB ends with a plain $finish, so xrun exits 0 —
# the [SIM_RESULT] FAILED sentinel is the only failure signal.
_FAIL_LOG = """\
xcelium> run
>>> Test 1/3: data/cfg1/lite_smoke_mem.txt
Error!                    Addr = 0000  e572f6d4 - Expected
Error!       FAILED       Addr = 0000  d572f6d4 - Received
Error!                    Addr = 0000  30000000 - Delta
Error!       full mismatch report -> mismatch_report.txt (sim work dir)
<<< Test 1/3: FAIL (25597 cycles)  data/cfg1/lite_smoke_mem.txt
  FAILED 1/1 tests
=================================================================
[SIM_RESULT] FAILED
Simulation complete via $finish(1) at time 274335 NS + 0
xcelium> exit
"""

# SVA failures do not stop the sim: the TB ran to its PASSED sentinel, yet
# xrun exited 1 and printed one *E,ASRTST per failing assertion instance
# (immediate asserts get a synthesized __assert_N name, named properties
# keep their label).
_ASSERT_LOG = """\
xcelium> run
xmsim: *E,ASRTST (./src/lite_0/tb/tb_lite.sv,608): (time 100 US) Assertion tb_lite.__assert_1 has failed
CAL immediate assertion failed
xmsim: *E,ASRTST (./src/lite_0/tb/tb_lite.sv,614): (time 100005 NS) Assertion tb_lite.chk_sva has failed
CAL concurrent assertion failed
  PASSED 1/1 tests
[SIM_RESULT] PASSED
Simulation complete via $finish(1) at time 272335 NS + 0
xcelium> exit
"""

# $fatal aborts the run before the TB summary (no sentinel); xrun exits 2.
_FATAL_LOG = """\
xcelium> run
xmsim: *F,FATSEV (./src/lite_0/tb/tb_lite.sv,607): (time 120 US).
TB forced fatal
./src/lite_0/tb/tb_lite.sv:607     $fatal(1, "TB forced fatal");
TOOL:\txrun(64)\t21.03-s001: Exiting on Jul 02, 2026 at 13:01:42 IDT  (total: 00:00:00)
"""

_COMPILE_ERROR_LOG = """\
xmvlog: *E,SVILTY (src/lite_0/rtl/lite_add.sv,383|8): Referring to a datatype as super.<type_name> or this.<type_name> is not legal SystemVerilog..
xrun: *E,VLGERR: An error occurred during parsing.  Review the log file for errors with the code *E and fix those identified problems to proceed.  Exiting with code (status 1).
"""

_ELAB_ERROR_LOG = """\
xmelab: *E,NOUNIT: Unable to find a unit named 'tb_liteX' in the libraries.
xrun: *E,ELBERR: Error during elaboration (status 1), exiting.
"""

# Stale hierarchical reference (a TB naming an instance that no longer
# exists) — the dut_hier_path diagnostic wording.
_HIER_ERROR_LOG = """\
xmelab: *E,CUVUNF (./src/lite_0/tb/tb_lite.sv,607|72): Hierarchical name component lookup failed for 'u_dut_stale_name' at 'tb_lite'.
xrun: *E,ELBERR: Error during elaboration (status 1), exiting.
"""

# External SIGTERM (e.g. a broker/timeout kill): warning-level NCTERM line,
# no sentinel, no summary — the exit code (124 under timeout(1)) is the signal.
_SIGTERM_LOG = """\
>>> Test 1/3: data/cfg1/lite_smoke_mem.txt
xmsim: *W,NCTERM: Simulation received SIGTERM signal from process 1785010, user id 11427 (timeout).
TOOL:\txrun(64)\t21.03-s001: Exiting on Jul 02, 2026 at 13:01:06 IDT  (total: 00:00:00)
"""

_CLEAN_NO_SENTINEL_LOG = """\
xcelium> run
Simulation complete via $finish(1) at time 100 NS + 0
xcelium> exit
"""


class TestEvaluateXceliumLog:
    def test_pass_sentinel_passes(self):
        v = evaluate_xcelium_log(_PASS_LOG, 0)
        assert v.passed and not v.inconclusive
        assert v.sva_errors == 0

    def test_fail_sentinel_fails(self):
        # Real exit code for a TB data mismatch is 0 (plain $finish) — the
        # sentinel alone must drive the verdict.
        v = evaluate_xcelium_log(_FAIL_LOG, 0)
        assert not v.passed and not v.inconclusive
        assert "Error!" in v.first_error

    def test_assertion_counts_and_fails(self):
        # Real behavior: assertions fail but the sim runs on to a PASSED
        # sentinel; the counted *E,ASRTST lines must still force a FAIL.
        # Each ASRTST line is counted once — the trailing "has failed"
        # wording is not double-counted by the generic TB markers.
        v = evaluate_xcelium_log(_ASSERT_LOG, 1)
        assert not v.passed
        assert v.sva_errors == 2

    def test_fatal_fails_without_sentinel(self):
        v = evaluate_xcelium_log(_FATAL_LOG, 2)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 1  # the *F,FATSEV line
        assert "*F,FATSEV" in v.first_error

    def test_compile_error_fails(self):
        v = evaluate_xcelium_log(_COMPILE_ERROR_LOG, 2)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 2  # *E,SVILTY + *E,VLGERR
        assert "*E,SVILTY" in v.first_error

    def test_elab_error_fails(self):
        v = evaluate_xcelium_log(_ELAB_ERROR_LOG, 2)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 2  # *E,NOUNIT + *E,ELBERR
        assert "*E,NOUNIT" in v.first_error

    def test_sigterm_kill_fails_on_exit_code(self):
        # *W,NCTERM is warning-level (not counted); exit 124 drives the FAIL.
        v = evaluate_xcelium_log(_SIGTERM_LOG, 124)
        assert not v.passed and not v.inconclusive
        assert v.sva_errors == 0

    def test_nonzero_exit_without_sentinel_fails(self):
        v = evaluate_xcelium_log("some xrun noise\n", 3)
        assert not v.passed and not v.inconclusive

    def test_clean_exit_without_sentinel_is_inconclusive(self):
        v = evaluate_xcelium_log(_CLEAN_NO_SENTINEL_LOG, 0)
        assert not v.passed and v.inconclusive
        assert v.first_error == ""

    def test_pass_sentinel_with_errors_fails(self):
        log = _PASS_LOG + "xmsim: *E,ASRTST (./t.sv,1): assertion failed\n"
        v = evaluate_xcelium_log(log, 0)
        assert not v.passed

    def test_sentinel_found_past_default_tail_window(self):
        # A long build preamble must not bury the sentinel (full-text scan).
        log = ("xmvlog compile noise line\n" * 500) + _PASS_LOG
        assert evaluate_xcelium_log(log, 0).passed


class TestReemitXceliumSummary:
    def test_appends_pass_summary(self):
        out = reemit_xcelium_summary(_PASS_LOG, 0)
        assert '[SIM_SUMMARY] {"passed":true' in out
        assert out.startswith("xcelium> run")

    def test_appends_fail_summary(self):
        out = reemit_xcelium_summary(_FAIL_LOG, 0)
        assert '[SIM_SUMMARY] {"passed":false' in out

    def test_clean_no_sentinel_marks_inconclusive(self):
        out = reemit_xcelium_summary(_CLEAN_NO_SENTINEL_LOG, 0)
        assert '"inconclusive":true' in out

    def test_idempotent_when_summary_present(self):
        raw = 'log\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        assert reemit_xcelium_summary(raw, 0) == raw


class TestOfflineCli:
    """`python -m booley.sim.xcelium_run --parse-log` — the copy-back loop helper."""

    def test_parse_pass_log(self, tmp_path: Path, capsys):
        log = tmp_path / "xrun.log"
        log.write_text(_PASS_LOG, encoding="utf-8")
        rc = main(["--parse-log", str(log)])
        assert rc == 0
        out = capsys.readouterr().out
        assert '[SIM_SUMMARY] {"passed":true' in out
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["passed"] is True

    def test_parse_fail_log_with_exit_code_and_work_dir(self, tmp_path: Path, capsys):
        log = tmp_path / "xrun.log"
        log.write_text(_ASSERT_LOG, encoding="utf-8")
        work = tmp_path / "out"
        work.mkdir()
        rc = main(["--parse-log", str(log), "--exit-code", "1", "--work-dir", str(work)])
        assert rc == 1
        result = json.loads((work / "result.json").read_text())
        assert result["passed"] is False
        assert result["sva_errors"] == 2
        assert result["returncode"] == 1
        assert "ASRTST" in result["first_error"]

    def test_parse_pass_log_writes_run_log(self, tmp_path: Path, capsys):
        """The full raw log is persisted verbatim as <work_dir>/run.log on a
        PASS — result.json alone can't survive stdout truncation."""
        log = tmp_path / "xrun.log"
        log.write_text(_PASS_LOG, encoding="utf-8")
        work = tmp_path / "out"
        work.mkdir()
        main(["--parse-log", str(log), "--work-dir", str(work)])
        assert (work / "run.log").read_text(encoding="utf-8") == _PASS_LOG
        assert (work / "result.json").exists()  # run.log sits beside result.json

    def test_parse_fail_log_writes_run_log(self, tmp_path: Path, capsys):
        """run.log is written on a FAIL too (default work dir = log dir)."""
        log = tmp_path / "xrun.log"
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
        log = tmp_path / "xrun.log"
        log.write_text("[SIM_RESULT] FAILED\n", encoding="utf-8")
        main(["--parse-log", str(log), "--exit-code", "0"])
        out = capsys.readouterr().out
        assert "FAILED (fail sentinel matched)" in out
        assert "rc=0" not in out
