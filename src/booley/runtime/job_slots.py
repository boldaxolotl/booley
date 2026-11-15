"""Shared on-disk slot store for Job admission (ADR 0028).

Sessions and Flow/Specialist subprocesses share one Sandbox. Admission
("may this Job run now, or must it wait?") uses a filesystem-coordinated store of
claim files under ``<runtime>/jobs/slots/`` that every process shares.
Container-only guarantees one PID namespace, so PID liveness and ``/proc``
argv identity can be checked against the process that owns each claim.

Every admission request is one **entry file** in the requesting Job's class
directory.  The scheme is bakery-style — create your own uniquely-named
entry, then rank all live entries; no entry ever needs to modify another
except to reap a provably-stale one:

* Entry names sort in scheduling order: ``h-`` holders (running) before
  ``w-`` waiters, then priority, sequence and PID, with the filename suffix breaking ties. Priority 0 is
  Interactive; other requester roles use priority 1. A later Interactive
  request overtakes queued lower-priority work, but *never* a holder: promotion renames only one's **own**
  ``w-`` entry to ``h-``, so running work cannot be preempted (ADR 0028
  Decision 6).
* Because the ordering is total and each process promotes only itself under a
  short per-class promotion gate, two processes cannot both promote into the
  last free slot.
* Entries appear atomically (content written to a tmp, then hard-linked
  into place), so a reader never sees a partial claim.
* Stale entries — dead PID, recycled PID (``/proc`` argv mismatch), or a
  holder past its deadline — are reaped by whoever notices.  Unique names
  make reaping race-free: the unlink can only ever hit the exact entry that
  was judged stale, never a fresh claim reusing the name.

The **memory** class (ADR 0087) admits by Memory Reservation: a claim holds while the reservations of every holder plus its own fit
its budget, or alone when it is marked run-alone. Order is the same total
order; the first waiter that does not fit (the front) blocks the claims behind
it, except that a claim with its own wall-clock estimate jumps ahead when it
fits now and is estimated to finish before the front can start. The front's
earliest start comes from the running claims' estimated finishes; an unknown
finish makes it unknown, so nothing jumps. A **companion** claim (a Specialist
or a mutation coordinator) never keeps another claim waiting on it alone: it
does not count toward a run-alone claim's wait, the first other claim starts
when only companions hold, and a companion that does not fit never becomes the
front. An entry whose claim cannot be read
counts as not fitting and ends jumping. A holder may lower its own reservation
as its phases finish (``shrink_reservation``), never raise it. A claim may also hold
a **seat** of a licensed EDA tool, judged against the tool's Seat Cap: it
starts only while fewer seats of that tool are held than the cap, and keeps the
cap it was created with, so a cap changed while Jobs wait applies to new claims
only. A seat is held exactly as long as the entry (and so the reservation) is.

Pure + stdlib-only, like ``runtime.job_records`` (whose deadline/identity
guards it reuses): PID liveness, ``/proc`` reads, the clock, and sleep are
all injectable, so scheduling behavior is unit-testable without processes.
This module is mechanism only — *enforcement* (claim on entry, release in
``finally``) lives at the endpoint-process entry point, ``McpTool.main``,
and Specialist entry points.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from booley.core.boundary import as_int, require_finite_number
from booley.core.file_lock import LockContentionError, acquire_file_lock, release_file_lock
from booley.runtime.execution_records import (
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    execution_paths,
    read_json,
    read_slot_entry,
    request_cancellation,
    verify_inherited_execution_id,
)
from booley.runtime.execution_recovery import recover_execution, recover_process_owner
from booley.runtime.filesystem_utils import replace_file, unlink_file
from booley.runtime.job_records import (
    DEADLINE_SLACK_SECONDS,
    _proc_cmdline,
    parse_stamp,
)
from booley.runtime.pid import (
    DEAD_PROCESS_STATES,
    ProcessIdentity,
    ProcessObservation,
    capture_process_identity,
    is_pid_alive,
    observe_process,
)
from booley.runtime.timefmt import rfc3339_from_epoch

__all__ = ["MemoryClaim"]

logger = logging.getLogger(__name__)

# The one Job class (ADR 0028 Decision 5): admitted by Memory Reservation against a budget (ADR 0087).
CLASS_MEMORY = "memory"
# Class directories older versions wrote; nothing admits into them, but a live
# holder there still blocks a legacy-directory migration.
RETIRED_JOB_CLASSES = ("heavy", "light")

# Requester roles → queue priority (lower sorts first). Interactive work
# jumps ahead of other queued work; holders are never displaced.
ROLE_INTERACTIVE = "interactive"
# Background baseline Jobs: queued interactive claims overtake them, and a
# holder is never displaced (ADR 0028 Decision 6).
ROLE_BACKGROUND = "background"
_ROLE_PRIORITY = {ROLE_INTERACTIVE: 0, ROLE_BACKGROUND: 1}

# Set by a process that already holds a Job Class slot for the child it spawns,
# as ``<class>/<entry name>``. The child's Flow admission then claims nothing:
# a second claim would double-count the run and wait behind its own parent.
INHERITED_SLOT_ENV = "BOOLEY_INHERITED_SLOT"

# Entry-name state prefixes. 'h' < 'w' lexicographically, so holders always
# rank ahead of every waiter regardless of priority — that ordering is what
# makes "no preemption" fall out of a plain sorted() call.
_HOLDER = "h"
_WAITER = "w"

# An entry file that cannot be parsed is foreign junk or corruption, never a
# half-written claim (creation is atomic). It is invisible to ranking and
# reaped once comfortably older than any plausible in-flight creation.
_UNREADABLE_REAP_AGE_SECONDS = 60.0

# How often a still-queued claim repeats its "waiting for slot" narration.
# Position-change-only narration went silent for the entire holder's run, which
# reads as a hang; a reminder every half minute is cheap and un-spammy (F-27).
NARRATE_INTERVAL_SECONDS = 30.0

# Windows may briefly deny rename/unlink while another claimant is reading the
# same entry. Retrying keeps the shared store from producing false losses or
# permanent phantom-holder deadlocks.
_ENTRY_IO_ATTEMPTS = 20
_ENTRY_IO_RETRY_SECONDS = 0.01
# Relists of the memory class a view makes when an entry vanished under its read.
_MEMORY_VIEW_ATTEMPTS = 3
# Bound on waiting for an in-flight renewal in ``release``: lease gate, read and
# replace each spend up to ~1.1 s of shared sharing-error backoff.
_RENEWAL_JOIN_SECONDS = 5.0
# A release that cannot take the lease gate retries in the background for about
# 90 x (1 s + the gate's own attempts), longer than the 60 s after which
# ``_lease_gate`` clears the gate a crashed process left behind.
_RELEASE_RETRY_ATTEMPTS = 90
_RELEASE_RETRY_SECONDS = 1.0
_ENTRY_CREATE_ATTEMPTS = 20

# Ranking and waiter->holder promotion must be one serialized decision. Without
# a gate, two processes can each observe themselves in the last free rank before
# either rename becomes visible, then both promote. A gate appears with its
# owner already written (hard link of a complete tmp), atomically on the local
# filesystems supported by the Sandbox and Windows CI.
_PROMOTION_GATE_NAME = ".promotion.lock"
# Breaking a stale gate happens under this per-class OS lock, which the kernel
# drops when its holder dies, so the lock itself can never go stale.
_GATE_BREAK_LOCK_NAME = ".gate-break.lock"

# Versioned holder identity + renewable recovery lease. Work budgets remain a
# separate field: an endpoint may legitimately have no work timeout, but
# every new holder must still be recoverable after its owner stops renewing.
SLOT_SCHEMA_VERSION = 2
LEASE_ACTIVE = "active"
LEASE_CANCELLING = "cancelling"
LEASE_DURATION_SECONDS = 30.0
LEASE_RENEW_INTERVAL_SECONDS = 10.0


class ClaimLostError(RuntimeError):
    """The waiting entry vanished — the queued job was cancelled.

    A live waiter's entry can only disappear through a deliberate unlink
    (``booley_cancel`` claiming the cancellation atomically); the ghost
    guards never reap a live, argv-matching waiter. Raised by ``acquire`` so
    a cancelled child exits instead of silently re-joining the queue —
    re-submitting would undo the cancellation.
    """


class ClaimAbortedError(RuntimeError):
    """The caller's ``should_abort`` hook fired while queued.

    Raised by ``acquire`` after withdrawing its own entry, so a shutdown
    request (e.g. the Runner's Ctrl+C event) can end a queue wait instead of
    idling until a slot frees.
    """


# Token states returned by SlotStore.refresh().
HOLDING = "holding"  # the token owns a slot — run
QUEUED = "queued"  # waiting; position is meaningful
LOST = "lost"  # the entry vanished (reaped or cancelled) — do not run


@dataclass(frozen=True)
class MemoryClaim:
    """What a memory-class entry asks for: a reservation, or the whole budget.

    ``budget_mb`` is the budget the claim is judged against (it is persisted so
    a reloaded token equals its owner's). Exactly one of ``reservation_mb`` and
    ``run_alone`` is given. A reservation above the budget could never start
    and would block the queue forever, so the caller must mark it run-alone.
    A holder may drop to zero reservation; it then blocks no claim.
    ``seats`` lists the licensed EDA tools the Job holds one seat of, each with
    the Seat Cap it is judged against (persisted like ``budget_mb``).
    ``wall_s`` is the Job's own wall-clock estimate in seconds (``None`` when it
    has no history of its own); only such a claim may jump ahead of the queue.
    ``companion`` marks a Specialist's or a mutation coordinator's claim: it
    reserves memory but never keeps another claim waiting on it alone (see
    ``_MemoryLoad._fits``), and is never run-alone.
    """

    budget_mb: float
    reservation_mb: float | None = None
    run_alone: bool = False
    seats: tuple[tuple[str, int], ...] = ()
    wall_s: float | None = None
    companion: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_alone, bool):
            raise ValueError(f"run_alone must be a bool, got {self.run_alone!r}")
        for tool, cap in self.seats:
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
                raise ValueError(f"Seat Cap of {tool!r} must be a whole number >= 1, got {cap!r}")
        if require_finite_number(self.budget_mb, field="budget_mb") < 0:
            raise ValueError(f"budget_mb must be >= 0, got {self.budget_mb!r}")
        if self.wall_s is not None and require_finite_number(self.wall_s, field="wall_s") < 0:
            raise ValueError(f"wall_s must be >= 0, got {self.wall_s!r}")
        if self.run_alone:
            if self.companion:
                raise ValueError("a companion memory claim is never run-alone")
            if self.reservation_mb is not None:
                raise ValueError("a run-alone memory claim carries no reservation_mb")
            return
        if self.reservation_mb is None:
            raise ValueError("a memory claim needs reservation_mb or run_alone")
        if require_finite_number(self.reservation_mb, field="reservation_mb") < 0:
            raise ValueError(f"reservation_mb must be >= 0, got {self.reservation_mb!r}")
        if self.reservation_mb > self.budget_mb:
            raise ValueError(
                f"reservation_mb {self.reservation_mb!r} exceeds budget_mb {self.budget_mb!r}: "
                "mark the claim run_alone"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "budget_mb": self.budget_mb,
            "reservation_mb": self.reservation_mb,
            "run_alone": self.run_alone,
            "seats": dict(self.seats),
            "wall_s": self.wall_s,
            "companion": self.companion,
        }

    @staticmethod
    def from_payload(value: object) -> MemoryClaim | None:
        """The claim an entry persisted, or None when absent or malformed."""
        if not isinstance(value, dict):
            return None
        seats = value.get("seats", {})  # an entry written before seats existed holds none
        try:
            if not isinstance(seats, dict):
                raise ValueError(f"seats must be a table, got {seats!r}")
            return MemoryClaim(
                budget_mb=value["budget_mb"],
                reservation_mb=value.get("reservation_mb"),
                run_alone=value.get("run_alone") is True,
                seats=tuple(seats.items()),
                wall_s=value.get("wall_s"),
                companion=value.get("companion") is True,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True)
class JobLabel:
    """Who an entry belongs to, for queue reports: the Job's run ID, Flow and Target.

    Presentation only: it never takes part in admission, so it lives on the
    entry beside ``argv`` rather than inside the ``MemoryClaim``. ``target`` is
    empty when the Job names none.
    """

    run_id: str
    flow: str
    target: str

    def to_payload(self) -> dict[str, str]:
        return {"run_id": self.run_id, "flow": self.flow, "target": self.target}

    @staticmethod
    def from_payload(value: object) -> JobLabel | None:
        """The label an entry persisted, or None when absent or malformed."""
        if not isinstance(value, dict):
            return None
        run_id, flow, target = (value.get(name) for name in ("run_id", "flow", "target"))
        if isinstance(run_id, str) and isinstance(flow, str) and isinstance(target, str):
            return JobLabel(run_id, flow, target)
        return None


#: How many of the largest memory holders a queue report names.
_WAIT_HOLDERS = 3


@dataclass(frozen=True)
class MemoryWait:
    """What one queued memory claim waits for, in the terms a queue report gives.

    ``position`` is 1-based among the blocked claims. ``free_mb`` is the Memory
    Budget headroom the running holders leave (what admission judges), not host
    free memory. ``seats`` lists each ``(tool, held, cap)`` whose seats are all
    taken. ``holders`` are up to three running claims, largest first
    (run-alone first, shown with ``None`` for the whole budget).
    """

    position: int
    claim: MemoryClaim
    free_mb: float
    seats: tuple[tuple[str, int, int], ...]
    holders: tuple[tuple[str, float | None], ...]
    reason: str | None = None  # why the claim exists again (a requeued Job), else None

    def describe(self) -> str:
        """One line: the need, the free memory, any blocked seat and the largest holders."""
        claim = self.claim
        if claim.run_alone:
            reason = (
                "Memory Budget unavailable"
                if claim.budget_mb == 0
                else "memory estimate unknown or above the Memory Budget"
            )
            text = f"waits to run alone ({reason})"
        else:
            text = (
                f"needs {claim.reservation_mb or 0.0:,.0f} MiB; {self.free_mb:,.0f} MiB free "
                f"of the {claim.budget_mb:,.0f} MiB Memory Budget"
            )
        text += "".join(
            f"; waits for a {tool} seat ({held} of {cap} held)" for tool, held, cap in self.seats
        )
        if self.holders:
            named = ", ".join(
                f"{name} {'whole budget' if mb is None else f'{mb:,.0f} MiB'}"
                for name, mb in self.holders
            )
            text += f"; largest holders: {named}"
        return text if self.reason is None else f"{text}; {self.reason}"


def _holder_name(token: SlotToken) -> str:
    """``run-1 (synth, a)`` for a labelled holder, ``pid N`` when no run ID is known."""
    label = token.job
    if label is None:
        return f"pid {token.pid}"
    detail = ", ".join(part for part in (label.flow, label.target) if part)
    name = label.run_id or f"pid {token.pid}"
    return f"{name} ({detail})" if detail else name


def memory_wait(
    own: SlotToken, holders: list[SlotToken], waiters: list[SlotToken], now: float
) -> MemoryWait | None:
    """What the memory waiter *own* waits for, or None when it holds no memory claim.

    Position counts like the narrator always did: the blocked claims ahead of
    *own* among the *waiters* ranked before it, behind the running *holders*.
    "Free" is the Memory Budget minus the holders' reservations (nothing while
    a run-alone Job runs); waiters ahead that are about to start are not
    subtracted, so it can overstate what is free the instant a holder leaves.
    """
    claim = own.memory
    if claim is None:
        return None
    ahead = [waiter.memory for waiter in waiters if waiter.path.name < own.path.name]
    running = [
        (held.memory, parse_stamp(held.promoted_at) or parse_stamp(held.created_at))
        for held in holders
    ]
    _starts, blocked_ahead = _memory_queue(running, ahead, claim, now)
    # Holders whose claim cannot be read say nothing about the budget: skip them.
    claimed = [(held, held.memory) for held in holders if held.memory is not None]
    alone = any(held_claim.run_alone for _held, held_claim in claimed)
    used_mb = sum(held_claim.reservation_mb or 0.0 for _held, held_claim in claimed)
    seats_held = Counter(tool for _held, held_claim in claimed for tool, _cap in held_claim.seats)
    largest = sorted(claimed, key=lambda pair: _holder_weight(pair[1]))[:_WAIT_HOLDERS]
    return MemoryWait(
        position=blocked_ahead + 1,
        claim=claim,
        reason=own.queue_reason,
        free_mb=0.0 if alone else max(0.0, claim.budget_mb - used_mb),
        seats=tuple(
            (tool, seats_held[tool], cap) for tool, cap in claim.seats if seats_held[tool] >= cap
        ),
        holders=tuple(
            (_holder_name(held), None if held_claim.run_alone else held_claim.reservation_mb)
            for held, held_claim in largest
        ),
    )


def _holder_weight(claim: MemoryClaim) -> tuple[int, float]:
    """Sort key putting run-alone holders first, then the largest reservation."""
    return (0, 0.0) if claim.run_alone else (1, -(claim.reservation_mb or 0.0))


def _reserves(claim: MemoryClaim) -> bool:
    """True when *claim* holds memory: a run-alone claim or a reservation above zero."""
    return claim.run_alone or bool(claim.reservation_mb)


def _reserves_ordinarily(claim: MemoryClaim) -> bool:
    """True when *claim* holds memory and is not a companion claim."""
    return _reserves(claim) and not claim.companion


def _fits_among(claim: MemoryClaim, running: list[MemoryClaim], budget_mb: float) -> bool:
    """True when *claim* can start beside the *running* claims under *budget_mb*."""
    seats_held = Counter(tool for other in running for tool, _cap in other.seats)
    if any(seats_held[tool] >= cap for tool, cap in claim.seats):
        return False
    ordinary_running = any(_reserves_ordinarily(other) for other in running)
    if claim.run_alone:
        return not ordinary_running
    if any(other.run_alone for other in running):
        return False
    if not claim.companion and not ordinary_running:
        return True  # only companions hold: the first other claim starts
    used_mb = sum(other.reservation_mb or 0.0 for other in running)
    return used_mb + (claim.reservation_mb or 0.0) <= budget_mb


def _finish(start: float | None, wall_s: float | None) -> float:
    """Estimated finish epoch of a claim started at *start*; ``inf`` when either is unknown."""
    return math.inf if start is None or wall_s is None else start + wall_s


@dataclass
class _MemoryLoad:
    """Running total of the memory claims virtually started so far."""

    budget_mb: float
    used_mb: float = 0.0
    running: int = 0  # ordinary claims that reserve memory (a zero or companion holder is none)
    alone: bool = False
    stopped: bool = False  # an earlier claim could not start: only a short claim may jump it
    seats_used: dict[str, int] = field(default_factory=dict[str, int])
    # Each running claim with its estimated finish (epoch seconds; inf = unknown).
    finishes: list[tuple[float, MemoryClaim]] = field(default_factory=list)
    # When the front (first blocked) claim can start; None = unknown, nothing jumps.
    front_start: float | None = None

    def admit(self, claim: MemoryClaim | None, now: float) -> bool:
        """Start *claim* if it fits (True); else block it, and the claims behind it unless they jump."""
        if claim is None:
            self.stopped = True  # unreadable: an unknown reservation never fits
            self.front_start = None
            return False
        if self.stopped:
            if not self._may_jump(claim, now):
                return False
        elif not self._fits(claim):
            if claim.companion:
                return False  # a waiting companion is never the front: it blocks nobody
            self.stopped = True
            self.front_start = self._earliest_start(claim, now)
            return False
        self.start(claim, _finish(now, claim.wall_s))
        return True

    def start(self, claim: MemoryClaim, finish: float) -> None:
        """Count *claim* as running until the estimated *finish* (``inf`` = unknown)."""
        self.running += _reserves_ordinarily(claim)
        self.alone = self.alone or claim.run_alone
        self.used_mb += claim.reservation_mb or 0.0
        for tool, _cap in claim.seats:
            self.seats_used[tool] = self.seats_used.get(tool, 0) + 1
        self.finishes.append((finish, claim))

    def _fits(self, claim: MemoryClaim) -> bool:
        """True when *claim* fits the memory budget and a seat of each tool it holds is free."""
        if any(self.seats_used.get(tool, 0) >= cap for tool, cap in claim.seats):
            return False
        if claim.run_alone:
            return self.running == 0
        if self.alone:
            return False
        if not claim.companion and self.running == 0:
            return True  # only companions hold: the first other claim starts
        # A companion claim fits under its own budget: a waiting one is judged as its owner is.
        budget_mb = claim.budget_mb if claim.companion else self.budget_mb
        return self.used_mb + (claim.reservation_mb or 0.0) <= budget_mb

    def _earliest_start(self, front: MemoryClaim, now: float) -> float | None:
        """When *front* can start under its own budget, from the running claims' finishes.

        ``now`` when it already fits; else the first finish, in finish order,
        after which it fits. An unknown finish reached first makes the answer
        unknown (None).
        """
        running = [held for _finish, held in self.finishes]
        if _fits_among(front, running, front.budget_mb):
            return now
        released = sorted(self.finishes, key=lambda pair: pair[0])
        # Once every claim has finished the front fits, so a finish is always found.
        finish = next(
            when
            for index, (when, _held) in enumerate(released)
            if _fits_among(front, [held for _f, held in released[index + 1 :]], front.budget_mb)
        )
        return None if math.isinf(finish) else finish

    def _may_jump(self, claim: MemoryClaim, now: float) -> bool:
        """True when *claim* fits now and is estimated to finish before the front starts."""
        return (
            self.front_start is not None
            and claim.wall_s is not None
            and self._fits(claim)
            and now + claim.wall_s < self.front_start
        )


def _memory_queue(
    holders: list[tuple[MemoryClaim | None, float | None]],
    ahead: list[MemoryClaim | None],
    claim: MemoryClaim,
    now: float,
) -> tuple[bool, int]:
    """Where *claim* stands behind running *holders* and the waiters *ahead*.

    Each holder comes with its start epoch (``None`` when unstamped). Returns
    ``(starts_now, position)``. The walk starts each claim in order
    against *claim*'s budget: a run-alone claim starts only when no ordinary
    claim reserves memory (companion holders do not count), any other claim only
    when nothing run-alone runs and its reservation still fits, or when only
    companion holders reserve memory. A companion claim that does not fit is
    skipped: it never becomes the front. A claim holding a seat of a licensed tool also needs a free seat
    under its own Seat Cap, which each claim carries (a cap changed while Jobs
    wait applies to new claims only). The first claim that does not start (the
    front) blocks the claims behind it, except a claim with its own wall-clock
    estimate that fits now and is estimated to finish before the front can
    start: it jumps ahead. A holder's
    finish is its start plus its estimate, unknown without either; an unknown
    finish the front waits for means nothing jumps. ``None`` (an unreadable
    claim) never fits and ends jumping, so an unknown reservation cannot
    overcommit. ``starts_now`` is the promotion rule (no waiter ahead starts
    first, and the claim starts); ``position`` counts the blocked claims ahead
    of it, and is 0 for a claim that fits.
    """
    load = _MemoryLoad(claim.budget_mb)
    for held, started in holders:
        if held is None:
            load.stopped = True
        else:
            load.start(held, _finish(started, held.wall_s))
    started_ahead = [load.admit(waiting, now) for waiting in ahead]
    fits = load.admit(claim, now)
    blocked_ahead = started_ahead.count(False)
    return (fits and not any(started_ahead), 0 if fits else blocked_ahead)


@dataclass
class TokenState:
    state: str
    position: int | None = None  # 0-based queue position when QUEUED
    # Why this entry was queued again (a requeued Job's claim); ``None`` for a first claim.
    reason: str | None = None


@dataclass(frozen=True)
class LeaseHealth:
    """Token-scoped signal that renewal no longer protects a holder."""

    lost: threading.Event


@dataclass
class SlotToken:
    """Handle for one admission request — one entry file, owned by one PID.

    ``path`` tracks the entry through its waiter→holder rename. Treat as
    opaque outside this module; only the creating process should promote or
    release it (reaping stale entries is the one sanctioned exception).
    """

    job_class: str
    role: str
    priority: int
    seq: int
    pid: int
    n: int
    argv: list[str]
    created_at: str
    timeout_s: float | None
    path: Path
    # Stamped into the entry payload when the owner promotes itself to holder
    # (None while waiting, or for a holder that crashed between the rename and
    # the payload rewrite). The holder deadline anchors here, NOT at
    # created_at: queue wait must never count against the run budget.
    promoted_at: str | None = None
    schema_version: int = 1
    lease_id: str = ""
    execution_id: ExecutionId | None = None
    lease_state: str = LEASE_ACTIVE
    lease_generation: int = 0
    lease_expires_at: str | None = None
    owner_identity: ProcessIdentity | None = None
    owner_kind: str = "process"
    lease_health: LeaseHealth = field(default_factory=lambda: LeaseHealth(threading.Event()))
    memory: MemoryClaim | None = None  # set exactly for memory-class entries
    # Why this claim exists again after the Job's first attempt was killed
    # (``None`` for a first claim); shown to the caller while the Job waits.
    queue_reason: str | None = None
    job: JobLabel | None = None  # who the entry belongs to, for queue reports only
    # Memory-class holders only: the admission ordinal, one more than the
    # largest among the class's holders when this entry was promoted. It orders
    # overlapping Jobs by admission (entry names order by priority and
    # submission, which an interactive claim can disagree with).
    admitted: int | None = None

    @property
    def is_holder(self) -> bool:
        return _is_holder_path(self.path)


@dataclass(frozen=True)
class _WaiterRequest:
    cls_dir: Path
    priority: int
    pid: int
    argv: list[str]
    role: str
    timeout_s: float | None
    created: float
    execution_id: ExecutionId | None
    memory: MemoryClaim | None = None
    queue_reason: str | None = None
    job: JobLabel | None = None


def project_slots_dir(booley_dir: Path) -> Path:
    """The slot store of the Project whose Booley Directory is *booley_dir*, with no override."""
    return booley_dir / "runtime" / "jobs" / "slots"


def slots_dir(booley_dir: Path | None = None) -> Path | None:
    """Root of the shared slot store, or None when no project is resolvable.

    Project-scoped (``.booley/runtime/jobs/slots/``): admission
    arbitrates across every workload in the Sandbox, so claims share one
    directory independently of each execution's Job records. ``BOOLEY_SLOTS_DIR`` overrides for tests and doctor probes.
    """
    env = os.environ.get("BOOLEY_SLOTS_DIR", "")
    if env:
        return Path(env)
    try:
        from booley.runtime.booley_dir import resolve_common_booley_dir

        project = booley_dir if booley_dir is not None else resolve_common_booley_dir()
    except (OSError, ValueError, RuntimeError):
        return None
    return project_slots_dir(project)


def _default_execution_is_terminal(execution_id: str) -> bool:
    try:
        payload = read_json(execution_paths(execution_id).record)
    except OSError:
        # A sharing error that outlasted the read retries: terminal is not
        # proven, so release keeps the slot and reaping falls through to the
        # ordinary staleness checks. release() must never raise.
        return False
    return bool(
        payload is not None
        and payload.get("state") == "terminal"
        and payload.get("tree_terminal") is True
    )


def _default_cancel_execution(execution_id: str) -> None:
    request_cancellation(execution_paths(execution_id))


def _default_recover_process_owner(identity: ProcessIdentity | None) -> bool:
    return identity is not None and recover_process_owner(identity)


def _default_inherited_execution_verifier(
    candidate: object, root: Path, owner_pid: int
) -> ExecutionId | None:
    try:
        from booley.runtime.booley_dir import resolve_booley_dir, resolve_common_booley_dir

        canonical_root = resolve_common_booley_dir() / "runtime" / "jobs" / "slots"
        if root.resolve() != canonical_root.resolve():
            return None
        # Execution records stay with their writers' checkout resolver (see #2603).
        return verify_inherited_execution_id(
            candidate, booley_dir=resolve_booley_dir(), owner_pid=owner_pid
        )
    except (OSError, RuntimeError, ValueError):
        return None


@dataclass(frozen=True)
class _GateOwner:
    """The process a promotion or lease gate names as its holder."""

    pid: int
    identity: ProcessIdentity | None

    def to_text(self) -> str:
        identity = self.identity.to_payload() if self.identity is not None else None
        return json.dumps({"pid": self.pid, "owner_identity": identity}) + "\n"


@dataclass(frozen=True)
class SlotRecovery:
    """Injected recovery boundary for durable execution and process owners."""

    execution_is_terminal: Callable[[str], bool] = _default_execution_is_terminal
    cancel_execution: Callable[[str], None] = _default_cancel_execution
    recover_execution: Callable[[str], bool] = recover_execution
    recover_process_owner: Callable[[ProcessIdentity | None], bool] = (
        _default_recover_process_owner
    )


def _entry_name(state: str, priority: int, seq: int, pid: int, n: int) -> str:
    # Fixed-width priority/sequence/PID fields preserve admission precedence;
    # the lease-derived suffix gives otherwise equal entries a stable total order.
    return f"{state}-p{priority}-s{seq:010d}-{pid:010d}-{n:04d}.json"


def _is_holder_path(path: Path) -> bool:
    return path.name.startswith(f"{_HOLDER}-")


def _parse_entry_name(name: str) -> tuple[str, int, int, int, int] | None:
    """(state, priority, seq, pid, n) from an entry filename, or None."""
    stem = name.removesuffix(".json")
    parts = stem.split("-")
    if len(parts) != 5 or parts[0] not in (_HOLDER, _WAITER):
        return None
    try:
        return (
            parts[0],
            int(parts[1].removeprefix("p")),
            int(parts[2].removeprefix("s")),
            int(parts[3]),
            int(parts[4]),
        )
    except ValueError:
        return None


def _optional_text(value: object) -> str | None:
    """A payload field that is a non-empty string, else ``None`` (absent or foreign)."""
    return value if isinstance(value, str) and value else None


def _utc_stamp(epoch: float) -> str:
    return rfc3339_from_epoch(epoch)


_default_pid_alive = is_pid_alive


class SlotStore:
    """Claim/queue/release over one slots directory. Safe across processes.

    All clock/PID/proc access is injected so tests can simulate multi-process
    interleavings deterministically; production callers use the defaults.
    """

    def __init__(
        self,
        root: Path,
        *,
        is_pid_alive: Callable[[int], bool] = _default_pid_alive,
        read_cmdline: Callable[[int], list[str] | None] = _proc_cmdline,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        capture_identity: Callable[[int], ProcessIdentity | None] = capture_process_identity,
        observe_identity: Callable[[ProcessIdentity], ProcessObservation] = observe_process,
        recovery: SlotRecovery | None = None,
        inherited_execution_verifier: Callable[
            [object, Path, int], ExecutionId | None
        ] = _default_inherited_execution_verifier,
    ) -> None:
        self.root = root
        self._is_pid_alive = is_pid_alive
        self._read_cmdline = read_cmdline
        self._now = now
        self._sleep = sleep
        self._capture_identity = capture_identity
        self._observe_identity = observe_identity
        self._recovery = recovery or SlotRecovery()
        self._inherited_execution_verifier = inherited_execution_verifier
        self._auto_renew = now is time.time and sleep is time.sleep
        self._renewals: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._deferred_releases: dict[str, threading.Event] = {}
        self._deferred_gate = threading.Lock()
        self._process_owner: _GateOwner | None = None
        self._break_lock_warned = False

    # ---------------------------------------------------------------- submit

    def submit(
        self,
        job_class: str,
        *,
        pid: int,
        argv: list[str] | None = None,
        role: str = ROLE_INTERACTIVE,
        timeout_s: float | None = None,
        execution_id: str | ExecutionId | None = None,
        memory: MemoryClaim | None = None,
        queue_reason: str | None = None,
        job: JobLabel | None = None,
    ) -> SlotToken:
        """Join *job_class* admission; the claim waits in queue order until admitted or withdrawn.

        Only the memory class admits, and it needs a *memory* claim.
        *queue_reason* names why a requeued Job claims again; it is stored in
        the entry so the caller can read it while the Job waits or runs.
        A claim that opens as zero is refused (only a holder reaches zero),
        except a companion claim, which may reserve nothing.
        """
        if memory is None or job_class != CLASS_MEMORY:
            raise ValueError(f"only the {CLASS_MEMORY} class admits, with a memory claim")
        if not (_reserves(memory) or memory.companion):
            raise ValueError("a new memory claim must reserve memory or run alone")
        cls_dir = self.root / job_class
        cls_dir.mkdir(parents=True, exist_ok=True)
        self.reap(job_class)

        priority = _ROLE_PRIORITY.get(role, _ROLE_PRIORITY[ROLE_BACKGROUND])
        created = self._now()
        linked_execution = self._linked_execution(execution_id, pid)
        token = self._create_entry(
            _WaiterRequest(
                cls_dir=cls_dir,
                priority=priority,
                pid=pid,
                argv=argv or [],
                role=role,
                timeout_s=timeout_s,
                created=created,
                execution_id=linked_execution,
                memory=memory,
                queue_reason=queue_reason,
                job=job,
            )
        )
        return token

    def _linked_execution(
        self, execution_id: str | ExecutionId | None, pid: int
    ) -> ExecutionId | None:
        """The execution a new entry links to: the given one, else the verified inherited one."""
        if execution_id is not None:
            return ExecutionId(execution_id)
        inherited = os.environ.get(RUNTIME_EXECUTION_ENV) or None
        if inherited is None:
            return None
        return self._inherited_execution_verifier(inherited, self.root, pid)

    # ---------------------------------------------------------- memory class

    def _names_before(self, token: SlotToken) -> list[str]:
        """Memory entries that rank before *token*'s own entry, in order."""
        own = token.path.name
        return [name for name in sorted(self._live_entry_names(CLASS_MEMORY)) if name < own]

    def _memory_view(
        self, claim: MemoryClaim, list_ahead: Callable[[], list[str]]
    ) -> tuple[bool, int]:
        """``_memory_queue`` over the entries *list_ahead* names, read fresh from disk.

        Reads each entry directly (not through ``snapshot``, which skips
        unreadable ones): an entry that cannot be read, or carries no claim,
        is ``None`` and fails closed. An entry that vanished between the
        listing and the read was released or promoted meanwhile, so the
        listing is stale: list again (a bounded number of times) instead of
        counting it as blocking.
        """
        for _attempt in range(_MEMORY_VIEW_ATTEMPTS):
            holders: list[tuple[MemoryClaim | None, float | None]] = []
            waiters: list[MemoryClaim | None] = []
            vanished = False
            for name in list_ahead():
                path = self.root / CLASS_MEMORY / name
                held, started = self._read_memory_entry(path)
                vanished = vanished or (held is None and not path.exists())
                if name.startswith(f"{_HOLDER}-"):
                    holders.append((held, started))
                else:
                    waiters.append(held)
            if not vanished:
                break
        return _memory_queue(holders, waiters, claim, self._now())

    def _read_memory_entry(self, path: Path) -> tuple[MemoryClaim | None, float | None]:
        """An entry's claim and start epoch (promotion, else creation), ``(None, None)`` if unreadable."""
        try:
            token = self._load_token(path)
        except OSError:
            return None, None
        if token is None:
            return None, None
        return token.memory, parse_stamp(token.promoted_at) or parse_stamp(token.created_at)

    def _queue_position(self, token: SlotToken) -> int | None:
        """0-based queue position of a waiter, or None when its turn has come."""
        if token.memory is None:
            return 0  # an entry without a readable claim never starts: fail closed
        starts_now, position = self._memory_view(token.memory, lambda: self._names_before(token))
        return None if starts_now else position

    def _create_entry(self, request: _WaiterRequest) -> SlotToken:
        """Create a uniquely-named waiter entry; atomic full-content appearance."""
        owner_identity = self._capture_identity(request.pid)
        lease_id = uuid.uuid4().hex
        payload_base = self._new_waiter_payload(request, lease_id, owner_identity)
        for _attempt in range(_ENTRY_CREATE_ATTEMPTS):
            seq = self._next_seq(request.cls_dir)
            # A lease owns its filename across waiter/holder transitions and
            # cancellation. Independent stores must never recycle that name.
            entry_number = int(lease_id, 16)
            name = _entry_name(_WAITER, request.priority, seq, request.pid, entry_number)
            path = request.cls_dir / name
            payload = dict(payload_base, seq=seq)
            tmp = request.cls_dir / f".{name}.{lease_id}.tmp"
            try:
                tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                os.link(tmp, path)
            except FileExistsError:
                continue
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
            return self._new_waiter_token(path, payload, request)
        raise RuntimeError("could not allocate a unique Job slot entry")

    @staticmethod
    def _new_waiter_payload(
        request: _WaiterRequest,
        lease_id: str,
        owner_identity: ProcessIdentity | None,
    ) -> dict:
        payload: dict = {
            "schema_version": SLOT_SCHEMA_VERSION,
            "role": request.role,
            "argv": request.argv,
            "created_at": _utc_stamp(request.created),
            "timeout_s": request.timeout_s,
            "lease_id": lease_id,
            "execution_id": request.execution_id,
            "owner_kind": "execution" if request.execution_id is not None else "process",
            "lease_state": LEASE_ACTIVE,
            "lease_generation": 0,
            "lease_expires_at": None,
            "owner_identity": owner_identity.to_payload() if owner_identity is not None else None,
        }
        if request.memory is not None:
            payload["memory"] = request.memory.to_payload()
        if request.queue_reason is not None:
            payload["queue_reason"] = request.queue_reason
        if request.job is not None:
            payload["job"] = request.job.to_payload()
        return payload

    def _new_waiter_token(
        self,
        path: Path,
        payload: dict,
        request: _WaiterRequest,
    ) -> SlotToken:
        published = _parse_entry_name(path.name)
        if published is None:
            raise ValueError(f"invalid published waiter entry name: {path.name}")
        return SlotToken(
            job_class=path.parent.name,
            role=payload["role"],
            priority=request.priority,
            seq=payload["seq"],
            pid=request.pid,
            n=published[4],
            argv=list(payload["argv"]),
            created_at=payload["created_at"],
            timeout_s=payload["timeout_s"],
            path=path,
            schema_version=SLOT_SCHEMA_VERSION,
            lease_id=payload["lease_id"],
            execution_id=payload["execution_id"],
            owner_identity=ProcessIdentity.from_payload(payload["owner_identity"]),
            owner_kind=payload["owner_kind"],
            memory=request.memory,
            queue_reason=request.queue_reason,
            job=request.job,
        )

    def _next_seq(self, cls_dir: Path) -> int:
        """Max seq among live entries + 1. Ties across processes are fine —
        the (seq, pid, n) name suffix keeps the order total."""
        top = 0
        for path in cls_dir.glob("*.json"):
            parsed = _parse_entry_name(path.name)
            if parsed is not None:
                top = max(top, parsed[2])
        return top + 1

    # ------------------------------------------------------- refresh/promote

    def refresh(self, token: SlotToken) -> TokenState:
        """Re-derive *token*'s state, promoting it to holder when its turn comes.

        Call repeatedly while QUEUED. Only the token's owner may call this —
        promotion renames the entry file, and that is an owner-only act.
        """
        self.reap(token.job_class)
        if not token.path.exists():
            return TokenState(LOST)
        if token.is_holder:
            return TokenState(HOLDING)
        return self._refresh_waiter(token)

    def _refresh_waiter(self, token: SlotToken) -> TokenState:
        """Recheck and promote one waiter without an observation race."""
        position = self._queue_position(token)
        if position is not None:
            return TokenState(QUEUED, position=position)

        with self._promotion_gate(token) as acquired:
            if not acquired:
                return TokenState(QUEUED, position=0)

            # Submissions do not take the gate, so repeat both cleanup and
            # ranking inside it. A newly-created earlier waiter must be
            # visible before this process commits its promotion.
            self.reap(token.job_class)
            if not token.path.exists():
                return TokenState(LOST)
            position = self._queue_position(token)
            if position is not None:
                return TokenState(QUEUED, position=position)

            return self._promote(token)

    def _promote(self, token: SlotToken) -> TokenState:
        """Rename one eligible waiter while its class promotion gate is held."""
        if token.memory is not None:
            token.admitted = self._next_admission_ordinal()
        holder_path = token.path.with_name(
            _entry_name(_HOLDER, token.priority, token.seq, token.pid, token.n)
        )
        # The lease gate spans the stamp and the rename, so a cancel or a
        # recovery claim either lands before the stamp (which then sees it)
        # or meets the promotion in progress and is refused.
        with self._lease_gate(token) as acquired:
            if not acquired:
                return TokenState(QUEUED, position=0)
            refused = self._stamp_promoted(token)
            if refused is not None:
                return refused
            if not self._rename_entry(token.path, holder_path):
                # A Windows sharing violation is not cancellation. If the
                # waiter still exists, leave it queued and retry next poll.
                return TokenState(QUEUED, position=0) if token.path.exists() else TokenState(LOST)
            token.path = holder_path
            return TokenState(HOLDING)

    def _next_admission_ordinal(self) -> int:
        """One more than the largest ordinal among memory holders (1 when none)."""
        holders, _waiters = self.observe(CLASS_MEMORY)
        return 1 + max((holder.admitted or 0 for holder in holders), default=0)

    def _promotion_gate(self, token: SlotToken) -> contextlib.AbstractContextManager[bool]:
        """Cross-process gate around the promotion decision, owned by the promoting process.

        Only a token's owner promotes it, so the token's own PID and identity name the gate.
        """
        owner = _GateOwner(token.pid, token.owner_identity)
        return self._gate(token.path.parent / _PROMOTION_GATE_NAME, owner)

    def _rename_entry(self, source: Path, target: Path) -> bool:
        """Promote an entry through transient Windows sharing violations."""
        return self._retry_entry_io(lambda: source.rename(target))

    def _retry_entry_io(
        self,
        operation: Callable[[], object],
        *,
        on_exhausted: Callable[[], None] | None = None,
    ) -> bool:
        """Run *operation*, retrying any ``PermissionError`` within the entry-I/O bound.

        True on success; False on any other ``OSError``, or when every attempt
        was denied (then *on_exhausted* runs, e.g. to log).
        """
        for attempt in range(_ENTRY_IO_ATTEMPTS):
            try:
                operation()
            except PermissionError:
                if attempt + 1 < _ENTRY_IO_ATTEMPTS:
                    self._sleep(_ENTRY_IO_RETRY_SECONDS)
            except OSError:
                return False
            else:
                return True
        if on_exhausted is not None:
            on_exhausted()
        return False

    def _token_payload(self, token: SlotToken) -> dict:
        payload: dict = {
            "schema_version": token.schema_version,
            "role": token.role,
            "argv": token.argv,
            "created_at": token.created_at,
            "timeout_s": token.timeout_s,
            "seq": token.seq,
            "promoted_at": token.promoted_at,
            "lease_id": token.lease_id,
            "execution_id": token.execution_id,
            "owner_kind": token.owner_kind,
            "lease_state": token.lease_state,
            "lease_generation": token.lease_generation,
            "lease_expires_at": token.lease_expires_at,
            "owner_identity": (
                token.owner_identity.to_payload() if token.owner_identity is not None else None
            ),
        }
        if token.memory is not None:
            payload["memory"] = token.memory.to_payload()
        if token.queue_reason is not None:
            payload["queue_reason"] = token.queue_reason
        if token.job is not None:
            payload["job"] = token.job.to_payload()
        if token.admitted is not None:
            payload["admitted"] = token.admitted
        return payload

    def _rewrite_token(self, token: SlotToken) -> bool:
        """Replace *token*'s entry file with its current payload.

        Rule for every caller: hold the entry's lease gate and have just
        reread the entry under it, refusing unless it is still an ``active``
        entry of the expected lease at that path. That is the caller's own
        lease for promotion, renewal, and shrinking; for
        ``_begin_execution_recovery``, a third party, the path itself fixes
        the lease because an entry name embeds it (``entry_number =
        int(lease_id, 16)`` in ``_create_entry``). Every unlink of a live
        entry (release, withdrawal) holds the same gate, so the exists check
        below fails the rewrite once a gated unlink won, and no unlink can
        land between that check and the replace. Reap is the one ungated
        unlinker; see the comment in ``reap``.
        """
        tmp = token.path.with_name(f".{token.path.name}.{os.getpid()}.rewrite.tmp")
        try:
            tmp.write_text(json.dumps(self._token_payload(token)) + "\n", encoding="utf-8")
            if not token.path.exists():
                tmp.unlink(missing_ok=True)
                return False
            replace_file(tmp, token.path, sleep=self._sleep)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            return False
        return True

    def _stamp_promoted(self, token: SlotToken) -> TokenState | None:
        """Stamp complete holder metadata before the waiter→holder rename.

        Returns ``None`` once stamped, else the state promotion must report:
        LOST when the entry is gone or corrupt (a cancel won; ``acquire``'s
        release removes the junk), QUEUED when it is
        unreadable, not ``active`` (recovery claimed it; reap removes it), or
        could not be rewritten. The caller holds the lease gate and the entry
        is reread under it; the name embeds the lease, so the file at
        ``token.path`` can only carry this owner's lease.

        The holder deadline (``_is_stale``) anchors at promotion, not entry
        creation — an entry can legitimately queue for longer than its own run
        budget, and charging that wait against the deadline would reap a
        *live* holder. Writing before rename makes a crash leave either a
        waiter or a fully described holder, never an immortal half-promotion.
        """
        promoted = self._now()
        try:
            current = self._load_token(token.path)
        except OSError:
            return TokenState(QUEUED, position=0)
        if current is None:
            return TokenState(LOST)
        if current.lease_state != LEASE_ACTIVE:
            return TokenState(QUEUED, position=0)
        token.promoted_at = _utc_stamp(promoted)
        token.lease_expires_at = _utc_stamp(promoted + LEASE_DURATION_SECONDS)
        return None if self._rewrite_token(token) else TokenState(QUEUED, position=0)

    def acquire(
        self,
        job_class: str,
        *,
        pid: int,
        argv: list[str] | None = None,
        role: str = ROLE_INTERACTIVE,
        timeout_s: float | None = None,
        poll_interval: float = 1.0,
        on_queued: Callable[[int | None], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        narrate_interval_s: float = NARRATE_INTERVAL_SECONDS,
        execution_id: str | ExecutionId | None = None,
        on_submitted: Callable[[SlotToken], None] | None = None,
        memory: MemoryClaim | None = None,
        queue_reason: str | None = None,
        job: JobLabel | None = None,
    ) -> SlotToken:
        """Submit and wait for a holder, withdrawing on cancellation or failure.

        Queue narration repeats when its interval elapses. ``should_abort``
        lets signal handlers end a wait without leaking the caller's entry.
        """
        token = self.submit(
            job_class,
            pid=pid,
            argv=argv,
            role=role,
            timeout_s=timeout_s,
            execution_id=execution_id,
            memory=memory,
            queue_reason=queue_reason,
            job=job,
        )
        if on_submitted is not None:
            on_submitted(token)
        try:
            return self._wait_for_promotion(
                token,
                should_abort=should_abort,
                on_queued=on_queued,
                poll_interval=poll_interval,
                narrate_interval_s=narrate_interval_s,
            )
        except BaseException:
            self.release(token)  # idempotent; no-op on the LOST path
            raise

    def _wait_for_promotion(
        self,
        token: SlotToken,
        *,
        should_abort: Callable[[], bool] | None,
        on_queued: Callable[[int | None], None] | None,
        poll_interval: float,
        narrate_interval_s: float,
    ) -> SlotToken:
        last_position: int | None = None
        last_narrated: float | None = None
        while token.path.exists():
            if should_abort is not None and should_abort():
                raise ClaimAbortedError(
                    f"aborted while waiting for a {token.job_class} slot (shutdown requested)"
                )
            state = self.refresh(token)
            if state.state == HOLDING:
                self._start_renewal(token)
                return token
            if state.state == LOST:
                break
            now = self._now()
            due = last_narrated is None or now - last_narrated >= narrate_interval_s
            if on_queued is not None and (state.position != last_position or due):
                last_position, last_narrated = state.position, now
                on_queued(state.position)
            self._sleep(poll_interval)
        raise ClaimLostError(
            f"{token.job_class} queue entry vanished while waiting — the job was cancelled"
        )

    # ------------------------------------------------------- release/inspect

    def release(self, token: SlotToken) -> None:
        """Release a slot or withdraw a queued entry. Idempotent, never raises."""
        with self._deferred_gate:
            deferred = self._deferred_releases.get(token.lease_id)
            if deferred is not None and not deferred.is_set():
                return
            if deferred is not None:
                self._deferred_releases.pop(token.lease_id, None)
        renewal = self._renewals.pop(token.lease_id, None)
        if renewal is not None:
            stop, thread = renewal
            stop.set()
            if thread is not threading.current_thread():
                thread.join(timeout=_RENEWAL_JOIN_SECONDS)
        if self._release_entry(token):
            return
        logger.warning(
            "Release of slot entry %s deferred: it could not take its lease gate. Retrying in the "
            "background; if this process exits first, the entry keeps its reservation "
            "until reap removes it",
            token.path.name,
        )
        try:
            threading.Thread(
                target=self._retry_release,
                args=(token,),
                name=f"booley-slot-release-{token.lease_id[:8]}",
                daemon=True,
            ).start()
        except RuntimeError:
            self._warn_release_gave_up(token)

    def _release_entry(self, token: SlotToken) -> bool:
        """Release under the lease gate: False only when the gate could not be taken.

        True also covers a gate that could not be taken because the entry is
        already gone (nothing is left to release). False means the gate could
        not be taken while the entry still exists.
        """
        with self._lease_gate(token) as acquired:
            if not acquired:
                return not token.path.exists()
            try:
                current = self._load_token(token.path)
            except OSError:
                current = None  # unreadable: still our own entry to release
            if current is None:
                self._unlink_entry(token.path)
                return True
            if current.lease_id and current.lease_id != token.lease_id:
                return True
            if (
                current.lease_state == LEASE_CANCELLING
                and current.execution_id is not None
                and not self._recovery.execution_is_terminal(current.execution_id)
            ):
                return True
            self._unlink_entry(token.path)
            return True

    def _retry_release(self, token: SlotToken) -> None:
        """Bounded background retry of a release whose lease gate was busy."""
        for _attempt in range(_RELEASE_RETRY_ATTEMPTS):
            self._sleep(_RELEASE_RETRY_SECONDS)
            if self._release_entry(token):
                return
        self._warn_release_gave_up(token)

    @staticmethod
    def _warn_release_gave_up(token: SlotToken) -> None:
        logger.warning(
            "Could not release slot entry %s: it could not take its lease gate; "
            "the entry keeps its reservation until reaped",
            token.path.name,
        )

    def defer_release_until(self, token: SlotToken, terminal: threading.Event) -> None:
        """Keep one holder unavailable until its escaped owner actually exits."""
        with self._deferred_gate:
            current = self._deferred_releases.get(token.lease_id)
            if current is not None and current is not terminal:
                raise RuntimeError("slot token already has another deferred release")
            self._deferred_releases[token.lease_id] = terminal
        monitor = threading.Thread(
            target=self._finish_deferred_release,
            args=(token, terminal),
            name=f"booley-deferred-slot-{token.lease_id[:8]}",
            daemon=True,
        )
        monitor.start()

    def _finish_deferred_release(self, token: SlotToken, terminal: threading.Event) -> None:
        terminal.wait()
        self.release(token)

    def renew(self, token: SlotToken) -> bool:
        """Extend one active holder lease; True only when this call extended it.

        Refuses after recovery claimed the lease or once it expired, and then sets
        ``lease_health.lost``. A lease gate held elsewhere refuses without
        setting it: the lease still holds until it expires, so the renewal
        thread keeps retrying until then (two consecutive missed attempts are
        survived at the current lease and interval lengths; the third finds the
        lease expired).
        """
        with self._lease_gate(token) as acquired:
            if not acquired:
                # Another process holds the gate (a reaper, or this holder's own
                # release or shrink), or the gate could not be created at all: the
                # lease is untouched, so the next renewal retries. It is lost
                # only once it has expired, whichever the cause.
                if self._lease_expired(token):
                    token.lease_health.lost.set()
                return False
            try:
                current = self._load_token(token.path)
            except OSError:  # unreadable through the whole shared retry budget
                current = None
            if (
                current is None
                or not current.is_holder
                or current.lease_id != token.lease_id
                or current.lease_state != LEASE_ACTIVE
                or self._lease_expired(current)
            ):
                token.lease_health.lost.set()
                return False
            current.lease_generation += 1
            current.lease_expires_at = _utc_stamp(self._now() + LEASE_DURATION_SECONDS)
            if not self._rewrite_token(current):
                token.lease_health.lost.set()
                return False
            token.lease_generation = current.lease_generation
            token.lease_expires_at = current.lease_expires_at
            return True

    def shrink_reservation(self, token: SlotToken, reservation_mb: float) -> bool:
        """Lower an active holder's Memory Reservation; never raise it (ADR 0087).

        Returns True only when the on-disk claim now carries *reservation_mb*.
        Refuses, writing nothing, when the entry is not this lease's active
        holder (recovery may have claimed it), carries no sized claim, or
        *reservation_mb* is negative or not lower than the held value. Zero keeps
        the lease and also works from a run-alone claim. Waiters
        read the claim fresh on their next poll, so a lower claim admits them.
        """
        with self._lease_gate(token) as acquired:
            if not acquired:
                return False
            try:
                current = self._load_token(token.path)
            except OSError:
                return False
            if (
                current is None
                or not current.is_holder
                or current.lease_id != token.lease_id
                or current.lease_state != LEASE_ACTIVE
                or current.memory is None
                or reservation_mb < 0
                or not self._lowers(current.memory, reservation_mb)
            ):
                return False
            lowered = replace(current.memory, reservation_mb=reservation_mb, run_alone=False)
            current.memory = lowered
            if not self._rewrite_token(current):
                return False
            token.memory = lowered
            return True

    @staticmethod
    def _lowers(held: MemoryClaim, reservation_mb: float) -> bool:
        """True when *reservation_mb* is a drop to zero or strictly under a sized *held* claim."""
        if reservation_mb == 0:
            return _reserves(held)
        return held.reservation_mb is not None and reservation_mb < held.reservation_mb

    def _start_renewal(self, token: SlotToken) -> None:
        if not self._auto_renew or not token.lease_id or token.lease_id in self._renewals:
            return
        stop = threading.Event()

        def maintain() -> None:
            while not stop.wait(LEASE_RENEW_INTERVAL_SECONDS):
                self.renew(token)
                if token.lease_health.lost.is_set():
                    return

        thread = threading.Thread(target=maintain, name=f"booley-lease-{token.lease_id[:8]}")
        thread.daemon = True
        self._renewals[token.lease_id] = (stop, thread)
        thread.start()

    @contextlib.contextmanager
    def _lease_gate(self, token: SlotToken) -> Iterator[bool]:
        if not token.lease_id:
            yield True  # legacy entries never race renewal
            return
        # Any process takes a lease's gate (its holder, a releaser, a reaper), so
        # the gate names this process rather than the token's owner.
        gate = token.path.parent / f".{token.lease_id}.lease.lock"
        with self._gate(gate, self._gate_owner()) as acquired:
            yield acquired

    def _gate_owner(self) -> _GateOwner:
        """This process as a gate owner; recaptured after a fork or a failed capture."""
        pid = os.getpid()
        owner = self._process_owner
        if owner is None or owner.pid != pid or owner.identity is None:
            owner = _GateOwner(pid, self._capture_identity(pid))
            self._process_owner = owner
        return owner

    @contextlib.contextmanager
    def _gate(self, gate: Path, owner: _GateOwner) -> Iterator[bool]:
        """Hold *gate* for *owner* within the entry-I/O bound; yield whether it was taken."""
        acquired = False
        for attempt in range(_ENTRY_IO_ATTEMPTS):
            try:
                acquired = self._create_gate(gate, owner)
            except OSError:
                break
            if acquired:
                break
            if not self._break_stale_gate(gate) and attempt + 1 < _ENTRY_IO_ATTEMPTS:
                self._sleep(_ENTRY_IO_RETRY_SECONDS)
        try:
            yield acquired
        finally:
            if acquired:
                # Never another process's gate: a gate is broken only once its
                # owner is gone, and this owner is alive to release it. The
                # shared sharing-error backoff (~1.1 s) keeps a reader on
                # Windows from leaking a gate this process would then wait on.
                with contextlib.suppress(OSError):
                    unlink_file(gate, missing_ok=True, sleep=self._sleep)

    @staticmethod
    def _create_gate(gate: Path, owner: _GateOwner) -> bool:
        """Publish *gate* with *owner* already written; False when another gate holds the name.

        Windows denies the link while a released gate's delete is still
        pending; that gate is busy too, not an error. A crash, or on Windows a
        scanner holding the tmp, between the write and the unlink leaves an
        orphaned tmp: accepted, like an entry tmp, because ``observe`` ignores
        it and it is one small file.
        """
        tmp = gate.with_name(f".{gate.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(owner.to_text(), encoding="utf-8")
            try:
                os.link(tmp, gate)
            except (FileExistsError, PermissionError):
                return False
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return True

    def _break_stale_gate(self, gate: Path) -> bool:
        """Remove *gate* if its owner is provably gone; True when this call removed it.

        The judgement is repeated under the class's break lock, and the gate is
        removed only there. Two breakers that judged the same gate stale would
        otherwise both remove "the gate", and the later removal would hit the
        fresh gate the first breaker had taken meanwhile. Under the lock the
        gate cannot change between judgement and removal: other breakers wait,
        new gates are created only while the name is free, and only a gate's
        owner releases it, which a stale gate's owner cannot do.
        """
        if not self._gate_is_stale(gate):
            return False
        with self._gate_break_lock(gate.parent) as locked:
            if not locked or not self._gate_is_stale(gate):
                return False
            if gate.is_dir():  # a lease gate made by mkdir before gates named their owner
                with contextlib.suppress(OSError):
                    gate.rmdir()
                return not gate.exists()
            return self._unlink_entry(gate)

    @contextlib.contextmanager
    def _gate_break_lock(self, directory: Path) -> Iterator[bool]:
        """Try the class's break lock; an OS lock, so a breaker that dies holding it releases it."""
        path = directory / _GATE_BREAK_LOCK_NAME
        with contextlib.ExitStack() as stack:
            try:
                handle = stack.enter_context(path.open("a+b"))
                acquire_file_lock(handle)
                stack.callback(release_file_lock, handle)
                locked = True
            except LockContentionError:  # another breaker is judging
                locked = False
            except OSError as exc:  # no stale gate in this class can be broken
                locked = False
                if not self._break_lock_warned:
                    self._break_lock_warned = True
                    logger.warning("Cannot take the gate break lock %s: %s", path, exc)
            yield locked

    def _gate_is_stale(self, gate: Path) -> bool:
        """True only when *gate*'s owner is provably gone: dead, a zombie, or its PID recycled.

        Uses holder reaping's process check, with no age rule: a gate appears
        with its owner written, so a dead owner's gate is broken at once. A
        gate without an owner (a bare PID or a lease-gate directory from an
        older release, or junk) is stale once old. An unreadable gate is not
        stale: its owner may be alive (#1905). A gate whose owner captured no
        identity degrades to PID liveness (the gate has no argv to compare), so
        a recycled PID keeps such a gate alive until that process exits.
        """
        if gate.is_dir():
            return self._is_old_junk(gate)
        try:
            data = gate.read_bytes()
        except OSError:
            return False
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:  # not UTF-8 or not JSON: junk, like a bare PID
            payload = None
        if isinstance(payload, dict) and type(payload.get("pid")) is int:
            identity = ProcessIdentity.from_payload(payload.get("owner_identity"))
            return self._process_is_gone(payload["pid"], identity)
        return self._is_old_junk(gate)

    def _unlink_entry(self, path: Path) -> bool:
        """Remove an entry, retrying transient Windows sharing violations."""
        return self._retry_entry_io(
            lambda: path.unlink(missing_ok=True),
            on_exhausted=lambda: logger.warning(
                "Could not remove slot entry after retries: %s", path.name
            ),
        )

    def observe(self, job_class: str) -> tuple[list[SlotToken], list[SlotToken]]:
        """Read slot entries without reaping, cancellation, recovery or lease renewal."""
        holders: list[SlotToken] = []
        waiters: list[SlotToken] = []
        directory = self.root / job_class
        if directory.is_symlink() or not directory.is_dir():
            return holders, waiters
        for path in sorted(directory.iterdir())[:4096]:
            if path.is_symlink():
                continue
            try:
                token = self._load_token(path)
            except (ValueError, OSError):
                continue
            if token is not None:
                (holders if token.is_holder else waiters).append(token)
        return holders, waiters

    def snapshot(self, job_class: str) -> tuple[list[SlotToken], list[SlotToken]]:
        """(holders, waiters) in scheduling order — for narration and doctor.

        Reaps first, so the view reflects only live claimants.
        """
        holders, waiters, _unreadable = self._scan(job_class)
        return holders, waiters

    def _scan(self, job_class: str) -> tuple[list[SlotToken], list[SlotToken], bool]:
        """Reaped view of *job_class* plus whether any live entry was unreadable."""
        self.reap(job_class)
        holders: list[SlotToken] = []
        waiters: list[SlotToken] = []
        unreadable = False
        cls_dir = self.root / job_class
        if not cls_dir.is_dir():
            return [], [], False
        for name in sorted(self._live_entry_names(job_class)):
            try:
                tok = self._load_token(cls_dir / name)
            except OSError:
                unreadable = True
                continue
            if tok is None:
                continue
            (holders if tok.is_holder else waiters).append(tok)
        return holders, waiters, unreadable

    def execution_token_absent(self, job_class: str, execution_id: str) -> bool:
        """Prove no entry of *job_class* carries *execution_id*; fail closed when unreadable."""
        holders, waiters, unreadable = self._scan(job_class)
        if unreadable:
            return False
        return all(candidate.execution_id != execution_id for candidate in (*holders, *waiters))

    def _load_token(self, path: Path) -> SlotToken | None:
        """Parse one entry: ``None`` = missing or corrupt, ``OSError`` = unreadable.

        An entry that stays unreadable through the shared retry budget (a
        Windows scanner or a racing rewrite) is not absent (#1905).
        """
        parsed = _parse_entry_name(path.name)
        if parsed is None:
            return None
        _state, priority, seq, pid, n = parsed
        payload = read_slot_entry(path, sleep=self._sleep)
        if payload is None or not isinstance(payload.get("argv", []), list):
            return None
        owner_identity = ProcessIdentity.from_payload(payload.get("owner_identity"))
        try:
            execution_id = ExecutionId(payload.get("execution_id"))
        except ValueError:
            execution_id = None
        return SlotToken(
            job_class=path.parent.name,
            role=payload.get("role", ROLE_INTERACTIVE),
            priority=priority,
            seq=seq,
            pid=pid,
            n=n,
            argv=list(payload.get("argv") or []),
            created_at=payload.get("created_at", ""),
            timeout_s=payload.get("timeout_s"),
            path=path,
            promoted_at=payload.get("promoted_at"),
            schema_version=payload.get("schema_version", 1),
            lease_id=payload.get("lease_id", ""),
            execution_id=execution_id,
            lease_state=payload.get("lease_state", LEASE_ACTIVE),
            lease_generation=payload.get("lease_generation", 0),
            lease_expires_at=payload.get("lease_expires_at"),
            owner_identity=owner_identity,
            owner_kind=payload.get("owner_kind", "legacy"),
            memory=MemoryClaim.from_payload(payload.get("memory")),
            queue_reason=_optional_text(payload.get("queue_reason")),
            job=JobLabel.from_payload(payload.get("job")),
            admitted=as_int(payload.get("admitted")),
        )

    def cancel_waiter(self, pid: int) -> bool:
        """Atomically cancel the QUEUED entry owned by *pid*. True on success.

        The unlink is the cancellation claim: it can only succeed while the
        entry still has its waiter name — promotion renames the file, so a
        child that became a holder in the meantime makes this fail (False)
        and the cancel must be refused. The unlink happens under the entry's
        lease gate, so a cancel either removes the waiter before any rewrite
        (which then fails) or returns False. A Windows sharing error is
        retried and, if it persists, refuses the withdrawal (False, logged).
        The cancelled child's ``acquire`` then sees LOST and raises instead of
        re-queueing.
        """
        _holders, waiters = self.snapshot(CLASS_MEMORY)
        for tok in waiters:
            if tok.pid == pid:
                return self._withdraw_waiter(tok, tok.path)
        return False

    def cancel_waiter_token(self, token: SlotToken) -> bool:
        """Withdraw exactly *token* while it is still queued."""
        # Read the path once: the owner's promotion reassigns ``token.path`` to
        # the holder name, and a cancel that waited for the gate must not then
        # withdraw the new holder.
        path = token.path
        if _is_holder_path(path):
            return False
        return self._withdraw_waiter(token, path)

    def _withdraw_waiter(self, token: SlotToken, path: Path) -> bool:
        """Unlink a queued entry under its lease gate: True when withdrawn.

        False when refused or lost, including a gate held by a promotion or a
        recovery claim in progress.

        Uses the shared winerror-filtered retry rather than ``_unlink_entry``'s
        any-``PermissionError`` loop on purpose: the error class decides the
        cancel outcome here. Only a missing file means promotion won the race;
        any other failure refuses the withdrawal and is logged, never mistaken
        for promotion.
        """
        with self._lease_gate(token) as acquired:
            if not acquired:
                return False
            try:
                current = self._load_token(path)
            except OSError:
                return False
            if current is None or current.lease_id != token.lease_id:
                return False
            try:
                unlink_file(path, sleep=self._sleep)
            except FileNotFoundError:
                return False  # promoted (renamed) or already removed: the race is lost
            except OSError as exc:
                logger.warning("Refusing to withdraw queued slot entry %s: %s", path.name, exc)
                return False
            return True

    def begin_token_recovery(self, token: SlotToken) -> bool:
        """Claim exact-token cancellation without releasing reusable capacity."""
        claimed = self._begin_execution_recovery(token, request_cancel=True)
        if claimed:
            token.lease_state = LEASE_CANCELLING
            token.lease_health.lost.set()
        return claimed

    def token_absent(self, token: SlotToken) -> bool:
        """Prove the exact lease ID is absent from its Job class."""
        holders, waiters = self.snapshot(token.job_class)
        if any(candidate.lease_id == token.lease_id for candidate in (*holders, *waiters)):
            return False
        # An entry that survived reaping but stayed unreadable through every
        # retry (e.g. a Windows scanner holding it) is not proof of absence.
        return not token.path.exists()

    def memory_wait(self, pid: int) -> MemoryWait | None:
        """What the memory waiter owned by *pid* waits for, or None when it is not one."""
        holders, waiters = self.snapshot(CLASS_MEMORY)
        for waiter in waiters:
            if waiter.pid == pid:
                return memory_wait(waiter, holders, waiters, self._now())
        return None

    def state_for_pid(self, pid: int) -> TokenState | None:
        """Admission state of the entry owned by *pid*, or None when absent.

        The submit/poll dispatch layer narrates a detached child's phase from
        this: a holder is genuinely RUNNING, a waiter is QUEUED at a position.
        Reads the memory class: the caller knows the PID (from the JobRecord).
        """
        holders, waiters = self.snapshot(CLASS_MEMORY)
        holding = [tok for tok in holders if tok.pid == pid]
        if holding:
            # A Simulation Record's work-item claims share the PID; any one may carry the reason.
            reason = next((tok.queue_reason for tok in holding if tok.queue_reason), None)
            return TokenState(HOLDING, reason=reason)
        for position, tok in enumerate(waiters):
            if tok.pid == pid:
                return TokenState(QUEUED, position=position, reason=tok.queue_reason)
        return None

    # ----------------------------------------------------------------- reap

    def _live_entry_names(self, job_class: str) -> list[str]:
        cls_dir = self.root / job_class
        if not cls_dir.is_dir():
            return []
        return [p.name for p in cls_dir.glob("*.json") if _parse_entry_name(p.name) is not None]

    def reap(self, job_class: str) -> list[str]:
        """Remove provably-stale entries; return the reaped filenames.

        Reuses the ADR 0027 ghost guards: a dead PID is stale; a live PID
        whose /proc argv no longer matches the recorded argv is a recycled
        PID (one shared PID namespace makes this judgment sound); a *holder*
        still present past ``created_at + timeout_s + slack`` is an
        unsupervised orphan nothing will ever release. Waiters get no
        deadline — queueing arbitrarily long is legitimate. Unreadable
        non-entry junk is removed once old enough to rule out in-flight
        creation.
        """
        cls_dir = self.root / job_class
        if not cls_dir.is_dir():
            return []
        # Reap unlinks without the lease gate; no rewrite can undo that. A
        # process-owned entry is reaped only once its owner is dead or reused
        # (or recover_process_owner stopped it), so no owner is left to
        # promote, renew, or shrink it, and recovery rewrites only
        # execution-owned entries. An execution-owned entry is reaped only
        # once ``cancelling`` is on disk, written under the lease gate, and
        # every gated rewrite refuses a ``cancelling`` entry.
        reaped: list[str] = []
        for path in list(cls_dir.glob("*.json")):
            if self._is_stale(path) and self._unlink_entry(path):
                reaped.append(path.name)
                logger.info("Reaped stale slot entry %s", path.name)
        return reaped

    def _is_stale(self, path: Path) -> bool:
        """True when the entry is removed: it may be stale and any recovery it needs succeeds."""
        if not self._may_be_stale(path):
            return False
        parsed = _parse_entry_name(path.name)
        if parsed is None:
            return self._is_old_junk(path)
        state = parsed[0]

        try:
            tok = self._load_token(path)
        except OSError:
            # Unreadable, not missing: reap only old entries whose owner is dead.
            return not self._is_pid_alive(parsed[3]) and self._is_old_junk(path)
        if tok is None:
            # Named like an entry but corrupt; same junk rule.
            return self._is_old_junk(path)
        return self._token_is_stale(tok, state)

    def _may_be_stale(self, path: Path) -> bool:
        """Whether a reap might remove the entry (it may still need a recovery call); reads only.

        ``_token_is_stale`` carries out the actions (lease claim, recovery)
        this only predicts. Unknown future schemas fail closed to ``False``.
        """
        parsed = _parse_entry_name(path.name)
        if parsed is None:
            return self._is_old_junk(path)
        try:
            tok = self._load_token(path)
        except OSError:
            return not self._is_pid_alive(parsed[3]) and self._is_old_junk(path)
        if tok is None:
            return self._is_old_junk(path)
        return self._token_may_be_stale(tok, parsed[0])

    def _token_may_be_stale(self, tok: SlotToken, state: str) -> bool:
        """``_may_be_stale`` for a readable entry; unknown future schemas fail closed."""
        if (
            not isinstance(tok.schema_version, int)
            or isinstance(tok.schema_version, bool)
            or tok.schema_version > SLOT_SCHEMA_VERSION
        ):
            return False
        deadline_reached = state == _HOLDER and (
            self._lease_expired(tok) or self._work_deadline_expired(tok)
        )
        if tok.owner_kind == "execution":
            return self._execution_may_be_stale(tok, deadline_reached=deadline_reached)
        return self._owner_is_stale(tok) or deadline_reached

    def _execution_may_be_stale(self, tok: SlotToken, *, deadline_reached: bool) -> bool:
        """``_may_be_stale`` for an entry owned by a durable execution."""
        if tok.execution_id is None:
            return False  # never reaped: nothing to ask about its owner
        if self._recovery.execution_is_terminal(tok.execution_id) or self._owner_is_stale(tok):
            return True
        return deadline_reached or tok.lease_state == LEASE_CANCELLING

    def _token_is_stale(self, tok: SlotToken, state: str) -> bool:
        # Unknown future schemas fail closed: this process cannot safely infer
        # ownership or recovery rules from fields it does not understand.
        if not isinstance(tok.schema_version, int) or isinstance(tok.schema_version, bool):
            return False
        if tok.schema_version > SLOT_SCHEMA_VERSION:
            return False

        if tok.owner_kind == "execution":
            return bool(
                tok.execution_id is not None and self._linked_execution_is_stale(tok, state)
            )
        return self._process_owner_is_stale(tok, state)

    def _linked_execution_is_stale(self, tok: SlotToken, state: str) -> bool:
        if self._recovery.execution_is_terminal(tok.execution_id or ""):
            return self._begin_execution_recovery(tok, request_cancel=False)
        owner_stale = self._owner_is_stale(tok)
        deadline_reached = state == _HOLDER and (
            self._lease_expired(tok) or self._work_deadline_expired(tok)
        )
        recovery_claimed = tok.lease_state == LEASE_CANCELLING
        if owner_stale or deadline_reached:
            recovery_claimed = self._begin_execution_recovery(tok, request_cancel=True)
        return bool(
            recovery_claimed
            and tok.execution_id is not None
            and self._recovery.recover_execution(tok.execution_id)
        )

    def _process_owner_is_stale(self, tok: SlotToken, state: str) -> bool:
        if self._owner_is_stale(tok):
            return True
        deadline_reached = state == _HOLDER and (
            self._lease_expired(tok) or self._work_deadline_expired(tok)
        )
        owner = tok.owner_identity or self._capture_identity(tok.pid)
        return bool(deadline_reached and self._recovery.recover_process_owner(owner))

    def _owner_is_stale(self, tok: SlotToken) -> bool:
        if self._process_is_gone(tok.pid, tok.owner_identity):
            return True
        return tok.owner_identity is None and self._argv_was_reused(tok)

    def _process_is_gone(self, pid: int, identity: ProcessIdentity | None) -> bool:
        """Dead, a zombie, or (with a captured identity) its PID now another process."""
        if identity is not None:
            return self._observe_identity(identity).state in DEAD_PROCESS_STATES
        return not self._is_pid_alive(pid)

    def _argv_was_reused(self, tok: SlotToken) -> bool:
        if not tok.argv:
            return False
        cmdline = self._read_cmdline(tok.pid)
        return cmdline is not None and cmdline != tok.argv

    def _lease_expired(self, tok: SlotToken) -> bool:
        """True once *tok*'s lease ran out; an expired lease is lost by contract.

        Reaping signals the owner process, or claims recovery of the owning
        execution, of an expired lease even while the owner is alive: an owner
        stalled by a host suspend or container pause cannot be told apart from
        a hung one. The holder keeps to the same contract: ``renew`` refuses an
        expired lease and sets ``lease_health.lost``, so a holder that resumes
        after expiry stops instead of extending a lease a reaper may act on.
        """
        deadline = parse_stamp(tok.lease_expires_at)
        if deadline is None:
            anchor = parse_stamp(tok.promoted_at) or parse_stamp(tok.created_at)
            deadline = anchor + LEASE_DURATION_SECONDS if anchor is not None else None
        return deadline is not None and self._now() > deadline

    def _work_deadline_expired(self, tok: SlotToken) -> bool:
        if tok.timeout_s is None:
            return False
        started = parse_stamp(tok.promoted_at)
        return bool(
            started is not None and self._now() > started + tok.timeout_s + DEADLINE_SLACK_SECONDS
        )

    def _begin_execution_recovery(self, tok: SlotToken, *, request_cancel: bool) -> bool:
        if tok.execution_id is None:
            return False
        if tok.lease_state == LEASE_CANCELLING:
            return True
        claimed = False
        with self._lease_gate(tok) as acquired:
            try:
                current = self._load_token(tok.path) if acquired else None
            except OSError:
                current = None  # unreadable: do not claim
            if current is not None and current.lease_state == LEASE_CANCELLING:
                claimed = True
            elif current is not None and current.lease_state == LEASE_ACTIVE:
                current.lease_state = LEASE_CANCELLING
                current.lease_generation += 1
                claimed = self._rewrite_token(current)
                if claimed and request_cancel:
                    self._recovery.cancel_execution(current.execution_id or tok.execution_id)
        return claimed

    def _is_old_junk(self, path: Path) -> bool:
        try:
            age = self._now() - path.stat().st_mtime
        except OSError:
            return False  # vanished under us — nothing to reap
        return age > _UNREADABLE_REAP_AGE_SECONDS
