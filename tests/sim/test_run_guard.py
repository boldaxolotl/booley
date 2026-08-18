"""Unit tests for the shared per-run sim guards (booley.sim.run_guard).

Covers the two onboarding-QA safety findings the guard closes:
  * SETUP-23 — a missing $readmemh memory-init file is fatal (the sim would
    otherwise spin forever on uninitialised RAM), detected from its one-line
    warning via ``readmemh_fatal_line``.
  * SETUP-25 — a runaway tracer/$dumpfile fills the run dir; ``DiskBudgetGuard``
    kills the run once the dir crosses a byte budget, and ``dir_size_bytes``
    measures it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.sim import run_guard as rg

# ---------------------------------------------------------------------------
# readmemh_fatal_line (SETUP-23)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        '%Warning: tb.sv:12: $readmemh: cannot open file "firmware.hex"',
        "$readmemh: Cannot open memory.hex",
        "ERROR: $readmemb: unable to open image.bin",
        "Cannot open firmware.hex for reading.",
        "vvp: Unable to open /path/to/mem.vmem for reading",
        "cannot open hex file boot.hex",
    ],
)
def test_readmemh_fatal_line_matches_missing_file_warnings(line: str):
    """Each simulator's missing-memory-file warning is flagged fatal."""
    matched = rg.readmemh_fatal_line(line)
    assert matched == line.strip()


@pytest.mark.parametrize(
    "line",
    [
        "$readmemh loaded 4096 words",
        "opening waveform trace.vcd",
        "some unrelated cannot happen here",
        "Note: file opened successfully",
        "",
    ],
)
def test_readmemh_fatal_line_ignores_benign_lines(line: str):
    """A normal $readmemh / an unrelated 'cannot' must not trip the guard."""
    assert rg.readmemh_fatal_line(line) is None


# ---------------------------------------------------------------------------
# dir_size_bytes (SETUP-25)
# ---------------------------------------------------------------------------


def test_dir_size_bytes_sums_nested_files(tmp_path: Path):
    run = tmp_path / "run"  # clean subdir (conftest seeds .booley_project in tmp_path)
    run.mkdir()
    (run / "a.txt").write_bytes(b"x" * 100)
    sub = run / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)
    assert rg.dir_size_bytes(run) == 350


def test_dir_size_bytes_missing_dir_is_zero(tmp_path: Path):
    """A run dir that does not exist yet reports 0, not an error."""
    assert rg.dir_size_bytes(tmp_path / "nope") == 0


def test_dir_size_bytes_skips_symlinks(tmp_path: Path):
    """Symlinks are not followed, so external targets aren't double-counted."""
    run = tmp_path / "run"
    run.mkdir()
    real = run / "real.bin"
    real.write_bytes(b"z" * 500)
    link = run / "link.bin"
    symlink_or_skip(link, real)
    # Only the real file's bytes count; the symlink is skipped.
    assert rg.dir_size_bytes(run) == 500


# ---------------------------------------------------------------------------
# DiskBudgetGuard (SETUP-25)
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal Popen stand-in: alive until killed, records the kill."""

    def __init__(self):
        self._alive = True
        self.killed = False
        self.pid = 4321

    def poll(self):
        return None if self._alive else 0

    def kill(self):
        self._alive = False
        self.killed = True

    def terminate(self):
        self.kill()

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def send_signal(self, _sig):
        self.kill()


def test_guard_disabled_when_budget_non_positive(tmp_path: Path):
    """budget <= 0 disables the guard: start() is a no-op, never trips."""
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(tmp_path, 0, proc, interval=0.01)
    guard.start()
    time.sleep(0.05)
    guard.stop()
    assert guard.tripped is False
    assert proc.killed is False


def _await_trip(guard, timeout_s: float = 2.0) -> None:
    """Poll until the guard trips (bounded so a bug can't hang the suite)."""
    deadline = time.monotonic() + timeout_s
    while not guard.tripped and time.monotonic() < deadline:
        time.sleep(0.01)
    guard.stop()


def test_guard_kills_on_budget_breach(tmp_path: Path, monkeypatch):
    """Growth past the budget kills the proc and exposes a factual message."""
    # Patch kill_process_tree so the fake proc is 'killed' without real signals.
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())

    run = tmp_path / "run"
    run.mkdir()
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(run, 1024, proc, interval=0.01)
    guard.start()
    # Written AFTER start(): the budget measures growth during the run (F-23).
    runaway = run / "runaway.vcd"
    runaway.write_bytes(b"0" * 2048)
    fresh = time.time() + 1
    os.utime(runaway, (fresh, fresh))
    _await_trip(guard)

    assert guard.tripped is True
    assert proc.killed is True
    assert guard.grown >= 2048
    msg = guard.message
    assert str(run) in msg
    assert "1,024-byte growth budget" in msg
    assert "max_rundir_bytes" in msg
    # F-23: the report names the file that actually grew, and offers causes
    # rather than asserting the one that used to be hardcoded.
    assert "runaway.vcd" in msg
    assert "Plausible causes" in msg


def test_guard_ignores_pre_existing_input_bytes(tmp_path: Path, monkeypatch):
    """F-23: staged inputs already in a shared run_cwd never count as output.

    The regression this closes: one traced run left a huge VCD in the shared
    run dir and every later sim died in seconds blaming a testbench tracer,
    because the guard measured the directory's total size — inputs included.
    """
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())

    run = tmp_path / "run"
    run.mkdir()
    (run / "vectors.bin").write_bytes(b"0" * 8192)  # staged INPUT, 8x the budget
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(run, 1024, proc, interval=0.01)
    guard.start()
    time.sleep(0.1)
    guard.stop()

    assert guard.baseline >= 8192
    assert guard.tripped is False
    assert proc.killed is False


def test_snapshot_dir_baseline_measures_before_the_spawn(tmp_path: Path):
    """The baseline is a caller-owned, pre-spawn measurement."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "vectors.bin").write_bytes(b"0" * 4096)
    used, at = rg.snapshot_dir_baseline(run, 1024)
    assert used == 4096
    assert at > 0
    # Disabled guard: not even the walk (it costs seconds on a multi-GB tree).
    assert rg.snapshot_dir_baseline(run, 0) == (0, 0.0)


def test_guard_charges_bytes_written_before_start(tmp_path: Path, monkeypatch):
    """The baseline must predate the spawn, not the watchdog thread.

    ``guard.start()`` runs AFTER the simulator's Popen, and ``dir_size_bytes``
    is a recursive walk that takes seconds on the multi-GB trees this guard
    exists for. Taking the baseline inside ``start()`` therefore absorbed
    everything the sim dumped during that walk — free bytes that silently
    raised the effective budget.
    """
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())

    run = tmp_path / "run"
    run.mkdir()
    baseline = rg.snapshot_dir_baseline(run, 1024)  # pre-spawn: the dir is empty
    # "The sim dumped this while start()'s walk was still running."
    (run / "early.vcd").write_bytes(b"0" * 4096)

    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(run, 1024, proc, interval=0.01, baseline=baseline)
    guard.start()
    _await_trip(guard)

    assert guard.baseline == 0  # NOT 4096
    assert guard.tripped is True
    assert guard.grown >= 4096


def test_guard_baseline_falls_to_the_low_water_mark(tmp_path: Path, monkeypatch):
    """Deleting files must not buy a runaway extra headroom.

    A testbench that ``rm -rf``s 3 GB of staged vectors at startup and then
    runs away tracing would get ``budget + 3 GB`` of real disk if growth were
    measured from the size at spawn.
    """
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())

    run = tmp_path / "run"
    run.mkdir()
    staged = run / "vectors.bin"
    staged.write_bytes(b"0" * 8192)  # 8x the budget, pre-existing
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(
        run, 1024, proc, interval=0.01, baseline=rg.snapshot_dir_baseline(run, 1024)
    )
    guard.start()
    assert guard.baseline == 8192

    staged.unlink()  # the TB clears its inputs...
    deadline = time.monotonic() + 2.0
    while guard.baseline != 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert guard.baseline == 0  # low-water mark, not the size at spawn

    (run / "runaway.vcd").write_bytes(b"0" * 2048)  # ...then runs away
    _await_trip(guard)

    assert guard.tripped is True
    assert proc.killed is True


def test_guard_kills_before_walking_for_evidence(tmp_path: Path, monkeypatch):
    """On a runaway, kill first, then work out what grew.

    ``largest_files_written_since`` is a SECOND full recursive walk of a tree
    that is, by definition, huge and still growing — running it first is more
    seconds of runaway writing on exactly the pathological run. A SIGKILLed sim
    cannot truncate anything, so the evidence survives the kill.
    """
    import booley.platform_paths as pp

    order: list[str] = []

    def _kill(p):
        order.append("kill")
        p.kill()

    def _walk(*_args, **_kwargs):
        order.append("walk")
        return [("runaway.vcd", 2048)]

    monkeypatch.setattr(pp, "kill_process_tree", _kill)
    monkeypatch.setattr(rg, "largest_files_written_since", _walk)

    run = tmp_path / "run"
    run.mkdir()
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(run, 1024, proc, interval=0.01)
    guard.start()
    (run / "runaway.vcd").write_bytes(b"0" * 2048)
    _await_trip(guard)

    assert order == ["kill", "walk"]
    # ...and the report still names the culprit (message waits for the walk).
    assert "runaway.vcd" in guard.message


def test_guard_does_not_trip_under_budget(tmp_path: Path, monkeypatch):
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())

    run = tmp_path / "run"
    run.mkdir()
    (run / "small.txt").write_bytes(b"0" * 100)
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(run, 1_000_000, proc, interval=0.01)
    guard.start()
    time.sleep(0.05)
    guard.stop()
    assert guard.tripped is False
    assert proc.killed is False


# ---------------------------------------------------------------------------
# SimTimeStallGuard — frozen simulator clock (ravenoc F-25)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # cocotb 1.x and 2.x share this prefix: "{time_ns:6.2f}ns" in 11 columns.
        ("     0.00ns INFO     cocotb.regression   running test_reset", 0.0),
        ("    10.50ns DEBUG    cocotb   edge seen", 10.5),
        ("123456.00ns INFO     cocotb   late", 123456.0),
    ],
)
def test_parse_sim_time_ns_reads_the_cocotb_prefix(line: str, expected: float):
    assert rg.parse_sim_time_ns(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "  -.--ns INFO     cocotb   time not resolvable yet",  # cocotb's own unknown
        "Vtop: starting simulation",
        "make: *** [Makefile:12: Vtop] Error 2",
        "0.00 ns INFO cocotb   space before unit",
    ],
)
def test_parse_sim_time_ns_ignores_non_cocotb_lines(line: str):
    assert rg.parse_sim_time_ns(line) is None


class _FakeClock:
    """Monotonic clock the test drives by hand (no sleeping in the suite)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _stall_guard(proc, clock, grace_s: float = 60.0) -> rg.SimTimeStallGuard:
    return rg.SimTimeStallGuard(proc, grace_s, interval=0.01, clock=clock)


def test_stall_guard_trips_when_sim_time_never_leaves_zero(monkeypatch, tmp_path: Path):
    """The ravenoc failure: cocotb logs at 0.00ns forever, wall clock runs on."""
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())
    clock = _FakeClock()
    proc = _FakeProc()
    guard = _stall_guard(proc, clock)
    guard.start()
    guard.observe("     0.00ns INFO     cocotb.regression   running test_ravenoc")
    assert guard.stalled() is False  # armed, but the grace has not elapsed

    clock.now = 61.0
    deadline = time.monotonic() + 2.0
    while not guard.tripped and time.monotonic() < deadline:
        time.sleep(0.01)
    guard.stop()

    assert guard.tripped is True
    assert proc.killed is True
    msg = guard.message
    assert rg.SIM_TIME_STALL_MARKER in msg
    assert "run-loop version mismatch" in msg
    assert "sim_time_grace_s" in msg


def test_stall_guard_never_trips_once_sim_time_advanced(monkeypatch):
    """A slow-but-live sim (time > 0) is permanently exempt."""
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())
    clock = _FakeClock()
    proc = _FakeProc()
    guard = _stall_guard(proc, clock)
    guard.start()
    guard.observe("     0.00ns INFO     cocotb   starting")
    guard.observe("     1.00ns INFO     cocotb   first edge")
    clock.now = 100_000.0
    time.sleep(0.05)
    guard.stop()
    assert guard.tripped is False
    assert proc.killed is False


def test_stall_guard_stays_disarmed_without_any_sim_time_line(monkeypatch):
    """A run that never printed a cocotb line is not ours to judge."""
    import booley.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())
    clock = _FakeClock()
    proc = _FakeProc()
    guard = _stall_guard(proc, clock)
    guard.start()
    guard.observe("Verilating model...")
    clock.now = 100_000.0
    time.sleep(0.05)
    guard.stop()
    assert guard.tripped is False
    assert proc.killed is False


def test_stall_guard_disabled_by_zero_grace(monkeypatch):
    clock = _FakeClock()
    proc = _FakeProc()
    guard = rg.SimTimeStallGuard(proc, 0, interval=0.01, clock=clock)
    guard.start()
    guard.observe("     0.00ns INFO     cocotb   running")
    clock.now = 100_000.0
    time.sleep(0.05)
    guard.stop()
    assert guard.tripped is False
    assert guard.stalled() is False


def test_find_sim_time_stall_extracts_the_diagnosis():
    output = (
        "     0.00ns INFO     cocotb   running\n"
        f"ERROR: {rg.format_sim_time_stall(180)}\n"
        "some trailing noise\n"
    )
    found = rg.find_sim_time_stall(output)
    assert found.startswith(rg.SIM_TIME_STALL_MARKER)
    assert not found.startswith("ERROR: ")
    assert rg.find_sim_time_stall("nothing to see") == ""


# ---------------------------------------------------------------------------
# Orphan containment (fpu F-13)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_child_death_kwargs_kill_the_simulator_when_its_parent_dies():
    """A sim spawned with these kwargs dies with the run-half that spawned it.

    The fpu F-13 shape without the wrapper: kill the supervisor, and the
    simulator it started must not survive as a CPU-burning orphan.
    """
    import subprocess
    import textwrap

    # A "supervisor" that spawns a sleeper with the parent-death kwargs, prints
    # the sleeper's pid, then blocks. Killing the supervisor must reap both.
    supervisor_src = textwrap.dedent(
        """
        import subprocess, sys, time
        from booley.sim.run_guard import child_death_kwargs
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **child_death_kwargs(),
        )
        print(child.pid, flush=True)
        time.sleep(60)
        """
    )
    sup = subprocess.Popen(
        [sys.executable, "-c", supervisor_src],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid = int(sup.stdout.readline().strip())
        sup.kill()
        sup.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except OSError:
                return  # reaped, as required
            time.sleep(0.05)
        os.kill(child_pid, 9)  # don't leak it out of the test
        raise AssertionError(f"child {child_pid} survived its parent's death")
    finally:
        if sup.poll() is None:
            sup.kill()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_prctl_is_resolved_once_at_import_in_the_parent():
    """The dlopen must have happened in the parent, never after fork()."""
    assert rg._PRCTL is not None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_child_death_preexec_never_imports(monkeypatch):
    """``preexec_fn`` runs between fork() and exec() of a THREADED process.

    Both streaming run-halves start a ``Heartbeat`` before their Popen, so the
    forked child can inherit a malloc/io/import lock held by a thread that does
    not exist on its side of the fork. ``import ctypes`` there — a real import
    the first time, with an import lock and a ``_ctypes`` dlopen — can deadlock
    the child before exec; the parent then blocks forever draining a pipe that
    never opens, and the deadline timer reports "printed NO output at all".
    Everything must be resolved in the parent.
    """
    import builtins
    import signal

    calls: list[tuple] = []
    monkeypatch.setattr(rg, "_PRCTL", lambda *args: calls.append(args) or 0)
    preexec = rg.child_death_kwargs()["preexec_fn"]

    real_import = builtins.__import__

    def _no_imports(name, *_a, **_k):
        raise AssertionError(f"preexec_fn imported {name!r} between fork and exec")

    builtins.__import__ = _no_imports
    try:
        preexec()
    finally:
        builtins.__import__ = real_import

    assert calls == [(rg._PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)]


def test_supervise_child_is_idempotent():
    proc = _FakeProc()
    rg._supervised_children.clear()
    rg.supervise_child(proc)
    rg.supervise_child(proc)
    assert rg._supervised_children.count(proc) == 1
    rg._supervised_children.clear()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_parent_death_guard_kills_the_whole_tree(monkeypatch):
    """SIGTERM from the guard tears the registered children down, then exits."""
    import signal

    killed = []
    proc = _FakeProc()
    rg._supervised_children.clear()
    rg.supervise_child(proc)
    monkeypatch.setattr("booley.platform_paths.kill_process_tree", killed.append)
    try:
        rg.install_parent_death_guard()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
        assert killed == [proc]
    finally:
        rg._supervised_children.clear()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
