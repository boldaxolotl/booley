"""Crash-recoverable publication of Ticket baseline participant commits."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_list,
    require_str,
)
from booley.runtime.project_dir import runtime_dir

from .acceptance_targets import AcceptanceTargetBinding
from .persistence import atomic_replace_bytes
from .ticket_baseline import (
    BasisParticipant,
    ProviderTargetBinding,
    TicketBaseline,
    provider_binding_from_mapping,
    ticket_machine_digest,
    ticket_machine_from_participants,
    valid_branch_ref,
    valid_ticket_ref,
)

_OPERATION_RE = re.compile(r"[0-9a-f]{32}")
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class BasisPublicationError(RuntimeError):
    """Ticket baseline commits cannot be published or recovered safely."""


@dataclass(frozen=True)
class ParticipantPreparation:
    """Immutable Git inputs for one participant commit."""

    role: str
    ticket_ref: str
    destination_ref: str
    destination_sha: str
    expected_old_sha: str
    tree_sha: str
    message: str


@dataclass(frozen=True)
class TicketPublicationRequest:
    """Inputs shared by fresh publication and journal recovery."""

    slug: str
    source_sha256: str
    effective_sha256: str
    authored_sha256: str
    repositories: dict[str, Path]
    operation_id: str | None = None
    participants: tuple[ParticipantPreparation, ...] | None = None
    bindings: tuple[AcceptanceTargetBinding, ...] | None = None
    removal_targets: tuple[str, ...] | None = None
    providers: tuple[ProviderTargetBinding, ...] | None = None


@dataclass(frozen=True)
class BasisPublicationJournal:
    """Durable identities needed to roll an enqueue preparation forward."""

    schema: int
    operation_id: str
    slug: str
    source_sha256: str
    effective_sha256: str
    authored_sha256: str
    participants: tuple[ParticipantPreparation, ...]
    bindings: tuple[dict[str, str], ...]
    removal_targets: tuple[str, ...]
    providers: tuple[dict[str, str], ...]
    prepared: dict[str, str]
    published: tuple[str, ...]

    def with_prepared(self, role: str, sha: str) -> BasisPublicationJournal:
        values = dict(self.prepared)
        values[role] = sha
        return replace(self, prepared=values)

    def with_published(self, role: str) -> BasisPublicationJournal:
        if role in self.published:
            return self
        return replace(self, published=(*self.published, role))


def _journal_path(project_root: Path, slug: str) -> Path:
    return runtime_dir(project_root) / "acceptance" / "basis-publication" / f"{slug}.json"


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
        raise BasisPublicationError(f"git {' '.join(args)} failed in {repository}: {exc}") from exc


def _require_git(repository: Path, *args: str) -> str:
    result = _git(repository, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no diagnostic"
        raise BasisPublicationError(
            f"git {' '.join(args)} failed in {repository} (rc={result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _write(project_root: Path, journal: BasisPublicationJournal) -> None:
    payload = asdict(journal)
    payload["participants"] = [asdict(item) for item in journal.participants]
    payload["bindings"] = list(journal.bindings)
    payload["removal_targets"] = list(journal.removal_targets)
    payload["providers"] = list(journal.providers)
    payload["published"] = list(journal.published)
    atomic_replace_bytes(
        _journal_path(project_root, journal.slug),
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
    )


def load_basis_publication(project_root: Path, slug: str) -> BasisPublicationJournal | None:
    """Load one current-schema basis publication journal."""
    path = _journal_path(project_root, slug)
    if not path.exists():
        return None
    try:
        journal = _parse_journal(json.loads(path.read_text(encoding="utf-8")))
    except (BoundaryError, OSError, json.JSONDecodeError) as exc:
        raise BasisPublicationError(
            f"basis publication journal is unreadable: {path}: {exc}"
        ) from exc
    _validate_journal(journal, slug)
    return journal


def _parse_journal(value: Any) -> BasisPublicationJournal:
    mapping = require_dict(value, field="basis publication journal")
    if set(mapping) != set(BasisPublicationJournal.__dataclass_fields__):
        raise BoundaryError("basis publication journal has invalid fields or schema")
    schema = require_int(mapping.get("schema"), field="basis publication schema")
    operation_id = require_str(mapping, "operation_id")
    digests = (
        require_str(mapping, "source_sha256"),
        require_str(mapping, "effective_sha256"),
        require_str(mapping, "authored_sha256"),
    )
    if schema != 1 or not _OPERATION_RE.fullmatch(operation_id):
        raise BoundaryError("basis publication journal has invalid identity or schema")
    if not all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests):
        raise BoundaryError("basis publication input digest is invalid")
    return BasisPublicationJournal(
        schema=schema,
        operation_id=operation_id,
        slug=require_str(mapping, "slug"),
        source_sha256=digests[0],
        effective_sha256=digests[1],
        authored_sha256=digests[2],
        participants=_parse_items(mapping, "participants", _parse_participant),
        bindings=_parse_items(mapping, "bindings", _parse_binding),
        removal_targets=_parse_strings(mapping, "removal_targets"),
        providers=_parse_items(mapping, "providers", _parse_provider),
        prepared=_sha_map(mapping.get("prepared"), "prepared"),
        published=_parse_strings(mapping, "published"),
    )


def _parse_items(mapping: dict, key: str, parser: Any) -> tuple:
    values = require_list(mapping.get(key), field=f"basis publication {key}")
    return tuple(parser(item, index) for index, item in enumerate(values))


def _parse_strings(mapping: dict, key: str) -> tuple[str, ...]:
    values = require_list(mapping.get(key), field=f"basis publication {key}")
    return tuple(
        _string_item(item, f"basis publication {key}[{index}]")
        for index, item in enumerate(values)
    )


def _parse_participant(value: Any, index: int) -> ParticipantPreparation:
    field = f"basis publication participants[{index}]"
    mapping = require_dict(value, field=field)
    if set(mapping) != set(ParticipantPreparation.__dataclass_fields__):
        raise BoundaryError(f"{field} has invalid fields")
    participant = ParticipantPreparation(
        role=require_str(mapping, "role"),
        ticket_ref=require_str(mapping, "ticket_ref"),
        destination_ref=require_str(mapping, "destination_ref"),
        destination_sha=require_str(mapping, "destination_sha"),
        expected_old_sha=require_str(mapping, "expected_old_sha"),
        tree_sha=require_str(mapping, "tree_sha"),
        message=require_str(mapping, "message"),
    )
    for key in ("destination_sha", "expected_old_sha", "tree_sha"):
        if not _SHA_RE.fullmatch(getattr(participant, key)):
            raise BoundaryError(f"{field}.{key} is invalid")
    if not valid_ticket_ref(participant.ticket_ref):
        raise BoundaryError(f"{field}.ticket_ref is not a generation-qualified ref")
    if not valid_branch_ref(participant.destination_ref):
        raise BoundaryError(f"{field}.destination_ref is not a full branch ref")
    return participant


def _parse_binding(value: Any, index: int) -> dict[str, str]:
    field = f"basis publication bindings[{index}]"
    mapping = require_dict(value, field=field)
    expected = set(AcceptanceTargetBinding.__dataclass_fields__)
    if set(mapping) != expected:
        raise BoundaryError(f"{field} has invalid fields")
    result = {key: require_str(mapping, key) for key in expected}
    try:
        _ = AcceptanceTargetBinding(**result).validate_persisted()
    except (TypeError, ValueError) as exc:
        raise BoundaryError(f"{field} is invalid: {exc}") from exc
    return result


def _parse_provider(value: Any, index: int) -> dict[str, str]:
    try:
        return provider_binding_from_mapping(value).as_dict()
    except ValueError as exc:
        raise BoundaryError(f"basis publication providers[{index}] is invalid: {exc}") from exc


def _sha_map(value: Any, label: str) -> dict[str, str]:
    mapping = require_dict(value, field=f"basis publication {label}")
    result = {
        str(role): _string_item(sha, f"basis publication {label}.{role}")
        for role, sha in mapping.items()
    }
    if any(not _SHA_RE.fullmatch(sha) for sha in result.values()):
        raise BoundaryError(f"basis publication {label} contains an invalid commit")
    return result


def _string_item(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise BoundaryError(f"{field} must be a string")
    return value


def _validate_journal(journal: BasisPublicationJournal, slug: str) -> None:
    if journal.slug != slug:
        raise BasisPublicationError("basis publication journal belongs to another Ticket")
    roles = tuple(item.role for item in journal.participants)
    if roles != tuple(sorted(set(roles))) or "outer" not in roles:
        raise BasisPublicationError("basis publication participants are invalid")
    if set(journal.prepared) - set(roles) or set(journal.published) - set(journal.prepared):
        raise BasisPublicationError("basis publication checkpoints are inconsistent")
    order = tuple(role for role in ("project", "outer") if role in roles)
    if journal.published != order[: len(journal.published)]:
        raise BasisPublicationError("basis publication participant order is invalid")


def publish_ticket_commits(
    project_root: Path,
    request: TicketPublicationRequest,
) -> tuple[TicketBaseline, str]:
    """Prepare and CAS-publish participant commits, resuming any prior journal."""
    journal = load_basis_publication(project_root, request.slug)
    if journal is None:
        journal = _new_journal(request)
        _write(project_root, journal)
    else:
        _validate_resume(journal, request)
    _validate_repositories(journal, request.repositories)
    journal = _prepare_participant_commits(project_root, request.repositories, journal)
    basis = _basis(journal)
    journal = _publish_participant_commits(project_root, request.repositories, journal)
    _publish_ticket_keepalives(request.repositories, basis, journal.operation_id)
    _retire_temporary_keepalives(request.repositories, journal)
    return basis, journal.operation_id


def _new_journal(request: TicketPublicationRequest) -> BasisPublicationJournal:
    if request.operation_id is None or not _OPERATION_RE.fullmatch(request.operation_id):
        raise BasisPublicationError("basis publication operation ID is invalid")
    if any(
        value is None
        for value in (
            request.participants,
            request.bindings,
            request.removal_targets,
            request.providers,
        )
    ):
        raise BasisPublicationError("new basis publication is missing prepared inputs")
    assert request.participants is not None
    assert request.bindings is not None
    assert request.removal_targets is not None
    assert request.providers is not None
    journal = BasisPublicationJournal(
        schema=1,
        operation_id=request.operation_id,
        slug=request.slug,
        source_sha256=request.source_sha256,
        effective_sha256=request.effective_sha256,
        authored_sha256=request.authored_sha256,
        participants=request.participants,
        bindings=tuple(binding.as_dict() for binding in request.bindings),
        removal_targets=request.removal_targets,
        providers=tuple(row.as_dict() for row in request.providers),
        prepared={},
        published=(),
    )
    _validate_journal(journal, request.slug)
    return journal


def _prepare_participant_commits(
    project_root: Path,
    repositories: dict[str, Path],
    journal: BasisPublicationJournal,
) -> BasisPublicationJournal:
    plans = {item.role: item for item in journal.participants}
    for role, sha in journal.prepared.items():
        _validate_prepared_commit(
            repositories[role],
            _temporary_ref(journal.operation_id, role),
            plans[role],
            sha,
            journal,
        )
    for plan in sorted(journal.participants, key=lambda item: item.role == "outer"):
        if plan.role in journal.prepared:
            continue
        sha = _recover_or_create_commit(repositories[plan.role], journal, plan)
        journal = journal.with_prepared(plan.role, sha)
        _write(project_root, journal)
    return journal


def _publish_participant_commits(
    project_root: Path,
    repositories: dict[str, Path],
    journal: BasisPublicationJournal,
) -> BasisPublicationJournal:
    plans = {item.role: item for item in journal.participants}
    for role in (item for item in ("project", "outer") if item in repositories):
        if role in journal.published:
            continue
        _publish_ticket_ref(repositories[role], plans[role], journal.prepared[role])
        _require_git(repositories[role], "read-tree", journal.prepared[role])
        journal = journal.with_published(role)
        _write(project_root, journal)
    return journal


def _validate_resume(journal: BasisPublicationJournal, request: TicketPublicationRequest) -> None:
    if journal.source_sha256 != request.source_sha256:
        raise BasisPublicationError("Ticket draft changed during basis publication")
    if journal.effective_sha256 != request.effective_sha256:
        raise BasisPublicationError("effective Ticket fields changed during basis publication")
    if journal.authored_sha256 != request.authored_sha256:
        raise BasisPublicationError("authored Ticket changed during publication")
    if request.participants is not None and request.participants != journal.participants:
        raise BasisPublicationError("basis publication participant inputs changed")
    if (
        request.bindings is not None
        and tuple(item.as_dict() for item in request.bindings) != journal.bindings
    ):
        raise BasisPublicationError("basis publication Target bindings changed")
    if request.removal_targets is not None and request.removal_targets != journal.removal_targets:
        raise BasisPublicationError("basis publication removal Targets changed")
    if (
        request.providers is not None
        and tuple(row.as_dict() for row in request.providers) != journal.providers
    ):
        raise BasisPublicationError("basis publication provider bindings changed")


def _validate_repositories(
    journal: BasisPublicationJournal, repositories: dict[str, Path]
) -> None:
    roles = {item.role for item in journal.participants}
    if set(repositories) != roles:
        raise BasisPublicationError("basis publication repositories do not match participants")


def _temporary_ref(operation_id: str, role: str) -> str:
    return f"refs/booley/enqueue/{operation_id}/{role}"


def _recover_or_create_commit(
    repository: Path,
    journal: BasisPublicationJournal,
    plan: ParticipantPreparation,
) -> str:
    temporary_ref = _temporary_ref(journal.operation_id, plan.role)
    existing = _git(repository, "rev-parse", "--verify", "--quiet", f"{temporary_ref}^{{commit}}")
    if existing.returncode == 0:
        sha = existing.stdout.strip()
    elif existing.returncode == 1:
        sha = _require_git(
            repository,
            "commit-tree",
            plan.tree_sha,
            "-p",
            plan.expected_old_sha,
            "-m",
            _commit_message(journal, plan),
        )
        _require_git(repository, "update-ref", temporary_ref, sha, "")
    else:
        detail = (existing.stderr or existing.stdout).strip() or "no diagnostic"
        raise BasisPublicationError(
            f"could not inspect temporary enqueue ref {temporary_ref}: {detail}"
        )
    _validate_prepared_commit(repository, temporary_ref, plan, sha, journal)
    return sha


def _commit_message(journal: BasisPublicationJournal, plan: ParticipantPreparation) -> str:
    if plan.role != "outer":
        return plan.message
    if any(
        item.role == "project" and item.role not in journal.prepared
        for item in journal.participants
    ):
        raise BasisPublicationError("project commit must precede outer commit")
    participants = tuple(
        BasisParticipant(
            item.role,
            journal.prepared.get(item.role, ""),
            item.ticket_ref,
            item.destination_ref,
            item.destination_sha,
        )
        for item in journal.participants
    )
    machine = ticket_machine_from_participants(
        participants,
        authored_sha256=journal.authored_sha256,
        generation=journal.operation_id,
        providers=tuple(provider_binding_from_mapping(row) for row in journal.providers),
    )
    return (
        f"{plan.message}\n\n"
        f"Booley-Ticket-Slug: {journal.slug}\n"
        f"Booley-Authored-SHA256: {journal.authored_sha256}\n"
        f"Booley-Machine-SHA256: {ticket_machine_digest(machine)}"
    )


def _validate_prepared_commit(
    repository: Path,
    temporary_ref: str,
    plan: ParticipantPreparation,
    sha: str,
    journal: BasisPublicationJournal,
) -> None:
    tree_and_parents = _require_git(repository, "show", "-s", "--format=%T%n%P", sha).splitlines()
    tree = tree_and_parents[0] if tree_and_parents else ""
    parents = tree_and_parents[1].split() if len(tree_and_parents) > 1 else []
    if tree != plan.tree_sha or parents != [plan.expected_old_sha]:
        raise BasisPublicationError(
            f"temporary enqueue ref {temporary_ref} does not match its journaled tree and parent"
        )
    actual_message = _require_git(repository, "show", "-s", "--format=%B", sha)
    if actual_message != _commit_message(journal, plan):
        raise BasisPublicationError(f"temporary enqueue ref {temporary_ref} has another message")


def _publish_ticket_ref(
    repository: Path,
    plan: ParticipantPreparation,
    prepared_sha: str,
) -> None:
    current = _require_git(repository, "rev-parse", "--verify", f"{plan.ticket_ref}^{{commit}}")
    if current == prepared_sha:
        return
    if current != plan.expected_old_sha:
        raise BasisPublicationError(
            f"ticket ref {plan.ticket_ref} changed during basis publication; "
            f"expected {plan.expected_old_sha} or {prepared_sha}, found {current}"
        )
    _require_git(
        repository,
        "update-ref",
        plan.ticket_ref,
        prepared_sha,
        plan.expected_old_sha,
    )


def _basis(journal: BasisPublicationJournal) -> TicketBaseline:
    participants = tuple(
        BasisParticipant(
            item.role,
            journal.prepared[item.role],
            item.ticket_ref,
            item.destination_ref,
            item.destination_sha,
        )
        for item in journal.participants
    )
    bindings = tuple(AcceptanceTargetBinding(**item) for item in journal.bindings)
    providers = tuple(provider_binding_from_mapping(row) for row in journal.providers)
    return TicketBaseline(participants, bindings, journal.removal_targets, providers=providers)


def _publish_ticket_keepalives(
    repositories: dict[str, Path], basis: TicketBaseline, generation: str
) -> None:
    for participant in basis.participants:
        repository = repositories[participant.role]
        ref = f"refs/booley/tickets/{generation}/{participant.role}"
        existing = _git(repository, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if existing.returncode == 0:
            if existing.stdout.strip() != participant.authoring_sha:
                raise BasisPublicationError(f"Ticket keepalive {ref} changed")
            continue
        if existing.returncode != 1:
            detail = (existing.stderr or existing.stdout).strip() or "no diagnostic"
            raise BasisPublicationError(f"could not inspect Ticket keepalive {ref}: {detail}")
        _require_git(repository, "update-ref", ref, participant.authoring_sha, "")


def _retire_temporary_keepalives(
    repositories: dict[str, Path], journal: BasisPublicationJournal
) -> None:
    for role, sha in journal.prepared.items():
        ref = _temporary_ref(journal.operation_id, role)
        existing = _git(
            repositories[role], "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
        )
        if existing.returncode == 1:
            continue
        if existing.returncode != 0:
            detail = (existing.stderr or existing.stdout).strip() or "no diagnostic"
            raise BasisPublicationError(f"could not inspect temporary enqueue ref {ref}: {detail}")
        if existing.stdout.strip() != sha:
            raise BasisPublicationError(f"temporary enqueue ref {ref} changed")
        _require_git(
            repositories[role],
            "update-ref",
            "-d",
            ref,
            sha,
        )


def finish_basis_publication(project_root: Path, slug: str, operation_id: str) -> None:
    """Retire a completed basis journal after the Board cutover is durable."""
    journal = load_basis_publication(project_root, slug)
    if journal is None:
        return
    if journal.operation_id != operation_id:
        raise BasisPublicationError("enqueue and basis publication operations disagree")
    roles = {item.role for item in journal.participants}
    if set(journal.published) != roles:
        raise BasisPublicationError("cannot retire an incompletely published basis journal")
    _journal_path(project_root, slug).unlink()


def abandon_basis_publication(
    project_root: Path,
    slug: str,
    operation_id: str,
    repositories: dict[str, Path],
) -> None:
    """Delete only refs owned by an unpublished replacement-basis operation."""
    journal = load_basis_publication(project_root, slug)
    if journal is None:
        return
    if journal.operation_id != operation_id:
        raise BasisPublicationError("basis abandonment operation identity changed")
    roles = {item.role for item in journal.participants}
    if not roles <= set(repositories):
        raise BasisPublicationError("basis abandonment repositories do not match participants")
    for participant in journal.participants:
        prepared = journal.prepared.get(participant.role)
        allowed = {participant.expected_old_sha, *([prepared] if prepared else [])}
        _delete_owned_ref(repositories[participant.role], participant.ticket_ref, allowed)
        if prepared:
            _delete_owned_ref(
                repositories[participant.role],
                _temporary_ref(operation_id, participant.role),
                {prepared},
            )
    if len(journal.prepared) == len(journal.participants):
        basis = _basis(journal)
        for participant in basis.participants:
            _delete_owned_ref(
                repositories[participant.role],
                f"refs/booley/tickets/{journal.operation_id}/{participant.role}",
                {participant.authoring_sha},
            )
    _journal_path(project_root, slug).unlink()


def _delete_owned_ref(repository: Path, ref: str, allowed: set[str]) -> None:
    existing = _git(repository, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if existing.returncode == 1:
        return
    if existing.returncode != 0:
        detail = (existing.stderr or existing.stdout).strip() or "no diagnostic"
        raise BasisPublicationError(f"could not inspect abandoned ref {ref}: {detail}")
    current = existing.stdout.strip()
    if current not in allowed:
        raise BasisPublicationError(f"abandoned ref {ref} changed unexpectedly")
    _require_git(repository, "update-ref", "-d", ref, current)
