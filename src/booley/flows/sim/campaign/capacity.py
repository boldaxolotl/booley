"""Borrow one admitted heavy lane and acquire bounded per-item child lanes."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from booley.flows.endpoint_admission import AdmissionContext
from booley.runtime.execution_records import ExecutionId
from booley.runtime.job_slots import (
    CLASS_HEAVY,
    ClaimAbortedError,
    LeaseHealth,
    SlotToken,
)


class HeavyCapacityError(RuntimeError):
    """The campaign can no longer safely use heavy capacity."""


@dataclass(frozen=True, slots=True)
class HeavyPermit:
    """The borrowed outer lane; it is never released by this module."""

    execution_id: str


@dataclass(frozen=True, slots=True)
class ChildHeavyPermit:
    """One exact child lease acquired for one Simulation Work Item."""

    work_item_id: str
    child_execution_id: ExecutionId
    token: SlotToken
    lease_health: LeaseHealth


class HeavyCapacity:
    """Deep adapter from an admitted endpoint context to campaign permits."""

    def __init__(
        self,
        admission: AdmissionContext,
        *,
        terminal_proof: Callable[[ExecutionId], bool],
        recover_child: Callable[[ExecutionId], bool],
    ) -> None:
        self._admission = admission
        self._terminal_proof = terminal_proof
        self._recover_child = recover_child
        self._shutdown = threading.Event()
        self._outer_entered = False
        self._waiting: dict[str, SlotToken] = {}
        self._gate = threading.Lock()

    @property
    def max_lanes(self) -> int:
        return self._admission.max_heavy

    @property
    def managed(self) -> bool:
        return self._admission.mode == "managed"

    @property
    def parent_execution_id(self) -> str:
        return self._admission.execution_id

    @property
    def shutdown_requested(self) -> bool:
        return self._cancelled()

    @property
    def shutdown_timeout_seconds(self) -> float:
        return self._admission.timeout_seconds or 30.0

    @contextmanager
    def outer_permit(self) -> Iterator[HeavyPermit]:
        """Lend the already-held outer permit exactly once."""
        if self._outer_entered:
            raise HeavyCapacityError("outer campaign permit may be borrowed exactly once")
        self._outer_entered = True
        if self.managed and (
            self._admission.slot_store is None or self._admission.outer_token is None
        ):
            raise HeavyCapacityError("managed admission is missing its outer heavy token")
        yield HeavyPermit(self._admission.execution_id)

    @contextmanager
    def child_permit(
        self, work_item_id: str, child_execution_id: ExecutionId
    ) -> Iterator[ChildHeavyPermit]:
        """Acquire and safely retire one exact child heavy claim."""
        store = self._admission.slot_store
        if not self.managed or store is None:
            raise HeavyCapacityError("unmanaged campaigns cannot acquire child permits")
        if self._cancelled():
            raise ClaimAbortedError("campaign shutdown requested")
        token = store.acquire(
            CLASS_HEAVY,
            pid=os.getpid(),
            argv=list(sys.argv),
            role=self._admission.role,
            timeout_s=self._admission.timeout_seconds,
            should_abort=self._cancelled,
            execution_id=child_execution_id,
        )
        with self._gate:
            self._waiting[token.lease_id] = token
        permit = ChildHeavyPermit(
            work_item_id, child_execution_id, token, token.lease_health
        )
        watch_stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_lease,
            args=(permit, watch_stop),
            name=f"booley-campaign-lease-{token.lease_id[:8]}",
            daemon=True,
        )
        watcher.start()
        try:
            yield permit
            if permit.lease_health.lost.is_set():
                raise HeavyCapacityError(
                    f"heavy lease lost for child execution {child_execution_id}"
                )
            if self._cancelled():
                raise ClaimAbortedError("campaign cancellation requested")
        finally:
            watch_stop.set()
            watcher.join(timeout=1)
            with self._gate:
                self._waiting.pop(token.lease_id, None)
            self._release_after_terminal(store, permit)

    def cancel_waiters(self) -> None:
        """Stop admission and withdraw only this campaign's queued claims."""
        self._shutdown.set()
        store = self._admission.slot_store
        if store is None:
            return
        with self._gate:
            waiting = tuple(self._waiting.values())
        for token in waiting:
            if store.cancel_waiter_token(token):
                continue
            if token.execution_id is not None:
                store.begin_token_recovery(token)
                self._recover_child(token.execution_id)

    def token_absent(self, token: SlotToken) -> bool:
        store = self._admission.slot_store
        return store is not None and store.token_absent(token)

    def _cancelled(self) -> bool:
        return self._shutdown.is_set() or self._admission.cancellation()

    def _watch_lease(
        self, permit: ChildHeavyPermit, stop: threading.Event
    ) -> None:
        while not stop.wait(0.01):
            if not permit.lease_health.lost.is_set():
                continue
            self._shutdown.set()
            store = self._admission.slot_store
            if store is not None:
                store.begin_token_recovery(permit.token)
            self._recover_child(permit.child_execution_id)
            return

    def _release_after_terminal(self, store, permit: ChildHeavyPermit) -> None:
        execution_id = permit.child_execution_id
        if not self._terminal_proof(execution_id):
            store.begin_token_recovery(permit.token)
            self._recover_child(execution_id)
        if self._terminal_proof(execution_id):
            store.release(permit.token)


__all__ = [
    "ChildHeavyPermit",
    "HeavyCapacity",
    "HeavyCapacityError",
    "HeavyPermit",
]
