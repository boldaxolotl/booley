"""Goal recording keeps simulation truth independent of coverage evaluation."""

from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.model import parse_goal_args
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.goals.conftest import bind, git


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
        EntryEnvironment(project_dir=layout.control),
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
    assert saved.criteria["coverage_top"].met is (coverage == "pass")
    assert result.exit_code == (0 if simulation == "pass" else 1), result.outcome
    assert len(saved.acceptance_transactions) == 1
