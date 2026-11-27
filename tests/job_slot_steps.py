"""Filesystem-step scheduling for Job slot stores that simulate concurrent processes.

Each simulated process (an ``Actor``) owns a ``SlotStore`` whose ``root`` is a
``SteppedPath``. Every filesystem call the store makes through that path is a
scheduling point: the calling thread parks until the ``StepScheduler`` grants it
that one step, or fails it with an injected Windows sharing violation. The lease renewal
wait of an actor that renews is a checkpoint too. Exactly
one thread runs at a time, so a schedule is a pure function of the choices that
produced it. The store's own code runs unchanged.

The seam is the store's ``root``: a direct ``os.*`` call on a path in
``job_slots.py`` would bypass it without failing anything, so a new filesystem
call there should go through ``Path`` methods.

It fakes process liveness, process identity and the clock for each store.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, ClassVar

from booley.core import file_lock
from booley.core.sharing_retry import link_file, unlink_file, write_file_text
from booley.runtime import job_slots
from booley.runtime.job_slots import SlotStore
from booley.runtime.pid import DEAD, REUSED, RUNNING, ProcessIdentity, ProcessObservation
from tests.time_budget import hang_guard_s

CLOCK_START = 1_000_000.0
# Inner deadline: the scaled hang guard, which the per-test limit stays above
# (see ``.github/ci/README.md#test-time-budgets``).
STEP_DEADLINE_SECONDS = hang_guard_s()
_POLL_SECONDS = 0.002
MAX_FAULTS_PER_THREAD = 3
DRAIN_STEP_CAP = 50_000
# More than one full gate attempt loop (20 attempts of about 6 steps).
FINISH_STEP_CAP = 300
# Above the kernel PID limit, so no fake PID can name a host process.
FIRST_ACTOR_PID = 4_194_401

# Checkpoints where a thread waits for another thread to release a lock. The holder
# must run before the waiter is granted again, so every loop that grants one worker
# repeatedly stops at one of these.
LOCK_WAIT_KINDS = frozenset({"wait", "lock_wait"})
# The production poll between attempts to take a record lock.
LOCK_POLL_SECONDS = 0.1
# The scheduler that owns the calling thread, for code that cannot be handed one.
_OWNER = threading.local()

READ_KINDS = frozenset(
    {
        *LOCK_WAIT_KINDS,
        "exists",
        "is_dir",
        "is_symlink",
        "stat",
        "glob",
        "iterdir",
        "read_bytes",
        "open_read",
        "sleep",
    }
)
# Steps Windows can fail with a sharing violation. Metadata and listing steps stay
# scheduling points only: CPython's ``os.stat`` falls back to ``FindFirstFileW``.
_FAULT_KINDS = frozenset(
    {
        "open_read",
        "open_write",
        "write_text",
        "read_bytes",
        "rename",
        "replace",
        "hardlink_to",
        "unlink",
        "mkdir",
        "rmdir",
    }
)


PROMOTION_GATE = ".promotion.lock"


def lease_gate(token: job_slots.SlotToken) -> Path:
    """The file a process holds while it renews, releases or promotes *token*."""
    return token.path.parent / f".{token.lease_id}.lease.lock"


@contextlib.contextmanager
def held_gate(gate: Path, pid: int, identity: ProcessIdentity | None) -> Iterator[None]:
    """Another process's gate on disk, as that process publishes it, until the block ends."""
    owner = {"pid": pid, "owner_identity": identity.to_payload() if identity else None}
    gate.parent.mkdir(parents=True, exist_ok=True)
    tmp = gate.with_name(f".{gate.name}.{uuid.uuid4().hex}.tmp")
    write_file_text(tmp, json.dumps(owner) + "\n")
    try:
        link_file(gate, tmp)  # exclusive: the gate must be free
    finally:
        unlink_file(tmp, missing_ok=True)
    try:
        yield
    finally:
        unlink_file(gate, missing_ok=True)


class FakeProcessWorld:
    """Clock and durable identities for simulated process owners."""

    def __init__(self) -> None:
        self.clock = CLOCK_START
        self.alive: set[int] = set()
        self.generations: dict[int, int] = {}

    def start(self, pid: int) -> None:
        if pid not in self.alive:
            self.generations[pid] = self.generations.get(pid, 0) + 1
            self.alive.add(pid)

    def stop(self, pid: int) -> None:
        self.alive.discard(pid)

    def is_pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def read_cmdline(self, pid: int) -> list[str] | None:
        if pid not in self.alive:
            return None
        return ["fake-owner", str(pid), str(self.generations[pid])]

    def now(self) -> float:
        return self.clock

    def advance(self, seconds: float) -> None:
        self.clock += seconds

    def capture_identity(self, pid: int) -> ProcessIdentity | None:
        if pid not in self.alive:
            return None
        return ProcessIdentity(pid, "fake-pid-namespace", self.generations[pid])

    def observe_identity(self, identity: ProcessIdentity) -> ProcessObservation:
        if identity.pid not in self.alive:
            return ProcessObservation(DEAD)
        if self.generations[identity.pid] != identity.start_ticks:
            return ProcessObservation(REUSED)
        return ProcessObservation(RUNNING)


class ActorKilled(BaseException):
    """Raised at every checkpoint of a killed actor, like a process ended by SIGKILL.

    A ``BaseException`` so the store's ``except OSError`` clauses and
    ``contextlib.suppress(OSError)`` cannot catch it: the threads unwind with no
    further filesystem effect.
    """


def sharing_violation(path: Path) -> PermissionError:
    """The error Windows reports for a file another handle holds, accepted on every platform."""
    error = PermissionError(errno.EACCES, "injected sharing violation", str(path))
    error.winerror = 32  # type: ignore[attr-defined]
    return error


@dataclass(eq=False)
class Worker:
    """One thread the scheduler owns: an operation, or a thread the store started."""

    actor: Actor
    label: str
    state: str = "running"  # running | parked | idle | done
    pending: tuple[str, str] | None = None  # (kind, file name) of the step it waits to run
    verdict: str = ""  # run | fail | kill, set by the grant
    command: str | None = None  # what an idle worker was told to do next
    faults: int = 0
    steps: int = 0
    result: Any = None
    oserror: OSError | None = None
    error: BaseException | None = None
    reported: bool = False
    acquired: set[str] = field(default_factory=set)  # gates this thread took
    origin: Worker | None = None  # the operation this thread belongs to
    thread: threading.Thread | None = None

    @property
    def root(self) -> Worker:
        return self.origin or self


class StepScheduler:
    """Runs scheduled threads one granted step at a time and checks invariants after each."""

    def __init__(
        self, world: FakeProcessWorld, trace: Callable[[str], None] | None = None
    ) -> None:
        self.world = world
        self.workers: list[Worker] = []
        self.holds: dict[str, set[Actor]] = {}
        self.checks: list[Callable[[], None]] = []
        self.checks_enabled = True
        self.violation: AssertionError | None = None
        # Steps where one actor changed a file another had read since the file last changed:
        # the check-then-act windows. Hypothesis steers its search toward more of them.
        self.conflicts = 0
        self._readers: dict[str, set[Actor]] = {}
        # Gates whose holder died: how many actors have read each to judge it stale.
        self.stale_judges: dict[str, set[Actor]] = {}
        self.most_judges = 0
        self.replaced_removals = 0
        self._gate_generation: dict[str, int] = {}
        self._judged_at: dict[tuple[Actor, str], int] = {}
        self._trace = trace
        self._unsent: list[str] = []
        self._cv = threading.Condition()
        self._local = threading.local()
        self._by_thread: dict[threading.Thread, Worker] = {}
        self._stepping: Worker | None = None
        # A thread the store started ends by ActorKilled when its actor is killed: not an error.
        self._previous_hook = threading.excepthook
        # Threads named with one of these run unstepped: no checkpoint, never awaited.
        self.unstepped_prefixes: tuple[str, ...] = ()
        threading.excepthook = self._thread_hook

    def _thread_hook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not ActorKilled:
            self._previous_hook(args)

    # -------------------------------------------------------------- threads

    def start(self, actor: Actor, label: str, operation: Callable[[], Any]) -> Worker:
        """Run *operation* on a new thread until it parks at its first checkpoint or ends."""
        worker = Worker(actor, label)
        worker.thread = threading.Thread(
            target=self._run_worker,
            args=(worker, operation),
            name=f"{actor.name}/{label}",
            daemon=True,
        )
        before = set(threading.enumerate())
        with self._cv:
            self.workers.append(worker)
            self._stepping = worker
        actor.operation = worker
        worker.thread.start()
        self._wait_quiet(before)
        self._after_step()
        return worker

    def _run_worker(self, worker: Worker, operation: Callable[[], Any]) -> None:
        self._local.worker = worker
        _OWNER.scheduler = self
        with self._cv:
            self._by_thread[threading.current_thread()] = worker
        try:
            worker.result = operation()
        except ActorKilled:
            worker.result = None
        except OSError as exc:
            worker.oserror = exc
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            worker.error = exc
        finally:
            with self._cv:
                worker.state = "done"
                self._cv.notify_all()

    def _worker_for_call(self) -> Worker | None:
        worker: Worker | None = getattr(self._local, "worker", None)
        if worker is not None or self._stepping is None:
            return worker
        if threading.current_thread().name.startswith(self.unstepped_prefixes):
            return None
        # A thread the store started during a granted step belongs to the stepping actor.
        parent = self._stepping
        worker = Worker(parent.actor, f"{parent.label}/background", origin=parent.root)
        worker.thread = threading.current_thread()
        self._local.worker = worker
        _OWNER.scheduler = self
        with self._cv:
            self.workers.append(worker)
            self._by_thread[threading.current_thread()] = worker
        return worker

    def current_worker(self) -> Worker:
        """The worker of the calling thread, which has reached a checkpoint."""
        return self._local.worker

    def owns_caller(self) -> bool:
        """Whether the calling thread's calls are steps."""
        return self._worker_for_call() is not None

    # ----------------------------------------------------------- checkpoint

    def call(
        self, kind: str, path: Path, perform: Callable[[], Any], *, shown: str | None = None
    ) -> Any:
        """Run *perform* as one scheduled step; calls from other threads pass straight through.

        *shown* is the file name the step is about when that is not ``path.name``: a rename or
        replace changes its destination.
        """
        worker = self._worker_for_call()
        if worker is None or getattr(self._local, "depth", 0):
            return perform()
        self._local.depth = 1
        try:
            return self._step_worker(worker, kind, path, perform, shown or path.name)
        finally:
            self._local.depth = 0

    def sleep(self, seconds: float) -> None:
        """The stores' injected sleep: a checkpoint that advances the machine clock when granted."""

        self.call("sleep", Path(), lambda: self.world.advance(seconds))

    def renewal_wait(self, stop: threading.Event, seconds: float) -> bool:
        """The stores' injected renewal wait: a ``sleep`` checkpoint named ``renewal``."""
        self.call("sleep", Path("renewal"), lambda: self.world.advance(seconds))
        return stop.is_set()

    def _step_worker(
        self, worker: Worker, kind: str, path: Path, perform: Callable[[], Any], name: str
    ) -> Any:
        actor = worker.actor
        if actor.killed:
            raise ActorKilled
        with self._cv:
            worker.pending = (kind, name)
            worker.state = "parked"
            self._cv.notify_all()
            self._cv.wait_for(lambda: worker.verdict != "")
            verdict, worker.verdict, worker.pending = worker.verdict, "", None
        if verdict == "kill":
            raise ActorKilled
        outcome = "ok"
        try:
            if verdict == "fail":
                outcome = "fault"
                raise sharing_violation(path)
            result = perform()
        except OSError as exc:
            outcome = outcome if outcome == "fault" else type(exc).__name__
            raise
        else:
            self._after_success(worker, kind, path, name)
            return result
        finally:
            worker.steps += 1
            self._unsent.append(f"{actor.name}/{worker.label} {kind} {name}: {outcome}")

    def _note_access(self, worker: Worker, kind: str, name: str) -> None:
        if name.endswith(".tmp") or not name:
            return
        if kind in READ_KINDS:
            self._readers.setdefault(name, set()).add(worker.actor)
        else:
            others = self._readers.pop(name, set()) - {worker.actor}
            self.conflicts += bool(others)

    def _note_judge(self, worker: Worker, kind: str, name: str) -> None:
        """Count stale-gate judgements: how many actors read a dead holder's gate, and how often
        an actor removes a gate that was replaced since it judged it (a lost break race)."""
        if not _is_gate(name):
            return
        actor = worker.actor
        if name in self.stale_judges and kind == "read_bytes":
            self.stale_judges[name].add(actor)
            self.most_judges = max(self.most_judges, len(self.stale_judges[name]))
        if kind == "read_bytes":
            self._judged_at[actor, name] = self._gate_generation.get(name, 0)
        elif kind == "hardlink_to":
            self._gate_generation[name] = self._gate_generation.get(name, 0) + 1
            self._judged_at.pop((actor, name), None)
            self.stale_judges.pop(name, None)
        elif kind in ("unlink", "rmdir"):
            judged = self._judged_at.pop((actor, name), None)
            self.replaced_removals += judged is not None and judged != self._gate_generation.get(
                name, 0
            )
            self.stale_judges.pop(name, None)

    def _after_success(self, worker: Worker, kind: str, path: Path, name: str) -> None:
        self._note_access(worker, kind, name)
        self._note_judge(worker, kind, name)
        if kind == "write_text":  # the file ages by the machine clock, as _is_old_junk reads it
            os.utime(path, (self.world.clock, self.world.clock))
        if not _is_gate(path.name):
            return
        if kind == "hardlink_to":
            self.holds.setdefault(path.name, set()).add(worker.actor)
            worker.acquired.add(path.name)
        elif kind in ("unlink", "rmdir"):
            self.holds.get(path.name, set()).discard(worker.actor)

    # ---------------------------------------------------------------- grant

    def parked(self) -> list[Worker]:
        """Workers waiting for a step, in a stable order."""
        waiting = [w for w in self.workers if w.state == "parked"]
        return sorted(waiting, key=lambda w: (w.actor.index, w.root is not w, w.label))

    def grant(self, worker: Worker, *, fail: bool = False) -> None:
        """Let *worker* perform its one pending step (or fail it), then run the checks."""
        assert worker.state == "parked", f"{worker.label} is not waiting for a step"
        assert worker.pending is not None
        faulty = (
            fail and worker.pending[0] in _FAULT_KINDS and worker.faults < MAX_FAULTS_PER_THREAD
        )
        if faulty:
            worker.faults += 1
        before = set(threading.enumerate())
        self._release_worker(worker, "fail" if faulty else "run")
        self._wait_quiet(before)
        self._after_step()

    def _release_worker(self, worker: Worker, verdict: str) -> None:
        with self._cv:
            worker.verdict = verdict
            worker.state = "running"
            self._stepping = worker
            self._cv.notify_all()

    def _wait_quiet(self, before: set[threading.Thread]) -> None:
        def quiet() -> bool:
            # A thread the store started ends without telling the scheduler.
            if not self._all_settled():
                return False
            # A thread the step started has not reached its first checkpoint yet.
            return all(
                t in before
                or not t.is_alive()
                or t in self._by_thread
                or t.name.startswith(self.unstepped_prefixes)
                for t in threading.enumerate()
            )

        with self._cv:
            finished = self._wait_until(quiet)
            self._stepping = None
            stuck = [w.label for w in self.workers if w.state == "running"]
        assert finished, f"threads did not reach a checkpoint within the deadline: {stuck}"

    def _wait_until(self, condition: Callable[[], bool]) -> bool:
        """Wait, holding the lock, until *condition* holds; threads ending are noticed by polling."""
        deadline = time.monotonic() + STEP_DEADLINE_SECONDS
        while not condition():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._cv.wait(min(remaining, _POLL_SECONDS))
        return True

    def _after_step(self) -> None:
        if self._trace is not None:
            for line in self._unsent:
                self._trace(line)
        self._unsent.clear()
        for worker in self.workers:
            if worker.error is not None and not worker.reported:
                worker.reported = True
                raise AssertionError(f"{worker.label} raised {worker.error!r}") from worker.error
        if self.checks_enabled:
            self.check()

    def check(self) -> None:
        """Run every invariant; the first violation stays the reported failure."""
        for check in self.checks:
            try:
                check()
            except AssertionError as exc:
                self.violation = self.violation or exc
                raise

    # --------------------------------------------------------- kill, finish

    def kill(self, actor: Actor) -> None:
        """End *actor*: its threads unwind through ``ActorKilled`` with no filesystem effect."""
        with self._cv:
            actor.killed = True
            self.world.stop(actor.pid)
            for name, gate in self.holds.items():
                if actor in gate:
                    gate.discard(actor)
                    self.stale_judges[name] = set()
            for worker in self.workers:
                if worker.actor is actor and worker.state in ("parked", "idle"):
                    worker.verdict = "kill"
                    worker.state = "running"
                    self._stepping = worker
            self._cv.notify_all()

    def kill_and_wait(self, actor: Actor) -> None:
        before = set(threading.enumerate())
        self.kill(actor)
        self._wait_quiet(before)
        self._after_step()

    def finish(self, worker: Worker, *, cap: int = FINISH_STEP_CAP) -> None:
        """Grant *worker* steps, without faults, until it ends or *cap* steps pass.

        A thread that waits on a gate another parked thread holds retries for a
        long time, so it stops at the cap and stays parked for the schedule to continue.
        """
        for _ in range(cap):
            if worker.state != "parked":
                return
            self.grant(worker)

    def _all_settled(self) -> bool:
        for worker in self.workers:
            if worker.state == "running" and not (worker.thread and worker.thread.is_alive()):
                worker.state = "done"
        return all(w.state != "running" for w in self.workers)

    def drain(self, *, until_idle: bool = False) -> None:
        """Run every remaining thread to completion without faults, checks still on.

        With *until_idle*, stop once every parked thread waits and some thread is idle
        (only its command ends those waits), and share the grants among lock waiters.
        """
        steps = 0
        while waiting := self.parked():
            # A lock waiter runs only when every parked thread waits for a lock: the holder
            # (whichever actor it belongs to) must run first or the waiter spins to its timeout.
            runnable = [w for w in waiting if not _at_lock_wait(w)]
            if not runnable and until_idle:
                if any(w.state == "idle" for w in self.workers):
                    return
                # The least-stepped first: a supervisor's timed wait must not starve a lane.
                self.grant(min(waiting, key=lambda w: w.steps))
            else:
                # Operations first: a background retry must not spin while the holder it waits
                # for is parked.
                self.grant(
                    min(
                        runnable or waiting,
                        key=lambda w: (w.root is not w, w.actor.index, w.label),
                    )
                )
            steps += 1
            assert steps <= DRAIN_STEP_CAP, "drain did not finish"

    # ---------------------------------------------------------- idle workers

    def await_command(self) -> str:
        """Called on a scheduled thread: wait, not at a step, until ``command`` names what to do.

        An idle worker is never picked by ``parked``, ``advance``, ``drain`` or ``finish``. It
        ends with ``ActorKilled`` when its actor is killed.
        """
        worker: Worker = self._local.worker
        if worker.actor.killed:
            raise ActorKilled
        with self._cv:
            worker.state = "idle"
            self._cv.notify_all()
            self._cv.wait_for(lambda: worker.verdict != "")
            verdict, worker.verdict = worker.verdict, ""
        if verdict == "kill":
            raise ActorKilled
        name, worker.command = worker.command, None
        assert name is not None
        return name

    def command(self, worker: Worker, name: str) -> None:
        """Hand an idle worker its next command and run it to its next checkpoint."""
        assert worker.state == "idle", f"{worker.label} is not waiting for a command"
        worker.command = name
        before = set(threading.enumerate())
        self._release_worker(worker, "run")
        self._wait_quiet(before)
        self._after_step()

    def abort(self) -> None:
        """Cleanup that cannot report a check failure: kill every actor and join every thread."""
        self.checks_enabled = False
        for actor in {w.actor for w in self.workers}:
            self.kill(actor)
        with self._cv:
            self._wait_until(self._all_settled)
        for thread in list(self._by_thread):
            thread.join(STEP_DEADLINE_SECONDS)
            assert not thread.is_alive(), f"{thread.name} did not end"
        threading.excepthook = self._previous_hook


class StepLock:
    """A mutual-exclusion lock for scheduled threads: a contended attempt waits at a checkpoint.

    A plain ``threading.Lock`` held across a step blocks the next thread outside any
    checkpoint, which stalls the machine. The wait is not a fault point and does not
    move the clock.
    """

    def __init__(self, scheduler: StepScheduler) -> None:
        self.scheduler = scheduler
        self._mutex = threading.Lock()

    def __enter__(self) -> StepLock:
        for _ in range(DRAIN_STEP_CAP):
            if self._mutex.acquire(blocking=False):
                return self
            self.scheduler.call("wait", Path(), lambda: None)
        raise AssertionError("a lock was never released: the schedule deadlocked")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._mutex.release()


class StepEvent(threading.Event):
    """An event whose waits are checkpoints of *scheduler* on the threads it owns.

    A timed wait is one ``lock_wait`` checkpoint named ``event`` that moves the machine
    clock by the timeout; a wait without one repeats ``wait`` checkpoints until set.
    """

    def __init__(self, scheduler: StepScheduler) -> None:
        super().__init__()
        self.scheduler = scheduler

    def wait(self, timeout: float | None = None) -> bool:
        if not self.scheduler.owns_caller():
            return super().wait(timeout)
        if timeout is None:
            for _ in range(DRAIN_STEP_CAP):
                if self.is_set():
                    return True
                self.scheduler.call("wait", Path("event"), lambda: None)
            raise AssertionError("an event was never set: the schedule deadlocked")
        advance = self.scheduler.world.advance
        self.scheduler.call("lock_wait", Path("event"), lambda: advance(timeout))
        return self.is_set()


def stepped_turn(scheduler: StepScheduler) -> Callable[[threading.Lock, float], bool]:
    """The lane scheduler's ``take_turn`` as one checkpoint, named ``turn``, per attempt."""

    def take(lock: threading.Lock, seconds: float) -> bool:
        def attempt() -> bool:
            taken = lock.acquire(blocking=False)
            if not taken:
                scheduler.world.advance(seconds)
            return taken

        return scheduler.call("lock_wait" if lock.locked() else "turn", Path("turn"), attempt)

    return take


def stepped_file_lock_wait(handle: IO[Any], *, timeout_s: float, **kwargs: Any) -> None:
    """``wait_for_file_lock`` whose waits are checkpoints of the scheduler owning the thread.

    Each contended attempt waits at a ``lock_wait`` checkpoint that moves the clock by the
    production poll, and the wait times out on that clock. A thread no scheduler owns takes
    the real wait.
    """
    scheduler: StepScheduler | None = getattr(_OWNER, "scheduler", None)
    if scheduler is None:
        file_lock.wait_for_file_lock(handle, timeout_s=timeout_s, **kwargs)
        return
    for _ in range(int(timeout_s / LOCK_POLL_SECONDS) + 1):
        try:
            file_lock.acquire_file_lock(
                handle, preserve_contents=bool(kwargs.get("preserve_contents", False))
            )
        except file_lock.LockContentionError:
            scheduler.call("lock_wait", Path(), lambda: scheduler.world.advance(LOCK_POLL_SECONDS))
        else:
            return
    raise file_lock.LockTimeoutError("file lock stayed contended")


def _at_lock_wait(worker: Worker) -> bool:
    return worker.pending is not None and worker.pending[0] in LOCK_WAIT_KINDS


def _is_gate(name: str) -> bool:
    return name == PROMOTION_GATE or (name.startswith(".") and name.endswith(".lease.lock"))


_PATH_BASE = type(Path())


def _stepped(name: str, kind: str) -> Callable[..., Any]:
    original = getattr(_PATH_BASE, name)

    def method(self: SteppedPath, *args: Any, **kwargs: Any) -> Any:
        shown = Path(args[0]).name if kind in ("rename", "replace") else None
        return self.scheduler.call(
            kind, self, lambda: original(self, *args, **kwargs), shown=shown
        )

    method.__name__ = name
    return method


def _listing(name: str) -> Callable[..., Any]:
    original = getattr(_PATH_BASE, name)

    def method(self: SteppedPath, *args: Any, **kwargs: Any) -> Any:
        # Sorted: one order a real directory may return, so a schedule never depends on hashing.
        listed = self.scheduler.call(name, self, lambda: sorted(original(self, *args, **kwargs)))
        return iter(listed)

    method.__name__ = name
    return method


class SteppedPath(_PATH_BASE):  # type: ignore[valid-type,misc]
    """A ``Path`` whose filesystem methods are scheduling points of its scheduler.

    Only the outermost call is a step: ``write_text`` calling ``open`` is one.
    """

    scheduler: ClassVar[StepScheduler]

    def open(self, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        kind = "open_read" if mode.startswith("r") and "+" not in mode else "open_write"
        original = _PATH_BASE.open
        return self.scheduler.call(kind, self, lambda: original(self, mode, *args, **kwargs))


for _name in (
    "exists",
    "is_dir",
    "is_symlink",
    "stat",
    "mkdir",
    "rmdir",
    "unlink",
    "rename",
    "replace",
    "hardlink_to",
    "write_text",
    "read_bytes",
):
    setattr(SteppedPath, _name, _stepped(_name, _name))
for _name in ("glob", "iterdir"):
    setattr(SteppedPath, _name, _listing(_name))


def stepped_root(root: Path, scheduler: StepScheduler) -> Path:
    """*root* as a path class bound to *scheduler*, so concurrent machines never share one."""
    bound = type("MachinePath", (SteppedPath,), {"scheduler": scheduler})
    return bound(root)


class Actor:
    """A simulated process: a fake PID and a store of its own over the stepped root."""

    def __init__(
        self,
        index: int,
        world: FakeProcessWorld,
        scheduler: StepScheduler,
        root: Path,
        recover: Callable[[Actor, ProcessIdentity | None], bool],
        *,
        name: str | None = None,
        recovery: job_slots.SlotRecovery | None = None,
        renews: bool = False,
    ) -> None:
        self.renews = renews
        self.index = index
        self.name = name or "ABC"[index]
        self._base_recovery = recovery or job_slots.SlotRecovery()
        self.pid = FIRST_ACTOR_PID + index
        self.world = world
        self.scheduler = scheduler
        self.root = root
        self._recover = recover
        self.killed = False
        self.tokens: list[job_slots.SlotToken] = []
        self.releasing: dict[str, Worker] = {}
        self.operation: Worker | None = None
        self.store = self._build_store()
        world.start(self.pid)

    def _build_store(self) -> SlotStore:
        return SlotStore(
            self.root,
            is_pid_alive=self.world.is_pid_alive,
            read_cmdline=self.world.read_cmdline,
            now=self.world.now,
            sleep=self.scheduler.sleep,
            renewal_wait=self.scheduler.renewal_wait if self.renews else None,
            capture_identity=self.world.capture_identity,
            observe_identity=self.world.observe_identity,
            recovery=dataclasses.replace(
                self._base_recovery, recover_process_owner=self._recover_owner
            ),
            getpid=lambda: self.pid,
        )

    def _recover_owner(self, identity: ProcessIdentity | None) -> bool:
        return self._recover(self, identity)

    @property
    def busy(self) -> bool:
        return self.operation is not None and self.operation.state != "done"

    def restart(self) -> None:
        """A new process on the same PID: a new identity, a fresh store, no tokens."""
        self.killed = False
        self.tokens = []
        self.releasing = {}
        self.operation = None
        self.world.start(self.pid)
        self.store = self._build_store()
