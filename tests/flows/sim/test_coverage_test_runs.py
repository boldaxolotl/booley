"""A coverage campaign collects several Test Runs of one Test (ADR 0072).

Two Test Variants of one Test, a pinned seed and ``test@xN`` are each one
simulator process in the one ``coverage_aggregate`` work item; the merged
coverage is the union over all of them.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
from booley.flows.sim.coverage_invocation import (
    CoverageInvocationRequest,
    prepare_coverage_invocation,
)
from booley.flows.sim.coverage_transaction import run_coverage_target
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import (
    SelectedCoverageTest,
    SimulationRunResult,
)
from booley.flows.target_test_plan import MAX_TEST_RUNS_PER_REQUEST, load_target_test_plan
from booley.flows.test_runs import TestRun, build_test_run
from tests.flows.sim.test_coverage_flow import (
    _AcceptanceAdapter,
    _prepare_atomic_coverage_state,
    _SplitMetricsExecution,
)
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution, Progress

_TESTS_TOML = """\
[sim_0]
tests = ["reset", "wrap"]

[sim_0.variants.reset]
default = "short"
short = []
soak = { args = ["+iters=9"], env = { SOAK = "1" } }
"""
# Characters a file name may hold on Windows as well as POSIX.
_WINDOWS_SAFE_NAME = re.compile(r"[A-Za-z0-9_.+@-]+\Z")


def _fixed_draw(count: int, *, exclude=()) -> tuple[int, ...]:
    """Deterministic stand-in for the random seed draw."""
    return tuple(seed for seed in (11, 22, 33, 44) if seed not in exclude)[:count]


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    context = project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text(_TESTS_TOML)
    return context


def _runs(tmp_path: Path, *selectors: str) -> tuple[TestRun, ...]:
    plan = load_target_test_plan(tmp_path, "sim_0", adapter="verilator")
    return plan.resolve(selectors, draw=_fixed_draw)


def _target_plan(tmp_path: Path, context, runs: tuple[TestRun, ...]):
    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), test_runs={"sim_0": runs}), context
    )
    assert prepared.plan is not None, prepared.findings
    return replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")


class _FailingRun(NativeExecution):
    """Every run passes except the one named *failing*."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self._failing = failing

    def run(self, request):
        result = super().run(request)
        if request.test.run_name == self._failing:
            return SimulationRunResult("fail", "scoreboard mismatch")
        return result


def test_two_variants_and_a_seed_count_are_one_process_each(tmp_path, monkeypatch):
    context = _project(tmp_path, monkeypatch)
    runs = _runs(tmp_path, "wrap@x3", "reset+soak", "reset")
    plan = _target_plan(tmp_path, context, runs)
    execution = NativeExecution()

    outcome = run_coverage_target(plan, execution, Progress())

    assert outcome.exit_code == 0
    names = ["reset+soak@1", "reset+short@1", "wrap@11", "wrap@22", "wrap@33"]
    # Grouped by Test; the runs of one Test keep the order of the request.
    assert [request.test.run_name for request in execution.runs] == names
    assert plan.selected_tests == ("reset", "wrap")
    assert [run.name for run in plan.test_runs] == names
    soak = execution.runs[0].test.run
    assert (soak.args, dict(soak.env), soak.seed) == (("+iters=9",), {"SOAK": "1"}, 1)
    assert [request.run_id for request in execution.runs] == [
        f"run:{index:03d}:{name}" for index, name in enumerate(names, start=1)
    ]
    assert [request.raw_path.name for request in execution.runs] == [
        f"{index:03d}-{name}.dat" for index, name in enumerate(names, start=1)
    ]
    assert all(_WINDOWS_SAFE_NAME.match(request.raw_path.name) for request in execution.runs)

    document = json.loads(outcome.campaign_path.read_text())
    assert document["$schema"] == "booley.coverage-campaign/v5"
    assert document["tests"]["selected"] == ["reset", "wrap"]
    assert [(run["test"], run["test_run"]) for run in document["tests"]["runs"]] == [
        ("reset", "reset+soak@1"),
        ("reset", "reset+short@1"),
        ("wrap", "wrap@11"),
        ("wrap", "wrap@22"),
        ("wrap", "wrap@33"),
    ]
    assert [run["fingerprint"] for run in document["tests"]["runs"]] == [
        run.fingerprint for run in plan.test_runs
    ]
    # The merged result is the union: every run contributes its own hits.
    campaign = load_coverage_campaign(outcome.campaign_path).campaign
    assert dict(campaign.points[0].hits_by_run) == {
        f"run:{index:03d}:{name}": 2 for index, name in enumerate(names, start=1)
    }
    rows = json.loads(outcome.simulation_path.read_text())["tests"]
    assert [(row["name"], row["test_run"], row["variant"], row["seed"]) for row in rows] == [
        ("reset", "reset+soak@1", "soak", 1),
        ("reset", "reset+short@1", "short", 1),
        ("wrap", "wrap@11", None, 11),
        ("wrap", "wrap@22", None, 22),
        ("wrap", "wrap@33", None, 33),
    ]


def test_one_failing_run_fails_the_coverage_campaign(tmp_path, monkeypatch):
    context = _project(tmp_path, monkeypatch)
    plan = _target_plan(tmp_path, context, _runs(tmp_path, "reset", "reset+soak", "wrap"))

    outcome = run_coverage_target(plan, _FailingRun("reset+soak@1"), Progress())

    assert outcome.exit_code == 1
    simulation = json.loads(outcome.simulation_path.read_text())
    assert simulation["passed"] is False
    assert simulation["simulation"] == "fail"
    assert [(row["test_run"], row["passed"]) for row in simulation["tests"]] == [
        ("reset+short@1", True),
        ("reset+soak@1", False),
        ("wrap@1", True),
    ]


def test_evidence_counts_a_test_as_passed_only_when_every_run_of_it_passed(tmp_path, monkeypatch):
    from booley.flows.sim.coverage_acceptance import _simulation_detail

    context = _project(tmp_path, monkeypatch)
    plan = _target_plan(tmp_path, context, _runs(tmp_path, "reset", "reset+soak", "wrap"))
    outcome = run_coverage_target(plan, _FailingRun("reset+soak@1"), Progress())
    campaign = load_coverage_campaign(outcome.campaign_path).campaign

    detail = _simulation_detail(plan, campaign, outcome.campaign_path)

    assert detail["selected_tests"] == ["reset", "wrap"]
    assert (detail["passed_tests"], detail["failed_tests"]) == (["wrap"], ["reset"])
    assert (detail["tests_passed"], detail["tests_total"]) == (1, 2)


def test_a_run_name_outside_the_safe_set_becomes_a_digest_not_a_lossy_name(tmp_path):
    from booley.flows.sim.verilator_coverage import _run_id, _run_stem

    plain = SelectedCoverageTest("smoke test")
    other = SelectedCoverageTest("smoke_test")

    assert _run_stem(1, plain).startswith("001-~sha256-")
    assert _run_stem(1, plain) != _run_stem(1, other) == "001-smoke_test"
    seeded = SelectedCoverageTest("stress", build_test_run("stress", variant="soak", seed=42))
    assert _run_stem(7, seeded) == "007-stress+soak@42"
    assert _run_id(7, seeded) == "run:007:stress+soak@42"


def test_a_selected_run_must_be_a_run_of_its_test():
    with pytest.raises(ValueError, match="not a run of selected Test"):
        SelectedCoverageTest("reset", build_test_run("wrap", seed=1))


def test_preflight_refuses_runs_of_an_unregistered_test(tmp_path, monkeypatch):
    context = _project(tmp_path, monkeypatch)
    runs = (build_test_run("ghost", seed=1),)

    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), test_runs={"sim_0": runs}), context
    )

    assert prepared.plan is None
    assert [finding.code for finding in prepared.findings] == ["COV_SUITE_INVALID"]


def _flow_result(tmp_path: Path, execution: NativeExecution, *tests: str):
    return SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: execution
    ).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
            test=tests or None,
        )
    )


def test_flow_records_every_test_run_in_one_coverage_aggregate(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    execution = NativeExecution()

    result = _flow_result(tmp_path, execution, "reset+short", "reset+soak", "wrap@x3")

    assert result.exit_code == 0, result.outcome
    manifest = json.loads(next((tmp_path / "reports").rglob("campaign/manifest.json")).read_text())
    (item,) = manifest["work_items"]
    assert item["kind"] == "coverage_aggregate"
    assert item["selection"]["names"] == ["reset", "wrap"]
    recorded = [run["name"] for run in item["test_runs"]]
    assert recorded[:2] == ["reset+short@1", "reset+soak@1"]
    assert len(recorded) == len(set(recorded)) == 5
    assert all(name.startswith("wrap@") for name in recorded[2:])
    # Each simulator process is one recorded Test Run, in the recorded order.
    assert [request.test.run_name for request in execution.runs] == recorded
    campaign = result.outcome.detail["campaigns"]["sim_0"]
    stored = json.loads(next((tmp_path / "reports").rglob("work-items/*/result.json")).read_text())
    assert [row["test_run"] for row in stored["observations"]] == recorded
    assert [row["name"] for row in campaign["test_runs"]] == recorded
    assert [(row["variant"], row["grade"]) for row in campaign["test_runs"][:2]] == [
        ("short", "pass"),
        ("soak", "pass"),
    ]
    for name in recorded:
        assert name in result.outcome.report_text
    nested = json.loads(
        next((tmp_path / "reports").rglob("coverage-campaign/coverage.json")).read_text()
    )
    assert [run["test_run"] for run in nested["tests"]["runs"]] == recorded


def _ticket_coverage_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *tests: str):
    """Run Ticket-backed coverage of ``sim_custom`` (one Test, ``gap``) with *tests*."""
    state_path, initial_state = _prepare_atomic_coverage_state(tmp_path, monkeypatch)
    adapter = _AcceptanceAdapter(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: _SplitMetricsExecution()
    ).execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
            test=tests or None,
        ),
        adapter=adapter,
    )
    return result, state_path, initial_state


@pytest.mark.parametrize("tests", [(), ("gap",), ("gap@1",)])
def test_default_coverage_runs_keep_recording_goal_evidence(tmp_path, monkeypatch, tests):
    result, state_path, initial_state = _ticket_coverage_result(tmp_path, monkeypatch, *tests)

    assert "test_run_goal_guard" not in result.outcome.detail
    assert state_path.read_bytes() != initial_state
    assert len(DevelopmentState.load(state_path).acceptance_transactions) == 1


def test_coverage_of_non_default_runs_records_no_goal_evidence(tmp_path, monkeypatch):
    result, state_path, _initial = _ticket_coverage_result(tmp_path, monkeypatch, "gap@7")

    assert result.outcome.detail["test_run_goal_guard"] == {
        "diagnostic": True,
        "test_runs": ["sim_custom: gap@7"],
    }
    assert "it records no Goal evidence" in result.outcome.report_text
    # The campaign ran and collected coverage; only the evidence is withheld.
    nested = next((tmp_path / "reports").rglob("coverage-campaign/coverage.json"))
    assert [run["test_run"] for run in json.loads(nested.read_text())["tests"]["runs"]] == [
        "gap@7"
    ]
    saved = DevelopmentState.load(state_path)
    assert not saved.acceptance_transactions
    assert all(entry.met is not True for entry in saved.criteria.values())


def test_flow_fails_when_one_of_several_runs_of_a_test_fails(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = _flow_result(tmp_path, _FailingRun("reset+soak@1"), "reset", "reset+soak")

    assert result.exit_code == 1, result.outcome
    rows = result.outcome.detail["campaigns"]["sim_0"]["test_runs"]
    assert [(row["name"], row["grade"]) for row in rows] == [
        ("reset+short@1", "pass"),
        ("reset+soak@1", "fail"),
    ]
    assert "booley flow sim --target sim_0 --test=reset+soak@1" in result.outcome.report_text


def test_coverage_refuses_more_test_runs_than_one_request_allows(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    execution = NativeExecution()

    result = _flow_result(tmp_path, execution, "wrap@x1000", "reset")

    assert result.exit_code != 0
    assert f"more than {MAX_TEST_RUNS_PER_REQUEST} Test Runs" in result.outcome.report_text
    assert execution.runs == []
