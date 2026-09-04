"""Recoverable blocked-to-draft transition for Acceptance Basis Tickets."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Callable
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
from booley.runtime.project_dir import (
    resolve_checkout_project_dir,
    resolve_project_dir,
    runtime_dir,
)
from booley.runtime.ticket_repositories import resolve_inner_project_repo, ticket_project_worktree

from .acceptance_basis import AcceptanceBasis, AcceptanceBasisError, load_acceptance_basis
from .contract_ops import (
    ContractWorktrees,
    _generation_branch,
    _generation_file,
    _open_generation,
    validate_basis_refs,
)
from .frontmatter import format_frontmatter, parse_frontmatter
from .persistence import atomic_replace_bytes

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
    basis: dict[str, Any]
    basis_id: str
    blocked_ticket: str
    blocked_sha256: str
    draft_ticket: str
    draft_sha256: str
    generation: str
    generation_sha256: str
    archive_dir: str
    has_project: bool

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
    if set(mapping) != set(DraftTransitionJournal.__dataclass_fields__):
        raise BoundaryError("return-to-draft journal has invalid fields")
    state = require_str(mapping, "state")
    return DraftTransitionJournal(
        schema=require_int(mapping.get("schema"), field="return-to-draft journal schema"),
        operation_id=require_str(mapping, "operation_id"),
        slug=require_str(mapping, "slug"),
        state=cast(Literal["initializing", "prepared", "cutover-ready", "published"], state),
        basis=require_dict(mapping.get("basis"), field="return-to-draft journal basis"),
        basis_id=require_str(mapping, "basis_id"),
        blocked_ticket=require_str(mapping, "blocked_ticket"),
        blocked_sha256=require_str(mapping, "blocked_sha256"),
        draft_ticket=require_str(mapping, "draft_ticket"),
        draft_sha256=require_str(mapping, "draft_sha256"),
        generation=require_str(mapping, "generation"),
        generation_sha256=require_str(mapping, "generation_sha256"),
        archive_dir=require_str(mapping, "archive_dir"),
        has_project=require_bool(mapping, "has_project"),
    )


def transition_pending(project_root: Path | str, slug: str) -> bool:
    """Return whether a recoverable return-to-draft journal exists."""
    return _journal_path(Path(project_root).resolve(), slug).exists()


def _validate_journal(
    root: Path, logs_dir: Path, slug: str, journal: DraftTransitionJournal
) -> None:
    if journal.schema != 1 or journal.slug != slug or journal.state not in _STATES:
        raise DraftTransitionError("return-to-draft journal identity or schema is invalid")
    if not _OPERATION_RE.fullmatch(journal.operation_id):
        raise DraftTransitionError("return-to-draft journal operation ID is invalid")
    try:
        basis = AcceptanceBasis.from_mapping(journal.basis)
    except AcceptanceBasisError as exc:
        raise DraftTransitionError(str(exc)) from exc
    if journal.basis_id != basis.basis_id:
        raise DraftTransitionError("return-to-draft journal basis identity changed")
    board = resolve_checkout_project_dir(root) / "tickets" / "board"
    draft = Path(journal.draft_ticket).resolve()
    if draft != (board / "drafts" / f"{slug}.md").resolve():
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
    blocked = Path(journal.blocked_ticket).resolve()
    if blocked != (board / "blocked" / f"{slug}.md").resolve():
        raise DraftTransitionError("return-to-draft blocked Ticket path is invalid")
    archive = Path(journal.archive_dir).resolve()
    archive_root = (logs_dir / slug / "runs").resolve()
    operation = _operation_dir(root, journal.operation_id).resolve()
    if operation.parent != _transition_root(root).resolve():
        raise DraftTransitionError("return-to-draft operation path is invalid")
    if archive.parent != archive_root or re.fullmatch(r"[0-9]{3}", archive.name) is None:
        raise DraftTransitionError("return-to-draft publication metadata is invalid")


def _next_archive(log_dir: Path) -> Path:
    runs = log_dir / "runs"
    existing = [int(path.name) for path in runs.glob("[0-9][0-9][0-9]") if path.is_dir()]
    return runs / f"{(max(existing, default=0) + 1):03d}"


def _draft_content(ticket: Path) -> tuple[dict[str, Any], str, bytes]:
    fields, body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    draft = dict(fields)
    for field in ("acceptance_basis", "created", "feature_branch", "steps_completed", "stage"):
        draft.pop(field, None)
    return fields, body, format_frontmatter(draft, body).encode()


def _new_journal(
    root: Path,
    ticket: Path,
    slug: str,
    status: str,
    logs_dir: Path,
) -> DraftTransitionJournal:
    if status != "blocked":
        raise DraftTransitionError(f"return-to-draft requires a blocked ticket, got {status!r}")
    fields, body, draft_content = _draft_content(ticket)
    try:
        basis = load_acceptance_basis(root, slug, fields, body)
    except AcceptanceBasisError as exc:
        raise DraftTransitionError(str(exc)) from exc
    operation_id = uuid.uuid4().hex
    operation = _operation_dir(root, operation_id)
    draft_path = operation / "draft.md"
    generation = secrets.token_hex(8)
    generation_content = (json.dumps({"generation": generation}, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(draft_path, draft_content, mode=0o644)
    atomic_replace_bytes(operation / "generation.json", generation_content)
    draft_destination = ticket.parent.parent / "drafts" / ticket.name
    journal = DraftTransitionJournal(
        1,
        operation_id,
        slug,
        "initializing",
        basis.as_dict(),
        basis.basis_id,
        str(ticket.resolve()),
        _digest(ticket.read_bytes()),
        str(draft_destination.resolve()),
        _digest(draft_content),
        generation,
        _digest(generation_content),
        str(_next_archive(logs_dir / slug).resolve()),
        False,
    )
    _write_journal(root, journal)
    return journal


def _prepare_generation(root: Path, journal: DraftTransitionJournal) -> DraftTransitionJournal:
    operation = _operation_dir(root, journal.operation_id)
    candidate = operation / "draft.md"
    fields, _body = parse_frontmatter(candidate.read_text(encoding="utf-8"))
    worktrees = _open_generation(
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


def _validate_cutover(root: Path, journal: DraftTransitionJournal) -> AcceptanceBasis:
    blocked = Path(journal.blocked_ticket)
    _require_file(blocked, journal.blocked_sha256, "blocked Ticket")
    candidate = _operation_dir(root, journal.operation_id) / "draft.md"
    _require_file(candidate, journal.draft_sha256, "replacement draft")
    fields, body = parse_frontmatter(blocked.read_text(encoding="utf-8"))
    try:
        basis = load_acceptance_basis(root, journal.slug, fields, body)
    except AcceptanceBasisError as exc:
        raise DraftTransitionError(str(exc)) from exc
    errors = validate_basis_refs(
        root,
        basis,
        slug=journal.slug,
        destination_branch=str(fields.get("branch", "")),
    )
    if errors:
        raise DraftTransitionError("old Acceptance Basis is invalid: " + "; ".join(errors))
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


def _worktree_for_ref(repository: Path, ref: str) -> Path:
    path: Path | None = None
    for line in [*_git(repository, "worktree", "list", "--porcelain").splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line == f"branch {ref}" and path is not None:
            return path
        elif not line:
            path = None
    raise DraftTransitionError(f"worktree for {ref} is unavailable in {repository}")


def _move_worktree(repository: Path, ref: str, destination: Path) -> None:
    source = _worktree_for_ref(repository, ref)
    if source.resolve() == destination.resolve():
        return
    if destination.exists():
        raise DraftTransitionError(f"worktree destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "move", str(source), str(destination))


def _relocate_worktrees(
    root: Path, journal: DraftTransitionJournal, basis: AcceptanceBasis
) -> ContractWorktrees:
    operation = _operation_dir(root, journal.operation_id)
    canonical_outer = resolve_project_dir(root) / "worktrees" / journal.slug
    project_repository = resolve_inner_project_repo(root)
    old_outer = basis.participant("outer")
    new_ref = f"refs/heads/{_generation_branch(journal.generation, journal.slug)}"
    if journal.has_project:
        if project_repository is None:
            raise DraftTransitionError("paired project repository is unavailable")
        old_project = basis.participant("project")
        _move_worktree(project_repository, old_project.ticket_ref, operation / "old-project")
    _move_worktree(root, old_outer.ticket_ref, operation / "old-outer")
    if journal.has_project and project_repository is not None:
        _move_worktree(project_repository, new_ref, operation / "new-project-moving")
    _move_worktree(root, new_ref, canonical_outer)
    project_path = None
    if journal.has_project and project_repository is not None:
        project_path = ticket_project_worktree(canonical_outer)
        _move_worktree(project_repository, new_ref, project_path)
    return ContractWorktrees(
        canonical_outer,
        project_path,
        old_outer.destination_sha,
        basis.project_sha and basis.participant("project").destination_sha,
        journal.generation,
    )


def _published_worktrees(
    root: Path, journal: DraftTransitionJournal, basis: AcceptanceBasis
) -> ContractWorktrees:
    outer = resolve_project_dir(root) / "worktrees" / journal.slug
    project = ticket_project_worktree(outer) if journal.has_project else None
    project_base = basis.participant("project").destination_sha if journal.has_project else ""
    return ContractWorktrees(
        outer,
        project,
        basis.participant("outer").destination_sha,
        project_base,
        journal.generation,
    )


def _finish_published_transition(
    root: Path, journal: DraftTransitionJournal, basis: AcceptanceBasis
) -> ContractWorktrees:
    """Confirm published identities, then retire the slug-level recovery journal."""
    draft = Path(journal.draft_ticket)
    _require_file(draft, journal.draft_sha256, "published draft")
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


def _publish_board(root: Path, journal: DraftTransitionJournal) -> None:
    operation = _operation_dir(root, journal.operation_id)
    blocked = Path(journal.blocked_ticket)
    blocked_backup = operation / "blocked.md"
    draft = Path(journal.draft_ticket)
    candidate = operation / "draft.md"
    if blocked.exists():
        _require_file(blocked, journal.blocked_sha256, "blocked Ticket")
        if blocked_backup.exists():
            raise DraftTransitionError("blocked Ticket and its backup both exist")
        blocked.replace(blocked_backup)
    elif not blocked_backup.exists() and not draft.exists():
        raise DraftTransitionError("blocked Ticket disappeared during cutover")
    if draft.exists():
        _require_file(draft, journal.draft_sha256, "published draft")
    else:
        _require_file(candidate, journal.draft_sha256, "replacement draft")
        draft.parent.mkdir(parents=True, exist_ok=True)
        candidate.replace(draft)


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
) -> ContractWorktrees:
    """Prepare and recoverably publish a new authoring generation."""
    root = Path(project_root).resolve()
    logs = Path(logs_dir)
    journal = _load_journal(root, logs.resolve(), slug)
    if journal is None:
        journal = _new_journal(root, Path(ticket_path), slug, status, logs)
    if journal.state == "initializing":
        journal = _prepare_generation(root, journal)
    basis = AcceptanceBasis.from_mapping(journal.basis)
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
        _publish_board(root, journal)
        append_transition(
            f"old basis {journal.basis_id}; new authoring generation {journal.generation}; "
            f"{journal.operation_id}"
        )
        journal = journal.with_state("published")
        _write_journal(root, journal)
    return _finish_published_transition(root, journal, basis)
