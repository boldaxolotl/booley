"""Return-to-draft Board cutover at one fixed document path (ADR 0065).

The cutover publishes the draft-form document first and deletes the state
record second, so a crash leaves at most a draft-form document beside a
blocked record, which rerunning the cutover rolls forward.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from booley.ticket_board import draft_transition
from booley.ticket_board.board_layout import (
    StateRecord,
    read_state_record,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.lifecycle import TicketState

BLOCKED = b"blocked executable\n"
DRAFT = b"draft form\n"


class _CrashError(Exception):
    """Simulated process death at one cutover checkpoint."""


@pytest.fixture
def cutover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return (root, tickets, journal) with a blocked Ticket and a prepared draft."""
    root = tmp_path / "root"
    tickets = root / ".booley_project" / "tickets"
    monkeypatch.setattr(
        draft_transition, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(draft_transition, "runtime_dir", lambda _root: root / ".runtime")
    document = ticket_document_path(tickets, "t1")
    document.parent.mkdir(parents=True)
    document.write_bytes(BLOCKED)
    write_state_record(
        tickets, "t1", StateRecord.fresh(TicketState.BLOCKED, blocked_reason="needs a human")
    )
    operation_id = "a" * 32
    operation = draft_transition._operation_dir(root, operation_id)
    operation.mkdir(parents=True)
    (operation / "draft.md").write_bytes(DRAFT)
    archive = tickets / "logs" / "t1" / "runs" / "001"
    journal = draft_transition.DraftTransitionJournal(
        2,
        operation_id,
        "t1",
        "cutover-ready",
        {},
        str(document),
        hashlib.sha256(BLOCKED).hexdigest(),
        str(document),
        hashlib.sha256(DRAFT).hexdigest(),
        "0" * 16,
        "1" * 64,
        str(archive),
        False,
    )
    return root, tickets, journal


def test_cutover_publishes_draft_then_deletes_record(cutover) -> None:
    root, tickets, journal = cutover

    draft_transition._publish_board(root, journal)

    assert ticket_document_path(tickets, "t1").read_bytes() == DRAFT
    assert read_state_record(tickets, "t1") is None
    archived = json.loads((Path(journal.archive_dir) / "state.json").read_text())
    assert archived["state"] == "blocked"
    assert archived["blocked_reason"] == "needs a human"
    backup = draft_transition._operation_dir(root, journal.operation_id) / "blocked.md"
    assert backup.read_bytes() == BLOCKED
    # Rerunning a finished cutover is a no-op.
    draft_transition._publish_board(root, journal)
    assert ticket_document_path(tickets, "t1").read_bytes() == DRAFT


def _crash_on_write(patch: pytest.MonkeyPatch, name: str) -> None:
    """Die just after the cutover writes the file called *name*."""
    real_write = draft_transition.atomic_replace_bytes

    def crashing(path: Path, content: bytes, **kwargs) -> None:
        real_write(path, content, **kwargs)
        if Path(path).name == name:
            raise _CrashError(name)

    patch.setattr(draft_transition, "atomic_replace_bytes", crashing)


@pytest.mark.parametrize("crash", ["backup", "document", "archive", "record"])
def test_crash_mid_cutover_rolls_forward(
    cutover, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    root, tickets, journal = cutover
    document = ticket_document_path(tickets, "t1")
    with monkeypatch.context() as patch:
        if crash == "backup":
            _crash_on_write(patch, "blocked.md")
        elif crash == "archive":
            _crash_on_write(patch, "state.json")
        elif crash == "document":
            real_replace = Path.replace

            def crashing(path: Path, target: Path) -> Path:
                if Path(target) == document:
                    raise _CrashError(crash)
                return real_replace(path, target)

            patch.setattr(Path, "replace", crashing)
        else:

            def crashing_delete(*_args, **_kwargs):
                raise _CrashError(crash)

            patch.setattr(draft_transition, "delete_state_record", crashing_delete)
        with pytest.raises(_CrashError):
            draft_transition._publish_board(root, journal)

    # Never an executable-form document without a record.
    record = read_state_record(tickets, "t1")
    if document.read_bytes() == BLOCKED:
        assert record is not None and record.state is TicketState.BLOCKED

    draft_transition._publish_board(root, journal)

    assert document.read_bytes() == DRAFT
    assert read_state_record(tickets, "t1") is None
    archived = json.loads((Path(journal.archive_dir) / "state.json").read_text())
    assert archived["state"] == "blocked"


@pytest.mark.parametrize("state", [TicketState.QUEUED, TicketState.RUNNING, TicketState.REVIEW])
def test_cutover_refuses_a_record_that_is_no_longer_blocked(cutover, state: TicketState) -> None:
    root, tickets, journal = cutover
    write_state_record(tickets, "t1", StateRecord.fresh(state))

    with pytest.raises(draft_transition.DraftTransitionError, match=f"found {state.status}"):
        draft_transition._publish_board(root, journal)

    assert ticket_document_path(tickets, "t1").read_bytes() == BLOCKED
    record = read_state_record(tickets, "t1")
    assert record is not None and record.state is state


def test_cutover_refuses_a_changed_blocked_document(cutover) -> None:
    root, tickets, journal = cutover
    document = ticket_document_path(tickets, "t1")
    document.write_bytes(b"edited\n")

    with pytest.raises(draft_transition.DraftTransitionError, match="changed unexpectedly"):
        draft_transition._publish_board(root, journal)

    assert document.read_bytes() == b"edited\n"
    assert read_state_record(tickets, "t1") is not None


def test_cutover_refuses_a_missing_replacement_draft(cutover) -> None:
    root, tickets, journal = cutover
    (draft_transition._operation_dir(root, journal.operation_id) / "draft.md").unlink()

    with pytest.raises(draft_transition.DraftTransitionError, match="replacement draft"):
        draft_transition._publish_board(root, journal)

    assert ticket_document_path(tickets, "t1").read_bytes() == BLOCKED
    assert read_state_record(tickets, "t1") is not None
