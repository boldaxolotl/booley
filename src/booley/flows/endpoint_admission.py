"""Endpoint job admission and claim lifetime."""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from booley.config.jobs import SeatCapError, parse_caps, required_seats
from booley.core.boundary import as_float
from booley.flows.endpoint_events import (
    _endpoint_progress_event,
    _write_display_event,
)
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.memory_admission import (
    MemoryAdmissionPlan,
    MemorySizing,
    fixed_job_threads,
    job_threads_at_start,
    measure_memory_claim,
    memory_admission_enabled,
)
from booley.flows.reservation_phases import PhasePlan, ReservationShrinker, shrinking
from booley.runtime import job_slots
from booley.runtime.booley_dir import resolve_common_booley_dir
from booley.runtime.cpu_budget import available_cpu_count
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    OOM_REQUEUE_REASON,
    EndpointOutcome,
    EndpointRejectedError,
)
from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV, ExecutionId
from booley.runtime.job_records import _proc_cmdline
from booley.runtime.shared_infra import load_job_budget_config
from booley.runtime.supervised_execution import no_cancellation_cause

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState
    from booley.targets.domain import TargetHandle


logger = logging.getLogger(__name__)


class AdmissionIdentityError(ValueError):
    """The enclosing runtime execution identity is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class AdmissionContext:
    """Borrowed outer admission facts for one admitted endpoint invocation.

    ``max_heavy`` is the store's heavy cap, used by Simulation Campaign
    capacity; other endpoints ignore it. A Job admitted by Memory Reservation
    has a ``max_heavy`` of one. Its Campaign sizes lanes from the container's cores
    and admits each work item by its own claim, once ``memory_sizing`` is set.
    ``memory_warning`` is the warning that Job's result carries (it ran alone
    because its estimate exceeded the Memory Budget, or the budget was
    unavailable). ``memory_sizing`` lets its Campaign give each work item its
    own claim, and is set only then.
    """

    mode: str
    slot_store: job_slots.SlotStore | None
    outer_token: object | None
    max_heavy: int
    role: str
    execution_id: ExecutionId | None
    timeout_seconds: float | None
    cancellation: Callable[[], bool]
    cancellation_cause: Callable[[], str | None] = no_cancellation_cause
    memory_warning: str | None = None
    memory_sizing: MemorySizing | None = None
    parent_execution_id: ExecutionId = field(init=False)

    def __post_init__(self) -> None:
        if self.mode not in {"managed", "unmanaged"}:
            raise ValueError("admission mode must be managed or unmanaged")
        if self.max_heavy < 1:
            raise ValueError("admission max_heavy must be positive")
        managed = self.mode == "managed"
        if managed != (self.slot_store is not None and self.outer_token is not None):
            raise ValueError("managed admission requires a store and outer token")
        if not managed and self.max_heavy != 1:
            raise ValueError("unmanaged admission has exactly one logical lane")
        execution_id = _coerce_execution_id(self.execution_id)
        object.__setattr__(self, "execution_id", execution_id)
        object.__setattr__(
            self,
            "parent_execution_id",
            execution_id or ExecutionId(uuid.uuid4().hex),
        )


@dataclass(frozen=True, slots=True)
class _Claim:
    """What one Job's admission claimed; all ``None`` when nothing was claimed.

    ``plan`` sized a memory-admitted Job's claim; ``threads`` is the Job's
    share of the cores, fixed when its Memory Reservation is admitted. Only a
    memory-admitted Job has either. ``sizing`` is what it lends its work items.
    """

    store: job_slots.SlotStore | None
    token: object | None
    plan: MemoryAdmissionPlan | None = None
    threads: int | None = None
    sizing: MemorySizing | None = None


class AdmissionGate:
    """Lazy admission entered once, after Target authorization and the binding gate."""

    def __init__(
        self,
        endpoint: EndpointState,
        prepared: PreparedExecution | None = None,
        *,
        requeued: bool = False,
    ) -> None:
        self._endpoint = endpoint
        # What the Job will run sizes its Memory Reservation; ``None`` is unknown.
        self._prepared = prepared
        # The Job's second attempt, after an OOM kill: it claims the memory to itself.
        self._requeued = requeued
        self._entered = False

    def _claim(self) -> _Claim:
        """Claim the Job's admission: a Memory Reservation under the switch, else a slot.

        Under the switch heavy Jobs, lint and Custom Flows are memory-admitted;
        the claim carries the plan that sized it (``None`` for a slot).
        """
        endpoint = self._endpoint
        if memory_admission_enabled() and endpoint._memory_admitted():
            return _acquire_memory_reservation(endpoint, self._prepared, requeued=self._requeued)
        store, token = endpoint._acquire_job_slot()
        return _Claim(store, token)

    @contextmanager
    def enter(self) -> Iterator[AdmissionContext]:
        if self._entered:
            raise RuntimeError("AdmissionGate may be entered exactly once")
        self._entered = True
        store: job_slots.SlotStore | None = None
        token: object | None = None
        try:
            execution_id = _runtime_execution_id()
            claim = self._claim()
            store, token = claim.store, claim.token
            token_execution_id = getattr(token, "execution_id", None)
            if token_execution_id is not None and token_execution_id != execution_id:
                raise AdmissionIdentityError(
                    f"managed Job Slot execution identity disagrees with {RUNTIME_EXECUTION_ENV}"
                )
            role = job_slots.ROLE_INTERACTIVE
            timeout = _slot_timeout_seconds()
            max_heavy = _heavy_lanes(store, token)
            cause = _admission_cancellation_cause(store, token)
            with ExitStack() as scope:
                # Phase ends lower the claim only while the Job holds it.
                memory_plan = claim.plan
                if (
                    memory_plan is not None
                    and memory_plan.phases
                    and memory_plan.claim.reservation_mb is not None
                    and store is not None
                    and isinstance(token, job_slots.SlotToken)
                ):
                    phase_plan = PhasePlan(memory_plan.phases, memory_plan.claim.reservation_mb)
                    scope.enter_context(shrinking(ReservationShrinker(phase_plan, store, token)))
                scope.enter_context(fixed_job_threads(claim.threads))
                yield AdmissionContext(
                    mode="managed" if store is not None else "unmanaged",
                    slot_store=store,
                    outer_token=token,
                    max_heavy=max_heavy,
                    role=role,
                    execution_id=execution_id,
                    timeout_seconds=timeout,
                    cancellation=lambda: cause() is not None,
                    cancellation_cause=cause,
                    memory_warning=None if memory_plan is None else memory_plan.warning,
                    memory_sizing=claim.sizing,
                )
        finally:
            if store is not None and token is not None:
                store.release(token)


def _heavy_lanes(store: job_slots.SlotStore | None, token: object | None) -> int:
    """How many heavy lanes the admitted Job may use: the heavy cap, or one under memory."""
    if store is None:
        return 1
    if isinstance(token, job_slots.SlotToken) and token.job_class == job_slots.CLASS_MEMORY:
        return 1
    return store.caps.max_heavy


def authorize_targets(
    endpoint: EndpointState,
    targets: tuple[TargetHandle, ...],
) -> EndpointOutcome | None:
    """Authorize the complete ordered Target tuple without acquiring admission."""
    if not endpoint.state.strict_criteria or getattr(endpoint.args, "diagnostic", False):
        return None
    missing = [
        target.selector
        for target in targets
        if not endpoint._bound_criterion_keys_for_target(target)
    ]
    if not missing:
        return None
    return EndpointOutcome(
        exit_code=EXIT_ERROR,
        report_text=(
            f"{endpoint.name}: Goal Mode has no authorized Criterion binding for Target(s): "
            + ", ".join(missing)
        ),
    )


def _slot_timeout_seconds() -> float | None:
    value = as_float(os.environ.get("BOOLEY_SLOT_TIMEOUT_S"))
    return value if value is not None and value > 0 else None


def _coerce_execution_id(value: ExecutionId | str | None) -> ExecutionId | None:
    if value is None or value == "":
        return None
    try:
        return ExecutionId(value)
    except ValueError as exc:
        raise AdmissionIdentityError(f"{RUNTIME_EXECUTION_ENV} is invalid: {exc}") from exc


def _runtime_execution_id() -> ExecutionId | None:
    return _coerce_execution_id(os.environ.get(RUNTIME_EXECUTION_ENV))


def _admission_cancellation_cause(
    store: job_slots.SlotStore | None, token: object | None
) -> Callable[[], str | None]:
    """Name why the borrowed outer claim was withdrawn or lost, else ``None``."""
    if store is None or not isinstance(token, job_slots.SlotToken):
        return no_cancellation_cause
    slot = f"{token.job_class}/{token.path.name}"

    def cause() -> str | None:
        if token.lease_health.lost.is_set():
            return f"Job Slot lease lost: {slot}"
        if store.token_absent(token):
            return f"Job Slot token missing: {slot}"
        return None

    return cause


@contextmanager
def admission(
    endpoint: EndpointState, prepared: PreparedExecution, *, requeued: bool = False
) -> Iterator[AdmissionContext | None]:
    """Gate, then hold one Flow-neutral admission through final reporting.

    The same for every endpoint: a non-Flow endpoint passes its binding gate
    first; a dry run takes no slot and yields ``None``; every other invocation
    yields the borrowed ``AdmissionContext``, whose slot (if any) is released
    when the invocation body ends, however it ends.
    """
    if endpoint.endpoint_kind != "flow":
        rejection = endpoint._criterion_binding_gate()
        if rejection is not None:
            raise EndpointRejectedError(rejection)
    if prepared.non_persisting_dry_run:
        yield None
        return
    with ExitStack() as held:
        borrowed = _enter_admission(endpoint, held, prepared, requeued=requeued)
        endpoint._pre_run_head = endpoint._get_head_sha()
        yield borrowed


def _enter_admission(
    endpoint: EndpointState,
    held: ExitStack,
    prepared: PreparedExecution | None = None,
    *,
    requeued: bool = False,
) -> AdmissionContext:
    """Enter the gate on ``held``, mapping admission failures to a rejection.

    Only entry failures are mapped; exceptions raised later by the invocation
    body must pass through unchanged.
    """
    try:
        return held.enter_context(AdmissionGate(endpoint, prepared, requeued=requeued).enter())
    except (
        job_slots.QueueFullError,
        job_slots.ClaimLostError,
        AdmissionIdentityError,
        SeatCapError,
    ) as exc:
        raise EndpointRejectedError(_admission_rejection(endpoint, exc)) from exc


def _admission_rejection(
    endpoint: EndpointState,
    exc: job_slots.QueueFullError
    | job_slots.ClaimLostError
    | AdmissionIdentityError
    | SeatCapError,
) -> EndpointOutcome:
    """Map one admission failure to the outcome every endpoint reports."""
    if isinstance(exc, job_slots.QueueFullError):
        logger.error("Job admission refused: %s", exc)
        text = f"BLOCKED: {exc}. Retry when queued work drains."
    elif isinstance(exc, job_slots.ClaimLostError):
        logger.error("Queued %s run was cancelled before it started", endpoint.name)
        text = (
            f"CANCELLED: this queued '{endpoint.name}' run was "
            f"withdrawn (booley_cancel) before it started."
        )
    elif isinstance(exc, SeatCapError):
        logger.error("%s admission refused: %s", endpoint.name, exc)
        text = f"{endpoint.name}: {exc}"
    else:
        logger.error(
            "%s admission refused: invalid %s: %s", endpoint.name, RUNTIME_EXECUTION_ENV, exc
        )
        text = f"{endpoint.name}: invalid {RUNTIME_EXECUTION_ENV}: {exc}"
    return EndpointOutcome(exit_code=EXIT_ERROR, report_text=text)


def _admission_root(endpoint: EndpointState) -> tuple[Path | None, Path] | None:
    """The Main Booley Directory and slot store root, or ``None`` when nothing is claimed.

    Nothing is claimed when the parent process already holds this run's slot
    (``INHERITED_SLOT_ENV``; claiming again would double-count it and deadlock
    at a cap of 1) or no runtime is configured.
    """
    inherited = os.environ.get(job_slots.INHERITED_SLOT_ENV)
    if inherited:
        logger.debug("%s runs under its parent's %s slot", endpoint.name, inherited)
        return None
    try:
        project = resolve_common_booley_dir()
    except (OSError, ValueError, RuntimeError):
        project = None
    root = job_slots.slots_dir(project)
    return None if root is None else (project, root)


def _holder_deadline_seconds() -> float | None:
    """Holder deadline for the reaper (``job_slots._is_stale``).

    The MCP dispatch layer exports its real watchdog budget; 2x headroom keeps
    the deadline a strict upper bound of any legitimate run (reaping a LIVE
    holder frees an occupied slot, which overcommits), while a wedged
    unsupervised holder is still reclaimed eventually. Bare CLI runs have no
    exported budget and keep no deadline: they are user-supervised, and the PID
    guards still apply.
    """
    env_budget = os.environ.get("BOOLEY_SLOT_TIMEOUT_S", "")
    if not env_budget:
        return None
    try:
        return 2.0 * float(env_budget)
    except ValueError:
        logger.warning("Ignoring unparseable BOOLEY_SLOT_TIMEOUT_S=%r", env_budget)
        return None


def _narrate_line(endpoint: EndpointState, line: str) -> None:
    """Narrate one queued-claim *line* on stderr, the log and the display.

    Direct Flow diagnostics run in-process without logging configured and
    without BOOLEY_LOGS_DIR, so stderr is the sink that keeps a queued run from
    looking like a hang with no hint that another job held the slot (F-27).
    """
    logger.info("%s: %s", endpoint.name, line)
    print(f"[slot] {endpoint.name}: {line}", file=sys.stderr, flush=True)
    _write_display_event(
        _endpoint_progress_event(endpoint.name, line, identity=endpoint._display_identity)
    )


def _queue_narrator(
    endpoint: EndpointState,
    store: job_slots.SlotStore,
    job_class: str,
    waiting_for: str,
) -> Callable[[int], None]:
    """Narrate a queued slot claim, naming who holds the slot."""

    def narrate(position: int) -> None:
        held_by = store.describe_holders(job_class)
        _narrate_line(
            endpoint, f"waiting for {waiting_for} (position {position + 1}); held by {held_by}"
        )

    return narrate


def _memory_narrator(
    endpoint: EndpointState, store: job_slots.SlotStore, pid: int
) -> Callable[[int], None]:
    """Narrate a queued memory claim: its need, the free memory, its position and the holders."""

    def narrate(_position: int) -> None:
        wait = store.memory_wait(pid)
        if wait is not None:  # None: promoted since the tick, nothing left to wait for
            _narrate_line(
                endpoint, f"waiting for memory (position {wait.position}): {wait.describe()}"
            )

    return narrate


def _acquire_job_slot(endpoint: EndpointState) -> tuple[job_slots.SlotStore | None, object | None]:
    """Claim this run's admission slot, waiting in queue order if needed.

    Returns ``(store, token)`` to release in main()'s finally, or
    ``(None, None)`` when no admission applies: the endpoint is unclassed, the
    parent process already holds this run's slot, or no runtime is configured
    (bare invocations outside a project keep working unguarded). Raises
    QueueFullError when the class queue is at ``queue_max``, the only admission
    outcome surfaced as BLOCKED.
    """
    job_class = endpoint._resolve_job_class()
    if job_class is None:
        return (None, None)  # unclassed: skip even the store lookup
    found = _admission_root(endpoint)
    if found is None:
        return (None, None)
    project, root = found
    # Caps must come from the SAME project the slot store belongs to: the Main
    # Booley Directory (Common Booley State), never work_dir or a linked
    # worktree's diverged booley.toml.
    caps = parse_caps(load_job_budget_config(booley_dir=project))
    # Claim identity is this live process: record the argv exactly as
    # /proc reports it so the ghost guards can match it later.
    pid = os.getpid()
    store = job_slots.SlotStore(root, caps)
    token = store.acquire(
        job_class,
        pid=pid,
        argv=_proc_cmdline(pid) or [],
        role=job_slots.ROLE_INTERACTIVE,
        timeout_s=_holder_deadline_seconds(),
        on_queued=_queue_narrator(endpoint, store, job_class, f"{job_class} slot"),
    )
    return (store, token)


def _acquire_memory_reservation(
    endpoint: EndpointState, prepared: PreparedExecution | None, *, requeued: bool = False
) -> _Claim:
    """Claim this memory-admitted Job's Memory Reservation, waiting in queue order if needed.

    Returns the claim with the Job's thread count; an empty claim when nothing
    is claimed, as for :func:`_acquire_job_slot`. That check comes before any
    sizing, so a child under its parent's claim does no estimate work.

    A *requeued* Job (its first attempt was OOM-killed) claims the whole
    budget, run alone, and waits at the back of the queue with the reason.
    """
    found = _admission_root(endpoint)
    if found is None:
        return _Claim(None, None)
    project, root = found
    config = load_job_budget_config(booley_dir=project)
    # Refused before any sizing or store entry: a Job that runs a licensed tool
    # without a Seat Cap never queues (SeatCapError is mapped by _enter_admission).
    seats = required_seats(endpoint._licensed_tools(), config)
    store = job_slots.SlotStore(root, parse_caps(config))
    requests = None if prepared is None else prepared.invocation.job_requests()
    plan = measure_memory_claim(project, config, requests)
    if requeued:
        _refuse_waiting_behind_own_claim(endpoint, store)
        plan = plan.alone()
    claim = dataclasses.replace(plan.claim, seats=seats)
    pid = os.getpid()
    # A Job that names no request (an unknown invocation) reports the selected Target, if any;
    # a Flow whose input is not prepared has none.
    selected = endpoint._selected_target if endpoint._args is not None else ""
    targets = ", ".join(dict.fromkeys(request.target for request in requests or ())) or selected
    label = job_slots.JobLabel(os.environ.get("BOOLEY_RUN_ID", ""), endpoint.name, targets)
    token = store.acquire(
        job_slots.CLASS_MEMORY,
        pid=pid,
        argv=_proc_cmdline(pid) or [],
        role=job_slots.ROLE_INTERACTIVE,
        timeout_s=_holder_deadline_seconds(),
        on_queued=_memory_narrator(endpoint, store, pid),
        memory=claim,
        job=label,
        queue_reason=OOM_REQUEUE_REASON if requeued else None,
    )
    try:
        threads = job_threads_at_start(store, available_cpu_count())
    except BaseException:
        # The caller never sees the token, so it cannot release it.
        store.release(token)
        raise
    return _Claim(store, token, plan, threads, MemorySizing(project, config))


def _refuse_waiting_behind_own_claim(endpoint: EndpointState, store: job_slots.SlotStore) -> None:
    """Refuse a requeued claim while this process still holds an entry in any class.

    A deferred release (a Campaign whose shutdown overruns) can keep the killed
    attempt's claim past its end; queueing behind it would wait for ourselves.
    The process runs one Job, so a held entry of any class is the killed attempt's.
    """
    state = store.state_for_pid(os.getpid())
    if state is not None and state.state == job_slots.HOLDING:
        raise EndpointRejectedError(
            EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"{endpoint.name}: the killed attempt's work still holds its claim, "
                    "so the Job cannot wait for the memory to itself."
                ),
                oom_killed=True,
            )
        )
