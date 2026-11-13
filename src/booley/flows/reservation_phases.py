"""Shrinking a running Job's Memory Reservation as its phases finish (ADR 0087).

A memory-admitted Job starts with the largest estimate of its phases. A Flow
reports each phase end through :func:`report_phase_end`; the Job keeps a count of
the planned runs of every phase and lowers its reservation to the largest
estimate among the phases still pending. The reservation never grows. Safety
rests on never under-counting: a phase a Flow cannot count exactly is never
reported, so its estimate stays reserved until the Job ends.

Leaf module: it imports only the slot store and the Resource History key.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from booley.runtime.job_slots import SlotStore, SlotToken
from booley.runtime.resource_history import HistoryKey

logger = logging.getLogger(__name__)

# A phase as Flows report it: (target, phase, comma-joined Resource History test names).
_PhaseId = tuple[str, str, str]


class PhasePlan:
    """The pending phase runs of one Job and the reservation they justify (pure policy).

    Flags are ignored: within one Job every request carries the same flags.
    """

    def __init__(self, phases: Sequence[tuple[HistoryKey, float]], held_mb: float) -> None:
        self._pending: dict[_PhaseId, int] = {}
        self._estimate_mb: dict[_PhaseId, float] = {}
        for key, estimate_mb in phases:
            phase_id = (key.target, key.phase, key.test_run)
            self._pending[phase_id] = self._pending.get(phase_id, 0) + 1
            self._estimate_mb[phase_id] = estimate_mb
        self._held_mb = held_mb

    @property
    def held_mb(self) -> float:
        """The reservation the plan believes is held."""
        return self._held_mb

    def finish(self, target: str, phase: str, test_run: str = "") -> float | None:
        """Count one run of the phase out; the lower reservation it allows, or ``None``.

        ``None`` (change nothing) when the phase has no pending run, when no
        planned run remains (the reservation is held through reporting), or when
        the remaining estimate is not positive or not lower than the held value.
        """
        phase_id = (target, phase, test_run)
        if self._pending.get(phase_id, 0) <= 0:
            return None
        self._pending[phase_id] -= 1
        remaining = [self._estimate_mb[p] for p, count in self._pending.items() if count > 0]
        if not remaining:
            return None
        lowered = max(remaining)
        if lowered <= 0 or lowered >= self._held_mb:
            return None
        return lowered

    def accept(self, reservation_mb: float) -> None:
        """Record that *reservation_mb* is now held."""
        self._held_mb = reservation_mb


class ReservationShrinker:
    """Lowers one Job's held claim as its Flow reports phase ends.

    A memory-admitted Job has one lane, so reports arrive one at a time and no
    lock is needed.
    """

    def __init__(self, plan: PhasePlan, store: SlotStore, token: SlotToken) -> None:
        self._plan = plan
        self._store = store
        self._token = token

    def phase_ended(self, target: str, phase: str, tests: Sequence[str] = ()) -> None:
        """Handle one reported phase end."""
        lowered = self._plan.finish(target, phase, ",".join(tests))
        if lowered is None:
            return
        if self._store.shrink_reservation(self._token, lowered):
            self._plan.accept(lowered)
        else:
            # The disk keeps the larger, safe value.
            logger.debug("Memory Reservation not lowered to %.0f MiB", lowered)


_active: ContextVar[ReservationShrinker | None] = ContextVar("reservation_shrinker", default=None)


@contextmanager
def shrinking(shrinker: ReservationShrinker) -> Iterator[None]:
    """Route :func:`report_phase_end` to *shrinker* inside the block."""
    token = _active.set(shrinker)
    try:
        yield
    finally:
        _active.reset(token)


def report_phase_end(target: str, phase: str, *, tests: Sequence[str] = ()) -> None:
    """Tell the running Job a phase finished; a no-op unless its claim can shrink."""
    shrinker = _active.get()
    if shrinker is not None:
        shrinker.phase_ended(target, phase, tests)
