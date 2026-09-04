"""Acceptance Journal model and persistence-boundary tests."""

from __future__ import annotations

import pytest

from booley.ticket_board.acceptance_journal._model import (
    AcceptanceJournal,
    AcceptanceJournalError,
    Candidate,
    JournalState,
    initial_journal,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _journal() -> AcceptanceJournal:
    return initial_journal(
        "change-target",
        [
            {
                "role": "outer",
                "sealed_sha": _SHA_A,
                "ticket_ref": "refs/heads/change-target",
                "destination_ref": "refs/heads/main",
                "destination_sha": _SHA_B,
            }
        ],
        cleanup=False,
    )


def test_record_owns_legal_transitions_without_mutating_prior_values() -> None:
    initial = _journal()
    sourced = initial.with_sources({"outer": _SHA_A})
    candidate = Candidate(
        prepared_sha=_SHA_A,
        finalized_sha=_SHA_A,
        staging_ref=f"refs/booley/acceptance/{initial.transaction}/outer",
        expected_destination_sha=_SHA_B,
    )
    prepared = sourced.with_candidate("outer", candidate).mark_prepared()
    published = prepared.mark_published("outer")
    accepted = published.mark_accepted()
    done = accepted.mark_done()

    assert initial.state is JournalState.INITIALIZING
    assert initial.sources == {}
    assert done.state is JournalState.DONE


def test_record_rejects_illegal_transition() -> None:
    with pytest.raises(AcceptanceJournalError, match="acceptance requires outer publication"):
        _journal().mark_accepted()


def test_record_collections_are_immutable() -> None:
    journal = _journal().with_sources({"outer": _SHA_A})

    with pytest.raises(TypeError):
        journal.sources["outer"] = _SHA_B  # type: ignore[index]
