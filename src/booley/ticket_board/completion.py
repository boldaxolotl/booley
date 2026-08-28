"""Recoverable acceptance of sealed multi-repository Tickets.

Acceptance is deliberately expressed as one transaction-like operation.  Git
cannot atomically update refs in two repositories, so every merge candidate is
prepared before either destination moves and a durable journal records the
subsequent roll-forward publication.
"""

from __future__ import annotations

import hashlib
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
from .target_finalization import (
    TargetFinalizationError,
    apply_target_removals,
    plan_target_removals,
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


def _removal_digest(removal_targets: tuple[str, ...]) -> str:
    payload = json.dumps(list(removal_targets), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _initial_journal(
    slug: str,
    contract: TargetContract,
    removal_targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "schema": 2,
        "transaction": uuid.uuid4().hex,
        "ticket": slug,
        "state": "initializing",
        "participants": [item.as_dict() for item in contract.participants],
        "sources": {},
        "candidates": {},
        "published": [],
        "removal_targets": list(removal_targets),
        "removal_digest": _removal_digest(removal_targets),
        "finalized": not removal_targets,
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
    sources: dict[str, str],
    candidates: dict[str, dict[str, str]],
    published: list[Any],
    *,
    finalized: bool,
    has_removals: bool,
) -> None:
    order = [role for role in ("project", "outer") if role in roles]
    if published != order[: len(published)]:
        raise BoundaryError("acceptance journal published roles are out of order")
    if candidates and not sources:
        raise BoundaryError("acceptance journal candidates require pinned sources")
    if sources and set(sources) != roles:
        raise BoundaryError("acceptance journal sources must pin every participant")
    if set(candidates) - set(sources) or set(published) - set(candidates):
        raise BoundaryError("acceptance journal checkpoints are inconsistent")
    if state == "initializing" and published:
        raise BoundaryError("initializing acceptance journal cannot contain publications")
    if state != "initializing" and set(candidates) != roles:
        raise BoundaryError(f"acceptance journal state {state!r} requires every candidate")
    expected_published = {
        "prepared": [],
        "published-project": ["project"],
        "published-outer": order,
        "done": order,
    }
    if state in expected_published and published != expected_published[state]:
        raise BoundaryError(f"acceptance journal state {state!r} conflicts with published roles")
    if published and not finalized:
        raise BoundaryError("acceptance journal cannot publish unfinalized candidates")
    if not has_removals and not finalized:
        raise BoundaryError("acceptance journal without removals must already be finalized")


def _validated_journal(  # noqa: PLR0912 - schema migration and recovery validation are ordered
    value: Any,
    slug: str,
    participants: list[dict[str, str]],
    removal_targets: tuple[str, ...],
) -> dict[str, Any]:
    journal = require_dict(value, field="acceptance journal")
    legacy_fields = {
        "schema",
        "transaction",
        "ticket",
        "state",
        "participants",
        "sources",
        "candidates",
        "published",
    }
    if require_str(journal, "ticket") != slug:
        raise BoundaryError(f"acceptance journal does not belong to Ticket {slug!r}")
    if journal.get("participants") != participants:
        raise BoundaryError("sealed repository participants changed after acceptance began")
    schema = journal.get("schema")
    if schema == 1:
        if removal_targets:
            raise BoundaryError("acceptance journal removal policy changed after acceptance began")
        if set(journal) != legacy_fields:
            raise BoundaryError("acceptance journal has invalid fields")
        finalized = True
    elif schema == 2:
        expected_fields = legacy_fields | {
            "removal_targets",
            "removal_digest",
            "finalized",
        }
        if set(journal) != expected_fields:
            raise BoundaryError("acceptance journal has invalid fields")
        persisted_targets = journal.get("removal_targets")
        if persisted_targets != list(removal_targets):
            raise BoundaryError("acceptance journal removal policy changed after acceptance began")
        if journal.get("removal_digest") != _removal_digest(removal_targets):
            raise BoundaryError("acceptance journal removal digest is invalid")
        finalized = journal.get("finalized")
        if not isinstance(finalized, bool):
            raise BoundaryError("acceptance journal finalized must be true or false")
    else:
        raise BoundaryError("acceptance journal schema must be 1 or 2")
    transaction = require_str(journal, "transaction")
    if not re.fullmatch(r"[0-9a-f]{32}", transaction):
        raise BoundaryError("acceptance journal transaction is invalid")
    state = require_str(journal, "state")
    if state not in {"initializing", "prepared", "published-project", "published-outer", "done"}:
        raise BoundaryError(f"acceptance journal state {state!r} is invalid")
    roles = {item["role"] for item in participants}
    sources = _validated_string_map(journal.get("sources"), "acceptance journal sources", roles)
    candidates = _validated_candidates(journal.get("candidates"), roles, transaction)
    published = require_list(journal.get("published"), field="acceptance journal published")
    _validate_journal_progress(
        state,
        roles,
        sources,
        candidates,
        published,
        finalized=finalized,
        has_removals=bool(removal_targets),
    )
    return {
        "schema": 2,
        "transaction": transaction,
        "ticket": slug,
        "state": state,
        "participants": participants,
        "sources": sources,
        "candidates": candidates,
        "published": published,
        "removal_targets": list(removal_targets),
        "removal_digest": _removal_digest(removal_targets),
        "finalized": finalized,
    }


def _load_journal(
    path: Path,
    slug: str,
    contract: TargetContract,
    removal_targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    expected = [item.as_dict() for item in contract.participants]
    if not path.exists():
        return _initial_journal(slug, contract, removal_targets)
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompletionError(f"acceptance journal is unreadable: {path}: {exc}") from exc
    try:
        return _validated_journal(journal, slug, expected, removal_targets)
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
            candidate = candidates[participant.role]
            actual = _commit(repository, candidate["staging_ref"])
            if actual != candidate["sha"]:
                if not journal["finalized"]:
                    raise CompletionError(
                        f"acceptance staging ref for {participant.role} no longer matches "
                        "its journal"
                    )
                _commit(repository, candidate["sha"])
                _require_git(
                    repository,
                    "update-ref",
                    candidate["staging_ref"],
                    candidate["sha"],
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
    journal["state"] = "prepared"
    _write_journal(journal_path, journal)


def _commit_finalized_paths(
    checkout: Path,
    paths: list[Path],
    slug: str,
) -> str:
    if not paths:
        return _commit(checkout, "HEAD")
    names = [path.as_posix() for path in paths]
    _require_git(checkout, "add", "--", *names)
    _require_git(
        checkout,
        "commit",
        "-m",
        f"chore({slug}): remove completed Ticket Targets",
    )
    return _commit(checkout, "HEAD")


def _finalize_all(  # noqa: PLR0912, PLR0915 - ordered multi-repository transaction
    root: Path,
    project_repository: Path | None,
    slug: str,
    contract: TargetContract,
    journal: dict[str, Any],
    journal_path: Path,
) -> None:
    """Apply removals to a composite candidate before either ref is published."""
    if journal["finalized"]:
        return
    temporary = Path(tempfile.mkdtemp(prefix="booley-accept-finalize-"))
    by_role = {item.role: item for item in contract.participants}
    project_checkout: Path | None = None
    try:
        _require_git(
            root,
            "worktree",
            "add",
            "--detach",
            str(temporary),
            journal["candidates"]["outer"]["staging_ref"],
        )
        if "project" in by_role:
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
                journal["candidates"]["project"]["staging_ref"],
            )
        try:
            removal_targets = tuple(journal["removal_targets"])
            plan = plan_target_removals(temporary, removal_targets, contract.bindings)
            changed = apply_target_removals(temporary, plan)
        except (TargetFinalizationError, OSError, ValueError) as exc:
            raise CompletionError(f"Target finalization failed: {exc}") from exc

        project_paths: list[Path] = []
        outer_paths: list[Path] = []
        if project_checkout is not None:
            project_prefix = project_checkout.relative_to(temporary)
            for path in changed:
                if path.is_relative_to(project_prefix):
                    project_paths.append(path.relative_to(project_prefix))
                else:
                    outer_paths.append(path)
        else:
            outer_paths.extend(changed)

        finalized: dict[str, str] = {}
        if project_checkout is not None:
            finalized["project"] = _commit_finalized_paths(
                project_checkout, project_paths, slug
            )
        finalized["outer"] = _commit_finalized_paths(temporary, outer_paths, slug)

        for role, sha in finalized.items():
            journal["candidates"][role]["sha"] = sha
        journal["finalized"] = True
        _write_journal(journal_path, journal)
        for role, sha in finalized.items():
            repository = _repository_for(root, project_repository, by_role[role])
            _require_git(
                repository,
                "update-ref",
                journal["candidates"][role]["staging_ref"],
                sha,
            )
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
    journal["state"] = "done"
    _write_journal(path, journal)


def _execute_completion(
    tio: Any,
    slug: str,
    entry: Mapping[str, Any],
    contract: TargetContract,
    path: Path,
    allowed_board_rename: tuple[Path, Path],
    removal_targets: tuple[str, ...],
) -> None:
    journal = _load_journal(path, slug, contract, removal_targets)
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
    _finalize_all(root, project_repository, slug, contract, journal, path)
    _publish_all(
        root,
        project_repository,
        contract,
        journal,
        path,
        allowed_board_rename,
    )
    if journal["state"] != "done":
        _finish_approval(tio, slug, contract, journal, path)


def _validate_removal_targets(
    contract: TargetContract, removal_targets: tuple[str, ...]
) -> None:
    bound_targets = {
        target
        for binding in contract.bindings
        for target in (binding.baseline, binding.candidate)
    }
    if (
        tuple(sorted(set(removal_targets))) != removal_targets
        or not set(removal_targets) <= bound_targets
    ):
        raise TargetContractError(
            "on_success.remove_targets must contain sorted canonical Targets bound by "
            "the sealed contract"
        )


def complete_review_ticket(tio: Any, slug: str, effective_policy: Any) -> bool:
    """Prepare, publish, and approve one schema-3 review Ticket.

    The journal makes calls idempotent after an interruption.  A retry reuses
    pinned candidates, recognizes refs already published, and continues toward
    the review-to-done transition.
    """
    if not effective_policy.merge:
        raise CompletionError("journaled completion requires merge policy")
    removal_targets = tuple(getattr(effective_policy, "remove_targets", ()))
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
    try:
        contract = TargetContract.from_mapping(entry.get("target_contract"))
        _validate_removal_targets(contract, removal_targets)
    except TargetContractError as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
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
            _execute_completion(
                tio,
                slug,
                entry,
                contract,
                path,
                allowed_board_rename,
                removal_targets,
            )
    except LockContentionError:
        print(f"Error: acceptance is already running for '{slug}'", file=sys.stderr)
        return False
    except (CompletionError, OSError, ValueError) as exc:
        print(f"Error: completion failed for '{slug}': {exc}", file=sys.stderr)
        return False
    return True
