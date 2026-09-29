"""Ticket operations over state records (ADR 0065).

Pins the compare-and-swap on the state record, that transitions never rename
the Ticket document, and that a corrupt state record makes every command on
that Ticket fail without changing the document or the record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.ticket_board.board_layout import (
    StateRecord,
    StateRecordError,
    read_state_record,
    state_record_path,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.io import TicketIO
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.operations import op_claim

from .conftest import make_ticket_file


def _record(tio: TicketIO, slug: str = "t1") -> StateRecord:
    record = read_state_record(tio.tickets_dir, slug)
    assert record is not None
    return record


class TestUnfinishedEnqueue:
    """A record beside a draft-form document belongs to enqueue recovery alone."""

    @staticmethod
    def _pending(tio: TicketIO) -> None:
        from booley.ticket_board.enqueue_publication import enqueue_pending

        journal = tio._project_root / ".runtime" / "acceptance" / "enqueue" / "t1.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}\n", encoding="utf-8")
        assert enqueue_pending(tio._project_root, "t1")

    def test_moves_and_claims_refuse(self, tio: TicketIO) -> None:
        make_ticket_file(tio, "queue", "t1")
        self._pending(tio)
        before = state_record_path(tio.tickets_dir, "t1").read_bytes()

        assert not op_claim(tio, "t1")
        assert not tio.move_and_update("t1", TicketState.BLOCKED, {})
        assert not tio.move_ticket_file("t1", TicketState.BLOCKED)

        assert state_record_path(tio.tickets_dir, "t1").read_bytes() == before

    def test_no_journal_means_nothing_pending(self, tio: TicketIO) -> None:
        from booley.ticket_board.enqueue_publication import enqueue_pending

        make_ticket_file(tio, "queue", "t1")
        assert not enqueue_pending(tio._project_root, "t1")
        assert op_claim(tio, "t1")


class TestCompareAndSwap:
    def test_move_commits_state_and_runtime_in_the_record(self, tio: TicketIO) -> None:
        document = make_ticket_file(tio, "queue", "t1")
        before = document.read_bytes()

        assert tio.move_and_update("t1", TicketState.BLOCKED, {"blocked_reason": "why"})

        record = _record(tio)
        assert record.state is TicketState.BLOCKED
        assert record.runtime["blocked_reason"] == "why"
        assert document == ticket_document_path(tio.tickets_dir, "t1")
        assert document.read_bytes() == before

    def test_stale_expected_status_changes_nothing(self, tio: TicketIO) -> None:
        make_ticket_file(tio, "queue", "t1")
        before = state_record_path(tio.tickets_dir, "t1").read_bytes()

        assert not tio.move_and_update("t1", TicketState.BLOCKED, {}, expected_status="running")

        assert state_record_path(tio.tickets_dir, "t1").read_bytes() == before

    def test_stale_expected_execution_id_changes_nothing(self, tio: TicketIO) -> None:
        make_ticket_file(tio, "active", "t1")
        write_state_record(
            tio.tickets_dir,
            "t1",
            StateRecord.fresh(TicketState.RUNNING, execution_id="current"),
        )
        before = state_record_path(tio.tickets_dir, "t1").read_bytes()

        assert not tio.move_and_update(
            "t1",
            TicketState.BLOCKED,
            {},
            expected_status="running",
            expected_execution_id="stale",
        )
        assert state_record_path(tio.tickets_dir, "t1").read_bytes() == before

        assert tio.move_and_update(
            "t1",
            TicketState.BLOCKED,
            {},
            expected_status="running",
            expected_execution_id="current",
        )
        assert _record(tio).state is TicketState.BLOCKED

    def test_draft_is_never_moved_by_a_plain_transition(self, tio: TicketIO) -> None:
        make_ticket_file(tio, "drafts", "t1")

        assert not tio.move_and_update("t1", TicketState.QUEUED, {})
        assert not tio.move_ticket_file("t1", TicketState.QUEUED)

        assert read_state_record(tio.tickets_dir, "t1") is None

    def test_nothing_moves_to_draft_by_a_plain_transition(self, tio: TicketIO) -> None:
        make_ticket_file(tio, "blocked", "t1")

        assert not tio.move_and_update("t1", TicketState.DRAFT, {})
        assert not tio.move_ticket_file("t1", TicketState.DRAFT)

        assert _record(tio).state is TicketState.BLOCKED

    def test_claim_only_takes_a_queued_ticket_once(self, tio: TicketIO) -> None:
        make_ticket_file(tio, "queue", "t1")

        assert op_claim(tio, "t1")
        claimed = _record(tio)
        assert claimed.state is TicketState.RUNNING
        assert claimed.execution_id
        assert not op_claim(tio, "t1")
        assert _record(tio) == claimed

    def test_stamp_execution_swaps_only_the_expected_generation(self, tio: TicketIO) -> None:
        make_ticket_file(tio, "active", "t1")
        write_state_record(
            tio.tickets_dir, "t1", StateRecord.fresh(TicketState.RUNNING, execution_id="a")
        )

        assert not tio.stamp_execution("t1", "b", 7, expected_execution_id="stale")
        assert _record(tio).execution_id == "a"
        assert tio.stamp_execution("t1", "b", 7, expected_execution_id="a")
        assert _record(tio).runtime["execution_owner_pid"] == 7

    def test_create_refuses_a_leftover_record(self, tio: TicketIO) -> None:
        write_state_record(tio.tickets_dir, "t1", StateRecord.fresh(TicketState.BLOCKED))

        assert tio.create_ticket_document("t1", "---\n") is None

        assert not ticket_document_path(tio.tickets_dir, "t1").exists()


def _corrupt(tio: TicketIO, content: bytes) -> tuple[Path, bytes, bytes]:
    document = make_ticket_file(tio, "queue", "t1")
    path = state_record_path(tio.tickets_dir, "t1")
    path.write_bytes(content)
    return document, document.read_bytes(), content


CORRUPT_RECORDS = [
    pytest.param(b"{", id="truncated"),
    pytest.param(b"not json", id="corrupt"),
    pytest.param(b"[1, 2]", id="non-object"),
    pytest.param(b'{"schema": 99, "state": "queued"}', id="unknown-schema"),
    pytest.param(b'{"schema": 1, "state": "queued"}', id="missing-fields"),
]


@pytest.mark.parametrize("content", CORRUPT_RECORDS)
class TestFailClosed:
    """A corrupt record is never a draft: each command errors and writes nothing."""

    @staticmethod
    def _assert_untouched(tio: TicketIO, document: Path, doc: bytes, record: bytes) -> None:
        assert document.read_bytes() == doc
        assert state_record_path(tio.tickets_dir, "t1").read_bytes() == record

    def test_find_ticket(self, tio: TicketIO, content: bytes) -> None:
        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            tio.find_ticket("t1")
        self._assert_untouched(tio, document, doc, record)

    def test_move_and_update(self, tio: TicketIO, content: bytes) -> None:
        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            tio.move_and_update("t1", TicketState.BLOCKED, {"blocked_reason": "x"})
        self._assert_untouched(tio, document, doc, record)

    def test_move_ticket_file(self, tio: TicketIO, content: bytes) -> None:
        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            tio.move_ticket_file("t1", TicketState.BLOCKED)
        self._assert_untouched(tio, document, doc, record)

    def test_claim(self, tio: TicketIO, content: bytes) -> None:
        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            op_claim(tio, "t1")
        self._assert_untouched(tio, document, doc, record)

    def test_stamp_execution(self, tio: TicketIO, content: bytes) -> None:
        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            tio.stamp_execution("t1", "b", 1, expected_execution_id="")
        self._assert_untouched(tio, document, doc, record)

    def test_init_ticket(self, tio: TicketIO, content: bytes, monkeypatch) -> None:
        document, doc, record = _corrupt(tio, content)
        # Reach the state check: conversion and basis validation are not under test.
        monkeypatch.setattr(tio, "_convert_ticket", lambda *_args: object())
        monkeypatch.setattr(tio, "_validate_enqueue_basis", lambda *_args: [])
        with pytest.raises(StateRecordError):
            tio.init_ticket(document)
        self._assert_untouched(tio, document, doc, record)

    def test_enqueue(self, tio: TicketIO, content: bytes) -> None:
        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            tio.enqueue_ticket("t1")
        self._assert_untouched(tio, document, doc, record)

    def test_create_over_the_slug(self, tio: TicketIO, content: bytes, monkeypatch) -> None:
        document, doc, record = _corrupt(tio, content)
        # Reach the slug lookup: draft validation is not under test.
        monkeypatch.setattr(tio, "_prepare_new_ticket_content", lambda _slug, text: text)
        with pytest.raises(StateRecordError):
            tio.create_ticket_document("t1", "---\n")
        self._assert_untouched(tio, document, doc, record)

    def test_reset(self, tio: TicketIO, content: bytes) -> None:
        from booley.ticket_board.operations import op_reset

        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            op_reset(tio, "t1", force=True)
        self._assert_untouched(tio, document, doc, record)

    def test_archive(self, tio: TicketIO, content: bytes) -> None:
        from booley.ticket_board.archive import _marker_path, op_archive
        from booley.ticket_board.ticket_history import read_closed_ticket

        document, doc, record = _corrupt(tio, content)
        outcome = op_archive(tio, "t1")
        assert "t1" in outcome.failures
        assert "state record" in outcome.failures["t1"]
        self._assert_untouched(tio, document, doc, record)
        # The Ticket never closed and no resumable archive was left behind.
        assert read_closed_ticket(tio.tickets_dir, "t1") is None
        assert not _marker_path(tio._project_root, "t1").exists()

    def test_return_to_draft(self, tio: TicketIO, content: bytes) -> None:
        document, doc, record = _corrupt(tio, content)
        with pytest.raises(StateRecordError):
            tio.return_to_draft("t1")
        self._assert_untouched(tio, document, doc, record)

    def test_update_board(self, tio: TicketIO, content: bytes) -> None:
        from types import SimpleNamespace

        from booley.ticket_board.cli_handlers import _apply_board_update

        document, doc, record = _corrupt(tio, content)
        args = SimpleNamespace(reset_steps=True, reset_steps_from=None, append_step=None, log=None)
        with pytest.raises(StateRecordError):
            _apply_board_update(tio, "t1", args, {"step": "lint"}, "queued", "")
        self._assert_untouched(tio, document, doc, record)

    def test_review_abandon(self, tio: TicketIO, content: bytes) -> None:
        from booley.ticket_board.review_lifecycle import _abandon_publication

        document, doc, record = _corrupt(tio, content)
        operation = {"entry": {"source_status": "blocked"}}
        with pytest.raises(StateRecordError):
            _abandon_publication(tio, "t1", operation)
        self._assert_untouched(tio, document, doc, record)

    def test_clear_from_step(self, tio: TicketIO, content: bytes) -> None:
        from booley.ticket_board.logs import clear_from_step

        document, doc, record = _corrupt(tio, content)
        (tio.logs_dir / "t1").mkdir(parents=True, exist_ok=True)
        with pytest.raises(StateRecordError):
            clear_from_step(tio.logs_dir, "t1", "lint")
        self._assert_untouched(tio, document, doc, record)

    def test_board_listing_hides_only_the_bad_ticket(self, tio: TicketIO, content: bytes) -> None:
        from booley.ticket_board.scanner import scan_all_tickets

        document, doc, record = _corrupt(tio, content)
        make_ticket_file(tio, "queue", "t2")
        assert [entry["file"] for entry in scan_all_tickets(tio.tickets_dir)] == ["board/t2.md"]
        self._assert_untouched(tio, document, doc, record)


def test_cli_reports_a_broken_record_instead_of_a_traceback(
    tio: TicketIO, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.ticket_board import cli

    _corrupt(tio, b"{")
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: tio.tickets_dir)

    assert cli.main(["show", "t1"]) == 2

    assert "state record for 't1' is not valid JSON" in capsys.readouterr().err


def test_booley_board_reports_a_broken_record_instead_of_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from argparse import Namespace

    from booley.harness import booley

    def broken(*_args):
        raise StateRecordError("state record for 't1' is invalid")

    monkeypatch.setattr(booley, "_run_board_command", broken)

    assert booley._cmd_board(Namespace(board_command="show", slug="t1"), tmp_path) == 2

    assert "state record for 't1' is invalid" in capsys.readouterr().err
