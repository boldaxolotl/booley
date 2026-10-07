"""Mutation changes preserve implicit producer floors and original observations."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from mcp import Client

from booley.criteria.state import DevelopmentState
from booley.goals.apply import ChangeEnvironment
from booley.goals.binding import bind_run
from booley.goals.change_policy import translate_after, validate_kind
from booley.goals.change_service import create_proposal
from booley.goals.changes import ChangeKind
from booley.goals.entry import EntryEnvironment
from booley.goals.paths import record_paths
from booley.goals.proposals import ProposalError
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from tests.goals.conftest import enter_goals
from tests.goals.test_proposal_wire import sdk
from tests.goals.test_proposals import observations

MUTATION = {"family": "mutation", "target": "top", "scope": ["rtl.v"]}
KEY = "mutation_score_top"


def publish_mutation(layout, argument):
    layout.record = enter_goals(layout, [argument])
    store = GoalStore(layout.control)
    recorder = GoalEvidenceRecorder(bind_run(store, layout.worktree, str(uuid4())))
    path = record_paths(layout.control, layout.record.id).state_file
    state = DevelopmentState.load(path, recorder.state_persistence())
    changes = state.set_criterion(
        KEY,
        False,
        detail={
            "detected": 8,
            "not_detected": 2,
            "invalid": 0,
            "min_detected": 10,
            "total_requested": 10,
            "total_valid": 10,
        },
    )
    recorder.record_changes(state, changes, invocation_id="original", producer="mutation_tester")
    state.save()
    return store, observations(layout)[0]


async def approve_through_modern_client(layout, monkeypatch, after):
    async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
        response = await client.call_tool(
            "goal_propose_change",
            {
                "work_dir": str(layout.worktree),
                "operation": "create",
                "kind": "relax",
                "goal_key": KEY,
                "after": after,
                "rationale": "accept eight detections from the existing run",
            },
        )
        assert not response.is_error, response.content
        saved = json.loads(response.content[0].text)
        response = await client.call_tool(
            "goal_propose_change",
            {
                "work_dir": str(layout.worktree),
                "operation": "approve",
                "proposal_id": saved["proposal_id"],
                "reason": "existing mutation score is acceptable",
                "approval_quote": "Approve eight detections for this fixture",
            },
        )
        assert not response.is_error, response.content
        assert json.loads(response.content[0].text)["state"] == "applied"


@pytest.mark.parametrize("explicit_total", [False, True])
def test_modern_mutation_relaxation_uses_implicit_floor_and_existing_evidence(
    layout, monkeypatch, explicit_total
):
    argument = {**MUTATION, **({"total": 10} if explicit_total else {})}
    store, original = publish_mutation(layout, argument)
    asyncio.run(
        approve_through_modern_client(layout, monkeypatch, {**argument, "min_detected": 8})
    )
    record = store.load(layout.record.id)
    status = build_status(store, record).goals[0]
    assert status.status == "met" and "need 8" in status.evidence_summary
    rows = observations(layout)
    assert len(rows) == 2 and rows[0] == original
    derived = rows[-1]
    assert derived["producer"] == "approved_goal_change" and derived["met"] is True
    assert derived["recorded_at"] == original["recorded_at"]
    assert derived["detail"]["_source_fingerprint"] == original["detail"]["_source_fingerprint"]
    assert (
        derived["detail"]["goal_derivation"]["source_evidence"][0]["sequence"]
        == original["sequence"]
    )
    assert sum(row["producer"] == "mutation_tester" for row in rows) == 1


@pytest.mark.parametrize("after_floor", [None, 9])
def test_mutation_relaxation_refuses_stricter_explicit_or_implicit_floor(layout, after_floor):
    before = {**MUTATION, "total": 10, "min_detected": 8}
    layout.record = enter_goals(layout, [before])
    after = {**MUTATION, "total": 10, **({"min_detected": after_floor} if after_floor else {})}
    env = ChangeEnvironment(EntryEnvironment(layout.control))
    with pytest.raises(ProposalError, match="weaken"):
        create_proposal(
            env,
            layout.record.id,
            {"kind": "relax", "goal_key": KEY, "after": after, "rationale": "stricter"},
            session_key=None,
        )


def test_unresolved_auto_mutation_floor_is_a_policy_error_not_an_ordering_error():
    before = translate_after({**MUTATION, "auto": True})
    after = translate_after({**MUTATION, "auto": True, "min_detected": 8})
    with pytest.raises(ProposalError, match="auto"):
        validate_kind(ChangeKind.RELAX, before, after)


def test_retarget_preserves_implicit_auto_mutation_acceptance_policy():
    before = translate_after({**MUTATION, "auto": True})
    after = translate_after({**MUTATION, "auto": True, "target": "base"})
    validate_kind(ChangeKind.RETARGET, before, after)
