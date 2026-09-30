"""Public enqueue contracts plus deterministic crash-checkpoint fault injection.

The direct private-helper tests are limited to filesystem cutover checkpoints whose
partial states cannot be produced reliably through the complete public transaction.
"""

from __future__ import annotations

import errno
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from booley.ticket_board import (
    enqueue_publication,
)
from booley.ticket_board.board_layout import (
    StateRecord,
    StateRecordError,
    delete_state_record,
    read_state_record,
    state_record_path,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.ticket_baseline import (
    BasisParticipant,
    TicketBaseline,
    ticket_machine_fields,
)


def _completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


def _enqueue_journal(tmp_path: Path) -> enqueue_publication.EnqueueJournal:
    operation_id = "0" * 32
    machine = ticket_machine_fields(
        TicketBaseline((_participant(),)), fields={}, body="", generation=operation_id
    )
    digest = "1" * 64
    operation = tmp_path / "operation"
    return enqueue_publication.EnqueueJournal(
        2,
        operation_id,
        "ticket",
        "prepared",
        str(tmp_path / "tickets/board/ticket.md"),
        digest,
        str(operation / "ticket.md"),
        "2" * 64,
        str(operation / "source.md"),
        False,
        "now",
        machine,
    )


def _bind_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    data = tmp_path / "data"
    (data / "tickets" / "board").mkdir(parents=True)
    runtime_alias = tmp_path / "booley-project"
    runtime_alias.symlink_to(data, target_is_directory=True)
    checkout = tmp_path / "work"
    checkout.mkdir()
    checkout_alias = checkout / ".booley_project"
    checkout_alias.symlink_to(data, target_is_directory=True)

    monkeypatch.setattr(
        enqueue_publication,
        "resolve_checkout_project_dir",
        lambda _root: checkout_alias,
    )
    monkeypatch.setattr(
        enqueue_publication,
        "resolve_project_dir",
        lambda _root: runtime_alias,
        raising=False,
    )
    return runtime_alias, checkout_alias


DRAFT = b"draft\n"
QUEUED = b"queued\n"


def _prepare(
    project_root: Path, document: Path, *, has_unmet: bool = False
) -> enqueue_publication.EnqueueJournal:
    document.write_bytes(DRAFT)
    operation_id = "0" * 32
    machine = ticket_machine_fields(
        TicketBaseline((_participant(),)), fields={}, body="", generation=operation_id
    )
    return enqueue_publication.prepare_enqueue(
        project_root,
        "ticket",
        document,
        QUEUED,
        has_unmet=has_unmet,
        created="now",
        machine=machine,
    )


def _single_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return (project root, tickets dir) with one unaliased Project directory."""
    project = tmp_path / "work"
    project_dir = project / ".booley_project"
    (project_dir / "tickets" / "board").mkdir(parents=True)
    monkeypatch.setattr(
        enqueue_publication, "resolve_checkout_project_dir", lambda _r: project_dir
    )
    monkeypatch.setattr(enqueue_publication, "resolve_project_dir", lambda _r: project_dir)
    return project, project_dir / "tickets"


def test_enqueue_journal_parser_and_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "journal.json"
    monkeypatch.setattr(enqueue_publication, "_journal_path", lambda *_args: journal_path)
    journal_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="invalid fields"):
        enqueue_publication.load_enqueue_journal(tmp_path, "ticket")
    journal = _enqueue_journal(tmp_path)
    (tmp_path / "tickets" / "board").mkdir(parents=True)
    monkeypatch.setattr(
        enqueue_publication,
        "_operation_directory",
        lambda *_args: tmp_path / "operation",
    )
    monkeypatch.setattr(enqueue_publication, "_transaction_project_dir", lambda _root: tmp_path)
    enqueue_publication.write_enqueue_journal(tmp_path, journal)
    assert enqueue_publication.load_enqueue_journal(tmp_path, "ticket") == journal
    for changed in (
        replace(journal, schema=1),  # the pre-ADR-0065 two-path journal
        replace(journal, operation_id="bad"),
        replace(journal, candidate="wrong"),
        replace(journal, backup="wrong"),
        replace(journal, document="wrong"),
        replace(journal, document=str(tmp_path / "tickets/board/queue/ticket.md")),
    ):
        enqueue_publication.write_enqueue_journal(tmp_path, changed)
        with pytest.raises(enqueue_publication.EnqueuePublicationError):
            enqueue_publication.load_enqueue_journal(tmp_path, "ticket")


def test_enqueue_payload_validation_rejects_each_bound_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _enqueue_journal(tmp_path)
    (tmp_path / "tickets" / "board").mkdir(parents=True)
    journal_path = tmp_path / "journal.json"
    monkeypatch.setattr(enqueue_publication, "_journal_path", lambda *_args: journal_path)
    monkeypatch.setattr(
        enqueue_publication, "_operation_directory", lambda *_args: tmp_path / "operation"
    )
    monkeypatch.setattr(enqueue_publication, "_transaction_project_dir", lambda _root: tmp_path)
    for changed in (
        replace(journal, source_sha256="bad"),
        replace(journal, machine={}),
        replace(journal, machine={**journal.machine, "generation": "f" * 32}),
    ):
        enqueue_publication.write_enqueue_journal(tmp_path, changed)
        with pytest.raises(enqueue_publication.EnqueuePublicationError):
            enqueue_publication.load_enqueue_journal(tmp_path, "ticket")


def test_enqueue_uses_one_anchor_for_normal_runtime_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_alias, _checkout_alias = _bind_aliases(tmp_path, monkeypatch)

    journal = _prepare(tmp_path / "work", runtime_alias / "tickets/board/ticket.md")

    assert all(
        Path(value).is_relative_to(runtime_alias)
        for value in (journal.document, journal.candidate, journal.backup)
    )


def test_enqueue_normalizes_relative_document_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    journal = _prepare(project, Path("work/.booley_project/tickets/board/ticket.md"))

    assert Path(journal.document) == tickets / "board" / "ticket.md"


def test_enqueue_prefers_explicit_checkout_over_unrelated_active_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "work"
    project_dir = project / ".booley_project"
    active_project_dir = tmp_path / "unrelated-project"
    for directory in (project_dir, active_project_dir):
        (directory / "tickets" / "board").mkdir(parents=True)
    monkeypatch.setattr(
        enqueue_publication,
        "resolve_checkout_project_dir",
        lambda _root: project_dir,
    )
    monkeypatch.setattr(
        enqueue_publication,
        "resolve_project_dir",
        lambda _root: active_project_dir,
    )

    journal = _prepare(project, project_dir / "tickets/board/ticket.md")

    assert all(
        Path(value).is_relative_to(project_dir)
        for value in (journal.document, journal.candidate, journal.backup)
    )


def test_enqueue_reanchors_checkout_alias_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_alias, checkout_alias = _bind_aliases(tmp_path, monkeypatch)
    real_replace = Path.replace

    def reject_cross_alias(path: Path, target: Path) -> Path:
        source_is_checkout = checkout_alias.absolute() in path.absolute().parents
        target_is_runtime = runtime_alias.absolute() in Path(target).absolute().parents
        if source_is_checkout and target_is_runtime:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", reject_cross_alias)

    journal = _prepare(tmp_path / "work", checkout_alias / "tickets/board/ticket.md")
    published = enqueue_publication.publish_enqueue(tmp_path / "work", journal)

    assert published.state == "published"
    assert Path(published.document).is_relative_to(runtime_alias)
    assert Path(published.document).read_bytes() == QUEUED


def test_load_reanchors_existing_mixed_alias_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_alias, checkout_alias = _bind_aliases(tmp_path, monkeypatch)
    journal = _prepare(tmp_path / "work", runtime_alias / "tickets/board/ticket.md")
    mixed = replace(journal, document=str(checkout_alias / "tickets/board/ticket.md"))
    enqueue_publication.write_enqueue_journal(tmp_path / "work", mixed)

    loaded = enqueue_publication.load_enqueue_journal(tmp_path / "work", "ticket")

    assert loaded == journal
    persisted = json.loads(
        (runtime_alias / ".runtime/acceptance/enqueue/ticket.json").read_text(encoding="utf-8")
    )
    assert persisted["document"] == journal.document


# Record-first publication and crash recovery (ADR 0065) ------------------------


class _CrashError(Exception):
    """Simulated process death at one publication checkpoint."""


def _crash_after(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Let enqueue_publication.<name> run once, then die before the next step."""
    real = getattr(enqueue_publication, name)

    def crashing(*args, **kwargs):
        real(*args, **kwargs)
        raise _CrashError(name)

    monkeypatch.setattr(enqueue_publication, name, crashing)


def _crash_on_document_replace(monkeypatch: pytest.MonkeyPatch, document: Path) -> None:
    real_replace = Path.replace

    def crashing(path: Path, target: Path) -> Path:
        if Path(target) == document:
            raise _CrashError("document replace")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", crashing)


@pytest.mark.parametrize(
    "has_unmet, state",
    [(False, TicketState.QUEUED), (True, TicketState.WAITING)],
)
def test_publish_writes_record_then_executable_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_unmet: bool, state: TicketState
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    document = ticket_document_path(tickets, "ticket")
    journal = _prepare(project, document, has_unmet=has_unmet)
    assert read_state_record(tickets, "ticket") is None  # prepare leaves the draft alone

    published = enqueue_publication.publish_enqueue(project, journal)
    done = enqueue_publication.finish_enqueue(project, published)

    assert done.state == "done"
    assert document.read_bytes() == QUEUED
    record = read_state_record(tickets, "ticket")
    assert record is not None and record.state is state
    assert record.runtime["last_update"] == "now"
    assert enqueue_publication.load_enqueue_journal(project, "ticket") is None


def test_prepare_refuses_a_ticket_that_already_has_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    write_state_record(tickets, "ticket", StateRecord.fresh(TicketState.BLOCKED))
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="not a draft"):
        _prepare(project, ticket_document_path(tickets, "ticket"))


@pytest.mark.parametrize(
    "crash",
    ["backup", "record", "document", "checkpoint"],
)
def test_crash_at_each_publication_step_recovers_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    document = ticket_document_path(tickets, "ticket")
    journal = _prepare(project, document)
    with monkeypatch.context() as patch:
        if crash == "backup":
            _crash_after(patch, "_preserve_source")
        elif crash == "record":
            _crash_after(patch, "write_state_record")
        elif crash == "document":
            _crash_on_document_replace(patch, document)
        else:
            _crash_after(patch, "write_enqueue_journal")
        with pytest.raises(_CrashError):
            enqueue_publication.publish_enqueue(project, journal)

    # The invariant after any crash: an executable document always has a record.
    record = read_state_record(tickets, "ticket")
    if document.read_bytes() == QUEUED:
        assert record is not None
    # Recovery rolls forward from whatever the journal on disk says.
    pending = enqueue_publication.load_enqueue_journal(project, "ticket")
    assert pending is not None
    if pending.state == "prepared":
        pending = enqueue_publication.publish_enqueue(project, pending)
    enqueue_publication.finish_enqueue(project, pending)
    assert document.read_bytes() == QUEUED
    record = read_state_record(tickets, "ticket")
    assert record is not None and record.state is TicketState.QUEUED


def test_record_beside_draft_rolls_back_when_candidate_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    document = ticket_document_path(tickets, "ticket")
    journal = _prepare(project, document)
    with monkeypatch.context() as patch:
        _crash_after(patch, "write_state_record")
        with pytest.raises(_CrashError):
            enqueue_publication.publish_enqueue(project, journal)
    assert read_state_record(tickets, "ticket") is not None
    Path(journal.candidate).unlink()

    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="left unchanged"):
        enqueue_publication.publish_enqueue(project, journal)

    assert document.read_bytes() == DRAFT
    assert read_state_record(tickets, "ticket") is None


def test_publish_refuses_a_changed_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    document = ticket_document_path(tickets, "ticket")
    journal = _prepare(project, document)
    document.write_bytes(b"edited\n")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="changed"):
        enqueue_publication.publish_enqueue(project, journal)
    assert read_state_record(tickets, "ticket") is None


def test_publish_refuses_a_record_in_another_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    document = ticket_document_path(tickets, "ticket")
    journal = _prepare(project, document)
    write_state_record(tickets, "ticket", StateRecord.fresh(TicketState.BLOCKED))
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="expected queued"):
        enqueue_publication.publish_enqueue(project, journal)
    assert document.read_bytes() == DRAFT


def test_publish_fails_closed_on_a_corrupt_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    document = ticket_document_path(tickets, "ticket")
    journal = _prepare(project, document)
    state_record_path(tickets, "ticket").parent.mkdir(parents=True, exist_ok=True)
    state_record_path(tickets, "ticket").write_text("{", encoding="utf-8")
    with pytest.raises(StateRecordError):
        enqueue_publication.publish_enqueue(project, journal)
    assert document.read_bytes() == DRAFT
    assert state_record_path(tickets, "ticket").read_text(encoding="utf-8") == "{"


def test_finish_requires_the_published_document_and_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tickets = _single_project(tmp_path, monkeypatch)
    document = ticket_document_path(tickets, "ticket")
    journal = _prepare(project, document)
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match=r"unavailable|changed"):
        enqueue_publication.finish_enqueue(project, journal)
    published = enqueue_publication.publish_enqueue(project, journal)
    delete_state_record(tickets, "ticket")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="no state record"):
        enqueue_publication.finish_enqueue(project, published)
    assert hashlib.sha256(document.read_bytes()).hexdigest() == journal.candidate_sha256
