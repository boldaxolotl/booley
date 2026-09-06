"""Recoverable pre-execution Acceptance Basis refresh for waiting Tickets."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from booley.core.models import TargetPlan
from booley.fusesoc import fusesoc_registry
from booley.runtime.project_dir import (
    resolve_checkout_project_dir,
    resolve_project_dir,
    runtime_dir,
)
from booley.runtime.ticket_repositories import (
    paired_project_repository,
    resolve_inner_project_repo,
    ticket_project_worktree,
)

from .acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    ProviderTargetBinding,
    load_acceptance_basis,
    write_basis_receipt,
)
from .frontmatter import parse_frontmatter
from .persistence import atomic_replace_bytes
from .planned_dependencies import PlannedDependencyError, target_surface_sha256
from .scanner import find_ticket_file
from .target_surface_edit import TargetSurfaceEditError, merge_target_definition
from .workspace_ops import (
    AcceptanceBasisOperationError,
    AuthoringWorkspace,
    _generation_branch,
    _open_generation,
    _remove_authoring_worktrees,
    _require_git,
    _worktree_owns_branch,
    prepare_replacement_acceptance_basis,
)

_OPERATION_RE = re.compile(r"[0-9a-f]{32}")
_GENERATION_RE = re.compile(r"[0-9a-f]{16}")


class BasisRefreshError(RuntimeError):
    """A waiting Ticket cannot be refreshed without new author approval."""


@dataclass(frozen=True)
class BasisRefreshJournal:
    """Stable identities needed to resume refresh publication and relocation."""

    schema: int
    operation_id: str
    generation: str
    slug: str
    old_basis_id: str
    state: str
    new_basis: dict[str, Any]
    receipt: dict[str, Any]

    def prepared(self, basis: AcceptanceBasis, receipt: dict[str, Any]) -> BasisRefreshJournal:
        return replace(self, state="prepared", new_basis=basis.as_dict(), receipt=receipt)


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
        if set(value) != set(BasisRefreshJournal.__dataclass_fields__):
            raise ValueError("invalid fields")
        journal = BasisRefreshJournal(**value)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BasisRefreshError(f"Basis Refresh journal is invalid: {path}") from exc
    if (
        journal.schema != 1
        or journal.slug != slug
        or journal.state not in {"building", "prepared"}
        or not _OPERATION_RE.fullmatch(journal.operation_id)
        or not _GENERATION_RE.fullmatch(journal.generation)
        or not re.fullmatch(r"[0-9a-f]{64}", journal.old_basis_id)
    ):
        raise BasisRefreshError(f"Basis Refresh journal identity is invalid: {path}")
    if journal.state == "prepared":
        try:
            AcceptanceBasis.from_mapping(journal.new_basis)
        except AcceptanceBasisError as exc:
            raise BasisRefreshError("Basis Refresh journal has an invalid new basis") from exc
    elif journal.new_basis or journal.receipt:
        raise BasisRefreshError("building Basis Refresh journal contains published output")
    return journal


def _new_journal(root: Path, slug: str, old_basis: AcceptanceBasis) -> BasisRefreshJournal:
    existing = load_basis_refresh(root, slug)
    if existing is not None:
        if existing.old_basis_id != old_basis.basis_id:
            raise BasisRefreshError("waiting Ticket changed during Basis Refresh")
        return existing
    journal = BasisRefreshJournal(
        1,
        secrets.token_hex(16),
        secrets.token_hex(8),
        slug,
        old_basis.basis_id,
        "building",
        {},
        {},
    )
    _write_journal(root, journal)
    return journal


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BasisRefreshError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout.strip()


def _require_untouched(root: Path, basis: AcceptanceBasis, slug: str) -> AuthoringWorkspace:
    outer = resolve_project_dir(root) / "worktrees" / slug
    paired = paired_project_repository(outer)
    project = paired.worktree if paired is not None else None
    repositories = {"outer": outer}
    if project is not None:
        repositories["project"] = project
    if set(repositories) != {row.role for row in basis.participants}:
        raise BasisRefreshError("waiting Ticket workspace participants changed")
    for participant in basis.participants:
        repository = repositories[participant.role]
        if _git(repository, "rev-parse", "HEAD") != participant.authoring_sha:
            raise BasisRefreshError("waiting Ticket workspace was already executed or changed")
        if _git(repository, "status", "--porcelain", "--untracked-files=all"):
            raise BasisRefreshError("waiting Ticket workspace is not pristine")
    project_base = basis.participant("project").destination_sha if project is not None else ""
    return AuthoringWorkspace(
        outer,
        project,
        basis.participant("outer").destination_sha,
        project_base,
    )


def _reapply_targets(old: Path, new: Path, plan: TargetPlan | None) -> None:
    if plan is None:
        return
    grouped: dict[Path, list[str]] = {}
    for entry in plan.entries:
        try:
            ref = fusesoc_registry.resolve_ref(old, entry.target)
            relative = ref.core_file.resolve().relative_to(old.resolve())
        except (ValueError, fusesoc_registry.FuseSocError) as exc:
            raise BasisRefreshError(
                f"cannot recover approved Target {entry.target!r}: {exc}"
            ) from exc
        grouped.setdefault(relative, []).append(ref.name)
    for relative, names in grouped.items():
        source_path = old / relative
        destination_path = new / relative
        for name in names:
            try:
                merge_target_definition(source_path, destination_path, name)
            except (OSError, TargetSurfaceEditError) as exc:
                raise BasisRefreshError(str(exc)) from exc


def _toml_table_block(content: str, key: str) -> str:
    header = re.compile(rf"(?m)^\[{re.escape(key)}\]\s*(?:#.*)?$")
    match = header.search(content)
    if match is None:
        quoted = re.compile(rf'(?m)^\["{re.escape(key)}"\]\s*(?:#.*)?$')
        match = quoted.search(content)
    if match is None:
        raise BasisRefreshError(f"approved tests.toml table {key!r} disappeared")
    following = re.search(r"(?m)^\[[^[][^]]*\]\s*(?:#.*)?$", content[match.end() :])
    end = match.end() + following.start() if following is not None else len(content)
    return content[match.start() : end].strip() + "\n"


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
        if key not in old_tables or key in new_tables:
            continue
        additions.append(_toml_table_block(old_text, key))
    if additions:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        prefix = new_path.read_text(encoding="utf-8").rstrip() if new_path.is_file() else ""
        new_path.write_text("\n\n".join([prefix, *additions]).lstrip() + "\n", encoding="utf-8")


def _changed_paths(repository: Path, participant: Any) -> tuple[str, ...]:
    output = _git(
        repository,
        "diff",
        "--name-only",
        participant.destination_sha,
        participant.authoring_sha,
        "--",
    )
    return tuple(line for line in output.splitlines() if line)


def _reapply_placeholders(
    old: AuthoringWorkspace, new: AuthoringWorkspace, basis: AcceptanceBasis, slug: str
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
            if path.replace("\\", "/").endswith(f"acceptance/bases/{slug}.json"):
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
    root: Path, workspace: Path, basis: AcceptanceBasis
) -> tuple[ProviderTargetBinding, ...]:
    tickets = resolve_checkout_project_dir(root) / "tickets"
    verified: dict[str, AcceptanceBasis] = {}
    refreshed: list[ProviderTargetBinding] = []
    for binding in basis.providers:
        provider_basis = verified.get(binding.provider)
        if provider_basis is None:
            ticket, status = find_ticket_file(tickets, binding.provider)
            if ticket is None or status != "done":
                raise BasisRefreshError(f"provider Ticket {binding.provider!r} is not accepted")
            fields, body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
            try:
                provider_basis = load_acceptance_basis(root, binding.provider, fields, body)
            except AcceptanceBasisError as exc:
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
        refreshed.append(replace(binding, basis_id=provider_basis.basis_id))
    return tuple(sorted(refreshed))


def _relocate_workspace(
    root: Path,
    slug: str,
    journal: BasisRefreshJournal,
    old: AuthoringWorkspace | None,
    new: AuthoringWorkspace,
    *,
    has_project: bool,
) -> None:
    canonical = resolve_project_dir(root) / "worktrees" / slug
    branch = _generation_branch(journal.generation, slug)
    operation = _operation_path(root, journal.operation_id)
    project_source = resolve_inner_project_repo(root)
    holding = operation / "new-project-moving"
    canonical_is_new = canonical.is_dir() and _worktree_owns_branch(root, canonical, branch)
    if not canonical_is_new:
        new_paired = paired_project_repository(new.outer) if new.outer.is_dir() else None
        if new_paired is not None:
            if project_source is None:
                raise BasisRefreshError("paired project repository is unavailable")
            if not holding.exists():
                _require_git(
                    project_source,
                    "worktree",
                    "move",
                    str(new_paired.worktree),
                    str(holding),
                )
        if canonical.exists():
            old_paired = paired_project_repository(canonical)
            _remove_authoring_worktrees(root, canonical, old_paired, project_source)
        if not new.outer.is_dir():
            raise BasisRefreshError("refreshed Ticket workspace disappeared during relocation")
        _require_git(root, "worktree", "move", str(new.outer), str(canonical))
    if holding.exists() and project_source is not None:
        destination = ticket_project_worktree(canonical)
        _require_git(project_source, "worktree", "move", str(holding), str(destination))
    if has_project and paired_project_repository(canonical) is None:
        raise BasisRefreshError("refreshed paired project workspace is unavailable")
    if not has_project and old is not None and old.project is not None:
        raise BasisRefreshError("Basis Refresh changed repository participation")


def _resume_prepared_refresh(
    root: Path,
    slug: str,
    fields: dict[str, Any],
    body: str,
    journal: BasisRefreshJournal,
) -> tuple[AcceptanceBasis, str]:
    candidate_fields = {**fields, "acceptance_basis": journal.new_basis}
    try:
        basis = load_acceptance_basis(root, slug, candidate_fields, body)
    except AcceptanceBasisError as exc:
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
    _relocate_workspace(
        root,
        slug,
        journal,
        None,
        workspace,
        has_project=has_project,
    )
    return basis, journal.operation_id


def prepare_waiting_basis_refresh(
    project_root: Path,
    ticket_path: Path,
    slug: str,
) -> tuple[AcceptanceBasis | None, str]:
    """Prepare and relocate a refreshed basis; Board publication remains with the caller."""
    root = project_root.resolve()
    fields, body = parse_frontmatter(ticket_path.read_text(encoding="utf-8"))
    pending = load_basis_refresh(root, slug)
    if pending is not None and pending.state == "prepared":
        return _resume_prepared_refresh(root, slug, fields, body, pending)
    try:
        old_basis = load_acceptance_basis(root, slug, fields, body)
    except AcceptanceBasisError as exc:
        raise BasisRefreshError(str(exc)) from exc
    if not old_basis.providers:
        return None, ""
    old = _require_untouched(root, old_basis, slug)
    journal = _new_journal(root, slug, old_basis)
    operation = _operation_path(root, journal.operation_id)
    new_outer = operation / "new-outer"
    effective_fields = dict(fields)
    effective_fields.pop("acceptance_basis", None)
    try:
        workspace = _open_generation(
            root,
            ticket_path,
            slug,
            effective_fields,
            journal.generation,
            new_outer,
        )
        provider_bindings = _verify_providers(root, workspace.outer, old_basis)
        _reapply_targets(old.outer, workspace.outer, old_basis.target_plan)
        _reapply_test_tables(old.outer, workspace.outer, old_basis.target_plan)
        _reapply_placeholders(old, workspace, old_basis, slug)
        if journal.state == "building":
            basis, operation_id = prepare_replacement_acceptance_basis(
                root,
                ticket_path,
                slug,
                workspace,
                provider_bindings,
                operation_id=journal.operation_id,
            )
            receipt = write_basis_receipt(
                root,
                slug,
                basis,
                source_sha256=hashlib.sha256(ticket_path.read_bytes()).hexdigest(),
                operation_id=operation_id,
            )
            journal = journal.prepared(basis, receipt)
            _write_journal(root, journal)
        else:
            basis = AcceptanceBasis.from_mapping(journal.new_basis)
        _relocate_workspace(
            root,
            slug,
            journal,
            old,
            workspace,
            has_project=any(row.role == "project" for row in basis.participants),
        )
    except (AcceptanceBasisOperationError, OSError, ValueError) as exc:
        raise BasisRefreshError(str(exc)) from exc
    return basis, journal.operation_id


def finish_basis_refresh(root: Path, slug: str, operation_id: str) -> None:
    """Retire refresh journals only after the queued Board pointer is durable."""
    journal = load_basis_refresh(root, slug)
    if journal is None:
        return
    if journal.operation_id != operation_id or journal.state != "prepared":
        raise BasisRefreshError("Basis Refresh completion identity changed")
    from .basis_publication import finish_basis_publication

    finish_basis_publication(root, slug, operation_id)
    _journal_path(root, slug).unlink()


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
        if ticket.get("acceptance_basis") != journal.new_basis:
            raise BasisRefreshError(
                f"queued Ticket {slug!r} disagrees with its Basis Refresh journal"
            )
        finish_basis_refresh(root, slug, journal.operation_id)
