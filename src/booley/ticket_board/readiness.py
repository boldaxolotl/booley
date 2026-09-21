"""Side-effect-limited, no-agent readiness checks for one ticket."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.project_prepare import prepare_project
from booley.ticket_board.ticket_repositories import resolve_inner_project_repo

from .scanner import find_ticket_file
from .ticket_document import (
    convert_ticket_document,
    ticket_conversion_context,
)
from .ticket_validation import validate_executable_ticket
from .validation import validate_ticket_spec


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


def check_ticket_ready(project_root: Path | str, slug: str) -> ReadinessResult:
    """Prepare and validate one ticket without agents or board transitions."""
    root = Path(project_root).resolve()
    tickets_dir = resolve_checkout_project_dir(root) / "tickets"
    ticket, _status = find_ticket_file(tickets_dir, slug)
    if ticket is None:
        return ReadinessResult(None, (f"ticket {slug!r} not found",))

    if (root / ".git").exists():
        if _status not in {"queued", "queue", "running", "active", "blocked"}:
            return ReadinessResult(
                ticket,
                (f"ticket {slug!r} is not executable (status: {_status})",),
            )
        results = validate_executable_ticket(root, slug)
    else:
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
        with ticket_conversion_context(root, slug, "draft") as context:
            conversion = convert_ticket_document(ticket.read_text(encoding="utf-8"), context)
        if conversion.document is None:
            return ReadinessResult(ticket, _conversion_errors(conversion))
        results = validate_ticket_spec(
            conversion.document.spec,
            check_files=True,
            check_git=False,
            project_root=root,
        )
    warnings = tuple(item for item in results if item.startswith("[warning] "))
    errors = [item for item in results if not item.startswith("[warning] ")]
    return ReadinessResult(ticket, tuple(errors), warnings)


def _conversion_errors(conversion) -> tuple[str, ...]:
    return tuple(f"{item.line}:{item.column}: {item.message}" for item in conversion.diagnostics)
