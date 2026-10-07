"""The Goal Flow execution adapter: admission, state path, and shared persistence (D5, B8)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.criteria.state import DevelopmentState
from booley.evidence.acceptance import PairedProjectBaseline, ResolvedFlowAcceptance
from booley.flows.execution_persistence import FlowExecutionAdapter, state_persistence_for
from booley.flows.request import FlowRequest
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.paths import record_paths
from booley.goals.state_store import GoalStatePersistence
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.goals.conftest import LINT_KEY, bind


def test_adapter_admits_the_goal_worktree_and_points_at_the_goal_state(
    goal_mode: SimpleNamespace,
) -> None:
    adapter: FlowExecutionAdapter = GoalFlowExecution(bind(goal_mode))
    request = FlowRequest(target="top", work_dir=goal_mode.worktree / "docs")

    resolved = adapter.validate_and_resolve(request)

    assert resolved == ResolvedFlowAcceptance(paired_project=PairedProjectBaseline.absent())
    assert request.state_file == record_paths(goal_mode.control, goal_mode.record.id).state_file
    assert request.slug == goal_mode.record.id


def test_adapter_blocks_a_request_for_another_worktree(
    goal_mode: SimpleNamespace, tmp_path: Path
) -> None:
    adapter = GoalFlowExecution(bind(goal_mode))

    outcome = adapter.validate_and_resolve(FlowRequest(target="top", work_dir=goal_mode.main))

    assert isinstance(outcome, EndpointOutcome)
    assert "is not the Goal worktree" in outcome.report_text


def test_endpoint_state_saves_through_the_adapters_persistence(
    goal_mode: SimpleNamespace,
) -> None:
    adapter = GoalFlowExecution(bind(goal_mode))
    persistence = state_persistence_for(adapter)
    assert isinstance(persistence, GoalStatePersistence)

    state = DevelopmentState.load(adapter.state_file, persistence)
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    adapter.record_changes(state, changes, invocation_id="lint-1", producer="lint")
    state.save()

    saved = DevelopmentState.load(adapter.state_file)
    assert saved.criteria[LINT_KEY].met is True
    assert (adapter.binding.project_dir / "goals" / goal_mode.record.id / "logs").is_dir()


def test_source_target_policy_uses_bound_goal_then_retains_unrelated_fallback(goal_mode):
    from tests.goals.conftest import bump_spec

    adapter = GoalFlowExecution(bind(goal_mode))
    assert adapter.criterion_source_target(LINT_KEY, "producer-target") == "top"
    assert adapter.criterion_source_target("custom_gate", "producer-target") == "producer-target"
    bump_spec(goal_mode, LINT_KEY)
    assert adapter.criterion_source_target(LINT_KEY, "producer-target") == "producer-target"
    state = DevelopmentState.load(adapter.state_file, adapter.state_persistence())
    assert adapter.criterion_is_current(state, LINT_KEY) is False


def test_missing_record_defers_source_policy_failure_to_publication(goal_mode):
    from booley.flows.execution_persistence import EvidenceDiscarded

    adapter = GoalFlowExecution(bind(goal_mode))
    state = DevelopmentState.load(adapter.state_file, adapter.state_persistence())
    record_paths(goal_mode.control, goal_mode.record.id).record_file.unlink()
    assert adapter.criterion_source_target(LINT_KEY, "producer-target") == "producer-target"
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    with pytest.raises(EvidenceDiscarded, match="record cannot be read"):
        adapter.record_changes(state, changes, invocation_id="ignored", producer="lint")
