"""Journal path canonicalization for enqueue publication.

The enqueue journal records the draft source and executable destination; on
recovery each recorded path must still name this slug's document in an
allowed state on this Project's board.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.ticket_board import enqueue_publication
from booley.ticket_board.enqueue_publication import EnqueuePublicationError
from booley.ticket_board.lifecycle import TicketState, ticket_document_path


@pytest.fixture
def tickets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project" / ".booley_project"
    monkeypatch.setattr(enqueue_publication, "_transaction_project_dir", lambda _root: project)
    tickets = project / "tickets"
    for state in (TicketState.DRAFT, TicketState.QUEUED):
        ticket_document_path(tickets, "t1", state).parent.mkdir(parents=True)
    return tickets


def _canonicalize(path: Path, states: tuple[TicketState, ...]) -> Path:
    return enqueue_publication._canonicalize_board_path(
        Path("unused"), "t1", path, states, "destination"
    )


def test_canonical_path_is_returned_unchanged(tickets: Path) -> None:
    path = ticket_document_path(tickets, "t1", TicketState.QUEUED)
    assert _canonicalize(path, (TicketState.QUEUED,)) == path


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_alias_spelling_canonicalizes_to_the_project_board(tickets: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tickets, target_is_directory=True)
    path = ticket_document_path(alias, "t1", TicketState.QUEUED)
    assert _canonicalize(path, (TicketState.QUEUED,)) == ticket_document_path(
        tickets, "t1", TicketState.QUEUED
    )


@pytest.mark.parametrize(
    ("slug", "state"),
    [("other", TicketState.QUEUED), ("t1", TicketState.DRAFT)],
)
def test_wrong_slug_or_state_is_invalid(tickets: Path, slug: str, state: TicketState) -> None:
    path = ticket_document_path(tickets, slug, state)
    with pytest.raises(EnqueuePublicationError, match="destination path is invalid"):
        _canonicalize(path, (TicketState.QUEUED,))


def test_path_on_another_board_is_invalid(tickets: Path, tmp_path: Path) -> None:
    path = ticket_document_path(tmp_path / "other", "t1", TicketState.QUEUED)
    with pytest.raises(EnqueuePublicationError, match="destination path is invalid"):
        _canonicalize(path, (TicketState.QUEUED,))


def test_unreadable_board_is_reported_as_unavailable(
    tickets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ticket_document_path(tmp_path / "other", "t1", TicketState.QUEUED)
    path.parent.mkdir(parents=True)

    def samefile(self: Path, other: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(other))

    monkeypatch.setattr(Path, "samefile", samefile)
    with pytest.raises(EnqueuePublicationError, match="destination board is unavailable"):
        _canonicalize(path, (TicketState.QUEUED,))
