"""Interface tests for the active Acceptance Journal module."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.ticket_board.acceptance_basis import AcceptanceBasis, BasisParticipant
from booley.ticket_board.acceptance_journal import (
    AcceptanceOperationError,
    AcceptanceOutcome,
    AcceptanceRecoveryBlockedError,
    AcceptanceRequest,
    advance_acceptance,
)
from booley.ticket_board.acceptance_journal import _advance as acceptance_impl
from booley.ticket_board.acceptance_journal._repository import LocalAcceptanceRepositories
from booley.ticket_board.acceptance_journal._store import (
    AcceptanceCheckpoint,
    FaultingAcceptanceStore,
    FileAcceptanceStore,
)
from booley.ticket_board.completion import complete_review_ticket
from tests.ticket_board.test_completion import (
    _contract,
    _git,
    _Policy,
    _repository,
    _ticket_commit,
    _TicketIO,
)


def _single_repository_acceptance(
    tmp_path: Path,
) -> tuple[Path, _TicketIO, AcceptanceRequest, str]:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = BasisParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    contract = _contract(root, (participant,))
    tio = _TicketIO(root, contract)
    request = AcceptanceRequest(
        root=root,
        slug="change-target",
        basis=contract,
        cleanup=False,
        ticket_status="review",
        allowed_board_rename=None,
    )
    return root, tio, request, base


def _composite_basis() -> AcceptanceBasis:
    return AcceptanceBasis(
        (
            BasisParticipant("outer", "a" * 40, "outer-src", "outer-dst", "b" * 40),
            BasisParticipant("project", "c" * 40, "project-src", "project-dst", "d" * 40),
        )
    )


def _trace_surface_validation(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(acceptance_impl, "_clone_checkout", lambda *_args: events.append("clone"))
    monkeypatch.setattr(
        acceptance_impl, "checkout_project_dir_relative_to", lambda _root: Path("project")
    )
    monkeypatch.setattr(
        acceptance_impl,
        "_materialize_surface_submodules",
        lambda *_args: events.append("materialize"),
    )
    monkeypatch.setattr(
        acceptance_impl,
        "assert_inputs_unchanged",
        lambda *_args: events.append("validate"),
    )
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_targets.validate_binding_selectors",
        lambda *_args: (),
    )
    return events


def test_source_surface_materializes_submodules_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _trace_surface_validation(monkeypatch)

    acceptance_impl._validate_source_surface(
        tmp_path, tmp_path / "project", _composite_basis(), {"outer": "a", "project": "b"}
    )

    assert events == ["clone", "clone", "materialize", "validate"]


def test_candidate_surface_materializes_submodules_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _trace_surface_validation(monkeypatch)
    journal = SimpleNamespace(
        candidates={
            "outer": SimpleNamespace(prepared_sha="a" * 40),
            "project": SimpleNamespace(prepared_sha="b" * 40),
        }
    )
    transaction = SimpleNamespace(
        root=tmp_path,
        project_repository=tmp_path / "project",
        basis=_composite_basis(),
        journal=journal,
    )

    acceptance_impl._validate_candidate_surface(transaction, {}, tmp_path)

    assert events == ["clone", "clone", "materialize", "validate"]


def test_advance_requests_approval_then_finishes_from_same_interface(tmp_path: Path) -> None:
    root, _tio, request, _base = _single_repository_acceptance(tmp_path)

    published = advance_acceptance(request)

    assert published.outcome is AcceptanceOutcome.APPROVAL_REQUIRED
    assert _git(root, "show", "main:design.txt") == "implemented"

    finished = advance_acceptance(
        AcceptanceRequest(
            root=request.root,
            slug=request.slug,
            basis=request.basis,
            cleanup=request.cleanup,
            ticket_status="done",
            allowed_board_rename=None,
        )
    )

    assert finished.outcome is AcceptanceOutcome.COMPLETE
    refs = _git(root, "for-each-ref", "--format=%(refname)", "refs/booley/acceptance")
    assert "/source-" not in refs
    assert "/finalized-" not in refs


def test_advance_rejects_ticket_head_changed_after_acceptance_freeze(tmp_path: Path) -> None:
    root, _tio, request, _base = _single_repository_acceptance(tmp_path)
    frozen_head = request.basis.participant("outer").authoring_sha
    _git(root, "switch", "change-target")
    (root / "design.txt").write_text("changed after handoff\n", encoding="utf-8")
    _git(root, "add", "design.txt")
    _git(root, "commit", "-m", "late change")
    _git(root, "switch", "main")
    guarded = AcceptanceRequest(
        root=request.root,
        slug=request.slug,
        basis=request.basis,
        cleanup=request.cleanup,
        ticket_status=request.ticket_status,
        allowed_board_rename=request.allowed_board_rename,
        expected_sources={"outer": frozen_head},
    )

    with pytest.raises(AcceptanceOperationError, match="changed after the accepted snapshot"):
        advance_acceptance(guarded)


def test_done_ticket_cannot_start_unpublished_acceptance(tmp_path: Path) -> None:
    root, _tio, request, base = _single_repository_acceptance(tmp_path)
    request = AcceptanceRequest(
        root=request.root,
        slug=request.slug,
        basis=request.basis,
        cleanup=request.cleanup,
        ticket_status="done",
        allowed_board_rename=None,
    )

    with pytest.raises(AcceptanceOperationError, match="done before acceptance publication"):
        advance_acceptance(request)

    assert _git(root, "rev-parse", "main") == base
    journal = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    assert not journal.exists()


def test_invalid_ticket_status_cannot_create_or_publish_acceptance(tmp_path: Path) -> None:
    root, _tio, request, base = _single_repository_acceptance(tmp_path)
    invalid = AcceptanceRequest(
        root=request.root,
        slug=request.slug,
        basis=request.basis,
        cleanup=request.cleanup,
        ticket_status="bogus",  # type: ignore[arg-type] - exercise the runtime boundary
        allowed_board_rename=None,
    )

    with pytest.raises(AcceptanceOperationError, match="invalid Ticket status"):
        advance_acceptance(invalid)

    assert _git(root, "rev-parse", "main") == base
    refs = _git(root, "for-each-ref", "--format=%(refname)", "refs/booley/acceptance")
    assert refs == ""
    journal = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    assert not journal.exists()


def test_completion_reports_premature_done_as_blocked(tmp_path: Path) -> None:
    root, tio, _request, base = _single_repository_acceptance(tmp_path)
    tio.entry["status"] = "done"

    assert complete_review_ticket(tio, "change-target", _Policy()) is False

    assert _git(root, "rev-parse", "main") == base


def test_destination_rewrite_after_approval_requires_inspection(tmp_path: Path) -> None:
    root, _tio, request, base = _single_repository_acceptance(tmp_path)
    assert advance_acceptance(request).outcome is AcceptanceOutcome.APPROVAL_REQUIRED
    _git(root, "update-ref", "refs/heads/main", base)
    done = AcceptanceRequest(
        root=request.root,
        slug=request.slug,
        basis=request.basis,
        cleanup=request.cleanup,
        ticket_status="done",
        allowed_board_rename=None,
    )

    with pytest.raises(AcceptanceRecoveryBlockedError, match="no longer contains"):
        advance_acceptance(done)

    assert _git(root, "rev-parse", "main") == base


def test_source_keepalive_preserves_pinned_commit_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, tio, request, _base = _single_repository_acceptance(tmp_path)
    store = FaultingAcceptanceStore(
        FileAcceptanceStore(), AcceptanceCheckpoint.SOURCES_PINNED, "after"
    )
    runner = acceptance_impl._AcceptanceRunner(store, LocalAcceptanceRepositories())
    complete = tio.find_ticket("change-target")
    assert complete is not None
    assert complete["status"] == "review"
    with pytest.raises(OSError, match="after sources-pinned checkpoint"):
        runner.advance(request)

    journal_path = FileAcceptanceStore().path(root, "change-target")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    source = journal["sources"]["outer"]
    source_ref = f"refs/booley/acceptance/{journal['transaction']}/source-outer"
    assert _git(root, "rev-parse", source_ref) == source

    _git(root, "update-ref", "-d", "refs/heads/change-target", source)
    _git(root, "reflog", "expire", "--expire=now", "--all")
    _git(root, "gc", "--prune=now")
    assert _git(root, "cat-file", "-t", source) == "commit"

    _git(root, "update-ref", "refs/heads/change-target", source)

    def reject_mutable_ref_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("journaled source identities must not be re-read")

    monkeypatch.setattr(acceptance_impl, "pin_basis_refs", reject_mutable_ref_read)
    assert advance_acceptance(request).outcome is AcceptanceOutcome.APPROVAL_REQUIRED
    finished = advance_acceptance(
        AcceptanceRequest(
            root=request.root,
            slug=request.slug,
            basis=request.basis,
            cleanup=request.cleanup,
            ticket_status="done",
            allowed_board_rename=None,
        )
    )
    assert finished.outcome is AcceptanceOutcome.COMPLETE
    refs = _git(root, "for-each-ref", "--format=%(refname)", source_ref)
    assert refs == ""


def test_prepared_ref_survives_checkpoint_interruption_and_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _tio, request, _base = _single_repository_acceptance(tmp_path)
    store = FaultingAcceptanceStore(
        FileAcceptanceStore(), AcceptanceCheckpoint.CANDIDATES_PREPARED, "before"
    )
    runner = acceptance_impl._AcceptanceRunner(store, LocalAcceptanceRepositories())
    with pytest.raises(OSError, match="before candidates-prepared checkpoint"):
        runner.advance(request)

    journal_path = FileAcceptanceStore().path(root, "change-target")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    prepared_ref = f"refs/booley/acceptance/{journal['transaction']}/outer"
    prepared = _git(root, "rev-parse", prepared_ref)
    source = _git(root, "rev-parse", "refs/heads/change-target")
    _git(root, "update-ref", "-d", "refs/heads/change-target", source)
    _git(root, "reflog", "expire", "--expire=now", "--all")
    _git(root, "gc", "--prune=now")
    assert _git(root, "cat-file", "-t", prepared) == "commit"

    _git(root, "update-ref", "refs/heads/change-target", source)
    assert advance_acceptance(request).outcome is AcceptanceOutcome.APPROVAL_REQUIRED


def test_orphaned_acceptance_ref_blocks_new_journal(tmp_path: Path) -> None:
    root, _tio, request, base = _single_repository_acceptance(tmp_path)
    orphan = "refs/booley/acceptance/0123456789abcdef0123456789abcdef/source-outer"
    _git(root, "update-ref", orphan, base)

    with pytest.raises(AcceptanceOperationError, match="orphaned acceptance ref"):
        advance_acceptance(request)

    assert _git(root, "rev-parse", "main") == base
