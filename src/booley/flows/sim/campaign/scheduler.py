"""Manifest-order bounded scheduling for Simulation Campaign work items."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path

from booley.flows.terminal_progress import progress_lane
from booley.runtime.supervised_execution import supervised_execution_scope

from .capacity import HeavyCapacity, LeaseLostError

_TURN_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class ScheduledAttempt:
    item: Mapping[str, object]
    attempt_id: str
    ordinal: int
    directory: Path
    collision_key: str


def _lane_label(attempt: ScheduledAttempt) -> str:
    """Name a lane by its test(s) and attempt, e.g. ``alpha/1a2b3c4d`` (no whitespace)."""
    selection = attempt.item.get("selection")
    names = selection.get("names") if isinstance(selection, Mapping) else None
    shown = [str(name) for name in names] if isinstance(names, list | tuple) else []
    if len(shown) > 3:
        shown = [*shown[:3], f"+{len(shown) - 3}"]
    test = ",".join(shown) or str(attempt.item.get("work_item_id", ""))
    short_attempt = attempt.attempt_id[:8]
    return "".join((f"{test}/{short_attempt}" if test else short_attempt).split())


class CampaignSchedulingError(RuntimeError):
    """A campaign-fatal worker failure stopped further admission."""


class _TurnHold:
    """One lane's turn to start an item; ``pass_on`` hands it over, once."""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._passed = False

    def pass_on(self) -> None:
        if not self._passed:
            self._passed = True
            self._lock.release()


class _StartTurn:
    """Lets one lane at a time take the next item and get its claim admitted.

    Items then start in manifest order, and the Campaign has at most one item
    claim waiting in the slot store at any time (a queued claim counts against
    every other Job's queue bound).
    """

    def __init__(self, stopped: Callable[[], bool]) -> None:
        self._lock = threading.Lock()
        self._stopped = stopped

    def take(self) -> _TurnHold | None:
        """Wait for the turn; ``None`` once the campaign stops."""
        while not self._lock.acquire(timeout=_TURN_POLL_SECONDS):
            if self._stopped():
                return None
        return _TurnHold(self._lock)


class BoundedCampaignScheduler:
    """Run ready items through one borrowed lane and bounded child lanes.

    Under memory admission (``capacity.items_admitted``) every lane, the outer
    one included, takes the start turn, then the next item and its claim: an
    item whose claim is ``None`` runs under the Flow's reservation and keeps the
    turn until it ends; otherwise the turn passes on once the claim is admitted,
    so the claims decide how many items run at once.
    """

    def __init__(
        self,
        capacity: HeavyCapacity,
        *,
        allocate: Callable[[Mapping[str, object]], ScheduledAttempt],
        execute: Callable[[ScheduledAttempt, str | None], None],
        prepare_child: Callable[[ScheduledAttempt, str], None] | None = None,
    ) -> None:
        self._capacity = capacity
        self._allocate = allocate
        self._execute = execute
        self._prepare_child = prepare_child or (lambda _attempt, _child: None)
        self._stop = threading.Event()
        self._errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
        # A lost child lease interrupts one lane's item without stopping the campaign.
        self._interruptions: queue.SimpleQueue[LeaseLostError] = queue.SimpleQueue()
        self._lane_gate = threading.Lock()
        self._live = 0
        self._lanes_exited = threading.Event()
        self._allocation_gate = threading.Lock()
        self._collision_condition = threading.Condition()
        self._active_collisions: set[str] = set()
        # Terminal progress is lane-scoped only while items really overlap.
        self._lanes_concurrent = False
        self._turn: _StartTurn | None = None

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
        self._turn = _StartTurn(self._stopped) if self._capacity.items_admitted else None
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
        """Start the outer lane (first item taken in manifest order) and the child lanes."""
        first = ready.get_nowait() if child_count and self._turn is None else None
        specs = [("outer", True, first)]
        specs.extend((f"child-{index + 1}", False, None) for index in range(child_count))
        self._live = len(specs)
        lanes: list[threading.Thread] = []
        try:
            for suffix, outer, first_item in specs:
                lane = self._thread(self._lane, (ready, outer, first_item), suffix)
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
            name=f"booley-campaign-{suffix}",
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
                raise CampaignSchedulingError(
                    f"campaign workers exceeded bounded shutdown: {alive}"
                )

    def _lane(
        self,
        ready: queue.Queue[Mapping[str, object]],
        outer: bool,
        first: Mapping[str, object] | None,
    ) -> None:
        """Run ``first`` then ready items until the queue drains or the campaign stops."""
        try:
            if self._turn is not None:
                while self._run_turn(ready, outer):
                    pass
                return
            item = first
            while True:
                if item is None:
                    if self._stopped():
                        return
                    try:
                        item = ready.get_nowait()
                    except queue.Empty:
                        return
                if not self._run_lane_item(item, outer):
                    return
                item = None
        finally:
            self._lane_exited()

    def _run_turn(self, ready: queue.Queue[Mapping[str, object]], outer: bool) -> bool:
        """Take the start turn, then the next item; ``False`` ends the lane (stopped, drained or failed)."""
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
            return self._run_lane_item(item, outer, hold)
        finally:
            hold.pass_on()

    def _run_lane_item(
        self, item: Mapping[str, object], outer: bool, hold: _TurnHold | None = None
    ) -> bool:
        """Run one item; ``False`` retires the lane because the item failed.

        A lost lease ends only its item. The outer lane takes the next item (its
        permit is a fresh claim, or the Flow's own); a child lane retires.
        """
        try:
            self._run_item(item, outer, hold)
        except LeaseLostError as exc:
            if self._capacity.shutdown_requested:
                self._fail_campaign(exc)
            else:
                # Only this item's lease is gone. A child lane's old token may still
                # be a cancelling holder, so it takes no further item; the others
                # drain the queue.
                self._interruptions.put(exc)
            return outer
        except BaseException as exc:  # noqa: BLE001 -- ferry lane failures to owner
            self._fail_campaign(exc)
            return False
        return True

    def _fail_campaign(self, exc: BaseException) -> None:
        self._errors.put(exc)
        self._capacity.cancel_waiters(_worker_failure_cause(exc))
        self._stop.set()

    def _run_item(
        self, item: Mapping[str, object], outer: bool, hold: _TurnHold | None = None
    ) -> None:
        """Run one item. Under memory admission (*hold* is the start turn) any lane asks
        for the item's claim and passes the turn on once it is admitted."""
        with self._allocation_gate:
            attempt = self._allocate(item)
        with self._collision_permit(attempt.collision_key), self._lane_scope(attempt):
            # The outer lane (and every lane under memory admission) runs under the
            # Flow's claim, unless the item takes its own.
            asks = outer or hold is not None
            memory = self._capacity.work_item_claim(item) if asks else None
            if asks and memory is None:
                with supervised_execution_scope(self._capacity.outer_scope(self._stop.is_set)):
                    self._execute(attempt, None)
                return
            with (
                self._capacity.child_permit(
                    str(item["work_item_id"]),
                    attempt_id=attempt.attempt_id,
                    attempt_ordinal=attempt.ordinal,
                    attempt_directory=attempt.directory,
                    publish=lambda child: self._prepare_child(attempt, str(child)),
                    memory=memory,
                ) as permit,
                supervised_execution_scope(permit.scope),
            ):
                if hold is not None:
                    hold.pass_on()
                self._execute(attempt, str(permit.child_execution_id))

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
                raise CampaignSchedulingError("campaign shutdown requested")
            self._active_collisions.add(key)
        try:
            yield
        finally:
            with self._collision_condition:
                self._active_collisions.discard(key)
                self._collision_condition.notify_all()


def _worker_failure_cause(exc: BaseException) -> str:
    return f"campaign stop requested after a work item failed ({type(exc).__name__})"


__all__ = ["BoundedCampaignScheduler", "CampaignSchedulingError", "ScheduledAttempt"]
