"""Public draft-transition contracts plus crash-checkpoint fault injection.

Direct private-helper tests are limited to deterministic filesystem and Git recovery
states that cannot be safely produced through the complete public transaction.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from booley.ticket_board import (
    draft_transition,
    workspace_ops,
)
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
)


def _completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


def _draft_journal(tmp_path: Path) -> draft_transition.DraftTransitionJournal:
    basis = AcceptanceBasis((_participant(),)).as_dict()
    return draft_transition.DraftTransitionJournal(
        1,
        "0" * 32,
        "ticket",
        "initializing",
        basis,
        AcceptanceBasis.from_mapping(basis).basis_id,
        str(tmp_path / "tickets/board/blocked/ticket.md"),
        "1" * 64,
        str(tmp_path / "tickets/board/drafts/ticket.md"),
        "2" * 64,
        "0123456789abcdef",
        "3" * 64,
        str(tmp_path / "logs/ticket/runs/001"),
        False,
    )


def test_draft_journal_parser_and_validation_reject_noncanonical_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "journal.json"
    monkeypatch.setattr(draft_transition, "_journal_path", lambda *_args: journal_path)
    journal_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(draft_transition.DraftTransitionError, match="journal is unreadable"):
        draft_transition.return_to_draft(
            tmp_path,
            tmp_path / "ticket.md",
            "ticket",
            status="blocked",
            logs_dir=tmp_path / "logs",
            append_transition=lambda _message: None,
        )
    journal = _draft_journal(tmp_path)
    monkeypatch.setattr(
        draft_transition,
        "resolve_checkout_project_dir",
        lambda _root: tmp_path,
    )
    monkeypatch.setattr(
        draft_transition,
        "_operation_dir",
        lambda *_args: tmp_path / "operations" / journal.operation_id,
    )
    monkeypatch.setattr(
        draft_transition,
        "_transition_root",
        lambda _root: tmp_path / "operations",
    )
    for changed in (
        replace(journal, schema=2),
        replace(journal, operation_id="bad"),
        replace(journal, basis={}),
        replace(journal, basis_id="wrong"),
        replace(journal, draft_ticket="wrong"),
        replace(journal, generation="wrong"),
        replace(journal, blocked_sha256="wrong"),
        replace(journal, blocked_ticket="wrong"),
        replace(journal, archive_dir=str(tmp_path / "wrong")),
    ):
        journal_path.write_text(json.dumps(asdict(changed)) + "\n", encoding="utf-8")
        with pytest.raises(draft_transition.DraftTransitionError):
            draft_transition.return_to_draft(
                tmp_path,
                tmp_path / "ticket.md",
                "ticket",
                status="blocked",
                logs_dir=tmp_path / "logs",
                append_transition=lambda _message: None,
            )


def test_draft_cutover_file_helpers_reject_conflicts_and_preserve_idempotence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _draft_journal(tmp_path)
    operation = tmp_path / "operation"
    monkeypatch.setattr(draft_transition, "_operation_dir", lambda *_args: operation)
    blocked = Path(journal.blocked_ticket)
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b"blocked")
    journal = replace(journal, blocked_sha256=hashlib.sha256(b"blocked").hexdigest())
    backup = operation / "blocked.md"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"blocked")
    with pytest.raises(draft_transition.DraftTransitionError, match="both exist"):
        draft_transition._publish_board(tmp_path, journal)
    blocked.unlink()
    backup.unlink()
    with pytest.raises(draft_transition.DraftTransitionError, match="disappeared"):
        draft_transition._publish_board(tmp_path, journal)

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("value", encoding="utf-8")
    destination.write_text("value", encoding="utf-8")
    with pytest.raises(draft_transition.DraftTransitionError, match="both exist"):
        draft_transition._move_archive_entry(source, destination)
    source.unlink()
    draft_transition._move_archive_entry(source, destination)


def test_draft_transition_requires_blocked_basis_and_exact_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(draft_transition.DraftTransitionError, match="requires a blocked"):
        draft_transition.return_to_draft(
            tmp_path,
            tmp_path / "ticket.md",
            "ticket",
            status="queue",
            logs_dir=tmp_path,
            append_transition=lambda _message: None,
        )
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(
        draft_transition,
        "load_acceptance_basis",
        lambda *_args: (_ for _ in ()).throw(AcceptanceBasisError("invalid basis")),
    )
    with pytest.raises(draft_transition.DraftTransitionError, match="invalid basis"):
        draft_transition.return_to_draft(
            tmp_path,
            ticket,
            "ticket",
            status="blocked",
            logs_dir=tmp_path,
            append_transition=lambda _message: None,
        )
    with pytest.raises(draft_transition.DraftTransitionError, match="unavailable"):
        draft_transition._require_file(tmp_path / "missing", "0" * 64, "draft")
    ticket.write_text("changed", encoding="utf-8")
    with pytest.raises(draft_transition.DraftTransitionError, match="changed unexpectedly"):
        draft_transition._require_file(ticket, "0" * 64, "draft")


def test_draft_git_and_worktree_helpers_reject_missing_or_conflicting_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        draft_transition.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="bad ref"),
    )
    with pytest.raises(draft_transition.DraftTransitionError, match="bad ref"):
        draft_transition._git(tmp_path, "status")
    monkeypatch.setattr(draft_transition, "_git", lambda *_args: "")
    with pytest.raises(draft_transition.DraftTransitionError, match="is unavailable"):
        draft_transition._worktree_for_ref(tmp_path, "refs/heads/ticket")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    monkeypatch.setattr(draft_transition, "_worktree_for_ref", lambda *_args: source)
    with pytest.raises(draft_transition.DraftTransitionError, match="destination already exists"):
        draft_transition._move_worktree(tmp_path, "refs/heads/ticket", destination)


def test_draft_relocation_requires_paired_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = replace(_draft_journal(tmp_path), has_project=True)
    project = _participant("project")
    basis = AcceptanceBasis((_participant(), project))
    monkeypatch.setattr(draft_transition, "resolve_inner_project_repo", lambda _root: None)
    with pytest.raises(draft_transition.DraftTransitionError, match="paired project"):
        draft_transition._relocate_worktrees(tmp_path, journal, basis)


def test_draft_published_transition_rejects_changed_worktree_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _draft_journal(tmp_path)
    monkeypatch.setattr(draft_transition, "_require_file", lambda *_args: None)
    monkeypatch.setattr(
        draft_transition,
        "_published_worktrees",
        lambda *_args: workspace_ops.AuthoringWorkspace(
            tmp_path / "expected", None, "a" * 40, "", journal.generation
        ),
    )
    monkeypatch.setattr(draft_transition, "_worktree_for_ref", lambda *_args: tmp_path / "actual")
    with pytest.raises(draft_transition.DraftTransitionError, match="identity changed"):
        draft_transition._finish_published_transition(
            tmp_path, journal, AcceptanceBasis((_participant(),))
        )
