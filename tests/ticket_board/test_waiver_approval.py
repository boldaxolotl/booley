"""Per-candidate waiver decisions at approval (ADR 0066), on a real Campaign.

The fixture's mandatory 70% line Criterion observes 1/2 points; one recorded
candidate for the uncovered line point can make it pass strictly once promoted.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.coverage_campaign import decode_coverage_point_id
from booley.ticket_board import waiver_approval as approval
from booley.ticket_board import waiver_candidates as store
from booley.ticket_board.acceptance_ledger import freeze_acceptance, record_changes
from booley.ticket_board.provisional_coverage import describe_waiver_candidates
from booley.ticket_board.waiver_approval import (
    WaiverDecisionError,
    WaiverDecisions,
    apply_waiver_decisions,
    promotion_plan_for_completion,
    promotion_plan_path,
    read_promotion_plan,
)
from tests.ticket_board.test_provisional_coverage import (
    _metric_key,
    _record_uncovered_line,
    ticket,  # noqa: F401 — pytest fixture
)

_APPROVER = "Ada Reviewer <ada@example.test>"


@pytest.fixture
def approving(ticket, monkeypatch):  # noqa: F811 — the imported fixture
    state_path, context = ticket
    root = context.worktree
    (root / ".booley_project" / "booley.toml").write_text(
        '[coverage.waivers]\nanchor = "rtl_repository"\ndirectory = "coverage-waivers"\n'
    )
    (root / "coverage-waivers").mkdir()
    monkeypatch.setattr(approval, "approver_identity", lambda _root: _APPROVER)
    inputs = approval._ApprovalInputs(
        slug=context.slug,
        log_dir=context.log_dir,
        state_path=state_path,
        worktree=root,
        project_root=root,
        context=context,
        inspection={
            "execution_id": "exec-1",
            "ticket_identity": {"generation": "d" * 32, "authored_sha256": "e" * 64},
            "capture_sha": "c" * 64,
            "state": json.loads(state_path.read_text()),
        },
    )
    return inputs


def _offered_id(inputs) -> str:
    views, _ = describe_waiver_candidates(
        DevelopmentState.load(inputs.state_path).criteria, inputs.context
    )
    (view,) = [item for item in views if item.status == "offered"]
    return view.candidate.candidate_id


def _line_met(inputs) -> bool:
    state = DevelopmentState.load(inputs.state_path)
    return state.criteria[_metric_key(state, "line")].met


def test_accepting_the_needed_candidate_promotes_and_meets_strict_coverage(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    candidate = _offered_id(inputs)

    plan = apply_waiver_decisions(inputs, WaiverDecisions(frozenset({candidate})), merge=True)

    assert plan is not None and plan.waiver_ids == (candidate,)
    assert plan.stamps.approved_by == _APPROVER
    assert plan.stamps.approval_ref == f"ticket:ticket@{'c' * 64}"
    assert read_promotion_plan(inputs.log_dir) == plan
    assert _line_met(inputs) is True
    state = DevelopmentState.load(inputs.state_path)
    detail = state.criteria[_metric_key(state, "line")].detail
    assert detail["waiver_promotion"]["plan_sha256"] == plan.sha256()
    assert detail["evaluation"]["status"] == "fail"  # optional branch still fails overall
    assert not (inputs.worktree / "coverage-waivers" / "rtl").exists()  # nothing tracked yet
    records = list((inputs.log_dir / "acceptance" / "evidence").rglob("record.json"))
    producers = {json.loads(path.read_text())["producer"] for path in records}
    assert "waiver-approval" in producers


def test_rejecting_the_needed_candidate_records_it_and_refuses(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    candidate = _offered_id(inputs)

    with pytest.raises(WaiverDecisionError, match="strict acceptance would still fail"):
        apply_waiver_decisions(
            inputs, WaiverDecisions(rejected=frozenset({candidate})), merge=True
        )

    record = store.load(inputs.context.tickets_dir, inputs.slug)
    assert record.rejections and record.rejections[0].rejected_by == _APPROVER
    assert not promotion_plan_path(inputs.log_dir).exists()
    assert _line_met(inputs) is False


@pytest.mark.parametrize(
    ("decisions", "message"),
    [
        (lambda c: WaiverDecisions(), "undecided"),
        (lambda c: WaiverDecisions(frozenset({c}), frozenset({c})), "accepted and rejected"),
        (lambda c: WaiverDecisions(frozenset({c, "wc-unknown"})), "no such candidate"),
    ],
)
def test_invalid_decisions_change_nothing(approving, decisions, message) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    before = inputs.state_path.read_bytes()
    record_before = store.load(inputs.context.tickets_dir, inputs.slug)

    with pytest.raises(WaiverDecisionError, match=message):
        apply_waiver_decisions(inputs, decisions(_offered_id(inputs)), merge=True)

    assert inputs.state_path.read_bytes() == before
    assert store.load(inputs.context.tickets_dir, inputs.slug) == record_before
    assert not promotion_plan_path(inputs.log_dir).exists()


def test_accepting_without_merge_is_refused(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)

    with pytest.raises(WaiverDecisionError, match="requires merge"):
        apply_waiver_decisions(
            inputs, WaiverDecisions(frozenset({_offered_id(inputs)})), merge=False
        )

    assert not promotion_plan_path(inputs.log_dir).exists()


def test_a_resumed_approval_reuses_its_durable_plan(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    candidate = _offered_id(inputs)
    first = apply_waiver_decisions(inputs, WaiverDecisions(frozenset({candidate})), merge=True)

    again = apply_waiver_decisions(inputs, WaiverDecisions(frozenset({candidate})), merge=True)

    assert again == first
    with pytest.raises(WaiverDecisionError, match="already approved"):
        apply_waiver_decisions(
            inputs, WaiverDecisions(rejected=frozenset({candidate})), merge=True
        )


def test_a_preexisting_plan_does_not_supply_human_decisions(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    views, _ = describe_waiver_candidates(
        DevelopmentState.load(inputs.state_path).criteria, inputs.context
    )
    plan = approval._build_plan(inputs, views, WaiverDecisions(frozenset({_offered_id(inputs)})))
    path = promotion_plan_path(inputs.log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plan.to_bytes())
    before = inputs.state_path.read_bytes()

    with pytest.raises(WaiverDecisionError, match="explicit"):
        apply_waiver_decisions(inputs, WaiverDecisions(), merge=True)

    assert inputs.state_path.read_bytes() == before
    assert not _line_met(inputs)


def test_prediction_errors_still_record_explicit_rejections(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    _record_uncovered_line(inputs.context, inputs.state_path, metric="branch")
    views, _ = describe_waiver_candidates(
        DevelopmentState.load(inputs.state_path).criteria, inputs.context
    )
    ids = {
        decode_coverage_point_id(view.candidate.proposal.point_id)[
            "metric"
        ]: view.candidate.candidate_id
        for view in views
    }
    (inputs.worktree / "coverage-waivers" / "malformed.toml").write_text("schema = [")

    with pytest.raises(WaiverDecisionError, match="cannot be promoted"):
        apply_waiver_decisions(
            inputs,
            WaiverDecisions(frozenset({ids["line"]}), frozenset({ids["branch"]})),
            merge=True,
        )

    record = store.load(inputs.context.tickets_dir, inputs.slug)
    assert len(record.rejections) == 1
    assert {item.candidate_id for item in record.candidates.values()} == {ids["line"]}
    assert not promotion_plan_path(inputs.log_dir).exists()
    assert not _line_met(inputs)


def test_completion_reads_only_the_plan_the_frozen_record_names(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    plan = apply_waiver_decisions(
        inputs, WaiverDecisions(frozenset({_offered_id(inputs)})), merge=True
    )
    freeze_acceptance(
        inputs.log_dir,
        DevelopmentState.load(inputs.state_path),
        execution_id="exec-1",
        ticket_identity=inputs.inspection["ticket_identity"],
        participant_heads={"outer": "a" * 40},
    )

    assert promotion_plan_for_completion(inputs.log_dir) == plan
    promotion_plan_path(inputs.log_dir).unlink()
    with pytest.raises(ValueError, match="disagrees"):
        promotion_plan_for_completion(inputs.log_dir)


def test_current_waiver_approval_freezes_with_selected_foreign_history(approving) -> None:
    inputs = approving
    _record_uncovered_line(inputs.context, inputs.state_path)
    apply_waiver_decisions(inputs, WaiverDecisions(frozenset({_offered_id(inputs)})), merge=True)
    state = DevelopmentState.load(inputs.state_path)
    name = _metric_key(state, "line")
    detail = state.criteria[name].detail
    state.set_criterion(name, False)
    changes = state.set_criterion(name, True, detail=detail)
    old = {**inputs.inspection["ticket_identity"], "generation": "a" * 32}
    record_changes(
        inputs.log_dir,
        state,
        changes,
        invocation_id="historical-waiver",
        producer="waiver-approval",
        execution_id="old-execution",
        ticket_identity=old,
        transaction_id="f" * 64,
    )
    state.acceptance_transactions.append("f" * 64)
    frozen = freeze_acceptance(
        inputs.log_dir,
        state,
        execution_id="exec-1",
        ticket_identity=inputs.inspection["ticket_identity"],
        participant_heads={"outer": "a" * 40},
    )
    records = list((inputs.log_dir / "acceptance/evidence").rglob("record.json"))
    foreign = {
        json.loads(path.read_text())["sequence"]
        for path in records
        if json.loads(path.read_text())["ticket_identity"] == old
    }
    assert foreign
    assert not foreign.intersection(ref["sequence"] for ref in frozen.evidence)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_approver_identity_refuses_the_agent_identity(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / ".booley_project").mkdir()
    (tmp_path / ".booley_project" / "booley.toml").write_text(
        '[agent.git]\nname = "Dev"\nemail = "dev@localhost"\n'
    )
    with pytest.raises(WaiverDecisionError, match=r"user\.name"):
        approval.approver_identity(tmp_path)

    _git(tmp_path, "config", "user.name", "Dev")
    _git(tmp_path, "config", "user.email", "dev@localhost")
    with pytest.raises(WaiverDecisionError, match="Developer Agent"):
        approval.approver_identity(tmp_path)

    _git(tmp_path, "config", "user.name", "Ada")
    _git(tmp_path, "config", "user.email", "ada@example.test")
    assert approval.approver_identity(tmp_path) == "Ada <ada@example.test>"
