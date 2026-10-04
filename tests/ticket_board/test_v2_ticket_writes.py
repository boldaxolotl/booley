"""V2 Ticket writes preserve the authored Ticket contract."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from booley.runtime.project_dir import reset_cache
from booley.ticket_board import basis_refresh, ticket_document
from booley.ticket_board.board_layout import (
    read_state_record,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.io import TicketIO
from booley.ticket_board.lifecycle import TicketState
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

from .conftest import place_ticket


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
    queued = read_state_record(board.tickets_dir, slug)
    assert queued is not None and queued.state is TicketState.QUEUED
    write_state_record(board.tickets_dir, slug, queued.with_state(TicketState.WAITING))
    return ticket_document_path(board.tickets_dir, slug)


def _state(board: TicketIO, slug: str) -> TicketState | None:
    """Return the recorded lifecycle state of *slug* (``None`` for a draft)."""
    record = read_state_record(board.tickets_dir, slug)
    return None if record is None else record.state


def _write_prepared_refresh(project: Path, board: TicketIO, slug: str, operation: str):
    ticket = ticket_document_path(board.tickets_dir, slug)
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

    assert _state(board, "preserve-v2") is TicketState.QUEUED
    after = waiting.read_text(encoding="utf-8")
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


# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).
@pytest.mark.timeout(120)
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
    assert _state(board, "a-fail-v2") is TicketState.BLOCKED
    assert waiting.read_bytes() == original_bytes
    assert _state(board, "z-success-v2") is TicketState.QUEUED
    assert ticket_document_path(board.tickets_dir, "z-success-v2").is_file()
    assert finished == []
    assert basis_refresh.load_basis_refresh(project, "a-fail-v2") == journal


def test_prepare_spec_fields_handles_legacy_invalid_and_removed_generated_values(
    tmp_path: Path, monkeypatch
) -> None:
    project, board = _git_project(tmp_path, monkeypatch)
    legacy = place_ticket(
        board.tickets_dir, "legacy", "drafts", "---\nsummary: Legacy\n---\n\nBody\n"
    )

    prepared = board._prepare_spec_fields(legacy, {"feature_branch": "legacy"})

    assert prepared is not None
    assert b"feature_branch: legacy" in prepared

    invalid = place_ticket(
        board.tickets_dir, "invalid", "queue", "---\nCRITERIA_MANDATORY: {}\n---\n"
    )
    with pytest.raises(ValueError, match="Ticket document is invalid"):
        board._prepare_spec_fields(invalid, {"created": "2026-09-29T00:00:00Z"})
    invalid.unlink()

    waiting = _enqueue_waiting(project, board, "remove-generated")
    without_branch = board._prepare_spec_fields(waiting, {"feature_branch": ""})
    assert without_branch is not None
    with ticket_conversion_context(project, "remove-generated", "executable") as context:
        converted = convert_ticket_document(without_branch.decode(), context)
    assert converted.document is not None
    assert "feature_branch" not in converted.document.generated


def test_waiting_refresh_without_prepared_journal_is_blocked(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project, board = _git_project(tmp_path, monkeypatch)
    waiting = _enqueue_waiting(project, board, "missing-refresh-journal")
    before = waiting.read_bytes()
    monkeypatch.setattr(
        basis_refresh,
        "prepare_waiting_basis_refresh",
        lambda *_args, **_kwargs: (object(), "d" * 32),
    )
    monkeypatch.setattr(basis_refresh, "load_basis_refresh", lambda *_args, **_kwargs: None)

    assert op_promote_waiting(board) == []

    assert _state(board, "missing-refresh-journal") is TicketState.BLOCKED
    assert waiting.read_bytes() == before
    assert "prepared waiting Ticket metadata is unavailable" in capsys.readouterr().err


def _interrupt_refresh_snapshot(patch, snapshot):
    replace_bytes = basis_refresh.atomic_replace_bytes

    def interrupt_snapshot(path, content, **kwargs):
        replace_bytes(path, content, **kwargs)
        if path == snapshot:
            raise OSError("runtime interrupted")

    patch.setattr(basis_refresh, "atomic_replace_bytes", interrupt_snapshot)


@pytest.mark.parametrize("interruption", ["snapshot", "finish"])
# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).
@pytest.mark.timeout(90)
def test_refresh_reconciles_existing_unexecuted_runtime_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board.acceptance_ledger import record_changes
    from booley.ticket_board.paths import runtime_file
    from booley.ticket_board.ticket_baseline import TicketBaselineError

    project, board = _git_project(tmp_path, monkeypatch)
    ticket = _enqueue_waiting(project, board, "runtime-refresh")
    old = board.load_basis("runtime-refresh").ticket_identity()
    snapshot = board.logs_dir / "runtime-refresh/ticket.md"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(ticket.read_bytes())
    state = DevelopmentState.load(
        runtime_file(board.logs_dir, "runtime-refresh", "booley_state.json")
    )
    state.slug = "runtime-refresh"
    state.init_criteria({"review_rtl_bugs_clean": True})
    changes = state.set_criterion("review_rtl_bugs_clean", False)
    record_changes(
        snapshot.parent,
        state,
        changes,
        invocation_id="partial",
        producer="review",
        execution_id="partial",
        ticket_identity=old,
        transaction_id="c" * 64,
    )
    state.acceptance_transactions = ["c" * 64]
    state.save()
    machine, journal = _write_prepared_refresh(project, board, "runtime-refresh", "a" * 32)
    with monkeypatch.context() as patch:
        if interruption == "snapshot":
            _interrupt_refresh_snapshot(patch, snapshot)
        else:
            patch.setattr(
                basis_refresh,
                "finish_basis_refresh",
                lambda *_args: (_ for _ in ()).throw(OSError("runtime interrupted")),
            )
        with pytest.raises(OSError, match="runtime interrupted"):
            op_promote_waiting(board)
    with pytest.raises(TicketBaselineError, match="publication is pending"):
        board.load_basis("runtime-refresh")
    if interruption == "snapshot":
        assert op_promote_waiting(board)
    else:
        # The ordinary promotion entry point recovers a queued, already-published refresh.
        assert op_promote_waiting(board) == []
    identity = board.load_basis("runtime-refresh", runtime_ticket_path=snapshot).ticket_identity()
    assert identity == machine and identity != old
    rebuilt = DevelopmentState.load(state._file_path)
    assert rebuilt.acceptance_transactions == []
    assert (
        snapshot.parent / "basis-refreshes" / f"{journal.operation_id}.prior-state.json"
    ).exists()
    assert len(list((snapshot.parent / "acceptance/evidence").glob("*/record.json"))) == 1
