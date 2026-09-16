"""Validate one Ticket against its authored Target view."""

from __future__ import annotations

from pathlib import Path

from booley.runtime.project_dir import resolve_project_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

from . import workspace_ops
from .acceptance_targets import deferable_rtl_or_tb_input
from .planned_dependencies import (
    PlannedDependencyError,
    target_surface_sha256,
    validate_planned_dependencies,
)
from .ticket_baseline import (
    TicketBaseline,
    TicketBaselineError,
    load_ticket_baseline_from_document,
)
from .ticket_document import TicketDocument, convert_ticket_document, ticket_conversion_context
from .validation import owned_draft_dirty_paths, validate_git_state, validate_ticket_spec
from .workspace_ops import (
    TicketBaselineOperationError,
    _validate_converted_basis_spec,
    load_draft_generation,
    validate_ticket_spec_authoring_inputs,
)

_EXECUTABLE_DIRS = frozenset(
    {"queue", "waiting", "active", "blocked", "review", "done", "archived"}
)


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
            return [
                f"{item.line}:{item.column}: {item.message}" for item in converted.diagnostics
            ]
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
        plan = validate_ticket_spec_authoring_inputs(
            project_root,
            checkout,
            document.spec,
            ticket_path=path,
            generation=generation,
            providers=providers,
        )
        errors = validate_ticket_spec(
            document.spec,
            project_root=checkout,
            provider_placeholders=providers.placeholder_paths,
        )
        if errors:
            return errors
        _validate_converted_basis_spec(document.spec, checkout, providers, plan)
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


def _published_provider_placeholders(
    checkout: Path, basis: TicketBaseline
) -> frozenset[str]:
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
