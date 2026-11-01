"""Cycle Count and coverage Goals: whose metric counts, and whose outcomes (ADR 0072).

A metric verdict counts only when the change that carries it also carries the
Goal's frozen Test Runs that produced the metric. For a Cycle Count Goal any
other change with a matching run enters that run's outcome into the Goal's
ledger and nothing else: a failure is sticky, and a pass never borrows another
run's metric. A coverage Goal is judged by its coverage floors alone: whether
its Test Runs passed never decides it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import cycle_count_criterion_key
from booley.flows.frozen_test_runs import OUTCOMES_ONLY_DETAIL_KEY
from booley.flows.test_runs import build_test_run
from booley.goals.test_run_evidence import REASON_FAILED, REASON_INCOMPLETE
from tests.goals.conftest import enter_goals, run_row
from tests.goals.test_test_run_evidence import (
    _contributors,
    _edit_rtl,
    _evidence_file,
    _outcomes,
    _record,
    _replayed,
    _shown,
    _state_file,
    _status,
)

PINNED = build_test_run("smoke", seed=7)
CYCLE_KEY = cycle_count_criterion_key("top", "smoke@7")
STRESS_TOML = b'[top]\ntests = ["smoke", "stress"]\ndefault_seed = 1\n'
STRESS = build_test_run("stress", seed=5)
OUTCOMES_ONLY = {OUTCOMES_ONLY_DETAIL_KEY: True}

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)


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


@pytest.mark.timeout(150)
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
    _record(
        cycle, "run-3", [run_row(PINNED, "fail")], key=CYCLE_KEY, met=False, extra=OUTCOMES_ONLY
    )
    assert _outcomes(cycle, CYCLE_KEY) == {"smoke@7": "failed"}
    status = _status(cycle, CYCLE_KEY)
    assert status.status == "unmet" and status.reason.startswith(REASON_FAILED)
    _record(cycle, "run-4", [run_row(PINNED)], key=CYCLE_KEY, extra=_cycles(800))
    assert _status(cycle, CYCLE_KEY).status == "unmet"


def _ever_failed(mode: SimpleNamespace, key: str) -> tuple[bool, bool]:
    """``ever_failed`` of Criterion *key* in the live state and as replayed from the ledger."""
    live = DevelopmentState.load(_state_file(mode)).criteria[key].ever_failed
    return live, _replayed(mode, key).ever_failed


def test_a_cycle_count_goal_recorded_not_measured_never_latches_ever_failed(
    cycle: SimpleNamespace,
) -> None:
    detail = _record(
        cycle, "run-1", [run_row(PINNED)], key=CYCLE_KEY, met=False, extra=OUTCOMES_ONLY
    )
    assert detail["goal_contract_violation"].startswith("this run did not measure the Goal")
    assert _status(cycle, CYCLE_KEY).status == "unmet"
    assert _ever_failed(cycle, CYCLE_KEY) == (False, False)


def test_a_repeated_unmeasured_cycle_count_run_stays_not_measured(
    cycle: SimpleNamespace,
) -> None:
    for run_id in ("run-1", "run-2"):
        _record(cycle, run_id, [run_row(PINNED)], key=CYCLE_KEY, met=False, extra=OUTCOMES_ONLY)
    # The second run adds nothing but a non-measurement: it is not a restated outcome.
    for sequence in (1, 2):
        row = json.loads(_evidence_file(cycle, sequence).read_text())
        assert row["reason"] == "goal-test-run-metric-missing"
    assert _ever_failed(cycle, CYCLE_KEY) == (False, False)


def test_a_measured_cycle_count_miss_latches_ever_failed(cycle: SimpleNamespace) -> None:
    _record(cycle, "run-1", [run_row(PINNED)], key=CYCLE_KEY, met=False, extra=_cycles(1500))
    assert _ever_failed(cycle, CYCLE_KEY) == (True, True)


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


def test_failure_from_an_ordinary_simulation_does_not_block_a_later_coverage_campaign(
    coverage: SimpleNamespace,
) -> None:
    key = coverage.key
    rows = [run_row(STRESS, "fail")]
    _record(coverage, "run-1", rows, key=key, met=False, extra=OUTCOMES_ONLY)
    assert _outcomes(coverage, coverage.key) == {"stress@5": "failed", "smoke@1": "missing"}
    # A failing or missing run is never why a coverage Goal is unmet: it is not measured yet.
    status = _status(coverage, key)
    assert status.status == "unmet" and "did not measure the Goal" in status.reason
    assert REASON_FAILED not in status.reason and REASON_INCOMPLETE not in status.reason
    assert _shown(status) == {"stress@5": "failed", "smoke@1": "missing"}
    # The coverage campaign runs exactly the frozen runs and reaches the floors.
    campaign = [run_row(STRESS), run_row("smoke")]
    detail = _record(coverage, "run-2", campaign, key=key, met=True)
    # Its ledger holds its own runs' outcomes; the earlier failure is not folded in.
    assert _outcomes(coverage, coverage.key) == {"stress@5": "passed", "smoke@1": "passed"}
    assert _contributors(coverage, key) == []
    assert "goal_contract_violation" not in detail
    assert _status(coverage, key).status == "stale"  # this bare detail has no coverage stamp


def test_coverage_floors_decide_a_campaign_whose_test_runs_failed(
    coverage: SimpleNamespace,
) -> None:
    key = coverage.key
    campaign = [run_row(STRESS, "fail"), run_row("smoke")]
    met = _record(coverage, "run-1", campaign, key=key, met=True)
    assert "goal_contract_violation" not in met
    assert _outcomes(coverage, coverage.key) == {"stress@5": "failed", "smoke@1": "passed"}
    status = _status(coverage, key)
    assert status.status == "stale" and "goal-test-run-" not in status.reason
    # The failing run is still shown, with the command that runs it again.
    assert status.test_runs is not None
    assert [run.name for run in status.test_runs.failed] == ["stress@5"]
    # Floors the producer found unmet keep the Goal unmet, whatever the runs did.
    unmet = _record(coverage, "run-2", [run_row(STRESS), run_row("smoke")], key=key, met=False)
    assert "goal_contract_violation" not in unmet
    assert _status(coverage, key).status == "unmet"


@pytest.mark.parametrize("grade", ["pass", "fail"])
def test_run_that_does_not_measure_coverage_restates_the_measurement(
    coverage: SimpleNamespace, grade: str
) -> None:
    key = coverage.key
    campaign = [run_row(STRESS), run_row("smoke")]
    _record(coverage, "run-1", campaign, key=key, met=True, extra={"evaluation": "kept"})
    # An ordinary simulation of one frozen run, passing or failing, at the same contents.
    again = _record(
        coverage, "run-2", [run_row(STRESS, grade)], key=key, met=False, extra=OUTCOMES_ONLY
    )
    assert again["evaluation"] == "kept" and OUTCOMES_ONLY_DETAIL_KEY not in again
    assert _outcomes(coverage, coverage.key) == {"stress@5": "passed", "smoke@1": "passed"}
    state = DevelopmentState.load(_state_file(coverage))
    assert state.criteria[key].met is True
    # Status recomputes the ledger and finds nothing outdated, failed or incomplete.
    assert "goal-test-run-" not in _status(coverage, key).reason
    # At other file contents nothing is restated: the Goal is not measured there.
    _edit_rtl(coverage, "module top; wire changed; endmodule\n")
    other = _record(
        coverage, "run-3", [run_row(STRESS, grade)], key=key, met=False, extra=OUTCOMES_ONLY
    )
    assert "evaluation" not in other and "did not measure" in other["goal_contract_violation"]


def test_run_that_does_not_measure_coverage_restates_nothing_before_a_measurement(
    coverage: SimpleNamespace,
) -> None:
    key = coverage.key
    first = [run_row(STRESS, "fail")]
    _record(coverage, "run-1", first, key=key, met=False, extra=OUTCOMES_ONLY)
    # No campaign measured the Goal yet, so a later ordinary run shows its own outcomes.
    later = _record(coverage, "run-2", [run_row("smoke")], key=key, met=False, extra=OUTCOMES_ONLY)
    assert _outcomes(coverage, coverage.key) == {"stress@5": "missing", "smoke@1": "passed"}
    assert "did not measure" in later["goal_contract_violation"]
    status = _status(coverage, key)
    assert status.status == "unmet" and "did not measure the Goal" in status.reason
    assert _shown(status) == {"stress@5": "missing", "smoke@1": "passed"}


def test_run_that_does_not_measure_coverage_never_restates_evidence_without_test_runs(
    coverage: SimpleNamespace,
) -> None:
    """An older Booley's met observation is no measurement: it must not be recorded again."""
    key = coverage.key
    campaign = [run_row(STRESS), run_row("smoke")]
    detail = _record(coverage, "run-1", campaign, key=key, met=True)
    # Rewrite the observation and its projection as the previous version stored them.
    old = {
        name: value
        for name, value in detail.items()
        if name not in {"test_run_ledger", "test_runs"}
    }
    evidence, path = _evidence_file(coverage, 1), _state_file(coverage)
    row = json.loads(evidence.read_text(encoding="utf-8"))
    row["detail"] = old
    evidence.write_text(json.dumps(row), encoding="utf-8")
    state = json.loads(path.read_text(encoding="utf-8"))
    state["criteria"][key]["detail"] = old
    path.write_text(json.dumps(state), encoding="utf-8")
    # An ordinary simulation at the same contents records itself, and measures nothing.
    later = _record(coverage, "run-2", [run_row("smoke")], key=key, met=False, extra=OUTCOMES_ONLY)
    assert "did not measure" in later["goal_contract_violation"]
    assert DevelopmentState.load(path).criteria[key].met is False
    assert _status(coverage, key).status == "unmet"


def test_coverage_verdict_needs_exactly_the_frozen_runs_in_the_same_change(
    coverage: SimpleNamespace,
) -> None:
    key = coverage.key
    # A superset campaign passed both frozen runs but merged coverage over a third run.
    superset = [run_row(STRESS), run_row("smoke"), run_row(build_test_run("stress", seed=6))]
    detail = _record(coverage, "run-1", superset, key=key, met=True)
    assert set(_outcomes(coverage, coverage.key).values()) == {"passed"}
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
