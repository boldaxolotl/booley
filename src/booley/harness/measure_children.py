"""Run the child Flows of one measurement inside the admission queue (``booley measure``, #3952).

Each rules table of a commit is one child Flow in a clean checkout
(:mod:`booley.harness.checkout_flow`). Every child claims admission like any other Flow
Job, and the admission queue is bounded, so starting all children at once would get the
later ones refused for a full queue. The :class:`ChildScheduler` therefore starts them
cheapest cost tier first, only while the queue has room for them, and reads the shared slot
store to do so.

Three behaviors sit on top of that:

* Without ``all_tables``, the first ``unmet`` or ``error`` verdict stops the measurement:
  children still waiting for admission are withdrawn, children already running finish and
  keep their verdicts, and the tables of children that never started are ``not_evaluated``.
* A cancellation withdraws the waiting children, stops the running ones and settles every
  one of them, so nothing is left waiting or running.
* A child that was killed for running out of memory claims admission again for its final
  attempt. That claim is never withdrawn, and the child is not finished (nor its verdicts
  read) until that attempt ends.

The window rule: the scheduler counts its *occupying* children, those started and not
ended, minus those last seen holding a slot on a requeued final attempt (that attempt cannot
queue again). It starts the next child only while that count is below
``max(1, queue_max - 1)``. The count rises only when a child starts, so at most ``window - 1``
of the measurement's own claims are ahead of any new claim of its own, which is below
``queue_max``: no child of the measurement is refused for a full queue, even when several
running children requeue at once. Other Jobs' waiting claims take room in the same queue,
so while a child is live a further one starts only when the foreign waiters fit too.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from booley.config.jobs import parse_caps
from booley.harness.checkout_flow import (
    STOP_GRACE_S,
    CheckoutFlowError,
    CheckoutFlowRequest,
    CheckoutFlowResult,
    prepare_checkout_flow,
)
from booley.measurement.flow_metrics import metric_declarations
from booley.measurement.metrics_file import TableRules
from booley.measurement.run_document import RunStatus, TableRun, Verdict
from booley.measurement.verdicts import (
    FlowFailed,
    FlowNotStarted,
    FlowOutcome,
    FlowValues,
    evaluate_table,
)
from booley.runtime import job_slots
from booley.runtime.shared_infra import load_job_budget_config

if TYPE_CHECKING:
    from booley.flows.metric_declarations import FlowMetricValues

logger = logging.getLogger(__name__)

# Seconds between two passes of the scheduler loop.
_POLL_S = 0.2
STOPPED_REASON = "not started: the measurement stopped after an unmet or error verdict"
CANCELLED_REASON = "the measurement was cancelled"
# The ``coverage`` table runs the ``sim`` Flow with coverage collection.
_COVERAGE_TABLE = "coverage"
_SIM_FLOW = "sim"
_FAILING_VERDICTS = (Verdict.UNMET, Verdict.ERROR)


class RunningChild(Protocol):
    """The surface of a started child Flow (:class:`~booley.harness.checkout_flow.CheckoutFlowChild`)."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> bool: ...

    def interrupt(self) -> None: ...

    def wait(self, timeout_s: float) -> bool: ...

    def kill(self) -> None: ...

    def settle(self) -> CheckoutFlowResult: ...


class PreparedChild(Protocol):
    """The surface of a child Flow that passed its refusals (``PreparedCheckoutFlow``)."""

    def start(self) -> RunningChild: ...

    def discard(self) -> None: ...


@dataclass(frozen=True)
class TableMeasurement:
    """One table's run, the reports behind it and whether its child ended on its own."""

    run: TableRun
    reports: tuple[Path, ...]
    # True only for a child that passed, failed or timed out by itself; False for a child
    # that never started, was withdrawn from the queue, or was stopped by a cancellation.
    finished: bool


@dataclass(frozen=True)
class ChildrenOutcome:
    """What the children of one measurement produced: its status and each table's run."""

    status: RunStatus
    tables: tuple[TableMeasurement, ...]


@dataclass
class _Entry:
    """One table's child, from planning to settlement."""

    rules: TableRules
    prepared: PreparedChild
    child: RunningChild | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    # The scheduler withdrew the child's waiting claim before admission.
    withdrawn: bool = False
    # Last seen holding a slot on a requeued final attempt: it cannot queue again.
    final_attempt: bool = False
    result: CheckoutFlowResult | None = None
    settled: bool = False
    measurement: TableMeasurement | None = None

    @property
    def live(self) -> bool:
        """Started and not yet ended."""
        return self.child is not None and not self.settled


def in_cost_tier_order(tables: Sequence[TableRules]) -> list[TableRules]:
    """*tables* cheapest first by the highest cost tier their Flow declares; ties keep order."""

    def tier(table: TableRules) -> int:
        return max(int(d.cost_tier) for d in metric_declarations(table.flow))

    return sorted(tables, key=tier)


class ChildScheduler:
    """Start, watch, withdraw and settle the child Flows of one measurement.

    *planned* is the ``(rules, prepared child)`` pairs in the order to start them. *store* is
    the slot store the children claim admission in.
    Drive it with :meth:`step` until that returns False, read :meth:`outcome`, and always
    call :meth:`close`.
    """

    def __init__(
        self,
        planned: Sequence[tuple[TableRules, PreparedChild]],
        store: job_slots.SlotStore,
        *,
        all_tables: bool,
    ) -> None:
        self._entries = [_Entry(rules, prepared) for rules, prepared in planned]
        self._store = store
        self._all_tables = all_tables
        self._window = max(1, store.caps.queue_max - 1)
        self._stopping = False
        self._cancelled = False

    # ------------------------------------------------------------------ driving

    def step(self, *, cancel: bool) -> bool:
        """Do one pass; return whether work remains."""
        self._reap_ended()
        if cancel:
            self._cancel()
            return False
        for entry in self._live():
            self._observe(entry)
        if self._stopping:
            for entry in self._live():
                self._withdraw_if_waiting(entry)
        else:
            self._start_while_room()
        return bool(self._live()) or (not self._stopping and self._has_planned())

    def _live(self) -> list[_Entry]:
        return [entry for entry in self._entries if entry.live]

    def _has_planned(self) -> bool:
        return any(entry.child is None for entry in self._entries)

    def _reap_ended(self) -> None:
        """Settle and evaluate every child that has ended; set ``stopping`` on a failing verdict."""
        for entry in self._live():
            child = entry.child
            assert child is not None
            if not child.poll():
                continue
            self._settle(entry)
            if entry.withdrawn:
                continue
            measurement = self._evaluate(entry)
            entry.measurement = measurement
            failed = any(result.verdict in _FAILING_VERDICTS for result in measurement.run.results)
            if failed and not self._all_tables:
                self._stopping = True

    def _settle(self, entry: _Entry) -> None:
        child = entry.child
        assert child is not None
        entry.ended_at = time.monotonic()
        entry.result = child.settle()
        entry.settled = True

    def _evaluate(self, entry: _Entry) -> TableMeasurement:
        """The table's run from its settled child: values, or the reason it has none."""
        result = entry.result
        assert result is not None
        rules = entry.rules
        values: FlowMetricValues | None = None
        outcome: FlowOutcome
        if len(result.metrics) == 1 and not result.unavailable:
            values = next(iter(result.metrics.values()))
            outcome = FlowValues(values, values.clocks)
        else:
            outcome = FlowFailed(result.excerpt or f"{rules.flow} returned no values")
        wall = result.elapsed_s if result.elapsed_s is not None else self._ran_for(entry)
        run = TableRun(rules.identity, rules.flow, wall, values, evaluate_table(rules, outcome))
        return TableMeasurement(run, result.reports, finished=True)

    @staticmethod
    def _ran_for(entry: _Entry) -> float:
        return max(0.0, entry.ended_at - entry.started_at)

    # --------------------------------------------------------------- observation

    def _observe(self, entry: _Entry) -> None:
        """Note whether a child holds a slot on a requeued final attempt right now.

        Not sticky: a Campaign's work items claim under the Flow's PID as well, and the item
        claims after a requeued one are not final, so the flag follows the claim held now.
        """
        if entry.child is None:
            return
        state = self._store.state_for_pid(entry.child.pid)
        entry.final_attempt = (
            state is not None and state.state == job_slots.HOLDING and bool(state.reason)
        )

    def _waiting_first_claim(self, pid: int) -> job_slots.SlotToken | None:
        """The waiting first claim of *pid*, when it holds no slot yet and none is requeued.

        A Flow that holds its admission and waits for a work item's claim (the same PID) is
        running: its waiting item claim is never withdrawn.
        """
        found: job_slots.SlotToken | None = None
        for job_class in job_slots.JOB_CLASSES:
            holders, waiters = self._store.snapshot(job_class)
            if any(token.pid == pid for token in holders):
                return None
            for token in waiters:
                if token.pid == pid and token.queue_reason is None:
                    found = token
        return found

    def _withdraw_if_waiting(self, entry: _Entry) -> None:
        """Withdraw a child still waiting for its first claim; a promoted one keeps running."""
        child = entry.child
        if child is None or entry.withdrawn:
            return
        token = self._waiting_first_claim(child.pid)
        if token is not None and self._store.cancel_waiter_token(token):
            entry.withdrawn = True

    def _foreign_waiting(self, own_pids: set[int]) -> int:
        """Waiting claims of both Job classes that belong to no child of this measurement."""
        count = 0
        for job_class in job_slots.JOB_CLASSES:
            _holders, waiters = self._store.snapshot(job_class)
            count += sum(1 for token in waiters if token.pid not in own_pids)
        return count

    # ------------------------------------------------------------------ starting

    def _start_while_room(self) -> None:
        """Start planned children while the window, and the foreign waiters, leave room."""
        live = self._live()
        foreign = self._foreign_waiting({e.child.pid for e in live if e.child})
        occupying = sum(1 for entry in live if not entry.final_attempt)
        any_live = bool(live)
        for entry in self._entries:
            if entry.child is not None:
                continue
            if occupying >= self._window:
                return
            # With no child live the next one starts whatever the foreign load: the
            # measurement keeps moving, bounded by each child's own timeout.
            if any_live and occupying + foreign >= self._window:
                return
            entry.child = entry.prepared.start()
            entry.started_at = time.monotonic()
            occupying += 1
            any_live = True

    # -------------------------------------------------------------- cancellation

    def _cancel(self) -> None:
        """Withdraw the waiting children, stop the running ones and settle every launched one."""
        self._cancelled = True
        live = self._live()
        for entry in live:
            self._withdraw_if_waiting(entry)
        running = [e.child for e in live if e.child is not None and not e.withdrawn]
        withdrawn = [e.child for e in live if e.child is not None and e.withdrawn]
        _stop_all(running, await_exit=withdrawn)
        for entry in live:
            self._settle(entry)
        self._discard_unstarted()

    def _discard_unstarted(self) -> None:
        for entry in self._entries:
            if entry.child is None:
                entry.prepared.discard()

    def close(self) -> None:
        """Leave no child running and no scratch behind, whatever happened before."""
        live = self._live()
        try:
            _stop_all([entry.child for entry in live if entry.child is not None])
        finally:
            for entry in live:
                try:
                    self._settle(entry)
                except Exception:  # one failed settlement must not skip the others
                    logger.warning(
                        "could not settle the child of %s", entry.rules.flow, exc_info=True
                    )
            self._discard_unstarted()

    # ------------------------------------------------------------------- outcome

    def outcome(self) -> ChildrenOutcome:
        """The run status and every table's run, in the order the tables were planned."""
        reason = CANCELLED_REASON if self._cancelled else STOPPED_REASON
        tables: list[TableMeasurement] = []
        unevaluated = False
        for entry in self._entries:
            measurement = entry.measurement
            if measurement is None:
                unevaluated = True
                measurement = self._not_evaluated(entry, reason)
            tables.append(measurement)
        if self._cancelled:
            status = RunStatus.CANCELLED
        elif self._stopping and unevaluated:
            status = RunStatus.STOPPED
        else:
            status = RunStatus.COMPLETED
        return ChildrenOutcome(status, tuple(tables))

    def _not_evaluated(self, entry: _Entry, reason: str) -> TableMeasurement:
        """The run of a table whose child never started, was withdrawn or was cancelled."""
        rules = entry.rules
        # A withdrawn or never-started child's Flow did not run, whatever its process did.
        ran = entry.child is not None and not entry.withdrawn
        wall: float | None = None
        if ran:
            elapsed = entry.result.elapsed_s if entry.result is not None else None
            wall = elapsed if elapsed is not None else self._ran_for(entry)
        results = evaluate_table(rules, FlowNotStarted(reason))
        run = TableRun(rules.identity, rules.flow, wall, None, results)
        reports = entry.result.reports if entry.result is not None else ()
        return TableMeasurement(run, reports, finished=False)


def _stop_all(
    children: Sequence[RunningChild], *, await_exit: Sequence[RunningChild] = ()
) -> None:
    """Interrupt every child, give them one shared grace period, then kill the ones left.

    The *await_exit* children are not interrupted (a withdrawn child ends through its lost
    claim and must say so in its log) but wait for and are killed like the others. A
    ``BaseException`` while signalling or waiting (a second Ctrl-C) kills every child that
    has not ended yet and propagates: no sibling is left running.
    """
    everyone = [*children, *await_exit]
    deadline = time.monotonic() + STOP_GRACE_S
    try:
        for child in children:
            child.interrupt()
        for child in everyone:
            if not child.wait(max(0.0, deadline - time.monotonic())):
                child.kill()
    except BaseException:
        for child in everyone:
            if not child.wait(0):
                child.kill()
        raise


def _request_for(
    table: TableRules,
    index: int,
    *,
    checkout: Path,
    main_booley_dir: Path,
    report_dir: Path,
    timeout_s: float | None,
) -> CheckoutFlowRequest:
    """The clean-checkout run of one table: ``coverage`` is the ``sim`` Flow with coverage."""
    is_coverage = table.flow == _COVERAGE_TABLE
    return CheckoutFlowRequest(
        checkout=checkout,
        main_booley_dir=main_booley_dir,
        report_dir=report_dir / f"{index:02d}-{table.flow}",
        flow=_SIM_FLOW if is_coverage else table.flow,
        target=table.target,
        timeout_s=timeout_s,
        coverage=is_coverage,
    )


def _prepare_all(
    ordered: Sequence[TableRules],
    *,
    checkout: Path,
    main_booley_dir: Path,
    report_dir: Path,
    timeout_s: float | None,
) -> list[tuple[TableRules, PreparedChild]]:
    """Preflight every table in order; a refusal for any one discards the others' scratch."""
    prepared: list[tuple[TableRules, PreparedChild]] = []
    try:
        for index, table in enumerate(ordered):
            request = _request_for(
                table,
                index,
                checkout=checkout,
                main_booley_dir=main_booley_dir,
                report_dir=report_dir,
                timeout_s=timeout_s,
            )
            try:
                prepared.append((table, prepare_checkout_flow(request)))
            except CheckoutFlowError as exc:
                raise CheckoutFlowError(f"[{table.target}.{table.flow}] {exc}") from exc
    except BaseException:
        for _table, child in prepared:
            child.discard()
        raise
    return prepared


def measure_tables(
    tables: Sequence[TableRules],
    *,
    checkout: Path,
    main_booley_dir: Path,
    report_dir: Path,
    timeout_s: float | None,
    all_tables: bool,
    cancelled: Callable[[], bool],
) -> ChildrenOutcome:
    """Measure every table in *checkout*, cheapest cost tier first, within the admission queue.

    Every table is preflighted before any child starts, so a refusal for any of them starts
    none. *cancelled* is polled each pass; a ``BaseException`` (a Ctrl-C) stops and settles
    every child before it propagates. The outcome lists the tables in *tables* order.

    Raises:
        CheckoutFlowError: a table was refused or could not be started; it names the table.
    """
    main = main_booley_dir.resolve()
    prepared = _prepare_all(
        in_cost_tier_order(tables),
        checkout=checkout,
        main_booley_dir=main,
        report_dir=report_dir,
        timeout_s=timeout_s,
    )
    store = job_slots.SlotStore(
        job_slots.project_slots_dir(main),
        parse_caps(load_job_budget_config(booley_dir=main)),
    )
    scheduler = ChildScheduler(prepared, store, all_tables=all_tables)
    try:
        while scheduler.step(cancel=cancelled()):
            time.sleep(_POLL_S)
        outcome = scheduler.outcome()
    finally:
        scheduler.close()
    position = {(table.identity, table.flow): rank for rank, table in enumerate(tables)}
    ranked = sorted(outcome.tables, key=lambda t: position[(t.run.identity, t.run.flow)])
    return ChildrenOutcome(outcome.status, tuple(ranked))
