"""The stored Measurement Run document (schema 1) and its Measured predicate.

The writer-side types are built by ``booley measure`` (#3507). The document holds
nothing that depends on a base (no rules diff, deltas, warnings or colours); those
are derived at read time (#3504).

Timestamps keep microseconds, a deliberate deviation from principle 13's second
resolution: two runs of one key finishing in the same second must still order by time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from booley.flows.metric_declarations import FlowMetricValues
from booley.measurement.metrics_file import MetricsRules
from booley.measurement.run_key import KeyedCommit
from booley.measurement.toolchain import ToolchainFingerprint
from booley.runtime.timefmt import rfc3339_from_datetime

SCHEMA = 1


class RunStatus(StrEnum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    CANCELLED = "cancelled"


class Verdict(StrEnum):
    MET = "met"
    UNMET = "unmet"
    ERROR = "error"
    TRACKED = "tracked"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class MetricResult:
    """One reported metric; one rules entry."""

    key: str  # as written in metrics.toml, e.g. "clk_i.fmax_mhz_min" or "cell_count"
    bound: int | float | None  # None for a tracked metric
    value: int | float | None
    verdict: Verdict
    detail: str | None  # why error, unmet or not evaluated


@dataclass(frozen=True)
class TableRun:
    """One ``[<target>.<flow>]`` table: one child Flow."""

    identity: str  # ``vlnv#name``
    flow: str
    wall_time_s: float | None  # None when the Flow never started
    values: FlowMetricValues | None  # every metric the Flow returned, reported or not
    results: tuple[MetricResult, ...]


@dataclass(frozen=True)
class MeasurementRun:
    run_id: str  # the parent Job's run id
    keyed: KeyedCommit
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    wall_time_s: float
    rules: MetricsRules  # effective rules; the file path is not stored
    toolchain: ToolchainFingerprint
    protected_paths: tuple[str, ...]  # from #3504's helper; never read by ``is_measured``
    tables: tuple[TableRun, ...]


def tables_in_rules_order(run: MeasurementRun) -> list[tuple[TableRun, list[MetricResult]]]:
    """Each rules table with its run and its results in rules order (bounds, then tracked).

    Serialization walks the rules, so an omitted bound cannot make a run look Measured.

    Raises:
        ValueError: a rules table, a rules entry or a result has no counterpart (a writer bug).
    """
    runs = {(table.identity, table.flow): table for table in run.tables}
    ordered: list[tuple[TableRun, list[MetricResult]]] = []
    for rules in run.rules.tables:
        table = runs.pop((rules.identity, rules.flow), None)
        if table is None:
            raise ValueError(f"no run for rules table {rules.identity} {rules.flow}")
        results = {result.key: result for result in table.results}
        keys = [entry.key for entry in (*rules.bounds, *rules.tracked)]
        if len(keys) != len(table.results) or set(keys) != set(results):
            raise ValueError(f"results of {rules.identity} {rules.flow} do not match its rules")
        ordered.append((table, [results[key] for key in keys]))
    if runs:
        raise ValueError(f"runs without rules: {sorted(runs)}")
    return ordered


def run_document(
    run: MeasurementRun, kept: Sequence[Mapping[str, Any]], dropped: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build the schema 1 document; *kept* and *dropped* describe the text reports."""
    keyed = run.keyed
    return {
        "schema": SCHEMA,
        "run_id": run.run_id,
        "key": asdict(keyed.key),
        "booley_commit": keyed.booley_commit,
        "commit": {"sha": keyed.commit, "message": keyed.message, "parents": keyed.parents},
        "status": run.status,
        "started_at": rfc3339_from_datetime(run.started_at, microseconds=True),
        "finished_at": rfc3339_from_datetime(run.finished_at, microseconds=True),
        "wall_time_s": run.wall_time_s,
        "toolchain": asdict(run.toolchain),
        "rules": [asdict(rules) for rules in run.rules.tables],
        "protected_paths": run.protected_paths,
        "tables": [
            {
                "identity": table.identity,
                "flow": table.flow,
                "wall_time_s": table.wall_time_s,
                "values": None if table.values is None else asdict(table.values),
                "results": [asdict(result) for result in results],
            }
            for table, results in tables_in_rules_order(run)
        ],
        "reports": list(kept),
        "dropped_reports": list(dropped),
    }


def is_measured(document: Mapping[str, Any]) -> bool:
    """Fail closed: completed, at least one result, every bound met, every tracked value reported."""
    if document["status"] != RunStatus.COMPLETED:
        return False
    results = [result for table in document["tables"] for result in table["results"]]
    return bool(results) and all(
        result["verdict"] == (Verdict.MET if result["bound"] is not None else Verdict.TRACKED)
        for result in results
    )
