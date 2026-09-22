"""Endpoint job admission and claim lifetime."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from booley.config.jobs import parse_caps
from booley.core.boundary import as_float
from booley.flows.endpoint_events import (
    _endpoint_progress_event,
    _write_display_event,
)
from booley.flows.endpoint_session import PreparedExecution
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EndpointOutcome,
    EndpointRejectedError,
)
from booley.runtime.job_records import _proc_cmdline

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState
    from booley.targets.domain import TargetHandle


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdmissionContext:
    """Borrowed outer heavy-admission facts for one Simulation invocation."""

    mode: str
    slot_store: job_slots.SlotStore | None
    outer_token: object | None
    max_heavy: int
    role: str
    execution_id: str
    timeout_seconds: float | None
    cancellation: Callable[[], bool]

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


class AdmissionGate:
    """Lazy admission entered once after Simulation Target authorization."""

    def __init__(self, endpoint: EndpointState) -> None:
        self._endpoint = endpoint
        self._entered = False

    @contextmanager
    def enter(self) -> Iterator[AdmissionContext]:
        if self._entered:
            raise RuntimeError("AdmissionGate may be entered exactly once")
        self._entered = True
        store: job_slots.SlotStore | None = None
        token: object | None = None
        try:
            store, token = self._endpoint._acquire_job_slot()
            role = (
                job_slots.ROLE_TICKET
                if os.environ.get("BOOLEY_AGENT_ROLE") == "ticket"
                else job_slots.ROLE_INTERACTIVE
            )
            timeout = _slot_timeout_seconds()
            max_heavy = store.caps.max_heavy if store is not None else 1
            yield AdmissionContext(
                mode="managed" if store is not None else "unmanaged",
                slot_store=store,
                outer_token=token,
                max_heavy=max_heavy,
                role=role,
                execution_id=self._endpoint._invocation_id,
                timeout_seconds=timeout,
                cancellation=_admission_cancellation(store, token),
            )
        finally:
            if store is not None and token is not None:
                store.release(token)


def authorize_simulation_targets(
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
            "sim: strict Ticket has no authorized Criterion binding for Target(s): "
            + ", ".join(missing)
        ),
    )


def _slot_timeout_seconds() -> float | None:
    value = as_float(os.environ.get("BOOLEY_SLOT_TIMEOUT_S"))
    return value if value is not None and value > 0 else None


def _admission_cancellation(
    store: job_slots.SlotStore | None, token: object | None
) -> Callable[[], bool]:
    """Observe withdrawal or renewal loss of the borrowed outer claim."""
    if store is None or not isinstance(token, job_slots.SlotToken):
        return lambda: False
    return lambda: token.lease_health.lost.is_set() or store.token_absent(token)


@contextmanager
def admission(endpoint: EndpointState, prepared: PreparedExecution) -> Iterator[object | None]:
    """Validate the Target, then hold admission through final reporting."""
    if endpoint.name == "sim" and prepared.simulation is not None:
        with _simulation_admission(endpoint, prepared) as borrowed:
            yield borrowed
        return
    slot_store: job_slots.SlotStore | None = None
    slot_token = None
    try:
        rejection = endpoint._criterion_binding_gate()
        if rejection is not None:
            if rejection.report_text:
                print(rejection.report_text, file=sys.stderr, flush=True)
            raise EndpointRejectedError(rejection)
        if prepared.non_persisting_dry_run:
            yield None
            return
        try:
            slot_store, slot_token = endpoint._acquire_job_slot()
        except job_slots.QueueFullError as exc:
            logger.error("Job admission refused: %s", exc)
            raise EndpointRejectedError(
                EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=f"BLOCKED: {exc}. Retry when queued work drains.",
                )
            ) from exc
        except job_slots.ClaimLostError as exc:
            logger.error("Queued %s run was cancelled before it started", endpoint.name)
            raise EndpointRejectedError(
                EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"CANCELLED: this queued '{endpoint.name}' run was "
                        f"withdrawn (booley_cancel) before it started."
                    ),
                )
            ) from exc
        endpoint._pre_run_head = endpoint._get_head_sha()
        yield None
    finally:
        if slot_store is not None and slot_token is not None:
            slot_store.release(slot_token)


@contextmanager
def _simulation_admission(
    endpoint: EndpointState, prepared: PreparedExecution
) -> Iterator[AdmissionContext | None]:
    if prepared.non_persisting_dry_run:
        yield None
        return
    gate = AdmissionGate(endpoint)
    try:
        with gate.enter() as borrowed:
            endpoint._pre_run_head = endpoint._get_head_sha()
            yield borrowed
    except job_slots.QueueFullError as exc:
        raise EndpointRejectedError(
            EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"BLOCKED: {exc}. Retry when queued work drains.",
            )
        ) from exc


def _acquire_job_slot(endpoint: EndpointState) -> tuple[job_slots.SlotStore | None, object | None]:
    """Claim this run's admission slot, waiting in queue order if needed.

    Returns ``(store, token)`` to release in main()'s finally, or
    ``(None, None)`` when no admission applies: the endpoint is unclassed, or
    no runtime is configured (bare invocations outside a project keep
    working unguarded). Raises QueueFullError when the class queue is at
    ``queue_max`` — the only admission outcome surfaced as BLOCKED.
    """
    job_class = endpoint._resolve_job_class()
    if job_class is None:
        return (None, None)  # unclassed: skip even the store lookup
    root = job_slots.slots_dir()
    if root is None:
        return (None, None)

    from booley.runtime.shared_infra import _load_rtl_config

    try:
        # Caps must come from the SAME project the slot store belongs to
        # (slots_dir → resolve_project_dir), never from work_dir: a
        # linked worktree can carry a diverged booley.toml, and two
        # claimants promoting under different caps overcommit the class.
        # None = the CWD/BOOLEY_PROJECT_DIR resolution path.
        cfg = _load_rtl_config(None)
    except Exception:  # noqa: BLE001 — best-effort; defaults are safe
        cfg = {}
    caps = parse_caps(cfg or {})
    role = (
        job_slots.ROLE_TICKET
        if os.environ.get("BOOLEY_AGENT_ROLE") == "ticket"
        else job_slots.ROLE_INTERACTIVE
    )
    # Claim identity is this live process: record the argv exactly as
    # /proc reports it so the ghost guards can match it later.
    pid = os.getpid()
    argv = _proc_cmdline(pid) or []

    store = job_slots.SlotStore(root, caps)

    # Holder deadline for the reaper (job_slots._is_stale): the MCP
    # dispatch layer exports its real watchdog budget; 2x headroom keeps
    # the deadline a strict upper bound of any legitimate run (reaping a
    # LIVE holder frees an occupied slot → overcommit), while a wedged
    # unsupervised holder is still reclaimed eventually. Bare CLI runs
    # have no exported budget and keep no deadline — they are
    # user-supervised, and the PID guards still apply.
    timeout_s: float | None = None
    env_budget = os.environ.get("BOOLEY_SLOT_TIMEOUT_S", "")
    if env_budget:
        try:
            timeout_s = 2.0 * float(env_budget)
        except ValueError:
            logger.warning("Ignoring unparseable BOOLEY_SLOT_TIMEOUT_S=%r", env_budget)

    def _narrate(position: int) -> None:
        # stderr as well as the log: direct Flow diagnostics run in-process without
        # logging configured and without BOOLEY_LOGS_DIR, so both of the
        # other sinks are no-ops there — a queued run looked like a hang
        # with no hint that another job held the slot (F-27).
        held_by = store.describe_holders(job_class)
        line = f"waiting for {job_class} slot (position {position + 1}); held by {held_by}"
        logger.info("%s: %s", endpoint.name, line)
        print(f"[slot] {endpoint.name}: {line}", file=sys.stderr, flush=True)
        _write_display_event(
            _endpoint_progress_event(
                endpoint.name,
                line,
                identity=endpoint._display_identity,
            )
        )

    token = store.acquire(
        job_class,
        pid=pid,
        argv=argv,
        role=role,
        timeout_s=timeout_s,
        on_queued=_narrate,
    )
    return (store, token)
