"""Simulation Record lanes interleaved with Job slot claimants, one filesystem step at a time.

This extends the machine of ``test_job_slot_step_machine.py`` with a fourth process, ``R``,
that runs a real ``SimulationRecordCapacity`` over the same slots and a stepped Booley
Directory: a lane's execution record, its execution-owned entry and every recovery are
scheduling points that can fail as sharing violations. R has up to two lanes, each one
thread that opens ``child_permit`` and takes commands (register or unregister a fake
supervised process, shrink, renew, close), and a supervisor that runs one
``recover_lost_children`` or ``cancel_waiters`` pass per operation. The standalone claimants'
``reap`` is the independent reaper, recovering with the real ``recover_execution`` (its
``/proc`` scan finds no process with a fresh marker, so it proves every lane tree empty).
Lock waits are scheduling points too: the record's file lock and the registry's lock.
The rules never stop the pool after a failed recovery pass. A scenario may run the real lane
scheduler: its lane threads, start turn, supervisor and deadline are steps on the machine clock.
Execution recovery scans Linux ``/proc``, so the module runs on Linux only; its fake PIDs are
above the kernel's PID limit, so no cancellation signal reaches a real process.

Checks after every step, besides the base machine's I2, I3 and I5:

- I1 (override) the holders' Memory Reservations sum to at most the budget.
- I4 (base, narrowed) process-owned entries only; I7 owns execution-owned ones.
- I6 an execution record once read terminal is never read non-terminal again.
- I7 every execution-owned entry is in use (its lane has not ended and R is alive, or it is
  R's Flow claim) or recoverable now.
- S (override) a live standalone waiter stays only while the holders leave its claim no room.
- S2 after the drain, two reaps leave no execution-owned entry but a live R's Flow claim.

``BOOLEY_HYPOTHESIS_EXPLORATORY=1`` selects the base machine's exploratory budget.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import precondition, rule, run_state_machine_as_test

from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.record import coordinator
from booley.flows.sim.record.capacity import (
    ChildPermit,
    LeaseLostError,
    SimulationRecordCapacity,
    SimulationRecordCapacityError,
)
from booley.flows.sim.record.child_protocol import ChildExecutionRegistry
from booley.flows.sim.record.codec import SimulationRecordIntegrityError
from booley.flows.sim.record.scheduler import (
    BoundedSimulationRecordScheduler,
    SimulationRecordSchedulingError,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.runtime import execution_records, job_slots
from booley.runtime.execution_records import (
    ExecutionId,
    execution_paths,
    read_json,
    request_cancellation,
)
from booley.runtime.execution_recovery import recover_execution
from booley.runtime.job_slots import (
    CLASS_MEMORY,
    ROLE_INTERACTIVE,
    ClaimAbortedError,
    ClaimLostError,
    SlotRecovery,
)
from tests.flows.sim.simulation_record_test_support import (
    _named_manifest,
    _pinned_lanes,
    _scheduled_attempt,
)
from tests.job_slot_steps import (
    DRAIN_STEP_CAP,
    FIRST_ACTOR_PID,
    LOCK_WAIT_KINDS,
    Actor,
    ActorKilled,
    FakeProcessWorld,
    StepEvent,
    StepLock,
    Worker,
    stepped_file_lock_wait,
    stepped_root,
    stepped_turn,
    thread_name,
)
from tests.test_job_slot_step_machine import (
    _CLAIM,
    EXPLORATORY_SETTINGS,
    PR_SETTINGS,
    JobSlotStepMachine,
    _fields,
    _step_until,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="execution recovery scans Linux /proc"
)

LANE_PR_SETTINGS = settings(PR_SETTINGS, max_examples=20)

_RECORD_INDEX = 3

_PARENT_EXECUTION = ExecutionId("a" * 32)
_MAX_LANES = 2
_MAX_REGISTERED = 2
_SETTLE_STEPS = 400
_COMMANDS = ("register", "register", "unregister", "unregister", "shrink", "renew", "close")
# Most rule picks do nothing special: a cancellation or a crash ends what the others build on.
_RARE = 10
# What a lane or the supervisor may end with: the checks judge the state it leaves.
_OUTCOMES = (
    ClaimAbortedError,
    ClaimLostError,
    SimulationRecordCapacityError,
    SimulationRecordIntegrityError,
    OSError,
)


@functools.cache
def _manifest() -> Any:
    return _named_manifest(("alpha", "beta"))


class FakeProcess:
    """A child no kernel knows: its PID is above the kernel limit, so no signal reaches a process."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = self.returncode or 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    terminate = kill


@dataclass(eq=False)
class Lane:
    """One lane thread of R: its identity, its fake processes and how it ended."""

    number: int
    work_item_id: str
    worker: Worker | None = None
    execution_id: str | None = None
    permit: ChildPermit | None = None
    registered: list[FakeProcess] = field(default_factory=list)
    shrinks: int = 0
    outcome: BaseException | None = None
    # A scheduled lane: its item's execute returned; a later item took its thread.
    returned: bool = False
    superseded: bool = False

    @property
    def label(self) -> str:
        return f"lane-{self.number}"

    @property
    def ended(self) -> bool:
        return self.superseded or (self.worker is not None and self.worker.state == "done")

    @property
    def idle(self) -> bool:
        return self.worker is not None and self.worker.state == "idle"

    def bind(self, execution_id: str) -> None:
        self.execution_id = str(execution_id)


@dataclass(eq=False)
class ScheduledRun:
    """One ``run`` of the real lane scheduler by R, and how it ended."""

    store: SimulationRecordStore
    worker: Worker | None = None
    error: Exception | None = None

    def lane_workers(self, scheduler: Any) -> list[Worker]:
        """The threads the run started, once each took a step."""
        return [w for w in scheduler.workers if w.root is self.worker and w is not self.worker]


class RecordProcess(Actor):
    """The Simulation Record process: a real capacity over R's store and the stepped Booley Directory."""

    def __init__(
        self,
        world: FakeProcessWorld,
        scheduler: Any,
        root: Path,
        booley_dir: Path,
        recover: Callable[[Actor, Any], bool],
        recovery: SlotRecovery,
    ) -> None:
        self.booley_dir = booley_dir
        self.lanes: list[Lane] = []
        self.supervisor: Worker | None = None
        self.cancelled = False
        self.capacity: SimulationRecordCapacity
        self.flow_token: job_slots.SlotToken | None = None
        self._locks: dict[ExecutionId, StepLock] = {}
        super().__init__(
            _RECORD_INDEX, world, scheduler, root, recover, name="R", recovery=recovery
        )
        self._open_capacity()

    def _open_capacity(self) -> None:
        """Build the process: the Flow's claim (once), then a capacity whose lane locks are steps."""
        self.lanes = []
        self.supervisor = None
        self.cancelled = False
        self._locks = {}
        self.open_capacity()

    def open_capacity(
        self,
        *,
        manifest: Any = None,
        lanes: int = _MAX_LANES,
        timeout: float | None = None,
        cancellation: Callable[[], bool] = lambda: False,
        cause: Callable[[], str | None] | None = None,
        claim: Callable[[Any], Any] = lambda _item: _CLAIM,
    ) -> None:
        """A capacity under the Flow's claim, with *lanes* pinned."""
        self.manifest = manifest or _manifest()
        if self.flow_token is None:
            # As ``flow_claim.py`` does before items take their own claims: admitted, then zero.
            self.flow_token = self.store.acquire(
                CLASS_MEMORY, pid=self.pid, execution_id=_PARENT_EXECUTION, memory=_CLAIM
            )
            self.store.shrink_reservation(self.flow_token, 0)
        admission = AdmissionContext(
            "managed",
            self.store,
            self.flow_token,
            job_slots.ROLE_INTERACTIVE,
            _PARENT_EXECUTION,
            timeout,
            cancellation,
        )
        if cause is not None:
            admission = replace(admission, cancellation_cause=cause)
        self.capacity = _pinned_lanes(
            admission, self.manifest, self.booley_dir, claim, lanes=lanes
        )
        # Private: the registry's per-execution lock is an in-process lock held across filesystem
        # steps. No public entry point exchanges it, and a plain threading.Lock held across a
        # step stalls the scheduler, so its waiters are made steps.
        self.capacity._lanes._record_lock = self._step_lock  # type: ignore[method-assign]

    def _step_lock(self, execution_id: ExecutionId) -> StepLock:
        return self._locks.setdefault(execution_id, StepLock(self.scheduler))

    def restart(self) -> None:
        """A new process on the same PID: no lanes, and the old Flow claim left to be recovered."""
        super().restart()
        self._open_capacity()


class LaneStepMachine(JobSlotStepMachine):
    """The three standalone claimants plus one Simulation Record process with lanes."""

    def __init__(self, parent: Path) -> None:
        self._torn_down = False
        super().__init__(parent, recovery=self._execution_recovery())
        # The scope watcher only polls ``cancelled``; no scenario registers a process in a run.
        self.scheduler.unstepped_prefixes = ("booley-record-process-cancellation",)
        self.booley_real = Path(self._temporary.name) / "booley"
        self.booley_dir = stepped_root(self.booley_real, self.scheduler)
        self._record_registry = ChildExecutionRegistry.from_project_data(
            _manifest(), self.booley_dir, booley_dir=self.booley_dir
        )
        root = self.actors[0].root
        self.record = RecordProcess(
            self.world,
            self.scheduler,
            root,
            self.booley_dir,
            self._recover,
            self._execution_recovery(),
        )
        self._terminal_ids: set[str] = set()
        self._lanes_opened = 0
        self._fake_pids = 0
        self._step_reads: dict[str, Any] | None = None
        self.scheduler.checks = [
            self._begin_checks,
            *self.scheduler.checks,
            self._check_terminal_records,
            self._check_execution_entries,
            self._end_checks,
        ]

    def teardown(self) -> None:
        if not self._torn_down:
            self._torn_down = True
            super().teardown()

    # ----------------------------------------------------------- recovery

    def _execution_recovery(self) -> SlotRecovery:
        """How every store recovers an execution-owned entry: the real recovery, stepped."""
        return SlotRecovery(
            execution_is_terminal=lambda execution_id: self._record_registry.is_terminal(
                ExecutionId(execution_id)
            ),
            cancel_execution=lambda execution_id: request_cancellation(
                execution_paths(execution_id, booley_dir=self.booley_dir)
            ),
            recover_execution=lambda execution_id: recover_execution(
                execution_id, booley_dir=self.booley_dir
            ),
        )

    # ------------------------------------------------------ lane operations

    def start_lane(self) -> Lane:
        """Open a lane of R; its thread parks at its first filesystem step."""
        record = self.record
        self._lanes_opened += 1
        items = _manifest().document["work_items"]
        lane = Lane(
            self._lanes_opened, str(items[self._lanes_opened % len(items)]["work_item_id"])
        )
        record.lanes.append(lane)
        lane.worker = self.scheduler.start(record, lane.label, lambda: self._run_lane(lane))
        return lane

    def _run_lane(self, lane: Lane) -> None:
        record = self.record
        try:
            with record.capacity.child_permit(
                lane.work_item_id,
                attempt_id=f"attempt-{lane.number}",
                attempt_ordinal=lane.number,
                attempt_directory=Path("attempt"),
                publish=lane.bind,
                memory=_CLAIM,
            ) as permit:
                lane.permit = permit
                while (name := self.scheduler.await_command()) != "close":
                    self._lane_command(lane, record, permit, name)
        except _OUTCOMES as exc:
            lane.outcome = exc

    def _lane_command(
        self, lane: Lane, record: RecordProcess, permit: ChildPermit, name: str
    ) -> None:
        processes = permit.scope.processes
        if name == "register":
            self._fake_pids += 1
            process = FakeProcess(FIRST_ACTOR_PID + 1000 + self._fake_pids)
            lane.registered.append(process)
            processes.register(process)  # type: ignore[arg-type]
        elif name == "unregister":
            processes.unregister(lane.registered.pop(0))  # type: ignore[arg-type]
        elif name == "shrink":
            lane.shrinks += 1
            record.store.shrink_reservation(permit.token, 0.5 if lane.shrinks == 1 else 0)
        elif name == "renew":
            record.store.renew(permit.token)

    def command_lane(self, lane: Lane, name: str) -> None:
        """Give an idle lane a command: ``unregister`` with nothing registered becomes ``register``,
        and ``close`` or ``register`` with the process set full becomes ``unregister``."""
        assert lane.worker is not None
        if name == "unregister" and not lane.registered:
            name = "register"
        if name == "close" and lane.registered:
            name = "unregister"
        if name == "register" and len(lane.registered) >= _MAX_REGISTERED:
            name = "unregister"
        self.scheduler.command(lane.worker, name)

    def start_supervisor(self, which: str) -> Worker:
        """One supervisor pass of R: it parks at its first filesystem or lock step."""
        record = self.record
        capacity = record.capacity
        if which == "cancel":
            record.cancelled = True

        def work() -> None:
            try:
                if which == "cancel":
                    capacity.cancel_waiters("machine cancel")
                else:
                    capacity.recover_lost_children()
            except _OUTCOMES:
                pass

        record.supervisor = self.scheduler.start(record, f"supervise-{which}", work)
        return record.supervisor

    # ------------------------------------------------------- lane scheduler

    def run_scheduler(
        self, items: Any = None, *, finish: Callable[[], None] = lambda: None
    ) -> ScheduledRun:
        """R runs the real lane scheduler; each item idles until ``close``, then *finish*es."""
        record = self.record
        run = ScheduledRun(SimulationRecordStore(Path(self._temporary.name) / "record"))
        run.store.publish_manifest(record.manifest)

        def prepare(attempt: Any, child_id: str) -> None:
            worker = self.scheduler.current_worker()
            for lane in record.lanes:
                lane.superseded |= lane.worker is worker
            item = str(attempt.item["work_item_id"])
            record.lanes.append(Lane(len(record.lanes) + 1, item, worker, str(child_id)))

        def execute(_attempt: Any, _child_id: str | None) -> None:
            worker = self.scheduler.current_worker()
            lane = next(x for x in record.lanes if x.worker is worker and not x.returned)
            while self.scheduler.await_command() != "close":
                pass
            finish()
            lane.returned = True

        pool = BoundedSimulationRecordScheduler(
            record.capacity,
            allocate=lambda item: _scheduled_attempt(run.store, item),
            execute=execute,
            prepare_child=prepare,
            clock=self.world.now,
            event=lambda: StepEvent(self.scheduler),
            take_turn=stepped_turn(self.scheduler),
        )

        def schedule() -> None:
            try:
                pool.run(items or record.manifest.document["work_items"])
            except Exception as exc:  # noqa: BLE001 -- the scenario judges it
                run.error = exc
            finally:
                record.store.release(record.flow_token)  # as the endpoint's ``finally``

        run.worker = self.scheduler.start(record, "schedule", schedule)
        return run

    def run_lanes(self, run: ScheduledRun, until: Callable[[], bool]) -> None:
        """Step the run's lane threads, not its supervisor, until *until* holds."""
        for _ in range(DRAIN_STEP_CAP):
            if until():
                return
            lanes = [w for w in run.lane_workers(self.scheduler) if w.state == "parked"]
            # Runnable before waiting, then the least-stepped, so every waiter gets turns.
            self.scheduler.grant(min(lanes, key=lambda w: (_waits(w), w.steps, thread_name(w))))
        raise AssertionError("the lanes did not get there")

    def start_idle(self, count: int, **kwargs: Any) -> tuple[ScheduledRun, list[Lane]]:
        """A run stepped until *count* items idle; the lanes are in the order they started."""
        run = self.run_scheduler(**kwargs)
        self.run_lanes(run, lambda: len(self._idle_lanes()) == count)
        return run, self._idle_lanes()

    def close_item(self, run: ScheduledRun, lane: Lane) -> None:
        """Close one item and run its lane to its end."""
        self.command_lane(lane, "close")
        self.run_lanes(run, lambda: lane.ended)

    def supervise(self, run: ScheduledRun, passes: int = 1) -> None:
        """Grant the supervisor's timed wait *passes* times, each with the pass it starts; a
        run that ended takes none, and the scenario judges how it ended."""
        worker = run.worker
        assert worker is not None
        for _ in range(passes):
            if worker.state != "parked":
                return
            for _ in range(DRAIN_STEP_CAP):
                self.scheduler.grant(worker)
                if worker.state != "parked" or worker.pending == ("lock_wait", "event"):
                    break
            else:
                raise AssertionError("the supervisor pass did not end")

    # ---------------------------------------------------------------- rules

    def _record_alive(self) -> bool:
        return not self.record.killed

    def _idle_lanes(self) -> list[Lane]:
        lanes = self.record.lanes if self._record_alive() else []
        return [lane for lane in lanes if lane.idle and not lane.returned]

    def _registering_lanes(self) -> list[Lane]:
        return [lane for lane in self._idle_lanes() if len(lane.registered) == 1]

    def _lost_lanes(self) -> list[Lane]:
        return [
            lane
            for lane in self.record.lanes
            if not lane.ended and lane.permit and lane.permit.lease_health.lost.is_set()
        ]

    def _supervisor_free(self) -> bool:
        supervisor = self.record.supervisor
        return self._record_alive() and (supervisor is None or supervisor.state == "done")

    def _can_open_lane(self) -> bool:
        lanes = self.record.lanes
        return (
            self._record_alive()
            and not self.record.cancelled
            and sum(not lane.ended for lane in lanes) < _MAX_LANES
        )

    def _open_registered_lane(self) -> Lane | None:
        """A lane that runs, with no fault, to its first command and registers a process."""
        lane = self.start_lane()
        self.settled(lane.worker)
        if lane.idle:
            self.command_lane(lane, "register")
            self.settled(lane.worker)
        return lane if lane in self._registering_lanes() else None

    @precondition(lambda self: self._can_open_lane())
    @rule(settle=st.booleans())
    def open_lane(self, settle: bool) -> None:
        """Open a lane, and with *settle* run it, with no fault, to its first command."""
        self.settled(self.start_lane().worker, settle)

    @precondition(lambda self: bool(self._idle_lanes()))
    @rule(which=st.integers(0, 99), name=st.sampled_from(_COMMANDS), settle=st.booleans())
    def command_a_lane(self, which: int, name: str, settle: bool) -> None:
        idle = self._idle_lanes()
        lane = idle[which % len(idle)]
        self.command_lane(lane, name)
        self.settled(lane.worker, settle)

    def settled(self, worker: Worker | None, settle: bool = True) -> None:
        """With *settle*, run *worker* without faults to its next command or its end, or until
        it waits (for a lock, or for admission, which may never come): the rules' steps are few."""
        for _ in range(_SETTLE_STEPS if settle else 0):
            if worker is None or worker.state != "parked" or worker.pending is None:
                return
            if worker.pending[0] in (*LOCK_WAIT_KINDS, "sleep"):
                return
            self.scheduler.grant(worker)

    @precondition(lambda self: self._supervisor_free() and bool(self._lost_lanes()))
    @rule()
    def recover_lost_children(self) -> None:
        """The supervisor's recovery pass, once a lane's lease is lost."""
        self.start_supervisor("recover")

    @precondition(lambda self: self._supervisor_free() and not self.record.cancelled)
    @rule(pick=st.integers(0, _RARE))
    def cancel_record(self, pick: int) -> None:
        """The supervisor's cancellation pass (rare: it ends admission for good)."""
        if pick == 0:
            self.start_supervisor("cancel")

    @rule(seconds=st.integers(1, 61), pick=st.integers(0, 2 * _RARE))
    def advance_clock(self, seconds: int, pick: int) -> None:
        """Time passes, but a lane's lease should usually outlive the schedule."""
        if pick == 0:
            self.world.clock += seconds

    @precondition(lambda self: bool(self._registering_lanes()) or self._can_open_lane())
    @rule(which=st.integers(0, 99), lapse=st.booleans(), lead=st.integers(0, 2))
    def contend_record(self, which: int, lapse: bool, lead: int) -> None:
        """A lane unregisters its last process, while every reaper and the supervisor recover it.

        With *lapse* the lane's lease has run out. The lane takes *lead* steps, then the
        recoveries run to their ends: the lane's record write meets a recovery that began
        before it and finished within it, at a different step each time.
        """
        lanes = self._registering_lanes() or [self._open_registered_lane()]
        lane = lanes[which % len(lanes)]
        if lane is None:
            return
        if lapse:
            self.world.clock += job_slots.LEASE_DURATION_SECONDS + 1
        self.command_lane(lane, "unregister")
        assert lane.worker is not None
        for _ in range(lead):
            if lane.worker.state == "parked":
                self.scheduler.grant(lane.worker)
        recoveries = [
            self.start_operation(actor, "reap", job_slots.ROLE_INTERACTIVE, 0)
            for actor in self._idle()
        ]
        if self._supervisor_free():
            recoveries.append(self.start_supervisor("recover"))
        for worker in recoveries:
            self.settled(worker)

    @rule(pick=st.integers(0, _RARE), recycle=st.booleans())
    def crash_record(self, pick: int, recycle: bool) -> None:
        """R dies (rarely: its lanes and supervisor go with it), and its PID may start a new
        process at once; or else a dead R restarts."""
        if pick != 0:
            return
        if self._record_alive():
            self.scheduler.kill_and_wait(self.record)
        if recycle or not self._record_alive():
            self.record.restart()

    # ------------------------------------------------------------- checks

    def _owner_of(self, pid: int) -> Actor:
        if pid == self.record.pid:
            return self.record
        assert FIRST_ACTOR_PID <= pid < FIRST_ACTOR_PID + _RECORD_INDEX, (
            f"an entry names PID {pid}, which no simulated process has"
        )
        return super()._owner_of(pid)

    def _reserved_mb(self, payload: dict[str, Any]) -> float:
        memory = payload.get("memory")
        reserved = memory.get("reservation_mb") if isinstance(memory, dict) else None
        return float(reserved) if isinstance(reserved, int | float) else 0.0

    def _check_one_holder_per_slot(self) -> None:
        reserved = sum(
            self._reserved_mb(payload)
            for name, payload in self.read_entries().items()
            if name.startswith("h-") and payload is not None
        )
        assert reserved <= _CLAIM.budget_mb, (
            f"I1: holders reserve {reserved} MB of {_CLAIM.budget_mb} MB"
        )

    def _begin_checks(self) -> None:
        self._step_reads = {}  # the checks of one step read the directories once between them

    def _end_checks(self) -> None:
        self._step_reads = None

    def _read_once(self, key: str, read: Callable[[], Any]) -> Any:
        if self._step_reads is None:
            return read()
        return self._step_reads.setdefault(key, read())

    def read_entries(self) -> dict[str, dict[str, Any] | None]:
        return self._read_once("entries", super().read_entries)

    def _step_records(self) -> dict[str, dict[str, Any] | None]:
        """Execution ID -> record payload (None when unreadable), read around the scheduler."""
        return self._read_once("records", self._read_records)

    def _read_records(self) -> dict[str, dict[str, Any] | None]:
        root = self.booley_real / ".runtime" / "executions"
        directories = sorted(root.iterdir()) if root.is_dir() else []
        return {path.name: self.read_payload(path / "record.json") for path in directories}

    @staticmethod
    def _is_terminal_record(record: dict[str, Any] | None) -> bool:
        return (
            record is not None
            and record.get("state") == "terminal"
            and record.get("tree_terminal") is True
        )

    def _check_terminal_records(self) -> None:
        for execution_id, record in self._step_records().items():
            if self._is_terminal_record(record):
                self._terminal_ids.add(execution_id)
            elif record is not None and execution_id in self._terminal_ids:
                raise AssertionError(
                    f"I6: the record of {execution_id} was terminal and reads {record.get('state')!r}"
                )

    def _check_execution_entries(self) -> None:
        records = self._step_records()
        for name, payload in self.read_entries().items():
            fields = _fields(name)
            if payload is None or fields is None or payload.get("owner_kind") != "execution":
                continue
            execution_id = str(payload.get("execution_id"))
            owner = self._owner_of(fields[1])
            in_use = self._execution_in_use(owner, execution_id)
            recoverable = (
                self._is_terminal_record(records.get(execution_id))
                or self._recoverable(owner, fields[0], payload)
                or payload.get("lease_state") == job_slots.LEASE_CANCELLING
            )
            assert in_use or recoverable, (
                f"I7: {name} of {execution_id} is neither in use nor recoverable: "
                f"record {records.get(execution_id)}"
            )

    def _execution_in_use(self, owner: Actor, execution_id: str) -> bool:
        if owner is not self.record or self.record.killed:
            return False
        if execution_id == _PARENT_EXECUTION:
            return True  # R's Flow claim
        return any(
            lane.execution_id == execution_id and not lane.ended for lane in self.record.lanes
        )

    def _is_flow_claim(self, payload: dict[str, Any]) -> bool:
        return payload.get("execution_id") == _PARENT_EXECUTION and not self.record.killed

    def _budget_is_full(self, entries: dict[str, dict[str, Any] | None]) -> bool:
        reserved = sum(
            self._reserved_mb(payload)
            for name, payload in entries.items()
            if name.startswith("h-") and payload is not None
        )
        return _CLAIM.budget_mb - reserved < _CLAIM.reservation_mb

    # ------------------------------------------------------------ teardown

    def _wind_down(self) -> None:
        """Cancel the record, close its lanes, drain, then judge S and S2."""
        if self._record_alive():
            # First: a lane still waiting for admission stops only once the record is cancelled.
            self.start_supervisor("cancel")
            self._close_lanes()
        super()._wind_down()
        self._check_executions_recovered()

    def _close_lanes(self) -> None:
        for _ in range(4 * _MAX_LANES * (_MAX_REGISTERED + 2)):
            # Idle lanes first: a drain cannot end the waits that depend on them.
            for lane in self._idle_lanes():
                assert lane.worker is not None
                self.scheduler.command(lane.worker, "unregister" if lane.registered else "close")
            self.scheduler.drain(until_idle=True)
            if not self._idle_lanes():
                return
        raise AssertionError("lanes did not close")

    def _check_executions_recovered(self) -> None:
        """Two reaps leave no execution-owned entry: recovery happened, whatever the record says."""
        reaper = next((actor for actor in self.actors if not actor.killed), None)
        if reaper is None and self._record_alive():
            reaper = self.record
        if reaper is None:
            return
        for _ in range(2):
            with contextlib.suppress(ActorKilled):
                reaper.store.reap(CLASS_MEMORY)
        left = [
            name
            for name, payload in self.read_entries().items()
            if payload is not None
            and payload.get("owner_kind") == "execution"
            and not self._is_flow_claim(payload)
        ]
        assert not left, f"S2: execution-owned entries survive two reaps: {left}"


def _waits(worker: Worker) -> bool:
    return worker.pending is not None and worker.pending[0] in (*LOCK_WAIT_KINDS, "sleep")


def _flow_holds(machine: LaneStepMachine) -> bool:
    return any(
        name.startswith("h-") and payload and payload.get("execution_id") == _PARENT_EXECUTION
        for name, payload in machine.read_entries().items()
    )


def _holders(machine: LaneStepMachine) -> int:
    return sum(name.startswith("h-") for name in machine.read_entries())


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def _no_inherited_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """``submit`` links no inherited execution, whatever environment the suite runs in."""
    monkeypatch.delenv(job_slots.RUNTIME_EXECUTION_ENV, raising=False)


@pytest.fixture(autouse=True)
def _stepped_record_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The execution record's file-lock wait is a scheduling point of the thread's scheduler."""
    monkeypatch.setattr(execution_records, "wait_for_file_lock", stepped_file_lock_wait)


@pytest.fixture
def build_machine(tmp_path: Path) -> Iterator[Callable[[], LaneStepMachine]]:
    """Builds the machine inside the test body and tears it down, with its threads, afterwards."""
    built: list[LaneStepMachine] = []

    def build() -> LaneStepMachine:
        built.append(LaneStepMachine(tmp_path))
        return built[-1]

    yield build
    for machine in reversed(built):
        machine.teardown()


# --------------------------------------------------------------------- tests


def _run_lane_to_idle(machine: LaneStepMachine, lane: Lane) -> None:
    machine.settled(lane.worker)
    assert lane.idle, f"{lane.label} did not reach its first command: {lane.outcome!r}"


def _execution_claims(machine: LaneStepMachine) -> list[dict[str, Any]]:
    payloads = (payload for payload in machine.read_entries().values() if payload is not None)
    return [payload for payload in payloads if payload.get("owner_kind") == "execution"]


def _queue_lane(machine: LaneStepMachine) -> tuple[Lane, Worker]:
    """Two standalone holders fill the budget, so a new lane waits at its admission poll."""
    for actor in machine.actors[:2]:
        for op in ("submit", "refresh"):
            machine.scheduler.finish(machine.start_operation(actor, op, ROLE_INTERACTIVE, 0))
    lane = machine.start_lane()
    machine.settled(lane.worker)
    assert lane.worker is not None and lane.worker.pending == ("sleep", "")
    return lane, lane.worker


def _lane_record(machine: LaneStepMachine, lane: Lane) -> dict[str, Any]:
    assert lane.execution_id is not None
    record = execution_paths(lane.execution_id, booley_dir=machine.booley_real).record
    payload = machine.read_payload(record)
    assert payload is not None
    return payload


def test_lane_operations_interleave_with_a_standalone_claimant(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    claimant = machine.actors[0]
    lane = machine.start_lane()
    assert lane.worker is not None
    # The lane has prepared its execution and not yet published its entry.
    _step_until(machine, lane.worker, "hardlink_to", "w-")
    machine.scheduler.finish(
        machine.start_operation(claimant, "submit", job_slots.ROLE_INTERACTIVE, 0)
    )
    machine.scheduler.finish(
        machine.start_operation(claimant, "refresh", job_slots.ROLE_INTERACTIVE, 0)
    )
    _run_lane_to_idle(machine, lane)
    owned = [claim["execution_id"] for claim in _execution_claims(machine)]
    assert owned == [_PARENT_EXECUTION, lane.execution_id]
    # `register` parks before it takes the record lock, and a reaper runs in between.
    machine.scheduler.command(lane.worker, "register")
    _step_until(machine, lane.worker, "open_write", "record.lock")
    machine.scheduler.finish(
        machine.start_operation(machine.actors[1], "reap", job_slots.ROLE_INTERACTIVE, 0)
    )
    machine.scheduler.finish(lane.worker)
    assert lane.idle
    machine.scheduler.command(lane.worker, "shrink")
    machine.scheduler.finish(lane.worker)
    claims = [c["memory"]["reservation_mb"] for c in _execution_claims(machine)]
    assert claims == [0, 0.5]


def test_execution_record_checks_run_after_every_step(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    # I6: a record read terminal and then waiting again.
    machine = build_machine()
    lane = machine.start_lane()
    _run_lane_to_idle(machine, lane)
    assert lane.execution_id is not None
    record = execution_paths(lane.execution_id, booley_dir=machine.booley_real).record
    waiting = record.read_bytes()
    reaper = machine.start_operation(machine.actors[1], "reap", job_slots.ROLE_INTERACTIVE, 0)
    record.write_bytes(
        waiting.replace(b'"state": "waiting"', b'"state": "terminal"').replace(
            b'"tree_terminal": false', b'"tree_terminal": true'
        )
    )
    machine.scheduler.grant(reaper)
    record.write_bytes(waiting)
    with pytest.raises(AssertionError, match="I6"):
        machine.scheduler.grant(reaper)

    # I7: an execution-owned entry of no lane, with a live owner, a live lease and a waiting record.
    machine = build_machine()
    reaper = machine.start_operation(machine.actors[1], "reap", job_slots.ROLE_INTERACTIVE, 0)
    identity = machine.world.capture_identity(machine.record.pid)
    assert identity is not None
    payload = {
        "owner_kind": "execution",
        "execution_id": "b" * 32,
        "lease_state": "active",
        "lease_expires_at": job_slots.rfc3339_from_epoch(machine.world.clock + 1000),
        "owner_identity": identity.to_payload(),
    }
    (machine.memory_dir / f"h-p0-s0000000099-{machine.record.pid:010d}-0001.json").write_text(
        json.dumps(payload), encoding="utf-8", newline="\n"
    )
    with pytest.raises(AssertionError, match="I7"):
        machine.scheduler.grant(reaper)

    # S2: recovery refuses an execution whose context names another one, and leaves its entry.
    machine = build_machine()
    lane = machine.start_lane()
    _run_lane_to_idle(machine, lane)
    assert lane.execution_id is not None
    context = execution_paths(lane.execution_id, booley_dir=machine.booley_real).context
    context.write_bytes(context.read_bytes().replace(lane.execution_id.encode(), b"c" * 32))
    machine.scheduler.kill_and_wait(machine.record)
    with pytest.raises(AssertionError, match="S2"):
        machine.teardown()


def test_unregister_cannot_replace_recovery_terminal_record(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    reaper_actor = machine.actors[0]
    lane = machine.start_lane()
    assert lane.worker is not None
    _run_lane_to_idle(machine, lane)
    machine.scheduler.command(lane.worker, "register")
    machine.scheduler.finish(lane.worker)
    assert lane.idle and lane.execution_id is not None
    # The lease runs out while the lane's process is still registered.
    machine.world.clock += job_slots.LEASE_DURATION_SECONDS + 1
    machine.scheduler.command(lane.worker, "unregister")
    _step_until(machine, lane.worker, "open_read", "record.json")
    machine.scheduler.grant(lane.worker)  # the lane has read the record: still running
    reaper = machine.start_operation(reaper_actor, "reap", job_slots.ROLE_INTERACTIVE, 0)
    for _ in range(100):  # below the record lock's timeout
        if reaper.state != "parked" or reaper.pending == ("lock_wait", ""):
            break
        machine.scheduler.grant(reaper)
    machine.scheduler.drain()
    final = _lane_record(machine, lane)
    assert final["state"] == "terminal"
    assert final["terminal_cause"] == "orphan_recovered"
    assert reaper.oserror is None


def test_lock_waiters_let_the_holder_run(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    lane = machine.start_lane()
    assert lane.worker is not None
    _run_lane_to_idle(machine, lane)
    assert lane.permit is not None
    # A lost lease keeps the lane from confirming it, so the supervisor may recover it.
    lane.permit.lease_health.lost.set()
    machine.scheduler.command(lane.worker, "close")
    _step_until(machine, lane.worker, "open_write", "record.lock")  # inside mark_terminal's lock
    supervisor = machine.start_supervisor("recover")
    for _ in range(50):
        machine._run_to_visible(supervisor)
        if supervisor.pending is not None and supervisor.pending[0] == "wait":
            break
    assert supervisor.pending is not None and supervisor.pending[0] == "wait"
    machine.scheduler.drain()
    assert lane.ended
    assert supervisor.state == "done"


def test_a_cancel_withdraws_a_queued_lane_claim(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    lane, worker = _queue_lane(machine)
    machine.scheduler.finish(machine.start_supervisor("cancel"))
    machine.scheduler.finish(worker)
    assert lane.permit is None and isinstance(lane.outcome, ClaimLostError)
    assert not [name for name in machine.read_entries() if name.startswith("w-")]


def test_a_cancel_after_a_waiting_lane_is_promoted_retires_it_as_an_abort(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    lane, worker = _queue_lane(machine)
    release = machine.start_operation(machine.actors[0], "release", ROLE_INTERACTIVE, 0)
    machine.scheduler.finish(release)
    machine.scheduler.finish(worker)  # promoted, it runs to its first command
    assert lane.idle and lane.permit is not None
    machine.scheduler.finish(machine.start_supervisor("cancel"))
    machine.command_lane(lane, "close")  # then the lane confirms its lease
    machine.scheduler.finish(worker)
    assert isinstance(lane.outcome, ClaimAbortedError) and "cancellation" in str(lane.outcome)
    assert not lane.permit.lease_health.lost.is_set()
    assert _lane_record(machine, lane)["terminal_cause"] == "orphan_recovered"
    assert [claim["execution_id"] for claim in _execution_claims(machine)] == [_PARENT_EXECUTION]


@pytest.mark.parametrize(
    ("while_busy", "error", "cause"),
    [
        ("freed", None, "completed"),
        ("renewed", None, "completed"),
        ("lost", LeaseLostError, "lease_lost"),
        ("cancelled", ClaimAbortedError, "orphan_recovered"),
    ],
)
def test_a_busy_lease_gate_at_lane_confirmation_is_retried(
    build_machine: Callable[[], LaneStepMachine],
    while_busy: str,
    error: type[BaseException] | None,
    cause: str,
) -> None:
    machine = build_machine()
    lane = machine.start_lane()
    _run_lane_to_idle(machine, lane)
    assert lane.permit is not None and lane.worker is not None
    record, token = machine.record, lane.permit.token
    gate = f".{token.lease_id}.lease.lock"
    # The lane's renewal thread takes the lease gate for a tick and holds it.
    tick = machine.scheduler.start(record, "tick", lambda: record.store.renew(token))
    _step_until(machine, tick, "hardlink_to", gate)
    machine.scheduler.grant(tick)
    machine.command_lane(lane, "close")
    for _ in range(21):  # a refused renewal reads the held gate 20 times: the lane now retries
        _step_until(machine, lane.worker, "read_bytes", gate)
        machine.scheduler.grant(lane.worker)
    if while_busy == "lost":
        machine.world.clock += job_slots.LEASE_DURATION_SECONDS + 1
    if while_busy == "renewed":  # the tick extends the lease and still holds the gate
        _step_until(machine, tick, "unlink", gate)
        machine.scheduler.finish(lane.worker)
        assert _lane_record(machine, lane)["terminal_cause"] == "completed"
    machine.scheduler.finish(
        machine.start_supervisor("cancel") if while_busy == "cancelled" else tick
    )
    machine.scheduler.drain()
    assert type(lane.outcome) is error if error else lane.outcome is None
    assert _lane_record(machine, lane)["terminal_cause"] == cause
    assert [claim["execution_id"] for claim in _execution_claims(machine)] == [_PARENT_EXECUTION]


def test_the_scheduler_starts_only_its_bounded_lane_threads(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    manifest = _named_manifest(("alpha", "beta", "gamma", "delta"))
    machine.record.open_capacity(manifest=manifest, lanes=3)
    run, _ = machine.start_idle(2)
    # The third lane is alive too, waiting for room in the budget.
    machine.run_lanes(
        run, lambda: any(w.pending == ("sleep", "") for w in run.lane_workers(machine.scheduler))
    )
    machine._close_lanes()
    threads = sorted(thread_name(w) for w in run.lane_workers(machine.scheduler))
    assert threads == ["booley-record-child-1", "booley-record-child-2", "booley-record-outer"]
    assert run.worker is not None and run.worker.state == "done" and run.error is None


def test_a_lane_past_the_cancel_deadline_holds_the_outer_slot_within_lanes(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    cancelled = threading.Event()
    machine.record.open_capacity(timeout=1.0, cancellation=cancelled.is_set)
    earlier = set(threading.enumerate())
    run, (alpha, beta) = machine.start_idle(2)
    cancelled.set()
    held = [_holders(machine)]
    machine.close_item(run, alpha)  # beta ignores the cancellation
    machine.supervise(run)
    machine.world.advance(1.0)
    machine.supervise(run)
    assert isinstance(run.error, SimulationRecordSchedulingError)
    assert "bounded shutdown" in str(run.error)
    assert beta.work_item_id in run.store.scan().interrupted
    assert _flow_holds(machine)  # past the endpoint's release
    # The cancellation recovered beta's claim, so A and B fit beside the Flow's; C does not.
    for actor in machine.actors:
        for op in ("submit", "refresh"):
            machine.scheduler.finish(machine.start_operation(actor, op, ROLE_INTERACTIVE, 0))
    first, second, third = (actor.tokens[0] for actor in machine.actors)
    assert first.is_holder and second.is_holder and not third.is_holder
    assert _flow_holds(machine) and max(*held, _holders(machine)) <= _MAX_LANES + 1
    alive = {
        t.name for t in set(threading.enumerate()) - earlier if t.name.startswith("booley-record-")
    }
    assert beta.worker is not None and beta.worker.thread is not None
    assert alive == {beta.worker.thread.name, "booley-record-process-cancellation"}
    machine.command_lane(beta, "close")
    machine.scheduler.drain(until_idle=True)
    assert not _flow_holds(machine)
    for actor, op in zip(machine.actors, ("release", "refresh", "refresh"), strict=True):
        machine.scheduler.finish(machine.start_operation(actor, op, ROLE_INTERACTIVE, 0))
    assert third.is_holder
    for actor in machine.actors[1:]:
        machine.scheduler.finish(machine.start_operation(actor, "release", ROLE_INTERACTIVE, 0))
    assert machine.read_entries() == {}


def test_a_failed_recovery_pass_still_holds_the_outer_slot_until_lanes_exit(
    build_machine: Callable[[], LaneStepMachine], monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = build_machine()
    machine.record.open_capacity(timeout=1.0)
    run, _ = machine.start_idle(2)

    def failing_recovery() -> None:
        # The owner loop must survive a recovery error (for example a file error). Patched:
        # no public entry point makes a lost child's recovery write fail.
        raise OSError("recovery write failed")

    monkeypatch.setattr(machine.record.capacity, "recover_lost_children", failing_recovery)
    machine.supervise(run)
    machine.world.advance(1.0)
    machine.supervise(run)
    assert isinstance(run.error, SimulationRecordSchedulingError)
    assert "bounded shutdown" in str(run.error)
    assert _flow_holds(machine)  # past the endpoint's release
    machine._close_lanes()
    assert machine.read_entries() == {}


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [("lease_lost", "lease lost"), ("token_missing", "Memory Reservation missing")],
    ids=["lease_lost", "token_missing"],
)
def test_the_outer_lane_names_the_lost_slot(
    build_machine: Callable[[], LaneStepMachine], scenario: str, reason: str
) -> None:
    machine = build_machine()
    token = machine.record.flow_token
    assert token is not None
    cause = endpoint_admission._admission_cancellation_cause(machine.record.store, token)
    machine.record.open_capacity(
        lanes=1, cancellation=lambda: cause() is not None, cause=cause, claim=lambda _item: None
    )
    messages: list[str] = []

    def publish() -> None:
        try:
            coordinator.raise_if_cancelled_before_publication()
        except SimulationRecordCapacityError as exc:
            messages.append(str(exc))

    items = machine.record.manifest.document["work_items"][:1]
    run, (item,) = machine.start_idle(1, items=items, finish=publish)
    entry = machine.memory_dir / token.path.name
    if scenario == "lease_lost":
        token.lease_health.lost.set()
    else:
        entry.unlink()  # as a reaper would
    machine.supervise(run)  # its argument-less cancel_waiters must not mask the cause
    machine.close_item(run, item)
    (message,) = messages
    assert "outer lane" in message and reason in message and token.path.name in message
    machine.scheduler.drain()
    assert scenario == "lease_lost" or not entry.exists()


def test_normal_work_runtime_does_not_consume_the_shutdown_budget(
    build_machine: Callable[[], LaneStepMachine],
) -> None:
    machine = build_machine()
    machine.record.open_capacity(timeout=0.01)
    run, _ = machine.start_idle(2)
    machine.world.advance(0.04)
    machine.supervise(run, passes=2)
    assert run.worker is not None and run.worker.state == "parked"  # still supervising
    machine._close_lanes()
    assert run.worker is not None and run.worker.state == "done" and run.error is None


def test_interleaved_lane_schedules(tmp_path: Path) -> None:
    selected = (
        EXPLORATORY_SETTINGS
        if os.environ.get("BOOLEY_HYPOTHESIS_EXPLORATORY") == "1"
        else LANE_PR_SETTINGS
    )
    run_state_machine_as_test(lambda: LaneStepMachine(tmp_path), settings=selected)


def test_atomic_write_json_keeps_its_temporary_file_beside_the_named_directory(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real" / "sub"
    real.mkdir(parents=True)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    target = tmp_path / "link" / ".." / "record.json"
    execution_records.atomic_write_json(target, {"state": "waiting"})
    assert read_json(target) == {"state": "waiting"}
    assert [path.name for path in tmp_path.rglob(".record.json.*")] == []
