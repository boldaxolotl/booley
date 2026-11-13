"""Borrow the admitted outer heavy lane and acquire bounded per-item child lanes."""

from __future__ import annotations

import os
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from booley.flows.endpoint_admission import AdmissionContext
from booley.runtime.execution_records import ExecutionId
from booley.runtime.job_slots import (
    CLASS_HEAVY,
    ClaimAbortedError,
    LeaseHealth,
    SlotToken,
)
from booley.runtime.supervised_execution import SupervisedExecutionScope

from .child_protocol import ChildExecutionRegistry, PreparedChild
from .model import SimulationCampaignManifest

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
    scope: SupervisedExecutionScope


class HeavyCapacity:
    """Deep adapter from an admitted endpoint context to campaign permits."""

    def __init__(
        self, admission: AdmissionContext, manifest: SimulationCampaignManifest, project_data: Path
    ) -> None:
        self._admission = admission
        self._lanes = ChildExecutionRegistry.from_project_data(
            manifest, project_data, booley_dir=project_data
        )
        self._shutdown = threading.Event()
        self._outer_entered = False
        self._waiting: dict[str, SlotToken] = {}
        # Child permits inside their work, and those whose lost lease was handled.
        self._held: dict[str, ChildHeavyPermit] = {}
        self._recovered_lost: set[str] = set()
        # Lease IDs proven held after the child's work, so the child may be
        # published terminal. A later token loss is then normal reaping.
        self._confirmed: set[str] = set()
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

    def outer_scope(self, stopped: Callable[[], bool]) -> SupervisedExecutionScope:
        """The supervised scope of the outer lane's items."""
        return SupervisedExecutionScope(
            None,
            self._lanes.project_data,
            lambda: stopped() or self.shutdown_requested,
            self.cancellation_cause,
        )

    @contextmanager
    def child_permit(
        self,
        work_item_id: str,
        *,
        attempt_id: str,
        attempt_ordinal: int,
        attempt_directory: Path,
        publish: Callable[[ExecutionId], None],
    ) -> Iterator[ChildHeavyPermit]:
        """Give one lane its identity and slot, retiring both on exit.

        ``publish`` gets the lane ID before the slot is requested.
        """
        store = self._admission.slot_store
        if not self.managed or store is None:
            raise HeavyCapacityError("unmanaged campaigns cannot acquire child permits")
        child_id = ExecutionId(uuid.uuid4().hex)
        prepared = self._lanes.prepare(
            child_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            attempt_ordinal=attempt_ordinal,
            attempt_directory=attempt_directory,
            parent_execution_id=self.parent_execution_id,
        )
        try:
            publish(child_id)
            if self._cancelled():
                raise ClaimAbortedError("campaign shutdown requested")
            token = self._acquire_child(store, child_id)
        except BaseException:
            self._lanes.mark_terminal(prepared, "record_error")
            raise
        with self._held_lane(store, work_item_id, prepared, token) as permit:
            yield permit
        if not store.token_absent(token):
            raise HeavyCapacityError(f"child token remained after terminal proof: {child_id}")

    @contextmanager
    def _held_lane(
        self, store, work_item_id: str, prepared: PreparedChild, token: SlotToken
    ) -> Iterator[ChildHeavyPermit]:
        """Run one lane's work on its slot; mark the lane terminal, then release the slot."""
        child_id = prepared.execution_id
        scope = SupervisedExecutionScope(
            child_id,
            self._lanes.project_data,
            lambda: token.lease_health.lost.is_set() or self.shutdown_requested,
            lambda: self._child_cancellation_cause(permit),
        )
        permit = ChildHeavyPermit(work_item_id, child_id, token, token.lease_health, scope)
        with self._gate:
            self._held[token.lease_id] = permit
        lease_lost = f"heavy lease lost for child execution {child_id}"
        cause = "record_error"
        try:
            try:
                try:
                    yield permit
                except HeavyCapacityError as exc:
                    cause = self._failed_cause(exc, permit)
                    if permit.lease_health.lost.is_set() and not self.shutdown_requested:
                        raise LeaseLostError(lease_lost) from exc
                    raise
                cause = "completed" if self._confirm_lease(permit) else "lease_lost"
                self._lanes.mark_terminal(prepared, cause)
                if cause == "lease_lost":
                    raise LeaseLostError(lease_lost)
                if self._cancelled():
                    raise ClaimAbortedError("campaign cancellation requested")
            finally:
                self._retire(store, child_id, token)
        except BaseException:
            # A "completed" cause here means marking it terminal itself failed.
            self._lanes.mark_terminal(prepared, "record_error" if cause == "completed" else cause)
            raise

    def _retire(self, store, child_id: ExecutionId, token: SlotToken) -> None:
        with self._gate:
            self._waiting.pop(token.lease_id, None)
            self._held.pop(token.lease_id, None)
            self._recovered_lost.discard(token.lease_id)
            self._confirmed.discard(token.lease_id)
        if not self._lanes.is_terminal(child_id):
            store.begin_token_recovery(token)
            self._lanes.cancel(child_id)
        if self._lanes.is_terminal(child_id):
            store.release(token)

    @staticmethod
    def _failed_cause(exc: HeavyCapacityError, permit: ChildHeavyPermit) -> str:
        """The terminal cause of a lane whose work raised a capacity error."""
        from booley.flows.sim.execution.contract import PreSimScopeStoppedError

        stopped = isinstance(exc.__cause__, PreSimScopeStoppedError)
        return "lease_lost" if stopped and permit.lease_health.lost.is_set() else "record_error"

    def _child_cancellation_cause(self, permit: ChildHeavyPermit) -> str | None:
        if permit.lease_health.lost.is_set():
            return f"Job Slot lease lost: {permit.token.job_class}/{permit.token.path.name}"
        return self.cancellation_cause()

    def _confirm_lease(self, permit: ChildHeavyPermit) -> bool:
        """Prove the lease is held before the lane goes terminal; reaping is normal after."""
        store = self._admission.slot_store
        assert store is not None, "child permits exist only under managed admission"
        held = not permit.lease_health.lost.is_set() and store.renew(permit.token)
        if held:
            with self._gate:
                self._confirmed.add(permit.token.lease_id)
        return held

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
                self._lanes.cancel(token.execution_id)

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
        scoped to that lane's item. Confirmed children are skipped: their token
        was reaped after the terminal record, so there is nothing to recover.
        """
        store = self._admission.slot_store
        with self._gate:
            lost = [
                permit
                for lease_id, permit in self._held.items()
                if permit.lease_health.lost.is_set()
                and lease_id not in self._recovered_lost
                and lease_id not in self._confirmed
            ]
            self._recovered_lost.update(permit.token.lease_id for permit in lost)
        for permit in lost:
            if store is not None:
                store.begin_token_recovery(permit.token)
            self._lanes.cancel(permit.child_execution_id)

    def _cancelled(self) -> bool:
        if self._shutdown.is_set():
            return True
        if not self._admission.cancellation():
            return False
        self._latch_cause(
            self._admission.cancellation_cause() or "admission cancellation requested"
        )
        return True


__all__ = [
    "ChildHeavyPermit",
    "HeavyCapacity",
    "HeavyCapacityError",
    "HeavyPermit",
    "LeaseLostError",
]
