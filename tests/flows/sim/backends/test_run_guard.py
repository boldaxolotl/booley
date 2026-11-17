"""Unit tests for the shared per-run sim guards (booley.flows.sim.run_guard).

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
from tests.time_budget import hang_guard_s

from booley.flows.sim import run_guard as rg

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


@pytest.mark.parametrize(
    ("line", "call", "filename"),
    [
        (
            '%Warning: tb.sv:12: $readmemh: cannot open file "./firmware.hex"',
            "$readmemh",
            "./firmware.hex",
        ),
        ("Cannot open boot.mem for reading.", "$readmemh", "boot.mem"),
        ("$readmemh: Cannot open memory.hex", "$readmemh", "memory.hex"),
        ("ERROR: $readmemb: unable to open image.bin", "$readmemb", "image.bin"),
        ("$readmemh: can't open vectors.hex", "$readmemh", "vectors.hex"),
        ("$readmemb: could not open boot.bin", "$readmemb", "boot.bin"),
        ("$readmemh: unable to open", "$readmemh", ""),  # no filename still requests the abort
    ],
)
def test_parse_readmem_fatal_retains_filename_and_call(
    line: str, call: str, filename: str
) -> None:
    parsed = rg.parse_readmemh_fatal(line)
    assert parsed is not None
    assert (parsed.filename, parsed.call, parsed.diagnostic) == (filename, call, line)


def test_readmemh_classification_distinguishes_staging_gap_from_design(tmp_path: Path) -> None:
    parsed = rg.parse_readmemh_fatal("Cannot open ./firmware.hex for reading.")
    assert parsed is not None
    declared = rg.classify_readmemh_termination(parsed, tmp_path, ("firmware.hex",))
    undeclared = rg.classify_readmemh_termination(parsed, tmp_path, ())
    assert (declared.kind, declared.failure_kind) == ("fatal_init", "infrastructure")
    assert "not staged" in declared.detail
    assert (undeclared.kind, undeclared.failure_kind) == ("fatal_init", "missing_input")
    assert "Target fileset" in undeclared.detail


# ---------------------------------------------------------------------------
# dir_size_bytes (SETUP-25)
# ---------------------------------------------------------------------------


def test_dir_size_bytes_sums_nested_files_and_a_missing_dir_is_zero(tmp_path: Path):
    run = tmp_path / "run"  # clean subdir (conftest seeds .booley in tmp_path)
    run.mkdir()
    (run / "a.txt").write_bytes(b"x" * 100)
    sub = run / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)
    assert rg.dir_size_bytes(run) == 350
    assert rg.dir_size_bytes(tmp_path / "nope") == 0  # not created yet: 0, not an error


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


@pytest.fixture
def fake_kill(monkeypatch):
    """Guards "kill" the fake proc without real signals."""
    import booley.runtime.platform_paths as pp

    monkeypatch.setattr(pp, "kill_process_tree", lambda p: p.kill())


def test_guard_disabled_when_budget_non_positive(tmp_path: Path):
    """budget <= 0 disables the guard: start() is a no-op, never trips."""
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(tmp_path, 0, proc, interval=0.01)
    guard.start()
    time.sleep(0.05)
    guard.stop()
    assert guard.tripped is False
    assert proc.killed is False


def _await_trip(guard) -> None:
    """Poll until the guard trips (bounded so a bug can't hang the suite)."""
    deadline = time.monotonic() + hang_guard_s()
    while not guard.tripped and time.monotonic() < deadline:
        time.sleep(0.01)
    guard.stop()


def test_guard_kills_on_budget_breach(tmp_path: Path, fake_kill):
    """Growth past the budget kills the proc and exposes a factual message."""
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


@pytest.mark.parametrize(("staged", "budget"), [(8192, 1024), (100, 1_000_000)])
def test_guard_does_not_trip_on_input_bytes_or_under_budget(
    tmp_path: Path, fake_kill, staged: int, budget: int
):
    """F-23: staged inputs already in a shared run_cwd never count as output.

    The regression this closes: one traced run left a huge VCD in the shared
    run dir and every later sim died in seconds blaming a testbench tracer,
    because the guard measured the directory's total size, inputs included.
    """
    run = tmp_path / "run"
    run.mkdir()
    (run / "vectors.bin").write_bytes(b"0" * staged)  # the first case is 8x the budget
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(run, budget, proc, interval=0.01)
    guard.start()
    time.sleep(0.1)
    guard.stop()

    assert guard.baseline >= staged
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


def test_guard_charges_bytes_written_before_start(tmp_path: Path, fake_kill):
    """The baseline must predate the spawn, not the watchdog thread.

    ``guard.start()`` runs AFTER the simulator's Popen, and ``dir_size_bytes``
    is a recursive walk that takes seconds on the multi-GB trees this guard
    exists for. Taking the baseline inside ``start()`` therefore absorbed
    everything the sim dumped during that walk — free bytes that silently
    raised the effective budget.
    """
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


def test_guard_baseline_falls_to_the_low_water_mark(tmp_path: Path, fake_kill):
    """Deleting files must not buy a runaway extra headroom.

    A testbench that ``rm -rf``s 3 GB of staged vectors at startup and then
    runs away tracing would get ``budget + 3 GB`` of real disk if growth were
    measured from the size at spawn.
    """
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
    deadline = time.monotonic() + hang_guard_s()
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
    import booley.runtime.platform_paths as pp

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


def test_stall_guard_trips_when_sim_time_never_leaves_zero(fake_kill):
    """The ravenoc failure: cocotb logs at 0.00ns forever, wall clock runs on."""
    clock = _FakeClock()
    proc = _FakeProc()
    guard = _stall_guard(proc, clock)
    guard.start()
    guard.observe("     0.00ns INFO     cocotb.regression   running test_ravenoc")
    assert guard.stalled() is False  # armed, but the grace has not elapsed

    clock.now = 61.0
    deadline = time.monotonic() + hang_guard_s()
    while not guard.tripped and time.monotonic() < deadline:
        time.sleep(0.01)
    guard.stop()

    assert guard.tripped is True
    assert proc.killed is True
    msg = guard.message
    assert rg.SIM_TIME_STALL_MARKER in msg
    assert "run-loop version mismatch" in msg
    assert "sim_time_grace_s" in msg


@pytest.mark.parametrize(
    ("grace_s", "lines"),
    [
        # A slow-but-live sim (time > 0) is permanently exempt.
        (60.0, ["     0.00ns INFO     cocotb   starting", "     1.00ns INFO     cocotb   edge"]),
        # A run that never printed a cocotb line is not ours to judge.
        (60.0, ["Verilating model..."]),
        (0, ["     0.00ns INFO     cocotb   running"]),  # zero grace turns the guard off
    ],
)
def test_stall_guard_does_not_trip(fake_kill, grace_s: float, lines: list[str]):
    clock = _FakeClock()
    proc = _FakeProc()
    guard = _stall_guard(proc, clock, grace_s)
    guard.start()
    for line in lines:
        guard.observe(line)
    clock.now = 100_000.0
    time.sleep(0.05)
    guard.stop()
    assert guard.tripped is False
    assert guard.stalled() is False
    assert proc.killed is False


def test_find_sim_time_stall_extracts_the_diagnosis():
    output = (
        "     0.00ns INFO     cocotb   running\n"
        f"ERROR: {rg.format_sim_time_stall(180)}\n"
        "some trailing noise\n"
    )
    found = rg.find_sim_time_stall(output)
    assert found == rg.format_sim_time_stall(180)
    assert not found.startswith("ERROR: ")
    assert rg.find_sim_time_stall("nothing to see") == ""


def test_sim_time_stall_diagnosis_names_the_fixes_that_work():
    """#4235: a pin cannot fix the mismatch, so the message names what can."""
    msg = rg.format_sim_time_stall(180)
    assert msg.startswith(rg.SIM_TIME_STALL_MARKER)
    assert "re-pin" not in msg.lower()
    steps = [
        "run-loop version mismatch",
        "cocotb-config --version",
        "porting the testbench to the image's cocotb 2.x",
        "`cocotb.start_soon`",
        "Project image with a matching Verilator",
        "minutes in Python setup",
        "sim_time_grace_s",
        "0 turns this watchdog off",
    ]
    positions = [msg.index(step) for step in steps]
    assert positions == sorted(positions)


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
        from booley.flows.sim.run_guard import child_death_kwargs
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
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [
                    str(Path(__file__).resolve().parents[4] / "src"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
        },
    )
    try:
        child_pid = int(sup.stdout.readline().strip())
        sup.kill()
        sup.wait(timeout=hang_guard_s())
        deadline = time.monotonic() + hang_guard_s()
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
            sup.wait(timeout=hang_guard_s())
        if sup.stdout is not None:
            sup.stdout.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_prctl_is_resolved_once_at_import_in_the_parent():
    """The dlopen must have happened in the parent, never after fork()."""
    # Private seam: `preexec_fn` runs only between fork and exec, so the
    # import-time binding itself is the observable.
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
    # Private seam: `preexec_fn` runs only between fork and exec, so the binding
    # must be faked to run it in-process.
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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_parent_death_guard_keeps_a_run_whose_parent_is_pid_1(monkeypatch):
    """Booley as a container's first process is a live parent with PID 1."""
    # Private seam: arming PDEATHSIG for real would bind the pytest worker to its parent.
    armed = []
    monkeypatch.setattr(rg.os, "getppid", lambda: 1)
    monkeypatch.setattr(rg, "_set_pdeathsig", lambda sig: armed.append(sig) or True)
    monkeypatch.setattr("signal.signal", lambda _signum, _handler: None)

    rg.install_parent_death_guard()

    # Still armed: the run survives its start, not the death of its parent.
    assert armed == [15]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_parent_death_guard_exits_when_the_parent_changed_while_arming(monkeypatch):
    """A parent lost between entry and prctl is caught by the one manual check."""
    # Private seam: arming PDEATHSIG for real would bind the pytest worker to its parent.
    parents = iter([4242, 1])
    monkeypatch.setattr(rg.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(rg, "_set_pdeathsig", lambda _signal: True)
    monkeypatch.setattr("signal.signal", lambda _signum, _handler: None)
    monkeypatch.setattr("booley.runtime.platform_paths.kill_process_tree", lambda _proc: None)

    with pytest.raises(SystemExit) as exit_info:
        rg.install_parent_death_guard()

    assert exit_info.value.code == 143


def _run_half_state(pid_file: Path, stderr_file: Path) -> str:
    """Describe a run-half that outlived its launcher, for a failure message.

    Reports its signal state from ``/proc`` (where there is one) and what it wrote
    to stderr, so an unreproduced CI flake names its cause the next time it fails.
    """
    wanted = ("State", "Threads", "SigPnd", "SigBlk", "SigIgn")
    try:
        pid = pid_file.read_text()
        status = Path(f"/proc/{pid}/status").read_text().splitlines()
        state = ", ".join(line for line in status if line.split(":")[0] in wanted)
    except (OSError, ValueError) as exc:
        state = f"no /proc status ({exc.__class__.__name__})"
    try:
        stderr = stderr_file.read_text()[-2000:]
    except OSError:
        stderr = ""
    return f"run-half state: {state}; stderr: {stderr!r}"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_parent_death_guard_stops_a_real_run_half_when_its_launcher_dies(tmp_path):
    """Unmocked: kill the launcher, and the guarded process under it exits 143."""
    import subprocess
    import textwrap

    exit_file = tmp_path / "run_half_exit"
    pid_file = tmp_path / "run_half_pid"
    stderr_file = tmp_path / "run_half_stderr"
    # A "run-half" that installs the guard, reports that it is armed, then
    # blocks. The handler leaves through SystemExit, whose code it records.
    # The signal can arrive as soon as "armed" is out, so the report is inside
    # the try; the code is renamed into place so it is never read half written.
    run_half_src = textwrap.dedent(
        f"""
        import os, time
        from booley.flows.sim.run_guard import install_parent_death_guard
        with open({str(pid_file)!r}, "w") as pid_out:
            pid_out.write(str(os.getpid()))
        install_parent_death_guard()
        try:
            print("armed", flush=True)
            time.sleep(60)
        except SystemExit as exc:
            with open({str(exit_file) + ".part"!r}, "w") as part:
                part.write(str(exc.code))
            os.replace({str(exit_file) + ".part"!r}, {str(exit_file)!r})
            raise
        """
    )
    # A "launcher" that starts the run-half on its own stdout, then blocks.
    launcher_src = textwrap.dedent(
        """
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", sys.argv[1]])
        time.sleep(60)
        """
    )
    stderr_out = stderr_file.open("w")
    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_src, run_half_src],
        stdout=subprocess.PIPE,
        stderr=stderr_out,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [
                    str(Path(__file__).resolve().parents[4] / "src"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
        },
    )
    try:
        assert launcher.stdout.readline().strip() == "armed"
        launcher.kill()
        launcher.wait(timeout=hang_guard_s())
        deadline = time.monotonic() + hang_guard_s()
        while time.monotonic() < deadline and not exit_file.exists():
            time.sleep(0.05)
        assert exit_file.exists(), (
            "the guarded process did not leave through the handler; "
            + _run_half_state(pid_file, stderr_file)
        )
        assert exit_file.read_text() == "143"
    finally:
        stderr_out.close()
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=hang_guard_s())
        if launcher.stdout is not None:
            launcher.stdout.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
def test_parent_death_guard_kills_the_whole_tree(monkeypatch):
    """SIGTERM from the guard tears the registered children down, then exits."""
    import signal

    killed = []
    proc = _FakeProc()
    # Private seam: the module-global registry has no public reset; clearing keeps
    # other tests' handlers out.
    rg._supervised_children.clear()
    rg.supervise_child(proc)
    rg.supervise_child(proc)  # idempotent: the tree is killed once
    monkeypatch.setattr(rg.os, "getppid", lambda: 2)
    monkeypatch.setattr("booley.runtime.platform_paths.kill_process_tree", killed.append)
    try:
        rg.install_parent_death_guard()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
        assert killed == [proc]
    finally:
        rg._supervised_children.clear()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


@pytest.mark.parametrize(
    ("budget", "used", "tripped"), [(8, 7, False), (8, 8, False), (8, 9, True), (0, 99, False)]
)
def test_final_budget_threshold_and_idempotence(tmp_path, monkeypatch, budget, used, tripped):
    proc = _FakeProc()
    guard = rg.DiskBudgetGuard(tmp_path, budget, proc, baseline=(100, time.time()))
    guard.start()
    proc.wait()
    walks = []
    monkeypatch.setattr(rg, "dir_size_bytes", lambda path: walks.append(path) or 100 + used)
    monkeypatch.setattr(rg, "largest_files_written_since", lambda *args: [])
    guard.finish()
    guard.finish()
    guard.stop()
    assert guard.tripped is tripped
    assert len(walks) == (1 if budget else 0)
    assert not proc.killed
    if budget:
        assert guard.grown == used
    if tripped:
        assert guard.message.startswith("simulation aborted:")


def test_final_budget_discards_delayed_poll(tmp_path, monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()
    proc = _FakeProc()
    caller = threading.current_thread()
    guard = rg.DiskBudgetGuard(tmp_path, 8, proc, interval=0.001, baseline=(0, time.time()))
    calls = []

    def measure(path):
        calls.append(threading.current_thread())
        if threading.current_thread() is not caller:  # the poll thread
            entered.set()
            assert release.wait(hang_guard_s())
            return 999
        return 9

    monkeypatch.setattr(rg, "dir_size_bytes", measure)
    monkeypatch.setattr(rg, "largest_files_written_since", lambda *args: [])
    guard.start()
    try:
        assert entered.wait(hang_guard_s())
        proc.wait()
        guard.finish()
        release.set()
        guard.stop()  # joins the released poll thread
        assert guard.used == 9
        assert guard.grown == 9
        assert guard.tripped
        assert not proc.killed
        guard.finish()
        assert len(calls) == 2
    finally:
        release.set()
        guard.stop()


@pytest.mark.parametrize("evidence", [False, True])
def test_final_budget_exception_is_not_retried(tmp_path, monkeypatch, evidence):
    guard = rg.DiskBudgetGuard(tmp_path, 8, _FakeProc(), baseline=(0, time.time()))
    guard.start()
    guard.proc.wait()
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError("walk failed")

    monkeypatch.setattr(rg, "dir_size_bytes", (lambda path: 9) if evidence else fail)
    monkeypatch.setattr(rg, "largest_files_written_since", fail)
    with pytest.raises(RuntimeError, match="walk failed"):
        guard.finish()
    guard.stop()
    guard.finish()
    assert calls == [1]
    if evidence:
        # Private seam: the evidence event is the only observable of a walk that raised.
        assert guard._evidence.is_set()


def test_final_budget_preserves_live_trip_evidence(tmp_path, monkeypatch):
    import threading

    import booley.runtime.platform_paths as platform

    entered = threading.Event()
    release = threading.Event()
    guard = rg.DiskBudgetGuard(tmp_path, 8, _FakeProc(), interval=0.001, baseline=(0, 0))
    monkeypatch.setattr(rg, "dir_size_bytes", lambda path: 9)
    monkeypatch.setattr(platform, "kill_process_tree", lambda proc: proc.kill())

    def evidence(*args):
        entered.set()
        assert release.wait(hang_guard_s())
        return [("output", 9)]

    monkeypatch.setattr(rg, "largest_files_written_since", evidence)
    guard.start()
    try:
        assert entered.wait(hang_guard_s())
        guard.finish()
        assert guard.tripped
        assert guard.grown == 9
        release.set()
        guard.stop()
        assert guard.message.startswith("simulation killed:")
        assert guard.biggest == [("output", 9)]
    finally:
        release.set()
        guard.stop()


def test_final_budget_uses_observed_low_water(tmp_path, monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(rg, "dir_size_bytes", lambda path: 90)
    guard = rg.DiskBudgetGuard(tmp_path, 8, proc, interval=0.001, baseline=(100, 0))
    guard.start()
    deadline = time.monotonic() + hang_guard_s()
    while guard.baseline != 90 and time.monotonic() < deadline:
        time.sleep(0.001)
    guard.stop()
    proc.wait()
    monkeypatch.setattr(rg, "dir_size_bytes", lambda path: 99)
    monkeypatch.setattr(rg, "largest_files_written_since", lambda *args: [])
    guard.finish()
    assert guard.baseline == 90
    assert guard.grown == 9
    assert guard.tripped
