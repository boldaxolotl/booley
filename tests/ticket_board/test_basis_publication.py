"""Focused boundary tests for Acceptance Basis helper modules."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from booley.ticket_board import (
    acceptance_targets,
    basis_publication,
)
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
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


def _publication_participant(role: str = "outer") -> basis_publication.ParticipantPreparation:
    return basis_publication.ParticipantPreparation(
        role,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "a" * 40,
        "b" * 40,
        "c" * 40,
        "publish basis",
    )


def _publication_journal() -> basis_publication.BasisPublicationJournal:
    return basis_publication.BasisPublicationJournal(
        1,
        "0" * 32,
        "ticket",
        "1" * 64,
        "2" * 64,
        (_publication_participant(),),
        (),
        (),
        {},
        (),
    )


def test_basis_publication_parsers_reject_invalid_rows() -> None:
    journal = _publication_journal()
    payload = {
        "schema": journal.schema,
        "operation_id": journal.operation_id,
        "slug": journal.slug,
        "source_sha256": journal.source_sha256,
        "effective_sha256": journal.effective_sha256,
        "participants": [vars(journal.participants[0])],
        "bindings": [],
        "removal_targets": [],
        "prepared": {},
        "published": [],
    }
    assert basis_publication._parse_journal(payload) == journal
    for mutate in (
        lambda value: value.update(extra=True),
        lambda value: value.update(schema=2),
        lambda value: value.update(source_sha256="bad"),
        lambda value: value.update(participants=[{}]),
        lambda value: value.update(removal_targets=[3]),
        lambda value: value.update(prepared={"outer": "bad"}),
    ):
        changed = dict(payload)
        mutate(changed)
        with pytest.raises(basis_publication.BoundaryError):
            basis_publication._parse_journal(changed)


def test_basis_publication_checkpoint_validation_rejects_bad_roles_and_order() -> None:
    journal = _publication_journal()
    with pytest.raises(basis_publication.BasisPublicationError, match="another Ticket"):
        basis_publication._validate_journal(journal, "other")
    with pytest.raises(basis_publication.BasisPublicationError, match="participants"):
        basis_publication._validate_journal(replace(journal, participants=()), "ticket")
    with pytest.raises(basis_publication.BasisPublicationError, match="checkpoints"):
        basis_publication._validate_journal(replace(journal, published=("outer",)), "ticket")
    paired = replace(
        journal,
        participants=(_publication_participant("outer"), _publication_participant("project")),
        prepared={"outer": "a" * 40, "project": "b" * 40},
        published=("outer",),
    )
    with pytest.raises(basis_publication.BasisPublicationError, match="order"):
        basis_publication._validate_journal(paired, "ticket")


def test_new_basis_publication_requires_complete_inputs() -> None:
    participant = _publication_participant()
    with pytest.raises(basis_publication.BasisPublicationError, match="operation ID"):
        basis_publication._new_journal("ticket", "1" * 64, "2" * 64, None, (participant,), (), ())
    with pytest.raises(basis_publication.BasisPublicationError, match="missing prepared"):
        basis_publication._new_journal("ticket", "1" * 64, "2" * 64, "0" * 32, None, (), ())


def test_basis_publication_resume_and_repository_inputs_are_immutable() -> None:
    journal = _publication_journal()
    for kwargs in (
        {"source_sha256": "changed"},
        {"effective_sha256": "changed"},
        {"participants": ()},
        {
            "bindings": (
                acceptance_targets.AcceptanceTargetBinding(
                    "sim", "criteria.mandatory.sim_pass", "base", "candidate", "base", "candidate"
                ),
            )
        },
        {"removal_targets": ("target",)},
    ):
        values = {
            "source_sha256": journal.source_sha256,
            "effective_sha256": journal.effective_sha256,
            "participants": journal.participants,
            "bindings": (),
            "removal_targets": (),
        }
        values.update(kwargs)
        with pytest.raises(basis_publication.BasisPublicationError):
            basis_publication._validate_resume(journal, **values)
    with pytest.raises(basis_publication.BasisPublicationError, match="repositories"):
        basis_publication._validate_repositories(journal, {})


def test_basis_publication_recovers_existing_commit_and_rejects_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _publication_participant()
    monkeypatch.setattr(
        basis_publication,
        "_git",
        lambda *_args: _completed("git", stdout="d" * 40),
    )
    monkeypatch.setattr(basis_publication, "_validate_prepared_commit", lambda *_args: None)
    assert basis_publication._recover_or_create_commit(tmp_path, "0" * 32, plan) == "d" * 40
    monkeypatch.setattr(
        basis_publication,
        "_git",
        lambda *_args: _completed("git", returncode=2, stderr="ref locked"),
    )
    with pytest.raises(basis_publication.BasisPublicationError, match="ref locked"):
        basis_publication._recover_or_create_commit(tmp_path, "0" * 32, plan)


def test_basis_publication_rejects_mismatched_commit_and_ticket_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _publication_participant()
    monkeypatch.setattr(basis_publication, "_require_git", lambda *_args: "wrong\nparent")
    with pytest.raises(basis_publication.BasisPublicationError, match="tree and parent"):
        basis_publication._validate_prepared_commit(tmp_path, "refs/temp", plan, "d" * 40)
    monkeypatch.setattr(basis_publication, "_require_git", lambda *_args: "e" * 40)
    with pytest.raises(basis_publication.BasisPublicationError, match="changed during"):
        basis_publication._publish_ticket_ref(tmp_path, plan, "d" * 40)


def test_basis_keepalives_reject_changed_and_uninspectable_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    monkeypatch.setattr(
        basis_publication,
        "_git",
        lambda *_args: _completed("git", stdout="c" * 40),
    )
    with pytest.raises(basis_publication.BasisPublicationError, match=r"keepalive .* changed"):
        basis_publication._publish_basis_keepalives({"outer": tmp_path}, basis)
    monkeypatch.setattr(
        basis_publication,
        "_git",
        lambda *_args: _completed("git", returncode=2, stderr="unavailable"),
    )
    with pytest.raises(basis_publication.BasisPublicationError, match="could not inspect"):
        basis_publication._publish_basis_keepalives({"outer": tmp_path}, basis)


def test_temporary_keepalive_and_finish_validation_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = replace(_publication_journal(), prepared={"outer": "d" * 40})
    monkeypatch.setattr(
        basis_publication,
        "_git",
        lambda *_args: _completed("git", returncode=2, stderr="unavailable"),
    )
    with pytest.raises(basis_publication.BasisPublicationError, match="could not inspect"):
        basis_publication._retire_temporary_keepalives({"outer": tmp_path}, journal)
    monkeypatch.setattr(
        basis_publication,
        "_git",
        lambda *_args: _completed("git", stdout="e" * 40),
    )
    with pytest.raises(basis_publication.BasisPublicationError, match=r"temporary .* changed"):
        basis_publication._retire_temporary_keepalives({"outer": tmp_path}, journal)
    monkeypatch.setattr(basis_publication, "load_basis_publication", lambda *_args: None)
    basis_publication.finish_basis_publication(tmp_path, "ticket", "0" * 32)
    monkeypatch.setattr(basis_publication, "load_basis_publication", lambda *_args: journal)
    with pytest.raises(basis_publication.BasisPublicationError, match="operations disagree"):
        basis_publication.finish_basis_publication(tmp_path, "ticket", "f" * 32)
    with pytest.raises(basis_publication.BasisPublicationError, match="incompletely published"):
        basis_publication.finish_basis_publication(tmp_path, "ticket", "0" * 32)
