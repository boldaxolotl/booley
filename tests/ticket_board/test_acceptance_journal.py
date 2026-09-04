"""Interface tests for the active Acceptance Journal module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from booley.ticket_board.acceptance_journal import (
    AcceptanceOperationError,
    AcceptanceOutcome,
    AcceptanceRecoveryBlockedError,
    AcceptanceRequest,
    advance_acceptance,
)
from booley.ticket_board.acceptance_journal import _advance as acceptance_impl
from booley.ticket_board.completion import complete_review_ticket
from booley.ticket_board.target_contract import ContractParticipant
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
    participant = ContractParticipant(
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
        contract=contract,
        cleanup=False,
        ticket_status="review",
        allowed_board_rename=None,
    )
    return root, tio, request, base


def test_advance_requests_approval_then_finishes_from_same_interface(tmp_path: Path) -> None:
    root, _tio, request, _base = _single_repository_acceptance(tmp_path)

    published = advance_acceptance(request)

    assert published.outcome is AcceptanceOutcome.APPROVAL_REQUIRED
    assert _git(root, "show", "main:design.txt") == "implemented"

    finished = advance_acceptance(
        AcceptanceRequest(
            root=request.root,
            slug=request.slug,
            contract=request.contract,
            cleanup=request.cleanup,
            ticket_status="done",
            allowed_board_rename=None,
        )
    )

    assert finished.outcome is AcceptanceOutcome.COMPLETE
    refs = _git(root, "for-each-ref", "--format=%(refname)", "refs/booley/acceptance")
    assert "/source-" not in refs
    assert "/finalized-" not in refs


def test_done_ticket_cannot_start_unpublished_acceptance(tmp_path: Path) -> None:
    root, _tio, request, base = _single_repository_acceptance(tmp_path)
    request = AcceptanceRequest(
        root=request.root,
        slug=request.slug,
        contract=request.contract,
        cleanup=request.cleanup,
        ticket_status="done",
        allowed_board_rename=None,
    )

    with pytest.raises(AcceptanceOperationError, match="done before acceptance publication"):
        advance_acceptance(request)

    assert _git(root, "rev-parse", "main") == base


def test_invalid_ticket_status_cannot_create_or_publish_acceptance(tmp_path: Path) -> None:
    root, _tio, request, base = _single_repository_acceptance(tmp_path)
    invalid = AcceptanceRequest(
        root=request.root,
        slug=request.slug,
        contract=request.contract,
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
        contract=request.contract,
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
    validate_surface = acceptance_impl._validate_source_surface

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise AcceptanceOperationError("interrupt after source pinning")

    monkeypatch.setattr(acceptance_impl, "_validate_source_surface", interrupt)
    complete = tio.find_ticket("change-target")
    assert complete is not None
    assert complete["status"] == "review"
    with pytest.raises(AcceptanceOperationError, match="after source pinning"):
        advance_acceptance(request)

    journal_path = acceptance_impl._journal_path(root, "change-target")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    source = journal["sources"]["outer"]
    source_ref = f"refs/booley/acceptance/{journal['transaction']}/source-outer"
    assert _git(root, "rev-parse", source_ref) == source

    _git(root, "update-ref", "-d", "refs/heads/change-target", source)
    _git(root, "reflog", "expire", "--expire=now", "--all")
    _git(root, "gc", "--prune=now")
    assert _git(root, "cat-file", "-t", source) == "commit"

    _git(root, "update-ref", "refs/heads/change-target", source)
    monkeypatch.setattr(acceptance_impl, "_validate_source_surface", validate_surface)

    def reject_mutable_ref_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("journaled source identities must not be re-read")

    monkeypatch.setattr(acceptance_impl, "pin_sealed_refs", reject_mutable_ref_read)
    assert advance_acceptance(request).outcome is AcceptanceOutcome.APPROVAL_REQUIRED
    finished = advance_acceptance(
        AcceptanceRequest(
            root=request.root,
            slug=request.slug,
            contract=request.contract,
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
    write_journal = acceptance_impl._write_journal
    interrupted = False

    def interrupt_candidate_checkpoint(
        path: Path,
        journal: dict[str, Any],
        checkpoint: Any,
    ) -> None:
        nonlocal interrupted
        if journal["candidates"] and not interrupted:
            interrupted = True
            raise OSError("before candidate checkpoint")
        write_journal(path, journal, checkpoint)

    monkeypatch.setattr(acceptance_impl, "_write_journal", interrupt_candidate_checkpoint)
    with pytest.raises(OSError, match="before candidate checkpoint"):
        advance_acceptance(request)

    journal_path = acceptance_impl._journal_path(root, "change-target")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    prepared_ref = f"refs/booley/acceptance/{journal['transaction']}/outer"
    prepared = _git(root, "rev-parse", prepared_ref)
    source = _git(root, "rev-parse", "refs/heads/change-target")
    _git(root, "update-ref", "-d", "refs/heads/change-target", source)
    _git(root, "reflog", "expire", "--expire=now", "--all")
    _git(root, "gc", "--prune=now")
    assert _git(root, "cat-file", "-t", prepared) == "commit"

    _git(root, "update-ref", "refs/heads/change-target", source)
    monkeypatch.setattr(acceptance_impl, "_write_journal", write_journal)
    assert advance_acceptance(request).outcome is AcceptanceOutcome.APPROVAL_REQUIRED


def test_orphaned_acceptance_ref_blocks_new_journal(tmp_path: Path) -> None:
    root, _tio, request, base = _single_repository_acceptance(tmp_path)
    orphan = "refs/booley/acceptance/0123456789abcdef0123456789abcdef/source-outer"
    _git(root, "update-ref", orphan, base)

    with pytest.raises(AcceptanceOperationError, match="orphaned acceptance ref"):
        advance_acceptance(request)

    assert _git(root, "rev-parse", "main") == base
