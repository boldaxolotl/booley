#!/usr/bin/env python3
"""Per-run safety guards shared by the streaming sim run-halves.

Two runaway failure modes an unattended sim can hit that the wall-clock timeout
does **not** catch — the sim is nominally "making progress", it is just eating
disk or spinning on uninitialised memory:

  * **Disk runaway (SETUP-25).** A testbench with a default-on tracer /
    ``$dumpfile`` / ``$fwrite`` sink fills the run directory (an Ibex
    ``ibex_tracer`` wrote 272MB per run; a runaway once left 27GB and filled the
    disk, killing an in-flight synthesis). :class:`DiskBudgetGuard` polls the run
    dir's size on a watchdog thread and kills the run once it crosses a byte
    budget — so even a *silent* runaway (bytes growing with the stdout loop
    blocked) is caught.
  * **Missing ``$readmemh`` init file (SETUP-23).** A ``$readmemh`` whose hex
    file is absent warns once, then leaves RAM uninitialised and the sim spins
    forever with no further output (observed: 6h, 0 output). The warning line is
    matched in the streamed output (:func:`readmemh_fatal_line`) and the run is
    killed fast instead of burning the whole wall-clock budget on a dead sim.
  * **Frozen simulator clock (ravenoc F-25).** A cocotb generation whose
    compiled run loop does not match the simulator's (e.g. a pinned cocotb
    1.5.1 VPI under Verilator 5.x) builds, loads and logs — and then no timed
    callback ever fires, so *simulation* time sits at 0.00 ns for the whole
    600 s budget while wall-clock progress looks healthy.
    :class:`SimTimeStallGuard` watches the simulator's own clock in the streamed
    log and aborts early with that diagnosis.

The run-halves (:mod:`booley.sim.verilator_run` / :mod:`booley.sim.iverilog_run`
/ :mod:`booley.sim.cocotb_run`) share this module so the guards live in one
place next to the timeout enforcement they complement.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A missing $readmemh/$readmemb memory-init file is fatal (SETUP-23): the sim
# would otherwise spin forever on uninitialised RAM. Match the one-line warning
# both simulators emit at time 0 — scoped to the memory-load context so a benign
# "cannot open" elsewhere in the log does not trip it:
#   * Verilator: ``%Warning: ...: $readmemh: cannot open file "firmware.hex"``
#   * Icarus:    ``... $readmemh: Unable to open <file> for reading.`` /
#                ``... Cannot open <file> for reading.``
_READMEMH_FATAL_RE = re.compile(
    r"\$readmem[hb]\b[^\n]*?(?:cannot|can't|unable to|could not|failed to)\s+open"
    r"|(?:cannot|can't|unable to|could not|failed to)\s+open\b[^\n]*?\bfor reading\b"
    r"|(?:cannot|can't|unable to|could not|failed to)\s+open\s+(?:memory|hex|mem)\s+file",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Orphan containment (fpu F-13)
# ---------------------------------------------------------------------------

# Linux prctl(2) option number for PR_SET_PDEATHSIG. Not exposed by the stdlib,
# so it is spelled out here and applied through ctypes.
_PR_SET_PDEATHSIG = 1

#: Sim children registered by the streaming run-halves, so the parent-death
#: handler can tear the whole tree down. A list, not a single slot: a run-half
#: may hold a bwave streamer alongside the simulator.
_supervised_children: list[Any] = []


def supervise_child(proc: Any) -> None:
    """Register *proc* as a child the parent-death handler must reap.

    Idempotent; a finished process left registered is harmless, since
    :func:`booley.platform_paths.kill_process_tree` no-ops on a dead one.
    """
    if proc not in _supervised_children:
        _supervised_children.append(proc)


def _bind_prctl() -> Any:
    """Bind ``libc.prctl`` ONCE, here in the parent; None when unavailable.

    ``preexec_fn`` runs between ``fork()`` and ``exec()`` of a process that
    already has threads — every run-half starts a :class:`~booley.heartbeat.
    Heartbeat` before spawning its simulator. Only async-signal-safe work is
    legal there, and ``import ctypes`` is the opposite of that: on the first
    call it is a *real* import taking the import lock, dlopen-ing ``_ctypes``
    and allocating, any of which can deadlock the forked child against a lock
    some thread happened to hold at fork time. The child would then never reach
    ``exec``, the parent would block forever in ``for line in proc.stdout``, and
    the deadline timer would report the sim "printed NO output at all".

    So the import, the ``dlopen`` and the signature setup all happen at module
    import in the parent, and the child is left with a single call through an
    already-resolved function pointer. Linux-only (``prctl(PR_SET_PDEATHSIG)``);
    None everywhere else and on any failure, so callers degrade to "no
    containment" rather than crash.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        prctl = libc.prctl
        # Pin the signature in the parent too, so the child's call does not even
        # run ctypes' argument-type inference.
        prctl.restype = ctypes.c_int
        prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    except (
        ImportError,
        OSError,
        AttributeError,
        ValueError,
    ):  # pragma: no cover - libc-shape defensive
        return None
    return prctl


#: ``libc.prctl``, resolved at import time in the parent. See :func:`_bind_prctl`.
_PRCTL = _bind_prctl()


def _set_pdeathsig(sig: int) -> bool:
    """Ask the kernel to send *sig* to this process when its parent dies.

    Parent-side helper (:func:`install_parent_death_guard`); the post-fork path
    uses the closure built by :func:`child_death_kwargs` instead. Returns False
    off Linux and on any failure.
    """
    if _PRCTL is None:
        return False
    try:
        return _PRCTL(_PR_SET_PDEATHSIG, sig, 0, 0, 0) == 0
    except (OSError, ValueError):  # pragma: no cover - libc-shape defensive
        return False


def child_death_kwargs() -> dict:
    """Popen kwargs making a spawned simulator die with the run-half that spawned it.

    Backstop for the case the run-half itself is SIGKILLed (nothing can run a
    handler then): the simulator gets SIGKILL from the kernel instead of being
    reparented to init and left burning a core. ``preexec_fn`` runs in the
    forked child before ``exec``, which is the only place PDEATHSIG can be set
    for it — the setting is cleared by ``fork`` and preserved across ``exec``.
    Empty (no-op) off Linux.
    """
    if _PRCTL is None:
        return {}
    import signal

    # Everything the child needs is resolved HERE, in the parent: the function
    # pointer and both integer arguments. See :func:`_bind_prctl` for why the
    # post-fork body must not import, dlopen, or take a lock.
    prctl, opt, sig = _PRCTL, _PR_SET_PDEATHSIG, int(signal.SIGKILL)

    def _preexec() -> None:
        prctl(opt, sig, 0, 0, 0)

    return {"preexec_fn": _preexec}


def install_parent_death_guard() -> None:
    """Make this run-half process die — with its simulator — when its parent does.

    fpu F-13: ``simulate``'s wrapper timeout kills only the ``sh -c`` it spawned;
    the ``python -m booley.sim.verilator_run`` supervisor underneath it and the
    ``V<top>`` it was running were reparented to init and stayed at 99.9 % CPU
    for 38 minutes, invisible to every later run's cleanup. Two kernel-level
    hooks close that: the supervisor asks for SIGTERM on parent death and, on
    receiving it, tears its registered children's process trees down before
    exiting (via ``SystemExit``, so the run-half's ``finally`` blocks still stop
    the watchdogs and clean up the trace FIFO).

    Call from a run-half's ``main()`` only — never from an in-process API — so
    importing the module can never install a signal handler in somebody else's
    process. Off the main thread it is a no-op; off Linux only the SIGTERM
    handler is installed (a deliberate improvement in its own right), since
    PDEATHSIG has no portable equivalent.
    """
    import signal

    def _on_parent_death(signum: int, _frame: Any) -> None:
        from booley.platform_paths import kill_process_tree

        for proc in list(_supervised_children):
            with contextlib.suppress(Exception):
                kill_process_tree(proc)
        logger.warning("run-half parent died (signal %d); killed the sim tree", signum)
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _on_parent_death)
    except ValueError:  # pragma: no cover - only reachable off the main thread
        return
    if not _set_pdeathsig(signal.SIGTERM):
        return
    # The parent may already be gone (we lost the race between fork and prctl);
    # the kernel would then never deliver anything, so check once by hand.
    if os.getppid() == 1:
        _on_parent_death(signal.SIGTERM, None)


def readmemh_fatal_line(line: str) -> str | None:
    """Return *line* stripped when it is a fatal missing-``$readmemh``-file
    warning, else ``None``.

    SETUP-23: a ``$readmemh`` whose backing hex file is missing warns once and
    then the sim spins forever on uninitialised RAM with no more output. The
    warning is our only signal, so the streaming loop scans each line through
    this and kills the run on a match rather than letting it hang.
    """
    return line.strip() if _READMEMH_FATAL_RE.search(line) else None


def dir_size_bytes(path: Path) -> int:
    """Best-effort recursive on-disk byte size of *path* (symlinks not followed).

    Entries that vanish mid-walk (a sim rotating temp files) or are unreadable
    are skipped rather than raising — the guard only needs an approximate total,
    and a missing run dir (not created yet) reports 0.
    """
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += dir_size_bytes(Path(entry.path))
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


#: How many of the run's biggest new files the runaway report names. Enough to
#: point at the culprit without turning the kill message into a directory listing.
_RUNAWAY_TOP_FILES = 3


def snapshot_dir_baseline(rundir: Path, budget: int) -> tuple[int, float]:
    """``(bytes, wall-clock mark)`` to charge a run's growth against.

    Take this **before spawning the run**, never after. :func:`dir_size_bytes`
    is a recursive scandir walk that costs seconds on the multi-GB trees this
    guard exists for (fpu F-23's 3.3 GB of staged vectors), so a baseline taken
    after the simulator started absorbs everything it dumped during the walk —
    free bytes that silently raise the effective budget — and puts a full tree
    walk on the sim-start critical path.

    Returns ``(0, 0.0)`` when the guard is disabled, so a disabled guard never
    pays for the walk at all.
    """
    if budget <= 0:
        return 0, 0.0
    return dir_size_bytes(rundir), time.time()


def largest_files_written_since(
    rundir: Path, since: float, limit: int = _RUNAWAY_TOP_FILES
) -> list[tuple[str, int]]:
    """The biggest files under *rundir* modified at/after *since*, largest first.

    "What actually grew" — the fact a disk-runaway report needs and could only
    guess at before (fpu F-23, where the message blamed a testbench tracer while
    the real growth was one ``--trace`` VCD sitting next to 3.3 GB of *input*
    vectors it had nothing to do with). Best-effort: unreadable entries are
    skipped, since this only ever decorates a message.
    """
    found: list[tuple[str, int]] = []

    def _walk(directory: Path) -> None:
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            _walk(Path(entry.path))
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if st.st_mtime >= since:
                            found.append((os.path.relpath(entry.path, rundir), st.st_size))
                    except OSError:
                        continue
        except OSError:
            return

    _walk(rundir)
    found.sort(key=lambda item: item[1], reverse=True)
    return found[:limit]


def format_disk_runaway(
    rundir: Path,
    grown: int,
    budget: int,
    *,
    baseline: int = 0,
    used: int = 0,
    biggest: list[tuple[str, int]] | None = None,
) -> str:
    """The disk-budget kill report: measured facts first, causes as a list.

    The budget is on **growth during this run**, not on the directory's total
    size — a shared ``run_cwd`` holding staged input vectors would otherwise
    spend the whole budget before the simulator started (fpu F-23: 3.3 GB of
    input vectors counted against a 5 GB *output* budget, killing every later
    run in ~5 s). And the message names what it measured rather than asserting
    a single cause, because the previous wording ("a default-on testbench
    tracer") was simply the wrong diagnosis for that run.
    """
    parts = [
        f"simulation killed: run directory {rundir} grew by {grown:,} bytes during "
        f"this run ({baseline:,} -> {used:,} bytes on disk), over the "
        f"{budget:,}-byte growth budget ([flows.sim].max_rundir_bytes)."
    ]
    if biggest:
        listed = ", ".join(f"{name} ({size:,} bytes)" for name, size in biggest)
        parts.append(f"Largest files written during the run: {listed}.")
    parts.append(
        "Plausible causes: a --trace / $dumpfile waveform with no bound, a "
        "default-on testbench tracer, or a $fwrite log sink (SETUP-25). Scope or "
        "gate the dump, point [flows.sim].run_cwd at a dedicated directory, "
        "or raise the budget."
    )
    return " ".join(parts)


#: How long :attr:`DiskBudgetGuard.message` waits for the post-kill "what grew"
#: walk. Roughly one poll interval's worth of budget — enough for the walk on a
#: tree the poller has just walked (warm cache), never enough to wedge a run.
_EVIDENCE_WALK_TIMEOUT_S = 10.0


class DiskBudgetGuard:
    """Watchdog thread that kills *proc* when *rundir* GROWS past *budget* bytes.

    Polls :func:`dir_size_bytes` every *interval* seconds on a daemon thread, so
    a silent disk runaway (a ``$dumpfile`` growing while the stdout-draining loop
    is blocked) is caught alongside the wall-clock timeout. A *budget* ``<= 0``
    disables the guard entirely: :meth:`start` is a no-op and :attr:`tripped`
    stays ``False``.

    The budget measures **growth from a baseline taken before the run was
    spawned** (:func:`snapshot_dir_baseline`, passed in as *baseline*), not the
    directory's absolute size. ``run_cwd`` is routinely shared with the
    testbench's own inputs (staged vectors, firmware images), and charging those
    pre-existing bytes to the run's output budget makes the guard fire on runs
    that wrote nothing at all — which is exactly how one traced run poisoned
    every subsequent sim in the fpu port (F-23). The baseline only ever moves
    *down*, to the low-water mark seen while polling, so deleting files cannot
    buy a runaway extra headroom.
    """

    def __init__(
        self,
        rundir: Path,
        budget: int,
        proc: Any,
        *,
        interval: float = 5.0,
        baseline: tuple[int, float] | None = None,
    ) -> None:
        self.rundir = Path(rundir)
        self.budget = int(budget)
        self.proc = proc
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.tripped = False
        self.baseline = 0
        self.used = 0
        self.grown = 0
        self.biggest: list[tuple[str, int]] = []
        self._started_at = 0.0
        self._baseline = baseline
        # Set once the post-kill "what grew" walk has landed, so :attr:`message`
        # does not render the report while the evidence is still being gathered.
        self._evidence = threading.Event()

    def start(self) -> None:
        """Start polling (no-op when the guard is disabled — budget <= 0).

        Uses the *baseline* the caller snapshotted **before** spawning the run.
        Without one it is taken here — late, with the run already writing into
        the tree being walked — so in-process callers should prefer
        :func:`snapshot_dir_baseline` at the spawn site.
        """
        if self.budget <= 0:
            return
        if self._baseline is None:
            self._baseline = snapshot_dir_baseline(self.rundir, self.budget)
        self.baseline, self._started_at = self._baseline
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self) -> None:
        from booley.platform_paths import kill_process_tree

        # Event.wait doubles as the sleep and the stop-signal: it returns True
        # the moment stop() is called, so a finished run tears the thread down
        # promptly instead of sleeping out the last interval.
        while not self._stop.wait(self.interval):
            if self.proc.poll() is not None:
                return  # run already exited — nothing to guard
            used = dir_size_bytes(self.rundir)
            # Growth is measured from the SMALLEST the tree has been, not from
            # its size at spawn: a testbench that deletes 3 GB of staged vectors
            # at startup would otherwise be handed budget + 3 GB of real disk
            # before the guard bit. (Overwriting a file in place is not a
            # defeat — same bytes, same disk — so only real shrinkage moves it.)
            self.baseline = min(self.baseline, used)
            if used - self.baseline > self.budget:
                self.used = used
                self.grown = used - self.baseline
                self.tripped = True
                # Kill FIRST, then collect evidence: largest_files_written_since
                # is a second full walk of a tree that is by definition huge and
                # still growing, and on exactly this pathological run that walk
                # is more seconds of runaway writing. A SIGKILLed sim cannot
                # truncate anything, so the evidence survives the kill.
                kill_process_tree(self.proc)
                self.biggest = largest_files_written_since(self.rundir, self._started_at)
                self._evidence.set()
                return

    def stop(self) -> None:
        """Stop the watchdog and join its thread (idempotent)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)

    @property
    def message(self) -> str:
        """The disk-runaway report (only meaningful once :attr:`tripped`)."""
        if self.tripped:
            # The "what grew" walk runs after the kill (see _poll); give it a
            # bounded moment to land rather than printing a report with no file
            # list. Bounded, because a report is never worth hanging a run for.
            self._evidence.wait(timeout=_EVIDENCE_WALK_TIMEOUT_S)
        return format_disk_runaway(
            self.rundir,
            self.grown,
            self.budget,
            baseline=self.baseline,
            used=self.used,
            biggest=self.biggest,
        )


# ---------------------------------------------------------------------------
# Frozen simulator clock (ravenoc F-25)
# ---------------------------------------------------------------------------

# Every cocotb log line is prefixed with the simulator's own time, formatted
# ``f"{time_ns:6.2f}ns"`` right-justified to 11 columns — identical in cocotb
# 1.x (``cocotb/log.py``) and 2.x (``cocotb/logging.py``), e.g.
#     "     0.00ns INFO     cocotb.regression   running test_reset (1/8)"
# That prefix is the only sim-time signal available while a run streams, so it
# is what the stall guard watches. A time cocotb cannot resolve prints as
# ``"  -.--ns"`` and simply does not match (no false zero).
_SIM_TIME_RE = re.compile(r"^\s*(\d+\.\d{2})ns\s")

# Wall-clock seconds a run may sit at exactly 0.00 ns of simulation time before
# the guard calls it a frozen run loop. Generous on purpose: a heavy design's
# Python-side testbench setup legitimately runs for minutes before the first
# clock edge, and killing that would be far worse than the stall it prevents.
DEFAULT_SIM_TIME_GRACE_S = 180.0

# Stable prefix of :func:`format_sim_time_stall`, so consumers (the cocotb
# run-half's verdict layer, tests) can recognise the abort reason in captured
# output without duplicating the whole sentence.
SIM_TIME_STALL_MARKER = "simulation killed: simulator time never advanced"


def parse_sim_time_ns(line: str) -> float | None:
    """Simulation time in ns from a cocotb log line's prefix, else ``None``."""
    m = _SIM_TIME_RE.match(line)
    return float(m.group(1)) if m else None


def format_sim_time_stall(grace_s: float) -> str:
    """The diagnosis printed when the simulator's clock never leaves 0.00 ns."""
    return (
        f"{SIM_TIME_STALL_MARKER} past 0.00 ns in {grace_s:.0f}s of wall clock. "
        "cocotb is loaded and logging, but no timed callback ever fired — the "
        "usual cause is a cocotb/simulator run-loop version mismatch (a pinned "
        "cocotb 1.5.x VPI under Verilator 5.x, say). Check the cocotb <-> "
        "simulator pairing (`cocotb-config --version` vs the simulator's), then "
        "re-pin one of them. Tune or disable this watchdog with "
        "[flows.sim].sim_time_grace_s (0 = off)."
    )


def find_sim_time_stall(output: str) -> str:
    """The stall diagnosis line inside *output*, or ``""`` when absent."""
    for line in output.splitlines():
        if SIM_TIME_STALL_MARKER in line:
            return line.strip().removeprefix("ERROR: ")
    return ""


class SimTimeStallGuard:
    """Watchdog thread that kills *proc* when its simulation clock is frozen.

    Armed only once the run has printed at least one sim-time-bearing line
    (proof the simulator started and the log dialect is understood), and trips
    only while the highest time seen is *exactly* 0.00 ns: a run that advanced
    even one timestep is never touched again, so a slow-but-live simulation
    cannot be killed by this guard. A *grace_s* ``<= 0`` disables it entirely.

    Polling on a thread rather than driving off the stream loop is the point:
    the frozen-run-loop failure mode stops producing output altogether, so a
    line-driven check would never run again.
    """

    def __init__(
        self,
        proc: Any,
        grace_s: float,
        *,
        interval: float = 5.0,
        clock: Any = time.monotonic,
    ) -> None:
        self.proc = proc
        self.grace_s = float(grace_s)
        self.interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._armed_at: float | None = None
        self._max_sim_time_ns = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.tripped = False

    def start(self) -> None:
        """Start polling (no-op when the guard is disabled — grace_s <= 0)."""
        if self.grace_s <= 0:
            return
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def observe(self, line: str) -> None:
        """Feed one streamed output line; records the simulator's clock."""
        if self.grace_s <= 0:
            return
        sim_time = parse_sim_time_ns(line)
        if sim_time is None:
            return
        with self._lock:
            if self._armed_at is None:
                self._armed_at = self._clock()
            self._max_sim_time_ns = max(self._max_sim_time_ns, sim_time)

    def stalled(self) -> bool:
        """True once the clock has sat at exactly 0.00 ns beyond the grace."""
        with self._lock:
            if self._armed_at is None or self._max_sim_time_ns > 0.0:
                return False
            return self._clock() - self._armed_at >= self.grace_s

    def _poll(self) -> None:
        from booley.platform_paths import kill_process_tree

        while not self._stop.wait(self.interval):
            if self.proc.poll() is not None:
                return  # run already exited — nothing to guard
            if self.stalled():
                self.tripped = True
                kill_process_tree(self.proc)
                return

    def stop(self) -> None:
        """Stop the watchdog and join its thread (idempotent)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)

    @property
    def message(self) -> str:
        """The stall diagnosis (only meaningful once :attr:`tripped`)."""
        return format_sim_time_stall(self.grace_s)
