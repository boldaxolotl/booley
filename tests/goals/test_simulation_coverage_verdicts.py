"""Goal recording keeps simulation truth independent of coverage evaluation."""

from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.goals.entry import EntryRequest, enter_goal_mode
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.model import parse_goal_args
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.goals.conftest import bind, entry_environment, git


@pytest.mark.parametrize("simulation", ["pass", "fail"])
@pytest.mark.parametrize("coverage", ["pass", "fail"])
def test_bound_flow_records_both_verdicts(layout, monkeypatch, simulation, coverage):
    root: Path = layout.worktree
    project(root)
    core = root / "counter.core"
    core.write_text(core.read_text().replace("sim_0", "top"))
    (root / ".booley_project/tests.toml").write_text('[top]\ntests = ["reset", "wrap"]\n')
    git(root, "add", "counter.core", "rtl/counter.sv")
    git(root, "commit", "-m", "simulation recipe")
    enter_goal_mode(
        EntryRequest(
            work_dir=root,
            slug="verdicts",
            goals=parse_goal_args(
                [
                    {"family": "sim", "target": "top"},
                    {
                        "family": "coverage",
                        "target": "top",
                        "tests": "all",
                        "metrics": {"line": 100},
                    },
                ]
            ),
        ),
        entry_environment(layout),
    )
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
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
    # A coverage Goal is judged by its frozen Test Runs too (ADR 0072): coverage
    # collected from a failing Test Run does not meet it.
    assert saved.criteria["coverage_top"].met is (coverage == "pass" and simulation == "pass")
    if coverage == "pass" and simulation == "fail":
        assert (
            saved.criteria["coverage_top"]
            .detail["goal_contract_violation"]
            .startswith("Test Runs failed")
        )
    assert result.exit_code == (0 if simulation == "pass" else 1), result.outcome
    assert len(saved.acceptance_transactions) == 1


def _enter(layout, monkeypatch, goals):
    """A coverage-capable ``top`` Target with Tests ``reset`` and ``wrap``, in Goal Mode."""
    root: Path = layout.worktree
    project(root)
    core = root / "counter.core"
    core.write_text(core.read_text().replace("sim_0", "top"))
    (root / ".booley_project/tests.toml").write_text('[top]\ntests = ["reset", "wrap"]\n')
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


def test_multi_seed_coverage_goal_is_met_only_by_a_campaign_of_exactly_its_frozen_seeds(
    layout, monkeypatch
):
    goals = [
        {"family": "coverage", "target": "top", "tests": ["reset@x2"], "metrics": {"line": 100}}
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
    assert "have not passed yet" in entry.detail["goal_contract_violation"]

    # ``reset@x2`` names the Goal's Target, Test Variant and N: it runs the frozen seeds.
    result, state = _coverage_run(layout, root, "frozen-seeds", "reset@x2")
    assert result.exit_code == 0, result.outcome
    entry = state.criteria["coverage_top"]
    assert entry.met is True
    assert [row["name"] for row in entry.detail["test_runs"]] == frozen
    outcomes = [run["outcome"] for run in entry.detail["test_run_ledger"]["runs"]]
    assert outcomes == ["passed", "passed"]
    assert "match no Goal" not in result.outcome.report_text


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
    (root / ".booley_project/tests.toml").write_text('[top]\ntests = ["reset", "wrap"]\n')
    git(root, "add", "counter.core", "rtl/counter.sv")
    git(root, "commit", "-m", "simulation recipe")
    record = enter_goal_mode(
        EntryRequest(
            work_dir=root,
            slug="killed",
            goals=parse_goal_args([{"family": "sim", "target": "top"}]),
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
