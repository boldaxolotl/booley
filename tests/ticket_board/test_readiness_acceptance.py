"""Focused boundary tests for Acceptance Basis helper modules."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.ticket_board import (
    readiness,
)
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
)


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


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
