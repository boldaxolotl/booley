"""Recoverable acceptance of recorded multi-repository Tickets.

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

from .acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
    assert_inputs_unchanged,
    load_acceptance_basis,
)
from .acceptance_journal import (
    AcceptanceJournal,
    AcceptanceJournalError,
    Candidate,
    JournalState,
    initial_journal,
    load_journal,
    load_persisted_journal,
)
from .contract_ops import ContractOperationError, pin_basis_refs
from .contract_path_policy import is_static_contract_path
from .frontmatter import parse_frontmatter
from .git_ops import worktree_is_clean
from .target_finalization import (
    TargetFinalizationError,
    apply_target_removals,
    plan_target_removals,
)
from .validation import retired_ticket_field_errors


class CompletionError(RuntimeError):
    """A recorded Ticket could not be prepared or published safely."""


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
    root: Path, project_repository: Path | None, participant: BasisParticipant
) -> Path:
    if participant.role == "outer":
        return root
    if participant.role == "project" and project_repository is not None:
        return project_repository
    raise CompletionError(f"recorded {participant.role} repository is unavailable")


def _journal_path(root: Path, slug: str) -> Path:
    directory = runtime_dir(root) / "acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{slug}.json"


def _write_journal(path: Path, journal: AcceptanceJournal) -> None:
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
    contract: AcceptanceBasis,
    *,
    cleanup: bool = False,
    removal_targets: tuple[str, ...] = (),
) -> AcceptanceJournal:
    return initial_journal(
        slug,
        [item.as_dict() for item in contract.participants],
        cleanup=cleanup,
        removal_targets=removal_targets,
    )


def _load_journal(
    path: Path,
    slug: str,
    contract: AcceptanceBasis,
    *,
    cleanup: bool = False,
    removal_targets: tuple[str, ...] = (),
) -> AcceptanceJournal:
    expected = [item.as_dict() for item in contract.participants]
    if not path.exists():
        return _initial_journal(slug, contract, cleanup=cleanup, removal_targets=removal_targets)
    try:
        return load_journal(
            path,
            slug,
            expected,
            cleanup=cleanup,
            removal_targets=removal_targets,
        )
    except AcceptanceJournalError as exc:
        raise CompletionError(str(exc)) from exc


def _changed_paths(repository: Path, before: str, after: str) -> set[str]:
    output = _require_git(repository, "diff", "--name-only", "-z", before, after)
    return {path for path in output.split("\0") if path}


def _validate_participant(
    repository: Path,
    participant: BasisParticipant,
    source: str,
    protected_paths: set[str],
) -> str:
    destination = _commit(repository, participant.destination_ref)
    if not _is_ancestor(repository, participant.authoring_sha, source):
        raise CompletionError(
            f"{participant.ticket_ref} no longer descends from recorded "
            f"{participant.role} commit {participant.authoring_sha}"
        )
    if not _is_ancestor(repository, participant.destination_sha, destination):
        raise CompletionError(
            f"{participant.destination_ref} rewrote the recorded destination history"
        )
    destination_changes = _changed_paths(repository, participant.destination_sha, destination)
    collisions = sorted(
        path
        for path in destination_changes
        if path in protected_paths or is_static_contract_path(path)
    )
    if collisions:
        raise CompletionError(
            f"{participant.destination_ref} changed recorded control path(s) also changed "
            f"by this Ticket: {', '.join(collisions)}"
        )
    return destination


@dataclass(frozen=True)
class _CandidatePlan:
    prepared_sha: str
    staging_ref: str
    expected_destination_sha: str
    source_repository: Path | None

    def journal_candidate(self) -> Candidate:
        return {
            "prepared_sha": self.prepared_sha,
            "finalized_sha": None,
            "staging_ref": self.staging_ref,
            "expected_destination_sha": self.expected_destination_sha,
        }


def _clone_checkout(repository: Path, destination: Path, commit: str) -> None:
    _require_git(
        repository,
        "clone",
        "--shared",
        "--no-checkout",
        str(repository),
        str(destination),
    )
    _copy_commit_identity(repository, destination)
    _require_git(destination, "checkout", "--detach", commit)


def _copy_commit_identity(repository: Path, destination: Path) -> None:
    """Preserve repository-local commit identity in an isolated shared clone."""
    for key in ("user.name", "user.email"):
        configured = _git(repository, "config", "--get", key)
        value = configured.stdout.strip()
        if configured.returncode == 0 and value:
            _require_git(destination, "config", key, value)


def _plan_candidate(
    repository: Path,
    participant: BasisParticipant,
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
            f"merge({slug}): recorded Ticket completed",
        )
        candidate = _commit(candidate_repository, "HEAD")
    return _CandidatePlan(candidate, staging_ref, destination, candidate_repository)


def _validate_source_surface(
    root: Path,
    project_repository: Path | None,
    contract: AcceptanceBasis,
    sources: Mapping[str, str],
) -> None:
    """Rebuild the recorded composite checkout and reject contract-control drift."""
    participants = {item.role: item for item in contract.participants}
    with tempfile.TemporaryDirectory(prefix="booley-accept-surface-") as directory:
        temporary = Path(directory) / "outer"
        _clone_checkout(root, temporary, sources["outer"])
        project = participants.get("project")
        if project is not None:
            if project_repository is None:
                raise CompletionError("recorded project repository is unavailable")
            try:
                project_relative = checkout_project_dir_relative_to(root)
            except (FileNotFoundError, ValueError) as exc:
                raise CompletionError(str(exc)) from exc
            project_checkout = temporary / project_relative
            _clone_checkout(project_repository, project_checkout, sources["project"])
        try:
            assert_inputs_unchanged(contract, temporary)
        except AcceptanceBasisError as exc:
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


def _direct_ref_identity(repository: Path, ref: str) -> str | None:
    """Return the object ID stored in a direct ref without peeling it."""
    symbolic = _git(repository, "symbolic-ref", "--quiet", ref)
    if symbolic.returncode == 0:
        raise CompletionError(f"acceptance ref {ref} is symbolic; expected an exact direct ref")
    if symbolic.returncode != 1:
        detail = (symbolic.stderr or symbolic.stdout).strip()
        raise CompletionError(f"could not inspect ref type for {ref}: {detail}")
    exists = _git(repository, "show-ref", "--verify", "--quiet", ref)
    if exists.returncode == 1:
        return None
    if exists.returncode != 0:
        detail = (exists.stderr or exists.stdout).strip()
        raise CompletionError(f"could not inspect {ref} in {repository}: {detail}")
    return _require_git(repository, "rev-parse", "--verify", ref)


def _delete_ref_at(repository: Path, ref: str, expected: str) -> None:
    """Delete one ref only while it points at its journaled identity."""
    current = _direct_ref_identity(repository, ref)
    if current is None:
        return
    if current != expected:
        raise CompletionError(f"refusing to delete {ref}: expected {expected}, found {current}")
    result = _git(repository, "update-ref", "--no-deref", "-d", ref, expected)
    if result.returncode == 0 or _direct_ref_identity(repository, ref) is None:
        return
    detail = (result.stderr or result.stdout).strip()
    raise CompletionError(f"could not delete {ref} at {expected}: {detail}")


def _validate_ref_at(repository: Path, ref: str, expected: str) -> None:
    current = _direct_ref_identity(repository, ref)
    if current is not None and current != expected:
        raise CompletionError(f"refusing to delete {ref}: expected {expected}, found {current}")


def _validate_ticket_worktree(
    repository: Path, participant: BasisParticipant, source: str
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
    participant: BasisParticipant,
    source: str,
    candidate: Candidate,
) -> None:
    if participant.ticket_ref == participant.destination_ref:
        raise CompletionError(
            f"refusing cleanup because {participant.ticket_ref} is also the destination ref"
        )
    _validate_ticket_worktree(repository, participant, source)
    _validate_ref_at(repository, participant.ticket_ref, source)
    finalized = _required_finalized_sha(candidate)
    _validate_ref_at(repository, candidate["staging_ref"], finalized)


def _remove_ticket_worktree(repository: Path, participant: BasisParticipant, source: str) -> None:
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
    participant: BasisParticipant,
    source: str,
    candidate: Candidate,
) -> None:
    _validate_cleanup_participant(repository, participant, source, candidate)
    _remove_ticket_worktree(repository, participant, source)
    _delete_ref_at(repository, participant.ticket_ref, source)
    _delete_ref_at(
        repository,
        candidate["staging_ref"],
        _required_finalized_sha(candidate),
    )


def _publish_candidate(
    repository: Path,
    participant: BasisParticipant,
    candidate: Candidate,
    allowed_board_rename: tuple[Path, Path],
) -> None:
    desired = _required_finalized_sha(candidate)
    staging_ref = candidate["staging_ref"]
    staging = _direct_ref_identity(repository, staging_ref)
    if staging != desired:
        raise CompletionError(
            f"acceptance staging ref {staging_ref} has identity {staging or 'absent'}; "
            f"expected finalized {desired}"
        )
    current = _commit(repository, participant.destination_ref)
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
        _require_git(checkout, "merge", "--ff-only", desired)
        return
    _require_git(
        repository,
        "update-ref",
        participant.destination_ref,
        desired,
        expected,
    )


def _protected_paths(
    contract: AcceptanceBasis, participant: BasisParticipant, project_prefix: str
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
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
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
            plan.prepared_sha,
        )
    _commit(repository, plan.prepared_sha)


def _required_prepared_sha(candidate: Candidate) -> str:
    prepared = candidate["prepared_sha"]
    assert prepared is not None, "prepared acceptance candidate must retain its identity"
    return prepared


def _required_finalized_sha(candidate: Candidate) -> str:
    finalized = candidate["finalized_sha"]
    assert finalized is not None, "published acceptance candidate must be finalized"
    return finalized


def _cas_ref(repository: Path, ref: str, desired: str, expected: str | None) -> None:
    current = _direct_ref_identity(repository, ref)
    if current == desired:
        return
    if current != expected:
        raise CompletionError(
            f"acceptance ref {ref} changed during reconciliation: "
            f"expected {expected or 'absent'}, found {current or 'absent'}"
        )
    old = expected if expected is not None else "0" * len(desired)
    result = _git(repository, "update-ref", "--no-deref", ref, desired, old)
    if result.returncode == 0 or _direct_ref_identity(repository, ref) == desired:
        return
    current = _direct_ref_identity(repository, ref)
    detail = (result.stderr or result.stdout).strip()
    raise CompletionError(
        f"acceptance ref {ref} changed during reconciliation: "
        f"expected {expected or 'absent'}, found {current or 'absent'}: {detail}"
    )


def _finalized_keepalive_ref(journal: AcceptanceJournal, role: str) -> str:
    return f"refs/booley/acceptance/{journal['transaction']}/finalized-{role}"


@dataclass(frozen=True)
class _RefReconciliation:
    repository: Path
    ref: str
    desired: str
    allowed: frozenset[str | None]
    expectation: str


def _reconcile_refs(plans: list[_RefReconciliation]) -> None:
    """Validate every ref identity before applying any compare-and-swap update."""
    states: list[tuple[_RefReconciliation, str | None]] = []
    for plan in plans:
        current = _direct_ref_identity(plan.repository, plan.ref)
        if current not in plan.allowed:
            raise CompletionError(
                f"acceptance ref {plan.ref} has unknown identity {current}; {plan.expectation}"
            )
        states.append((plan, current))
    for plan, current in states:
        if current != plan.desired:
            _cas_ref(plan.repository, plan.ref, plan.desired, current)


def _protect_finalized_candidates(
    root: Path,
    project_repository: Path | None,
    participants: Mapping[str, BasisParticipant],
    journal: AcceptanceJournal,
) -> None:
    plans: list[_RefReconciliation] = []
    for role, candidate in journal["candidates"].items():
        repository = _repository_for(root, project_repository, participants[role])
        finalized = _required_finalized_sha(candidate)
        ref = _finalized_keepalive_ref(journal, role)
        plans.append(
            _RefReconciliation(
                repository,
                ref,
                finalized,
                frozenset({None, finalized}),
                f"expected absent or finalized {finalized}",
            )
        )
    _reconcile_refs(plans)


def _reject_unjournaled_keepalives(
    root: Path,
    project_repository: Path | None,
    participants: Mapping[str, BasisParticipant],
    journal: AcceptanceJournal,
) -> None:
    for role, participant in participants.items():
        repository = _repository_for(root, project_repository, participant)
        ref = _finalized_keepalive_ref(journal, role)
        current = _direct_ref_identity(repository, ref)
        if current is not None:
            raise CompletionError(
                f"acceptance keepalive {ref} records unjournaled identity {current}"
            )


def _reconcile_prepared_refs(
    root: Path,
    project_repository: Path | None,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
) -> None:
    by_role = {item.role: item for item in contract.participants}
    plans: list[_RefReconciliation] = []
    for role, candidate in journal["candidates"].items():
        repository = _repository_for(root, project_repository, by_role[role])
        prepared = _required_prepared_sha(candidate)
        ref = candidate["staging_ref"]
        plans.append(
            _RefReconciliation(
                repository,
                ref,
                prepared,
                frozenset({None, prepared}),
                f"expected absent or prepared {prepared}",
            )
        )
    _reconcile_refs(plans)


def _legacy_prepared_identity(
    repository: Path,
    candidate: Candidate,
    ticket: str,
) -> str | None:
    """Recover the parent recorded implicitly by a legacy finalization commit."""
    finalized = _required_finalized_sha(candidate)
    current = _direct_ref_identity(repository, candidate["staging_ref"])
    if current is None or current == finalized:
        return None
    metadata = _require_git(repository, "show", "-s", "--format=%P%n%s", finalized).splitlines()
    parents = metadata[0].split()
    subject = metadata[1] if len(metadata) > 1 else ""
    expected_subject = f"chore({ticket}): remove completed Ticket Targets"
    if parents == [current] and subject == expected_subject:
        return current
    raise CompletionError(
        f"acceptance ref {candidate['staging_ref']} has unknown legacy identity {current}; "
        f"expected absent, the exact parent of finalized {finalized}, or the finalized candidate"
    )


def _reconcile_finalized_refs(
    root: Path,
    project_repository: Path | None,
    participants: Mapping[str, BasisParticipant],
    journal: AcceptanceJournal,
    journal_path: Path,
) -> None:
    plans: list[_RefReconciliation] = []
    recovered: dict[str, str] = {}
    for role, candidate in journal["candidates"].items():
        repository = _repository_for(root, project_repository, participants[role])
        finalized = _required_finalized_sha(candidate)
        prepared = candidate["prepared_sha"]
        if prepared is None:
            prepared = _legacy_prepared_identity(repository, candidate, journal["ticket"])
            if prepared is not None:
                recovered[role] = prepared
        ref = candidate["staging_ref"]
        _commit(repository, finalized)
        allowed = {None, finalized}
        if prepared is not None:
            allowed.add(prepared)
        plans.append(
            _RefReconciliation(
                repository,
                ref,
                finalized,
                frozenset(allowed),
                "expected absent, its exact prepared candidate, or its finalized candidate",
            )
        )
    for role, prepared in recovered.items():
        journal["candidates"][role]["prepared_sha"] = prepared
    if recovered:
        _write_journal(journal_path, journal)
    _reconcile_refs(plans)


def _validate_ticket_refs(
    root: Path,
    project_repository: Path | None,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
) -> None:
    for participant in contract.participants:
        repository = _repository_for(root, project_repository, participant)
        expected = journal["sources"][participant.role]
        current = _direct_ref_identity(repository, participant.ticket_ref)
        if current == expected:
            continue
        raise CompletionError(
            f"Ticket ref {participant.ticket_ref} has identity {current or 'absent'}; "
            f"expected {expected}"
        )


def _persist_candidate_plans(
    root: Path,
    project_repository: Path | None,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
    path: Path,
    plans: dict[str, _CandidatePlan],
) -> None:
    by_role = {item.role: item for item in contract.participants}
    for role, plan in plans.items():
        repository = _repository_for(root, project_repository, by_role[role])
        _import_candidate(repository, plan)
        journal["candidates"][role] = plan.journal_candidate()
    if not journal["removal_targets"]:
        for candidate in journal["candidates"].values():
            candidate["finalized_sha"] = candidate["prepared_sha"]
    if plans:
        _write_journal(path, journal)
    if any(candidate["finalized_sha"] is None for candidate in journal["candidates"].values()):
        _reconcile_prepared_refs(root, project_repository, contract, journal)


def _prepare_all(
    root: Path,
    project_repository: Path | None,
    slug: str,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
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
        _persist_candidate_plans(root, project_repository, contract, journal, journal_path, plans)
    if not journal["published"]:
        journal["state"] = JournalState.PREPARED
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


def _add_finalization_worktrees(
    root: Path,
    temporary: Path,
    project_repository: Path | None,
    has_project: bool,
    journal: AcceptanceJournal,
) -> Path | None:
    _require_git(
        root,
        "worktree",
        "add",
        "--detach",
        str(temporary),
        journal["candidates"]["outer"]["staging_ref"],
    )
    if not has_project:
        return None
    if project_repository is None:
        raise CompletionError("recorded project repository is unavailable")
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
    return project_checkout


def _planned_finalization_paths(
    temporary: Path, contract: AcceptanceBasis, journal: AcceptanceJournal
) -> list[Path]:
    try:
        plan = plan_target_removals(
            temporary,
            journal["removal_targets"],
            contract.bindings,
        )
        return apply_target_removals(temporary, plan)
    except (TargetFinalizationError, OSError, ValueError) as exc:
        raise CompletionError(f"Target finalization failed: {exc}") from exc


def _partition_finalization_paths(
    temporary: Path,
    project_checkout: Path | None,
    changed: list[Path],
) -> tuple[list[Path], list[Path]]:
    if project_checkout is None:
        return [], changed
    project_prefix = project_checkout.relative_to(temporary)
    project_paths = [
        path.relative_to(project_prefix) for path in changed if path.is_relative_to(project_prefix)
    ]
    outer_paths = [path for path in changed if not path.is_relative_to(project_prefix)]
    return project_paths, outer_paths


def _commit_finalized_candidates(
    temporary: Path,
    project_checkout: Path | None,
    changed: list[Path],
    slug: str,
) -> dict[str, str]:
    project_paths, outer_paths = _partition_finalization_paths(
        temporary, project_checkout, changed
    )
    finalized: dict[str, str] = {}
    if project_checkout is not None:
        finalized["project"] = _commit_finalized_paths(project_checkout, project_paths, slug)
    finalized["outer"] = _commit_finalized_paths(temporary, outer_paths, slug)
    return finalized


def _update_finalized_refs(
    root: Path,
    project_repository: Path | None,
    participants: Mapping[str, BasisParticipant],
    journal: AcceptanceJournal,
    journal_path: Path,
) -> None:
    _reconcile_finalized_refs(root, project_repository, participants, journal, journal_path)


def _remove_finalization_worktrees(
    root: Path,
    temporary: Path,
    project_repository: Path | None,
    project_checkout: Path | None,
) -> None:
    if project_checkout is not None and project_repository is not None:
        _git(project_repository, "worktree", "remove", "--force", str(project_checkout))
    _git(root, "worktree", "remove", "--force", str(temporary))
    if temporary.exists():
        temporary.rmdir()


def _finalization_directory(root: Path, journal: AcceptanceJournal) -> Path:
    return runtime_dir(root) / "acceptance-worktrees" / str(journal["transaction"])


def _retained_project_checkout(root: Path, temporary: Path, has_project: bool) -> Path | None:
    if not has_project:
        return None
    try:
        return temporary / checkout_project_dir_relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise CompletionError(str(exc)) from exc


def _finalize_all(
    root: Path,
    project_repository: Path | None,
    slug: str,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
    journal_path: Path,
) -> None:
    """Apply removals to a composite candidate before either ref is published."""
    by_role = {item.role: item for item in contract.participants}
    temporary = _finalization_directory(root, journal)
    project_checkout = _retained_project_checkout(root, temporary, "project" in by_role)
    if journal["candidates"] and all(
        candidate["finalized_sha"] is not None for candidate in journal["candidates"].values()
    ):
        _protect_finalized_candidates(root, project_repository, by_role, journal)
        _remove_finalization_worktrees(root, temporary, project_repository, project_checkout)
        return
    _reject_unjournaled_keepalives(root, project_repository, by_role, journal)
    _remove_finalization_worktrees(root, temporary, project_repository, project_checkout)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    journaled = False
    protected = False
    try:
        project_checkout = _add_finalization_worktrees(
            root, temporary, project_repository, "project" in by_role, journal
        )
        changed = _planned_finalization_paths(temporary, contract, journal)
        finalized = _commit_finalized_candidates(temporary, project_checkout, changed, slug)
        for role, sha in finalized.items():
            journal["candidates"][role]["finalized_sha"] = sha
        journaled = True
        _write_journal(journal_path, journal)
        _protect_finalized_candidates(root, project_repository, by_role, journal)
        protected = True
    finally:
        if not journaled or protected:
            _remove_finalization_worktrees(root, temporary, project_repository, project_checkout)


def _publish_all(
    root: Path,
    project_repository: Path | None,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
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
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
    path: Path,
) -> None:
    try:
        current = pin_basis_refs(
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
        if not _is_ancestor(repository, participant.authoring_sha, source):
            raise CompletionError(
                f"pinned {role} source no longer descends from its recorded commit"
            )


def _finish_approval(
    tio: Any,
    slug: str,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
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


def _retire_finalized_keepalives(
    root: Path,
    project_repository: Path | None,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
) -> None:
    by_role = {item.role: item for item in contract.participants}
    refs: list[tuple[Path, str, str]] = []
    for role, participant in by_role.items():
        repository = _repository_for(root, project_repository, participant)
        finalized = _required_finalized_sha(journal["candidates"][role])
        destination = _commit(repository, participant.destination_ref)
        if not _is_ancestor(repository, finalized, destination):
            raise CompletionError(
                f"cannot retire finalization keepalive before {finalized} is contained in "
                f"{participant.destination_ref}"
            )
        ref = _finalized_keepalive_ref(journal, role)
        current = _direct_ref_identity(repository, ref)
        if current not in {None, finalized}:
            raise CompletionError(
                f"acceptance keepalive {ref} has unknown identity {current}; "
                f"expected finalized {finalized}"
            )
        refs.append((repository, ref, finalized))
    for repository, ref, finalized in refs:
        _delete_ref_at(repository, ref, finalized)


def _cleanup_all(
    root: Path,
    project_repository: Path | None,
    contract: AcceptanceBasis,
    journal: AcceptanceJournal,
    path: Path,
) -> None:
    if not journal["policy"]["cleanup"]:
        _reconcile_finalized_refs(
            root,
            project_repository,
            {item.role: item for item in contract.participants},
            journal,
            path,
        )
        _validate_ticket_refs(root, project_repository, contract, journal)
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


@dataclass(frozen=True)
class _AcceptanceTransaction:
    root: Path
    project_repository: Path | None
    slug: str
    contract: AcceptanceBasis
    journal: AcceptanceJournal
    path: Path

    @property
    def participants(self) -> dict[str, BasisParticipant]:
        return {item.role: item for item in self.contract.participants}


def _prepare_pending_publication(
    transaction: _AcceptanceTransaction, destination_branch: str, *, cleanup: bool
) -> None:
    _ensure_sources(
        transaction.root,
        transaction.project_repository,
        transaction.slug,
        destination_branch,
        transaction.contract,
        transaction.journal,
        transaction.path,
    )
    _validate_source_surface(
        transaction.root,
        transaction.project_repository,
        transaction.contract,
        transaction.journal["sources"],
    )
    _prepare_all(
        transaction.root,
        transaction.project_repository,
        transaction.slug,
        transaction.contract,
        transaction.journal,
        transaction.path,
    )
    _finalize_all(
        transaction.root,
        transaction.project_repository,
        transaction.slug,
        transaction.contract,
        transaction.journal,
        transaction.path,
    )
    _update_finalized_refs(
        transaction.root,
        transaction.project_repository,
        transaction.participants,
        transaction.journal,
        transaction.path,
    )
    if not cleanup:
        _validate_ticket_refs(
            transaction.root,
            transaction.project_repository,
            transaction.contract,
            transaction.journal,
        )


def _publish_pending_candidates(
    transaction: _AcceptanceTransaction,
    tio: Any,
    allowed_board_rename: tuple[Path, Path],
) -> None:
    _publish_all(
        transaction.root,
        transaction.project_repository,
        transaction.contract,
        transaction.journal,
        transaction.path,
        allowed_board_rename,
    )
    _update_finalized_refs(
        transaction.root,
        transaction.project_repository,
        transaction.participants,
        transaction.journal,
        transaction.path,
    )
    _finish_approval(
        tio,
        transaction.slug,
        transaction.contract,
        transaction.journal,
        transaction.path,
    )


def _destination_branch(entry: Mapping[str, Any]) -> str:
    branch = entry.get("branch")
    if not isinstance(branch, str) or not branch:
        raise CompletionError("Ticket has no destination branch")
    return branch


def _execute_completion(
    tio: Any,
    slug: str,
    entry: Mapping[str, Any],
    contract: AcceptanceBasis,
    path: Path,
    allowed_board_rename: tuple[Path, Path],
    cleanup: bool,
    removal_targets: tuple[str, ...],
) -> None:
    journal = _load_journal(
        path,
        slug,
        contract,
        cleanup=cleanup,
        removal_targets=removal_targets,
    )
    _write_journal(path, journal)
    state = JournalState(journal["state"])
    if state is JournalState.DONE:
        if entry.get("status") == "done":
            return
        raise CompletionError("acceptance journal is done but the Ticket is not")
    root = Path(tio._project_root).resolve()
    project_repository = resolve_inner_project_repo(root)
    transaction = _AcceptanceTransaction(root, project_repository, slug, contract, journal, path)
    destination_branch = _destination_branch(entry)
    if state.publication_pending:
        _prepare_pending_publication(
            transaction,
            destination_branch,
            cleanup=cleanup,
        )
        _publish_pending_candidates(
            transaction,
            tio,
            allowed_board_rename,
        )
    if JournalState(journal["state"]) is not JournalState.DONE:
        _retire_finalized_keepalives(root, project_repository, contract, journal)
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


def _validate_completion_plan(contract: AcceptanceBasis, *, cleanup: bool) -> None:
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
) -> tuple[Mapping[str, Any], AcceptanceBasis] | None:
    if getattr(effective_policy, "merge", None) is not True:
        raise CompletionError("journaled completion requires merge policy to be true")
    if not isinstance(getattr(effective_policy, "cleanup", None), bool):
        raise CompletionError("journaled completion requires cleanup policy to be boolean")
    removal_targets = tuple(getattr(effective_policy, "remove_targets", ()))
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
        ticket_path = Path(tio.tickets_dir) / str(entry["file"])
        fields, body = parse_frontmatter(ticket_path.read_text(encoding="utf-8"))
        contract = load_acceptance_basis(tio._project_root, slug, fields, body)
        if removal_targets != contract.removal_targets:
            raise AcceptanceBasisError(
                "on_success.remove_targets changed after Acceptance Basis publication"
            )
    except AcceptanceBasisError as exc:
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
    contract: AcceptanceBasis,
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
            removal_targets=tuple(getattr(effective_policy, "remove_targets", ())),
        )


def _report_completion_failure(tio: Any, slug: str, path: Path, exc: Exception) -> bool:
    current = tio.find_ticket(slug)
    if current and current.get("status") == "done" and _cleanup_pending(path):
        print(f"Warning: accepted '{slug}' but cleanup is pending: {exc}", file=sys.stderr)
        return True
    print(f"Error: completion failed for '{slug}': {exc}", file=sys.stderr)
    return False


def complete_review_ticket(tio: Any, slug: str, effective_policy: Any) -> bool:
    """Prepare, publish, and approve one recorded review Ticket.

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
