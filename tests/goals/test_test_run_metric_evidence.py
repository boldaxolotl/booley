"""Cycle Count and coverage Goals: whose metric counts, and whose outcomes (ADR 0072).

A metric verdict counts only when the change that carries it also carries the
Goal's frozen Test Runs that produced the metric. Any other change with a
matching run enters that run's outcome into the Goal's ledger and nothing
else: a failure is sticky, and a pass never borrows another run's metric.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from booley.criteria.templates import cycle_count_criterion_key
from booley.flows.frozen_test_runs import OUTCOMES_ONLY_DETAIL_KEY
from booley.flows.test_runs import build_test_run
from booley.goals.test_run_evidence import REASON_FAILED
from tests.goals.conftest import enter_goals, run_row
from tests.goals.test_test_run_evidence import _outcomes, _record, _status

PINNED = build_test_run("smoke", seed=7)
CYCLE_KEY = cycle_count_criterion_key("top", "smoke@7")
STRESS_TOML = b'[top]\ntests = ["smoke", "stress"]\ndefault_seed = 1\n'
STRESS = build_test_run("stress", seed=5)
OUTCOMES_ONLY = {OUTCOMES_ONLY_DETAIL_KEY: True}


@pytest.fixture
def cycle(layout: SimpleNamespace) -> SimpleNamespace:
    """A Cycle Count Goal on the pinned Test Run ``smoke@7`` (at most 900 cycles)."""
    goal = {
        "family": "CYCLE_COUNT",
        "target": "top",
        "test": "smoke@7",
        "thresholds": {"cycle_count_max": 900},
    }
    return SimpleNamespace(**vars(layout), record=enter_goals(layout, [goal]))


def _cycles(count: int) -> dict[str, Any]:
    return {"cycles": count, "cycle_observation": "observed", "test": "smoke"}


def test_cycle_count_verdict_of_a_run_that_is_not_the_frozen_run_does_not_count(
    cycle: SimpleNamespace,
) -> None:
    # The frozen run passes simulation but is over the threshold.
    _record(cycle, "run-1", [run_row(PINNED)], key=CYCLE_KEY, met=False, extra=_cycles(1500))
    assert _status(cycle, CYCLE_KEY).status == "unmet"
    # The default-seed run of the same Test is fast; it is not the Goal's Test Run.
    detail = _record(cycle, "run-2", [run_row("smoke")], key=CYCLE_KEY, extra=_cycles(100))
    assert detail["test_runs"] == [run_row(PINNED)]  # the over-threshold observation, restated
    assert detail["cycles"] == 1500
    assert _status(cycle, CYCLE_KEY).status == "unmet"


def test_cycle_count_goal_with_only_a_non_matching_run_is_unmet_and_says_so(
    cycle: SimpleNamespace,
) -> None:
    detail = _record(cycle, "run-1", [run_row("smoke")], key=CYCLE_KEY, extra=_cycles(100))
    assert detail["test_runs"] == []
    status = _status(cycle, CYCLE_KEY)
    assert status.status == "unmet"
    assert detail["goal_contract_violation"] == "Test Runs have not passed yet: smoke@7"


def test_outcomes_only_pass_keeps_a_measured_verdict_and_a_failure_unmeets_it(
    cycle: SimpleNamespace,
) -> None:
    _record(cycle, "run-1", [run_row(PINNED)], key=CYCLE_KEY, extra=_cycles(800))
    assert _status(cycle, CYCLE_KEY).status == "met"
    # A campaign that ran the frozen run without measuring its Cycle Count adds nothing.
    again = _record(
        cycle, "run-2", [run_row(PINNED)], key=CYCLE_KEY, met=False, extra=OUTCOMES_ONLY
    )
    assert again["cycles"] == 800 and OUTCOMES_ONLY_DETAIL_KEY not in again
    assert _status(cycle, CYCLE_KEY).status == "met"
    # A failure of the frozen run at the same contents is sticky, measured or not.
    failed = _record(
        cycle, "run-3", [run_row(PINNED, "fail")], key=CYCLE_KEY, met=False, extra=OUTCOMES_ONLY
    )
    assert _outcomes(failed) == {"smoke@7": "failed"}
    status = _status(cycle, CYCLE_KEY)
    assert status.status == "unmet" and status.reason.startswith(REASON_FAILED)
    _record(cycle, "run-4", [run_row(PINNED)], key=CYCLE_KEY, extra=_cycles(800))
    assert _status(cycle, CYCLE_KEY).status == "unmet"


@pytest.fixture
def coverage(layout: SimpleNamespace) -> SimpleNamespace:
    """A coverage Goal over the frozen Test Runs ``stress@5`` and ``smoke@1``."""
    goal = {
        "family": "COVERAGE",
        "target": "top",
        "tests": ["stress@5", "smoke"],
        "metrics": {"line": 80},
    }
    record = enter_goals(layout, [goal], tests_toml=STRESS_TOML)
    (key,) = record.test_run_resolutions
    return SimpleNamespace(**vars(layout), record=record, key=key)


def test_failure_from_an_ordinary_simulation_blocks_a_later_coverage_campaign(
    coverage: SimpleNamespace,
) -> None:
    key = coverage.key
    rows = [run_row(STRESS, "fail")]
    failed = _record(coverage, "run-1", rows, key=key, met=False, extra=OUTCOMES_ONLY)
    assert _outcomes(failed) == {"stress@5": "failed", "smoke@1": "missing"}
    # The coverage campaign runs exactly the frozen runs and reaches the floors.
    campaign = [run_row(STRESS), run_row("smoke")]
    detail = _record(coverage, "run-2", campaign, key=key, met=True)
    assert _outcomes(detail)["stress@5"] == "failed"
    status = _status(coverage, key)
    assert status.status == "unmet" and status.reason.startswith(REASON_FAILED)


def test_coverage_verdict_needs_exactly_the_frozen_runs_in_the_same_change(
    coverage: SimpleNamespace,
) -> None:
    key = coverage.key
    # A superset campaign passed both frozen runs but merged coverage over a third run.
    superset = [run_row(STRESS), run_row("smoke"), run_row(build_test_run("stress", seed=6))]
    detail = _record(coverage, "run-1", superset, key=key, met=True)
    assert set(_outcomes(detail).values()) == {"passed"}
    assert "did not measure" in detail["goal_contract_violation"]
    status = _status(coverage, key)
    assert status.status == "unmet" and "did not measure the Goal" in status.reason
    # A subset campaign measured something else too.
    _record(coverage, "run-2", [run_row(STRESS)], key=key, met=True)
    assert _status(coverage, key).status == "unmet"
    exact = _record(coverage, "run-3", [run_row(STRESS), run_row("smoke")], key=key, met=True)
    # The recorder takes the verdict; this bare detail has no coverage stamp to stay fresh by.
    assert "goal_contract_violation" not in exact
    assert _status(coverage, key).status == "stale"
