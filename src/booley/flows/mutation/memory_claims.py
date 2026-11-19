"""Memory Reservations for a mutation campaign's builds and simulations.

The campaign is one memory-class Job with a zero companion claim, and each
variant build and each simulation of the Target's tests holds a Memory
Reservation of its own while it runs, sized from the Simulation Target's
Resource History. One that cannot be sized runs alone,
as an unsized Simulation Record work item does. A run the OOM killer took at
the Sandbox's limit runs once more, alone; a host kill is not retried (ADR 0087).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, TypeVar

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.prepared_invocation import PreparedInvocation
from booley.runtime import job_slots, oom_priority
from booley.runtime.endpoint_execution import OOM_REQUEUE_REASON
from booley.runtime.job_records import _proc_cmdline

if TYPE_CHECKING:
    from booley.flows.job_estimates import JobRequest
    from booley.flows.mutation.flow import MutationFlow
    from booley.flows.mutation.tool_runs import OomKill
    from booley.runtime.endpoint_execution import EndpointOutcome

_Run = TypeVar("_Run")

#: Why a host-killed run is not retried, and the remedy (the Job result's wording, ADR 0087).
_HOST_OOM_KILL_REASON = (
    "OOM-killed: the host ran out of memory while the Sandbox stayed within its "
    "[sandbox].memory limit, so it is not retried. Free memory on the host, or lower "
    "[sandbox].memory and recreate the Sandbox, then run it again"
)


class CampaignClaims:
    """What a campaign's tool runs hold: a claim each under managed admission, else nothing."""

    def __init__(self, admission: AdmissionContext | None = None) -> None:
        self._admission = admission

    @contextmanager
    def hold(
        self,
        request: Callable[[], JobRequest | None],
        *,
        build: bool,
        target: str,
        tests: Sequence[str] = (),
        alone: bool = False,
    ) -> Iterator[None]:
        """Hold one Memory Reservation for the block, waiting in queue order for it.

        *request* names what the block runs (``None``: unknown, so it runs
        alone); it is not called without managed admission. *target* and *tests*
        name the block in queue reports (a build has no tests). With *alone* the
        claim is the whole budget: the block runs again after an OOM kill.
        Raises ``ClaimAbortedError`` when the Job is cancelled while waiting and
        ``ClaimLostError`` when the waiting entry is withdrawn.
        """
        admission = self._admission
        if admission is None or admission.slot_store is None or admission.memory_sizing is None:
            yield
            return
        store = admission.slot_store
        claim = admission.memory_sizing.plan_work_item(request(), build=build)
        if alone:
            claim = replace(claim, reservation_mb=None, run_alone=True)
        pid = os.getpid()
        token = store.acquire(
            job_slots.CLASS_MEMORY,
            pid=pid,
            argv=_proc_cmdline(pid) or [],
            role=admission.role,
            timeout_s=admission.timeout_seconds,
            should_abort=admission.cancellation,
            memory=claim,
            queue_reason=OOM_REQUEUE_REASON if alone else None,
            job=admission.work_item_label(target, tests),
        )
        try:
            # Entered last, so ranking stops before the claim is released.
            with oom_priority.job_oom_priority(store, token):
                yield
        finally:
            store.release(token)

    def run_retrying_oom(
        self,
        run: Callable[[bool], _Run],
        oom_kill: Callable[[_Run], OomKill | None],
        *,
        name: str,
    ) -> tuple[_Run, str]:
        """Run *run*, once more alone after a kill at the Sandbox's limit; the run and its kill.

        *run* takes whether it runs alone and holds its own claim. The kill is
        why the final run says nothing about the Mutant (``""``: it was not
        OOM-killed). Without managed admission nothing could run alone, so a
        kill is final, as an unadmitted Job's is.
        """
        result = run(False)
        killed = oom_kill(result)
        admission = self._admission
        managed = (
            admission is not None
            and admission.slot_store is not None
            and admission.memory_sizing is not None
        )
        if killed == "sandbox_limit" and managed:
            print(f"[requeue] {name}: {OOM_REQUEUE_REASON}; it runs again alone", flush=True)
            result = run(True)
            return result, _oom_kill_reason(oom_kill(result), retried=True)
        return result, _oom_kill_reason(killed, retried=False)


def _oom_kill_reason(killed: OomKill | None, *, retried: bool) -> str:
    """Why a run that the OOM killer took is not run again; ``""`` when it was not taken."""
    if killed is None:
        return ""
    if killed == "host":
        return _HOST_OOM_KILL_REASON
    if retried:
        return "OOM-killed at the Sandbox's [sandbox].memory limit, also when run alone"
    return "OOM-killed at the Sandbox's [sandbox].memory limit; without memory admission it is not retried"


@dataclass(frozen=True)
class MutationInvocation(PreparedInvocation):
    """The campaign run, which borrows its admission to claim memory for each tool run."""

    borrows_admission: ClassVar[bool] = True

    flow: MutationFlow

    def run(self, admission: object | None) -> EndpointOutcome:
        claims = CampaignClaims(admission if isinstance(admission, AdmissionContext) else None)
        return self.flow.run_with_claims(claims)
