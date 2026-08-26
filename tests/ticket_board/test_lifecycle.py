"""Tests for the canonical ticket lifecycle (booley.ticket_board.lifecycle).

Two jobs here:
  1. Pin the state machine's own invariants (dir/status pairing, transition
     legality, user-move gate).
  2. Regression-guard the consolidation: assert the values now DERIVED from
     TicketState are byte-for-byte identical to the literals they replaced,
     so the refactor provably changed no behaviour.
"""

from __future__ import annotations

import pytest

from booley.ticket_board.lifecycle import (
    REQUIRED_BOARD_DIRS,
    SETTLED_STATES,
    SETTLED_STATUSES,
    STATE_BY_DIR,
    STATE_BY_STATUS,
    TRANSITIONS,
    USER_BOARD_MOVES,
    TicketState,
    can_transition,
    format_transition_error,
    format_user_board_moves,
    is_user_board_move,
)


class TestTicketStateEnum:
    def test_dir_and_status_pairing(self):
        # The historical name skew, encoded once.
        assert TicketState.RUNNING.dir_name == "active"
        assert TicketState.RUNNING.status == "running"
        assert TicketState.QUEUED.dir_name == "queue"
        assert TicketState.QUEUED.status == "queued"

    def test_board_dir_prefix(self):
        assert TicketState.RUNNING.board_dir == "board/active"
        assert TicketState.DRAFT.board_dir == "board/drafts"

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

    def test_dir_status_map_matches_legacy(self):
        from booley.ticket_board.constants import DIR_STATUS_MAP

        assert DIR_STATUS_MAP == {
            "board/drafts": "draft",
            "board/queue": "queued",
            "board/waiting": "waiting",
            "board/active": "running",
            "board/blocked": "blocked",
            "board/review": "review",
            "board/done": "done",
            "board/archived": "archived",
        }

    def test_ticket_dirs_matches_legacy(self):
        from booley.ticket_board.constants import TICKET_DIRS

        assert TICKET_DIRS == [
            "board/drafts",
            "board/queue",
            "board/waiting",
            "board/active",
            "board/blocked",
            "board/review",
            "board/done",
            "board/archived",
        ]

    def test_required_board_dirs_matches_legacy(self):
        # Legacy init_cmd.BOARD_STATES / doctor.required_states (set-equal;
        # order was never load-bearing — both were used only to build dirs).
        assert set(REQUIRED_BOARD_DIRS) == {
            "drafts",
            "queue",
            "active",
            "review",
            "done",
            "blocked",
            "waiting",
        }
        assert "archived" not in REQUIRED_BOARD_DIRS

    def test_settled_statuses_matches_legacy(self):
        assert frozenset({"done", "review"}) == SETTLED_STATUSES
        assert frozenset({TicketState.DONE, TicketState.REVIEW}) == SETTLED_STATES

    def test_board_states_alias_is_required_dirs(self):
        from booley.harness.init_cmd import BOARD_STATES

        assert BOARD_STATES == REQUIRED_BOARD_DIRS


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
            "illegal ticket transition blocked -> done; legal from blocked: queued, running"
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
