"""The MCP composition root passes one binding to built-in and Project Flows."""

import json
import sys

import pytest

from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.paths import record_paths
from booley.mcp import goal_flow_runner
from tests.goals.conftest import bind


@pytest.fixture(autouse=True)
def preview(monkeypatch):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")


@pytest.fixture
def admitted(goal_mode, monkeypatch):
    binding = bind(goal_mode)
    monkeypatch.setenv("BOOLEY_GOAL_RUN_BINDING", json.dumps(binding.to_json()))
    return binding


@pytest.mark.parametrize(
    ("name", "flow_type"),
    [
        ("lint", LintFlow),
        ("sim", SimulateFlow),
        ("fpga", FpgaImplFlow),
        ("synth", AsicSynthesizeFlow),
    ],
)
def test_builtin_runner_forwards_args_and_admitted_adapter(admitted, monkeypatch, name, flow_type):
    def run(self, argv, *, adapter):
        assert argv == ["--target", "top"]
        assert isinstance(adapter, GoalFlowExecution)
        assert adapter.binding == admitted
        assert (
            adapter.state_file == record_paths(admitted.project_dir, admitted.record_id).state_file
        )
        return 7

    monkeypatch.setattr(flow_type, "main", run)
    monkeypatch.setattr(sys, "argv", ["goal-runner", name, "--target", "top"])
    assert goal_flow_runner.main() == 7


def test_project_runner_records_under_the_admitted_goal(admitted, tmp_path):
    from booley.criteria.state import DevelopmentState

    module = tmp_path / "project_flow.py"
    module.write_text("""from booley.flows.base import BooleyFlow
from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import state_persistence_for
class ProjectFlow(BooleyFlow):
    name = "project-check"
    def _add_args(self, parser):
        parser.add_argument("--source")
    def _run(self):
        raise AssertionError("unused by this mechanical fixture")
    def main(self, argv):
        assert argv == ["--source", "rtl.v"]
        adapter = self.execution_adapter
        state = DevelopmentState.load(adapter.state_file, state_persistence_for(adapter))
        changes = state.set_criterion("lint_clean_top", True, detail={"warnings": 0})
        adapter.record_changes(state, changes, invocation_id="ignored", producer=self.name)
        state.save()
        return 0
""")
    assert (
        goal_flow_runner.main(["--custom-path", str(module), "project-check", "--source", "rtl.v"])
        == 0
    )
    state = DevelopmentState.load(
        record_paths(admitted.project_dir, admitted.record_id).state_file
    )
    assert state.slug == admitted.record_id
    assert state.criteria["lint_clean_top"].met


@pytest.mark.parametrize("argv", [[], ["unknown"], ["--custom-path"]])
def test_runner_refuses_unsupported_invocation(admitted, capsys, argv):
    assert goal_flow_runner.main(argv) == 2
    assert "supported Flow name" in capsys.readouterr().err


@pytest.mark.parametrize("raw", [None, "{not-json", "{}"])
def test_runner_refuses_missing_or_invalid_binding(goal_mode, monkeypatch, capsys, raw):
    if raw is None:
        monkeypatch.delenv("BOOLEY_GOAL_RUN_BINDING", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_GOAL_RUN_BINDING", raw)
    assert goal_flow_runner.main(["lint"]) == 2
    assert "binding" in capsys.readouterr().err


@pytest.mark.parametrize("exists", [False, True])
def test_project_runner_reports_missing_or_unmatched_flow(admitted, tmp_path, capsys, exists):
    module = tmp_path / "missing.py"
    if exists:
        module.write_text("# No named Flow class\n")
    assert goal_flow_runner.main(["--custom-path", str(module), "custom"]) == 2
    assert "project Flow" in capsys.readouterr().err


def test_runner_reports_execution_io_failure(admitted, monkeypatch, capsys):
    def denied(self, argv, *, adapter):
        raise PermissionError("cannot create report directory")

    monkeypatch.setattr(LintFlow, "main", denied)
    assert goal_flow_runner.main(["lint"]) == 2
    assert "cannot create report directory" in capsys.readouterr().err
