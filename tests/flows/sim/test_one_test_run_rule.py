"""Rules about Test Runs that have exactly one implementation (ADR 0072)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.campaign.test_run_binding import observation_test_runs
from booley.flows.sim.campaign.test_run_report import (
    TEST_RUN_GRADES,
    _observed_run,
    test_run_grade,
    test_run_row,
)
from booley.flows.sim.execution.engine import _observed_test_run
from booley.flows.test_runs import (
    build_test_run,
    discovered_test_run,
    test_run_document,
)
from booley.targets.domain import TargetHandle

_HANDLE = cast(TargetHandle, SimpleNamespace(selector="sim"))
_TARGET = {"selector": "sim"}


def _item(unnamed):
    """A work item whose only Test Run is the unnamed invocation *unnamed*."""
    return {"work_item_id": "w", "target": _TARGET, "test_runs": (test_run_document(unnamed),)}


@pytest.mark.parametrize("seed", [None, 9])
@pytest.mark.parametrize("test", ["found_a", "default", "has space"])
def test_results_facts_and_reports_name_a_discovered_test_the_same_way(test, seed):
    """One rule, three readers: the engine, the facts binding and the report rows."""
    unnamed = build_test_run(None, seed=seed)
    expected = discovered_test_run(test, unnamed, target="sim")

    engine = _observed_test_run(test, _HANDLE, (unnamed,))
    item = _item(unnamed)
    ((bound_name, bound_fingerprint),) = observation_test_runs(item, ({"test": test},)) or ()
    observation = {"work_item_id": "w", "target": _TARGET, "test": test, "test_run": bound_name}
    report = _observed_run(observation, {("w", unnamed.name): unnamed, ("w", ""): unnamed})

    assert engine == report == expected
    assert (bound_name, bound_fingerprint) == (expected.name, expected.fingerprint)


def test_a_discovered_test_runs_at_the_unnamed_invocations_seed():
    unnamed = build_test_run(None, seed=9)
    assert discovered_test_run("found_a", unnamed, target="sim").name == "found_a@9"
    assert discovered_test_run("found_a", build_test_run(None), target="sim").name == "found_a"
    # A name outside the Test name rules stays under the unnamed invocation.
    assert discovered_test_run("default", unnamed, target="sim") is unnamed


@pytest.mark.parametrize("seed", [None, 9])
@pytest.mark.parametrize("name", ["", "sim"])
def test_results_facts_and_reports_bind_the_unnamed_invocations_own_result_to_it(name, seed):
    """A result named after the Target (or nothing) is the unnamed run, not a discovered Test."""
    unnamed = build_test_run(None, seed=seed)

    engine = _observed_test_run(name, _HANDLE, (unnamed,))
    ((bound_name, bound_fingerprint),) = observation_test_runs(_item(unnamed), ({"test": name},))
    # "test_run" names no recorded run, so the report must bind through the Test name.
    observation = {"work_item_id": "w", "target": _TARGET, "test": name, "test_run": "unrecorded"}
    report = _observed_run(observation, {("w", unnamed.name): unnamed, ("w", ""): unnamed})

    assert engine is unnamed and report is unnamed
    assert (bound_name, bound_fingerprint) == (unnamed.name, unnamed.fingerprint)


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
