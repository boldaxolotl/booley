"""Focused boundary tests for Acceptance Basis helper modules."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.fusesoc import core_projection
from booley.ticket_board import (
    acceptance_basis,
    acceptance_targets,
    basis_publication,
    draft_transition,
    enqueue_publication,
    operations,
    readiness,
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


def _attachment(tmp_path: Path) -> workspace_ops._OpenAttachment:
    return workspace_ops._OpenAttachment(
        tmp_path / "repository",
        tmp_path / "worktree",
        "ticket",
        "a" * 40,
    )


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


def _record() -> dict[str, object]:
    return acceptance_basis.authored_ticket_record(
        {
            "summary": "Ticket",
            "type": "feature",
            "branch": "main",
            "scope": [],
            "criteria": {"mandatory": {"review_rtl_bugs": True}},
        },
        "## Description\n\nTest.\n",
        (),
    )


def _enqueue_journal(tmp_path: Path) -> enqueue_publication.EnqueueJournal:
    basis = AcceptanceBasis((_participant(),)).as_dict()
    operation_id = "0" * 32
    digest = "1" * 64
    operation = tmp_path / "operation"
    return enqueue_publication.EnqueueJournal(
        1,
        operation_id,
        "ticket",
        "prepared",
        str(tmp_path / "tickets/board/drafts/ticket.md"),
        digest,
        str(tmp_path / "tickets/board/queue/ticket.md"),
        str(operation / "ticket.md"),
        "2" * 64,
        str(operation / "source.md"),
        False,
        "now",
        basis,
        {
            "operation_id": operation_id,
            "source_sha256": digest,
            "basis_id": AcceptanceBasis.from_mapping(basis).basis_id,
            "participants": basis["participants"],
        },
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


def test_enqueue_journal_parser_and_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(enqueue_publication.BoundaryError, match="invalid fields"):
        enqueue_publication._parse_enqueue_journal({})
    journal = _enqueue_journal(tmp_path)
    monkeypatch.setattr(
        enqueue_publication,
        "_operation_directory",
        lambda *_args: tmp_path / "operation",
    )
    monkeypatch.setattr(
        enqueue_publication,
        "resolve_checkout_project_dir",
        lambda _root: tmp_path,
    )
    enqueue_publication._validate_journal(tmp_path, "ticket", journal)
    for changed in (
        replace(journal, schema=2),
        replace(journal, operation_id="bad"),
        replace(journal, candidate="wrong"),
        replace(journal, backup="wrong"),
        replace(journal, source="wrong"),
        replace(journal, destination="wrong"),
    ):
        with pytest.raises(enqueue_publication.EnqueuePublicationError):
            enqueue_publication._validate_journal(tmp_path, "ticket", changed)


def test_enqueue_payload_validation_rejects_each_bound_identity(tmp_path: Path) -> None:
    journal = _enqueue_journal(tmp_path)
    for changed in (
        replace(journal, source_sha256="bad"),
        replace(journal, basis={}),
        replace(journal, receipt={**journal.receipt, "operation_id": "f" * 32}),
        replace(journal, receipt={**journal.receipt, "source_sha256": "f" * 64}),
        replace(journal, receipt={**journal.receipt, "basis_id": "f" * 64}),
        replace(journal, receipt={**journal.receipt, "participants": []}),
    ):
        with pytest.raises(enqueue_publication.EnqueuePublicationError):
            enqueue_publication._validate_journal_payload(changed)


def test_enqueue_cutover_helpers_are_idempotent_and_fail_closed(tmp_path: Path) -> None:
    journal = _enqueue_journal(tmp_path)
    source = Path(journal.source)
    backup = Path(journal.backup)
    destination = Path(journal.destination)
    candidate = Path(journal.candidate)
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"source")
    journal = replace(journal, source_sha256=enqueue_publication._digest(b"source"))
    enqueue_publication._preserve_source(source, backup, destination, journal)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="both exist"):
        enqueue_publication._preserve_source(source, backup, destination, journal)
    source.unlink()
    backup.unlink()
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="disappeared"):
        enqueue_publication._preserve_source(source, backup, destination, journal)

    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"candidate")
    expected = enqueue_publication._digest(b"candidate")
    enqueue_publication._publish_candidate(candidate, destination, expected)
    enqueue_publication._publish_candidate(candidate, destination, expected)
    destination.write_bytes(b"changed")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="changed unexpectedly"):
        enqueue_publication._require_digest(destination, expected, "queued Ticket")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="unavailable"):
        enqueue_publication._require_digest(tmp_path / "missing", expected, "missing")


def test_draft_journal_parser_and_validation_reject_noncanonical_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(draft_transition.BoundaryError, match="invalid fields"):
        draft_transition._parse_journal({})
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
    draft_transition._validate_journal(tmp_path, tmp_path / "logs", "ticket", journal)
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
        with pytest.raises(draft_transition.DraftTransitionError):
            draft_transition._validate_journal(tmp_path, tmp_path / "logs", "ticket", changed)


def test_draft_cutover_file_helpers_reject_conflicts_and_preserve_idempotence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _draft_journal(tmp_path)
    operation = tmp_path / "operation"
    monkeypatch.setattr(draft_transition, "_operation_dir", lambda *_args: operation)
    blocked = Path(journal.blocked_ticket)
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b"blocked")
    journal = replace(journal, blocked_sha256=draft_transition._digest(b"blocked"))
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


def test_draft_transition_requires_blocked_basis_and_exact_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(draft_transition.DraftTransitionError, match="requires a blocked"):
        draft_transition._new_journal(
            tmp_path, tmp_path / "ticket.md", "ticket", "queue", tmp_path
        )
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(
        draft_transition,
        "load_acceptance_basis",
        lambda *_args: (_ for _ in ()).throw(AcceptanceBasisError("invalid basis")),
    )
    with pytest.raises(draft_transition.DraftTransitionError, match="invalid basis"):
        draft_transition._new_journal(tmp_path, ticket, "ticket", "blocked", tmp_path)
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


def test_reset_helpers_report_missing_basis_and_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = {"acceptance_basis": {"schema": 1}, "branch": "main"}
    assert operations._reset_ticket_branches(tmp_path, "ticket", entry, None) is False
    assert "authoritative Acceptance Basis is unavailable" in capsys.readouterr().err
    assert operations._preflight_reset_branches(tmp_path, "ticket", entry, None) is None
    assert "authoritative Acceptance Basis is unavailable" in capsys.readouterr().err
    monkeypatch.setattr(
        workspace_ops,
        "preflight_basis_reset",
        lambda *_args: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("cannot preflight")
        ),
    )
    assert (
        operations._preflight_reset_branches(
            tmp_path, "ticket", entry, AcceptanceBasis((_participant(),))
        )
        is None
    )
    assert "cannot preflight" in capsys.readouterr().err


def test_readiness_checkout_boundary_and_preparation_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    tickets = root / ".booley_project/tickets"
    assert readiness._validate_checkout_basis(root, tickets, "ticket", {}, "body") == []
    (root / ".git").mkdir(parents=True)
    assert (
        "legacy Target Contract"
        in readiness._validate_checkout_basis(
            root, tickets, "ticket", {"target_contract": {}}, "body"
        )[0]
    )
    assert readiness._validate_checkout_basis(root, tickets, "ticket", {}, "body") == [
        "executable Ticket has no Acceptance Basis"
    ]
    monkeypatch.setattr(
        readiness,
        "materialize_current_ticket_checkout",
        lambda *_args: root,
    )
    monkeypatch.setattr(
        readiness,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="prepare failed"),
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)
    with pytest.raises(AcceptanceBasisError, match="prepare failed"):
        readiness._validate_current_ticket_view(
            root,
            tickets / "ticket.md",
            "ticket",
            AcceptanceBasis((_participant(),)),
            {},
            "body",
        )


def test_non_git_readiness_reports_preparation_failure_and_checkout_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(readiness, "resolve_checkout_project_dir", lambda _root: tmp_path)
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (ticket, "queue"))
    monkeypatch.setattr(readiness, "_checkout_statuses", lambda _root: ("clean",))
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)
    monkeypatch.setattr(
        readiness,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="prepare failed"),
    )
    assert readiness.check_ticket_ready(tmp_path, "ticket").errors == ("prepare failed",)

    statuses = iter([("clean",), ("dirty",)])
    monkeypatch.setattr(readiness, "_checkout_statuses", lambda _root: next(statuses))
    monkeypatch.setattr(
        readiness,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    assert readiness.check_ticket_ready(tmp_path, "ticket").errors == (
        "project preparation changed Git-visible checkout state",
    )


def test_checkout_readiness_reports_missing_project_repository_and_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    tickets = root / ".booley_project/tickets"
    (root / ".git").mkdir(parents=True)
    paired = AcceptanceBasis((_participant(), _participant("project")))
    monkeypatch.setattr("booley.ticket_board.io.TicketIO.load_basis", lambda *_args: paired)
    monkeypatch.setattr(readiness, "resolve_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(readiness, "resolve_inner_project_repo", lambda _root: None)
    fields = {"acceptance_basis": paired.as_dict()}
    assert (
        "project participant repository is missing"
        in readiness._validate_checkout_basis(root, tickets, "ticket", fields, "body")[0]
    )

    native = AcceptanceBasis((_participant(),))
    monkeypatch.setattr("booley.ticket_board.io.TicketIO.load_basis", lambda *_args: native)
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (None, None))
    assert (
        "unavailable during readiness"
        in readiness._validate_checkout_basis(
            root, tickets, "ticket", {"acceptance_basis": native.as_dict()}, "body"
        )[0]
    )


def test_isolated_core_normalization_rejects_invalid_files_and_normalizes_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / core_projection.ISOLATED_REGISTRY_SUBDIR
    registry.mkdir(parents=True)
    core = registry / "test.core"
    core.write_text("one line\n", encoding="utf-8")
    assert core_projection._normalized_isolated_core(core) is None
    core.write_text(
        "CAPI=2:\n# Booley stealth core projection: marker\ninvalid: [\n",
        encoding="utf-8",
    )
    assert core_projection._normalized_isolated_core(core) is None
    core.write_text("name: test\n# Booley stealth core projection: marker\n", encoding="utf-8")
    assert core_projection._normalized_isolated_core(core) is None
    core.write_text(
        f"CAPI=2:\n# Booley stealth core projection: marker\nroot: {tmp_path}\n",
        encoding="utf-8",
    )
    marker, document = core_projection._normalized_isolated_core(core, checkout_root=tmp_path) or (
        "",
        {},
    )
    assert marker.startswith("# Booley stealth core projection:")
    assert document["root"] == "${BOOLEY_WORKTREE}"
    monkeypatch.setattr(
        Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    assert core_projection._normalized_isolated_core(core) is None


def test_attach_and_registration_helpers_cover_failure_and_success_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already points"):
        workspace_ops._attach_worktree(tmp_path, destination, "ticket", "main")
    destination.mkdir()
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "a" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="path already exists"):
        workspace_ops._attach_worktree(tmp_path, destination, "ticket", "main")

    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    assert workspace_ops._registered_worktree(tmp_path, destination) is False

    attachment = _attachment(tmp_path / "success")
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    workspace_ops._create_attachment(attachment)
    assert attachment.worktree_attached is True


def test_handoff_preparation_short_circuits_jobs_and_reuses_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tio = SimpleNamespace(logs_dir=tmp_path)
    monkeypatch.setattr(operations, "_handoff_jobs_clear", lambda *_args: False)
    assert operations._prepare_handoff_snapshot(tio, "ticket", None, None) is False
    monkeypatch.setattr(operations, "_handoff_jobs_clear", lambda *_args: True)
    monkeypatch.setattr(operations, "_handoff_basis_heads", lambda *_args: {"outer": "a" * 40})
    monkeypatch.setattr(operations, "_bind_existing_handoff_snapshot", lambda *_args: True)
    assert operations._prepare_handoff_snapshot(tio, "ticket", None, None) is True


def test_handoff_jobs_clear_reports_active_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = SimpleNamespace(endpoint="sim", run_id="run-1")
    monkeypatch.setattr("booley.harness.job_fence.active_ticket_jobs", lambda _path: [job])
    assert operations._handoff_jobs_clear(tmp_path, "ticket") is False
    assert "sim (run-1)" in capsys.readouterr().err


def test_materialized_handoff_requires_ticket_and_successful_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tio = SimpleNamespace(tickets_dir=tmp_path, _project_root=tmp_path)
    monkeypatch.setattr("booley.ticket_board.io.find_ticket_file", lambda *_args: (None, None))
    with pytest.raises(AcceptanceBasisError, match="unavailable during Basis validation"):
        operations._prepare_materialized_basis_view(
            tio, "ticket", tmp_path, AcceptanceBasis((_participant(),))
        )
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket", encoding="utf-8")
    monkeypatch.setattr(
        "booley.ticket_board.io.find_ticket_file", lambda *_args: (ticket, "queue")
    )
    monkeypatch.setattr(
        "booley.runtime.project_dir.resolve_checkout_project_dir", lambda _root: tmp_path
    )
    monkeypatch.setattr(
        "booley.runtime.project_prepare.prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="prepare failed"),
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)
    with pytest.raises(AcceptanceBasisError, match="prepare failed"):
        operations._prepare_materialized_basis_view(
            tio, "ticket", tmp_path, AcceptanceBasis((_participant(),))
        )


def test_completion_snapshot_rejects_basis_and_selector_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    tio = SimpleNamespace(
        _project_root=tmp_path,
        load_basis=lambda _slug: basis,
    )
    snapshot = SimpleNamespace(
        acceptance_basis={"different": True}, participant_heads={"outer": "a" * 40}
    )
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_ledger.validate_review_package_binding",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        acceptance_basis,
        "load_basis_receipt",
        lambda *_args: {"current": True},
    )
    from booley.ticket_board.acceptance_ledger import AcceptanceLedgerError

    with pytest.raises(AcceptanceLedgerError, match="different Board Acceptance Basis"):
        operations._validate_accepted_snapshot(tio, "ticket", tmp_path, snapshot)


def test_handoff_basis_heads_validates_materialized_composite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    basis = AcceptanceBasis((_participant(),))
    tio = SimpleNamespace(_project_root=tmp_path)
    monkeypatch.setattr(operations, "_load_handoff_basis", lambda *_args: basis)
    monkeypatch.setattr(
        acceptance_basis,
        "validate_current_basis_refs",
        lambda *_args: {"outer": "a" * 40},
    )
    monkeypatch.setattr(
        acceptance_basis,
        "materialize_ticket_commits",
        lambda _root, _basis, destination, _heads: destination,
    )
    monkeypatch.setattr(acceptance_basis, "assert_live_inputs_unchanged", lambda *_args: None)
    monkeypatch.setattr(operations, "_prepare_materialized_basis_view", lambda *_args: [])
    assert operations._handoff_basis_heads(tio, "ticket") == {"outer": "a" * 40}

    monkeypatch.setattr(
        operations,
        "_prepare_materialized_basis_view",
        lambda *_args: ["target changed"],
    )
    assert operations._handoff_basis_heads(tio, "ticket") is None
    assert "selectors changed" in capsys.readouterr().err


def test_completion_acceptance_reports_unreadable_corrupt_and_valid_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.ticket_board.acceptance_ledger import AcceptanceLedgerError

    snapshot = SimpleNamespace()
    outcomes = iter(
        [
            SimpleNamespace(kind="accepted", snapshot=None),
            SimpleNamespace(kind="accepted", snapshot=snapshot),
            SimpleNamespace(kind="accepted", snapshot=snapshot),
        ]
    )
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_ledger.read_acceptance",
        lambda *_args: next(outcomes),
    )
    validation = iter([AcceptanceLedgerError("broken binding"), None])

    def validate(*_args: object) -> None:
        result = next(validation)
        if result is not None:
            raise result

    monkeypatch.setattr(operations, "_validate_accepted_snapshot", validate)
    tio = SimpleNamespace(logs_dir=tmp_path)

    assert operations._completion_acceptance_valid(tio, "ticket") is None
    assert "unreadable" in capsys.readouterr().err
    assert operations._completion_acceptance_valid(tio, "ticket") is None
    assert "broken binding" in capsys.readouterr().err
    assert operations._completion_acceptance_valid(tio, "ticket") is snapshot


def test_authoring_change_validation_rejects_non_acceptance_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert workspace_ops._is_authoring_path(tmp_path, "generated.core", set()) is True
    monkeypatch.setattr(workspace_ops, "_status_paths", lambda _repository: ["rtl/design.sv"])
    monkeypatch.setattr(
        workspace_ops,
        "_local_manifest_paths",
        lambda _surface, _project_repository: set(),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="non-authoring changes"):
        workspace_ops._validate_authoring_changes(tmp_path, tmp_path, False, set())


def test_changed_core_targets_report_parse_and_identity_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "changed.core"
    core.write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(
        workspace_ops.fusesoc_registry,
        "read_core",
        lambda _path: (_ for _ in ()).throw(
            workspace_ops.fusesoc_registry.FuseSocError("invalid core")
        ),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="invalid core"):
        workspace_ops._changed_targets(tmp_path, [core.name], None, [])

    monkeypatch.setattr(workspace_ops.fusesoc_registry, "read_core", lambda _path: {})
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="no valid name"):
        workspace_ops._changed_targets(tmp_path, [core.name], None, [])


def test_attachment_rollback_helpers_surface_git_inspection_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    attachment.upstream_changed = True
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("git unavailable")
        ),
    )
    assert workspace_ops._restore_attachment_upstream(attachment) == "git unavailable"

    attachment.worktree_attached = True
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: True)
    assert workspace_ops._remove_attachment_worktree(attachment) == (
        ["git unavailable"],
        False,
    )

    attachment.branch_created = True
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    assert workspace_ops._delete_created_branch(attachment, False) == "git unavailable"


def test_attachment_rollback_and_open_base_validation_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_remove_attachment_worktree", lambda *_args: ([], True))
    monkeypatch.setattr(workspace_ops, "_restore_attachment_upstream", lambda *_args: None)
    monkeypatch.setattr(workspace_ops, "_delete_created_branch", lambda *_args, **_kwargs: None)
    assert workspace_ops._rollback_attachment(attachment) == ([], True)
    assert workspace_ops._rollback_open(attachment, None) == []

    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "b" * 40)
    with pytest.raises(
        workspace_ops.AcceptanceBasisOperationError, match="moved during preflight"
    ):
        workspace_ops._validate_open_bases(tmp_path, "main", "a" * 40, None)

    project = SimpleNamespace(source=tmp_path / "project", base_branch="main", base_sha="a" * 40)
    monkeypatch.setattr(
        workspace_ops,
        "_full_commit",
        lambda repository, _branch: "a" * 40 if repository == tmp_path else "b" * 40,
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="paired project"):
        workspace_ops._validate_open_bases(tmp_path, "main", "a" * 40, project)


def test_path_policy_and_basis_require_supported_schema_and_outer_participant() -> None:
    with pytest.raises(AcceptanceBasisError, match="unsupported Acceptance Path Policy"):
        acceptance_basis.AcceptancePathPolicy(schema=2).discover(Path.cwd())
    with pytest.raises(AcceptanceBasisError, match="requires an outer"):
        AcceptanceBasis((_participant("project"),))
    with pytest.raises(AcceptanceBasisError, match="participants must be a list"):
        AcceptanceBasis.from_mapping({"schema": 1, "participants": {}})


def test_basis_participant_lookup_and_record_routing_fail_loudly() -> None:
    basis = AcceptanceBasis((_participant(),))
    with pytest.raises(AcceptanceBasisError, match="no 'project'"):
        basis.participant("project")
    with pytest.raises(AcceptanceBasisError, match="frontmatter is invalid"):
        basis.with_record({"bindings": [], "ticket": {"frontmatter": []}})
    with pytest.raises(AcceptanceBasisError, match="outer destination disagrees"):
        acceptance_basis._validate_record_routing(basis, {"branch": "release"})
    with pytest.raises(AcceptanceBasisError, match="without a participant"):
        acceptance_basis._validate_record_routing(
            basis, {"branch": "main", "project_destination_ref": "refs/heads/main"}
        )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ([], "must be a mapping"),
        ({"role": "outer"}, "must contain exactly"),
        (
            {
                "role": 3,
                "authoring_sha": "a" * 40,
                "ticket_ref": "refs/heads/booley-generation/0123456789abcdef/outer",
                "destination_ref": "refs/heads/main",
                "destination_sha": "b" * 40,
            },
            "must be a non-empty string",
        ),
        ({**_participant().as_dict(), "role": "other"}, "must be outer or project"),
        ({**_participant().as_dict(), "authoring_sha": "short"}, "full Git SHAs"),
        ({**_participant().as_dict(), "ticket_ref": "refs/heads/ticket"}, "generation-qualified"),
        ({**_participant().as_dict(), "destination_ref": "main"}, "full branch ref"),
    ],
)
def test_participant_parser_rejects_malformed_rows(row: object, message: str) -> None:
    with pytest.raises(AcceptanceBasisError, match=message):
        acceptance_basis._parse_participant(row, 0)


def test_authored_record_rejects_unknown_field_and_invalid_on_success() -> None:
    fields = {"branch": "main", "unknown": True}
    with pytest.raises(AcceptanceBasisError, match="unknown authored"):
        acceptance_basis.authored_ticket_record(fields, "body", ())
    with pytest.raises(AcceptanceBasisError, match="on_success must be a mapping"):
        acceptance_basis.authored_ticket_record({"branch": "main", "on_success": []}, "body", ())


def test_binding_record_parser_rejects_invalid_schema() -> None:
    with pytest.raises(AcceptanceBasisError, match="invalid schema"):
        acceptance_basis._binding_from_record({"flow": "sim"})


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda record: record.update(extra=True), "invalid top-level"),
        (lambda record: record.update(schema=2), "unsupported schema"),
        (lambda record: record["ticket"].update(extra=True), "ticket has an invalid schema"),
        (
            lambda record: record["ticket"]["frontmatter"].update(extra=True),
            "unknown authored field",
        ),
        (
            lambda record: record["ticket"]["frontmatter"].pop("scope"),
            "authored defaults are not canonical",
        ),
        (
            lambda record: record["ticket"]["frontmatter"].update(on_success="bad"),
            "on_success must be a mapping",
        ),
    ],
)
def test_record_validation_rejects_noncanonical_shapes(mutate: object, message: str) -> None:
    record = _record()
    mutate(record)  # type: ignore[operator]
    with pytest.raises(AcceptanceBasisError, match=message):
        acceptance_basis._validate_record(record)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (subprocess.CompletedProcess(["git"], 1, b"", b"missing"), "record is unavailable"),
        (subprocess.CompletedProcess(["git"], 0, b"not json", b""), "invalid JSON"),
        (subprocess.CompletedProcess(["git"], 0, b'{"schema":1}\n', b""), "record.ticket"),
    ],
)
def test_load_basis_record_reports_git_and_payload_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str],
    message: str,
) -> None:
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(acceptance_basis.subprocess, "run", lambda *_args, **_kwargs: result)
    with pytest.raises(AcceptanceBasisError, match=message):
        acceptance_basis.load_basis_record(tmp_path, "ticket", AcceptanceBasis((_participant(),)))


def test_load_basis_record_rejects_noncanonical_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    payload = (" " + acceptance_basis.canonical_json(record).decode()).encode()
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["git"], 0, payload, b""),
    )
    with pytest.raises(AcceptanceBasisError, match="not canonical JSON"):
        acceptance_basis.load_basis_record(tmp_path, "ticket", AcceptanceBasis((_participant(),)))


def test_record_validation_rejects_invalid_on_success_policy() -> None:
    record = _record()
    record["ticket"]["frontmatter"]["on_success"] = {  # type: ignore[index]
        "destination": "invalid",
        "merge": True,
        "cleanup": True,
        "triage_report": True,
        "remove_targets": [],
    }
    with pytest.raises(AcceptanceBasisError, match="destination"):
        acceptance_basis._validate_record(record)


def test_receipt_validation_reports_missing_and_mismatched_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    record = _record()
    monkeypatch.setattr(
        acceptance_basis,
        "_receipt_path",
        lambda *_args: tmp_path / "receipt.json",
    )
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    with pytest.raises(AcceptanceBasisError, match="receipt is unavailable"):
        acceptance_basis._validate_receipt(tmp_path, "ticket", basis, record)
    receipt = acceptance_basis._receipt_payload(
        tmp_path,
        "ticket",
        basis,
        record,
        source_sha256="1" * 64,
        operation_id="2" * 32,
    )
    path = tmp_path / "receipt.json"
    path.write_bytes(acceptance_basis.canonical_json({**receipt, "schema": 2}))
    with pytest.raises(AcceptanceBasisError, match="receipt mismatch"):
        acceptance_basis._validate_receipt(tmp_path, "ticket", basis, record)


def test_write_basis_receipt_rejects_source_drift_and_write_once_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    record = _record()
    path = tmp_path / "receipt.json"
    path.write_text("present", encoding="utf-8")
    monkeypatch.setattr(acceptance_basis, "load_basis_record", lambda *_args: record)
    monkeypatch.setattr(acceptance_basis, "_receipt_path", lambda *_args: path)
    monkeypatch.setattr(
        acceptance_basis,
        "record_relative_path",
        lambda *_args, **_kwargs: Path(".booley_project/acceptance/bases"),
    )
    monkeypatch.setattr(
        acceptance_basis,
        "_validate_receipt",
        lambda *_args: {"source_sha256": "old"},
    )
    with pytest.raises(AcceptanceBasisError, match="different source"):
        acceptance_basis.write_basis_receipt(
            tmp_path, "ticket", basis, source_sha256="new", operation_id="2" * 32
        )
    path.unlink()
    monkeypatch.setattr(
        acceptance_basis,
        "atomic_write_once",
        lambda *_args: (_ for _ in ()).throw(acceptance_basis.WriteOnceConflictError("race")),
    )
    with pytest.raises(AcceptanceBasisError, match="conflicting Acceptance Basis receipt"):
        acceptance_basis.write_basis_receipt(
            tmp_path, "ticket", basis, source_sha256="1" * 64, operation_id="2" * 32
        )


def test_git_path_and_worktree_command_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_basis,
        "_worktree_git_command",
        lambda *_args: ["git"],
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="bad worktree"),
    )
    with pytest.raises(AcceptanceBasisError, match="bad worktree"):
        acceptance_basis._git_paths(tmp_path, "status")

    dot_git = tmp_path / ".git"
    dot_git.write_text("malformed", encoding="utf-8")
    assert acceptance_basis._worktree_git_command(tmp_path) == ["git"]
    dot_git.write_text("other: relative", encoding="utf-8")
    assert acceptance_basis._worktree_git_command(tmp_path) == ["git"]
    dot_git.write_text("gitdir: relative", encoding="utf-8")
    monkeypatch.setattr(acceptance_basis, "_git_common_dir", lambda *_args: None)
    assert acceptance_basis._worktree_git_command(tmp_path) == ["git"]


def test_worktree_command_remaps_inaccessible_admin_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dot_git = tmp_path / ".git"
    dot_git.write_text("gitdir: /host/repo/.git/worktrees/ticket\n", encoding="utf-8")
    common = tmp_path / "common"
    mounted = common / "worktrees/ticket"
    mounted.mkdir(parents=True)
    monkeypatch.setattr(acceptance_basis, "_git_common_dir", lambda *_args: common)
    assert acceptance_basis._worktree_git_command(tmp_path) == [
        "git",
        f"--git-dir={mounted}",
        f"--work-tree={tmp_path}",
    ]


def test_destination_and_ticket_commit_inputs_require_complete_full_sha_maps(
    tmp_path: Path,
) -> None:
    basis = AcceptanceBasis((_participant(),))
    with pytest.raises(AcceptanceBasisError, match="cover every participant"):
        acceptance_basis.validate_destination_refs(tmp_path, basis, {})
    with pytest.raises(AcceptanceBasisError, match="must be a full Git SHA"):
        acceptance_basis.validate_destination_refs(tmp_path, basis, {"outer": "bad"})
    with pytest.raises(AcceptanceBasisError, match="cover every Basis participant"):
        acceptance_basis.materialize_ticket_commits(tmp_path, basis, tmp_path / "out", {})
    with pytest.raises(AcceptanceBasisError, match="must be a full Git SHA"):
        acceptance_basis.materialize_ticket_commits(
            tmp_path, basis, tmp_path / "out", {"outer": "bad"}
        )


def test_validate_ticket_view_wraps_generated_projection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.fusesoc import core_projection

    monkeypatch.setattr(
        core_projection,
        "reconcile_projected_cores",
        lambda *_args: (_ for _ in ()).throw(core_projection.CoreProjectionError("broken")),
    )
    with pytest.raises(AcceptanceBasisError, match="could not prepare generated"):
        acceptance_basis.validate_ticket_view(
            tmp_path, AcceptanceBasis((_participant(),)), allow_generated=True
        )


def test_worktree_mapping_and_identity_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = Path("/host/repo/not-an-index/ticket")
    monkeypatch.setattr(
        acceptance_basis,
        "_worktree_records",
        lambda _root: (
            (Path("/host/repo"), "refs/heads/main"),
            (recorded, _participant().ticket_ref),
        ),
    )
    with pytest.raises(AcceptanceBasisError, match="could not be identified"):
        acceptance_basis.worktree_for_ref(tmp_path, _participant().ticket_ref)

    responses = iter(
        [
            _completed("git", returncode=1),
            _completed("git", stdout=str(tmp_path / "other") + "\n"),
        ]
    )
    monkeypatch.setattr(
        acceptance_basis,
        "_worktree_git_command",
        lambda *_args: ["git"],
    )
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )
    assert (
        acceptance_basis._worktree_has_identity(tmp_path, "refs/heads/ticket", tmp_path) is False
    )
    assert (
        acceptance_basis._worktree_has_identity(tmp_path, "refs/heads/ticket", tmp_path) is False
    )


def test_descendant_and_project_repository_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )
    with pytest.raises(AcceptanceBasisError, match="ref is unavailable"):
        acceptance_basis._descendant_ref_commit(
            tmp_path,
            "refs/heads/ticket",
            "a" * 40,
            kind="ticket",
            role="outer",
        )
    monkeypatch.setattr(acceptance_basis, "paired_project_repository", lambda _root: None)
    monkeypatch.setattr(acceptance_basis, "resolve_inner_project_repo", lambda _root: None)
    with pytest.raises(AcceptanceBasisError, match="paired project repository"):
        acceptance_basis._project_repository(tmp_path)


def test_clone_commit_reports_clone_and_checkout_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter([_completed("git", returncode=1, stderr="clone failed")])
    monkeypatch.setattr(
        acceptance_basis.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(AcceptanceBasisError, match="clone failed"):
        acceptance_basis._clone_commit(tmp_path, tmp_path / "clone", "a" * 40)
    responses = iter(
        [_completed("git"), _completed("git", returncode=1, stderr="checkout failed")]
    )
    with pytest.raises(AcceptanceBasisError, match="checkout failed"):
        acceptance_basis._clone_commit(tmp_path, tmp_path / "clone", "a" * 40)


def test_generated_path_equivalence_handles_symlinks_and_mode_mismatch(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.symlink_to("target")
    right.symlink_to("target")
    assert acceptance_basis._same_generated_path(left, right) is True
    right.unlink()
    right.symlink_to("other")
    assert acceptance_basis._same_generated_path(left, right) is False
    left.unlink()
    right.unlink()
    left.write_text("same", encoding="utf-8")
    right.write_text("same", encoding="utf-8")
    left.chmod(0o755)
    right.chmod(0o644)
    assert acceptance_basis._same_generated_path(left, right) is False


def test_load_acceptance_basis_rejects_legacy_and_authored_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    with pytest.raises(AcceptanceBasisError, match="hard cutoff"):
        acceptance_basis.load_acceptance_basis(tmp_path, "ticket", {"target_contract": {}})
    record = _record()
    monkeypatch.setattr(acceptance_basis, "load_basis_record", lambda *_args: record)
    monkeypatch.setattr(acceptance_basis, "_validate_receipt", lambda *_args: {})
    fields = dict(record["ticket"]["frontmatter"])  # type: ignore[index]
    fields["acceptance_basis"] = basis.as_dict()
    fields["summary"] = "Changed"
    with pytest.raises(AcceptanceBasisError, match="frontmatter changed"):
        acceptance_basis.load_acceptance_basis(tmp_path, "ticket", fields)
    fields["summary"] = "Ticket"
    with pytest.raises(AcceptanceBasisError, match="body changed"):
        acceptance_basis.load_acceptance_basis(tmp_path, "ticket", fields, "different")


def test_target_binding_rejects_incomplete_and_noncanonical_values() -> None:
    binding = acceptance_targets.AcceptanceTargetBinding(
        "sim",
        "criteria.mandatory.sim_pass",
        "acme:lib:toy:1#sim",
        "acme:lib:toy:1#sim",
        "sim",
        " sim",
    )

    with pytest.raises(ValueError, match="candidate_selector"):
        binding.validate_persisted()
    with pytest.raises(ValueError, match="full"):
        acceptance_targets.AcceptanceTargetBinding(
            "sim", "sim_pass", "base", "candidate", "base", "candidate"
        ).validate_persisted()


def test_target_binding_accepts_optional_criterion_path() -> None:
    binding = acceptance_targets.AcceptanceTargetBinding(
        "lint",
        "criteria.optional.lint_clean",
        "acme:lib:toy:1#lint",
        "acme:lib:toy:1#lint",
        "lint",
        "lint",
    )

    assert binding.validate_persisted() is binding
    assert binding.criterion_key == "lint_clean"
    assert binding.as_dict()["criterion"] == "criteria.optional.lint_clean"


def test_target_control_helpers_handle_external_and_missing_project_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / "outside.core"
    assert acceptance_targets._identity(tmp_path, outside) == outside.as_posix()
    monkeypatch.setattr(
        acceptance_targets,
        "resolve_checkout_project_dir",
        lambda _root: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert tuple(acceptance_targets._project_control_files(tmp_path)) == ()


def test_core_referenced_files_ignore_invalid_entries(tmp_path: Path) -> None:
    core = tmp_path / "toy.core"
    document = {
        "filesets": {
            "not-a-map": [],
            "no-files": {},
            "mixed": {
                "files": [
                    "rtl/toy.sv",
                    {"constraints/toy.sdc": {"file_type": "SDC"}},
                    {},
                    7,
                ]
            },
        }
    }

    assert [
        path.relative_to(tmp_path).as_posix()
        for path in acceptance_targets._core_referenced_files(tmp_path, core, document)
    ] == [
        "rtl/toy.sv",
        "constraints/toy.sdc",
    ]
    assert tuple(acceptance_targets._core_referenced_files(tmp_path, core, {})) == ()


def test_tracked_gitlinks_reports_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="bad index"),
    )

    with pytest.raises(acceptance_targets.BoundaryError, match="bad index"):
        acceptance_targets._tracked_gitlinks(tmp_path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"target": "lint_a"}, [("lint_a", "lint_a", False)]),
        (
            {
                "targets": [{"baseline": "synth_a", "candidate": "synth_b"}],
                "area_reduce_at_least": "5%",
            },
            [("synth_b", "synth_a", True)],
        ),
        ({"targets": [7]}, []),
        ("invalid", []),
    ],
)
def test_target_value_parser_handles_supported_and_invalid_shapes(
    value: object, expected: list[tuple[str, str, bool]]
) -> None:
    assert acceptance_targets._targets_from_value("synthesis_ok", value) == expected


def test_target_list_parser_handles_coverage_sim_and_invalid_items() -> None:
    value = [
        {"targets": ["cov_a", "cov_b"]},
        {"target": "sim_map"},
        "tb/test.sv @ sim_text @ all @ none -> pass",
        "lint_plain",
        "invalid @ text",
        3,
    ]
    assert acceptance_targets._targets_from_list("coverage", value) == [
        ("cov_a", "cov_a", False),
        ("cov_b", "cov_b", False),
        ("sim_map", "sim_map", False),
        ("sim_text", "sim_text", False),
        ("lint_plain", "lint_plain", False),
    ]


def test_target_list_parser_ignores_invalid_sim_expression() -> None:
    assert acceptance_targets._targets_from_list("sim_pass", ["broken -> expression"]) == []
    assert acceptance_targets._criterion_flow("unknown") is None


def test_missing_target_sources_normalizes_relative_and_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "rtl/existing.sv"
    existing.parent.mkdir()
    existing.write_text("module existing; endmodule\n", encoding="utf-8")
    absolute = tmp_path / "absolute.sv"
    monkeypatch.setattr(
        acceptance_targets,
        "inspect_target_selector",
        lambda *_args: SimpleNamespace(
            inputs=(
                SimpleNamespace(path="rtl/existing.sv"),
                SimpleNamespace(path="rtl/missing.sv"),
                SimpleNamespace(path=str(absolute)),
                SimpleNamespace(path="rtl/missing.sv"),
            )
        ),
    )

    assert acceptance_targets._missing_target_sources(tmp_path, "sim") == [
        str(absolute),
        "rtl/missing.sv",
    ]


def test_criterion_targets_ignore_unknown_sections_and_keys() -> None:
    criteria = {
        "mandatory": {"review_rtl_bugs": True, "lint_clean": ["lint"]},
        "optional": "invalid",
    }

    assert acceptance_targets.criterion_targets(None) == ()
    (binding,) = acceptance_targets.criterion_targets(criteria)
    assert binding.target == "lint"
    assert binding.flow == "lint"


def test_coverage_suite_validation_reports_registry_and_selection_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    criteria = {"mandatory": {"coverage": [{"targets": ["sim"], "tests": ["missing"]}]}}
    monkeypatch.setattr(
        acceptance_targets,
        "resolve_checkout_project_dir",
        lambda _root: tmp_path / ".booley_project",
    )
    assert (
        "cannot validate registered tests"
        in acceptance_targets._validate_coverage_suites(criteria, tmp_path)[0]
    )
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "tests.toml").write_text("[targets.sim]\ntests = ['known']\n", encoding="utf-8")
    assert acceptance_targets._validate_coverage_suites(criteria, tmp_path) == [
        "criteria.mandatory.coverage: target 'sim' has unregistered tests: missing"
    ]
    assert acceptance_targets._validate_coverage_suites({}, tmp_path) == []


@pytest.mark.parametrize(
    ("scope", "path", "expected"),
    [
        (None, "rtl/new.sv", False),
        ([7, "rtl/new.sv"], "rtl/new.sv", False),
        (["rtl/new.sv [new]"], "./rtl/new.sv", True),
        (["rtl/new [new]"], "rtl/new/child.sv", True),
        (["rtl/*.sv [new]"], "rtl/new.sv", True),
    ],
)
def test_new_scope_matching(scope: object, path: str, expected: bool) -> None:
    assert acceptance_targets._new_scope_matches(scope, path) is expected


def test_validate_changed_targets_handles_duplicate_missing_and_resolvable_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = {"new": ["rtl/new.sv"], "undeclared": ["rtl/nope.sv"], "ready": []}
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_sources",
        lambda _root, target: missing[target],
    )
    monkeypatch.setattr(
        acceptance_targets,
        "_dry_resolve_binding",
        lambda binding, _root, build: [f"resolved {binding.target} in {build.name}"],
    )

    errors = acceptance_targets._validate_changed_targets(
        {"scope": ["rtl/new.sv [new]"]},
        tmp_path,
        tmp_path / "build",
        ["seen", "new", "undeclared", "ready", "ready"],
        seen={"seen"},
    )

    assert errors[0].startswith("changed Target 'undeclared'")
    assert errors[1] == "resolved ready in ready"


def test_validate_acceptance_targets_stops_after_binding_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets, "validate_criterion_targets", lambda *_args: ["bad binding"]
    )
    assert acceptance_targets.validate_acceptance_targets({}, tmp_path, tmp_path / "build") == [
        "bad binding"
    ]


def test_required_targets_promote_baselines_and_resolvable_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = acceptance_targets.CriterionTarget(
        "mandatory", "synthesis_ok", "candidate", "synth", True, "baseline"
    )
    second = acceptance_targets.CriterionTarget(
        "optional", "synthesis_ok", "candidate", "synth", False
    )
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_sources",
        lambda _root, target: ["new.sv"] if target == "candidate" else [],
    )
    required = acceptance_targets._required_targets(tmp_path, (first, second))
    assert required["candidate"][1] is False
    assert required["baseline"] == (first, True)


def test_validate_required_targets_skips_deferred_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.CriterionTarget(
        "mandatory", "sim_pass", "candidate", "sim", False
    )
    calls: list[str] = []
    monkeypatch.setattr(
        acceptance_targets,
        "_dry_resolve_binding",
        lambda _binding, _root, _build, *, target=None: calls.append(target or "") or ["bad"],
    )
    errors = acceptance_targets._validate_required_targets(
        tmp_path,
        tmp_path / "build",
        {"deferred": (binding, False), "required": (binding, True)},
    )
    assert errors == ["bad"]
    assert calls == ["required"]


def test_comparison_basis_reports_resolution_and_recipe_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.CriterionTarget(
        "mandatory", "synthesis_ok", "candidate", "synth", True, "baseline"
    )
    monkeypatch.setattr(
        acceptance_targets,
        "_comparison_snapshots",
        lambda *_args: (_ for _ in ()).throw(acceptance_targets.BoundaryError("broken")),
    )
    assert (
        "cannot compare"
        in acceptance_targets._validate_comparison_basis(binding, tmp_path, tmp_path / "build")[0]
    )
    monkeypatch.setattr(acceptance_targets, "_comparison_snapshots", lambda *_args: None)
    assert (
        acceptance_targets._validate_comparison_basis(binding, tmp_path, tmp_path / "build") == []
    )

    monkeypatch.setattr(
        acceptance_targets,
        "_comparison_snapshots",
        lambda *_args: ({"tool": "a"}, {"tool": "b"}),
    )
    monkeypatch.setattr(
        "booley.flows.recipe_evidence.implementation_comparison_basis", lambda value: value
    )
    monkeypatch.setattr(
        "booley.flows.recipe_evidence.recipe_changes",
        lambda _left, _right: [{"path": "tool"}],
    )
    assert (
        "incompatible measurement bases (tool)"
        in acceptance_targets._validate_comparison_basis(binding, tmp_path, tmp_path / "build")[0]
    )


def test_comparison_snapshots_dispatch_by_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda target, **_kwargs: SimpleNamespace(name=target),
    )
    monkeypatch.setattr("booley.flows.synth.recipe.default_recipe_args", SimpleNamespace)
    monkeypatch.setattr(
        "booley.flows.synth.recipe.synthesis_recipe_snapshot",
        lambda resolved, _args, *, target: {"target": target, "resolved": resolved.name},
    )
    synth = acceptance_targets.CriterionTarget(
        "mandatory", "synthesis_ok", "candidate", "synth", True, "baseline"
    )
    assert acceptance_targets._comparison_snapshots(synth, tmp_path, tmp_path / "build") == (
        {"target": "baseline", "resolved": "baseline"},
        {"target": "candidate", "resolved": "candidate"},
    )
    other = acceptance_targets.CriterionTarget(
        "mandatory", "sim_pass", "candidate", "sim", False, "baseline"
    )
    assert acceptance_targets._comparison_snapshots(other, tmp_path, tmp_path / "build") is None


def test_comparison_snapshots_dispatch_fpga(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda target, **_kwargs: SimpleNamespace(name=target),
    )
    monkeypatch.setattr(
        "booley.flows.fpga.recipe.fpga_recipe_snapshot",
        lambda resolved, *, target: {"target": target, "resolved": resolved.name},
    )
    binding = acceptance_targets.CriterionTarget(
        "mandatory", "fpga_impl_ok", "candidate", "fpga", True, "baseline"
    )
    assert acceptance_targets._comparison_snapshots(binding, tmp_path, tmp_path / "build") == (
        {"target": "baseline", "resolved": "baseline"},
        {"target": "candidate", "resolved": "candidate"},
    )


def test_dry_resolve_binding_reports_failure_and_missing_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.CriterionTarget("mandatory", "lint_clean", "lint", "lint", False)
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    assert (
        "dry-run failed"
        in acceptance_targets._dry_resolve_binding(binding, tmp_path, tmp_path / "build")[0]
    )
    monkeypatch.setattr(
        acceptance_targets.fusesoc_registry,
        "resolve_target",
        lambda *_args, **_kwargs: SimpleNamespace(toplevel=""),
    )
    assert (
        "without a toplevel"
        in acceptance_targets._dry_resolve_binding(
            binding, tmp_path, tmp_path / "build", target="other"
        )[0]
    )


def test_validate_binding_reports_resolution_flow_and_scope_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.CriterionTarget(
        "mandatory", "synthesis_ok", "candidate", "synth", True, "baseline"
    )

    def select(_root: Path, target: str) -> SimpleNamespace:
        if target == "candidate":
            raise acceptance_targets.fusesoc_registry.FuseSocError("unknown")
        return SimpleNamespace(flow="sim", eda_tool="iverilog")

    monkeypatch.setattr(acceptance_targets, "select_target", select)
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: False)
    errors = acceptance_targets._validate_binding(binding, {}, tmp_path)
    assert "candidate target 'candidate': unknown" in errors[0]
    assert "cannot satisfy synthesis_ok" in errors[1]

    monkeypatch.setattr(
        acceptance_targets,
        "select_target",
        lambda *_args: SimpleNamespace(flow="synth", eda_tool="yosys"),
    )
    monkeypatch.setattr(acceptance_targets, "flow_can_drive", lambda *_args: True)
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_sources",
        lambda _root, target: [f"rtl/{target}.sv"],
    )
    errors = acceptance_targets._validate_binding(binding, {"scope": []}, tmp_path)
    assert "not declared Scope [new]" in errors[0]
    assert "relative-QoR baseline" in errors[1]


def test_validate_binding_selectors_reports_failure_and_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = acceptance_targets.AcceptanceTargetBinding(
        "sim",
        "criteria.mandatory.sim_pass",
        "expected-base",
        "expected-candidate",
        "base",
        "candidate",
    )

    def select(_root: Path, selector: str) -> SimpleNamespace:
        if selector == "base":
            raise ValueError("missing")
        return SimpleNamespace(identity="actual")

    monkeypatch.setattr(acceptance_targets, "select_target", select)
    errors = acceptance_targets.validate_binding_selectors(tmp_path, [binding])
    assert "cannot be resolved" in errors[0]
    assert "resolves to 'actual'" in errors[1]


def test_resolve_commit_rejects_nonexact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        acceptance_targets.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed("git", stdout="b" * 40 + "\n"),
    )
    with pytest.raises(ValueError, match="does not resolve exactly"):
        acceptance_targets.resolve_commit(tmp_path, "a" * 40)


def test_git_helpers_report_process_and_command_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git missing")),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="git missing"):
        workspace_ops._git(tmp_path, "status")
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=3, stderr="bad ref"),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="bad ref"):
        workspace_ops._require_git(tmp_path, "rev-parse", "HEAD")


def test_strict_branch_lookup_distinguishes_missing_and_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1),
    )
    assert workspace_ops._strict_branch_sha(tmp_path, "ticket") is None
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="locked"),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="locked"):
        workspace_ops._strict_branch_sha(tmp_path, "ticket")


def test_attachment_planning_rejects_moved_branch_and_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already points"):
        workspace_ops._plan_open_attachment(tmp_path, tmp_path / "new", "ticket", "a" * 40)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: None)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="already exists"):
        workspace_ops._plan_open_attachment(tmp_path, existing, "ticket", "a" * 40)


def test_attach_worktree_reuses_matching_branch_or_creates_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "worktrees/ticket"
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "a" * 40)
    commands: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *args, **_kwargs: commands.append(args) or "",
    )
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "a" * 40)
    assert workspace_ops._attach_worktree(tmp_path, destination, "ticket", "main") == "a" * 40
    assert commands[-1][-4:] == ("worktree", "add", str(destination), "ticket")

    commands.clear()
    second = tmp_path / "worktrees/second"
    monkeypatch.setattr(workspace_ops, "_branch_sha", lambda *_args: "")
    workspace_ops._attach_worktree(tmp_path, second, "second", "main")
    assert commands[-1][-6:] == (
        "worktree",
        "add",
        "-b",
        "second",
        str(second),
        "a" * 40,
    )


def test_current_and_project_branch_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="detached"):
        workspace_ops._current_branch(tmp_path)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: None)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="does not exist"):
        workspace_ops._project_base_branch(tmp_path, "refs/heads/main")


def test_preflight_project_repository_validates_destination_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: None)
    assert workspace_ops._preflight_project_repository(tmp_path, "main", None) is None
    monkeypatch.setattr(workspace_ops, "resolve_inner_project_repo", lambda _root: tmp_path)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="full refs/heads"):
        workspace_ops._preflight_project_repository(tmp_path, "main", 3)


def test_registered_worktree_and_branch_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda _repo, *args, **_kwargs: (
            f"worktree {tmp_path / 'other'}\n\nworktree {worktree}\n"
            if args == ("worktree", "list", "--porcelain")
            else "ticket"
        ),
    )
    assert workspace_ops._registered_worktree(tmp_path, worktree) is True
    assert workspace_ops._worktree_owns_branch(tmp_path, worktree, "ticket") is True
    assert workspace_ops._worktree_owns_branch(tmp_path, tmp_path / "missing", "ticket") is False


def test_create_attachment_retains_unconfirmed_created_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    responses = iter([None, "a" * 40])
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: next(responses))
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("update failed")
        ),
    )

    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="retained"):
        workspace_ops._create_attachment(attachment)


def test_create_attachment_records_partial_path_and_moved_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)

    def fail_add(*_args: object, **_kwargs: object) -> str:
        attachment.worktree.mkdir()
        raise workspace_ops.AcceptanceBasisOperationError("attach failed")

    monkeypatch.setattr(workspace_ops, "_require_git", fail_add)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="attach failed"):
        workspace_ops._create_attachment(attachment)
    assert attachment.partial_path is True

    attachment = _attachment(tmp_path / "moved")
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(workspace_ops, "_require_git", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "b" * 40)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="moved"):
        workspace_ops._create_attachment(attachment)


def test_upstream_change_tracks_ambiguous_success_and_restores_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    upstreams = iter([None, "main"])
    monkeypatch.setattr(workspace_ops, "_branch_upstream", lambda *_args: next(upstreams))
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("set failed")
        ),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="set failed"):
        workspace_ops._set_attachment_upstream(attachment, "main")
    assert attachment.upstream_changed is True

    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="unset failed"),
    )
    assert "could not restore upstream" in workspace_ops._restore_attachment_upstream(attachment)


def test_upstream_noop_and_successful_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_branch_upstream", lambda *_args: "main")
    monkeypatch.setattr(
        workspace_ops,
        "_require_git",
        lambda *_args, **_kwargs: pytest.fail("matching upstream must be a no-op"),
    )
    workspace_ops._set_attachment_upstream(attachment, "main")
    assert attachment.upstream_changed is False

    attachment.upstream_changed = True
    attachment.previous_upstream = "old"
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git"),
    )
    assert workspace_ops._restore_attachment_upstream(attachment) is None
    attachment.branch_created = True
    assert workspace_ops._restore_attachment_upstream(attachment) is None


def test_remove_attachment_handles_ambiguous_partial_and_failed_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: True)
    failures, clear = workspace_ops._remove_attachment_worktree(attachment)
    assert failures == [f"retained ambiguously created worktree {attachment.worktree}"]
    assert clear is False

    attachment.worktree.mkdir()
    attachment.partial_path = True
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: False)
    failures, clear = workspace_ops._remove_attachment_worktree(attachment)
    assert failures == [] and clear is True
    assert not attachment.worktree.exists()

    attachment.worktree_attached = True
    monkeypatch.setattr(workspace_ops, "_registered_worktree", lambda *_args: True)
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1, stderr="busy"),
    )
    failures, clear = workspace_ops._remove_attachment_worktree(attachment)
    assert "busy" in failures[0] and clear is False


def test_created_branch_cleanup_retains_moved_ref_and_reports_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    attachment.branch_created = True
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "b" * 40)
    assert "retained moved branch" in workspace_ops._delete_created_branch(attachment, False)
    monkeypatch.setattr(workspace_ops, "_strict_branch_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=1, stderr="locked"),
    )
    assert "could not delete" in workspace_ops._delete_created_branch(attachment, False)


def test_created_branch_cleanup_handles_lookup_failure_and_absent_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = _attachment(tmp_path)
    assert workspace_ops._delete_created_branch(attachment, False) is None
    attachment.branch_created = True
    assert workspace_ops._delete_created_branch(attachment, True) is None
    monkeypatch.setattr(
        workspace_ops,
        "_strict_branch_sha",
        lambda *_args: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("cannot inspect")
        ),
    )
    assert workspace_ops._delete_created_branch(attachment, False) == "cannot inspect"


def test_rollback_open_retains_outer_when_paired_cleanup_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _attachment(tmp_path / "outer")
    project = _attachment(tmp_path / "project")
    monkeypatch.setattr(
        workspace_ops,
        "_rollback_attachment",
        lambda attachment: (
            (["project failed"], False)
            if attachment is project
            else pytest.fail("outer must be retained")
        ),
    )

    assert workspace_ops._rollback_open(outer, project) == [
        "project failed",
        f"retained outer worktree {outer.worktree} with paired state",
    ]


def test_draft_generation_rejects_invalid_and_conflicting_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = workspace_ops._generation_file(tmp_path, "ticket")
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="invalid"):
        workspace_ops._draft_generation(tmp_path, "ticket")
    path.unlink()
    monkeypatch.setattr(
        workspace_ops,
        "atomic_write_once",
        lambda *_args: (_ for _ in ()).throw(workspace_ops.WriteOnceConflictError("race")),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="conflicting"):
        workspace_ops._draft_generation(tmp_path, "ticket")


def test_draft_generation_reuses_valid_descriptor(tmp_path: Path) -> None:
    path = workspace_ops._generation_file(tmp_path, "ticket")
    path.write_text('{"generation": "0123456789abcdef"}\n', encoding="utf-8")
    assert workspace_ops._draft_generation(tmp_path, "ticket") == "0123456789abcdef"


def test_ensure_ticket_workspace_rejects_bad_slug_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: ''\n---\n", encoding="utf-8")
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="unsafe"):
        workspace_ops.ensure_ticket_workspace(tmp_path, "missing", "bad/slug")
    monkeypatch.setattr(workspace_ops, "_draft_generation", lambda *_args: "0" * 16)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="destination branch"):
        workspace_ops.ensure_ticket_workspace(tmp_path, ticket, "ticket")


def test_workspace_preparation_and_status_fail_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_ops,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="hook failed"),
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: True)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="hook failed"):
        workspace_ops._prepare_workspace_project(
            tmp_path, tmp_path, tmp_path / "ticket.md", "ticket"
        )
    monkeypatch.setattr(
        workspace_ops,
        "_git",
        lambda *_args, **_kwargs: _completed("git", returncode=2, stderr="status failed"),
    )
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="status failed"):
        workspace_ops._status_paths(tmp_path)


def test_reset_project_source_validation_rejects_repository_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = BasisParticipant("outer", "a" * 40, "refs/heads/ticket", "refs/heads/main", "b" * 40)
    project = BasisParticipant(
        "project", "c" * 40, "refs/heads/ticket", "refs/heads/main", "d" * 40
    )
    from booley.ticket_board.acceptance_basis import AcceptanceBasis

    paired = AcceptanceBasis((outer, project))
    native = AcceptanceBasis((outer,))
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="unavailable"):
        workspace_ops._validate_reset_project_source(None, paired)
    with pytest.raises(workspace_ops.AcceptanceBasisOperationError, match="no project"):
        workspace_ops._validate_reset_project_source(tmp_path, native)
    monkeypatch.setattr(workspace_ops, "_full_commit", lambda *_args: "c" * 40)
    workspace_ops._validate_reset_project_source(tmp_path, paired)
