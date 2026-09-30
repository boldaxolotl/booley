"""Real Campaign approval retries preserve evidence and the inspected inputs."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path, PureWindowsPath

import pytest

from booley.criteria.state import DevelopmentState
from booley.ticket_board import review_preparation as prep
from booley.ticket_board import waiver_approval as approval
from booley.ticket_board import waiver_approval_transaction as transaction
from booley.ticket_board import waiver_candidates as store
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.provisional_coverage import describe_waiver_candidates
from booley.ticket_board.review_records import ReviewEntryError
from booley.ticket_board.waiver_approval import WaiverDecisionError, WaiverDecisions
from tests.ticket_board.test_provisional_coverage import (
    _metric_key,
    ticket,  # noqa: F401 — fixture required by campaign_ticket
)
from tests.ticket_board.test_waiver_candidates_pipeline import (
    _commit_project,
    _record_analyst_candidate,
    _review_context,
    campaign_ticket,  # noqa: F401 — pytest fixture
)


@pytest.fixture
def selected(campaign_ticket, monkeypatch, request):  # noqa: F811 — imported fixture
    state_path, context = campaign_ticket
    context = replace(context, tickets_dir=tickets_dir_from_project_root(context.worktree))
    _commit_project(context.worktree)
    _record_analyst_candidate(state_path, context)
    monkeypatch.setattr(approval, "approver_identity", lambda _: "Ada Reviewer <ada@example.test>")
    review = _review_context(state_path, context)
    views, _ = describe_waiver_candidates(DevelopmentState.load(state_path).criteria, context)
    candidate = next(view.candidate.candidate_id for view in views if view.status == "offered")
    decisions = (
        WaiverDecisions(rejected=frozenset({candidate}))
        if getattr(request, "param", "accept") == "reject"
        else WaiverDecisions(frozenset({candidate}))
    )
    inputs = approval._ApprovalInputs(
        context.slug,
        context.log_dir,
        state_path,
        context.worktree,
        context.worktree,
        context,
        review.inspection,
    )

    def stage(staging):
        return approval.apply_waiver_decisions(
            replace(
                inputs,
                state_path=staging / "log" / ".runtime" / "booley_state.json",
                log_dir=staging / "log",
                context=replace(context, tickets_dir=staging / "tickets"),
            ),
            decisions,
            merge=True,
        )

    return review, decisions, stage


@pytest.mark.parametrize("stop_after", range(7))
def test_retry_recovers_each_partial_publication(selected, monkeypatch, stop_after):
    ctx, decisions, stage = selected
    inspection = json.dumps(ctx.inspection, sort_keys=True)
    write = transaction.atomic_replace_bytes
    calls = 0

    def interrupted(path, content):
        nonlocal calls
        if calls == stop_after:
            raise OSError("power lost during approval")
        calls += 1
        write(path, content)

    monkeypatch.setattr(transaction, "atomic_replace_bytes", interrupted)
    try:
        transaction.apply_transaction(ctx, decisions, stage)
    except OSError as exc:
        assert "power lost" in str(exc)
    assert transaction.retry_capture(ctx)
    monkeypatch.setattr(transaction, "atomic_replace_bytes", write)
    with pytest.raises(WaiverDecisionError, match="explicit"):
        transaction.apply_transaction(ctx, WaiverDecisions(), stage)

    capture = transaction.apply_transaction(ctx, decisions, lambda _: pytest.fail("restaged"))
    assert capture == transaction.retry_capture(ctx)
    state = DevelopmentState.load(ctx.log_dir / ".runtime" / "booley_state.json")
    assert state.criteria[_metric_key(state, "line")].met
    assert json.dumps(ctx.inspection, sort_keys=True) == inspection


@pytest.mark.parametrize("alter", ["state", "evidence", "missing_plan", "timestamp"])
def test_recovery_revalidates_all_transaction_outputs(selected, alter):
    ctx, decisions, stage = selected
    transaction.apply_transaction(ctx, decisions, stage)
    path = transaction._transaction_path(ctx)
    record = json.loads(path.read_text())
    if alter == "missing_plan":
        record["patches"] = [
            p for p in record["patches"] if p["label"] != "acceptance/waiver-promotion.json"
        ]
        record["answers"]["accepted"] = []
    else:
        suffix = "booley_state.json" if alter in {"state", "timestamp"} else "record.json"
        patch = next(p for p in record["patches"] if p["label"].endswith(suffix))
        output = json.loads(base64.b64decode(patch["after"]))
        if alter == "state":
            output["criteria"]["_report_submitted"]["met"] = False
        elif alter == "timestamp":
            output["last_updated"] = "2026-10-01T00:00:00+00:00"
        else:
            output["producer"] = "different-producer"
        content = json.dumps(output).encode()
        patch["after"] = base64.b64encode(content).decode()
        (ctx.log_dir / patch["label"]).write_bytes(content)
    path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match=r"decisions|outputs|projection"):
        transaction.retry_capture(ctx)


def test_retry_rejects_changes_outside_approval(selected):
    ctx, decisions, stage = selected
    transaction.apply_transaction(ctx, decisions, stage)
    report = ctx.log_dir / "REPORT.md"
    report.write_text("Changed outside approval.\n")
    before = report.read_bytes()
    with pytest.raises(ReviewEntryError, match="inputs changed"):
        transaction.retry_capture(ctx)
    assert report.read_bytes() == before


def test_recovery_uses_portable_source_labels(selected, monkeypatch):
    """Windows path rendering must preserve the transaction's inspected fingerprint."""
    ctx, decisions, stage = selected
    relative_to = Path.relative_to

    def windows_relative(path, *args, **kwargs):
        return PureWindowsPath(relative_to(path, *args, **kwargs).as_posix())

    monkeypatch.setattr(Path, "relative_to", windows_relative)
    ctx.inspection["capture_sha"] = prep._source_fingerprint(ctx)
    capture = transaction.apply_transaction(ctx, decisions, stage)

    assert capture == transaction.retry_capture(ctx)


@pytest.mark.parametrize("selected", ["reject"], indirect=True)
@pytest.mark.parametrize("interrupted", [False, True])
def test_rejection_only_transaction_preserves_failure_and_recovers(
    selected, monkeypatch, interrupted
):
    ctx, decisions, stage = selected
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    before = state_path.read_bytes()
    write = transaction.atomic_replace_bytes

    def interrupt(_path, _content):
        raise OSError("power lost before rejection publication")

    if interrupted:
        monkeypatch.setattr(transaction, "atomic_replace_bytes", interrupt)
        with pytest.raises(OSError, match="power lost"):
            transaction.apply_transaction(ctx, decisions, stage)
        monkeypatch.setattr(transaction, "atomic_replace_bytes", write)
    else:
        transaction.apply_transaction(ctx, decisions, stage)
    record = json.loads(transaction._transaction_path(ctx).read_text())
    assert "strict acceptance" in record["error"]
    assert transaction.retry_capture(ctx)
    transaction.apply_transaction(ctx, decisions, lambda _: pytest.fail("restaged"))
    with pytest.raises(WaiverDecisionError, match="strict acceptance"):
        transaction.raise_transaction_error(ctx)
    recorded = store.load(tickets_dir_from_project_root(ctx.project_root), ctx.slug)
    assert not recorded.candidates
    assert len(recorded.rejections) == 1
    assert state_path.read_bytes() == before
    assert not approval.promotion_plan_path(ctx.log_dir).exists()


@pytest.mark.parametrize("selected", ["reject"], indirect=True)
def test_rejection_only_recovery_rejects_mismatched_identity(selected):
    ctx, decisions, stage = selected
    transaction.apply_transaction(ctx, decisions, stage)
    path = transaction._transaction_path(ctx)
    record = json.loads(path.read_text())
    patch = next(p for p in record["patches"] if p["label"] == "waiver-candidates")
    output = json.loads(base64.b64decode(patch["after"]))
    output["rejections"][0]["rejected_by"] = "Different reviewer"
    content = json.dumps(output).encode()
    patch["after"] = base64.b64encode(content).decode()
    transaction._candidate_path(ctx).write_bytes(content)
    path.write_text(json.dumps(record))

    with pytest.raises(ReviewEntryError, match="explicit rejections"):
        transaction.retry_capture(ctx)
