"""Recoverable acceptance of recorded multi-repository Tickets.

Acceptance is deliberately expressed as one transaction-like operation.  Git
cannot atomically update refs in two repositories, so every merge candidate is
prepared before either destination moves and a durable journal records the
subsequent roll-forward publication.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from booley.runtime.project_dir import checkout_project_dir_relative_to, runtime_dir

from ..acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
    assert_inputs_unchanged,
)
from ..git_ops import worktree_is_clean
from ..target_finalization import (
    TargetFinalizationError,
    apply_target_removals,
    plan_target_removals,
)
from ..workspace_ops import AcceptanceBasisOperationError, pin_basis_refs
from ._model import (
    AcceptanceJournal,
    AcceptanceJournalError,
    Candidate,
    JournalState,
    initial_journal,
)
from ._repository import (
    AcceptanceRepositories,
    LocalAcceptanceRepositories,
    RepositoryBoundary,
)
from ._store import (
    AcceptanceCheckpoint as _Checkpoint,
)
from ._store import (
    AcceptanceStore,
    FileAcceptanceStore,
)


class AcceptanceOperationError(RuntimeError):
    """A recorded Ticket could not be prepared or published safely."""


class AcceptanceRecoveryBlockedError(AcceptanceOperationError):
    """A completed Ticket has repository state that cannot roll forward safely."""


class AcceptanceOutcome(StrEnum):
    """Progress returned to Ticket Board completion policy."""

    APPROVAL_REQUIRED = "approval-required"
    COMPLETE = "complete"
    ACCEPTED_PENDING = "accepted-pending"


@dataclass(frozen=True)
class AcceptanceProgress:
    """Observable result of advancing one Acceptance Journal."""

    outcome: AcceptanceOutcome
    pending_phase: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class AcceptanceRequest:
    """Normalized Ticket Board facts needed to advance acceptance."""

    root: Path
    slug: str
    basis: AcceptanceBasis
    cleanup: bool
    ticket_status: Literal["review", "done"]
    allowed_board_rename: tuple[Path, Path] | None
    expected_sources: Mapping[str, str] | None = None


def _git(
    repository: Path,
    *args: str,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceOperationError(
            f"git {' '.join(args)} failed in {repository}: {exc}"
        ) from exc


def _require_git(
    repository: Path,
    *args: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    result = _git(repository, *args, environment=environment)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AcceptanceOperationError(
            f"git {' '.join(args)} failed in {repository} (rc={result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _commit(repository: Path, ref: str) -> str:
    return _require_git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    result = _git(repository, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in {0, 1}:
        detail = (result.stderr or result.stdout).strip()
        raise AcceptanceOperationError(f"could not compare Git history in {repository}: {detail}")
    return result.returncode == 0


def _repository_for(
    root: Path, project_repository: Path | None, participant: BasisParticipant
) -> Path:
    if participant.role == "outer":
        return root
    if participant.role == "project" and project_repository is not None:
        return project_repository
    raise AcceptanceOperationError(f"recorded {participant.role} repository is unavailable")


def _initial_journal(
    slug: str,
    basis: AcceptanceBasis,
    *,
    cleanup: bool = False,
    removal_targets: tuple[str, ...] = (),
) -> AcceptanceJournal:
    return initial_journal(
        slug,
        [item.as_dict() for item in basis.participants],
        cleanup=cleanup,
        removal_targets=removal_targets,
    )


def _load_journal(
    path: Path,
    slug: str,
    basis: AcceptanceBasis,
    *,
    cleanup: bool = False,
    removal_targets: tuple[str, ...] = (),
    store: AcceptanceStore | None = None,
) -> AcceptanceJournal:
    store = store or FileAcceptanceStore()
    expected = [item.as_dict() for item in basis.participants]
    if not path.exists():
        return _initial_journal(slug, basis, cleanup=cleanup, removal_targets=removal_targets)
    try:
        return store.load(
            path,
            slug,
            expected,
            cleanup=cleanup,
            removal_targets=removal_targets,
        )
    except AcceptanceJournalError as exc:
        raise AcceptanceOperationError(str(exc)) from exc


def _validate_participant(
    repository: Path,
    participant: BasisParticipant,
    source: str,
) -> str:
    destination = _commit(repository, participant.destination_ref)
    if not _is_ancestor(repository, participant.authoring_sha, source):
        raise AcceptanceOperationError(
            f"{participant.ticket_ref} no longer descends from recorded "
            f"{participant.role} commit {participant.authoring_sha}"
        )
    if not _is_ancestor(repository, participant.destination_sha, destination):
        raise AcceptanceOperationError(
            f"{participant.destination_ref} rewrote the recorded destination history"
        )
    return destination


@dataclass(frozen=True)
class _CandidatePlan:
    prepared_sha: str
    staging_ref: str
    expected_destination_sha: str
    source_repository: Path | None

    def journal_candidate(self) -> Candidate:
        return Candidate(
            prepared_sha=self.prepared_sha,
            finalized_sha=None,
            staging_ref=self.staging_ref,
            expected_destination_sha=self.expected_destination_sha,
        )


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


def _candidate_commit_environment(repository: Path, *commits: str) -> dict[str, str]:
    latest = max(
        int(_require_git(repository, "show", "-s", "--format=%ct", commit)) for commit in commits
    )
    timestamp = datetime.fromtimestamp(latest + 1, UTC).strftime("%Y-%m-%dT%H:%M:%S +0000")
    environment = dict(os.environ)
    environment["GIT_AUTHOR_DATE"] = timestamp
    environment["GIT_COMMITTER_DATE"] = timestamp
    return environment


def _plan_candidate(
    repository: Path,
    participant: BasisParticipant,
    source: str,
    transaction: str,
    slug: str,
    plan_directory: Path,
) -> _CandidatePlan:
    destination = _validate_participant(repository, participant, source)
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
            environment=_candidate_commit_environment(
                candidate_repository,
                destination,
                source,
            ),
        )
        candidate = _commit(candidate_repository, "HEAD")
    return _CandidatePlan(candidate, staging_ref, destination, candidate_repository)


def _validate_source_surface(
    root: Path,
    project_repository: Path | None,
    basis: AcceptanceBasis,
    sources: Mapping[str, str],
) -> None:
    """Rebuild the recorded composite checkout and reject acceptance-input drift."""
    participants = {item.role: item for item in basis.participants}
    with tempfile.TemporaryDirectory(prefix="booley-accept-surface-") as directory:
        temporary = Path(directory) / "outer"
        _clone_checkout(root, temporary, sources["outer"])
        project = participants.get("project")
        if project is not None:
            if project_repository is None:
                raise AcceptanceOperationError("recorded project repository is unavailable")
            try:
                project_relative = checkout_project_dir_relative_to(root)
            except (FileNotFoundError, ValueError) as exc:
                raise AcceptanceOperationError(str(exc)) from exc
            project_checkout = temporary / project_relative
            _clone_checkout(project_repository, project_checkout, sources["project"])
        try:
            assert_inputs_unchanged(basis, temporary)
            from ..acceptance_targets import validate_binding_selectors

            selector_errors = validate_binding_selectors(temporary, basis.bindings)
            if selector_errors:
                raise AcceptanceBasisError(
                    "Acceptance Basis selectors changed: " + "; ".join(selector_errors)
                )
        except AcceptanceBasisError as exc:
            raise AcceptanceOperationError(str(exc)) from exc


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
    raise AcceptanceOperationError(f"could not inspect {ref} in {repository}: {detail}")


def _direct_ref_identity(repository: Path, ref: str) -> str | None:
    """Return the object ID stored in a direct ref without peeling it."""
    symbolic = _git(repository, "symbolic-ref", "--quiet", ref)
    if symbolic.returncode == 0:
        raise AcceptanceRecoveryBlockedError(
            f"acceptance ref {ref} is symbolic; expected an exact direct ref"
        )
    if symbolic.returncode != 1:
        detail = (symbolic.stderr or symbolic.stdout).strip()
        raise AcceptanceOperationError(f"could not inspect ref type for {ref}: {detail}")
    exists = _git(repository, "show-ref", "--verify", "--quiet", ref)
    if exists.returncode == 1:
        return None
    if exists.returncode != 0:
        detail = (exists.stderr or exists.stdout).strip()
        raise AcceptanceOperationError(f"could not inspect {ref} in {repository}: {detail}")
    return _require_git(repository, "rev-parse", "--verify", ref)


def _delete_ref_at(repository: Path, ref: str, expected: str) -> None:
    """Delete one ref only while it points at its journaled identity."""
    current = _direct_ref_identity(repository, ref)
    if current is None:
        return
    if current != expected:
        raise AcceptanceRecoveryBlockedError(
            f"refusing to delete {ref}: expected {expected}, found {current}"
        )
    result = _git(repository, "update-ref", "--no-deref", "-d", ref, expected)
    if result.returncode == 0 or _direct_ref_identity(repository, ref) is None:
        return
    detail = (result.stderr or result.stdout).strip()
    raise AcceptanceOperationError(f"could not delete {ref} at {expected}: {detail}")


def _validate_ref_at(repository: Path, ref: str, expected: str) -> None:
    current = _direct_ref_identity(repository, ref)
    if current is not None and current != expected:
        raise AcceptanceRecoveryBlockedError(
            f"refusing to delete {ref}: expected {expected}, found {current}"
        )


def _validate_ticket_worktree(
    repository: Path, participant: BasisParticipant, source: str
) -> None:
    checkout = _checked_out_at(repository, participant.ticket_ref)
    if checkout is None:
        return
    head = _commit(checkout, "HEAD")
    if head != source:
        raise AcceptanceOperationError(
            f"refusing to remove {checkout}: expected Ticket HEAD {source}, found {head}"
        )
    if not worktree_is_clean(str(checkout)):
        raise AcceptanceOperationError(f"refusing to remove dirty Ticket worktree {checkout}")


def _validate_cleanup_participant(
    repository: Path,
    participant: BasisParticipant,
    source: str,
    candidate: Candidate,
) -> None:
    if participant.ticket_ref == participant.destination_ref:
        raise AcceptanceOperationError(
            f"refusing cleanup because {participant.ticket_ref} is also the destination ref"
        )
    _validate_ticket_worktree(repository, participant, source)
    _validate_ref_at(repository, participant.ticket_ref, source)
    finalized = _required_finalized_sha(candidate)
    _validate_ref_at(repository, candidate.staging_ref, finalized)


def _remove_ticket_worktree(repository: Path, participant: BasisParticipant, source: str) -> None:
    _validate_ticket_worktree(repository, participant, source)
    checkout = _checked_out_at(repository, participant.ticket_ref)
    if checkout is None:
        return
    result = _git(repository, "worktree", "remove", str(checkout))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AcceptanceOperationError(f"could not remove Ticket worktree {checkout}: {detail}")


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
        candidate.staging_ref,
        _required_finalized_sha(candidate),
    )


def _publish_candidate(
    repository: Path,
    participant: BasisParticipant,
    candidate: Candidate,
    allowed_board_rename: tuple[Path, Path] | None,
) -> None:
    desired = _required_finalized_sha(candidate)
    staging_ref = candidate.staging_ref
    staging = _direct_ref_identity(repository, staging_ref)
    if staging != desired:
        raise AcceptanceOperationError(
            f"acceptance staging ref {staging_ref} has identity {staging or 'absent'}; "
            f"expected finalized {desired}"
        )
    current = _commit(repository, participant.destination_ref)
    if current == desired or _is_ancestor(repository, desired, current):
        return
    expected = candidate.expected_destination_sha
    if current != expected:
        raise AcceptanceOperationError(
            f"{participant.destination_ref} moved from {expected} to {current} "
            "after acceptance preparation; retry after inspecting the journal"
        )
    checkout = _checked_out_at(repository, participant.destination_ref)
    if checkout is not None:
        if not worktree_is_clean(str(checkout), allowed_unstaged_rename=allowed_board_rename):
            raise AcceptanceOperationError(
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


def _plan_missing_candidates(
    root: Path,
    project_repository: Path | None,
    slug: str,
    basis: AcceptanceBasis,
    journal: AcceptanceJournal,
    plan_directory: Path,
    repositories: AcceptanceRepositories,
) -> dict[str, _CandidatePlan]:
    plans: dict[str, _CandidatePlan] = {}
    for participant in basis.participants:
        if participant.role in journal["candidates"]:
            continue
        repository = _repository_for(root, project_repository, participant)
        plans[participant.role] = repositories.perform(
            RepositoryBoundary.PREPARATION,
            participant.role,
            lambda repository=repository, participant=participant: _plan_candidate(
                repository,
                participant,
                journal["sources"][participant.role],
                journal["transaction"],
                slug,
                plan_directory,
            ),
        )
    return plans


def _candidate_identity(
    role: str,
    journal: AcceptanceJournal,
    plans: Mapping[str, _CandidatePlan],
) -> str:
    if role in plans:
        return plans[role].prepared_sha
    prepared = journal.candidates[role].prepared_sha
    if prepared is None:
        raise AcceptanceOperationError(f"prepared {role} acceptance candidate has no identity")
    return prepared


def _candidate_repository(
    root: Path,
    project_repository: Path | None,
    role: str,
    plans: Mapping[str, _CandidatePlan],
) -> Path:
    plan = plans.get(role)
    if plan is not None and plan.source_repository is not None:
        return plan.source_repository
    if role == "outer":
        return root
    if project_repository is None:
        raise AcceptanceOperationError("recorded project repository is unavailable")
    return project_repository


def _validate_candidate_surface(
    transaction: _AcceptanceTransaction,
    plans: Mapping[str, _CandidatePlan],
    directory: Path,
) -> None:
    outer = directory / "candidate-surface"
    outer_source = _candidate_repository(
        transaction.root, transaction.project_repository, "outer", plans
    )
    _clone_checkout(
        outer_source,
        outer,
        _candidate_identity("outer", transaction.journal, plans),
    )
    if any(row.role == "project" for row in transaction.basis.participants):
        project_source = _candidate_repository(
            transaction.root, transaction.project_repository, "project", plans
        )
        project_path = outer / checkout_project_dir_relative_to(transaction.root)
        _clone_checkout(
            project_source,
            project_path,
            _candidate_identity("project", transaction.journal, plans),
        )
    try:
        assert_inputs_unchanged(transaction.basis, outer)
    except AcceptanceBasisError as exc:
        raise AcceptanceOperationError(str(exc)) from exc


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
    prepared = candidate.prepared_sha
    assert prepared is not None, "prepared acceptance candidate must retain its identity"
    return prepared


def _required_finalized_sha(candidate: Candidate) -> str:
    finalized = candidate.finalized_sha
    assert finalized is not None, "published acceptance candidate must be finalized"
    return finalized


def _cas_ref(repository: Path, ref: str, desired: str, expected: str | None) -> None:
    current = _direct_ref_identity(repository, ref)
    if current == desired:
        return
    if current != expected:
        raise AcceptanceOperationError(
            f"acceptance ref {ref} changed during reconciliation: "
            f"expected {expected or 'absent'}, found {current or 'absent'}"
        )
    old = expected if expected is not None else "0" * len(desired)
    result = _git(repository, "update-ref", "--no-deref", ref, desired, old)
    if result.returncode == 0 or _direct_ref_identity(repository, ref) == desired:
        return
    current = _direct_ref_identity(repository, ref)
    detail = (result.stderr or result.stdout).strip()
    raise AcceptanceOperationError(
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
            raise AcceptanceRecoveryBlockedError(
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
            raise AcceptanceOperationError(
                f"acceptance keepalive {ref} records unjournaled identity {current}"
            )


def _reconcile_prepared_refs(
    root: Path,
    project_repository: Path | None,
    basis: AcceptanceBasis,
    journal: AcceptanceJournal,
) -> None:
    by_role = {item.role: item for item in basis.participants}
    plans: list[_RefReconciliation] = []
    for role, candidate in journal["candidates"].items():
        repository = _repository_for(root, project_repository, by_role[role])
        prepared = candidate.prepared_sha
        if prepared is None:
            continue
        ref = candidate.staging_ref
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


def _reconcile_finalized_refs(
    transaction: _AcceptanceTransaction,
) -> None:
    plans: list[_RefReconciliation] = []
    journal = transaction.journal
    for role, candidate in journal["candidates"].items():
        repository = transaction.repository(transaction.participants[role])
        finalized = _required_finalized_sha(candidate)
        prepared = _required_prepared_sha(candidate)
        ref = candidate.staging_ref
        _commit(repository, finalized)
        plans.append(
            _RefReconciliation(
                repository,
                ref,
                finalized,
                frozenset({None, prepared, finalized}),
                "expected absent, its exact prepared candidate, or its finalized candidate",
            )
        )
    _reconcile_refs(plans)


def _validate_ticket_refs(
    root: Path,
    project_repository: Path | None,
    basis: AcceptanceBasis,
    journal: AcceptanceJournal,
) -> None:
    for participant in basis.participants:
        repository = _repository_for(root, project_repository, participant)
        expected = journal["sources"][participant.role]
        current = _direct_ref_identity(repository, participant.ticket_ref)
        if current == expected:
            continue
        raise AcceptanceRecoveryBlockedError(
            f"Ticket ref {participant.ticket_ref} has identity {current or 'absent'}; "
            f"expected {expected}"
        )


def _persist_candidate_plans(
    transaction: _AcceptanceTransaction,
    plans: dict[str, _CandidatePlan],
) -> None:
    journal = transaction.journal
    for role, plan in plans.items():
        repository = transaction.repository(transaction.participants[role])
        _import_candidate(repository, plan)
        candidate = plan.journal_candidate()
        if not journal.removal_targets:
            candidate = candidate.with_finalized(plan.prepared_sha)
        journal = journal.with_candidate(role, candidate)
    prepared_refs: list[_RefReconciliation] = []
    for role, plan in plans.items():
        repository = transaction.repository(transaction.participants[role])
        prepared_refs.append(
            _RefReconciliation(
                repository,
                plan.staging_ref,
                plan.prepared_sha,
                frozenset({None, plan.prepared_sha}),
                f"expected absent or prepared {plan.prepared_sha}",
            )
        )
    _reconcile_refs(prepared_refs)
    if plans:
        transaction.persist(journal, _Checkpoint.CANDIDATES_PREPARED)


def _prepare_all(
    transaction: _AcceptanceTransaction,
) -> None:
    with tempfile.TemporaryDirectory(prefix="booley-accept-plan-") as directory:
        plan_directory = Path(directory)
        plans = _plan_missing_candidates(
            transaction.root,
            transaction.project_repository,
            transaction.slug,
            transaction.basis,
            transaction.journal,
            plan_directory,
            transaction.repositories,
        )
        _validate_candidate_surface(transaction, plans, plan_directory)
        _persist_candidate_plans(transaction, plans)
    if transaction.journal.state is JournalState.INITIALIZING:
        transaction.persist(
            transaction.journal.mark_prepared(),
            _Checkpoint.PREPARATION_COMPLETE,
        )


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
        journal.candidates["outer"].staging_ref,
    )
    if not has_project:
        return None
    if project_repository is None:
        raise AcceptanceOperationError("recorded project repository is unavailable")
    try:
        project_relative = checkout_project_dir_relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise AcceptanceOperationError(str(exc)) from exc
    project_checkout = temporary / project_relative
    _require_git(
        project_repository,
        "worktree",
        "add",
        "--detach",
        str(project_checkout),
        journal.candidates["project"].staging_ref,
    )
    return project_checkout


def _planned_finalization_paths(
    temporary: Path, basis: AcceptanceBasis, journal: AcceptanceJournal
) -> list[Path]:
    try:
        plan = plan_target_removals(
            temporary,
            list(journal.removal_targets),
            basis.bindings,
        )
        return list(apply_target_removals(temporary, plan))
    except (TargetFinalizationError, OSError, ValueError) as exc:
        raise AcceptanceOperationError(f"Target finalization failed: {exc}") from exc


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
    transaction: _AcceptanceTransaction,
) -> None:
    _reconcile_finalized_refs(transaction)


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
        raise AcceptanceOperationError(str(exc)) from exc


def _finalization_was_recorded(
    transaction: _AcceptanceTransaction,
    journal: AcceptanceJournal,
) -> bool:
    """Return whether every computed finalized identity reached durable storage."""
    try:
        persisted = transaction.store.load(
            transaction.path,
            transaction.slug,
            [item.as_dict() for item in transaction.basis.participants],
            cleanup=journal.cleanup,
            removal_targets=journal.removal_targets,
        )
    except (AcceptanceJournalError, OSError, ValueError):
        return False
    return all(
        persisted.candidates.get(role) == candidate
        for role, candidate in journal.candidates.items()
    )


def _compute_finalized_journal(
    transaction: _AcceptanceTransaction,
    temporary: Path,
    journal: AcceptanceJournal,
) -> tuple[AcceptanceJournal, Path | None]:
    project_checkout = _add_finalization_worktrees(
        transaction.root,
        temporary,
        transaction.project_repository,
        "project" in transaction.participants,
        journal,
    )
    changed = _planned_finalization_paths(temporary, transaction.basis, journal)
    finalized = _commit_finalized_candidates(
        temporary, project_checkout, changed, transaction.slug
    )
    for role, sha in finalized.items():
        journal = journal.with_finalized_identity(role, sha)
    return journal, project_checkout


def _finalize_all(
    transaction: _AcceptanceTransaction,
) -> None:
    """Apply removals to a composite candidate before either ref is published."""
    journal = transaction.journal
    by_role = transaction.participants
    temporary = _finalization_directory(transaction.root, journal)
    project_checkout = _retained_project_checkout(
        transaction.root, temporary, "project" in by_role
    )
    if journal["candidates"] and all(
        candidate.finalized_sha is not None for candidate in journal["candidates"].values()
    ):
        _protect_finalized_candidates(
            transaction.root, transaction.project_repository, by_role, journal
        )
        _remove_finalization_worktrees(
            transaction.root, temporary, transaction.project_repository, project_checkout
        )
        return
    _reject_unjournaled_keepalives(
        transaction.root, transaction.project_repository, by_role, journal
    )
    _remove_finalization_worktrees(
        transaction.root, temporary, transaction.project_repository, project_checkout
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    journaled = False
    protected = False
    try:
        journal, project_checkout = _compute_finalized_journal(transaction, temporary, journal)
        transaction.persist(journal, _Checkpoint.CANDIDATES_FINALIZED)
        journaled = True
        _protect_finalized_candidates(
            transaction.root, transaction.project_repository, by_role, journal
        )
        protected = True
    finally:
        if not journaled:
            journaled = _finalization_was_recorded(transaction, journal)
        if not journaled or protected:
            _remove_finalization_worktrees(
                transaction.root,
                temporary,
                transaction.project_repository,
                project_checkout,
            )


def _publish_all(
    transaction: _AcceptanceTransaction,
    allowed_board_rename: tuple[Path, Path] | None,
) -> None:
    by_role = transaction.participants
    # Publish the hidden control repository first.  The user-visible outer ref
    # moves last, after every candidate is known to be conflict-free.
    roles = [role for role in ("project", "outer") if role in by_role]
    for role in roles:
        if role in transaction.journal.published:
            continue
        participant = by_role[role]
        repository = transaction.repository(participant)
        transaction.repositories.perform(
            RepositoryBoundary.PUBLICATION,
            role,
            lambda repository=repository, participant=participant, role=role: _publish_candidate(
                repository,
                participant,
                transaction.journal.candidates[role],
                allowed_board_rename,
            ),
        )
        checkpoint = (
            _Checkpoint.PROJECT_PUBLISHED if role == "project" else _Checkpoint.OUTER_PUBLISHED
        )
        transaction.persist(transaction.journal.mark_published(role), checkpoint)


def _validate_recorded_destinations(
    root: Path,
    project_repository: Path | None,
    basis: AcceptanceBasis,
    journal: AcceptanceJournal,
    *,
    after_approval: bool,
) -> None:
    by_role = {item.role: item for item in basis.participants}
    for role in journal["published"]:
        participant = by_role[role]
        repository = _repository_for(root, project_repository, participant)
        finalized = _required_finalized_sha(journal["candidates"][role])
        destination = _commit(repository, participant.destination_ref)
        if _is_ancestor(repository, finalized, destination):
            continue
        message = (
            f"{participant.destination_ref} no longer contains finalized "
            f"{role} candidate {finalized}"
        )
        if after_approval:
            raise AcceptanceRecoveryBlockedError(message)
        raise AcceptanceOperationError(message)


def _validate_published_destinations(
    root: Path,
    project_repository: Path | None,
    basis: AcceptanceBasis,
    journal: AcceptanceJournal,
    *,
    after_approval: bool,
) -> None:
    expected = {item.role for item in basis.participants}
    if set(journal["published"]) != expected:
        raise AcceptanceOperationError("cannot approve before every repository is published")
    _validate_recorded_destinations(
        root,
        project_repository,
        basis,
        journal,
        after_approval=after_approval,
    )


def _source_reconciliation(
    transaction: _AcceptanceTransaction,
    participant: BasisParticipant,
    sources: dict[str, str],
    current: dict[str, str],
    has_journaled_sources: bool,
) -> tuple[str, _RefReconciliation]:
    repository = transaction.repository(participant)
    ref = _source_keepalive_ref(transaction.journal, participant.role)
    existing = _direct_ref_identity(repository, ref)
    if has_journaled_sources:
        source = sources[participant.role]
    elif existing is not None:
        source = _validated_source_keepalive(repository, participant, ref, existing)
    else:
        source = current[participant.role]
    _commit(repository, source)
    if not _is_ancestor(repository, participant.authoring_sha, source):
        raise AcceptanceOperationError(
            f"pinned {participant.role} source no longer descends from its recorded commit"
        )
    plan = _RefReconciliation(
        repository,
        ref,
        source,
        frozenset({None, source}),
        f"expected absent or pinned source {source}",
    )
    return source, plan


def _ensure_sources(
    transaction: _AcceptanceTransaction,
    destination_branch: str,
    expected_sources: Mapping[str, str] | None,
) -> None:
    sources = dict(transaction.journal.sources)
    has_journaled_sources = bool(sources)
    current: dict[str, str] = {}
    if not has_journaled_sources:
        try:
            current = pin_basis_refs(
                transaction.root,
                transaction.basis,
                slug=transaction.slug,
                destination_branch=destination_branch,
            )
        except AcceptanceBasisOperationError as exc:
            raise AcceptanceOperationError(str(exc)) from exc
    if expected_sources is not None:
        expected = dict(expected_sources)
        actual = sources if has_journaled_sources else current
        if set(expected) != set(transaction.participants) or actual != expected:
            raise AcceptanceOperationError(
                "Ticket heads changed after the accepted snapshot was frozen"
            )
    plans: list[_RefReconciliation] = []
    for participant in transaction.basis.participants:
        source, plan = _source_reconciliation(
            transaction, participant, sources, current, has_journaled_sources
        )
        plans.append(plan)
        sources[participant.role] = source
    _reconcile_refs(plans)
    if not has_journaled_sources:
        transaction.persist(
            transaction.journal.with_sources(sources),
            _Checkpoint.SOURCES_PINNED,
        )


def _source_keepalive_ref(journal: AcceptanceJournal, role: str) -> str:
    return f"refs/booley/acceptance/{journal['transaction']}/source-{role}"


def _validated_source_keepalive(
    repository: Path,
    participant: BasisParticipant,
    ref: str,
    identity: str,
) -> str:
    commit = _commit(repository, ref)
    if identity != commit:
        raise AcceptanceOperationError(
            f"acceptance source keepalive {ref} points at non-commit object {identity}"
        )
    if not _is_ancestor(repository, participant.authoring_sha, identity):
        raise AcceptanceOperationError(
            f"acceptance source keepalive {ref} no longer descends from recorded "
            f"{participant.role} commit {participant.authoring_sha}"
        )
    return identity


def _validated_keepalives(
    root: Path,
    project_repository: Path | None,
    basis: AcceptanceBasis,
    journal: AcceptanceJournal,
) -> list[tuple[Path, str, str]]:
    by_role = {item.role: item for item in basis.participants}
    refs: list[tuple[Path, str, str]] = []
    for role, participant in by_role.items():
        repository = _repository_for(root, project_repository, participant)
        finalized = _required_finalized_sha(journal["candidates"][role])
        destination = _commit(repository, participant.destination_ref)
        if not _is_ancestor(repository, finalized, destination):
            raise AcceptanceOperationError(
                f"cannot retire finalization keepalive before {finalized} is contained in "
                f"{participant.destination_ref}"
            )
        ref = _finalized_keepalive_ref(journal, role)
        current = _direct_ref_identity(repository, ref)
        if current not in {None, finalized}:
            raise AcceptanceRecoveryBlockedError(
                f"acceptance keepalive {ref} has unknown identity {current}; "
                f"expected finalized {finalized}"
            )
        refs.append((repository, ref, finalized))
        source = journal["sources"][role]
        source_ref = _source_keepalive_ref(journal, role)
        source_current = _direct_ref_identity(repository, source_ref)
        if source_current not in {None, source}:
            raise AcceptanceRecoveryBlockedError(
                f"acceptance source keepalive {source_ref} has unknown identity "
                f"{source_current}; expected pinned source {source}"
            )
        refs.append((repository, source_ref, source))
    return refs


def _retire_keepalives(
    root: Path,
    project_repository: Path | None,
    basis: AcceptanceBasis,
    journal: AcceptanceJournal,
) -> None:
    refs = _validated_keepalives(root, project_repository, basis, journal)
    for repository, ref, finalized in refs:
        _delete_ref_at(repository, ref, finalized)


def _validate_cleaned_participant(
    repository: Path,
    participant: BasisParticipant,
    candidate: Candidate,
) -> None:
    checkout = _checked_out_at(repository, participant.ticket_ref)
    if checkout is not None:
        raise AcceptanceRecoveryBlockedError(
            f"cleaned Ticket worktree for {participant.role} was recreated at {checkout}"
        )
    for ref in (participant.ticket_ref, candidate.staging_ref):
        current = _direct_ref_identity(repository, ref)
        if current is not None:
            raise AcceptanceRecoveryBlockedError(
                f"cleaned {participant.role} ref {ref} was recreated at {current}"
            )


def _prevalidate_cleanup(transaction: _AcceptanceTransaction, roles: tuple[str, ...]) -> None:
    for role in roles:
        participant = transaction.participants[role]
        repository = transaction.repository(participant)
        if role in transaction.journal.cleaned:
            _validate_cleaned_participant(
                repository,
                participant,
                transaction.journal.candidates[role],
            )
            continue
        _validate_cleanup_participant(
            repository,
            participant,
            transaction.journal.sources[role],
            transaction.journal.candidates[role],
        )


def _cleanup_pending(transaction: _AcceptanceTransaction, roles: tuple[str, ...]) -> None:
    for role in roles:
        if role in transaction.journal.cleaned:
            continue
        participant = transaction.participants[role]
        repository = transaction.repository(participant)
        transaction.repositories.perform(
            RepositoryBoundary.RETIREMENT,
            role,
            lambda repository=repository, participant=participant, role=role: _cleanup_participant(
                repository,
                participant,
                transaction.journal.sources[role],
                transaction.journal.candidates[role],
            ),
        )
        checkpoint = (
            _Checkpoint.PROJECT_CLEANED if role == "project" else _Checkpoint.OUTER_CLEANED
        )
        transaction.persist(transaction.journal.mark_cleaned(role), checkpoint)


def _cleanup_all(transaction: _AcceptanceTransaction) -> None:
    if not transaction.journal.cleanup:
        _reconcile_finalized_refs(transaction)
        _validate_ticket_refs(
            transaction.root,
            transaction.project_repository,
            transaction.basis,
            transaction.journal,
        )
    else:
        roles = transaction.journal.transition_order
        _prevalidate_cleanup(transaction, roles)
        _cleanup_pending(transaction, roles)
    transaction.persist(transaction.journal.mark_done(), _Checkpoint.DONE)


@dataclass
class _AcceptanceTransaction:
    root: Path
    project_repository: Path | None
    slug: str
    basis: AcceptanceBasis
    journal: AcceptanceJournal
    path: Path
    store: AcceptanceStore
    repositories: AcceptanceRepositories

    @property
    def participants(self) -> dict[str, BasisParticipant]:
        return {item.role: item for item in self.basis.participants}

    def repository(self, participant: BasisParticipant) -> Path:
        """Resolve one Acceptance Basis participant to its local repository."""
        return _repository_for(self.root, self.project_repository, participant)

    def persist(
        self,
        journal: AcceptanceJournal,
        checkpoint: _Checkpoint,
    ) -> None:
        """Persist one legal transition before exposing it to later steps."""
        self.store.write(self.path, journal, checkpoint)
        self.journal = journal


def _prepare_pending_publication(
    transaction: _AcceptanceTransaction,
    destination_branch: str,
    *,
    cleanup: bool,
    expected_sources: Mapping[str, str] | None,
) -> None:
    _ensure_sources(transaction, destination_branch, expected_sources)
    _validate_source_surface(
        transaction.root,
        transaction.project_repository,
        transaction.basis,
        transaction.journal["sources"],
    )
    _prepare_all(transaction)
    _finalize_all(transaction)
    _update_finalized_refs(transaction)
    if not cleanup:
        _validate_ticket_refs(
            transaction.root,
            transaction.project_repository,
            transaction.basis,
            transaction.journal,
        )


def _publish_pending_candidates(
    transaction: _AcceptanceTransaction,
    allowed_board_rename: tuple[Path, Path] | None,
) -> None:
    _validate_recorded_destinations(
        transaction.root,
        transaction.project_repository,
        transaction.basis,
        transaction.journal,
        after_approval=False,
    )
    _publish_all(transaction, allowed_board_rename)
    _update_finalized_refs(transaction)
    _validate_published_destinations(
        transaction.root,
        transaction.project_repository,
        transaction.basis,
        transaction.journal,
        after_approval=False,
    )


def _destination_branch(basis: AcceptanceBasis) -> str:
    outer = next(item for item in basis.participants if item.role == "outer")
    prefix = "refs/heads/"
    if not outer.destination_ref.startswith(prefix):
        raise AcceptanceOperationError("outer Acceptance Basis destination must be a branch ref")
    return outer.destination_ref.removeprefix(prefix)


def _validate_ticket_status(journal: AcceptanceJournal, ticket_status: str) -> None:
    state = JournalState(journal["state"])
    if ticket_status == "review" and state in {
        JournalState.ACCEPTED,
        JournalState.CLEANUP_PROJECT,
        JournalState.CLEANUP_OUTER,
        JournalState.DONE,
    }:
        raise AcceptanceOperationError(
            f"acceptance journal is {state} but the Ticket is still in review"
        )
    if ticket_status == "done" and state in {
        JournalState.INITIALIZING,
        JournalState.PREPARED,
        JournalState.PUBLISHED_PROJECT,
    }:
        raise AcceptanceRecoveryBlockedError(
            f"Ticket is done before acceptance publication reached the outer repository ({state})"
        )


def _finish_done(transaction: _AcceptanceTransaction) -> AcceptanceProgress:
    _validate_published_destinations(
        transaction.root,
        transaction.project_repository,
        transaction.basis,
        transaction.journal,
        after_approval=True,
    )
    try:
        _retire_keepalives(
            transaction.root,
            transaction.project_repository,
            transaction.basis,
            transaction.journal,
        )
    except AcceptanceRecoveryBlockedError:
        raise
    except (AcceptanceOperationError, OSError, ValueError) as exc:
        return AcceptanceProgress(
            AcceptanceOutcome.ACCEPTED_PENDING,
            pending_phase="keepalive-retirement",
            detail=str(exc),
        )
    return AcceptanceProgress(AcceptanceOutcome.COMPLETE)


def _advance_publication(
    transaction: _AcceptanceTransaction,
    request: AcceptanceRequest,
) -> AcceptanceProgress | None:
    if transaction.journal.state is JournalState.PUBLISHED_OUTER:
        _validate_published_destinations(
            transaction.root,
            transaction.project_repository,
            transaction.basis,
            transaction.journal,
            after_approval=request.ticket_status == "done",
        )
    else:
        _prepare_pending_publication(
            transaction,
            _destination_branch(request.basis),
            cleanup=request.cleanup,
            expected_sources=request.expected_sources,
        )
        _publish_pending_candidates(transaction, request.allowed_board_rename)
    if request.ticket_status == "review":
        return AcceptanceProgress(AcceptanceOutcome.APPROVAL_REQUIRED)
    return None


def _advance_after_approval(transaction: _AcceptanceTransaction) -> AcceptanceProgress:
    _validate_published_destinations(
        transaction.root,
        transaction.project_repository,
        transaction.basis,
        transaction.journal,
        after_approval=True,
    )
    try:
        if transaction.journal.state is JournalState.PUBLISHED_OUTER:
            transaction.persist(transaction.journal.mark_accepted(), _Checkpoint.ACCEPTED)
        _validated_keepalives(
            transaction.root,
            transaction.project_repository,
            transaction.basis,
            transaction.journal,
        )
        _cleanup_all(transaction)
        _retire_keepalives(
            transaction.root,
            transaction.project_repository,
            transaction.basis,
            transaction.journal,
        )
    except AcceptanceRecoveryBlockedError:
        raise
    except (AcceptanceOperationError, OSError, ValueError) as exc:
        return AcceptanceProgress(
            AcceptanceOutcome.ACCEPTED_PENDING,
            pending_phase="cleanup-or-checkpoint",
            detail=str(exc),
        )
    return AcceptanceProgress(AcceptanceOutcome.COMPLETE)


def _advance_locked(
    runner: _AcceptanceRunner,
    request: AcceptanceRequest,
    path: Path,
    project_repository: Path | None,
) -> AcceptanceProgress:
    journal = _load_journal(
        path,
        request.slug,
        request.basis,
        cleanup=request.cleanup,
        removal_targets=request.basis.removal_targets,
        store=runner.store,
    )
    _validate_ticket_status(journal, request.ticket_status)
    root = request.root.resolve()
    transaction = _AcceptanceTransaction(
        root,
        project_repository,
        request.slug,
        request.basis,
        journal,
        path,
        runner.store,
        runner.repositories,
    )
    transaction.persist(journal, _Checkpoint.NORMALIZED)
    if journal.state is JournalState.DONE:
        return _finish_done(transaction)
    if journal.state.publication_pending:
        progress = _advance_publication(transaction, request)
        if progress is not None:
            return progress
    return _advance_after_approval(transaction)


def _assert_completion_slot(
    store: AcceptanceStore,
    directory: Path,
    slug: str,
    repositories: tuple[Path, ...],
) -> None:
    """Serialize publication while an earlier Ticket still has public work pending."""
    transactions: set[str] = set()
    for candidate in store.journals(directory):
        try:
            journal = store.load_persisted(candidate)
        except AcceptanceJournalError as exc:
            raise AcceptanceOperationError(
                f"cannot inspect earlier acceptance journal {candidate}: {exc}"
            ) from exc
        transactions.add(journal["transaction"])
        if candidate.stem == slug:
            continue
        state = JournalState(journal["state"])
        if state.publication_pending:
            ticket = journal.get("ticket", candidate.stem)
            raise AcceptanceOperationError(
                f"Ticket {ticket!r} has unfinished repository publication; resume it first"
            )
    _assert_no_orphan_acceptance_refs(repositories, transactions)


def _assert_no_orphan_acceptance_refs(
    repositories: tuple[Path, ...], transactions: set[str]
) -> None:
    prefix = "refs/booley/acceptance/"
    for repository in repositories:
        output = _require_git(
            repository,
            "for-each-ref",
            "--format=%(refname)",
            prefix,
        )
        for ref in output.splitlines():
            remainder = ref.removeprefix(prefix)
            transaction, separator, _name = remainder.partition("/")
            if separator and transaction not in transactions:
                raise AcceptanceOperationError(
                    f"orphaned acceptance ref {ref} in {repository}; inspect before retry"
                )


def cleanup_finished(root: Path, slug: str) -> bool:
    """Return whether a journaled acceptance finished its configured cleanup."""
    store = FileAcceptanceStore()
    path = store.path(root, slug)
    if not path.exists():
        return False
    try:
        journal = store.load_persisted(path)
    except AcceptanceJournalError as exc:
        raise AcceptanceOperationError(str(exc)) from exc
    return bool(journal.cleanup and journal.state is JournalState.DONE)


@dataclass(frozen=True)
class _AcceptanceRunner:
    """Advance acceptance through explicit persistence and repository adapters."""

    store: AcceptanceStore
    repositories: AcceptanceRepositories

    def advance(self, request: AcceptanceRequest) -> AcceptanceProgress:
        """Advance one recoverable acceptance as far as durable facts permit."""
        if request.ticket_status not in {"review", "done"}:
            raise AcceptanceOperationError(
                f"invalid Ticket status for acceptance: {request.ticket_status!r}"
            )
        root = request.root.resolve()
        path = self.store.path(root, request.slug)
        with self.store.locked(path):
            project_repository = self.repositories.project_repository(root)
            all_repositories = (root,)
            if project_repository is not None:
                all_repositories += (project_repository,)
            _assert_completion_slot(
                self.store,
                path.parent,
                request.slug,
                all_repositories,
            )
            return _advance_locked(self, request, path, project_repository)


def advance_acceptance(request: AcceptanceRequest) -> AcceptanceProgress:
    """Advance acceptance using the production store and Git adapters."""
    return _AcceptanceRunner(FileAcceptanceStore(), LocalAcceptanceRepositories()).advance(request)
