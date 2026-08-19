"""Repository routing for Ticket Mode's paired project-data checkout."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.project_dir import PROJECT_DIR_NAME, resolve_project_dir

PROJECT_BRANCH_PREFIX = "booley-ticket/"


@dataclass(frozen=True)
class TicketRepository:
    """One repository participating in a ticket handoff."""

    worktree: Path
    path_prefix: str = ""

    def local_path(self, ticket_path: str) -> str:
        """Translate a ticket-root-relative path into this repository."""
        if not self.path_prefix:
            return ticket_path
        prefix = f"{self.path_prefix}/"
        if not ticket_path.startswith(prefix):
            raise ValueError(f"path {ticket_path!r} is outside {self.path_prefix!r}")
        return ticket_path.removeprefix(prefix)

    def ticket_path(self, local_path: str) -> str:
        """Translate a repository-relative path into the ticket checkout."""
        return f"{self.path_prefix}/{local_path}" if self.path_prefix else local_path


def project_ticket_branch(slug: str) -> str:
    """Return the isolated inner-repository branch for *slug*."""
    return f"{PROJECT_BRANCH_PREFIX}{slug}"


def ticket_project_worktree(ticket_worktree: Path) -> Path:
    """Return the conventional project-data checkout nested in a ticket."""
    return ticket_worktree / PROJECT_DIR_NAME


def scope_mentions_project_repo(scope: list[str]) -> bool:
    """Whether ticket Scope explicitly names project-directory content."""
    return bool(project_repository_scope(scope))


def project_repository_scope(scope: list[str]) -> list[str]:
    """Translate ticket Scope entries for the inner repository's hook."""
    prefix = f"{PROJECT_DIR_NAME}/"
    translated: list[str] = []
    for raw_entry in scope:
        is_new = raw_entry.endswith(" [new]")
        entry = raw_entry.removesuffix(" [new]").removeprefix("./")
        if entry.rstrip("/") == PROJECT_DIR_NAME:
            translated.append("** [new]" if is_new else "**")
            continue
        if not entry.startswith(prefix):
            continue
        local = entry.removeprefix(prefix)
        translated.append(f"{local} [new]" if is_new else local)
    return translated


def resolve_inner_project_repo(project_root: Path) -> Path | None:
    """Return the resolved project dir only when it is its own Git repo."""
    configured = os.environ.get("BOOLEY_PROJECT_DIR")
    if configured and not Path(configured).is_dir():
        return None
    try:
        project_dir = resolve_project_dir(project_root).resolve()
    except FileNotFoundError:
        return None
    if not (project_dir / ".git").is_dir():
        return None
    result = _git(project_dir, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    try:
        top = Path(result.stdout.strip()).resolve()
    except OSError:
        return None
    return project_dir if top == project_dir else None


def project_repository_expected(project_root: Path) -> bool:
    """Whether this checkout is configured for a standalone project repository."""
    configured = os.environ.get("BOOLEY_PROJECT_DIR")
    if configured:
        configured_path = Path(configured)
        if (configured_path / ".git").exists():
            return True
    result = _git(project_root, "check-ignore", "-q", "--", PROJECT_DIR_NAME)
    return result.returncode == 0


def paired_project_repository(ticket_worktree: Path) -> TicketRepository | None:
    """Return the ticket's linked inner worktree, when one is installed."""
    nested = ticket_project_worktree(ticket_worktree)
    if not (nested / ".git").is_file():
        return None
    result = _git(nested, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    try:
        top = Path(result.stdout.strip()).resolve()
    except OSError:
        return None
    if top != nested.resolve():
        return None
    return TicketRepository(nested, PROJECT_DIR_NAME)


def ticket_repositories(ticket_worktree: Path) -> tuple[TicketRepository, ...]:
    """Return every repository whose dirty state belongs to one ticket."""
    outer = TicketRepository(ticket_worktree)
    project = paired_project_repository(ticket_worktree)
    return (outer, project) if project is not None else (outer,)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
