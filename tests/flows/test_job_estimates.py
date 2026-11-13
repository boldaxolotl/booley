"""Phase estimates from the Resource History: own, borrowed and unknown (#3237).

The module under test is imported inside each test body (through ``_estimates``)
so a missing module fails the test itself, not its collection.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


def _estimates() -> ModuleType:
    return importlib.import_module("booley.flows.job_estimates")


def _record(
    target: str,
    phase: str,
    *,
    memory: float | None = None,
    wall: float | None = None,
    test_run: str = "",
    flags: tuple[str, ...] = (),
    outcome: str = "exit_zero",
) -> dict[str, Any]:
    """A history record with only the fields the estimator reads."""
    return {
        "key": {"target": target, "phase": phase, "flags": list(flags), "test_run": test_run},
        "peak_memory_mb": memory,
        "wall_s": wall,
        "outcome": outcome,
    }


def _sim(target: str, *tests: str, flags: tuple[str, ...] = ()) -> Any:
    return _estimates().JobRequest("sim", target, test_runs=tests, flags=flags)


def _phase(job: Any, phase: str, test_run: str = "") -> Any:
    """The estimate of one phase of *job*."""
    (match,) = [
        item for item in job.phases if (item.key.phase, item.key.test_run) == (phase, test_run)
    ]
    return match


def test_memory_estimate_is_largest_record_times_margin() -> None:
    records = [_record("t", "sim_run", memory=mb, test_run="a") for mb in (2048, 2560, 6656)]

    job = _estimates().ResourceEstimator(records).estimate([_sim("t", "a")])

    estimate = _phase(job, "sim_run", "a").memory_mb
    # 6.5 GiB x 1.25 = 8.125 GiB.
    assert estimate.value == 8320.0
    assert estimate.source is _estimates().Source.OWN


def test_oom_killed_record_only_raises_the_estimate() -> None:
    for oom_peak, expected in ((8192, 10240.0), (1024, 8320.0)):
        records = [_record("t", "sim_run", memory=mb, test_run="a") for mb in (2048, 2560, 6656)]
        records.append(_record("t", "sim_run", memory=oom_peak, test_run="a", outcome="oom_kill"))

        job = _estimates().ResourceEstimator(records).estimate([_sim("t", "a")])

        assert _phase(job, "sim_run", "a").memory_mb.value == expected, oom_peak


def _borrow_records() -> list[dict[str, Any]]:
    return [
        # Another Test Run and other flags of the same Target and phase.
        _record("t1", "sim_run", memory=4000, test_run="old", flags=("trace",)),
        _record("t1", "sim_run", memory=1000, test_run="small"),
        # Another Target: the Project's largest, never the Target's.
        _record("t2", "sim_run", memory=9000, test_run="x"),
        # Another phase of the same Target: never borrowed for sim_run.
        _record("t1", "sim_build", memory=20000),
    ]


def test_missing_key_borrows_from_target_then_project() -> None:
    estimates = _estimates()
    estimator = estimates.ResourceEstimator(_borrow_records())

    known_target = estimator.estimate([_sim("t1", "new", "small")])
    borrowed = _phase(known_target, "sim_run", "new").memory_mb
    assert (borrowed.value, borrowed.source) == (5000.0, estimates.Source.TARGET)
    own = _phase(known_target, "sim_run", "small").memory_mb
    assert (own.value, own.source) == (1250.0, estimates.Source.OWN), "own beats the Target"
    assert _phase(known_target, "sim_build").memory_mb.source is estimates.Source.OWN

    new_target = estimator.estimate([_sim("t3", "new")])
    run = _phase(new_target, "sim_run", "new").memory_mb
    assert (run.value, run.source) == (11250.0, estimates.Source.PROJECT)
    build = _phase(new_target, "sim_build").memory_mb
    assert (build.value, build.source) == (25000.0, estimates.Source.PROJECT)

    lint = estimator.estimate([estimates.JobRequest("lint", "t1")])
    unknown = _phase(lint, "lint").memory_mb
    assert (unknown.value, unknown.source) == (None, estimates.Source.UNKNOWN)


def test_job_reservation_and_budget_fit() -> None:
    estimates = _estimates()
    estimator = estimates.ResourceEstimator(_borrow_records())
    fit = estimates.Fit

    # Two Targets in one Job: the reservation is the largest phase over both.
    job = estimator.estimate([_sim("t1", "small"), _sim("t2", "x")])
    assert job.reservation_mb == 25000.0
    assert job.fit(30000) is fit.FITS
    assert job.fit(25000) is fit.FITS
    assert job.fit(24999) is fit.OVER_BUDGET

    # A phase without any estimate: the Job runs alone, unless already too large.
    unknown = estimator.estimate([_sim("t1", "small"), estimates.JobRequest("lint", "t1")])
    assert unknown.reservation_mb == 25000.0
    assert unknown.fit(30000) is fit.RUN_ALONE
    assert unknown.fit(100) is fit.OVER_BUDGET

    nothing = estimator.estimate([estimates.JobRequest("lint", "t1")])
    assert nothing.reservation_mb is None
    assert nothing.fit(30000) is fit.RUN_ALONE


def test_wall_clock_follows_the_same_rules() -> None:
    estimates = _estimates()
    records = [
        _record("t1", "sim_run", wall=w, test_run="a", outcome=o)
        for w, o in ((4.0, "exit_zero"), (20.0, "timeout"), (10.0, "exit_zero"))
    ]
    records += [
        # Memory is known but wall-clock is not: wall-clock borrows alone.
        _record("t1", "sim_run", memory=7000, test_run="blank"),
        _record("t2", "sim_run", wall=2.0, test_run="a"),
    ]
    estimator = estimates.ResourceEstimator(records)

    # The timed-out 20 s is a lower bound: it raises the estimate (25 s).
    for target, test, source in (
        ("t1", "a", estimates.Source.OWN),
        ("t1", "new", estimates.Source.TARGET),
        ("t3", "new", estimates.Source.PROJECT),
    ):
        wall = _phase(estimator.estimate([_sim(target, test)]), "sim_run", test).wall_s
        assert (wall.value, wall.source) == (25.0, source), (target, test)


def test_wall_clock_unknown_and_independent_of_memory() -> None:
    estimates = _estimates()
    records = [_record("t1", "sim_run", memory=7000, test_run="blank")]

    job = estimates.ResourceEstimator(records).estimate([_sim("t1", "blank")])

    run = _phase(job, "sim_run", "blank")
    assert (run.memory_mb.value, run.memory_mb.source) == (8750.0, estimates.Source.OWN)
    assert (run.wall_s.value, run.wall_s.source) == (None, estimates.Source.UNKNOWN)


def test_own_wall_clock_needs_own_history_for_every_phase() -> None:
    estimates = _estimates()
    records = [
        _record("t1", "sim_build", memory=100, wall=8.0),
        _record("t1", "sim_run", memory=100, wall=12.0, test_run="a"),
        _record("t2", "sim_run", memory=100, wall=2.0, test_run="b"),
    ]
    estimator = estimates.ResourceEstimator(records)

    own = estimator.estimate([_sim("t1", "a")])
    borrowed = estimator.estimate([_sim("t1", "a"), _sim("t1", "new")])
    unknown = estimator.estimate([_sim("t3", "a")])

    # Both phases have their own history: (8 + 12) x 1.25.
    assert own.own_wall_s == 25.0
    assert borrowed.own_wall_s is None  # the second Test Run borrows its wall-clock
    assert unknown.own_wall_s is None
    assert estimator.estimate([]).own_wall_s is None  # no phases
    # A phase measured for memory only has no wall-clock of its own.
    blank = estimates.ResourceEstimator([_record("t1", "sim_run", memory=1, test_run="a")])
    assert blank.estimate([_sim("t1", "a")]).own_wall_s is None
