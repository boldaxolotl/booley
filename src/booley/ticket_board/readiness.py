"""Side-effect-limited, no-agent readiness checks for one ticket."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.project_prepare import prepare_project
from booley.runtime.ticket_repositories import resolve_inner_project_repo

from .acceptance_basis import (
    AcceptanceBasisError,
    assert_inputs_unchanged,
)
from .acceptance_targets import resolve_commit, validate_targets_for_seal
from .frontmatter import parse_frontmatter
from .scanner import find_ticket_file
from .validation import validate_ticket_fields


@dataclass(frozen=True)
class ReadinessResult:
    """Machine-readable readiness outcome."""

    ticket: Path | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors


class ReadinessInspectionError(RuntimeError):
    """Git state required for readiness could not be inspected."""


def _checkout_statuses(root: Path) -> tuple[str, ...]:
    """Capture Git-visible state across the outer and optional project repo."""
    repositories = [root]
    project_repository = resolve_inner_project_repo(root)
    if project_repository is not None:
        repositories.append(project_repository)
    statuses: list[str] = []
    for repository in repositories:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise ReadinessInspectionError(
                f"git status failed in {repository} (rc={result.returncode}): {detail}"
            )
        statuses.append(result.stdout)
    return tuple(statuses)


def _validate_checkout_basis(
    root: Path,
    tickets_dir: Path,
    slug: str,
    fields: dict[str, object],
) -> list[str]:
    """Load the authoritative basis, then validate its clean authoring checkout."""
    if not (root / ".git").exists():
        return []
    if fields.get("target_contract") is not None:
        return ["legacy Target Contract tickets are unsupported after the hard cutoff"]
    if fields.get("acceptance_basis") is None:
        return ["executable Ticket has no Acceptance Basis"]
    try:
        from .io import TicketIO

        contract = TicketIO(tickets_dir, project_root=root).load_basis(slug)
        resolve_commit(root, contract.outer_sha)
        if contract.project_sha:
            project_repository = resolve_inner_project_repo(root)
            if project_repository is None:
                return ["Acceptance Basis project participant repository is missing"]
            resolve_commit(project_repository, contract.project_sha)
        inspection_root = _worktree_for_ref(root, contract.participant("outer").ticket_ref)
        assert_inputs_unchanged(contract, inspection_root or root)
    except (AcceptanceBasisError, OSError, ValueError) as exc:
        return [str(exc)]
    return []


def _worktree_for_ref(repository: Path, ref: str) -> Path | None:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None
    worktree: Path | None = None
    for line in [*result.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree "))
        elif line == f"branch {ref}":
            return worktree
        elif not line:
            worktree = None
    return None


def check_ticket_ready(project_root: Path | str, slug: str) -> ReadinessResult:
    """Prepare and validate one ticket without agents or board transitions."""
    root = Path(project_root).resolve()
    tickets_dir = resolve_checkout_project_dir(root) / "tickets"
    ticket, _status = find_ticket_file(tickets_dir, slug)
    if ticket is None:
        return ReadinessResult(None, (f"ticket {slug!r} not found",))

    fields, body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    from booley.flows.execution import flow_enabled

    status_before = _checkout_statuses(root)
    preparation = prepare_project(
        root,
        root,
        slug=slug,
        ticket_path=ticket,
        sim_flow_enabled=flow_enabled("sim", root),
    )
    if not preparation.ok:
        return ReadinessResult(ticket, (preparation.error,))
    if _checkout_statuses(root) != status_before:
        return ReadinessResult(
            ticket,
            ("project preparation changed Git-visible checkout state",),
        )

    results = validate_ticket_fields(
        fields,
        body,
        check_files=True,
        check_git=False,
        project_root=root,
        check_tb_files=True,
    )
    warnings = tuple(item for item in results if item.startswith("[warning] "))
    errors = [item for item in results if not item.startswith("[warning] ")]
    errors.extend(_validate_checkout_basis(root, tickets_dir, slug, fields))
    if not errors:
        with tempfile.TemporaryDirectory(prefix="booley-ready-") as build_root:
            errors.extend(validate_targets_for_seal(fields, root, build_root))
    return ReadinessResult(ticket, tuple(errors), warnings)
