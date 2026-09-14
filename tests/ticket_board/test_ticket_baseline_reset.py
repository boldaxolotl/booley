"""Ticket baseline reset refuses to discard runtime state without an authoritative baseline."""

from pathlib import Path

import pytest

from booley.ticket_board import TicketIO, operations
from booley.ticket_board.frontmatter import format_frontmatter


def test_reset_and_preflight_reject_missing_authoritative_ticket_baseline(
    tmp_path: Path, capsys
) -> None:
    entry = {"machine": {"schema": 1}, "branch": "main"}
    assert operations._preflight_reset_branches(tmp_path, "ticket", entry, None) is None
    assert "authoritative Ticket baseline is unavailable" in capsys.readouterr().err
    assert operations._reset_ticket_branches(tmp_path, "ticket", entry, None) is False
    assert "authoritative Ticket baseline is unavailable" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("acceptance_basis", {"schema": 1}, "without acceptance_basis"),
        ("machine", {"schema": 1}, "draft Tickets cannot contain machine"),
    ],
)
def test_enqueue_draft_rejects_retired_contract_and_published_machine_state(
    tmp_path: Path, capsys, field: str, value: object, message: str
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        format_frontmatter({"branch": "main", field: value}, "body"), encoding="utf-8"
    )
    tio = TicketIO(tmp_path / "tickets", project_root=tmp_path)
    assert tio._draft_enqueue_fields(ticket, None) is None
    assert message in capsys.readouterr().err
