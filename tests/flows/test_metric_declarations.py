"""Typed metric declarations of the synth and FPGA Flows (issue #3496).

Each Flow declares the metrics it emits (name, unit, better direction, cost tier,
kind); a run returns typed values through ``flow_metrics()``. The modules under
test are imported inside the test bodies so a missing module fails the test, not
collection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "vivado_reports"

# name -> (unit, better, kind), written out literally so a wrong direction, unit or
# kind in a declaration cannot hide behind the code that produced it.
_EXPECTED_DECLARATIONS: dict[str, dict[str, tuple[str, str, str]]] = {
    "synth": {
        "area_um2": ("μm²", "lower", "plain"),
        "area_kge": ("kGE", "lower", "plain"),
        "cell_count": ("cells", "lower", "plain"),
        "fmax_mhz": ("MHz", "higher", "per_clock"),
        "critical_path_ps": ("ps", "lower", "per_clock"),
        "wns_ns": ("ns", "higher", "per_clock"),
        "whs_ns": ("ns", "higher", "per_clock"),
    },
    "fpga": {
        "lut_count": ("LUTs", "lower", "plain"),
        "ff_count": ("FFs", "lower", "plain"),
        "bram_count": ("BRAM tiles", "lower", "plain"),
        "dsp_count": ("DSPs", "lower", "plain"),
        "fmax_mhz": ("MHz", "higher", "per_clock"),
        "critical_path_ps": ("ps", "lower", "per_clock"),
        "wns_ns": ("ns", "higher", "per_clock"),
        "whs_ns": ("ns", "higher", "per_clock"),
    },
}

# Vivado "Clock Summary" + "Intra Clock Table" for two clocks.
_TWO_CLOCK_REPORT = """\
Clock Summary
Clock  Waveform(ns)  Period(ns)  Frequency(MHz)
-----  ------------  ----------  --------------
clk    {0.000 5.000} 10.000      100.000
clk2   {0.000 2.500} 5.000       200.000

Intra Clock Table
Clock  WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)
-----  -------  -------  ---------------------  -------------------  -------  -------
clk    0.250    0.000    0                      1234                 0.100    0.000
clk2   -0.100   0.500    3                      200                  0.050    0.000
"""


def _synth_metrics() -> Any:
    """Synth metrics parsed from canned two-clock Yosys/STA output."""
    from booley.flows.synth.flow import _parse_synth_output

    marker = "STA_PERCLOCK: name={} period_ns={:.6f} wns_ns={:.6f} whs_ns={:.6f}\n"
    output = (
        "Chip area for top module '\\t': 52480.0\n"
        "Number of cells: 12345\n"
        + marker.format("clk_core", 2.0, 0.75, 0.1)
        + marker.format("clk_io", 5.0, -1.0, 0.2)
    )
    return _parse_synth_output(output, 1.0)


def _fpga_metrics() -> Any:
    """FPGA metrics built from a real routed report plus a two-clock timing table."""
    from booley.flows.fpga.backends.vivado import edam
    from booley.flows.fpga.flow import FpgaImplFlow

    text = (_FIXTURES / "vivado_routed_real_2025_2.rpt").read_text(encoding="utf-8")
    raw = edam.parse_fpga_reports(text + "\n" + _TWO_CLOCK_REPORT)
    return FpgaImplFlow._metrics_from_parsed_reports(raw, elapsed_s=1.0)


_SAMPLE_RUNS = {"synth": _synth_metrics, "fpga": _fpga_metrics}


@pytest.mark.parametrize("flow", ["synth", "fpga"])
def test_flow_declarations_expose_name_unit_direction_tier_kind(flow: str) -> None:
    from booley.flows.metric_declarations import CostTier
    from booley.measurement.flow_metrics import metric_declarations

    declarations = metric_declarations(flow)

    actual = {d.name: (d.unit, d.better.value, d.kind.value) for d in declarations}
    assert actual == _EXPECTED_DECLARATIONS[flow]
    assert len(actual) == len(declarations)  # no duplicate names
    assert {d.cost_tier for d in declarations} == {CostTier.HIGH}


def test_undeclared_flow_is_a_lookup_error() -> None:
    from booley.measurement.flow_metrics import metric_declarations

    with pytest.raises(LookupError, match="lint"):
        metric_declarations("lint")


def test_synth_run_returns_cell_count_and_per_clock_timing() -> None:
    metrics = _synth_metrics()

    values = metrics.flow_metrics()

    assert values.plain["cell_count"] == 12345 == metrics.qor_detail()["cells"]
    assert values.per_clock["fmax_mhz"] == pytest.approx(
        {"clk_core": 800.0, "clk_io": 166.667}, rel=1e-4
    )
    assert values.per_clock["wns_ns"] == pytest.approx({"clk_core": 0.75, "clk_io": -1.0})
    assert values.per_clock["whs_ns"] == pytest.approx({"clk_core": 0.1, "clk_io": 0.2})


def test_fpga_run_returns_lut_count_and_per_clock_timing() -> None:
    values = _fpga_metrics().flow_metrics()

    assert values.plain["lut_count"] == 18
    assert values.per_clock["fmax_mhz"] == pytest.approx(
        {"clk": 102.5641, "clk2": 196.0784}, rel=1e-4
    )
    assert values.per_clock["wns_ns"] == pytest.approx({"clk": 0.25, "clk2": -0.1})
    assert values.per_clock["whs_ns"] == pytest.approx({"clk": 0.1, "clk2": 0.05})


@pytest.mark.parametrize("flow", ["synth", "fpga"])
def test_flow_emits_exactly_its_declarations(flow: str) -> None:
    from booley.measurement.flow_metrics import flows_with_metric_declarations, metric_declarations

    # A Flow that declares metrics must be covered here, so a new one cannot go untested.
    assert set(_SAMPLE_RUNS) == set(flows_with_metric_declarations())

    values = _SAMPLE_RUNS[flow]().flow_metrics()

    emitted = set(values.plain) | set(values.per_clock)
    assert emitted == {d.name for d in metric_declarations(flow)}


def test_metric_values_rejects_undeclared_metric() -> None:
    from booley.flows.metric_declarations import UndeclaredMetricError, metric_values
    from booley.measurement.flow_metrics import metric_declarations

    with pytest.raises(UndeclaredMetricError, match="cells"):
        metric_values(metric_declarations("synth"), plain={"cells": 1}, per_clock={})


def test_metric_values_rejects_per_clock_name_declared_plain() -> None:
    from booley.flows.metric_declarations import UndeclaredMetricError, metric_values
    from booley.measurement.flow_metrics import metric_declarations

    with pytest.raises(UndeclaredMetricError, match="fmax_mhz"):
        metric_values(metric_declarations("synth"), plain={"fmax_mhz": 1.0}, per_clock={})


def test_metric_values_drops_unmeasured_values() -> None:
    from booley.evidence.timing import ClockTiming
    from booley.flows.metric_declarations import metric_values
    from booley.measurement.flow_metrics import metric_declarations

    values = metric_values(
        metric_declarations("synth"),
        plain={"area_um2": None, "cell_count": 7},
        per_clock={"clk": ClockTiming(clock="clk", wns_ns=0.5)},
    )

    assert values.plain == {"cell_count": 7}
    assert values.per_clock == {"wns_ns": {"clk": 0.5}}
