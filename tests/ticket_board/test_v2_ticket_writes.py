"""V2 Ticket writes preserve the authored Ticket contract."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime.project_dir import reset_cache
from booley.ticket_board import basis_refresh, ticket_document
from booley.ticket_board.io import TicketIO
from booley.ticket_board.operations import op_promote_waiting
from booley.ticket_board.readiness import check_ticket_ready
from booley.ticket_board.ticket_document import (
    convert_ticket_document,
    ticket_conversion_context,
)
from booley.ticket_board.workspace_ops import prepare_converted_ticket_baseline


def _git_project(tmp_path: Path, monkeypatch) -> tuple[Path, TicketIO]:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project / ".booley_project"))
    reset_cache()
    for command in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "."),
        ("add", "-f", ".booley_project/booley.toml", ".booley_project/.gitignore"),
        ("commit", "-qm", "baseline"),
    ):
        subprocess.run(["git", *command], cwd=project, check=True, capture_output=True)
    return project, TicketIO(project / ".booley_project" / "tickets", project_root=project)


def _draft_document() -> str:
    return (
        "---\n"
        "summary: Preserve authored whitespace\n"
        "type: feature\n"
        "branch: main\n"
        "scope: [README.md]\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY:\n"
        "  REVIEW: {rtl: {bugs: done}}\n"
        "---\n"
        "\n## Description\n\nPreserve this Ticket.  \n\n"
    )


def _without_machine_node(source: str) -> str:
    lines = source.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.startswith("machine:"))
    end = start + 1
    while end < len(lines) and (lines[end].startswith(" ") or not lines[end].strip()):
        end += 1
    return "".join((*lines[:start], *lines[end:]))


def test_waiting_basis_refresh_preserves_v2_authored_body(tmp_path: Path, monkeypatch) -> None:
    project, board = _git_project(tmp_path, monkeypatch)
    created = board.create_ticket_document("preserve-v2", _draft_document())
    assert created is not None
    prepare_converted_ticket_baseline(project, created, "preserve-v2")
    assert board.enqueue_ticket("preserve-v2")
    queued = board.tickets_dir / "board" / "queue" / "preserve-v2.md"
    waiting = board.tickets_dir / "board" / "waiting" / queued.name
    waiting.parent.mkdir(parents=True, exist_ok=True)
    queued.replace(waiting)
    before = waiting.read_text(encoding="utf-8")
    with ticket_conversion_context(project, "preserve-v2", "executable") as context:
        old = convert_ticket_document(before, context)
    assert old.document is not None
    prepared_machine = old.document.generated["machine"]
    prepared_basis = board.load_basis("preserve-v2")
    monkeypatch.setattr(
        basis_refresh,
        "prepare_waiting_basis_refresh",
        lambda *_args: (prepared_basis, "a" * 32),
    )
    monkeypatch.setattr(
        basis_refresh,
        "load_basis_refresh",
        lambda *_args: SimpleNamespace(state="prepared", machine=prepared_machine),
    )
    monkeypatch.setattr(basis_refresh, "finish_basis_refresh", lambda *_args: None)

    assert op_promote_waiting(board) == [
        {"slug": "preserve-v2", "summary": "Preserve authored whitespace"}
    ]

    promoted = board.tickets_dir / "board" / "queue" / waiting.name
    after = promoted.read_text(encoding="utf-8")
    with ticket_conversion_context(project, "preserve-v2", "executable") as context:
        new = convert_ticket_document(after, context)
    assert new.document is not None
    assert new.document.spec.body == old.document.spec.body
    assert new.document.spec.semantic_digest() == old.document.spec.semantic_digest()
    assert _without_machine_node(after) == _without_machine_node(before)
    assert new.document.generated["machine"] == prepared_machine
    assert (
        new.document.generated["machine"]["authored_sha256"] == new.document.spec.semantic_digest()
    )
    assert check_ticket_ready(project, "preserve-v2").errors == ()


def test_waiting_refresh_retries_after_ticket_serialization_failure(
    tmp_path: Path, monkeypatch
) -> None:
    project, board = _git_project(tmp_path, monkeypatch)
    created = board.create_ticket_document("retry-v2", _draft_document())
    assert created is not None
    prepare_converted_ticket_baseline(project, created, "retry-v2")
    assert board.enqueue_ticket("retry-v2")
    queued = board.tickets_dir / "board/queue/retry-v2.md"
    waiting = board.tickets_dir / "board/waiting/retry-v2.md"
    waiting.parent.mkdir(parents=True, exist_ok=True)
    queued.replace(waiting)
    original_bytes = waiting.read_bytes()
    with ticket_conversion_context(project, "retry-v2", "executable") as context:
        converted = convert_ticket_document(waiting.read_text(encoding="utf-8"), context)
    assert converted.document is not None
    prepared_machine = converted.document.generated["machine"]
    prepared_basis = board.load_basis("retry-v2")
    journal = SimpleNamespace(state="prepared", machine=prepared_machine)
    monkeypatch.setattr(
        basis_refresh,
        "prepare_waiting_basis_refresh",
        lambda *_args: (prepared_basis, "b" * 32),
    )
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args: journal)
    monkeypatch.setattr(basis_refresh, "recover_published_basis_refreshes", lambda *_args: None)
    finished: list[str] = []
    monkeypatch.setattr(
        basis_refresh,
        "finish_basis_refresh",
        lambda _root, _slug, operation: finished.append(operation),
    )
    original_serializer = ticket_document.serialize_ticket_document
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("injected serialization failure")
        return original_serializer(*args, **kwargs)

    monkeypatch.setattr(ticket_document, "serialize_ticket_document", fail_once)

    with pytest.raises(ValueError, match="injected serialization failure"):
        op_promote_waiting(board)
    assert waiting.read_bytes() == original_bytes
    assert finished == []

    assert op_promote_waiting(board) == [
        {"slug": "retry-v2", "summary": "Preserve authored whitespace"}
    ]
    assert (board.tickets_dir / "board/queue/retry-v2.md").is_file()
    assert finished == ["b" * 32]
