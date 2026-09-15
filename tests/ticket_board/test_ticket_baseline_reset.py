"""Ticket baseline reset refuses to discard runtime state without an authoritative baseline."""

from pathlib import Path

import pytest
import yaml

from booley.ticket_board import TicketIO, operations


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
        ("acceptance_basis", {"schema": 1}, "Old Ticket format"),
        ("machine", {"schema": 1}, "Draft Ticket cannot contain generated"),
    ],
)
def test_enqueue_draft_rejects_retired_contract_and_published_machine_state(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    ticket = tmp_path / "ticket.md"
    fields = {
        "summary": "Ticket",
        "type": "feature",
        "branch": "main",
        "scope": [],
        "on_success": ["review"],
        "CRITERIA_MANDATORY": {"REVIEW": {"rtl": {"bugs": "clean"}}},
        field: value,
    }
    ticket.write_text(
        "---\n" + yaml.safe_dump(fields, sort_keys=False) + "---\n\n## Description\n\nTest.\n",
        encoding="utf-8",
    )
    tio = TicketIO(tmp_path / "tickets", project_root=tmp_path)
    with pytest.raises(ValueError, match=message):
        tio._convert_ticket(ticket, "ticket", "draft")
