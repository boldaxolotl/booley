"""Recoverable prepare-first publication for one Ticket enqueue operation.

Enqueue turns a draft into an executable Ticket in two writes at one fixed
document path (ADR 0065): the Ticket's state record is created first, then the
executable document replaces the draft. A crash in between leaves a state
record next to a draft-form document; the journal rolls that forward, or back
when the prepared candidate is gone, so the Board never keeps an
executable-form document without a record.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool,
    require_dict,
    require_int,
    require_str,
)
from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir

from .board_layout import (
    StateRecord,
    delete_state_record,
    is_board_document,
    read_state_record,
    ticket_document_path,
    write_state_record,
)
from .lifecycle import TicketState
from .persistence import atomic_replace_bytes

_OPERATION_RE = re.compile(r"[0-9a-f]{32}")
_STATES = {"prepared", "published", "transitioned", "done"}
# Schema 2: one fixed document path plus a state record (ADR 0065).
_SCHEMA = 2


class EnqueuePublicationError(RuntimeError):
    """An enqueue transaction cannot be recovered without manual inspection."""


@dataclass(frozen=True)
class EnqueueJournal:
    """Every identity needed to reconcile a partially published enqueue."""

    schema: int
    operation_id: str
    slug: str
    state: Literal["prepared", "published", "transitioned", "done"]
    document: str
    source_sha256: str
    candidate: str
    candidate_sha256: str
    backup: str
    has_unmet: bool
    created: str
    machine: dict[str, Any]

    def with_state(
        self, state: Literal["prepared", "published", "transitioned", "done"]
    ) -> EnqueueJournal:
        return replace(self, state=state)

    @property
    def queue_state(self) -> TicketState:
        """The state the enqueued Ticket enters: waiting on unmet dependencies, else queued."""
        return TicketState.WAITING if self.has_unmet else TicketState.QUEUED


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _journal_path(project_root: Path, slug: str) -> Path:
    return _transaction_runtime_dir(project_root) / "acceptance" / "enqueue" / f"{slug}.json"


def _operation_directory(project_root: Path, operation_id: str) -> Path:
    return _transaction_runtime_dir(project_root) / "acceptance" / "enqueue" / operation_id


def _transaction_project_dir(project_root: Path) -> Path:
    """Choose one Project alias for every path in an enqueue transaction."""
    checkout = resolve_checkout_project_dir(project_root)
    active = resolve_project_dir(project_root)
    if checkout == active:
        return active
    try:
        if checkout.samefile(active):
            return active
    except OSError:
        pass
    return checkout


def _transaction_runtime_dir(project_root: Path) -> Path:
    directory = _transaction_project_dir(project_root) / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def enqueue_pending(project_root: Path, slug: str) -> bool:
    """Return whether *slug* has an enqueue journal that has not been retired.

    Read-only: unlike the journal writers it creates no runtime directory.
    """
    path = _transaction_project_dir(project_root) / ".runtime" / "acceptance" / "enqueue"
    return (path / f"{slug}.json").exists()


def write_enqueue_journal(project_root: Path, journal: EnqueueJournal) -> None:
    """Atomically checkpoint an enqueue transaction."""
    payload = (json.dumps(asdict(journal), indent=2, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(_journal_path(project_root, journal.slug), payload)


def load_enqueue_journal(project_root: Path, slug: str) -> EnqueueJournal | None:
    """Load and validate a pending or completed enqueue transaction."""
    path = _journal_path(project_root, slug)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        journal = _parse_enqueue_journal(value)
    except (BoundaryError, OSError, json.JSONDecodeError) as exc:
        raise EnqueuePublicationError(f"enqueue journal is unreadable: {path}: {exc}") from exc
    _validate_journal_identity(slug, journal)
    canonical = _canonicalize_document_path(project_root, slug, Path(journal.document))
    normalized = replace(journal, document=str(canonical))
    _validate_journal(project_root, slug, normalized)
    if normalized != journal:
        write_enqueue_journal(project_root, normalized)
    return normalized


def _parse_enqueue_journal(value: Any) -> EnqueueJournal:
    mapping = require_dict(value, field="enqueue journal")
    if set(mapping) != set(EnqueueJournal.__dataclass_fields__):
        raise BoundaryError("enqueue journal has invalid fields")
    state = require_str(mapping, "state")
    return EnqueueJournal(
        schema=require_int(mapping.get("schema"), field="enqueue journal schema"),
        operation_id=require_str(mapping, "operation_id"),
        slug=require_str(mapping, "slug"),
        state=cast(Literal["prepared", "published", "transitioned", "done"], state),
        document=require_str(mapping, "document"),
        source_sha256=require_str(mapping, "source_sha256"),
        candidate=require_str(mapping, "candidate"),
        candidate_sha256=require_str(mapping, "candidate_sha256"),
        backup=require_str(mapping, "backup"),
        has_unmet=require_bool(mapping, "has_unmet"),
        created=require_str(mapping, "created"),
        machine=require_dict(mapping.get("machine"), field="enqueue journal machine"),
    )


def _validate_journal(project_root: Path, slug: str, journal: EnqueueJournal) -> None:
    _validate_journal_identity(slug, journal)
    operation = _operation_directory(project_root, journal.operation_id)
    if Path(journal.candidate) != operation / "ticket.md":
        raise EnqueuePublicationError("enqueue journal candidate path is invalid")
    if Path(journal.backup) != operation / "source.md":
        raise EnqueuePublicationError("enqueue journal backup path is invalid")
    if Path(journal.document) != ticket_document_path(
        _transaction_tickets_dir(project_root), slug
    ):
        raise EnqueuePublicationError("enqueue journal document path is invalid")
    _validate_journal_payload(journal)


def _validate_journal_identity(slug: str, journal: EnqueueJournal) -> None:
    if journal.schema != _SCHEMA or journal.slug != slug or journal.state not in _STATES:
        raise EnqueuePublicationError("enqueue journal identity or schema is invalid")
    if not _OPERATION_RE.fullmatch(journal.operation_id):
        raise EnqueuePublicationError("enqueue journal operation ID is invalid")


def _transaction_tickets_dir(project_root: Path) -> Path:
    return _transaction_project_dir(project_root) / "tickets"


def _canonicalize_document_path(project_root: Path, slug: str, path: Path) -> Path:
    """Map any alias of the board document of *slug* onto this transaction's spelling."""
    tickets = _transaction_tickets_dir(project_root)
    try:
        on_board = is_board_document(tickets, path)
    except OSError as exc:
        raise EnqueuePublicationError("enqueue journal Ticket Board is unavailable") from exc
    if not on_board or path.name != f"{slug}.md":
        raise EnqueuePublicationError("enqueue journal document path is invalid")
    return ticket_document_path(tickets, slug)


def _validate_journal_payload(journal: EnqueueJournal) -> None:
    digests = (journal.source_sha256, journal.candidate_sha256)
    if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in digests):
        raise EnqueuePublicationError("enqueue journal content identity is invalid")
    if journal.machine.get("generation") != journal.operation_id:
        raise EnqueuePublicationError("enqueue journal Ticket generation changed")


def prepare_enqueue(
    project_root: Path,
    slug: str,
    source: Path,
    candidate_content: bytes,
    *,
    has_unmet: bool,
    created: str,
    machine: dict[str, Any],
) -> EnqueueJournal:
    """Persist a complete candidate and recovery journal without touching the draft."""
    existing = load_enqueue_journal(project_root, slug)
    if existing is not None:
        return existing
    operation_id = machine.get("generation")
    if not isinstance(operation_id, str) or not _OPERATION_RE.fullmatch(operation_id):
        raise EnqueuePublicationError("Ticket generation is invalid")
    document = _canonicalize_document_path(project_root, slug, source)
    if read_state_record(_transaction_tickets_dir(project_root), slug) is not None:
        raise EnqueuePublicationError(f"Ticket {slug!r} is not a draft")
    operation = _operation_directory(project_root, operation_id)
    candidate = operation / "ticket.md"
    backup = operation / "source.md"
    journal = EnqueueJournal(
        _SCHEMA,
        operation_id,
        slug,
        "prepared",
        str(document),
        _digest(document.read_bytes()),
        str(candidate),
        _digest(candidate_content),
        str(backup),
        has_unmet,
        created,
        machine,
    )
    _validate_journal(project_root, slug, journal)
    atomic_replace_bytes(candidate, candidate_content, mode=0o644)
    write_enqueue_journal(project_root, journal)
    return journal


def publish_enqueue(project_root: Path, journal: EnqueueJournal) -> EnqueueJournal:
    """Create the state record, then publish the executable document; checkpoint.

    Caller holds the per-Ticket lock. Each step is idempotent so a crash at any
    point resumes here from the same journal.
    """
    tickets = _transaction_tickets_dir(project_root)
    document = Path(journal.document)
    candidate = Path(journal.candidate)
    published = _document_is(document, journal.candidate_sha256)
    if not published:
        _require_digest(document, journal.source_sha256, "enqueue source draft")
        _preserve_source(document, Path(journal.backup), journal)
        if not _document_is(candidate, journal.candidate_sha256):
            # The executable form is gone, so the draft cannot become executable:
            # roll the record back so the draft stays a consistent draft.
            _rollback_record(tickets, journal)
            raise EnqueuePublicationError(
                f"enqueue candidate is unavailable or changed: {candidate}; "
                "the draft was left unchanged"
            )
    _publish_record(tickets, journal)
    if not published:
        candidate.replace(document)
    checkpoint = journal.with_state("published")
    write_enqueue_journal(project_root, checkpoint)
    return checkpoint


def _document_is(path: Path, expected: str) -> bool:
    try:
        return _digest(path.read_bytes()) == expected
    except FileNotFoundError:
        return False


def _preserve_source(document: Path, backup: Path, journal: EnqueueJournal) -> None:
    """Keep a verified copy of the draft until the enqueue finishes."""
    if backup.exists():
        _require_digest(backup, journal.source_sha256, "enqueue source backup")
        return
    atomic_replace_bytes(backup, document.read_bytes(), mode=0o644)


def _publish_record(tickets: Path, journal: EnqueueJournal) -> None:
    record = read_state_record(tickets, journal.slug)
    expected = journal.queue_state
    if record is None:
        write_state_record(
            tickets, journal.slug, StateRecord.fresh(expected, last_update=journal.created)
        )
    elif record.state is not expected:
        raise EnqueuePublicationError(
            f"enqueue found Ticket {journal.slug!r} {record.state.status}, expected "
            f"{expected.status}"
        )


def _rollback_record(tickets: Path, journal: EnqueueJournal) -> None:
    record = read_state_record(tickets, journal.slug)
    if record is not None and record.state is journal.queue_state:
        delete_state_record(tickets, journal.slug)


def _require_digest(path: Path, expected: str, label: str) -> None:
    try:
        actual = _digest(path.read_bytes())
    except OSError as exc:
        raise EnqueuePublicationError(f"{label} is unavailable: {path}") from exc
    if actual != expected:
        raise EnqueuePublicationError(f"{label} changed unexpectedly: {path}")


def finish_enqueue(project_root: Path, journal: EnqueueJournal) -> EnqueueJournal:
    """Mark publication complete and retire its recoverable source backup."""
    _require_digest(Path(journal.document), journal.candidate_sha256, "queued Ticket")
    record = read_state_record(_transaction_tickets_dir(project_root), journal.slug)
    if record is None:
        raise EnqueuePublicationError(f"queued Ticket {journal.slug!r} has no state record")
    done = journal.with_state("done")
    write_enqueue_journal(project_root, done)
    from .basis_publication import finish_basis_publication

    finish_basis_publication(project_root, done.slug, done.operation_id)
    Path(done.backup).unlink(missing_ok=True)
    Path(done.candidate).unlink(missing_ok=True)
    _journal_path(project_root, done.slug).unlink(missing_ok=True)
    with suppress(OSError):
        _operation_directory(project_root, done.operation_id).rmdir()
    return done
