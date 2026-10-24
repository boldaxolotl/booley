"""Manifest-order bounded scheduling for Simulation Campaign work items."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path

from booley.flows.terminal_progress import progress_lane
from booley.runtime.execution_records import ExecutionId
from booley.runtime.job_slots import SlotToken
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    supervised_execution_scope,
)

from .capacity import ChildHeavyPermit, HeavyCapacity, HeavyCapacityError, LeaseLostError
from .child_protocol import ChildExecutionRegistry


@dataclass(frozen=True, slots=True)
class ScheduledAttempt:
    item: Mapping[str, object]
    attempt_id: str
    ordinal: int
    directory: Path
    collision_key: str


@dataclass(slots=True)
class _ChildRunState:
    terminal_cause: str = "completed"
    lease_id: str | None = None
    token: SlotToken | None = None


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


class BoundedCampaignScheduler:
    """Run ready items through one borrowed lane and bounded child lanes."""

    def __init__(
        self,
        capacity: HeavyCapacity,
        registry: ChildExecutionRegistry,
        *,
        allocate: Callable[[Mapping[str, object]], ScheduledAttempt],
        execute: Callable[[ScheduledAttempt, str | None, str | None], None],
        prepare_child: Callable[[ScheduledAttempt, str, str], None] | None = None,
    ) -> None:
        self._capacity = capacity
        self._registry = registry
        self._allocate = allocate
        self._execute = execute
        self._prepare_child = prepare_child or (lambda _attempt, _child, _digest: None)
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
        self._lanes_exited.clear()
        with self._capacity.outer_permit():
            lanes = self._start_lanes(ready, child_count)
            self._await_lanes(lanes, self._lanes_exited)
            self._stop.set()
        self._registry.recover_unretired(self._capacity.slot_store)
        if not self._errors.empty():
            raise self._errors.get()
        if not self._interruptions.empty():
            raise self._interruptions.get()

    def _start_lanes(
        self, ready: queue.Queue[Mapping[str, object]], child_count: int
    ) -> list[threading.Thread]:
        """Start the outer lane (first item taken in manifest order) and the child lanes."""
        first = ready.get_nowait() if child_count else None
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
                self._capacity.recover_lost_children()
            if deadline is None and (self._stop.is_set() or self._capacity.shutdown_requested):
                self._stop.set()
                self._capacity.cancel_waiters()
                deadline = time.monotonic() + self._capacity.shutdown_timeout_seconds
            if deadline is not None and time.monotonic() >= deadline:
                if exited.is_set():
                    # Every lane exited during the last wait: a clean shutdown. Raising
                    # here would also skip the caller's recover_unretired() net (#977).
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
            item = first
            while True:
                if item is None:
                    if self._stop.is_set() or self._capacity.shutdown_requested:
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

    def _run_lane_item(self, item: Mapping[str, object], outer: bool) -> bool:
        """Run one item; ``False`` retires the lane because the item failed."""
        try:
            self._run_item(item, outer)
        except LeaseLostError as exc:
            if self._capacity.shutdown_requested:
                self._fail_campaign(exc)
            else:
                # Only this lane's lease is gone. Its old token may still be a
                # cancelling holder, so the lane takes no further item; the others
                # drain the queue.
                self._interruptions.put(exc)
            return False
        except BaseException as exc:  # noqa: BLE001 -- ferry lane failures to owner
            self._fail_campaign(exc)
            return False
        return True

    def _fail_campaign(self, exc: BaseException) -> None:
        self._errors.put(exc)
        self._capacity.cancel_waiters(_worker_failure_cause(exc))
        self._stop.set()

    def _run_item(self, item: Mapping[str, object], outer: bool) -> None:
        with self._allocation_gate:
            attempt = self._allocate(item)
        with self._collision_permit(attempt.collision_key), self._lane_scope(attempt):
            if outer:
                scope = SupervisedExecutionScope(
                    None,
                    self._registry.project_data,
                    lambda: self._stop.is_set() or self._capacity.shutdown_requested,
                    self._capacity.cancellation_cause,
                )
                with supervised_execution_scope(scope):
                    self._execute(attempt, None, None)
                return
            self._run_child(item, attempt)

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

    def _run_child(self, item: Mapping[str, object], attempt: ScheduledAttempt) -> None:
        child_id = ExecutionId(uuid.uuid4().hex)
        prepared = self._registry.prepare(
            child_id,
            work_item_id=str(item["work_item_id"]),
            attempt_id=attempt.attempt_id,
            attempt_ordinal=attempt.ordinal,
            attempt_directory=attempt.directory,
            parent_execution_id=self._capacity_parent_id(),
        )
        state = _ChildRunState()
        try:
            self._prepare_child(attempt, str(child_id), prepared.entry_sha256)
            self._execute_child(item, attempt, child_id, prepared, state)
        except BaseException:
            if state.terminal_cause == "completed":
                state.terminal_cause = "campaign_error"
            self._registry.mark_terminal(prepared, state.terminal_cause)
            raise
        finally:
            token_absent = (
                self._capacity.token_absent(state.token)
                if state.token is not None
                else self._capacity.execution_token_absent(child_id)
            )
            if self._registry.is_terminal(child_id) and token_absent:
                self._registry.retire(
                    prepared,
                    lease_id=state.lease_id,
                    terminal_cause=state.terminal_cause,
                    token_absent=True,
                )

    def _execute_child(self, item, attempt, child_id, prepared, state) -> None:
        with self._capacity.child_permit(str(item["work_item_id"]), child_id) as permit:
            state.lease_id = permit.token.lease_id
            state.token = permit.token
            scope = SupervisedExecutionScope(
                child_id,
                self._registry.project_data,
                lambda: permit.lease_health.lost.is_set() or self._capacity.shutdown_requested,
                lambda: self._child_cancellation_cause(permit),
            )
            try:
                with supervised_execution_scope(scope):
                    self._execute(attempt, str(child_id), prepared.entry_sha256)
            except HeavyCapacityError as exc:
                from booley.flows.sim.execution.contract import PreSimScopeStoppedError

                if (
                    isinstance(exc.__cause__, PreSimScopeStoppedError)
                    and permit.lease_health.lost.is_set()
                ):
                    state.terminal_cause = "lease_lost"
                if permit.lease_health.lost.is_set() and not self._capacity.shutdown_requested:
                    raise LeaseLostError(
                        f"heavy lease lost for child execution {child_id}"
                    ) from exc
                raise
            if permit.lease_health.lost.is_set():
                state.terminal_cause = "lease_lost"
            self._registry.mark_terminal(prepared, state.terminal_cause)
        if state.token is not None and not self._capacity.token_absent(state.token):
            raise HeavyCapacityError(f"child token remained after terminal proof: {child_id}")

    def _child_cancellation_cause(self, permit: ChildHeavyPermit) -> str | None:
        if permit.lease_health.lost.is_set():
            return f"Job Slot lease lost: {permit.token.job_class}/{permit.token.path.name}"
        return self._capacity.cancellation_cause()

    def _capacity_parent_id(self) -> ExecutionId:
        return self._capacity.parent_execution_id


def _worker_failure_cause(exc: BaseException) -> str:
    return f"campaign stop requested after a work item failed ({type(exc).__name__})"


__all__ = ["BoundedCampaignScheduler", "CampaignSchedulingError", "ScheduledAttempt"]
