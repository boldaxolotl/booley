"""The base-run view of a measured commit: Δ and base-run warnings (ADR 0092, #3045, #3698).

Contract for the caller (``booley measure``, #3507; rendering and ``--json`` are #3505):
compute :func:`base_run_view` at read time from the head run's stored document and the
resolved :class:`~booley.measurement.base.Base`. The view is derived, never stored, and
it never feeds a verdict, ``is_measured``, the header state or the exit code: it only
reads the store and the head document, and it never starts a run. A stored document
the store cannot load (unreadable, unknown schema) is reported in
:attr:`BaseRunView.problem` instead of raised. The documents are trusted past that check
because Booley wrote them: the head document must come from the running Booley (a renamed
metric or Flow would fail the lookup), and per-test rows need rules resolved against a
checkout (``test_run`` set), else they read "no base value".

``tests_total`` is compared for every paired table, so when it is also a reported metric
a drop appears both in :attr:`BaseRunView.worsened` and in
:attr:`BaseRunView.tests_dropped`; the renderer (#3505) may merge the two.

A base run is the latest stored run of the Base's project tree, whatever its rules tree
or status, so an unmet, errored or stopped base run still gives Δ and warnings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.flows.metric_declarations import Better, MetricKind
from booley.measurement.base import Base
from booley.measurement.flow_metrics import metric_declarations
from booley.measurement.run_store import MeasurementStore, MeasurementStoreError
from booley.runtime.pinned_history import raw_git

_TESTS_TOTAL = "tests_total"


class NoDelta(StrEnum):
    """Why a Δ cannot be computed; the value is the human wording."""

    NO_BASE = "no base"
    NO_BASE_RUN = "no base run"
    NO_BASE_VALUE = "no base value"


@dataclass(frozen=True)
class MetricDelta:
    """Δ of one reported metric: head minus base, or why there is none."""

    identity: str
    flow: str
    key: str  # as written in metrics.toml, the head result's key
    value: int | float | None  # the head value, what its verdict judged
    base_value: int | float | None
    delta: int | float | None
    missing: NoDelta | None  # None exactly when ``delta`` is set


@dataclass(frozen=True)
class Worsened:
    """A reported metric that moved against its declared better direction."""

    identity: str
    flow: str
    key: str
    base_value: int | float
    value: int | float
    better: Better


@dataclass(frozen=True)
class TestsDropped:
    """A table whose ``tests_total`` is lower than in the base run."""

    __test__ = False  # not a pytest class, whatever the name says

    identity: str
    flow: str
    base_total: int | float
    total: int | float


@dataclass(frozen=True)
class BaseRunView:
    """Everything a base run adds to a Measurement Report; it holds no verdict."""

    base: str | None  # the Base commit; None means no Base
    base_run_id: str | None
    command: str | None  # the command that would create the base run, when one could
    problem: str | None  # the store error that made the base run unreadable
    deltas: tuple[MetricDelta, ...]  # one per head result, in head rules order
    worsened: tuple[Worsened, ...]
    tests_dropped: tuple[TestsDropped, ...]
    toolchain_changes: tuple[str, ...]  # differing fingerprint fields; empty = same toolchain


@dataclass(frozen=True)
class _Row:
    """One head result with the address of its value."""

    identity: str
    flow: str
    key: str
    metric: str
    where: str | None  # a clock name or the Test Run; None for a plain or bare key
    value: int | float | None
    better: Better
    kind: MetricKind


def _head_rows(head: Mapping[str, Any]) -> list[_Row]:
    """Every result of the head document with where to find its metric."""
    entries: dict[tuple[str, str, str], tuple[str, str | None]] = {}
    for rules in head["rules"]:
        for entry in (*rules["bounds"], *rules["tracked"]):
            where = entry["test_run"] or entry["prefix"]
            entries[(rules["identity"], rules["flow"], entry["key"])] = (entry["metric"], where)
    rows: list[_Row] = []
    for table in head["tables"]:
        declared = {item.name: item for item in metric_declarations(table["flow"])}
        for result in table["results"]:
            metric, where = entries[(table["identity"], table["flow"], result["key"])]
            declaration = declared[metric]
            rows.append(
                _Row(
                    table["identity"],
                    table["flow"],
                    result["key"],
                    metric,
                    where,
                    result["value"],
                    declaration.better,
                    declaration.kind,
                )
            )
    return rows


def _targets(document: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    """``(identity, flow)`` -> Target key as written, from the document's own rules."""
    return {(rules["identity"], rules["flow"]): rules["target"] for rules in document["rules"]}


def _tables_by_target(document: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Tables keyed by ``(Target key as written, flow)``, so a version bump still pairs them."""
    targets = _targets(document)
    return {
        (targets[(table["identity"], table["flow"])], table["flow"]): table
        for table in document["tables"]
    }


def _base_value(base_table: Mapping[str, Any] | None, row: _Row) -> int | float | None:
    """The row's metric in the base table's values, None when the base has none."""
    values = None if base_table is None else base_table["values"]
    if values is None:
        return None
    if row.kind is MetricKind.PLAIN:
        return values["plain"].get(row.metric)
    if row.kind is MetricKind.PER_TEST:
        return values["per_test"].get(row.metric, {}).get(row.where)
    clocks: Mapping[str, int | float] = values["per_clock"].get(row.metric, {})
    if row.where is None:  # a bare key of a single-clock design
        return next(iter(clocks.values())) if len(clocks) == 1 else None
    return clocks.get(row.where)


def _tests_dropped(
    head_tables: Mapping[tuple[str, str], Mapping[str, Any]],
    base_tables: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[TestsDropped, ...]:
    """Paired tables whose ``tests_total`` fell, reported or not."""
    dropped: list[TestsDropped] = []
    for target_flow, table in head_tables.items():
        base_table = base_tables.get(target_flow)
        if base_table is None or table["values"] is None or base_table["values"] is None:
            continue
        total = table["values"]["plain"].get(_TESTS_TOTAL)
        base_total = base_table["values"]["plain"].get(_TESTS_TOTAL)
        if total is not None and base_total is not None and total < base_total:
            dropped.append(TestsDropped(table["identity"], table["flow"], base_total, total))
    return tuple(dropped)


def _toolchain_changes(head: Mapping[str, Any], base: Mapping[str, Any]) -> tuple[str, ...]:
    """Names of the fingerprint fields that differ, sorted; tools as ``tools.<name>``."""
    changed = [
        name
        for name in ("image", "booley", "booley_kind", "booley_note")
        if head[name] != base[name]
    ]
    head_tools, base_tools = head["tools"], base["tools"]
    changed.extend(
        f"tools.{name}"
        for name in {*head_tools, *base_tools}
        if head_tools.get(name) != base_tools.get(name)
    )
    return tuple(sorted(changed))


def _without_base_run(head: Mapping[str, Any], why: NoDelta) -> tuple[MetricDelta, ...]:
    """One Δ-less row per head result, all for the same reason *why*."""
    return tuple(
        MetricDelta(row.identity, row.flow, row.key, row.value, None, None, why)
        for row in _head_rows(head)
    )


def _compare(head: Mapping[str, Any], base: Mapping[str, Any]) -> BaseRunView:
    """Δ and the three warnings of *head* against *base*, both stored documents."""
    head_tables, base_tables = _tables_by_target(head), _tables_by_target(base)
    head_targets = _targets(head)
    by_head_identity = {
        (identity, flow): base_tables.get((target, flow))
        for (identity, flow), target in head_targets.items()
    }
    deltas: list[MetricDelta] = []
    worsened: list[Worsened] = []
    for row in _head_rows(head):
        base_value = _base_value(by_head_identity[(row.identity, row.flow)], row)
        if row.value is not None and base_value is not None:
            delta = row.value - base_value
            deltas.append(
                MetricDelta(row.identity, row.flow, row.key, row.value, base_value, delta, None)
            )
            against = delta > 0 if row.better is Better.LOWER else delta < 0
            if against:
                worsened.append(
                    Worsened(row.identity, row.flow, row.key, base_value, row.value, row.better)
                )
        else:
            deltas.append(
                MetricDelta(
                    row.identity,
                    row.flow,
                    row.key,
                    row.value,
                    base_value,
                    None,
                    NoDelta.NO_BASE_VALUE,
                )
            )
    return BaseRunView(
        base=None,
        base_run_id=None,
        command=None,
        problem=None,
        deltas=tuple(deltas),
        worsened=tuple(worsened),
        tests_dropped=_tests_dropped(head_tables, base_tables),
        toolchain_changes=_toolchain_changes(head["toolchain"], base["toolchain"]),
    )


def base_run_view(
    repository: Path, store: MeasurementStore, head: Mapping[str, Any], base: Base
) -> BaseRunView:
    """The Δ and base-run warnings of the *head* run document against the Base's run.

    No Base: every Δ reads "no base" and no command is given. A Base the store has no run
    of: every Δ reads "no base run" and ``command`` would create it (never started here).
    An unreadable stored document: ``problem`` carries the store's message, every Δ reads
    "no base run" and there is no command, because a new run would not heal the lookup.
    """
    if base.commit is None:
        return BaseRunView(
            base=None,
            base_run_id=None,
            command=None,
            problem=None,
            deltas=_without_base_run(head, NoDelta.NO_BASE),
            worsened=(),
            tests_dropped=(),
            toolchain_changes=(),
        )
    tree = raw_git(repository, "rev-parse", "--verify", f"{base.commit}^{{tree}}")
    try:
        run = store.base_run(tree.decode("ascii").strip())
    except MeasurementStoreError as error:
        deltas = _without_base_run(head, NoDelta.NO_BASE_RUN)
        return BaseRunView(
            base=base.commit,
            base_run_id=None,
            command=None,
            problem=str(error),
            deltas=deltas,
            worsened=(),
            tests_dropped=(),
            toolchain_changes=(),
        )
    if run is None:
        deltas = _without_base_run(head, NoDelta.NO_BASE_RUN)
        command = f"booley measure --all {base.commit}"
        return BaseRunView(
            base=base.commit,
            base_run_id=None,
            command=command,
            problem=None,
            deltas=deltas,
            worsened=(),
            tests_dropped=(),
            toolchain_changes=(),
        )
    compared = _compare(head, run.document)
    return replace(compared, base=base.commit, base_run_id=run.run_id)
