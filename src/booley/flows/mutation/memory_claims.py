"""Memory Reservations for a mutation campaign's builds and simulations.

The campaign is one memory-class Job with a zero companion claim, and each
variant build and each simulation of the Target's tests holds a Memory
Reservation of its own while it runs, sized from the Simulation Target's
Resource History. One that cannot be sized runs alone,
as an unsized Simulation Record work item does.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.prepared_invocation import PreparedInvocation
from booley.runtime import job_slots, oom_priority
from booley.runtime.job_records import _proc_cmdline

if TYPE_CHECKING:
    from booley.flows.job_estimates import JobRequest
    from booley.flows.mutation.flow import MutationFlow
    from booley.runtime.endpoint_execution import EndpointOutcome


class CampaignClaims:
    """What a campaign's tool runs hold: a claim each under managed admission, else nothing."""

    def __init__(self, admission: AdmissionContext | None = None) -> None:
        self._admission = admission

    @contextmanager
    def hold(self, request: Callable[[], JobRequest | None], *, build: bool) -> Iterator[None]:
        """Hold one Memory Reservation for the block, waiting in queue order for it.

        *request* names what the block runs (``None``: unknown, so it runs
        alone); it is not called without managed admission. Raises
        ``ClaimAbortedError`` when the Job is cancelled while waiting and
        ``ClaimLostError`` when the waiting entry is withdrawn.
        """
        admission = self._admission
        if admission is None or admission.slot_store is None or admission.memory_sizing is None:
            yield
            return
        store = admission.slot_store
        claim = admission.memory_sizing.plan_work_item(request(), build=build)
        pid = os.getpid()
        token = store.acquire(
            job_slots.CLASS_MEMORY,
            pid=pid,
            argv=_proc_cmdline(pid) or [],
            role=admission.role,
            timeout_s=admission.timeout_seconds,
            should_abort=admission.cancellation,
            memory=claim,
        )
        try:
            # Entered last, so ranking stops before the claim is released.
            with oom_priority.job_oom_priority(store, token):
                yield
        finally:
            store.release(token)


@dataclass(frozen=True)
class MutationInvocation(PreparedInvocation):
    """The campaign run, which borrows its admission to claim memory for each tool run."""

    borrows_admission: ClassVar[bool] = True

    flow: MutationFlow

    def run(self, admission: object | None) -> EndpointOutcome:
        claims = CampaignClaims(admission if isinstance(admission, AdmissionContext) else None)
        return self.flow.run_with_claims(claims)
