"""ADR 0066 end to end: a recorded Waiver Candidate becomes an Approved Waiver.

Chains the real pieces on one Ticket-shaped Git checkout: a Simulation Campaign
fails a mandatory 70% line Criterion (1/2 points); the Analyst's recording seam
stores a candidate for the uncovered point; disposition turns provisional; the
Human accepts it; approval records strict evidence; the Acceptance Journal
promotes the waiver in its merge candidate; and the destination's real Approved
Waiver Set then passes the persisted Campaign strictly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_reference import resolve_persisted_coverage_campaign_reference
from booley.flows.sim.coverage_waiver_application import strict_reevaluation
from booley.flows.sim.coverage_waivers import CoverageRepositoryRoots, load_approved_waiver_set
from booley.specialists.coverage_candidate_recording import (
    TicketCandidateContext,
    record_ticket_candidates,
)
from booley.ticket_board import review_preparation as prep
from booley.ticket_board import waiver_approval as approval
from booley.ticket_board.acceptance_journal import (
    AcceptanceOutcome,
    AcceptanceRequest,
    advance_acceptance,
)
from booley.ticket_board.acceptance_ledger import freeze_acceptance
from booley.ticket_board.criteria_acceptance import check_criteria_acceptance
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.provisional_coverage import (
    describe_waiver_candidates,
    evaluate_ticket_provisional_coverage,
)
from booley.ticket_board.ticket_baseline import BasisParticipant
from booley.ticket_board.waiver_approval import (
    WaiverDecisions,
    apply_waiver_decisions,
    promotion_plan_for_completion,
    read_promotion_plan,
)
from booley.ticket_board.waiver_approval_transaction import apply_transaction
from tests.ticket_board.test_completion import _contract
from tests.ticket_board.test_provisional_coverage import (
    _line_reference,
    _metric_key,
    ticket,  # noqa: F401 — pytest fixture used by campaign_ticket
)

_APPROVER = "Ada Reviewer <ada@example.test>"
_IDENTITY = {"generation": "d" * 32, "authored_sha256": "e" * 64}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _commit_project(root: Path) -> str:
    """Version the checkout; runtime Ticket state stays out of Git."""
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.eol", "lf")
    (root / ".git" / "info" / "exclude").write_text(
        "logs/\ntickets/\nstate.json\n.runtime/\n.booley_project/tickets/\n"
    )
    (root / ".booley_project" / "booley.toml").write_text(
        '[coverage.waivers]\nanchor = "rtl_repository"\ndirectory = "coverage-waivers"\n'
    )
    (root / "coverage-waivers").mkdir()
    (root / "coverage-waivers" / ".keep").write_text("")
    _git(root, "add", "-A")
    _git(root, "rm", "--cached", "-q", "coverage-waivers/.keep")
    (root / "coverage-waivers" / ".keep").unlink()
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "branch", "ticket-branch")
    return _git(root, "rev-parse", "HEAD")


def _record_analyst_candidate(state_path: Path, context) -> None:
    """What the Coverage Analyst's non-model code records after screening."""
    reference = _line_reference(state_path)
    resolved = resolve_persisted_coverage_campaign_reference(context.flow_reports_root, reference)
    campaign = resolved.loaded.campaign
    point = next(
        item
        for item in campaign.points
        if item.identity.metric == "line" and not sum(item.hits_by_run.values())
    )
    source = str(point.identity.location["source"])
    sha = {row["path"]: row["sha256"] for row in campaign.source_closure["rtl"]}[source]
    section = record_ticket_candidates(
        TicketCandidateContext(context.slug, context.tickets_dir, context.flow_reports_root),
        campaign,
        resolved.loaded.summary,
        resolved.reference_path,
        [
            {
                "point_id": point.id,
                "reason": "unreachable",
                "evidence": "Line 4 sits behind a tied-off reset branch.",
                "proof_reference": "",
                "source_fingerprint": sha,
                "screening": "ready_for_human_review",
            }
        ],
        invocation_id="7",
    )
    assert section["status"] == "recorded", section
    assert section["verdicts"]["provisional"] in {"pass", "fail"}


@pytest.fixture
def campaign_ticket(ticket):  # noqa: F811 — wraps the imported fixture
    state_path, context = ticket
    runtime_state = context.log_dir / ".runtime" / "booley_state.json"
    runtime_state.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.write_bytes(state_path.read_bytes())
    return runtime_state, context


def test_candidate_to_approved_waiver_on_the_destination(
    campaign_ticket, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, context = campaign_ticket
    context = replace(context, tickets_dir=tickets_dir_from_project_root(context.worktree))
    base = _commit_project(context.worktree)
    _record_analyst_candidate(state_path, context)

    def judge(state, unmet):
        return evaluate_ticket_provisional_coverage(state, unmet, context).met_keys

    assert check_criteria_acceptance(state_path, work_dir=context.worktree).disposition == "failed"
    provisional = check_criteria_acceptance(
        state_path, work_dir=context.worktree, provisional=judge
    )
    assert provisional.disposition == "review" and provisional.provisional
    plan = _approve_candidate(state_path, context, monkeypatch)
    _freeze_and_promote(state_path, context, base, plan)
    _assert_destination_coverage(state_path, context, plan)


def _approve_candidate(state_path, context, monkeypatch):
    views, _ = describe_waiver_candidates(DevelopmentState.load(state_path).criteria, context)
    (offered,) = [view.candidate.candidate_id for view in views if view.status == "offered"]
    monkeypatch.setattr(approval, "approver_identity", lambda _root: _APPROVER)
    review = _review_context(state_path, context)
    inputs = approval._ApprovalInputs(
        slug=context.slug,
        log_dir=context.log_dir,
        state_path=state_path,
        worktree=context.worktree,
        project_root=context.worktree,
        context=context,
        inspection=review.inspection,
    )
    decisions = WaiverDecisions(frozenset({offered}))

    def stage(staging):
        staged = replace(
            inputs,
            state_path=staging / "log" / ".runtime" / "booley_state.json",
            log_dir=staging / "log",
            context=replace(context, tickets_dir=staging / "tickets"),
        )
        return apply_waiver_decisions(staged, decisions, merge=True)

    assert apply_transaction(review, decisions, stage)
    plan = read_promotion_plan(context.log_dir)
    assert plan is not None
    return plan


def _review_context(state_path, context):
    ticket_path = context.worktree / "tickets" / "ticket.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_bytes(b"Synthetic coverage Ticket.\n")
    inspection = {
        "generation": "f" * 32,
        "ticket_generation": _IDENTITY["generation"],
        "execution_id": "exec-1",
        "ticket_identity": _IDENTITY,
        "heads": {"outer": _git(context.worktree, "rev-parse", "HEAD")},
        "disposition": "unaccepted",
        "reason": "provisional coverage",
        "state": json.loads(state_path.read_text()),
    }
    review = SimpleNamespace(
        worktree=context.worktree,
        project_root=context.worktree,
        project_repository=None,
        log_dir=context.log_dir,
        slug=context.slug,
        ticket_path=ticket_path,
        inspection=inspection,
    )
    inspection["capture_sha"] = prep._source_fingerprint(review)
    return review


def _freeze_and_promote(state_path, context, base, plan):
    root = context.worktree
    assert check_criteria_acceptance(state_path, work_dir=root).disposition == "review"
    freeze_acceptance(
        context.log_dir,
        DevelopmentState.load(state_path),
        execution_id="exec-1",
        ticket_identity=_IDENTITY,
        participant_heads={"outer": base},
    )
    assert promotion_plan_for_completion(context.log_dir) == plan
    participant = BasisParticipant(
        "outer", base, "refs/heads/ticket-branch", "refs/heads/main", base
    )
    request = AcceptanceRequest(
        root=root,
        slug=context.slug,
        basis=_contract(root, (participant,)),
        cleanup=False,
        ticket_status="review",
        waiver_promotion=plan,
    )
    assert advance_acceptance(request).outcome is AcceptanceOutcome.APPROVAL_REQUIRED
    assert _git(root, "log", "-1", "--format=%s", "main") == (
        f"chore({context.slug}): approve coverage waivers"
    )
    assert _git(root, "rev-parse", "ticket-branch") == base


def _assert_destination_coverage(state_path, context, plan):
    root = context.worktree
    _git(root, "checkout", "-q", "main")
    waivers = load_approved_waiver_set(
        plan.config,
        CoverageRepositoryRoots(
            rtl_repository=root, project_data_repository=root / ".booley_project"
        ),
        (DurableTargetIdentity(plan.candidates[0].target),),
    )
    resolved = resolve_persisted_coverage_campaign_reference(
        context.flow_reports_root, _line_reference_any(state_path)
    )
    evaluated = strict_reevaluation(resolved.loaded.campaign, waivers)
    line = next(row for row in evaluated.evaluation["metrics"] if row["metric"] == "line")
    assert line["verdict"] == "pass"
    state = DevelopmentState.load(state_path)
    assert state.criteria[_metric_key(state, "line")].met is True


def _line_reference_any(state_path: Path) -> dict:
    """The line Criterion's Campaign reference, whether or not it is met."""
    state = DevelopmentState.load(state_path)
    return state.criteria[_metric_key(state, "line")].detail["coverage_campaign_reference"]
