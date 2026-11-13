"""The base-run view: Δ, base-run warnings and the commands it gives (#3698)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from booley.flows.metric_declarations import FlowMetricValues
from booley.measurement.base import Base
from booley.measurement.metrics_file import Bound, MetricsRules, TableRules, TrackedMetric
from booley.measurement.run_document import (
    MeasurementRun,
    MetricResult,
    RunStatus,
    TableRun,
    Verdict,
)
from booley.measurement.run_store import MeasurementStore
from tests.measurement.test_run_store import fingerprint, in_tree_repository, key_of
from tests.runtime.test_commit_export import commit_all, git, write

if TYPE_CHECKING:
    from booley.measurement.base_run import BaseRunView, MetricDelta
    from booley.measurement.toolchain import ToolchainFingerprint

EPOCH = datetime(2026, 2, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Entry:
    """One reported metric of a table: a bound when ``bound`` is set, else tracked."""

    key: str
    metric: str
    value: float | None
    bound: float | None = None
    prefix: str | None = None
    test_run: str | None = None
    verdict: Verdict | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Table:
    """One rules table with the run of it."""

    flow: str
    entries: tuple[Entry, ...] = ()
    values: FlowMetricValues | None = None
    identity: str = "acme:core:1.0#top"
    target: str = "core"


def values(
    plain: dict[str, float] | None = None,
    per_clock: dict[str, dict[str, float]] | None = None,
    per_test: dict[str, dict[str, float]] | None = None,
) -> FlowMetricValues:
    return FlowMetricValues(plain or {}, per_clock or {}, per_test or {})


def build_run(
    keyed: Any,
    run_id: str,
    tables: tuple[Table, ...],
    *,
    minute: int = 0,
    status: RunStatus = RunStatus.COMPLETED,
    toolchain: ToolchainFingerprint | None = None,
    verdict: Verdict | None = None,
) -> MeasurementRun:
    """A run whose rules and results come from the same *tables*; *verdict* overrides all."""
    rules: list[TableRules] = []
    runs: list[TableRun] = []
    for table in tables:
        bounds: list[Bound] = []
        tracked: list[TrackedMetric] = []
        results: list[MetricResult] = []
        for entry in table.entries:
            limit = "min" if entry.key.endswith("_min") else "max"
            if entry.bound is None:
                tracked.append(
                    TrackedMetric(entry.key, entry.metric, entry.prefix, entry.test_run)
                )
                default = Verdict.TRACKED
            else:
                bounds.append(
                    Bound(
                        entry.key,
                        entry.metric,
                        limit,
                        entry.bound,
                        entry.prefix,
                        entry.test_run,
                        None,
                    )
                )
                default = Verdict.MET
            results.append(
                MetricResult(
                    entry.key,
                    entry.bound,
                    entry.value,
                    verdict or entry.verdict or default,
                    entry.detail,
                )
            )
        rules.append(
            TableRules(table.target, table.identity, table.flow, tuple(bounds), tuple(tracked))
        )
        runs.append(TableRun(table.identity, table.flow, 1.0, table.values, tuple(results)))
    finished = EPOCH + timedelta(minutes=minute)
    return MeasurementRun(
        run_id,
        keyed,
        status,
        finished - timedelta(seconds=30),
        finished,
        30.0,
        MetricsRules(Path("metrics.toml"), tuple(rules)),
        toolchain or fingerprint(),
        (),
        tuple(runs),
    )


@dataclass(frozen=True)
class World:
    """A repository with a Base commit and a head commit, and the store beside them."""

    repository: Path
    store: MeasurementStore
    base: Base
    base_keyed: Any
    head_keyed: Any

    def store_base(self, *args: Any, **kwargs: Any) -> None:
        self.store.write(build_run(self.base_keyed, *args, **kwargs), [])

    def head_document(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.store.write(build_run(self.head_keyed, *args, **kwargs), []).document

    def view(self, head: dict[str, Any]) -> BaseRunView:
        return self.view_against(head, self.base)

    def view_against(self, head: dict[str, Any], base: Base) -> BaseRunView:
        from booley.measurement.base_run import base_run_view

        return base_run_view(self.repository, self.store, head, base)


@pytest.fixture
def world(tmp_path: Path) -> World:
    repository, base_sha = in_tree_repository(tmp_path / "repo")
    write(repository / "rtl.v", "module b; endmodule\n")
    head_sha = commit_all(repository, "head")
    return World(
        repository,
        MeasurementStore(repository / ".booley"),
        Base(base_sha, None, None, None),
        key_of(repository, base_sha),
        key_of(repository, head_sha),
    )


def delta_of(view: BaseRunView, flow: str, key: str) -> MetricDelta:
    (found,) = [d for d in view.deltas if (d.flow, d.key) == (flow, key)]
    return found


def test_delta_for_every_reported_metric_with_a_value_in_both_runs(world: World) -> None:
    from booley.measurement.base_run import NoDelta

    head_tables = (
        Table(
            "synth",
            (
                Entry("cell_count_max", "cell_count", 90, bound=100),
                Entry("area_um2", "area_um2", 12.5),
                Entry("clk_i.fmax_mhz_min", "fmax_mhz", 210.0, bound=100, prefix="clk_i"),
                Entry("critical_path_ps", "critical_path_ps", 900),  # bare, one clock in base
                Entry("wns_ns", "wns_ns", 0.5),  # bare, two clocks in base
                Entry("area_kge", "area_kge", 3.0),  # metric absent in base
            ),
            identity="acme:core:1.1#top",  # a version bump must still pair the tables
        ),
        Table("sim", (Entry("cycles", "cycle_count", 70, prefix="smoke", test_run="smoke_1"),)),
        Table("coverage", (Entry("line_coverage", "line_coverage", 80),)),  # base values: null
        Table("fpga", (Entry("lut_count", "lut_count", 5),)),  # no base table
        Table(
            "lint",
            (Entry("errors_max", "errors", None, bound=0, verdict=Verdict.ERROR),),
        ),
    )
    base_tables = (
        Table(
            "synth",
            values=values(
                plain={"cell_count": 100, "area_um2": 10.0},
                per_clock={
                    "fmax_mhz": {"clk_i": 200.0, "clk_j": 1.0},
                    "critical_path_ps": {"only": 1000},
                    "wns_ns": {"a": 1.0, "b": 2.0},
                },
            ),
            identity="acme:core:1.0#top",
            entries=(
                Entry("cell_count_max", "cell_count", 100, bound=100),
                Entry("area_um2", "area_um2", 10.0),
                Entry("clk_i.fmax_mhz_min", "fmax_mhz", 200.0, bound=100, prefix="clk_i"),
                Entry("critical_path_ps", "critical_path_ps", 1000),
                Entry("wns_ns", "wns_ns", 1.0),
                Entry("area_kge", "area_kge", 0.0),
            ),
        ),
        Table(
            "sim",
            (Entry("cycles", "cycle_count", 100, prefix="smoke", test_run="smoke_1"),),
            values(per_test={"cycle_count": {"smoke_1": 100}}),
        ),
        Table("coverage", (Entry("line_coverage", "line_coverage", 70),), None),
        Table("lint", (Entry("errors_max", "errors", 0, bound=0),), values()),
    )
    world.store_base("base", base_tables)
    head = world.head_document("head", head_tables, minute=1)

    view = world.view(head)

    expected: dict[tuple[str, str], tuple[Any, Any]] = {
        ("synth", "cell_count_max"): (-10, None),
        ("synth", "area_um2"): (2.5, None),
        ("synth", "clk_i.fmax_mhz_min"): (10.0, None),
        ("synth", "critical_path_ps"): (-100, None),
        ("synth", "wns_ns"): (None, NoDelta.NO_BASE_VALUE),
        ("synth", "area_kge"): (None, NoDelta.NO_BASE_VALUE),
        ("sim", "cycles"): (-30, None),
        ("coverage", "line_coverage"): (None, NoDelta.NO_BASE_VALUE),
        ("fpga", "lut_count"): (None, NoDelta.NO_BASE_VALUE),
        ("lint", "errors_max"): (None, NoDelta.NO_BASE_VALUE),
    }
    assert {(d.flow, d.key): (d.delta, d.missing) for d in view.deltas} == expected
    assert [d.flow for d in view.deltas][:2] == ["synth", "synth"]  # head rules order
    cells = delta_of(view, "synth", "cell_count_max")
    assert (cells.value, cells.base_value) == (90, 100)
    assert delta_of(view, "lint", "errors_max").value is None
    assert (view.base, view.base_run_id, view.command, view.problem) == (
        world.base.commit,
        "base",
        None,
        None,
    )


def test_worse_metric_is_warned_by_its_better_direction(world: World) -> None:
    head_tables = (
        Table(
            "synth",
            (
                Entry("cell_count_max", "cell_count", 95, bound=100),  # lower is better: worse
                Entry("area_um2", "area_um2", 10.0),  # equal: not worse
                Entry("area_kge", "area_kge", 2.0),  # lower is better: improved
                Entry("clk_i.fmax_mhz_min", "fmax_mhz", 150.0, bound=100, prefix="clk_i"),  # worse
                Entry("wns_ns", "wns_ns", 0.1),  # bare per-clock, higher is better: worse
                Entry("whs_ns", "whs_ns", 0.9),  # higher is better: improved
            ),
        ),
    )
    base_tables = (
        Table(
            "synth",
            entries=head_tables[0].entries,
            values=values(
                plain={"cell_count": 90, "area_um2": 10.0, "area_kge": 5.0},
                per_clock={
                    "fmax_mhz": {"clk_i": 200.0},
                    "wns_ns": {"clk_i": 0.4},
                    "whs_ns": {"clk_i": 0.2},
                },
            ),
        ),
    )
    world.store_base("base", base_tables)

    view = world.view(world.head_document("head", head_tables, minute=1))

    assert [(w.key, w.base_value, w.value, w.better.value) for w in view.worsened] == [
        ("cell_count_max", 90, 95, "lower"),
        ("clk_i.fmax_mhz_min", 200.0, 150.0, "higher"),
        ("wns_ns", 0.4, 0.1, "higher"),
    ]


def test_tests_total_drop_is_warned_even_when_not_reported(world: World) -> None:
    def table(flow: str, total: float | None, *, with_values: bool = True) -> Table:
        plain = {} if total is None else {"tests_total": total}
        return Table(
            flow,
            (Entry("tests_failed", "tests_failed", 0),),
            values(plain) if with_values else None,
        )

    head_tables = (
        table("sim", 8),  # dropped
        table("coverage", 12),  # grew
        Table(  # same total: not a drop
            "sim",
            (Entry("tests_failed", "tests_failed", 0),),
            values({"tests_total": 5}),
            "acme:core:1.0#other",
            "other",
        ),
        Table("fpga", (Entry("x", "lut_count", 0),), values({"tests_total": 1})),  # no base total
        Table("synth", (Entry("cell_count", "cell_count", 1),), None),  # head values null
    )
    base_tables = (
        table("sim", 10),
        table("coverage", 10),
        Table(
            "sim",
            (Entry("tests_failed", "tests_failed", 0),),
            values({"tests_total": 5}),
            "acme:core:1.0#other",
            "other",
        ),
        Table("fpga", (Entry("x", "lut_count", 0),), values()),
        Table("synth", (Entry("cell_count", "cell_count", 1),), values({"tests_total": 3})),
    )
    world.store_base("base", base_tables)

    view = world.view(world.head_document("head", head_tables, minute=1))

    assert [(t.flow, t.base_total, t.total) for t in view.tests_dropped] == [("sim", 10, 8)]
    assert view.worsened == ()  # tests_failed is unchanged: only tests_total warns


def test_toolchain_difference_is_warned_with_the_fields_that_differ(world: World) -> None:
    cases: list[tuple[dict[str, Any], tuple[str, ...]]] = [
        ({}, ()),
        ({"image": "sha256:img"}, ("image",)),
        ({"booley": "sha256:b"}, ("booley",)),
        (
            {"booley_kind": "source", "booley_note": "no build identity"},
            ("booley_kind", "booley_note"),
        ),
        ({"booley_note": "only a note"}, ("booley_note",)),
        ({"tools": {"yosys": "0.2"}}, ("tools.yosys",)),
        ({"tools": {"yosys": "0.1", "verilator": "5"}}, ("tools.verilator",)),
        ({"tools": {}}, ("tools.yosys",)),
    ]
    tables = (Table("synth", (Entry("cell_count", "cell_count", 1),), values({"cell_count": 1})),)
    world.store_base("base", tables, toolchain=fingerprint())

    for number, (changed, expected) in enumerate(cases):
        toolchain = replace(fingerprint(), **changed)
        head = world.head_document(f"head{number}", tables, minute=1 + number, toolchain=toolchain)

        assert world.view(head).toolchain_changes == expected, changed


def test_without_a_base_run_delta_reads_no_base_run_and_gives_the_command(world: World) -> None:
    from booley.measurement.base_run import NoDelta

    tables = (Table("synth", (Entry("cell_count", "cell_count", 1),), values({"cell_count": 1})),)
    # The store holds the head's own run, a run of another tree: it is not the base run.
    head = world.head_document("head", tables)

    view = world.view(head)

    assert [(d.missing, d.delta) for d in view.deltas] == [(NoDelta.NO_BASE_RUN, None)]
    assert view.command == f"booley measure --all {world.base.commit}"
    assert (view.base, view.base_run_id, view.problem) == (world.base.commit, None, None)
    assert (view.worsened, view.tests_dropped, view.toolchain_changes) == ((), (), ())
    assert [p.name for p in (world.store._root).iterdir()] == [  # pyright: ignore[reportPrivateUsage]
        world.head_keyed.key.project_tree
    ]  # nothing was started or stored by the view


def test_without_a_base_delta_reads_no_base_and_gives_no_command(world: World) -> None:
    from booley.measurement.base_run import NoDelta

    tables = (Table("synth", (Entry("cell_count", "cell_count", 1),), values({"cell_count": 1})),)
    head = world.head_document("head", tables)

    view = world.view_against(head, Base(None, None, None, "no default branch"))

    assert [(d.missing, d.delta) for d in view.deltas] == [(NoDelta.NO_BASE, None)]
    assert (view.base, view.command, view.problem, view.base_run_id) == (None, None, None, None)


def test_unreadable_base_run_is_reported_without_a_command(world: World) -> None:
    from booley.measurement.base_run import NoDelta

    tables = (Table("synth", (Entry("cell_count", "cell_count", 1),), values({"cell_count": 1})),)
    head = world.head_document("head", tables)
    base_tree = git(world.repository, "rev-parse", f"{world.base.commit}^{{tree}}")
    write(world.store._root / base_tree / "bad.json", "{not json")  # pyright: ignore[reportPrivateUsage]

    view = world.view(head)

    assert [d.missing for d in view.deltas] == [NoDelta.NO_BASE_RUN]
    assert view.command is None
    assert view.problem is not None and "bad.json" in view.problem


def test_unmet_or_error_base_run_leaves_the_measured_run_unchanged(world: World) -> None:
    from booley.measurement.run_document import is_measured

    entries = (Entry("cell_count_max", "cell_count", 95, bound=100),)
    cases = [
        (Verdict.UNMET, RunStatus.COMPLETED),
        (Verdict.ERROR, RunStatus.COMPLETED),
        (Verdict.MET, RunStatus.STOPPED),
    ]
    head = world.head_document(
        "head", (Table("synth", entries, values({"cell_count": 95})),), minute=0
    )
    before = copy.deepcopy(head)
    assert is_measured(head)

    for number, (verdict, status) in enumerate(cases):
        # The newest base run is the base run, so each case replaces the one before.
        world.store_base(
            f"base{number}",
            (Table("synth", entries, values({"cell_count": 90})),),
            minute=10 + number,
            status=status,
            verdict=verdict,
        )

        view = world.view(head)

        assert head == before
        assert is_measured(head)
        assert view.base_run_id == f"base{number}"
        assert [(d.key, d.delta) for d in view.deltas] == [("cell_count_max", 5)]
        assert [w.key for w in view.worsened] == ["cell_count_max"]
