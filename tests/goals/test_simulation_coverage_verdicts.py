"""Goal recording keeps simulation truth independent of coverage evaluation."""

from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.frozen_test_runs import OUTCOMES_ONLY_DETAIL_KEY
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.goals.entry import EntryRequest, enter_goal_mode
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.model import parse_goal_args
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.goals.conftest import bind, entry_environment, git

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)


@pytest.mark.parametrize("simulation", ["pass", "fail"])
@pytest.mark.parametrize("coverage", ["pass", "fail"])
def test_bound_flow_records_both_verdicts(layout, monkeypatch, simulation, coverage):
    root, record = _enter(
        layout,
        monkeypatch,
        [
            {"family": "SIM", "target": "top"},
            {"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 100}},
        ],
    )
    adapter = GoalFlowExecution(bind(layout))
    result = SimulateFlow(
        coverage_execution=lambda *_args: NativeExecution(
            hits=1 if coverage == "pass" else 0,
            verdict=simulation,
        )
    ).execute(
        SimRequest(target="top", work_dir=root, coverage=True, report_dir=root / "reports"),
        adapter=adapter,
    )
    saved = DevelopmentState.load(adapter.state_file)
    assert saved.criteria["sim_pass_top"].met is (simulation == "pass")
    # A coverage Goal is judged by its coverage floors alone: a Test Run that failed
    # its checks is the simulation Goal's concern, not the coverage Goal's.
    entry = saved.criteria["coverage_top"]
    assert entry.met is (coverage == "pass")
    violation = entry.detail.get("goal_contract_violation", "")
    assert "Test Runs failed" not in violation and "have not passed" not in violation
    # The failing runs stay visible in what the Goal shows of its Test Runs.
    view = build_status(GoalStore(layout.control), record, work_dir=root)
    status = next(goal for goal in view.goals if goal.key == "coverage_top")
    assert {run.outcome for run in status.test_runs.runs} == {
        "passed" if simulation == "pass" else "failed"
    }
    # (This substituted campaign records no waiver policy, so met evidence reads stale.)
    assert (status.status == "unmet") is (coverage == "fail"), status
    assert "goal-test-run-" not in status.reason
    assert result.exit_code == (0 if simulation == "pass" else 1), result.outcome
    assert len(saved.acceptance_transactions) == 1


def _enter(layout, monkeypatch, goals):
    """A coverage-capable ``top`` Target with Tests ``reset`` and ``wrap``, in Goal Mode."""
    root: Path = layout.worktree
    project(root)
    core = root / "counter.core"
    core.write_text(core.read_text().replace("sim_0", "top"))
    (root / ".booley/tests.toml").write_text('[top]\ntests = ["reset", "wrap"]\n')
    git(root, "add", "counter.core", "rtl/counter.sv")
    git(root, "commit", "-m", "simulation recipe")
    record = enter_goal_mode(
        EntryRequest(work_dir=root, slug="seeds", goals=parse_goal_args(goals)),
        entry_environment(layout),
    ).record
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    return root, record


def _coverage_run(layout, root, run_id, *tests):
    adapter = GoalFlowExecution(bind(layout, run_id))
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution(hits=1)).execute(
        SimRequest(
            target="top",
            work_dir=root,
            coverage=True,
            report_dir=root / "reports" / run_id,
            test=tests or None,
        ),
        adapter=adapter,
    )
    return result, DevelopmentState.load(adapter.state_file)


@pytest.mark.timeout(120)  # Windows CI needs 3x the observed 30-33s runtime.
def test_multi_seed_coverage_goal_is_met_only_by_a_campaign_of_exactly_its_frozen_seeds(
    layout, monkeypatch
):
    goals = [
        {"family": "COVERAGE", "target": "top", "tests": ["reset@x2"], "metrics": {"line": 100}}
    ]
    root, record = _enter(layout, monkeypatch, goals)
    frozen = [run.name for run in record.test_run_resolutions["coverage_top"].test_runs]
    assert len(set(frozen)) == 2 and all(name.startswith("reset@") for name in frozen)

    # The default seed of the same Test is not one of the frozen seeds: diagnostic.
    result, state = _coverage_run(layout, root, "default-seed", "reset")
    assert state.criteria["coverage_top"].met is False
    assert state.criteria["coverage_top"].detail == {}
    assert "match no Goal" in result.outcome.report_text
    # One frozen seed alone is not the Goal's campaign either: coverage is not merged
    # across campaigns. Its outcome is recorded for the Goal, its coverage is not.
    _result, state = _coverage_run(layout, root, "one-seed", frozen[0])
    entry = state.criteria["coverage_top"]
    assert entry.met is False and "evaluation" not in entry.detail
    assert [row["name"] for row in entry.detail["test_runs"]] == [frozen[0]]
    assert "did not measure the Goal" in entry.detail["goal_contract_violation"]

    # ``reset@x2`` names the Goal's Target, Test Variant and N: it runs the frozen seeds.
    result, state = _coverage_run(layout, root, "frozen-seeds", "reset@x2")
    assert result.exit_code == 0, result.outcome
    entry = state.criteria["coverage_top"]
    assert entry.met is True
    assert [row["name"] for row in entry.detail["test_runs"]] == frozen
    view = build_status(GoalStore(layout.control), record, work_dir=root)
    status = next(goal for goal in view.goals if goal.key == "coverage_top")
    outcomes = [run.outcome for run in status.test_runs.runs]
    assert outcomes == ["passed", "passed"]
    assert "match no Goal" not in result.outcome.report_text


@pytest.mark.parametrize("grade", ["pass", "fail"])
def test_an_ordinary_run_never_changes_a_measured_coverage_verdict(layout, monkeypatch, grade):
    """After the campaign measured the Goal, a frozen run's outcome elsewhere decides nothing."""
    from types import SimpleNamespace

    from tests.goals.conftest import run_row
    from tests.goals.test_test_run_evidence import _record, _shown, _state_file, _status

    goals = [{"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 100}}]
    root, record = _enter(layout, monkeypatch, goals)
    mode = SimpleNamespace(**vars(layout), record=record)
    frozen = record.test_run_resolutions["coverage_top"].test_runs
    result, state = _coverage_run(layout, root, "campaign")
    assert result.exit_code == 0, result.outcome
    measured = state.criteria["coverage_top"].detail
    before = _status(mode, "coverage_top")
    # (This substituted campaign records no waiver policy, so met evidence reads stale.)
    assert before.status != "unmet" and "goal-test-run-" not in before.reason

    # An ordinary simulation at the same file contents runs one frozen Test Run.
    ordinary = _record(
        mode,
        "ordinary",
        [run_row(frozen[0], grade)],
        key="coverage_top",
        met=False,
        extra={OUTCOMES_ONLY_DETAIL_KEY: True},
    )
    assert ordinary["evaluation"] == measured["evaluation"]  # the measurement, restated
    after = _status(mode, "coverage_top")
    assert (after.status, after.reason) == (before.status, before.reason)
    assert DevelopmentState.load(_state_file(mode)).criteria["coverage_top"].met is True
    assert set(_shown(after).values()) == {"passed"}  # the measuring campaign's outcomes


def test_a_published_coverage_campaign_whose_acceptance_never_ran_is_accounted_for(
    layout, monkeypatch
):
    """Collected on its own and published, then the process died: its failing run counts."""
    from dataclasses import replace

    from booley.flows.sim.coverage_invocation import (
        CoverageInvocationRequest,
        prepare_coverage_invocation,
    )
    from booley.flows.sim.coverage_transaction import run_coverage_target
    from booley.flows.sim.unpublished_results import (
        read_unpublished_results,
        started_coverage_campaign,
    )
    from booley.flows.test_runs import build_test_run
    from booley.goals.campaign_accounting import account_for_campaigns
    from booley.goals.recorder import GoalEvidenceRecorder
    from booley.goals.status import build_status
    from booley.goals.store import GoalStore
    from tests.flows.sim.test_coverage_transaction import Progress

    root: Path = layout.worktree
    context = replace(project(root), test_names={"top": ("reset", "wrap")})
    core = root / "counter.core"
    core.write_text(core.read_text().replace("sim_0", "top"))
    (root / ".booley/tests.toml").write_text('[top]\ntests = ["reset", "wrap"]\n')
    git(root, "add", "counter.core", "rtl/counter.sv")
    git(root, "commit", "-m", "simulation recipe")
    record = enter_goal_mode(
        EntryRequest(
            work_dir=root,
            slug="killed",
            goals=parse_goal_args([{"family": "SIM", "target": "top"}]),
        ),
        entry_environment(layout),
    ).record
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    frozen = record.test_run_resolutions["sim_pass_top"].test_runs
    assert [run.name for run in frozen] == ["reset@1", "wrap@1"]
    invocation = root / "reports" / "sim" / "1"
    started = started_coverage_campaign(invocation / "targets" / "top", ("top",))
    GoalEvidenceRecorder(bind(layout, "killed")).register_campaign(started)

    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("top",)), context)
    assert prepared.plan is not None, prepared
    plan = replace(
        prepared.plan.targets[0],
        invocation_dir=invocation,
        test_runs=tuple(build_test_run(name, seed=1) for name in ("reset", "wrap")),
    )
    outcome = run_coverage_target(plan, NativeExecution(verdict="fail"), Progress())
    assert outcome.campaign_path == started.manifest_path  # published; no acceptance ran

    durable = read_unpublished_results(started.manifest_path, started.manifest_sha256)
    assert durable.complete and [(row["name"], row["grade"]) for row in durable.rows] == [
        ("reset@1", "fail"),
        ("wrap@1", "fail"),
    ]
    store = GoalStore(layout.control)
    account_for_campaigns(store, record.id, reader=read_unpublished_results)
    (goal,) = build_status(store, record, work_dir=root).goals
    assert goal.status == "unmet" and goal.reason.startswith("goal-test-run-failed")


def test_unpublished_coverage_results_before_publication_and_of_another_campaign(tmp_path):
    from booley.flows.sim.unpublished_results import (
        read_unpublished_results,
        started_coverage_campaign,
    )

    started = started_coverage_campaign(tmp_path / "targets" / "top", ("top", "lib#top"))
    assert started.candidate and started.manifest_path.name == "coverage.json"
    # Collection has not published yet: no result, and more may come.
    nothing = read_unpublished_results(started.manifest_path, started.manifest_sha256)
    assert (nothing.results, nothing.rows, nothing.complete) == ((), (), False)
    with pytest.raises(ValueError, match="not the one that was registered"):
        read_unpublished_results(started.manifest_path, "sha256:" + "0" * 64)
    started.manifest_path.parent.mkdir(parents=True)
    started.manifest_path.write_bytes(b"{not a campaign")
    with pytest.raises(ValueError, match="cannot be authenticated"):
        read_unpublished_results(started.manifest_path, started.manifest_sha256)
