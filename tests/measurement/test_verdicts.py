"""Tests for ``booley.measurement.verdicts``: one verdict per reported metric (#3949)."""

from __future__ import annotations

import importlib
from typing import Any

from booley.flows.metric_declarations import FlowMetricValues
from booley.measurement.metrics_file import Bound, TableRules, TrackedMetric
from booley.measurement.run_document import Verdict


def _verdicts() -> Any:
    """Import the module lazily so the file collects (and fails in its body) on the base."""
    return importlib.import_module("booley.measurement.verdicts")


def _bound(
    key: str,
    metric: str,
    limit: str,
    value: float,
    prefix: str | None = None,
    test_run: str | None = None,
) -> Bound:
    return Bound(key, metric, limit, value, prefix, test_run, None)  # type: ignore[arg-type]


def _tracked(key: str, metric: str, prefix: str | None = None) -> TrackedMetric:
    return TrackedMetric(key, metric, prefix, None)


def _rules(
    flow: str, bounds: tuple[Bound, ...] = (), tracked: tuple[TrackedMetric, ...] = ()
) -> TableRules:
    return TableRules("t", "vlnv:x#t", flow, bounds, tracked)


def _values(
    plain: dict[str, float] | None = None,
    per_clock: dict[str, dict[str, float]] | None = None,
    per_test: dict[str, dict[str, float]] | None = None,
) -> FlowMetricValues:
    return FlowMetricValues(plain=plain or {}, per_clock=per_clock or {}, per_test=per_test or {})


def _flow_values(values: FlowMetricValues, clocks: tuple[str, ...] = ()) -> Any:
    return _verdicts().FlowValues(values=values, clocks=clocks)


def test_enforced_metric_is_met_at_its_bound_and_unmet_past_it() -> None:
    module = _verdicts()
    max_rules = _rules("synth", (_bound("cell_count_max", "cell_count", "max", 100),))
    min_rules = _rules("sim", (_bound("tests_total_min", "tests_total", "min", 5),))
    cases = [
        (max_rules, "cell_count", 100, Verdict.MET, 100),
        (max_rules, "cell_count", 101, Verdict.UNMET, 100),
        (min_rules, "tests_total", 5, Verdict.MET, 5),
        (min_rules, "tests_total", 4, Verdict.UNMET, 5),
    ]
    for rules, metric, value, verdict, bound in cases:
        (result,) = module.evaluate_table(rules, _flow_values(_values({metric: value})))
        assert result.verdict is verdict, (metric, value)
        assert result.value == value
        assert result.bound == bound
        if verdict is Verdict.UNMET:
            assert str(value) in result.detail and str(bound) in result.detail
        else:
            assert result.detail is None
    (above,) = module.evaluate_table(max_rules, _flow_values(_values({"cell_count": 101})))
    assert "above" in above.detail
    (below,) = module.evaluate_table(min_rules, _flow_values(_values({"tests_total": 4})))
    assert "below" in below.detail


def test_tracked_metric_is_tracked_and_a_missing_metric_is_error() -> None:
    module = _verdicts()
    rules = _rules(
        "synth",
        (_bound("cell_count_max", "cell_count", "max", 100),),
        (_tracked("area_um2", "area_um2"),),
    )
    bound_result, tracked_result = module.evaluate_table(
        rules, _flow_values(_values({"cell_count": 90, "area_um2": 12.5}))
    )
    assert bound_result.verdict is Verdict.MET
    assert tracked_result.verdict is Verdict.TRACKED
    assert tracked_result.bound is None
    assert tracked_result.value == 12.5

    # Tracked metric missing: error naming the metric; the table exits 2 although the bound is met.
    results = module.evaluate_table(rules, _flow_values(_values({"cell_count": 90})))
    assert results[0].verdict is Verdict.MET
    assert results[1].verdict is Verdict.ERROR
    assert "area_um2" in results[1].detail
    assert module.measure_exit_code(r.verdict for r in results) == 2

    # Enforced metric missing: error naming the metric.
    results = module.evaluate_table(rules, _flow_values(_values({"area_um2": 1.0})))
    assert results[0].verdict is Verdict.ERROR
    assert "cell_count" in results[0].detail
    assert results[0].value is None
    assert results[0].bound == 100


def test_flow_outcome_decides_a_table_without_values() -> None:
    module = _verdicts()
    rules = _rules(
        "sim",
        (_bound("tests_failed_max", "tests_failed", "max", 0),),
        (_tracked("tests_total", "tests_total"),),
    )
    failed = module.evaluate_table(rules, module.FlowFailed("verilator: not found"))
    not_started = module.evaluate_table(
        rules, module.FlowNotStarted("stopped after another table")
    )
    for results, verdict, text in (
        (failed, Verdict.ERROR, "verilator: not found"),
        (not_started, Verdict.NOT_EVALUATED, "stopped after another table"),
    ):
        assert [r.verdict for r in results] == [verdict, verdict]
        assert all(text in r.detail for r in results)
        assert all(r.value is None for r in results)
        assert results[0].bound == 0
        assert results[1].bound is None

    # A Target with values is judged on them even when a test failed.
    (failing, total) = module.evaluate_table(
        rules, _flow_values(_values({"tests_failed": 2, "tests_total": 7}))
    )
    assert failing.verdict is Verdict.UNMET
    assert total.verdict is Verdict.TRACKED


def test_clock_prefix_must_name_one_reported_clock() -> None:
    module = _verdicts()
    two = _flow_values(
        _values(per_clock={"fmax_mhz": {"clk_a": 200.0, "clk_b": 300.0}}), ("clk_a", "clk_b")
    )
    rules = _rules(
        "synth",
        (
            _bound("clk_x.fmax_mhz_min", "fmax_mhz", "min", 150, prefix="clk_x"),
            _bound("fmax_mhz_min", "fmax_mhz", "min", 150),
        ),
        (_tracked("fmax_mhz", "fmax_mhz"),),
    )
    for result in module.evaluate_table(rules, two):
        assert result.verdict is Verdict.ERROR, result.key
        assert "clk_a" in result.detail and "clk_b" in result.detail

    single_rules = _rules("synth", (_bound("fmax_mhz_min", "fmax_mhz", "min", 150),))
    for fmax, verdict in ((200.0, Verdict.MET), (100.0, Verdict.UNMET)):
        one = _flow_values(_values(per_clock={"fmax_mhz": {"clk": fmax}}), ("clk",))
        (result,) = module.evaluate_table(single_rules, one)
        assert result.verdict is verdict
        assert result.value == fmax

    no_value = _flow_values(_values(), ("clk",))
    (result,) = module.evaluate_table(single_rules, no_value)
    assert result.verdict is Verdict.ERROR
    assert "clk" in result.detail

    none_reported = _flow_values(_values())
    prefixed_rules = _rules(
        "synth", (_bound("clk.fmax_mhz_min", "fmax_mhz", "min", 150, prefix="clk"),)
    )
    for table in (single_rules, prefixed_rules):
        (result,) = module.evaluate_table(table, none_reported)
        assert result.verdict is Verdict.ERROR
        assert "no clock" in result.detail


def test_reported_clock_without_a_value_is_error() -> None:
    module = _verdicts()
    outcome = _flow_values(_values(per_clock={"fmax_mhz": {"clk_a": 200.0}}), ("clk_a", "clk_b"))
    rules = _rules(
        "synth",
        (
            _bound("fmax_mhz_min", "fmax_mhz", "min", 150),
            _bound("clk_b.fmax_mhz_min", "fmax_mhz", "min", 150, prefix="clk_b"),
            _bound("clk_a.fmax_mhz_min", "fmax_mhz", "min", 150, prefix="clk_a"),
        ),
    )
    unprefixed, missing, present = module.evaluate_table(rules, outcome)
    assert unprefixed.verdict is Verdict.ERROR
    assert "clk_a" in unprefixed.detail and "clk_b" in unprefixed.detail
    assert missing.verdict is Verdict.ERROR
    assert "clk_b" in missing.detail and "no fmax_mhz value" in missing.detail
    assert present.verdict is Verdict.MET


def test_per_test_metric_reads_its_test_run() -> None:
    module = _verdicts()
    rules = _rules(
        "sim",
        (_bound("smoke.cycle_count_max", "cycle_count", "max", 50, "smoke", "smoke@1"),),
    )
    (met,) = module.evaluate_table(
        rules, _flow_values(_values(per_test={"cycle_count": {"smoke@1": 40}}))
    )
    assert met.verdict is Verdict.MET
    (error,) = module.evaluate_table(
        rules, _flow_values(_values(per_test={"cycle_count": {"other@1": 40}}))
    )
    assert error.verdict is Verdict.ERROR
    assert "smoke@1" in error.detail


def test_exit_code_of_a_set_of_verdicts() -> None:
    module = _verdicts()
    cases = [
        ([Verdict.MET, Verdict.TRACKED, Verdict.NOT_EVALUATED], 0),
        ([Verdict.MET, Verdict.UNMET], 1),
        ([Verdict.UNMET, Verdict.ERROR], 2),
    ]
    for verdicts, code in cases:
        assert module.measure_exit_code(verdicts) == code, verdicts
