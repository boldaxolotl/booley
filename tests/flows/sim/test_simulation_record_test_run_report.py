"""Per-Test-Run report rows and rerun commands of a Simulation Record (ADR 0072)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.builtin_cli import parse_request
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record.test_run_report import (
    simulation_record_test_run_lines,
    simulation_record_test_run_rows,
    test_run_report_lines,
)
from booley.flows.sim.record_files import canonical_json

# Private seam: the only public path to the structured rows is a full run's report.json, and
# the 32-row cap needs 33 Test Runs.
from booley.flows.sim.record_presentation import _simulation_record_report_detail
from booley.flows.target_test_plan import build_target_test_plan
from booley.flows.test_runs import TestRun, build_test_run, test_run_document
from tests.flows.sim.simulation_record_test_support import _recorded_simulation_record
from tests.flows.sim.test_simulation_record_test_runs import _hdl_plan, _run_simulation_record

_SOAK = build_test_run("count", variant="soak", seed=42, args=("+iters=9",))


def _grade_members(*, passed: bool) -> dict:
    """The members of an observation fact that grade it; identity comes from the manifest."""
    return {
        "execution": "completed",
        "failure_class": None if passed else "design",
        "functional": "pass" if passed else "fail",
        "assertions": "clean",
        "assertion_count": 0,
        "cycle_count": None,
        "detail": {"reason": ""},
    }


def _outcome(tmp_path: Path, runs: tuple[TestRun, ...], passed: tuple[bool, ...], required=None):
    """A record outcome whose manifest is on disk and whose runs ended as *passed*."""
    recorded = _recorded_simulation_record(
        tmp_path, runs, ("reset", "count") if required is None else required
    )
    observations = tuple(
        recorded.fact(run, test=run.test, **_grade_members(passed=ok))
        for run, ok in zip(runs, passed, strict=True)
    )
    return SimpleNamespace(manifest_path=recorded.manifest_path, observations=observations)


def test_rows_join_each_observation_to_its_recorded_test_run(tmp_path: Path):
    runs = (build_test_run("reset", seed=1), _SOAK)
    rows = simulation_record_test_run_rows(_outcome(tmp_path, runs, (True, False)))
    assert rows == [
        {**test_run_document(runs[0]), "grade": "pass"},
        {**test_run_document(_SOAK), "grade": "fail"},
    ]
    assert rows[1]["args"] == ["+iters=9"]


def test_report_lists_every_test_run_and_how_to_rerun_each_failure(tmp_path: Path):
    runs = (build_test_run("count", seed=3), build_test_run("count", seed=4), _SOAK)
    outcome = _outcome(tmp_path, runs, (True, False, False))
    assert simulation_record_test_run_lines("sim", outcome, default_seed=1) == [
        "  Test Runs: 3 (1 passed, 2 failed)",
        "    PASS         count@3",
        "    FAIL         count@4",
        "    FAIL         count+soak@42",
        "  Rerun a failed Test Run with the same seed:",
        "    booley flow sim --target sim --test=count@4",
        "    booley flow sim --target sim --test=count+soak@42",
    ]


@pytest.mark.parametrize(
    ("grades", "tail"),
    [
        ((True, True), []),
        (
            (True, False),
            [
                "  Rerun a failed Test Run with the same seed:",
                "    booley flow sim --target sim --test=count@1",
            ],
        ),
    ],
    ids=["passing-adds-no-rows", "failing-lists-rerun-command"],
)
def test_default_runs_list_only_failures_on_the_target_line(
    tmp_path: Path, grades: tuple[bool, ...], tail: list[str]
):
    runs = (build_test_run("reset", seed=1), build_test_run("count", seed=1))
    outcome = _outcome(tmp_path, runs, grades)
    lines = simulation_record_test_run_lines("sim", outcome, default_seed=1)
    assert (lines[-2:] if tail else lines) == tail
    assert len(simulation_record_test_run_rows(outcome)) == 2


def test_rows_and_rerun_commands_are_capped():
    rows = [
        {**test_run_document(build_test_run("t", seed=n)), "grade": "fail"} for n in range(1, 41)
    ]
    lines = test_run_report_lines("sim", rows)
    assert lines[0] == "  Test Runs: 40 (40 failed)"
    assert lines.count("    ... and 8 more") == 2
    assert len(lines) == 1 + 32 + 1 + 1 + 32 + 1


def test_structured_detail_carries_bounded_rows(tmp_path: Path):
    runs = tuple(build_test_run("count", seed=n) for n in range(1, 36))
    outcome = _run_simulation_record(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))
    detail = _simulation_record_report_detail(outcome, None)
    assert detail["test_run_total"] == 35
    assert detail["test_runs_truncated"] is True
    assert [row["name"] for row in detail["test_runs"]] == [run.name for run in runs[:32]]
    assert {row["grade"] for row in detail["test_runs"]} == {"fail"}
    assert set(detail["test_runs"][0]) == {
        "name",
        "test",
        "variant",
        "seed",
        "args",
        "env",
        "fingerprint",
        "grade",
    }


def _gone_manifest(tmp_path: Path, outcome):
    """The outcome with its manifest deleted."""
    return SimpleNamespace(manifest_path=tmp_path / "gone.json", observations=outcome.observations)


def _hand_edited_fingerprint(tmp_path: Path, outcome):
    """The outcome whose manifest carries a Test Run fingerprint that no longer matches."""
    document = json.loads(outcome.manifest_path.read_text())
    document["work_items"][0]["test_runs"][0]["fingerprint"] = "sha256:" + "0" * 64
    # Canonical bytes, so the codec accepts the document and only the fingerprint is wrong.
    outcome.manifest_path.write_bytes(canonical_json(document))
    return outcome


@pytest.mark.parametrize("damage", [_gone_manifest, _hand_edited_fingerprint])
def test_a_damaged_simulation_record_has_no_rows(tmp_path: Path, damage):
    outcome = _outcome(tmp_path, (_SOAK,), (False,))
    assert simulation_record_test_run_rows(damage(tmp_path, outcome)) == []


_TESTS = {
    "tests": ["reset", "count"],
    "variants": {"count": {"default": "short", "short": [], "soak": ["+iters=9"]}},
}


@pytest.mark.parametrize(
    ("target", "adapter", "run_of", "command"),
    [
        (
            "sim",
            "verilator",
            lambda plan: plan.run("count", "soak", 42),
            "booley flow sim --target sim --test=count+soak@42",
        ),
        (
            "sim_icarus",
            "icarus",
            lambda plan: plan.run("count", "soak"),
            "booley flow sim --target sim_icarus --test=count+soak",
        ),
        (
            "booley:ibex:port#sim",
            "verilator",
            lambda plan: plan.run("reset", seed=2147483647),
            "booley flow sim --target 'booley:ibex:port#sim' --test=reset@2147483647",
        ),
    ],
)
def test_emitted_rerun_command_selects_exactly_the_failed_test_run(
    target, adapter, run_of, command
):
    """Parse the printed line back and resolve it: it must name the same Test Run."""
    plan = build_target_test_plan(
        target, normalize_tests_toml({target: _TESTS})[target], adapter=adapter
    )
    failed = run_of(plan)
    row = {**test_run_document(failed), "grade": "fail"}
    line = test_run_report_lines(target, [row])[-1].strip()
    assert line == command

    argv = shlex.split(line)
    request = parse_request(SimulateFlow(), argv[3:])
    assert request.target == target
    assert plan.resolve(request.test, seed=request.seed) == (failed,)


@pytest.mark.parametrize(
    ("adapter", "seed", "tail"), [("verilator", 9, ["--seed", "9"]), ("icarus", None, [])]
)
def test_rerun_command_of_the_unnamed_invocation(adapter, seed, tail):
    plan = build_target_test_plan("sim", None, adapter=adapter)
    failed = plan.unnamed_run(seed)
    row = {**test_run_document(failed), "grade": "fail"}
    argv = shlex.split(test_run_report_lines("sim", [row])[-1].strip())
    assert argv == ["booley", "flow", "sim", "--target", "sim", *tail]
    request = parse_request(SimulateFlow(), argv[3:])
    assert request.test is None
    assert plan.resolve(request.test, seed=request.seed) == (failed,)


def _batch_outcome(tmp_path: Path, seed: int | None, *graded: tuple[str | None, bool]):
    """An unnamed-invocation outcome with one fact per ``(discovered name, passed)`` pair."""
    unnamed = build_test_run(None, seed=seed)
    recorded = _recorded_simulation_record(tmp_path, (unnamed,), ())
    observations = tuple(
        recorded.fact(unnamed, discovered=name, **_grade_members(passed=ok)) for name, ok in graded
    )
    return SimpleNamespace(manifest_path=recorded.manifest_path, observations=observations)


def test_tests_an_unfiltered_cocotb_batch_discovered_get_rows(tmp_path: Path):
    outcome = _batch_outcome(tmp_path, 9, ("found_a", True), ("found_b", False))
    rows = simulation_record_test_run_rows(outcome)
    assert [(row["name"], row["grade"]) for row in rows] == [
        ("found_a@9", "pass"),
        ("found_b@9", "fail"),
    ]


def test_failed_discovered_tests_share_one_batch_rerun_command(tmp_path: Path):
    """A discovered Test has no registered name to select, so its batch reruns as one."""
    outcome = _batch_outcome(
        tmp_path, 9, ("found_a", True), ("found_b", False), ("found_c", False), ("found_d", False)
    )
    lines = simulation_record_test_run_lines("sim", outcome, default_seed=1)
    assert lines[-2:] == [
        "  Rerun the whole batch with the same seed "
        "(register its test IDs as Tests in tests.toml to rerun one alone):",
        "    booley flow sim --target sim --seed 9",
    ]
    assert "  Rerun a failed Test Run with the same seed:" not in lines
    assert not any("--test=" in line for line in lines)


def test_batch_run_failure_and_discovered_failures_print_one_shared_command(tmp_path: Path):
    """The unnamed run's own failed row and discovered rows share one command line."""
    outcome = _batch_outcome(tmp_path, 9, (None, False), ("found_b", False))
    lines = simulation_record_test_run_lines("sim", outcome, default_seed=1)
    assert [line for line in lines if "booley flow sim" in line] == [
        "    booley flow sim --target sim --seed 9"
    ]
    assert lines.count("    booley flow sim --target sim --seed 9") == 1
    assert "  Rerun a failed Test Run with the same seed:" not in lines


def test_unseeded_batch_rerun_command_has_no_seed_flag(tmp_path: Path):
    """An unseeded batch reruns with the bare Target command."""
    outcome = _batch_outcome(tmp_path, None, ("found_b", False))
    lines = simulation_record_test_run_lines("sim", outcome, default_seed=1)
    assert lines[-1] == "    booley flow sim --target sim"
