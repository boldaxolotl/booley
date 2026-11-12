"""Registry of the metric declarations of every Flow that declares metrics."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from booley.flows.fpga.backends.vivado.metrics import FPGA_METRIC_DECLARATIONS
from booley.flows.fpga.backends.vivado.metrics import report_metric_values as _fpga_values
from booley.flows.lint.flow import LINT_METRIC_DECLARATIONS
from booley.flows.metric_declarations import FlowMetricValues, MetricDeclaration
from booley.flows.sim.metrics import COVERAGE_METRIC_DECLARATIONS, SIM_METRIC_DECLARATIONS
from booley.flows.synth.flow import SYNTH_METRIC_DECLARATIONS
from booley.flows.synth.flow import report_metric_values as _synth_values

# The Flow's ``metrics.toml`` table name -> its declarations. Elaboration Check has no table.
_DECLARATIONS_BY_FLOW: dict[str, tuple[MetricDeclaration, ...]] = {
    "lint": LINT_METRIC_DECLARATIONS,
    "sim": SIM_METRIC_DECLARATIONS,
    "coverage": COVERAGE_METRIC_DECLARATIONS,
    "synth": SYNTH_METRIC_DECLARATIONS,
    "fpga": FPGA_METRIC_DECLARATIONS,
}
# Booley Flow name -> reader of its typed values from a report's ``detail``.
_READERS_BY_FLOW: dict[str, Callable[[dict[str, Any]], dict[str, FlowMetricValues]]] = {
    "synth": _synth_values,
    "fpga": _fpga_values,
}


def flows_with_metric_declarations() -> tuple[str, ...]:
    """Names of the Flows that declare metrics."""
    return tuple(_DECLARATIONS_BY_FLOW)


def metric_declarations(flow: str) -> tuple[MetricDeclaration, ...]:
    """Return the metrics *flow* declares; raise ``LookupError`` for any other Flow."""
    try:
        return _DECLARATIONS_BY_FLOW[flow]
    except KeyError:
        raise LookupError(
            f"Flow {flow!r} declares no metrics; Flows that do: {', '.join(_DECLARATIONS_BY_FLOW)}"
        ) from None


def report_metric_values(flow: str, detail: dict[str, Any]) -> dict[str, FlowMetricValues]:
    """Typed metric values per Target from a *flow* report's ``detail``; ``{}`` if it declares none."""
    reader = _READERS_BY_FLOW.get(flow)
    return {} if reader is None else reader(detail)
