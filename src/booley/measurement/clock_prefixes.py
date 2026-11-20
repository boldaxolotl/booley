"""Per-clock keys of ``metrics.toml`` that name a clock no stored Measurement Run reported."""

from __future__ import annotations

from typing import TYPE_CHECKING

from booley.flows.metric_declarations import MetricKind
from booley.measurement.flow_metrics import metric_declarations
from booley.measurement.metrics_file import table_header

if TYPE_CHECKING:
    from collections.abc import Sequence

    from booley.measurement.metrics_file import TableRules
    from booley.measurement.run_store import MeasurementStore


def unreported_clock_warnings(
    tables: Sequence[TableRules], store: MeasurementStore
) -> tuple[str, ...]:
    """One line per per-clock bound or tracked metric whose clock no stored run reported.

    A table with no stored clock is skipped: its first measurement has not run yet.
    """
    lines: list[str] = []
    for table in tables:
        per_clock = {
            d.name for d in metric_declarations(table.flow) if d.kind is MetricKind.PER_CLOCK
        }
        entries = [
            entry
            for entry in (*table.bounds, *table.tracked)
            if entry.prefix is not None and entry.metric in per_clock
        ]
        known = store.reported_clocks(table.identity, table.flow) if entries else frozenset()
        listed = ", ".join(sorted(known))
        lines.extend(
            f"{table_header(table.target, table.flow)} {entry.key}: "
            f"clock {entry.prefix} was never reported; reported clocks: {listed}"
            for entry in entries
            if known and entry.prefix not in known
        )
    return tuple(lines)
