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
    SETTLED_STATES,
    SETTLED_STATUSES,
    STATE_BY_DIR,
    STATE_BY_STATUS,
    TRANSITIONS,
    USER_BOARD_MOVES,
    TicketState,
    board_target_choices,
    can_transition,
    format_transition_error,
    format_user_board_moves,
    is_user_board_move,
    parse_board_target,
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
        assert can_transition(TicketState.RUNNING, TicketState.RUNNING)  # resume

    def test_known_illegal_edges(self):
        assert not can_transition(TicketState.DONE, TicketState.QUEUED)
        assert not can_transition(TicketState.DRAFT, TicketState.RUNNING)
        assert not can_transition(TicketState.ARCHIVED, TicketState.DONE)
        # Done closes into Ticket History; there is no done -> archived edge.
        assert not can_transition(TicketState.DONE, TicketState.ARCHIVED)
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

    def test_done_is_a_sink(self):
        assert TRANSITIONS[TicketState.DONE] == frozenset()

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
