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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.runtime.file_lock import LockContentionError, nonblocking_file_lock
from booley.runtime.project_dir import checkout_project_dir_relative_to, runtime_dir
from booley.runtime.ticket_repositories import resolve_inner_project_repo

from .acceptance_journal import (
    AcceptanceJournalError,
    JournalState,
    initial_journal,
    load_journal,
    load_persisted_journal,
)
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
    return initial_journal(
        slug,
        [item.as_dict() for item in contract.participants],
        cleanup=cleanup,
    )


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
        return load_journal(path, slug, expected, cleanup=cleanup)
    except AcceptanceJournalError as exc:
        raise CompletionError(str(exc)) from exc


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


@dataclass(frozen=True)
class _CandidatePlan:
    details: dict[str, str]
    source_repository: Path | None


def _clone_checkout(repository: Path, destination: Path, commit: str) -> None:
    _require_git(
        repository,
        "clone",
        "--shared",
        "--no-checkout",
        str(repository),
        str(destination),
    )
    _require_git(destination, "checkout", "--detach", commit)


def _plan_candidate(
    repository: Path,
    participant: ContractParticipant,
    source: str,
    transaction: str,
    slug: str,
    protected_paths: set[str],
    plan_directory: Path,
) -> _CandidatePlan:
    destination = _validate_participant(repository, participant, source, protected_paths)
    staging_ref = f"refs/booley/acceptance/{transaction}/{participant.role}"
    if _is_ancestor(repository, source, destination):
        candidate = destination
        candidate_repository = None
    else:
        candidate_repository = plan_directory / participant.role
        _clone_checkout(repository, candidate_repository, destination)
        _require_git(
            candidate_repository,
            "merge",
            "--no-ff",
            source,
            "-m",
            f"merge({slug}): sealed Ticket completed",
        )
        candidate = _commit(candidate_repository, "HEAD")
    details = {
        "sha": candidate,
        "staging_ref": staging_ref,
        "expected_destination_sha": destination,
    }
    return _CandidatePlan(details, candidate_repository)


def _validate_source_surface(
    root: Path,
    project_repository: Path | None,
    contract: TargetContract,
    sources: Mapping[str, str],
) -> None:
    """Rebuild the sealed composite checkout and reject contract-control drift."""
    participants = {item.role: item for item in contract.participants}
    with tempfile.TemporaryDirectory(prefix="booley-accept-surface-") as directory:
        temporary = Path(directory) / "outer"
        _clone_checkout(root, temporary, sources["outer"])
        project = participants.get("project")
        if project is not None:
            if project_repository is None:
                raise CompletionError("sealed project repository is unavailable")
            try:
                project_relative = checkout_project_dir_relative_to(root)
            except (FileNotFoundError, ValueError) as exc:
                raise CompletionError(str(exc)) from exc
            project_checkout = temporary / project_relative
            _clone_checkout(project_repository, project_checkout, sources["project"])
        try:
            verify_surface(contract, temporary)
        except TargetContractError as exc:
            raise CompletionError(str(exc)) from exc


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


def _validate_ref_at(repository: Path, ref: str, expected: str) -> None:
    current = _ref_commit(repository, ref)
    if current is not None and current != expected:
        raise CompletionError(f"refusing to delete {ref}: expected {expected}, found {current}")


def _validate_ticket_worktree(
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


def _validate_cleanup_participant(
    repository: Path,
    participant: ContractParticipant,
    source: str,
    candidate: Mapping[str, str],
) -> None:
    if participant.ticket_ref == participant.destination_ref:
        raise CompletionError(
            f"refusing cleanup because {participant.ticket_ref} is also the destination ref"
        )
    _validate_ticket_worktree(repository, participant, source)
    _validate_ref_at(repository, participant.ticket_ref, source)
    _validate_ref_at(repository, candidate["staging_ref"], candidate["sha"])


def _remove_ticket_worktree(
    repository: Path, participant: ContractParticipant, source: str
) -> None:
    _validate_ticket_worktree(repository, participant, source)
    checkout = _checked_out_at(repository, participant.ticket_ref)
    if checkout is None:
        return
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
    _validate_cleanup_participant(repository, participant, source, candidate)
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


def _protected_paths(
    contract: TargetContract, participant: ContractParticipant, project_prefix: str
) -> set[str]:
    has_project = any(item.role == "project" for item in contract.participants)
    if participant.role == "project":
        return {
            item.path.removeprefix(project_prefix)
            for item in contract.surface_entries
            if item.path.startswith(project_prefix)
        }
    if has_project:
        return {
            item.path
            for item in contract.surface_entries
            if not item.path.startswith(project_prefix)
        }
    return {item.path for item in contract.surface_entries}


def _plan_missing_candidates(
    root: Path,
    project_repository: Path | None,
    slug: str,
    contract: TargetContract,
    journal: dict[str, Any],
    plan_directory: Path,
    project_prefix: str,
) -> dict[str, _CandidatePlan]:
    plans: dict[str, _CandidatePlan] = {}
    for participant in contract.participants:
        if participant.role in journal["candidates"]:
            continue
        repository = _repository_for(root, project_repository, participant)
        plans[participant.role] = _plan_candidate(
            repository,
            participant,
            journal["sources"][participant.role],
            journal["transaction"],
            slug,
            _protected_paths(contract, participant, project_prefix),
            plan_directory,
        )
    return plans


def _import_candidate(repository: Path, plan: _CandidatePlan) -> None:
    if plan.source_repository is not None:
        _require_git(
            repository,
            "fetch",
            "--no-write-fetch-head",
            str(plan.source_repository),
            plan.details["sha"],
        )
    _commit(repository, plan.details["sha"])


def _install_staging_ref(repository: Path, candidate: Mapping[str, str]) -> None:
    current = _ref_commit(repository, candidate["staging_ref"])
    if current is None:
        _require_git(repository, "update-ref", candidate["staging_ref"], candidate["sha"])
    elif current != candidate["sha"]:
        raise CompletionError(
            f"acceptance staging ref moved from {candidate['sha']} to {current}"
        )


def _persist_candidate_plans(
    root: Path,
    project_repository: Path | None,
    contract: TargetContract,
    journal: dict[str, Any],
    path: Path,
    plans: dict[str, _CandidatePlan],
) -> None:
    by_role = {item.role: item for item in contract.participants}
    for role, plan in plans.items():
        repository = _repository_for(root, project_repository, by_role[role])
        _import_candidate(repository, plan)
        journal["candidates"][role] = plan.details
    if plans:
        _write_journal(path, journal)
    for role, candidate in journal["candidates"].items():
        repository = _repository_for(root, project_repository, by_role[role])
        _commit(repository, candidate["sha"])
        _install_staging_ref(repository, candidate)


def _prepare_all(
    root: Path,
    project_repository: Path | None,
    slug: str,
    contract: TargetContract,
    journal: dict[str, Any],
    journal_path: Path,
) -> None:
    try:
        project_prefix = checkout_project_dir_relative_to(root).as_posix().rstrip("/") + "/"
    except (FileNotFoundError, ValueError) as exc:
        raise CompletionError(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="booley-accept-plan-") as directory:
        plans = _plan_missing_candidates(
            root,
            project_repository,
            slug,
            contract,
            journal,
            Path(directory),
            project_prefix,
        )
        _persist_candidate_plans(
            root, project_repository, contract, journal, journal_path, plans
        )
    if not journal["published"]:
        journal["state"] = JournalState.PREPARED
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
        journal["state"] = JournalState(f"published-{role}")
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
    journal["state"] = JournalState.ACCEPTED
    _write_journal(path, journal)


def _cleanup_all(
    root: Path,
    project_repository: Path | None,
    contract: TargetContract,
    journal: dict[str, Any],
    path: Path,
) -> None:
    if not journal["policy"]["cleanup"]:
        journal["state"] = JournalState.DONE
        _write_journal(path, journal)
        return
    by_role = {item.role: item for item in contract.participants}
    roles = [role for role in ("project", "outer") if role in by_role]
    for role in roles:
        if role in journal["cleaned"]:
            continue
        participant = by_role[role]
        repository = _repository_for(root, project_repository, participant)
        _validate_cleanup_participant(
            repository,
            participant,
            journal["sources"][role],
            journal["candidates"][role],
        )
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
        journal["state"] = JournalState(f"cleanup-{role}")
        _write_journal(path, journal)
    journal["state"] = JournalState.DONE
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
    state = JournalState(journal["state"])
    if state is JournalState.DONE:
        if entry.get("status") == "done":
            return
        raise CompletionError("acceptance journal is done but the Ticket is not")
    root = Path(tio._project_root).resolve()
    project_repository = resolve_inner_project_repo(root)
    destination_branch = entry.get("branch")
    if not isinstance(destination_branch, str) or not destination_branch:
        raise CompletionError("Ticket has no destination branch")
    if state.publication_pending:
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
    if JournalState(journal["state"]) is not JournalState.DONE:
        _cleanup_all(root, project_repository, contract, journal, path)


def _assert_completion_slot(directory: Path, slug: str) -> None:
    """Serialize publication while an earlier Ticket still has public work pending."""
    for candidate in directory.glob("*.json"):
        if candidate.stem == slug:
            continue
        try:
            journal = load_persisted_journal(candidate)
        except AcceptanceJournalError as exc:
            raise CompletionError(
                f"cannot inspect earlier acceptance journal {candidate}: {exc}"
            ) from exc
        state = JournalState(journal["state"])
        if state.publication_pending:
            ticket = journal.get("ticket", candidate.stem)
            raise CompletionError(
                f"Ticket {ticket!r} has unfinished repository publication; resume it first"
            )


def cleanup_finished(root: Path, slug: str) -> bool:
    """Return whether a journaled acceptance finished its configured cleanup."""
    path = _journal_path(root, slug)
    if not path.exists():
        return False
    try:
        journal = load_persisted_journal(path)
    except AcceptanceJournalError as exc:
        raise CompletionError(str(exc)) from exc
    return bool(
        journal.get("policy") == {"merge": True, "cleanup": True}
        and JournalState(journal["state"]) is JournalState.DONE
    )


def _cleanup_pending(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        journal = load_persisted_journal(path)
    except AcceptanceJournalError as exc:
        raise CompletionError(str(exc)) from exc
    state = JournalState(journal["state"])
    return journal.get("policy") == {"merge": True, "cleanup": True} and state.cleanup_pending


def _validate_completion_plan(contract: TargetContract, *, cleanup: bool) -> None:
    if not cleanup:
        return
    for participant in contract.participants:
        if participant.ticket_ref == participant.destination_ref:
            raise CompletionError(
                f"cannot clean {participant.role} participant because its Ticket ref "
                "is also the destination ref"
            )


def _completion_inputs(
    tio: Any, slug: str, effective_policy: Any
) -> tuple[Mapping[str, Any], TargetContract] | None:
    if getattr(effective_policy, "merge", None) is not True:
        raise CompletionError("journaled completion requires merge policy to be true")
    if not isinstance(getattr(effective_policy, "cleanup", None), bool):
        raise CompletionError("journaled completion requires cleanup policy to be boolean")
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return None
    status = entry.get("status", "")
    if status not in {"review", "done"}:
        print(
            f"Error: cannot complete '{slug}' from status '{status}'; must be in review",
            file=sys.stderr,
        )
        return None
    retired_errors = retired_ticket_field_errors(entry)
    if retired_errors:
        print(f"Error: cannot complete '{slug}': {retired_errors[0]}", file=sys.stderr)
        return None
    try:
        contract = TargetContract.from_mapping(entry.get("target_contract"))
    except TargetContractError as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
        return None
    try:
        _validate_completion_plan(contract, cleanup=effective_policy.cleanup)
    except CompletionError as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
        return None
    return entry, contract


def _run_locked_completion(
    tio: Any,
    slug: str,
    entry: Mapping[str, Any],
    contract: TargetContract,
    effective_policy: Any,
    path: Path,
) -> None:
    ticket_name = Path(str(entry["file"])).name
    allowed_board_rename = (
        tio.tickets_dir / "board" / "queue" / ticket_name,
        tio.tickets_dir / str(entry["file"]),
    )
    lock_path = path.parent / ".lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
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


def _report_completion_failure(tio: Any, slug: str, path: Path, exc: Exception) -> bool:
    current = tio.find_ticket(slug)
    if current and current.get("status") == "done" and _cleanup_pending(path):
        print(f"Warning: accepted '{slug}' but cleanup is pending: {exc}", file=sys.stderr)
        return True
    print(f"Error: completion failed for '{slug}': {exc}", file=sys.stderr)
    return False


def complete_review_ticket(tio: Any, slug: str, effective_policy: Any) -> bool:
    """Prepare, publish, and approve one schema-3 review Ticket.

    The journal makes calls idempotent after an interruption. A retry reuses
    pinned candidates and continues toward the review-to-done transition.
    """
    inputs = _completion_inputs(tio, slug, effective_policy)
    if inputs is None:
        return False
    entry, contract = inputs
    path = _journal_path(Path(tio._project_root).resolve(), slug)
    try:
        _run_locked_completion(tio, slug, entry, contract, effective_policy, path)
    except LockContentionError:
        print("Error: another acceptance is already running", file=sys.stderr)
        return False
    except (CompletionError, OSError, ValueError) as exc:
        return _report_completion_failure(tio, slug, path, exc)
    return True
