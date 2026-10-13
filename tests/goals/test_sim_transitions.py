"""Simulation transitions are reports of exact Test Runs, never Goal decisions."""

from dataclasses import replace

import pytest

from booley.flows.test_runs import build_test_run
from booley.goals.sim_transitions import transition_for, transition_summary

RUN = build_test_run("stress", seed=7)


@pytest.mark.parametrize(
    ("baseline", "current", "seen", "expected"),
    [
        ("pass", "passed", False, "pass -> pass"),
        ("fail", "passed", False, "fail -> pass"),
        ("pass", "failed", False, "pass -> fail"),
        ("fail", "failed", False, "fail -> fail"),
        ("new", "passed", True, "new (seen failing) -> pass"),
        ("new", "passed", False, "new (never seen failing) -> pass"),
        ("new", "failed", False, "new -> fail"),
    ],
)
def test_each_transition_is_reported(baseline, current, seen, expected):
    row = transition_for(RUN, baseline, current, seen_failing=seen)
    assert row["transition"] == expected
    summary = transition_summary([row])
    assert expected in summary
    assert (RUN.name in summary) is (expected != "pass -> pass")


def test_unavailable_comparison_keeps_its_reason():
    row = transition_for(RUN, "unavailable", "passed", reason="build error")
    assert "build error" in transition_summary([row])
    assert "fail -> pass" not in transition_summary([row])


def test_every_exception_is_named_without_truncation():
    rows = [transition_for(replace(RUN, test=f"test_{n}"), "new", "passed") for n in range(80)]
    summary = transition_summary(rows)
    assert all(row["name"] in summary for row in rows)
