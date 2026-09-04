"""Recoverable prepare-first publication for one Ticket enqueue operation."""

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
from booley.runtime.project_dir import resolve_checkout_project_dir, runtime_dir

from .acceptance_basis import AcceptanceBasis, AcceptanceBasisError
from .persistence import atomic_replace_bytes

_OPERATION_RE = re.compile(r"[0-9a-f]{32}")
_STATES = {"prepared", "published", "transitioned", "done"}


class EnqueuePublicationError(RuntimeError):
    """An enqueue transaction cannot be recovered without manual inspection."""


@dataclass(frozen=True)
class EnqueueJournal:
    """Every identity needed to reconcile a partially published enqueue."""

    schema: int
    operation_id: str
    slug: str
    state: Literal["prepared", "published", "transitioned", "done"]
    source: str
    source_sha256: str
    destination: str
    candidate: str
    candidate_sha256: str
    backup: str
    has_unmet: bool
    created: str
    basis: dict[str, Any]
    receipt: dict[str, Any]

    def with_state(
        self, state: Literal["prepared", "published", "transitioned", "done"]
    ) -> EnqueueJournal:
        return replace(self, state=state)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _journal_path(project_root: Path, slug: str) -> Path:
    return runtime_dir(project_root) / "acceptance" / "enqueue" / f"{slug}.json"


def _operation_directory(project_root: Path, operation_id: str) -> Path:
    return runtime_dir(project_root) / "acceptance" / "enqueue" / operation_id


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
    _validate_journal(project_root, slug, journal)
    return journal


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
        source=require_str(mapping, "source"),
        source_sha256=require_str(mapping, "source_sha256"),
        destination=require_str(mapping, "destination"),
        candidate=require_str(mapping, "candidate"),
        candidate_sha256=require_str(mapping, "candidate_sha256"),
        backup=require_str(mapping, "backup"),
        has_unmet=require_bool(mapping, "has_unmet"),
        created=require_str(mapping, "created"),
        basis=require_dict(mapping.get("basis"), field="enqueue journal basis"),
        receipt=require_dict(mapping.get("receipt"), field="enqueue journal receipt"),
    )


def _validate_journal(project_root: Path, slug: str, journal: EnqueueJournal) -> None:
    if journal.schema != 1 or journal.slug != slug or journal.state not in _STATES:
        raise EnqueuePublicationError("enqueue journal identity or schema is invalid")
    if not _OPERATION_RE.fullmatch(journal.operation_id):
        raise EnqueuePublicationError("enqueue journal operation ID is invalid")
    operation = _operation_directory(project_root, journal.operation_id)
    if Path(journal.candidate) != operation / "ticket.md":
        raise EnqueuePublicationError("enqueue journal candidate path is invalid")
    if Path(journal.backup) != operation / "source.md":
        raise EnqueuePublicationError("enqueue journal backup path is invalid")
    _validate_board_paths(project_root, slug, journal)
    _validate_journal_payload(journal)


def _validate_board_paths(project_root: Path, slug: str, journal: EnqueueJournal) -> None:
    board = resolve_checkout_project_dir(project_root) / "tickets" / "board"
    source = Path(journal.source).resolve()
    destination = Path(journal.destination).resolve()
    if source != (board / "drafts" / f"{slug}.md").resolve():
        raise EnqueuePublicationError("enqueue journal source path is invalid")
    destinations = {(board / state / f"{slug}.md").resolve() for state in ("queue", "waiting")}
    if destination not in destinations:
        raise EnqueuePublicationError("enqueue journal destination path is invalid")


def _validate_journal_payload(journal: EnqueueJournal) -> None:
    digests = (journal.source_sha256, journal.candidate_sha256)
    if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in digests):
        raise EnqueuePublicationError("enqueue journal content identity is invalid")
    try:
        basis = AcceptanceBasis.from_mapping(journal.basis)
    except AcceptanceBasisError as exc:
        raise EnqueuePublicationError(str(exc)) from exc
    basis_id = basis.basis_id
    if journal.receipt.get("operation_id") != journal.operation_id:
        raise EnqueuePublicationError("enqueue journal Acceptance Basis receipt is invalid")
    if journal.receipt.get("source_sha256") != journal.source_sha256:
        raise EnqueuePublicationError("enqueue journal source fingerprint changed")
    if journal.receipt.get("basis_id") != basis_id:
        raise EnqueuePublicationError("enqueue journal Acceptance Basis identity changed")
    if journal.receipt.get("participants") != journal.basis.get("participants"):
        raise EnqueuePublicationError("enqueue journal participant receipt changed")


def prepare_enqueue(
    project_root: Path,
    slug: str,
    source: Path,
    destination: Path,
    candidate_content: bytes,
    *,
    has_unmet: bool,
    created: str,
    basis: dict[str, Any],
    receipt: dict[str, Any],
) -> EnqueueJournal:
    """Persist a complete candidate and recovery journal without touching the draft."""
    existing = load_enqueue_journal(project_root, slug)
    if existing is not None:
        return existing
    operation_id = receipt.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_RE.fullmatch(operation_id):
        raise EnqueuePublicationError("Acceptance Basis receipt operation ID is invalid")
    operation = _operation_directory(project_root, operation_id)
    candidate = operation / "ticket.md"
    backup = operation / "source.md"
    atomic_replace_bytes(candidate, candidate_content, mode=0o644)
    journal = EnqueueJournal(
        1,
        operation_id,
        slug,
        "prepared",
        str(source),
        _digest(source.read_bytes()),
        str(destination),
        str(candidate),
        _digest(candidate_content),
        str(backup),
        has_unmet,
        created,
        basis,
        receipt,
    )
    _validate_journal(project_root, slug, journal)
    write_enqueue_journal(project_root, journal)
    return journal


def publish_enqueue(project_root: Path, journal: EnqueueJournal) -> EnqueueJournal:
    """Reconcile the Board cutover and checkpoint its published state."""
    source = Path(journal.source)
    destination = Path(journal.destination)
    candidate = Path(journal.candidate)
    backup = Path(journal.backup)
    _preserve_source(source, backup, destination, journal)
    _publish_candidate(candidate, destination, journal.candidate_sha256)
    published = journal.with_state("published")
    write_enqueue_journal(project_root, published)
    return published


def _preserve_source(
    source: Path, backup: Path, destination: Path, journal: EnqueueJournal
) -> None:
    if backup.exists():
        _require_digest(backup, journal.source_sha256, "enqueue source backup")
        if source.exists():
            raise EnqueuePublicationError("enqueue source and backup both exist")
        return
    if not source.exists():
        if destination.exists():
            _require_digest(destination, journal.candidate_sha256, "queued Ticket")
            return
        raise EnqueuePublicationError("enqueue source disappeared before Board cutover")
    _require_digest(source, journal.source_sha256, "enqueue source draft")
    backup.parent.mkdir(parents=True, exist_ok=True)
    source.replace(backup)


def _publish_candidate(candidate: Path, destination: Path, expected: str) -> None:
    if destination.exists():
        _require_digest(destination, expected, "queued Ticket")
        return
    _require_digest(candidate, expected, "enqueue candidate")
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate.replace(destination)


def _require_digest(path: Path, expected: str, label: str) -> None:
    try:
        actual = _digest(path.read_bytes())
    except OSError as exc:
        raise EnqueuePublicationError(f"{label} is unavailable: {path}") from exc
    if actual != expected:
        raise EnqueuePublicationError(f"{label} changed unexpectedly: {path}")


def finish_enqueue(project_root: Path, journal: EnqueueJournal) -> EnqueueJournal:
    """Mark publication complete and retire its recoverable source backup."""
    destination = Path(journal.destination)
    _require_digest(destination, journal.candidate_sha256, "queued Ticket")
    done = journal.with_state("done")
    write_enqueue_journal(project_root, done)
    Path(done.backup).unlink(missing_ok=True)
    Path(done.candidate).unlink(missing_ok=True)
    _journal_path(project_root, done.slug).unlink(missing_ok=True)
    with suppress(OSError):
        _operation_directory(project_root, done.operation_id).rmdir()
    return done
