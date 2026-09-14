"""Recoverable pre-execution Ticket baseline refresh for waiting Tickets."""

from __future__ import annotations

import json
import re
import secrets
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str
from booley.core.models import TargetPlan
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_dir import (
    resolve_checkout_project_dir,
    resolve_project_dir,
    runtime_dir,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError
from booley.ticket_board.ticket_repositories import paired_project_repository

from .basis_publication import BasisPublicationError
from .persistence import atomic_replace_bytes
from .planned_dependencies import PlannedDependencyError, target_surface_sha256
from .scanner import find_ticket_file
from .target_surface_edit import (
    TargetSurfaceEditError,
    merge_target_definition,
    toml_table_block,
)
from .ticket_baseline import (
    ProviderTargetBinding,
    TicketBaseline,
    TicketBaselineError,
    load_ticket_baseline_from_document,
    ticket_baseline_from_machine,
    ticket_machine_from_spec,
)
from .ticket_document import TicketDocument, convert_ticket_document, ticket_conversion_context
from .workspace_ops import (
    AuthoringWorkspace,
    TicketBaselineOperationError,
    basis_changed_paths,
    discard_generation_refs,
    discard_refresh_workspace,
    load_refresh_source_workspace,
    open_authoring_generation,
    prepare_replacement_ticket_baseline,
    relocate_refresh_workspace,
)

_OPERATION_RE = re.compile(r"[0-9a-f]{32}")
_GENERATION_RE = re.compile(r"[0-9a-f]{16}")


class BasisRefreshError(RuntimeError):
    """A waiting Ticket cannot be refreshed without new author approval."""


def _converted_ticket(root: Path, ticket: Path, slug: str) -> TicketDocument:
    with ticket_conversion_context(root, slug, "executable") as context:
        converted = convert_ticket_document(ticket.read_text(encoding="utf-8"), context)
    if converted.document is None:
        detail = "; ".join(item.message for item in converted.diagnostics)
        raise BasisRefreshError(f"Ticket document is invalid: {detail}")
    return converted.document


@dataclass(frozen=True)
class BasisRefreshJournal:
    """Stable identities needed to resume refresh publication and relocation."""

    schema: int
    operation_id: str
    generation: str
    slug: str
    old_ticket_generation: str
    state: str
    machine: dict[str, Any]

    def prepared(self, machine: dict[str, Any]) -> BasisRefreshJournal:
        return replace(self, state="prepared", machine=machine)


@dataclass(frozen=True)
class _RefreshBuild:
    root: Path
    ticket: Path
    slug: str
    fields: dict[str, Any]
    old_basis: TicketBaseline
    old: AuthoringWorkspace
    journal: BasisRefreshJournal

    @property
    def operation(self) -> Path:
        return _operation_path(self.root, self.journal.operation_id)


def _journal_path(root: Path, slug: str) -> Path:
    return runtime_dir(root) / "acceptance" / "refresh" / f"{slug}.json"


def _operation_path(root: Path, operation_id: str) -> Path:
    return runtime_dir(root) / "acceptance" / "refresh" / operation_id


def _write_journal(root: Path, journal: BasisRefreshJournal) -> None:
    atomic_replace_bytes(
        _journal_path(root, journal.slug),
        (json.dumps(asdict(journal), indent=2, sort_keys=True) + "\n").encode(),
    )


def load_basis_refresh(root: Path, slug: str) -> BasisRefreshJournal | None:
    """Load and validate one pending refresh operation."""
    path = _journal_path(root, slug)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        mapping = require_dict(value, field="Basis Refresh journal")
        if set(mapping) != set(BasisRefreshJournal.__dataclass_fields__):
            raise ValueError("invalid fields")
        journal = _parse_journal(mapping)
    except (BoundaryError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BasisRefreshError(f"Basis Refresh journal is invalid: {path}") from exc
    _validate_journal(journal, slug, path)
    return journal


def _parse_journal(value: dict[str, Any]) -> BasisRefreshJournal:
    return BasisRefreshJournal(
        require_int(value.get("schema"), field="Basis Refresh journal.schema"),
        require_str(value, "operation_id"),
        require_str(value, "generation"),
        require_str(value, "slug"),
        require_str(value, "old_ticket_generation"),
        require_str(value, "state"),
        require_dict(value.get("machine"), field="Basis Refresh journal.machine"),
    )


def _validate_journal(journal: BasisRefreshJournal, slug: str, path: Path) -> None:
    if (
        journal.schema != 1
        or journal.slug != slug
        or journal.state not in {"building", "prepared"}
        or not _OPERATION_RE.fullmatch(journal.operation_id)
        or not _GENERATION_RE.fullmatch(journal.generation)
        or not re.fullmatch(r"[0-9a-f]{32}", journal.old_ticket_generation)
    ):
        raise BasisRefreshError(f"Basis Refresh journal identity is invalid: {path}")
    if journal.state == "prepared":
        try:
            ticket_baseline_from_machine(journal.machine)
        except TicketBaselineError as exc:
            raise BasisRefreshError("Basis Refresh journal has an invalid new basis") from exc
    elif journal.machine:
        raise BasisRefreshError("building Basis Refresh journal contains published output")


def _new_journal(root: Path, slug: str, old_basis: TicketBaseline) -> BasisRefreshJournal:
    existing = load_basis_refresh(root, slug)
    if existing is not None:
        if existing.old_ticket_generation != old_basis.ticket_identity()["generation"]:
            raise BasisRefreshError("waiting Ticket changed during Basis Refresh")
        return existing
    journal = BasisRefreshJournal(
        1,
        secrets.token_hex(16),
        secrets.token_hex(8),
        slug,
        old_basis.ticket_identity()["generation"],
        "building",
        {},
    )
    _write_journal(root, journal)
    return journal


def _reapply_targets(old: Path, new: Path, plan: TargetPlan | None) -> None:
    if plan is None:
        return
    grouped: dict[Path, list[str]] = {}
    try:
        catalog = TargetCatalog.build(old)
    except FuseSocError as exc:
        raise BasisRefreshError(f"cannot inspect approved Targets: {exc}") from exc
    for entry in plan.entries:
        try:
            handle = catalog.select(entry.target)
            relative = handle.core_file.resolve().relative_to(old.resolve())
        except (ValueError, FuseSocError) as exc:
            raise BasisRefreshError(
                f"cannot recover approved Target {entry.target!r}: {exc}"
            ) from exc
        grouped.setdefault(relative, []).append(handle.name)
    for relative, names in grouped.items():
        source_path = old / relative
        destination_path = new / relative
        for name in names:
            try:
                merge_target_definition(source_path, destination_path, name)
            except (OSError, TargetSurfaceEditError) as exc:
                raise BasisRefreshError(str(exc)) from exc


def _reapply_test_tables(old: Path, new: Path, plan: TargetPlan | None) -> None:
    if plan is None:
        return
    old_path = resolve_checkout_project_dir(old) / "tests.toml"
    new_path = resolve_checkout_project_dir(new) / "tests.toml"
    if not old_path.is_file():
        return
    old_text = old_path.read_text(encoding="utf-8")
    try:
        old_tables = tomllib.loads(old_text)
        new_tables = (
            tomllib.loads(new_path.read_text(encoding="utf-8")) if new_path.is_file() else {}
        )
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BasisRefreshError(f"cannot recover approved tests.toml content: {exc}") from exc
    additions = []
    for entry in plan.entries:
        bare = entry.target.rsplit("#", 1)[-1]
        key = entry.target if entry.target in old_tables else bare
        if key not in old_tables:
            continue
        if key in new_tables:
            if new_tables[key] != old_tables[key]:
                raise BasisRefreshError(
                    f"approved tests.toml table {key!r} conflicts with current destination"
                )
            continue
        try:
            additions.append(toml_table_block(old_text, key))
        except TargetSurfaceEditError as exc:
            raise BasisRefreshError(str(exc)) from exc
    if additions:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        prefix = new_path.read_text(encoding="utf-8").rstrip() if new_path.is_file() else ""
        content = "\n\n".join([prefix, *additions]).lstrip() + "\n"
        atomic_replace_bytes(new_path, content.encode(), mode=0o644)


def _changed_paths(repository: Path, participant: Any) -> tuple[str, ...]:
    return basis_changed_paths(repository, participant.destination_sha, participant.authoring_sha)


def _reapply_placeholders(
    old: AuthoringWorkspace, new: AuthoringWorkspace, basis: TicketBaseline, slug: str
) -> None:
    old_repositories = {"outer": old.outer}
    new_repositories = {"outer": new.outer}
    if old.project is not None and new.project is not None:
        old_repositories["project"] = old.project
        new_repositories["project"] = new.project
    for participant in basis.participants:
        source_root = old_repositories[participant.role]
        destination_root = new_repositories[participant.role]
        for path in _changed_paths(source_root, participant):
            source = source_root / path
            if source.suffix.casefold() == ".core" or source.name == "tests.toml":
                continue
            if not source.is_file() or source.stat().st_size != 0:
                raise BasisRefreshError(
                    f"approved authoring contains non-placeholder content at {path}"
                )
            destination = destination_root / path
            if destination.exists() and destination.stat().st_size != 0:
                raise BasisRefreshError(f"placeholder path now has content: {path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.touch()


def _verify_providers(
    root: Path, workspace: Path, basis: TicketBaseline
) -> tuple[ProviderTargetBinding, ...]:
    tickets = resolve_checkout_project_dir(root) / "tickets"
    verified: dict[str, TicketBaseline] = {}
    refreshed: list[ProviderTargetBinding] = []
    for binding in basis.providers:
        provider_basis = verified.get(binding.provider)
        if provider_basis is None:
            ticket, status = find_ticket_file(tickets, binding.provider)
            if ticket is None or status != "done":
                raise BasisRefreshError(f"provider Ticket {binding.provider!r} is not accepted")
            try:
                provider_document = _converted_ticket(root, ticket, binding.provider)
                provider_basis = load_ticket_baseline_from_document(
                    root, binding.provider, provider_document
                )
            except TicketBaselineError as exc:
                raise BasisRefreshError(
                    f"provider Ticket {binding.provider!r} has no valid accepted basis: {exc}"
                ) from exc
            verified[binding.provider] = provider_basis
        exported = (
            {(entry.target, entry.role.value) for entry in provider_basis.target_plan.entries}
            if provider_basis.target_plan is not None
            else set()
        )
        if (binding.target, binding.role) not in exported:
            raise BasisRefreshError(
                f"provider Ticket {binding.provider!r} no longer exports {binding.target!r}"
            )
        try:
            digest = target_surface_sha256(workspace, binding.target)
        except PlannedDependencyError as exc:
            raise BasisRefreshError(str(exc)) from exc
        if digest != binding.surface_sha256:
            raise BasisRefreshError(
                f"provider Target {binding.target!r} changed after it was pinned"
            )
        if provider_basis.ticket_identity()["generation"] != binding.ticket_generation:
            raise BasisRefreshError(
                f"provider Ticket {binding.provider!r} changed generation after it was pinned"
            )
        refreshed.append(binding)
    return tuple(sorted(refreshed))


def _resume_prepared_refresh(
    root: Path,
    slug: str,
    document: TicketDocument,
    journal: BasisRefreshJournal,
) -> tuple[TicketBaseline, str]:
    candidate = TicketDocument(document.spec, {**document.generated, "machine": journal.machine})
    try:
        basis = load_ticket_baseline_from_document(root, slug, candidate)
    except TicketBaselineError as exc:
        raise BasisRefreshError(str(exc)) from exc
    operation = _operation_path(root, journal.operation_id)
    candidate = operation / "new-outer"
    canonical = resolve_project_dir(root) / "worktrees" / slug
    outer = candidate if candidate.is_dir() else canonical
    paired = paired_project_repository(outer) if outer.is_dir() else None
    has_project = any(row.role == "project" for row in basis.participants)
    workspace = AuthoringWorkspace(
        outer,
        paired.worktree if paired is not None else None,
        basis.participant("outer").destination_sha,
        basis.participant("project").destination_sha if has_project else "",
        journal.generation,
    )
    relocate_refresh_workspace(
        root,
        slug,
        journal.generation,
        operation,
        workspace,
        has_project=has_project,
    )
    return basis, journal.operation_id


def prepare_waiting_basis_refresh(
    project_root: Path,
    ticket_path: Path,
    slug: str,
) -> tuple[TicketBaseline | None, str]:
    """Prepare and relocate a refreshed basis; Board publication remains with the caller."""
    root = project_root.resolve()
    try:
        return _prepare_waiting_basis_refresh(root, ticket_path, slug)
    except BasisRefreshError:
        raise
    except (TicketBaselineOperationError, BasisPublicationError, OSError, ValueError) as exc:
        raise BasisRefreshError(str(exc)) from exc


def _prepare_waiting_basis_refresh(
    root: Path, ticket_path: Path, slug: str
) -> tuple[TicketBaseline | None, str]:
    document = _converted_ticket(root, ticket_path, slug)
    pending = load_basis_refresh(root, slug)
    if pending is not None and pending.state == "prepared":
        return _resume_prepared_refresh(root, slug, document, pending)
    try:
        old_basis = load_ticket_baseline_from_document(root, slug, document)
    except TicketBaselineError as exc:
        raise BasisRefreshError(str(exc)) from exc
    if not old_basis.providers:
        return None, ""
    journal = _new_journal(root, slug, old_basis)
    operation = _operation_path(root, journal.operation_id)
    effective_fields = dict(document.spec.fields)
    old = load_refresh_source_workspace(root, old_basis, slug, operation)
    workspace, provider_bindings = _build_refresh_workspace(
        _RefreshBuild(root, ticket_path, slug, effective_fields, old_basis, old, journal)
    )
    basis, journal = _publish_refresh_basis(
        root, ticket_path, slug, workspace, provider_bindings, journal
    )
    relocate_refresh_workspace(
        root,
        slug,
        journal.generation,
        operation,
        workspace,
        has_project=any(row.role == "project" for row in basis.participants),
    )
    return basis, journal.operation_id


def _build_refresh_workspace(
    refresh: _RefreshBuild,
) -> tuple[AuthoringWorkspace, tuple[ProviderTargetBinding, ...]]:
    workspace = open_authoring_generation(
        refresh.root,
        refresh.ticket,
        refresh.slug,
        refresh.fields,
        refresh.journal.generation,
        refresh.operation / "new-outer",
    )
    provider_bindings = _verify_providers(refresh.root, workspace.outer, refresh.old_basis)
    _reapply_targets(refresh.old.outer, workspace.outer, refresh.old_basis.target_plan)
    _reapply_test_tables(refresh.old.outer, workspace.outer, refresh.old_basis.target_plan)
    _reapply_placeholders(refresh.old, workspace, refresh.old_basis, refresh.slug)
    return workspace, provider_bindings


def _publish_refresh_basis(
    root: Path,
    ticket: Path,
    slug: str,
    workspace: AuthoringWorkspace,
    providers: tuple[ProviderTargetBinding, ...],
    journal: BasisRefreshJournal,
) -> tuple[TicketBaseline, BasisRefreshJournal]:
    if journal.state != "building":
        return ticket_baseline_from_machine(journal.machine), journal
    basis, _operation_id = prepare_replacement_ticket_baseline(
        root, ticket, slug, workspace, providers, operation_id=journal.operation_id
    )
    document = _converted_ticket(root, ticket, slug)
    machine = ticket_machine_from_spec(basis, document.spec, generation=journal.operation_id)
    prepared = journal.prepared(machine)
    _write_journal(root, prepared)
    return basis, prepared


def finish_basis_refresh(root: Path, slug: str, operation_id: str) -> None:
    """Retire refresh journals only after the queued Board pointer is durable."""
    journal = load_basis_refresh(root, slug)
    if journal is None:
        return
    if journal.operation_id != operation_id or journal.state != "prepared":
        raise BasisRefreshError("Basis Refresh completion identity changed")
    from .basis_publication import finish_basis_publication

    finish_basis_publication(root, slug, operation_id)
    safe_rmtree(_operation_path(root, operation_id))
    _journal_path(root, slug).unlink()


def discard_basis_refresh(root: Path, slug: str) -> None:
    """Abandon a failed refresh so the Ticket can return to draft."""
    journal = load_basis_refresh(root, slug)
    if journal is None:
        return
    operation = _operation_path(root, journal.operation_id)
    try:
        repositories = discard_refresh_workspace(root, slug, journal.generation, operation)
        from .basis_publication import abandon_basis_publication

        abandon_basis_publication(root, slug, journal.operation_id, repositories)
        discard_generation_refs(repositories, slug, journal.generation)
        safe_rmtree(operation, protect_git_root=False)
        _journal_path(root, slug).unlink()
    except (OSError, RuntimeError, ValueError) as exc:
        raise BasisRefreshError(str(exc)) from exc


def recover_published_basis_refreshes(root: Path, tickets: list[dict[str, Any]]) -> None:
    """Finish journals whose refreshed Board pointer already reached queue."""
    for ticket in tickets:
        if ticket.get("status") != "queued":
            continue
        slug = str(ticket.get("feature_branch") or "")
        if not slug:
            continue
        journal = load_basis_refresh(root, slug)
        if journal is None:
            continue
        if ticket.get("machine") != journal.machine:
            raise BasisRefreshError(
                f"queued Ticket {slug!r} disagrees with its Basis Refresh journal"
            )
        finish_basis_refresh(root, slug, journal.operation_id)
