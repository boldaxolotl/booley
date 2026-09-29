"""Tests for the canonical ticket lifecycle (booley.ticket_board.lifecycle).

Two jobs here:
  1. Pin the state machine's own invariants (dir/status pairing, transition
     legality, user-move gate).
  2. Regression-guard the consolidation: assert the values now DERIVED from
     TicketState are byte-for-byte identical to the literals they replaced,
     so the refactor provably changed no behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.ticket_board.lifecycle import (
    REQUIRED_BOARD_STATES,
    SETTLED_STATES,
    SETTLED_STATUSES,
    STATE_BY_DIR,
    STATE_BY_STATUS,
    TRANSITIONS,
    USER_BOARD_MOVES,
    TicketState,
    board_relative_document_path,
    board_root,
    board_target_choices,
    can_transition,
    document_stage,
    document_state,
    documents_in_state,
    format_transition_error,
    format_user_board_moves,
    is_user_board_move,
    iter_board_documents,
    locate_document,
    parse_board_target,
    required_board_directories,
    state_directory,
    ticket_document_path,
)


class TestTicketStateEnum:
    def test_dir_and_status_pairing(self):
        # The historical name skew, encoded once.
        assert TicketState.RUNNING.dir_name == "active"
        assert TicketState.RUNNING.status == "running"
        assert TicketState.QUEUED.dir_name == "queue"
        assert TicketState.QUEUED.status == "queued"

    def test_conversion_stage(self):
        assert TicketState.DRAFT.conversion_stage == "draft"
        for state in TicketState:
            if state is not TicketState.DRAFT:
                assert state.conversion_stage == "executable"

    def test_status_values_unique(self):
        statuses = [s.status for s in TicketState]
        assert len(statuses) == len(set(statuses))

    def test_dir_names_unique(self):
        dirs = [s.dir_name for s in TicketState]
        assert len(dirs) == len(set(dirs))

    def test_lookup_indexes_round_trip(self):
        for s in TicketState:
            assert STATE_BY_STATUS[s.status] is s
            assert STATE_BY_DIR[s.dir_name] is s

    @pytest.mark.parametrize(
        ("state", "terminal"),
        [
            (TicketState.DONE, True),
            (TicketState.ARCHIVED, True),
            (TicketState.REVIEW, False),  # awaits a human — NOT terminal
            (TicketState.RUNNING, False),
            (TicketState.QUEUED, False),
            (TicketState.BLOCKED, False),
        ],
    )
    def test_is_terminal(self, state, terminal):
        assert state.is_terminal is terminal


class TestDerivedConstantsMatchLegacy:
    """The refactor must not change any observable value."""

    def test_state_directories_match_legacy(self, tmp_path):
        assert [
            state_directory(tmp_path, s).relative_to(tmp_path).as_posix() for s in TicketState
        ] == [
            "board/drafts",
            "board/queue",
            "board/waiting",
            "board/active",
            "board/blocked",
            "board/review",
            "board/done",
            "board/archived",
        ]

    def test_required_board_directories_match_legacy(self, tmp_path):
        # Legacy init_cmd.BOARD_STATES / doctor.required_states (set-equal;
        # order was never load-bearing — both were used only to build dirs).
        names = {path.name for path in required_board_directories(tmp_path)}
        assert names == {"drafts", "queue", "active", "review", "done", "blocked", "waiting"}
        assert TicketState.ARCHIVED not in REQUIRED_BOARD_STATES

    def test_settled_statuses_matches_legacy(self):
        assert frozenset({"done", "review"}) == SETTLED_STATUSES
        assert frozenset({TicketState.DONE, TicketState.REVIEW}) == SETTLED_STATES


class TestTransitions:
    def test_known_legal_edges(self):
        assert can_transition(TicketState.DRAFT, TicketState.QUEUED)
        assert can_transition(TicketState.QUEUED, TicketState.RUNNING)
        assert can_transition(TicketState.RUNNING, TicketState.REVIEW)
        assert can_transition(TicketState.RUNNING, TicketState.BLOCKED)
        assert can_transition(TicketState.BLOCKED, TicketState.QUEUED)
        assert can_transition(TicketState.BLOCKED, TicketState.RUNNING)
        assert can_transition(TicketState.REVIEW, TicketState.DONE)
        assert can_transition(TicketState.WAITING, TicketState.QUEUED)
        assert can_transition(TicketState.DONE, TicketState.ARCHIVED)
        assert can_transition(TicketState.RUNNING, TicketState.RUNNING)  # resume

    def test_known_illegal_edges(self):
        assert not can_transition(TicketState.DONE, TicketState.QUEUED)
        assert not can_transition(TicketState.DRAFT, TicketState.RUNNING)
        assert not can_transition(TicketState.ARCHIVED, TicketState.DONE)
        assert not can_transition(TicketState.BLOCKED, TicketState.DONE)
        assert not can_transition(TicketState.REVIEW, TicketState.QUEUED)

    def test_transition_error_lists_allowed_destinations(self):
        error = format_transition_error(TicketState.BLOCKED, TicketState.DONE)
        assert error == (
            "illegal ticket transition blocked -> done; legal from blocked: queued, review, running"
        )

    def test_review_only_has_normal_transition_to_done(self):
        assert TRANSITIONS[TicketState.REVIEW] == frozenset({TicketState.DONE})

    def test_archived_is_a_sink(self):
        assert TRANSITIONS[TicketState.ARCHIVED] == frozenset()

    def test_every_state_has_an_entry(self):
        assert set(TRANSITIONS) == set(TicketState)

    def test_transition_targets_are_reachable_states(self):
        # No edge points at a state that isn't in the enum.
        for targets in TRANSITIONS.values():
            for t in targets:
                assert isinstance(t, TicketState)


class TestUserBoardMoves:
    def test_user_moves_are_subset_of_transitions(self):
        for src, dst in USER_BOARD_MOVES:
            assert can_transition(src, dst), f"{src} -> {dst} not in graph"

    def test_is_user_board_move(self):
        assert is_user_board_move(TicketState.DRAFT, TicketState.QUEUED)
        assert is_user_board_move(TicketState.REVIEW, TicketState.DONE)
        # legal harness edge, but not a user-triggerable board move:
        assert can_transition(TicketState.QUEUED, TicketState.RUNNING)
        assert not is_user_board_move(TicketState.QUEUED, TicketState.RUNNING)

    def test_format_matches_legacy_help_string(self):
        assert (
            format_user_board_moves()
            == "draft->queue, blocked->queue, review->done, running->queue"
        )


class TestBoardLayout:
    """The board-layout seam: the only code that knows ``board/<state>`` paths."""

    @staticmethod
    def _write(tickets: Path, slug: str, state: TicketState) -> Path:
        path = ticket_document_path(tickets, slug, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n", encoding="utf-8")
        return path

    def test_document_path(self, tmp_path):
        assert ticket_document_path(tmp_path, "t1", TicketState.RUNNING) == (
            tmp_path / "board" / "active" / "t1.md"
        )
        assert board_root(tmp_path) == tmp_path / "board"

    def test_document_state_round_trips_every_state(self, tmp_path):
        for state in TicketState:
            assert document_state(tmp_path, ticket_document_path(tmp_path, "t1", state)) is state

    @pytest.mark.parametrize(
        "relative",
        [
            "logs/t1/ticket.md",  # runtime snapshot
            "logs/drafts/ticket.md",  # a slug named like a state directory
            "board/unknown/t1.md",  # not a state directory
            "board/queue/t1.txt",  # not a Ticket document
            "other/queue/t1.md",  # a state-named directory off the board
        ],
    )
    def test_document_state_rejects_non_board_paths(self, tmp_path, relative):
        assert document_state(tmp_path, tmp_path / relative) is None

    def test_document_state_rejects_another_board(self, tmp_path):
        other = tmp_path / "other"
        path = ticket_document_path(other, "t1", TicketState.QUEUED)
        assert document_state(tmp_path / "mine", path) is None

    def test_board_relative_document_path(self, tmp_path):
        relative = board_relative_document_path("t1", TicketState.DONE)
        assert relative == Path("board", "done", "t1.md")
        assert tmp_path / relative == ticket_document_path(tmp_path, "t1", TicketState.DONE)

    def test_document_state_accepts_board_through_bind_mount(self, tmp_path, monkeypatch):
        # A bind mount shows one directory at two paths that resolve() cannot
        # unify; samefile() is the only check that sees they are the same.
        mount = tmp_path / "mount"
        real = tmp_path / "real"
        path = self._write(mount, "t1", TicketState.WAITING)
        board_root(real).mkdir(parents=True)
        mount_board = board_root(mount).resolve()
        real_board = board_root(real).resolve()

        def samefile(self: Path, other: Path) -> bool:
            return {self.resolve(), Path(other).resolve()} == {mount_board, real_board}

        monkeypatch.setattr(Path, "samefile", samefile)
        assert document_state(real, path) is TicketState.WAITING

    def test_document_state_propagates_unreadable_board(self, tmp_path, monkeypatch):
        path = self._write(tmp_path / "elsewhere", "t1", TicketState.QUEUED)
        board_root(tmp_path / "mine").mkdir(parents=True)

        def samefile(self: Path, other: Path) -> bool:
            raise PermissionError(13, "Permission denied", str(other))

        monkeypatch.setattr(Path, "samefile", samefile)
        with pytest.raises(PermissionError):
            document_state(tmp_path / "mine", path)

    def test_document_stage_uses_state_on_board(self, tmp_path):
        for state in TicketState:
            path = ticket_document_path(tmp_path, "t1", state)
            for off_board in ("draft", "executable"):
                stage = document_stage(tmp_path, path, off_board=off_board)
                assert stage == state.conversion_stage

    @pytest.mark.parametrize("off_board", ["draft", "executable"])
    def test_document_stage_off_board_uses_caller_default(self, tmp_path, off_board):
        path = tmp_path / "logs" / "t1" / "ticket.md"
        assert document_stage(tmp_path, path, off_board=off_board) == off_board

    def test_document_state_accepts_unnormalized_spelling(self, tmp_path):
        path = tmp_path / "x" / ".." / "board" / "queue" / "t1.md"
        assert document_state(tmp_path, path) is TicketState.QUEUED

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_document_state_accepts_board_through_alias(self, tmp_path):
        real = tmp_path / "real"
        path = self._write(real, "t1", TicketState.BLOCKED)
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        assert document_state(alias, path) is TicketState.BLOCKED

    def test_documents_in_state_sorted_and_missing_dir(self, tmp_path):
        assert documents_in_state(tmp_path, TicketState.DONE) == []
        second = self._write(tmp_path, "b", TicketState.DONE)
        first = self._write(tmp_path, "a", TicketState.DONE)
        (state_directory(tmp_path, TicketState.DONE) / "notes.txt").write_text("x")
        assert documents_in_state(tmp_path, TicketState.DONE) == [first, second]

    def test_iter_board_documents_lifecycle_order(self, tmp_path):
        done = self._write(tmp_path, "a", TicketState.DONE)
        draft = self._write(tmp_path, "z", TicketState.DRAFT)
        running = self._write(tmp_path, "m", TicketState.RUNNING)
        assert list(iter_board_documents(tmp_path)) == [
            (draft, TicketState.DRAFT),
            (running, TicketState.RUNNING),
            (done, TicketState.DONE),
        ]

    def test_locate_document(self, tmp_path):
        assert locate_document(tmp_path, "t1") is None
        path = self._write(tmp_path, "t1", TicketState.REVIEW)
        assert locate_document(tmp_path, "t1") == (path, TicketState.REVIEW)

    def test_locate_document_prefers_earliest_state(self, tmp_path):
        queued = self._write(tmp_path, "t1", TicketState.QUEUED)
        self._write(tmp_path, "t1", TicketState.DONE)
        assert locate_document(tmp_path, "t1") == (queued, TicketState.QUEUED)

    def test_locate_document_never_traverses(self, tmp_path):
        self._write(tmp_path, "t1", TicketState.QUEUED)
        (tmp_path / "escape.md").write_text("x")
        assert locate_document(tmp_path, "../../escape") is None
        assert locate_document(tmp_path, "T1") is None


class TestBoardTargets:
    @pytest.mark.parametrize("state", list(TicketState))
    def test_parse_both_spellings(self, state):
        assert parse_board_target(state.dir_name) is state
        assert parse_board_target(f"board/{state.dir_name}") is state

    @pytest.mark.parametrize("value", ["", "running", "board/", "board/running", "x/queue"])
    def test_parse_rejects_unknown(self, value):
        assert parse_board_target(value) is None

    def test_choices_match_legacy_cli(self):
        dirs = [s.dir_name for s in TicketState]
        assert board_target_choices() == [f"board/{d}" for d in dirs] + dirs
