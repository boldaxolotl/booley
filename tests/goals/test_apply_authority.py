"""Recovery publishes only the complete transaction bound by separate authority."""

from __future__ import annotations

import base64
import json
import shutil
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


@pytest.mark.parametrize("boundary", ["approved", "intent"])
def test_deleted_captured_effects_never_clear_the_apply_barrier(layout, boundary):
    env = setup_cycle(layout)
    publish(layout)
    view = proposal(layout, env)
    path, captured = interrupted_transaction(layout, env, view, boundary=boundary)
    original = json.loads(path.read_bytes())
    captured["effects"] = []
    require_untouched_conflict(layout, env, view, path, captured)
    path.write_text(json.dumps(original))
    recover(env.store, layout.record.id, env)
    assert resume_proposal(env, layout.record.id, view.proposal.id).state == "applied"


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
    require_untouched_conflict(
        layout, env, view, path, captured, match="durable decision authority"
    )


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


@pytest.mark.parametrize("answer", ["approve", "reject"])
def test_waiver_approval_and_rejection_effects_are_bound_in_full(layout, answer):
    env, candidate, _root = prepared(layout)
    view = propose(layout, env, candidate)
    path, captured = interrupted_transaction(layout, env, view, answer=answer)
    captured["effects"] = []
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
def test_decided_proposal_without_transaction_digest_is_an_invalid_record(layout, answer):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    approve(layout, env, view.proposal.id, answer=answer)
    root = record_paths(layout.control, layout.record.id).root
    directory = proposal_path(root, view.proposal.id)
    state = json.loads((directory / "state.json").read_bytes())
    state.pop("transaction_digest")
    (directory / "state.json").write_text(json.dumps(state))
    with pytest.raises(
        ProposalError,
        match=r"cannot read proposal .*: invalid prepared transaction authority digest",
    ) as raised:
        load_proposal(root, view.proposal.id)
    assert raised.value.path == directory


def test_interrupted_apply_without_captured_transaction_is_an_invalid_record(layout):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    path, _captured = interrupted_transaction(layout, env, view, boundary="intent")
    root = path.parent.parent
    metadata = proposal_path(root, view.proposal.id) / "state.json"
    raw = json.loads(metadata.read_bytes())
    raw.pop("transaction_digest")
    metadata.write_text(json.dumps(raw))
    before = snapshot(root)
    with pytest.raises(ProposalError, match="invalid prepared transaction authority digest"):
        recover(env.store, layout.record.id, env)
    assert before == snapshot(root)


def test_interrupted_intent_without_its_proposal_is_an_invalid_record(layout):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    path, _captured = interrupted_transaction(layout, env, view, boundary="intent")
    root = path.parent.parent
    shutil.rmtree(proposal_path(root, view.proposal.id))
    with pytest.raises(
        ProposalError, match="Change Log intent names no prepared Goal apply"
    ) as raised:
        recover(env.store, layout.record.id, env)
    assert raised.value.path == record_paths(layout.control, layout.record.id).changes_file


@pytest.mark.parametrize(
    "met, field",
    [
        pytest.param(False, "met", marks=pytest.mark.timeout(120), id="False-met"),
        pytest.param(False, "params", id="False-params"),
        pytest.param(False, "detail", id="False-detail"),
        pytest.param(True, "met", id="True-met"),
        pytest.param(True, "params", id="True-params"),
        pytest.param(True, "detail", id="True-detail"),
    ],
)
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
    immutable = {path: path.read_bytes() for path in paths.logs_dir.rglob("record.json")}
    paths.state_file.write_text(json.dumps(state))
    status = build_status(env.store, env.store.load(layout.record.id)).goals[0]
    assert status.status == ("met" if met else "unmet")
    recovered = load_goal_state(env.store, env.store.load(layout.record.id))
    assert recovered.criteria[CYCLE_KEY].met is met
    assert recovered.criteria[CYCLE_KEY].params["cycle_count_max"] == (200 if met else 20)
    assert recovered.criteria[CYCLE_KEY].detail["cycles"] != 1
    assert {path: path.read_bytes() for path in immutable} == immutable


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
    endpoint._review_evidence_path = tmp_path / "result.json"
    endpoint._review_evidence_written = False
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
