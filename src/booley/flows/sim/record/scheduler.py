"""Manifest-order bounded scheduling for Simulation Record work items."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from contextvars import copy_context
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from booley.flows.sim.build_session import SimulationBuildLeaseTimeoutError
from booley.flows.terminal_progress import progress_lane
from booley.runtime import oom_priority
from booley.runtime.endpoint_execution import OOM_REQUEUE_REASON, job_attempt
from booley.runtime.job_slots import MemoryClaim, short_test_list
from booley.runtime.supervised_execution import supervised_execution_scope

from .capacity import ChildPermit, LeaseLostError, SimulationRecordCapacity
from .child_protocol import PreparedChild

_TURN_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class ScheduledAttempt:
    item: Mapping[str, object]
    attempt_id: str
    ordinal: int
    directory: Path
    collision_key: str
    #: What the attempt does once its shared build is ready; set only for a first item
    #: that runs under the Flow's claim, so it can take a claim of its own.
    build_settled: Callable[[], None] | None = None


def _item_tests(item: Mapping[str, object]) -> tuple[str, ...]:
    """The test names an item selects (none for a default or unfiltered selection)."""
    selection = item.get("selection")
    names = selection.get("names") if isinstance(selection, Mapping) else None
    return tuple(str(name) for name in names) if isinstance(names, list | tuple) else ()


def work_item_label(item: Mapping[str, object]) -> str:
    """Name a work item by its test(s), else its ID."""
    return short_test_list(_item_tests(item)) or str(item.get("work_item_id", ""))


def _lane_label(attempt: ScheduledAttempt) -> str:
    """Name a lane by its test(s) and attempt, e.g. ``alpha/1a2b3c4d`` (no whitespace)."""
    test = work_item_label(attempt.item)
    short_attempt = attempt.attempt_id[:8]
    return "".join((f"{test}/{short_attempt}" if test else short_attempt).split())


class SimulationRecordSchedulingError(RuntimeError):
    """A record-fatal worker failure stopped further admission."""


class _TurnHold:
    """One lane's turn to start an item; ``pass_on`` hands it over, once."""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._passed = False

    def pass_on(self) -> None:
        """Hand the turn to the next lane; a second call does nothing."""
        if not self._passed:
            self._passed = True
            self._lock.release()


class _StartTurn:
    """Lets one lane at a time take the next item and get its claim admitted.

    Items then start in manifest order, and the Simulation Record has at most one item
    claim waiting in the slot store at any time.
    """

    def __init__(self, stop: threading.Event) -> None:
        self._lock = threading.Lock()
        self._stop = stop

    def take(self) -> _TurnHold | None:
        """Wait for the turn; ``None`` once the record stops.

        Waiters check only the stop event, which the supervisor sets within moments of
        any shutdown: a full cancellation check would rescan the slot store per lane.
        """
        while not self._lock.acquire(timeout=_TURN_POLL_SECONDS):
            if self._stop.is_set():
                return None
        return _TurnHold(self._lock)


class BoundedSimulationRecordScheduler:
    """Run ready items through one borrowed lane and bounded child lanes.

    Under memory admission (``capacity.items_admitted``) every lane, the outer
    one included, takes the start turn, then the next item and its claim: an
    item whose claim is ``None`` runs under the Flow's reservation and keeps the
    turn until its shared build settles, then takes over the Flow's reservation and
    queue place as its own claim and passes the turn on (it keeps the turn until it
    ends when its build never settles); otherwise the turn passes on once the claim
    is admitted, so the claims decide how many items run at once.

    An item with a claim of its own runs as its own Job attempt: an out-of-memory
    kill of its commands stops only that item. When its executor reports the
    attempt as unpublished (``True``), the item goes to the back of the queue and
    runs once more, alone and unarmed (ADR 0087).

    An item whose wait for the Target slot lease times out also ends alone and
    without a result, so a later resume runs it; its lane takes the next item.
    """

    def __init__(
        self,
        capacity: SimulationRecordCapacity,
        *,
        allocate: Callable[[Mapping[str, object]], ScheduledAttempt],
        execute: Callable[[ScheduledAttempt, str | None], bool | None],
        prepare_child: Callable[[ScheduledAttempt, str], None] | None = None,
    ) -> None:
        self._capacity = capacity
        self._allocate = allocate
        self._execute = execute
        self._prepare_child = prepare_child or (lambda _attempt, _child: None)
        self._stop = threading.Event()
        self._errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
        # A lost child lease or a lease wait timeout interrupts one lane's item without stopping the record.
        self._interruptions: queue.SimpleQueue[RuntimeError] = queue.SimpleQueue()
        self._lane_gate = threading.Lock()
        self._live = 0
        self._lanes_exited = threading.Event()
        self._allocation_gate = threading.Lock()
        self._collision_condition = threading.Condition()
        self._active_collisions: set[str] = set()
        # Terminal progress is lane-scoped only while items really overlap.
        self._lanes_concurrent = False
        self._turn: _StartTurn | None = None
        # Work item IDs already requeued after an out-of-memory kill: never requeued again.
        self._requeued: set[str] = set()
        self._requeued_gate = threading.Lock()

    def _stopped(self) -> bool:
        return self._stop.is_set() or self._capacity.shutdown_requested

    def run(self, items: Sequence[Mapping[str, object]]) -> None:
        if not items:
            return
        ready: queue.Queue[Mapping[str, object]] = queue.Queue()
        for item in items:
            ready.put(item)
        child_count = (
            min(len(items) - 1, max(0, self._capacity.max_lanes - 1))
            if self._capacity.managed
            else 0
        )
        self._lanes_concurrent = child_count > 0
        self._turn = _StartTurn(self._stop) if self._capacity.items_admitted else None
        self._lanes_exited.clear()
        with self._capacity.outer_permit():
            lanes = self._start_lanes(ready, child_count)
            self._await_lanes(lanes, self._lanes_exited)
            self._stop.set()
        if not self._errors.empty():
            raise self._errors.get()
        if not self._interruptions.empty():
            raise self._interruptions.get()

    def _start_lanes(
        self, ready: queue.Queue[Mapping[str, object]], child_count: int
    ) -> list[threading.Thread]:
        """Start the outer lane and the child lanes.

        Under memory admission every lane takes its items through the start turn;
        otherwise the outer lane runs them one at a time.
        """
        specs = [("outer", True)]
        specs.extend((f"child-{index + 1}", False) for index in range(child_count))
        self._live = len(specs)
        lanes: list[threading.Thread] = []
        try:
            for suffix, outer in specs:
                lane = self._thread(self._lane, (ready, outer), suffix)
                lane.start()
                lanes.append(lane)
        except BaseException:
            # Lanes that never started cannot report their exit.
            for _ in range(len(specs) - len(lanes)):
                self._lane_exited()
            self._stop.set()
            self._capacity.cancel_waiters()
            self._await_lanes(lanes, self._lanes_exited)
            raise
        return lanes

    @staticmethod
    def _thread(target, args, suffix: str) -> threading.Thread:
        return threading.Thread(
            target=copy_context().run,
            args=(target, *args),
            name=f"booley-record-{suffix}",
            daemon=True,
        )

    def _lane_exited(self) -> None:
        with self._lane_gate:
            self._live -= 1
            if self._live == 0:
                self._lanes_exited.set()

    def _await_lanes(self, lanes: Sequence[threading.Thread], exited: threading.Event) -> None:
        """Supervise the pool until every lane exits, or bound the wait after cancellation.

        This owner loop is the pool's only supervisor: it withdraws queued claims on
        cancellation and starts exact recovery of a child whose lease was lost.
        """
        deadline: float | None = None
        while not exited.wait(0.02):
            if self._capacity.managed:
                try:
                    self._capacity.recover_lost_children()
                except Exception as exc:  # noqa: BLE001 -- keep the supervisor alive
                    # A failed recovery (for example a file error) must still stop the
                    # pool through the bounded-shutdown path below; leaving the loop
                    # here would free the outer slot under running lanes.
                    self._errors.put(exc)
                    self._stop.set()
            if deadline is None and (self._stop.is_set() or self._capacity.shutdown_requested):
                self._stop.set()
                self._capacity.cancel_waiters()
                deadline = time.monotonic() + self._capacity.shutdown_timeout_seconds
            if deadline is not None and time.monotonic() >= deadline:
                if exited.is_set():
                    # Every lane exited during the last wait: a clean shutdown.
                    return
                self._capacity.cancel_waiters()
                self._capacity.hold_outer_until(exited)
                alive = ", ".join(lane.name for lane in lanes if lane.is_alive())
                raise SimulationRecordSchedulingError(
                    f"record workers exceeded bounded shutdown: {alive}"
                )

    def _lane(self, ready: queue.Queue[Mapping[str, object]], outer: bool) -> None:
        """Run ready items until the queue drains or the record stops."""
        try:
            if self._turn is not None:
                while self._run_turn(ready, outer):
                    pass
                return
            while not self._stopped():
                try:
                    item = ready.get_nowait()
                except queue.Empty:
                    return
                if not self._run_lane_item(item, outer, ready):
                    return
        finally:
            self._lane_exited()

    def _run_turn(self, ready: queue.Queue[Mapping[str, object]], outer: bool) -> bool:
        """Take the start turn, then the next item.

        ``False`` ends the lane: the record stopped, the queue drained or the item failed.
        """
        assert self._turn is not None
        hold = self._turn.take()
        if hold is None:
            return False  # stopped while waiting: the recorded error stays the real cause
        try:
            if self._stopped():
                return False
            try:
                item = ready.get_nowait()
            except queue.Empty:
                return False
            return self._run_lane_item(item, outer, ready, hold)
        finally:
            hold.pass_on()

    def _run_lane_item(
        self,
        item: Mapping[str, object],
        outer: bool,
        ready: queue.Queue[Mapping[str, object]],
        hold: _TurnHold | None = None,
    ) -> bool:
        """Run one item; ``False`` retires the lane because the item failed.

        A lost lease ends only its item. The outer lane takes the next item (its
        permit is a fresh claim, or the Flow's own); a child lane retires. An item
        killed by the out-of-memory killer goes to the back of *ready*, so this
        lane goes on even when every other lane already left.
        """
        try:
            if self._run_item(item, hold):
                with self._requeued_gate:
                    self._requeued.add(str(item["work_item_id"]))
                ready.put(item)
        except LeaseLostError as exc:
            if self._capacity.shutdown_requested:
                self._fail_simulation_record(exc)
            else:
                # Only this item's lease is gone. A child lane's old token may still
                # be a cancelling holder, so it takes no further item; the others
                # drain the queue.
                self._interruptions.put(exc)
            return outer
        except SimulationBuildLeaseTimeoutError as exc:
            # Only this item ends, with no result; a later resume runs it (#4843).
            self._interruptions.put(exc)
            return True
        except BaseException as exc:  # noqa: BLE001 -- ferry lane failures to owner
            self._fail_simulation_record(exc)
            return False
        return True

    def _fail_simulation_record(self, exc: BaseException) -> None:
        self._errors.put(exc)
        self._capacity.cancel_waiters(_worker_failure_cause(exc))
        self._stop.set()

    def _run_item(self, item: Mapping[str, object], hold: _TurnHold | None = None) -> bool:
        """Run one item; ``True`` when its attempt was OOM-killed and must run again.

        Under memory admission (*hold* is the start turn) the item's claim is
        admitted before the turn passes on.
        """
        with self._requeued_gate:
            requeued = item.get("work_item_id") in self._requeued
        with self._allocation_gate:
            attempt = self._allocate(item)
        with self._collision_permit(attempt.collision_key), self._lane_scope(attempt):
            # An item with no claim of its own runs under the Flow's claim.
            memory = self._capacity.work_item_claim(item, alone=requeued)
            if memory is None and hold is None:
                with supervised_execution_scope(self._capacity.outer_scope(self._stop.is_set)):
                    self._execute(attempt, None)
                return False
            if memory is None:
                assert hold is not None
                killed = self._run_under_flow_claim(attempt, hold)
            else:
                killed = self._run_own_claim(attempt, memory, hold, requeued=requeued)
        if killed:
            # The claim is released: the item queues again and takes the whole budget.
            print(
                f"[requeue] {_lane_label(attempt)}: {OOM_REQUEUE_REASON}; it runs again alone",
                flush=True,
            )
        return killed

    def _run_own_claim(
        self,
        attempt: ScheduledAttempt,
        memory: MemoryClaim,
        hold: _TurnHold | None,
        *,
        requeued: bool,
    ) -> bool:
        """Run an item on a claim of its own; ``True`` when it was OOM-killed."""
        with (
            self._capacity.child_permit(
                str(attempt.item["work_item_id"]),
                attempt_id=attempt.attempt_id,
                attempt_ordinal=attempt.ordinal,
                attempt_directory=attempt.directory,
                publish=lambda child: self._prepare_child(attempt, str(child)),
                memory=memory,
                queue_reason=OOM_REQUEUE_REASON if requeued else None,
                tests=_item_tests(attempt.item),
            ) as permit,
            supervised_execution_scope(permit.scope),
            self._own_job_attempt(permit, requeued=requeued),
        ):
            if hold is not None:
                hold.pass_on()
            return bool(self._execute(attempt, str(permit.child_execution_id)))

    def _run_under_flow_claim(self, attempt: ScheduledAttempt, hold: _TurnHold) -> bool:
        """Run an item on the Flow's claim until its shared build settles; ``True`` when killed.

        The item's lane is prepared before the build, so its attempt names it. When the
        build settles, the executor calls ``build_settled``: the item takes a claim of its
        own that takes over the Flow's reservation and queue place, passes the turn on and
        runs as its own Job attempt. Those contexts live in *own*, which stays open until the
        attempt returns: the coordinator decides on a requeue after the executor
        returns, and it reads the item's own Job attempt. A kill during the build is the
        Flow's and is not reported here.
        """
        work_item_id = str(attempt.item["work_item_id"])
        taken: list[bool] = []
        with (
            self._capacity.child_lane(
                work_item_id,
                attempt_id=attempt.attempt_id,
                attempt_ordinal=attempt.ordinal,
                attempt_directory=attempt.directory,
                publish=lambda child: self._prepare_child(attempt, str(child)),
            ) as lane,
            supervised_execution_scope(self._capacity.outer_scope(self._stop.is_set)),
            ExitStack() as own,
        ):
            settled = partial(self._take_own_claim, own, lane, attempt, hold, taken)
            killed = self._execute(replace(attempt, build_settled=settled), str(lane.execution_id))
        return bool(killed) and bool(taken)

    def _take_own_claim(
        self,
        own: ExitStack,
        lane: PreparedChild,
        attempt: ScheduledAttempt,
        hold: _TurnHold,
        taken: list[bool],
    ) -> None:
        """Take over the Flow's claim as the item's own, entering its contexts into *own*.

        Nothing happens while another build of the Record is still unsettled: the item
        stays under the Flow's claim until it returns.
        """
        memory = self._capacity.work_item_claim(attempt.item)
        if memory is None:
            return
        work_item_id = str(attempt.item["work_item_id"])
        permit = own.enter_context(
            self._capacity.admitted_lane(
                lane, work_item_id, memory=memory, tests=_item_tests(attempt.item)
            )
        )
        own.enter_context(supervised_execution_scope(permit.scope))
        taken.append(True)
        hold.pass_on()
        # An item under the Flow's claim was never killed under its own claim: it is armed.
        own.enter_context(self._own_job_attempt(permit, requeued=False))

    @staticmethod
    @contextmanager
    def _own_job_attempt(permit: ChildPermit, *, requeued: bool):
        """Run an item holding its own claim as its own Job attempt.

        Its processes rank as their own Job. The first attempt is armed: a kill
        stops only this item. The requeued attempt is the last one and never armed.
        """
        with (
            oom_priority.work_item_processes(permit.token.lease_id, permit.scope.processes),
            job_attempt(stop_on_oom=not requeued, work_item=True),
        ):
            yield

    def _lane_scope(self, attempt: ScheduledAttempt) -> AbstractContextManager[None]:
        """Scope terminal progress to this attempt when lanes run concurrently."""
        if not self._lanes_concurrent:
            return nullcontext()
        return progress_lane(_lane_label(attempt))

    @contextmanager
    def _collision_permit(self, key: str):
        with self._collision_condition:
            while key in self._active_collisions and not self._stop.is_set():
                self._collision_condition.wait(timeout=0.05)
            if self._stop.is_set():
                raise SimulationRecordSchedulingError("record shutdown requested")
            self._active_collisions.add(key)
        try:
            yield
        finally:
            with self._collision_condition:
                self._active_collisions.discard(key)
                self._collision_condition.notify_all()


def _worker_failure_cause(exc: BaseException) -> str:
    return f"record stop requested after a work item failed ({type(exc).__name__})"


__all__ = [
    "BoundedSimulationRecordScheduler",
    "ScheduledAttempt",
    "SimulationRecordSchedulingError",
]
