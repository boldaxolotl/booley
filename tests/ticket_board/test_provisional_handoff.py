"""A provisionally met Ticket goes to review without freezing acceptance (ADR 0066)."""

from __future__ import annotations

import json

import pytest
import yaml

from booley.flows.sim.coverage_provisional import CandidateScreen, ProvisionalVerdict
from booley.ticket_board.acceptance_ledger import read_acceptance
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.provisional_coverage import (
    ProvisionalCriterion,
    TicketProvisionalCoverage,
)
from booley.ticket_board.provisional_handoff import (
    op_handoff_provisional,
    read_provisional_marker,
)
from booley.ticket_board.review_lifecycle import _awaits_provisional_inspection
from booley.ticket_board.scanner import find_ticket_file
from tests.ticket_board.conftest import publish_handoff_snapshot
from tests.ticket_board.test_ticket_board import (
    _make_handoff_ready_ticket,
    _synthetic_non_git_ticket_view,  # noqa: F401 — autouse: synthetic Ticket baselines
    make_tio,
)

_HEADS = {"outer": "a" * 40}


def _provisional() -> TicketProvisionalCoverage:
    verdict = ProvisionalVerdict(
        "fail",
        {"status": "pass", "provisional": True},
        (
            CandidateScreen("wc-aaa", "offered", "ok", needed=True),
            CandidateScreen("wc-bbb", "offered", "ok"),
            CandidateScreen("wc-ccc", "stale", "source changed"),
        ),
    )
    criterion = ProvisionalCriterion(
        "coverage_line", "campaign:1", "sim/1/targets/t/coverage.json", verdict
    )
    return TicketProvisionalCoverage((criterion,), "sha256:" + "f" * 64)


def _set_destination_done(tio, slug: str) -> None:
    path, _status = find_ticket_file(tio.tickets_dir, slug)
    _opening, frontmatter, body = path.read_text(encoding="utf-8").split("---", 2)
    fields = yaml.safe_load(frontmatter)
    fields["on_success"] = []  # destination=done
    path.write_text("---\n" + yaml.safe_dump(fields, sort_keys=False) + "---" + body)


@pytest.fixture
def tio(tmp_path, monkeypatch):
    tio = make_tio(tmp_path)
    _make_handoff_ready_ticket(tio, "t1")
    monkeypatch.setattr(
        "booley.ticket_board.operations._handoff_basis_heads", lambda *_args: dict(_HEADS)
    )
    return tio


def test_provisional_handoff_reaches_review_without_freezing(tio) -> None:
    _set_destination_done(tio, "t1")  # destination=done must not auto-complete

    assert op_handoff_provisional(tio, "t1", _provisional()) is True

    _path, status = find_ticket_file(tio.tickets_dir, "t1")
    assert status == TicketState.REVIEW.status
    log_dir = tio.logs_dir / "t1"
    assert read_acceptance(log_dir).kind == "unavailable"
    marker = read_provisional_marker(log_dir)
    assert marker is not None
    assert marker["heads"] == _HEADS
    assert marker["candidate_record_sha256"] == "sha256:" + "f" * 64
    assert marker["criteria"] == [
        {
            "key": "coverage_line",
            "campaign_id": "campaign:1",
            "reference_path": "sim/1/targets/t/coverage.json",
            "offered": ["wc-aaa", "wc-bbb"],
            "needed": ["wc-aaa"],
        }
    ]
    transitions = (log_dir / "human-logs" / "transitions.log").read_text()
    assert "provisional coverage: coverage_line" in transitions
    assert _awaits_provisional_inspection(tio, "t1", None) is True


def test_failed_basis_validation_leaves_the_ticket_running(tio, monkeypatch) -> None:
    monkeypatch.setattr("booley.ticket_board.operations._handoff_basis_heads", lambda *_a: None)

    assert op_handoff_provisional(tio, "t1", _provisional()) is False

    _path, status = find_ticket_file(tio.tickets_dir, "t1")
    assert status == TicketState.RUNNING.status
    assert read_provisional_marker(tio.logs_dir / "t1") is None


def test_already_frozen_acceptance_refuses_a_provisional_handoff(tio, capsys) -> None:
    publish_handoff_snapshot(tio, "t1")

    assert op_handoff_provisional(tio, "t1", _provisional()) is False

    assert "acceptance is already recorded" in capsys.readouterr().err
    assert read_provisional_marker(tio.logs_dir / "t1") is None


def test_handoff_requires_a_provisional_criterion(tio) -> None:
    with pytest.raises(ValueError, match="at least one"):
        op_handoff_provisional(tio, "t1", TicketProvisionalCoverage((), ""))


def test_ordinary_review_does_not_await_a_provisional_inspection(tio) -> None:
    assert _awaits_provisional_inspection(tio, "t1", None) is False
    op_handoff_provisional(tio, "t1", _provisional())
    assert _awaits_provisional_inspection(tio, "t1", {"entry": "published"}) is False


def test_unknown_marker_schema_is_rejected(tio) -> None:
    op_handoff_provisional(tio, "t1", _provisional())
    path = tio.logs_dir / "t1" / "review" / "provisional-handoff.json"
    path.write_text(json.dumps({"schema": 99}))

    with pytest.raises(ValueError, match="schema"):
        read_provisional_marker(tio.logs_dir / "t1")


def test_a_marker_from_another_execution_cannot_open_the_review_guard(tio, capsys) -> None:
    """The unaccepted-review exception is bound to the current execution only."""
    path = tio.logs_dir / "t1" / "review" / "provisional-handoff.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 1, "execution_id": "some-older-execution"}))

    assert tio.move_and_update("t1", TicketState.REVIEW, {}) is False

    assert "unaccepted review requires" in capsys.readouterr().err
    _path, status = find_ticket_file(tio.tickets_dir, "t1")
    assert status == TicketState.RUNNING.status
