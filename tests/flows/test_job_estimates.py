"""Phase estimates from the Resource History: own, borrowed and unknown (#3237)."""

from __future__ import annotations

from typing import Any

import pytest

from booley.flows import job_estimates as estimates


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


def _sim(
    target: str, *tests: str, flags: tuple[str, ...] = (), pre_sim_commands: bool = False
) -> Any:
    return estimates.JobRequest(
        "sim", target, test_runs=tests, flags=flags, pre_sim_commands=pre_sim_commands
    )


def _phase(job: Any, phase: str, test_run: str = "") -> Any:
    """The estimate of one phase of *job*."""
    (match,) = [
        item for item in job.phases if (item.key.phase, item.key.test_run) == (phase, test_run)
    ]
    return match


def test_memory_estimate_is_largest_record_times_margin() -> None:
    records = [_record("t", "sim_run", memory=mb, test_run="a") for mb in (2048, 2560, 6656)]

    job = estimates.ResourceEstimator(records).estimate([_sim("t", "a")])

    estimate = _phase(job, "sim_run", "a").memory_mb
    # 6.5 GiB x 1.25 = 8.125 GiB.
    assert estimate.value == 8320.0
    assert estimate.source is estimates.Source.OWN


@pytest.mark.parametrize("outcome", ["oom_kill", "host_oom_kill"])
def test_oom_killed_record_only_raises_the_estimate(outcome: str) -> None:
    for oom_peak, expected in ((8192, 10240.0), (1024, 8320.0)):
        records = [_record("t", "sim_run", memory=mb, test_run="a") for mb in (2048, 2560, 6656)]
        records.append(_record("t", "sim_run", memory=oom_peak, test_run="a", outcome=outcome))

        job = estimates.ResourceEstimator(records).estimate([_sim("t", "a")])

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
        # FuseSoC setup is a measured phase of every sim request (#3787).
        _record("t1", "sim_prepare", memory=500),
        # Pre-Sim Commands are measured per Target (#4534).
        _record("t2", "sim_pre_run", memory=3000),
    ]


def test_missing_key_borrows_from_target_then_project() -> None:
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

    # Pre-Sim Commands: the Target's own measurement, else the Target's build, never
    # another Target's measurement.
    measured = estimator.estimate([_sim("t2", "x", pre_sim_commands=True)])
    pre_sim = _phase(measured, "sim_pre_run").memory_mb
    assert (pre_sim.value, pre_sim.source) == (3750.0, estimates.Source.OWN)
    unmeasured = estimator.estimate([_sim("t1", "x", pre_sim_commands=True)])
    fallback = _phase(unmeasured, "sim_pre_run")
    assert (fallback.memory_mb.value, fallback.memory_mb.source) == (
        25000.0,
        estimates.Source.OWN,
    )
    assert fallback.key.phase == "sim_pre_run"

    lint = estimator.estimate([estimates.JobRequest("lint", "t1")])
    unknown = _phase(lint, "lint").memory_mb
    assert (unknown.value, unknown.source) == (None, estimates.Source.UNKNOWN)


def test_wall_clock_unknown_and_independent_of_memory() -> None:
    records = [_record("t1", "sim_run", memory=7000, test_run="blank")]

    job = estimates.ResourceEstimator(records).estimate([_sim("t1", "blank")])

    run = _phase(job, "sim_run", "blank")
    assert (run.memory_mb.value, run.memory_mb.source) == (8750.0, estimates.Source.OWN)
    assert (run.wall_s.value, run.wall_s.source) == (None, estimates.Source.UNKNOWN)


def test_own_wall_clock_needs_own_history_for_every_phase() -> None:
    records = [
        _record("t1", "sim_prepare", memory=100, wall=4.0),
        _record("t1", "sim_build", memory=100, wall=8.0),
        _record("t1", "sim_run", memory=100, wall=12.0, test_run="a"),
        _record("t2", "sim_run", memory=100, wall=2.0, test_run="b"),
    ]
    estimator = estimates.ResourceEstimator(records)

    own = estimator.estimate([_sim("t1", "a")])
    borrowed = estimator.estimate([_sim("t1", "a"), _sim("t1", "new")])
    unknown = estimator.estimate([_sim("t3", "a")])

    # Every phase has its own history: (4 + 8 + 12) x 1.25.
    assert own.own_wall_s == 30.0
    assert borrowed.own_wall_s is None  # the second Test Run borrows its wall-clock
    assert unknown.own_wall_s is None
    assert estimator.estimate([]).own_wall_s is None  # no phases
    # A phase measured for memory only has no wall-clock of its own.
    blank = estimates.ResourceEstimator([_record("t1", "sim_run", memory=1, test_run="a")])
    assert blank.estimate([_sim("t1", "a")]).own_wall_s is None
    # A corrupt (negative) record is no history either.
    corrupt = estimates.ResourceEstimator(
        [_record("t1", "sim_run", memory=1, wall=-5.0, test_run="a")]
    )
    assert corrupt.estimate([_sim("t1", "a")]).own_wall_s is None


def test_every_sim_request_starts_with_sim_prepare() -> None:
    """FuseSoC setup is a measured phase of every Simulation Job, not only coverage (#3787)."""
    ordinary = _sim("t", "a", "b")
    elab_only = _sim("t")
    coverage = _sim("t", "a", flags=("coverage",))

    for request in (ordinary, elab_only, coverage):
        keys = estimates.phase_keys(request)
        assert keys[0] == estimates.HistoryKey(
            "t", "sim_prepare", tuple(sorted(request.flags)), ""
        ), request
    assert [key.phase for key in estimates.phase_keys(ordinary)] == [
        "sim_prepare",
        "sim_build",
        "sim_run",
        "sim_run",
    ]
    assert [key.phase for key in estimates.phase_keys(elab_only)] == ["sim_prepare", "sim_build"]
    # Only coverage post-processes.
    assert [key.phase for key in estimates.phase_keys(coverage)][-1] == "sim_postprocess"
    # Pre-Sim Commands are a phase of a Job that runs Tests (#4534).
    hooked = _sim("t", "a", "b", pre_sim_commands=True)
    assert [key.phase for key in estimates.phase_keys(hooked)] == [
        "sim_prepare",
        "sim_build",
        "sim_pre_run",
        "sim_run",
        "sim_run",
    ]
    assert "sim_pre_run" not in {
        key.phase for key in estimates.phase_keys(_sim("t", pre_sim_commands=True))
    }
    assert "sim_postprocess" not in {key.phase for key in estimates.phase_keys(ordinary)}
