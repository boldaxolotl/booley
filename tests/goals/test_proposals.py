"""Public proposal decisions, immutable derivations and durable recovery contracts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from booley.criteria.evidence_ledger import (
    AcceptanceLedgerError,
    replay_projection,
    validated_evidence_records,
)
from booley.criteria.state import DevelopmentState
from booley.criteria.templates import cycle_count_criterion_key
from booley.flows.execution_persistence import EvidenceDiscarded
from booley.goals.apply import ChangeEnvironment, recover
from booley.goals.apply_barrier import pending_applies
from booley.goals.binding import GoalBindingError, bind_run
from booley.goals.change_service import create_proposal, record_decision, resume_proposal
from booley.goals.changes import Approval, read_change_log
from booley.goals.entry import EntryEnvironment
from booley.goals.format import render_status
from booley.goals.paths import record_paths
from booley.goals.proposals import ProposalError, list_proposals, load_proposal, proposal_path
from booley.goals.recorder import GOAL_SCOPE, GoalEvidenceRecorder, GoalProjection
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from tests.goals.conftest import LINT_KEY, enter_goals, git

CYCLE_ARG = {
    "family": "cycle_count",
    "target": "top",
    "test": "smoke",
    "thresholds": {"cycle_count_max": 1},
}
CYCLE_KEY = cycle_count_criterion_key("top", "smoke")


class Crash(BaseException):
    """Kill a transaction without unwinding it into a product rollback."""


def interrupted_transaction(layout, env, view, *, answer="approve", boundary=None):
    def crash(at):
        if at == (boundary or ("approved" if answer == "approve" else "rejected")):
            raise Crash(at)

    with pytest.raises(Crash):
        approve(layout, replace(env, on_boundary=crash), view.proposal.id, answer=answer)
    root = record_paths(layout.control, layout.record.id).root
    saved = load_proposal(root, view.proposal.id)
    path = root / "applies" / f"{saved.decision.transaction_id}.json"
    return path, json.loads(path.read_bytes())


@pytest.mark.parametrize("attack", ["traversal", "unknown", "record", "state", "ledger"])
def test_restart_rejects_unauthorized_destinations_before_any_publication(layout, attack):
    env = setup_cycle(layout)
    publish(layout)
    path, transaction = interrupted_transaction(layout, env, proposal(layout, env))
    root = record_paths(layout.control, layout.record.id).root
    effect = next(row for row in transaction["effects"] if row["label"] == "ledger")
    victim = root.parent / "victim"
    if attack == "traversal":
        effect["path"] = str(root / ".." / "victim")
    elif attack == "unknown":
        effect["label"] = "unrecognized"
        effect["path"] = str(root / "victim")
    else:
        effect["label"] = attack
        effect["path"] = str(root / "victim")
    path.write_text(json.dumps(transaction))
    before = {item: item.read_bytes() for item in root.rglob("*") if item.is_file()}
    for _retry in range(2):
        with pytest.raises(ProposalError):
            recover(env.store, layout.record.id, env)
    assert before == {item: item.read_bytes() for item in root.rglob("*") if item.is_file()}
    assert not victim.exists() and not (root / "victim").exists()
    assert not root.joinpath("changes.jsonl").exists()


def setup_cycle(layout: SimpleNamespace) -> ChangeEnvironment:
    layout.record = enter_goals(layout, [CYCLE_ARG, {"family": "lint", "target": "top"}])
    return ChangeEnvironment(EntryEnvironment(layout.control))


def publish(
    layout: SimpleNamespace,
    *,
    healthy: bool = True,
    cycles: int | None = 100,
    sibling: bool = True,
):
    recorder = GoalEvidenceRecorder(
        bind_run(GoalStore(layout.control), layout.worktree, str(uuid4()))
    )
    state = DevelopmentState.load(
        record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
    )
    detail = {"cycles": cycles, "cycle_observation": "observed", "test": "smoke"}
    if not healthy:
        detail["reason"] = "current test did not pass"
    changes = state.set_criterion(CYCLE_KEY, False, detail=detail)
    if sibling:
        changes.extend(state.set_criterion(LINT_KEY, True, detail={"warnings": 0}))
    recorder.record_changes(state, changes, invocation_id="test", producer="simulation_campaign")
    state.save()
    return recorder, state


def proposal(layout: SimpleNamespace, env: ChangeEnvironment, *, maximum: int = 200):
    return create_proposal(
        env,
        layout.record.id,
        {
            "kind": "relax",
            "goal_key": CYCLE_KEY,
            "after": {**CYCLE_ARG, "thresholds": {"cycle_count_max": maximum}},
            "rationale": "the measured workload requires a larger bound",
        },
        session_key="test-session",
    )


def approve(
    layout: SimpleNamespace, env: ChangeEnvironment, proposal_id: str, *, answer="approve"
):
    return record_decision(
        env,
        layout.record.id,
        proposal_id,
        answer=answer,
        reason="Measured behavior is acceptable",
        source=Approval.AGENT_RECORDED,
        quote="I approve this exact change",
        session_key="test-session",
    )


def observations(layout: SimpleNamespace):
    store = GoalStore(layout.control)
    record = store.load(layout.record.id)
    state = load_goal_state(store, record)
    return validated_evidence_records(
        GOAL_SCOPE, record_paths(layout.control, record.id).logs_dir, state, {}
    )


def test_fresh_failed_bound_derives_success_and_preserves_sibling_and_provenance(layout):
    env = setup_cycle(layout)
    old_recorder, old_state = publish(layout)
    before = observations(layout)
    view = proposal(layout, env)
    assert view.proposal.runtime_params["cycle_count_max"] == 200
    assert approve(layout, env, view.proposal.id).state == "applied"
    record = env.store.load(layout.record.id)
    state = load_goal_state(env.store, record)
    status = build_status(env.store, record)
    assert [goal.status for goal in status.goals] == ["met", "met"]
    after = observations(layout)
    derived = after[-2:]
    assert all(row["producer"] == "approved_goal_change" for row in derived)
    for row in derived:
        source = next(old for old in before if old["criterion"] == row["criterion"])
        assert row["recorded_at"] == source["recorded_at"]
        assert row["detail"]["_source_fingerprint"] == source["detail"]["_source_fingerprint"]
        assert (
            row["detail"]["goal_derivation"]["source_evidence"][0]["sequence"]
            == source["sequence"]
        )
    log = read_change_log(record_paths(layout.control, record.id).changes_file)
    assert len(log.applied) == 1 and not log.interrupted
    assert log.applied[0].reason == "Measured behavior is acceptable"
    assert log.applied[0].quote == "I approve this exact change"
    assert log.applied[0].transaction_ref is not None
    old_state.set_criterion(CYCLE_KEY, True, detail={"cycles": 100})
    with pytest.raises(EvidenceDiscarded, match="changed"):
        old_state.save()
    with pytest.raises(AcceptanceLedgerError, match="specification revision"):
        replay_projection(
            GOAL_SCOPE,
            GoalProjection(env.store, record.id),
            old_recorder.log_dir,
            old_state,
            old_recorder.identity_for([CYCLE_KEY, LINT_KEY]),
        )
    assert load_goal_state(env.store, record).criteria[CYCLE_KEY].params["cycle_count_max"] == 200
    assert state.criteria[LINT_KEY].met


@pytest.mark.parametrize("defect", ["failed", "missing", "source", "target", "absent", "foreign"])
def test_invalid_or_stale_evidence_cannot_become_success(layout, defect):
    env = setup_cycle(layout)
    publish(layout, healthy=defect != "failed", cycles=None if defect == "missing" else 100)
    if defect == "source":
        (layout.worktree / "rtl.v").write_text("module top; wire changed; endmodule\n")
    elif defect == "target":
        path = layout.worktree / "top.core"
        path.write_bytes(
            path.read_bytes() + b"parameters:\n  width: {datatype: int, default: 2}\n"
        )
    elif defect == "absent":
        root = record_paths(layout.control, layout.record.id).logs_dir / "acceptance" / "evidence"
        for path in root.glob("*/record.json"):
            path.unlink()
        for path in root.glob("[0-9]*"):
            path.rmdir()
    elif defect == "foreign":
        for path in (
            record_paths(layout.control, layout.record.id).logs_dir / "acceptance" / "evidence"
        ).glob("*/record.json"):
            row = json.loads(path.read_bytes())
            row["goal_identity"]["record_id"] = "foreign-20261006T120000Z"
            path.write_text(json.dumps(row))
    view = proposal(layout, env)
    approve(layout, env, view.proposal.id)
    state = load_goal_state(env.store, env.store.load(layout.record.id))
    assert not state.criteria[CYCLE_KEY].met


@pytest.mark.parametrize(
    "boundary",
    [
        "prepared",
        "approved",
        "intent",
        "effect:record:0",
        "effect:ledger:1",
        "effect:ledger:2",
        "effect:state:3",
        "applied",
        "finalized",
    ],
)
def test_restart_at_every_durable_boundary_is_idempotent_and_status_is_read_only(layout, boundary):
    env = setup_cycle(layout)
    recorder, old_state = publish(layout)
    view = proposal(layout, env)

    def crash(at):
        if at == boundary:
            raise Crash(at)

    crashing = replace(env, on_boundary=crash)
    with pytest.raises(Crash):
        approve(layout, crashing, view.proposal.id)
    root = record_paths(layout.control, layout.record.id).root
    images = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    report = render_status((build_status(env.store, env.store.load(layout.record.id)),))
    assert view.proposal.id in report
    assert images == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    if boundary not in {"prepared", "finalized"}:
        assert view.proposal.id in pending_applies(root)
        with pytest.raises(GoalBindingError, match="apply interrupted"):
            bind_run(env.store, layout.worktree, "during")
        with pytest.raises(EvidenceDiscarded, match="apply interrupted"):
            old_state.save()
        with pytest.raises(AcceptanceLedgerError, match="apply interrupted"):
            recorder.projection().require_current(recorder.identity_for([LINT_KEY]))
    if boundary == "prepared":
        assert load_proposal(root, view.proposal.id).state == "pending"
        approve(layout, env, view.proposal.id)
    else:
        recover(env.store, layout.record.id, env)
    committed = env.store.load(layout.record.id)
    evidence_count = len(observations(layout))
    recover(env.store, layout.record.id, env)
    assert env.store.load(layout.record.id) == committed
    assert len(observations(layout)) == evidence_count == 4
    assert load_proposal(root, view.proposal.id).state == "applied"
    assert len(read_change_log(root / "changes.jsonl").applied) == 1
    assert not pending_applies(root)


def test_recovery_refuses_third_party_destination_bytes_and_finishes_after_restore(layout):
    env = setup_cycle(layout)
    publish(layout)
    view = proposal(layout, env)

    def crash(at):
        if at == "intent":
            raise Crash(at)

    with pytest.raises(Crash):
        approve(layout, replace(env, on_boundary=crash), view.proposal.id)
    path = record_paths(layout.control, layout.record.id).state_file
    before = path.read_bytes()
    path.write_bytes(b"external edit")
    with pytest.raises(ProposalError, match="recovery conflict"):
        recover(env.store, layout.record.id, env)
    assert path.read_bytes() == b"external edit"
    path.write_bytes(before)
    (layout.worktree / "rtl.v").write_text("module top; wire drift; endmodule\n")
    recover(env.store, layout.record.id, env)
    assert build_status(env.store, env.store.load(layout.record.id)).goals[0].status == "stale"


def test_competing_saved_proposals_can_be_rejected_and_terminal_decisions_do_not_replay(layout):
    env = setup_cycle(layout)
    first = proposal(layout, env)
    second = proposal(layout, env, maximum=300)
    approve(layout, env, first.proposal.id)
    with pytest.raises(ProposalError, match="changed since"):
        approve(layout, env, second.proposal.id)
    assert approve(layout, env, second.proposal.id, answer="reject").state == "rejected"
    for view in (first, second):
        with pytest.raises(ProposalError, match="already decided"):
            approve(layout, env, view.proposal.id)
    status = build_status(env.store, env.store.load(layout.record.id))
    assert status.pending_proposals == 0
    assert len(list_proposals(record_paths(layout.control, layout.record.id).root)) == 2
    assert (
        len(read_change_log(record_paths(layout.control, layout.record.id).changes_file).applied)
        == 1
    )


def test_payload_substitution_is_refused(layout):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    path = (
        proposal_path(record_paths(layout.control, layout.record.id).root, view.proposal.id)
        / "payload.json"
    )
    raw = json.loads(path.read_bytes())
    raw["rationale"] = "substituted"
    path.write_text(json.dumps(raw))
    with pytest.raises(ProposalError, match="substituted"):
        resume_proposal(env, layout.record.id, view.proposal.id)


def test_torn_change_log_tail_recovers_and_new_evidence_before_intent_is_preserved(layout):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    publish(layout)
    root = record_paths(layout.control, layout.record.id).root
    (root / "changes.jsonl").write_bytes(b'{"torn":')
    approve(layout, env, view.proposal.id)
    assert len(observations(layout)) == 4
    assert load_goal_state(env.store, env.store.load(layout.record.id)).criteria[LINT_KEY].met


def test_two_proposals_and_recoverers_serialize(layout):
    env = setup_cycle(layout)
    publish(layout)
    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(lambda maximum: proposal(layout, env, maximum=maximum), (200, 300)))

    def decide_one(view):
        try:
            return approve(layout, env, view.proposal.id).state
        except ProposalError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide_one, views))
    assert sorted(results) == ["applied", "conflict"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recover(env.store, layout.record.id, env), range(2)))
    assert results[0] == results[1]


def test_add_preserves_existing_identity_groups_and_starts_unmet(goal_mode):
    env = ChangeEnvironment(EntryEnvironment(goal_mode.control))
    recorder = GoalEvidenceRecorder(bind_run(env.store, goal_mode.worktree, "lint"))
    state = DevelopmentState.load(
        record_paths(goal_mode.control, goal_mode.record.id).state_file,
        recorder.state_persistence(),
    )
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    recorder.record_changes(state, changes, invocation_id="lint", producer="lint")
    state.save()
    view = create_proposal(
        env,
        goal_mode.record.id,
        {
            "kind": "add",
            "after": {"family": "elab", "target": "top"},
            "rationale": "also validate elaboration",
        },
        session_key=None,
    )
    approve(goal_mode, env, view.proposal.id)
    statuses = {
        goal.key: goal.status
        for goal in build_status(env.store, env.store.load(goal_mode.record.id)).goals
    }
    assert statuses[LINT_KEY] == "met" and statuses["elab_pass_top"] == "unmet"


def test_retarget_away_and_back_allocates_new_spec_and_never_inherits_evidence(layout):
    env = setup_cycle(layout)
    _recorder, state = publish(layout)
    revisions = []
    for target in ("base", "top"):
        record = env.store.load(layout.record.id)
        old = next(goal.spec for goal in record.goals if goal.spec.family.value == "cycle_count")
        view = create_proposal(
            env,
            record.id,
            {
                "kind": "retarget",
                "goal_key": old.key,
                "after": {**CYCLE_ARG, "target": target},
                "rationale": "use another binding",
            },
            session_key=None,
        )
        approve(layout, env, view.proposal.id)
        changed = next(
            goal
            for goal in env.store.load(record.id).goals
            if goal.spec.family.value == "cycle_count"
        )
        revisions.append(changed.spec_revision)
        assert (
            not load_goal_state(env.store, env.store.load(record.id))
            .criteria[changed.spec.key]
            .met
        )
    assert revisions[0] > 1 and revisions[1] > revisions[0]
    state.set_criterion(CYCLE_KEY, True, detail={"cycles": 100})
    with pytest.raises(EvidenceDiscarded, match="changed"):
        state.save()


@pytest.mark.parametrize(
    "after",
    [
        {**CYCLE_ARG, "test": "other", "thresholds": {"cycle_count_max": 200}},
        {**CYCLE_ARG, "target": "base", "thresholds": {"cycle_count_max": 200}},
        {**CYCLE_ARG, "thresholds": {"cycle_count_reduce_at_least": "10%"}},
        {**CYCLE_ARG, "thresholds": {"cycle_count_max": 0}},
        {"family": "lint", "target": "top"},
    ],
)
def test_incomparable_mutations_are_refused(layout, after):
    env = setup_cycle(layout)
    with pytest.raises(ValueError):
        create_proposal(
            env,
            layout.record.id,
            {"kind": "relax", "goal_key": CYCLE_KEY, "after": after, "rationale": "change"},
            session_key=None,
        )


def test_add_relative_cycle_after_head_advances_pins_original_base(goal_mode):
    env = ChangeEnvironment(EntryEnvironment(goal_mode.control))
    (goal_mode.worktree / "new.v").write_text("module new; endmodule\n")
    git(goal_mode.worktree, "add", "new.v")
    git(goal_mode.worktree, "commit", "-m", "advance candidate")
    view = create_proposal(
        env,
        goal_mode.record.id,
        {
            "kind": "add",
            "after": {**CYCLE_ARG, "thresholds": {"cycle_count_reduce_at_least": "10%"}},
            "rationale": "compare original base",
        },
        session_key=None,
    )
    assert view.proposal.runtime_params["_baseline_ref"] == goal_mode.record.base_sha


def test_pending_proposal_cannot_survive_specification_aba(layout):
    env = setup_cycle(layout)
    pending = proposal(layout, env)
    for target in ("base", "top"):
        current = env.store.load(layout.record.id)
        old = next(item.spec for item in current.goals if item.spec.family.value == "cycle_count")
        view = create_proposal(
            env,
            current.id,
            {
                "kind": "retarget",
                "goal_key": old.key,
                "after": {**CYCLE_ARG, "target": target},
                "rationale": "another binding",
            },
            session_key=None,
        )
        approve(layout, env, view.proposal.id)
    with pytest.raises(ProposalError, match="revision changed"):
        approve(layout, env, pending.proposal.id)
    assert approve(layout, env, pending.proposal.id, answer="reject").state == "rejected"


def test_clean_to_done_uses_original_terminal_receipt_with_open_findings(layout, monkeypatch):
    env, clean, detail = publish_open_review(layout, monkeypatch)
    key = "review_rtl_bugs_clean"
    view = create_proposal(
        env,
        layout.record.id,
        {
            "kind": "relax",
            "goal_key": key,
            "after": {**clean, "verdict": "done"},
            "rationale": "accept advisory findings",
        },
        session_key=None,
    )
    approve(layout, env, view.proposal.id)
    current = env.store.load(layout.record.id)
    assert build_status(env.store, current).goals[0].status == "met"
    final = load_goal_state(env.store, current).criteria["review_rtl_bugs_done"]
    assert final.detail["receipt_id"] == detail["receipt_id"]
    assert final.detail["issue_list"] == detail["issue_list"]
    with pytest.raises(ProposalError):
        create_proposal(
            env,
            current.id,
            {"kind": "add", "after": clean, "rationale": "duplicate review slot"},
            session_key=None,
        )
    with pytest.raises(ProposalError):
        create_proposal(
            env,
            current.id,
            {
                "kind": "relax",
                "goal_key": "review_rtl_bugs_done",
                "after": clean,
                "rationale": "reverse order",
            },
            session_key=None,
        )


def publish_open_review(layout, monkeypatch):
    from booley.evidence.review_receipt import (
        ReviewInvocation,
        build_review_contract_detail,
        finalize_review_detail,
    )
    from booley.goals.freshness import stamp_goal_detail

    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    clean = {"family": "review", "review": "rtl_bugs", "verdict": "clean"}
    layout.record = enter_goals(layout, [clean])
    env = ChangeEnvironment(EntryEnvironment(layout.control))
    key = "review_rtl_bugs_clean"
    goal = layout.record.goals[0].spec
    contract = build_review_contract_detail(
        ReviewInvocation(layout.worktree, "rtl", "bugs", ("rtl.v",), "clean")
    )
    detail = stamp_goal_detail(
        key,
        {
            "review_detail_version": 4,
            "contract": contract,
            "issue_list": [{"severity": "minor", "title": "open finding"}],
            "issues": 1,
        },
        work_dir=layout.worktree,
        goal=goal,
    )
    detail = finalize_review_detail(detail, detail["_source_fingerprint"])
    recorder = GoalEvidenceRecorder(bind_run(env.store, layout.worktree, "review"))
    state = DevelopmentState.load(
        record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
    )
    changes = state.set_criterion(key, False, detail=detail)
    recorder.record_changes(state, changes, invocation_id="review", producer="reviewer")
    state.save()
    return env, clean, detail


@pytest.mark.parametrize("file", ["payload.json", "state.json"])
def test_interrupted_proposal_creation_never_publishes_an_incomplete_pair(
    layout, monkeypatch, file
):
    from booley.goals import proposals as module

    env = setup_cycle(layout)
    original = module.atomic_write_once

    def interrupted(path, content, **kwargs):
        result = original(path, content, **kwargs)
        if path.name == file:
            raise Crash(file)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write_once", interrupted)
        with pytest.raises(Crash):
            proposal(layout, env)
    root = record_paths(layout.control, layout.record.id).root
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert list_proposals(root) == ()
    assert build_status(env.store, env.store.load(layout.record.id)).pending_proposals == 0
    assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    recover(env.store, layout.record.id, env)
    view = proposal(layout, env)
    assert len(list_proposals(root)) == 1
    assert approve(layout, env, view.proposal.id).state == "applied"


def test_missing_published_decision_metadata_never_resets_a_saved_decision(layout):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    approve(layout, env, view.proposal.id, answer="reject")
    root = record_paths(layout.control, layout.record.id).root
    (proposal_path(root, view.proposal.id) / "state.json").unlink()
    with pytest.raises(ProposalError, match="cannot read"):
        load_proposal(root, view.proposal.id)
