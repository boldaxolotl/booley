"""Recovery publishes only the complete transaction bound by separate authority."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from booley.goals.apply import recover
from booley.goals.change_service import resume_proposal
from booley.goals.changes import read_change_log
from booley.goals.paths import record_paths
from booley.goals.proposals import ProposalError, digest, load_proposal, proposal_path
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status
from tests.goals.test_goal_waivers import prepared, propose
from tests.goals.test_proposals import (
    CYCLE_KEY,
    approve,
    interrupted_transaction,
    observations,
    proposal,
    publish,
    setup_cycle,
)


def snapshot(root):
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def authority_files(root, proposal_id):
    directory = proposal_path(root, proposal_id)
    return {
        path: path.read_bytes() for path in (directory / "payload.json", directory / "state.json")
    }


def require_untouched_conflict(layout, env, view, path, changed, *, match="recovery conflict"):
    root = record_paths(layout.control, layout.record.id).root
    authority = authority_files(root, view.proposal.id)
    path.write_text(json.dumps(changed))
    before = snapshot(root)
    for _retry in range(2):
        with pytest.raises(ProposalError, match=match):
            recover(env.store, layout.record.id, env)
    assert before == snapshot(root)
    assert authority == authority_files(root, view.proposal.id)
    assert load_proposal(root, view.proposal.id).applied_at is None


@pytest.mark.parametrize("deleted", ["all", "record", "state", "ledger-first", "ledger-last"])
@pytest.mark.parametrize("boundary", ["approved", "intent"])
def test_deleted_captured_effects_never_clear_the_apply_barrier(layout, deleted, boundary):
    env = setup_cycle(layout)
    publish(layout)
    view = proposal(layout, env)
    path, captured = interrupted_transaction(layout, env, view, boundary=boundary)
    original = json.loads(path.read_bytes())
    delete_effect(captured, deleted)
    require_untouched_conflict(layout, env, view, path, captured)
    path.write_text(json.dumps(original))
    recover(env.store, layout.record.id, env)
    assert resume_proposal(env, layout.record.id, view.proposal.id).state == "applied"


def delete_effect(captured, deleted):
    if deleted == "all":
        captured["effects"] = []
        return
    label = deleted.split("-")[0]
    indexes = [
        index for index, effect in enumerate(captured["effects"]) if effect["label"] == label
    ]
    captured["effects"].pop(indexes[-1] if deleted.endswith("-last") else indexes[0])


def test_allowed_state_afterimage_cannot_turn_failed_producer_evidence_into_success(layout):
    env = setup_cycle(layout)
    publish(layout, cycles=100)
    view = proposal(layout, env, maximum=20)
    path, captured = interrupted_transaction(layout, env, view)
    effect = next(effect for effect in captured["effects"] if effect["label"] == "state")
    state = json.loads(base64.b64decode(effect["after"]))
    assert state["criteria"][CYCLE_KEY]["met"] is False
    state["criteria"][CYCLE_KEY]["met"] = True
    effect["after"] = base64.b64encode(json.dumps(state).encode()).decode()
    require_untouched_conflict(
        layout, env, view, path, captured, match="durable decision authority"
    )
    saved = load_goal_state(env.store, env.store.load(layout.record.id))
    assert not saved.criteria[CYCLE_KEY].met
    assert not next(row for row in observations(layout) if row["criterion"] == CYCLE_KEY)["met"]
    assert not read_change_log(record_paths(layout.control, layout.record.id).changes_file).applied


@pytest.mark.parametrize(
    "field",
    ["reason", "quote", "after", "transaction_ref", "session_key", "peer_process", "approval"],
)
def test_substituted_transaction_audit_is_not_the_approved_change(layout, field):
    env = setup_cycle(layout)
    publish(layout)
    view = proposal(layout, env)
    path, captured = interrupted_transaction(layout, env, view)
    change_entry(captured["entry"], field)
    require_untouched_conflict(layout, env, view, path, captured, match="ChangeEntry differs")


def change_entry(entry, field):
    if field == "after":
        entry[field]["params"]["cycle_count_max"] = 300
    elif field == "transaction_ref":
        entry[field] = str(uuid4())
    elif field == "approval":
        entry[field] = "elicited"
    else:
        entry[field] = "substituted provenance"


@pytest.mark.parametrize("boundary", ["intent", "effect:state:3", "applied"])
@pytest.mark.parametrize("field", ["reason", "quote", "after", "transaction_ref"])
def test_existing_intent_must_equal_complete_captured_entry_before_recovery(
    layout, boundary, field
):
    env = setup_cycle(layout)
    publish(layout)
    view = proposal(layout, env)
    interrupted_transaction(layout, env, view, boundary=boundary)
    path = record_paths(layout.control, layout.record.id).changes_file
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    original = path.read_bytes()
    change_entry(rows[0]["entry"], field)
    authority = authority_files(path.parent, view.proposal.id)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = snapshot(path.parent)
    with pytest.raises(ProposalError, match="persisted Change Log entry differs"):
        recover(env.store, layout.record.id, env)
    assert before == snapshot(path.parent)
    assert authority == authority_files(path.parent, view.proposal.id)
    path.write_bytes(original)
    recover(env.store, layout.record.id, env)
    assert resume_proposal(env, layout.record.id, view.proposal.id).state == "applied"


@pytest.mark.parametrize("deleted", ["all", "waiver-first", "waiver-last", "candidate"])
def test_waiver_approval_and_rejection_effects_are_bound_in_full(layout, deleted):
    env, candidate, _root = prepared(layout)
    view = propose(layout, env, candidate)
    answer = "reject" if deleted == "candidate" else "approve"
    path, captured = interrupted_transaction(layout, env, view, answer=answer)
    delete_effect(captured, deleted)
    require_untouched_conflict(layout, env, view, path, captured)


def test_digest_covers_complete_canonical_body_without_self_reference(layout):
    env = setup_cycle(layout)
    publish(layout)
    view = proposal(layout, env)
    path, captured = interrupted_transaction(layout, env, view)
    saved = load_proposal(path.parent.parent, view.proposal.id)
    assert saved.transaction_digest == digest(captured)
    assert (
        "transaction_digest" not in captured and "transaction_digest" not in captured["decision"]
    )
    assert saved.metadata()["decision"] == captured["decision"]
    path.write_text(json.dumps(captured, indent=4))
    recover(env.store, layout.record.id, env)
    assert (
        resume_proposal(env, layout.record.id, view.proposal.id).transaction_digest
        == saved.transaction_digest
    )


@pytest.mark.parametrize("answer", ["approve", "reject"])
def test_terminal_legacy_metadata_without_authority_remains_readable(layout, answer):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    approve(layout, env, view.proposal.id, answer=answer)
    root = record_paths(layout.control, layout.record.id).root
    path = proposal_path(root, view.proposal.id) / "state.json"
    legacy = json.loads(path.read_bytes())
    legacy.pop("transaction_digest")
    path.write_text(json.dumps(legacy))
    before = snapshot(root)
    assert load_proposal(root, view.proposal.id).transaction_digest is None
    recover(env.store, layout.record.id, env)
    assert before == snapshot(root)
    assert resume_proposal(env, layout.record.id, view.proposal.id).state == (
        "applied" if answer == "approve" else "rejected"
    )


def test_unresolved_legacy_capture_without_separate_authority_fails_closed(layout):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    path, _captured = interrupted_transaction(layout, env, view, boundary="intent")
    root = path.parent.parent
    metadata = proposal_path(root, view.proposal.id) / "state.json"
    raw = json.loads(metadata.read_bytes())
    raw.pop("transaction_digest")
    metadata.write_text(json.dumps(raw))
    before = snapshot(root)
    with pytest.raises(ProposalError, match="durable decision authority"):
        recover(env.store, layout.record.id, env)
    assert before == snapshot(root)


@pytest.mark.parametrize("field", ["met", "params", "detail"])
@pytest.mark.parametrize("met", [False, True])
def test_status_never_credits_projection_values_that_differ_from_selected_observation(
    layout, field, met
):
    env = setup_cycle(layout)
    publish(layout)
    approve(layout, env, proposal(layout, env, maximum=200 if met else 20).proposal.id)
    paths = record_paths(layout.control, layout.record.id)
    state = json.loads(paths.state_file.read_bytes())
    entry = state["criteria"][CYCLE_KEY]
    assert entry["met"] is met
    if field == "met":
        entry["met"] = not met
    elif field == "params":
        entry["params"]["cycle_count_max"] = 1000
    else:
        entry["detail"]["cycles"] = 1
    paths.state_file.write_text(json.dumps(state))
    status = build_status(env.store, env.store.load(layout.record.id)).goals[0]
    assert status.status != "met"
    assert "selected immutable producer observation" in status.reason


@pytest.mark.parametrize("goal_receipt", [False, True])
def test_reviewer_report_keeps_goal_receipt_immutable_even_when_result_aliases_state(
    tmp_path, monkeypatch, goal_receipt
):
    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.mcp.base import McpToolResult
    from booley.specialists.reviewer import ReviewerSpecialist

    endpoint = ReviewerSpecialist()
    endpoint._acceptance_current = False if goal_receipt else None
    endpoint._args = SimpleNamespace(diagnostic=False)
    detail = {
        "audit_evidence": "original-receipt.json",
        "filtered": [{"reason": "excluded", "evidence": ""}],
        "rejected": [],
        "artifacts": {"reviewer_evidence": "original-receipt.json"},
    }
    before = deepcopy(detail)
    entry = CriterionEntry(detail=detail)
    endpoint._state = DevelopmentState.in_memory()
    endpoint.state.criteria["review_done_rtl_bugs"] = entry
    monkeypatch.setattr(endpoint.state, "save", MagicMock())
    endpoint._evidence_path = lambda label: tmp_path / f"{label}.json"
    result = McpToolResult(exit_code=0, report_text="Review complete", detail=detail)
    returned = endpoint._publish_review_evidence(result, "review_done_rtl_bugs")
    assert str(tmp_path / "result.json") in returned.report_text
    assert returned.detail["artifacts"]["reviewer_evidence"] == str(tmp_path / "result.json")
    assert json.loads((tmp_path / "result.json").read_text())["detail"] == returned.detail
    if goal_receipt:
        assert entry.detail == before
        endpoint.state.save.assert_not_called()
    else:
        assert entry.detail["audit_evidence"] == str(tmp_path / "result.json")
        endpoint.state.save.assert_called_once()
