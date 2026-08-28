"""Recoverable acceptance of sealed multi-repository Tickets.

Acceptance is deliberately expressed as one transaction-like operation.  Git
cannot atomically update refs in two repositories, so every merge candidate is
prepared before either destination moves and a durable journal records the
subsequent roll-forward publication.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_list, require_str
from booley.runtime.file_lock import LockContentionError, nonblocking_file_lock
from booley.runtime.project_dir import checkout_project_dir_relative_to, runtime_dir
from booley.runtime.ticket_repositories import resolve_inner_project_repo

from .contract_ops import ContractOperationError, pin_sealed_refs
from .git_ops import worktree_is_clean
from .target_contract import (
    ContractParticipant,
    TargetContract,
    TargetContractError,
    verify_surface,
)
from .validation import retired_ticket_field_errors


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


def _initial_journal(
    slug: str,
    contract: TargetContract,
    *,
    cleanup: bool = False,
) -> dict[str, Any]:
    return {
        "schema": 2,
        "transaction": uuid.uuid4().hex,
        "ticket": slug,
        "state": "initializing",
        "policy": {"merge": True, "cleanup": cleanup},
        "participants": [item.as_dict() for item in contract.participants],
        "sources": {},
        "candidates": {},
        "published": [],
        "cleaned": [],
    }


def _validated_string_map(value: Any, field: str, roles: set[str]) -> dict[str, str]:
    mapping = require_dict(value, field=field)
    if not set(mapping) <= roles:
        raise BoundaryError(f"{field} contains an unknown participant role")
    result: dict[str, str] = {}
    for role in mapping:
        item = require_str(mapping, role)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item):
            raise BoundaryError(f"{field}.{role} must be a full Git commit SHA")
        result[role] = item
    return result


def _validated_candidates(
    value: Any,
    roles: set[str],
    transaction: str,
) -> dict[str, dict[str, str]]:
    mapping = require_dict(value, field="acceptance journal candidates")
    if not set(mapping) <= roles:
        raise BoundaryError("acceptance journal candidates contains an unknown role")
    result: dict[str, dict[str, str]] = {}
    for role, raw in mapping.items():
        candidate = require_dict(raw, field=f"acceptance journal candidates.{role}")
        if set(candidate) != {"sha", "staging_ref", "expected_destination_sha"}:
            raise BoundaryError(f"acceptance journal candidate {role!r} has invalid fields")
        strings = {key: require_str(candidate, key) for key in candidate}
        for key in ("sha", "expected_destination_sha"):
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", strings[key]):
                raise BoundaryError(f"acceptance journal candidates.{role}.{key} is invalid")
        expected_ref = f"refs/booley/acceptance/{transaction}/{role}"
        if strings["staging_ref"] != expected_ref:
            raise BoundaryError(
                f"acceptance journal candidates.{role}.staging_ref must be {expected_ref!r}"
            )
        result[str(role)] = strings
    return result


def _validate_journal_progress(
    state: str,
    roles: set[str],
    cleanup: bool,
    sources: dict[str, str],
    candidates: dict[str, dict[str, str]],
    published: list[Any],
    cleaned: list[Any],
) -> None:
    order = [role for role in ("project", "outer") if role in roles]
    if published != order[: len(published)]:
        raise BoundaryError("acceptance journal published roles are out of order")
    if cleaned != order[: len(cleaned)]:
        raise BoundaryError("acceptance journal cleaned roles are out of order")
    if candidates and not sources:
        raise BoundaryError("acceptance journal candidates require pinned sources")
    if sources and set(sources) != roles:
        raise BoundaryError("acceptance journal sources must pin every participant")
    if set(candidates) - set(sources) or set(published) - set(candidates):
        raise BoundaryError("acceptance journal checkpoints are inconsistent")
    if set(cleaned) - set(published):
        raise BoundaryError("acceptance journal cannot clean unpublished participants")
    if state == "initializing" and (published or cleaned):
        raise BoundaryError("initializing acceptance journal cannot contain terminal progress")
    if state != "initializing" and set(candidates) != roles:
        raise BoundaryError(f"acceptance journal state {state!r} requires every candidate")
    expected_published = {
        "prepared": [],
        "published-project": ["project"],
        "published-outer": order,
        "accepted": order,
        "cleanup-project": order,
        "cleanup-outer": order,
        "done": order,
    }
    if state in expected_published and published != expected_published[state]:
        raise BoundaryError(f"acceptance journal state {state!r} conflicts with published roles")
    expected_cleaned = {
        "prepared": [],
        "published-project": [],
        "published-outer": [],
        "accepted": [],
        "cleanup-project": ["project"],
        "cleanup-outer": order,
        "done": order if cleanup else [],
    }
    if state in expected_cleaned and cleaned != expected_cleaned[state]:
        raise BoundaryError(f"acceptance journal state {state!r} conflicts with cleaned roles")
    if cleaned and not cleanup:
        raise BoundaryError("acceptance journal cleaned roles require cleanup policy")


def _validated_journal(
    value: Any,
    slug: str,
    participants: list[dict[str, str]],
    *,
    cleanup: bool,
) -> dict[str, Any]:
    journal = require_dict(value, field="acceptance journal")
    expected_fields = {
        "schema",
        "transaction",
        "ticket",
        "state",
        "policy",
        "participants",
        "sources",
        "candidates",
        "published",
        "cleaned",
    }
    if require_str(journal, "ticket") != slug:
        raise BoundaryError(f"acceptance journal does not belong to Ticket {slug!r}")
    if journal.get("participants") != participants:
        raise BoundaryError("sealed repository participants changed after acceptance began")
    if set(journal) != expected_fields:
        raise BoundaryError("acceptance journal has invalid fields")
    if journal.get("schema") != 2:
        raise BoundaryError("acceptance journal schema must be 2")
    transaction = require_str(journal, "transaction")
    if not re.fullmatch(r"[0-9a-f]{32}", transaction):
        raise BoundaryError("acceptance journal transaction is invalid")
    state = require_str(journal, "state")
    if state not in {
        "initializing",
        "prepared",
        "published-project",
        "published-outer",
        "accepted",
        "cleanup-project",
        "cleanup-outer",
        "done",
    }:
        raise BoundaryError(f"acceptance journal state {state!r} is invalid")
    policy = require_dict(journal.get("policy"), field="acceptance journal policy")
    if set(policy) != {"merge", "cleanup"}:
        raise BoundaryError("acceptance journal policy has invalid fields")
    if policy.get("merge") is not True or not isinstance(policy.get("cleanup"), bool):
        raise BoundaryError("acceptance journal policy is invalid")
    if policy["cleanup"] != cleanup:
        raise BoundaryError("acceptance journal cleanup policy changed after acceptance began")
    roles = {item["role"] for item in participants}
    sources = _validated_string_map(journal.get("sources"), "acceptance journal sources", roles)
    candidates = _validated_candidates(journal.get("candidates"), roles, transaction)
    published = require_list(journal.get("published"), field="acceptance journal published")
    cleaned = require_list(journal.get("cleaned"), field="acceptance journal cleaned")
    _validate_journal_progress(
        state,
        roles,
        cleanup,
        sources,
        candidates,
        published,
        cleaned,
    )
    return {
        "schema": 2,
        "transaction": transaction,
        "ticket": slug,
        "state": state,
        "policy": {"merge": True, "cleanup": cleanup},
        "participants": participants,
        "sources": sources,
        "candidates": candidates,
        "published": published,
        "cleaned": cleaned,
    }


def _upgrade_schema_one_journal(value: Any, *, cleanup: bool) -> Any:
    """Make an existing publication journal resumable by the cleanup-aware schema."""
    if not isinstance(value, dict) or value.get("schema") != 1:
        return value
    expected = {
        "schema",
        "transaction",
        "ticket",
        "state",
        "participants",
        "sources",
        "candidates",
        "published",
    }
    if set(value) != expected:
        return value
    upgraded = dict(value)
    upgraded["schema"] = 2
    upgraded["policy"] = {"merge": True, "cleanup": cleanup}
    upgraded["cleaned"] = []
    if upgraded["state"] == "done" and cleanup:
        upgraded["state"] = "accepted"
    return upgraded


def _load_journal(
    path: Path,
    slug: str,
    contract: TargetContract,
    *,
    cleanup: bool = False,
) -> dict[str, Any]:
    expected = [item.as_dict() for item in contract.participants]
    if not path.exists():
        return _initial_journal(slug, contract, cleanup=cleanup)
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompletionError(f"acceptance journal is unreadable: {path}: {exc}") from exc
    try:
        upgraded = _upgrade_schema_one_journal(journal, cleanup=cleanup)
        return _validated_journal(upgraded, slug, expected, cleanup=cleanup)
    except BoundaryError as exc:
        raise CompletionError(f"acceptance journal is malformed: {path}: {exc}") from exc


def _changed_paths(repository: Path, before: str, after: str) -> set[str]:
    output = _require_git(repository, "diff", "--name-only", "-z", before, after)
    return {path for path in output.split("\0") if path}


def _validate_participant(
    repository: Path,
    participant: ContractParticipant,
    source: str,
    protected_paths: set[str],
) -> str:
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
    source: str,
    transaction: str,
    slug: str,
    protected_paths: set[str],
) -> dict[str, str]:
    destination = _validate_participant(repository, participant, source, protected_paths)
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
                    source,
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
    sources: Mapping[str, str],
) -> None:
    """Rebuild the sealed composite checkout and reject contract-control drift."""
    participants = {item.role: item for item in contract.participants}
    temporary = Path(tempfile.mkdtemp(prefix="booley-accept-surface-"))
    project_checkout: Path | None = None
    try:
        _require_git(root, "worktree", "add", "--detach", str(temporary), sources["outer"])
        project = participants.get("project")
        if project is not None:
            if project_repository is None:
                raise CompletionError("sealed project repository is unavailable")
            try:
                project_relative = checkout_project_dir_relative_to(root)
            except (FileNotFoundError, ValueError) as exc:
                raise CompletionError(str(exc)) from exc
            project_checkout = temporary / project_relative
            _require_git(
                project_repository,
                "worktree",
                "add",
                "--detach",
                str(project_checkout),
                sources["project"],
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


def _ref_commit(repository: Path, ref: str) -> str | None:
    """Return one full-ref commit, or ``None`` when the ref is absent."""
    result = _git(repository, "show-ref", "--verify", "--quiet", ref)
    if result.returncode == 0:
        return _commit(repository, ref)
    if result.returncode == 1:
        return None
    detail = (result.stderr or result.stdout).strip()
    raise CompletionError(f"could not inspect {ref} in {repository}: {detail}")


def _delete_ref_at(repository: Path, ref: str, expected: str) -> None:
    """Delete one ref only while it points at its journaled identity."""
    current = _ref_commit(repository, ref)
    if current is None:
        return
    if current != expected:
        raise CompletionError(f"refusing to delete {ref}: expected {expected}, found {current}")
    result = _git(repository, "update-ref", "-d", ref, expected)
    if result.returncode == 0 or _ref_commit(repository, ref) is None:
        return
    detail = (result.stderr or result.stdout).strip()
    raise CompletionError(f"could not delete {ref} at {expected}: {detail}")


def _remove_ticket_worktree(
    repository: Path, participant: ContractParticipant, source: str
) -> None:
    checkout = _checked_out_at(repository, participant.ticket_ref)
    if checkout is None:
        return
    head = _commit(checkout, "HEAD")
    if head != source:
        raise CompletionError(
            f"refusing to remove {checkout}: expected Ticket HEAD {source}, found {head}"
        )
    if not worktree_is_clean(str(checkout)):
        raise CompletionError(f"refusing to remove dirty Ticket worktree {checkout}")
    result = _git(repository, "worktree", "remove", str(checkout))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CompletionError(f"could not remove Ticket worktree {checkout}: {detail}")


def _cleanup_participant(
    repository: Path,
    participant: ContractParticipant,
    source: str,
    candidate: Mapping[str, str],
) -> None:
    if participant.ticket_ref == participant.destination_ref:
        raise CompletionError(
            f"refusing cleanup because {participant.ticket_ref} is also the destination ref"
        )
    _remove_ticket_worktree(repository, participant, source)
    _delete_ref_at(repository, participant.ticket_ref, source)
    _delete_ref_at(repository, candidate["staging_ref"], candidate["sha"])


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
    try:
        project_prefix = checkout_project_dir_relative_to(root).as_posix().rstrip("/") + "/"
    except (FileNotFoundError, ValueError) as exc:
        raise CompletionError(str(exc)) from exc
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
            protected_paths = {
                item.path.removeprefix(project_prefix)
                for item in contract.surface_entries
                if item.path.startswith(project_prefix)
            }
        elif has_project:
            protected_paths = {
                item.path
                for item in contract.surface_entries
                if not item.path.startswith(project_prefix)
            }
        else:
            protected_paths = {item.path for item in contract.surface_entries}
        candidates[participant.role] = _prepare_candidate(
            repository,
            participant,
            journal["sources"][participant.role],
            transaction,
            slug,
            protected_paths,
        )
        _write_journal(journal_path, journal)
    if not journal["published"]:
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


def _ensure_sources(
    root: Path,
    project_repository: Path | None,
    slug: str,
    destination_branch: str,
    contract: TargetContract,
    journal: dict[str, Any],
    path: Path,
) -> None:
    try:
        current = pin_sealed_refs(
            root,
            contract,
            slug=slug,
            destination_branch=destination_branch,
        )
    except ContractOperationError as exc:
        raise CompletionError(str(exc)) from exc
    if not journal["sources"]:
        journal["sources"] = current
        _write_journal(path, journal)
        return
    participants = {item.role: item for item in contract.participants}
    for role, source in journal["sources"].items():
        participant = participants[role]
        repository = _repository_for(root, project_repository, participant)
        _commit(repository, source)
        if not _is_ancestor(repository, participant.sealed_sha, source):
            raise CompletionError(
                f"pinned {role} source no longer descends from its sealed commit"
            )


def _finish_approval(
    tio: Any,
    slug: str,
    contract: TargetContract,
    journal: dict[str, Any],
    path: Path,
) -> None:
    expected = {item.role for item in contract.participants}
    if set(journal["published"]) != expected:
        raise CompletionError("cannot approve before every repository is published")
    entry = tio.find_ticket(slug)
    status = entry.get("status", "") if entry else ""
    if status == "review" and not _approve(tio, slug):
        raise CompletionError(
            "repository publication succeeded but the board transition failed; retry"
        )
    if status not in {"review", "done"}:
        raise CompletionError(
            f"repository publication succeeded but Ticket status is {status!r}; inspect and retry"
        )
    journal["state"] = "accepted"
    _write_journal(path, journal)


def _cleanup_all(
    root: Path,
    project_repository: Path | None,
    contract: TargetContract,
    journal: dict[str, Any],
    path: Path,
) -> None:
    if not journal["policy"]["cleanup"]:
        journal["state"] = "done"
        _write_journal(path, journal)
        return
    by_role = {item.role: item for item in contract.participants}
    roles = [role for role in ("project", "outer") if role in by_role]
    for role in roles:
        if role in journal["cleaned"]:
            continue
        participant = by_role[role]
        repository = _repository_for(root, project_repository, participant)
        _cleanup_participant(
            repository,
            participant,
            journal["sources"][role],
            journal["candidates"][role],
        )
        journal["cleaned"].append(role)
        journal["state"] = f"cleanup-{role}"
        _write_journal(path, journal)
    journal["state"] = "done"
    _write_journal(path, journal)


def _execute_completion(
    tio: Any,
    slug: str,
    entry: Mapping[str, Any],
    contract: TargetContract,
    path: Path,
    allowed_board_rename: tuple[Path, Path],
    *,
    cleanup: bool,
) -> None:
    journal = _load_journal(path, slug, contract, cleanup=cleanup)
    _write_journal(path, journal)
    if journal["state"] == "done":
        if entry.get("status") == "done":
            return
        raise CompletionError("acceptance journal is done but the Ticket is not")
    root = Path(tio._project_root).resolve()
    project_repository = resolve_inner_project_repo(root)
    destination_branch = entry.get("branch")
    if not isinstance(destination_branch, str) or not destination_branch:
        raise CompletionError("Ticket has no destination branch")
    publication_states = {
        "initializing",
        "prepared",
        "published-project",
        "published-outer",
    }
    if journal["state"] in publication_states:
        _ensure_sources(
            root,
            project_repository,
            slug,
            destination_branch,
            contract,
            journal,
            path,
        )
        _validate_source_surface(root, project_repository, contract, journal["sources"])
        _prepare_all(root, project_repository, slug, contract, journal, path)
        _publish_all(
            root,
            project_repository,
            contract,
            journal,
            path,
            allowed_board_rename,
        )
        _finish_approval(tio, slug, contract, journal, path)
    if journal["state"] != "done":
        _cleanup_all(root, project_repository, contract, journal, path)


def _assert_completion_slot(directory: Path, slug: str) -> None:
    """Serialize publication while an earlier Ticket still has public work pending."""
    for candidate in directory.glob("*.json"):
        if candidate.stem == slug:
            continue
        try:
            journal = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CompletionError(
                f"cannot inspect earlier acceptance journal {candidate}: {exc}"
            ) from exc
        if not isinstance(journal, dict):
            raise CompletionError(f"earlier acceptance journal is malformed: {candidate}")
        state = journal.get("state")
        if state not in {"accepted", "cleanup-project", "cleanup-outer", "done"}:
            ticket = journal.get("ticket", candidate.stem)
            raise CompletionError(
                f"Ticket {ticket!r} has unfinished repository publication; resume it first"
            )


def _journal_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return journal if isinstance(journal, dict) else None


def cleanup_finished(root: Path, slug: str) -> bool:
    """Return whether a journaled acceptance finished its configured cleanup."""
    journal = _journal_snapshot(_journal_path(root, slug))
    return bool(
        journal
        and journal.get("policy") == {"merge": True, "cleanup": True}
        and journal.get("state") == "done"
    )


def _cleanup_pending(path: Path) -> bool:
    journal = _journal_snapshot(path)
    return bool(
        journal
        and journal.get("policy") == {"merge": True, "cleanup": True}
        and journal.get("state")
        in {"published-outer", "accepted", "cleanup-project", "cleanup-outer"}
    )


def _validate_completion_plan(contract: TargetContract, *, cleanup: bool) -> None:
    if not cleanup:
        return
    for participant in contract.participants:
        if participant.ticket_ref == participant.destination_ref:
            raise CompletionError(
                f"cannot clean {participant.role} participant because its Ticket ref "
                "is also the destination ref"
            )


def complete_review_ticket(  # noqa: PLR0911 - fail-closed boundary checks
    tio: Any, slug: str, effective_policy: Any
) -> bool:
    """Prepare, publish, and approve one schema-3 review Ticket.

    The journal makes calls idempotent after an interruption.  A retry reuses
    pinned candidates, recognizes refs already published, and continues toward
    the review-to-done transition.
    """
    if getattr(effective_policy, "merge", None) is not True:
        raise CompletionError("journaled completion requires merge policy to be true")
    if not isinstance(getattr(effective_policy, "cleanup", None), bool):
        raise CompletionError("journaled completion requires cleanup policy to be boolean")
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return False
    status = entry.get("status", "")
    if status not in {"review", "done"}:
        print(
            f"Error: cannot complete '{slug}' from status '{status}'; must be in review",
            file=sys.stderr,
        )
        return False
    retired_errors = retired_ticket_field_errors(entry)
    if retired_errors:
        print(f"Error: cannot complete '{slug}': {retired_errors[0]}", file=sys.stderr)
        return False
    try:
        contract = TargetContract.from_mapping(entry.get("target_contract"))
    except TargetContractError as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
        return False
    try:
        _validate_completion_plan(contract, cleanup=effective_policy.cleanup)
    except CompletionError as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
        return False
    root = Path(tio._project_root).resolve()
    ticket_name = Path(str(entry["file"])).name
    allowed_board_rename = (
        tio.tickets_dir / "board" / "queue" / ticket_name,
        tio.tickets_dir / str(entry["file"]),
    )
    path = _journal_path(root, slug)
    lock_path = path.parent / ".lock"
    lock_path.touch(exist_ok=True)
    try:
        with (
            lock_path.open("a+", encoding="utf-8") as handle,
            nonblocking_file_lock(handle),
        ):
            _assert_completion_slot(path.parent, slug)
            _execute_completion(
                tio,
                slug,
                entry,
                contract,
                path,
                allowed_board_rename,
                cleanup=effective_policy.cleanup,
            )
    except LockContentionError:
        print("Error: another acceptance is already running", file=sys.stderr)
        return False
    except (CompletionError, OSError, ValueError) as exc:
        current = tio.find_ticket(slug)
        if current and current.get("status") == "done" and _cleanup_pending(path):
            print(f"Warning: accepted '{slug}' but cleanup is pending: {exc}", file=sys.stderr)
            return True
        print(f"Error: completion failed for '{slug}': {exc}", file=sys.stderr)
        return False
    return True
