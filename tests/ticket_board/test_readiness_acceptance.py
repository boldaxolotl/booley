"""Public readiness checks at the executable-ticket boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.ticket_board import readiness


def test_readiness_delegates_prepared_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    project_dir = root / ".booley_project"
    ticket = project_dir / "tickets/board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    (root / ".git").mkdir()
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda selected_root, slug: calls.append((selected_root, slug)) or [],
    )

    assert readiness.check_ticket_ready(root, "ticket").ready
    assert calls == [(root.resolve(), "ticket")]


def test_readiness_checkout_boundary_and_preparation_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    tickets = root / ".booley_project/tickets"
    ticket = tickets / "board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        readiness, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (ticket, "queue"))
    ticket.write_text("---\ntarget_contract: {}\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda *_args: ["missing required fields"],
    )
    assert "missing required fields" in readiness.check_ticket_ready(root, "ticket").errors[0]
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda *_args: ["prepare failed"],
    )
    assert "prepare failed" in readiness.check_ticket_ready(root, "ticket").errors[0]


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
    ticket = tickets / "board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setattr(
        readiness, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (ticket, "queue"))
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda *_args: ["project participant repository is missing"],
    )
    assert "project participant repository is missing" in readiness.check_ticket_ready(
        root, "ticket"
    ).errors[0]
