"""Typed metrics of the simulation and coverage Flows (issue #3497).

A run reports ``tests_failed``/``tests_total``, a Cycle Count per Test Run and, for a
coverage run, the five coverage percentages. The module under test is imported inside
the test bodies so a missing module fails the test, not collection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import SimulationRunResult
from tests.flows.sim.simulation_record_test_support import _observation, _simulation_outcome
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution


def _row(test: str, **members: object) -> dict[str, object]:
    """One observation of *test*; a passing, assertion-clean, completed run by default."""
    return _observation(test) | members


# A timed-out Test Run as the serial executor records it (no verdict was observed).
_TIMED_OUT: dict[str, object] = {
    "execution": "timeout",
    "failure_class": "design",
    "functional": "not_observed",
    "assertions": "not_observed",
    "cycle_count": None,
}


def test_failing_and_timed_out_tests_count_as_failed(tmp_path: Path) -> None:
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(
        tmp_path,
        [
            _row("pass_test", cycle_count=120),
            _row("fail_test", functional="fail", failure_class="design"),
            _row("hang_test", **_TIMED_OUT),
        ],
        required=("pass_test", "fail_test", "hang_test"),
    )

    values = simulation_flow_metrics(outcome)

    # Exact equality: a plain run carries no coverage metric.
    assert values.plain == {"tests_failed": 2, "tests_total": 3}


def test_inconclusive_test_counts_as_failed_and_its_cycles_are_not_a_value(
    tmp_path: Path,
) -> None:
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(
        tmp_path,
        [
            _row("ok", cycle_count=10),
            _row("silent", functional="inconclusive", cycle_count=77),
            _row("broken", functional="fail", failure_class="design", cycle_count=55),
        ],
        required=("ok", "silent", "broken"),
    )

    values = simulation_flow_metrics(outcome)

    assert values.plain == {"tests_failed": 2, "tests_total": 3}
    assert values.per_test == {"cycle_count": {"ok": 10}}


def test_cycle_count_is_keyed_by_test_run_name(tmp_path: Path) -> None:
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(
        tmp_path,
        [_row("reset", cycle_count=40), _row("smoke", cycle_count=90)],
        required=("reset", "smoke"),
        seeds={"smoke": 7},
    )

    values = simulation_flow_metrics(outcome)

    assert values.per_test == {"cycle_count": {"reset": 40, "smoke@7": 90}}


def test_test_run_without_a_cycle_count_gives_no_value(tmp_path: Path) -> None:
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(tmp_path, [_row("reset"), _row("count", cycle_count=3)])

    values = simulation_flow_metrics(outcome)

    assert values.per_test == {"cycle_count": {"count": 3}}


def test_incomplete_simulation_record_produces_no_metrics(tmp_path: Path) -> None:
    from booley.flows.metric_declarations import MetricsUnavailableError
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(tmp_path, [_row("reset")], complete=False)

    with pytest.raises(MetricsUnavailableError, match="did not finish"):
        simulation_flow_metrics(outcome)


def test_infrastructure_failure_produces_no_metrics(tmp_path: Path) -> None:
    from booley.flows.metric_declarations import MetricsUnavailableError
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(
        tmp_path,
        [
            _row("reset"),
            _row(
                "count",
                execution="setup_error",
                failure_class="infrastructure",
                functional="not_observed",
                detail={"reason": "build crashed"},
            ),
        ],
    )

    with pytest.raises(MetricsUnavailableError, match="count") as raised:
        simulation_flow_metrics(outcome)
    assert "(build crashed)" in str(raised.value)  # the first errored Test Run's reason


def test_cocotb_import_failure_produces_no_metrics_and_names_the_module(
    tmp_path: Path,
) -> None:
    from booley.flows.metric_declarations import MetricsUnavailableError
    from booley.flows.sim.backends.cocotb_results import IMPORT_FAILURE_PREFIX
    from booley.flows.sim.metrics import simulation_flow_metrics

    reason = f"{IMPORT_FAILURE_PREFIX} 'tb_top': ModuleNotFoundError: No module named 'pyuvm'"
    outcome = _simulation_outcome(
        tmp_path,
        [_row("silent", functional="inconclusive", detail={"reason": reason})],
        required=("silent",),
    )

    with pytest.raises(MetricsUnavailableError, match=r"tb_top.*pyuvm"):
        simulation_flow_metrics(outcome)


# ---- coverage runs ----------------------------------------------------------------------


def _coverage_record(metric: str, line: int, hits: int) -> str:
    """One native Verilator coverage record of *metric* at *line* of ``rtl/counter.sv``."""
    identity = (
        f"\x01f\x02rtl/counter.sv\x01l\x02{line}\x01n\x021"
        f"\x01h\x02TOP.counter\x01t\x02{metric}\x01o\x02point{line}"
    )
    return f"C '{identity}' {hits}\n"


class _MixedCoverageExecution(NativeExecution):
    """Hits chosen so every metric has a different, known percentage.

    line 1/2, branch 3/4, expression 1/1, toggle 2/3 and user (cover property) 1/5. A
    point's hits scale with *hits* so the merged database equals the sum of the runs.
    """

    def payload(self, hits: int) -> str:
        records = (
            ("line", (1, 0)),
            ("branch", (1, 1, 1, 0)),
            ("expr", (1,)),
            ("toggle", (1, 1, 0)),
            ("user", (1, 0, 0, 0, 0)),
        )
        body = ""
        line = 1
        for metric, point_hits in records:
            for point_hit in point_hits:
                body += _coverage_record(metric, line, point_hit * hits)
                line += 1
        return "# SystemC::Coverage-3\n" + body


class _TimedOutCoverageExecution(_MixedCoverageExecution):
    """The ``wrap`` Test Run never finishes; ``reset`` passes."""

    def run(self, request: Any) -> SimulationRunResult:
        result = super().run(request)
        if request.test.name == "wrap":
            return SimulationRunResult("timeout", termination="timeout")
        return result


def _coverage_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execution: Any) -> Any:
    """Run ``sim --coverage`` on a two-test Target and return its Campaign outcome."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    flow = SimulateFlow(coverage_execution=lambda *_args: execution)
    flow.execute(
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    (outcome,) = flow._simulation_record_outcomes
    return outcome


def test_coverage_run_returns_coverage_percents_with_simulation_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _coverage_outcome(tmp_path, monkeypatch, _MixedCoverageExecution())

    values = simulation_flow_metrics(outcome)

    assert values.plain == {
        "tests_failed": 0,
        "tests_total": 2,
        "line_coverage": 50.0,
        "branch_coverage": 75.0,
        "expression_coverage": 100.0,
        # Exact fraction: a value rounded to two decimals (66.67) would fail this.
        "toggle_coverage": 200 / 3,
        "cover_property_coverage": 20.0,
    }
    assert values.per_test == {}  # a coverage run observes no Cycle Count


def test_coverage_run_with_timed_out_test_keeps_simulation_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _coverage_outcome(tmp_path, monkeypatch, _TimedOutCoverageExecution())

    values = simulation_flow_metrics(outcome)

    # Collection fails after a timeout: no coverage value, but the failing test is a value.
    assert values.plain == {"tests_failed": 1, "tests_total": 2}


def test_boolean_cycle_count_gives_no_value(tmp_path: Path) -> None:
    from booley.flows.sim.metrics import simulation_flow_metrics

    outcome = _simulation_outcome(
        tmp_path, [_row("flag", cycle_count=True), _row("count", cycle_count=3)]
    )

    assert simulation_flow_metrics(outcome).per_test == {"cycle_count": {"count": 3}}


def test_unreadable_coverage_campaign_produces_no_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.metric_declarations import MetricsUnavailableError
    from booley.flows.sim.metrics import coverage_campaign_path, simulation_flow_metrics

    outcome = _coverage_outcome(tmp_path, monkeypatch, _MixedCoverageExecution())
    coverage_campaign_path(outcome).write_bytes(b"{ not json")

    with pytest.raises(MetricsUnavailableError, match="Coverage Campaign could not be read"):
        simulation_flow_metrics(outcome)
