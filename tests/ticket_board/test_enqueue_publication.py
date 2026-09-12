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
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
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
    basis = AcceptanceBasis((_participant(),)).as_dict()
    operation_id = "0" * 32
    digest = "1" * 64
    operation = tmp_path / "operation"
    return enqueue_publication.EnqueueJournal(
        1,
        operation_id,
        "ticket",
        "prepared",
        str(tmp_path / "tickets/board/drafts/ticket.md"),
        digest,
        str(tmp_path / "tickets/board/queue/ticket.md"),
        str(operation / "ticket.md"),
        "2" * 64,
        str(operation / "source.md"),
        False,
        "now",
        basis,
        {
            "operation_id": operation_id,
            "source_sha256": digest,
            "basis_id": AcceptanceBasis.from_mapping(basis).basis_id,
            "participants": basis["participants"],
        },
    )


def _bind_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    data = tmp_path / "data"
    for state in ("drafts", "queue", "waiting"):
        (data / "tickets" / "board" / state).mkdir(parents=True)
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


def _prepare(
    project_root: Path,
    source: Path,
    destination: Path,
) -> enqueue_publication.EnqueueJournal:
    content = b"draft\n"
    source.write_bytes(content)
    basis = AcceptanceBasis((_participant(),)).as_dict()
    operation_id = "0" * 32
    receipt = {
        "operation_id": operation_id,
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "basis_id": AcceptanceBasis.from_mapping(basis).basis_id,
        "participants": basis["participants"],
    }
    return enqueue_publication.prepare_enqueue(
        project_root,
        "ticket",
        source,
        destination,
        b"queued\n",
        has_unmet=False,
        created="now",
        basis=basis,
        receipt=receipt,
    )


def test_enqueue_journal_parser_and_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "journal.json"
    monkeypatch.setattr(enqueue_publication, "_journal_path", lambda *_args: journal_path)
    journal_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="invalid fields"):
        enqueue_publication.load_enqueue_journal(tmp_path, "ticket")
    journal = _enqueue_journal(tmp_path)
    monkeypatch.setattr(
        enqueue_publication,
        "_operation_directory",
        lambda *_args: tmp_path / "operation",
    )
    monkeypatch.setattr(enqueue_publication, "_transaction_project_dir", lambda _root: tmp_path)
    enqueue_publication.write_enqueue_journal(tmp_path, journal)
    assert enqueue_publication.load_enqueue_journal(tmp_path, "ticket") == journal
    for changed in (
        replace(journal, schema=2),
        replace(journal, operation_id="bad"),
        replace(journal, candidate="wrong"),
        replace(journal, backup="wrong"),
        replace(journal, source="wrong"),
        replace(journal, destination="wrong"),
    ):
        enqueue_publication.write_enqueue_journal(tmp_path, changed)
        with pytest.raises(enqueue_publication.EnqueuePublicationError):
            enqueue_publication.load_enqueue_journal(tmp_path, "ticket")


def test_enqueue_payload_validation_rejects_each_bound_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _enqueue_journal(tmp_path)
    journal_path = tmp_path / "journal.json"
    monkeypatch.setattr(enqueue_publication, "_journal_path", lambda *_args: journal_path)
    monkeypatch.setattr(
        enqueue_publication, "_operation_directory", lambda *_args: tmp_path / "operation"
    )
    monkeypatch.setattr(enqueue_publication, "_transaction_project_dir", lambda _root: tmp_path)
    for changed in (
        replace(journal, source_sha256="bad"),
        replace(journal, basis={}),
        replace(journal, receipt={**journal.receipt, "operation_id": "f" * 32}),
        replace(journal, receipt={**journal.receipt, "source_sha256": "f" * 64}),
        replace(journal, receipt={**journal.receipt, "basis_id": "f" * 64}),
        replace(journal, receipt={**journal.receipt, "participants": []}),
    ):
        enqueue_publication.write_enqueue_journal(tmp_path, changed)
        with pytest.raises(enqueue_publication.EnqueuePublicationError):
            enqueue_publication.load_enqueue_journal(tmp_path, "ticket")


def test_enqueue_uses_one_anchor_for_normal_runtime_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_alias, _checkout_alias = _bind_aliases(tmp_path, monkeypatch)
    source = runtime_alias / "tickets/board/drafts/ticket.md"
    destination = runtime_alias / "tickets/board/queue/ticket.md"

    journal = _prepare(tmp_path / "work", source, destination)

    assert all(
        Path(value).is_relative_to(runtime_alias)
        for value in (
            journal.source,
            journal.destination,
            journal.candidate,
            journal.backup,
        )
    )


def test_enqueue_normalizes_relative_paths_before_destination_directory_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "work"
    project_dir = project / ".booley_project"
    (project_dir / "tickets/board/drafts").mkdir(parents=True)
    monkeypatch.setattr(
        enqueue_publication,
        "resolve_checkout_project_dir",
        lambda _root: project_dir,
    )
    monkeypatch.setattr(enqueue_publication, "resolve_project_dir", lambda _root: project_dir)
    monkeypatch.chdir(tmp_path)
    source = Path("work/.booley_project/tickets/board/drafts/ticket.md")
    destination = Path("work/.booley_project/tickets/board/queue/ticket.md")

    journal = _prepare(project, source, destination)

    assert Path(journal.source) == project_dir / "tickets/board/drafts/ticket.md"
    assert Path(journal.destination) == project_dir / "tickets/board/queue/ticket.md"
    assert not destination.parent.exists()


def test_enqueue_prefers_explicit_checkout_over_unrelated_active_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "work"
    project_dir = project / ".booley_project"
    active_project_dir = tmp_path / "unrelated-project"
    for directory in (project_dir, active_project_dir):
        for state in ("drafts", "queue", "waiting"):
            (directory / "tickets" / "board" / state).mkdir(parents=True)
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
    source = project_dir / "tickets/board/drafts/ticket.md"
    destination = project_dir / "tickets/board/queue/ticket.md"

    journal = _prepare(project, source, destination)

    assert all(
        Path(value).is_relative_to(project_dir)
        for value in (
            journal.source,
            journal.destination,
            journal.candidate,
            journal.backup,
        )
    )


def test_enqueue_reanchors_checkout_alias_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_alias, checkout_alias = _bind_aliases(tmp_path, monkeypatch)
    source = checkout_alias / "tickets/board/drafts/ticket.md"
    destination = checkout_alias / "tickets/board/queue/ticket.md"
    real_replace = Path.replace

    def reject_cross_alias(path: Path, target: Path) -> Path:
        source_is_checkout = checkout_alias.absolute() in path.absolute().parents
        target_is_runtime = runtime_alias.absolute() in Path(target).absolute().parents
        if source_is_checkout and target_is_runtime:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", reject_cross_alias)

    journal = _prepare(tmp_path / "work", source, destination)
    published = enqueue_publication.publish_enqueue(tmp_path / "work", journal)

    assert published.state == "published"
    assert Path(published.source).is_relative_to(runtime_alias)
    assert Path(published.destination).is_relative_to(runtime_alias)


def test_load_reanchors_existing_mixed_alias_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_alias, checkout_alias = _bind_aliases(tmp_path, monkeypatch)
    source = runtime_alias / "tickets/board/drafts/ticket.md"
    destination = runtime_alias / "tickets/board/queue/ticket.md"
    journal = _prepare(tmp_path / "work", source, destination)
    mixed = replace(
        journal,
        source=str(checkout_alias / "tickets/board/drafts/ticket.md"),
        destination=str(checkout_alias / "tickets/board/queue/ticket.md"),
    )
    enqueue_publication.write_enqueue_journal(tmp_path / "work", mixed)

    loaded = enqueue_publication.load_enqueue_journal(tmp_path / "work", "ticket")

    assert loaded == journal
    persisted = json.loads(
        (runtime_alias / ".runtime/acceptance/enqueue/ticket.json").read_text(encoding="utf-8")
    )
    assert persisted["source"] == journal.source
    assert persisted["destination"] == journal.destination


def test_enqueue_cutover_helpers_are_idempotent_and_fail_closed(tmp_path: Path) -> None:
    journal = _enqueue_journal(tmp_path)
    source = Path(journal.source)
    backup = Path(journal.backup)
    destination = Path(journal.destination)
    candidate = Path(journal.candidate)
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"source")
    journal = replace(journal, source_sha256=hashlib.sha256(b"source").hexdigest())
    enqueue_publication._preserve_source(source, backup, destination, journal)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="both exist"):
        enqueue_publication._preserve_source(source, backup, destination, journal)
    source.unlink()
    backup.unlink()
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="disappeared"):
        enqueue_publication._preserve_source(source, backup, destination, journal)

    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"candidate")
    expected = hashlib.sha256(b"candidate").hexdigest()
    enqueue_publication._publish_candidate(candidate, destination, expected)
    enqueue_publication._publish_candidate(candidate, destination, expected)
    destination.write_bytes(b"changed")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="changed unexpectedly"):
        enqueue_publication._require_digest(destination, expected, "queued Ticket")
    with pytest.raises(enqueue_publication.EnqueuePublicationError, match="unavailable"):
        enqueue_publication._require_digest(tmp_path / "missing", expected, "missing")
