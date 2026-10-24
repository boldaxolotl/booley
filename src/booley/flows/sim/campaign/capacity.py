"""Borrow the admitted outer heavy lane and acquire bounded per-item child lanes."""

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

_MAX_CHILD_ACQUIRE_POLL_SECONDS = 0.01
_SHUTDOWN_POLL_SLICES = 10


class HeavyCapacityError(RuntimeError):
    """The campaign can no longer safely use heavy capacity."""


class LeaseLostError(HeavyCapacityError):
    """One child lane lost its lease; the other lanes can still finish."""


@dataclass(frozen=True, slots=True)
class HeavyPermit:
    """The borrowed outer lane; it is never released by this module."""

    execution_id: ExecutionId | None


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
        # Child permits inside their work, and those whose lost lease was handled.
        self._held: dict[str, ChildHeavyPermit] = {}
        self._recovered_lost: set[str] = set()
        self._gate = threading.Lock()
        self._cause: str | None = None

    @property
    def max_lanes(self) -> int:
        return self._admission.max_heavy

    @property
    def managed(self) -> bool:
        return self._admission.mode == "managed"

    @property
    def parent_execution_id(self) -> ExecutionId:
        return self._admission.parent_execution_id

    @property
    def shutdown_requested(self) -> bool:
        return self._cancelled()

    @property
    def shutdown_timeout_seconds(self) -> float:
        return self._admission.timeout_seconds or 30.0

    @property
    def slot_store(self):
        """Return the managed store used to prove child-token retirement."""
        return self._admission.slot_store

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
        token = self._acquire_child(store, child_execution_id)
        permit = ChildHeavyPermit(work_item_id, child_execution_id, token, token.lease_health)
        with self._gate:
            self._held[token.lease_id] = permit
        try:
            yield permit
            if permit.lease_health.lost.is_set() or not store.renew(permit.token):
                raise LeaseLostError(f"heavy lease lost for child execution {child_execution_id}")
            if self._cancelled():
                raise ClaimAbortedError("campaign cancellation requested")
        finally:
            with self._gate:
                self._waiting.pop(token.lease_id, None)
                self._held.pop(token.lease_id, None)
                self._recovered_lost.discard(token.lease_id)
            self._release_after_terminal(store, permit)

    def _acquire_child(self, store, child_execution_id: ExecutionId) -> SlotToken:
        submitted: list[SlotToken] = []

        def register(token: SlotToken) -> None:
            with self._gate:
                self._waiting[token.lease_id] = token
            submitted.append(token)

        try:
            token = store.acquire(
                CLASS_HEAVY,
                pid=os.getpid(),
                argv=list(sys.argv),
                role=self._admission.role,
                timeout_s=self._admission.timeout_seconds,
                poll_interval=min(
                    _MAX_CHILD_ACQUIRE_POLL_SECONDS,
                    self.shutdown_timeout_seconds / _SHUTDOWN_POLL_SLICES,
                ),
                should_abort=self._cancelled,
                execution_id=child_execution_id,
                on_submitted=register,
            )
        except BaseException:
            with self._gate:
                for queued in submitted:
                    self._waiting.pop(queued.lease_id, None)
            raise
        return token

    def cancellation_cause(self) -> str | None:
        """Why the campaign was first cancelled, or ``None`` while it was not."""
        with self._gate:
            return self._cause

    def _latch_cause(self, cause: str | None) -> None:
        """Record the first cancellation cause; later triggers never overwrite it."""
        if cause is None:
            return
        with self._gate:
            if self._cause is None:
                self._cause = cause

    def cancel_waiters(self, cause: str | None = None) -> None:
        """Stop admission and withdraw only this campaign's queued claims.

        ``cause`` is latched before shutdown is signalled; calls without one
        never mask the cause that triggered them.
        """
        self._latch_cause(cause)
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

    def execution_token_absent(self, execution_id: ExecutionId) -> bool:
        """Prove that no holder or waiter remains for one exact child."""
        store = self._admission.slot_store
        if store is None:
            return True
        return store.execution_token_absent(CLASS_HEAVY, execution_id)

    def hold_outer_until(self, exited: threading.Event) -> None:
        """Keep the outer slot held until ``exited`` is set (the last lane left)."""
        store = self._admission.slot_store
        token = self._admission.outer_token
        if store is None or not isinstance(token, SlotToken):
            return
        store.defer_release_until(token, exited)

    def recover_lost_children(self) -> None:
        """Start exact recovery of each held child whose lease was lost, once.

        Called from the scheduler's await loop, so recovery begins while the lane's
        work is still running. It never stops the campaign: a lost child lease is
        scoped to that lane's item.
        """
        store = self._admission.slot_store
        with self._gate:
            lost = [
                permit
                for lease_id, permit in self._held.items()
                if permit.lease_health.lost.is_set() and lease_id not in self._recovered_lost
            ]
            self._recovered_lost.update(permit.token.lease_id for permit in lost)
        for permit in lost:
            if store is not None:
                store.begin_token_recovery(permit.token)
            self._recover_child(permit.child_execution_id)

    def _cancelled(self) -> bool:
        if self._shutdown.is_set():
            return True
        if not self._admission.cancellation():
            return False
        self._latch_cause(
            self._admission.cancellation_cause() or "admission cancellation requested"
        )
        return True

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
    "LeaseLostError",
]
