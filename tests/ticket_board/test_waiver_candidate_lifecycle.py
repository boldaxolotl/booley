"""Waiver Candidates follow the Ticket lifecycle (ADR 0066).

Return-to-draft and reset drop candidates but keep rejections; closing a
Ticket discards both, and a crash mid-close leaves a detectable leftover.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from booley.ticket_board import waiver_candidates as store
from booley.ticket_board.board_layout import waiver_candidates_path
from booley.ticket_board.ticket_history import finish_closing, interrupted_closings
from tests.ticket_board.conftest import make_closed_ticket
from tests.ticket_board.test_ticket_baseline import _blocked_ticket
from tests.ticket_board.test_ticket_board import (
    _synthetic_non_git_ticket_view,  # noqa: F401 — autouse: synthetic Ticket baselines
    make_ticket_in_dir,
    make_tio,
)

_SHA = "sha256:" + "a" * 64
_POINT = "cp1:eyJsb2NhdGlvbiI6eyJzb3VyY2UiOiJydGwvY291bnRlci5zdiJ9fQ"


def _seed(tickets_dir: Path, slug: str) -> None:
    """One candidate and one rejection for *slug*."""
    binding = store.CampaignBinding(
        "campaign:1", _SHA, _SHA, "sim/1/targets/t/coverage.json", "acme:x:y:1#sim", "sim"
    )
    proposal = store.WaiverProposal(_POINT, "rtl/counter.sv", _SHA, "unreachable", "Tied off")
    store.record_proposals(
        tickets_dir,
        slug,
        binding,
        [proposal],
        invocation_id="1",
        now=datetime(2026, 9, 30, tzinfo=UTC),
    )
    store.record_rejections(
        tickets_dir,
        slug,
        [store.Rejection("acme:x:y:1#sim", "cp1:other", _SHA, "2026-09-30T00:00:00Z", "A")],
    )


def test_closing_discards_candidates_and_rejections(tmp_path: Path) -> None:
    tio = make_tio(tmp_path)
    _seed(tio.tickets_dir, "t1")

    assert finish_closing(tio.tickets_dir, "t1") is True

    assert not waiver_candidates_path(tio.tickets_dir, "t1").exists()
    assert finish_closing(tio.tickets_dir, "t1") is False  # idempotent


def test_a_leftover_record_marks_an_interrupted_closing(tmp_path: Path) -> None:
    tio = make_tio(tmp_path)
    make_closed_ticket(tio, "t1")
    assert interrupted_closings(tio.tickets_dir) == []
    _seed(tio.tickets_dir, "t1")

    assert interrupted_closings(tio.tickets_dir) == ["t1"]


def test_reset_drops_candidates_but_keeps_rejections(tmp_path: Path) -> None:
    from booley.ticket_board.operations import _reset_runtime_state

    tio = make_tio(tmp_path)
    make_ticket_in_dir(tio, "blocked", "t1")
    _seed(tio.tickets_dir, "t1")

    _reset_runtime_state(tio, "t1")

    record = store.load(tio.tickets_dir, "t1")
    assert not record.candidates
    assert len(record.rejections) == 1


def test_return_to_draft_drops_candidates_but_keeps_rejections(tmp_path: Path) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    _seed(tio.tickets_dir, "blocked-again")

    tio.return_to_draft("blocked-again")

    record = store.load(tio.tickets_dir, "blocked-again")
    assert not record.candidates
    assert len(record.rejections) == 1
