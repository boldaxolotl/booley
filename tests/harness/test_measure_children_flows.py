"""A measurement's child Flows run for real, as processes, within the admission queue (#3952).

The children are the stub Flow of ``test_checkout_flow.py`` (a light-class Flow with a
typed metric), run in an exported checkout whose Main Booley Directory holds the slot store.
The scheduler's decisions against scripted children are covered in ``test_measure_children.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from booley.config.jobs import SlotCaps
from booley.flows.metric_declarations import (
    Better,
    CostTier,
    FlowMetricValues,
    MetricDeclaration,
    MetricKind,
)
from booley.harness.standalone_job import standalone_jobs_dir, standalone_log_path
from booley.measurement import flow_metrics
from booley.measurement.metrics_file import TableRules, TrackedMetric
from booley.measurement.run_document import RunStatus, Verdict
from booley.runtime import job_records, job_slots
from tests.harness.test_checkout_flow import (
    FLOW,
    PROBE_CORE,
    TIMEOUT_S,
    World,
    WorldBuilder,
    assert_refused_before_any_run,
    linux_only,
    make_world,
    poll,
    use_fusesoc,
)

if TYPE_CHECKING:
    from booley.harness.measure_children import ChildrenOutcome

__all__ = ["make_world"]


def stub_table(target: str, flow: str = FLOW) -> TableRules:
    """A table of *flow* that tracks the stub's one metric for *target*."""
    tracked = TrackedMetric("marker", "marker", None, None)
    return TableRules(target, f"acme:ip:dut:0.1#{target}", flow, (), (tracked,))


def declare_stub_metrics(monkeypatch: pytest.MonkeyPatch, tier: CostTier = CostTier.LOW) -> None:
    """Give the stub Flow a cost tier and a reader for its typed ``marker`` detail."""

    def reader(detail: dict[str, object]) -> dict[str, FlowMetricValues]:
        if "marker" not in detail:  # a cancelled run reports no values
            return {}
        marker = int(str(detail["marker"]))
        return {"top": FlowMetricValues(plain={"marker": marker}, per_clock={})}

    declaration = MetricDeclaration("marker", "x", Better.LOWER, tier, MetricKind.PLAIN)
    monkeypatch.setitem(flow_metrics._DECLARATIONS_BY_FLOW, FLOW, (declaration,))
    monkeypatch.setitem(flow_metrics._READERS_BY_FLOW, FLOW, reader)


def measure(world: World, tables: list[TableRules], **options: object) -> ChildrenOutcome:
    """Measure *tables* in the world's checkout; ``all_tables`` and ``cancelled`` default off."""
    from booley.harness.measure_children import measure_tables

    arguments: dict[str, object] = {"all_tables": False, "cancelled": lambda: False}
    arguments.update(options)
    return measure_tables(
        tables,
        checkout=world.checkout,
        main_booley_dir=world.main,
        report_dir=world.report_dir,
        timeout_s=TIMEOUT_S,
        **arguments,  # type: ignore[arg-type]
    )


def started_targets(world: World) -> list[str]:
    """The targets whose Flow has started, in start order (the stub appends one line each)."""
    try:
        return (world.ctl / "order").read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return []


def processes_in(directory: Path) -> list[int]:
    """Processes whose working directory is *directory* (the children run in the checkout)."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "cwd").readlink() == directory:
                found.append(int(entry.name))
        except OSError:
            continue
    return found


@linux_only
def test_every_table_is_measured_without_a_refusal(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_stub_metrics(monkeypatch)
    # Main's settings rule: one slot, so four children wait behind the first.
    world = make_world()
    tables = [stub_table(f"metrics-{n}") for n in range(5)]

    outcome = measure(world, tables)

    assert outcome.status is RunStatus.COMPLETED
    assert [t.run.identity for t in outcome.tables] == [t.identity for t in tables]
    for measured in outcome.tables:
        # A child refused admission would have no values: an error verdict.
        assert [r.verdict for r in measured.run.results] == [Verdict.TRACKED]
        assert measured.finished and measured.reports
        assert measured.run.wall_time_s is not None
    assert sorted(started_targets(world)) == sorted(t.target for t in tables)


@linux_only
def test_cancelled_measurement_leaves_no_child_waiting_or_running(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    declare_stub_metrics(monkeypatch)
    world = make_world()
    # The caller's own slot directory must not be the one the scheduler watches.
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "elsewhere"))
    store = job_slots.SlotStore(job_slots.project_slots_dir(world.main), SlotCaps(max_light=1))

    def cancelled() -> bool:
        # One child runs (it records itself once admitted) and the other queues behind it.
        return len(started_targets(world)) == 1 and bool(store.snapshot(job_slots.CLASS_LIGHT)[1])

    tables = [stub_table("wait-a"), stub_table("wait-b")]
    outcome = measure(world, tables, cancelled=cancelled)

    assert outcome.status is RunStatus.CANCELLED
    assert [t.finished for t in outcome.tables] == [False, False]
    assert {r.verdict for t in outcome.tables for r in t.run.results} == {Verdict.NOT_EVALUATED}
    assert not processes_in(world.checkout)
    records = job_records.list_records(standalone_jobs_dir(world.main), skip_malformed=True)
    assert len(records) == 2 and all(r.status != job_records.STATUS_RUNNING for r in records)
    for job_class in job_slots.JOB_CLASSES:
        holders, waiters = store.snapshot(job_class)
        assert not holders and not waiters
    # The queued child was withdrawn from Main's store before it started: only the scheduler
    # watching Main's store (not the caller's ``BOOLEY_SLOTS_DIR``) can have done that.
    logs = [standalone_log_path(world.main, r.run_id).read_text(encoding="utf-8") for r in records]
    withdrawn = [log for log in logs if "withdrawn (booley_cancel) before it started" in log]
    assert len(withdrawn) == 1


@linux_only
@pytest.mark.parametrize("when", ["first", "later"])
def test_a_preflight_refusal_of_any_table_starts_no_child(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError

    use_fusesoc(monkeypatch)
    # The stub runs at the tier opposite the refused table: it comes after it, or before it.
    declare_stub_metrics(monkeypatch, CostTier.HIGH if when == "first" else CostTier.LOW)
    # Relative, so the static scan passes; FuseSoC resolves it into the live Booley Directory.
    world = make_world(
        files={"probe.core": PROBE_CORE.format(files="../../live/.booley/shared.v")}
    )
    (world.main / "shared.v").write_bytes(b"module top; endmodule\n")
    refused = stub_table("probe", "lint" if when == "first" else "synth")

    with pytest.raises(CheckoutFlowError, match=r"\[probe\.") as raised:
        measure(world, [stub_table("metrics-0"), refused])

    assert "Target probe" in str(raised.value)
    assert_refused_before_any_run(world)
    poll(lambda: not processes_in(world.checkout), "no child process")
