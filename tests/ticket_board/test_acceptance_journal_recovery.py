"""Recovery tests driven through Acceptance Journal adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from booley.ticket_board import completion
from booley.ticket_board.acceptance_basis import BasisParticipant
from booley.ticket_board.acceptance_journal import _advance as acceptance_impl
from booley.ticket_board.acceptance_journal._repository import (
    FaultingAcceptanceRepositories,
    LocalAcceptanceRepositories,
    RepositoryBoundary,
)
from booley.ticket_board.acceptance_journal._store import (
    AcceptanceCheckpoint,
    FaultingAcceptanceStore,
    FileAcceptanceStore,
)
from booley.ticket_board.completion import complete_review_ticket
from tests.ticket_board.test_completion import (
    _git,
    _paired_completion,
    _Policy,
    _TicketIO,
)


def _install_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: FileAcceptanceStore | FaultingAcceptanceStore | None = None,
    repositories: LocalAcceptanceRepositories | FaultingAcceptanceRepositories | None = None,
) -> None:
    runner = acceptance_impl._AcceptanceRunner(
        store or FileAcceptanceStore(),
        repositories or LocalAcceptanceRepositories(),
    )
    monkeypatch.setattr(completion, "advance_acceptance", runner.advance)


def _assert_finished(
    root: Path,
    project: Path,
    tio: _TicketIO,
    participants: tuple[BasisParticipant, ...],
    *,
    cleanup: bool,
) -> None:
    assert tio.entry["status"] == "done"
    assert _git(root, "show", "main:design.txt") == "outer implementation"
    assert _git(project, "show", "main:design.txt") == "project implementation"
    if cleanup:
        assert acceptance_impl._ref_commit(root, participants[0].ticket_ref) is None
        assert acceptance_impl._ref_commit(project, participants[1].ticket_ref) is None
    path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "done"


@pytest.mark.parametrize(
    "fault_checkpoint",
    [
        AcceptanceCheckpoint.NORMALIZED,
        AcceptanceCheckpoint.SOURCES_PINNED,
        AcceptanceCheckpoint.CANDIDATES_PREPARED,
        AcceptanceCheckpoint.PREPARATION_COMPLETE,
        AcceptanceCheckpoint.PROJECT_PUBLISHED,
        AcceptanceCheckpoint.OUTER_PUBLISHED,
        AcceptanceCheckpoint.ACCEPTED,
        AcceptanceCheckpoint.PROJECT_CLEANED,
        AcceptanceCheckpoint.OUTER_CLEANED,
        AcceptanceCheckpoint.DONE,
    ],
)
@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_survives_every_semantic_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_checkpoint: AcceptanceCheckpoint,
    timing: Literal["before", "after"],
) -> None:
    root, project, tio, participants = _paired_completion(tmp_path, monkeypatch)
    store = FaultingAcceptanceStore(FileAcceptanceStore(), fault_checkpoint, timing)
    _install_runner(monkeypatch, store=store)
    complete_review_ticket(tio, "change-target", _Policy(cleanup=True))
    assert store.triggered is True

    _install_runner(monkeypatch)
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    _assert_finished(root, project, tio, participants, cleanup=True)


@pytest.mark.parametrize(
    ("boundary", "cleanup"),
    [
        (RepositoryBoundary.PREPARATION, False),
        (RepositoryBoundary.PUBLICATION, False),
        (RepositoryBoundary.RETIREMENT, True),
    ],
)
@pytest.mark.parametrize("role", ["project", "outer"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_survives_each_repository_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: RepositoryBoundary,
    cleanup: bool,
    role: str,
    timing: Literal["before", "after"],
) -> None:
    root, project, tio, participants = _paired_completion(tmp_path, monkeypatch)
    repositories = FaultingAcceptanceRepositories(
        LocalAcceptanceRepositories(),
        boundary,
        role,
        timing,
        completion.CompletionError(f"{timing} {role} {boundary}"),
    )
    _install_runner(monkeypatch, repositories=repositories)
    complete_review_ticket(tio, "change-target", _Policy(cleanup=cleanup))
    assert repositories.triggered is True

    _install_runner(monkeypatch)
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=cleanup)) is True
    _assert_finished(root, project, tio, participants, cleanup=cleanup)
