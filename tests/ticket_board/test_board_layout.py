"""Tests for the Ticket Board layout and state records (ADR 0065)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.ticket_board.board_layout import (
    RUNTIME_DEFAULTS,
    StateRecord,
    StateRecordError,
    board_documents,
    board_relative_document_path,
    board_root,
    delete_state_record,
    document_stage,
    document_state,
    documents_in_state,
    iter_board_documents,
    locate_document,
    read_state_record,
    required_board_directories,
    state_record_path,
    state_root,
    ticket_document_path,
    ticket_state,
    write_state_record,
)
from booley.ticket_board.lifecycle import TicketState


def _place(tickets: Path, slug: str, state: TicketState) -> Path:
    """Write a board document and, unless *state* is draft, its state record."""
    path = ticket_document_path(tickets, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n", encoding="utf-8")
    if state is not TicketState.DRAFT:
        write_state_record(tickets, slug, StateRecord.fresh(state))
    return path


class TestPaths:
    def test_document_path_is_stable(self, tmp_path):
        assert ticket_document_path(tmp_path, "t1") == tmp_path / "board" / "t1.md"
        assert board_relative_document_path("t1") == Path("board", "t1.md")
        assert board_root(tmp_path) == tmp_path / "board"

    def test_state_record_path(self, tmp_path):
        assert state_record_path(tmp_path, "t1") == tmp_path / "state" / "t1.json"
        assert state_root(tmp_path) == tmp_path / "state"

    def test_required_directories(self, tmp_path):
        assert required_board_directories(tmp_path) == [tmp_path / "board", tmp_path / "state"]

    @pytest.mark.parametrize("slug", ["", ".", "..", "../x", "a/b", "a\\b"])
    def test_slug_never_leaves_the_board(self, tmp_path, slug):
        with pytest.raises(ValueError):
            ticket_document_path(tmp_path, slug)
        with pytest.raises(ValueError):
            state_record_path(tmp_path, slug)


class TestStateRecords:
    def test_missing_record_means_draft(self, tmp_path):
        assert read_state_record(tmp_path, "t1") is None
        assert ticket_state(tmp_path, "t1") is TicketState.DRAFT

    @pytest.mark.parametrize(
        "state", [s for s in TicketState if s not in {TicketState.DRAFT, TicketState.ARCHIVED}]
    )
    def test_round_trip(self, tmp_path, state):
        record = StateRecord.fresh(state, execution_id="e1", execution_owner_pid=42)
        write_state_record(tmp_path, "t1", record)
        assert read_state_record(tmp_path, "t1") == record
        stored = json.loads(state_record_path(tmp_path, "t1").read_text())
        assert stored["schema"] == 1
        assert stored["state"] == state.status
        assert stored["execution_id"] == "e1"

    def test_draft_has_no_record(self):
        with pytest.raises(StateRecordError):
            StateRecord.fresh(TicketState.DRAFT)

    def test_archived_record_fails_closed(self, tmp_path):
        """Archived Tickets live only in Ticket History (ADR 0065)."""
        with pytest.raises(StateRecordError, match="Ticket History"):
            StateRecord.fresh(TicketState.ARCHIVED)
        path = state_record_path(tmp_path, "t1")
        path.parent.mkdir(parents=True)
        stored = StateRecord.fresh(TicketState.BLOCKED).to_json() | {"state": "archived"}
        path.write_text(json.dumps(stored), encoding="utf-8")
        with pytest.raises(StateRecordError, match="invalid"):
            read_state_record(tmp_path, "t1")

    def test_with_state_and_runtime_keep_other_fields(self):
        record = StateRecord.fresh(TicketState.QUEUED, step="lint")
        moved = record.with_state(TicketState.RUNNING).with_runtime({"execution_id": "e2"})
        assert moved.state is TicketState.RUNNING
        assert moved.runtime["step"] == "lint"
        assert moved.execution_id == "e2"
        assert record.execution_id == ""

    def test_progress_is_a_copy(self):
        record = StateRecord.fresh(TicketState.QUEUED)
        progress = record.progress()
        progress["steps_completed"].append("lint")
        assert record.runtime["steps_completed"] == []

    def test_delete_returns_ticket_to_draft(self, tmp_path):
        write_state_record(tmp_path, "t1", StateRecord.fresh(TicketState.BLOCKED))
        assert delete_state_record(tmp_path, "t1") is True
        assert ticket_state(tmp_path, "t1") is TicketState.DRAFT
        assert delete_state_record(tmp_path, "t1") is False


class TestFailClosed:
    """Only an absent record means draft; anything else untrustworthy raises."""

    @staticmethod
    def _raw(tickets: Path, content: bytes) -> Path:
        path = state_record_path(tickets, "t1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    @staticmethod
    def _valid() -> dict:
        return {"schema": 1, "state": "queued", **RUNTIME_DEFAULTS}

    @pytest.mark.parametrize(
        "content",
        [
            b"",  # empty
            b'{"schema": 1, "state": "queu',  # truncated
            b"not json",  # corrupt
            b"[]",  # non-object
            b"\xff\xfe",  # not UTF-8
        ],
    )
    def test_unparseable_record_raises(self, tmp_path, content):
        self._raw(tmp_path, content)
        with pytest.raises(StateRecordError):
            read_state_record(tmp_path, "t1")
        with pytest.raises(StateRecordError):
            ticket_state(tmp_path, "t1")

    @pytest.mark.parametrize(
        "change",
        [
            {"schema": 2},  # unknown schema
            {"schema": True},
            {"schema": "1"},
            {"state": "draft"},  # drafts have no record
            {"state": "flying"},
            {"state": None},
            {"step": 3},
            {"steps_completed": "lint"},
            {"steps_completed": [1]},
            {"error": {"x": 1}},
            {"execution_owner_pid": True},
            {"execution_owner_pid": "12"},
            {"surprise": 1},  # unknown field
        ],
    )
    def test_invalid_fields_raise(self, tmp_path, change):
        self._raw(tmp_path, json.dumps({**self._valid(), **change}).encode())
        with pytest.raises(StateRecordError):
            read_state_record(tmp_path, "t1")

    def test_missing_field_raises(self, tmp_path):
        value = self._valid()
        del value["execution_id"]
        self._raw(tmp_path, json.dumps(value).encode())
        with pytest.raises(StateRecordError):
            read_state_record(tmp_path, "t1")

    def test_unreadable_record_raises(self, tmp_path):
        # A directory where the record should be is present but unreadable.
        state_record_path(tmp_path, "t1").mkdir(parents=True)
        with pytest.raises(StateRecordError):
            read_state_record(tmp_path, "t1")

    def test_locate_raises_for_corrupt_record(self, tmp_path):
        _place(tmp_path, "t1", TicketState.DRAFT)
        self._raw(tmp_path, b"{")
        with pytest.raises(StateRecordError):
            locate_document(tmp_path, "t1")
        with pytest.raises(StateRecordError):
            document_state(tmp_path, ticket_document_path(tmp_path, "t1"))

    def test_listing_skips_corrupt_record(self, tmp_path):
        good = _place(tmp_path, "good", TicketState.QUEUED)
        _place(tmp_path, "t1", TicketState.DRAFT)
        self._raw(tmp_path, b"{")
        assert list(iter_board_documents(tmp_path)) == [(good, TicketState.QUEUED)]


class TestBoardDocuments:
    def test_document_state_follows_record(self, tmp_path):
        path = _place(tmp_path, "t1", TicketState.DRAFT)
        assert document_state(tmp_path, path) is TicketState.DRAFT
        for state in TicketState:
            if state not in {TicketState.DRAFT, TicketState.ARCHIVED}:
                write_state_record(tmp_path, "t1", StateRecord.fresh(state))
                assert document_state(tmp_path, path) is state

    @pytest.mark.parametrize(
        "relative",
        [
            "logs/t1/ticket.md",  # runtime snapshot
            "board/queue/t1.md",  # a legacy state directory
            "board/t1.txt",  # not a Ticket document
            "other/t1.md",  # off the board
        ],
    )
    def test_document_state_rejects_non_board_paths(self, tmp_path, relative):
        board_root(tmp_path).mkdir(parents=True, exist_ok=True)
        assert document_state(tmp_path, tmp_path / relative) is None

    def test_document_state_rejects_another_board(self, tmp_path):
        path = _place(tmp_path / "other", "t1", TicketState.QUEUED)
        board_root(tmp_path / "mine").mkdir(parents=True)
        assert document_state(tmp_path / "mine", path) is None

    def test_document_state_accepts_board_through_bind_mount(self, tmp_path, monkeypatch):
        # A bind mount shows one directory at two paths that resolve() cannot
        # unify; samefile() is the only check that sees they are the same.
        mount = tmp_path / "mount"
        real = tmp_path / "real"
        path = _place(mount, "t1", TicketState.DRAFT)
        board_root(real).mkdir(parents=True)
        write_state_record(real, "t1", StateRecord.fresh(TicketState.WAITING))
        mount_board = board_root(mount).resolve()
        real_board = board_root(real).resolve()

        def samefile(self: Path, other: Path) -> bool:
            return {self.resolve(), Path(other).resolve()} == {mount_board, real_board}

        monkeypatch.setattr(Path, "samefile", samefile)
        assert document_state(real, path) is TicketState.WAITING

    def test_document_state_propagates_unreadable_board(self, tmp_path, monkeypatch):
        path = _place(tmp_path / "elsewhere", "t1", TicketState.QUEUED)
        board_root(tmp_path / "mine").mkdir(parents=True)

        def samefile(self: Path, other: Path) -> bool:
            raise PermissionError(13, "Permission denied", str(other))

        monkeypatch.setattr(Path, "samefile", samefile)
        with pytest.raises(PermissionError):
            document_state(tmp_path / "mine", path)

    def test_document_state_accepts_unnormalized_spelling(self, tmp_path):
        _place(tmp_path, "t1", TicketState.QUEUED)
        path = tmp_path / "x" / ".." / "board" / "t1.md"
        assert document_state(tmp_path, path) is TicketState.QUEUED

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_document_state_accepts_board_through_alias(self, tmp_path):
        real = tmp_path / "real"
        path = _place(real, "t1", TicketState.BLOCKED)
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        assert document_state(alias, path) is TicketState.BLOCKED

    def test_document_stage(self, tmp_path):
        path = _place(tmp_path, "t1", TicketState.DRAFT)
        assert document_stage(tmp_path, path, off_board="executable") == "draft"
        write_state_record(tmp_path, "t1", StateRecord.fresh(TicketState.REVIEW))
        assert document_stage(tmp_path, path, off_board="draft") == "executable"

    @pytest.mark.parametrize("off_board", ["draft", "executable"])
    def test_document_stage_off_board_uses_caller_default(self, tmp_path, off_board):
        path = tmp_path / "logs" / "t1" / "ticket.md"
        assert document_stage(tmp_path, path, off_board=off_board) == off_board

    def test_board_documents_ignores_other_entries(self, tmp_path):
        assert board_documents(tmp_path) == []
        doc = _place(tmp_path, "a", TicketState.DRAFT)
        (board_root(tmp_path) / "notes.txt").write_text("x")
        (board_root(tmp_path) / "queue").mkdir()
        (board_root(tmp_path) / "queue" / "b.md").write_text("x")
        assert board_documents(tmp_path) == [doc]

    def test_documents_in_state_sorted(self, tmp_path):
        assert documents_in_state(tmp_path, TicketState.DONE) == []
        second = _place(tmp_path, "b", TicketState.DONE)
        first = _place(tmp_path, "a", TicketState.DONE)
        _place(tmp_path, "c", TicketState.QUEUED)
        assert documents_in_state(tmp_path, TicketState.DONE) == [first, second]

    def test_iter_board_documents_lifecycle_order(self, tmp_path):
        done = _place(tmp_path, "a", TicketState.DONE)
        draft = _place(tmp_path, "z", TicketState.DRAFT)
        running = _place(tmp_path, "m", TicketState.RUNNING)
        assert list(iter_board_documents(tmp_path)) == [
            (draft, TicketState.DRAFT),
            (running, TicketState.RUNNING),
            (done, TicketState.DONE),
        ]

    def test_locate_document(self, tmp_path):
        assert locate_document(tmp_path, "t1") is None
        path = _place(tmp_path, "t1", TicketState.REVIEW)
        assert locate_document(tmp_path, "t1") == (path, TicketState.REVIEW)

    def test_record_without_document_is_not_a_ticket(self, tmp_path):
        board_root(tmp_path).mkdir(parents=True)
        write_state_record(tmp_path, "t1", StateRecord.fresh(TicketState.QUEUED))
        assert locate_document(tmp_path, "t1") is None
        assert list(iter_board_documents(tmp_path)) == []

    def test_locate_document_never_traverses(self, tmp_path):
        _place(tmp_path, "t1", TicketState.QUEUED)
        (tmp_path / "escape.md").write_text("x")
        assert locate_document(tmp_path, "../../escape") is None
        assert locate_document(tmp_path, "T1") is None


def test_project_gitignore_ignores_board_and_state_but_not_history():
    """runtime cannot import ticket_board, so its ignore literals are pinned here."""
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE_PATTERNS
    from booley.ticket_board.board_layout import HISTORY_DIR_NAME, STATE_DIR_NAME
    from booley.ticket_board.lifecycle import BOARD_DIR_NAME

    assert f"tickets/{BOARD_DIR_NAME}/" in PROJECT_GITIGNORE_PATTERNS
    assert f"tickets/{STATE_DIR_NAME}/" in PROJECT_GITIGNORE_PATTERNS
    assert not any(HISTORY_DIR_NAME in pattern for pattern in PROJECT_GITIGNORE_PATTERNS)
