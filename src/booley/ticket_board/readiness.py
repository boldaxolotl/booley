"""Side-effect-limited, no-agent readiness checks for one ticket."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.project_prepare import prepare_project
from booley.runtime.ticket_repositories import resolve_inner_project_repo

from .frontmatter import parse_frontmatter
from .scanner import find_ticket_file
from .target_contract import (
    TargetContract,
    TargetContractError,
    resolve_commit,
    validate_targets_for_seal,
    verify_surface,
)
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


def _validate_checkout_contract(root: Path, fields: dict[str, object]) -> list[str]:
    """Validate a seal from a clean checkout without authoring worktrees."""
    if not (root / ".git").exists():
        return []
    raw = fields.get("target_contract")
    if raw is None:
        return ["target_contract.schema: 1 is required for readiness"]
    try:
        contract = TargetContract.from_mapping(raw)
        resolve_commit(root, contract.outer_sha)
        if contract.project_sha:
            project_repository = resolve_inner_project_repo(root)
            if project_repository is None:
                return ["target_contract.project_sha is set but project repository is missing"]
            resolve_commit(project_repository, contract.project_sha)
        verify_surface(contract, root)
    except (TargetContractError, OSError, ValueError) as exc:
        return [str(exc)]
    return []


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
    errors.extend(_validate_checkout_contract(root, fields))
    if not errors:
        with tempfile.TemporaryDirectory(prefix="booley-ready-") as build_root:
            errors.extend(validate_targets_for_seal(fields, root, build_root))
    return ReadinessResult(ticket, tuple(errors), warnings)
