"""Approval retries preserve the inspection and reject unrelated input changes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from booley.ticket_board import review_lifecycle as lifecycle
from booley.ticket_board import waiver_approval_transaction as transaction
from booley.ticket_board.review_records import ReviewEntryError
from booley.ticket_board.waiver_approval import WaiverDecisionError, WaiverDecisions
from tests.review.test_requested_review import (
    _approval_with_rejection,
    blocked,  # noqa: F401 — pytest fixture
)


@pytest.fixture
def selected(blocked):  # noqa: F811 — imported fixture
    root, tio, _ = blocked
    outcome = asyncio.run(lifecycle.request_review_command(root, "demo", reason="inspect"))
    assert outcome.ready, outcome.message
    return tio, lifecycle._selected_context(tio, "demo")


def _staged_writes(tio, ctx, staging: Path) -> None:
    _approval_with_rejection(tio, ctx, WaiverDecisions(), merge=True, staging=staging)
    state = staging / "log" / ".runtime" / "booley_state.json"
    document = json.loads(state.read_text())
    document["last_updated"] = "2026-10-01T00:00:00Z"
    state.write_bytes(json.dumps(document).encode())


@pytest.mark.parametrize("stop_after", [0, 1, 2])
def test_retry_recovers_each_partial_publication(selected, monkeypatch, stop_after):
    tio, ctx = selected
    write = transaction.atomic_replace_bytes
    calls = 0

    def interrupted(path, content):
        nonlocal calls
        if calls == stop_after:
            raise OSError("power lost during approval")
        calls += 1
        write(path, content)

    monkeypatch.setattr(transaction, "atomic_replace_bytes", interrupted)

    def apply(staging):
        _staged_writes(tio, ctx, staging)

    if stop_after < 2:
        with pytest.raises(OSError, match="power lost"):
            transaction.apply_transaction(ctx, WaiverDecisions(), apply)
    else:
        transaction.apply_transaction(ctx, WaiverDecisions(), apply)
    assert transaction.retry_capture(ctx)
    monkeypatch.setattr(transaction, "atomic_replace_bytes", write)

    def unexpected_stage(_staging):
        pytest.fail("retry recomputed a durable transaction")

    capture = transaction.apply_transaction(ctx, WaiverDecisions(), unexpected_stage)
    assert capture == transaction.retry_capture(ctx)
    state = json.loads((ctx.log_dir / ".runtime" / "booley_state.json").read_text())
    assert state["last_updated"] == "2026-10-01T00:00:00Z"


def test_retry_rejects_changes_outside_approval(selected):
    tio, ctx = selected
    transaction.apply_transaction(
        ctx, WaiverDecisions(), lambda staging: _staged_writes(tio, ctx, staging)
    )
    report = ctx.log_dir / "REPORT.md"
    report.write_text("Changed outside approval.\n")
    before = report.read_bytes()

    with pytest.raises(ReviewEntryError, match="inputs changed"):
        transaction.retry_capture(ctx)

    assert report.read_bytes() == before


def test_unfinished_transaction_requires_its_explicit_answers(selected):
    tio, ctx = selected
    decisions = WaiverDecisions(rejected=frozenset({"wc-explicit"}))
    transaction.apply_transaction(
        ctx, decisions, lambda staging: _staged_writes(tio, ctx, staging)
    )
    before = (ctx.log_dir / ".runtime" / "booley_state.json").read_bytes()

    with pytest.raises(WaiverDecisionError, match="explicit"):
        transaction.apply_transaction(ctx, WaiverDecisions(), lambda _: None)

    assert (ctx.log_dir / ".runtime" / "booley_state.json").read_bytes() == before
