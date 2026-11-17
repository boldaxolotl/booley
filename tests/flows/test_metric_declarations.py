"""Typed metric declarations of the synth, FPGA, sim, coverage and lint Flows (#3496, #3497).

Each Flow declares the metrics it emits (name, unit, better direction, cost tier,
kind); a run returns typed values through ``flow_metrics()``. The modules under
test are imported inside the test bodies so a missing module fails the test, not
collection.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.flows.sim.simulation_record_test_support import _observation, _simulation_outcome
from tests.flows.sim.test_sim_flow_metrics import _coverage_outcome, _MixedCoverageExecution

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "vivado_reports"

# name -> (unit, better, kind), written out literally so a wrong direction, unit or
# kind in a declaration cannot hide behind the code that produced it.
_SIM_DECLARATIONS = {
    "tests_failed": ("tests", "lower", "plain"),
    "tests_total": ("tests", "higher", "plain"),
}
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
    "sim": {**_SIM_DECLARATIONS, "cycle_count": ("cycles", "lower", "per_test")},
    "coverage": {
        **_SIM_DECLARATIONS,
        "line_coverage": ("%", "higher", "plain"),
        "branch_coverage": ("%", "higher", "plain"),
        "expression_coverage": ("%", "higher", "plain"),
        "toggle_coverage": ("%", "higher", "plain"),
        "cover_property_coverage": ("%", "higher", "plain"),
    },
    "lint": {
        "warnings": ("warnings", "lower", "plain"),
        "errors": ("errors", "lower", "plain"),
    },
    "mutation": {
        "mutation_score": ("%", "higher", "plain"),
        "mutants_detected": ("Mutants", "higher", "plain"),
        "mutants_survived": ("Mutants", "lower", "plain"),
    },
}

# Cost tier of every declaration of a Flow: lint < sim < coverage <= synth, FPGA.
_EXPECTED_TIERS = {
    "synth": "HIGH",
    "fpga": "HIGH",
    "sim": "MEDIUM",
    "coverage": "HIGH",
    "lint": "LOW",
    "mutation": "HIGH",
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


_SYNTH_OUTPUT = (
    "Chip area for top module '\\t': 52480.0\n"
    "Number of cells: 12345\n"
    "STA_PERCLOCK: name=clk_core period_ns=2.000000 wns_ns=0.750000 whs_ns=0.100000\n"
    "STA_PERCLOCK: name=clk_io period_ns=5.000000 wns_ns=-1.000000 whs_ns=0.200000\n"
)


def _synth_metrics(output: str = _SYNTH_OUTPUT) -> Any:
    """Synth metrics parsed from canned Yosys/STA output."""
    # A public run needs Yosys; this parser sits between the tool output and the stored report.
    from booley.flows.synth import flow as synth_flow

    return synth_flow._parse_synth_output(output, 1.0)


def _synth_detail(metrics: Any) -> Any:
    """The synth report detail, read back from JSON the way a caller does."""
    # A public run needs Yosys; this writer builds the stored report from the parsed metrics.
    from booley.flows.synth import flow as synth_flow

    return json.loads(json.dumps(synth_flow._aggregate_detail(["asic"], {"asic": metrics})))


def _fpga_metrics() -> Any:
    """FPGA metrics built from a real routed report plus a two-clock timing table."""
    from booley.flows.fpga.backends.vivado import edam
    from booley.flows.fpga.flow import FpgaImplFlow

    text = (_FIXTURES / "vivado_routed_real_2025_2.rpt").read_text(encoding="utf-8")
    raw = edam.parse_fpga_reports(text + "\n" + _TWO_CLOCK_REPORT)
    # A public run needs Vivado; this turns parsed reports into the Flow's metrics.
    return FpgaImplFlow._metrics_from_parsed_reports(raw, elapsed_s=1.0)


def _sim_values(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Any:
    """Sim values of a passing run that reported a Cycle Count."""
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(tmp_path, [_observation("reset") | {"cycle_count": 40}])
    return simulation_flow_metrics(outcome)


def _coverage_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Coverage values of a real ``sim --coverage`` run."""
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _coverage_outcome(tmp_path, monkeypatch, _MixedCoverageExecution())
    return simulation_flow_metrics(outcome)


def _lint_values(_tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Any:
    """Lint values of one Target with a warning and an error."""
    from booley.flows.lint.flow import LintConfigResult, count_lint_errors, parse_warnings

    output = (
        "%Warning-UNUSEDSIGNAL: rtl/a.v:3:5: Signal is not used: 'x'\n"
        "%Error: rtl/a.v:4:1: rejected\n"
    )
    return LintConfigResult(
        target="lite",
        warnings=parse_warnings(output, "lite"),
        error_count=count_lint_errors(output, "verilator"),
    ).flow_metrics()


def _mutation_values() -> Any:
    """Mutation values of a run that scored four valid Mutants and detected three."""
    from booley.flows.mutation.metrics import mutation_metric_values

    return mutation_metric_values(3, 1)


# Flows with no report reader of their own: ``coverage`` has no Flow, the ``sim`` reader serves it.
_FLOWS_WITHOUT_REPORT_READER = {"coverage"}

# Every Flow that declares metrics maps to a run that returns its typed values.
_SAMPLE_RUNS: dict[str, Callable[[Path, pytest.MonkeyPatch], Any]] = {
    "synth": lambda _tmp, _mp: _synth_metrics().flow_metrics(),
    "fpga": lambda _tmp, _mp: _fpga_metrics().flow_metrics(),
    "sim": _sim_values,
    "coverage": _coverage_values,
    "lint": _lint_values,
    "mutation": lambda _tmp, _mp: _mutation_values(),
}


@pytest.mark.parametrize("flow", list(_EXPECTED_DECLARATIONS))
def test_flow_declarations_expose_name_unit_direction_tier_kind(flow: str) -> None:
    from booley.flows.metric_declarations import CostTier
    from booley.measurement.flow_metrics import metric_declarations

    declarations = metric_declarations(flow)

    actual = {d.name: (d.unit, d.better.value, d.kind.value) for d in declarations}
    assert actual == _EXPECTED_DECLARATIONS[flow]
    assert len(actual) == len(declarations)  # no duplicate names
    assert {d.cost_tier for d in declarations} == {CostTier[_EXPECTED_TIERS[flow]]}


def test_undeclared_flow_is_a_lookup_error() -> None:
    from booley.measurement.flow_metrics import metric_declarations

    # Elaboration Check is a mode of the sim Flow with no metrics.toml table.
    with pytest.raises(LookupError, match="elab"):
        metric_declarations("elab")


def test_synth_run_returns_cell_count_and_per_clock_timing() -> None:
    metrics = _synth_metrics()

    values = metrics.flow_metrics()

    assert values.plain["cell_count"] == 12345 == metrics.qor_detail()["cell_count"]
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


def test_synth_report_yields_declared_metric_values() -> None:
    from booley.measurement.flow_metrics import report_metric_values

    # ``jtag`` reports a period but no slack (NA), so no fmax can be derived for it.
    metrics = _synth_metrics(
        _SYNTH_OUTPUT + "STA_PERCLOCK: name=jtag period_ns=10.000000 wns_ns=NA whs_ns=NA\n"
    )
    metrics.area_kge = 65.8
    detail = _synth_detail(metrics)

    values = report_metric_values("synth", detail)

    assert set(values) == {"asic"}
    assert "cells" not in detail["asic"] and detail["asic"]["cell_count"] == 12345
    assert values["asic"].plain == {"area_um2": 52480.0, "area_kge": 65.8, "cell_count": 12345}
    assert values["asic"].clocks == ("clk_core", "clk_io", "jtag")
    assert values["asic"].per_clock["wns_ns"] == pytest.approx({"clk_core": 0.75, "clk_io": -1.0})


def test_synth_report_reads_cell_count() -> None:
    from booley.measurement.flow_metrics import report_metric_values

    stored: dict[str, object] = {"area_kge": 65.8, "cell_count": 12345, "per_clock": {}}
    values = report_metric_values("synth", {"targets": ["asic"], "asic": stored})
    assert values["asic"].plain == {"area_kge": 65.8, "cell_count": 12345}

    older = {"area_kge": 65.8, "cells": 12345, "per_clock": {}}
    values = report_metric_values("synth", {"targets": ["asic"], "asic": older})
    assert values["asic"].plain == {"area_kge": 65.8}


def test_fpga_report_yields_declared_metric_values() -> None:
    from booley.flows.fpga.flow import FpgaImplFlow
    from booley.flows.fpga.implementation_report import build_fpga_implementation_report
    from booley.flows.implementation_report import (
        ImplementationContext,
        build_implementation_aggregate,
    )
    from booley.measurement.flow_metrics import report_metric_values

    context = ImplementationContext(flow="fpga", target="board", eda_tool="vivado")
    report = build_fpga_implementation_report(context, _fpga_metrics(), None)
    aggregate = build_implementation_aggregate({"board": report})
    # A public run needs Vivado; this writes the stored report from the parsed metrics.
    detail = json.loads(
        json.dumps(
            FpgaImplFlow._aggregate_detail(["board"], {"board": _fpga_metrics()}, aggregate)
        )
    )

    values = report_metric_values("fpga", detail)

    assert set(values) == {"board"}
    assert values["board"].plain["lut_count"] == 18
    assert values["board"].per_clock["wns_ns"] == pytest.approx({"clk": 0.25, "clk2": -0.1})


def test_report_without_declared_metrics_or_targets_yields_none() -> None:
    from booley.measurement.flow_metrics import report_metric_values

    assert report_metric_values("lint", {"targets": ["asic"], "asic": {"cells": 3}}) == {}
    # A Target whose summary is missing or malformed is skipped, not guessed.
    assert report_metric_values("synth", {"targets": ["a", "b"], "a": "oops"}) == {}
    assert report_metric_values("fpga", {"implementation": {"results": {"a": {}}}}) == {}
    assert report_metric_values("fpga", {}) == {}


def test_every_declaring_flow_has_a_sample_run_and_a_report_reader() -> None:
    from booley.measurement import flow_metrics
    from booley.measurement.flow_metrics import flows_with_metric_declarations

    # A Flow that declares metrics must be covered here, so a new one cannot go untested.
    assert set(_SAMPLE_RUNS) == set(flows_with_metric_declarations())
    # ...and one that declares metrics must have a report reader, or its values read back as
    # ``{}``. No public call tells "no reader" from "reader found nothing".
    assert set(flow_metrics._READERS_BY_FLOW) | _FLOWS_WITHOUT_REPORT_READER == set(
        flows_with_metric_declarations()
    )


@pytest.mark.parametrize("flow", list(_EXPECTED_DECLARATIONS))
def test_flow_emits_exactly_its_declarations(
    flow: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement.flow_metrics import metric_declarations

    values = _SAMPLE_RUNS[flow](tmp_path, monkeypatch)

    emitted = set(values.plain) | set(values.per_clock) | set(values.per_test)
    assert emitted == {d.name for d in metric_declarations(flow)}


@pytest.mark.parametrize(
    ("flow", "plain", "per_clock", "per_test", "name"),
    [
        ("synth", {"cells": 1}, {}, {}, "cells"),
        # A per-clock name is not a plain metric.
        ("synth", {"fmax_mhz": 1.0}, {}, {}, "fmax_mhz"),
        ("sim", {}, {}, {"cycles": {"reset": 1}}, "cycles"),
        # A declared plain metric is not a per-test one.
        ("sim", {}, {}, {"tests_total": {"a": 1}}, "tests_total"),
    ],
)
def test_metric_values_rejects_undeclared_metric(flow, plain, per_clock, per_test, name) -> None:
    from booley.flows.metric_declarations import UndeclaredMetricError, metric_values
    from booley.measurement.flow_metrics import metric_declarations

    with pytest.raises(UndeclaredMetricError, match=name):
        metric_values(
            metric_declarations(flow), plain=plain, per_clock=per_clock, per_test=per_test
        )


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


def test_metric_values_carries_per_test_values_and_drops_unmeasured_ones() -> None:
    from booley.flows.metric_declarations import metric_values
    from booley.measurement.flow_metrics import metric_declarations

    values = metric_values(
        metric_declarations("sim"),
        plain={"tests_total": 2},
        per_test={"cycle_count": {"reset": 40, "smoke@7": None}},
    )

    assert values.plain == {"tests_total": 2}
    assert values.per_test == {"cycle_count": {"reset": 40}}
    assert values.per_clock == {}

    only_unmeasured = metric_values(
        metric_declarations("sim"), plain={}, per_test={"cycle_count": {"reset": None}}
    )
    assert only_unmeasured.per_test == {}
