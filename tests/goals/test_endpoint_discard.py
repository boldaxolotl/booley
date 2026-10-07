"""Discarded evidence through the endpoint completion path (ADR 0067 B1, D7).

A real endpoint (a custom MCP tool) runs with the Goal Flow execution adapter
and sets a Goal's Criterion in its ``_run``. When the publication gate
refuses, the endpoint catches :class:`EvidenceDiscarded` once, writes nothing
more for the invocation (not even its timeline entry), and returns its own
result prefixed with ``evidence discarded: <reason>``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import EvidenceDiscarded
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.model import GoalState
from booley.goals.paths import record_paths
from booley.mcp.base import McpTool, McpToolResult
from booley.runtime.endpoint_execution import ExecutionResult
from tests.goals.conftest import (
    LINT_KEY,
    bind,
    bump_spec,
    campaign_facts,
    edit_protected,
    tree_digest,
    update_record,
)


class _GoalProbe(McpTool):
    """Sets the lint Goal met after running *during* (a stand-in for the run's work)."""

    name = "goal_probe"
    description = "Synthetic Goal evidence endpoint"
    config_aware = False
    during: ClassVar[Callable[[], None]] = staticmethod(lambda: None)

    def _add_args(self, parser: Any) -> None:
        return

    def _run(self) -> McpToolResult:
        type(self).during()
        self.set_criterion(LINT_KEY, True, detail={"warnings": 0})
        self.state.record_mcp_tool_run("probe-step", 0)
        return McpToolResult(
            exit_code=0, report_text="probe passed", criterion_key=LINT_KEY, criterion_met=True
        )


@pytest.fixture
def endpoint_env(goal_mode: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The environment an endpoint of the Goal Mode runs in."""
    for name in ("BOOLEY_TICKET_FILE", "BOOLEY_LOGS_DIR", "BOOLEY_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    state_file = record_paths(goal_mode.control, goal_mode.record.id).state_file
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    return goal_mode


def _run(layout: SimpleNamespace, during: Callable[[], None]) -> tuple[ExecutionResult, Any]:
    adapter = GoalFlowExecution(bind(layout))
    probe = type("Probe", (_GoalProbe,), {"during": staticmethod(during)})()
    probe.configure_flow_execution(adapter)
    return probe.execute_cli(["--work-dir", str(layout.worktree)]), adapter


def _record_root(layout: SimpleNamespace) -> Path:
    return record_paths(layout.control, layout.record.id).root


def _assert_discarded(result: ExecutionResult, reason: str) -> None:
    outcome = result.outcome
    assert result.exit_code == 0  # the run's own result
    assert outcome.report_text.startswith(f"evidence discarded: {reason}")
    assert outcome.report_text.endswith("probe passed")
    assert outcome.summary.startswith("evidence discarded: ")
    assert outcome.detail["evidence_discarded"].startswith(reason)


def _assert_later_saves_refused(layout: SimpleNamespace, adapter: GoalFlowExecution) -> None:
    """Even with the protected input restored, the invocation's gate stays closed."""
    state = DevelopmentState.load(adapter.state_file, adapter.state_persistence())
    state.record_mcp_tool_run("late", 0)
    with pytest.raises(EvidenceDiscarded):
        state.save()


def test_protected_input_changed_before_execution_is_discarded(
    endpoint_env: SimpleNamespace,
) -> None:
    edit_protected(endpoint_env)
    before = tree_digest(_record_root(endpoint_env))

    result, adapter = _run(endpoint_env, lambda: None)

    _assert_discarded(result, "protected input changed before the run")
    assert tree_digest(_record_root(endpoint_env)) == before
    _assert_later_saves_refused(endpoint_env, adapter)


def test_protected_input_changed_at_the_end_is_discarded(endpoint_env: SimpleNamespace) -> None:
    before = tree_digest(_record_root(endpoint_env))
    original: list[str] = []

    result, adapter = _run(endpoint_env, lambda: original.append(edit_protected(endpoint_env)))

    _assert_discarded(result, "protected input changed during the run")
    assert tree_digest(_record_root(endpoint_env)) == before
    edit_protected(endpoint_env, original[0])
    _assert_later_saves_refused(endpoint_env, adapter)


def test_changed_before_and_restored_before_completion_is_discarded(
    endpoint_env: SimpleNamespace,
) -> None:
    """The start sample (at admission) differed; restoring before the end changes nothing."""
    original = edit_protected(endpoint_env)
    before = tree_digest(_record_root(endpoint_env))

    result, adapter = _run(endpoint_env, lambda: edit_protected(endpoint_env, original))

    _assert_discarded(result, "protected input changed before the run")
    assert tree_digest(_record_root(endpoint_env)) == before
    _assert_later_saves_refused(endpoint_env, adapter)


def test_in_flight_change_and_revert_is_not_caught(endpoint_env: SimpleNamespace) -> None:
    """D7's documented gap: both samples match the record, so the evidence publishes."""

    def change_and_revert() -> None:
        edit_protected(endpoint_env, edit_protected(endpoint_env))

    result, adapter = _run(endpoint_env, change_and_revert)

    assert result.outcome.report_text == "probe passed"
    assert "evidence_discarded" not in result.outcome.detail
    saved = DevelopmentState.load(adapter.state_file)
    assert saved.criteria[LINT_KEY].met is True
    assert [entry["mcp_tool"] for entry in saved.timeline] == ["probe-step", "goal_probe"]


class _CampaignProbe(McpTool):
    """Hands one campaign outcome to the generic completion path, which records it as V2."""

    name = "goal_campaign_probe"
    description = "Synthetic Simulation Campaign endpoint"
    config_aware = False

    def _add_args(self, parser: Any) -> None:
        return

    def _run(self) -> McpToolResult:
        self._simulation_campaign_outcomes = (object(),)
        self.flow = SimpleNamespace(record_campaign_acceptance=self._record_campaign)
        return McpToolResult(exit_code=0, report_text="campaign passed")

    def _record_campaign(self, _outcomes: tuple[object, ...]) -> None:
        shadow = DevelopmentState.from_json_object(self.state.to_dict())
        changes = shadow.set_criterion(LINT_KEY, True, detail={"warnings": 0})
        self._acceptance_recorder.record_or_verify_transaction(
            self.state, changes, acceptance_facts=campaign_facts(), ticket_identity={}
        )


def test_v2_campaign_discard_is_caught_once_at_completion(endpoint_env: SimpleNamespace) -> None:
    adapter = GoalFlowExecution(bind(endpoint_env))
    bump_spec(endpoint_env, LINT_KEY)
    before = tree_digest(_record_root(endpoint_env))
    probe = _CampaignProbe()
    probe.configure_flow_execution(adapter)

    result = probe.execute_cli(["--work-dir", str(endpoint_env.worktree)])

    assert result.exit_code == 0
    assert result.outcome.report_text.startswith(
        f"evidence discarded: Goal {LINT_KEY} changed during the run"
    )
    assert "completion_error" not in result.outcome.detail
    assert tree_digest(_record_root(endpoint_env)) == before
    _assert_later_saves_refused(endpoint_env, adapter)


class _QuietProbe(McpTool):
    """Sets no Criterion; the record closes while it runs."""

    name = "goal_quiet_probe"
    description = "Synthetic endpoint without Criteria"
    config_aware = False
    layout: ClassVar[SimpleNamespace]

    def _add_args(self, parser: Any) -> None:
        return

    def _run(self) -> McpToolResult:
        update_record(type(self).layout, state=GoalState.ABANDONED)
        return McpToolResult(exit_code=0, report_text="quiet passed")


def test_discard_first_met_at_final_timeline_persistence(endpoint_env: SimpleNamespace) -> None:
    adapter = GoalFlowExecution(bind(endpoint_env))
    before = tree_digest(_record_root(endpoint_env))
    probe = type("Quiet", (_QuietProbe,), {"layout": endpoint_env})()
    probe.configure_flow_execution(adapter)

    result = probe.execute_cli(["--work-dir", str(endpoint_env.worktree)])

    assert result.exit_code == 0
    assert result.outcome.report_text.startswith("evidence discarded: Goal Mode abandoned")
    assert result.outcome.report_text.endswith("quiet passed")
    assert "completion_error" not in result.outcome.detail
    after = tree_digest(_record_root(endpoint_env))
    assert {k: v for k, v in after.items() if k != "record.json"} == {
        k: v for k, v in before.items() if k != "record.json"
    }
    _assert_later_saves_refused(endpoint_env, adapter)
