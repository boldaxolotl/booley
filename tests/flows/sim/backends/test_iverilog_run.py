"""Unit tests for the edalize-era Icarus run-half (booley.flows.sim.backends.icarus).

These cover the pure run-half logic that does not need a real ``vvp``: image
discovery from the ``.scr`` sibling, the vvp command shape (``-M`` + absolute
image, no ``-fst`` → VCD), arg round-trip, and the missing-image early return.
The real vvp run + VCD→bwave postprocess is exercised end-to-end in the Sandbox
(it needs ``fusesoc``/``iverilog`` and per-project Icarus ``.core`` Targets).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from booley.flows.sim.backends import icarus as ir
from booley.flows.sim.backends.shared import find_icarus_image


def test_find_image_strips_scr_extension(tmp_path: Path):
    """Edalize writes <name>.scr next to the vvp image named <name> (no ext)."""
    (tmp_path / "sim_cfg.scr").write_text("")
    assert find_icarus_image(tmp_path) == "sim_cfg"


def test_find_image_none_when_no_scr(tmp_path: Path):
    assert find_icarus_image(tmp_path) is None


def test_build_vvp_cmd_uses_M_and_absolute_image_no_fst(tmp_path: Path):  # noqa: N802 — `M` names the -M flag under test
    cmd = ir._build_vvp_cmd("vvp", tmp_path, "sim_cfg", ["test_id=1", "+already"])
    assert cmd[0] == "vvp"
    assert "-n" in cmd
    # VPI modules resolve from any cwd: -M<abs build dir> + absolute image path.
    assert f"-M{tmp_path}" in cmd
    assert str(tmp_path / "sim_cfg") in cmd
    # bare plusargs gain a leading +, already-prefixed ones are left alone
    assert "+test_id=1" in cmd
    assert "+already" in cmd
    # -fst is omitted so the dump is a VCD the postprocess understands
    assert "-fst" not in cmd


def test_build_vvp_cmd_no_plusargs(tmp_path: Path):
    cmd = ir._build_vvp_cmd("vvp", tmp_path, "sim_cfg", None)
    assert cmd == ["vvp", "-n", f"-M{tmp_path}", str(tmp_path / "sim_cfg")]


def test_parse_args_round_trips_run_options():
    args = ir._parse_args(
        [
            "--build-dir",
            "build/sim",
            "--run-cwd",
            "util/sim",
            "--work-dir",
            "out",
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
    assert args.build_dir == "build/sim"
    assert args.run_cwd == "util/sim"
    assert args.work_dir == "out"
    assert args.timeout == 120
    assert args.trace is True
    assert args.trace_scope == "tb.dut"
    assert args.plusargs == ["test_id=2", "verbose"]


def test_parse_args_round_trips_sentinels():
    args = ir._parse_args(
        [
            "--build-dir",
            "b",
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
    # TB emits its own wording; no [SIM_RESULT] marker. With the configured
    # pass sentinel the run-half must still call it a PASS.
    ir._evaluate_verdict(
        "ALL TESTS PASSED.",
        0,
        tmp_path,
        pass_sentinels=["ALL TESTS PASSED."],
        fail_sentinels=["ERROR!"],
    )
    assert "PASSED" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_parse_args_rejects_non_positive_timeout(bad: str):
    # A non-positive timeout would fire threading.Timer immediately; reject at CLI.
    with pytest.raises(SystemExit):
        ir._parse_args(["--build-dir", "b", "--timeout", bad])


def test_run_icarus_image_missing_image_returns_error(tmp_path: Path):
    """A build dir with no .scr yields a clear error string (no crash, no vvp)."""
    out = ir.run_icarus_image(build_dir=tmp_path)
    assert "no vvp image" in out


def test_run_icarus_image_creates_missing_work_dir(tmp_path: Path):
    """The run-half owns its output dir: a caller-derived work dir that does not
    exist yet is created before any run (parity with the Verilator run-half)."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    work_dir = tmp_path / "sim" / "config_a"  # nested, does not exist
    # No image present → returns early after the mkdir, before any vvp run.
    ir.run_icarus_image(build_dir=build_dir, work_dir=work_dir)
    assert work_dir.is_dir()


def test_evaluate_verdict_pass_writes_result(tmp_path: Path, capsys):
    ir._evaluate_verdict("[SIM_RESULT] PASSED", 0, tmp_path)
    out = capsys.readouterr().out
    assert "PASSED" in out
    assert (tmp_path / "result.json").exists()


def test_evaluate_verdict_inconclusive_on_silent_rc0(tmp_path: Path, capsys):
    ir._evaluate_verdict("no sentinel here", 0, tmp_path)
    assert "INCONCLUSIVE" in capsys.readouterr().out


def test_evaluate_verdict_fail_sentinel_rc0_avoids_confusing_rc0(tmp_path: Path, capsys):
    # A FAIL sentinel with a clean exit must NOT print the confusing "(rc=0)".
    ir._evaluate_verdict("[SIM_RESULT] FAILED", 0, tmp_path)
    out = capsys.readouterr().out
    assert "FAILED (fail sentinel matched)" in out
    assert "rc=0" not in out


def test_evaluate_verdict_fail_sentinel_rc1_cites_rc(tmp_path: Path, capsys):
    ir._evaluate_verdict("[SIM_RESULT] FAILED", 1, tmp_path)
    assert "FAILED (rc=1)" in capsys.readouterr().out


def test_evaluate_verdict_pass_writes_run_log(tmp_path: Path, capsys):
    """The full raw output lands in run.log next to result.json on a PASS."""
    output = "vvp banner\nsome TB chatter\n[SIM_RESULT] PASSED\n"
    ir._evaluate_verdict(output, 0, tmp_path)
    assert (tmp_path / "run.log").read_text(encoding="utf-8") == output


def test_evaluate_verdict_fail_writes_run_log(tmp_path: Path, capsys):
    """run.log is written on a FAIL too — that's when it matters most."""
    output = "ERROR: scoreboard mismatch\n[SIM_RESULT] FAILED\n"
    ir._evaluate_verdict(output, 1, tmp_path)
    assert (tmp_path / "run.log").read_text(encoding="utf-8") == output


def test_evaluate_verdict_captures_uppercase_error_bang(tmp_path: Path, capsys):
    """Project fail sentinels commonly use uppercase ``ERROR!`` wording."""
    output = "[0]ERROR! list crc 0x1234 - should be 0x5678\n"
    ir._evaluate_verdict(output, 0, tmp_path, fail_sentinels=["ERROR!"])
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["first_error"] == output.strip()


@pytest.mark.parametrize("line", ["Error at cycle 9", "Error :( expected 1"])
def test_evaluate_verdict_captures_common_error_spellings(tmp_path: Path, capsys, line: str):
    ir._evaluate_verdict(f"{line}\n[SIM_RESULT] FAILED\n", 1, tmp_path)
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["first_error"] == line


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-half execs a POSIX vvp image (#!/bin/sh stub); real sims run in-container",
)
def test_run_icarus_image_writes_run_log_end_to_end(tmp_path: Path, monkeypatch, capsys):
    """The full run path persists the raw output to <work_dir>/run.log."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "sim_cfg.scr").write_text("")  # image discovery sibling
    (build_dir / "sim_cfg").write_text("")  # the (fake) vvp image
    fake_vvp = tmp_path / "fake_vvp"
    fake_vvp.write_text("#!/bin/sh\necho 'TB chatter'\necho '[SIM_RESULT] PASSED'\n")
    fake_vvp.chmod(0o755)
    monkeypatch.setattr(ir, "_find_vvp", lambda: str(fake_vvp))

    work_dir = tmp_path / "work"
    ir.run_icarus_image(build_dir=build_dir, work_dir=work_dir, timeout=30)

    log = (work_dir / "run.log").read_text(encoding="utf-8")
    assert "TB chatter" in log
    assert "[SIM_RESULT] PASSED" in log
    assert (work_dir / "result.json").exists()  # run.log sits beside result.json


# ---------------------------------------------------------------------------
# Per-run safety guards: missing $readmemh (SETUP-23) + disk budget (SETUP-25)
# ---------------------------------------------------------------------------


def test_parse_args_round_trips_max_rundir_bytes():
    args = ir._parse_args(["--build-dir", "b", "--max-rundir-bytes", "5000"])
    assert args.max_rundir_bytes == 5000
    # Unset -> guard disabled (0).
    assert ir._parse_args(["--build-dir", "b"]).max_rundir_bytes == 0


def test_stream_output_kills_on_missing_readmemh(tmp_path: Path):
    """SETUP-23: the missing-$readmemh warning kills the run fast, not at the
    wall-clock timeout — a real spinning child is torn down on the warning line."""
    import sys
    import time

    run = tmp_path / "run"
    run.mkdir()
    # Emit the warning, flush, then spin so only the guard can end it.
    script = (
        "import sys, time; "
        'print("Cannot open firmware.hex for reading."); sys.stdout.flush(); '
        "time.sleep(60)"
    )
    start = time.monotonic()
    lines, proc = ir._stream_output([sys.executable, "-c", script], run, timeout=30)
    elapsed = time.monotonic() - start
    out = "".join(lines)
    assert "missing $readmemh" in out
    assert elapsed < 15  # killed on the warning, well before the 30s timeout
    assert proc.poll() is not None


def test_stream_output_kills_on_disk_runaway(tmp_path: Path, monkeypatch):
    """SETUP-25: a run dir over budget is killed even with no stdout (silent
    runaway) — the watchdog polls the dir while the stdout loop is blocked."""
    import functools
    import sys
    import time

    import booley.flows.sim.run_guard as rg

    # Poll fast so the test doesn't wait out the 5s production interval.
    monkeypatch.setattr(
        rg,
        "DiskBudgetGuard",
        functools.partial(rg.DiskBudgetGuard, interval=0.02),
    )
    run = tmp_path / "run"
    run.mkdir()
    # Write 200KB up front, then spin silently — no stdout for the loop to see.
    script = (
        f'open({str(run / "big.bin")!r}, "wb").write(b"0" * 200_000); import time; time.sleep(60)'
    )
    start = time.monotonic()
    lines, proc = ir._stream_output(
        [sys.executable, "-c", script],
        run,
        timeout=30,
        max_rundir_bytes=1024,
    )
    elapsed = time.monotonic() - start
    out = "".join(lines)
    assert "run directory" in out and "budget" in out
    assert "max_rundir_bytes" in out
    assert elapsed < 15  # killed on the disk breach, not at the timeout
    assert proc.poll() is not None


def test_disk_baseline_is_taken_before_the_spawn(tmp_path: Path, monkeypatch):
    """A3: the pre-existing-bytes snapshot must predate vvp.

    ``dir_size_bytes`` is a recursive scandir walk — seconds on the multi-GB
    trees this budget exists for — so a baseline taken after the Popen absorbs
    everything vvp dumped during the walk, silently raising the budget.
    """
    import subprocess
    import sys

    import booley.flows.sim.run_guard as rg

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
    ir._stream_output([sys.executable, "-c", "pass"], run, timeout=30, max_rundir_bytes=1 << 20)
    assert order == ["baseline", "spawn"]
