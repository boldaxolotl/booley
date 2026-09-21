"""Validate authored Ticket contracts and prepared executable Ticket views."""

from __future__ import annotations

import tempfile
from pathlib import Path

from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

from . import workspace_ops
from .acceptance_targets import deferable_rtl_or_tb_input
from .acceptance_validation import prepare_acceptance_checkout
from .io import TicketIO
from .planned_dependencies import (
    PlannedDependencyError,
    target_surface_sha256,
    validate_planned_dependencies,
)
from .scanner import find_ticket_file
from .ticket_baseline import (
    TicketBaseline,
    TicketBaselineError,
    assert_live_inputs_unchanged,
    load_ticket_baseline_from_document,
    materialize_current_ticket_checkout,
    validate_ticket_view,
)
from .ticket_document import TicketDocument, convert_ticket_document, ticket_conversion_context
from .validation import owned_draft_dirty_paths, validate_git_state, validate_ticket_spec
from .workspace_ops import (
    TicketBaselineOperationError,
    _validate_converted_basis_targets,
    load_draft_generation,
    validate_draft_target_plan,
)

_EXECUTABLE_DIRS = frozenset(
    {"queue", "waiting", "active", "blocked", "review", "done", "archived"}
)

_OPERATIONAL_STATUSES = frozenset({"queued", "queue", "running", "active", "blocked"})


def validate_executable_ticket(
    project_root: Path,
    slug: str,
    *,
    runtime_ticket_path: Path | None = None,
) -> list[str]:
    """Validate one executable Ticket in a prepared current-generation checkout."""
    root = Path(project_root).resolve()
    tickets_dir = resolve_checkout_project_dir(root) / "tickets"
    ticket, status = find_ticket_file(tickets_dir, slug, project_root=root)
    if ticket is None:
        return [f"executable Ticket Board entry {slug!r} is unavailable"]
    if status not in _OPERATIONAL_STATUSES:
        return [f"ticket {slug!r} is not operationally executable (status: {status})"]

    try:
        document = _convert_executable_ticket(root, ticket, slug)
        basis = TicketIO(tickets_dir, project_root=root).load_basis(
            slug,
            runtime_ticket_path=runtime_ticket_path,
        )
        return _validate_prepared_checkout(root, ticket, slug, document, basis)
    except (TicketBaselineError, PlannedDependencyError, FuseSocError, OSError, ValueError) as exc:
        return [str(exc)]


def _convert_executable_ticket(root: Path, ticket: Path, slug: str) -> TicketDocument:
    with ticket_conversion_context(root, slug, "executable") as context:
        converted = convert_ticket_document(ticket.read_text(encoding="utf-8"), context)
    if converted.document is None:
        details = "; ".join(
            f"{item.line}:{item.column}: {item.message}" for item in converted.diagnostics
        )
        raise TicketBaselineError(details)
    return converted.document


def _validate_prepared_checkout(
    root: Path,
    ticket: Path,
    slug: str,
    document: TicketDocument,
    basis: TicketBaseline,
) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="booley-operational-basis-") as directory:
        checkout = materialize_current_ticket_checkout(root, basis, Path(directory) / "checkout")
        prepare_acceptance_checkout(root, checkout, slug=slug, ticket_path=ticket)
        placeholders = _published_provider_placeholders(checkout, basis)
        errors = validate_ticket_spec(
            document.spec,
            check_files=True,
            check_git=False,
            project_root=checkout,
            provider_placeholders=placeholders,
        )
        errors.extend(validate_ticket_view(checkout, basis))
        _assert_live_inputs_unchanged(basis, root, checkout)
        return errors


def _assert_live_inputs_unchanged(
    basis: TicketBaseline, root: Path, checkout: Path
) -> None:
    """Check any still-mounted authoring worktrees without requiring one to exist."""
    try:
        assert_live_inputs_unchanged(basis, root, checkout)
    except TicketBaselineError as exc:
        if "registered worktree" not in str(exc) or "is unavailable at" not in str(exc):
            raise


def validate_ticket_document(
    project_root: Path,
    path: Path,
    tickets_dir: Path,
    *,
    check_git: bool = False,
) -> list[str]:
    """Validate the authored contract without publishing or refreshing a generation."""
    if not path.is_file():
        return [f"File not found: {path}"]
    stage = "executable" if path.parent.name in _EXECUTABLE_DIRS else "draft"
    git_marker = project_root / ".git"
    workspace = resolve_project_dir(project_root) / "worktrees" / path.stem
    if stage == "draft" and git_marker.exists() and not workspace.is_dir():
        try:
            workspace_ops.ensure_ticket_workspace(project_root, path, path.stem)
        except (RuntimeError, ValueError, OSError) as exc:
            return [f"Ticket workspace preparation failed: {exc}"]
    has_git = git_marker.is_file() or (git_marker / "HEAD").is_file()

    with ticket_conversion_context(project_root, path.stem, stage) as context:
        converted = convert_ticket_document(path.read_text(encoding="utf-8"), context)
        if converted.document is None:
            return [f"{item.line}:{item.column}: {item.message}" for item in converted.diagnostics]
        assert context.checkout_root is not None
        checkout = context.checkout_root()
        document = converted.document
        if stage == "draft" and has_git:
            errors = _validate_draft(project_root, path, checkout, document)
        elif stage == "executable" and has_git:
            errors = _validate_published(project_root, path, checkout, document)
        else:
            errors = validate_ticket_spec(document.spec, project_root=checkout)

    if check_git:
        allowed = owned_draft_dirty_paths(path, tickets_dir)
        errors.extend(validate_git_state(dict(document.spec.fields), project_root, allowed))
    return errors


def _validate_draft(
    project_root: Path, path: Path, checkout: Path, document: TicketDocument
) -> list[str]:
    try:
        generation = load_draft_generation(project_root, path.stem)
        providers = validate_planned_dependencies(project_root, path.stem, generation, path)
        plan = validate_draft_target_plan(
            project_root,
            checkout,
            path,
            generation,
            providers,
            document.spec,
        )
        errors = validate_ticket_spec(
            document.spec,
            project_root=checkout,
            provider_placeholders=providers.placeholder_paths,
        )
        if errors:
            return errors
        _validate_converted_basis_targets(document.spec, checkout, providers, plan)
    except (TicketBaselineOperationError, PlannedDependencyError, FuseSocError, OSError) as exc:
        return [str(exc)]
    return []


def _validate_published(
    project_root: Path, path: Path, checkout: Path, document: TicketDocument
) -> list[str]:
    try:
        basis = load_ticket_baseline_from_document(
            project_root, path.stem, document, authoring_checkout=checkout
        )
        placeholders = _published_provider_placeholders(checkout, basis)
    except (TicketBaselineError, PlannedDependencyError, FuseSocError, OSError) as exc:
        return [str(exc)]
    return validate_ticket_spec(
        document.spec, project_root=checkout, provider_placeholders=placeholders
    )


def _published_provider_placeholders(checkout: Path, basis: TicketBaseline) -> frozenset[str]:
    """Recover deferable provider inputs from the pinned, verified Target surfaces."""
    catalog = TargetCatalog.build(checkout)
    placeholders: set[str] = set()
    for binding in basis.providers:
        if target_surface_sha256(checkout, binding.target) != binding.surface_sha256:
            raise PlannedDependencyError(
                f"published provider Target {binding.target!r} differs from its pinned surface"
            )
        handle = catalog.select(binding.target)
        for item in catalog.inspect(handle).inputs:
            candidate = Path(item.path)
            if not candidate.is_absolute():
                candidate = catalog.project_root / candidate
            if not candidate.exists() and deferable_rtl_or_tb_input(item):
                placeholders.add(item.path)
    return frozenset(placeholders)
