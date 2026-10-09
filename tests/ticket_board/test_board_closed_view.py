"""Board views hide Closed Tickets unless asked with ``--all`` (ADR 0065, phase 6)."""

from __future__ import annotations

import json
import re

import pytest

from booley.ticket_board import cli
from booley.ticket_board.board_layout import history_document_path
from booley.ticket_board.frontmatter import format_frontmatter
from booley.ticket_board.scanner import scan_all_tickets
from booley.ticket_board.ticket_history import read_closed_ticket

from .conftest import make_closed_ticket, make_ticket_file, place_closed_ticket, place_ticket


@pytest.fixture
def board(tio):
    """A board with one live Ticket and two Closed Tickets, one per outcome."""
    make_ticket_file(tio, "queue", "live")
    make_closed_ticket(tio, "shipped", "done")
    make_closed_ticket(tio, "dropped", "archived")
    return tio


def _statuses(entries: list[dict]) -> dict[str, str]:
    return {entry["file"]: entry["status"] for entry in entries}


def test_scan_lists_live_tickets_only_by_default(board):
    assert _statuses(scan_all_tickets(board.tickets_dir)) == {"board/live.md": "queued"}


def test_scan_with_closed_adds_history_under_its_outcome(board):
    entries = scan_all_tickets(board.tickets_dir, include_closed=True)

    assert _statuses(entries) == {
        "board/live.md": "queued",
        "history/shipped.md": "done",
        "history/dropped.md": "archived",
    }
    closed = {entry["file"]: entry.get("closed") for entry in entries}
    assert closed["board/live.md"] is None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", closed["history/shipped.md"])


def test_closed_block_does_not_change_how_the_document_converts(tio):
    """A Closed Ticket converts exactly like its live twin: the block never leaks in."""
    fields = {
        "summary": "Same document",
        "type": "feature",
        "branch": "master",
        "scope": [],
        "on_success": ["review"],
        "CRITERIA_MANDATORY": {"REVIEW": {"rtl": {"bugs": "done"}}},
    }
    document = format_frontmatter(fields, "## Description\nWork.\n")
    place_ticket(tio.tickets_dir, "twin", "review", document)
    place_closed_ticket(tio.tickets_dir, "shipped", document, "done")

    entries = {e["file"]: e for e in scan_all_tickets(tio.tickets_dir, include_closed=True)}
    live, closed = entries["board/twin.md"], entries["history/shipped.md"]
    assert closed.get("ticket_error") == live.get("ticket_error")


@pytest.mark.parametrize("command", ["board", "show", "read-board"])
def test_ticket_board_cli_takes_all_flag(board, monkeypatch, capsys, command):
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: board.tickets_dir)

    assert cli.main([command]) == 0
    assert "shipped" not in capsys.readouterr().out
    assert cli.main([command, "--all"]) == 0
    out = capsys.readouterr().out
    assert "shipped" in out
    assert "dropped" in out


def test_read_board_json_carries_closed_date(board, monkeypatch, capsys):
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: board.tickets_dir)

    assert cli.main(["read-board", "--all"]) == 0
    tickets = json.loads(capsys.readouterr().out)["tickets"]
    assert {t["file"]: t.get("closed") for t in tickets}["history/dropped.md"] is not None


def _closed_date(tio, slug: str) -> str:
    return read_closed_ticket(tio.tickets_dir, slug).block.date


def test_closed_rows_are_dated_and_sorted_by_their_closing(board):
    entries = {e["file"]: e for e in scan_all_tickets(board.tickets_dir, include_closed=True)}

    assert entries["history/shipped.md"]["last_update"] == _closed_date(board, "shipped")


def test_malformed_history_record_is_noted_not_fatal(board, monkeypatch, capsys):
    bad = history_document_path(board.tickets_dir, "bad")
    bad.write_text("---\nclosed:\n  outcome: maybe\n---\n", encoding="utf-8")

    entries = {e["file"]: e for e in scan_all_tickets(board.tickets_dir, include_closed=True)}
    assert entries["history/shipped.md"]["status"] == "done"
    assert "bad.md is invalid" in entries["history/bad.md"]["ticket_error"]

    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: board.tickets_dir)
    assert cli.main(["board", "--all"]) == 0
    assert "shipped" in capsys.readouterr().out


@pytest.mark.parametrize(
    "content,message",
    [
        (b"---\nsummary: x\n---\n", "no closed block"),
        (b"---\nsummary: \xff\n---\n", "unreadable"),
    ],
)
def test_history_document_without_a_record_is_an_error_row(
    board, monkeypatch, capsys, content, message
):
    """Unreadable or blockless history lists as an error row, not a failed listing."""
    bad = history_document_path(board.tickets_dir, "bad")
    bad.write_bytes(content)

    entries = {e["file"]: e for e in scan_all_tickets(board.tickets_dir, include_closed=True)}
    assert entries["history/shipped.md"]["status"] == "done"
    assert entries["history/bad.md"]["status"] == "closed"
    assert message in entries["history/bad.md"]["ticket_error"]

    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: board.tickets_dir)
    assert cli.main(["board", "--all"]) == 0
    assert "shipped" in capsys.readouterr().out


def _module_board(board, monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: board.tickets_dir)
    words = [word for word in argv[1:] if word != "--all"]
    words = words or ["board"]
    if "--all" in argv:
        words.append("--all")
    try:
        return cli.main(words)
    except SystemExit as exc:
        return int(exc.code)


@pytest.mark.parametrize(
    "argv", [["board", "--all"], ["board", "show", "--all"], ["board", "--all", "show"]]
)
def test_module_board_takes_all_flag(board, monkeypatch, capsys, argv):
    plain = [arg for arg in argv if arg != "--all"]
    assert _module_board(board, monkeypatch, plain) == 0
    assert "shipped" not in capsys.readouterr().out

    assert _module_board(board, monkeypatch, argv) == 0
    assert "shipped" in capsys.readouterr().out


def test_module_board_refuses_all_on_commands_that_do_not_list(board, monkeypatch, capsys):
    assert _module_board(board, monkeypatch, ["board", "--all", "reset", "live"]) == 2
    assert "--all" in capsys.readouterr().err


def test_module_board_show_finds_a_closed_ticket(board, monkeypatch, capsys):
    assert _module_board(board, monkeypatch, ["board", "show", "dropped"]) == 0
    out = capsys.readouterr().out
    assert "ticket:    dropped" in out
    assert "status:    archived" in out
    assert _closed_date(board, "dropped") in out


@pytest.mark.parametrize("slug", ["shipped", "shipped.md"])
def test_ticket_board_show_finds_a_closed_ticket(board, monkeypatch, capsys, slug):
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: board.tickets_dir)

    assert cli.main(["show", slug]) == 0
    out = capsys.readouterr().out
    assert "status:    done" in out
    assert f"closed:    {_closed_date(board, 'shipped')}" in out
    # Like a live Ticket's, the path is the host's native absolute path.
    assert f"file:      {history_document_path(board.tickets_dir, 'shipped')}" in out
