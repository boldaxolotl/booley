"""Recoverable acceptance cleanup for Tickets that deliberately omit merge."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.runtime.project_dir import runtime_dir

from .acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
    validate_current_basis_refs,
    worktree_for_ref,
)
from .helpers import validate_ticket_slug
from .persistence import atomic_replace_bytes
from .ticket_repositories import resolve_inner_project_repo

_STATES = ("pinned", "approved", "cleaned_project", "cleaned_outer", "done")


class CleanupOnlyError(RuntimeError):
    """Accepted Ticket heads could not be safely retained or cleaned."""


def _journal_path(root: Path, slug: str, basis: AcceptanceBasis) -> Path:
    validate_ticket_slug(slug)
    return runtime_dir(root) / "acceptance" / "cleanup-only" / slug / f"{basis.basis_id}.json"


def _source_ref(slug: str, basis: AcceptanceBasis, role: str) -> str:
    return f"refs/booley/acceptance/cleanup-only/{slug}/{basis.basis_id}/{role}"


def _repository(root: Path, participant: BasisParticipant) -> Path:
    if participant.role == "outer":
        return root
    project = resolve_inner_project_repo(root)
    if project is None:
        raise CleanupOnlyError("paired Project repository is unavailable")
    return project


def _git(repository: Path, *args: str, absent_ok: bool = False) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if absent_ok and result.returncode == 1:
        return None
    detail = (result.stderr or result.stdout).strip()
    raise CleanupOnlyError(f"git {' '.join(args)} failed in {repository}: {detail}")


def _ref_sha(repository: Path, ref: str) -> str | None:
    return _git(
        repository, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", absent_ok=True
    )


def _pin_sources(
    root: Path, slug: str, basis: AcceptanceBasis, sources: Mapping[str, str]
) -> None:
    for participant in basis.participants:
        repository = _repository(root, participant)
        source = sources[participant.role]
        ref = _source_ref(slug, basis, participant.role)
        current = _ref_sha(repository, ref)
        if current is None:
            _git(repository, "update-ref", ref, source, "0" * 40)
        elif current != source:
            raise CleanupOnlyError(f"accepted source pin {ref} changed")


def _read_journal(root: Path, slug: str, basis: AcceptanceBasis) -> dict[str, Any] | None:
    path = _journal_path(root, slug, basis)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupOnlyError(f"cleanup journal is unreadable: {path}") from exc
    roles = {participant.role for participant in basis.participants}
    if (
        not isinstance(record, dict)
        or set(record) != {"schema", "slug", "basis_id", "sources", "state"}
        or type(record["schema"]) is not int
        or record["schema"] != 1
        or record["slug"] != slug
        or record["basis_id"] != basis.basis_id
        or record["state"] not in _STATES
        or not isinstance(record["sources"], dict)
        or set(record["sources"]) != roles
        or any(
            not isinstance(value, str) or len(value) != 40 for value in record["sources"].values()
        )
    ):
        raise CleanupOnlyError("cleanup journal identity or schema is invalid")
    return record


def _write_journal(root: Path, slug: str, basis: AcceptanceBasis, record: dict[str, Any]) -> None:
    atomic_replace_bytes(
        _journal_path(root, slug, basis),
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def cleanup_only_sources(
    root: Path,
    slug: str,
    basis: AcceptanceBasis,
    expected_sources: Mapping[str, str],
) -> dict[str, str] | None:
    """Return durable accepted heads after checking every retained Git ref."""
    record = _read_journal(root, slug, basis)
    if record is None:
        return None
    sources = dict(record["sources"])
    if sources != dict(expected_sources):
        raise CleanupOnlyError("cleanup journal sources differ from accepted Ticket heads")
    for participant in basis.participants:
        repository = _repository(root, participant)
        ref = _source_ref(slug, basis, participant.role)
        if _ref_sha(repository, ref) != sources[participant.role]:
            raise CleanupOnlyError(f"accepted source pin {ref} is unavailable or changed")
    return sources


def _retire_participant(root: Path, participant: BasisParticipant, source: str) -> None:
    repository = _repository(root, participant)
    if participant.ticket_ref == participant.destination_ref:
        raise CleanupOnlyError("Ticket ref is also the destination ref")
    current = _ref_sha(repository, participant.ticket_ref)
    if current is None:
        return
    if current != source:
        raise CleanupOnlyError(f"Ticket ref {participant.ticket_ref} changed before cleanup")
    checkout = worktree_for_ref(repository, participant.ticket_ref)
    if checkout is not None:
        _git(repository, "worktree", "remove", str(checkout))
    _git(repository, "update-ref", "-d", participant.ticket_ref, source)


def advance_cleanup_only(
    tio: Any,
    slug: str,
    basis: AcceptanceBasis,
    expected_sources: Mapping[str, str],
) -> bool:
    """Pin, approve, and retire unmerged Ticket refs in recoverable order."""
    from .operations import _approve_transition

    root = Path(tio._project_root).resolve()
    record = _read_journal(root, slug, basis)
    if record is None:
        sources = validate_current_basis_refs(root, basis)
        if sources != dict(expected_sources):
            raise CleanupOnlyError("Ticket heads changed after accepted review")
        _pin_sources(root, slug, basis, sources)
        record = {
            "schema": 1,
            "slug": slug,
            "basis_id": basis.basis_id,
            "sources": sources,
            "state": "pinned",
        }
        _write_journal(root, slug, basis, record)
    else:
        cleanup_only_sources(root, slug, basis, expected_sources)
    if record["state"] == "pinned":
        entry = tio.find_ticket(slug)
        if entry is None or entry["status"] not in {"review", "done"}:
            raise CleanupOnlyError("Ticket is not awaiting completion")
        if entry["status"] == "review" and not _approve_transition(
            tio, slug, actor="op-complete", detail="cleanup without merge"
        ):
            raise CleanupOnlyError("Ticket approval did not complete")
        record["state"] = "approved"
        _write_journal(root, slug, basis, record)
    for role in ("project", "outer"):
        participant = next((item for item in basis.participants if item.role == role), None)
        if participant is None:
            continue
        checkpoint = f"cleaned_{role}"
        if _STATES.index(record["state"]) >= _STATES.index(checkpoint):
            continue
        _retire_participant(root, participant, record["sources"][role])
        record["state"] = checkpoint
        _write_journal(root, slug, basis, record)
    if record["state"] != "done":
        record["state"] = "done"
        _write_journal(root, slug, basis, record)
    return True
