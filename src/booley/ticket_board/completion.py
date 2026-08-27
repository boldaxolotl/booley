"""Recoverable acceptance of sealed multi-repository Tickets.

Acceptance is deliberately expressed as one transaction-like operation.  Git
cannot atomically update refs in two repositories, so every merge candidate is
prepared before either destination moves and a durable journal records the
subsequent roll-forward publication.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from booley.runtime.file_lock import LockContentionError, nonblocking_file_lock
from booley.runtime.project_dir import runtime_dir
from booley.runtime.ticket_repositories import resolve_inner_project_repo

from .git_ops import worktree_is_clean
from .target_contract import (
    ContractParticipant,
    TargetContract,
    TargetContractError,
    verify_surface,
)


class CompletionError(RuntimeError):
    """A sealed Ticket could not be prepared or published safely."""


def _git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompletionError(f"git {' '.join(args)} failed in {repository}: {exc}") from exc


def _require_git(repository: Path, *args: str) -> str:
    result = _git(repository, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CompletionError(
            f"git {' '.join(args)} failed in {repository} (rc={result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _commit(repository: Path, ref: str) -> str:
    return _require_git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    result = _git(repository, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in {0, 1}:
        detail = (result.stderr or result.stdout).strip()
        raise CompletionError(f"could not compare Git history in {repository}: {detail}")
    return result.returncode == 0


def _repository_for(
    root: Path, project_repository: Path | None, participant: ContractParticipant
) -> Path:
    if participant.role == "outer":
        return root
    if participant.role == "project" and project_repository is not None:
        return project_repository
    raise CompletionError(f"sealed {participant.role} repository is unavailable")


def _journal_path(root: Path, slug: str) -> Path:
    directory = runtime_dir(root) / "acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{slug}.json"


def _write_journal(path: Path, journal: Mapping[str, Any]) -> None:
    """Atomically persist and fsync a recovery checkpoint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(journal, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _initial_journal(slug: str, contract: TargetContract) -> dict[str, Any]:
    return {
        "schema": 1,
        "transaction": uuid.uuid4().hex,
        "ticket": slug,
        "state": "initializing",
        "participants": [item.as_dict() for item in contract.participants],
        "candidates": {},
        "published": [],
    }


def _load_journal(path: Path, slug: str, contract: TargetContract) -> dict[str, Any]:
    expected = [item.as_dict() for item in contract.participants]
    if not path.exists():
        return _initial_journal(slug, contract)
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompletionError(f"acceptance journal is unreadable: {path}: {exc}") from exc
    if not isinstance(journal, dict) or journal.get("ticket") != slug:
        raise CompletionError(f"acceptance journal does not belong to Ticket {slug!r}")
    if journal.get("participants") != expected:
        raise CompletionError(
            "sealed repository participants changed after acceptance began; "
            f"inspect {path} before retrying"
        )
    return journal


def _changed_paths(repository: Path, before: str, after: str) -> set[str]:
    output = _require_git(repository, "diff", "--name-only", "-z", before, after)
    return {path for path in output.split("\0") if path}


def _validate_participant(
    repository: Path,
    participant: ContractParticipant,
    protected_paths: set[str],
) -> str:
    source = _commit(repository, participant.ticket_ref)
    destination = _commit(repository, participant.destination_ref)
    if not _is_ancestor(repository, participant.sealed_sha, source):
        raise CompletionError(
            f"{participant.ticket_ref} no longer descends from sealed "
            f"{participant.role} commit {participant.sealed_sha}"
        )
    if not _is_ancestor(repository, participant.destination_sha, destination):
        raise CompletionError(
            f"{participant.destination_ref} rewrote the sealed destination history"
        )
    ticket_changes = _changed_paths(repository, participant.destination_sha, source)
    destination_changes = _changed_paths(repository, participant.destination_sha, destination)
    collisions = sorted(ticket_changes & destination_changes & protected_paths)
    if collisions:
        raise CompletionError(
            f"{participant.destination_ref} changed sealed control path(s) also changed "
            f"by this Ticket: {', '.join(collisions)}"
        )
    return destination


def _prepare_candidate(
    repository: Path,
    participant: ContractParticipant,
    transaction: str,
    slug: str,
    protected_paths: set[str],
) -> dict[str, str]:
    destination = _validate_participant(repository, participant, protected_paths)
    source = _commit(repository, participant.ticket_ref)
    staging_ref = f"refs/booley/acceptance/{transaction}/{participant.role}"
    if _is_ancestor(repository, source, destination):
        candidate = destination
    else:
        temporary = Path(tempfile.mkdtemp(prefix=f"booley-accept-{participant.role}-"))
        try:
            _require_git(repository, "worktree", "add", "--detach", str(temporary), destination)
            try:
                _require_git(
                    temporary,
                    "merge",
                    "--no-ff",
                    participant.ticket_ref,
                    "-m",
                    f"merge({slug}): sealed Ticket completed",
                )
                candidate = _commit(temporary, "HEAD")
            finally:
                _git(repository, "worktree", "remove", "--force", str(temporary))
        finally:
            with suppress(FileNotFoundError):
                temporary.rmdir()
    _require_git(repository, "update-ref", staging_ref, candidate)
    return {
        "sha": candidate,
        "staging_ref": staging_ref,
        "expected_destination_sha": destination,
    }


def _validate_source_surface(
    root: Path,
    project_repository: Path | None,
    contract: TargetContract,
) -> None:
    """Rebuild the sealed composite checkout and reject contract-control drift."""
    participants = {item.role: item for item in contract.participants}
    outer = participants["outer"]
    temporary = Path(tempfile.mkdtemp(prefix="booley-accept-surface-"))
    project_checkout: Path | None = None
    try:
        _require_git(root, "worktree", "add", "--detach", str(temporary), outer.ticket_ref)
        project = participants.get("project")
        if project is not None:
            if project_repository is None:
                raise CompletionError("sealed project repository is unavailable")
            project_checkout = temporary / ".booley_project"
            _require_git(
                project_repository,
                "worktree",
                "add",
                "--detach",
                str(project_checkout),
                project.ticket_ref,
            )
        try:
            verify_surface(contract, temporary)
        except TargetContractError as exc:
            raise CompletionError(str(exc)) from exc
    finally:
        if project_checkout is not None and project_repository is not None:
            _git(
                project_repository,
                "worktree",
                "remove",
                "--force",
                str(project_checkout),
            )
        _git(root, "worktree", "remove", "--force", str(temporary))
        with suppress(FileNotFoundError):
            temporary.rmdir()


def _checked_out_at(repository: Path, destination_ref: str) -> Path | None:
    output = _require_git(repository, "worktree", "list", "--porcelain")
    worktree: Path | None = None
    for line in [*output.splitlines(), ""]:
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree "))
        elif line == f"branch {destination_ref}":
            return worktree
        elif not line:
            worktree = None
    return None


def _publish_candidate(
    repository: Path,
    participant: ContractParticipant,
    candidate: Mapping[str, str],
    allowed_board_rename: tuple[Path, Path],
) -> None:
    current = _commit(repository, participant.destination_ref)
    desired = candidate["sha"]
    if current == desired or _is_ancestor(repository, desired, current):
        return
    expected = candidate["expected_destination_sha"]
    if current != expected:
        raise CompletionError(
            f"{participant.destination_ref} moved from {expected} to {current} "
            "after acceptance preparation; retry after inspecting the journal"
        )
    checkout = _checked_out_at(repository, participant.destination_ref)
    if checkout is not None:
        if not worktree_is_clean(str(checkout), allowed_unstaged_rename=allowed_board_rename):
            raise CompletionError(
                f"cannot publish {participant.destination_ref}: its checkout at "
                f"{checkout} has changes outside this Ticket's board transition"
            )
        _require_git(checkout, "merge", "--ff-only", candidate["staging_ref"])
        return
    _require_git(
        repository,
        "update-ref",
        participant.destination_ref,
        desired,
        expected,
    )


def _prepare_all(
    root: Path,
    project_repository: Path | None,
    slug: str,
    contract: TargetContract,
    journal: dict[str, Any],
    journal_path: Path,
) -> None:
    candidates = journal["candidates"]
    transaction = journal["transaction"]
    has_project = any(item.role == "project" for item in contract.participants)
    for participant in contract.participants:
        if participant.role in candidates:
            repository = _repository_for(root, project_repository, participant)
            if (
                _commit(repository, candidates[participant.role]["staging_ref"])
                != candidates[participant.role]["sha"]
            ):
                raise CompletionError(
                    f"acceptance staging ref for {participant.role} no longer matches its journal"
                )
            continue
        repository = _repository_for(root, project_repository, participant)
        if participant.role == "project":
            prefix = ".booley_project/"
            protected_paths = {
                item.path.removeprefix(prefix)
                for item in contract.surface_entries
                if item.path.startswith(prefix)
            }
        elif has_project:
            protected_paths = {
                item.path
                for item in contract.surface_entries
                if not item.path.startswith(".booley_project/")
            }
        else:
            protected_paths = {item.path for item in contract.surface_entries}
        candidates[participant.role] = _prepare_candidate(
            repository,
            participant,
            transaction,
            slug,
            protected_paths,
        )
        _write_journal(journal_path, journal)
    journal["state"] = "prepared"
    _write_journal(journal_path, journal)


def _publish_all(
    root: Path,
    project_repository: Path | None,
    contract: TargetContract,
    journal: dict[str, Any],
    journal_path: Path,
    allowed_board_rename: tuple[Path, Path],
) -> None:
    by_role = {item.role: item for item in contract.participants}
    # Publish the hidden control repository first.  The user-visible outer ref
    # moves last, after every candidate is known to be conflict-free.
    roles = [role for role in ("project", "outer") if role in by_role]
    for role in roles:
        if role in journal["published"]:
            continue
        participant = by_role[role]
        repository = _repository_for(root, project_repository, participant)
        _publish_candidate(
            repository,
            participant,
            journal["candidates"][role],
            allowed_board_rename,
        )
        journal["published"].append(role)
        journal["state"] = f"published-{role}"
        _write_journal(journal_path, journal)


def _approve(tio: Any, slug: str) -> bool:
    return tio.move_and_update(
        slug,
        "done",
        {"step": "complete"},
        transition=(
            "review:summary",
            "done:complete",
            "op-complete",
            "terminal actions",
        ),
        enforce_lifecycle=True,
        expected_status="review",
    )


def complete_review_ticket(tio: Any, slug: str, effective_policy: Any) -> bool:
    """Prepare, publish, and approve one schema-3 review Ticket.

    The journal makes calls idempotent after an interruption.  A retry reuses
    pinned candidates, recognizes refs already published, and continues toward
    the review-to-done transition.
    """
    if not effective_policy.merge:
        raise CompletionError("journaled completion requires merge policy")
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return False
    try:
        contract = TargetContract.from_mapping(entry.get("target_contract"))
    except TargetContractError as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
        return False
    if not contract.participants:
        print(
            f"Error: cannot complete '{slug}': sealed repository participants are missing",
            file=sys.stderr,
        )
        return False

    root = Path(tio._project_root).resolve()
    ticket_name = Path(str(entry["file"])).name
    allowed_board_rename = (
        tio.tickets_dir / "board" / "queue" / ticket_name,
        tio.tickets_dir / str(entry["file"]),
    )
    path = _journal_path(root, slug)
    lock_path = path.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    try:
        with (
            lock_path.open("a+", encoding="utf-8") as handle,
            nonblocking_file_lock(handle),
        ):
            journal = _load_journal(path, slug, contract)
            _write_journal(path, journal)
            project_repository = resolve_inner_project_repo(root)
            _validate_source_surface(root, project_repository, contract)
            _prepare_all(root, project_repository, slug, contract, journal, path)
            _publish_all(
                root,
                project_repository,
                contract,
                journal,
                path,
                allowed_board_rename,
            )
            if journal.get("state") != "done":
                if not _approve(tio, slug):
                    raise CompletionError(
                        "repository publication succeeded but the board transition failed; retry"
                    )
                journal["state"] = "done"
                _write_journal(path, journal)
    except LockContentionError:
        print(f"Error: acceptance is already running for '{slug}'", file=sys.stderr)
        return False
    except (CompletionError, OSError, ValueError) as exc:
        print(f"Error: completion failed for '{slug}': {exc}", file=sys.stderr)
        return False
    return True
