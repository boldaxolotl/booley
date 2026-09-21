"""Manifest-order bounded scheduling for Simulation Campaign work items."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.execution_records import ExecutionId
from booley.runtime.job_slots import SlotToken
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    supervised_execution_scope,
)

from .capacity import HeavyCapacity, HeavyCapacityError
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
    ) -> None:
        self._capacity = capacity
        self._registry = registry
        self._allocate = allocate
        self._execute = execute
        self._stop = threading.Event()
        self._errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
        self._allocation_gate = threading.Lock()
        self._collision_condition = threading.Condition()
        self._active_collisions: set[str] = set()

    def run(self, items: Sequence[Mapping[str, object]]) -> None:
        if not items:
            return
        ready: queue.Queue[Mapping[str, object]] = queue.Queue()
        for item in items:
            ready.put(item)
        child_count = max(0, self._capacity.max_lanes - 1) if self._capacity.managed else 0
        with self._capacity.outer_permit():
            if child_count == 0:
                self._run_serial(ready)
                return
            first = ready.get_nowait()
            workers = [
                threading.Thread(
                    target=self._outer_worker,
                    args=(first, ready),
                    name="booley-campaign-outer",
                )
            ]
            workers.extend(
                threading.Thread(
                    target=self._worker,
                    args=(ready, False),
                    name=f"booley-campaign-child-{index + 1}",
                )
                for index in range(child_count)
            )
            for worker in workers:
                worker.start()
            monitor = threading.Thread(target=self._monitor_cancellation, daemon=True)
            monitor.start()
            self._join_workers(workers)
            self._stop.set()
            monitor.join(timeout=1)
        if not self._errors.empty():
            raise self._errors.get()

    def _run_serial(self, ready: queue.Queue[Mapping[str, object]]) -> None:
        while (
            not ready.empty()
            and not self._stop.is_set()
            and not self._capacity.shutdown_requested
        ):
            item = ready.get_nowait()
            try:
                self._run_item(item, True)
            finally:
                ready.task_done()

    def _monitor_cancellation(self) -> None:
        while not self._stop.wait(0.02):
            if self._capacity.shutdown_requested:
                self._capacity.cancel_waiters()
                return

    def _join_workers(self, workers: list[threading.Thread]) -> None:
        deadline = time.monotonic() + self._capacity.shutdown_timeout_seconds
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [worker.name for worker in workers if worker.is_alive()]
        if alive:
            self._capacity.cancel_waiters()
            self._stop.set()
            raise CampaignSchedulingError(
                "campaign workers exceeded bounded shutdown: " + ", ".join(alive)
            )

    def _worker(self, ready: queue.Queue[Mapping[str, object]], outer: bool) -> None:
        while not self._stop.is_set() and not self._capacity.shutdown_requested:
            try:
                item = ready.get_nowait()
            except queue.Empty:
                return
            try:
                self._run_item(item, outer)
            except BaseException as exc:  # noqa: BLE001 -- ferry worker failures to owner
                self._errors.put(exc)
                self._stop.set()
                self._capacity.cancel_waiters()
                return
            finally:
                ready.task_done()

    def _outer_worker(
        self, first: Mapping[str, object], ready: queue.Queue[Mapping[str, object]]
    ) -> None:
        try:
            self._run_item(first, True)
        except BaseException as exc:  # noqa: BLE001 -- ferry worker failures to owner
            self._errors.put(exc)
            self._stop.set()
            self._capacity.cancel_waiters()
        finally:
            ready.task_done()
        if not self._stop.is_set():
            self._worker(ready, True)

    def _run_item(self, item: Mapping[str, object], outer: bool) -> None:
        with self._allocation_gate:
            attempt = self._allocate(item)
        with self._collision_permit(attempt.collision_key):
            if outer:
                self._execute(attempt, None, None)
                return
            self._run_child(item, attempt)

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

    def _run_child(
        self, item: Mapping[str, object], attempt: ScheduledAttempt
    ) -> None:
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
            self._execute_child(item, attempt, child_id, prepared, state)
        except Exception:
            if state.terminal_cause == "completed":
                state.terminal_cause = "campaign_error"
            self._registry.mark_terminal(prepared, state.terminal_cause)
            raise
        finally:
            if (
                self._registry.is_terminal(child_id)
                and state.token is not None
                and self._capacity.token_absent(state.token)
            ):
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
                lambda: (
                    permit.lease_health.lost.is_set()
                    or self._capacity.shutdown_requested
                ),
            )
            with supervised_execution_scope(scope):
                self._execute(attempt, str(child_id), prepared.entry_sha256)
            if permit.lease_health.lost.is_set():
                state.terminal_cause = "lease_lost"
            self._registry.mark_terminal(prepared, state.terminal_cause)
        if state.token is not None and not self._capacity.token_absent(state.token):
            raise HeavyCapacityError(
                f"child token remained after terminal proof: {child_id}"
            )

    def _capacity_parent_id(self) -> str:
        return self._capacity.parent_execution_id


__all__ = ["BoundedCampaignScheduler", "CampaignSchedulingError", "ScheduledAttempt"]
