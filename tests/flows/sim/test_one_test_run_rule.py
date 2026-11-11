"""Rules about Test Runs that have exactly one implementation (ADR 0072)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.flows.sim.campaign.test_run_binding import observation_test_runs
from booley.flows.sim.campaign.test_run_report import (
    TEST_RUN_GRADES,
    _observed_run,
    test_run_grade,
    test_run_row,
)
from booley.flows.test_runs import (
    build_test_run,
    discovered_test_run,
    test_run_document,
)


@pytest.mark.parametrize("seed", [None, 9])
@pytest.mark.parametrize("test", ["found_a", "default", "has space"])
def test_results_facts_and_reports_name_a_discovered_test_the_same_way(test, seed):
    """One rule, two readers: the facts binding and the report rows."""
    unnamed = build_test_run(None, seed=seed)
    expected = discovered_test_run(test, unnamed)

    item = {"work_item_id": "w", "test_runs": (test_run_document(unnamed),)}
    ((bound_name, bound_fingerprint),) = observation_test_runs(item, ({"test": test},)) or ()
    observation = {"work_item_id": "w", "test": test, "test_run": bound_name}
    report = _observed_run(observation, {("w", unnamed.name): unnamed, ("w", ""): unnamed})

    assert report == expected
    assert (bound_name, bound_fingerprint) == (expected.name, expected.fingerprint)


def test_a_discovered_test_runs_at_the_unnamed_invocations_seed():
    unnamed = build_test_run(None, seed=9)
    assert discovered_test_run("found_a", unnamed).name == "found_a@9"
    assert discovered_test_run("found_a", build_test_run(None)).name == "found_a"
    # A name outside the Test name rules stays under the unnamed invocation.
    assert discovered_test_run("default", unnamed) is unnamed


@pytest.mark.parametrize(
    ("passed", "infrastructure", "inconclusive", "grade"),
    [
        (True, False, False, "pass"),
        (True, True, True, "pass"),
        (False, True, False, "error"),
        (False, True, True, "error"),
        (False, False, True, "inconclusive"),
        (False, False, False, "fail"),
    ],
)
def test_the_one_grade_table(passed, infrastructure, inconclusive, grade):
    assert (
        test_run_grade(passed=passed, infrastructure=infrastructure, inconclusive=inconclusive)
        == grade
    )
    assert grade in TEST_RUN_GRADES


def test_coverage_runs_are_graded_by_the_campaign_table():
    """An infrastructure failure is an error on the coverage path, even when inconclusive."""
    from booley.flows.sim.coverage_acceptance import coverage_run_grade

    coverage = SimpleNamespace(
        simulation_verdict="inconclusive", attributes={"failure_kind": "infrastructure"}
    )
    assert coverage_run_grade(coverage) == "error"  # type: ignore[arg-type]


def test_a_row_is_the_test_run_record_plus_its_grade():
    run = build_test_run("stress", variant="soak", seed=7, args=("+iters=9",))
    assert test_run_row(run, "fail") == {**test_run_document(run), "grade": "fail"}
