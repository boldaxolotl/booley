"""A Coverage Record collects several Test Runs of one Test (ADR 0072).

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

from booley.flows.sim.coverage_invocation import (
    CoverageInvocationRequest,
    prepare_coverage_invocation,
)
from booley.flows.sim.coverage_json import coverage_mapping_document
from booley.flows.sim.coverage_record_store import load_coverage_record
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.verilator_coverage import (
    SelectedCoverageRun,
    SimulationRunResult,
)
from booley.flows.target_test_plan import MAX_TEST_RUNS_PER_REQUEST
from booley.flows.target_test_plan_loading import load_target_test_plan
from booley.flows.test_runs import TestRun, build_test_run, test_run_document
from tests.flows.sim.test_coverage_flow import (
    _coverage,
    _pin_revision,
    _prepare_sim_custom_project,
    _SplitMetricsExecution,
)
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution, run_target

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
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(_TESTS_TOML)
    return context


def _runs(tmp_path: Path, *selectors: str) -> tuple[TestRun, ...]:
    plan = load_target_test_plan(tmp_path, "sim_0", identity="sim_0", adapter="verilator")
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

    outcome = run_target(plan, execution)

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

    document = json.loads(outcome.coverage_record_path.read_text())
    assert document["$schema"] == "booley.coverage-record/v1"
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
    coverage_record = load_coverage_record(outcome.coverage_record_path).coverage_record
    assert dict(coverage_record.points[0].hits_by_run) == {
        f"run:{index:03d}:{name}": 2 for index, name in enumerate(names, start=1)
    }
    rows = coverage_mapping_document(outcome.detail)["tests"]
    assert [(row["name"], row["test_run"], row["variant"], row["seed"]) for row in rows] == [
        ("reset", "reset+soak@1", "soak", 1),
        ("reset", "reset+short@1", "short", 1),
        ("wrap", "wrap@11", None, 11),
        ("wrap", "wrap@22", None, 22),
        ("wrap", "wrap@33", None, 33),
    ]


def test_one_failing_run_fails_the_coverage_simulation_record(tmp_path, monkeypatch):
    context = _project(tmp_path, monkeypatch)
    plan = _target_plan(tmp_path, context, _runs(tmp_path, "reset", "reset+soak", "wrap"))

    outcome = run_target(plan, _FailingRun("reset+soak@1"))

    assert outcome.exit_code == 1
    simulation = coverage_mapping_document(outcome.detail)
    assert simulation["passed"] is False
    assert simulation["simulation"] == "fail"
    assert [(row["test_run"], row["passed"]) for row in simulation["tests"]] == [
        ("reset+short@1", True),
        ("reset+soak@1", False),
        ("wrap@1", True),
    ]


def test_a_selected_run_must_be_a_run_of_its_test():
    with pytest.raises(ValueError, match="not a run of selected Test"):
        SelectedCoverageRun("reset", build_test_run("wrap", seed=1))


def test_preflight_refuses_runs_of_an_unregistered_test(tmp_path, monkeypatch):
    context = _project(tmp_path, monkeypatch)
    runs = (build_test_run("ghost", seed=1),)

    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), test_runs={"sim_0": runs}), context
    )

    assert prepared.plan is None
    assert [finding.code for finding in prepared.findings] == ["COV_SUITE_INVALID"]


def test_flow_records_every_test_run_in_one_coverage_aggregate(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    execution = NativeExecution()

    result = _coverage(tmp_path, execution, test=("reset+short", "reset+soak", "wrap@x3"))

    assert result.exit_code == 0, result.outcome
    manifest = json.loads(next((tmp_path / "reports").rglob("record/manifest.json")).read_text())
    (item,) = manifest["work_items"]
    assert item["kind"] == "coverage_aggregate"
    assert item["selection"]["names"] == ["reset", "wrap"]
    recorded = [run["name"] for run in item["test_runs"]]
    assert recorded[:2] == ["reset+short@1", "reset+soak@1"]
    assert len(recorded) == len(set(recorded)) == 5
    assert all(name.startswith("wrap@") for name in recorded[2:])
    # Each simulator process is one recorded Test Run, in the recorded order.
    assert [request.test.run_name for request in execution.runs] == recorded
    simulation_record = result.outcome.detail["records"]["sim_0"]
    stored = json.loads(next((tmp_path / "reports").rglob("work-items/*/result.json")).read_text())
    assert [row["test_run"] for row in stored["observations"]] == recorded
    assert [row["name"] for row in simulation_record["test_runs"]] == recorded
    assert [(row["variant"], row["grade"]) for row in simulation_record["test_runs"][:2]] == [
        ("short", "pass"),
        ("soak", "pass"),
    ]
    for name in recorded:
        assert name in result.outcome.report_text
    nested = json.loads(
        next((tmp_path / "reports").rglob("coverage-record/coverage.json")).read_text()
    )
    assert [run["test_run"] for run in nested["tests"]["runs"]] == recorded


@pytest.mark.parametrize("tests", [(), ("gap",), ("gap@1",)])
def test_default_coverage_runs_keep_recording_evidence(tmp_path, monkeypatch, tests):
    _prepare_sim_custom_project(tmp_path, monkeypatch)
    result = _coverage(tmp_path, _SplitMetricsExecution(), target="sim_custom", test=tests or None)

    assert "diagnostic_test_runs" not in result.outcome.detail
    assert list((tmp_path / "reports").rglob("simulation.json"))


def test_flow_fails_when_one_of_several_runs_of_a_test_fails(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = _coverage(tmp_path, _FailingRun("reset+soak@1"), test=("reset", "reset+soak"))

    assert result.exit_code == 1, result.outcome
    rows = result.outcome.detail["records"]["sim_0"]["test_runs"]
    assert [(row["name"], row["grade"]) for row in rows] == [
        ("reset+short@1", "pass"),
        ("reset+soak@1", "fail"),
    ]
    assert "booley flow sim --target sim_0 --test=reset+soak@1" in result.outcome.report_text


def test_coverage_refuses_more_test_runs_than_one_request_allows(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    execution = NativeExecution()

    result = _coverage(tmp_path, execution, test=("wrap@x1000", "reset"))

    assert result.exit_code != 0
    assert f"more than {MAX_TEST_RUNS_PER_REQUEST} Test Runs" in result.outcome.report_text
    assert execution.runs == []


_MULTI_RUN = ("reset+short", "reset+soak", "wrap@x3")


class _InterruptedAfterFirstRun(NativeExecution):
    """The simulator process dies while the second Test Run is running."""

    def run(self, request):
        if self.runs:
            raise KeyboardInterrupt("simulated process interruption")
        return super().run(request)


def _resumable_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The multi-Variant Project at one fixed revision, as resume requires."""
    _pin_revision(monkeypatch, "c" * 40)
    _project(tmp_path, monkeypatch)


def _recorded_runs(manifest: Path) -> list[dict]:
    (item,) = json.loads(manifest.read_bytes())["work_items"]
    return item["test_runs"]


def _forbid_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resumed record must never resolve or draw Test Runs again."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a resumed record resolved its Test Runs again")

    # A deterministic re-resolution gives the same Test Runs; only this patch shows resume never resolves again.
    monkeypatch.setattr(SimulateFlow, "_resolve_test_runs", refuse)
    monkeypatch.setattr("booley.flows.test_runs.secrets.SystemRandom", refuse)


def test_interrupted_multi_run_collection_names_its_frozen_test_runs_for_the_new_run(
    tmp_path, monkeypatch
):
    """A collection cut after its first run is not resumed; its seeds are not lost.

    Resuming an unfinished coverage collection is refused by design (it would
    re-run every Test Run). The refusal's command names the frozen Test Runs,
    so the new run repeats the drawn seeds instead of drawing three others.
    """
    _resumable_project(tmp_path, monkeypatch)
    reports = tmp_path / "reports"
    execution = _InterruptedAfterFirstRun()
    with pytest.raises(KeyboardInterrupt):
        _coverage(tmp_path, execution, test=_MULTI_RUN)
    assert len(execution.runs) == 1
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    frozen = manifest.read_bytes()
    names = [run["name"] for run in _recorded_runs(manifest)]
    assert names[:2] == ["reset+short@1", "reset+soak@1"] and len(set(names)) == 5
    _forbid_resolution(monkeypatch)

    resumed = _coverage(tmp_path, resume_from=manifest)

    assert resumed.exit_code == 2, resumed.outcome.report_text
    assert manifest.read_bytes() == frozen
    command = next(
        line.strip()
        for line in resumed.outcome.report_text.splitlines()
        if line.strip().startswith("booley flow sim")
    )
    assert command == (
        "booley flow sim --target sim_0 "
        + " ".join(f"--test={name}" for name in names)
        + " --coverage"
    )
    # The named command resolves to exactly the frozen Test Runs: nothing is drawn.
    replayed = load_target_test_plan(
        tmp_path, "sim_0", identity="sim_0", adapter="verilator"
    ).resolve(names, draw=lambda *_args, **_kwargs: pytest.fail("a seed was drawn again"))
    assert json.dumps([test_run_document(run) for run in replayed], sort_keys=True) == (
        json.dumps(_recorded_runs(manifest), sort_keys=True)
    )


def test_resumed_multi_run_coverage_replays_the_frozen_test_runs_byte_identically(
    tmp_path, monkeypatch
):
    """A collected record cut during publication resumes from its own manifest."""
    _resumable_project(tmp_path, monkeypatch)
    reports = tmp_path / "reports"
    execution = NativeExecution()
    armed = [True]

    def crash(boundary):
        if armed[0] and boundary == "before:summary_replace":
            armed[0] = False
            raise OSError("injected crash before:summary_replace")

    interrupted = _coverage(tmp_path, execution, checkpoint=crash, test=_MULTI_RUN)
    assert interrupted.exit_code == 2
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    frozen = manifest.read_bytes()
    recorded = _recorded_runs(manifest)
    names = [run["name"] for run in recorded]
    launched = [request.test.run_name for request in execution.runs]
    assert launched == names and len(names) == 5
    _forbid_resolution(monkeypatch)

    resumed = _coverage(tmp_path, execution, resume_from=manifest)

    assert resumed.exit_code == 0, resumed.outcome.report_text
    assert manifest.read_bytes() == frozen
    # Nothing ran again, and every published record names the frozen Test Runs.
    assert [request.test.run_name for request in execution.runs] == launched
    rows = resumed.outcome.detail["records"]["sim_0"]["test_runs"]
    assert [{key: row[key] for key in recorded[0]} for row in rows] == recorded
    nested = json.loads(next(reports.rglob("coverage-record/coverage.json")).read_text())
    assert [(run["test_run"], run["fingerprint"]) for run in nested["tests"]["runs"]] == [
        (run["name"], run["fingerprint"]) for run in recorded
    ]
    projection = json.loads((reports / "sim/1/targets/sim_0/simulation.json").read_text())
    assert [row["name"] for row in projection["test_runs"]] == names
