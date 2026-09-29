"""Journal path canonicalization for enqueue publication.

The enqueue journal records the Ticket's one board document path (ADR 0065); on
recovery that path must still name this slug's document on this Project's board.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.ticket_board import enqueue_publication
from booley.ticket_board.board_layout import board_root, ticket_document_path
from booley.ticket_board.enqueue_publication import EnqueuePublicationError


@pytest.fixture
def tickets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project" / ".booley_project"
    monkeypatch.setattr(enqueue_publication, "_transaction_project_dir", lambda _root: project)
    tickets = project / "tickets"
    board_root(tickets).mkdir(parents=True)
    return tickets


def _canonicalize(path: Path) -> Path:
    return enqueue_publication._canonicalize_document_path(Path("unused"), "t1", path)


def test_canonical_path_is_returned_unchanged(tickets: Path) -> None:
    path = ticket_document_path(tickets, "t1")
    assert _canonicalize(path) == path


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_alias_spelling_canonicalizes_to_the_project_board(tickets: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tickets, target_is_directory=True)
    path = ticket_document_path(alias, "t1")
    assert _canonicalize(path) == ticket_document_path(tickets, "t1")


@pytest.mark.parametrize(
    "relative",
    ["board/other.md", "board/queue/t1.md", "board/t1.txt", "logs/t1.md"],
)
def test_wrong_slug_or_location_is_invalid(tickets: Path, relative: str) -> None:
    with pytest.raises(EnqueuePublicationError, match="document path is invalid"):
        _canonicalize(tickets / relative)


def test_path_on_another_board_is_invalid(tickets: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    board_root(other).mkdir(parents=True)
    with pytest.raises(EnqueuePublicationError, match="document path is invalid"):
        _canonicalize(ticket_document_path(other, "t1"))


def test_unreadable_board_is_reported_as_unavailable(
    tickets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ticket_document_path(tmp_path / "other", "t1")
    path.parent.mkdir(parents=True)

    def samefile(self: Path, other: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(other))

    monkeypatch.setattr(Path, "samefile", samefile)
    with pytest.raises(EnqueuePublicationError, match="board is unavailable"):
        _canonicalize(path)
