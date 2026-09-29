"""V2 Ticket writes preserve the authored Ticket contract."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from booley.runtime.project_dir import reset_cache
from booley.ticket_board import basis_refresh, ticket_document
from booley.ticket_board.io import TicketIO
from booley.ticket_board.operations import op_promote_waiting
from booley.ticket_board.readiness import check_ticket_ready
from booley.ticket_board.ticket_baseline import ticket_machine_from_spec
from booley.ticket_board.ticket_document import (
    convert_ticket_document,
    ticket_conversion_context,
)
from booley.ticket_board.workspace_ops import (
    AuthoringWorkspace,
    prepare_converted_ticket_baseline,
    prepare_replacement_ticket_baseline,
)


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


def _enqueue_waiting(project: Path, board: TicketIO, slug: str) -> Path:
    created = board.create_ticket_document(slug, _draft_document())
    assert created is not None
    prepare_converted_ticket_baseline(project, created, slug)
    assert board.enqueue_ticket(slug)
    queued = board.tickets_dir / "board" / "queue" / f"{slug}.md"
    waiting = board.tickets_dir / "board" / "waiting" / queued.name
    waiting.parent.mkdir(parents=True, exist_ok=True)
    queued.replace(waiting)
    return waiting


def _write_prepared_refresh(project: Path, board: TicketIO, slug: str, operation: str):
    ticket = board.tickets_dir / "board/waiting" / f"{slug}.md"
    with ticket_conversion_context(project, slug, "executable") as context:
        converted = convert_ticket_document(ticket.read_text(encoding="utf-8"), context)
    assert converted.document is not None
    old_basis = board.load_basis(slug)
    operation_path = basis_refresh._operation_path(project, operation)
    candidate = operation_path / "new-outer"
    candidate.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            f"booley-generation/{'b' * 16}/{slug}",
            str(candidate),
            "main",
        ],
        cwd=project,
        check=True,
        capture_output=True,
    )
    workspace = AuthoringWorkspace(
        candidate,
        None,
        old_basis.participant("outer").destination_sha,
        "",
        "b" * 16,
    )
    refreshed, _operation = prepare_replacement_ticket_baseline(
        project, ticket, slug, workspace, (), operation_id=operation
    )
    machine = ticket_machine_from_spec(refreshed, converted.document.spec, generation=operation)
    journal = basis_refresh.BasisRefreshJournal(
        1,
        operation,
        "b" * 16,
        slug,
        old_basis.ticket_identity()["generation"],
        "prepared",
        machine,
    )
    basis_refresh._write_journal(project, journal)
    return machine, journal


def test_waiting_basis_refresh_preserves_v2_authored_body(tmp_path: Path, monkeypatch) -> None:
    project, board = _git_project(tmp_path, monkeypatch)
    waiting = _enqueue_waiting(project, board, "preserve-v2")
    before = waiting.read_text(encoding="utf-8")
    with ticket_conversion_context(project, "preserve-v2", "executable") as context:
        old = convert_ticket_document(before, context)
    assert old.document is not None
    prepared_machine, journal = _write_prepared_refresh(project, board, "preserve-v2", "a" * 32)
    original_finish = basis_refresh.finish_basis_refresh
    finished: list[str] = []

    def finish(root: Path, slug: str, operation: str) -> None:
        finished.append(operation)
        original_finish(root, slug, operation)

    monkeypatch.setattr(basis_refresh, "finish_basis_refresh", finish)

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
    assert new.document.generated["machine"] != old.document.generated["machine"]
    assert (
        new.document.generated["machine"]["authored_sha256"] == new.document.spec.semantic_digest()
    )
    assert check_ticket_ready(project, "preserve-v2").errors == ()
    assert finished == [journal.operation_id]
    assert basis_refresh.load_basis_refresh(project, "preserve-v2") is None
    assert not basis_refresh._operation_path(project, journal.operation_id).exists()


def test_waiting_refresh_serialization_failure_is_blocked_and_scan_continues(
    tmp_path: Path, monkeypatch
) -> None:
    project, board = _git_project(tmp_path, monkeypatch)
    waiting = _enqueue_waiting(project, board, "a-fail-v2")
    _enqueue_waiting(project, board, "z-success-v2")
    original_bytes = waiting.read_bytes()
    _prepared_machine, journal = _write_prepared_refresh(project, board, "a-fail-v2", "c" * 32)
    finished: list[str] = []
    original_finish = basis_refresh.finish_basis_refresh

    def finish(root: Path, slug: str, operation: str) -> None:
        finished.append(operation)
        original_finish(root, slug, operation)

    monkeypatch.setattr(
        basis_refresh,
        "finish_basis_refresh",
        finish,
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

    assert op_promote_waiting(board) == [
        {"slug": "z-success-v2", "summary": "Preserve authored whitespace"}
    ]
    blocked = board.tickets_dir / "board/blocked/a-fail-v2.md"
    assert blocked.read_bytes() == original_bytes
    assert (board.tickets_dir / "board/queue/z-success-v2.md").is_file()
    assert finished == []
    assert basis_refresh.load_basis_refresh(project, "a-fail-v2") == journal
