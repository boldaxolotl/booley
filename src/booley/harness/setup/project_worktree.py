"""Create the paired inner-repository worktree used by stealth tickets."""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.harness.models import TicketContext
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_dir import PROJECT_DIR_NAME
from booley.runtime.ticket_repositories import (
    paired_project_repository,
    project_ticket_branch,
    resolve_inner_project_repo,
    scope_mentions_project_repo,
    ticket_project_worktree,
)


class ProjectWorktreeError(RuntimeError):
    """Raised when an explicitly scoped project repository cannot be isolated."""


def prepare_project_worktree(ctx: TicketContext) -> Path | None:
    """Install a linked inner-repository checkout for scoped stealth content."""
    if ctx.worktree_path is None:
        return None
    source = resolve_inner_project_repo(ctx.project_root)
    existing = paired_project_repository(ctx.worktree_path)
    if existing is not None:
        if source is None:
            raise ProjectWorktreeError(
                "paired project checkout exists but its source repository is unavailable"
            )
        _verify_existing_worktree(existing.worktree, source, ctx.slug)
        return existing.worktree

    if source is None:
        if scope_mentions_project_repo(ctx.scope_raw) and _outer_ignores_project_dir(
            ctx.project_root
        ):
            raise ProjectWorktreeError(
                f"Scope names {PROJECT_DIR_NAME}/ but the hidden project directory "
                "is not a standalone Git repository; run `booley init` and commit "
                "the project repository before retrying"
            )
        return None

    _require_clean_source(source)
    destination = ticket_project_worktree(ctx.worktree_path)
    branch = project_ticket_branch(ctx.slug)
    base = _ticket_base_branch(source, ctx.branch)
    _prepare_destination(destination)
    _git(source, "worktree", "prune")
    _attach_branch(source, destination, branch, base, resume=ctx.workspace_intent == "resume")
    _set_local_upstream(source, branch, base)
    return destination


def remove_project_worktree(project_root: Path, ticket_worktree: Path) -> None:
    """Remove a nested inner worktree before its containing checkout disappears."""
    nested = ticket_project_worktree(ticket_worktree)
    source = resolve_inner_project_repo(project_root)
    if source is None or not (nested / ".git").is_file():
        return
    _git(source, "worktree", "remove", "--force", str(nested))
    _git(source, "worktree", "prune")


def _outer_ignores_project_dir(project_root: Path) -> bool:
    result = _git(project_root, "check-ignore", "-q", "--", PROJECT_DIR_NAME)
    return result.returncode == 0


def _require_clean_source(source: Path) -> None:
    result = _git(source, "status", "--porcelain", "--untracked-files=all")
    if result.returncode != 0:
        raise ProjectWorktreeError(f"could not inspect project repository: {result.stderr.strip()}")
    if result.stdout.strip():
        raise ProjectWorktreeError(
            f"project repository at {source} has uncommitted changes; commit or restore "
            "them before starting or resuming a ticket"
        )


def _current_branch(source: Path) -> str:
    result = _git(source, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        raise ProjectWorktreeError("project repository must have a checked-out base branch")
    return result.stdout.strip()


def _ticket_base_branch(source: Path, requested: str) -> str:
    """Use the ticket's lifecycle branch when the project repository has it."""
    if requested and _ref_sha(source, f"refs/heads/{requested}"):
        return requested
    return _current_branch(source)


def _prepare_destination(destination: Path) -> None:
    if not destination.exists():
        return
    if (destination / ".git").exists():
        raise ProjectWorktreeError(
            f"refusing to replace unexpected Git checkout at {destination}"
        )
    safe_rmtree(destination)


def _attach_branch(
    source: Path,
    destination: Path,
    branch: str,
    base: str,
    *,
    resume: bool,
) -> None:
    branch_sha = _ref_sha(source, f"refs/heads/{branch}")
    base_sha = _ref_sha(source, f"refs/heads/{base}")
    if branch_sha and not resume and branch_sha != base_sha:
        raise ProjectWorktreeError(
            f"refusing to reset surviving project branch {branch!r}; resume or triage it first"
        )
    args = ("worktree", "add", str(destination), branch)
    if not branch_sha:
        args = ("worktree", "add", "-b", branch, str(destination), base)
    result = _git(source, *args)
    if result.returncode != 0:
        raise ProjectWorktreeError(
            f"could not create paired project worktree (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def _set_local_upstream(source: Path, branch: str, base: str) -> None:
    result = _git(source, "branch", f"--set-upstream-to={base}", branch)
    if result.returncode != 0:
        raise ProjectWorktreeError(
            f"could not record project merge target {base!r}: {result.stderr.strip()}"
        )


def _verify_existing_worktree(worktree: Path, source: Path, slug: str) -> None:
    result = _git(worktree, "branch", "--show-current")
    expected = project_ticket_branch(slug)
    if result.returncode != 0 or result.stdout.strip() != expected:
        raise ProjectWorktreeError(
            f"paired project worktree uses {result.stdout.strip()!r}, expected {expected!r}"
        )
    source_common = _common_git_dir(source)
    worktree_common = _common_git_dir(worktree)
    if not source_common or source_common != worktree_common:
        raise ProjectWorktreeError(
            "paired project checkout belongs to a different source repository"
        )
    upstream = _git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}")
    if upstream.returncode != 0 or not upstream.stdout.strip():
        raise ProjectWorktreeError(
            f"paired project branch {expected!r} has no recorded merge target"
        )


def _common_git_dir(worktree: Path) -> Path | None:
    result = _git(worktree, "rev-parse", "--git-common-dir")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = worktree / path
    try:
        return path.resolve()
    except OSError:
        return None


def _ref_sha(source: Path, ref: str) -> str:
    result = _git(source, "rev-parse", "--verify", ref)
    return result.stdout.strip() if result.returncode == 0 else ""


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
        raise ProjectWorktreeError(f"git {' '.join(args)} failed in {cwd}: {exc}") from exc
