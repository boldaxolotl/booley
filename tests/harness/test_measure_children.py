"""The scheduler of a measurement's child Flows (#3952), against a real slot store.

The children are scripted fakes: no process runs. Their admission claims are real entries in
a :class:`~booley.runtime.job_slots.SlotStore` whose process checks are faked, so the start order
and the withdrawal of a waiting claim are the store's own behavior.
The real child processes are covered in ``test_measure_children_flows.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from booley.config.jobs import SlotCaps
from booley.flows.metric_declarations import FlowMetricValues
from booley.harness.checkout_flow import CheckoutFlowResult, CheckoutFlowStatus
from booley.measurement.metrics_file import Bound, TableRules
from booley.measurement.run_document import RunStatus, Verdict
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import OOM_REQUEUE_REASON
from tests.test_job_slots import FakeWorld, make_store, spawn

if TYPE_CHECKING:
    from booley.harness.measure_children import ChildrenOutcome

# The scheduler module is imported inside each test body, so its absence fails that test,
# not collection.

BOUND = 5


@pytest.fixture()
def world() -> FakeWorld:
    return FakeWorld()


def table(target: str, flow: str = "lint") -> TableRules:
    """A table of *flow* with one bound: ``warnings`` at most ``BOUND`` (the lint metric)."""
    bound = Bound("warnings_max", "warnings", "max", BOUND, None, None, None)
    return TableRules(target, f"core#{target}", flow, (bound,), ())


def empty_table(target: str, flow: str) -> TableRules:
    """A table with no entries: only its place in the order matters."""
    return TableRules(target, f"core#{target}", flow, (), ())


def result(
    warnings: int | None, *, elapsed_s: float | None = 1.5, excerpt: str = "boom"
) -> CheckoutFlowResult:
    """A settled child: its ``warnings`` value, or no values at all when None."""
    metrics = (
        {}
        if warnings is None
        else {"t": FlowMetricValues(plain={"warnings": warnings}, per_clock={})}
    )
    return CheckoutFlowResult(
        run_id="run",
        status=CheckoutFlowStatus.PASSED if warnings is not None else CheckoutFlowStatus.FAILED,
        exit_code=0 if warnings is not None else 1,
        reports=(Path("report.json"),),
        metrics=metrics,
        log=Path("job.log"),
        excerpt=None if warnings is not None else excerpt,
        elapsed_s=elapsed_s,
    )


def met() -> CheckoutFlowResult:
    return result(0)


def unmet() -> CheckoutFlowResult:
    return result(BOUND + 4)


def errored() -> CheckoutFlowResult:
    return result(None)


Claim = Callable[[job_slots.SlotStore, int], job_slots.SlotToken]


class FakeChild:
    """A scripted child: it ends when told to, or on SIGINT, and records how it was treated."""

    def __init__(
        self,
        world: FakeWorld,
        pid: int,
        token: job_slots.SlotToken | None,
        *,
        exits_on_interrupt: bool = True,
        raise_on_first_wait: bool = False,
        raise_on_interrupt: bool = False,
    ) -> None:
        self.world = world
        self.pid = pid
        self.token = token
        self.exits_on_interrupt = exits_on_interrupt
        self.raise_on_first_wait = raise_on_first_wait
        self.raise_on_interrupt = raise_on_interrupt
        self.outcome: CheckoutFlowResult = met()
        self.ended = False
        self.interrupted = False
        self.killed = False
        self.settles = 0

    def finish(self, outcome: CheckoutFlowResult | None = None) -> None:
        """The child's process ends with *outcome*; its claim goes with it."""
        self.outcome = outcome or met()
        self.ended = True
        self.world.alive.pop(self.pid, None)

    def poll(self) -> bool:
        return self.ended

    def interrupt(self) -> None:
        self.interrupted = True
        if self.raise_on_interrupt:
            raise KeyboardInterrupt
        if self.exits_on_interrupt:
            self.finish(errored())

    def wait(self, timeout_s: float) -> bool:
        if not self.ended and self.token is not None and not self.token.path.exists():
            self.finish(errored())  # a withdrawn claim ends the child by itself
        if self.raise_on_first_wait:
            self.raise_on_first_wait = False
            raise KeyboardInterrupt
        return self.ended

    def kill(self) -> None:
        self.killed = True
        self.finish(errored())

    def settle(self) -> CheckoutFlowResult:
        self.settles += 1
        return self.outcome


class FakePrepared:
    """A preparation that starts a :class:`FakeChild` and opens its first claim."""

    def __init__(self, fleet: Fleet, target: str, claim: Claim | None) -> None:
        self.fleet = fleet
        self.target = target
        self.claim = claim
        self.child: FakeChild | None = None
        self.discarded = False

    def start(self) -> FakeChild:
        pid = self.fleet.next_pid
        self.fleet.next_pid += 1
        spawn(self.fleet.world, pid)
        token = self.claim(self.fleet.store, pid) if self.claim is not None else None
        self.child = FakeChild(
            self.fleet.world, pid, token, exits_on_interrupt=self.fleet.exits_on_interrupt
        )
        self.fleet.started.append(self.target)
        return self.child

    def discard(self) -> None:
        self.discarded = True


class Fleet:
    """The prepared children of one measurement and what happened to them."""

    def __init__(self, world: FakeWorld, store: job_slots.SlotStore) -> None:
        self.world = world
        self.store = store
        self.next_pid = 1000
        self.started: list[str] = []
        self.exits_on_interrupt = True
        self.prepared: dict[str, FakePrepared] = {}

    def plan(
        self, rules: list[TableRules], claim: Claim | None = None
    ) -> list[tuple[TableRules, FakePrepared]]:
        planned: list[tuple[TableRules, FakePrepared]] = []
        for entry in rules:
            prepared = FakePrepared(self, entry.target, claim)
            self.prepared[entry.target] = prepared
            planned.append((entry, prepared))
        return planned

    def child(self, target: str) -> FakeChild:
        child = self.prepared[target].child
        assert child is not None, f"{target} has not started"
        return child

    def live(self) -> list[str]:
        return [
            target
            for target, prepared in self.prepared.items()
            if prepared.child is not None and not prepared.child.ended
        ]


def light_claim(store: job_slots.SlotStore, pid: int) -> job_slots.SlotToken:
    """The first claim of a Flow child: a light-class entry of the interactive role."""
    return store.submit(job_slots.CLASS_LIGHT, pid=pid, role=job_slots.ROLE_INTERACTIVE)


def make_fleet(root: Path, world: FakeWorld, **caps: int) -> Fleet:
    return Fleet(world, make_store(root, world, SlotCaps(**caps)))


def by_target(outcome: ChildrenOutcome) -> dict[str, tuple[Verdict, ...]]:
    return {
        table_run.run.identity.split("#")[1]: tuple(r.verdict for r in table_run.run.results)
        for table_run in outcome.tables
    }


def test_children_all_start_cheapest_tier_first(root_dir: Path, world: FakeWorld):
    from booley.harness.measure_children import ChildScheduler, in_cost_tier_order

    flows = ("synth", "lint", "sim") * 3
    rules = [empty_table(f"t{number}-{flow}", flow) for number, flow in enumerate(flows)]
    ordered = in_cost_tier_order(rules)
    assert [entry.flow for entry in ordered] == ["lint"] * 3 + ["sim"] * 3 + ["synth"] * 3

    fleet = make_fleet(root_dir, world, max_light=1)
    scheduler = ChildScheduler(fleet.plan(ordered, light_claim), fleet.store, all_tables=True)
    scheduler.step(cancel=False)
    assert fleet.started == [entry.target for entry in ordered]  # none is held back
    assert len(fleet.live()) == 9
    while scheduler.step(cancel=False):
        fleet.child(fleet.live()[0]).finish()
    assert scheduler.outcome().status is RunStatus.COMPLETED


WAITING = "cdefghi"


def failing_script(root_dir: Path, world: FakeWorld, *, all_tables: bool):
    """Nine children (a, b and seven more), all started at once.

    ``a`` and ``b`` hold the two slots, the other seven wait behind them.
    """
    from booley.harness.measure_children import ChildScheduler

    fleet = make_fleet(root_dir, world, max_light=2)
    planned = fleet.plan([table(name) for name in "ab" + WAITING], light_claim)
    scheduler = ChildScheduler(planned, fleet.store, all_tables=all_tables)
    scheduler.step(cancel=False)
    assert fleet.started == list("ab" + WAITING)
    for name in "ab":
        assert fleet.store.refresh(fleet.child(name).token).state == job_slots.HOLDING  # type: ignore[arg-type]
    for name in WAITING:
        assert fleet.store.state_for_pid(fleet.child(name).pid).state == job_slots.QUEUED  # type: ignore[union-attr]
    return fleet, scheduler


@pytest.mark.parametrize("failing", ["unmet", "error"])
def test_a_failing_verdict_withdraws_waiting_children_and_keeps_running_ones(
    root_dir: Path, world: FakeWorld, failing: str
):
    from booley.harness.measure_children import STOPPED_REASON

    fleet, scheduler = failing_script(root_dir, world, all_tables=False)
    waiting = [fleet.child(name) for name in WAITING]

    fleet.child("a").finish(unmet() if failing == "unmet" else errored())
    assert scheduler.step(cancel=False)
    # The waiting children's claims are withdrawn; the running one keeps its slot.
    assert [fleet.store.state_for_pid(child.pid) for child in waiting] == [None] * 7
    assert fleet.store.state_for_pid(fleet.child("b").pid) is not None
    for child in waiting:
        child.finish(errored())  # a withdrawn child ends through its lost claim
    assert scheduler.step(cancel=False)

    fleet.child("b").finish(result(1))
    assert not scheduler.step(cancel=False)
    outcome = scheduler.outcome()

    assert outcome.status is RunStatus.STOPPED
    verdicts = by_target(outcome)
    assert verdicts["a"] in {(Verdict.UNMET,), (Verdict.ERROR,)}
    assert verdicts["b"] == (Verdict.MET,)
    assert {verdicts[name] for name in WAITING} == {(Verdict.NOT_EVALUATED,)}
    finished = {run.run.identity.split("#")[1]: run.finished for run in outcome.tables}
    assert finished == {"a": True, "b": True, **dict.fromkeys(WAITING, False)}
    walls = {run.run.identity.split("#")[1]: run.run.wall_time_s for run in outcome.tables}
    assert walls["b"] == 1.5
    assert {walls[name] for name in WAITING} == {None}
    details = {r.detail for run in outcome.tables[2:] for r in run.run.results}
    assert details == {STOPPED_REASON}
    scheduler.close()
    assert [child.settles for child in (*waiting, fleet.child("a"), fleet.child("b"))] == [1] * 9


def test_all_tables_runs_every_child_after_a_failing_verdict(root_dir: Path, world: FakeWorld):
    fleet, scheduler = failing_script(root_dir, world, all_tables=True)

    fleet.child("a").finish(unmet())
    assert scheduler.step(cancel=False)
    assert fleet.store.state_for_pid(fleet.child("c").pid) is not None  # not withdrawn
    assert fleet.started == list("ab" + WAITING)
    for name in "b" + WAITING:
        fleet.child(name).finish()
    assert not scheduler.step(cancel=False)
    outcome = scheduler.outcome()

    assert outcome.status is RunStatus.COMPLETED
    assert by_target(outcome) == {
        "a": (Verdict.UNMET,),
        **dict.fromkeys("b" + WAITING, (Verdict.MET,)),
    }
    assert all(run.finished for run in outcome.tables)


def test_cancel_withdraws_waiting_children_and_stops_running_ones(
    root_dir: Path, world: FakeWorld
):
    from booley.harness.measure_children import CANCELLED_REASON, ChildScheduler

    waiting_names = [f"waiting{number}" for number in range(7)]
    fleet = make_fleet(root_dir, world, max_light=1)
    scheduler = ChildScheduler(
        fleet.plan([table(name) for name in ("exited", "running", *waiting_names)], light_claim),
        fleet.store,
        all_tables=False,
    )
    scheduler.step(cancel=False)
    assert fleet.started == ["exited", "running", *waiting_names]  # all nine started
    exited, running = fleet.child("exited"), fleet.child("running")
    waiting = [fleet.child(name) for name in waiting_names]
    assert exited.token is not None and running.token is not None
    fleet.store.refresh(exited.token)
    # The first child exits between two ticks: its claim goes, the next one is promoted.
    fleet.store.release(exited.token)
    exited.finish()
    assert fleet.store.refresh(running.token).state == job_slots.HOLDING
    waiting_pids = [child.pid for child in waiting]

    assert not scheduler.step(cancel=True)
    outcome = scheduler.outcome()

    assert [fleet.store.state_for_pid(pid) for pid in waiting_pids] == [None] * 7  # withdrawn
    assert running.interrupted and running.settles == 1
    assert [child.settles for child in waiting] == [1] * 7 and exited.settles == 1
    assert fleet.live() == []
    assert all(prepared.child is not None for prepared in fleet.prepared.values())
    assert outcome.status is RunStatus.CANCELLED
    verdicts = by_target(outcome)
    assert verdicts["exited"] == (Verdict.MET,)  # it ended before the cancellation
    for name in ("running", *waiting_names):
        assert verdicts[name] == (Verdict.NOT_EVALUATED,)
    finished = {run.run.identity.split("#")[1]: run.finished for run in outcome.tables}
    assert finished == {"exited": True, "running": False, **dict.fromkeys(waiting_names, False)}
    assert {r.detail for r in outcome.tables[2].run.results} == {CANCELLED_REASON}


def test_a_second_interrupt_during_cleanup_still_kills_every_child(
    root_dir: Path, world: FakeWorld
):
    from booley.harness.measure_children import ChildScheduler

    fleet = make_fleet(root_dir, world, max_light=3)
    fleet.exits_on_interrupt = False
    scheduler = ChildScheduler(fleet.plan([table("a"), table("b")]), fleet.store, all_tables=True)
    scheduler.step(cancel=False)
    first, second = fleet.child("a"), fleet.child("b")
    first.raise_on_first_wait = True  # a second Ctrl-C while the first child is awaited

    with pytest.raises(KeyboardInterrupt):
        scheduler.step(cancel=True)
    assert first.killed and second.killed  # no sibling is skipped

    scheduler.close()  # the caller's ``finally``
    assert first.settles == 1 and second.settles == 1
    assert fleet.live() == []


def test_close_settles_every_child_even_when_one_settlement_fails(
    root_dir: Path, world: FakeWorld, caplog: pytest.LogCaptureFixture
):
    from booley.harness.measure_children import ChildScheduler

    fleet = make_fleet(root_dir, world, max_light=3)
    scheduler = ChildScheduler(
        fleet.plan([table("a"), table("b"), table("c")]), fleet.store, all_tables=True
    )
    scheduler.step(cancel=False)
    broken = fleet.child("a")

    def fail() -> CheckoutFlowResult:
        raise OSError("record unwritable")

    broken.settle = fail  # type: ignore[method-assign]

    scheduler.close()

    assert fleet.child("b").settles == 1 and fleet.child("c").settles == 1
    assert "could not settle" in caplog.text
    assert fleet.live() == []


def test_requeued_children_are_not_refused_and_report_their_final_attempt(
    root_dir: Path, world: FakeWorld
):
    from booley.harness.measure_children import ChildScheduler

    store = make_store(root_dir, world, SlotCaps())
    budget = 1000.0
    fleet = Fleet(world, store)
    names = list("abcdefghi")

    def first_attempt(store: job_slots.SlotStore, pid: int) -> job_slots.SlotToken:
        claim = job_slots.MemoryClaim(budget_mb=budget, reservation_mb=400)
        return store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=claim)

    scheduler = ChildScheduler(
        fleet.plan([table(name) for name in names], first_attempt), store, all_tables=True
    )
    scheduler.step(cancel=False)
    assert fleet.started == names  # all nine started; seven of them wait
    first, second = fleet.child("a"), fleet.child("b")
    assert first.token is not None and second.token is not None
    for child in (first, second):
        assert store.refresh(child.token).state == job_slots.HOLDING  # type: ignore[arg-type]

    # Both are killed for memory and claim again, alone: neither is refused behind the queue.
    requeued: dict[str, job_slots.SlotToken] = {}
    for child in (first, second):
        store.release(child.token)  # type: ignore[arg-type]
        claim = job_slots.MemoryClaim(budget_mb=budget, run_alone=True)
        requeued[str(child.pid)] = store.submit(
            job_slots.CLASS_MEMORY,
            pid=child.pid,
            memory=claim,
            queue_reason=OOM_REQUEUE_REASON,
        )
    scheduler.step(cancel=False)
    assert first.settles == 0 and second.settles == 0  # neither is finished while it waits
    assert store.refresh(requeued[str(first.pid)]).state == job_slots.QUEUED

    # The waiting children run in queue order; then each final attempt runs alone.
    for name in names[2:]:
        child = fleet.child(name)
        assert store.refresh(child.token).state == job_slots.HOLDING  # type: ignore[arg-type]
        child.finish()
        store.release(child.token)  # type: ignore[arg-type]
    assert store.refresh(requeued[str(first.pid)]).state == job_slots.HOLDING
    first.finish(result(2, elapsed_s=7.0))  # the verdicts come from the final attempt
    store.release(requeued[str(first.pid)])
    assert store.refresh(requeued[str(second.pid)]).state == job_slots.HOLDING
    second.finish(result(3, elapsed_s=9.0))
    assert not scheduler.step(cancel=False)
    outcome = scheduler.outcome()

    assert outcome.status is RunStatus.COMPLETED
    assert by_target(outcome) == dict.fromkeys(names, (Verdict.MET,))
    walls = {run.run.identity.split("#")[1]: run.run.wall_time_s for run in outcome.tables}
    assert walls["a"] == 7.0 and walls["b"] == 9.0
    values = [run.run.values for run in outcome.tables]
    assert [v.plain["warnings"] for v in values if v is not None] == [2, 3, *[0] * 7]


@pytest.fixture()
def root_dir(tmp_path: Path) -> Path:
    return tmp_path / "slots"


def test_a_running_flow_waiting_for_a_work_item_claim_is_not_withdrawn(
    root_dir: Path, world: FakeWorld
):
    """A Campaign's work items claim under the Flow's PID: that Flow is running, not waiting."""
    from booley.harness.measure_children import STOPPED_REASON, ChildScheduler

    fleet = make_fleet(root_dir, world, max_light=2)
    scheduler = ChildScheduler(
        fleet.plan([table("a"), table("b"), table("c")], light_claim),
        fleet.store,
        all_tables=False,
    )
    scheduler.step(cancel=False)
    campaign = fleet.child("b")
    for name in "ab":  # the two slots are taken: both children hold their admission
        assert fleet.store.refresh(fleet.child(name).token).state == job_slots.HOLDING  # type: ignore[arg-type]
    # Its admission is held; one of its work items waits for a slot of its own.
    item = fleet.store.submit(
        job_slots.CLASS_LIGHT, pid=campaign.pid, role=job_slots.ROLE_INTERACTIVE
    )
    assert fleet.store.refresh(item).state == job_slots.QUEUED  # type: ignore[arg-type]
    fleet.child("a").finish(unmet())
    scheduler.step(cancel=False)

    # The item's claim stands (it is queued, or promoted once a's slot went); never withdrawn.
    assert fleet.store.refresh(item).state in {job_slots.QUEUED, job_slots.HOLDING}  # type: ignore[arg-type]
    assert fleet.store.state_for_pid(fleet.child("c").pid) is None  # the waiting Flow was
    campaign.finish(result(1))
    scheduler.step(cancel=False)
    outcome = scheduler.outcome()
    assert by_target(outcome)["b"] == (Verdict.MET,)
    assert outcome.tables[1].finished
    assert STOPPED_REASON not in {r.detail for r in outcome.tables[1].run.results}


def test_a_second_interrupt_while_signalling_still_kills_every_child(
    root_dir: Path, world: FakeWorld
):
    from booley.harness.measure_children import ChildScheduler

    fleet = make_fleet(root_dir, world, max_light=3)
    fleet.exits_on_interrupt = False
    scheduler = ChildScheduler(fleet.plan([table("a"), table("b")]), fleet.store, all_tables=True)
    scheduler.step(cancel=False)
    first, second = fleet.child("a"), fleet.child("b")
    first.raise_on_interrupt = True  # a Ctrl-C lands while the children are being signalled

    with pytest.raises(KeyboardInterrupt):
        scheduler.close()

    assert first.killed and second.killed  # the one never signalled is killed too
    assert first.settles == 1 and second.settles == 1
    assert fleet.live() == []


def test_measure_tables_starts_children_cheapest_tier_first_and_reports_in_table_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.harness import measure_children

    started: list[str] = []

    class Instant:
        """A child that has already ended with a result."""

        pid = 4242

        def __init__(self, flow: str) -> None:
            self.flow = flow

        def start(self) -> Instant:
            started.append(self.flow)
            return self

        def discard(self) -> None:
            pass

        def poll(self) -> bool:
            return True

        def settle(self) -> CheckoutFlowResult:
            return met()

    def prepare(request: object) -> Instant:
        return Instant(request.flow)  # type: ignore[attr-defined]

    monkeypatch.setattr(measure_children, "prepare_checkout_flow", prepare)
    tables = [empty_table("a", "synth"), empty_table("b", "sim"), empty_table("c", "lint")]

    outcome = measure_children.measure_tables(
        tables,
        checkout=tmp_path,
        main_booley_dir=tmp_path,
        report_dir=tmp_path / "reports",
        timeout_s=None,
        all_tables=True,
        cancelled=lambda: False,
    )

    assert started == ["lint", "sim", "synth"]  # cheapest first
    assert [t.run.identity for t in outcome.tables] == [t.identity for t in tables]
