"""Memory admission of heavy, lint and Custom Flow Jobs behind a development switch (ADR 0087).

With ``BOOLEY_DEV_MEMORY_ADMISSION=1`` such a Job is admitted in the ``memory``
Job Class: it holds a Memory Reservation estimated from the Resource History
and runs beside other Jobs while the reservations fit the Memory Budget. A Job
that cannot be sized runs alone, and a Job is never refused.

The switch is undocumented and temporary: the cutover that makes memory
admission the only admission removes it (ADR 0087, Rollout). Every number here
is in MiB, the unit of the Resource History's ``peak_memory_mb``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.flows.job_estimates import Fit, JobRequest, ResourceEstimator
from booley.runtime.job_slots import MemoryClaim
from booley.runtime.memory_budget import compute_memory_budget
from booley.runtime.resource_history import load_records

logger = logging.getLogger(__name__)

#: The development switch; only the exact value ``"1"`` turns memory admission on.
MEMORY_ADMISSION_ENV = "BOOLEY_DEV_MEMORY_ADMISSION"

_BYTES_PER_MIB = 1024 * 1024


def memory_admission_enabled() -> bool:
    """Whether memory-admitted Jobs (heavy, lint, Custom Flow) take a Memory Reservation."""
    return os.environ.get(MEMORY_ADMISSION_ENV) == "1"


@dataclass(frozen=True)
class MemoryAdmissionPlan:
    """The claim a Job makes, and the warning its result carries (``None`` for none)."""

    claim: MemoryClaim
    warning: str | None = None

    def describe(self) -> str:
        """What the Job asks of the budget, for queue narration."""
        if self.claim.run_alone or self.claim.reservation_mb is None:
            return "runs alone"
        return (
            f"needs {self.claim.reservation_mb:,.0f} MiB "
            f"of the {self.claim.budget_mb:,.0f} MiB Memory Budget"
        )


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
    wall_s = estimate.own_wall_s
    alone = MemoryClaim(budget_mb=budget_mb, run_alone=True, wall_s=wall_s)
    reservation = estimate.reservation_mb
    if reservation is None:
        return MemoryAdmissionPlan(alone)
    fit = estimate.fit(budget_mb)
    if fit is Fit.OVER_BUDGET:
        return MemoryAdmissionPlan(
            alone,
            f"Memory estimate {reservation:.0f} MiB exceeds the Memory Budget "
            f"of {budget_mb:.0f} MiB; this Job ran alone.",
        )
    if fit is Fit.RUN_ALONE or reservation <= 0:
        return MemoryAdmissionPlan(alone)
    return MemoryAdmissionPlan(
        MemoryClaim(budget_mb=budget_mb, reservation_mb=reservation, wall_s=wall_s)
    )


def measure_memory_claim(
    booley_dir: Path | None,
    config: Mapping[str, object],
    requests: Sequence[JobRequest] | None,
) -> MemoryAdmissionPlan:
    """Read the Resource History and the Memory Budget, then size the claim.

    An unreadable or foreign history reads as empty (every Job unknown), as
    ``append_records`` treats it; an uncomputable budget makes the Job run
    alone with a warning. *booley_dir* is ``None`` without a resolvable Booley
    Directory, which has no history.
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
    return plan_memory_claim(requests, records, budget_mb, budget_error)
