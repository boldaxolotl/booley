"""Recoverable blocked-to-draft transition for Tickets with a recorded baseline.

The Ticket document keeps its path (ADR 0065). The cutover first replaces the
blocked executable document with its draft form, then deletes the state record,
so a crash can leave at most a draft-form document next to a blocked record,
which the journal rolls forward.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool,
    require_dict,
    require_int,
    require_str,
    require_str_value,
)
from booley.runtime.project_dir import (
    resolve_checkout_project_dir,
    runtime_dir,
)
from booley.runtime.worktree_paths import relative_worktree_paths, ticket_workspace_path
from booley.runtime.worktree_relocation import (
    WorktreeMove,
    WorktreeRelocationError,
    preflight_worktree_moves,
    relocate_worktree,
)
from booley.ticket_board.ticket_repositories import (
    resolve_inner_project_repo,
    ticket_project_worktree,
)

from . import waiver_candidates
from .board_layout import (
    StateRecord,
    delete_state_record,
    read_state_record,
    ticket_document_path,
)
from .lifecycle import TicketState
from .persistence import atomic_replace_bytes
from .ticket_baseline import (
    AUTHORED_DRIFT_REASON,
    TicketBaseline,
    TicketBaselineError,
    authored_drift_reason,
    load_ticket_baseline_from_document,
    load_ticket_recovery_baseline_from_document,
    ticket_baseline_from_machine,
)
from .ticket_document import (
    TicketDocument,
    convert_ticket_document,
    serialize_ticket_document,
    ticket_conversion_context,
)
from .workspace_ops import (
    AuthoringWorkspace,
    TicketBaselineOperationError,
    _generation_branch,
    _generation_file,
    open_authoring_generation,
    preflight_authoring_destination,
    validate_basis_refs,
)

_OPERATION_RE = re.compile(r"[0-9a-f]{32}")
_STATES = {"initializing", "prepared", "cutover-ready", "published"}


class DraftTransitionError(RuntimeError):
    """A return-to-draft transaction needs recovery or manual inspection."""


@dataclass(frozen=True)
class DraftTransitionJournal:
    """Identity record for one recoverable blocked-to-draft transition."""

    schema: int
    operation_id: str
    slug: str
    state: Literal["initializing", "prepared", "cutover-ready", "published"]
    machine: dict[str, Any]
    blocked_ticket: str
    blocked_sha256: str
    draft_ticket: str
    draft_sha256: str
    generation: str
    generation_sha256: str
    archive_dir: str
    has_project: bool
    authored_drift: dict[str, str] = field(default_factory=dict)
    # Schema 3: the blocked record's execution identity. The document no longer
    # changes with state (ADR 0065), so the record is the compare-and-swap target.
    blocked_execution_id: str | None = None

    def with_state(
        self,
        state: Literal["initializing", "prepared", "cutover-ready", "published"],
        *,
        has_project: bool | None = None,
    ) -> DraftTransitionJournal:
        return replace(
            self,
            state=state,
            has_project=self.has_project if has_project is None else has_project,
        )


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _tickets_dir(root: Path) -> Path:
    return resolve_checkout_project_dir(root) / "tickets"


def _transition_root(root: Path) -> Path:
    return runtime_dir(root) / "acceptance" / "return-to-draft"


def _journal_path(root: Path, slug: str) -> Path:
    return _transition_root(root) / f"{slug}.json"


def _operation_dir(root: Path, operation_id: str) -> Path:
    return _transition_root(root) / operation_id


def _write_journal(root: Path, journal: DraftTransitionJournal) -> None:
    payload = (json.dumps(asdict(journal), indent=2, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(_journal_path(root, journal.slug), payload)


def _load_journal(root: Path, logs_dir: Path, slug: str) -> DraftTransitionJournal | None:
    path = _journal_path(root, slug)
    if not path.exists():
        return None
    try:
        journal = _parse_journal(json.loads(path.read_text(encoding="utf-8")))
    except (BoundaryError, OSError, json.JSONDecodeError) as exc:
        raise DraftTransitionError(f"return-to-draft journal is unreadable: {path}") from exc
    _validate_journal(root, logs_dir, slug, journal)
    return journal


def _parse_journal(value: Any) -> DraftTransitionJournal:
    mapping = require_dict(value, field="return-to-draft journal")
    schema = require_int(mapping.get("schema"), field="return-to-draft journal schema")
    expected = set(DraftTransitionJournal.__dataclass_fields__)
    if schema < 3:
        expected.remove("blocked_execution_id")
    if schema == 1:
        expected.remove("authored_drift")
    if set(mapping) != expected:
        raise BoundaryError("return-to-draft journal has invalid fields")
    state = require_str(mapping, "state")
    return DraftTransitionJournal(
        schema=schema,
        operation_id=require_str(mapping, "operation_id"),
        slug=require_str(mapping, "slug"),
        state=cast(Literal["initializing", "prepared", "cutover-ready", "published"], state),
        machine=require_dict(mapping.get("machine"), field="return-to-draft journal machine"),
        blocked_ticket=require_str(mapping, "blocked_ticket"),
        blocked_sha256=require_str(mapping, "blocked_sha256"),
        draft_ticket=require_str(mapping, "draft_ticket"),
        draft_sha256=require_str(mapping, "draft_sha256"),
        generation=require_str(mapping, "generation"),
        generation_sha256=require_str(mapping, "generation_sha256"),
        archive_dir=require_str(mapping, "archive_dir"),
        has_project=require_bool(mapping, "has_project"),
        authored_drift=(
            require_dict(mapping.get("authored_drift"), field="return-to-draft authored drift")
            if schema >= 2
            else {}
        ),
        blocked_execution_id=(
            require_str_value(
                mapping.get("blocked_execution_id"),
                field="return-to-draft blocked execution",
                allow_empty=True,
            )
            if schema >= 3
            else None
        ),
    )


def transition_pending(project_root: Path | str, slug: str) -> bool:
    """Return whether a recoverable return-to-draft journal exists."""
    return _journal_path(Path(project_root).resolve(), slug).exists()


def _validate_journal(
    root: Path, logs_dir: Path, slug: str, journal: DraftTransitionJournal
) -> None:
    if journal.schema not in {1, 2, 3} or journal.slug != slug or journal.state not in _STATES:
        raise DraftTransitionError("return-to-draft journal identity or schema is invalid")
    _validate_authored_drift(journal)
    if not _OPERATION_RE.fullmatch(journal.operation_id):
        raise DraftTransitionError("return-to-draft journal operation ID is invalid")
    try:
        ticket_baseline_from_machine(journal.machine)
    except TicketBaselineError as exc:
        raise DraftTransitionError(str(exc)) from exc
    document = ticket_document_path(_tickets_dir(root), slug).resolve()
    if Path(journal.draft_ticket).resolve() != document:
        raise DraftTransitionError("return-to-draft destination path is invalid")
    if not re.fullmatch(r"[0-9a-f]{16}", journal.generation):
        raise DraftTransitionError("return-to-draft generation token is invalid")
    digests = (
        journal.blocked_sha256,
        journal.draft_sha256,
        journal.generation_sha256,
    )
    if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in digests):
        raise DraftTransitionError("return-to-draft content identity is invalid")
    if Path(journal.blocked_ticket).resolve() != document:
        raise DraftTransitionError("return-to-draft blocked Ticket path is invalid")
    archive = Path(journal.archive_dir).resolve()
    archive_root = (logs_dir / slug / "runs").resolve()
    operation = _operation_dir(root, journal.operation_id).resolve()
    if operation.parent != _transition_root(root).resolve():
        raise DraftTransitionError("return-to-draft operation path is invalid")
    if archive.parent != archive_root or re.fullmatch(r"[0-9]{3}", archive.name) is None:
        raise DraftTransitionError("return-to-draft publication metadata is invalid")


def _validate_authored_drift(journal: DraftTransitionJournal) -> None:
    if journal.schema == 1:
        if journal.authored_drift:
            raise DraftTransitionError("return-to-draft authored drift is invalid")
        return
    expected_fields = {
        "expected_authored_sha256",
        "observed_authored_sha256",
        "reason",
    }
    if not journal.authored_drift:
        return
    try:
        expected_digest = require_str(journal.authored_drift, "expected_authored_sha256")
        observed_digest = require_str(journal.authored_drift, "observed_authored_sha256")
        reason = require_str(journal.authored_drift, "reason")
    except BoundaryError as exc:
        raise DraftTransitionError("return-to-draft authored drift is invalid") from exc
    digests_valid = all(
        re.fullmatch(r"[0-9a-f]{64}", value) for value in (expected_digest, observed_digest)
    )
    if (
        set(journal.authored_drift) != expected_fields
        or reason != AUTHORED_DRIFT_REASON
        or expected_digest != journal.machine.get("authored_sha256")
        or observed_digest == expected_digest
        or not digests_valid
    ):
        raise DraftTransitionError("return-to-draft authored drift is invalid")


def _next_archive(log_dir: Path) -> Path:
    runs = log_dir / "runs"
    existing = [int(path.name) for path in runs.glob("[0-9][0-9][0-9]") if path.is_dir()]
    return runs / f"{(max(existing, default=0) + 1):03d}"


def _converted_document(root: Path, ticket: Path, slug: str, stage: str) -> TicketDocument:
    with ticket_conversion_context(root, slug, stage) as context:
        converted = convert_ticket_document(ticket.read_text(encoding="utf-8"), context)
    if converted.document is None:
        details = "; ".join(item.message for item in converted.diagnostics)
        raise DraftTransitionError(f"Ticket is invalid: {details}")
    return converted.document


def _draft_content(root: Path, ticket: Path, slug: str) -> tuple[TicketDocument, bytes]:
    document = _converted_document(root, ticket, slug, "executable")
    draft = TicketDocument(document.spec, {})
    with ticket_conversion_context(root, slug, "draft") as context:
        content = serialize_ticket_document(draft, context).encode()
    return document, content


def _load_transition_basis(
    root: Path, slug: str, document: TicketDocument, drift_reason: str | None
) -> TicketBaseline:
    try:
        if drift_reason is not None:
            return load_ticket_recovery_baseline_from_document(root, slug, document)
        return load_ticket_baseline_from_document(root, slug, document)
    except TicketBaselineError as exc:
        raise DraftTransitionError(str(exc)) from exc


def _authored_drift_record(document: TicketDocument, drift_reason: str | None) -> dict[str, str]:
    if drift_reason is None:
        return {}
    return {
        "expected_authored_sha256": document.generated["machine"]["authored_sha256"],
        "observed_authored_sha256": document.spec.semantic_digest(),
        "reason": drift_reason,
    }


def _new_journal(
    root: Path,
    ticket: Path,
    slug: str,
    status: str,
    logs_dir: Path,
) -> DraftTransitionJournal:
    if status != "blocked":
        raise DraftTransitionError(f"return-to-draft requires a blocked ticket, got {status!r}")
    document, draft_content = _draft_content(root, ticket, slug)
    fields = dict(document.spec.fields)
    try:
        preflight_authoring_destination(root, fields)
    except TicketBaselineOperationError as exc:
        raise DraftTransitionError(str(exc)) from exc
    drift_reason = authored_drift_reason(document)
    basis = _load_transition_basis(root, slug, document, drift_reason)
    operation_id = uuid.uuid4().hex
    operation = _operation_dir(root, operation_id)
    draft_path = operation / "draft.md"
    generation = secrets.token_hex(8)
    generation_content = (json.dumps({"generation": generation}, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(draft_path, draft_content, mode=0o644)
    atomic_replace_bytes(operation / "generation.json", generation_content)
    draft_destination = ticket_document_path(_tickets_dir(root), slug)
    record = read_state_record(_tickets_dir(root), slug)
    journal = DraftTransitionJournal(
        3,
        operation_id,
        slug,
        "initializing",
        basis.ticket_identity(),
        str(ticket.resolve()),
        _digest(ticket.read_bytes()),
        str(draft_destination.resolve()),
        _digest(draft_content),
        generation,
        _digest(generation_content),
        str(_next_archive(logs_dir / slug).resolve()),
        False,
        _authored_drift_record(document, drift_reason),
        record.execution_id if record is not None else "",
    )
    _write_journal(root, journal)
    return journal


def _prepare_generation(root: Path, journal: DraftTransitionJournal) -> DraftTransitionJournal:
    operation = _operation_dir(root, journal.operation_id)
    candidate = operation / "draft.md"
    fields = dict(_converted_document(root, candidate, journal.slug, "draft").spec.fields)
    worktrees = open_authoring_generation(
        root,
        candidate,
        journal.slug,
        fields,
        journal.generation,
        operation / "new-outer",
    )
    prepared = journal.with_state("prepared", has_project=worktrees.project is not None)
    _write_journal(root, prepared)
    return prepared


def _require_file(path: Path, digest: str, label: str) -> None:
    try:
        actual = _digest(path.read_bytes())
    except OSError as exc:
        raise DraftTransitionError(f"{label} is unavailable: {path}") from exc
    if actual != digest:
        raise DraftTransitionError(f"{label} changed unexpectedly: {path}")


def _validate_cutover(root: Path, journal: DraftTransitionJournal) -> TicketBaseline:
    blocked = Path(journal.blocked_ticket)
    _require_file(blocked, journal.blocked_sha256, "blocked Ticket")
    record = read_state_record(_tickets_dir(root), journal.slug)
    if record is None:
        raise DraftTransitionError("return-to-draft requires a blocked ticket, found draft")
    _require_captured_record(journal, record)
    candidate = _operation_dir(root, journal.operation_id) / "draft.md"
    _require_file(candidate, journal.draft_sha256, "replacement draft")
    document = _converted_document(root, blocked, journal.slug, "executable")
    try:
        basis = (
            load_ticket_recovery_baseline_from_document(root, journal.slug, document)
            if journal.authored_drift
            else load_ticket_baseline_from_document(root, journal.slug, document)
        )
    except TicketBaselineError as exc:
        raise DraftTransitionError(str(exc)) from exc
    errors = validate_basis_refs(
        root,
        basis,
        slug=journal.slug,
        destination_branch=basis.participant("outer").destination_ref.removeprefix("refs/heads/"),
    )
    if errors:
        raise DraftTransitionError("old Ticket baseline is invalid: " + "; ".join(errors))
    return basis


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise DraftTransitionError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout.strip()


def _find_worktree_for_ref(repository: Path, ref: str) -> Path | None:
    path: Path | None = None
    for line in [*_git(repository, "worktree", "list", "--porcelain").splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line == f"branch {ref}" and path is not None:
            return path
        elif not line:
            path = None
    return None


def _worktree_for_ref(repository: Path, ref: str) -> Path:
    path = _find_worktree_for_ref(repository, ref)
    if path is None:
        raise DraftTransitionError(f"worktree for {ref} is unavailable in {repository}")
    return path


def _move_worktree(
    repository: Path, ref: str, destination: Path, *, project_root: Path | None = None
) -> None:
    source = _worktree_for_ref(repository, ref)
    if source.resolve() == destination.resolve():
        return
    if source.exists() and destination.exists():
        raise DraftTransitionError(f"worktree destination already exists: {destination}")
    try:
        relocate_worktree(
            repository,
            ref,
            source,
            destination,
            relative_paths=relative_worktree_paths(project_root or repository),
        )
    except WorktreeRelocationError as exc:
        raise DraftTransitionError(str(exc)) from exc


def _move_worktree_if_present(
    repository: Path, ref: str, destination: Path, *, project_root: Path | None = None
) -> None:
    if _find_worktree_for_ref(repository, ref) is not None:
        _move_worktree(repository, ref, destination, project_root=project_root)


def _preflight_relocation(
    root: Path, journal: DraftTransitionJournal, basis: TicketBaseline
) -> None:
    operation = _operation_dir(root, journal.operation_id)
    canonical_outer = ticket_workspace_path(root, journal.slug)
    project_repository = resolve_inner_project_repo(root)
    old_outer = basis.participant("outer")
    new_ref = f"refs/heads/{_generation_branch(journal.generation, journal.slug)}"
    moves: list[WorktreeMove] = []
    old_outer_path = _find_worktree_for_ref(root, old_outer.ticket_ref)
    if old_outer_path is not None:
        moves.append(
            WorktreeMove(root, old_outer.ticket_ref, old_outer_path, operation / "old-outer")
        )
    new_outer_path = _worktree_for_ref(root, new_ref)
    moves.append(WorktreeMove(root, new_ref, new_outer_path, canonical_outer))
    if journal.has_project:
        if project_repository is None:
            raise DraftTransitionError("paired project repository is unavailable")
        old_project_path = _find_worktree_for_ref(
            project_repository, basis.participant("project").ticket_ref
        )
        if old_project_path is not None:
            moves.append(
                WorktreeMove(
                    project_repository,
                    basis.participant("project").ticket_ref,
                    old_project_path,
                    operation / "old-project",
                )
            )
        new_project_path = _worktree_for_ref(project_repository, new_ref)
        moves.append(
            WorktreeMove(
                project_repository,
                new_ref,
                new_project_path,
                operation / "new-project-moving",
            )
        )
    try:
        preflight_worktree_moves(tuple(moves))
    except WorktreeRelocationError as exc:
        raise DraftTransitionError(str(exc)) from exc


def _relocate_worktrees(
    root: Path, journal: DraftTransitionJournal, basis: TicketBaseline
) -> AuthoringWorkspace:
    operation = _operation_dir(root, journal.operation_id)
    canonical_outer = ticket_workspace_path(root, journal.slug)
    project_repository = resolve_inner_project_repo(root)
    old_outer = basis.participant("outer")
    new_ref = f"refs/heads/{_generation_branch(journal.generation, journal.slug)}"
    if journal.has_project and project_repository is None:
        raise DraftTransitionError("paired project repository is unavailable")
    _preflight_relocation(root, journal, basis)
    if journal.has_project:
        assert project_repository is not None
        old_project = basis.participant("project")
        _move_worktree_if_present(
            project_repository,
            old_project.ticket_ref,
            operation / "old-project",
            project_root=root,
        )
    _move_worktree_if_present(root, old_outer.ticket_ref, operation / "old-outer")
    if journal.has_project and project_repository is not None:
        _move_worktree(
            project_repository, new_ref, operation / "new-project-moving", project_root=root
        )
    _move_worktree(root, new_ref, canonical_outer)
    project_path = None
    if journal.has_project and project_repository is not None:
        project_path = ticket_project_worktree(canonical_outer)
        _move_worktree(project_repository, new_ref, project_path, project_root=root)
    outer_base = _git(canonical_outer, "rev-parse", "HEAD")
    project_base = _git(project_path, "rev-parse", "HEAD") if project_path else ""
    return AuthoringWorkspace(
        canonical_outer,
        project_path,
        outer_base,
        project_base,
        journal.generation,
    )


def _published_worktrees(
    root: Path, journal: DraftTransitionJournal, basis: TicketBaseline
) -> AuthoringWorkspace:
    outer = ticket_workspace_path(root, journal.slug)
    project = ticket_project_worktree(outer) if journal.has_project else None
    outer_base = _git(outer, "rev-parse", "HEAD")
    project_base = _git(project, "rev-parse", "HEAD") if project else ""
    return AuthoringWorkspace(
        outer,
        project,
        outer_base,
        project_base,
        journal.generation,
    )


def _finish_published_transition(
    root: Path, journal: DraftTransitionJournal, basis: TicketBaseline
) -> AuthoringWorkspace:
    """Confirm published identities, then retire the slug-level recovery journal."""
    draft = Path(journal.draft_ticket)
    _require_file(draft, journal.draft_sha256, "published draft")
    if read_state_record(_tickets_dir(root), journal.slug) is not None:
        raise DraftTransitionError("published draft still has a state record")
    _require_file(_generation_file(root, journal.slug), journal.generation_sha256, "generation")
    worktrees = _published_worktrees(root, journal, basis)
    new_ref = f"refs/heads/{_generation_branch(journal.generation, journal.slug)}"
    if _worktree_for_ref(root, new_ref).resolve() != worktrees.outer.resolve():
        raise DraftTransitionError("published outer authoring worktree identity changed")
    if journal.has_project:
        project = resolve_inner_project_repo(root)
        if project is None or worktrees.project is None:
            raise DraftTransitionError("published project authoring worktree is unavailable")
        if _worktree_for_ref(project, new_ref).resolve() != worktrees.project.resolve():
            raise DraftTransitionError("published project authoring worktree identity changed")
    _journal_path(root, journal.slug).unlink()
    return worktrees


def _require_captured_record(journal: DraftTransitionJournal, record: StateRecord) -> None:
    """Refuse a record that is no longer the blocked execution this journal captured."""
    if record.state is not TicketState.BLOCKED:
        raise DraftTransitionError(
            f"return-to-draft requires a blocked ticket, found {record.state.status}"
        )
    if (
        journal.blocked_execution_id is not None
        and record.execution_id != journal.blocked_execution_id
    ):
        raise DraftTransitionError(
            "blocked Ticket was re-run since return-to-draft started; retry return-to-draft"
        )


def _publish_board(root: Path, journal: DraftTransitionJournal) -> None:
    """Publish the draft-form document first, then delete the state record.

    The record must still be the blocked generation this journal captured
    (or already gone): that compare-and-swap runs before anything is written,
    including when the cutover resumes after a crash.
    """
    operation = _operation_dir(root, journal.operation_id)
    document = Path(journal.draft_ticket)
    tickets = _tickets_dir(root)
    record = read_state_record(tickets, journal.slug)
    if record is not None:
        _require_captured_record(journal, record)
    try:
        published = _digest(document.read_bytes()) == journal.draft_sha256
    except FileNotFoundError as exc:
        raise DraftTransitionError("blocked Ticket disappeared during cutover") from exc
    if not published:
        _publish_draft_document(operation, document, journal)
    if record is not None:
        # Keep the retired generation's runtime fields with its archived logs.
        atomic_replace_bytes(
            Path(journal.archive_dir) / "state.json", record.to_bytes(), mode=0o644
        )
        delete_state_record(tickets, journal.slug)
    # ADR 0066: the draft's Waiver Candidates go; its rejections stay. Idempotent.
    waiver_candidates.clear_candidates(tickets, journal.slug)


def _publish_draft_document(
    operation: Path, document: Path, journal: DraftTransitionJournal
) -> None:
    """Back up the blocked document, then replace it with the prepared draft."""
    _require_file(document, journal.blocked_sha256, "blocked Ticket")
    blocked_backup = operation / "blocked.md"
    if blocked_backup.exists():
        _require_file(blocked_backup, journal.blocked_sha256, "blocked Ticket backup")
    else:
        atomic_replace_bytes(blocked_backup, document.read_bytes(), mode=0o644)
    candidate = operation / "draft.md"
    _require_file(candidate, journal.draft_sha256, "replacement draft")
    candidate.replace(document)


def _publish_generation(root: Path, journal: DraftTransitionJournal) -> None:
    operation = _operation_dir(root, journal.operation_id)
    current = _generation_file(root, journal.slug)
    old = operation / "generation-old.json"
    candidate = operation / "generation.json"
    if current.exists() and _digest(current.read_bytes()) != journal.generation_sha256:
        if old.exists():
            raise DraftTransitionError("draft generation descriptor and backup both exist")
        current.replace(old)
    if current.exists():
        _require_file(current, journal.generation_sha256, "draft generation descriptor")
        return
    _require_file(candidate, journal.generation_sha256, "new generation descriptor")
    candidate.replace(current)


def _archive_runtime(log_dir: Path, archive: Path, operation_id: str) -> None:
    transition = log_dir / "human-logs" / "transitions.log"
    if transition.exists() and operation_id in transition.read_text(encoding="utf-8"):
        return
    archive.mkdir(parents=True, exist_ok=True)
    runtime = log_dir / ".runtime"
    if runtime.is_dir():
        archived_runtime = archive / ".runtime"
        archived_runtime.mkdir(exist_ok=True)
        for entry in list(runtime.iterdir()):
            if entry.name != "ticket.lock":
                _move_archive_entry(entry, archived_runtime / entry.name)
    for entry in list(log_dir.iterdir()):
        if entry.name not in {".runtime", "runs"}:
            _move_archive_entry(entry, archive / entry.name)


def _write_authored_drift_record(archive: Path, authored_drift: dict[str, str]) -> None:
    if not authored_drift:
        return
    content = (json.dumps(authored_drift, indent=2, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(archive / "authored-drift.json", content, mode=0o644)


def _move_archive_entry(source: Path, destination: Path) -> None:
    if destination.exists():
        if source.exists():
            raise DraftTransitionError(f"archive source and destination both exist: {source}")
        return
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))


def return_to_draft(
    project_root: Path | str,
    ticket_path: Path | str,
    slug: str,
    *,
    status: str,
    logs_dir: Path | str,
    append_transition: Callable[[str], None],
) -> AuthoringWorkspace:
    """Prepare and recoverably publish a fresh Ticket draft."""
    root = Path(project_root).resolve()
    logs = Path(logs_dir)
    journal = _load_journal(root, logs.resolve(), slug)
    if journal is None:
        journal = _new_journal(root, Path(ticket_path), slug, status, logs)
    if journal.state == "initializing":
        journal = _prepare_generation(root, journal)
    basis = ticket_baseline_from_machine(journal.machine)
    if journal.state == "prepared":
        basis = _validate_cutover(root, journal)
        journal = journal.with_state("cutover-ready")
        _write_journal(root, journal)
    if journal.state == "published":
        return _finish_published_transition(root, journal, basis)
    _relocate_worktrees(root, journal, basis)
    if journal.state == "cutover-ready":
        _publish_generation(root, journal)
        _archive_runtime(logs / slug, Path(journal.archive_dir), journal.operation_id)
        _write_authored_drift_record(Path(journal.archive_dir), journal.authored_drift)
        _publish_board(root, journal)
        append_transition(
            f"old Ticket generation {journal.machine['generation']}; "
            f"new draft identity {journal.generation}; "
            f"{journal.operation_id}"
        )
        journal = journal.with_state("published")
        _write_journal(root, journal)
    return _finish_published_transition(root, journal, basis)
