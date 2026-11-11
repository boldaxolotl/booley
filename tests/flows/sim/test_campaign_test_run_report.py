"""Per-Test-Run report rows and rerun commands of a Simulation Campaign (ADR 0072)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.builtin_cli import parse_request
from booley.flows.sim.campaign.test_run_report import (
    _recorded_test_runs,
    campaign_test_run_rows,
    test_run_report_lines,
)
from booley.flows.sim.flow import (
    SimulateFlow,
    _campaign_report_detail,
    _campaign_test_run_lines,
)
from booley.flows.target_test_plan import build_target_test_plan
from booley.flows.test_runs import TestRun, build_test_run, test_run_document
from tests.flows.sim.campaign_test_support import _recorded_campaign
from tests.flows.sim.test_campaign_test_runs import _hdl_plan, _run_campaign

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
    """A campaign outcome whose manifest is on disk and whose runs ended as *passed*."""
    recorded = _recorded_campaign(
        tmp_path, runs, ("reset", "count") if required is None else required
    )
    observations = tuple(
        recorded.fact(run, test=run.test, **_grade_members(passed=ok))
        for run, ok in zip(runs, passed, strict=True)
    )
    return SimpleNamespace(manifest_path=recorded.manifest_path, observations=observations)


def test_rows_join_each_observation_to_its_recorded_test_run(tmp_path: Path):
    runs = (build_test_run("reset", seed=1), _SOAK)
    rows = campaign_test_run_rows(_outcome(tmp_path, runs, (True, False)))
    assert rows == [
        {**test_run_document(runs[0]), "grade": "pass"},
        {**test_run_document(_SOAK), "grade": "fail"},
    ]
    assert rows[1]["args"] == ["+iters=9"]


def test_report_lists_every_test_run_and_how_to_rerun_each_failure(tmp_path: Path):
    runs = (build_test_run("count", seed=3), build_test_run("count", seed=4), _SOAK)
    outcome = _outcome(tmp_path, runs, (True, False, False))
    assert _campaign_test_run_lines("sim", outcome) == [
        "  Test Runs: 3 (1 passed, 2 failed)",
        "    PASS         count@3",
        "    FAIL         count@4",
        "    FAIL         count+soak@42",
        "  Rerun a failed Test Run with the same seed:",
        "    booley flow sim --target sim --test=count@4",
        "    booley flow sim --target sim --test=count+soak@42",
    ]


def test_default_runs_that_pass_add_no_rows_to_the_target_line(tmp_path: Path):
    runs = (build_test_run("reset", seed=1), build_test_run("count", seed=1))
    outcome = _outcome(tmp_path, runs, (True, True))
    assert _campaign_test_run_lines("sim", outcome) == []
    assert len(campaign_test_run_rows(outcome)) == 2


def test_a_failing_default_run_is_listed_with_its_rerun_command(tmp_path: Path):
    runs = (build_test_run("reset", seed=1), build_test_run("count", seed=1))
    lines = _campaign_test_run_lines("sim", _outcome(tmp_path, runs, (True, False)))
    assert lines[-2:] == [
        "  Rerun a failed Test Run with the same seed:",
        "    booley flow sim --target sim --test=count@1",
    ]


def test_rows_and_rerun_commands_are_capped(tmp_path: Path):
    rows = [
        {**test_run_document(build_test_run("t", seed=n)), "grade": "fail"} for n in range(1, 41)
    ]
    lines = test_run_report_lines("sim", rows)
    assert lines[0] == "  Test Runs: 40 (40 failed)"
    assert lines.count("    ... and 8 more") == 2
    assert len(lines) == 1 + 32 + 1 + 1 + 32 + 1


def test_structured_detail_carries_bounded_rows(tmp_path: Path):
    runs = tuple(build_test_run("count", seed=n) for n in range(1, 36))
    outcome = _run_campaign(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))
    detail = _campaign_report_detail(outcome, None)
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


def test_a_campaign_whose_manifest_is_gone_has_no_rows(tmp_path: Path):
    outcome = _outcome(tmp_path, (_SOAK,), (False,))
    gone = SimpleNamespace(manifest_path=tmp_path / "gone.json", observations=outcome.observations)
    assert campaign_test_run_rows(gone) == []


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


def test_tests_an_unfiltered_cocotb_batch_discovered_get_rows(tmp_path: Path):
    unnamed = build_test_run(None, seed=9)
    recorded = _recorded_campaign(tmp_path, (unnamed,), ())
    observations = (
        recorded.fact(unnamed, discovered="found_a", **_grade_members(passed=True)),
        recorded.fact(unnamed, discovered="found_b", **_grade_members(passed=False)),
    )
    rows = campaign_test_run_rows(
        SimpleNamespace(manifest_path=recorded.manifest_path, observations=observations)
    )
    assert [(row["name"], row["grade"]) for row in rows] == [
        ("found_a@9", "pass"),
        ("found_b@9", "fail"),
    ]


def test_failed_discovered_tests_share_one_batch_rerun_command(tmp_path: Path):
    """A discovered Test has no registered name to select, so its batch reruns as one."""
    unnamed = build_test_run(None, seed=9)
    recorded = _recorded_campaign(tmp_path, (unnamed,), ())
    observations = (
        recorded.fact(unnamed, discovered="found_a", **_grade_members(passed=True)),
        recorded.fact(unnamed, discovered="found_b", **_grade_members(passed=False)),
        recorded.fact(unnamed, discovered="found_c", **_grade_members(passed=False)),
        recorded.fact(unnamed, discovered="found_d", **_grade_members(passed=False)),
    )
    outcome = SimpleNamespace(manifest_path=recorded.manifest_path, observations=observations)
    lines = _campaign_test_run_lines("sim", outcome)
    assert lines[-2:] == [
        "  Rerun the whole batch with the same seed "
        "(register its test functions as Tests in tests.toml to rerun one alone):",
        "    booley flow sim --target sim --seed 9",
    ]
    assert "  Rerun a failed Test Run with the same seed:" not in lines
    assert not any("--test=" in line for line in lines)


def test_batch_run_failure_and_discovered_failures_print_one_shared_command(tmp_path: Path):
    """The unnamed run's own failed row and discovered rows share one command line."""
    unnamed = build_test_run(None, seed=9)
    recorded = _recorded_campaign(tmp_path, (unnamed,), ())
    observations = (
        recorded.fact(unnamed, **_grade_members(passed=False)),
        recorded.fact(unnamed, discovered="found_b", **_grade_members(passed=False)),
    )
    outcome = SimpleNamespace(manifest_path=recorded.manifest_path, observations=observations)
    lines = _campaign_test_run_lines("sim", outcome)
    assert [line for line in lines if "booley flow sim" in line] == [
        "    booley flow sim --target sim --seed 9"
    ]
    assert lines.count("    booley flow sim --target sim --seed 9") == 1
    assert "  Rerun a failed Test Run with the same seed:" not in lines


def test_unseeded_batch_rerun_command_has_no_seed_flag(tmp_path: Path):
    """An unseeded batch reruns with the bare Target command."""
    unnamed = build_test_run(None, seed=None)
    recorded = _recorded_campaign(tmp_path, (unnamed,), ())
    observations = (recorded.fact(unnamed, discovered="found_b", **_grade_members(passed=False)),)
    outcome = SimpleNamespace(manifest_path=recorded.manifest_path, observations=observations)
    lines = _campaign_test_run_lines("sim", outcome)
    assert lines[-1] == "    booley flow sim --target sim"


def test_a_hand_edited_test_run_fingerprint_reads_as_no_recorded_runs(tmp_path: Path):
    outcome = _outcome(tmp_path, (build_test_run("reset"),), (True,))
    document = json.loads(outcome.manifest_path.read_text())
    document["work_items"][0]["test_runs"][0]["fingerprint"] = "sha256:" + "0" * 64
    outcome.manifest_path.write_text(json.dumps(document))

    assert _recorded_test_runs(outcome.manifest_path) == {}
