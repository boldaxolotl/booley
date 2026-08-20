"""Suite-level evidence aggregation for mutation and coverage."""

import subprocess
import types
from pathlib import Path

from booley.specialists.coverage_analyst import (
    CoverageAnalystSpecialist,
    SignalStats,
)
from booley.specialists.mutation_tester import (
    MutationSpec,
    MutationTesterSpecialist,
    MutationTestRun,
)


def _process(returncode: int, output: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=output, stderr="")


def _spec() -> MutationSpec:
    return MutationSpec(1, "operator", "rtl/dut.sv", 1, "a+b", "a-b", mut_id=1)


def _mutation_endpoint(tmp_path: Path) -> MutationTesterSpecialist:
    endpoint = MutationTesterSpecialist()
    endpoint._args = types.SimpleNamespace(work_dir=tmp_path)
    endpoint.emit_progress = lambda _line: None
    endpoint._reset_mutant_logs = lambda: None
    endpoint._persist_mutant_log = lambda _mut_id, _output: ""
    return endpoint


def test_mutant_is_killed_when_any_target_test_fails(tmp_path: Path) -> None:
    endpoint = _mutation_endpoint(tmp_path)
    endpoint._run_target_test_suite = lambda *_args, **_kwargs: [
        MutationTestRun("reset", process=_process(0), output="PASS"),
        MutationTestRun("corner", process=_process(1), output="FAIL"),
    ]

    results, _elapsed = endpoint._run_sim_sweep(
        [_spec()], "sim", tmp_path, tmp_path, "tb"
    )

    assert results[0].detected is True
    assert results[0].invalid is False


def test_mutant_survives_only_when_every_target_test_passes(tmp_path: Path) -> None:
    endpoint = _mutation_endpoint(tmp_path)
    endpoint._run_target_test_suite = lambda *_args, **_kwargs: [
        MutationTestRun("reset", process=_process(0), output="PASS"),
        MutationTestRun("corner", process=_process(0), output="PASS"),
    ]

    results, _elapsed = endpoint._run_sim_sweep(
        [_spec()], "sim", tmp_path, tmp_path, "tb"
    )

    assert results[0].detected is False
    assert results[0].invalid is False


def test_infrastructure_failure_is_invalid_without_a_kill(tmp_path: Path) -> None:
    endpoint = _mutation_endpoint(tmp_path)
    endpoint._run_target_test_suite = lambda *_args, **_kwargs: [
        MutationTestRun("reset", error="simulator missing"),
        MutationTestRun("corner", process=_process(0), output="PASS"),
    ]

    results, _elapsed = endpoint._run_sim_sweep(
        [_spec()], "sim", tmp_path, tmp_path, "tb"
    )

    assert results[0].detected is False
    assert results[0].invalid is True


def test_mutant_timeout_counts_as_kill_not_invalid(tmp_path: Path) -> None:
    endpoint = _mutation_endpoint(tmp_path)
    endpoint._run_target_test_suite = lambda *_args, **_kwargs: [
        MutationTestRun("wedged", timed_out=True),
        MutationTestRun("corner", process=_process(0), output="PASS"),
    ]

    results, _elapsed = endpoint._run_sim_sweep(
        [_spec()], "sim", tmp_path, tmp_path, "tb"
    )

    assert results[0].detected is True
    assert results[0].invalid is False


def test_coverage_signal_evidence_merges_across_target_traces() -> None:
    merged = CoverageAnalystSpecialist._merge_signal_stats(
        [
            [SignalStats("tb.dut.ready", transitions=1, value_hist={"0": 2})],
            [SignalStats("tb.dut.ready", transitions=2, value_hist={"1": 3})],
        ]
    )

    assert merged == [
        SignalStats(
            "tb.dut.ready",
            transitions=3,
            value_hist={"0": 2, "1": 3},
        )
    ]
