"""Memory admission of sim, synth, fpga, lint and Custom Flow Jobs (ADR 0087).

Such a Job is admitted in the ``memory`` Job Class: it holds a Memory Reservation estimated from the Resource History
and runs beside other Jobs while the reservations fit the Memory Budget. A Job
that cannot be sized runs alone. A Job is never refused, except one that uses a
licensed tool with no Seat Cap (``[jobs.seats]``).

Every number here is in MiB, the unit of the Resource History's ``peak_memory_mb``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.flows.job_estimates import (
    Fit,
    JobEstimate,
    JobRequest,
    ResourceEstimator,
    phase_occurrences,
)
from booley.runtime.job_slots import CLASS_MEMORY, MemoryClaim, SlotStore, SlotToken
from booley.runtime.memory_budget import compute_memory_budget
from booley.runtime.resource_history import HistoryKey, load_records

logger = logging.getLogger(__name__)

#: Carries a Job's thread count into a child process that runs under its parent's claim.
JOB_THREADS_ENV = "BOOLEY_INTERNAL_JOB_THREADS"

_BYTES_PER_MIB = 1024 * 1024

_job_threads: ContextVar[int | None] = ContextVar("booley_job_threads", default=None)

#: Flows whose Jobs report phase ends, so their reservation can shrink.
_SHRINKING_FLOWS = frozenset({"sim", "synth", "fpga"})


@dataclass(frozen=True)
class MemoryAdmissionPlan:
    """The claim a Job makes, and the warning its result carries (``None`` for none)."""

    claim: MemoryClaim
    warning: str | None = None
    #: A shrinkable claim's planned phase runs, in run order, each with its memory
    #: estimate in MiB (a key repeats once per planned run). Empty when the Job
    #: holds its claim to the end: run-alone, lint, Custom Flow and coverage Jobs.
    phases: tuple[tuple[HistoryKey, float], ...] = ()

    def alone(self) -> MemoryAdmissionPlan:
        """The same Job claiming the whole budget, never shrinking: a requeued Job's plan."""
        claim = MemoryClaim(
            budget_mb=self.claim.budget_mb, run_alone=True, wall_s=self.claim.wall_s
        )
        return MemoryAdmissionPlan(claim, self.warning)


def plan_memory_claim(
    requests: Sequence[JobRequest] | None,
    records: Sequence[dict[str, Any]],
    budget_mb: float | None,
    budget_error: str | None,
) -> MemoryAdmissionPlan:
    """Size one Job's claim (pure policy).

    A Job runs alone when the Memory Budget is unavailable, when its footprint
    is unknown (no requests, or no recorded peak for any of its phases), when
    some phase has no estimate, or when its estimate exceeds the budget; only
    the unavailable budget and the over-budget Job carry a warning.
    """
    if budget_mb is None:
        return MemoryAdmissionPlan(
            MemoryClaim(budget_mb=0.0, run_alone=True),
            f"Memory Budget unavailable ({budget_error}); this Job ran alone.",
        )
    if not requests:
        return MemoryAdmissionPlan(MemoryClaim(budget_mb=budget_mb, run_alone=True))
    estimate = ResourceEstimator(records).estimate(requests)
    claim, warning = _claim_for_estimate(estimate, budget_mb)
    if claim.run_alone:
        return MemoryAdmissionPlan(claim, warning)
    phases = _shrinkable_phases(requests, estimate) if _can_shrink(requests) else ()
    return MemoryAdmissionPlan(claim, phases=phases)


def _claim_for_estimate(estimate: JobEstimate, budget_mb: float) -> tuple[MemoryClaim, str | None]:
    """The claim a sized Job makes under *budget_mb*, and its warning (over budget only)."""
    wall_s = estimate.own_wall_s
    alone = MemoryClaim(budget_mb=budget_mb, run_alone=True, wall_s=wall_s)
    reservation = estimate.reservation_mb
    if reservation is None:
        return alone, None
    fit = estimate.fit(budget_mb)
    if fit is Fit.OVER_BUDGET:
        return alone, (
            f"Memory estimate {reservation:.0f} MiB exceeds the Memory Budget "
            f"of {budget_mb:.0f} MiB; this Job ran alone."
        )
    if fit is Fit.RUN_ALONE or reservation <= 0:
        return alone, None
    return MemoryClaim(budget_mb=budget_mb, reservation_mb=reservation, wall_s=wall_s), None


def plan_work_item_claim(
    request: JobRequest | None,
    *,
    build: bool,
    records: Sequence[dict[str, Any]],
    budget_mb: float | None,
    budget_error: str | None,
) -> tuple[MemoryClaim, str | None]:
    """Size one Simulation Campaign work item's claim like a Job's, and its warning.

    *request* is ``None`` when the item cannot be sized (it runs alone). Its
    build phase counts only when *build* is true: a settled shared build is not
    the item's to reserve. An item never holds a licensed seat.
    """
    if request is None or budget_mb is None:
        plan = plan_memory_claim(None, records, budget_mb, budget_error)
        return plan.claim, plan.warning
    keys = dict.fromkeys(
        key for key in phase_occurrences(request) if build or key.phase != "sim_build"
    )
    return _claim_for_estimate(ResourceEstimator(records).estimate_keys(keys), budget_mb)


def _can_shrink(requests: Sequence[JobRequest]) -> bool:
    """Whether every request is a sim (not coverage), synth or fpga run.

    Lint, Custom Flows and coverage Jobs report no phase ends: their phase
    counts are not exactly known from the request, so they hold their claim.
    """
    return all(
        request.flow in _SHRINKING_FLOWS
        and not (request.flow == "sim" and "coverage" in request.flags)
        for request in requests
    )


def _shrinkable_phases(
    requests: Sequence[JobRequest], estimate: JobEstimate
) -> tuple[tuple[HistoryKey, float], ...]:
    """Each planned phase run with its memory estimate (every key has one: the Job is sized)."""
    by_key = {phase.key: phase.memory_mb.value or 0.0 for phase in estimate.phases}
    return tuple((key, by_key[key]) for request in requests for key in phase_occurrences(request))


def _read_history_and_budget(
    booley_dir: Path | None, config: Mapping[str, object]
) -> tuple[list[dict[str, Any]], float | None, str | None]:
    """The Resource History records, the Memory Budget in MiB, and why it is unavailable.

    An unreadable or foreign history reads as empty (every Job unknown), as
    ``append_records`` treats it; an uncomputable budget is ``None`` with its
    reason. *booley_dir* is ``None`` without a resolvable Booley Directory,
    which has no history.
    """
    records: list[dict[str, Any]] = []
    if booley_dir is not None:
        try:
            records = load_records(booley_dir)
        except (OSError, ValueError) as exc:
            logger.warning("Resource History unreadable; sizing Jobs as unknown: %s", exc)
    budget_mb: float | None = None
    budget_error: str | None = None
    try:
        budget_mb = compute_memory_budget(config).bytes / _BYTES_PER_MIB
    except (OSError, ValueError) as exc:
        budget_error = str(exc)
    return records, budget_mb, budget_error


def measure_memory_claim(
    booley_dir: Path | None,
    config: Mapping[str, object],
    requests: Sequence[JobRequest] | None,
) -> MemoryAdmissionPlan:
    """Read the Resource History and the Memory Budget, then size the claim."""
    return plan_memory_claim(requests, *_read_history_and_budget(booley_dir, config))


@dataclass
class MemorySizing:
    """What an admitted Job lends its Campaign to size each work item's claim.

    ``warnings`` collects the items' distinct warnings for the Job's result.
    """

    booley_dir: Path | None
    config: Mapping[str, object]
    warnings: list[str] = field(default_factory=list[str], init=False)

    def plan_work_item(self, request: JobRequest | None, *, build: bool) -> MemoryClaim:
        """Size one item from the history as it is now (earlier items add to it)."""
        records, budget_mb, budget_error = _read_history_and_budget(self.booley_dir, self.config)
        claim, warning = plan_work_item_claim(
            request, build=build, records=records, budget_mb=budget_mb, budget_error=budget_error
        )
        if warning is not None and warning not in self.warnings:
            self.warnings.append(warning)
        return claim


def job_threads_at_start(
    store: SlotStore, cores: int, *, own_flow: SlotToken | None = None
) -> int:
    """The thread count one memory-admitted Job gets: its share of *cores*, rounded down.

    The cores are divided by the Jobs holding a Memory Reservation. Call this
    once, right after the Job's own claim is admitted, so the holders include
    the Job itself; the result is fixed for the Job's run and later Jobs never
    shrink it. It is never above *cores* and never below one.

    A Campaign work item that took its own claim passes its Flow's token as
    *own_flow*: the Flow's reservation is zero by then (an item takes a claim
    only after the Flow dropped to zero), so it holds no share of the cores and
    is left out. Every other caller passes nothing and counts every holder.
    """
    holders, _waiters = store.snapshot(CLASS_MEMORY)
    if own_flow is not None:
        holders = [token for token in holders if token.lease_id != own_flow.lease_id]
    return max(1, cores // max(1, len(holders)))


@contextmanager
def fixed_job_threads(count: int | None) -> Iterator[None]:
    """Make *count* the running Job's thread count (``None`` for none) inside the block."""
    token = _job_threads.set(count)
    try:
        yield
    finally:
        _job_threads.reset(token)


def job_thread_count() -> int | None:
    """The running Job's thread count: the admission's, else its parent's, else ``None``.

    The parent hands its count over in ``JOB_THREADS_ENV``. Only Booley writes
    that variable, so a malformed value raises instead of being ignored.
    """
    count = _job_threads.get()
    if count is not None:
        return count
    inherited = os.environ.get(JOB_THREADS_ENV)
    return None if inherited is None else int(inherited)
